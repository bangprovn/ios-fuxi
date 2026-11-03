#!/usr/bin/env python3
"""Generate/check patch 0121 (fuxi-exec-inject) from xnu/fuxi-inject/.

xnu/fuxi-inject/hooks.diff holds the two kern_exec.c hunks (boot-arg fxinj; one more environment string in
exec_extract_strings). The new file bsd/kern/fuxi_exec_inject.h is a snapshot of the canonical source next to it.
Run it in WSL/Linux: Windows Python writes CRLF and the kernel build fails.

  --check          the checked-in patch must equal the generated one
  --tree DIR       also dry-run the patch with `patch -p1` in DIR, an xnu tree with every patch before 0121 applied
"""
import argparse
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'xnu/fuxi-inject'
PATCH = ROOT / 'xnu/patches/0121-fuxi-exec-inject.patch'
NEW = ('fuxi_exec_inject.h',)


def new_file(path, text):
    lines = text.splitlines(True)
    assert lines and text.endswith('\n'), path
    return '--- a/%s\n+++ b/%s\n@@ -0,0 +1,%d @@\n' % (path, path, len(lines)) + ''.join('+' + l for l in lines)


def expected():
    hooks = (SRC / 'hooks.diff').read_text()
    paths = re.findall(r'^--- a/(\S+)\n', hooks, re.M)
    assert paths == ['bsd/kern/kern_exec.c'], 'hooks.diff: kern_exec.c only'
    for hunk in re.finditer(r'^@@ -\d+,(\d+) \+\d+,(\d+) @@\n((?:[ +-].*\n)+)', hooks, re.M):
        body = hunk[3].splitlines()
        assert int(hunk[1]) == sum(l[0] in ' -' for l in body) and int(hunk[2]) == sum(l[0] in ' +' for l in body), hunk[0]
    return hooks + ''.join(new_file('bsd/kern/' + n, (SRC / n).read_text()) for n in NEW)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--tree')
    args = ap.parse_args()
    text = expected()
    assert '\r' not in text
    for prior in sorted(PATCH.parent.glob('*.patch')):
        if prior.name != PATCH.name:
            assert not re.search(r'^\+\+\+ b/bsd/kern/fuxi_exec_inject', prior.read_text(errors='surrogateescape'), re.M), prior.name
    if args.check:
        assert PATCH.read_text() == text, 'fuxi-inject sources differ; run tools/sync-fuxi-inject.py'
    else:
        PATCH.write_text(text)
    if args.tree:
        subprocess.run(['patch', '-p1', '--dry-run', '-s', '-N'], input=text, text=True, cwd=args.tree, check=True)
    print('PASS: fuxi-inject header and hooks.diff match 0121' + ('; 0121 applies' if args.tree else ''))


if __name__ == '__main__':
    main()
