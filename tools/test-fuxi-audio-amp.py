#!/usr/bin/env python3
"""shim/fuxi-probe/au_amp.h against a fake GENI engine and fake amplifiers under the sanitizers: it reads who they are and can write nothing."""
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
#include <string.h>

/* The engine: registers, and a command list the fake acts on as they are written. */
static uint32_t regs[0x400];
static unsigned writes_seen, reads_seen, delays;
static uint32_t irq;
static uint32_t tx_len, rx_len, pending_cmd, pending_addr;
static uint32_t cur_reg, rx_word, rx_count;
static int mode;		/* 0 answers, 1 NACK on the address, 2 bus error, 3 never done, 4 short read, 5 faulted write, 6 NACK for the first polls */
static unsigned nacks_left, fault_after;
static uint32_t amp[0x20000], boot_after;
static uint32_t answered_address;
static unsigned tx_commands, rx_commands, tx_bytes_total;

#define SE 0x1000u
static uint32_t fake_rd(uint64_t pa)
{
	uint32_t off = (uint32_t)(pa - SE);
	assert(off < 0x1000);
	reads_seen++;
	if (off == 0x610) return irq;
	if (off == 0x804) return rx_count;
	if (off == 0x780) { assert(rx_count != 0); rx_count = 0; return rx_word; }
	return regs[off / 4];
}
static int fake_wr(uint64_t pa, uint32_t v)
{
	uint32_t off = (uint32_t)(pa - SE);
	assert(off < 0x1000);
	if (mode == 5 && writes_seen >= fault_after) return 1;
	writes_seen++;
	if (off == 0x618) { irq &= ~v; return 0; }
	if (off == 0x604) { assert(v == 2); irq |= 1u << 5; return 0; }
	if (off == 0x26c) { tx_len = v; return 0; }
	if (off == 0x270) { rx_len = v; return 0; }
	if (off == 0x600) {
		uint32_t op = v >> 27, address = (v >> 9) & 0x7f;
		assert((irq & 1u) == 0);		/* cleared before each command */
		pending_cmd = op; pending_addr = address;
		if (op == 1) {
			/* The write: only ever 4 bytes, the register's address; never without the stretch that keeps it a read's first half. */
			assert(tx_len == 4 && (v & (1u << 2)) != 0);
			tx_commands++; tx_bytes_total += tx_len;
		} else {
			assert(op == 2 && rx_len == 4 && (v & (1u << 2)) == 0);
			rx_commands++;
			if (mode == 4) { irq |= 1; return 0; }
			if (mode == 3) return 0;
			if (mode == 2) { irq |= 1u << 12; return 0; }
			if (mode == 6 && nacks_left) { nacks_left--; irq |= 1u << 10; return 0; }
			if (mode == 1) { irq |= 1u << 10; return 0; }
			/* big-endian on the wire, first byte the low byte of the FIFO word */
			uint32_t val = cur_reg == 0x1001c ? (boot_after ? (boot_after--, 0u) : 2u) : amp[cur_reg / 4];
			rx_word = val >> 24 | (val >> 8 & 0xff00u) | (val << 8 & 0xff0000u) | val << 24;
			rx_count = 1;
			answered_address = address;
			irq |= 1;
		}
		return 0;
	}
	if (off == 0x700) {
		assert(pending_cmd == 1);
		cur_reg = v >> 24 | (v >> 8 & 0xff00u) | (v << 8 & 0xff0000u) | v << 24;
		assert(cur_reg == 0 || cur_reg == 4 || cur_reg == 0x10 || cur_reg == 0x10018 || cur_reg == 0x1001c);	/* only the five */
		if (mode == 1) { irq |= 1u << 10; return 0; }
		if (mode == 2) { irq |= 1u << 12; return 0; }
		if (mode == 3) return 0;
		if (mode == 6 && nacks_left) { nacks_left--; irq |= 1u << 10; return 0; }
		irq |= 1;
		return 0;
	}
	regs[off / 4] = v;
	return 0;
}
#define AU_AMP_RD(pa) fake_rd(pa)
#define AU_AMP_WR(pa, v) fake_wr((pa), (v))
#define AU_AMP_DELAY_US(us) (delays += (us))
#include "au_amp.h"

static void fresh(int m)
{
	memset(regs, 0, sizeof(regs));
	regs[0x068 / 4] = 0x303; regs[0xe24 / 4] = 0x201029e4;
	mode = m; irq = 0; writes_seen = reads_seen = delays = 0; tx_commands = rx_commands = tx_bytes_total = 0; rx_count = 0; pending_cmd = 0; boot_after = 0; nacks_left = 0;
	memset(amp, 0, sizeof(amp));
	amp[0] = 0x35a40; amp[1] = 0xb2; amp[0x10 / 4] = 8; amp[0x10018 / 4] = 0;
}

