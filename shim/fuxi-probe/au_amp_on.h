/* Fuxi audio lane: one CS35L41 up, muted, by its registers, for a first tone with no DSP on the amplifier. The sequence is research_notes/cs35l41-research.md 3.2, 3.3 and 3.5,
 * from driver sources: there is no datasheet, and it has never run on anything. **Writing to an amplifier is the one thing in this lane that can damage hardware: a DC or a
 * misframed full-scale signal into the speaker.** Only audio.c's au_tone_stage() calls this, at the lowest level, on the bottom amplifier, on the owner's word; tools/test-fuxi-audio-amp-on.py holds that.
 *
 * Every write of a whole register is read back, and a difference ends it. Boost peak is set before anything can enable the boost. Analog gain stays at its lowest, 0.5 dB, the
 * digital volume muted, until au_amp_on_unmute() with one of three fixed levels. Any error: au_amp_on_abort(), then the caller holds the amplifier in reset. Nothing is retried.
 * Define as for au_amp.h, which comes first: AU_AMP_RD, AU_AMP_WR, AU_AMP_DELAY_US.
 */
#ifndef AU_AMP_ON_H
#define AU_AMP_ON_H

enum { AU_ON_OK = 0, AU_ON_I2C = 0x10, AU_ON_READBACK = 0x20, AU_ON_OTP = 0x30, AU_ON_WHO = 0x40, AU_ON_PLL = 0x50, AU_ON_PUP = 0x60, AU_ON_FAULT = 0x70 };

#define AU_ON_ERRORS 0x800281c0u    /* 0x10010: amp short, temp error, temp warn, boost short, DCM UVP, boost OVP */
#define AU_ON_MUTE   0xa678u        /* 0x6000: HPF on, volume muted */
#define AU_ON_LEVELS 3u

struct au_on {
	uint64_t se;
	uint32_t address, failed_reg, failed_want, failed_got, step, status[4], otp_fields, pup_ms;
};

/* The one write: address, value, both big-endian, in a single I2C message. Only this file calls it. */
static inline int au_on_write(struct au_on *o, uint32_t reg, uint32_t value)
{
	uint32_t seen = 0;
	int r;

	if (AU_AMP_WR(o->se + AU_SE_M_IRQ_CLEAR, 0xffffffffu) || AU_AMP_WR(o->se + AU_SE_S_IRQ_CLEAR, 0xffffffffu) || AU_AMP_WR(o->se + AU_SE_TX_TRANS_LEN, 8u)) return AU_ON_I2C;
	if (AU_AMP_WR(o->se + AU_SE_M_CMD0, AU_I2C_WRITE << 27 | o->address << 9)) return AU_ON_I2C;
	if (AU_AMP_WR(o->se + AU_SE_TX_FIFO, au_amp_be(reg)) || AU_AMP_WR(o->se + AU_SE_TX_FIFO, au_amp_be(value))) return AU_ON_I2C;
	r = au_amp_wait(o->se, &seen);
	return r == AU_AMP_OK ? AU_ON_OK : AU_ON_I2C;
}

static inline int au_on_read(struct au_on *o, uint32_t reg, uint32_t *value)
{
	uint32_t seen = 0;

	if (au_amp_read(o->se, o->address, reg, value, &seen) != AU_AMP_OK) {
		o->failed_reg = reg;
		return AU_ON_I2C;
	}
	return AU_ON_OK;
}

/* A write, and the same value read back. For registers that hold what they are given. */
static inline int au_on_set(struct au_on *o, uint32_t reg, uint32_t value)
{
	uint32_t got = 0;
	int r = au_on_write(o, reg, value);

	if (r == AU_ON_OK) r = au_on_read(o, reg, &got);
	if (r != AU_ON_OK) { o->failed_reg = reg; return r; }
	if (got != value) { o->failed_reg = reg; o->failed_want = value; o->failed_got = got; return AU_ON_READBACK; }
	return AU_ON_OK;
}

static inline int au_on_rmw(struct au_on *o, uint32_t reg, uint32_t mask, uint32_t value)
{
	uint32_t was = 0;
	int r = au_on_read(o, reg, &was);

	return r != AU_ON_OK ? r : au_on_set(o, reg, (was & ~mask) | (value & mask));
}

