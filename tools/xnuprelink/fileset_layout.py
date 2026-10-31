"""Segment placement and linker-authored split-segment relocations for filesets.

The v2 record grammar is documented by Apple's dyld mach_o/SplitSeg.cpp.
Section zero denotes the Mach-O header; other ordinals follow load-command order.
"""
from collections import Counter
import struct

PAGE = 0x4000
MASK64 = (1 << 64) - 1


def align(value, alignment=PAGE):
    return (value + alignment - 1) & -alignment


def split_references(image):
    found = image.command(0x1e)
    if found is None:
        raise ValueError('Native image has no split-segment relocation information')
    offset, size = struct.unpack_from('<II', image.data, found[0] + 8)
    blob = image.data[offset:offset + size]
    if len(blob) != size or not blob or blob[0] != 0x7f:
        raise ValueError('Expected complete split-segment v2 information')
    at = 1

    def uleb():
        nonlocal at
        result = 0
        for shift in range(0, 70, 7):
            if at >= len(blob):
                raise ValueError('Truncated split-segment ULEB')
            byte = blob[at]
            at += 1
            result |= (byte & 127) << shift
            if not byte & 128:
                if result > MASK64:
                    raise ValueError('Overflowing split-segment ULEB')
                return result
        raise ValueError('Overflowing split-segment ULEB')

    for _ in range(uleb()):
        source, target, count = uleb(), uleb(), uleb()
        if max(source, target) > len(image.sections):
            raise ValueError('Split-segment section ordinal is out of bounds')
        target_offset = 0
        for _ in range(count):
            target_offset += uleb()
            for _ in range(uleb()):
                kind, count_sources = uleb(), uleb()
                if not 1 <= kind <= 13:
                    raise ValueError(f'Invalid split-segment kind {kind}')
                source_offset = 0
                for _ in range(count_sources):
                    source_offset += uleb()
                    yield kind, source, source_offset, target, target_offset
    if any(blob[at:]):
        raise ValueError('Unexpected bytes after split-segment records')


def encode_uleb(value):
    if value < 0:
        raise ValueError('Negative ULEB')
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return result


