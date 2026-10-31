#!/usr/bin/env python3
"""Assemble the iOS 27 root for Fuxi from IPSW disk images (host side, read only on the inputs).

The root follows UniXNU's raw-window design (docs-lanes/ios27-userland.md): the sealed OS, SystemOS and AppOS
APFS images are copied byte for byte into 1 MiB aligned windows behind a 4 KiB manifest; the kernel publishes each
window as a block device, root-matches the OS volume by UUID, mounts the cryptexes and the Data volume before
launchd. Nothing inside the Apple images is modified, so the volume seals and launchd.plist.sig stay valid; every
Fuxi change lives in the Data volume, staged here as a plain tree.

  ios27-root.py im4p IN OUT                         unwrap an IM4P (LZFSE payloads are decoded)
  ios27-root.py trust-module OUT CDHASH...          v2 trust-cache module holding our own cdhashes (hash type 2)
  ios27-root.py trust-bundle OUT MODULE...          count/offset bundle of v2 modules (IM4P or raw), checked
  ios27-root.py trust-check FILE                    validate a bundle or a single module like the loader does
  ios27-root.py image OUT NAME=PATH[:rw]...         manifest + windows (sparse copy, sha256 per window)
  ios27-root.py image-check IMG [--verify]          parse the manifest; --verify rehashes every window
  ios27-root.py inventory OUT.json --os IMG [--systemos IMG] [--appos IMG] [--scan]
  ios27-root.py data-stage DIR --os IMG [--lite] [--disable LABEL]... [--job-env LABEL:KEY=VALUE]... [--hook FILE.py]...

The manifest uses the header layout UniXNU documents for its userdata image (magic, version 1, window count,
image bytes, 128-byte window entries, SHA-256 of bytes 0..4063 at 4064), so a kernel reader written to that
layout reads ours. On Fuxi the image is a pinned file inside f2fs userdata rather than the raw partition; the
kernel reaches it through an extent table (see the lane doc).

data-stage writes DIR/root (the Data volume root, mounted at /private/var), DIR/meta.tsv (mode uid:gid path for
every staged object, since ownership cannot be set without root) and DIR/apply-owners.sh. The tree is seeded from
the OS image's /private/var (minus staged_system_apps), then the tweaks run, then each --hook FILE.py's
hook(stage). Outputs that hold Apple data stay out of git.
"""
import argparse, hashlib, importlib.util, json, os, plistlib, stat as st, struct, sys, uuid

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location('apfs_ro', os.path.join(HERE, 'apfs-ro.py'))
apfs = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(apfs)

MIB = 1 << 20
MAGIC, HDR, ENTRY, WIN_RO = b'SHNGROOT', 4096, 128, 1
MAX_WINDOWS = (4064 - 32 - 24) // ENTRY		# entries end before the 32 reserved bytes at 4032
TC_MAX_MODULES = 128

# -- IM4P

def der(buf, off):
    tag, ln = buf[off], buf[off + 1]; off += 2
    if ln & 0x80:
        n = ln & 0x7f; ln = int.from_bytes(buf[off:off + n], 'big'); off += n
    return tag, off, off + ln


def im4p(buf):
    """Return (fourcc, payload); LZFSE payloads come back decoded, encrypted ones are refused."""
    tag, s, e = der(buf, 0)
    if tag != 0x30: raise ValueError('not DER')
    items, off = [], s
    while off < e:
        t, vs, ve = der(buf, off); items.append((t, buf[vs:ve])); off = ve
    if items[0][1] != b'IM4P': raise ValueError('not an IM4P')
    if any(t == 0x04 for t, _ in items[4:]): raise ValueError('encrypted IM4P (KBAG)')
    payload = items[3][1]
    if payload[:3] == b'bvx': payload = apfs.lzfse_decode(payload)
    return items[1][1].decode(), payload


def unwrap(buf):
    return im4p(buf)[1] if buf[:1] == b'\x30' else buf

# -- trust caches (v2 modules: u32 version, uuid[16], u32 count, 24-byte entries)

