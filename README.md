# ios-fuxi

**Apple's iOS 16 userland on a Xiaomi 13 (`fuxi`, Snapdragon 8 Gen 2 / SM8550).**

This project runs Apple's open-source XNU kernel (xnu-8796, the iOS 16 generation) on the Xiaomi 13, with the **unmodified iOS 16.7 (20H392) userland of the iPhone X** on top: launchd, the system daemons, backboardd, SpringBoard and the stock apps. It is a fork of [ios-sweet](https://github.com/dipa-studio/ios-sweet), which did the same on the Redmi Note 10 Pro. The Sweet documentation is kept in [docs/sweet.md](docs/sweet.md).

The phone boots to the iOS home screen. You can use the touchscreen and the hardware buttons and open apps such as Settings.

> Experimental bring-up, not a usable phone OS. Most hardware is missing, rendering is done by the CPU, and Apple's security model is largely switched off. Do not put anything personal on it.

## Status

| Area | State |
| --- | --- |
| Boot | Stock Xiaomi bootloader → temporary `fastboot boot` of our loader → XNU. Nothing is flashed to the boot partitions |
| Loader | Bare-metal arm64 loader (`shim/fuxi-loader`) with a USB control interface on a second core for logs, RAM reads and diagnostics while XNU runs |
| CPU | 7 of the 8 cores. The loader caps the cluster clocks through EPSS, which keeps the SoC around 51 °C in sustained use (peaks near 63 °C without the caps) |
| Storage | Own UFS host driver. The iOS root is a journaled HFS+ image stored as a pinned file inside Android's `userdata`; XNU reads it through an extent table (patch 0088) |
| Display | 1080×2400 DSC panel. CoreAnimation renders on the CPU, frames are copied to the bootloader framebuffer at 60 Hz |
| Touch | FTS touch controller over SPI, delivered as HID digitizer events with adaptive polling |
| Buttons | Power (PMK8550 PON) and volume down/up (PMK8550 PON, PM8550 GPIO) over the SPMI arbiter, as HID keys. Power turns the display off and on |
| GPU | Adreno A740 bring-up in the loader: the GMU firmware boots, the HFI handshake completes and the GPU core is powered. Command-processor stages are written but not yet tested on the device. No GPU driver in XNU yet |
| Not working | Wi-Fi, Bluetooth, cellular, audio, camera, sensors, Face ID / SEP, GPU acceleration in iOS |

### Milestone 7 memory layout

The offline 0097 images select 5088 MiB of XNU RAM at `0x8c0000000`, with
32 MiB at `0x9fe000000` reserved for GPU scratch, firmware and handoff. This
uses the observed contiguous 5 GiB bank; upper-bank runtime validation is
pending. `mkadt-fuxi.py --ram-layout 1g` preserves the old 1 GiB XNU window.
See the [memory audit and address map](docs-lanes/memory.md).

## How it boots

```
fastboot boot fuxi-xnu-boot.img
  -> fuxi-loader (Linux Image header, MMU off)
       - reads the live device tree, sets up RAM, display and CPU clocks
       - starts a USB monitor on a second core
       - jumps to XNU with an Apple device tree (ADT)
  -> XNU -> UFS driver -> HFS+ root (file extents inside userdata)
  -> launchd -> backboardd -> SpringBoard
```

The host side, `tools/fuxi-xnu-monitor.py`, talks to the loader over USB. It streams the kernel console, samples temperatures and clocks, applies CPU clock limits and resets the phone back to Android when a session ends.

## Repository layout

