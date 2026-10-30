#!/usr/bin/env python3
"""Host checks for the opt-in arm64e kernel (CI kernel=metal4e, xnu/patches/0115-fuxi-arm64e.patch).

  - 0115 touches only its six files; every added assembler/header line sits in an added HAS_NATIVE_PAC
    (or FUXI && __arm64e__) block, so the arm64 FUXI/SWEET kernels compile exactly as before;
  - the MakeInc.def hunks, run through GNU make: FUXI + FUXI_ARM64E=1 is arm64e with -march=armv8.3-a and the
    ld-classic -threaded_starts_section link, every other combination keeps arm64/armv8.2-a/-kernel;
  - the fuxi_native_pac layout in pac_asm.h equals FP_NATIVE_* in fuxi_pac_logic.h and the PACGA modifier;
  - PAC_INIT_KEY_STATE assembles (clang-18, Mach-O, armv8.3-a) to the expected
    sequence: splitmix64 constants, ten key writes in APIA..APGA Lo/Hi order, ten readbacks, one PACGA, a WFE
    park, cache cleans of the three lines, every access inside the 24-word block;
  - mkpayload refuses arm64e kernels it cannot boot correctly (auth chained fixups, no __thread_starts);
  - the CI plan/dry-run/workflow/kernel-inputs plumbing, with metal4/wlan6g/all unchanged.

  - 0116 (user JOP for arm64 userland): only FUXI && __arm64e__ blocks in kern_machdep.c/kern_exec.c, nothing
    removed; fuxi_arm64e_grade compiled and run: arm64e slices grade 8 (below arm64 10 / arm64_all 9) unless
    fuxiarm64e=1, an unacceptable slice stays 0.

  --tree DIR   also dry-run 0115 and 0116 with `patch -p1` in DIR, an xnu-8796.141.3 tree with 0001..0114
               applied, and compile DIR's real grade_binary with 0116 for a host-subtype-arm64e fat-binary choice
"""
import argparse
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATCH_PATH = ROOT/'xnu/patches/0115-fuxi-arm64e.patch'
PATCH = PATCH_PATH.read_text()
FILES = ['iokit/Kernel/arm/SweetCPU.cpp', 'makedefs/MakeInc.def', 'osfmk/arm64/machine_routines_asm.s',
         'osfmk/arm64/pac_asm.h', 'osfmk/arm64/start.s', 'pexpert/pexpert/arm64/SWEET.h']
JOP_PATH = ROOT/'xnu/patches/0116-fuxi-arm64e-user-jop.patch'
JOP = JOP_PATH.read_text()
JOP_FILES = ['bsd/dev/arm/kern_machdep.c', 'bsd/kern/kern_exec.c']
GATE = '#if defined(FUXI) && defined(__arm64e__)'
sys.path.insert(0, str(ROOT/'tools'))
import mkpayload  # noqa: E402


def section(path, patch=PATCH):
    m = re.search(r'^--- a/' + re.escape(path) + r'\n\+\+\+ b/' + re.escape(path) + r'\n(.*?)(?=^--- a/|\Z)', patch,
                  re.M | re.S)
    assert m, path
    return m[1]


def hunks(path, patch=PATCH):
    return [h.splitlines()[1:] for h in re.split(r'^(?=@@ )', section(path, patch), flags=re.M) if h.startswith('@@')]


def new_side(lines):
    return [l[1:] for l in lines if l[:1] in ' +']


