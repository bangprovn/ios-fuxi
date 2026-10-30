/*
 * SWEET internal UFS root device.
 *
 * The Qualcomm UFS host (UFSHCI 2.1) is left enabled with the link up by the
 * bootloader, which loaded our boot image from it. This driver reuses that
 * state: it points the transfer request list at its own memory (verifying the
 * host took it before ringing any doorbell) and issues polled SCSI commands,
 * one at a time, to LUN 0. It finds one GPT partition by name (boot-arg
 * ufsroot=, default "userdata") and exposes it as the BSD disk ufsroot0
 * (rufsroot0), with 512-byte logical sectors over the device's 4 KiB blocks.
 * Root on it with rd=ufsroot0.
 *
 * All DMA goes through one physically contiguous allocation (descriptors and
 * a 1 MiB bounce buffer); the host is not cache coherent, so caches are
 * cleaned/invalidated around each command.
 *
 * Writes are refused unless boot-arg ufsrw=1 is set. Every write is checked
 * against the partition bounds again right before it is issued, so nothing
 * outside the named partition can be written.
 *
 * Ported from UniXNU drivers/NativeUfs/RootCommands.h (BSD-2-Clause-Patent)
 * Ported from UniXNU drivers/NativeUfs/NativeUfs.cpp (BSD-2-Clause-Patent)
 * Optional completion/quarantine and invalidate-only DMA policy; see LICENSE.NativeUfs and LICENSE.UniXNU.
 */
#if defined(ARM64_BOARD_CONFIG_SWEET)
#include "fuxi_unixnu_ufs.h"
#include <sys/param.h>
#include <sys/systm.h>
#include <sys/buf.h>
#include <sys/conf.h>
#include <sys/disk.h>
#include <sys/errno.h>
#include <sys/proc.h>
#include <sys/uio.h>
#include <miscfs/devfs/devfs.h>
#include <kern/locks.h>
#include <kern/clock.h>
#include <kern/sched_prim.h>
#include <machine/machine_routines.h>
#include <arm/caches_internal.h>
#include <vm/pmap.h>
#include <vm/vm_kern.h>
#include <pexpert/pexpert.h>
#include <pexpert/device_tree.h>
#include <libkern/libkern.h>
#include <machine/atomic.h>

extern ppnum_t pmap_find_phys(pmap_t pmap, addr64_t va);
/* osfmk/arm/machine_routines.h, not exported to the BSD layer */
extern vm_offset_t ml_io_map(vm_offset_t phys_addr, vm_size_t size);
/* osfmk/arm/cpu_data.h, not exported to the BSD layer */
extern int get_preemption_level(void);
extern kern_return_t kernel_thread_start(thread_continue_t, void *, thread_t *);
extern void thread_deallocate(thread_t);
extern int hz;
static void sweet_log_start(void);
extern void sweet_sample_start(void);
extern void sweet_keys_init(void);
extern void sweet_touch_init(void);
extern void fuxi_touch_init(void);
extern void fuxi_keys_init(void);
extern void fuxi_wlan_init(void);
extern void sweet_usb_probe(void);

#define REG_CAP         0x00
#define REG_IS          0x20
#define REG_IE          0x24
#define REG_HCS         0x30
#define REG_HCE         0x34
#define REG_UTRLBA      0x50
#define REG_UTRLBAU     0x54
#define REG_UTRLDBR     0x58
#define REG_UTRLCLR     0x5c
#define REG_UTRLRSR     0x60

#define HCS_DP          (1u << 0)
#define HCS_UTRLRDY     (1u << 1)
#define CAP_64AS        (1u << 24)              /* 64-bit DMA addressing */

#define UFS_BLOCK       4096u
#define BOUNCE_SIZE     (1u << 20)
#define PRDT_CHUNK      (256u << 10)            /* PRDT byte count field: 18 bits */
#define PRDT_MAX        (BOUNCE_SIZE / PRDT_CHUNK)
#define CMD_TIMEOUT_NS  (5ull * NSEC_PER_SEC)
#define CMD_SPIN_NS     (50ull * NSEC_PER_USEC)
#define CMD_SLEEP_NS    (200ull * NSEC_PER_USEC)

struct utrd {
	uint32_t dw0, dw1, dw2, dw3;
	uint32_t ucdba, ucdbau;
	uint32_t resp;          /* [31:16] response UPIU offset, [15:0] length, DWORDs */
	uint32_t prdt;          /* [31:16] PRDT offset (DWORDs), [15:0] entries */
};

struct ucd {
	uint8_t cmd[512];
	uint8_t resp[512];
	struct {
		uint32_t addr_lo, addr_hi, rsvd, dbc;
	} prdt[PRDT_MAX];
};

/* Layout of the contiguous DMA allocation. */
#define DMA_UTRL        0x0000                  /* 1 KiB aligned */
#define DMA_UCD         0x1000                  /* 128 byte aligned */
#define DMA_BOUNCE      0x4000
#define DMA_SIZE        (DMA_BOUNCE + BOUNCE_SIZE)

static struct {
	vm_offset_t regs;
	vm_offset_t dma;                        /* kernel VA of the DMA allocation */
	uint64_t dma_pa;
	uint8_t lun;
	uint8_t tag;
	uint64_t first_block, nblocks;          /* partition, in 4 KiB device blocks */
	uint32_t secsize;                       /* logical sector size for the BSD layer */
	bool writable;                          /* boot-arg ufsrw=1 */
	bool cache;                             /* boot-arg ufscache=1 */
	bool wait;                              /* boot-arg ufswait=1 */
	int bmajor, cmajor;
	dev_t bdev;
	bool ready;
	bool unixnu;                            /* fuxi_unixnu_ufs=1, default off */
	bool poisoned;                          /* DMA allocation retained until reboot */
} su;

#if defined(FUXI)
/*
 * Fuxi root lives in one pinned, preallocated file inside Android's f2fs
 * userdata (the user allowed only that file's blocks). ADT /chosen
 * "fuxi-root-extents" lists it as u64 triples {file block, partition block,
 * blocks} (4 KiB units, partition-relative) and "fuxi-root-blocks" is the
 * root size. ufsroot0 is the file's contents; no I/O leaves its extents.
 */
