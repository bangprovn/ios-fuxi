#!/usr/bin/env python3
"""Host tests for tools/ios27-prelink.py (K2, docs-lanes/ios27.md#k2): profile roots, preflight refusals, the
check-prelink-xml lexer rules and the structural checks of an emitted fileset. Synthetic inputs only: no Apple data.
The XnuPrelink emitter itself is covered by tools/xnuprelink/test_*.py.

  test-ios27-prelink.py [-v]
"""
import importlib.util, os, plistlib, struct, subprocess, sys, tempfile, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('ios27_prelink', HERE / 'ios27-prelink.py')
pl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pl)
KIT = pl.load_kit(pl.KIT)
LINK = pl.LINK


def seg(name, va, size, off, filesize=None):
    return struct.pack('<II16s4Q4I', 0x19, 72, name.encode(), va, size, off, size if filesize is None else filesize,
                       5, 5, 0, 0)


def symtab_cmd(at, names):
    """LC_SYMTAB at file offset `at` (the symbols and strings follow it in the file)."""
    strtab = b'\0' + b''.join(n.encode() + b'\0' for n in names)
    nlist, off = b'', 1
    for i, n in enumerate(names):
        nlist += struct.pack('<IBBHQ', off, 0x0f, 1, 0, LINK + 0x4000 + 16 * i)
        off += len(n) + 1
    return struct.pack('<6I', 2, 24, at, len(names), at + len(nlist), len(strtab)), nlist + strtab


def native(names=(), split=False, hook=False):
    """arm64e MH_EXECUTE at LINK: __TEXT, __TEXT_EXEC (one section), __LINKEDIT with the symbol table."""
    text_exec = seg('__TEXT_EXEC', LINK + 0x4000, 0x4000, 0x4000)
    text_exec = text_exec[:64] + struct.pack('<I', 1) + text_exec[68:]
    text_exec = struct.pack('<II', 0x19, 72 + 80) + text_exec[8:] + struct.pack(
        '<16s16sQQIIIIIIII', b'__text', b'__TEXT_EXEC', LINK + 0x4000, 0x4000, 0x4000, 2, 0, 0, 0x80000400, 0, 0, 0)
    sym, blob = symtab_cmd(0x8000, list(names))
    cmds = seg('__TEXT', LINK, 0x4000, 0) + text_exec + seg('__LINKEDIT', LINK + 0x8000, 0x4000, 0x8000) + sym
    if split:
        cmds += struct.pack('<4I', pl.LC_SEGMENT_SPLIT_INFO, 16, 0x8000 + len(blob), 0)
    count = 4 + split
    data = bytearray(0xc000)
    data[:32 + len(cmds)] = struct.pack('<8I', 0xfeedfacf, 0x100000c, 0x80000002, 2, count, len(cmds), 0, 0) + cmds
    data[0x8000:0x8000 + len(blob)] = blob
    if hook:
        data[0x5000:0x5000 + len(pl.HOOK)] = pl.HOOK
    return bytes(data)


def fileset(info, pc=LINK + 0x8010, kext_va=LINK + 0xc000):
    """Flat MH_FILESET: root __TEXT/__TEXT_EXEC/__PRELINK_INFO, entries com.apple.kernel and com.example.kext."""
    def entry(name, va, off):
        raw = name.encode() + b'\0'
        raw = raw.ljust((len(raw) + 7) & ~7, b'\0')
        return struct.pack('<IIQQII', 0x80000035, 32 + len(raw), va, off, 32, 0) + raw
    thread = struct.pack('<IIII', 0x5, 16 + 34 * 8 + 8, 6, 68) + bytes(32 * 8) + struct.pack('<Q', pc) + bytes(16)
    cmds = (seg('__TEXT', LINK, 0x4000, 0) + seg('__TEXT_EXEC', LINK + 0x4000, 0xc000, 0x4000)
            + seg('__PRELINK_INFO', LINK + 0x10000, 0x4000, 0x10000) + thread
            + entry('com.apple.kernel', LINK + 0x4000, 0x4000) + entry('com.example.kext', kext_va, 0xc000))
    data = bytearray(0x14000)
    data[:32 + len(cmds)] = struct.pack('<8I', 0xfeedfacf, 0x100000c, 0xc0000002, 12, 6, len(cmds), 0, 0) + cmds
    for at, va in ((0x4000, LINK + 0x8000), (0xc000, kext_va)):
        inner = seg('__TEXT_EXEC', va, 0x4000, at + 0x4000 if at == 0x4000 else at)
        data[at:at + 32 + len(inner)] = struct.pack('<8I', 0xfeedfacf, 0x100000c, 0xc0000002,
                                                    2 if at == 0x4000 else 11, 1, len(inner), 0, 0) + inner
    data[0x10000:0x10000 + len(info)] = info
    return bytes(data)


def prelink_info(addr=LINK + 0xc000):
    return plistlib.dumps({'_PrelinkInfoDictionary': [
        {'CFBundleIdentifier': 'com.example.kext', '_PrelinkExecutableLoadAddr': addr - (1 << 64)},
        {'CFBundleIdentifier': 'com.example.codeless', '_PrelinkExecutableLoadAddr': pl.CODELESS}]})


