#!/usr/bin/env python3
"""Exercise candidate emission using wholly synthetic component images."""
import hashlib
import struct
import unittest
import xml.etree.ElementTree as ET

from emit_prelink import ADLER32_VECTOR_DIGEST, VECTOR_REPEAT_DIGEST, emit
from native_prelink import backed_offset, macho, unserialize_xml
from test_native_prelink import header, segment, xml_value


def section_segment(name, va, size, off, length, section_name, section_va, section_size, section_off):
    command = bytearray(segment(name, va, size, off, length))
    struct.pack_into('<I', command, 4, 152)
    struct.pack_into('<I', command, 64, 1)
    command.extend(struct.pack('<16s16sQQ8I', section_name.encode(), name.encode(),
                               section_va, section_size, section_off, 2, 0, 0, 0, 0, 0, 0))
    return bytes(command)


def images():
    native = bytearray(0x2000)
    commands = [section_segment('__TEXT', 0x100000, 0x1000, 0, 0x1000, '__const', 0x100900, 4, 0x900)]
    for name, section_name in [('__PRELINK_TEXT', '__text'), ('__PLK_TEXT_EXEC', '__text'),
                               ('__PLK_DATA_CONST', '__data'), ('__PRELINK_DATA', '__data'),
                               ('__PRELINK_INFO', '__info')]:
        commands.append(section_segment(name, 0x101000, 0, 0, 0, section_name, 0x101000, 0, 0))
    names = b'\0_kpi\0_kalloc_sparse_prelink_index_version\0_native_prelink_security_init_version\0'
    commands.append(struct.pack('<6I', 2, 24, 0x1000, 3, 0x1030, len(names)))
    blob = header(2, commands); native[:len(blob)] = blob
    struct.pack_into('<IBBHQ', native, 0x1000, 1, 0x0f, 1, 0, 0x100800)
    struct.pack_into('<IBBHQ', native, 0x1010, 6, 0x0f, 1, 0, 0x100900)
    struct.pack_into('<IBBHQ', native, 0x1020, names.index(b'_native_prelink_security_init_version'), 0x0f, 1, 0, 0x100904)
    native[0x1030:0x1030 + len(names)] = names
    struct.pack_into('<I', native, 0x900, 1)
    struct.pack_into('<I', native, 0x904, 1)
    original = bytearray(0x7000)
    commands = [segment('__TEXT', 0x9000, 0x1000, 0, 0x1000)]
    for name, va, off in [('__PRELINK_TEXT', 0x2000, 0x1000), ('__PLK_TEXT_EXEC', 0x3000, 0x2000),
                           ('__PLK_DATA_CONST', 0x4000, 0x3000), ('__PRELINK_DATA', 0x5000, 0x4000),
                           ('__PRELINK_INFO', 0x6000, 0x5000)]:
        commands.append(segment(name, va, 0x1000, off, 0x1000))
    commands.append(struct.pack('<6I', 2, 24, 0x6000, 1, 0x6010, 6))
    blob = header(2, commands); original[:len(blob)] = blob
    struct.pack_into('<IBBHQ', original, 0x6000, 1, 0x0f, 1, 0, 0x9000)
    original[0x6010:0x6016] = b'\0_kpi\0'
    commands = [segment('__TEXT', 0x2000, 0x400, 0, 0x400),
        section_segment('__TEXT_EXEC', 0x3000, 0x1000, 0x1000, 0x1000, '__text', 0x3000, 4, 0x1000),
        segment('__DATA_CONST', 0x4000, 0x1000, 0x2000, 0x1000),
        segment('__DATA', 0x5000, 0x1000, 0x3000, 0x1000)]
    blob = header(11, commands); original[0x1000:0x1000 + len(blob)] = blob
    text_exec = next(s for s in macho(original, 0x1000)['segments'] if s['name'] == '__TEXT_EXEC')
    struct.pack_into('<2I', original, text_exec['command_offset'] + 56, 5, 5)
    struct.pack_into('<I', original, 0x2000, 0xd65f03c0)  # RET, own code.
    struct.pack_into('<2Q', original, 0x3000, 0x9000, 0x3000)
    struct.pack_into('<Q', original, 0x4000 + 156, 0x2000)
    struct.pack_into('<2Q', original, 0x4000 + 180, 0x3000, 0x3000)
    metadata = {'_PrelinkInfoDictionary': [{'CFBundleIdentifier': 'test.component',
        '_PrelinkExecutableLoadAddr': 0x2000, '_PrelinkExecutableSourceAddr': 0x2000,
        '_PrelinkExecutableSize': 0x400, '_PrelinkKmodInfo': 0x5000,
        'CFBundleName': 'Native © <test> & fixture', 'TestFlag': True, 'EmptyArray': [],
        'AppleSecurityExtension': True}],
        '_PrelinkLinkKASLROffsets': struct.pack('<5I', 4, 0x2000, 0x2008, 0x30b4, 0x30bc)}
    blob = ET.tostring(xml_value(metadata)) + b'\0'; original[0x5000:0x5000 + len(blob)] = blob
    return bytes(native), bytes(original)


