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
- **K4** UFS root (port our FuxiUFS or UniXNU's UFS layer) and launchd.
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
- The CI ADT seed is fixed per build (random per run, the same on every boot of that artifact).
