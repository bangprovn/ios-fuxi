#!/usr/bin/env python3
"""xnu/fuxi-audio/fuxi_audio_dsp.c, the ADSP's kernel thread: it builds against stubs of what it takes from XNU, stays behind its boot argument, and reaches no pin or amplifier."""
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
STUB = r"""
#include <stdint.h>
#include <stddef.h>
typedef uintptr_t vm_offset_t;
typedef uintptr_t vm_size_t;
typedef int wait_result_t;
typedef int kern_return_t;
typedef void *thread_t;
typedef void *event_t;
typedef void (*thread_continue_t)(void *, wait_result_t);
#define KERN_SUCCESS 0
#define THREAD_UNINT 0
#define THREAD_CONTINUE_NULL ((thread_continue_t)0)
#define NSEC_PER_MSEC 1000000ull
int printf(const char *, ...);
void assert_wait_timeout(event_t, int, uint32_t, uint32_t);
int thread_block(thread_continue_t);
uint64_t mach_absolute_time(void);
void absolutetime_to_nanoseconds(uint64_t, uint64_t *);
thread_t current_thread(void);
void thread_deallocate(thread_t);
kern_return_t kernel_thread_start(thread_continue_t, void *, thread_t *);
int PE_parse_boot_argn(const char *, void *, int);
__attribute__((noreturn)) void panic(const char *, ...);
typedef int dev_t;
typedef void *proc_t;
typedef char *caddr_t;
struct uio;
long uio_resid(struct uio *);
int uiomove(caddr_t, int, struct uio *);
struct cdevsw { void *d_open, *d_close, *d_read, *d_write, *d_ioctl, *d_stop, *d_reset, *d_ttys, *d_select, *d_mmap, *d_strategy, *d_reserved_1, *d_reserved_2; int d_type; };
int cdevsw_add(int, const struct cdevsw *);
void *devfs_make_node(dev_t, int, int, int, int, const char *);
#define makedev(a, b) ((a) << 24 | (b))
#define minor(d) ((d) & 0xff)
#define ENXIO 6
#define DEVFS_CHAR 1
#define UID_ROOT 0
#define GID_WHEEL 0
int eno_rdwrt(void), eno_ioctl(void), eno_stop(void), eno_reset(void), eno_select(void), eno_mmap(void), eno_strat(void), eno_getc(void), eno_putc(void);
"""


HAPTIC_RUN = r"""
#include <math.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef char *caddr_t;
typedef int wait_result_t;
typedef void *event_t;
typedef void (*thread_continue_t)(void *, wait_result_t);
#define THREAD_UNINT 0
#define THREAD_CONTINUE_NULL ((thread_continue_t)0)
#define NSEC_PER_MSEC 1000000ull
#define pragma_unused(...)
struct uio { const uint8_t *p; long left; };
static long uio_resid(struct uio *u) { return u->left; }
static int uiomove(caddr_t dst, int n, struct uio *u) { memcpy(dst, u->p, (size_t)n); u->p += n; u->left -= n; return 0; }
static uint64_t now;
static uint64_t mach_absolute_time(void) { return now; }
static void absolutetime_to_nanoseconds(uint64_t t, uint64_t *ns) { *ns = t; }
static void assert_wait_timeout(event_t e, int i, uint32_t a, uint64_t b) { (void)e; (void)i; (void)a; (void)b; }
static int thread_block(thread_continue_t c) { (void)c; return 0; }
#include "haptics.c"
/* what the plug-in sends: writes of 480 frames */
static void put(const int16_t *s, unsigned n) {
 for (unsigned at = 0; at < n; at += 480) {
  struct uio u = { (const uint8_t *)(s + at), (long)((n - at < 480 ? n - at : 480) * 2) };
  now += 10000000ull;
  if (fh_write(&u) != 0 || u.left != 0) abort();
 }
}
static int16_t wave[48000 * 2];
int main(void) {
 unsigned n = 0;
 (void)fh_thread; (void)fh_on; (void)fh_opens;
 n += 4800;                                                        /* 100 ms of silence */
 for (unsigned i = 0; i < 480; i++) wave[n + i] = (int16_t)(20000 * sin(2 * M_PI * 160 * i / 48000.0));  /* a tap: 10 ms at 160 Hz */
 n += 480 + 4800;
 for (unsigned i = 0; i < 19200; i++) wave[n + i] = (int16_t)(15000 * sin(2 * M_PI * 145 * i / 48000.0)); /* a vibration: 400 ms at 145 Hz */
 n += 19200 + 4800;
 for (unsigned i = 0; i < 960; i++) wave[n + i] = (int16_t)((i & 1) ? 200 : -200);                /* under the threshold: nothing */
 n += 960 + 2400;
 put(wave, n);
 if (fh_head != 2 || fh_lost != 0 || fh_in) { printf("events %u lost %u in %d\n", fh_head, fh_lost, fh_in); return 1; }
 for (unsigned k = 0; k < 2; k++) {
  const struct fh_event *e = &fh_queue[k];
  unsigned ms = e->frames / 48, hz = (unsigned)((uint64_t)e->crossings_loud * 24000u / e->frames), rms = fh_isqrt(e->sum_sq_loud / e->frames);
  printf("event %u: ms %u peak %u rms %u hz %u\n", e->number, ms, e->peak, rms, hz);
 }
 return 0;
}
"""


