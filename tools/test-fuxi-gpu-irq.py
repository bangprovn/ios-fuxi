#!/usr/bin/env python3
"""GPU fence interrupt (fuxigpuncimport bit 26): the actual fuxi_gpu_irq.inc and the WaitFence spin loop on host models.

1. fuxi_gpu_irq.inc with kernel-shaped declarations, compiled and assembled for arm64 under xnu's -Weverything list (-Werror).
2. The same source against a modelled GICv3 distributor and RBBM: opt-in, attach sequence and readback, CPU binding order, source
   clear of only bit 20, storm masks (empty run, burst, foreign SPI, no delivery, isolation), sleep race/timeout/written, mask repair.
3. FuxiAdrenoUserClient::spin: interrupt sleeps only while the fence is unwritten, polls otherwise, retire stamp and first-poll flag.
No hardware, SDK or GIC is touched; device interrupt delivery is not observed here.
"""
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
XNU_WARN = ('-Weverything -Wno-pedantic -Wno-bad-function-cast -Wno-bitwise-instead-of-logical -Wno-c++-compat -Wno-c++98-compat '
            '-Wno-conditional-uninitialized -Wno-covered-switch-default -Wno-disabled-macro-expansion -Wno-documentation-unknown-command '
            '-Wno-extra-semi-stmt -Wno-format-non-iso -Wno-language-extension-token -Wno-missing-variable-declarations -Wno-packed '
            '-Wno-padded -Wno-partial-availability -Wno-reserved-id-macro -Wno-shift-sign-overflow -Wno-switch-enum -Wno-unaligned-access '
            '-Wno-undef -Wno-unused-macros -Wno-used-but-marked-unused -Wno-variadic-macros -Wno-vla -Wno-zero-length-array '
            '-Wno-unknown-warning-option -Wno-anon-enum-enum-conversion -Wno-error=declaration-after-statement -Wno-sign-compare '
            '-Wno-sign-conversion -Wno-declaration-after-statement -Wno-unsafe-buffer-usage -Wno-reserved-identifier -Wno-reserved-macro-identifier '
            '-Wno-pre-c11-compat -Wno-c23-compat -Wno-missing-noreturn').split()

