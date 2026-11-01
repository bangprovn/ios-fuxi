#!/usr/bin/env python3
"""shim/fuxi-probe/au_glink.h against a fake DSP under the sanitizers: the handshake, wrap, malformed lengths, no data unless asked, and then the one packet."""
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
#include "fuxi_audioreach.h"

#define FIFO 1024u
static uint32_t desc[8], tx[FIFO / 4], rx[FIFO / 4], logs[AU_GL_REC_WORDS * 32];
static uint32_t far_head;	/* what the DSP has written to */
static uint32_t far_tail;	/* what the DSP has read of ours */
static bool may_send;		/* heard() fails on data otherwise */
static bool may_vote;		/* and on a vote */
static bool may_graph;		/* and on a graph's packet */
static bool may_play;		/* and on a buffer */
static uint32_t asks, votes, graphs;

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
	if (*cmd == AU_GL_TX_DATA) {
		/* only the question, or the votes as xnu/fuxi-audio frames them, and only when asked */
		static const uint32_t ask[6] = {0x1860, 0x0302, 1, 1, 0, AU_GL_OP_GET_SPF_STATE};
		uint32_t word[17];
		assert(may_send);
		for (uint32_t i = 0; i < 17; i++) word[i] = tx[(far_tail + 8 + 4 * i) % FIFO / 4];
		assert(word[1] == 0);
		if (word[0] == 24) {
			for (uint32_t i = 0; i < 6; i++) assert(i == 4 ? word[2 + i] == asks + 1 : word[2 + i] == ask[i]);
			asks++;
			total += 32;
		} else {
			static const uint8_t block[4] = {FUXI_AR_HW_MACRO, FUXI_AR_HW_DCODEC, FUXI_AR_HW_DCODEC, FUXI_AR_HW_MACRO};
			uint8_t want[128];
			uint32_t step = word[6] - AU_GL_PRM_TOKEN, n;
			if (word[6] >= AU_GL_READ_TOKEN) {
				assert(may_play && word[0] <= 128);
				total += (8 + word[0] + 7) & ~7u;
			} else if (word[6] >= AU_GL_WRITE_TOKEN) {
				/* a buffer: the same shape as a graph's packet, its bytes by tools/fuxi-audio-host */
				assert(may_play && word[0] <= 128);
				total += (8 + word[0] + 7) & ~7u;
			} else if (word[6] >= AU_GL_GRAPH_TOKEN) {
				/* a graph's packet: checked by its size and order, its bytes by tools/fuxi-audio-host */
				assert(may_graph && word[6] == AU_GL_GRAPH_TOKEN + graphs && word[0] <= AU_GL_GPR_BUFFER);
				graphs++;
				total += (8 + word[0] + 7) & ~7u;
			} else {
				assert(may_vote && word[0] == 60 && step == votes);
				n = fuxi_ar_prm_vote(want, sizeof(want), 2, word[6], block[step], step < 2);
				assert(n == 60 && !memcmp(want, word + 2, 60));
				votes++;
				total += (8 + 60 + 7) & ~7u;
			}
		}
	} else if (*cmd == AU_GL_INTENT) {
		total += 8 * *p2;
	}
	assert(*cmd != AU_GL_TX_DATA_CONT);
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

	/* --gpr. Without its option nothing can be asked, even with a channel and a buffer there. */
	fresh(&g, AU_GL_ANSWER); assert(au_gl_start(&g)); assert(heard(&cmd, &p1, &p2, 0));
	g.gpr_lcid = 5; g.gpr_acked = 1; g.gpr_riid_n = 1;
	assert(!au_gl_gpr_ask(&g));
	/* Another channel's name is not adsp_apps. */
	fresh(&g, AU_GL_ANSWER | AU_GL_GPR); assert(au_gl_start(&g)); assert(heard(&cmd, &p1, &p2, 0));
	say(AU_GL_OPEN, 7, 10, "adsp_appz\0", 10);
	assert(au_gl_step(&g) && g.gpr_lcid == 0);
	assert(heard(&cmd, &p1, &p2, 0) && heard(&cmd, &p1, &p2, 0));
	say(AU_GL_OPEN_ACK, 1, 0, 0, 0);
	assert(au_gl_step(&g) && !g.gpr_acked && !heard(&cmd, &p1, &p2, 0) && !au_gl_gpr_ask(&g));
	/* The way it goes on the phone: lcid 2 for its rcid 9. */
	fresh(&g, AU_GL_ANSWER | AU_GL_GPR); may_send = true; asks = 0;
	assert(au_gl_start(&g)); assert(heard(&cmd, &p1, &p2, 0));
	g.opened = 1; g.rcid[0] = 3;
	say(AU_GL_OPEN, 9, 10, "adsp_apps\0", 10);
	assert(au_gl_step(&g) && g.gpr_lcid == 2 && g.gpr_rcid == 9);
	assert(heard(&cmd, &p1, &p2, 0) && cmd == AU_GL_OPEN_ACK && p1 == 9);
	assert(heard(&cmd, &p1, &p2, name) && cmd == AU_GL_OPEN && p1 == 2 && !strcmp(name, "adsp_apps"));
	assert(!au_gl_gpr_ask(&g));	/* not before its ack */
	say(AU_GL_OPEN_ACK, 2, 0, 0, 0);
	assert(au_gl_step(&g) && g.gpr_acked && g.gpr_offers == 2);
	assert(heard(&cmd, &p1, &p2, 0) && cmd == AU_GL_INTENT && p1 == 2 && p2 == 1);
	assert(heard(&cmd, &p1, &p2, 0) && cmd == AU_GL_INTENT && p1 == 2 && p2 == 1);
	assert(!au_gl_gpr_ask(&g));	/* not before a buffer of its own */
	{
		uint32_t small[2] = {16, 30}, other[2] = {128, 31}, good[2] = {128, 14};
		say(AU_GL_INTENT, 3, 1, other, 8);	/* another channel's */
		assert(au_gl_step(&g) && g.gpr_riid_n == 0);
		say(AU_GL_INTENT, 9, 1, small, 8);	/* too small for the packet */
		assert(au_gl_step(&g) && g.gpr_riid_n == 0);
		say(AU_GL_INTENT, 9, 1, good, 8);
		assert(au_gl_step(&g) && g.gpr_riid_n == 1);
	}
	assert(au_gl_gpr_ask(&g) && asks == 0 && heard(&cmd, &p1, &p2, 0) && cmd == AU_GL_TX_DATA && p1 == 2 && p2 == 14 && asks == 1);
	assert(!au_gl_gpr_ask(&g) && g.gpr_asked == 1);	/* out of its buffers */
	{
		/* Not up yet: answered, its buffer back with reuse, nothing ready. */
		uint32_t later[10] = {28, 0, 0x1c60, 0x0203, 1, 1, 1, AU_GL_OP_SPF_STATE, 0, 0};
		say(AU_GL_TX_DATA, 9, 1, later, 36);
		assert(au_gl_step(&g) && g.gpr_heard == 1 && !g.gpr_ready && g.gpr_first[6] == 0 && g.gpr_first_bytes == 28);
		assert(heard(&cmd, &p1, &p2, 0) && cmd == AU_GL_RX_DONE_REUSE && p1 == 2 && p2 == 1);
		/* A basic result is not a yes. */
		uint32_t refused[10] = {32, 0, 0x2060, 0x0203, 1, 1, 1, 0x02001005, AU_GL_OP_GET_SPF_STATE, 1};
		say(AU_GL_TX_DATA, 9, 1, refused, 40);
		assert(au_gl_step(&g) && g.gpr_heard == 2 && !g.gpr_ready && g.gpr_last[7] == 1);
		assert(heard(&cmd, &p1, &p2, 0) && cmd == AU_GL_RX_DONE_REUSE);
		/* Ready; the first answer is kept apart. */
		uint32_t yes[10] = {28, 0, 0x1c60, 0x0203, 1, 1, 2, AU_GL_OP_SPF_STATE, 1, 0};
		say(AU_GL_TX_DATA, 9, 1, yes, 36);
		assert(au_gl_step(&g) && g.gpr_heard == 3 && g.gpr_ready && g.gpr_first[6] == 0 && g.gpr_last[6] == 1);
		assert(heard(&cmd, &p1, &p2, 0) && cmd == AU_GL_RX_DONE_REUSE);
		/* Without reuse its buffer is gone, so another is offered. */
		g.features = 0;
		say(AU_GL_TX_DATA, 9, 7, yes, 36);
		assert(au_gl_step(&g));
		assert(heard(&cmd, &p1, &p2, 0) && cmd == AU_GL_RX_DONE && p1 == 2 && p2 == 7);
		assert(heard(&cmd, &p1, &p2, 0) && cmd == AU_GL_INTENT && p1 == 2);
		/* Data elsewhere is only counted. */
		say(AU_GL_TX_DATA, 3, 1, yes, 36);
		assert(au_gl_step(&g) && g.gpr_other == 1 && !heard(&cmd, &p1, &p2, 0));
		/* A request for a buffer on it: granted while small, refused when big, and offers end. */
		say(AU_GL_INTENT_REQ, 9, 64, 0, 0);
		assert(au_gl_step(&g));
		assert(heard(&cmd, &p1, &p2, 0) && cmd == AU_GL_INTENT);
		assert(heard(&cmd, &p1, &p2, 0) && cmd == AU_GL_INTENT_REQ_ACK && p1 == 2 && p2 == 1);
		say(AU_GL_INTENT_REQ, 9, AU_GL_GPR_BUFFER + 1, 0, 0);
		assert(au_gl_step(&g) && heard(&cmd, &p1, &p2, 0) && cmd == AU_GL_INTENT_REQ_ACK && p2 == 0);
		g.gpr_offers = AU_GL_GPR_OFFERS;
		say(AU_GL_INTENT_REQ, 9, 64, 0, 0);
		assert(au_gl_step(&g) && heard(&cmd, &p1, &p2, 0) && cmd == AU_GL_INTENT_REQ_ACK && p2 == 0);
	}
	/* Asks are bounded, whatever buffers it gives. */
	{
		uint32_t good[2] = {128, 14};
		g.gpr_asked = 0; asks = 0;
		for (unsigned i = 0; i < AU_GL_GPR_ASKS + 4; i++) {
			say(AU_GL_INTENT, 9, 1, good, 8);
			assert(au_gl_step(&g));
			bool asked = au_gl_gpr_ask(&g);
			assert(asked == (i < AU_GL_GPR_ASKS));
			if (asked) assert(heard(&cmd, &p1, &p2, 0) && cmd == AU_GL_TX_DATA);
		}
		assert(g.gpr_asked == AU_GL_GPR_ASKS && asks == AU_GL_GPR_ASKS);
	}
	/* --prm. Not without its option, not before ready, then the four in order, answered or not. */
	{
		uint32_t good[2] = {128, 14}, yes[10] = {28, 0, 0x1c60, 0x0203, 1, 1, 1, AU_GL_OP_SPF_STATE, 1, 0};
		uint32_t vote_on[11] = {32, 0, 0x2060, 0x0203, 2, 2, AU_GL_PRM_TOKEN, AU_GL_OP_PRM_REQUEST_DONE, 0x88, 0, 0};
		uint32_t basic[11] = {32, 0, 0x2060, 0x0203, 2, 2, AU_GL_PRM_TOKEN + 3, AU_GL_OP_BASIC_RESULT, AU_GL_OP_PRM_RELEASE, 7, 0};
		uint32_t stray[11] = {32, 0, 0x2060, 0x0203, 2, 2, AU_GL_PRM_TOKEN + 2, AU_GL_OP_PRM_RELEASE_DONE, 0, 9, 0};

		fresh(&g, AU_GL_ANSWER | AU_GL_GPR); may_send = may_vote = true; votes = 0; asks = 0;
		assert(au_gl_start(&g)); assert(heard(&cmd, &p1, &p2, 0));
		g.gpr_lcid = 1; g.gpr_rcid = 9; g.gpr_acked = 1; g.gpr_ready = 1; g.gpr_riid_n = 2;
		assert(!au_gl_prm_step(&g));	/* no option */
		g.options |= AU_GL_PRM; g.gpr_ready = 0;
		assert(!au_gl_prm_step(&g));	/* not ready */
		for (unsigned i = 0; i < 6; i++) { say(AU_GL_INTENT, 9, 1, good, 8); assert(au_gl_step(&g)); }
		assert(g.gpr_riid_n == 8);
		assert(au_gl_gpr_ask(&g) && heard(&cmd, &p1, &p2, 0) && asks == 1);
		say(AU_GL_TX_DATA, 9, 1, yes, 36);
		assert(au_gl_step(&g) && g.gpr_ready && heard(&cmd, &p1, &p2, 0) && cmd == AU_GL_RX_DONE_REUSE);
		g.ms = 100;
		assert(au_gl_prm_step(&g) && g.prm_sent == 1 && g.prm_status[0] == AU_GL_PRM_NO_ANSWER && g.prm_sent_ms[0] == 100);
		assert(heard(&cmd, &p1, &p2, 0) && cmd == AU_GL_TX_DATA && votes == 1);
		/* An answer to a step not sent yet counts for nothing. */
		say(AU_GL_TX_DATA, 9, 1, stray, 40);
		assert(au_gl_step(&g) && g.prm_status[2] == 0 && heard(&cmd, &p1, &p2, 0));
		g.ms = 101;
		say(AU_GL_TX_DATA, 9, 1, vote_on, 40);
		assert(au_gl_step(&g) && g.prm_status[0] == 0 && g.prm_heard_ms[0] == 101 && g.prm_opcode[0] == AU_GL_OP_PRM_REQUEST_DONE && heard(&cmd, &p1, &p2, 0));
		for (unsigned i = 1; i < AU_GL_PRM_STEPS; i++) {
			assert(au_gl_prm_step(&g) && heard(&cmd, &p1, &p2, 0) && votes == i + 1);
		}
		assert(!au_gl_prm_step(&g) && g.prm_sent == AU_GL_PRM_STEPS);	/* no more, whatever comes */
		assert(g.prm_status[1] == AU_GL_PRM_NO_ANSWER);			/* step 1 never answered: said so, and the rest went */
		say(AU_GL_TX_DATA, 9, 1, basic, 40);
		assert(au_gl_step(&g) && g.prm_status[3] == 7 && heard(&cmd, &p1, &p2, 0));
		may_vote = false;
	}
	/* --graph. A long buffer is asked for once, a refusal counted; packets go only into it, then only when it is back, and only three. */
	{
		static uint32_t m[2 + AU_GL_GPR_BUFFER / 4 + 2];
		uint32_t big[2] = {4096, 77}, small[2] = {128, 14};
		uint32_t answer[11] = {32, 0, 0x2060, 0x0203, 1, 1, AU_GL_GRAPH_TOKEN, AU_GL_OP_BASIC_RESULT, 0x01001000, 3, 0};

		fresh(&g, AU_GL_ANSWER | AU_GL_GPR | AU_GL_PRM); may_send = may_graph = true; graphs = 0; g.graph_steps = 3;
		assert(au_gl_start(&g)); assert(heard(&cmd, &p1, &p2, 0));
		g.gpr_lcid = 1; g.gpr_rcid = 9; g.gpr_acked = 1; g.gpr_ready = 1; g.big_have = 1; g.big_size = 4096;
		assert(!au_gl_big_ask(&g) && !au_gl_graph_send(&g, m, 64));	/* no option */
		/* The long buffer is kept out of the small ones without the option as well. */
		say(AU_GL_INTENT, 9, 1, big, 8);
		assert(au_gl_step(&g) && g.gpr_riid_n == 0);
		g.big_have = 0; g.big_size = 0;
		g.options |= AU_GL_GRAPH; g.gpr_acked = 0;
		assert(!au_gl_big_ask(&g));
		g.gpr_acked = 1;
		assert(au_gl_big_ask(&g) && heard(&cmd, &p1, &p2, 0) && cmd == AU_GL_INTENT_REQ && p1 == 1 && p2 == AU_GL_GPR_BUFFER);
		assert(!au_gl_big_ask(&g) && !heard(&cmd, &p1, &p2, 0));
		say(AU_GL_INTENT_REQ_ACK, 9, 0, 0, 0);
		assert(au_gl_step(&g) && g.big_refused == 1);
		say(AU_GL_INTENT, 9, 1, small, 8);
		assert(au_gl_step(&g) && !g.big_have && g.gpr_riid_n == 1);
		assert(!au_gl_graph_send(&g, m, 64));				/* its small one is not for this */
		say(AU_GL_INTENT_REQ_ACK, 9, 1, 0, 0);
		assert(au_gl_step(&g) && g.big_refused == 1);
		say(AU_GL_INTENT, 9, 1, big, 8);
		assert(au_gl_step(&g) && g.big_have && g.big_riid == 77 && !g.big_asked);
		assert(!au_gl_graph_send(&g, m, AU_GL_GPR_BUFFER + 4));		/* too long */
		for (uint32_t i = 0; i < 3; i++) {
			memset(m, 0x77, sizeof(m));
			m[6] = AU_GL_GRAPH_TOKEN + i;				/* GPR's token, word 4, after GLINK's two */
			assert(au_gl_graph_send(&g, m, i == 0 ? 500 : 60 + 4));
			assert(heard(&cmd, &p1, &p2, 0) && cmd == AU_GL_TX_DATA && p1 == 1 && p2 == 77 && graphs == i + 1);
			assert(!g.big_have && !au_gl_graph_send(&g, m, 64));	/* one to a buffer */
			if (i == 1) {
				/* An answer: its status kept by its token. */
				answer[6] = AU_GL_GRAPH_TOKEN; g.ms = 55;
				say(AU_GL_TX_DATA, 9, 1, answer, 40);
				assert(au_gl_step(&g) && g.graph_status[0] == 3 && g.graph_heard_ms[0] == 55 && g.graph_status[1] == AU_GL_PRM_NO_ANSWER && heard(&cmd, &p1, &p2, 0));
				/* Of a step not sent yet: nothing. */
				answer[6] = AU_GL_GRAPH_TOKEN + 2;
				say(AU_GL_TX_DATA, 9, 1, answer, 40);
				assert(au_gl_step(&g) && g.graph_status[2] == 0 && heard(&cmd, &p1, &p2, 0));
			}
			/* Another buffer's return changes nothing, its own does. */
			say(AU_GL_RX_DONE_REUSE, 9, 14, 0, 0);
			assert(au_gl_step(&g) && !g.big_have);
			say(AU_GL_RX_DONE_REUSE, 9, 77, 0, 0);
			assert(au_gl_step(&g) && g.big_have);
		}
		assert(!au_gl_graph_send(&g, m, 64) && g.graph_sent == 3);
		may_graph = false;

		/* --play: no buffer without its option or before the map's handle; a handle by its step's token; WRITE_DONE counted and its status kept. */
		{
			uint32_t mapped[9] = {28, 0, 0x1c60, 0x0203, 1, 1, AU_GL_GRAPH_TOKEN + 1, AU_GL_OP_MEM_MAP_DONE, 0x55};
			uint32_t played[13] = {40, 0, 0x2860, 0x0203, 0x6001, 1, AU_GL_WRITE_TOKEN, AU_GL_OP_WRITE_DONE, 0xd0000000u, 1, 0x55, 0, 0};
			uint32_t good[2] = {128, 14};

			fresh(&g, AU_GL_ANSWER | AU_GL_GPR | AU_GL_PRM | AU_GL_GRAPH); may_send = may_play = may_graph = true; graphs = 0; g.graph_steps = 15;
			assert(au_gl_start(&g)); assert(heard(&cmd, &p1, &p2, 0));
			g.gpr_lcid = 1; g.gpr_rcid = 9; g.gpr_acked = 1; g.gpr_ready = 1; g.big_size = 4096; g.big_riid = 77;
			say(AU_GL_INTENT, 9, 1, good, 8); assert(au_gl_step(&g) && g.gpr_riid_n == 1);
			g.map_handle = 0x55;
			memset(m, 0x77, sizeof(m)); m[6] = AU_GL_WRITE_TOKEN;
			assert(!au_gl_data_send(&g, m, 68));			/* no option */
			g.options |= AU_GL_PLAY; g.map_handle = 0;
			assert(!au_gl_data_send(&g, m, 68));			/* no map */
			/* Two steps: the second is the map. */
			g.big_have = 1; m[6] = AU_GL_GRAPH_TOKEN;
			assert(au_gl_graph_send(&g, m, 64) && heard(&cmd, &p1, &p2, 0));
			g.big_have = 1; m[6] = AU_GL_GRAPH_TOKEN + 1;
			assert(au_gl_graph_send(&g, m, 64) && heard(&cmd, &p1, &p2, 0));
			say(AU_GL_TX_DATA, 9, 1, mapped, 36);
			assert(au_gl_step(&g) && g.map_handle == 0x55 && g.graph_status[1] == 0 && heard(&cmd, &p1, &p2, 0));
			m[6] = AU_GL_WRITE_TOKEN;
			assert(!au_gl_data_send(&g, m, 200));			/* too long for its small ones */
			assert(au_gl_data_send(&g, m, 68) && g.write_sent == 1 && g.gpr_riid_n == 0 && heard(&cmd, &p1, &p2, 0) && cmd == AU_GL_TX_DATA && p2 == 14);
			assert(!au_gl_data_send(&g, m, 68));			/* none left */
			/* Its buffer back: the next can go. */
			say(AU_GL_RX_DONE_REUSE, 9, 14, 0, 0);
			assert(au_gl_step(&g) && g.gpr_riid_n == 1);
			g.ms = 500; say(AU_GL_TX_DATA, 9, 1, played, 48);
			assert(au_gl_step(&g) && g.write_done == 1 && g.write_first_ms == 500 && g.write_bad == 0 && heard(&cmd, &p1, &p2, 0));
			played[11] = 3; g.ms = 520; say(AU_GL_TX_DATA, 9, 1, played, 48);
			assert(au_gl_step(&g) && g.write_done == 2 && g.write_last_ms == 520 && g.write_first_ms == 500 && g.write_status == 3 && g.write_bad == 1 && heard(&cmd, &p1, &p2, 0));
			/* What comes back: one empty buffer sent, its answer counted; no answer without one. */
			{
				uint32_t back[13] = {44, 0, 0x2c60, 0x0203, 0x6024, 1, AU_GL_READ_TOKEN, AU_GL_OP_READ_DONE, 0, 0xd0010000u, 1, 0x55, 15360};
				memset(m, 0x77, sizeof(m)); m[6] = AU_GL_READ_TOKEN;
				say(AU_GL_RX_DONE_REUSE, 9, 14, 0, 0); assert(au_gl_step(&g));
				assert(au_gl_read_send(&g, m, 64) && g.read_sent == 1 && heard(&cmd, &p1, &p2, 0) && cmd == AU_GL_TX_DATA);
				say(AU_GL_TX_DATA, 9, 1, back, 52);
				assert(au_gl_step(&g) && g.read_done == 1 && g.read_bad == 0 && g.read_bytes == 15360 && heard(&cmd, &p1, &p2, 0));
			}
			may_play = false; may_graph = false;
		}
	}
	may_send = false;

	/* The log is bounded and says how much it lost. */
	fresh(&g, 0); desc[1] = 0; desc[2] = 0; g.log_most = 2;
	for (unsigned i = 0; i < 5; i++) { say(AU_GL_SIGNALS, 1, i, 0, 0); assert(au_gl_step(&g)); }
	assert(g.log_n == 2 && g.log_lost == 3);

	/* Random rings, under the sanitizers: whatever comes, nothing is read or written past the ring. */
	{
		uint32_t seed = 0x51ed2701;
		for (unsigned round = 0; round < 40000; round++) {
			/* (round & 3) == 3 is --gpr, with the channel up from the start for the rings that would never open it. */
			fresh(&g, (round & 1) | ((round & 3) == 3 ? AU_GL_GPR | AU_GL_PRM | AU_GL_GRAPH | AU_GL_PLAY : 0)); (void)au_gl_start(&g);
			if ((round & 3) == 3) { g.gpr_lcid = 1; g.gpr_rcid = round & 15; g.gpr_acked = round & 4; g.prm_sent = (round >> 4) & 7; g.graph_steps = 15; g.graph_sent = (round >> 7) & 15; }
			for (unsigned i = 0; i < FIFO / 4; i++) { seed ^= seed << 13; seed ^= seed >> 17; seed ^= seed << 5; rx[i] = (i & 1) && (seed & 8) ? seed >> 24 : seed & 0x000f000f; }
			seed ^= seed << 13; seed ^= seed >> 17; seed ^= seed << 5;
			desc[3] = (seed & 0xf8) % FIFO;
			for (unsigned steps = 0; steps < 40 && au_gl_step(&g); steps++) { desc[0] = desc[1]; }
			assert(desc[2] < FIFO && desc[1] < FIFO);
			for (unsigned i = 0; i < FIFO / 4; i++) { (void)tx[i]; }
		}
	}
	puts("au_glink: handshake, opening and refusals, wrap, half messages, malformed input, the question and the votes and only when asked");
	return 0;
}
"""


def main():
    with tempfile.TemporaryDirectory(prefix='fuxi-glink-') as tmp:
        source = Path(tmp) / 'glink.c'
        source.write_text(DRIVER)
        program = Path(tmp) / 'glink'
        subprocess.run([os.environ.get('CC', 'cc'), '-std=gnu11', '-Wall', '-Wextra', '-Wno-unused-function', '-Wno-unused-parameter', '-g', '-O1',
                        '-fsanitize=address,undefined', '-fno-sanitize-recover=all', f'-I{ROOT / "shim/fuxi-probe"}', f'-I{ROOT / "xnu/fuxi-audio"}', str(source),
                        str(ROOT / 'xnu/fuxi-audio/fuxi_audioreach.c'), str(ROOT / 'xnu/fuxi-audio/fuxi_gpr.c'), '-o', str(program)], check=True)
        subprocess.run([str(program)], check=True)
    # What could put any other audio command on the wire is simply not there: five TX_DATA, each in its own function. Starting comes only with --play,
    # whose samples are zeros, and nothing here touches a pin's mux or an amplifier: the TLMM's and the I2C's ranges do not appear.
    text = (ROOT / 'shim/fuxi-probe/au_glink.h').read_text()
    code = text[text.index('#define AU_GL_FEATURE_REUSE'):]
    assert code.count('AU_GL_TX_DATA,') == 5 and code.count('au_gl_tx(g, AU_GL_TX_DATA,') == 5
    ask = code[code.index('static inline bool au_gl_gpr_ask'):code.index('static inline void au_gl_gpr_take')]
    assert 'au_gl_tx(g, AU_GL_TX_DATA,' in ask and 'AU_GL_OP_GET_SPF_STATE' in ask
    assert 'AU_GL_TX_DATA_CONT,' not in code
    vote = code[code.index('static inline bool au_gl_prm_step'):code.index('/* An answer on adsp_apps')]
    assert 'au_gl_tx(g, AU_GL_TX_DATA,' in vote and 'gpr_ready' in vote and '0x08001032u' in vote
    audio = (ROOT / 'shim/fuxi-probe/audio.c').read_text()
    assert 'au_gl_tx(' not in audio.replace('au_gl_tx_', '')
    # The GLINK and graph code alone: au_amp.h's section reads the amplifiers with its own test, and nothing in here calls it.
    run = audio[audio.index('#include "au_glink.h"'):audio.index("/* ---- the amplifiers' identities")]
    assert audio.count('GRAPH_START') == 4 and audio.count('fuxi_ar_map') == 1 and audio.count('fuxi_ar_write') == 2
    assert 'au_wr(AU_PLAY_PA + 4 * i, 0);' in run
    for word in ('i2s1', '0xf100000', 'geni', 'cs35l41', 'au_se0', 'au_qup2'):
        assert word not in run.lower(), word
    print('PASS: au_glink')


main()