| Path | What |
| --- | --- |
| `xnu/patches/` | Kernel patches. 0001–0081 come from ios-sweet, 0082 onwards are Fuxi (RAM, UFS, extent root, SMP, touch, buttons, display) |
| `tools/build-fuxi-native.sh` | Native kernel build on an Apple-silicon Mac |
| `shim/fuxi-loader/` | The Fuxi loader and its boot image build (`build.sh`) |
| `shim/fuxi-probe/` | Hardware probes linked into the loader: display, touch, thermal/EPSS, GPU (`gpu.c`) |
| `tools/fuxi-*.py`, `tools/mkadt-fuxi.py` | USB monitor, GPU probe decoder, ADT builder, root staging, DTB inspection |
| `tools/mkiosroot.sh`, `tools/sweet-root-tweaks.py` | Build the iOS root image from the IPSW and apply the bring-up changes |
| `xnu/fuxi-sys/`, `tools/fuxi-audio/`, `tools/fuxi-sb/`, `tools/fuxi-sys-root.py` | Opt-in P-SYS system fixes (patch 0107, HAL plug-in, SpringBoard library, root edits) |
| `docs-lanes/` | Per-subsystem notes: GPU, thermal, buttons, touch/display |
| `docs/sweet.md` | The original ios-sweet README |

Not in git: partition backups, device trees, recovery images, Apple firmware and root images, GPU firmware and build outputs. They contain device serials and calibration data, or files that cannot be redistributed. The GPU firmware (`gmu_gen70200.bin`, `a740_sqe.fw`) comes from linux-firmware and is passed to the loader build through environment variables.

## Building

