#!/usr/bin/env python3
"""Pack an xnu kernel Mach-O + ADT into the payload the sweet shim boots.

  mkpayload.py KERNEL ADT OUT [--cmdline "..."] [--ramdisk IMAGE]

The kernel is linked as a PIE with LC_DYLD_CHAINED_FIXUPS. Nobody else
(no kmutil, no iBoot) resolves those for us, and xnu only rebases static
kernelcaches itself, so the chains are walked here and every pointer is
rewritten to its unslid target. The shim then only copies flat segments.

An arm64e kernel (CI kernel=metal4e, patch 0115) is linked with
__TEXT,__thread_starts instead: arm_init rebases and PAC-signs those chains on
the device, so the segments are copied untouched. Authenticated chained fixups
are refused, because resolving them here would leave unsigned pointers that the
arm64e kernel then fails to authenticate (--allow-unsigned-auth overrides, for
the stock VMAPPLE kernel only).

Payload layout (little endian, all offsets from payload start):
  0x000  char  magic[8] = "SWEETXNU"
  0x008  u32   version = 1, nsegs
  0x010  u64   virt_base      kernel VA that maps to phys_base
  0x018  u64   entry_va       LC_UNIXTHREAD pc
  0x020  u64   adt_off, adt_size
  0x030  u64   kernel_end_va  end of the highest segment (page aligned)
  0x038  u64   phys_base      kernel-managed RAM (same values as the ADT's dram-base/size)
  0x040  u64   mem_size
  0x048  char  cmdline[608]
  0x2a8  seg[nsegs] { u64 vmaddr, vmsize, file_off(in payload), file_size }

Version 2 (--boot-line-length 1024, the xnu-12377 kernel of docs-lanes/ios27.md,
whose boot_args.CommandLine grew from 608 to 1024 bytes) is the same header
with cmdline[1024] at 0x048 and the segment table at 0x448.

--ramdisk appends IMAGE as one more segment right after the kernel and moves
kernel_end past it, so the shim needs no changes: the image lands below the
ADT and boot_args, inside topOfKernelData (never handed to the VM), and the
ADT gets /chosen/memory-map "RAMDisk" = <phys, size> for IOFindBSDRoot to
turn into md0 (boot with rd=md0).

--prelinked FILESET (K2, docs-lanes/ios27.md#k2) packs the MH_FILESET that
tools/ios27-prelink.py emitted from KERNEL instead of KERNEL itself. KERNEL must
be the native kernel named in FILESET's report (FILESET with suffix .json), and
the report must name FILESET. The root segments are copied flat, untouched:
xnu's arm_init slides the collection itself (kernel_collection_slide over the
root LC_DYLD_CHAINED_FIXUPS, signing the arm64e pointers), so no fixup is
applied here and no __thread_starts is needed. An incomplete mapping (made with
--allow-missing) is refused unless --allow-incomplete is given.
"""
import argparse, hashlib, json, os, struct, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import adt  # noqa: E402

LC_SEGMENT_64, LC_UNIXTHREAD, LC_DYLD_CHAINED_FIXUPS = 0x19, 0x5, 0x80000034
LC_FILESET_ENTRY = 0x80000035
MH_EXECUTE, MH_FILESET = 2, 12
PAGE = 0x4000
BOOT_LINE_LENGTH = 608
CMDLINE = 0x48
SEG_TABLE = CMDLINE + BOOT_LINE_LENGTH	# 0x2a8
# payload version -> boot_args.CommandLine size of the kernel it carries
BOOT_LINES = {1: 608, 2: 1024}


def parse_macho(buf):
    magic, cputype, cpusub, ftype, ncmds, sizeofcmds, flags, _ = struct.unpack_from('<8I', buf, 0)
    assert magic == 0xfeedfacf, 'not a 64-bit Mach-O'
    segs, entry, fixups, thread_starts, entries = [], None, None, None, {}
    off = 32
    for _ in range(ncmds):
        cmd, size = struct.unpack_from('<II', buf, off)
        if cmd == LC_SEGMENT_64:
            name = buf[off + 8:off + 24].split(b'\0')[0].decode()
            vmaddr, vmsize, fileoff, filesize = struct.unpack_from('<4Q', buf, off + 24)
            segs.append(dict(name=name, vmaddr=vmaddr, vmsize=vmsize, fileoff=fileoff, filesize=filesize))
            nsects, = struct.unpack_from('<I', buf, off + 64)
            for s in range(nsects):
                sect = off + 72 + 80 * s
                if buf[sect:sect + 16].split(b'\0')[0] == b'__thread_starts':
                    thread_starts = struct.unpack_from('<2Q', buf, sect + 32)	# addr, size
        elif cmd == LC_UNIXTHREAD:
            flavor, count = struct.unpack_from('<II', buf, off + 8)
            # arm_thread_state64: x0..x28, fp, lr, sp, pc, cpsr
            entry, = struct.unpack_from('<Q', buf, off + 16 + 32 * 8)
        elif cmd == LC_DYLD_CHAINED_FIXUPS:
            fixups = struct.unpack_from('<II', buf, off + 8)	# dataoff, datasize
        elif cmd == LC_FILESET_ENTRY:
            vmaddr, fileoff, name_at = struct.unpack_from('<QQI', buf, off + 8)
            entries[buf[off + name_at:buf.index(b'\0', off + name_at)].decode()] = (vmaddr, fileoff)
        off += size
    return dict(cputype=cputype, cpusub=cpusub & 0xff, ftype=ftype, segs=segs, entry=entry, fixups=fixups,
                thread_starts=thread_starts, auth_fixups=0, entries=entries)


