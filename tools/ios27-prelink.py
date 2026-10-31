#!/usr/bin/env python3
"""K2: prelink the original Apple kexts of one iOS 27 kernelcache into the Fuxi kernel (docs-lanes/ios27.md#k2).

  ios27-prelink.py --kernel NATIVE --kernelcache KC --out DIR [--reference RELEASE] [--allow-missing]

NATIVE is the unstripped DEVELOPMENT kernel linked with split-segment info (CI
kernel=ios27: mach.development.fuxi.chained.unstripped). KC is the decompressed
MH_FILESET kernelcache whose kexts are kept; it is read, never copied into git.
RELEASE (optional, CI: mach.release.fuxi.unstripped) is the symbol-bearing LTO
comparison build that XnuPrelink's matcher uses to name the functions the
stripped Apple kernel calls (--fileset-reference); without it the matcher runs
on the native kernel alone and says so.

The roots are UniXNU's ipados-27.0.1-24A446 profile list (31 bundles, kept by
default: AMFI, AppleImage4, sandbox, corecrypto, apfs and their families need
the platform kexts they link against, and their dependency closure pulls most of
them in anyway). --drop-soc removes the nine SoC hardware roots (ApplePMGR,
scaler, AV/HDCP/CEC, IIS, audio, clocking); no trouble from keeping them has
been shown yet. AMFI's original policy initializer runs before lockdown through
native-security-init (--early-security-init, the profile's address).

Outputs in DIR (refused if kernel.prelinked exists, unless --force):
  kernel.prelinked   the MH_FILESET (tools/mkpayload.py --prelinked packs it)
  kernel.json        XnuPrelink's report (closure, statistics, stubbed/missing per kext)
  kernel.map.json    the matcher's pairing, reused while inputs and matcher match
  summary.json       per-root kernel references: matched, stubbed, missing, with
                     neighbour-name hints for unmatched targets (hints are guesses)
  checks.json        structural checks of the image (UniXNU check-prelink-xml rules,
                     flat root, entries, entry point, fixups)
Preflight (before the minute-long matcher) refuses a native kernel without
split-segment info, the stub and reviewed-site symbols, the
_NativePrelinkEarlySecurityInit hook, or whose segments do not fit around the
retained kexts. --trial continues past preflight failures (counts only, never
for a boot; with a --kit copy that bypasses them).
"""
import argparse
import bisect
from collections import defaultdict
import hashlib
import json
import plistlib
import re
import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / 'tools/xnuprelink'
LINK = 0xfffffff007004000
# UniXNU 88604d0 profiles/os/ipados-27.0.1-24A446/profile.toml, [kernel.prelink]
PROFILE_KERNELCACHE_SHA256 = '38e6d9fa5f789b8228f4617ef4c6d2a3fd8ab564abdc9b62920f1db53fc1780a'
EARLY_SECURITY_INIT = 'com.apple.driver.AppleMobileFileIntegrity=0xfffffe00092551ac'
ROOTS = [
    'com.apple.AppleFSCompression.AppleFSCompressionTypeZlib', 'com.apple.driver.AppleARMPlatform',
    'com.apple.driver.AppleARMWatchdogTimer', 'com.apple.driver.AppleAudioClockLibs',
    'com.apple.driver.AppleEffaceableStorage', 'com.apple.driver.AppleIISController',
    'com.apple.driver.AppleM2ScalerCSCDriver', 'com.apple.driver.AppleMobileFileIntegrity',
    'com.apple.driver.ApplePMGR', 'com.apple.driver.IODARTFamily', 'com.apple.filesystems.apfs',
    'com.apple.iokit.CoreAnalyticsFamily', 'com.apple.iokit.IOAVFamily', 'com.apple.iokit.IOAudio2Family',
    'com.apple.iokit.IOCECFamily', 'com.apple.iokit.IOCryptoAcceleratorFamily', 'com.apple.iokit.IOGPUFamily',
    'com.apple.iokit.IOHDCPFamily', 'com.apple.iokit.IOMobileGraphicsFamily', 'com.apple.iokit.IOReportFamily',
    'com.apple.iokit.IOSlowAdaptiveClockingFamily', 'com.apple.iokit.IOStorageFamily', 'com.apple.iokit.IOSurface',
    'com.apple.kec.Libm', 'com.apple.kec.corecrypto', 'com.apple.kext.AppleMatch', 'com.apple.kext.CoreTrust',
    'com.apple.security.AppleImage4', 'com.apple.security.sandbox', 'com.apple.iokit.IOHIDFamily',
    'com.apple.iokit.IOHIDEventDriver',
]
SOC_ROOTS = [
    'com.apple.driver.ApplePMGR', 'com.apple.driver.AppleM2ScalerCSCDriver', 'com.apple.iokit.IOAVFamily',
    'com.apple.iokit.IOHDCPFamily', 'com.apple.iokit.IOCECFamily', 'com.apple.driver.AppleIISController',
    'com.apple.iokit.IOAudio2Family', 'com.apple.driver.AppleAudioClockLibs',
    'com.apple.iokit.IOSlowAdaptiveClockingFamily',
]
STUB = '_unixnu_fileset_unmapped'
HOOK = b'_NativePrelinkEarlySecurityInit'
LC_SEGMENT_SPLIT_INFO, LC_UNIXTHREAD, LC_DYLD_CHAINED_FIXUPS = 0x1e, 0x5, 0x80000034
CODELESS = 0x7fffffffffffffff
TAG_MAX_LENGTH = 32     # OSUnserializeXMLSharedImplementation.h: getTag() keeps < 32 bytes of a tag name


