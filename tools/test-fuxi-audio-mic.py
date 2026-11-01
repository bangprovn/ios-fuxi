#!/usr/bin/env python3
"""shim/fuxi-probe/au_mic.h against a fake LPASS, SoundWire master and WCD938x under the sanitizers: each proof step stops the run where it fails, it comes back down, and what it may touch is held to the source."""
import importlib.util
import os
from pathlib import Path
import re
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

/* What may be reached at all: anything else, amplifiers, TDM, the RX master, is an assert. */
#define VA 0x06d44000ull
#define TX 0x06ae0000ull
#define SWR 0x06d30000ull
#define LPI 0x06e80000ull
#define SLEW 0x0725a000ull
#define PAD 0x0f16c000ull

static uint32_t va[0x400], txm[0x400], lpi[4], slew = 0x10000, pad[2] = {0x10, 0};
static uint32_t swr_regs[0x5100 / 4];
static uint8_t codec[0x600];		/* 0x3000.. */
static uint8_t dp[0x40];			/* 0x100.., its first data port */
static uint8_t scp[6] = {0x23, 0x02, 0x17, 0x01, 0x0d, 0x00};
static unsigned delays, votes_ok, writes_codec, reads_codec;
static uint32_t rx_fifo[8], rx_n;
static bool rail_on, attached, frames, reset_was_low;
static unsigned vote_log[8][2], votes;
static uint64_t abort_rd, abort_wr;	/* an address that aborts */
static bool abort_rd_on, abort_wr_on;
static uint32_t cmddb = 0x40a00, rpmh_fail_at = 99;
static int no_frames, no_attach, read_silent, stale_id, wrong_version, gate_stuck, pad_stuck, lpi_stuck, sticky_reg, clash;
static uint32_t attach_after, polled_attach;
static unsigned codec_writes_seen;
static uint16_t codec_write_log[256];

static bool pin_index(uint64_t pa, unsigned *i)
{
	static const unsigned pins[4] = {0, 1, 2, 14};

	for (unsigned k = 0; k < 4; k++) {
		if (pa == LPI + 0x1000u * pins[k]) { *i = k; return true; }
	}
	return false;
}

static uint32_t *slot(uint64_t pa)
{
	unsigned i;

	if (pa >= VA && pa < VA + 0x1000) return &va[(pa - VA) / 4];
	if (pa >= TX && pa < TX + 0x1000) { assert(va[0x8 / 4] & 1); return &txm[(pa - TX) / 4]; }	/* behind the VA's gate */
	if (pin_index(pa, &i)) return &lpi[i];
	if (pa == SLEW) return &slew;
	if (pa == PAD) return &pad[0];
	if (pa == PAD + 4) return &pad[1];
	return 0;
}

static uint32_t fake_rd(uint64_t pa, uint32_t *faulted)
{
	*faulted = 0;
	assert(votes_ok);	/* every read of LPASS and the pad is behind the votes */
	if (abort_rd_on && pa == abort_rd) { *faulted = 1; return 0xdeadfa17u; }
	if (pa >= SWR && pa < SWR + 0x10000) {
		uint32_t off = (uint32_t)(pa - SWR);

		/* As Linux and sheng: nothing of the master before both macros' gates. */
		assert((va[0x8 / 4] & 1) && (txm[0x8 / 4] & 1) && (txm[0] & 1));
		if (off == 0x00) return wrong_version ? 0x01070000u : 0x02000000u;
		if (off == 0x5064) return frames && !no_frames;
		if (off == 0x5050) return rx_n << 16;
		if (off == 0x5040) { assert(rx_n > 0); return rx_fifo[--rx_n]; }
		if (off == 0x1090) {
			if (attached && !no_attach && polled_attach++ >= attach_after) return 1u << 2;
			return 0;
		}
		if (off == 0x5000) return (uint32_t)(attached << 1) | (clash ? 1u << 3 : 0u);
		if (off == 0x538) return 0x170223u;
		if (off == 0x53c) return 0x10d01u;
		assert(off / 4 < sizeof(swr_regs) / 4);
		return swr_regs[off / 4];
	}
	uint32_t *p = slot(pa);

	assert(p != 0);
	return *p;
}

static int fake_wr(uint64_t pa, uint32_t v)
{
	assert(votes_ok);
	if (abort_wr_on && pa == abort_wr) return 1;
	if (pa >= SWR && pa < SWR + 0x10000) {
		uint32_t off = (uint32_t)(pa - SWR);

		assert((va[0x8 / 4] & 1) && (txm[0x8 / 4] & 1));
		if (off == 0x5020) {
			uint32_t reg = v & 0xffff, data = v >> 24;

			assert(((v >> 20) & 0xf) == 1);
			/* The fake's own allow-list, so that the source's cannot grow unseen: no efuse, no HPH, MICB1 alone, VOUT at most 0x23. */
			static const uint16_t ok[] = {0x3001, 0x300e, 0x300f, 0x3022, 0x3029, 0x306b, 0x306e, 0x3071, 0x3074, 0x308f, 0x3103, 0x312a, 0x31df, 0x31e1,
						      0x3408, 0x3409, 0x3410, 0x3417, 0x3457, 0x345b, 0x34d0, 0x34d1, 0x34d2, 0x34d3, 0x34d4,
						      0x102, 0x103, 0x105, 0x120, 0x122, 0x123, 0x124, 0x125, 0x126, 0x128, 0x130, 0x132, 0x133, 0x134, 0x135, 0x136, 0x138};
			bool found = false;

			for (unsigned i = 0; i < sizeof(ok) / sizeof(ok[0]); i++) found = found || ok[i] == reg;
			assert(found);
			if (reg == 0x3022) assert((data & 0x3f) <= 0x23);
			if (codec_writes_seen < 256) codec_write_log[codec_writes_seen] = (uint16_t)reg;
			codec_writes_seen++;
			writes_codec++;
			if (reg < 0x140) { dp[reg - 0x100] = (uint8_t)data; return 0; }
			if ((int)reg != sticky_reg) codec[reg - 0x3000] = (uint8_t)data;
			return 0;
		}
		if (off == 0x5024) {
			uint32_t reg = v & 0xffff, id = (v >> 16) & 0xf;

			assert(((v >> 20) & 0xf) == 1 && (v >> 24) == 1);
			reads_codec++;
			if (read_silent) return 0;
			uint32_t value = reg >= 0x50 && reg <= 0x55 ? scp[reg - 0x50] : reg < 0x140 ? dp[reg - 0x100] : codec[reg - 0x3000];

			rx_fifo[0] = (stale_id ? (id + 1) & 0xf : id) << 8 | value;
			rx_n = 1;
			return 0;
		}
		if (off == 0x308) { rx_n = 0; return 0; }
		if (off == 0x5060 && v == 1) frames = true;
		if (off == 0x008) { frames = false; attached = false; }
		swr_regs[off / 4] = v;
		if (frames && (swr_regs[0x004 / 4] & 1) && rail_on && (pad[1] & 2)) attached = true;
		return 0;
	}
	uint32_t *p = slot(pa);

	assert(p != 0);
	if (pa == PAD + 4 && (v & 2) == 0) reset_was_low = true;
	if (pa == PAD + 4 && (v & 2) != 0) assert(reset_was_low);	/* a pulse, and its pad already output when it goes high */
	if (pad_stuck && pa == PAD) return 0;
	if (lpi_stuck && pa >= LPI && pa < LPI + 0x10000) return 0;
	if (gate_stuck == 1 && pa == TX + 8) return 0;
	if (gate_stuck == 2 && pa == VA + 8) return 0;
	if (gate_stuck == 3 && pa == TX + 0x90) return 0;
	*p = v;
	return 0;
}