def check_fileset(buf, m):
    """An emitted K2 collection as arm_init expects it: flat root, kernel entry, pc in its code."""
    if m['ftype'] != MH_FILESET:
        raise SystemExit('--prelinked input is not an MH_FILESET')
    if 'com.apple.kernel' not in m['entries']:
        raise SystemExit('fileset has no com.apple.kernel entry')
    text = m['segs'][0]
    if text['name'] != '__TEXT' or text['fileoff'] != 0:
        raise SystemExit('fileset root does not start with __TEXT at file offset 0')
    for s in m['segs']:
        if s['filesize'] and (s['vmaddr'] - text['vmaddr'] != s['fileoff'] or s['fileoff'] + s['filesize'] > len(buf)):
            raise SystemExit(f"fileset root segment {s['name']} is not flat (va - base == file offset) inside the file")
    va, fileoff = m['entries']['com.apple.kernel']
    kernel = parse_macho(buf[fileoff:])
    code = [s for s in kernel['segs'] if s['name'] == '__TEXT_EXEC']
    if kernel['ftype'] != MH_EXECUTE or not code or m['entry'] is None:
        raise SystemExit('fileset kernel entry is not an MH_EXECUTE with __TEXT_EXEC and a root LC_UNIXTHREAD')
    if not code[0]['vmaddr'] <= m['entry'] < code[0]['vmaddr'] + code[0]['vmsize']:
        raise SystemExit(f"fileset entry {m['entry']:#x} is outside the kernel's __TEXT_EXEC")
    if not any(s['vmaddr'] <= m['entry'] < s['vmaddr'] + s['vmsize'] for s in m['segs']):
        raise SystemExit('fileset entry is outside the root segments')


def prelinked_report(kernel_path, fileset_path, allow_incomplete=False):
    """The tools/ios27-prelink.py report for FILESET, checked against KERNEL and FILESET."""
    report_path = os.path.splitext(fileset_path)[0] + '.json'
    try:
        report = json.load(open(report_path))
    except (OSError, ValueError) as e:
        raise SystemExit(f'{report_path}: no readable prelink report ({e})')
    sha = lambda p: hashlib.sha256(open(p, 'rb').read()).hexdigest()
    if report.get('format') != 'MH_FILESET' or report.get('inputs', {}).get('native_sha256') != sha(kernel_path):
        raise SystemExit(f'{report_path}: not made from {kernel_path}')
    if report.get('candidate_sha256') != sha(fileset_path):
        raise SystemExit(f'{report_path}: does not describe {fileset_path}')
    incomplete = not report.get('mapping_complete')
    if incomplete and not allow_incomplete:
        raise SystemExit(f'{fileset_path}: kext mapping incomplete (emitted with --allow-missing); '
                         'pass --allow-incomplete to pack it anyway')
    return dict(fileset=os.path.basename(fileset_path), mapping_complete=not incomplete,
                stubbed=sum(len(v) for v in report.get('stubbed', {}).values()),
                missing=sum(len(v) for v in report.get('missing', {}).values()),
                closure=len(report.get('closure') or ()), reference=report.get('reference_sha256'))


def apply_chained_fixups(buf, m):
    """Rewrite every chained rebase in buf (the whole file) to its plain target VA."""
    if not m['fixups']:
        return 0
    data_off, _ = m['fixups']
    _, starts_off, _, _, imports_count, _, _ = struct.unpack_from('<7I', buf, data_off)
    assert imports_count == 0, 'kernel has binds; only rebases are supported'
    image_base = m['segs'][0]['vmaddr']		# __TEXT, holds the mach header
    seg_count, = struct.unpack_from('<I', buf, data_off + starts_off)
    nfix = 0
    for i in range(seg_count):
        seg_info, = struct.unpack_from('<I', buf, data_off + starts_off + 4 + 4 * i)
        if not seg_info:
            continue
        base = data_off + starts_off + seg_info
        size, page_size, fmt, seg_offset, max_valid, page_count = struct.unpack_from('<IHHQIH', buf, base)
        starts = struct.unpack_from('<%dH' % page_count, buf, base + 22)
        seg = m['segs'][i]
        for p, start in enumerate(starts):
            if start == 0xffff:
                continue
            assert not start & 0x8000, 'DYLD_CHAINED_PTR_START_MULTI not supported'
            loc = seg['fileoff'] + p * page_size + start
            while True:
                raw, = struct.unpack_from('<Q', buf, loc)
                target, nxt, stride = decode(raw, fmt, image_base)
                m['auth_fixups'] += fmt in (1, 7, 9, 12) and raw >> 63
                struct.pack_into('<Q', buf, loc, target)
                nfix += 1
                if not nxt:
                    break
                loc += nxt * stride
    return nfix


