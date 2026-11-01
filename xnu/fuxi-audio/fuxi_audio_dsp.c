/* Fuxi audio lane, P-AU3 and P-AU4 in XNU: the ADSP the loader left running (boot/ios-remote.sh ADSP=1), spoken to from a kernel thread.
 * boot-arg fuxiaudio=1: GLINK over SMEM, polled, as shim/fuxi-probe/au_glink.h; the APM's state; LPASS's votes; the secondary TDM endpoint's graph and a stream into
 * it, started on a second of zeros, and taken down again. What a-play2 did before iOS, here beside iOS. No pin is muxed, no amplifier reached, nothing is heard.
 * The loader has put apps_smmu's streams 0x1001/0x80 and 0x1061 into context bank 4 (poke 2 and 18); this does not touch the SMMU. The buffer is outside the bank XNU owns. */
#ifdef FUXI
#include <stdint.h>
#include <stdbool.h>
#include <sys/param.h>
#include <sys/systm.h>
#include <string.h>
#include <kern/clock.h>
#include <kern/thread.h>
#include <kern/sched_prim.h>
#include <machine/machine_routines.h>
#include <pexpert/pexpert.h>
#include "fuxi_audioreach.h"

extern vm_offset_t ml_io_map(vm_offset_t, vm_size_t);
void fuxi_audio_dsp_init(void);

