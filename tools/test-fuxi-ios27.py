#!/usr/bin/env python3
"""Host checks for the iOS 27 kernel lane (xnu27/, docs-lanes/ios27.md). Linux, no Apple toolchain.

  - xnu27/series lists exactly the committed patches, the UniXNU layers keep their origin/licence notices,
    UniXNU's source-corecrypto patch and any corecrypto source stay out, the component sources match the
    sha256 table, and the Fuxi patches keep the USB-monitor symbol contract (_fuxi_early_status,
    _sweet_log_ring, 256 KiB since fuxi/0006 and exported as _sweet_log_size, _sweet_log_total) and gate their crypto/console code on FUXI;
  - mkadt-fuxi27.py builds the tree ArmDTPlatform/ArmCpu/12377 read: 256-byte random-seed, sha2-384,
    kernel-ctrr-to-be-enabled 0, a valid CHRP NVRAM bank, cpu nodes ('cpu' + u32 MPIDR), /arm-io ranges and a
    GICv3 intc ArmCpu accepts, /psci only with secondaries, no SHA-1-hashed chip ids, <= 16 KiB without K4 options;
  - payload v2: mkpayload --boot-line-length 1024 puts the segment table at 0x448, v1 stays 0x2a8 and
    byte-identical to the default; shim/fuxi-loader/build.sh's payload check accepts both; loader.c carries
    the v1/v2 boot_args layouts (CommandLine at 108, 736 / 1152 bytes); mkpayload --prelinked packs a K2
    MH_FILESET flat and untouched (arm_init slides it), bound to its report and native kernel;
  - fuxi-xnu-monitor.py symbols() on thin and fileset kernels, ring size from _sweet_log_size;
  - kernel-inputs.py --variant ios27 and the CI plan/dry-run/workflow plumbing (ios27 not in all);
  - K4 root storage (fuxi/0007): FUXI gating, read-only command set and extent guard, the IOStorage/IOMedia
    vtable mirror in IOStorageFamily's iOS order, fuxi_root27.h built with the host cc against its Python mirror
    (extent table, plans, guard, manifest, window UUID), mkadt-fuxi27 --root-* round-trip, the 64 KiB v2 ADT limit;
  - K2 libpthread: the libpthread-539.100.4 pin (UniXNU profile), xnu27/libpthread/series = UniXNU's patch verbatim,
    NOTICE entries, no Apple binaries or libpthread source in the repo, the bootstrap only in the archive, and
    libpthread_kernel.a force-loaded and checked in the DEVELOPMENT, chained and RELEASE links.

  --xnu DIR         also apply the series (xnu27/apply.sh) to a temporary worktree of DIR, a git clone of
                    apple-oss-distributions/xnu containing xnu-12377.121.6 (ac9718f), and check the result
  --libpthread DIR  also apply xnu27/libpthread/series to a copy of DIR, a libpthread checkout at LIBPTHREAD_SHA
"""
import argparse
import ast
import hashlib
import json
import random
import re
import struct
import subprocess
import sys
import tempfile
import uuid
import zlib
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
X27 = ROOT / 'xnu27'
sys.path.insert(0, str(ROOT / 'tools'))
import adt  # noqa: E402
import mkpayload  # noqa: E402
import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location('fuxi_adt27', ROOT / 'tools/mkadt-fuxi27.py')
fuxi27 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fuxi27)
XNU_SHA = 'ac9718fb1af618d5ce8678d0dc6e8a58f252216f'


def series():
    return [l for l in (X27 / 'series').read_text().splitlines() if l and not l.startswith('#')]


def layout():
    listed = series()
    files = sorted(p.relative_to(X27 / 'patches').as_posix() for p in (X27 / 'patches').rglob('*.patch'))
    assert sorted(listed) == files and len(set(listed)) == len(listed), 'series and patches/ differ'
    assert listed[-7:] == ['fuxi/0001-machine-config.patch', 'fuxi/0002-builtin-crypto.patch',
                           'fuxi/0003-console.patch', 'fuxi/0004-components.patch', 'fuxi/0005-crypto-kext-handoff.patch',
                           'fuxi/0006-log-ring-fileset.patch', 'fuxi/0007-root27-storage.patch']
    assert listed[0] == 'unixnu/bcm2837-machine-config.patch' and len([p for p in listed if p.startswith('unixnu/')]) == 51
    for bad in ('source-corecrypto14-bootstrap', 'zzz-boot-progress', 'offline-', 'zzw', 'zzy'):
        assert not any(bad in p for p in listed), bad
    for p in (X27 / 'patches').rglob('*.patch'):
        t = p.read_text()
        assert 'CORECRYPTO INTERNAL USE' not in t and 'corecrypto-2023' not in t, p
        assert not re.search(r'^\+\+\+ b/(EXTERNAL_HEADERS/corecrypto|osfmk/corecrypto)/', t, re.M), p
        assert not re.search(r'/(hdd2|home|Users|tmp)/', t), p
    notice = (X27 / 'NOTICE').read_text()
    for need in ('SPDX-License-Identifier: BSD-2-Clause-Patent', 'github.com/UniXNU/UniXNU,', 'commit 88604d0',
                 'UniXNU-sheng', '5b7d83c', 'UniXNU-sm8550-common', '23b2049', 'components/README.md',
                 'make-sheng-adt.py'):
        assert need in notice, need
    for name in ('sheng/sheng-cpu-mmu.patch', 'sheng/sheng-kernel-stack-shift.patch', 'sm8550/native-smp-secondary-start.patch'):
        head = (X27 / 'patches' / name).read_text().split('\ndiff --git', 1)[0]
        assert 'BSD-2-Clause-Patent' in head and 'xnu27/NOTICE' in head, name
    table = (X27 / 'components/README.md').read_text()
    for comp in ('ArmCpu', 'ArmDTPlatform'):
        src = (X27 / 'components' / comp / (comp + '.cpp')).read_bytes()
        assert hashlib.sha256(src).hexdigest() in table and src.startswith(b'// SPDX-License-Identifier: BSD-2-Clause-Patent')
        assert 'BSD-2-Clause-Patent' in (X27 / 'components' / comp / 'LICENSE').read_text()
    assert f'XNU27_SHA={XNU_SHA}' in (X27 / 'sources.env').read_text()
    console = (X27 / 'patches/fuxi/0003-console.patch').read_text()
    assert re.search(r'^\+volatile struct fuxi_early_record fuxi_early_status ', console, re.M)
    assert re.search(r'^\+static char sweet_log_ring\[SWEET_LOG_SIZE\]', console, re.M)
    assert re.search(r'^\+static uint64_t _Atomic sweet_log_total', console, re.M)
    size = re.search(r'^\+#define SWEET_LOG_SIZE\s+(.+)$', console, re.M).group(1)
    assert eval(size.split('/*')[0].replace('u', '').replace('U', '')) == 0x400000, size
    # K2: the native __DATA must fit the fileset gap before the first retained kext __DATA (0x1e8000 on 24A446),
    # so fuxi/0006 shrinks the ring and exports its size for tools/fuxi-xnu-monitor.py.
    ring = (X27 / 'patches/fuxi/0006-log-ring-fileset.patch').read_text()
    size = re.search(r'^\+#define SWEET_LOG_SIZE\s+(.+)$', ring, re.M).group(1)
    assert eval(size.split('/*')[0].replace('u', '').replace('U', '')) == 0x40000, size
    assert re.search(r'^-#define SWEET_LOG_SIZE \(4u << 20\)$', ring, re.M)
    assert '+const uint64_t sweet_log_size __attribute__((used)) = SWEET_LOG_SIZE;' in ring
    assert re.search(r'^\+\tchar text\[120\];', console, re.M)
    crypto = (X27 / 'patches/fuxi/0002-builtin-crypto.patch').read_text()
    for f in ('libkern/crypto/corecrypto_rand.c', 'libkern/crypto/corecrypto_sha2.c', 'osfmk/prng/prng_random.c'):
        hunk = crypto.split(f'+++ b/{f}\n', 1)[1].split('\ndiff --git', 1)[0]
        assert '+#if FUXI' in hunk or '+#if defined(FUXI)' in hunk, f
    # K2: a prelinked com.apple.kec.corecrypto supplies crypto; the fallback never registers the crypto table
    # (register_crypto_functions stays the kext's single call) and the kmem RNG backend is latched once.
    handoff = (X27 / 'patches/fuxi/0005-crypto-kext-handoff.patch').read_text()
    for t in (crypto, handoff):
        assert 'register_crypto_functions' not in t and 'XNU_SOURCE_CORECRYPTO14' not in t
    assert handoff.count('+\tif (FUXI_KMEM_BUILTIN()) {') == 4 and '+static bool fuxi_kmem_builtin;' in handoff
    assert 'FUXI crypto: kmem RNG from corecrypto' in handoff and 'built-in DRBG unused' in handoff
    # K2: symbols tools/xnuprelink needs from the native kernel (fileset_emit stub, reviewed AMFI site, OSKext hook)
    allp = ''.join((X27 / 'patches' / p).read_text() for p in listed)
    for sym in ('+unixnu_fileset_unmapped(uint64_t arg0, uint64_t arg1)', 'SECURITY_READ_ONLY_LATE(uint32_t) unixnu_kernel_stack_shift;',
                '"_NativePrelinkEarlySecurityInit"', 'trust_cache_mut_rt_reserved', '-Wl,-threaded_starts_section'):
        assert sym in allp, sym
    print(f'PASS: series = {len(listed)} committed patches (51 UniXNU verbatim, 2 sheng, sm8550, 7 Fuxi), notices and '
          'component hashes present, no corecrypto source, monitor symbol contract kept, kext crypto handoff latched')


