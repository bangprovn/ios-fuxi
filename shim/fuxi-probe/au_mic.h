/* Fuxi audio lane: the built-in microphone's way as far as a codec that answers, with its bias and ADC1 on and read back. No capture graph. Plain C so that
 * tools/test-fuxi-audio-mic.py runs it against a fake LPASS, a fake SoundWire master and a fake WCD938x under the sanitizers.
 *
 * Proof steps as research_notes/mic-research.md 3, ending at the first that fails: 1 the DSP's clock, 2 LPASS reachable, 3 macros clocked and the master's version,
 * 4 frames, 5 the codec enumerated with its id, 6 its registers, 7 init and bias written and read back. Whatever it reached, au_mic_down() undoes it, and audio.c then
 * lets go of the clock and the votes. All of it is done while the votes are held; reads and writes of LPASS come by AU_MIC_RD and AU_MIC_WR, the safe ones.
 *
 * Limits the test holds to this source: au_mic_allowed[] is all a codec write can reach, so no efuse (0x3460, 0x3461, 0x3490-0x34c6), no HPH, and of the biases MICB1 alone,
 * with VOUT at most 0x23. L15B is only voted on, and only if cmd-db says 0x40a00. BOB1 is not voted. Only the TX master, 0x6d30000. Of the TLMM, gpio108 alone.
 *
 * Sequence ported from UniXNU-sheng package/AudioProbe.cpp 08cde20edc32 (BSD-2-Clause-Patent), with fuxi's AMIC1, MICB1, ADC1 for its AMIC3, MICB3, ADC2. Numbers checked against
 * Linux 6.12 wcd938x.c, qcom.c, lpass-tx-macro.c, lpass-va-macro.c as facts; pad and bias values from boot-logs/a-ios6-probe/live.dtb.
 * Define AU_MIC_RD(pa, faulted), AU_MIC_WR (nonzero on a fault), AU_MIC_DELAY_US, AU_MIC_CMDDB(name), AU_MIC_RPMH(addr, data) (nonzero on failure). au_glink.h and fuxi_audioreach.h come first.
 */
#ifndef AU_MIC_H
#define AU_MIC_H

#define AU_MIC_MAGIC    0xa0d10007u
#define AU_MIC_VA       0x06d44000ull
#define AU_MIC_TX       0x06ae0000ull
#define AU_MIC_SWR      0x06d30000ull
#define AU_MIC_LPI      0x06e80000ull
#define AU_MIC_SLEW     0x0725a000ull
#define AU_MIC_RESET    0x0f16c000ull  /* TLMM gpio108: CTL, then IO at +4. High runs the codec (live DT) */
#define AU_MIC_L15B     0x40a00u       /* ldob15 in cmd-db: research on the WLAN lane, and held here */
#define AU_MIC_VOUT_MAX 0x23u          /* 2.75 V, (mV - 1000) / 50: Android's own, live DT */
#define AU_MIC_MICB1    0x3022u

/* The list on the PRM: three tries at the clock, as sheng's (it saw the first refused), and letting go. */
#define AU_MIC_STEPS 4u
#define AU_MIC_TRIES 3u
#define AU_MIC_CLOCK 0x30cu            /* TX core: the PRM's number, live DT and q6prm.h. Upstream's VA macro runs on it too */

enum { AU_MIC_WHY_NONE = 0, AU_MIC_WHY_ABORT = 1, AU_MIC_WHY_VOTES = 2, AU_MIC_WHY_REFUSED = 3, AU_MIC_WHY_NO_ANSWER = 4, AU_MIC_WHY_CMDDB = 5, AU_MIC_WHY_RPMH = 6,
       AU_MIC_WHY_RESET = 7, AU_MIC_WHY_PADS = 8, AU_MIC_WHY_GATES = 9, AU_MIC_WHY_VERSION = 10, AU_MIC_WHY_NO_FRAMES = 11, AU_MIC_WHY_NO_DEVICE = 12, AU_MIC_WHY_NOT_THE_CODEC = 13,
       AU_MIC_WHY_READ = 14, AU_MIC_WHY_CHIP = 15, AU_MIC_WHY_WRITE = 16, AU_MIC_WHY_READBACK = 17, AU_MIC_WHY_BUS = 18 };
enum { AU_MIC_DID_RESET = 1u, AU_MIC_DID_PADS = 2u, AU_MIC_DID_GATES = 4u, AU_MIC_DID_MASTER = 8u, AU_MIC_DID_BIAS = 16u, AU_MIC_DID_TX = 32u };

/* The report, 128 words: tools/fuxi-audio-probe.py decode_mic() reads these, and the test holds the two together. */
#define AU_MIC_R_PASSED 1
#define AU_MIC_R_STEP   2
#define AU_MIC_R_WHY    3
#define AU_MIC_R_FAULTS 4
#define AU_MIC_R_FAULT_PA 5
#define AU_MIC_R_DID    6
#define AU_MIC_R_DOWN_FAULTS 7
#define AU_MIC_R_VOTE   8   /* 2 words */
#define AU_MIC_R_TRIES  10  /* 3 words */
#define AU_MIC_R_TAKEN  13
#define AU_MIC_R_CORE   14  /* VA CORE_ID 0 to 3 */
#define AU_MIC_R_CMDDB  19
#define AU_MIC_R_RPMH   20  /* 3 words */
#define AU_MIC_R_PAD    23  /* gpio108 found: CTL, IO; set: CTL, IO */
#define AU_MIC_R_LPI    27  /* found 4, then set 4 */
#define AU_MIC_R_SLEW   35  /* found, set */
#define AU_MIC_R_GATES  37  /* VA +0, +4, +8, +0x80, TX +0, +4, +8, +0x90 */
#define AU_MIC_R_VERSION 45
#define AU_MIC_R_LINK   46  /* +0x5064, ms, +0x5000, +0x100, +0x104c */
#define AU_MIC_R_SLAVES 51  /* +0x1090, ms, device number, enumerator's two words */
#define AU_MIC_R_SCP    56  /* 6 words */
#define AU_MIC_R_READ_FAILS 62
#define AU_MIC_R_CHIP   63  /* 0x3401 to 0x3404 */
#define AU_MIC_R_RESETS 67  /* 0x3022, 0x3029, 0x3103, 0x34b0 */
#define AU_MIC_R_DIFFER 71  /* of those, a bit for each not what the driver's table says */
#define AU_MIC_R_BAD    72  /* first readback wrong: register, want, got */
#define AU_MIC_R_BUS    75  /* +0x5000 after the writes */
#define AU_MIC_R_BACK_N 76
#define AU_MIC_R_BACK   77  /* 25 words, au_mic_want[] */
#define AU_MIC_R_DOWN   104 /* MICB1 after off, gpio108 CTL, IO, gpio0's pad, TX +8 */
#define AU_MIC_R_PORTS  109 /* with a capture: codec 0x128, 0x120; the master's port CTRL, CTRL2, HCTRL; TX +0x100, +0x104, +0x404, +0x400 */