def tc_parse(mod):
    if len(mod) < 24: raise ValueError('module shorter than its header')
    ver, uid, n = struct.unpack_from('<I16sI', mod)
    if ver != 2: raise ValueError('module version %d, want 2' % ver)
    if len(mod) != 24 + 24 * n: raise ValueError('module length %d != 24 + 24*%d' % (len(mod), n))
    ents = [struct.unpack_from('<20sBBBB', mod, 24 + 24 * i) for i in range(n)]
    for i, (h, ht, *_r) in enumerate(ents):
        if not 1 <= ht <= 4: raise ValueError('entry %d hash type %d' % (i, ht))
        if i and h <= ents[i - 1][0]: raise ValueError('entry %d not strictly ascending' % i)
    return uuid.UUID(bytes=uid), ents


def tc_module(cdhashes, name='fuxi.owner', flags=0, category=0):
    hs = sorted(set(bytes.fromhex(h) if isinstance(h, str) else bytes(h) for h in cdhashes))
    if not hs or any(len(h) != 20 for h in hs): raise ValueError('need 20-byte cdhashes')
    uid = uuid.uuid5(uuid.NAMESPACE_URL, name + ':' + ''.join(h.hex() for h in hs))
    return struct.pack('<I16sI', 2, uid.bytes, len(hs)) + b''.join(h + bytes([2, flags, category, 0]) for h in hs)


def tc_bundle(modules):
    if not 0 < len(modules) <= TC_MAX_MODULES: raise ValueError('module count %d' % len(modules))
    out, offs = bytearray(), []
    base = 4 + 4 * len(modules)
    for m in modules:
        tc_parse(m); offs.append(base + len(out)); out += m	# modules are 24+24n bytes: offsets stay 4-aligned
    return struct.pack('<%dI' % (1 + len(modules)), len(modules), *offs) + bytes(out)


def tc_split(data):
    """Split and validate a bundle the way the boot loader does (count 1..128, ordered aligned offsets)."""
    n, = struct.unpack_from('<I', data)
    if not 0 < n <= TC_MAX_MODULES: raise ValueError('bundle count %d' % n)
    offs = list(struct.unpack_from('<%dI' % n, data, 4)) + [len(data)]
    mods = []
    for i in range(n):
        s, e = offs[i], offs[i + 1]
        if s < 4 + 4 * n or s & 3 or e > len(data) or e - s < 24: raise ValueError('module %d bounds %d..%d' % (i, s, e))
        mods.append(data[s:e]); tc_parse(mods[-1])
    return mods

# -- code signatures (Mach-O load commands and the signature blob only)

def cdhash(macho):
    """cdhash (20 bytes) of a thin arm64/arm64e Mach-O: SHA-256 CodeDirectory preferred, else SHA-1."""
    magic, cpu, sub, ftype, ncmds = struct.unpack_from('<5I', macho)
    if magic != 0xfeedfacf: raise ValueError('not a thin 64-bit Mach-O')
    off, sig = 32, None
    for _ in range(ncmds):
        cmd, size = struct.unpack_from('<II', macho, off)
        if cmd == 0x1d: sig = struct.unpack_from('<II', macho, off + 8)
        off += size
    if not sig: raise ValueError('no LC_CODE_SIGNATURE')
    so, sl = sig; blob = macho[so:so + sl]
    m, ln, cnt = struct.unpack_from('>III', blob)
    if m != 0xfade0cc0: raise ValueError('bad SuperBlob')
    best = None
    for i in range(cnt):
        typ, o = struct.unpack_from('>II', blob, 12 + 8 * i)
        if typ == 0 or 0x1000 <= typ < 0x1005:
            cm, cl = struct.unpack_from('>II', blob, o)
            if cm != 0xfade0c02: continue
            ht = blob[o + 37]
            cd = blob[o:o + cl]
            h = hashlib.sha256(cd).digest() if ht == 2 else hashlib.sha1(cd).digest() if ht == 1 else hashlib.sha384(cd).digest()
            if best is None or (ht == 2) > (best[0] == 2): best = (ht, h[:20])
    if best is None: raise ValueError('no CodeDirectory')
    return best[1]