def apply_check(xnu):
    with tempfile.TemporaryDirectory(dir=xnu.parent) as tmp:
        tree = Path(tmp) / 'xnu27-apply'
        subprocess.run(['git', '-C', str(xnu), 'worktree', 'add', '-q', '--detach', str(tree), XNU_SHA], check=True)
        try:
            r = subprocess.run(['bash', str(X27 / 'apply.sh'), str(tree)], capture_output=True, text=True)
            assert r.returncode == 0, r.stderr
            assert f'applied {len(series())} patches' in r.stdout, r.stdout
            for f in ('pexpert/pexpert/arm64/FUXI.h', 'config/MASTER.arm64.fuxi', 'osfmk/arm64/fuxi_console.c',
                      'iokit/Kernel/arm/ArmCpu.cpp', 'iokit/Kernel/arm/ArmDTPlatform.cpp',
                      'pexpert/pexpert/arm64/BCM2837.h', 'iokit/Kernel/arm/FuxiRoot27.cpp',
                      'iokit/Kernel/arm/fuxi_root27.h', 'iokit/Kernel/arm/fuxi_storage_abi.h'):
                assert (tree / f).is_file(), f
            # K4: compiled into the iokit component; started once, after vfsinit(), on FUXI only.
            assert re.search(r'^iokit/Kernel/arm/FuxiRoot27\.cpp\s+optional iokitcpp$',
                             (tree / 'iokit/conf/files.arm64').read_text(), re.M)
            init = (tree / 'bsd/kern/bsd_init.c').read_text()
            assert init.count('fuxi_root27_start();') == 1
            assert re.search(r'\tvfsinit\(\);\n#if defined\(FUXI\)\n\tfuxi_root27_start\(\);\n#endif\n', init)
            assert 'BOOT_LINE_LENGTH 1024' in (tree / 'pexpert/pexpert/arm64/boot.h').read_text()
            assert re.search(r'SUPPORTED_ARM64_MACHINE_CONFIGS = .*\bFUXI\b', (tree / 'makedefs/MakeInc.def').read_text())
            # K2 libpthread: xnu never compiles the bootstrap (only libpthread_kernel.a carries it); the link
            # default this build replaces and the archive dependency hook are what build-xnu27.sh assumes.
            boot = (tree / 'bsd/kern/source_pthread_bootstrap.c').read_text()
            assert 'STARTUP(EARLY_BOOT, STARTUP_RANK_SECOND, source_pthread_bootstrap);' in boot
            assert 'extern kern_return_t pthread_start(kmod_info_t *, void *);' in boot and 'weak' not in boot
            assert not any('source_pthread_bootstrap' in f.read_text() for f in tree.glob('*/conf/files*'))
            mk = (tree / 'makedefs/MakeInc.def').read_text()
            assert 'LD_KERNEL_ARCHIVES = $(LDFLAGS_KERNEL_SDK) -lfirehose_kernel\n' in mk
            assert 'LDFLAGS_KERNEL_SDK\t= -L$(KERNEL_SDKROOT)/usr/local/lib/kernel -L$(KERNEL_SDKROOT)/usr/local/lib/kernel/platform' in mk
            assert ': $(XNU_EXTERNAL_KERNEL_ARCHIVES)' in (tree / 'makedefs/MakeInc.kernel').read_text()
            assert '-MJ $(@:o=o.json)' in (tree / 'makedefs/MakeInc.rule').read_text()
            assert (tree / 'osfmk/prng/prng_random.c').is_file() and 'osfmk/prng/prng_random.c\tstandard' in (tree / 'osfmk/conf/files').read_text()
            assert 'panic("pthread kernel extension not loaded' in (tree / 'bsd/pthread/pthread_shims.c').read_text()
        finally:
            subprocess.run(['git', '-C', str(xnu), 'worktree', 'remove', '--force', str(tree)], check=True)
    print('PASS: xnu27/apply.sh applies the whole series to xnu-12377.121.6 and stages the components; the pthread '
          'bootstrap is outside conf/files and the link/MJ/archive-dependency hooks build-xnu27.sh uses are present')


LIBPTHREAD_TAG, LIBPTHREAD_SHA = 'libpthread-539.100.4', '1f4f5265b319111142f1bf3a27d4484ef5a98314'
# UniXNU 88604d0 patches/libpthread/explicit-kernel-address.patch
LIBPTHREAD_PATCH_SHA256 = 'e07db8de4ca0bd7f827fd0340482496e5ece1c47e02175771f123e6603aea26d'


def lp_series():
    return [l for l in (X27 / 'libpthread/series').read_text().splitlines() if l and not l.startswith('#')]