struct au_mic {
	uint32_t *r;
	uint32_t did, step, faults, read_fails, dev;
	bool votes_held, clock_held, going_down, over, asked;
	bool capture, held;    /* capture: the list goes on with a graph, and a stage that passes stays up for it; held: it is up now */
	uint32_t port, lane, mic;    /* master's data port, its lane, the TX macro's SWR_MIC number + 1 */
	uint32_t asked_ms;
	uint8_t id;
	uint32_t found_pad[2], found_lpi[4], found_slew, found_va[3];
};

static const uint8_t au_mic_lpi_pins[4] = {0, 1, 2, 14};    /* clock, data 0 to 2: tx_swr_* of live DT */

/* Everything a codec write can reach: wcd938x_io_init()'s capture lines, MICB1, and ADC1. */
static const uint16_t au_mic_allowed[] = {0x3001, 0x300e, 0x300f, 0x3022, 0x3029, 0x306b, 0x306e, 0x3071, 0x3074, 0x308f, 0x3103, 0x312a, 0x31df, 0x31e1,
					  0x3408, 0x3409, 0x3410, 0x3417, 0x3457, 0x345b, 0x34d0, 0x34d1, 0x34d2, 0x34d3, 0x34d4,
					  /* Its first data port, in both banks: capture only. */
					  0x102, 0x103, 0x105, 0x120, 0x122, 0x123, 0x124, 0x125, 0x126, 0x128, 0x130, 0x132, 0x133, 0x134, 0x135, 0x136, 0x138};

struct au_mic_write { uint16_t reg; uint8_t mask, value; uint16_t wait_us; };
static const struct au_mic_write au_mic_writes[] = {
	/* wcd938x_io_init(), with the hardware's waits; its HPH lines are left out, as sheng's. */
	{0x3103, 0x0e, 0x0e, 0}, {0x3103, 0x80, 0x80, 1000}, {0x3103, 0x40, 0x40, 1000}, {0x312a, 0x10, 0x00, 0}, {0x3029, 0xf0, 0x80, 0},
	{0x3001, 0x80, 0x80, 0}, {0x3001, 0x40, 0x40, 10000}, {0x3001, 0x40, 0x00, 0}, {0x345b, 0x02, 0x02, 0}, {0x31e1, 0xff, 0x14, 0}, {0x31df, 0x1f, 0x08, 0},
	{0x34d0, 0xff, 0x55, 0}, {0x34d1, 0xff, 0x44, 0}, {0x34d2, 0xff, 0x11, 0}, {0x34d3, 0xff, 0x00, 0}, {0x34d4, 0xff, 0x00, 0},
	{0x306b, 0xe0, 0xe0, 0}, {0x306e, 0xe0, 0xe0, 0}, {0x3071, 0xe0, 0xe0, 0}, {0x3074, 0xe0, 0xe0, 0}, {0x308f, 0x01, 0x00, 0},
	/* MIC BIAS1 at 2.75 V and on, the ADC clocks; ADC1's request in normal mode, its filter let go, gain 8 as Android's amic1. */
	{AU_MIC_MICB1, 0x3f, AU_MIC_VOUT_MAX, 0}, {0x3409, 0xf0, 0xf0, 0}, {0x3408, 0x10, 0x10, 0}, {0x3417, 0x01, 0x01, 0}, {AU_MIC_MICB1, 0xc0, 0x40, 0},
	{0x3408, 0x18, 0x18, 0}, {0x3457, 0x03, 0x02, 0}, {0x300f, 0x40, 0x40, 0}, {0x3410, 0x0f, 0x03, 0}, {0x3409, 0x10, 0x10, 0}, {0x300f, 0x40, 0x00, 0}, {0x300e, 0x1f, 0x08, 0},
};
struct au_mic_check { uint16_t reg; uint8_t mask, want; };
static const struct au_mic_check au_mic_want[25] = {
	{0x3103, 0xce, 0xce}, {0x312a, 0x10, 0x00}, {0x3029, 0xf0, 0x80}, {0x3001, 0xc0, 0x80}, {0x345b, 0x02, 0x02}, {0x31e1, 0xff, 0x14}, {0x31df, 0x1f, 0x08},
	{0x34d0, 0xff, 0x55}, {0x34d1, 0xff, 0x44}, {0x34d2, 0xff, 0x11}, {0x34d3, 0xff, 0x00}, {0x34d4, 0xff, 0x00},
	{0x306b, 0xe0, 0xe0}, {0x306e, 0xe0, 0xe0}, {0x3071, 0xe0, 0xe0}, {0x3074, 0xe0, 0xe0}, {0x308f, 0x01, 0x00},
	{AU_MIC_MICB1, 0xff, 0x40 | AU_MIC_VOUT_MAX}, {0x3409, 0xf0, 0xf0}, {0x3408, 0x18, 0x18}, {0x3417, 0x01, 0x01}, {0x3457, 0x03, 0x02}, {0x300f, 0x40, 0x00}, {0x3410, 0x0f, 0x03}, {0x300e, 0x1f, 0x08},
};

static inline void au_mic_begin(struct au_mic *m, uint32_t *r)
{
	for (unsigned i = 0; i < sizeof(*m); i++) ((volatile uint8_t *)m)[i] = 0;
	for (unsigned i = 0; i < 128; i++) r[i] = 0;
	m->r = r;
	r[0] = AU_MIC_MAGIC;
	for (unsigned i = 0; i < AU_MIC_TRIES; i++) r[AU_MIC_R_TRIES + i] = 0xfffffffeu;    /* not tried */
}

static inline bool au_mic_fail(struct au_mic *m, uint32_t step, uint32_t why)
{
	if (m->r[AU_MIC_R_STEP] == 0u) { m->r[AU_MIC_R_STEP] = step; m->r[AU_MIC_R_WHY] = why; }
	return false;
}

