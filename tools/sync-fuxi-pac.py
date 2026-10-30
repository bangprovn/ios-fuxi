#!/usr/bin/env python3
"""Generate/check patch 0114 (fuxi-pac) from xnu/fuxi-pac/.

xnu/fuxi-pac/hooks.diff holds the hunks for upstream files (FUXI-only hooks in
SweetCPU.cpp, sleh.c and files.arm64). The new files osfmk/arm64/fuxi_pac.c and
fuxi_pac_logic.h are snapshots of the canonical sources next to it.

  --check          the checked-in patch must equal the generated one
  --tree DIR       also dry-run the patch with `patch -p1` in DIR, an xnu
                   tree with every patch before 0114 applied (and not 0114)
"""
import argparse
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'xnu/fuxi-pac'
PATCH = ROOT / 'xnu/patches/0114-fuxi-pac-probe.patch'
NEW = ('fuxi_pac_logic.h', 'fuxi_pac.c')


def new_file(path, text):
    lines = text.splitlines(True)
    assert lines and text.endswith('\n'), path
    return '--- a/%s\n+++ b/%s\n@@ -0,0 +1,%d @@\n' % (path, path, len(lines)) + ''.join('+' + l for l in lines)


def expected():
    hooks = (SRC / 'hooks.diff').read_text()
    paths = re.findall(r'^--- a/(\S+)\n', hooks, re.M)
    assert paths == sorted(paths) and len(paths) == len(set(paths)), 'hooks.diff: one sorted section per file'
    assert not any(p.startswith('osfmk/arm64/fuxi_pac') for p in paths), 'hooks.diff: new files come from sources'
    return hooks + ''.join(new_file('osfmk/arm64/' + n, (SRC / n).read_text()) for n in NEW)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--tree')
    args = ap.parse_args()
    text = expected()
    for prior in sorted(PATCH.parent.glob('*.patch')):
        if prior.name != PATCH.name:
            assert not re.search(r'^\+\+\+ b/osfmk/arm64/fuxi_pac', prior.read_text(errors='surrogateescape'), re.M), prior.name
    if args.check:
        assert PATCH.read_text() == text, 'fuxi-pac sources differ; run tools/sync-fuxi-pac.py'
    else:
        PATCH.write_text(text)
    if args.tree:
        subprocess.run(['patch', '-p1', '--dry-run', '-s', '-N', '-F0'], input=text, text=True,
                       cwd=args.tree, check=True)
    print('PASS: fuxi-pac sources and hooks.diff match 0114' + ('; 0114 applies' if args.tree else ''))


if __name__ == '__main__':
    main()