# -- windows image

def align(n, a=MIB):
    return (n + a - 1) // a * a


def image_build(out, windows, log=print):
    """windows: [(name, path, read_only)]. Writes OUT, returns the manifest as a list of dicts."""
    if not 0 < len(windows) <= MAX_WINDOWS: raise ValueError('window count %d' % len(windows))
    hdr, ents, off = bytearray(HDR), [], MIB
    with open(out, 'wb') as f:
        for i, (name, path, ro) in enumerate(windows):
            nb = name.encode()
            if not nb or len(nb) > 31: raise ValueError('window name %r' % name)
            size = os.path.getsize(path)
            if size == 0 or size % 512: raise ValueError('%s: size %d not a positive multiple of 512' % (name, size))
            h = hashlib.sha256(); f.seek(off)
            with open(path, 'rb') as src:
                while True:
                    b = src.read(MIB)
                    if not b: break
                    h.update(b)
                    if b.count(0) == len(b): f.seek(len(b), 1)	# keep zero runs sparse
                    else: f.write(b)
            d = h.digest()
            struct.pack_into('<32sQQII32s40s', hdr, 24 + ENTRY * i, nb, off, size, 512, WIN_RO if ro else 0, d, bytes(40))
            ents.append({'name': name, 'offset': off, 'size': size, 'read_only': ro, 'sha256': d.hex()})
            log('window %-9s offset=%#x size=%d (%.1f MiB) %s' % (name, off, size, size / MIB, 'ro' if ro else 'rw'))
            off = align(off + size)
        struct.pack_into('<8sIIQ', hdr, 0, MAGIC, 1, len(windows), off)
        hdr[4064:4096] = hashlib.sha256(bytes(hdr[:4064])).digest()
        f.seek(0); f.write(hdr); f.truncate(off)
    return ents


def image_parse(hdr):
    magic, ver, n, total = struct.unpack_from('<8sIIQ', hdr)
    if magic != MAGIC or ver != 1: raise ValueError('bad manifest magic/version')
    if hashlib.sha256(hdr[:4064]).digest() != hdr[4064:4096]: raise ValueError('manifest sha256 mismatch')
    if not 0 < n <= MAX_WINDOWS: raise ValueError('window count %d' % n)
    ents, prev = [], MIB
    for i in range(n):
        nb, off, size, bs, flags, d, _ = struct.unpack_from('<32sQQII32s40s', hdr, 24 + ENTRY * i)
        if off % MIB or off < prev or size == 0 or off + size > total or bs != 512:
            raise ValueError('window %d bounds' % i)
        prev = off + size
        ents.append({'name': nb.split(b'\0')[0].decode(), 'offset': off, 'size': size, 'read_only': bool(flags & 1),
                     'sha256': d.hex()})
    return total, ents


def image_verify(path, ents):
    with open(path, 'rb') as f:
        for e in ents:
            h, left = hashlib.sha256(), e['size']; f.seek(e['offset'])
            while left:
                b = f.read(min(MIB, left)); h.update(b); left -= len(b)
            if h.hexdigest() != e['sha256']: raise ValueError('window %s sha256 mismatch' % e['name'])

# -- inventory

def volume_info(path, scan=False):
    c = apfs.Container(path)
    out = {'path': os.path.basename(path), 'bytes': os.path.getsize(path), 'block_size': c.bs, 'blocks': c.block_count,
           'volumes': []}
    for v in c.volumes:
        vi = {'name': v.name, 'uuid': v.uuid_str, 'role': apfs.ROLE_NAMES.get(v.role, hex(v.role)), 'sealed': v.sealed,
              'files': v.num_files, 'dirs': v.num_dirs, 'symlinks': v.num_symlinks, 'incompat': hex(v.incompat)}
        if scan:
            comp = {}
            for p, ino in v.listing():
                if not st.S_ISREG(ino.mode): continue
                t = v.compression_type(ino)
                k = comp.setdefault(str(t), [0, 0]); k[0] += 1; k[1] += ino.size
            vi['compression'] = {t: {'files': n, 'bytes': b} for t, (n, b) in sorted(comp.items())}
        out['volumes'].append(vi)
    return out, c


