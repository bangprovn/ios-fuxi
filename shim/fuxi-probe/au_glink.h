/* Fuxi audio lane: the AP's end of GLINK over SMEM with the ADSP, polled, enough to see what the DSP opens. Plain C so that
 * tools/test-fuxi-audio-glink.py runs it against a fake DSP under the sanitizers; audio.c gives it the SMEM accessors.
 * The wire format follows Linux qcom_glink_native.c and qcom_glink_smem.c. It negotiates the version, answers CLOSE and an
 * intent request, and with AU_GL_ANSWER also completes the opening of a channel. Only with AU_GL_GPR does any data leave, and
 * then one fixed packet alone: APM_CMD_GET_SPF_STATE on adsp_apps, by au_gl_gpr_ask(), which reads a state and sets up nothing.
 * Define AU_GL_RD, AU_GL_WR, AU_GL_WMB and AU_GL_KICK.
 */
#ifndef AU_GLINK_H
#define AU_GLINK_H

enum { AU_GL_VERSION = 0, AU_GL_VERSION_ACK = 1, AU_GL_OPEN = 2, AU_GL_CLOSE = 3, AU_GL_OPEN_ACK = 4, AU_GL_INTENT = 5,
       AU_GL_RX_DONE = 6, AU_GL_INTENT_REQ = 7, AU_GL_INTENT_REQ_ACK = 8, AU_GL_TX_DATA = 9, AU_GL_CLOSE_ACK = 11,
       AU_GL_TX_DATA_CONT = 12, AU_GL_READ_NOTIF = 13, AU_GL_RX_DONE_REUSE = 14, AU_GL_SIGNALS = 15 };
#define AU_GL_FEATURE_REUSE 1u
#define AU_GL_RESERVE 16u      /* FIFO_FULL_RESERVE + TX_BLOCKED_CMD_RESERVE */
#define AU_GL_NAME_MOST 32u    /* GLINK_NAME_SIZE */
#define AU_GL_ANSWER 1u        /* options: complete the opening of what the DSP opens */
#define AU_GL_REC_WORDS 12u    /* ms, cmd | param1 << 16, param2, bytes after the header, then 32 bytes of them */
#define AU_GL_FROM_US 0x8000u  /* in a record's cmd: what we sent */
#define AU_GL_CHANNELS 16u
#define AU_GL_RESENDS 4u
#define AU_GL_GPR 2u           /* options: ask the APM on adsp_apps whether it is up. Needs AU_GL_ANSWER */
#define AU_GL_GPR_ASKS 8u
#define AU_GL_GPR_INTENTS 8u   /* of the DSP's buffers, as many as there are asks */
#define AU_GL_GPR_WORDS 16u    /* kept of an answer */
#define AU_GL_GPR_BUFFER 512u  /* offered for its answers: a size only, what comes in is read from the ring. Linux GPR_MAX 4096 is for graphs */
#define AU_GL_GPR_OFFERS 16u
#define AU_GL_OP_GET_SPF_STATE 0x01001021u
#define AU_GL_OP_SPF_STATE 0x02001007u  /* payload {status}: 1 ready */

struct au_gl {
	uint64_t desc, tx_fifo, rx_fifo;
	uint32_t tx_len, rx_len, options, features, ms;
	uint32_t *log;
	uint32_t log_most, log_n, log_lost;
	uint32_t rx_msgs, tx_msgs, tx_full, stuck, version_acked, version_seen, resends, opened, acked_by_dsp;
	uint16_t rcid[AU_GL_CHANNELS];
	uint32_t gpr_lcid, gpr_rcid, gpr_acked, gpr_offers, gpr_asked, gpr_heard, gpr_other, gpr_ready;
	uint32_t gpr_asked_ms, gpr_first_ms, gpr_last_ms, gpr_first_bytes, gpr_last_bytes, gpr_riid_n;
	uint32_t gpr_riid[AU_GL_GPR_INTENTS], gpr_first[AU_GL_GPR_WORDS], gpr_last[AU_GL_GPR_WORDS];
};

static inline uint32_t au_gl_align8(uint32_t n) { return (n + 7u) & ~7u; }