PREFIX = r'''
#include <stdint.h>
#include "fuxi_gpu_abi.h"
int printf(const char *, ...) __attribute__((format(printf, 1, 2)));
typedef int kern_return_t; typedef void *event_t; typedef int wait_result_t; typedef int wait_interrupt_t; typedef unsigned boolean_t;
typedef struct processor *processor_t; typedef void (*thread_continue_t)(void *, wait_result_t);
#define KERN_SUCCESS 0
#ifndef NULL
#define NULL ((void *)0)
#endif
#define FALSE 0u
#define THREAD_UNINT 0
#define THREAD_AWAKENED 0
#define THREAD_TIMED_OUT 1
#define THREAD_CONTINUE_NULL ((thread_continue_t)0)
#define NSEC_PER_USEC 1000ull
#define NSEC_PER_MSEC 1000000ull
#define GPU_REG_BASE 0x03d00000ULL
#define G3_FENCE_PA 0x80000000ULL
#define FG_ABI_FENCE_OFF 0x100u
#define FG_GUARD int fg_guard_ __attribute__((unused, cleanup(fg_unguard))) = fg_guard()
struct fg_window { uint64_t pa, size; uintptr_t va; };
static unsigned fg_abi_live, fg_abi_poisoned, fg_access_failed, fg_irq_want, fg_ready = 1, guards, guardDepth;
static uint32_t rbbm[0x400], fenceMem;
static uint8_t gicd[0x10000] __attribute__((aligned(4096)));
static uint64_t fakeMpidr = 0x281000000ULL, fakeNow, waitDeadline;
static unsigned cpuCount = 7, bound[16], binds, installs[16], installSource, wakes, waits, mapFail, rbbmPtrFail, blockResult, typerLines = 31;
static void (*installed)(void *, void *, void *, int);
static int fg_guard(void) { guards++; guardDepth++; return 0; }
static void fg_unguard(int *g) { (void)g; guardDepth--; }
static int fuxi_gpu_ready(void) { return (int)fg_ready; }
static kern_return_t fg_map_window(struct fg_window *w) { if (mapFail || w->va) return 1; w->va = (uintptr_t)gicd; return KERN_SUCCESS; }
uintptr_t fuxi_gpu_nc_pointer(uint64_t pa, uint64_t bytes);
uintptr_t fuxi_gpu_nc_pointer(uint64_t pa, uint64_t bytes) { (void)bytes; return rbbmPtrFail ? 0 : (uintptr_t)&rbbm[(pa - GPU_REG_BASE)/4]; }
static uint32_t g3_gr(unsigned reg) { return rbbm[reg]; }
static int g3_gw(unsigned reg, uint32_t v) { if (fg_access_failed) return 0; if (reg == 0x37) rbbm[0x201] &= ~v; rbbm[reg] = v; return 1; }
static uint32_t g3_read(uint64_t pa) { return pa == G3_FENCE_PA + FG_ABI_FENCE_OFF + 4 ? fenceMem : 0; }
#define rsr(r) (fakeMpidr + 0*sizeof(r))
static void delay(unsigned us) { fakeNow += us*1000; }
static unsigned ml_get_cpu_count(void) { return cpuCount; }
static unsigned processors[16];
static processor_t cpu_to_processor(int i) { return (processor_t)(void *)&processors[i]; }
static processor_t thread_bind(processor_t p) { if (p) bound[binds++ % 16] = (unsigned)((unsigned *)(void *)p - processors); return 0; }
static unsigned blockPending;
static wait_result_t thread_block(thread_continue_t c) { (void)c; if (!blockPending) return THREAD_AWAKENED; blockPending = 0; return (int)blockResult; }
static void ml_install_interrupt_handler(void *nub, int source, void *target, void (*h)(void *, void *, void *, int), void *ref)
{ (void)nub; (void)target; (void)ref; installs[bound[(binds - 1) % 16]]++; installSource = (unsigned)source; installed = h; }
static wait_result_t assert_wait_deadline(event_t e, wait_interrupt_t i, uint64_t d) { (void)e; (void)i; waits++; waitDeadline = d; blockPending = 1; return 0; }
static void clock_interval_to_deadline(unsigned n, uint64_t scale, uint64_t *d) { *d = fakeNow + n*scale; }
static uint64_t mach_absolute_time(void) { return fakeNow; }
static void nanoseconds_to_absolutetime(uint64_t ns, uint64_t *t) { *t = ns; }
kern_return_t thread_wakeup_prim(event_t e, boolean_t one, wait_result_t r);
kern_return_t thread_wakeup_prim(event_t e, boolean_t one, wait_result_t r) { (void)e; (void)one; (void)r; wakes++; if (blockPending) { blockPending = 0; blockResult = THREAD_AWAKENED; } return 0; }
'''

USE = r'''
void fg_irq_use(void);
void fg_irq_use(void) { fg_irq_quiesce(); (void)typerLines; }
'''

