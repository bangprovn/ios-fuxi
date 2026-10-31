# iOS 27 kernel lane (xnu-12377, arm64e)

Goal: boot iOS 27.0.1 (24A446, donor userland iPhone12,3) on Fuxi the way UniXNU boots iPadOS 27 on sheng
(Xiaomi Pad 6S Pro, the same SM8550). This page is the kernel side: the source layers, how CI builds the kernel,
how it is packed and booted, the milestones, and what is still unknown. The userland/root plan is a separate lane.
The xnu-8796 kernel (`xnu/`, CI `metal4`/`wlan6g`/`metal4e`) is unchanged and stays the daily kernel.

Words: **OBSERVED** means seen here (a file, a test, a command result). **GUESS** means not checked yet.
Nothing on this page has run on the device: device effects are not yet observed.

## Decisions (user, 2026-10-31)

- Follow UniXNU: xnu-12377.121.6 (`ac9718f`, the newest published xnu; 24A446 runs on it with UniXNU's Darwin 26
  syscall/KPI patches), arm64e, native PAC keys, UniXNU's BCM2837-style machine layer (classic pmap, no SPTM/TXM,
  no monitor), 4K pages as sheng. FUXI.md ID_AA64MMFR0 `0x22200101122` decodes to PARange 40-bit, 16-bit ASIDs,
  TGran4 supported, TGran16 supported, TGran64 not: no 4K blocker (OBSERVED from the value).
- Apple arm64e kexts (corecrypto, AMFI, sandbox, APFS, IOSurface, ...) are prelinked from the user's own donor
  kernelcache at local pack time only, in a later milestone. Never in CI, never committed.
- Keep shim/fuxi-loader, `tools/mkpayload.py`, the ADT generator and the USB console/monitor.

## Layers (`xnu27/`, applied in `xnu27/series` order)

| Layer | Origin | Licence | Content |
|---|---|---|---|
| `patches/unixnu/` (51) | UniXNU `88604d0` `patches/xnu-12377/`, verbatim | BSD-2-Clause-Patent | arm64e + ipados-27 profile series minus the exclusions below |
| `patches/sheng/` | UniXNU-sheng `5b7d83c` (feat/ipados27-gpu-parity) | BSD-2-Clause-Patent | CPU/MMU subset of `zzzz-sheng-ipados27-bringup` + `zzye-sheng-cpu-idle`: T0SZ 17 (47-bit user VA), TCR KVA entry, 16-bit ASID, sigp recover, WFE idle, pmap WIMG, builtin boot-args; `sheng-kernel-stack-shift` (the `unixnu_kernel_stack_shift` symbol XnuPrelink's reviewed 24A446 sites name) |
| `patches/sm8550/` | UniXNU-sm8550-common `23b2049` | BSD-2-Clause-Patent | `native-smp-secondary` start.s MPIDR mask |
| `patches/fuxi/0001` | ours | APSL file headers | `MACHINE_CONFIGS=FUXI` wrapping BCM2837 (`-DFUXI=1`, 8 CPUs/2 clusters, mockfs in MASTER) |
| `patches/fuxi/0002` | ours (xnu-8796 patch 0014) | APSL file headers | HMAC-SHA256 DRBG, SHA-256/512 via xnu's in-tree `ccsha*_ltc_di`, SplitMix64 kmem RNG, all only while no corecrypto is registered |
| `patches/fuxi/0003` | ours (xnu-8796 framebuffer console) | APSL file headers | `fuxi_early_status`, 4 MiB `sweet_log_ring`, start.s/arm_init/bsd_init stage marks, fbcon on the ABL splash buffer |
| `patches/fuxi/0004` + `components/` | UniXNU ArmCpu `4bd3230`, ArmDTPlatform `417ff95` | BSD-2-Clause-Patent | GICv3/timer/PSCI CPU driver and DT platform expert, compiled into the kernel |
| `patches/fuxi/0005` | ours | APSL file headers | K2 crypto handoff: the kmem RNG backend is latched once (built-in or corecrypto), log lines say which; the fallback never calls `register_crypto_functions` |
| `patches/fuxi/0006` | ours | APSL file headers | K2: `sweet_log_ring` 256 KiB (native `__DATA` must fit the fileset gap), size exported as `sweet_log_size` |
| `patches/fuxi/0007` | ours (UFS path of xnu-8796 `sweet_ufs.c`); IOStorage/IOMedia declarations from IOStorageFamily-337.100.1 | APSL file headers | K4a: read-only, extent-guarded UFS reads of the pinned root file; its windows published as Apple IOMedia (`fuxi_root27=1`) |

Left out of the UniXNU series: `source-corecrypto14-bootstrap` (see below), `offline-*` and `zzz-boot-progress`
(sheng offline tooling; `zzz-boot-progress` depends on them), `zzw*`/`zzy*` (sandbox grants, GPU addon mount,
staged apps, cryptex view, launch constraints: userland milestones). Notices: `xnu27/NOTICE`, README credits.
`tools/test-fuxi-ios27.py --xnu DIR` applies the full series to a fresh xnu-12377.121.6 (OBSERVED: 60 patches apply).

### corecrypto (licence blocker, decision needed)

UniXNU's arm64e profile builds `corecrypto-2023` source into the kernel (`source-corecrypto14-bootstrap`). That
source is Apple's corecrypto under the "CORECRYPTO INTERNAL USE LICENSE AGREEMENT" (internal use, limited term,
no redistribution) and is not an apple-oss-distributions release (OBSERVED in its licence file). It is not used.
UniXNU's own ipados-27 profile sets `CONFIG_XNU_CRYPTO_BOOTSTRAP="prelinked"`: there the real
`com.apple.kec.corecrypto` comes from the prelinked kernelcache (OBSERVED in its profile).

