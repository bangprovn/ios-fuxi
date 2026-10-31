# iOS 27 userland lane (U1..U5)

Offline work, 2026-10-31. Donor build: iPhone12,3 (D421AP), iOS 27.0.1 24A446. No phone access,
deployment, push or CI run. Every device effect below is **not yet observed**. The kernel side
(xnu-12377.121.6, arm64e, UniXNU layers, fuxi-loader) is a separate lane; this document states what
the userland needs from it.

Tags: **OBSERVED** means read from the IPSW images, the UniXNU sources or our own tool runs on the host.
**GUESS** means inferred and not yet checked on a device.

## Tools

| Tool | Purpose |
| --- | --- |
| `tools/apfs-ro.py` | Our own read-only APFS reader, written from Apple's public APFS reference. It handles checkpoints, object maps, sealed hashed fs trees, the fext tree, xattrs, symlinks and decmpfs (zlib, LZVN, LZFSE, stored). LZBITMAP is reported, not decoded. It also decodes IM4P LZFSE payloads. Commands: `info`, `ls`, `stat`, `cat`, `list`, `extract`. |
| `tools/ios27-root.py` | `im4p` unwrap; trust-cache `trust-module` / `trust-bundle` / `trust-check`; the windows root `image` / `image-check --verify`; `inventory` (JSON: volumes, compression, launchd jobs); `data-stage` (the Data volume tree plus `meta.tsv`, `apply-owners.sh` and `--hook FILE.py`). |
| `tools/ios27-mockfs.py` | Builds the N4KROOT two-file RAM disk used by the kernel lane's mockfs patch (K2/K3), plus an owner trust module for the served Mach-O files that the static cache does not already cover. |
| `tools/mkinit.py --arm64e` | The static init from `sweet-init.S` with header subtype `0x80000002`. |
| `tools/test-ios27-root.py` | 14 host tests on synthetic inputs only. |

## The images (OBSERVED, 24A446)

| Image | Size | Volume | Sealed | Files | Compression (files / logical size) |
| --- | --- | --- | --- | --- | --- |
| OS | 9.3 GB raw APFS, 4 KiB blocks | role System | no (incompat 0x8 only) | 226,505 | zlib 44,991 / 9.3 GiB; stored 183,059; plain 29,188 / 4.3 GiB |
| SystemOS cryptex | 6.0 GB | one volume, no role | yes (hashed fs tree, fext tree) | 1,834 | plain 210 / 4.9 GiB (the dyld shared cache); LZBITMAP 1,747 / 1.9 GiB |
| AppOS cryptex | 16 MB | one volume, no role | yes | 858 | LZBITMAP 869 / 13 MiB |

What the images contain:

- The OS image has no `/etc/fstab`. The fstab lives in the ADT: `/filesystems/fstab` lists the System, Preboot, Data, xART, Baseband, Update and Hardware volumes by role.
- `/System/Cryptexes/{OS,App}` are symlinks into `/private/preboot/Cryptexes/...`. WebKit, JavaScriptCore and other frameworks are symlinks into `/System/Cryptexes/OS`.
- `/private/var` holds a seed: about 1 MiB, plus `staged_system_apps` at 1.1 GiB.
- launchd runs from the consolidated, signed cache `/System/Library/xpc/launchd.plist`, which has a `.sig` beside it. Its keys: VersionNumber 7, 729 LaunchDaemons entries (724 labels), 66 KeepAlive/RunAtLoad jobs and 10 `_PanicOnCrash` jobs, among them logd, watchdogd, SpringBoard, backboardd and containermanagerd.
- `/sbin/launchd` and `/usr/lib/dyld` are thin arm64e (subtype `0x80000002`). launchd is dynamic. The dyld shared cache is only in SystemOS.
- The cdhashes we computed for `/sbin/launchd` and `/usr/lib/dyld` were both found in the IPSW static trust cache. This checks both `ios27-root.py cdhash()` and the module parser.
- Trust caches are IM4P (`trst` static, `trcs` SystemOS, `trca` AppOS), each one v2 module. Sizes: static 3,712 entries, SystemOS 130, AppOS 34. All entries are hash type 2. Flags and constraint-category bytes vary per entry.
- The kernelcache is an MH_FILESET of 243 kexts. It includes apfs, AppleFSCompressionTypeZlib, tmpfs, AMFI, sandbox, CoreTrust, AppleImage4, IOSurface, IOMobileGraphicsFamily, IOGPUFamily, AppleSEPKeyStore, AppleEffaceableStorage and com.apple.kec.Compression. It has **no HFS kext**.
- The firmware set includes `sptm.t8030` and `txm.iphoneos`: iOS 27 on A13 uses SPTM/TXM.