def haptic_events(tmp):
    """The haptics sink's events, from a tap and a vibration written as the plug-in writes them."""
    text = (ROOT / 'xnu/fuxi-audio/fuxi_audio_dsp.c').read_text()
    (tmp / 'haptics.c').write_text(text[text.index('/* ---- the haptics sink, fuxihaptic=1'):text.index('/* ---- the bottom amplifier')])
    (tmp / 'run.c').write_text(HAPTIC_RUN)
    exe = tmp / 'run'
    subprocess.run([os.environ.get('CC', 'cc'), '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-Wno-unknown-pragmas', '-Wno-unused-parameter', '-fsanitize=address,undefined', '-g',
                    str(tmp / 'run.c'), '-lm', '-o', str(exe)], check=True)
    out = subprocess.run([str(exe)], capture_output=True, text=True, check=True).stdout
    lines = [l.split() for l in out.splitlines() if l.startswith('event ')]
    assert len(lines) == 2, out
    tap, vib = ({k: int(v) for k, v in zip(l[2::2], l[3::2])} for l in lines)
    assert 9 <= tap['ms'] <= 10 and 19000 <= tap['peak'] <= 20000 and 13000 <= tap['rms'] <= 14500 and 120 <= tap['hz'] <= 200, tap
    assert 398 <= vib['ms'] <= 400 and 14900 <= vib['peak'] <= 15000 and 10300 <= vib['rms'] <= 10700 and 140 <= vib['hz'] <= 150, vib
    print('haptics sink: ' + '; '.join(out.splitlines()))


