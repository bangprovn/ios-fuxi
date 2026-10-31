"""Regression checks for copied Mach-O commands and address rewriting."""
import struct
import hashlib
import json
from pathlib import Path
import plistlib
import tempfile
import unittest

from fileset_emit import (Emit, dependency_closure, prelink_info, rebase_collection,
                          validate_map, write_candidate, chain_word, decode, MH_DYLIB_IN_CACHE,
                          inline_data_ranges, emit_images)
from kc_image import Image, signed


def collection_fixture():
    base = 0xfffffe0007004000
    def segment(name, address, offset, size):
        return struct.pack('<II16sQQQQIIII', 0x19, 72, name.encode(),
                           address, size, offset, size, 7, 5, 0, 0)
    def header(commands, kind):
        return struct.pack('<8I', 0xfeedfacf, 0x100000c, 2, kind, len(commands),
                           sum(map(len, commands)), 0, 0) + b''.join(commands)
    commands = [segment('__TEXT', base, 0, 0x1000),
                segment('__PRELINK_TEXT', base + 0x1000, 0x1000, 0x3000),
                segment('__PRELINK_INFO', base + 0x4000, 0x4000, 0x4000)]
    raw = bytearray(0x8000)
    for name, offset in (('a', 0x1000), ('b', 0x2000), ('com.apple.kernel', 0x3000)):
        string = name.encode() + b'\0'
        size = (32 + len(string) + 7) & ~7
        commands.append(struct.pack('<IIQQII', 0x80000035, size, base + offset, offset, 32, 0)
                        + string + bytes(size - 32 - len(string)))
        embedded = header([segment('__TEXT', base + offset, offset, 0x1000)], 2)
        raw[offset:offset + len(embedded)] = embedded
    root = header(commands, 12)
    raw[:len(root)] = root
    metadata = {'_PrelinkInfoDictionary': [
        {'CFBundleIdentifier': 'a', 'OSBundleLibraries': {'kpi': '1'},
         '_PrelinkExecutableLoadAddr': signed(base + 0x1000, 64),
         '_PrelinkExecutableSourceAddr': signed(base + 0x1000, 64),
         '_PrelinkKmodInfo': signed(base + 0x3300, 64)},
        {'CFBundleIdentifier': 'b', '_PrelinkExecutableLoadAddr': signed(base + 0x2000, 64)},
        {'CFBundleIdentifier': 'kpi', '_PrelinkExecutableLoadAddr': (1 << 63) - 1},
    ]}
    xml = plistlib.dumps(metadata) + b'\0'
    raw[0x4000:0x4000 + len(xml)] = xml
    struct.pack_into('<Q', raw, 0x3300 + 156, base + 0x1000)
    return Image(bytes(raw))