def pthread_check():
    env = (X27 / 'sources.env').read_text()
    assert f'LIBPTHREAD_TAG={LIBPTHREAD_TAG}\n' in env and f'LIBPTHREAD_SHA={LIBPTHREAD_SHA}\n' in env
    listed = lp_series()
    assert listed == ['explicit-kernel-address.patch'] and sorted(p.name for p in (X27 / 'libpthread').glob('*.patch')) == listed
    patch = (X27 / 'libpthread/explicit-kernel-address.patch').read_bytes()
    assert hashlib.sha256(patch).hexdigest() == LIBPTHREAD_PATCH_SHA256
    assert set(re.findall(rb'^\+\+\+ b/(\S+)', patch, re.M)) == {b'kern/kern_support.c', b'kern/kern_synch.c', b'kern/kern_trace.h'}
    notice = (X27 / 'NOTICE').read_text()
    for need in ('libpthread/explicit-kernel-address.patch', 'tools/ci/build-libpthread27.py', 'build-libpthread-kernel.py',
                 'build-xnu-4k-baseline.sh', 'build-fileset-release-reference.py', 'LIBPTHREAD_SHA'):
        assert need in notice, need
    # No Apple binaries or Apple libpthread/xnu sources: only text, and no copied kern/*.c next to the lane.
    tracked = subprocess.run(['git', '-C', str(ROOT), 'ls-files', 'xnu27', 'tools/ci', 'tools/xnuprelink'],
                             capture_output=True, text=True, check=True).stdout.split()
    for f in tracked:
        head = (ROOT / f).read_bytes()[:8]
        assert head[:4] not in (b'\xcf\xfa\xed\xfe', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca') and head != b'!<arch>\n', f
        assert not re.search(r'\.(a|o|dylib|kext|ipsw|im4p)$', f) and Path(f).name not in (
            'kern_init.c', 'kern_support.c', 'kern_synch.c', 'pthread_shims.c', 'source_pthread_bootstrap.c'), f
    tool = (ROOT / 'tools/ci/build-libpthread27.py').read_text()
    assert tool.startswith('#!/usr/bin/env python3\n# SPDX-License-Identifier: BSD-2-Clause-Patent\n')
    for need in ("env['LIBPTHREAD_SHA']", "'xnu27/libpthread/series'", "('kern_init.c', 'kern_support.c', 'kern_synch.c')",
                 "'bsd/kern/source_pthread_bootstrap.c'", "'osfmk/DEVELOPMENT/prng_random.o.json'", "'link.filelist'",
                 "{'_pthread_kern', '_current_uthread'} <= collisions", "'-mkernel'", "'___dso_handle'", "'ar', 'rcs'",
                 "'_source_pthread_status'", "DEPS_ARG = {'-MF', '-MJ', '-MT', '-MQ'}"):
        assert need in tool, need
    assert not re.search(r'/(hdd2|home|Users|tmp)/|curl|wget|kernelcache', tool)
    # The bootstrap is xnu-side (UniXNU patch) but compiled only into the archive.
    assert 'unixnu/source-pthread-bootstrap.patch' in series()
    allp = ''.join((X27 / 'patches' / p).read_text() for p in series())
    assert not re.search(r'^\+.*source_pthread_bootstrap\.c\s+(standard|optional)', allp, re.M)
    sh = (ROOT / 'tools/ci/build.sh').read_text()
    assert 'source_checkout libpthread "$apple/libpthread.git" "$LIBPTHREAD_SHA" "$work/src/libpthread"' in sh
    assert 'xnu27/libpthread/*.patch' in sh
    b27 = (ROOT / 'tools/ci/build-xnu27.sh').read_text()
    assert 'libdispatch libpthread trustcache' in b27 and '"$LIBPTHREAD_SHA"' in b27
    first = b27.index('make -C "$xnu" build "${common[@]}" DSTROOT="$dst" "${kflags[@]}" BUILD_LTO=0\n')
    build = b27.index('python3 "$root/tools/ci/build-libpthread27.py"')
    second = b27.index('make -C "$xnu" build "${common[@]}" DSTROOT="$dst" "${kflags[@]}" "${pthread[@]}" BUILD_LTO=0\n')
    copy = b27.index('cp "$kdir/mach.development.fuxi" "$out/"')
    assert first < build < second < copy, 'DEVELOPMENT pass, libpthread archive, relink with it, then copy out'
    assert '--triple "arm64e-apple-ios$XNU27_IOS_VERSION"' in b27 and '--kdir "$kdir"' in b27
    arch = re.search(r'pthread=\((.*?)\)\n', b27, re.S).group(1)
    assert '"LD_KERNEL_ARCHIVES=-L$fake/usr/local/lib/kernel -L$fake/usr/local/lib/kernel/platform -lfirehose_kernel -Wl,-force_load,$pthread_a"' in arch
    assert '"XNU_EXTERNAL_KERNEL_ARCHIVES=$pthread_a"' in arch
    chained = b27[b27.index('# K2 prelink input'):b27.index('# K2 matching reference')]
    rel = b27[b27.index('# K2 matching reference'):]
    assert '"${kflags[@]}" "${pthread[@]}" BUILD_LTO=0 XNU_KERNEL_CHAINED_FIXUPS=1' in chained
    assert '"${kflags[@]}" "${pthread[@]}" BUILD_LTO=1' in rel
    assert b27.count('"${pthread[@]}"') == 3 and b27.count('"${kflags[@]}"') == 4
    # Every kept kernel is checked; the check dies (set -e + pipefail) when a link lacks the archive.
    assert 'for sym in _pthread_start _source_pthread_status; do' in b27 and 'xcrun nm -U -j "$1"' in b27
    assert re.search(r'pthread_linked\(\) \{.*?die "\$\(basename "\$1"\) lacks \$sym', b27, re.S)
    assert 'set -euo pipefail' in b27
    assert 'pthread_linked "$out/mach.development.fuxi.unstripped"' in b27[copy:b27.index('# K2 prelink input')]
    assert 'pthread_linked "$out/mach.development.fuxi.chained.unstripped"' in chained
    assert 'pthread_linked "$raw"' in rel and 'mach.release.fuxi.unstripped*' in rel
    assert b27.index('pthread_linked() {') < second
    print(f'PASS: K2 libpthread: {LIBPTHREAD_TAG} {LIBPTHREAD_SHA[:8]} pinned, UniXNU patch verbatim in its series, '
          'NOTICE entries, no Apple binaries/sources tracked, bootstrap only in the archive, libpthread_kernel.a '
          'force-loaded into the DEVELOPMENT, chained and RELEASE links, each checked for _pthread_start')


def libpthread_apply_check(src):
    head = subprocess.run(['git', '-C', str(src), 'rev-parse', 'HEAD'], capture_output=True, text=True, check=True).stdout.strip()
    assert head == LIBPTHREAD_SHA, head
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / 'libpthread'
        subprocess.run(['git', 'clone', '-q', '--shared', str(src), str(work)], check=True)
        subprocess.run(['git', '-C', str(work), 'checkout', '-q', '--detach', LIBPTHREAD_SHA], check=True)
        for p in lp_series():
            subprocess.run(['git', '-C', str(work), 'apply', str(X27 / 'libpthread' / p)], check=True)
        for f in ('kern/kern_init.c', 'kern/kern_support.c', 'kern/kern_synch.c'):
            assert (work / f).is_file(), f
        init = (work / 'kern/kern_init.c').read_text()
        assert re.search(r'^kern_return_t pthread_start\(', init, re.M) and re.search(r'^pthread_callbacks_t pthread_kern;', init, re.M)
        assert 'kevent_list = NULL' not in (work / 'kern/kern_support.c').read_text()
    print(f'PASS: xnu27/libpthread/series applies to {LIBPTHREAD_TAG}; kern_init.c defines pthread_start and pthread_kern')


def props(n):
    return {k: v for k, _, v in n.props}


def child(n, name):
    return next(c for c in n.children if c.name == name)


def adt_check():
    for cpus in (1, 7):
        blob = adt.serialize(fuxi27.build(cpus=cpus, seed=bytes(range(256))))
        assert len(blob) <= 0x4000
        root, end = adt.parse(blob)
        assert end == len(blob) and root.name == 'device-tree'   # ArmDTPlatform IONameMatch 'device-tree'
        ch = props(child(root, 'chosen'))
        assert ch['random-seed'] == bytes(range(256))
        assert ch['crypto-hash-method'] == b'sha2-384\0' and ch['kernel-ctrr-to-be-enabled'] == struct.pack('<I', 0)
        assert struct.unpack('<Q', ch['dram-base'])[0] == 0x8c0000000
        assert 'chip-id' not in ch and 'unique-chip-id' not in ch
        # K2: what the original AppleImage4 and AMFI read (UniXNU profile [adt.chosen], make-sheng-adt.py)
        for k, v in (('certificate-production-status', 0), ('effective-production-status-ap', 0),
                     ('certificate-security-mode', 1), ('effective-security-mode-sep', 1), ('amfi-only-platform-code', 1)):
            assert ch[k] == struct.pack('<I', v), k
        mp = child(child(root, 'chosen'), 'manifest-properties')
        assert set(props(mp)) == {'name'} and not mp.children
        prod = props(child(root, 'product'))
        assert prod['product-name'] == b'iPhone 11 Pro\0' and prod['model'] == b'iPhone12,3\0'
        img = ch['nvram-proxy-data']
        assert struct.unpack('<I', ch['nvram-bank-size'])[0] == len(img) == 8192
        assert struct.unpack_from('<I', img, 16)[0] == zlib.adler32(img[20:])
        off = 0
        names = []
        while off < len(img) and img[off] == 0x70:
            sig, csum, units, name = struct.unpack_from('<BBH12s', img, off)
            hdr = bytearray(img[off:off + 16])
            hdr[1] = 0
            total = sum(hdr)
            while total > 255:
                total = (total & 255) + (total >> 8)
            assert total == csum, name
            names.append(name.rstrip(b'\0'))
            off += units * 16
        assert names == [b'nvram', b'common', b'free'] and off == len(img)
        cpu_nodes = child(root, 'cpus').children
        assert len(cpu_nodes) == cpus
        regs = []
        for c in cpu_nodes:
            p = props(c)
            assert p['device_type'] == b'cpu\0' and len(p['reg']) == 4   # ArmCpu::probe: length 4, u32 reg
            regs.append(struct.unpack('<I', p['reg'])[0])
        assert regs == list(fuxi27.CPU_REGS[:cpus]) and 0x100 not in regs   # cpu@100 runs the USB monitor
        assert props(cpu_nodes[0])['state'] == b'running\0'
        assert any(c.name == 'psci' for c in root.children) == (cpus > 1)
        io = child(root, 'arm-io')
        assert props(io)['ranges'] == struct.pack('<3Q', 0, 0x17000000, 0x08000000)
        gic = props(child(io, 'intc'))
        assert gic['compatible'] == b'arm,gic-v3\0'
        r = struct.unpack('<4Q', gic['reg'])
        assert r[0] + 0x17000000 == 0x17100000 and r[1] >= 0x10000 and r[2] + 0x17000000 == 0x17180000 and r[3] >= 0x20000
        assert struct.unpack('<Q', gic['redistributor-stride'])[0] in (0x20000, 0x30000, 0x40000)
        assert struct.unpack('<I', props(child(root, 'defaults'))['vmm-present'])[0] == 1
        assert any(c.name == 'options' for c in root.children)
    board = props(child(adt.parse(adt.serialize(fuxi27.build(product='board')))[0], 'product'))
    assert board == {'name': b'product\0', 'product-name': b'Xiaomi 13\0', 'model': b'xiaomi,fuxi\0'}
    print('PASS: mkadt-fuxi27 (1 and 7 CPUs): seed, sha2-384, security tuple, amfi-only-platform-code, manifest-properties, '
          '/product iPhone12,3 (board with --product board), CTRR off, CHRP NVRAM, cpu/GIC/arm-io shape, '
          'psci only with secondaries, no chip ids, <= 16 KiB')


def build_sh_check():
    text = (ROOT / 'shim/fuxi-loader/build.sh').read_text()
    return text.split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]


LINK = 0xfffffff007004000