def decode(raw, fmt, image_base):
    """Return (unslid target VA, next, stride) for one chained pointer."""
    if fmt in (2, 6):		# DYLD_CHAINED_PTR_64 / _64_OFFSET
        assert not raw >> 63, 'bind in kernel'
        target = raw & ((1 << 36) - 1)
        high8 = (raw >> 36) & 0xff
        nxt = (raw >> 51) & 0xfff
        if fmt == 6:
            target += image_base
        return target | (high8 << 56), nxt, 4
    if fmt == 8:		# DYLD_CHAINED_PTR_64_KERNEL_CACHE
        target = raw & ((1 << 30) - 1)
        nxt = (raw >> 51) & 0xfff
        return image_base + target, nxt, 4
    if fmt in (1, 7, 9, 12):	# arm64e variants (stock VMAPPLE kernel only; PAC is not re-signed)
        auth = raw >> 63
        nxt = (raw >> 51) & 0x7ff
        stride = 4 if fmt in (7, 12) else 8
        if auth:
            target = raw & 0xffffffff
        else:
            target = raw & ((1 << 43) - 1)
            target |= ((raw >> 43) & 0xff) << 56
        if fmt in (7, 9, 12) or auth:
            target += image_base
        return target, nxt, stride
    raise ValueError(f'unsupported chained pointer format {fmt}')


def add_ramdisk_to_adt(adt_blob, phys, size):
    """Return the ADT with /chosen/memory-map RAMDisk = <phys, size> (two uintptr_t)."""
    root, _ = adt.parse(adt_blob)
    chosen = next(c for c in root.children if c.name == 'chosen')
    mmap = next(c for c in chosen.children if c.name == 'memory-map')
    assert mmap.prop('RAMDisk') is None, 'ADT already has a RAMDisk'
    mmap.props.append(('RAMDisk', 0, struct.pack('<2Q', phys, size)))
    return adt.serialize(root)


def adt_dram(adt_blob):
    """Kernel-managed RAM <base, size> from the ADT's /chosen dram-base/dram-size."""
    root, _ = adt.parse(adt_blob)
    chosen = next(c for c in root.children if c.name == 'chosen')
    return (struct.unpack('<Q', chosen.prop('dram-base'))[0],
            struct.unpack('<Q', chosen.prop('dram-size'))[0])


CPU_SUBTYPE_ARM64E = 2


