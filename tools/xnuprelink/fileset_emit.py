#!/usr/bin/env python3
"""Rebind a fileset kernelcache onto a native kernel; kexts and their addresses stay put."""
import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import plistlib
import struct
import tempfile

from kc_image import Image, signed

LC_SEGMENT_64, LC_SYMTAB = 0x19, 0x2
LC_DYLD_CHAINED_FIXUPS, LC_FILESET_ENTRY, LC_UNIXTHREAD = 0x80000034, 0x80000035, 0x5
PAGE = 0x4000
BRANCH_MASK, BRANCH_VALUE = 0x7c000000, 0x14000000
ADRP_MASK, ADRP_VALUE = 0x9f000000, 0x90000000
ADR_MASK, ADR_VALUE = 0x9f000000, 0x10000000
MASK64 = (1 << 64) - 1
MH_DYLIB_IN_CACHE = 0x80000000

# Complete 23F84 Libm pools, between RETAB and the next PACIBSP. Each has
# ADR/literal-load references and no observed incoming direct branch. Prefixes
# alone are insufficient: a changed pool must be examined again as code/data.
FILESET_MATH_POOLS = (
    ('1f2003d500000000004a87c0f039fafe', 2156, '1187961410797e1dca751f916026de525922e55ef569770be507eaa817823a6d'),
    ('0000807f0000f0420000804cdb0f493f', 1260, '5396a940a73ec06075fe8d9fb0e36b79745424d60d2968bf6d9f2a4889b47b05'),
    ('00000000001018c00ee526157bcbdb3f', 2184, '747d8fd1b75aaa671ce2a5b32576235f4ac560f9497aa1b6dc8d7a5f8a736632'),
    ('0000c1c00000807f1f2003d5ff799f50', 1092, '08b778c55cf3169dbc5cd4729430069adfbe5a75a422fd5233ab9f2f60174fda'),
)

# Constant pools that are not bracketed by RETAB/NOP. The SHA-256 round
# constants sit directly before a BTI prologue, so a full-length digest is the
# only evidence: a changed pool must be examined again as code/data.
FILESET_DATA_POOLS = (
    ('982f8a42', 256, '74ef7306e7452d6859b6463ce496b8df30925f69e1b2969e1f3f34bbc9c6af04'),
)


def inline_data_ranges(blob, pools=FILESET_MATH_POOLS):
    result = []
    for prefix, size, digest in pools:
        search = 0
        while (search := blob.find(bytes.fromhex(prefix), search)) >= 0:
            if search >= 4 and search % 4 == 0 and blob[search - 4:search] == bytes.fromhex('ff0f5fd6') \
                    and blob[search + size:search + size + 4] == bytes.fromhex('7f2303d5') \
                    and hashlib.sha256(blob[search:search + size]).hexdigest() == digest:
                result.append((search, search + size))
            search += 4
    for prefix, size, digest in FILESET_DATA_POOLS:
        search = 0
        while (search := blob.find(bytes.fromhex(prefix), search)) >= 0:
            if search % 4 == 0 and search + size <= len(blob) and \
                    hashlib.sha256(blob[search:search + size]).hexdigest() == digest:
                result.append((search, search + size))
            search += 4
    return sorted(result)


def prelink_info(cache):
    segment = next(s for s in cache.segments if s['name'] == '__PRELINK_INFO')
    raw = cache.data[segment['fileoff']:segment['fileoff'] + segment['filesize']].split(b'\0', 1)[0]
    return segment, plistlib.loads(raw)


def dependency_closure(cache, roots):
    if not roots:
        return None
    _, metadata = prelink_info(cache)
    entries = {e['CFBundleIdentifier']: e for e in metadata['_PrelinkInfoDictionary']}
    closure, pending = set(), list(roots)
    while pending:
        identifier = pending.pop()
        if identifier in closure:
            continue
        if identifier not in entries:
            raise ValueError(f'Missing prelink dependency: {identifier}')
        closure.add(identifier)
        pending.extend(entries[identifier].get('OSBundleLibraries') or {})
    return closure


