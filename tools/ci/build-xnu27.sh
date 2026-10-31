#!/bin/bash
# Build the iOS 27 kernel (xnu-12377.121.6 + xnu27/series, MACHINE_CONFIGS=FUXI,
# arm64e) from source checkouts already in WORK, the way UniXNU's
# build-xnu-4k-baseline.sh builds sheng's (docs-lanes/ios27.md). Mac only.
#
#   build-xnu27.sh WORK OUT
#
# WORK must hold clean checkouts at the xnu27/sources.env pins: xnu,
# AvailabilityVersions, libplatform, libdispatch and trustcache (xnu-8796.101.5,
# EXTERNAL_HEADERS/TrustCache only). tools/ci/build.sh kernel-ios27 fetches them.
# OUT receives mach.development.fuxi(.unstripped), fuxi27.adt, cmdline*.txt,
# kernel-arch.txt, unless FUXI_XNU27_CHAINED=0 the K2 prelink input
# mach.development.fuxi.chained.unstripped (same objects relinked with
# XNU_KERNEL_CHAINED_FIXUPS=1: split-seg info + chained fixups, not standalone
# bootable), and, unless FUXI_XNU27_RELEASE_REF=0, the symbol-bearing RELEASE
# comparison kernel mach.release.fuxi.unstripped(.noctf) plus release-reference.txt
# (K2 prelink matching, tools/ios27-prelink.py --reference; never booted).
# Nothing Apple-binary is fetched: no KDK, IPSW or kernelcache.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
work=$(cd "${1:?Usage: build-xnu27.sh WORK OUT}" && pwd)
out=${2:?Usage: build-xnu27.sh WORK OUT}
mkdir -p "$out"
out=$(cd "$out" && pwd)
die() { echo "build-xnu27: $*" >&2; exit 2; }
[[ $(uname -s) == Darwin ]] || die 'needs macOS with Xcode'
# shellcheck source=xnu27/sources.env
source "$root/xnu27/sources.env"
xnu=$work/xnu fake=$work/fakeroot obj=$work/obj sym=$work/sym dst=$work/dst
jobs=$(sysctl -n hw.logicalcpu)
for name in xnu AvailabilityVersions libplatform libdispatch trustcache; do
    [[ -d $work/$name/.git ]] || die "missing checkout $work/$name"
done
[[ $(git -C "$xnu" rev-parse HEAD) == "$XNU27_SHA" ]] || die 'xnu is not at XNU27_SHA'
git -C "$xnu" diff --quiet || die 'xnu checkout is not clean'

# ld-classic emits __TEXT,__thread_starts, which a standalone arm64e kernel
# needs (UniXNU arm64e-threaded-starts.patch). Fail before a long build.
xcodebuild -version
ldc=$(xcrun -sdk iphoneos -find ld-classic) || die 'this Xcode has no ld-classic (needed for -threaded_starts_section)'
echo "ld-classic: $ldc"

bash "$root/xnu27/apply.sh" "$xnu"
cp -R "$work/trustcache/EXTERNAL_HEADERS/TrustCache" "$xnu/EXTERNAL_HEADERS/"

mkdir -p "$fake" "$obj" "$sym" "$dst"
make -C "$work/AvailabilityVersions" install -j"$jobs" OBJROOT="$work/av-obj" SYMROOT="$work/av-sym" \
    DSTROOT="$fake" CMAKE="$(command -v cmake)" NINJA="$(command -v ninja)"
export XNU_AVAILABILITY_PL=$fake/usr/local/libexec/availability.pl
[[ -x $XNU_AVAILABILITY_PL ]] || die 'AvailabilityVersions did not install availability.pl'
export KERNEL_SDKROOT=$fake
mkdir -p "$fake/usr/local/include/kernel" "$fake/usr/local/include/os" "$obj/EXPORT_HDRS/libsa" \
    "$fake/System/Library/Frameworks/Kernel.framework/Versions/A/PrivateHeaders/platform"
cp -R "$work/libplatform/include/." "$fake/usr/local/include/"
cp -R "$work/libplatform/private/." "$fake/usr/local/include/"
cp -R "$work/libplatform/private/platform/." \
    "$fake/System/Library/Frameworks/Kernel.framework/Versions/A/PrivateHeaders/platform/"
