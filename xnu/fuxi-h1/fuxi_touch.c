/*
 * FUXI touch: ST FTS (FTM5) on QUP1 SE4, SPI through QUP1 GPI DMA (polled).
 * Hardware facts and protocol: research/touch/hw-report.md; first proven in
 * the loader (shim/fuxi-probe/probe.c request 0x55). The loader votes the
 * RPMh rails (ldob12 vdd 1.8 V, ldom6 avdd 3.2 V) before the XNU handoff;
 * this driver does not talk to RPMh. SE4 is GSI-only (GENI_IF_DISABLE_RO),
 * so transfers use GPII 0 (TX channel 0, RX channel 1, one event ring), with
 * apps-smmu SMRs for streams 0xb6 (GPI) and 0xa3 (QUP1) routed to context
 * bank 4 (stage 1 off), as the loader does (research/touch/gpi-recipe.md,
 * ts-12). It takes the IC through its reset,
 * waits for the controller-ready event, enables multi-touch scanning and
 * polls the event FIFO while GPIO25 (IRQ, active low) is asserted.
 * No firmware upload: the FTS keeps its firmware in its own flash.
 *
 * Interrupt (default): GPIO25 through the TLMM summary interrupt (GIC SPI
 * 208) wakes the thread, which reads the FIFO only then; see "Touch
 * interrupt" below. If that cannot be set up, or the interrupt does not come
 * while the line says there is data, the driver polls as follows, for the
 * rest of the boot (boot-arg fuxitouch_irq=0 polls from the start).
 *
 * Polling (0093): every 4 ms while a finger is down or the IRQ line was
 * seen recently, every 8 ms when idle and every 32 ms while the display is
 * off (AppleCLCD RequestPowerChange(0)); deadlines are absolute and use the
 * critical timer urgency, so no coalescing leeway is added. Touches are not
 * reported while the display is off (boot-arg fuxitouch_offsuppress=0 keeps
 * reporting). Boot-arg fuxitouch_poll_us sets the active period (default
 * 4000), fuxitouch_idle_us the idle one (default 8000).
 *
 * Tap-to-wake (H1, boot-arg fuxitaptowake=1, default off): a new contact
 * while the display is off asks bsd/dev/fuxi_keys.c for a power press,
 * which it sends only while backboardd's TouchDetectionMode is 0xfe. After
 * such a press the contacts of that off period stay hidden from iOS until
 * they lift. Without a wake press nothing changes.
 * Ported from HackiOS hackios-xnu-userland userland/src/inject.c (MIT):
 * the waking touch is kept from iOS. MIT notice in fuxi_key_state.h.
 *
 * Report batching/counters: adapted from UniXNU/NativeHidInput,
 * 75efe1c, e43c254, 22f2dd4 and ba03e5d (BSD-2-Clause-Patent).
 * Notice: iokit/Kernel/arm/hid/LICENSE.NativeHidInput. fuxitbatch=1
 * sends the complete ID-indexed contact set in one digitizer collection.
 */
#if defined(FUXI)
#include <sys/param.h>
#include <sys/systm.h>
#include <sys/errno.h>
#include <kern/clock.h>
#include <kern/kern_types.h>
#include <kern/thread.h>
#include <kern/sched_prim.h>
#include <machine/machine_routines.h>
#include <pexpert/pexpert.h>
#include <vm/vm_kern.h>
#include <vm/pmap.h>
#include <arm/caches_internal.h>
#include "fuxi_key_state.h"

extern vm_offset_t ml_io_map(vm_offset_t, vm_size_t);
extern kern_return_t thread_terminate(thread_t);
extern void sweet_hid_touch(int finger, int down, int x, int y);
extern int sweet_hid_touch_report(uint32_t mask, const int *x, const int *y);
extern ppnum_t pmap_find_phys(pmap_t pmap, addr64_t va);
extern kern_return_t kernel_thread_start_priority(thread_continue_t continuation,
    void *parameter, integer_t priority, thread_t *new_thread);
extern int sweet_fb_display_off;       /* iokit/Kernel/arm/SweetFramebuffer.cpp */
extern bool fuxi_keys_touch_wake(void); /* bsd/dev/fuxi_keys.c */
extern processor_t cpu_to_processor(int);
extern processor_t thread_bind(processor_t);
void fuxi_touch_init(void);

#define FT_SE_PA     0x00a90000u
#define FT_GPI_PA    0x00a10000u   /* GPII 0: ee base 0x9f0000 + 0x20000 */
#define FT_SMMU_PA   0x15000000u
#define FT_GCC_PA    0x00100000u
#define FT_TLMM_PA   0x0f100000u   /* upstream tlmm@f100000; 0x0f0xxxxx aborts */
#define FT_GPIO(n)   ((uint32_t)(n) * 0x1000u)
#define FT_RESET     24u
#define FT_IRQ       25u
#define FT_MAX       10u
#define FT_EV_READY  0x03u
#define FT_EV_ENTER  0x13u
#define FT_EV_MOTION 0x23u
#define FT_EV_LEAVE  0x33u
#define FT_TYPE_HOVER 0x05u        /* ftsSoftware.h TOUCH_TYPE_HOVER: not touching */
#define FT_PRIORITY  92            /* BASEPRI_PREEMPT */

static struct {
	vm_offset_t se, gcc, tlmm, gpi, smmu;
	vm_offset_t dma;
	uint64_t dma_pa;
	unsigned depth, level, wp[2], evrp;
	bool started;
	unsigned batch;              /* opt-in report-wide HID collection */
	bool retry;                  /* latest set not yet constructed by HID */
	uint32_t active;
	int x[FT_MAX], y[FT_MAX];
	bool ic_reset;                 /* controller-ready event while sensing */
	unsigned fifo_hits;            /* FIFO reads that found an event (interrupt checks) */
	/* Per-contact statistics (first finger down -> last finger up). */
	uint64_t c_start, c_last, c_poll_sum, c_poll_max;
	unsigned c_reports, c_polls, contacts;
	unsigned c_multi, c_max, c_lifts;
	struct fuxi_wake_touch wake;   /* tap-to-wake contact filter */
} ft;
static int ft_chan;

static uint32_t
ft_rd(vm_offset_t base, uint32_t off)
{
	uint32_t v = *(volatile uint32_t *)(base + off);
	__asm__ volatile("dmb oshld" ::: "memory");
	return v;
}

static void
ft_wr(vm_offset_t base, uint32_t off, uint32_t v)
{
	__asm__ volatile("dmb oshst" ::: "memory");
	*(volatile uint32_t *)(base + off) = v;
	__asm__ volatile("dsb osh" ::: "memory");
}

static void
ft_sleep_until(uint64_t deadline)
{
	(void)assert_wait_deadline_with_leeway(&ft_chan, THREAD_UNINT,
	    TIMEOUT_URGENCY_SYS_CRITICAL, deadline, 0);
	thread_block(THREAD_CONTINUE_NULL);
}

static void
ft_sleep(unsigned ms)
{
	uint64_t deadline;
	clock_interval_to_deadline(ms, NSEC_PER_MSEC, &deadline);
	(void)assert_wait_deadline(&ft_chan, THREAD_UNINT, deadline);
	thread_block(THREAD_CONTINUE_NULL);
}