def rebase_collection(cache, base, closure=None, early_security_init=None, roots=None):
    """Move collection metadata to XNU's link address; chain targets stay relative.

    XNU start.s and arm_init.c locate the root header at VM_KERNEL_LINK_ADDRESS.
    kmod_info.address is deliberately outside the fixup chains (pack(4), +156).
    Other kmod pointers are handled by the collection's chained fixups.
    """
    delta = base - cache.base
    data = bytearray(cache.data)
    for at in [0] + sorted({offset for _, offset in cache.entries.values()}):
        image = Image(cache.data, at)
        for command, offset, length in image.commands:
            if command == LC_SEGMENT_64:
                address, = struct.unpack_from('<Q', data, offset + 24)
                struct.pack_into('<Q', data, offset + 24, (address + delta) & MASK64)
                count, = struct.unpack_from('<I', data, offset + 64)
                for index in range(count):
                    field = offset + 72 + 80 * index + 32
                    address, = struct.unpack_from('<Q', data, field)
                    struct.pack_into('<Q', data, field, (address + delta) & MASK64)
            elif command == LC_FILESET_ENTRY:
                address, = struct.unpack_from('<Q', data, offset + 8)
                struct.pack_into('<Q', data, offset + 8, (address + delta) & MASK64)
            elif command == LC_UNIXTHREAD:
                address, = struct.unpack_from('<Q', data, offset + 272)
                struct.pack_into('<Q', data, offset + 272, (address + delta) & MASK64)
            elif command == LC_SYMTAB:
                symoff, count = struct.unpack_from('<II', data, offset + 8)
                for index in range(count):
                    symbol = symoff + 16 * index
                    kind = data[symbol + 4]
                    if not kind & 0xe0 and kind & 0x0e == 0x0e:
                        value, = struct.unpack_from('<Q', data, symbol + 8)
                        struct.pack_into('<Q', data, symbol + 8, (value + delta) & MASK64)
    segment, metadata = prelink_info(cache)
    retained, dropped_personalities = [], []
    for entry in metadata['_PrelinkInfoDictionary']:
        if closure is not None and entry['CFBundleIdentifier'] not in closure:
            continue
        kmod = entry.get('_PrelinkKmodInfo', 0) & MASK64
        if kmod:
            field = cache.offset(kmod + 156, 8)
            value, = struct.unpack_from('<Q', data, field)
            if value:
                cache.offset(value)  # refuse a non-collection raw address
                struct.pack_into('<Q', data, field, (value + delta) & MASK64)
        for key in ('_PrelinkExecutableLoadAddr', '_PrelinkExecutableSourceAddr', '_PrelinkKmodInfo'):
            value = entry.get(key, 0) & MASK64
            if value not in (0, MASK64, (1 << 63) - 1):
                cache.offset(value)
                entry[key] = signed((value + delta) & MASK64, 64)
        if roots and entry['CFBundleIdentifier'] not in roots and entry.pop('IOKitPersonalities', None):
            # A dependency is loaded for its code. Its drivers would match this board's device tree
            # (cpu, pmgr, platform expert) and start against hardware they were not written for.
            dropped_personalities.append(entry['CFBundleIdentifier'])
        entry.pop('_NativePrelinkEarlySecurityInit', None)
        if entry['CFBundleIdentifier'] in (early_security_init or {}):
            # native-security-init.patch: OSKext calls this original registration routine after start.
            if entry.get('AppleSecurityExtension') is not True:
                raise ValueError(f'{entry["CFBundleIdentifier"]} is not a security extension')
            entry['_NativePrelinkEarlySecurityInit'] = signed(
                (early_security_init[entry['CFBundleIdentifier']] + delta) & MASK64, 64)
        retained.append(entry)
    missing = set(early_security_init or {}) - {e['CFBundleIdentifier'] for e in retained}
    if missing:
        raise ValueError(f'Early security initializers for components outside the collection: {sorted(missing)}')
    metadata['_PrelinkInfoDictionary'] = retained
    xml = plistlib.dumps(metadata, sort_keys=False) + b'\0'
    if len(xml) > segment['filesize']:
        raise ValueError('Rebased prelink metadata exceeds its segment')
    begin, size = segment['fileoff'], segment['filesize']
    data[begin:begin + size] = xml + bytes(size - len(xml))
    return Image(bytes(data)), delta, dropped_personalities


def ranges(segments):
    return [(s['va'], s['va'] + s['size']) for s in segments if s['size'] and s['name'] != '__LINKEDIT']


def inside(items, address):
    return any(b <= address < e for b, e in items)


def chain_word(auth, key, diversity, address_diversity, target_offset, step):
    if not 0 <= target_offset < 1 << 30 or not 0 <= step < 1 << 12:
        raise ValueError('Chained pointer target or next step is out of range')
    value = (target_offset & 0x3fffffff) | ((step & 0xfff) << 51)
    if auth:
        value |= 1 << 63 | ((key & 3) << 49) | ((diversity & 0xffff) << 32) | ((address_diversity & 1) << 48)
    return value


def decode(value):
    """Format 8 chain word -> (target offset, auth, key, diversity, address diversity, step)."""
    if (value >> 30) & 3:
        raise ValueError('Only kernel cache level 0 is supported')
    auth = value >> 63
    return (value & 0x3fffffff, auth, (value >> 49) & 3, (value >> 32) & 0xffff,
            (value >> 48) & 1, (value >> 51) & 0xfff)


