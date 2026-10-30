#!/usr/bin/env python3
"""Host test: IIG pregen capture/verify/replay refuses every mismatch (synthetic inputs only)."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

tool = Path(__file__).resolve().parent / 'xnu-iig-cache.py'
fails = 0


def run(*args, env=None):
    return subprocess.run([sys.executable, str(tool), *map(str, args)], capture_output=True, text=True,
                          env={**os.environ, **(env or {})})


def check(name, ok, detail=''):
    global fails
    print(('ok   ' if ok else 'FAIL ') + name + ('' if ok else ': ' + detail))
    fails += not ok


def tree(base, names=('OSObject', 'IOService'), objroots=('hdrs.obj/X86_64', 'xnu.obj/ARM64')):
    src = base / 'xnu/iokit/DriverKit'
    src.mkdir(parents=True)
    for n in names:
        (src / (n + '.iig')).write_text(f'class {n} {{}};\n')
        for o in objroots:
            d = base / 'build' / o / 'iokit/DriverKit'
            d.mkdir(parents=True, exist_ok=True)
            (d / (n + '.h')).write_text(f'/* iig(DriverKit-286) generated from {n}.iig */\nh\n')
            (d / (n + '.iig.cpp')).write_text(f'/* iig(DriverKit-286) generated from {n}.iig */\ncpp\n')
    return base / 'xnu', base / 'build'


with tempfile.TemporaryDirectory() as t:
    base = Path(t)
    xnu, build = tree(base / 'a')
    cache = base / 'cache'
    r = run('--prepare', xnu, build, cache)
    check('prepare', r.returncode == 0 and len(json.loads((cache / 'manifest.json').read_text())) == 2, r.stderr)
    check('prepare refuses existing destination', run('--prepare', xnu, build, cache).returncode != 0)
    r = run('--verify', xnu, cache)
    check('verify', r.returncode == 0 and 'DriverKit-286' in r.stdout, r.stdout + r.stderr)
    # The exact argument shape iokit/DriverKit/Makefile passes to $(IIG).
    out = base / 'out'
    args = ['--def', xnu / 'iokit/DriverKit/OSObject.iig', '--header', out / 'OSObject.h', '--impl', out / 'OSObject.iig.cpp',
            '--framework-name', 'DriverKit', '--', '-isysroot', '/sdk', '-x', 'c++', '-D__IIG=1']
    r = run(*args, env={'XNU_IIG_CACHE': str(cache)})
    check('replay', r.returncode == 0 and (out / 'OSObject.iig.cpp').read_bytes() == (cache / 'OSObject.iig.cpp').read_bytes(), r.stderr)
    (cache / 'IOService.h').write_text('tampered\n')
    check('verify refuses tampered output', run('--verify', xnu, cache).returncode != 0)
    r = run('--def', xnu / 'iokit/DriverKit/IOService.iig', '--header', out / 'IOService.h', '--impl', out / 'IOService.iig.cpp',
            env={'XNU_IIG_CACHE': str(cache)})
    check('replay refuses tampered output', r.returncode != 0 and 'hash mismatch' in r.stderr, r.stderr)
    (xnu / 'iokit/DriverKit/OSObject.iig').write_text('class OSObject { int edited; };\n')
    check('verify refuses edited input', run('--verify', xnu, cache).returncode != 0)
    check('replay refuses edited input', run(*args, env={'XNU_IIG_CACHE': str(cache)}).returncode != 0)
    (xnu / 'iokit/DriverKit/IONew.iig').write_text('class IONew {};\n')
    r = run('--def', xnu / 'iokit/DriverKit/IONew.iig', '--header', out / 'IONew.h', '--impl', out / 'IONew.iig.cpp',
            env={'XNU_IIG_CACHE': str(cache)})
    check('replay refuses unknown input', r.returncode != 0, r.stderr)

    xnu2, build2 = tree(base / 'b')
    (build2 / 'xnu.obj/ARM64/iokit/DriverKit/IOService.h').write_text('different\n')
    check('prepare refuses conflicting headers', run('--prepare', xnu2, build2, base / 'c2').returncode != 0)
    xnu3, build3 = tree(base / 'd')
    (build3 / 'xnu.obj/ARM64/iokit/DriverKit/OSObject.iig.cpp').write_text('different\n')
    check('prepare refuses conflicting implementations', run('--prepare', xnu3, build3, base / 'c3').returncode != 0)
    xnu4, build4 = tree(base / 'e')
    (xnu4 / 'iokit/DriverKit/IOExtra.iig').write_text('class IOExtra {};\n')
    check('prepare refuses missing outputs', run('--prepare', xnu4, build4, base / 'c4').returncode != 0)
    xnu5, build5 = tree(base / 'f')
    cache5 = base / 'c5'
    run('--prepare', xnu5, build5, cache5)
    (xnu5 / 'iokit/DriverKit/IOExtra.iig').write_text('class IOExtra {};\n')
    check('verify refuses uncovered input', run('--verify', xnu5, cache5).returncode != 0)

check('replay script is executable', os.access(tool, os.X_OK))
sys.exit(1 if fails else 0)