static inline void au_mic_fault(struct au_mic *m, uint64_t pa)
{
	if (m->going_down) { m->r[AU_MIC_R_DOWN_FAULTS]++; return; }
	if (m->faults++ == 0u) m->r[AU_MIC_R_FAULT_PA] = (uint32_t)pa;
	m->r[AU_MIC_R_FAULTS] = m->faults;
}

/* Reads and writes of LPASS and the pad. After a fault nothing more is written, until the way down, which tries everything it can. */
static inline uint32_t au_mic_rd(struct au_mic *m, uint64_t pa, bool *ok)
{
	uint32_t f = 0, v = AU_MIC_RD(pa, &f);

	if (f != 0u) { au_mic_fault(m, pa); if (ok != 0) *ok = false; return 0; }
	if (ok != 0) *ok = true;
	return v;
}

static inline void au_mic_wr(struct au_mic *m, uint64_t pa, uint32_t v)
{
	if (m->faults != 0u && !m->going_down) return;
	if (AU_MIC_WR(pa, v)) au_mic_fault(m, pa);
}

/* Read, change, write: a value that faulted is not written back, and after a fault not even read: what comes next may be behind what failed. */
static inline void au_mic_set(struct au_mic *m, uint64_t pa, uint32_t clear, uint32_t set)
{
	bool ok;
	uint32_t v;

	if (m->faults != 0u && !m->going_down) return;
	v = au_mic_rd(m, pa, &ok);
	if (ok) au_mic_wr(m, pa, (v & ~clear) | set);
}

/* ---- SoundWire's command FIFO, v2.0 layout: qcom.c, as sheng's swrRead and swrWrite ---- */
static inline uint32_t au_mic_pack(uint8_t data, uint32_t dev, uint32_t id, uint32_t reg) { return reg | id << 16 | dev << 20 | (uint32_t)data << 24; }
static inline uint32_t au_mic_next_id(struct au_mic *m) { m->id = m->id < 14u ? m->id + 1u : 0u; return m->id; }

static inline bool au_mic_codec_read(struct au_mic *m, uint32_t reg, uint32_t *value)
{
	uint32_t id = au_mic_next_id(m);

	au_mic_wr(m, AU_MIC_SWR + 0x5024u, au_mic_pack(1, m->dev, id, reg));
	for (unsigned i = 0; i < 200u; i++) {
		bool ok;
		uint32_t st = au_mic_rd(m, AU_MIC_SWR + 0x5050u, &ok);

		if (!ok) break;
		if (((st >> 16) & 0x1fu) != 0u) {
			uint32_t v = au_mic_rd(m, AU_MIC_SWR + 0x5040u, &ok);

			if (ok && ((v >> 8) & 0xfu) == id) { *value = v & 0xffu; return true; }
			au_mic_wr(m, AU_MIC_SWR + 0x308u, 1u);    /* a stale answer: flushed, and given up on */
			break;
		}
		AU_MIC_DELAY_US(50);
	}
	m->r[AU_MIC_R_READ_FAILS] = ++m->read_fails;
	return false;
}

/* The one way a value reaches the codec. */
static inline bool au_mic_codec_write(struct au_mic *m, uint32_t reg, uint32_t value)
{
	bool allowed = false;

	for (unsigned i = 0; i < sizeof(au_mic_allowed) / sizeof(au_mic_allowed[0]); i++) allowed = allowed || au_mic_allowed[i] == reg;
	if (!allowed || value > 0xffu || (reg == AU_MIC_MICB1 && (value & 0x3fu) > AU_MIC_VOUT_MAX)) return false;
	for (unsigned i = 0; i < 100u; i++) {
		bool ok;
		uint32_t st = au_mic_rd(m, AU_MIC_SWR + 0x5050u, &ok);

		if (!ok) return false;
		if (((st >> 8) & 0x1fu) < 8u) break;
		AU_MIC_DELAY_US(50);
	}
	au_mic_wr(m, AU_MIC_SWR + 0x5020u, au_mic_pack((uint8_t)value, m->dev, au_mic_next_id(m), reg));
	return m->faults == 0u || m->going_down;
}

static inline bool au_mic_codec_update(struct au_mic *m, uint32_t reg, uint32_t mask, uint32_t value)
{
	uint32_t v = 0;

	return au_mic_codec_read(m, reg, &v) && au_mic_codec_write(m, reg, (v & ~mask) | (value & mask));
}

/* ---- step 2: LPASS answers, with the votes and the clock held: the VA macro alone, as Linux's probe reads it. The TX macro comes behind the VA's frame sync and the master behind both gates, as Linux and sheng: a gated read may be an SError, which resets ---- */
static inline bool au_mic_step2(struct au_mic *m)
{
	for (unsigned i = 0; i < 4u; i++) m->r[AU_MIC_R_CORE + i] = au_mic_rd(m, AU_MIC_VA + 0xc0u + 4u * i, 0);
	return m->faults == 0u ? true : au_mic_fail(m, 2, AU_MIC_WHY_ABORT);
}

/* ---- step 3: the rail, the codec out of reset, the pads, the macros' clocks ---- */
static inline bool au_mic_rail(struct au_mic *m)
{
	uint32_t address = AU_MIC_CMDDB("ldob15");

	m->r[AU_MIC_R_CMDDB] = address;
	if (address != AU_MIC_L15B) return au_mic_fail(m, 3, AU_MIC_WHY_CMDDB);
	/* On, and only on: high power mode, 1.8 V, enable (qcom-rpmh-regulator.c, sheng's order). Wi-Fi's IO is on it too, and a vote is one value, so off is never sent. */
	m->r[AU_MIC_R_RPMH] = AU_MIC_RPMH(AU_MIC_L15B + 8u, 7u);
	if (m->r[AU_MIC_R_RPMH] != 0u) return au_mic_fail(m, 3, AU_MIC_WHY_RPMH);
	m->r[AU_MIC_R_RPMH + 1] = AU_MIC_RPMH(AU_MIC_L15B, 1800u);
	if (m->r[AU_MIC_R_RPMH + 1] != 0u) return au_mic_fail(m, 3, AU_MIC_WHY_RPMH);
	m->r[AU_MIC_R_RPMH + 2] = AU_MIC_RPMH(AU_MIC_L15B + 4u, 1u);
	if (m->r[AU_MIC_R_RPMH + 2] != 0u) return au_mic_fail(m, 3, AU_MIC_WHY_RPMH);
	AU_MIC_DELAY_US(1000);
	return true;
}

