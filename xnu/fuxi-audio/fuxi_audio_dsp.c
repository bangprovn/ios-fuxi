/* Fuxi audio lane, P-AU3 and P-AU4 in XNU: the ADSP the loader left running (boot/ios-remote.sh ADSP=1), spoken to from a kernel thread.
 * boot-arg fuxiaudio=1: GLINK over SMEM, polled, as shim/fuxi-probe/au_glink.h; the APM's state; LPASS's votes; the secondary TDM endpoint's graph and a stream into
 * it, started on a second of zeros, and taken down again. What a-play2 did before iOS, here beside iOS.
 * fuxiaudio=2: instead /dev/fuxiaudio, which takes 48 kHz stereo 16 bit from tools/fuxi-audio's HAL plug-in in mediaserverd and never blocks it. The graph starts at the first
 * samples and comes down after two seconds without any. Whichever: no pin is muxed, no amplifier reached, nothing is heard; what this proves is iOS's samples reaching the DSP.
 * fuxiaudioamp=1, with fuxiaudio=2: the bottom amplifier as well, its pins and up while there are samples, at the lowest level the owner has agreed to in the loader. **Not yet run: the
 * owner has agreed to a second of tone there (a-tone5), not to iOS's sound.** Off without it, and then the two functions it calls do nothing at all.
 * fuxiaudio=3: as 2, and /dev/fuxiaudiomic gives what the mic hears to whoever has it open: 48 kHz, one channel of 16 bits, never holding the reader. fuxiaudio=4: as 3, with eight seconds of it taken at the start and counted, with nobody reading.
 * The loader has put apps_smmu's streams 0x1001/0x80 and 0x1061 into context bank 4 (poke 2 and 18); this does not touch the SMMU. The buffer is outside the bank XNU owns. */
#ifdef FUXI
#include <stdint.h>
#include <stdbool.h>
#include <sys/param.h>
#include <sys/systm.h>
#include <sys/conf.h>
#include <sys/errno.h>
#include <sys/proc.h>
#include <sys/uio.h>
#include <miscfs/devfs/devfs.h>
#include <string.h>
#include <kern/clock.h>
#include <kern/thread.h>
#include <kern/sched_prim.h>
#include <machine/machine_routines.h>
#include <pexpert/pexpert.h>
#include "fuxi_audioreach.h"

extern vm_offset_t ml_io_map(vm_offset_t, vm_size_t);
extern void delay_for_interval(uint32_t, uint32_t);
extern kern_return_t thread_terminate(thread_t);
void fuxi_audio_dsp_init(void);

#define FA_SMEM         0x81d00000ull   /* smem_region */
#define FA_SMEM_SIZE    0x00200000u
#define FA_IPCC         0x00408000ull
#define FA_IPCC_SIZE    0x00001000u
#define FA_IPCC_GLINK   ((3u << 16) | 0u)    /* client LPASS, signal GLINK_QMP */
#define FA_BUFFER       0xd0000000ull
#define FA_BUFFER_BYTES (4u * 15360u)
#define FA_BUFFER_SID   1ull            /* 0x1001 & 0xf, as Linux q6apm_dai */
#define FA_PERIOD_BYTES 15360u          /* 20 ms of 48 kHz, 4 slots of 32 bits: the same on all four, as a-tone5 proved */
#define FA_TDM_CLOCK    0x202u          /* LPASS_CLK_ID_SEC_TDM_IBIT, as the PRM numbers it */
#define FA_PERIODS      50u
#define FA_PERIOD_FRAMES 960u
#define FA_RING_FRAMES  16384u          /* a power of two: about 340 ms */
#define FA_AHEAD        3u              /* periods the DSP holds at once */
#define FA_IDLE_PERIODS 100u            /* 2 s of none, and the graph comes down */

static vm_offset_t fa_smem, fa_ipcc, fa_buffer;

static uint32_t
fa_rd(uint64_t pa)
{
	if (pa >= FA_SMEM && pa + 4 <= FA_SMEM + FA_SMEM_SIZE) {
		return *(volatile uint32_t *)(fa_smem + (vm_offset_t)(pa - FA_SMEM));
	}
	return 0xffffffffu;
}

static void
fa_wr(uint64_t pa, uint32_t v)
{
	__asm__ volatile ("dmb oshst" ::: "memory");
	if (pa >= FA_SMEM && pa + 4 <= FA_SMEM + FA_SMEM_SIZE) {
		*(volatile uint32_t *)(fa_smem + (vm_offset_t)(pa - FA_SMEM)) = v;
	}
	__asm__ volatile ("dsb osh" ::: "memory");
}

#define AU_GL_RD(pa) fa_rd(pa)
#define AU_GL_WR(pa, v) fa_wr((pa), (v))
#define AU_GL_WMB() __asm__ volatile ("dsb osh" ::: "memory")
#define AU_GL_KICK() (*(volatile uint32_t *)(fa_ipcc + 0xc) = FA_IPCC_GLINK)
#include "fuxi_au_glink.h"

struct fa_part {
	uint64_t pa;
	uint32_t size, free_unc;
};

/* SMEM's table of partitions, then the uncached items of one: Linux smem.c, bounded, as shim/fuxi-probe/audio.c reads them. */
static bool
fa_smem_part(unsigned h0, unsigned h1, struct fa_part *p)
{
	uint64_t pt = FA_SMEM + FA_SMEM_SIZE - 0x1000;
	uint32_t n;

	if (fa_rd(pt) != 0x434f5424u || (n = fa_rd(pt + 8)) > 64) {
		return false;
	}
	for (unsigned i = 0; i < n; i++) {
		uint64_t e = pt + 0x20 + 0x30 * i;
		uint32_t off = fa_rd(e), size = fa_rd(e + 4), hosts = fa_rd(e + 12);
		unsigned a = hosts & 0xffff, b = hosts >> 16;

		if (!((a == h0 && b == h1) || (a == h1 && b == h0))) {
			continue;
		}
		if (off == 0 || off >= FA_SMEM_SIZE || size < 0x20 || size > FA_SMEM_SIZE - off) {
			return false;
		}
		p->pa = FA_SMEM + off;
		p->size = size;
		if (fa_rd(p->pa) != 0x54525024u) {
			return false;
		}
		p->free_unc = fa_rd(p->pa + 12);
		return p->free_unc >= 0x20 && p->free_unc <= size;
	}
	return false;
}