static uint32_t fake_cmddb(const char *name) { assert(strcmp(name, "ldob15") == 0); return cmddb; }
static uint32_t fake_rpmh(uint32_t addr, uint32_t data)
{
	assert(votes < 8);
	if (votes == rpmh_fail_at) return 2;
	/* L15B alone, and on alone: no other address, no zero, BOB1 nowhere. */
	assert((addr == 0x40a08 && data == 7) || (addr == 0x40a00 && data == 1800) || (addr == 0x40a04 && data == 1));
	vote_log[votes][0] = addr; vote_log[votes][1] = data; votes++;
	if (addr == 0x40a04) rail_on = true;
	return 0;
}

#define AU_MIC_RD(pa, f) fake_rd((pa), (f))
#define AU_MIC_WR(pa, v) fake_wr((pa), (v))
#define AU_MIC_DELAY_US(us) (delays += (us))
#define AU_MIC_CMDDB(name) fake_cmddb(name)
#define AU_MIC_RPMH(addr, data) fake_rpmh((addr), (data))
#define AU_GL_RD(pa) 0u
#define AU_GL_WR(pa, v) ((void)(v))
#define AU_GL_WMB()
#define AU_GL_KICK()
#include "au_glink.h"
#include "fuxi_audioreach.h"
#include "au_mic.h"

static struct au_mic m;
static uint32_t rep[128];
static struct au_gl g;

static void fresh(void)
{
	memset(dp, 0, sizeof(dp)); memset(va, 0, sizeof(va)); memset(txm, 0, sizeof(txm)); memset(swr_regs, 0, sizeof(swr_regs)); memset(codec, 0, sizeof(codec));
	for (unsigned i = 0; i < 4; i++) lpi[i] = 0x3c + i;
	slew = 0x10000; pad[0] = 0x10; pad[1] = 0;
	codec[0x3403 - 0x3000] = 0x0d; codec[0x3404 - 0x3000] = 0x01; codec[0x3022 - 0x3000] = 0x10; codec[0x3029 - 0x3000] = 0x55; codec[0x3103 - 0x3000] = 0x16;
	codec[0x34b0 - 0x3000] = 5u << 1; codec[0x300e - 0x3000] = 0x20;
	scp[0] = 0x23; scp[3] = 0x01; scp[4] = 0x0d;
	votes_ok = 1; delays = 0; writes_codec = reads_codec = 0; rx_n = 0; rail_on = attached = frames = reset_was_low = false; votes = 0;
	abort_rd_on = abort_wr_on = false; cmddb = 0x40a00; rpmh_fail_at = 99;
	no_frames = no_attach = read_silent = stale_id = wrong_version = gate_stuck = pad_stuck = lpi_stuck = clash = 0; sticky_reg = -1;
	attach_after = 0; polled_attach = 0; codec_writes_seen = 0;
	memset(&g, 0, sizeof(g));
	au_mic_begin(&m, rep);
	m.votes_held = m.clock_held = true;
}

/* Whatever it reached, nothing stays: bias off, codec back as found, gates off, pads as found. L15B stays on; never a vote off. */
static void assert_down(void)
{
	assert(m.going_down);
	if (m.did & AU_MIC_DID_RESET) { assert(pad[0] == 0x10 && pad[1] == 0); }
	if (m.did & AU_MIC_DID_TX) { assert((txm[0x8 / 4] & 1) == 0 && (txm[0] & 1) == 0 && (txm[4 / 4] & 1) == 0); }
	if (m.did & AU_MIC_DID_GATES && !gate_stuck) { assert((va[0x8 / 4] & 1) == 0 && (va[0] & 1) == 0 && (va[4 / 4] & 1) == 0 && (va[0x80 / 4] & 2) == 0); }
	if (m.did & AU_MIC_DID_PADS) { for (unsigned i = 0; i < 4; i++) assert(lpi[i] == 0x3c + i); assert(slew == 0x10000); }
	for (unsigned i = 0; i < votes; i++) assert(vote_log[i][1] != 0);
}

static void stops(uint32_t passed, uint32_t step, uint32_t why)
{
	if (rep[AU_MIC_R_PASSED] != passed || rep[AU_MIC_R_STEP] != step || rep[AU_MIC_R_WHY] != why)
		fprintf(stderr, "got passed %u step %u why %u, wanted %u %u %u\n", rep[AU_MIC_R_PASSED], rep[AU_MIC_R_STEP], rep[AU_MIC_R_WHY], passed, step, why);
	assert(rep[AU_MIC_R_PASSED] == passed && rep[AU_MIC_R_STEP] == step && rep[AU_MIC_R_WHY] == why);
	assert_down();
}