/* Output, 16 mA, as live DT; low 20 ms, then high 20 ms, as sheng's (Linux waits 20 us). TLMM's eGPIO bits as au_pin() in audio.c. */
static inline bool au_mic_reset_release(struct au_mic *m)
{
	uint32_t was = au_mic_rd(m, AU_MIC_RESET, 0), io = au_mic_rd(m, AU_MIC_RESET + 4u, 0);
	uint32_t ctl = 7u << 6 | 1u << 9 | (was & 1u << 11) | ((was & 1u << 11) != 0u ? 1u << 12 : 0u);

	m->r[AU_MIC_R_PAD] = was; m->r[AU_MIC_R_PAD + 1] = io;
	if (m->faults != 0u) return au_mic_fail(m, 3, AU_MIC_WHY_ABORT);
	m->found_pad[0] = was; m->found_pad[1] = io;
	m->did |= AU_MIC_DID_RESET;
	au_mic_wr(m, AU_MIC_RESET + 4u, 0);
	au_mic_wr(m, AU_MIC_RESET, ctl);
	AU_MIC_DELAY_US(20000);
	au_mic_wr(m, AU_MIC_RESET + 4u, 2u);
	AU_MIC_DELAY_US(20000);
	m->r[AU_MIC_R_PAD + 2] = au_mic_rd(m, AU_MIC_RESET, 0);
	m->r[AU_MIC_R_PAD + 3] = au_mic_rd(m, AU_MIC_RESET + 4u, 0);
	if (m->faults != 0u) return au_mic_fail(m, 3, AU_MIC_WHY_ABORT);
	if ((m->r[AU_MIC_R_PAD + 2] & 0x3ffu) != (ctl & 0x3ffu) || (m->r[AU_MIC_R_PAD + 3] & 2u) == 0u) return au_mic_fail(m, 3, AU_MIC_WHY_RESET);
	return true;
}

/* Func 1, 4 mA (live DT), the clock with no pull and the data with a keeper; slew 1. pinctrl-lpass-lpi.h's fields, sm8550's slew shifts 0, 2, 4, 6. */
static inline bool au_mic_pads(struct au_mic *m)
{
	uint32_t slew;

	for (unsigned i = 0; i < 4u; i++) m->r[AU_MIC_R_LPI + i] = m->found_lpi[i] = au_mic_rd(m, AU_MIC_LPI + 0x1000u * au_mic_lpi_pins[i], 0);
	m->r[AU_MIC_R_SLEW] = m->found_slew = slew = au_mic_rd(m, AU_MIC_SLEW, 0);
	if (m->faults != 0u) return au_mic_fail(m, 3, AU_MIC_WHY_ABORT);
	m->did |= AU_MIC_DID_PADS;
	for (unsigned i = 0; i < 4u; i++) {
		au_mic_wr(m, AU_MIC_LPI + 0x1000u * au_mic_lpi_pins[i], 1u << 2 | 1u << 6 | (i != 0u ? 2u : 0u));
		slew = (slew & ~(3u << (2u * i))) | 1u << (2u * i);
	}
	au_mic_wr(m, AU_MIC_SLEW, slew);
	for (unsigned i = 0; i < 4u; i++) {
		m->r[AU_MIC_R_LPI + 4 + i] = au_mic_rd(m, AU_MIC_LPI + 0x1000u * au_mic_lpi_pins[i], 0);
		if ((m->r[AU_MIC_R_LPI + 4 + i] & 0x3ffu) != (1u << 2 | 1u << 6 | (i != 0u ? 2u : 0u))) au_mic_fail(m, 3, AU_MIC_WHY_PADS);
	}
	m->r[AU_MIC_R_SLEW + 1] = au_mic_rd(m, AU_MIC_SLEW, 0);
	if (m->faults != 0u) { m->r[AU_MIC_R_STEP] = 0; return au_mic_fail(m, 3, AU_MIC_WHY_ABORT); }
	if ((m->r[AU_MIC_R_SLEW + 1] & 0xffu) != (slew & 0xffu)) au_mic_fail(m, 3, AU_MIC_WHY_PADS);
	return m->r[AU_MIC_R_STEP] == 0u;
}

/* The VA macro's frame sync and its gate on the master, the TX macro's clock and gate. lpass-va-macro.c, lpass-tx-macro.c; sheng found the master takes no write without the VA's. */
static inline bool au_mic_gates(struct au_mic *m)
{
	/* TX +0x90 is only reported: a-mic1 read it 0 with every clock bit up, and nothing below has been shown to need it read back. */
	static const struct { uint64_t pa; uint32_t bits; } back[8] = {
		{AU_MIC_VA, 1u}, {AU_MIC_VA + 4u, 1u}, {AU_MIC_VA + 8u, 1u}, {AU_MIC_VA + 0x80u, 2u},
		{AU_MIC_TX, 1u}, {AU_MIC_TX + 4u, 1u}, {AU_MIC_TX + 8u, 1u}, {AU_MIC_TX + 0x90u, 0u}};

	for (unsigned i = 0; i < 3u; i++) m->found_va[i] = au_mic_rd(m, AU_MIC_VA + 0xd0u + 4u * i, 0);
	if (m->faults != 0u) return au_mic_fail(m, 3, AU_MIC_WHY_ABORT);
	m->did |= AU_MIC_DID_GATES;
	au_mic_set(m, AU_MIC_VA, 0u, 1u);
	au_mic_set(m, AU_MIC_VA + 4u, 0u, 3u);
	au_mic_set(m, AU_MIC_VA + 4u, 2u, 1u);
	au_mic_set(m, AU_MIC_VA + 0x80u, 0u, 2u);
	for (unsigned i = 0; i < 3u; i++) au_mic_wr(m, AU_MIC_VA + 0xd0u + 4u * i, (m->found_va[i] & ~0xeeu) | 0xccu);
	au_mic_set(m, AU_MIC_VA + 8u, 0u, 2u);
	au_mic_set(m, AU_MIC_VA + 8u, 0u, 1u);
	au_mic_set(m, AU_MIC_VA + 8u, 2u, 0u);
	/* The TX macro only behind the VA's gate that held. */
	m->r[AU_MIC_R_GATES + 2] = au_mic_rd(m, AU_MIC_VA + 8u, 0);
	if (m->faults != 0u) return au_mic_fail(m, 3, AU_MIC_WHY_ABORT);
	if ((m->r[AU_MIC_R_GATES + 2] & 1u) == 0u) return au_mic_fail(m, 3, AU_MIC_WHY_GATES);
	m->did |= AU_MIC_DID_TX;
	au_mic_set(m, AU_MIC_TX + 0x90u, 0u, 1u);
	au_mic_set(m, AU_MIC_TX, 0u, 1u);
	au_mic_set(m, AU_MIC_TX + 4u, 0u, 1u);
	au_mic_set(m, AU_MIC_TX + 8u, 0u, 2u);
	au_mic_set(m, AU_MIC_TX + 8u, 0u, 1u);
	au_mic_set(m, AU_MIC_TX + 8u, 2u, 0u);
	for (unsigned i = 0; i < 8u; i++) {
		m->r[AU_MIC_R_GATES + i] = au_mic_rd(m, back[i].pa, 0);
		if (back[i].bits != 0u && (m->r[AU_MIC_R_GATES + i] & back[i].bits) == 0u && m->faults == 0u) au_mic_fail(m, 3, AU_MIC_WHY_GATES);
	}
	if (m->faults != 0u) { m->r[AU_MIC_R_STEP] = 0; return au_mic_fail(m, 3, AU_MIC_WHY_ABORT); }
	return m->r[AU_MIC_R_STEP] == 0u;
}