static uint64_t
fa_smem_item(const struct fa_part *p, unsigned item, uint32_t *size)
{
	uint64_t e = p->pa + 0x20, end = p->pa + p->free_unc;

	for (unsigned i = 0; i < 512 && e + 16 <= end; i++) {
		uint32_t w0 = fa_rd(e), sz = fa_rd(e + 4), w2 = fa_rd(e + 8);
		uint64_t data = e + 16 + (w2 >> 16);

		if ((w0 & 0xffff) != 0xa5a5 || sz > end - data) {
			return 0;
		}
		if ((w0 >> 16) == item) {
			if (size != NULL) {
				*size = sz - (w2 & 0xffff);
			}
			return data;
		}
		e = data + sz;
	}
	return 0;
}

/* The DSP's ready bits, from its SMP2P item: "slave-kernel" fatal b0, ready b1, handover b2. */
static uint32_t
fa_ready_bits(const struct fa_part *p)
{
	uint64_t in = fa_smem_item(p, 443, NULL);

	if (in == 0 || fa_rd(in) != 0x504d5324u) {
		return 0x40000000u;
	}
	unsigned valid = fa_rd(in + 12) >> 16;
	for (unsigned e = 0; e < valid && e < 16; e++) {
		uint64_t ent = in + 20 + 20 * e;

		if (fa_rd(ent) == 0x76616c73u && fa_rd(ent + 4) == 0x656b2d65u) {
			return fa_rd(ent + 16) & 0xffff;
		}
	}
	return 0x10000000u;
}

static struct au_gl fa_g;
static uint32_t fa_log[AU_GL_REC_WORDS * 8];
static uint32_t fa_m[2 + AU_GL_GPR_BUFFER / 4 + 2];
static uint64_t fa_start;
static uint32_t fa_polls;

static void
fa_sleep_ms(unsigned ms)
{
	assert_wait_timeout((event_t)&fa_g, THREAD_UNINT, ms, NSEC_PER_MSEC);
	thread_block(THREAD_CONTINUE_NULL);
}

/* What has come, for up to `ms`, a millisecond at a time. False if the ring cannot be trusted any more or the DSP says it has fallen over. */
static bool
fa_poll(unsigned ms)
{
	struct fa_part p;

	for (unsigned i = 0; i <= ms; i++) {
		uint64_t ns;

		absolutetime_to_nanoseconds(mach_absolute_time() - fa_start, &ns);
		fa_g.ms = (uint32_t)(ns / NSEC_PER_MSEC);
		while (au_gl_step(&fa_g)) {
		}
		if (fa_g.stuck != 0) {
			printf("FUXI AUDIO: GLINK stuck %u\n", fa_g.stuck);
			return false;
		}
		if ((fa_polls++ & 0xffu) == 0 && fa_smem_part(0, 2, &p)) {
			uint32_t bits = fa_ready_bits(&p);

			if ((bits & 0xf0000000u) == 0 && (bits & 1) != 0) {
				printf("FUXI AUDIO: the DSP says it has fallen over, bits %#x\n", bits);
				return false;
			}
		}
		if (i < ms) {
			fa_sleep_ms(1);
		}
	}
	return true;
}

/* One step of the list, into the long buffer: waits for its answer, up to a second. The status, or AU_GL_PRM_NO_ANSWER. */
static uint32_t
fa_step(uint32_t bytes)
{
	uint32_t step = fa_g.graph_sent;

	for (unsigned i = 0; i < 50 && !fa_g.big_have; i++) {
		(void)au_gl_big_ask(&fa_g);
		if (!fa_poll(20)) {
			return AU_GL_PRM_NO_ANSWER;
		}
	}
	if (bytes == 0 || !au_gl_graph_send(&fa_g, fa_m, bytes)) {
		printf("FUXI AUDIO: step %u not sent: %u bytes, the long buffer %u\n", step, bytes, fa_g.big_have);
		fa_g.graph_steps = 0;
		return AU_GL_PRM_NO_ANSWER;
	}
	for (unsigned i = 0; i < 1000; i++) {
		if (!fa_poll(1)) {
			break;
		}
		if (fa_g.graph_status[step] != AU_GL_PRM_NO_ANSWER) {
			break;
		}
	}
	return fa_g.graph_status[step];
}

