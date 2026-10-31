#!/usr/bin/env python3
"""Host checks for the iOS 27 kernel lane (xnu27/, docs-lanes/ios27.md). Linux, no Apple toolchain.

  - xnu27/series lists exactly the committed patches, the UniXNU layers keep their origin/licence notices,
    UniXNU's source-corecrypto patch and any corecrypto source stay out, the component sources match the
    sha256 table, and the Fuxi patches keep the USB-monitor symbol contract (_fuxi_early_status,
    _sweet_log_ring, 256 KiB since fuxi/0006 and exported as _sweet_log_size, _sweet_log_total) and gate their crypto/console code on FUXI;
  - mkadt-fuxi27.py builds the tree ArmDTPlatform/ArmCpu/12377 read: 256-byte random-seed, sha2-384,
    kernel-ctrr-to-be-enabled 0, a valid CHRP NVRAM bank, cpu nodes ('cpu' + u32 MPIDR), /arm-io ranges and a
    GICv3 intc ArmCpu accepts, /psci only with secondaries, no SHA-1-hashed chip ids, <= 16 KiB;
  - payload v2: mkpayload --boot-line-length 1024 puts the segment table at 0x448, v1 stays 0x2a8 and
    byte-identical to the default; shim/fuxi-loader/build.sh's payload check accepts both; loader.c carries
    the v1/v2 boot_args layouts (CommandLine at 108, 736 / 1152 bytes);
  - kernel-inputs.py --variant ios27 and the CI plan/dry-run/workflow plumbing (ios27 not in all).

  --xnu DIR   also apply the series (xnu27/apply.sh) to a temporary worktree of DIR, a git clone of
              apple-oss-distributions/xnu containing xnu-12377.121.6 (ac9718f), and check the result
"""
import argparse
import ast
import hashlib
import re
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

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
    assert listed[-6:] == ['fuxi/0001-machine-config.patch', 'fuxi/0002-builtin-crypto.patch',
                           'fuxi/0003-console.patch', 'fuxi/0004-components.patch', 'fuxi/0005-crypto-kext-handoff.patch',
                           'fuxi/0006-log-ring-fileset.patch']
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
    print(f'PASS: series = {len(listed)} committed patches (51 UniXNU verbatim, 2 sheng, sm8550, 6 Fuxi), notices and '
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
                      'pexpert/pexpert/arm64/BCM2837.h'):
                assert (tree / f).is_file(), f
            assert 'BOOT_LINE_LENGTH 1024' in (tree / 'pexpert/pexpert/arm64/boot.h').read_text()
            assert re.search(r'SUPPORTED_ARM64_MACHINE_CONFIGS = .*\bFUXI\b', (tree / 'makedefs/MakeInc.def').read_text())
        finally:
            subprocess.run(['git', '-C', str(xnu), 'worktree', 'remove', '--force', str(tree)], check=True)
    print('PASS: xnu27/apply.sh applies the whole series to xnu-12377.121.6 and stages the components')


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
          'loader build check; version 3 and an over-long v2 line are refused; loader/monitor layouts match')


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
    assert b27.count('"${kflags[@]}"') == 3 and 'BUILD_LTO=0' in b27 and 'BUILD_LTO=0' not in rel
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


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--xnu', type=Path, help='git clone of apple-oss-distributions/xnu containing ' + XNU_SHA)
    args = ap.parse_args()
    layout()
    adt_check()
    payload_check()
    monitor_check()
    ci()
    if args.xnu:
        apply_check(args.xnu.resolve())


if __name__ == '__main__':
    main()