static inline void au_gl_note(struct au_gl *g, uint32_t cmd, uint32_t p1, uint32_t p2, uint32_t extra, const uint32_t *w, uint32_t words)
{
	if (g->log_n >= g->log_most) { g->log_lost++; return; }
	uint32_t *r = g->log + AU_GL_REC_WORDS * g->log_n++;
	r[0] = g->ms; r[1] = cmd | p1 << 16; r[2] = p2; r[3] = extra;
	for (uint32_t i = 0; i < 8; i++) r[4 + i] = i < words ? w[i] : 0;
}

/* Nothing is sent unless all of it fits. False when it does not, or when the far end's tail cannot be true. */
static inline bool au_gl_tx(struct au_gl *g, uint32_t cmd, uint32_t p1, uint32_t p2, const uint32_t *extra, uint32_t extra_bytes)
{
	uint32_t head = AU_GL_RD(g->desc + 4), tail = AU_GL_RD(g->desc), total = 8u + au_gl_align8(extra_bytes);
	uint32_t room;

	if (head >= g->tx_len || tail >= g->tx_len || (head & 7u) != 0) { g->tx_full++; return false; }
	room = tail <= head ? g->tx_len - head + tail : tail - head;
	if (room < AU_GL_RESERVE + total) { g->tx_full++; return false; }
	AU_GL_WR(g->tx_fifo + head, cmd | p1 << 16);
	AU_GL_WR(g->tx_fifo + (head + 4u) % g->tx_len, p2);
	for (uint32_t i = 0; i < au_gl_align8(extra_bytes) / 4u; i++) {
		AU_GL_WR(g->tx_fifo + (head + 8u + 4u * i) % g->tx_len, extra[i]);
	}
	AU_GL_WMB();
	AU_GL_WR(g->desc + 4, (head + total) % g->tx_len);
	AU_GL_KICK();
	g->tx_msgs++;
	au_gl_note(g, cmd | AU_GL_FROM_US, p1, p2, extra_bytes, extra, extra_bytes / 4u + ((extra_bytes & 3u) != 0));
	return true;
}

static inline bool au_gl_start(struct au_gl *g)
{
	AU_GL_WR(g->desc + 8, 0);    /* our tail of what it sends */
	AU_GL_WR(g->desc + 4, 0);    /* our head of what we send */
	g->features = AU_GL_FEATURE_REUSE;
	return au_gl_tx(g, AU_GL_VERSION, 1, g->features, 0, 0);
}

/* The word `offset` bytes past the tail. Offsets stay word aligned, as the head is 8 byte aligned. */
static inline uint32_t au_gl_word(const struct au_gl *g, uint32_t tail, uint32_t offset)
{
	return AU_GL_RD(g->rx_fifo + (tail + offset) % g->rx_len);
}

static inline int au_gl_channel(const struct au_gl *g, uint32_t rcid)
{
	for (uint32_t i = 0; i < g->opened && i < AU_GL_CHANNELS; i++) {
		if (g->rcid[i] == rcid) return (int)i;
	}
	return -1;
}

static inline void au_gl_gpr_offer(struct au_gl *g, uint32_t iid)
{
	uint32_t pair[2] = {AU_GL_GPR_BUFFER, iid};

	if (g->gpr_offers >= AU_GL_GPR_OFFERS) return;
	if (au_gl_tx(g, AU_GL_INTENT, g->gpr_lcid, 1, pair, sizeof(pair))) g->gpr_offers++;
}

/* The one data packet that can leave here. True when it went: a buffer of the DSP's was there and the ring had room. */
static inline bool au_gl_gpr_ask(struct au_gl *g)
{
	uint32_t m[8];

	if ((g->options & AU_GL_GPR) == 0 || !g->gpr_acked || g->gpr_riid_n == 0 || g->gpr_asked >= AU_GL_GPR_ASKS) return false;
	m[0] = 24; m[1] = 0;                            /* chunk size, left size */
	m[2] = 0u | 6u << 4 | 24u << 8;                 /* GPR: version 0, 6 header words, 24 bytes */
	m[3] = 2u | 3u << 8;                            /* to the ADSP's domain, from the APPS' */
	m[4] = 1; m[5] = 1;                             /* source port and destination: the APM's, as Linux q6apm */
	m[6] = g->gpr_asked + 1u;                       /* token */
	m[7] = AU_GL_OP_GET_SPF_STATE;
	if (!au_gl_tx(g, AU_GL_TX_DATA, g->gpr_lcid, g->gpr_riid[g->gpr_riid_n - 1u], m, sizeof(m))) return false;
	g->gpr_riid_n--;
	if (g->gpr_asked++ == 0) g->gpr_asked_ms = g->ms;
	return true;
}