## U1 decision: root format

| | (a) raw APFS windows (UniXNU/sheng) | (b) our HFS+ extent root (16.7 style) |
| --- | --- | --- |
| Building it | Copy the images byte for byte. 14.3 GiB, built in 3 minutes on Linux, no root needed. | Unpack 226k files plus the cryptexes from APFS into HFS+. LZBITMAP files cannot be decoded on Linux, so the cryptex would need a Mac. |
| Integrity | Seals and `launchd.plist.sig` are untouched. Cryptex and trust-cache hashes match the IPSW modules unchanged. | Every file is re-encoded. Cryptex sealing and the per-image trust caches no longer correspond to a mounted volume. |
| Kernel needs | Apple's own apfs kext (present), windowed IOMedia, UUID root match, union/bind mounts, Data mount before launchd. All of these exist as UniXNU patches. | An HFS kext. iOS 27 ships none, so our built-in HFS (0026) would need porting to xnu-12377. That means an HFS root plus a cryptex merge into a single tree. |
| Changes we make | Only on Data (or by kernel binds). The system volume cannot be edited in place. | The 16.7 workflow (Linux-editable, `root-edit-linux.sh`). |
| Proven | sheng boots iPadOS 27 this way (UniXNU docs, OBSERVED as documentation only). | Not on 27. |

**Recommendation: (a).** Keep the Apple images unmodified in windows, and put every Fuxi change on the Data volume or in kernel boot policy.

Fuxi difference: the sheng manifest sits at offset 0 of the userdata partition. Here, userdata is f2fs and holds the 16.7 root, which must survive until 27 boots. So the windows image is a **pinned, preallocated f2fs file**. Its physical extents go to the kernel through the ADT the same way patch 0088 does for the 16.7 root (`fuxi-root-extents`): a second property set, for example `fuxi-root27-extents` and `fuxi-root27-blocks`. That is a kernel-lane decision.

The kernel reads the manifest from the file's first block. It then publishes one IOMedia per window: read-only for OS, SystemOS and AppOS, read-write for Data. The root is matched by OS volume UUID through `/chosen/root-matching`.

The manifest uses UniXNU's documented layout, so a reader ported from UniXNU parses it unchanged:

- `SHNGROOT`, version 1, window count, image bytes;
- 128-byte entries `{name[32], offset, size, block_size=512, flags bit0=RO, sha256, reserved}`;
- SHA-256 of bytes 0..4063 at byte 4064;
- windows aligned to 1 MiB from 0x100000.

## How sheng assembles its root, mapped onto ours

These are OBSERVED from the UniXNU patches and scripts, read only. Each item gives the sheng mechanism, then the Fuxi plan.

1. **Volumes.**
   - sheng: OS and SystemOS windows in a raw userdata image. Root is matched by UUID.
   - Fuxi: the same windows plus AppOS and a Data window, inside the pinned f2fs file. `ios27-root.py image`.
2. **SystemOS cryptex.**
   - sheng: before launchd, in `load_init_program`, the kernel mounts the SystemOS volume (found by UUID) read-only, union over `/`. It sets the commpage `dyld_flags` bit 3 so dyld does not look for a cryptex ignition.
   - Two union fixes come with it. A lookup returns ENOENT when the lower vnode is not a directory, because the OS has symlinks where the cryptex has directories. `getattrlistbulk` on union directories is also fixed.
   - The non-union variant mounts at `/private/preboot` and sets `DYLD_SHARED_CACHE_DIR`.
   - Fuxi: the same, in the kernel lane.
3. **The cryptex OS view.**
   - sheng: union-mounts the Data directories `ngpu/preboot/Cryptexes/OS/{System,usr}` over `/private/preboot`, then binds `/System` and `/usr` to `.../Cryptexes/OS/...`, so `/System/Cryptexes/OS` resolves. WebKit ExtensionKit needs this.
   - Fuxi: the same mechanism. `data-stage` can create those directories through a hook.