#define FX_MAX_EXTENTS 8192
static struct { uint64_t file, part, count; } fx_ext[FX_MAX_EXTENTS];
static unsigned fx_next;
static bool fx_root;                    /* extent table loaded and validated */
static uint64_t fx_part_first, fx_part_blocks;

/* Device LBA of root block lblk; *run = blocks left in that extent. */
static bool
fx_map(uint64_t lblk, uint64_t *lba, uint64_t *run)
{
	for (unsigned i = 0; i < fx_next; i++) {
		if (lblk >= fx_ext[i].file && lblk - fx_ext[i].file < fx_ext[i].count) {
			uint64_t off = lblk - fx_ext[i].file;
			*lba = fx_part_first + fx_ext[i].part + off;
			*run = fx_ext[i].count - off;
			return true;
		}
	}
	return false;
}

/* True only if device blocks [lba, lba+count) lie inside a single extent. */
static bool
fx_write_allowed(uint64_t lba, uint64_t count)
{
	if (!fx_root || count == 0) {
		return false;
	}
	for (unsigned i = 0; i < fx_next; i++) {
		uint64_t first = fx_part_first + fx_ext[i].part;
		if (lba >= first && lba - first < fx_ext[i].count &&
		    count <= fx_ext[i].count - (lba - first)) {
			return true;
		}
	}
	return false;
}

/* Load and validate the extent table against the userdata partition bounds. */
static int
fx_load_extents(void)
{
	DTEntry chosen;
	uint64_t const *ext = NULL, *blocks = NULL;
	unsigned int size = 0, bsize = 0;
	if (SecureDTLookupEntry(NULL, "/chosen", &chosen) != kSuccess ||
	    SecureDTGetProperty(chosen, "fuxi-root-extents", (void const **)&ext, &size) != kSuccess ||
	    SecureDTGetProperty(chosen, "fuxi-root-blocks", (void const **)&blocks, &bsize) != kSuccess ||
	    bsize != sizeof(uint64_t) || size == 0 || size % (3 * sizeof(uint64_t)) ||
	    size / (3 * sizeof(uint64_t)) > FX_MAX_EXTENTS) {
		printf("FUXI UFS: no valid fuxi-root-extents in /chosen\n");
		return ENXIO;
	}
	unsigned n = size / (3 * sizeof(uint64_t));
	uint64_t want = *blocks, pos = 0;
	for (unsigned i = 0; i < n && pos < want; i++) {
		uint64_t file = ext[3 * i], part = ext[3 * i + 1], count = ext[3 * i + 2];
		if (file != pos || count == 0 || part >= fx_part_blocks ||
		    count > fx_part_blocks - part) {
			printf("FUXI UFS: bad extent %u\n", i);
			return EINVAL;
		}
		count = MIN(count, want - pos);
		for (unsigned j = 0; j < fx_next; j++) {
			if (part < fx_ext[j].part + fx_ext[j].count && fx_ext[j].part < part + count) {
				printf("FUXI UFS: overlapping extents %u/%u\n", j, i);
				return EINVAL;
			}
		}
		fx_ext[fx_next].file = file;
		fx_ext[fx_next].part = part;
		fx_ext[fx_next].count = count;
		fx_next++;
		pos += count;
	}
	if (pos != want) {
		printf("FUXI UFS: extents cover 0x%llx of 0x%llx blocks\n", pos, want);
		fx_next = 0;
		return EINVAL;
	}
	fx_root = true;
	return 0;
}
#endif

static LCK_GRP_DECLARE(sweet_ufs_grp, "sweet_ufs");
static LCK_MTX_DECLARE(sweet_ufs_lock, &sweet_ufs_grp);

static inline uint32_t
ufs_rd(uint32_t off)
{
	return *(volatile uint32_t *)(su.regs + off);
}

static inline void
ufs_wr(uint32_t off, uint32_t v)
{
	*(volatile uint32_t *)(su.regs + off) = v;
}

static void
dma_sync(uint32_t off, uint32_t len)
{
	flush_dcache64((addr64_t)(su.dma_pa + off), len, TRUE);
}

static void
dma_clean(uint32_t off, uint32_t len)
{
	clean_dcache64((addr64_t)(su.dma_pa + off), len, TRUE);
}

/* All ranges live in our dedicated, permanently allocated, cache-line-aligned DMA area. */
static void
fuxi_ufs_cache(uint32_t off, uint32_t len, bool invalidate)
{
	uintptr_t start = (su.dma + off) & ~(uintptr_t)63;
	uintptr_t end = su.dma + off + len;
	__asm__ volatile("dsb sy" ::: "memory");
	for (; start < end; start += 64) {
		if (invalidate) __asm__ volatile("dc ivac, %0" :: "r"(start) : "memory");
		else __asm__ volatile("dc civac, %0" :: "r"(start) : "memory");
	}
	__asm__ volatile("dsb sy" ::: "memory");
}

/* Called with sweet_ufs_lock held, before any CPU write to shared DMA memory. */
static int
fuxi_ufs_poison(const char *reason, uint32_t db, uint32_t status, uint32_t ocs, int error)
{
	if (!su.poisoned) {
		su.poisoned = true;
		printf("FUXI UNIXNU UFS: quarantined reason=%s db=%#x is=%#x ocs=%#x; no retry until reboot\n",
		    reason, db, status, ocs);
	}
	return error;
}

static int
fuxi_ufs_check_idle(void)
{
	if (!su.unixnu) return 0;
	if (su.poisoned) return EIO;
	__asm__ volatile("dsb sy" ::: "memory");
	uint32_t db = ufs_rd(REG_UTRLDBR), status = ufs_rd(REG_IS);
	if (db || (status & FUXI_UFS_FATAL_STATUS))
		return fuxi_ufs_poison("preflight", db, status, 15, EIO);
	return 0;
}

/*
 * One SCSI command in slot 0, data (if any) to/from the bounce buffer
 * (to the device when write is set). Returns 0 or an errno.
 */