/* OTP's trims onto their registers: map 1 for OTPID 1 or 8, as Cirrus's mcu-drivers (Apache-2.0) has it. Starts at word 2, bit 16; bits LSB first across words; address 0 only uses them up. */
struct au_otp_field { uint32_t addr; uint8_t shift, size; };
static const struct au_otp_field au_otp_map1[99] = {
	{0x02030,0,4},{0x02030,7,1},{0x0208c,24,6},{0x02090,14,4},{0x02090,10,4},{0x0300c,11,4},
	{0x0394c,23,2},{0x03950,0,7},{0x03950,8,7},{0x03950,16,8},{0x03950,24,8},{0x03954,0,7},
	{0x03954,8,7},{0x03954,16,8},{0x03954,24,8},{0x03958,0,7},{0x03958,8,7},{0x03958,16,8},
	{0x03958,24,8},{0x0395c,0,7},{0x0395c,8,7},{0x0395c,16,8},{0x0395c,24,8},{0x0416c,0,8},
	{0x04160,0,7},{0x0416c,8,8},{0x04160,16,10},{0x0416c,16,12},{0x0416c,28,1},{0x04170,0,6},
	{0x04170,6,1},{0x04170,8,6},{0x04170,14,1},{0x04170,16,9},{0x04360,0,5},{0x04360,6,9},
	{0x04448,0,8},{0x04448,8,8},{0x04448,16,8},{0x04448,24,8},{0x0444c,0,3},{0x06e30,0,5},
	{0x06e30,8,5},{0x06e30,16,5},{0x06e30,24,5},{0x06e34,0,5},{0x06e34,8,5},{0x06e34,16,5},
	{0x06e34,24,5},{0x06e38,0,5},{0x06e38,8,5},{0x06e38,16,5},{0x06e38,24,5},{0x06e3c,0,5},
	{0x06e3c,8,5},{0x06e3c,16,5},{0x06e3c,24,5},{0x06e40,0,5},{0x06e40,8,5},{0x06e40,16,5},
	{0x06e40,24,5},{0x06e44,0,5},{0x06e48,0,10},{0x06e48,10,10},{0x06e48,20,10},{0x06e4c,0,10},
	{0x06e4c,10,10},{0x06e4c,20,10},{0x06e50,0,10},{0x06e50,10,10},{0x06e50,20,10},{0x06e54,0,10},
	{0x06e54,10,10},{0x06e54,20,10},{0x06e58,0,10},{0x06e58,10,10},{0x06e58,20,10},{0x06e5c,0,10},
	{0x06e5c,10,10},{0x06e5c,20,10},{0x06e60,0,10},{0x06e60,10,10},{0x06e60,20,10},{0x06e64,0,10},
	{0x07418,7,5},{0x0741c,0,5},{0x0741c,11,4},{0x0741c,19,4},{0x07434,17,1},{0x07434,18,7},
	{0x07068,0,9},{0x0410c,7,1},{0x0400c,0,7},{0x00000,0,1},{0x17040,0,8},{0x17040,8,8},
	{0x17040,16,8},{0x17040,24,8},{0x17044,0,24},
};

static inline uint32_t au_otp_take(const uint32_t words[32], uint32_t *bit, uint32_t size)
{
	uint32_t value = 0;

	for (uint32_t i = 0; i < size; i++, (*bit)++) {
		if (*bit / 32u >= 32u) return 0;
		value |= ((words[*bit / 32u] >> (*bit % 32u)) & 1u) << i;
	}
	return value;
}

static inline int au_on_otp(struct au_on *o)
{
	static uint32_t words[32];
	uint32_t bit = 80u;     /* word 2, bit 16 */
	int r;

	for (uint32_t i = 0; i < 32; i++) {
		if ((r = au_on_read(o, 0x400u + 4u * i, &words[i])) != AU_ON_OK) return r;
	}
	for (uint32_t i = 0; i < 99; i++) {
		const struct au_otp_field *f = &au_otp_map1[i];
		uint32_t value = au_otp_take(words, &bit, f->size), mask = f->size >= 32u ? 0xffffffffu : (1u << f->size) - 1u;

		if (f->addr == 0u) continue;
		/* Whatever these are, each is a trim from the part's own OTP. Some read back otherwise, so only the I2C is checked. */
		uint32_t was = 0;
		if ((r = au_on_read(o, f->addr, &was)) != AU_ON_OK) return r;
		if ((r = au_on_write(o, f->addr, (was & ~(mask << f->shift)) | (value & mask) << f->shift)) != AU_ON_OK) { o->failed_reg = f->addr; return r; }
		o->otp_fields++;
	}
	return AU_ON_OK;
}

