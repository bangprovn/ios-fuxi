/* Fuxi audio lane: reading the two CS35L41 amplifiers' identities over I2C, by GENI FIFO commands, polled. Plain C so that
 * tools/test-fuxi-audio-amp.py runs it against a fake serial engine and fake amplifiers under the sanitizers.
 *
 * It reads and nothing else. An I2C read of this amplifier is a 4 byte write of the register's address followed by a 4 byte read, and that
 * first write is the one thing sent: au_amp_read() is the only caller of the write command, with a 4 byte length, and no function here takes
 * a value to write. The test holds this to the source. Registers and order from research_notes/cs35l41-research.md 3.2, steps 1 to 6.
 * GENI numbers from Linux qcom-geni-se.h and i2c-qcom-geni.c. Define AU_AMP_RD, AU_AMP_WR (returning nonzero on a fault), AU_AMP_DELAY_US.
 */
#ifndef AU_AMP_H
#define AU_AMP_H

#define AU_SE_FORCE_DEFAULT 0x020u
#define AU_SE_OUTPUT_CTRL   0x024u
#define AU_SE_CGC_CTRL      0x028u
#define AU_SE_STATUS        0x040u
#define AU_SE_M_CLK_CFG     0x048u
#define AU_SE_IF_DISABLE    0x064u
#define AU_SE_FW_REV        0x068u
#define AU_SE_CLK_SEL       0x07cu
#define AU_SE_DMA_MODE      0x258u
#define AU_SE_TX_PACK0      0x260u
#define AU_SE_TX_PACK1      0x264u
#define AU_SE_TX_TRANS_LEN  0x26cu
#define AU_SE_RX_TRANS_LEN  0x270u
#define AU_SE_SCL_COUNTERS  0x278u
#define AU_SE_RX_PACK0      0x284u
#define AU_SE_RX_PACK1      0x288u
#define AU_SE_M_CMD0        0x600u
#define AU_SE_M_CMD_CTRL    0x604u
#define AU_SE_M_IRQ_STATUS  0x610u
#define AU_SE_M_IRQ_EN      0x614u
#define AU_SE_M_IRQ_CLEAR   0x618u
#define AU_SE_S_IRQ_CLEAR   0x648u
#define AU_SE_TX_FIFO       0x700u
#define AU_SE_RX_FIFO       0x780u
#define AU_SE_RX_STATUS     0x804u
#define AU_SE_RX_WM         0x810u
#define AU_SE_RX_RFR_WM     0x814u
#define AU_SE_GSI_EVENT_EN  0xe18u
#define AU_SE_IRQ_EN        0xe1cu
#define AU_SE_HW_PARAM0     0xe24u

#define AU_M_CMD_DONE   (1u << 0)
#define AU_M_ABORT_DONE (1u << 5)
#define AU_M_ERRORS     (1u << 1 | 1u << 2 | 1u << 3 | 1u << 10 | 1u << 11 | 1u << 12 | 1u << 13)  /* overrun, illegal, failure, the four the I2C firmware raises */
#define AU_M_NACK       (1u << 10)
#define AU_I2C_WRITE    1u
#define AU_I2C_READ     2u
#define AU_I2C_STRETCH  (1u << 2)  /* no stop: the read follows */

enum { AU_AMP_OK = 0, AU_AMP_NACK = 1, AU_AMP_BUS = 2, AU_AMP_TIMEOUT = 3, AU_AMP_FAULT = 4, AU_AMP_SHORT = 5 };

#define AU_AMP_DEVID     0x00000u
#define AU_AMP_REVID     0x00004u
#define AU_AMP_OTPID     0x00010u
#define AU_AMP_IRQ1_ST3  0x10018u  /* bit 31 OTP boot error */
#define AU_AMP_IRQ1_ST4  0x1001cu  /* bit 1 OTP boot done */

/* One amplifier's answers: each read's outcome, then the values. */
struct au_amp {
	uint32_t address, result, boot_ms, boot_status, boot_error, devid, revid, otpid, irq_seen, faults;
};