static int
ufs_scsi(const uint8_t *cdb, unsigned cdb_len, uint32_t len, bool write)
{
	if (su.unixnu && (!cdb || !cdb_len || cdb_len > 16 || len > BOUNCE_SIZE)) return EINVAL;
	int idle_error = fuxi_ufs_check_idle();
	if (idle_error) return idle_error;
#if defined(FUXI)
    /* Also permit only the exact device-WLUN ACTIVE power command used by Linux. */
    bool active_power=!write && len==0 && cdb_len==6 && su.lun==0xd0 &&
        cdb[0]==0x1b && cdb[1]==0 && cdb[2]==0 && cdb[3]==0 && cdb[4]==0x10 && cdb[5]==0;
    bool root_write=write && su.writable && su.lun==0 && cdb_len==10 && cdb[0]==0x2a &&
        fx_write_allowed((uint64_t)cdb[2]<<24|cdb[3]<<16|cdb[4]<<8|cdb[5],
            (uint64_t)cdb[7]<<8|cdb[8]) && len==((uint32_t)cdb[7]<<8|cdb[8])*UFS_BLOCK;
    bool root_sync=!write && len==0 && su.writable && fx_root && su.lun==0 &&
        cdb_len==10 && cdb[0]==0x35;
    if (!root_write && !root_sync &&
        (write || !cdb_len || (cdb[0]!=0x00 && cdb[0]!=0x28 && !active_power))) return EROFS;
#endif

	struct utrd *utrd = (struct utrd *)(su.dma + DMA_UTRL);
	struct ucd *ucd = (struct ucd *)(su.dma + DMA_UCD);
	uint64_t ucd_pa = su.dma_pa + DMA_UCD, bounce_pa = su.dma_pa + DMA_BOUNCE;

	assert(len <= BOUNCE_SIZE);
	bzero(utrd, sizeof(*utrd));
	bzero(ucd, sizeof(*ucd));

	ucd->cmd[0] = 0x01;                     /* COMMAND UPIU */
	ucd->cmd[1] = len ? (write ? 0x20 : 0x40) : 0x00;      /* W: data out, R: data in */
	ucd->cmd[2] = su.lun;
	ucd->cmd[3] = ++su.tag;
	ucd->cmd[12] = (uint8_t)(len >> 24);
	ucd->cmd[13] = (uint8_t)(len >> 16);
	ucd->cmd[14] = (uint8_t)(len >> 8);
	ucd->cmd[15] = (uint8_t)len;
	memcpy(&ucd->cmd[16], cdb, cdb_len);

	/* DD: 1 = memory to device, 2 = device to memory. */
	utrd->dw0 = (1u << 28) | ((len ? (write ? 1u : 2u) : 0u) << 25) | (1u << 24);
	utrd->dw2 = 0x0f;                       /* OCS invalid until updated */
	utrd->ucdba = (uint32_t)ucd_pa;
	utrd->ucdbau = (uint32_t)(ucd_pa >> 32);
	utrd->resp = (512u / 4) << 16 | (512u / 4);
	if (len) {
		uint32_t n = 0;
		for (uint32_t done = 0; done < len; done += PRDT_CHUNK, n++) {
			uint32_t chunk = MIN(PRDT_CHUNK, len - done);
			ucd->prdt[n].addr_lo = (uint32_t)(bounce_pa + done);
			ucd->prdt[n].addr_hi = (uint32_t)((bounce_pa + done) >> 32);
			ucd->prdt[n].dbc = chunk - 1;
		}
		utrd->prdt = (1024u / 4) << 16 | n;
	}

	if (su.unixnu) {
		fuxi_ufs_cache(DMA_UTRL, sizeof(*utrd), false);
		fuxi_ufs_cache(DMA_UCD, sizeof(*ucd), false);
		if (len) fuxi_ufs_cache(DMA_BOUNCE, len, false);
		ufs_wr(REG_IS, 1);              /* completion only; preserve error evidence */
		__asm__ volatile("dsb sy" ::: "memory");
	} else {
		if (su.cache) {
			dma_clean(DMA_UTRL, sizeof(*utrd));
			dma_clean(DMA_UCD, 16 + cdb_len);
			dma_sync(DMA_UCD + offsetof(struct ucd, resp), sizeof(ucd->resp));
			if (len) {
				dma_clean(DMA_UCD + offsetof(struct ucd, prdt),
				    (utrd->prdt & 0xffff) * sizeof(ucd->prdt[0]));
			}
		} else {
			dma_sync(DMA_UTRL, sizeof(*utrd));
			dma_sync(DMA_UCD, sizeof(*ucd));
		}
		if (len) {
			if (su.cache && write) {
				dma_clean(DMA_BOUNCE, len);
			} else {
				dma_sync(DMA_BOUNCE, len);
			}
		}
		__builtin_arm_dsb(0xf);
	}
	ufs_wr(REG_UTRLDBR, 1);
	uint64_t deadline;
	nanoseconds_to_absolutetime(CMD_TIMEOUT_NS, &deadline);
	deadline += mach_absolute_time();
	bool sleepable = su.wait && su.ready && ml_get_interrupts_enabled() &&
	    !ml_at_interrupt_context() && get_preemption_level() == 0;
	uint64_t spin_until = 0, sleep_interval = 0;
	if (sleepable) {
		nanoseconds_to_absolutetime(CMD_SPIN_NS, &spin_until);
		spin_until += mach_absolute_time();
		nanoseconds_to_absolutetime(CMD_SLEEP_NS, &sleep_interval);
	}
	for (;;) {
		uint32_t db = ufs_rd(REG_UTRLDBR);
		uint64_t now = mach_absolute_time();
		if (su.unixnu) {
			__asm__ volatile("dsb sy" ::: "memory");
			uint32_t status = ufs_rd(REG_IS), ocs = 15;
			if (!(db & 1)) {
				fuxi_ufs_cache(DMA_UTRL, sizeof(*utrd), true);
				ocs = *(volatile uint32_t *)&utrd->dw2 & 0xff;
			}
			enum fuxi_ufs_end end = fuxi_unixnu_ufs_end(db, status, ocs, now >= deadline);
			if (end == FUXI_UFS_COMPLETE) break;
			if (end == FUXI_UFS_FATAL || end == FUXI_UFS_TIMEOUT)
				return fuxi_ufs_poison(end == FUXI_UFS_FATAL ? "fatal" : "timeout",
				    db, status, ocs, end == FUXI_UFS_TIMEOUT ? ETIMEDOUT : EIO);
		} else {
			if (!(db & 1)) break;
			if (now > deadline) {
				ufs_wr(REG_UTRLCLR, ~1u);
				printf("SWEET UFS: command 0x%02x timed out\n", cdb[0]);
				return ETIMEDOUT;
			}
		}
		if (sleepable && now >= spin_until) {
			uint64_t wake = now + MIN(sleep_interval, deadline - now);
			/* Keep the mutex: slot 0 and the bounce buffer are still owned. */
			if (assert_wait_deadline_with_leeway((event_t)&su.wait, THREAD_UNINT,
			    TIMEOUT_URGENCY_SYS_CRITICAL, wake, 0) == THREAD_WAITING) {
				thread_block(THREAD_CONTINUE_NULL);
			}
		} else {
			delay(1);
		}
	}
	__builtin_arm_dsb(0xf);
	if (su.unixnu) {
		ufs_wr(REG_IS, 1);
		fuxi_ufs_cache(DMA_UTRL, sizeof(*utrd), true);
		fuxi_ufs_cache(DMA_UCD, sizeof(*ucd), true);
		if (len && !write) fuxi_ufs_cache(DMA_BOUNCE, len, true);
		if (!(utrd->dw2 & 0xff) && !fuxi_unixnu_ufs_response(ucd->resp, su.lun, su.tag))
			return fuxi_ufs_poison("response", 0, ufs_rd(REG_IS), utrd->dw2 & 0xff, EIO);
	} else {
		ufs_wr(REG_IS, ufs_rd(REG_IS));         /* acknowledge completion status */

		dma_sync(DMA_UTRL, sizeof(*utrd));
		if (su.cache) {
			dma_sync(DMA_UCD + offsetof(struct ucd, resp), sizeof(ucd->resp));
		} else {
			dma_sync(DMA_UCD, sizeof(*ucd));
		}
		if (len && !(su.cache && write)) {
			dma_sync(DMA_BOUNCE, len);
		}
	}

	uint32_t ocs = utrd->dw2 & 0xff;
	if (ocs || ucd->resp[0] != 0x21 || ucd->resp[6] || ucd->resp[7]) {
		printf("SWEET UFS: command 0x%02x failed: ocs 0x%x upiu 0x%x response 0x%x status 0x%x\n",
		    cdb[0], ocs, ucd->resp[0], ucd->resp[6], ucd->resp[7]);
#if defined(FUXI)
        const uint32_t *r=(const uint32_t *)ucd->resp;
        printf("FUXI UFS response 00: %08x %08x %08x %08x %08x %08x %08x %08x\n",
            r[0],r[1],r[2],r[3],r[4],r[5],r[6],r[7]);
        printf("FUXI UFS response 20: %08x %08x %08x %08x %08x %08x %08x %08x\n",
            r[8],r[9],r[10],r[11],r[12],r[13],r[14],r[15]);
#endif
		return EIO;
	}
	return 0;
}