static inline bool au_mic_step3(struct au_mic *m)
{
	if (!au_mic_rail(m) || !au_mic_reset_release(m) || !au_mic_pads(m) || !au_mic_gates(m)) return false;
	m->r[AU_MIC_R_VERSION] = au_mic_rd(m, AU_MIC_SWR, 0);
	if (m->faults != 0u) return au_mic_fail(m, 3, AU_MIC_WHY_ABORT);
	return (m->r[AU_MIC_R_VERSION] >> 24) == 2u ? true : au_mic_fail(m, 3, AU_MIC_WHY_VERSION);
}

/* ---- step 4: the master started as qcom_swrm_init(): 50 rows, 16 columns, auto enumeration, no-ping limit, CPU execution environment, clock start, 7 retries, enabled, pulse ---- */
static inline bool au_mic_step4(struct au_mic *m)
{
	const uint64_t s = AU_MIC_SWR;
	uint32_t ms;

	m->did |= AU_MIC_DID_MASTER;
	au_mic_wr(m, s + 0x008u, 1u);
	AU_MIC_DELAY_US(1000);
	au_mic_wr(m, s + 0x101cu, 1u << 3 | 7u);
	au_mic_wr(m, s + 0x500u, 1u);
	au_mic_set(m, s + 0x1048u, 0x1fu << 17, 0x1fu << 17);
	au_mic_wr(m, s + 0x018u, 1u);
	au_mic_wr(m, s + 0x5060u, 1u);
	au_mic_wr(m, s + 0x314u, 7u | 1u << 31);
	au_mic_wr(m, s + 0x004u, 1u);
	au_mic_wr(m, s + 0x004u, 2u);
	au_mic_wr(m, s + 0x5008u, 0xffffffffu);
	au_mic_wr(m, s + 0x5004u, 0x1ffffu);
	au_mic_wr(m, s + 0x004u, 3u);
	for (ms = 0; ms < 500u && m->faults == 0u; ms++) {
		m->r[AU_MIC_R_LINK] = au_mic_rd(m, s + 0x5064u, 0);
		if ((m->r[AU_MIC_R_LINK] & 1u) != 0u) break;
		AU_MIC_DELAY_US(1000);
	}
	m->r[AU_MIC_R_LINK + 1] = ms;
	m->r[AU_MIC_R_LINK + 2] = au_mic_rd(m, s + 0x5000u, 0);
	m->r[AU_MIC_R_LINK + 3] = au_mic_rd(m, s + 0x100u, 0);
	m->r[AU_MIC_R_LINK + 4] = au_mic_rd(m, s + 0x104cu, 0);
	if (m->faults != 0u) return au_mic_fail(m, 4, AU_MIC_WHY_ABORT);
	return (m->r[AU_MIC_R_LINK] & 1u) != 0u ? true : au_mic_fail(m, 4, AU_MIC_WHY_NO_FRAMES);
}

/* ---- step 5: the codec attaches and takes a number by itself. Live DT's address 0x0D 0x01170223, so 23 02 17 01 0D 00; the manufacturer and part decide ---- */
static inline bool au_mic_step5(struct au_mic *m)
{
	const uint64_t s = AU_MIC_SWR;
	uint32_t ms, slaves = 0;

	for (ms = 0; ms < 1000u && m->faults == 0u; ms++) {
		slaves = au_mic_rd(m, s + 0x1090u, 0);
		if ((slaves & 0x3fffcu) != 0u) break;
		AU_MIC_DELAY_US(1000);
	}
	m->r[AU_MIC_R_SLAVES] = slaves; m->r[AU_MIC_R_SLAVES + 1] = ms;
	if (m->faults != 0u) return au_mic_fail(m, 5, AU_MIC_WHY_ABORT);
	if ((slaves & 0x3fffcu) == 0u) return au_mic_fail(m, 5, AU_MIC_WHY_NO_DEVICE);
	m->dev = 1;
	while (m->dev < 9u && ((slaves >> (2u * m->dev)) & 3u) == 0u) m->dev++;
	m->r[AU_MIC_R_SLAVES + 2] = m->dev;
	m->r[AU_MIC_R_SLAVES + 3] = au_mic_rd(m, s + 0x530u + 8u * m->dev, 0);
	m->r[AU_MIC_R_SLAVES + 4] = au_mic_rd(m, s + 0x534u + 8u * m->dev, 0);
	for (unsigned i = 0; i < 6u; i++) {
		if (!au_mic_codec_read(m, 0x50u + i, &m->r[AU_MIC_R_SCP + i])) return au_mic_fail(m, 5, m->faults != 0u ? AU_MIC_WHY_ABORT : AU_MIC_WHY_READ);
	}
	if (m->r[AU_MIC_R_SCP + 1] != 0x02u || m->r[AU_MIC_R_SCP + 2] != 0x17u || m->r[AU_MIC_R_SCP + 3] != 0x01u || m->r[AU_MIC_R_SCP + 4] != 0x0du) return au_mic_fail(m, 5, AU_MIC_WHY_NOT_THE_CODEC);
	return true;
}