/* TLMM CTL: pull [1:0] (1 down, 3 up), func [5:2], drive [8:6] = mA/2-1, OE [9]. */
static void
ft_gpio(unsigned n, unsigned func, unsigned pull, unsigned ma, int out)
{
	if (out >= 0) {
		ft_wr(ft.tlmm, FT_GPIO(n) + 4, out ? 2u : 0u);
	}
	ft_wr(ft.tlmm, FT_GPIO(n), (ft_rd(ft.tlmm, FT_GPIO(n)) & ~0x3ffu) | pull |
	    (func << 2) | ((ma / 2 - 1) << 6) | (out >= 0 ? 0x200u : 0u));
}

static bool
ft_on(uint32_t cbcr)
{
	return !(ft_rd(ft.gcc, cbcr) & 0x80000000u);
}

static int
ft_clock(void)
{
	/* Vote wrapper1 core/core2x/m_ahb/s_ahb and SE4 only. */
	ft_wr(ft.gcc, 0x52008, ft_rd(ft.gcc, 0x52008) | (0xfu << 18) | (1u << 26));
	for (unsigned i = 0; i < 10000 && !ft_on(0x2327c); i++) {
		delay(1);
	}
	if (!ft_on(0x2327c)) {
		/* SE registers hang the bus without the wrapper AHB clock. */
		printf("FUXI TOUCH: wrapper1 AHB off (vote %#x)\n", ft_rd(ft.gcc, 0x52008));
		return EIO;
	}
	/* S4 is a DFS RCG under hardware control: the SE's level request brings
	 * it up. Level from XO, undivided (19.2 MHz); SE divider 2: 9.6 MHz. */
	unsigned level = 0;
	if (ft_rd(ft.gcc, 0x18504) & 1u) {
		while (level < 8 && (ft_rd(ft.gcc, 0x1850c + 4 * level) & 0x71fu) > 1u) {
			level++;
		}
	} else if ((ft_rd(ft.gcc, 0x184f4) & 0x71fu) > 1u) {
		level = 8;
	}
	if (level == 8) {
		printf("FUXI TOUCH: no XO clock level\n");
		return EIO;
	}
	ft.level = level;
	ft_wr(ft.se, 0x07c, level);
	ft_wr(ft.se, 0x048, (2u << 4) | 1u);
	for (unsigned i = 0; i < 10000 && !ft_on(0x184e4); i++) {
		delay(1);
	}
	if (!ft_on(0x184e4)) {
		printf("FUXI TOUCH: SE4 clock off (cbcr %#x)\n", ft_rd(ft.gcc, 0x184e4));
		return EIO;
	}
	return 0;
}

/* DMA page: event ring (4 KiB, 256 events), TX and RX rings (1 KiB, 64 TREs
 * each), then the TX and RX data buffers. Cacheable: clean before a doorbell,
 * invalidate before reading what the hardware wrote. */
#define FT_EV_OFF    0x0000u
#define FT_RING_OFF(k) (0x1000u + 0x400u * (k))
#define FT_TXB_OFF   0x1800u
#define FT_RXB_OFF   0x1c00u
#define FT_RING      64u
#define FT_EVRING    256u
#define GPI_CH(k)    (0x0000u + 0x80u * (k))
#define GPI_EV       0x1000u
#define GPI_CHDB(k)  (0x2000u + 8u * (k))
#define GPI_EVDB     0x2100u
#define GPI_GPII     0x3000u

static void
ft_dma_sync(uint32_t off, uint32_t len)
{
	flush_dcache64((addr64_t)(ft.dma_pa + off), len, TRUE);
}

static uint32_t
ft_state(uint32_t ctx)
{
	return (ft_rd(ft.gpi, ctx) >> 20) & 0xf;
}

static bool
ft_gpi_cmd(uint32_t reg, uint32_t v, uint32_t ctx, uint32_t want)
{
	ft_wr(ft.gpi, GPI_GPII + reg, v);
	for (unsigned i = 0; i < 50000; i++) {
		uint32_t st = ft_state(ctx);
		if (st == want) {
			ft_wr(ft.gpi, GPI_GPII + 0xa0, ft_rd(ft.gpi, GPI_GPII + 0x90));
			ft_wr(ft.gpi, GPI_GPII + 0xa4, ft_rd(ft.gpi, GPI_GPII + 0x94));
			return true;
		}
		if (st == 0xf) {
			break;
		}
		delay(1);
	}
	printf("FUXI TOUCH: GPI cmd %#x -> state %#x (want %u), error log %#x\n",
	    v, ft_state(ctx), want, ft_rd(ft.gpi, GPI_GPII + 0x200));
	return false;
}

/* Bus-masters without an SMR fault (sCR0.USFCFG) and the hypervisor turns an
 * S2CR bypass into fault: route GPI and QUP1 to CB4 like DWC3. Idempotent. */
static int
ft_smmu(void)
{
	static const uint16_t sid[] = {0xb6, 0xa3};
	uint32_t nsmr = ft_rd(ft.smmu, 0x20) & 0xff;
	for (unsigned j = 0; j < 2; j++) {
		unsigned i, freei = ~0u;
		for (i = 0; i < nsmr; i++) {
			uint32_t smr = ft_rd(ft.smmu, 0x800 + 4 * i);
			if (!(smr & 0x80000000u)) {
				if (freei == ~0u) {
					freei = i;
				}
				continue;
			}
			if (((sid[j] ^ smr) & ~(smr >> 16) & 0x7fff) == 0) {
				break;
			}
		}
		if (i < nsmr) {
			continue;
		}
		if (freei == ~0u) {
			return ENOSPC;
		}
		ft_wr(ft.smmu, 0xc00 + 4 * freei, 4);
		ft_wr(ft.smmu, 0x800 + 4 * freei, 0x80000000u | sid[j]);
		printf("FUXI TOUCH: SMR %u = sid %#x -> S2CR %#x\n", freei, sid[j],
		    ft_rd(ft.smmu, 0xc00 + 4 * freei));
	}
	return 0;
}

