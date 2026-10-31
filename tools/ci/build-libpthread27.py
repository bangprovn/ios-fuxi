#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause-Patent
# Port of UniXNU scripts/build-libpthread-kernel.py (github.com/UniXNU/UniXNU 88604d0; xnu27/NOTICE).
"""Build libpthread's kernel part into libpthread_kernel.a for the iOS 27 kernel (docs-lanes/ios27.md, K2 libpthread).

  build-libpthread27.py --libpthread DIR --xnu DIR --kdir DIR --fakeroot DIR --triple arm64e-apple-ios27.0 --out DIR

The archive holds libpthread's three real C sources (kern/kern_init.c, kern_support.c, kern_synch.c: pthread_start,
the pthread_functions table bsd/pthread/pthread_shims.c panics without) and xnu's bsd/kern/source_pthread_bootstrap.c
(UniXNU source-pthread-bootstrap.patch: calls pthread_start at STARTUP(EARLY_BOOT)). xnu's own build never compiles
the bootstrap (it is not in bsd/conf/files), so the kernel registers pthread only when the archive is force-loaded;
tools/ci/build-xnu27.sh links it into all three kernels and checks for _pthread_start/_source_pthread_status.

KDIR is a DEVELOPMENT_ARM64_FUXI object directory after a kernel pass: its osfmk/DEVELOPMENT/prng_random.o.json
(clang -MJ) is the exact kernel compile command reused for the bootstrap, and its link.filelist/all-alias.exp give
the kernel's names (libpthread definitions that collide are renamed with -include; imports must all be kernel names).
Fuxi: paths and the pin come from arguments and xnu27/sources.env; patches from xnu27/libpthread/series; -M*
dependency outputs are dropped from the reused command so the kernel's prng_random.d/.o.json stay untouched.
"""
import argparse
import json
from pathlib import Path
import shutil
import struct
import subprocess

ROOT = Path(__file__).resolve().parents[2]
ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
for name in ('--libpthread', '--xnu', '--kdir', '--fakeroot', '--out'):
    ap.add_argument(name, type=Path, required=True)
ap.add_argument('--triple', required=True)
args = ap.parse_args()
env = dict(l.split('=', 1) for l in (ROOT / 'xnu27/sources.env').read_text().splitlines() if l and not l.startswith('#'))
REVISION = env['LIBPTHREAD_SHA']
SOURCE = args.libpthread.resolve()
if subprocess.check_output(['git', '-C', SOURCE, 'rev-parse', 'HEAD'], text=True).strip() != REVISION:
    raise SystemExit('Unexpected libpthread source revision')
if subprocess.check_output(['git', '-C', SOURCE, 'status', '--porcelain'], text=True):
    raise SystemExit('The libpthread checkout must remain clean')
OUT = args.out.resolve()
WORK = OUT / 'libpthread-work'
shutil.rmtree(WORK, ignore_errors=True)
OUT.mkdir(parents=True, exist_ok=True)
# A plain copy in its own repository (the CI checkout is shallow; patches never touch it).
shutil.copytree(SOURCE, WORK, ignore=shutil.ignore_patterns('.git'))
subprocess.run(['git', 'init', '-q', WORK], check=True)
series = [l for l in (ROOT / 'xnu27/libpthread/series').read_text().splitlines() if l and not l.startswith('#')]
for patch in series:
    subprocess.run(['git', '-C', WORK, 'apply', ROOT / 'xnu27/libpthread' / patch], check=True)
SOURCE = WORK
kdir = args.kdir.resolve()
recipe = kdir / 'osfmk/DEVELOPMENT/prng_random.o.json'
record = json.loads(recipe.read_text().strip().rstrip(','))
sdk = subprocess.check_output(['xcrun', '--sdk', 'iphoneos', '--show-sdk-path'], text=True).strip()
ARCH, IOS = args.triple.split('-apple-ios')
SUBTYPE = {'arm64': 0, 'arm64e': 2}[ARCH]
_major, _minor = (IOS.split('.') + ['0'])[:2]
DEPLOYMENT = (2, int(_major) << 16 | int(_minor) << 8)
DEPS = {'-MD', '-MMD', '-MP'}
DEPS_ARG = {'-MF', '-MJ', '-MT', '-MQ'}


def check_object(data, what):
    """Refuse a relocatable object whose architecture or deployment differs from the kernel target."""
    magic, cpu, subtype, kind, count = struct.unpack_from('<5I', data)
    if (magic, cpu, subtype & 0xff, kind) != (0xfeedfacf, 0x100000c, SUBTYPE, 1):
        raise SystemExit(f'{what}: not an {ARCH} object')
    offset, deployment = 32, None
    for _ in range(count):
        command, length = struct.unpack_from('<II', data, offset)
        if command == 0x32:
            deployment = struct.unpack_from('<II', data, offset + 8)
        offset += length
    if deployment != DEPLOYMENT:
        raise SystemExit(f'{what}: deployment {deployment}, expected {DEPLOYMENT}')