def load_kit(kit):
    sys.path.insert(0, str(kit))
    import kc_image, fileset_emit, fileset_layout, fileset_match, fileset_segmented  # noqa: E401
    return kc_image, fileset_emit, fileset_layout, fileset_match, fileset_segmented


def parse_initializers(items):
    result = {}
    for item in items:
        bundle, sep, address = item.partition('=')
        if not sep or not bundle or bundle in result:
            raise SystemExit(f'--early-security-init {item!r}: use one BUNDLE=ORIGINAL_VA per bundle')
        result[bundle] = int(address, 0)
    return result


def preflight(native_data, cache_data, roots, initializers, allow_missing, kit, default_initializer):
    """Everything the emitter would trip over later, collected; returns (failures, facts)."""
    kc_image, fileset_emit, fileset_layout = kit[:3]
    failures, facts = [], {}
    native, cache = kc_image.Image(native_data), kc_image.Image(cache_data)
    cache_sha = hashlib.sha256(cache_data).hexdigest()
    facts['kernelcache_sha256'] = cache_sha
    facts['native_sha256'] = hashlib.sha256(native_data).hexdigest()
    if native.kind != 2 or struct.unpack_from('<I', native_data, 8)[0] & 0xff != 2:
        failures.append('native kernel is not an arm64e MH_EXECUTE')
    if native.base != LINK:
        failures.append(f'native kernel is linked at {native.base:#x}, not {LINK:#x}')
    if cache.kind != 12 or 'com.apple.kernel' not in cache.entries:
        failures.append('kernelcache is not an MH_FILESET with a com.apple.kernel entry')
        return failures, facts
    if default_initializer and cache_sha != PROFILE_KERNELCACHE_SHA256:
        failures.append('kernelcache is not the 24A446 j820 one the profile AMFI address is keyed to; '
                        'pass --early-security-init')
    if not native.command(LC_SEGMENT_SPLIT_INFO):
        failures.append('native kernel has no LC_SEGMENT_SPLIT_INFO (link with XNU_KERNEL_CHAINED_FIXUPS=1: '
                        'CI mach.development.fuxi.chained.unstripped)')
    names = {name for found in native.symbols().values() for name in found}
    need = {STUB} if allow_missing else set()
    for fixture in sorted((Path(kit[1].__file__).parent / 'fixtures').glob('fileset-*-symbols.json')):
        record = json.loads(fixture.read_text())
        if record['inputs']['kernelcache_sha256'] == cache_sha:
            need |= {site['native_symbol'] for site in record.get('sites', [])}
            reviewed = [f['native_symbol'] for f in record.get('functions', [])]
            facts['reviewed_functions_absent'] = sorted(set(reviewed) - names)
    for name in sorted(need - names):
        failures.append(f'native kernel lacks {name}')
    if HOOK not in native_data:
        failures.append('native kernel lacks the _NativePrelinkEarlySecurityInit hook (native-security-init.patch)')
    try:
        closure = fileset_emit.dependency_closure(cache, roots)
        facts['closure'] = len(closure)
        for bundle, address in initializers.items():
            entry = kc_image.Image(cache.data, cache.entries[bundle][1])
            if not any(s['name'] == '__TEXT_EXEC' and s['va'] <= address < s['va'] + s['size'] for s in entry.segments):
                failures.append(f'early security initializer {address:#x} is outside {bundle} code')
        rebased, _, _ = fileset_emit.rebase_collection(cache, native.base, closure, initializers, set(roots))
        layout = fileset_layout.Layout(rebased, native, closure)
        facts['layout'] = layout.describe()
    except (ValueError, KeyError) as e:
        failures.append(f'layout: {e}')
    data = next((s for s in native.segments if s['name'] == '__DATA'), None)
    facts['native_data_size'] = hex(data['size']) if data else None
    return failures, facts