static void print_report(const char *name)
{
	printf("REPORT %s", name);
	for (unsigned i = 0; i < 128; i++) printf(" %u", rep[i]);
	printf("\n");
}

int main(void)
{
	/* Everything goes right: all seven, in order; the bias at 2.75 V and on during, off after; 25 read back. */
	fresh();
	au_mic_run(&m);
	stops(7, 0, 0);
	assert(votes == 3 && vote_log[0][0] == 0x40a08 && vote_log[1][0] == 0x40a00 && vote_log[2][0] == 0x40a04);
	assert(rep[AU_MIC_R_VERSION] == 0x02000000u && rep[AU_MIC_R_SLAVES + 2] == 1 && (rep[AU_MIC_R_LINK] & 1));
	assert(rep[AU_MIC_R_SCP] == 0x23 && rep[AU_MIC_R_SCP + 4] == 0x0d && rep[AU_MIC_R_CHIP + 2] == 0x0d && rep[AU_MIC_R_BACK_N] == 25);
	assert(rep[AU_MIC_R_BACK + 17] == 0x63 && rep[AU_MIC_R_DOWN] == 0x23 && (codec[0x3022 - 0x3000] & 0xc0) == 0);
	assert(rep[AU_MIC_R_LPI + 4] == 0x44 && rep[AU_MIC_R_LPI + 5] == 0x46 && rep[AU_MIC_R_LPI + 7] == 0x46 && (rep[AU_MIC_R_SLEW + 1] & 0xff) == 0x55 && rep[AU_MIC_R_DIFFER] == 0);
	/* Read before written, always: every write of the list is a read and a write. Reset low before high, by the fake. */
	assert(writes_codec == sizeof(au_mic_writes) / sizeof(au_mic_writes[0]) + 1 && delays > 40000);
	/* The master's soft reset, and the interrupt enable off, on the way down. */
	assert(swr_regs[0x5004 / 4] == 0 && swr_regs[0x008 / 4] == 1);
	print_report("all");

	/* Slow to attach: polled, and no more. */
	fresh(); attach_after = 300; au_mic_run(&m); stops(7, 0, 0); assert(rep[AU_MIC_R_SLAVES + 1] == 300);

	/* No votes, or no clock: nothing touched at all. */
	fresh(); votes_ok = 0; m.votes_held = false; au_mic_run(&m); assert(rep[AU_MIC_R_STEP] == 1 && rep[AU_MIC_R_PASSED] == 0 && m.did == 0);
	fresh(); votes_ok = 0; m.clock_held = false; au_mic_run(&m); assert(rep[AU_MIC_R_STEP] == 1 && rep[AU_MIC_R_PASSED] == 0 && m.did == 0);
	votes_ok = 1;

	/* Step 2: the first read of LPASS aborts. Nothing voted, nothing written. */
	fresh(); abort_rd = VA + 0xc0; abort_rd_on = true; au_mic_run(&m); stops(1, 2, AU_MIC_WHY_ABORT);
	assert(votes == 0 && m.did == 0 && rep[AU_MIC_R_FAULT_PA] == (uint32_t)(VA + 0xc0));
	print_report("abort");

	/* Step 3, each in turn. cmd-db: no vote at all. */
	fresh(); cmddb = 0x40a10; au_mic_run(&m); stops(2, 3, AU_MIC_WHY_CMDDB); assert(votes == 0 && m.did == 0 && pad[1] == 0);
	fresh(); cmddb = 0; au_mic_run(&m); stops(2, 3, AU_MIC_WHY_CMDDB); assert(votes == 0);
	fresh(); rpmh_fail_at = 2; au_mic_run(&m); stops(2, 3, AU_MIC_WHY_RPMH); assert(votes == 2 && !rail_on && m.did == 0);
	fresh(); abort_rd = PAD; abort_rd_on = true; au_mic_run(&m); stops(2, 3, AU_MIC_WHY_ABORT); assert(m.did == 0);
	fresh(); pad_stuck = 1; au_mic_run(&m); assert(rep[AU_MIC_R_PASSED] == 2 && rep[AU_MIC_R_STEP] == 3 && rep[AU_MIC_R_WHY] == AU_MIC_WHY_RESET); assert((m.did & AU_MIC_DID_PADS) == 0);
	fresh(); abort_wr = PAD; abort_wr_on = true; au_mic_run(&m); assert(rep[AU_MIC_R_PASSED] == 2 && rep[AU_MIC_R_STEP] == 3 && rep[AU_MIC_R_WHY] == AU_MIC_WHY_ABORT && (m.did & AU_MIC_DID_PADS) == 0);
	fresh(); lpi_stuck = 1; au_mic_run(&m); assert(rep[AU_MIC_R_PASSED] == 2 && rep[AU_MIC_R_STEP] == 3 && rep[AU_MIC_R_WHY] == AU_MIC_WHY_PADS && (m.did & AU_MIC_DID_GATES) == 0 && pad[0] == 0x10);
	fresh(); abort_wr = LPI; abort_wr_on = true; au_mic_run(&m); assert(rep[AU_MIC_R_PASSED] == 2 && rep[AU_MIC_R_STEP] == 3 && rep[AU_MIC_R_WHY] == AU_MIC_WHY_ABORT && (m.did & AU_MIC_DID_GATES) == 0);
	fresh(); abort_wr = VA + 8; abort_wr_on = true; au_mic_run(&m); assert(rep[AU_MIC_R_PASSED] == 2 && rep[AU_MIC_R_STEP] == 3 && rep[AU_MIC_R_WHY] == AU_MIC_WHY_ABORT && (m.did & AU_MIC_DID_MASTER) == 0);
	/* A macro's clock that does not stay. The VA's: the TX macro is never touched. */
	fresh(); gate_stuck = 1; au_mic_run(&m); assert(rep[AU_MIC_R_PASSED] == 2 && rep[AU_MIC_R_STEP] == 3 && rep[AU_MIC_R_WHY] == AU_MIC_WHY_GATES && (m.did & AU_MIC_DID_MASTER) == 0 && (txm[0] & 1) == 0);
	fresh(); gate_stuck = 2; au_mic_run(&m); assert(rep[AU_MIC_R_PASSED] == 2 && rep[AU_MIC_R_STEP] == 3 && rep[AU_MIC_R_WHY] == AU_MIC_WHY_GATES && (m.did & AU_MIC_DID_TX) == 0 && txm[0] == 0 && txm[0x90 / 4] == 0);
	/* TX +0x90 is only reported, as a-mic1 read it. */
	fresh(); gate_stuck = 3; au_mic_run(&m); assert(rep[AU_MIC_R_STEP] == 0 && rep[AU_MIC_R_PASSED] == 7 && rep[AU_MIC_R_GATES + 7] == 0 && rep[AU_MIC_R_VERSION] == 0x02000000u);
	/* The master's version comes behind the gates; another version stops before it is started. */
	fresh(); wrong_version = 1; au_mic_run(&m); stops(2, 3, AU_MIC_WHY_VERSION); assert((m.did & AU_MIC_DID_MASTER) == 0 && !frames);
	fresh(); abort_rd = SWR; abort_rd_on = true; au_mic_run(&m); stops(2, 3, AU_MIC_WHY_ABORT); assert((m.did & AU_MIC_DID_MASTER) == 0);

	/* Step 4: no frames in half a second. The codec is never read. */
	fresh(); no_frames = 1; au_mic_run(&m); stops(3, 4, AU_MIC_WHY_NO_FRAMES); assert(reads_codec == 0 && rep[AU_MIC_R_LINK + 1] == 500);
	print_report("frames");
	fresh(); abort_wr = SWR + 0x101c; abort_wr_on = true; au_mic_run(&m); stops(3, 4, AU_MIC_WHY_ABORT);

	/* Step 5: nobody for a second; or somebody else. Nothing written to it. */
	fresh(); no_attach = 1; au_mic_run(&m); stops(4, 5, AU_MIC_WHY_NO_DEVICE); assert(rep[AU_MIC_R_SLAVES + 1] == 1000 && writes_codec == 0);
	fresh(); scp[3] = 0x02; au_mic_run(&m); stops(4, 5, AU_MIC_WHY_NOT_THE_CODEC); assert(writes_codec == 0);
	fresh(); read_silent = 1; au_mic_run(&m); stops(4, 5, AU_MIC_WHY_READ); assert(writes_codec == 0 && rep[AU_MIC_R_READ_FAILS] >= 1);
	fresh(); stale_id = 1; au_mic_run(&m); stops(4, 5, AU_MIC_WHY_READ); assert(writes_codec == 0);
	/* A different unique id on this manufacturer and part is reported, and goes on. */
	fresh(); scp[0] = 0x24; au_mic_run(&m); stops(7, 0, 0); assert(rep[AU_MIC_R_SCP] == 0x24);

	/* Step 6: the wrong chip id stops it; another table value is only reported. */
	fresh(); codec[0x3403 - 0x3000] = 0x0e; au_mic_run(&m); stops(5, 6, AU_MIC_WHY_CHIP); assert(writes_codec == 0);
	fresh(); codec[0x3029 - 0x3000] = 0x56; codec[0x34b0 - 0x3000] = 3u << 1; au_mic_run(&m); stops(7, 0, 0); assert(rep[AU_MIC_R_DIFFER] == (2u | 8u));

	/* Step 7: one register does not take: all 25 are still read, the first wrong named. */
	fresh(); sticky_reg = 0x300e; au_mic_run(&m); stops(6, 7, AU_MIC_WHY_READBACK);
	assert(rep[AU_MIC_R_BAD] == 0x300e && rep[AU_MIC_R_BAD + 1] == 8 && rep[AU_MIC_R_BAD + 2] == 0x20 && rep[AU_MIC_R_BACK_N] == 25);
	print_report("readback");
	/* The bus complained during them. */
	fresh(); clash = 1; au_mic_run(&m); stops(6, 7, AU_MIC_WHY_BUS);
	/* An abort on the command FIFO: stops at once, and no write goes after it. */
	fresh(); abort_wr = SWR + 0x5020; abort_wr_on = true; au_mic_run(&m); stops(6, 7, AU_MIC_WHY_ABORT); assert(writes_codec == 0 && rep[AU_MIC_R_FAULTS] >= 1);
	/* A fault on the way down is counted, and the rest of the way is still gone. */
	fresh(); au_mic_run(&m);
	fresh(); m.did = AU_MIC_DID_GATES | AU_MIC_DID_TX | AU_MIC_DID_PADS; abort_wr = VA + 8; abort_wr_on = true; va[0] = 1; va[0x8 / 4] = 1; txm[0] = 1; au_mic_down(&m); assert(rep[AU_MIC_R_DOWN_FAULTS] == 1 && (txm[0] & 1) == 0 && (va[0] & 1) == 0);

	/* The one way to the codec, over all 65536 addresses: only the list, and MICB1's VOUT at most 0x23 whatever is asked. */
	fresh(); au_mic_run(&m);
	fresh();
	va[0x8 / 4] = 1; txm[0x8 / 4] = 1; txm[0] = 1; m.dev = 1;
	{
		unsigned through = 0;

		for (uint32_t reg = 0; reg < 0x10000; reg++) {
			if (au_mic_codec_write(&m, reg, 0x05)) through++;
		}
		assert(through == sizeof(au_mic_allowed) / sizeof(au_mic_allowed[0]) && writes_codec == through);
		for (uint32_t v = 0; v < 0x100; v++) {
			bool ok = au_mic_codec_write(&m, AU_MIC_MICB1, v);

			assert(ok == ((v & 0x3f) <= 0x23));
		}
		assert(!au_mic_codec_write(&m, 0x3001, 0x100));
	}

	/* The list on the PRM. One long-buffer packet each: from port 2, the clock's id, tries in order, then letting go; nothing past. */
	{
		static uint32_t p[2 + AU_GL_GPR_BUFFER / 4 + 2];
		uint32_t n = au_mic_build(0, p, AU_GL_GPR_BUFFER);
		uint8_t *b = (uint8_t *)(p + 2);
		uint32_t w[32];

		memset(w, 0, sizeof(w)); memcpy(w, b, n);
		assert(n > 24 && w[2] == 2 && w[3] == 2 && w[4] == AU_GL_GRAPH_TOKEN && w[5] == 0x0100100Fu);
		bool saw = false;

		for (unsigned i = 6; i + 3 < n / 4; i++) saw = saw || (w[i] == 0x30c && w[i + 1] == 19200000u && w[i + 2] == 1);
		assert(saw);
		n = au_mic_build(2, p, AU_GL_GPR_BUFFER); memcpy(w, b, n); saw = false;
		for (unsigned i = 6; i + 3 < n / 4; i++) saw = saw || (w[i] == 0x30c && w[i + 1] == 19200000u && w[i + 2] == 0);
		assert(saw && w[4] == AU_GL_GRAPH_TOKEN + 2);
		memset(w, 0, sizeof(w)); n = au_mic_build(3, p, AU_GL_GPR_BUFFER); memcpy(w, b, n);
		assert(w[5] == 0x01001010u && w[4] == AU_GL_GRAPH_TOKEN + 3);
		assert(au_mic_build(4, p, AU_GL_GPR_BUFFER) == 0);
	}

	/* The turn: votes refused, the stage never runs and nothing is asked. */
	fresh(); au_mic_begin(&m, rep); votes_ok = 0; g.graph_steps = AU_MIC_STEPS; g.prm_status[0] = 0; g.prm_status[1] = 3;
	assert(!au_mic_turn(&g, &m) && g.graph_sent == AU_MIC_STEPS && rep[AU_MIC_R_STEP] == 1 && rep[AU_MIC_R_WHY] == AU_MIC_WHY_VOTES && rep[AU_MIC_R_VOTE + 1] == 3);
	print_report("votes");
	/* The clock refused three times: tries in order, stage never runs, nothing to let go. */
	fresh(); au_mic_begin(&m, rep); votes_ok = 0;
	assert(!au_mic_turn(&g, &m));			/* the first try goes */
	g.graph_sent = 1; g.graph_status[0] = AU_GL_PRM_NO_ANSWER; g.ms = 100;
	assert(au_mic_turn(&g, &m));			/* it waits */
	g.graph_status[0] = 1;
	assert(!au_mic_turn(&g, &m) && g.graph_sent == 1);	/* the next goes */
	g.graph_sent = 2; g.graph_status[1] = 1;
	assert(!au_mic_turn(&g, &m) && g.graph_sent == 2);
	g.graph_sent = 3; g.graph_status[2] = 1;
	assert(!au_mic_turn(&g, &m) && g.graph_sent == AU_MIC_STEPS && m.over && rep[AU_MIC_R_STEP] == 1 && rep[AU_MIC_R_WHY] == AU_MIC_WHY_REFUSED && m.did == 0);
	assert(rep[AU_MIC_R_TRIES] == 1 && rep[AU_MIC_R_TRIES + 2] == 1 && rep[AU_MIC_R_TAKEN] == 0);
	print_report("refused");
	/* No answer in five seconds: the stage never runs, and the clock is let go in case. */
	fresh(); au_mic_begin(&m, rep); votes_ok = 0;
	g.graph_sent = 1; g.graph_status[0] = AU_GL_PRM_NO_ANSWER; g.ms = 1000;
	assert(au_mic_turn(&g, &m));
	g.ms = 5999; assert(au_mic_turn(&g, &m));
	g.ms = 6000; assert(!au_mic_turn(&g, &m) && g.graph_sent == AU_MIC_TRIES && m.over && rep[AU_MIC_R_WHY] == AU_MIC_WHY_NO_ANSWER && m.did == 0);
	/* The second try taken: the stage runs behind it, whole, and the list then lets go. Once. */
	fresh(); au_mic_begin(&m, rep);
	g.graph_sent = 1; g.graph_status[0] = 1; assert(!au_mic_turn(&g, &m));
	g.graph_sent = 2; g.graph_status[1] = 0;
	assert(!au_mic_turn(&g, &m) && m.over && g.graph_sent == AU_MIC_TRIES && rep[AU_MIC_R_TAKEN] == 2 && rep[AU_MIC_R_PASSED] == 7);
	{ uint32_t seen = writes_codec; assert(!au_mic_turn(&g, &m) && writes_codec == seen); }
	/* A capture: the stage stays up, the ports as Android's amic1 and sheng's, nothing of them without one. */
	fresh(); m.capture = true; m.port = 3; m.lane = 1; m.mic = 5; au_mic_run(&m);
	assert(m.held && !m.going_down && rep[AU_MIC_R_STEP] == 0 && rep[AU_MIC_R_PASSED] == 7);
	assert(rep[AU_MIC_R_PORTS] == 1 && rep[AU_MIC_R_PORTS + 1] == 1 && rep[AU_MIC_R_PORTS + 2] == 0x01000001u && rep[AU_MIC_R_PORTS + 3] == 1 && rep[AU_MIC_R_PORTS + 4] == 0xf0);
	assert(swr_regs[0x1324 / 4] == 0x01000001u && swr_regs[0x1364 / 4] == 0x01000001u && swr_regs[0x1368 / 4] == 1 && swr_regs[0x1374 / 4] == 0xf0);
	assert(dp[0x28] == 1 && dp[0x38] == 1 && dp[0x20] == 1 && dp[0x30] == 1 && dp[0x05] == 1 && dp[0x22] == 1 && dp[0x26] == 0xff);
	assert((txm[0x100 / 4] & 0xf) == 5 && (txm[0x104 / 4] & 3) == 1 && (txm[0x404 / 4] & 0xe6) == 0x40 && (txm[0x400 / 4] & 0x3f) == 0x34);
	au_mic_down(&m); assert_down();
	assert((codec[0x3022 - 0x3000] & 0xc0) == 0);
	/* Sheng's: its port, lane 2, its mux for this channel. */
	fresh(); m.capture = true; m.port = 2; m.lane = 2; m.mic = 1; au_mic_run(&m);
	assert(m.held && swr_regs[0x1224 / 4] == 0x02000001u && swr_regs[0x1268 / 4] == 1 && dp[0x28] == 2 && (txm[0x100 / 4] & 0xf) == 1);
	au_mic_down(&m);
	/* A stage that fails does not stay up for a graph: down, and the list goes to letting go. */
	fresh(); m.capture = true; m.port = 3; m.lane = 1; m.mic = 5; wrong_version = 1;
	g.graph_sent = 1; g.graph_status[0] = 0; assert(!au_mic_turn(&g, &m));
	assert(m.over && !m.held && m.going_down && g.graph_sent == AU_MIC_CAP_RELEASE);
	/* It passes: on to the graph; refused three times, past the list. */
	fresh(); m.capture = true; m.port = 3; m.lane = 1; m.mic = 5;
	g.graph_sent = 1; g.graph_status[0] = 0; assert(!au_mic_turn(&g, &m));
	assert(m.over && m.held && g.graph_sent == AU_MIC_TRIES);
	au_mic_down(&m);
	fresh(); au_mic_begin(&m, rep); m.capture = true; votes_ok = 0;
	g.graph_sent = 1; g.graph_status[0] = 1; assert(!au_mic_turn(&g, &m));
	g.graph_sent = 2; g.graph_status[1] = 1; assert(!au_mic_turn(&g, &m));
	g.graph_sent = 3; g.graph_status[2] = 1; assert(!au_mic_turn(&g, &m) && g.graph_sent == AU_MIC_CAP_STEPS && !m.held);
	/* The capture's list: each packet's opcode in its place, the token its step's, the clock let go at the end, nothing past it. */
	{
		static uint32_t p[2 + AU_GL_GPR_BUFFER / 4 + 2];
		static const FuxiArFormat f = {48000, 16, 1};
		uint8_t *b = (uint8_t *)(p + 2);
		uint32_t w[8];
		static const uint32_t want[18] = {0, 0, 0, FUXI_AR_OP_GRAPH_OPEN, FUXI_AR_OP_SET_CFG, FUXI_AR_OP_GRAPH_OPEN, FUXI_AR_OP_SET_CFG, FUXI_AR_OP_MEM_MAP,
						  FUXI_AR_OP_GRAPH_PREPARE, FUXI_AR_OP_GRAPH_PREPARE, FUXI_AR_OP_GRAPH_START, FUXI_AR_OP_GRAPH_START,
						  FUXI_AR_OP_GRAPH_STOP, FUXI_AR_OP_GRAPH_STOP, FUXI_AR_OP_GRAPH_CLOSE, FUXI_AR_OP_GRAPH_CLOSE, FUXI_AR_OP_MEM_UNMAP, 0x01001010u};

		for (uint32_t i = 3; i < AU_MIC_CAP_STEPS; i++) {
			uint32_t n = au_mic_cap_build(i, p, AU_GL_GPR_BUFFER, &f, 0xd0000000ull | 1ull << 32, 0x10000u + 3840u, 7);

			assert(n >= 24); memcpy(w, b, 24);
			assert(w[4] == AU_GL_GRAPH_TOKEN + i && w[5] == want[i]);
		}
		{ uint32_t n = au_mic_cap_build(AU_MIC_CAP_RELEASE, p, AU_GL_GPR_BUFFER, &f, 0, 0, 0); assert(n >= 24); memcpy(w, b, 24); assert(w[2] == 2 && w[3] == 2); }
		assert(au_mic_cap_build(AU_MIC_CAP_STEPS, p, AU_GL_GPR_BUFFER, &f, 0, 0, 0) == 0);
	}
	puts("au_mic: all seven steps, each stopping where it fails and coming back down; the clock's tries; only the allow-list and VOUT 0x23 reach the codec; L15B only on, and only at 0x40a00");
	return 0;
}
"""


def strip_comments(text):
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    return re.sub(r'//[^\n]*', '', text)


def main():
    with tempfile.TemporaryDirectory(prefix='fuxi-mic-') as tmp:
        source = Path(tmp) / 'mic.c'
        source.write_text(DRIVER)
        program = Path(tmp) / 'mic'
        subprocess.run([os.environ.get('CC', 'cc'), '-std=gnu11', '-Wall', '-Wextra', '-Wno-unused-function', '-Wno-unused-parameter', '-Wno-misleading-indentation', '-g', '-O1',
                        '-fsanitize=address,undefined', '-fno-sanitize-recover=all', f'-I{ROOT / "shim/fuxi-probe"}', f'-I{ROOT / "xnu/fuxi-audio"}', str(source),
                        str(ROOT / 'xnu/fuxi-audio/fuxi_audioreach.c'), str(ROOT / 'xnu/fuxi-audio/fuxi_gpr.c'), '-o', str(program)], check=True)
        run = subprocess.run([str(program)], check=True, capture_output=True, text=True)
    reports = {}
    for line in run.stdout.splitlines():
        if line.startswith('REPORT '):
            parts = line.split()
            reports[parts[1]] = [int(x) for x in parts[2:]]
        else:
            print(line)

    # The report's words and the host tool's reading of them are one thing.
    spec = importlib.util.spec_from_file_location('fuxi_audio_probe', ROOT / 'tools/fuxi-audio-probe.py')
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    text = tool.decode_mic(reports['all'])
    assert '7 of 7 proof steps passed' in text and '-- as live DT' in text and 'FAILED' not in text and 'not reached' not in text, text
    assert 'cmd-db ldob15 0x40a00' in text and 'master version 0x2000000' in text and 'variant 5 (WCD9385)' in text and 'read back 25 of 25' in text and '3022=63' in text and 'set 0x44 0x46' in text, text
    assert 'down: 0x3022 0x23' in text, text
    for step in range(1, 8):
        assert f'  {step} {tool.MIC_STEPS[step]}: ok' in text, text
    text = tool.decode_mic(reports['abort'])
    assert 'stopped at 2' in text and 'an abort' in text and '2 LPASS reachable: FAILED' in text and '3 macros clocked' in text and "3 macros clocked, the master's version: not reached" in text and f'first at {0x6d440c0:#x}' in text, text
    text = tool.decode_mic(reports['frames'])
    assert 'stopped at 4' in text and 'no frames' in text and '5 codec enumerated, its id: not reached' in text and 'after 500 ms' in text, text
    text = tool.decode_mic(reports['readback'])
    assert 'stopped at 7' in text and 'first wrong 0x300e, want 0x8, got 0x20' in text and '300e=20' in text, text
    text = tool.decode_mic(reports['refused'])
    assert 'stopped at 1' in text and 'refused all three ways' in text and 'clock 19.2 MHz, 9.6 MHz, attribute 0: 0x1 0x1 0x1' in text, text
    text = tool.decode_mic(reports['votes'])
    assert 'stopped at 1' in text and 'votes were refused' in text and 'not tried not tried not tried' in text, text
    # The host tool's list of what is read back is the header's.
    header = (ROOT / 'shim/fuxi-probe/au_mic.h').read_text()
    code = strip_comments(header)
    want = re.search(r'au_mic_want\[25\] = \{(.*?)\n\};', code, re.S).group(1).replace('AU_MIC_MICB1', '0x3022').replace('0x40 | AU_MIC_VOUT_MAX', '0x63')
    rows = re.findall(r'\{(0x[0-9a-f]+), (0x[0-9a-f]+), (0x[0-9a-f]+)\}', want)
    assert [(int(a, 16), int(b, 16), int(c, 16)) for a, b, c in rows] == tool.MIC_WANT

    ports_allowed = {0x102, 0x103, 0x105, 0x120, 0x122, 0x123, 0x124, 0x125, 0x126, 0x128, 0x130, 0x132, 0x133, 0x134, 0x135, 0x136, 0x138}    # the first data port's, for a capture
    # Codec writes: this exact list, so no efuse (0x3460, 0x3461, 0x3490-0x34c6), no HPH (0x3008-0x300d and 0x3100s' HPH), no other bias; and what is written is all allowed.
    allowed = {int(x, 16) for x in re.findall(r'0x[0-9a-f]+', re.search(r'au_mic_allowed\[\] = \{(.*?)\};', code, re.S).group(1))}
    assert allowed == {0x3001, 0x300e, 0x300f, 0x3022, 0x3029, 0x306b, 0x306e, 0x3071, 0x3074, 0x308f, 0x3103, 0x312a, 0x31df, 0x31e1, 0x3408, 0x3409, 0x3410, 0x3417, 0x3457, 0x345b, 0x34d0, 0x34d1, 0x34d2, 0x34d3, 0x34d4} | ports_allowed
    forbidden = {0x3460, 0x3461} | set(range(0x3490, 0x34d0)) | set(range(0x3008, 0x300e)) | {0x3023, 0x3024, 0x3025, 0x3026}
    assert not allowed & forbidden
    writes = re.search(r'au_mic_writes\[\] = \{(.*?)\n\};', code, re.S).group(1).replace('AU_MIC_MICB1', '0x3022')
    written = {int(a, 16) for a in re.findall(r'\{(0x[0-9a-f]+),', writes)}
    assert written | ports_allowed == allowed and not written & ports_allowed
    # The bias: VOUT's bound is 0x23, it is the only value given for VOUT, and the check stands in the one function that sends.
    assert re.search(r'#define AU_MIC_VOUT_MAX 0x23u', code) and 'reg == AU_MIC_MICB1 && (value & 0x3fu) > AU_MIC_VOUT_MAX' in code
    bias_writes = re.findall(r'\{AU_MIC_MICB1, ([^}]*)\}', re.search(r'au_mic_writes\[\] = \{(.*?)\n\};', code, re.S).group(1))
    assert bias_writes == ['0x3f, AU_MIC_VOUT_MAX, 0', '0xc0, 0x40, 0'], bias_writes
    # Only au_mic_codec_write() puts a value in the FIFO, and only a list or the bias's off go through it.
    assert code.count('AU_MIC_SWR + 0x5020u') == 1
    send = code[code.index('static inline bool au_mic_codec_write'):code.index('static inline bool au_mic_codec_update')]
    assert 'AU_MIC_SWR + 0x5020u' in send and 'allowed' in send
    ports = code[code.index('static inline bool au_mic_ports'):code.index('static inline void au_mic_run')]
    assert 'au_mic_codec_write(m, reg,' in code and code.replace(ports, '').count('au_mic_codec_write(') == 2
    assert {int(x, 16) for x in re.findall(r'au_mic_codec_write\(m, (0x[0-9a-f]+)u', ports)} == {0x102, 0x103, 0x105, 0x120, 0x122, 0x123, 0x124, 0x125, 0x126, 0x128}
    assert 'bias' not in ports and 'AU_MIC_MICB1' not in ports
    assert re.findall(r'au_mic_codec_update\(m, ([^,]+),', code) == ['w->reg', 'AU_MIC_MICB1']
    # L15B: three votes, all on, all to its constant, in the function that checked cmd-db; BOB1 nowhere.
    votes = re.findall(r'AU_MIC_RPMH\(([^)]*)\)', code)
    assert votes == ['AU_MIC_L15B + 8u, 7u', 'AU_MIC_L15B, 1800u', 'AU_MIC_L15B + 4u, 1u'], votes
    assert '#define AU_MIC_L15B     0x40a00u' in code and 'if (address != AU_MIC_L15B) return au_mic_fail(m, 3, AU_MIC_WHY_CMDDB);' in code
    rail = code[code.index('static inline bool au_mic_rail'):code.index('static inline bool au_mic_reset_release')]
    assert rail.index('AU_MIC_WHY_CMDDB') < rail.index('AU_MIC_RPMH(') and code.count('AU_MIC_RPMH(') == 3 and rail.count('AU_MIC_RPMH(') == 3 and code.count('AU_MIC_CMDDB(') == 1
    assert 'bob' not in code.lower()
    # Addresses: the blocks named, and no other. No RX master (0x6ad0000), no WSA, no amplifier or TDM pad, no I2C, no apps_smmu. Of the TLMM, gpio108: 0x0f100000 + 108 * 0x1000.
    hexes = {x.lower() for x in re.findall(r'0x[0-9a-fA-F]{7,}', code)}
    assert 0x0f100000 + 108 * 0x1000 == 0x0f16c000
    assert hexes == {'0x06d44000', '0x06ae0000', '0x06d30000', '0x06e80000', '0x0725a000', '0x0f16c000', '0xa0d10007', '0xfffffffe', '0xffffffff'}, hexes
    for bad in ('AU_TLMM', 'AU_SE', 'AU_SMMU', 'au_amp', 'au_on', 'au_pin', 'volatile uint32_t', 'probe_safe', 'rh_send', 'usb_rd', 'au_wr', 'AU_TONE'):
        assert bad not in code.replace('au_mic_wr', ''), bad
    for pin in (68, 121, 122, 123, 124, 182, 183):
        assert f'{0x0f100000 + pin * 0x1000:#x}' not in code
    # Reads and writes go through the two functions alone; every read of hardware is AU_MIC_RD, every write AU_MIC_WR.
    assert code.count('AU_MIC_RD(') == 1 and code.count('AU_MIC_WR(') == 1
    # No HPH and no RX by name.
    assert 'HPH' not in code and 'RX' not in re.sub(r'AU_GL_[A-Z_]+', '', code)

    # The hook: the safe accessors, nothing else, after a boot that reached ready, between the votes; no capture graph, no amplifier, no SMMU in this run.
    audio = (ROOT / 'shim/fuxi-probe/audio.c').read_text()
    assert '#define AU_MIC_RD(pa, f) probe_safe_rd32((pa), (f))' in audio and '#define AU_MIC_WR(pa, v) probe_safe_wr32((pa), (v))' in audio
    assert 'return rh_send(0, addr, data);' in audio and audio.count('au_mic_rpmh(') == 2
    hook = audio[audio.index("if (value == 0x4143"):audio.index("if (value == 0x4143 && (index == 0x4d32")]
    assert "au_boot[1] == 1" in hook and '!monitor_report[MON_LAUNCH]' in hook and '!au_gl_rep[0]' in hook and 'index == 0x4d31' in hook
    assert 'AU_GL_ANSWER | AU_GL_GPR | AU_GL_PRM | AU_GL_GRAPH | AU_GL_MIC' in hook and 'AU_GL_PLAY' not in hook and 'AU_GL_TONE' not in hook
    # The capture: the same, with --play's apps_smmu; no tone, and in its stage no amplifier, no pad, no other codec register than the gain and the bias, whose values are 0x40 and 0. Only numbers go out.
    cap_hook = audio[audio.index("if (value == 0x4143 && (index == 0x4d32"):audio.index("if (value == 0x4149")]
    assert "au_boot[1] == 1" in cap_hook and '!monitor_report[MON_LAUNCH]' in cap_hook and '!au_gl_rep[0]' in cap_hook and 'AU_GL_PLAY | AU_GL_MIC' in cap_hook and 'AU_GL_TONE' not in cap_hook
    stage = audio[audio.index('static void au_mic_scan'):audio.index('static uint32_t audio_request')]
    # 0x4d34 alone adds the tone, by au_tone_stage() unchanged: the only sound here, and the tool wants --owner-agreed for it.
    loop = stage[stage.index('static void au_mic_tone_edge'):stage.index('static uint32_t au_mic_cap_stage')]
    stage = stage.replace(loop, '')
    assert loop.count('= au_tone_stage(g, au_gl_rep3, t0, hz)') == 1 and re.findall(r'au_mic_codec_update\(mic, ([^,]+),', loop) == ['0x300eu', 'AU_MIC_MICB1'] and 'AU_MIC_MICB1, 0xc0u, 0u' in loop and 'au_on_' not in loop and 'AU_AMP' not in loop and 'au_pin' not in loop
    assert audio.count('au_mic_loop = ') == 1 and 'au_mic_loop = index == 0x4d34 || index == 0x4d35;' in cap_hook and audio.count('au_mic_loop_stage(') == 2 and 'if (au_mic_loop) au_mic_loop_stage(g, q, t0, hz);' in stage
    probe = (ROOT / 'tools/fuxi-audio-probe.py').read_text()
    assert "if a.cmd.startswith('mic-loop') and not a.owner_agreed:" in probe and probe.count('0x4d34') == 1 and probe.count('0x4d35') == 1
    for bad in ('au_on', 'AU_AMP', 'au_pin', 'AU_TLMM', 'AU_SE0', 'memcpy', 'au_mic_rep', 'au_tone_'):
        assert bad not in stage, bad
    assert re.findall(r'au_mic_codec_update\(m, ([^,]+),', stage) == ['0x300eu', 'AU_MIC_MICB1'] and '{{8, 0x40}, {16, 0x40}, {16, 0}, {16, 0x40}}' in stage
    # Run inside the list, between the votes (prm_sent == 2): the clock let go after it by the list's last step, the votes then by au_gl_prm_step()'s last two.
    assert audio.count('au_mic_turn(') == 1 and audio.count('au_mic_run(') == 0
    run_branch = audio[audio.index('g.prm_sent == 2 && g.graph_sent < g.graph_steps'):audio.index('else if (au_gl_prm_step(&g))')]
    assert 'au_mic_turn(&g, &au_mic_state)' in run_branch and 'au_mic_build(g.graph_sent, m, AU_GL_GPR_BUFFER)' in run_branch
    glink = (ROOT / 'shim/fuxi-probe/au_glink.h').read_text()
    assert 'block[AU_GL_PRM_STEPS] = {1, 2, 2, 1}' in glink and '#define AU_GL_PRM_STEPS 4u' in glink
    # A capture graph or a buffer does not come with this: au_mic.h's list names only the clock.
    list_code = code[code.index('static inline uint32_t au_mic_build'):code.index('static inline uint32_t au_mic_cap_build')]
    assert list_code.count('fuxi_ar_') == 2 and 'fuxi_ar_prm_clock_attr' in list_code
    cap_list = code[code.index('static inline uint32_t au_mic_cap_build'):code.index('static inline bool au_mic_turn')]
    for bad in ('fuxi_ar_open', 'fuxi_ar_graph', 'fuxi_ar_map', 'fuxi_ar_read', 'fuxi_ar_write'):
        assert bad not in code.replace(cap_list, ''), bad
    assert 'fuxi_ar_write' not in cap_list and 'speaker' not in cap_list and 'playback' not in cap_list and 'FUXI_AR_SG_PLAY' not in cap_list and 'FUXI_AR_SG_SPEAKER' not in cap_list
    # Licence: comments here say where it came from; nothing is marked as lifted from Linux code.
    assert 'BSD-2-Clause-Patent' in header
    # CI runs it.
    assert 'python3 tools/test-fuxi-audio-mic.py' in (ROOT / 'tools/ci/build.sh').read_text()
    print('PASS: au_mic')


main()
