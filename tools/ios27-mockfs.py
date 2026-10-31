#!/usr/bin/env python3
"""Build the two-file mockfs RAM disk for the iOS 27 kernel milestones K2/K3 (kernel lane mockfs-test-file patch).

  ios27-mockfs.py OUT --launchd FILE [--test FILE] [--trust OUT.tc] [--known-tc TC]... [--report OUT.json]

Image ('N4KROOT', native 4K offsets, at most 32 MiB):
  0     char magic[8] = "N4KROOT\\0"; u32 version = 1; u32 count = 2; u64 launch_offset = 4096, launch_size,
        test_offset (4K aligned, >= 8192), test_size; zero padded to 4096
  4096  the launchd file (served as /sbin/launchd)
  ...   the test file (served under the names dyld and probe.bin, e.g. /usr/lib/dyld)
The image is padded to a 4K multiple. Without --test the launchd file is served for both.

Typical inputs:
  K2  --launchd init (our static arm64e init: clang -target arm64e-apple-ios27.0 -c sweet-init.S; mkinit.py --arm64e)
  K3  --launchd <IPSW /sbin/launchd> --test <IPSW /usr/lib/dyld>   (both are in the IPSW static trust cache)

--trust writes a v2 trust-cache module (ios27-root.py tc_module) with the cdhashes of the served Mach-O files that
are not already in any --known-tc module or bundle; the kernel lane loads it next to the static trust cache.
"""
import argparse, importlib.util, json, os, struct, sys

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location('ios27_root', os.path.join(HERE, 'ios27-root.py'))
root = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(root)

MAGIC, PAGE, MAX_BYTES = b'N4KROOT\0', 4096, 32 << 20
HDR_FMT = '<8sII4Q'


def pad(n, a=PAGE):
    return (n + a - 1) // a * a


def build(launch, test=None):
    test = launch if test is None else test
    if not launch or not test: raise ValueError('empty file')
    toff = max(8192, pad(PAGE + len(launch)))
    total = pad(toff + len(test))
    if total > MAX_BYTES: raise ValueError('image %d bytes > 32 MiB' % total)
    img = bytearray(total)
    struct.pack_into(HDR_FMT, img, 0, MAGIC, 1, 2, PAGE, len(launch), toff, len(test))
    img[PAGE:PAGE + len(launch)] = launch; img[toff:toff + len(test)] = test
    return bytes(img)


def parse(img):
    """Check an image exactly as the kernel patch does; return (launch, test)."""
    magic, ver, cnt, lo, ls, to, ts = struct.unpack_from(HDR_FMT, img)
    n = len(img)
    if (magic != MAGIC or ver != 1 or cnt != 2 or n > MAX_BYTES or lo != PAGE or not ls or not ts or to & 4095 or to < 8192
            or to > n or ls > to - lo or ts > n - to):
        raise ValueError('bad N4KROOT header')
    return img[lo:lo + ls], img[to:to + ts]


def known_hashes(paths):
    out = set()
    for p in paths:
        d = root.unwrap(open(p, 'rb').read())
        mods = [d] if struct.unpack_from('<I', d)[0] == 2 else root.tc_split(d)
        for m in mods: out |= {e[0] for e in root.tc_parse(m)[1]}
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('out'); ap.add_argument('--launchd', required=True); ap.add_argument('--test')
    ap.add_argument('--trust'); ap.add_argument('--known-tc', action='append', default=[]); ap.add_argument('--report')
    a = ap.parse_args(argv)
    files = {'launchd': open(a.launchd, 'rb').read()}
    if a.test: files['test'] = open(a.test, 'rb').read()
    img = build(files['launchd'], files.get('test')); parse(img)
    open(a.out, 'wb').write(img)
    known, rep, own = known_hashes(a.known_tc), {'image': os.path.basename(a.out), 'bytes': len(img), 'files': {}}, []
    for k, data in files.items():
        try: h = root.cdhash(data)
        except (ValueError, struct.error) as e: rep['files'][k] = {'bytes': len(data), 'cdhash': None, 'note': str(e)}; continue
        rep['files'][k] = {'bytes': len(data), 'cdhash': h.hex(), 'in_known_tc': h in known}
        if h not in known: own.append(h)
    if a.trust:
        if own: open(a.trust, 'wb').write(root.tc_module(own, 'fuxi.mockfs')); rep['trust_module_entries'] = len(own)
        else: rep['trust_module_entries'] = 0
    for k, f in rep['files'].items():
        print('%-7s %8d bytes cdhash=%s known=%s' % (k, f['bytes'], f['cdhash'], f.get('in_known_tc')))
    print('%s: %d bytes; trust module entries: %s' % (a.out, len(img), rep.get('trust_module_entries', '-')))
    if a.report: json.dump(rep, open(a.report, 'w'), indent=1)


if __name__ == '__main__':
    main()