class ChainReader:
    """Ordered chain sites of every root segment that carries a table."""

    def __init__(self, data, image):
        self.data, self.image = data, image
        found = image.command(LC_DYLD_CHAINED_FIXUPS)
        dataoff, _ = struct.unpack_from('<II', data, found[0] + 8)
        _, starts_offset, _, _, imports, _, _ = struct.unpack_from('<7I', data, dataoff)
        if imports:
            raise ValueError('Chained fixup imports are not supported')
        table = dataoff + starts_offset
        count, = struct.unpack_from('<I', data, table)
        self.tables = {}
        for index in range(count):
            relative, = struct.unpack_from('<I', data, table + 4 + 4 * index)
            if not relative:
                continue
            at = table + relative
            _, page_size, fmt, segment_offset, _, pages = struct.unpack_from('<IHHQIH', data, at)
            if fmt != 8 or page_size != PAGE:
                raise ValueError(f'Unsupported chain table: format {fmt}, page {page_size}')
            segment = next((s for s in image.segments if s['va'] - image.base == segment_offset), None)
            if segment is None:
                raise ValueError(f'Chain table outside known segments: {segment_offset:#x}')
            self.tables[segment['name']] = (at, pages)

    def pages(self, name):
        """page -> ordered [(offset in page, value)]."""
        found = self.tables.get(name)
        if not found:
            return {}
        at, count = found
        result = {}
        for page, start in enumerate(struct.unpack_from(f'<{count}H', self.data, at + 22)):
            if start == 0xffff:
                continue
            if start & 0x8000:
                raise ValueError('Multi-start pages are not supported')
            offset, sites = start, []
            while True:
                value, = struct.unpack_from('<Q', self.data,
                                            next(s for s in self.image.segments if s['name'] == name)['fileoff']
                                            + page * PAGE + offset)
                sites.append((offset, value))
                step = (value >> 51) & 0xfff
                if not step:
                    break
                offset += step * 4
                if offset >= PAGE:
                    raise ValueError('Chain crosses a page boundary')
            result[page] = sites
        return result