/* ---- step 6: its registers, before any write. Chip id decides; what the driver's table says of the rest after reset is only reported ---- */
static inline bool au_mic_step6(struct au_mic *m)
{
	static const uint16_t chip[4] = {0x3401, 0x3402, 0x3403, 0x3404};
	static const struct { uint16_t reg; uint8_t want; } table[3] = {{AU_MIC_MICB1, 0x10}, {0x3029, 0x55}, {0x3103, 0x16}};
	uint32_t v, variant;

	for (unsigned i = 0; i < 4u; i++) {
		if (!au_mic_codec_read(m, chip[i], &m->r[AU_MIC_R_CHIP + i])) return au_mic_fail(m, 6, m->faults != 0u ? AU_MIC_WHY_ABORT : AU_MIC_WHY_READ);
	}
	if (m->r[AU_MIC_R_CHIP + 2] != 0x0du || m->r[AU_MIC_R_CHIP + 3] != 0x01u) return au_mic_fail(m, 6, AU_MIC_WHY_CHIP);
	for (unsigned i = 0; i < 3u; i++) {
		if (!au_mic_codec_read(m, table[i].reg, &v)) return au_mic_fail(m, 6, m->faults != 0u ? AU_MIC_WHY_ABORT : AU_MIC_WHY_READ);
		m->r[AU_MIC_R_RESETS + i] = v;
		if (v != table[i].want) m->r[AU_MIC_R_DIFFER] |= 1u << i;
	}
	if (!au_mic_codec_read(m, 0x34b0u, &v)) return au_mic_fail(m, 6, m->faults != 0u ? AU_MIC_WHY_ABORT : AU_MIC_WHY_READ);
	m->r[AU_MIC_R_RESETS + 3] = v;
	variant = (v >> 1) & 0xfu;
	if (variant != 0u && variant != 5u) m->r[AU_MIC_R_DIFFER] |= 1u << 3;    /* WCD9380, WCD9385 */
	return true;
}

/* ---- step 7: the writes, each one read and changed; then every one read back, all of them reported, and the bus's complaints ---- */
static inline bool au_mic_step7(struct au_mic *m)
{
	uint32_t st;

	au_mic_wr(m, AU_MIC_SWR + 0x5008u, 0xffffffffu);
	m->did |= AU_MIC_DID_BIAS;
	for (unsigned i = 0; i < sizeof(au_mic_writes) / sizeof(au_mic_writes[0]); i++) {
		const struct au_mic_write *w = &au_mic_writes[i];

		if (!au_mic_codec_update(m, w->reg, w->mask, w->value)) {
			m->r[AU_MIC_R_BAD] = w->reg;
			return au_mic_fail(m, 7, m->faults != 0u ? AU_MIC_WHY_ABORT : AU_MIC_WHY_WRITE);
		}
		if (w->wait_us != 0u) AU_MIC_DELAY_US(w->wait_us);
	}
	for (unsigned i = 0; i < 25u; i++) {
		uint32_t v = 0;

		if (!au_mic_codec_read(m, au_mic_want[i].reg, &v)) {
			m->r[AU_MIC_R_BAD] = au_mic_want[i].reg;
			return au_mic_fail(m, 7, m->faults != 0u ? AU_MIC_WHY_ABORT : AU_MIC_WHY_READ);
		}
		m->r[AU_MIC_R_BACK + i] = v;
		m->r[AU_MIC_R_BACK_N] = i + 1u;
		if ((v & au_mic_want[i].mask) != au_mic_want[i].want && m->r[AU_MIC_R_BAD] == 0u) {
			m->r[AU_MIC_R_BAD] = au_mic_want[i].reg; m->r[AU_MIC_R_BAD + 1] = au_mic_want[i].want; m->r[AU_MIC_R_BAD + 2] = v;
			au_mic_fail(m, 7, AU_MIC_WHY_READBACK);
		}
	}
	st = au_mic_rd(m, AU_MIC_SWR + 0x5000u, 0);
	m->r[AU_MIC_R_BUS] = st;
	if (m->faults != 0u) { m->r[AU_MIC_R_STEP] = 0; return au_mic_fail(m, 7, AU_MIC_WHY_ABORT); }
	if ((st & (1u << 3 | 1u << 7)) != 0u) au_mic_fail(m, 7, AU_MIC_WHY_BUS);    /* a clash, a command's error */
	return m->r[AU_MIC_R_STEP] == 0u;
}

/* However far it got, back the way it came, with the votes and the clock still held: the bias, the master, the codec into reset and its pad as found, the macros' clocks, the pads as found. L15B stays on. */
static inline void au_mic_down(struct au_mic *m)
{
	uint32_t v = 0;

	m->going_down = true;
	if ((m->did & AU_MIC_DID_BIAS) != 0u) {
		(void)au_mic_codec_update(m, AU_MIC_MICB1, 0xc0u, 0u);
		if (au_mic_codec_read(m, AU_MIC_MICB1, &v)) m->r[AU_MIC_R_DOWN] = v;
	}
	if ((m->did & AU_MIC_DID_MASTER) != 0u) {
		au_mic_wr(m, AU_MIC_SWR + 0x5004u, 0u);
		au_mic_wr(m, AU_MIC_SWR + 0x008u, 1u);
		AU_MIC_DELAY_US(1000);
	}
	if ((m->did & AU_MIC_DID_RESET) != 0u) {
		au_mic_wr(m, AU_MIC_RESET + 4u, 0);
		AU_MIC_DELAY_US(1000);
		au_mic_wr(m, AU_MIC_RESET + 4u, m->found_pad[1] & 2u);
		au_mic_wr(m, AU_MIC_RESET, m->found_pad[0]);
		m->r[AU_MIC_R_DOWN + 1] = au_mic_rd(m, AU_MIC_RESET, 0);
		m->r[AU_MIC_R_DOWN + 2] = au_mic_rd(m, AU_MIC_RESET + 4u, 0);
	}
	if ((m->did & AU_MIC_DID_TX) != 0u) {
		au_mic_set(m, AU_MIC_TX + 8u, 1u, 0u);
		au_mic_set(m, AU_MIC_TX + 4u, 1u, 0u);
		au_mic_set(m, AU_MIC_TX, 1u, 0u);
		m->r[AU_MIC_R_DOWN + 4] = au_mic_rd(m, AU_MIC_TX + 8u, 0);
	}
	if ((m->did & AU_MIC_DID_GATES) != 0u) {
		au_mic_set(m, AU_MIC_VA + 8u, 1u, 0u);
		au_mic_set(m, AU_MIC_VA, 1u, 0u);
		au_mic_set(m, AU_MIC_VA + 4u, 1u, 0u);
		au_mic_set(m, AU_MIC_VA + 0x80u, 2u, 0u);
		for (unsigned i = 0; i < 3u; i++) au_mic_wr(m, AU_MIC_VA + 0xd0u + 4u * i, m->found_va[i]);
	}
	if ((m->did & AU_MIC_DID_PADS) != 0u) {
		for (unsigned i = 0; i < 4u; i++) au_mic_wr(m, AU_MIC_LPI + 0x1000u * au_mic_lpi_pins[i], m->found_lpi[i]);
		au_mic_wr(m, AU_MIC_SLEW, m->found_slew);
		m->r[AU_MIC_R_DOWN + 3] = au_mic_rd(m, AU_MIC_LPI, 0);
	}
	m->r[AU_MIC_R_DID] = m->did;
}

