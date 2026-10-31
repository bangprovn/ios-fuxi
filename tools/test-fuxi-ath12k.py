#!/usr/bin/env python3
"""Tests for the ath12k link check (design M0): gates, edits, R2, and what must stay untouched.

The gates run on small fixtures and need no external tree. With --deps (a directory holding the
linux and freebsd checkouts) the edits are also matched against the real files, the PAGE_SIZE
assertion is shown to fire, and with --full the whole build runs clean.
"""
import argparse
import hashlib
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
                   'ath12k-stage-registered', 'ath12k-stage-quiet', 'ath12k-no-eht', 'ath12k-wmi-event-log'):
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
    assert not (tmp / 'metal4/cmdline-ath12k1.txt').exists() and not (tmp / 'metal4/cmdline-ath12k2.txt').exists()
    for stage in (1, 2):
        line = (tmp / f'metal4wifi/cmdline-ath12k{stage}.txt').read_text()
        assert line == base.rstrip('\n') + f' fuxiath12k={stage}\n' and len(line.encode()) - 1 < 608


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
    'linux/skbuff.h': r"""#pragma once
struct sk_buff { int x; };
static inline void dev_kfree_skb_any(struct sk_buff *s) { free(s); }
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
enum nl80211_band { NL80211_BAND_2GHZ, NL80211_BAND_5GHZ, NL80211_BAND_6GHZ, NUM_NL80211_BANDS };
enum { NL80211_REGDOM_SET_BY_USER, NL80211_REGDOM_SET_BY_DRIVER };
enum { IEEE80211_CHAN_DISABLED = 1, IEEE80211_CHAN_NO_IR = 2, IEEE80211_CHAN_RADAR = 8 };
enum { NL80211_RRF_NO_IR = 1, NL80211_RRF_DFS = 2 };
#define WIPHY_FLAG_NOTIFY_REGDOM_BY_DRIVER (1u << 24)
struct linuxkpi_ieee80211_channel {
    signed char psd; enum nl80211_band band; uint32_t center_freq, flags, orig_flags; int max_power, max_reg_power, max_antenna_gain, orig_mpwr;
};
struct ieee80211_rate { int bitrate; };
struct ieee80211_sband_iftype_data { struct { bool has_he; } he_cap; struct { bool has_eht; } eht_cap; };
struct ieee80211_supported_band {
    struct linuxkpi_ieee80211_channel *channels; struct ieee80211_rate *bitrates; struct ieee80211_sband_iftype_data *iftype_data;
    int n_channels, n_bitrates, n_iftype_data; enum nl80211_band band;
};
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
struct cfg80211_bss; struct cfg80211_scan_info;
""",
    'net/mac80211.h': r"""#pragma once
#include <linux/skbuff.h>
#include <linux/workqueue.h>
#define D80211_TODO 1
struct ieee80211_ops; struct ieee80211_vif; struct ieee80211_sta; struct ieee80211_txq; struct ieee80211_chanctx_conf; struct napi_struct;
struct ieee80211_hw { struct wiphy *wiphy; void *priv; };
struct ieee80211_tx_status { struct sk_buff *skb; };
enum ieee80211_iface_iter { IEEE80211_IFACE_ITER_NORMAL };
""",
}

FA_80211_DRIVER = r"""
#include "fa_80211.c"
int fail_workqueue, queues_destroyed;
static char said[8192];
static unsigned locks, notified, notified_initiator, rddm;
static uint8_t notified_alpha2[2];
void lx_tell(const char *f, ...) { va_list l; va_start(l, f); vsnprintf(said + strlen(said), sizeof(said) - strlen(said), f, l); va_end(l); }
long lx_stub(const char *n) { return 0; }
void lx_os_lock(void) { assert(!locks); locks++; }
void lx_os_unlock(void) { assert(locks); locks--; }
void lx_os_sleep(const void *a, unsigned long long n) { abort(); }
void lx_os_wakeup(const void *a) {}
void fuxi_ath12k_force_rddm(void) { rddm++; }
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
    said[0] = 0; assert(linuxkpi_regulatory_set_wiphy_regd_sync(w, regd) == 0);
    memset(regd, 0, size); free(regd);
    assert(w->regd && w->regd->alpha2[0] == 'C' && w->regd->reg_rules[1].power_rule.max_eirp == 3000);
    assert(notified == 1 && notified_initiator == NL80211_REGDOM_SET_BY_DRIVER && notified_alpha2[1] == 'N');
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
    subprocess.run([cc, '-std=gnu11', '-w', '-g', f'-I{tmp / "fake"}', f'-I{own / "src"}', str(tmp / 'driver.c'), '-o', str(program)], check=True)
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
    for word in ('wl_fa_mac_configure', 'wl_fa_handoff', 'fuxi_ath12k_firmware_start', 'fw_bar_window'):
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
