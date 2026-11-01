#!/usr/bin/env python3
"""shim/fuxi-probe/au_glink.h against a fake DSP under the sanitizers: the handshake, wrap, malformed lengths, and no data."""
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]

DRIVER = r"""
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static unsigned kicks, wmbs;
#define AU_GL_RD(pa) (*(volatile uint32_t *)(uintptr_t)(pa))
#define AU_GL_WR(pa, v) (*(volatile uint32_t *)(uintptr_t)(pa) = (v))
#define AU_GL_WMB() (wmbs++)
#define AU_GL_KICK() (kicks++)
#include "au_glink.h"

#define FIFO 256u
static uint32_t desc[8], tx[FIFO / 4], rx[FIFO / 4], logs[AU_GL_REC_WORDS * 32];
static uint32_t far_head;	/* what the DSP has written to */
static uint32_t far_tail;	/* what the DSP has read of ours */

static void fresh(struct au_gl *g, uint32_t options)
{
	memset(g, 0, sizeof(*g)); memset(desc, 0x55, sizeof(desc)); memset(tx, 0xaa, sizeof(tx)); memset(rx, 0, sizeof(rx));
	desc[0] = 0; desc[3] = 0; far_head = 0; far_tail = 0; kicks = 0;
	g->desc = (uintptr_t)desc; g->tx_fifo = (uintptr_t)tx; g->tx_len = FIFO; g->options = options;
	g->log = logs; g->log_most = 32;
	g->rx_fifo = (uintptr_t)rx; g->rx_len = FIFO;
}
/* A message as the DSP writes it, wrapping like the real ring. */
static void say(uint32_t cmd, uint32_t p1, uint32_t p2, const void *extra, uint32_t bytes)
{
	uint32_t words[64] = {0};
	words[0] = cmd | p1 << 16; words[1] = p2;
	if (bytes != 0) memcpy(words + 2, extra, bytes);
	uint32_t total = 8 + ((bytes + 7) & ~7u);
	for (uint32_t i = 0; i < total / 4; i++) { rx[(far_head + 4 * i) % FIFO / 4] = words[i]; }
	far_head = (far_head + total) % FIFO;
	desc[3] = far_head;
}
/* What we sent, read as the DSP reads it. Returns its cmd, and its params. */
static bool heard(uint32_t *cmd, uint32_t *p1, uint32_t *p2, char *name)
{
	if (far_tail == desc[1]) return false;
	uint32_t w0 = tx[far_tail / 4], total = 8;
	*cmd = w0 & 0xffff; *p1 = w0 >> 16; *p2 = tx[(far_tail + 4) % FIFO / 4];
	if (*cmd == AU_GL_OPEN) {
		total += (*p2 + 7) & ~7u;
		if (name) for (uint32_t i = 0; i < *p2; i++) name[i] = ((char *)tx)[(far_tail + 8 + i) % FIFO];
	}
	assert(*cmd != AU_GL_TX_DATA && *cmd != AU_GL_TX_DATA_CONT);	/* never data, whatever it is asked */
	far_tail = (far_tail + total) % FIFO;
	desc[0] = far_tail;
	return true;
}

int main(void)
{
	struct au_gl g; uint32_t cmd, p1, p2; char name[64];

	/* Start: both of our ends reset, then the version, with a kick. */
	fresh(&g, 0);
	assert(au_gl_start(&g));
	assert(desc[2] == 0 && desc[1] == 8 && kicks == 1 && wmbs == 1);
	assert(heard(&cmd, &p1, &p2, 0) && cmd == AU_GL_VERSION && p1 == 1 && p2 == AU_GL_FEATURE_REUSE);
	assert(!au_gl_step(&g));

	/* Its version: answered. Its ack with our features: done, nothing sent. */
	say(AU_GL_VERSION, 1, 3, 0, 0);
	assert(au_gl_step(&g) && heard(&cmd, &p1, &p2, 0) && cmd == AU_GL_VERSION_ACK && p1 == 1 && p2 == 1 && g.features == 1);
	say(AU_GL_VERSION_ACK, 1, 1, 0, 0);
	assert(au_gl_step(&g) && g.version_acked && !heard(&cmd, &p1, &p2, 0));

	/* An ack with fewer features: asked again, bounded. */
	fresh(&g, 0); assert(au_gl_start(&g)); assert(heard(&cmd, &p1, &p2, 0));
	for (unsigned i = 0; i < AU_GL_RESENDS + 3; i++) {
		g.features = 1;
		say(AU_GL_VERSION_ACK, 1, 0, 0, 0);
		assert(au_gl_step(&g));
		if (i < AU_GL_RESENDS) { assert(heard(&cmd, &p1, &p2, 0) && cmd == AU_GL_VERSION && p2 == 0); }
		else { assert(!heard(&cmd, &p1, &p2, 0)); }
	}

	/* Without the option an opening is only noted: name logged, nothing sent, the ring moves past the padding. */
	fresh(&g, 0); assert(au_gl_start(&g)); assert(heard(&cmd, &p1, &p2, 0));
	say(AU_GL_OPEN, 7, 12, "adsp_apps\0\0", 12);
	assert(au_gl_step(&g) && !heard(&cmd, &p1, &p2, 0) && desc[2] == 24 && g.opened == 0);
	{
		uint32_t *r = logs + AU_GL_REC_WORDS;	/* record 0 is our version */
		assert((r[1] & 0xffff) == AU_GL_OPEN && r[1] >> 16 == 7 && r[2] == 12 && r[3] == 12 && !strcmp((char *)(r + 4), "adsp_apps"));
	}
	/* With it: its opening acknowledged, then ours under the same name with our id; its ack is counted. */
	fresh(&g, AU_GL_ANSWER); assert(au_gl_start(&g)); assert(heard(&cmd, &p1, &p2, 0));
	say(AU_GL_OPEN, 7, 8, "gprsvc\0", 8);
	assert(au_gl_step(&g));
	assert(heard(&cmd, &p1, &p2, 0) && cmd == AU_GL_OPEN_ACK && p1 == 7);
	assert(heard(&cmd, &p1, &p2, name) && cmd == AU_GL_OPEN && p1 == 1 && p2 == 8 && !strcmp(name, "gprsvc"));
	say(AU_GL_OPEN_ACK, 1, 0, 0, 0);
	assert(au_gl_step(&g) && g.acked_by_dsp == 1);
	/* The same channel opened again is not answered twice; an intent request is refused and data is only skipped. */
	say(AU_GL_OPEN, 7, 8, "gprsvc\0", 8);
	assert(au_gl_step(&g) && !heard(&cmd, &p1, &p2, 0));
	say(AU_GL_INTENT_REQ, 7, 512, 0, 0);
	assert(au_gl_step(&g) && heard(&cmd, &p1, &p2, 0) && cmd == AU_GL_INTENT_REQ_ACK && p1 == 1 && p2 == 0);
	say(AU_GL_INTENT_REQ, 99, 512, 0, 0);
	assert(au_gl_step(&g) && !heard(&cmd, &p1, &p2, 0));
	{
		uint32_t data[4] = {4, 0, 0x11223344, 0};	/* chunk size, left size, then bytes */
		say(AU_GL_TX_DATA, 7, 3, data, 12);
		uint32_t tail = desc[2];
		assert(au_gl_step(&g) && !heard(&cmd, &p1, &p2, 0) && desc[2] == (tail + 24) % FIFO);
	}
	say(AU_GL_CLOSE, 7, 0, 0, 0);
	assert(au_gl_step(&g) && heard(&cmd, &p1, &p2, 0) && cmd == AU_GL_CLOSE_ACK && p1 == 7);

	/* Intents, read notifications and the rest move on by the right length. */
	{
		uint32_t pairs[4] = {64, 1, 128, 2};
		say(AU_GL_INTENT, 7, 2, pairs, 16);
		uint32_t tail = desc[2];
		assert(au_gl_step(&g) && desc[2] == (tail + 24) % FIFO);
		unsigned before = kicks;
		say(AU_GL_READ_NOTIF, 0, 0, 0, 0);
		assert(au_gl_step(&g) && kicks == before + 1 && !heard(&cmd, &p1, &p2, 0));
	}

	/* The ring wraps both ways, many times over. */
	fresh(&g, AU_GL_ANSWER); assert(au_gl_start(&g)); assert(heard(&cmd, &p1, &p2, 0));
	for (unsigned i = 0; i < 200; i++) {
		g.opened = 0; g.log_n = 0;
		say(AU_GL_OPEN, 40, 11, "wrap-a-lot\0", 12);
		assert(au_gl_step(&g));
		assert(heard(&cmd, &p1, &p2, name) && cmd == AU_GL_OPEN_ACK);
		assert(heard(&cmd, &p1, &p2, name) && cmd == AU_GL_OPEN && !strcmp(name, "wrap-a-lot"));
		assert(!au_gl_step(&g));
	}

	/* Half a message waits. */
	fresh(&g, 0); assert(au_gl_start(&g));
	say(AU_GL_OPEN, 7, 12, "adsp_apps\0\0", 12);
	desc[3] = 8;
	assert(!au_gl_step(&g) && desc[2] == 0 && !g.stuck);
	desc[3] = 24;
	assert(au_gl_step(&g));

	/* Lengths and positions that cannot be true stop the reader and touch nothing. */
	{
		static const struct { uint32_t cmd, p2, stuck; } bad[] = {
			{AU_GL_OPEN, 0, 2}, {AU_GL_OPEN, FIFO, 2}, {AU_GL_OPEN, 0xffffffffu, 2}, {AU_GL_INTENT, 0x10000000u, 3}, {77, 0, 5},
		};
		for (unsigned i = 0; i < sizeof(bad) / sizeof(bad[0]); i++) {
			fresh(&g, AU_GL_ANSWER); assert(au_gl_start(&g)); assert(heard(&cmd, &p1, &p2, 0));
			say(bad[i].cmd, 1, bad[i].p2, 0, 0);
			assert(!au_gl_step(&g) && g.stuck == bad[i].stuck && !heard(&cmd, &p1, &p2, 0) && desc[2] == 0);
			assert(!au_gl_step(&g));
		}
		uint32_t huge[4] = {0x7fffffffu, 0, 0, 0};
		fresh(&g, 0); assert(au_gl_start(&g));
		say(AU_GL_TX_DATA, 1, 1, huge, 8);
		assert(!au_gl_step(&g) && g.stuck == 4);
		fresh(&g, 0); assert(au_gl_start(&g));
		desc[3] = FIFO + 8;
		assert(!au_gl_step(&g) && g.stuck == 1);
		fresh(&g, 0); assert(au_gl_start(&g));
		desc[3] = 6;
		assert(!au_gl_step(&g) && g.stuck == 1);
	}
	/* No room, or a tail that cannot be true: nothing is written. */
	fresh(&g, 0);
	desc[1] = 0; desc[0] = 16;
	assert(!au_gl_tx(&g, AU_GL_VERSION, 1, 1, 0, 0) && desc[1] == 0 && tx[0] == 0xaaaaaaaau && g.tx_full == 1);
	fresh(&g, 0);
	desc[1] = 0; desc[0] = FIFO + 4;
	assert(!au_gl_tx(&g, AU_GL_VERSION, 1, 1, 0, 0) && desc[1] == 0);

	/* The log is bounded and says how much it lost. */
	fresh(&g, 0); desc[1] = 0; desc[2] = 0; g.log_most = 2;
	for (unsigned i = 0; i < 5; i++) { say(AU_GL_SIGNALS, 1, i, 0, 0); assert(au_gl_step(&g)); }
	assert(g.log_n == 2 && g.log_lost == 3);

	/* Random rings, under the sanitizers: whatever comes, nothing is read or written past the ring. */
	{
		uint32_t seed = 0x51ed2701;
		for (unsigned round = 0; round < 40000; round++) {
			fresh(&g, round & 1); (void)au_gl_start(&g);
			for (unsigned i = 0; i < FIFO / 4; i++) { seed ^= seed << 13; seed ^= seed >> 17; seed ^= seed << 5; rx[i] = (i & 1) && (seed & 8) ? seed >> 24 : seed & 0x000f000f; }
			seed ^= seed << 13; seed ^= seed >> 17; seed ^= seed << 5;
			desc[3] = (seed & 0xf8) % FIFO;
			for (unsigned steps = 0; steps < 40 && au_gl_step(&g); steps++) { desc[0] = desc[1]; }
			assert(desc[2] < FIFO && desc[1] < FIFO);
			for (unsigned i = 0; i < FIFO / 4; i++) { (void)tx[i]; }
		}
	}
	puts("au_glink: handshake, opening and refusals, wrap, half messages, malformed input, no data ever");
	return 0;
}
"""