#define FA_SMEM         0x81d00000ull   /* smem_region */
#define FA_SMEM_SIZE    0x00200000u
#define FA_IPCC         0x00408000ull
#define FA_IPCC_SIZE    0x00001000u
#define FA_IPCC_GLINK   ((3u << 16) | 0u)    /* client LPASS, signal GLINK_QMP */
#define FA_BUFFER       0xd0000000ull
#define FA_BUFFER_BYTES (4u * 7680u)
#define FA_BUFFER_SID   1ull            /* 0x1001 & 0xf, as Linux q6apm_dai */
#define FA_PERIOD_BYTES 7680u           /* 20 ms of 48 kHz, 2 channels of 32 bits */
#define FA_PERIODS      50u

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
		if (fa_smem_part(0, 2, &p)) {
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

static void
fa_thread(void *arg, wait_result_t result)
{
#pragma unused(arg, result)
	static const FuxiArFormat format = {48000, 32, 2};
	static const FuxiArTdm tdm = {.intf = 1, .slot_mask = 5, .slots = 4, .slot_width = 32, .sync_mode = 1, .invert_sync = 0, .sync_data_delay = 1, .lane = 1, .clock_attribute = 4};
	struct fa_part p;
	uint32_t desc_size = 0, tx_size = 0, rx_size = 0, bad = 0;
	uint64_t desc, tx, rx = 0;
	unsigned i;

	fa_start = mach_absolute_time();
	fa_smem = ml_io_map((vm_offset_t)FA_SMEM, FA_SMEM_SIZE);
	fa_ipcc = ml_io_map((vm_offset_t)FA_IPCC, FA_IPCC_SIZE);
	fa_buffer = ml_io_map((vm_offset_t)FA_BUFFER, FA_BUFFER_BYTES);
	if (fa_smem == 0 || fa_ipcc == 0 || fa_buffer == 0) {
		printf("FUXI AUDIO: no mapping\n");
		goto out;
	}
	if (!fa_smem_part(0, 2, &p)) {
		printf("FUXI AUDIO: no AP-ADSP partition\n");
		goto out;
	}
	uint32_t bits = fa_ready_bits(&p);
	printf("FUXI AUDIO: DSP ready bits %#x\n", bits);
	if (bits != 6) {
		printf("FUXI AUDIO: not ready: was the loader told ADSP=1?\n");
		goto out;
	}
	desc = fa_smem_item(&p, 478, &desc_size);
	tx = fa_smem_item(&p, 479, &tx_size);
	if (desc == 0 || desc_size != 32 || tx == 0 || tx_size < 1024 || tx_size > 0x100000 || (tx_size & 7) != 0) {
		printf("FUXI AUDIO: GLINK's items: descriptor %#llx %u, tx %#llx %u\n", (unsigned long long)desc, desc_size, (unsigned long long)tx, tx_size);
		goto out;
	}
	fa_g.desc = desc;
	fa_g.tx_fifo = tx;
	fa_g.tx_len = tx_size;
	fa_g.options = AU_GL_ANSWER | AU_GL_GPR | AU_GL_PRM | AU_GL_GRAPH | AU_GL_PLAY;
	fa_g.log = fa_log;
	fa_g.log_most = 8;
	fa_g.graph_steps = 15;
	if (!au_gl_start(&fa_g)) {
		printf("FUXI AUDIO: GLINK did not start\n");
		goto out;
	}
	for (i = 0; i < 30000 && rx == 0; i++) {
		rx = fa_smem_item(&p, 480, &rx_size);
		if (rx == 0) {
			fa_sleep_ms(1);
			(void)fa_smem_part(0, 2, &p);
		}
	}
	if (rx == 0 || rx_size < 1024 || rx_size > 0x100000 || (rx_size & 7) != 0) {
		printf("FUXI AUDIO: the DSP's FIFO: %#llx %u\n", (unsigned long long)rx, rx_size);
		goto out;
	}
	fa_g.rx_fifo = rx;
	fa_g.rx_len = rx_size;
	if (!fa_poll(2000)) {
		goto out;
	}
	printf("FUXI AUDIO: GLINK up: messages %u, channels %u, adsp_apps %u, its buffers %u\n", fa_g.rx_msgs, fa_g.opened, fa_g.gpr_lcid, fa_g.gpr_riid_n);
	(void)au_gl_gpr_ask(&fa_g);
	if (!fa_poll(500)) {
		goto out;
	}
	printf("FUXI AUDIO: APM ready %u after %u answers\n", fa_g.gpr_ready, fa_g.gpr_heard);
	if (!fa_g.gpr_ready) {
		goto out;
	}
	for (i = 0; i < 2; i++) {
		(void)au_gl_prm_step(&fa_g);
		if (!fa_poll(100)) {
			goto out;
		}
		printf("FUXI AUDIO: vote %u: status %#x\n", i, fa_g.prm_status[i]);
	}
	for (i = 0; i < FA_BUFFER_BYTES / 4; i++) {
		((volatile uint32_t *)fa_buffer)[i] = 0;
	}
	__asm__ volatile ("dsb osh" ::: "memory");
	FA_STEP("open device", fuxi_ar_open_speaker_tdm);
	FA_STEP("configure device", fuxi_ar_config_speaker_tdm, &format, &tdm);
	FA_STEP("open stream", fuxi_ar_open_playback_stream_tdm);
	FA_STEP("configure stream", fuxi_ar_config_playback_stream, &format);
	FA_STEP("configure its input", fuxi_ar_config_playback_shmem, &format);
	FA_STEP("map the buffers", fuxi_ar_map, FA_BUFFER | FA_BUFFER_SID << 32, FA_BUFFER_BYTES);
	FA_STEP("prepare device", fuxi_ar_graph_command, FUXI_AR_OP_GRAPH_PREPARE, FUXI_AR_SG_SPEAKER);
	FA_STEP("prepare stream", fuxi_ar_graph_command, FUXI_AR_OP_GRAPH_PREPARE, FUXI_AR_SG_PLAY);
	FA_STEP("start device", fuxi_ar_graph_command, FUXI_AR_OP_GRAPH_START, FUXI_AR_SG_SPEAKER);
	FA_STEP("start stream", fuxi_ar_graph_command, FUXI_AR_OP_GRAPH_START, FUXI_AR_SG_PLAY);
	if (bad == 0 && fa_g.map_handle != 0) {
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
	FA_STEP("stop stream", fuxi_ar_graph_command, FUXI_AR_OP_GRAPH_STOP, FUXI_AR_SG_PLAY);
	FA_STEP("stop device", fuxi_ar_graph_command, FUXI_AR_OP_GRAPH_STOP, FUXI_AR_SG_SPEAKER);
	FA_STEP("close stream", fuxi_ar_graph_command, FUXI_AR_OP_GRAPH_CLOSE, FUXI_AR_SG_PLAY);
	FA_STEP("close device", fuxi_ar_graph_command, FUXI_AR_OP_GRAPH_CLOSE, FUXI_AR_SG_SPEAKER);
	FA_STEP("unmap", fuxi_ar_unmap, fa_g.map_handle);
	for (i = 2; i < 4; i++) {
		(void)au_gl_prm_step(&fa_g);
		if (!fa_poll(100)) {
			goto out;
		}
		printf("FUXI AUDIO: vote %u: status %#x\n", i, fa_g.prm_status[i]);
	}
	printf("FUXI AUDIO: done, %u steps failed; rx %u tx %u full %u\n", bad, fa_g.rx_msgs, fa_g.tx_msgs, fa_g.tx_full);
out:
	thread_terminate(current_thread());
}

void
fuxi_audio_dsp_init(void)
{
	int on = 0;
	thread_t thread;

	if (!PE_parse_boot_argn("fuxiaudio", &on, sizeof(on)) || on <= 0) {
		return;
	}
	if (kernel_thread_start(fa_thread, NULL, &thread) == KERN_SUCCESS) {
		thread_deallocate(thread);
	} else {
		printf("FUXI AUDIO: kernel_thread_start failed\n");
	}
}
#endif /* FUXI */