def launchd_jobs(vol):
    data = plistlib.loads(vol.read('/System/Library/xpc/launchd.plist'))
    jobs = {}
    for path, j in data.get('LaunchDaemons', {}).items():
        args = j.get('ProgramArguments') or ['']
        jobs[j.get('Label', path)] = {'program': j.get('Program') or args[0], 'user': j.get('UserName', 'root'),
                                      'keepalive': j.get('KeepAlive') is True, 'runatload': bool(j.get('RunAtLoad')),
                                      'panic_on_crash': bool(j.get('_PanicOnCrash')), 'disabled': bool(j.get('Disabled')),
                                      'mach_services': sorted(j.get('MachServices', {}))}
    return data.get('VersionNumber'), jobs


def inventory(args):
    rep = {}
    for key in ('os', 'systemos', 'appos'):
        p = getattr(args, key)
        if p: rep[key], c = volume_info(p, args.scan)
        if key == 'os' and p:
            vol = c.volumes[0]
            ver, jobs = launchd_jobs(vol)
            rep['launchd'] = {'version': ver, 'jobs': len(jobs), 'keepalive_or_runatload': sum(j['keepalive'] or j['runatload'] for j in jobs.values()),
                              'panic_on_crash': sorted(l for l, j in jobs.items() if j['panic_on_crash']),
                              'disabled_by_default': sorted(l for l, j in jobs.items() if j['disabled']),
                              'early_disable': {l: l in jobs for l in early_disable(True)}}
            rep['launchd_jobs'] = jobs
            try: rep['system_version'] = plistlib.loads(vol.read('/System/Library/CoreServices/SystemVersion.plist'))
            except (apfs.ApfsError, KeyError, ValueError): pass
    with open(args.out, 'w') as f: json.dump(rep, f, indent=1, sort_keys=True, default=str)
    for key in ('os', 'systemos', 'appos'):
        for v in rep.get(key, {}).get('volumes', []):
            print('%-8s %-40s role=%s sealed=%d files=%d %s' % (key, v['name'], v['role'], v['sealed'], v['files'],
                  ' '.join('t%s:%d/%.0fMiB' % (t, d['files'], d['bytes'] / MIB) for t, d in v.get('compression', {}).items())))
    if 'launchd' in rep:
        l = rep['launchd']; print('launchd jobs=%d keepalive|runatload=%d panic_on_crash=%d missing_early=%s' % (
            l['jobs'], l['keepalive_or_runatload'], len(l['panic_on_crash']), [k for k, ok in l['early_disable'].items() if not ok]))

# -- Data volume staging

# UniXNU's sheng-lite list (iPad: fairplayd.A2), with the iPhone fairplayd.H2 label; EARLY: Fuxi has none of this hardware.
LITE = ['identityservicesd', 'locationd', 'CommCenter', 'bluetoothd', 'photoanalysisd', 'mediaanalysisd', 'mediastream.mstreamd',
        'icloud.searchpartyd', 'findmy.findmybeaconingd', 'familycircled', 'financed', 'softwareupdateservicesd', 'ndoagent',
        'biomesyncd', 'email.maild', 'amsengagementd', 'adid', 'eyereliefd', 'fairplayd.H2', 'passd', 'mobile.obliteration',
        'coreduetd']
EARLY = ['watchdogd', 'cameracaptured', 'wifid', 'WirelessRadioManager', 'nfcd']


def early_disable(lite):
    return ['com.apple.' + n for n in (EARLY + (LITE if lite else []))]