/* Steps 1 to 26 of the sequence, out of reset already and the other amplifier held in it: right up to the amplifier's enable, with no clocks needed yet and GLOBAL_EN off. */
static inline int au_on_prepare(struct au_on *o, bool right_slot, bool monitor)
{
	static const uint32_t errata[][2] = {{0x4100,0},{0x4310,0},{0x4400,0},{0x381c,0x51},{0x02bc20e0,0},{0x02bc2020,0},{0x2018,0},{0x6c04,0},{0x4c28,0},{0x4c2c,0}};
	uint32_t status = 0, who = 0, rev = 0, otp = 0;
	int r;

#define AU_ON_STEP(n, call) do { o->step = (n); if ((r = (call)) != AU_ON_OK) return r; } while (0)
	for (unsigned i = 0; ; i++) {
		AU_ON_STEP(2, au_on_read(o, 0x1001c, &status));
		if (status & 2u) break;
		if (i >= 100) return AU_ON_OTP;
		AU_AMP_DELAY_US(1000);
	}
	AU_ON_STEP(3, au_on_read(o, 0x10018, &status));
	if (status >> 31) return AU_ON_OTP;
	AU_ON_STEP(4, au_on_read(o, 0, &who));
	AU_ON_STEP(4, au_on_read(o, 4, &rev));
	AU_ON_STEP(6, au_on_read(o, 0x10, &otp));
	if (who != 0x35a40u || (rev != 0xb2u && rev != 0xb0u) || (otp != 1u && otp != 8u)) return AU_ON_WHO;
	AU_ON_STEP(7, au_on_write(o, 0x40, 0x55));
	AU_ON_STEP(7, au_on_write(o, 0x40, 0xaa));
	for (unsigned i = 0; i < sizeof(errata) / sizeof(errata[0]); i++) AU_ON_STEP(8, au_on_write(o, errata[i][0], errata[i][1]));
	AU_ON_STEP(9, au_on_write(o, 0x02bc1000, 0));
	AU_ON_STEP(10, au_on_otp(o));
	AU_ON_STEP(11, au_on_write(o, 0x40, 0xcc));
	AU_ON_STEP(11, au_on_write(o, 0x40, 0x33));
	/* Stale flags away, each status word written back */
	for (unsigned i = 0; i < 4; i++) {
		AU_ON_STEP(12, au_on_read(o, 0x10010 + 4u * i, &status));
		AU_ON_STEP(12, au_on_write(o, 0x10010 + 4u * i, status));
	}
	/* Boost for this board: 1000 nH, 15 uF, 4000 mA, never above it. Reset's 4500 mA is why this comes first. */
	AU_ON_STEP(13, au_on_rmw(o, 0x3810, 0xffff, 0x2424));
	AU_ON_STEP(13, au_on_rmw(o, 0x3814, 0xff03, 0x7500));
	AU_ON_STEP(13, au_on_rmw(o, 0x3808, 0x7f, 0x40));
	AU_ON_STEP(14, au_on_read(o, 0x3800, &status));
	if (status != 0u) return AU_ON_READBACK;
	AU_ON_STEP(15, au_on_set(o, 0x2018, 0x20));
	AU_ON_STEP(16, au_on_set(o, 0x6c04, 0));
	AU_ON_STEP(16, au_on_set(o, 0x6000, AU_ON_MUTE));
	AU_ON_STEP(17, au_on_set(o, 0x2c0c, 3));
	AU_ON_STEP(17, au_on_set(o, 0x4804, 0x28));
	AU_ON_STEP(18, au_on_set(o, 0x4808, 0x20200000));
	AU_ON_STEP(19, au_on_set(o, 0x4840, 0x18));
	AU_ON_STEP(19, au_on_set(o, 0x4830, 0x18));
	AU_ON_STEP(20, au_on_set(o, 0x4820, right_slot ? 0x00000401u : 0x00000200u));
	AU_ON_STEP(21, au_on_set(o, 0x480c, 3));
	AU_ON_STEP(22, au_on_set(o, 0x4c00, 8));
	for (unsigned i = 0; i < 4; i++) AU_ON_STEP(22, au_on_set(o, 0x4c20 + 4u * i, 0));
	AU_ON_STEP(23, au_on_set(o, 0x2d10, 0x00018010));
	AU_ON_STEP(24, au_on_set(o, 0x4800, monitor ? 0x00010003u : 0x00010000u));
	if (monitor) {
		/* Its voltage and current back on SDOUT, in slots 2 and 3, where nothing is sent to it: ASPTX1 VMON, ASPTX2 IMON. Only reads what it is doing. */
		AU_ON_STEP(24, au_on_set(o, 0x4810, 0x03020302));    /* SP_FRAME_TX_SLOT */
		AU_ON_STEP(24, au_on_set(o, 0x4c20, 0x18));
		AU_ON_STEP(24, au_on_set(o, 0x4c24, 0x19));
	}
	/* The PLL from the bit clock: open loop, reference, off, source, closed, on */
	AU_ON_STEP(25, au_on_rmw(o, 0x2c04, 0x800, 0x800));
	AU_ON_STEP(25, au_on_rmw(o, 0x2c04, 0x7e0, 0x28u << 5));
	AU_ON_STEP(25, au_on_rmw(o, 0x2c04, 0x10, 0));
	AU_ON_STEP(25, au_on_rmw(o, 0x2c04, 0x7, 0));
	AU_ON_STEP(25, au_on_rmw(o, 0x2c04, 0x800, 0));
	AU_ON_STEP(25, au_on_rmw(o, 0x2c04, 0x10, 0x10));
	AU_ON_STEP(26, au_on_set(o, 0x2018, monitor ? 0x3021u : 0x21u));    /* with VMON_EN and IMON_EN as Android has them */
	return AU_ON_OK;
}