def fileset(flat=True, pc=LINK + 0x8010):
    """A minimal K2 collection: flat root (__TEXT, __TEXT_EXEC), LC_UNIXTHREAD, a com.apple.kernel entry."""
    seg = lambda name, va, size, off: struct.pack('<II16s4Q4I', 0x19, 72, name.encode(), va, size, off, size, 5, 5, 0, 0)
    name = b'com.apple.kernel\0'.ljust(24, b'\0')
    thread = struct.pack('<IIII', 0x5, 16 + 34 * 8 + 8, 6, 68) + bytes(32 * 8) + struct.pack('<Q', pc) + bytes(8 + 8)
    cmds = (seg('__TEXT', LINK, 0x4000, 0) + seg('__TEXT_EXEC', LINK + 0x4000, 0x8000, 0x4000 if flat else 0x5000)
            + thread + struct.pack('<IIQQII', 0x80000035, 32 + len(name), LINK + 0x4000, 0x4000, 32, 0) + name)
    raw = bytearray(0xc000)
    raw[:32 + len(cmds)] = struct.pack('<8I', 0xfeedfacf, 0x100000c, 0xc0000002, 12, 4, len(cmds), 0, 0) + cmds
    inner = seg('__TEXT_EXEC', LINK + 0x8000, 0x4000, 0x8000)
    raw[0x4000:0x4000 + 32 + len(inner)] = struct.pack('<8I', 0xfeedfacf, 0x100000c, 0xc0000002, 2, 1, len(inner), 0, 0) + inner
    raw[0x5000:0x5017] = b'DEVELOPMENT_ARM64_FUXI\0'
    return bytes(raw)


def prelinked_check(tmp, check):
    native = tmp / 'native'
    native.write_bytes(b'native kernel')
    sha = lambda b: hashlib.sha256(b).hexdigest()

    def emit(blob, complete=False, native_sha=None):
        (tmp / 'kc.prelinked').write_bytes(blob)
        (tmp / 'kc.json').write_text(json.dumps({
            'format': 'MH_FILESET', 'inputs': {'native_sha256': native_sha or sha(b'native kernel')},
            'candidate_sha256': sha(blob), 'mapping_complete': complete, 'closure': ['a', 'b'],
            'stubbed': {'a': {'0x1': 1, '0x2': 3}}, 'missing': {'b': {'0x3': 1}}, 'reference_sha256': None}))
        return tmp / 'kc.prelinked'

    def refused(*args, **kw):
        try:
            mkpayload.build(*args, **kw)
        except SystemExit:
            return True
        return False

    kc = emit(fileset())
    assert refused(native, tmp / 'a', 'cpus=1', boot_line_length=1024, prelinked_path=kc)   # incomplete
    blob, info = mkpayload.build(native, tmp / 'a', 'cpus=1', boot_line_length=1024, prelinked_path=kc, allow_incomplete=True)
    assert info['fixups'] == 0 and info['entry'] == hex(LINK + 0x8010) and info['virt_base'] == hex(LINK & ~0x1ffffff)
    assert info['prelinked'] == {'fileset': 'kc.prelinked', 'mapping_complete': False, 'stubbed': 2, 'missing': 1,
                                 'closure': 2, 'reference': None}
    assert [s[:2] for s in info['segs']] == [('__TEXT', hex(LINK)), ('__TEXT_EXEC', hex(LINK + 0x4000))]
    va, vs, off, sz = struct.unpack_from('<4Q', blob, 0x448)
    assert va == LINK and struct.unpack_from('<I', blob, off + 12)[0] == 12   # root MH_FILESET header copied untouched
    va, vs, off, sz = struct.unpack_from('<4Q', blob, 0x468)
    assert blob[off:off + sz] == fileset()[0x4000:0xc000]
    (tmp / 'p').write_bytes(blob)
    r = subprocess.run([sys.executable, '-', str(tmp / 'p')], input=check, text=True, capture_output=True)
    assert r.returncode == 0, r.stderr
    assert mkpayload.build(native, tmp / 'a', 'cpus=1', boot_line_length=1024, prelinked_path=emit(fileset(), True))[0] == blob
    assert refused(kc, tmp / 'a', 'cpus=1', boot_line_length=1024)                           # fileset without --prelinked
    assert refused(native, tmp / 'a', 'cpus=1', boot_line_length=1024, prelinked_path=emit(fileset(), True, 'f' * 64))
    kc = emit(fileset(), True)
    (tmp / 'kc.prelinked').write_bytes(fileset() + b'x')                                       # report names other bytes
    assert refused(native, tmp / 'a', 'cpus=1', boot_line_length=1024, prelinked_path=kc)
    for bad in (fileset(flat=False), fileset(pc=LINK + 0x4010)):                               # not flat; pc outside kernel code
        assert refused(native, tmp / 'a', 'cpus=1', boot_line_length=1024, prelinked_path=emit(bad, True))


def payload_check():
    spec_t = importlib.util.spec_from_file_location('t_arm64e', ROOT / 'tools/test-fuxi-arm64e.py')
    t = importlib.util.module_from_spec(spec_t)
    spec_t.loader.exec_module(t)
    check = build_sh_check()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        raw = bytearray(t.macho(2, ('__text', '__thread_starts')))
        raw[0x3000:0x3017] = b'DEVELOPMENT_ARM64_FUXI\0'   # inside __TEXT, as the version string is
        (tmp / 'k').write_bytes(raw)
        (tmp / 'a').write_bytes(adt.serialize(fuxi27.build(cpus=1)))
        line = 'x' + ' ' * 700 + 'y'   # longer than v1 allows, fits v2
        v1d, _ = mkpayload.build(tmp / 'k', tmp / 'a', 'cpus=1')
        v1, i1 = mkpayload.build(tmp / 'k', tmp / 'a', 'cpus=1', boot_line_length=608)
        v2, i2 = mkpayload.build(tmp / 'k', tmp / 'a', line, boot_line_length=1024)
        assert v1 == v1d and i1['version'] == 1 and i2['version'] == 2
        for blob, ver, seg, cl in ((v1, 1, 0x2a8, b'cpus=1'), (v2, 2, 0x448, line.encode())):
            v, n = struct.unpack_from('<II', blob, 8)
            assert v == ver and blob[0x48:0x48 + len(cl)] == cl and blob[0x48 + len(cl)] == 0
            va, vs, off, sz = struct.unpack_from('<4Q', blob, seg)
            assert va == 0xfffffff007004000 and blob[off:off + 4] == b'\xcf\xfa\xed\xfe'
            (tmp / 'p').write_bytes(blob)
            r = subprocess.run([sys.executable, '-', str(tmp / 'p')], input=check, text=True, capture_output=True)
            assert r.returncode == 0, r.stderr
        try:
            mkpayload.build(tmp / 'k', tmp / 'a', 'z' * 1024, boot_line_length=1024)
            raise AssertionError('v2 accepted a 1024-byte command line')
        except SystemExit:
            pass
        bad = bytearray(v2)
        struct.pack_into('<I', bad, 8, 3)
        (tmp / 'p').write_bytes(bad)
        assert subprocess.run([sys.executable, '-', str(tmp / 'p')], input=check, text=True, capture_output=True).returncode
        prelinked_check(tmp, check)
        # --ramdisk then --trustcache: one segment each after the kernel, memory-map ranges point at them.
        mod = struct.pack('<I16sI', 2, b'\1' * 16, 1) + b'\2' * 20 + bytes(4)
        (tmp / 'tc').write_bytes(struct.pack('<2I', 1, 8) + mod)
        (tmp / 'rd').write_bytes(b'N4KROOT\0' + bytes(100))
        rt, it = mkpayload.build(tmp / 'k', tmp / 'a', 'rd=md0', ramdisk_path=tmp / 'rd', trustcache_path=tmp / 'tc',
                                 boot_line_length=1024)
        n = struct.unpack_from('<I', rt, 12)[0]
        segs = [struct.unpack_from('<4Q', rt, 0x448 + 32 * i) for i in range(n)]
        (rva, _, roff, rsz), (tva, tvs, toff, tsz) = segs[-2:]
        assert rt[roff:roff + 7] == b'N4KROOT' and rt[toff:toff + tsz] == (tmp / 'tc').read_bytes()
        assert tva >= rva + rsz and tva + tvs == int(it['kernel_end'], 16) and it['trustcache'][2] == 1
        adt_off, adt_size = struct.unpack_from('<2Q', rt, 0x20)
        root, _ = adt.parse(rt[adt_off:adt_off + adt_size])
        mmap = next(c for c in next(c for c in root.children if c.name == 'chosen').children if c.name == 'memory-map')
        assert struct.unpack('<2Q', mmap.prop('TrustCache')) == (int(it['trustcache'][0], 16), tsz)
        assert struct.unpack('<2Q', mmap.prop('RAMDisk'))[0] == int(it['ramdisk'][0], 16)
        (tmp / 'tc').write_bytes(struct.pack('<2I', 2, 12) + mod)
        try:
            mkpayload.build(tmp / 'k', tmp / 'a', 'x', trustcache_path=tmp / 'tc', boot_line_length=1024)
            raise AssertionError('a bundle with a bad module table was accepted')
        except SystemExit:
            pass
    loader = (ROOT / 'shim/fuxi-loader/loader.c').read_text()
    for need in ('#define BOOT_LINE_V1\t\t608', '#define BOOT_LINE_V2\t\t1024',
                 '_Static_assert(BOOT_ARGS_FLAGS(BOOT_LINE_V1) + 16 == 736',
                 '_Static_assert(BOOT_ARGS_FLAGS(BOOT_LINE_V2) + 16 == 1152',
                 '(p->version != 1 && p->version != 2)'):
        assert need in loader, need
    # 12377 boot_args: CommandLine[1024] at 108, then u64 bootFlags, memSizeActual -> 1136/1144/1152.
    flags = (108 + 1024 + 7) & ~7
    assert (flags, flags + 8, flags + 16) == (1136, 1144, 1152) and ((108 + 608 + 7) & ~7) + 16 == 736
    mon = (ROOT / 'tools/fuxi-xnu-monitor.py').read_text()
    assert "line = 1024 if struct.unpack_from('<I', payload, 8)[0] == 2 else 608" in mon
    print('PASS: payload v2 (cmdline[1024], segments at 0x448) and v1 (0x2a8, default byte-identical) pass the '
          'loader build check; version 3 and an over-long v2 line are refused; loader/monitor layouts match; '
          '--prelinked packs a flat MH_FILESET untouched (entry in kernel __TEXT_EXEC, report bound to both inputs, '
          'incomplete mapping only with --allow-incomplete)')