1. **Kernel:** build xnu-8796 on macOS with [blacktop/darwin-xnu-build](https://github.com/blacktop/darwin-xnu-build) (`MACOS_VERSION=13.5`) with `xnu/patches/` applied, or use `tools/build-fuxi-native.sh` (see [xnu/README.md](xnu/README.md)).
2. **iOS root:** `tools/mkiosroot.sh` and `tools/fuxi-stage-root.py` build the HFS+ root from the iOS 16.7 IPSW, which you supply.
3. **Payload and loader:** `tools/mkadt-fuxi.py` and `tools/mkpayload.py` pack the kernel and ADT, then `shim/fuxi-loader/build.sh <payload>` builds the boot image with an LLVM toolchain (`CLANG`, `LLD`, `OBJCOPY` and `MKBOOTIMG` environment variables).
4. **Run:** `fastboot boot` the image, then attach `tools/fuxi-xnu-monitor.py`.

You need an unlocked bootloader, a rooted recovery for installing the root file, and backups of your critical partitions (`tools/fuxi-backup-critical.py`). Everything here is temporary-boot only, but you can still lose data.

## Credits

- **HackiOS ([kernel](https://github.com/oosexgang/hackios-xnu-kernel), APSL 2.0; [userland](https://github.com/oosexgang/hackios-xnu-userland), MIT) by OOSEX GANG**: xnu + iOS 16.5 on the POCO X5 Pro. Ported or adapted, each with its licence header kept:
  - Wi-Fi iOS side: the iOS Wi-Fi interface, Apple80211 ioctl/station bridge, property-only WLAN discovery, network-stack stand-in and WPA2 arithmetic (`xnu/fuxi-wifi-ios/`, patch 0104); see [P-WIFI](docs-lanes/wifi-ios.md).
  - Turnip: the ir3 default-rounding fix (`tools/patches/mesa-fuxi-ir3-round-default.patch`); see [P-MESA](docs-lanes/mesa-port.md).
  - Buttons: AMFI/Sandbox query layouts, debounce/bus-lock ideas, tap-to-wake through `TouchDetectionMode` and the button usage map (`xnu/fuxi-h1/`, MIT notice in `fuxi_key_state.h`); see [kernel H1](docs-lanes/kernel-h1.md#reference-credit).
  - Cooling: the launchd on-demand pass (`tools/launchd-on-demand.py`), the mediaserverd/SpringBoard Metal denial and the cpuload log, sleep refusal and xpcproxy identity hunks (patch 0105); see [cooling](docs-lanes/cooling.md).
  - Thermal pressure: the MIT `thermald` bridge from [hackios-xnu-userland](https://github.com/oosexgang/hackios-xnu-userland) `fcc3214` + `a79ff07`, adapted to our governor and opt-in launchd staging; see [thermal pressure to iOS](docs-lanes/cooling.md#thermal-pressure-to-ios).
  - System: the privilege-by-entitlement table and uncontained container-path result (`xnu/fuxi-sys/`, patch 0107), the Speaker/Hawking/Actuator/Codec AudioServerPlugIn (`tools/fuxi-audio/`), the SpringBoard snapshot-format fix (`tools/fuxi-sb/`) and the respawn-job list; see [P-SYS](docs-lanes/sys-port.md).

- **[metal2vulkan](https://github.com/steelbrain/metal2vulkan)** by Anees Iqbal and contributors: external AIR-to-SPIR-V translator, pinned to `720ef1ce9ae2a2068999a8675717418b9416283e`. Its declared licence is GPL-2.0-or-later OR LGPL-3.0-or-later; the Metal lane selects **LGPL-3.0-or-later** for a separate, replaceable `libmetal2vulkan.dylib` loaded with `dlopen`. No translator source is vendored or statically linked into FuxiMetal/XNU. The ignored build output includes corresponding library/adapter/dependency source, lockfile, LGPL/GPL texts, notices and an offline rebuild recipe. Preserve those materials and the rights/information needed to replace and debug modifications to the library when distributing it; dynamic loading alone does not discharge the obligations. Our original code retains its existing repository terms. See [Metal build, licensing and installation details](docs-lanes/metal.md#m5b-external-translator-and-offscreen-pipeline).

- **UniXNU-sheng contributors**: WCN7850 bring-up ideas adapted in `xnu/fuxi-wlan/fuxi_wlan.c` P17 (PMK8550 gpio3 sleep-clock pin sequence, power-of-two DMA alignment, RAM-dump error-text scan) from `package/WifiProbe.cpp` at `08cde20` (BSD-2-Clause-Patent, Copyright (c) 2026 UniXNU contributors). See [P17](docs-lanes/wlan-p17.md). The opt-in GPU fence interrupt (`xnu/fuxi-adreno/fuxi_gpu_irq.inc`) adapts `package/GpuPowerProbe.cpp` `gpuIrq`/`irqAndTimer` (`b0ccf61`, `36ec562`) and the `[gpu pending]` boot-log idea from `BootLog.c` (`e34beda`); notice in `xnu/fuxi-adreno/LICENSE.UniXNU-sheng`. See [GPU fence interrupt](docs-lanes/unixnu-port.md#gpu-fence-interrupt-fuxigpuncimport-bit-26).
- **[UniXNU](https://github.com/UniXNU/UniXNU) / NativeAdreno, NativeMetal, NativeMobileDisplay, NativeUfs and NativeHidInput contributors**: MIT NativeAdreno wire ABI and A7xx BR/BV submission framing; retained notice in `xnu/fuxi-adreno/LICENSE.NativeAdreno`. NativeMetal's CA encoder, queue and neutral depth/stencil adapters are adapted in FuxiMetal, with notice in `tools/fuxi-metal/LICENSE.NativeMetal` and the signed/staged bundle. Their cache design informed our Vulkan pipeline cache and the opt-in persistent translation/driver layers; the [GPU-CACHE audit and options](docs-lanes/metal-gpu-cache.md) adapt the approach without copying the export/import transport. See [M9b comparison and provenance](docs-lanes/metal-m9b.md). P-UNIXNU also adapts NativeMobileDisplay’s bounded/differential scanout copy and timer policy, and NativeUfs’s completion, response validation and DMA quarantine policy (source SPDX: BSD-2-Clause-Patent). Notices, including NativeUfs’s BSD-2-Clause `LICENSE`, are retained in `xnu/fuxi-unixnu/LICENSE.*`. See the [driver audit, provenance and default-off options](docs-lanes/unixnu-port.md). GPU-CLOCK adapts AdrenoFirmware BCM vote arithmetic and VCD grouping (BSD-2-Clause-Patent); the original notice is retained in `shim/fuxi-probe/LICENSE.AdrenoFirmware`. See [fixed GPU levels, DDR votes and clock diagnostics](docs-lanes/gpu-clock.md). GPU-SCANOUT adapts NativeMetal’s finished-frame GPU-copy and pixel-check design to our kernel blitter, with its MIT notice also retained in `xnu/fuxi-adreno/LICENSE.NativeMetal`; Fuxi display gating and per-submission surface leases are original code. See [GPU scanout](docs-lanes/gpu-scanout.md). TOUCH adapts [NativeHidInput](https://github.com/UniXNU/NativeHidInput) report batching and multi-finger/lift counters from `75efe1c5ee32b2da3ef8dc978adbdc683eef11a8`, `e43c2541133e25bdb48df564a4283888b64cc231`, `22f2dd454e8742f0639a737065f59ab42927a165` and `ba03e5d147452b2166aab7435d219fa10877672b` (BSD-2-Clause-Patent); notice in `xnu/fuxi-h1/LICENSE.NativeHidInput`. See the [touch comparison, log audit and opt-in control](docs-lanes/touch-display.md#unixnu-nativehidinput-report-fixes-2026-10-24). The [2026-10-24 port](docs-lanes/unixnu-port.md#unixnu-2026-10-24-port) adapts NativeAdreno `b686b90` cache handoff (MIT), NativeMetal `c6d29ca` present priority (MIT), and UniXNU `cb07499` union-directory lookup (BSD-2-Clause-Patent), each behind an optional boot argument. The GPU fence interrupt adapts ArmCpu's `armcpu_spi_attach` distributor sequence (`1951cc8`, BSD-2-Clause-Patent, Copyright (c) 2026 ArmCpu contributors; notice in `xnu/fuxi-adreno/LICENSE.ArmCpu`) and Adreno740's fence-pending histogram idea (`f53000c`, MIT).

- **[UniXNU](https://github.com/UniXNU/UniXNU), [UniXNU-sheng](https://github.com/UniXNU/UniXNU-sheng), [UniXNU-sm8550-common](https://github.com/UniXNU/UniXNU-sm8550-common), [ArmCpu](https://github.com/UniXNU/ArmCpu), [ArmDTPlatform](https://github.com/UniXNU/ArmDTPlatform) and [XnuPrelink](https://github.com/UniXNU/XnuPrelink) contributors** (BSD-2-Clause-Patent, Copyright (c) 2026 UniXNU contributors): the iOS 27 kernel lane (`xnu27/`) applies UniXNU's xnu-12377 patch layer (`88604d0`, verbatim), the sheng CPU/MMU fixes (`5b7d83c`), the SM8550 secondary-CPU fix (`23b2049`) and compiles in ArmCpu and ArmDTPlatform unchanged; `tools/mkadt-fuxi27.py` follows sheng's `make-sheng-adt.py` tree. `tools/xnuprelink/` is [XnuPrelink](https://github.com/UniXNU/XnuPrelink) `76452c6` verbatim (fileset matcher and emitter that prelinks Apple kexts from a local kernelcache into our kernel; its own `LICENSE` is kept). Origins, revisions and the licence text are in `xnu27/NOTICE`; see [iOS 27 kernel](docs-lanes/ios27.md).
- **[ios-sweet](https://github.com/dipa-studio/ios-sweet) by [dipa-studio](https://github.com/dipa-studio)**: the original project this repository is forked from. The XNU port, the iOS userland bring-up, the kernel patches up to 0081 and most of the tooling come from it.
- **Claude Opus 5.5 ([Anthropic](https://www.anthropic.com))**: AI pair engineer on the Xiaomi 13 bring-up: loader, XNU patches, drivers, thermal work and device testing.
- **[OpenAI Codex](https://openai.com/codex)**: the GPU lane: GMU memory setup, firmware loading, HFI startup, GX power-up and the command-processor probe stages.
- Built on Apple's open-source releases (xnu, IOHIDFamily, libpthread, hfs), [BearSSL](https://bearssl.org), [blacktop/darwin-xnu-build](https://github.com/blacktop/darwin-xnu-build), [ipsw](https://github.com/blacktop/ipsw), the Linux kernel (msm DRM, Qualcomm clock, interconnect and SMMU drivers) and linux-firmware for hardware details, Qualcomm/Xiaomi's published kernel sources, and [OrangeFox Recovery](https://orangefox.download).

iOS itself is Apple's and is not distributed here.
