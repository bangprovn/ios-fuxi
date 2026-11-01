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
void thread_terminate(thread_t);
thread_t current_thread(void);
void thread_deallocate(thread_t);
kern_return_t kernel_thread_start(thread_continue_t, void *, thread_t *);
int PE_parse_boot_argn(const char *, void *, int);
"""


def main():
    with tempfile.TemporaryDirectory(prefix='fuxi-dsp-') as tmp:
        stubs = Path(tmp)
        for header in ('sys/param.h', 'sys/systm.h', 'kern/clock.h', 'kern/thread.h', 'kern/sched_prim.h', 'machine/machine_routines.h', 'pexpert/pexpert.h'):
            (stubs / header).parent.mkdir(parents=True, exist_ok=True)
            (stubs / header).write_text('#include "stub.h"\n')
        (stubs / 'stub.h').write_text('#pragma once\n' + STUB)
        (stubs / 'fuxi_au_glink.h').write_text((ROOT / 'shim/fuxi-probe/au_glink.h').read_text())
        subprocess.run([os.environ.get('CC', 'cc'), '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-Wno-unknown-pragmas', '-Wno-unused-function', '-Wno-unused-parameter', '-DFUXI=1', '-fsyntax-only',
                        f'-I{stubs}', f'-I{ROOT / "xnu/fuxi-audio"}', str(ROOT / 'xnu/fuxi-audio/fuxi_audio_dsp.c')], check=True)
    text = (ROOT / 'xnu/fuxi-audio/fuxi_audio_dsp.c').read_text()
    lower = text.lower()
    for word in ('i2s1', '0xf100000', 'tlmm', 'geni', 'cs35l41', '0x880000', '0x15000000', 'smmu_'):
        assert word not in lower, word
    assert text.count('ml_io_map(') == 4 and 'PE_parse_boot_argn("fuxiaudio"' in text
    subprocess.run(['python3', str(ROOT / 'tools/sync-fuxi-audio.py'), '--check'], check=True, capture_output=True)
    hook = (ROOT / 'xnu/patches/0118-fuxi-audio-dsp.patch').read_text()
    assert 'fuxi_audio_dsp_init();' in hook and 'bsd/dev/fuxi_audio_dsp.c' in hook and 'bsd/dev/fuxi_audioreach.c' in hook
    print('PASS: fuxi_audio_dsp')


main()