static int
ft_gpi_init(void)
{
	uint32_t lo = (uint32_t)ft.dma_pa, hi = (uint32_t)(ft.dma_pa >> 32);
	/* The loader leaves both channels started on its own rings: stop, reset, free. */
	for (unsigned k = 0; k < 2; k++) {
		if (ft_state(GPI_CH(k)) == 2) {
			(void)ft_gpi_cmd(0x08, (2u << 24) | k, GPI_CH(k), 3);
		}
		if (ft_state(GPI_CH(k))) {
			(void)ft_gpi_cmd(0x08, (9u << 24) | k, GPI_CH(k), 1);
			ft_wr(ft.gpi, GPI_GPII + 0x08, (10u << 24) | k);
			delay(1000);
		}
	}
	if (ft_state(GPI_EV)) {
		(void)ft_gpi_cmd(0x10, 9u << 24, GPI_EV, 1);
		ft_wr(ft.gpi, GPI_GPII + 0x10, 10u << 24);
		delay(1000);
	}
	bzero((void *)ft.dma, PAGE_SIZE);
	ft_dma_sync(0, PAGE_SIZE);
	ft_wr(ft.gpi, GPI_GPII + 0x88, 0x4f);
	ft_wr(ft.gpi, GPI_GPII + 0xb8, 1);
	ft_wr(ft.gpi, GPI_GPII + 0x98, 3);
	ft_wr(ft.gpi, GPI_GPII + 0x9c, 1);
	ft_wr(ft.gpi, GPI_GPII + 0x108, 1);
	ft_wr(ft.gpi, GPI_GPII + 0x120, 0xf);
	ft_wr(ft.gpi, GPI_GPII + 0x188, 0);
	ft_wr(ft.gpi, GPI_GPII + 0x18c, 0);
	ft_wr(ft.gpi, GPI_GPII + 0x400, 0);
	ft_wr(ft.gpi, GPI_GPII + 0x404, 0);
	ft_wr(ft.gpi, GPI_GPII + 0x180, 1);
	ft_wr(ft.gpi, GPI_GPII + 0x200, 0);
	if (!ft_gpi_cmd(0x10, 0, GPI_EV, 1)) {
		return EIO;
	}
	ft_wr(ft.gpi, GPI_EV + 0x00, 0x10010002u);
	ft_wr(ft.gpi, GPI_EV + 0x04, FT_EVRING * 16);
	ft_wr(ft.gpi, GPI_EV + 0x08, lo + FT_EV_OFF);
	ft_wr(ft.gpi, GPI_EV + 0x0c, hi);
	ft_wr(ft.gpi, GPI_EVDB + 4, hi);
	for (uint32_t o = 0x20; o <= 0x34; o += 4) {
		ft_wr(ft.gpi, GPI_EV + o, 0);
	}
	ft.evrp = 0;
	ft_wr(ft.gpi, GPI_EVDB, lo + FT_EV_OFF + FT_EVRING * 16 - 16);
	for (unsigned k = 0; k < 2; k++) {
		if (!ft_gpi_cmd(0x08, k, GPI_CH(k), 1)) {
			return EIO;
		}
		ft_wr(ft.gpi, GPI_CH(k) + 0x00, k ? 0x10000002u : 0x1000000au);
		ft_wr(ft.gpi, GPI_CH(k) + 0x04, FT_RING * 16);
		ft_wr(ft.gpi, GPI_CH(k) + 0x08, lo + FT_RING_OFF(k));
		ft_wr(ft.gpi, GPI_CH(k) + 0x0c, hi);
		ft_wr(ft.gpi, GPI_CHDB(k) + 4, hi);
		/* SCRATCH_0: pair, SPI protocol 1, SE 4. */
		ft_wr(ft.gpi, GPI_CH(k) + 0x60, k ? 0x00000014u : 0x00010014u);
		ft_wr(ft.gpi, GPI_CH(k) + 0x64, 0);
		ft_wr(ft.gpi, GPI_CH(k) + 0x68, 0);
		ft_wr(ft.gpi, GPI_CH(k) + 0x6c, 0);
		ft_wr(ft.gpi, GPI_CH(k) + 0x5c, 1);
		ft.wp[k] = 0;
	}
	for (unsigned k = 0; k < 2; k++) {
		if (!ft_gpi_cmd(0x08, (1u << 24) | k, GPI_CH(k), 2)) {
			return EIO;
		}
	}
	return 0;
}

static int
ft_se_init(void)
{
	uint32_t fw = ft_rd(ft.se, 0x068);
	if (((fw >> 8) & 0xff) != 1 || (ft_rd(ft.se, 0x040) & 1)) {
		printf("FUXI TOUCH: SE4 fw %#x status %#x: not idle SPI\n", fw, ft_rd(ft.se, 0x040));
		return ENODEV;
	}
	/* geni_se_select_mode(GENI_GPI_DMA): the CONFIG0 TRE sets mode, word
	 * size, packing and clock. */
	ft_wr(ft.se, 0xe18, 0);
	ft_wr(ft.se, 0x618, 0xffffffffu);
	ft_wr(ft.se, 0x648, 0xffffffffu);
	ft_wr(ft.se, 0xc44, 0xffffffffu);
	ft_wr(ft.se, 0xd44, 0xffffffffu);
	ft_wr(ft.se, 0x028, ft_rd(ft.se, 0x028) | 0x7f);
	ft_wr(ft.se, 0xe30, ft_rd(ft.se, 0xe30) | 0xf);
	ft_wr(ft.se, 0x024, 0x7f);
	ft_wr(ft.se, 0x020, 1);
	ft_wr(ft.se, 0xe1c, 0);
	ft_wr(ft.se, 0x614, ft_rd(ft.se, 0x614) & ~((1u << 0) | (1u << 26) | (1u << 27) | (1u << 30)));
	ft_wr(ft.se, 0x644, ft_rd(ft.se, 0x644) & ~1u);
	ft_wr(ft.se, 0x258, 1);
	ft_wr(ft.se, 0xe18, ft_rd(ft.se, 0xe18) | 0xf);
	return 0;
}

static void
ft_tre(unsigned k, uint32_t d0, uint32_t d1, uint32_t d2, uint32_t d3)
{
	uint32_t off = FT_RING_OFF(k) + 16 * ft.wp[k];
	volatile uint32_t *t = (volatile uint32_t *)(ft.dma + off);
	t[0] = d0;
	t[1] = d1;
	t[2] = d2;
	t[3] = d3;
	ft_dma_sync(off, 16);
	ft.wp[k] = (ft.wp[k] + 1) % FT_RING;
}

static void
ft_ring_db(unsigned k)
{
	__asm__ volatile("dsb sy" ::: "memory");
	ft_wr(ft.gpi, GPI_CHDB(k), (uint32_t)ft.dma_pa + FT_RING_OFF(k) + 16 * ft.wp[k]);
}

/* Drain events until an end-of-transfer (code 2) from chid arrives. */
static int
ft_gpi_wait(unsigned chid, uint32_t *len)
{
	for (unsigned us = 0; us < 5000; us++) {
		uint32_t hw = ft_rd(ft.gpi, GPI_EV + 0x10);
		bool found = false, bad = false;
		while (hw != (uint32_t)ft.dma_pa + FT_EV_OFF + 16 * ft.evrp) {
			uint32_t off = FT_EV_OFF + 16 * ft.evrp;
			ft_dma_sync(off, 16);
			volatile uint32_t *e = (volatile uint32_t *)(ft.dma + off);
			uint32_t d2 = e[2], d3 = e[3];
			ft.evrp = (ft.evrp + 1) % FT_EVRING;
			unsigned code = d2 >> 24, type = (d3 >> 16) & 0xff, ch = d3 >> 24;
			if (code == 16 || type == 0xff || ch >= 2) {
				bad = true;
				printf("FUXI TOUCH: GPI event %#x %#x\n", d2, d3);
			} else if (type == 0x22 && ch == chid && code == 2) {
				found = true;
				*len = d2 & 0xffffff;
			}
		}
		ft_wr(ft.gpi, GPI_EVDB, (uint32_t)ft.dma_pa + FT_EV_OFF +
		    16 * ((ft.evrp + FT_EVRING - 1) % FT_EVRING));
		ft_wr(ft.gpi, GPI_GPII + 0xc0, 1);
		if (bad) {
			return EIO;
		}
		if (found) {
			return 0;
		}
		delay(1);
	}
	return ETIMEDOUT;
}

#define FT_C0()  ft_tre(0, 0x03000004u, 0, (ft.level << 16) | 2u, 0x00220001u)

static int
ft_tx(const uint8_t *tx, unsigned nt, bool frag)
{
	uint32_t len = 0;
	int err;
	memcpy((void *)(ft.dma + FT_TXB_OFF), tx, nt);
	ft_dma_sync(FT_TXB_OFF, nt);
	FT_C0();
	ft_tre(0, (frag ? (1u << 26) : 0) | 1u, 0, 0, 0x00200001u);
	ft_tre(0, (uint32_t)ft.dma_pa + FT_TXB_OFF, (uint32_t)(ft.dma_pa >> 32), nt, 0x00100200u);
	ft_ring_db(0);
	if ((err = ft_gpi_wait(0, &len))) {
		return err;
	}
	return len == nt ? 0 : EIO;
}