def structure():
    assert re.findall(r'^--- a/(\S+)$', PATCH, re.M) == FILES, 'one sorted section per file'
    assert re.findall(r'^\+\+\+ b/(\S+)$', PATCH, re.M) == FILES
    assert re.findall(r'^-(?!--).*$', PATCH, re.M) == [
        '-\tvm_offset_t entry = ml_static_vtop((vm_offset_t)&sweet_secondary_entry);'], '0115 only adds lines (but one)'
    for prior in sorted(PATCH_PATH.parent.glob('*.patch')):
        if prior != PATCH_PATH:
            text = prior.read_text(errors='surrogateescape')
            assert 'HAS_NATIVE_PAC' not in text and 'FUXI_ARM64E' not in text, prior.name
            # 0114's live mode only reads the block: under __arm64e__, which only 0115's MakeInc.def selects.
            assert 'fuxi_native_pac' not in text or (prior.name.startswith('0114-') and
                                                     'extern uint64_t fuxi_native_pac[FP_NATIVE_WORDS];' in text), prior.name
    gates = ('#if HAS_NATIVE_PAC', '#if defined(FUXI) && defined(__arm64e__)')
    for path in FILES[2:]:
        for h in hunks(path):
            depth = 0
            for l in h:
                if not l.startswith('+'):
                    assert depth == 0, (path, 'context inside an added block', l)
                    continue
                s = l[1:].strip()
                if depth == 0:
                    assert s == '' or s in gates or s.startswith(('/*', '*', '//')), (path, 'added outside a gate', l)
                    depth += s in gates
                elif s.startswith('#if'):
                    depth += 1
                elif s.startswith('#endif'):
                    depth -= 1
            assert depth == 0, path
    sweet = ''.join(l[1:] + '\n' for h in hunks(FILES[0]) for l in h if l.startswith('+'))
    assert '#include <ptrauth.h>' in sweet and 'ptrauth_strip((void *)&sweet_secondary_entry, ptrauth_key_function_pointer)' in sweet
    header = ''.join(l[1:] + '\n' for h in hunks(FILES[5]) for l in h if l.startswith('+'))
    assert re.search(r'#if defined\(FUXI\) && defined\(__arm64e__\)\n#define HAS_APPLE_PAC +1\n#define HAS_NATIVE_PAC +1\n#endif',
                     header), header
    mr = '\n'.join(new_side([l for h in hunks(FILES[2]) for l in h]))
    assert re.search(r'LEXT\(ml_enable_user_jop_key\)\n#if HAS_NATIVE_PAC\n(?:\t/\*.*\*/\n)?\tmov\t\tx0, #0\n\tret\n#endif', mr)
    assert re.search(r'LEXT\(ml_disable_user_jop_key\)\n#if HAS_NATIVE_PAC\n\tret\n#endif', mr)
    start = '\n'.join(new_side([l for h in hunks(FILES[4]) for l in h]))
    assert 'orr\t\tx1, x1, #(3 << 40)\n#endif\n\tmsr\t\tHCR_EL2, x1' in start
    assert re.search(r'\t\.align 6\n\t\.globl EXT\(fuxi_native_pac\)\nLEXT\(fuxi_native_pac\)\n\t\.space\tNATIVE_PAC_WORDS \* 8, 0', start)
    print('PASS: 0115 adds only gated lines to its six files; jop-key fall-through, HCR_EL2.API/APK, ptrauth_strip')