def main():
    with tempfile.TemporaryDirectory(prefix='fuxi-dsp-') as tmp:
        stubs = Path(tmp)
        for header in ('sys/param.h', 'sys/systm.h', 'kern/clock.h', 'kern/thread.h', 'kern/sched_prim.h', 'machine/machine_routines.h', 'pexpert/pexpert.h', 'sys/conf.h', 'sys/errno.h', 'sys/proc.h', 'sys/uio.h', 'miscfs/devfs/devfs.h'):
            (stubs / header).parent.mkdir(parents=True, exist_ok=True)
            (stubs / header).write_text('#include "stub.h"\n')
        (stubs / 'stub.h').write_text('#pragma once\n' + STUB)
        for name, source in (('fuxi_au_glink.h', 'au_glink.h'), ('fuxi_au_amp.h', 'au_amp.h'), ('fuxi_au_amp_on.h', 'au_amp_on.h'), ('fuxi_au_mic.h', 'au_mic.h')):
            (stubs / name).write_text((ROOT / 'shim/fuxi-probe' / source).read_text())
        subprocess.run([os.environ.get('CC', 'cc'), '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-Wno-unknown-pragmas', '-Wno-unused-function', '-Wno-unused-parameter', '-Wno-incompatible-pointer-types', '-DFUXI=1', '-fsyntax-only',
                        f'-I{stubs}', f'-I{ROOT / "xnu/fuxi-audio"}', str(ROOT / 'xnu/fuxi-audio/fuxi_audio_dsp.c')], check=True)
    text = (ROOT / 'xnu/fuxi-audio/fuxi_audio_dsp.c').read_text()
    lower = text.lower()
    # Nothing reaches a pin or an amplifier but the section behind fuxiaudioamp=1, and that keeps to what the loader's tone did: the bottom one, the lowest level.
    amp = text[text.index('/* ---- the bottom amplifier'):text.index("/* What has come, or silence, into the next")]
    rest = (text.replace(amp, '')).lower()
    for word in ('i2s1', 'geni', 'cs35l41', '0x15000000', 'smmu_', 'au_on_'):
        assert word not in rest, word
    assert amp.count('au_on_unmute(') == 1 and 'au_on_unmute(&fa_amp, fa_amp_level)' in amp and 'fa_amp.address = 0x40;' in amp and '0x42' not in amp
    assert 'fa_amp_pin(182, 1u | 1u << 9, 0);' in amp and 'fa_amp_pin(182, 1u | 1u << 9, 1)' not in amp and ', 68' not in amp and 'fa_amp_pin(68' not in amp
    assert 'if (!fa_amp_wanted ||' in amp and 'PE_parse_boot_argn("fuxiaudioamp", &amp, sizeof(amp)) && fa_amp_asked(amp)' in text and 'static unsigned fa_amp_level;' in amp and '(unsigned)amp > AU_ON_LEVELS' in amp
    assert text.count('ml_io_map(') == 9 and 'PE_parse_boot_argn("fuxiaudio"' in text
    # The mic: behind fuxiaudio=3 or 4, its registers behind the votes, and no vote of a rail: the driver of the RSC is Wi-Fi's alone. Nothing in it reaches the amplifier.
    mic = text[text.index('/* ---- the microphone, fuxiaudio=3'):text.index('/* Minor 0 the speaker')]
    assert '#define AU_MIC_RPMH(addr, data) 0u' in mic and 'if (on >= 3) {' in text and 'if (!fa_mic_on) {' in mic
    for word in ('fa_amp', 'au_on_', '0x17a2', 'fuxi_ar_write'):
        assert word not in mic, word
    up = mic[mic.index('fa_mic_up(void)'):mic.index('fa_mic_down(void)')]
    assert up.index('fa_votes_take()') < up.index('au_mic_run(&fa_mic)') and up.index('"mic clock"') < up.index('au_mic_run(&fa_mic)')
    down = mic[mic.index('fa_mic_down(void)'):mic.index('fa_mic_pump(void)')]
    assert down.index('au_mic_down(&fa_mic)') < down.index('fa_votes_drop()') and down.index('au_mic_down(&fa_mic)') < down.index('"mic clock off"')
    assert 'fa_votes++ != 0' in text and '--fa_votes != 0' in text and text.count('au_gl_prm_step(') == 2
    # The haptics sink: behind fuxihaptic=1 alone, a node and a thread, and nothing in it reaches a register, a mapping or the DSP.
    hap = text[text.index('/* ---- the haptics sink, fuxihaptic=1'):text.index('/* ---- the bottom amplifier')]
    for word in ('volatile', 'ml_io_map', '_wr(', '_rd(', 'au_', 'fa_amp', 'fa_mic', 'fuxi_ar_', 'fa_g.', 'smmu', 'spmi', '0x7f'):
        assert word not in hap.lower(), word
    assert 'fh_on = PE_parse_boot_argn("fuxihaptic", &haptic, sizeof(haptic)) && haptic == 1;' in text and '"fuxihaptic") != NULL' in text
    assert 'if (minor(dev) == 2) {\n\t\treturn fh_write(uio);' in text
    with tempfile.TemporaryDirectory(prefix='fuxi-haptic-') as tmp:
        haptic_events(Path(tmp))
    subprocess.run(['python3', str(ROOT / 'tools/sync-fuxi-audio.py'), '--check'], check=True, capture_output=True)
    hook = (ROOT / 'xnu/patches/0118-fuxi-audio-dsp.patch').read_text()
    assert 'fuxi_audio_dsp_init();' in hook and 'bsd/dev/fuxi_audio_dsp.c' in hook and 'bsd/dev/fuxi_audioreach.c' in hook
    print('PASS: fuxi_audio_dsp')


main()