static inline uint32_t au_amp_be(uint32_t v) { return v >> 24 | (v >> 8 & 0xff00u) | (v << 8 & 0xff0000u) | v << 24; }

static inline void au_amp_abort(uint64_t se)
{
	(void)AU_AMP_WR(se + AU_SE_M_CMD_CTRL, 2u);    /* M_GENI_CMD_ABORT */
	for (unsigned i = 0; i < 2000; i++) {
		if (AU_AMP_RD(se + AU_SE_M_IRQ_STATUS) & AU_M_ABORT_DONE) break;
		AU_AMP_DELAY_US(10);
	}
	(void)AU_AMP_WR(se + AU_SE_M_IRQ_CLEAR, 0xffffffffu);
}

/* A command's end. 100 kHz: 4 bytes and an address are under a millisecond, so 20 ms means it is stuck, and the clock stretched by nothing here. */
static inline int au_amp_wait(uint64_t se, uint32_t *seen)
{
	for (unsigned i = 0; i < 2000; i++) {
		uint32_t m = AU_AMP_RD(se + AU_SE_M_IRQ_STATUS);

		*seen |= m;
		if (m & AU_M_NACK) { au_amp_abort(se); return AU_AMP_NACK; }
		if (m & AU_M_ERRORS) { au_amp_abort(se); return AU_AMP_BUS; }
		if (m & AU_M_CMD_DONE) return AU_AMP_OK;
		AU_AMP_DELAY_US(10);
	}
	au_amp_abort(se);
	return AU_AMP_TIMEOUT;
}

/* The one read: the register's address, with no stop, then its 4 bytes. Big-endian both ways, stride 4. */
static inline int au_amp_read(uint64_t se, uint32_t address, uint32_t reg, uint32_t *value, uint32_t *seen)
{
	int r;
	uint32_t room;

	if (AU_AMP_WR(se + AU_SE_M_IRQ_CLEAR, 0xffffffffu) || AU_AMP_WR(se + AU_SE_S_IRQ_CLEAR, 0xffffffffu)) return AU_AMP_FAULT;
	if (AU_AMP_WR(se + AU_SE_TX_TRANS_LEN, 4u)) return AU_AMP_FAULT;
	if (AU_AMP_WR(se + AU_SE_M_CMD0, AU_I2C_WRITE << 27 | AU_I2C_STRETCH | address << 9)) return AU_AMP_FAULT;
	if (AU_AMP_WR(se + AU_SE_TX_FIFO, au_amp_be(reg))) return AU_AMP_FAULT;
	r = au_amp_wait(se, seen);
	if (r != AU_AMP_OK) return r;
	if (AU_AMP_WR(se + AU_SE_M_IRQ_CLEAR, 0xffffffffu)) return AU_AMP_FAULT;
	if (AU_AMP_WR(se + AU_SE_RX_TRANS_LEN, 4u)) return AU_AMP_FAULT;
	if (AU_AMP_WR(se + AU_SE_M_CMD0, AU_I2C_READ << 27 | address << 9)) return AU_AMP_FAULT;
	r = au_amp_wait(se, seen);
	if (r != AU_AMP_OK) return r;
	room = AU_AMP_RD(se + AU_SE_RX_STATUS) & 0x1ffffffu;
	if (room == 0u) return AU_AMP_SHORT;
	*value = au_amp_be(AU_AMP_RD(se + AU_SE_RX_FIFO));
	return AU_AMP_OK;
}

