#!/usr/bin/env python3
"""Generate/check patch 0125 (fuxi-aks-bt) from xnu/fuxi-aksbt/.

A diagnostic for the built-in AppleKeyStore (patch 0038, SweetKeyStore.cpp): the "SWEET AKS:" call log gets a rate
limit (the first calls per process and selector, then one count per 10 s) and a process that makes a burst of
device-state calls (selector 17) gets its calling thread's user stack printed ("SWEET AKS BT:"). No answer changes.

xnu/fuxi-aksbt/hooks.diff holds the build hooks: in SweetKeyStore.cpp the include, the init in
AppleKeyStore::start and the per-call IOLog replaced by fuxi_aks_log(); in osfmk/conf/files the new
osfmk/kern/fuxi_aks_bt.c. The new files iokit/Kernel/arm/fuxi_aks_log.h and osfmk/kern/fuxi_aks_bt.c are
snapshots of the canonical sources next to it. Run it in WSL/Linux: Windows Python writes CRLF and the build fails.

  --check          the checked-in patch must equal the generated one
  --xnu DIR        also check that 0125 applies after every earlier delta to SweetKeyStore.cpp and osfmk/conf/files,
                   on DIR's stock osfmk/conf/files (xnu-8796.141.3), with and without 0117 (only kernel-metal4wifi
                   applies 0117). Without it the SweetKeyStore.cpp hunks are still checked: earlier patches create it.
"""
import argparse
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'xnu/fuxi-aksbt'
PATCH = ROOT / 'xnu/patches/0125-fuxi-aks-bt.patch'
NEW = (('iokit/Kernel/arm/fuxi_aks_log.h', 'fuxi_aks_log.h'), ('osfmk/kern/fuxi_aks_bt.c', 'fuxi_aks_bt.c'))
HOOKED = ('iokit/Kernel/arm/SweetKeyStore.cpp', 'osfmk/conf/files')
# The only lines hooks.diff may add and remove, in order.
ADDED = [
    '#include "fuxi_aks_log.h"      /* the call log\'s rate limit and burst backtrace */',
    '\tfuxi_aks_log_init();',
    '\tfuxi_aks_log(selector, arg0, in_scalars, in_struct, out_scalars, out_struct, ret);',
    'osfmk/kern/fuxi_aks_bt.c\t\tstandard',
]
REMOVED = [
    '\tchar name[32];',
    '\tproc_selfname(name, sizeof(name));',
    '\tIOLog("SWEET AKS: %s sel %u arg0 %llu in %u/%u out %u/%u -> 0x%x\\n", name, selector,',
    '\t    arg0, in_scalars, in_struct, out_scalars, out_struct, ret);',
]


def new_file(path, text):
    lines = text.splitlines(True)
    assert lines and text.endswith('\n'), path
    return '--- /dev/null\n+++ b/%s\n@@ -0,0 +1,%d @@\n' % (path, len(lines)) + ''.join('+' + l for l in lines)


def expected():
    hooks = (SRC / 'hooks.diff').read_text()
    paths = re.findall(r'^--- a/(\S+)\n', hooks, re.M)
    assert paths == list(HOOKED), 'hooks.diff: SweetKeyStore.cpp and osfmk/conf/files only, got %s' % paths
    lines, added, removed, hunks, i = hooks.splitlines(), [], [], 0, 0
    while i < len(lines):
        head = re.match(r'@@ -\d+,(\d+) \+\d+,(\d+) @@$', lines[i])
        i += 1
        if not head:
            assert re.match(r'(--- a/|\+\+\+ b/)\S+$', lines[i - 1]), 'hooks.diff: not a header or hunk: ' + lines[i - 1]
            continue
        old, new = int(head[1]), int(head[2])
        while old or new:
            line = lines[i]
            i += 1
            assert line[:1] in (' ', '+', '-'), 'hooks.diff: ' + line
            old -= line[:1] in ' -'
            new -= line[:1] in ' +'
            (added if line[:1] == '+' else removed if line[:1] == '-' else []).append(line[1:])
            assert old >= 0 and new >= 0, head[0]
        hunks += 1
    assert hunks == 4, 'hooks.diff: the include, the init, the log call, the conf line'
    assert added == ADDED, 'hooks.diff adds other lines: %r' % added
    assert removed == REMOVED, 'hooks.diff removes other lines: %r' % removed
    return hooks + ''.join(new_file(path, (SRC / name).read_text()) for path, name in NEW)


def chunks(text, paths):
    for chunk in re.split(r'(?=^--- (?:a/|/dev/null))', text, flags=re.M):
        match = re.search(r'^\+\+\+ b/(\S+)', chunk, re.M)
        if match and match[1] in paths:
            yield chunk


def baseline(tmp, stock, with_0117):
    """The hooked files as every patch before 0125 leaves them (osfmk/conf/files only with stock)."""
    paths = HOOKED if stock else HOOKED[:1]
    if stock:
        (tmp / 'osfmk/conf').mkdir(parents=True)
        shutil.copyfile(stock / 'osfmk/conf/files', tmp / 'osfmk/conf/files')
    for prior in sorted(PATCH.parent.glob('*.patch')):
        if prior.name >= PATCH.name:
            break
        if prior.name.startswith('0117-') and not with_0117:
            continue
        for chunk in chunks(prior.read_text(errors='surrogateescape'), paths):
            subprocess.run(['git', 'apply', '--whitespace=nowarn', '-'], input=chunk, text=True, cwd=tmp,
                           check=True, capture_output=True)
    return paths


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--xnu', type=Path)
    args = ap.parse_args()
    text = expected()
    assert '\r' not in text
    for prior in sorted(PATCH.parent.glob('*.patch')):
        if prior.name != PATCH.name:
            body = prior.read_text(errors='surrogateescape')
            for path, _ in NEW:
                assert not re.search(r'^\+\+\+ b/%s$' % re.escape(path), body, re.M), prior.name + ' already creates ' + path
            assert not re.search(r'^[+-].*fuxi_aks_(log|user_bt)', body, re.M), prior.name + ' already names fuxi_aks_log'
    if args.check:
        assert PATCH.exists() and PATCH.read_text() == text, 'fuxi-aksbt sources differ; run tools/sync-fuxi-aksbt.py'
    else:
        with open(PATCH, 'w', newline='') as f:
            f.write(text)
    said = []
    for with_0117 in (True, False):
        with tempfile.TemporaryDirectory(prefix='fuxi-aksbt-') as tmp:
            paths = baseline(Path(tmp), args.xnu, with_0117)
            hooks = ''.join(chunks(text, paths))
            subprocess.run(['git', 'apply', '--check', '-'], input=hooks, text=True, cwd=tmp, check=True)
            said.append('%s %s' % ('+'.join(Path(p).name for p in paths), 'with 0117' if with_0117 else 'without 0117'))
    print('PASS: fuxi-aksbt sources and hooks.diff match 0125; its hunks apply to ' + ', '.join(said))


if __name__ == '__main__':
    main()