/* FTS read: opcode + address (CS held), then one dummy byte and n data bytes. */
static int
ft_read(const uint8_t *cmd, unsigned nc, uint8_t *out, unsigned n)
{
	uint32_t len = 0;
	int err;
	if (nc > 8 || n > 8 * 32) {
		return EINVAL;
	}
	if ((err = ft_tx(cmd, nc, true))) {
		return err;
	}
	ft_dma_sync(FT_RXB_OFF, n + 1);
	ft_tre(1, (uint32_t)ft.dma_pa + FT_RXB_OFF, (uint32_t)(ft.dma_pa >> 32), n + 1, 0x00100200u);
	ft_tre(0, 2u, 0, n + 1, 0x00200900u);
	ft_ring_db(1);
	ft_ring_db(0);
	if ((err = ft_gpi_wait(1, &len))) {
		return err;
	}
	ft_dma_sync(FT_RXB_OFF, n + 1);
	memcpy(out, (const void *)(ft.dma + FT_RXB_OFF + 1), n);
	return 0;
}

static uint64_t
ft_us(uint64_t abs)
{
	uint64_t ns;
	absolutetime_to_nanoseconds(abs, &ns);
	return ns / 1000;
}

/* One line per contact: how often iOS got a report and what a FIFO read costs. */
static void
ft_contact_end(void)
{
	uint64_t dur = ft_us(ft.c_last - ft.c_start);
	ft.contacts++;
	printf("FUXI TOUCH: contact %u: %u reports in %llu ms (%llu us apart), %u polls, "
	    "FIFO read avg %llu us max %llu us, multi %u max %u lifts %u\n",
	    ft.contacts, ft.c_reports, dur / 1000,
	    ft.c_reports > 1 ? dur / (ft.c_reports - 1) : 0ULL, ft.c_polls,
	    ft.c_polls ? ft_us(ft.c_poll_sum) / ft.c_polls : 0ULL, ft_us(ft.c_poll_max),
	    ft.c_multi, ft.c_max, ft.c_lifts);
}

extern void fuxi_perf_input(void);
static void
ft_report(uint32_t mask)
{
	fuxi_perf_input();
	uint64_t now = mach_absolute_time();
	if (mask && !ft.active) {
		ft.c_start = now;
		ft.c_reports = ft.c_polls = 0;
		ft.c_poll_sum = ft.c_poll_max = 0;
		ft.c_multi = ft.c_max = ft.c_lifts = 0;
	}
	unsigned down_count = 0;
	for (unsigned id = 0; id < FT_MAX; id++) {
		uint32_t bit = 1u << id;
		if (mask & bit) {
			down_count++;
			if (!ft.batch) {
				sweet_hid_touch((int)id, 1, ft.x[id], ft.y[id]);
			}
		} else if (ft.active & bit) {
			ft.c_lifts++;
			if (!ft.batch) {
				sweet_hid_touch((int)id, 0, ft.x[id], ft.y[id]);
			}
		}
	}
	if (ft.batch && (mask || ft.active)) {
		ft.retry = !sweet_hid_touch_report(mask, ft.x, ft.y);
	}
	ft.c_multi += down_count >= 2;
	ft.c_max = down_count > ft.c_max ? down_count : ft.c_max;
	if (mask || ft.active) {
		ft.c_reports++;
		ft.c_last = now;
	}
	bool ended = ft.active && !mask;
	ft.active = mask;
	if (ended) {
		ft_contact_end();
	}
}

/* Retry the latest set, including final all-up, without counting a new sample. */
static void
ft_retry_report(void)
{
	if (ft.retry) {
		ft.retry = !sweet_hid_touch_report(ft.active, ft.x, ft.y);
	}
}

/*
 * Drain the FIFO in two reads (as fts_event_handler): one event, whose
 * byte 7 holds the number of events still queued, then all of those at
 * once. With deliver false (display off) the events are consumed and
 * dropped.
 */
static int
ft_poll(bool deliver)
{
	static const uint8_t fifo[] = {0x87};
	uint8_t e[8 * 32];
	uint32_t mask = ft.active;
	bool changed = false;
	int err = ft_read(fifo, 1, e, 8);
	if (err) {
		return err;
	}
	if (e[0] == 0) {
		return 0;
	}
	ft.fifo_hits++;
	unsigned n = 1 + (e[7] & 0x1f);
	if (n > 32) {
		n = 32;
	}
	if (n > 1 && (err = ft_read(fifo, 1, e + 8, 8 * (n - 1)))) {
		return err;
	}
	for (unsigned i = 0; i < n; i++) {
		const uint8_t *ev = e + 8 * i;
		if (ev[0] == 0) {
			break;
		}
		if (ev[0] == FT_EV_READY) {
			ft.ic_reset = true;
			continue;
		}
		unsigned id = ev[1] >> 4, type = ev[1] & 0xf;
		if ((ev[0] != FT_EV_ENTER && ev[0] != FT_EV_MOTION && ev[0] != FT_EV_LEAVE) ||
		    id >= FT_MAX || (type == 0 && ev[0] != FT_EV_LEAVE)) {
			continue;
		}
		bool touching = ev[0] != FT_EV_LEAVE && type != FT_TYPE_HOVER, fresh;
		if (fuxi_wake_touch_event(&ft.wake, !deliver, id, touching, &fresh)) {
			if (fresh && fuxi_keys_touch_wake()) {
				fuxi_wake_touch_woke(&ft.wake);
			}
			if (deliver) {
				continue;      /* a contact that woke the display: not for iOS */
			}
		}
		/* Super-resolution coordinates, 10x the 1080x2400 panel. */
		int x = ((ev[3] << 8) | ev[2]) / 10, y = ((ev[5] << 8) | ev[4]) / 10;
		ft.x[id] = x > 1079 ? 1079 : x;
		ft.y[id] = y > 2399 ? 2399 : y;
		if (ev[0] == FT_EV_LEAVE || type == FT_TYPE_HOVER) {
			mask &= ~(1u << id);
		} else {
			mask |= 1u << id;
		}
		changed = true;
	}
	if (changed && deliver) {
		ft_report(mask);
	}
	return 0;
}