def main():
    with tempfile.TemporaryDirectory(prefix='fuxi-glink-') as tmp:
        source = Path(tmp) / 'glink.c'
        source.write_text(DRIVER)
        program = Path(tmp) / 'glink'
        subprocess.run([os.environ.get('CC', 'cc'), '-std=gnu11', '-Wall', '-Wextra', '-Wno-unused-function', '-Wno-unused-parameter', '-g', '-O1',
                        '-fsanitize=address,undefined', '-fno-sanitize-recover=all', f'-I{ROOT / "shim/fuxi-probe"}', str(source), '-o', str(program)], check=True)
        subprocess.run([str(program)], check=True)
    # What could put an audio command on the wire is simply not there.
    text = (ROOT / 'shim/fuxi-probe/au_glink.h').read_text()
    code = text[text.index('#define AU_GL_FEATURE_REUSE'):]
    assert 'AU_GL_TX_DATA,' not in code.replace('case AU_GL_TX_DATA: case AU_GL_TX_DATA_CONT:', '')
    assert 'au_gl_tx(g, AU_GL_TX_DATA' not in code
    audio = (ROOT / 'shim/fuxi-probe/audio.c').read_text()
    assert 'au_gl_tx(' not in audio.replace('au_gl_tx_', '')
    print('PASS: au_glink')


main()