def make_eval():
    if not shutil.which('make'):
        print('SKIP: no make; MakeInc.def hunks not evaluated')
        return
    h = [new_side(x) for x in hunks(FILES[1])]

    def cut(lines, first):
        i = next(k for k, l in enumerate(lines) if l.startswith(first))
        j = next(k for k in range(i, len(lines)) if lines[k] == 'endif')
        return '\n'.join(lines[i:j + 1])
    mk = '\n'.join(['XCRUN = echo xcrun', 'SDKROOT = macosx', 'ARCH_STRING_FOR_CURRENT_MACHINE_CONFIG := arm64',
                    cut(h[0], 'ifeq ($(FUXI_ARM64E),1)'), cut(h[1], 'ifeq ($(filter SWEET FUXI'),
                    cut(h[2], 'ifeq ($(CURRENT_MACHINE_CONFIG)-'),
                    'all:', '\t@echo "$(ARCH_STRING_FOR_CURRENT_MACHINE_CONFIG)|$(MACHINE_FLAGS_ARM64_FUXI)|'
                    '$(LDFLAGS_KERNEL_ONLY_SUBARCH_arm64e)"', ''])
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp)/'Makefile').write_text(mk)

        def run(*args):
            env = {'PATH': '/usr/bin:/bin'}
            out = subprocess.run(['make', '-s', '-C', tmp, *args], check=True, capture_output=True, text=True, env=env)
            return out.stdout.strip().split('|')
        arch, flags, ld = run('CURRENT_MACHINE_CONFIG=FUXI', 'FUXI_ARM64E=1')
        assert arch == 'arm64e' and flags.split() == ['-DARM64_BOARD_CONFIG_SWEET', '-DFUXI=1', '-DFUXI_ARM64E=1',
                                                      '-march=armv8.3-a+crypto'], (arch, flags)
        assert ld.split() == ['-fuse-ld=xcrun', '-sdk', 'macosx', '-find', 'ld-classic', '-Wl,-threaded_starts_section'], ld
        for args in (['CURRENT_MACHINE_CONFIG=FUXI'], ['CURRENT_MACHINE_CONFIG=FUXI', 'FUXI_ARM64E=0'],
                     ['CURRENT_MACHINE_CONFIG=SWEET', 'FUXI_ARM64E=1']):
            arch, flags, ld = run(*args)
            assert arch == 'arm64' and ld.split() == ['-Wl,-add_split_seg_info', '-Wl,-kernel'], (args, arch, ld)
            if 'FUXI_ARM64E=1' not in args:
                assert flags.split() == ['-DARM64_BOARD_CONFIG_SWEET', '-DFUXI=1', '-march=armv8.2-a+crypto'], flags
    print('PASS: MakeInc.def: only FUXI + FUXI_ARM64E=1 is arm64e/armv8.3-a/ld-classic threaded starts')


def defines(text, prefix):
    return {m[1]: int(m[2], 0) for m in re.finditer(r'#define\s+' + prefix + r'(\w+)\s+(0x[0-9a-fA-F]+|\d+)', text)}


def layout():
    pac = '\n'.join(new_side(hunks(FILES[3])[0]))
    native = defines(pac, 'NATIVE_PAC_')
    logic = defines((ROOT/'xnu/fuxi-pac/fuxi_pac_logic.h').read_text(), 'FP_NATIVE_')
    assert logic and all(native[k] == v for k, v in logic.items()), (native, logic)
    assert native['KEY_WORDS'] == 10 == native['READY'] and native['WORDS'] * 8 == 192
    assert max(v for k, v in native.items() if k not in ('WORDS', 'MODIFIER')) < native['WORDS']
    fp = defines((ROOT/'xnu/fuxi-pac/fuxi_pac.c').read_text(), 'FP_')
    assert native['MODIFIER'] == fp['MODIFIER'], 'PACGA modifier shared with the 0114 probe'
    print('PASS: fuxi_native_pac layout matches fuxi_pac_logic.h (%d words)' % native['WORDS'])
    return native


STUBS = '''#define FUXI 1
#define EXT(x) _ ## x
#define LEXT(x) _ ## x:
.macro MOV64
	movk $0, #((($1) >> 48) & 0x000000000000FFFF), lsl #48
	movk $0, #((($1) >> 32) & 0x000000000000FFFF), lsl #32
	movk $0, #((($1) >> 16) & 0x000000000000FFFF), lsl #16
	movk $0, #((($1) >> 00) & 0x000000000000FFFF), lsl #00
.endmacro
'''
KEYS = ['AP%sKey%s_EL1' % (k, h) for k in ('IA', 'IB', 'DA', 'DB', 'GA') for h in ('Lo', 'Hi')]