/*
 * Touch interrupt. Adapted from UniXNU-sheng package/TouchProbe.cpp armTouchIrq/touchIrq (5872a4f, GPIO 25 through
 * the TLMM summary SPI 208 on SM8550; BSD-2-Clause-Patent, bsd/dev/LICENSE.UniXNU-sheng) and, for the distributor,
 * ArmCpu armcpu_spi_attach as fuxi_gpu_irq.inc fg_spi_on has it (BSD-2-Clause-Patent, bsd/dev/LICENSE.ArmCpu).
 *
 * Facts. Stock DT (dtbo_a fuxi overlay fragment@34 fts@0; merged: /soc/qcom,qupv3_1_geni_se@ac0000/spi@a90000/fts@0):
 * interrupts = <25 0x2008> on the TLMM, fts,irq-gpio = <&tlmm 25 0x2008>, fts,irq-flags = <0x2008>: IRQ_TYPE_LEVEL_LOW
 * (8) with IRQF_ONESHOT (0x2000). The TLMM (/soc/pinctrl@f000000, pins from 0xf100000) has one summary interrupt,
 * <0 208 4> = SPI 208 level high = INTID 240; GICD at 0x17100000 (/soc/interrupt-controller@17100000 reg[0]). Pin
 * registers at TLMM + 0x1000 * pin (Linux pinctrl-sm8550.c PINGROUP, as UniXNU): +0x8 INTR_CFG, bit 0 enable, bit 1
 * polarity, bits 3:2 detection (0 level), bit 4 raw status enable, bits 7:5 target (3 = apps); +0xc INTR_STATUS bit 0,
 * acknowledged by writing 0. Only GPIO25's two interrupt registers are written; no other pin is touched.
 *
 * Level handling as pinctrl-msm.c (handle_level_irq): the handler masks the pin (enable and raw status off, so the
 * held level cannot latch again), acknowledges it and wakes the thread, which drains the FIFO and unmasks; a line
 * still low then latches at once. A FIFO found empty with the line still low, or a read error, unmasks only after
 * one poll period. The thread wakes every FT_WATCH_MS to see the display state and that the path still works, and
 * reads the FIFO every FT_SWEEP_MS regardless.
 *
 * Routing. sleh_irq (patch 0004) calls one handler per CPU (cpu_data interrupt_handler). The GPU fence interrupt
 * (fuxi_gpu_irq.inc) installs its handler on every CPU and routes INTID 332 to CPU 0, so this routes INTID 240 to
 * FT_IRQ_CPU and installs its handler there only (it masks any other INTID that reaches it, as the GPU's does; never
 * route another SPI to that CPU). If the GPU attaches after this, its handler takes FT_IRQ_CPU's slot and masks INTID
 * 240 at the first touch: the watchdog sees the line low with INTID 240 disabled and installs this handler again
 * (FT_REPAIRS times at most).
 *
 * Fallback to polling, logged once, for the rest of the boot: setup failed; FT_SPUR_MAX interrupts in a row with
 * GPIO25's status clear or FT_BURST_MAX within 100 ms (the handler masks the SPI); the line low and the pin armed
 * FT_MISS_MS with no interrupt, twice in a row; FIFO data at FT_SWEEP_STRIKES sweeps in a row with no interrupt
 * between them; more than FT_REPAIRS re-installs.
 */
#define FT_INTID         240u          /* TLMM summary interrupt, GIC SPI 208 */
#define FT_GICD_PA       0x17100000u
#define FT_IRQ_CPU       1             /* logical CPU 1 (setup logs its MPIDR): not CPU 0, the GPU fence SPI's */
#define FT_IC_EN         (1u << 0)
#define FT_IC_POL        (1u << 1)
#define FT_IC_DET        (3u << 2)
#define FT_IC_RAW        (1u << 4)
#define FT_IC_TGT        (7u << 5)
#define FT_IC_APPS       (3u << 5)
#define FT_WATCH_MS      100u
#define FT_MISS_MS       5u
#define FT_SWEEP_MS      1000u
#define FT_SWEEP_STRIKES 3u
#define FT_STATS_S       60u
#define FT_SPUR_MAX      100u
#define FT_BURST_MAX     500u
#define FT_REPAIRS       3u

static struct {
	vm_offset_t gicd, pin;           /* GICD; GPIO25's TLMM registers */
	unsigned state;                  /* 0 off, 1 armed, 2 masked by the handler, 3 polling */
	unsigned why;                    /* state 2: 1 status clear in a row, 2 burst */
	uint32_t seq;                    /* interrupts taken, handler to thread */
	uint32_t spur_run, burst;        /* handler only (one CPU) */
	uint64_t burst_t0, burst_abs;
	uint64_t irqs, spurious, foreign; /* handler counters */
	unsigned rd_irq, rd_timer, rd_timer_data, missed, repairs;  /* thread counters */
	uint64_t mpidr;
	int event;
} fi;

static volatile uint32_t *
ft_gicd(unsigned off)
{
	return (volatile uint32_t *)(fi.gicd + off);
}

static bool
ft_spi_bit(unsigned base, unsigned id)
{
	return (*ft_gicd(base + (id / 32) * 4) >> (id % 32)) & 1u;
}

static void
ft_spi_mask(unsigned id)
{
	if (!fi.gicd || id < 32 || id >= 1020) {
		return;
	}
	*ft_gicd(0x180 + (id / 32) * 4) = 1u << (id % 32);
	__asm__ volatile("dsb sy" ::: "memory");
}

static void
ft_irq_wake(void)
{
	(void)thread_wakeup((event_t)&fi.event);
}

static void
ft_irq_dead(unsigned why)
{
	if (!fi.why) {
		fi.why = why;
	}
	__atomic_store_n(&fi.state, 2u, __ATOMIC_RELEASE);   /* before the mask, so a thread that sees it masked falls back */
	ft_spi_mask(FT_INTID);
	ft_irq_wake();
}

/* Interrupt context on FT_IRQ_CPU, interrupts masked, before sleh's EOI. */
static void
ft_irq_handler(void *target, void *refcon, void *nub, int source)
{
	(void)target;
	(void)refcon;
	(void)nub;
	unsigned id = (unsigned)source;
	if (id != FT_INTID || __atomic_load_n(&fi.state, __ATOMIC_ACQUIRE) != 1) {
		ft_spi_mask(id);        /* nobody here to clear it */
		if (id != FT_INTID) {
			__atomic_fetch_add(&fi.foreign, 1, __ATOMIC_RELAXED);
		}
		return;
	}
	uint64_t now = mach_absolute_time();
	if (now - fi.burst_t0 > fi.burst_abs) {
		fi.burst_t0 = now;
		fi.burst = 0;
	}
	if (ft_rd(fi.pin, 0xc) & 1u) {
		ft_wr(fi.pin, 0x8, ft_rd(fi.pin, 0x8) & ~(FT_IC_EN | FT_IC_RAW));
		ft_wr(fi.pin, 0xc, 0);
		fi.spur_run = 0;
		__atomic_fetch_add(&fi.irqs, 1, __ATOMIC_RELAXED);
		__atomic_fetch_add(&fi.seq, 1, __ATOMIC_RELEASE);
		ft_irq_wake();
	} else {
		__atomic_fetch_add(&fi.spurious, 1, __ATOMIC_RELAXED);
		if (++fi.spur_run >= FT_SPUR_MAX) {
			ft_irq_dead(1);
		}
	}
	if (++fi.burst >= FT_BURST_MAX) {
		ft_irq_dead(2);
	}
}

/* The handler into FT_IRQ_CPU's slot (bound there in turn, as fuxi_gpu_irq.inc does); that CPU's affinity. */
static const char *
ft_irq_install(uint64_t *mpidr)
{
	if (ml_get_cpu_count() <= FT_IRQ_CPU) {
		return "no second CPU";
	}
	processor_t p = cpu_to_processor(FT_IRQ_CPU);
	if (!p) {
		return "no processor for the CPU";
	}
	processor_t previous = thread_bind(p);
	(void)thread_block(THREAD_CONTINUE_NULL);
	ml_install_interrupt_handler(NULL, (int)FT_INTID, NULL, ft_irq_handler, NULL);
	*mpidr = __builtin_arm_rsr64("MPIDR_EL1") & 0xff00ffffffULL;
	(void)thread_bind(previous);
	return NULL;
}

