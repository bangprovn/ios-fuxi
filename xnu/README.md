# XNU for sweet and fuxi

Built on macOS (Apple silicon) with
[blacktop/darwin-xnu-build](https://github.com/blacktop/darwin-xnu-build) (MIT) at a pinned commit.

- `MACOS_VERSION=13.5` -> xnu-8796 (same generation as iOS 16.6/16.7).
- `patches/*.patch` (and the `*.sh` staging scripts) are copied into the build script's patch dir and applied to the xnu tree with `git apply`, in numeric order.
- `tools/build-fuxi-native.sh` does the same natively; `tools/xnu-cc.py` compiles single files for quick checks.
- Output: `kernel.development.<config>` plus the unstripped kernel used for symbolication.

## Manual hosted builds

`.github/workflows/fuxi-build.yml` runs only on `workflow_dispatch`. It needs no
repository secrets. Select `kernel=none|metal4|wlan6g|metal4e|all` (default `metal4`),
and independent boolean inputs `metal`, `mesa`, `tools` (default `false`).
`all` builds both kernel profiles; `metal4e` (opt-in arm64e build of the metal4
kernel, patches 0115/0116, `docs-lanes/arm64e.md`) is never part of `all`; selecting nothing fails before allocating a
Mac. Each target runs independently, with a 120-minute limit and one artifact
retained for **7 days**. Failed builds upload logs and their checksum manifest;
check the job result before treating an artifact as complete.

The Mac entry point is `bash tools/ci/build.sh TARGET`, where TARGET is
`kernel-metal4`, `kernel-wlan6g`, `kernel-metal4e`, `metal`, `mesa` or `tools`. It uses the runner's
cmake/ninja/jq/pkg-config, builds or downloads the remaining tools (gum, bash,
glslang) from checksum-pinned sources in `tools/ci/pins.env` (macOS 14 has no
Homebrew bottles any more, so nothing is brew-installed), requires a full preinstalled Xcode, and uses ignored `deps/` and
`tools/out/` directories. Existing target work/output directories must be moved
aside before another build. `--dry-run` and `--plan` work on Linux without
downloading anything. Kernel builds require Xcode 15.x unless `FUXI_IIG_MODE=replay`; set `DEVELOPER_DIR` for
another installed Xcode 15 location. The workflow explicitly selects Xcode 15.4
on `macos-14`, preserving XNU 8796's compatible **iig**. Xcode 16 iig emits an
incompatible IORPC ABI. This uses the pinned blacktop builder
`663906528d1cd5ea8fde7de6f16de73db9511549`, macOS source version 13.5,
`DEVELOPMENT ARM64 FUXI`, and KDK 13.5 / 22G74 (installed on the runner when
absent). The shared staging scripts are adapted for FUXI, firehose includes
plain arm64, and failure to apply any repository patch fails the build.
The default needs no IIG cache; the replay mode below uses the same tool as
the native local builder (`tools/xnu-iig-cache.py`).

### Runner migration: iig pregen (macos-14 is unsupported from 2026-11-02)

Facts (runner-image READMEs, 2026-10-30): macos-14 ships Xcode 15.0.1-15.4 and
16.1/16.2; **macos-15 ships only Xcode 16.0-16.4 and 26.0.1-26.3; macos-26 only
26.x**. No supported image keeps Xcode 15, Apple's downloads need an Apple ID
(so `xcodes` cannot fetch it in CI), and caching an Xcode/iig binary would put an
Apple tool into CI storage. Only `iig` is version-bound: it turns XNU's 17
`iokit/DriverKit/*.iig` inputs into `*.h` + `*.iig.cpp`, and Xcode 16+ iig
(DriverKit-4xx) emits a newer IORPC layout (`IORPC.kernelContent`) than XNU 8796
expects. Newer clang/ld already compile 8796 with `xnu/patches` (the native
builder builds it with Xcode 27.1 plus IIG replay; a hosted Xcode 16 build is
still to be proven by the runs below).

The workflow therefore has opt-in inputs; defaults keep the existing path:

| Input | Default | Meaning |
| --- | --- | --- |
| `runner` | `macos-15` | `macos-14`, `macos-15` or `macos-26` |
| `xcode` | `16.2` | `DEVELOPER_DIR=/Applications/Xcode_<xcode>.app`; a missing Xcode fails the job before building |
| `iig_mode` | `replay` | `xcode`: run Xcode 15 iig (fails on any other Xcode). `capture`: the same, then archive the iig outputs. `replay`: never run iig; install pregenerated outputs (any Xcode) |
| `iig_run` | `41098982212` | replay source: a capture run's ID (`fuxi-iig-pregen-*` artifacts, kept 90 days); empty means a committed `xnu/iig-pregen/` |

Defaults since 2026-10-31: macos-15 + Xcode 16.2 + replay of capture run 41098982212 (macos-14/15.4 at 2ae1d27). A kernel built this way (run 41102135821) booted on the device with the same behaviour as the macos-14 builds. macOS 14 brownouts already cancel macos-14 jobs. The capture artifacts expire about 2027-01-29: capture again before then, or commit `xnu/iig-pregen/`. A capture needs a runner that still has Xcode 15.

`capture`/`replay` add `iig-pregen/` (17 header/implementation pairs plus
`manifest.json` with SHA-256 of each `.iig` input and output) to the kernel
artifact, plus a separate `fuxi-iig-pregen-<target>` artifact. `--prepare`
collects the outputs from every build directory and refuses differing copies;
`--verify` requires the cache to cover exactly this tree's `.iig` inputs with
matching hashes. During replay, make's `$(IIG)` is the replay script, which
refuses an edited, new or unknown `.iig` and any output whose hash differs;
after the build the outputs are re-collected and their manifest must equal the
cache's byte for byte. Patches never touch `iokit/DriverKit`; if one ever does,
regenerate on an Xcode 15 host (none remains hosted) rather than editing outputs.
`python3 tools/test-fuxi-iig-cache.py` covers these refusals on synthetic input.

Licence: the outputs are mechanical expansions of XNU's APSL-2.0 `.iig` files
and reproduce their APSL notices verbatim; they contain no tool binary, SDK
header or Apple data beyond the APSL source. APSL 2.0 section 2.1 permits
redistributing such covered source with its notices and a copy of the licence,
so committing them under `xnu/iig-pregen/` (with the APSL text) is licence-
compatible. They are **not committed** here because this repository keeps Apple
source out of git; the 90-day artifact is the default store. Committing them,
or attaching the capture artifact to a release, is the owner's call.

Validation order (all on the same commit; macos-14 steps must finish before
2026-11-02):

1. `runner=macos-14 xcode=15.4 iig_mode=capture`: reference kernel K1 and the pregen.
2. `runner=macos-14 xcode=15.4 iig_mode=replay iig_run=<run 1>`: K2 must be
   byte-identical to K1 (proves replay changes nothing).
3. `runner=macos-14 xcode=16.2 iig_mode=replay iig_run=<run 1>`: Xcode 16 clang/ld K3
   (expected to differ from K1; must build).
4. `runner=macos-15 xcode=16.2 iig_mode=replay iig_run=<run 1>`: K4 must be
   byte-identical to K3 (Xcode 16.2 16C5032a is on both images).

Then boot K3/K4 before switching the defaults to `macos-15`/`16.2`/`replay`.
The userspace targets (`metal`, `mesa`, `tools`) are independent of iig but
have only been built with Xcode 15.4 here; check them separately on the new runner.

| Artifact | Contents in addition to `SHA256SUMS` and `logs/build.log` | Estimated cold runtime |
| --- | --- | --- |
| `fuxi-kernel-metal4` | `kernel.development.fuxi`, `kernel.development.fuxi.unstripped`, public `fuxi.adt`, `cmdline.txt` for the Metal smoke host | 15–35 min |
| `fuxi-kernel-metal4e` | arm64e kernel pair (`FUXI_ARM64E=1`: ld-classic `__TEXT,__thread_starts`, keys programmed in start.s), public `fuxi.adt`, metal4 `cmdline.txt`, `cmdline-cpus1.txt` (one-core fallback), `kernel-arch.txt` | 15–35 min (not yet built) |
| `fuxi-kernel-wlan6g` | Same kernel pair and public ADT, M6g `cmdline.txt` (full) and `cmdline-s22.txt` (stop at BHI completion) and `cmdline-s22-oldiova.txt` (same, old IOVA pool) | 15–35 min |
| `fuxi-metal` | Complete ad-hoc signed `FuxiMetal.bundle`, `fuxi-metal-smoke`, our compiled `metal-smoke.metallib`, separately signed `libmetal2vulkan.dylib`, `metal2vulkan-source.tar.gz` with licences and vendored crate sources, `metal-clients.conf`, `metal-smoke.conf` | 10–25 min |
| `fuxi-mesa` | Signed `libvulkan_freedreno.dylib`, `patches/`, `tools/` build/audit recipes, `mesa-licenses/` | 15–35 min |
| `fuxi-tools` | Signed `fuxi-vk-probe`, `fuxi-vk-smoke`, `fuxi-gpu-abi-test`, `vk-smoke.conf`, import-audit sidecars | 5–15 min |

Estimates are planning ranges, not measurements of this restored workflow.
Jobs run concurrently subject to the account's runner capacity. Warm caches
avoid repeated source/tool/crate downloads; they do not restore build products.
Source snapshots, pinned tool downloads and Cargo registry caches are keyed
by pinned revisions and the relevant recipe. No SDK, KDK, Xcode, research tree,
firmware or boot images are cached or uploaded. The translator checkout uses a
partial clone and sparse checkout to avoid fetching precompiled AIR/metallib
fixtures at all. UniXNU contributes only the five hash-verified text patches;
its full tree is never fetched. Mesa and metal2vulkan revisions remain the ones
in the existing lane recipes, recorded in `tools/ci/pins.env` and every build log.
Metal signing and audits use the existing M5b/M5c build scripts unchanged.

### Trigger and download (after reviewing and pushing)

The workflow must first exist on the repository's default branch for GitHub
to expose manual dispatch. Use `--ref` for the pushed branch to build. These
examples assume the reviewed change has been integrated into `main`:

```sh
# Run each target separately; all flags are explicit to avoid the kernel default.
gh workflow run fuxi-build.yml -R bangprovn/ios-fuxi --ref main -f kernel=metal4 -f metal=false -f mesa=false -f tools=false
gh workflow run fuxi-build.yml -R bangprovn/ios-fuxi --ref main -f kernel=wlan6g -f metal=false -f mesa=false -f tools=false
gh workflow run fuxi-build.yml -R bangprovn/ios-fuxi --ref main -f kernel=metal4e -f metal=false -f mesa=false -f tools=false
gh workflow run fuxi-build.yml -R bangprovn/ios-fuxi --ref main -f kernel=none -f metal=true -f mesa=false -f tools=false
gh workflow run fuxi-build.yml -R bangprovn/ios-fuxi --ref main -f kernel=none -f metal=false -f mesa=true -f tools=false
gh workflow run fuxi-build.yml -R bangprovn/ios-fuxi --ref main -f kernel=none -f metal=false -f mesa=false -f tools=true
# Or all five artifacts in one run:
gh workflow run fuxi-build.yml -R bangprovn/ios-fuxi --ref main -f kernel=all -f metal=true -f mesa=true -f tools=true

gh run list -R bangprovn/ios-fuxi --workflow fuxi-build.yml --limit 10
# Set RUN_ID to the chosen run's numeric ID, and verify it succeeded.
gh run watch "$RUN_ID" -R bangprovn/ios-fuxi --exit-status
gh run download "$RUN_ID" -R bangprovn/ios-fuxi -n fuxi-kernel-metal4 -D xnu/out-ci-metal4
gh run download "$RUN_ID" -R bangprovn/ios-fuxi -n fuxi-kernel-wlan6g -D xnu/out-ci-wlan6g
gh run download "$RUN_ID" -R bangprovn/ios-fuxi -n fuxi-metal -D tools/out/download-metal
gh run download "$RUN_ID" -R bangprovn/ios-fuxi -n fuxi-mesa -D tools/out/download-mesa
gh run download "$RUN_ID" -R bangprovn/ios-fuxi -n fuxi-tools -D tools/out/download-tools
(cd xnu/out-ci-metal4 && sha256sum -c SHA256SUMS)
```

For separate runs, use each corresponding RUN_ID and download only that run's
artifact. On macOS use `shasum -a 256 -c SHA256SUMS`. GitHub artifact extraction
does not preserve executable modes; restore `chmod +x` on downloaded userspace
executables and bundle executables before staging. Do not alter bundle resources;
the complete resource signature is included.

### Pack only on local Linux

Both profiles build the **current commit's complete FUXI patch set**. Profile
names select boot arguments, not historical binary snapshots: Metal leaves
WLAN disabled, while M6f opts into the current `0099` driver with limits 1/22.
The command lines match the existing metal4/M6f image scripts, including their
different exec-swap hosts. No `build-fuxi-*-images.sh`, `mkpayload.py` or
`shim/fuxi-loader/build.sh` runs in CI.

The public ADT is generated entirely from our measured hardware recipe: 5088 MiB
XNU RAM, seven CPUs, UFS and the top-of-bank 32 MiB GPU reservation. It contains
**no private root-file extents or copied Apple /product node**. Consequently,
before packing a boot for the existing pinned-file root, preserve the integration
ADT you already use locally. Set `BASE_ADT` to that existing `fuxi.adt`; the local
helper validates/preserves its root mapping and identity and refreshes only the
GPU reservation. No Apple data or device dump needs to enter the public repo or CI.

```sh
# Run from the repository root on Linux. BASE_ADT is an existing local input.
python3 tools/ci/kernel-inputs.py --variant metal4 --local-adt "$BASE_ADT" --out xnu/out-local-metal4
mkdir -p shim/out
python3 tools/mkpayload.py xnu/out-ci-metal4/kernel.development.fuxi \
  xnu/out-local-metal4/fuxi.adt shim/out/fuxi-ci-metal4-payload.bin \
  --cmdline "$(cat xnu/out-ci-metal4/cmdline.txt)"
# Supply the same private firmware/toolchain environment as existing local packs:
# FUXI_GMU_FIRMWARE, FUXI_SQE_FIRMWARE, FUXI_GPU_ZAP_MBN, MKBOOTIMG.
# Also export CLANG, LLD, OBJCOPY and READELF for the AArch64 loader toolchain.
bash shim/fuxi-loader/build.sh shim/out/fuxi-ci-metal4-payload.bin

# WLAN: same locally preserved integration ADT, with full or stop-at-22 cmdline.
python3 tools/ci/kernel-inputs.py --variant wlan6g --local-adt "$BASE_ADT" --out xnu/out-local-wlan6g
python3 tools/mkpayload.py xnu/out-ci-wlan6g/kernel.development.fuxi \
  xnu/out-local-wlan6g/fuxi.adt shim/out/fuxi-ci-wlan6g-s22-payload.bin \
  --cmdline "$(cat xnu/out-ci-wlan6g/cmdline-s22.txt)"
bash shim/fuxi-loader/build.sh shim/out/fuxi-ci-wlan6g-s22-payload.bin
```

The loader's output is `shim/out/fuxi-loader/fuxi-xnu-boot.img`; save/rename it
locally before another pack. Firmware provision, root-image edits and device
testing remain separate local operations. CI never fetches or embeds GPU/WLAN
firmware and uploads no Apple binaries, SDK headers, support libraries or build trees.