class Layout:
    """Fit native segments around retained kexts, preserving their relative addresses."""
    def __init__(self, cache, native, closure):
        from kc_image import Image
        self.cache, self.native = cache, native
        self.roots = {s['name']: dict(s) for s in cache.segments}
        self.occupied = []
        for name, (_, offset) in cache.entries.items():
            if name == 'com.apple.kernel' or closure is not None and name not in closure:
                continue
            for segment in Image(cache.data, offset).segments:
                if segment['size'] and segment['name'] != '__LINKEDIT':
                    self.occupied.append((segment['va'], segment['va'] + segment['size']))
        self.placements = {}
        self.cursor = align(max(len(cache.data), max(s['va'] + s['size'] - cache.base
                                                  for s in cache.segments)))
        self.reserve('__TEXT', self.hole('__PRELINK_TEXT', self.segment('__TEXT')['size']))
        const = self.roots['__DATA_CONST']
        self.reserve('__DATA_CONST', const['va'])
        self.reserve('__LASTDATA_CONST', align(self.end('__DATA_CONST')))
        # All native code follows selected kext code; XNU derives the kext-code
        # range from the start of native __TEXT_EXEC.
        code = self.roots['__TEXT_EXEC']
        end_kept = max([code['va']] + [end for begin, end in self.occupied
                                      if code['va'] <= begin < code['va'] + code['size']])
        self.reserve('__TEXT_EXEC', align(end_kept))
        self.reserve('__KLD', align(self.end('__TEXT_EXEC')))
        self.reserve('__LAST', self.end('__KLD'))
        code_end = self.end('__KLD')
        # The old kernel boot segment and prelink XML are replaceable, provided
        # no retained kext uses them. Move XML into its own mapping below.
        if code_end > self.roots['__DATA']['va']:
            raise ValueError('Native code would collide with collection data')
        self.roots.pop('__TEXT_BOOT_EXEC', None)
        code['size'] = code['filesize'] = code_end - code['va']
        self.reserve('__DATA', self.roots['__DATA']['va'])
        link = self.roots['__LINKEDIT']
        for name in ('__LINKINFO', '__LINKEDIT'):
            self.reserve(name, self.allocate(self.segment(name)['size']))
        link['size'] = link['filesize'] = self.cursor - link['fileoff']
        # Reserve enough root-linkedit space for new starts/fixups tables. This
        # is derived from site counts, not a bound tied to one release.
        fixup_count = len(cache.fixups()) + len(native.fixups())
        self.table_begin = self.cursor
        self.allocate(align(fixup_count * 2 + len(native.function_starts()) * 10 + 0x10000))
        self.table_end = self.cursor
        link['size'] = link['filesize'] = self.cursor - link['fileoff']
        info = self.roots['__PRELINK_INFO']
        info['va'] = self.allocate(info['size'])
        info['fileoff'] = info['va'] - cache.base
        for name in ('__KLDDATA', '__BOOTDATA'):
            segment = self.segment(name)
            address = self.allocate(segment['size'])
            self.reserve(name, address)
            self.roots[name] = dict(segment, va=address, fileoff=address-cache.base,
                                    filesize=segment['size'])
        # Zero-sized markers have distinct semantics even when their old
        # addresses alias a real segment. Relocate them by section ordinal.
        for segment in native.segments:
            if segment['name'] in self.placements:
                continue
            if segment['size']:
                raise ValueError(f'Unclassified native segment {segment["name"]}')
            address = self.end('__DATA') if segment['name'] == '__PRELINK_DATA' else self.end('__KLD')
            self.reserve(segment['name'], address)
        self.size = self.cursor
        self.validate()

    def segment(self, name):
        return next(s for s in self.native.segments if s['name'] == name)

    def allocate(self, size):
        address = self.cache.base + self.cursor
        self.cursor = align(self.cursor + size)
        return address

    def reserve(self, name, address):
        self.placements[name] = address

    def end(self, name):
        return self.placements[name] + self.segment(name)['size']

    def hole(self, root_name, size):
        root = self.roots[root_name]
        cursor, limit = root['va'], root['va'] + root['size']
        for begin, end in sorted(self.occupied):
            if end <= cursor or begin >= limit:
                continue
            if align(cursor) + size <= begin:
                return align(cursor)
            cursor = max(cursor, end)
        if align(cursor) + size <= limit:
            return align(cursor)
        raise ValueError(f'No room for {size:#x} bytes in {root_name}')

    def translate(self, address):
        matches = [s for s in self.native.segments if s['va'] <= address < s['va'] + s['size']]
        if len(matches) != 1:
            raise ValueError(f'Native address has no unique segment: {address:#x}')
        segment = matches[0]
        return self.placements[segment['name']] + address - segment['va']

    def section_address(self, ordinal, offset, new=False):
        if ordinal == 0:
            if offset > self.segment('__TEXT')['size']:
                raise ValueError('Header-relative split-segment offset is out of range')
            return (self.placements['__TEXT'] if new else self.native.base) + offset
        section = self.native.sections[ordinal - 1]
        if offset > section['size']:
            raise ValueError(f'Split-segment offset exceeds section {ordinal}')
        delta = self.placements[section['segment']] - self.segment(section['segment'])['va'] if new else 0
        return section['va'] + offset + delta

    def file(self, old_offset, length=1):
        matches = [s for s in self.native.segments
                   if s['filesize'] and s['fileoff'] <= old_offset
                   and old_offset + length <= s['fileoff'] + s['filesize']]
        if len(matches) != 1:
            raise ValueError(f'Native file offset has no unique segment: {old_offset:#x}')
        segment = matches[0]
        return self.placements[segment['name']] - self.cache.base + old_offset - segment['fileoff']

    def validate(self):
        placed = []
        for segment in self.native.segments:
            if not segment['size']:
                continue
            begin, end = self.placements[segment['name']], self.end(segment['name'])
            if begin % PAGE:
                raise ValueError('Unaligned native segment')
            if any(begin < e and b < end for b, e in self.occupied + placed):
                raise ValueError(f'Native {segment["name"]} overlaps a retained segment')
            if not any(s['va'] <= begin and end <= s['va'] + s['size'] for s in self.roots.values()):
                raise ValueError(f'Native {segment["name"]} is outside collection mappings')
            placed.append((begin, end))
        ordered = sorted((s['va'], s['va'] + s['size']) for s in self.roots.values() if s['size'])
        if any(a[1] > b[0] for a, b in zip(ordered, ordered[1:])):
            raise ValueError('Root mappings overlap')
        if self.end('__LASTDATA_CONST') > min(b for b, e in self.occupied
                                            if self.roots['__DATA_CONST']['va'] <= b < self.roots['__DATA_CONST']['va'] + self.roots['__DATA_CONST']['size']):
            raise ValueError('Native constants follow retained kext constants')

    def describe(self):
        return {s['name']: {'old': hex(s['va']), 'new': hex(self.placements[s['name']]),
                            'size': hex(s['size'])} for s in self.native.segments}