class EmitTests(unittest.TestCase):
    def test_explicit_early_security_initializer_metadata(self):
        native, original = images()
        output, report = emit(native, original, ['test.component'], 0x4000, {'test.component': 0x3000})
        self.assertEqual(report['early_security_initializers'], {'test.component': 0x7000})
        info = next(s for s in macho(output)['segments'] if s['name'] == '__PRELINK_INFO')
        entry = unserialize_xml(output[info['fileoff']:info['fileoff'] + info['filesize']])['_PrelinkInfoDictionary'][0]
        self.assertEqual(entry['_NativePrelinkEarlySecurityInit'], 0x7000)
        at = backed_offset(macho(output)['segments'], 0x7000, 0x1000, len(output))
        self.assertEqual(output[at:at + 0x1000], original[0x2000:0x3000])

    def test_refuse_early_security_initializer_without_source_abi(self):
        native, original = images()
        native = native.replace(b'_native_prelink_security_init_version', b'_absent_prelink_security_init_version')
        with self.assertRaisesRegex(ValueError, 'lacks early security initializer ABI'):
            emit(native, original, ['test.component'], 0x4000, {'test.component': 0x3000})
        native, original = images(); native = bytearray(native)
        struct.pack_into('<I', native, 0x904, 2)
        with self.assertRaisesRegex(ValueError, 'Unsupported early security initializer ABI'):
            emit(native, original, ['test.component'], 0x4000, {'test.component': 0x3000})

    def test_refuse_early_security_initializer_outside_module_code(self):
        native, original = images()
        for address in (0x3001, 0x2000, 0x4000, 0x9000):
            with self.subTest(address=address), self.assertRaisesRegex(ValueError, 'outside original module code'):
                emit(native, original, ['test.component'], 0x4000, {'test.component': address})
        with self.assertRaisesRegex(ValueError, 'selected security extension'):
            emit(native, original, ['test.component'], 0x4000, {'test.unselected': 0x3000})

    def test_refuse_nonsecurity_and_inherited_initializer(self):
        native, original = images()
        original = bytearray(original)
        metadata = unserialize_xml(original[0x5000:0x6000])
        metadata['_PrelinkInfoDictionary'][0]['AppleSecurityExtension'] = False
        blob = ET.tostring(xml_value(metadata)) + b'\0'
        original[0x5000:0x6000] = blob.ljust(4096, b'\0')
        with self.assertRaisesRegex(ValueError, 'selected security extension'):
            emit(native, original, ['test.component'], 0x4000, {'test.component': 0x3000})
        metadata['_PrelinkInfoDictionary'][0]['_NativePrelinkEarlySecurityInit'] = 0x3000
        blob = ET.tostring(xml_value(metadata)) + b'\0'
        original[0x5000:0x6000] = blob.ljust(4096, b'\0')
        with self.assertRaisesRegex(ValueError, 'input metadata'):
            emit(native, original, ['test.component'], 0x4000)

    def test_candidate_metadata_header_pointers_and_original_code(self):
        native, original = images()
        hashes = [hashlib.sha256(data).digest() for data in (native, original)]
        output, report = emit(native, original, ['test.component'], 0x4000)
        self.assertEqual(hashes, [hashlib.sha256(data).digest() for data in (native, original)])
        self.assertEqual(report['module_entrypoints']['test.component']['start_va'], 0x7000)
        kmod = backed_offset(macho(output)['segments'], 0x9000 + 156, 8, len(output))
        self.assertEqual(struct.unpack_from('<Q', output, kmod)[0], 0x6000)
        segments = macho(output)['segments']
        at = backed_offset(segments, 0x7000, 0x1000, len(output))
        self.assertEqual(output[at:at + 0x1000], original[0x2000:0x3000])
        at = backed_offset(segments, 0x8000, 16, len(output))
        self.assertEqual(struct.unpack_from('<2Q', output, at), (0x100800, 0x7000))
        at = backed_offset(segments, 0x6000, 32, len(output))
        component = macho(output, at)
        text = next(s for s in component['segments'] if s['name'] == '__TEXT_EXEC')
        self.assertEqual((text['va'], text['sections'][0]['va']), (0x7000, 0x7000))
        self.assertEqual(text['sections'][0]['fileoff'], 0x1000)
        info = next(s for s in segments if s['name'] == '__PRELINK_INFO')
        xml = output[info['fileoff']:info['fileoff'] + info['filesize']]
        self.assertIn('©'.encode(), xml)
        self.assertNotIn(b'&#169;', xml)
        self.assertNotIn(b' />', xml)
        self.assertIn(b'<true/>', xml)
        metadata = unserialize_xml(xml)
        entry = metadata['_PrelinkInfoDictionary'][0]
        self.assertEqual((entry['_PrelinkExecutableLoadAddr'], entry['_PrelinkKmodInfo']), (0x6000, 0x9000))
        self.assertFalse(report['runtime_qualified'])
        self.assertEqual(report['sparse_index']['headers'], [0x6000])
        text = next(s for s in segments if s['name'] == '__PRELINK_TEXT')
        index = next(s for s in text['sections'] if s['name'] == '__macho_list')
        self.assertEqual(struct.unpack_from('<Q', output, index['fileoff'])[0], 0x6000)

    def test_refuse_unhandled_external_branch(self):
        native, original = images(); original = bytearray(original)
        struct.pack_into('<I', original, 0x2000, 0x14000400)  # B from 0x3000 to 0x4000: internal.
        emit(native, original, ['test.component'], 0x4000)
        struct.pack_into('<I', original, 0x2000, 0x14002000)  # B to 0xb000: outside collection.
        with self.assertRaisesRegex(ValueError, 'External PC-relative'):
            emit(native, original, ['test.component'], 0x4000)

    def test_exact_mathematical_vector_literals_preserved(self):
        native, original = images(); original = bytearray(original)
        table = bytes(i % distance if distance else 0 for distance in range(16) for i in range(32))
        table += bytes(32 if d < 3 else 16 if d in (4, 8) else (32 // d) * d for d in range(16))
        self.assertEqual(hashlib.sha256(table).hexdigest(), VECTOR_REPEAT_DIGEST)
        original[0x2100:0x2100 + len(table)] = table
        output, report = emit(native, original, ['test.component'], 0x4000)
        self.assertEqual(report['known_inline_data'], [dict(module='test.component', va=0x3100, size=528, sha256=VECTOR_REPEAT_DIGEST)])
        at = backed_offset(macho(output)['segments'], 0x7100, len(table), len(output))
        self.assertEqual(output[at:at + len(table)], table)
        for changed in (0, 64, 100, 527):
            bad = bytearray(original); bad[0x2100 + changed] ^= 1
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, 'External PC-relative'):
                emit(native, bad, ['test.component'], 0x4000)
        struct.pack_into('<I', original, 0x2100 + len(table), 0x14002000)
        with self.assertRaisesRegex(ValueError, 'External PC-relative'):
            emit(native, original, ['test.component'], 0x4000)

    def test_exact_adler_coefficients_preserved(self):
        native, original = images(); original = bytearray(original)
        table = bytes(range(32, 0, -1)) + struct.pack('<II', 65521, 0x80078071)
        self.assertEqual(hashlib.sha256(table).hexdigest(), ADLER32_VECTOR_DIGEST)
        original[0x2100:0x2100 + len(table)] = table
        output, report = emit(native, original, ['test.component'], 0x4000)
        self.assertEqual(report['known_inline_data'], [dict(module='test.component', va=0x3100, size=40, sha256=ADLER32_VECTOR_DIGEST)])
        at = backed_offset(macho(output)['segments'], 0x7100, len(table), len(output))
        self.assertEqual(output[at:at + len(table)], table)
        for changed in (0, 32, 39):
            bad = bytearray(original); bad[0x2100 + changed] ^= 1
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, 'External PC-relative'):
                emit(native, bad, ['test.component'], 0x4000)

    def test_refuse_missing_sparse_index_abi(self):
        native, original = images()
        native = native.replace(b'_kalloc_sparse_prelink_index_version', b'_unsupported_prelink_index_version__')
        with self.assertRaisesRegex(ValueError, 'lacks sparse-prelink index ABI'):
            emit(native, original, ['test.component'], 0x4000)

    def test_refuse_unknown_sparse_index_abi(self):
        native, original = images(); native = bytearray(native)
        struct.pack_into('<I', native, 0x900, 2)
        with self.assertRaisesRegex(ValueError, 'Unsupported native sparse-prelink'):
            emit(native, original, ['test.component'], 0x4000)

    def test_refuse_missing_native_placeholder(self):
        native, original = images(); native = bytearray(native)
        at = next(s['command_offset'] for s in macho(native)['segments'] if s['name'] == '__PLK_TEXT_EXEC')
        native[at + 8:at + 24] = b'__NOT_PRELINK'.ljust(16, b'\0')
        with self.assertRaisesRegex(ValueError, 'Expected one empty native'):
            emit(native, original, ['test.component'], 0x4000)


if __name__ == '__main__':
    unittest.main()