4. **AppOS.**
   - sheng: runs the App cryptex content from owner Data and puts the AppOS hashes in the owner trust module.
   - Fuxi: mount the AppOS window read-only at `/private/preboot/Cryptexes/App` (kernel). If that is not possible, extract it into Data. AppOS is 869 LZBITMAP files, which only Apple's kernel decompressor reads, so mounting the window is the better choice.
5. **Data volume.**
   - sheng: an APFS RAM-block seed made on macOS with hdiutil, mounted read-write at `/private/var` by the kernel before launchd; boot panics on failure. The seed has migration removed, Setup skipped and the CA framebuffer compression keys set false.
   - Fuxi: `data-stage` builds the same tree on Linux. It starts from the OS image's `/private/var` (minus `staged_system_apps`, which sheng bind-mounts read-only over `/Applications` with `owner-staged-apps=1`), then writes:
     - `db/com.apple.xpc.launchd/disabled.plist`;
     - `mobile/Library/Preferences/com.apple.{migration,purplebuddy,purplebuddy.notbackedup,springboard,managedconfiguration.notbackedup}`;
     - `com.apple.backboardd` and `.GlobalPreferences` for mobile and root (CA keys);
     - the containers directories;
     - `db/fuxi/job-env.plist`.

     The APFS image is then built on a Mac (below). Linux has no APFS writer. Writing one is U3 work.
6. **launchd and daemons.**
   - sheng: leaves the signed cache alone. Every boot it writes launchd's native override database `/private/var/db/com.apple.xpc.launchd/disabled.plist` (label → true), and adds a reset of the setup plists.
   - Fuxi: `data-stage --lite --disable LABEL`, with labels checked against the image's job list. Our 16.7 path (editing `launchd.plist` under `launchd_unsecure_cache=1`) is not used: the cache is on a sealed volume and has a `.sig`.
   - The FUXI_MTL_* job environment can no longer be added to job plists. It goes to `db/fuxi/job-env.plist`, which the Metal plugin, or a kernel/launchd shim, has to read. GUESS: nothing reads it yet.
7. **Trust caches.**
   - sheng: a DarwinBoot bundle (u32 count, u32 offsets, v2 modules). An owner module with its own cdhashes comes first, then the static and SystemOS modules byte for byte. The loader rules: count 1..128, aligned ordered offsets, ascending hashes, hash type 1..4. The address goes into `/chosen/memory-map/TrustCache`.
   - Fuxi: `ios27-root.py trust-bundle` builds it, and `tc_split` checks it under the same rules.
8. **Launch constraints.**
   - sheng: the `sheng-no-lc` boot-arg makes `mac_proc_check_launch_constraints` return 0 for cryptex and owner binaries.
   - Fuxi: the kernel lane needs the same. Our owner module entries use constraint category 0.

## Inventory: what iOS 27 userland expects for the D421AP identity

**ADT (OBSERVED from DeviceTree.d421ap).** fuxi-loader has to provide these on Fuxi. Values come from the donor tree; we have no syscfg, so those fields need fixed values.

- Root:
  - `model` iPhone12,3, `compatible` `D421AP|iPhone12,3|AppleARM`, `target-type` D421, `target-sub-type` D421AP;
  - the syscfg placeholders `serial-number`, `mlb-serial-number`, `region-info`, `model-number`, `regulatory-model-number`, `config-number`, `country-of-origin` and `model-config` must hold values;
  - `/product/device-colors`, `display-backlight-compensation` and `primary-calibration-matrix` are syscfg placeholders too.
- `/product`:
  - `product-description`, `unique-model`, `artwork-*` (scale 3, idiom phone, gamut P3), `graphics-featureset-class` (APPLE6) and fallbacks, `app-macho-architecture` arm64e, `device-perf-memory-class`, `ui-background-quality`, `framebuffer-identifier`, `oled-display`, `partition-style` iOS, `exclaves-enabled`, `ephemeral-data-mode`;
  - MobileGestalt answers come from these properties and the IORegistry. sheng drops the MobileGestalt cache on boot (`containers/Shared/SystemGroup/systemgroup.com.apple.mobilegestaltcache/.../com.apple.MobileGestalt.plist`) and `data-stage` removes it too.
  - GUESS: hardware the device lacks (NFC, Find My, Siri, cameras) is better switched off through `/product` than by disabling daemons.
