/*
 * A mailbox from the PC to the phone's kernel, for looking at iOS with nobody there (boot/phone.py): one touch or key
 * command at a time. The USB monitor runs with its MMU off, so each side keeps the other in mind: the kernel owns the
 * first cache line and cleans it after writing, the monitor owns the second and the kernel invalidates it before reading.
 * Both sides check every field with these functions, which need only <stdint.h>, <stdbool.h>.
 *
 * There is no text command, by design: a tap is a tap, and nothing here is for typing anybody's password.
 *
 * SPDX-License-Identifier: MIT
 */
#ifndef _FUXI_REMOTE_H_
#define _FUXI_REMOTE_H_

#define FUXI_REMOTE_MAGIC	0x45544f4d45525846ull	/* little-endian FXREMOTE */
#define FUXI_REMOTE_VERSION	1u
#define FUXI_REMOTE_WIDTH	1080
#define FUXI_REMOTE_HEIGHT	2400
#define FUXI_REMOTE_MS_LEAST	20u
#define FUXI_REMOTE_FLICK_MOST	5000u
#define FUXI_REMOTE_KEY_MOST	2000u
#define FUXI_REMOTE_KEYS	3	/* power, volume up, volume down */

enum { FUXI_REMOTE_TAP = 1, FUXI_REMOTE_FLICK = 2, FUXI_REMOTE_KEY = 3 };

enum {
	FUXI_REMOTE_OK = 0,
	FUXI_REMOTE_OFF = 1,		/* no mailbox there: the kernel has no fuxiremote=1, or this is not one */
	FUXI_REMOTE_BUSY = 2,		/* the last command is not finished */
	FUXI_REMOTE_OUT_OF_ORDER = 3,	/* its number does not follow the last one */
	FUXI_REMOTE_BAD = 4,		/* a field out of bounds */
};

struct fuxi_remote_cmd {
	uint32_t seq;
	uint32_t kind;
	int32_t x0, y0, x1, y1;		/* a tap uses x0, y0; a key's number is x0 */
	uint32_t ms;			/* a flick's length, a key's hold */
	uint32_t check;
};

struct fuxi_remote_box {
	/* the kernel's cache line */
	uint64_t magic;
	uint32_t version;
	uint32_t done;			/* the last number finished, whether it was done or refused */
	uint32_t refused;
	uint32_t pad0[11];
	/* the monitor's cache line */
	struct fuxi_remote_cmd cmd;
	uint32_t pad1[8];
};

static inline uint32_t
fuxi_remote_check(const struct fuxi_remote_cmd *c)
{
	return (c->seq ^ c->kind ^ (uint32_t)c->x0 ^ ((uint32_t)c->y0 << 3) ^ ((uint32_t)c->x1 << 7) ^ ((uint32_t)c->y1 << 11) ^ (c->ms << 13) ^ 0x46585243u);
}

static inline bool
fuxi_remote_inside(int32_t x, int32_t y)
{
	return (x >= 0 && x < FUXI_REMOTE_WIDTH && y >= 0 && y < FUXI_REMOTE_HEIGHT);
}

/* Whether the fields can be acted on: the same answer on both sides. */
static inline bool
fuxi_remote_valid(const struct fuxi_remote_cmd *c)
{
	if (c->seq == 0 || c->check != fuxi_remote_check(c)) {
		return (false);
	}
	switch (c->kind) {
	case FUXI_REMOTE_TAP:
		return (fuxi_remote_inside(c->x0, c->y0));
	case FUXI_REMOTE_FLICK:
		return (fuxi_remote_inside(c->x0, c->y0) && fuxi_remote_inside(c->x1, c->y1) && c->ms >= FUXI_REMOTE_MS_LEAST && c->ms <= FUXI_REMOTE_FLICK_MOST);
	case FUXI_REMOTE_KEY:
		return (c->x0 >= 0 && c->x0 < FUXI_REMOTE_KEYS && c->ms >= FUXI_REMOTE_MS_LEAST && c->ms <= FUXI_REMOTE_KEY_MOST);
	default:
		return (false);
	}
}

/* The monitor's question before writing a command, from what it reads of the box. */
static inline uint32_t
fuxi_remote_may_post(uint64_t magic, uint32_t version, uint32_t done, uint32_t pending, const struct fuxi_remote_cmd *c)
{
	if (magic != FUXI_REMOTE_MAGIC || version != FUXI_REMOTE_VERSION) {
		return (FUXI_REMOTE_OFF);
	}
	if (pending != done) {
		return (FUXI_REMOTE_BUSY);
	}
	if (c->seq != done + 1) {
		return (FUXI_REMOTE_OUT_OF_ORDER);
	}
	return (fuxi_remote_valid(c) ? FUXI_REMOTE_OK : FUXI_REMOTE_BAD);
}

#endif