def matching(kit, cache_data, native_data, reference_data, map_path):
    """XnuPrelink's pairing, reused when it was made for these inputs by this matcher (as emit_prelink.py does)."""
    _, fileset_emit, _, fileset_match, _ = kit
    reference_sha = hashlib.sha256(reference_data).hexdigest() if reference_data is not None else None
    if map_path.is_file():
        cached = json.loads(map_path.read_text())
        try:
            fileset_emit.validate_map(cache_data, native_data, cached)
            if (cached.get('matcher_version') == fileset_match.MATCHER_VERSION
                    and cached.get('matcher_signature') == fileset_match.matcher_signature()
                    and cached.get('reference_sha256') == reference_sha):
                print(f'matcher: reusing {map_path.name}')
                return cached
        except ValueError:
            pass
    started = time.monotonic()
    pairing = fileset_match.match_images(cache_data, native_data, reference_data=reference_data)
    fileset_emit.atomic_write(map_path, (json.dumps(pairing, indent=2) + '\n').encode())
    print(f'matcher: {time.monotonic() - started:.0f} s')
    return pairing


def emit(kit, cache_data, native_data, pairing, roots, initializers, allow_missing):
    """emit_images with every kernel-facing reference recorded per owner (no behaviour change)."""
    segmented = kit[4]
    seen = defaultdict(set)
    original = segmented.SegmentedEmit.rebind

    def rebind(self, target, site):
        if self.mapped(target):
            seen[self.owner(site)].add(target)
        return original(self, target, site)

    segmented.SegmentedEmit.rebind = rebind
    try:
        image, report = kit[1].emit_images(cache_data, native_data, pairing, roots,
                                           stub_symbol=STUB if allow_missing else None,
                                           early_security_init=initializers or None)
    finally:
        segmented.SegmentedEmit.rebind = original
    return image, report, seen