MOBILE, ROOT = (501, 501), (0, 0)
SETUP_PREFS = {
    'com.apple.purplebuddy': {'SetupDone': True, 'SetupFinishedAllSteps': True, 'SetupVersion': 11, 'daemonizeNeedsToRun': False},
    'com.apple.purplebuddy.notbackedup': {k: False for k in ('ShouldRunDiagnosticsMiniBuddy', 'ShouldRunHSA2MiniBuddy',
        'ShouldRunKeychainSyncMiniBuddy', 'ShouldRunMesaMiniBuddy', 'ShouldRunPaymentMiniBuddy', 'ShouldRunStandardMiniBuddy',
        'SilentICDPUpgrade')},
    'com.apple.springboard': {'BKNoWatchdogs': True, 'FBLaunchWatchdogExceptions': {'com.apple.purplebuddy': 100.0},
                              'MCForceShowCloudConfigurationUI': False},
    'com.apple.managedconfiguration.notbackedup': {'LockdownActivationIndicatesCloudConfigurationAvailable': False},
}
CA_PREFS = {'CAEnableDeepFramebuffer': False, 'CAEnableFramebufferCompression': False, 'CAEnableIOSurfaceCompression': False}
MG_CACHE = 'containers/Shared/SystemGroup/systemgroup.com.apple.mobilegestaltcache/Library/Caches/com.apple.MobileGestalt.plist'


class Stage:
    """The Data volume tree under DIR/root plus the owners/modes it should carry (path relative to /private/var)."""

    def __init__(self, top):
        self.top, self.root, self.meta, self.log = top, os.path.join(top, 'root'), {}, []

    def p(self, rel): return os.path.join(self.root, rel.strip('/'))

    def mkdir(self, rel, mode=0o755, owner=ROOT):
        parts = rel.strip('/').split('/')
        for i in range(1, len(parts) + 1):
            r = '/'.join(parts[:i])
            if not os.path.isdir(self.p(r)): os.makedirs(self.p(r)); self.meta.setdefault(r, (st.S_IFDIR | 0o755, ROOT))
        self.meta[rel.strip('/')] = (st.S_IFDIR | mode, owner)

    def write(self, rel, data, mode=0o644, owner=ROOT):
        rel = rel.strip('/'); d = os.path.dirname(rel)
        if d and not os.path.isdir(self.p(d)): self.mkdir(d, 0o755, owner)
        with open(self.p(rel), 'wb') as f: f.write(data)
        self.meta[rel] = (st.S_IFREG | mode, owner); self.log.append('write ' + rel)

    def plist(self, rel, values, mode=0o600, owner=MOBILE, merge=True):
        cur = plistlib.load(open(self.p(rel), 'rb')) if merge and os.path.exists(self.p(rel)) else {}
        cur.update(values); self.write(rel, plistlib.dumps(cur, fmt=plistlib.FMT_BINARY), mode, owner)

    def pref(self, domain, values, user='mobile'):
        base = 'mobile/Library/Preferences' if user == 'mobile' else 'root/Library/Preferences'
        self.plist('%s/%s.plist' % (base, domain), values, 0o600, MOBILE if user == 'mobile' else ROOT)

    def remove(self, rel):
        rel = rel.strip('/')
        if os.path.lexists(self.p(rel)): os.unlink(self.p(rel)); self.meta.pop(rel, None); self.log.append('remove ' + rel)

    def symlink(self, rel, target, owner=ROOT):
        rel = rel.strip('/')
        if os.path.lexists(self.p(rel)): os.unlink(self.p(rel))
        os.symlink(target, self.p(rel)); self.meta[rel] = (st.S_IFLNK | 0o755, owner)

    def finish(self):
        rows = sorted(self.meta.items())
        with open(os.path.join(self.top, 'meta.tsv'), 'w') as f:
            for rel, (mode, (u, g)) in rows: f.write('%o\t%d:%d\t%s\n' % (mode, u, g, rel))
        with open(os.path.join(self.top, 'apply-owners.sh'), 'w') as f:
            f.write('#!/bin/sh\n# usage: apply-owners.sh MOUNTED_DATA_ROOT  (run as root on the host that builds the APFS Data image)\nset -e\ncd "$1"\n')
            for rel, (mode, (u, g)) in rows:
                q = "'" + rel.replace("'", "'\\''") + "'"
                f.write('chown -h %d:%d %s\n' % (u, g, q))
                if not st.S_ISLNK(mode): f.write('chmod %o %s\n' % (mode & 0o7777, q))
        os.chmod(os.path.join(self.top, 'apply-owners.sh'), 0o755)
        return len(rows)