def assemble(native):
    cc, dis = shutil.which('clang-18'), shutil.which('llvm-objdump-18')
    if not (cc and dis):
        print('SKIP: clang-18/llvm-objdump-18 not installed; PAC_INIT_KEY_STATE not assembled')
        return
    defs = ''.join(l[1:] + '\n' for l in hunks(FILES[3])[0] if l.startswith('+'))
    h2 = new_side(hunks(FILES[3])[1])
    first = h2.index('#if HAS_NATIVE_PAC')
    mac = h2.index('.macro PAC_INIT_KEY_STATE\ttmp, tmp2')
    end = next(i for i in range(mac, len(h2)) if h2[i] == '#if HAS_PARAVIRTUALIZED_PAC')
    data = '\n'.join(l for l in new_side([l for h in hunks(FILES[4]) for l in h])
                     if 'fuxi_native_pac' in l or l.startswith('\t.space') or l.startswith('\t.align 6') or
                     l.startswith('\t.section __DATA'))
    src = (STUBS + '#define HAS_NATIVE_PAC 1\n' + defs + '\n'.join(h2[first:end]) + '\n.endmacro\n'
           '\t.text\n\t.globl _t\n_t:\n\tPAC_INIT_KEY_STATE tmp=x0, tmp2=x1\n\tret\n' + data +
           '\n\t.globl _fuxi_early_status\n_fuxi_early_status:\n\t.space 64\n')
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp/'k.S').write_text(src)
        subprocess.run([cc, '-target', 'arm64-apple-macos13', '-march=armv8.3-a', '-c', str(tmp/'k.S'), '-o',
                        str(tmp/'k.o')], check=True)
        out = subprocess.run([dis, '-d', '--mattr=+v8.3a', '--no-show-raw-insn', str(tmp/'k.o')], check=True,
                             capture_output=True, text=True).stdout
        insns = [re.sub(r'\s+', ' ', l.split(':', 1)[1].split(';')[0].strip()) for l in out.splitlines()
                 if re.match(r'\s+[0-9a-f]+:', l)]
        sects = subprocess.run([dis, '-h', str(tmp/'k.o')], check=True, capture_output=True, text=True).stdout
    writes = [i.split()[1].rstrip(',') for i in insns if i.startswith('msr AP')]
    reads = [i.split()[2] for i in insns if i.startswith('mrs x2, AP')]
    assert writes == KEYS and reads == KEYS, (writes, reads)
    assert sum(i.startswith('pacga x6, x0, x4') for i in insns) == 1
    for c in ('#0x9e37', '#0x7c15', '#0xbf58', '#0xe5b9', '#0x94d0', '#0x11eb', '#0x5eed', '#0x1234'):
        assert any(i.startswith('movk') and c in i for i in insns), c
    assert insns.count('wfe') == 1 and sum(i.startswith('dc civac') for i in insns) == 3 * 3 + 3, insns
    assert insns[-1] == 'ret' and insns.index('isb') > insns.index('msr APGAKeyHi_EL1, x3')
    offs = [int(m[1], 0) for i in insns for m in [re.search(r'\[x0, #(0x[0-9a-f]+|\d+)\]', i)] if m]
    assert offs and max(offs) < native['WORDS'] * 8 and all(o % 8 == 0 for o in offs), offs
    for word in ('READY', 'MODE', 'CPUS', 'BAD', 'BAD_MPIDR', 'GA_REF', 'GA_BAD', 'GA_MPIDR'):
        assert native[word] * 8 in offs, word
    assert any(i == 'mov x17, #0x35' for i in insns) and any(i == 'mov x17, #0x31' for i in insns)
    assert re.search(r'__data\s+00000100', sects), sects     # 192-byte block + the 64-byte stand-in status word
    print('PASS: PAC_INIT_KEY_STATE assembles (armv8.3-a): %d instructions, keys in order, 192-byte block'
          % len(insns))