TEST = r'''
#include <assert.h>
#include <stdio.h>
#include <string.h>
static uint32_t gd(unsigned off) { uint32_t v; memcpy(&v, gicd + off, 4); return v; }
static void gdw(unsigned off, uint32_t v) { memcpy(gicd + off, &v, 4); }
static void reset(void)
{
 memset(gicd, 0, sizeof(gicd)); memset(rbbm, 0, sizeof(rbbm)); memset(installs, 0, sizeof(installs));
 gdw(0, 0x10u|3u); gdw(4, typerLines); gdw(0xc00 + (332/16)*4, ~0u); /* every line edge before attach: level must be set */
 fg_irq_state = fg_irq_ok = fg_irq_inside = fg_irq_why = 0; fg_irq_gicd.va = 0; fg_irq_empty_run = fg_irq_burst = 0; fg_irq_window = 0;
 memset(fg_irq_stats, 0, sizeof(fg_irq_stats)); binds = 0; fg_abi_live = 1; fg_ready = 1; fg_access_failed = 0; fg_abi_poisoned = 0; fg_irq_want = 1;
 rbbm[0x38] = 0; mapFail = rbbmPtrFail = 0; installed = 0; fakeNow = 1000000000ull;
}
int main(void)
{
 unsigned w = (332/32)*4; uint32_t bit = 1u << (332%32); uint64_t s[FG_IRQ_STATS]; unsigned why;
 /* Off by default: nothing mapped, nothing installed, waiters poll. */
 reset(); fg_irq_want = 0;
 assert(!fuxi_gpu_irq_setup() && !fg_irq_gicd.va && !installed && fuxi_gpu_irq_stats(s, &why) == 0 && fuxi_gpu_irq_sleep(0, 1, 1000) == -1);
 /* Attach refusals leave the SPI disabled, the RBBM mask untouched and are not retried. */
 reset(); fg_abi_live = 0; assert(!fuxi_gpu_irq_setup() && fg_irq_state == 2 && !installed && rbbm[0x38] == 0 && !(gd(0x100 + w) & bit) && !fg_irq_ok);
 assert(!fuxi_gpu_irq_setup() && fuxi_gpu_irq_stats(s, &why) == 2);
 reset(); gdw(0, 3u); assert(!fuxi_gpu_irq_setup() && !installed);           /* ARE_NS clear */
 reset(); typerLines = 9; reset(); assert(!fuxi_gpu_irq_setup()); typerLines = 31; /* 320 lines < INTID 332 */
 reset(); rbbmPtrFail = 1; assert(!fuxi_gpu_irq_setup() && !installed);
 reset(); fg_access_failed = 1; assert(!fuxi_gpu_irq_setup() && !(gd(0x100 + w) & bit));
 /* Readback mismatch (RWP never clears): masked, not armed. */
 reset(); gdw(0, 0x10u|0x80000000u); assert(!fuxi_gpu_irq_setup() && (gd(0x180 + w) & bit) && !fg_irq_ok && rbbm[0x38] == 0); gdw(0, 0x10u);
 /* Attach: handler on every CPU, CPU 0 last; stale bit 20 cleared; only bit 20 unmasked; group 1, priority 0xa0, level, routed to CPU 0. */
 reset(); rbbm[0x201] = (1u<<20)|2u; rbbm[0x38] = 0;
 assert(fuxi_gpu_irq_setup() == 1 && fg_irq_state == 1 && fg_irq_ok && installSource == 332 && installed);
 for (unsigned i = 0; i < cpuCount; i++) assert(installs[i] == 1);
 assert(binds == cpuCount); for (unsigned i = 0; i < cpuCount; i++) assert(bound[i] == cpuCount - 1 - i); /* CPU 0 last: its MPIDR is the route */
 assert(rbbm[0x38] == (1u<<20) && rbbm[0x201] == 2u && fg_irq_mask_was == 0);
 assert((gd(0x80 + w) & bit) && gicd[0x400 + 332] == 0xa0 && !(gd(0xc00 + (332/16)*4) & (1u << ((332%16)*2+1))) && (gd(0x100 + w) & bit));
 { uint64_t r; memcpy(&r, gicd + 0x6000 + 8*332, 8); assert(r == (fakeMpidr & 0xff00ffffffULL) && r == 0x200000000ULL); }
 assert(fuxi_gpu_irq_setup() == 1 && fuxi_gpu_irq_stats(s, &why) == 1 && !why);
 assert(gd(0x180 + w) & bit); gdw(0x180 + w, 0); /* attach disables before configuring; later masks are checked from here */
 /* Handler: clears only bit 20 (fault bits stay for g3_faulted), counts, wakes. */
 rbbm[0x201] = (1u<<20)|0x40u; rbbm[0x37] = 0; uint32_t seq = fuxi_gpu_irq_seq(); wakes = 0;
 installed(0, 0, 0, 332);
 assert(rbbm[0x37] == (1u<<20) && fuxi_gpu_irq_seq() == seq + 1 && wakes == 1 && fg_irq_stats[FG_IRQ_N] == 1 && !fg_irq_stats[FG_IRQ_EMPTY]);
 /* A foreign SPI is masked at the distributor; a PPI is ignored. */
 installed(0, 0, 0, 100); assert((gd(0x180 + (100/32)*4) & (1u << (100%32))) && fg_irq_stats[FG_IRQ_FOREIGN] == 1);
 installed(0, 0, 0, 27); assert(fg_irq_stats[FG_IRQ_FOREIGN] == 2 && fg_irq_state == 1);
 /* Empty run: 999 empty interrupts in a row keep it, one with the bit resets the run, 1000 in a row mask (why 1). */
 rbbm[0x201] = 0; for (unsigned i = 0; i < 999; i++) { fakeNow += 1000000; installed(0, 0, 0, 332); } assert(fg_irq_state == 1);
 rbbm[0x201] = 1u<<20; installed(0, 0, 0, 332); rbbm[0x201] = 0;
 for (unsigned i = 0; i < 999; i++) { fakeNow += 1000000; installed(0, 0, 0, 332); } assert(fg_irq_state == 1 && !(gd(0x180 + w) & bit));
 installed(0, 0, 0, 332); assert(fg_irq_state == 3 && fg_irq_why == 1 && (gd(0x180 + w) & bit));
 assert(fuxi_gpu_irq_stats(s, &why) == 3 && why == 1 && s[FG_IRQ_EMPTY] == 1999 && fuxi_gpu_irq_sleep(fuxi_gpu_irq_seq(), 1, 1000) == -1);
 /* Burst: 5000 non-empty interrupts within 100 ms mask (why 2); the same rate spread over windows does not. */
 reset(); assert(fuxi_gpu_irq_setup()); gdw(0x180 + w, 0);
 for (unsigned i = 0; i < 4999; i++) { rbbm[0x201] = 1u<<20; fakeNow += 10000; installed(0, 0, 0, 332); } assert(fg_irq_state == 1);
 fakeNow += 100000001; for (unsigned i = 0; i < 4999; i++) { rbbm[0x201] = 1u<<20; installed(0, 0, 0, 332); } assert(fg_irq_state == 1);
 rbbm[0x201] = 1u<<20; installed(0, 0, 0, 332); assert(fg_irq_state == 3 && fg_irq_why == 2);
 /* Sleep: written fence polls (-2), seq moved before the assert is a race (woken at once), interrupt wakes, timeout. */
 reset(); assert(fuxi_gpu_irq_setup()); fenceMem = 7;
 waits = 0; assert(fuxi_gpu_irq_sleep(fuxi_gpu_irq_seq(), 7, 1000) == -2 && fuxi_gpu_irq_sleep(fuxi_gpu_irq_seq(), 3, 1000) == -2 && !waits && fg_irq_stats[FG_IRQ_WRITTEN] == 2);
 seq = fuxi_gpu_irq_seq(); rbbm[0x201] = 1u<<20; installed(0, 0, 0, 332); wakes = 0;
 assert(fuxi_gpu_irq_sleep(seq, 8, 1000) == 1 && waits == 1 && wakes == 1 && !blockPending && fg_irq_stats[FG_IRQ_RACES] == 1);
 assert(waitDeadline == fakeNow + 1000000);
 blockResult = THREAD_TIMED_OUT; assert(fuxi_gpu_irq_sleep(fuxi_gpu_irq_seq(), 8, 1000) == 0 && fg_irq_stats[FG_IRQ_TIMEOUTS] == 1);
 blockResult = THREAD_AWAKENED; assert(fuxi_gpu_irq_sleep(fuxi_gpu_irq_seq(), 8, 1000) == 1 && fg_irq_stats[FG_IRQ_WAKES] == 1);
 fenceMem = 5; assert(fuxi_gpu_irq_sleep(fuxi_gpu_irq_seq(), 0xfffffffeull, 1000) == -2); /* 32-bit wrap compare */
 fenceMem = 7; fg_abi_live = 0; assert(fuxi_gpu_irq_sleep(fuxi_gpu_irq_seq(), 8, 1000) == -2); fg_abi_live = 1;
 /* Many timeouts after an interrupt was seen keep it armed. */
 blockResult = THREAD_TIMED_OUT; for (unsigned i = 0; i < 100; i++) assert(fuxi_gpu_irq_sleep(fuxi_gpu_irq_seq(), 8, 1000) == 0); assert(fg_irq_state == 1);
 /* No delivery: 64 timed-out sleeps without one interrupt mask (why 4). */
 reset(); assert(fuxi_gpu_irq_setup()); gdw(0x180 + w, 0);
 for (unsigned i = 0; i < 63; i++) assert(fuxi_gpu_irq_sleep(fuxi_gpu_irq_seq(), 8, 1000) == 0);
 assert(fg_irq_state == 1); assert(fuxi_gpu_irq_sleep(fuxi_gpu_irq_seq(), 8, 1000) == 0 && fg_irq_state == 3 && fg_irq_why == 4 && (gd(0x180 + w) & bit));
 blockResult = THREAD_AWAKENED;
 /* RBBM_INT_0_MASK lost (GPU reinit) is restored by the periodic report, under the GPU lock. */
 reset(); assert(fuxi_gpu_irq_setup()); rbbm[0x38] = 0; guards = 0;
 assert(fuxi_gpu_irq_stats(s, &why) == 1 && rbbm[0x38] == (1u<<20) && s[FG_IRQ_MASKFIX] == 1 && guards == 1 && !guardDepth);
 assert(fuxi_gpu_irq_stats(s, &why) == 1 && s[FG_IRQ_MASKFIX] == 1);
 /* Isolation: no GPU access from the handler afterwards, masked (why 3), waiters woken. */
 wakes = 0; fg_irq_quiesce(); assert(!fg_irq_ok && fg_irq_state == 3 && fg_irq_why == 3 && wakes == 1 && (gd(0x180 + w) & bit));
 rbbm[0x201] = 1u<<20; rbbm[0x37] = 0; installed(0, 0, 0, 332); assert(rbbm[0x37] == 0 && rbbm[0x201] == (1u<<20));
 wakes = 0; fg_irq_quiesce(); assert(!wakes); /* idempotent */
 reset(); fg_irq_quiesce(); assert(fg_irq_state == 0); /* never armed: nothing to do */
 puts("PASS: actual fuxi_gpu_irq.inc: opt-in, refusals, GICD attach/readback/routing, per-CPU install order, bit-20-only clear, empty/burst/foreign/no-delivery/isolation masks, sleep race/timeout/written/wrap, mask repair");
 return 0;
}
'''

