#!/usr/bin/env python3
"""Tests for the ath12k link check (design M0): gates, edits, R2, and what must stay untouched.

The gates run on small fixtures and need no external tree. With --deps (a directory holding the
linux and freebsd checkouts) the edits are also matched against the real files, the PAGE_SIZE
assertion is shown to fire, and with --full the whole build runs clean.
"""
import argparse
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
# sha256 of fuxi_wlan.c at fa93d15, before the port: what metal4 and wlan6g must keep compiling.
METAL4_WLAN_C = '0c69a14561985958d904c535047a8eb6037e0beaca6499f708f61d6f287c94f7'


def load_build():
    spec = importlib.util.spec_from_file_location('build_ath12k', ROOT / 'tools/ci/build-ath12k.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build = load_build()
checks = []


def check(function):
    checks.append(function)
    return function


def refused(call, *args, text=''):
    try:
        call(*args)
    except build.Stop as stop:
        assert text in str(stop), f'wrong refusal: {stop}'
        return
    raise AssertionError(f'{call.__name__} let it through')


def fixture_linux(tmp):
    base = tmp / 'linux' / build.ATH12K
    (base / 'wifi7').mkdir(parents=True)
    (tmp / 'linux/include/linux').mkdir(parents=True)
    (tmp / 'linux/include/linux/mhi.h').write_text('/* SPDX-License-Identifier: GPL-2.0 */\n')
    return tmp / 'linux', base


@check
def license_gate_needs_the_identifier(tmp):
    linux, base = fixture_linux(tmp)
    (base / 'good.c').write_text(f'// {build.SPDX}\n')
    (base / 'gpl.c').write_text('// SPDX-License-Identifier: GPL-2.0-only\n')
    build.license_gate(linux, ('good.c',))
    refused(build.license_gate, linux, ('gpl.c',), text='does not carry')
    refused(build.license_gate, linux, ('absent.c',), text='is missing')


@check
def linux_include_is_refused(tmp):
    linux, base = fixture_linux(tmp)
    build.refuse_linux_include(linux, [base, tmp / 'elsewhere'])
    refused(build.refuse_linux_include, linux, [linux / 'include'], text='refused')
    refused(build.refuse_linux_include, linux, [linux], text='refused')


@check
def dependency_audit_sees_what_was_read(tmp):
    linux, base = fixture_linux(tmp)
    edited = tmp / 'work/src'
    edited.mkdir(parents=True)
    ours = tmp / 'ours'
    ours.mkdir()
    (base / 'a.h').write_text(f'// {build.SPDX}\n')
    (edited / 'a.c').write_text(f'// {build.SPDX}\n')
    (ours / 'b.h').write_text('/* SPDX-License-Identifier: MIT */\n')
    (ours / 'dual.h').write_text('/* SPDX-License-Identifier: GPL-2.0 OR MIT */\n')
    (ours / 'gnu.h').write_text('under the GNU General Public License\n')
    roots = [edited, ours]

    def audit(*read):
        dep = tmp / 'x.d'
        dep.write_text('x.o: ' + ' \\\n '.join(str(p) for p in read) + '\n')
        return build.audit_dependencies(linux, edited, roots, [dep])

    assert audit(base / 'a.h', edited / 'a.c', ours / 'b.h') == 3
    refused(audit, linux / 'include/linux/mhi.h', text='outside ath12k')
    refused(audit, ours / 'dual.h', text='GPL text')
    refused(audit, ours / 'gnu.h', text='GPL text')
    (tmp / 'stray.h').write_text('')
    refused(audit, tmp / 'stray.h', text='outside the allowed')
    (edited / 'nolicense.h').write_text('')
    refused(audit, edited / 'nolicense.h', text='does not carry')


@check
def edits_must_match_exactly_once(tmp):
    source = tmp / 's.c'
    target = tmp / 'out/s.c'
    source.write_text('one two two\n')
    locate = (lambda tree, rel: source, lambda tree, rel: target)
    edit = {'id': 'x', 'tree': 'ath12k', 'file': 's.c', 'find': 'one', 'replace': 'ONE'}
    assert build.apply_edits([edit], *locate) == 1
    assert target.read_text() == 'ONE two two\n'
    refused(build.apply_edits, [dict(edit, find='two')], *locate, text='found 2')
    refused(build.apply_edits, [dict(edit, find='three')], *locate, text='found 0')


@check
def edit_list_is_well_formed(tmp):
    edits = json.loads((ROOT / 'tools/ath12k-edits.json').read_text())['edits']
    ids = [e['id'] for e in edits]
    assert len(ids) == len(set(ids))
    for edit in edits:
        assert set(edit) == {'id', 'why', 'tree', 'file', 'find', 'replace'}, edit['id']
        assert edit['tree'] in ('ath12k', 'freebsd') and edit['why'] and edit['find'] != edit['replace']
    for wanted in ('ath12k-rfkill-pin', 'ath12k-reo-qdesc-alloc', 'ath12k-rx-refill-guard-a', 'ath12k-mac-override', 'ath12k-stage-htc',
                   'ath12k-stage-registered', 'ath12k-stage-quiet', 'ath12k-no-eht', 'ath12k-wmi-event-log', 'ath12k-stage-up',
                   'ath12k-knob-hw-mode', 'ath12k-knob-idle-ps', 'ath12k-vdev-created', 'ath12k-scan-vdev-ready', 'ath12k-scan-link',
                   'ath12k-no-11d', 'ath12k-wmi-command'):
        assert wanted in ids, wanted


@check
def undefined_symbols_are_checked_both_ways(tmp):
    allow = {'groups': {'kernel': {'optional': True, 'symbols': ['memcpy', 'memset']},
                        'ours': {'symbols': ['lx_os_lock']}}}
    groups, unknown, stale = build.check_undefined(['memcpy', 'lx_os_lock'], allow)
    assert groups == {'kernel': ['memcpy'], 'ours': ['lx_os_lock']} and not unknown and not stale
    assert build.check_undefined(['memcpy', 'lx_os_lock', 'mhi_alloc'], allow)[1] == ['mhi_alloc']
    assert build.check_undefined(['memcpy'], allow)[2] == ['lx_os_lock']
    real = json.loads((ROOT / 'tools/ath12k-undefined-allow.json').read_text())
    assert all(name.startswith(build.EXPORTED_PREFIX) for name in real['exported'])
    listed = [s for group in real['groups'].values() for s in group['symbols']]
    assert len(listed) == len(set(listed))
    forbidden = ('mhi_', 'qmi_', 'qrtr_', 'ieee80211_', 'cfg80211_', 'linuxkpi_', 'ath12k_')
    assert not [s for s in listed if s.startswith(forbidden)], 'these must be defined in the object'


@check
def kobj_headers_declare_methods(tmp):
    text = '''#include <sys/bus.h>
INTERFACE demo;
HEADER {
	typedef int demo_kind_t;
};
CODE { static int never(void) { return 0; } };
/* comment */
METHOD int probe {
	device_t dev;
	const char *name;
} DEFAULT never;
STATICMETHOD void classy {
	kobj_class_t cls;
};
'''
    out = build.kobj_header(text)
    assert 'typedef int demo_kind_t;' in out and 'never' not in out
    assert 'typedef int demo_probe_t(device_t dev, const char *name);' in out
    assert 'KOBJOPLOOKUP(((kobj_t)dev)->ops,demo_probe);' in out
    assert 'rc = ((demo_probe_t *) _m)(dev, name);' in out
    assert 'KOBJOPLOOKUP(cls->ops,demo_classy);' in out


@check
def own_files_have_notices_and_lf(tmp):
    own = ROOT / 'xnu/fuxi-ath12k'
    assert (own / 'compat/LICENSE.hackios').read_text().startswith('MIT License')
    assert 'hakios-xnu-kernel' in (own / 'compat/PROVENANCE.md').read_text()
    files = [p for p in own.rglob('*') if p.is_file()] + [
        ROOT / 'tools/ci/build-ath12k.py', ROOT / 'tools/ath12k-edits.json',
        ROOT / 'tools/ath12k-undefined-allow.json', ROOT / 'tools/test-fuxi-ath12k.py', ROOT / 'tools/ci/pins.env']
    for path in files:
        data = path.read_bytes()
        assert b'\r' not in data, f'CR in {path}'
        assert build.SPDX.encode() not in data or path.name == 'build-ath12k.py', f'Linux text committed? {path}'
    for path in own.rglob('*.[ch]'):
        text = path.read_text()
        assert 'SPDX-License-Identifier: GPL' not in text, path


@check
def page_size_assertions_are_in_place(tmp):
    prelude = (ROOT / 'xnu/fuxi-ath12k/compat/include/lx_prelude.h').read_text()
    assert '_Static_assert(PAGE_SIZE == 4096' in prelude
    assert '_Static_assert(ATH12K_PAGE_SIZE == 4096' in (ROOT / 'xnu/fuxi-ath12k/src/fa_hif.c').read_text()


def without_ath12k(text):
    """What the C is without FUXI_ATH12K: guarded blocks are whole lines, and never nest."""
    out, inside = [], False
    for line in text.splitlines(True):
        if line == '#ifdef FUXI_ATH12K\n':
            assert not inside
            inside = True
        elif line == '#endif /* FUXI_ATH12K */\n':
            assert inside
            inside = False
        elif not inside:
            out.append(line)
    assert not inside
    return ''.join(out)


@check
def metal4_compiles_the_same_c(tmp):
    """fuxi_wlan.c without its guarded blocks is byte for byte the file before the port, and nothing in it
    depends on line numbers, so metal4's object code is unchanged. A deliberate change to the C the other
    kernels compile must change this digest on purpose."""
    text = (ROOT / 'xnu/fuxi-wlan/fuxi_wlan.c').read_text()
    assert hashlib.sha256(without_ath12k(text).encode()).hexdigest() == METAL4_WLAN_C
    assert 'FUXI_ATH12K' in text
    for word in ('__LINE__', '__FILE__', '__COUNTER__', 'assert(', 'panic('):
        assert word not in without_ath12k(text).replace('_Static_assert(', ''), word


def fake_builder(tmp):
    builder = tmp / 'dxb'
    (builder / 'patches').mkdir(parents=True)
    (builder / 'build.sh').write_text('ARCHS="x86_64 arm64e" VALID_ARCHS="x86_64 arm64e"\nwarning "Skipping patch ${PATCH}\n')
    return builder


@check
def only_metal4wifi_gets_patch_0117(tmp):
    script = ROOT / 'tools/ci/prepare-kernel.py'
    plain, ath = fake_builder(tmp / 'a'), fake_builder(tmp / 'b')
    subprocess.run([sys.executable, str(script), str(plain)], check=True)
    subprocess.run([sys.executable, str(script), str(ath), '--ath12k'], check=True)
    names = sorted(p.name for p in (plain / 'patches').iterdir())
    extra = sorted(set(p.name for p in (ath / 'patches').iterdir()) - set(names))
    assert '0099-fuxi-wlan.patch' in names and not [n for n in names if n.startswith('0117-')]
    assert extra == ['0117-fuxi-ath12k.patch', '0117-fuxi-ath12k.patch.fuxi-required']
    patch = (ROOT / 'xnu/patches/0117-fuxi-ath12k.patch').read_text()
    assert 'LD_KERNEL_ARCHIVES += $(FUXI_OBJECTS)' in patch and '-DFUXI_ATH12K=1' in patch
    assert 'bsd/dev/fuxi_ath12k_os.c\t\tstandard' in patch
    subprocess.run([sys.executable, str(ROOT / 'tools/sync-fuxi-ath12k.py'), '--check'], check=True, capture_output=True)
    for other in (ROOT / 'xnu/patches').glob('*.patch'):
        if other.name != '0117-fuxi-ath12k.patch':
            assert 'FUXI_OBJECTS' not in other.read_text(errors='surrogateescape'), other.name


@check
def ci_plumbing_adds_one_profile(tmp):
    def plan(kernel):
        result = subprocess.run(['bash', str(ROOT / 'tools/ci/build.sh'), '--plan', kernel, 'false', 'false', 'false'],
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)['target']
    assert plan('metal4wifi') == ['kernel-metal4wifi']
    assert plan('metal4') == ['kernel-metal4']
    assert plan('all') == ['kernel-metal4', 'kernel-wlan6g']
    workflow = (ROOT / '.github/workflows/fuxi-build.yml').read_text()
    assert 'options: [none, metal4, wlan6g, metal4e, metal4wifi, all]' in workflow and 'default: metal4\n' in workflow
    script = (ROOT / 'tools/ci/build.sh').read_text()
    assert 'unset FUXI_OBJECTS' in script and 'prepare_args=(--ath12k)' in script
    inputs = ROOT / 'tools/ci/kernel-inputs.py'
    for variant in ('metal4', 'metal4wifi'):
        subprocess.run([sys.executable, str(inputs), '--variant', variant, '--out', str(tmp / variant)], check=True, capture_output=True)
    base = (tmp / 'metal4/cmdline.txt').read_text()
    assert (tmp / 'metal4wifi/cmdline.txt').read_text() == base and 'fuxiath12k' not in base
    for stage in (1, 2, 4):
        assert not (tmp / f'metal4/cmdline-ath12k{stage}.txt').exists()
        line = (tmp / f'metal4wifi/cmdline-ath12k{stage}.txt').read_text()
        assert line == base.rstrip('\n') + f' fuxiath12k={stage}\n' and len(line.encode()) - 1 < 608
    # The way stage 4 is booted: the file's line plus the core mask, with every knob at its longest. Limit: tools/ci/kernel-inputs.py.
    line = (tmp / 'metal4wifi/cmdline-ath12k4.txt').read_text().rstrip('\n')
    assert len((line + ' fuxicores=0x7c fuxiath12kq=0x1b').encode()) < 608
    # Stage 5 has no name in its file: the test adds it. The longest name, the core mask, every knob and an address still fit.
    assert not (tmp / 'metal4/cmdline-ath12k5.txt').exists()
    line = (tmp / 'metal4wifi/cmdline-ath12k5.txt').read_text().rstrip('\n')
    assert line.endswith(' fuxiath12k=5') and 'fuxiwifissid' not in line and 'sweetsample' not in line and 'sweetscan' not in line
    longest = line + ' fuxicores=0x7c fuxiath12kq=0x1b fuxiwifimac=02:46:55:58:49:01 fuxiwifissid=' + 'x' * 32
    assert len(longest.encode()) < 608, len(longest.encode())


OS_STUBS = {
    'kern/clock.h': '#include <stddef.h>\n#include <stdint.h>\nuint64_t mach_absolute_time(void);\n'
                    'void absolutetime_to_nanoseconds(uint64_t, uint64_t *);\n'
                    'void nanoseconds_to_absolutetime(uint64_t, uint64_t *);\n',
    'kern/kalloc.h': 'void *kalloc_data(unsigned long, int);void kfree_data(void *, unsigned long);\n'
                     '#define Z_WAITOK 1\n#define Z_ZERO 2\n'
                     'void *fake_kalloc(unsigned long);void fake_kfree(void *);\n'
                     '#define kalloc_type(t, f) ((t *)fake_kalloc(sizeof(t)))\n#define kfree_type(t, p) fake_kfree(p)\n',
    'kern/locks.h': 'typedef struct { int x; } lck_mtx_t;typedef struct { int x; } lck_grp_t;\n'
                    '#define LCK_GRP_DECLARE(n, s) lck_grp_t n\n#define LCK_MTX_DECLARE(n, g) lck_mtx_t n\n'
                    'void lck_mtx_lock(lck_mtx_t *);void lck_mtx_unlock(lck_mtx_t *);\n'
                    '#define LCK_SLEEP_DEFAULT 0\n#define THREAD_UNINT 0\n'
                    'typedef void *event_t;int lck_mtx_sleep_deadline(lck_mtx_t *, int, event_t, int, unsigned long long);\n',
    'kern/sched_prim.h': 'int thread_block(void *);void thread_wakeup(void *);\n#define THREAD_CONTINUE_NULL ((void *)0)\n',
    'kern/thread.h': 'typedef void *thread_t;typedef int wait_result_t;typedef int kern_return_t;\n#define KERN_SUCCESS 0\n'
                     'thread_t current_thread(void);void thread_deallocate(thread_t);\n'
                     'kern_return_t kernel_thread_start(void (*)(void *, wait_result_t), void *, thread_t *);\n',
    'libkern/libkern.h': '', 'mach/mach_types.h': '',
    'pexpert/pexpert.h': 'int PE_parse_boot_argn(const char *, void *, int);\n',
    'sys/errno.h': '#define ENOMEM 12\n#define EAGAIN 35\n',
    'sys/random.h': 'void read_random(void *, unsigned);typedef unsigned u_int;\n',
    'sys/systm.h': 'int printf(const char *, ...);void panic(const char *, ...) __attribute__((noreturn));\n'
                   '#ifndef __unused\n#define __unused __attribute__((unused))\n#endif\n'
                   '#ifndef __dead2\n#define __dead2 __attribute__((noreturn))\n#endif\n',
}


@check
def os_file_implements_its_allow_list_group(tmp):
    """Compiles the XNU side on stub headers: every name the object wants from it is defined, and fuxi_wlan.c
    defines the rest, so a kernel with patches 0099 and 0117 leaves only libkern names for the link."""
    for name, text in OS_STUBS.items():
        path = tmp / 'stubs' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    own = ROOT / 'xnu/fuxi-ath12k'
    (tmp / 'stubs/fa_lx_os.h').write_bytes((own / 'compat/include/lx_os.h').read_bytes())
    obj = tmp / 'os.o'
    cc = os.environ.get('CC', 'cc')
    subprocess.run([cc, '-std=gnu11', '-Wall', '-Werror', '-Wno-unused-function', '-Wno-unused-variable', '-DFUXI', '-DFUXI_ATH12K=1',
                    f'-I{tmp / "stubs"}', f'-I{own / "src"}', '-c', str(own / 'os/fuxi_ath12k_os.c'), '-o', str(obj)], check=True)
    nm = subprocess.run(['nm', '-g', str(obj)], capture_output=True, text=True, check=True).stdout
    defined = {line.split()[-1].lstrip('_') for line in nm.splitlines() if ' T ' in line}
    groups = json.loads((ROOT / 'tools/ath12k-undefined-allow.json').read_text())['groups']
    missing = [s for s in groups['fuxi-ath12k-os']['symbols'] if s not in defined]
    assert not missing, missing
    assert {'fuxi_ath12k_mhi_lock', 'fuxi_ath12k_mhi_unlock'} <= defined
    wlan = (ROOT / 'xnu/fuxi-wlan/fuxi_wlan.c').read_text()
    for name in groups['fuxi-wlan']['symbols']:
        assert re.search(r'^(?:\w[\w ]*\*?\s*)?' + name + r'\(', wlan, re.M), name
    for prototype in ('fa_firmware_ready', 'fa_stop', 'fa_set_radio_mac'):
        assert prototype + '(' in wlan


def fake_ld(tmp, fail):
    """A stand-in `ld` for the Xcode route: fails the listed ways, otherwise copies its first object."""
    tool = tmp / 'ld'
    tool.write_text(f"""#!{sys.executable}
import shutil, sys
args = sys.argv[1:]
open({str(tmp / 'calls')!r}, 'a').write(' '.join(args) + chr(10))
classic, listed = '-ld_classic' in args, '-exported_symbols_list' in args
key = 'classic' if classic else 'list' if listed else 'plain'
if key in {sorted(fail)!r}:
    sys.stderr.write('ld: refused in the ' + key + ' way' + chr(10))
    sys.exit(1)
lines = open(args[args.index('-filelist') + 1]).read().split()
shutil.copyfile(lines[0], args[args.index('-o') + 1])
""")
    tool.chmod(0o755)
    return tool


@check
def xcode_link_tries_each_way_and_says_why(tmp):
    (tmp / 'a.o').write_text('object')
    objects = [tmp / 'a.o']

    class Tool:
        route = 'ld64'
    for fail, used, calls in ((set(), 'ld -r -exported_symbols_list', 1),
                              ({'list'}, 'ld -ld_classic -r -exported_symbols_list', 2),
                              ({'list', 'classic'}, 'ld -r with hidden visibility only', 3)):
        folder = tmp / ('w' + str(calls))
        folder.mkdir()
        Tool.ld = str(fake_ld(folder, fail))
        output = folder / 'out.o'
        assert build.link(Tool, objects, output, ['fa_stop'], folder) == used
        assert output.read_text() == 'object'
        written = (folder / 'calls').read_text().splitlines()
        assert len(written) == calls and all(' -r ' in f' {line} ' and '-arch arm64' in line for line in written)
        assert (folder / 'exported.txt').read_text() == '_fa_stop\n'
    folder = tmp / 'bad'
    folder.mkdir()
    Tool.ld = str(fake_ld(folder, {'list', 'classic', 'plain'}))
    try:
        build.link(Tool, objects, folder / 'out.o', ['fa_stop'], folder)
        raise AssertionError('link must fail')
    except build.Stop as stop:
        text = str(stop)
        assert 'never run before' in text and text.count('ld: refused') == 3 and 'hidden visibility' in text


# Just enough of LinuxKPI's shapes for fa_80211.c to run on the host. The real headers compile the same file
# in the object (--full), and they cannot run here: their atomics and machine headers are the phone's.
FAKE_HEADERS = {
    'linux/kernel.h': r"""#pragma once
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#ifndef __printflike
#define __printflike(a, b) __attribute__((format(printf, a, b)))
#endif
#ifndef __dead2
#define __dead2 __attribute__((noreturn))
#endif
#ifndef __containerof
#define __containerof(p, t, m) ((t *)((char *)(p) - offsetof(t, m)))
#endif
#ifndef roundup2
#define roundup2(x, y) (((x) + ((y) - 1)) & ~((__typeof__(x))(y) - 1))
#endif
typedef uint8_t u8;
typedef uint16_t u16;
typedef uint32_t u32;
typedef uint64_t u64;
struct list_head { struct list_head *next, *prev; };
static inline void INIT_LIST_HEAD(struct list_head *h) { h->next = h->prev = h; }
static inline bool list_empty(const struct list_head *h) { return h->next == h; }
static inline void list_add_tail(struct list_head *n, struct list_head *h) { n->prev = h->prev; n->next = h; h->prev->next = n; h->prev = n; }
static inline void list_del_init(struct list_head *n) { n->prev->next = n->next; n->next->prev = n->prev; INIT_LIST_HEAD(n); }
#define list_first_entry(h, t, m) __containerof((h)->next, t, m)
""",
    'linux/etherdevice.h': r"""#pragma once
#define ETH_ALEN 6
static inline bool is_zero_ether_addr(const u8 *a) { return !(a[0] | a[1] | a[2] | a[3] | a[4] | a[5]); }
""",
    'linux/random.h': r"""#pragma once
static inline void get_random_bytes(void *p, int n) { for (int i = 0; i < n; i++) ((uint8_t *)p)[i] = (uint8_t)i; }
""",
    'linux/skbuff.h': r"""#pragma once
#ifndef cpu_to_be16
#define cpu_to_be16(x) ((uint16_t)__builtin_bswap16(x))
#endif
struct sk_buff { uint8_t cb[48]; uint8_t *data; unsigned len; uint8_t *head; uint16_t protocol; uint32_t priority; };
extern int fail_skb;
static inline void dev_kfree_skb_any(struct sk_buff *s) { free(s->head); free(s); }
static inline struct sk_buff *dev_alloc_skb(unsigned n) {
    if (fail_skb) return NULL;
    struct sk_buff *s = calloc(1, sizeof(*s));
    s->head = s->data = malloc(n);
    return s;
}
static inline void skb_reserve(struct sk_buff *s, unsigned n) { s->data += n; }
static inline uint8_t *skb_put(struct sk_buff *s, unsigned n) { uint8_t *at = s->data + s->len; s->len += n; return at; }
static inline void skb_trim(struct sk_buff *s, unsigned n) { assert(n <= s->len); s->len = n; }
""",
    'linux/slab.h': r"""#pragma once
#define GFP_KERNEL 0
static inline void *kzalloc(size_t n, int f) { return calloc(1, n); }
static inline void *kmalloc(size_t n, int f) { return malloc(n); }
static inline void kfree(const void *p) { free((void *)p); }
""",
    'linux/workqueue.h': r"""#pragma once
struct workqueue_struct { int x; };
struct work_struct { void (*fn)(struct work_struct *); int queued, cancelled; };
struct delayed_work { struct work_struct work; };
extern int fail_workqueue, queues_destroyed;
static inline struct workqueue_struct *alloc_ordered_workqueue(const char *n, int f) { return fail_workqueue ? NULL : calloc(1, sizeof(struct workqueue_struct)); }
static inline void destroy_workqueue(struct workqueue_struct *q) { queues_destroyed++; free(q); }
static inline void INIT_WORK(struct work_struct *w, void (*f)(struct work_struct *)) { w->fn = f; w->queued = w->cancelled = 0; }
static inline bool queue_work(struct workqueue_struct *q, struct work_struct *w) { w->queued++; return true; }
static inline bool queue_delayed_work(struct workqueue_struct *q, struct delayed_work *w, int d) { w->work.queued++; return true; }
static inline bool cancel_work_sync(struct work_struct *w) { w->cancelled++; return false; }
""",
    'net/cfg80211.h': r"""#pragma once
#include <linux/etherdevice.h>
#ifndef nitems
#define nitems(x) (sizeof(x) / sizeof((x)[0]))
#endif
enum nl80211_band { NL80211_BAND_2GHZ, NL80211_BAND_5GHZ, NL80211_BAND_6GHZ, NUM_NL80211_BANDS };
enum nl80211_iftype { NL80211_IFTYPE_UNSPECIFIED, NL80211_IFTYPE_STATION };
enum { NL80211_REGDOM_SET_BY_USER, NL80211_REGDOM_SET_BY_DRIVER };
enum { IEEE80211_CHAN_DISABLED = 1, IEEE80211_CHAN_NO_IR = 2, IEEE80211_CHAN_RADAR = 8 };
enum { NL80211_RRF_NO_IR = 1, NL80211_RRF_DFS = 2 };
#define WIPHY_FLAG_NOTIFY_REGDOM_BY_DRIVER (1u << 24)
struct linuxkpi_ieee80211_channel {
    signed char psd; enum nl80211_band band; uint32_t center_freq, flags, orig_flags; int max_power, max_reg_power, max_antenna_gain, orig_mpwr;
};
struct ieee80211_rate { int bitrate; };
enum nl80211_chan_width { NL80211_CHAN_WIDTH_20 = 1 };
struct cfg80211_chan_def { struct linuxkpi_ieee80211_channel *chan; enum nl80211_chan_width width; uint32_t center_freq1; };
struct ieee80211_sta_ht_cap { bool ht_supported; uint8_t ampdu_density, ampdu_factor; uint16_t cap; uint8_t mcs[16]; };
struct ieee80211_sta_vht_cap { bool vht_supported; uint32_t cap; uint8_t vht_mcs[8]; };
struct ieee80211_sta_he_cap {
    bool has_he; struct { uint8_t mac_cap_info[6], phy_cap_info[11]; } he_cap_elem;
    struct { uint16_t rx_mcs_80, tx_mcs_80, rx_mcs_160, tx_mcs_160, rx_mcs_80p80, tx_mcs_80p80; } he_mcs_nss_supp;
    uint8_t ppe_thres[25];
};
struct ieee80211_sband_iftype_data { enum nl80211_iftype types_mask; struct ieee80211_sta_he_cap he_cap; struct { bool has_eht; } eht_cap; };
struct ieee80211_supported_band {
    struct linuxkpi_ieee80211_channel *channels; struct ieee80211_rate *bitrates; struct ieee80211_sband_iftype_data *iftype_data;
    int n_channels, n_bitrates, n_iftype_data; enum nl80211_band band;
    struct ieee80211_sta_ht_cap ht_cap; struct ieee80211_sta_vht_cap vht_cap;
};
static inline const struct ieee80211_sta_he_cap *
ieee80211_get_he_iftype_cap(const struct ieee80211_supported_band *band, enum nl80211_iftype type) {
    for (int i = 0; i < band->n_iftype_data; i++) {
        if (band->iftype_data[i].types_mask == type) return &band->iftype_data[i].he_cap;
    }
    return NULL;
}
struct ieee80211_reg_rule {
    uint32_t flags; signed char psd;
    struct { int start_freq_khz, end_freq_khz; } freq_range;
    struct { int max_eirp, max_antenna_gain; } power_rule;
};
struct linuxkpi_ieee80211_regdomain { uint8_t alpha2[2]; int n_reg_rules; struct ieee80211_reg_rule reg_rules[]; };
struct regulatory_request { uint8_t alpha2[2]; int initiator, dfs_region, user_reg_hint_type; };
struct fake_mutex { int locked; };
struct wiphy {
    struct fake_mutex mtx; uint32_t flags; struct ieee80211_supported_band *bands[NUM_NL80211_BANDS]; uint8_t perm_addr[ETH_ALEN];
    const struct linuxkpi_ieee80211_regdomain *regd; void (*reg_notifier)(struct wiphy *, struct regulatory_request *);
    uint32_t rts_threshold, frag_threshold; int retry_long, retry_short;
    uint8_t priv[0] __attribute__((aligned(64)));
};
struct wiphy_work { struct list_head entry; void (*fn)(struct wiphy *, struct wiphy_work *); };
#define mutex_init(m) ((m)->locked = 0)
static inline void wiphy_lock(struct wiphy *w) { assert(!w->mtx.locked); w->mtx.locked = 1; }
static inline void wiphy_unlock(struct wiphy *w) { assert(w->mtx.locked); w->mtx.locked = 0; }
enum ieee80211_bss_type { IEEE80211_BSS_TYPE_ANY }; enum ieee80211_privacy { IEEE80211_PRIVACY_ANY };
struct cfg80211_bss_ies { uint8_t *data; size_t len; };
struct cfg80211_bss { struct cfg80211_bss_ies *ies, *beacon_ies; int32_t signal; };
struct cfg80211_scan_info { bool aborted; };
struct cfg80211_ssid { int ssid_len; uint8_t ssid[32]; };
struct cfg80211_scan_request {
    struct wiphy *wiphy; int ie_len; uint8_t *ie; int n_ssids; int n_channels; struct cfg80211_ssid *ssids;
    struct linuxkpi_ieee80211_channel *channels[0];
};
""",
    'net/mac80211.h': r"""#pragma once
#include <linux/skbuff.h>
#include <linux/workqueue.h>
#define D80211_TODO 1
struct ieee80211_vif; struct ieee80211_sta; struct ieee80211_txq; struct ieee80211_chanctx_conf; struct napi_struct;
struct ieee80211_hw { struct wiphy *wiphy; void *priv; size_t vif_data_size, sta_data_size, chanctx_data_size; unsigned long flags; uint16_t max_rx_aggregation_subframes; struct { uint32_t listen_interval; } conf; };
enum ieee80211_sta_state { IEEE80211_STA_NOTEXIST, IEEE80211_STA_NONE, IEEE80211_STA_AUTH, IEEE80211_STA_ASSOC, IEEE80211_STA_AUTHORIZED };
enum { IEEE80211_STA_RX_BW_20 = 1 }; enum { IEEE80211_SMPS_OFF = 1 };
enum { IEEE80211_AC_VO, IEEE80211_AC_VI, IEEE80211_AC_BE, IEEE80211_AC_BK };
enum { BSS_CHANGED_ASSOC = 1, BSS_CHANGED_ERP_PREAMBLE = 8, BSS_CHANGED_ERP_SLOT = 16, BSS_CHANGED_BASIC_RATES = 64, BSS_CHANGED_BEACON_INT = 128,
       BSS_CHANGED_BSSID = 256, BSS_CHANGED_QOS = 512, BSS_CHANGED_BEACON_INFO = 1024 };
enum { IEEE80211_TX_STAT_ACK = 2 };
struct ieee80211_chanctx_conf { struct cfg80211_chan_def def, min_def; uint8_t rx_chains_dynamic, rx_chains_static; uint8_t drv_priv[0] __attribute__((aligned(64))); };
struct ieee80211_key_conf;
struct ieee80211_tx_info { uint32_t flags; enum nl80211_band band; struct { struct ieee80211_vif *vif; struct ieee80211_key_conf *hw_key; } control; };
#define IEEE80211_SKB_CB(s) ((struct ieee80211_tx_info *)((s)->cb))
struct ieee80211_tx_control { struct ieee80211_sta *sta; };
struct ieee80211_tx_queue_params { uint16_t cw_min, cw_max, txop; uint8_t aifs; int acm; };
struct ieee80211_link_sta {
    struct ieee80211_sta *sta; uint8_t addr[ETH_ALEN]; uint32_t supp_rates[NUM_NL80211_BANDS]; struct ieee80211_sta_ht_cap ht_cap;
    struct ieee80211_sta_vht_cap vht_cap; struct ieee80211_sta_he_cap he_cap; uint8_t rx_nss; int bandwidth, smps_mode;
};
struct ieee80211_sta {
    uint8_t addr[ETH_ALEN]; uint16_t aid; bool wme; struct ieee80211_link_sta deflink; struct ieee80211_link_sta *link[15];
    uint8_t drv_priv[0] __attribute__((aligned(64)));
};
enum { IEEE80211_HW_SUPPORTS_TX_ENCAP_OFFLOAD = 1, IEEE80211_HW_SUPPORTS_RX_DECAP_OFFLOAD = 2 };
#define ieee80211_hw_check(hw, flag) (((hw)->flags & IEEE80211_HW_ ## flag) != 0)
enum { IEEE80211_OFFLOAD_ENCAP_ENABLED = 1, IEEE80211_OFFLOAD_DECAP_ENABLED = 2 };
enum { RX_FLAG_NO_SIGNAL_VAL = 1 << 19, RX_FLAG_8023 = 1 << 27, RX_FLAG_DECRYPTED = 1 << 1, RX_FLAG_IV_STRIPPED = 1 << 4 };
enum set_key_cmd { SET_KEY, DISABLE_KEY };
enum { IEEE80211_KEY_FLAG_PAIRWISE = 1 };
#define WLAN_CIPHER_SUITE_CCMP 0x000fac04
struct ieee80211_key_conf { uint32_t cipher; uint8_t icv_len, iv_len, hw_key_idx, keyidx; uint16_t flags; int8_t link_id; uint8_t keylen; uint8_t key[0]; };
struct ieee80211_rx_status { uint32_t flag; uint16_t freq; int8_t signal; };
#define IEEE80211_SKB_RXCB(s) ((struct ieee80211_rx_status *)((s)->cb))
struct ieee80211_bss_conf {
    struct ieee80211_vif *vif; const uint8_t *bssid; uint8_t addr[ETH_ALEN]; struct { struct cfg80211_chan_def oper; } chanreq;
    struct ieee80211_chanctx_conf *chanctx_conf; uint16_t beacon_int, ht_operation_mode; int assoc_capability; uint8_t dtim_period;
    bool use_short_slot, use_short_preamble, qos, he_support; uint32_t basic_rates; struct { uint32_t params; } he_oper;
};
struct ieee80211_vif_cfg { uint16_t aid; bool assoc; size_t ssid_len; uint8_t ssid[32]; uint8_t ap_addr[ETH_ALEN]; };
struct ieee80211_vif {
    enum nl80211_iftype type; int offload_flags; uint8_t addr[ETH_ALEN]; struct ieee80211_vif_cfg cfg; struct ieee80211_bss_conf bss_conf; struct ieee80211_bss_conf *link_conf[15];
    uint8_t drv_priv[0] __attribute__((aligned(64)));
};
struct ieee80211_scan_request { struct { int x; } ies; struct cfg80211_scan_request req; };
enum ieee80211_ampdu_mlme_action { IEEE80211_AMPDU_RX_START, IEEE80211_AMPDU_RX_STOP };
struct ieee80211_ampdu_params { struct ieee80211_sta *sta; enum ieee80211_ampdu_mlme_action action; uint16_t buf_size, timeout, ssn; uint8_t tid; bool amsdu; };
struct ieee80211_ops {
    int (*start)(struct ieee80211_hw *); void (*stop)(struct ieee80211_hw *, bool);
    int (*add_interface)(struct ieee80211_hw *, struct ieee80211_vif *); void (*remove_interface)(struct ieee80211_hw *, struct ieee80211_vif *);
    int (*hw_scan)(struct ieee80211_hw *, struct ieee80211_vif *, struct ieee80211_scan_request *); void (*cancel_hw_scan)(struct ieee80211_hw *, struct ieee80211_vif *);
    int (*add_chanctx)(struct ieee80211_hw *, struct ieee80211_chanctx_conf *); void (*remove_chanctx)(struct ieee80211_hw *, struct ieee80211_chanctx_conf *);
    int (*assign_vif_chanctx)(struct ieee80211_hw *, struct ieee80211_vif *, struct ieee80211_bss_conf *, struct ieee80211_chanctx_conf *);
    void (*unassign_vif_chanctx)(struct ieee80211_hw *, struct ieee80211_vif *, struct ieee80211_bss_conf *, struct ieee80211_chanctx_conf *);
    int (*sta_state)(struct ieee80211_hw *, struct ieee80211_vif *, struct ieee80211_sta *, enum ieee80211_sta_state, enum ieee80211_sta_state);
    void (*link_info_changed)(struct ieee80211_hw *, struct ieee80211_vif *, struct ieee80211_bss_conf *, u64);
    void (*vif_cfg_changed)(struct ieee80211_hw *, struct ieee80211_vif *, u64);
    int (*conf_tx)(struct ieee80211_hw *, struct ieee80211_vif *, u32, u16, const struct ieee80211_tx_queue_params *);
    void (*flush)(struct ieee80211_hw *, struct ieee80211_vif *, u32, bool);
    void (*tx)(struct ieee80211_hw *, struct ieee80211_tx_control *, struct sk_buff *);
    int (*set_key)(struct ieee80211_hw *, enum set_key_cmd, struct ieee80211_vif *, struct ieee80211_sta *, struct ieee80211_key_conf *);
    int (*ampdu_action)(struct ieee80211_hw *, struct ieee80211_vif *, struct ieee80211_ampdu_params *);
};
struct ieee80211_tx_status { struct sk_buff *skb; struct ieee80211_tx_info *info; };
enum ieee80211_iface_iter { IEEE80211_IFACE_ITER_NORMAL };
""",
}

FA_80211_DRIVER = r"""
#include "fa_80211.c"
int lx_os_thread(const char *name, void (*function)(void *), void *argument) { return 0; }
int fail_workqueue, queues_destroyed, fail_skb;
static char said[8192];
static unsigned locks, notified, notified_initiator, rddm;
static unsigned long long now = 1;
static uint8_t notified_alpha2[2];
void lx_tell(const char *f, ...) { va_list l; va_start(l, f); vsnprintf(said + strlen(said), sizeof(said) - strlen(said), f, l); va_end(l); }
long lx_stub(const char *n) { return 0; }
void lx_pool_peaks(unsigned *rings, unsigned *slots) { *rings = 0; *slots = 7; }
void lx_os_lock(void) { assert(!locks); locks++; }
void lx_os_unlock(void) { assert(locks); locks--; }
void lx_os_sleep(const void *a, unsigned long long n) { abort(); }
void lx_os_wakeup(const void *a) {}
unsigned long long lx_os_nanoseconds(void) { return now; }
void fuxi_ath12k_force_rddm(void) { rddm++; }
/* Stage 4 has its own driver below: nothing here reaches these. */
void fa_m4_credits(const char *w) { abort(); }
void fa_m4_rx_ring(const char *w) { abort(); }
void fa_m5_rx_ring(const char *w) { abort(); }
void fa_m4_scan_started(void) { abort(); }
void fa_m4_evidence(const char *w) { abort(); }
bool fa_m4_trouble(void) { abort(); }
void fa_m4_firmware_dead(void) { abort(); }
static void notifier(struct wiphy *w, struct regulatory_request *r) {
    assert(!locks);
    notified++; notified_initiator = r->initiator; memcpy(notified_alpha2, r->alpha2, 2);
}
static struct linuxkpi_ieee80211_channel two[3], five[2];
static struct ieee80211_rate rates[1];
static struct ieee80211_sband_iftype_data data[1];
static struct ieee80211_supported_band b2, b5;
#define SAID(text) (strstr(said, text) != NULL)
int main(void) {
    fail_workqueue = 1; assert(!linuxkpi_ieee80211_alloc_hw(32, NULL)); fail_workqueue = 0;
    struct ieee80211_hw *hw = linuxkpi_ieee80211_alloc_hw(256, NULL);
    struct wiphy *w = hw->wiphy;
    assert(hw && !((uintptr_t)hw->priv & 63) && linuxkpi_wiphy_to_ieee80211_hw(w) == hw);
    assert(w->rts_threshold == (uint32_t)-1 && w->frag_threshold == (uint32_t)-1 && w->retry_short == 7 && w->retry_long == 4);
    memset(hw->priv, 0xa5, 256);

    /* The address: nothing given leaves the firmware's. */
    u8 a[6] = {1, 2, 3, 4, 5, 6}; static const u8 ours[6] = {2, 0x46, 0x55, 0x58, 0x49, 1};
    fa_radio_mac(a); assert(a[0] == 1 && SAID("no station address given"));
    fa_set_radio_mac(ours); fa_radio_mac(a); assert(!memcmp(a, ours, 6));

    /* Refused, each with a line: no address, no bands, an empty band, EHT. */
    said[0] = 0; assert(linuxkpi_80211_wiphy_register(w) == -EINVAL && SAID("refused: no address"));
    memcpy(w->perm_addr, ours, 6);
    said[0] = 0; assert(linuxkpi_80211_wiphy_register(w) == -EINVAL && SAID("refused: no bands"));
    w->bands[NL80211_BAND_2GHZ] = &b2;
    said[0] = 0; assert(linuxkpi_80211_wiphy_register(w) == -EINVAL && SAID("refused: band 0 has 0 channels"));
    two[0] = (struct linuxkpi_ieee80211_channel){ .center_freq = 2412, .max_power = 30 };
    two[1] = (struct linuxkpi_ieee80211_channel){ .center_freq = 2472, .max_power = 10 };
    two[2] = (struct linuxkpi_ieee80211_channel){ .center_freq = 2484, .max_power = 30, .flags = IEEE80211_CHAN_DISABLED };
    five[0] = (struct linuxkpi_ieee80211_channel){ .center_freq = 5180, .max_power = 30 };
    five[1] = (struct linuxkpi_ieee80211_channel){ .center_freq = 5500, .max_power = 30 };
    b2 = (struct ieee80211_supported_band){ .channels = two, .n_channels = 3, .bitrates = rates, .n_bitrates = 1, .iftype_data = data, .n_iftype_data = 1 };
    data[0].types_mask = NL80211_IFTYPE_STATION;
    b5 = (struct ieee80211_supported_band){ .channels = five, .n_channels = 2, .bitrates = rates, .n_bitrates = 1, .band = NL80211_BAND_2GHZ };
    w->bands[NL80211_BAND_5GHZ] = &b5;
    data[0].he_cap.has_he = true; data[0].eht_cap.has_eht = true;
    said[0] = 0; assert(linuxkpi_80211_wiphy_register(w) == -EINVAL && SAID("eht 1") && SAID("refused: EHT must not be offered"));
    data[0].eht_cap.has_eht = false;
    said[0] = 0; assert(linuxkpi_80211_wiphy_register(w) == 0);
    assert(SAID("wiphy 2 GHz channels 3 rates 1 he 1 eht 0") && SAID("wiphy 5 GHz channels 2 rates 1 he 0 eht 0"));
    assert(b5.band == NL80211_BAND_5GHZ && five[1].band == NL80211_BAND_5GHZ);
    assert(two[1].orig_mpwr == 10 && two[2].orig_flags == IEEE80211_CHAN_DISABLED);

    /* The firmware's domain: refused until attached, then a copy that outlives the caller's. */
    size_t size = sizeof(struct linuxkpi_ieee80211_regdomain) + 2 * sizeof(struct ieee80211_reg_rule);
    struct linuxkpi_ieee80211_regdomain *regd = calloc(1, size);
    regd->alpha2[0] = 'C'; regd->alpha2[1] = 'N'; regd->n_reg_rules = 2;
    regd->reg_rules[0].freq_range.start_freq_khz = 2402000; regd->reg_rules[0].freq_range.end_freq_khz = 2500000;
    regd->reg_rules[0].power_rule.max_eirp = 2000; regd->reg_rules[0].power_rule.max_antenna_gain = 300; regd->reg_rules[0].psd = 7;
    regd->reg_rules[1].freq_range.start_freq_khz = 5170000; regd->reg_rules[1].freq_range.end_freq_khz = 5250000;
    regd->reg_rules[1].power_rule.max_eirp = 3000; regd->reg_rules[1].flags = NL80211_RRF_NO_IR | NL80211_RRF_DFS;
    w->reg_notifier = notifier; w->flags = WIPHY_FLAG_NOTIFY_REGDOM_BY_DRIVER;
    assert(linuxkpi_regulatory_set_wiphy_regd_sync(w, NULL) == -EINVAL);
    assert(linuxkpi_regulatory_set_wiphy_regd_sync(w, regd) == -ENODEV && !notified && !w->regd);
    assert(linuxkpi_ieee80211_ifattach(hw) == 0 && SAID("radio registered; no interface"));
    assert(radio_of(hw)->regd_applied == 0);
    said[0] = 0; assert(linuxkpi_regulatory_set_wiphy_regd_sync(w, regd) == 0);
    memset(regd, 0, size); free(regd);
    assert(w->regd && w->regd->alpha2[0] == 'C' && w->regd->reg_rules[1].power_rule.max_eirp == 3000);
    assert(notified == 1 && notified_initiator == NL80211_REGDOM_SET_BY_DRIVER && notified_alpha2[1] == 'N');
    assert(radio_of(hw)->regd_applied == 1 && radio_of(hw)->regd_at == now);
    assert(SAID("regd CN rules 2: usable channels 2 GHz 2, 5 GHz 1, 6 GHz 0"));
    /* The channel ath12k disabled stays so although a rule covers it. Limits come from the rule and the channel. */
    assert(two[0].flags == 0 && two[0].max_reg_power == 20 && two[0].max_antenna_gain == 3 && two[0].max_power == 20 && two[0].psd == 7);
    assert(two[1].max_power == 10 && (two[2].flags & IEEE80211_CHAN_DISABLED));
    assert(five[0].flags == (IEEE80211_CHAN_NO_IR | IEEE80211_CHAN_RADAR) && five[1].flags == IEEE80211_CHAN_DISABLED);
    /* A later domain starts again from what ath12k set, not from the last one's flags. */
    regd = calloc(1, size); regd->alpha2[0] = '0'; regd->alpha2[1] = '0'; regd->n_reg_rules = 1;
    regd->reg_rules[0].freq_range.start_freq_khz = 5170000; regd->reg_rules[0].freq_range.end_freq_khz = 5510000;
    regd->reg_rules[0].power_rule.max_eirp = 2300;
    said[0] = 0; assert(linuxkpi_regulatory_set_wiphy_regd_sync(w, regd) == 0); free(regd);
    assert(five[0].flags == 0 && five[1].flags == 0 && (two[0].flags & IEEE80211_CHAN_DISABLED) && notified == 2);
    assert(SAID("regd 00 rules 1: usable channels 2 GHz 0, 5 GHz 2, 6 GHz 0"));
    /* No EHT, ever: and said once. */
    said[0] = 0; assert(fa_refuse_eht() && fa_refuse_eht());
    assert(SAID("802.11be; not offered") && !strstr(strstr(said, "802.11be") + 1, "802.11be"));
    /* Going away: nothing more is applied, and the runner cannot outlive the memory. */
    linuxkpi_ieee80211_unregister_hw(hw);
    regd = calloc(1, size); regd->n_reg_rules = 0;
    assert(linuxkpi_regulatory_set_wiphy_regd_sync(w, regd) == -ENODEV && notified == 2); free(regd);
    struct fa_radio *radio = radio_of(hw);
    linuxkpi_ieee80211_iffree(hw);
    assert(radio->wiphy_runner.cancelled == 1 && queues_destroyed == 1 && radio->work == NULL);
    linuxkpi_ieee80211_iffree(hw); assert(queues_destroyed == 1);
    linuxkpi_wiphy_free(w);
    assert(!locks);
    puts("fa_80211: alloc, register, refusals, EHT, regd, notifier, address, free");
    return 0;
}
"""


@check
def fa_80211_runs_on_fake_headers(tmp):
    """The radio as ath12k makes, registers and frees it, on the host. Design M2: no EHT, regd kept right."""
    for name, text in FAKE_HEADERS.items():
        path = tmp / 'fake' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    own = ROOT / 'xnu/fuxi-ath12k'
    (tmp / 'fake/lx.h').write_bytes((own / 'compat/lx.h').read_bytes())
    (tmp / 'fake/lx_os.h').write_bytes((own / 'compat/include/lx_os.h').read_bytes())
    (tmp / 'driver.c').write_text('#include <stdarg.h>\n#include <linux/kernel.h>\n' + FA_80211_DRIVER)
    program = tmp / 'driver'
    cc = os.environ.get('CC', 'cc')
    subprocess.run([cc, '-std=gnu11', '-w', '-g', f'-I{tmp / "fake"}', f'-I{own / "src"}', str(tmp / 'driver.c'), str(own / 'src/fa_wpa.c'), '-o', str(program)], check=True)
    subprocess.run([str(program)], check=True)


STATION_DRIVER = r"""
#include "fa_80211.c"
int lx_os_thread(const char *name, void (*function)(void *), void *argument) { return 0; }
int fail_workqueue, queues_destroyed, fail_skb;
static char said[65536];
static unsigned locks, rddm;
static char calls[256];
static unsigned long long now = 1;
static int scan_mode, start_error, add_error, trouble_after = -1, die_in_remove;
static struct linuxkpi_ieee80211_regdomain *apply_in_add;
static unsigned trouble_asks, evidence_count;
static char first_evidence[64];
static struct ieee80211_scan_request *seen;
static int seen_channels, seen_ssids, seen_ie_len, seen_ssid_len;
static uint8_t seen_ie[64];
static unsigned seen_freqs[64];
void lx_tell(const char *f, ...) { va_list l; va_start(l, f); vsnprintf(said + strlen(said), sizeof(said) - strlen(said), f, l); va_end(l); }
long lx_stub(const char *n) { return 0; }
void lx_pool_peaks(unsigned *rings, unsigned *slots) { *rings = 0; *slots = 7; }
void lx_os_lock(void) { assert(!locks); locks++; }
void lx_os_unlock(void) { assert(locks); locks--; }
void lx_os_sleep(const void *a, unsigned long long n) { assert(locks); now += n; }
void lx_os_wakeup(const void *a) {}
unsigned long long lx_os_nanoseconds(void) { return now; }
void fuxi_ath12k_force_rddm(void) { abort(); }
#define NOTE(x) (strcat(calls, x), strcat(calls, ","))
void fa_m4_credits(const char *w) { assert(!locks); }
void fa_m4_rx_ring(const char *w) { assert(!locks); NOTE("ring"); }
void fa_m5_rx_ring(const char *w) { abort(); }
void fa_m4_scan_started(void) { NOTE("started"); }
void fa_m4_evidence(const char *w) { assert(!locks); if (!evidence_count++) strcpy(first_evidence, w); }
bool fa_m4_trouble(void) { assert(!locks); if (trouble_after >= 0 && (int)trouble_asks++ >= trouble_after) fa_m4_evidence("forced"); return evidence_count != 0; }
void fa_m4_firmware_dead(void) { assert(!locks); if (!rddm++) NOTE("rddm"); }

static uint8_t frame[256];
static struct sk_buff *skb_of(const char *name, unsigned length, uint8_t last, uint8_t kind, unsigned freq, int signal, unsigned flag) {
    static const uint8_t bssid[6] = {0x02, 0xa1, 0xb2, 0xc3, 0xd4, 0};
    struct sk_buff *s = calloc(1, sizeof(*s));
    memset(frame, 0, sizeof(frame));
    frame[0] = kind;
    memcpy(frame + 16, bssid, 6); frame[21] = last;
    frame[36] = 0; frame[37] = (uint8_t)length; memcpy(frame + 38, name, length);
    s->data = frame; s->len = 38 + length;
    struct ieee80211_rx_status *st = IEEE80211_SKB_RXCB(s);
    st->freq = (uint16_t)freq; st->signal = (int8_t)signal; st->flag = flag;
    return s;
}
static struct napi_struct { int x; } a_napi;
static struct ieee80211_hw *the_hw;
static void radio_hears(void) {
    /* By WMI (no napi) and by the data path. A quote, a line feed and a percent sign in a name. */
    linuxkpi_ieee80211_rx(the_hw, skb_of("home", 4, 1, 0x80, 2412, -40, 0), NULL, NULL, NULL);
    linuxkpi_ieee80211_rx(the_hw, skb_of("a\"b\n%s", 6, 2, 0x50, 5180, -50, 0), NULL, NULL, NULL);
    linuxkpi_ieee80211_rx(the_hw, skb_of("data", 4, 3, 0x80, 5200, 0, RX_FLAG_NO_SIGNAL_VAL), NULL, (void *)&a_napi, NULL);
    linuxkpi_ieee80211_rx(the_hw, skb_of("ether", 5, 4, 0x80, 5200, -30, RX_FLAG_8023), NULL, (void *)&a_napi, NULL);
    linuxkpi_ieee80211_rx(the_hw, skb_of("frame", 5, 5, 0x08, 5200, -30, 0), NULL, NULL, NULL);
    linuxkpi_ieee80211_rx(the_hw, skb_of("a name of thirty-three bytes long", 33, 6, 0x80, 5200, -30, 0), NULL, NULL, NULL);
    struct sk_buff *cut = skb_of("cut", 3, 7, 0x80, 5200, -30, 0); cut->len = 30;
    linuxkpi_ieee80211_rx(the_hw, cut, NULL, NULL, NULL);
}
static int op_start(struct ieee80211_hw *hw) { assert(hw->wiphy->mtx.locked); NOTE("start"); return start_error; }
static void op_stop(struct ieee80211_hw *hw, bool s) { assert(hw->wiphy->mtx.locked && !s); NOTE("stop"); }
static int op_add(struct ieee80211_hw *hw, struct ieee80211_vif *v) {
    assert(hw->wiphy->mtx.locked && v->type == NL80211_IFTYPE_STATION && !memcmp(v->addr, hw->wiphy->perm_addr, 6));
    assert(v->bss_conf.bssid != NULL && v->link_conf[0] == &v->bss_conf && v->bss_conf.vif == v && !memcmp(v->bss_conf.addr, v->addr, 6));
    assert(!((uintptr_t)v & 63));
    NOTE("add");
    if (apply_in_add != NULL) assert(linuxkpi_regulatory_set_wiphy_regd_sync(hw->wiphy, apply_in_add) == 0);
    return add_error;
}
static void op_remove(struct ieee80211_hw *hw, struct ieee80211_vif *v) { assert(hw->wiphy->mtx.locked); NOTE("remove"); if (die_in_remove) fa_m4_evidence("died"); }
static void op_cancel(struct ieee80211_hw *hw, struct ieee80211_vif *v) { assert(hw->wiphy->mtx.locked); NOTE("cancel"); }
static int op_scan(struct ieee80211_hw *hw, struct ieee80211_vif *v, struct ieee80211_scan_request *r) {
    assert(hw->wiphy->mtx.locked);
    NOTE("hw_scan");
    seen_channels = r->req.n_channels; seen_ssids = r->req.n_ssids; seen_ie_len = r->req.ie_len;
    seen_ssid_len = r->req.n_ssids ? r->req.ssids[0].ssid_len : -1;
    assert(r->req.wiphy == hw->wiphy);
    if (r->req.ie_len) memcpy(seen_ie, r->req.ie, r->req.ie_len);
    for (int i = 0; i < r->req.n_channels; i++) seen_freqs[i] = r->req.channels[i]->center_freq;
    if (scan_mode == 3) return -110;
    radio_hears();
    /* The interface is there for a beacon's iteration, as the poll thread would have it. */
    if (scan_mode == 0) { struct cfg80211_scan_info i = { .aborted = false }; linuxkpi_ieee80211_scan_completed(hw, &i); }
    if (scan_mode == 2) { struct cfg80211_scan_info i = { .aborted = true }; linuxkpi_ieee80211_scan_completed(hw, &i); }
    return 0;
}
static const struct ieee80211_ops ops = { .start = op_start, .stop = op_stop, .add_interface = op_add, .remove_interface = op_remove,
    .hw_scan = op_scan, .cancel_hw_scan = op_cancel };
static struct linuxkpi_ieee80211_channel two[3], five[3];
static struct ieee80211_rate rates[12];
static struct ieee80211_supported_band b2, b5;
#define SAID(text) (strstr(said, text) != NULL)
static const char *const ALL = "start,add,hw_scan,started,ring,remove,stop,";

static void fresh(int mode) {
    scan_mode = mode; start_error = add_error = die_in_remove = 0; trouble_after = -1; trouble_asks = evidence_count = 0; rddm = 0; apply_in_add = NULL;
    first_evidence[0] = 0; said[0] = 0; calls[0] = 0; seen_channels = seen_ssids = seen_ie_len = 0; seen_ssid_len = -2;
    the_hw = linuxkpi_ieee80211_alloc_hw(256, &ops);
    the_hw->vif_data_size = 512;
    struct wiphy *w = the_hw->wiphy;
    static const u8 ours[6] = {2, 0x46, 0x55, 0x58, 0x49, 1};
    memcpy(w->perm_addr, ours, 6);
    memset(two, 0, sizeof(two)); memset(five, 0, sizeof(five));
    two[0].center_freq = 2412; two[1].center_freq = 2472; two[2].center_freq = 2484; two[2].flags = IEEE80211_CHAN_DISABLED;
    five[0].center_freq = 5180; five[1].center_freq = 5260; five[1].flags = IEEE80211_CHAN_NO_IR | IEEE80211_CHAN_RADAR;
    five[2].center_freq = 5500; five[2].flags = IEEE80211_CHAN_DISABLED;
    static const int r[12] = {10, 20, 55, 110, 60, 90, 120, 180, 240, 360, 480, 540};
    for (int i = 0; i < 12; i++) rates[i].bitrate = r[i];
    b2 = (struct ieee80211_supported_band){ .channels = two, .n_channels = 3, .bitrates = rates, .n_bitrates = 12 };
    b5 = (struct ieee80211_supported_band){ .channels = five, .n_channels = 3, .bitrates = rates + 4, .n_bitrates = 8 };
    w->bands[NL80211_BAND_2GHZ] = &b2; w->bands[NL80211_BAND_5GHZ] = &b5;
}
static void done(void) {
    assert(!locks && !the_hw->wiphy->mtx.locked);
    linuxkpi_ieee80211_unregister_hw(the_hw);
    linuxkpi_ieee80211_iffree(the_hw);
    linuxkpi_wiphy_free(the_hw->wiphy);
}
static void no_address_in_the_log(void) {
    /* The proof lines never show a BSSID: neither as text nor its bytes run together. */
    assert(!strstr(said, "a1:b2") && !strstr(said, "a1b2") && !strstr(said, "A1:B2") && !strstr(said, "d4:"));
}

int main(void) {
    /* Active, both routes, up to the proof, in mac80211's order, and only usable channels. */
    fresh(0);
    linuxkpi_ieee80211_ifattach(the_hw); /* so that a domain applies */
    assert(fa_m4_station(the_hw, false) == 0);
    assert(!strcmp(calls, ALL));
    assert(seen_channels == 4 && seen_freqs[0] == 2412 && seen_freqs[1] == 2472 && seen_freqs[2] == 5180 && seen_freqs[3] == 5260);
    assert(SAID("M4 scan active channels 4 of them passive by regd 1, probe rates 16 bytes"));
    /* A wildcard SSID, the legacy rates and nothing else. */
    assert(seen_ssids == 1 && seen_ssid_len == 0 && seen_ie_len == 16);
    assert(seen_ie[0] == 1 && seen_ie[1] == 8 && seen_ie[10] == 50 && seen_ie[11] == 4);
    assert(SAID("FUXI ATH12K: M4 scan done channels 4 bss 3\n"));
    assert(SAID("FUXI ATH12K: M4 bss 2412 -40 \"home\"\n"));
    assert(SAID("FUXI ATH12K: M4 bss 5180 -50 \"a\\\"b\\x0a%s\"\n"));
    assert(SAID("FUXI ATH12K: M4 bss 5200 -128 \"data\"\n"));
    assert(strstr(said, "M4 bss 2412") < strstr(said, "M4 bss 5180") && strstr(said, "M4 bss 5180") < strstr(said, "M4 bss 5200"));
    assert(SAID("M4 rx wmi 5 data 2 beacons 2 responses 1 malformed 2 others 2 lost 0"));
    assert(SAID("M4 regd unchanged after 5"));
    assert(SAID("M4 remove_interface done") && SAID("M4 ops.stop done") && !SAID("cancel"));
    assert(!rddm && !evidence_count);
    no_address_in_the_log();
    /* A frame after the scan goes nowhere, and nothing in the table changes. */
    {
        struct fa_radio *radio = radio_of(the_hw);
        unsigned before = radio->heard.count, late = radio->rx_late;
        linuxkpi_ieee80211_rx(the_hw, skb_of("late", 4, 9, 0x80, 2412, -30, 0), NULL, NULL, NULL);
        assert(radio->heard.count == before && radio->rx_late == late + 1);
    }
    done();

    /* Passive: nothing for ath12k to put in a probe, which it passes on as WMI_SCAN_FLAG_PASSIVE. */
    fresh(0);
    assert(fa_m4_station(the_hw, true) == 0 && !strcmp(calls, ALL));
    assert(seen_ssids == 0 && seen_ie_len == 0 && seen_ssid_len == -1 && SAID("M4 scan passive channels 4"));
    assert(SAID("M4 scan done channels 4 bss 3"));
    done();

    /* The firmware's domain coming again after add_interface is waited for, quietly. */
    fresh(0);
    linuxkpi_ieee80211_ifattach(the_hw);
    {
        size_t size = sizeof(struct linuxkpi_ieee80211_regdomain) + sizeof(struct ieee80211_reg_rule);
        struct linuxkpi_ieee80211_regdomain *regd = calloc(1, size);
        regd->alpha2[0] = 'C'; regd->alpha2[1] = 'N'; regd->n_reg_rules = 1;
        regd->reg_rules[0].freq_range.start_freq_khz = 2402000; regd->reg_rules[0].freq_range.end_freq_khz = 2482000;
        regd->reg_rules[0].power_rule.max_eirp = 2000;
        apply_in_add = regd;
        assert(fa_m4_station(the_hw, false) == 0);
        free(regd);
    }
    /* The domain decides: only what it left usable is in the list. */
    assert(seen_channels == 2 && seen_freqs[0] == 2412 && seen_freqs[1] == 2472);
    assert(SAID("M4 regd applied again after 500 ms"));
    done();

    /* Nothing comes: bounded by the scan's own wait, no scan done, evidence, RDDM before the way down. */
    fresh(1);
    {
        unsigned long long began = now;
        assert(fa_m4_station(the_hw, false) == -ETIMEDOUT);
        assert(now - began >= FA_SCAN_WAIT_NS && now - began < FA_SCAN_WAIT_NS + FA_REGD_WAIT_NS + 1000000000ull);
    }
    assert(!strcmp(calls, "start,add,hw_scan,started,ring,rddm,cancel,remove,stop,"));
    assert(!strcmp(first_evidence, "scan timeout") && !SAID("M4 scan done") && SAID("M4 scan not over after 30"));
    assert(SAID("M4 not proven: channels 4 bss 3") && SAID("M4 bss 2412 -40 \"home\""));
    done();

    /* ath12k gave up by itself. */
    fresh(2);
    assert(fa_m4_station(the_hw, false) == -ETIMEDOUT);
    assert(!strcmp(calls, "start,add,hw_scan,started,ring,rddm,remove,stop,"));
    assert(!strcmp(first_evidence, "scan aborted by ath12k's timeout") && !SAID("M4 scan done") && SAID("M4 scan aborted"));
    done();

    /* hw_scan refused. */
    fresh(3);
    assert(fa_m4_station(the_hw, false) == -110);
    assert(!strcmp(calls, "start,add,hw_scan,started,rddm,remove,stop,"));
    assert(!strcmp(first_evidence, "hw_scan failed") && !SAID("M4 scan done"));
    done();

    /* ops.start fails: nothing further, nothing to undo. */
    fresh(0); start_error = -11;
    assert(fa_m4_station(the_hw, false) == -11);
    assert(!strcmp(calls, "start,rddm,"));
    assert(SAID("M4 not proven: bring-up -11") && !strcmp(first_evidence, "bring-up failed"));
    done();

    /* add_interface fails: stopped again, no interface to remove. */
    fresh(0); add_error = -12;
    assert(fa_m4_station(the_hw, false) == -12);
    assert(!strcmp(calls, "start,add,rddm,stop,"));
    done();

    /* The firmware dies before the scan: it is not asked. */
    fresh(0); trouble_after = 0;
    assert(fa_m4_station(the_hw, false) == -EIO);
    assert(!strcmp(calls, "start,add,rddm,remove,stop,") && seen_channels == 0);
    done();

    /* It dies during the scan: the wait ends at once and the scan is cancelled. */
    fresh(1); trouble_after = 150; /* about 100 asks go to the domain's wait */
    {
        unsigned long long began = now;
        assert(fa_m4_station(the_hw, false) == -ETIMEDOUT);
        assert(now - began < FA_SCAN_WAIT_NS);
    }
    assert(!strcmp(calls, "start,add,hw_scan,started,ring,rddm,cancel,remove,stop,"));
    assert(!strcmp(first_evidence, "forced") && SAID("M4 scan not over after"));
    done();

    /* It dies on the way down, after the proof: the dump comes, and it is reported. */
    fresh(0); die_in_remove = 1;
    assert(fa_m4_station(the_hw, false) == -EIO && rddm == 1);
    assert(!strcmp(calls, "start,add,hw_scan,started,ring,remove,stop,rddm,") && SAID("M4 scan done channels 4 bss 3"));
    done();

    /* No usable channel at all: no scan is asked for. */
    fresh(0);
    two[0].flags = two[1].flags = five[0].flags = five[1].flags = IEEE80211_CHAN_DISABLED;
    assert(fa_m4_station(the_hw, false) == -EINVAL && SAID("M4 no usable channel"));
    assert(!strstr(calls, "hw_scan") && !strcmp(calls, "start,add,remove,stop,"));
    done();

    /* Many networks: forty lines, the strongest, and it says the rest. */
    fresh(0);
    {
        struct fa_radio *radio = radio_of(the_hw);
        linuxkpi_ieee80211_rx(the_hw, skb_of("not yet", 7, 1, 0x80, 2412, -40, 0), NULL, NULL, NULL); /* not collecting */
        assert(radio->heard.count == 0);
        radio->collecting = true;
        for (unsigned i = 0; i < 50; i++) {
            char name[8]; int n = snprintf(name, sizeof(name), "n%u", i);
            linuxkpi_ieee80211_rx(the_hw, skb_of(name, (unsigned)n, (uint8_t)(i + 10), 0x80, 2412, -30 - (int)i, 0), NULL, NULL, NULL);
        }
        radio->collecting = false;
        radio->say_names = true;
        say_heard(radio);
        unsigned lines = 0;
        for (const char *at = said; (at = strstr(at, "M4 bss 2412")) != NULL; at++) lines++;
        assert(lines == FA_BSS_SAID_MOST && SAID("M4 bss 10 weaker ones not said") && SAID("\"n39\"") && !SAID("\"n40\""));
        no_address_in_the_log();
    }
    done();
    assert(!locks);
    puts("fa_80211: stage 4 order, both routes, regd, active and passive, timeouts, evidence before the way down, forty lines");
    return 0;
}
"""


@check
def station_runs_on_fake_headers(tmp):
    """Stage 4's station on the host with a fake driver and clock: each way it can end. Design M4."""
    for name, text in FAKE_HEADERS.items():
        path = tmp / 'fake' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    own = ROOT / 'xnu/fuxi-ath12k'
    (tmp / 'fake/lx.h').write_bytes((own / 'compat/lx.h').read_bytes())
    (tmp / 'fake/lx_os.h').write_bytes((own / 'compat/include/lx_os.h').read_bytes())
    (tmp / 'driver.c').write_text('#include <stdarg.h>\n#include <linux/kernel.h>\n' + STATION_DRIVER)
    program = tmp / 'driver'
    cc = os.environ.get('CC', 'cc')
    subprocess.run([cc, '-std=gnu11', '-w', '-g', '-fsanitize=address,undefined', '-fno-sanitize-recover=all',
                    f'-I{tmp / "fake"}', f'-I{own / "src"}', str(tmp / 'driver.c'), str(own / 'src/fa_wpa.c'), '-o', str(program)], check=True)
    subprocess.run([str(program)], check=True)



JOIN_DRIVER = r"""
#include <signal.h>
#include "fa_80211.c"
int fail_workqueue, queues_destroyed, fail_skb;
static char said[1048576];
static unsigned locks, rddm;
static char calls[65536];
static unsigned long long now = 1;
static int trouble_after = -1;
static unsigned trouble_asks, evidence_count, r6_count;
static char first_evidence[96];
void lx_tell(const char *f, ...) { va_list l; va_start(l, f); vsnprintf(said + strlen(said), sizeof(said) - strlen(said), f, l); va_end(l); }
long lx_stub(const char *n) { return 0; }
void lx_pool_peaks(unsigned *rings, unsigned *slots) { *rings = 0; *slots = 7; }
void lx_os_lock(void) { assert(!locks); locks++; }
void lx_os_unlock(void) { assert(locks); locks--; }
void lx_os_sleep(const void *a, unsigned long long n) { assert(locks); now += n; }
void lx_os_wakeup(const void *a) {}
unsigned long long lx_os_nanoseconds(void) { return now; }
void fuxi_ath12k_force_rddm(void) { abort(); }
#define NOTE(x) (strcat(calls, x), strcat(calls, ","))
void fa_m4_credits(const char *w) { assert(!locks); }
void fa_m4_rx_ring(const char *w) { assert(!locks); }
void fa_m5_rx_ring(const char *w) { assert(!locks); r6_count++; }
void fa_m4_scan_started(void) {}
void fa_m4_evidence(const char *w) { assert(!locks); if (!evidence_count++) strcpy(first_evidence, w); }
static void (*tick)(void);
bool fa_m4_trouble(void) {
    assert(!locks);
    if (tick != NULL) tick();
    if (trouble_after >= 0 && (int)trouble_asks++ >= trouble_after) fa_m4_evidence("forced");
    return evidence_count != 0;
}
void fa_m4_firmware_dead(void) { assert(!locks); if (!rddm++) NOTE("rddm"); }

static const uint8_t AP[6] = {0x02, 0xa1, 0xb2, 0xc3, 0xd4, 0x55}, OTHER[6] = {0x02, 0xa1, 0xb2, 0xc3, 0xd4, 0x66};
static const uint8_t OWN[6] = {2, 0x46, 0x55, 0x58, 0x49, 1};
static const char NAME[] = "fuxi-test-net";
static struct ieee80211_hw *the_hw;
static struct napi_struct { int x; } a_napi;

static void deliver(const uint8_t *bytes, unsigned length, unsigned freq, int signal, unsigned flag, bool napi) {
    struct sk_buff *s = calloc(1, sizeof(*s));
    s->head = s->data = malloc(length ? length : 1); /* of exactly its size: the sanitizer sees a read past its end */
    memcpy(s->data, bytes, length); s->len = length;
    struct ieee80211_rx_status *st = IEEE80211_SKB_RXCB(s);
    st->freq = (uint16_t)freq; st->signal = (int8_t)signal; st->flag = flag;
    linuxkpi_ieee80211_rx(the_hw, s, NULL, napi ? (void *)&a_napi : NULL, NULL);
}
/* A beacon or probe response: its elements are the name's, then those given. */
static void hear(uint8_t kind, const uint8_t *bssid, const char *name, unsigned cap, const uint8_t *more, unsigned more_length, unsigned freq, int signal) {
    uint8_t f[1600] = {0};
    unsigned n = name ? (unsigned)strlen(name) : 0, at = 36;
    f[0] = kind; memset(f + 4, 0xff, 6); memcpy(f + 10, bssid, 6); memcpy(f + 16, bssid, 6);
    f[32] = 100; f[34] = (uint8_t)cap; f[35] = (uint8_t)(cap >> 8);
    f[at++] = 0; f[at++] = (uint8_t)n; memcpy(f + at, name ? name : "", n); at += n;
    memcpy(f + at, more, more_length); at += more_length;
    deliver(f, at, freq, signal, 0, false);
}
static unsigned answer_frame(uint8_t *f, uint8_t kind, const uint8_t *from, const uint8_t *to) {
    memset(f, 0, 24); f[0] = kind; memcpy(f + 4, to, 6); memcpy(f + 10, from, 6); memcpy(f + 16, from, 6);
    return 24;
}
static void give_auth(const uint8_t *from, const uint8_t *to, unsigned algorithm, unsigned step, unsigned status) {
    uint8_t f[64]; unsigned at = answer_frame(f, FA_KIND_AUTH, from, to);
    f[at] = (uint8_t)algorithm; f[at + 1] = 0; f[at + 2] = (uint8_t)step; f[at + 3] = 0; f[at + 4] = (uint8_t)status; f[at + 5] = (uint8_t)(status >> 8);
    deliver(f, at + 6, 2412, -40, 0, false);
}
static void give_assoc(const uint8_t *from, unsigned status, unsigned aid, const uint8_t *elements, unsigned length) {
    uint8_t f[1024]; unsigned at = answer_frame(f, FA_KIND_ASSOC_RESPONSE, from, OWN);
    f[at] = 1; f[at + 1] = 0; f[at + 2] = (uint8_t)status; f[at + 3] = (uint8_t)(status >> 8); f[at + 4] = (uint8_t)aid; f[at + 5] = (uint8_t)(aid >> 8);
    memcpy(f + at + 6, elements, length);
    deliver(f, at + 6 + length, 2412, -40, 0, false);
}
static void give_door(uint8_t kind, unsigned reason) {
    uint8_t f[64]; unsigned at = answer_frame(f, kind, AP, OWN);
    f[at] = (uint8_t)reason; f[at + 1] = 0;
    deliver(f, at + 2, 2412, -40, 0, false);
}
static void give_data(const uint8_t *from, uint8_t kind, bool ethernet) {
    uint8_t f[64] = {0};
    f[0] = kind; f[1] = 2; memset(f + 4, 0xff, 6); memcpy(f + 10, from, 6); memcpy(f + 16, from, 6);
    deliver(f, 40, 2412, 0, RX_FLAG_NO_SIGNAL_VAL | (ethernet ? RX_FLAG_8023 : 0), true);
}

static const uint8_t OPEN_HT[] = { 1, 8, 0x82, 0x84, 0x8b, 0x96, 12, 18, 24, 36, 50, 4, 48, 72, 96, 108, 3, 1, 6,
    45, 26, 0x7f, 0x02, 0x1b,
    0xff, 0xff, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,	/* the MCS set */
    0, 0, 0, 0, 0, 0, 0,					/* extended, beamforming, selection */
    61, 22, 6, 0, 4, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    221, 7, 0x00, 0x50, 0xf2, 2, 0, 1, 0 };
static const uint8_t WMM_PARAM[] = { 221, 24, 0x00, 0x50, 0xf2, 2, 1, 1, 0, 0, 3, 0xa4, 0, 0, 0x27, 0xa4, 0, 0, 0x42, 0x43, 94, 0, 0x62, 0x32, 47, 0 };

/* ---- the handshake's peer, stage 6 ---- */
static struct { bool forged_first, any_reply, silent, no_m3, door, die_at_m3, goes_open; int fail_key; bool m1_given, m3_given, forged_given, stayed; const unsigned char *group_reply; unsigned group_reply_length; } wpa;
static unsigned eapols, key_calls;
static bool asked_bss, bss_had_rsn, asked_bss_open;
static uint8_t WPA_HEARD[sizeof((uint8_t[]){ 0 }) * 22 + 128];
static void give_eapol(const uint8_t *body, unsigned length) {
    uint8_t f[800] = {0}; unsigned at = 26;
    f[0] = 0x88; f[1] = 0x02; memcpy(f + 4, OWN, 6); memcpy(f + 10, AP, 6); memcpy(f + 16, AP, 6);
    memcpy(f + at, "\xaa\xaa\x03\x00\x00\x00\x88\x8e", 8); at += 8;
    memcpy(f + at, body, length); at += length;
    deliver(f, at, 2412, -40, 0, true);
}

/* ---- the network's peer, stage 7 ---- */
static const uint8_t V_ME[4] = {10, 0, 0, 9}, V_GW[4] = {10, 0, 0, 1}, V_BEYOND[4] = {1, 1, 1, 1};
static struct { bool on, silent, unprotected, nak, no_router, broadcast_only, no_arp, no_beyond; unsigned discovers, requests, releases, arps, echoes, everybody_flags; uint32_t xid; } net;
static void give_net(unsigned type, const uint8_t *body, unsigned length, bool to_all) {
    uint8_t f[800] = {0}; unsigned at = 26;
    f[0] = 0x88; f[1] = 0x02; memcpy(f + 4, to_all ? (const uint8_t *)"\xff\xff\xff\xff\xff\xff" : OWN, 6); memcpy(f + 10, AP, 6); memcpy(f + 16, OTHER, 6);
    memcpy(f + at, "\xaa\xaa\x03\x00\x00\x00", 6); f[at + 6] = type >> 8; f[at + 7] = type; at += 8;
    memcpy(f + at, body, length); at += length;
    deliver(f, at, 2412, -40, net.unprotected ? 0 : RX_FLAG_DECRYPTED | RX_FLAG_IV_STRIPPED, true);
}
static void give_dhcp(unsigned kind, bool to_all) {
    uint8_t m[400] = {0}, *udp = m + 20, *b = udp + 8, *o = b + 240;
    b[0] = 2; b[1] = 1; b[2] = 6; b[4] = net.xid >> 24; b[5] = net.xid >> 16; b[6] = net.xid >> 8; b[7] = net.xid;
    memcpy(b + 16, V_ME, 4); memcpy(b + 28, OWN, 6); b[236] = 99; b[237] = 130; b[238] = 83; b[239] = 99;
    *o++ = 53; *o++ = 1; *o++ = kind; *o++ = 54; *o++ = 4; memcpy(o, V_GW, 4); o += 4;
    *o++ = 1; *o++ = 4; *o++ = 255; *o++ = 255; *o++ = 255; *o++ = 0;
    if (!net.no_router) { *o++ = 3; *o++ = 4; memcpy(o, V_GW, 4); o += 4; }
    *o++ = 51; *o++ = 4; *o++ = 0; *o++ = 0; *o++ = 0x0e; *o++ = 0x10; *o++ = 255;
    unsigned udp_length = (unsigned)(o - udp);
    fa_ip_header(m, 17, V_GW, V_ME, 20 + udp_length, 9);
    udp[1] = 67; udp[3] = 68; udp[4] = udp_length >> 8; udp[5] = udp_length;
    give_net(FA_TYPE_IP, m, 20 + udp_length, to_all);
}
/* What the network hears from us: over the air, ciphered, after the keys. It answers from inside op_tx, as the fake's clock has no other time. */
static struct { unsigned arps, echoes, wrong; bool given; } serve;
static uint8_t net_last_to[6];
static int serve_end;
static void serve_tick(void);
static void net_heard(const uint8_t *data, unsigned n, unsigned type) {
    assert(net.on && radio_of(the_hw)->reached == IEEE80211_STA_AUTHORIZED);
    /* Answers of the load test: to whoever asked, by the bytes. */
    if (type == FA_TYPE_ARP && n == 28 && data[7] == 2) {
        static const uint8_t THEM[4] = {10, 0, 0, 77};
        serve.arps++;
        serve.wrong += memcmp(net_last_to, OTHER, 6) != 0 || memcmp(data + 8, OWN, 6) != 0 || memcmp(data + 14, V_ME, 4) != 0 || memcmp(data + 18, OTHER, 6) != 0 || memcmp(data + 24, THEM, 4) != 0;
        return;
    }
    if (type == FA_TYPE_IP && n >= 28 && data[9] == 1 && data[20] == 0) {
        static const uint8_t THEM[4] = {10, 0, 0, 77};
        serve.echoes++;
        serve.wrong += memcmp(net_last_to, OTHER, 6) != 0 || fa_ip_sum(data, 20) != 0 || fa_ip_sum(data + 20, n - 20) != 0 || memcmp(data + 12, V_ME, 4) != 0 || memcmp(data + 16, THEM, 4) != 0 || fa_be16(data + 24) != 0x4242;
        return;
    }
    if (type == FA_TYPE_ARP) {
        net.arps++;
        assert(n == 28 && data[7] == 1 && !memcmp(data + 14, V_ME, 4) && !memcmp(data + 24, V_GW, 4));
        if (!net.no_arp) { uint8_t a[28]; memcpy(a, "\0\1\10\0\6\4\0\2", 8); memcpy(a + 8, OTHER, 6); memcpy(a + 14, V_GW, 4); memcpy(a + 18, OWN, 6); memcpy(a + 24, V_ME, 4); give_net(FA_TYPE_ARP, a, 28, false); }
        return;
    }
    assert(type == FA_TYPE_IP && n >= 20 && fa_ip_sum(data, 20) == 0);
    if (data[9] == 1) {
        net.echoes++;
        if (net.no_beyond && !memcmp(data + 16, V_BEYOND, 4)) return;
        assert(!memcmp(data + 12, V_ME, 4));
        uint8_t e[80]; memcpy(e, data, n); memcpy(e + 12, data + 16, 4); memcpy(e + 16, data + 12, 4); e[10] = e[11] = 0; fa_put16(e + 10, fa_ip_sum(e, 20));
        e[20] = 0; e[22] = e[23] = 0; fa_put16(e + 22, fa_ip_sum(e + 20, n - 20));
        give_net(FA_TYPE_IP, e, n, false);
        return;
    }
    assert(data[9] == 17 && data[23] == 67 && n == FA_DHCP_LENGTH);
    const uint8_t *b = data + 28; unsigned kind = b[242];
    net.xid = fa_be32(b + 4);
    assert(!memcmp(b + 28, OWN, 6));
    bool all = (b[10] & 0x80) != 0;
    net.everybody_flags += all;
    if (kind == FA_DHCP_DISCOVER) { net.discovers++; if (net.silent || (net.broadcast_only && !all)) return; give_dhcp(FA_DHCP_OFFER, all); }
    else if (kind == FA_DHCP_REQUEST) { net.requests++; assert(!memcmp(fa_element(b + 240, 60, 50) + 2, V_ME, 4) && !memcmp(fa_element(b + 240, 60, 54) + 2, V_GW, 4)); give_dhcp(net.nak ? FA_DHCP_NAK : FA_DHCP_ACK, all); }
    else { assert(kind == FA_DHCP_RELEASE && !memcmp(data + 12, V_ME, 4) && !memcmp(data + 16, V_GW, 4) && !memcmp(b + 12, V_ME, 4)); net.releases++; }
}
/* ---- stage 8: iOS's side, and frames from the network ---- */
static struct { unsigned rx, rx_length, ups, downs, losts; int lost; uint8_t frame[1600]; } m8;
static uint8_t m8_sent[1600]; static unsigned m8_sent_length, m8_sent_count; static bool m8_sent_key;
static void m8_rx(const unsigned char *f, unsigned long n) { assert(!locks && n <= sizeof(m8.frame)); memcpy(m8.frame, f, n); m8.rx_length = n; m8.rx++; }
static void m8_link(int up) { assert(!locks); if (up) m8.ups++; else m8.downs++; }
static void m8_lost(int a) { assert(!locks); m8.losts++; m8.lost = a; }
static unsigned m8_alones; static int m8_alone;
static void m8_joined_alone(int answer) { m8_alones++; m8_alone = answer; }
static const struct fa_ios_input M8_INPUT = { .rx = m8_rx, .link = m8_link, .lost = m8_lost, .joined_alone = m8_joined_alone };
/* A data frame from the network: to us or to all, and in front of its payload a CCMP header with this number and key, when `cipher` is. */
static void m8_give(bool to_all, unsigned flags, bool cipher, unsigned long long number, unsigned key, unsigned type, const uint8_t *body, unsigned length) {
    uint8_t f[1700] = {0}; unsigned at = 26;
    f[0] = 0x88; f[1] = 0x02 | (cipher ? 0x40 : 0);
    memcpy(f + 4, to_all ? (const uint8_t *)"\xff\xff\xff\xff\xff\xff" : OWN, 6); memcpy(f + 10, AP, 6); memcpy(f + 16, OTHER, 6);
    if (cipher) { f[at] = number; f[at + 1] = number >> 8; f[at + 3] = (uint8_t)(0x20 | key << 6); f[at + 4] = number >> 16; f[at + 5] = number >> 24; f[at + 6] = number >> 32; f[at + 7] = number >> 40; at += 8; }
    memcpy(f + at, "\xaa\xaa\x03\x00\x00\x00", 6); f[at + 6] = type >> 8; f[at + 7] = type; at += 8;
    memcpy(f + at, body, length); at += length;
    deliver(f, at, 2412, 0, RX_FLAG_NO_SIGNAL_VAL | flags, true);
}
static void eapol_sent(struct ieee80211_hw *hw, struct sk_buff *skb) {
    unsigned header = skb->data[0] == 0x88 ? 26 : 24;
    if (skb->protocol != cpu_to_be16(0x888e) && radio_of(hw)->ios) {
        memcpy(m8_sent, skb->data, skb->len); m8_sent_length = skb->len; m8_sent_count++;
        m8_sent_key = IEEE80211_SKB_CB(skb)->control.hw_key != NULL;
        assert(m8_sent_key == radio_of(hw)->protect && skb->priority == 0 && (!m8_sent_key || IEEE80211_SKB_CB(skb)->control.hw_key == radio_of(hw)->pairwise_key));
        IEEE80211_SKB_CB(skb)->flags |= IEEE80211_TX_STAT_ACK;
        struct ieee80211_tx_status s = { .skb = skb, .info = IEEE80211_SKB_CB(skb) };
        linuxkpi_ieee80211_tx_status_ext(hw, &s);
        return;
    }
    if (skb->protocol != cpu_to_be16(0x888e)) {
        unsigned type = fa_be16(skb->data + header + 6);
        assert(skb->data[1] == 0x41 && IEEE80211_SKB_CB(skb)->control.hw_key == radio_of(hw)->pairwise_key && radio_of(hw)->pairwise_key != NULL && skb->priority == 0 && skb->protocol == cpu_to_be16(type));
        assert(!memcmp(skb->data + header, "\xaa\xaa\x03\x00\x00\x00", 6));
        uint8_t copy[1600]; unsigned n = skb->len - header - 8; memcpy(copy, skb->data + header + 8, n);
        memcpy(net_last_to, skb->data + 16, 6);
        IEEE80211_SKB_CB(skb)->flags |= IEEE80211_TX_STAT_ACK;
        struct ieee80211_tx_status s = { .skb = skb, .info = IEEE80211_SKB_CB(skb) };
        linuxkpi_ieee80211_tx_status_ext(hw, &s);
        net_heard(copy, n, type);
        return;
    }
    assert(skb->protocol == cpu_to_be16(0x888e) && skb->len > header + 8 && skb->priority == 7);
    assert(eapols >= 2 ? skb->data[1] == 0x41 && IEEE80211_SKB_CB(skb)->control.hw_key == radio_of(hw)->pairwise_key && radio_of(hw)->pairwise_key != NULL : skb->data[1] == 1 && IEEE80211_SKB_CB(skb)->control.hw_key == NULL);
    const uint8_t *body = skb->data + header + 8; unsigned n = skb->len - header - 8;
    eapols++;
    if (!wpa.any_reply) {
        if (eapols == 1) assert(n == sizeof(V_M2) && !memcmp(body, V_M2, n));
        else if (eapols == 2) assert(n == sizeof(V_M4) && !memcmp(body, V_M4, n));
        else assert(wpa.group_reply && n == wpa.group_reply_length && !memcmp(body, wpa.group_reply, n));
    }
    IEEE80211_SKB_CB(skb)->flags |= IEEE80211_TX_STAT_ACK;
    struct ieee80211_tx_status s = { .skb = skb, .info = IEEE80211_SKB_CB(skb) };
    linuxkpi_ieee80211_tx_status_ext(hw, &s);
}
/* Only stage 8 may come here, never from the poll thread (give_action asserts it). */
static unsigned ba_calls, action_sent, action_length; static int ba_fail; static struct ieee80211_ampdu_params ba_last; static uint8_t action[64]; static bool on_poll;
static int op_ampdu(struct ieee80211_hw *hw, struct ieee80211_vif *v, struct ieee80211_ampdu_params *p) {
    assert(hw->wiphy->mtx.locked && radio_of(hw)->ios && !on_poll && !locks && p->sta == radio_of(hw)->sta && p->sta != NULL);
    ba_calls++; ba_last = *p;
    NOTE(p->action == IEEE80211_AMPDU_RX_START ? "ba_start" : "ba_stop");
    return p->action == IEEE80211_AMPDU_RX_START ? ba_fail : 0;
}
static void give_action(const uint8_t *from, const uint8_t *body, unsigned length) {
    uint8_t f[80] = {0};
    f[0] = FA_KIND_ACTION; memcpy(f + 4, OWN, 6); memcpy(f + 10, from, 6); memcpy(f + 16, from, 6); memcpy(f + 24, body, length);
    on_poll = true; deliver(f, 24 + length, 2412, -40, 0, false); on_poll = false;
}
static int op_set_key(struct ieee80211_hw *hw, enum set_key_cmd cmd, struct ieee80211_vif *v, struct ieee80211_sta *s, struct ieee80211_key_conf *k) {
    bool pairwise = (k->flags & IEEE80211_KEY_FLAG_PAIRWISE) != 0;
    assert(hw->wiphy->mtx.locked && k->cipher == WLAN_CIPHER_SUITE_CCMP && k->keylen == 16 && k->iv_len == 8 && k->link_id == -1 && (cmd == DISABLE_KEY || radio_of(hw)->ios ? radio_of(hw)->reached >= IEEE80211_STA_ASSOC : radio_of(hw)->reached == IEEE80211_STA_ASSOC));
    if (pairwise) assert(s == radio_of(hw)->sta && k->keyidx == 0 && (cmd != SET_KEY || !memcmp(k->key, V_TK, 16)));
    else if (radio_of(hw)->ios) assert(s == NULL && (k->keyidx == 1 || k->keyidx == 2));
    else assert(s == NULL && k->keyidx == 1 && (cmd != SET_KEY || !memcmp(k->key, V_GTK, 16)));
    NOTE(cmd == SET_KEY ? (pairwise ? "key_p" : "key_g") : (pairwise ? "off_p" : "off_g"));
    if (cmd == SET_KEY && (int)++key_calls == wpa.fail_key) return -5;
    return 0;
}
static struct {
    const uint8_t *hear_elements; unsigned hear_length, cap;
    const char *heard_name;
    bool not_heard, silent_auth, silent_assoc, no_complete, bad_auth_first, bad_assoc_first;
    unsigned auth_status, assoc_status, aid, frequency;
    const uint8_t *response; unsigned response_length;
    int fail_add, fail_assign, fail_sta_to;
    void (*also_heard)(void);
    bool hidden_beacon;
} ap;
static unsigned auth_tries, assoc_tries, deauths, scans, scan_channels;
int lx_os_thread(const char *name, void (*function)(void *), void *argument) { return 0; }
static uint8_t last_assoc[1024], last_deauth[64], last_auth[64];
static unsigned last_assoc_length;
static uint8_t seen_ssid[33]; static int seen_ssid_length;
static uint64_t first_link_change, second_link_change;
static unsigned link_changes;

static int op_start(struct ieee80211_hw *hw) { assert(hw->wiphy->mtx.locked); NOTE("start"); return 0; }
static void op_stop(struct ieee80211_hw *hw, bool s) { assert(hw->wiphy->mtx.locked); NOTE("stop"); }
static int op_add(struct ieee80211_hw *hw, struct ieee80211_vif *v) { NOTE("add"); return 0; }
static void op_remove(struct ieee80211_hw *hw, struct ieee80211_vif *v) { assert(hw->wiphy->mtx.locked && v->bss_conf.chanctx_conf == NULL); NOTE("remove"); }
static void op_cancel(struct ieee80211_hw *hw, struct ieee80211_vif *v) { NOTE("cancel"); }
static int op_scan(struct ieee80211_hw *hw, struct ieee80211_vif *v, struct ieee80211_scan_request *r) {
    NOTE("scan"); scans++;
    scan_channels = r->req.n_channels;
    seen_ssid_length = r->req.n_ssids ? r->req.ssids[0].ssid_len : -1;
    if (r->req.n_ssids) memcpy(seen_ssid, r->req.ssids[0].ssid, r->req.ssids[0].ssid_len);
    /* Neighbours, the wanted name as a prefix of another and the other way round, at any strength. */
    hear(FA_KIND_BEACON, OTHER, "fuxi-test-net2", 0, OPEN_HT, sizeof(OPEN_HT), 2437, -10);
    hear(FA_KIND_BEACON, OTHER, "fuxi-test-ne", 0x10, OPEN_HT, sizeof(OPEN_HT), 2437, -10);
    if (!ap.not_heard) {
        if (ap.hidden_beacon) hear(FA_KIND_BEACON, AP, "", ap.cap, ap.hear_elements, ap.hear_length, ap.frequency, -40);
        hear(ap.hidden_beacon ? FA_KIND_PROBE_RESPONSE : FA_KIND_BEACON, AP, ap.heard_name ? ap.heard_name : NAME, ap.cap, ap.hear_elements, ap.hear_length, ap.frequency, -40);
        if (ap.also_heard != NULL) ap.also_heard();
    }
    struct cfg80211_scan_info i = { .aborted = false };
    linuxkpi_ieee80211_scan_completed(hw, &i);
    return 0;
}
static int op_add_chanctx(struct ieee80211_hw *hw, struct ieee80211_chanctx_conf *c) {
    assert(hw->wiphy->mtx.locked && c->def.width == NL80211_CHAN_WIDTH_20 && c->def.center_freq1 == c->def.chan->center_freq);
    NOTE("add_chanctx"); return ap.fail_add;
}
static void op_remove_chanctx(struct ieee80211_hw *hw, struct ieee80211_chanctx_conf *c) { assert(hw->wiphy->mtx.locked); NOTE("remove_chanctx"); }
static int op_assign(struct ieee80211_hw *hw, struct ieee80211_vif *v, struct ieee80211_bss_conf *l, struct ieee80211_chanctx_conf *c) {
    assert(hw->wiphy->mtx.locked && l == &v->bss_conf && l->chanctx_conf == c);
    NOTE("assign"); return ap.fail_assign;
}
static void op_unassign(struct ieee80211_hw *hw, struct ieee80211_vif *v, struct ieee80211_bss_conf *l, struct ieee80211_chanctx_conf *c) { assert(hw->wiphy->mtx.locked); NOTE("unassign"); }
static int op_sta_state(struct ieee80211_hw *hw, struct ieee80211_vif *v, struct ieee80211_sta *s, enum ieee80211_sta_state from, enum ieee80211_sta_state to) {
    char what[16];
    assert(hw->wiphy->mtx.locked && s == radio_of(hw)->sta && !memcmp(s->addr, AP, 6) && s->link[0] == &s->deflink);
    assert(from + 1 == (int)to || from == (int)to + 1);
    /* The way it finds its station: every state it is in, as the driver asks (mac.c, ath12k_bss_assoc()). */
    assert(linuxkpi_ieee80211_find_sta(v, AP) == s && linuxkpi_ieee80211_find_sta(v, OTHER) == NULL);
    snprintf(what, sizeof(what), "sta%d%d", from, to); NOTE(what);
    /* What ath12k asks in the peer's association (mac.c), and what it reads out of the answer. */
    if (to == IEEE80211_STA_ASSOC && from < to) {
        struct cfg80211_bss *b = linuxkpi_cfg80211_get_bss(hw->wiphy, NULL, AP, NULL, 0, IEEE80211_BSS_TYPE_ANY, IEEE80211_PRIVACY_ANY);
        assert(linuxkpi_cfg80211_get_bss(hw->wiphy, NULL, OTHER, NULL, 0, IEEE80211_BSS_TYPE_ANY, IEEE80211_PRIVACY_ANY) == NULL);
        if (radio_of(hw)->protect) { asked_bss = b != NULL; bss_had_rsn = b != NULL && fa_element(b->ies->data, b->ies->len, FA_EID_RSN) != NULL; linuxkpi_cfg80211_put_bss(hw->wiphy, b); }
        else asked_bss_open = b != NULL;
    }
    return ap.fail_sta_to != 0 && ap.fail_sta_to == (int)to && from < to ? -110 : 0;
}
static uint32_t he_params_seen;
static void op_link(struct ieee80211_hw *hw, struct ieee80211_vif *v, struct ieee80211_bss_conf *l, u64 what) {
    assert(hw->wiphy->mtx.locked && l == &v->bss_conf);
    he_params_seen = l->he_oper.params;
    NOTE("link"); *(link_changes++ == 0 ? &first_link_change : &second_link_change) = what;
    if (what & BSS_CHANGED_BSSID) assert(!memcmp(l->bssid, AP, 6));
}
static void op_vif(struct ieee80211_hw *hw, struct ieee80211_vif *v, u64 what) {
    assert(hw->wiphy->mtx.locked && what == BSS_CHANGED_ASSOC);
    NOTE(v->cfg.assoc ? "assoc_told" : "disassoc_told");
    if (v->cfg.assoc) assert(!memcmp(v->cfg.ap_addr, AP, 6) && linuxkpi_ieee80211_find_sta(v, v->cfg.ap_addr) != NULL && v->cfg.aid == ap.aid);
}
static int op_conf_tx(struct ieee80211_hw *hw, struct ieee80211_vif *v, u32 link, u16 ac, const struct ieee80211_tx_queue_params *p) { assert(hw->wiphy->mtx.locked); NOTE("conf_tx"); return 0; }
static void op_flush(struct ieee80211_hw *hw, struct ieee80211_vif *v, u32 q, bool drop) { assert(hw->wiphy->mtx.locked && !drop); NOTE("flush"); }
static void op_tx(struct ieee80211_hw *hw, struct ieee80211_tx_control *c, struct sk_buff *skb) {
    assert(!locks && !hw->wiphy->mtx.locked && c->sta == radio_of(hw)->sta);
    assert(IEEE80211_SKB_CB(skb)->control.vif == radio_of(hw)->vif && skb->len >= 24);
    uint8_t kind = skb->data[0];
    assert(!memcmp(skb->data + 4, AP, 6) && !memcmp(skb->data + 10, OWN, 6));
    if (kind == 0x88 || kind == 0x08) { eapol_sent(hw, skb); return; }
    assert(!memcmp(skb->data + 16, AP, 6));
    if (kind == FA_KIND_AUTH) {
        NOTE("auth"); auth_tries++; assert(skb->len == FA_AUTH_LENGTH); memcpy(last_auth, skb->data, skb->len);
    } else if (kind == FA_KIND_ASSOC_REQUEST) {
        NOTE("assoc"); assoc_tries++; memcpy(last_assoc, skb->data, skb->len); last_assoc_length = skb->len;
    } else if (kind == FA_KIND_ACTION) {
        NOTE("action"); action_sent++; action_length = skb->len; assert(skb->len <= sizeof(action)); memcpy(action, skb->data, skb->len);
    } else if (kind == FA_KIND_DEAUTH) {
        NOTE("deauth"); deauths++; assert(skb->len == FA_DEAUTH_LENGTH); memcpy(last_deauth, skb->data, skb->len);
    } else {
        abort();
    }
    if (ap.no_complete) {
        dev_kfree_skb_any(skb);
    } else {
        IEEE80211_SKB_CB(skb)->flags |= IEEE80211_TX_STAT_ACK;
        linuxkpi_ieee80211_tx_status(hw, skb);
    }
    if (kind == FA_KIND_AUTH && !ap.silent_auth) {
        if (ap.bad_auth_first) {
            /* Each of these moves nothing: another BSSID, another station, a step or algorithm that is not the answer, a stale answer to the other frame. */
            give_auth(OTHER, OWN, 0, 2, 0);
            give_auth(AP, OTHER, 0, 2, 0);
            give_auth(AP, OWN, 0, 1, 0);
            give_auth(AP, OWN, 1, 2, 0);
            give_assoc(AP, 0, 7, OPEN_HT, 0);
            uint8_t f[64]; unsigned at = answer_frame(f, FA_KIND_AUTH, AP, OWN);
            memset(f + at, 0, 6); f[at + 2] = 2;
            deliver(f, at + 5, 2412, -40, 0, false);	/* one byte short */
            f[1] = 0x40; deliver(f, at + 6, 2412, -40, 0, false);	/* protected */
            f[1] = 0;
            assert(radio_of(hw)->answer == FA_ANSWER_NONE);
        }
        give_auth(AP, OWN, 0, 2, ap.auth_status);
    } else if (kind == FA_KIND_ASSOC_REQUEST && !ap.silent_assoc) {
        if (ap.bad_assoc_first && assoc_tries == 1) {
            give_assoc(AP, 0, 0, ap.response, 0);	/* no association number */
            give_assoc(AP, 0, FA_AID_MOST + 1, ap.response, 0);
            give_assoc(OTHER, 0, 9, ap.response, 0);
            assert(radio_of(hw)->answer == FA_ANSWER_NONE);
            return;
        }
        give_assoc(AP, ap.assoc_status, ap.aid | 0xc000, ap.response, ap.response_length);
    }
}
static const struct ieee80211_ops ops = { .start = op_start, .stop = op_stop, .add_interface = op_add, .remove_interface = op_remove,
    .hw_scan = op_scan, .cancel_hw_scan = op_cancel, .add_chanctx = op_add_chanctx, .remove_chanctx = op_remove_chanctx,
    .assign_vif_chanctx = op_assign, .unassign_vif_chanctx = op_unassign, .sta_state = op_sta_state, .link_info_changed = op_link,
    .vif_cfg_changed = op_vif, .conf_tx = op_conf_tx, .flush = op_flush, .tx = op_tx, .set_key = op_set_key, .ampdu_action = op_ampdu };

static struct linuxkpi_ieee80211_channel two[3], five[3];
static struct ieee80211_rate rates[12];
static struct ieee80211_sband_iftype_data he_data;
static struct ieee80211_supported_band b2, b5;
#define SAID(text) (strstr(said, text) != NULL)

static void fresh(void) {
    memset(&ap, 0, sizeof(ap));
    ap.hear_elements = OPEN_HT; ap.hear_length = sizeof(OPEN_HT); ap.aid = 7; ap.frequency = 2437; ap.cap = FA_CAP_ESS | FA_CAP_SHORT_SLOT;
    ap.response = WMM_PARAM; ap.response_length = sizeof(WMM_PARAM);
    trouble_after = -1; trouble_asks = evidence_count = rddm = r6_count = 0; first_evidence[0] = 0; said[0] = 0; calls[0] = 0;
    auth_tries = assoc_tries = deauths = scans = link_changes = 0; first_link_change = second_link_change = 0;
    memset(last_deauth, 0, sizeof(last_deauth)); tick = NULL; seen_ssid_length = -2;
    the_hw = linuxkpi_ieee80211_alloc_hw(256, &ops);
    the_hw->vif_data_size = 512; the_hw->sta_data_size = 256; the_hw->chanctx_data_size = 128; the_hw->max_rx_aggregation_subframes = 1024;
    struct wiphy *w = the_hw->wiphy;
    memcpy(w->perm_addr, OWN, 6);
    memset(two, 0, sizeof(two)); memset(five, 0, sizeof(five));
    two[0].center_freq = 2412; two[1].center_freq = 2437; two[2].center_freq = 2472; two[2].flags = IEEE80211_CHAN_DISABLED;
    five[0].center_freq = 5180; five[0].band = NL80211_BAND_5GHZ; five[1].center_freq = 5260; five[1].band = NL80211_BAND_5GHZ; five[1].flags = IEEE80211_CHAN_NO_IR | IEEE80211_CHAN_RADAR;
    five[2].center_freq = 5500; five[2].band = NL80211_BAND_5GHZ; five[2].flags = IEEE80211_CHAN_DISABLED;
    five[0].max_power = 20;
    static const int r[12] = {10, 20, 55, 110, 60, 90, 120, 180, 240, 360, 480, 540};
    for (int i = 0; i < 12; i++) rates[i].bitrate = r[i];
    memset(&he_data, 0, sizeof(he_data));
    he_data.types_mask = NL80211_IFTYPE_STATION; he_data.he_cap.has_he = true;
    memset(he_data.he_cap.he_cap_elem.mac_cap_info, 0x11, 6);
    memset(he_data.he_cap.he_cap_elem.phy_cap_info, 0x22, 11);
    he_data.he_cap.he_cap_elem.phy_cap_info[0] = 0x0c; he_data.he_cap.he_cap_elem.phy_cap_info[6] = 0x80;
    he_data.he_cap.he_mcs_nss_supp.rx_mcs_80 = 0xfffa; he_data.he_cap.he_mcs_nss_supp.tx_mcs_80 = 0xfffa;
    b2 = (struct ieee80211_supported_band){ .band = NL80211_BAND_2GHZ, .channels = two, .n_channels = 3, .bitrates = rates, .n_bitrates = 12, .iftype_data = &he_data, .n_iftype_data = 1 };
    b2.ht_cap.ht_supported = true; b2.ht_cap.cap = 0x0163 | FA_HT_CAP_40 | FA_HT_CAP_SGI_40; b2.ht_cap.ampdu_factor = 3; b2.ht_cap.ampdu_density = 5; b2.ht_cap.mcs[0] = 0xff; b2.ht_cap.mcs[1] = 0xff;
    b5 = b2; b5.band = NL80211_BAND_5GHZ; b5.channels = five; b5.bitrates = rates + 4; b5.n_bitrates = 8;
    b5.vht_cap.vht_supported = true; b5.vht_cap.cap = 0x338001b2 | FA_VHT_CAP_WIDTH | FA_VHT_CAP_SGI_160;
    memset(b5.vht_cap.vht_mcs, 0xfa, 8);
    w->bands[NL80211_BAND_2GHZ] = &b2; w->bands[NL80211_BAND_5GHZ] = &b5;
    fa_set_target_ssid((const unsigned char *)NAME, sizeof(NAME) - 1);
}
static void done(void) {
    assert(!locks && !the_hw->wiphy->mtx.locked);
    struct fa_radio *radio = radio_of(the_hw);
    assert(radio->chanctx == NULL && radio->sta == NULL && radio->vif == NULL && !radio->started && !radio->joining);
    linuxkpi_ieee80211_unregister_hw(the_hw);
    linuxkpi_ieee80211_iffree(the_hw);
    linuxkpi_wiphy_free(the_hw->wiphy);
}
/* Never in any line: the network's name, anybody's, or an address. */
static void log_is_clean(void) {
    assert(!SAID(NAME) && !SAID("fuxi-test") && !SAID("M4 bss") && !SAID("a1:b2") && !SAID("a1b2") && !SAID("d4:"));
}
static const char *const DOWN_TAIL = "unassign,remove_chanctx,remove,stop,";

/* The stay: what comes while it lasts, one each tick of 100 ms. */
static unsigned ticks;
static void stay_frames(void) {
    if (radio_of(the_hw)->vif == NULL || !radio_of(the_hw)->vif->cfg.assoc || ticks > 100) return;
    ticks++;
    if (ticks == 2) { give_data(AP, 0x88, false); give_data(AP, 0x08, false); }	/* QoS data, and data */
    if (ticks == 3) { give_data(AP, 0x88, true); }				/* Ethernet form */
    if (ticks == 4) { give_data(AP, 0xc8, false); give_data(AP, 0x48, false); give_data(OTHER, 0x88, false); }	/* null, null, another's */
    if (ticks == 5) hear(FA_KIND_BEACON, AP, NAME, ap.cap, OPEN_HT, sizeof(OPEN_HT), 2437, -41);
    if (ticks == 6) hear(FA_KIND_BEACON, OTHER, "other", 0, OPEN_HT, 0, 2437, -11);
    if (ticks == 7) hear(FA_KIND_BEACON, AP, NULL, ap.cap, OPEN_HT, sizeof(OPEN_HT), 2437, -42);	/* it hides its name now: the same access point */
    if (ticks == 8) { static const uint8_t ASK[9] = { 3, 0, 1, 0x03, 0x10, 0, 0, 0, 0 }; give_action(AP, ASK, 9); }	/* stages 5 to 7 do not answer this: op_ampdu and op_tx would say */
}
static void trouble_in_the_stay(void) { stay_frames(); if (ticks == 5) fa_m4_evidence("died"); }
static void door_in_the_stay(void) { stay_frames(); if (ticks == 5) { ticks = 200; give_door(FA_KIND_DEAUTH, 8); } }
static void disassoc_in_the_stay(void) { stay_frames(); if (ticks == 5) { ticks = 200; give_door(FA_KIND_DISASSOC, 4); } }
static void lost_in_the_stay(void) { stay_frames(); if (ticks == 5) { ticks = 200; linuxkpi_ieee80211_beacon_loss(radio_of(the_hw)->vif); linuxkpi_ieee80211_connection_loss(radio_of(the_hw)->vif); } }
static const uint8_t RSN_ELEMENT[] = { 48, 20, 1, 0, 0, 0x0f, 0xac, 4, 1, 0, 0, 0x0f, 0xac, 4, 1, 0, 0, 0x0f, 0xac, 2, 0, 0 };
static void rsn_in_the_stay(void) { stay_frames(); if (ticks == 5) { ticks = 200; hear(FA_KIND_BEACON, AP, NAME, ap.cap, RSN_ELEMENT, sizeof(RSN_ELEMENT), 2437, -41); } }
static void rsn_sibling(void) { hear(FA_KIND_BEACON, OTHER, NAME, 0, RSN_ELEMENT, sizeof(RSN_ELEMENT), 2412, -90); }

static void wpa_tick(void) {
    struct fa_radio *r = radio_of(the_hw);
    if (r->vif == NULL || !r->vif->cfg.assoc || r->eapol_length != 0 || wpa.silent) return;
    if (!wpa.m1_given) { wpa.m1_given = true; give_eapol(V_M1, sizeof(V_M1)); return; }
    if (eapols == 1 && !wpa.m3_given) {
        if (wpa.no_m3) return;
        if (wpa.door) { wpa.door = false; give_door(FA_KIND_DEAUTH, 15); return; }
        if (wpa.die_at_m3) { wpa.die_at_m3 = false; fa_m4_evidence("died"); return; }
        if (wpa.forged_first && !wpa.forged_given) { wpa.forged_given = true; give_eapol(V_M3_FORGED, sizeof(V_M3_FORGED)); return; }
        wpa.m3_given = true; give_eapol(V_M3, sizeof(V_M3)); return;
    }
    if (r->reached == IEEE80211_STA_AUTHORIZED && !wpa.stayed && r->rx_ok + r->rx_clear == 0 && lx_os_nanoseconds() > 0) {
        static unsigned count;
        if (++count < 3) return;	/* after the stay has begun and counted from nothing */
        count = 0; wpa.stayed = true;
        uint8_t f[64] = {0};
        f[0] = 0x88; f[1] = 2; memset(f + 4, 0xff, 6); memcpy(f + 10, AP, 6); memcpy(f + 16, AP, 6);
        deliver(f, 40, 2412, 0, RX_FLAG_NO_SIGNAL_VAL | RX_FLAG_DECRYPTED, true);
        deliver(f, 40, 2412, 0, RX_FLAG_NO_SIGNAL_VAL | RX_FLAG_8023, true);
        deliver(f, 40, 2412, 0, RX_FLAG_NO_SIGNAL_VAL, true);
        hear(FA_KIND_BEACON, AP, NAME, ap.cap, WPA_HEARD, sizeof(RSN_ELEMENT) + sizeof(OPEN_HT), 2437, -41);
        if (wpa.goes_open) hear(FA_KIND_BEACON, AP, NAME, FA_CAP_ESS, OPEN_HT, sizeof(OPEN_HT), 2437, -41);
    }
}
static void fresh_wpa(const uint8_t *key) {
    fresh();
    memset(&wpa, 0, sizeof(wpa)); eapols = key_calls = 0; asked_bss = bss_had_rsn = false;
    fa_set_target_join((const unsigned char *)NAME, sizeof(NAME) - 1, key);
    ap.hear_elements = WPA_HEARD; ap.hear_length = sizeof(RSN_ELEMENT) + sizeof(OPEN_HT); ap.cap = FA_CAP_ESS | FA_CAP_SHORT_SLOT | FA_CAP_PRIVACY;
    tick = wpa_tick;
    memset(&net, 0, sizeof(net));
}
static void fresh_net(void) { fresh_wpa(V_PMK); fa_m7_ask(); net.on = true; memset(&serve, 0, sizeof(serve)); serve_end = 0; tick = serve_tick; }
static void m8_count(const struct fa_heard *h, unsigned *count) { assert(h->age_ms < 200000); (*count)++; }
static void m8_again(void) { unsigned s = wpa.fail_key = 0; memset(&wpa, 0, sizeof(wpa)); wpa.stayed = true; eapols = key_calls = s; tick = wpa_tick; }
/* The network's side of the load test: what comes, once the join is answering or, without the knob, staying. */
static bool serve_on;
static int serve_end;
static const uint8_t V_THEM[4] = {10, 0, 0, 77};
static void give_arp_request(const uint8_t *wanted) {
    uint8_t q[28]; memcpy(q, "\0\1\10\0\6\4\0\1", 8); memcpy(q + 8, OTHER, 6); memcpy(q + 14, V_THEM, 4); memset(q + 18, 0, 6); memcpy(q + 24, wanted, 4);
    give_net(FA_TYPE_ARP, q, 28, true);
}
static void give_echo_request(const uint8_t *to, unsigned sequence) {
    uint8_t e[FA_ECHO_LENGTH]; (void)fa_echo_request(e, sizeof(e), V_THEM, to, 0x4242, sequence);
    give_net(FA_TYPE_IP, e, sizeof(e), false);
}
static void give_udp(void) {
    uint8_t u[64] = {0}; fa_ip_header(u, 17, V_THEM, V_ME, sizeof(u), 3); u[23] = 9; u[25] = 44;
    give_net(FA_TYPE_IP, u, sizeof(u), false);
}
static void serve_tick(void) {
    struct fa_radio *r = radio_of(the_hw);
    wpa_tick();
    if (!serve_on || serve.given || !r->leased || (!r->serving && target_serve)) return;
    if (!target_serve && !strstr(said, "M5 after 5 s")) return;
    serve.given = true;
    give_arp_request(V_ME);
    give_arp_request(V_THEM);			/* for another: not kept */
    give_echo_request(V_THEM, 1);		/* for another: not kept */
    give_udp();					/* only counted */
    give_echo_request(V_ME, 1);
    /* More than the inbox holds, with nothing taken out in between: two are lost, and that is said. */
    give_arp_request(V_ME); give_echo_request(V_ME, 2); give_arp_request(V_ME); give_echo_request(V_ME, 3);
    if (serve_end == 1) give_door(FA_KIND_DEAUTH, 3);
    if (serve_end == 2) { linuxkpi_ieee80211_connection_loss(r->vif); }
    if (serve_end == 3) fa_m4_evidence("died");
}
static const uint8_t *find_in_request(unsigned number) { return fa_element(last_assoc + 28, last_assoc_length - 28, number); }
/* What was said and done, when an assertion stops the run. */
static void on_abort(int s) { fprintf(stderr, "calls: %s\nsaid:\n%s\n", calls, said); signal(SIGABRT, SIG_DFL); }

int main(void) {
    signal(SIGABRT, on_abort);
    /* No name: nothing at all. */
    fresh(); fa_set_target_ssid(NULL, 0);
    assert(fa_m5_station(the_hw, false) == 0 && !calls[0] && SAID("M5 not run: no usable fuxiwifissid"));
    done();
    fresh(); fa_set_target_ssid((const unsigned char *)"x", 33);
    assert(fa_m5_station(the_hw, false) == 0 && !calls[0]);
    done();

    /* The whole way, at 2.4 GHz with HT and WMM: in mac80211's order, the proof, the stay, the deauthentication. */
    fresh(); ticks = 0; tick = stay_frames;
    linuxkpi_ieee80211_ifattach(the_hw);
    {
        unsigned long long began = now;
        assert(fa_m5_station(the_hw, false) == 0);
        assert(now - began >= FA_JOIN_STAY_NS && now - began < FA_JOIN_STAY_NS + FA_REGD_WAIT_NS + 2000000000ull);
    }
    assert(!strcmp(calls, "start,add,scan,add_chanctx,assign,link,sta01,auth,sta12,assoc,sta23,sta34,assoc_told,conf_tx,conf_tx,conf_tx,conf_tx,link,"
                          "deauth,flush,sta43,sta32,sta21,sta10,disassoc_told,unassign,remove_chanctx,remove,stop,"));
    assert(scans == 1 && seen_ssid_length == (int)sizeof(NAME) - 1 && !memcmp(seen_ssid, NAME, sizeof(NAME) - 1));
    assert(first_link_change == (BSS_CHANGED_BSSID | BSS_CHANGED_BASIC_RATES | BSS_CHANGED_BEACON_INT));
    assert(second_link_change == (BSS_CHANGED_BEACON_INT | BSS_CHANGED_BEACON_INFO | BSS_CHANGED_ERP_SLOT | BSS_CHANGED_ERP_PREAMBLE | BSS_CHANGED_QOS));
    assert(SAID("FUXI ATH12K: M5 target ssid length 13\n"));
    assert(SAID("FUXI ATH12K: M5 auth ok assoc ok aid 7 freq 2437\n"));
    assert(SAID("FUXI ATH12K: M5 rx data 3 beacons 2 rssi -42\n"));
    assert(SAID("M5 after 5 s: rx data 3 beacons 2") && SAID("M5 stayed 20") && SAID("M5 deauthentication sent") && SAID("M5 left"));
    assert(r6_count >= 5 && !rddm && !evidence_count && auth_tries == 1 && assoc_tries == 1 && deauths == 1);
    assert(strstr(said, "M5 auth ok") < strstr(said, "M5 rx data") && strstr(said, "M5 rx data") < strstr(said, "M5 deauthentication"));
    log_is_clean();
    /* The frames themselves: nothing about protection in what we ask, our abilities at 20 MHz, HE and WMM as offered. */
    assert(last_auth[24] == 0 && last_auth[25] == 0 && last_auth[26] == 1 && last_auth[28] == 0);
    assert(last_deauth[0] == FA_KIND_DEAUTH && last_deauth[24] == FA_REASON_LEAVING && last_deauth[25] == 0);
    assert((last_assoc[24] | last_assoc[25] << 8) == (FA_CAP_ESS | FA_CAP_SHORT_SLOT));
    {
        const uint8_t *e = find_in_request(FA_EID_SSID);
        assert(e && e[1] == sizeof(NAME) - 1 && !memcmp(e + 2, NAME, e[1]));
        assert(fa_elements_whole(last_assoc + 28, last_assoc_length - 28));
        assert(find_in_request(FA_EID_RSN) == NULL && find_in_request(FA_EID_WAPI) == NULL);
        assert(fa_vendor_element(last_assoc + 28, last_assoc_length - 28, 0x00, 0x50, 0xf2, 0x01) == NULL);
        assert(fa_ext_element(last_assoc + 28, last_assoc_length - 28, 108) == NULL); /* no EHT */
        e = find_in_request(FA_EID_RATES); assert(e && e[1] == 8 && e[2] == 0x82 && e[6] == 12);
        e = find_in_request(FA_EID_RATES_MORE); assert(e && e[1] == 4);
        e = find_in_request(FA_EID_HT); assert(e && e[1] == FA_HT_BODY);
        assert(((e[2] | e[3] << 8) & (FA_HT_CAP_40 | FA_HT_CAP_SGI_40)) == 0 && e[4] == (3 | 5 << 2) && e[5] == 0xff && e[6] == 0xff);
        assert(find_in_request(FA_EID_VHT) == NULL);
        assert(fa_ext_element(last_assoc + 28, last_assoc_length - 28, FA_EXT_HE) == NULL); /* it has no HE here */
        assert(fa_wmm_element(last_assoc + 28, last_assoc_length - 28) != NULL);
    }
    done();

    /* 5 GHz with VHT and HE on both sides, no channel element, spectrum management, and the driver's station as asked. */
    fresh(); ticks = 0; tick = stay_frames;
    {
        static uint8_t el[256]; unsigned n = 0;
        static const uint8_t r[] = {1, 8, 0x8c, 18, 0x98, 36, 0xb0, 72, 96, 108};
        memcpy(el + n, r, sizeof(r)); n += sizeof(r);
        el[n++] = 45; el[n++] = 26; memset(el + n, 0, 26); el[n] = 0x6f; el[n + 1] = 0x01; el[n + 2] = 0x17; el[n + 3] = 0xff; el[n + 4] = 0xff; n += 26;
        el[n++] = 191; el[n++] = 12; memset(el + n, 0, 12); el[n] = 0xb2; el[n + 4] = 0xfa; el[n + 5] = 0xff; n += 12;
        el[n++] = 255; el[n++] = 1 + 6 + 11 + 4 + 4; el[n++] = 35; memset(el + n, 0, 6 + 11); el[n + 6] = FA_HE_PHY0_160 | 0x04; n += 17; el[n++] = 0xfa; el[n++] = 0xff; el[n++] = 0xfa; el[n++] = 0xff; el[n++] = 0xea; el[n++] = 0xff; el[n++] = 0xea; el[n++] = 0xff;
        el[n++] = 255; el[n++] = 7; el[n++] = 36; el[n++] = 0x34; el[n++] = 0x12; el[n++] = 0x08; memset(el + n, 0, 3); n += 3;
        ap.hear_elements = el; ap.hear_length = n; ap.frequency = 5180; ap.cap = FA_CAP_ESS | FA_CAP_SPECTRUM; ap.aid = 2007;
        ap.response = el; ap.response_length = n;
        assert(fa_m5_station(the_hw, false) == 0);
    }
    assert(SAID("M5 auth ok assoc ok aid 2007 freq 5180\n") && SAID("HT 1 VHT 1 HE 1, 2 streams, QoS 0"));
    assert(he_params_seen == 0x00081234);	/* three bytes of parameters, and the colour's byte */
    assert(!strstr(calls, "conf_tx"));
    {
        const uint8_t *e = find_in_request(FA_EID_VHT);
        assert(e && e[1] == FA_VHT_BODY && (e[2] & (FA_VHT_CAP_WIDTH | FA_VHT_CAP_SGI_160)) == 0 && e[6] == 0xfa);
        e = fa_ext_element(last_assoc + 28, last_assoc_length - 28, FA_EXT_HE);
        assert(e && e[1] == FA_HE_OFFER_MOST - 2 && e[3] == 0x11 && e[9] == 0 && (e[15] & FA_HE_PHY6_PPE) == 0 && e[9 + 11] == 0xfa);
        e = find_in_request(FA_EID_POWER_CAP); assert(e && e[1] == 2 && e[3] == 20);
        e = find_in_request(FA_EID_CHANNELS); assert(e && e[2] == 36 && e[3] == 1);
        e = find_in_request(FA_EID_RATES); assert(e && e[1] == 8 && e[2] == 0x8c);
        assert(fa_ext_element(last_assoc + 28, last_assoc_length - 28, 108) == NULL);
    }
    log_is_clean();
    done();

    /* Refused for each kind of protection, before any frame or channel. The name went out in the probes alone. */
    {
        static const uint8_t WPA[] = { 221, 6, 0x00, 0x50, 0xf2, 1, 1, 0 }, WAPI[] = { 68, 2, 1, 0 }, LIAR[] = { 3, 1, 6, 48, 250, 0 };
        const struct { const uint8_t *e; unsigned n, cap; const char *line; } kinds[] = {
            { RSN_ELEMENT, sizeof(RSN_ELEMENT), FA_CAP_ESS | FA_CAP_PRIVACY, "M5 refused: the target has an RSN element (1 frames), only open networks are allowed at this stage" },
            { WPA, sizeof(WPA), FA_CAP_ESS, "M5 refused: the target has a WPA element" },
            { WAPI, sizeof(WAPI), FA_CAP_ESS, "M5 refused: the target has a WAPI element" },
            { OPEN_HT, sizeof(OPEN_HT), FA_CAP_ESS | FA_CAP_PRIVACY, "M5 refused: the target has the privacy bit" },
            { LIAR, sizeof(LIAR), FA_CAP_ESS, "M5 refused: the target has elements that cannot be read to their end" },
        };
        for (unsigned i = 0; i < sizeof(kinds) / sizeof(kinds[0]); i++) {
            fresh();
            ap.hear_elements = kinds[i].e; ap.hear_length = kinds[i].n; ap.cap = kinds[i].cap;
            assert(fa_m5_station(the_hw, false) == 0);
            assert(!strcmp(calls, "start,add,scan,remove,stop,") && SAID(kinds[i].line) && !rddm && !evidence_count);
            assert(!SAID("M5 auth ok") && !auth_tries && !radio_of(the_hw)->joining);
            log_is_clean();
            done();
        }
    }
    /* The same name with protection anywhere refuses it all, however weak: a mixed network is no open one. */
    fresh(); ap.also_heard = rsn_sibling;
    assert(fa_m5_station(the_hw, false) == 0 && !strcmp(calls, "start,add,scan,remove,stop,") && SAID("M5 refused: the target has an RSN element"));
    done();

    /* Not heard: two scans, and only the name that was asked for counts. Neighbours are not taken instead. */
    fresh(); ap.not_heard = true;
    assert(fa_m5_station(the_hw, false) == 0 && scans == FA_JOIN_SCANS && !auth_tries);
    assert(!strcmp(calls, "start,add,scan,scan,remove,stop,") && SAID("M5 not proven: the target was not heard in 2 scans"));
    log_is_clean();
    done();
    /* A name that only starts the same way is not it. */
    fresh(); ap.heard_name = "fuxi-test-netw";
    assert(fa_m5_station(the_hw, false) == 0 && scans == FA_JOIN_SCANS && !auth_tries);
    done();

    /* A hidden network answers by name in its probe response. */
    fresh(); ticks = 0; ap.hidden_beacon = true; tick = stay_frames;
    assert(fa_m5_station(the_hw, false) == 0 && SAID("M5 auth ok assoc ok aid 7"));
    done();

    /* Where it says it is, not where it was heard. A channel the domain took away is not used. */
    fresh(); ticks = 0; ap.frequency = 2412; tick = stay_frames; /* its element says channel 6 */
    assert(fa_m5_station(the_hw, false) == 0 && SAID("M5 channel 2437 MHz") && SAID("freq 2437\n"));
    done();
    fresh(); two[1].flags = IEEE80211_CHAN_DISABLED;
    assert(fa_m5_station(the_hw, false) == 0 && SAID("M5 not proven: the access point's channel may not be used here") && !strstr(calls, "add_chanctx"));
    done();

    /* Silence after a frame that came back: three tries, bounded, nothing proven, no firmware blamed. */
    fresh(); ap.silent_auth = true;
    {
        unsigned long long began = now;
        assert(fa_m5_station(the_hw, false) == 0);
        assert(auth_tries == 3 && now - began < 3 * FA_JOIN_ANSWER_NS + FA_REGD_WAIT_NS + 1000000000ull);
    }
    assert(!strcmp(calls, "start,add,scan,add_chanctx,assign,link,sta01,auth,auth,auth,sta10,unassign,remove_chanctx,remove,stop,"));
    assert(SAID("M5 not proven: no answer to authentication") && SAID("frames sent 3 done 3 acked 3") && !rddm && !evidence_count && !deauths);
    done();
    /* A frame that never came back: the firmware's account, before the way down. */
    fresh(); ap.silent_auth = true; ap.no_complete = true;
    assert(fa_m5_station(the_hw, false) == -EIO && rddm && !strcmp(first_evidence, "management frames never came back from the firmware"));
    assert(strstr(calls, "rddm,sta10,") != NULL && !deauths);
    done();
    /* Silence to the association: authenticated, so the deauthentication goes out. */
    fresh(); ap.silent_assoc = true;
    assert(fa_m5_station(the_hw, false) == 0 && assoc_tries == 3 && deauths == 1);
    assert(strstr(calls, "assoc,assoc,assoc,deauth,flush,sta21,sta10,") != NULL && !strstr(calls, "assoc_told"));
    done();

    /* Refused by the access point. */
    fresh(); ap.auth_status = 13;
    assert(fa_m5_station(the_hw, false) == 0 && auth_tries == 1 && SAID("M5 not proven: authentication refused, code 13") && !deauths && !assoc_tries);
    done();
    fresh(); ap.assoc_status = 17;
    assert(fa_m5_station(the_hw, false) == 0 && SAID("M5 not proven: association refused, code 17") && deauths == 1 && !strstr(calls, "sta23"));
    done();

    /* Answers that are no answers. */
    fresh(); ticks = 0; ap.bad_auth_first = true; ap.bad_assoc_first = true; tick = stay_frames;
    assert(fa_m5_station(the_hw, false) == 0 && auth_tries == 1 && assoc_tries == 2 && SAID("M5 auth ok assoc ok aid 7"));
    done();

    /* The driver or the firmware says no at each step: its account, then what got made is undone. */
    fresh(); ap.fail_add = -5;
    assert(fa_m5_station(the_hw, false) == -5 && !strcmp(first_evidence, "channel context refused"));
    assert(strstr(calls, "add_chanctx,rddm,remove,stop,") != NULL);
    done();
    fresh(); ap.fail_assign = -5;
    assert(fa_m5_station(the_hw, false) == -5 && strstr(calls, "assign,rddm,remove_chanctx,remove,stop,") != NULL && !strstr(calls, "unassign"));
    done();
    fresh(); ap.fail_sta_to = IEEE80211_STA_NONE;
    assert(fa_m5_station(the_hw, false) == -110 && !strcmp(first_evidence, "peer create or vdev start failed"));
    assert(strstr(calls, "sta01,rddm,unassign,remove_chanctx,remove,stop,") != NULL && !auth_tries);
    done();
    fresh(); ap.fail_sta_to = IEEE80211_STA_AUTH;
    assert(fa_m5_station(the_hw, false) == -110 && strstr(calls, "sta12,rddm,sta10,") != NULL && !deauths);
    done();
    fresh(); ap.fail_sta_to = IEEE80211_STA_AUTHORIZED;
    assert(fa_m5_station(the_hw, false) == -110 && strstr(calls, "sta34,rddm,sta32,sta21,sta10,") != NULL && !deauths && !SAID("M5 auth ok"));
    done();

    /* The firmware dies during the stay: the end comes at once, its account first, and no frame goes out. */
    fresh(); ticks = 0; tick = trouble_in_the_stay;
    {
        unsigned long long began = now;
        assert(fa_m5_station(the_hw, false) == -EIO);
        assert(now - began < FA_JOIN_STAY_NS / 2 && rddm && !deauths && SAID("M5 auth ok assoc ok"));
    }
    assert(strstr(calls, "rddm,sta43,sta32,sta21,sta10,disassoc_told,") != NULL && SAID("M5 rx data"));
    done();
    /* It dies before anything: the scan is not asked for. */
    fresh(); trouble_after = 0;
    assert(fa_m5_station(the_hw, false) == -EIO && !strcmp(calls, "start,add,rddm,remove,stop,"));
    done();

    /* The access point shows us the door, or the link is gone: no deauthentication after that, and the stay ends. */
    fresh(); ticks = 0; tick = door_in_the_stay;
    {
        unsigned long long began = now;
        assert(fa_m5_station(the_hw, false) == 0 && now - began < FA_JOIN_STAY_NS / 2 && !deauths && !rddm);
    }
    assert(SAID("shown us the door, reason 8") && SAID("M5 rx data") && !strstr(calls, "deauth"));
    done();
    fresh(); ticks = 0; tick = disassoc_in_the_stay;
    assert(fa_m5_station(the_hw, false) == 0 && !deauths && SAID("shown us the door, reason 4"));
    done();
    fresh(); ticks = 0; tick = lost_in_the_stay;
    assert(fa_m5_station(the_hw, false) == 0 && !deauths && SAID("M5 connection loss from the driver") && SAID("M5 beacon losses 1"));
    done();
    /* It begins to ask for protection: that is the end of it too, and we say goodbye. */
    fresh(); ticks = 0; tick = rsn_in_the_stay;
    assert(fa_m5_station(the_hw, false) == 0 && deauths == 1 && SAID("the access point now asks for protection: leaving"));
    done();

    /* No buffer: said, bounded, no frame. */
    fresh(); fail_skb = 1;
    assert(fa_m5_station(the_hw, false) == 0 && SAID("M5 no buffer for the authentication frame") && !auth_tries);
    fail_skb = 0;
    done();

    /* ---- stage 6 ---- */
    memcpy(WPA_HEARD, RSN_ELEMENT, sizeof(RSN_ELEMENT)); memcpy(WPA_HEARD + sizeof(RSN_ELEMENT), OPEN_HT, sizeof(OPEN_HT));
    /* The whole way: mac80211's order, the association told before the handshake, the keys, then authorized, and the way down. */
    fresh_wpa(V_PMK);
    assert(fa_m5_station(the_hw, false) == 0);
    assert(!strcmp(calls, "start,add,scan,add_chanctx,assign,link,sta01,auth,sta12,assoc,sta23,assoc_told,conf_tx,conf_tx,conf_tx,conf_tx,link,key_p,key_g,sta34,"
                          "deauth,flush,off_g,off_p,sta43,sta32,sta21,sta10,disassoc_told,unassign,remove_chanctx,remove,stop,"));
    assert(eapols == 2 && asked_bss && bss_had_rsn);
    assert(SAID("M6 target offers group 4 pairwise 0x10 akm 0x4") && SAID("message 1, answered") && SAID("message 3, good, answered"));
    assert(SAID("M6 message 4 after 0 ms: data done 2 acked 2") && SAID("M6 keys to the driver: 0, group number 1") && SAID("M6 4-way ok pairwise group installed"));
    assert(SAID("M6 rx ok 2 dropped unprotected 1\n") && SAID("M5 auth ok assoc ok aid 7") && !rddm && !evidence_count);
    assert(strstr(said, "M6 4-way ok") < strstr(said, "M5 auth ok") && strstr(said, "M5 auth ok") < strstr(said, "M6 rx ok"));
    {
        const uint8_t *e = find_in_request(FA_EID_RSN);
        assert(e && e[1] == 20 && !memcmp(e, FA_RSN_OURS, FA_RSN_LENGTH) && (last_assoc[24] & FA_CAP_PRIVACY) != 0);
        assert(e > find_in_request(FA_EID_RATES) && e < find_in_request(FA_EID_HT));	/* Table 9-35 */
    }
    /* Nothing of the key is left, here or in the log. */
    {
        struct fa_radio *radio = radio_of(the_hw);
        static const struct fa_wpa zero;
        assert(!memcmp(&radio->wpa, &zero, sizeof(zero)) && !memcmp(target_key, zero.pmk, sizeof(target_key)));
        for (unsigned i = 0; i < 2; i++) { assert(!memcmp(((struct ieee80211_key_conf *)radio->key_memory[i])->key, zero.pmk, 16)); }
    }
    log_is_clean();
    done();
    /* Fresh after it: a later open network is not taken as protected. */
    fresh(); assert(!target_protected); done();

    /* Forged first: a message that fails its check does not end the wait, and the genuine one still counts. */
    fresh_wpa(V_PMK); wpa.forged_first = true;
    assert(fa_m5_station(the_hw, false) == 0 && SAID("not good: its signature is not right") && SAID("M6 4-way ok") && eapols == 2);
    done();

    /* The wrong key: its message 3 fails, bounded, no key given, and we say goodbye. Never authorized. */
    {
        static uint8_t wrong[32]; memset(wrong, 0x5a, 32);
        fresh_wpa(wrong); wpa.any_reply = true;
        unsigned long long began = now;
        assert(fa_m5_station(the_hw, false) == 0 && now - began < FA_HANDSHAKE_NS + FA_REGD_WAIT_NS + 2000000000ull);
        assert(SAID("M6 not proven: the handshake did not come through") && SAID("a message was not good: its signature") && !strstr(calls, "key_") && !strstr(calls, "sta34"));
        assert(deauths == 1 && !SAID("M5 auth ok") && !rddm && !evidence_count);
        log_is_clean();
        done();
    }
    /* It never begins, or never answers message 2: bounded, and which of them is said. */
    fresh_wpa(V_PMK); wpa.silent = true;
    assert(fa_m5_station(the_hw, false) == 0 && SAID("the access point did not begin it") && !eapols && deauths == 1);
    done();
    fresh_wpa(V_PMK); wpa.no_m3 = true;
    assert(fa_m5_station(the_hw, false) == 0 && SAID("our message 2 was not answered") && eapols == 1 && deauths == 1);
    done();
    /* It shows us the door in the middle: at once, and no goodbye after that. */
    fresh_wpa(V_PMK); wpa.door = true;
    {
        unsigned long long began = now;
        assert(fa_m5_station(the_hw, false) == 0 && now - began < FA_REGD_WAIT_NS + 2000000000ull && SAID("shown us the door (a wrong passphrase gives this)") && !deauths);
    }
    done();
    /* The firmware says no to a key: its account, and the one it took comes back. */
    fresh_wpa(V_PMK); wpa.fail_key = 2;
    assert(fa_m5_station(the_hw, false) == -5 && !strcmp(first_evidence, "keys refused or peer not authorized") && strstr(calls, "key_p,key_g,rddm,off_p,") && !strstr(calls, "off_g"));
    done();
    fresh_wpa(V_PMK); wpa.fail_key = 1;
    assert(fa_m5_station(the_hw, false) == -5 && !strstr(calls, "key_g") && !strstr(calls, "off_"));
    done();
    /* The firmware dies during the handshake. */
    fresh_wpa(V_PMK); wpa.die_at_m3 = true;
    assert(fa_m5_station(the_hw, false) == -EIO && rddm && !strstr(calls, "key_") && !deauths);
    done();
    /* The beacon loses its RSN element during the stay: that is no network of ours any more. */
    fresh_wpa(V_PMK); wpa.goes_open = true;
    assert(fa_m5_station(the_hw, false) == 0 && SAID("now asks for something else: leaving") && deauths == 1);
    done();

    /* Refused before any frame: an open network of the name, or an offer we do not make. */
    fresh_wpa(V_PMK); ap.hear_elements = OPEN_HT; ap.hear_length = sizeof(OPEN_HT); ap.cap = FA_CAP_ESS;
    assert(fa_m5_station(the_hw, false) == 0 && !strcmp(calls, "start,add,scan,remove,stop,") && SAID("M6 refused: the target has nothing (1 frames)") && !auth_tries);
    done();
    {
        static const struct { unsigned at; uint8_t value; const char *text; } offers[] = {
            { 7, 2, "M6 refused: its group cipher is not CCMP" }, { 19, 8, "M6 refused: it does not take a WPA2 passphrase's key" }, { 20, 0x40, "M6 refused: it requires protected management frames" },
        };
        for (unsigned i = 0; i < 3; i++) {
            static uint8_t e[sizeof(RSN_ELEMENT)];
            memcpy(e, RSN_ELEMENT, sizeof(e)); e[offers[i].at] = offers[i].value;
            fresh_wpa(V_PMK); ap.hear_elements = e; ap.hear_length = sizeof(e);
            assert(fa_m5_station(the_hw, false) == 0 && !strcmp(calls, "start,add,scan,remove,stop,") && SAID(offers[i].text) && !auth_tries);
            done();
        }
    }
    /* What is left of a key is never given to an open network's way: stage 5 asks for no bss. */
    fresh(); ticks = 0; tick = stay_frames;
    assert(fa_m5_station(the_hw, false) == 0 && !asked_bss_open);
    done();

    /* ---- stage 7's load test, fuxiath12kq=0x20 ---- */
    /* Answers what is for it, keeps nothing else, says its numbers, is bounded, and then the stay, the address back and goodbye as ever. */
    fresh_net(); fa_m7_serve(); serve_on = true;
    {
        unsigned long long began = now;
        assert(fa_m5_station(the_hw, false) == 0 && !rddm && !evidence_count);
        assert(now - began >= FA_SERVE_NS + FA_JOIN_STAY_NS && now - began < FA_SERVE_NS + FA_JOIN_STAY_NS + FA_REGD_WAIT_NS + 5000000000ull);
    }
    assert(serve.arps == 2 && serve.echoes == 2 && !serve.wrong);
    assert(SAID("M7 serving for 600 s") && SAID("arp 2 echo 2") && SAID("inbox lost 2") && SAID("M7 served 600"));
    assert(strstr(said, "M7 ping beyond") < strstr(said, "M7 serving for") && strstr(said, "M7 served") < strstr(said, "M5 stayed") && strstr(said, "M5 stayed") < strstr(said, "M7 the address given back"));
    assert(net.releases == 1 && deauths == 1);
    {
        unsigned lines = 0; const char *at = said;
        while ((at = strstr(at, "M7 serving: rx ok")) != NULL) { lines++; at++; }
        assert(lines >= 59 && lines <= 61);
    }
    log_is_clean();
    assert(!SAID("02:") && !SAID("10.0.0.77"));
    done();
    /* Without an address it does not begin. */
    fresh_net(); fa_m7_serve(); serve_on = true; net.silent = true;
    assert(fa_m5_station(the_hw, false) == 0 && !SAID("M7 serving") && !net.releases);
    done();
    /* The door, the link and the firmware each end it at once. The door and the firmware mean no goodbye. */
    fresh_net(); fa_m7_serve(); serve_on = true; serve_end = 1;
    {
        unsigned long long began = now;
        assert(fa_m5_station(the_hw, false) == 0 && now - began < FA_SERVE_NS / 2 && SAID("M7 served") && !deauths && !net.releases);
    }
    done();
    fresh_net(); fa_m7_serve(); serve_on = true; serve_end = 2;
    {
        unsigned long long began = now;
        assert(fa_m5_station(the_hw, false) == 0 && now - began < FA_SERVE_NS / 2 && SAID("M7 served") && !deauths);
    }
    done();
    fresh_net(); fa_m7_serve(); serve_on = true; serve_end = 3;
    {
        unsigned long long began = now;
        assert(fa_m5_station(the_hw, false) == -EIO && now - began < FA_SERVE_NS / 2 && rddm && SAID("M7 served") && !net.releases);
    }
    done();
    /* Stage 6 and a stage 7 without the knob do not begin it, and a stage 7 without the knob keeps no request. */
    fresh_wpa(V_PMK); fa_m7_serve(); serve_on = true; net.on = false;
    assert(fa_m5_station(the_hw, false) == 0 && !SAID("M7 serving") && !net.discovers);
    done();
    fresh_net(); serve_on = true;
    assert(fa_m5_station(the_hw, false) == 0 && !SAID("M7 serv") && !SAID("arp 2") && !serve.arps && !serve.echoes && net.echoes == 2);
    done();
    serve_on = false;

    /* ---- stage 7 ---- */
    /* The whole way: an address, the router, an echo to it and beyond it, the stay, the address given back, then goodbye. */
    fresh_net();
    assert(fa_m5_station(the_hw, false) == 0 && !rddm && !evidence_count);
    assert(net.discovers == 1 && net.requests == 1 && net.arps == 1 && net.echoes == 2 && net.releases == 1 && !net.everybody_flags);
    assert(SAID("M7 DHCP offer server 10.0.0.1 yiaddr 10.0.0.9") && SAID("M7 DHCP ack server 10.0.0.1 yiaddr 10.0.0.9, lease 3600 s") && SAID("M7 network mask 255.255.255.0 router 10.0.0.1"));
    assert(SAID("M7 router found") && SAID("M7 ping router 10.0.0.1 ok, try 1") && SAID("M7 ping beyond 1.1.1.1 ok, try 1") && SAID("M7 the address given back") && SAID("kept lost 0"));
    assert(strstr(said, "M6 4-way ok") < strstr(said, "M7 DHCP offer") && strstr(said, "M7 ping beyond") < strstr(said, "M5 stayed") && strstr(said, "M5 stayed") < strstr(said, "M7 the address given back"));
    assert(strstr(said, "M7 the address given back") < strstr(said, "M5 deauthentication sent"));
    log_is_clean();
    done();
    /* Stage 6 never asks: the same network with no fa_m7_ask(). */
    fresh_wpa(V_PMK); net.on = false;
    assert(fa_m5_station(the_hw, false) == 0 && !SAID("M7 ") && !net.discovers && SAID("M6 4-way ok"));
    done();
    /* No offer: four tries, the last two asking for an answer to all, bounded, nothing proven, the stay and the way down still follow. */
    fresh_net(); net.silent = true;
    {
        unsigned long long began = now;
        assert(fa_m5_station(the_hw, false) == 0 && now - began < FA_REGD_WAIT_NS + FA_DHCP_TRIES * FA_DHCP_ANSWER_NS + FA_JOIN_STAY_NS + 3000000000ull);
    }
    assert(net.discovers == 4 && net.everybody_flags == 2 && SAID("M7 not proven: no DHCP offer after 4 discovers") && !net.releases && deauths == 1 && SAID("M5 stayed"));
    done();
    /* An answer only to all, which is what the group's key alone would show. */
    fresh_net(); net.broadcast_only = true;
    assert(fa_m5_station(the_hw, false) == 0 && net.discovers == 3 && SAID("try 3") && SAID("M7 DHCP ack") && net.releases == 1);
    done();
    /* Design W3: what comes unciphered is counted, and never kept. */
    fresh_net(); net.unprotected = true;
    assert(fa_m5_station(the_hw, false) == 0 && SAID("M7 not proven: no DHCP offer") && strstr(said, "dropped unprotected 4") && !net.requests);
    done();
    fresh_net(); net.nak = true;
    assert(fa_m5_station(the_hw, false) == 0 && SAID("M7 not proven: the server said no") && !net.arps && !net.releases);
    done();
    fresh_net(); net.no_router = true;
    assert(fa_m5_station(the_hw, false) == 0 && SAID("M7 the network names no router") && !net.arps && net.releases == 1);
    done();
    fresh_net(); net.no_arp = true;
    assert(fa_m5_station(the_hw, false) == 0 && SAID("M7 not proven: the router does not answer ARP") && net.arps == 3 && !net.echoes && net.releases == 1);
    done();
    fresh_net(); net.no_beyond = true;
    assert(fa_m5_station(the_hw, false) == 0 && SAID("M7 ping router 10.0.0.1 ok") && SAID("M7 ping beyond 1.1.1.1 no answer to 3 tries") && net.releases == 1);
    done();

    /* ---- stage 8's auto join, fuxiwifiauto=1 ---- */
    {
        const uint8_t *name = (const uint8_t *)NAME; unsigned n = sizeof(NAME) - 1;
        static const struct fa_wpa zero_wpa;
        static struct fa_ios_state state;
        struct fa_radio *radio;
        const char *alone_input = NULL;

        /* Off by default: nothing given, nothing done, however long it waits. */
        fresh_wpa(V_PMK); memset(&m8, 0, sizeof(m8)); wpa.stayed = true; radio = radio_of(the_hw); fa_ios_start(&M8_INPUT); m8_alones = 0;
        assert(fa_m8_up(the_hw) == 0);
        now += FA_AUTO_WAIT_NS * 2; ios_auto_step(radio); ios_auto_step(radio);
        assert(!SAID("M8 auto join") && !m8.ups && !radio->ios_joined && !m8_alones);
        assert(fa_ios_leave() == FA_IOS_DONE); station_down(radio); done();

        /* Given: it waits for the radio to be asked for, or for its time; then joins as wifid would, with nobody asking, once. */
        fresh_wpa(V_PMK); memset(&m8, 0, sizeof(m8)); wpa.stayed = true; radio = radio_of(the_hw); fa_ios_start(&M8_INPUT); m8_alones = 0;
        fa_set_auto_join(name, n, V_PMK);
        assert(fa_m8_up(the_hw) == 0);
        ios_auto_step(radio); assert(!SAID("M8 auto join") && auto_name_length == n);
        assert(fa_ios_power(1) == FA_IOS_DONE);
        m8_again(); ios_auto_step(radio);
        assert(SAID("M8 auto join: name of 13 bytes") && SAID("M8 auto join: answer 0") && SAID("M6 4-way ok") && m8.ups == 1 && m8_alones == 1 && m8_alone == FA_IOS_DONE);
        assert(radio->ios_joined && radio->ios_auto_joined && auto_name_length == 0);
        fa_ios_state(&state); assert(state.phase == FA_IOS_JOINED && state.bss.ssid_length == n && !memcmp(state.bss.bssid, AP, 6));
        { static const uint8_t none[32]; assert(!memcmp(auto_key, none, 32)); }
        ios_auto_step(radio); assert(m8.ups == 1 && m8_alones == 1);	/* once */
        log_is_clean();
        /* Its key is gone from what it read too: only what the join itself keeps for its messages. */
        assert(strstr(said, "M8 auto join: name") < strstr(said, "M8 joined"));

        /* Lost: said, and no other join behind anyone's back. */
        m8.losts = 0; linuxkpi_ieee80211_connection_loss(radio->vif); ios_step(radio);
        assert(m8.losts == 1 && !radio->ios_joined && !radio->ios_auto_joined && SAID("M8 auto join: the network is lost, not joining again"));
        { unsigned ups = m8.ups; now += FA_AUTO_WAIT_NS * 2; ios_auto_step(radio); ios_step(radio); assert(m8.ups == ups && !radio->ios_joined); }
        station_down(radio); done();

        /* The radio asked for by nobody: the time ends the wait. */
        fresh_wpa(V_PMK); memset(&m8, 0, sizeof(m8)); wpa.stayed = true; radio = radio_of(the_hw); fa_ios_start(&M8_INPUT); m8_alones = 0;
        fa_set_auto_join(name, n, V_PMK);
        assert(fa_m8_up(the_hw) == 0);
        ios_auto_step(radio); assert(!m8.ups);
        now += FA_AUTO_WAIT_NS + 1; m8_again(); ios_auto_step(radio); assert(m8.ups == 1 && radio->ios_auto_joined);
        /* wifid leaves: that stands, and nothing joins again. */
        assert(fa_ios_leave() == FA_IOS_DONE && !radio->ios_joined && !radio->ios_auto_joined);
        { unsigned ups = m8.ups; now += FA_AUTO_WAIT_NS * 2; ios_auto_step(radio); assert(m8.ups == ups); }
        station_down(radio); done();

        /* wifid speaks first: a leave, a join of its own, or the radio off, and the key is wiped unused. */
        for (unsigned kind = 0; kind < 3; kind++) {
            static const uint8_t none[32];
            fresh_wpa(V_PMK); memset(&m8, 0, sizeof(m8)); wpa.stayed = true; radio = radio_of(the_hw); fa_ios_start(&M8_INPUT); m8_alones = 0;
            fa_set_auto_join(name, n, V_PMK);
            assert(fa_m8_up(the_hw) == 0);
            m8_again();
            if (kind == 0) { assert(fa_ios_leave() == FA_IOS_DONE && SAID("M8 auto join: given up, wifid asked for no network")); }
            if (kind == 1) { assert(fa_ios_join(name, n, NULL, V_PMK) == FA_IOS_DONE && SAID("M8 auto join: given up, wifid asked for a network") && m8.ups == 1 && !radio->ios_auto_joined); }
            if (kind == 2) { assert(fa_ios_power(0) == FA_IOS_DONE && SAID("M8 auto join: given up, wifid asked for the radio off")); }
            assert(auto_name_length == 0 && !memcmp(auto_key, none, 32));
            { unsigned ups = m8.ups; now += FA_AUTO_WAIT_NS * 2; ios_auto_step(radio); assert(m8.ups == ups && m8_alones == 0 && !SAID("M8 auto join: name")); }
            if (kind == 1) assert(fa_ios_leave() == FA_IOS_DONE);
            station_down(radio); done();
        }
        /* It speaks between the auto join's turn and the join: the join gives way. */
        fresh_wpa(V_PMK); memset(&m8, 0, sizeof(m8)); wpa.stayed = true; radio = radio_of(the_hw); fa_ios_start(&M8_INPUT); m8_alones = 0;
        assert(fa_m8_up(the_hw) == 0);
        radio->ios_wifid_asked = true;
        assert(ios_join_run(radio, name, n, NULL, V_PMK, true) == FA_IOS_REFUSED && !m8.ups && SAID("M8 auto join: given up, wifid has spoken"));
        station_down(radio); done();
        /* Not heard: the answer is said and passed on, and no second try. */
        fresh_wpa(V_PMK); memset(&m8, 0, sizeof(m8)); wpa.stayed = true; radio = radio_of(the_hw); fa_ios_start(&M8_INPUT); m8_alones = 0;
        fa_set_auto_join(name, n, V_PMK); ap.not_heard = true;
        assert(fa_m8_up(the_hw) == 0);
        assert(fa_ios_power(1) == FA_IOS_DONE); m8_again(); ios_auto_step(radio);
        assert(m8_alones == 1 && m8_alone == FA_IOS_NOT_HEARD && !m8.ups && SAID("M8 auto join: answer 3") && !radio->ios_auto_joined);
        ios_auto_step(radio); assert(m8_alones == 1);
        ap.not_heard = false; station_down(radio); done();
    }

    /* ---- stage 8 ---- */
    {
        static const uint8_t PAYLOAD[46] = "not a real payload";
        static struct fa_ios_state state;
        static uint8_t wrong[32]; memset(wrong, 0x5a, 32);
        static const unsigned short ONLY[1] = { 2412 }, NOWHERE[1] = { 9999 };
        const uint8_t *name = (const uint8_t *)NAME; unsigned n = sizeof(NAME) - 1;
        uint8_t eth[1600]; struct fa_radio *radio;

        /* Before the radio: asked, it waits a minute and says so; nothing passes. */
        fresh_wpa(V_PMK); memset(&m8, 0, sizeof(m8)); wpa.stayed = true; radio = radio_of(the_hw);
        fa_ios_start(&M8_INPUT);
        {
            unsigned long long began = now;
            assert(fa_ios_power(1) == FA_IOS_NO_ANSWER && now - began >= FA_IOS_READY_NS && now - began < FA_IOS_READY_NS + 1000000000ull);
        }
        memset(eth, 0, sizeof(eth)); memcpy(eth, OTHER, 6); memcpy(eth + 6, OWN, 6); eth[12] = 8; eth[13] = 0; memcpy(eth + 14, PAYLOAD, sizeof(PAYLOAD));
        assert(!fa_ios_tx(eth, 60));
        fa_ios_state(&state); assert(state.phase == FA_IOS_IDLE);

        /* Up, and staying. */
        assert(fa_m8_up(the_hw) == 0 && SAID("M8 radio up") && !strcmp(calls, "start,add,") && !m8.ups && !m8.losts);
        assert(fa_ios_power(1) == FA_IOS_DONE && SAID("M8 power 1, firmware up"));

        /* Scans: every network once with its elements; by name; by channel; the list when joined. */
        scans = 0;
        assert(fa_ios_scan(name, 0, ONLY, 0, 0) == FA_IOS_DONE && scans == 1 && radio->ios_table_count == 3 && SAID("M8 scan: 0, 3 networks"));
        {
            const struct fa_heard *got = NULL;
            for (unsigned i = 0; i < radio->ios_table_count; i++) if (!memcmp(radio->ios_table[i].bssid, AP, 6)) got = &radio->ios_table[i];
            assert(got && got->ssid_length == n && !memcmp(got->ssid, NAME, n) && got->frequency == 2437 && got->rssi == -40 && got->interval == 100);
            assert(got->capability == ap.cap && got->elements_length == 2 + n + ap.hear_length && fa_element(got->elements, got->elements_length, FA_EID_RSN));
        }
        { static unsigned each; static unsigned count; count = 0; fa_ios_heard((void (*)(const struct fa_heard *, void *))m8_count, &count); assert(count == 3); }
        assert(fa_ios_scan(name, n, ONLY, 0, 0) == FA_IOS_DONE && radio->ios_table_count == 1 && seen_ssid_length == (int)n && !memcmp(seen_ssid, NAME, n));
        assert(fa_ios_scan(name, 0, ONLY, 1, 1) == FA_IOS_DONE && scan_channels == 1);
        assert(fa_ios_scan(name, 0, NOWHERE, 1, 0) == FA_IOS_DONE && radio->ios_table_count == 0 && scans == 4 - 1);
        assert(fa_ios_scan(name, 0, ONLY, 0, 0) == FA_IOS_DONE);
        log_is_clean();

        /* A join: keys, then the link says so; the same order as stage 6. */
        calls[0] = 0; auth_tries = deauths = 0; eapols = key_calls = 0;
        assert(fa_ios_join(name, n, NULL, V_PMK) == FA_IOS_DONE && m8.ups == 1 && !m8.downs);
        assert(strstr(calls, "add_chanctx,assign,link,sta01,auth,sta12,assoc,sta23,assoc_told,") == calls + strlen("scan,") && strstr(calls, "link,key_p,key_g,sta34,") && SAID("M8 joined 2437 MHz at -40 dBm"));
        assert(strstr(said, "M6 4-way ok") != NULL);
        fa_ios_state(&state);
        assert(state.phase == FA_IOS_JOINED && state.protect && state.bss.ssid_length == n && !memcmp(state.bss.bssid, AP, 6) && fa_element(state.bss.elements, state.bss.elements_length, FA_EID_RSN));
        assert(!target_key[0] && !target_key[31]);

        /* TX: Ethernet to 802.11, ciphered by the firmware. What is not ours to send is dropped. */
        m8_sent_count = 0;
        assert(fa_ios_tx(eth, 60) == 1 && m8_sent_count == 1 && m8_sent_key);
        assert(m8_sent_length == 26 + 8 + 46 && m8_sent[0] == 0x88 && m8_sent[1] == 0x41 && !memcmp(m8_sent + 4, AP, 6) && !memcmp(m8_sent + 10, OWN, 6) && !memcmp(m8_sent + 16, OTHER, 6));
        assert(!memcmp(m8_sent + 26, "\xaa\xaa\x03\x00\x00\x00\x08\x00", 8) && !memcmp(m8_sent + 34, PAYLOAD, 46));
        assert(fa_ios_tx(eth, 1514) == 1 && !fa_ios_tx(eth, 1515) && !fa_ios_tx(eth, 13) && m8_sent_count == 2);
        eth[12] = 0x88; eth[13] = 0x8e; assert(!fa_ios_tx(eth, 60)); eth[12] = 0; eth[13] = 60; assert(!fa_ios_tx(eth, 60)); eth[12] = 8; eth[13] = 0;
        eth[6] ^= 2; assert(!fa_ios_tx(eth, 60)); eth[6] ^= 2;
        radio->ios_said = 0; ios_step(radio); assert(SAID("M8 joined: rx ok 0 dropped unprotected 0"));
        assert(SAID("M8 filtered 0\n") && strstr(said, "M8 since the join") < strstr(said, "M8 filtered 0\n"));
        /* A test's filter on what iOS sends. Off, as here by default, everything goes. On, what it takes says yes to iOS, goes nowhere and is counted, and only that. */
        {
            static const uint8_t ALL[6] = { 0xff, 0xff, 0xff, 0xff, 0xff, 0xff };
            uint8_t t[60]; struct fa_tx_counts before;
            memset(t, 0, sizeof(t)); memcpy(t, OTHER, 6); memcpy(t + 6, OWN, 6); t[12] = 8; t[14] = 0x45; t[17] = 46; t[37] = 53;
            assert(!radio->tx_filtered);
            m8_sent_count = 0; before = radio->tx;
            t[14 + 9] = 6; assert(fa_ios_tx(t, 60) == 1 && m8_sent_count == 1 && radio->tx.tcp == before.tcp + 1);
            t[14 + 9] = 17; assert(fa_ios_tx(t, 60) == 1 && m8_sent_count == 2 && radio->tx.udp == before.udp + 1);
            t[12] = 0x86; t[13] = 0xdd; assert(fa_ios_tx(t, 60) == 1 && m8_sent_count == 3 && radio->tx.ip6 == before.ip6 + 1);
            memcpy(t, ALL, 6); t[12] = 8; t[13] = 0; assert(fa_ios_tx(t, 60) == 1 && m8_sent_count == 4);
            assert(!radio->tx_filtered);
            fa_m8_tx_filter(true, true, true, true); assert(SAID("M8 tx filter: ipv6 1 tcp 1 udp 1 multicast 1"));
            before = radio->tx; m8_sent_count = 0;
            assert(fa_ios_tx(t, 60) == 1 && radio->tx_filtered == 1);				/* a broadcast */
            memcpy(t, OTHER, 6);
            assert(fa_ios_tx(t, 60) == 1 && radio->tx_filtered == 2);				/* UDP to 53 */
            t[14 + 9] = 6; assert(fa_ios_tx(t, 60) == 1 && radio->tx_filtered == 3);		/* TCP */
            t[12] = 0x86; t[13] = 0xdd; assert(fa_ios_tx(t, 60) == 1 && radio->tx_filtered == 4);	/* IPv6 */
            assert(!m8_sent_count && !memcmp(&before, &radio->tx, sizeof(before)));
            /* What was never to go out is still refused, not counted. */
            t[12] = 8; t[13] = 0; t[6] ^= 2; assert(!fa_ios_tx(t, 60)); t[6] ^= 2;
            t[12] = 0x88; t[13] = 0x8e; assert(!fa_ios_tx(t, 60));
            t[12] = 0; t[13] = 60; assert(!fa_ios_tx(t, 60));
            assert(!fa_ios_tx(t, 13) && !fa_ios_tx(t, 1515) && radio->tx_filtered == 4 && !m8_sent_count);
            /* ARP and DHCP pass with everything on, to one and to all; an echo to one passes too. */
            t[12] = 8; t[13] = 6; assert(fa_ios_tx(t, 60) == 1 && m8_sent_count == 1 && radio->tx.arp == before.arp + 1);
            memcpy(t, ALL, 6); assert(fa_ios_tx(t, 60) == 1 && m8_sent_count == 2 && radio->tx.arp == before.arp + 2);
            t[13] = 0; t[14 + 9] = 17; t[35] = 68; t[37] = 67; assert(fa_ios_tx(t, 60) == 1 && m8_sent_count == 3 && radio->tx.udp == before.udp + 1);
            memcpy(t, OTHER, 6); t[35] = 67; t[37] = 68; assert(fa_ios_tx(t, 60) == 1 && m8_sent_count == 4 && radio->tx.udp == before.udp + 2);
            t[14 + 9] = 1; assert(fa_ios_tx(t, 60) == 1 && m8_sent_count == 5 && radio->tx.icmp == before.icmp + 1);
            assert(radio->tx_filtered == 4);
            said[0] = 0; radio->ios_said = 0; ios_step(radio); assert(SAID("M8 filtered 4\n") && SAID("tx arp ") && strstr(said, "M8 since the join") < strstr(said, "M8 filtered 4\n"));
            /* Each bit alone takes what is its own. */
            fa_m8_tx_filter(true, false, false, false); assert(tx_filter == FA_TX_DROP_IP6);
            fa_m8_tx_filter(false, true, false, false); assert(tx_filter == FA_TX_DROP_TCP);
            fa_m8_tx_filter(false, false, true, false); assert(tx_filter == FA_TX_DROP_UDP);
            fa_m8_tx_filter(false, false, false, true); assert(tx_filter == FA_TX_DROP_GROUP);
            fa_m8_tx_filter(false, false, false, false); assert(tx_filter == 0);
            m8_sent_count = 0; t[14 + 9] = 6; assert(fa_ios_tx(t, 60) == 1 && m8_sent_count == 1 && radio->tx_filtered == 4);
            log_is_clean();
        }
        /* Block ack: heard on the poll thread, done on the stage's own, answered in the standard's bytes. */
        {
            static const uint8_t ADDBA[9] = { 3, 0, 0x5a, 0x17, 0x08, 0xe8, 0x03, 0x30, 0x12 }, WANT[9] = { 3, 1, 0x5a, 0, 0, 0x17, 0x08, 0xe8, 0x03 };
            static const uint8_t DELBA[6] = { 3, 2, 0x00, 0x58, 37, 0 }, DELBA_OURS[6] = { 3, 2, 0x00, 0x50, 37, 0 };
            uint8_t b[9];
            calls[0] = 0; ba_calls = 0; action_sent = 0;
            give_action(AP, ADDBA, 9);
            assert(!ba_calls && (radio->ios_pending & IOS_BA) && radio->ba_want == 1u << 5);
            ios_step(radio);
            assert(!strcmp(calls, "ba_start,action,") && ba_last.tid == 5 && ba_last.buf_size == 32 && ba_last.ssn == 0x123 && ba_last.timeout == 1000 && ba_last.amsdu && ba_last.sta == radio->sta);
            assert(SAID("M8 block-ack tid 5: size 32, result 0") && radio->ba_live == 1u << 5 && action_sent == 1 && action_length == 33 && !memcmp(action + 24, WANT, 9));
            /* Again for a live class: it restarts. */
            calls[0] = 0; give_action(AP, ADDBA, 9); ios_step(radio); assert(!strcmp(calls, "ba_stop,ba_start,action,") && radio->ba_live == 1u << 5);
            /* No size says ours; too large is cut to what HT has. */
            memcpy(b, ADDBA, 9); b[3] = 0x03; b[4] = 0; give_action(AP, b, 9); ios_step(radio); assert(ba_last.tid == 0 && ba_last.buf_size == 64 && radio->ba_live == (1u << 5 | 1u));
            assert((action[29] | action[30] << 8) >> 6 == 64);
            memcpy(b, ADDBA, 9); b[3] = 0x07; b[4] = 0xfa; give_action(AP, b, 9); ios_step(radio); assert(ba_last.tid == 1 && ba_last.buf_size == 64);
            /* Refused by the driver or for its policy: status 37, the class not live, frames still pass. */
            calls[0] = 0; ba_fail = -5; memcpy(b, ADDBA, 9); b[3] = 0x0b; b[4] = 0x08; give_action(AP, b, 9); ios_step(radio); ba_fail = 0;
            assert(!strcmp(calls, "ba_start,action,") && action[27] == 37 && !(radio->ba_live & 1u << 2) && SAID("M8 block-ack tid 2: size 32, result -5"));
            calls[0] = 0; memcpy(b, ADDBA, 9); b[3] = 0x0d; b[4] = 0x08; give_action(AP, b, 9); ios_step(radio);
            assert(!strcmp(calls, "action,") && action[27] == 37 && SAID("M8 block-ack tid 3: size 0, result -"));
            assert(fa_ios_tx(eth, 60) == 1);
            /* DELBA: the class stops, nothing is sent. The recipient's own, another's and a broken one move nothing. */
            calls[0] = 0; give_action(AP, DELBA, 6); ios_step(radio); assert(!strcmp(calls, "ba_stop,") && !(radio->ba_live & 1u << 5));
            calls[0] = 0; give_action(AP, DELBA_OURS, 6); give_action(OTHER, ADDBA, 9); give_action(AP, ADDBA, 8);
            assert(!(radio->ios_pending & IOS_BA)); ios_step(radio); assert(!calls[0]);
            /* What the run reads: the counts in the joined line, and a limit on the stale lines. */
            radio->ios_said = 0; fa_rx_stale("reo", 3, 64); fa_rx_msdu_note(false); fa_rx_msdu_note(true); ios_step(radio);
            assert(SAID("rx descriptor with no buffer, reo, ring 3 tp 64, number 1") && SAID("block-ack 4 refused 2 stopped 2") && SAID("stale 1, msdu not done 2\n") && SAID("slots peak 7, msdu done later 1"));
            for (int i = 0; i < 40; i++) fa_rx_stale("wbm", -1, 0);
            { unsigned lines = 0; for (const char *at = said; (at = strstr(at, "rx descriptor with no buffer")) != NULL; at++) lines++; assert(lines == FA_RX_STALE_SAID); }
            /* What is live goes back before the keys and the peer, without a word to the access point. */
            give_action(AP, ADDBA, 9); ios_step(radio); assert(radio->ba_live != 0);
            calls[0] = 0; deauths = 0;
            assert(fa_ios_leave() == FA_IOS_DONE && strstr(calls, "deauth,flush,ba_stop,ba_stop,ba_stop,off_g,off_p,sta43,") && !radio->ba_live && !radio->ba_want);
            m8_again();
            assert(radio->tx_filtered == 4);
            assert(fa_ios_join(name, n, NULL, V_PMK) == FA_IOS_DONE && !radio->ba_live);
            assert(!radio->tx_filtered);	/* the count is since the join */
            /* Loss: no goodbye, and live classes back. */
            give_action(AP, ADDBA, 9); ios_step(radio); assert(radio->ba_live == 1u << 5);
            {
                /* What a run reads at a loss: the door's numbers, what went out, wifid's last sets. No name and no address. */
                static const uint8_t ACTION_RM[] = { 5, 1, 2 };
                uint8_t ip[60] = {0}; ip[0] = 0x45; ip[9] = 17; memcpy(eth + 14, ip, 20); eth[12] = 8; eth[13] = 0;
                give_action(AP, ACTION_RM, 3);
                assert(fa_ios_tx(eth, 60) == 1);
                for (int i = 0; i < 20; i++) fa_ios_note_request(i < 19 ? 383 : 365, i);
                said[0] = 0;
                uint8_t f[64]; unsigned at = answer_frame(f, FA_KIND_ACTION, AP, OTHER);
                f[at] = 3; f[at + 1] = 2; deliver(f, at + 2, 2412, -40, 0, false);	/* a stranger's: moves nothing */
                at = answer_frame(f, FA_KIND_DEAUTH, OTHER, OWN); f[at] = 1; f[at + 1] = 0; deliver(f, at + 2, 2412, -50, 0, false);
                assert(radio->door_other == 1 && !radio->door_seen);
            }
            calls[0] = 0; deauths = 0; m8.losts = 0; m8.downs = 0; give_door(FA_KIND_DEAUTH, 7); ios_step(radio);
            assert(SAID("M8 door: kind 0xc0 reason 7 seq 0 retry 0 protected 0, 26 bytes, rssi -40") && SAID("door others 1") && SAID("M8 the last ") && SAID("udp 1"));
            assert(SAID("by category: 3:1 5:1 ") && SAID("M8 before the loss: wifid set 383 value 4,") && SAID("M8 before the loss: wifid set 365 value 19,") && !SAID("value 3,"));
            assert(SAID("M8 filtered 0\n"));
            assert(!SAID("fuxi-test") && !SAID("d4:") && !SAID("a1:b2"));
            assert(strstr(calls, "ba_stop,off_g,off_p,") && !strstr(calls, "action") && !deauths && m8.losts == 1 && !radio->ba_live);
            m8_again();
            assert(fa_ios_join(name, n, NULL, V_PMK) == FA_IOS_DONE);
            m8.ups = 1; m8.downs = 0; m8.losts = 0; calls[0] = 0;
        }


        /* RX: deciphered data as Ethernet, outside the lock (m8_rx asserts it). Design W3: unciphered is dropped. */
        m8.rx = 0;
        m8_give(false, RX_FLAG_DECRYPTED | RX_FLAG_IV_STRIPPED, false, 0, 0, 0x0800, PAYLOAD, 46);
        assert(m8.rx == 1 && m8.rx_length == 60 && !memcmp(m8.frame, OWN, 6) && !memcmp(m8.frame + 6, OTHER, 6) && m8.frame[12] == 8 && m8.frame[13] == 0 && !memcmp(m8.frame + 14, PAYLOAD, 46));
        m8_give(false, 0, false, 0, 0, 0x0800, PAYLOAD, 46);
        assert(m8.rx == 1 && radio->rx_clear == 1);
        /* Longer than Ethernet carries. */
        { static uint8_t big[1501]; m8_give(false, RX_FLAG_DECRYPTED | RX_FLAG_IV_STRIPPED, false, 0, 0, 0x0800, big, 1501); assert(m8.rx == 1 && radio->rx_long == 1); }
        { static uint8_t big[1500]; m8_give(false, RX_FLAG_DECRYPTED | RX_FLAG_IV_STRIPPED, false, 0, 0, 0x0800, big, 1500); assert(m8.rx == 2 && m8.rx_length == 1514); m8.rx = 1; }

        /* Design W4: frames to everybody, by their number. */
        m8.rx = 0;
        m8_give(true, RX_FLAG_DECRYPTED, true, 1, 1, 0x0806, PAYLOAD, 28);
        assert(m8.rx == 1 && m8.frame[0] == 0xff && m8.frame[12] == 8 && m8.frame[13] == 6);
        m8_give(true, RX_FLAG_DECRYPTED, true, 1, 1, 0x0806, PAYLOAD, 28);
        m8_give(true, RX_FLAG_DECRYPTED, true, 0, 1, 0x0806, PAYLOAD, 28);
        assert(m8.rx == 1 && radio->rx_replay == 2);
        m8_give(true, RX_FLAG_DECRYPTED, true, 2, 3, 0x0806, PAYLOAD, 28);	/* a key we do not have */
        m8_give(true, RX_FLAG_DECRYPTED | RX_FLAG_IV_STRIPPED, false, 0, 0, 0x0806, PAYLOAD, 28);	/* no number */
        assert(m8.rx == 1 && radio->rx_no_key == 2);
        m8_give(true, RX_FLAG_DECRYPTED, true, 0x10000000002ull, 1, 0x0806, PAYLOAD, 28);
        assert(m8.rx == 2 && radio->group_pn.last[1] == 0x10000000002ull);

        /* A message 1 after the handshake moves nothing (W1). */
        give_eapol(V_M1, sizeof(V_M1)); assert(radio->ios_pending == IOS_EAPOL);
        ios_step(radio);
        assert(radio->ios_joined && SAID("M8 handshake message after the keys, 99 bytes: not good: message 1 after the handshake is over") && !memcmp(radio->wpa.pairwise, V_TK, 16));

        /* A new group key under another number: answered ciphered, in, then the old one out. */
        calls[0] = 0; wpa.group_reply = V_G2_REPLY; wpa.group_reply_length = sizeof(V_G2_REPLY);
        give_eapol(V_G2, sizeof(V_G2)); ios_step(radio);
        assert(!strcmp(calls, "key_g,off_g,") && SAID("M8 new group key, number 2: 0") && eapols == 3 && !memcmp(radio->group_key->key, V_GTK2, 16) && radio->group_key->keyidx == 2);
        m8.rx = 0;
        m8_give(true, RX_FLAG_DECRYPTED, true, 5, 2, 0x0806, PAYLOAD, 28); assert(m8.rx == 0 && radio->rx_replay == 3);	/* its counter said 5 */
        m8_give(true, RX_FLAG_DECRYPTED, true, 6, 2, 0x0806, PAYLOAD, 28); assert(m8.rx == 1);
        m8_give(true, RX_FLAG_DECRYPTED, true, 0x20000000000ull, 1, 0x0806, PAYLOAD, 28); assert(m8.rx == 1 && radio->rx_no_key == 3);	/* the old number is gone */
        /* Refused: the old one stays and frames go on (W10). */
        calls[0] = 0; wpa.fail_key = key_calls + 1; wpa.group_reply = V_G3_REPLY; wpa.group_reply_length = sizeof(V_G3_REPLY);
        give_eapol(V_G3, sizeof(V_G3)); ios_step(radio);
        assert(!strcmp(calls, "key_g,") && SAID("M8 new group key, number 1: -5") && radio->group_key->keyidx == 2 && !memcmp(radio->group_key->key, V_GTK2, 16) && !evidence_count && radio->ios_joined);
        m8_give(true, RX_FLAG_DECRYPTED, true, 7, 2, 0x0806, PAYLOAD, 28); assert(m8.rx == 2);
        /* Under the same number: given, and nothing taken back, which would take the new one. */
        calls[0] = 0; wpa.fail_key = 0; wpa.group_reply = V_G4_REPLY; wpa.group_reply_length = sizeof(V_G4_REPLY);
        give_eapol(V_G4, sizeof(V_G4)); ios_step(radio);
        assert(!strcmp(calls, "key_g,") && !memcmp(radio->group_key->key, V_GTK3, 16) && radio->group_slot != 0);
        log_is_clean();

        /* A scan while joined goes nowhere near the air. */
        scans = 0; assert(fa_ios_scan(name, 0, ONLY, 0, 0) == FA_IOS_DONE && !scans && SAID("M8 scan while joined: none"));

        /* Left when asked: the goodbye, the keys back, nothing of them left. Then again. */
        calls[0] = 0; deauths = 0;
        assert(fa_ios_leave() == FA_IOS_DONE && m8.downs == 1 && deauths == 1 && strstr(calls, "deauth,flush,off_g,off_p,sta43,sta32,sta21,sta10,disassoc_told,unassign,remove_chanctx,"));
        fa_ios_state(&state); assert(state.phase == FA_IOS_IDLE && !radio->ios_joined && !radio->joining && !radio->wpa.kck[0]);
        assert(!fa_ios_tx(eth, 60));
        /* The filter's yes is for a joined station alone. */
        eth[12] = 0x86; eth[13] = 0xdd; fa_m8_tx_filter(true, false, false, false); assert(!fa_ios_tx(eth, 60) && !radio->tx_filtered); fa_m8_tx_filter(false, false, false, false); eth[12] = 8; eth[13] = 0;
        assert(radio->vif != NULL && radio->started);
        { void *memory = radio->sta_memory;
          m8_again();
          assert(fa_ios_join(name, n, NULL, V_PMK) == FA_IOS_DONE && m8.ups == 2 && radio->sta_memory == memory && radio->group_slot == 1 && SAID("M8 joined")); }
        assert(fa_ios_tx(eth, 60) == 1);

        /* Lost: the door. Told after the snapshot says so; no goodbye; and it can be joined again. */
        calls[0] = 0; deauths = 0; m8.losts = 0; m8.downs = 0;
        give_door(FA_KIND_DEAUTH, 7); assert(radio->ios_pending & IOS_LOSS);
        ios_step(radio);
        fa_ios_state(&state);
        assert(m8.losts == 1 && m8.lost == FA_IOS_GONE && m8.downs == 1 && state.phase == FA_IOS_IDLE && state.last_answer == FA_IOS_GONE && !deauths && !radio->ios_joined && radio->chanctx == NULL);
        m8_again(); assert(fa_ios_join(name, n, NULL, V_PMK) == FA_IOS_DONE && m8.ups == 3);
        /* Lost: the driver's word, and a beacon that no longer asks for a key. */
        m8.losts = 0; linuxkpi_ieee80211_connection_loss(radio->vif); ios_step(radio); assert(m8.losts == 1 && !radio->ios_joined);
        m8_again(); assert(fa_ios_join(name, n, NULL, V_PMK) == FA_IOS_DONE);
        m8.losts = 0; hear(FA_KIND_BEACON, AP, NAME, FA_CAP_ESS, OPEN_HT, sizeof(OPEN_HT), 2437, -41); ios_step(radio); assert(m8.losts == 1 && !radio->ios_joined && radio->went_protected);

        /* Power off leaves and keeps the interface; on again gives an idle radio. Nothing happens to the firmware. */
        m8_again(); assert(fa_ios_join(name, n, NULL, V_PMK) == FA_IOS_DONE);
        m8.downs = 0; assert(fa_ios_power(0) == FA_IOS_DONE && m8.downs == 1 && radio->vif != NULL && radio->started);
        fa_ios_state(&state); assert(state.phase == FA_IOS_OFF);
        assert(fa_ios_power(1) == FA_IOS_DONE); fa_ios_state(&state); assert(state.phase == FA_IOS_IDLE);

        /* The answers wifid reads. */
        m8_again(); wpa.any_reply = true;
        {
            unsigned long long began = now;
            assert(fa_ios_join(name, n, NULL, wrong) == FA_IOS_NO_AUTH && now - began < FA_HANDSHAKE_NS + 3000000000ull && !radio->ios_joined);
            fa_ios_state(&state); assert(state.phase == FA_IOS_IDLE && state.last_answer == FA_IOS_NO_AUTH);
        }
        m8_again(); wpa.silent = true; assert(fa_ios_join(name, n, NULL, V_PMK) == FA_IOS_NO_ANSWER);
        m8_again(); ap.not_heard = true; assert(fa_ios_join(name, n, NULL, V_PMK) == FA_IOS_NOT_HEARD); ap.not_heard = false;
        m8_again(); assert(fa_ios_join(name, n, OTHER, V_PMK) == FA_IOS_NOT_HEARD);
        m8_again(); assert(fa_ios_join(name, 0, NULL, V_PMK) == FA_IOS_NOT_HEARD);
        m8_again(); ap.auth_status = 13; assert(fa_ios_join(name, n, NULL, V_PMK) == FA_IOS_REFUSED); ap.auth_status = 0;
        m8_again(); assert(fa_ios_join(name, n, AP, V_PMK) == FA_IOS_DONE); assert(fa_ios_leave() == FA_IOS_DONE);
        /* What it asks for and what it is must agree: a key for an open network, none for a protected one, WPA3 alone. */
        m8_again(); assert(fa_ios_join(name, n, NULL, NULL) == FA_IOS_NOT_BUILT && !radio->ios_joined);
        { const uint8_t *e = ap.hear_elements; unsigned l = ap.hear_length, c = ap.cap;
          ap.hear_elements = OPEN_HT; ap.hear_length = sizeof(OPEN_HT); ap.cap = FA_CAP_ESS;
          m8_again(); assert(fa_ios_join(name, n, NULL, V_PMK) == FA_IOS_NOT_BUILT);
          /* Open, and joined as such: not ciphered either way. */
          m8_again(); m8.ups = 0; assert(fa_ios_join(name, n, NULL, NULL) == FA_IOS_DONE && m8.ups == 1 && !strstr(calls + strlen(calls), "key_"));
          m8_sent_count = 0; assert(fa_ios_tx(eth, 60) == 1 && m8_sent[1] == 0x01 && !m8_sent_key);
          m8.rx = 0; m8_give(false, 0, false, 0, 0, 0x0800, PAYLOAD, 46); assert(m8.rx == 1);
          m8_give(true, 0, false, 0, 0, 0x0806, PAYLOAD, 28); assert(m8.rx == 2);
          assert(fa_ios_leave() == FA_IOS_DONE);
          ap.hear_elements = e; ap.hear_length = l; ap.cap = c; }
        { static uint8_t third[sizeof(RSN_ELEMENT)];
          const uint8_t *e = ap.hear_elements; unsigned l = ap.hear_length;
          memcpy(third, RSN_ELEMENT, sizeof(third)); third[19] = 8; ap.hear_elements = third; ap.hear_length = sizeof(third);
          m8_again(); assert(fa_ios_join(name, n, NULL, V_PMK) == FA_IOS_NOT_BUILT && SAID("M6 refused: it does not take a WPA2 passphrase's key"));
          ap.hear_elements = e; ap.hear_length = l; }
        log_is_clean();

        /* The firmware goes: once, its account follows, and from then on every answer says so. */
        m8_again(); assert(fa_ios_join(name, n, NULL, V_PMK) == FA_IOS_DONE);
        calls[0] = 0; m8.losts = 0; m8.downs = 0; fa_m4_evidence("died"); fa_m8_firmware_gone(); ios_step(radio);
        assert(m8.losts == 1 && m8.lost == FA_IOS_RADIO_DEAD && m8.downs == 1 && rddm && radio->ios_dead && !radio->ios_joined);
        assert(strstr(calls, "rddm,") != NULL && strstr(calls, "rddm,") < strstr(calls, "sta43,"));
        assert(fa_ios_scan(name, 0, ONLY, 0, 0) == FA_IOS_RADIO_DEAD && fa_ios_join(name, n, NULL, V_PMK) == FA_IOS_RADIO_DEAD && !fa_ios_tx(eth, 60));
        assert(fa_ios_power(1) == FA_IOS_RADIO_DEAD);
        fa_ios_state(&state); assert(state.phase == FA_IOS_OFF && state.last_answer == FA_IOS_RADIO_DEAD);
        fa_m8_firmware_gone(); ios_step(radio); assert(m8.losts == 1);
        log_is_clean();
        /* The rest of the way down, which no phone takes: only so that the sanitizer sees nothing left. */
        station_down(radio);
        done();
    }

    /* The two iterators: only what exists. */
    fresh();
    {
        struct fa_radio *radio = radio_of(the_hw);
        assert(radio->sta == NULL && radio->chanctx == NULL);
        linuxkpi_ieee80211_iterate_stations_atomic(the_hw, (void *)abort, NULL);
        linuxkpi_ieee80211_iterate_chan_contexts(the_hw, (void *)abort, NULL);
        assert(linuxkpi_ieee80211_find_sta(NULL, AP) == NULL);
    }
    done();
    assert(!locks);
    puts("fa_80211: stage 5 order, frames, 5 GHz, refusals, bounds, the stay's counts, the firmware's account before the way down, log holds no name");
    return 0;
}
"""


FA_JOIN_HOST = r"""
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "fa_bss.h"
#include "fa_join.h"

static uint32_t seed = 0x2468ace1;
static uint32_t next(void) { seed ^= seed << 13; seed ^= seed >> 17; seed ^= seed << 5; return seed; }
static const uint8_t AP[6] = {0x02, 0xa1, 0xb2, 0xc3, 0xd4, 0x55}, OWN[6] = {2, 0x46, 0x55, 0x58, 0x49, 1}, OTHER[6] = {0x02, 0, 0, 0, 0, 9};

/* A frame in a piece of memory of exactly its size, so the sanitizer sees a read past its end. */
static uint8_t *exactly(const uint8_t *frame, unsigned length) { uint8_t *e = malloc(length ? length : 1); memcpy(e, frame, length); return e; }
static enum fa_target_result take(struct fa_target *t, const uint8_t *f, unsigned n, const char *name) {
    uint8_t *e = exactly(f, n);
    enum fa_target_result r = fa_target_take(t, e, n, 2412, -50, (const uint8_t *)name, (unsigned)strlen(name));
    free(e);
    return r;
}
static unsigned beacon(uint8_t *f, uint8_t kind, const uint8_t *bssid, unsigned cap, const void *elements, unsigned length) {
    memset(f, 0, 36); f[0] = kind; memcpy(f + 10, bssid, 6); memcpy(f + 16, bssid, 6);
    f[32] = 100; f[34] = (uint8_t)cap; f[35] = (uint8_t)(cap >> 8);
    memcpy(f + 36, elements, length);
    return 36 + length;
}

static void protection(void) {
    static const uint8_t none[] = {0, 1, 'a', 1, 1, 0x82};
    static const uint8_t rsn[] = {48, 2, 1, 0};
    static const uint8_t wpa[] = {221, 4, 0x00, 0x50, 0xf2, 1};
    static const uint8_t wmm[] = {221, 4, 0x00, 0x50, 0xf2, 2};
    static const uint8_t wapi[] = {68, 1, 1};
    static const uint8_t lie[] = {0, 1, 'a', 48, 200, 0};
    static const uint8_t cut[] = {0, 1, 'a', 3};
    assert(fa_protection(1, none, sizeof(none)) == FA_OPEN);
    assert(fa_protection(0x11, none, sizeof(none)) == FA_PROTECT_PRIVACY);
    assert(fa_protection(1, rsn, sizeof(rsn)) == FA_PROTECT_RSN);
    assert(fa_protection(1, wpa, sizeof(wpa)) == FA_PROTECT_WPA);
    assert(fa_protection(1, wmm, sizeof(wmm)) == FA_OPEN);
    assert(fa_protection(1, wapi, sizeof(wapi)) == FA_PROTECT_WAPI);
    assert(fa_protection(1, lie, sizeof(lie)) == FA_PROTECT_UNREADABLE);
    assert(fa_protection(1, cut, sizeof(cut)) == FA_PROTECT_UNREADABLE);
    assert(fa_protection(1, none, 0) == FA_OPEN);
    assert(fa_elements_whole(none, sizeof(none)) && !fa_elements_whole(none, sizeof(none) - 1) && fa_elements_whole(none, 3));
    /* A short vendor element does not read as WPA, nor past its end. */
    static const uint8_t brief[] = {221, 3, 0x00, 0x50, 0xf2};
    uint8_t *e = exactly(brief, sizeof(brief));
    assert(fa_vendor_element(e, sizeof(brief), 0, 0x50, 0xf2, 1) == NULL && fa_protection(1, e, sizeof(brief)) == FA_OPEN);
    free(e);
    static const uint8_t emptyext[] = {255, 0, 255, 1, 35};
    e = exactly(emptyext, sizeof(emptyext));
    assert(fa_ext_element(e, sizeof(emptyext), 35) == e + 2);
    free(e);
    puts("protection: each kind, what cannot be read counts as one");
}

static void target(void) {
    static struct fa_target t;
    uint8_t f[2048];
    static const uint8_t home[] = {0, 4, 'h', 'o', 'm', 'e', 3, 1, 6};
    static const uint8_t home_rsn[] = {0, 4, 'h', 'o', 'm', 'e', 48, 2, 1, 0};
    static const uint8_t hidden[] = {0, 0, 3, 1, 6};
    unsigned n = beacon(f, FA_KIND_BEACON, AP, 1, home, sizeof(home));
    /* Every cut of a good frame: never read past its end, and only the whole frame counts as open: a cut one cannot be told from a lie. */
    for (unsigned cut = 0; cut <= n; cut++) {
        memset(&t, 0, sizeof(t));
        enum fa_target_result r = take(&t, f, cut, "home");
        assert(t.found == (r == FA_TARGET_TAKEN));
        if (cut < 2) assert(r == FA_TARGET_OTHER);
        if (cut >= 2 && cut < 36) assert(r == FA_TARGET_MALFORMED);
        if (cut == n) assert(r == FA_TARGET_TAKEN && t.elements_length == sizeof(home) && !memcmp(t.elements, home, sizeof(home)));
    }
    /* What it keeps. */
    memset(&t, 0, sizeof(t));
    assert(take(&t, f, n, "home") == FA_TARGET_TAKEN);
    assert(t.found && !memcmp(t.address, AP, 6) && t.frequency == 2412 && t.interval == 100 && t.capability == 1 && t.rssi == -50 && t.heard == 1);
    /* Another name, longer and shorter. */
    assert(take(&t, f, n, "hom") == FA_TARGET_ELSEWHERE && take(&t, f, n, "homer") == FA_TARGET_ELSEWHERE && take(&t, f, n, "HOME") == FA_TARGET_ELSEWHERE);
    /* Not a beacon or a response. */
    for (unsigned kind = 0; kind < 256; kind++) {
        if (kind != FA_KIND_BEACON && kind != FA_KIND_PROBE_RESPONSE) {
            f[0] = (uint8_t)kind; assert(take(&t, f, n, "home") == FA_TARGET_OTHER);
        }
    }
    f[0] = FA_KIND_PROBE_RESPONSE; assert(take(&t, f, n, "home") == FA_TARGET_TAKEN); f[0] = FA_KIND_BEACON;
    /* Protected frame, group or empty address, no frequency. */
    f[1] = 0x40; assert(take(&t, f, n, "home") == FA_TARGET_MALFORMED); f[1] = 0;
    f[16] |= 1; assert(take(&t, f, n, "home") == FA_TARGET_MALFORMED); f[16] &= 0xfe;
    assert(fa_target_take(&t, f, n, 0, -50, (const uint8_t *)"home", 4) == FA_TARGET_MALFORMED);
    assert(fa_target_take(&t, f, n, 0x10000, -50, (const uint8_t *)"home", 4) == FA_TARGET_MALFORMED);
    uint8_t zero[64]; unsigned z = beacon(zero, FA_KIND_BEACON, (uint8_t[6]){0}, 1, home, sizeof(home));
    assert(take(&t, zero, z, "home") == FA_TARGET_MALFORMED);
    /* The strongest hearing wins, across access points; a weaker one changes nothing but the count. */
    memset(&t, 0, sizeof(t));
    assert(fa_target_take(&t, f, n, 2412, -60, (const uint8_t *)"home", 4) == FA_TARGET_TAKEN);
    uint8_t g[128]; unsigned m = beacon(g, FA_KIND_BEACON, OTHER, 1, home, sizeof(home));
    assert(fa_target_take(&t, g, m, 2437, -40, (const uint8_t *)"home", 4) == FA_TARGET_TAKEN && !memcmp(t.address, OTHER, 6) && t.frequency == 2437 && t.rssi == -40);
    assert(fa_target_take(&t, f, n, 2412, -70, (const uint8_t *)"home", 4) == FA_TARGET_TAKEN && !memcmp(t.address, OTHER, 6) && t.heard == 3);
    assert(fa_target_take(&t, f, n, 2412, 1000, (const uint8_t *)"home", 4) == FA_TARGET_TAKEN && t.rssi == 127);
    assert(fa_target_take(&t, f, n, 2412, -1000, (const uint8_t *)"home", 4) == FA_TARGET_TAKEN && t.rssi == 127);
    /* Protection sticks, and is of this name only; a neighbour's says nothing. The first kind stays. */
    memset(&t, 0, sizeof(t));
    m = beacon(g, FA_KIND_BEACON, AP, 0x11, home_rsn, sizeof(home_rsn));
    assert(take(&t, g, m, "other") == FA_TARGET_ELSEWHERE && t.protected_frames == 0);
    assert(take(&t, g, m, "home") == FA_TARGET_PROTECTED && t.protected_frames == 1 && t.why == FA_PROTECT_RSN && !t.found);
    assert(take(&t, f, n, "home") == FA_TARGET_TAKEN && t.protected_frames == 1);
    m = beacon(g, FA_KIND_BEACON, AP, 0x11, home, sizeof(home));
    assert(take(&t, g, m, "home") == FA_TARGET_PROTECTED && t.protected_frames == 2 && t.why == FA_PROTECT_RSN);
    /* A hidden beacon is not of this name. */
    memset(&t, 0, sizeof(t));
    m = beacon(g, FA_KIND_BEACON, AP, 1, hidden, sizeof(hidden));
    assert(take(&t, g, m, "home") == FA_TARGET_ELSEWHERE && !t.found);
    /* Fixed: the chosen access point's frames only, its protection whatever name they carry, nothing else moves. */
    memset(&t, 0, sizeof(t));
    assert(take(&t, f, n, "home") == FA_TARGET_TAKEN);
    t.fixed = true;
    unsigned before = t.elements_length;
    assert(take(&t, g, m, "home") == FA_TARGET_TAKEN && t.elements_length == before && t.heard == 1);
    unsigned k = beacon(g, FA_KIND_BEACON, OTHER, 1, home_rsn, sizeof(home_rsn));
    assert(take(&t, g, k, "home") == FA_TARGET_ELSEWHERE && t.protected_frames == 0);
    k = beacon(g, FA_KIND_BEACON, AP, 1, home_rsn, sizeof(home_rsn));
    assert(take(&t, g, k, "home") == FA_TARGET_PROTECTED && t.protected_frames == 1);
    /* Elements longer than the room are cut at a whole element. */
    memset(&t, 0, sizeof(t));
    static uint8_t many[FA_TARGET_ELEMENTS_MOST + 300];
    unsigned at = 0;
    memcpy(many, home, sizeof(home)); at = sizeof(home);
    while (at + 202 <= sizeof(many)) { many[at] = 200; many[at + 1] = 200; at += 202; }
    k = beacon(f, FA_KIND_BEACON, AP, 1, many, at);
    assert(take(&t, f, k, "home") == FA_TARGET_TAKEN && t.elements_length <= FA_TARGET_ELEMENTS_MOST && fa_elements_whole(t.elements, t.elements_length));
    assert(t.elements_length > FA_TARGET_ELEMENTS_MOST - 202);
    puts("target: the name exactly, the strongest, protection sticks, fixed, bounded");
}

static void reply(void) {
    struct fa_reply r;
    uint8_t f[128];
    static const uint8_t el[] = {1, 1, 0x82};
    memset(f, 0, sizeof(f)); memcpy(f + 4, OWN, 6); memcpy(f + 10, AP, 6); memcpy(f + 16, AP, 6);
#define READ(n) do { uint8_t *e = exactly(f, n); fa_reply_read(&r, e, n, OWN, AP); free(e); } while (0)
    /* Authentication: algorithm, step, status. */
    f[0] = FA_KIND_AUTH; f[26] = 2;
    READ(30); assert(r.answer == FA_ANSWER_AUTH && r.code == 0);
    for (unsigned cut = 0; cut < 30; cut++) { READ(cut); assert(r.answer == FA_ANSWER_NONE); }
    f[28] = 13; READ(30); assert(r.answer == FA_ANSWER_REFUSED && r.code == 13); f[28] = 0;
    f[26] = 1; READ(30); assert(r.answer == FA_ANSWER_NONE); f[26] = 4; READ(30); assert(r.answer == FA_ANSWER_NONE); f[26] = 2;
    f[24] = 1; READ(30); assert(r.answer == FA_ANSWER_NONE); f[24] = 0;
    /* Both addresses, and the BSSID field. */
    f[4] ^= 1; READ(30); assert(r.answer == FA_ANSWER_NONE); f[4] ^= 1;
    f[10] ^= 1; READ(30); assert(r.answer == FA_ANSWER_NONE); f[10] ^= 1;
    f[16] ^= 1; READ(30); assert(r.answer == FA_ANSWER_NONE); f[16] ^= 1;
    /* Protected, or not a management frame of version 0. */
    f[1] = 0x40; READ(30); assert(r.answer == FA_ANSWER_NONE); f[1] = 0;
    f[0] = FA_KIND_AUTH | 1; READ(30); assert(r.answer == FA_ANSWER_NONE);
    f[0] = FA_KIND_AUTH | 8; READ(30); assert(r.answer == FA_ANSWER_NONE); f[0] = FA_KIND_AUTH;
    /* +HTC moves the fixed fields. */
    f[1] = 0x80; memset(f + 24, 0xee, 4); memset(f + 28, 0, 6); f[30] = 2;
    READ(34); assert(r.answer == FA_ANSWER_AUTH); READ(33); assert(r.answer == FA_ANSWER_NONE);
    f[1] = 0;
    /* Association response: capabilities, status, number, elements. */
    memset(f + 24, 0, 40); f[0] = FA_KIND_ASSOC_RESPONSE; f[28] = 7; f[29] = 0xc0; memcpy(f + 30, el, sizeof(el));
    READ(30 + sizeof(el)); assert(r.answer == FA_ANSWER_ASSOC && r.aid == 7 && r.elements_length == sizeof(el));
    READ(30); assert(r.answer == FA_ANSWER_ASSOC && r.elements_length == 0);
    for (unsigned cut = 0; cut < 30; cut++) { READ(cut); assert(r.answer == FA_ANSWER_NONE); }
    f[28] = 0; f[29] = 0xc0; READ(30); assert(r.answer == FA_ANSWER_NONE && r.aid == 0);
    f[28] = 0xd8; f[29] = 0xc7; READ(30); assert(r.answer == FA_ANSWER_NONE); /* 2008 */
    f[28] = 0xd7; f[29] = 0xc7; READ(30); assert(r.answer == FA_ANSWER_ASSOC && r.aid == 2007);
    f[26] = 17; READ(30); assert(r.answer == FA_ANSWER_REFUSED && r.code == 17 && r.aid == 0); f[26] = 0;
    /* Deauthentication, disassociation: a reason. */
    memset(f + 24, 0, 40);
    f[0] = FA_KIND_DEAUTH; f[24] = 8; READ(26); assert(r.answer == FA_ANSWER_GONE && r.code == 8); READ(25); assert(r.answer == FA_ANSWER_NONE);
    f[0] = FA_KIND_DISASSOC; READ(26); assert(r.answer == FA_ANSWER_GONE);
    /* The rest of the kinds say nothing. */
    for (unsigned kind = 0; kind < 256; kind++) {
        if (kind != FA_KIND_AUTH && kind != FA_KIND_ASSOC_RESPONSE && kind != FA_KIND_DEAUTH && kind != FA_KIND_DISASSOC) {
            f[0] = (uint8_t)kind; READ(64); assert(r.answer == FA_ANSWER_NONE);
        }
    }
    puts("replies: every cut, both addresses, association number range, protected refused");
}

static void builders(void) {
    uint8_t f[768];
    assert(fa_auth_request(f, FA_AUTH_LENGTH - 1, OWN, AP) == 0 && fa_auth_request(f, FA_AUTH_LENGTH, OWN, AP) == FA_AUTH_LENGTH);
    static const uint8_t auth[30] = {0xb0, 0, 0, 0, 2, 0xa1, 0xb2, 0xc3, 0xd4, 0x55, 2, 0x46, 0x55, 0x58, 0x49, 1, 2, 0xa1, 0xb2, 0xc3, 0xd4, 0x55, 0, 0, 0, 0, 1, 0, 0, 0};
    assert(!memcmp(f, auth, 30));
    assert(fa_deauth(f, FA_DEAUTH_LENGTH - 1, OWN, AP, 3) == 0 && fa_deauth(f, FA_DEAUTH_LENGTH, OWN, AP, 0x0103) == FA_DEAUTH_LENGTH);
    assert(f[0] == 0xc0 && f[24] == 3 && f[25] == 1 && !memcmp(f + 16, AP, 6));

    uint8_t he[FA_HE_OFFER_MOST], mac[6], phy[11];
    memset(mac, 0x11, 6); memset(phy, 0xff, 11);
    assert(fa_he_offer(he, sizeof(he) - 1, mac, phy, 0xfffa, 0xfff5) == 0);
    assert(fa_he_offer(he, sizeof(he), mac, phy, 0xfffa, 0xfff5) == FA_HE_OFFER_MOST);
    assert(he[0] == 255 && he[1] == FA_HE_OFFER_MOST - 2 && he[2] == 35 && he[3] == 0x11);
    assert(he[9] == 0x01 && he[15] == 0x7f && he[20] == 0xfa && he[21] == 0xff && he[22] == 0xf5);
    struct fa_he read;
    assert(fa_he_read(he, &read) && read.rx_mcs_80 == 0xfffa && read.tx_mcs_80 == 0xfff5 && read.phy[0] == 1);

    struct fa_offer o;
    memset(&o, 0, sizeof(o));
    o.capability = FA_CAP_ESS; o.ssid = (const uint8_t *)"home"; o.ssid_length = 4;
    for (unsigned i = 0; i < 12; i++) o.rates[o.rate_count++] = (uint8_t)(2 * (i + 1));
    unsigned n = fa_assoc_request(f, sizeof(f), OWN, AP, &o);
    assert(n == 24 + 4 + 2 + 4 + 2 + 8 + 2 + 4 && f[0] == 0 && f[28] == 0 && f[29] == 4 && f[26] == FA_LISTEN_INTERVAL);
    assert(fa_elements_whole(f + 28, n - 28) && fa_element(f + 28, n - 28, 50) != NULL);
    /* Everything at once, its largest. */
    o.ssid_length = 32; o.ssid = (const uint8_t *)"0123456789012345678901234567890123"; o.rate_count = FA_RATES_MOST;
    o.spectrum = true; o.has_ht = true; o.has_vht = true; o.wmm = true; memcpy(o.he, he, sizeof(he)); o.he_length = sizeof(he);
    n = fa_assoc_request(f, sizeof(f), OWN, AP, &o);
    assert(n != 0 && fa_elements_whole(f + 28, n - 28));
    assert(fa_element(f + 28, n - 28, FA_EID_RSN) == NULL && fa_ext_element(f + 28, n - 28, 108) == NULL);
    assert(fa_ext_element(f + 28, n - 28, FA_EXT_HE) != NULL && fa_wmm_element(f + 28, n - 28) != NULL);
    /* Vendor last. */
    assert(fa_wmm_element(f + 28, n - 28) + 9 == f + n);
    /* Too little room, each size: nothing written past it, and an answer of 0. */
    for (unsigned size = 0; size < n; size++) {
        uint8_t *e = malloc(size ? size : 1);
        assert(fa_assoc_request(e, size, OWN, AP, &o) == 0);
        free(e);
    }
    /* Nothing a station may ask with. */
    o.ssid_length = 33; assert(fa_assoc_request(f, sizeof(f), OWN, AP, &o) == 0);
    o.ssid_length = 0; assert(fa_assoc_request(f, sizeof(f), OWN, AP, &o) == 0);
    o.ssid_length = 4; o.rate_count = 0; assert(fa_assoc_request(f, sizeof(f), OWN, AP, &o) == 0);
    o.rate_count = FA_RATES_MOST + 1; assert(fa_assoc_request(f, sizeof(f), OWN, AP, &o) == 0);
    puts("frames we send: exact, bounded, no protection, no EHT");
}

static void he_and_streams(void) {
    uint8_t e[64];
    struct fa_he he;
    memset(e, 0, sizeof(e));
    e[0] = 255; e[2] = 35;
    /* 80 MHz only, the least it can be. Shorter is refused. */
    e[1] = 1 + 6 + 11 + 4; e[20] = 0xfa; e[21] = 0xff; e[22] = 0xf5; e[23] = 0xff;
    assert(fa_he_read(e, &he) && he.rx_mcs_80 == 0xfffa && he.tx_mcs_80 == 0xfff5 && he.rx_mcs_160 == 0);
    for (unsigned body = 1; body < 22u; body++) { e[1] = (uint8_t)body; assert(!fa_he_read(e, &he)); }
    /* 160 and 80+80 each take four more. */
    e[9] = FA_HE_PHY0_160; e[1] = 1 + 6 + 11 + 4; assert(!fa_he_read(e, &he));
    e[1] = 1 + 6 + 11 + 8; e[24] = 0xaa; assert(fa_he_read(e, &he) && he.rx_mcs_160 == 0x00aa);
    e[9] |= FA_HE_PHY0_80_80; assert(!fa_he_read(e, &he));
    e[1] = 1 + 6 + 11 + 12; e[28] = 0xbb; assert(fa_he_read(e, &he) && he.rx_mcs_80_80 == 0x00bb);
    /* PPE thresholds: said to be there, and there. More than the room is cut. */
    e[9] = 0; e[15] = FA_HE_PHY6_PPE; e[1] = 1 + 6 + 11 + 4; assert(!fa_he_read(e, &he));
    e[1] = 1 + 6 + 11 + 4 + 2; e[24] = 0x77; assert(fa_he_read(e, &he) && he.ppe[0] == 0x77);
    e[1] = 1 + 6 + 11 + 4 + 40; assert(fa_he_read(e, &he));
    /* Every real length of an element, in memory of exactly its size. */
    for (unsigned body = 1; body < 60; body++) {
        uint8_t *x = malloc(2 + body); memset(x, (int)next(), 2 + body); x[0] = 255; x[1] = (uint8_t)body; x[2] = 35;
        (void)fa_he_read(x, &he);
        free(x);
    }
    assert(fa_mcs_map_streams(0xffff) == 0 && fa_mcs_map_streams(0xfffc) == 1 && fa_mcs_map_streams(0xfffa) == 2 && fa_mcs_map_streams(0xaaaa) == 8);
    uint8_t ht[28] = {45, 26}, vht[14] = {191, 12};
    assert(fa_peer_streams(NULL, NULL, NULL) == 1);
    ht[5] = 0xff; assert(fa_peer_streams(ht, NULL, NULL) == 1);
    ht[6] = 0xff; assert(fa_peer_streams(ht, NULL, NULL) == 2);
    ht[1] = 25; assert(fa_peer_streams(ht, NULL, NULL) == 1); ht[1] = 26;
    vht[6] = 0xea; vht[7] = 0xff; assert(fa_peer_streams(ht, vht, NULL) == 3);
    he.rx_mcs_80 = 0xff00; assert(fa_peer_streams(ht, vht, &he) == 4);
    he.rx_mcs_80 = 0xffff; assert(fa_peer_streams(NULL, NULL, &he) == 1);
    static const uint16_t bits[] = {60, 90, 120, 180};
    static const uint8_t rates[] = {1, 2, 0x8c, 18, 50, 2, 0x98, 200, 7, 0};
    uint32_t basic;
    assert(fa_rates_mask(bits, 4, rates, sizeof(rates), &basic) == (1u | 2u | 4u) && basic == (1u | 4u));
    assert(fa_rates_mask(bits, 4, rates, 0, &basic) == 0 && basic == 0);
    puts("HE, streams, rates: every length");
}

static void kinds(void) {
    uint8_t f[40] = {0};
    memcpy(f + 10, AP, 6); memcpy(f + 16, AP, 6);
    /* Block ack: the requests of the access point, and our answer, by the standard's bytes. */
    {
        uint8_t f[80] = {0}; struct fa_ba_ask ask;
        static const uint8_t ADDBA[9] = { 3, 0, 0x5a, 0x17, 0x08, 0xe8, 0x03, 0x30, 0x12 };	/* A-MSDU, immediate, tid 5, 32; 1000 TU; SSN 0x123 */
        f[0] = FA_KIND_ACTION; memcpy(f + 4, OWN, 6); memcpy(f + 10, AP, 6); memcpy(f + 16, AP, 6); memcpy(f + 24, ADDBA, 9);
        assert(fa_ba_read(f, 33, OWN, AP, &ask) && ask.start && ask.amsdu && ask.immediate && ask.token == 0x5a && ask.tid == 5 && ask.size == 32 && ask.timeout == 1000 && ask.ssn == 0x123);
        for (unsigned i = 0; i < 33; i++) {
            uint8_t *exact = malloc(i ? i : 1); memcpy(exact, f, i);
            assert(!fa_ba_read(exact, i, OWN, AP, &ask));
            free(exact);
        }
        assert(!fa_ba_read(f, 33, OTHER, AP, &ask) && !fa_ba_read(f, 33, OWN, OTHER, &ask));
        f[1] = 0x40; assert(!fa_ba_read(f, 33, OWN, AP, &ask)); f[1] = 0;
        memcpy(f + 16, OTHER, 6); assert(!fa_ba_read(f, 33, OWN, AP, &ask)); memcpy(f + 16, AP, 6);
        f[0] = FA_KIND_AUTH; assert(!fa_ba_read(f, 33, OWN, AP, &ask)); f[0] = FA_KIND_ACTION;
        f[24] = 4; assert(!fa_ba_read(f, 33, OWN, AP, &ask)); f[24] = 3;
        f[25] = 1; assert(!fa_ba_read(f, 33, OWN, AP, &ask)); f[25] = 7; assert(!fa_ba_read(f, 33, OWN, AP, &ask)); f[25] = 0;
        f[27] = (uint8_t)(f[27] | 0x20); assert(!fa_ba_read(f, 33, OWN, AP, &ask)); f[27] &= (uint8_t)~0x20;	/* traffic class 13 */
        /* With an HT control field in the header. */
        memmove(f + 28, f + 24, 9); memset(f + 24, 0, 4); f[1] = 0x80;
        assert(fa_ba_read(f, 37, OWN, AP, &ask) && ask.tid == 5); assert(!fa_ba_read(f, 36, OWN, AP, &ask));
        memmove(f + 24, f + 28, 9); f[1] = 0;
        /* DELBA: the originator's stops the class; the recipient's is about what we send. */
        { static const uint8_t D[6] = { 3, 2, 0x00, 0x58, 37, 0 }; memcpy(f + 24, D, 6);
          assert(fa_ba_read(f, 30, OWN, AP, &ask) && !ask.start && ask.tid == 5); assert(!fa_ba_read(f, 29, OWN, AP, &ask));
          f[27] = 0x50; assert(!fa_ba_read(f, 30, OWN, AP, &ask)); f[27] = 0xd8; assert(!fa_ba_read(f, 30, OWN, AP, &ask)); }
        assert(fa_ba_size(32, FA_BA_SIZE_HT, 1024) == 32 && fa_ba_size(0, FA_BA_SIZE_HT, 1024) == 64 && fa_ba_size(1000, FA_BA_SIZE_HE, 1024) == 256 && fa_ba_size(0, 64, 16) == 16 && fa_ba_size(8, 64, 0) == 8);
        { struct fa_ba_ask a = { .start = true, .amsdu = true, .immediate = true, .token = 0x5a, .tid = 5, .size = 32, .timeout = 1000 };
          static const uint8_t WANT[9] = { 3, 1, 0x5a, 0, 0, 0x17, 0x08, 0xe8, 0x03 };
          assert(fa_addba_response(f, FA_ADDBA_RESPONSE_LENGTH - 1, OWN, AP, &a, 0, 32) == 0);
          assert(fa_addba_response(f, sizeof(f), OWN, AP, &a, 0, 32) == 33 && f[0] == FA_KIND_ACTION && !memcmp(f + 4, AP, 6) && !memcmp(f + 10, OWN, 6) && !memcmp(f + 16, AP, 6) && !memcmp(f + 24, WANT, 9));
          assert(fa_addba_response(f, sizeof(f), OWN, AP, &a, 37, 32) == 33 && f[27] == 37 && f[28] == 0); }
    }
    /* What a lost link's account reads: numbers only, and nothing past a frame's end. */
    {
        struct fa_door door; char line[64];
        uint8_t f[64] = {0};
        f[0] = FA_KIND_DEAUTH; f[1] = 0x08; f[22] = 0x50; f[23] = 0x01; f[24] = 3; f[25] = 0;
        assert(fa_door_read(f, 26, &door) && door.kind == FA_KIND_DEAUTH && door.reason == 3 && door.seq == 0x15 && door.retry && !door.protect && door.length == 26);
        assert(fa_door_read(f, 24, &door) && door.reason == 0 && !fa_door_read(f, 23, &door));
        f[1] = 0x40; assert(fa_door_read(f, 26, &door) && door.protect && door.reason == 0);
        f[0] = FA_KIND_DISASSOC; f[1] = 0; assert(fa_door_read(f, 26, &door) && door.kind == FA_KIND_DISASSOC);
        f[0] = FA_KIND_AUTH; assert(!fa_door_read(f, 26, &door));
        f[0] = FA_KIND_ACTION; f[24] = 0xfd;
        assert(fa_action_category(f, 25) == 0x7d && fa_action_category(f, 24) == -1);
        f[1] = 0x40; assert(fa_action_category(f, 25) == -1); f[1] = 0x80; f[28] = 3; assert(fa_action_category(f, 29) == 3 && fa_action_category(f, 28) == -1); f[1] = 0;
        f[0] = FA_KIND_AUTH; assert(fa_action_category(f, 25) == -1);
        assert(fa_frame_seq(f, 23) == 0);
        static const uint8_t E[] = { 1, 2, 9, 9, 90, 3, 1, 1, 1, 221, 0 };
        assert(fa_say_elements(line, sizeof(line), E, sizeof(E)) == 3 && !strcmp(line, "1:2 90:3 221:0 "));
        static const uint8_t L[] = { 1, 200, 9 };
        assert(fa_say_elements(line, sizeof(line), L, sizeof(L)) == 0 && !line[0]);
        assert(fa_say_elements(line, 7, E, sizeof(E)) == 1 && !strcmp(line, "1:2 "));
        for (unsigned i = 0; i < 200; i++) {	/* any bytes, in memory of exactly their size */
            unsigned n = next() % 60; uint8_t *x = malloc(n ? n : 1); for (unsigned k = 0; k < n; k++) x[k] = (uint8_t)next();
            char out[32]; (void)fa_say_elements(out, sizeof(out), x, n); (void)fa_door_read(x, n, &door); (void)fa_action_category(x, n); (void)fa_frame_seq(x, n);
            free(x);
        }
        unsigned at = fa_say_number(line, sizeof(line), 0, 0, ':'); at = fa_say_number(line, sizeof(line), at, 1234567890u, ' '); assert(!strcmp(line, "0:1234567890 "));
    }
    /* Stage 6's frames: the wanted protection, what an RSN element offers, the handshake's frames. */
    {
        static const uint8_t GOOD[] = { 48, 20, 1, 0, 0, 0x0f, 0xac, 4, 1, 0, 0, 0x0f, 0xac, 4, 1, 0, 0, 0x0f, 0xac, 2, 0, 0 };
        struct fa_rsn rsn;
        assert(sizeof(FA_RSN_OURS) == FA_RSN_LENGTH && !memcmp(FA_RSN_OURS, GOOD, sizeof(GOOD)));
        assert(fa_rsn_read(GOOD, sizeof(GOOD), &rsn) == NULL && rsn.group == 4 && rsn.pairwise == 1u << 4 && rsn.akm == 1u << 2);
        assert(fa_rsn_read(GOOD, 0, &rsn) != NULL);
        for (unsigned length = 0; length < sizeof(GOOD); length++) {	/* every length the element could say */
            uint8_t *e = malloc(length + 2); memcpy(e, GOOD, length + 2 > sizeof(GOOD) ? sizeof(GOOD) : length + 2); e[1] = (uint8_t)length;
            const char *why = fa_rsn_read(e, length + 2, &rsn);
            assert(length >= 18 ? why == NULL : why != NULL);
            free(e);
        }
        const struct { unsigned at; uint8_t value; const char *text; } changes[] = {
            { 7, 2, "group cipher" }, { 13, 2, "CCMP" }, { 19, 8, "WPA2 passphrase" }, { 19, 1, "WPA2 passphrase" }, { 20, 0x40, "protected management" }, { 2, 2, "not one this reads" },
        };
        for (unsigned i = 0; i < sizeof(changes) / sizeof(changes[0]); i++) {
            uint8_t e[sizeof(GOOD)]; memcpy(e, GOOD, sizeof(e)); e[changes[i].at] = changes[i].value;
            assert(strstr(fa_rsn_read(e, sizeof(e), &rsn), changes[i].text));
        }
        /* a list that says it is longer than the element */
        { uint8_t e[sizeof(GOOD)]; memcpy(e, GOOD, sizeof(e)); e[8] = 200; assert(strstr(fa_rsn_read(e, sizeof(e), &rsn), "ciphers")); }
        { uint8_t e[sizeof(GOOD)]; memcpy(e, GOOD, sizeof(e)); e[14] = 200; assert(strstr(fa_rsn_read(e, sizeof(e), &rsn), "key management")); }
        /* mixed: both suites offered, it is still joined */
        { uint8_t e[] = { 48, 24, 1, 0, 0, 0x0f, 0xac, 4, 1, 0, 0, 0x0f, 0xac, 4, 2, 0, 0, 0x0f, 0xac, 8, 0, 0x0f, 0xac, 2, 0x80, 0 };
          assert(fa_rsn_read(e, sizeof(e), &rsn) == NULL && rsn.akm == ((1u << 8) | (1u << 2)) && rsn.capabilities == 0x80); }

        const uint8_t body[5] = { 2, 3, 0, 1, 9 };
        uint8_t f[64]; const uint8_t *out; unsigned out_length, n;
        assert(fa_eapol_frame(f, 10, OWN, AP, true, body, 5) == 0);
        n = fa_eapol_frame(f, sizeof(f), OWN, AP, true, body, 5);
        assert(n == 26 + 8 + 5 && f[0] == 0x88 && f[1] == 1 && f[24] == 7 && !memcmp(f + 4, AP, 6) && !memcmp(f + 10, OWN, 6) && !memcmp(f + 16, AP, 6));
        assert(f[32] == 0x88 && f[33] == 0x8e && !memcmp(f + 34, body, 5));
        n = fa_eapol_frame(f, sizeof(f), OWN, AP, false, body, 5);
        assert(n == 24 + 8 + 5 && f[0] == 0x08);
        /* What we build, seen from the other side: the same but from the access point. */
        memcpy(f + 4, OWN, 6); memcpy(f + 10, AP, 6); f[1] = 2;
        assert(fa_eapol_read(f, n, false, false, OWN, AP, &out, &out_length) && out_length == 5 && !memcmp(out, body, 5));
        for (unsigned i = 0; i < n; i++) {
            uint8_t *exact = malloc(i ? i : 1); memcpy(exact, f, i);
            assert(!fa_eapol_read(exact, i, false, false, OWN, AP, &out, &out_length) || i > 32);
            free(exact);
        }
        assert(!fa_eapol_read(f, n, false, false, OTHER, AP, &out, &out_length));	/* for another */
        assert(!fa_eapol_read(f, n, false, false, OWN, OTHER, &out, &out_length));	/* from another */
        f[1] = 1; assert(!fa_eapol_read(f, n, false, false, OWN, AP, &out, &out_length)); f[1] = 2;	/* to the system, not from it */
        f[0] = 0x48; assert(!fa_eapol_read(f, n, false, false, OWN, AP, &out, &out_length)); f[0] = 0x08;	/* null data */
        f[31] = 0x00; assert(!fa_eapol_read(f, n, false, false, OWN, AP, &out, &out_length)); f[31] = 0x8e;	/* another type */
        memcpy(f + 16, OTHER, 6); assert(!fa_eapol_read(f, n, false, false, OWN, AP, &out, &out_length)); memcpy(f + 16, AP, 6);	/* relayed from another */
        /* Protected: the cipher's 8 bytes are skipped only if they are there. */
        memmove(f + 32, f + 24, n - 24); memset(f + 24, 0, 8); f[1] = 0x42;
        assert(fa_eapol_read(f, n + 8, false, false, OWN, AP, &out, &out_length) && out_length == 5);
        assert(!fa_eapol_read(f, n + 8, false, true, OWN, AP, &out, &out_length));
        /* Ethernet form. */
        memset(f, 0, sizeof(f)); memcpy(f, OWN, 6); memcpy(f + 6, AP, 6); f[12] = 0x88; f[13] = 0x8e; memcpy(f + 14, body, 5);
        assert(fa_eapol_read(f, 19, true, false, OWN, AP, &out, &out_length) && out_length == 5 && out == f + 14);
        assert(!fa_eapol_read(f, 14, true, false, OWN, AP, &out, &out_length) && !fa_eapol_read(f, 19, true, false, OWN, OTHER, &out, &out_length));
        f[13] = 0; assert(!fa_eapol_read(f, 19, true, false, OWN, AP, &out, &out_length));
    }
    f[0] = FA_KIND_BEACON; assert(fa_rx_kind(f, 24, AP) == FA_RX_BEACON); assert(fa_rx_kind(f, 24, OTHER) == FA_RX_ELSE); assert(fa_rx_kind(f, 23, AP) == FA_RX_ELSE);
    f[0] = FA_KIND_AUTH; assert(fa_rx_kind(f, 24, AP) == FA_RX_MANAGEMENT);
    f[0] = 0x88; assert(fa_rx_kind(f, 24, AP) == FA_RX_DATA); assert(fa_rx_kind(f, 24, OTHER) == FA_RX_ELSE);
    f[0] = 0x08; assert(fa_rx_kind(f, 24, AP) == FA_RX_DATA);
    f[0] = 0xc8; assert(fa_rx_kind(f, 24, AP) == FA_RX_ELSE);
    f[0] = 0x48; assert(fa_rx_kind(f, 24, AP) == FA_RX_ELSE);
    f[0] = 0xd4; assert(fa_rx_kind(f, 24, AP) == FA_RX_ELSE);
    f[0] = 0x89; assert(fa_rx_kind(f, 24, AP) == FA_RX_ELSE);
    puts("kinds of frame");
}

/* Noise, and good frames with some bytes changed. Nothing may read outside its frame, and the result stays sound. */
static void noise(void) {
    static struct fa_target t;
    uint8_t f[1024];
    struct fa_reply r;
    struct fa_he he;
    static const uint8_t good[] = {0, 4, 'h', 'o', 'm', 'e', 1, 1, 0x82, 3, 1, 6, 45, 3, 1, 2, 3, 48, 2, 1, 0, 221, 4, 0, 0x50, 0xf2, 1, 255, 3, 35, 1, 2};
    for (unsigned round = 0; round < 400000; round++) {
        unsigned n;
        if (round % 2) {
            n = beacon(f, round % 4 == 1 ? FA_KIND_BEACON : FA_KIND_PROBE_RESPONSE, AP, next() & 0xffff, good, sizeof(good));
            for (unsigned k = next() % 5; k > 0; k--) f[next() % n] = (uint8_t)next();
            n = next() % 8 == 0 ? next() % (n + 1) : n;
        } else {
            n = next() % sizeof(f);
            for (unsigned i = 0; i < n; i++) f[i] = (uint8_t)next();
            f[0] = round % 8 == 0 ? (uint8_t)next() : round % 16 == 2 ? FA_KIND_AUTH : round % 16 == 4 ? FA_KIND_ASSOC_RESPONSE : FA_KIND_BEACON;
        }
        uint8_t *e = exactly(f, n);
        t.fixed = round % 64 == 0;
        (void)fa_target_take(&t, e, n, 1 + next() % 0xffff, (int)(next() % 400) - 200, (const uint8_t *)"home", 4);
        assert(t.elements_length <= sizeof(t.elements));
        fa_reply_read(&r, e, n, OWN, AP);
        assert(r.answer != FA_ANSWER_ASSOC || (r.aid != 0 && r.aid <= FA_AID_MOST && r.elements + r.elements_length == e + n));
        (void)fa_rx_kind(e, n, AP);
        if (n >= 36) {
            const uint8_t *x = fa_ext_element(e + 36, n - 36, FA_EXT_HE);
            uint32_t basic;
            static const uint16_t bits[] = {10, 20, 55, 110};
            if (x != NULL) (void)fa_he_read(x, &he);
            (void)fa_protection(next(), e + 36, n - 36);
            (void)fa_rates_mask(bits, 4, e + 36, n - 36, &basic);
            (void)fa_peer_streams(fa_element(e + 36, n - 36, FA_EID_HT), fa_element(e + 36, n - 36, FA_EID_VHT), NULL);
        }
        free(e);
    }
    printf("noise: found %d, heard %u, protected %u\n", t.found, t.heard, t.protected_frames);
}

int main(void) {
    protection();
    target();
    reply();
    builders();
    he_and_streams();
    kinds();
    noise();
    return 0;
}
"""


@check
def join_runs_on_fake_headers(tmp):
    """Stage 5's join on the host with a fake driver, access point and clock: every way it can end. Design M5."""
    for name, text in FAKE_HEADERS.items():
        path = tmp / 'fake' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    own = ROOT / 'xnu/fuxi-ath12k'
    (tmp / 'fake/lx.h').write_bytes((own / 'compat/lx.h').read_bytes())
    (tmp / 'fake/lx_os.h').write_bytes((own / 'compat/include/lx_os.h').read_bytes())
    vectors = wpa_vectors(b'fuxi-test-net', bytes.fromhex('024655584901'), bytes.fromhex('02a1b2c3d455'))
    (tmp / 'driver.c').write_text('#include <stdarg.h>\n#include <linux/kernel.h>\n' + vectors + JOIN_DRIVER)
    program = tmp / 'driver'
    cc = os.environ.get('CC', 'cc')
    subprocess.run([cc, '-std=gnu11', '-w', '-g', '-fsanitize=address,undefined', '-fno-sanitize-recover=all',
                    f'-I{tmp / "fake"}', f'-I{own / "src"}', str(tmp / 'driver.c'), str(own / 'src/fa_wpa.c'), '-o', str(program)], check=True)
    subprocess.run([str(program)], check=True)


@check
def join_frames_on_malformed_input(tmp):
    """src/fa_join.h under the sanitizers: each frame in memory of exactly its size. Design M5: refuses RSN and WPA."""
    (tmp / 'join.c').write_text(FA_JOIN_HOST)
    own = ROOT / 'xnu/fuxi-ath12k/src'
    cc = os.environ.get('CC', 'cc')
    flags = ['-std=gnu11', '-Wall', '-Wextra', '-Wno-unused-function', '-g', '-O1', f'-I{own}']
    program = tmp / 'join'
    subprocess.run([cc, *flags, '-fsanitize=address,undefined', '-fno-sanitize-recover=all', str(tmp / 'join.c'), '-o', str(program)], check=True)
    subprocess.run([str(program)], check=True)


def code_only(text):
    """C without its comments and strings: what the program does, not what it says."""
    return re.sub(r'/\*.*?\*/|//[^\n]*|"(?:[^"\\]|\\.)*"', ' ', text, flags=re.S)


@check
def the_key_stays_in_its_place(tmp):
    """Stage 5 knows no key, and stage 6's comes from the device tree alone: never a boot argument or a file, never in a line of the log."""
    own = ROOT / 'xnu/fuxi-ath12k/src'
    wlan = (ROOT / 'xnu/fuxi-wlan/fuxi_wlan.c').read_text()
    for path in (own / 'fa_hif.c', own / 'fa_qmi.c', ROOT / 'xnu/fuxi-ath12k/os/fuxi_ath12k_os.c'):
        code = code_only(path.read_text()).lower()
        for word in ('passphrase', 'psk', 'pmk', 'ptk', 'gtk', 'set_key', 'wpa_', 'vnode', 'vn_open', 'kern_open', 'test-network'):
            assert word not in code, f'{path.name}: {word}'
    for path in (own / 'fa_80211.c', own / 'fa_join.h', own / 'fa_net.h', own / 'fa_wpa.c', own / 'fa_wpa.h'):
        code = code_only(path.read_text()).lower()
        for word in ('passphrase', 'vnode', 'vn_open', 'kern_open', 'test-network', 'lx_os_option', 'pe_parse'):
            assert word not in code, f'{path.name}: {word}'
    assert 'printf' not in code_only((own / 'fa_wpa.c').read_text()) and 'lx_tell' not in (own / 'fa_wpa.c').read_text()
    # No line of the log is given anything of a key.
    for call in re.findall(r'lx_tell\(.*?\);', (own / 'fa_80211.c').read_text(), flags=re.S):
        for word in ('target_key', '.pmk', 'wpa.pairwise', 'wpa.group,', 'wpa.group)', '.kck', '.kek', 'nonce', 'radio->eapol,', '->key', 'target_name,', 'target_name)'):
            assert word not in call, call
    block = wlan[wlan.index('static void\nwl_fa_join_configure(void)'):]
    block = block[:block.index('\n}\n')]
    # The one boot argument it reads says whether stage 8 looks at all: never a key or a name.
    assert 'wl_fa_stage != 6 && wl_fa_stage != 7' in block and 'PE_parse' not in block.replace('PE_parse_boot_argn("fuxiwifiauto", &wants_auto, sizeof(wants_auto))', '') and 'fuxi-wifi-join' in block and block.count('printf(') == 3
    assert 'bytes + 1 + bytes[0]' not in ''.join(line for line in block.splitlines() if 'printf' in line)
    assert not re.search(r'PE_parse[a-z_]*\("fuxiwifi(key|pmk|pass|psk)', wlan)
    guarded = wlan[wlan.index('static void\nwl_fa_ssid_configure(void)'):]
    guarded = guarded[:guarded.index('\n}\n')]
    assert 'wl_fa_stage != 5' in guarded and 'text);' not in guarded.replace('strlen(text);', '').replace('(const unsigned char *)text', '')
    assert wlan.count('"fuxiwifissid"') == 1
    # What transmits sits in the stage 5 block, and that block is reached from stage 5 only.
    text = (own / 'fa_80211.c').read_text()
    block = text.index('/* ---- stage 5: one open network')
    for word in ('ops->tx(', 'ops->sta_state', 'ops->assign_vif_chanctx', 'ops->add_chanctx', 'ops->vif_cfg_changed', 'ops->link_info_changed', 'dev_alloc_skb'):
        assert text.index(word) > block, word
    assert text.count('ops->set_key') == 2 and text.index('ops->set_key') > block
    assert text.count('radio->say_names = true') == 1 and text.index('radio->say_names = true') > text.index('\nfa_m4_station(')
    hif = (own / 'fa_hif.c').read_text()
    assert 'hif.stage == FA_STAGE_JOIN || hif.stage == FA_STAGE_WPA || hif.stage == FA_STAGE_ADDRESS ? fa_m5_station(' in hif and hif.count('fa_m5_station(') == 1
    # 20 MHz and no EHT: the join sets one width, and no element 108 is built.
    join = (own / 'fa_join.h').read_text()
    assert 'NL80211_CHAN_WIDTH_20' in text and 'NL80211_CHAN_WIDTH_40' not in text and 'NL80211_CHAN_WIDTH_80' not in text
    assert 'eht' not in code_only(join).lower()



def wpa_vectors(name, own, ap, wrong=False):
    """A peer of the handshake made from the standard, no code of the kernel's: what it sends and what it must hear. C arrays."""
    from cryptography.hazmat.primitives.keywrap import aes_key_wrap
    ours = bytes.fromhex('30140100000fac040100000fac040100000fac020000')
    pmk = hashlib.pbkdf2_hmac('sha1', b'not a real passphrase', name, 4096, 32)
    snonce, anonce = bytes(range(32)), bytes(range(32, 64))
    data = min(own, ap) + max(own, ap) + min(snonce, anonce) + max(snonce, anonce)
    ptk = b''.join(hmac.digest(pmk, b'Pairwise key expansion\0' + data + bytes([i]), 'sha1') for i in range(3))[:48]
    gtk, gtk2, gtk3 = bytes(range(100, 116)), bytes(range(120, 136)), bytes(range(140, 156))

    def message(info, counter, nonce, payload=b'', signed=True, rsc=0):
        out = bytearray(99 + len(payload))
        out[65:71] = rsc.to_bytes(6, 'little')
        out[:4] = bytes([2, 3]) + (len(out) - 4).to_bytes(2, 'big')
        out[4] = 2
        out[5:7] = info.to_bytes(2, 'big')
        out[9:17] = counter.to_bytes(8, 'big')
        out[17:49] = nonce
        out[97:99] = len(payload).to_bytes(2, 'big')
        out[99:] = payload
        if signed:
            out[81:97] = hmac.digest(ptk[:16], out, 'sha1')[:16]
        return bytes(out)

    def wrapped(rsn=ours, kde=b'\xdd\x16\x00\x0f\xac\x01\x01\x00' + gtk):
        plain = rsn + kde
        plain += b'\xdd\x00' + bytes(-(len(plain) + 2) % 8)
        return aes_key_wrap(ptk[16:32], plain)

    def array(label, value):
        return f'static const unsigned char {label}[]={{' + ','.join(map(str, value)) + '};\n'

    m3 = message(0x13ca, 2, anonce, wrapped())
    forged = bytearray(m3)
    forged[81] ^= 1
    return ''.join(array(k, v) for k, v in [
        ('V_RSN', ours), ('V_PMK', pmk), ('V_TK', ptk[32:]), ('V_GTK', gtk),
        ('V_M1', message(0x8a, 1, anonce, signed=False)),
        ('V_M2', message(0x010a, 1, snonce, ours)),
        ('V_M3', m3), ('V_M3_FORGED', forged),
        ('V_M3_SHORT_GTK', message(0x13ca, 2, anonce, wrapped(kde=b'\xdd\x0e\x00\x0f\xac\x01\x01\x00' + gtk[:8]))),
        ('V_M3_INDEX_0', message(0x13ca, 2, anonce, wrapped(kde=b'\xdd\x16\x00\x0f\xac\x01\x00\x00' + gtk))),
        ('V_M3_OTHER_RSN', message(0x13ca, 2, anonce, wrapped(rsn=ours[:18] + b'\x08' + ours[19:]))),
        ('V_M3_NO_RSN', message(0x13ca, 2, anonce, wrapped(rsn=b''))),
        ('V_G1', message(0x1382, 3, anonce, wrapped(rsn=b''))),
        ('V_GTK2', gtk2), ('V_GTK3', gtk3),
        ('V_G2', message(0x1382, 3, anonce, wrapped(rsn=b'', kde=b'\xdd\x16\x00\x0f\xac\x01\x02\x00' + gtk2), rsc=5)),
        ('V_G2_REPLY', message(0x0302, 3, bytes(32))),
        ('V_G3', message(0x1382, 4, anonce, wrapped(rsn=b'', kde=b'\xdd\x16\x00\x0f\xac\x01\x01\x00' + gtk3))),
        ('V_G3_REPLY', message(0x0302, 4, bytes(32))),
        ('V_G4', message(0x1382, 5, anonce, wrapped(rsn=b'', kde=b'\xdd\x16\x00\x0f\xac\x01\x02\x00' + gtk3), rsc=8)),
        ('V_G4_REPLY', message(0x0302, 5, bytes(32))),
        ('V_M4', message(0x030a, 2, bytes(32))),
    ])


FA_WPA_DRIVER = r"""
#include <assert.h>
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include "fa_wpa.h"
static const unsigned char OWN[6] = {2, 0x46, 0x55, 0x58, 0x49, 1}, AP[6] = {0x02, 0xa1, 0xb2, 0xc3, 0xd4, 0x55};
static void begin(struct fa_wpa *w) {
    memset(w, 0, sizeof(*w)); memcpy(w->pmk, V_PMK, 32); memcpy(w->own, OWN, 6); memcpy(w->peer, AP, 6);
    for (int i = 0; i < 32; i++) w->own_nonce[i] = (unsigned char)i;
    w->rsn = V_RSN; w->rsn_length = sizeof(V_RSN); w->peer_rsn = V_RSN; w->peer_rsn_length = sizeof(V_RSN);
}
/* In memory of exactly its size. */
static int say(struct fa_wpa *w, const unsigned char *m, unsigned n, unsigned char *reply, unsigned *length, const char **why) {
    unsigned char *exact = malloc(n ? n : 1); memcpy(exact, m, n);
    int r = fa_wpa_message(w, exact, n, reply, length, why);
    free(exact);
    return r;
}
int main(void) {
    struct fa_wpa w; unsigned char reply[FA_WPA_REPLY_MOST]; unsigned n; const char *why;
    begin(&w);
    assert(say(&w, V_M3, sizeof(V_M3), reply, &n, &why) == FA_WPA_BAD);		/* no message 1 yet */
    assert(say(&w, V_M1, sizeof(V_M1), reply, &n, &why) == FA_WPA_ANSWER && n == sizeof(V_M2) && !memcmp(reply, V_M2, n));
    assert(!memcmp(w.pairwise, V_TK, 16));
    assert(say(&w, V_M1, sizeof(V_M1), reply, &n, &why) == FA_WPA_ANSWER && !memcmp(reply, V_M2, n));	/* sent again */
    assert(say(&w, V_M3_FORGED, sizeof(V_M3_FORGED), reply, &n, &why) == FA_WPA_BAD && strstr(why, "signature"));
    assert(say(&w, V_M3, sizeof(V_M3), reply, &n, &why) == FA_WPA_KEYS && n == sizeof(V_M4) && !memcmp(reply, V_M4, n));
    assert(w.group_length == 16 && w.group_index == 1 && !memcmp(w.group, V_GTK, 16));
    assert(say(&w, V_M3, sizeof(V_M3), reply, &n, &why) == FA_WPA_BAD);		/* played again */
    /* W1: no message moves the keys once message 3 was good. */
    assert(say(&w, V_M1, sizeof(V_M1), reply, &n, &why) == FA_WPA_BAD && strstr(why, "over") && !memcmp(w.pairwise, V_TK, 16));
    assert(say(&w, V_G1, sizeof(V_G1), reply, &n, &why) == FA_WPA_GROUP);
    /* W7: the group key is 16 bytes with a number a group key may have, and message 3 says what the beacon said. */
    const struct { const unsigned char *m; unsigned n; const char *text; } bad[] = {
        { V_M3_SHORT_GTK, sizeof(V_M3_SHORT_GTK), "no group key" }, { V_M3_INDEX_0, sizeof(V_M3_INDEX_0), "no group key" },
        { V_M3_OTHER_RSN, sizeof(V_M3_OTHER_RSN), "RSN" }, { V_M3_NO_RSN, sizeof(V_M3_NO_RSN), "RSN" },
    };
    for (unsigned i = 0; i < sizeof(bad) / sizeof(bad[0]); i++) {
        begin(&w);
        assert(say(&w, V_M1, sizeof(V_M1), reply, &n, &why) == FA_WPA_ANSWER);
        assert(say(&w, bad[i].m, bad[i].n, reply, &n, &why) == FA_WPA_BAD && strstr(why, bad[i].text) && !w.replay_known);
    }
    /* Every shorter length, and every byte changed, under the sanitizers. */
    for (unsigned i = 0; i < sizeof(V_M3); i++) {
        begin(&w);
        assert(say(&w, V_M1, sizeof(V_M1), reply, &n, &why) == FA_WPA_ANSWER);
        assert(say(&w, V_M3, i, reply, &n, &why) != FA_WPA_KEYS);
        unsigned char *changed = malloc(sizeof(V_M3)); memcpy(changed, V_M3, sizeof(V_M3)); changed[i] ^= 0x41;
        begin(&w);
        (void)say(&w, V_M1, sizeof(V_M1), reply, &n, &why);
        assert(say(&w, changed, sizeof(V_M3), reply, &n, &why) != FA_WPA_KEYS || i == 0);	/* (the version byte is signed but free) */
        free(changed);
    }
    puts("fa_wpa: independent peer, messages 2 and 4 byte for byte, W1, W7, lengths and changed bytes");
    return 0;
}
"""


@check
def wpa_against_an_independent_peer(tmp):
    """src/fa_wpa.c: the keys and every byte we answer with, against a peer made from the standard in Python. Design M6."""
    own = ROOT / 'xnu/fuxi-ath12k/src'
    vectors = wpa_vectors(b'fuxi-test-net', bytes.fromhex('024655584901'), bytes.fromhex('02a1b2c3d455'))
    (tmp / 'wpa.c').write_text(vectors + FA_WPA_DRIVER)
    cc = os.environ.get('CC', 'cc')
    program = tmp / 'wpa'
    subprocess.run([cc, '-std=gnu11', '-Wall', '-Wextra', '-g', '-O1', '-fsanitize=address,undefined', '-fno-sanitize-recover=all', f'-I{own}',
                    str(tmp / 'wpa.c'), str(own / 'fa_wpa.c'), '-o', str(program)], check=True)
    subprocess.run([str(program)], check=True)


FA_NET_HOST = r"""
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "fa_bss.h"
#include "fa_join.h"
#include "fa_net.h"
#include "fa_xnu.h"
#include "fa_ios.h"
static const uint8_t OWN[6] = {2, 0x46, 0x55, 0x58, 0x49, 1}, AP[6] = {0x02, 0xa1, 0xb2, 0xc3, 0xd4, 0x55}, SERVER[6] = {0x02, 0, 0, 0, 0, 9};
static const uint8_t ME[4] = {10, 0, 0, 9}, THEM[4] = {10, 0, 0, 1};
static uint32_t seed = 0x1234567;
/* The filter on what iOS sends, in memory of exactly the frame's size. */
static bool tx_takes(unsigned filter, const uint8_t *frame, unsigned length) {
    uint8_t *x = malloc(length ? length : 1); memcpy(x, frame, length);
    bool taken = fa_tx_filtered(filter, x, length);
    free(x);
    return taken;
}
/* An IPv4 frame with a header of `words` and ports after it, of 60 bytes: to one, or to all. */
static unsigned tx_ip(uint8_t *t, bool all, unsigned protocol, unsigned words, unsigned source, unsigned target) {
    memset(t, 0, 100); memset(t, all ? 0xff : 2, 6); memcpy(t + 6, OWN, 6); t[12] = 8;
    t[14] = (uint8_t)(0x40 | words); t[14 + 3] = 46; t[14 + 9] = (uint8_t)protocol;
    if (words >= 5 && words <= 10) { fa_put16(t + 14 + words * 4, source); fa_put16(t + 14 + words * 4 + 2, target); }
    return 60;
}
static uint32_t next(void) { seed ^= seed << 13; seed ^= seed >> 17; seed ^= seed << 5; return seed; }
/* A server's answer, by the RFC's bytes. */
static unsigned offer(uint8_t *out, unsigned kind, uint32_t xid, const uint8_t *chaddr) {
    uint8_t *udp = out + 20, *b = udp + 8, *o = b + 240;
    memset(out, 0, 400);
    b[0] = 2; b[1] = 1; b[2] = 6; b[4] = xid >> 24; b[5] = xid >> 16; b[6] = xid >> 8; b[7] = xid;
    memcpy(b + 16, ME, 4); memcpy(b + 28, chaddr, 6); b[236] = 99; b[237] = 130; b[238] = 83; b[239] = 99;
    *o++ = 53; *o++ = 1; *o++ = kind; *o++ = 0; *o++ = 54; *o++ = 4; memcpy(o, THEM, 4); o += 4;
    *o++ = 1; *o++ = 4; *o++ = 255; *o++ = 255; *o++ = 255; *o++ = 0;
    *o++ = 3; *o++ = 8; memcpy(o, THEM, 4); memset(o + 4, 7, 4); o += 8;
    *o++ = 6; *o++ = 4; *o++ = 8; *o++ = 8; *o++ = 8; *o++ = 8;
    *o++ = 51; *o++ = 4; *o++ = 0; *o++ = 1; *o++ = 0; *o++ = 0; *o++ = 255;
    unsigned udp_length = (unsigned)(o - udp), total = 20 + udp_length;
    fa_ip_header(out, 17, THEM, ME, total, 7);
    udp[1] = 67; udp[3] = 68; udp[4] = udp_length >> 8; udp[5] = udp_length;
    return total;
}
int main(void) {
    uint8_t m[2048], f[2048]; struct fa_dhcp_reply r; struct fa_data d; uint8_t hw[6];
    /* What we send, by the RFC's bytes. */
    unsigned n = fa_dhcp_build(m, sizeof(m), FA_DHCP_DISCOVER, OWN, 0x11223344, false, NULL, NULL, NULL);
    assert(n == FA_DHCP_LENGTH && fa_dhcp_build(m, n - 1, FA_DHCP_DISCOVER, OWN, 1, false, NULL, NULL, NULL) == 0);
    assert(fa_ip_sum(m, 20) == 0 && m[9] == 17 && !memcmp(m + 12, "\0\0\0\0", 4) && !memcmp(m + 16, "\xff\xff\xff\xff", 4));
    assert(m[21] == 68 && m[23] == 67 && m[28] == 1 && m[28 + 4] == 0x11 && m[28 + 10] == 0 && !memcmp(m + 28 + 28, OWN, 6) && !memcmp(m + 28 + 236, "\x63\x82\x53\x63", 4));
    assert(m[28 + 240] == 53 && m[28 + 242] == 1);
    n = fa_dhcp_build(m, sizeof(m), FA_DHCP_REQUEST, OWN, 5, true, ME, THEM, NULL);
    assert(m[28 + 10] == 0x80 && fa_element(m + 28 + 240, 60, 50) && !memcmp(fa_element(m + 28 + 240, 60, 50) + 2, ME, 4) && !memcmp(fa_element(m + 28 + 240, 60, 54) + 2, THEM, 4));
    n = fa_dhcp_build(m, sizeof(m), FA_DHCP_RELEASE, OWN, 5, false, NULL, THEM, ME);
    assert(!memcmp(m + 12, ME, 4) && !memcmp(m + 16, THEM, 4) && !memcmp(m + 28 + 12, ME, 4) && m[28 + 242] == 7 && fa_ip_sum(m, 20) == 0);
    /* A good answer, then each way it is not one. */
    n = offer(m, 2, 0xabcd, OWN);
    assert(fa_dhcp_read(m, n, OWN, 0xabcd, &r) && r.kind == 2 && !memcmp(r.address, ME, 4) && !memcmp(r.server, THEM, 4) && !memcmp(r.router, THEM, 4) && r.mask[2] == 255 && r.name_server[0] == 8 && r.lease == 65536);
    assert(!fa_dhcp_read(m, n, OWN, 0xabce, &r) && !fa_dhcp_read(m, n, SERVER, 0xabcd, &r));
    n = offer(m, 6, 1, OWN); assert(fa_dhcp_read(m, n, OWN, 1, &r) && r.kind == 6);
    n = offer(m, 9, 1, OWN); assert(!fa_dhcp_read(m, n, OWN, 1, &r));
    n = offer(m, 2, 1, OWN); m[10] ^= 1; assert(!fa_dhcp_read(m, n, OWN, 1, &r));	/* a wrong header checksum */
    n = offer(m, 2, 1, OWN); m[6] = 0x20; m[10] = m[11] = 0; fa_put16(m + 10, fa_ip_sum(m, 20)); assert(!fa_dhcp_read(m, n, OWN, 1, &r));	/* a fragment */
    /* Every shorter length and random bytes, each in memory of exactly its size. */
    n = offer(m, 2, 1, OWN);
    for (unsigned i = 0; i < n; i++) {
        uint8_t *x = malloc(i ? i : 1); memcpy(x, m, i);
        (void)fa_dhcp_read(x, i, OWN, 1, &r); (void)fa_net_wanted(FA_TYPE_IP, x, i); (void)fa_echo_read(x, i, THEM, ME, 1, 1); (void)fa_arp_read(x, i, ME, THEM, hw); (void)fa_net_wanted(FA_TYPE_ARP, x, i);
        free(x);
    }
    for (unsigned round = 0; round < 40000; round++) {
        unsigned size = next() % 500; uint8_t *x = malloc(size ? size : 1);
        memcpy(x, m, size < n ? size : n);
        for (unsigned k = next() % 6; k > 0 && size > 0; k--) x[next() % size] = (uint8_t)next();
        if (size > 20 && (next() & 1)) { x[10] = x[11] = 0; fa_put16(x + 10, fa_ip_sum(x, 20)); }
        (void)fa_dhcp_read(x, size, OWN, 1, &r); (void)fa_net_wanted(FA_TYPE_IP, x, size); (void)fa_echo_read(x, size, THEM, ME, 1, 1);
        (void)fa_data_read(x, size, next() & 1, next() & 1, OWN, AP, &d);
        free(x);
    }
    /* What goes out, counted, reading nothing past the length. */
    {
        struct fa_tx_counts c; memset(&c, 0, sizeof(c)); uint8_t b[20] = {0};
        fa_tx_note(&c, FA_TYPE_ARP, b, 0); fa_tx_note(&c, 0x86dd, b, 0); fa_tx_note(&c, 0x888e, b, 0); fa_tx_note(&c, 0x88b5, b, 0);
        fa_tx_note(&c, FA_TYPE_IP, b, 19);
        b[9] = 17; fa_tx_note(&c, FA_TYPE_IP, b, 20); b[9] = 6; fa_tx_note(&c, FA_TYPE_IP, b, 20); b[9] = 1; fa_tx_note(&c, FA_TYPE_IP, b, 20); b[9] = 47; fa_tx_note(&c, FA_TYPE_IP, b, 20);
        assert(c.arp == 1 && c.ip6 == 1 && c.eapol == 1 && c.other == 1 && c.udp == 1 && c.tcp == 1 && c.icmp == 1 && c.ip_other == 2);
    }
    /* The filter on what iOS sends: what each bit takes, and that it reads nothing past the frame. */
    {
        enum { I = FA_TX_DROP_IP6, T = FA_TX_DROP_TCP, U = FA_TX_DROP_UDP, G = FA_TX_DROP_GROUP, EVERY = I | T | U | G };
        uint8_t t[100]; unsigned size;
        /* Each kind of frame against every mask: `wanted` says the bits that take it. */
        static const struct { bool all; unsigned type, protocol, source, target, wanted; } KINDS[] = {
            { false, 0x86dd, 6, 1000, 80, I },		{ true, 0x86dd, 17, 546, 547, I | G },		/* IPv6, DHCPv6 is no exception */
            { false, 0x0800, 6, 1000, 443, T },		{ true, 0x0800, 6, 1000, 443, T | G },
            { false, 0x0800, 6, 68, 67, T },		{ true, 0x0800, 6, 67, 68, T | G },		/* the ports are UDP's alone */
            { false, 0x0800, 17, 1000, 53, U },		{ true, 0x0800, 17, 5353, 5353, U | G },
            { false, 0x0800, 17, 66, 69, U },
            { false, 0x0800, 1, 0, 0, 0 },		{ true, 0x0800, 1, 0, 0, G },			/* neither TCP nor UDP */
            { false, 0x0800, 47, 0, 0, 0 },		{ true, 0x0800, 47, 0, 0, G },
            { false, 0x88b5, 0, 0, 0, 0 },		{ true, 0x88b5, 0, 0, 0, G },
            { false, 0x8100, 0, 0, 0, 0 },							/* what is inside a tag is not looked at */
            { false, 0x0806, 0, 0, 0, 0 },		{ true, 0x0806, 0, 0, 0, 0 },			/* ARP always */
            { false, 0x888e, 0, 0, 0, 0 },		{ true, 0x888e, 0, 0, 0, 0 },
            { false, 0x0800, 17, 68, 67, 0 },		{ true, 0x0800, 17, 68, 67, 0 },		/* DHCP always, any end */
            { false, 0x0800, 17, 67, 68, 0 },		{ true, 0x0800, 17, 67, 68, 0 },
            { false, 0x0800, 17, 1000, 67, 0 },		{ true, 0x0800, 17, 68, 1000, 0 },
        };
        for (unsigned k = 0; k < sizeof(KINDS) / sizeof(KINDS[0]); k++) {
            for (unsigned mask = 0; mask <= EVERY; mask++) {
                size = tx_ip(t, KINDS[k].all, KINDS[k].protocol, 5, KINDS[k].source, KINDS[k].target);
                t[17] = 46; fa_put16(t + 12, KINDS[k].type);
                assert(tx_takes(mask, t, size) == ((mask & KINDS[k].wanted) != 0));
            }
        }
        /* Off: nothing, even what is no frame at all. */
        assert(!fa_tx_filtered(0, NULL, 0) && !tx_takes(EVERY, t, 0) && !tx_takes(EVERY, t, 13));
        /* Headers alone, to one: nothing says TCP or UDP but a header that can be one; to all, whatever is not ARP or DHCP goes. */
        size = tx_ip(t, false, 17, 5, 1000, 53);
        for (unsigned n = 14; n < 14 + 20; n++) { assert(!tx_takes(T | U | I, t, n)); memset(t, 0xff, 6); assert(tx_takes(G, t, n)); memset(t, 2, 6); }
        assert(tx_takes(U, t, 14 + 20) && !tx_takes(T, t, 14 + 20));			/* no room for ports: not DHCP */
        t[14] = 0x44; assert(!tx_takes(EVERY & ~G, t, size)); memset(t, 0xff, 6); assert(tx_takes(G, t, size)); memset(t, 2, 6);	/* words below 5 */
        t[14] = 0x65; assert(!tx_takes(EVERY & ~G, t, size)); t[14] = 0x45;		/* version 6 under the IPv4 type */
        { uint8_t a[14]; memcpy(a, t, 14); a[12] = 8; a[13] = 6; assert(!tx_takes(EVERY, a, 14)); a[0] = 0xff; assert(!tx_takes(EVERY, a, 14)); }	/* an ARP with nothing in it */
        /* What only looks like DHCP. */
        size = tx_ip(t, true, 17, 5, 68, 67); t[17] = 28; assert(!tx_takes(EVERY, t, size));
        t[17] = 27; assert(tx_takes(U, t, size) && tx_takes(G, t, size));		/* its length has no room for the UDP header */
        t[17] = 19; assert(tx_takes(U, t, size));					/* nor for the IP header */
        fa_put16(t + 16, 47); assert(!tx_takes(EVERY, t, 14 + 47) && tx_takes(U, t, 14 + 46) && tx_takes(G, t, 14 + 46));	/* longer than the frame */
        fa_put16(t + 16, 28); assert(!tx_takes(EVERY, t, 14 + 28) && tx_takes(U, t, 14 + 27) && tx_takes(U, t, 14 + 22) && tx_takes(U, t, 14 + 21));
        t[20] = 0x20; assert(tx_takes(U, t, size) && tx_takes(G, t, size)); t[20] = 0;	/* a first fragment */
        t[21] = 1; assert(tx_takes(U, t, size)); t[21] = 0;				/* a later one: its first bytes are no ports */
        t[20] = 0x40; assert(!tx_takes(EVERY, t, size)); t[20] = 0;			/* "do not fragment" is no fragment */
        t[24] = 0xde; t[25] = 0xad; assert(!tx_takes(EVERY, t, size));			/* a wrong checksum is not asked after */
        /* The ports are after the header as long as it says. */
        size = tx_ip(t, true, 17, 6, 68, 67); fa_put16(t + 16, 32); assert(!tx_takes(EVERY, t, size));
        size = tx_ip(t, true, 17, 6, 1000, 53); fa_put16(t + 16, 32); t[34] = 0; t[35] = 68; t[36] = 0; t[37] = 67; assert(tx_takes(U, t, size));
        size = tx_ip(t, true, 17, 15, 0, 0); assert(tx_takes(U, t, 14 + 21) && tx_takes(U, t, size));	/* a header past the frame */
        fa_put16(t + 16, 68); t[14 + 60] = 0; t[14 + 61] = 68; t[14 + 62] = 0; t[14 + 63] = 67; assert(tx_takes(U, t, 14 + 64) && !tx_takes(EVERY, t, 14 + 68));
        /* Random bytes, of each size: the sanitizers read along; off takes nothing, ARP passes, and more bits never take less. */
        for (int i = 0; i < 20000; i++) {
            unsigned length = next() % 96; uint8_t *x = malloc(length ? length : 1);
            for (unsigned at = 0; at < length; at++) x[at] = (uint8_t)next();
            if (length > 13 && (next() & 1)) x[12] = 8, x[13] = next() & 1 ? 0 : 6;
            if (length > 14 && (next() & 1)) x[14] = 0x45;
            if (length > 23 && (next() & 1)) x[23] = next() & 1 ? 17 : 6;
            assert(!fa_tx_filtered(0, x, length));
            for (unsigned mask = 1; mask <= EVERY; mask++) {
                bool taken = fa_tx_filtered(mask, x, length);
                if (taken) assert(fa_tx_filtered(EVERY, x, length));
                if (length >= 14 && x[12] == 8 && x[13] == 6) assert(!taken);
            }
            free(x);
        }
    }
    /* ARP and the echo. */
    assert(fa_arp_request(f, 27, OWN, ME, THEM) == 0 && fa_arp_request(f, 28, OWN, ME, THEM) == 28 && f[7] == 1 && !memcmp(f + 8, OWN, 6) && !memcmp(f + 14, ME, 4) && !memcmp(f + 24, THEM, 4));
    { uint8_t a[28]; memcpy(a, "\0\1\10\0\6\4\0\2", 8); memcpy(a + 8, SERVER, 6); memcpy(a + 14, THEM, 4); memcpy(a + 18, OWN, 6); memcpy(a + 24, ME, 4);
      assert(fa_net_wanted(FA_TYPE_ARP, a, 28) && fa_arp_read(a, 28, ME, THEM, hw) && !memcmp(hw, SERVER, 6) && !fa_arp_read(a, 28, THEM, THEM, hw) && !fa_arp_read(a, 27, ME, THEM, hw));
      a[7] = 1; assert(!fa_net_wanted(FA_TYPE_ARP, a, 28) && !fa_arp_read(a, 28, ME, THEM, hw)); }
    n = fa_echo_request(f, sizeof(f), ME, THEM, 77, 3);
    assert(n == FA_ECHO_LENGTH && fa_ip_sum(f, 20) == 0 && fa_ip_sum(f + 20, n - 20) == 0 && f[20] == 8);
    assert(!fa_net_wanted(FA_TYPE_IP, f, n));	/* a request is not kept */
    /* The answer, as the other side turns it round. */
    memcpy(f + 12, THEM, 4); memcpy(f + 16, ME, 4); f[20] = 0; f[22] = f[23] = 0; fa_put16(f + 22, fa_ip_sum(f + 20, n - 20));
    assert(fa_net_wanted(FA_TYPE_IP, f, n) && fa_echo_read(f, n, THEM, ME, 77, 3) && !fa_echo_read(f, n, THEM, ME, 77, 4) && !fa_echo_read(f, n, ME, THEM, 77, 3));
    f[30] ^= 1; assert(!fa_echo_read(f, n, THEM, ME, 77, 3));
    /* Stage 8's table and numbers. */
    {
        static struct fa_heard table[FA_HEARD_MOST];
        unsigned count = 0;
        uint8_t b[300] = {0}, *e = b + 36;
        b[0] = FA_KIND_BEACON; memset(b + 4, 0xff, 6); memcpy(b + 10, AP, 6); memcpy(b + 16, AP, 6); b[32] = 100; b[34] = 0x11;
        e[0] = 0; e[1] = 4; memcpy(e + 2, "name", 4); e[6] = 48; e[7] = 2; e[8] = 1; e[9] = 0;
        unsigned length = 36 + 10;
        assert(fa_heard_take(table, &count, b, length, 2412, -50, 5, NULL, 0) && count == 1 && table[0].rssi == -50 && table[0].elements_length == 10 && table[0].interval == 100 && table[0].capability == 0x11 && table[0].heard_at == 5);
        assert(fa_heard_take(table, &count, b, length, 2437, -40, 6, NULL, 0) && count == 1 && table[0].frequency == 2437 && table[0].rssi == -40);
        assert(fa_heard_take(table, &count, b, length, 2412, -70, 7, NULL, 0) && count == 1 && table[0].frequency == 2437 && table[0].heard_at == 7);
        assert(fa_heard_take(table, &count, b, length, 2412, -128, 8, NULL, 0) && table[0].rssi == -40);
        assert(!fa_heard_take(table, &count, b, length, 2412, -60, 9, (const uint8_t *)"nam", 3) && fa_heard_take(table, &count, b, length, 2412, -60, 9, (const uint8_t *)"name", 4));
        assert(!fa_heard_take(table, &count, b, length, 0, -60, 9, NULL, 0) && !fa_heard_take(table, &count, b, length, 0x10000, -60, 9, NULL, 0));
        b[1] = 0x40; assert(!fa_heard_take(table, &count, b, length, 2412, -60, 9, NULL, 0)); b[1] = 0;
        b[16] = 1; assert(!fa_heard_take(table, &count, b, length, 2412, -60, 9, NULL, 0)); b[16] = AP[0];
        e[1] = 33; assert(!fa_heard_take(table, &count, b, 36 + 2 + 33, 2412, -60, 9, NULL, 0)); e[1] = 4;
        /* A full table takes no new one, and refuses a name it has room for by another access point only when full. */
        count = 0;
        for (unsigned i = 0; i < FA_HEARD_MOST; i++) { b[16] = 2; b[17] = (uint8_t)i; assert(fa_heard_take(table, &count, b, length, 2412, -60, 9, NULL, 0)); }
        b[17] = 0; b[18] = 1; assert(count == FA_HEARD_MOST && !fa_heard_take(table, &count, b, length, 2412, -60, 9, NULL, 0)); b[18] = AP[2];
        /* More elements than are kept: cut at a whole one. */
        { static uint8_t big[2000]; memcpy(big, b, 36); uint8_t *q = big + 36; q[0] = 0; q[1] = 1; q[2] = 'x'; q += 3; for (unsigned i = 0; i < 9; i++) { q[0] = 221; q[1] = 200; q += 202; }
          count = 0; assert(fa_heard_take(table, &count, big, (unsigned)(q - big), 2412, -60, 9, NULL, 0) && table[0].elements_length == 3 + 202 * 5 && table[0].elements_length <= FA_IOS_ELEMENTS_MOST); }
        /* Every shorter length, each in memory of exactly its size. */
        b[16] = AP[0]; b[17] = AP[1];
        for (unsigned i = 0; i < length; i++) { uint8_t *x = malloc(i ? i : 1); memcpy(x, b, i); count = 0; assert(fa_heard_take(table, &count, x, i, 2412, -60, 9, NULL, 0) == (i >= 36 + 6)); free(x); }

        struct fa_group_pn g; memset(&g, 0, sizeof(g));
        uint8_t c[8] = {1, 0, 0, 0x20 | 1 << 6, 0, 0, 0, 0}, rsc[8] = {5, 4, 3, 2, 1, 7, 0xee, 0xee};
        assert(fa_rsc_read(rsc) == 0x070102030405ull);
        assert(fa_group_check(&g, c) == FA_GROUP_NO_KEY);
        g.known[1] = true; g.last[1] = 0;
        assert(fa_group_check(&g, c) == FA_GROUP_FRESH && g.last[1] == 1 && fa_group_check(&g, c) == FA_GROUP_REPLAY);
        c[3] = 1 << 6; c[0] = 9; assert(fa_group_check(&g, c) == FA_GROUP_NO_KEY);	/* no Extended IV bit: this is no CCMP header */
        c[3] = 0x20 | 2 << 6; assert(fa_group_check(&g, c) == FA_GROUP_NO_KEY); c[3] = 0x20 | 1 << 6;
        c[0] = 0; c[1] = 0; c[4] = 0; c[5] = 0; c[6] = 0; c[7] = 1; assert(fa_group_check(&g, c) == FA_GROUP_FRESH && g.last[1] == 0x10000000000ull);
        c[7] = 0; c[6] = 0xff; assert(fa_group_check(&g, c) == FA_GROUP_REPLAY);
    }
    /* Answering, for stage 7's load test. */
    {
        uint8_t q[28], a[28], out[1600];
        memcpy(q, "\0\1\10\0\6\4\0\1", 8); memcpy(q + 8, SERVER, 6); memcpy(q + 14, THEM, 4); memset(q + 18, 0, 6); memcpy(q + 24, ME, 4);
        assert(fa_arp_asks_for(q, 28, ME) && !fa_arp_asks_for(q, 27, ME) && !fa_arp_asks_for(q, 28, THEM));
        assert(fa_net_served(FA_TYPE_ARP, q, 28, ME) && !fa_net_served(FA_TYPE_ARP, q, 28, THEM) && !fa_net_wanted(FA_TYPE_ARP, q, 28));
        assert(fa_arp_reply(a, 27, OWN, ME, q, 28) == 0 && fa_arp_reply(a, 28, OWN, THEM, q, 28) == 0 && fa_arp_reply(a, 28, OWN, ME, q, 27) == 0);
        assert(fa_arp_reply(a, 28, OWN, ME, q, 28) == 28);
        assert(!memcmp(a, "\0\1\10\0\6\4\0\2", 8) && !memcmp(a + 8, OWN, 6) && !memcmp(a + 14, ME, 4) && !memcmp(a + 18, SERVER, 6) && !memcmp(a + 24, THEM, 4));
        q[7] = 2; assert(!fa_arp_asks_for(q, 28, ME) && fa_arp_reply(a, 28, OWN, ME, q, 28) == 0); q[7] = 1;
        q[1] = 6; assert(!fa_arp_asks_for(q, 28, ME)); q[1] = 1;
        unsigned e = fa_echo_request(f, sizeof(f), THEM, ME, 0x1234, 7), r = fa_echo_reply(out, sizeof(out), ME, f, e);
        assert(e == FA_ECHO_LENGTH && r == e && fa_net_served(FA_TYPE_IP, f, e, ME) && !fa_net_served(FA_TYPE_IP, f, e, THEM));
        assert(fa_ip_sum(out, 20) == 0 && out[9] == 1 && !memcmp(out + 12, ME, 4) && !memcmp(out + 16, THEM, 4) && out[20] == 0 && out[21] == 0 && fa_ip_sum(out + 20, r - 20) == 0);
        assert(fa_be16(out + 24) == 0x1234 && fa_be16(out + 26) == 7 && !memcmp(out + 28, f + 28, FA_ECHO_DATA) && fa_echo_read(out, r, ME, THEM, 0x1234, 7));
        assert(fa_echo_reply(out, r - 1, ME, f, e) == 0 && fa_echo_reply(out, 10, ME, f, e) == 0 && fa_echo_reply(out, sizeof(out), THEM, f, e) == 0);
        /* A wrong ICMP checksum, or an answer, is not asked of us. */
        f[30] ^= 1; assert(fa_echo_reply(out, sizeof(out), ME, f, e) == 0 && !fa_net_served(FA_TYPE_IP, f, e, ME)); f[30] ^= 1;
        f[20] = 0; assert(fa_echo_reply(out, sizeof(out), ME, f, e) == 0); f[20] = 8;
        /* Larger than a type's limit, another type, and every shorter length in memory of exactly its size. */
        assert(!fa_net_served(FA_TYPE_ARP, q, FA_NET_MOST + 1, ME) && !fa_net_served(0x86dd, q, 28, ME));
        for (unsigned i = 0; i < e; i++) {
            uint8_t *x = malloc(i ? i : 1); memcpy(x, f, i);
            assert(fa_echo_reply(out, sizeof(out), ME, x, i) == 0 && !fa_net_served(FA_TYPE_IP, x, i, ME));
            free(x);
        }
        for (unsigned i = 0; i < 28; i++) {
            uint8_t *x = malloc(i ? i : 1); memcpy(x, q, i);
            assert(fa_arp_reply(a, 28, OWN, ME, x, i) == 0 && !fa_net_served(FA_TYPE_ARP, x, i, ME));
            free(x);
        }
        for (unsigned round = 0; round < 40000; round++) {
            unsigned size = next() % 200; uint8_t *x = malloc(size ? size : 1);
            memcpy(x, f, size < e ? size : e);
            for (unsigned k = next() % 6; k > 0 && size > 0; k--) x[next() % size] = (uint8_t)next();
            if (size > 20 && (next() & 1)) { x[10] = x[11] = 0; fa_put16(x + 10, fa_ip_sum(x, 20)); }
            unsigned g = fa_echo_reply(out, sizeof(out), ME, x, size);
            assert(g == 0 || (g >= 28 && g <= size && fa_ip_sum(out, 20) == 0 && fa_ip_sum(out + 20, g - 20) == 0));
            (void)fa_net_served(FA_TYPE_IP, x, size, ME); (void)fa_arp_reply(a, 28, OWN, ME, x, size);
            free(x);
        }
    }
    /* Frames. */
    n = fa_data_frame(f, sizeof(f), OWN, AP, SERVER, true, FA_TYPE_ARP, (const uint8_t *)"abcd", 4);
    assert(n == 26 + 8 + 4 && f[0] == 0x88 && f[1] == 0x41 && !memcmp(f + 4, AP, 6) && !memcmp(f + 10, OWN, 6) && !memcmp(f + 16, SERVER, 6) && f[32] == 8 && f[33] == 6);
    assert(fa_data_frame(f, n - 1, OWN, AP, SERVER, true, FA_TYPE_ARP, (const uint8_t *)"abcd", 4) == 0);
    /* The same seen from the other side: a frame for us, for everybody, for another, our own heard back, from another access point. */
    memcpy(f + 4, OWN, 6); memcpy(f + 10, AP, 6); memcpy(f + 16, SERVER, 6); f[1] = 0x42;
    assert(fa_data_read(f, n, false, true, OWN, AP, &d) && d.type == FA_TYPE_ARP && d.length == 4 && !memcmp(d.from, SERVER, 6));
    assert(!fa_data_read(f, n, false, false, OWN, AP, &d));	/* the cipher's bytes would be taken for the LLC */
    f[4] = 0xff; assert(fa_data_read(f, n, false, true, OWN, AP, &d)); memcpy(f + 4, SERVER, 6); assert(!fa_data_read(f, n, false, true, OWN, AP, &d)); memcpy(f + 4, OWN, 6);
    memcpy(f + 16, OWN, 6); assert(!fa_data_read(f, n, false, true, OWN, AP, &d)); memcpy(f + 16, SERVER, 6);
    assert(!fa_data_read(f, n, false, true, OWN, SERVER, &d));
    f[0] = 0xc8; assert(!fa_data_read(f, n, false, true, OWN, AP, &d));
    /* Ethernet form. */
    memcpy(f, OWN, 6); memcpy(f + 6, SERVER, 6); f[12] = 8; f[13] = 0; memcpy(f + 14, "abcd", 4);
    assert(fa_data_read(f, 18, true, false, OWN, AP, &d) && d.type == FA_TYPE_IP && d.length == 4 && !fa_data_read(f, 13, true, false, OWN, AP, &d));
    puts("fa_net: DHCP, ARP, echo and data frames, by the RFC's bytes and on malformed input");
    return 0;
}
"""


@check
def net_frames_on_malformed_input(tmp):
    """src/fa_net.h under the sanitizers: what stage 7 builds and reads. Design M7."""
    own = ROOT / 'xnu/fuxi-ath12k/src'
    (tmp / 'net.c').write_text(FA_NET_HOST)
    cc = os.environ.get('CC', 'cc')
    program = tmp / 'net'
    subprocess.run([cc, '-std=gnu11', '-Wall', '-Wextra', '-Wno-unused-function', '-g', '-O1', '-fsanitize=address,undefined', '-fno-sanitize-recover=all', f'-I{own}',
                    str(tmp / 'net.c'), '-o', str(program)], check=True)
    subprocess.run([str(program)], check=True)


BRIDGE_DRIVER = r"""
#include <assert.h>
#include <stdio.h>
#include "fuxi_ath12k_ios.c"
static unsigned char seen_name[32], seen_key[32], seen_bssid[6]; static unsigned seen_length, joins; static bool key_given, bssid_given;
static int answer; static const struct fa_ios_input *driver_input; static unsigned attached, lost_errno, lost_count, rx_length, link_ups, pmks;
void fa_ios_start(const struct fa_ios_input *i) { driver_input = i; attached++; }
int fa_ios_power(int on) { return answer; }
int fa_ios_scan(const unsigned char *s, unsigned l, const unsigned short *f, unsigned c, int p) { assert(l == 4 && !memcmp(s, "name", 4) && c == 1 && f[0] == 2412 && p); return answer; }
void fa_ios_heard(void (*each)(const struct fa_heard *, void *), void *c) { static struct fa_heard h; memcpy(h.bssid, "abcdef", 6); h.ssid_length = 4; memcpy(h.ssid, "name", 4); h.frequency = 2412; h.rssi = -40; h.interval = 100; h.capability = 0x11; h.age_ms = 9; h.elements_length = 1024; memset(h.elements, 0x77, 1024); each(&h, c); }
int fa_ios_join(const unsigned char *s, unsigned l, const unsigned char *b, const unsigned char *k) { joins++; memcpy(seen_name, s, l); seen_length = l; bssid_given = b != NULL; if (b) memcpy(seen_bssid, b, 6); key_given = k != NULL; if (k) memcpy(seen_key, k, 32); return answer; }
int fa_ios_leave(void) { return answer; }
void fa_ios_state(struct fa_ios_state *s) { memset(s, 0, sizeof(*s)); s->phase = FA_IOS_JOINED; s->protect = 1; s->last_answer = answer; s->bss.frequency = 2412; s->bss.ssid_length = 4; memcpy(s->bss.ssid, "name", 4); s->bss.elements_length = 3; }
int fa_ios_tx(const unsigned char *f, unsigned long n) { return n == 60; }
void fa_ios_note_request(int number, int value) {}
void fuxi_ath12k_station_address(unsigned char *m) { memcpy(m, "\x02\x46\x55\x58\x49\x01", 6); }
void fuxi_wifi_wpa_pmk(const unsigned char *p, unsigned pl, const unsigned char *n, unsigned nl, unsigned char k[32]) { assert(pl == 13 && !memcmp(p, "the test word", 13) && nl == 4 && !memcmp(n, "name", 4)); memset(k, 0x33, 32); pmks++; }
void fuxi_wifi_sta_lost(int e) { lost_errno = e; lost_count++; }
static unsigned alone_links;
void fuxi_wifi_apple80211_event(enum fuxi_wifi_sta_event event, int status) { assert(event == FUXI_WIFI_STA_LINK && status == 1); alone_links++; }
static void in_rx(const uint8_t *f, size_t n) { rx_length = n; }
static void in_link(bool up) { link_ups += up; }
static const struct fuxi_wifi_input IN = { in_rx, in_link };
static unsigned heard_count; static void each_heard(const struct fuxi_wifi_sta_bss *b, void *c) { assert(c == &IN && b->ssid_length == 4 && !memcmp(b->ssid, "name", 4) && !memcmp(b->bssid, "abcdef", 6) && b->frequency == 2412 && b->rssi == -40 && b->beacon_interval == 100 && b->capability == 0x11 && b->age_ms == 9 && b->elements_length == 1024 && b->elements[1023] == 0x77); heard_count++; }
int main(void) {
    const struct fuxi_wifi_backend *b = fuxi_ath12k_ios_backend();
    static const struct { int from, to; } maps[] = { {FA_IOS_DONE, 0}, {FA_IOS_NO_AUTH, EAUTH}, {FA_IOS_NO_ANSWER, ETIMEDOUT}, {FA_IOS_NOT_HEARD, ENOENT}, {FA_IOS_REFUSED, ECONNREFUSED}, {FA_IOS_NOT_BUILT, EOPNOTSUPP}, {FA_IOS_NO_MEMORY, ENOMEM}, {FA_IOS_GONE, ECONNRESET}, {FA_IOS_RADIO_DEAD, ENETDOWN}, {FA_IOS_FAILED, EIO} };
    static struct fuxi_wifi_sta_join join; static struct fuxi_wifi_sta_scan scan; static struct fuxi_wifi_sta_state state; static struct fuxi_wifi_sta_channel ch[96];
    assert(!memcmp(b->station.address, "\x02\x46\x55\x58\x49\x01", 6) && b->tx_frame && b->start && !attached);
    b->start(&IN); assert(attached == 1 && driver_input);
    for (unsigned i = 0; i < sizeof(maps) / sizeof(maps[0]); i++) {
        answer = maps[i].from;
        memcpy(join.ssid, "name", 4); join.ssid_length = 4; join.security = FUXI_WIFI_STA_OPEN;
        assert(b->station.power(true) == maps[i].to && b->station.leave() == maps[i].to && b->station.join(&join) == maps[i].to);
        memcpy(scan.ssid, "name", 4); scan.ssid_length = 4; scan.frequencies[0] = 2412; scan.frequency_count = 1; scan.passive = true;
        assert(b->station.scan(&scan) == maps[i].to);
        driver_input->lost(answer); assert(lost_errno == (unsigned)maps[i].to);
        b->station.state(&state); assert(state.last_error == maps[i].to);
    }
    answer = FA_IOS_DONE;
    /* Open: no key crosses. A passphrase: only the key made of it. A key as it is. Another kind: nothing asked. */
    assert(!key_given && !bssid_given && seen_length == 4 && !memcmp(seen_name, "name", 4));
    join.security = FUXI_WIFI_STA_WPA2_PSK; memcpy(join.passphrase, "the test word", 13); join.passphrase_length = 13;
    assert(b->station.join(&join) == 0 && key_given && seen_key[0] == 0x33 && seen_key[31] == 0x33 && pmks == 1);
    join.passphrase_length = 0; memset(join.passphrase, 0, sizeof(join.passphrase)); memset(join.pmk, 0x44, 32); memcpy(join.bssid, "abcdef", 6);
    assert(b->station.join(&join) == 0 && seen_key[0] == 0x44 && pmks == 1 && bssid_given && !memcmp(seen_bssid, "abcdef", 6));
    { unsigned before = joins; join.security = 7; assert(b->station.join(&join) == EOPNOTSUPP && joins == before); }
    b->station.heard(each_heard, (void *)&IN); assert(heard_count == 1);
    b->station.state(&state); assert(state.phase == FUXI_WIFI_STA_JOINED && state.security == FUXI_WIFI_STA_WPA2_PSK && state.bss.frequency == 2412 && state.bss.ssid_length == 4 && state.bss.elements_length == 3);
    unsigned n = b->station.channels(ch, 96); assert(n == 26 && ch[0].frequency == 2412 && ch[25].frequency == 5825 && (ch[17].flags & FUXI_WIFI_STA_CHANNEL_RADAR));
    assert(b->station.channels(ch, 3) == 3);
    assert(b->tx_frame((const uint8_t *)"", 60) && !b->tx_frame((const uint8_t *)"", 59));
    driver_input->rx((const unsigned char *)"", 77); assert(rx_length == 77);
    driver_input->link(1); assert(link_ups == 1);
    puts("fuxi_ath12k_ios: answers, keys, the table, channels and frames across the two domains");
    return 0;
}
"""


@check
def ios_bridge_crosses_the_domains(tmp):
    """os/fuxi_ath12k_ios.c on stub headers, against a fake driver domain: its answers are XNU's errno, and a passphrase ends there. Design M8."""
    own = ROOT / 'xnu/fuxi-ath12k'
    ios = ROOT / 'xnu/fuxi-wifi-ios'
    stubs = tmp / 'bridge'
    (stubs / 'sys').mkdir(parents=True)
    (stubs / 'sys/systm.h').write_text('#pragma once\n#include <stdbool.h>\n#include <stdint.h>\n#include <stddef.h>\n#include <string.h>\n#include <strings.h>\n#include <errno.h>\n#ifndef EAUTH\n#define EAUTH 80\n#endif\n')
    (stubs / 'sys/fuxi_wifi_ios.h').write_text(f'#pragma once\n#define KERNEL 1\n#include "{ios / "fuxi_wifi_ios.h"}"\n#undef KERNEL\n')
    (stubs / 'sys/fuxi_wifi_sta.h').write_text(f'#pragma once\n#include "{ios / "fuxi_wifi_sta.h"}"\n')
    (stubs / 'sys/fuxi_wifi_wpa.h').write_text(f'#pragma once\n#include "{ios / "fuxi_wifi_wpa.h"}"\n')
    (stubs / 'driver.c').write_text(BRIDGE_DRIVER)
    program = stubs / 'driver'
    subprocess.run([os.environ.get('CC', 'cc'), '-std=gnu11', '-Wall', '-Werror', '-Wno-unused-function', '-g', '-fsanitize=address,undefined', '-fno-sanitize-recover=all', '-DFUXI',
                    f'-I{stubs}', f'-I{own / "src"}', f'-I{own / "os"}', str(stubs / 'driver.c'), '-o', str(program)], check=True)
    subprocess.run([str(program)], check=True)


@check
def auto_join_is_off_by_default_and_says_no_key(tmp):
    """fuxiwifiauto: stage 8 ignores the device tree without exactly 1, and iOS's word wins. No key in a line of the log."""
    wlan = (ROOT / 'xnu/fuxi-wlan/fuxi_wlan.c').read_text()
    block = wlan[wlan.index('static void\nwl_fa_join_configure(void)'):]
    block = block[:block.index('\n}\n')]
    gate = block[:block.index('SecureDTLookupEntry')]
    assert 'wl_fa_stage == 8' in gate and 'PE_parse_boot_argn("fuxiwifiauto"' in gate and '(wants_auto != 1 && wants_auto != 2)) { return; }' in gate
    assert block.count('fa_set_auto_join(') == 1 and wlan.count('fa_set_auto_join(') == 1
    assert wlan.count('"fuxiwifiauto"') == 1
    text = (ROOT / 'xnu/fuxi-ath12k/src/fa_80211.c').read_text()
    assert text.count('ios_auto_step(radio);') == 1 and text.index('ios_auto_step(radio);') > text.index('ios_runs(void *argument)')
    for word in ('fa_set_auto_join', 'auto_key', 'auto_name'):
        assert word not in text[:text.index('/* ---- stage 8: the radio for iOS') - 1].replace('static uint8_t auto_name[FA_SSID_MOST], auto_key[32];', '').replace('static unsigned auto_name_length;', ''), word
    for call in re.findall(r'lx_tell\(.*?\);', text, flags=re.S):
        assert 'auto_key' not in call and 'auto_name,' not in call and 'name,' not in call.replace('name, what', ''), call


@check
def stage_8_is_reached_from_stage_8_alone(tmp):
    """The stage that does not stop is a path of its own: stages 4 to 7 end as they did, and nothing else makes a radio stay."""
    own = ROOT / 'xnu/fuxi-ath12k/src'
    hif = (own / 'fa_hif.c').read_text()
    text = (own / 'fa_80211.c').read_text()
    wlan = (ROOT / 'xnu/fuxi-wlan/fuxi_wlan.c').read_text()
    up = hif[hif.index('\nfa_stage_up(struct ath12k_base *ab)'):hif.index('/* tools/ath12k-edits.json, ath12k-wmi-event-log')]
    assert up.index('hif.stage == FA_STAGE_IOS') < up.index('return (false);\t\t\t/* it stays') < up.index('fa_m5_station(') < up.index('fa_hif_quiesce();') < up.index('hif.stage_hit = true')
    ready = hif[hif.index('\nfa_firmware_ready('):hif.index('\nfa_stop(void)')]
    assert hif.count('fa_m8_up(') == 1 and ready.index('if (stage == FA_STAGE_IOS) {') < ready.index('fa_m8_up(')
    assert 'hif.ios_up = error == 0;' in ready and hif.count('hif.ios_up') == 2 and hif.count('fa_m8_firmware_gone()') == 1
    assert text.count('ios_radio = radio;') == 1 and text.index('ios_radio = radio;') > text.index('\nfa_m8_up(struct ieee80211_hw *hw)')
    assert text.count('radio->ios = true;') == 1 and text.index('radio->ios = true;') > text.index('\nfa_m8_up(struct ieee80211_hw *hw)')
    assert 'radio->ios_joined = true;' in text[text.index('\nios_join_run('):] and text.count('radio->ios_joined = true;') == 1
    # What was the firmware's end on stages 4 to 7 stays so: only stage 8 may be let off.
    for word in ('"hw_scan failed"', '"scan aborted by ath12k'):
        at = text.index(word)
        assert 'if (!radio->ios)' in text[at - 120:at]
    # fw_thread: stage 8 returns nowhere, and the stop below it is for all the others.
    handoff = wlan[wlan.index('\nwl_fa_handoff(void)'):]
    assert handoff.index('wl_fa_stage == 8') < handoff.index('for (;;) { wl_sleep(60000); }') < handoff.index('fa_stop();')
    # The hand-off's one, and the two that say stage 8 reads the device tree only when told to.
    configure = wlan[wlan.index('static void\nwl_fa_join_configure(void)'):]
    configure = configure[:configure.index('\n}\n')]
    assert configure.count('wl_fa_stage == 8') == 2 and wlan.count('wl_fa_stage == 8') == 3
    # Patch 0117 alone selects it.
    patch = (ROOT / 'xnu/patches/0117-fuxi-ath12k.patch').read_text()
    assert '+bsd/dev/fuxi_ath12k_ios.c\t\tstandard' in patch and '+\tif (on == 2 && !enabled) {' in patch and '+\t\tlink.name = "ath12k";' in patch
    for other in (ROOT / 'xnu/patches').glob('*.patch'):
        if other.name != '0117-fuxi-ath12k.patch':
            assert 'fuxi_ath12k_ios' not in other.read_text(errors='surrogateescape'), other.name
    assert 'on != 1 || enabled' in (ROOT / 'xnu/fuxi-wifi-ios/fuxi_wifi_ios.c').read_text() and 'ath12k' not in (ROOT / 'xnu/fuxi-wifi-ios/fuxi_wifi_ios.c').read_text()
    # A passphrase never reaches the driver domain.
    for path in (own / 'fa_80211.c', own / 'fa_ios.h', own / 'fa_xnu.h'):
        assert 'passphrase' not in code_only(path.read_text()).lower(), path.name

@check
def rx_ring_order_and_stale_buffers_are_edited_in(tmp):
    """w-ios1's panic: the edits that order every destination ring's head pointer, count what is stale and keep the warning."""
    edits = {e['id']: e for e in json.loads((ROOT / 'tools/ath12k-edits.json').read_text())['edits']}
    always = edits['ath12k-dst-ring-barrier-always']['replace']
    assert 'if (' not in always and always.index('cached_hp = hp') < always.index('dma_rmb()')
    assert edits['ath12k-dst-ring-barrier-sync']['replace'].endswith('dma_rmb();\n')
    for name in ('reo', 'err-buf', 'err-drop', 'wbm'):
        edit = edits['ath12k-rx-stale-' + name]
        assert edit['file'] == 'wifi7/dp_rx.c' and edit['replace'].index('fa_rx_stale(') < edit['replace'].index('desc_info->skb = NULL')
    done = edits['ath12k-rx-msdu-not-done-count']
    assert 'fa_rx_msdu_not_done(lrx_desc);' in done['replace'] and done['find'].strip() in done['replace']
    fresh = edits['ath12k-rx-buffer-fresh']
    assert fresh['file'] == 'dp_rx.c' and fresh['replace'].index('fa_rx_buffer_fresh(skb->data);') < fresh['replace'].index('dma_map_single')
    hif = (ROOT / 'xnu/fuxi-ath12k/src/fa_hif.c').read_text()
    not_done = hif[hif.index('fa_rx_msdu_not_done(const void *desc)'):]
    assert 'msdu_end.info14' in not_done[:300] and 'fa_rx_msdu_note(up);' in not_done[:600] and 'return (true)' not in not_done[:600]
    # No retry and nothing that hides the cause.
    for edit in edits.values():
        if edit['id'].startswith('ath12k-rx-'):
            assert 'goto try_again' not in edit['replace'] and 'udelay' not in edit['replace']


@check
def block_ack_is_stage_8_alone(tmp):
    """Stages 4 to 7 do not answer: the frame goes on as it did, and only the stage's own thread calls the driver."""
    text = (ROOT / 'xnu/fuxi-ath12k/src/fa_80211.c').read_text()
    assert "radio->ios && skb->data[0] == FA_KIND_ACTION" in text
    assert text.count('ops->ampdu_action') == 3
    assert text.index('ba_stop_all(radio);') < text.index('key_take_back(radio, &radio->group_key, false);')


@check
def busy_watch_is_in_patch_0117_alone(tmp):
    """The black screen's watch: only kernel-metal4wifi has it, it changes one count once, behind a check and an opt-out, and says no name of a network."""
    source = (ROOT / 'xnu/fuxi-wifi-ios/FuxiNetworkStack.cpp').read_text()
    assert 'M8 busy' not in source and 'fuxi_busy' not in source
    for patch in sorted((ROOT / 'xnu/patches').glob('*.patch')):
        if patch.name != '0117-fuxi-ath12k.patch':
            assert 'M8 busy' not in patch.read_text(errors='surrogateescape'), patch.name
    patch = (ROOT / 'xnu/patches/0117-fuxi-ath12k.patch').read_text()
    hunk = patch[patch.index('--- a/iokit/Kernel/arm/FuxiNetworkStack.cpp'):]
    added = '\n'.join(line[1:] for line in hunk.splitlines() if line.startswith('+') and not line.startswith('+++'))
    assert 'M8 busy: the root reached 0' in added and 'FUXI_BUSY_LINES_MOST    40' in added and 'seconds == 30' in added and 'fuxi_busy_watch();' in added
    assert added.count('adjustBusy') == 1 and 'root->adjustBusy(-1);' in added
    give_up = added[added.index('fuxi_busy_give_up(unsigned busy_under)'):added.index('root->adjustBusy(-1);')]
    assert 'allowed == 0' in give_up and 'busy_under != 0 || root->getBusyState() != 1' in give_up
    assert added.count('fuxi_busy_give_up(busy_under);') == 1 and 'if (seconds == 30) {' in added
    for word in ('setProperty', 'terminate', 'waitQuiet', 'ssid', 'address', 'SSID'):
        assert word not in added, word


@check
def tx_filter_is_stage_8s_alone_and_off_by_default(tmp):
    """fuxiath12kq's filter on what iOS sends: four free bits, none on unless asked, set on stage 8 alone, and past the refusals in fa_ios_tx() alone."""
    own = ROOT / 'xnu/fuxi-ath12k/src'
    hif = (own / 'fa_hif.c').read_text()
    text = (own / 'fa_80211.c').read_text()
    knobs = {name: int(value, 16) for name, value in re.findall(r'#define (FA_KNOB_\w+)\s+(0x[0-9a-f]+)u', hif)}
    assert len(knobs) == 10 and sum(knobs.values()) == 0x3ff and all(bin(value).count('1') == 1 for value in knobs.values())
    assert (knobs['FA_KNOB_TX_NO_IP6'], knobs['FA_KNOB_TX_NO_TCP'], knobs['FA_KNOB_TX_NO_UDP'], knobs['FA_KNOB_TX_NO_GROUP']) == (0x40, 0x80, 0x100, 0x200)
    assert hif.count('fa_m8_tx_filter(') == 1
    at = hif.index('fa_m8_tx_filter(')
    assert hif.rindex('if (stage == FA_STAGE_IOS) {', 0, at) > hif.index('hif.knobs = lx_os_option("fuxiath12kq");') and hif.rindex('}', 0, at) < hif.rindex('if (stage == FA_STAGE_IOS) {', 0, at)
    call = hif[at:hif.index(';', at)]
    assert call.index('NO_IP6') < call.index('NO_TCP') < call.index('NO_UDP') < call.index('NO_GROUP')
    assert 'static unsigned tx_filter;' in text and text.count('tx_filter =') == 1 and text.count('fa_m8_tx_filter(') == 1
    tx = text[text.index('\nfa_ios_tx('):text.index('/* Design section 2: recovery')]
    assert tx.index('type == 0x888e') < tx.index('fa_tx_filtered(tx_filter,') < tx.index('!radio->ios_joined') < tx.index('if (filtered) {') < tx.index('radio->ios_tx_busy++') < tx.index('fa_tx_note(') < tx.index('net_send(')
    gone = tx[tx.index('if (filtered) {'):tx.index('radio->ios_tx_busy++')]
    assert 'radio->tx_filtered++;' in gone and 'lx_os_unlock();' in gone and 'return (1);' in gone
    assert len(re.findall(r'\btx_filter\b', text)) == 3 and text.count('radio->tx_filtered = 0;') == 1
    assert text.index('memset(&radio->tx, 0,') < text.index('radio->tx_filtered = 0;') < text.index('memset(radio->tx_then, 0,')


@check
def load_test_is_stage_7s_alone(tmp):
    """station_serve() answers on the network: one caller, behind stage 7's own ask and the knob, and stage 8 has no way there."""
    own = ROOT / 'xnu/fuxi-ath12k/src'
    text = (own / 'fa_80211.c').read_text()
    assert text.count('station_serve(') == 2 and text.count('station_serve(radio);') == 1
    at = text.index('station_serve(radio);')
    assert text.rindex('\nfa_m5_station(', 0, at) > text.rindex('\nstation_address_back(', 0, at)
    assert 'if (target_serve && radio->leased) {' in text and text.count('target_serve = true;') == 1
    ios = text[text.index('/* ---- stage 8') if '/* ---- stage 8' in text else text.index('\nfa_m8_'):]
    assert 'station_serve' not in ios and 'fa_m7_serve' not in ios and 'target_serve' not in ios
    hif = (own / 'fa_hif.c').read_text()
    assert hif.count('fa_m7_serve();') == 1
    stage = hif[hif.index('if (hif.stage == FA_STAGE_ADDRESS) {'):]
    assert stage.index('fa_m7_serve();') < stage.index('}\n')
    assert '(hif.knobs & FA_KNOB_SERVE) != 0' in stage



FA_AKS_HOST = r"""
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "fuxi_aks_wrap.h"
/* In memory of exactly its size, each way, so that the sanitizer sees a step past the end. */
static unsigned wrap(const unsigned char *k, unsigned n, unsigned c, unsigned char **out, unsigned room) {
    unsigned char *in = malloc(n ? n : 1); memcpy(in, k, n);
    *out = malloc(room ? room : 1);
    unsigned r = fuxi_aks_wrap(in, n, c, *out, room);
    free(in);
    return r;
}
static unsigned unwrap(const unsigned char *w, unsigned n, unsigned c, unsigned char **out, unsigned room) {
    unsigned char *in = malloc(n ? n : 1); memcpy(in, w, n);
    *out = malloc(room ? room : 1);
    unsigned r = fuxi_aks_unwrap(in, n, c, *out, room);
    free(in);
    return r;
}
int main(void) {
    unsigned char key[FUXI_AKS_KEY_MOST + 1], *w, *k;
    for (unsigned i = 0; i < sizeof(key); i++) key[i] = (unsigned char)(i * 7 + 3);
    /* What securityd asks: 32 bytes in, room for 40, the class back. Layout by hand. */
    unsigned n = wrap(key, 32, 6, &w, 40);
    assert(n == 40 && !memcmp(w, "FXAK", 4) && w[4] == 6 && w[5] == 0 && w[6] == 0 && w[7] == 0);
    assert(w[8] == (key[0] ^ 0x5a) && w[9] == (unsigned char)(key[1] ^ (0x5a ^ 0x1f)));
    assert(memcmp(w + 8, key, 32) != 0);
    unsigned r = unwrap(w, 40, 6, &k, 32);
    assert(r == 32 && !memcmp(k, key, 32));
    free(k);
    /* Another class is refused, and so is a class that differs only high up; too little room; a changed mark. */
    assert(unwrap(w, 40, 7, &k, 32) == 0); free(k);
    assert(unwrap(w, 40, 6 | 1u << 24, &k, 32) == 0); free(k);
    assert(unwrap(w, 40, 6, &k, 31) == 0); free(k);
    w[0] ^= 1; assert(unwrap(w, 40, 6, &k, 32) == 0); free(k); w[0] ^= 1;
    /* Every length that is too short: never a key of no size, no read past the end. */
    for (unsigned i = 0; i <= 8; i++) { assert(unwrap(w, i, 6, &k, 32) == 0); free(k); }
    free(w);
    /* Every size that may be wrapped comes back, GSSCred's 64 too; each class. */
    for (unsigned size = 1; size <= FUXI_AKS_KEY_MOST; size++) {
        unsigned c = size * 0x01010101u;
        assert(wrap(key, size, c, &w, size + 8) == size + 8);
        assert(unwrap(w, size + 8, c, &k, size) == size && !memcmp(k, key, size));
        free(k); free(w);
        assert(wrap(key, size, c, &w, size + 7) == 0); free(w);
    }
    assert(wrap(key, 0, 1, &w, 64) == 0); free(w);
    assert(wrap(key, FUXI_AKS_KEY_MOST + 1, 1, &w, FUXI_AKS_WRAPPED_MOST + 8) == 0); free(w);
    /* Over the limit on the way back as well. */
    unsigned char big[FUXI_AKS_WRAPPED_MOST + 1] = "FXAK";
    assert(unwrap(big, sizeof(big), 0, &k, sizeof(big)) == 0); free(k);
    puts("fuxi_aks_wrap: layout, round trip at every size, refusals");
    return 0;
}
"""


@check
def keychain_wrap_round_trip_and_refusals(tmp):
    """os/fuxi_aks_wrap.h under the sanitizers, and it says what it is not. In patch 0117 alone."""
    own = ROOT / 'xnu/fuxi-ath12k/os'
    (tmp / 'aks.c').write_text(FA_AKS_HOST)
    cc = os.environ.get('CC', 'cc')
    program = tmp / 'aks'
    subprocess.run([cc, '-std=gnu11', '-Wall', '-Wextra', '-g', '-O1', '-fsanitize=address,undefined', '-fno-sanitize-recover=all', f'-I{own}',
                    str(tmp / 'aks.c'), '-o', str(program)], check=True)
    subprocess.run([str(program)], check=True)
    assert 'NOT ENCRYPTION' in (own / 'fuxi_aks_wrap.h').read_text()
    for patch in sorted((ROOT / 'xnu/patches').glob('*.patch')):
        if patch.name != '0117-fuxi-ath12k.patch':
            assert 'sweet_aks_crypt' not in patch.read_text(errors='surrogateescape'), patch.name
    patch = (ROOT / 'xnu/patches/0117-fuxi-ath12k.patch').read_text()
    hunk = patch[patch.index('--- a/iokit/Kernel/arm/SweetKeyStore.cpp'):]
    assert '+\tcase kAKSWrapKey:' in hunk and '+\tcase kAKSUnwrapKey:' in hunk and '+#include "fuxi_aks_wrap.h"' in hunk
    added = '\n'.join(line[1:] for line in hunk.splitlines() if line.startswith('+') and not line.startswith('+++'))
    assert 'IOLog' not in added and 'bzero(out' in added and 'bzero(in' in added


@check
def peer_id_zero_is_a_peer(tmp):
    """w-ios5: the lookups that refused id 0, and the line that shows what the firmware gave. Numbers only."""
    edits = {e['id']: e for e in json.loads((ROOT / 'tools/ath12k-edits.json').read_text())['edits']}
    assert '!peer_id' in edits['ath12k-peer-id-zero']['find'] and '!peer_id' not in edits['ath12k-peer-id-zero']['replace']
    assert 'ATH12K_DP_PEER_ID_INVALID' in edits['ath12k-peer-id-zero']['replace']
    assert 'if (rxcb->peer_id)' in edits['ath12k-rx-peer-id-zero']['find'] and 'if (' not in edits['ath12k-rx-peer-id-zero']['replace']
    log = edits['ath12k-peer-map-log']['replace']
    assert log.index('fa_peer_mapped(') < log.index('spin_lock_bh') and 'mac_addr)' in log and 'mac_addr, ' not in log[:log.index('spin_lock_bh')]
    text = (ROOT / 'xnu/fuxi-ath12k/src/fa_hif.c').read_text()
    body = text[text.index('fa_peer_mapped(u32 vdev'):]
    assert 'said < 64' in body[:body.index('\n}\n')] and 'mac' not in body[:body.index('\n}\n')].lower()



FUXI_REMOTE_HOST = r"""
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include "fuxi_remote.h"
static struct fuxi_remote_cmd make(uint32_t seq, uint32_t kind, int x0, int y0, int x1, int y1, uint32_t ms) {
    struct fuxi_remote_cmd c = { seq, kind, x0, y0, x1, y1, ms, 0 };
    c.check = fuxi_remote_check(&c);
    return c;
}
int main(void) {
    /* two cache lines, the monitor's beginning on the second */
    assert(sizeof(struct fuxi_remote_cmd) == 32 && offsetof(struct fuxi_remote_box, cmd) == 64 && sizeof(struct fuxi_remote_box) == 128);
    struct fuxi_remote_cmd c = make(1, FUXI_REMOTE_TAP, 0, 0, 0, 0, 0);
    assert(fuxi_remote_valid(&c));
    c = make(1, FUXI_REMOTE_TAP, 1079, 2399, 0, 0, 0); assert(fuxi_remote_valid(&c));
    /* bounds */
    c = make(1, FUXI_REMOTE_TAP, 1080, 0, 0, 0, 0); assert(!fuxi_remote_valid(&c));
    c = make(1, FUXI_REMOTE_TAP, 0, 2400, 0, 0, 0); assert(!fuxi_remote_valid(&c));
    c = make(1, FUXI_REMOTE_TAP, -1, 0, 0, 0, 0); assert(!fuxi_remote_valid(&c));
    c = make(1, FUXI_REMOTE_TAP, 0, -1, 0, 0, 0); assert(!fuxi_remote_valid(&c));
    c = make(2, FUXI_REMOTE_FLICK, 540, 2390, 540, 1100, 250); assert(fuxi_remote_valid(&c));
    c = make(2, FUXI_REMOTE_FLICK, 540, 2390, 1080, 1100, 250); assert(!fuxi_remote_valid(&c));
    c = make(2, FUXI_REMOTE_FLICK, 540, 2390, 540, 2400, 250); assert(!fuxi_remote_valid(&c));
    c = make(2, FUXI_REMOTE_FLICK, 540, 2390, 540, 1100, 19); assert(!fuxi_remote_valid(&c));
    c = make(2, FUXI_REMOTE_FLICK, 540, 2390, 540, 1100, 5001); assert(!fuxi_remote_valid(&c));
    c = make(3, FUXI_REMOTE_KEY, 0, 0, 0, 0, 120); assert(fuxi_remote_valid(&c));
    c = make(3, FUXI_REMOTE_KEY, 2, 0, 0, 0, 2000); assert(fuxi_remote_valid(&c));
    c = make(3, FUXI_REMOTE_KEY, 3, 0, 0, 0, 120); assert(!fuxi_remote_valid(&c));
    c = make(3, FUXI_REMOTE_KEY, -1, 0, 0, 0, 120); assert(!fuxi_remote_valid(&c));
    c = make(3, FUXI_REMOTE_KEY, 0, 0, 0, 0, 2001); assert(!fuxi_remote_valid(&c));
    /* nothing but the three kinds exists, seq 0 means nothing, and any changed field shows */
    for (uint32_t kind = 0; kind < 256; kind++) {
        c = make(1, kind, 1, 1, 1, 1, 100);
        assert(fuxi_remote_valid(&c) == (kind >= 1 && kind <= 3));
    }
    c = make(0, FUXI_REMOTE_TAP, 1, 1, 0, 0, 0); assert(!fuxi_remote_valid(&c));
    c = make(1, FUXI_REMOTE_TAP, 1, 1, 0, 0, 0); c.x0 = 2; assert(!fuxi_remote_valid(&c));
    c = make(1, FUXI_REMOTE_TAP, 1, 1, 0, 0, 0); c.y0 = 2; assert(!fuxi_remote_valid(&c));
    c = make(1, FUXI_REMOTE_FLICK, 1, 1, 2, 2, 100); c.ms = 101; assert(!fuxi_remote_valid(&c));
    c = make(1, FUXI_REMOTE_FLICK, 1, 1, 2, 2, 100); c.x1 = 3; assert(!fuxi_remote_valid(&c));
    c = make(1, FUXI_REMOTE_TAP, 1, 1, 0, 0, 0); c.kind = FUXI_REMOTE_KEY; assert(!fuxi_remote_valid(&c));
    /* the monitor's question: off by default, one at a time, in order */
    c = make(1, FUXI_REMOTE_TAP, 1, 1, 0, 0, 0);
    assert(fuxi_remote_may_post(0, 0, 0, 0, &c) == FUXI_REMOTE_OFF);
    assert(fuxi_remote_may_post(FUXI_REMOTE_MAGIC, 2, 0, 0, &c) == FUXI_REMOTE_OFF);
    assert(fuxi_remote_may_post(FUXI_REMOTE_MAGIC, 1, 0, 0, &c) == FUXI_REMOTE_OK);
    assert(fuxi_remote_may_post(FUXI_REMOTE_MAGIC, 1, 0, 1, &c) == FUXI_REMOTE_BUSY);
    assert(fuxi_remote_may_post(FUXI_REMOTE_MAGIC, 1, 1, 1, &c) == FUXI_REMOTE_OUT_OF_ORDER);
    c = make(2, FUXI_REMOTE_TAP, 1, 1, 0, 0, 0);
    assert(fuxi_remote_may_post(FUXI_REMOTE_MAGIC, 1, 1, 1, &c) == FUXI_REMOTE_OK);
    assert(fuxi_remote_may_post(FUXI_REMOTE_MAGIC, 1, 0, 0, &c) == FUXI_REMOTE_OUT_OF_ORDER);
    c = make(2, FUXI_REMOTE_TAP, 1080, 1, 0, 0, 0);
    assert(fuxi_remote_may_post(FUXI_REMOTE_MAGIC, 1, 1, 1, &c) == FUXI_REMOTE_BAD);
    /* the number wraps with the done count */
    c = make(0, FUXI_REMOTE_TAP, 1, 1, 0, 0, 0);
    assert(fuxi_remote_may_post(FUXI_REMOTE_MAGIC, 1, 0xffffffffu, 0xffffffffu, &c) == FUXI_REMOTE_BAD);
    c = make(7, FUXI_REMOTE_FLICK, 540, 2390, 12, 1100, 250); printf("check %u\n", c.check);
    puts("fuxi_remote: fields, bounds, off by default, one at a time, in order");
    return 0;
}
"""


@check
def remote_commands_are_bounded(tmp):
    """include fuxi_remote.h under the sanitizers: what both ends check before acting."""
    (tmp / 'remote.c').write_text(FUXI_REMOTE_HOST)
    cc = os.environ.get('CC', 'cc')
    program = tmp / 'remote'
    subprocess.run([cc, '-std=gnu11', '-Wall', '-Wextra', '-g', '-O1', '-fsanitize=address,undefined', '-fno-sanitize-recover=all',
                    f'-I{ROOT / "xnu/fuxi-ath12k/os"}', str(tmp / 'remote.c'), '-o', str(program)], check=True)
    said = subprocess.run([str(program)], check=True, capture_output=True, text=True).stdout
    # The monitor's own sum, read from its source, against the header's.
    monitor = (ROOT / 'tools/fuxi-xnu-monitor.py').read_text()
    formula = re.search(r'^\s*check = (.+)$', monitor, flags=re.M)[1]
    values = dict(seq=7, kind=2, x0=540, y0=2390, x1=12, y1=1100, ms=250)
    assert f'check {eval(formula, {}, values)}' in said


@check
def remote_control_stays_in_its_place(tmp):
    """Only kernel-metal4wifi has it, it starts only with fuxiremote=1, and no way to send text exists on any side."""
    keys = (ROOT / 'xnu/fuxi-h1/fuxi_keys.c').read_text()
    assert 'fuxi_remote' not in keys and 'fuxiremote' not in keys
    for patch in sorted((ROOT / 'xnu/patches').glob('*.patch')):
        if patch.name != '0117-fuxi-ath12k.patch':
            text = patch.read_text(errors='surrogateescape')
            assert 'fuxiremote' not in text and 'fuxi_remote' not in text, patch.name
    text = (ROOT / 'xnu/patches/0117-fuxi-ath12k.patch').read_text(errors='surrogateescape')
    hunk = text[text.index('--- a/bsd/dev/fuxi_keys.c'):]
    hunk = hunk[:hunk.index('\n--- a/', 10)]
    added = '\n'.join(line[1:] for line in hunk.splitlines() if line.startswith('+') and not line.startswith('+++'))
    assert 'PE_parse_boot_argn("fuxiremote", &remote, sizeof(remote)) && remote == 1' in added
    start = added.index('unsigned remote = 0;')
    assert added.count('kernel_thread_start(fk_remote') == 1 and added.index('kernel_thread_start(fk_remote') > start
    assert 'fuxi_remote_valid(&cmd)' in added and 'FUXI REMOTE: tap %d %d seq %u' in added
    # What the magic hangs on: the thread writes it, so that off leaves zero and the monitor refuses.
    assert added.index('fuxi_remote_box.magic = FUXI_REMOTE_MAGIC') > added.index('fk_remote(void *arg')
    # Delegate: only after the handoff, only the checked command, and the number last.
    probe = (ROOT / 'shim/fuxi-probe/probe.c').read_text(errors='surrogateescape')
    assert "req == 0x5c && length == sizeof(struct fuxi_remote_cmd) && monitor_report[MON_LAUNCH]" in probe
    body = probe[probe.index('remote_post(uint64_t off'):]
    body = body[:body.index('\n}\n')]
    assert '(off & 63) != 0' in body and 'fuxi_remote_may_post(' in body and body.index('box->cmd.check') < body.index('box->cmd.seq = cmd.seq')
    assert 'box->magic =' not in body and 'box->done =' not in body
    # Nothing that could carry text, in the mailbox or the tools.
    header = code_only((ROOT / 'xnu/fuxi-ath12k/os/fuxi_remote.h').read_text()).lower()
    monitor = (ROOT / 'tools/fuxi-xnu-monitor.py').read_text()
    for word in ('char ', 'text', 'string', 'keyboard', 'type_'):
        assert word not in header, word
    assert "'--remote'" in monitor and "if args.remote and status['stage'] >= 8:" in monitor
    act = monitor[monitor.index('def remote_act(req):'):monitor.index('def remote_tick():')]
    assert re.findall(r"op == '(\w+)'", act) == ['see', 'tap', 'flick', 'key']

FK_DENSE_HOST = r"""
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
typedef int wait_result_t;
#define THREAD_CONTINUE_NULL ((void *)0)
struct fuxi_key_inject { unsigned key, kind, ms; };
#define FUXI_KEY_INJECT_CLICK 3u
static int sweet_fb_display_off = 1;
static unsigned fk_human_loops = 10;
static unsigned long now_ms;
static void fuxi_perf_input(void) {}
static void sweet_hid_touch(int finger, int down, int x, int y) { printf("touch %lu %d %d %d %d\n", now_ms, finger, down, x, y); }
static void fk_script_sleep(unsigned ms) { printf("sleep %u\n", ms); now_ms += ms; }
static int fk_inject(const struct fuxi_key_inject *cmd) { printf("key %u\n", cmd->kind); sweet_fb_display_off = 0; return 0; }
static void *current_thread(void) { return NULL; }
static int thread_terminate(void *thread) { (void)thread; return 0; }
static void thread_block(void *next) { (void)next; printf("end %lu\n", now_ms); exit(0); }
FK_TAP
FK_DENSE
int main(void) { fk_human_dense((void *)(uintptr_t)60, 0); return 1; }
"""


@check
def dense_touches_are_dense(tmp):
    """Run fuxihumanm=1's own code on a clock of its sleeps, and read what the screen would be told."""
    spec = importlib.util.spec_from_file_location('sync_ath12k', ROOT / 'tools/sync-fuxi-ath12k.py')
    sync = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sync)
    keys = (ROOT / 'xnu/fuxi-h1/fuxi_keys.c').read_text()
    tap = keys[keys.index('static void\nfk_tap(int x, int y)'):keys.index('static void\nfk_tour_shot(')]
    (tmp / 'dense.c').write_text(FK_DENSE_HOST.replace('FK_TAP', tap).replace('FK_DENSE', sync.KEYS_DENSE))
    cc = os.environ.get('CC', 'cc')
    program = tmp / 'dense'
    subprocess.run([cc, '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function', '-g', '-O1', '-fsanitize=address,undefined',
                    '-fno-sanitize-recover=all', str(tmp / 'dense.c'), '-o', str(program)], check=True)
    said = subprocess.run([str(program)], check=True, capture_output=True, text=True).stdout.splitlines()
    assert said.count('key 3') == 1 and sum(line.startswith('FUXI HUMAN: loop') and line.endswith('start') for line in said) == 10
    # One wait of the start and one wake, and after them no sleep a person would call a pause.
    sleeps = sorted(int(line.split()[1]) for line in said if line.startswith('sleep '))
    assert sleeps[-2:] == [1500, 60000] and sleeps[-3] <= 600, sleeps[-3:]
    contacts, loops, loop = [], [], None
    for line in said:
        if line.endswith(' start') and line.startswith('FUXI HUMAN: loop'):
            loop = []
            loops.append(loop)
        if not line.startswith('touch '):
            continue
        at, finger, down, x, y = map(int, line.split()[1:])
        assert finger == 0 and 0 <= x < 1080 and 0 <= y < 2400, line
        if down:
            if not contacts or contacts[-1]['up'] is not None:
                contacts.append(dict(start=at, points=[], up=None))
                loop.append(contacts[-1])
            contacts[-1]['points'].append((x, y))
        else:
            contacts[-1]['up'] = at
    # Long enough for a 300 s boot from 60 s, with fuxihumann left out: the clock here is the shortest the phone can take.
    assert int(said[-1].split()[1]) - 61500 >= 240000
    gaps = [b['start'] - a['up'] for a, b in zip(contacts, contacts[1:])]
    assert max(gaps) <= 600, max(gaps)
    assert sum(gap <= 100 for gap in gaps) / len(gaps) >= 0.85
    # Taps: only these, and each once a loop. Nothing lands on Phone or Messages.
    taps = [c['points'][0] for c in contacts if len(set(c['points'])) == 1]
    assert sorted(set(taps)) == [(148, 285), (148, 570), (540, 2247)] and all(taps.count(t) == 10 for t in set(taps))
    for c in contacts:
        for dock in ((280, 2247), (800, 2247)):
            assert abs(c['points'][0][0] - dock[0]) > 60 or abs(c['points'][0][1] - dock[1]) > 60, c
    # Let go at speed: a flick's last step is no slower than its average, or iOS has nothing to coast on. The switcher does slow.
    switcher = lambda c: c['points'][0] == (680, 2375)
    for c in contacts:
        if len(set(c['points'])) > 1 and not switcher(c):
            first, near, last = c['points'][0], c['points'][-2], c['points'][-1]
            whole = abs(last[0] - first[0]) + abs(last[1] - first[1])
            assert (abs(last[0] - near[0]) + abs(last[1] - near[1])) * (len(c['points']) - 1) >= whole * 0.8, c
    for loop in loops:
        assert sum(map(switcher, loop)) >= 4
        # The far-left page: from page 1 the finger goes right before it ever goes left.
        sideways = [c for c in loop if c['points'][0][1] in (1500, 1520)]
        assert all(c['points'][0][0] < c['points'][-1][0] for c in sideways[:3])
        # Settings: a long run one way, then back.
        at = next(i for i, c in enumerate(loop) if c['points'][0] == (148, 570))
        run = 0
        while loop[at + 1 + run]['points'][0] == (800, 1900):
            run += 1
        assert run >= 12, run
    # Only kernel-metal4wifi has it, and fk_human stays as it is when the argument is absent.
    assert 'fuxihumanm' not in keys and 'fk_human_dense' not in keys
    for patch in sorted((ROOT / 'xnu/patches').glob('*.patch')):
        if patch.name != '0117-fuxi-ath12k.patch':
            assert 'fuxihumanm' not in patch.read_text(errors='surrogateescape'), patch.name
    text = (ROOT / 'xnu/patches/0117-fuxi-ath12k.patch').read_text(errors='surrogateescape')
    hunk = text[text.index('--- a/bsd/dev/fuxi_keys.c'):]
    hunk = hunk[:hunk.index('\n--- a/', 10)]
    gone = [line[1:] for line in hunk.splitlines() if line.startswith('-') and not line.startswith('---')]
    assert gone == [sync.KEYS_DENSE_START_FIND.rstrip('\n')]
    assert 'dense == 1 ? fk_human_dense : fk_human' in hunk and 'fk_dense_flick(' in hunk


FA_BSS_DRIVER = r"""
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "fa_bss.h"

static uint32_t seed = 0x13579bdf;
static uint32_t next(void) { seed ^= seed << 13; seed ^= seed >> 17; seed ^= seed << 5; return seed; }

/* A frame in a piece of memory of exactly its size, so the sanitizer sees a read past its end. */
static enum fa_bss_result take(struct fa_bss_table *t, const uint8_t *frame, unsigned length, unsigned freq, int rssi) {
    uint8_t *exact = malloc(length ? length : 1);
    memcpy(exact, frame, length);
    enum fa_bss_result r = fa_bss_take(t, exact, length, freq, rssi);
    free(exact);
    return r;
}

/* Beacon header with this BSSID's last byte, then fixed fields, then the elements given. */
static unsigned frame_of(uint8_t *out, uint8_t kind, uint8_t last, const void *elements, unsigned length) {
    memset(out, 0, 36);
    out[0] = kind;
    memcpy(out + 16, (uint8_t[]){0x02, 0xa1, 0xb2, 0xc3, 0xd4, last}, 6);
    memcpy(out + 36, elements, length);
    return 36 + length;
}

static void check_table(const struct fa_bss_table *t) {
    assert(t->count <= FA_BSS_MOST);
    for (unsigned i = 0; i < t->count; i++) {
        assert(t->entry[i].ssid_length <= FA_SSID_MOST && t->entry[i].frequency != 0 && t->entry[i].times != 0);
    }
}

static void elements(void) {
    static const uint8_t list[] = {0, 2, 'h', 'i', 3, 1, 6, 221, 0, 48, 3, 1, 2, 3};
    assert(fa_element(list, sizeof(list), 0) == list);
    assert(fa_element(list, sizeof(list), 3) == list + 4);
    assert(fa_element(list, sizeof(list), 221) == list + 7);
    assert(fa_element(list, sizeof(list), 48) == list + 9);
    assert(fa_element(list, sizeof(list), 99) == NULL);
    for (unsigned cut = 0; cut < sizeof(list); cut++) {
        uint8_t *exact = malloc(cut ? cut : 1);
        memcpy(exact, list, cut);
        /* An element is found only when all of it is there. */
        assert((fa_element(exact, cut, 48) != NULL) == (cut >= 14));
        assert((fa_element(exact, cut, 0) != NULL) == (cut >= 4));
        free(exact);
    }
    static const uint8_t lying[] = {0, 200, 'x'};
    assert(fa_element(lying, sizeof(lying), 0) == NULL);
    static const uint8_t lying_later[] = {3, 1, 6, 48, 255, 0, 2, 'o', 'k'};
    assert(fa_element(lying_later, sizeof(lying_later), 3) == lying_later);
    assert(fa_element(lying_later, sizeof(lying_later), 0) == NULL); /* nothing after a lie is read */
    assert(fa_element(list, 0, 0) == NULL && fa_element(list, 1, 0) == NULL);
    puts("elements: bounds");
}

static void refused(void) {
    static struct fa_bss_table t;
    uint8_t f[600];
    static const uint8_t named[] = {0, 3, 'a', 'b', 'c'};
    unsigned n = frame_of(f, 0x80, 1, named, sizeof(named));
    /* Every cut of a good frame: refused or taken, never read past the end. A name is taken only whole. */
    for (unsigned cut = 0; cut <= n; cut++) {
        memset(&t, 0, sizeof(t));
        enum fa_bss_result r = take(&t, f, cut, 2412, -50);
        assert(r == (cut < 2 ? FA_BSS_OTHER : cut < n ? FA_BSS_MALFORMED : FA_BSS_TAKEN));
        assert(t.count == (cut == n));
    }
    /* Not a beacon or a response: other kinds, a frame version other than 0. */
    for (unsigned kind = 0; kind < 256; kind++) {
        if (kind != 0x80 && kind != 0x50) {
            f[0] = (uint8_t)kind;
            memset(&t, 0, sizeof(t));
            assert(take(&t, f, n, 2412, -50) == FA_BSS_OTHER && t.count == 0 && t.others == 1);
        }
    }
    memset(&t, 0, sizeof(t));
    /* Protected, group or empty BSSID, no frequency, a frequency that does not fit. */
    n = frame_of(f, 0x80, 1, named, sizeof(named));
    f[1] = 0x40; assert(take(&t, f, n, 2412, -50) == FA_BSS_MALFORMED); f[1] = 0;
    f[16] |= 1; assert(take(&t, f, n, 2412, -50) == FA_BSS_MALFORMED); f[16] &= 0xfe;
    assert(take(&t, f, n, 0, -50) == FA_BSS_MALFORMED);
    assert(take(&t, f, n, 0x10000, -50) == FA_BSS_MALFORMED);
    uint8_t zero[600];
    n = frame_of(zero, 0x80, 0, named, sizeof(named));
    memset(zero + 16, 0, 6);
    assert(take(&t, zero, n, 2412, -50) == FA_BSS_MALFORMED);
    /* SSID missing, 33 bytes, claiming more than there is, after a lying element. */
    static const uint8_t none[] = {1, 1, 0x82};
    n = frame_of(f, 0x80, 1, none, sizeof(none));
    assert(take(&t, f, n, 2412, -50) == FA_BSS_MALFORMED);
    uint8_t long_name[35] = {0, 33};
    memset(long_name + 2, 'x', 33);
    n = frame_of(f, 0x80, 1, long_name, sizeof(long_name));
    assert(take(&t, f, n, 2412, -50) == FA_BSS_MALFORMED);
    static const uint8_t claims[] = {0, 32, 'x'};
    n = frame_of(f, 0x80, 1, claims, sizeof(claims));
    assert(take(&t, f, n, 2412, -50) == FA_BSS_MALFORMED);
    static const uint8_t after_lie[] = {48, 250, 0, 2, 'o', 'k'};
    n = frame_of(f, 0x80, 1, after_lie, sizeof(after_lie));
    assert(take(&t, f, n, 2412, -50) == FA_BSS_MALFORMED);
    assert(t.count == 0 && t.malformed == 9 && t.beacons == 0);
    /* The +HTC field moves the fixed fields and the elements: a frame that only just has them, and one that has not. */
    memset(f, 0, sizeof(f));
    f[0] = 0x80; f[1] = 0x80; memcpy(f + 16, (uint8_t[]){2, 1, 1, 1, 1, 1}, 6);
    memcpy(f + 40, named, sizeof(named));
    assert(take(&t, f, 40 + sizeof(named), 2412, -50) == FA_BSS_TAKEN && t.count == 1 && t.entry[0].ssid_length == 3);
    assert(take(&t, f, 39, 2412, -50) == FA_BSS_MALFORMED);
    puts("frames: refused");
}

static void table(void) {
    static struct fa_bss_table t;
    uint8_t f[100], hidden_nul[100];
    static const uint8_t named[] = {0, 4, 'h', 'o', 'm', 'e'}, empty[] = {0, 0}, nul[] = {0, 3, 0, 0, 0}, full[34] = {0, 32};
    unsigned n = frame_of(f, 0x80, 1, named, sizeof(named));
    assert(take(&t, f, n, 2412, -60) == FA_BSS_TAKEN);
    /* Another hearing: stronger keeps the new frequency, weaker keeps the old. */
    assert(take(&t, f, n, 2417, -40) == FA_BSS_TAKEN && t.count == 1 && t.entry[0].frequency == 2417 && t.entry[0].rssi == -40);
    assert(take(&t, f, n, 2422, -70) == FA_BSS_TAKEN && t.entry[0].frequency == 2417 && t.entry[0].rssi == -40 && t.entry[0].times == 3);
    /* A hidden beacon does not take the name away; a response gives a hidden network its name. */
    n = frame_of(f, 0x80, 1, empty, sizeof(empty));
    assert(take(&t, f, n, 2417, -40) == FA_BSS_TAKEN && t.entry[0].ssid_length == 4 && !memcmp(t.entry[0].ssid, "home", 4));
    n = frame_of(hidden_nul, 0x80, 1, nul, sizeof(nul));
    assert(take(&t, hidden_nul, n, 2417, -40) == FA_BSS_TAKEN && t.entry[0].ssid_length == 4);
    n = frame_of(f, 0x80, 2, nul, sizeof(nul));
    assert(take(&t, f, n, 5180, -50) == FA_BSS_TAKEN && t.count == 2 && t.entry[1].ssid_length == 3);
    n = frame_of(f, 0x50, 2, named, sizeof(named));
    assert(take(&t, f, n, 5180, -50) == FA_BSS_TAKEN && t.entry[1].ssid_length == 4 && t.responses == 1);
    n = frame_of(f, 0x80, 3, empty, sizeof(empty));
    assert(take(&t, f, n, 5180, -50) == FA_BSS_TAKEN && t.count == 3 && t.entry[2].ssid_length == 0);
    n = frame_of(f, 0x80, 4, full, sizeof(full));
    assert(take(&t, f, n, 5180, -50) == FA_BSS_TAKEN && t.entry[3].ssid_length == 32);
    /* Signal values outside what the entry holds, and the data path's "none". */
    n = frame_of(f, 0x80, 5, named, sizeof(named));
    assert(take(&t, f, n, 5180, 1000) == FA_BSS_TAKEN && t.entry[4].rssi == 127);
    n = frame_of(f, 0x80, 6, named, sizeof(named));
    assert(take(&t, f, n, 5180, -1000) == FA_BSS_TAKEN && t.entry[5].rssi == FA_RSSI_NONE);
    /* Full: a stronger network pushes out the weakest, and a weaker one is left out. Both counted. */
    memset(&t, 0, sizeof(t));
    for (unsigned i = 0; i < FA_BSS_MOST; i++) {
        n = frame_of(f, 0x80, (uint8_t)i, named, sizeof(named));
        assert(take(&t, f, n, 2412, -50 - (int)(i % 20)) == FA_BSS_TAKEN);
    }
    assert(t.count == FA_BSS_MOST && t.lost == 0);
    n = frame_of(f, 0x80, 200, named, sizeof(named));
    assert(take(&t, f, n, 2412, -90) == FA_BSS_LOST && t.count == FA_BSS_MOST && t.lost == 1);
    assert(take(&t, f, n, 2412, -30) == FA_BSS_TAKEN && t.count == FA_BSS_MOST && t.lost == 2);
    unsigned found = 0;
    for (unsigned i = 0; i < t.count; i++) {
        found += t.entry[i].address[5] == 200 && t.entry[i].rssi == -30;
    }
    assert(found == 1);
    /* One already kept is only updated, full or not. */
    n = frame_of(f, 0x80, 200, named, sizeof(named));
    assert(take(&t, f, n, 2417, -20) == FA_BSS_TAKEN && t.lost == 2);
    check_table(&t);
    /* The strongest first, at most so many; an empty table says nothing. */
    uint8_t order[FA_BSS_SAID_MOST];
    assert(fa_bss_strongest(&t, order, FA_BSS_SAID_MOST) == FA_BSS_SAID_MOST);
    assert(t.entry[order[0]].rssi == -20);
    for (unsigned i = 1; i < FA_BSS_SAID_MOST; i++) {
        assert(t.entry[order[i - 1]].rssi >= t.entry[order[i]].rssi);
    }
    static struct fa_bss_table none;
    assert(fa_bss_strongest(&none, order, FA_BSS_SAID_MOST) == 0);
    puts("table: identity, hidden names, signal, bounded, strongest first");
}

static void escapes(void) {
    char out[FA_SSID_ESCAPED_MOST + 8];
    uint8_t all[FA_SSID_MOST];
    /* Every byte value, in every place: what comes out is printable, closed, and cannot end the quoted text. */
    for (unsigned v = 0; v < 256; v++) {
        memset(all, (int)v, sizeof(all));
        memset(out, 0x7f, sizeof(out));
        unsigned n = fa_ssid_escape(out, FA_SSID_ESCAPED_MOST, all, FA_SSID_MOST);
        assert(n < FA_SSID_ESCAPED_MOST && out[n] == 0 && out[FA_SSID_ESCAPED_MOST] == 0x7f);
        for (unsigned i = 0; i < n; i++) {
            assert(out[i] >= 0x20 && out[i] < 0x7f);
            assert(out[i] != '"' || out[i - 1] == '\\');
        }
    }
    assert(fa_ssid_escape(out, sizeof(out), (const uint8_t *)"cafe", 4) == 4 && !strcmp(out, "cafe"));
    assert(!strcmp((fa_ssid_escape(out, sizeof(out), (const uint8_t *)"a\"b\\c", 5), out), "a\\\"b\\\\c"));
    assert(!strcmp((fa_ssid_escape(out, sizeof(out), (const uint8_t *)"\n%s\xff\x00", 5), out), "\\x0a%s\\xff\\x00"));
    assert(fa_ssid_escape(out, sizeof(out), all, 0) == 0 && out[0] == 0);
    /* A length beyond the standard's is cut at it, not read. */
    memset(all, 'y', sizeof(all));
    assert(fa_ssid_escape(out, sizeof(out), all, 1000) == FA_SSID_MOST);
    /* Every small buffer: closed, and cut at a whole escape. */
    assert(fa_ssid_escape(out, 0, all, 4) == 0);
    for (unsigned size = 1; size <= FA_SSID_ESCAPED_MOST; size++) {
        char *exact = malloc(size);
        memset(all, 0xfe, sizeof(all));
        unsigned n = fa_ssid_escape(exact, size, all, FA_SSID_MOST);
        assert(n < size && exact[n] == 0 && n % 4 == 0);
        free(exact);
    }
    puts("ssid text: bounded, escaped");
}

static void rates(void) {
    static const uint16_t twelve[] = {10, 20, 55, 110, 60, 90, 120, 180, 240, 360, 480, 540};
    uint8_t out[32];
    unsigned n = fa_probe_rates(out, sizeof(out), twelve, 12);
    static const uint8_t wanted[] = {1, 8, 2, 4, 11, 22, 12, 18, 24, 36, 50, 4, 48, 72, 96, 108};
    assert(n == sizeof(wanted) && !memcmp(out, wanted, n));
    static const uint16_t eight[] = {60, 90, 120, 180, 240, 360, 480, 540};
    n = fa_probe_rates(out, sizeof(out), eight, 8);
    assert(n == 10 && out[0] == 1 && out[1] == 8 && out[9] == 108);
    assert(fa_probe_rates(out, sizeof(out), twelve, 0) == 0);
    /* Too little room is no element at all. */
    for (unsigned size = 0; size < sizeof(wanted); size++) {
        uint8_t *exact = malloc(size ? size : 1);
        assert(fa_probe_rates(exact, size, twelve, 12) == 0);
        free(exact);
    }
    assert(fa_probe_rates(out, sizeof(wanted), twelve, 12) == sizeof(wanted));
    /* What it makes is what the elements' walk reads back. */
    n = fa_probe_rates(out, sizeof(out), twelve, 12);
    assert(fa_element(out, n, FA_EID_RATES) == out && fa_element(out, n, FA_EID_RATES_MORE) == out + 10);
    puts("probe rates: only rates, bounded");
}

/* Noise, and good frames with some bytes changed. Nothing here may read outside its frame or break the table. */
static void noise(void) {
    static struct fa_bss_table t;
    uint8_t f[512];
    for (unsigned round = 0; round < 400000; round++) {
        unsigned n = next() % sizeof(f);
        if (round % 2) {
            static const uint8_t good[] = {0, 3, 'a', 'b', 'c', 1, 1, 0x82, 3, 1, 6, 48, 4, 1, 2, 3, 4};
            unsigned at = frame_of(f, round % 4 == 1 ? 0x80 : 0x50, (uint8_t)next(), good, sizeof(good));
            n = at;
            for (unsigned k = next() % 4; k > 0; k--) {
                f[next() % at] = (uint8_t)next();
            }
            n = next() % 8 == 0 ? next() % (at + 1) : at;
        } else {
            for (unsigned i = 0; i < n; i++) {
                f[i] = (uint8_t)next();
            }
            f[0] = round % 8 == 0 ? (uint8_t)next() : round % 4 == 0 ? 0x80 : 0x50;
        }
        (void)take(&t, f, n, 1 + next() % 0xffff, (int)(next() % 400) - 200);
        if (round % 1000 == 0) {
            check_table(&t);
        }
    }
    check_table(&t);
    assert(t.beacons + t.responses + t.malformed + t.others + t.lost >= 400000 && t.count > 0);
    char out[FA_SSID_ESCAPED_MOST];
    uint8_t order[FA_BSS_SAID_MOST];
    unsigned said = fa_bss_strongest(&t, order, FA_BSS_SAID_MOST);
    for (unsigned i = 0; i < said; i++) {
        (void)fa_ssid_escape(out, sizeof(out), t.entry[order[i]].ssid, t.entry[order[i]].ssid_length);
    }
    printf("noise: %u kept, %u taken, %u malformed, %u others, %u lost\n", t.count, t.beacons + t.responses, t.malformed, t.others, t.lost);
}

int main(void) {
    elements();
    refused();
    table();
    escapes();
    rates();
    noise();
    return 0;
}
"""


@check
def bss_table_and_elements_on_malformed_frames(tmp):
    """src/fa_bss.h under the sanitizers: each frame in memory of exactly its size. Design M4: IE parsing bounds-checked."""
    (tmp / 'bss.c').write_text(FA_BSS_DRIVER)
    own = ROOT / 'xnu/fuxi-ath12k/src'
    cc = os.environ.get('CC', 'cc')
    flags = ['-std=gnu11', '-Wall', '-Wextra', '-Wno-unused-function', '-g', '-O1', f'-I{own}']
    program = tmp / 'bss'
    subprocess.run([cc, *flags, '-fsanitize=address,undefined', '-fno-sanitize-recover=all', str(tmp / 'bss.c'), '-o', str(program)], check=True)
    subprocess.run([str(program)], check=True)


@check
def whole_wlan_file_parses_with_and_without_ath12k(tmp):
    """Every guard of fuxi_wlan.c, not only the block the W15 harness compiles. M1's first CI run failed on a
    guard's closing comment inside a block comment: -Wcomment sees that, and the guards must balance both ways.
    The includes are dropped, so this checks the preprocessor's view of the text only; the XNU build compiles it."""
    text = (ROOT / 'xnu/fuxi-wlan/fuxi_wlan.c').read_text()
    source = tmp / 'wlan.c'
    source.write_text(''.join(line for line in text.splitlines(True) if not line.startswith('#include')))
    cc = os.environ.get('CC', 'cc')
    outputs = {}
    for flags in (['-DFUXI'], ['-DFUXI', '-DFUXI_ATH12K=1']):
        result = subprocess.run([cc, '-E', '-P', '-Wcomment', '-Werror=comment', *flags, str(source)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr[:1500]
        assert 'fuxi_wlan_init' in result.stdout, flags
        outputs[len(flags)] = result.stdout
    for word in ('wl_fa_mac_configure', 'wl_fa_handoff', 'fuxi_ath12k_firmware_start', 'fuxi_ath12k_mhi_failed', 'fw_bar_window'):
        assert word in outputs[2] and word not in outputs[1], word


@check
def pins_are_recorded(tmp):
    pins = build.read_pins()
    assert pins['ATH12K_LINUX_SHA'] == '458c6079fc1d41d564c37679c8ace02cd83ee817'
    assert pins['LKPI_FREEBSD_SHA'] == '95719d8a895bf8170851e9844d9b4092b1d2b6fe'
    for key in ('ATH12K_LINUX_TREE_SHA256', 'LKPI_FREEBSD_TREE_SHA256'):
        assert len(pins[key]) == 64


def real_edits_match_once(deps, tmp):
    edits = json.loads((ROOT / 'tools/ath12k-edits.json').read_text())['edits']

    def source_of(tree, rel):
        return (deps / 'linux' / build.ATH12K / rel) if tree == 'ath12k' else (deps / 'freebsd' / rel)
    build.apply_edits(edits, source_of, lambda tree, rel: tmp / 'edited' / tree / rel)
    print(f'ok real_edits_match_once ({len(edits)})')


def stage_hooks_are_where_the_design_says(deps, tmp):
    """Order in the edited core.c and mac.c: what each stage has behind it when it stops, and that no radio work is."""
    edits = json.loads((ROOT / 'tools/ath12k-edits.json').read_text())['edits']

    def source_of(tree, rel):
        return (deps / 'linux' / build.ATH12K / rel) if tree == 'ath12k' else (deps / 'freebsd' / rel)
    build.apply_edits(edits, source_of, lambda tree, rel: tmp / 'edited' / tree / rel)
    core = (tmp / 'edited/ath12k/core.c').read_text()

    def body(text, signature):
        start = text.index(signature)
        return text[start:text.index('\n}\n', start)]
    start = body(core, 'static int ath12k_core_start(struct ath12k_base *ab)')
    assert start.index('ath12k_htc_wait_target') < start.index('fa_stage_htc_ready') < start.index('ath12k_dp_htt_connect')
    for later in ('ath12k_wmi_wait_for_unified_ready', 'ath12k_wmi_set_hw_mode', 'ath12k_dp_tx_htt_h2t_ver_req_msg'):
        assert 'fa_stage_registered' not in start and start.index('fa_stage_htc_ready') < start.index(later)
    group = body(core, 'static int ath12k_core_hw_group_start(struct ath12k_hw_group *ag)')
    assert group.index('ath12k_mac_register(ag)') < group.index('fa_stage_registered') < group.index('set_bit(ATH12K_GROUP_FLAG_REGISTERED')
    stop = group[group.index('fa_stage_registered'):]
    assert stop.index('ath12k_mac_unregister(ag)') < stop.index('-ECANCELED') < stop.index('goto err_mlo_teardown') < stop.index('ath12k_core_pdev_create(ab)')
    assert group.index('fa_stage_registered') < group.index('ath12k_hif_irq_enable(ab)')
    ready = body(core, 'int ath12k_core_qmi_firmware_ready(struct ath12k_base *ab)')
    assert ready.index('ath12k_core_hw_group_start(ag)') < ready.index('ret != -ECANCELED') < ready.index('goto err_core_stop')
    assert 'ath12k_mac_op_start' not in core and 'ath12k_mac_start' not in core
    mac = (tmp / 'edited/ath12k/mac.c').read_text()
    eht = body(mac, 'static void ath12k_mac_copy_eht_cap(struct ath12k *ar,')
    assert eht.index('fa_refuse_eht()') < eht.index('eht_cap->has_eht = true')
    assert 'fa_radio_mac(ar->mac_addr)' in body(mac, 'static int ath12k_mac_hw_register(struct ath12k_hw *ah)')
    wmi = body((tmp / 'edited/ath12k/wmi.c').read_text(), 'static void ath12k_wmi_op_rx(struct ath12k_base *ab, struct sk_buff *skb)')
    assert wmi.index('fa_wmi_event(id, skb->len)') < wmi.index('switch (id)')

    # Stage 4 runs with everything up, and stops through the group's own error path.
    tail = group[group.index('fa_stage_up'):]
    assert group.index('ath12k_hif_irq_enable(ab)') < group.index('ath12k_core_rfkill_config(ab)') < group.index('fa_stage_up')
    assert tail.index('-ECANCELED') < tail.index('goto err') < tail.index('return 0') < tail.index('ath12k_core_hw_group_stop(ag)')
    assert group.count('fa_stage_up') == 1 and core.count('fa_stage_up') == 1
    # The stage's stop reaches ath12k_core_stop(), whose MODE off is skipped once the flush is set.
    stop = body(core, 'static void ath12k_core_stop(struct ath12k_base *ab)')
    assert stop.index('ATH12K_FLAG_CRASH_FLUSH') < stop.index('ath12k_qmi_firmware_stop(ab)') < stop.index('ath12k_hif_stop(ab)')
    # The knobs are at the commands they name.
    assert 'single_pdev_only && !fa_knob_no_hw_mode()' in start
    mac_start = body(mac, 'static int ath12k_mac_start(struct ath12k *ar)')
    assert 'ab->hw_params->idle_ps && !fa_knob_no_idle_ps()' in mac_start
    # The credit level and the settle come right after the vdev create command, before its first parameter.
    vdev = body(mac, '\nint ath12k_mac_vdev_create(struct ath12k *ar, struct ath12k_link_vif *arvif)\n')
    assert vdev.index('ath12k_wmi_vdev_create(ar') < vdev.index('fa_vdev_created(ab, arvif->vdev_id)') < vdev.index('ath12k_mac_update_vif_offload(arvif)')
    assert vdev.index('fa_vdev_created') < vdev.index('WMI_VDEV_PARAM_NSS')
    assert '!fa_no_11d_scan()' in vdev
    scan = body(mac, 'static int ath12k_mac_initiate_hw_scan(struct ieee80211_hw *hw,')
    assert scan.index('ath12k_mac_vdev_create(ar, arvif)') < scan.index('fa_scan_vdev_ready') < scan.index('ath12k_start_scan(ar, arg)')
    # A station's scan with no SSID is what ath12k sends as passive, so the knob needs no edit of its own.
    assert scan.index('if (req->n_ssids)') < scan.index('arg->scan_f_passive = 1') < scan.index('ath12k_start_scan(ar, arg)')
    link = body(mac, 'ath12k_mac_find_link_id_by_ar(struct ath12k_vif *ahvif, struct ath12k *ar)')
    assert link.index('fa_scan_on_link_zero(ar->ab)') < link.index('scan_links_map = ~ahvif->links_map')
    # A WMI timeout reaches the evidence, with the upstream line first.
    send = body((tmp / 'edited/ath12k/wmi.c').read_text(), 'int ath12k_wmi_cmd_send(struct ath12k_wmi_pdev *wmi, struct sk_buff *skb,')
    assert send.index('wmi command %d timeout') < send.index('fa_wmi_command(wmi_ab->ab, cmd_id, ret)') < send.index('return ret')
    # The flush fails what is sent after the evidence at once: wmi.c's own check.
    assert 'ATH12K_FLAG_CRASH_FLUSH' in send
    # remove_interface before stop: stop empties pdevs_active, where the vdev delete response looks for its radio.
    assert 'rcu_assign_pointer(ar->ab->pdevs_active[ar->pdev_idx], NULL)' in body(mac, '\nstatic void ath12k_mac_stop(struct ath12k *ar)\n')
    assert 'pdevs_active[i]' in body(mac, 'struct ath12k *ath12k_mac_get_ar_by_vdev_id(struct ath12k_base *ab, u32 vdev_id)')
    own = (ROOT / 'xnu/fuxi-ath12k/src/fa_80211.c').read_text()
    down = body(own, 'station_down(struct fa_radio *radio)')
    assert down.index('ops->remove_interface') < down.index('ops->stop')
    # In mac80211's order: the deauthentication and the station's states before the channel is given up.
    leave = body(own, 'station_leave(struct fa_radio *radio)')
    assert leave.index('fa_deauth(') < leave.index('ops->flush') < leave.index('station_state(') < leave.index('BSS_CHANGED_ASSOC') < leave.index('ops->unassign_vif_chanctx') < leave.index('ops->remove_chanctx')
    join = body(own, 'station_join(struct fa_radio *radio, bool *joined)')
    assert join.index('ops->add_chanctx') < join.index('ops->assign_vif_chanctx') < join.index('link_changed(radio, BSS_CHANGED_BSSID') < join.index('IEEE80211_STA_NOTEXIST, IEEE80211_STA_NONE')
    assert join.index('IEEE80211_STA_NONE, IEEE80211_STA_AUTH') < join.index('IEEE80211_STA_AUTH, IEEE80211_STA_ASSOC') < join.index('IEEE80211_STA_ASSOC, IEEE80211_STA_AUTHORIZED') < join.index('vif_changed(radio, BSS_CHANGED_ASSOC)')
    # What the join leans on in ath12k: no RSN or WPA goes to the firmware, the peer is authorized when the vdev comes up, the flush runs the mgmt work.
    assoc = body(mac, 'static void ath12k_bss_assoc(struct ath12k *ar,')
    assert assoc.index('ath12k_wmi_vdev_up') < assoc.index('peer->is_authorized') < assoc.index('WMI_PEER_AUTHORIZE')
    add = body(mac, 'static int ath12k_mac_station_add(struct ath12k *ar,')
    assert add.index('ath12k_peer_create') < add.index('ath12k_start_vdev_delay')
    assert 'wiphy_work_flush(hw->wiphy, &ar->wmi_mgmt_tx_work)' in body(mac, 'void ath12k_mac_op_flush(struct ieee80211_hw *hw, struct ieee80211_vif *vif,')
    # The 5 GHz fix: the first range was right because this condition was not there, and the regd update now has both branches.
    reg = body((tmp / 'edited/ath12k/reg.c').read_text(), 'int ath12k_regd_update(struct ath12k *ar, bool init)')
    assert reg.index('reg_freq_2ghz') < reg.index('ar->mac.sbands[NL80211_BAND_5GHZ].channels') < reg.index('reg_freq_5ghz') < reg.index('reg_freq_6ghz')
    setup = body(mac, 'static int ath12k_mac_setup_channels_rates(struct ath12k *ar,')
    assert setup.index('sizeof(ath12k_6ghz_channels)') < setup.index('sizeof(ath12k_5ghz_channels)') < setup.index('ab->reg_freq_5ghz.start_freq')
    # WCN7850 has no host2rxdma group: only what is reaped goes back, which is what the refill reading of design R6 rests on.
    hw = (tmp / 'edited/ath12k/wifi7/hw.c').read_text() if (tmp / 'edited/ath12k/wifi7/hw.c').exists() else (deps / 'linux' / build.ATH12K / 'wifi7/hw.c').read_text()
    mask = hw[hw.index('ath12k_wifi7_hw_ring_mask_wcn7850 = {'):]
    assert re.search(r'\.host2rxdma = \{\s*\}', mask[:mask.index('};')])
    print('ok stage_hooks_are_where_the_design_says')


def page_size_assertion_fires(deps, tmp):
    """The prelude's assertion, compiled against machine/param.h as it is and as if it said 16 KiB."""
    sysdir = deps / 'freebsd/sys'
    machine = sysdir / 'arm64/include'
    real = (machine / 'param.h').read_text()
    fake = tmp / 'fake/machine'
    fake.mkdir(parents=True)
    for path in machine.iterdir():
        (fake / path.name).symlink_to(path)
    (fake / 'param.h').unlink()
    changed = real.replace('#define\tPAGE_SHIFT\tPAGE_SHIFT_4K', '#define\tPAGE_SHIFT\tPAGE_SHIFT_16K')
    assert changed != real, 'the fixture did not change PAGE_SHIFT'
    (fake / 'param.h').write_text(changed)
    (tmp / 'machine').symlink_to(machine, target_is_directory=True)
    prelude = (ROOT / 'xnu/fuxi-ath12k/compat/include/lx_prelude.h').read_text()
    assertion = next(line for line in prelude.splitlines() if line.startswith('_Static_assert(PAGE_SIZE'))
    probe = tmp / 'probe.c'
    probe.write_text('#include <sys/param.h>\n' + assertion + '\n')

    def compile_with(first):
        command = ['clang', '-target', build.TARGET, '-ffreestanding', '-nostdinc', '-fsyntax-only', '-w',
                   '-D__FreeBSD__=15', '-D_KERNEL', f'-I{first}', f'-I{sysdir}', str(probe)]
        return subprocess.run(command, capture_output=True, text=True)
    good = compile_with(tmp)
    assert good.returncode == 0, good.stderr[:800]
    bad = compile_with(tmp / 'fake')
    assert bad.returncode != 0 and 'R2' in bad.stderr, bad.stderr[:800]
    print('ok page_size_assertion_fires')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deps', type=Path, help='directory with the linux and freebsd checkouts')
    parser.add_argument('--full', action='store_true', help='also run the whole build from clean')
    parser.add_argument('--work', type=Path, help='work directory of the --full build')
    args = parser.parse_args()

    for function in checks:
        tmp = Path(tempfile.mkdtemp(prefix='fa-test-'))
        try:
            function(tmp)
            print('ok', function.__name__)
        finally:
            shutil.rmtree(tmp)
    if args.deps:
        for function in (real_edits_match_once, stage_hooks_are_where_the_design_says, page_size_assertion_fires):
            tmp = Path(tempfile.mkdtemp(prefix='fa-test-'))
            try:
                function(args.deps, tmp)
            finally:
                shutil.rmtree(tmp)
    else:
        print('skip real trees: no --deps')
    if args.full:
        assert args.deps, '--full needs --deps'
        work = args.work or Path(tempfile.gettempdir()) / 'fa-test-build'
        command = [sys.executable, str(ROOT / 'tools/ci/build-ath12k.py'), '--clean', '--work', str(work),
                   '--linux', str(args.deps / 'linux'), '--freebsd', str(args.deps / 'freebsd')]
        result = subprocess.run(command, capture_output=True, text=True)
        print(result.stdout, end='')
        assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
        last = result.stdout.strip().splitlines()[-1]
        assert last.startswith('fuxi-ath12k: 33 sources, object ') and last.endswith('all allowed'), last
        print('ok full_build')
    print('fuxi-ath12k tests passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