/* READ(10) of count device blocks at lba into the bounce buffer. */
static int
ufs_read_blocks(uint64_t lba, uint32_t count)
{
	if (lba > UINT32_MAX || count > BOUNCE_SIZE / UFS_BLOCK) {
		return EINVAL;
	}
	uint8_t cdb[10] = {
		0x28, 0,
		(uint8_t)(lba >> 24), (uint8_t)(lba >> 16), (uint8_t)(lba >> 8), (uint8_t)lba,
		0, (uint8_t)(count >> 8), (uint8_t)count, 0
	};
	return ufs_scsi(cdb, sizeof(cdb), count * UFS_BLOCK, false);
}

/*
 * WRITE(10) of count device blocks from the bounce buffer to lba. The range
 * must lie inside the partition: this is the only place data goes out.
 */
static int
ufs_write_blocks(uint64_t lba, uint32_t count)
{
#if defined(FUXI)
	if (!su.writable || !fx_write_allowed(lba, count) || count > BOUNCE_SIZE / UFS_BLOCK ||
	    lba + count - 1 > UINT32_MAX) {
#else
	if (!su.writable || !su.nblocks || count == 0 || count > BOUNCE_SIZE / UFS_BLOCK ||
	    lba < su.first_block || lba - su.first_block >= su.nblocks ||
	    count > su.nblocks - (lba - su.first_block) || lba + count - 1 > UINT32_MAX) {
#endif
		printf("SWEET UFS: refusing write of %u blocks at 0x%llx\n", count, lba);
		return EPERM;
	}
	uint8_t cdb[10] = {
		0x2a, 0,
		(uint8_t)(lba >> 24), (uint8_t)(lba >> 16), (uint8_t)(lba >> 8), (uint8_t)lba,
		0, (uint8_t)(count >> 8), (uint8_t)count, 0
	};
	return ufs_scsi(cdb, sizeof(cdb), count * UFS_BLOCK, true);
}

static int
ufs_sync_cache(void)
{
	static const uint8_t cdb[10] = { 0x35 };       /* SYNCHRONIZE CACHE(10), whole LUN */
	return ufs_scsi(cdb, sizeof(cdb), 0, false);
}

static int
ufs_find_partition(const char *want)
{
	const uint8_t *b = (const uint8_t *)(su.dma + DMA_BOUNCE);
	int error = ufs_read_blocks(1, 1);
	if (error) {
		return error;
	}
	if (memcmp(b, "EFI PART", 8) != 0) {
		printf("SWEET UFS: LUN %u has no GPT\n", su.lun);
		return ENXIO;
	}
	uint64_t entry_lba;
	uint32_t nent, esz;
	memcpy(&entry_lba, b + 72, 8);
	memcpy(&nent, b + 80, 4);
	memcpy(&esz, b + 84, 4);
	if (esz != 128 || nent == 0 || nent * esz > BOUNCE_SIZE) {
		printf("SWEET UFS: unsupported GPT entries %u x %u\n", nent, esz);
		return ENXIO;
	}
	error = ufs_read_blocks(entry_lba, (nent * esz + UFS_BLOCK - 1) / UFS_BLOCK);
	if (error) {
		return error;
	}
	for (uint32_t i = 0; i < nent; i++) {
		const uint8_t *e = b + i * esz;
		char name[37];
		unsigned c;
		for (c = 0; c < 36; c++) {
			uint16_t ch = (uint16_t)(e[56 + 2 * c] | e[57 + 2 * c] << 8);
			if (ch == 0 || ch > 0x7f) {
				break;
			}
			name[c] = (char)ch;
		}
		name[c] = 0;
		if (strcmp(name, want) != 0) {
			continue;
		}
		uint64_t first, last;
		memcpy(&first, e + 32, 8);
		memcpy(&last, e + 40, 8);
		if (last < first) {
			return ENXIO;
		}
		su.first_block = first;
		su.nblocks = last - first + 1;
		return 0;
	}
	printf("SWEET UFS: no GPT partition named \"%s\"\n", want);
	return ENOENT;
}

static int
ufs_take_over_host(void)
{
	uint32_t hce = ufs_rd(REG_HCE), hcs = ufs_rd(REG_HCS);
	if (!(hce & 1) || !(hcs & HCS_DP) || !(hcs & HCS_UTRLRDY)) {
		printf("SWEET UFS: host not ready (hce 0x%x hcs 0x%x)\n", hce, hcs);
		return ENXIO;
	}
	if (ufs_rd(REG_UTRLDBR)) {
		printf("SWEET UFS: requests outstanding from the bootloader\n");
		return EBUSY;
	}
	/* With RAM above 4 GiB (mkadt.py --highmem) the DMA memory can be there too. */
	if (((su.dma_pa + DMA_SIZE - 1) >> 32) && !(ufs_rd(REG_CAP) & CAP_64AS)) {
		printf("SWEET UFS: DMA memory at 0x%llx but the host has no 64-bit addressing\n",
		    su.dma_pa);
		return ENXIO;
	}
	int idle_error = fuxi_ufs_check_idle();
	if (idle_error) return idle_error;
	ufs_wr(REG_IE, 0);                      /* polled operation */
	ufs_wr(REG_IS, su.unixnu ? 1 : ufs_rd(REG_IS));
	ufs_wr(REG_UTRLRSR, 0);
	uint64_t list = su.dma_pa + DMA_UTRL;
	ufs_wr(REG_UTRLBA, (uint32_t)list);
	ufs_wr(REG_UTRLBAU, (uint32_t)(list >> 32));
	ufs_wr(REG_UTRLRSR, 1);
	/* Never ring a doorbell unless the host uses our list. */
	if (ufs_rd(REG_UTRLBA) != (uint32_t)list || ufs_rd(REG_UTRLBAU) != (uint32_t)(list >> 32) ||
	    !(ufs_rd(REG_UTRLRSR) & 1)) {
		printf("SWEET UFS: host did not take our request list\n");
		return EIO;
	}
	return 0;
}

static int
sweet_ufs_open(__unused dev_t dev, int flags, __unused int devtype, __unused struct proc *p)
{
	if (!su.ready) {
		return ENXIO;
	}
	return ((flags & FWRITE) && !su.writable) ? EROFS : 0;
}

static int
sweet_ufs_close(__unused dev_t dev, __unused int flags, __unused int devtype, __unused struct proc *p)
{
	return 0;
}

static void
sweet_ufs_strategy(buf_t bp)
{
	int error = 0;
	caddr_t vaddr = NULL;
	uint64_t off = (uint64_t)buf_blkno(bp) * su.secsize;
	uint32_t count = buf_count(bp);
	uint64_t part_bytes = su.nblocks * UFS_BLOCK;

	bool rd = buf_flags(bp) & B_READ;

	buf_setresid(bp, count);
	if (!rd && !su.writable) {
		error = EROFS;
		goto done;
	}
	if (off > part_bytes || count > part_bytes - off) {
		error = EINVAL;
		goto done;
	}
	if (count == 0) {
		goto done;
	}
	if (buf_map(bp, &vaddr)) {
		error = EIO;
		goto done;
	}

	lck_mtx_lock(&sweet_ufs_lock);
	for (uint32_t done = 0; done < count;) {
		if ((error = fuxi_ufs_check_idle())) break;
		uint64_t pos = off + done;
		uint64_t lba = pos / UFS_BLOCK;
		uint32_t skip = (uint32_t)(pos % UFS_BLOCK);
		uint32_t want = MIN(count - done, BOUNCE_SIZE - skip);
		uint32_t blocks = (skip + want + UFS_BLOCK - 1) / UFS_BLOCK;
		void *bounce = (void *)(su.dma + DMA_BOUNCE);
		uint64_t dev_lba = su.first_block + lba;
#if defined(FUXI)
		if (fx_root) {
			uint64_t run;
			if (!fx_map(lba, &dev_lba, &run)) {
				error = EIO;
				break;
			}
			if (blocks > run) {
				blocks = (uint32_t)run;
				want = blocks * UFS_BLOCK - skip;
			}
		}
#endif
		if (rd) {
			error = ufs_read_blocks(dev_lba, blocks);
			if (!error) {
				memcpy(vaddr + done, (const char *)bounce + skip, want);
			}
		} else {
			/* Partial device blocks at either end: read, merge, write back. */
			if (skip || (want % UFS_BLOCK)) {
				error = ufs_read_blocks(dev_lba, blocks);
			}
			if (!error) {
				memcpy((char *)bounce + skip, vaddr + done, want);
				error = ufs_write_blocks(dev_lba, blocks);
			}
		}
		if (error) {
			break;
		}
		done += want;
		buf_setresid(bp, count - done);
	}
	lck_mtx_unlock(&sweet_ufs_lock);
	buf_unmap(bp);

done:
	if (error) {
		buf_seterror(bp, error);
	}
	buf_biodone(bp);
}

static int
sweet_ufs_ioctl(__unused dev_t dev, u_long cmd, caddr_t data, __unused int flag,
    __unused struct proc *p, int is_char)
{
	uint32_t *f = (uint32_t *)data;
	uint64_t *o = (uint64_t *)data;

	switch (cmd) {
	case DKIOCGETBLOCKSIZE:
		*f = su.secsize;
		return 0;
	case DKIOCSETBLOCKSIZE:
		if (is_char) {
			return ENODEV;
		}
		if (*f < DEV_BSIZE || *f > UFS_BLOCK || (*f & (*f - 1))) {
			return EINVAL;
		}
		su.secsize = *f;
		return 0;
	case DKIOCGETPHYSICALBLOCKSIZE:
		*f = UFS_BLOCK;
		return 0;
	case DKIOCGETBLOCKCOUNT:
		*o = su.nblocks * UFS_BLOCK / su.secsize;
		return 0;
	case DKIOCISWRITABLE:
		*f = su.writable;
		return 0;
	case DKIOCSYNCHRONIZE:
	case DKIOCSYNCHRONIZECACHE: {
		if (!su.writable) {
			return 0;
		}
		lck_mtx_lock(&sweet_ufs_lock);
		int error = ufs_sync_cache();
		lck_mtx_unlock(&sweet_ufs_lock);
		return error;
	}
	case DKIOCGETMAXBLOCKCOUNTREAD:
		*o = BOUNCE_SIZE / su.secsize;
		return 0;
	case DKIOCGETMAXBLOCKCOUNTWRITE:
		*o = su.writable ? BOUNCE_SIZE / su.secsize : 0;
		return 0;
	case DKIOCGETMAXSEGMENTCOUNTREAD:
	case DKIOCGETMAXSEGMENTCOUNTWRITE:
		*o = 1;
		return 0;
	default:
		return ENOTTY;
	}
}

static int
sweet_ufs_bioctl(dev_t dev, u_long cmd, caddr_t data, int flag, struct proc *p)
{
	return sweet_ufs_ioctl(dev, cmd, data, flag, p, 0);
}

static int
sweet_ufs_cioctl(dev_t dev, u_long cmd, caddr_t data, int flag, struct proc *p)
{
	return sweet_ufs_ioctl(dev, cmd, data, flag, p, 1);
}

static int
sweet_ufs_psize(__unused dev_t dev)
{
	return su.ready ? (int)MIN(su.nblocks * UFS_BLOCK / su.secsize, INT_MAX) : -1;
}

static int
sweet_ufs_read(dev_t dev, struct uio *uio, __unused int ioflag)
{
	return physio(sweet_ufs_strategy, NULL, dev, B_READ, minphys, uio, (int)su.secsize);
}

static int
sweet_ufs_write(dev_t dev, struct uio *uio, __unused int ioflag)
{
	if (!su.writable) {
		return EROFS;
	}
	return physio(sweet_ufs_strategy, NULL, dev, B_WRITE, minphys, uio, (int)su.secsize);
}

static const struct bdevsw sweet_ufs_bdevsw = {
	.d_open     = sweet_ufs_open,
	.d_close    = sweet_ufs_close,
	.d_strategy = sweet_ufs_strategy,
	.d_ioctl    = sweet_ufs_bioctl,
	.d_dump     = eno_dump,
	.d_psize    = sweet_ufs_psize,
	.d_type     = D_DISK,
};

static const struct cdevsw sweet_ufs_cdevsw = {
	.d_open       = sweet_ufs_open,
	.d_close      = sweet_ufs_close,
	.d_read       = sweet_ufs_read,
	.d_write      = sweet_ufs_write,
	.d_ioctl      = sweet_ufs_cioctl,
	.d_stop       = eno_stop,
	.d_reset      = eno_reset,
	.d_ttys       = NULL,
	.d_select     = eno_select,
	.d_mmap       = eno_mmap,
	.d_strategy   = eno_strat,
	.d_reserved_1 = eno_getc,
	.d_reserved_2 = eno_putc,
	.d_type       = D_DISK,
};

static int
sweet_ufs_init(void)
{
	if (su.poisoned) return EIO;
#if defined(FUXI)
    uint32_t requested=0;
    if (!PE_parse_boot_argn("fuxi_ufs_probe", &requested, sizeof(requested)) || requested!=1)
        return ENXIO;
    vm_offset_t gcc=ml_io_map(0x177000,0x1000);
    if (!gcc) return ENOMEM;
    volatile uint32_t *ahb=(volatile uint32_t *)(gcc+0x24);
    uint32_t old=*ahb;
    if (!(old&1) || !(*(volatile uint32_t *)(gcc+4)&0x80000000) ||
        ((*(volatile uint32_t *)(gcc+0x18)|*(volatile uint32_t *)(gcc+0x68))&0x80000000)) {
        printf("FUXI UFS: power/AXI/UniPro not ready\n"); return ENXIO;
    }
    *ahb=old&~2u; /* Disable only automatic AHB gating, verified by native probe-09. */
    __asm__ volatile("dsb sy" ::: "memory");
    uint64_t deadline;
    clock_interval_to_deadline(1,NSEC_PER_MSEC,&deadline);
    while ((*ahb&0x80000000) && mach_absolute_time()<deadline) __asm__ volatile("yield");
    if (*ahb&0x80000000) { *ahb=old; return ETIMEDOUT; }
#endif
	DTEntry node;
	uintptr_t const *reg = NULL;
	unsigned int size = 0;
	char want[64] = "userdata";
	int error;


#if defined(FUXI)
    const char *ufs_node="/fuxi-ufs";
#else
    const char *ufs_node="/sweet-ufs";
#endif
    if (SecureDTLookupEntry(NULL, ufs_node, &node) != kSuccess ||
	    SecureDTGetProperty(node, "reg", (void const **)&reg, &size) != kSuccess ||
	    size < 2 * sizeof(uintptr_t)) {
		printf("SWEET UFS: no /sweet-ufs node in the device tree\n");
		return ENXIO;
	}
	PE_parse_boot_argn("ufsroot", want, sizeof(want));
	uint32_t rw = 0;
	PE_parse_boot_argn("ufsrw", &rw, sizeof(rw));
#if defined(FUXI)
    /* Writes need the extent-mapped root on userdata (checked after the GPT read). */
    if (strcmp(want, "userdata") != 0) rw=0;
#endif
	uint32_t cache = 0, wait = 0;
	PE_parse_boot_argn("ufscache", &cache, sizeof(cache));
	PE_parse_boot_argn("ufswait", &wait, sizeof(wait));
	su.cache = (cache == 1);
	su.wait = (wait == 1);
#if defined(FUXI)
	uint32_t unixnu = 0;
	PE_parse_boot_argn("fuxi_unixnu_ufs", &unixnu, sizeof(unixnu));
	su.unixnu = (unixnu == 1);
	if (su.unixnu) printf("FUXI UNIXNU UFS: guarded completion, fatal/timeout quarantine enabled\n");
#endif

	su.regs = ml_io_map(reg[0], reg[1]);
	if (kmem_alloc_contig(kernel_map, &su.dma, DMA_SIZE, PAGE_MASK, 0, 0,
	    KMA_DATA | KMA_ZERO | KMA_PERMANENT, VM_KERN_MEMORY_IOKIT) != KERN_SUCCESS) {
		printf("SWEET UFS: no contiguous DMA memory\n");
		return ENOMEM;
	}
	ppnum_t pn = pmap_find_phys(kernel_pmap, (addr64_t)su.dma);
	if (!pn) {
		return ENOMEM;
	}
	su.dma_pa = ptoa_64(pn);
	su.lun = 0;
	su.secsize = DEV_BSIZE;

	lck_mtx_lock(&sweet_ufs_lock);
#if defined(FUXI)
	printf("FUXI UFS diagnostic: CAP=0x%x HCS=0x%x HCE=0x%x DMA=0x%llx\n",
        ufs_rd(REG_CAP),ufs_rd(REG_HCS),ufs_rd(REG_HCE),su.dma_pa);
#endif
    error = ufs_take_over_host();
#if defined(FUXI)
    if (!error) {
        static const uint8_t active[6]={0x1b,0,0,0,0x10,0};
        su.lun=0xd0; /* UFS device well-known LUN, not a block-data LUN. */
        for (int i=0;i<3 && (error=ufs_scsi(active,sizeof(active),0,false));i++) { }
        su.lun=0;
        printf("FUXI UFS device ACTIVE power command result=%d\n",error);
    }
#endif
	if (!error) {
		static const uint8_t tur[6] = { 0x00 };
		/* The first command after the bootloader may report a unit attention. */
		for (int i = 0; i < 3 && (error = ufs_scsi(tur, sizeof(tur), 0, false)); i++) {
		}
	}
	if (!error) {
		error = ufs_find_partition(want);
	}
#if defined(FUXI)
	if (!error && strcmp(want, "userdata") == 0) {
		fx_part_first = su.first_block;
		fx_part_blocks = su.nblocks;
		error = fx_load_extents();
		if (!error) {
			/* ufsroot0 is now the pinned file only. */
			su.nblocks = fx_ext[fx_next - 1].file + fx_ext[fx_next - 1].count;
			printf("FUXI UFS: root file in userdata: %u extents, %llu blocks\n",
			    fx_next, su.nblocks);
		}
	}
	if (!fx_root) rw=0;
#endif
	lck_mtx_unlock(&sweet_ufs_lock);
	if (error) {
		return error;
	}

	su.bmajor = bdevsw_add(-1, &sweet_ufs_bdevsw);
	if (su.bmajor < 0) {
		return ENXIO;
	}
	su.cmajor = cdevsw_add_with_bdev(-1, &sweet_ufs_cdevsw, su.bmajor);
	if (su.cmajor < 0) {
		return ENXIO;
	}
	su.bdev = makedev(su.bmajor, 0);
	devfs_make_node(su.bdev, DEVFS_BLOCK, UID_ROOT, GID_OPERATOR, 0640, "ufsroot0");
	devfs_make_node(makedev(su.cmajor, 0), DEVFS_CHAR, UID_ROOT, GID_OPERATOR, 0640, "rufsroot0");
	/* Only now, with the partition bounds known, can writes be allowed. */
	su.writable = (rw != 0);
	su.ready = true;
	printf("SWEET UFS: \"%s\" on LUN %u: first block 0x%llx, %llu MiB, %s, DMA at 0x%llx\n",
	    want, su.lun, su.first_block, su.nblocks * UFS_BLOCK >> 20,
	    su.writable ? "read-write" : "read-only", su.dma_pa);

#if defined(FUXI)
	/*
	 * Stack sampler only, and only on request. sweet_log_start() stays off: it
	 * would write the tail of ufsroot0, which here is the HFS image itself.
	 */
	char smp[8];
	if (PE_parse_boot_argn("sweetsample", smp, sizeof(smp)) && smp[0] != '-') {
		sweet_sample_start();
	}
	/* Touch (bsd/dev/fuxi_touch.c; rails voted by the loader). */
	fuxi_touch_init();
	/* Power / volume keys (bsd/dev/fuxi_keys.c, SPMI arbiter v7). */
	fuxi_keys_init();
	/* WCN7850 PCIe0/MHI probe, boot-arg fuxiwlan (bsd/dev/fuxi_wlan.c). */
	fuxi_wlan_init();
#else
	sweet_log_start();
	sweet_sample_start();
	/* Input drivers (HID services in iokit/Kernel/arm/hid/SweetHID.cpp). */
	sweet_keys_init();
	sweet_touch_init();
	sweet_usb_probe();
#endif
	return 0;
}

#if defined(FUXI)
void fuxi_storage_probe(void);
void fuxi_storage_probe(void)
{
 uint32_t requested=0;
 if (!PE_parse_boot_argn("fuxi_ufs_probe",&requested,sizeof(requested)) || requested!=1) return;
 int error=sweet_ufs_init();
 printf("FUXI UFS read-only probe result=%d\n",error);
 if (!error) printf("FUXI UFS GPT bounds: first=0x%llx count=0x%llx (4K blocks)\n",su.first_block,su.nblocks);
}
#endif

/*
 * Persistent console log ("sweetlog"). Filming the phone screen is the only
 * other way to see the console, so every character that reaches the
 * console (kernel printf and /dev/console writes of daemons, both through
 * _cnputs) is also kept in a 4 MiB ring, and once the partition is known
 * and writable a thread copies new ring blocks to the LAST 4 MiB + 4 KiB of
 * the partition every ufslogsecs seconds (default 10), far behind the HFS
 * image at its start. Device block 0 of that area is a header:
 *   "SWEETLOG" | u32 version 1 | u32 ring size | u64 total bytes written
 *   | u64 flush sequence
 * ring offset of byte n = n % ring size. Read it from a recovery with
 * tools/sweet-log-pull.sh. Disabled with boot-arg sweetlog=0.
 */
#define SWEET_LOG_SIZE          (4u << 20)
#define SWEET_LOG_BLOCKS        (1 + SWEET_LOG_SIZE / UFS_BLOCK)
#define SWEET_LOG_SECS          10u

static char sweet_log_ring[SWEET_LOG_SIZE];
static uint64_t _Atomic sweet_log_total;
static int sweet_log_chan;
static uint32_t sweet_log_secs = SWEET_LOG_SECS;

/* Called for all console output, any context: never blocks. */
void sweet_log_append(const char *c, int size);

void
sweet_log_append(const char *c, int size)
{
	if (size <= 0) {
		return;
	}
	uint64_t at = os_atomic_add_orig(&sweet_log_total, (uint64_t)size, relaxed);
	for (int i = 0; i < size; i++) {
        uint64_t slot=(at+(uint64_t)i)%SWEET_LOG_SIZE;
        sweet_log_ring[slot]=c[i];
#if defined(FUXI)
        /* Publish to the isolated native USB monitor, which cannot snoop XNU's cache. */
        if (((uintptr_t)&sweet_log_ring[slot] & 63)==63 || slot==SWEET_LOG_SIZE-1 || i==size-1)
            __asm__ volatile("dc cvac, %0" :: "r"(&sweet_log_ring[slot]) : "memory");
#endif
    }
#if defined(FUXI)
    __asm__ volatile("dc cvac, %0; dsb sy" :: "r"(&sweet_log_total) : "memory");
#endif
}

/*
 * Copy console bytes [*pos, total) (at most max) for live log streaming
 * (sweet_usb.c). A reader that fell more than the ring behind skips ahead.
 * Returns the byte count; *pos advances.
 */
uint32_t sweet_log_copy(uint64_t *pos, char *out, uint32_t max);

uint32_t
sweet_log_copy(uint64_t *pos, char *out, uint32_t max)
{
	uint64_t total = os_atomic_load(&sweet_log_total, relaxed);
	if (total - *pos > SWEET_LOG_SIZE) {
		*pos = total - SWEET_LOG_SIZE;
	}
	uint32_t n = (uint32_t)MIN((uint64_t)max, total - *pos);
	for (uint32_t i = 0; i < n; i++) {
		out[i] = sweet_log_ring[(*pos + i) % SWEET_LOG_SIZE];
	}
	*pos += n;
	return n;
}

uint64_t sweet_log_position(void);

uint64_t
sweet_log_position(void)
{
	return os_atomic_load(&sweet_log_total, relaxed);
}

struct sweet_log_header {
	char magic[8];
	uint32_t version, size;
	uint64_t total, seq;
};

static void
sweet_log_thread(__unused void *arg, __unused wait_result_t wr)
{
	uint64_t base = su.first_block + su.nblocks - SWEET_LOG_BLOCKS;
	uint64_t flushed = 0, seq = 0;
	void *bounce = (void *)(su.dma + DMA_BOUNCE);

	printf("SWEET LOG: console log to the last %u KiB of the partition (block 0x%llx)\n",
	    SWEET_LOG_BLOCKS * UFS_BLOCK >> 10, base);
	for (;;) {
		(void)tsleep(&sweet_log_chan, PZERO, "sweetlog", hz * (int)sweet_log_secs);
		uint64_t total = os_atomic_load(&sweet_log_total, relaxed);
		if (total == flushed) {
			continue;
		}
		uint64_t pos = total - flushed >= SWEET_LOG_SIZE ? total - SWEET_LOG_SIZE : flushed;
		int error = 0;

		lck_mtx_lock(&sweet_ufs_lock);
		error = fuxi_ufs_check_idle();
		while (pos < total && !error) {
			uint32_t off = (uint32_t)(pos % SWEET_LOG_SIZE);
			uint32_t blk = off / UFS_BLOCK;
			uint64_t need = ((uint64_t)off % UFS_BLOCK + (total - pos) + UFS_BLOCK - 1) / UFS_BLOCK;
			uint32_t n = (uint32_t)MIN(MIN(need, (uint64_t)(SWEET_LOG_SIZE / UFS_BLOCK - blk)),
			    (uint64_t)(BOUNCE_SIZE / UFS_BLOCK));
			memcpy(bounce, sweet_log_ring + (uint64_t)blk * UFS_BLOCK, n * UFS_BLOCK);
			error = ufs_write_blocks(base + 1 + blk, n);
			pos += (uint64_t)(blk + n) * UFS_BLOCK - off;
		}
		if (!error) {
			struct sweet_log_header h = {
				.magic = { 'S', 'W', 'E', 'E', 'T', 'L', 'O', 'G' },
				.version = 1, .size = SWEET_LOG_SIZE, .total = total, .seq = ++seq,
			};
			bzero(bounce, UFS_BLOCK);
			memcpy(bounce, &h, sizeof(h));
			error = ufs_write_blocks(base, 1);
		}
		if (!error) {
			error = ufs_sync_cache();
		}
		lck_mtx_unlock(&sweet_ufs_lock);
		if (error) {
			printf("SWEET LOG: write failed (%d), stopping\n", error);
			return;
		}
		flushed = total;
	}
}

static void
sweet_log_start(void)
{
	uint32_t on = 1;
	thread_t thread;

	PE_parse_boot_argn("sweetlog", &on, sizeof(on));
	if (!on || !su.writable || su.nblocks < 2 * SWEET_LOG_BLOCKS) {
		return;
	}
	PE_parse_boot_argn("ufslogsecs", &sweet_log_secs, sizeof(sweet_log_secs));
	if (sweet_log_secs == 0 || sweet_log_secs > 60) {
		sweet_log_secs = SWEET_LOG_SECS;
	}
	if (kernel_thread_start(sweet_log_thread, NULL, &thread) == KERN_SUCCESS) {
		thread_deallocate(thread);
	}
}

/* Root device for rd=ufsroot0 (IOFindBSDRoot). */
int sweet_ufs_rootdev(dev_t *dev);

int
sweet_ufs_rootdev(dev_t *dev)
{
	if (!su.ready) {
		int error = sweet_ufs_init();
		if (error) {
			return error;
		}
	}
	*dev = su.bdev;
	return 0;
}
#endif /* ARM64_BOARD_CONFIG_SWEET */