def build(kernel_path, adt_path, cmdline, ramdisk_path=None, allow_unsigned_auth=False, boot_line_length=BOOT_LINE_LENGTH,
          prelinked_path=None, allow_incomplete=False):
    version = next((v for v, n in BOOT_LINES.items() if n == boot_line_length), None)
    if version is None:
        raise SystemExit(f'boot line length must be one of {sorted(BOOT_LINES.values())}')
    seg_table_off = CMDLINE + boot_line_length
    prelinked = None
    if prelinked_path:
        prelinked = prelinked_report(kernel_path, prelinked_path, allow_incomplete)
        kernel_path = prelinked_path
    buf = bytearray(open(kernel_path, 'rb').read())
    m = parse_macho(buf)
    if prelinked is not None or m['ftype'] == MH_FILESET:
        if prelinked is None:
            raise SystemExit(f'{kernel_path}: an MH_FILESET kernel is packed with --prelinked (and its native kernel)')
        check_fileset(buf, m)
        nfix = 0	# arm_init slides and signs the collection (kernel_collection_slide)
    else:
        nfix = apply_chained_fixups(buf, m)
        if m['auth_fixups'] and not allow_unsigned_auth:
            raise SystemExit(f"{kernel_path}: {m['auth_fixups']} authenticated chained fixups would stay unsigned; "
                             'link the arm64e kernel with -threaded_starts_section (patch 0115)')
        if m['cpusub'] == CPU_SUBTYPE_ARM64E and not m['thread_starts'] and not allow_unsigned_auth:
            raise SystemExit(f'{kernel_path}: arm64e kernel without __TEXT,__thread_starts: nothing signs its pointers')
    segs = [s for s in m['segs'] if s['vmsize']]
    text = m['segs'][0]
    assert text['name'] == '__TEXT' and text['fileoff'] == 0
    # virt_base: 32MB (16K-granule L2 block) below the mach header, so start.s's
    # block mapping that starts at the header's block begins exactly at phys_base.
    virt_base = text['vmaddr'] & ~(0x2000000 - 1)
    kernel_end = max(s['vmaddr'] + s['vmsize'] for s in segs)
    kernel_end = (kernel_end + PAGE - 1) & ~(PAGE - 1)

    adt_blob = open(adt_path, 'rb').read()
    # The ADT decides where the kernel's RAM is (mkadt.py --highmem).
    PHYS_BASE, MEM_SIZE = adt_dram(adt_blob)
    pieces = [(s['vmaddr'], s['vmsize'], buf[s['fileoff']:s['fileoff'] + s['filesize']]) for s in segs]
    ramdisk = None
    if ramdisk_path:
        image = open(ramdisk_path, 'rb').read()
        rd_size = (len(image) + PAGE - 1) & ~(PAGE - 1)
        rd_va = kernel_end
        kernel_end = rd_va + rd_size
        rd_pa = rd_va - virt_base + PHYS_BASE
        pieces.append((rd_va, rd_size, image))
        adt_blob = add_ramdisk_to_adt(adt_blob, rd_pa, rd_size)
        ramdisk = (hex(rd_pa), hex(rd_size))

    hdr_size = seg_table_off + 32 * len(pieces)
    out = bytearray(hdr_size)
    seg_table = []
    for vmaddr, vmsize, data in pieces:
        off = len(out)
        out += data
        out += b'\0' * (-len(out) & 0xf)
        seg_table.append((vmaddr, vmsize, off, len(data)))
    adt_off = len(out)
    out += adt_blob
    out += b'\0' * (-len(out) & 0xf)

    struct.pack_into('<8sII', out, 0, b'SWEETXNU', version, len(pieces))
    struct.pack_into('<7Q', out, 0x10, virt_base, m['entry'], adt_off, len(adt_blob), kernel_end,
                     PHYS_BASE, MEM_SIZE)
    cl = cmdline.encode()
    if version == 1:
        cl = cl[:boot_line_length - 1]	# v1 has always truncated
    elif len(cl) >= boot_line_length:
        raise SystemExit(f'command line is {len(cl)} bytes; this kernel takes at most {boot_line_length - 1}')
    out[CMDLINE:CMDLINE + len(cl)] = cl
    for i, t in enumerate(seg_table):
        struct.pack_into('<4Q', out, seg_table_off + 32 * i, *t)
    info = dict(version=version, cputype=hex(m['cputype']), cpusub=m['cpusub'], fixups=nfix, auth_fixups=m['auth_fixups'],
                thread_starts=m['thread_starts'] and (hex(m['thread_starts'][0]), hex(m['thread_starts'][1])),
                virt_base=hex(virt_base),
                entry=hex(m['entry']), kernel_end=hex(kernel_end), ramdisk=ramdisk, prelinked=prelinked, size=len(out),
                segs=[(s['name'], hex(s['vmaddr']), hex(s['vmsize'])) for s in segs])
    return bytes(out), info


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('kernel'); ap.add_argument('adt'); ap.add_argument('out')
    ap.add_argument('--cmdline', default='debug=0xc serial=3 cpus=1 keepsyms=1')
    ap.add_argument('--ramdisk', help='root image for md0 (boot with rd=md0)')
    ap.add_argument('--allow-unsigned-auth', action='store_true',
                    help='pack arm64e authenticated fixups unsigned (stock VMAPPLE kernel experiments only)')
    ap.add_argument('--boot-line-length', type=int, choices=sorted(BOOT_LINES.values()), default=BOOT_LINE_LENGTH,
                    help='boot_args.CommandLine size of the kernel: 608 (xnu-8796, payload v1) or 1024 (xnu-12377, v2)')
    ap.add_argument('--prelinked', metavar='FILESET',
                    help='K2: pack this MH_FILESET emitted from KERNEL by tools/ios27-prelink.py (report FILESET.json)')
    ap.add_argument('--allow-incomplete', action='store_true',
                    help='with --prelinked: pack a fileset whose kext mapping is incomplete (--allow-missing)')
    a = ap.parse_args()
    if a.allow_incomplete and not a.prelinked:
        ap.error('--allow-incomplete needs --prelinked')
    blob, info = build(a.kernel, a.adt, a.cmdline, a.ramdisk, a.allow_unsigned_auth, a.boot_line_length,
                       a.prelinked, a.allow_incomplete)
    open(a.out, 'wb').write(blob)
    for k, v in info.items():
        if k != 'segs':
            print(f'{k}: {v}')
    for s in info['segs']:
        print('  ', *s)
