"""Reject malformed linker records and verify independently relocated references."""
import struct
from types import SimpleNamespace
import unittest

from fileset_layout import split_references, encode_uleb, relocate_native, Layout


def records(groups):
    result = bytearray(b'\x7f') + encode_uleb(len(groups))
    for source, target, targets in groups:
        for value in (source, target, len(targets)):
            result.extend(encode_uleb(value))
        for delta, kinds in targets:
            for value in (delta, len(kinds)):
                result.extend(encode_uleb(value))
            for kind, deltas in kinds:
                for value in (kind, len(deltas), *deltas):
                    result.extend(encode_uleb(value))
    return bytes(result)


def image_fixture(blob):
    return SimpleNamespace(data=struct.pack('<4I', 0x1e, 16, 16, len(blob))+blob,
                           command=lambda _: (0, 16), sections=[{}, {}])


class SplitSegmentTests(unittest.TestCase):
    def test_offsets_accumulate_per_target_but_reset_per_kind(self):
        blob = records([(1, 2, [(8, [(5, [4, 12]), (6, [8, 12])]),
                               (16, [(5, [32])])])])
        self.assertEqual(list(split_references(image_fixture(blob))),
                         [(5, 1, 4, 2, 8), (5, 1, 16, 2, 8),
                          (6, 1, 8, 2, 8), (6, 1, 20, 2, 8), (5, 1, 32, 2, 24)])

    def test_truncation_overflow_invalid_kind_and_section_are_rejected(self):
        valid = records([(1, 2, [(0, [(7, [0])])])])
        for blob in (valid[:-1], b'\x7f'+b'\xff'*10, b'\x01', valid+b'\x01',
                     records([(1, 3, [])]), records([(1, 2, [(0, [(14, [0])])])])):
            with self.subTest(blob=blob), self.assertRaises(ValueError):
                list(split_references(image_fixture(blob)))

    def test_section_ordinal_distinguishes_empty_marker_from_data(self):
        layout = Layout.__new__(Layout)
        layout.native = SimpleNamespace(base=0x10000, segments=[
            dict(name='__LAST', va=0x20000, size=0),
            dict(name='__DATA', va=0x20000, size=0x4000)], sections=[
            dict(segment='__LAST', va=0x20000, size=0),
            dict(segment='__DATA', va=0x20000, size=0x100)])
        layout.placements = {'__LAST': 0x80000, '__DATA': 0xa0000}
        self.assertEqual(layout.section_address(1, 0, True), 0x80000)
        self.assertEqual(layout.section_address(2, 0, True), 0xa0000)
        self.assertEqual(layout.translate(0x20000), 0xa0000)

    def test_native_branch_relocation_checks_old_target_and_signed_range(self):
        source, target = 0x10000, 0x20000
        blob = records([(1, 2, [(0, [(7, [0])])])])
        native = image_fixture(blob)
        native.sections = [dict(va=source), dict(va=target)]
        native.fixups = lambda: {}
        word = 0x94000000 | ((target-source)//4)
        native.read = lambda a, n: struct.pack('<I', word)
        layout = SimpleNamespace(native=native, cache=SimpleNamespace(base=0),
                                 section_address=lambda i, o, new=False:
                                 ([source, target] if not new else [0x100, 0x80])[i-1]+o)
        output = bytearray(0x200)
        pointers, counts = relocate_native(output, layout)
        self.assertEqual(struct.unpack_from('<I', output, 0x100)[0], 0x97ffffe0)
        self.assertEqual(counts['kind_7'], 1)
        self.assertEqual(pointers, {})
        word ^= 1
        with self.assertRaisesRegex(ValueError, 'disagrees'):
            relocate_native(output, layout)


if __name__ == '__main__':
    unittest.main()