- `/chosen`:
  - sheng sets `certificate-production-status=0`, `effective-production-status-ap=0`, `certificate-security-mode=1`, `effective-security-mode-sep=1`, `amfi-only-platform-code=1`, `crypto-hash-method=sha2-384` and `kernel-ctrr-to-be-enabled=0`;
  - `root-matching` gives the OS volume UUID;
  - `memory-map/TrustCache` gives the bundle address;
  - the donor tree also has `amfi-allows-trust-cache-load` and `cryptex1-certificate-format`.
- `/defaults`:
  - sheng adds `vmm-present=1` so AMFI does not wait for the SEP or PMGR, sets `kern.maxvnodes=78848` (the donor has 10,000) and `sleep-disabled=1`;
  - the donor has `content-protect` and `cpx-encryption-mode=1`. GUESS: these must go, or be satisfied by an AppleKeyStore stand-in such as sheng's `sheng-aks=1` / `owner-dataprotect=1`, because Fuxi has no SEP.
- `/filesystems/fstab`: the donor's role-based Data/xART/Update/Hardware/Baseband entries assume one container. Our Data sits in its own window, so the kernel mounts it before launchd as sheng does. GUESS: the role entries other than System should be removed, or matched to Data, so `apfs_boot_util` / `mount -a` does not fail on them.

**Daemons to disable early** (`data-stage --lite`; the labels exist in 24A446):

- sheng's `sheng-lite` list, with `com.apple.fairplayd.H2` (the iPhone label) in place of A2: identityservicesd, locationd, CommCenter, bluetoothd, photoanalysisd, mediaanalysisd, mediastream.mstreamd, icloud.searchpartyd, findmy.findmybeaconingd, familycircled, financed, softwareupdateservicesd, ndoagent, biomesyncd, email.maild, amsengagementd, adid, eyereliefd, fairplayd.H2, passd, mobile.obliteration, coreduetd;
- for Fuxi hardware: watchdogd (one of the 10 `_PanicOnCrash` jobs), cameracaptured, wifid, WirelessRadioManager, nfcd.

`ios27-root.py inventory` writes every job (program, user, KeepAlive, RunAtLoad, panic, Mach services) to a local JSON file for further cuts.

## What the kernel lane must provide

Syscalls and runtime:

- Darwin 26 syscall and `posix_spawnattr` layout. sheng converts a 0x100-byte attribute block in which the fields after `psa_launch_type` moved.
- The commpage `dyld_flags` bit 3 (no ignition).
- 4K-page code-signing hashes (our init is signed with 4K pages).
- An arm64e user ABI with subtype `0x80000002`, the subtype of every Apple Mach-O.

Storage:

- Window IOMedia over the pinned-file extents (the manifest above).
- Root matching by APFS volume UUID.
- Apple's apfs kext with AppleFSCompressionTypeZlib and com.apple.kec.Compression. GUESS: the LZBITMAP decoder for the cryptexes is in-kernel.
- tmpfs (present).

Mounts before launchd:

- SystemOS union over `/` with the lower-directory and `getattrlistbulk` union fixes;
- AppOS at `/private/preboot/Cryptexes/App`;
- the cryptex OS view binds;
- Data read-write at `/private/var` (a panic on failure is preferable to a half-booted userland);
- optionally `staged_system_apps` bound over `/Applications`.

Trust and policy:

- Trust-cache loading of the static, SystemOS and AppOS modules plus the owner module (sheng `trustcache-runtime-layout`).
- A launch-constraints bypass.
- AMFI/CoreTrust policy for platform code.
- A data-protection stand-in, since Fuxi has no SEP. AppleSEPKeyStore and AppleEffaceableStorage are in the kernelcache and will look for SEP hardware.

ADT: the identity and `/chosen` / `/defaults` properties listed above.

Firmware note: Apple's iOS 27 A13 kernel expects SPTM/TXM. The xnu-12377 open-source build used by the kernel lane decides whether that applies to us.

K2/K3 mockfs (`ios27-mockfs.py`):

