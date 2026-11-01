#!/usr/bin/env python3
"""shim/fuxi-probe/au_amp_on.h against a fake GENI engine and a fake CS35L41 under the sanitizers: the order that matters, the limits, and stopping at the first thing wrong."""
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
#include <string.h>

#define SE 0x1000u
#define AMP_REGS (0x20000u / 4)
static uint32_t regs[0x400], amp[AMP_REGS], irq;
static uint32_t top_regs[16], top_addr[16];	/* the handful above 0x20000 */
static unsigned top_n;
static uint32_t tx_len, cmd, addr_word, tx_words, tx_buf[2], cur_reg, rx_word, rx_count;
static unsigned writes_to_amp, bad_after, nack_after, ms;
static uint32_t log_reg[2000], log_val[2000];
static int stuck_reg = -1;
static bool fault_at_pup, no_pup, no_pll;

static uint32_t *amp_reg(uint32_t reg)
{
	if (reg / 4 < AMP_REGS) return &amp[reg / 4];
	for (unsigned i = 0; i < top_n; i++) if (top_addr[i] == reg) return &top_regs[i];
	assert(top_n < 16);
	top_addr[top_n] = reg;
	return &top_regs[top_n++];
}
static uint32_t be(uint32_t v) { return v >> 24 | (v >> 8 & 0xff00u) | (v << 8 & 0xff0000u) | v << 24; }
static uint32_t fake_rd(uint64_t pa)
{
	uint32_t off = (uint32_t)(pa - SE);
	if (off == 0x610) return irq;
	if (off == 0x804) return rx_count;
	if (off == 0x780) { assert(rx_count); rx_count = 0; return rx_word; }
	return regs[off / 4];
}
static void amp_write(uint32_t reg, uint32_t v)
{
	assert(writes_to_amp < 2000);
	log_reg[writes_to_amp] = reg; log_val[writes_to_amp] = v; writes_to_amp++;
	if (reg == 0x10010 || reg == 0x10014 || reg == 0x10018 || reg == 0x1001c) { *amp_reg(reg) &= ~v; return; }	/* write one to clear */
	if ((int)reg == stuck_reg) return;
	*amp_reg(reg) = v;
	if (reg == 0x2014 && (v & 1)) {
		uint32_t *st = amp_reg(0x10010);
		if (fault_at_pup) *st |= 1u << 6;
		if (!no_pup) *st |= 1u << 24;
		if (!no_pll) *amp_reg(0x10018) |= 2;
	}
	if (reg == 0x2014 && !(v & 1)) *amp_reg(0x10010) |= 1u << 23;
}
static int fake_wr(uint64_t pa, uint32_t v)
{
	uint32_t off = (uint32_t)(pa - SE);
	if (off == 0x618) { irq &= ~v; return 0; }
	if (off == 0x604) { irq |= 1u << 5; return 0; }
	if (off == 0x26c) { tx_len = v; return 0; }
	if (off == 0x600) {
		cmd = v >> 27; addr_word = (v >> 9) & 0x7f; tx_words = 0;
		if (nack_after && writes_to_amp >= nack_after) { irq |= 1u << 10; return 0; }
		if (cmd == 2) { rx_word = be(*amp_reg(cur_reg)); rx_count = 1; irq |= 1; }
		return 0;
	}
	if (off == 0x700) {
		if (nack_after && writes_to_amp >= nack_after) return 0;
		tx_buf[tx_words++] = be(v);
		if (tx_len == 4 && tx_words == 1) { cur_reg = tx_buf[0]; irq |= 1; }
		if (tx_len == 8 && tx_words == 2) { amp_write(tx_buf[0], tx_buf[1]); irq |= 1; }
		return 0;
	}
	if (off == 0x270) return 0;
	regs[off / 4] = v;
	return 0;
}
#define AU_AMP_RD(pa) fake_rd(pa)
#define AU_AMP_WR(pa, v) fake_wr((pa), (v))
#define AU_AMP_DELAY_US(us) (ms += (us) / 1000)
#include "au_amp.h"
#include "au_amp_on.h"

static void fresh(void)
{
	memset(regs, 0, sizeof(regs)); memset(amp, 0, sizeof(amp)); top_n = 0;
	irq = 0; writes_to_amp = 0; bad_after = 0; nack_after = 0; ms = 0; stuck_reg = -1; fault_at_pup = no_pup = no_pll = false;
	amp[0] = 0x35a40; amp[1] = 0xb2; amp[0x10 / 4] = 8; amp[0x1001c / 4] = 2;
	amp[0x3808 / 4] = 0x4a;		/* reset's 4500 mA */
	for (unsigned i = 0; i < 32; i++) amp[(0x400 + 4 * i) / 4] = 0xa5a50000u + i * 0x1111u;
}
static int index_of(uint32_t reg, uint32_t val)
{
	for (unsigned i = 0; i < writes_to_amp; i++) if (log_reg[i] == reg && log_val[i] == val) return (int)i;
	return -1;
}
static struct au_on on(void) { struct au_on o; memset(&o, 0, sizeof(o)); o.se = SE; o.address = 0x40; return o; }

