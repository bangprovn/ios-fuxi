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
fetches source only, at `xnu27/sources.env`: xnu, AvailabilityVersions, libplatform, libdispatch, and
`EXTERNAL_HEADERS/TrustCache` from xnu-8796.101.5. Then `tools/ci/build-xnu27.sh`: apply the series, build the
kernel SDK root (availability.pl, libplatform and libdispatch headers, `installhdrs`), build `libfirehose_kernel`
for arm64e, then make with UniXNU's flags (`ARCH_STRING_FOR_CURRENT_MACHINE_CONFIG=arm64e`, link address
`0xfffffff007004000`, `-UARM_LARGE_MEMORY`, iOS 27.0 deployment, no LTO, native PAN, CS hash cache,
`XNU_KERNEL_CHAINED_FIXUPS=0` = ld-classic `-threaded_starts_section`). Artifact `fuxi-kernel-ios27`:
`mach.development.fuxi`, `mach.development.fuxi.unstripped` (keep it for prelinking and the monitor),
`kernel-arch.txt`, `fuxi27.adt` + `cmdline.txt` (1 CPU), `fuxi27-cpus7.adt` + `cmdline-cpus7.txt`,
`xnu27-SHA256SUMS.txt`, logs. `bash tools/ci/build.sh kernel-ios27 --dry-run` prints the plan on Linux.

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

- **K1** CI kernel, banner + early log over USB, clean stop at root mount. Built, not yet booted.
- **K2** RAM-disk root (`rd=md0`, mockfs is in the config) and the crypto decision; libpthread-539.100.4 in-tree
  (UniXNU `source-pthread-bootstrap` is imported but not wired).
- **K3** Local prelink of donor kexts (`unstripped` kernel + the user's kernelcache, UniXNU XnuPrelink approach),
  AMFI/sandbox early init (`native-security-init`), trust cache placement in the loader handoff.
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
