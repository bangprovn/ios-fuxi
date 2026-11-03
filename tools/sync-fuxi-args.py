#!/usr/bin/env python3
"""Generate/check patch 0122 (fuxi-adt-args) from xnu/fuxi-args/.

xnu/fuxi-args/hooks.diff holds the stock-file hunks: pexpert/gen/bootargs.c (the merged line, the parser scanning it,
the FUXI ARGS log) and pexpert/arm/pe_init.c (taken after SecureDTInit, said in PE_init_iokit). The new header
pexpert/gen/fuxi_boot_args.h is a snapshot of the canonical source next to it. No earlier patch touches these files.
Run it in WSL/Linux: Windows Python writes CRLF and the kernel build fails.

  --check          the checked-in patch must equal the generated one
  --tree DIR       also dry-run the patch with `patch -p1` in DIR, an xnu tree with every patch before 0122 applied
                   (stock xnu-8796.141.3 does, as no earlier patch touches the two files)
"""
import argparse
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'xnu/fuxi-args'
PATCH = ROOT / 'xnu/patches/0122-fuxi-adt-args.patch'
HOOKED = ['pexpert/gen/bootargs.c', 'pexpert/arm/pe_init.c']
NEW = {'pexpert/gen/fuxi_boot_args.h': 'fuxi_boot_args.h'}


def new_file(path, text):
    lines = text.splitlines(True)
    assert lines and text.endswith('\n'), path
    return '--- /dev/null\n+++ b/%s\n@@ -0,0 +1,%d @@\n' % (path, len(lines)) + ''.join('+' + l for l in lines)


def expected():
    hooks = (SRC / 'hooks.diff').read_text()
    paths = re.findall(r'^--- a/(\S+)\n', hooks, re.M)
    assert paths == HOOKED, 'hooks.diff: %s only, in that order' % ', '.join(HOOKED)
    for hunk in re.finditer(r'^@@ -\d+,(\d+) \+\d+,(\d+) @@\n((?:[ +-].*\n)+)', hooks, re.M):
        body = [l for l in hunk[3].splitlines() if not l.startswith(('--- ', '+++ '))]
        assert int(hunk[1]) == sum(l[0] in ' -' for l in body) and int(hunk[2]) == sum(l[0] in ' +' for l in body), hunk[0]
    return hooks + ''.join(new_file(path, (SRC / local).read_text()) for path, local in NEW.items())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--tree')
    args = ap.parse_args()
    text = expected()
    assert '\r' not in text, 'CRLF in xnu/fuxi-args: regenerate in WSL/Linux'
    for prior in sorted(PATCH.parent.glob('*.patch')):
        if prior.name >= PATCH.name:
            continue
        body = prior.read_text(errors='surrogateescape')
        for path in HOOKED + list(NEW):
            assert not re.search(r'^\+\+\+ b/' + re.escape(path) + r'$', body, re.M), '%s touches %s' % (prior.name, path)
    if args.check:
        assert PATCH.exists() and PATCH.read_text() == text, 'fuxi-args sources differ; run tools/sync-fuxi-args.py'
    else:
        PATCH.write_text(text)
    if args.tree:
        subprocess.run(['patch', '-p1', '--dry-run', '-s', '-N'], input=text, text=True, cwd=args.tree, check=True)
    print('PASS: fuxi-args header and hooks.diff match 0122' + ('; 0122 applies' if args.tree else ''))


if __name__ == '__main__':
    main()
