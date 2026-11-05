#!/usr/bin/env python3
"""Map the "SWEET AKS BT:" lines of a boot log (patch 0125, osfmk/kern/fuxi_aks_bt.c) to dyld shared cache images.

  fuxi-aks-bt-map.py DSC_MAIN BOOT.LOG          (run it in WSL; read-only)

  e.g. python3 tools/fuxi-aks-bt-map.py ~/work/dsc/dyld_shared_cache_arm64 \\
           /mnt/c/Users/DangTQ/Xiaomi-Backup/boot-logs/NAME/boot.log

DSC_MAIN is the main cache file of the root the phone ran; its subcaches (DSC_MAIN.01, .02, ...) are read too.
For each backtrace the kernel printed it says, per frame: the address, then
  - in the shared region: the cache image and offset, from unslid = address - slide (the slide is the kernel's,
    checked against the cache: start - slide must be the main file's first mapping);
  - outside it: the image the kernel found in the process's dyld image list (highest load address at or below the
    address) and the offset from it: a bundle or the app's own binary, which are not in the cache.
The kernel's lr (return address into the trap stub's caller) is mapped too. No symbols: the copy in ~/work/dsc
has no __LINKEDIT subcache (5 Nov: its mappings end at 0x20ce4c000, the images' __LINKEDIT is at 0x217040000).
"""
import argparse
import bisect
import glob
import re
import struct
import sys

LC_SEGMENT_64 = 0x19
LINE = re.compile(r'SWEET AKS BT: (.+?)\[(\d+)\] (.*)$')


class Cache:
    def __init__(self, main):
        self.files = [main] + sorted(f for f in glob.glob(main + '.*') if f[len(main) + 1:].isdigit())
        self.fh = {f: open(f, 'rb') for f in self.files}
        self.maps = []                          # (unslid address, size, file offset, file)
        for f in self.files:
            hdr = self.read_file(f, 0, 0x200)
            mo, mc = struct.unpack_from('<II', hdr, 0x10)
            raw = self.read_file(f, mo, 32 * mc)
            for i in range(mc):
                addr, size, off, _, _ = struct.unpack_from('<QQQII', raw, 32 * i)
                self.maps.append((addr, size, off, f))
        self.base = self.maps[0][0]             # the main file's first mapping: where the header goes, unslid
        h = self.read_file(main, 0, 0x200)
        io, ic = struct.unpack_from('<II', h, 0x18)
        if ic == 0:
            io, ic = struct.unpack_from('<II', h, 0x1c0)
        raw = self.read_file(main, io, 32 * ic)
        self.images = []                        # (__TEXT start, __TEXT end, header address, path), unslid
        for i in range(ic):
            addr, _, _, po, _ = struct.unpack_from('<QQQII', raw, 32 * i)
            path = self.read_file(main, po, 512).split(b'\0')[0].decode(errors='replace')
            start, end = self.text_range(addr)
            self.images.append((start, end, addr, path))
        self.images.sort()
        self.starts = [i[0] for i in self.images]

    def read_file(self, f, off, n):
        self.fh[f].seek(off)
        return self.fh[f].read(n)

    def rd(self, addr, n):
        for a, s, o, f in self.maps:
            if a <= addr < a + s:
                return self.read_file(f, o + addr - a, n)
        raise KeyError(hex(addr))

    def commands(self, addr):
        _, _, _, _, ncmds, size, _, _ = struct.unpack('<IiiIIIII', self.rd(addr, 32))
        lc, p = self.rd(addr + 32, size), 0
        for _ in range(ncmds):
            cmd, cs = struct.unpack_from('<II', lc, p)
            yield cmd, lc[p:p + cs]
            p += cs

    def text_range(self, addr):
        for cmd, body in self.commands(addr):
            if cmd == LC_SEGMENT_64 and body[8:24].split(b'\0')[0] == b'__TEXT':
                vm, vs = struct.unpack_from('<QQ', body, 24)
                return vm, vm + vs
        return addr, addr + 1

    def image(self, unslid):
        i = bisect.bisect_right(self.starts, unslid) - 1
        if i >= 0 and self.images[i][0] <= unslid < self.images[i][1]:
            return self.images[i]
        return None


def blocks(lines):
    """The backtraces in the log: a dict per header line, frames in order, keyed by NAME[PID] while it runs."""
    open_blocks, out = {}, []
    for line in lines:
        m = LINE.search(line)
        if not m:
            continue
        key, rest = (m[1], m[2]), m[3]
        if ' calls in 1 s; ' in rest:
            b = {'name': m[1], 'pid': m[2], 'head': rest, 'region': None, 'frames': [], 'end': None}
            b['lr'] = int(re.search(r'\blr (0x[0-9a-f]+)', rest)[1], 16)
            open_blocks[key] = b
            out.append(b)
            continue
        b = open_blocks.get(key)
        if b is None:
            continue
        r = re.match(r'shared region (0x[0-9a-f]+) size (0x[0-9a-f]+) start (0x[0-9a-f]+) slide (0x[0-9a-f]+) '
                     r'main (0x[0-9a-f]+)$', rest)
        f = re.match(r'#(\d+) (0x[0-9a-f]+)(?: img (\?|0x[0-9a-f]+)(?: (.*))?)?$', rest)
        if r:
            b['region'] = [int(x, 16) for x in r.groups()]
        elif f:
            load = None if f[3] in (None, '?') else int(f[3], 16)
            b['frames'].append((int(f[1]), int(f[2], 16), f[3] is not None, load, f[4]))
        elif rest.startswith('end: '):
            b['end'] = rest
            del open_blocks[key]
    return out


def describe(cache, region, addr):
    base, size, start, slide, main = region
    if not (base <= addr < base + size):
        return None
    unslid = addr - slide
    img = cache.image(unslid)
    if img is None:
        return 'cache 0x%x (in no image\'s __TEXT)' % unslid
    text = '%s +0x%x' % (img[3].rsplit('/', 1)[-1], unslid - img[2])
    return '%-40s (%s, cache 0x%x)' % (text, img[3], unslid)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('dsc_main')
    ap.add_argument('log')
    args = ap.parse_args()
    with open(args.log, 'rb') as f:
        lines = f.read().decode('latin-1').splitlines()
    found = blocks(lines)
    if not found:
        print('no "SWEET AKS BT:" backtrace in %s (check: grep -a "SWEET AKS" %s)' % (args.log, args.log))
        return 1
    cache = Cache(args.dsc_main)
    for b in found:
        print('%s[%s] %s' % (b['name'], b['pid'], b['head']))
        if b['region'] is None:
            print('  no "shared region" line: cache addresses cannot be unslid')
            b['region'] = [0, 0, 0, 0, 0]
        base, size, start, slide, main = b['region']
        ok = 'matches' if start - slide == cache.base else 'DOES NOT match (another cache?)'
        print('  slide 0x%x; start - slide = 0x%x %s the cache\'s first mapping 0x%x; main binary at 0x%x' %
              (slide, start - slide, ok, cache.base, main))
        where = describe(cache, b['region'], b['lr'])
        print('  lr  0x%x  %s' % (b['lr'], where or 'outside the shared region'))
        for n, addr, outside, load, path in b['frames']:
            where = describe(cache, b['region'], addr)
            if where is None:
                if load is None:
                    where = 'outside the shared region, no image found'
                else:
                    who = 'main binary ' if load == main else ''
                    where = '%s%s +0x%x   (%s)' % (who, (path or '?').rsplit('/', 1)[-1], addr - load, path)
            print('  #%02d 0x%x  %s' % (n, addr, where))
        print('  ' + (b['end'] or 'no "end:" line (log cut?)'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
