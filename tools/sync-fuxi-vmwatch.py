#!/usr/bin/env python3
"""Generate/check patch 0123 (fuxi-vm-watch) from xnu/fuxi-vmwatch/.

xnu/fuxi-vmwatch/hooks.diff holds the three vm_map.c hunks (the include, vm_map_protect, vm_map_delete);
the new file osfmk/vm/fuxi_vm_watch.h is a snapshot of the canonical source next to it. Boot-arg
fuxivmwatch=NAME turns it on (fuxi_vm_watch.h says what it logs). No earlier patch touches vm_map.c.
Run it in WSL/Linux: Windows Python writes CRLF and the kernel build fails.

  --check          the checked-in patch must equal the generated one
  --tree DIR       also dry-run the patch with `patch -p1` in DIR, an xnu tree with every patch before 0123 applied
"""
import argparse
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'xnu/fuxi-vmwatch'
PATCH = ROOT / 'xnu/patches/0123-fuxi-vm-watch.patch'
NEW = ('fuxi_vm_watch.h',)


def new_file(path, text):
    lines = text.splitlines(True)
    assert lines and text.endswith('\n'), path
    return '--- a/%s\n+++ b/%s\n@@ -0,0 +1,%d @@\n' % (path, path, len(lines)) + ''.join('+' + l for l in lines)


def expected():
    hooks = (SRC / 'hooks.diff').read_text()
    paths = re.findall(r'^--- a/(\S+)\n', hooks, re.M)
    assert paths == ['osfmk/vm/vm_map.c'], 'hooks.diff: vm_map.c only'
    hunks = list(re.finditer(r'^@@ -\d+,(\d+) \+\d+,(\d+) @@\n((?:[ +-].*\n)+)', hooks, re.M))
    assert len(hunks) == 3, 'hooks.diff: include, vm_map_protect, vm_map_delete'
    for hunk in hunks:
        body = hunk[3].splitlines()
        assert int(hunk[1]) == sum(l[0] in ' -' for l in body) and int(hunk[2]) == sum(l[0] in ' +' for l in body), hunk[0]
        assert not any(l.startswith('-') for l in body), 'hooks.diff only adds lines'
    return hooks + ''.join(new_file('osfmk/vm/' + n, (SRC / n).read_text()) for n in NEW)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--tree')
    args = ap.parse_args()
    text = expected()
    assert '\r' not in text
    for prior in sorted(PATCH.parent.glob('*.patch')):
        if prior.name != PATCH.name:
            body = prior.read_text(errors='surrogateescape')
            assert not re.search(r'^\+\+\+ b/osfmk/vm/(vm_map\.c|fuxi_vm_watch\.h)', body, re.M), prior.name
    if args.check:
        assert PATCH.read_text() == text, 'fuxi-vmwatch sources differ; run tools/sync-fuxi-vmwatch.py'
    else:
        PATCH.write_text(text)
    if args.tree:
        subprocess.run(['patch', '-p1', '--dry-run', '-s', '-N'], input=text, text=True, cwd=args.tree, check=True)
    print('PASS: fuxi-vmwatch header and hooks.diff match 0123' + ('; 0123 applies' if args.tree else ''))


if __name__ == '__main__':
    main()