def thin_kernel(syms, filetype=2, at=0):
    """A minimal arm64 Mach-O carrying only LC_SYMTAB with external N_SECT symbols; `at` = its file offset in a
    fileset (LC_SYMTAB offsets are whole-file offsets there)."""
    strtab = b'\0' + b''.join(n.encode() + b'\0' for n in syms)
    nlist, off = b'', 1
    for n, v in syms.items():
        nlist += struct.pack('<IBBHQ', off, 0x0f, 1, 0, v)
        off += len(n) + 1
    symoff = at + 32 + 24
    hdr = struct.pack('<8I', 0xfeedfacf, 0x100000c, 0x80000002, filetype, 1, 24, 0, 0)
    cmd = struct.pack('<6I', 2, 24, symoff, len(syms), symoff + len(nlist), len(strtab))
    return hdr + cmd + nlist + strtab


def monitor_check():
    tree = ast.parse((ROOT / 'tools/fuxi-xnu-monitor.py').read_text())
    keep = {'WANTED', 'OPTIONAL', 'RING_DEFAULT'}
    nodes = [n for n in tree.body if (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in keep for t in n.targets))
             or (isinstance(n, ast.FunctionDef) and n.name == 'symbols')]
    ns = {'struct': struct}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<monitor symbols>', 'exec'), ns)
    old = {'_fuxi_early_status': 0xfffffff007800000, '_sweet_log_ring': 0xfffffff007900000, '_sweet_log_total': 0xfffffff007d00000}
    new = dict(old, _sweet_log_size=0xfffffff007010000)
    with tempfile.TemporaryDirectory() as tmp:
        k = Path(tmp) / 'k'
        k.write_bytes(thin_kernel(dict(old, _other=1)))
        assert ns['symbols'](k) == old
        k.write_bytes(thin_kernel(new))
        assert ns['symbols'](k) == new
        k.write_bytes(thin_kernel({'_sweet_log_ring': 1}))
        try:
            ns['symbols'](k)
            raise AssertionError('missing symbols accepted')
        except ValueError:
            pass
        # MH_FILESET: two LC_FILESET_ENTRY commands; the symbols come from the com.apple.kernel entry's LC_SYMTAB.
        inner = thin_kernel(new, at=0x8000)
        names = [b'com.apple.kec.corecrypto\0', b'com.apple.kernel\0']
        cmds, base = b'', 0x4000
        for i, name in enumerate(names):
            size = (32 + len(name) + 7) & ~7
            cmds += struct.pack('<IIQQI', 0x80000035, size, 0xfffffff007004000 + i * 0x4000, base * (i + 1), 32, ) + b'\0' * 4
            cmds += name.ljust(size - 32, b'\0')
        hdr = struct.pack('<8I', 0xfeedfacf, 0x100000c, 0xc0000002, 12, len(names), len(cmds), 0, 0)
        blob = bytearray((hdr + cmds).ljust(0x8000, b'\0'))
        other = thin_kernel({'_sweet_log_ring': 5}, at=0x4000)  # the corecrypto entry: the wrong place to look
        blob[0x4000:0x4000 + len(other)] = other
        blob += inner
        k.write_bytes(bytes(blob))
        assert ns['symbols'](k) == new
    mon = (ROOT / 'tools/fuxi-xnu-monitor.py').read_text()
    assert 'index = start % ring' in mon and '% 0x400000' not in mon
    print('PASS: monitor symbols() reads thin kernels (with and without _sweet_log_size) and the com.apple.kernel '
          'entry of an MH_FILESET; the ring size comes from _sweet_log_size')


def ci():
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        subprocess.run([sys.executable, str(ROOT / 'tools/ci/kernel-inputs.py'), '--variant', 'ios27', '--out', str(out)],
                       check=True, capture_output=True)
        assert sorted(p.name for p in out.iterdir()) == ['cmdline-cpus7.txt', 'cmdline.txt', 'fuxi27-cpus7.adt', 'fuxi27.adt']
        one, seven = (out / 'cmdline.txt').read_text(), (out / 'cmdline-cpus7.txt').read_text()
        assert ' cpus=1 ' in one and ' cpus=7 ' in seven and 'sheng-smp=1' in seven and 'sheng-smp' not in one
        assert len(seven.encode()) < 1024 and 'rd=' not in one   # K1: no root device yet
        assert len(adt.parse((out / 'fuxi27-cpus7.adt').read_bytes())[0].children[1].children) == 7
    sh = ['bash', str(ROOT / 'tools/ci/build.sh')]

    def run(*args):
        return subprocess.run(sh + list(args), check=True, capture_output=True, text=True).stdout
    assert run('--plan', 'ios27', 'false', 'false', 'false').strip() == '{"target": ["kernel-ios27"]}'
    assert run('--plan', 'all', 'false', 'false', 'false').strip() == '{"target": ["kernel-metal4", "kernel-wlan6g"]}'
    dry = run('kernel-ios27', '--dry-run')
    assert 'artifact=fuxi-kernel-ios27\n' in dry and XNU_SHA in dry and f'{len(series())} patches' in dry
    assert f'{LIBPTHREAD_TAG}={LIBPTHREAD_SHA}' in dry
    text = (ROOT / 'tools/ci/build.sh').read_text()
    body = text[text.index('\nkernel_ios27() {'):text.index('\nmesa_source() {')]
    assert 'python3 tools/test-fuxi-ios27.py' in body and 'bash tools/ci/build-xnu27.sh' in body
    assert 'KDK' not in body.replace('No KDK', '') and 'ipsw' not in body.lower()
    b27 = (ROOT / 'tools/ci/build-xnu27.sh').read_text()
    for need in ('MACHINE_CONFIGS=FUXI', 'ARCH_STRING_FOR_CURRENT_MACHINE_CONFIG=arm64e', 'XNU_KERNEL_CHAINED_FIXUPS=0',
                 '-find ld-classic', 'xnu27/apply.sh', '--variant ios27', 'DEVELOPMENT_ARM64_FUXI'):
        assert need in b27, need
    rel = b27[b27.index('# K2 matching reference'):]
    for need in ('KERNEL_CONFIGS=RELEASE', 'BUILD_LTO=1', 'OBJROOT="$robj"', '"${kflags[@]}"', 'MACHINE_CONFIGS=FUXI',
                 'FUXI_XNU27_RELEASE_REF', 'mach.release.fuxi.unstripped', 'release-reference.txt'):
        assert need in rel or need in b27, need
    assert b27.count('"${kflags[@]}"') == 4 and 'BUILD_LTO=0' in b27 and 'BUILD_LTO=0' not in rel
    # K2 prelink input: the same DEVELOPMENT objects relinked with split-seg info (a later make assignment wins).
    chained = b27[b27.index('# K2 prelink input'):b27.index('# K2 matching reference')]
    assert 'XNU_KERNEL_CHAINED_FIXUPS=1' in chained and chained.index('"${kflags[@]}"') < chained.rindex('XNU_KERNEL_CHAINED_FIXUPS=1')
    assert 'OBJROOT' not in chained and '"${common[@]}"' in chained and 'FUXI_XNU27_CHAINED' in chained
    assert 'mach.development.fuxi.chained.unstripped' in chained and '0x1e in cmds' in chained and '0x80000034 in cmds' in chained
    assert b27.index('cp "$kdir/mach.development.fuxi" "$out/"') < b27.index('# K2 prelink input')
    assert not re.search(r'kernelcache|\.ipsw|KDK|curl|wget', b27.split('\nset -euo pipefail', 1)[1], re.I)
    wf = (ROOT / '.github/workflows/fuxi-build.yml').read_text()
    assert 'options: [none, metal4, wlan6g, metal4e, ios27, all]' in wf and "'xnu27/sources.env'" in wf
    assert wf.count("matrix.target != 'kernel-ios27'") == 3
    print('PASS: kernel-inputs ios27 (fuxi27 ADTs, cpus=1 and cpus7+sheng-smp lines < 1024), CI plan ios27 only '
          '(not in all), dry-run pins, workflow option and iig steps skipped for ios27, chained relink (split-seg info), RELEASE reference build (LTO, own OBJROOT)')


