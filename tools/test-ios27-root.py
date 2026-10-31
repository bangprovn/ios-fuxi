#!/usr/bin/env python3
"""Host tests for the iOS 27 userland tools: apfs-ro.py decoders, ios27-root.py (IM4P, trust caches, cdhash, windows
image, Data staging), ios27-mockfs.py (N4KROOT) and mkinit.py --arm64e. Synthetic inputs only: no Apple data.

  test-ios27-root.py [-v]
"""
import hashlib, importlib.util, os, plistlib, random, stat, struct, sys, tempfile, unittest, zlib

HERE = os.path.dirname(os.path.abspath(__file__))


def load(name, fname):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, fname))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


apfs, root, mockfs, mkinit = (load('apfs_ro', 'apfs-ro.py'), load('ios27_root', 'ios27-root.py'),
                              load('ios27_mockfs', 'ios27-mockfs.py'), load('mkinit', 'mkinit.py'))


def der_len(n):
    if n < 0x80: return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, 'big'); return bytes([0x80 | len(b)]) + b


def der(tag, body): return bytes([tag]) + der_len(len(body)) + body


def fake_im4p(fourcc, payload, kbag=False):
    body = der(0x16, b'IM4P') + der(0x16, fourcc.encode()) + der(0x16, b'test') + der(0x04, payload)
    if kbag: body += der(0x04, b'\0' * 8)
    return der(0x30, body)


def fake_object(code):
    """A minimal arm64 MH_OBJECT: one __text section, a global _start at 0, no relocations."""
    sect = struct.pack('<16s16sQQIIIIIIII', b'__text', b'__TEXT', 0, len(code), 0, 2, 0, 0, 0x80000400, 0, 0, 0)
    seg_len, sym_len = 72 + 80, 24
    code_off = 32 + seg_len + sym_len
    seg = struct.pack('<II16sQQQQIIII', 0x19, seg_len, b'', 0, len(code), code_off, len(code), 7, 7, 1, 0) + sect
    sect = bytearray(seg); struct.pack_into('<I', sect, 72 + 48, code_off); seg = bytes(sect)
    symoff = code_off + len(code); stroff = symoff + 16; strtab = b'\0_start\0'
    symtab = struct.pack('<6I', 0x2, sym_len, symoff, 1, stroff, len(strtab))
    hdr = struct.pack('<8I', 0xfeedfacf, 0x0100000c, 0, 1, 2, seg_len + sym_len, 0, 0)
    return hdr + seg + symtab + code + struct.pack('<IBBHQ', 1, 0x0f, 1, 0, 0) + strtab


CODE = struct.pack('<3I', 0xd503201f, 0xd503201f, 0x14000000)	# nop; nop; b .