#define FA_STEP(what, build, ...) do { \
	uint32_t status = fa_step(build(((uint8_t *)(fa_m + 2)), AU_GL_GPR_BUFFER, 1, AU_GL_GRAPH_TOKEN + fa_g.graph_sent, ##__VA_ARGS__)); \
	printf("FUXI AUDIO: %s: status %#x\n", what, status); \
	if (status != 0) { bad++; } \
} while (0)

static const FuxiArFormat fa_format = {48000, 32, 4};
static const FuxiArTdm fa_tdm = {.intf = 1, .slot_mask = 0xf, .slots = 4, .slot_width = 32, .sync_mode = 1, .invert_sync = 0, .sync_data_delay = 1, .lane = 1, .clock_attribute = 4};

/* The mappings, GLINK and the APM's answer. */
static bool
fa_bring_up(void)
{
	struct fa_part p;
	uint32_t desc_size = 0, tx_size = 0, rx_size = 0, bits;
	uint64_t desc, tx, rx = 0;

	fa_start = mach_absolute_time();
	fa_smem = ml_io_map((vm_offset_t)FA_SMEM, FA_SMEM_SIZE);
	fa_ipcc = ml_io_map((vm_offset_t)FA_IPCC, FA_IPCC_SIZE);
	fa_buffer = ml_io_map((vm_offset_t)FA_BUFFER, FA_BUFFER_BYTES);
	if (fa_smem == 0 || fa_ipcc == 0 || fa_buffer == 0) {
		printf("FUXI AUDIO: no mapping\n");
		return false;
	}
	if (!fa_smem_part(0, 2, &p)) {
		printf("FUXI AUDIO: no AP-ADSP partition\n");
		return false;
	}
	bits = fa_ready_bits(&p);
	printf("FUXI AUDIO: DSP ready bits %#x\n", bits);
	if (bits != 6) {
		printf("FUXI AUDIO: not ready: was the loader told ADSP=1?\n");
		return false;
	}
	desc = fa_smem_item(&p, 478, &desc_size);
	tx = fa_smem_item(&p, 479, &tx_size);
	if (desc == 0 || desc_size != 32 || tx == 0 || tx_size < 1024 || tx_size > 0x100000 || (tx_size & 7) != 0) {
		printf("FUXI AUDIO: GLINK's items: descriptor %#llx %u, tx %#llx %u\n", (unsigned long long)desc, desc_size, (unsigned long long)tx, tx_size);
		return false;
	}
	fa_g.desc = desc;
	fa_g.tx_fifo = tx;
	fa_g.tx_len = tx_size;
	fa_g.options = AU_GL_ANSWER | AU_GL_GPR | AU_GL_PRM | AU_GL_GRAPH | AU_GL_PLAY;
	fa_g.log = fa_log;
	fa_g.log_most = 8;
	fa_g.graph_steps = AU_GL_GRAPH_MOST;
	if (!au_gl_start(&fa_g)) {
		printf("FUXI AUDIO: GLINK did not start\n");
		return false;
	}
	for (unsigned i = 0; i < 30000 && rx == 0; i++) {
		rx = fa_smem_item(&p, 480, &rx_size);
		if (rx == 0) {
			fa_sleep_ms(1);
			(void)fa_smem_part(0, 2, &p);
		}
	}
	if (rx == 0 || rx_size < 1024 || rx_size > 0x100000 || (rx_size & 7) != 0) {
		printf("FUXI AUDIO: the DSP's FIFO: %#llx %u\n", (unsigned long long)rx, rx_size);
		return false;
	}
	fa_g.rx_fifo = rx;
	fa_g.rx_len = rx_size;
	if (!fa_poll(2000)) {
		return false;
	}
	printf("FUXI AUDIO: GLINK up: messages %u, channels %u, adsp_apps %u, its buffers %u\n", fa_g.rx_msgs, fa_g.opened, fa_g.gpr_lcid, fa_g.gpr_riid_n);
	(void)au_gl_gpr_ask(&fa_g);
	if (!fa_poll(500)) {
		return false;
	}
	printf("FUXI AUDIO: APM ready %u after %u answers\n", fa_g.gpr_ready, fa_g.gpr_heard);
	return fa_g.gpr_ready != 0;
}

/* The TDM's bit clock, from the PRM: Linux's card asks it before anything opens. 6.144 MHz, inverted. The same shape as the others for FA_STEP; 0 lets it go. */
static uint32_t
fa_prm_clock(uint8_t *out, uint32_t capacity, uint32_t port, uint32_t token, uint32_t hz)
{
#pragma unused(port)
	return fuxi_ar_prm_clock_attr(out, capacity, 2, token, FA_TDM_CLOCK, hz, 4);
}

/* LPASS's votes, held for the speaker and the mic alike: on with the first, off with the last, so nothing of LPASS is touched behind them. How many failed. */
static unsigned fa_votes;

static uint32_t
fa_votes_take(void)
{
	uint32_t bad = 0;

	if (fa_votes++ != 0) {
		return 0;
	}
	fa_g.prm_sent = 0;
	for (unsigned i = 0; i < 2; i++) {
		(void)au_gl_prm_step(&fa_g);
		if (!fa_poll(100)) {
			return 1;
		}
		printf("FUXI AUDIO: vote %u: status %#x\n", i, fa_g.prm_status[i]);
		bad += fa_g.prm_status[i] != 0;
	}
	return bad;
}

static void
fa_votes_drop(void)
{
	if (--fa_votes != 0) {
		return;
	}
	fa_g.prm_sent = 2;
	for (unsigned i = 2; i < 4; i++) {
		(void)au_gl_prm_step(&fa_g);
		if (!fa_poll(100)) {
			break;
		}
		printf("FUXI AUDIO: vote %u: status %#x\n", i, fa_g.prm_status[i]);
	}
}

/* LPASS's votes on, and the list to the graph's start. How many failed. */
static uint32_t
fa_graph_up(void)
{
	uint32_t bad = 0;

	fa_g.graph_sent = 0;
	fa_g.map_handle = 0;
	fa_g.write_sent = 0;
	fa_g.write_done = 0;
	fa_g.write_bad = 0;
	bad += fa_votes_take();
	for (unsigned i = 0; i < FA_BUFFER_BYTES / 4; i++) {
		((volatile uint32_t *)fa_buffer)[i] = 0;
	}
	__asm__ volatile ("dsb osh" ::: "memory");
	FA_STEP("TDM clock", fa_prm_clock, 6144000u);
	FA_STEP("open device", fuxi_ar_open_speaker_tdm);
	FA_STEP("configure device", fuxi_ar_config_speaker_tdm, &fa_format, &fa_tdm);
	FA_STEP("open stream", fuxi_ar_open_playback_stream_tdm);
	FA_STEP("configure stream", fuxi_ar_config_playback_stream, &fa_format);
	FA_STEP("configure its input", fuxi_ar_config_playback_shmem, &fa_format);
	FA_STEP("map the buffers", fuxi_ar_map, FA_BUFFER | FA_BUFFER_SID << 32, FA_BUFFER_BYTES);
	FA_STEP("prepare device", fuxi_ar_graph_command, FUXI_AR_OP_GRAPH_PREPARE, FUXI_AR_SG_SPEAKER);
	FA_STEP("prepare stream", fuxi_ar_graph_command, FUXI_AR_OP_GRAPH_PREPARE, FUXI_AR_SG_PLAY);
	FA_STEP("start device", fuxi_ar_graph_command, FUXI_AR_OP_GRAPH_START, FUXI_AR_SG_SPEAKER);
	FA_STEP("start stream", fuxi_ar_graph_command, FUXI_AR_OP_GRAPH_START, FUXI_AR_SG_PLAY);
	return bad;
}

/* The list's other end, whatever came of the start: the votes come off even so. */
static void
fa_graph_down(void)
{
	uint32_t bad = 0;

	/* In step with au_gl_graph_send(): the list's place is its token. */
	fa_g.graph_sent = 11;
	FA_STEP("stop stream", fuxi_ar_graph_command, FUXI_AR_OP_GRAPH_STOP, FUXI_AR_SG_PLAY);
	FA_STEP("stop device", fuxi_ar_graph_command, FUXI_AR_OP_GRAPH_STOP, FUXI_AR_SG_SPEAKER);
	FA_STEP("close stream", fuxi_ar_graph_command, FUXI_AR_OP_GRAPH_CLOSE, FUXI_AR_SG_PLAY);
	FA_STEP("close device", fuxi_ar_graph_command, FUXI_AR_OP_GRAPH_CLOSE, FUXI_AR_SG_SPEAKER);
	FA_STEP("unmap", fuxi_ar_unmap, fa_g.map_handle);
	FA_STEP("TDM clock off", fa_prm_clock, 0u);
	fa_votes_drop();
	printf("FUXI AUDIO: down, %u steps failed; rx %u tx %u full %u\n", bad, fa_g.rx_msgs, fa_g.tx_msgs, fa_g.tx_full);
}

/* ---- /dev/fuxiaudio: one writer, mediaserverd's I/O thread, and this file's thread. Packed left, right. Too much is dropped and counted: the writer is never held. ---- */
static uint32_t fa_ring[FA_RING_FRAMES];
static uint32_t fa_ring_head, fa_ring_tail, fa_dropped;
static int fa_dev_read(dev_t dev, struct uio *uio, int flags);
static int fa_dev_open(dev_t dev, int flags, int mode, proc_t p);
static int fa_dev_close(dev_t dev, int flags, int mode, proc_t p);

static uint32_t
fa_ring_count(void)
{
	return __atomic_load_n(&fa_ring_head, __ATOMIC_ACQUIRE) - __atomic_load_n(&fa_ring_tail, __ATOMIC_RELAXED);
}

static int
fa_dev_write(dev_t dev, struct uio *uio, int flags)
{
#pragma unused(dev, flags)
	uint32_t chunk[256];

	while (uio_resid(uio) >= 4) {
		int error;
		uint32_t frames = uio_resid(uio) / 4 > 256 ? 256u : (uint32_t)(uio_resid(uio) / 4);
		uint32_t head = __atomic_load_n(&fa_ring_head, __ATOMIC_RELAXED), tail = __atomic_load_n(&fa_ring_tail, __ATOMIC_ACQUIRE);

		if ((error = uiomove((caddr_t)chunk, (int)(frames * 4), uio)) != 0) {
			return error;
		}
		for (uint32_t i = 0; i < frames; i++) {
			if (head - tail >= FA_RING_FRAMES) {
				fa_dropped += frames - i;
				break;
			}
			fa_ring[head++ & (FA_RING_FRAMES - 1)] = chunk[i];
		}
		__atomic_store_n(&fa_ring_head, head, __ATOMIC_RELEASE);
	}
	return 0;
}

static const struct cdevsw fa_cdevsw = {
	.d_open       = fa_dev_open,
	.d_close      = fa_dev_close,
	.d_read       = fa_dev_read,
	.d_write      = fa_dev_write,
	.d_ioctl      = eno_ioctl,
	.d_stop       = eno_stop,
	.d_reset      = eno_reset,
	.d_ttys       = NULL,
	.d_select     = eno_select,
	.d_mmap       = eno_mmap,
	.d_strategy   = eno_strat,
	.d_reserved_1 = eno_getc,
	.d_reserved_2 = eno_putc,
	.d_type       = 0,
};

/* ---- the bottom amplifier, fuxiaudioamp=1: au_amp_on.h's sequence, as the loader's au_tone_stage() ran it ---- */
#define FA_SE0  0x00880000ull
#define FA_GCC  0x00100000ull
#define FA_TLMM 0x0f100000ull
static vm_offset_t fa_se0, fa_gcc, fa_tlmm;
static bool fa_amp_wanted, fa_amp_up_now, fa_amp_gave_up;

static vm_offset_t
fa_amp_va(uint64_t pa)
{
	if (pa >= FA_SE0 && pa < FA_SE0 + 0x4000) {
		return fa_se0 + (vm_offset_t)(pa - FA_SE0);
	}
	if (pa >= FA_GCC && pa < FA_GCC + 0x80000) {
		return fa_gcc + (vm_offset_t)(pa - FA_GCC);
	}
	if (pa >= FA_TLMM && pa < FA_TLMM + 0x100000 + 0x4000) {
		return fa_tlmm + (vm_offset_t)(pa - FA_TLMM);
	}
	panic("FUXI AUDIO: no window for %#llx", (unsigned long long)pa);
}
static uint32_t fa_amp_rd(uint64_t pa) { return *(volatile uint32_t *)fa_amp_va(pa); }
static int
fa_amp_wr(uint64_t pa, uint32_t v)
{
	__asm__ volatile ("dmb oshst" ::: "memory");
	*(volatile uint32_t *)fa_amp_va(pa) = v;
	__asm__ volatile ("dsb osh" ::: "memory");
	return 0;
}
#define AU_AMP_RD(pa) fa_amp_rd(pa)
#define AU_AMP_WR(pa, v) fa_amp_wr((pa), (v))
#define AU_AMP_DELAY_US(us) delay_for_interval((us), 1000u)
#include "fuxi_au_amp.h"
#include "fuxi_au_amp_on.h"

static const uint8_t fa_amp_pins[8] = {0, 1, 182, 183, 121, 122, 123, 124};
static uint32_t fa_amp_saved[8][2];
static struct au_on fa_amp;

/* As the loader's au_pin(): the pin's control, claimed from LPASS where it can be, and its level before the output turns on. */
static void
fa_amp_pin(unsigned n, uint32_t ctl, uint32_t out)
{
	uint64_t c = FA_TLMM + 0x1000u * n;
	uint32_t was = fa_amp_rd(c);

	ctl |= was & (1u << 11);
	if ((was & (1u << 11)) != 0) {
		ctl |= 1u << 12;
	}
	if ((ctl & (1u << 9)) != 0) {
		(void)fa_amp_wr(c + 4, out << 1);
	}
	(void)fa_amp_wr(c, ctl);
}

static void
fa_amp_pins_back(void)
{
	fa_amp_pin(183, 1u | 1u << 9, 0);
	delay_for_interval(3000, 1000);
	for (unsigned i = 0; i < 8; i++) {
		(void)fa_amp_wr(FA_TLMM + 0x1000u * fa_amp_pins[i] + 4, fa_amp_saved[i][1] & 2u);
		(void)fa_amp_wr(FA_TLMM + 0x1000u * fa_amp_pins[i], fa_amp_saved[i][0]);
	}
}

/* With the graph started, zeros going. Any trouble: it goes down and stays down until the next boot. Nothing is retried. */
static void
fa_amp_up(void)
{
	int r;

	if (!fa_amp_wanted || fa_amp_gave_up || fa_amp_up_now) {
		return;
	}
	(void)fa_amp_wr(FA_GCC + 0x52010, fa_amp_rd(FA_GCC + 0x52010) | 0x1fu);
	for (unsigned i = 0; i < 10000 && (fa_amp_rd(FA_GCC + 0x233cc) >> 31) != 0; i++) {
		delay_for_interval(10, 1000);
	}
	if ((fa_amp_rd(FA_GCC + 0x233cc) >> 31) != 0 || !au_amp_engine(FA_SE0)) {
		printf("FUXI AUDIO: amp: no bus\n");
		fa_amp_gave_up = true;
		return;
	}
	for (unsigned i = 0; i < 10000 && (fa_amp_rd(FA_GCC + 0x1e004) >> 31) != 0; i++) {
		delay_for_interval(10, 1000);
	}
	for (unsigned i = 0; i < 8; i++) {
		fa_amp_saved[i][0] = fa_amp_rd(FA_TLMM + 0x1000u * fa_amp_pins[i]);
		fa_amp_saved[i][1] = fa_amp_rd(FA_TLMM + 0x1000u * fa_amp_pins[i] + 4);
	}
	/* The top one in reset all along; gpio68, its earpiece switch, not touched. */
	fa_amp_pin(182, 1u | 1u << 9, 0);
	fa_amp_pin(183, 1u | 1u << 9, 0);
	fa_amp_pin(0, 3u | 2u << 2, 0);
	fa_amp_pin(1, 3u | 2u << 2, 0);
	fa_amp_pin(121, 1u << 2 | 3u << 6, 0);
	fa_amp_pin(123, 1u << 2 | 3u << 6, 0);
	fa_amp_pin(124, 1u << 2 | 3u << 6, 0);
	delay_for_interval(3000, 1000);
	fa_amp_pin(183, 1u | 1u << 9, 1);
	delay_for_interval(3000, 1000);
	memset(&fa_amp, 0, sizeof(fa_amp));
	fa_amp.se = FA_SE0;
	fa_amp.address = 0x40;
	r = au_on_prepare(&fa_amp, true, false);
	if (r == AU_ON_OK) {
		r = au_on_power_up(&fa_amp);
	}
	if (r == AU_ON_OK) {
		r = au_on_check(&fa_amp);
	}
	if (r == AU_ON_OK) {
		r = au_on_unmute(&fa_amp, 0);
	}
	printf("FUXI AUDIO: amp up: %#x at step %u, reg %#x want %#x got %#x, status %#x %#x\n", r, fa_amp.step, fa_amp.failed_reg, fa_amp.failed_want, fa_amp.failed_got, fa_amp.status[0], fa_amp.status[2]);
	if (r != AU_ON_OK) {
		fa_amp_gave_up = true;
		au_on_down(&fa_amp);
		fa_amp_pins_back();
		return;
	}
	fa_amp_up_now = true;
}

static void
fa_amp_down(void)
{
	if (!fa_amp_up_now) {
		return;
	}
	au_on_down(&fa_amp);
	fa_amp_pins_back();
	fa_amp_up_now = false;
	printf("FUXI AUDIO: amp down\n");
}

/* Once in a while as it plays: a flag and it comes down for good. */
static void
fa_amp_watch(void)
{
	if (fa_amp_up_now && au_on_check(&fa_amp) != AU_ON_OK) {
		printf("FUXI AUDIO: amp flag: status %#x %#x %#x %#x\n", fa_amp.status[0], fa_amp.status[1], fa_amp.status[2], fa_amp.status[3]);
		fa_amp_gave_up = true;
		fa_amp_down();
	}
}

/* ---- the microphone, fuxiaudio=3 and 4: au_mic.h's stage and the capture a-cap1 ran in the loader, while someone has /dev/fuxiaudiomic open. Numbers go to the log, samples only to that reader.
 * L15B is not voted here: "no other XNU code drives the fuxi apps RSC" (xnu/fuxi-wlan), which votes it on and never off. Without it the codec does not attach, and this stops at step 5, whole. ---- */
#define FA_MIC_BUFFER   0xd0010000ull   /* as the loader's: past the period buffers */
#define FA_MIC_PERIOD   960u            /* 10 ms of 48 kHz, one channel of 16 bits */
#define FA_MIC_SLOTS    4u
#define FA_MIC_RING     16384u          /* samples, a power of two: about 340 ms */
#define FA_MIC_LIST     17u             /* the list's places after the speaker's, one list at a time */
#define FA_MIC_WINDOWS  6u
static const struct { uint64_t pa; uint32_t size; } fa_mic_windows[FA_MIC_WINDOWS] = {
	{0x06d44000ull, 0x1000u}, {0x06ae0000ull, 0x1000u}, {0x06d30000ull, 0x10000u}, {0x06e80000ull, 0xf000u}, {0x0725a000ull, 0x1000u}, {0x0f16c000ull, 0x1000u},
};
static vm_offset_t fa_mic_va[FA_MIC_WINDOWS], fa_mic_buffer;

static vm_offset_t
fa_mic_at(uint64_t pa)
{
	for (unsigned i = 0; i < FA_MIC_WINDOWS; i++) {
		if (pa >= fa_mic_windows[i].pa && pa < fa_mic_windows[i].pa + fa_mic_windows[i].size) {
			return fa_mic_va[i] + (vm_offset_t)(pa - fa_mic_windows[i].pa);
		}
	}
	panic("FUXI AUDIO: no window for %#llx", (unsigned long long)pa);
}
static uint32_t
fa_mic_rd(uint64_t pa, uint32_t *faulted)
{
	*faulted = 0;
	return *(volatile uint32_t *)fa_mic_at(pa);
}
static unsigned
fa_mic_wr(uint64_t pa, uint32_t v)
{
	__asm__ volatile ("dmb oshst" ::: "memory");
	*(volatile uint32_t *)fa_mic_at(pa) = v;
	__asm__ volatile ("dsb osh" ::: "memory");
	return 0;
}
#define AU_MIC_RD(pa, f) fa_mic_rd((pa), (f))
#define AU_MIC_WR(pa, v) fa_mic_wr((pa), (v))
#define AU_MIC_DELAY_US(us) delay_for_interval((us), 1000u)
#define AU_MIC_CMDDB(name) AU_MIC_L15B
#define AU_MIC_RPMH(addr, data) 0u
#include "fuxi_au_mic.h"

static const FuxiArFormat fa_mic_format = {48000, 16, 1};
static struct au_mic fa_mic;
static uint32_t fa_mic_rep[128];
static bool fa_mic_on, fa_mic_up_now, fa_mic_gave_up, fa_mic_muted;
static volatile bool fa_mic_wanted;
static uint32_t fa_mic_handle, fa_mic_scanned, fa_mic_since, fa_mic_test_until, fa_mic_said, fa_mic_dropped;
static uint32_t fa_mic_peak, fa_mic_count;
static uint64_t fa_mic_abs, fa_mic_total;
static int16_t fa_mic_ring[FA_MIC_RING];
static uint32_t fa_mic_head, fa_mic_tail;

/* The TX core's clock, from the PRM: the same shape as the others for FA_STEP; 0 lets it go. */
static uint32_t
fa_mic_clock(uint8_t *out, uint32_t capacity, uint32_t port, uint32_t token, uint32_t hz)
{
#pragma unused(port)
	return fuxi_ar_prm_clock_attr(out, capacity, 2, token, AU_MIC_CLOCK, hz, 1);
}

static void
fa_mic_up(void)
{
	uint32_t bad = 0, before = fa_g.map_handle, *r = fa_mic_rep;

	if (fa_votes_take() != 0) {
		printf("FUXI AUDIO: mic: the votes did not come\n");
		fa_votes_drop();
		fa_mic_gave_up = true;
		return;
	}
	fa_g.graph_sent = FA_MIC_LIST;
	FA_STEP("mic clock", fa_mic_clock, 19200000u);
	if (bad != 0) {
		fa_votes_drop();
		fa_mic_gave_up = true;
		return;
	}
	au_mic_begin(&fa_mic, fa_mic_rep);
	fa_mic.capture = true;
	fa_mic.port = 3;
	fa_mic.lane = 1;
	fa_mic.mic = 5;
	fa_mic.votes_held = true;
	fa_mic.clock_held = true;
	au_mic_run(&fa_mic);
	printf("FUXI AUDIO: mic: %u of 7 steps, stopped at %u why %u; master %#x, device %u, chip %02x %02x, %u read back, bus %#x; port lane %#x channels %#x\n", r[AU_MIC_R_PASSED], r[AU_MIC_R_STEP], r[AU_MIC_R_WHY],
	    r[AU_MIC_R_VERSION], r[AU_MIC_R_SLAVES + 2], r[AU_MIC_R_CHIP + 2], r[AU_MIC_R_CHIP + 3], r[AU_MIC_R_BACK_N], r[AU_MIC_R_BUS], r[AU_MIC_R_PORTS], r[AU_MIC_R_PORTS + 1]);
	if (!fa_mic.held) {
		fa_g.graph_sent = FA_MIC_LIST;
		FA_STEP("mic clock off", fa_mic_clock, 0u);
		fa_votes_drop();
		fa_mic_gave_up = true;
		return;
	}
	FA_STEP("open mic device", fuxi_ar_open_mic_device);
	FA_STEP("configure it", fuxi_ar_config_mic_device, &fa_mic_format, 1u);
	FA_STEP("open capture", fuxi_ar_open_capture_stream);
	FA_STEP("configure it", fuxi_ar_config_capture_stream, &fa_mic_format);
	FA_STEP("map the mic's buffers", fuxi_ar_map, FA_MIC_BUFFER | FA_BUFFER_SID << 32, FA_MIC_SLOTS * FA_MIC_PERIOD);
	/* The speaker's handle stays where its list looks for it. Any handle lets au_gl_read_send() go; the packets say which. */
	fa_mic_handle = fa_g.map_handle;
	if (before != 0) {
		fa_g.map_handle = before;
	}
	FA_STEP("prepare mic device", fuxi_ar_graph_command, FUXI_AR_OP_GRAPH_PREPARE, FUXI_AR_SG_MIC);
	FA_STEP("prepare capture", fuxi_ar_graph_command, FUXI_AR_OP_GRAPH_PREPARE, FUXI_AR_SG_CAPTURE);
	FA_STEP("start mic device", fuxi_ar_graph_command, FUXI_AR_OP_GRAPH_START, FUXI_AR_SG_MIC);
	FA_STEP("start capture", fuxi_ar_graph_command, FUXI_AR_OP_GRAPH_START, FUXI_AR_SG_CAPTURE);
	fa_g.read_sent = 0;
	fa_g.read_done = 0;
	fa_g.read_bad = 0;
	fa_mic_scanned = 0;
	fa_mic_up_now = true;
	fa_mic_muted = true;
	fa_mic_since = fa_g.ms;
	fa_mic_said = fa_g.ms;
	fa_mic_peak = 0;
	fa_mic_abs = 0;
	fa_mic_count = 0;
	fa_mic_total = 0;
	if (bad != 0) {
		printf("FUXI AUDIO: mic: %u steps of its graph failed\n", bad);
	}
}

static void
fa_mic_down(void)
{
	uint32_t bad = 0;

	fa_mic_up_now = false;
	fa_g.graph_sent = FA_MIC_LIST;
	FA_STEP("stop capture", fuxi_ar_graph_command, FUXI_AR_OP_GRAPH_STOP, FUXI_AR_SG_CAPTURE);
	FA_STEP("stop mic device", fuxi_ar_graph_command, FUXI_AR_OP_GRAPH_STOP, FUXI_AR_SG_MIC);
	FA_STEP("close capture", fuxi_ar_graph_command, FUXI_AR_OP_GRAPH_CLOSE, FUXI_AR_SG_CAPTURE);
	FA_STEP("close mic device", fuxi_ar_graph_command, FUXI_AR_OP_GRAPH_CLOSE, FUXI_AR_SG_MIC);
	FA_STEP("unmap the mic's", fuxi_ar_unmap, fa_mic_handle);
	fa_mic.held = false;
	au_mic_down(&fa_mic);
	FA_STEP("mic clock off", fa_mic_clock, 0u);
	fa_votes_drop();
	printf("FUXI AUDIO: mic down: %u periods, %u bad, %llu samples, %u dropped, %u steps failed; 0x3022 %#x, gpio108 %#x\n", fa_g.read_done, fa_g.read_bad, (unsigned long long)fa_mic_total, fa_mic_dropped, bad, fa_mic_rep[AU_MIC_R_DOWN], fa_mic_rep[AU_MIC_R_DOWN + 2]);
}

/* What has come back into the reader's ring, too much dropped and counted; the next empty buffers; the unmute, after the analog side's 300 ms, and Android's digital gain; a line a second. */
static void
fa_mic_pump(void)
{
	uint32_t head = __atomic_load_n(&fa_mic_head, __ATOMIC_RELAXED), tail = __atomic_load_n(&fa_mic_tail, __ATOMIC_ACQUIRE);

	while (fa_mic_scanned != fa_g.read_done) {
		volatile uint32_t *in = (volatile uint32_t *)(fa_mic_buffer + (fa_mic_scanned % FA_MIC_SLOTS) * FA_MIC_PERIOD);

		for (uint32_t i = 0; i < FA_MIC_PERIOD / 4; i++) {
			uint32_t w = in[i];

			for (uint32_t k = 0; k < 2; k++) {
				int16_t v = (int16_t)(w >> (16 * k));
				uint32_t a = (uint32_t)(v < 0 ? -(int32_t)v : (int32_t)v);

				fa_mic_peak = a > fa_mic_peak ? a : fa_mic_peak;
				fa_mic_abs += a;
				fa_mic_count++;
				if (head - tail >= FA_MIC_RING) {
					fa_mic_dropped++;
				} else {
					fa_mic_ring[head++ & (FA_MIC_RING - 1)] = v;
				}
			}
		}
		fa_mic_scanned++;
		__atomic_store_n(&fa_mic_head, head, __ATOMIC_RELEASE);
	}
	if (fa_g.read_sent < fa_g.read_done + 3) {
		uint32_t bytes = fuxi_ar_read((uint8_t *)(fa_m + 2), AU_GL_GPR_BUFFER, 1, AU_GL_READ_TOKEN + (fa_g.read_sent & 0xffffu), (FA_MIC_BUFFER + (fa_g.read_sent % FA_MIC_SLOTS) * FA_MIC_PERIOD) | FA_BUFFER_SID << 32, fa_mic_handle, FA_MIC_PERIOD);

		if (bytes != 0) {
			(void)au_gl_read_send(&fa_g, fa_m, bytes);
		}
	}
	if (fa_mic_muted && fa_g.ms - fa_mic_since >= 300) {
		fa_mic_muted = false;
		au_mic_set(&fa_mic, AU_MIC_TX + 0x400u, 0x10u, 0u);
		au_mic_set(&fa_mic, AU_MIC_TX + 0x40cu, 0xffu, 0x12u);
	}
	if (fa_g.ms - fa_mic_said >= 1000 && fa_mic_count != 0) {
		fa_mic_total += fa_mic_count;
		printf("FUXI AUDIO: mic: %u periods, %u bad, peak %u, mean abs %u, %u dropped\n", fa_g.read_done, fa_g.read_bad, fa_mic_peak, (uint32_t)(fa_mic_abs / fa_mic_count), fa_mic_dropped);
		fa_mic_said = fa_g.ms;
		fa_mic_peak = 0;
		fa_mic_abs = 0;
		fa_mic_count = 0;
	}
}

/* From this file's thread, between everything else: whether anyone wants it, and its buffers while they do. Once it has failed it stays down until the next boot. */
static void
fa_mic_tick(void)
{
	if (!fa_mic_on) {
		return;
	}
	if (fa_mic_test_until != 0 && fa_g.ms >= fa_mic_test_until) {
		fa_mic_test_until = 0;
		fa_mic_wanted = false;
	}
	if (fa_mic_wanted && !fa_mic_up_now && !fa_mic_gave_up) {
		fa_mic_up();
	} else if (!fa_mic_wanted && fa_mic_up_now) {
		fa_mic_down();
	}
	if (fa_mic_up_now) {
		fa_mic_pump();
	}
}

/* Minor 0 the speaker's, minor 1 the mic's. Only the last close of a node comes here, and the reader of the mic is one. */
static int
fa_dev_open(dev_t dev, int flags, int mode, proc_t p)
{
#pragma unused(flags, mode, p)
	if (minor(dev) == 1) {
		fa_mic_wanted = true;
	}
	return 0;
}

static int
fa_dev_close(dev_t dev, int flags, int mode, proc_t p)
{
#pragma unused(flags, mode, p)
	if (minor(dev) == 1) {
		fa_mic_wanted = false;
	}
	return 0;
}

/* What there is, up to what is asked: nothing, if nothing. The reader's next thing is its own I/O cycle. */
static int
fa_dev_read(dev_t dev, struct uio *uio, int flags)
{
#pragma unused(flags)
	int16_t chunk[256];

	if (minor(dev) != 1) {
		return ENXIO;
	}
	while (uio_resid(uio) >= 2) {
		int error;
		uint32_t tail = __atomic_load_n(&fa_mic_tail, __ATOMIC_RELAXED), have = __atomic_load_n(&fa_mic_head, __ATOMIC_ACQUIRE) - tail;
		uint32_t n = uio_resid(uio) / 2 > 256 ? 256u : (uint32_t)(uio_resid(uio) / 2);

		n = n < have ? n : have;
		if (n == 0) {
			break;
		}
		for (uint32_t i = 0; i < n; i++) {
			chunk[i] = fa_mic_ring[(tail + i) & (FA_MIC_RING - 1)];
		}
		__atomic_store_n(&fa_mic_tail, tail + n, __ATOMIC_RELEASE);
		if ((error = uiomove((caddr_t)chunk, (int)(n * 2), uio)) != 0) {
			return error;
		}
	}
	return 0;
}

/* What has come, or silence, into the next of the DSP's periods. The frames taken. */
static uint32_t
fa_fill_period(uint32_t index, uint32_t *peak)
{
	volatile uint32_t *out = (volatile uint32_t *)(fa_buffer + index * FA_PERIOD_BYTES);
	uint32_t tail = __atomic_load_n(&fa_ring_tail, __ATOMIC_RELAXED), have = fa_ring_count(), taken = have < FA_PERIOD_FRAMES ? have : FA_PERIOD_FRAMES;

	for (uint32_t i = 0; i < FA_PERIOD_FRAMES; i++) {
		uint32_t frame = i < taken ? fa_ring[tail++ & (FA_RING_FRAMES - 1)] : 0;
		int32_t left = (int16_t)(frame & 0xffff), right = (int16_t)(frame >> 16), mono = (left + right) / 2;
		uint32_t a = (uint32_t)(mono < 0 ? -mono : mono);

		*peak = a > *peak ? a : *peak;
		for (uint32_t c = 0; c < 4; c++) {
			out[4 * i + c] = (uint32_t)(mono * 65536);
		}
	}
	__atomic_store_n(&fa_ring_tail, tail, __ATOMIC_RELEASE);
	__asm__ volatile ("dsb osh" ::: "memory");
	return taken;
}

static void
fa_stream(void)
{
	for (;;) {
		uint64_t frames = 0;
		uint32_t idle = 0, peak = 0, short_periods = 0, sent_at_last = 0, last_said = 0;

		while (fa_ring_count() == 0) {
			if (!fa_poll(0)) {
				return;
			}
			fa_mic_tick();
			fa_sleep_ms(fa_mic_up_now ? 2 : 20);
		}
		printf("FUXI AUDIO: samples are coming\n");
		if (fa_graph_up() != 0) {
			printf("FUXI AUDIO: the graph did not come up\n");
			fa_graph_down();
			return;
		}
		fa_amp_up();
		while (idle < FA_IDLE_PERIODS) {
			if (fa_g.write_sent < fa_g.write_done + FA_AHEAD) {
				uint32_t index = fa_g.write_sent % 4, have = fa_ring_count();

				/* Not ahead of the writer while it is writing: a period's wait for what is on its way beats a gap. */
				if (have >= FA_PERIOD_FRAMES || fa_g.write_sent < fa_g.write_done + 1) {
					uint32_t taken = fa_fill_period(index, &peak);
					uint32_t bytes = fuxi_ar_write((uint8_t *)(fa_m + 2), AU_GL_GPR_BUFFER, 1, AU_GL_WRITE_TOKEN + (fa_g.write_sent & 0xffffu),
					    (FA_BUFFER + index * FA_PERIOD_BYTES) | FA_BUFFER_SID << 32, fa_g.map_handle, FA_PERIOD_BYTES);

					frames += taken;
					short_periods += taken < FA_PERIOD_FRAMES;
					idle = taken == 0 ? idle + 1 : 0;
					if (bytes == 0 || !au_gl_data_send(&fa_g, fa_m, bytes)) {
						printf("FUXI AUDIO: a buffer could not go, its buffers %u\n", fa_g.gpr_riid_n);
						idle = FA_IDLE_PERIODS;
					}
				}
			}
			if (!fa_poll(1)) {
				return;
			}
			fa_mic_tick();
			if (fa_g.write_sent - sent_at_last >= 250) {
				sent_at_last = fa_g.write_sent;
				fa_amp_watch();
				if (last_said != fa_g.write_sent) {
					printf("FUXI AUDIO: playing: %u periods sent, %u played, %u bad, %u s of samples, peak %u, %u periods short, %u frames dropped\n", fa_g.write_sent, fa_g.write_done, fa_g.write_bad,
					    (uint32_t)(frames / 48000), peak, short_periods, fa_dropped);
					last_said = fa_g.write_sent;
					peak = 0;
				}
			}
		}
		fa_amp_down();
		printf("FUXI AUDIO: quiet: %u periods sent, %u played, %u bad, %u s of samples, %u frames dropped\n", fa_g.write_sent, fa_g.write_done, fa_g.write_bad, (uint32_t)(frames / 48000), fa_dropped);
		fa_graph_down();
	}
}

/* fuxiaudio=1: a second of zeros, through the whole list. */
static void
fa_self_test(void)
{
	uint32_t i;

	if (fa_graph_up() == 0 && fa_g.map_handle != 0) {
		for (i = 0; i < 4000 && (fa_g.write_sent < FA_PERIODS || fa_g.write_done < FA_PERIODS); i++) {
			if (fa_g.write_sent < FA_PERIODS && fa_g.write_sent < fa_g.write_done + 2) {
				uint32_t bytes = fuxi_ar_write((uint8_t *)(fa_m + 2), AU_GL_GPR_BUFFER, 1, AU_GL_WRITE_TOKEN + fa_g.write_sent,
				    (FA_BUFFER + (fa_g.write_sent % 4) * FA_PERIOD_BYTES) | FA_BUFFER_SID << 32, fa_g.map_handle, FA_PERIOD_BYTES);

				if (bytes != 0) {
					(void)au_gl_data_send(&fa_g, fa_m, bytes);
				}
			}
			if (!fa_poll(1)) {
				break;
			}
		}
		printf("FUXI AUDIO: buffers sent %u played %u, first at %u ms, last at %u ms, last status %#x, bad %u\n", fa_g.write_sent, fa_g.write_done,
		    fa_g.write_first_ms, fa_g.write_last_ms, fa_g.write_status, fa_g.write_bad);
		if (fa_g.write_done > 1) {
			printf("FUXI AUDIO: %u us a buffer of 20 ms\n", (fa_g.write_last_ms - fa_g.write_first_ms) * 1000u / (fa_g.write_done - 1u));
		}
	}
	fa_graph_down();
}

static void
fa_thread(void *arg, wait_result_t result)
{
#pragma unused(result)
	if (fa_bring_up()) {
		if ((uintptr_t)arg >= 2) {
			if ((uintptr_t)arg == 4) {
				fa_mic_wanted = true;
				fa_mic_test_until = fa_g.ms + 8000;
			}
			fa_stream();
		} else {
			fa_self_test();
		}
	}
	(void)thread_terminate(current_thread());
	thread_block(THREAD_CONTINUE_NULL);
}

void
fuxi_audio_dsp_init(void)
{
	int on = 0;
	thread_t thread;

	if (!PE_parse_boot_argn("fuxiaudio", &on, sizeof(on)) || on <= 0) {
		return;
	}
	if (on >= 2) {
		int major = cdevsw_add(-1, &fa_cdevsw), amp = 0;

		if (PE_parse_boot_argn("fuxiaudioamp", &amp, sizeof(amp)) && amp == 1) {
			fa_se0 = ml_io_map((vm_offset_t)FA_SE0, 0x4000);
			fa_gcc = ml_io_map((vm_offset_t)FA_GCC, 0x80000);
			fa_tlmm = ml_io_map((vm_offset_t)FA_TLMM, 0x104000);
			fa_amp_wanted = fa_se0 != 0 && fa_gcc != 0 && fa_tlmm != 0;
			printf("FUXI AUDIO: the amplifier was asked for: %d\n", fa_amp_wanted);
		}

		if (major < 0 || devfs_make_node(makedev(major, 0), DEVFS_CHAR, UID_ROOT, GID_WHEEL, 0666, "fuxiaudio") == NULL) {
			printf("FUXI AUDIO: no /dev/fuxiaudio\n");
			return;
		}
		if (on >= 3) {
			bool mapped = true;

			for (unsigned i = 0; i < FA_MIC_WINDOWS; i++) {
				fa_mic_va[i] = ml_io_map((vm_offset_t)fa_mic_windows[i].pa, fa_mic_windows[i].size);
				mapped = mapped && fa_mic_va[i] != 0;
			}
			fa_mic_buffer = ml_io_map((vm_offset_t)FA_MIC_BUFFER, FA_MIC_SLOTS * FA_MIC_PERIOD);
			fa_mic_on = mapped && fa_mic_buffer != 0 && devfs_make_node(makedev(major, 1), DEVFS_CHAR, UID_ROOT, GID_WHEEL, 0666, "fuxiaudiomic") != NULL;
			printf("FUXI AUDIO: the mic can be asked for: %d\n", fa_mic_on);
		}
	}
	if (kernel_thread_start(fa_thread, (void *)(uintptr_t)on, &thread) == KERN_SUCCESS) {
		thread_deallocate(thread);
	} else {
		printf("FUXI AUDIO: kernel_thread_start failed\n");
	}
}
#endif /* FUXI */