def seed_from_os(stage, vol, skip=('staged_system_apps',)):
    """Copy the OS image's /private/var (what restore seeds the Data volume with) into the stage."""
    oid = vol.lookup('/private/var'); stack = [('', oid)]; n = 0
    while stack:
        rel, oid = stack.pop(); ino = vol.inode(oid)
        if rel: stage.meta[rel] = (ino.mode, (ino.uid, ino.gid))
        dest = stage.p(rel) if rel else stage.root
        if ino.is_dir:
            os.makedirs(dest, exist_ok=True)
            for name, fid, _ in vol.readdir(oid):
                if not rel and name in skip: continue
                stack.append(((rel + '/' if rel else '') + name, fid))
        elif st.S_ISLNK(ino.mode): os.symlink(vol.readlink(ino), dest)
        elif st.S_ISREG(ino.mode):
            with open(dest, 'wb') as f: f.write(vol.read_inode(ino))
        n += 1
    return n


def tweaks(stage, args, build, labels):
    disabled = early_disable(args.lite) + list(args.disable)
    unknown = sorted(l for l in disabled if labels is not None and l not in labels)
    known = sorted(set(disabled) - set(unknown))
    # launchd's own override database on Data: the signed launchd.plist cache stays untouched.
    stage.plist('db/com.apple.xpc.launchd/disabled.plist', {l: True for l in known}, 0o644, ROOT)
    for d, owner in (('containers/Bundle', (33, 33)), ('containers/Data/System', ROOT), ('containers/Shared/SystemGroup', ROOT),
                     ('containers/Temp', ROOT)):
        stage.mkdir(d, 0o755, owner)
    stage.pref('com.apple.migration', {'LastSystemVersion': build,
                                       'DMUserDataDisposition': {'SyntheticDidUpgrade': False, 'buildVersion': build}})
    for dom, vals in SETUP_PREFS.items(): stage.pref(dom, vals)
    for user in ('mobile', 'root'):
        stage.pref('com.apple.backboardd', CA_PREFS, user); stage.pref('.GlobalPreferences', CA_PREFS, user)
    stage.remove(MG_CACHE)
    env = {}
    for spec in args.job_env:
        label, kv = spec.split(':', 1); k, v = kv.split('=', 1)
        if v == '': env.setdefault(label, {}).pop(k, None)
        else: env.setdefault(label, {})[k] = v
    stage.plist('db/fuxi/job-env.plist', env, 0o644, ROOT, merge=False)
    return known, unknown


def data_stage(args):
    stage = Stage(args.dir)
    if os.path.exists(stage.root) and os.listdir(stage.root): raise SystemExit('%s exists and is not empty' % stage.root)
    os.makedirs(stage.root, exist_ok=True)
    labels, build = None, args.build
    if args.os:
        vol = apfs.Container(args.os).volumes[0]
        n = seed_from_os(stage, vol); print('seeded %d objects from the OS image /private/var' % n)
        _, jobs = launchd_jobs(vol); labels = set(jobs)
        if not build: build = plistlib.loads(vol.read('/System/Library/CoreServices/SystemVersion.plist'))['ProductBuildVersion']
    if not build: raise SystemExit('need --build or --os')
    known, unknown = tweaks(stage, args, build, labels)
    for h in args.hook:
        spec = importlib.util.spec_from_file_location('hook', h); mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod); mod.hook(stage); print('hook', h)
    n = stage.finish()
    print('build=%s disabled=%d unknown_labels=%s objects=%d' % (build, len(known), unknown, n))
    for l in stage.log: print(' ', l)