for header in "$work"/libdispatch/os/*.h; do
    unifdef -DKERNEL=1 -DOS_FIREHOSE_SPI=1 -DOS_VOUCHER_ACTIVITY_SPI_TYPES=1 -UOS_VOUCHER_ACTIVITY_SPI \
        -o "$fake/usr/local/include/os/$(basename "$header")" "$header" || [[ $? == 1 ]]
done

base=(ARCH_CONFIGS=ARM64 MACHINE_CONFIGS=FUXI
      RC_DARWIN_KERNEL_VERSION="$XNU27_DARWIN" RC_ProjectSourceVersion="${XNU27_TAG#xnu-}" MAKEJOBS=-j"$jobs")
common=("${base[@]}" KERNEL_CONFIGS=DEVELOPMENT OBJROOT="$obj" SYMROOT="$sym")
make -C "$xnu" installhdrs SDKROOT=macosx "${common[@]}" DSTROOT="$fake"

# libfirehose_kernel from libdispatch, against the headers just installed.
python3 - "$work/libdispatch/xcodeconfig/libfirehose_kernel.xcconfig" "$xnu/EXTERNAL_HEADERS" <<'PY'
from pathlib import Path
import sys
p, ext = Path(sys.argv[1]), sys.argv[2]
s = p.read_text()
s = s.replace('$(SDKROOT)/System/Library/Frameworks/Kernel.framework', '$(FAKEROOT_DIR)/System/Library/Frameworks/Kernel.framework')
s = s.replace('$(SDKROOT)/usr/local/include', '$(FAKEROOT_DIR)/usr/local/include')
lines = s.splitlines()
hits = [i for i, l in enumerate(lines) if l.startswith('HEADER_SEARCH_PATHS =')]
if not hits:
    raise SystemExit('libfirehose_kernel.xcconfig has no HEADER_SEARCH_PATHS')
for i in hits:
    lines[i] += f' $(SDKROOT)/usr/include $(FAKEROOT_DIR)/usr/local/include {ext}'
p.write_text('\n'.join(lines) + '\n')
PY
xcodebuild install -project "$work/libdispatch/libdispatch.xcodeproj" -target libfirehose_kernel \
    -configuration Release -sdk iphoneos -jobs "$jobs" ARCHS=arm64e VALID_ARCHS=arm64e ONLY_ACTIVE_ARCH=NO \
    IPHONEOS_DEPLOYMENT_TARGET="$XNU27_IOS_VERSION" CODE_SIGNING_ALLOWED=NO GCC_TREAT_WARNINGS_AS_ERRORS=NO \
    OBJROOT="$work/firehose-obj" SYMROOT="$work/firehose-sym" DSTROOT="$fake" FAKEROOT_DIR="$fake"
lib=$fake/usr/local/lib/kernel
[[ -f $lib/liblibfirehose_kernel.a ]] && cp "$lib/liblibfirehose_kernel.a" "$lib/libfirehose_kernel.a"
xcrun lipo -info "$lib/libfirehose_kernel.a"

# UniXNU's arm64e/ipados-27 build: BCM2837-style layer, 4K, no LTO; FUXI wraps it.
# PAN instructions need armv8.1 in the three files that use them (native-arm-pan).
kflags=(SDKROOT=iphoneos
    ARCH_STRING_FOR_CURRENT_MACHINE_CONFIG=arm64e LARGE_MEMORY_DEFINE=-UARM_LARGE_MEMORY
    ARM64_LINK_ADDR=0xfffffff007004000 ARM64_PLKSEG_ADDR=0xfffffff004004000
    DEPLOYMENT_TARGET_FLAGS="-miphoneos-version-min=$XNU27_IOS_VERSION -DXNU_TARGET_OS_IOS"
    DEPLOYMENT_LINKER_FLAGS="-Wl,-ios_version_min,$XNU27_IOS_VERSION"
    BOUND_CHECKS=0 BUILD_WERROR=0 XNU_KERNEL_CHAINED_FIXUPS=0
    CFLAGS_EXTRA="-Wno-reserved-identifier -Wno-pre-c11-compat -Wno-missing-designated-field-initializers -Wno-suggest-override -Wno-unnecessary-virtual-specifier -Wno-suggest-destructor-override -Wno-ms-bitfield-padding -DXNU_NATIVE_PAN=1"
    copyio.o_CFLAGS_ADD=-march=armv8.1-a pcb.o_CFLAGS_ADD=-march=armv8.1-a platform_tests.o_CFLAGS_ADD=-march=armv8.1-a
    ubc_subr.o_CFLAGS_ADD=-DXNU_NATIVE_CS_HASH_CACHE=1)
make -C "$xnu" build "${common[@]}" DSTROOT="$dst" "${kflags[@]}" BUILD_LTO=0

kdir=$obj/DEVELOPMENT_ARM64_FUXI
[[ -f $kdir/mach.development.fuxi ]] || die "no $kdir/mach.development.fuxi"
cp "$kdir/mach.development.fuxi" "$out/"
for raw in "$kdir/mach.development.fuxi.unstripped" "$kdir/mach.development.fuxi.unstripped.noctf"; do
    [[ -f $raw ]] && { cp "$raw" "$out/mach.development.fuxi.unstripped"; break; }
done
[[ -f $out/mach.development.fuxi.unstripped ]] || die 'no unstripped kernel'
# The loader build accepts a 12377 payload only with this object-path identity.
grep -q DEVELOPMENT_ARM64_FUXI "$out/mach.development.fuxi" || die 'kernel lacks the DEVELOPMENT_ARM64_FUXI identity'
python3 - "$out/mach.development.fuxi" "$root/tools" <<'PY' | tee "$out/kernel-arch.txt"
import re, sys
sys.path.insert(0, sys.argv[2])
import mkpayload
buf = open(sys.argv[1], 'rb').read()
m = mkpayload.parse_macho(buf)
version = re.search(rb'Darwin Kernel Version [^\0]*', buf)
print(version.group().decode(errors='replace') if version else 'no version string')
print(f"cputype={m['cputype']:#x} cpusub={m['cpusub']} thread_starts={m['thread_starts']} chained_fixups={bool(m['fixups'])}")
if m['cpusub'] != mkpayload.CPU_SUBTYPE_ARM64E or not m['thread_starts']:
    raise SystemExit('ios27 kernel is not arm64e with __TEXT,__thread_starts')
PY
python3 "$root/tools/ci/kernel-inputs.py" --variant ios27 --out "$out"

# K2 prelink input (docs-lanes/ios27.md#k2): XnuPrelink moves native segments
# around the retained kexts using LC_SEGMENT_SPLIT_INFO, which ld only writes
# with -add_split_seg_info -kernel (MakeInc.def, XNU_KERNEL_CHAINED_FIXUPS=1),
# as UniXNU's sheng kernel is linked. Only the link differs, so the same objects
# are relinked (.LDFLAGS changes); the later assignment on the make command line
# wins over the =0 in kflags. The K1 standalone kernel above is already copied.
if [[ ${FUXI_XNU27_CHAINED:-1} == 0 ]]; then
    echo 'chained kernel: skipped (FUXI_XNU27_CHAINED=0)'
elif make -C "$xnu" build "${common[@]}" DSTROOT="$dst" "${kflags[@]}" BUILD_LTO=0 XNU_KERNEL_CHAINED_FIXUPS=1; then
    chained=
    for raw in "$kdir/mach.development.fuxi.unstripped" "$kdir/mach.development.fuxi.unstripped.noctf"; do
        [[ -f $raw ]] && { chained=$raw; break; }
    done
    [[ -n $chained ]] || die 'no unstripped chained kernel'
    cp "$chained" "$out/mach.development.fuxi.chained.unstripped"
    python3 - "$out/mach.development.fuxi.chained.unstripped" <<'PY' | tee -a "$out/kernel-arch.txt"
import struct, sys
b = open(sys.argv[1], 'rb').read()
magic, cpu, sub, ftype, count = struct.unpack_from('<5I', b)
off, cmds = 32, set()
for _ in range(count):
    cmd, size = struct.unpack_from('<II', b, off)
    cmds.add(cmd)
    off += size
print(f'chained: filetype={ftype} cpusub={sub:#x} split_seg_info={0x1e in cmds} chained_fixups={0x80000034 in cmds}')
if ftype != 2 or sub & 0xff != 2 or 0x1e not in cmds or 0x80000034 not in cmds:
    raise SystemExit('chained kernel lacks MH_EXECUTE arm64e, LC_SEGMENT_SPLIT_INFO or LC_DYLD_CHAINED_FIXUPS')
PY
else
    echo '::warning::chained-fixups relink failed; DEVELOPMENT outputs kept' >&2
fi

# K2 matching reference (docs-lanes/ios27.md#k2), after UniXNU's
# build-fileset-release-reference.py --lto: the same patched tree, machine,
# arch and flags as above, KERNEL_CONFIGS=RELEASE with LTO, in its own
# OBJROOT/SYMROOT. Its symbols name the functions the stripped Apple kexts call;
# its addresses are never emitted. A failure here keeps the DEVELOPMENT outputs.
if [[ ${FUXI_XNU27_RELEASE_REF:-1} == 0 ]]; then
    echo 'release reference: skipped (FUXI_XNU27_RELEASE_REF=0)' | tee "$out/release-reference.txt"
    exit 0
fi
robj=$work/release-obj
mkdir -p "$robj/EXPORT_HDRS/libsa" "$work/release-sym" "$work/release-dst"
# RELEASE links with -exported_symbols_list all-kpi.exp, which names Apple
# SPTM/IOMMU KPIs (IOUnifiedAddressTranslator, pmap_iommu_*, sart/nvme PPL)
# that this non-SPTM kernel does not define. The comparison kernel is never
# booted, so a failed first link is retried once with those names removed from
# a copy of the list. ld reports C++ names demangled, so each list entry is
# compared both as written and through c++filt.
release_make() {
    make -C "$xnu" build "${base[@]}" KERNEL_CONFIGS=RELEASE OBJROOT="$robj" SYMROOT="$work/release-sym" \
        DSTROOT="$work/release-dst" "${kflags[@]}" BUILD_LTO=1 \
        LDFLAGS_KERNEL_LTO="-miphoneos-version-min=$XNU27_IOS_VERSION -Wl,-mllvm,-inline-threshold=100" \
        "$@" 2>&1 | tee "$work/release-make.log"
    return "${PIPESTATUS[0]}"
}
rdir=$robj/RELEASE_ARM64_FUXI
release_ok=0
if release_make; then
    release_ok=1
elif [[ -f $rdir/all-kpi.exp ]]; then
    sed -n '/^Undefined symbols for architecture/,/^ld: symbol(s) not found/s/^  "\(.*\)", referenced from:$/\1/p' \
        "$work/release-make.log" | sort -u > "$work/release-missing.txt"
    if [[ -s $work/release-missing.txt ]]; then
        echo "release reference: $(wc -l < "$work/release-missing.txt") exported KPIs undefined; relinking without them"
        xcrun c++filt -_ < "$rdir/all-kpi.exp" > "$work/release-kpi.demangled"
        paste -d '\t' "$rdir/all-kpi.exp" "$work/release-kpi.demangled" | awk -F '\t' \
            'NR == FNR { miss[$0] = 1; next } !(($1 in miss) || ($2 in miss)) { print $1 }' \
            "$work/release-missing.txt" - > "$work/release-kpi.exp"
        echo "release reference: kept $(wc -l < "$work/release-kpi.exp") of $(wc -l < "$rdir/all-kpi.exp") exported KPIs"
        release_make LDFLAGS_KERNEL_ONLY_CONFIG_RELEASEARM64="-Wl,-exported_symbols_list,$work/release-kpi.exp" && release_ok=1
    fi
fi
if [[ $release_ok == 1 ]]; then
    for raw in mach.release.fuxi.unstripped mach.release.fuxi.unstripped.noctf; do
        [[ -f $rdir/$raw ]] && cp "$rdir/$raw" "$out/"
    done
    [[ -f $out/mach.release.fuxi.unstripped || -f $out/mach.release.fuxi.unstripped.noctf ]] || die "no unstripped RELEASE kernel in $rdir"
    (cd "$out" && shasum -a 256 mach.release.fuxi.unstripped*) | tee "$out/release-reference.txt"
    if [[ -s $work/release-missing.txt ]]; then cp "$work/release-missing.txt" "$out/release-kpi-removed.txt"; fi
else
    echo '::warning::RELEASE reference build failed; DEVELOPMENT outputs kept' >&2
    echo 'release reference: build failed' > "$out/release-reference.txt"
fi