def compile_one(source, destination, namespace=None):
    if source.name == 'source_pthread_bootstrap.c':
        command = []
        arguments = record['arguments']
        index = 0
        while index < len(arguments):
            value = arguments[index]
            if value == record['file']:
                command.append(str(source))
            elif value == '-o' or value in DEPS_ARG:
                if value == '-o':
                    command.extend(['-o', str(destination)])
                index += 1
            elif value not in DEPS:
                command.append(value)
            index += 1
        if str(source) not in command or command.count('-o') != 1:
            raise SystemExit(f'{recipe}: unexpected compile record')
    else:
        framework = args.fakeroot.resolve() / 'System/Library/Frameworks/Kernel.framework/Versions/A'
        command = ['xcrun', '--sdk', 'iphoneos', 'clang', '-target', args.triple,
                   '-isysroot', sdk, '-mkernel', '-ffreestanding', '-fno-builtin', '-fno-stack-check',
                   '-std=gnu11', '-fblocks', '-O2', '-mno-implicit-float',
                   '-DKERNEL=1', '-DKERNEL_PRIVATE=1', '-DXNU_KERNEL_PRIVATE=1',
                   '-DMACH_KERNEL_PRIVATE=1', '-DMACH_ASSERT=1', '-DDEBUG=1',
                   '-D__PTHREAD_EXPOSE_INTERNALS__=1', '-DABSOLUTETIME_SCALAR_TYPE=1', '-DNEEDS_SCHED_CALL_T=1',
                   '-I', str(SOURCE / 'private'), '-I', str(SOURCE / 'include'), '-I', str(SOURCE),
                   '-I', str(framework / 'PrivateHeaders'), '-I', str(framework / 'Headers'),
                   '-Wno-deprecated-declarations', '-c', str(source), '-o', str(destination)]
    if namespace:
        command += ['-include', str(namespace)]
    result = subprocess.run(command, cwd=record['directory'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    destination.with_suffix('.log').write_text(result.stdout)
    if result.returncode:
        raise SystemExit(result.stdout[-4500:])
    check_object(destination.read_bytes(), destination.name)


def names(paths, mode, global_only=True):
    found = set()
    paths = [str(p) for p in paths]
    for start in range(0, len(paths), 200):
        out = subprocess.check_output(['xcrun', 'nm', *(['-g'] if global_only else []), mode, '-j', *paths[start:start + 200]], text=True)
        found |= {line.strip() for line in out.splitlines() if line.startswith('_')}
    return found


def kernel_names():
    """Names of the XNU kernel alone: the objects of its link (link.filelist) plus the exported aliases."""
    filelist = kdir / 'link.filelist'
    if not filelist.is_file():
        raise SystemExit(f'No XNU symbol names: {filelist} is missing (run a kernel pass first)')
    objects = [kdir / line.strip() for line in filelist.read_text().splitlines() if line.strip()]
    found = names([p for p in objects if p.is_file()], '-U', global_only=False)
    aliases = kdir / 'all-alias.exp'
    if aliases.is_file():
        found |= {word for line in aliases.read_text().splitlines() for word in line.split() if word.startswith('_')}
    return found


sources = [SOURCE / 'kern' / name for name in ('kern_init.c', 'kern_support.c', 'kern_synch.c')]
objects = []
for source in sources:
    destination = OUT / (source.stem + '.o')
    compile_one(source, destination)
    objects.append(destination)
kernel = kernel_names()
definitions = names(objects, '-U')
collisions = definitions & kernel
if collisions:
    namespace = OUT / 'namespace.h'
    namespace.write_text(''.join(f'#define {name[1:]} pthread519_{name[1:]}\n' for name in sorted(collisions)))
    for source, destination in zip(sources, objects):
        compile_one(source, destination, namespace)
definitions = names(objects, '-U')
imports = names(objects, '-u') - definitions
# The component is statically linked: os_log's DSO identity is the real
# executable Mach-O header, synthesized by the Mach-O executable linker.
missing = sorted(imports - kernel - {'___dso_handle'})
report = {'revision': REVISION, 'series': series, 'sources': 3, 'target': args.triple, 'target_audited': True,
          'namespace_collisions_isolated': sorted(collisions), 'missing_kernel_names': missing,
          'linker_synthesized_symbols': ['___dso_handle'], 'registration_observed': False}
(OUT / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
if not {'_pthread_kern', '_current_uthread'} <= collisions:
    raise SystemExit('Missing expected pthread/XNU static symbol isolation')
if missing:
    raise SystemExit(f'Unavailable real kernel imports: {missing}')
if '_pthread_start' not in definitions:
    raise SystemExit('libpthread objects do not define _pthread_start')
bootstrap = OUT / 'source_pthread_bootstrap.o'
compile_one(args.xnu.resolve() / 'bsd/kern/source_pthread_bootstrap.c', bootstrap)
if not {'_source_pthread_status'} <= names([bootstrap], '-U') or '_pthread_start' not in names([bootstrap], '-u'):
    raise SystemExit('source_pthread_bootstrap.o does not define _source_pthread_status or call _pthread_start')
archive = OUT / 'libpthread_kernel.a'
archive.unlink(missing_ok=True)
subprocess.run(['xcrun', 'ar', 'rcs', archive, *objects, bootstrap], check=True)
print(f'libpthread {REVISION[:12]}: 3 real C sources + bootstrap, {args.triple}, '
      f'{len(collisions)} names isolated, no unavailable kernel names -> {archive}')
