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
    for wanted in ('ath12k-rfkill-pin', 'ath12k-reo-qdesc-alloc', 'ath12k-rx-refill-guard-a', 'ath12k-mac-override', 'ath12k-stage-htc'):
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
    assert not (tmp / 'metal4/cmdline-ath12k1.txt').exists()
    line = (tmp / 'metal4wifi/cmdline-ath12k1.txt').read_text()
    assert line == base.rstrip('\n') + ' fuxiath12k=1\n' and len(line.encode()) - 1 < 608


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
                   '#define __unused __attribute__((unused))\n#define __dead2 __attribute__((noreturn))\n',
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
    for prototype in ('fa_firmware_ready', 'fa_stop'):
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
        for function in (real_edits_match_once, page_size_assertion_fires):
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