/*
 * For a capture: the codec's first data port, ADC1 on its first channel, 1 bit per sample, an interval of 2, as sheng's; the master's port m->port on m->lane; TX decimator 0 from SWR_MIC,
 * 48 kHz, 150 Hz high-pass, clocked and muted. Numbers: sheng's micStream(), with the channel, port and lane of research_notes/mic-research.md. Each read back is reported; the bus's complaints stop it.
 */
static inline bool au_mic_ports(struct au_mic *m)
{
	const uint64_t master = AU_MIC_SWR + 0x1124u + 0x100u * (m->port - 1u);
	bool ok = true;
	uint32_t v = 0, st;

	for (uint32_t bank = 0; bank <= 0x10u && ok; bank += 0x10u)
		ok = au_mic_codec_write(m, 0x122u + bank, 1u) && au_mic_codec_write(m, 0x123u + bank, 0u) && au_mic_codec_write(m, 0x124u + bank, 0u) &&
		     au_mic_codec_write(m, 0x125u + bank, 0u) && au_mic_codec_write(m, 0x126u + bank, 0xffu) && au_mic_codec_write(m, 0x128u + bank, m->lane);
	ok = ok && au_mic_codec_write(m, 0x102u, 0u) && au_mic_codec_write(m, 0x103u, 0u) && au_mic_codec_write(m, 0x105u, 1u);
	for (uint32_t bank = 0; bank <= 0x10u && ok; bank += 0x10u) ok = au_mic_codec_write(m, 0x120u + bank, 1u);
	if (!ok) return au_mic_fail(m, 8, m->faults != 0u ? AU_MIC_WHY_ABORT : AU_MIC_WHY_WRITE);
	for (uint32_t bank = 0; bank < 2u; bank++) {
		au_mic_wr(m, master + 0x40u * bank, 1u | m->lane << 24);
		au_mic_wr(m, master + 4u + 0x40u * bank, 1u);
		au_mic_wr(m, master + 0x10u + 0x40u * bank, 0xf0u);
	}
	au_mic_wr(m, master + 8u, 0u);
	au_mic_set(m, AU_MIC_TX + 0x100u, 0xfu, m->mic);
	au_mic_set(m, AU_MIC_TX + 0x104u, 3u, 1u);
	au_mic_set(m, AU_MIC_TX + 0x404u, 1u << 7 | 3u << 5 | 3u << 1, 2u << 5);
	au_mic_set(m, AU_MIC_TX + 0x400u, 0x3fu, 4u | 0x10u);
	au_mic_set(m, AU_MIC_TX + 0x400u, 0u, 0x20u);
	if (au_mic_codec_read(m, 0x128u, &v)) m->r[AU_MIC_R_PORTS] = v;
	if (au_mic_codec_read(m, 0x120u, &v)) m->r[AU_MIC_R_PORTS + 1] = v;
	m->r[AU_MIC_R_PORTS + 2] = au_mic_rd(m, master, 0);
	m->r[AU_MIC_R_PORTS + 3] = au_mic_rd(m, master + 4u, 0);
	m->r[AU_MIC_R_PORTS + 4] = au_mic_rd(m, master + 0x10u, 0);
	m->r[AU_MIC_R_PORTS + 5] = au_mic_rd(m, AU_MIC_TX + 0x100u, 0);
	m->r[AU_MIC_R_PORTS + 6] = au_mic_rd(m, AU_MIC_TX + 0x104u, 0);
	m->r[AU_MIC_R_PORTS + 7] = au_mic_rd(m, AU_MIC_TX + 0x404u, 0);
	m->r[AU_MIC_R_PORTS + 8] = au_mic_rd(m, AU_MIC_TX + 0x400u, 0);
	st = au_mic_rd(m, AU_MIC_SWR + 0x5000u, 0);
	if (m->faults != 0u) return au_mic_fail(m, 8, AU_MIC_WHY_ABORT);
	return (st & (1u << 3 | 1u << 7)) != 0u ? au_mic_fail(m, 8, AU_MIC_WHY_BUS) : true;
}

/* Steps 2 to 7, stopping at the first that fails, and the way down: unless a capture is to follow, which comes down by au_mic_down() when it is over. Touches nothing at all unless the votes and the clock are held. */
static inline void au_mic_run(struct au_mic *m)
{
	if (!m->votes_held || !m->clock_held) { au_mic_fail(m, 1, AU_MIC_WHY_VOTES); return; }
	m->r[AU_MIC_R_PASSED] = 1;
	if (au_mic_step2(m)) {
		m->r[AU_MIC_R_PASSED] = 2;
		if (au_mic_step3(m)) {
			m->r[AU_MIC_R_PASSED] = 3;
			if (au_mic_step4(m)) {
				m->r[AU_MIC_R_PASSED] = 4;
				if (au_mic_step5(m)) {
					m->r[AU_MIC_R_PASSED] = 5;
					if (au_mic_step6(m)) {
						m->r[AU_MIC_R_PASSED] = 6;
						if (au_mic_step7(m)) m->r[AU_MIC_R_PASSED] = 7;
					}
				}
			}
		}
	}
	if (m->capture && m->r[AU_MIC_R_PASSED] == 7u && au_mic_ports(m)) { m->held = true; return; }
	au_mic_down(m);
}