int main(void)
{
	struct au_amp a;

	/* The engine: FIFO, 100 kHz from XO; refused, untouched, on other firmware, with FIFOs off, or busy. */
	fresh(0);
	assert(au_amp_engine(SE));
	assert(regs[0x258 / 4] == 0 && regs[0x48 / 4] == 0x71 && regs[0x278 / 4] == (10u << 20 | 12u << 10 | 26u) && regs[0x7c / 4] == 0);
	assert(regs[0x260 / 4] == 0x7f8fe && regs[0x264 / 4] == 0xffefe && regs[0x284 / 4] == 0x7f8fe && regs[0x288 / 4] == 0xffefe && regs[0x814 / 4] == 14);
	assert((regs[0x614 / 4] & 1) && (regs[0xe1c / 4] & 0xf) == 0xf);
	fresh(0); regs[0x068 / 4] = 0x103; assert(!au_amp_engine(SE) && writes_seen == 0);
	fresh(0); regs[0x064 / 4] = 1;     assert(!au_amp_engine(SE) && writes_seen == 0);
	fresh(0); regs[0x040 / 4] = 1;     assert(!au_amp_engine(SE) && writes_seen == 0);
	fresh(0); regs[0xe24 / 4] = 0;     assert(!au_amp_engine(SE) && writes_seen == 0);
	fresh(5); fault_after = 3;         assert(!au_amp_engine(SE));

	/* A good amplifier: five reads, each a 4 byte address and 4 bytes back, values right way round. */
	fresh(0); memset(&a, 0, sizeof(a)); a.address = 0x40;
	au_amp_identify(SE, &a);
	assert(a.result == AU_AMP_OK && a.devid == 0x35a40 && a.revid == 0xb2 && a.otpid == 8 && a.boot_error == 0 && (a.boot_status & 2) && a.boot_ms == 0);
	assert(tx_commands == 5 && rx_commands == 5 && tx_bytes_total == 20 && answered_address == 0x40);

	/* OTP boot takes a while: polled, one read a millisecond. */
	fresh(0); boot_after = 7; memset(&a, 0, sizeof(a)); a.address = 0x42;
	au_amp_identify(SE, &a);
	assert(a.result == AU_AMP_OK && a.boot_ms == 7 && a.devid == 0x35a40 && answered_address == 0x42);
	/* It never comes: given up on at 100 ms, and nothing further is read. */
	fresh(0); boot_after = 1000; memset(&a, 0, sizeof(a)); a.address = 0x42;
	au_amp_identify(SE, &a);
	assert(a.boot_ms == 100 && (a.boot_status & 2) == 0);
	/* NACKed while it wakes, then fine. */
	fresh(6); nacks_left = 5; memset(&a, 0, sizeof(a)); a.address = 0x40;
	au_amp_identify(SE, &a);
	assert(a.result == AU_AMP_OK && a.boot_ms == 5 && a.devid == 0x35a40);

	/* Nobody there: NACK, each time aborted, ended by the clock and not by a hang. */
	fresh(1); memset(&a, 0, sizeof(a)); a.address = 0x40;
	au_amp_identify(SE, &a);
	assert(a.result == AU_AMP_NACK && a.boot_ms == 100 && a.devid == 0 && (a.irq_seen & (1u << 10)) && irq == 0);
	/* A bus error ends it at once. */
	fresh(2); memset(&a, 0, sizeof(a)); a.address = 0x40;
	au_amp_identify(SE, &a);
	assert(a.result == AU_AMP_BUS && a.boot_ms == 0);
	/* A command that never ends is bounded. */
	fresh(3); memset(&a, 0, sizeof(a)); a.address = 0x40;
	au_amp_identify(SE, &a);
	assert(a.result == AU_AMP_TIMEOUT && a.boot_ms == 0 && delays >= 20000 && delays < 100000);
	/* Done with nothing in the FIFO, or a faulted write. */
	fresh(4); memset(&a, 0, sizeof(a)); a.address = 0x40;
	au_amp_identify(SE, &a);
	assert(a.result == AU_AMP_SHORT);
	fresh(5); fault_after = 2; memset(&a, 0, sizeof(a)); a.address = 0x40;
	au_amp_identify(SE, &a);
	assert(a.result == AU_AMP_FAULT);
	puts("au_amp: identity read, right way round; refusals, NACK, bus error, hang, short and faults all bounded; only 4 byte addresses go out");
	return 0;
}
"""


def main():
    with tempfile.TemporaryDirectory(prefix='fuxi-amp-') as tmp:
        source = Path(tmp) / 'amp.c'
        source.write_text(DRIVER)
        program = Path(tmp) / 'amp'
        subprocess.run([os.environ.get('CC', 'cc'), '-std=gnu11', '-Wall', '-Wextra', '-Wno-unused-function', '-Wno-unused-parameter', '-g', '-O1',
                        '-fsanitize=address,undefined', '-fno-sanitize-recover=all', f'-I{ROOT / "shim/fuxi-probe"}', str(source), '-o', str(program)], check=True)
        subprocess.run([str(program)], check=True)
    text = (ROOT / 'shim/fuxi-probe/au_amp.h').read_text()
    code = text[text.index('#define AU_SE_FORCE_DEFAULT'):]
    # The write command appears once, in the read. Only five registers are named, all read ones. Nothing here takes a value to send.
    assert code.count('AU_I2C_WRITE <<') == 1
    read = code[code.index('static inline int au_amp_read'):code.index('static inline bool au_amp_engine')]
    assert 'AU_I2C_WRITE <<' in read and 'AU_SE_TX_TRANS_LEN, 4u' in read and read.count('AU_SE_TX_FIFO') == 1 and 'au_amp_be(reg)' in read
    assert code.count('AU_SE_TX_FIFO') == 2
    assert set(re.findall(r'#define (AU_AMP_(?:DEVID|REVID|OTPID|IRQ1_ST\d))\b', code)) == {'AU_AMP_DEVID', 'AU_AMP_REVID', 'AU_AMP_OTPID', 'AU_AMP_IRQ1_ST3', 'AU_AMP_IRQ1_ST4'}
    print('PASS: au_amp')


main()