class Profile(unittest.TestCase):
    def test_roots_and_soc_subset(self):
        self.assertEqual(len(pl.ROOTS), 31)
        self.assertEqual(len(set(pl.ROOTS)), 31)
        self.assertTrue(set(pl.SOC_ROOTS) <= set(pl.ROOTS) and len(pl.SOC_ROOTS) == 9)
        for need in ('com.apple.driver.AppleMobileFileIntegrity', 'com.apple.security.AppleImage4',
                     'com.apple.kec.corecrypto', 'com.apple.security.sandbox', 'com.apple.filesystems.apfs'):
            self.assertIn(need, pl.ROOTS)
            self.assertNotIn(need, pl.SOC_ROOTS)
        self.assertEqual(pl.parse_initializers([pl.EARLY_SECURITY_INIT]),
                         {'com.apple.driver.AppleMobileFileIntegrity': 0xfffffe00092551ac})
        with self.assertRaises(SystemExit):
            pl.parse_initializers(['a=1', 'a=2'])

    def test_kit_is_the_ported_copy(self):
        self.assertEqual(Path(KIT[1].__file__).resolve().parent, HERE / 'xnuprelink')


class Preflight(unittest.TestCase):
    def run_preflight(self, data, cache=None):
        return pl.preflight(data, cache or fileset(prelink_info()), pl.ROOTS, {}, True, KIT, False)

    def test_k1_style_kernel_is_refused(self):
        failures, facts = self.run_preflight(native())
        text = '\n'.join(failures)
        self.assertIn('LC_SEGMENT_SPLIT_INFO', text)
        self.assertIn(pl.STUB, text)
        self.assertIn('_NativePrelinkEarlySecurityInit', text)
        self.assertIn('layout:', text)       # the synthetic cache has none of the profile roots

    def test_symbols_hook_and_split_info_found(self):
        failures, _ = self.run_preflight(native([pl.STUB], split=True, hook=True))
        self.assertFalse([f for f in failures if 'LC_SEGMENT_SPLIT_INFO' in f or pl.STUB in f or 'Early' in f])

    def test_non_fileset_cache_stops_early(self):
        failures, _ = pl.preflight(native(), native(), pl.ROOTS, {}, False, KIT, True)
        self.assertIn('kernelcache is not an MH_FILESET with a com.apple.kernel entry', failures)

    def test_default_initializer_needs_the_profile_kernelcache(self):
        failures, facts = pl.preflight(native(), fileset(prelink_info()), ['com.example.kext'], {}, False, KIT, True)
        self.assertTrue(any('24A446' in f for f in failures))
        self.assertNotEqual(facts['kernelcache_sha256'], pl.PROFILE_KERNELCACHE_SHA256)


class XmlRules(unittest.TestCase):
    def test_lexer_rules(self):
        self.assertEqual(pl.xml_rules(prelink_info()), [])
        self.assertEqual(pl.xml_rules(b'<dict><key>a</key><true/></dict>'), [])
        bad = b'<dict><key>a</key><true></true><' + b'x' * 40 + b'/><string>a<b</string></dict>'
        problems = pl.xml_rules(bad)
        self.assertTrue(any('non-empty <true>' in p for p in problems))
        self.assertTrue(any('tag too long' in p for p in problems))
        self.assertTrue(any('markup inside <string>' in p for p in problems))


class Checks(unittest.TestCase):
    report = {'closure': ['com.example.kext', 'com.example.codeless']}

    def run_checks(self, image):
        import hashlib
        return pl.checks(KIT, image, dict(self.report, candidate_sha256=hashlib.sha256(image).hexdigest()))

    def test_good_fileset(self):
        result = self.run_checks(fileset(prelink_info()))
        failed = {k for k, v in result.items() if v['ok'] is False}
        # The synthetic root carries no chained fixups; an emitted one always does.
        self.assertEqual(failed, {'root_fixups_target_mappings'})
        self.assertIsNone(result['rebuild_equivalence']['ok'])

    def test_load_address_entry_and_pc(self):
        result = self.run_checks(fileset(prelink_info(LINK + 0x4000)))
        self.assertFalse(result['prelink_load_addresses_match_entries']['ok'])
        result = self.run_checks(fileset(prelink_info(), pc=LINK + 0x4010))
        self.assertFalse(result['entry_in_kernel_text_exec']['ok'])
        result = self.run_checks(fileset(prelink_info(), kext_va=LINK + 0x20000))
        self.assertFalse(result['entry_segments_inside_root']['ok'])


class Cli(unittest.TestCase):
    def test_refuses_existing_output_and_reports_preflight(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / 'n').write_bytes(native())
            (tmp / 'kc').write_bytes(fileset(prelink_info()))
            cmd = [sys.executable, '-E', '-s', '-B', str(HERE / 'ios27-prelink.py'), '--kernel', str(tmp / 'n'),
                   '--kernelcache', str(tmp / 'kc'), '--out', str(tmp / 'out'), '--allow-missing']
            r = subprocess.run(cmd, capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn('no RELEASE comparison build given', r.stdout)
            self.assertIn('preflight failed', r.stderr)
            self.assertTrue((tmp / 'out/preflight.json').is_file())
            (tmp / 'out/kernel.prelinked').write_bytes(b'x')
            r = subprocess.run(cmd, capture_output=True, text=True)
            self.assertIn('exists', r.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2 if '-v' in sys.argv else 1, argv=sys.argv[:1])