/* The list with a capture: the clock's tries, the graph as sheng's, the stage after the twelfth, the graph's way down, and letting go of the clock. `ring` is the map's address with its stream. */
#define AU_MIC_CAP_STEPS 18u
#define AU_MIC_CAP_RUN 12u
#define AU_MIC_CAP_RELEASE 17u
static inline uint32_t au_mic_release_step(const struct au_mic *m) { return m->capture ? AU_MIC_CAP_RELEASE : AU_MIC_TRIES; }

/* The list on the PRM: its packets, as au_tone_build(), from the PRM's own port; `m` as for au_gl_graph_send(). 19.2 MHz with attribute 1, couple no, is what the macros ask. Nothing past the end. */
static inline uint32_t au_mic_build(uint32_t step, uint32_t *m, uint32_t capacity)
{
	static const struct { uint32_t hz, attribute; } tries[AU_MIC_TRIES] = {{19200000u, 1u}, {9600000u, 1u}, {19200000u, 0u}};
	uint8_t *out = (uint8_t *)(m + 2);
	uint32_t t = AU_GL_GRAPH_TOKEN + step;

	if (step < AU_MIC_TRIES) return fuxi_ar_prm_clock_attr(out, capacity, 2, t, AU_MIC_CLOCK, tries[step].hz, tries[step].attribute);
	if (step == AU_MIC_TRIES) return fuxi_ar_prm_clock_attr(out, capacity, 2, t, AU_MIC_CLOCK, 0, 1);
	return 0;
}

static inline uint32_t au_mic_cap_build(uint32_t step, uint32_t *m, uint32_t capacity, const FuxiArFormat *f, uint64_t ring, uint32_t ring_bytes, uint32_t handle)
{
	uint8_t *out = (uint8_t *)(m + 2);
	uint32_t t = AU_GL_GRAPH_TOKEN + step;

	switch (step) {
	case 0: case 1: case 2: return au_mic_build(step, m, capacity);
	case 3: return fuxi_ar_open_mic_device(out, capacity, 1, t);
	case 4: return fuxi_ar_config_mic_device(out, capacity, 1, t, f, 1u);
	case 5: return fuxi_ar_open_capture_stream(out, capacity, 1, t);
	case 6: return fuxi_ar_config_capture_stream(out, capacity, 1, t, f);
	case 7: return fuxi_ar_map(out, capacity, 1, t, ring, ring_bytes);
	case 8: return fuxi_ar_graph_command(out, capacity, 1, t, FUXI_AR_OP_GRAPH_PREPARE, FUXI_AR_SG_MIC);
	case 9: return fuxi_ar_graph_command(out, capacity, 1, t, FUXI_AR_OP_GRAPH_PREPARE, FUXI_AR_SG_CAPTURE);
	case 10: return fuxi_ar_graph_command(out, capacity, 1, t, FUXI_AR_OP_GRAPH_START, FUXI_AR_SG_MIC);
	case 11: return fuxi_ar_graph_command(out, capacity, 1, t, FUXI_AR_OP_GRAPH_START, FUXI_AR_SG_CAPTURE);
	case 12: return fuxi_ar_graph_command(out, capacity, 1, t, FUXI_AR_OP_GRAPH_STOP, FUXI_AR_SG_CAPTURE);
	case 13: return fuxi_ar_graph_command(out, capacity, 1, t, FUXI_AR_OP_GRAPH_STOP, FUXI_AR_SG_MIC);
	case 14: return fuxi_ar_graph_command(out, capacity, 1, t, FUXI_AR_OP_GRAPH_CLOSE, FUXI_AR_SG_CAPTURE);
	case 15: return fuxi_ar_graph_command(out, capacity, 1, t, FUXI_AR_OP_GRAPH_CLOSE, FUXI_AR_SG_MIC);
	case 16: return fuxi_ar_unmap(out, capacity, 1, t, handle);
	case AU_MIC_CAP_RELEASE: return fuxi_ar_prm_clock_attr(out, capacity, 2, t, AU_MIC_CLOCK, 0, 1);
	default: return 0;
	}
}

/*
 * au_glink_run() asks this before each packet of the list, while the votes are on. True: wait, send nothing yet. False: send the list's next, if there is one. When the clock is
 * granted the whole stage runs here, then the list goes on to let it go, and the loop to the votes coming off. Votes refused, or the clock refused all three times: the stage never runs.
 */
static inline bool au_mic_turn(struct au_gl *g, struct au_mic *m)
{
	uint32_t last, status;

	if (m->over) return false;
	if (g->graph_sent == 0u) {
		m->r[AU_MIC_R_VOTE] = g->prm_status[0]; m->r[AU_MIC_R_VOTE + 1] = g->prm_status[1];
		if (g->prm_status[0] == 0u && g->prm_status[1] == 0u) return false;
		au_mic_fail(m, 1, AU_MIC_WHY_VOTES);
		m->over = true;
		g->graph_sent = au_mic_release_step(m) + 1u;
		return false;
	}
	last = g->graph_sent - 1u;
	status = g->graph_status[last];
	m->r[AU_MIC_R_TRIES + last] = status;
	if (status == AU_GL_PRM_NO_ANSWER) {
		if (!m->asked) { m->asked = true; m->asked_ms = g->ms; }
		if (g->ms - m->asked_ms < 5000u) return true;
		au_mic_fail(m, 1, AU_MIC_WHY_NO_ANSWER);
		m->over = true;
		g->graph_sent = au_mic_release_step(m);    /* it may yet have taken it: let go */
		return false;
	}
	m->asked = false;
	if (status != 0u) {
		if (last + 1u < AU_MIC_TRIES) return false;
		au_mic_fail(m, 1, AU_MIC_WHY_REFUSED);
		m->over = true;
		g->graph_sent = au_mic_release_step(m) + 1u;
		return false;
	}
	m->r[AU_MIC_R_TAKEN] = last + 1u;
	m->votes_held = true;
	m->clock_held = true;
	au_mic_run(m);
	m->over = true;
	g->graph_sent = m->held ? AU_MIC_TRIES : au_mic_release_step(m);
	return false;
}

#endif