# K4: tools/test-fuxi-ios27.py mirrors iokit/Kernel/arm/fuxi_root27.h (xnu27/patches/fuxi/0007) in Python and
# runs the header itself, built with the host cc, against the same cases.
K4_PATCH = 'fuxi/0007-root27-storage.patch'
FX_OK, FX_E_TABLE, FX_E_EXTENT, FX_E_OVERLAP, FX_E_SHORT, FX_E_MAGIC, FX_E_COUNT, FX_E_WINDOW, FX_E_SIZE, FX_E_MAP = range(10)
FX_BLOCK, FX_MIB, FX_BOUNCE, FX_MAX_EXTENTS = 4096, 1 << 20, 1 << 20, 1024
# iOS IOStorageFamily-337.100.1 slot order (TARGET_OS_OSX 0, no vtable padding): new virtuals only.
IOSTORAGE_SLOTS = ['open', 'read', 'write', 'read', 'write', 'unmap', 'lockPhysicalExtents', 'copyPhysicalExtent',
                   'unlockPhysicalExtents', 'setPriority', 'synchronize', 'getProvisionStatus']
IOMEDIA_SLOTS = ['getPreferredBlockSize', 'getSize', 'getBase', 'isEjectable', 'isFormatted', 'isWhole', 'isWritable',
                 'getContent', 'getContentHint', 'init', 'getAttributes']


