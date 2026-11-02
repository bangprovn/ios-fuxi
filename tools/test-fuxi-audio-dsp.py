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
    subprocess.run(['python3', str(ROOT / 'tools/sync-fuxi-audio.py'), '--check'], check=True, capture_output=True)
    hook = (ROOT / 'xnu/patches/0118-fuxi-audio-dsp.patch').read_text()
    assert 'fuxi_audio_dsp_init();' in hook and 'bsd/dev/fuxi_audio_dsp.c' in hook and 'bsd/dev/fuxi_audioreach.c' in hook
    print('PASS: fuxi_audio_dsp')


main()