/* fg_spi_on's sequence: level, group 1 NS, priority 0xa0, routed to one affinity; readback grp|en<<1|edge<<2|prio<<8. */
static const char *
ft_spi_on(unsigned id, uint64_t affinity, uint32_t *readback, uint64_t *router)
{
	unsigned word = (id / 32) * 4, cfg = 0xc00 + (id / 16) * 4, shift = (id % 16) * 2 + 1, n = 0;
	uint32_t bit = 1u << (id % 32);
	*ft_gicd(0x180 + word) = bit;
	while ((*ft_gicd(0) & 0x80000000u) && n < 100000) {
		n++;
		delay(1);
	}
	if (n == 100000) {
		return "GICD_CTLR.RWP stuck";
	}
	*ft_gicd(0x80 + word) |= bit;
	*(volatile uint8_t *)(fi.gicd + 0x400 + id) = 0xa0;
	*ft_gicd(cfg) &= ~(1u << shift);
	*(volatile uint64_t *)(fi.gicd + 0x6000 + 8 * id) = affinity;
	__asm__ volatile("dsb sy" ::: "memory");
	*ft_gicd(0x100 + word) = bit;
	__asm__ volatile("dsb sy" ::: "memory");
	*readback = (ft_spi_bit(0x80, id) ? 1u : 0) | (ft_spi_bit(0x100, id) ? 2u : 0) |
	    (((*ft_gicd(cfg) >> shift) & 1u) << 2) | ((uint32_t)*(volatile uint8_t *)(fi.gicd + 0x400 + id) << 8);
	*router = *(volatile uint64_t *)(fi.gicd + 0x6000 + 8 * id);
	return (*readback & 7u) != 3u || *router != affinity ? "GICD readback" : NULL;
}

/* One line either way; returns whether interrupts are armed. */
static bool
ft_irq_setup(void)
{
	uint32_t was = 0, cfg = 0, got = 0, readback = 0;
	uint64_t router = 0;
	unsigned pending = 0;
	const char *fail = NULL;
	fi.pin = ft.tlmm + FT_GPIO(FT_IRQ);
	nanoseconds_to_absolutetime(100 * NSEC_PER_MSEC, &fi.burst_abs);
	fi.gicd = ml_io_map(FT_GICD_PA, 0x10000);
	if (!fi.gicd) {
		fail = "GICD map";
	} else if (!(*ft_gicd(0) & 0x10u)) {
		fail = "GICD_CTLR.ARE_NS clear";
	} else if (((*ft_gicd(4) & 31u) + 1) * 32 <= FT_INTID) {
		fail = "GICD_TYPER lines";
	} else {
		/* msm_gpio_irq_set_type(IRQ_TYPE_LEVEL_LOW) with the pin masked: apps target, level, active low, raw
		 * status on; then acknowledge whatever latched before. */
		was = ft_rd(fi.pin, 0x8);
		cfg = was & ~(FT_IC_EN | FT_IC_POL | FT_IC_DET | FT_IC_TGT | FT_IC_RAW);
		ft_wr(fi.pin, 0x8, cfg);
		cfg |= FT_IC_APPS | FT_IC_RAW;
		ft_wr(fi.pin, 0x8, cfg);
		ft_wr(fi.pin, 0xc, 0);
		got = ft_rd(fi.pin, 0x8);                /* read back while masked: the log shows what took */
		pending = ft_spi_bit(0x200, FT_INTID);   /* summary already high from another pin? */
		fail = ft_irq_install(&fi.mpidr);
		if (!fail) {
			/* Armed before the SPI is enabled, so whatever arrives is counted (and a stuck summary masked). */
			__atomic_store_n(&fi.state, 1u, __ATOMIC_RELEASE);
			fail = ft_spi_on(FT_INTID, fi.mpidr, &readback, &router);
		}
		if (fail) {
			__atomic_store_n(&fi.state, 0u, __ATOMIC_RELEASE);
			ft_spi_mask(FT_INTID);
			ft_wr(fi.pin, 0x8, cfg & ~FT_IC_RAW);
		} else {
			ft_wr(fi.pin, 0x8, cfg | FT_IC_EN);   /* unmasked: a line already low latches now */
		}
	}
	printf("FUXI TOUCH IRQ: GPIO%u level low -> TLMM summary SPI 208 (INTID %u) -> cpu %d mpidr %#llx router %#llx, "
	    "gicd %#x (grp|en<<1|edge<<2|prio<<8), intr_cfg %#x->%#x read %#x (+enable), pending %u: %s%s\n", FT_IRQ,
	    FT_INTID, FT_IRQ_CPU, fi.mpidr, router, readback, was, cfg, got, pending,
	    fail ? "failed, polling: " : "interrupt-driven", fail ? fail : "");
	return !fail;
}

/* Thread: unmask GPIO25 after the FIFO was drained (the handler masked it). */
static void
ft_irq_arm(void)
{
	ft_wr(fi.pin, 0x8, ft_rd(fi.pin, 0x8) | FT_IC_EN | FT_IC_RAW);
}

/* Another handler took FT_IRQ_CPU's slot and masked INTID 240: take the slot back and enable it again. */
static const char *
ft_irq_repair(void)
{
	uint64_t mpidr = 0;
	const char *fail = ft_irq_install(&mpidr);
	if (!fail && mpidr != fi.mpidr) {
		fail = "CPU affinity changed";
	}
	if (!fail) {
		*ft_gicd(0x100 + (FT_INTID / 32) * 4) = 1u << (FT_INTID % 32);
		__asm__ volatile("dsb sy" ::: "memory");
	}
	return fail;
}

static void
ft_irq_stats(const char *mode)
{
	static uint64_t last_irqs = ~0ULL;
	static unsigned last_rd = ~0u, last_data = ~0u, last_missed = ~0u;
	uint64_t irqs = __atomic_load_n(&fi.irqs, __ATOMIC_RELAXED);
	if (irqs == last_irqs && fi.rd_irq == last_rd && fi.rd_timer_data == last_data && fi.missed == last_missed) {
		return;
	}
	last_irqs = irqs;
	last_rd = fi.rd_irq;
	last_data = fi.rd_timer_data;
	last_missed = fi.missed;
	printf("FUXI TOUCH IRQ: %llu interrupts (%llu spurious, %llu foreign); reads %u on interrupt, %u on timer "
	    "(%u with data); %u missed, %u repaired; %s\n", irqs, __atomic_load_n(&fi.spurious, __ATOMIC_RELAXED),
	    __atomic_load_n(&fi.foreign, __ATOMIC_RELAXED), fi.rd_irq, fi.rd_timer, fi.rd_timer_data, fi.missed,
	    fi.repairs, mode);
}

/* Polling from now on: mask the SPI and the pin. Logged once. */
static void
ft_irq_fallback(const char *why)
{
	__atomic_store_n(&fi.state, 3u, __ATOMIC_RELEASE);
	ft_spi_mask(FT_INTID);
	ft_wr(fi.pin, 0x8, ft_rd(fi.pin, 0x8) & ~(FT_IC_EN | FT_IC_RAW));
	ft_wr(fi.pin, 0xc, 0);
	printf("FUXI TOUCH IRQ: falling back to polling: %s\n", why);
	ft_irq_stats("polling");
}