def fx_load_extents(table, want, part_blocks):
    if not table or len(table) % 24 or len(table) // 24 > FX_MAX_EXTENTS or want == 0:
        return FX_E_TABLE, []
    ext, pos = [], 0
    for i in range(len(table) // 24):
        if pos >= want:
            break
        file, part, blocks = struct.unpack_from('<3Q', table, 24 * i)
        if file != pos or blocks == 0 or part >= part_blocks or blocks > part_blocks - part:
            return FX_E_EXTENT, []
        blocks = min(blocks, want - pos)
        if any(part < p + c and p < part + blocks for _, p, c in ext):
            return FX_E_OVERLAP, []
        ext.append((file, part, blocks))
        pos += blocks
    return (FX_OK, ext) if pos == want else (FX_E_SHORT, [])


def fx_io_allowed(ext, part_first, lba, count):
    return int(count > 0 and any(part_first + p <= lba < part_first + p + c and count <= c - (lba - part_first - p)
                                 for _, p, c in ext))


def fx_plan(ext, part_first, pos, left):
    """(err, lba, blocks, skip, want): one bounce-buffer read, never across an extent."""
    skip = pos % FX_BLOCK
    want = min(left, FX_BOUNCE - skip)
    blocks = -(-(skip + want) // FX_BLOCK)
    hit = next(((f, p, c) for f, p, c in ext if f <= pos // FX_BLOCK < f + c), None)
    if left == 0 or hit is None:
        return FX_E_MAP, 0, 0, 0, 0
    f, p, c = hit
    lblk = pos // FX_BLOCK
    run = c - (lblk - f)
    if blocks > run:
        blocks, want = run, run * FX_BLOCK - skip
    return FX_OK, part_first + p + lblk - f, blocks, skip, want


def fx_manifest(hdr, root_bytes):
    magic, ver, n, total = struct.unpack_from('<8sIIQ', hdr)
    if magic != b'SHNGROOT' or ver != 1:
        return FX_E_MAGIC, []
    if not 0 < n <= 31:
        return FX_E_COUNT, []
    if total > root_bytes:
        return FX_E_SIZE, []
    wins, prev = [], FX_MIB
    for i in range(n):
        nb, off, size, bs, flags, d, _ = struct.unpack_from('<32sQQII32s40s', hdr, 24 + 128 * i)
        name = nb.split(b'\0')[0]
        if (not name or b'\0' not in nb or off % FX_MIB or off < prev or size == 0 or size % 512 or off > total
                or size > total - off or bs != 512):
            return FX_E_WINDOW, []
        wins.append((name.decode(), off, size, flags, fx_uuid(d)))
        prev = off + size
    return FX_OK, wins


def fx_uuid(sha):
    u = bytearray(sha[:16])
    u[6] = (u[6] & 0x0f) | 0x40
    u[8] = (u[8] & 0x3f) | 0x80
    return str(uuid.UUID(bytes=bytes(u))).upper()


K4_HARNESS = r'''
#include <stdio.h>
#include <stdlib.h>
#include "fuxi_root27.h"
static unsigned char buf[1 << 16];
int main(int argc, char **argv)
{
	if (argc < 4) {
		return 2;
	}
	FILE *f = fopen(argv[2], "rb");
	size_t n = fread(buf, 1, sizeof(buf), f);
	fclose(f);
	if (argv[1][0] == 'e') {
		static struct fx27_extent ext[FX27_MAX_EXTENTS];
		unsigned count = 99;
		unsigned long long want = strtoull(argv[3], 0, 0), part = strtoull(argv[4], 0, 0), first = strtoull(argv[5], 0, 0);
		int e = fx27_load_extents(n ? buf : 0, n, want, part, ext, &count);
		printf("%d %u\n", e, count);
		for (unsigned i = 0; i < count; i++) {
			printf("%llu %llu %llu\n", (unsigned long long)ext[i].file, (unsigned long long)ext[i].part,
			    (unsigned long long)ext[i].count);
		}
		unsigned long long a, b;
		while (scanf("%llu %llu", &a, &b) == 2) {
			struct fx27_io io = { 0, 0, 0, 0 };
			int r = fx27_plan(ext, count, first, a, b, &io);
			printf("%d %llu %u %u %u %d %d\n", r, r ? 0ULL : (unsigned long long)io.lba, r ? 0 : io.blocks,
			    r ? 0 : io.skip, r ? 0 : io.want, fx27_io_allowed(ext, count, first, a, b),
			    r ? 0 : fx27_io_allowed(ext, count, first, io.lba, io.blocks));
		}
	} else {
		struct fx27_window win[FX27_MAX_WINDOWS];
		unsigned count = 99;
		char u[37];
		int e = fx27_manifest(buf, strtoull(argv[3], 0, 0), win, &count);
		printf("%d %u\n", e, count);
		for (unsigned i = 0; i < count; i++) {
			fx27_window_uuid(win[i].sha256, u);
			printf("%s %llu %llu %u %s\n", win[i].name, (unsigned long long)win[i].offset,
			    (unsigned long long)win[i].size, win[i].flags, u);
		}
	}
	return 0;
}
'''


def patch_new_file(patch, path):
    """The content of a file the patch creates."""
    body = patch.split(f'+++ b/{path}\n', 1)[1].split('\ndiff --git', 1)[0]
    return ''.join(l[1:] + '\n' for l in body.split('\n')[1:] if l.startswith('+'))


def k4_patch_check():
    patch = (X27 / 'patches' / K4_PATCH).read_text()
    files = re.findall(r'^\+\+\+ b/(\S+)', patch, re.M)
    assert files == ['bsd/kern/bsd_init.c', 'iokit/Kernel/arm/FuxiRoot27.cpp', 'iokit/Kernel/arm/fuxi_root27.h',
                     'iokit/Kernel/arm/fuxi_storage_abi.h', 'iokit/conf/files.arm64', 'bsd/vfs/vfs_subr.c'], files
    assert not re.search(r'^-', patch.replace('\n--- ', '\n'), re.M), 'K4 only adds lines'
    drv = patch_new_file(patch, 'iokit/Kernel/arm/FuxiRoot27.cpp')
    abi = patch_new_file(patch, 'iokit/Kernel/arm/fuxi_storage_abi.h')
    hdr = patch_new_file(patch, 'iokit/Kernel/arm/fuxi_root27.h')
    assert drv.split('*/\n', 1)[1].startswith('#if defined(FUXI)\n') and drv.rstrip().endswith('#endif /* FUXI */')
    init = patch.split('+++ b/bsd/kern/bsd_init.c\n', 1)[1].split('\ndiff --git', 1)[0]
    assert '+#if defined(FUXI)\n+\tfuxi_root27_start();\n+#endif' in init
    # fuxi_root27_nomockfs=1 drops mockfs from vfs_mountroot's candidates; every added line is FUXI-gated.
    vfs = patch.split('+++ b/bsd/vfs/vfs_subr.c\n', 1)[1]
    added = [l[1:] for l in vfs.splitlines() if l.startswith('+')]
    assert added.count('#if defined(FUXI)') == 5 == added.count('#endif'), added
    assert 'PE_parse_boot_argn("fuxi_root27_nomockfs", &fuxi_nomockfs, sizeof(fuxi_nomockfs));' in vfs
    assert 'if (fuxi_nomockfs == 1 && strcmp(vfsp->vfc_name, "mockfs") == 0) {' in vfs
    # fuxi_root27_hiderootfs=1 clears MNT_ROOTFS only around the mountroot call and restores it.
    assert "mp->mnt_flag &= ~MNT_ROOTFS;" in vfs and "mp->mnt_flag |= MNT_ROOTFS;" in vfs
    # Read-only transport: READ(10), TEST UNIT READY and the device-WLUN ACTIVE power command only; once the
    # extents are loaded every READ(10) is checked against them; no write opcode is ever built.
    scsi = drv[drv.index('ufs_scsi(const uint8_t'):drv.index('struct utrd *utrd')]
    assert 'cdb[0] == 0x28' in scsi and 'cdb[0] == 0x1b' in scsi and 'cdb[0] == 0x00' in scsi
    assert '!fr.guard ||\n\t    fx27_io_allowed(' in scsi and 'kIOReturnNotPermitted' in scsi
    assert not re.search(r'0x2a|0x8a|0x35|0x42|0x9[13]', drv), 'write/sync/unmap opcode in the K4 driver'
    assert "ucd->cmd[1] = len ? 0x40 : 0x00;" in drv and '(len ? 2u : 0u) << 25' in drv   # data in only
    assert drv.count('fx27_io_allowed(') == 2 and 'fr.guard = true;' in drv
    assert 'media_create(meta, w->size, 512, true, NULL, NULL)' in drv and 'media_create(meta, w->size, 512, false, hint, pp)' in drv
    assert 'm->init(0, size, block, 0, whole, media_rw != 0, hint, props)' in drv and 'static uint32_t media_rw;' in drv   # isWritable only with fuxi_root27_rwmedia=1; write() refuses either way
    assert 'return kIOReturnNotWritable;' in drv and 'kIOReturnNotWritable, 0);' in drv
    for need in ('"fuxi_root27"', '"fuxi_root27_hint"', '"7C3457EF-0000-11AA-AA11-00306543ECAC"', '"fuxi-root-extents"',
                 '"fuxi-root-blocks"', '"/fuxi-ufs"', '"GUID_partition_scheme"', 'part->setLocation("1");',
                 'copyMetaClassWithName(name)', 'SHA256_Final(digest, &ctx);', 'fx27_manifest(hdr, fr.root_blocks * FX27_BLOCK'):
        assert need in drv, need
    # The ABI mirror: Apple's APSL header and source named; slots in IOStorageFamily's iOS order.
    assert abi.startswith('/*\n * Copyright (c) 1998-2015 Apple Inc.') and '@APPLE_LICENSE_HEADER_START@' in abi
    assert 'IOStorageFamily-337.100.1' in abi and 'apple-oss-distributions/IOStorageFamily' in abi
    body = lambda cls: abi.split(f'\nclass {cls} : ', 1)[1].split('\n};', 1)[0]
    slots = lambda cls: re.findall(r'virtual [^(~]*?\b(\w+)\(', body(cls))
    assert slots('IOStorage') == IOSTORAGE_SLOTS and slots('IOMedia') == IOMEDIA_SLOTS
    assert '= 0;' in body('IOMedia') and 'void *_expansionData;' in body('IOStorage')
    for t in ('UInt32 IOStorageAccess', 'UInt8 IOStoragePriority', 'UInt32 IOStorageSynchronizeOptions',
              'UInt64 IOStorageGetProvisionStatusOptions', 'UInt32 IOMediaAttributeMask', 'UInt32 IOStorageUnmapOptions'):
        assert f'typedef {t};' in abi, t
    assert 'static const OSMetaClass &gMetaClass;' in abi and 'const OSMetaClass &IOStorage::gMetaClass = IOService::gMetaClass;' in drv
    assert '#include <' not in hdr.replace('#include <stdint.h>', '')
    notice = (X27 / 'NOTICE').read_text()
    assert 'fuxi/0007-root27-storage.patch' in notice and 'IOStorageFamily-337.100.1' in notice
    return hdr


def k4_logic_check(hdr):
    rng = random.Random(27)
    spec_r = importlib.util.spec_from_file_location('ios27_root', ROOT / 'tools/ios27-root.py')
    root = importlib.util.module_from_spec(spec_r)
    spec_r.loader.exec_module(root)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / 'fuxi_root27.h').write_text(hdr)
        (tmp / 'h.c').write_text(K4_HARNESS)
        subprocess.run(['cc', '-std=c11', '-O1', '-Wall', '-Wextra', '-Werror', '-o', str(tmp / 'h'), str(tmp / 'h.c')],
                       check=True, cwd=tmp)

        def run(*args, stdin=''):
            return subprocess.run([str(tmp / 'h')] + [str(a) for a in args], input=stdin, capture_output=True,
                                  text=True, check=True).stdout.split('\n')

        cases = 0
        for trial in range(300):
            part_blocks, part_first = rng.randrange(64, 5000), rng.randrange(0, 1 << 20)
            n = rng.randrange(1, 12)
            cuts = sorted(rng.sample(range(1, part_blocks), 2 * n))   # disjoint runs inside the partition
            triples, pos = [], 0
            for i in range(n):
                count = cuts[2 * i + 1] - cuts[2 * i]
                triples.append([pos, cuts[2 * i], count])
                pos += count
            rng.shuffle(triples) if trial % 7 == 0 else None
            want = pos - rng.randrange(0, triples[-1][2]) if trial % 3 else pos
            kind = trial % 11
            if kind == 1:
                triples[-1][1] = triples[0][1]                                 # overlap (or out of order)
            elif kind == 2:
                triples[-1][2] = part_blocks                                   # past the partition end
            elif kind == 3:
                want = pos + 1                                                 # extents end early
            elif kind == 4 and n > 1:
                triples[1][0] += 1                                             # gap in file blocks
            elif kind == 5:
                triples[0][2] = 0
            table = struct.pack(f'<{3 * len(triples)}Q', *sum(triples, []))
            if kind == 6:
                table = table[:-8]
            err, ext = fx_load_extents(table, want, part_blocks)
            queries = []
            for _ in range(40):
                p = rng.randrange(0, want * FX_BLOCK + 2)
                queries.append((p, rng.choice((1, 511, 4096, 5000, 1 << 20, 3 << 20, rng.randrange(1, 1 << 22)))))
            queries += [(want * FX_BLOCK, 1), (0, 0)]
            (tmp / 't').write_bytes(table)
            out = run('e', tmp / 't', want, part_blocks, part_first,
                      stdin=''.join(f'{a} {b}\n' for a, b in queries))
            assert out[0] == f'{err} {len(ext)}', (trial, out[0], err)
            assert out[1:1 + len(ext)] == [f'{f} {p} {c}' for f, p, c in ext], trial
            for (a, b), line in zip(queries, out[1 + len(ext):]):
                r = fx_plan(ext, part_first, a, b)
                lba_ok = fx_io_allowed(ext, part_first, r[1], r[2]) if r[0] == FX_OK else 0
                assert line == ' '.join(map(str, r + (fx_io_allowed(ext, part_first, a, b), lba_ok))), (trial, a, b, line, r)
                assert r[0] != FX_OK or lba_ok == 1        # every planned read passes the guard
                cases += 1
            if err == FX_OK and trial % 10 == 0:
                # A read of the whole file through the plans returns the file, never touching other blocks.
                disk = bytearray(rng.randbytes((part_blocks) * FX_BLOCK))
                expect = b''.join(disk[p * FX_BLOCK:(p + c) * FX_BLOCK] for _, p, c in sorted(ext))
                got, at = bytearray(), rng.randrange(0, FX_BLOCK)
                while at + len(got) < want * FX_BLOCK:
                    e, lba, blocks, skip, w = fx_plan(ext, 0, at + len(got), want * FX_BLOCK - at - len(got))
                    assert e == FX_OK and fx_io_allowed(ext, 0, lba, blocks) and skip + w <= blocks * FX_BLOCK <= FX_BOUNCE
                    got += disk[lba * FX_BLOCK + skip:lba * FX_BLOCK + skip + w]
                assert bytes(got) == expect[at:]
        # Manifests from ios27-root.py image, then damaged copies.
        wins = []
        for name, size in (('OS', 3 << 20), ('SystemOS', 512), ('AppOS', (1 << 20) + 4096)):
            (tmp / name).write_bytes(rng.randbytes(size))
            wins.append((name, str(tmp / name), name != 'AppOS'))
        ents = root.image_build(str(tmp / 'img'), wins, log=lambda *a: None)
        good = (tmp / 'img').read_bytes()[:4096]
        total = (tmp / 'img').stat().st_size
        mans = [(good, total), (good, total - 1), (good, total + (1 << 30))]
        for off, val in ((0, b'X'), (8, b'\2'), (12, b'\0'), (12, b'\40'), (24, b'\0'), (24 + 31, b'A'), (24 + 32, b'\1'),
                         (24 + 128 + 40, b'\1'), (24 + 128 + 48, b'\0\x10'), (24 + 256 + 32, b'\0'), (24 + 256 + 40, b'\xff')):
            bad = bytearray(good)
            bad[off:off + len(val)] = val
            mans.append((bytes(bad), total))
        for blob, rb in mans:
            (tmp / 'm').write_bytes(blob)
            err, w = fx_manifest(blob, rb)
            out = run('m', tmp / 'm', rb)
            assert out[0] == f'{err} {len(w)}' and out[1:1 + len(w)] == [' '.join(map(str, x)) for x in w], (out, err, w)
            cases += 1
        err, w = fx_manifest(good, total)
        assert err == FX_OK and [(x[0], x[1], x[2], bool(x[3] & 1)) for x in w] == [
            (e['name'], e['offset'], e['size'], e['read_only']) for e in ents]
        assert [x[4] for x in w] == [fx_uuid(bytes.fromhex(e['sha256'])) for e in ents]
        assert fx_manifest(mans[1][0], total - 1)[0] == FX_E_SIZE
    return cases


def k4_adt_check():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        ext, pos, phys = [], 0, 0x7f00000000 // 4096 * 4096
        for i in range(366):                                    # the observed extent count of the root file
            ln = (11 << 20) if i < 365 else (4 << 30) - pos
            ext.append([pos, phys, ln])
            pos, phys = pos + ln, phys + ln + (1 << 20)
        (tmp / 'e.json').write_text(json.dumps({'size': pos, 'extents': ext}))
        vol = '0b5e6f2a-1c3d-4e5f-8a9b-0c1d2e3f4a5b'
        rp = fuxi27.fuxi.root_extent_props(tmp / 'e.json', 4 << 30)
        for cpus in (1, 7):
            blob = adt.serialize(fuxi27.build(cpus=cpus, seed=bytes(256), root_props=rp, root_uuid=vol))
            assert 0x4000 < len(blob) <= fuxi27.ADT_MAX_ROOT == mkpayload.ADT_MAX[2], len(blob)
            root, end = adt.parse(blob)
            ch = props(child(root, 'chosen'))
            words = struct.unpack(f'<{len(ch["fuxi-root-extents"]) // 8}Q', ch['fuxi-root-extents'])
            assert [list(words[i:i + 3]) for i in range(0, len(words), 3)] == [[a // 4096, b // 4096, c // 4096] for a, b, c in ext]
            assert struct.unpack('<Q', ch['fuxi-root-blocks'])[0] == (4 << 30) // 4096
            err, kept = fx_load_extents(ch['fuxi-root-extents'], (4 << 30) // 4096, 1 << 40)
            assert err == FX_OK and len(kept) == 366
            xml = ch['root-matching']
            assert xml.endswith(b'</dict></dict>\0') and xml.count(b'\0') == 1
            m = ElementTree.fromstring(xml[:-1])
            kv = dict(zip([k.text for k in m.findall('key')], m.findall('*')[1::2]))
            assert kv['IOProviderClass'].text == 'IOMedia'
            inner = kv['IOPropertyMatch']
            assert [e.text for e in inner] == ['UUID', vol.upper()]
            ufs = props(child(root, 'fuxi-ufs'))
            assert struct.unpack('<2Q', ufs['reg']) == (0x1d84000, 0x3000)
        plain = adt.parse(adt.serialize(fuxi27.build(cpus=7)))[0]
        assert not any(c.name == 'fuxi-ufs' for c in plain.children)
        assert not {'fuxi-root-extents', 'fuxi-root-blocks', 'root-matching'} & set(props(child(plain, 'chosen')))
        tool = [sys.executable, str(ROOT / 'tools/mkadt-fuxi27.py'), str(tmp / 'o.adt')]
        r = subprocess.run(tool + ['--root-extents', str(tmp / 'e.json'), '--root-bytes', str(4 << 30), '--root-uuid', vol],
                           capture_output=True, text=True)
        assert r.returncode == 0 and 'root 366 extents' in r.stderr, r.stderr
        assert 'system-volume-auth-blob' not in props(child(adt.parse((tmp / 'o.adt').read_bytes())[0], 'chosen'))
        blob_in = bytes(range(229))                              # synthetic; the real root hash stays local
        (tmp / 'rh').write_bytes(blob_in)
        r = subprocess.run(tool + ['--root-extents', str(tmp / 'e.json'), '--root-bytes', str(4 << 30), '--root-uuid', vol,
                                   '--root-hash', str(tmp / 'rh')], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        assert props(child(adt.parse((tmp / 'o.adt').read_bytes())[0], 'chosen'))['system-volume-auth-blob'] == blob_in
        assert not any(c.name == 'filesystems' for c in adt.parse((tmp / 'o.adt').read_bytes())[0].children)
        r = subprocess.run(tool + ['--root-extents', str(tmp / 'e.json'), '--root-bytes', str(4 << 30), '--fstab'],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        fstab = child(child(adt.parse((tmp / 'o.adt').read_bytes())[0], 'filesystems'), 'fstab')
        assert props(fstab)['os_env_type'] == struct.pack('<I', 1) and props(fstab)['max_fs_entries'] == struct.pack('<I', 1)
        sysvol = props(child(fstab, 'system-vol'))
        assert (sysvol['vol.fs_file'], sysvol['vol.fs_type'], sysvol['vol.fs_role']) == (b'/\0', b'ro\0', struct.pack('<I', 1))
        for bad in (['--root-uuid', vol], ['--root-extents', str(tmp / 'e.json')],
                    ['--root-extents', str(tmp / 'e.json'), '--root-bytes', str((4 << 30) + 4096)],
                    ['--root-extents', str(tmp / 'e.json'), '--root-bytes', str(4 << 30), '--root-uuid', 'nope']):
            assert subprocess.run(tool + bad, capture_output=True).returncode, bad
        # The loader copies at most 64 KiB of ADT for v2 payloads and still 16 KiB for v1; mkpayload agrees.
        spec_t = importlib.util.spec_from_file_location('t_arm64e', ROOT / 'tools/test-fuxi-arm64e.py')
        t = importlib.util.module_from_spec(spec_t)
        spec_t.loader.exec_module(t)
        raw = bytearray(t.macho(2, ('__text', '__thread_starts')))
        raw[0x3000:0x3017] = b'DEVELOPMENT_ARM64_FUXI\0'
        (tmp / 'k').write_bytes(raw)
        blob, _ = mkpayload.build(tmp / 'k', tmp / 'o.adt', 'fuxi_root27=1', boot_line_length=1024)
        adt_off, adt_size = struct.unpack_from('<2Q', blob, 0x20)
        assert adt_size == (tmp / 'o.adt').stat().st_size > 0x4000
        (tmp / 'p').write_bytes(blob)
        r = subprocess.run([sys.executable, '-', str(tmp / 'p')], input=build_sh_check(), text=True, capture_output=True)
        assert r.returncode == 0, r.stderr
        try:
            mkpayload.build(tmp / 'k', tmp / 'o.adt', 'x', boot_line_length=608)
            raise AssertionError('a v1 payload took a 20 KiB ADT')
        except SystemExit:
            pass
    loader = (ROOT / 'shim/fuxi-loader/loader.c').read_text()
    assert '#define ADT_MAX_V2\t\t(4 * PAGE_16K)' in loader and fuxi27.ADT_MAX_ROOT == 4 * 0x4000
    assert 'const uint64_t adt_max = p->version == 1 ? PAGE_16K : ADT_MAX_V2;' in loader
    assert 'p->kernel_end - p->virt_base > p->mem_size - adt_max - PAGE_16K ||' in loader
    assert '!p->adt_size || p->adt_size > adt_max ||' in loader and 'p->adt_size > PAGE_16K' not in loader


def k4_check():
    hdr = k4_patch_check()
    cases = k4_logic_check(hdr)
    k4_adt_check()
    print(f'PASS: K4 root storage: fuxi/0007 is FUXI-gated, read-only (READ(10)/TUR/WLUN power only, extent guard), '
          f'IOStorage/IOMedia mirror in iOS slot order; fuxi_root27.h (host cc) = Python mirror on {cases} '
          'extent/plan/guard/manifest cases, whole-file reads stay inside the extents, ios27-root.py manifests parse; '
          'mkadt-fuxi27 --root-extents/--root-bytes/--root-uuid/--root-hash/--fstab round-trip (366 extents, root-matching XML, '
          '/fuxi-ufs, system-volume-auth-blob), --fstab system-vol at /, '
          'v2 payload ADT up to 64 KiB, v1 still 16 KiB')


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--xnu', type=Path, help='git clone of apple-oss-distributions/xnu containing ' + XNU_SHA)
    ap.add_argument('--libpthread', type=Path, help='libpthread checkout at ' + LIBPTHREAD_SHA)
    args = ap.parse_args()
    layout()
    pthread_check()
    adt_check()
    payload_check()
    monitor_check()
    ci()
    k4_check()
    if args.xnu:
        apply_check(args.xnu.resolve())
    if args.libpthread:
        libpthread_apply_check(args.libpthread.resolve())


if __name__ == '__main__':
    main()