12377 needs crypto early: `random_cpu_init` panics without a registered PRNG, `bootseed_init` needs SHA-512, the
kmem allocator needs a random context, and IOPlatformExpert hashes chip ids with SHA-1. Patch 0002 covers the first
three with code already inside xnu (`osfmk/corecrypto` SHA-256/512) and our own DRBG. SHA-1, MD5, AES and SHA-384
are still missing and panic if called. So the K1 ADT has no `chip-id`/`unique-chip-id`.
Options for K2 and later: (a) prelink the donor corecrypto kext locally (UniXNU's route; it must register before
anything needs SHA-1/AES, and the 0002 kmem context must not change layout under it), or (b) port the BearSSL (MIT)
providers from our xnu-8796 patches 0020/0023/0027/0032/0033. The user chooses.

## Build (CI)

```
gh workflow run fuxi-build.yml -f kernel=ios27 -f runner=macos-26 -f xcode=26.0
```

Use any Xcode 26 or newer that the runner image has; the "Select Xcode" step lists them. `iig_mode`/`iig_run`
are ignored for ios27. `tools/ci/build.sh kernel-ios27` runs `test-fuxi-ios27.py` and `test-fuxi-arm64e.py`. It then
fetches source only, at `xnu27/sources.env`: xnu, AvailabilityVersions, libplatform, libdispatch, libpthread, and
`EXTERNAL_HEADERS/TrustCache` from xnu-8796.101.5. Then `tools/ci/build-xnu27.sh`: apply the series, build the
kernel SDK root (availability.pl, libplatform and libdispatch headers, `installhdrs`), build `libfirehose_kernel`
for arm64e, then make with UniXNU's flags (`ARCH_STRING_FOR_CURRENT_MACHINE_CONFIG=arm64e`, link address
`0xfffffff007004000`, `-UARM_LARGE_MEMORY`, iOS 27.0 deployment, no LTO, native PAN, CS hash cache,
`XNU_KERNEL_CHAINED_FIXUPS=0` = ld-classic `-threaded_starts_section`), build libpthread's kernel part and relink
with it (K2 libpthread, below). Artifact `fuxi-kernel-ios27`:
`mach.development.fuxi`, `mach.development.fuxi.unstripped` (keep it for prelinking and the monitor),
`kernel-arch.txt`, `fuxi27.adt` + `cmdline.txt` (1 CPU), `fuxi27-cpus7.adt` + `cmdline-cpus7.txt`,
`xnu27-SHA256SUMS.txt`, `libpthread-report.json`, `libpthread-linked.txt`, logs.
`bash tools/ci/build.sh kernel-ios27 --dry-run` prints the plan on Linux.
For K2a the same job also uploads `mach.development.fuxi.chained.unstripped` (the same objects relinked with
`XNU_KERNEL_CHAINED_FIXUPS=1`; skip with `FUXI_XNU27_CHAINED=0`) and the RELEASE LTO comparison kernel
`mach.release.fuxi.unstripped(.noctf)` + `release-reference.txt` (own OBJROOT; skip with
`FUXI_XNU27_RELEASE_REF=0`). Either one failing to build only warns; one that links without libpthread stops
the job. Neither has been built yet (no CI run since).

## Pack and boot (user, local)

