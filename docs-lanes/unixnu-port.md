# P-UNIXNU: driver audit and selective ports

Offline implementation, 2026-10-23. Patch **0106**, canonical sources in
`xnu/fuxi-unixnu/`. No phone access, deployment, push, hosted build or CI run.
This is an audit of source contracts, not a claim that sheng behavior works on
Fuxi. The selected changes are independently **default off**.

Read alongside [touch/display](touch-display.md), [thermal](thermal.md),
[K1](gpu-k1.md), [host import](metal-hostimport.md), [N1](nativemetal-n1.md),
[T1](turnip-t1.md) and [H1](kernel-h1.md). README's historical CPU-rendering
status does not describe every later experimental GPU lane.

## Reference and licensing

The reference superproject is `research/UniXNU`, revision
`fb6aad6ab1f50199a4d99d2da1d0ca403b067633`, targeting
**xnu-10063.141.1 / Darwin 23 / iPadOS 17.7.11**. Many driver and kit directories
in the supplied checkout were empty submodule placeholders. Their exact
superproject gitlink revisions below were retrieved as text references outside
the repository. No reference script, module, test or build was executed or
imported, and neither supplied reference checkout was modified. The sheng
board/package source was read for integration context. HackiOS code is not
ported by this patch.

UniXNU's root licence is BSD-2-Clause-Patent; licences are also per submodule
and per file. AdrenoPM4, ArmSmmu4K and most AirNir sources declare MIT;
AirNir's `FusedF32.h` declares BSD-3-Clause. NativeUfs's selected source files
have **BSD-2-Clause-Patent SPDX headers**, but its submodule `LICENSE` contains
**BSD-2-Clause**, copyright 2026 blu-sharky. We preserve that exact file in
`LICENSE.NativeUfs`, the superproject patent notice in `LICENSE.UniXNU`, and
the source SPDX headers. NativeMobileDisplay's exact `LICENSE` (including its
ArmVirtPlatform contributor credit) is retained in `LICENSE.NativeMobileDisplay`.
This records the upstream discrepancy without relabelling its notices.
The README Credits entry now covers both ports. No GPL, Mesa, NativeMetal,
metal2vulkan source, firmware, Apple binary or device dump is added here.

## Driver comparison

Value is **stability / GPU / power**, with H/M/L indicating expected value of
closing the remaining gap, rather than measured benefit. Effort estimates
include adaptation and testing: S = localized changes, M = subsystem work,
L = new transport/ABI/boot architecture. Links pin the audited source revision.