def macho(cpusub, sections=(), fixups=None):
    """Minimal MH_EXECUTE: __TEXT (+sections), __DATA with 8 bytes, LC_UNIXTHREAD, optional chained fixups."""
    page = 0x4000
    text_va, data_va = 0xfffffff007004000, 0xfffffff007008000
    cmds = []
    sect = b''.join(struct.pack('<16s16sQQIIIIIIII', n.encode(), b'__TEXT', text_va + 0x100, 8, 0x100, 3, 0, 0, 0, 0, 0, 0)
                    for n in sections)
    cmds.append(struct.pack('<II16sQQQQiiII', 0x19, 72 + len(sect), b'__TEXT', text_va, page, 0, page, 5, 5,
                            len(sections), 0) + sect)
    cmds.append(struct.pack('<II16sQQQQiiII', 0x19, 72, b'__DATA', data_va, page, page, page, 3, 3, 0, 0))
    cmds.append(struct.pack('<IIII', 0x5, 16 + 68 * 4, 6, 68) + b'\0' * (32 * 8) + struct.pack('<Q', text_va + 0x200)
                + b'\0' * (68 * 4 - 33 * 8))
    blob = b''
    if fixups is not None:
        # dyld_chained_fixups_header + starts_in_image (2 segs) + starts_in_segment for __DATA (one page)
        seg = struct.pack('<IHHQIHH', 24, page, fixups, 0, 0, 1, 0)
        image = struct.pack('<III', 2, 0, 12)
        hdr = struct.pack('<7I', 0, 28, 28 + len(image) + len(seg), 0, 0, 1, 0)
        blob = hdr + image + seg
        cmds.append(struct.pack('<IIII', 0x80000034, 16, 2 * page, len(blob)))
    head = struct.pack('<8I', 0xfeedfacf, 0x0100000c, cpusub, 2, len(cmds), sum(map(len, cmds)), 0, 0)
    buf = bytearray(head + b''.join(cmds))
    buf += b'\0' * (2 * page + len(blob) - len(buf))
    if fixups is not None:
        buf[2 * page:] = blob
        # one auth rebase (bit 63) to __TEXT+0x10, end of chain
        struct.pack_into('<Q', buf, page, (1 << 63) | 0x10)
    return bytes(buf)


def payload():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        subprocess.run([sys.executable, str(ROOT/'tools/ci/kernel-inputs.py'), '--variant', 'metal4e', '--out', str(tmp/'e')],
                       check=True, capture_output=True)
        subprocess.run([sys.executable, str(ROOT/'tools/ci/kernel-inputs.py'), '--variant', 'metal4', '--out', str(tmp/'m')],
                       check=True, capture_output=True)
        assert (tmp/'e/cmdline.txt').read_text() == (tmp/'m/cmdline.txt').read_text()
        assert (tmp/'e/fuxi.adt').stat().st_size == (tmp/'m/fuxi.adt').stat().st_size   # same recipe (ADT has random seeds)
        one = (tmp/'e/cmdline-cpus1.txt').read_text()
        assert one == (tmp/'m/cmdline.txt').read_text().replace(' cpus=7 ', ' cpus=1 ') and ' cpus=1 ' in one
        assert sorted(p.name for p in (tmp/'e').iterdir()) == ['cmdline-cpus1.txt', 'cmdline.txt', 'fuxi.adt']
        adt = tmp/'e/fuxi.adt'
        cases = {'arm64': macho(0), 'arm64e-ts': macho(2, ('__text', '__thread_starts')),
                 'arm64e-none': macho(2, ('__text',)), 'arm64e-auth': macho(2, ('__text',), fixups=9)}
        for name, raw in cases.items():
            (tmp/name).write_bytes(raw)
        _, info = mkpayload.build(tmp/'arm64', adt, 'x')
        assert info['cpusub'] == 0 and info['thread_starts'] is None and info['auth_fixups'] == 0
        _, info = mkpayload.build(tmp/'arm64e-ts', adt, 'x')
        assert info['cpusub'] == 2 and info['thread_starts'] == ('0xfffffff007004100', '0x8') and info['fixups'] == 0
        for name, why in (('arm64e-none', 'without __TEXT,__thread_starts'), ('arm64e-auth', 'authenticated chained')):
            try:
                mkpayload.build(tmp/name, adt, 'x')
            except SystemExit as e:
                assert why in str(e), e
            else:
                raise AssertionError(name + ' was packed')
        _, info = mkpayload.build(tmp/'arm64e-auth', adt, 'x', allow_unsigned_auth=True)
        assert info['auth_fixups'] == 1 and info['fixups'] == 1
    print('PASS: metal4e inputs = metal4 + cmdline-cpus1.txt; mkpayload packs arm64/arm64e+__thread_starts, '
          'refuses unsigned auth fixups and arm64e without thread starts')