int main(void)
{
	struct au_on o = on();

	fresh();
	assert(au_on_prepare(&o, true) == AU_ON_OK);
	/* The boost's limit comes before anything that can enable it; analog and digital at their lowest. */
	assert(amp[0x3808 / 4] == 0x40);
	int ipk = index_of(0x3808, 0x40), bst = index_of(0x2018, 0x20), en = index_of(0x2018, 0x21);
	assert(ipk >= 0 && bst > ipk && en > bst);
	assert(index_of(0x6000, AU_ON_MUTE) > ipk && amp[0x6c04 / 4] == 0 && amp[0x6000 / 4] == AU_ON_MUTE);
	/* No global enable yet, and the test key locked again; the slot as asked; OTP's fields all gone through. */
	assert(amp[0x2014 / 4] == 0 && amp[0x40 / 4] == 0x33 && amp[0x4820 / 4] == 0x401 && amp[0x4c00 / 4] == 8 && o.otp_fields == 98);
	assert(amp[0x2c04 / 4] == 0x510);	/* the PLL's final form, as Cirrus's 0x430 at 3.072 MHz */
	assert(amp[0x4808 / 4] == 0x20200000 && amp[0x2c0c / 4] == 3 && amp[0x480c / 4] == 3);
	assert(au_on_power_up(&o) == AU_ON_OK && (amp[0x2014 / 4] & 1) == 1);
	assert(index_of(0x2084, 0x002f1aa0) >= 0 && index_of(0x2084, 0x002f1aa0) < index_of(0x2014, 1));
	/* Only the three levels. */
	assert(au_on_unmute(&o, 0) == AU_ON_OK && amp[0x6000 / 4] == 0xb885);
	assert(au_on_unmute(&o, AU_ON_LEVELS) != AU_ON_OK && amp[0x6000 / 4] == 0xb885);
	assert(amp[0x6c04 / 4] == 0);
	assert(au_on_check(&o) == AU_ON_OK);
	amp[0x10010 / 4] |= 1u << 15;		/* a temperature warning */
	assert(au_on_check(&o) == AU_ON_FAULT);
	au_on_down(&o);
	assert(amp[0x6000 / 4] == AU_ON_MUTE && (amp[0x2014 / 4] & 1) == 0 && amp[0x4800 / 4] == 0 && (amp[0x2018 / 4] & 1) == 0 && amp[0x2084 / 4] == 0x002f1aa3);

	/* Not who it should be: not one write. */
	fresh(); o = on(); amp[0] = 0x35b40; assert(au_on_prepare(&o, false) == AU_ON_WHO && writes_to_amp == 0);
	fresh(); o = on(); amp[1] = 0xa0;    assert(au_on_prepare(&o, false) == AU_ON_WHO && writes_to_amp == 0);
	fresh(); o = on(); amp[0x10 / 4] = 2; assert(au_on_prepare(&o, false) == AU_ON_WHO && writes_to_amp == 0);
	fresh(); o = on(); amp[0x10018 / 4] = 1u << 31; assert(au_on_prepare(&o, false) == AU_ON_OTP && writes_to_amp == 0);
	fresh(); o = on(); amp[0x1001c / 4] = 0; assert(au_on_prepare(&o, false) == AU_ON_OTP && writes_to_amp == 0 && ms >= 100);
	/* A register that does not take what it is told: stops there, says which, and nothing after it goes. */
	fresh(); o = on(); stuck_reg = 0x6000;
	assert(au_on_prepare(&o, false) == AU_ON_READBACK && o.failed_reg == 0x6000 && index_of(0x2018, 0x21) < 0 && index_of(0x2c0c, 3) < 0);
	fresh(); o = on(); stuck_reg = 0x3808;
	assert(au_on_prepare(&o, false) == AU_ON_READBACK && o.failed_reg == 0x3808 && index_of(0x2018, 0x20) < 0);
	/* A bus that goes away part of the way. */
	fresh(); o = on(); nack_after = 40; assert(au_on_prepare(&o, false) == AU_ON_I2C && index_of(0x2018, 0x21) < 0);
	/* Power up: a boost fault, no done, no lock: each stops, still muted. */
	fresh(); o = on(); assert(au_on_prepare(&o, false) == AU_ON_OK); fault_at_pup = true; assert(au_on_power_up(&o) == AU_ON_FAULT && amp[0x6000 / 4] == AU_ON_MUTE);
	fresh(); o = on(); assert(au_on_prepare(&o, false) == AU_ON_OK); no_pup = true; assert(au_on_power_up(&o) == AU_ON_PUP && o.pup_ms == 100);
	fresh(); o = on(); assert(au_on_prepare(&o, false) == AU_ON_OK); no_pll = true; assert(au_on_power_up(&o) == AU_ON_PLL);
	/* OTP's bits: LSB first across words. */
	{
		uint32_t words[32] = {0}, bit = 28;
		words[0] = 0xf0000000u; words[1] = 0x5;
		assert(au_otp_take(words, &bit, 8) == 0x5f && bit == 36);
		bit = 1023; assert(au_otp_take(words, &bit, 4) == 0);
		uint32_t total = 0;
		for (unsigned i = 0; i < 99; i++) total += au_otp_map1[i].size;
		assert(total == 688);
	}
	puts("au_amp_on: order, limits, readback, stopping at the first thing wrong");
	return 0;
}
"""


def main():
    with tempfile.TemporaryDirectory(prefix='fuxi-amp-on-') as tmp:
        source = Path(tmp) / 'on.c'
        source.write_text(DRIVER)
        program = Path(tmp) / 'on'
        subprocess.run([os.environ.get('CC', 'cc'), '-std=gnu11', '-Wall', '-Wextra', '-Wno-unused-function', '-Wno-unused-parameter', '-g', '-O1',
                        '-fsanitize=address,undefined', '-fno-sanitize-recover=all', f'-I{ROOT / "shim/fuxi-probe"}', str(source), '-o', str(program)], check=True)
        subprocess.run([str(program)], check=True)
    audio = (ROOT / 'shim/fuxi-probe/audio.c').read_text()
    # Nothing compiles this into the loader yet; when something does, it comes with the owner's own say.
    assert 'au_amp_on.h' not in audio
    print('PASS: au_amp_on')


main()
