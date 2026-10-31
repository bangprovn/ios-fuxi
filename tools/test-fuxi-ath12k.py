#!/usr/bin/env python3
"""Tests for the ath12k link check (design M0): gates, edits, R2, and what must stay untouched.

The gates run on small fixtures and need no external tree. With --deps (a directory holding the
linux and freebsd checkouts) the edits are also matched against the real files, the PAGE_SIZE
assertion is shown to fire, and with --full the whole build runs clean.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BASE_COMMIT = 'fa93d15'


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
    for wanted in ('ath12k-rfkill-pin', 'ath12k-reo-qdesc-alloc', 'ath12k-rx-refill-guard-a', 'ath12k-mac-override'):
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


@check
def metal4_inputs_are_untouched(tmp):
    paths = ['tools/ci/build.sh', 'tools/ci/prepare-kernel.py', 'tools/ci/kernel-inputs.py',
             'xnu/fuxi-wlan/fuxi_wlan.c', 'xnu/patches']
    result = subprocess.run(['git', '-C', str(ROOT), 'diff', '--stat', BASE_COMMIT, '--', *paths],
                            capture_output=True, text=True)
    if 'not a git repository' in result.stderr:
        # A worktree made by Windows git names its gitdir with a drive letter, which WSL cannot read.
        print('skip git part of metal4_inputs_are_untouched: this git cannot read the worktree')
    else:
        assert result.returncode == 0, result.stderr
        assert result.stdout == '', f'changed since {BASE_COMMIT}:\n{result.stdout}'
    assert not (ROOT / 'xnu/patches/0117-fuxi-ath12k.patch').exists()
    assert 'ath12k' not in (ROOT / 'tools/ci/build.sh').read_text()


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
