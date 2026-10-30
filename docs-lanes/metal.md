# Metal lane M5c: accept the iOS 16.7 descriptor defaults

2026-10-21. **Built, signed and host-verified; M5c device run pending.**
The coordinator reports M5b on the metal4 kernel: stages 0–3 PASS (34 fences,
no hang), then the combined pipeline descriptor/functions rejection before
translation. backboardd/SpringBoard remained denied; no crash/panic reported.

The root-cause hypothesis is now specific: **`vertexDescriptor` is a lazy
getter on 20H392**, so reading the untouched default produces a non-nil empty
`MTLVertexDescriptorInternal`. M5b rejected `d.vertexDescriptor` merely for
being present. The same lazy behavior is visible on the current Mac host;
the earlier positive Vulkan tests called the backend directly, and frontend
encoder tests constructed a pipeline object manually. Neither tested a valid
library/function/descriptor combination through the frontend pipeline gate.

M5c accepts only nil or the public empty vertex shape: all 31 attribute formats
invalid, offsets/buffer indices zero, and all 31 layouts stride zero,
per-vertex/1 stepping. Configured vertex descriptors still reject. An explicitly
supplied empty descriptor and the lazy default are indistinguishable through
public properties and have the same rendering meaning. No private ivar reads,
selector probes or object-layout assumptions were added.

Each pipeline attempt prints one descriptor line before validation, then the
first failing field and actual value as `FUXI MTL: pipeline reject <field> <value>`.
Class failures print the actual class name (or nil); device failures print the
actual object address. Indexed vertex/attachment failures include the index.
The NSError also names the rejected field. Texture, render-pass, encoder,
draw and readback checks use the same named-rejection convention. Render-pass
resolve targets on otherwise unused attachments now also reject. All settings
that affect the supported rendering remain checked: owning device/stages,
vertex input, BGRA8 target 0 only, full target-0 write mask, no blending,
one sample, rasterization, no depth/stencil, one direct triangle and whole
texture readback with bounds/alignment/earlier-render checks.

Tessellation format/max factor are dumped but ignored for this non-patch draw.
`supportIndirectCommandBuffers` is likewise inert for the sole direct draw;
no indirect encoder API is supplied. Unspecified or triangle input topology
is compatible; point/line reject. Blend/mask settings on absent targets and
`allowGPUOptimizedContents` (a storage optimization hint) cannot change these
pixels and no longer gate acceptance. Amplification remains bounded to 1.
The external translator's independent graphics/resource checks remain intact.

`newFunctionWithName:` still resolves stage metadata through the dlopened,
pinned translator. The new Mac regression loads the actual smoke metallib
through both FuxiLibrary URL/data paths and verifies FuxiFunction class,
`_stage` **1/2**, public vertex/fragment type and the same owning device.
Production function creation now logs those identities. The test checks that
the real-default descriptor reaches backend compilation, where the CPU model
explicitly stops, and tests rendering-changing mutations at the frontend gate.

`translation PASS` is intentionally lazy: library loading parses AIR, function
creation queries metadata, and `Backend::compilePipeline` invokes the translator
and prints its timing only **after** descriptor validation. Thus M5b's missing
translation line matches the earlier rejection; it does not show a translator
failure. No translator/source-pin/backend/smoke/shader/kernel changes were made.

## 20H392 defaults and reproduction

Source: the supplied iOS 16.7 / 20H392 arm64 shared cache, main-file SHA256
`76874e8375252ee643011ff8063f0cce12355f05cf5f9897a34e5679667a65aa`,
UUID `8F970452-C9E3-3E88-B114-FEB1864BC9E0`. `ipsw dyld info` reports iOS 16.7.
The table records public values inferred from init/reset, corresponding getters,
zero-initialized object storage and lazily created child descriptors. It contains
facts about the interface, not Apple implementation code. No disassembly, cache,
firmware or Apple code is tracked.

| Pipeline property | 20H392 init/reset default; smoke change |
| --- | --- |
| `vertexFunction`, `fragmentFunction` | nil; smoke assigns the Fuxi functions (stage 1/2, owning device) |
| `vertexDescriptor` | backing reference nil; getter lazily returns an empty object, including after reset |
| Vertex attributes 0–30 `format`, `offset`, `bufferIndex` | Invalid/0, 0, 0 (zero-init; array getter allocates children) |
| Vertex layouts 0–30 `stride`, `stepFunction`, `stepRate` | 0, PerVertex/1, 1 |
| `rasterSampleCount` | 1 |
| `alphaToCoverageEnabled`, `alphaToOneEnabled`, `rasterizationEnabled` | NO, NO, YES |
| `depthAttachmentPixelFormat`, `stencilAttachmentPixelFormat` | Invalid/0, Invalid/0 |
| `inputPrimitiveTopology` | Unspecified/0 |
| `tessellationFactorFormat`, `maxTessellationFactor` | Half/0, 16 (inert here) |
| `supportIndirectCommandBuffers` | NO (inert here) |
| `maxVertexAmplificationCount` | 1 |
| `vertexLinkedFunctions`, `fragmentLinkedFunctions` | backing references nil; getters lazily return empty objects |
| Linked `functions`, `binaryFunctions`, `privateFunctions`, `groups` | nil / count 0; nil and empty collections are accepted |
| Color attachments 0–7 `pixelFormat`, `blendingEnabled`, `writeMask` | Invalid/0, NO, All/15; smoke sets only attachment 0 format to BGRA8Unorm/80 |

Evidence entry addresses: pipeline init `0x181a31690`, reset `0x181b0f66c`,
vertex-descriptor getter `0x181a452e0`, vertex-layout init `0x181a314b0`,
vertex attribute array getter `0x181b22360`, color-attachment init `0x181a315cc`,
linked getters `0x181a480f8` / `0x181a49800`. Getter values are mapped back to
init/reset writes, including the sample-count constants; assumptions about
SDK enum defaults alone are insufficient.

| Remaining stage-4 properties | 20H392 defaults; smoke change / validation |
| --- | --- |
| Render pass `renderTargetWidth`, `renderTargetHeight`, `renderTargetArrayLength`, `defaultRasterSampleCount` | all 0, derive from attachments; accept 0 or matching 256/256/1/1 |
| Pass `visibilityResultBuffer`, `rasterizationRateMap` | nil |
| Pass `imageblockSampleLength`, `tileWidth`, `tileHeight`, `threadgroupMemoryLength`, custom sample-position count | all 0 |
| Pass color `texture`, `resolveTexture`, `level`, `slice`, `depthPlane` | nil, nil, 0, 0, 0; smoke supplies color-0 texture |
| Pass color `loadAction`, `storeAction` | DontCare/0, Store/1; smoke explicitly sets Clear/2, Store/1 |
| Pass color `clearColor` | (0,0,0,1); smoke explicitly sets the same; all components must be finite and in 0..1 |
| Pass depth/stencil attachment objects | lazy objects; texture/resolveTexture nil (check those, not object presence) |
| Texture `textureType`, `pixelFormat`, width/height | init is 2D/2, RGBA8Unorm/70, 1/1; smoke factory sets BGRA8Unorm/80, 256/256 |
| Texture `depth`, `mipmapLevelCount`, `arrayLength`, `sampleCount` | all 1; nonmipmapped smoke factory retains these |
| Texture `cpuCacheMode`, `storageMode`, `hazardTrackingMode` | default-cache/0, shared/0, default/0; smoke changes storage to private/2; default/0 and tracked/2 hazards accepted |
| Texture `usage`, `allowGPUOptimizedContents` | usage is SDK-conditioned 0/1 at init (smoke explicitly sets RenderTarget/4); optimization YES (ignored hint) |
| Render encoder and draw | our original objects; no Apple encoder defaults consumed. One explicit Triangle/3, vertexStart 0, vertexCount 3; backend sets viewport/scissor/raster state |
| Readback | explicit slice/level/origin 0, size 256x256x1, offset 256, row 1024, image 262144; no default-valued descriptor involved |

Render-pass init is `0x181a3e6d4`, color-attachment init `0x181a3d52c`,
texture init `0x181a36b2c`, 2D factory `0x181a1a4b8`. Read constants as raw
bytes: `dump --addr` applies pointer decoding and can distort nonpointer data
such as doubles. The color load/store constant is (0,1), and clear color is
(0,0,0,1). The public-default host
fixtures assign the checked values explicitly instead of borrowing host defaults.
No additional host/device default mismatch was found in the remaining path.

Reproduce against the same external cache; set `ANALYSIS` to an ignored analysis
directory, and keep every generated output there:

```sh
"$IPSW" dyld info "$DSC" > "$ANALYSIS/cache-info.txt"
shasum -a 256 "$DSC"
"$IPSW" dyld disass "$DSC" --image Metal > "$ANALYSIS/Metal.disass"
# Focused reproductions of the gate/default findings:
"$IPSW" dyld disass "$DSC" --symbol-image Metal \
  --symbol '-[MTLRenderPipelineDescriptorInternal init]'
"$IPSW" dyld disass "$DSC" --symbol-image Metal \
  --symbol '-[MTLRenderPipelineDescriptorInternal reset]'
"$IPSW" dyld disass "$DSC" --symbol-image Metal \
  --symbol '-[MTLRenderPipelineDescriptorInternal vertexDescriptor]'
"$IPSW" dyld disass "$DSC" --symbol-image Metal \
  --symbol '-[MTLRenderPassDescriptorInternal init]'
"$IPSW" dyld disass "$DSC" --symbol-image Metal \
  --symbol '-[MTLTextureDescriptorInternal init]'
# Constants referenced by pipeline init/reset and texture init:
"$IPSW" dyld dump "$DSC" 0x181b60200 --bytes --size 16 | xxd
"$IPSW" dyld dump "$DSC" 0x181b52e20 --bytes --size 16 | xxd
"$IPSW" dyld dump "$DSC" 0x181b5f1e0 --bytes --size 16 | xxd
"$IPSW" dyld dump "$DSC" 0x181b5fef0 --bytes --size 32 | xxd
```

## M5c build, checks and staging

The assigned Mac lane built only the changed frontend with the same M5b
compiler flags, external Vulkan headers and ad-hoc signing. It retained the
byte-identical M5b smoke, translator, metallib, configs, source archive and ICD.
The external translator cache is clean at
`720ef1ce9ae2a2068999a8675717418b9416283e`; it remains dlopened, with no
translator load command or linked translator objects in FuxiMetal.

Reproduce the frontend-only rebuild from an existing verified M5b output set:

```sh
SDK=$(xcrun --sdk iphoneos --show-sdk-path)
"$(xcrun --sdk iphoneos --find clang++)" \
  -target arm64-apple-ios16.0 -isysroot "$SDK" -O2 -Wall -Wextra -Werror \
  -Werror=unguarded-availability-new -fstack-protector-strong -fno-objc-arc -fblocks \
  -std=c++17 -I"$VULKAN_HEADERS" -dynamiclib tools/fuxi-metal/Frontend.mm \
  -install_name /System/Library/Extensions/FuxiMetal.bundle/FuxiMetal \
  -framework Foundation -framework Metal -o tools/out/FuxiMetal.bundle/FuxiMetal
codesign -f -s - tools/out/FuxiMetal.bundle
codesign --verify --strict tools/out/FuxiMetal.bundle
python3 tools/audit-fuxi-metal.py tools/out/FuxiMetal.bundle/FuxiMetal \
  tools/out/fuxi-metal-smoke tools/out/libmetal2vulkan.dylib
# Build the host ABI-v1 translator from the existing external pin/adapter:
cargo +1.98.0 build --locked --offline --release \
  --manifest-path deps/metal2vulkan-bridge/Cargo.toml
python3 tools/test-fuxi-metal-defaults.py \
  --translator deps/metal2vulkan-bridge/target/release/libmetal2vulkan.dylib \
  --metallib tools/out/metal-smoke.metallib
python3 tools/test-fuxi-metal-frontend.py
python3 tools/test-fuxi-metal-stage.py
```

Passed: the new real-library/public-default ASan/UBSan regression, the full
frontend/gate/census/smoke suite (including stages 0–3), render-pass/draw/readback
refusals, retained resource ownership and temporary-root staging/idempotence/
signature checks. Mach-O checks passed for arm64 iOS 16.0, only system loads,
no RPATH and no CTR_EL0 reads. All **211** strong imports across frontend,
smoke and translator resolve in the supplied cache's expected system providers.
The unchanged backend/translator rendering result remains M5b's host evidence;
no M5c device/GPU render is claimed. No phone, mounted production root or push
was used.

Signed artifacts and logs are under ignored `tools/out/` on the assigned Mac
lane; the complete staging set and `M5c-SHA256SUMS` are also mirrored to this
worktree's ignored `tools/out/`. Only the bundle executable changed; stage the
whole signed bundle with its existing resource-signature files. All destinations
are root:wheel (0:0), directories 0755. The staging helper and metal4 boot image
remain unchanged.

| Artifact under `tools/out/` | iOS destination | Mode | SHA256 |
| --- | --- | --- | --- |
| `FuxiMetal.bundle/FuxiMetal` | `/System/Library/Extensions/FuxiMetal.bundle/FuxiMetal` | 0755 | `c4fc86b16613bf55b2474ead9768ce6f269803d2678e8df3a636becf13d222de` |
| `FuxiMetal.bundle/Info.plist` | `/System/Library/Extensions/FuxiMetal.bundle/Info.plist` | 0644 | `2578f53d73942a55e0163fa5c80c29beb00839bfb8db477a4060e0a81ab5fd06` |
| `FuxiMetal.bundle/_CodeSignature/CodeResources` | `/System/Library/Extensions/FuxiMetal.bundle/_CodeSignature/CodeResources` | 0644 | `9fcd9223ba2b40aee792c83c8edf32c7c1847f01369fbb0f230e1108a493451b` |
| `libmetal2vulkan.dylib` | `/usr/local/lib/libmetal2vulkan.dylib` | 0755 | `015d536f2617827cb7c83fd4a58682d370881c59a9bb719f19b00908bae4bd88` |
| `metal-smoke.metallib` | `/usr/local/share/fuxi/metal-smoke.metallib` | 0644 | `c1da69afc209598f775022dae5325894c1128935334efa024d378a179c6c6464` |
| `metal2vulkan-source.tar.gz` | `/usr/local/share/fuxi/metal2vulkan-source.tar.gz` | 0644 | `9eb4d8ab39fcb53cf169ec0b450e71fe3fcdbb88bf88641f68b631a2f4d8770b` |
| `fuxi-metal-smoke` | `/usr/local/bin/fuxi-metal-smoke` | 0755 | `87e652a1915c5830b7ebda274f6f4ef1f8a63b0936a01a74f9a0a293c8b9e1a4` |
| `metal-smoke.conf` | `/usr/local/share/fuxi/metal-smoke.conf` | 0644 | `7de1555df0c2700329e815b93b32c571c3ea54dc967b89e81ab73b9972b72d1d` |
| `metal-clients.conf` | `/usr/local/share/fuxi/metal-clients.conf` | 0644 | `8d5b163a382ff4eca7579803ec5dd9e2f24f8c0a4e3d65fc6965c539b3e7e6cc` |
| `libvulkan_freedreno.dylib` | `/usr/local/lib/libvulkan_freedreno.dylib` | 0755 | `5661a054b5e4be57fe54bc38f3cccf6d264ae20562473caf72e50d2adc158a8c` |

Expected additional device lines, after stages 0–3 (the descriptor line below
is abbreviated; actual output includes every scalar/linked count and all eight
attachment format/blend/mask tuples):

```text
FUXI MTL: function fuxi_vertex class=FuxiFunction stage=1 device=1
FUXI MTL: function fuxi_fragment class=FuxiFunction stage=2 device=1
FUXI MTL: pipeline descriptor present=1 vertex=FuxiFunction/stage1/device1 fragment=FuxiFunction/stage2/device1 vertexDescriptor=1 samples=1 ...
FUXI MTL: translation PASS <milliseconds> ms (no cache)
FUXI MTL: stage 4 pipeline PASS BGRA8 256x256 inside/outside 6 red 8192 clear 57344 + sentinel edges
FUXI MTL: done PASS
```

Any remaining unsupported descriptor value should instead produce exactly the
field/value rejection line before the existing NSError/exception/stage FAIL.
Translation and device pixels still need the coordinator's next run.

---

# Metal lane M5b: external shader pipeline

## M5b external translator and offscreen pipeline

2026-10-21. **Built and host-verified; device verification pending.** No new
M5a device observation was supplied in this task; its gate/census device report
will be recorded when available. M4's reported device PASS remains the baseline.
No phone, mounted production root, kernel build/change or push was involved.
The boot image remains **metal4**, SHA256
`e10f981a0bae32ad5c892d978a786eabe4cc21acda6e35ed064f1ad68a53c4b6`.

The original source `tools/shaders/metal-smoke.metal` defines `fuxi_vertex`
(`vertex_id`, three positions) and `fuxi_fragment` (constant opaque red).
Xcode compiles this source to AIR/metallib **at build time** on the Mac.
No Apple shaders/metallibs, CA inputs, translated replacement blobs, copied
Apple implementation or firmware are tracked or used for this test.

The smoke's stages 0–3 retain their existing fill/copy/reuse flow. Stage 4,
**pipeline**, exercises both `newLibraryWithURL:error:` and
`newLibraryWithData:error:`, `newFunctionWithName:` and
`newRenderPipelineStateWithDescriptor:error:`. It renders one triangle over
opaque black into a **private 256x256 BGRA8Unorm** texture, then blits the whole
image into a shared buffer at offset 256. CPU verification checks all 65,536
pixels, six inside/outside samples, colour counts (8,192 ±128 red) and 256-byte
sentinels at both ends. A failed API, command, translation or verification
prints FAIL and terminates; there is no replacement shader/success fallback.

The original frontend owns Metal library/function/pipeline/texture/render
objects and retains command resources through submission. Its independent C
ABI v1 wrapper calls the external translator's library and render-pipeline
APIs. The translator queries the actual Vulkan physical device, restricted to
Vulkan **1.2** and the empty enable set used at `vkCreateDevice`; it supplies
SPIR-V and its planned fragment `ColorWriteRule` push bytes. Fuxi creates
Turnip shader modules, a pipeline/layout/render pass, framebuffer, render and
image-to-buffer copy commands. Negative viewport height preserves Metal's
upper-left viewport convention. The existing two-second fence wait and
poisoned-device/quarantine behavior also apply to stage 4.

This is deliberately bounded: one sample, one BGRA8 attachment, no blending,
depth/stencil, vertex attributes, shader resource bindings, specialization,
linked functions, additional draws or partial texture copies. Other descriptor
shapes/encoders fail explicitly. Only a four-byte fragment push write from the
translator's own draw planner is accepted. The gate still defaults to only
`fuxi-metal-smoke`; census mode still stops at a nil command queue before any
GPU/translator construction. Normal clients still require unsandboxed root.
The shipped `metal-smoke.conf` is **4**; accepted maximum stages are 0–4.
Missing config retains the earlier stage-3 default. `metal-clients.conf` is
byte-identical to M5a's default.

### External build and licence boundary