| Driver | Ours today | Theirs, source examined | Gap and value (S/G/P) | Effort and disposition |
| --- | --- | --- | --- | --- |
| [ArmCpu](https://github.com/UniXNU/ArmCpu/tree/1951cc8444b340d7d8879e9418220cf29eb711d7) | `SweetCPU.cpp` from 0018/0065/0090; seven CPUs, PSCI CPU_ON, GICv3 virtual timer PPI 27, SGI1. XNU `osfmk/arm64/cpu.c::cpu_idle` already does DSB/WFI. `xnu/fuxi-perf/` owns EPSS/TSENS touch boost (0097). | `ArmCpu.cpp`: CPU/GIC/timer, redistributor discovery, opt-in secondaries, SPI handler table, SPI-based IPI workaround, quiesce/halt diagnostics. | SPI dispatch is useful for eventual TE/touch/storage IRQs (M/M/M). **No cpufreq or ordinary idle governor exists in this driver**; its terminal WFI halt disables GIC/timer and cannot replace scheduler idle. | L for IRQ routing. Defer: our SGIs work, timer/PSCI already exist; importing sheng's SPI IPI workaround or terminal halt could break SMP/wakeup. CPU frequency already has better Fuxi thermal guards. |
| [ArmDTPlatform](https://github.com/UniXNU/ArmDTPlatform/tree/0d544705c2e89725caabc0542bb96bc8febd8c70) | 0016 `SweetPlatformExpert.cpp`, Fuxi matching from 0082, `tools/mkadt-fuxi.py`; board-specific nubs. | `ArmDTPlatform.cpp`: generic DT platform, requires real CHRP NVRAM proxy and creates compatible-matched device nubs; empty delete/exclude lists. | Generic enumeration/NVRAM proxy, not a missing display or GPU primitive (L/L/L). | M; defer replacement of a working board expert and its ADT contract. |
| [DarwinBoot](https://github.com/UniXNU/DarwinBoot/tree/d099354d0eab34df5088ca1ba89c9c88bc3260bb) | `shim/fuxi-loader/`, `tools/mkpayload.py`, `mkadt-fuxi.py`; temporary ABL Linux-image handoff and separate monitor core. | `DarwinBootPkg/Driver/DarwinBoot.c`, `BootArgs.h`, `PackedRamdisk.h`: UEFI GOP, segment loading, trust-cache and packed RAM disk handoff. | UEFI boot architecture, no runtime GPU stability fix (L/L/L). | L; defer. Darwin 23 boot_args and firmware-volume assumptions are not drop-in loader updates. |
| NativeAdreno | 0096/0098, `xnu/fuxi-adreno/`, K1 counters, HostImport, watchdog and DMA quarantine. | Existing audited K1 reference, same superproject pin; NativeAdreno submodule `cedcaa2949c795e71a47e78ae413c8a41b17d7d0`. | Already assigned/ported, GPU multi-client scheduling remains separate. | Excluded per task; preserve kernel ABI 2 and current ownership. |
| [NativeAES](https://github.com/UniXNU/NativeAES/tree/a34964b8c2fec2e83c8e4f68fb7d6fb7e4b2227b) | 0020 BearSSL MIT portable AES-CBC/ECB provider, FUXI configuration in 0082; existing AppleKeyStore software compatibility in 0038. | `NativeAES.cpp`, `OriginalAESABI.h`: software AES-CBC adapter subclassing original `IOAESAccelerator`; NIST KAT, descriptor/task overloads, raw software keys only. | Missing original-family registration, **not missing AES-CBC**. No hardware engine, SEP/device keys, XTS or GCM added by theirs (L/L/L absent a consumer). | M/L: reconstruct actual iOS 16.7 parent vtable/size first. Defer: no demonstrated 20H392 consumer blocked on this service, and the supplied ABI's 0x120 instance/virtual layout is for their original family. Cannot fix hardware keybags. |
| [NativeGeniSpi](https://github.com/UniXNU/NativeGeniSpi/tree/b5399b5646c4d7415deec6d887fd51715a97a024) | `bsd/dev/fuxi_touch.c` (0091/0093): SM8550 SE4 GPI DMA rings, FTS FIFO draining/reinit; Sweet FIFO GENI in 0060/0063. | `GeniSpi.c/.h`: callback-based FIFO register I/O, packing and bounded cancel/reset, FIFO width/depth discovery. | Shared bus abstraction/FIFO fallback, not our missing DMA transport (L/L/L). | M; defer: Fuxi relies on inherited GPI/FTS configuration, not sheng's FIFO transaction mode. |
| [NativeHidInput](https://github.com/UniXNU/NativeHidInput/tree/28f9b463b48e862f2def91d29c11b9143785715e) | 0058 IOHIDFamily sources + SweetHID events, 0091 FTS digitizer, 0092/H1 buttons; edge-origin flag already present. | `NativeHidInput.cpp`: original HID-device ABI, keyboard/consumer reports, normalized ten-finger descriptor and original-event edge flag hook. | Combined reports and original-family plumbing (M/L/L); no FTS hardware controller. | M/L; buttons explicitly belong to another lane. No iOS 17 vtable hooks copied. |
| [NativeKeyStoreEvents](https://github.com/UniXNU/NativeKeyStoreEvents/tree/e59bac4ad1ac8825919ae94da8440a64d8ab90b3) | 0038 `SweetKeyStore.cpp`: already publishes AppleKeyStore, no-passcode/unlocked state, DER device state and selector logging. | `NativeKeyStoreEvents.cpp`: matching notification service, opt-in `sheng-aks=1`, selectors 7/14/17/35 etc.; state only, no secure key operations. | Minor selector/notification differences, largely duplicate compatibility that even cites Sweet's iOS 16.7 behavior (L/L/L). | S/M if logs identify a missing selector; defer speculative successful replies. |
| NativeMetal | FuxiMetal plus external Turnip route, N1/T1 and subsequent Metal lanes. | Existing N1/T1 audit; submodule `a96feceda55e8fed1e20c0a2998736cef7aab006`. | Already assigned/ported. | Excluded per task; no sources vendored. |
| [NativeMobileDisplay](https://github.com/UniXNU/NativeMobileDisplay/tree/c031d882b9721d943383d11f66dc4748252ce929) | `SweetFramebuffer.cpp` (0046/0061/0079/0093/0096), `SweetSurface.cpp` (0068/0096/0098): absolute software tick, latest-frame queue, centered crop, asynchronous kernel GPU blit, one-in-flight poll, power-off black fill; 0087 TE autorefresh. | `NativeLegacyDisplay.cpp`, `NativeLegacyRequest.h`, `NativeGOPPresenter.cpp`: bounded scalar/device copy and shadow diff; client-demand software vsync; original-family retirement after actual copy; privileged userspace GPU offer/complete/abandon. `NativeMobileDisplay.cpp` itself is metadata prototype only. | Bounded copy and idle ticks (M/M/M). No actual panel TE/vblank/DSI power driver exists in this component. | S/M; **ported bounded/differential CPU copy and demand timer**, retaining Fuxi GPU path. Defer original-family request/retirement offsets and new userspace presenter ABI. |
| [NativeRamBlock](https://github.com/UniXNU/NativeRamBlock/tree/d4d22d9819b0162eda49f6402235dddc2279410d) | 0021/0022/0024 RAM-root/mockfs support; production root uses 0088 pinned-file HFS+ extents. | `NativeRamBlock.cpp`, `SparseDisk.h`, `PersistOverlay.h`, `SeedDisk.h`, `DirectDisk.h`: IOBlockStorage RAM disk, sparse seed, SHA-identified changed-page persistent overlay/store. | Alternate seeded Data filesystem architecture (M/L/L), not UFS timeout recovery. | L; defer: requires provisioned store windows/seed identity and a mount/data migration, none authorized here. |
| [NativeUfs](https://github.com/UniXNU/NativeUfs/tree/af785c1777161a62931425c2faaa536ce0bebd5b) | `sweet_ufs.c` (0028/0030/0034/0069/0084–0088): ABL host reuse, single slot/bounce, five-second timeout, optional sleep, extent-confined writes. `shim/ufs.c` is a legacy Sweet read-only MMU-off probe. | `TransferPoll.h`, `RootCommands.h`, `NativeUfs.cpp`, `SmmuTakeover.h`: OCS-confirmed completion, fatal detection, invalidate-only DMA reads, response/tag/LUN/residual validation, poisoned allocation retention; native SMMU ownership, IOStorage root/store windows. | High impact DMA lifetime gap: old timeout cleared slot then next command could overwrite descriptors/bounce before DMA ownership was proved (H/M/L). | S for policy, L for complete host replacement. **Ported completion/cache/response/quarantine policy**; no timeout retry or new SMMU/PHY/clock/partition ownership. |
| [NativeVirtioBlock](https://github.com/UniXNU/NativeVirtioBlock/tree/98909eb9198a4360d7a43be831887cb8b74d814d) | Physical UFS and RAM-root support; no virtio-mmio block frontend. | `NativeVirtioBlock.cpp`: modern split-ring MMIO block, optional VerifiedRoot authorization. | QEMU/testing transport; Fuxi firmware exposes no such device (L/L/L on hardware). | M; defer until a virtio test target is requested. |
| [NativeVirtioConsole](https://github.com/UniXNU/NativeVirtioConsole/tree/066f9bded311233ed56824e13a5628077619efda) | Loader monitor/log ring, framebuffer console and existing USB support; no virtio transport. | `NativeVirtioConsole.cpp`: modern MMIO queues, exclusive client and binary broker channel. | QEMU offline GPU broker rather than physical A740 acceleration (L/L/L). | M; defer, no Fuxi MMIO device to bind. |

## Kit comparison

| Kit | Ours today | Theirs, source examined | Gap and value (S/G/P) | Effort and disposition |
| --- | --- | --- | --- | --- |
| [AdrenoFirmware](https://github.com/UniXNU/AdrenoFirmware/tree/40fdb8ea87606612ac6f1e80bbb49955d0ce4051) | Loader `shim/fuxi-probe/gpu.c`, XNU `fuxi_adreno.c`: external GMU/SQE, HFI tables, SCM/ZAP, votes, GX/CP startup. | `AdrenoFirmware.c`, `AdrenoHfi.c`, `AdrenoHfiStartup.h/.c`, `AdrenoHfiVotes.c`: bounded staged HFI ACK sequence, explicit votes and single-attempt poison/retention contracts. BSD-2-Clause-Patent. | Reusable startup validation/ownership, board votes remain caller-supplied (M/H/M). | M/L; defer GPU power sequencing to its owner. No sheng bandwidth/ACD/IFPC vote table is assumed safe for Fuxi. No firmware copied. |
| [AdrenoPM4](https://github.com/UniXNU/AdrenoPM4/tree/445f8fe6d115738352b9d78dc70af6d61aa7f62f) | Existing PM4 framing/blits in `fuxi_adreno.c` and ABI backend; Turnip emits real graphics state. | `AdrenoPM4.h/.c`, `AdrenoShader.h`, `AdrenoVPC.h`, `AdrenoRaster.h`: structural plan/packet encoders and graphics bindings; explicitly not command authorization. MIT. | Alternative encoder abstractions, no new compositor capability by themselves (L/M/L). | M; defer redundant kernel encoding; cannot replace Turnip's state tracking. |
| [AirNir](https://github.com/UniXNU/AirNir/tree/032a867b5dc3121164bbd706e4e81e3b9fcd2b8f) | External replaceable AIR translator plus SPIR-V/Vulkan/Turnip; original Metal tests and frontend. | `AirNir.h`, `Frontend.cpp`, `MesaBackend.c`, `ConstantUploads.h`: offline AIR→NIR→IR3, constants and resource interfaces. MIT, with BSD-3-Clause `FusedF32.h`. | Different compiler architecture with Mesa coupling (L/M/L). | L; outside kernel driver lane. Do not vendor Mesa or compiler trees. |
| [ArmSmmu4K](https://github.com/UniXNU/ArmSmmu4K/tree/4d0d6598b1a36aeec3de01091893fa6660e49388) | 0096/0098 GPU native 4 KiB tables over 16 KiB XNU pages, alias/cache/import lifetime, TLB and fault containment; UFS ABL takeover. | `ArmSmmu4K*.h/.c`: table/stream/context planning, alias audits, live map/unmap with whole-graph verification and poisoned ownership. MIT. | Shared generic audits and storage isolation (M/H/L). | L; defer replacing proven GPU ABI backing, streams/context-bank ownership and quarantine. UFS isolation must first establish Fuxi stream IDs and all inherited users. |
| [NativePrelink](https://github.com/UniXNU/NativePrelink/tree/2b6ffb50117eee820b98aca9bd51b5c7dfb2970e) | Built-in XNU drivers and existing native builder; stand-ins implement required services. | `native_prelink.py`, `emit_prelink.py`, xnu-10063 sparse-index/security-init patches: original-kext relocation and prelink metadata. | Whole original-family/kernel ABI migration (L/M/L). | L; no iOS 17 binary layout, addresses or patches can be applied to 8796/20H392. No Apple binaries redistributed. |
| [VerifiedRoot](https://github.com/UniXNU/VerifiedRoot/tree/2854c61f6c4dfda870ac3d93e1f03d9181edb041) | Extent-bounded writable HFS+ image, permissive research AMFI (H1); no authenticated immutable root. | `src/verified_root.c`, `include/verified_root.h`, `adapters/xnu_crypto.c`: RSA3072/SHA256 manifest and block hashes, independent trusted anchor and generation/UUID checks. | Real owner-root integrity, separate from GPU/timeout fixes (M/L/L). | L; requires key provisioning, monotonic-generation policy and immutable-root/data design. Not silently grafted onto writable root or presented as restored Apple security. |

## What 0106 ports

### UFS: `fuxi_unixnu_ufs=1`

Adapted `drivers/NativeUfs/TransferPoll.h` and `RootCommands.h` to a small C
policy header plus our existing BSD driver; adapted `NativeUfs.cpp`'s real
64-byte-line CIVAC/IVAC maintenance. The allocation is already dedicated,
contiguous, aligned and permanent. Original SPDX/provenance lines and both
upstream licence notices ship with the patch.

Host takeover preserves inherited error evidence and refuses a fatal status before
changing the request list. Before **any descriptor write**, inspect the complete transfer doorbell and
fatal IS mask `0x71880`. Strategy and persistent-log writers also check before
touching the bounce buffer. On submission, publish descriptor/data cache lines
with real CIVAC and ordered MMIO; acknowledge only completion bit 0. While
polling, DB=0 must accompany OCS != 15 (invalid sentinel). This also accepts
fast completion where software never saw DB=1. Device-owned descriptor reads
use **IVAC only**, never clean/writeback over a potentially newer DMA result.
Successful completion invalidates descriptors/response and read data before
CPU consumption. Existing `ufswait` pacing and five-second deadline remain.

Fatal status or timeout permanently poisons the allocation; **no UTRLCLR,
reset or resubmission** occurs in this path. All subsequent commands and
strategy writes fail before buffer mutation; the allocation stays resident
until reboot. A malformed type/tag/LUN/EHS/length/sense/residual response is
also quarantined. Ordinary completed SCSI CHECK CONDITION or OCS errors remain
errors without pretending they are success. The existing three-attempt
ACTIVE/TUR startup loops can still consume completed status errors, but a
poisoned attempt immediately fails every later call without ringing again.
No new retries were added: upstream explicitly prohibits timeout retries while
DMA ownership is uncertain. The existing root extent/write/sync checks remain.

This is fail-stop robustness, **not recovery**: a genuine root storage failure
may leave iOS unable to proceed. The old default timeout/clear behavior remains
available with the option absent or zero. `shim/ufs.c` was audited, not changed;
its Sweet physical address/MMU-off assumptions do not belong in the Fuxi kernel.

### Display copies: `fuxi_unixnu_diff=1`

Ports the bounded copy-layout/plain copy/fill/differential helpers from
`drivers/NativeMobileDisplay/NativeLegacyRequest.h`. Diagnostic pixel scans,
legacy swap masks and private-family ABI declarations are not copied.

Differential copies compare 64-byte source blocks against a packed cached
shadow; changed blocks/tails get aligned volatile 64-bit stores to scanout.
The shadow is bounded to 64 MiB, allocated once, and freed with the framebuffer.
Allocation/geometry failure logs `unavailable` and keeps the ordinary path.
Only one composed **full-panel BGRA** CPU frame, with console muted using the
existing `sweetfb_noconsole=1`, is eligible. Fuxi's 1125×2436→1080×2400 centered
crop remains unchanged. Layout checks are adapted to permit the last visible
cropped row without requiring padding past the descriptor; the shadow now has
an explicit byte capacity. The plain helper handles unaligned-width tails
with scalar stores and validates every addressed row.

GPU submission stays first choice. Any accepted GPU copy invalidates the
shadow; GPU retirement still precedes display kick, next copy or black clear.
CPU partial/multilayer copies, rejected surfaces and power-off blanking also
invalidate it. After GPU use or wake, the next eligible CPU copy repopulates
the entire shadow. Default-framebuffer surfaces invalidate rather than reusing
a stale shadow. Writers must obey the existing producer/scanout ownership
contract; the option is unsuitable for other direct writers into scanout.
This does not add asynchronous producer fences or fix default SwapWait semantics.

With the option enabled, non-differential CPU copies use the bounded scalar
helper; without it, the old memcpy path remains. No new GPU service, GPU ABI,
userland client or firmware is needed. UniXNU itself notes diff was not faster
on sheng and keeps it opt-in after problematic early boots. Benchmark Fuxi;
reduced stored bytes alone is **not evidence of lower frame time or power**.

### Display scheduling: `fuxi_unixnu_idlefb=1`

Adapts the demand-driven timer/absolute-deadline policy of
`drivers/NativeMobileDisplay/NativeLegacyDisplay.cpp` to the existing
`AppleCLCD` client list and lock. No raw private-object offsets are used.
The present timer disarms when there is no pending frame, outstanding GPU
copy, required black fill or subscribed vsync client. Swap submission,
notification registration/port installation and display power requests wake
it under the same lock as its rearm decision. This avoids a lost wake when a
swap races the last callback. No new MMIO/power state is introduced.

Outstanding GPU copies continue polling at the existing cadence even on a
power-off request; black is written only after retirement. Black-screen swaps
stay pending without waking the timer once the clear is complete. Wake forces
the newest frame to redraw. Vsync subscribers retain continuous notification
behavior, including while off, to preserve the existing client contract.
Absolute deadlines skip missed periods instead of producing catch-up bursts.
The first idle/wake transition logs once each, avoiding per-frame log traffic.

This reduces **CPU timer wakeups**, not panel refresh. ABL INTF1 TE autorefresh,
DSC, physical panel power and brightness stay inherited. UniXNU's GOP display
also lacks real vblank/DSI panel-off control; its logical PM states explicitly
do not claim hardware power output. No transplanted register sequence can
supply that missing driver. Existing 60 Hz software vsync values remain the
**8796/iOS 16.7 six-word notification ABI**, not iOS 17's original-family sender.
No 120 Hz claim, physical vsync timestamp or extra boot argument is invented.

## Version adaptations and deliberate omissions

* **8796 versus 10063:** retain our built-in `SweetFramebuffer` and `SweetSurface`
  classes, iOS 16.7 selectors/0x46c record, crop and six-word notification;
  reject private original-family object offsets (for example vsync client list
  at +5592) and iOS 17 virtual-slot declarations. Do not prelink either original
  display or crypto family. The prototype `NativeMobileDisplay` is not their
  functioning legacy/GOP presenter and is not a replacement driver to register.
* **NativeUfs integration:** C/errno/BSD single-slot path replaces their C++
  templated IOReturn/IOBlockStorage plumbing. Reuse our DMA layout, 16 KiB XNU
  pages, 64-byte ARM cache lines, Fuxi boot host and pinned-file extent policy.
  No NativeRamBlock store windows, WRITE/FUA policy, SMMU bank selection,
  APFS/verified-root publication or clocks/PHY reset are copied.
* **ArmCpu:** ordinary WFI already lives in XNU 8796; frequency scaling lives
  in 0097's EPSS/TSENS policy. UniXNU `haltCPU` is terminal, and no frequency
  implementation exists there to port. Do not disable GIC/timer on ordinary
  idle or duplicate the boost governor. SPI dispatch/TE IRQ work is deferred
  until controller routing can be validated. Buttons remain another lane.
* **NativeAES:** iOS 16.7 already boots with portable AES-CBC/ECB. Theirs delegates
  to those same legacy APIs and implements only raw software CBC keys. Its
  private `IOAESAccelerator` parent ABI requires 20H392-specific verification
  and an actual blocked consumer; adding its iOS 17 layout would not provide
  hardware keys/SEP or accelerate the GPU. No AES service or crypto boot arg
  is added on speculation.
* **GPU scanout:** our 0096/0098 path already pins sources, maps/imports, submits
  blits, retires fences and falls back after containment. Theirs delegates the
  scanout copy to a privileged userspace offer/wait/complete client. Replacing
  ours would add another ABI/process lifetime without solving a demonstrated
  gap. Existing K1/N1/T1 remain the owners of GPU/Metal/Turnip work.

## Reproduction and tests

```sh
python3 tools/sync-fuxi-unixnu.py
python3 tools/sync-fuxi-unixnu.py --check
python3 tools/test-fuxi-unixnu.py
```

The sync tool replays every preceding relevant numbered patch with `git apply`,
compares canonical bytes, generates **0106-fuxi-unixnu.patch**, and checks the
result applies over that baseline. No new translation unit or build list is
needed; headers/licences are installed beside the including sources. The
normal builders enumerate the patch directory, so 0106 needs no CI option.
Number gaps 0104/0105 are intentionally unclaimed by this lane.

Host tests compile the **actual ported** UFS submit/read/write/strategy and
framebuffer present/tick/swap/rearm methods under ASan/UBSan. Stubs replace
MMIO, clock, IOKit surfaces and cache hardware, not completion/copy policy.
They cover fast/delayed/phantom completions, timeout/fatal/invalid response,
cache and sleep combinations, byte-identical DMA buffers after quarantine,
extent write refusal, and unchanged default timeout/clear. Copy tests cover
padded/cropped rows, alignment, last-pixel bounds, shadow capacity, dirty
blocks/tails and arithmetic overflow. Display tests cover GPU pending/retire/
fallback, CPU→GPU→CPU shadow invalidation, black fill/wake, off-screen swaps,
notifications and both sides of idle/wake ordering. The actual ARM cache
and copy routines are also cross-compiled with `--target=aarch64-none-elf
-mgeneral-regs-only -Wall -Wextra -Werror`.

Also passed: `test-fuxi-h1.py`, `test-fuxi-adreno.py`, `test-fuxi-metal.py` and
`test-fuxi-perf.py`, including existing GPU DMA/async-source/containment tests.

The old build log has no compile commands, but its component `.CFLAGS` and
`.CXXFLAGS` are present. With those saved flags and actual 8796 exported/kernel
headers, **both complete modified translation units passed** an arm64 Mach-O
syntax check with `-mgeneral-regs-only -Werror`. Two existing C++ designated-
initializer warnings in XNU allocation macros are suppressed; Apple-only
allocation type builtins use the same syntax-check stand-ins as `xnu-cc.py`.
Only our canonical sources and the patch-reconstructed `SweetSurface.h` plus
our `fuxi_adreno.h` are staged in a temporary directory. No reference source
or build output is modified. Reproduce this optional check with:

```sh
python3 tools/test-fuxi-unixnu.py --xnu-buildtree "$XNU_BUILDTREE"
```

These models/checks do not prove bus accessibility, cache coherence, a physical
TE, source-producer synchronization, thermal benefit, kernel linking or UI
pixels. A full macOS 8796 compile/link remains required before device testing.

## Device acceptance plan — not executed

Build 0106 through the normal FUXI kernel builder later; retain the same ADT,
RAM layout, root extent table, external firmware and baseline boot command
line. Prepare separate temporary payloads differing **only** in these options.
No root edit or new userland executable is required. Do not change the baseline
`ufsrw`, `ufswait`, `ufscache`, `fuxiperf` or GPU client policy during the A/B.

1. **Control:** all three options absent (or `=0`). Expect existing
   `SWEET FB: present up to 60 Hz (every 1 ticks)`, root extent/mount and GPU
   readiness lines; **no `FUXI UNIXNU` lines**. Unlock, scroll Settings, lock and
   wake; confirm baseline behavior. Save the usual display/GPU counters/log.
2. **UFS only:** add `fuxi_unixnu_ufs=1` to that command line, keeping the existing
   `fuxi_ufs_probe=1` needed for storage. Expect exactly:
   `FUXI UNIXNU UFS: guarded completion, fatal/timeout quarantine enabled`, then
   `FUXI UFS device ACTIVE power command result=0`,
   `FUXI UFS: root file in userdata: <N> extents, <B> blocks`, mount/launchd and
   SpringBoard. Open/read apps and Settings for 15 minutes. If baseline already
   permits root writes, create/read/remove a disposable file **inside that root
   image** and verify persistence after orderly reboot; do not enable additional
   write targets. Expect no `quarantined`, timeout, UFS response errors or panic.
   Check the same files still read correctly. A real error must produce one
   `FUXI UNIXNU UFS: quarantined reason=<preflight|fatal|timeout|response> db=... is=... ocs=...; no retry until reboot`;
   subsequent requests return errors, never new doorbells or buffer reuse.
   Do not induce a physical storage fault: timeout/fatal injection is host-only.
3. **CPU display diff only:** add `fuxi_unixnu_diff=1 sweetfb_noconsole=1` and use
   a separately prepared CPU baseline (`fuxigpu=0` in both halves of this A/B).
   Expect `FUXI UNIXNU FB: differential copy enabled (CPU full-panel BGRA only)`
   and `FUXI UNIXNU FB: diff 1 frames stored <X> of <X> bytes` on the first eligible
   frame; at 1080×2400 X is **10368000**. Scroll, pause on a clock/static Settings
   page and scroll again. After 600 eligible frames expect
   `FUXI UNIXNU FB: diff 600 frames stored <S> of 6220800000 bytes`.
   S should be lower when content is unchanged. Compare existing
   `SWEET FB: stats ... copy avg ... max ...` against control; keep disabled if
   frame time worsens, pixels stale, edges/padding corrupt or UI freezes.
   Lock/wake repeatedly; first post-wake CPU diff must refresh the full panel.
4. **Demand timer only:** add `fuxi_unixnu_idlefb=1`. Expect
   `FUXI UNIXNU FB: demand timer enabled, software 60 Hz, panel power inherited`,
   then first `FUXI UNIXNU FB: timer idle`; the next swap or subscription should
   log first `FUXI UNIXNU FB: timer wake` and present within the normal tick.
   Pause on static UI, scroll, lock, tap the black screen, wake and scroll again.
   No stuck black display or lost first frame is acceptable. No idle log is
   expected while an active vsync subscriber keeps requesting ticks; check
   selector-72 `notifications type 5 on/off` logs before attributing this to a bug.
5. **Combined GPU path:** same GPU-enabled baseline, then all three options plus
   `sweetfb_noconsole=1`. Exercise the existing admitted Metal/GPU smoke route,
   scroll and lock/wake while frames are queued. Existing
   `FUXI GPU: presents GPU <N> CPU <N> ... busy ticks <N> failed <N>` must show
   successful retirement; no black clear or CPU copy may race an in-flight
   GPU write. Diff counters advance only for eligible CPU fallback frames.
   Use the existing separately reviewed GPU fault/containment test to validate
   fallback if desired; do not introduce new device fault injection here.
   Verify full CPU repaint after fallback and no SMMU fault, DMA lifetime panic,
   stale shadow, UFS quarantine or input regression during a 15-minute run.

Record counters, frame-time distributions, dropped swaps, thermal readings and
screen observations for each control/option pair. An idle log or lower byte
count does not establish lower power; compare existing read-only thermal/clock
telemetry with the same workload, startup temperature and monitor settings.
If any option regresses, omit just that option on the next temporary boot;
reboot is required to clear a quarantined UFS allocation. No runtime toggle,
flash operation or automatic deployment is part of 0106.

## UniXNU 2026-10-24 port

Offline review of the supplied upstream diffs and two saved boot logs. Sources
remain canonical in `xnu/fuxi-adreno/` and `xnu/fuxi-unixnu/`, synchronized into
0098 and 0106. New patch **0110** integrates the lower-directory check into
XNU's existing union traversal. All three new switches require the exact
integer value **1**, default off. No deployment or hardware test was performed.

### Sources and dispositions

| Project / repository | Commit and license | Result |
| --- | --- | --- |
| [UniXNU NativeAdreno](https://github.com/UniXNU/NativeAdreno) | `b686b9054f57d393c80195b82568a09f8ca106a2`, MIT | Adapt writable-import cache handoff, retaining completion invalidation; `fuxigpuncimport=1`. Existing `LICENSE.NativeAdreno` retained. |
| [UniXNU ArmSmmu4K / Adreno740](https://github.com/UniXNU) (`research/unixnu-new`; commit ids not readable, files compared) | MIT | Adapt: cached overlap mirror for caller-owned maps, one segment lookup per run in BoImport, ranged TLBIVAL for small unmaps (`fuxigpuncimport=128`), coherency-proven skipping of import cache maintenance (`=64`). See metal.md "GPU map/unmap cost". |
| Same | `26220f1ab79fec643517336fbd81574bd1e0d05a`, MIT | Already present: `fuxi_abi_wire.h` programs IFPC off (`P1(0x65,0x101)`); no power sequencing change. |
| [UniXNU NativeMetal](https://github.com/UniXNU/NativeMetal) | `c6d29caad6d880cc8204ebe3bc34d77479a3255d`, MIT | Adapt screen-delivery priority to the kernel timer callback; `fuxifbprio=1`. Existing `LICENSE.NativeMetal` retained. No NativeMetal source tree modified or vendored. |
| Same | `dce1bdc468716bfa9e8f58ac05bfacdec6b6128a`, `eed65758722bd4a1219de5161a8ed9ef0bc547da`, MIT | Already at the final setting: `ClientSetup.hpp` supplies `TU_DEBUG=sysmem,nocb` without overwriting an explicit environment value. Do not restore the superseded `nobin,nocb`. |
| [UniXNU NativeMobileDisplay](https://github.com/UniXNU/NativeMobileDisplay) | `8666a9950c9699b368931d18cac92c0fdb2ce9c0`, BSD-2-Clause-Patent | Reviewed; retirement timing is inapplicable to our current immediate-success SwapWait stub. No `fuxiearlyretire` switch or early-retirement counters added; explanation below. |
| Same | `c99046af4ac1cecfcef2575bb0025d2e21e005a4`, BSD-2-Clause-Patent | Deferred: no original-family `getRefreshAttributes` implementation/interval layout exists in our reconstructed framebuffer ABI. Our software timer is already fixed at 60 Hz; this does not prove what userland assumes for unimplemented refresh queries. |
| [UniXNU superproject](https://github.com/UniXNU/UniXNU) | `cb0749940c598f2bd7fb113c1a660fcc2fd825ce`, BSD-2-Clause-Patent | Port lower-directory eligibility/iocount handling; `fuxiuniondir=1`. Notice retained in `LICENSE.UniXNU`. Skip unrelated MobileSafari lookup tracing. |
| Same | `477e530a8689af4a09eacb4eb9bdb878c737d8b6`, BSD-2-Clause-Patent | Inapplicable: no `native-4k-signature-hash-cache.patch`/`cs_native4k_prepare` in our patch stack. Our 16 KiB kernel uses the existing signature path, not their native 4 KiB rehashing pass. Adding 1 MiB reads/IO_NOCACHE without that consumer has no effect. |
| Same | `11c818217b7ba90fb8d3e356d4f1ede88f952cf6`, BSD-2-Clause-Patent | Inapplicable: no `native_sandbox_grant`/`NativeSandboxGrantProcess` machinery. Patch 0050 registers a permissive Sandbox policy with no `mpo_iokit_check_set_properties` hook. Do not add a second bypass for a restriction this policy does not impose. |
| Same | `52d18cef78f362d7ca0c72865fc97d1b98bf4a15`, BSD-2-Clause-Patent | Already covered: `Translator::cacheIdentity` requires `fuxi_m2v_cache_identity`; its SHA-256 covers translator pin, adapter source, translator `SOURCE_DIGEST`, semantics and artifact versions. `Backend.hpp` requires this identity for persistent products; `ArtifactCache.hpp` validates it on load. Missing identity keeps memory-only operation. No upstream build script or padded-row translator patch copied. |

Both UniXNU and HackiOS are already credited in README. The supplied HackiOS
kernel diff changes the APFS extractor prerequisite and QEMU's ramfb height,
which do not affect this physical panel/GPU path. The userland diff adds a
thermal notification daemon for `kern.redwood.thermal_*`, a stress probe, and
boot-state writes for its bundle loader/store layout. Fuxi exposes a different
thermal API and uses a different loader/storage contract. These are skipped,
not assumed to be GPU fixes; their diffs contain no commit identifiers, and
no code from them is copied. Existing credits identify
[HackiOS kernel](https://github.com/oosexgang/hackios-xnu-kernel) (APSL 2.0) and
[HackiOS userland](https://github.com/oosexgang/hackios-xnu-userland) (MIT).
No unrelated older superproject patch inventory is imported. Reference diffs
were read only, never executed or built. No GPL code, firmware, Apple binaries,
Mesa/metal2vulkan/NativeMetal source changes, or research artifacts are included.

### OBSERVED: import work and traces

The production `fg_abi_import_cache()` previously CIVACed **every mapped
import** before every submission and IVACed each at completion, under the GPU
mutex. Imports already use Normal NC, outer-shareable SMMU leaves (`wb=0`,
`sh=2`); copying upstream's coherent/non-coherent mapping change is unnecessary.
Read-only GPU permissions do not mean the CPU's bytes stayed unchanged, and
PM4 can reference resources indirectly. An IB-only cache walk would be wrong.

Saved `boot-cores98.log` reports four IOSurface-import admissions and
`boot-use95.log` eleven, with row stride 4608 and allocation **11,239,424 B**
for 1125x2436 BGRA. These are log events, **not simultaneous live-import
counts**. Both contain 17 successful map trace rows, but the 64-row ABI trace
budget mixes ordinary BOs/imports and stops early. There is no complete live
import-byte census in either log; multiplying those event counts as if all
were resident would overstate the evidence.

One allocation covers **175,616 cache lines** at 64 B/line, hence **351,232
cache instructions per submission/completion pair**. For N such live imports,
the old walk executes `351232 * N` operations even if only one is used.
After the initial map-time clean, the option saves `175616 * Nw` CIVACs per
submission for Nw writable allocations; completion IVACs remain.

`boot-cores98.log` has four nonempty frame-trace windows (2, 1, 78 and 13
samples). Its `backend_submit` p50/p90 pairs in ms are **1.034/4.194,
1.512/1.512, 1.262/1.534, 1.874/1.967**; corresponding `gpu_wait` pairs are
**2.504/2.943, 2.781/2.781, 1.856/4.338, 7.173/7.543**. These include other
backend/wait work; they do not isolate cache cost or establish upstream's
roughly 60 ms/frame claim on this board. `boot-use95.log` has no frame-trace
samples. Its scanout summary reports 120 frames, 118 GPU/2 CPU,
copy average **1231 us**, maximum **2227 us**, `deferred=730`.

### GUESS: cost and likely benefit

Cache cost depends on dirty residency, memory traffic and CPU frequency.
For one logged allocation, a hypothetical average 10 ns per maintenance
instruction makes the old pair about **3.51 ms** and the skipped half about
**1.76 ms**; at 1 ns these are 0.351/0.176 ms. These are sensitivity estimates,
not measured latency or predicted savings. The sampled submit times argue
against asserting a persistent 60 ms submit penalty here. Multiple resident
imports can still turn the all-import walk into meaningful lock contention.
The new live-count/byte and cumulative-maintenance counters enable an actual
before/after comparison; no UI speedup is claimed from host tests.

### Cache ownership and diagnostics: `fuxigpuncimport=1`

This opt-in borrows upstream's immutable CPU-pixels assumption only for GPU
writable imports (permissions bit 2). It CIVACs the whole import once on a
successful map, before the GPU lock permits any submission. Subsequent submits
skip its CIVAC. **No CPU writer, including an alias or a CPU fallback renderer,
may modify that allocation until it is unmapped**, even between GPU fences.
The existing ABI has no dirty-generation protocol to prove this: writable GPU
permissions alone are insufficient. Disable the option for mixed CPU/GPU
writers or CPU initialization after mapping. The ordinary default remains
submission-scoped cache handoff and permits synchronized CPU updates.

Unlike upstream, completion and confirmed-drain fault paths still IVAC all
imports before publishing successful fences or releasing memory. This retains
CPU-reader/pixel-oracle visibility across frames without cleaning stale CPU
lines over GPU writes. Failed DMA drains still quarantine imports, with no
cache operation/release that implies completion. Read-only imports retain
normal submit cleaning and completion invalidation. The map attributes, PM4,
ABI version, watchdog and shared-ring exclusion are unchanged.

The existing 600-frame `FUXI GPU: presents` line now appends `imports`, `mapped`,
`cache cleaned`, `skipped`, and `invalidated` bytes. Import count/bytes are a
locked live snapshot; cache-byte counters are cumulative since boot and include
initial map cleans. Counter reads release the mutex before printing. With the
option absent, skipped bytes must stay zero. Compare deltas, not absolute
cumulative counts, and check the live byte census before estimating cost.

### Presentation priority and retirement

`fuxifbprio=1` selects `kIOTimerEventSourceOptionsPriorityHigh`, which maps to
`THREAD_CALL_PRIORITY_HIGH`. This is where `vsyncFired` actually polls the
copy, invokes `present`, and kicks the panel, while holding the work-loop gate.
Raising only the IOWorkLoop thread or FuxiMetal's pipeline compilation queue
would miss this callback. The old timer already used KERNEL_HIGH; NativeMetal's
reported user-default queue contention and measured benefit are not assumed
for Fuxi. Default scheduling and the 100 ms ABI watchdog priority stay intact.

`deferred` counts **polls**, including two sites: an unsuccessful scanout
ownership acquisition (mandatory 20 ms drain, transfer still active, busy GPU,
or a held/error state), and a source-writer/ring deferral in `present`. It is
not a frame-drop or scheduler-lateness counter. Pending swaps stay queued and
writers are blocked during the in-flight copy. A higher priority cannot bypass
the drain, GPU fences or the source lease. The fixed 20 ms guard already limits
sustained manual-transfer pacing to less than 50 Hz before copy overhead;
reducing it without panel completion evidence risks tearing and is not done.

Upstream 8666a99 moves its original-family completion earlier because its
compositor waits for retirement. Here selector 6 (`SwapWait`, seen with three
scalar inputs in both logs) already returns success immediately; `SwapEnd`
queues the latest frame and also returns immediately. There is no owned
original-family swap request, `completeCurrentSwap`, completion-report queue,
or implemented retirement notification to move. The GPU copy retains its own
SweetSurface reference until the fence; `fGpuPending` prevents a following
copy/black fill, and producer surface leases prevent reuse during that copy.
Thus producer acknowledgement is already earlier than upstream's change.
Adding `fuxiearlyretire=1` and incrementing counters here would label an event
without changing retirement. A real implementation first needs the iOS 16.7
SwapWait/completion ABI and generation-aware pending/superseded/error handling;
this port does not invent those semantics or claim early-retirement speedup.

Likewise c99046a changes a private original-family refresh method that we do
not implement. No original vtable offsets or guessed selector outputs are
introduced. Fixed 60 Hz software notifications do not by themselves implement
refresh attribute reporting.

### Union lookup: `fuxiuniondir=1`

After successful lower-layer `namei`, a non-directory vnode cannot contain the
missing child. The opt-in helper releases that vnode's iocount and returns
ENOENT; directories preserve the existing successful handoff. The caller still
runs `nameidone`, initializes the output to NULL, and preserves the original
namei failures/root handling. This addresses the upstream lower symlink versus
upper directory case without adding per-lookup tracing. It changes an errno,
so it is gated even though no files or mounts are changed by the option.

### Validation and remaining hardware work

Production-code ASan/UBSan models cover opt-in/default/invalid values, initial
map handoff, RW/RO indirect imports over repeated submits, byte counters, busy
and rejected maps, watchdog completion, drained/undrained faults and retained
ownership. Existing scanout tests retain busy-source/ring, delayed publication,
CPU fallback, blanking and full-pixel checks. The new port test checks actual
lower-vnode policy iocount/errno ownership and default/SWEET/FUXI priority
selection. Full modified translation units pass ARM64 syntax checks using saved
XNU headers: GPU driver, framebuffer, surface, display kick and union lookup.
Patch sync/application checks cover 0096/0098/0100, the preceding framebuffer
stack plus 0106, 0108, and 0110 against the local saved lookup source.

The broad Linux run passed **45** default-invocation host scripts (including
this port's new model). `test-fuxi-metal-stage.py --policy-storage-only` and
`test-fuxi-metal-m9i.py --compile-only` also passed. Full staging and WLAN-artifact
checks could not run because the staged bundle/build outputs are absent; the
full M9i run requires a supplied translator. Other translator/SDK/firmware
artifact tests and macOS-only tests were not run. No downloads or reference
builds were used to fill those gaps. Matching sync checks and the downstream
0109 backlight overlay check passed as well.

For a later authorized hardware A/B, add only one option at a time. For the
cache option first establish that mapped writable imports have no CPU writers;
compare maintenance-byte deltas and frame-trace submit/wait percentiles, and
check pixel validation, scrolling, CPU fallback and lock/wake. For priority,
compare frame intervals under app-launch load while retaining identical drain
settings. For union lookup, exercise the lower-symlink/upper-directory case and
ordinary directory traversal. Scheduling latency, real cache coherency, visual
correctness, frame pacing and performance remain **unverified on hardware**.
The cmdline additions cost 18, 13 and 15 bytes respectively including one
separator (`fuxigpuncimport=1`, `fuxifbprio=1`, `fuxiuniondir=1`); they need not be
enabled together. No early-retirement/refresh switch is added without a matching
ABI consumer.

## AdrenoMetal / UniXNU Turnip and runtime tuning port

Offline review, nothing built or run on a device; every effect below is **not yet
observed**. Sources (read only): [AdrenoMetal](https://github.com/UniXNU/AdrenoMetal)
`ce5e19e33e05e1873d38d818e23cabfe0ac708c3`, MIT, and the UniXNU `turnip-darwin-kmd.patch`
newer than our pinned `fb6aad6`, BSD-2-Clause-Patent. Checked against metal2vulkan `720ef1ce`
(downloaded for a dry apply only): AdrenoMetal 0001-0016 apply in order with zero fuzz, and
0009-0015 also apply on the unpatched pin. The NativeMetal pin `a96fece` ships only 0001-0007
(0003, device entry-point priming, is therefore already in the native stack).

| Item | Result |
| --- | --- |
| Let Turnip choose tile or system memory per pass | Already available: `fuxigpugmem=1` gives `TU_DEBUG=nocb` (autotune). Default stays `sysmem,nocb`: the earlier GMEM trial changed nothing (gpu-translation.md) and A740 GMEM hangs are on record. AdrenoMetal pairs it with patch 0016 (tile stores kept in a texture's rows), which only matters under its `AliasWrites` policy; nothing here selects that policy, so 0008/0016 are not carried. |
| System memory in every Metal process | Already done: `setupMetalClient` sets `TU_DEBUG` for every admitted process (not only backboardd), preserving an explicit value. |
| No concurrent binning in every process | Already done: `nocb` is in both defaults. |
| Cache device answers that take an argument | Not applicable: `FuxiMTLDevice` answers from constants and one backend field, it does not query a runtime per call. AdrenoMetal's cache lives in NativeMetal's own frontend, which the native helper does not use. |
| metal2vulkan 0009-0015 | Ported as `tools/patches/metal2vulkan-adrenometal-*.patch` (MIT header and source revision kept), applied after NativeMetal's 0001-0007 by `build-fuxi-nativemetal.py`; `FUXI_NATIVE_M2V_DRAW=0` omits them. Only the `fuximetal=native` helper gains them: the default FuxiMetal bridge uses the runtime for binding plans only and its translator checkout stays unpatched (0013 touches `bind/`). Expected effect: fewer allocations and rebuilds per draw on the native path; none on the default path. |
| 0003 device entry-point priming | Already in the native stack (NativeMetal pin). Default path does not use runtime startup priming. |
| Fence wait up to 100 ms a call | Ported as `tools/patches/mesa-fuxi-fence-wait-100ms.patch` (after `mesa-fuxi-smoke.patch`, verified with zero fuzz against the reconstructed file). The kernel `WaitFence` polls to the fence or the slice (clamped to 1 s), so fewer IOKit round trips per frame wait, same latency. |
| OS-image shader priming, linked visible functions, zeroed constants | Not ported. It needs compute pipelines, function constants, linked functions and libraries over 4 MiB, which `Frontend.mm` refuses by design; it also spends CPU on all 166 OS libraries while frames run. `fuxigpucache=1` already persists the artifacts and Vulkan pipeline cache of pipelines actually used. A future seeding pass should replay recorded pipeline descriptors instead. |

Test: `tools/test-fuxi-patch-stack.py`.

## Adreno740 2026-10-24..26 submission and ring port

Offline port of the recent Adreno740 (`NativeAdreno`, MIT) submission and ring
work into `xnu/fuxi-adreno/`, synchronized into 0098/0100/0106 by the sync
tools. The upstream history could not be read in this session (only its current
files), so the commits below are named by subject and file; record exact hashes
at the next authorized review. No device was touched: every device effect is
**not yet observed**.

| Source (repo `Adreno740`, MIT, path) | Result |
| --- | --- |
| `NativeAdrenoHandleIndex.h` ("find objects through a handle index") | Adapted as `FuxiHandleIndex.h` (header and `LICENSE.NativeAdreno` kept). The user client looked a BO up by walking all 1024 slots for every selector and every IB; it now uses an open-addressing index (2048 cells, backward-shift delete) plus a free-slot hint. Slot choice is unchanged (lowest free). The BO cap stays 1024; the `peak_bos` log field says whether it must grow. |
| "a whole frame of up to 128 IBs in one submission" | Applied. `NativeAdrenoAbiMaxBuffers` 16 -> 128, `NativeAdrenoAbiSubmit` 3104 bytes, `FG_ABI_FRAME_WORDS` 581. Because the backend resets the CP context every submit, a frame split into chunks of 16 lost state between them; one record keeps it atomic. Turnip is built against this header (`-Dtu-darwin-abi-dir`), so the upstream KMD chunking and `mesa-fuxi-smoke.patch` (which refuses `total > NativeAdrenoAbiMaxBuffers`) follow the new constant with no patch change. The kernel image and the root's libvulkan are updated separately, so the old 416-byte record (up to 16 IBs) is still accepted for method 5 only; any other size is refused. |
| "write WPTR as a fenced register and keep the ring's register window alive" | Fenced write applied, opt-in `fuxigpuncimport` bit 1 (value 2). After writing CP_RB_WPTR, read GMU `AHB_FENCE_STATUS` (dword 0x23b13, bit 0); if set, write `_CLR` (0x23b14) = 7 and write WPTR again, at most 200 times. Default off keeps the single plain write. "Keep the window alive": already true, all `fg_windows` are mapped in `kernel_map` for the lifetime of the driver. |
| "reset BR/BV context state when the submitting client changes" | Already have, as a superset: `fg_abi_frame` emits the CP context reset on every submit, and the present path resets its own. Skipping it for consecutive submits of one client is a possible later optimization, not done. |
| Fence-stall handling (bus drain, WPTR re-issue, snapshots) | Applied, opt-in `fuxigpuncimport` bit 2 (value 4). A job pending 30 ms gets a CP/SMMU/IB snapshot, an ASID TLB invalidate and the same WPTR again; at 60 ms the GPU bus is drained (GBIF halt, wait for ack, release); the existing 100 ms deadline is still fatal. A job that completes after a stage is counted. The snapshot (without the recovery) is also appended to the fatal hang dump. |
| "stop emptying the GPU TLB on every submission" | **Not applicable.** Their table is one table: a map fills only invalid leaves, unmap invalidates, and tables above the leaves never change, so nothing needs a flush at submit. Here the client page table (`FG_ABI_PT`) and the present table (`G3_PT`) are different roots under the same ASID, and each submit swaps TTBR between them; the same ASID would otherwise serve stale entries of the other root, so the invalidate stays. Unmap keeps its own. Untested ideas: a separate ASID per root, or restoring `G3_PT` lazily. |

`fuxigpuncimport` is one bitmask parsed at one site (1 import cache handoff, 2
fenced WPTR, 4 stall recovery, 64 coherent-import self-test, 128 ranged TLBIVAL);
the value `1` behaves exactly as before and no cmdline bytes are added.

New log fields. `FUXI GPU STALL:` lines (only with bit 2, or on the fatal
path): `pending 30 ms ...`, CP/RBBM state, IB1/IB2 base and remaining, SMMU
fault record, and 16 words under BR-IB1, BV-IB1 and BV-IB2 (`unread N` counts
words outside the mapped range; `seq a/b` with a > b means the buffer was
mapped after the submission). The 600-frame stats window adds one line when any
submit ran: `FUXI GPU ABI: submits N ibs N max N over16 N peak_bos N wptr writes
N dropped N repeated N stuck N stall kicks N drains N acks N/N kick_ok N
drain_ok N snaps N`. `over16` growing proves the 128-IB record is in use.

Tests: `test-fuxi-metal.py` (wire sizes and 1..128-IB PM4 decode, 128-IB frame
across the ring wrap, fenced WPTR off/on/retry bound, stall stages and
snapshots, bitmask parsing), `test-fuxi-gpu-k1.py` (handle index after churn,
128-IB and legacy 416-byte records through the real user client),
`test-fuxi-hostimport.py`, `test-fuxi-adreno.py` (fenced doorbell compiled into
the present path), `test-fuxi-unixnu.py`. `test-fuxi-turnip-submit.py` (native
macOS) was updated by inspection only.

## GPU fence interrupt: `fuxigpuncimport` bit 26

Offline, 2026-10-30. Nothing here has been run on the device. Every device
effect below is **not yet observed**. Opt-in: add 67108864 (bit 26) to the
`fuxigpuncimport` value. This adds one cmdline character, for example the
current boot base 2948728 becomes 70057592. Bits 22-25 belong to other lanes
(22 DPU SMMU survey, 23-24 reserved for DPU scanout, 25 PAC self-test) and are
not read by this code. The other bits behave the same when bit 26 is clear.

### Sources and licences

| Source | Used for |
| --- | --- |
| UniXNU-sheng `package/GpuPowerProbe.cpp` (`gpuIrq`, `ringIdle`, `irqAndTimer`; revisions `b0ccf61`, `36ec562`), BSD-2-Clause-Patent | Handler shape: clear `RBBM_INT_0_STATUS` through `RBBM_INT_CLEAR_CMD`, then wake the waiters. Waits are at most 1000 us a slice. The SPI is masked after a run of empty interrupts (sheng uses 100k; we use 1000). |
| UniXNU ArmCpu `armcpu_spi_attach` (`1951cc8`, not re-verified from here), BSD-2-Clause-Patent | Distributor sequence: ICENABLER and an RWP wait, IGROUPR, IPRIORITYR 0xa0, ICFGR, IROUTER set to the boot CPU's affinity, ISENABLER, then a readback in the form group\|en<<1\|edge<<2\|prio<<8. Also: mask an SPI that has no owner. |
| Adreno740 `NativeAdrenoSubmit.cpp` @ `f53000c` (`native_adreno_fence_pending`), MIT; sheng `BootLog.c` @ `e34beda` (`[gpu pending]`) | Idea for a pending-time histogram. Our buckets are in microseconds and are our own. |

The notices are kept in `xnu/fuxi-adreno/LICENSE.ArmCpu` and
`LICENSE.UniXNU-sheng`, installed as `bsd/dev/LICENSE.*` by 0098. The code is
`xnu/fuxi-adreno/fuxi_gpu_irq.inc`, included at the end of `fuxi_adreno.c`. No
reference code was built or executed.

### Interrupt routing on Fuxi (observed in our sources)

- `pe_fiq.c` (patches 0004/0017) programs only the redistributor (SGI/PPI
  priority 0xA0, group 1) and `GICD_CTLR` ARE_NS\|Grp1.
- In `sleh_irq`'s SWEET branch, an INTID that is not spurious, not the vtimer and
  not an SGI calls `cpu_data->interrupt_handler(target, refCon, nub, intid)` if
  one is set, then EOIs (EOImode 0).
- Before this change nothing set that handler, and SweetCPU does not use
  IOCPUInterruptController. An SPI can therefore be enabled, given a priority,
  configured level or edge, routed with IROUTER (ARE on, `mkadt-fuxi.py`
  insists on GICD 0x17100000) and dispatched by the per-CPU handler.
- The SPI is routed to CPU 0 (core 0), never to core 1, which runs the loader's
  USB monitor. `ml_install_interrupt_handler` is run while bound to each CPU in
  turn, ending on CPU 0, whose MPIDR (`& 0xff00ffffff`) becomes the route.
- 0018/0065, `pe_fiq.c`, `sleh.c` and per-CPU startup are not touched.

Source: SPI 300 / INTID 332, level-high (`kgsl_3d0_irq`). Only
`RBBM_INT_0_MASK` bit 20 is unmasked (CP_CACHE_FLUSH_TS). The client frame's
BR fence already asks for it (`0x88000031`), and the present copy's fence does
not. The handler clears only bit 20, so the fault bits that `g3_faulted` reads
from raw status are kept.

### Behaviour

- **Attach.** Happens once, at the first WaitFence that would wait. It refuses
  and keeps polling if: the GICD does not map, ARE_NS is clear, there are too
  few lines, the GPU is not live, MMIO fails, or RWP is stuck. On a readback
  mismatch it masks the SPI and restores the old mask.
- **Wait.** Read the interrupt count, then poll. If the fence is still pending
  and its value is unwritten, sleep until the interrupt or 1 ms. If the value is
  written but the ring is still draining, poll at `fuxigpuwaitus`; a present copy
  queued behind raises no interrupt. A race between the count read and the
  sleep is caught and woken at once.
- **Storm and failure masks** (the SPI stays masked for the boot, and waiters
  poll):
  - why 1: 1000 empty interrupts in a row.
  - why 2: 5000 interrupts in 100 ms.
  - why 3: GPU isolation (`fg_isolate` → `fg_irq_quiesce`, which also waits out
    a handler that is reading MMIO).
  - why 4: 64 timed-out sleeps with no interrupt ever seen.
  - Any other SPI that reaches the handler is masked and counted as `foreign`.
- **Mask repair.** The 600-wait report re-checks `RBBM_INT_0_MASK` under the
  GPU lock and rewrites it if a reinit cleared it (`maskfix`).

### Log lines

```
FUXI GPU IRQ: attach INTID 332 cpus N mpidr 0x.. router 0x.. readback 0xa003 (grp|en<<1|edge<<2|prio<<8) int0_mask 0x..->0x100000 armed
FUXI GPU IRQ: not attached (<reason>), WaitFence keeps polling
FUXI GPU IRQ: 64 timed-out sleeps and no interrupt, masked; WaitFence polls
FUXI ABI: fence pending per 600 waits us <250 N <500 N <1000 N <2000 N <4000 N <8000 N <16000 N <32000 N more N untimed N ready N irq state S why W n N empty N foreign N wakes N timeouts N races N written N maskfix N
```

- **Pending line.** Always on, whether or not the bit is set. It measures from
  submit to the wait that saw the fence retire, for each successful WaitFence
  or submit-embedded wait. `untimed` means the fence was not among the client's
  last 16 submits. `ready` means it had retired at the first poll; these waits
  are also binned.
- **`state`.** 0 off, 1 armed, 2 attach failed, 3 masked, 4 attaching.
- **`why`.** As listed above.
- **Existing line.** `fence waits per 600 sleeping` is unchanged. With the bit
  set, its `polls_avg` counts wake-ups, not 100 us polls.

### Expected effect (GUESS, not yet observed)

- **CPU wake-ups.** A WaitFence currently wakes every 100 us, about 80 times
  for an 8 ms frame. With the bit it should wake about once or twice per frame,
  plus a few polls while the ring drains. That should save CPU time and power
  on the waiting core.
- **Latency.** WaitFence latency should be about the same or a little lower:
  interrupt, then wake-up, then poll, instead of waiting up to a 100 us poll
  period. Scheduler wake-up latency is extra.
- **Where to look.** The pending histogram is the measurement. Compare bit off
  and bit on at the same workload. Watch the `n`, `wakes` and `timeouts` ratios.

### Risks

- If Gunyah does not pass SPI 332 to the NS GIC, nothing happens. The cost is
  64 slices of 1 ms (why 4), then polling as before.
- A source that never clears could still storm before the mask. That is
  bounded at 5000 interrupts in 100 ms, or 1000 empty interrupts in a row.
- Unmasking RBBM bit 20 may change GMU/IFPC power behaviour (guess).
- The handler runs on CPU 0's interrupt stack. MMIO there is not
  fault-contained, so it is gated by the `fg_irq_ok` handshake and
  `fg_access_failed`, and `fg_isolate` clears it first. A GPU power collapse
  without isolation while an interrupt is in flight is not covered.
- The strict-flag build was approximated on the host: the source compiles and
  assembles for arm64 under xnu's `-Weverything` list, with kernel-shaped
  declarations. The real kernel build is CI's.

Tests: `test-fuxi-gpu-irq.py` (new), `test-fuxi-adreno-abi-perf.py` (pending
line), `test-fuxi-gpu-k1.py`, `test-fuxi-adreno.py`, `test-fuxi-metal.py`, and
every `sync-fuxi-*.py --check`.

### Later: MDSS TE / vblank for 120 Hz pacing (documentation only)

The same path would serve the display. The following was not implemented, and
the register offsets in it are unverified (GUESS).

- **Which interrupt.** Qualcomm's `kalama-sde-common.dtsi` gives `mdss_mdp@ae00000`
  `interrupts = <GIC_SPI 83 IRQ_TYPE_LEVEL_HIGH>`, which is INTID 115. MDSS is a
  nested controller. The top-level `HW_INTR_STATUS` names the sub-block, and
  the INTF (vsync) or pingpong/INTF TE (`rd_ptr`, tear-check) status and clear
  registers then decide.
- **Handler.** The handler would dispatch by `source`: 332 for the GPU, 115 for
  MDSS. It would read the top status, read the sub-block status and clear only
  the TE/vsync bit, then increment a vblank sequence and wake the framebuffer
  workloop. That replaces the timer-driven present pacing.
- **Masks and attach.** Use the same storm masks, since tear-check is level and
  can stick if the panel stops. Use the same attach sequence with its own
  `fg_window` for MDSS, and route it to CPU 0 as well.
- **Before enabling.** Check the MDSS offsets in the Linux SDE driver for
  kalama, and check that Gunyah forwards SPI 83.