def summarize(kit, cache_data, report, pairing, seen, roots):
    """Per root bundle: distinct original-kernel targets it references, and how each was bound."""
    kc_image = kit[0]
    slide = int(report['collection_slide'], 16)
    mask = (1 << 64) - 1
    cache = kc_image.Image(cache_data)
    kernel = kc_image.Image(cache_data, cache.entries['com.apple.kernel'][1])
    sections = [(s['va'], s['va'] + s['size'], f"{s['segment']},{s['name']}") for s in kernel.sections if s['size']]
    # Hints name the nearest matched neighbours of the same kind (code next to code, data next to data)
    # in the original kernel: where an unmatched target sits, not what it is (a guess to steer review).
    neighbours = {}
    for kind in ('functions', 'data'):
        named = sorted((int(k, 16), v['name']) for k, v in pairing[kind].items() if v.get('name'))
        neighbours[kind] = ([a for a, _ in named], named)

    def where(original):
        section = next((name for b, e, name in sections if b <= original < e), '?')
        starts, named = neighbours['functions' if 'EXEC' in section else 'data']
        i = bisect.bisect_right(starts, original) - 1
        hint = None
        if i >= 0:
            below = f'{named[i][1]}+{original - named[i][0]:#x}'
            above = named[i + 1][1] if i + 1 < len(named) else None
            hint = f'after {below}' + (f', before {above}' if above else '')
        return section, hint

    per_root = {}
    for bundle in sorted(set(report.get('closure') or ()) | set(roots)):
        targets = seen.get(bundle, set())
        stubbed = {int(k, 16) for k in report['stubbed'].get(bundle, {})}
        missing = {int(k, 16) for k in report['missing'].get(bundle, {})}
        unmatched = []
        for target in sorted(stubbed | missing):
            original = (target - slide) & mask
            section, hint = where(original)
            unmatched.append({'original': hex(original), 'kind': 'stubbed' if target in stubbed else 'missing',
                              'section': section, 'neighbour_hint_guess': hint})
        per_root[bundle] = {'root': bundle in roots, 'kernel_targets': len(targets | stubbed | missing),
                            'matched': len(targets - stubbed - missing), 'stubbed': len(stubbed),
                            'missing': len(missing), 'unmatched': unmatched}
    return per_root


def xml_rules(raw):
    """UniXNU check-prelink-xml.py's question (does XNU's tag/string lexer accept the metadata?), answered by
    the lexer's rules rather than by compiling Apple's lexer: tag names under TAG_MAX_LENGTH, booleans only as
    empty tags, every key/string body free of '<', and the document a well-formed plist."""
    problems = []
    for m in re.finditer(rb'<(/?)([A-Za-z]+)([^>]*)>', raw):
        name = m.group(2)
        if len(name) >= TAG_MAX_LENGTH:
            problems.append(f'tag too long at {m.start()}')
        if name in (b'true', b'false') and not m.group(3).rstrip().endswith(b'/'):
            problems.append(f'non-empty <{name.decode()}> at {m.start()}')
    for m in re.finditer(rb'<(key|string)>(.*?)</\1>', raw, re.S):
        if b'<' in m.group(2):
            problems.append(f'markup inside <{m.group(1).decode()}> at {m.start()}')
    return problems


