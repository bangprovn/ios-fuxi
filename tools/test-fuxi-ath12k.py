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
struct ieee80211_hw { struct wiphy *wiphy; void *priv; size_t vif_data_size, sta_data_size, chanctx_data_size; unsigned long flags; struct { uint32_t listen_interval; } conf; };
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
};
struct ieee80211_tx_status { struct sk_buff *skb; struct ieee80211_tx_info *info; };
enum ieee80211_iface_iter { IEEE80211_IFACE_ITER_NORMAL };
""",
}

FA_80211_DRIVER = r"""
#include "fa_80211.c"
int fail_workqueue, queues_destroyed, fail_skb;
static char said[8192];
static unsigned locks, notified, notified_initiator, rddm;
static unsigned long long now = 1;
static uint8_t notified_alpha2[2];
void lx_tell(const char *f, ...) { va_list l; va_start(l, f); vsnprintf(said + strlen(said), sizeof(said) - strlen(said), f, l); va_end(l); }
long lx_stub(const char *n) { return 0; }
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
static char said[65536];
static unsigned locks, rddm;
static char calls[1024];
static unsigned long long now = 1;
static int trouble_after = -1;
static unsigned trouble_asks, evidence_count, r6_count;
static char first_evidence[96];
void lx_tell(const char *f, ...) { va_list l; va_start(l, f); vsnprintf(said + strlen(said), sizeof(said) - strlen(said), f, l); va_end(l); }
long lx_stub(const char *n) { return 0; }
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
static struct { bool forged_first, any_reply, silent, no_m3, door, die_at_m3, goes_open; int fail_key; bool m1_given, m3_given, forged_given, stayed; } wpa;
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
static void net_heard(const uint8_t *data, unsigned n, unsigned type) {
    assert(net.on && radio_of(the_hw)->reached == IEEE80211_STA_AUTHORIZED);
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
static void eapol_sent(struct ieee80211_hw *hw, struct sk_buff *skb) {
    unsigned header = skb->data[0] == 0x88 ? 26 : 24;
    if (skb->protocol != cpu_to_be16(0x888e)) {
        unsigned type = fa_be16(skb->data + header + 6);
        assert(skb->data[1] == 0x41 && IEEE80211_SKB_CB(skb)->control.hw_key == radio_of(hw)->pairwise_key && radio_of(hw)->pairwise_key != NULL && skb->priority == 0 && skb->protocol == cpu_to_be16(type));
        assert(!memcmp(skb->data + header, "\xaa\xaa\x03\x00\x00\x00", 6));
        uint8_t copy[1600]; unsigned n = skb->len - header - 8; memcpy(copy, skb->data + header + 8, n);
        IEEE80211_SKB_CB(skb)->flags |= IEEE80211_TX_STAT_ACK;
        struct ieee80211_tx_status s = { .skb = skb, .info = IEEE80211_SKB_CB(skb) };
        linuxkpi_ieee80211_tx_status_ext(hw, &s);
        net_heard(copy, n, type);
        return;
    }
    assert(skb->data[1] == 1 && skb->protocol == cpu_to_be16(0x888e) && skb->len > header + 8 && skb->priority == 7);
    const uint8_t *body = skb->data + header + 8; unsigned n = skb->len - header - 8;
    eapols++;
    if (!wpa.any_reply) {
        if (eapols == 1) assert(n == sizeof(V_M2) && !memcmp(body, V_M2, n));
        else assert(n == sizeof(V_M4) && !memcmp(body, V_M4, n));
    }
    IEEE80211_SKB_CB(skb)->flags |= IEEE80211_TX_STAT_ACK;
    struct ieee80211_tx_status s = { .skb = skb, .info = IEEE80211_SKB_CB(skb) };
    linuxkpi_ieee80211_tx_status_ext(hw, &s);
}
static int op_set_key(struct ieee80211_hw *hw, enum set_key_cmd cmd, struct ieee80211_vif *v, struct ieee80211_sta *s, struct ieee80211_key_conf *k) {
    bool pairwise = (k->flags & IEEE80211_KEY_FLAG_PAIRWISE) != 0;
    assert(hw->wiphy->mtx.locked && k->cipher == WLAN_CIPHER_SUITE_CCMP && k->keylen == 16 && k->iv_len == 8 && k->link_id == -1 && (cmd == DISABLE_KEY ? radio_of(hw)->reached >= IEEE80211_STA_ASSOC : radio_of(hw)->reached == IEEE80211_STA_ASSOC));
    if (pairwise) assert(s == radio_of(hw)->sta && k->keyidx == 0 && (cmd != SET_KEY || !memcmp(k->key, V_TK, 16)));
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
static unsigned auth_tries, assoc_tries, deauths, scans;
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
    .vif_cfg_changed = op_vif, .conf_tx = op_conf_tx, .flush = op_flush, .tx = op_tx, .set_key = op_set_key };

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
    the_hw->vif_data_size = 512; the_hw->sta_data_size = 256; the_hw->chanctx_data_size = 128;
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
static void fresh_net(void) { fresh_wpa(V_PMK); fa_m7_ask(); net.on = true; }
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
    assert 'wl_fa_stage != 6 && wl_fa_stage != 7' in block and 'PE_parse' not in block and 'fuxi-wifi-join' in block and block.count('printf(') == 3
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
    gtk = bytes(range(100, 116))

    def message(info, counter, nonce, payload=b'', signed=True):
        out = bytearray(99 + len(payload))
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
static const uint8_t OWN[6] = {2, 0x46, 0x55, 0x58, 0x49, 1}, AP[6] = {0x02, 0xa1, 0xb2, 0xc3, 0xd4, 0x55}, SERVER[6] = {0x02, 0, 0, 0, 0, 9};
static const uint8_t ME[4] = {10, 0, 0, 9}, THEM[4] = {10, 0, 0, 1};
static uint32_t seed = 0x1234567;
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