/* Steps 28 to 31. The caller has the TDM clocks running, on zeros, first: the PLL needs them. Muted all the way. */
static inline int au_on_power_up(struct au_on *o)
{
	uint32_t status = 0;
	int r;

	AU_ON_STEP(28, au_on_write(o, 0x40, 0x55));
	AU_ON_STEP(28, au_on_write(o, 0x40, 0xaa));
	AU_ON_STEP(28, au_on_write(o, 0x2084, 0x002f1aa0));
	AU_ON_STEP(28, au_on_write(o, 0x40, 0xcc));
	AU_ON_STEP(28, au_on_write(o, 0x40, 0x33));
	AU_ON_STEP(29, au_on_rmw(o, 0x2014, 1, 1));
	for (o->pup_ms = 0; ; o->pup_ms++) {
		AU_ON_STEP(30, au_on_read(o, 0x10010, &status));
		if (status & AU_ON_ERRORS) return AU_ON_FAULT;
		if (status & (1u << 24)) break;
		if (o->pup_ms >= 100) return AU_ON_PUP;
		AU_AMP_DELAY_US(1000);
	}
	AU_ON_STEP(30, au_on_write(o, 0x10010, 1u << 24));
	AU_ON_STEP(31, au_on_read(o, 0x10018, &status));
	if ((status & 2u) == 0u || (status & (1u << 4)) != 0u || (status >> 31) != 0u) return AU_ON_PLL;
	return AU_ON_OK;
}

/* Anything latched since, each of the four words kept. A nonzero answer means stop now. */
static inline int au_on_check(struct au_on *o)
{
	int r;

	for (unsigned i = 0; i < 4; i++) {
		if ((r = au_on_read(o, 0x10010 + 4u * i, &o->status[i])) != AU_ON_OK) return r;
	}
	return (o->status[0] & AU_ON_ERRORS) != 0u ? AU_ON_FAULT : AU_ON_OK;
}

/* Digital volume, by the table of the research's section 5: only these, -30, -20 and -12 dB, analog staying at 0.5 dB. Keep the high-pass on: that is the guard against DC. */
static inline int au_on_unmute(struct au_on *o, unsigned level)
{
	static const uint16_t table[AU_ON_LEVELS] = {0xb885, 0xbb05, 0xbd05};

	return level < AU_ON_LEVELS ? au_on_set(o, 0x6000, table[level]) : AU_ON_READBACK;
}

/* 3.5, and for any failure too. Best effort: every step is tried, and the caller then holds the amplifier in reset whatever this says. */
static inline void au_on_down(struct au_on *o)
{
	uint32_t status = 0;

	(void)au_on_write(o, 0x6c04, 0);
	(void)au_on_write(o, 0x6000, AU_ON_MUTE);
	(void)au_on_rmw(o, 0x2014, 1, 0);
	for (unsigned i = 0; i < 100; i++) {
		if (au_on_read(o, 0x10010, &status) != AU_ON_OK || (status & (1u << 23)) != 0u) break;
		AU_AMP_DELAY_US(1000);
	}
	(void)au_on_write(o, 0x10010, 1u << 23);
	(void)au_on_write(o, 0x40, 0x55);
	(void)au_on_write(o, 0x40, 0xaa);
	(void)au_on_write(o, 0x2084, 0x002f1aa3);
	(void)au_on_write(o, 0x40, 0xcc);
	(void)au_on_write(o, 0x40, 0x33);
	(void)au_on_rmw(o, 0x2018, 1, 0);
	(void)au_on_write(o, 0x4800, 0);
}

#endif