class Emit:
    def __init__(self, cache, native, pairs, closure=None):
        self.data = bytearray(cache.data)
        self.cache, self.native, self.pairs = cache, native, pairs
        _, at = cache.entries['com.apple.kernel']
        self.old = Image(bytes(cache.data), at)
        self.old_ranges = ranges(self.old.segments)
        self.entry_ranges = {}
        for name, (va, entry_at) in cache.entries.items():
            self.entry_ranges[name] = ranges(Image(bytes(cache.data), entry_at).segments)
        self.owner_ranges = sorted((begin, end, name) for name, items in self.entry_ranges.items()
                                   for begin, end in items)
        self.owner_starts = [begin for begin, _, _ in self.owner_ranges]
        self.closure = closure
        self.slide = self.fileoff = self.fixups_at = 0
        self.native_pages = {}
        self.report = Counter()
        self.missing = defaultdict(Counter)

    def in_closure(self, address):
        return self.closure is None or self.owner(address) in self.closure


    def owner(self, address):
        index = bisect_right(self.owner_starts, address) - 1
        if index >= 0:
            begin, end, name = self.owner_ranges[index]
            if address < end:
                return name
        return 'com.apple.kernel'

    def mapped(self, address):
        return inside(self.old_ranges, address)

    def rebind(self, target, site):
        if not self.mapped(target):
            return target
        native = self.pairs.get(target)
        if native is None:
            self.missing[self.owner(site)][target] += 1
            return None
        return (native + self.slide) & ((1 << 64) - 1)

    def place_kernel(self):
        """Copy the native kernel after the original file, slid above every original segment."""
        native_end = max(s['fileoff'] + s['filesize'] for s in self.native.segments if s['filesize'])
        self.fileoff = (len(self.data) + PAGE - 1) & ~(PAGE - 1)
        # Root __LINKEDIT grows over the appended file bytes. Reserve its entire
        # eventual mapping before placing the native segments, including tables.
        _, fixups_size = struct.unpack_from('<II', self.cache.data,
                                           self.cache.command(LC_DYLD_CHAINED_FIXUPS)[0] + 8)
        native_tables = sum(22 + 2 * ((s['filesize'] + PAGE - 1) // PAGE)
                            for s in self.native.segments)
        top = max(max(s['va'] + s['size'] for s in self.cache.segments if s['size']),
                  self.cache.base + self.fileoff + native_end + fixups_size + native_tables + PAGE)
        base = (top + PAGE - 1) & ~(PAGE - 1)
        self.slide = (base - self.native.base) & ((1 << 64) - 1)
        self.data.extend(bytes(self.fileoff + native_end - len(self.data)))
        for segment in self.native.segments:
            if not segment['filesize']:
                continue
            at = self.fileoff + segment['fileoff']
            self.data[at:at + segment['filesize']] = \
                self.native.data[segment['fileoff']:segment['fileoff'] + segment['filesize']]
        new = ranges(self.native.segments)
        for start, end in new:
            start, end = (start + self.slide) & ((1 << 64) - 1), (end + self.slide) & ((1 << 64) - 1)
            if any(b < end and start < e for b, e in self.old_ranges + ranges(self.cache.segments)):
                raise ValueError(f'Native segment {start:#x} collides with the original collection')

    def rewrite_native_chains(self):
        """The native kernel's own format 7 fixups become format 8 words in the copy."""
        sites_by_segment = defaultdict(dict)
        for site, value in self.native.fixups().items():
            segment = next((s for s in self.native.segments
                            if s['va'] <= site < s['va'] + s['filesize'] and s['filesize']), None)
            if segment is None:
                raise ValueError(f'Native fixup site outside file-backed segments: {site:#x}')
            sites_by_segment[segment['name']][site - segment['va']] = value
        for segment in self.native.segments:
            sites = sites_by_segment.get(segment['name'])
            if not sites:
                continue
            pages = defaultdict(list)
            for offset in sorted(sites):
                pages[offset // PAGE].append(offset)
            self.native_pages[segment['name']] = {page: offsets for page, offsets in pages.items()}
            for page, offsets in pages.items():
                for index, offset in enumerate(offsets):
                    target, auth, key, diversity, address_diversity = sites[offset]
                    step = (offsets[index + 1] - offset) // 4 if index + 1 < len(offsets) else 0
                    target_offset = (target + self.slide - self.cache.base) & MASK64
                    at = self.fileoff + segment['fileoff'] + offset
                    struct.pack_into('<Q', self.data, at,
                                     chain_word(auth, key, diversity, address_diversity, target_offset, step))
            self.report['native_chain_sites'] += len(sites)

    def rebuild_linkedit(self):
        """One starts table for the collection: patched kext chains plus converted native chains."""
        reader = ChainReader(bytes(self.data), self.cache)
        order = [s for s in self.cache.segments] + [s for s in self.native.segments if s['size']]
        blob = bytearray(4 + 4 * len(order))
        offsets = []

        def append_piece(segment_fileoff, segment_offset, pages_of_sites):
            piece = bytearray(22 + 2 * (max(pages_of_sites) + 1))
            piece[22:] = b'\xff' * (len(piece) - 22)
            for page, chain in pages_of_sites.items():
                for index, (offset, target_offset, auth, key, diversity, address_diversity) in enumerate(chain):
                    step = (chain[index + 1][0] - offset) // 4 if index + 1 < len(chain) else 0
                    at = segment_fileoff + page * PAGE + offset
                    struct.pack_into('<Q', self.data, at,
                                     chain_word(auth, key, diversity, address_diversity, target_offset, step))
                struct.pack_into('<H', piece, 22 + 2 * page, chain[0][0])
            struct.pack_into('<IHHQIH', piece, 0, len(piece), PAGE, 8, segment_offset, 0,
                             max(pages_of_sites) + 1)
            offsets.append(len(blob))
            blob.extend(piece)

        for segment in self.cache.segments:
            pages = reader.pages(segment['name'])
            if not pages:
                offsets.append(0)
                continue
            kept = {}
            for page, sites in pages.items():
                chain = []
                for offset, value in sites:
                    site = segment['va'] + page * PAGE + offset
                    if self.mapped(site):
                        self.report['old_kernel_sites_dropped'] += 1
                        continue
                    if not self.in_closure(site):
                        self.report['unselected_chain_sites_dropped'] += 1
                        continue
                    target_offset, auth, key, diversity, address_diversity, _ = decode(value)
                    target = self.cache.base + target_offset
                    if self.mapped(target):
                        if not self.in_closure(site):
                            chain.append((offset, target_offset, auth, key, diversity, address_diversity))
                            continue
                        rebound = self.rebind(target, site)
                        if rebound is None:
                            self.report['closure_pointers_unmapped'] += 1
                            chain.append((offset, target_offset, auth, key, diversity, address_diversity))
                            continue
                        target_offset = (rebound - self.cache.base) & MASK64
                        self.report['kext_pointers_rebound'] += 1
                    chain.append((offset, target_offset, auth, key, diversity, address_diversity))
                if chain:
                    kept[page] = chain
            if not kept:
                offsets.append(0)
                continue
            append_piece(segment['fileoff'], segment['va'] - self.cache.base, kept)
            self.report['kext_chain_sites_kept'] += sum(len(c) for c in kept.values())
        for segment in self.native.segments:
            if not segment['size']:
                continue
            pages = self.native_pages.get(segment['name'])
            if not pages:
                offsets.append(0)
                continue
            kept = {}
            for page, offsets_in_page in pages.items():
                chain = []
                for index, offset in enumerate(offsets_in_page):
                    value, = struct.unpack_from('<Q', self.data,
                                                self.fileoff + segment['fileoff'] + offset)
                    target_offset, auth, key, diversity, address_diversity, _ = decode(value)
                    chain.append((offset - page * PAGE, target_offset, auth, key, diversity,
                                  address_diversity))
                kept[page] = chain
            append_piece(self.fileoff + segment['fileoff'],
                         (segment['va'] + self.slide - self.cache.base) & ((1 << 64) - 1), kept)
        struct.pack_into('<I', blob, 0, len(order))
        for index, offset in enumerate(offsets):
            struct.pack_into('<I', blob, 4 + 4 * index, offset)
        self.fixups_at = (len(self.data) + 15) & ~15
        self.data.extend(bytes(self.fixups_at - len(self.data)))
        self.data.extend(struct.pack('<7I', 0, 28, 28, 28, 0, 1, 0))
        self.data.extend(blob)

    def rewrite_branches(self):
        """Kext BL/B, ADR and adrp pairs into the old kernel become native kernel references."""
        for segment in self.cache.segments:
            if segment['name'] != '__TEXT_EXEC' or not segment['filesize']:
                continue
            blob = bytes(self.cache.data[segment['fileoff']:segment['fileoff'] + segment['filesize']])
            literals = inline_data_ranges(blob)
            self.report['inline_data_pools_preserved'] += len(literals)
            self.report['inline_data_bytes_preserved'] += sum(end - begin for begin, end in literals)
            pages = {}
            for offset in range(0, len(blob) - 3, 4):
                if inside(literals, offset):
                    pages.clear()
                    continue
                word, = struct.unpack_from('<I', blob, offset)
                pc = segment['va'] + offset
                if inside(self.old_ranges, pc):
                    continue
                if word & BRANCH_MASK == BRANCH_VALUE:
                    target = pc + signed(word & 0x3ffffff, 26) * 4
                    if self.mapped(target) and self.in_closure(pc):
                        rebound = self.rebind(target, pc)
                        if rebound is None:
                            self.report['closure_branches_unmapped'] += 1
                            continue
                        delta = (rebound - pc) >> 2
                        if abs(delta) >= 1 << 25 or (rebound - pc) & 3:
                            raise ValueError(f'Branch out of range at {pc:#x}')
                        struct.pack_into('<I', self.data, segment['fileoff'] + offset,
                                         (word & 0xfc000000) | (delta & 0x3ffffff))
                        self.report['branches_rebound'] += 1
                    if word >> 31:
                        for register in list(pages):
                            pages.pop(register)
                    continue
                if word & ADRP_MASK == ADRP_VALUE:
                    immediate = signed(((word >> 5) & 0x7ffff) << 2 | ((word >> 29) & 3), 21)
                    pages[word & 31] = (((pc & ~4095) + (immediate << 12)) & ((1 << 64) - 1), offset)
                elif word & ADR_MASK == ADR_VALUE:
                    immediate = signed(((word >> 5) & 0x7ffff) << 2 | ((word >> 29) & 3), 21)
                    address = (pc + immediate) & ((1 << 64) - 1)
                    if self.mapped(address) and self.in_closure(pc):
                        rebound = self.rebind(address, pc)
                        if rebound is None:
                            continue
                        new_immediate = rebound - pc
                        if abs(new_immediate) >= 1 << 20:
                            raise ValueError(f'Adr out of range at {pc:#x}')
                        word = (word & 0x9f00001f) | (((new_immediate >> 2) & 0x7ffff) << 5) \
                            | ((new_immediate & 3) << 29)
                        struct.pack_into('<I', self.data, segment['fileoff'] + offset, word)
                        self.report['adr_rebound'] += 1
                else:
                    base = (word >> 5) & 31
                    if base not in pages:
                        continue
                    page_value, adrp_offset = pages[base]
                    if word & 0xff800000 == 0x91000000:
                        shift = 12 if word & 0x400000 else 0
                        address = (page_value + (((word >> 10) & 0xfff) << shift)) & ((1 << 64) - 1)
                        if self.mapped(address) and self.in_closure(pc):
                            rebound = self.rebind(address, pc)
                            if rebound is None:
                                pages.pop(base, None)
                                continue
                            if shift or rebound & 0xfff != address & 0xfff:
                                raise ValueError(f'Adrp+add cannot express {rebound:#x} at {pc:#x}')
                            self.patch_adrp_pair(segment, adrp_offset, offset, rebound, 0)
                    elif word & 0x3b000000 == 0x39000000:
                        size = word >> 30
                        if word & 0x04800000 == 0x04800000:
                            size = 4
                        address = (page_value + (((word >> 10) & 0xfff) << size)) & ((1 << 64) - 1)
                        if self.mapped(address) and self.in_closure(pc):
                            rebound = self.rebind(address, pc)
                            if rebound is None:
                                pages.pop(base, None)
                                continue
                            if (rebound ^ address) & (size and (1 << size) - 1 or 0xfff):
                                raise ValueError(f'Adrp+ldst cannot express {rebound:#x} at {pc:#x}')
                            self.patch_adrp_pair(segment, adrp_offset, offset, rebound, size)
                    else:
                        continue
                    pages.pop(base, None)

    def patch_adrp_pair(self, segment, adrp_offset, use_offset, rebound, size):
        word, = struct.unpack_from('<I', self.data, segment['fileoff'] + adrp_offset)
        pc = segment['va'] + adrp_offset
        immediate = ((rebound & ~4095) - (pc & ~4095)) >> 12
        if abs(immediate) >= 1 << 20:
            raise ValueError(f'Adrp out of range at {pc:#x}')
        struct.pack_into('<I', self.data, segment['fileoff'] + adrp_offset,
                         (word & 0x9f00001f) | (((immediate >> 2) & 0x7ffff) << 5) | ((immediate & 3) << 29))
        use, = struct.unpack_from('<I', self.data, segment['fileoff'] + use_offset)
        low = (rebound & 0xfff) >> size
        struct.pack_into('<I', self.data, segment['fileoff'] + use_offset, (use & 0xffc003ff) | (low << 10))
        self.report['adrp_pairs_rebound'] += 1

    def patch_entry_header(self):
        """Rebase the copied native kernel header: slide vmaddrs, shift file offsets."""
        at = self.fileoff
        rebuilt = []
        for command, offset, length in self.native.commands:
            blob = bytearray(self.native.data[offset:offset + length])
            if command == LC_DYLD_CHAINED_FIXUPS:
                continue  # the collection's root chains govern the entry
            if command == LC_SEGMENT_64:
                vmaddr, vmsize, fileoff, filesize = struct.unpack_from('<QQQQ', blob, 24)
                struct.pack_into('<QQ', blob, 24, (vmaddr + self.slide) & ((1 << 64) - 1), vmsize)
                if filesize:
                    struct.pack_into('<Q', blob, 40, fileoff + at)
                nsects, = struct.unpack_from('<I', blob, 64)
                for index in range(nsects):
                    s = 72 + 80 * index
                    address, = struct.unpack_from('<Q', blob, s + 32)
                    struct.pack_into('<Q', blob, s + 32, (address + self.slide) & ((1 << 64) - 1))
                    for field in (48, 56):  # section data and relocation offsets
                        value, = struct.unpack_from('<I', blob, s + field)
                        if value:
                            struct.pack_into('<I', blob, s + field, value + at)
            elif command == LC_SYMTAB:
                symoff, nsyms, stroff, strsize = struct.unpack_from('<4I', blob, 8)
                struct.pack_into('<4I', blob, 8, symoff + at, nsyms, stroff + at, strsize)
                for index in range(nsyms):
                    symbol = at + symoff + 16 * index
                    kind = self.data[symbol + 4]
                    if not kind & 0xe0 and kind & 0x0e == 0x0e:
                        value, = struct.unpack_from('<Q', self.data, symbol + 8)
                        struct.pack_into('<Q', self.data, symbol + 8,
                                         (value + self.slide) & ((1 << 64) - 1))
            elif command in (0x1d, 0x1e, 0x26, 0x29, 0x2b, 0x2e, 0x80000033):
                value, = struct.unpack_from('<I', blob, 8)
                if value:
                    struct.pack_into('<I', blob, 8, value + at)
            elif command == 0xb:
                for field in (32, 40, 48, 56, 64, 72):
                    value, = struct.unpack_from('<I', blob, field)
                    if value:
                        struct.pack_into('<I', blob, field, value + at)
            elif command in (0x22, 0x80000022):
                for field in (8, 16, 24, 32, 40):
                    value, = struct.unpack_from('<I', blob, field)
                    if value:
                        struct.pack_into('<I', blob, field, value + at)
            elif command == LC_UNIXTHREAD:
                pc, = struct.unpack_from('<Q', blob, 16 + 256)
                struct.pack_into('<Q', blob, 16 + 256, (pc + self.slide) & ((1 << 64) - 1))
            rebuilt.append(bytes(blob))
        payload = b''.join(rebuilt)
        first = 32 + struct.unpack_from('<I', self.native.data, 20)[0]
        if len(payload) + 32 > first:
            raise ValueError('Rebuilt entry header overruns its payload')
        self.data[at + 32:at + first] = payload + bytes(first - 32 - len(payload))
        struct.pack_into('<II', self.data, at + 16, len(rebuilt), len(payload))
        flags, = struct.unpack_from('<I', self.data, at + 24)
        struct.pack_into('<I', self.data, at + 24, flags | MH_DYLIB_IN_CACHE)

    def patch_headers(self):
        """Repoint the kernel entry, add native segments, grow linkedit, move the entry PC."""
        at = None
        for command, offset, length in self.cache.commands:
            if command == LC_FILESET_ENTRY:
                _, _, name_at = struct.unpack_from('<QQI', self.data, offset + 8)
                end = self.data.index(b'\0', offset + name_at)
                if self.data[offset + name_at:end].decode() == 'com.apple.kernel':
                    at = offset
        if at is None:
            raise ValueError('No com.apple.kernel fileset entry')
        struct.pack_into('<QQ', self.data, at + 8, (self.native.base + self.slide) & ((1 << 64) - 1),
                         self.fileoff)
        extra = []
        for segment in self.native.segments:
            if not segment['size']:
                continue
            command = next(o for c, o, l in self.native.commands if c == LC_SEGMENT_64
                           and self.native.data[o + 8:o + 24].rstrip(b'\0').decode() == segment['name'])
            maxprot, initprot = struct.unpack_from('<II', self.native.data, command + 56)
            extra.append(struct.pack('<II16sQQQQIIII', LC_SEGMENT_64, 72,
                                     ('__N' + segment['name'][2:]).encode()[:16].ljust(16, b'\0'),
                                     (segment['va'] + self.slide) & ((1 << 64) - 1), segment['size'],
                                     self.fileoff + segment['fileoff'] if segment['filesize'] else 0,
                                     segment['filesize'], maxprot, initprot, 0, 0))
        assert all(len(blob) == 72 for blob in extra)
        grown_bytes = b''.join(extra)
        end_of_commands = max(offset + length for command, offset, length in self.cache.commands)
        limit = next(s['filesize'] for s in self.cache.segments if s['name'] == '__TEXT')
        # Entry names live inside their own commands; the rest of the header page is zero padding.
        if end_of_commands + len(grown_bytes) > limit:
            raise ValueError('No room in the root header for the native segments')
        new_size = len(self.data) - self.fixups_at
        for command, offset, length in self.cache.commands:
            if command == LC_DYLD_CHAINED_FIXUPS:
                struct.pack_into('<II', self.data, offset + 8, self.fixups_at, new_size)
        linkedit = next(s for s in self.cache.segments if s['name'] == '__LINKEDIT')
        grown = len(self.data) - linkedit['fileoff']
        linkedit_end = linkedit['va'] + ((grown + PAGE - 1) & ~(PAGE - 1))
        if linkedit_end > (self.native.base + self.slide) & ((1 << 64) - 1):
            raise ValueError('Expanded root __LINKEDIT overlaps the native kernel')
        struct.pack_into('<QQQ', self.data, linkedit['command_offset'] + 32,
                         (grown + PAGE - 1) & ~(PAGE - 1), linkedit['fileoff'], grown)
        native_pc = None
        for command, offset, length in self.native.commands:
            if command == LC_UNIXTHREAD:
                native_pc = struct.unpack_from('<Q', self.native.data, offset + 16 + 256)[0]
        for command, offset, length in self.cache.commands:
            if command == LC_UNIXTHREAD and native_pc:
                struct.pack_into('<Q', self.data, offset + 16 + 256,
                                 (native_pc + self.slide) & ((1 << 64) - 1))
        rebuilt = []
        for command, offset, length in self.cache.commands:
            if command == LC_FILESET_ENTRY and self.closure is not None:
                name_at, = struct.unpack_from('<I', self.data, offset + 24)
                end = self.data.index(b'\0', offset + name_at)
                name = self.data[offset + name_at:end].decode()
                if name != 'com.apple.kernel' and name not in self.closure:
                    self.report['unselected_entries_dropped'] += 1
                    continue
            rebuilt.append(bytes(self.data[offset:offset + length]))
        rebuilt.extend(extra)
        commands = b''.join(rebuilt)
        self.data[32:end_of_commands + len(grown_bytes)] = commands + bytes(
            end_of_commands + len(grown_bytes) - 32 - len(commands))
        struct.pack_into('<II', self.data, 16, len(rebuilt), len(commands))
        self.report['native_base'] = f'{(self.native.base + self.slide) & ((1 << 64) - 1):#x}'
        self.report['native_fileoff'] = f'{self.fileoff:#x}'


def reviewed_sites(inputs, native, collection_slide):
    """Reviewed per-site bindings: one original pointer site (a component's GOT slot) bound to a named native object.
    Used when a kernel object means different things to different consumers, so pairing its target is not enough."""
    names = {}
    for address, symbols in native.symbols().items():
        for name in symbols:
            names[name] = address
    sites = {}
    for path in sorted((Path(__file__).parent / 'fixtures').glob('fileset-*-symbols.json')):
        record = json.loads(path.read_text())
        pinned = record['inputs']
        if (pinned['kernelcache_sha256'], pinned['entry']) != (inputs['kernelcache_sha256'], inputs['entry']):
            continue
        for entry in record.get('sites', []):
            if not entry.get('evidence'):
                raise ValueError('Reviewed site requires recorded evidence')
            if entry['native_symbol'] not in names:
                raise ValueError(f'Reviewed site names a missing native symbol: {entry["native_symbol"]}')
            sites[(int(entry['site'], 16) + collection_slide) & MASK64] = names[entry['native_symbol']]
    return sites


def validate_map(raw, native_data, payload):
    expected = {'kernelcache_sha256': hashlib.sha256(raw).hexdigest(),
                'native_sha256': hashlib.sha256(native_data).hexdigest(),
                'entry': 'com.apple.kernel'}
    if payload.get('inputs') != expected:
        raise ValueError('Pairing inputs do not match these images; rerun fileset_match.py')
    return expected


def emit_images(raw, native_data, payload, roots, stub_symbol=None, early_security_init=None):
    inputs = validate_map(raw, native_data, payload)
    from fileset_match import MATCHER_VERSION, matcher_signature
    if payload.get('matcher_version') != MATCHER_VERSION or payload.get('matcher_signature') != matcher_signature():
        raise ValueError('Pairing predates the current matching checks; rerun fileset_match.py')
    cache, native = Image(raw), Image(native_data)
    if cache.kind != 12 or native.kind != 2:
        raise ValueError('Expected an MH_FILESET collection and an MH_EXECUTE native kernel')
    pairs = {int(key, 16): int(value['native'], 16) for key, value in payload['functions'].items()}
    pairs.update({int(key, 16): int(value['native'], 16) for key, value in payload['data'].items()})
    closure = dependency_closure(cache, roots)
    for identifier, address in (early_security_init or {}).items():
        entry = Image(cache.data, cache.entries[identifier][1])
        if not any(s['name'] == '__TEXT_EXEC' and s['va'] <= address < s['va'] + s['size'] for s in entry.segments):
            raise ValueError(f'Early security initializer {address:#x} is outside {identifier} code')
    cache, collection_slide, dropped_personalities = rebase_collection(
        cache, native.base, closure, early_security_init, set(roots))
    pairs = {(address + collection_slide) & MASK64: target for address, target in pairs.items()}
    from fileset_segmented import SegmentedEmit
    emit = SegmentedEmit(cache, native, pairs, closure)
    emit.site_pairs = reviewed_sites(inputs, native, collection_slide)
    emit.stub, emit.stubbed = None, defaultdict(Counter)
    emit.old_starts = set(emit.old.function_starts())
    if stub_symbol:
        found = [address for address, names in native.symbols().items() if stub_symbol in names]
        if len(found) != 1:
            raise ValueError(f'Native kernel has no unique {stub_symbol}')
        emit.stub = found[0]
    emit.place_kernel()
    emit.rewrite_native_chains()
    emit.rebuild_linkedit()
    emit.rewrite_branches()
    emit.seal_components()
    emit.patch_entry_header()
    emit.patch_headers()
    image = bytes(emit.data)
    return image, {
        'format': 'MH_FILESET', 'inputs': inputs,
        'matcher_version': payload['matcher_version'], 'matcher_signature': payload['matcher_signature'],
        'reference_sha256': payload.get('reference_sha256'),
        'closure': sorted(closure) if closure is not None else None,
        'collection_base': hex(cache.base), 'collection_slide': hex(collection_slide),
        'native_link_address': hex(native.base), 'statistics': dict(emit.report),
        'missing': {owner: {hex(target): count for target, count in sorted(targets.items())}
                    for owner, targets in sorted(emit.missing.items())},
        'stubbed': {owner: {hex(target): count for target, count in sorted(targets.items())}
                    for owner, targets in sorted(emit.stubbed.items())},
        'stub_symbol': stub_symbol,
        'personalities_dropped': dropped_personalities,
        'early_security_init': {k: hex(v) for k, v in (early_security_init or {}).items()},
        'missing_address_space': 'output collection',
        'mapping_complete': not bool(emit.missing), 'runtime_qualified': False, 'loadable': False,
        'candidate_bytes': len(image), 'candidate_sha256': hashlib.sha256(image).hexdigest(),
    }


def atomic_write(path, contents):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.' + path.name + '.', delete=False) as f:
            name = f.name
            f.write(contents)
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def write_candidate(image, report, output, report_path, allow_missing=False):
    report['output'] = str(Path(output).resolve())
    report['candidate_written'] = False
    if report['missing'] and not allow_missing:
        atomic_write(report_path, (json.dumps(report, indent=2) + '\n').encode())
        total = sum(sum(targets.values()) for targets in report['missing'].values())
        raise ValueError(f'Incomplete pairing: {total} unresolved references; see {report_path}')
    atomic_write(output, image)
    report['candidate_written'] = True
    report['allow_missing'] = allow_missing
    atomic_write(report_path, (json.dumps(report, indent=2) + '\n').encode())
    print(f'wrote {output} ({len(image)} bytes)')
    total = sum(sum(targets.values()) for targets in report['missing'].values())
    stubbed = sum(len(targets) for targets in report.get('stubbed', {}).values())
    print(f'unresolved closure references: {total}; stubbed functions: {stubbed}; runtime qualification: pending')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kernelcache')
    parser.add_argument('native')
    parser.add_argument('--map', required=True, help='fileset_match JSON pairing for these exact inputs')
    parser.add_argument('--output', required=True)
    parser.add_argument('--report', help='defaults to OUTPUT with a .json suffix')
    parser.add_argument('--root', action='append', default=[], help='root bundle id and its dependencies')
    parser.add_argument('--allow-missing', action='store_true', help='emit an incomplete experimental image')
    parser.add_argument('--unmapped-stub', metavar='SYMBOL',
                        help='native function that receives calls to unmapped original kernel functions')
    parser.add_argument('--early-security-init', action='append', default=[], metavar='BUNDLE=ORIGINAL_VA',
                        help='original registration routine OSKext runs after this component starts')
    args = parser.parse_args()
    early = {}
    for item in args.early_security_init:
        identifier, _, address = item.partition('=')
        early[identifier] = int(address, 0)
    payload = json.loads(Path(args.map).read_text())
    image, report = emit_images(Path(args.kernelcache).read_bytes(), Path(args.native).read_bytes(),
                                payload, args.root, args.unmapped_stub, early)
    report.update(kernelcache=str(Path(args.kernelcache).resolve()), native=str(Path(args.native).resolve()),
                  map=str(Path(args.map).resolve()))
    write_candidate(image, report, args.output, args.report or Path(args.output).with_suffix('.json'),
                    args.allow_missing)


if __name__ == '__main__':
    main()