Source: [steelbrain/metal2vulkan](https://github.com/steelbrain/metal2vulkan),
revision **720ef1ce9ae2a2068999a8675717418b9416283e**. All workspace crates
state **GPL-2.0-or-later OR LGPL-3.0-or-later**. The approved choice for this
separate dynamic library is **LGPL-3.0-or-later**. Fuxi's own code keeps its
existing repository terms. No translator/Mesa source or objects enter the
tracked tree, kernel or FuxiMetal binary. The original adapter source is ours;
Cargo builds it and the translator into their own `libmetal2vulkan.dylib` in
ignored `deps/` caches. FuxiMetal resolves its versioned C symbols with `dlopen`
and does not link against Rust/translator archives. The dylib has only a system
`libSystem` load; FuxiMetal/smoke retain system framework/library loads.

On the assigned Mac tree, with external Vulkan headers and Xcode installed:

```sh
# Requires Rust/rustup and the Xcode Metal compiler component.
xcodebuild -downloadComponent MetalToolchain
# If Xcode's default component mapping is stale, select/install the matching
# component explicitly and set FUXI_METAL_TOOLS to its usr/bin directory.
# This build used Metal component 27A266a, compiler 32023.921.6, iPhoneOS 27.1 SDK.
xcodebuild -downloadComponent MetalToolchain -buildVersion 27A266a
bash tools/build-fuxi-metal.sh "$VULKAN_HEADERS"
```

`build-fuxi-metal-translator.sh` fetches/checks the pin into ignored
`deps/metal2vulkan`, creates the adapter crate in ignored
`deps/metal2vulkan-bridge`, installs **Rust 1.98.0** and target
`aarch64-apple-ios`, and runs:

```sh
export IPHONEOS_DEPLOYMENT_TARGET=16.0
export SDKROOT="$(xcrun --sdk iphoneos --show-sdk-path)"
export CARGO_TARGET_AARCH64_APPLE_IOS_LINKER="$(xcrun --sdk iphoneos --find clang)"
export CARGO_TARGET_AARCH64_APPLE_IOS_RUSTFLAGS='-C link-arg=-miphoneos-version-min=16.0 -C link-arg=-Wl,-install_name,/usr/local/lib/libmetal2vulkan.dylib'
cargo +1.98.0 build --manifest-path deps/metal2vulkan-bridge/Cargo.toml \
  --release --target aarch64-apple-ios
# The main build then compiles our shader and frontend:
"$METAL" -isysroot "$SDKROOT" -std=ios-metal2.4 -mios-version-min=16.0 \
  -fno-fast-math -c tools/shaders/metal-smoke.metal -o tools/out/metal-smoke.air
"$METALLIB" tools/out/metal-smoke.air -o tools/out/metal-smoke.metallib
```

All three Mach-O artifacts target plain arm64 **iOS 16.0**, are ad-hoc signed,
strict-signature verified and audited for system imports/load paths and no
EL0 `CTR_EL0` reads. All **210** strong imports across these artifacts were
found in the supplied iOS 16.7 cache (same SHA256 as the M5a analysis).
`libSystem`'s explicit reexports include `libdispatch` and `libcommonCrypto`;
the cache audit now recognizes those actual providers. Translation is measured at pipeline creation with
`steady_clock`; every call prints elapsed milliseconds and PASS/FAIL.
No persistent shader or pipeline cache is implemented. The translator's optional
runtime/cache crate is not built. No independent AIR subset alternative was
needed: the pin accepted our compiler's actual AIR.

The source archive distributed **beside** the dylib contains the library source,
original adapter, exact Cargo lockfile, registry dependency sources/notices,
Rust notices, upstream LGPL-3.0/GPL-3.0 texts and notice, build script and
`metal2vulkan-bridge/REBUILD.sh`. It omits upstream shader fixtures/corpus,
build outputs, SDKs/toolchains and git history. Extract it in an ignored cache
and run that rebuild script on a Mac with the stated toolchain; it rewrites the
external source path and uses the included registry sources with
`--locked --offline`. Ad-hoc sign the modified library and replace the same
installed dylib through the coordinator's offline root-image procedure.
The packaged source was extracted into a fresh ignored directory and successfully
rebuilt with `--locked --offline` on the Mac. This checks rebuildability, not
bit-for-bit reproducibility across source paths. No FuxiMetal relink or kernel
change is required for an ABI-compatible replacement.

Distribution must retain the LGPL/GPL texts and prominent library/use notices,
provide corresponding library source and modifications (including this adapter,
lockfile and build inputs), preserve dependency notices, and permit library
replacement/relinking and reverse engineering for debugging those modifications.
Supply installation/replacement information where required; do not impose terms
that remove these rights. This lane supplies source and the replacement recipe
instead of relying solely on a download link or on dynamic loading as an
exemption. No upstream translator code was modified.

### Validation and coordinator staging

Host evidence: real external translator + lavapipe, under frontend/backend
ASan/UBSan checks, produced **8,192 red / 57,344 clear pixels** in four renders
(two reuse rounds across two device opens). Each emitted module passes
`spirv-val --target-env vulkan1.2`. Invalid library header, missing function,
fence timeout, device loss and last-pixel corruption fail. Measured Linux-host
translation calls in the recorded run were **9.547 ms and 5.140 ms**; these are
not iOS/A740 timings. Native Mac frontend tests retain M5a gate/census coverage
and add descriptor/encoder/ownership refusal checks; the CPU model never
submits a render. Native smoke tests retain stages 0–3 and verify stage-4 failure
when its required library is unavailable. Staging tests use the real signed
artifacts on a temporary root and check ownership/modes, idempotence, stage-4
config and linked/invalid destination refusal.

```sh
python3 tools/test-fuxi-metal-frontend.py                 # Mac
python3 tools/test-fuxi-metal-stage.py                    # Mac, temporary root
python3 tools/test-fuxi-metal-backend.py --headers "$VULKAN_HEADERS"  # Linux
python3 tools/test-fuxi-metal-pipeline.py --headers "$VULKAN_HEADERS" \
  --translator "$HOST_M2V_DYLIB" --metallib tools/out/metal-smoke.metallib
# Coordinator only, on an already mounted writable iOS root:
python3 tools/stage-fuxi-metal.py "$IOS_ROOT" \
  tools/out/libvulkan_freedreno.dylib tools/out/FuxiMetal.bundle \
  tools/out/fuxi-metal-smoke
```

All destinations are **root:wheel (0:0)**; new directories are 0755.

| Artifact under `tools/out/` | iOS destination | Mode |
| --- | --- | --- |
| `libvulkan_freedreno.dylib` (unchanged) | `/usr/local/lib/libvulkan_freedreno.dylib` | 0755 |
| `libmetal2vulkan.dylib` | `/usr/local/lib/libmetal2vulkan.dylib` | 0755 |
| `fuxi-metal-smoke` | `/usr/local/bin/fuxi-metal-smoke` | 0755 |
| `metal-smoke.metallib` (our compiled source) | `/usr/local/share/fuxi/metal-smoke.metallib` | 0644 |
| `metal2vulkan-source.tar.gz` | `/usr/local/share/fuxi/metal2vulkan-source.tar.gz` | 0644 |
| `metal-smoke.conf` (`4`) | `/usr/local/share/fuxi/metal-smoke.conf` | 0644 |
| `metal-clients.conf` (unchanged) | `/usr/local/share/fuxi/metal-clients.conf` | 0644 |
| `FuxiMetal.bundle/FuxiMetal` | `/System/Library/Extensions/FuxiMetal.bundle/FuxiMetal` | 0755 |
| `FuxiMetal.bundle/Info.plist` | `/System/Library/Extensions/FuxiMetal.bundle/Info.plist` | 0644 |
| `FuxiMetal.bundle/_CodeSignature/CodeResources` | `/System/Library/Extensions/FuxiMetal.bundle/_CodeSignature/CodeResources` | 0644 |

Expected additional lines on a successful **future device run** (timing and
boundary coverage counts may vary within the verified bounds):

```text
FUXI MTL: translation PASS <milliseconds> ms (no cache)
FUXI MTL: stage 4 pipeline PASS BGRA8 256x256 inside/outside 6 red 8192 clear 57344 + sentinel edges
FUXI MTL: done PASS
```

Risks: the actual iOS `dlopen`/AIR translation/Turnip stage-4 submission and
pixels remain unobserved; host/software-driver success is not device success.
The in-process Rust translator has its own standard 20-second cooperative budget
and the diagnostic's 60-second stage alarm, not a separate hard memory-isolated
worker. Alpha translator correctness, cold RSS/latency and driver compilation
on A740 still need measurement. This does not enable CA, IOSurface import,
sampling, presentation or additional processes. Keep the gate/census stop.

Complete signed output manifest: `tools/out/M5b-SHA256SUMS`.

| M5b artifact | SHA256 |
| --- | --- |
| `tools/out/FuxiMetal.bundle/FuxiMetal` | `bb071e27f1a3b7e4f4e6b5fc369a475df826ad0e1590bdc4502da0de199edb2c` |
| `tools/out/FuxiMetal.bundle/Info.plist` | `2578f53d73942a55e0163fa5c80c29beb00839bfb8db477a4060e0a81ab5fd06` |
| `tools/out/FuxiMetal.bundle/_CodeSignature/CodeResources` | `9fcd9223ba2b40aee792c83c8edf32c7c1847f01369fbb0f230e1108a493451b` |
| `tools/out/libmetal2vulkan.dylib` | `015d536f2617827cb7c83fd4a58682d370881c59a9bb719f19b00908bae4bd88` |
| `tools/out/metal-smoke.metallib` | `c1da69afc209598f775022dae5325894c1128935334efa024d378a179c6c6464` |
| `tools/out/metal2vulkan-source.tar.gz` | `9eb4d8ab39fcb53cf169ec0b450e71fe3fcdbb88bf88641f68b631a2f4d8770b` |
| `tools/out/fuxi-metal-smoke` | `87e652a1915c5830b7ebda274f6f4ef1f8a63b0936a01a74f9a0a293c8b9e1a4` |
| `tools/out/metal-smoke.conf` | `7de1555df0c2700329e815b93b32c571c3ea54dc967b89e81ab73b9972b72d1d` |
| `tools/out/metal-clients.conf` | `8d5b163a382ff4eca7579803ec5dd9e2f24f8c0a4e3d65fc6965c539b3e7e6cc` |
| `tools/out/libvulkan_freedreno.dylib` (unchanged) | `5661a054b5e4be57fe54bc38f3cccf6d264ae20562473caf72e50d2adc158a8c` |


## M5a process safety gate

2026-10-20. The coordinator reports **M4 observed PASS**: native metaclass
matching, system Metal discovery (528-byte base and subclass), 64 KiB fill,
completion handlers, copy with sentinel edges, 16 reuse rounds and done PASS;
no panic, with the home screen continuing on the CPU path. backboardd and
SpringBoard also loaded the M4 bundle. They must not receive a transfer-only
device during normal UI operation.

The plugin now checks `/usr/local/share/fuxi/metal-clients.conf` before any
IOKit lookup or Turnip construction. The installed default contains only
`fuxi-metal-smoke`. It compares the actual dyld executable basename, independent
of exec-swap's preserved daemon argv. Missing, unreadable, linked, oversized,
untrusted or writable-by-others files deny access. Names match exactly;
comments/blank lines are allowed. Policy is cached until process exit and logs
one `FUXI MTL: gate allow <proc>` or `FUXI MTL: gate deny <proc>` per process.
Staging installs the policy root:wheel **0:0 0644**.

Nil-init verification, same iOS 16.7 cache SHA256 documented below:
`_MTLRegisterDevices` calls `initWithAcceleratorPort:` at `0x181b14580`;
its nil branch at `0x181b14584` goes directly to `0x181b14594`, releases the
service at `0x181b14598`, and advances the iterator at `0x181b145a0`.
`_MTLAddDevice` at `0x181b1458c` is skipped. This is an explicit supported
absence path, not a call through nil into registration. The default-device
array initializes once; no initializer-failure retry loop is present there.

Native Mac gate tests passed under ASan/UBSan: 64 denied initialization
attempts return nil with one policy read/log, before IOKit/backend access;
exact names, malformed input, NULs, conflicts, missing/untrusted/writable files
and symlinks fail closed. Existing frontend and smoke tests passed. The arm64
iOS 16.0 plugin and smoke were built, ad-hoc signed, signature-verified and
audited. No kernel changes, phone access, mounted-root edit or push.

## M5a census and the CA stop point

Add `census backboardd` to the same policy file for an **explicit diagnostic
run**, then restart the process/reboot; the shipped file does not enable this.
A census entry grants a device even to a nonroot process, but **never constructs
Turnip, opens a native GPU client, allocates GPU memory or submits work**.
It uses registryID 0, conservative M4 capabilities and associated state with no
backend. Normal allow entries retain M4's root requirement and transfers.

Every implemented Metal API entry logs its first call. Unsupported messages on
the device and resource classes enter Objective-C forwarding with encodings from
the private superclass/public protocols; private protocols loaded in the process
also supply signatures. NSInvocation returns zero bytes for the actual return
size, covering nil, integer, float, structures (including indirect returns), and
void no-op. Inherited private device implementations cannot execute against the
uninitialized base. A mutex protects a fixed **256 (class, selector)** table
shared across all frontend objects; the logger allocates nothing and duplicate
calls produce no further lines, and reaching capacity prints one limit line. Lifetime and
forwarding machinery itself is excluded from API logging.

An entirely untyped private selector uses the conventional object/word nil
signature and is logged. Objective-C supplies no ABI for such a selector:
arbitrary unknown floating/structure returns cannot be guaranteed by guessing.
The typed Metal/public and discovered private protocol cases are covered by the
host tests; this is not a universal substitute for an unknown private ABI.
Unimplemented textures, libraries and pipelines return nil; census does not
fabricate successful resources. No texture/render/compute object is vended yet;
forwarding applies to such protocol methods when a diagnostic object exposes
them. M4 buffer/queue/command-buffer/blit classes have the same instrumentation.

**Safety takes priority over depth:** census `newCommandQueue` returns nil.
The cache's `new_metal_context` handles this by returning null
(`0x188487ef0..0x188488108`). Allowing a queue would advance into loading
QuartzCore's metallib; returning nil there **aborts the client**, rather than
selecting software. Consequently M5a learns the safe first blocker dynamically;
the deeper census below is static cache evidence, not an observed backboardd
trace or a claim that all CA shaders form a small fixed set. Advance the stop
point only as each real requirement is implemented and independently tested.
The generic forwarding tests can exercise queue/command/encoder/texture protocol
messages without opening a GPU; these do not qualify CA runtime behavior.

Expected new lines (examples, not an M5a phone observation):

```text
FUXI MTL: gate allow fuxi-metal-smoke
FUXI MTL: gate deny backboardd
FUXI MTL: gate deny SpringBoard
```

With an explicitly staged census entry, replace backboardd's deny with:

```text
FUXI MTL: gate allow backboardd
FUXI MTL: census enabled backboardd (no GPU)
FUXI MTL: census backboardd FuxiMTLDevice initWithAcceleratorPort: implemented
FUXI MTL: census backboardd FuxiMTLDevice newCommandQueue implemented
```

The queue line occurs only if CA actually asks for one. Registration may log
other selectors first. The existing smoke stages should still end `done PASS`
for the default allowlisted executable. No new on-device run was performed.

## Repeated matching investigation

The supplied M4 report shows many **matching1** requests. In the existing XNU
source that diagnostic is in `internal_io_service_get_matching_service`, i.e.
**IOServiceGetMatchingService**, not notification delivery or the iterator used
by Metal registration. `IOTaskRegistryCompatibilityMatching` adds
`IOServiceNotificationUser=true` to ordinary requests unconditionally; that key
is not proof of a notification re-arm. The service publishes once and does not
re-register/change matching properties after publication.

In this cache, Metal's `_MTLRegisterDevices` uses
**IOServiceGetMatchingServices**, enumerates once and is reached through the
device-array `dispatch_once` (`_MTLCreateSystemDefaultDevice`,
`0x181a37770..0x181a37828`). Initializer failure does not schedule a retry.
QuartzCore's accelerator wait (`0x18853e664..0x18853e6d4`) retries only while
**no service exists**, in 100 ms steps with a bounded counter; finding our
service exits that wait. It cannot explain repeated successful matching1 calls.

OpenGLES `_eagl_init` performs a single-service IOAcceleratorES probe at
`0x1d3f525d4..0x1d3f525e4`; `_EAGLMemoryNotificationRecycling` does another at
`0x1d3f528b4..0x1d3f528c4`. These are concrete alternative call sites for the
observed request shape. EAGL init can fail to establish its GL renderer and be
called again by clients; this service implements no GL renderer. Neither lookup
contains a property-wait or notification retry loop. The report has no caller
stacks, so attributing all requests to one function would be speculation.
**No kernel/property change is justified.** Do not add fictitious Apple/GL
plugin properties to suppress legitimate failed GL probes. The safety gate
removes our Metal device from those processes without changing service matching.

## CoreAnimation requirements from the iOS 16.7 cache

Same iPhone10,3 / 20H392 arm64 cache and SHA256 as below. Addresses are unslid.
These are call-site findings; branches depend on renderer flags, device limits
and workload, and are not evidence that every listed path runs on fuxi.

| Requirement | Cache evidence and consequence |
| --- | --- |
| Device/queue selection | `CAMetalContextCreate` (`0x18838421c`) calls the system default-device API. `CA::OGL::new_metal_context` (`0x188487e28`) creates a queue, checks queue/device identity, labels it, and conditionally sets GPU/background priorities and submission/completion queues. Queue failure returns null; a non-nil MTLDevice is not proof of CA compatibility. |
| Init state | `0x188488078..0x18848809c`: render-pass and depth/stencil descriptor/state creation. `0x18848822c..0x188488364`: `supportsMemorylessRenderTargets`, optional `CA_ENABLE_MEMORYLESS`, and `minConstantBufferAlignmentBytes`. These private queries need honest values, not inherited Apple implementations. |
| Feature/family/limits | `0x1884886d4..0x188488adc`: `supportsFamily:` includes Apple1/2/3/5 (1001/1002/1003/1005), vendorName, maxFragmentTextures/Samplers, maxTextureWidth2D/Height2D, maxColorAttachments, supportsRasterOrderGroups, supportsYCBCRFormats12/packed variants, supportsNativeHardwareFP16, supportsTileShaders, supportsSIMDGroup and iosurfaceReadOnlyTextureAlignmentBytes. `supportsFeatureSet:` gates offline pipeline variants at `0x1883758b8/0x1883758e0` and `0x18847eb30..0x18847ebac`. `readWriteTextureSupport` has **no direct call found** in this QuartzCore image; retain its conservative zero answer and census it if another framework asks. |
| Its own libraries | `0x1884883e0..0x188488424`: NSBundle for com.apple.QuartzCore resolves **default.metallib**, then calls **newLibraryWithURL:error:**. No direct `newLibraryWithData:` or `newDefaultLibrary` call was found here; supporting those alone would miss this entry. The frontend ultimately needs a URL-to-metallib/AIR path with checked lengths and errors, without invoking Apple's GPU compiler. |
| Function specialization | `load_shader_function` (`0x188482774`), especially `0x1884829bc..0x1884829e8`, uses MTLFunctionDescriptor, name, constant values and `newFunctionWithDescriptor:error:`. The vertex path at `0x1884862b0` uses precision suffixes and constants including fc_generic, fc_vertex_layout, fc_texcoord_count and fc_base_color; the fragment path starts at `0x18848566c`. Translation must include specialization and binding/vertex-layout metadata. |
| Pipelines | `create_pipeline_state` (`0x188380b94`) configures vertex/fragment functions, attachments, formats and blend/write state, then calls **newRenderPipelineStateWithDescriptor:error:** at `0x188381b9c/0x188381bf4`. Tile pipelines at `0x18848214c` and compute pipelines at `0x188482e20` are additional workload paths. Optional **default.pipelinelib**, newPipelineLibraryWithFilePath:error: (`0x188488534`), and lookup by pipeline name (`0x188375908`) are Apple precompiled-pipeline optimizations, not portable Vulkan binaries. |
| Pixel formats | `four_cc_to_mtl_format` (`0x1882d69f4..0x1882d7084`) converts IOSurface FourCCs using MetalLimits. Concrete cases include BGRA -> 80/BGRA8Unorm (`0x1882d6c1c..0x1882d6c3c`), A8 -> 1, L8 -> 10/R8Unorm, L16 -> 20/R16Unorm. Other paths include wide/half-float and YCbCr formats gated by limits. Unsupported combinations return format 0. First target BGRA8Unorm, then sampling/sRGB/plane formats actually requested; do not advertise the whole conversion table. |
| IOSurface textures | `create_surface_from_iosurface` (`0x188366dd0`): validates dimensions/format/limits, makes a texture descriptor with usage 5 (shader read + render target), applies protection options, calls **newTextureWithDescriptor:iosurface:plane:** at `0x18836702c` (plane 0 in this path), and cleans up if texture creation returns nil. Other multi-plane/import paths exist at `0x18830afc8`, `0x18831ee5c`, `0x1883471a4`. Real import requires HostImport/page lifetime/coherence and synchronization, absent from the current ABI. |
| Events/fences | CAMetalDrawable's **sharedEvent** is consumed at `0x18829e0c8`; `-[CAMetalDrawable sharedEvent]` is at `0x18829e240`. No direct **newSharedEvent** factory call was found in this QuartzCore image. This distinguishes drawable-supplied event consumption from a device-init requirement. Event ownership, wait/signal and cross-process lifetime still need census/implementation before CAMetalLayer presentation. |
| Software fallback | `CA::CG::AccelRenderer` selection at `0x188507960..0x188507af8`: nil device, disabled Metal route, or null Metal-context result goes to software creation when the caller's surface/backend policy permits it; another branch returns null. The window-server get_ctx block (`0x18853bfe8`) skips Metal-context construction for nil device. These branches do not establish universal fallback for every CA client. |
| Fatal failures after selection | Missing bundle/metallib URL hits assertions; nil library follows `0x188488424 -> 0x188488c80..0x188488d68` into **abort_with_payload**. Render-pipeline failure has a similar fatal diagnostic. Returning nil for every unsupported API is ABI-safe forwarding but is **not** sufficient to keep an already-selected CA Metal renderer alive. Deny by default, and retain the early census queue stop. |

Minimal shader candidates identified **statically**, not from a live census:
`std_vert0`/`std_vert1` in the vertex type table (`0x1d5b48050`), their
`_lph`/`_lpf` precision variants, and the fallback fixed vertex/fragment families
(`0x18848634c`, `0x188485724`), including `fixed_vert_lpf_spc`,
`fixed_vert_lph_spc`, `fixed_frag_lpf_cpf` and `fixed_frag_lph_cph`. Many blur/path/image/compositing variants and
function constants also exist. Do not infer a small complete shader set from
a first solid-background test.

Reproduce using external tools/Apple inputs, saving all output only in an
ignored analysis directory; no disassembly, cache, metallib or Apple source
belongs in git:

```sh
"$IPSW" dyld disass "$DSC" --symbol _MTLRegisterDevices --symbol-image Metal
"$IPSW" dyld disass "$DSC" --symbol _MTLCreateSystemDefaultDevice --symbol-image Metal
"$IPSW" dyld macho "$DSC" QuartzCore --symbols
"$IPSW" dyld disass "$DSC" --image QuartzCore > "$ANALYSIS/QuartzCore.disass"
"$IPSW" dyld disass "$DSC" --vaddr 0x188487e28 --count 1100
"$IPSW" dyld disass "$DSC" --vaddr 0x188380b94 --count 1150
"$IPSW" dyld disass "$DSC" --vaddr 0x188366dd0 --count 200
"$IPSW" dyld disass "$DSC" --vaddr 0x188507950 --count 110
"$IPSW" dyld dump "$DSC" 0x1d5b48050 --count 54 --addr
"$IPSW" dyld disass "$DSC" --symbol _eagl_init --symbol-image OpenGLES
"$IPSW" dyld disass "$DSC" --symbol _EAGLMemoryNotificationRecycling --symbol-image OpenGLES
```

## Shader translation decision

**The user approved (a) for M5b: an external dynamically loaded metal2vulkan
adapter.** M5a itself links none of it. M5b starts with our own constant-colour
MSL, not the CA shader candidates in this earlier survey. Cache design below
is future work; M5b has no shader/pipeline cache. Reviewed the actual pin
`720ef1ce9ae2a2068999a8675717418b9416283e`, including Cargo manifests, API,
AIR front end and cache documentation. The repository identifies itself as
alpha; this is a candidate, not an established CA/A740 translator. No CA
metallib was translated or benchmarked in M5a.

Effort below is an engineering estimate, excluding IOSurface/kernel work:

| Option | Concrete integration, effort and legal boundary | Device load cost and caching | First target |
| --- | --- | --- | --- |
| (a) external metal2vulkan | Medium: days for a pinned AIR parse/translation probe and narrow versioned C ABI adapter, weeks or more for specialization/binding/format correctness and iOS integration. The pin exposes Rust crates, not a ready stable C ABI/cdylib; build the adapter in the **external tree**, dlopen it from our original frontend, and pass bounded bytes/descriptors, results and free callbacks. Its AIR front end already understands metallib/bitcode, metadata, intrinsics and constants and emits SPIR-V plus pipeline/binding plans. All workspace crates declare **GPL-2.0-or-later OR LGPL-3.0-or-later**. Select/comply with LGPL for a separately replaceable dynamic library: notices/license texts, corresponding library source and modifications, and required relinking/replacement/installation rights/information. Dynamic loading alone does not waive those obligations. No static incorporation or vendoring here. Apple inputs and derived shader outputs stay external/private, outside repository/distribution artifacts. | Parse at library load; specialize/translate requested entry points at pipeline creation, then Turnip SPIR-V -> NIR/ir3 compilation. The library requires no llvm-as subprocess for binary AIR. The translator supplies content/profile/version keys and artifact encodings; it does **not** itself persist a cache. Its optional runtime has disk/memory and driver pipeline caches, but defaults (25 GiB disk/256 MiB memory) are unsuitable here. Use an explicit small bounded cache, atomic writes and corruption-as-miss. No A740 timing claim. | One std_vert0_lpf variant (or fixed_vert_lpf_spc) plus a fixed_frag_lpf_cpf constant-color specialization from CA's private default.metallib, BGRA8Unorm, one sample, no texture, no destination read/tile/compute. Get exact entry/constants/bindings from the external metallib; cache names alone do not establish their AIR ABI. |
| (b) independent LLVM AIR -> SPIR-V | High: weeks for a narrow prototype, months for robust graphics. LLVM reading bitcode is only the front end: independently lower AIR metadata, address spaces, resource binding, sampling/interpolation, derivatives, function constants and graphics stage ABI into Vulkan Shader-capability SPIR-V. Khronos SPIRV-LLVM-Translator explicitly targets **OpenCL Kernel** SPIR-V; it is not a drop-in Vulkan vertex/fragment compiler. LLVM version compatibility and bitcode dialect remain checks. Observe LLVM/translator licenses and notices for the exact selected versions; using metal2vulkan's AIR crate/parser would retain its GPL/LGPL boundary rather than creating an independent permissive path. Never copy Apple's compiler implementation. | Larger LLVM dependency, startup/RSS and optimization cost on cold loads; still needs Turnip compilation. Translate only requested specializations, with finite budgets and the same content/ABI/driver cache keys. Host-assisted translation may speed experiments but is not the required on-device runtime. | The same constant-color pair, first proving metadata/resource/vertex IO lowering and spirv-val Vulkan validation. A generic llvm-spirv invocation is not this milestone. |
| (c) original hand-ported replacements | Low for one offscreen constant-color test (days); medium/high and open-ended for CA compatibility (weeks/months as variants grow). Reimplement the observed interface and mathematically specified transform/color behavior in original GLSL/SPIR-V. No copied/decompiled Apple shader bodies, embedded Apple metallibs or instruction-by-instruction transcription; keep provenance for each implementation. Map only understood entry name + specialization + layout tuples and refuse every other variant. A small live working set has **not** been established. | Ship only our own compiled SPIR-V; no AIR translation at load. Still pay Vulkan pipeline/ir3 compilation once, cache by replacement version + specialization + render state + driver identity. Lower startup/RSS than an LLVM path but a growing compatibility table. | Original transform + solid premultiplied-color pair, one BGRA8Unorm target. Useful as a separately named validation control for (a); not evidence that it replaces CA's shader library. |

Sources: pinned [manifest/license](https://github.com/steelbrain/metal2vulkan/blob/720ef1ce9ae2a2068999a8675717418b9416283e/Cargo.toml),
[API and caching](https://github.com/steelbrain/metal2vulkan/blob/720ef1ce9ae2a2068999a8675717418b9416283e/docs/HOWTO.md),
and Khronos [translator scope](https://github.com/KhronosGroup/SPIRV-LLVM-Translator#llvmspir-v-bi-directional-translator).

M5b's concrete gate: parse the user's own external QuartzCore metallib, list
entry points/constant IDs/binding metadata, choose the pair above, translate
under a finite budget, validate Vulkan SPIR-V, create a bounded offscreen
256x256 BGRA target and compare every pixel for an identity-transform solid
quad plus edge sentinels. Record cold translation/pipeline time, warm cache
load time and RSS on device; restart and test cache invalidation after a
translator/profile/driver change. Validate color order, alpha, clipping and
vertex layout before attempting texture sampling. Keep backboardd denied:
passing that test does not supply the IOSurface/shared-event/presentation paths.

## M5a build, verification and coordinator staging

No XNU source/patch change. `build-fuxi-metal5a-images.sh` copies the M4 stripped
and unstripped kernels, ADT and **exact command-line file**, then packs the new
image. The resulting boot image is byte-identical to M4. The userspace bundle
and policy must be staged for the gate to take effect; booting the renamed
image alone does not install them.

```sh
# Native Mac, same external pinned Vulkan headers:
bash tools/build-fuxi-metal.sh "$VULKAN_HEADERS"
python3 tools/test-fuxi-metal-frontend.py
python3 tools/test-fuxi-metal-stage.py
# Linux actual backend with software Vulkan and injected faults:
python3 tools/test-fuxi-metal-backend.py --headers "$VULKAN_HEADERS"
# External absolute firmware/tool paths, same as M4:
export FUXI_GMU_FIRMWARE FUXI_SQE_FIRMWARE FUXI_GPU_ZAP_MBN MKBOOTIMG
bash tools/build-fuxi-metal5a-images.sh "$METAL4_ARTIFACTS"
python3 tools/test-fuxi-metal-artifacts.py --lane metal5a \
  --base "$METAL4_ARTIFACTS" --out xnu/out-fuxi-metal5a \
  --firmware "$FUXI_GMU_FIRMWARE" --sqe "$FUXI_SQE_FIRMWARE" --zap "$FUXI_GPU_ZAP_MBN"
# Coordinator only, on an already mounted writable root, as root:
python3 tools/stage-fuxi-metal.py "$IOS_ROOT" \
  tools/out/libvulkan_freedreno.dylib tools/out/FuxiMetal.bundle \
  tools/out/fuxi-metal-smoke --conf tools/out/metal-smoke.conf \
  --clients tools/out/metal-clients.conf
```

Passed: Mac ASan/UBSan frontend/gate/census and actual smoke control-flow tests;
Linux software-Vulkan backend fill/copy/reopen plus timeout/loss/corruption;
temporary-root staging and **codesign verification of the staged bundle** on
Mac; existing Metal ABI/containment tests; artifact geometry, firmware sections,
M4 byte equality and hash manifests. Plain arm64 iOS 16.0 artifacts were ad-hoc
signed and signature-verified; load-command/CTR_EL0 audit and all **140** strong
imports against the supplied iOS 16.7 cache passed. Audit reports now live
outside the signed bundle and stale reports are removed before signing, so
staging the three intended bundle members preserves its resource signature.
These checks qualify host behavior/packaging, not M5a phone execution.

Install all files below, **root:wheel (0:0)**. The default policy is exactly
`fuxi-metal-smoke` plus newline, with no system UI allow/census entries. No
launchd job, mounts, phone access, push, shader translator or firmware staging
is part of this helper.

| Artifact under `tools/out/` | iOS destination | Mode |
| --- | --- | --- |
| `libvulkan_freedreno.dylib` (unchanged M4) | `/usr/local/lib/libvulkan_freedreno.dylib` | 0755 |
| `fuxi-metal-smoke` (unchanged M4) | `/usr/local/bin/fuxi-metal-smoke` | 0755 |
| `metal-smoke.conf` | `/usr/local/share/fuxi/metal-smoke.conf` | 0644 |
| **`metal-clients.conf`** | **`/usr/local/share/fuxi/metal-clients.conf`** | **0644** |
| `FuxiMetal.bundle/FuxiMetal` | `/System/Library/Extensions/FuxiMetal.bundle/FuxiMetal` | 0755 |
| `FuxiMetal.bundle/Info.plist` | `/System/Library/Extensions/FuxiMetal.bundle/Info.plist` | 0644 |
| `FuxiMetal.bundle/_CodeSignature/CodeResources` | `/System/Library/Extensions/FuxiMetal.bundle/_CodeSignature/CodeResources` | 0644 |

| M5a artifact | SHA256 |
| --- | --- |
| `xnu/out-fuxi-metal5a/kernel.development.fuxi` | `8bfeae158e8250e906e3b3ffbcbddb8f5d63404eedd9807cf8e2838cf7a1b5d9` |
| `xnu/out-fuxi-metal5a/kernel.development.fuxi.unstripped` | `858e6cfe0b636530f7f8d097f23ca84a5f7efd0516093b74c5aba90431d353a8` |
| `xnu/out-fuxi-metal5a/fuxi.adt` | `39aacea4d7db372314bdc7928a66271b3b5cc2bd878bec4d9cfe2f5191e83533` |
| `shim/out/fuxi-metal5a-payload.bin` | `99ac95954ae0c743dac817d8be460933bcc99e7b05dff98925b4cda14ad7bcd8` |
| `shim/out/fuxi-metal5a-boot.img` | `e10f981a0bae32ad5c892d978a786eabe4cc21acda6e35ed064f1ad68a53c4b6` |
| `shim/out/fuxi-metal5a-loader.elf` | `0e5ae80e22a6a88d5e880a97d8b66a7c41750bae804728d8c99f76d9e23c07e9` |
| `tools/out/libvulkan_freedreno.dylib` | `5661a054b5e4be57fe54bc38f3cccf6d264ae20562473caf72e50d2adc158a8c` |
| `tools/out/FuxiMetal.bundle/FuxiMetal` | `1c4c8ab83cff014d45cb6dd8cb55802ff1ce7d8b693ca219967d0cfc0e6da37c` |
| `tools/out/FuxiMetal.bundle/Info.plist` | `2578f53d73942a55e0163fa5c80c29beb00839bfb8db477a4060e0a81ab5fd06` |
| `tools/out/FuxiMetal.bundle/_CodeSignature/CodeResources` | `9fcd9223ba2b40aee792c83c8edf32c7c1847f01369fbb0f230e1108a493451b` |
| `tools/out/fuxi-metal-smoke` | `64e747d6a7079fee0a675b2f757752c1569b20c19484259d5122fcc63467aec7` |
| `tools/out/metal-smoke.conf` | `1121cfccd5913f0a63fec40a6ffd44ea64f9dc135c66634ba001d10bcf4302a2` |
| `tools/out/metal-clients.conf` | `8d5b163a382ff4eca7579803ec5dd9e2f24f8c0a4e3d65fc6965c539b3e7e6cc` |

Complete manifests and analysis/test output are ignored. The supplied M4
device input remains untracked. Only the original source, tools and this
summary are committed; no external dependency, Apple code/binary, device log,
firmware, machine-specific path or address is included.

---

# Metal lane M4: native discovery and transfer-only frontend

2026-10-20. **Observed PASS on device, coordinator report above.** M3
passed the coordinator's metal3 device run (report below). M4 adds a native
`IOAcceleratorES` discovery metaclass, the `FuxiMetal.bundle` Objective-C++
frontend and the root `fuxi-metal-smoke` executable. The executable calls the
system **MTLCreateSystemDefaultDevice()**: no interposition, direct constructor,
Apple family/user-client ABI, shader compiler or MSL translation is used.
No phone access, mounted-root edit or push was performed.

## M4 route decision and evidence

**Route (a)** is small enough in this kernel. An original empty
`IOAcceleratorES : IOService` class is defined in the existing native client
translation unit; `NativeAdrenoGpuPublished` now derives from it. The published
service keeps its name, root-only open types and NativeAdrenoABI contract, and
adds string properties `MetalPluginName=FuxiMetal` and
`MetalPluginClassName=FuxiMTLDevice`. It checks `metaCast("IOAcceleratorES")`
before publication and logs PASS/FAIL. No IOGPUFamily implementation is added.
The canonical source and regenerated 0100 delta remain byte-checked together.

Re-derived from the same iOS 16.7 cache identified below:

| Check | Evidence and implication |
| --- | --- |
| Metal lookup | `_MTLRegisterDevices`, `0x181b14514..0x181b1452c`, passes `IOAcceleratorES` to IOServiceMatching/GetMatchingServices. It allocates the returned plugin class and calls `initWithAcceleratorPort:` at `0x181b14578..0x181b14580`. |
| Exact dictionary semantics | `_IOServiceMatching`, `0x18e556804..0x18e556810`, constructs the **IOProviderClass** string property. In the cached XNU 8796 sources, `IOService::copyExistingServices` calls `OSMetaClass::applyToInstancesOfClassName`; the instance set recursively includes subclass sets. `matchInternal` uses `metaCast` when the class test was not already completed. This matches the **service's metaclass ancestry**, not its provider object's class, registry name, or an IOClass property. Defining the missing base metaclass is sufficient for this lookup. |
| Bundle loading | `_getMetalPluginClassForService`, `0x181a3c7c8..0x181a3c8f4`, constructs `/System/Library/Extensions/FuxiMetal.bundle`, tries `classNamed:`, falls back to `principalClass`, checks the result is an `_MTLDevice` subclass, then returns it. |
| Runtime class ownership | `-[NSBundle classNamed:]`, `0x1811e2b5c`, loads the bundle but requires `bundleForClass:` to equal that bundle. A runtime-created class has no image ownership. Therefore the plugin deliberately relies on the documented **principalClass fallback**, with `NSPrincipalClass=FuxiMTLDevice`. `__NSBundleGetPrincipalClassFromInfoDict`, `0x1811f17c0`, resolves that value with NSClassFromString without the ownership test. |
| Registration hooks | `_MTLAddDevice`, `0x181a1b3b0..0x181a1b3f0`, checks MTLDevice and MTLDeviceSPI conformance, then calls `initLimits` and `initFeatureQueries`. The frontend implements those registration hooks; capability queries use its own conservative overrides rather than Apple's family tables. The default-device block at `0x181a37050` returns the sole registered device. |

Reproduce these checks with external Apple inputs; keep the output ignored:

```sh
"$IPSW" dyld disass "$DSC" --symbol _MTLRegisterDevices --symbol-image Metal
"$IPSW" dyld disass "$DSC" --symbol _getMetalPluginClassForService --symbol-image Metal
"$IPSW" dyld disass "$DSC" --symbol _IOServiceMatching \
  --symbol-image /System/Library/Frameworks/IOKit.framework/Versions/A/IOKit
"$IPSW" dyld disass "$DSC" --symbol '-[NSBundle classNamed:]' --symbol-image Foundation
"$IPSW" dyld disass "$DSC" --symbol __NSBundleGetPrincipalClassFromInfoDict --symbol-image Foundation
"$IPSW" dyld disass "$DSC" --symbol _MTLAddDevice --symbol-image Metal
"$IPSW" dyld disass "$DSC" --symbol ___MTLCreateSystemDefaultDevice_block_invoke --symbol-image Metal
```

This establishes the lookup contract by disassembly/source review and native
linking. The coordinator subsequently observed the boot-time metaclass check and
actual bundle/Metal registration PASS, as recorded in M5a above.
M4a direct construction was unnecessary once this small route was established.

## M4 frontend contract

`tools/fuxi-metal/Frontend.mm` registers `FuxiMTLDevice` with
objc_allocateClassPair only if **class_getInstanceSize(_MTLDevice)==528**.
It adds no private-layout ivars: its instance size is also 528. State lives in
an associated owner object. The private superclass initializer/deallocator
is bypassed; inherited private operations are replaced with forwarding, and
NSObject lifetime/introspection behavior is supplied explicitly. Runtime
protocol signatures govern unsupported calls: object results log and return
nil; unsupported scalar/struct/void calls log and raise, never report success.
The native ABI remains root-only, and plugin initialization also requires root.
The registryID comes from IORegistryEntryGetRegistryEntryID on the actual
accelerator port; the cache defines that dynamically resolved IOKit symbol.

Supported: name/registryID, a command queue, retained command buffers, shared
buffers (with or without initial bytes), contents/length, blit fill/copy,
endEncoding, commit/wait/status/error and completion handlers before or after
completion. Creation accepts default shared options **0** and lengths
**1..16 MiB**. Transfer offsets/lengths must be in bounds and **4-byte aligned**;
overlapping copies and unsupported options fail loudly. The frontend advertises
no Metal GPU family/feature set, shader/texture sampling, ray tracing, dynamic
libraries or render/compute thread capacity. This diagnostic subset is not a
complete Metal family. Inherited ABI limits (64 BOs, 256 MiB live wired budget,
one client and one hardware job) still apply; allocation failure returns nil.

`Backend.hpp` directly dlopens `/usr/local/lib/libvulkan_freedreno.dylib` after
setting the same service/open/debug environment as M3: NativeAdrenoGpuPublished,
open type 0, TU_DEBUG=nocb and MESA_LOG_LEVEL=warning unless the environment
already sets it (info printed one line per submit). It requires the observed
Qualcomm device identity. Operations use vkCmdFillBuffer/vkCmdCopyBuffer, explicit
host/transfer and transfer/transfer barriers, mapped-memory flush/invalidate,
and a finite **2 s fence wait**. Commit is synchronous; the owner mutex
serializes Vulkan work/allocation/destruction. Encoded resources stay retained
through completion. Unknown ownership after submit/fence failure poisons the
backend and skips ordinary driver/resource teardown; process death and the
existing kernel containment path own cleanup. There is no recovery claim.

The smoke ignores every argv value and reads a prefix stage **0..3** from
`/usr/local/share/fuxi/metal-smoke.conf` (absent defaults to 3). Malformed or
unreadable config fails at stage 0. Stdout/stderr are unbuffered. Discovery and
fill/copy have 30 s process alarms, reuse 60 s, cleanup 10 s. Failure exits
without driver teardown. The tool enforces the FuxiMTLDevice class and size,
not merely any non-nil device. Expected output, **not device observations**:

```text
FUXI MTL: stage 0 discovery PASS name Fuxi Turnip Adreno 740 (M4 transfers only) class FuxiMTLDevice base-size 528 instance-size 528 registryID <actual ID>
FUXI MTL: stage 1 fill PASS verified 65536 bytes + completion handlers
FUXI MTL: stage 2 copy PASS verified CPU-write/GPU-copy/readback + sentinel edges
FUXI MTL: stage 3 reuse PASS verified 16 fill/copy rounds
FUXI MTL: done PASS
```

The kernel should additionally report `FUXI MTL: IOAcceleratorES metaclass match
PASS plugin FuxiMetal/FuxiMTLDevice`; plugin stderr reports runtime class sizes.
Require all selected stages, no GPU loss/SMMU fault/panic, and CPU-present
fallback continuity. Bundle trust/loading, sandboxed clients and competition
for the single native connection remain unqualified.

## M4 rebuild, staging and verification

On the assigned native Mac tree, using the external pinned M3 Vulkan headers:

```sh
bash tools/build-fuxi-metal.sh "$VULKAN_HEADERS"
python3 tools/test-fuxi-metal-frontend.py
# Linux: actual backend on the external software Vulkan ICD, with ASan/UBSan
python3 tools/test-fuxi-metal-backend.py --headers "$VULKAN_HEADERS"
```

The build targets plain arm64-apple-ios16.0 with the iPhoneOS 27.1 SDK and
ad-hoc signs/verifies the bundle and executable. Load-command audits passed:
iOS 16.0 minimum, system dependencies only, no RPATH/strong dynamic lookup and
no CTR_EL0 reads. All **126** combined strong imports were checked against the
supplied iOS 16.7 cache. No Mesa, metal2vulkan, firmware or Apple binary/source
is vendored. M5's external licensing/relinking work remains separate.

Host validation: actual Vulkan fill/copy/barriers/readback with sentinel edges,
32 submissions, buffer reuse and full backend reopen under ASan/UBSan; injected
fence timeout, loss and corruption stop execution. Native Mac Objective-C tests
use a synthetic 528-byte superclass and CPU backend, exercising size rejection,
protocols, inherited unsupported calls, ownership, handlers, errors, bounds,
alignment and overlap. The actual smoke source also passed all stage prefixes,
default/invalid configs, host argv, nil/nonroot/failure/corruption and alarm
checks with that explicit host adapter. Those tests do not qualify the private
iOS base class or real discovery. Existing ABI/backend/watchdog/quarantine,
managed-BO lifetime, NC mapping, exec-swap and patch-chain tests passed.

Native XNU was rebuilt with the standard native/IIG setup. All 32 native input
hashes match the M3 manifest except the intended canonical client change.
Outputs are in `xnu/out-fuxi-metal4/`. Its ADT is byte-identical to metal3.
The command line changes the exec-swap target to
`sweetexecswap=peakpowermanagerd:/usr/local/bin/fuxi-metal-smoke` and appends
fuxi-metal-smoke to metal3's sweetnull list; all other arguments are preserved.
Pack with all firmware/tool variables naming existing **absolute external files**:

```sh
export FUXI_GMU_FIRMWARE FUXI_SQE_FIRMWARE FUXI_GPU_ZAP_MBN MKBOOTIMG
bash tools/build-fuxi-metal4-images.sh "$METAL3_ARTIFACTS"
python3 tools/test-fuxi-metal-artifacts.py --lane metal4 \
  --base "$METAL3_ARTIFACTS" --out xnu/out-fuxi-metal4 \
  --firmware "$FUXI_GMU_FIRMWARE" --sqe "$FUXI_SQE_FIRMWARE" --zap "$FUXI_GPU_ZAP_MBN"
python3 tools/test-fuxi-metal-stage.py
```

Packaging tests passed native service/metaclass and recovery symbols, exact
command line/ADT, boot-v4/Image/payload geometry and RAM bounds, allocated exact
pinned GMU/SQE/zap bytes in the final boot Image, signatures/minimum OS and
SHA256SUMS. Firmware stays external and ignored.

For the coordinator's later offline root edit, as root on an already mounted
writable iOS root:

```sh
python3 tools/stage-fuxi-metal.py "$IOS_ROOT" \
  tools/out/libvulkan_freedreno.dylib tools/out/FuxiMetal.bundle \
  tools/out/fuxi-metal-smoke --conf tools/out/metal-smoke.conf
```

The helper validates every source/destination before copying, refuses unsigned
or wrong-target artifacts and escaping symlinks, installs no launchd job, and
sets newly created directories to root:wheel 0755. Temporary-root tests passed
idempotence, file/directory modes/ownership and malformed input/symlink rejection.
Stage these files; all owners are **root:wheel (0:0)**:

| Local artifact | iOS destination | Mode |
| --- | --- | --- |
| `tools/out/libvulkan_freedreno.dylib` | `/usr/local/lib/libvulkan_freedreno.dylib` | 0755 |
| `tools/out/fuxi-metal-smoke` | `/usr/local/bin/fuxi-metal-smoke` | 0755 |
| `tools/out/metal-smoke.conf` | `/usr/local/share/fuxi/metal-smoke.conf` | 0644 |
| `tools/out/FuxiMetal.bundle/FuxiMetal` | `/System/Library/Extensions/FuxiMetal.bundle/FuxiMetal` | 0755 |
| `tools/out/FuxiMetal.bundle/Info.plist` | `/System/Library/Extensions/FuxiMetal.bundle/Info.plist` | 0644 |
| `tools/out/FuxiMetal.bundle/_CodeSignature/CodeResources` | `/System/Library/Extensions/FuxiMetal.bundle/_CodeSignature/CodeResources` | 0644 |

Boot artifact: `shim/out/fuxi-metal4-boot.img`. The following hashes describe
this build; complete manifests also live in the ignored output directories.

| M4 artifact | SHA256 |
| --- | --- |
| `xnu/out-fuxi-metal4/kernel.development.fuxi` | `8bfeae158e8250e906e3b3ffbcbddb8f5d63404eedd9807cf8e2838cf7a1b5d9` |
| `xnu/out-fuxi-metal4/kernel.development.fuxi.unstripped` | `858e6cfe0b636530f7f8d097f23ca84a5f7efd0516093b74c5aba90431d353a8` |
| `shim/out/fuxi-metal4-boot.img` | `e10f981a0bae32ad5c892d978a786eabe4cc21acda6e35ed064f1ad68a53c4b6` |
| `tools/out/libvulkan_freedreno.dylib` | `5661a054b5e4be57fe54bc38f3cccf6d264ae20562473caf72e50d2adc158a8c` |
| `tools/out/FuxiMetal.bundle/FuxiMetal` | `9535102237abd309c94978b3ab52a1435f124b4f022ab6bdfeab0b39f60dc603` |
| `tools/out/FuxiMetal.bundle/Info.plist` | `2578f53d73942a55e0163fa5c80c29beb00839bfb8db477a4060e0a81ab5fd06` |
| `tools/out/FuxiMetal.bundle/_CodeSignature/CodeResources` | `26aaa98e843bdbffb42ecaefb507ff685bdf749a45ceb1d0c68a0c15d58e903c` |
| `tools/out/fuxi-metal-smoke` | `64e747d6a7079fee0a675b2f757752c1569b20c19484259d5122fcc63467aec7` |
| `tools/out/metal-smoke.conf` | `1121cfccd5913f0a63fec40a6ffd44ea64f9dc135c66634ba001d10bcf4302a2` |


M6 still needs IOSurface/texture import, CAMetalLayer drawables, producer/consumer
fences and lifetime/coherency, pixel format/stride/orientation, and the render
and compute paths actually used by CoreAnimation. M4's discovery and buffer
transfers do not supply any of those. The existing kernel CPU-present fallback
is unchanged; accelerated app/CA correctness, latency, thermal and memory
measurements remain acceptance gates after M5 translation/licensing.

---

# Metal lane M3: staged Turnip submission smoke

2026-10-20. **M2 observed on metal2h**, reported by the coordinator: direct ICD
load, instance creation, one Qualcomm physical device (`0x5143` / `0x43050a01`,
Turnip Adreno 740, pinned Mesa 26.2.0-devel), 1.5 GiB device-local heap,
logical device/queue creation and destruction all passed. Three internal BOs
were allocated, GPU-mapped, CPU-mapped/unmapped and freed before close. No panic
and no managed-page physical-descriptor warning occurred. This confirms the
metal2h mapping fix and the M2 lifetime gate; no GPU work was submitted.

**M3 observed PASS on metal3 (`5fd2350`), coordinator report 2026-10-20:**
root smoke exec-swapped into peakpowermanagerd completed setup, fill (16384
words, fence 1.2 ms), compute (16384 words), triangle (six probes, red 8192 /
black 57344), 16 fill reuse rounds, device/instance destruction and reopen,
then fill/compute again, ending `done PASS`. No hang, SMMU fault or panic was
reported. This qualifies the M3 Turnip workload, not Metal or CoreAnimation.

**M3 build work:** No phone access, root-image
edit or push was performed. The new kernel changes only add a read-only,
once-per-loss register snapshot before existing isolation. The native tree
also includes the inherited WLAN RC SID initialization fix at this branch tip;
`fuxiwlan` remains absent/off. No speculative CP initialization change is made.

`tools/fuxi-vk-smoke.cpp` ignores every argv value, including the executable
name, because exec-swap supplies the host daemon's argv. It reads a maximum
stage **1..4** from `/usr/local/share/fuxi/vk-smoke.conf`; absent config defaults
to **4**, malformed/unreadable config fails before loading the ICD. The staged
config contains `4` and a newline. Stages run as a prefix, stop at the first
failure and print unbuffered stdout:

| Stage | Work and acceptance |
| --- | --- |
| 1 `fill` | Recreated 64 KiB host-visible buffer; `vkCmdFillBuffer`, transfer-to-host barrier, submit/fence, invalidate mapped range, compare all 16384 words against `0xa740cafe`. |
| 2 `compute` | 256 workgroups of 64 invocations; storage buffer receives `(i * 1664525 + 1013904223) ^ 0xa740cafe` with uint32 arithmetic. Compare all 16384 words. |
| 3 `triangle` | Offscreen 256x256 RGBA8 UNORM render pass, opaque red triangle on opaque black, transfer to host buffer. Validate all pixels are red/black, six known inside/outside samples, and red count 8192 +/-128. |
| 4 `reuse-reopen` | Repeat fill 16 times with destruction/recreation and varying patterns; destroy device **and instance**, create both again in the same process (closing/reopening the global Darwin connection), then repeat fill and compute. |

The three GLSL files live in `tools/shaders/`. `glslangValidator -V
--target-env vulkan1.1` generates `tools/out/vk-smoke-spv.h` at build time; C
arrays are embedded in the signed executable. No runtime shader compiler or
shader sidecar is staged. Each submit prints BEGIN and fence rc/elapsed-ms.
The smoke forces `TU_DEBUG=nocb`, direct service/open type 0, and
`MESA_LOG_LEVEL=debug` **before dlopen**. No sysmem/gmem/flushall/syncdraw override
changes the submission path. The ICD overlay logs accepted IB count and seqno.

Fence waits request **2 s**. On timeout the tool prints FAIL and elapsed time,
then attempts `vkDeviceWaitIdle` once under a **1 s SIGALRM wall bound**; either
return or alarm exits nonzero without driver teardown. Setup/stages/teardown
also have 30/30/10 s wall bounds (60 s for stage 4), covering a stuck submit or
cleanup. Other failures exit immediately and leave connection cleanup to
process death and the independent kernel watchdog. These software bounds do
not make wedged MMIO/interconnect access recoverable.

Rebuild with the existing M2 external pins/cache and the same
`tools/build-fuxi-turnip.py` command below. It now builds/signs both diagnostics,
applies `tools/patches/mesa-fuxi-smoke.patch` **after** the iOS16 overlay, and
checks the exact patched tree, including an already patched M3 cache. The M3
overlay SHA256 is
`6a2f66e50170887d26fb30a3ed67c1aabe3a5a49d4fdfbc6de0f17b1c77f2c13`.
The historical M2 artifact hashes below describe the observed no-submit build.

Native XNU was rebuilt in the assigned `ci-lane-metal2` tree with all 32 native
inputs checked against the branch/canonical sources and unchanged base inputs.
Use the standard native build/IIG setup, copy stripped/unstripped kernels to
`xnu/out-fuxi-metal3/`, then pack/check:

```sh
export FUXI_GMU_FIRMWARE FUXI_SQE_FIRMWARE FUXI_GPU_ZAP_MBN MKBOOTIMG
# Every variable above must name an existing absolute external file.
bash tools/build-fuxi-metal3-images.sh "$METAL2H_ARTIFACTS"
python3 tools/test-fuxi-metal-artifacts.py --lane metal3 \
  --base "$METAL2H_ARTIFACTS" --out xnu/out-fuxi-metal3 \
  --firmware "$FUXI_GMU_FIRMWARE" --sqe "$FUXI_SQE_FIRMWARE" --zap "$FUXI_GPU_ZAP_MBN"
```

The ADT is byte-identical to metal2h. The command line changes only the
exec-swap target to
`sweetexecswap=peakpowermanagerd:/usr/local/bin/fuxi-vk-smoke` and adds
`sweetnull=backboardd,SpringBoard,fuxi-vk-smoke,peakpowermanagerd`.
Firmware remains external, hash-pinned and present in the boot Image.

For a later root edit, as the mounted-root editor:

```sh
python3 tools/stage-fuxi-vulkan.py "$IOS_ROOT" \
  tools/out/libvulkan_freedreno.dylib tools/out/fuxi-vk-probe \
  --smoke tools/out/fuxi-vk-smoke --conf tools/out/vk-smoke.conf
```

This retains the probe and stages the ICD and both binaries as **0:0 0755**,
and the config at `/usr/local/share/fuxi/vk-smoke.conf` as **0:0 0644**. No job
is installed. Boot `shim/out/fuxi-metal3-boot.img` through the coordinator's
usual temporary-boot workflow only after those files are staged.

Expected acceptance lines (not device observations):

```text
FUXI VK: stage 0 setup PASS instance/device/graphics+compute queue
FUXI VK: stage 1 fill PASS verified readback
FUXI VK: stage 2 compute PASS verified readback
FUXI VK: stage 3 triangle PASS verified readback
FUXI VK: stage 4 reuse-reopen PASS verified 16 recreations + device/instance reopen + fill/compute
FUXI VK: done PASS
```

## M3 submit/fence review and failure triage

Reviewed the pinned `tu_knl_darwin.cc`, Turnip's `tu_queue.cc`,
`tu_cmd_buffer.cc`, A6XX/A7XX XML registers, and this tree's ABI client,
`fuxi_abi_wire.h`, backend and ring bring-up. Findings:

- **Timeouts:** relative ABI nanoseconds, zero polls once, kernel client slices
  at 1 ms and caps any call at 1 s. Independent hardware ownership deadline is
  100 ms. Darwin waits use monotonic absolute deadlines and 1 ms ABI slices.
  The overlay caps the final slice to remaining time, saturates relative-to-
  absolute addition, and propagates device loss in wait-any instead of looping.
  A watchdog loss is reported as ABI Timeout, so an infinite Vulkan wait could
  keep polling it; this tool always uses finite waits and a process alarm.
- **Seqnos:** global uint32 monotonic fence with per-connection first/last
  bounds; kernel refuses wrap. KMD's uint32 fence storage matches this limit;
  a fresh connection resets KMD tracking but kernel seqnos continue. Queue 0
  exists at open; hardware accepts one outstanding job. Darwin's eight-entry
  inflight history is bookkeeping, not eight supported Fuxi hardware slots.
- **IBs/BOs:** 1..16 top-level IBs; each handle must be live and GPU-mapped,
  4-byte address/length alignment, nonempty, wholly inside that BO, at most
  `0xfffff` dwords (4194300 bytes). Queue/reserved/wait-fence fields and exact
  struct sizes are checked before submission. There is no separate residency
  list: all live mapped BOs are already in the client's root, including shader,
  descriptor and nested IB BOs. No PM4 opcode/nested-IB validation or security
  boundary is claimed. Freed mappings are invalidated before release; busy
  work prevents unmap. The KMD previously split >16 entries into separate
  submits, but this kernel resets context on each submit: the overlay rejects
  such lists **before any partial submission** with `Fuxi IB list N exceeds
  atomic submit limit 16`. It does not truncate or insert extra doorbells.
- **Caches:** KMD BoMap requests Normal NC (`writeback=0`, outer-shareable 2),
  matching CPU write-combine BO views. The kernel publishes CPU stores with
  `dsb sy`, flushes/invalidates GPU caches in the frame and switches TTBR only
  while idle with ASID invalidation/TLBSYNC. Completion uses BR/BV timestamps,
  idle/ring checks and restored root. The tool flushes initialized mappings,
  emits shader/transfer-to-host barriers, and invalidates before verification,
  including noncoherent memory. No cached-coherent or HostImport promise was
  added; actual shader/UCHE/CCU coherence remains a device gate.
- **Ring contract:** cold init programs SQE/ring/RPTR/BV RPTR bases, privileged
  access/protection, HBB/UBWC NC and UCHE GMEM/trap ranges. CP_ME_INIT omits the
  unavailable IFPC restore-list ordinal; GX stays held, IFPC/preemption are
  disabled, no preemption records or separate BV/LPAC ring are invented. The
  per-submit frame synchronizes BR, resets BOTH threads/context, waits for
  PERFCTR SRAM initialization, invalidates caches, runs IBs and retires BV then
  BR with idle. Turnip marks `has_preemption=false`; its command buffers emit
  static register initialization, CP_SET_DRAW_STATE, CP_SET_MARKER render modes,
  BR/BV offsets/ambles/thread controls and CCU/GMEM configuration themselves.
  Thus missing marker/draw state or preemption records cannot be asserted as
  the cause of UniXNU's reported CP hang. The later M3 run above qualified this
  subset for its tested Turnip workload; raw CP_MEM_WRITE/BLIT alone was weaker
  evidence. No speculative framing change was justified by the code review.

Historical pre-run hypotheses (the reported M3 run subsequently passed):

1. **CP/thread/context initialization mismatch.** Stage-1 accepted submit then
   no retirement; `CP_RB_RPTR/WPTR`, `CP_IB1_BASE_*`, `CP_IB1_REM_SIZE`,
   `CP_STATUS_1`, `CP_HW_FAULT`, `CP_INTERRUPT_STATUS`, `CP_PROTECT_STATUS` and
   `RBBM_STATUS/STATUS3` distinguish fetch/protection/error evidence from a
   busy BR/BV wait. An unchanged IB remainder with no faults suggests a stalled
   packet/wait, not proof of which packet. Preserve all snapshot lines.
2. **Client SMMU/nested resource mapping.** `SMMU GPU CB` logs TTBR, FSR, FAR,
   FSYNR0/1 and GFSR. Nonzero fault plus FAR into an IB/shader/descriptor range
   points to translation/access or lifetime; clean FSR alone does not prove
   all mappings or cache attributes correct.
3. **Cache visibility/flush ordering.** Fence rc 0 followed by `fill ... word`
   or `compute word ... got ... want ...` fails data rather than retirement.
   Failures first appearing in stage 4 implicate reuse/alias/lifetime/TLBI.
4. **Shader or graphics state/GMEM/UBWC setup.** Fill PASS then compute FAIL
   narrows to shader/SP/descriptor execution; compute PASS then triangle FAIL
   narrows to graphics/CCU/image copy. `triangle pixel`, `triangle sample` or
   `triangle count` distinguishes invalid bytes, geometry and coverage. Kernel
   GMEM range initialization is not a complete render-pipeline setup.
5. **ABI/quota/IB limits before CP.** Tool API `rc`, KMD `submit failed: 0x...`
   or explicit atomic IB-limit error, with no accepted `FUXI KMD: submit IBs N
   fence F`, distinguishes rejected work. BO limit is 64 and live wired quota
   256 MiB despite the advertised 1.5 GiB heap. On reopen failure, check close,
   open and `stage 4 reuse-reopen` setup failure before blaming the shader.

On a pending fault/deadline the kernel prints **16 bounded `FUXI GPU HANG:`
lines once per boot-wide loss, before reset**, including all registers above
and MMIO-access-failed state. The independent 2 ms watchdog observes the 100 ms
job deadline even without WaitFence calls. It permanently disables submission,
drains GBIF (bounded polling), holds reset and faults SID0/1; `GBIF drain ACK`
permits release, `GBIF drain FAILED ... source quarantined until reset` retains
DMA backing. CPU presentation remains the existing fallback; no CP restart or
recovery is attempted. Timing depends on scheduler/MMIO progress.

Validation: real smoke commands/readbacks on the software ICD under ASan/UBSan
passed all four stages, exact 8192/57344 pixel counts and complete reopen.
Injected final-word corruption, device loss, timeout, hung idle, malformed
config and host argv passed stop/bound checks. Extracted actual Darwin KMD
functions passed finite/poll/overflow waits, wait-any loss propagation, 17-IB
rejection before submit, 16-IB and empty-submit fence tests. Existing no-submit
probe, ABI/backend/framing/patch-chain, exec-swap, NC/cache/mapping/lifetime,
present quarantine, memory and native SCM checks passed. Mach-O audit/signature
verification passed (plain arm64, iOS 16 minimum, system dependencies, no
CTR_EL0 reads); the eight additional strong imports were found in the supplied
iOS 16 cache. These checks do not qualify Turnip GPU execution or Metal.

| M3 artifact | SHA256 |
| --- | --- |
| `xnu/out-fuxi-metal3/kernel.development.fuxi` | `194e4222564b1b27152ca43b992d39ca7e38bc8f7bfc118748cadf300375a9e0` |
| `xnu/out-fuxi-metal3/kernel.development.fuxi.unstripped` | `6efaf3446b989820b056cb96ca1214bde44165d2975d399d94e548359db45482` |
| `shim/out/fuxi-metal3-boot.img` | `61240f263a112d6f5d2bcd51aa959ccfafaa0c8dae59a8b8374d2b5c778fe2b5` |
| `tools/out/libvulkan_freedreno.dylib` | `5661a054b5e4be57fe54bc38f3cccf6d264ae20562473caf72e50d2adc158a8c` |
| `tools/out/fuxi-vk-smoke` | `1d7fda3c9cd94827a18b77a4111a058eb75644535c66623cc1498c1dab0edc34` |
| `tools/out/vk-smoke.conf` | `7de1555df0c2700329e815b93b32c571c3ea54dc967b89e81ab73b9972b72d1d` |

---

# Metal lane M2 / metal2h: managed BO CPU mapping cleanup

2026-10-20. **M1 qualified on device**, as reported by the coordinator for
metal2g (`5c0b2da`): root pid 78, hosted by peakpowermanagerd, completed
open/query/device-info/alloc/map/cpu-token/cpu-map/submit/wait with rc 0,
`mem 0xa740cafe bad-pixels 0 rounds 1/2`, close/reopen, the same result for
`rounds 2/2`, then free/close with no panic. This is observed raw PM4 success,
not Turnip/shader/Metal qualification. One nonfatal managed-page physical
IOMemoryDescriptor warning occurred in cpu-map.

**metal2h change:** `FuxiAdrenoBoView` now derives from
`IOSubMemoryDescriptor`, using the original IOBufferMemoryDescriptor as its
parent for the entire allocated BO. It no longer builds physical-address
ranges over managed RAM. `clientMemoryForType` still returns the same retained
view for map/unmap, with Normal NC/write-combine flags. The view keeps the
matched prepare and buffer reference until its final mapping is destroyed;
failed DMA drain still quarantines all references. Reserved carve-out NC
mappings and pmap cache synchronization are unchanged.

**Validation:** the real view/bind/cpu-map/drop methods run under ASan/UBSan,
covering retained CPU maps after BO close, same-descriptor unmap identity,
missing/expired tokens, failed bind and failed drain. ABI/backend, patch-chain,
execswap/quota, cache-sync/NC range tests pass. Native build succeeded in the
assigned `ci-lane-metal2` tree. Packaging checks verify metal2g's byte-identical
ADT and command line, external pinned GMU/SQE/zap bytes, recovery entries,
service symbols and artifact hashes. No phone was accessed. Warning removal and the M2 device lifetime gate were subsequently observed on
metal2h; see the M3 report above.

Build with the same native XNU/IIG setup as metal2g, copy stripped/unstripped
outputs into `xnu/out-fuxi-metal2h/`, then:

```sh
# All firmware/tool variables must name existing absolute external files.
export FUXI_GMU_FIRMWARE FUXI_SQE_FIRMWARE FUXI_GPU_ZAP_MBN MKBOOTIMG
bash tools/build-fuxi-metal2-images.sh "$METAL2G_ARTIFACTS" metal2h
python3 tools/test-fuxi-metal2h.py
python3 tools/test-fuxi-metal-artifacts.py --lane metal2h \
  --base "$METAL2G_ARTIFACTS" --out xnu/out-fuxi-metal2h \
  --firmware "$FUXI_GMU_FIRMWARE" --sqe "$FUXI_SQE_FIRMWARE" --zap "$FUXI_GPU_ZAP_MBN"
```

| Artifact | SHA256 |
| --- | --- |
| `xnu/out-fuxi-metal2h/kernel.development.fuxi` | `e375ac6962acd8856f5d00219ca2e999b43734867ebc6786f8197e753fd5c848` |
| `xnu/out-fuxi-metal2h/kernel.development.fuxi.unstripped` | `75d52a0c6f876f2f957bfb0b67d4098aba7fa486d2a76b3a9f86ced4e4f0e785` |
| `shim/out/fuxi-metal2h-boot.img` | `3212f853c3787b2c128162d7d9cbb5ab3ee88c73f9a9e950b06c3184567510b0` |

metal2h intentionally still exec-swaps the raw ABI test. Expect the same two
successful rounds as metal2g, without `IOMemoryDescriptor physical with managed
page`. The M2 Vulkan probe needs a separate coordinator-selected invocation.

## M2: iOS 16 Turnip cross-build (offline complete)

**M2 lifetime gate subsequently observed on metal2h** (coordinator report; see
M3 above). `tools/build-fuxi-turnip.py` builds/signs the real
Turnip ICD against this tree's `xnu/fuxi-adreno/NativeAdrenoABI.h`, then builds
`tools/fuxi-vk-probe.cpp` using Mesa's Vulkan headers. The probe directly dlopens
the ICD, creates an instance, enumerates physical properties/heaps, creates one
logical device/queue, and destroys them. It never resolves or calls a submit
entrypoint, allocates application GPU resources, creates a surface, or presents.
Source review of the pinned CreateDevice/queue-init path found BO/command-stream
initialization but no hardware submission. The M2 report above qualifies enumeration/device lifetime only.

### Exact inputs and patch order

The following records the historical M2 build. For the current nine-patch
baseline, retained overlays and release profile, use [Turnip T1](turnip-t1.md).

- Mesa commit `85fda3b36405bcaea8bffc64aa4f2651dc90770d`, base tree
  `b72ff1eb2138026ed6aba524770899faabf7d7c8` (Mesa `26.2.0-devel`).
- UniXNU commit `7b52e7968c76453cdc18b5aa174fc368d5293b91`,
  [build recipe](https://github.com/UniXNU/UniXNU/blob/7b52e7968c76453cdc18b5aa174fc368d5293b91/scripts/build-turnip-darwin.py)
  and its `scripts/data/adreno-compiler-inputs.json`. Keep its MIT notices in
  the external patched source. No upstream checkout or large patch is vendored.
- NativeAdrenoABI.h SHA256
  `c37a965ce521f57bd201b61ee0578d72b809b3d1c3697fd572ee1f3753af2e89`.
  Direct unsandboxed root service: `NativeAdrenoGpuPublished`, open type **0**;
  ABI v1, 31-bit IOVA / 36-bit PA, 256 MiB per-client wired limit, no HostImport.

Apply the following in order (the build script verifies upstream patch hashes,
then checks the entire patched worktree against an independently built Git index):

| Patch | SHA256 |
| --- | --- |
| `freedreno-tools-nir.patch` | `77e966b6e9a725892d10889a4d8deeefb03a32ae3a007904868132e56350191f` |
| `ir3-darwin-build-id.patch` | `be3ca7840a16cb7696d8d7e67195c8d097cf0e38a3b5011edc674a5aed9c6975` |
| `ir3-portable-asm-roundtrip.patch` | `bfc5552bc2bd6e1668d75773cce7bf62fd19065e6281bf7da49f9ddfc2e79339` |
| `offline-adreno-compiler-probe.patch` | `b7d7a113beb5ef99edea24858d906194d4b4755c987520545ad2f7f3136bb4fb` |
| `turnip-darwin-kmd.patch` | `dcf6a88890995243516f5fd06a41b4ff91117f25ca08e7dc4fc7b90836b11e37` |
| `tools/patches/mesa-fuxi-ios16.patch` (local, last) | `e7879df64e66442c015ca25f6484bad6ba57b56bed9bb10ac357aa46e1b40306` |

The local patch adds the missing `tu_physical_device::has_host_import` boolean:
the upstream patch references/sets it but omits its declaration. This is a
reproduced compile failure, independent of target OS. It stays false because our
Query flags are zero. No HostImport support is added.

**iOS 16 derivation:** C/C++/ObjC compile and link flags all use
`-target arm64-apple-ios16.0`, the real iPhoneOS SDK and
`-Werror=unguarded-availability-new`. No macOS SDK fallback. The upstream Darwin
condition-variable workaround and 64-byte cache-line substitution apply to iOS
16 as well. None of the iOS 17 IOGPU/Metal object-size assumptions are used.
`platforms=macos` selects Mesa's Darwin/Metal WSI sources; it does not change the
Mach-O platform to macOS. The probe enables no WSI extensions. The WSI path that
calls `MTLCreateSystemDefaultDevice` is reached when creating a swapchain, which
this probe never does; this does not provide a Metal frontend or presentation.

### Build procedure

Observed toolchain: Apple clang `21.0.0 (clang-2100.3.34.2)`, iPhoneOS SDK
**27.1**, Meson **1.12.1**, Ninja **1.13.2**, native glslangValidator **16.6.0**,
Python **3.14.8**, Bison **3.8.2**. The installed newer SDK was used with a 16.0
minimum; required imports were also checked against the supplied iOS 16.7 cache.
Apple Bison 2.3 fails IR3 parser generation (`-Wcounterexamples` unsupported).
The pinned GNU tarball SHA256 is
`9bba0214ccf7f1079c5d59210045227bcf619519840ebfa80cd3849cff5a5bf2`.

Use absolute environment paths for an external/ignored cache, checkouts and
build directory. Only the assigned Mac lane and its own dependency cache were
written. Example setup, with native Ninja/glslang already on PATH:

```sh
mkdir -p "$CACHE"
python3 -m venv "$CACHE/venv"
"$CACHE/venv/bin/pip" install meson==1.12.1 mako==1.4.3 MarkupSafe==3.0.4 \
  pyyaml==6.0.3 packaging==26.3
curl -fL https://ftp.gnu.org/gnu/bison/bison-3.8.2.tar.xz -o "$CACHE/bison-3.8.2.tar.xz"
# Verify the SHA256 above before unpacking/building.
tar -C "$CACHE" -xf "$CACHE/bison-3.8.2.tar.xz"
(cd "$CACHE/bison-3.8.2" && ./configure --prefix="$CACHE/native" && make -j6 && make install)
export PATH="$CACHE/native/bin:$CACHE/venv/bin:$PATH"

git init "$MESA_SRC"
git -C "$MESA_SRC" remote add origin https://chromium.googlesource.com/external/gitlab.freedesktop.org/mesa/mesa
git -C "$MESA_SRC" fetch --depth 1 origin 85fda3b36405bcaea8bffc64aa4f2651dc90770d
git -C "$MESA_SRC" checkout --detach FETCH_HEAD
git init "$UNIXNU_SRC"
git -C "$UNIXNU_SRC" remote add origin https://github.com/UniXNU/UniXNU.git
git -C "$UNIXNU_SRC" fetch --depth 1 origin 7b52e7968c76453cdc18b5aa174fc368d5293b91
git -C "$UNIXNU_SRC" checkout --detach FETCH_HEAD
python3 tools/build-fuxi-turnip.py --mesa "$MESA_SRC" \
  --patches "$UNIXNU_SRC/patches/mesa" --build "$MESA_BUILD"
```

The script uses the manifest's options: `vulkan-drivers=freedreno`,
`freedreno-kmds=darwin`, `tu-darwin-abi-dir` set to our ABI directory,
`platforms=macos`; Gallium, GL/GLX/GLES/EGL/GBM, LLVM, shared-glapi, XML config,
shader cache, tools/tests/codecs/layers, unwind/sensors/valgrind/expat/zstd are
disabled; `--wrap-mode=nodownload`, native glslang/build generators. It writes a
cross file to the ignored build directory. If resuming a cache configured with
the wrong native Bison, use a fresh build directory. `--verify-inputs-only`
checks source/patch state without configuring or rebuilding.

The ICD install ID becomes `/usr/local/lib/libvulkan_freedreno.dylib` before
`codesign -f -s -`, exactly the raw test's ad-hoc signing method, with no added
entitlements. `codesign --verify --strict` passes for both outputs.

### Artifact/load-command audit

| Artifact | SHA256 |
| --- | --- |
| `tools/out/libvulkan_freedreno.dylib` | `fe2bb259cb173762aa902754f5ec0ee9b567fb5ce97351d9a8c2ba32339c2928` |
| `tools/out/fuxi-vk-probe` | `ca7851eb7f0051ccb57927971d67570c34b4b2f07695f8209c8bbfd564124a52` |

Both are thin plain arm64 (not arm64e), `LC_BUILD_VERSION platform IOS`,
`minos 16.0`, `sdk 27.1`, embedded ad-hoc signatures, no LC_RPATH. The ICD exports
`vk_icdGetInstanceProcAddr` (also `vkGetInstanceProcAddr`). Instruction-word scans
of every executable section and independent LLVM disassembly found **no
`MRS CTR_EL0`**. The upstream Darwin helper substitutes 64 bytes.

Exact ICD `LC_LOAD_DYLIB` paths:

```text
/usr/lib/libSystem.B.dylib
/usr/lib/libz.1.dylib
/System/Library/Frameworks/IOKit.framework/Versions/A/IOKit
/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics
/System/Library/Frameworks/QuartzCore.framework/QuartzCore
/System/Library/Frameworks/Metal.framework/Metal
/usr/lib/libc++.1.dylib
/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation
/usr/lib/libobjc.A.dylib
```

The probe loads only libc++ and libSystem. These are iOS 16 system libraries;
the IOKit versioned path is present in the supplied cache. All **262 distinct
strong imports** (251 ICD, 29 probe, with overlap) were found defined in their
expected system providers/reexports in the supplied 20H392 cache (hash recorded
in the discovery section below). No required import is a dynamic lookup. The
ICD has **5571 weak dynamic references** from generated optional Turnip/WSI
entrypoint tables. The upstream `-undefined dynamic_lookup` is retained; ld
warns that it is deprecated on iOS, plus a duplicate `-lz` warning. These weak
references outside the exercised M2 path remain unqualified; the root
load/device-lifetime gate subsequently passed on metal2h.

Reproduce audit on the Mac, then use the supplied cache offline:

```sh
python3 tools/audit-fuxi-vulkan.py tools/out/libvulkan_freedreno.dylib tools/out/fuxi-vk-probe
"$IPSW" dyld symaddr "$DSC" --in tools/out/M2-required-symbols.json \
  --output tools/out/M2-ios16-symbols.json
python3 tools/audit-fuxi-vulkan.py --cache-symbols tools/out/M2-ios16-symbols.json \
  --required tools/out/M2-required-symbol-libs.json
python3 tools/test-fuxi-vulkan.py --headers "$MESA_SRC/include"
```

Host tests run the actual probe against a fake ICD that rejects unexpected
entrypoint lookups, including submit calls; they cover successful lifetime,
cleanup after enumeration/device-creation failure, exec-swap host argv, and
idempotent stage modes/ownership. They do not execute Turnip or an iOS dyld.

### Later root edit and device acceptance

Run `tools/stage-fuxi-vulkan.py ROOT tools/out/libvulkan_freedreno.dylib
tools/out/fuxi-vk-probe` as the root-image editor, against an already mounted
writable iOS root. It copies only:

| Root path | Owner | Mode |
| --- | --- | --- |
| `/usr/local/lib/libvulkan_freedreno.dylib` | `0:0` (root:wheel on iOS) | `0755` |
| `/usr/local/bin/fuxi-vk-probe` | `0:0` | `0755` |

No launchd job is installed and no root was edited in this lane. No Vulkan
loader or ICD JSON is required because the probe dlopens the ICD directly.
Invoke once as unsandboxed root, after the raw test has closed its exclusive
connection. The probe forces `TU_DARWIN_SERVICE=NativeAdrenoGpuPublished` and
`TU_DARWIN_OPEN_TYPE=0`. Direct invocation optionally accepts an absolute ICD
path; an exec-swap host basename causes all host arguments to be ignored.
metal2h's unchanged command line still selects the raw test; a coordinator must
choose a separate root invocation or a later probe-specific exec-swap image.

M2 acceptance output (the coordinator subsequently reported this lifetime gate
passing on metal2h; no submission was part of that run) includes:

```text
FUXI VK: create-instance rc 0
FUXI VK: physical-device-count rc 0
FUXI VK: physical-devices rc 0
FUXI VK: physical-devices 1
FUXI VK: device 0 vendor 0x5143 device 0x43050a01 name Turnip Adreno (TM) 740... driver turnip Mesa driver (Mesa 26.2.0-devel...)
FUXI VK: heap 0 bytes N flags 0x1
FUXI VK: create-device rc 0
FUXI VK: queue family 0 index 0 PASS
FUXI VK: destroy-device PASS; no work submitted
FUXI VK: done PASS
```

`N` is queried, not hardcoded: this pin uses a RAM heuristic capped by the
`0x60000000` (1.5 GiB) IOVA window. This is larger than our 256 MiB wired quota;
it must not be interpreted as a tested allocation budget. The device name may
include `(raytracing disabled)`; no features/extensions are enabled by the probe.
Expected kernel calls include open/query/device-info/close for enumeration,
reopen/query/device-info, internal BO alloc/map/cpu-map/free and close for device
lifetime. **No Submit/WaitFence calls or GPU results are expected.**

Remaining risks after the observed M2 gate: weak optional entrypoints outside
the exercised path, advertised heap vs
kernel quota, single-client/global backend state, and existing A7xx
cache/TTBR/submit assumptions. The no-submit gate passed without panic; M3 separately requires observed
fill/compute/triangle/readback. No shader submission,
Metal discovery/frontend, IOSurface import or CoreAnimation integration is
qualified by this build.

---

# Metal lane, milestone 2g: full-width cache sync and bounded NC access

2026-10-20, based on metal2e and Wi-Fi M6. No device execution in this lane.

**Observed metal2f:** exec-swap as peakpowermanagerd reached open, query and
DeviceInfo successfully, then panicked in `phystokv_range` with PA
`0xc4f40000`, below the managed RAM base `0x8c0000000`. No backtrace was
available. IOMFB_FDR_Loader previously passed `--launchd_boot`, which made the
old test exit with usage before opening the GPU.

**Root cause identified in the BO allocation cache path:**
`FuxiAdrenoUserClient::allocate` requests `kIOMapWriteCombineCache` from
`IOBufferMemoryDescriptor`. Changing wired pages from copyback to NC runs
`pmap_sync_wimg` -> `pmap_sync_page_attributes_phys` -> `flush_dcache` ->
`phystokv_range`. Upstream `osfmk/arm/pmap/pmap.c:6884` shifts the 32-bit
`ppnum_t` before casting: `(vm_offset_t)(pp << PAGE_SHIFT)`. The metal2f
kernel actually emits `lsl w0, w0, #14`. For example, managed PA
`0x8c4f40000` becomes the exact reported illegal PA `0xc4f40000`. The log
alone does not identify which original high RAM page was used; it does not
establish that the reported low address was a real carve-out allocation.

Patch 0101 widens before shifting. The final native kernel emits
`ubfiz x19, x0, #14, #32`, preserving all 32 PFN bits. For reserved pages it
looks up the entire page in the published GPU kernel_map NC windows and
flushes through that VA, with `phys=FALSE`. Unknown non-RAM/non-window PAs
are rejected before any physmap helper. Managed BO pages keep their full PA.

**Access audit:** the ABI backend, present mapper, user client and shared
GPU helpers contain no calls to physical zero/copy/read/write or physmap
conversion functions. The user client accesses BOs through their descriptor
KVAs. ABI/present `pmap_find_phys` calls translate wired source KVAs to PAs;
`pmap_enter_options_addr` installs the persistent NC mappings and
`pmap_remove` unwinds failed installation. These are not carve-out data
accesses through physmap. Table walks, zeroing, ring storage, firmware and
handoff reads now validate their full byte range through `G_PTR_RANGE` and
`fuxi_gpu_nc_pointer`, with overflow and window-edge checks. MMIO validates
the actual 4/8-byte width. There is no physmap fallback for NC windows.

The test ignores host arguments whenever argv[0]'s basename differs from
`fuxi-gpu-abi-test`. Only direct self invocation accepts `--watchdog`; host
arguments can never select the hanging IB. Direct invalid arguments still
return usage status 2.

**Validation:** ASan/UBSan tests execute the actual NC lookup, scratch zero,
patched pmap helper and argument parser. The original pmap helper fails the
strict physmap trap with the reported low PA. All mapped apertures, unknown
PAs, unpublished mappings, zero lengths, crossing ranges and overflow are
covered. Existing ABI, execswap, mapper, present, shared probe, memory, WLAN
M6 and final native SCM checks pass. Artifact checks verify firmware bytes,
geometry, recovery table, service symbols, signed iOS arm64 tool and hashes.

The Mac tree was synchronized to the current worktree, including replacing
0099 with Wi-Fi M6, before building. The kernel and ad-hoc signed test were
built on the Mac; the loader was packed with the existing host toolchain.
The metal2g pack lane requires absolute external firmware/tool paths and
copies metal2f's ADT and command line exactly: peakpowermanagerd exec-swap,
bluetoothd/fairplayd.H2 blocked, and no `fuxiwlan` (Wi-Fi off).

Artifacts: `xnu/out-fuxi-metal2g/`, `shim/out/fuxi-metal2g-boot.img`, and
`tools/out/fuxi-gpu-abi-test`. SHA256s:

- Kernel: `c476cba84af5e795d93e165c029df669b4127bd22d9de368b22f2a1c7942e217`
- Unstripped: `dc80e71de18e10753eff3598fdc3ca248dbf13bfd0491dcc68f50e43b7fab7a3`
- Boot: `aceb71bdf42b0741db826d636e6bbbbbe348181ff9497d77a3dcc91e76110556`
- Signed test: `1114d442a5918ca05794acec6b0a4472ec92f62a5a1357a7886840682f252cb7`

The rebuilt test still needs offline root restaging. Expected next device
lines (not observed here), after the existing open/query/device-info lines:

```text
SWEET EXECSWAP: peakpowermanagerd -> /usr/local/bin/fuxi-gpu-abi-test pid N
FUXI ABI: alloc pid N rc 0x00000000
FUXI ABI: map pid N rc 0x00000000
FUXI ABI: submit pid N rc 0x00000000
FUXI ABI: wait pid N rc 0x00000000
FUXI ABI: result pid N rc 0x00000000 mem 0xa740cafe bad-pixels 0 rounds 1/2
```

Then close/reopen, another successful alloc/map/submit/wait and result
`rounds 2/2`, followed by close. A later respawn should report that the test
already passed 2/2 rounds this boot. No physmap panic is expected.

---

# Metal lane, milestone 2c: page-granular UCHE readback

2026-10-20, branch `codex-metal2c`, based on `2800fd5` (metal2b plus Wi-Fi
M5). Native build, all eleven host test scripts, shim host renderer, native
SCM audit and artifact checks pass. No device execution is claimed.

**Observed (supplied metal2b boot log):** GMEM MAX reads `0x12ff000` after
writing inclusive end `0x12fffff`. The other snapshot values match. This
explains the named init failure before the NC/aperture/table setup.

The canonical backend and regenerated patch 0098 now compare UCHE address
low words (WRITE_THRU, TRAP, GMEM MIN/MAX) through mask `0xfffff000` on both
operands. Address high words and NC mode retain exact comparisons; HBB
retains its exact extracted-field check and SMMU OAS its minimum check.
Raw snapshots and failure messages still report the unmasked values.
No programmed register value or userspace ABI changes.

**GMEM:** reconstruct the inclusive end before computing size:
`(0x12ff000 | 0xfff) - 0x1000000 + 1 = 0x300000` (3 MiB).
[Linux v6.16 A740 catalog](https://github.com/torvalds/linux/blob/v6.16/drivers/gpu/drm/msm/adreno/a6xx_catalog.c#L1370-L1376)
lists chip `0x43050a01` with `3 * SZ_1M`;
[Linux UCHE setup](https://github.com/torvalds/linux/blob/v6.16/drivers/gpu/drm/msm/adreno/a6xx_gpu.c)
programs GMEM MAX as MIN + size - 1, with a 16 MiB base for A740.
DeviceInfo already advertises this base and 3 MiB size; it remains unchanged.
Query reports ABI/address/allocation limits, not GMEM size. Host tests now
compile and execute the actual Query/DeviceInfo branches and check these
values against the observed range, avoiding the 4095-byte underestimate.

**Follow-on audit:** `g3_gw`/`g3_write`/`probe_safe_wr32` and `fg_access`
report MMIO access faults, not exact value readback. Thus the eight NC and
BR/BV/default aperture writes have no similar equality assumption to fix.
They still fail closed with a named write failure. `g_zero`, root/L2/L3
construction and fence initialization use ordinary reserved NC memory
stores, followed by `dsb sy`, rather than hardware address registers.
Later TTBR switches do compare the 4 KiB-aligned root PA plus ASID exactly;
there is no inclusive-end offset to reconstruct or discard there. The
existing SMMU setup uses the same aligned addressing. Explicit BR/BV aperture
selection, ABI table use and userspace submissions still need device testing.

Host tests use `0x12ff000` by default, accept both rounded and inclusive MAX
models, exercise each of the 12 ignored offset bits in all four low words,
and reject page-address/high-word/HBB/NC corruption. All prior read faults,
eight write failures, publication/table refusal, one-time snapshot bounds,
PM4, mapping, watchdog, execswap, governor, root-tool and Wi-Fi M5 tests pass.

**Build:** only `ci-lane-metal2` was used. Its prior 32 native input hashes
were verified before synchronization. All tracked repository files were
synchronized; the native source overlay includes the current 0099 M5 source
and this backend change. The final 32-input manifest differs from metal2b
only in those two files; all hashes pass on the Mac. The packed command line
and ADT are byte-identical to metal2b, including the absent `fuxiwlan` gate
(Wi-Fi off). The existing signed raw tool and pinned loader inputs are
unchanged. Kernel linking has the existing toolchain/linker warnings; loader
packing completes without warnings.

Repack and verify using the same private build environment:

```sh
bash tools/build-fuxi-metal2-images.sh "$METAL2B_ARTIFACTS" metal2c
python3 tools/test-fuxi-metal-artifacts.py --lane metal2c \
  --base "$METAL2B_ARTIFACTS" --out xnu/out-fuxi-metal2c
```

Outputs, manifests and logs: `xnu/out-fuxi-metal2c/`.
Image: `shim/out/fuxi-metal2c-boot.img`, SHA256
`eca58669dd03844dbeee53d6d67e8ae54b5544c8e95a359d489b3f317129cc91`.
The complete artifact manifest is `xnu/out-fuxi-metal2c/SHA256SUMS`.

**Expected next device evidence, not an observed result:** retain the eleven
raw init lines, including `UCHE_GMEM_RANGE_MAX_LO [0xe0d]=0x12ff000`, with no
MAX mismatch. If subsequent setup succeeds:

```text
FUXI GPU ABI: NativeAdrenoGpuPublished v1
FUXI ABI: open pid N rc 0x00000000
FUXI ABI: query pid N rc 0x00000000
FUXI ABI: device-info pid N rc 0x00000000
FUXI ABI: submit pid N rc 0x00000000
FUXI ABI: wait pid N rc 0x00000000
FUXI ABI: result pid N rc 0x00000000 mem 0xa740cafe bad-pixels 0 rounds 1/2
FUXI ABI: result pid N rc 0x00000000 mem 0xa740cafe bad-pixels 0 rounds 2/2
```

Allocation/mapping/cleanup lines occur between these markers as in 2a. The
second round follows close/reopen; later diagnostic opens are refused after
two successful rounds. A named `init write ... failed`, service withheld,
nonzero rc, bad pixels, watchdog or missing round remains failure evidence.

---

# Metal lane, milestone 2b: identify ABI init failures

2026-10-20, branch `codex-metal2b`. Built in the existing `ci-lane-metal2`
native tree. All ten host test scripts, the shim host renderer and artifact
checks pass. This lane did not access a phone or boot this image.

**Observed:** the supplied `ios-0098-metal2a/boot.log` reaches GPU stage 16
PASS (including zap and clear/copy), then prints ready and
`FUXI GPU ABI: initialization failed, service withheld`. Execswap runs the
raw test as pid 84 and again as pid 241. There are no ABI open/query lines.
The old compound check does not record its failing operand. The root cause
of that refusal remains unresolved; no register-value correction is claimed.

**Source audit:** `g3_hw_init()` runs before CP startup in both ABI and normal
boots. It writes the following values using dword-indexed GPU offsets and
separate 32-bit low/high stores. `g3_sr()` instead takes a byte offset.

| Register | Offset(s) | Programmed / required value |
| --- | --- | --- |
| UCHE_WRITE_THRU_BASE | `0xe07/0xe08` | `0xfffff000 / 0x1ffff` |
| UCHE_TRAP_BASE | `0xe09/0xe0a` | `0xfffff000 / 0x1ffff` |
| UCHE_GMEM_RANGE_MIN | `0xe0b/0xe0c` | `0x1000000 / 0` |
| UCHE_GMEM_RANGE_MAX | `0xe0d/0xe0e` | `0x12fffff / 0` (3 MiB, inclusive end) |
| UCHE_MODE_CNTL | `0xe01` | `0x600000`; bits 22:21 = 3, HBB = 13+3 = 16 |
| RBBM_NC_MODE_CNTL | `0x534` | `1` (macrotile mode; HBB is in UCHE) |
| SMMU_ID2 | byte `0x28` | earlier observed `0x5511`, OAS code 1 = 36 bits |

The supplied Linux `a6xx_gpu.c` and `a6xx.xml` agree. Qualcomm's
[adreno_gen7.c](https://github.com/qualcomm-linux/kgsl/blob/2b2d762c0b843c560d2ae2203f6b3cfe1474fb3f/adreno_gen7.c),
[register header](https://github.com/qualcomm-linux/kgsl/blob/2b2d762c0b843c560d2ae2203f6b3cfe1474fb3f/gen7_reg.h)
and [Gen7.2 catalog](https://github.com/qualcomm-linux/kgsl/blob/2b2d762c0b843c560d2ae2203f6b3cfe1474fb3f/adreno-gpulist.h)
also agree on the offsets, trap/write-through address, 16 MiB GMEM alignment,
3 MiB size and HBB16. Reference copies remain in ignored `out-fuxi-metal2b/refs`.
The old ABI verified WRITE_THRU, despite the DeviceInfo text discussing TRAP;
both are programmed identically. The new snapshot/checks include both pairs.
No source evidence establishes a read-as-zero mask for these registers, so
2b retains exact comparisons rather than guessing a mask or rewriting state.

`gpu_probe(16)` reaches `g5_run()`, which exits secure mode and explicitly
runs `gpu_m4(14)`. That clear/copy path already writes RB/TPL1/SP/GRAS NC
values `0x816/6/0x26/0x60`, even with the ABI enabled. No later software reset
or write to the checked UCHE registers was found between `g3_hw_init()` and
`fg_abi_init()`. `g3_hold_gx()` retains GMU OOB until reset, with no idle
collapse. Thus missing stage14 setup or a deliberate GX power release is
not supported by the code/observed stage results. This does not establish
what each UCHE register actually read back on the failing boot.

The ABI-only writes use the same GPU MMIO window and fault-contained helper
as stage14. The helper executes `dsb sy` after each access. Host aperture
`0xa00` uses PIPE bits 13:12, BR=1/BV=2, then restores zero, matching Qualcomm's
`gen7_host_aperture_set()` / GRAS setup. CP packet protection does not prohibit
these CPU AHB writes. Init finishes before ready is released to display
callers. RB/TPL1/SP were already writable during the observed clear/copy;
the explicit BR/BV GRAS selections still need device qualification.

The diagnostic change is in the canonical backend and regenerated patch
0098; patch 0100 stays unchanged. On the first init attempt, it takes one
snapshot of all ten GPU registers plus SMMU_ID2 before any comparison. It
prints eleven `FUXI GPU ABI: init regs ...` lines, then at most one named
`init check <name> got 0x... want 0x...` or
`init write <reg> [0x...]=0x... failed` line. The existing service-withheld
line follows failure. There is no retry or periodic dump. Access failures
have a separate `register_access` check; raw snapshot values following a
fault must not be treated as valid hardware facts. The first mismatch still
stops setup, and publication/table construction stay behind all checks.

Host coverage injects every register mismatch, GPU and SMMU read failures,
and each of the eight write failures into the actual C init function. It
checks the named reason, one-time dump cap, and untouched tables/unpublished
service after refusal. Existing PM4, mapping, async present, watchdog,
quarantine, execswap, governor, root-tool and SCM regressions pass. Artifact
checks verify the linked native recovery/SCM code, exact metal2a ADT and
cmdline, boot geometry, existing signed raw tool and all pinned firmware.
All 32 recorded native inputs match metal2a except the intended backend edit.
These are host tests; raw IB/readback and service publication are unproven.

Repack using the normal private loader/toolchain environment, after copying
the native kernels into `xnu/out-fuxi-metal2b`:

```sh
bash tools/build-fuxi-metal2-images.sh "$METAL2A_ARTIFACTS" metal2b
python3 tools/test-fuxi-metal-artifacts.py --lane metal2b \
  --base "$METAL2A_ARTIFACTS" --out xnu/out-fuxi-metal2b
```

Outputs and build/test logs: `xnu/out-fuxi-metal2b/`. Image:
`shim/out/fuxi-metal2b-boot.img`, SHA256
`0d0cb22d70c78380baab5fef91efb7a84f31eb5c2971a0943ad915b9be32f172`.
`SHA256SUMS` records the kernel, ADT, payload, image, loader and raw tool.

Expected next device output: eleven named raw-register lines, followed by
the precise failing check/write and service withheld if the failure repeats.
If all checks/writes pass, expect `FUXI GPU ABI: NativeAdrenoGpuPublished v1`
and then the existing bounded `FUXI ABI:` open/query/submit/wait/result trace.
Do not treat that success path as an observed 2b result.

---

# Metal lane, milestone 2a: route the existing raw test and observe it

2026-10-20, branch `codex-metal2`, base `b978770` (0098i integration plus
Wi-Fi M3/M4). Native build and all ten host test scripts pass. **This image has
not been booted by this lane.** The coordinator reports that 0098i reached
GPU stage 16 PASS, valid PERF temperatures and iOS userland, but never exec'd
the staged raw test. Stage 16 is not a userspace ABI test result.

## Patch 0100 and repeat handling

Apply `0100-sweet-execswap-gpu-abi-trace.patch` after 0099 (and 0095/0098).
0098 is unchanged; `sync-fuxi-metal2.py` maintains the canonical helper and
client delta. `sync-fuxi-adreno-shared.py --check` checks both generations.
The only Mac tree used was `ci-lane-metal2`, created with the requested APFS
clone of `ci-integ98`; 0099 and 0100 applied without fuzz. All 32 affected
native source files match a clean 0098i + 0099 + 0100 result, including the
m7c/m7d governor and default-off Wi-Fi sources. Logs/manifests are under
`xnu/out-fuxi-metal2a`. No phone/root-image access, push or binary rebuild.

`sweetexecswap=<name>:<absolute path>` substitutes the original executable
pathname after copyin and before namei, permissions, activation and signing.
It accepts one basename (1..31 bytes) and one non-root absolute target within
a 192-byte buffer; rejects truncation, lists, whitespace and extra colons.
Initialization and the eight-line boot log quota use a mutex. Without the
boot argument it changes no executable path. argv/env keep their original
userspace segment. A private unused image flag (0x10000) carries substitution
through activation, suppressing the late sweetnoexec check for this exec.
If the original name is also blocked, the routing line gains
` (wins over sweetnoexec)`. Other sweetnoexec entries retain their behavior.
No fallback to the original daemon occurs if the replacement fails.

The current signed test stays byte-identical (SHA below). For exactly
`bluetoothd:/usr/local/bin/fuxi-gpu-abi-test`, opens of type 0x4147 by the test
process (its actual exec basename, as exposed through truncated p_comm) are
marked diagnostic. After successful WaitFence, the kernel reads that client's
64 KiB BO at IOVA 0x20000000: word at +0x1000 must be 0xa740cafe, and every one
of 256 BGRA pixels at +0x2000 must be 0xff336699. It counts at most one verified
round per connection. Two successful rounds allow the existing binary's
close/reopen sequence, then refuse later diagnostic opens with kIOReturnBusy.
Other clients/types and boots without that exact swap configuration have no
round quota. This is a diagnostic signature check, not proof that stdout said
PASS or that a Metal/Vulkan client works. Later respawns exit quickly with the
existing binary's open-failure exit status; the kernel explains the refusal.

A single boot-wide budget of 64 `FUXI ABI:` lines covers open, query, device
info, alloc, GPU/CPU map, submit, wait, result, cleanup and refusal, including
pid and IOReturn. It works with bluetoothd's inherited stdout even if /dev/null.
The original independent 100 ms GPU watchdog and quarantine remain intact.

## launchd finding and future root edit

Inspected only the supplied `launchd.bin` (SHA256
`12370e5ee35bf28ae479b6c4b4455f4f4693ab803e37c5eb3032537c18cac61c`), using
strings, embedded configuration parsing and arm64 disassembly. Addresses below
are unslid and specific to this binary; Apple analysis outputs remain ignored.

* Embedded `Bootstrap.Paths` enumerates `/System/Library/LaunchDaemons`,
  LaunchAngels, AppleInternal, Nano and DeveloperMode directories. It omits
  `/Library/LaunchDaemons`. `SystemRoots` likewise omits `/Library`.
* At 0x100019c08..0x100019c40, `LaunchDaemons` is accepted as an XPC dictionary;
  0x100019f68..0x100019fc8 installs its entries into the cache. Merely adding
  a key is not itself a bootstrap request.
* The directory loader at 0x100019638..0x1000196f8 checks the configured path
  arrays and enumerates cached entries under the requested directory. Its
  callback at 0x100019e0c..0x100019e64 tests the entry's prefix before creating
  the load object. The prefix helper at 0x100016210 uses strlen/strncmp.
  Thus the old `/Library/LaunchDaemons/...` key is outside the default boot
  directory selection. This explains the observed absence of any exec attempt;
  confirmation of the corrected job still requires the next root edit/boot.
* ProgramArguments-only jobs are accepted (including embedded boot jobs).
  The supplied job has no `_LimitLoadTo*` restrictions. There is no evidence
  here that missing POSIXSpawnType or a mandatory Program key caused this skip.

`stage-fuxi-gpu-abi-test.py` now writes both the file and cache key at
`/System/Library/LaunchDaemons/org.fuxi.gpu-abi-test.plist`, explicitly sets
Program as well as ProgramArguments, and migrates/removes the legacy key/file.
It preserves unrelated jobs and validates the input before migration.
**This fix is committed for the next offline root edit; it was not staged.**

## Build, artifacts and expected device evidence

`build-fuxi-metal2-images.sh BASE_0098I_ARTIFACT_DIR` uses the same mkpayload
and loader/boot-v4 packing as 0098i, with its exact external GMU/SQE/zap blobs.
The ADT is byte-identical. The 463-byte command line is precisely 0098i's line
with `bluetoothd,` removed from sweetnoexec and
` sweetexecswap=bluetoothd:/usr/local/bin/fuxi-gpu-abi-test` appended.
`fuxigpuabi=1` stays on; `fuxiwlan` is absent/off, with patch 0099 still linked.
The build requires the same private toolchain/firmware environment as 0098i.

All host scripts passed: adreno-cache, adreno-mapping, adreno, gpu-probe,
memory, metal, metal2, perf, scm (including final native instruction audit),
and metal-artifacts (including firmware, raw signed Mach-O, geometry, symbols,
recovery table, exact command line/ADT and hashes). New ASan/UBSan tests compile
the actual parser/exec helper and result/mount/trace methods, checking parse
once, default-off, overlap logging, eight/64-line limits, failed pixels, two
rounds, repeat refusal, unrelated clients and stage migration/removal.
These are host checks, not device qualification.

Expected success sequence (N is the test PID, unchanged across its reopen):

```text
SWEET EXECSWAP: bluetoothd -> /usr/local/bin/fuxi-gpu-abi-test pid N
exec fuxi-gpu-abi-test
FUXI ABI: open pid N rc 0x00000000
FUXI ABI: query pid N rc 0x00000000
FUXI ABI: device-info pid N rc 0x00000000
FUXI ABI: alloc pid N rc 0x00000000
FUXI ABI: map pid N rc 0x00000000
FUXI ABI: cpu-token pid N rc 0x00000000
FUXI ABI: cpu-map pid N rc 0x00000000
FUXI ABI: submit pid N rc 0x00000000
FUXI ABI: wait pid N rc 0x00000000
FUXI ABI: result pid N rc 0x00000000 mem 0xa740cafe bad-pixels 0 rounds 1/2
FUXI ABI: cpu-unmap pid N rc 0x00000000
FUXI ABI: free pid N rc 0x00000000
FUXI ABI: close pid N rc 0x00000000
```

Then open/alloc/map/cpu-token/cpu-map/submit/wait/result/cleanup repeat, with
`rounds 2/2` (no second query/device-info in the current binary). A later
bluetoothd respawn, with a new PID M, should log the routing and exec again,
then:

```text
FUXI ABI: open refused pid M rc 0xe00002d5 already passed 2/2 rounds this boot
FUXI ABI: open pid M rc 0xe00002d5
```

Routing stops logging after eight substitutions; ABI tracing stops after 64
lines. Any nonzero step rc, nonzero bad-pixels, missing second round, watchdog
or fault must be retained as failure evidence. Do not infer a PASS from exec
or service publication alone.

| Artifact | SHA256 |
| --- | --- |
| `xnu/out-fuxi-metal2a/kernel.development.fuxi` | `247d669f7aa563e7bd03435e4d0268a3e557728b54146a6ae5f23141ec2781a1` |
| `xnu/out-fuxi-metal2a/kernel.development.fuxi.unstripped` | `71c87cfe4170c4ae5cc7ce3b20d77146cec4d15087c0086c709bc5bec886764d` |
| `xnu/out-fuxi-metal2a/fuxi.adt` | `39aacea4d7db372314bdc7928a66271b3b5cc2bd878bec4d9cfe2f5191e83533` |
| `shim/out/fuxi-metal2a-payload.bin` | `f281445725dfcc30a36194c071418e0155e504432378847710b4ee4f4e383dfb` |
| `shim/out/fuxi-metal2a-boot.img` | `b8c441ee639f0b2d125398d6748296d38625bac22262a9ca05faa439f6942056` |
| `shim/out/fuxi-metal2a-loader.elf` | `8385bb46c6346fa31788b48751e38ba9c2bcbe8fd70979a3fa06ceac4b9f6e25` |
| `tools/out/fuxi-gpu-abi-test` (unchanged) | `fc04e8157061a21d6f713197625010006e28b77b56c01cddf1d38ffd5cd85ae1` |

The complete artifact manifest is `xnu/out-fuxi-metal2a/SHA256SUMS`.

---

# Metal lane, milestone 1: NativeAdreno userspace ABI

2026-10-20, branch `codex-metal1`, based on `fuxi-bringup` `8dae9f4`.
**Native build and offline verification pass. No device execution is claimed.**
No phone access, push, firmware redistribution or Apple binaries in git.
`AGENTS.md`, `NOTES.md`, `FUXI.md`, patch 0097 and `xnu/fuxi-perf` are unchanged.
The only Mac build tree used was `ci-lane-metal`, cloned from `ci-lane-gpu6`.
The 27 native source inputs match the clean 0097b + 0098 patch result;
`native-inputs.sha256` and `native-inputs-check.log` accompany the kernel.

## Implemented contract

Apply `0098-fuxi-native-adreno-abi.patch` after 0096; 0097 is unchanged.
Canonical sources are in `xnu/fuxi-adreno`. `sync-fuxi-metal.py` generates
0098, and `sync-fuxi-adreno-shared.py --check` checks the combined snapshots.
0096 remains independently applicable. The default-off driver behavior and
loader addresses remain unchanged when `fuxigpuabi` is absent or not 1.

Both `fuxigpu=1` and `fuxigpuabi=1` are required. The service is an ordinary
`IOService` subclass, **NativeAdrenoGpuPublished**, registry name
`NativeAdrenoGpu`, accepting open type **0x4147** (also type 0 for Turnip's
direct backend). Only an administrator/root connection is admitted. There is
one live connection, recycled on close/death; no sandbox tunnel or mailbox.
For the unmodified Turnip patch use:

```sh
export TU_DARWIN_SERVICE=NativeAdrenoGpuPublished
export TU_DARWIN_OPEN_TYPE=0x4147
export TU_DEBUG=nocb
```

All 16 direct selectors and fixed struct lengths match NativeAdrenoABI v1.
Query, GetDeviceInfo, BoAlloc/Free/Map, BoCpuMap/Unmap, Submit and WaitFence
are implemented. Metadata and queue selectors are implemented as well.
GetTimestamp, GetStatus and BoImport are unsupported. Query does not advertise
HostImport or cached coherence. Queue 0 exists at open; extra queue IDs share
the same serialized hardware queue.

Limits: 64 BOs, 256 MiB per BO and 256 MiB live BO allocation budget per connection,
16 IBs per submission, one hardware job in flight. Allocation tries contiguous
wired memory first, then scattered wired pages unless contiguity is required.
It honors alignment 12..24 and constrains physical addresses to 36 bits.
Backing is rounded/zeroed to 16 KiB, while GPU mappings cover the requested
4 KiB-multiple length. BoCpuMap reports the actual CPU view length.
`clientMemoryForType` returns a stable physical-range descriptor for its token.
Both kernel and user BO mappings use Normal NC/write-combine. The descriptor
retains the prepared backing through its last CPU mapping, including forgotten
unmaps. Correct users unmap memory, release the CPU token, then free the BO.
Unreleased CPU mappings can retain memory until connection/task teardown.

The client has a separate 4 KiB SMMU root in the final 4 MiB of reserved
scratch (offset 28 MiB). Static bounds and the host model keep it beyond
the retained zap metadata, cold handoff and all three firmware slots.
Its full aperture `[0x20000000, 0x80000000)` is available; overlapping mappings
are rejected. The original scanout and four present-source windows stay in
the present root. With the ABI enabled only, runtime SQE/ring/pointer/fence
IOVAs move below the client aperture (`0x10000000` onward), identically in
both roots. TTBR switching on the existing GPU bank happens only with an idle
ring; ASID invalidation/TLBSYNC precedes a new doorbell and backing release.
No GMU bank, SID ownership or present PTE is repurposed for BO leaves.

A shared mutex serializes ring/control/PTE operations with present calls;
BO allocation and scheduler waits do not hold it. While a client owns the
ring, presentation uses its existing CPU copy. On successful client retirement
the present root is restored and GPU presents become eligible again.
Submission wraps client IBs in CP_INDIRECT_BUFFER with BR sync, context reset,
PERFCTR SRAM-init status wait, GPU cache flush/invalidate, BOTH threads and
IFPC off. Completion writes BV timestamp, waits on BR, writes the BR timestamp
with the IRQ bit, restores BOTH/IFPC and waits for idle. Context reset is emitted
on every return from the present context, including the first client submit.
RBBM interrupt delivery remains masked; no GIC/IRQ completion is claimed.

WaitFence sleeps in `fuxigpuwaitus` slices (default 100 us) without holding the client lock,
and returns `kIOReturnTimeout` for a user or hardware deadline. Zero timeout observes once. An independent delayed callback
checks every 2 ms, even when no client calls WaitFence, and declares a pending
job lost at 100 ms. Completion observed after the deadline wins if the ring is
already retired. Scheduling can delay observation beyond these intervals.
Kernel-side call cost (not yet observed on device): Submit, BoMap and BoFree
observe a pending job once instead of waiting for the 2 ms callback, so a busy
retry succeeds as soon as the fence is written. BoMap walks pmap once per 16K
page; BoFree keeps its TLB invalidate; read-only
imports are cleaned at submit but not invalidated at retirement; BoAlloc looks
up one physical segment per run. BoAlloc still zero-fills (see source comment).
The SWEET FB stats cadence also prints `FUXI GPU ABI: calls n/avg/max us ...`
per selector plus lock wait, segment walk, zero fill and sleeps, then restarts.
A client fault/hang permanently disables GPU submission for this boot, drains
GBIF, holds reset and faults SID0/1, allowing CPU presentation to continue.
Failed drain quarantines the wired BO references until reset. There is no
attempt to recover the CP or reset it beneath active UI work.

## Chip facts and reference provenance

References read: UniXNU `7b52e7968c76453cdc18b5aa174fc368d5293b91`,
UniXNU-sheng `8a0c9ddab92038a50abc07fffd4882ec4e301f45`, including
`NativeAdrenoABI.h`, `NativeAdrenoRing.cpp` and `GpuPowerProbe.cpp`.
MIT ABI/framing notices are retained in `LICENSE.NativeAdreno`; README credits
UniXNU. The sheng board code is BSD-2-Clause-Patent; it is a reference here.

| Field | Value and basis |
| --- | --- |
| chip_id, GMEM | `0x43050a01` / C510v2, 3 MiB, catalog entry using `a740_sqe.fw`, `gmu_gen70200.bin`, `a740_zap.mdt`; matches our previously qualified A740 firmware family. This milestone performs no fresh phone chip-ID read. |
| GMEM base / trap | `0x1000000` / `0x1fffffffff000`, exactly the UCHE values our existing bring-up programs; ABI publication checks their live readback and GMEM end `0x12fffff`. |
| HBB / swizzle / macrotile / UBWC | `16 / 6 / 1 / 4`: SM8550 platform configuration, encoder 4.0 (decoder is 4.3). Existing NC values are `0x816 / 6 / 0x26 / 0x60`; ABI init also initializes BR/BV GRAS banks. HBB UCHE bits are checked. |
| VA | start `0x20000000`, size `0x60000000`, ClientIova flag; driver policy, independently reserved in the client root. |
| Query widths | input 31, **output 36**. Recorded Fuxi SMMU ID2 is `0x5511`: OAS code 1 means 36 bits. The existing present mapper also rejects PA >= 2^36. UniXNU's Query advertises its own 40-bit allocation ceiling; we use the observed hardware limit. Turnip does not require 40. |

The pinned [Linux A740 catalog](https://github.com/ianchb/sm8550-mainline/blob/42f3b40c702abbc37c3a04c95af008cd283aeaf3/drivers/gpu/drm/msm/adreno/a6xx_catalog.c)
was independently hash-checked (`ff1abc1b7735d6bd6aa07d5df776e2549be2ee70ffcc02ff24aac646dfd6a3c7`).
The same tree's [UBWC configuration](https://github.com/ianchb/sm8550-mainline/blob/42f3b40c702abbc37c3a04c95af008cd283aeaf3/drivers/soc/qcom/ubwc_config.c)
and [SMMU ID decoding](https://github.com/ianchb/sm8550-mainline/blob/42f3b40c702abbc37c3a04c95af008cd283aeaf3/drivers/iommu/arm/arm-smmu/arm-smmu.c)
support the remaining configuration facts. Device-specific coherency still
requires execution; the catalog's coherent-capability quirk is not proof that
our XNU BO mappings are coherent.

## Raw test, build and coordinator procedure

`tools/build-fuxi-gpu-abi-test.sh` was run on the Mac using the installed
**iPhoneOS 27.1 SDK**, targeting **arm64-apple-ios16.0**, then ad-hoc signed.
The binary's LC_BUILD_VERSION is iOS 16.0. It dynamically resolves IOKitLib
because the public iOS SDK omits its headers. The script attempts the macOS
SDK with the same iOS target only if the iOS SDK is absent; that fallback was
not used or qualified in this run. Binaries go to gitignored `tools/out`.

The normal test waits at most 60 s for the service, opens 0x4147, queries,
allocates/maps a 64 KiB BO, writes an IB with CP_MEM_WRITE plus a 16x16 BGRA
solid CP_BLIT, submits, waits and verifies `0xa740cafe` and all 256 pixels
`0xff336699`. It then unmaps/frees/closes and repeats after reopening, exercising
address reuse and the enumeration/open-close pattern. Every step prints
PASS/FAIL to stdout; final success is `PASS: fuxi-gpu-abi-test`, exit 0.

The least invasive launch method is a dedicated root one-shot job in the
existing unsecure launchd cache, using the same plist editing mechanism as
`sweet-root-tweaks.py`. On an **already mounted writable root image**, the
coordinator can stage the tool offline:

```sh
sudo python3 tools/stage-fuxi-gpu-abi-test.py "$IOS_ROOT" tools/out/fuxi-gpu-abi-test
# After qualification, remove only this binary/job:
sudo python3 tools/stage-fuxi-gpu-abi-test.py "$IOS_ROOT" --remove
```

This copies to `/usr/local/bin/fuxi-gpu-abi-test`, writes
`/Library/LaunchDaemons/org.fuxi.gpu-abi-test.plist` and adds that job to
`/System/Library/xpc/launchd.plist`. It preserves other jobs, uses root without
a sandbox profile, RunAtLoad=true, KeepAlive=false, and stdout/stderr
`/dev/console`. No existing daemon or SpringBoard injection is needed. Keep
the existing root-image signing/AMFI policy; the tool is ad-hoc signed and no
new trust-policy bypass is added by this lane. The helper never mounts an image,
accesses USB or installs anything on a phone.

Coordinator acceptance, serially:

1. Install/stage through the existing root-image workflow and use
   `shim/out/fuxi-metal1-boot.img` through the coordinator's normal temporary
   boot procedure. Look for `FUXI GPU ABI: NativeAdrenoGpuPublished v1`.
2. Capture the complete PASS/FAIL output, exit status if available, GPU logs
   and present statistics. Require both normal rounds, reopening and readbacks
   to pass; exercise touch/app switching while presentation remains alive.
3. On a separate boot, stage with `--watchdog`. This deliberately submits an
   unsatisfied CP_WAIT_REG_MEM. Require `watchdog returns timeout`, a
   `client fence deadline (100 ms)` / GBIF containment log, continuing UI/CPU
   presents, and refusal of later client work. Failed drain should quarantine
   BOs and may make the free check fail; report it rather than treating it as
   safe reclamation. A reboot is needed to restore GPU submission.
4. Remove the diagnostic job afterwards. Test default-off by omitting
   `fuxigpuabi=1` from a separately packed image: no ABI service should appear
   and the existing present path should remain usable.

To reproduce the image, build the patched XNU in `ci-lane-metal` with
`tools/build-fuxi-native.sh` and the matching IIG cache, copy stripped/unstripped
kernels into `xnu/out-fuxi-metal1`, then supply the existing private toolchain
and GMU/SQE/zap environment variables:

```sh
bash tools/build-fuxi-metal-images.sh "$BASE_0097B_ARTIFACTS"
python3 tools/test-fuxi-metal.py
python3 tools/test-fuxi-adreno.py
python3 tools/test-fuxi-memory.py
python3 tools/test-fuxi-scm.py --kernel xnu/out-fuxi-metal1/kernel.development.fuxi.unstripped
python3 tools/test-fuxi-metal-artifacts.py --base "$BASE_0097B_ARTIFACTS" \
  --firmware "$FUXI_GMU_FIRMWARE" --sqe "$FUXI_SQE_FIRMWARE" --zap "$FUXI_GPU_ZAP_MBN"
sha256sum -c xnu/out-fuxi-metal1/SHA256SUMS
```

Offline coverage includes fixed layouts/selectors and overflow/flag checks;
an independent PM4 decoder checking reset/SRAM wait, parity, IFPC and BV/BR
ordering; the actual C backend compiled under ASan/UBSan with modeled register,
SMMU and scheduler interfaces; present exclusion and late fences; watchdog with
no WaitFence calls, faults and failed drain; clean patch application; existing
pixel/cache/lifetime regression models; native recovery/SCM instructions;
boot-v4/Image/payload/full-RAM bounds, unchanged 0097b ADT/root/CPU identity and
exact command line plus ` fuxigpuabi=1`; exact external firmware blobs; signed
raw Mach-O target. These are host models/build checks, not GPU execution.

## iOS 16.7 Metal discovery, re-derived

Facts from the supplied iPhone10,3 / 20H392 arm64 shared cache, inspected with
`ipsw dyld disass`, `macho --symbols`, `macho --objc` and `dump`. Main cache
SHA256: `76874e8375252ee643011ff8063f0cce12355f05cf5f9897a34e5679667a65aa`.
Addresses below are unslid and specific to this cache.

| Observation | Evidence |
| --- | --- |
| `_MTLRegisterDevices` at `0x181b144c8` matches `IOAcceleratorES` | String/matching at `0x181b14514..0x181b1452c`; iteration calls `_getMetalPluginClassForService`, `initWithAcceleratorPort:` (`0x181b14580`), then `_MTLAddDevice` (`0x181b1458c`) for a non-nil result. |
| Plugin properties and bundle lookup | `_getMetalPluginClassForService` at `0x181a3c7c8` reads `MetalPluginName`, builds `/System/Library/Extensions/<name>.bundle`, reads `MetalPluginClassName` or falls back to principalClass; checks `isSubclassOfClass:_MTLDevice` at `0x181a3c8d0`. |
| `_MTLDevice` instance size is **528**, not 536 | Class symbol `0x1e1d88a68`, decoded class data at `0x1d59e51a8`; class_ro fields flags=4, instanceStart=8, instanceSize=`0x210`. This must also be checked with `class_getInstanceSize` before constructing a runtime subclass. |
| Stock IOGPU open type | `_IOGPUDeviceCreateWithAPIProperty` at `0x1ece1ce38` constructs `1 | (api << 16)` at `0x1ece1ce68..0x1ece1ce80`, then calls IOServiceOpen. Our 0x4147 ABI is a different connection contract. |
| Kernel family availability | The linked metal1 kernel has the new native service metaclass and ABI symbols, and no `IOAcceleratorES` or `IOGPUFamily` implementation/metaclass symbols. This standalone payload does not include Apple's IOGPUFamily. Publishing an ordinary service alone cannot create an MTLDevice. |

Reproduce disassembly without copying any Apple code into git:

```sh
"$IPSW" dyld disass "$DSC" --symbol _MTLRegisterDevices --symbol-image Metal
"$IPSW" dyld disass "$DSC" --symbol _getMetalPluginClassForService --symbol-image Metal
"$IPSW" dyld macho "$DSC" Metal --symbols
"$IPSW" dyld dump "$DSC" 0x1d59e51a8 --size 24
"$IPSW" dyld disass "$DSC" --symbol _IOGPUDeviceCreateWithAPIProperty --symbol-image IOGPU
```

**Unverified:** iOS 16 sandbox selector/class allowances, private IOGPU object
vtable layouts, root-image plugin signing/loading and CoreAnimation's complete
requirements. Do not import UniXNU's iOS 17 IOGPU size 736 or `_MTLDevice` size
536 into this kernel. Its published plugin returns nil; its raw IB success does
not establish Turnip or Metal success. Its documented Turnip IBs still hang CP.

## Remaining milestones and acceptance gates

1. **M1 device qualification:** normal raw IB/readback/reopen, present coexistence
   and watchdog containment above. Current status: raw IB/readback/reopen qualified on metal2g as recorded above;
   no new watchdog-containment or presentation qualification is claimed here.
2. **M2 Turnip cross-build for iOS 16:** offline build/audits complete;
   root load/enumeration/device lifetime passed on metal2h (see M3 report above). Follow UniXNU
   `scripts/build-turnip-darwin.py` and `scripts/data/adreno-compiler-inputs.json`.
   The actual build pin is Mesa **85fda3b36405bcaea8bffc64aa4f2651dc90770d**;
   `gpu-metal-vulkan-inputs.json` also records **c823a83e...** as a reference
   survey tree, not the revision the build script checks. Apply, in manifest
   order: `freedreno-tools-nir.patch`, `ir3-darwin-build-id.patch`,
   `ir3-portable-asm-roundtrip.patch`, `offline-adreno-compiler-probe.patch`,
   `turnip-darwin-kmd.patch`. Retain MIT notices; keep source/build caches out
   of git. Change the cross target from ios17.0 to **arm64-apple-ios16.0**.
   Use iPhoneOS SDK, Meson `freedreno-kmds=darwin`,
   `tu-darwin-abi-dir` pointing at our ABI header, `vulkan-drivers=freedreno`,
   `platforms=macos`, no Gallium/GL/EGL/GBM/LLVM, and native glslang/build tools.
   Audit iOS availability, Mach-O load commands/dependencies, cache-line helpers
   and absence of EL0 CTR_EL0 instructions; the supplied patch now substitutes
   Darwin's 64-byte cache-line value. Require exported vk_icdGetInstanceProcAddr
   and a root-daemon load/enumeration before submitting shaders.
3. **M3 Turnip smoke:** observed PASS on metal3 (see report above), including
   fill, compute, triangle, reuse and reopen with TU_DEBUG=nocb. This qualifies
   that tested workload; no Metal frontend was involved. Preserve finite waits
   and CPU UI fallback after any future loss. Broader coherence, reset/recovery
   and workloads beyond the tested subset remain separate gates.
4. **M4 Metal discovery/frontend:** native discovery class + signed transfer-only
   bundle and smoke built/host-tested (M4 section above). **Observed PASS:** coordinator reported real unsandboxed-root
   MTLCreateSystemDefaultDevice, shared-buffer blit fill, copy and 16 reuse
   rounds. M5a adds a default-deny process gate and safe census stop. A runtime size guard
   requires 528. No iOS sandbox access, Apple user-client ABI or selector tunnel
   is implemented. Stop on failure; derive any additional requirements from
   the supplied iOS 16 cache before expanding the interface.
5. **M5 Translation and licensing:** keep **metal2vulkan external** at pinned
   `720ef1ce9ae2a2068999a8675717418b9416283e`; its declared choice is
   GPL-2.0+ OR LGPL-3.0+. Do not vendor it or statically fold it into this repo's
   kernel/plugin artifacts. Distribution needs a separate license/relinking
   plan before shipping a combined frontend. Validate AIR/metallib translation
   and shader caching separately. Imported host/IOSurface BOs, page lifetime
   and coherence are additional kernel work; M1 advertises no HostImport.
6. **M6 presentation/CoreAnimation:** implement IOSurface texture import,
   CAMetalLayer drawables, producer/consumer fences and buffer lifetime, pixel
   format/stride/orientation handling, and CA's required render/compute paths.
   Preserve the existing CPU-present fallback through frontend/plugin failure.
   Require actual GPU-rendered app and CA content, UI correctness, latency,
   thermal and memory measurements. Neither UniXNU's nil-returning plugin nor
   its partial frontend supplies this integration today.

## Risks and delivery

metal2g qualified raw IB/readback/reopen; metal3 subsequently qualified the
reported Turnip fill/compute/triangle/reuse/reopen workload. These do not
qualify general coherency, CP recovery, shaders beyond that workload, or M4
Metal discovery. M4 discovery/transfers passed on device; M5a adds host-verified gating and
census. M5a device acceptance and all presentation integration remain pending. Raw PM4 is privileged and not validated as a security
boundary; only an unsandboxed root diagnostic client is supported. Arbitrary
root IBs can access privileged GPU state. Failed bus drain leaks/quarantines
memory until reboot; wedged interconnect/MMIO can exceed software containment.
There is no CP recovery, preemption, fair scheduling, host import, IRQ completion,
MTLDevice, drawables or accelerated CoreAnimation in M1.

Artifacts and their exact hashes are in `xnu/out-fuxi-metal1/SHA256SUMS` and
`tools/out/SHA256SUMS`; all binaries/firmware/Apple analysis output are ignored.
The command line is the supplied 0097b line plus exactly ` fuxigpuabi=1`.
The ADT remains byte-identical, including seven CPUs, pinned UFS root and
5088 MiB of XNU RAM with the existing 32 MiB GPU reservation.

| Artifact | SHA256 |
| --- | --- |
| `xnu/out-fuxi-metal1/kernel.development.fuxi` | `40662b2881e536fbcada825e92768718d92aa35999afa52d13c5dc8211b80088` |
| `xnu/out-fuxi-metal1/kernel.development.fuxi.unstripped` | `4376b4eefb3c66f0c1eac928459df7b7d3fab7d9473837ba889c7e45e7fc0b22` |
| `xnu/out-fuxi-metal1/fuxi.adt` | `39aacea4d7db372314bdc7928a66271b3b5cc2bd878bec4d9cfe2f5191e83533` |
| `shim/out/fuxi-metal1-payload.bin` | `781e358637e37205e6bc1140406d7965801293516ad74c9453a733273969eeb2` |
| `shim/out/fuxi-metal1-boot.img` | `c430735773e9b97f0fa60f37244da085ab219ad97c46224dbf2f297834b59851` |
| `tools/out/fuxi-gpu-abi-test` | `fc04e8157061a21d6f713197625010006e28b77b56c01cddf1d38ffd5cd85ae1` |

## CA scheduled wait and commit reservations

2026-10-26. Host-verified only; no device/120 fps result. The supplied profiler
finds about 19% of CA's display thread in the scheduled wait; supplied frame
traces suggest about 4 ms of waiting between commit and swap. Letting the next
encode overlap submission/GPU work is the performance hypothesis, not a measured
result of this change.

**Previous behavior, facts from code:** without overlap, commit synchronously
submits, waits for the fence and retires caches; `waitUntilScheduled` is a no-op.
With overlap, caller-side `reserveOverlap` takes an in-flight semaphore and
reserves serial `_submitQueue` order (including explicit enqueue order). Commit
captures no-copy CPU bytes and releases `_commitReady`. The worker waits for
older queue entries, takes sorted exclusive `SurfaceLease`s, refreshes shadows
and calls `Backend::submit`. Backend can wait for a free slot, full drain or
host-range conflict before recording, flushing and calling `QueueSubmit`.
Successful submission sets `_scheduled`/Scheduled status; empty submissions
also schedule. Event-split work can set the flag on the first submitted chunk.
`waitUntilScheduled` returns if already scheduled/finished/error, otherwise waits
for `_scheduledGroup`, released after the worker publishes scheduled handlers;
that wait can therefore include handlers. `commitAndWaitUntilSubmitted` commits,
waits and reports scheduled/no-error. It need not await this frame's fence, but
admission, surface reuse and backend waits can transitively await older fences.

The supplied CA stack calls `MetalContext::flush(true)` immediately before IOMFB
swaps the drawable IOSurface. The inferred requirement is that swap must not
copy an incomplete surface, rather than requiring the CPU to observe Vulkan
submission. This is an inference about this CA path, not all private Apple uses.

**Kernel ordering, facts from the original reservation change:** swap saved
the newest record/`fDirty`; the timer copied it later. The FIFO correction
below supersedes that mailbox behavior. Normal IOSurface writer locks count
`gpuWriters` under `sweet_gpu_lock` after waiting for an older display copy of that surface.
The worker holds its lease from before submission through render-fence/cache
retirement. GPU copy rejects writers/quarantine (owned mode returns 3 and
preserves dirty work without CPU fallback). `fuxi_gpu_scanout_reserve` also
observes the client fence/ring-idle state via `fg_abi_observe`, retires imported
caches/restores TTBR, and excludes new client submissions until copy ownership
is established. `sweet_surface_gpu_poll` polls **the display copy**, retaining
its exact source until fence/drain; it cannot track an unsubmitted Metal render.
Double scanout publishes the completed back buffer only after transfer/TE/START
guards pass. The previous CA wait ensured the worker had acquired the producer
lease before swap; same-ring order alone cannot protect an early unsubmitted
render.

**Enable early CA waits** with these existing exact switches together; no new
`fuxigpuperf` bit:

```
fuxigpuoverlap=1 fuxigpusubmit=1 fuxigpuscanout=1 sweetfb_noconsole=1
```

Only backboardd changes behavior; any missing/invalid switch keeps its previous
wait. Keep the existing GPU/ABI and qualified panel/TE/double-scanout setup;
`fuxigpupipelined=1` remains independent. Startup logs `FUXI MTL: CA scheduled
wait early=1 commit-reserved surfaces`. Use the matching frontend/kernel.

Commit now synchronously acquires a deduplicated **counted pending lease** on
all retained imported surfaces with `IOSurfaceLock(kIOSurfaceLockAvoidSync)`
(option 2), before returning or releasing `_commitReady`. The updated kernel
increments `gpuWriters` without sleeping for old display readers/copies.
The worker still needs its normal exclusive writer lease before touching pixels.
Queue order stays caller-reserved; in-flight admission moves to the worker,
so a third commit cannot await an older GPU flight on CA. `waitUntilScheduled`
returns immediately on this path; commit-and-wait reports accepted/no currently
known error. Scheduled status and handlers still follow real backend scheduling;
explicit enqueue, events, completion/presentation handlers and completion waits
retain their ordering and behavior.

Before copying any layer, guarded present retains the exact source objects and
atomically checks all of them for producers/quarantine, holding a read reservation
through CPU/GPU copying. A pending surface defers the **whole** record, leaves
the previous panel frame, restores dirty work and keeps polling without another
swap. The FIFO correction below retains pending records instead of replacing
them with each newer swap. The guard covers CPU/multi-layer/unsupported-GPU
fallback too. If pending commit races an existing read, its count increments immediately but the worker's
normal lock waits for that read, and for the copy fence if a GPU copy started.
If commit wins first, present cannot start reading. Render fence/cache retirement
precedes the final producer unlock. This mutual exclusion proves ordering without
assuming render-before-copy GPU ring order; multiple pending producers remain
unreadable until **all** retire. Existing cache/ring/copy/publication guards stay.

Success releases both leases before callbacks. Errors after reservation keep it
closed, even known pre-submit errors, so a swapped partial multi-submit render
cannot become readable. Unknown DMA retains existing poisoned ownership too.
Such surfaces may remain undisplayable until teardown/reboot. Reservation failure
returns failure and submits no render; reader admission/allocation failure defers.
GPU flights remain bounded to the queue limit/two slots, but queued commands,
resources and snapshots can accumulate if encoding outruns retirement. Physical
framebuffer writers and clients bypassing normal writer leases remain outside
this contract.

The original backend drain was retained here (superseded by
[Index admission and unchanged sampled shadows](#index-admission-and-unchanged-sampled-shadows)):
`hostUses` tracks noncoherent allocations only,
but indexed validation also CPU-reads coherent index bytes; the opaque shadow
`prepare` callback can CPU-write shared coherent backing. Replacing these drains
with today's range test misses coherent GPU/CPU conflicts and aliases. Narrowing
requires complete read/write and prepare-destination metadata. These waits now
happen on the opted-in worker.

`fuxigpuframetrace=1` (or 2) adds `sched_wait=p50/p90` ms, measuring actual
entry-to-return of overlap `waitUntilScheduled`, including immediate returns and
excluding commit. An atomic queue handoff attributes the most recent wait to
its **next commit**, like the existing prior-completion fields; first sample is
zero. Compare steady-state `sched_wait`, `commit_call`, `admission_wait`,
`submit_queue`, `slot_wait` and `commit_gap`; small waits alone do not prove fps.

Verification: portable `test-fuxi-metal-frontend.py` ASan/UBSan models pass;
new `OverlapFrontendTests.mm` paused-queue/recording, enqueue/handler ordering and
failure tests are **not compiled/run on Linux** and still need macOS.
`test-fuxi-metal-overlap.py` passes lavapipe two-flight pixels/resource lifetime,
noncoherent exclusion and loss/timeout; `test-fuxi-metal-frametrace.py` passes.
`test-fuxi-gpu-scanout.py`/`test-fuxi-unixnu.py` pass production-code sanitizer
models for pending deferral/retry, multiple producers, CPU/read/copy exclusion,
real trap option/error wiring and quarantine, plus existing TE/double failures.
Complete changed surface/framebuffer translation units and unchanged Adreno/
console integration pass AArch64 syntax against saved XNU headers/flags; no
kernel link or macOS/iOS frontend build. `test-fuxi-adreno-mapping.py` passes.
Patches **0106** and **0108** are regenerated; **0098** is unchanged. Every
`tools/sync-fuxi-*.py --check` passes. Device qualification still must establish
IOSurface option propagation, image/cache correctness, failure behavior,
backlog/latency and CA/START cadence; no phone access or deployment occurred.

### Presentation FIFO correction

2026-10-27. The supplied device observations on 7a0decf show the intended
zero scheduled wait, but about 23 shown frames/s and about 36 present deferrals
per shown frame. Code confirms the starvation mechanism: `AppleCLCD::swap`
replaced its single record and counted dirty replacement as not shown;
`present` rejected a pending/active producer, restored only dirty state, and
writer retirement did not wake the presenter. Faster CA commits could therefore
replace each pending record before the next poll could consume it.

CLCD now keeps three waiting records, retaining the exact source objects at
swap. It selects the oldest readable record under the queue lock and takes the
existing all-layer read reservation before removing it. An unreadable head stays
queued without a drop; the first readable newer record may skip unreadable
predecessors. Overflow drops the oldest waiting record. Copying runs outside the
queue lock, so swaps can arrive during a copy. A ring/ownership refusal restores
the selected record to the head; if arrivals filled the FIFO meanwhile, that
older deferred record is the overflow victim. The last accepted record is
retained separately for redraw and exact failed-copy CPU replay, ahead of newer
queued swaps. This bounds waiting records, not frontend command-buffer backlog.

The final nonquarantined writer decrement queues one coalesced thread call.
Its worker enters the existing workloop gate and arms the existing presentation
timer at 1 us only for dirty, powered-on, unheld work. Unlock performs no display
work or timer manipulation. Registration/unregistration uses the surface lock;
teardown unregisters and cancels/waits for the call. Early wakeups preserve
notification deadlines/counts, including before scanout ownership is acquired.

A completed double-buffer copy waiting for TE publication cannot be overwritten
by another GPU copy. CLCD keeps polling it before selecting another FIFO entry.
All existing CPU/black-fill, double re-entry, console, drain, quarantine and stop
guards remain. SWEET FB completed-frame accounting advances at successful START
(or the legacy kick), including a deferred publication retry; submission and
failed copies do not count. The FUXI SCANOUT 120-copy summary still counts copies,
not panel transfers. Neither counter proves optical panel completion.

The existing 600-frame SWEET FB line adds cumulative `queued_pending`,
`fifo_overflow`, `ready_skips` and `ownership_defers`. The first counts record
read-admission refusals, including pending/active writers and quarantine; the
last counts fence/ring/ownership/drain/publication retry deferrals. These are
poll/record attempts, not frame losses. `not shown` counts only overflow plus
ready skips since the preceding stats line; queued pending records are excluded.
No new per-frame log or logging cadence is added.

Host coverage exercises production FIFO order, multiple writer deferral and
retirement, final-release wake/coalescing/gate delivery, oldest overflow, ready
skips, source retention, arrivals during copy, exact CPU replay, TE back-buffer
protection and publication accounting, plus existing fail-closed paths. The
portable frontend/lease and overlap tests and saved-XNU AArch64 syntax checks
remain applicable. Native Objective-C frontend execution and real GPU/cache,
TE/START cadence, image integrity and bounded device latency still need device
qualification; no phone access, deployment, kernel link, push or CI run occurred.
The frontend reservation/fence-retirement contract is unchanged. Generated
patches changed: **0098**, **0106**, **0108**; **0100** is unchanged.

Validation passed: 14 related host suites (including production scanout/unixnu,
frontend lease models, lavapipe overlap and external KMD submit), all 12 sync
checks, and complete framebuffer/surface/Adreno/console/vfs/UFS syntax with saved
XNU headers. Native Objective-C tests are skipped on Linux.

### Retirement queue and source-reuse pacing

2026-10-27. **Device results for this revision: not yet observed.** See
[the scanout pacing audit](gpu-scanout.md#source-consumption-before-start-and-retirement-queue-isolation)
for the supplied 43.7 shown/s baseline, short shared-cache conclusions,
SwapWait/source-hold contract, latency accounting and qualification criteria.
CA's ordinary reuse waits pass individual page IDs and mode 0; displayed-frame
checks use mode 1. The cache does not prove queue-acceptance retirement or the
runtime CA page count. Source consumption after clean-copy retirement now
releases mode 0 and display holds before TE/START, while mode 1 remains tied
to START. Writer/read/cache/unknown-DMA guards remain mandatory.

The overlap retire queue now ends after backend fence/cache retirement,
producer-lease destruction and flight-slot release. A separate serial finish
queue handles scheduled-group waits, gate/trace reporting, completion delivery
and command-resource cleanup. This prevents a slow finish tail from delaying
the next lease retirement. Submit ordering, actual scheduled status, handler
order, completion waits, backend conflict/index/shadow drains and poison
retention remain. Slow completion work can retain completed command resources;
the two GPU slots do not bound the early-submit command backlog.

`retire_queue=p50/p90` ms measures dispatch-to-retirement entry in the existing
frame trace; other retirement, submit and fence fields retain their meaning.
`wall` starts at commit and ends after lease release; it excludes the separate
`cpu_before_commit`. No comparison of p50 wall/submit/GPU time isolates a single
extra delay. The new native paused-finish test requires that three same-surface
commands retire and release every lease/flight while completion work is paused,
then deliver their completion handlers in order. Native `.mm` compilation and
execution still require macOS; Linux portable tests cannot establish that result.

## Display-thread CPU allocation and encoding cost

2026-10-27. **Device results for these changes: not yet observed.** The supplied
60 s / 30,000-sample display-thread profile reports 49% runloop idle, 513 texture
`vkAllocateMemory` samples, about 1,250 record-copy samples, 174 operation-vector
growth samples, 1,382 registry samples and 227 SwapWait samples. The supplied
trace p50s are CPU-before-commit 6 ms, backend submission 3 ms, GPU execution
6.3 ms and commit gap 13 ms, with about 48 fps and 17% multi-tick intervals.
These describe the preceding device build; they are not host test results.

`Backend::allocateTextureUnlocked` now takes device-local memory from an exact
requirements-size/alignment, Vulkan-format/usage and memory-type pool. The pool
holds at most **32 allocations / 64 MiB**, within the existing **256 MiB** total
resource charge. It caches memory, not images: each new image starts undefined
with a fresh layout/defined-level state. `destroyTextureUnlocked` donates only
successfully created ordinary allocations after the final reference disappears.
Async `submit` pins every used texture before QueueSubmit and drops those pins
only after fence/cache retirement. Synchronous submission and destruction share
the backend mutex. Imports, borrowed buffer-image memory and failed creations
never donate memory; IOSurface lifetime, lock, use-count and ownership semantics
are unchanged. Device loss poisons the backend even without a live flight;
poisoned pools neither reuse nor free memory, retaining containment semantics.

Unused entries expire after **five seconds**, checked by the maintenance worker
once a second and on ordinary texture allocation. Resource-budget pressure,
texture allocation OOM (one retry), Darwin memory-pressure warning/critical
notifications and clean close trim quiescent cached allocations. Notification
cancellation completes before backend destruction. Trimming can contend for the
backend mutex; it never waits for a live texture to become recyclable. The
maintenance thread now also runs when persistent shader caching is disabled.

The existing five-second `FUXI MTL: backboardd frames=...` line appends lifetime
**`tex_pool_hits`, `tex_pool_misses`, `tex_pool_trims`, `tex_pool_bytes`**.
Hits/misses count ordinary texture-memory requests, trims count cached memory
objects actually freed, and bytes is the current retained-memory gauge (including
quarantined idle memory after loss). Capacity refusals immediately free the
returning allocation and do not count as cached-object trims. There is no new
per-frame logging. Existing frame-trace `allocations` counts actual Vulkan
allocations, so a hit contributes zero.

**Slab sub-allocation (N167 follow-up; device effect not yet observed).** Device-local
images up to 4 MiB are bound at aligned offsets inside shared 16 MiB `VkDeviceMemory`
slabs (`SlabMemory.hpp`, at most 8 slabs, charged to the resource budget when the slab
is created). A texture's range is returned only from `destroyTextureUnlocked`, i.e.
after the submit that pinned it has retired, so a block is never reissued while the
GPU may use it. Larger images keep the exact-class block pool (now 256 entries, 30 s
idle trim). Texture aliasing checks compare memory plus offset range
(`Backend::memoryAlias`). Empty slabs are freed after 30 s idle except the newest per
type, and under budget pressure. `fuxigpunoslab=1` restores the pool-only path.
The backboardd line appends **`tex_pool_miss_causes`** (cold, inflight, type, slack,
small, idle_trim, pressure, reject_full, reject_budget; each pool miss counted once),
and **`slab_hits`, `slab_grows`, `slab_grow_cold/frag/full`, `slab_grow_refused`,
`slab_frees`, `slab_trims`, `slab_bytes`, `slab_free_bytes`**. A slab grow is one
kernel allocation; steady state should show `slab_hits` growing with `slab_grows` flat.

`record` previously copied the **2 x 16 `BufferBind` array, 768 bytes** with its
explicit `memcpy(op.buffers, _buffers, sizeof(_buffers))`. These are pointers,
offsets and flags, not CA's vertex/index payload. It also copied `_state` into
a temporary `Operation` and moved that into `_ops`; the LP64 production operation
is **1,712 bytes**. The supplied `+0xfec` PC cannot be mapped conclusively to
one compiler-generated copy without that build's binary/symbol mapping. These
are the exact source-level copies in that location, rather than an inferred
inline-data length. Bindings now live in `_state.buffers`; `record` constructs
the snapshot directly in `_ops`, eliminating the separate 768-byte binding copy
and temporary-to-vector move. One immutable state snapshot per draw remains.

Each queue recycles up to four empty operation vectors within **8 MiB**, reserving
256 operations initially and remembering the bounded completed-command high-water
capacity. A first larger workload can still grow; warmed commands reuse storage.
Live/queued commands never share vectors; overflow storage is destroyed with its
command. Completion returns storage, and abandoned commands return it at dealloc.
Backend submission still makes its existing per-submit working vector copy.

`setBytes` already copied each payload once, but allocated a Vulkan buffer for
every call. It now copies once into a command-owned upload ring of **64 KiB**
blocks, with 256-byte slice alignment and zeroed 16-byte tail padding. Each
binding records its logical end in existing `BufferBind` padding, keeping the
24-byte binding / 1,712-byte operation size. Translator input, descriptor range
and vertex-fetch checks honor that end instead of the block's spare capacity.
Caller mutations and later setters cannot change an older draw's snapshot.
A command is limited to 64 blocks (**4 MiB**); a queue retains at most eight
retired blocks (**512 KiB**) until queue destruction. Live blocks remain charged
and retained through completion; loss discards them from reuse. This is bounded
upload storage, not a cache for imported or general-purpose buffer ownership.

Read-only iOS 16.7 cache conclusions (facts only, no Apple code/binaries tracked):
`IOMobileFramebuffer`'s `kern_CopyProperty` queries the opened framebuffer
service in plane **`IOService`**, allocator null, options **0** (no recursive or
parent-search bits). QuartzCore's `copy_display_property` fallback likewise
uses the display service, `IOService`, options 0. In this cache,
`IOMFBDisplay::begin_work_interval` starts at **0x1882c1e38**; **+0xf8** is
**0x1882c1f30**, after work-interval start, not a property-search call site.
Its direct calls and resolved heartbeat/minimum-duration paths do not establish
a searched key. Thus the supplied stack attribution does **not** identify a
well-defined missing property, and absence alone does not establish a recursive
walk. **The key remains unresolved; no AppleCLCD property is fabricated.**
A matching caller PC/build mapping is needed to attribute those registry samples.

Code identifies synchronous WaitFence candidates, not the caller of the older
uncaptured poll sample: non-overlap `commitSynchronous` submits without a
retirement ticket; CPU texture `getBytes`/`replaceRegion` transfers do the same;
event-split chunks also synchronously retire before crossing the CPU event.
`Backend::submit` then calls `WaitForFences` before cache invalidation/return.
Overlap's final chunk instead waits in the retire worker. Backend slot/index/
shadow drains wait on its condition variable for prior retirement, not directly
in the kernel poll. `allocateTextureUnlocked` and `record` contain no explicit
fence wait; their driver calls cannot be attributed to the uncaptured stack.
`FuxiAdrenoUserClient::wait` sleeps between `fuxi_gpu_abi_poll` attempts using
`delay(waitPollUs)` (default 100 us). None of these waits is proven unnecessary;
removing CPU-read/write or source/cache retirement guards would be incorrect.
SwapWait/TE/transfer guards are also unchanged by this frontend-only revision.

Expected benefit is fewer kernel BO allocations and fewer render-state copies
and heap growth stalls. At 48 frames/s the three sampled allocation/copy/growth
sites represent roughly **0.36 / 0.87 / 0.12 ms per frame** of sampled thread
residence (about **1.35 ms/frame total opportunity**, not a promised saving).
Residual snapshot copies, misses, scheduling, GPU execution and display waits
remain. Registry and fence samples have no claimed saving. No additive latency
budget or tail bound follows from these samples: stable 60 fps / all frames
under 16.6 ms and more frames under 8.3 ms still require device qualification.

Validation: portable frontend/policy/storage/upload tests, production Vulkan
memory-pool tests (software ICD, ASan/UBSan, loss/timeout), backend, frame-trace,
unixnu and scanout suites pass. Native `EncodingFrontendTests.mm` adds actual
selector/storage/slice/reuse checks; native compilation/execution and Darwin
memory-pressure delivery remain unverified on Linux. All twelve sync checks
pass. Regenerated **0098**, **0106**, **0108** are byte-identical; **no generated
patch changes**. No kernel or external Mesa/metal2vulkan/NativeMetal source
changes, device access, deployment, firmware download, push or workflow run.

## Index admission and unchanged sampled shadows

The shadow-reuse portion below is historical and has been reverted after device
measurement; see [Restore simple texture shadows](#restore-simple-texture-shadows).
The read-only-index admission remains in effect.

2026-10-27. **Device results for this revision: not yet observed.** The supplied
227-frame backboardd trace on iOS 16.7 / FuxiMetal `78aaa3a` reports
`wait_reasons=drain:180,conflict:0,slots:0`, slot-wait p50/p90 **6.312/8.160 ms**,
submit queue **3.0/8.1 ms**, QueueSubmit **1.04 ms**, commit gap **12.5/33.3 ms**,
GPU execution p50 about **6–8.7 ms**, wall p50 **24 ms**, and cadence ticks
**1:192,2:21,3:1,4+:13**. These are the preceding device baseline, not results
of these changes. They support the serialization diagnosis, not a measured
60/120 fps improvement.

`Backend::submit` previously drained whenever a retirement ticket accompanied
any `Operation::indices`. The protected read is the CPU `memcpy` scan of
`indices->contents + indexOffset + indexWidth*i`: it finds the largest index,
excluding primitive restart for strips, then checks fixed-function attribute
fetch bounds with `vertexFetchFits`. The translator draw planner receives only
length/offset/type metadata, not those bytes. An earlier GPU fill, copy,
image-to-buffer readback, mapped attachment alias or storage-buffer shader could
otherwise change an index during inspection, or leave noncoherent host bytes
stale until retirement. `hostUses` alone cannot protect this: it intentionally
excludes coherent allocations. Same-command GPU-written draw input still needs
a separate submission; inspecting old bytes cannot validate future GPU results.

Each live slot now retains **all resource uses and possible GPU writes**, separate
from its existing noncoherent cache-maintenance uses. Writes include explicit
buffer destinations, color/clear/upload/copy/mip texture destinations (including
borrowed backing), and storage buffers from the **final runtime draw plan**.
Only a final-module explicit `NonWritable` proof removes a storage writer;
unknown storage remains writable, independently of merge policy. Matching
Vulkan memory or overlapping host address ranges catch buffer/image/import
aliases. Tracking is conservative at allocation granularity. The metadata lasts
through fence wait **and cache invalidation**, then clears under the backend
mutex with slot release. Synchronous submissions hold that mutex through their
fence/cache retirement. Poison wakes admission into `requireLive` without
inspecting potentially quarantined shadow bytes.

Index inspection now requests a drain only while another live slot may write
its allocation. The wait predicate rechecks after every wake. A command-owned
upload ring or ordinary buffer with no pending writer does **not** request an
index drain. Upload provenance is diagnostic, never permission to ignore a GPU
writer. `setBytes` marks its command-owned 64 KiB blocks; slices remain immutable
through completion and reuse still starts only after retirement. An opaque
`prepare` callback retains its full drain. Noncoherent whole-allocation
flush/invalidate conflicts retain their existing exclusion. No third slot was
added: two remain independently resettable and pinned. Baseline slot-only waits
were zero; whether two slots limit the improved workload is **not yet observed**.
The existing third-submit test proves admission blocks until a slot retires.

The supplied **8,752,548 bytes/frame** in each of `snapshot_bytes`,
`shadow_copy_bytes` and `texture_shadow_bytes`, with about **46.74 sampled
shadow-buffer uses/frame**, describes the no-copy **buffer fallback**:
`newBufferWithBytesNoCopy` tries host import, then allocates GPU backing when
unaligned/read-only/COW client pages cannot be imported. Its public `contents`
remains `_wrapped`; `retainCommandTexture` records the sampled buffer texture's
offset/row extent, and `prepareCommandShadows` counts that extent once per
backing buffer. `commit` freezes caller bytes before returning;
`executeCommitted` previously installed an opaque callback that copied the
snapshot into shared backing after a full drain. It is not an IOSurface snapshot:
`importTexture` uses the same IOSurface pages and the existing producer leases.
`textureRepitchedBytes` is a separate GPU buffer-to-optimal-image pitch fallback
in `Backend::submit`'s `refresh`, not the CPU snapshot/copy. Its supplied
**513,947 bytes/frame** is unchanged by this revision.

**No-copy import range (code only, device effect not yet observed).** J163 logged one
refusal: `Vulkan rc -1000072003 at Backend.hpp:433 (host import)`, i.e.
`VK_ERROR_INVALID_EXTERNAL_HANDLE` from `AllocateMemory` after the 16 KiB pointer and
length checks had passed, so that buffer was refused by Turnip/the kernel, not by
FuxiMetal alignment. The log is one-shot, so the share of unaligned pointers is unknown.
`importBuffer` now imports the enclosing 16 KiB pages (`HostImportRange.hpp`) and binds the
`VkBuffer` at the pointer offset (`Buffer::memoryOffset`, folded into linear-texture binds and
the alignment check), so unaligned or ragged-length CA pointers no longer fail on FuxiMetal's own
rule; every imported page holds a caller byte. Offsets the buffer cannot bind at are refused.
The kernel `importHost` logs bounded `FUXI GPU: host import refused stage N ... detail` lines
(1 args, 2 quota, 3 slots, 4 no VM entry, 5 prepare, 6 length, 7 physical page: detail is the
offset, 8 kernel mapping, 9 wire alias), and the `no-copy buffer shadowed` line now carries
`ptr%16K len%16K` and attempts/aligned/enclosed/plan_refused/vulkan_refused counters. A
read-only/COW page refusal would need a read-only import ABI; not attempted without that
evidence. No generation-based skip exists for raw pointers, so shadow copies stay for refused imports.

Raw no-copy pointers have no observable CPU mutation generation or IOSurface
seed. Skipping on object identity, shader-read usage or `didModifyRange` alone
would miss legal CPU writes. `ShadowCache` instead compares exact bytes at
commit and shares immutable chunks of at most **4 KiB** with command snapshots.
Only changed chunks allocate/copy; previous queued versions remain immutable.
The cache retains at most **4 MiB per fallback buffer** and clears on capacity
pressure; eviction affects cost only. Completed snapshots release their chunk
references; queued commands can retain older versions, as with the existing
unbounded early-submit backlog. Caching applies only to bounded, sampled-only
buffer textures. General buffer/index access, targets, explicit useResource,
unknown/full ranges and synchronous commit retain the original packed path.
Both raw pointer writes and `replaceRegion` through texture views are detected.

Shadow application now supplies explicit snapshot destinations to the backend,
rather than an opaque callback. Before comparing mapped backing it waits for
any overlapping GPU writer, including coherent buffers and aliased attachments.
Snapshot destinations are ordinary mapped fallback buffers; borrowed linear
images also publish their backing buffer, so their writers expose that range.
Unchanged cached chunks require no backing write and can overlap prior readers;
changed chunks wait for overlapping GPU readers/writers before copying. An
unrelated flight need not retire. Each comparison/application runs under the
backend mutex; a condition-variable wake rechecks hazards. Comparing the actual
backing also detects intervening GPU writes or another queue's older version;
a CPU version match alone never licenses skipping the backing refresh. Packed
fallback snapshots still copy all their recorded ranges and exclude overlapping
flights. Event-split commands apply the snapshot once before the first chunk.
Opaque callers of `prepare` still drain.

The existing aggregate frame-trace line adds:

- `drain_submits`: submit admission calls requesting a drain, including empty
  or subsequently rejected calls; counted once per call, even across wakes.
- `drain_reasons=indices-upload,indices-gpu-writable,prepare`: counts of calls
  encountering each reason. Upload is a subset of GPU-writable and normally
  zero; reasons may overlap. Upload bytes alone never cause a drain.
- `drain_reasons` now ends `prepare:N,prepare-conflict:C,prepare-unknown:U`.
  `prepare` still counts calls carrying a prepare callback. The overlap shadow
  refresh declares its destinations (`PrepareWrite`: buffer, offset, bytes) and
  waits only (`prepare-conflict`, also in `wait_reasons` conflict) for busy
  flights whose all-memory `uses` (coherent included) alias them; it no longer
  drains. Only an undeclared callback (`prepare-unknown`) still drains. Not yet
  observed on device.
- `index_submits=upload,readonly,gpu_writable`: calls containing upload indices,
  ordinary indices without an observed pending writer, or a pending GPU index
  writer. Mixed calls can occupy multiple categories.
- `shadow_waits`: calls encountering a snapshot destination hazard; separate
  from full-drain, cache-conflict and slot-only reasons. Existing `wait_reasons`
  counts each reason once per call, including hazards first seen after a wake.
- `snapshot_reused_bytes/frame`: frozen texture bytes reused without a new CPU
  snapshot copy; `snapshot_bytes/frame` now counts actual new snapshot payload.
- `shadow_skipped_bytes/frame`: snapshot payload already equal to GPU backing;
  `shadow_copy_bytes/frame` counts actual applied payload. `texture_shadow_bytes`
  retains its logical range meaning, so it need not equal either copy counter.

For unchanged warm sampled inputs, the opportunity is **8.75 MB/frame fewer
snapshot copies plus 8.75 MB/frame fewer backing copies**, about **17.5 MB/frame**
of memcpy payload (1.05 GB/s at 60 fps, 2.10 GB/s at 120). Dirty inputs copy only
changed chunks; first use/cache eviction copies again. Byte comparisons still
read the ranges and may contend for cache/memory bandwidth; the previous
snapshot p50 **1.26 ms** is an opportunity, not a promised time saving. Removing
unnecessary drains permits CPU recording/submission alongside the previous GPU
flight, but the observed 6–8.7 ms GPU time, surface leases, noncoherent conflicts,
slot pressure, queue backlog and display pacing still govern 60/120 fps.
Device qualification must compare new reason/byte counters, slot/admission/
submit waits, pixels, cadence, latency and GPU execution; no device access or
new device timing is claimed.

Validation: all **34** `tools/test-fuxi-metal*.py` entrypoints were invoked.
The **17 runnable scripts passed**: backend, frontend (portable ASan/UBSan models),
frametrace, shadows (including new real-Vulkan `AdmissionTests`), overlap,
merge/barriers, memory, mipfeedback, M6, M9m/n/o, metal, metal2, metal2g/h and
stage with `--policy-storage-only`. Shadows additionally compare cached/bounded/
full pixels over 25 textures / 125 draws, unchanged repeated frames and CPU
mutations; admission holds real retirement tickets open to check upload/read-only
indices, fill/attachment/storage GPU writers, opaque prepare, unchanged/dirty/
GPU-written shadow backing and unrelated flights. Portable cache tests cover
4 KiB dirty chunks, frozen queued A/B/A versions, changed extents, eviction,
backing invalidation and policy fallback. Frame-trace tests check aggregation,
reset and all added fields. Native encoding/shadow tests add upload provenance,
view CPU writes, paused submission and reused/dirty snapshot assertions; native
`.mm` compilation/execution remains **unverified on Linux**. Two-slot exclusion,
noncoherent conflicts and loss/timeout quarantine still pass. `slot_wait` includes
admission predicate work as well as sleeps; `shadow_copy` includes the backing
comparison/application, so skipped bytes do not imply zero CPU time.

The other **17 scripts could not run their suites** because required external
translator/checkouts, Apple SDK/metallib/cache or boot/artifact inputs are absent:
artifacts, ca-offline, cache/cache-hits, defaults, draw-plans, M6b, M7,
M9a/b/g/h/i/k/l, pipeline and translation. Their argument/precondition failures
are not passes. M9o's optional external shader/line portion and stage's real
artifact verification are likewise unavailable. Old M9n/o/shadow teardown
assertions now explicitly trim intentionally pooled memory before checking the
baseline; the mip-feedback fixture disables draw-plan reuse because its push
bytes change through an environment variable outside `DrawInput` (production
planning has no such input). All **12** `tools/sync-fuxi-*.py --check` pass;
no generated patches changed. No device access, deployment, external project
changes, vendoring, Apple/GPL code or binaries, push or workflow run occurred.

## Restore simple texture shadows

2026-10-27. The supplied **observed** backboardd trace for `53ddcd8` (149 frames),
compared with `78aaa3a` (227 frames), validates read-only-index admission:
`wait_reasons=drain:0` versus `drain:180`, with `index_submits=readonly:149`.
It rejects byte-compared texture-shadow chunk reuse. Shadow-copy p50/p90 rose
from **0.634/1.350 ms** to **12.220/118.277 ms**; commit snapshot rose from
**1.262/1.969 ms** to **3.007/4.341 ms**, with commit-call about **3 ms**.
Retire-lock rose from **0.001/0.001 ms** to **1.356/104.638 ms**; slot-wait was
**0.229/53.838 ms**, submit-queue **0.040/102.120 ms**, and backend-submit
**14.9/122.5 ms** versus **3.6/5.8 ms**. Wall p50 rose from **24 ms** to **75 ms**,
effective fps fell from **49** to **30**, and one-tick 60 Hz cadence fell from
**84%** to **44%**. Reusing **3.2 MB/frame** of snapshot bytes and skipping
**6.9 MB/frame** of backing copies (only **230 bytes/frame** copied, with
**3.7 MB/frame** of new snapshot bytes) cost more than copying the logical
**6.9 MB/frame** of texture shadows. These are supplied device observations,
not a new run or a timing claim for this restoration.

Code attribution in **`53ddcd8`**, with line numbers at that revision:

- `Frontend.mm:1393–1397` calls `ShadowSnapshot::addCached` directly on the
  caller's commit thread. `ShadowRanges.hpp:93–101` (`ShadowCache::capture`)
  locks the per-buffer cache mutex for each chunk, does a `std::map` lookup and
  full-byte `memcmp`, and allocates a vector/shared ownership for changed
  chunks. `addCached` at lines 118–121 also appends a reference for every chunk,
  even on unchanged frames. This work precedes commit return; it does not hold
  the backend mutex.
- `Backend.hpp:893` acquires `Backend::mutex` before admission; lines 895–899
  also collect/deduplicate all resource uses under it. `shadowHazard`
  at lines 908–912 scans each snapshot segment/chunk against live-slot writer
  and use lists. After excluding writers, it calls `eachDirty`
  (`ShadowRanges.hpp:127`) to compare mapped backing bytes, then checks dirty
  ranges against overlapping GPU readers. The condition-variable predicate at
  `Backend.hpp:918–923` repeats this work on wakes while holding that mutex.
  The actual condition-variable sleep releases the mutex; the comparisons and
  nested range scans do not. `slot_wait` includes predicate CPU work.
- `Backend.hpp:934` applies snapshots under the same backend mutex.
  `ShadowSnapshot::apply` (`ShadowRanges.hpp:138–142`) compares cached backing
  bytes again before copying and releases chunk references, potentially freeing
  older queued versions there. `shadow_copy` includes that work, even with
  almost no copied payload. The retirement callback waits for the fence outside
  the mutex, then takes it at `Backend.hpp:1572–1573` before invalidation,
  resource release and slot wakeup. Thus admission/application can delay
  retirement despite a completed fence. The submit queue is serial
  (`Frontend.mm:1750–1751` in that revision), so a slow submit also delays later
  commands. The trace fits this contention mechanism; it does not isolate the
  cost of each individual compare, allocation or scan.

Restore the `78aaa3a` shadow implementation: one command-owned packed snapshot
of recorded ranges before commit returns, then unconditional `memcpy` of those
ranges into shared backing. The existing two-entry `ShadowPool` still reuses
retired packed storage. Remove `ShadowCache`, immutable chunk maps/references,
backing comparisons, explicit shadow-destination admission and its all-use
metadata. Overlap mode again installs the original opaque `prepare` callback,
which fully drains before writing shared backing under the backend mutex;
event-split commands apply it once. The synchronous refresh and bounded/full
range rules are restored too. Raw caller pointers have no reliable generation
or mutation seed, so identity alone cannot safely skip their copies.

Keep the `53ddcd8` index-writer tracking, coherent/import/attachment alias checks,
final-plan storage-writer proof, upload provenance, read-only-index admission,
noncoherent exclusion, poison handling and all index/drain counters unchanged.
Only the reader-use metadata needed by removed shadow admission is dropped;
GPU-writer metadata remains through fence wait and invalidation. A shadow copy
can once again increment `prepare`/drain counters; an otherwise read-only index
does not cause a drain. Keep every frame-trace field and its aggregation/reset:
`snapshot_reused_bytes`, `shadow_skipped_bytes` and `shadow_waits` now read zero;
`snapshot_bytes` and `shadow_copy_bytes` again count all recorded payload.

Portable controls now assert full copies for unchanged inputs, frozen queued
A/B/A versions, overwriting intervening GPU writes, bounded/full/fallback pixels,
and prepare exclusion even for unrelated flights. The real-Vulkan admission
controls retain the upload/read-only and GPU-written index checks. Native
`ShadowFrontendTests.mm` keeps view writes and paused queued snapshots, expecting
all 142 bytes captured/applied on every commit and zero reuse/skip counters.
Its submit calls and the modeled backend match the restored four-argument API;
native `.mm` compilation/execution remains unavailable on Linux and was checked
by inspection.

Validation: invoked all **34** `tools/test-fuxi-metal*.py` entrypoints. All
**17 runnable suites passed**: backend, frontend, frametrace, shadows (including
real-Vulkan admission and synchronous/overlap pixels), overlap, merge/barriers,
memory, mipfeedback, M6, M9m/n/o, metal, metal2, metal2g/h, and stage with
`--policy-storage-only`. Existing local shader tools enabled the Vulkan controls;
no dependencies were downloaded. The other **17** require unavailable external
translator/checkouts, Apple SDK/metallib/cache or boot/artifact inputs:
artifacts, ca-offline, cache/cache-hits, defaults, draw-plans, M6b, M7,
M9a/b/g/h/i/k/l, pipeline and translation. Their argument/precondition failures
are not passes. M9o's optional external shader/line controls and stage's real
artifact verification remain unavailable. All **12**
`tools/sync-fuxi-*.py --check` pass; `git diff --check` passes and generated
patches are unchanged. Frame-trace definitions/tests are unchanged from
`53ddcd8`. No device access or new device measurement, external project changes,
vendoring, Apple/GPL code or binaries, push or workflow run occurred.


## 3D sampled textures (backboardd GPU-gate latch, boot K164)

OBSERVED (K164): backboardd requested `type=7 format=70 size=16x16x16 mips=1
usage=0x1` (MTLTextureType3D, RGBA8Unorm, ShaderRead; probably a CoreAnimation
colour lookup table), FuxiMetal refused it on `textureType` and the refusal
latched the GPU gate (`next-respawn=CPU`). The swizzle in that descriptor was the
identity (2,3,4,5) and was never the cause.

Change (host-tested on lavapipe, device effect not yet observed):
- `newTextureWithDescriptor` accepts `MTLTextureType3D` when ShaderRead only
  (no RenderTarget), one mip, one array slice, one sample, depth within the 2D
  device limit and `ResourceLimits::volume`. `textureShapeAccepted` gained
  depth/levels arguments; `allocateTexture` gained a trailing `depth`.
- Backend: `VK_IMAGE_TYPE_3D` images with depth extent, `VK_IMAGE_VIEW_TYPE_3D`
  sampled views (`viewTypeFor`/`imageTypeFor`, also used by the descriptor path;
  the translator-reflected `texture_type` 7 already matches `Texture::textureType`),
  `depth` in `TextureLevels` (`levelDepth`). Sampler address mode W was already
  mapped from `rAddressMode`. The component swizzle path is unchanged.
- Transfers: `Operation` gained `srcZ/dstZ/depth/imageBytes` (0 = whole level
  depth / tightly packed rows). `replaceRegion`/`getBytes` (z origin, depth,
  bytesPerImage), buffer<->texture and texture<->texture blits carry slices;
  Vulkan `bufferImageHeight = bytesPerImage / bytesPerRow`; alias snapshots cover
  all slices. A 2D transfer still requires depth 1, z 0 and no image stride.
- Still refused cleanly: 3D render targets/clears, 3D with mips or
  `generateMipmaps`, linear (buffer-backed) 3D, IOSurface 3D, array/cube/MSAA,
  2D<->3D copies, depth slices on 1D/2D textures, out-of-range z/depth,
  `bytesPerImage` not a multiple of `bytesPerRow`.
- Tests: `tools/test-fuxi-metal-m9p.py` (16x16x16 RGBA8 upload/readback tight and
  padded, partial box, overlapping z copy, refusals; sampling a 3D texture in a
  draw needs the external translator and was not run), plus a 3D block in
  `BlitFrontendTests.mm` (compiles only on macOS; checked by inspection).
- New log fields: none. Expected device evidence: no `texture reject
  textureType 7` line and no `gate runtime-failure`; not yet observed.

## AdrenoMetal port review (surface import reuse, refusing render encoder)

Source: AdrenoMetal (MIT), `NativeBridgeMetalFrontend.mm` (`surfaceBuffer`, `encoder:handle:what:`) and
`bridge/patches/0008..0016`. The source's git history was not readable from this lane; the current tree was compared.

- Present import reuse (ported in spirit, device effect not yet observed): `Backend::importTexture` takes the IOSurface ID
  as `identity`; a live import with the same (id, base, bytes, row, offset, w, h) is shared by reference count instead of a new
  `vkAllocateMemory` + `vkMapMemory` + image/view per CA wrap. The cache holds no reference, so an entry dies with its last wrapper
  or pending command; a new mapping, size or layout misses. New log fields on `backboardd frames=`: `surface_import_hits`,
  `surface_import_misses`. Test: `MemoryReuseTests.cpp` (lavapipe host import).
- Refusing render encoder (ported, device effect not yet observed): every refused `renderCommandEncoderWithDescriptor:` returns a
  `FuxiRefusedRender` that forwards all selectors to `rejectSelector:` (command buffer stays rejected and errors on commit), instead
  of nil. RenderBox caches encoder IMPs with `methodForSelector:`, which is NULL for nil. Census mode still returns nil.
- Not ported, padded-row aliasing (0008) and tile-store bound (0016): they edit the metal2vulkan Rust runtime. FuxiMetal's own
  `linearTexture` only aliases when Vulkan's linear pitch equals Metal's and otherwise falls back to a shadow copy (J163 shows A8
  masks at row 256/512/768 against Turnip 64-byte pitches). A wider image over the padded row would need the sampled coordinates
  scaled by width/pitch in the translated shader, which this lane cannot do; written-texture aliasing is not reachable (the fallback
  only exists for sampled-only textures). Candidate for the translator/build-patch lane.
- Not ported, 0009..0015: all metal2vulkan runtime/bind/canon changes with no FuxiMetal equivalent.
- Not ported, frame/wait trace: FrameTrace already records `waitUntilCompleted`/`waitUntilScheduled` durations per frame; their
  addition is the caller return address, judged too little to add.
## GPU map/unmap cost, ranged TLBI and IO-coherent imports (not yet observed on device)

Ported from UniXNU `ArmSmmu4K` and `Adreno740` (MIT; newest work compared by reading the source files, git history of the
sources was not readable). Sources reviewed: `ArmSmmu4KLive.c` (`ArmSmmu4KSyncContextTlbPages`, `ArmSmmu4KTlbPageLimit=512`,
the `aliases=0`/exclusive map path), `ArmSmmu4KProgram.c` (TLBIVAL +0x620, AArch64 format VA[55:12] | ASID<<48, TLBSYNC +0x7f0),
UniXNU docs `drivers/arm-smmu4k.md` and `drivers/adreno740.md` ("Memory and TLB" table). Our client root is a flat 4K table with
no per-range alias classes, so the port is the same idea in our shape:

- **Overlap test without the NC read-back** (`fuxi_gpu_abi_backend.inc`): map pass 1 used to read every target leaf from NC
  table memory. `fg_abi_used[]` is a cached 48 KiB bit-per-4K mirror written only by map, unmap and init; pass 1 reads that.
  Refusal is still exact to 4K and publishes nothing. Pass 1 also no longer resolves the leaf table pointer per 2 MiB.
- **Segment lookup per run in BoImport** (`FuxiAdrenoUserClient.cpp` `importHost`): one `getPhysicalSegment` per physical run;
  the pmap cache-class predicate (`fuxi_adreno_host_page_ok`) still runs for every 16K page. Refusal stage 7 now reports the
  offset of the failing page.
- **Ranged TLBI on unmap, boot-arg `fuxigpuncimport` value 128, default off**: up to 512 4K pages are invalidated with one
  64-bit TLBIVAL each plus TLBSYNC and the same status poll as `g4_tlb`; larger unmaps, and every unmap without the bit, keep
  the whole-ASID invalidate. A failed write or sync is a fault (`client unmap TLB sync`), never a skip. It needs a 64-bit store
  to the SMMU context bank (`g3_sw64`, via the existing wide `fg_access`), unverified on this SMMU: hence opt-in. Note
  `fg_abi_switch` still does a full ASID invalidate on every root switch (submit and retire); UniXNU does none at submit. That
  needs a separate ASID for the client root and is not done here.
- **IO-coherent imports, value 64, default off**: at the first `BoMap` of an import the kernel runs a fail-closed self-test.
  A cacheable kernel page is mapped with the import leaf attributes (Normal NC, outer-shareable), the CPU leaves 8 lines
  dirty and caches 8 others, and one IB makes the GPU copy the 8 dirty lines to an NC result page (`CP_MEM_TO_MEM`) and
  overwrite the 8 cached lines (`CP_MEM_WRITE`). Only 16/16 matches set the verdict "coherent"; then submit stops cleaning
  imports, retirement stops invalidating them and the map-time clean (value 1) is skipped, the avoided bytes count in `skipped`.
  Stale lines, partial matches, a map failure inside the probe window, a fault or a 100 ms hang leave the existing
  clean/invalidate path (a hang or fault also poisons the GPU like any client fault). Busy results retry, three times at most.
  The probe uses 4 KiB of NC scratch after the ABI page tables, the last 64 KiB of the client aperture and a 16 KiB kernel BSS
  page; it unmaps them and cleans the page afterwards. Scope of the proof: the kernel page and the import pages share the CPU
  attribute class and the leaf bits; a PASS on a few lines at boot cannot prove every later line, which is why it is opt-in.
  Expected device evidence: `FUXI GPU ABI: coherent-import self-test pass cpu_to_gpu 8/8 gpu_to_cpu 8/8 -> skip ...` or
  `stale ...` (the expected result on a non-snooping path), then `cleaned`/`skipped` deltas in the presents line.
- `fuxigpuncimport` is a bitmask now: 1 NC-import (unchanged), 64 coherency self-test, 128 ranged TLBI. Values 2..32 are
  deliberately unused so concurrent lanes can claim them; other values of those bits were not meaningful before.
  (Since claimed: 2 fenced WPTR, 4 stall recovery, 8/16/32 frame log, copy priority, superseded skip; see gpu-scanout.md.)
  Bit 25 (value 33554432) is not a GPU option: it runs the kernel PAC self-test (patch 0114, [arm64e.md](arm64e.md)).
  Bit 27 (value 134217728, needs 16) parks the job copy priority would refuse instead of refusing it (gpu-scanout.md).
- Tests: `tools/test-fuxi-metal.py` (overlap mirror, TLBIVAL values/limit/fallback/fault, self-test pass/stale/partial/hang/
  fault/busy/occupied, bitmask parse), `tools/test-fuxi-hostimport.py` (segment lookups, per-page predicate in a long run).
- New log field: the self-test line above. Owned BO maps still need one pmap lookup per 16K page (scattered BOs); a contiguous
  BO could skip it but the backend does not know contiguity, not changed.

**Submit waits and shadow rename (O168 analysis; device effect not yet observed).**
`lease_back` in the frame trace is `Backend::submit` entry to slot acquisition (backend
mutex plus the slot/conflict wait), not a display or IOSurface lease; `lease_lock` is
`lease_front` (the IOSurface locks, about 0.03 ms) plus `lease_back`, which is why it
equals `slot_wait`. `wait_reasons conflict` and `drain_reasons prepare-conflict` mean a
declared shadow prepare (`ShadowSnapshot::apply`, which memcpys changed ranges into a
backend buffer) overlaps memory used by an in-flight slot, so the submit waits for that
flight's fence (GPU retirement). `conflict_kinds=buffer,shadow,texture,surface,index`
counts submits that waited per resource kind; `display_lease_wait_ms/frame` is the only
display-side wait. `fuxigpurename=1` (opt-in; requires overlap) renames the backing
buffer of a read-only shadow texture (never bound directly, tracked by
`Buffer::directUse`) to a spare storage of equal size: the current bytes are copied
there, the write goes to the new storage, and the flight keeps reading the old one. The
snapshot key is `Buffer::shadowKey()` so queued snapshots follow the swap. New fields:
`shadow_renames/frame`, `shadow_rename_bytes/frame`.

**Why the first rename never engaged (P169; fix not yet observed).** `conflict_kinds`
reported shadow and texture together at the same count because the busy use was one
linear alias texture: `linearTexture` binds an image directly to the shadowed buffer's
memory (it adds the `texture` use for the mapped range and the `shadow` use for its
`backingBuffer`). Such a buffer is sampled in place, so its storage cannot be swapped
under the bound image, and `Buffer::aliased` now declines it. With rename enabled,
`FuxiBuffer`'s sample-only linear textures over a shadowed buffer take the shadow-copy
path (`shadowed` argument), after which the buffer is a plain shadow backing and
renameable. The policy init logs `FUXI MTL: policy shadow_rename=N source=boot|env|none`,
and `rename_declined=off,not_backing,direct_use,texture,budget,other` counts conflicting
writes that could not be renamed.

## Rolled uber-shader loops (FUXI_MTL_SPIRV_UNROLL, default off, not yet observed on device)

Evidence (boots L217/M218/N219, `FUXI_MTL_PIPELINE_STATS=1` + `FUXI_MTL_GPU_DRAW_TIMES=1`; those boots carried leftover
knobs, but the ir3 counts are the same in all three). Of ~116 pipelines, the 6 that keep loops after Turnip
(`Loops=3`) compile to 1553-1784 ir3 instructions. Every other `fixed_frag_lph_cph` variant compiles to 43-568 and has no
loops. Those 6 take 45-55% of the sampled top-pass draw time, and in M218's heavy samples (top pass >= 2 ms) pipes
71/3a/6f alone take 64%. Measured over the scissor area, pipe 3a costs ~4000 ns/kpx and pipe 71 2000-2400. Their
loop-free sibling 32 (same SPIR-V shape, 270 ir3) costs ~370. The loopy variants are the ones whose colour program is set: `fc_color_opcodes0` uchar3 = 05 01 06 and
`fc_color_inputs0`=3. Each has `loops_dontunroll=2`. Those two loops are metal2vulkan's roll pass (`spirv/roll`, above
24576 inlined instructions). It writes identical regions once as a k-trip loop marked DontUnroll and chooses each trip's
constants by OpSelect on the trip index. So the per-trip opcode switch no longer folds: the driver keeps the loop and
every switch arm (INFERRED from the stats; the SPIR-V was not inspected).

`FUXI_MTL_SPIRV_UNROLL=1` turns DontUnroll into Unroll on fragment loops that contain no image instruction, right before
vkCreateShaderModule. Only the LoopControl word changes. Loops that sample (metal2vulkan's anisotropic taps) are kept.
NIR (`nir_opt_loop_unroll`) force-unrolls an Unroll loop when its trip count is known and no inner loop remains. Mode `2`
also marks the plain loops nested inside a flipped loop. The translation, recipes and prewarm stay as they are. Changed
modules are new driver-cache keys, so the first boot compiles cold.

Logs:
- `FUXI MTL: policy spirv_unroll=N`.
- One `spirv unroll pipe= fh= mode= loops= dont_unroll= flipped= nested= kept_sampling= create_ms= rc=` line for each
  pipeline it touches.
- `fh=` (a hash of the translated fragment SPIR-V, which does not depend on the knob) on the `pipeline stats` and
  `gpu draw top` lines, plus `unroll=`.
- Every 20 draw-time samples, `gpu draw pipes window=all|heavy` lists the top 8 pipelines over all passes, with
  `ms/sample`, `share_pct` and `ns_per_kpx` (heavy = a sampled submit with >= 4 ms of draws). This lets two boots be
  compared per pipeline by `fh`.

Device check:
- `pipeline exec ... name=FS` for the six loopy `fh` values should drop to `Loops=0` and a few hundred instructions.
- Their `gpu draw pipes` ms/sample should fall.
- `create_ms` shows the compile cost.
