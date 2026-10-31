# xnu27: the iOS 27 kernel (xnu-12377.121.6, arm64e)

Kernel lane for the iOS 27 port (plan, milestones and risks:
[docs-lanes/ios27.md](../docs-lanes/ios27.md)). It follows UniXNU's iPadOS 27
build for the Xiaomi Pad 6S Pro (sheng, same SM8550): Apple's xnu-12377.121.6
with UniXNU's BCM2837-style machine layer (classic pmap, no SPTM/TXM, 4K pages,
native PAC keys), the sheng CPU/MMU fixes, ArmCpu (GICv3/timer/PSCI) and
ArmDTPlatform, plus a small Fuxi layer.

| Path | What |
|---|---|
| `series` | patch order; `apply.sh XNU_DIR` applies it with `git apply` (stops on the first failure) and stages the components |
| `patches/unixnu/` | UniXNU firmware layer, verbatim (BSD-2-Clause-Patent) |
| `patches/sheng/`, `patches/sm8550/` | UniXNU-sheng CPU/MMU subset, SM8550 secondary-CPU fix |
| `patches/fuxi/` | `FUXI` machine config (wraps BCM2837, adds mockfs), built-in PRNG/SHA-2/kmem RNG while no corecrypto is registered, early console (fbcon, USB monitor ring, stage marks), component wiring |
| `components/` | ArmCpu, ArmDTPlatform sources (compiled into the kernel) |
| `sources.env` | Apple open-source pins (fetched by CI, never committed) |
| `NOTICE` | origins and the BSD-2-Clause-Patent text |

Build: GitHub Actions `fuxi-build.yml` with `kernel=ios27`
(`tools/ci/build-xnu27.sh`, Xcode 26 on macos-26). Output
`mach.development.fuxi` (+ `.unstripped`), `fuxi27.adt`, `cmdline.txt`; pack with
`tools/mkpayload.py --boot-line-length 1024` (payload v2) and
`shim/fuxi-loader/build.sh`.

Not here on purpose: UniXNU's source-corecrypto patch and its corecrypto source
(Apple internal-use licence), Apple kexts (prelinked locally from your own
kernelcache with `tools/ios27-prelink.py`, docs-lanes/ios27.md K2a; never in CI
or git), userland patches (K3+). CI also uploads the K2a prelink inputs
`mach.development.fuxi.chained.unstripped` and `mach.release.fuxi.unstripped`.