# -- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest='cmd', required=True)
    a = sp.add_parser('im4p'); a.add_argument('inp'); a.add_argument('out')
    a = sp.add_parser('trust-module'); a.add_argument('out'); a.add_argument('cdhash', nargs='+'); a.add_argument('--name', default='fuxi.owner')
    a = sp.add_parser('trust-bundle'); a.add_argument('out'); a.add_argument('module', nargs='+')
    a = sp.add_parser('trust-check'); a.add_argument('file')
    a = sp.add_parser('image'); a.add_argument('out'); a.add_argument('window', nargs='+'); a.add_argument('--report')
    a = sp.add_parser('image-check'); a.add_argument('img'); a.add_argument('--verify', action='store_true')
    a = sp.add_parser('inventory'); a.add_argument('out')
    for k in ('os', 'systemos', 'appos'): a.add_argument('--' + k)
    a.add_argument('--scan', action='store_true', help='walk every file for compression statistics (minutes for the OS)')
    a = sp.add_parser('data-stage'); a.add_argument('dir'); a.add_argument('--os'); a.add_argument('--build')
    a.add_argument('--lite', action='store_true'); a.add_argument('--disable', action='append', default=[])
    a.add_argument('--job-env', action='append', default=[], metavar='LABEL:KEY=VALUE'); a.add_argument('--hook', action='append', default=[])
    args = ap.parse_args(argv)
    if args.cmd == 'im4p':
        t, p = im4p(open(args.inp, 'rb').read()); open(args.out, 'wb').write(p); print('%s: %s %d bytes' % (args.out, t, len(p)))
    elif args.cmd == 'trust-module':
        m = tc_module(args.cdhash, args.name); open(args.out, 'wb').write(m); print('%s: %d entries' % (args.out, (len(m) - 24) // 24))
    elif args.cmd == 'trust-bundle':
        mods = [unwrap(open(p, 'rb').read()) for p in args.module]
        b = tc_bundle(mods); tc_split(b); open(args.out, 'wb').write(b)
        print('%s: %d modules, %d entries, %d bytes' % (args.out, len(mods), sum((len(m) - 24) // 24 for m in mods), len(b)))
    elif args.cmd == 'trust-check':
        d = unwrap(open(args.file, 'rb').read())
        mods = [d] if struct.unpack_from('<I', d)[0] == 2 and len(d) == 24 + 24 * struct.unpack_from('<I', d, 20)[0] else tc_split(d)
        for m in mods:
            u, ents = tc_parse(m); print('module entries=%d hash_types=%s' % (len(ents), sorted({e[1] for e in ents})))
    elif args.cmd == 'image':
        wins = []
        for w in args.window:
            name, path = w.split('=', 1); ro = True
            if path.endswith(':rw'): path, ro = path[:-3], False
            wins.append((name, path, ro))
        ents = image_build(args.out, wins); total = os.path.getsize(args.out)
        print('%s: %d windows, %d bytes (%.2f GiB)' % (args.out, len(ents), total, total / (1 << 30)))
        if args.report:
            vols = {}
            for name, path, _ in wins:
                try: vols[name] = [{'name': v.name, 'uuid': v.uuid_str, 'role': apfs.ROLE_NAMES.get(v.role)} for v in apfs.Container(path).volumes]
                except (apfs.ApfsError, struct.error): vols[name] = None
            json.dump({'image_bytes': total, 'windows': ents, 'volumes': vols}, open(args.report, 'w'), indent=1)
    elif args.cmd == 'image-check':
        total, ents = image_parse(open(args.img, 'rb').read(HDR))
        for e in ents: print('%-9s offset=%#x size=%d %s' % (e['name'], e['offset'], e['size'], 'ro' if e['read_only'] else 'rw'))
        if args.verify: image_verify(args.img, ents); print('all window hashes match')
        print('image_bytes=%d' % total)
    elif args.cmd == 'inventory': inventory(args)
    elif args.cmd == 'data-stage': data_stage(args)


if __name__ == '__main__':
    main()