def ci():
    sh = ['bash', str(ROOT/'tools/ci/build.sh')]

    def out(*args):
        return subprocess.run(sh + list(args), check=True, capture_output=True, text=True).stdout
    assert out('--plan', 'metal4e', 'false', 'false', 'false').strip() == '{"target": ["kernel-metal4e"]}'
    assert out('--plan', 'all', 'false', 'false', 'false').strip() == '{"target": ["kernel-metal4", "kernel-wlan6g"]}'
    assert out('--plan', 'metal4', 'false', 'false', 'false').strip() == '{"target": ["kernel-metal4"]}'
    assert out('--plan', 'wlan6g', 'true', 'false', 'false').strip() == '{"target": ["kernel-wlan6g", "metal"]}'
    dry = out('kernel-metal4e', '--dry-run')
    assert 'artifact=fuxi-kernel-metal4e\n' in dry and 'FUXI_ARM64E=1' in dry
    assert 'arm64e' not in out('kernel-metal4', '--dry-run') and 'arm64e' not in out('kernel-wlan6g', '--dry-run')
    assert subprocess.run(sh + ['--plan', 'arm64e', 'false', 'false', 'false'], capture_output=True).returncode == 2
    text = (ROOT/'tools/ci/build.sh').read_text()
    body = text[text.index('\nkernel() {'):text.index('\nmesa_source() {')]
    assert 'if [[ $arm64e == 1 ]]; then export FUXI_ARM64E=1; else unset FUXI_ARM64E; fi' in body
    assert body.count('FUXI_ARM64E') == 2 and '[[ $target == kernel-metal4e ]] && arm64e=1' in body
    assert 'python3 tools/test-fuxi-arm64e.py' in body
    assert "xcrun --sdk macosx --find ld-classic" in text
    wf = (ROOT/'.github/workflows/fuxi-build.yml').read_text()
    assert 'options: [none, metal4, wlan6g, metal4e, all]' in wf and 'default: metal4\n' in wf
    print('PASS: CI kernel=metal4e plans kernel-metal4e only (not in all); metal4/wlan6g/all plans unchanged')


def jop_structure():
    assert re.findall(r'^--- a/(\S+)$', JOP, re.M) == JOP_FILES and re.findall(r'^\+\+\+ b/(\S+)$', JOP, re.M) == JOP_FILES
    for path in JOP_FILES:
        stack = []          # one entry per open added #if: True once its #else is reached
        for h in hunks(path, JOP):
            assert not [l for l in h if l.startswith('-')], path + ': 0116 removes nothing'
            for l in h:
                if not l.startswith('+'):
                    # original lines may only sit in the #else branch (the unchanged non-FUXI-arm64e path)
                    assert not stack or stack[-1], path + ': original line inside an added FUXI block'
                    continue
                t = l[1:].strip()
                if t.startswith('#if'):
                    assert stack or t == GATE, l
                    stack.append(False)
                elif t.startswith('#else'):
                    stack[-1] = True
                elif t.startswith('#endif'):
                    stack.pop()
                elif t:
                    assert stack and not stack[-1], path + ': ungated added line ' + l
            assert not stack, path
    md = '\n'.join(new_side([l for h in hunks(JOP_FILES[0], JOP) for l in h]))
    assert '\t\t\t\treturn fuxi_arm64e_grade(grade_arm64e_binary(execfeatures));\n#else\n' \
           '\t\t\t\treturn grade_arm64e_binary(execfeatures);\n#endif\n\t\t\tcase CPU_SUBTYPE_ARM64_V8:\n\t\t\t\treturn 10;' in md
    ex = '\n'.join(new_side([l for h in hunks(JOP_FILES[1], JOP) for l in h]))
    assert re.search(r'imgp->ip_flags \|= IMGPF_NOJOP;\n\t}\n' + re.escape(GATE) + r'\n.*?if \(!\(imgp->ip_flags & '
                     r'IMGPF_NOJOP\)\) \{\n\t\tprintf\("FUXI ARM64E: exec %s arm64e slice cpusub=0x%x user_jop=1\\n"', ex, re.S)
    print('PASS: 0116 only adds FUXI && __arm64e__ blocks (grade_binary arm64e case, exec log after IMGPF_NOJOP)')