class Decoders(unittest.TestCase):
    def test_fletcher64_validates(self):
        b = bytearray(random.Random(1).randbytes(4096)); struct.pack_into('<Q', b, 0, apfs.fletcher64(b))
        words = struct.unpack('<1024I', bytes(b[8:] + b[:8])); s1 = s2 = 0
        for w in words: s1 = (s1 + w) % 0xffffffff; s2 = (s2 + s1) % 0xffffffff
        self.assertEqual((s1, s2), (0, 0))	# data then stored checksum sums to zero (APFS verify)

    def test_lzvn_literals_and_matches(self):
        # 'abc' literal (0xe3), then small-distance match lit=0 m=3+3 d=3 (0b00_011_000 | d>>8, d&0xff), then end
        src = bytes([0xe3]) + b'abc' + bytes([0x18, 3]) + bytes([0x06]) + bytes(7)
        self.assertEqual(bytes(apfs.lzvn_decode(src)), b'abc' + b'abcabc')
        self.assertRaises(apfs.ApfsError, apfs.lzvn_decode, bytes([0x18, 9, 0x06]))	# distance past the start

    def test_lzfse_raw_and_lzvn_blocks(self):
        lz = bytes([0xe2]) + b'xy' + bytes([0x06]) + bytes(7)
        src = b'bvx-' + struct.pack('<I', 5) + b'hello' + b'bvxn' + struct.pack('<II', 2, len(lz)) + lz + b'bvx$'
        self.assertEqual(bytes(apfs.lzfse_decode(src)), b'helloxy')
        self.assertRaises(apfs.ApfsError, apfs.lzfse_decode, b'bvx-' + struct.pack('<I', 1) + b'a')

    def test_decmpfs_chunks(self):
        data = b'decmpfs' * 100
        self.assertEqual(apfs.decmpfs_chunk(4, zlib.compress(data), len(data)), data)
        self.assertEqual(apfs.decmpfs_chunk(3, b'\x0f' + data, len(data)), data)
        self.assertEqual(apfs.decmpfs_chunk(9, b'\xcc' + data, len(data)), data)
        self.assertEqual(apfs.decmpfs_chunk(12, b'\xff' + data, len(data)), data)
        self.assertRaises(apfs.ApfsError, apfs.decmpfs_chunk, 14, b'\x00' + data, len(data))

    def test_mode_string(self):
        self.assertEqual(apfs.mode_string(stat.S_IFDIR | 0o755), 'drwxr-xr-x')
        self.assertEqual(apfs.mode_string(stat.S_IFREG | 0o4755), '-rwsr-xr-x')
        self.assertEqual(apfs.mode_string(stat.S_IFDIR | 0o1777), 'drwxrwxrwt')


class Im4pTrust(unittest.TestCase):
    def test_im4p_raw_and_lzfse(self):
        self.assertEqual(root.im4p(fake_im4p('trst', b'payload')), ('trst', b'payload'))
        lz = b'bvx-' + struct.pack('<I', 3) + b'abc' + b'bvx$'
        self.assertEqual(root.im4p(fake_im4p('krnl', lz))[1], b'abc')
        self.assertEqual(root.im4p(fake_im4p('krnl', bytes(300)))[1], bytes(300))	# long-form DER length
        self.assertRaises(ValueError, root.im4p, fake_im4p('rkrn', b'x', kbag=True))
        self.assertEqual(root.unwrap(b'\x02\0\0\0raw'), b'\x02\0\0\0raw')

    def test_module_sorted_unique(self):
        hs = [bytes([9 - i]) * 20 for i in range(5)] + [bytes([5]) * 20]
        m = root.tc_module(hs)
        uid, ents = root.tc_parse(m)
        self.assertEqual([e[0] for e in ents], sorted(set(hs)))
        self.assertTrue(all(e[1:] == (2, 0, 0, 0) for e in ents))
        self.assertEqual(len(m), 24 + 24 * 5)
        self.assertEqual(root.tc_module(list(reversed(hs))), m)	# uuid5 is content derived
        self.assertRaises(ValueError, root.tc_module, [b'short'])

    def test_module_rejects(self):
        m = bytearray(root.tc_module([b'\1' * 20, b'\2' * 20]))
        bad = bytearray(m); bad[0] = 1; self.assertRaises(ValueError, root.tc_parse, bytes(bad))
        bad = bytearray(m); bad[24:44], bad[48:68] = m[48:68], m[24:44]; self.assertRaises(ValueError, root.tc_parse, bytes(bad))
        bad = bytearray(m); bad[44] = 9; self.assertRaises(ValueError, root.tc_parse, bytes(bad))
        self.assertRaises(ValueError, root.tc_parse, bytes(m[:-1]))

    def test_bundle_roundtrip_and_loader_rules(self):
        a, b = root.tc_module([b'\1' * 20]), root.tc_module([b'\3' * 20, b'\4' * 20], 'other')
        bundle = root.tc_bundle([a, b])
        self.assertEqual(struct.unpack_from('<3I', bundle), (2, 12, 12 + len(a)))
        self.assertEqual(root.tc_split(bundle), [a, b])
        self.assertRaises(ValueError, root.tc_bundle, [])
        self.assertRaises(ValueError, root.tc_split, struct.pack('<I', 0))
        bad = bytearray(bundle); struct.pack_into('<I', bad, 4, 13); self.assertRaises(ValueError, root.tc_split, bytes(bad))
        bad = bytearray(bundle); struct.pack_into('<I', bad, 4, 4); self.assertRaises(ValueError, root.tc_split, bytes(bad))