class FilesetEmitTests(unittest.TestCase):
    def test_inline_data_requires_entire_identity_and_code_boundaries(self):
        pool = bytes.fromhex('0000807f0000f042') + bytes(24)
        spec = [(pool[:8].hex(), len(pool), hashlib.sha256(pool).hexdigest())]
        before, after = bytes.fromhex('ff0f5fd6'), bytes.fromhex('7f2303d5')
        blob = before + pool + after
        self.assertEqual(inline_data_ranges(blob, spec), [(4, 36)])
        for position in (0, 4, 35, 36):
            changed = bytearray(blob)
            changed[position] ^= 1
            self.assertEqual(inline_data_ranges(changed, spec), [])

    def test_chain_words_reject_truncation_and_other_cache_levels(self):
        for target, step in ((-1, 0), (1 << 30, 0), (0, 1 << 12)):
            with self.assertRaisesRegex(ValueError, 'out of range'):
                chain_word(0, None, None, None, target, step)
        with self.assertRaisesRegex(ValueError, 'cache level'):
            decode(1 << 30)

    def test_copied_header_preserves_counts_uuid_and_versions(self):
        base, at, slide = 0x100000000, 0x8000, 0x200000
        segment = struct.pack('<II16sQQQQIIII', 0x19, 72, b'__TEXT',
                              base, 0x4000, 0, 0x4000, 7, 5, 0, 0)
        dysymtab = [0, 1, 1, 2, 3, 0, 0x2800, 1, 0x2820, 1,
                    0x2840, 1, 0x2860, 1, 0x2880, 1, 0x28a0, 1]
        commands = [segment, struct.pack('<6I', 2, 24, 0x2000, 1, 0x2100, 8),
                    struct.pack('<20I', 0xb, 80, *dysymtab),
                    struct.pack('<II16s', 0x1b, 24, b'unchanged-uuid!!!'),
                    struct.pack('<IIQ', 0x2a, 16, 0x123456789),
                    struct.pack('<4I', 0x26, 16, 0x2200, 16),
                    struct.pack('<4I', 0x1e, 16, 0x2300, 32),
                    struct.pack('<4I', 0x80000034, 16, 0x2400, 48)]
        raw = bytearray(0x4000)
        header = struct.pack('<8I', 0xfeedfacf, 0x100000c, 2, 2, len(commands),
                             sum(map(len, commands)), 0, 0)
        raw[:32 + sum(map(len, commands))] = header + b''.join(commands)
        struct.pack_into('<IBBHQ', raw, 0x2000, 1, 0x0f, 1, 0, base + 0x1000)
        raw[0x2100:0x2108] = b'\0_name\0\0'
        emit = Emit.__new__(Emit)
        emit.native = Image(bytes(raw))
        emit.fileoff, emit.slide = at, slide
        emit.data = bytearray(at) + raw
        emit.patch_entry_header()
        moved = Image(bytes(emit.data), at)
        self.assertTrue(struct.unpack_from('<I', emit.data, at + 24)[0] & MH_DYLIB_IN_CACHE)
        self.assertIsNone(moved.command(0x80000034))
        self.assertEqual(moved.symbols(), {base + 0x1000 + slide: ['_name']})
        for cmd in (0x1b, 0x2a):
            before, size = emit.native.command(cmd)
            after, _ = moved.command(cmd)
            self.assertEqual(raw[before:before + size], emit.data[after:after + size])
        after, _ = moved.command(2)
        self.assertEqual(struct.unpack_from('<4I', emit.data, after + 8),
                         (at + 0x2000, 1, at + 0x2100, 8))
        for cmd, offset, size in ((0x26, 0x2200, 16), (0x1e, 0x2300, 32)):
            after, _ = moved.command(cmd)
            self.assertEqual(struct.unpack_from('<2I', emit.data, after + 8), (at + offset, size))
        after, _ = moved.command(0xb)
        actual = list(struct.unpack_from('<18I', emit.data, after + 8))
        expected = [v + at if i in (6, 8, 10, 12, 14, 16) else v
                    for i, v in enumerate(dysymtab)]
        self.assertEqual(actual, expected)

    def test_adrp_preserves_signed_high_immediate_bits(self):
        for immediate in (0x34567, 0xb4567, -0x34567):
            with self.subTest(immediate=immediate):
                emit = Emit.__new__(Emit)
                emit.data = bytearray(struct.pack('<II', 0x90000000, 0x91000000))
                emit.report = {'adrp_pairs_rebound': 0}
                pc = 0x100000000
                target = pc + immediate * 0x1000 + 0xabc
                emit.patch_adrp_pair({'fileoff': 0, 'va': pc}, 0, 4, target, 0)
                word, use = struct.unpack('<II', emit.data)
                decoded = signed(((word >> 5) & 0x7ffff) << 2 | ((word >> 29) & 3), 21)
                self.assertEqual(pc + decoded * 0x1000 + ((use >> 10) & 0xfff), target)

    def test_owner_respects_segment_end_and_gaps(self):
        emit = Emit.__new__(Emit)
        emit.owner_ranges = [(10, 20, 'first'), (30, 40, 'second')]
        emit.owner_starts = [10, 30]
        self.assertEqual(emit.owner(19), 'first')
        self.assertEqual(emit.owner(20), 'com.apple.kernel')
        self.assertEqual(emit.owner(30), 'second')

    def test_rebase_metadata_and_raw_kmod_address(self):
        original = collection_fixture()
        closure = dependency_closure(original, ['a'])
        self.assertEqual(closure, {'a', 'kpi'})
        base = 0xfffffff007004000
        rebased, delta, _ = rebase_collection(original, base, closure)
        self.assertEqual(rebased.base, base)
        self.assertEqual(rebased.entries['a'], (base + 0x1000, 0x1000))
        self.assertEqual(Image(rebased.data, 0x1000).base, base + 0x1000)
        _, metadata = prelink_info(rebased)
        entries = {e['CFBundleIdentifier']: e for e in metadata['_PrelinkInfoDictionary']}
        self.assertEqual(set(entries), closure)
        self.assertEqual(entries['a']['_PrelinkExecutableLoadAddr'], signed(base + 0x1000, 64))
        self.assertEqual(entries['kpi']['_PrelinkExecutableLoadAddr'], (1 << 63) - 1)
        self.assertEqual(struct.unpack_from('<Q', rebased.data, 0x3300 + 156)[0], base + 0x1000)
        self.assertEqual(struct.unpack_from('<Q', original.data, 0x3300 + 156)[0], original.base + 0x1000)

    def test_missing_dependency_is_not_silently_ignored(self):
        with self.assertRaisesRegex(ValueError, 'Missing prelink dependency'):
            dependency_closure(collection_fixture(), ['missing'])

    def test_pairing_is_bound_to_both_inputs(self):
        inputs = {'kernelcache_sha256': hashlib.sha256(b'cache').hexdigest(),
                  'native_sha256': hashlib.sha256(b'native').hexdigest(), 'entry': 'com.apple.kernel'}
        self.assertEqual(validate_map(b'cache', b'native', {'inputs': inputs}), inputs)
        for cache, native, payload in ((b'other', b'native', {'inputs': inputs}),
                                       (b'cache', b'new native', {'inputs': inputs}),
                                       (b'cache', b'native', {})):
            with self.assertRaisesRegex(ValueError, 'Pairing inputs'):
                validate_map(cache, native, payload)

    def test_strict_failure_preserves_existing_candidate_and_writes_report(self):
        with tempfile.TemporaryDirectory() as directory:
            output, record = Path(directory) / 'kernel.macho', Path(directory) / 'kernel.json'
            output.write_bytes(b'existing')
            report = {'missing': {'a': {'0x1234': 3}}}
            with self.assertRaisesRegex(ValueError, 'Incomplete pairing: 3'):
                write_candidate(b'incomplete', report, output, record)
            self.assertEqual(output.read_bytes(), b'existing')
            self.assertFalse(json.loads(record.read_text())['candidate_written'])

    def test_lower_level_emission_rejects_stale_analysis_before_parsing_images(self):
        from fileset_match import MATCHER_VERSION, matcher_signature
        payload = {'inputs': {'kernelcache_sha256': hashlib.sha256(b'cache').hexdigest(),
                              'native_sha256': hashlib.sha256(b'native').hexdigest(),
                              'entry': 'com.apple.kernel'}}
        for version, signature in ((MATCHER_VERSION-1, matcher_signature()), (MATCHER_VERSION,'stale')):
            with self.subTest(version=version, signature=signature):
                payload.update(matcher_version=version, matcher_signature=signature)
                with self.assertRaisesRegex(ValueError, 'current matching checks'):
                    emit_images(b'cache', b'native', payload, [])


if __name__ == '__main__':
    unittest.main()