JOP_HARNESS = r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static int bootarg = -1;
static int PE_parse_boot_argn(const char *n, void *p, int size)
{
	if (strcmp(n, "fuxiarm64e") || bootarg < 0 || size != (int)sizeof(int)) return 0;
	memcpy(p, &bootarg, sizeof(int));
	return 1;
}
%s
int main(int argc, char **argv)
{
	(void)argc;
	bootarg = atoi(argv[1]);
	printf("%%d %%d %%d\n", fuxi_arm64e_grade(12), fuxi_arm64e_grade(11), fuxi_arm64e_grade(0));
	return 0;
}
'''


def jop_grade():
    cc = shutil.which('clang-18') or shutil.which('cc')
    if not cc:
        print('SKIP: no host C compiler; fuxi_arm64e_grade not run')
        return
    src = new_side(hunks(JOP_FILES[0], JOP)[1])
    helper = '\n'.join(src[src.index(GATE) + 1:src.index('#endif /* FUXI && __arm64e__ */')])
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp/'g.c').write_text(JOP_HARNESS % helper)
        subprocess.run([cc, '-std=c11', '-Wall', '-Wextra', '-Werror', str(tmp/'g.c'), '-o', str(tmp/'g')], check=True)
        got = {a: subprocess.run([str(tmp/'g'), a], check=True, capture_output=True, text=True).stdout.split()
               for a in ('-1', '0', '1')}
    assert got == {'-1': ['8', '8', '0'], '0': ['8', '8', '0'], '1': ['12', '11', '0']}, got
    print('PASS: fuxi_arm64e_grade: arm64e slice 12/11 -> 8 (< arm64 10, arm64_all 9), 0 stays 0; fuxiarm64e=1 keeps 12/11')


def jop_tree(tree):
    """DIR's real kern_machdep.c with 0116, built for a host whose cpu_subtype() is arm64e."""
    cc = shutil.which('clang-18') or shutil.which('cc')
    if not cc:
        print('SKIP: no host C compiler; real grade_binary not compiled')
        return
    hdr = (Path(tree)/'osfmk/mach/machine.h').read_text()
    want = ('CPU_ARCH_ABI64', 'CPU_TYPE_ARM', 'CPU_TYPE_ARM64', 'CPU_SUBTYPE_MASK', 'CPU_SUBTYPE_ARM64_ALL',
            'CPU_SUBTYPE_ARM64_V8', 'CPU_SUBTYPE_ARM64E', 'CPU_SUBTYPE_PTRAUTH_ABI',
            'CPU_SUBTYPE_ARM64_PTR_AUTH_MASK', 'CPU_SUBTYPE_ARM64_PTR_AUTH_VERSION',
            'CPU_SUBTYPE_ARM64_PTR_AUTH_CURRENT_VERSION')
    defs = [m[0] for m in re.finditer(r'^#define\s+(CPU_\w+)\b.*$', hdr, re.M)
            if m[1] in want or m[1].startswith('CPU_SUBTYPE_ARM_')]
    for w in want:
        assert any(re.match(r'#define\s+' + w + r'\b', d) for d in defs), w
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp/'src/bsd/dev/arm').mkdir(parents=True)
        shutil.copy(Path(tree)/JOP_FILES[0], tmp/'src'/JOP_FILES[0])
        one = '--- a/%s\n+++ b/%s\n' % (JOP_FILES[0], JOP_FILES[0]) + section(JOP_FILES[0], JOP)
        subprocess.run(['patch', '-p1', '-s', '-N', '-F0'], input=one, text=True, cwd=tmp/'src', check=True)
        inc = tmp/'inc'
        stubs = {
            'sys/types.h': '#include <stdbool.h>\n#include <stdint.h>\n#define __unused __attribute__((unused))\n',
            'mach/machine.h': 'typedef int integer_t;\ntypedef integer_t cpu_type_t;\ntypedef integer_t cpu_subtype_t;\n'
                              + '\n'.join(defs) + '\n',
            'kern/cpu_number.h': 'cpu_subtype_t cpu_subtype(void);\n', 'libkern/libkern.h': '', 'machine/exec.h': '', 'pexpert/arm64/board_config.h': '',
            'pexpert/pexpert.h': 'extern int PE_parse_boot_argn(const char *, void *, int);\n',
        }
        for name, text in stubs.items():
            (inc/name).parent.mkdir(parents=True, exist_ok=True)
            (inc/name).write_text(text)
        (tmp/'h.c').write_text(r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <mach/machine.h>
int grade_binary(cpu_type_t, cpu_subtype_t, cpu_subtype_t, bool);
static int bootarg = -1;
cpu_subtype_t cpu_subtype(void) { return CPU_SUBTYPE_ARM64E; }
int PE_parse_boot_argn(const char *n, void *p, int size)
{
	if (strcmp(n, "fuxiarm64e") || bootarg < 0 || size != (int)sizeof(int)) return 0;
	memcpy(p, &bootarg, sizeof(int));
	return 1;
}
int main(int argc, char **argv)
{
	(void)argc;
	bootarg = atoi(argv[1]);
	cpu_subtype_t e = CPU_SUBTYPE_ARM64E | CPU_SUBTYPE_PTRAUTH_ABI |
	    (CPU_SUBTYPE_ARM64_PTR_AUTH_CURRENT_VERSION << 24);
	printf("%d %d %d %d\n", grade_binary(CPU_TYPE_ARM64, CPU_SUBTYPE_ARM64E, e, false),
	    grade_binary(CPU_TYPE_ARM64, CPU_SUBTYPE_ARM64_V8, 0, false),
	    grade_binary(CPU_TYPE_ARM64, CPU_SUBTYPE_ARM64_ALL, 0, false),
	    grade_binary(CPU_TYPE_ARM64, CPU_SUBTYPE_ARM64E, CPU_SUBTYPE_ARM64E, false));
	return 0;
}
''')
        res = {}
        for flags in (['-D__arm64e__=1'], []):
            exe = tmp/('g%d' % len(flags))
            subprocess.run([cc, '-std=c11', '-w', '-D__arm64__=1', '-DFUXI=1', '-DXNU_TARGET_OS_IOS=1', *flags,
                            '-I', str(inc), str(tmp/'src'/JOP_FILES[0]), str(tmp/'h.c'), '-o', str(exe)], check=True)
            for a in ('-1', '1'):
                res[(len(flags), a)] = subprocess.run([str(exe), a], check=True, capture_output=True,
                                                      text=True).stdout.split()
    # arm64e host: fat arm64e+arm64 picks arm64 (10 > 8) by default, arm64e (12) with fuxiarm64e=1; unversioned 0.
    assert res[(1, '-1')] == ['8', '10', '9', '0'] and res[(1, '1')] == ['12', '10', '9', '0'], res
    assert res[(0, '-1')] == res[(0, '1')] == ['12', '10', '9', '0'], res      # no __arm64e__: Apple grading
    print('PASS: real grade_binary + 0116 on an arm64e host: fat binary -> arm64 slice (10 > 8); fuxiarm64e=1 -> arm64e (12)')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--tree')
    args = ap.parse_args()
    structure()
    make_eval()
    native = layout()
    assemble(native)
    payload()
    ci()
    jop_structure()
    jop_grade()
    if args.tree:
        for name, text in (('0115', PATCH), ('0116', JOP)):
            subprocess.run(['patch', '-p1', '--dry-run', '-s', '-N', '-F0'], input=text, text=True, cwd=args.tree,
                           check=True)
            print('PASS: %s applies to %s' % (name, args.tree))
        jop_tree(args.tree)


if __name__ == '__main__':
    main()
