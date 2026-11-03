#!/usr/bin/env python3
"""Generate/check patch 0119 (fuxi-clock) against the preceding patch stack.

The canonical final SweetPlatformExpert.cpp is in xnu/fuxi-clock/. Its baseline
is rebuilt with git apply from every earlier patch that touches it (0016, 0046,
0059 today), so a change to the platform expert by an earlier patch is caught.
Run it in WSL/Linux: Windows Python writes CRLF and the kernel build fails.
"""
import argparse
import difflib
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'xnu/patches/0119-fuxi-clock.patch'
FILES = {
    'iokit/Kernel/arm/SweetPlatformExpert.cpp': 'SweetPlatformExpert.cpp',
}


def baseline():
    with tempfile.TemporaryDirectory(prefix='fuxi-clock-stack-') as tmp:
        for patch in sorted(PATCH.parent.glob('*.patch')):
            if patch.name >= PATCH.name:
                break
            chunks = re.split(r'(?=^--- (?:a/|/dev/null))', patch.read_text(errors='surrogateescape'), flags=re.M)
            for chunk in chunks:
                match = re.search(r'^\+\+\+ b/(.+)$', chunk, re.M)
                if not match or match[1] not in FILES:
                    continue
                subprocess.run(['git', 'apply', '--whitespace=nowarn', '-'], input=chunk,
                               text=True, cwd=tmp, check=True, capture_output=True)
        return {name: (Path(tmp) / name).read_text() if (Path(tmp) / name).exists() else ''
                for name in FILES}


def expected_patch(before):
    return ''.join(''.join(difflib.unified_diff(
        before[name].splitlines(True), (ROOT / 'xnu/fuxi-clock' / local).read_text().splitlines(True),
        fromfile='a/' + name, tofile='b/' + name))
        for name, local in FILES.items())


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--check', action='store_true')
    args = ap.parse_args()
    before = baseline()
    for name, text in before.items():
        if not text:
            raise SystemExit('no earlier patch creates ' + name)
    expected = expected_patch(before)
    if args.check:
        assert PATCH.read_text() == expected, 'fuxi-clock source differs; run tools/sync-fuxi-clock.py'
    else:
        PATCH.write_text(expected)
    with tempfile.TemporaryDirectory(prefix='fuxi-clock-check-') as tmp:
        for name, text in before.items():
            dest = Path(tmp) / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text)
        subprocess.run(['git', 'apply', '--check', '-'], input=expected, text=True,
                       cwd=tmp, check=True)
    print('PASS: fuxi-clock canonical source matches 0119; preceding patches and 0119 apply')


if __name__ == '__main__':
    main()