/* Sleep until the touch interrupt (or the handler giving up) or the deadline. */
static void
ft_irq_wait(uint32_t seen, uint64_t deadline, uint64_t leeway)
{
	(void)assert_wait_deadline_with_leeway((event_t)&fi.event, THREAD_UNINT,
	    leeway ? TIMEOUT_URGENCY_SYS_NORMAL | TIMEOUT_URGENCY_LEEWAY : TIMEOUT_URGENCY_SYS_CRITICAL, deadline, leeway);
	if (__atomic_load_n(&fi.seq, __ATOMIC_ACQUIRE) != seen || __atomic_load_n(&fi.state, __ATOMIC_ACQUIRE) != 1) {
		ft_irq_wake();   /* it came between the caller's check and the assert: return at once */
	}
	(void)thread_block(THREAD_CONTINUE_NULL);
}

static void
ft_thread(void *arg, wait_result_t result)
{
	static const uint8_t id_cmd[] = {0xfb, 0x20, 0x00, 0x00, 0x00}, fifo[] = {0x87};
	static const uint8_t sense[] = {0xa0, 0x00, 0x01};
	uint8_t ev[8] = {0}, chip[2] = {0};
	unsigned delay_s = 30, errors = 0;
	unsigned poll_us = 4000, idle_us = 8000, offsup = 1, want_irq = 1;
	bool irq_seen = false, was_off = false;
	(void)arg;
	(void)result;
	(void)PE_parse_boot_argn("fuxitouch_irq", &want_irq, sizeof(want_irq));
	(void)PE_parse_boot_argn("fuxitouchdelay", &delay_s, sizeof(delay_s));
	(void)PE_parse_boot_argn("fuxitouch_poll_us", &poll_us, sizeof(poll_us));
	(void)PE_parse_boot_argn("fuxitouch_idle_us", &idle_us, sizeof(idle_us));
	(void)PE_parse_boot_argn("fuxitouch_offsuppress", &offsup, sizeof(offsup));
	(void)PE_parse_boot_argn("fuxitbatch", &ft.batch, sizeof(ft.batch));
	poll_us = poll_us < 1000 ? 1000 : poll_us;
	idle_us = idle_us < poll_us ? poll_us : idle_us;
	printf("FUXI TOUCH: starting in %u s (poll %u us active, %u us idle)\n", delay_s,
	    poll_us, idle_us);
	if (ft.batch) {
		printf("FUXI TOUCH: report collections enabled (fuxitbatch=1)\n");
	}
	ft_sleep(delay_s * 1000u);
	ft.gcc = ml_io_map(FT_GCC_PA, 0x1f0000);
	ft.tlmm = ml_io_map(FT_TLMM_PA, 0x30000);
	ft.se = ml_io_map(FT_SE_PA, 0x4000);
	ft.gpi = ml_io_map(FT_GPI_PA, 0x4000);
	ft.smmu = ml_io_map(FT_SMMU_PA, 0x1000);
	if (!ft.gcc || !ft.tlmm || !ft.se || !ft.gpi || !ft.smmu) {
		goto out;
	}
	if (kmem_alloc_contig(kernel_map, &ft.dma, PAGE_SIZE, PAGE_MASK, 0, 0,
	    KMA_DATA | KMA_ZERO | KMA_PERMANENT, VM_KERN_MEMORY_IOKIT) != KERN_SUCCESS) {
		printf("FUXI TOUCH: no DMA memory\n");
		goto out;
	}
	ppnum_t pn = pmap_find_phys(kernel_pmap, (addr64_t)ft.dma);
	if (!pn) {
		goto out;
	}
	ft.dma_pa = ptoa_64(pn);
	int ierr;
	if ((ierr = ft_clock()) || (ierr = ft_se_init()) || (ierr = ft_smmu()) || (ierr = ft_gpi_init())) {
		printf("FUXI TOUCH: init failed %d\n", ierr);
		goto out;
	}
	printf("FUXI TOUCH: GPI up, DMA page %#llx\n", ft.dma_pa);
	ft_gpio(FT_IRQ, 0, 3, 8, -1);
	ft_gpio(44, 1, 1, 6, -1);
	ft_gpio(45, 1, 1, 6, -1);
	ft_gpio(46, 1, 1, 6, -1);
	ft_gpio(47, 1, 3, 6, -1);
	/* fts_system_reset: reset low 10 ms, high, then poll for controller ready. */
	ft_gpio(FT_RESET, 0, 3, 8, 0);
	ft_sleep(10);
	ft_gpio(FT_RESET, 0, 3, 8, 1);
	int err = EIO;
	for (unsigned t = 0; t < 100; t++) {
		ft_sleep(2);
		if ((err = ft_read(fifo, 1, ev, 8)) || ev[0] == FT_EV_READY) {
			break;
		}
	}
	int cerr = ft_read(id_cmd, 5, chip, 2);
	printf("FUXI TOUCH: reset err=%d event %02x %02x %02x %02x; chip id err=%d %02x%02x\n",
	    err, ev[0], ev[1], ev[2], ev[3], cerr, chip[0], chip[1]);
	if (err || cerr || ev[0] != FT_EV_READY) {
		goto out;
	}
	if ((err = ft_tx(sense, 3, false))) {
		printf("FUXI TOUCH: sense on failed %d\n", err);
		goto out;
	}
	bool irqmode = want_irq && ft_irq_setup();
	if (!want_irq) {
		printf("FUXI TOUCH IRQ: off (fuxitouch_irq=0)\n");
	}
	printf("FUXI TOUCH: sensing, %s GPIO%u\n", irqmode ? "interrupt from" : "polling", FT_IRQ);
	/*
	 * IRQ is level low while the FIFO holds events. Polling: check it every
	 * poll period (absolute deadlines, no timer leeway) and read the FIFO
	 * every 32 ms regardless in case the IC does not drive the line.
	 * Interrupt: read after each interrupt; see "Touch interrupt".
	 */
	uint64_t poll_abs, idle_abs, off_abs, recent_abs, sweep_abs;
	uint64_t watch_abs, miss_abs, isweep_abs, stats_abs, leeway_abs;
	nanoseconds_to_absolutetime(poll_us * 1000ULL, &poll_abs);
	nanoseconds_to_absolutetime(idle_us * 1000ULL, &idle_abs);
	nanoseconds_to_absolutetime(32 * NSEC_PER_MSEC, &off_abs);
	nanoseconds_to_absolutetime(32 * NSEC_PER_MSEC, &sweep_abs);
	nanoseconds_to_absolutetime(150 * NSEC_PER_MSEC, &recent_abs);
	nanoseconds_to_absolutetime(FT_WATCH_MS * NSEC_PER_MSEC, &watch_abs);
	nanoseconds_to_absolutetime(FT_MISS_MS * NSEC_PER_MSEC, &miss_abs);
	nanoseconds_to_absolutetime(FT_SWEEP_MS * NSEC_PER_MSEC, &isweep_abs);
	nanoseconds_to_absolutetime(FT_STATS_S * NSEC_PER_SEC, &stats_abs);
	nanoseconds_to_absolutetime(10 * NSEC_PER_MSEC, &leeway_abs);
	uint64_t next = mach_absolute_time(), last_irq = 0, last_read = 0;
	uint64_t suspect = 0, rearm_at = 0, last_stats = next;
	uint32_t seen = 0, sweep_seq = 0;   /* 0: an interrupt taken right after setup armed the pin still counts */
	unsigned missed_run = 0, sweep_run = 0;
	bool armed = irqmode;
	for (;;) {
		uint64_t now = mach_absolute_time();
		bool off = offsup && sweet_fb_display_off;
		if (off != was_off) {
			was_off = off;
			fuxi_wake_touch_display(&ft.wake, off);
			if (off && ft.active) {
				ft_report(0);   /* release fingers held when the display went off */
			}
		}
		bool woke = false;
		if (irqmode) {
			uint32_t s = __atomic_load_n(&fi.seq, __ATOMIC_ACQUIRE);
			if (s != seen) {
				seen = s;
				woke = true;
				armed = false;   /* the handler masked GPIO25 */
				missed_run = 0;
			}
			if (__atomic_load_n(&fi.state, __ATOMIC_ACQUIRE) != 1) {
				ft_irq_fallback(fi.why == 1 ? "interrupts with GPIO25's status clear" : "interrupt storm");
				irqmode = false;
			}
		}
		bool irq = !(ft_rd(ft.tlmm, FT_GPIO(FT_IRQ) + 4) & 1u);
		if (irq) {
			last_irq = now;
			if (!irq_seen) {
				irq_seen = true;
				printf("FUXI TOUCH: IRQ line asserted\n");
			}
		}
		bool late = false;
		if (irqmode && armed && irq) {
			/* Armed and the line low: the interrupt is due within microseconds. */
			if (!ft_spi_bit(0x100, FT_INTID)) {
				/* Not by this handler (it sets state 2 first; the next pass falls back then). */
				if (__atomic_load_n(&fi.state, __ATOMIC_ACQUIRE) == 1) {
					const char *rfail = ++fi.repairs > FT_REPAIRS ? "too many" : ft_irq_repair();
					printf("FUXI TOUCH IRQ: INTID %u found disabled with the line low (another handler on cpu %d?), "
					    "re-install %u: %s\n", FT_INTID, FT_IRQ_CPU, fi.repairs, rfail ? rfail : "done");
					if (rfail) {
						ft_irq_fallback("INTID 240 disabled by another handler");
						irqmode = false;
					}
				}
				suspect = 0;
			} else if (!suspect) {
				suspect = now;
			} else if (now - suspect >= miss_abs && __atomic_load_n(&fi.seq, __ATOMIC_ACQUIRE) == seen) {
				late = true;
				suspect = 0;
				fi.missed++;
				if (fi.missed <= 3) {
					printf("FUXI TOUCH IRQ: line low %u ms, no interrupt: intr_cfg %#x status %#x, INTID %u "
					    "enabled %u pending %u active %u\n", FT_MISS_MS, ft_rd(fi.pin, 0x8), ft_rd(fi.pin, 0xc),
					    FT_INTID, (unsigned)ft_spi_bit(0x100, FT_INTID), (unsigned)ft_spi_bit(0x200, FT_INTID),
					    (unsigned)ft_spi_bit(0x300, FT_INTID));
				}
			}
		} else {
			suspect = 0;
		}
		bool sweep = now - last_read >= (irqmode ? isweep_abs : sweep_abs);
		bool doread = irqmode ? woke || late || sweep : irq || ft.active || sweep;
		if (doread) {
			unsigned hits = ft.fifo_hits;
			last_read = now;
			int perr = ft_poll(!off);
			uint64_t took = mach_absolute_time() - now;
			bool data = ft.fifo_hits != hits;
			if (woke) {
				fi.rd_irq++;
			} else {
				fi.rd_timer++;
				fi.rd_timer_data += data;
			}
			if (irqmode && late && ++missed_run >= 2) {
				ft_irq_fallback("the line low with no interrupt, twice");
				irqmode = false;
			}
			if (irqmode && sweep && !woke) {
				/* Data at a sweep with no interrupt since the last one: the line may not tell. */
				sweep_run = data && seen == sweep_seq ? sweep_run + 1 : 0;
				sweep_seq = seen;
				if (sweep_run >= FT_SWEEP_STRIKES) {
					ft_irq_fallback("FIFO data at sweeps with no interrupt");
					irqmode = false;
				}
			}
			if (irqmode && !armed) {
				bool low = !(ft_rd(ft.tlmm, FT_GPIO(FT_IRQ) + 4) & 1u);
				if (!perr && (data || !low)) {
					ft_irq_arm();
					armed = true;
				} else {
					rearm_at = mach_absolute_time() + poll_abs;   /* empty or failed: not straight back */
				}
			}
			if (ft.active) {
				ft.c_polls++;
				ft.c_poll_sum += took;
				ft.c_poll_max = took > ft.c_poll_max ? took : ft.c_poll_max;
			}
			if (perr) {
				errors++;
				if (errors == 1 || (errors % 100u) == 0) {
					printf("FUXI TOUCH: FIFO read error %d (%u in a row)\n", perr, errors);
				}
				/* Do not leave a finger stuck down while reads fail. */
				if (errors >= 2 && ft.active) {
					ft_report(0);
				}
				if (errors >= 2) {
					fuxi_wake_touch_reset(&ft.wake);
				}
				/* A timed-out transfer leaves TREs queued: reset the GPII rings. */
				if (errors == 3 || (errors % 50u) == 0) {
					int rerr = ft_gpi_init();
					printf("FUXI TOUCH: GPI re-init after %u errors -> %d\n", errors, rerr);
				}
			} else {
				if (errors >= 3) {
					printf("FUXI TOUCH: FIFO reads recovered after %u errors\n", errors);
				}
				errors = 0;
			}
			if (ft.ic_reset) {
				/* Controller-ready event while sensing: the IC reset itself. */
				ft.ic_reset = false;
				if (ft.active) {
					ft_report(0);
				}
				fuxi_wake_touch_reset(&ft.wake);
				int serr = ft_tx(sense, 3, false);
				printf("FUXI TOUCH: controller reset event, sense on again -> %d\n", serr);
			}
		}
		ft_retry_report();
		if (now - last_stats >= stats_abs) {
			last_stats = now;
			ft_irq_stats(irqmode ? "interrupt-driven" : "polling");
		}
		if (irqmode) {
			now = mach_absolute_time();
			if (!armed && now >= rearm_at) {
				ft_irq_arm();
				armed = true;
			}
			uint64_t deadline = now + watch_abs, leeway = leeway_abs;
			if (suspect) {
				deadline = suspect + miss_abs;
				leeway = 0;
			}
			if (!armed && rearm_at < deadline) {
				deadline = rearm_at;
				leeway = 0;
			}
			if (ft.retry && now + poll_abs < deadline) {
				deadline = now + poll_abs;
				leeway = 0;
			}
			ft_irq_wait(seen, deadline, leeway);
			next = mach_absolute_time();   /* polling restarts from here after a fallback */
			continue;
		}
		uint64_t period = off ? off_abs :
		    (ft.active || now - last_irq < recent_abs) ? poll_abs : idle_abs;
		next += period;
		now = mach_absolute_time();
		if (next <= now) {
			next = now + period;
		}
		ft_sleep_until(next);
	}
out:
	printf("FUXI TOUCH: stopped\n");
	(void)thread_terminate(current_thread());
	thread_block(THREAD_CONTINUE_NULL);
}

void
fuxi_touch_init(void)
{
	int enabled = 1;
	thread_t thread;
	if (ft.started) {
		return;
	}
	ft.started = true;
	(void)PE_parse_boot_argn("fuxitouch", &enabled, sizeof(enabled));
	if (!enabled) {
		return;
	}
	if (kernel_thread_start_priority(ft_thread, NULL, FT_PRIORITY, &thread) == KERN_SUCCESS) {
		thread_deallocate(thread);
	}
}
#endif /* FUXI */