```
python3 tools/mkpayload.py OUT/mach.development.fuxi OUT/fuxi27.adt payload27.bin \
    --boot-line-length 1024 --cmdline "$(cat OUT/cmdline.txt)"
shim/fuxi-loader/build.sh payload27.bin        # same env vars as today
fastboot boot <image>; tools/fuxi-xnu-monitor.py --kernel OUT/mach.development.fuxi.unstripped --payload payload27.bin --out LOGDIR
```

Payload v2 (`--boot-line-length 1024`): 12377 grew `boot_args.CommandLine` from 608 to 1024 bytes, so the
segment table moves to 0x448 and `bootFlags`/`memSizeActual` to 1136/1144. The loader, its build check and the
monitor read the version (OBSERVED in `test-fuxi-ios27.py`). v1 payloads are byte-identical to before.

## K2a: prelinked Apple kexts

Why (OBSERVED, `ios-0098-k27` boot log): K1 reaches VM/zone init, prints "SDT: unable to find prelink info" and
panics "image4 interface not available" at `kern_trustcache.c:592`. AppleImage4, AMFI and sandbox are kexts.

Approach (UniXNU's): keep the original arm64e kexts of one kernelcache and emit an `MH_FILESET` around our
native kernel. `tools/xnuprelink/` is XnuPrelink `76452c6` (the revision UniXNU `88604d0` pins), verbatim apart
from one marked `Fuxi:` change (no `__fips_hmacs` seal: we build no corecrypto into the kernel). Its matcher
pairs the stripped Apple kernel's functions/data with ours (strings, call graphs, PAC shapes, reviewed fixtures for
24A446); the emitter places native segments around the retained kexts using split-segment info, rebinds every
kext pointer, branch and adrp into the old kernel, stubs unmatched functions to `_unixnu_fileset_unmapped`
(`--allow-missing`) and leaves unmatched data references listed as missing. xnu's `arm_init` slides the collection
itself, so `mkpayload.py --prelinked` copies the root segments flat and untouched.

Kernel side (in the series): `native-security-init` (`_NativePrelinkEarlySecurityInit`, AMFI's original
initializer before lockdown), `fileset-unmapped-call`, `trustcache-runtime-layout`, `arm64e-threaded-starts`,
`sheng-kernel-stack-shift` (the symbol a reviewed AMFI site needs), `fuxi/0005` (the corecrypto kext registers
crypto once, our kmem RNG backend is latched once; log `FUXI crypto: kmem RNG built-in|from corecrypto`,
`FUXI PRNG: corecrypto registered the kernel PRNG; built-in DRBG unused`), `fuxi/0006` (256 KiB console ring so
native `__DATA` fits the 0x1e8000 gap before the first retained kext data; `_sweet_log_size`, read by the monitor).
No double `register_crypto_functions` (only the kext calls it). AES and SHA-1 have no fallback; they are reached only
through `g_crypto_funcs`, which the kext fills during InitIOKit, before any caller found (GUESS: no earlier caller).

```
python3 tools/ios27-prelink.py --kernel OUT/mach.development.fuxi.chained.unstripped \
    --reference OUT/mach.release.fuxi.unstripped --kernelcache KERNELCACHE --out K2DIR --allow-missing
python3 tools/mkpayload.py OUT/mach.development.fuxi.chained.unstripped OUT/fuxi27.adt payload27k2.bin \
    --boot-line-length 1024 --cmdline "$(cat OUT/cmdline.txt) rd=md0" --prelinked K2DIR/kernel.prelinked --allow-incomplete \
    --ramdisk MOCKFS/k2.img --trustcache MOCKFS/k2-bundle.tc   # tools/ios27-mockfs.py + ios27-root.py trust-bundle
tools/fuxi-xnu-monitor.py --kernel K2DIR/kernel.prelinked ...   # symbols come from the fileset's kernel entry
```

`KERNELCACHE` is the decompressed iPad17,3 24A446 `kernelcache.release.j820` (sha256 `38e6d9fa…780a`, the input
the 24A446 fixtures and the AMFI address `0xfffffe00092551ac` are keyed to); it never goes into git. Roots: UniXNU's
31 profile roots, all kept (`--drop-soc` drops the nine SoC hardware ones; no trouble from them shown yet). The
script preflights before the minute-long matcher and writes `summary.json` (per bundle: distinct old-kernel
targets, matched/stubbed/missing, neighbour-name hints) and `checks.json` (UniXNU `check-prelink-xml` lexer rules,
flat root, entries, load addresses, entry pc, fixup targets; `check-prelink-rebuild-equivalence` covers plain
arm64 static prelinks only and is recorded as not applicable). The ADT carries what AppleImage4/AMFI read
(`/chosen` security tuple, `crypto-hash-method` sha2-384, `amfi-only-platform-code`, `manifest-properties`) and a
`/product` node for iPhone12,3 (iPhone 11 Pro, the userland identity; `mkadt-fuxi27.py --product board` gives
Xiaomi 13 / xiaomi,fuxi).

