#!/usr/bin/env python3
"""Linux checks for the build-time Mesa/metal2vulkan patch files (no network, no sources needed).

Every patch must be a well-formed unified diff (hunk line counts match), ported ones must carry their
origin and licence, and the build recipes must actually list them.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HUNK = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')


def check_hunks(path):
    lines = path.read_text().split('\n')
    i, hunks = 0, 0
    while i < len(lines):
        m = HUNK.match(lines[i])
        if not m:
            i += 1
            continue
        old, new = int(m.group(2) or 1), int(m.group(4) or 1)
        i += 1
        while (old or new) and i < len(lines):
            c = lines[i][:1]
            if c == ' ' or (c == '' and not lines[i]):
                old, new = old - 1, new - 1
            elif c == '-':
                old -= 1
            elif c == '+':
                new -= 1
            elif c == '\\':
                pass
            else:
                break
            i += 1
        assert old == 0 and new == 0, f'{path.name}: hunk line counts do not match near line {i}'
        hunks += 1
    assert hunks, f'{path.name}: no hunks'
    return hunks


def main():
    patches = sorted((ROOT/'tools/patches').glob('*.patch'))
    assert patches
    for p in patches:
        check_hunks(p)
    draw = sorted((ROOT/'tools/patches').glob('metal2vulkan-adrenometal-*.patch'))
    assert [p.name[25:29] for p in draw] == ['0009', '0010', '0011', '0012', '0013', '0014', '0015'], draw
    for p in draw:
        text = p.read_text()
        head = text[:text.index('diff --git')]
        assert 'AdrenoMetal bridge/patches/' + p.name[len('metal2vulkan-adrenometal-'):] in head, p.name
        assert 'MIT License' in head and 'ce5e19e33e05e1873d38d818e23cabfe0ac708c3' in head, p.name
        assert all(f.startswith('crates/metal2vulkan') for f in re.findall(r'^\+\+\+ b/(\S+)', text, re.M) if f.endswith('.rs')), p.name
    nm = (ROOT/'tools/build-fuxi-nativemetal.py').read_text()
    assert "metal2vulkan-adrenometal-*.patch" in nm and 'M2V_DRAW_PATCHES' in nm
    assert nm.count('M2V_DRAW_PATCHES') == 3, 'stack, archive and definition must all use the draw patches'
    # The fallback translator checkout stays unpatched.
    assert 'patch' not in (ROOT/'tools/build-fuxi-metal-translator.sh').read_text()
    fence = ROOT/'tools/patches/mesa-fuxi-fence-wait-100ms.patch'
    text = fence.read_text()
    assert 'BSD-2-Clause-Patent' in text and '+      uint64_t slice = 100000000; /* 100 ms */' in text
    turnip = (ROOT/'tools/build-fuxi-turnip.py').read_text()
    order = [turnip.index(f"'tools/patches/{n}'") for n in ('mesa-fuxi-smoke.patch', 'mesa-fuxi-hostimport.patch',
             'mesa-fuxi-ir3-round-default.patch', 'mesa-fuxi-fence-wait-100ms.patch', 'mesa-fuxi-cached-coherent.patch',
             'mesa-fuxi-bo-cache.patch')]
    assert order == sorted(order), 'the 100 ms slice patch must apply after mesa-fuxi-smoke.patch, then cached-coherent, the BO cache last'
    cached = (ROOT/'tools/patches/mesa-fuxi-cached-coherent.patch').read_text()
    assert all(s in cached for s in ('NativeAdrenoAllocCachedCoherent', '.writeback = cached ? 1u : 0u', '.shareability = cached ? 3u : 2u',
               'bo->darwin_cached ? kIOMapDefaultCache : kIOMapWriteCombineCache')), 'cached-coherent BO flags'
    # UniXNU c7353ea/83cce1d/caffd5d/b624df5: origin and licence kept, a cached BO only goes back to an allocation with the same kernel
    # cache attributes, no ABI flag our kernel lacks, and the idle/ring-space waits stay bounded (our watchdog makes WaitFence time out).
    cache = (ROOT/'tools/patches/mesa-fuxi-bo-cache.patch').read_text()
    head, body = cache[:cache.index('\n--- a/')], cache[cache.index('\n--- a/'):]
    assert 'BSD-2-Clause-Patent' in head and all(c in head for c in ('c7353ea', '83cce1d', 'caffd5d', 'b624df5')), 'BO cache origin'
    assert all(s in body for s in ('+   bool read_only, cached;', 'e->cached == cached', 'bo->darwin_read_only, bo->darwin_cached};',
               '.contiguous = cached ? (uint32_t)NativeAdrenoAllocCachedCoherent : 0u};',
               '+         .writeback = cached ? 1u : 0u,', 'DISPATCH_SOURCE_TYPE_MEMORYPRESSURE', 'now - retire->cache[i].freed_s > 1')), 'BO cache'
    assert not any(s in body for s in ('NativeAdrenoParamsOwnedCoherent', 'NativeAdrenoParamsPoolPressure', 'kIOMapCopybackCache',
                                       "the kernel's hangcheck", 'waiting for ring space')), 'BO cache: UniXNU-only ABI or unbounded wait'
    assert 'NativeAdrenoParamsOwnedCoherent' not in (ROOT/'xnu/fuxi-adreno/NativeAdrenoABI.h').read_text()
    abi = (ROOT/'xnu/fuxi-adreno/NativeAdrenoABI.h').read_text()
    assert 'NativeAdrenoAllocCachedCoherent = 2' in abi
    print(f'PASS: {len(patches)} patch files well formed; draw-path patches 0009-0015 carry origin/licence; build recipes list them')


if __name__ == '__main__':
    sys.exit(main())
