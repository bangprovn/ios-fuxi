#!/usr/bin/env python3
"""Minimal read-only APFS reader (own implementation, written from the public Apple File System Reference).

Enough to list and read files of an unencrypted APFS container image, such as the OS, SystemOS and AppOS disk
images of an iOS IPSW: checkpoint selection, object maps, file-system and sealed (fext) trees, inodes, directory
records, extended attributes, symlinks, hard links and decmpfs compression (zlib, LZVN, LZFSE, uncompressed;
LZBITMAP is reported, not decoded). No writes, no encryption, no snapshots.

  apfs-ro.py IMAGE info
  apfs-ro.py IMAGE ls PATH
  apfs-ro.py IMAGE cat PATH [OUT]
  apfs-ro.py IMAGE stat PATH
  apfs-ro.py IMAGE list [--vol N]          # 'mode uid:gid size ./path -> target', the format of a Mac stat listing
  apfs-ro.py IMAGE extract PATH OUTDIR     # one file or a whole subtree; owners and modes go to OUTDIR.meta.tsv

Usable as a module: Container(path).volumes[0].read('/sbin/launchd').
"""
import io, os, stat as st, struct, sys, zlib

OBJ_PHYSICAL, OBJ_TYPE_MASK, OBJ_STORAGE_MASK = 0x40000000, 0x0000ffff, 0xc0000000
T_NXSB, T_BTREE, T_BTNODE, T_OMAP, T_FS = 0x1, 0x2, 0x3, 0xb, 0xd
BTNODE_ROOT, BTNODE_LEAF, BTNODE_FIXED, BTNODE_NOHEADER = 1, 2, 4, 0x10
BTREE_HASHED = 0x80
J_INODE, J_XATTR, J_EXTENT, J_DREC = 3, 4, 8, 9
INO_EXT_DSTREAM = 8
XATTR_STREAM, XATTR_EMBEDDED = 1, 2
INCOMPAT_CASE_INSENSITIVE, INCOMPAT_NORM_INSENSITIVE, INCOMPAT_SEALED = 0x1, 0x8, 0x20
UF_COMPRESSED = 0x20
ROLE_NAMES = {0: 'none', 1: 'System', 2: 'User', 4: 'Recovery', 8: 'VM', 16: 'Preboot', 32: 'Installer',
              64: 'Data', 128: 'Baseband', 192: 'Update', 256: 'xART', 320: 'Hardware', 384: 'Backup'}


class ApfsError(Exception):
    pass