class Signing(unittest.TestCase):
    def test_arm64e_init_and_cdhash(self):
        obj = fake_object(CODE)
        plain, e = mkinit.build(obj), mkinit.build(obj, arm64e=True)
        self.assertEqual(struct.unpack_from('<3I', plain), (0xfeedfacf, 0x0100000c, 0))
        self.assertEqual(struct.unpack_from('<3I', e), (0xfeedfacf, 0x0100000c, 0x80000002))
        self.assertEqual(e[0x400:0x400 + len(CODE)], CODE)
        so = mkinit.SEG						# the signature follows the one __TEXT page
        cd_off = struct.unpack_from('>I', e, so + 16)[0]
        cd_len = struct.unpack_from('>I', e, so + cd_off + 4)[0]
        self.assertEqual(root.cdhash(e), hashlib.sha256(e[so + cd_off:so + cd_off + cd_len]).digest()[:20])
        self.assertNotEqual(root.cdhash(e), root.cdhash(plain))		# the header is inside the hashed page
        self.assertRaises(ValueError, root.cdhash, obj)		# objects carry no signature


class Image(unittest.TestCase):
    def test_windows_manifest(self):
        rnd = random.Random(2)
        with tempfile.TemporaryDirectory() as d:
            p = lambda n: os.path.join(d, n)
            open(p('os'), 'wb').write(rnd.randbytes(4096) + bytes(3 * (1 << 20)) + rnd.randbytes(512))
            open(p('sys'), 'wb').write(rnd.randbytes(1024))
            open(p('data'), 'wb').write(rnd.randbytes(2048))
            ents = root.image_build(p('img'), [('OS', p('os'), True), ('SystemOS', p('sys'), True), ('Data', p('data'), False)],
                                    log=lambda *a: None)
            img = open(p('img'), 'rb').read()
            total, parsed = root.image_parse(img[:4096])
            self.assertEqual(parsed, ents); self.assertEqual(total, len(img))
            self.assertEqual([e['offset'] for e in ents], [1 << 20, 5 << 20, 6 << 20])
            self.assertEqual(img[:8], b'SHNGROOT'); self.assertEqual(struct.unpack_from('<IIQ', img, 8), (1, 3, 7 << 20))
            self.assertEqual(struct.unpack_from('<32sQQII', img, 24 + 128 * 2)[3:], (512, 0))
            self.assertEqual(img[1 << 20:(1 << 20) + os.path.getsize(p('os'))], open(p('os'), 'rb').read())
            self.assertEqual(ents[0]['sha256'], hashlib.sha256(open(p('os'), 'rb').read()).hexdigest())
            root.image_verify(p('img'), parsed)
            with open(p('img'), 'r+b') as f: f.seek((5 << 20) + 10); f.write(b'X')
            self.assertRaises(ValueError, root.image_verify, p('img'), parsed)
            bad = bytearray(img[:4096]); bad[30] ^= 1; self.assertRaises(ValueError, root.image_parse, bytes(bad))
            open(p('odd'), 'wb').write(b'x' * 100)
            self.assertRaises(ValueError, root.image_build, p('x'), [('OS', p('odd'), True)], lambda *a: None)
            self.assertRaises(ValueError, root.image_build, p('x'), [('N' * 32, p('sys'), True)], lambda *a: None)


