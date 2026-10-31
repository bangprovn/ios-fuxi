"""Emit a coalesced kernel collection using per-segment native relocation."""
from collections import defaultdict
import hashlib
import hmac
import struct

from fileset_emit import (Emit, ChainReader, decode, chain_word, PAGE, MASK64,
                          LC_SEGMENT_64, LC_SYMTAB, LC_DYLD_CHAINED_FIXUPS,
                          LC_FILESET_ENTRY, LC_UNIXTHREAD, MH_DYLIB_IN_CACHE)
from fileset_layout import Layout, relocate_native, encode_uleb, align
from kc_image import Image, scan, signed
from fileset_emit import inline_data_ranges, inside


class SegmentedEmit(Emit):
    site_pairs = {}

    def rebind(self, target, site):
        if not self.mapped(target):
            return target
        # A reviewed site binding wins over the target's pairing (one consumer needs a different native object).
        native = self.site_pairs.get(site) if self.site_pairs else None
        if native is None:
            native = self.pairs.get(target)
        if native is None and self.stub is not None and target in self.old_starts:
            # A kernel function this kernel does not have: the stub reports the call site.
            self.stubbed[self.owner(site)][target] += 1
            native = self.stub
        if native is None:
            self.missing[self.owner(site)][target] += 1
            return None
        return self.layout.translate(native)

    def place_kernel(self):
        self.layout = Layout(self.cache, self.native, self.closure)
        self.report['segment_layout'] = self.layout.describe()
        self.fileoff = self.layout.placements['__TEXT'] - self.cache.base
        self.data.extend(bytes(self.layout.size - len(self.data)))
        for segment in self.native.segments:
            if not segment['size']:
                continue
            at = self.layout.placements[segment['name']] - self.cache.base
            self.data[at:at + segment['size']] = bytes(segment['size'])
            source, size = segment['fileoff'], segment['filesize']
            self.data[at:at + size] = self.native.data[source:source + size]
        old_info = next(s for s in self.cache.segments if s['name'] == '__PRELINK_INFO')
        new_info = self.layout.roots['__PRELINK_INFO']
        self.data[new_info['fileoff']:new_info['fileoff'] + new_info['filesize']] = \
            self.cache.data[old_info['fileoff']:old_info['fileoff'] + old_info['filesize']]
        self.table_cursor = self.layout.table_begin

    def append_table(self, blob):
        at = align(self.table_cursor, 16)
        if at + len(blob) > self.layout.table_end:
            raise ValueError('Derived linkedit reservation is insufficient')
        self.data[at:at + len(blob)] = blob
        self.table_cursor = at + len(blob)
        return at

    def rewrite_native_chains(self):
        self.native_pointers, counts = relocate_native(self.data, self.layout)
        self.report['native_split_relocations'] = dict(counts)
        self.report['native_chain_sites'] = len(self.native_pointers)

    def rebuild_linkedit(self):
        # Read original chains before any native bytes were placed in vacated
        # pages: a chain can span old-kernel, removed-kext and retained-kext sites.
        reader = ChainReader(self.cache.data, self.cache)
        pointers = dict(self.native_pointers)
        for segment in self.cache.segments:
            for page, sites in reader.pages(segment['name']).items():
                for offset, value in sites:
                    site = segment['va'] + page * PAGE + offset
                    if self.mapped(site):
                        self.report['old_kernel_sites_dropped'] += 1
                        continue
                    if not self.in_closure(site):
                        self.report['unselected_chain_sites_dropped'] += 1
                        continue
                    target_offset, auth, key, diversity, addrdiv, _ = decode(value)
                    target = self.cache.base + target_offset
                    if self.mapped(target):
                        rebound = self.rebind(target, site)
                        if rebound is None:
                            self.report['closure_pointers_unmapped'] += 1
                        else:
                            target = rebound
                            self.report['kext_pointers_rebound'] += 1
                    if site in pointers:
                        raise ValueError('Native and kext fixup sites overlap')
                    pointers[site] = (target, auth, key, diversity, addrdiv)
                    self.report['kext_chain_sites_kept'] += 1
        order = list(self.layout.roots.values())
        blob = bytearray(4 + 4 * len(order))
        struct.pack_into('<I', blob, 0, len(order))
        grouped = defaultdict(lambda: defaultdict(list))
        for site, pointer in sorted(pointers.items()):
            matches = [i for i, s in enumerate(order) if s['va'] <= site and site + 8 <= s['va'] + s['filesize']]
            if len(matches) != 1 or site % 4:
                raise ValueError(f'Fixup is not within one root mapping: {site:#x}')
            index = matches[0]
            relative = site - order[index]['va']
            grouped[index][relative // PAGE].append((relative % PAGE, pointer))
        for index, pages in grouped.items():
            count = max(pages) + 1
            if count > 65535:
                raise ValueError('Too many chained-fixup pages')
            piece = bytearray(struct.pack('<IHHQIH', 22 + count*2, PAGE, 8,
                                         order[index]['va'] - self.cache.base, 0, count))
            piece.extend(b'\xff\xff' * count)
            for page, sites in pages.items():
                struct.pack_into('<H', piece, 22 + page*2, sites[0][0])
                for n, (offset, pointer) in enumerate(sites):
                    target, auth, key, diversity, addrdiv = pointer
                    step = (sites[n+1][0]-offset)//4 if n+1 < len(sites) else 0
                    word = chain_word(auth, key, diversity, addrdiv, target-self.cache.base, step)
                    struct.pack_into('<Q', self.data, order[index]['fileoff']+page*PAGE+offset, word)
            struct.pack_into('<I', blob, 4 + index*4, len(blob))
            blob.extend(piece)
        blob = struct.pack('<7I', 0, 28, 28, 28, 0, 1, 0) + blob
        self.fixups_at, self.fixups_size = self.append_table(blob), len(blob)
        previous, starts = self.layout.placements['__TEXT'], bytearray()
        for address in sorted(self.layout.translate(a) for a in self.native.function_starts()):
            starts.extend(encode_uleb(address - previous))
            previous = address
        starts.append(0)
        self.starts_at, self.starts_size = self.append_table(starts), len(starts)

    def rewrite_branches(self):
        """Rewrite selected executable entries using explicit ADRP provenance."""
        for name, (_, entry_at) in self.cache.entries.items():
            if name == 'com.apple.kernel' or self.closure is not None and name not in self.closure:
                continue
            for segment in Image(self.cache.data, entry_at).segments:
                if segment['name'] != '__TEXT_EXEC' or not segment['filesize']:
                    continue
                blob = self.cache.data[segment['fileoff']:segment['fileoff']+segment['filesize']]
                pools = inline_data_ranges(blob)
                self.report['inline_data_pools_preserved'] += len(pools)
                self.report['inline_data_bytes_preserved'] += sum(e-b for b,e in pools)
                begin = 0
                calls, refs = [], []
                for end, next_begin in pools + [(len(blob), len(blob))]:
                    chunk_calls, chunk_refs, _ = scan(blob[begin:end], segment['va']+begin, with_origins=True)
                    calls.extend(chunk_calls)
                    refs.extend(chunk_refs)
                    begin = next_begin
                for pc, target, _ in calls:
                    if not self.mapped(target):
                        continue
                    rebound = self.rebind(target, pc)
                    if rebound is None:
                        self.report['closure_branches_unmapped'] += 1
                        continue
                    displacement = rebound-pc
                    if displacement % 4 or not -(1 << 27) <= displacement < 1 << 27:
                        raise ValueError(f'Kext branch out of range at {pc:#x}')
                    at = segment['fileoff']+pc-segment['va']
                    word, = struct.unpack_from('<I', self.data, at)
                    struct.pack_into('<I', self.data, at, (word & 0xfc000000) | ((displacement>>2) & 0x3ffffff))
                    self.report['branches_rebound'] += 1
                groups = defaultdict(list)
                producers = set()
                for pc, target, kind, producer, scale in refs:
                    if producer is not None:
                        producers.add(producer)
                        groups[producer].append((pc,target,kind,scale))
                        continue
                    if not self.mapped(target):
                        continue
                    rebound = self.rebind(target,pc)
                    if rebound is None:
                        self.report['closure_adr_unmapped'] += 1
                        continue
                    immediate = rebound-pc
                    if not -(1 << 20) <= immediate < 1 << 20:
                        raise ValueError(f'Kext ADR out of range at {pc:#x}')
                    at = segment['fileoff']+pc-segment['va']
                    word, = struct.unpack_from('<I', self.data, at)
                    struct.pack_into('<I', self.data, at, (word & 0x9f00001f) |
                                     (((immediate>>2)&0x7ffff)<<5) | ((immediate&3)<<29))
                    self.report['adr_rebound'] += 1
                for producer, uses in groups.items():
                    rewritten = []
                    for pc,target,kind,scale in uses:
                        rebound = self.rebind(target,pc)
                        if rebound is None:
                            self.report['closure_adrp_unmapped'] += 1
                            continue
                        if self.mapped(target):
                            if kind == 'add' and scale:
                                raise ValueError('Shifted ADD cannot be rewritten as a low page offset')
                            if rebound % (1 << scale):
                                raise ValueError('Relocated load/store target is not aligned')
                        rewritten.append((pc,rebound,kind,scale,target))
                    if len(rewritten) != len(uses) or not any(self.mapped(t) for _,_,_,_,t in rewritten):
                        continue
                    pages = {target & ~4095 for _,target,_,_,_ in rewritten}
                    if len(pages) != 1:
                        raise ValueError(f'ADRP consumers require different pages at {producer:#x}')
                    for pc,target,kind,scale,old_target in rewritten:
                        self.patch_adrp_pair(segment,producer-segment['va'],pc-segment['va'],target,scale)
                # An ADRP into the old kernel without a proven consumer cannot
                # be silently left pointing at discarded code or data.
                for offset in range(0,len(blob)-3,4):
                    if inside(pools,offset):
                        continue
                    word, = struct.unpack_from('<I',blob,offset)
                    pc = segment['va']+offset
                    if word & 0x9f000000 != 0x90000000 or pc in producers:
                        continue
                    immediate = signed(((word>>5)&0x7ffff)<<2 | ((word>>29)&3),21)
                    page = (pc & ~4095)+(immediate<<12)
                    if self.mapped(page):
                        self.missing[name][page] += 1
                        self.report['unproven_kernel_page_references'] += 1

    def patch_entry_header(self):
        rebuilt = []
        section_index = 0
        for command, offset, length in self.native.commands:
            blob = bytearray(self.native.data[offset:offset+length])
            if command in (LC_DYLD_CHAINED_FIXUPS, 0x1d):
                continue
            if command == LC_SEGMENT_64:
                name = blob[8:24].rstrip(b'\0').decode()
                segment = self.layout.segment(name)
                address = self.layout.placements[name]
                struct.pack_into('<Q', blob, 24, address)
                struct.pack_into('<Q', blob, 40, address-self.cache.base if segment['filesize'] else 0)
                if name == '__LINKEDIT':
                    size = self.layout.table_end - (address-self.cache.base)
                    struct.pack_into('<Q', blob, 32, size)
                    struct.pack_into('<Q', blob, 48, size)
                count, = struct.unpack_from('<I', blob, 64)
                for index in range(count):
                    section_index += 1
                    s = 72 + index*80
                    struct.pack_into('<Q', blob, s+32, self.layout.section_address(section_index, 0, True))
                    for field in (48, 56):
                        value, = struct.unpack_from('<I', blob, s+field)
                        if value:
                            struct.pack_into('<I', blob, s+field, self.layout.file(value))
            elif command == LC_SYMTAB:
                symoff, nsyms, stroff, strsize = struct.unpack_from('<4I', blob, 8)
                at = self.layout.file(symoff, nsyms*16)
                struct.pack_into('<4I', blob, 8, at, nsyms, self.layout.file(stroff, strsize), strsize)
                for index in range(nsyms):
                    symbol = at + index*16
                    kind, ordinal = self.data[symbol+4:symbol+6]
                    if not kind & 0xe0 and kind & 0x0e == 0x0e:
                        value, = struct.unpack_from('<Q', self.data, symbol+8)
                        if not 1 <= ordinal <= len(self.native.sections):
                            raise ValueError('N_SECT symbol has invalid section ordinal')
                        section = self.native.sections[ordinal-1]
                        struct.pack_into('<Q', self.data, symbol+8,
                                         self.layout.section_address(ordinal, value-section['va'], True))
            elif command == 0x26:
                struct.pack_into('<II', blob, 8, self.starts_at, self.starts_size)
            elif command in (0x1e, 0x29, 0x2b, 0x2e, 0x80000033):
                value, size = struct.unpack_from('<II', blob, 8)
                if size:
                    struct.pack_into('<I', blob, 8, self.layout.file(value, size))
            elif command == 0xb:
                for field in (32, 40, 48, 56, 64, 72):
                    value, = struct.unpack_from('<I', blob, field)
                    if value:
                        struct.pack_into('<I', blob, field, self.layout.file(value))
            elif command in (0x22, 0x80000022):
                for field in (8, 16, 24, 32, 40):
                    value, size = struct.unpack_from('<II', blob, field)
                    if size:
                        struct.pack_into('<I', blob, field, self.layout.file(value, size))
            elif command == LC_UNIXTHREAD:
                pc, = struct.unpack_from('<Q', blob, 272)
                struct.pack_into('<Q', blob, 272, self.layout.translate(pc))
            rebuilt.append(blob)
        commands = b''.join(rebuilt)
        oldsize, = struct.unpack_from('<I', self.native.data, 20)
        if len(commands) > oldsize:
            raise ValueError('Native load commands exceed available header space')
        self.data[self.fileoff+32:self.fileoff+32+oldsize] = commands + bytes(oldsize-len(commands))
        struct.pack_into('<II', self.data, self.fileoff+16, len(rebuilt), len(commands))
        flags, = struct.unpack_from('<I', self.data, self.fileoff+24)
        struct.pack_into('<I', self.data, self.fileoff+24, flags | MH_DYLIB_IN_CACHE)
        # corecrypto authenticates the relocated __text bytes in a fileset.
        code = next(s for s in self.native.sections if s['segment'] == '__TEXT_EXEC' and s['name'] == '__text')
        seals = [s for s in self.native.sections if s['name'] == '__fips_hmacs']
        if not seals:  # Fuxi: no in-kernel corecrypto (com.apple.kec.corecrypto is prelinked), nothing to seal
            self.report['corecrypto_hmac_sha256'] = None
            return
        if len(seals) != 1 or seals[0]['size'] != 32:
            raise ValueError('Expected one 32-byte corecrypto HMAC field')
        code_at = self.layout.translate(code['va']) - self.cache.base
        digest = hmac.new(b'\0', self.data[code_at:code_at+code['size']], hashlib.sha256).digest()
        seal_at = self.layout.translate(seals[0]['va']) - self.cache.base
        self.data[seal_at:seal_at+32] = digest
        self.report['corecrypto_hmac_sha256'] = digest.hex()

    def seal_components(self):
        """Reseal original components whose FIPS HMAC covers __TEXT_EXEC,__text that branch rebinding changed.

        Only a field that matched the original bytes is rewritten; the new value is the same HMAC over the
        emitted bytes, which fipspost_get_hmac reads through MH_DYLIB_IN_CACHE (section address + slide).
        """
        sealed = {}
        for name, (_, entry_at) in self.cache.entries.items():
            if name == 'com.apple.kernel' or self.closure is not None and name not in self.closure:
                continue
            sections = Image(self.cache.data, entry_at).sections
            seals = [s for s in sections if s['name'] == '__fips_hmacs']
            if not seals:
                continue
            code = [s for s in sections if s['segment'] == '__TEXT_EXEC' and s['name'] == '__text']
            if len(seals) != 1 or seals[0]['size'] != 32 or len(code) != 1:
                raise ValueError(f'{name}: unexpected FIPS HMAC layout')
            code_at, seal_at = (self.cache.offset(code[0]['va'], code[0]['size']),
                                self.cache.offset(seals[0]['va'], 32))
            original = hmac.new(b'\0', self.cache.data[code_at:code_at + code[0]['size']], hashlib.sha256).digest()
            if original != self.cache.data[seal_at:seal_at + 32]:
                raise ValueError(f'{name}: original FIPS HMAC does not cover its __text')
            digest = hmac.new(b'\0', self.data[code_at:code_at + code[0]['size']], hashlib.sha256).digest()
            self.data[seal_at:seal_at + 32] = digest
            sealed[name] = digest.hex()
        self.report['component_hmacs'] = sealed

    def patch_headers(self):
        rebuilt = []
        native_pc = struct.unpack_from('<Q', self.native.data, self.native.command(LC_UNIXTHREAD)[0]+272)[0]
        for command, offset, length in self.cache.commands:
            blob = bytearray(self.cache.data[offset:offset+length])
            if command == LC_SEGMENT_64:
                name = blob[8:24].rstrip(b'\0').decode()
                if name not in self.layout.roots:
                    continue
                segment = self.layout.roots[name]
                old_address, = struct.unpack_from('<Q', blob, 24)
                struct.pack_into('<4Q', blob, 24, segment['va'], segment['size'], segment['fileoff'], segment['filesize'])
                count, = struct.unpack_from('<I', blob, 64)
                for index in range(count):
                    s = 72 + index*80
                    address, = struct.unpack_from('<Q', blob, s+32)
                    fileoff, = struct.unpack_from('<I', blob, s+48)
                    delta = segment['va'] - old_address
                    struct.pack_into('<Q', blob, s+32, address+delta)
                    if fileoff:
                        struct.pack_into('<I', blob, s+48, fileoff+delta)
            elif command == LC_DYLD_CHAINED_FIXUPS:
                struct.pack_into('<II', blob, 8, self.fixups_at, self.fixups_size)
            elif command == LC_FILESET_ENTRY:
                name_at, = struct.unpack_from('<I', blob, 24)
                name = blob[name_at:].split(b'\0',1)[0].decode()
                if name == 'com.apple.kernel':
                    struct.pack_into('<QQ', blob, 8, self.layout.placements['__TEXT'], self.fileoff)
                elif self.closure is not None and name not in self.closure:
                    self.report['unselected_entries_dropped'] += 1
                    continue
            elif command == LC_UNIXTHREAD:
                struct.pack_into('<Q', blob, 272, self.layout.translate(native_pc))
            elif command in (0x1d,):
                continue
            rebuilt.append(blob)
        original_names = {s['name'] for s in self.cache.segments}
        for name, segment in self.layout.roots.items():
            if name in original_names:
                continue
            source = self.layout.segment(name)
            maxprot, initprot = struct.unpack_from('<II', self.native.data, source['command_offset']+56)
            rebuilt.append(struct.pack('<II16sQQQQIIII', LC_SEGMENT_64, 72, name.encode().ljust(16,b'\0'),
                                       segment['va'], segment['size'], segment['fileoff'], segment['filesize'],
                                       maxprot, initprot, 0, 0))
        payload = b''.join(rebuilt)
        limit = self.layout.roots['__TEXT']['filesize']
        if len(payload)+32 > limit:
            raise ValueError('Root commands exceed header mapping')
        self.data[32:limit] = payload + bytes(limit-32-len(payload))
        struct.pack_into('<II', self.data, 16, len(rebuilt), len(payload))
        self.report['native_base'] = hex(self.layout.placements['__TEXT'])
        self.report['native_fileoff'] = hex(self.fileoff)