SPIN = r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
typedef int IOReturn;
enum { kIOReturnSuccess = 0, kIOReturnTimeout = 1, kIOReturnIOError = 2 };
enum { FG_ABI_STAT_SLEEP = 19 };
static uint64_t now;
static uint64_t mach_absolute_time() { return now; }
static void nanoseconds_to_absolutetime(uint64_t ns, uint64_t *t) { *t = ns; }
static int polls[64], npoll, pollAt, irqArmed, sleepRet[64], nsleep, sleepAt, pauses, stats, setups;
static uint32_t seqNow, sleepSeq[64];
static int fuxi_gpu_abi_poll(uint64_t) { now += 1000; return pollAt < npoll ? polls[pollAt++] : 0; }
static int fuxi_gpu_irq_setup() { setups++; return irqArmed; }
static uint32_t fuxi_gpu_irq_seq() { return seqNow; }
static int fuxi_gpu_irq_sleep(uint32_t seq, uint64_t fence, unsigned us) { assert(fence == 9 && us == 1000); sleepSeq[sleepAt] = seq; now += 50000; return sleepRet[sleepAt++]; }
static void abiPause() { pauses++; now += 100000; }
static void abiStat(unsigned slot, uint64_t) { assert(slot == FG_ABI_STAT_SLEEP); stats++; }
struct Client {
 static void waitStat(uint64_t, uint64_t) {}
%s
};
static void reset(int armed) { npoll = pollAt = nsleep = sleepAt = pauses = stats = setups = 0; irqArmed = armed; seqNow = 40; now = 1; }
int main() {
 Client c; uint64_t retired = 0; bool first = false;
 /* Ready at the first poll: no setup, no sleep, first-poll flag. */
 reset(1); assert(c.spin(9, 1000000000, &retired, &first) == kIOReturnSuccess && first && retired == 1001 && !setups && !pauses);
 /* Off: polls every waitPollUs as before. */
 reset(0); polls[0] = polls[1] = 1; npoll = 2; assert(c.spin(9, 1000000000, &retired, &first) == kIOReturnSuccess && !first && pauses == 2 && setups == 1 && !sleepAt);
 /* Armed: setup once, re-poll, then interrupt sleeps (seq read before each poll); written (-2) falls back to one poll period. */
 reset(1); for (int i = 0; i < 5; i++) polls[i] = 1; npoll = 5; sleepRet[0] = 0; sleepRet[1] = 1; sleepRet[2] = -2; sleepRet[3] = 1;
 assert(c.spin(9, 1000000000, &retired, &first) == kIOReturnSuccess && setups == 1 && sleepAt == 4 && pauses == 1 && stats == 3 && sleepSeq[0] == 40);
 /* Not armed any more (masked mid-wait): poll for the rest of the wait, no further sleeps. */
 reset(1); for (int i = 0; i < 4; i++) polls[i] = 1; npoll = 4; sleepRet[0] = -1;
 assert(c.spin(9, 1000000000, &retired, &first) == kIOReturnSuccess && sleepAt == 1 && pauses == 3);
 /* Errors and the deadline still end the wait. */
 reset(1); polls[0] = -3; npoll = 1; assert(c.spin(9, 1000000000, &retired, &first) == kIOReturnTimeout);
 reset(1); polls[0] = 1; polls[1] = -2; npoll = 2; assert(c.spin(9, 1000000000, &retired, &first) == kIOReturnIOError);
 reset(1); for (int i = 0; i < 64; i++) { polls[i] = 1; sleepRet[i] = 0; } npoll = 64; assert(c.spin(9, 200000, &retired, &first) == kIOReturnTimeout && sleepAt < 8);
 puts("PASS: actual WaitFence spin: interrupt sleep while unwritten, poll when written/unarmed/off, one setup, retire stamp, errors and deadline");
}
'''


def main():
    irq = (ROOT/'xnu/fuxi-adreno/fuxi_gpu_irq.inc').read_text()
    # Enable bit: fuxigpuncimport bit 26 (67108864) only; 22 DPU SMMU survey, 23-24 DPU, 25 PAC self-test belong to other lanes.
    backend = (ROOT/'xnu/fuxi-adreno/fuxi_gpu_abi_backend.inc').read_text()
    sets = re.findall(r'fg_irq_want\s*=([^;]*);', backend + irq)
    assert sets == ['(bits>>26)&1'], sets
    for bit in range(22, 26):
        assert not re.search(rf'fg_irq\w*\s*=[^;]*bits\s*>>\s*{bit}\b', backend + irq), bit
    assert 'bit 26 = 67108864' in irq
    client = (ROOT/'xnu/fuxi-adreno/FuxiAdrenoUserClient.cpp').read_text()
    spin = client[client.index(' IOReturn spin('):client.index(' static void waitStat(')]
    inc = '-I' + str(ROOT/'xnu/fuxi-adreno')
    with tempfile.TemporaryDirectory() as tmp:
        strict = Path(tmp)/'strict.c'
        strict.write_text(PREFIX + irq + USE)
        # arm64 Darwin: uint64_t is unsigned long long there, as in the kernel's printf formats.
        subprocess.run(['clang', '-target', 'arm64-apple-darwin', '-ffreestanding', '-std=gnu11', '-O2', '-c', '-Werror', *XNU_WARN, inc, str(strict), '-o', str(strict.with_suffix('.o'))], check=True)
        test = Path(tmp)/'irq.c'
        test.write_text(PREFIX + irq.replace('"dsb sy"', '""').replace('"yield"', '""') + TEST)  # barriers are arm64-only; assembled above
        exe = test.with_suffix('')
        subprocess.run(['clang', '-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function', '-Wno-format', '-fsanitize=address,undefined', inc, str(test), '-o', str(exe)], check=True)
        subprocess.run([str(exe)], check=True)
        model = Path(tmp)/'spin.cpp'
        model.write_text(SPIN % spin)
        exe = model.with_suffix('')
        subprocess.run(['clang++', '-std=c++17', '-O1', '-g', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined', str(model), '-o', str(exe)], check=True)
        subprocess.run([str(exe)], check=True)
    print('PASS: enable is fuxigpuncimport bit 26 only (22-25 untouched); fuxi_gpu_irq.inc compiles and assembles for arm64 under the xnu -Weverything list with -Werror (kernel-shaped declarations)')


if __name__ == '__main__':
    main()