/* The engine for FIFO commands at 100 kHz from XO, as Linux geni_i2c_probe() and geni_se_init(). False, touching nothing, unless it holds I2C firmware with FIFOs allowed. */
static inline bool au_amp_engine(uint64_t se)
{
	uint32_t depth;

	if (((AU_AMP_RD(se + AU_SE_FW_REV) >> 8) & 0xffu) != 3u || (AU_AMP_RD(se + AU_SE_IF_DISABLE) & 1u) != 0u || (AU_AMP_RD(se + AU_SE_STATUS) & 1u) != 0u) return false;
	depth = (AU_AMP_RD(se + AU_SE_HW_PARAM0) >> 16) & 0x3fu;
	if (depth < 4u) return false;
	return !(AU_AMP_WR(se + AU_SE_GSI_EVENT_EN, 0) || AU_AMP_WR(se + AU_SE_DMA_MODE, 0) ||
	    AU_AMP_WR(se + AU_SE_M_IRQ_CLEAR, 0xffffffffu) || AU_AMP_WR(se + AU_SE_S_IRQ_CLEAR, 0xffffffffu) ||
	    AU_AMP_WR(se + AU_SE_CGC_CTRL, AU_AMP_RD(se + AU_SE_CGC_CTRL) | 0x7fu) ||
	    AU_AMP_WR(se + AU_SE_OUTPUT_CTRL, 0x7fu) || AU_AMP_WR(se + AU_SE_FORCE_DEFAULT, 1u) ||
	    AU_AMP_WR(se + AU_SE_IRQ_EN, AU_AMP_RD(se + AU_SE_IRQ_EN) | 0xfu) ||
	    AU_AMP_WR(se + AU_SE_RX_WM, 1u) || AU_AMP_WR(se + AU_SE_RX_RFR_WM, depth - 2u) ||
	    /* Bytes in order, each sent from its top bit, as geni_se_config_packing(8, 4, true, ...) */
	    AU_AMP_WR(se + AU_SE_TX_PACK0, 0x7f8feu) || AU_AMP_WR(se + AU_SE_TX_PACK1, 0xffefeu) ||
	    AU_AMP_WR(se + AU_SE_RX_PACK0, 0x7f8feu) || AU_AMP_WR(se + AU_SE_RX_PACK1, 0xffefeu) ||
	    AU_AMP_WR(se + AU_SE_M_IRQ_EN, AU_AMP_RD(se + AU_SE_M_IRQ_EN) | AU_M_CMD_DONE | AU_M_ABORT_DONE | AU_M_ERRORS) ||
	    /* XO, undivided at DFS level 0; the 100 kHz line of geni_i2c_clk_map_19p2mhz: divider 7, high 10, low 12, cycle 26 */
	    AU_AMP_WR(se + AU_SE_CLK_SEL, 0) || AU_AMP_WR(se + AU_SE_M_CLK_CFG, 7u << 4 | 1u) ||
	    AU_AMP_WR(se + AU_SE_SCL_COUNTERS, 10u << 20 | 12u << 10 | 26u));
}

/* Out of reset already: its OTP boot, then who it is. Each read's failure ends it, with what came so far. */
static inline void au_amp_identify(uint64_t se, struct au_amp *a)
{
	uint32_t status = 0;
	int r = AU_AMP_NACK;

	/* Up to 100 ms for OTP boot, by bit 1; a NACK here may be the part still waking. */
	for (a->boot_ms = 0; a->boot_ms < 100; a->boot_ms++) {
		r = au_amp_read(se, a->address, AU_AMP_IRQ1_ST4, &status, &a->irq_seen);
		if (r == AU_AMP_OK && (status & 2u) != 0u) break;
		if (r != AU_AMP_OK && r != AU_AMP_NACK) break;
		AU_AMP_DELAY_US(1000);
	}
	a->boot_status = status;
	a->result = (uint32_t)r;
	if (r != AU_AMP_OK) return;
	if ((r = au_amp_read(se, a->address, AU_AMP_IRQ1_ST3, &a->boot_error, &a->irq_seen)) != AU_AMP_OK ||
	    (r = au_amp_read(se, a->address, AU_AMP_DEVID, &a->devid, &a->irq_seen)) != AU_AMP_OK ||
	    (r = au_amp_read(se, a->address, AU_AMP_REVID, &a->revid, &a->irq_seen)) != AU_AMP_OK ||
	    (r = au_amp_read(se, a->address, AU_AMP_OTPID, &a->otpid, &a->irq_seen)) != AU_AMP_OK) {
		a->result = (uint32_t)r;
	}
}

#endif