def relocate_native(data, layout):
    """Apply exact linker records, retaining PAC metadata for the root chain writer."""
    from kc_image import signed
    native, base = layout.native, layout.cache.base
    fixups = native.fixups()
    pointers, counts, written = {}, Counter(), set()
    for kind, si, so, ti, to in split_references(native):
        source = layout.section_address(si, so)
        target = layout.section_address(ti, to)
        new_source = layout.section_address(si, so, True)
        new_target = layout.section_address(ti, to, True)
        width = 8 if kind in (2, 4, 13) else 4
        at = new_source - base
        original = int.from_bytes(native.read(source, width), 'little')
        if source in written:
            raise ValueError(f'Duplicate split-segment site {source:#x}')
        written.add(source)
        if kind in (2, 13):
            if source not in fixups or fixups[source][0] != target:
                raise ValueError(f'Split-segment pointer disagrees with fixup at {source:#x}')
            pointers[new_source] = (new_target,) + fixups[source][1:]
            counts['pointers'] += 1
            continue
        if kind in (3, 4):
            value = signed(original, width * 8) + (new_target-target) - (new_source-source)
            if not -(1 << (width*8-1)) <= value < 1 << (width*8-1):
                raise ValueError(f'Relative value overflow at {source:#x}')
            value &= (1 << (width*8)) - 1
        elif kind == 5:
            if original & 0x9f000000 != 0x90000000:
                raise ValueError(f'Expected ADRP at {source:#x}')
            previous = (source & ~4095) + (signed(((original >> 5) & 0x7ffff) << 2 |
                                                  ((original >> 29) & 3), 21) << 12)
            if previous != target & ~4095:
                raise ValueError(f'ADRP target disagrees with linker at {source:#x}')
            immediate = ((new_target & ~4095) - (new_source & ~4095)) >> 12
            if not -(1 << 20) <= immediate < 1 << 20:
                raise ValueError('Relocated ADRP out of range')
            value = (original & 0x9f00001f) | (((immediate >> 2) & 0x7ffff) << 5) | ((immediate & 3) << 29)
        elif kind == 6:
            if original & 0xffc00000 == 0x91000000:
                scale = 0
            elif original & 0x3b000000 == 0x39000000:
                scale = 4 if original & 0x04800000 == 0x04800000 else original >> 30
            else:
                raise ValueError(f'Expected ADD or load/store at {source:#x}')
            low = new_target & 4095
            if low % (1 << scale) or ((((original >> 10) & 4095) << scale) & 4095) != target & 4095:
                raise ValueError(f'Page offset disagrees with linker at {source:#x}')
            # Page-aligned segment moves preserve the low bits, including vector
            # loads whose scaled immediate can exceed a page.
            if (new_target - target) % PAGE:
                raise ValueError('Split-segment move must preserve page offsets')
            value = original
        elif kind == 7:
            if original & 0x7c000000 != 0x14000000 or source + signed(original & 0x3ffffff, 26)*4 != target:
                raise ValueError(f'Branch disagrees with linker at {source:#x}')
            displacement = new_target - new_source
            if displacement % 4 or not -(1 << 27) <= displacement < 1 << 27:
                raise ValueError('Relocated branch out of range')
            value = (original & 0xfc000000) | ((displacement >> 2) & 0x3ffffff)
        else:
            raise ValueError(f'Unsupported native split-segment kind {kind}')
        data[at:at + width] = value.to_bytes(width, 'little')
        counts[f'kind_{kind}'] += 1
    for site, pointer in fixups.items():
        new_site = layout.translate(site)
        if new_site not in pointers:
            pointers[new_site] = (layout.translate(pointer[0]),) + pointer[1:]
            counts['pointers_without_split_record'] += 1
    return pointers, counts