def fletcher64(block):
    """APFS object checksum over block[8:], as stored in the first 8 bytes."""
    words = struct.unpack_from('<%dI' % ((len(block) - 8) // 4), block, 8)
    s1 = s2 = 0
    for w in words:
        s1 = (s1 + w) % 0xffffffff
        s2 = (s2 + s1) % 0xffffffff
    c1 = 0xffffffff - (s1 + s2) % 0xffffffff
    c2 = 0xffffffff - (s1 + c1) % 0xffffffff
    return (c2 << 32) | c1


# ---------------------------------------------------------------------------------------------------- decompression
def lzvn_decode(src, out=None, limit=None):
    """LZVN stream decoder (the opcode set of Apple's LZVN as documented in the open lzfse sources' format)."""
    dst = bytearray() if out is None else out
    i, n, d = 0, len(src), 0
    while i < n:
        op = src[i]
        if op == 0x06:					# end of stream
            break
        if op in (0x0e, 0x16):				# nop
            i += 1; continue
        hi = op >> 4
        if op >= 0xf0:					# matches with the previous distance
            if op == 0xf0:
                m = src[i + 1] + 16; i += 2
            else:
                m = op & 0xf; i += 1
            lit = 0
        elif op >= 0xe0:				# literals only
            if op == 0xe0:
                lit = src[i + 1] + 16; i += 2
            else:
                lit = op & 0xf; i += 1
            dst += src[i:i + lit]; i += lit
            continue
        elif hi == 0xd or hi == 0x7 or (op < 0x40 and (op & 7) == 6):
            raise ApfsError('lzvn: undefined opcode %#x at %d' % (op, i))
        elif 0xa0 <= op < 0xc0:				# medium distance
            t = src[i + 1] | src[i + 2] << 8
            lit = (op >> 3) & 3
            m = (((op & 7) << 2) | (t & 3)) + 3
            d = t >> 2; i += 3
        elif (op & 7) == 7:				# large distance
            lit, m = op >> 6, ((op >> 3) & 7) + 3
            d = src[i + 1] | src[i + 2] << 8; i += 3
        elif (op & 7) == 6:				# previous distance
            lit, m = op >> 6, ((op >> 3) & 7) + 3; i += 1
        else:						# small distance
            lit, m = op >> 6, ((op >> 3) & 7) + 3
            d = ((op & 7) << 8) | src[i + 1]; i += 2
        if lit:
            dst += src[i:i + lit]; i += lit
        if m:
            if d == 0 or d > len(dst):
                raise ApfsError('lzvn: bad distance %d' % d)
            start = len(dst) - d
            if d >= m:
                dst += dst[start:start + m]
            else:
                for k in range(m):
                    dst.append(dst[start + k])
        if limit is not None and len(dst) >= limit:
            break
    return dst


# LZFSE (v2 'bvx2' / v1 'bvx1' blocks with FSE-coded literals and L/M/D streams, 'bvxn' LZVN blocks, 'bvx-' raw).
LZFSE_L_BITS = [0] * 16 + [2, 3, 5, 8]
LZFSE_L_BASE = list(range(16)) + [16, 20, 28, 60]
LZFSE_M_BITS = [0] * 16 + [3, 5, 8, 11]
LZFSE_M_BASE = list(range(16)) + [16, 24, 56, 312]
LZFSE_D_BITS = [0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4, 5, 5, 5, 5, 6, 6, 6, 6, 7, 7, 7, 7,
                8, 8, 8, 8, 9, 9, 9, 9, 10, 10, 10, 10, 11, 11, 11, 11, 12, 12, 12, 12, 13, 13, 13, 13, 14, 14, 14, 14,
                15, 15, 15, 15]
LZFSE_D_BASE = [0, 1, 2, 3, 4, 6, 8, 10, 12, 16, 20, 24, 28, 36, 44, 52, 60, 76, 92, 108, 124, 156, 188, 220, 252,
                316, 380, 444, 508, 636, 764, 892, 1020, 1276, 1532, 1788, 2044, 2556, 3068, 3580, 4092, 5116, 6140,
                7164, 8188, 10236, 12284, 14332, 16380, 20476, 24572, 28668, 32764, 40956, 49148, 57340, 65532, 81916,
                98300, 114684, 131068, 163836, 196604, 229372]
LZFSE_FREQ_NBITS = [2, 3, 2, 5, 2, 3, 2, 8, 2, 3, 2, 5, 2, 3, 2, 14, 2, 3, 2, 5, 2, 3, 2, 8, 2, 3, 2, 5, 2, 3, 2, 14]
LZFSE_FREQ_VALUE = [0, 2, 1, 4, 0, 3, 1, -1, 0, 2, 1, 5, 0, 3, 1, -1, 0, 2, 1, 6, 0, 3, 1, -1, 0, 2, 1, 7, 0, 3, 1, -1]


def _fse_table(nstates, freqs):
    """Decoder entries (k, delta, symbol) per state for a normalised frequency table."""
    table = [None] * nstates
    nclz = (nstates.bit_length() - 1)
    pos = 0
    for sym, f in enumerate(freqs):
        if not f:
            continue
        k = (f.bit_length() - 1)
        k = nclz - k				# bits for the high states
        j0 = ((2 * nstates) >> k) - f
        for j in range(f):
            if j < j0:
                table[pos] = (k, ((f + j) << k) - nstates, sym)
            else:
                table[pos] = (k - 1, ((j - j0) << (k - 1)), sym)
            pos += 1
    if pos != nstates:
        raise ApfsError('lzfse: frequency table does not fill %d states' % nstates)
    return table


def _value_table(nstates, freqs, vbits, vbase):
    table = _fse_table(nstates, freqs)
    return [(k, d, vbits[s], vbase[s]) for k, d, s in table]


class _BackBits:
    """FSE bit stream, read backwards from the end of a buffer (the lzfse 64-bit stream layout)."""

    def __init__(self, buf, start, end, nbits_initial):
        self.buf, self.start, self.pos = buf, start, end
        # The stream ends with the first accumulator: 8 bytes holding 64+n valid bits (n = -7..-1), or 7 bytes when n = 0.
        if nbits_initial < -7 or nbits_initial > 0:
            raise ApfsError('lzfse: bad initial bit count')
        nbytes = 8 if nbits_initial else 7
        self.pos -= nbytes
        if self.pos < start:
            raise ApfsError('lzfse: bit stream underrun')
        self.acc = int.from_bytes(buf[self.pos:self.pos + nbytes], 'little')
        self.n = 8 * nbytes + nbits_initial
        if self.acc >> self.n:
            raise ApfsError('lzfse: bad bit stream padding')

    def fill(self):
        if self.n >= 56:
            return
        nbytes = (63 - self.n) >> 3
        self.pos -= nbytes
        if self.pos < self.start:
            raise ApfsError('lzfse: bit stream underrun')
        incoming = int.from_bytes(self.buf[self.pos:self.pos + nbytes], 'little')
        self.acc = (self.acc << (nbytes * 8)) | incoming
        self.n += nbytes * 8
        self.acc &= (1 << self.n) - 1

    def pull(self, k):
        if k == 0:
            return 0
        self.n -= k
        v = self.acc >> self.n
        self.acc &= (1 << self.n) - 1
        return v


def _lzfse_freqs_v2(buf, off, counts):
    """Decode the variable-length frequency tables of a v2 header into one list of counts."""
    out, acc, nbits = [], 0, 0
    for total in counts:
        f = []
        for _ in range(total):
            while nbits < 14 + 8 and off < len(buf):
                acc |= buf[off] << nbits; nbits += 8; off += 1
            idx = acc & 31
            nb = LZFSE_FREQ_NBITS[idx]
            if nb == 8:
                v = ((acc >> 4) & 0xf) + 8
            elif nb == 14:
                v = ((acc >> 4) & 0x3ff) + 24
            else:
                v = LZFSE_FREQ_VALUE[idx]
            acc >>= nb; nbits -= nb
            f.append(v)
        out.append(f)
    return out


def _lzfse_block(buf, off, dst):
    """Decode one compressed v1/v2 block at buf[off:] (after the magic) into dst; return the offset past it."""
    magic = buf[off - 4:off]
    if magic == b'bvx2':
        n_raw, = struct.unpack_from('<I', buf, off)
        p0, p1, p2 = struct.unpack_from('<3Q', buf, off + 4)
        n_literals = p0 & 0xfffff
        n_literal_payload = (p0 >> 20) & 0xfffff
        n_matches = (p0 >> 40) & 0xfffff
        literal_bits = ((p0 >> 60) & 7) - 7
        lit_state = [(p1 >> s) & 0x3ff for s in (0, 10, 20, 30)]
        n_lmd_payload = (p1 >> 40) & 0xfffff
        lmd_bits = ((p1 >> 60) & 7) - 7
        header_size = p2 & 0xffffffff
        l_state, m_state, d_state = (p2 >> 32) & 0x3ff, (p2 >> 42) & 0x3ff, (p2 >> 52) & 0x3ff
        freqs = _lzfse_freqs_v2(buf, off + 4 + 24, (20, 20, 64, 256))
        hdr_end = off - 4 + header_size
    elif magic == b'bvx1':
        (n_raw, n_payload, n_literals, n_matches, n_literal_payload, n_lmd_payload, literal_bits) = struct.unpack_from('<7I', buf, off)
        literal_bits = struct.unpack_from('<i', buf, off + 24)[0]
        lit_state = list(struct.unpack_from('<4H', buf, off + 28))
        lmd_bits = struct.unpack_from('<i', buf, off + 36)[0]
        l_state, m_state, d_state = struct.unpack_from('<3H', buf, off + 40)
        q = off + 46
        fl = list(struct.unpack_from('<20H', buf, q)); q += 40
        fm = list(struct.unpack_from('<20H', buf, q)); q += 40
        fd = list(struct.unpack_from('<64H', buf, q)); q += 128
        fli = list(struct.unpack_from('<256H', buf, q)); q += 512
        freqs = [fl, fm, fd, fli]
        hdr_end = q
    else:
        raise ApfsError('lzfse: unknown block %r' % magic)
    fl, fm, fd, fli = freqs
    l_tab = _value_table(64, fl, LZFSE_L_BITS, LZFSE_L_BASE)
    m_tab = _value_table(64, fm, LZFSE_M_BITS, LZFSE_M_BASE)
    d_tab = _value_table(256, fd, LZFSE_D_BITS, LZFSE_D_BASE)
    lit_tab = _fse_table(1024, fli)
    # Literals: four interleaved FSE states over one backward bit stream.
    lit_start = hdr_end
    # The literal stream may refill from bytes before its payload (the header), as the reference decoder allows.
    bits = _BackBits(buf, 0, lit_start + n_literal_payload, literal_bits)
    literals = bytearray(n_literals)
    st4 = lit_state[:]
    for i in range(0, n_literals, 4):
        bits.fill()
        for j in range(4):
            k, delta, sym = lit_tab[st4[j]]
            st4[j] = delta + bits.pull(k)
            if i + j < n_literals:
                literals[i + j] = sym
    # L, M, D triples.
    lmd_start = lit_start + n_literal_payload
    bits = _BackBits(buf, lmd_start, lmd_start + n_lmd_payload, lmd_bits)
    lp, d = 0, -1
    for _ in range(n_matches):
        bits.fill()
        k, delta, vb, base = l_tab[l_state]; l_state = delta + bits.pull(k); L = base + bits.pull(vb)
        k, delta, vb, base = m_tab[m_state]; m_state = delta + bits.pull(k); M = base + bits.pull(vb)
        k, delta, vb, base = d_tab[d_state]; d_state = delta + bits.pull(k); D = base + bits.pull(vb)
        if D:
            d = D
        dst += literals[lp:lp + L]; lp += L
        if M:
            if d <= 0 or d > len(dst):
                raise ApfsError('lzfse: bad distance %d' % d)
            start = len(dst) - d
            if d >= M:
                dst += dst[start:start + M]
            else:
                for k2 in range(M):
                    dst.append(dst[start + k2])
    return lmd_start + n_lmd_payload


def lzfse_decode(src):
    dst = bytearray()
    off = 0
    while off + 4 <= len(src):
        magic = bytes(src[off:off + 4])
        if magic == b'bvx$':
            return dst
        if magic == b'bvx-':
            n, = struct.unpack_from('<I', src, off + 4)
            dst += src[off + 8:off + 8 + n]; off += 8 + n
        elif magic == b'bvxn':
            n_raw, n_payload = struct.unpack_from('<II', src, off + 4)
            dst += lzvn_decode(src[off + 12:off + 12 + n_payload]); off += 12 + n_payload
        elif magic in (b'bvx1', b'bvx2'):
            off = _lzfse_block(src, off + 4, dst)
        else:
            raise ApfsError('lzfse: bad block magic %r at %d' % (magic, off))
    raise ApfsError('lzfse: missing end of stream')


def decmpfs_chunk(ctype, chunk, want):
    """One compressed chunk of decmpfs type ctype, expanded to want bytes."""
    if ctype in (3, 4):
        if chunk[:1] and (chunk[0] & 0x0f) == 0x0f:
            return bytes(chunk[1:1 + want])
        return zlib.decompress(chunk)
    if ctype in (7, 8):
        if chunk[:1] == b'\x06':
            return bytes(chunk[1:1 + want])
        return bytes(lzvn_decode(chunk, limit=want))
    if ctype in (11, 12):
        if chunk[:1] == b'\xff':
            return bytes(chunk[1:1 + want])
        return bytes(lzfse_decode(chunk))
    if ctype in (9, 10):				# stored: one 0xcc marker byte, then the data
        return bytes(chunk[1:1 + want] if len(chunk) > want and chunk[:1] == b'\xcc' else chunk[:want])
    if ctype in (13, 14):
        if chunk[:1] == b'\xff':
            return bytes(chunk[1:1 + want])
        raise ApfsError('LZBITMAP (decmpfs type %d) is not decoded by this reader' % ctype)
    raise ApfsError('unknown decmpfs type %d' % ctype)


# ---------------------------------------------------------------------------------------------------- B-trees
class Node:
    __slots__ = ('flags', 'level', 'nkeys', 'entries', 'buf')

    def __init__(self, buf, fixed_kv=None):
        self.buf = buf
        self.flags, self.level, self.nkeys = struct.unpack_from('<HHI', buf, 0x20)
        toff, tlen = struct.unpack_from('<HH', buf, 0x28)
        key_base = 0x38 + toff + tlen
        val_end = len(buf) - (40 if self.flags & BTNODE_ROOT else 0)
        ents = []
        if self.flags & BTNODE_FIXED:
            ks, vs = fixed_kv
            if not self.flags & BTNODE_LEAF:
                vs = 8
            for i in range(self.nkeys):
                k, v = struct.unpack_from('<HH', buf, 0x38 + toff + 4 * i)
                ents.append((key_base + k, ks, val_end - v, vs))
        else:
            for i in range(self.nkeys):
                k, kl, v, vl = struct.unpack_from('<4H', buf, 0x38 + toff + 8 * i)
                ents.append((key_base + k, kl, val_end - v, vl))
        self.entries = ents

    def key(self, i):
        o, l, _, _ = self.entries[i]
        return self.buf[o:o + l]

    def val(self, i):
        _, _, o, l = self.entries[i]
        return self.buf[o:o + l]


class Container:
    def __init__(self, path):
        self.path = path
        self.f = open(path, 'rb')
        b0 = self._raw(0, 4096)
        if b0[32:36] != b'NXSB':
            raise ApfsError('%s: no APFS container superblock' % path)
        self.bs, = struct.unpack_from('<I', b0, 0x24)
        self.sb = self._latest_superblock(b0)
        sb = self.sb
        self.block_count, = struct.unpack_from('<Q', sb, 0x28)
        self.uuid = sb[0x48:0x58]
        self.xid, = struct.unpack_from('<Q', sb, 0x10)
        omap_oid, = struct.unpack_from('<Q', sb, 0xa0)
        self.omap_root = self._omap_root(omap_oid)
        self.volumes = []
        for idx, voloid in enumerate(struct.unpack_from('<100Q', sb, 0xb8)):
            if voloid:
                paddr = self.omap_lookup(self.omap_root, voloid, self.xid)
                self.volumes.append(Volume(self, self.block(paddr), idx))

    def _raw(self, off, n):
        self.f.seek(off)
        b = self.f.read(n)
        if len(b) != n:
            raise ApfsError('short read at %#x' % off)
        return b

    def block(self, paddr, n=1):
        return self._raw(paddr * self.bs, n * self.bs)

    def _latest_superblock(self, b0):
        """Newest valid NXSB in the checkpoint descriptor area (falls back to block 0)."""
        best = b0
        try:
            desc_blocks, = struct.unpack_from('<I', b0, 0x68)
            desc_base, = struct.unpack_from('<Q', b0, 0x70)
            if desc_blocks & 0x80000000:
                return b0				# a tree of descriptor blocks: not needed for restore images
            for i in range(desc_blocks):
                b = self.block(desc_base + i)
                if b[32:36] != b'NXSB' or struct.unpack_from('<Q', b, 0)[0] != fletcher64(b):
                    continue
                if struct.unpack_from('<Q', b, 0x10)[0] > struct.unpack_from('<Q', best, 0x10)[0]:
                    best = b
        except ApfsError:
            pass
        return best

    def _omap_root(self, omap_oid):
        om = self.block(omap_oid)
        if struct.unpack_from('<I', om, 0x18)[0] & OBJ_TYPE_MASK != T_OMAP:
            raise ApfsError('object %d is not an object map' % omap_oid)
        tree_oid, = struct.unpack_from('<Q', om, 0x30)
        return tree_oid

    def node(self, paddr, fixed_kv=None):
        b = self.block(paddr)
        t = struct.unpack_from('<I', b, 0x18)[0] & OBJ_TYPE_MASK
        # Sealed volumes store fs-tree nodes without an object header (BTNODE_NOHEADER; the parent holds their hash).
        if t not in (T_BTREE, T_BTNODE) and not (t == 0 and struct.unpack_from('<H', b, 0x20)[0] & BTNODE_NOHEADER):
            raise ApfsError('block %d is not a B-tree node (type %#x)' % (paddr, t))
        return Node(b, fixed_kv)

    def omap_lookup(self, tree, oid, xid):
        """Physical address of virtual object oid at the newest transaction <= xid."""
        n = self.node(tree, (16, 16))
        while True:
            best = None
            for i in range(n.nkeys):
                koid, kxid = struct.unpack('<QQ', n.key(i))
                if (koid, kxid) <= (oid, xid):
                    best = i
                else:
                    break
            if best is None:
                raise ApfsError('oid %d not in object map' % oid)
            if n.flags & BTNODE_LEAF:
                koid, kxid = struct.unpack('<QQ', n.key(best))
                if koid != oid:
                    raise ApfsError('oid %d not in object map' % oid)
                _, _, paddr = struct.unpack('<IIQ', n.val(best))
                return paddr
            child, = struct.unpack_from('<Q', n.val(best), 0)
            n = self.node(child, (16, 16))


class Inode:
    __slots__ = ('oid', 'parent', 'private_id', 'mtime', 'ctime', 'bsd_flags', 'uid', 'gid', 'mode', 'nlink',
                 'nchildren', 'size', 'xattrs', 'internal_flags')

    @property
    def is_dir(self):
        return st.S_ISDIR(self.mode)


class Volume:
    def __init__(self, c, sb, index):
        if sb[32:36] != b'APSB':
            raise ApfsError('volume %d: no APSB' % index)
        self.c, self.sb, self.index = c, sb, index
        self.incompat, = struct.unpack_from('<Q', sb, 0x38)
        self.root_tree_type, = struct.unpack_from('<I', sb, 0x74)
        omap_oid, root_tree, = struct.unpack_from('<QQ', sb, 0x80)
        self.name = sb[0x2c0:0x3c0].split(b'\0')[0].decode('utf-8', 'replace')
        self.uuid = sb[0xf0:0x100]
        self.role, = struct.unpack_from('<H', sb, 0x3c4)
        self.fext_tree, = struct.unpack_from('<Q', sb, 0x408)
        self.fext_tree_type, = struct.unpack_from('<I', sb, 0x410)
        self.num_files, self.num_dirs, self.num_symlinks = struct.unpack_from('<3Q', sb, 0xb8)
        self.xid, = struct.unpack_from('<Q', sb, 0x10)
        self.omap_root = c._omap_root(omap_oid)
        self.hashed = bool(self.incompat & (INCOMPAT_CASE_INSENSITIVE | INCOMPAT_NORM_INSENSITIVE))
        self.sealed = bool(self.incompat & INCOMPAT_SEALED)
        self.root_physical = bool(self.root_tree_type & OBJ_PHYSICAL)
        self.root_tree_oid = root_tree
        self.root_tree = root_tree if self.root_physical else c.omap_lookup(self.omap_root, root_tree, self.xid)
        self._node_cache = {}
        # A hashed (sealed) virtual fs tree stores child node oids relative to the tree's own root oid (observed on the
        # 24A446 cryptex images: the level-2 nodes 5068/5069 appear as 3779/3780 under root oid 1289).
        root = c.block(self.root_tree)
        bt_flags, = struct.unpack_from('<I', root, len(root) - 40)
        self.child_base = root_tree if (bt_flags & BTREE_HASHED and not self.root_physical) else 0

    @property
    def uuid_str(self):
        u = self.uuid.hex().upper()
        return '%s-%s-%s-%s-%s' % (u[:8], u[8:12], u[12:16], u[16:20], u[20:])

    # -- fs tree access
    def _child(self, oid):
        return oid if self.root_physical else self.c.omap_lookup(self.omap_root, oid + self.child_base, self.xid)

    def _fs_node(self, paddr):
        n = self._node_cache.get(paddr)
        if n is None:
            n = self.c.node(paddr)
            if len(self._node_cache) > 4096:
                self._node_cache.clear()
            self._node_cache[paddr] = n
        return n

    @staticmethod
    def _prefix(key):
        v, = struct.unpack_from('<Q', key, 0)
        return (v & 0x0fffffffffffffff, v >> 60)

    def records(self, oid, rtype=None):
        """All (type, key, value) leaf records of object oid (optionally of one record type), in key order."""
        lo = (oid, rtype if rtype is not None else 0)
        hi = (oid, rtype if rtype is not None else 15)
        out = []
        self._collect(self.root_tree, lo, hi, out)
        return out

    def _collect(self, paddr, lo, hi, out):
        n = self._fs_node(paddr)
        if n.flags & BTNODE_LEAF:
            for i in range(n.nkeys):
                k = n.key(i)
                p = self._prefix(k)
                if lo <= p <= hi:
                    out.append((p[1], k, n.val(i)))
            return
        keys = [self._prefix(n.key(i)) for i in range(n.nkeys)]
        for i in range(n.nkeys):
            nxt = keys[i + 1] if i + 1 < n.nkeys else None
            if keys[i] > hi:
                break
            if nxt is not None and nxt < lo:
                continue
            child, = struct.unpack_from('<Q', n.val(i), 0)
            self._collect(self._child(child), lo, hi, out)

    def walk_leaves(self, paddr=None):
        """Every fs-tree leaf record in key order (one pass over the whole tree)."""
        n = self._fs_node(self.root_tree if paddr is None else paddr)
        if n.flags & BTNODE_LEAF:
            for i in range(n.nkeys):
                k = n.key(i)
                yield self._prefix(k), k, n.val(i)
            return
        for i in range(n.nkeys):
            child, = struct.unpack_from('<Q', n.val(i), 0)
            yield from self.walk_leaves(self._child(child))

    # -- record decoding
    def _drec(self, k, v):
        if self.hashed:
            nlh, = struct.unpack_from('<I', k, 8)
            name = k[12:12 + (nlh & 0x3ff)]
        else:
            nl, = struct.unpack_from('<H', k, 8)
            name = k[10:10 + nl]
        file_id, _, flags = struct.unpack_from('<QQH', v, 0)
        return name.rstrip(b'\0').decode('utf-8', 'surrogateescape'), file_id, flags & 0xf

    @staticmethod
    def _xattr(k, v):
        nl, = struct.unpack_from('<H', k, 8)
        name = k[10:10 + nl].rstrip(b'\0').decode('utf-8', 'surrogateescape')
        flags, dlen = struct.unpack_from('<HH', v, 0)
        return name, flags, v[4:4 + dlen]

    def _inode_from(self, oid, v):
        ino = Inode()
        ino.oid = oid
        (ino.parent, ino.private_id, _, ino.mtime, ino.ctime, _, ino.internal_flags, nkids, _, _, ino.bsd_flags,
         ino.uid, ino.gid, ino.mode) = struct.unpack_from('<QQQQQQQiIIIIIH', v, 0)
        ino.nlink = ino.nchildren = nkids
        ino.size = 0
        ino.xattrs = {}
        if len(v) > 0x5c:
            nexts, _ = struct.unpack_from('<HH', v, 0x5c)
            hdr = 0x60
            data = hdr + 4 * nexts
            for i in range(nexts):
                xtype, _, xsize = struct.unpack_from('<BBH', v, hdr + 4 * i)
                if xtype == INO_EXT_DSTREAM:
                    ino.size, = struct.unpack_from('<Q', v, data)
                data += (xsize + 7) & ~7
        return ino

    def inode(self, oid):
        recs = self.records(oid)
        ino = None
        for t, k, v in recs:
            if t == J_INODE:
                ino = self._inode_from(oid, v)
        if ino is None:
            raise ApfsError('no inode %d' % oid)
        for t, k, v in recs:
            if t == J_XATTR:
                name, flags, data = self._xattr(k, v)
                ino.xattrs[name] = (flags, data)
        self._fix_size(ino)
        return ino

    def _fix_size(self, ino):
        if ino.bsd_flags & UF_COMPRESSED and 'com.apple.decmpfs' in ino.xattrs:
            hdr = self.xattr_data(ino, 'com.apple.decmpfs')
            if hdr[:4] == b'fpmc':
                ino.size, = struct.unpack_from('<Q', hdr, 8)
        elif st.S_ISLNK(ino.mode) and 'com.apple.fs.symlink' in ino.xattrs:
            ino.size = len(self.xattr_data(ino, 'com.apple.fs.symlink').rstrip(b'\0'))
        elif st.S_ISDIR(ino.mode):
            ino.size = 32 * (ino.nchildren + 2)		# what stat(2) reports for an APFS directory

    def readdir(self, oid):
        out = []
        for t, k, v in self.records(oid, J_DREC):
            out.append(self._drec(k, v))
        return out

    def lookup(self, path):
        oid = 2					# root directory
        for part in [p for p in path.split('/') if p]:
            for name, fid, _ in self.readdir(oid):
                if name == part:
                    oid = fid
                    break
            else:
                raise FileNotFoundError(path)
        return oid

    def resolve(self, path, depth=0):
        """Inode of path, following symlinks inside the volume (absolute targets are taken relative to its root)."""
        parts = [p for p in path.split('/') if p]
        oid, walked = 2, []
        for i, part in enumerate(parts):
            if part == '..':
                walked = walked[:-1]
                oid = self.lookup('/'.join(walked)); continue
            for name, fid, dt in self.readdir(oid):
                if name == part:
                    break
            else:
                raise FileNotFoundError(path)
            ino = self.inode(fid)
            if st.S_ISLNK(ino.mode) and (i + 1 < len(parts) or depth >= 0):
                if depth > 32:
                    raise ApfsError('symlink loop at %s' % path)
                target = self.readlink(ino)
                base = '' if target.startswith('/') else '/'.join(walked)
                return self.resolve(base + '/' + target + '/' + '/'.join(parts[i + 1:]), depth + 1)
            oid = fid
            walked.append(part)
        return self.inode(oid)

    def xattr_data(self, ino, name):
        flags, data = ino.xattrs[name]
        if flags & XATTR_EMBEDDED:
            return data
        stream_oid, size = struct.unpack_from('<QQ', data, 0)
        return self._read_stream(stream_oid, size)

    def readlink(self, ino):
        return self.xattr_data(ino, 'com.apple.fs.symlink').rstrip(b'\0').decode('utf-8', 'surrogateescape')

    def _extents(self, stream_id):
        """[(logical, length, physical block)] of a data stream."""
        exts = []
        if self.fext_tree:
            root = self.fext_tree
            self._fext_collect(root, stream_id, exts)
        else:
            for t, k, v in self.records(stream_id, J_EXTENT):
                logical, = struct.unpack_from('<Q', k, 8)
                lf, pb = struct.unpack_from('<QQ', v, 0)
                exts.append((logical, lf & 0x00ffffffffffffff, pb))
        exts.sort()
        return exts

    def _fext_collect(self, paddr, sid, out):
        n = self.c.node(paddr, (16, 16))
        if n.flags & BTNODE_LEAF:
            for i in range(n.nkeys):
                pid, logical = struct.unpack('<QQ', n.key(i))
                if pid == sid:
                    lf, pb = struct.unpack_from('<QQ', n.val(i), 0)
                    out.append((logical, lf & 0x00ffffffffffffff, pb))
            return
        keys = [struct.unpack_from('<Q', n.key(i), 0)[0] for i in range(n.nkeys)]
        for i in range(n.nkeys):
            if keys[i] > sid:
                break
            if i + 1 < n.nkeys and keys[i + 1] < sid:
                continue
            child, = struct.unpack_from('<Q', n.val(i), 0)
            self._fext_collect(child, sid, out)

    def _read_stream(self, stream_id, size):
        out = bytearray()
        bs = self.c.bs
        for logical, length, pb in self._extents(stream_id):
            if logical > len(out):
                out += bytes(logical - len(out))
            if pb == 0:
                out += bytes(length)
            else:
                out += self.c._raw(pb * bs, length)
            if len(out) >= size:
                break
        if len(out) < size:
            out += bytes(size - len(out))
        return bytes(out[:size])

    def read_inode(self, ino):
        if st.S_ISLNK(ino.mode):
            return self.readlink(ino).encode()
        if ino.bsd_flags & UF_COMPRESSED and 'com.apple.decmpfs' in ino.xattrs:
            return self._read_compressed(ino)
        dsize = ino.size
        return self._read_stream(ino.private_id, dsize)

    def compression_type(self, ino):
        if ino.bsd_flags & UF_COMPRESSED and 'com.apple.decmpfs' in ino.xattrs:
            return struct.unpack_from('<I', self.xattr_data(ino, 'com.apple.decmpfs'), 4)[0]
        return 0

    def _read_compressed(self, ino):
        hdr = self.xattr_data(ino, 'com.apple.decmpfs')
        magic, ctype, usize = struct.unpack_from('<4sIQ', hdr, 0)
        if magic != b'fpmc':
            raise ApfsError('bad decmpfs header')
        if ctype in (3, 7, 9, 11, 13):			# inline in the xattr
            return decmpfs_chunk(ctype, hdr[16:], usize)[:usize]
        rsrc = self.xattr_data(ino, 'com.apple.ResourceFork')
        out = bytearray()
        if ctype == 4:					# zlib in a classic resource fork
            data_off, = struct.unpack_from('>I', rsrc, 0)
            base = data_off + 4
            count, = struct.unpack_from('<I', rsrc, base)
            for i in range(count):
                o, l = struct.unpack_from('<II', rsrc, base + 4 + 8 * i)
                out += decmpfs_chunk(4, rsrc[base + o:base + o + l], min(65536, usize - len(out)))
        elif ctype in (8, 10, 12, 14):			# u32 chunk offset table, 64 KiB chunks
            nchunks = (usize + 65535) // 65536
            offs = struct.unpack_from('<%dI' % (nchunks + 1), rsrc, 0)
            for i in range(nchunks):
                out += decmpfs_chunk(ctype, rsrc[offs[i]:offs[i + 1]], min(65536, usize - len(out)))
        else:
            raise ApfsError('unsupported decmpfs type %d' % ctype)
        return bytes(out[:usize])

    def read(self, path, follow=True):
        ino = self.resolve(path, 0 if follow else -1) if follow else self.inode(self.lookup(path))
        return self.read_inode(ino)

    # -- whole-volume listing
    def listing(self):
        """Yield (path, Inode-like) for every object, from one pass over the fs tree."""
        inodes, xattrs, children = {}, {}, {}
        for (oid, t), k, v in self.walk_leaves():
            if t == J_INODE:
                inodes[oid] = self._inode_from(oid, v)
            elif t == J_XATTR:
                name, flags, data = self._xattr(k, v)
                if name in ('com.apple.decmpfs', 'com.apple.fs.symlink'):
                    xattrs.setdefault(oid, {})[name] = (flags, data)
            elif t == J_DREC:
                name, fid, _ = self._drec(k, v)
                children.setdefault(oid, []).append((name, fid))
        for oid, xs in xattrs.items():
            if oid in inodes:
                inodes[oid].xattrs = xs
        stack = [('.', 2)]
        while stack:
            path, oid = stack.pop()
            ino = inodes.get(oid)
            if ino is None:
                continue
            self._fix_size(ino)
            yield path, ino
            for name, fid in sorted(children.get(oid, ()), reverse=True):
                stack.append((path + '/' + name, fid))


def mode_string(mode):
    kinds = {st.S_IFDIR: 'd', st.S_IFLNK: 'l', st.S_IFREG: '-', st.S_IFCHR: 'c', st.S_IFBLK: 'b', st.S_IFIFO: 'p',
             st.S_IFSOCK: 's'}
    s = kinds.get(st.S_IFMT(mode), '?')
    for who in range(3):
        bits = (mode >> (6 - 3 * who)) & 7
        s += ('r' if bits & 4 else '-') + ('w' if bits & 2 else '-')
        special = mode & (st.S_ISUID, st.S_ISGID, st.S_ISVTX)[who]
        x = bits & 1
        s += ('s' if x else 'S') if special and who < 2 else ('t' if x else 'T') if special else ('x' if x else '-')
    return s


def list_line(vol, path, ino):
    target = vol.readlink(ino) if st.S_ISLNK(ino.mode) else ''
    return '%s %d:%d %d %s -> %s' % (mode_string(ino.mode), ino.uid, ino.gid, ino.size, path, target)


def extract(vol, path, outdir, meta):
    """Copy path (file or subtree) into outdir; record 'mode uid:gid relpath' per object in meta."""
    oid = vol.lookup(path)
    stack = [(path.strip('/'), oid)]
    n = 0
    while stack:
        rel, oid = stack.pop()
        ino = vol.inode(oid)
        dest = os.path.join(outdir, rel)
        meta.write('%o\t%d:%d\t%s\n' % (ino.mode, ino.uid, ino.gid, rel))
        if ino.is_dir:
            os.makedirs(dest, exist_ok=True)
            for name, fid, _ in vol.readdir(oid):
                stack.append((rel + '/' + name, fid))
        elif st.S_ISLNK(ino.mode):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if os.path.lexists(dest):
                os.unlink(dest)
            os.symlink(vol.readlink(ino), dest)
        elif st.S_ISREG(ino.mode):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, 'wb') as f:
                f.write(vol.read_inode(ino))
            os.chmod(dest, (ino.mode & 0o755) | 0o600)
        n += 1
    return n


def main(argv):
    if len(argv) < 3:
        sys.exit(__doc__)
    c = Container(argv[1])
    cmd, rest = argv[2], argv[3:]
    vi = 0
    if '--vol' in rest:
        vi = int(rest[rest.index('--vol') + 1]); del rest[rest.index('--vol'):rest.index('--vol') + 2]
    vol = c.volumes[vi]
    if cmd == 'info':
        print('container block_size=%d blocks=%d xid=%d volumes=%d' % (c.bs, c.block_count, c.xid, len(c.volumes)))
        for v in c.volumes:
            print('volume %d name=%r role=%s uuid=%s sealed=%d hashed=%d root_physical=%d fext=%d files=%d dirs=%d '
                  'symlinks=%d incompat=%#x' % (v.index, v.name, ROLE_NAMES.get(v.role, hex(v.role)), v.uuid_str,
                                                v.sealed, v.hashed, v.root_physical, bool(v.fext_tree), v.num_files,
                                                v.num_dirs, v.num_symlinks, v.incompat))
    elif cmd == 'ls':
        for name, fid, dt in sorted(vol.readdir(vol.lookup(rest[0] if rest else '/'))):
            ino = vol.inode(fid)
            print(list_line(vol, name, ino))
    elif cmd == 'stat':
        ino = vol.inode(vol.lookup(rest[0]))
        print(list_line(vol, rest[0], ino), 'compression=%d bsd_flags=%#x nlink=%d' %
              (vol.compression_type(ino), ino.bsd_flags, ino.nlink))
    elif cmd == 'cat':
        data = vol.read(rest[0])
        if len(rest) > 1:
            open(rest[1], 'wb').write(data)
        else:
            sys.stdout.buffer.write(data)
    elif cmd == 'list':
        out = sys.stdout
        for path, ino in vol.listing():
            out.write(list_line(vol, path, ino) + '\n')
    elif cmd == 'extract':
        outdir = rest[1]
        os.makedirs(outdir, exist_ok=True)
        with open(outdir.rstrip('/') + '.meta.tsv', 'a') as meta:
            print('%d objects' % extract(vol, rest[0], outdir, meta))
    else:
        sys.exit(__doc__)


if __name__ == '__main__':
    main(sys.argv)