def checks(kit, image, report):
    kc_image = kit[0]
    result = {}

    def check(name, ok, detail=''):
        result[name] = {'ok': bool(ok), 'detail': detail}

    root = kc_image.Image(image)
    check('root_fileset_arm64e', root.kind == 12 and struct.unpack_from('<I', image, 8)[0] & 0xff == 2)
    text = root.segments[0]
    check('root_text_at_link_address', text['name'] == '__TEXT' and text['fileoff'] == 0 and text['va'] == LINK,
          hex(text['va']))
    bad = [s['name'] for s in root.segments
           if s['filesize'] and (s['va'] - LINK != s['fileoff'] or s['fileoff'] + s['filesize'] > len(image))]
    check('root_flat', not bad, ','.join(bad))
    ordered = sorted((s['va'], s['va'] + s['size']) for s in root.segments if s['size'])
    check('root_segments_disjoint', all(a[1] <= b[0] for a, b in zip(ordered, ordered[1:])))
    closure = set(report.get('closure') or ())
    extra = sorted(set(root.entries) - closure - {'com.apple.kernel'})
    check('entries_within_closure', 'com.apple.kernel' in root.entries and not extra,
          f'{len(root.entries)} entries' + (f'; not in closure: {",".join(extra[:8])}' if extra else ''))
    outside = []
    for name, (_, at) in root.entries.items():
        for s in kc_image.Image(image, at).segments:
            if s['size'] and s['name'] != '__LINKEDIT' and not any(b <= s['va'] and s['va'] + s['size'] <= e
                                                                   for b, e in ordered):
                outside.append(f'{name}:{s["name"]}')
    check('entry_segments_inside_root', not outside, ','.join(outside[:8]))
    kernel = kc_image.Image(image, root.entries['com.apple.kernel'][1])
    thread = root.command(LC_UNIXTHREAD)
    pc = struct.unpack_from('<Q', image, thread[0] + 16 + 32 * 8)[0] if thread else None
    code = next((s for s in kernel.segments if s['name'] == '__TEXT_EXEC'), None)
    check('entry_in_kernel_text_exec', pc is not None and code and code['va'] <= pc < code['va'] + code['size'],
          hex(pc) if pc else 'no LC_UNIXTHREAD')
    try:
        fixups = root.fixups()
        wild = sum(1 for target, *_ in fixups.values() if not any(b <= target < e for b, e in ordered))
        check('root_fixups_target_mappings', bool(fixups) and not wild, f'{len(fixups)} sites, {wild} outside')
    except ValueError as e:
        check('root_fixups_target_mappings', False, str(e))
    info = next((s for s in root.segments if s['name'] == '__PRELINK_INFO'), None)
    raw = image[info['fileoff']:info['fileoff'] + info['filesize']].split(b'\0', 1)[0] if info else b''
    problems = xml_rules(raw)
    check('prelink_xml_lexer_rules', raw and not problems, '; '.join(problems[:5]))
    try:
        listed = plistlib.loads(raw)['_PrelinkInfoDictionary']
        ids = {e['CFBundleIdentifier'] for e in listed}
        # Plist integers are signed: kernel addresses come back negative. Codeless bundles (the kpi.* pseudo
        # kexts, personality-only drivers) carry the 0x7fffffffffffffff sentinel and have no fileset entry.
        loads = {e['CFBundleIdentifier']: e['_PrelinkExecutableLoadAddr'] & ((1 << 64) - 1)
                 for e in listed if e.get('_PrelinkExecutableLoadAddr', CODELESS) != CODELESS}
        wrong = [b for b, va in loads.items() if b not in root.entries or root.entries[b][0] != va]
        codeless = sorted(closure - set(loads) - set(root.entries))
        check('prelink_info_lists_closure', closure <= ids, f'{len(ids)} bundles; codeless: {",".join(codeless)}')
        check('prelink_load_addresses_match_entries', not wrong and set(loads) == set(root.entries) - {'com.apple.kernel'},
              ','.join(wrong[:8]))
    except (ValueError, KeyError, plistlib.InvalidFileException) as e:
        check('prelink_info_lists_closure', False, str(e))
    check('report_hash_matches', report.get('candidate_sha256') == hashlib.sha256(image).hexdigest())
    result['rebuild_equivalence'] = {'ok': None, 'detail': 'not applicable: UniXNU check-prelink-rebuild-equivalence '
                                     'covers plain arm64 static prelink output, not arm64e filesets'}
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--kernel', required=True, type=Path, help='unstripped DEVELOPMENT kernel with split-seg info')
    ap.add_argument('--kernelcache', required=True, type=Path, help='decompressed MH_FILESET kext donor')
    ap.add_argument('--out', required=True, type=Path)
    ap.add_argument('--reference', type=Path, help='symbol-bearing RELEASE comparison kernel (mach.release.fuxi.unstripped)')
    ap.add_argument('--root', action='append', help='replace the profile roots (repeat)')
    ap.add_argument('--drop-soc', action='store_true', help='leave out the nine SoC hardware roots')
    ap.add_argument('--early-security-init', action='append', metavar='BUNDLE=ORIGINAL_VA',
                    help=f'default: {EARLY_SECURITY_INIT} (24A446 j820 only)')
    ap.add_argument('--allow-missing', action='store_true',
                    help=f'stub unmatched functions to {STUB} and keep unmatched data references (experimental)')
    ap.add_argument('--kit', type=Path, default=KIT, help='XnuPrelink copy (default tools/xnuprelink)')
    ap.add_argument('--trial', action='store_true', help='continue past preflight failures (counts only, not for boot)')
    ap.add_argument('--preflight-only', action='store_true')
    ap.add_argument('--force', action='store_true', help='replace kernel.prelinked and its reports in OUT')
    a = ap.parse_args()
    kit = load_kit(a.kit.resolve())
    roots = list(a.root or ROOTS)
    if a.drop_soc:
        roots = [r for r in roots if r not in SOC_ROOTS]
    initializers = parse_initializers(a.early_security_init or [EARLY_SECURITY_INIT])
    out = a.out
    output = out / 'kernel.prelinked'
    if output.exists() and not a.force and not a.preflight_only:
        raise SystemExit(f'{output} exists (pass --force to replace it)')
    native_data, cache_data = a.kernel.read_bytes(), a.kernelcache.read_bytes()
    reference_data = a.reference.read_bytes() if a.reference else None
    print('reference: ' + (f'{a.reference} (--fileset-reference)' if a.reference else
                           'none: matching against the native kernel only (no RELEASE comparison build given)'))
    failures, facts = preflight(native_data, cache_data, roots, initializers, a.allow_missing, kit,
                                not a.early_security_init)
    out.mkdir(parents=True, exist_ok=True)
    pre = {'roots': roots, 'early_security_init': {k: hex(v) for k, v in initializers.items()},
           'reference': str(a.reference) if a.reference else None, 'kit': str(a.kit), 'failures': failures,
           'trial': a.trial, **{k: v for k, v in facts.items() if k != 'layout'}, 'layout': facts.get('layout')}
    (out / 'preflight.json').write_text(json.dumps(pre, indent=2) + '\n')
    for f in failures:
        print(f'preflight: {f}')
    if failures and not a.trial:
        raise SystemExit(f'preflight failed ({len(failures)}); see {out / "preflight.json"}')
    print(f'preflight: {"ok" if not failures else "TRIAL, failures ignored"}; closure {facts.get("closure")}, '
          f'native __DATA {facts.get("native_data_size")}')
    if a.preflight_only:
        return
    for name in ('kernel.prelinked', 'kernel.json', 'summary.json', 'checks.json'):
        (out / name).unlink(missing_ok=True)
    pairing = matching(kit, cache_data, native_data, reference_data, out / 'kernel.map.json')
    image, report, seen = emit(kit, cache_data, native_data, pairing, roots, initializers, a.allow_missing)
    report.update(kernelcache=str(a.kernelcache.resolve()), native=str(a.kernel.resolve()),
                  map=str((out / 'kernel.map.json').resolve()), trial=a.trial, preflight_failures=failures)
    kit[1].write_candidate(image, report, output, out / 'kernel.json', a.allow_missing)
    per_root = summarize(kit, cache_data, report, pairing, seen, roots)
    stats = pairing['stats']
    summary = {'trial': a.trial, 'reference': report.get('reference_sha256'), 'output_bytes': len(image),
               'matcher': {k: stats[k] for k in stats if not isinstance(stats[k], dict)},
               'matcher_by_method': stats.get('functions_by_method'), 'statistics': report['statistics'],
               'distinct': {kind: len({u['original'] for r in per_root.values() for u in r['unmatched'] if u['kind'] == kind})
                            for kind in ('stubbed', 'missing')},
               'per_root': per_root}
    (out / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    result = checks(kit, image, report)
    (out / 'checks.json').write_text(json.dumps(result, indent=2) + '\n')
    print(f'{"bundle":58} {"refs":>5} {"match":>5} {"stub":>5} {"miss":>5}')
    for bundle, r in sorted(per_root.items(), key=lambda kv: (not kv[1]['root'], kv[0])):
        print(f'{bundle + ("" if r["root"] else " (dep)"):58} {r["kernel_targets"]:5} {r["matched"]:5} '
              f'{r["stubbed"]:5} {r["missing"]:5}')
    failed = [k for k, v in result.items() if v['ok'] is False]
    print(f'checks: {len(result) - len(failed) - 1} ok, {len(failed)} failed {failed}; output {len(image)} bytes')
    if a.trial:
        print('TRIAL output: counts only, not for a boot')


if __name__ == '__main__':
    main()