- **K2**: our static arm64e init as `/sbin/launchd`, plus a one-entry owner trust module. It proves exec, the console and the trust-cache path.
- **K3**: IPSW `/sbin/launchd` and `/usr/lib/dyld`. Both are already in the static trust cache, so no owner module is needed. Without the shared cache, dyld should stop with a missing-cache error. GUESS: if it does, that proves the dynamic exec path and dyld's arm64e start.
- **K3p**: our init plus dyld served as the test file.

## Mac step for the Data image (GUESS, not yet run)

```
hdiutil create -size 2g -fs APFS -volname Data -layout NONE data.dmg
hdiutil attach -nomount data.dmg; diskutil mount <volume dev>
sudo ditto <stage>/root /Volumes/Data && sudo <stage>/apply-owners.sh /Volumes/Data
hdiutil detach ...; hdiutil convert data.dmg -format UDTO -o data.raw
```

The raw image goes in the image as `Data=PATH:rw`. Write its UUID into the kernel's Data-mount policy.

## Results (host runs, 2026-10-31)

Paths are relative to the local work directory `research/ios27-work/`, which is gitignored.

| Artifact | Size | Result |
| --- | --- | --- |
| `root27-24A446.img` (OS + SystemOS + AppOS windows) | 15,345,909,760 B (14.29 GiB) | 3 windows (OS at 0x100000, SystemOS, AppOS) built in 3 min; `image-check --verify` passes |
| `u27-out/inventory-24A446.json` | 298 KB | full scan of all three images in 26 s |
| `u27-out/data-stage/` | 1.6 MiB, 247 objects | 27 labels disabled, 0 unknown |
| `u27-out/mockfs/k2.img` | 45,056 B | arm64e init 16,635 B; owner module 1 entry |
| `u27-out/mockfs/k3.img` | 2 MiB | launchd and dyld cdhashes found in the static trust cache |
| `u27-out/mockfs/k2-bundle.tc` | 89,172 B | owner + static, 3,713 entries, loader-valid |

`apfs-ro.py` checks (OBSERVED):

- Its SystemOS listing matches a macOS listing line for line (3,084 lines).
- Its OS listing matches apart from two entries the Mac listing could not stat.
- It decodes the IM4P LZFSE DeviceTree (237,108 B) and kernelcache (67,584,000 B).

## Milestones

| Milestone | What it covers | Status |
| --- | --- | --- |
| U1 | Format decision, the tools, the inventory, the K2/K3 mockfs. | This document; done on the host. |
| U2 | First real root boot: windows image in a pinned f2fs file, an empty-tweak Data image, launchd reaching logd/containermanagerd. Needs kernel K4 (window IOMedia, root match, Data mount). | |
| U3 | Our own APFS writer for the Data volume (no Mac step), a `fuxi-stage-root` equivalent for root27 (allocate, pin, extents to ADT), and the job-env reader for the Metal plugin. | |
| U4 | SpringBoard: MobileGestalt identity complete, Setup and migration skipped, backboardd with our display/GPU stack, staged apps. | |
| U5 | Persistence: Data survives reboots; disabled daemons re-enabled one at a time (wifi through our stack, audio). | |

## Risks

- OBSERVED: the OS image is not sealed, but the BuildManifest pairs it with a root hash and mtree. Apple's restore seals the volume. GUESS: an unsealed root needs the kernel to skip authenticated-root checks, as sheng does through its owner boot-manifest-hash.
- OBSERVED: LZBITMAP is used in both cryptexes. Our reader cannot decode it, so any plan that copies cryptex files out on Linux fails for 1.9 GiB of them. Mounting the windows avoids this.
- GUESS: launchd on 27 honours `disabled.plist` overrides for jobs from the signed cache. sheng relies on this.
- GUESS: iOS 27 dyld and launchd accept a union root with SystemOS layered over `/`.
- GUESS: the job-env move breaks the FUXI_MTL knob path until a reader exists.
- OBSERVED: the data-protection path depends on the SEP (AppleSEPKeyStore). GUESS: without a stand-in, keybagd and containermanagerd stall early.
- The 14.3 GiB windows file plus a Data image have to fit next to the 16.7 root in userdata. Check free space before U2. That step is destructive and needs user confirmation.
