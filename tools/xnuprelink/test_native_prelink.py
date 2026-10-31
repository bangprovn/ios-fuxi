#!/usr/bin/env python3
"""Synthetic prelink images test dependency and binding refusal paths."""
import base64
import struct
import unittest
import xml.etree.ElementTree as ET

from native_prelink import macho, plan, unserialize_xml


def segment(name, va, size, off, length):
    return struct.pack('<II16s4Q4I', 0x19, 72, name.encode(), va, size, off, length, 7, 3, 0, 0)


def header(kind, commands, subtype=0):
    return struct.pack('<8I', 0xfeedfacf, 0x100000c, subtype, kind, len(commands),
                       sum(map(len, commands)), 0, 0) + b''.join(commands)


def xml_value(value):
    if isinstance(value, dict):
        element = ET.Element('dict')
        for key, child in value.items():
            ET.SubElement(element, 'key').text = key
            element.append(xml_value(child))
    elif isinstance(value, list):
        element = ET.Element('array')
        element.extend(xml_value(child) for child in value)
    elif isinstance(value, bool):
        element = ET.Element('true' if value else 'false')
    elif isinstance(value, int):
        element = ET.Element('integer'); element.text = hex(value)
    elif isinstance(value, bytes):
        element = ET.Element('data'); element.text = base64.b64encode(value).decode()
    else:
        element = ET.Element('string'); element.text = value
    return element


def fixture(*, aliases=False, missing=False, dependency=True, duplicate=False,
            overlap=False, straddling=False, missing_dependency=False):
    # Independent kernel aliases either agree, or intentionally disagree.
    names = b'\0' + (b'_other' if missing else b'_kpi') + b'\0_alias\0'
    original_names = b'\0_kpi\0_alias\0'
    native = bytearray(0x200)
    native_header = header(2, [segment('__TEXT', 0x100000, 0x100, 0, 0x100),
                               struct.pack('<6I', 2, 24, 0x100, 2, 0x120, len(names))])
    native[:len(native_header)] = native_header
    second_name = names.index(b'_alias')
    struct.pack_into('<IBBHQ', native, 0x100, 1, 0x0f, 1, 0, 0x100080)
    struct.pack_into('<IBBHQ', native, 0x110, second_name, 0x0f, 1, 0, 0x100088 if aliases else 0x100080)
    native[0x120:0x120 + len(names)] = names
    entries = [{'CFBundleIdentifier': 'test.a', '_PrelinkExecutableLoadAddr': 0x2000,
                '_PrelinkExecutableSize': 0x100,
                'OSBundleLibraries': ({'test.b': '1', 'test.kernel': '1'} if dependency else {})},
               {'CFBundleIdentifier': 'test.b', '_PrelinkExecutableLoadAddr': 0x3000,
                '_PrelinkExecutableSize': 0x100},
               {'CFBundleIdentifier': 'test.kernel', 'OSKernelResource': True}]
    if missing_dependency:
        entries.pop(1)
    # With missing=True, neither original alias may remain available.
    if missing:
        native[0x120:0x120 + len(names)] = names.replace(b'_alias', b'_xxxxx')
    offsets = [0x80, 0x80 if duplicate else (0xfc if straddling else 0x88)]
    metadata = {'_PrelinkInfoDictionary': entries,
                '_PrelinkLinkKASLROffsets': struct.pack('<3I', 2, *offsets)}
    xml = ET.tostring(xml_value(metadata)) + b'\0'
    original = bytearray(0x6000)
    original_header = header(2, [segment('__PRELINK_TEXT', 0x2000, 0x2000, 0x1000, 0x2000),
        segment('__PRELINK_INFO', 0x8000, 0x1000, 0x4000, len(xml)),
        struct.pack('<6I', 2, 24, 0x5000, 2, 0x5020, len(original_names))])
    original[:len(original_header)] = original_header
    for at, va in [(0x1000, 0x2000), (0x2000, 0x3000)]:
        extent = 0x1100 if overlap and va == 0x2000 else 0x100
        module = header(11, [segment('__TEXT', va, extent, 0, 0x100)])
        original[at:at + len(module)] = module
    struct.pack_into('<Q', original, 0x1080, 0x3080)
    struct.pack_into('<Q', original, 0x10fc if straddling else 0x1088, 0x5000)
    original[0x4000:0x4000 + len(xml)] = xml
    for at, strx in [(0x5000, 1), (0x5010, 6)]:
        struct.pack_into('<IBBHQ', original, at, strx, 0x0f, 1, 0, 0x5000)
    original[0x5020:0x5020 + len(original_names)] = original_names
    return bytes(native), bytes(original)


class NativePrelinkTests(unittest.TestCase):
    def test_exact_pointer_binding_and_closure(self):
        report = plan(*fixture(), ['test.a'], 0x4000)
        self.assertEqual(report['components'], ['test.a', 'test.b'])
        self.assertEqual(report['kernel_interfaces'], ['test.kernel'])
        self.assertEqual(report['pointer_counts'], {'component': 1, 'kernel': 1})
        self.assertEqual([r['new_target'] for r in report['bindings']], [0x7080, 0x100080])
        self.assertFalse(report['loadable'])
        self.assertFalse(report['images_modified'])

    def test_refuse_ambiguous_aliases(self):
        with self.assertRaisesRegex(ValueError, '2 native address candidates'):
            plan(*fixture(aliases=True), ['test.a'], 0x4000)

    def test_refuse_missing_exports(self):
        with self.assertRaisesRegex(ValueError, '0 native address candidates'):
            plan(*fixture(missing=True), ['test.a'], 0x4000)

    def test_refuse_undeclared_pointer_dependency(self):
        with self.assertRaisesRegex(ValueError, 'Undeclared pointer dependency'):
            plan(*fixture(dependency=False), ['test.a'], 0x4000)

    def test_refuse_missing_declared_dependency(self):
        with self.assertRaisesRegex(ValueError, 'Missing declared dependency'):
            plan(*fixture(missing_dependency=True), ['test.a'], 0x4000)

    def test_refuse_duplicate_pointer_sites(self):
        with self.assertRaisesRegex(ValueError, 'Duplicate pointer relocation'):
            plan(*fixture(duplicate=True), ['test.a'], 0x4000)

    def test_refuse_component_overlap(self):
        with self.assertRaisesRegex(ValueError, 'Overlapping component'):
            plan(*fixture(overlap=True), ['test.a'], 0x4000)

    def test_refuse_straddling_pointer(self):
        with self.assertRaisesRegex(ValueError, 'Pointer spans'):
            plan(*fixture(straddling=True), ['test.a'], 0x4000)

    def test_refuse_native_kernel_collision(self):
        with self.assertRaisesRegex(ValueError, 'collides with native'):
            plan(*fixture(), ['test.a'], 0xfe000)

    def test_refuse_misaligned_slide(self):
        with self.assertRaisesRegex(ValueError, '4K alignment'):
            plan(*fixture(), ['test.a'], 1)

    def test_refuse_arm64e(self):
        data = bytearray(fixture()[0]); struct.pack_into('<I', data, 8, 2)
        with self.assertRaisesRegex(ValueError, 'ordinary'):
            macho(data)

    def test_xml_references_and_duplicate_rejection(self):
        self.assertEqual(unserialize_xml(b'<array><string ID="0">same</string><string IDREF="0"/></array>'), ['same', 'same'])
        with self.assertRaisesRegex(ValueError, 'Duplicate XML dictionary'):
            unserialize_xml(b'<dict><key>x</key><true/><key>x</key><false/></dict>')


if __name__ == '__main__':
    unittest.main()