class Mockfs(unittest.TestCase):
    def test_layout_and_kernel_checks(self):
        img = mockfs.build(b'L' * 5000, b'T' * 100)
        self.assertEqual(struct.unpack_from(mockfs.HDR_FMT, img), (b'N4KROOT\0', 1, 2, 4096, 5000, 12288, 100))
        self.assertEqual(len(img), 16384); self.assertEqual(mockfs.parse(img), (b'L' * 5000, b'T' * 100))
        small = mockfs.build(b'L')
        self.assertEqual(struct.unpack_from(mockfs.HDR_FMT, small)[5], 8192)	# test file never below 8192
        self.assertEqual(mockfs.parse(small), (b'L', b'L'))
        self.assertRaises(ValueError, mockfs.build, b'L' * (32 << 20))
        bad = bytearray(img); struct.pack_into('<Q', bad, 32, 12289); self.assertRaises(ValueError, mockfs.parse, bytes(bad))
        bad = bytearray(img); struct.pack_into('<Q', bad, 40, 1 << 20); self.assertRaises(ValueError, mockfs.parse, bytes(bad))

    def test_cli_trust_module(self):
        init = mkinit.build(fake_object(CODE), arm64e=True)
        with tempfile.TemporaryDirectory() as d:
            p = lambda n: os.path.join(d, n)
            open(p('init'), 'wb').write(init)
            known = root.tc_module([root.cdhash(mkinit.build(fake_object(CODE)))]); open(p('known.tc'), 'wb').write(known)
            mockfs.main([p('img'), '--launchd', p('init'), '--test', p('known.tc'), '--trust', p('own.tc'), '--known-tc', p('known.tc')])
            _, ents = root.tc_parse(open(p('own.tc'), 'rb').read())
            self.assertEqual([e[0] for e in ents], [root.cdhash(init)])		# only the unsigned-for Mach-O, not data


class DataStage(unittest.TestCase):
    def test_tweaks(self):
        class A: lite, disable, job_env = True, ['com.example.x'], ['L:K=1', 'L:J=2', 'L:J=']
        with tempfile.TemporaryDirectory() as d:
            s = root.Stage(d); os.makedirs(s.root)
            labels = set(root.early_disable(True)) | {'com.example.x'}
            known, unknown = root.tweaks(s, A, '99Z999', labels)
            self.assertEqual(unknown, []); self.assertIn('com.apple.fairplayd.H2', known)
            dis = plistlib.load(open(s.p('db/com.apple.xpc.launchd/disabled.plist'), 'rb'))
            self.assertEqual(set(dis), labels); self.assertTrue(all(v is True for v in dis.values()))
            mig = plistlib.load(open(s.p('mobile/Library/Preferences/com.apple.migration.plist'), 'rb'))
            self.assertEqual(mig['LastSystemVersion'], '99Z999')
            self.assertEqual(plistlib.load(open(s.p('db/fuxi/job-env.plist'), 'rb')), {'L': {'K': '1'}})
            self.assertFalse(plistlib.load(open(s.p('root/Library/Preferences/com.apple.backboardd.plist'), 'rb'))['CAEnableDeepFramebuffer'])
            os.makedirs(os.path.dirname(s.p(root.MG_CACHE)), exist_ok=True); open(s.p(root.MG_CACHE), 'w').close()
            s.remove(root.MG_CACHE); self.assertFalse(os.path.exists(s.p(root.MG_CACHE)))
            n = s.finish()
            meta = dict((l.split('\t')[2].strip(), l.split('\t')[:2]) for l in open(os.path.join(d, 'meta.tsv')))
            self.assertEqual(meta['containers/Bundle'], ['40755', '33:33'])
            self.assertEqual(meta['mobile/Library/Preferences/com.apple.purplebuddy.plist'], ['100600', '501:501'])
            self.assertEqual(len(meta), n)
            sh = open(os.path.join(d, 'apply-owners.sh')).read()
            self.assertIn("chown -h 33:33 'containers/Bundle'", sh)
            known2, unknown2 = root.tweaks(root.Stage(d), A, '1', {'com.example.x'})
            self.assertIn('com.apple.locationd', unknown2)		# labels missing from the image are reported, not written


if __name__ == '__main__':
    unittest.main()