Trial (OBSERVED 2026-10-31, local, K1 DEVELOPMENT kernel, no RELEASE reference, `--allow-missing`). The K1 kernel
fails three preflight checks, all fixed in this branch but not yet built: no split-segment info (CI chained relink),
no `_unixnu_kernel_stack_shift` (sheng patch), native `__DATA` 0x54c000 > 0x1e8000 gap (fuxi/0006). The trial
therefore used a scratch kit copy outside the repo that skips native relocation, empties the reviewed sites and
parks `__DATA` in linkedit, so its image is counts only, not loadable:

| | |
|---|---|
| matcher | 6622 / 21792 original functions matched (49524 native), 644 data; 56 s |
| closure | 39 bundles (31 roots, 7 `kpi.*`, `com.apple.security.Image4`), 32 fileset entries |
| stubbed functions | 952 distinct (4738 summed per bundle) |
| missing data refs | 156 distinct (597 references) |
| output | 102 219 776 bytes; payload v2 99 987 600 bytes (K1: 18.5 MB kernel) |
| checks.json | 12/12 pass (rebuild-equivalence n/a) |

Per root, distinct old-kernel targets (matched / stubbed / missing): AMFI 457 (223 / 213 / 21), AppleImage4 305
(130 / 168 / 7), Image4 255 (103 / 146 / 6), sandbox 274 (140 / 115 / 19), apfs 858 (392 / 387 / 79), corecrypto 36
(25 / 9 / 2), CoreTrust 7 (6 / 0 / 1), AppleMatch 12 (7 / 4 / 1); the IOKit families around 40 % matched. The
most shared unmatched targets are OSMetaClass/OSObject/IORegistryEntry functions (hints, GUESS) and one page-aligned
`__DATA,__common` object every bundle uses. With these stubs any kext start would hit `_unixnu_fileset_unmapped`
(GUESS: immediately).

Before a device boot: a CI run producing the chained and RELEASE kernels; a prelink against both (GUESS: the
RELEASE reference is required: UniXNU's profile always passes it, and the shared metaclass/registry functions
are the kind name-based matching needs); missing data at 0 or reviewed; then the boot itself. Risks (GUESS): the
~100 MB boot image vs ABL `fastboot boot` limits and where ABL places it; DEVELOPMENT vs RELEASE layout drift
in the shapes the matcher compares; the relinked chained kernel itself has never booted standalone (it is not
meant to); Apple kexts touching SoC hardware that the Fuxi lacks (the nine SoC roots).

## K2 libpthread: the pthread kernel part

Why (OBSERVED, K2a prelinked boot): AppleImage4, AMFI, Sandbox, CoreAnalytics and BSD init (mbinit) pass, then
panic "pthread kernel extension not loaded (function table is NULL). @pthread_shims.c:319". xnu's `pthread_init`
needs the `pthread_functions` table that Apple's pthread kext registers; we have no such kext.

Before this change: `unixnu/source-pthread-bootstrap.patch` adds `bsd/kern/source_pthread_bootstrap.c`
(`STARTUP(EARLY_BOOT)` calls `pthread_start`, panics "source libpthread registration failed" on error), but the
file is not in `bsd/conf/files`, so xnu never compiled it. `pthread_start` is neither weak nor behind a define;
nothing referenced it, so the kernel linked cleanly and only failed at boot. That is UniXNU's design too: the
bootstrap lives in the archive, not in xnu's build.