/* An answer on adsp_apps, while it is still in the ring. `tail` is at its header, so its bytes begin 16 past. */
static inline void au_gl_gpr_take(struct au_gl *g, uint32_t tail, uint32_t bytes)
{
	uint32_t *keep = g->gpr_heard == 0 ? g->gpr_first : g->gpr_last;

	for (uint32_t i = 0; i < AU_GL_GPR_WORDS; i++) keep[i] = i * 4u < bytes ? au_gl_word(g, tail, 16u + 4u * i) : 0;
	if (g->gpr_heard == 0) { g->gpr_first_ms = g->ms; g->gpr_first_bytes = bytes; }
	g->gpr_last_ms = g->ms; g->gpr_last_bytes = bytes;
	if (keep[5] == AU_GL_OP_SPF_STATE && bytes >= 28u && keep[6] == 1u) g->gpr_ready = 1;
	g->gpr_heard++;
}

/*
 * One message from the DSP, if a whole one is there. True when one was taken. Lengths come from the far end, so each is
 * bounded before use; what cannot be read safely stops the reader for good (g->stuck), as there is no way to find the next.
 */
static inline bool au_gl_step(struct au_gl *g)
{
	uint32_t head, tail, avail, w0, cmd, p1, p2, extra = 0, total, words[8];
	bool gpr_channel;

	if (g->stuck || !g->rx_fifo || !g->rx_len) return false;
	head = AU_GL_RD(g->desc + 12);
	tail = AU_GL_RD(g->desc + 8);
	if (head >= g->rx_len || tail >= g->rx_len || ((head | tail) & 3u) != 0) { g->stuck = 1; return false; }
	avail = head < tail ? g->rx_len - tail + head : head - tail;
	if (avail < 8) return false;
	w0 = au_gl_word(g, tail, 0);
	p2 = au_gl_word(g, tail, 4);
	cmd = w0 & 0xffffu;
	p1 = w0 >> 16;
	total = 8;
	switch (cmd) {
	case AU_GL_VERSION: case AU_GL_VERSION_ACK: case AU_GL_CLOSE: case AU_GL_OPEN_ACK: case AU_GL_RX_DONE:
	case AU_GL_INTENT_REQ: case AU_GL_INTENT_REQ_ACK: case AU_GL_CLOSE_ACK: case AU_GL_READ_NOTIF:
	case AU_GL_RX_DONE_REUSE: case AU_GL_SIGNALS:
		break;
	case AU_GL_OPEN:
		if (p2 == 0 || p2 > g->rx_len / 2) { g->stuck = 2; return false; }
		extra = p2;
		total = 8u + au_gl_align8(p2);
		break;
	case AU_GL_INTENT:
		if (p2 > g->rx_len / 16) { g->stuck = 3; return false; }
		extra = 8u * p2;
		total = 8u + extra;
		break;
	case AU_GL_TX_DATA: case AU_GL_TX_DATA_CONT:
		if (avail < 16) return false;
		extra = au_gl_word(g, tail, 8);
		if (extra > g->rx_len / 2) { g->stuck = 4; return false; }
		total = au_gl_align8(16u + extra);
		break;
	default:
		g->stuck = 5;
		au_gl_note(g, cmd, p1, p2, 0, 0, 0);
		return false;
	}
	if (avail < total) return false;
	for (uint32_t i = 0; i < 8; i++) {
		words[i] = i * 4u < total - 8u ? au_gl_word(g, tail, 8u + 4u * i) : 0;
	}
	g->rx_msgs++;
	gpr_channel = g->gpr_lcid != 0 && p1 == g->gpr_rcid;
	/* Every channel's buffers would fill the log before the answer comes. */
	if ((g->options & AU_GL_GPR) == 0 || cmd != AU_GL_INTENT || gpr_channel) au_gl_note(g, cmd, p1, p2, extra, words, 8);
	if (cmd == AU_GL_TX_DATA && gpr_channel) au_gl_gpr_take(g, tail, extra);
	AU_GL_WR(g->desc + 8, (tail + total) % g->rx_len);

	switch (cmd) {
	case AU_GL_VERSION:
		if (p1 == 0) break;
		if (p1 == 1) g->features &= p2;
		g->version_seen = 1;
		(void)au_gl_tx(g, AU_GL_VERSION_ACK, 1, g->features, 0, 0);
		break;
	case AU_GL_VERSION_ACK:
		if (p1 == 0) break;
		if (p1 == 1 && p2 == g->features) { g->version_acked = 1; break; }
		if (p1 == 1) g->features &= p2;
		if (g->resends++ < AU_GL_RESENDS) (void)au_gl_tx(g, AU_GL_VERSION, 1, g->features, 0, 0);
		break;
	case AU_GL_OPEN:
		if ((g->options & AU_GL_ANSWER) != 0 && g->opened < AU_GL_CHANNELS && p2 <= AU_GL_NAME_MOST && au_gl_channel(g, p1) < 0) {
			/* As qcom_glink_create_remote(): its opening acknowledged, then ours under the same name. */
			uint32_t lcid = ++g->opened;

			g->rcid[lcid - 1] = (uint16_t)p1;
			(void)au_gl_tx(g, AU_GL_OPEN_ACK, p1, 0, 0, 0);
			(void)au_gl_tx(g, AU_GL_OPEN, lcid, p2, words, p2);
			/* "adsp_apps" and its NUL */
			if ((g->options & AU_GL_GPR) != 0 && p2 == 10u && words[0] == 0x70736461u && words[1] == 0x7070615fu && (words[2] & 0xffffu) == 0x0073u) {
				g->gpr_lcid = lcid;
				g->gpr_rcid = p1;
			}
		}
		break;
	case AU_GL_OPEN_ACK:
		g->acked_by_dsp++;
		/* As Linux once a channel is up: buffers offered, so that the APM has somewhere to answer. */
		if (g->gpr_lcid != 0 && p1 == g->gpr_lcid && !g->gpr_acked) {
			g->gpr_acked = 1;
			au_gl_gpr_offer(g, 1);
			au_gl_gpr_offer(g, 2);
		}
		break;
	case AU_GL_INTENT:
		if (gpr_channel) {
			for (uint32_t i = 0; i < p2 && i < 4u; i++) {
				if (words[2u * i] >= 24u && g->gpr_riid_n < AU_GL_GPR_INTENTS) g->gpr_riid[g->gpr_riid_n++] = words[2u * i + 1u];
			}
		}
		break;
	case AU_GL_TX_DATA: case AU_GL_TX_DATA_CONT:
		if (!gpr_channel) { g->gpr_other++; break; }
		/* Its buffer back at once, once all of it is in. */
		if (words[1] == 0u) {
			if ((g->features & AU_GL_FEATURE_REUSE) != 0) { (void)au_gl_tx(g, AU_GL_RX_DONE_REUSE, g->gpr_lcid, p2, 0, 0); }
			else { (void)au_gl_tx(g, AU_GL_RX_DONE, g->gpr_lcid, p2, 0, 0); au_gl_gpr_offer(g, p2); }
		}
		break;
	case AU_GL_CLOSE:
		(void)au_gl_tx(g, AU_GL_CLOSE_ACK, p1, 0, 0, 0);
		break;
	case AU_GL_INTENT_REQ: {
		int channel = au_gl_channel(g, p1);

		/* adsp_apps is given what it asks, up to a point. Anywhere else no buffer is offered: nothing is to come in there. */
		if (gpr_channel && p2 <= AU_GL_GPR_BUFFER && g->gpr_offers < AU_GL_GPR_OFFERS) {
			au_gl_gpr_offer(g, g->gpr_offers + 1u);
			(void)au_gl_tx(g, AU_GL_INTENT_REQ_ACK, g->gpr_lcid, 1, 0, 0);
		} else if (channel >= 0) {
			(void)au_gl_tx(g, AU_GL_INTENT_REQ_ACK, (uint32_t)channel + 1u, 0, 0, 0);
		}
		break;
	}
	case AU_GL_READ_NOTIF:
		AU_GL_KICK();
		break;
	default:
		break;
	}
	return true;
}

#endif