Now (UniXNU's method, `scripts/build-libpthread-kernel.py` + `build-xnu-4k-baseline.sh` `XNU_SOURCE_PTHREAD=1` +
`build-fileset-release-reference.py`): CI fetches libpthread-539.100.4 (`LIBPTHREAD_SHA` `1f4f5265`, UniXNU's
ipados-27.0.1-24A446 profile pin) and `tools/ci/build-libpthread27.py` applies `xnu27/libpthread/series` (UniXNU's
`explicit-kernel-address.patch`, verbatim), compiles `kern/kern_init.c`, `kern_support.c`, `kern_synch.c` for
`arm64e-apple-ios27.0` (`-mkernel`, the fakeroot Kernel.framework headers, UniXNU's defines), renames definitions
that collide with kernel names (expected at least `pthread_kern`, `current_uthread`) with an `-include`d
`#define` header, refuses any import the kernel does not define (except linker-made `___dso_handle`), compiles
`source_pthread_bootstrap.c` with xnu's own recorded command for `prng_random.o` (`-MJ` JSON, `-M*` outputs
dropped), and archives all four into `libpthread_kernel.a`. The kernel names and compile record come from a first
DEVELOPMENT pass (a pthread-less kernel that is never copied out). Then every link gets
`LD_KERNEL_ARCHIVES=-L… -lfirehose_kernel -Wl,-force_load,libpthread_kernel.a` and
`XNU_EXTERNAL_KERNEL_ARCHIVES` (UniXNU's `external-archive-dependencies.patch` makes the link depend on it):
the DEVELOPMENT relink, the chained relink and the RELEASE LTO reference. `build-xnu27.sh` then checks each kept
unstripped kernel for `_pthread_start` and `_source_pthread_status` (`nm -U`) and stops the job if one lacks them,
so a pthread-less kernel cannot be produced silently; `libpthread-linked.txt` records the checks.

Expected boot (GUESS, not yet observed): `pthread_start` registers at EARLY_BOOT, so `pthread_init` in bsd_init
finds the table. Risks (GUESS): the first compile of libpthread against 12377's Kernel.framework headers with
Xcode 26 (UniXNU builds it, with their toolchain); libpthread is compiled with `-DDEBUG=1 -DMACH_ASSERT=1` as
UniXNU does, and its imports are audited only against DEVELOPMENT names, so the RELEASE reference link could miss
one (that only warns, as before); kexts that the prelink matched to the pthread-less kernel's layout must be
re-prelinked against the new chained kernel (the native kernel changed).

## K4a: the root file on UFS as IOStorageFamily media (`xnu27/patches/fuxi/0007`)

Kernel and host side only; nothing here has run on the device (every effect below is **not yet observed**).

Input: the root image of `tools/ios27-root.py image` (4 KiB SHNGROOT manifest, then the OS, SystemOS and AppOS APFS
images at 1 MiB-aligned windows) stored raw in one pinned, preallocated file in f2fs `userdata` (UFS LUN 0, 4 KiB
blocks). Its extents reach the kernel as on the xnu-8796 lane (`xnu/patches/0088`): ADT `/chosen`
`fuxi-root-extents` = u64 triples {file block, userdata block, blocks} and `fuxi-root-blocks`, built by
`mkadt-fuxi27.py --root-extents EXTENTS.json --root-bytes N` (same JSON and checks as `mkadt-fuxi.py`), plus a
`/fuxi-ufs` node with the UFS host `reg` (0x1d84000/0x3000).

`iokit/Kernel/arm/FuxiRoot27.cpp` (FUXI only, off unless boot-arg `fuxi_root27=1`), started from `bsd_init()` right
after `vfsinit()` on its own thread:

1. UFS transport: the default (non-UniXNU) path of `xnu/fuxi-unixnu/sweet_ufs.c`, unchanged in substance: the
   GCC UFS PHY AHB auto-gating step, take over the bootloader-initialised host (verify `UTRLBA` readback before any
   doorbell), polled UTP requests in slot 0, full clean+invalidate around DMA, 1 MiB bounce buffer
   (physically contiguous `IOBufferMemoryDescriptor`), 5 s timeout. Only three commands can be built: ACTIVE power
   (START STOP UNIT to the device WLUN 0xd0), TEST UNIT READY and READ(10) on LUN 0. There is no write path at all.
2. GPT `userdata`, then the extent table is validated (file blocks contiguous from 0, every extent inside
   `userdata`, no two extents sharing blocks, covering `fuxi-root-blocks`). From then on every READ(10) must lie
   inside one extent (`fx27_io_allowed`, checked again in the command builder).
3. File block 0: SHA-256 of the manifest checked (`libkern/crypto/sha2.h`), windows parsed (`fx27_manifest`).
4. Each window is published read-only, like an iPhone's `disk0`/`disk0s1`:
   `FuxiRootWindow` (on the platform expert) -> whole `IOMedia` -> `FuxiRootWindow` with `IOMatchCategory` =
   `IOStorage` and `Content Mask` = `GUID_partition_scheme` -> partition `IOMedia` named after the window, location
   `1`, `Partition ID` 1, `Base` 0, block size 512, `Content Hint` = the APFS GPT type
   `7C3457EF-0000-11AA-AA11-00306543ECAC` (boot-arg `fuxi_root27_hint=` replaces it, e.g. `Apple_APFS`), `UUID` =
   the first 16 bytes of the window's manifest SHA-256 as a v4 UUID (`fx27_window_uuid`). Our providers serve
   `read` directly from the window (async and sync); writes complete with `kIOReturnNotWritable` and both IOMedia
   are created with `isWritable = false`. The `rw` manifest flag is logged only.

Pure logic (extent table, map, guard, read planning, manifest, UUID) lives in `iokit/Kernel/arm/fuxi_root27.h`, which
`tools/test-fuxi-ios27.py` compiles with the host cc and compares with its Python mirror (random tables including
overlaps, gaps, out-of-partition and short tables; plans at every alignment; manifests from `ios27-root.py image`
and damaged copies), and checks that a whole-file read through the plans returns exactly the file's blocks.

### Why an IOMedia mirror, not an IOBlockStorageDevice

IOStorageFamily and APFS are prelinked Apple kexts; the native kernel cannot link against kext symbols
(`tools/xnuprelink` resolves kext->kernel only), so an in-kernel `IOBlockStorageDevice` subclass is not possible,
and adding our own kext to the prelink is a larger step (bundle, personalities, signing). Instead
`iokit/Kernel/arm/fuxi_storage_abi.h` restates IOStorage's and IOMedia's virtual methods from Apple's public
IOStorageFamily-337.100.1 headers as compiled for iOS (`TARGET_OS_OSX` 0: no `attach`/`synchronizeCache`/`discard`
slots; no vtable padding on iOS, so the reserved slots do not exist). The classes keep Apple's names because arm64e
signs every vtable entry with a discriminator derived from the mangled name of the method that introduced the slot;
the same declarations give the same slot index and the same signature. The IOMedia objects are the kext's own:
allocated with `OSMetaClass::copyMetaClassWithName("IOMedia")->alloc()` (after waiting up to 60 s for the class),
initialised with the virtual `IOMedia::init(...)`, then checked through their properties (`Size`, `Whole`,
`Content Hint`); a mismatch is logged as "vtable mirror out of date" and nothing is published. Our providers are
not kext `IOStorage` instances (`OSDynamicCast(IOStorage)` fails), so `IOMedia::handleOpen` skips the provider open,
while `IOMedia::read` calls `getProvider()->read(...)` unchecked through the mirrored slot. UniXNU's NativeUfs
(an IOBlockStorageDevice built into their kernel) and null-root-media were read for reference only; no code taken.

### Root selection

`mkadt-fuxi27.py --root-uuid OS-VOLUME-UUID` adds `/chosen root-matching`:
`<dict><key>IOProviderClass</key><string>IOMedia</string><key>IOPropertyMatch</key><dict><key>UUID</key><string>…</string></dict></dict>`.
`IOFindBSDRoot` reads it before `boot-uuid` and the default `Content = Apple_HFS` match, so the boot needs **no
`rd=`** (`rd=md0`/`rootdev=` would override it) and **no `--ramdisk`**. The UUID is the APFS system volume's UUID
from `ios27-root.py image --report` (`volumes.OS[*].uuid`, the `System` role), upper case. GUESS: APFS publishes each
volume as an IOMedia under its container with `UUID` = the volume UUID (as on a Mac); if not, the per-window
partition UUID (`fx27_window_uuid`) or `rd=diskNsM` from the log are the fallbacks.

### Next boot (not yet observed)

A CI kernel with `fuxi/0007` (the native kernel changes, so re-run `ios27-prelink.py` as in K2a), then:

```
python3 tools/mkadt-fuxi27.py fuxi27-k4.adt --root-extents EXTENTS.json --root-bytes ROOT_BYTES \
    --root-uuid OS_SYSTEM_VOLUME_UUID          # 20 KiB with 366 extents (cpus 7: 22 KiB)
python3 tools/mkpayload.py OUT/mach.development.fuxi.chained.unstripped fuxi27-k4.adt payload27k4.bin \
    --boot-line-length 1024 --cmdline "$(cat OUT/cmdline.txt) fuxi_root27=1 dyld_flags=8" \
    --prelinked K2DIR/kernel.prelinked --allow-incomplete --trustcache K3-BUNDLE.tc   # as K3 without rd=md0/--ramdisk
```

The ADT with the extent table exceeds the loader's old 16 KiB cap: `shim/fuxi-loader/loader.c` now accepts up to
64 KiB for version 2 payloads (v1 unchanged) and `mkpayload.py` refuses larger ADTs per version, so the loader must
be rebuilt. Expected log: `FUXI ROOT27: CAP=...`, `userdata first block ...; root 366 extents`, one
`FUXI ROOT27: window OS/SystemOS/AppOS ... read-only, UUID ..., hint ...` line per window, IOMediaBSDClient nodes,
APFS container/volume matching, then `BSD root: diskNsM`. Failure points to look for: the GCC/host checks (same as
the 8796 lane), `IOMedia::init did not take` (mirror wrong), no APFS match (content hint; try
`fuxi_root27_hint=Apple_APFS`), and the root mount itself (sealed system volume and its snapshot).

### K4 on the device (OBSERVED, boots A-E)

The UFS reader works: `FUXI ROOT27: window OS/SystemOS/AppOS ... read-only` for all three windows, APFS matches and
mounts every container (`nx_mount ... checkpoint`), and `IOFindBSDRoot` picks the OS volume through
`root-matching` (`Got boot device = .../AppleAPFSContainer/Rave24A446.D421D431OS@1`, `BSD root: diskNs1`). The root
mount then fails in a loop: `apfs_vfsop_mount: not allowed to mount as root` (EPERM), after `mockfs_mountroot
failed: 12` (mockfs probes the volume first). APFS logs `ARV enabled` and imports the root hash before that.

| Boot | Change | Result |
| --- | --- | --- |
| A | root-matching only | refused; `could not retrieve system-volume-auth-blob from device tree` |
| B | + `-apfs_shared_datavolume` | refused (the 24A446 kernelcache has no such string: no-op on iOS 27) |
| C | + `fuxi_root27_rwmedia=1` (IOMedia writable) | refused |
| D | `mkadt-fuxi27.py --root-hash` | root hash import succeeds (no error); still refused |
| E | + `mkadt-fuxi27.py --fstab` | still refused |
| F | + `fuxi_root27_nomockfs=1` (mockfs no longer probes first) | still refused |
| G | 16 MiB APFS ramdisk made with `newfs_apfs`, `rd=md0`, no UFS | refused at once, before APFS opens the device |
| H | prelink binds APFS/Sandbox's 4-byte global to `_rootdev` instead of `_mountroot` | still refused |
| I | `fuxi_root27_hiderootfs=1`: `MNT_ROOTFS` cleared while APFS mountroot runs | still refused (`fuxi: apfs mountroot without MNT_ROOTFS: 1`) |
| J | `fuxi_root27_kmount=1`: no apfs mountroot; `VFS_MOUNT` as a kernel mount with the BaseSystem flag and no args, as imageboot does | still refused (`fuxi: apfs kernel mount of /dev/disk2s1 as root: 1`) |

Boots I and J rule out `MNT_ROOTFS` and the mountroot entry point: `apfs_vfsop_mount` itself refuses. What is
left in the root mount is that nothing is mounted yet (no covered vnode, no `rootvnode`); a pivot from a temporary
root would change that (GUESS).

Boot G makes the refusal a global APFS gate: it does not depend on the device, the volume or the DT root
properties. `ios27-prelink.py` now writes `targets.json` (every kernel reference of each root kext with the native
symbol and the match method) to audit what APFS calls; 427 of APFS's 856 kernel references are bound by
`reference-order` only, so a mis-paired policy query is one candidate (GUESS). The `_rootdev` pair (H) is kept:
the original is a 4-byte global and `mountroot` an 8-byte pointer, so the old binding was wrong either way.

Observed about the image: the IPSW OS volume is not sealed (incompat 0x8 only, no snapshots, no integrity
metadata), like the unsealed iOS 17 OS volume UniXNU mounts as root. The iOS config has no CSR (`kern_csr.c` is
`config_csr` only), so there is no SIP/ARV bit to set.

Boot-time knobs (no rebuild once the kernel has them): `fuxi_root27_nomockfs=1` (`fuxi/0007`, `vfs_subr.c`) drops mockfs from
`vfs_mountroot`, so APFS sees the device first. `mkadt-fuxi27.py --root-hash FILE` puts a local root-hash file (the
restore's `SystemVolume` IM4P; never committed) in `/chosen system-volume-auth-blob`. `--fstab` adds
`/filesystems/fstab` with the `system-vol` entry (`/`, `ro`, role System, `os_env_type` 1) that iOS device trees
carry. Fallback if APFS keeps refusing `mountroot`: mount the OS volume as an ordinary read-only mount and
`vfs_switch_root` into it, as `imageboot_pivot_image` does.

Out of scope for K4a, next: the Data volume (writable, `/private/var`), the SystemOS/AppOS cryptex grafts (union with
the OS volume), the sealed-volume root snapshot (`root-snapshot-name`/ APFS seal checks), launchd changes, and a
writable path (extent-guarded WRITE(10) as on the 8796 lane).

## K1 on the device: what to expect (GUESS, not yet observed)

1. Loader as today, then `XNU jumping`. The monitor stage word moves through start.s stages 1-4 (`0x10001..4`), then
   `0x10101` (before rebase/sign), `0x10102` (PE_init_platform), `0x10103` (CPU topology), `0x10105`, `0x10107`
   (arm_vm_init), `0x10108` (serial_init, fbcon up), `0x1010f` (machine_startup).
2. The ring fills with the xnu banner (`Darwin Kernel Version 25.5.0 ... xnu-12377.121.6~.../DEVELOPMENT_ARM64_FUXI`),
   `FUXI PRNG: HMAC-SHA256 DRBG registered`, IOKit matching ArmDTPlatform and ArmCpu (GIC, timer).
3. Stages `0x10120`/`0x10121` (bsd_autoconf/setconf), then a panic at root mount (no root device in K1). That is
   the K1 pass condition.
Likely early stops: a PAC key or sheng start.s difference (stage 1-4), `arm_slide_rebase_and_sign_image` with
ld-classic chains (0x101), ADT parsing in ArmDTPlatform/ArmCpu (after 0x10f), an Apple-only path calling SHA-1/AES
(panic text names it).

## Milestones

- **K1** CI kernel, banner + early log over USB, clean stop at root mount. Booted (stops at the image4 panic
  without kexts).
- **K2** RAM-disk root (`rd=md0`, mockfs is in the config) and the crypto decision; libpthread-539.100.4 kernel part
  force-loaded into every kernel (K2 libpthread above). **Booted (OBSERVED):** with the prelinked kexts (8 stubbed
  kernel functions, 4 missing data references), AppleImage4, AMFI, Sandbox and vnguard initialize, the trust-cache
  bundle loads (2 modules), APFS and AppleFSCompressionTypeZlib start, `BSD root: md0`, and our arm64e init runs as
  PID 1 (`/sbin/launchd`, served by mockfs, cdhash in the owner module) and keeps printing its heartbeat.
- **K3** IPSW `/sbin/launchd` + `/usr/lib/dyld` from mockfs (`k3.img`, static trust cache from the IPSW).
  **Booted (OBSERVED):** both exec from the static trust cache. Without boot-args launchd enters libignition
  ("Darwin Ignition Sequence", ignition level 0x5) and stays silent. With `dyld_flags=8` (dyld bit 3 = no ignition,
  as UniXNU sets it; the DEVELOPMENT kernel passes the tunable to every exec) dyld runs and stops as expected:
  `Library not loaded: /usr/lib/libSystem.B.dylib ... no dyld cache`, then "initproc failed to start". The panic
  path itself nests (open). Next: a real root with the shared cache (K4).
- **K4** UFS root and launchd. **K4a (kernel + host tools, not yet observed):** `fuxi/0007` reads the pinned root
  file over UFS (read-only, extent-guarded) and publishes the OS/SystemOS/AppOS windows as Apple IOMedia; root by
  `/chosen root-matching` on the OS volume UUID (section above).
- **K5** Display/backlight/touch/buttons on 12377 (port the xnu-8796 Fuxi drivers).
- **K6** GPU (Adreno 740 path) and SMP on all 7 usable cores.

## Open risks

- Never compiled here: no Apple toolchain on Linux. The first CI run is the first compile (GUESS: Fuxi patches need
  small fixes). ArmCpu/ArmDTPlatform are compiled in-tree, not as UniXNU's archives (GUESS: same flags apart from
  `-I osfmk`).
- ld-classic in Xcode 26+: needed for `__thread_starts`; the build stops early if `xcrun -find ld-classic` fails.
- iig and SDK: 12377 with Xcode 26+ iig (GUESS: matches; UniXNU builds 12377 with a current Xcode).
- `deviceTreeP`/`virtBase` alignment for 4K start.s: the loader keeps the 32 MiB-aligned virt_base (GUESS fine).
- PSCI method (`smc` under Gunyah, as the xnu-8796 kernel) and 7 of 8 cores; cpu@100 stays with the loader monitor.
- K4a: the IOStorage/IOMedia mirror is checked only against the public 337.100.1 headers; the iOS 27 kext may
  differ (the property check after `init` catches an init slot mismatch, not a read slot mismatch). APFS matching
  keys and root mounting from an IOMedia without iBoot's `boot-uuid` are GUESSES. Polled UFS reads run in the
  caller's thread under one lock (no queueing, no interrupts): slow but simple.
- The CI ADT seed is fixed per build (random per run, the same on every boot of that artifact).
