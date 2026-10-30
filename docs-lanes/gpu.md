# Milestone 7b — observed 0097 results and rebuild

**OBSERVED on fuxi-0097-boot.img (coordinator, 2026-10-20):** full RAM works,
SpringBoard runs, no panic. GPU reports `async one-in-flight, no timer sleep`.
One recorded window:

```text
SWEET FB: stats 600 frames in 132086 ms, copy avg 656 us max 1730 us, 608 swaps, 8 not shown
FUXI GPU: presents GPU 594 CPU 2, GPU retire avg 16055 us CPU avg 963 us
split: clean 619 submit 30 wait 16028 us; GPU CPU-time 685 us
cache hit 593 miss 2, sleeps 0, busy ticks 0 failed 0
```

The ~16 ms retire/wait value is submission-to-next-tick observation, not GPU
execution latency. These results confirm full-RAM boot and async presentation,
not improved subjective smoothness. Touch boost failed during init before the
GPU's first line; [thermal notes](thermal.md) distinguish the confirmed LUT-tail
read defect from the unproven cause of that particular device failure.

0097b changes the governor's LUT reads, failure diagnostics and CPU-sensor
quorum. It retains the 0097 RAM layout and async GPU backend. Native build exit
0 in **ci-lane-gpu6 only**; all sixteen clean-patch source inputs match the
native tree. GPU/CPU loader builds pass with the existing pinned external
GMU/SQE/zap blobs. No firmware or binaries are tracked. Both payloads use the
same kernel/ADT, seven CPUs and pinned root, with `fuxiperf=1`; they differ in
exactly the one byte selecting `fuxigpu=1` or `fuxigpu=0`. No phone access or push.

Offline checks: shared-source sync; clean 0096/0097 patch application and native
input hashes; ASan/UBSan GPU submit/poll/cache/mapping and governor models;
full/1g RAM and >4 GiB USB offsets; external-firmware probe models; SCM native
instruction/register audit; linked read/write abort recovery; boot-v4/Image,
payload/ADT memory bounds, root/CPU identity and exact embedded firmware.
The governor model also compiles the actual XNU `io_map` from the supplied tree
and checks 16 KiB page offsets in both allocation paths. No 0097b device result
is claimed.

```sh
python3 tools/sync-fuxi-adreno-shared.py --check
python3 tools/test-fuxi-adreno.py
python3 tools/test-fuxi-memory.py
python3 tools/test-fuxi-perf.py --xnu-tree "$XNU_SOURCE"
python3 tools/test-fuxi-scm.py --kernel xnu/out-fuxi-0097b/kernel.development.fuxi.unstripped
# Use the existing private firmware paths for --firmware / --sqe / --zap:
python3 tools/test-fuxi-gpu-probe.py --firmware "$GMU" --sqe "$SQE" --zap "$ZAP"
```

| Artifact | SHA256 |
| --- | --- |
| `xnu/out-fuxi-0097b/kernel.development.fuxi` | `f3968b0138e7e224c834b220c5655d5f96eff6c57cc496ce45b4b9d8263cb8eb` |
| `xnu/out-fuxi-0097b/kernel.development.fuxi.unstripped` | `1db45880370d21aa92f2a67ef07caca11f6fba1ec655e74dfa150c8bdc251957` |
| `xnu/out-fuxi-0097b/fuxi.adt` | `39aacea4d7db372314bdc7928a66271b3b5cc2bd878bec4d9cfe2f5191e83533` |
| `shim/out/fuxi-0097b-boot.img` | `4873e5a9c0d8e8d9d8953accfb8195e75ed75bd1322a748b3450def995e47bb4` |
| `shim/out/fuxi-0097b-payload.bin` | `93c9677da8d2ccca619ecbf726f8cf048bfd9b6b6ff993ce9018adb436ebf2dd` |
| `shim/out/fuxi-0097b-cpu-boot.img` | `0fafacfaa34101ddbc19b1124ba2344bce09f17775f1d2c1de35e872e7fcc4d0` |
| `shim/out/fuxi-0097b-cpu-payload.bin` | `ef23ac4e04512f865f7971fa7ff1ff6fba6b3ce897a3090eae501bf24f26bf93` |

# Milestone 7 — full RAM and asynchronous present

XNU now gets **5088 MiB**, with the last 32 MiB of the observed 5 GiB bank
reserved for GPU scratch and the cold handoff. See [memory layout](memory.md)
for addresses, XNU limit audit, USB offset extension and the `--ram-layout 1g`
compatibility option. The coordinator has now booted 0097 successfully; see
the observed results above. 0097b remains unbooted.

Coordinator evidence for 0096e: GPU presents work at 1.58–1.61 ms wall and
0.72–0.74 ms CPU time, with nearly all cache hits. The user reports worse
subjective smoothness than CPU-present 1 GiB builds. RAM pressure, cool CPU
votes and the sleeping display-timer wait are hypotheses; milestone 7 changes
all three, so the GPU/CPU image pair isolates only the present backend.

## Async ownership and completion

The display timer submits once and returns. At the start of every subsequent
60 Hz tick it checks the fence, read/write pointers, idle and fault gates once.
It never calls `thread_block`, `assert_wait` or a completion sleep. A finished
fence retires even if the timer woke beyond its 100 ms timeout; a still-pending
job at that deadline disables the GPU permanently. There is at most one frame
in flight. Every busy tick preserves the latest pending swap, sends VSYNC as
usual and defers all framebuffer writes, including power-off clearing. A
successful retirement kicks the display; submission alone does not.

The source `SweetSurface` has a strong reference until completion/drain and its
cached IOBuffer remains retained and prepared. Cache unmap/eviction refuses the
in-flight slot. IOSurface writer lock traps wait on the **producer thread**
until retirement, and submission refuses an existing writer. Writer unlock
releases that exclusion. The timer does not wait for a producer. This contract
requires producers to obey IOSurface lock/unlock; arbitrary writes through raw
mappings cannot be fenced by retaining an OSObject. Failed drain quarantines
the surface as well as the prepared descriptor, rejects later writer traps,
and holds isolation/reset/SID faulting as before. CPU fallback is retained;
on async failure the newest pending swap is replayed through it.

`FUXI GPU: ready, ... async one-in-flight, no timer sleep` identifies the path.
Stats add `busy ticks N failed N`; `GPU retire avg` and split `wait` measure
submission-to-observed-retirement wall time, normally about one tick even when
the GPU finishes much earlier. They are **not** GPU execution latency. `sleeps`
is zero. CPU-time still excludes descheduled time and counts submit/poll/cache
work. ASan/UBSan tests compile the actual split submit/poll and cache functions:
pixel copies, duplicate-submit/unmap refusal, busy ticks, writer exclusion,
retained-source lifetime, late fence, timeout/fault/isolation and quarantine.
Offline tests do not establish producer compliance, real DMA coherency, tearing
or smoothness. Failure containment can spend bounded time polling GBIF; normal
completion has no poll loop. The observations above cover 0097; they do
not establish IOSurface writer compliance or improved subjective smoothness.

## 0097 offline build handoff

Native build exit 0 in **ci-lane-gpu6 only**. Patches 0096 then 0097 apply
cleanly to the integration baseline, and all sixteen resulting source inputs
match the native build tree. Loader builds for both variants pass `-Werror`
with zero warnings using clang-r547379 and the same pinned external GMU/SQE/zap
firmware as 0096e. No firmware or binaries are tracked. No phone access or push.

Validation passes shared-source sync, actual C/C++ ASan/UBSan GPU/cache tests,
full/1g memory/USB-offset tests, actual governor policy/register tests, exact
external-firmware probe models, native SCM instruction audit, linked abort
recovery records, boot-v4/Image geometry, payload/ADT memory bounds and identity.
GPU and CPU payloads differ in exactly one byte (`fuxigpu=1` versus `=0`);
command line is 0096e plus ` fuxiperf=1`. Both use the same full-RAM kernel and
ADT, pinned root extents and seven CPUs. Monitor runs must omit `--cpu-level`
and `--cpu-at` with the governor enabled; the monitor enforces this.

```sh
python3 tools/sync-fuxi-adreno-shared.py --check
python3 tools/test-fuxi-adreno.py
python3 tools/test-fuxi-memory.py
python3 tools/test-fuxi-perf.py
python3 tools/test-fuxi-scm.py --kernel xnu/out-fuxi-0097/kernel.development.fuxi.unstripped
```

| Artifact | SHA256 |
| --- | --- |
| `xnu/out-fuxi-0097/kernel.development.fuxi` | `c977e00d69b3ba3acf2b1d4d4db28df6c18d28b237fef8083a7c10c3c53ca0c1` |
| `xnu/out-fuxi-0097/kernel.development.fuxi.unstripped` | `b79d38fadc93a56b20e4a1b07a6414034902a978bdfb4ad06e481dbe211e2a98` |
| `xnu/out-fuxi-0097/fuxi.adt` | `39aacea4d7db372314bdc7928a66271b3b5cc2bd878bec4d9cfe2f5191e83533` |
| `shim/out/fuxi-0097-boot.img` | `f315e1b20dc8f26573180acaea3ba9821efb42bc419939ce082958a4fcf3b2e5` |
| `shim/out/fuxi-0097-payload.bin` | `dff125f724a7b8399a6737af8111808530302c8c2e3d7f609dda1ba7ff2e6808` |
| `shim/out/fuxi-0097-cpu-boot.img` | `06de9f3a7e3a0a7068241aa91156001091e6ae725d49717d260d20498dbb2bf0` |
| `shim/out/fuxi-0097-cpu-payload.bin` | `18456f035e036dda278dbb9df4cab98a9151251f4a763effd4b626a46ccd7d21` |

Risks requiring coordinator testing: upper-bank DMA/coherency and VM pressure,
IOSurface producer compliance, display latency/tearing, EPSS/TSENS behavior in
XNU and high-vote heating. The guard samples rather than enforcing a hardware
thermal trip. PAS/SMC hangs, MMIO interconnect hangs and failed DMA-drain risks
remain. Offline success is not evidence of improved smoothness or cooling.

# Milestone 6d — present CPU cost

Branch `codex-gpu-m6d`, based on M6c `1bc3668`. **0096e built natively and
verified offline; device performance and correctness acceptance are pending.**
This section supersedes the pending 0096d device status below. No phone access
or push; `AGENTS.md`, `NOTES.md`, and `FUXI.md` are unchanged.

## 0096d device result (coordinator)

`research-log/ios-0096d-01-gpu.txt` is private/untracked. With `fuxigpu=1`,
all SCM calls returned x0=0 on CPU0, zap loaded, TRUST=0 and self-test pixels
passed. The log reached `ready, BGRA row blits + fence, 220 MHz, polling` and
SpringBoard ran. First interval:

```text
SWEET FB: stats 600 frames in 146872 ms, copy avg 13553 us max 17555 us, 628 swaps, 28 not shown
FUXI GPU: presents GPU 597 CPU 0, GPU avg 13613 us CPU avg 0 us
```

Functionality succeeded; performance did not. CPU memcpy on the same kernel
family averages about 1250 us, max about 3000 us. The old full-screen GPU path
encoded 2400 row blits, repeated setup/fence/wait 38 times, rebuilt page tables
and did two TLB synchronizations per frame. The new code removes those repeated
operations; which component dominated on hardware remains unmeasured.

## Instrumentation and cache ownership

Every 600-frame GPU stats line now includes average **elapsed** pin/cache
lookup, physical-page lookup, SMMU table construction plus TLB sync, cache
clean, encode/submit, completion wait and unmap times. Cold lookup uses one
`pmap_find_phys` per XNU 16 KiB page; a fixed 1025-entry PFN array separates
lookup from NC table-store timing. No allocation occurs for that array.
`thread_get_runtime_self()` measures actual scheduler CPU runtime around the
cache operation, including cleanup on failure and destruction/eviction costs,
while excluding descheduled time. It is reported separately from elapsed wait.
Split and CPU-time averages use attempted GPU copies as their denominator;
`GPU avg` remains elapsed time per GPU-presented frame. Current GPU eligibility
is a single enabled layer, so successful copies and GPU frames coincide.

`SweetSurface.cpp` owns a four-entry LRU protected by a fallible `IOLock`.
The key includes the surface pointer, IOBuffer pointer, source address/length
and XNU's `getPreparationID()` physical-page-list generation. A miss retains
and prepares the descriptor once, checks its preparation ID, and maps it.
A hit checks that ID without a page walk, prepare/complete or TLB operation.
The accepted buffers are wired kernel IOBuffer allocations with default cached
user mappings (`SweetSurface` creation uses no pageable/purgeable option).
Their extra preparation stays held for the entire cached mapping lifetime.
Source/length/object/preparation changes retire the old mapping first. Slot
reuse also gets a monotonically increasing submission generation.

Each slot has a disjoint 16 MiB IOVA window, 64 MiB total, with fixed reserved
L3 storage. Physically contiguous aligned 2 MiB spans use L2 blocks; complete
aligned 64 KiB spans use the architected contiguous hint, otherwise 4 KiB
leaves. A 4 KiB SMMU granule has no 16 KiB block format. L2 entries start invalid;
release/eviction clears them and completes TLBSYNC before reuse, preserving
break-before-make. The bounded cache retains at most four backing descriptors;
it adds no surface backing allocation.

`SweetSurface::free()` retires its weak cache key before releasing pixels.
The framebuffer's retained surface covers the synchronous copy, and the cache
lock serializes destruction, eviction and submission. A failed unmap sync or
present disables the GPU and uses the existing GBIF drain/reset/SID-fault
containment. If draining fails, the descriptor AND preparation remain
quarantined until reset. CPU fallback, opt-out and startup diagnostics remain.

## Rect blits, cleaning and waiting

The panel pitch is 4320 bytes. Mesa's
[A7xx register definitions](https://chromium.googlesource.com/external/gitlab.freedesktop.org/mesa/mesa/+/6a7d3020a781a191e21c35ed9410d9da73e0de4a/src/freedreno/registers/adreno/a6xx.xml)
encode the destination pitch in 64-byte units; rounding 4320 down would corrupt
rows. The required split is **even/odd row classes**, with destination pitch
8640: two rect blits cover a normal full screen, in one submission with one
fence. Source pitches with smaller alignment use up to sixteen classes to
make both strides representable. Base alignment is handled by X phases.
Mesa's [blitter](https://chromium.googlesource.com/external/gitlab.freedesktop.org/mesa/mesa/+/63350a034b2bc4e862115b783493f151da012850/src/gallium/drivers/freedreno/a6xx/fd6_blitter.cc)
uses a 16K dimension limit; all these rects are far smaller. The normal frame
uses 97 command dwords, with at most 419 for unusual dword pitches, inside the
8192-word ring. There is one setup and one final cache-flush/fence sequence.
Multi-layer records use the CPU path to preserve one GPU submission per frame.

The CPU-rendered source is cached, so cleaning cannot be skipped. Only cache
lines intersecting copied rows are cleaned. Touching intervals are merged;
1080-pixel rows with 4352-byte pitch become one clean range because each row's
32-byte padding shares the final cache line. Narrow crops use separate ranges.
`CleanPoC_DcacheRegion_Force` avoids `flush_dcache64`'s repeated VA-to-PA walks,
page-sized barriers and invalidation. Final native disassembly confirms its
**DC CVAC** loop on this generic ARM64 target. No Apple-coherent no-op is assumed.

Completion checks the fence, rptr, wptr, idle and faults. After an immediate
check, each pending check uses `assert_wait_deadline` / `thread_block` for
250 us, with no per-frame busy-spin loop. A pending job has a 1/60-second
counter deadline and an 80-check ceiling, then permanent CPU fallback and
isolation. Scheduler delays can extend wall time; an already-completed fence
is accepted on wake. No IRQ delivery is enabled.

Saved live Fuxi DT identifies `qcom,kgsl-3d0@3d00000`, `kgsl_3d0_irq`,
`interrupts=<0 300 4>`: level-high SPI 300, GIC INTID 332. A7xx RBBM interrupt
bits are CP_RB_DONE_TS=17 and CP_CACHE_FLUSH_TS=20. The current `pe_fiq.c`
initializes timer/IPI delivery; `sleh_irq` delegates other INTIDs to the generic
CPU interrupt handler. Fuxi touch, keys and UFS use polling, and no GPU DT nub,
SPI routing/registration or GPU interrupt handler exists. This build chooses
scheduler sleeping without introducing an unverified GIC integration.
Later (2026-10-30) an opt-in path, `fuxigpuncimport` bit 26 (value 67108864),
routes this SPI to CPU 0 and lets the native ABI's WaitFence sleep until the
fence interrupt arrives. Default off. See
[GPU fence interrupt](unixnu-port.md#gpu-fence-interrupt-fuxigpuncimport-bit-26).

Present stays synchronous on the framebuffer workloop, including the sleeping
wait, before `fuxi_display_kick`. The swap API records IDs and VSYNC notifications;
it provides no producer-release fence for an asynchronous DMA ownership
contract. No early return, extra in-flight frame or deferred ID lookup is added.
The display timer can still be delayed by a slow GPU or scheduler wakeup.

The clock stays **220 MHz**. There is no split device measurement justifying a
550–680 MHz DCVS vote yet. Reconsider only if completion remains GPU-bound
while cache/CPU costs meet the target; do not mask encoding overhead with heat.

## Validation, artifacts and acceptance

Native build exit 0 in **`ci-lane-gpu6` only**. The complete patch applies to the
integration baseline; all twelve resulting files match native inputs.
ASan/UBSan tests compile the actual C submit/map/clean/sleep functions and C++
cache. They cover 1022 pixel-exact full/cropped/unaligned copies, 1000 hot
submits without lookup/TLB work, ring wraps, independent slots/generations,
2 MiB blocks/64 KiB hints, exact clean coverage, delayed completion through
three sleeps, deadline/fault fallback, LRU eviction, preparation-ID changes,
release, preparation/map failures and failed-drain quarantine. Existing ten
window/2144 page-failure startup tests, native SCM instruction audit and exact
external GMU/SQE/zap probe models pass. Models do not execute real DMA or prove
physical cache/SMMU/display coherency or scheduler latency.

```sh
python3 tools/test-fuxi-adreno.py
python3 tools/test-fuxi-adreno-cache.py
python3 tools/test-fuxi-scm.py --kernel xnu/out-fuxi-0096e/kernel.development.fuxi.unstripped
python3 tools/sync-fuxi-adreno-shared.py --check
```

Boot-v4/Image/payload layout, linked abort recovery, 768 MiB memory bounds and
exact external firmware are verified. ADT identity, root extents and seven
CPUs match integ1; only RAM reservation properties change. Command line is
**exactly integ1 plus ` fuxigpu=1`**. Binaries, firmware, logs and sidecars are
external/gitignored.

| Artifact | SHA256 |
| --- | --- |
| `xnu/out-fuxi-0096e/kernel.development.fuxi` | `a2bfd375a097f7615c024a75d9feebb056d0704bcde0d38b86cd12c266387bcd` |
| `xnu/out-fuxi-0096e/kernel.development.fuxi.unstripped` | `271d534da98f467ddbfc1bbea01beef9f75549618fdd5212c53f6abf59edbf8e` |
| `shim/out/fuxi-0096e-payload.bin` | `1f6f80abeaf95c29b015725528b2898c3ad80925ebcce80cc75b5d47c20fc216` |
| `shim/out/fuxi-0096e-boot.img` | `9cfd7a0161d29da1561dfb826f3f68e8de22d328155f3196e3da5addb78a5fda` |

Expected **shape**, with measured numbers substituted after a coordinator boot:

```text
FUXI GPU: ready, BGRA strided rects + one fence, 4 cached backings, 220 MHz, sleep/poll
SWEET FB: stats 600 frames in ... ms, copy avg ... us max ... us, ... swaps, ... not shown
FUXI GPU: presents GPU ... CPU ..., GPU avg ... us CPU avg ... us, split pin ... lookup ... map+TLB ... clean ... submit ... wait ... unmap ... us, GPU CPU-time ... us, cache hit ... miss ..., sleeps ..., 2M ... 64K ...
```

After warm-up expect almost all hits and near-zero lookup/map/unmap averages
when CA reuses its surfaces. Acceptance requires pixel correctness, no fault
fallback, **GPU CPU-time well below 1000 us** and GPU completion within a frame.
Elapsed `wait` includes sleeping and must not be read as CPU consumption.
If `clean` dominates CPU cost, investigate source cache policy; if wait dominates
elapsed time, investigate hardware bandwidth/clock using these measurements.

Risks: larger/strided rects, cached translations and block/contiguous descriptors
are new hardware paths; they require device validation. Cache cleaning may still
exceed the CPU-time target; no measured improvement is claimed for 0096e. Timer
stall/wakeup jitter, tearing on live scanout, producer ownership violations and
late DMA after a failed drain remain possible. The prior SMC-stall and hardware
abort risks remain. No 0096e device boot or thermal result is claimed.

# Milestone 6c — emit the SCM instruction in XNU

Branch `codex-gpu-m6c`, based on `e289e5f` (M6b).
**0096d built and verified offline; device acceptance is pending.** This section
supersedes the pending 0096c status and SCM behavior in the historical sections
below. No phone access or push. Protected handoff files are unchanged.

## 0096c device result and proven cause

Coordinator evidence: `research-log/ios-0096c-01-gpu.txt` (private, untracked).
With `fuxigpu=1`, 768 MiB, there was no panic. Inside XNU, clock=7 and
GMU/HFI/GX/CB/CP/fence=1 all passed. The first PAS transport query,
`is_call_available(0x02000207)`, returned x0=`0x42000601` (its function ID) and
x1=1 (its arginfo). Stage16 ended with zap=`0xf05`, clear/copy=0; GPU permanently
disabled itself and CPU presents continued through SpringBoard. The same loader
query previously returned x0=0, x1=1. That register-preservation symptom initially
suggested an unserviced TZ call; disassembly proves the instruction was absent.

`llvm-objdump-18` on the SHA256-verified 0096c unstripped kernel shows
`g5_smc_raw` inlined into `_g5_scm`. At `0xfffffe00074f8fe0..8ffc`, it correctly
sets x0=fn, x1=info, x2=a, x3=b, x4=c and x5=x6=x7=0. At
`0xfffffe00074f9000` it executes `dsb sy`, then immediately copies x0..x3 into
saved registers at `0xfffffe00074f9004..9010`. There is **no SMC and no second
DSB** between argument setup and result capture. The source was:

```c
__asm__ volatile("dsb sy; smc #0; dsb sy" /* register operands */);
```

Darwin's ARM64 assembler treats the semicolon as a comment. The same string
compiled for ELF emits all three instructions; Mach-O emits only the first DSB.
A minimal cross-compile reproduces this independently of XNU, optimization,
scheduling and hardware. The repair uses `"dsb sy\n\tsmc #0\n\tdsb sy"` and keeps
`g5_smc_raw` out of line for a stable audit symbol. In 0096d, `_g5_smc_raw`
sets all eight argument registers, executes DSB / **SMC at
`0xfffffe00074f92e0`, opcode `0xd4000003`** / DSB, and stores x0/x1 at `[x19]`
and x2/x3 at `[x19+0x10]`. Both the cross-compiled objects and final native
kernel pass a register-flow check with distinct 64-bit argument/result values.
This fixes a demonstrated missing instruction; successful TZ/PAS execution
in 0096d still requires a device run.

## Linux comparison and execution context

Linux v6.12's [`qcom_scm-smc.c`](https://github.com/torvalds/linux/blob/v6.12/drivers/firmware/qcom/qcom_scm-smc.c)
uses `ARM_SMCCC_QUIRK_QCOM_A6` as host-side metadata, **not a magic x6 argument**.
`__scm_smc_do_quirk` starts `quirk.state.a6=0`; its first SMC passes zero in x6.
On `QCOM_SCM_INTERRUPTED=1`, it resumes with x0=1 and the saved returned x6
while reloading the other arguments. Non-atomic calls serialize, handle
`WAITQ_SLEEP=2` by waiting for completion and issuing WAITQ_RESUME with the
returned call context, and retry EBUSY=-12 after 30 ms with a finite retry
counter. XNU has no WAITQ completion/IRQ infrastructure: these returned statuses
remain explicit failures, with no continuation or replay. Its sole retry is
for x0 equal to the original function ID. The original x6=0 was already correct.

The Linux ARM64 [`smccc-call.S`](https://github.com/torvalds/linux/blob/v6.12/arch/arm64/kernel/smccc-call.S)
issues `smc #0`, saves x0..x3, and copies returned x6 into the quirk structure.
The shared PAS ABI matches: standard SMC64/SIP fn, x1=arginfo, x2..x4=arguments,
x5=0 for these calls, x6 initially zero, x7=0. No HVC conduit probe was added.

Linux's [ARM64 boot contract](https://www.kernel.org/doc/html/latest/arch/arm64/booting.html)
accepts nonsecure EL1 as well as EL2. Qualcomm's SM8550 stack runs Linux under
the platform hypervisor; EL1 is not an XNU-only constraint. The
[SM8550 device tree](https://github.com/torvalds/linux/blob/v6.12/arch/arm64/boot/dts/qcom/sm8550.dtsi)
reserves hyp memory, defines the SM8550 SCM service and selects SMC for PSCI;
the downstream [Kalama VM description](https://github.com/LineageOS/android_kernel_xiaomi_sm8550-devicetrees/blob/43b5ada5654ed84c25f74789bc8f6326b7bacd20/qcom/kalama-vm.dtsi)
also exposes the Gunyah infrastructure. The local loader requires CurrentEL=EL1
at its Linux Image entry and has already demonstrated SCM via SMC there.
These facts provide no evidence for switching the SCM conduit to HVC.
MMU/caches, SCTLR/PAN/TCR and exception routing were possible context differences,
but cannot explain an instruction that never reached the binary. MMU/caches
and translation state remain live; disabling them inside XNU would be unsafe.

## XNU guard and diagnostics

For each logical SCM call the sleepable worker binds to `cpu_to_processor(0)`,
then calls `thread_block(THREAD_CONTINUE_NULL)` to force migration. Merely setting
the binding is insufficient. It saves/restores the previous binding on every
returned path. Around **each physical attempt** it saves the interrupt state,
uses `ml_set_interrupts_enabled(FALSE)`, checks the actual logical CPU via
MPIDR, calls SMC only on CPU0, captures all results, and restores the saved
state. The audited XNU API masks IRQ/FIQ/SError and prevents preemption during
this interval. An unexpected CPU skips SMC and fails closed with a clear log.
Logging and the retry delay are outside the added interrupt-masked interval.

Every attempt logs step/function/attempt before entry and actual CPU plus all
four 64-bit return registers afterward. Eight static volatile records retain
both attempts per logical call (fn, info, count, CPU and x0..x3); the existing
probe report slot retains the final result without changing its wire layout.
If x0==fn, it records/logs `not serviced`, delays 100 us, and retries once with
the original arguments. A repeated match logs `bounded retry exhausted` and
selects the existing permanent CPU fallback. Real returned errors are never
retried. There are no new allocations, HVC calls or firmware changes.

## Validation and artifacts

Native build exit0 in the existing **`ci-lane-gpu6` only**. The complete updated
0096 patch applies to the integration baseline and all ten resulting files
match the native build inputs. Shared-source check passes. The new SCM test
compiles the actual primitive for Mach-O and ELF, checks the actual final native
kernel, and models CPU migration/binding restoration, interrupt restoration,
both retained 64-bit result sets, retry-success/exhaustion, off-CPU refusal and
non-retry of INTERRUPTED/WAITQ/EBUSY/errors. ASan/UBSan passes. These tests neither
execute an SMC on the host nor emulate TZ or the native scheduler.

Existing mapping/startup tests pass all ten VA failures and 2144 page failures;
the submit model passes 1018 pixel copies, 1000 frames and 333 ring wraps.
The probe model passes with the exact external GMU/SQE/zap inputs. Firmware-enabled
loader builds without warnings. Offline checks pass boot-v4/Image/payload
geometry, 768 MiB memory bounds, native abort recovery records and exact embedded
firmware. ADT changes only dram-size and the GPU reservation property; integration
identity, seven CPUs and pinned root extents are preserved. The payload command
line is **exactly the integ1 command line plus ` fuxigpu=1`**.

```sh
python3 tools/test-fuxi-adreno.py
python3 tools/test-fuxi-scm.py --kernel xnu/out-fuxi-0096d/kernel.development.fuxi.unstripped
python3 tools/test-fuxi-gpu-probe.py --firmware "$FW/gmu_gen70200.bin" \
  --sqe "$FW/a740_sqe.fw" --zap "$ZAP/a740_zap.mbn"
llvm-objdump-18 --disassemble-symbols=_g5_smc_raw \
  xnu/out-fuxi-0096d/kernel.development.fuxi.unstripped
```

| Artifact | SHA256 |
| --- | --- |
| `xnu/out-fuxi-0096d/kernel.development.fuxi` | `ecb2e3d1322098ff7d913a58890266c4023f707a5f9abfe1d9f915d29b7e80e7` |
| `xnu/out-fuxi-0096d/kernel.development.fuxi.unstripped` | `d794daf2cf103dd7252f203d12702002f8c57c3f11ab3c94a98313110dd2a4cc` |
| `shim/out/fuxi-0096d-payload.bin` | `8e8d4e0069cc55fd7209b74b234e8641223c31a6bf2890c683b6848864fac3b3` |
| `shim/out/fuxi-0096d-boot.img` | `520e245e82ef87224e04edda72e095c72a9eb7a0eb9c861d9cf8dc4bd5b5d74f` |

Binaries, firmware, disassemblies, build/test logs and SHA256 sidecars remain
external/gitignored. Fresh-boot coordinator acceptance: expect first query
CPU0 x0=0 x1=1 (support-query x1 may still be false as in M5b), real PAS calls
x0=x1=0, then TRUST0, clear/copy PASS, `ready` and GPU present counters. If a call
returns unserviced twice, capture both full return logs and confirm continuing
CPU presents. No 0096d device success is claimed.

Risks: an entered SMC can stall indefinitely with interrupts masked; EL1 cannot
cancel it. Long returning calls can also trigger XNU interrupt-latency diagnostics.
Interrupted/WAITQ calls still fail closed. Actual TZ behavior, cache/SMMU/display
coherency and performance remain unverified in XNU beyond the 0096c CP fence.
The earlier asynchronous-abort and failed-drain/late-DMA risks remain.

# Milestone 6b — fallible XNU GPU mappings

Branch `codex-gpu-m6b`, based on M6 `8b36bdc` plus monitor fix `33d5615`.
**0096c built and checked offline; device acceptance is pending.** No phone,
adb, fastboot, USB access or push. Protected handoff files are unchanged.
The M6 section below describes the original implementation and artifacts;
this section supersedes its mapping and startup-allocation behavior.

## Device result and root cause

Coordinator reports two identical M6 boots (0096 and 0096b, `fuxigpu=1`,
768 MiB RAM). XNU registers AppleCLCD at `0xb8000000`, 1080×2400, stride 4320,
and starts all seven CPUs. About two seconds later, immediately after
`using 935 buffer headers`, it panics:
`kmem_alloc(<map>, 33554432, 0x100021): failed with 3 @vm_kern.c:178`.
Adding `mbuf_pool=64` made no difference. Private evidence is
`research-log/ios-0096b-01-boot.log`; it is not committed. No backtrace was
printed, so the caller attribution comes from the allocation size, timing and
source audit rather than a symbolized device stack.

The exact native XNU tree confirms the cause: `osfmk/arm/io_map.c` defines
`IO_MAP_SIZE (8ul << 20)`, registers `io_range` with
`KMEM_RANGE_REGISTER_STATIC`, and creates `io_submap` at KMEM startup.
After startup, `ml_io_map` in `osfmk/arm64/machine_routines.c` calls `io_map`,
which allocates from that submap with
`KMA_NOFAIL | KMA_PAGEABLE | KMA_PERMANENT` (`0x100021`).
The failed 33554432-byte request is the driver's entire 32 MiB scratch aperture,
not a buffer-header allocation. It exceeds even an empty 8 MiB IO range;
other platform mappings and the GPU's earlier MMIO mappings further reduce
headroom. This IO VA limit is fixed, independent of the 768 MiB physical RAM
window or mbuf pool. `KERN_NO_SPACE=3` panics before the old null check.

## Fix and complete 0096 allocation audit

All ten windows now use the same checked mapper, including GPU/GCC/GMU/RSCC/
SMMU/RPMh/cmd-db MMIO, display geometry and the enclosing 16 KiB zap page.
It reserves VA in `kernel_map` with
`KMA_VAONLY | KMA_KOBJECT | KMA_NOPAGEWAIT`, without NOFAIL or PERMANENT,
then enters each 16 KiB physical page using `pmap_enter_options_addr`,
RW/NX, `VM_WIMG_IO`, wired, `PMAP_OPTIONS_NOWAIT`. It checks every return.
KOBJECT reuses `kernel_object`; VAONLY allocates no new scratch backing RAM.
The kernel-page alignment and physical-overflow checks precede allocation.
A page-entry failure removes the incomplete window and frees its VA before
publishing any pointer. A mapping failure logs PA, size, error and completed
mapped bytes, calls `fg_fail`, and permanently keeps CPU presents before any
probe/MMIO/SCM operation. Completed mappings remain retained until reset.

This uses the permitted fallible mapping alternative and deliberately retains
the existing full 32 MiB scratch mapping. The shared probe clears the entire
GMU icache backing (16 MiB minus 16 KiB) and 1 MiB table pools, in addition to
firmware staging,
GMU data/HFI/log/debug, SQE/ring/rptr/fence, PAS metadata, self-test surfaces,
and source leaves. Their offsets, zeroing and ownership stay unchanged.
Success logs exactly:

```text
FUXI GPU: mapped 35127296 bytes NC in kernel_map, IO submap 0 bytes
```

That is 33.5 MiB total: 32 MiB scratch plus 1.5 MiB other windows, 2144 kernel
pages. The physical reservation still excludes scratch from both VM bounds
and ADT RAM. Mapping kernel VA does not allocate another 32 MiB of RAM.

The audit also covers the APIs behind apparent failure returns:

- `ml_io_map_with_prot`, `ml_io_map_wcomb`, and `ml_io_map_unmappable` all
  reach the same NOFAIL allocation; the unmappable variant merely chooses
  `kernel_map`. None is used. `pmap_map` panics on entry failure; it is also
  avoided. `pmap_map_high_window_bd` has exhaustion panics and installs default
  cache attributes, so it is unsuitable here.
- The original `kernel_thread_start` reaches a `Z_NOFAIL` thread allocation
  and `stack_alloc`'s `KMA_NOFAIL` stack allocation. GPU startup now uses a
  static `thread_call_data_t`, set up once and enqueued with a two-second
  deadline at kernel priority. The callback runs in sleepable worker context
  while display presents continue. It allocates neither a private thread/stack
  nor a call record; it does not use the panic-on-exhaustion internal pool of
  `thread_call_func_delayed`. There is no callback self-termination or retry.
- Tables, HFI/GMU buffers, reports, firmware and PM4 storage are fixed scratch
  or static storage. Present uses existing source leaves/ring, `pmap_find_phys`
  and cache maintenance; it allocates no VM, page tables or DMA buffers.
  The framebuffer workloop serializes presents. Accepted sources are wired
  kernel `IOBufferMemoryDescriptor`s, which auto-prepare at construction;
  present's matched `prepare`/`complete` increments/decrements that existing
  preparation. A returned prepare error selects CPU copy, and the existing
  failed-drain path retains the preparation/reference until reset.

There is no remaining driver-controlled panic-on-allocation-failure API for a
recoverable GPU resource. XNU's shared VM-map/zone and worker infrastructure
still has system-wide invariants; this change does not promise survival of
arbitrary global kernel resource exhaustion. Hardware abort containment,
asynchronous faults, SMC stalls and failed-drain DMA risks remain as in M6.

## Build, validation and artifacts

Rebuilt natively, exit 0, in the existing `ci-lane-gpu6` only. Updated 0096 applies
cleanly to the integration baseline; all ten patched files match the native
build tree. `sync-fuxi-adreno-shared.py --check` passes. The ASan/UBSan model
compiles the actual mapper, startup and failure functions and injects all 10 VA
reservation failures and all 2144 page-insertion failures, checking partial
unwind, unpublished pointers, exact accounting, permanent CPU fallback and no
hardware calls. It also checks static delayed startup, opt-out, invalid
reservation/VM bounds and duplicate-start suppression. The existing 1018
pixel-copy / 1000-frame model and exact external GMU/SQE/zap probe model pass.
These are software models, not tests of native VM exhaustion on hardware.

Packaging follows M6: extract the original integ1 ADT and command line, apply
`mkadt-fuxi.py --template ... --gpu-reserve`, run `mkpayload.py` with the rebuilt
kernel and **exactly the integ1 command line plus ` fuxigpu=1`**, then build the
loader with the same externally supplied SHA256-pinned firmware. No added
`mbuf_pool` argument. Offline verification passes boot-v4/Image/payload geometry,
768 MiB memory bounds, native linked read/write abort recovery records and
exact embedded firmware. ADT comparison changes only `dram-size` and the GPU
reservation property: identity, root extents and seven CPUs are preserved.

| Artifact | SHA256 |
| --- | --- |
| `xnu/out-fuxi-0096c/kernel.development.fuxi` | `fe3b2112313c6e9110bbc06b7dda8d8765d6a2827777ce7c4db20f7cab3612dd` |
| `xnu/out-fuxi-0096c/kernel.development.fuxi.unstripped` | `3e68c8226109164a295d60526d3c8a5d3f6b2d372b62167d92fc9c94e6fa5581` |
| `shim/out/fuxi-0096c-payload.bin` | `b38d7da9b2a25c387b1c41eb49019134be34425540e1f57bef498ea8ec4e5d08` |
| `shim/out/fuxi-0096c-boot.img` | `68432587f28360f653affbd122e2ea91596a024cf0f70354c9c681157f6ff8cf` |

All binaries, sidecars and build/test logs are gitignored. Firmware remains
external and untracked. Coordinator acceptance: fresh 0096c boot, inspect the
mapped-byte log, then the M6 stage/PAS/self-test/ready and 600-frame present
checks. A native mapping shortage should instead log its failure and
`disabled permanently`, with CPU frames continuing. No 0096c device outcome,
performance improvement or fix for the other M6 hardware risks is claimed.

# Milestone 6 — XNU GPU present copy

Implemented on `codex-gpu-m6`, based on `0f827d9`, in the self-contained kernel
patch `xnu/patches/0096-fuxi-adreno.patch`. **Built and checked offline; XNU
hardware testing is pending.** No phone, USB, adb or fastboot access; no push.
`AGENTS.md`, `NOTES.md`, and `FUXI.md` are unchanged. The coordinator's loader
M4/M5 results at the end of this document remain the hardware baseline.

## Behavior and ownership

`fuxigpu=1` opts in; omitted/zero preserves CPU presents. After AppleCLCD
registers, a kernel thread waits two seconds and runs the shared verified
clocks/SMMU/GMU/HFI/GX/SQE/CP/fence/PAS/secure-exit/scratch clear+copy sequence.
CPU frames continue during initialization. Only successful scratch validation
and live scanout geometry permit real presents. No loader GPU probe is run
before XNU. All returned failures permanently select the CPU path; no panic,
retry or resource reclamation is requested by the driver.

The source is a retained/prepared linear BGRA IOBuffer surface. XNU maps only
4KiB pages intersecting copied rows, obtains their actual physical addresses
from the16KiB kernel pmap, cleans CPU writes, and synchronizes GPU TLBs.
The destination is the existing `0xb8000000` scanout,1080×2400, pitch4320.
Independent64-byte bases and X phases preserve both pitches/crops. Fenced
batches of at most64 rows reuse the32KiB ring only after rptr/wptr/idle retire;
a full frame uses38 batches. Completion polls have an overall50ms frame
counter deadline and5000×1us iteration limit per batch. Source mappings are
removed/TLBSYNCed before descriptor completion. Other formats/memory kinds
keep CPU copying. Layer order and the display's existing timer stay intact.

Every600 frames, retain `SWEET FB: stats` and inspect
`FUXI GPU: presents GPU N CPU N, GPU avg ... us CPU avg ... us`.
Mixed-layer frames can count in both groups; no-copy frames count in neither. GPU timing includes pinning,
mapping, cache cleaning, encoding and waiting; CPU timing covers its memcpy.
No device speedup or thermal improvement is claimed yet.

A timed-out/faulted submission requests bounded GBIF client/AXI drain,
holds core reset, faults owned SID0/1 streams and resumes CPU copying. If drain
ACK fails, one prepared source/reference stays quarantined until reset.
A missing ACK leaves a possible late-scanout-write risk; this is not a proven
reset/recovery implementation. GMU/GPU power votes and all scratch remain
held. No IRQ delivery, suspend/resume, collapse, Metal or AGX interface.

## Memory and firmware

The opt-in `mkadt-fuxi.py --gpu-reserve` configuration admits only
`[0x8c0000000,0x8f0000000)` to VM:768MiB instead of1GiB. ADT dram-size,
payload geometry and both boot_args memory sizes agree. Scratch is the
contiguous32MiB `[0x8f0000000,0x8f2000000)`, mapped uncached by XNU;
the remaining224MiB above it is deliberately unused. Old1GiB payloads still
work with GPU off; enabled GPU refuses a missing reservation before MMIO.
This uses a smaller RAM span rather than assuming reserved-memory holes work.

The loader only stages externally supplied, SHA256-pinned GMU/SQE/zap files
into reserved slots starting at `0x8f1804000`, publishes a versioned header at
`0x8f1800000`, then hands off cold hardware. XNU checks the reservation against
actual VM bounds and validates sizes/FNV1a transfer checksums. PAS metadata at
`0x8f1600000` is separate, contiguous and retained after init. Zap still uses
Fuxi's `0x9b09a000` reservation. Firmware is never tracked.

The GPU SMMU maps the proven private buffers and scratch self-test surfaces,
scanout at IOVA`0x41000000` (rounded4KiB span), and sparse, read-only transient
source leaves within IOVA`0x50000000..0x51000000` (16MiB cap). There is no
identity mapping of XNU RAM. Contexts/ASIDs/stream slots are selected using the
probe's live ownership checks, not hardcoded CB numbers. No secure SID2 or
apps-SMMU changes. See `gpu-xnu-plan.md` for the full map/lifetime/failure audit.

The register/PM4/PAS implementation stays canonical in
`shim/fuxi-probe/gpu.c`, `gpu-a740.h`, and `gpu-zap.h`, with a shared handoff
header. XNU's standalone patch contains exact snapshots. The adapter/API live
in `xnu/fuxi-adreno`. Keep them synchronized with:

```sh
python3 tools/sync-fuxi-adreno-shared.py       # refresh 0096 snapshots
python3 tools/sync-fuxi-adreno-shared.py --check
```

Mapped MMIO uses a private aligned access routine with XNU abort recovery;
a narrowly scoped exception-handler addition also handles precise external
aborts in that routine. Asynchronous SError/interconnect hangs remain hardware
risks. PAS entry/return is logged; a synchronous SMC that never returns cannot
be cancelled/timed out by EL1. Each call is attempted once, with no retries or
WAITQ continuation. The timeouts cover software polling and returned errors.

## Build and artifacts

The native build used a private `ci-lane-gpu6` clone of `ci-integ`, with copied
build outputs discarded before compiling to avoid stale absolute dependencies.
No other build tree was changed. With build-host/root variables configured:

```sh
# On the native Apple Silicon build host, inside the build workspace:
cp -c -R ci-integ ci-lane-gpu6
cd ci-lane-gpu6
rm -rf native-obj build  # only the newly created lane's copied outputs
(cd xnu && patch -p1 < ../0096-fuxi-adreno.patch)
XNU_IIG_CACHE="$IIG_CACHE" "$NATIVE_BUILD_TOOL" "$PWD/xnu" > kernel-gpu6.log 2>&1
```

Use `tools/build-fuxi-native.sh` and the matching IIG cache as before. The final
native command exited0. Copy the stripped/unstripped kernel into
`xnu/out-fuxi-0096/`. To preserve integration identity, CPUs and pinned root
extents, extract its existing ADT and command line from the payload:

```sh
python3 - <<'PY'
from pathlib import Path
import struct
p = Path('shim/out/fuxi-integ1-payload.bin').read_bytes()
o, n = struct.unpack_from('<QQ', p, 32)
Path('xnu/out-fuxi-0096/integ1.adt').write_bytes(p[o:o+n])
Path('xnu/out-fuxi-0096/cmdline.txt').write_text(p[72:680].split(b'\0')[0].decode())
PY
python3 tools/mkadt-fuxi.py xnu/out-fuxi-0096/fuxi-0096.adt \
  --live-fdt "$LIVE_FDT" --template xnu/out-fuxi-0096/integ1.adt --gpu-reserve
python3 tools/mkpayload.py xnu/out-fuxi-0096/kernel.development.fuxi \
  xnu/out-fuxi-0096/fuxi-0096.adt shim/out/fuxi-0096-payload.bin \
  --cmdline "$(cat xnu/out-fuxi-0096/cmdline.txt) fuxigpu=1"
FUXI_GMU_FIRMWARE="$FW/gmu_gen70200.bin" \
FUXI_SQE_FIRMWARE="$FW/a740_sqe.fw" FUXI_GPU_ZAP_MBN="$ZAP/a740_zap.mbn" \
CLANG="$LLVM/clang" LLD="$LLVM/ld.lld" \
OBJCOPY=llvm-objcopy-18 READELF=llvm-readelf-18 NM=llvm-nm-18 \
MKBOOTIMG="$MKBOOTIMG_DIR/mkbootimg.py" \
  bash shim/fuxi-loader/build.sh shim/out/fuxi-0096-payload.bin
cp shim/out/fuxi-loader/fuxi-xnu-boot.img shim/out/fuxi-0096-boot.img
```

Kernel/image hashes and companion image configurations:

| Artifact | Embedded GPU args | SHA256 |
| --- | --- | --- |
| `xnu/out-fuxi-0096/kernel.development.fuxi` | `—` | `0ce80838a401e7870a3633c67890509cc8498ae22c07d23faf11a0339e7ee1e1` |
| `xnu/out-fuxi-0096/kernel.development.fuxi.unstripped` | `—` | `42fd34aba534d61412a3c268cf9a7d03dc19783ef3be603960e6412d969a6907` |
| `shim/out/fuxi-0096-boot.img` | `fuxigpu=1` | `bbea1f3438e475a66d2bb45be91ddeefedb19c7b24de04da59213c028dbeb946` |
| `shim/out/fuxi-0096-cpu-boot.img` | `none` | `5a5cd939707d9b9a91c1987fde038cc3e0ec981e661e3d9f1a983bdb5706ee21` |
| `shim/out/fuxi-0096-stage13-boot.img` | `fuxigpu=1 fuxigpu_stage=13` | `8fa49dfb42f20fa6f9605d5f524d9583eeefa585b06a7265155ed127cdd7b865` |
| `shim/out/fuxi-0096-stage16-boot.img` | `fuxigpu=1 fuxigpu_stage=16` | `04fb3aeb49a50d9dbd36e703d4b5f47a2c25b8e2df9e1233fc2bca9d8b8d9784` |

Images, payloads, ELF files, hash sidecars and build/test logs are ignored.
The integration ADT comparison passed: only dram-size and the GPU reservation
property changed; product identity, all root extents and seven CPUs match.
Normal monitor delegation/request49 is still required to launch XNU; the new
code does not change that host protocol. Never run a GPU probe before launch.

## Validation and device test order

Offline: native kernel link PASS; shared-source check and clean patch
application PASS; exact external GMU/SQE/zap UBSan probe model PASS; standalone,
firmware-enabled and empty-firmware loader builds PASS with zero warnings.
The M6 model compiles actual0096 functions with ASan/UBSan and checks1018
pixel-exact copies, full-panel38-batch submission, all16 base/X phases,
noncontiguous16KiB/4KiB translations, sparse permissions,1000 repeated frames,
333 ring wraps, bounds/pmap/TLBSYNC/fence/fault/access errors, bounded drain
failure/quarantine and permanent fallback. It models GPU consumption; it does
not execute firmware or validate real cache/SMMU/display behavior.

```sh
python3 tools/test-fuxi-adreno.py
python3 tools/test-fuxi-gpu-probe.py --firmware "$FW/gmu_gen70200.bin" \
  --sqe "$FW/a740_sqe.fw" --zap "$ZAP/a740_zap.mbn"
```

Coordinator acceptance, separate fresh boots in this order:

1. `fuxi-0096-cpu-boot.img`:768MiB memory/allocator stress and GUI/touch/root
   regression. No GPU stage logs; GPU0/CPU counts. Old1GiB GPU-off payload is
   still supported for a separate compatibility check.
2. `fuxi-0096-stage13-boot.img`: expect `through stage 13 PASS`, fence
   `a740f00d`, then intentional `diagnostic stop` and continuing CPU frames.
3. `fuxi-0096-stage16-boot.img`: PAS enter/return logs, real operations x0=x1=0
   despite support x1=0; `through stage 16 PASS`, TRUST0, zap1/pixels1, then
   diagnostic stop. Scratch only; no GPU scanout write yet.
4. `fuxi-0096-boot.img`: `FUXI GPU: ready`, visible full/cropped updates and
   increasing GPU counts across600+ frames. Inspect visual integrity/tearing,
   layer order, ring reuse, off/on, long idle, CPU time and heat vs CPU image.
5. Rebuild diagnostic payloads with `fuxigpu=1 fuxigpu_fail=1` (stop after
   scratch) and `fuxigpu=1 fuxigpu_fail=2` (fail immediately after first present
   doorbell). Expect `disabled permanently`, `GBIF drain ACK`, reset held,
   SID0/1 faulted, continuing CPU frames and no further GPU submissions.
   Missing ACK means source quarantine and residual late-write risk: capture
   evidence and reset, without attempting same-boot GPU reuse.

# Milestone 5b

Branch `codex-gpu-m5b`, based on M5 `fd83647`. This note supersedes M5's
support gates and report size below. Coordinator fresh-boot evidence for M5
image `ec9f0953`: stages7..13 PASS; stage16 stopped at step4, `f06`, after
availability returned x0=0/x1=1 and PAS13 support returned x0=0/x1=0.
Private decode: `research-log/stage-16m5.log` (not committed).
**Guess:** PAS13's real operations may work despite that support boolean.

M5b records both query booleans and continues, including zero/unknown x1.
It queries availability (`42000601`, arginfo1) immediately before init, setup,
and auth, with SIP-owner arguments `02000201`, `02000202`, `02000205`.
Any nonzero transport x0 still stops at the first call (`f05` availability,
`f06` support); init/setup/auth/resume also require service x1=0. No retries.
All other power, memory, secure-exit, pixel, scanout and one-shot gates remain.

Rechecked the supplied Linux MDT/SCM sources: only valid non-hash, nonempty
PT_LOADs contribute; any bit27 enables relocation. Init precedes conditional
setup with **reserved PA, ALIGN(max LOAD end,4096)-min LOAD paddr**, then segment
copy/zero/readback and auth. This MBN needs `mem_setup(13,9b09a000,1000)`;
fixed images skip setup and its availability query. The
[Linux zap path](https://github.com/torvalds/linux/blob/v6.12/drivers/gpu/drm/msm/adreno/adreno_gpu.c)
does not gate on PAS support. The
[SCM metadata contract](https://github.com/torvalds/linux/blob/v6.12/drivers/firmware/qcom/qcom_scm.c)
requires physically contiguous, 4KiB-aligned, uncached memory. Our existing
dedicated scratch retains the packed `fcc` bytes untouched through auth and
until reset, including failures; no SHM bridge or metadata-size SCM argument.

Report **v9**, signature `a7400009`: **8192 bytes, sixteen pages**, selectors
`100..10f`. Pages0..14 keep field layouts. Page14 words48..127 hold SCM1..5;
new page15 has signature/stage16 at words0/1 and SCM6..8 at words16..63.
Records retain step, fn, arginfo, arguments, full x0..x3 and in-flight marker;
setup's third argument remains page14 word16. Relocatable cold path has eight
calls; fixed path six. Decoder supports v8 and older captures unchanged.

Validation: synthetic and exact external GMU/SQE/zap UBSan models PASS;
Linux/Mesa audits PASS; false/unknown queries, every transport error at every
slot, operational service failures, metadata retention, fixed relocation and
frozen reports covered. Firmware-enabled/empty loader and standalone probe
builds have **zero warnings**, using M5's compiler, firmware environment and
unchanged integ1 payload. Boot-v4/Image/ELF checks and exact blob comparison PASS.

Image: `shim/out/lane-gpu/fuxi-gpu-m5b-boot.img` (20,897,792 bytes).
SHA256: `eaabc9a4606f03800754beb797f50503495efaed70795fa03d1c5baa78ee2616`.
Image, ELF and SHA256 sidecar are saved alongside it; firmware stays untracked.
No phone access or push. Hardware acceptance remains pending: TZ can deny,
stall or reset; inherited SCM bandwidth is unverified; successful auth alone
does not prove secure exit or working pixels. Coordinator order remains fresh
7..13 →16, inspect16/14, then optional15 in the same boot.

# Milestone 5 — Fuxi-signed zap through TrustZone PAS

Branch `codex-gpu-m5`, based on `3633e99` (M4b). This section supersedes the
hardware-pending statements and artifact in the historical M4 sections below.
No phone/USB/adb/fastboot access, no push, no XNU handoff. Hardware acceptance
of **M5 remains pending**. `AGENTS.md`, `NOTES.md`, `FUXI.md` are unchanged.

## M4b device result and M5 hypothesis

**Fact, coordinator fresh boot:** M4b image
`d5807c0de62c50359e5453814e5ee8ca835eba4f2b4e91c2b367ad35633be397`
passed stages7..13. Stage14's source-free solid clear and patterned copy both
left all65536 destination pixels at `deadbeef`. Both fences landed; the ring
was fully consumed (`rptr=wptr=156`). After100us RBBM_STATUS=1; CP_INT,
HW_FAULT, PROTECT, GFSR and defined SMMU faults were0. TRUST_CNTL remained1.
Private decoded evidence: `research-log/stage-14m4b.log`; do not commit it.

**Guess to test:** secure-mode retention explains CP memory writes succeeding
while RB/2D writes do not reach non-secure memory. TRUST1 plus absent zap is
consistent with this explanation; these observations do not prove the render
engines' access policy or rule out additional cold graphics state omissions.
A successful PAS call alone does not prove secure exit or working rendering.
M5 requires the secure packet to retire, idle, TRUST0, then both pixel tests.

## Stage16 ordering and retained gates

Stage16 is an alternate branch from13, **after stage12 CP init and the proven
stage13 fence, before any2D**. It runs 7→…→13 as needed, then PAS/secure exit,
then the existing stage14 source-free clear and independent patterned copy.
It never runs15. The stage14 report remains on page12, with its original
clear/copy/per-pixel/source/guard/CPU-comparator meaning. Direct14 remains the
M4b baseline. Direct15 keeps the14 acceptance gate and scanout semantics.
For the zap path, request **16 then15 in the same boot**, only after inspecting
16/14 acceptance. A fresh direct15 does not implicitly opt into zap.

If14 or15 was already attempted,16 refuses with `f0f`, before any SCM call or
zap-memory write. Every16 attempt, success or failure, freezes its result;
repeating16 or fetching pages issues no SCM/PM4. Any16 failure also blocks
subsequent14/15 submissions. Never try a baseline14 and16 in one boot.
The existing permanent `gpu_gmu_started` latch still refuses request`49`.
GPU_SET, power, mappings and partial authenticated state remain until reset;
there is no shutdown, collapse, recovery, retry or secure SID2 mapping.

## Source-derived sequence and memory

References are the supplied Linux6.12 tree:
`drivers/gpu/drm/msm/adreno/{adreno_gpu,a6xx_gpu,a5xx_gpu}.c`,
`drivers/soc/qcom/mdt_loader.c`, `drivers/firmware/qcom/{qcom_scm.c,`
`qcom_scm.h,qcom_scm-smc.c}`, `include/linux/soc/qcom/mdt_loader.h`,
`arch/arm64/boot/dts/qcom/sm8550.dtsi`, and the PM4 XML. Public cross-check:
[Linux6.12 SCM SMC transport](https://github.com/torvalds/linux/blob/v6.12/drivers/firmware/qcom/qcom_scm-smc.c).

The optional external `FUXI_GPU_ZAP_MBN` input is a **single-file**
`a740_zap.mbn` (12088 bytes), SHA256
`86e4c3f61d4ddbe581405f4cc496182cf385bee39aaefec61639a31d3619fd35`.
It has ELF32 LE/QDSP6 headers: first non-LOAD header segment size`94`, one
relocatable LOAD at file`1000`, p_paddr`1000`, filesz=memsz`430`, and a hash
segment at file`2000`, size`f38`. No firmware is tracked or copied into tracked
paths. The build only emits `.incbin` data into ignored `shim/out`.

| Reservation | Physical range | Use |
|---|---|---|
| PAS metadata, dedicated GPU scratch | `0x8f1600000..0x8f1601fff` | Two contiguous4KiB pages, aligned4KiB, MMU/cache off, outside .bss and all existing GPU buffers/guards |
| Fuxi zap no-map region | `0x9b09a000..0x9b09bfff` | Exact live-DT and SM8550 DTS reservation; no substitute RAM |

The scratch reservation was already checked against the loader/FDT before
GMU setup; compile-time assertions place metadata after the2D guard and inside
the32MiB scratch window. Copy/readback uses abort-contained32-bit accesses.
The parser accepts at most64 program headers/64KiB MBN, checks every file,
metadata and physical bound in64-bit arithmetic, rejects overlapping LOADs,
split/truncated inputs, sections and unaligned LOAD sizes/addresses. All parsing
and segment placement checks precede the first SMC/reserved-region write.

1. Require13 PASS, retained GPU_SET, idle rptr=wptr=11, intact fence guards,
   no safe-access or defined CP/RBBM/SMMU faults. Read TRUST and TSB status.
2. Compute Linux's `min(p_paddr)` and `ALIGN(max(p_paddr+p_memsz),4KiB)`
   over non-hash PT_LOADs; here `1000..2000`, span`1000`. Relocation is set
   if any valid LOAD has flag bit27. For relocatable images use `paddr-min`;
   for fixed images use `paddr-reserved_base`. Bound each segment against the
   computed span as `adreno_gpu.c` does, and against the reserved region.
3. Query support-call availability, then PAS13 `is_supported`. Require x0=0
   and x1!=0 for both. There is no ARM32/legacy fallback or guessed convention.
4. Zero the dedicated metadata scratch and pack **header bytes`0..93`, then
   hash bytes`2000..2f37` with no padding**: metadata size`fcc` (4044 bytes).
   Full readback precedes `init_image(13, metadata_PA)`. Keep metadata untouched
   after init because TZ/hypervisor can protect its pages. No SHM bridge calls.
5. Because this image is relocatable, `mem_setup(13,9b09a000,1000)` follows
   successful init. Fixed-address images skip this call, as Linux does.
6. Copy the LOAD to `9b09a000`, not `9b09b000`; zero only any memsz-filesz tail
   (none in this MBN), and verify every written word before auth.
7. `auth_and_reset(13)`; latch loaded only on both transport/service success.
   Never infer an existing authenticated image from TRUST or support queries.
8. Emit `CP_SET_SECURE_MODE,0` (two words), `dsb sy`, doorbell, then bounded
   rptr=wptr/idle polling with fault checks (5000×10us maximum delay budget).
   Linux's `a6xx_flush` means publish the ring and write WPTR; it does not add a
   cache-flush event or WFI packet. Read TRUST and A7xx TSB low/high after idle;
   require TRUST0 and intact guards before running14.
9. Run unchanged clear+copy and full pixel/source/guard verification. Stage16
   returns the stage14 failure code if either fails; only all acceptance checks
   and the CPU comparator make16 PASS.

PAS ID13 is decimal (`0xd`). ARM64 non-atomic SMCCC uses STD_CALL + SMC64 + owner SIP: base`42000000`,
function `(service<<8)|command`, x1=arginfo, arguments in x2..x4 (x5..x7 zero,
including QCOM_A6 initial state). Raw x0 is transport status; x1 is the service
result; x2/x3 are retained even when unused. Arginfo count occupies bits0..3;
argument types use2-bit fields starting at bit4. Init's second arg is RW=2,
so arginfo=`2|(2<<6)=82`, with **no metadata-size register argument**.

| Call | Full SMC fn ID | Arginfo | Arguments | Successful x1 |
|---|---:|---:|---|---|
| INFO is_call_available(PIL is_supported) | `42000601` | `1` | `02000207` (SIP owner included) | nonzero |
| PIL is_supported | `42000207` | `1` | `0xd` | nonzero |
| PIL init_image | `42000201` | `82` | `0xd,0x8f1600000` | 0 |
| PIL mem_setup, relocatable only | `42000202` | `3` | `0xd,0x9b09a000,0x1000` | 0 |
| PIL auth_and_reset | `42000205` | `1` | `0xd` | 0 |
| BOOT set_remote_state, owned loaded path | `4200010a` | `2` | `0,0xd` | 0 |

**Source detail:** the supplied A6xx init uses a static loaded latch and simply
returns success on later calls. The explicit `SCM_GPU_ZAP_SHADER_RESUME=0`
remote-state path is in **A5xx** in this6.12 tree. M5 retains that ABI for an
already authenticated image owned by this loader, with offline coverage;
normal16/report replays never re-enter it because GX is retained and16 is
one-shot. No automatic cold-load fallback or resume follows an SCM error.

Unlike Linux's transport retry/continuation handling, **every nonzero x0 stops**
here: `-1` generic, `-2` argument, `-3` address, `-4` unsupported, `-5` memory,
`-12` busy, `1` interrupted, `2` WAITQ_SLEEP. Linux maps the negatives to
EIO/EINVAL/EOPNOTSUPP/ENOMEM/EBUSY; M5 reports raw64-bit returns. Nonzero service
x1 on init/setup/auth/resume also stops. Software loops and call count are
bounded; EL1 cannot time out a non-returning SMC. TZ denial, XPU/interconnect
SError, a reset or a stalled call remain hardware risks. SCM clock/bandwidth setup is a platform assumption to verify: the supplied
SM8550 SCM node has no clocks but does have a crypto→DDR interconnect path;
Linux votes peak UINT_MAX during PAS. This loader retains ABL's interconnect
state and the proven GPU/GMU votes, and does not add a crypto ICC driver or
invent RPMh votes. Whether inherited crypto bandwidth suffices is unverified.

## Report v8: fifteen512-byte pages, selectors100..10e

Total7680 bytes. Signatures become`a7400008`; pages0..13 keep v7 field meanings.
Decoder still accepts legacy512-byte and v2..v7 captures. Stage16 is page14
(selector`10e`), local little-endian u32 words:

| Words | Meaning |
|---|---|
| 0..5 | signature, stage16 attempted marker, result, substep, successful cold-auth latch, issued SMC count |
| 6..11 | MBN bytes, packed metadata bytes, metadata PA low/high, zap PA, reservation bytes |
| 12..18 | min paddr, aligned max end, relocatable, LOAD count, computed span, copied bytes, zero-tail bytes |
| 19/20 | TRUST before/after (after valid at step≥11) |
| 21..24 | TSB before low/high, after low/high (after valid at step≥11) |
| 25..32 | pre rptr/wptr, secure-packet target, post rptr/wptr, idle polls, last RBBM_STATUS, frozen CP_INT after a submitted secure packet |
| 33..36 | cumulative safe-access faults, last fault PA low/high, ESR |
| 37/38 | stage14 aggregate result, path1 cold /2 owned resume |
| 39/40 | actual two secure-packet ring words |
| 41/42 | original fence guards before PAS (also verified after packet) |
| 43/44 | secure packet doorbell issued, failed SMC slot (1-based;0 none) |
| 45 | frozen CP_PROTECT_STATUS after a submitted secure packet |
| 46/47 | frozen CP_HW_FAULT / RBBM_INT_0_STATUS after a submitted secure packet |
| 48..127 | five16-word SCM records, below |

Each SCM record at `48+16*i`: local0 step,1 full fn ID,2 arginfo,3 returned
marker,4/5 arg0 low/high,6/7 arg1 low/high,8/9 raw x0 low/high,10/11 x1,
12/13 x2,14/15 x3. Mem_setup arg2 is page word16. Count/step/args are stored
and a DSB completes before entering TZ. Returned0 means in flight, so zero
return words are not mistaken for success. Records never wrap or overwrite.
On a non-returning SMC the monitor may not be able to service a report request.

Substeps:1 preconditions;2 parse;3 availability;4 PAS support;5 metadata/init;
6 mem_setup;7 LOAD/zero/readback;8 auth;9 secure packet;10 idle;11 SECVID and
guards;12 stage14;13 complete. Stage16 result0=not finished,1=PASS; its auth
latch can be1 even if a later gate fails. If CP stalls, post SECVID is unrun.

| Failure | Meaning |
|---|---|
| `f01` | idle/ring/GPU_SET/guard precondition lost |
| `f02` | zap omitted from build |
| `f03` | ELF32 header/program-header format rejected |
| `f04` | metadata/LOAD bounds, overlap, relocation or single-file check failed |
| `f05` / `f06` | support call unavailable / PAS13 unsupported, or corresponding SCM error |
| `f07` / `f08` | init_image / mem_setup SCM error |
| `f09` | metadata/LOAD/zero safe-access failure or readback mismatch |
| `f0a` | auth_and_reset SCM error |
| `f0b` / `f0c` | safe-access fault / defined CP/RBBM/SMMU fault |
| `f0d` / `f0e` | secure-packet idle timeout / TRUST still nonzero |
| `f0f` / `f10` | prior2D attempt refusal / owned resume SCM error |
| `e01..e0b` | existing stage14 failures; consult both clear/copy and final faults |

Expected zap-path rings: stage12 **0→7**, stage13 **7→11**, secure exit
**11→13**, clear **13→84**, copy **84→158**, optional15 **158→1681**.
Tables remain3→5→10; fence addresses/magics and v7 timing-validity bits stay
unchanged. Stage16 is entered after13 even though its stage14 subtest has a
lower number; base report's actual stage is restored to16 afterwards.

## Build, offline validation, artifact and coordinator order

Use section6's compiler/binutils/mkbootimg environment and unchanged integ1
payload, adding the external zap input (all paths below are placeholders):

```sh
FUXI_GMU_FIRMWARE=$FW/gmu_gen70200.bin \
FUXI_SQE_FIRMWARE=$FW/a740_sqe.fw \
FUXI_GPU_ZAP_MBN=$ZAP/a740_zap.mbn \
CLANG=$LLVM/clang LLD=$LLVM/ld.lld \
OBJCOPY=/usr/bin/llvm-objcopy-18 READELF=/usr/bin/llvm-readelf-18 \
NM=/usr/bin/llvm-nm-18 MKBOOTIMG=$MKBOOTIMG_DIR/mkbootimg.py \
bash shim/fuxi-loader/build.sh "$PAYLOAD"

python3 tools/test-fuxi-gpu-probe.py
python3 tools/test-fuxi-gpu-probe.py --mesa "$MESA" --linux-msm "$MSM" \
  --firmware "$FW/gmu_gen70200.bin" --sqe "$FW/a740_sqe.fw" \
  --zap "$ZAP/a740_zap.mbn"
```

Firmware-enabled loader, explicitly empty-firmware loader, and standalone
probe: **PASS, zero warnings**, clang r547379 / LLVM source `b718bcaf8c19`.
Loader `-Werror`, C relocation checks, link and Image guards pass. Saved ELF
has no relocations. Inspection verifies boot v4, empty ramdisk/zero padding,
exact GMU/SQE/zap bytes and unchanged integ1 payload SHA256
`800b9ea17ce18f1872c0533d5ef13f916c490eb7b1524e677fc2555378f00d66`.
Compiled SMC disassembly confirms x0..x3 are retained at full64-bit width,
arguments/arginfo reach the expected registers, and x5..x7 start at zero.

Native UBSan model (`-Wall -Wextra -Werror`) passes synthetic and exact external
GMU/SQE/Fuxi zap inputs. Coverage includes contiguous metadata packing,
relocatable and fixed-address loading (mem_setup skipped for fixed), zero-tail
copy, malformed/truncated/oversized ELF spans, support/transport/service errors
at every call, interrupted/WAITQ/busy stop behavior, abort-contained metadata/
LOAD failures, packet stall/fault/TRUST retention, prior2D refusal, missing zap,
clear/copy failure preventing15, successful16→15, frozen16 replays and owned
resume ABI. Mesa/Linux register audits and new Linux PAS ID/RW-arginfo/PM4/DT
audit pass. Fifteen-page decoding retains full return high halves and in-flight
markers; legacy v7/v6/v5 decoding and CPU=N/A validity checks pass. The model
substitutes SMC/MMIO and does not authenticate or execute any firmware.

Ignored artifact: `shim/out/lane-gpu/fuxi-gpu-m5-boot.img`, **20,897,792 bytes**.
SHA256: `ec9f0953f9a479fe91b94ec7cd5f8464c9e2636719294cbe22b77b5651b4d46e`.
Saved Image (20,889,680 bytes), ELF and SHA256 file are alongside it. Firmware
ranges in the saved ELF: GMU`16070..2658c`, SQE`2658c..38c00`,
zap`38c00..3bb38`, BSS`3c000..6c000`, payload`7c000`, end`13ec050`.
The image was saved **before** empty-firmware validation; the lane artifact
contains all three blobs. Commit: the commit containing this section.

Coordinator device test order (lane does not execute these):

1. Regress7, fresh8a ownership review,10, then fresh11→12→13. Expect the proven
   init/fence7/11, TRUST1 and no new defined faults. Preserve reserved FSR400.
2. **Fresh boot16**, or advance the successful fresh13 to16 with no14/15 in
   between. Save all15 pages with updated decoder `--stage 16 --raw --save
   <private-report>`. Require five returned SMC records, x0=0 throughout,
   support x1 nonzero and init/setup/auth x1=0; metadatafcc, span1000,
   copied430, auth1; secure packet target/rptr/wptr13, TRUST0, no faults.
   Require16 result1/step13, page12 stage14 result1/step8, validity3f,
   clear/copy rings84/158, both fences, all pixels/source/guards correct and
   CPU timing valid. Stop immediately on any failure or reset; no recovery.
3. After acceptance, **same-boot15** only; require its14 gate stays frozen and
   passing, end1681, validity39, expected geometry and GPU/CPU/border checks,
   and visual panel bars. For another cold scanout test repeat **16→15** in
   a fresh boot. Do not use a fresh direct15 as a zap-path test.
4. Repeat16/report reads after successful16; require frozen page14 and no
   additional SMC/ring work. Keep request49 refusal. On any failure retain all
   available15 pages, exact image hash/reset outcome, and stop that boot.

Risks: no M5 device result; signed firmware/TZ acceptance and high-PA metadata
protection are untested under this hypervisor. PAS/secure-mode transitions can
reset or hang the SoC. TRUST0 may still expose additional2D state/writeback
problems; clear+copy acceptance is required before scanout. Offline simulation
checks software sequencing and failure gates, not real TZ, signed code, DMA,
cache behavior, secure-SID routing or physical display output.

---

# Milestone 4b — A7xx render-thread selection and blit-cache writeback

Branch `codex-gpu-m4b`, based on `fuxi-bringup` / M4 commit `1302cee`.
This section supersedes M4's artifact, ring counts and report layout below.
**Hardware acceptance is pending.** No phone, USB, adb or fastboot access was
used for M4b, and nothing was pushed. The GMU-start handoff lock remains.

## M4 device result (fact)

Coordinator's fresh boot of M4 image SHA256
`00fc43ebbba2d0b3b01fa2e3d42a4c0a5aae9b46e9cbf1ba2a39272807041782`
ran stages7..14 together. Stages7..13 PASS, including fence `a740f00d` and
rptr7→11. Stage14 returned `e06`, step6: all65536 destination pixels remained
`deadbeef`, first x/y0/0 expected`00000000`; source and every guard were intact.
The ring was consumed, rptr=wptr74, and the following event fence `a740b014`
landed. CP_INT, HW_FAULT, PROTECT, RBBM_INT and GFSR were0; both context banks
retained only the pre-existing FSR bit10 (`400`). Frozen RBBM_STATUS was1 and
STATUS1/2/3 were0. Private decoded evidence is `research-log/stage-14m4.log`;
it is not part of the commit. This establishes CP progress without visible
2D destination writes; it does not establish a working render pipeline.

CPU=0.000us was a decoder validity bug: M4 never ran a stage14 CPU comparator,
and failed GPU verification also prevented the stage15 comparator. Zero in an
unreached timing field is **not a measured zero-duration memcpy**.

## Source comparison and candidate fixes

Primary references are the supplied **Mesa25.0.7** tree and **Linux6.12** msm
source, not a newer driver's register names. UniXNU's A740 derivation scripts,
NativeAdreno hw-init table and AdrenoPM4 builders were read as secondary
cross-checks; no implementation was copied from them.

- **Confirmed omission:** Linux `a7xx_cp_init` emits THREAD_CONTROL=`08000000`
  to disable concurrent binning. That does not select BR: the XML enumerates
  BR=1, BV=2, BOTH=3. Turnip `tu_cmd_buffer.cc:tu6_init_hw` explicitly emits
  `08000001` before graphics state. Linux `a7xx_submit` also selects BR before
  submission. M4 did neither after CP init. M4b selects **BR, binning disabled**
  at the beginning of each clear/copy stream, preserving stages1..13's sequence.
  **Hypothesis:** M4's zero thread selector allowed CP control/fence progress
  while render work/state did not reach BR. The exact retail SQE behavior for
  selector0 remains unverified; source omission alone is not a device proof.
- **Confirmed omission:** Turnip's `FD_CCU_CLEAN_BLIT_CACHE` maps through
  `common/freedreno_gpu_event.h` to A7xx **CCU_RESOLVE_CLEAN26**. Its flush code
  explicitly requests this in addition to ordinary CCU/UCHE maintenance.
  M4 emitted color/depth cleans33/32 and CACHE_CLEAN49, but no26. M4b emits26
  before work and after each job, then color/depth clean and invalidate, then
  the write-enabled CACHE_CLEAN49 fence and WFI. **Hypothesis:** undrained
  A7xx blit writes contributed to the untouched CPU-visible destination.
- M4b adds WFI after static `RB_CCU_CNTL=0`, matching Turnip's requirement that
  static A7xx CCU configuration take effect across WFI. Resolve/unresolve
  concurrency stays disabled. Linux's A7xx `RB_CMP_DBG_ECO_CNTL` bit11 ordering
  workaround is applied by safe CPU read/modify/write at stage14, before jobs.
- **Already present/correct:** UCHE TRAP/WRITE_THRU bases=`1fffffffff000`,
  A740 GMEM VA range=`1000000..12fffff`, UCHE cache/CMDQ/GBIF setup, Linux's
  A740 HBB16 RB/TPL1/SP/GRAS NC configuration, HWCG and CP_PROTECT tables.
  These are not newly guessed missing writes. Mesa `tu_device.cc` allocates
  six256KiB depth CCUs before the color cache, yielding color offset`180000`;
  `emit_rb_ccu_cntl` and XML encode full caches as `RB_CCU_CNTL2=c0000000`.
- The `r2d_setup_common/src_buffer/dst/run` comparison retains format`30`,
  UNORM8 ifmt`10`, maskf, WXYZ BGRA swap1, SP output`f180`, A7xx source at
  `b2c0`, TPL1 TEX_2D at`b2d2`, inclusive BR coordinates, no scissor/UBWC/sRGB.
  Mesa25.0.7 A7xx source pitch is bytes<<3; for aligned1024 bytes its value
  `2000` equals M4's `(pitch>>6)<<9`. Destination pitch is bytes>>6. These
  encodings were rechecked, not changed. RM6_BLIT2DSCALE was already emitted.

The two principal omissions are corrected together; a passing M4b run cannot
attribute the fix to one of them. Missing additional cold graphics state or
retail secure-mode behavior (TRUST remains1) are residual hypotheses if neither
variant writes. No secure-mode transition, zap/PAS/SCM or SID2 mapping is added.

## One stage14 run: source-free clear, then independent patterned copy

After existing mapping/idle/fault/guard gates, stage14 submits a **solid-color
2D clear** using SOLID_COLOR bit7, RB/GRAS control`10f03080`, per-channel
R/G/B/A=`33/66/99/ff`, and destination BGRA pixel`ff336699`. It emits no source
surface registers. The original read-only source mapping is retained so CPU
checks can detect any source corruption. Clear uses its own fence
`40029030=a740c014`. Every destination/source pixel and every scratch/old
fence guard is checked after fence+rptr+wptr+idle completion and a100us delay.

A clear **pixel-only mismatch** is retained while the separately planned copy
runs once; any fault, timeout, safe abort, source corruption or guard damage
stops immediately. After a clean/invalidated, idle clear, CPU refills destination
with `deadbeef` so the copy test is independent. The original full256x256 pattern
copy then uses `40029010=a740b014`. Both variants must pass before stage14 can
PASS. There is no retry, recovery or ring reuse. Stage15 remains gated on14 PASS.

| Clear / copy observation | Interpretation limit |
|---|---|
| Both untouched, both fences and idle succeed | General 2D routing/state/writeback remains suspect; source fetch alone cannot explain the clear failure |
| Clear passes, patterned copy fails | Destination write path works for solid color; investigate source fetch/format/coordinates |
| Clear fails, copy passes | Inspect solid channel packing/clear state; new result `e0b` blocks15 |
| Both pass, all guards/source checks pass | Scratch acceptance; proceed to the separately authorized scanout test |

After GPU verification, stage14 now also times256 identical1024-byte CPU row
copies to scratch; stage15 retains64 row copies to its panel region. Counter
reads have ISB/compiler barriers; final DSB stays inside the interval. This is
still the loader's uncached volatile-byte memcpy, not an optimized XNU baseline.
CPU readback follows timing. A validity bit distinguishes an executed benchmark
from an unrun field, including failed-stage14 legacy-v6 reports in the decoder.

Expected successful rings: clear **11→82** (71 words), patterned copy
**82→156** (74 words), stage15 **156→1679** (1523 words). Page tables remain
3→5→10. Primary stage14 ring/timing fields describe the copy only. Its GPU time
excludes clear,100us diagnostic delay and CPU pixel verification.

## Report v7 (same fourteen512-byte pages)

Signatures become`a7400007`; selectors100..10d and7168-byte size stay unchanged.
The host still accepts v2..v6. Existing words0..42,45..53,64..95 retain meaning,
with primary ring fields valid only if copy-submitted is set. New layout:

| Local words on page12/13 | Meaning |
|---|---|
| 43 | Validity bits0 CPU benchmark ran,1 clear submitted,2 clear completed,3 copy submitted,4 copy completed,5 delayed copy state sampled |
| 44 | Clear result (0 not run,1 PASS,`e0b` pixels wrong; other M4 failures unchanged) |
| 54..58 | Clear end rptr target, observed clear fence, mismatch count, first actual mismatch, count still`deadbeef` |
| 59/60 | RBBM_STATUS / CP_INT sampled100us after clear wait returns, including timeout |
| 61..63 | Clear guard mismatches (scratch+old fence), source mismatches, completion polls |
| 96..111 | First16 **copy** command words (v6 stored32) |
| 112/113 | RBBM_STATUS / CP_INT sampled100us after copy wait returns, including timeout |
| 114..127 | Raw CCU_CNTL, CCU_CNTL2, RB_BLIT, GRAS_BLIT, SP_FORMAT, SRC_INFO, SRC_SIZE, SRC_PITCH, DST_INFO, DST_PITCH, TPL1_BLIT, RB_CMP_DBG, RB_DBG, APERTURE_HOST |

Raw context-register reads depend on host aperture; do not mistake a banked
readback for packet decoding. Defined XML RBBM busy bits7/8/9 report RB/CCU/A2D;
no speculative RB/CCU status offsets are accessed. Frozen final fault snapshots
remain in64..95. Safe diagnostic aborts stop acceptance. A successful14 has
validity`3f`, successful15=`39` (no clear there). CPU fields are N/A until bit0.

## Offline validation, artifact and device order

Native UBSan model (`-Wall -Wextra -Werror`) passes with synthetic and exact
sheng GMU/SQE inputs, including both variant readbacks, guards, no next stage
on failure, clear-only/copy-only write loss, omitted BR, omitted resolve-clean,
fence/stall/fault/abort/TLB/geometry/ring capacity cases and frozen replay.
The model deliberately separates render-thread routing and queued blit-cache
writes from CP consumption/fences; this tests handling of those hypotheses,
**not their real SQE/cache behavior**. Mesa XML field/event audit and Linux
54-HWCG/48-protection/39-offset audit pass. v7/v6/v5 decoding passes, and the
private M4 log was reconstructed/decoded offline with CPU=N/A.

```
python3 tools/test-fuxi-gpu-probe.py
python3 tools/test-fuxi-gpu-probe.py --mesa "$MESA" --linux-msm "$MSM" \
  --firmware "$FW/gmu_gen70200.bin" --sqe "$FW/a740_sqe.fw"
```

The firmware-enabled loader, explicitly empty-firmware loader and standalone
probe build with clang-r547379: **PASS, zero warnings**. Loader enforces-Werror;
C relocation, link and Image guards pass. Saved image inspection verifies boot
v4, empty ramdisk/padding, exact GMU/SQE bytes, unchanged integ1 payload and no
linked relocations. Build environment/recipe is the M4 section6 below, with
external firmware inputs; the firmware image was saved before empty-fw checks.

Ignored image: `shim/out/lane-gpu/fuxi-gpu-m4b-boot.img`, **20,881,408 bytes**.
SHA256: `d5807c0de62c50359e5453814e5ee8ca835eba4f2b4e91c2b367ad35633be397`.
Kernel Image20,873,296 bytes. Saved ELF GMU`147c0..24cdc`, SQE`24cdc..37350`,
BSS`38000..68000`, payload`78000`, end`13e8050`; firmware/payload hashes are
unchanged from M4. Saved ELF/Image and a SHA256 file sit beside the boot image.
No firmware, private log, generated binary or local absolute input path is
committed. Commit: the commit containing this section.

Coordinator order (temporary fresh boots; lane itself must not access device):

1. Regress7,8a ownership review,10, then11→12→13. Expect the proven7/11 init/fence
   counts, TRUST1/read-only, no new defined faults; retain both raw FSR`400`s.
2. Fresh stage14 with updated decoder `--raw --save <private-report>`; inspect
   **both** clear and copy. Require result1/step8, validity3f, rings82/156,
   both magic fences, all pixel/source/guard counts0 and CPU timing valid.
   Check delayed busy/CP_INT, raw CCU state and final faults. Stop on failure.
3. Only after14 acceptance, stage15 on a fresh boot (its14 gate reruns and must
   pass), then same-boot11→12→13→14→15 with report fetches/delays. Require15
   end1679, validity39, geometry and GPU/CPU/perimeter checks, and visually
   inspect bars. A readback alone is not proof of visible scanout.
4. Replay stages/report reads without new submissions; keep request49 refusal.
   Any failure: retain all14 pages, exact image hash and reset outcome; no
   climbing, retries or recovery in that boot.

Remaining risk: the source-derived fix has **no M4b device result** yet. Cold2D
may still hang/fault or fail readback, and new safe state reads may abort.
Stage15 additionally writes active scanout; it cannot run until both scratch
variants and the CPU comparator pass. No offline PASS is a claim of stage14
hardware success or physical panel output.

---

# Lane: GPU A740 — milestone 4, 2026-10-20

Branch `codex-gpu-m4`, based on main `b6996c1`. This section supersedes earlier
handoffs below; retain their history and **Device results, milestone3**.
No phone/USB/adb/fastboot access, no push, no handoff. The GMU-start lock stays.

## Verified baseline and new device hypotheses

Coordinator M3d verified stages1..13, including SQE/CP consuming PM4 through
CB2 and CP_MEM_WRITE landing at the CPU-visible fence. TRUST remained1, and
reserved CB FSR bit10 remained report-only. M4 adds **unverified on hardware**
pixel work. The offline model executes a small interpreted PM4 subset, not
SQE firmware, real SMMU translation, cache behavior or the rendering pipeline.

Stage12 now reads SECVID/TSB only: its legacy ignored TRUST=0 fallback write is
removed to leave secure mode alone. Page6[43]=0 means no write was attempted;
[44]/[45] are two raw read samples. No secure mode packet, zap/PAS/SCM call or
SID2 mapping is added. Init/fence command sizes remain7/11 and stage11 still
uses3 page-table pages. New mappings are installed only when their stage runs.

Local Mesa was found before using external references: revision
`875160ed32cc5bc32777fa04870a0a57ed38c987`, `src/gallium/drivers/freedreno/a6xx/`
`fd6_blitter.cc`, `fd6_emit.{cc,h}`, `src/freedreno/vulkan/tu_clear_blit.cc`,
`common/fd6_gmem_cache.h`, `freedreno_devices.py`, `fdl/fd6_format_table.c`,
and `registers/adreno/{a6xx.xml,a6xx_enums.xml}`. Linux reference remains the
supplied kernel-guest msm tree. `$MESA`, `$MSM`, `$FW`, `$LLVM`, `$PAYLOAD` below
are local input placeholders, never tracked machine paths.

## Stage14: 256x256 scratch BGRA copy

Stage14 requires13 PASS, retained GPU_SET, idle CP with expected rptr/wptr,
no defined faults and intact old fence guards. It adds two NC mappings to the
existing GPU table pool, with AF/shareability and permissions matching M3,
then publishes a barrier, invalidates the owned ASID and polls TLBSYNC at most
1000x1us. Neither context control nor stream ownership is reprogrammed; faults
are not cleared. Mapping/setup failure is one-shot, with no rollback or retry.

| Surface | GPU VA | Physical address | Mapped bytes / AP |
|---|---:|---:|---|
| Source | `0x40201000` | `0x8f1401000` | `0x40000`, read-only AP3 |
| Destination | `0x40401000` | `0x8f1501000` | `0x40000`, RW AP1 |
| Panel, stage15 only | `0x41000000` | `0xb8000000` | `0x9e4000`, RW AP1 |

Both scratch surfaces have a CPU-written 4KiB guard page immediately before
and after them, outside their GPU mappings. All guards and all source/dest
pixels are checked after completion. Pattern `(x^y)<<24 | x<<16 | y<<8 |
((13*x+7*y)&255)` varies every channel, including alpha; destination starts
as `deadbeef`. Expected and actual checksums are FNV-style **u32-word** folds:
start2166136261, `(hash ^ pixel)*16777619 mod2^32` in row-major order. Counts
and first bad x/y/expected/actual are primary evidence, not checksum alone.

Submission follows fd6/Turnip's linear 2D path: WFI, RM6_BLIT2DSCALE, A7xx CCU
clean/invalidate and UCHE invalidate events, sysmem CCU configuration, matching
RB/GRAS_2D_BLIT_CNTL (format30 UNORM8, maskf), A7xx SP_2D_DST_FORMAT at `a9bf`,
SP_PS_2D_SRC_* at `b2c0` (not A6xx `b4c0`), TPL1_A2D_BLT_CNTL at `b2d2`
(TEX_2D), BGRA WXYZ swap1, nearest, linear, single sample, no UBWC/sRGB.
Mesa's corrected SP output field has float type0 and half_precision0; do not
set the older Linux XML's misleading NORM bit0. Source pitch is encoded in
64-byte units at bit9, destination pitch in64-byte units at bit0. GRAS source
coordinates are shifted8; source/dest BR coordinates are inclusive.

A740's six CCUs x256KiB depth cache put the sysmem color cache at GMEM offset
`0x180000`: RB_CCU_CNTL2=`c0000000`, full caches; RB_CCU_CNTL=0 disables
concurrent resolve. Protected RB/TPL1/SP/GRAS NC registers use safe **CPU MMIO**
with Linux's A740 HBB16 configuration, after the idle gate. They never appear
as protected PKT4 writes. The PM4 model audits every PKT4 against the existing
NORDWR protection ranges. No 3D shaders, draw state, preemption or GMEM resolve
is enabled, and no bulk unrelated Mesa 3D magic is copied. Whether this minimal
cold 2D setup suffices on retail A740 remains a device question.

Each copy emits LABEL then `CP_BLIT / BLIT_OP_SCALE`, followed by WFI. The job
ends with CCU_CLEAN_COLOR/DEPTH and A7xx `CP_EVENT_WRITE7` CACHE_CLEAN49,
WRITE_ENABLED bit27, user32 payload to its own fence. This is Linux
`a7xx_submit`'s A7xx event encoding, not A6xx timestamp bit31 or a bare
CP_MEM_WRITE. A final WFI follows. CPU completion requires expected fence,
rptr=wptr and idle RBBM, with CP/RBBM/global/both-bank faults checked every16
polls and at completion. Wait is at most5000x10us plus MMIO cost. All new MMIO
and verification reads use abort-recovering wrappers; CPU-owned scratch fills
use the existing uncached stores. Failure retains power, mappings and ring.

Expected stage14 ring: starts11, appends63 words, ends74; tables3→5;
fence `0x40029010`=`a740b014`, all65536 pixels match, no guard/source damage.

## Stage15: visible bars and CPU comparison, gated on14 PASS

Before mapping or touching scanout, safe reads check the same source pipe as
request4e: PA=`b8000000`, stride4320, size=`09600438` (1080x2400),
format=`237ff`. A mismatch/abort stops with no panel submission. Never read
MDSS/MDP top, program display registers, or issue manual START.

The region is x64..319, y256..319: 256x64 BGRA pixels /65536 bytes. Eight
32-pixel bars are red, green, blue, white, yellow, cyan, magenta, dark grey.
Source is rebuilt in the same scratch surface. The destination's4320-byte
stride is **not** a multiple of64 and cannot be encoded directly. Submit64
one-row blits: base=`align_down(panelVA+row*4320+x*4,64)`, destination x phase
=`(byteOffset&63)/4`, padded single-row pitch1088; source y selects the row
from the256-wide scratch image. Pitch is never used to advance to another
panel row. WFI serializes coordinate/base changes. The 32KiB ring is never
wrapped or reused, and worst-case capacity is checked before writing new words.

Expected stage15 ring: starts74, appends1512 words, ends1586; tables5→10;
fence `0x40029020`=`a740b015`. Compare all16384 output/source pixels before any
CPU benchmark. Compare saved pixels immediately left/right of every row and
above/below the region, plus all scratch guards and old fence guards. This is
a perimeter check, not a proof that every other framebuffer pixel is unchanged.

Only after GPU verification succeeds, time a CPU `memcpy` of the exact same
64 rows/source/target and verify the region and perimeter again. The comparator
is probe.c's **volatile byte memcpy with MMU/caches off**, not an optimized XNU
memcpy. `cntpct_el0` GPU time includes doorbell, polling, fence and idle, excludes
mapping, pattern creation, packet construction and pixel verification. CPU time
includes64 memcpy calls plus final `dsb sy`. Both are u64 ticks with CNTFRQ in
Hz; polling granularity/MMIO overhead can dominate this small job. No speedup
or physical-panel visibility is claimed offline. CPU readback cannot establish
panel scanout/tearing; coordinator checks the actual bars.

## Report v6: fourteen512-byte pages

Selectors100..10d (7168 bytes); page1/page6/extended signatures=`a7400006`.
Updated host accepts `--stage 14/15`, fetches all14 pages, and still decodes
legacy v2/v3/v4/v5 sizes. `--read-reports` copies frozen RAM only. Stage12/13's
frozen records are preserved. Pages12/13 hold one128-u32 record for14/15:

| Words (local indices) | Meaning |
|---|---|
| 0/1/2/3 | signature / attempted stage (0 absent) / result / step |
| 4..8 | width,height,source pitch,logical destination pitch,bytes copied |
| 9/10 | source/destination GPU base VA |
| 11..14 | source PA low/high, destination PA low/high |
| 15/16 | destination x/y within logical surface |
| 17/18 | event fence VA / expected magic |
| 19/20/21 | expected checksum / actual checksum / mismatch count |
| 22..25 | first bad x,y,expected,actual; x/y=`ffffffff` when none |
| 26/27 | scratch guard or panel perimeter mismatch count / old fence low guard |
| 28..30 | table pages / last TLBSTATUS / TLB polls |
| 31..37 | ring start,end,count / completion polls / rptr,wptr / fence value |
| 38..42 | GPU ticks low/high, CPU ticks low/high, CNTFRQ Hz |
| 43/44 | reserved0 |
| 45..49 | scratch guard mismatches / source pixel mismatches / CPU pixel mismatches / old fence high guard / CPU perimeter mismatches |
| 50..53 | stage15 raw display PA,stride,size,format |
| 54..63 | reserved0 |
| 64..71 | frozen rptr,wptr,RBBM_STATUS,CP_INT,CP_HW_FAULT,CP_PROTECT_STATUS,RBBM_INT,GFSR |
| 72..79 | GPU CB,FSR,FAR low/high,FSYNR0/1,CBFRSYNRA,SCTLR |
| 80..87 | GMU bank, same fields |
| 88..91 | TRUST read-only / RBBM_STATUS1/2/3 |
| 92..95 | cumulative safe access faults / last PA low/high / ESR; page6 retains FAR/op/first fault |
| 96..127 | first32 command words of this stage |

Steps:1 prerequisites,2 map/TLB,3 pattern/guards,4 render setup/encode,
5 submit/wait,6 GPU verification,7 CPU benchmark/verification,8 complete.
Unreached fields are0; checksum, counts and timing are valid only once the
corresponding step ran. Snapshot reads are sequential, not atomic. Guard/source
failures stop before CPU overwrites. Diagnostic abort converts a tentative PASS
to failure. Result1=PASS,0=not run. Shared M4 failure codes:

| Code | Meaning |
|---|---|
| e01 | safe register/memory access abort (see page6 and record92..95) |
| e02 | defined CP/RBBM/global or either-bank fault |
| e03 | live mapping TLB sync timeout |
| e04 | OOB/previous stage/ring/idle/capacity precondition lost |
| e05 | event fence/rptr/wptr/idle timeout |
| e06 | destination or source pixel mismatch |
| e07 | scratch guard, old fence guard or panel perimeter damage |
| e08 | live scanout geometry mismatch |
| e09 | page-table mapping failure |
| e0a | CPU benchmark readback/perimeter mismatch |

## Validation and coordinator order

Offline synthetic and exact sheng GMU/SQE native UBSan models pass with
`-Wall -Wextra -Werror`. The model interprets PKT4/PKT7, walks real emitted PTEs
and enforces write permissions; it copies every pixel using decoded pitches,
base alignment and inclusive coordinates. It verifies full surface copies,
panel row phases, event fences, frozen records, one-shot failure/no next stage,
corrupt destination/source/guards/perimeter, missing fence, stall, CP error,
safe-access failures, TLB timeout, geometry refusal and ring capacity refusal.
It does not emulate SQE, caches, physical addresses or real rendering. Source
audit checks54 HWCG pairs,48 protection entries and36 CP/2D offsets against
Linux, plus cold RUN order and firmware copy. v6/legacy v5 decoders pass offline.

Reproduce without a device:
```
python3 tools/test-fuxi-gpu-probe.py
python3 tools/test-fuxi-gpu-probe.py --linux-msm "$MSM" \
  --firmware "$FW/gmu_gen70200.bin" --sqe "$FW/a740_sqe.fw"
```

1. Fresh boots: regression7,8a ownership review,10; then11,12,13. On12 expect
   TRUST1 with **write_ok0**, CP init rptr7; on13 fence/rptr11, no defined faults.
2. Fresh stage14 (`--raw --save <private-report>`). Require14 result1/step8,
   ring74, event magic, checksum equality, mismatch/source/guard counts0,
   no defined CP/RBBM/SMMU faults or safe aborts. Keep both bank FSR bit10 raw.
3. Only after14 passes, fresh stage15. Require region/geometry/fence/ring1586,
   all GPU/CPU mismatch and perimeter counts0; retain raw timings and visually
   check bars. Do not infer visibility solely from CPU memory.
4. Same boot11→12→13→14→15, report fetches and delays between stages. Repeated
   stages must preserve records/no new submission. Fetch all14 pages with the
   lane decoder; an old12-page runner loses M4 evidence. Keep request49 refusal.
5. Any failure/reset: stop climbing; retain all available reports, stage/step,
   exact boot outcome and image hash. No retries/recovery/handoff in that boot.

Risk by stage:1..13 retain proven hardware sequence apart from12's removal of
the ineffective TRUST write and report format;14 first exercises the cold2D
pipeline and can hang/fault despite bounded CPU polling;15 additionally permits
GPU writes to the active framebuffer and can damage output if address/stride
hardware semantics differ. None of M4 has been tested on the phone here.

Firmware-enabled loader (`-Werror`), explicitly empty-firmware loader, and
standalone probe build with clang-r547379: PASS, **zero warnings**. C relocation,
link, Image header/size guards pass. Independent inspection verifies boot v4,
zero ramdisk/padding, exact embedded sheng GMU/SQE and unchanged integ1 payload,
and no relocations in the saved firmware ELF. Firmware hashes match prior
milestones. Payload SHA256:
`800b9ea17ce18f1872c0533d5ef13f916c490eb7b1524e677fc2555378f00d66`.

Final ignored artifact: `shim/out/lane-gpu/fuxi-gpu-m4-boot.img`, **20,881,408 B**.
SHA256:
`00fc43ebbba2d0b3b01fa2e3d42a4c0a5aae9b46e9cbf1ba2a39272807041782`.
Kernel Image:20,873,296 B. Saved firmware ELF: GMU `[0x138c8,0x23de4)` (66844 B),
SQE `[0x23de4,0x36458)` (75380 B), BSS `[0x38000,0x68000)`,
payload start`0x78000`, Image end`0x13e8050`. The lane image/ELF/Image were
saved before the empty-firmware build; the lane artifact contains both blobs.
A `.sha256` file sits beside it. No firmware, dump, ELF, boot image or local
absolute input path is committed. Commit: the commit containing this handoff.

For the post-handoff driver, see [gpu-xnu-plan.md](gpu-xnu-plan.md).

---

# Lane: GPU A740 — milestone 3c, 2026-10-20

Current branch remains `codex-gpu-m3b`, continuing merged commit `86753f3`.
This M3c handoff supersedes the M3b instructions below. No phone access.

## M3c diagnosis: cold SQE control ordering

The coordinator's fresh M3b boot (`c645f6d7...a53`) passed stages1–11, then
stage12 returned `c06`, step7, **phase1, SQE_started=0**, wptr0, polls0.
Offline decoding of the supplied private report confirms CP_INT `80`
(BR_ILLEGAL_INSTR), GPU CB2 FSR `402`, FAR0, FSYNR `b0041/0`, SID0/MID0.
All programmed pointers read back correctly; frozen SQE_CNTL is1. The CPU
payload matches the source firmware, but instruction debug words are0.

The important correction is that this failure precedes the intended final
`SQE_CNTL=1` write, not just the PM4 doorbell. M3b wrote **SQE_CNTL=3 before
hw_init and INSTR_BASE**. Linux uses3 only to halt an already-running SQE in
`a6xx_recover`; cold `hw_init` writes the base/ring/shadows first, then1.
The supplied XML does not document the control bits or latch timing, so the
precise effect of cold-writing3 is not established. The snapshot is compatible
with an early enable/reset and a latched zero-base instruction fetch; later
base readback cannot prove what address was used for that earlier fetch.

Ranked against the supplied Linux source and exact sheng firmware:

1. **Our recovery command used during cold initialization.** Strongest new
   lead: phase1/started0, only prior control write3, frozen control1, FAR0 and
   BR illegal instruction. M3c removes that write. It makes one control write,
   value1, after programming/verifying all pointers and a `dsb sy` barrier.
   This is a candidate hardware fix, not a claimed device success.
2. **Instruction state/base latched by that early control write.** Closely
   related to1. Linux prescribes no CP reset between INSTR_BASE and RUN in
   cold `hw_init`. Adding SW_RESET after the base would depart from Linux and
   disturb the experiment. M3c reads SW_RESET/MISC/CHICKEN at three checkpoints
   but performs no speculative reset or new halt sequence.
3. **Multi-section layout / missing BV or LPAC pointer.** The image has
   repeated section-like headers (table below), but Linux `a6xx_ucode_load`
   calls `adreno_fw_create_bo` unchanged for A7xx. That helper allocates
   `fw->size-4` with WC/GPU_READONLY, copies `fw->data[4:]` at BO offset0 and
   never patches section pointers. `msm_gem_vma_init` requests PAGE_SIZE IOVA
   alignment; there is no SQE-specific 64KiB rule in this path. Our IOVA and PA
   are already 64KiB aligned. No separate BV instruction-base register is
   defined in the supplied XML; cold A740 writes only the shared base830/831.
   BR, rather than BV/LPAC, reports illegal instruction. Keep the full copied
   payload and unmapped IOVA0; new full BV/AC sweeps can test this residual lead.
4. **Other missing Linux setup, firmware/platform or translation behavior.**
   The prior complete hw_init comparison remains applicable. Linux A740 does
   not cold-write CP_MISC, CP_CHICKEN, CP_CONTEXT_SWITCH_SMMU_INFO, AQE/LPAC
   instruction bases, BV ring base, or an IFPC/power-up list before RUN.
   Preemption is not enabled here. Rendering/performance setup is still omitted.
   Neither the source audit nor native model proves real GPU DMA/cache state.

Reference locations in the supplied `kernel-guest-3a7d1771d492` msm tree:
`adreno/adreno_gpu.c:554` (BO copy), `adreno/a6xx_gpu.c:780` (ucode),
`:880` (cold hw_init), `:1137` (base), `:1170` (RUN), `:1266` (recovery halt),
`:1822` (SW reset helper), `msm_gem_vma.c:137` (alignment),
`adreno/a6xx_gpu_state.h:411` (BR/BV/AC indexed window bounds), and
`registers/adreno/a6xx.xml` (register offsets). The offline audit now checks
cold ordering and firmware-copy expressions in addition to 54 HWCG pairs,
48 protection slots and 21 named offsets.

### Firmware inspection

Same sheng SQE SHA256 `6d38046654333a6a5a4ee77ab67abc24e3c0c5c68237513afe98e94da8fc9fcc`,
75380 file bytes /75376 copied bytes (`499c` dwords). File word0 is0, and
payload word1 is `0100499c`: its low24 bits equal the complete copied word
count. These are raw observations, not a newly assumed loader format.
Repeated `0174xxxx` prefixes and following words in the copied payload:

| Payload dword index | First words | Interpretation limit |
|---|---|---|
| 0 | `01740168 0100499c` | Outer prefix / total size match |
| 2 | `01740139 01002458 01013fa0 b8020000` | First section-like header |
| 24d8 | `01740139 01001b78 01000000 b8020000` | Second section-like header |
| 40d0 | `01740055 01000848 b8020000 70030007` | Third section-like header |

This supports a composite image, but does not establish which internal engine
owns each section or how it locates it. Linux does no section splitting.
The newer linux-firmware image also starts with zero plus a total-size prefix
and comparable repeated headers. M3c keeps the exact tested sheng inputs;
no firmware substitution, header reinsertion or guessed section-base writes.

### M3c report v5 and bounded diagnostics

**Use the updated host tool: 12 pages /6144 bytes, selectors100..10b.**
Page1/page6/extended signatures become `a7400005`; the decoder still accepts
v2/v3/v4 saved reports. Page6[97..108] holds up to12 raw payload prefix words,
[109] their count; [119] is checkpoint-valid mask (bits0/1/2). File word0
remains at page6[19]. Existing packet sizes stay7 /11 dwords for init/fence.

Page7 stage12 record now uses its previously reserved words34..63 for three
10-word checkpoints: OOB held before CP setup, configured before RUN, immediately
after RUN. Each is:
```
SQE_CNTL, INSTR_BASE low/high, CP_INT, RBBM_STATUS, CP_HW_FAULT,
RBBM_SW_RESET_CMD, CP_MISC_CNTL, CP_CHICKEN_DBG, GPU_CB_FSR
```
The first checkpoint has phase4 (cold entry). A defined fault there stops with
`c06`; unreadable MMIO stops with `c05`; nonzero initial SQE_CNTL stops with
new `c0c`, without taking over or resetting the engine. Pointer verification
still uses phase1, RUN/wptr0 phase2, doorbell phase3. Set phase2 immediately
before the sole RUN write; `SQE_started=1` means that write succeeded. A
checkpoint records sequential samples, not an atomic hardware snapshot.

New pages10/11 are frozen stage12/stage13 pipe diagnostics,128 u32 each:
```
[0] a7400005 [1] stage [2:3] BV/AC SQE_STAT word counts [4] MMIO abort count
[5:8] BV raw UCODE_DBG indices0..3 [9:12] AC raw UCODE_DBG indices0..3
[13:15] reserved0
[16:66] BV SQE_STAT indices0..50 [67:117] AC SQE_STAT indices0..50
[118:121] raw AQE_INSTR_BASE_0/1 low/high pairs (not enabled/programmed)
[122:124] CP_MISC / BR_CHICKEN / BV_CHICKEN
[125] APERTURE_CNTL_CD [126:127] CONTEXT_SWITCH_SMMU_INFO low/high
```
All added MMIO uses abort-recovering wrappers. New indexed sweeps stop their
window on an abort and restore selectors; each is capped at Linux's0x33 words.
Unsupported/sentinel register reads remain raw evidence. No fault clear, SMMU
resume, IOVA0 mapping, additional engine enable, or error-path retry is added.
Stage13 preserves the stage12 checkpoints; report fetches copy frozen RAM only.

### M3c coordinator test order

1. Fresh boots: stage7, stage8a ownership review, stage10 regression.
2. Fresh stage11: PASS, unchanged buffers/context,3 tables, no defined faults.
3. Fresh stage12, save **all12 pages** with the updated tool. On `c0c` or
   phase4 failure inspect OOB/pre-setup before any CP control write. On phase1
   failure compare configured/pre-RUN with OOB/pre-setup; RUN has not occurred.
   On step7/phase2 compare the immediate post-RUN checkpoint and BR/BV/AC
   PCs/status/debug words. No PM4 doorbell has occurred. Do not retry that boot.
   PASS requires result1/step6, rptr/wptr7, trust0 and no defined faults.
4. Only after12 passes, fresh stage13: fence `a740f00d`, rptr/wptr11, guards
   intact, both frozen stage records. Stop climbing on failure/reset.
5. Same-boot11->12->13 with report fetches and a delay before13; repeated13
   and `--read-reports` preserve results without new MMIO. Retain the permanent
   request49 handoff-refusal check. Device runs belong to the coordinator.

### M3c build handoff

Firmware-enabled loader, explicitly empty-firmware loader and standalone probe:
**PASS, zero warnings**, clang-r547379. Loader `-Werror`, C relocation checks,
link and Image size/header checks pass; final firmware ELF has no relocations.
Native UBSan model passes with synthetic inputs and the exact sheng GMU/SQE.
The model covers the old early-control fault sequence, sole delayed RUN,
preexisting live SQE refusal, checkpoint MMIO abort, full BV/AC sweeps and a
bounded diagnostic read abort, alongside existing ownership/HFI/fence checks.
Linux source audit and twelve-page decoder pass. The supplied private M3b
v4 report still decodes offline. These checks do not execute SQE instructions
or establish the hardware effect of control3. No phone access or M3c device run.

Artifact (gitignored), **20,881,408 B**:
`shim/out/lane-gpu/fuxi-gpu-m3c-boot.img`.
SHA256: `943bb50e54012f6c0c997e2d4322b302eb98c9eb421deb94ae578ca0313f08be`.
Kernel Image: 20,873,296 B. Independent artifact inspection verifies boot
header v4, Image geometry, zero ramdisk/padding, exact embedded GMU/SQE bytes
and unchanged integ1 payload SHA256:
`800b9ea17ce18f1872c0533d5ef13f916c490eb7b1524e677fc2555378f00d66`.

Firmware ELF geometry: `_gpu_fw_start/end=0x11534/0x21a50` (66844 B),
`_sqe_fw_start/end=0x21a50/0x340c4` (75380 B),
`_bss_start/end=0x38000/0x68000`, `_payload_start=0x78000`,
`_image_end=0x13e8050`. The image and firmware-enabled ELF were saved in the
ignored lane output before the empty-firmware verification build. The saved
lane artifact contains both exact firmware inputs. No firmware, dump, ELF,
boot image or local absolute input path is committed. The handoff commit is
the commit containing this section on `codex-gpu-m3b`.

Next action: coordinator follows the M3c test order above using the updated
12-page host tool. The leading cold-control hypothesis still needs device
confirmation.

---

# Lane: GPU A740 — milestone 3b, 2026-10-20

Branch `codex-gpu-m3b`, based on published main containing M3 `7ed3102`.
This lane uses no phone, USB, adb, fastboot or CI. The coordinator owns device
runs. `NOTES.md`, `FUXI.md` and `AGENTS.md` are untouched by explicit instruction.
The M3b handoff below supersedes M3 packet sizes, report version and test targets;
the remaining M2/M3 material is retained as implementation history.

## M3b evidence and diagnosis

**Observed on the coordinator's original M3 image**
(`12b193ecf2ae55f26fe9ca553dca496528a8abcfb9ab047f41207baa856a4cdd`):
stages1–10 PASS, fresh stage11 PASS, fresh stage12 `c06` at step4, stage13 not
run. Offline decode of the supplied private `gpu-m2/stage-12m3.bin` confirms:
GPU CB2 FSR `402`, FAR `0`, FSYNR0/1 `b0041/0`; CP rptr/wptr `0/10`,
CP_INTERRUPT_STATUS `80`, CP_HW_FAULT/protection/global FSR zero,
RBBM_STATUS `800005`, no CPU MMIO aborts. SQE CPU copy/readback completed;
this does **not** prove successful SQE instruction fetching/execution.

Source-backed interpretation (`a6xx_gpu.c`, `a6xx.xml`, `arm-smmu.h`):

- FSYNR1 low byte 0 identifies **CP**. FSYNR0: read, level1, IND=1,
  PNU=0, PTWF=0. IND is the reported instruction attribute; it does not alone
  distinguish every internal CP DMA path. CBFRSYNRA (SID) was missing in M3.
- CP_INT bit7 is **BR illegal instruction**, not the opcode/HW-fault interrupt.
  SQE_STAT[1]=0 is a sampled packet/header field, not a successful firmware PC.
- RBBM `800005` reports GPU_BUSY_IGN_AHB, CP_BUSY and CP_AHB_BUSY_CX_MASTER
  under Linux's shared XML bit definitions. It does not identify UCHE activity.
- The M3 capture has no instruction/ring base register readbacks or SQE PC;
  it cannot establish whether the first failing read preceded the doorbell.

**Ranked hypotheses, not a confirmed hardware root cause:**

1. SQE instruction fetch/startup uses a zero/stale base or stale instruction
   state. FAR0 + BR illegal instruction + IND + no rptr progress makes this
   the leading hypothesis. No incorrect instruction-base offset or reversed
   word order was found: Linux uses low word then high word at `830/831` too.
   M3b verifies both base words before RUN and captures raw UCODE debug words
   alongside the CPU firmware words. Step7 with wptr0 isolates startup faults.
2. CP_ME_INIT's enabled ordinal8 register list dereferences the supplied null
   pointer. M3 copied Linux's `TODO` sequence: mask `14b`, pointer0, length0.
   The SQE may special-case length0; the source comparison cannot prove that
   for this external firmware. M3b removes ordinal8 **and its three payload
   words**, since the probe implements no IFPC/preemption list. This is the
   candidate fix being tested, not a claim that the capture proves this reader.
   Qualcomm's [Gen7 CP init](https://github.com/qualcomm-linux/kgsl/blob/master/adreno_gen7.c)
   labels bit8 REGISTER_INIT_LIST_WITH_SPINLOCK and supplies an allocated list;
   the supplied drm/msm source instead sends zeros. No IOVA0 mapping is added.
3. Another CP path (BV/LPAC, context restore, MRB/IB) starts unexpectedly.
   Linux A740 hw_init does not supply BV/LPAC instruction/ring bases or write
   CP_CONTEXT_SWITCH_SMMU_INFO. Inventing these pointers would hide the cause.
   M3b records LPAC enable/base, BR/BV/LPAC APRIV, aperture, MRB/IB state and
   BV/LPAC PCs/headers. A fault's MID/SID plus SQE PC/header narrows the path.
4. Omitted render/performance setup or firmware/platform mismatch. Lower rank:
   no graphics work was submitted, SQE bytes match the reference blob, and no
   matching missing pointer write appears in the supplied Linux A740 path.

### Complete Linux A740 hw_init / cp_init comparison

Reference is the supplied `kernel-guest-3a7d1771d492` tree, chip `43050a01`,
ADRENO_7XX_GEN2. This tree contains XML and generated snapshot range headers,
but no generated `a6xx.xml.h`/Gen7 register define header. XML is the offset
source; A6xx-only branches must not be added to A740. The companion current
[Mesa register XML](https://chromium.googlesource.com/external/gitlab.freedesktop.org/mesa/mesa/+/61b01302910401cbb4ad25555fd4c23e88d23272/src/freedreno/registers/adreno/a6xx.xml)
also retains the same A6xx–A7xx SQE/base offsets.

| Linux writes on this A740 path | Probe comparison / disposition |
|---|---|
| GPU_SET OOB | Held through stage12/13, including failure |
| GBIF_HALT `3c45`, RBBM_GBIF_HALT `16` =0; TSB control/base/size `f803/f800/f802` =0 | Same writes; safe reads in M3b flush both halt clears |
| GMU CGC `9309..930b`; all 54 A740 HWCG entries | Same offsets/values; automated source audit PASS |
| GBIF QSB `3c03..3c06` =71620, client QoS `11` =2120212 | Same |
| UCHE GX config `e3a`=10240e0; TRAP/WRITE_THRU `e09/e07`; GMEM `e0b/e0d`=16MiB..19MiB-1; CACHE_WAYS `e17`=BIT23 | Same; 64-bit writes low then high |
| PERFCTR_GPU_BUSY_MASKED, PERFCTR_CNTL, IFPC counter select, CP_PERFCTR_CP_SEL0; GMU busy mask/power counter select/enable | Omitted performance accounting; no pointer registers |
| ROQ thresholds / MEM_POOL_SIZE / LPAC FIFO size / CP_ADDR_MODE_CNTL | **Not written by Linux on A740**; A6xx branches are excluded |
| PC_DBG_ECO_CNTL primitive FIFO threshold | A740 catalog has no prim_fifo_threshold; not written |
| CP_AHB_CNTL `98d`=1; hang `1f`=40cfffff; UCHE_CLIENT_PF `e19`=81 | Same |
| RB/TPL1/SP/GRAS UBWC NC controls; UCHE/RBBM NC `e01/534` | UCHE/RBBM same; render pipeline controls omitted (no rendering) |
| CP_PROTECT_CNTL `84f`=b; 48 A730 slots starting850, skipping45/46 | Same; automated source audit checks every encoded slot |
| UCHE_CMDQ_CONFIG `e3c`=6690e | Same |
| BR/BV/LPAC APRIV `844/ad0/b31`=6f/f/f | Same; programming/readback gate before RUN |
| RB_CMP_DBG_ECO_CNTL bit11 | Omitted rendering optimization |
| RBBM_INT_0_MASK | Probe writes0 (poll raw status, no GIC handler) |
| adreno_hw_init | Linux resets software ring pointers/shadows; probe zeroes its fixed buffers and requires reset hardware pointers |
| SQE base `830/831`, RB base `800/801`, RB_CNTL `802`=20c | Same; new pre-RUN readback gate and frozen readbacks |
| BR RPTR `804/805`, BV RPTR `a98/a99` | Same register pairs, own mapped shadow words; no separate BV ring required |
| SQE_CNTL `808`=1 | Same; probe first halts with3 as Linux recovery does; new 32x10us wptr0 fault check before submission |
| THREAD_CONTROL BIT27; ME_INIT ordinals0/1/3/6/8 with seven payload words | M3b retains0/1/3/6, omits absent list ordinal8; four payload words |
| Zap then fallback TRUST_CNTL `f400`=0 | Same no-zap fallback after CP init; no secure SID2 or SCM calls |

Reproduce the register/table audit and native UBSan model with:
```
python3 tools/test-fuxi-gpu-probe.py --linux-msm "$MSM" \
  --firmware "$FW/gmu_gen70200.bin" --sqe "$FW/a740_sqe.fw"
```
`$MSM` denotes `drivers/gpu/drm/msm` in the supplied Linux tree. This checks
54 HWCG offset/value pairs, all48 protection slots and15 critical CP offsets.
It does not execute firmware or validate GPU DMA.

### M3b behavior and report additions (v4)

Stages and one-shot/OOB/handoff locking remain unchanged. Stage12 now validates
critical pointer/control readbacks before starting SQE (`c0b`, page6[94] is
1-based failed index). After starting SQE it enters **step7**, holds wptr0 for
32 polls x10us and checks CP/RBBM/both-bank/global faults before the doorbell.
A step7 failure proves that CP_ME_INIT has not been submitted. A successful
320us startup sample does not prove no later asynchronous startup fault.
Then step4 submits **7 dwords**, expects rptr/wptr7, and the trust fallback is
still step5. Stage13 adds4 dwords and expects **11**, fence `a740f00d`, intact
guards. New init packet (no null list):
```
70970001 08000000 70c80004 0000004b 00000003 20000000 00000002
```

Reports are now **10 pages /5120 B**, selectors `100..109`.
Page1[0] and page6[0] are `a7400004`. Existing v3 fields retain their positions;
page6[80..86] contains the7 init words, [87..89] remain0; [94] is the readback
failure index, [95:96] retain GBIF_HALT/RBBM_GBIF_HALT clear readbacks.
Legacy v2/v3 saved reports still decode (including the original
M3 capture). Pages8/9 are frozen extended stage12/stage13 snapshots; stage13
copies the original pre-RUN samples from page8. Each is128 little-endian u32:
```
[0] a7400004 [1] stage [2] substep at capture
[3] phase: 1 pointer verification, 2 SQE RUN/wptr0, 3 doorbell attempted
[4] completed startup sample polls [5] last pre-doorbell CP_INT
[6] last pre-doorbell GPU FSR [7] last pre-doorbell RBBM_STATUS
[8:23] pointers/controls before RUN (indices below)
[24:39] same registers at frozen capture
[40:41] GPU/GMU GR1 CBFRSYNRA (SID in bits15:0)
[42] UCHE_CLIENT_PF [43:45] RBBM_STATUS1/2/3
[46] CP_STATUS_1 [47] CP_CP2GMU_STATUS
[48:50] IB1 base low/high, remaining [51:53] IB2 same
[54] ROQ_RB_STAT [55] ROQ_MRB_STAT [56:58] MRB base low/high, remaining
[59] BV_HW_FAULT [60] BV_PROTECT_STATUS [61] cumulative safe MMIO abort count
[62] reserved0 [63] SQE_STAT words sampled
[64:114] BR SQE_STAT indices0..50 (Linux a7xx_indexed_reglist length33 hex)
[115:116] BV SQE_STAT indices0/1 [117:118] LPAC indices0/1 [119] reserved0
[120:123] raw CP_SQE_UCODE_DBG words0..3 (interpretation not assumed)
[124:127] CPU SQE buffer words0..3
```
Pointer/control indices0..15 (page6[94] uses index+1):
`SQElo, SQEhi, RBlo, RBhi, RB_CNTL, RPTRlo, RPTRhi, BV_RPTRlo, BV_RPTRhi,
BR_APRIV, BV_APRIV, LPAC_APRIV, APERTURE_HOST, LPAC_SQE_CNTL, LPAC_SQElo,
LPAC_SQEhi`. Only indices0..12 are compared to programmed/reset expectations;
LPAC state is evidence, not an invented enable/reset requirement.

All added MMIO uses safe read/write wrappers. SQE index sweeps are bounded;
selectors return to known indices afterwards. No fault is cleared/resumed by
diagnostics and page fetches still copy frozen RAM only. On error, first/last
CPU abort addresses remain in page6; the extended count includes the entire
snapshot. A diagnostic abort after an otherwise successful stage still fails
that stage. No IOVA0 mapping or diagnostic dummy page is enabled or added.
FSYNR1 low-byte MID follows Linux's A7xx client decoder (CP, CCU, flag cache,
CDP, GMU, UCHE BR/BV/LPAC using UCHE_CLIENT_PF); stale bank syndromes without a
defined FSR fault are not treated as a valid fault client. RBBM bit names use
Linux's shared XML definition; raw words remain authoritative.

### M3b verification and coordinator test order

Build results, exact image hash and geometry are recorded at the end of this
file. No M3b device result exists. Model additions cover dropped SQE-base
programming (RUN/doorbell refused), the exact observed fault signature injected
at SQE start (step7, no doorbell), all51 SQE indices, and existing fence,
timeout, ownership, abort and one-shot failure paths. These are CPU-side model
checks, not confirmation of the candidate CP_INIT fix.

1. Fresh boots: stage7, stage8a (review ownership), stage10 regression.
2. Fresh stage11: unchanged expected context/mappings,3 tables, defined faults0.
3. Fresh stage12: save all10 pages. If `c0b`, use page6[94] and page8[8..23].
   If step7/phase2, the fault precedes PM4: inspect MID/SID, base readbacks,
   BR SQE PC/header, UCODE debug and CPU words. Do not blame the init list.
   If step4/phase3, inspect BR PC/header/MRB/IB and fault attributes; compare
   against this exact SQE blob. PASS requires rptr/wptr7, step6, trust0, no faults.
4. Fresh stage13 only after12 passes: expect fence magic, rptr/wptr11, guards
   intact, both frozen records. Stop climbing on any failure/reset.
5. Same boot11->12->13 with report fetches and a delay before13; repeat13 and
   `--read-reports` must preserve frozen results without new hardware access.
   Keep the permanent request0x49 refusal check. Coordinator performs temporary
   boots only; this lane performs no device operation.

## M2/M3 implementation history

Stages 1–7 passed on Fuxi (coordinator runs gpu-01, gpu-03..07): CX on,
PLL1 locked after 6 polls, GMU reset=1, TCM retention=0, RSC idle, HFI not ready.
SMMU ID0/1/2 = `0x4c017e18 / 0x40000016 / 0x5511` (24 SMRs, 22 CBs,
4K register pages, CB base +0x20000, 36-bit OAS); sCR0 `0x002f0406`.
These are observations copied from the handoff, not new device results.

Reference: the supplied Linux msm tree under
`linux-6.12/drivers/gpu/drm/msm/`,
`adreno/a6xx_gmu.c` (memory_probe/fw_load/fw_start/start/rpmh_init),
`a6xx_hfi.c` + `.h` (queue layouts, messages, ARC votes, A740 BW table),
`a6xx_gpu.c` (OOB lifetime), `registers/adreno/a6xx_gmu.xml`, plus
`drivers/iommu/arm/arm-smmu/arm-smmu.{c,h}` and `drivers/soc/qcom/cmd-db.c`.
The supplied source has A740 support beyond vanilla upstream v6.12; its local
register definitions are authoritative for this port. Upstream comparison:
[Linux v6.12 GMU](https://github.com/torvalds/linux/blob/v6.12/drivers/gpu/drm/msm/adreno/a6xx_gmu.c).
UniXNU-sheng is a secondary register cross-check, not a device result for Fuxi.

## 1. Fuxi-verified GPU facts (facts vs. guesses)

Source of "live" facts: `research/device/probe-09/live.dtb` (the booted Fuxi
DTB) read with `tools/fuxi-inspect-dtb.py`. Register maths: pinned Linux
`42f3b40c`. "sheng" = the reference tablet; differences are called out.

### Identity / memory map (FACT, live DT)
| Item | Value | Source |
|------|-------|--------|
| GPU node | `qcom,kgsl-3d0@3d00000`, compatible `qcom,adreno-gpu-gen7-2-1` (A740 v2) | live DT |
| chip_id | `0x43050a01` ("C510v2") | live DT + a6xx_catalog.c (same as sheng) |
| GPU reg window | `0x03d00000` size `0x40000` | live DT |
| gpucc | `0x03d90000` size `0xa000` (qcom,kalama-gpucc) | live DT |
| GCC | `0x00100000` (kalama-gcc-v2); gpu branch page `0x171000`, gpll0 vote `0x152000` | live DT + gcc-kalama.c |
| adreno SMMU | `kgsl-smmu@3da0000` size `0x40000`, `qcom,qsmmu-v500`+`qcom,adreno-smmu`, dma-coherent, 3-lvl | live DT |
| GMU node | `qcom,gmu`, reg `gmu`=`0x3d68000/0x37000`, `gmu_pdc`=`0xb280000/0x10000`, `gmu_ao_blk_dec0`=`0x3d40000/0x10000` | live DT |
| GMU core base used by probe | `0x3d6a000` (upstream/sheng); Fuxi's `0x3d68000/0x37000` node **contains** it — same registers, same absolute addresses | note |
| rscc | `0x3d50000`; cx_misc/cx_mem `0x3d9e000` | live DT / sheng |
| cmd-db reserved region | `aop_cmd_db_region@81c60000` reg `0x81c60000` size `0x20000`, `no-map` | **live DT (FACT)** — matches sheng `0x81c60000/0x20000` |
| zap reserved region | `gpu_micro_code_region@9b09a000` size `0x2000`, `no-map` | live DT (same as sheng) |

### SMMU stream IDs / banks (FACT, live DT)
- `kgsl-iommu` context banks: `gfx3d_user` SID 0, `gfx3d_lpac` SID 1,
  `gfx3d_secure` SID 2 (mask 0). GMU: `iommus = <… SID 5>`.
- `qcom,actlr = <0 0x3ff 0x32b>`; QTB `gpu_qtb@3de8000` stream-id-range `0 0x400`.
- Driver-chosen (from sheng, GUESS-but-matches-arm-smmu-qcom): GPU uses **bank 0,
  ASID 2**, SMR slots 0/1 → SIDs 0/1; GMU uses **bank 1, ASID 1, SMR slot 2 → SID 5**.
  This was a reference assumption, not a device observation. Milestone 2
  selects an unused disabled bank, a free SMR and an unused ASID from stage 8a;
  bank 0 is deliberately left for the GPU. Existing SID5 matches cause refusal.

### IRQs / clocks (FACT, live DT)
- GPU IRQ SPI 300 level. GMU: hfi SPI 304, gmu SPI 305.
- gpucc GDSCs: `gpu_cc_cx_gdsc` gdscr `0x9108` (gds_hw_ctrl `0x953c`, VOTABLE,
  RETAIN_FF), `gpu_cc_gx_gdsc` gdscr `0x905c` (owned by the GMU — never touched
  by CPU here). Branch/PLL/RCG offsets confirmed against gpucc-sm8550.c:
  ahb `0x911c`, cx_gmu `0x913c`, hlos smmu vote `0x7000`, cxo_aon `0x9004`,
  demet `0x900c`, cxo `0x9144`, hub_cx_int `0x9148`, gmu RCG `0x9318`,
  hub RCG `0x93ec`, PLL0 `0x0`, PLL1 `0x1000`.
- GCC gpu branches (page `0x71000`): cfg_ahb `+0x004` (critical, hwcg), memnoc
  `+0x010`, snoc_dvm `+0x018`, ddrss_gpu_axi `+0x154`; gpll0 votes in `0x52000` bits 15/16.

### Power levels (FACT, live DT `gpu-pwrlevels-0`, initial level 8 = 220 MHz)
9 levels, freq/level(ARC): 719.6 MHz/0xe0, 680/0xc0, 615/0x90, 550/0x80,
475/0x50, 401/0x40, 348/0x3c, 295/0x38 (corrected 2026-10-20 from live DT; bin not yet confirmed), 220/0x34. GMU opp table: 500 MHz/SVS,
200 MHz/LOW_SVS. gmu-freq-table: 220 MHz vote 0x40, 550 MHz vote 0x80.
(Upstream sm8550.dtsi GPU opp table differs slightly in top bins; Fuxi's own
`gpu-pwrlevels-0` above is authoritative and is what the perf table must use.)

### Firmware (stored, NOT committed — under `research/firmware/gpu/`, gitignored)
| File | SHA256 | Bytes | Source |
|------|--------|-------|--------|
| a740_sqe.fw (sheng, **default**) | `6d38046654333a6a5a4ee77ab67abc24e3c0c5c68237513afe98e94da8fc9fcc` | 75380 | github ianchb/sheng-firmware master |
| gmu_gen70200.bin | `1a2a419c39046d3141fc5fed5aa7f971de2db40cc7a1d89693c3e26fad64dd98` | 66844 | ianchb/sheng-firmware |
| a740_sqe.fw (linux-firmware, newer) | `96fee336424b139100fc60b5b45a907360e4b3936d7e1d00406b9bd80ca48473` | 76724 | gitlab kernel-firmware main `30979b11` |
| gmu_gen70200.bin (linux-firmware) | identical to sheng | 66844 | " |

- SQE is copied into the ring's SQE BO **without its first dword**
  (`adreno_fw_create_bo`). GMU fw is the block format `{addr,size,type,value}`
  + payload: ITCM `{0,0x4000}`, icache `{0x4000,0xffc000}`, DTCM
  `{0x10004000,0x4000}`; the file is 11 zero-size prealloc headers + 4 data blocks (ITCM 0x0 0x1dc, icache 0x4000 0x10210, DTCM 0x10007f98 0x28, 0x10007fdc 0x18); prealloc needs the whole icache window backed (corrected 2026-10-20).
- Fuxi's own vendor blobs (`a740_sqe.fw`, `gmu_gen70200.bin`, and the signed
  `a740_zap.*`) are **not available locally** — no Fuxi vendor image was
  extracted. Any future zap step would need a **Fuxi-signed** zap; sheng
  reached the desktop *without* the zap (`sheng-gpu-zap=0`), so milestone 1 and
  the first userspace attempt can skip it. GUESS: Fuxi's SQE/GMU blobs are
  byte-identical to the generic a740 ones (same chip_id), so the sheng/
  linux-firmware copies are expected to work; confirm by SHA against a Fuxi
  vendor image when one is dumped.

### Differences from sheng
- GMU DT node base `0x3d68000/0x37000` (Fuxi) vs `0x3d6a000/0x35000`
  (sheng/upstream) — same block, Fuxi's starts 0x2000 lower; absolute register
  addresses are identical, so the `dword*4 - 0x6a000` offset math is unchanged.
- cmd-db region, zap region, chip_id, SIDs: identical to sheng (confirmed FACT).
- Fuxi `gpu-pwrlevels-0` is the downstream 9-level table (above); build the HFI
  perf table from it, not from the upstream dtsi opp table.

---

## 2. Stages and USB interface

GPU code stays in `shim/fuxi-probe/gpu.c`, included by `probe.c`. The dispatch
changes only add page selection and the XNU handoff guard. Loader MMU/caches
are off, VA==PA. CPU accesses are uncached; every publication has barriers.
New/possibly secure MMIO reads use `probe_safe_rd32`. M3 also uses
`probe_safe_wr32` for every new MMIO write. Both recover local synchronous
aborts and record ESR/FAR; interconnect hangs or asynchronous SError can still
reset the SoC, and a software timeout cannot contain them.

| Stage / wIndex | Operation | Risk / gating |
|---|---|---|
| 1 | Existing gated GCC/GPUCC/SMMU/GPU snapshot | Read-only, verified |
| 2 | Enable GCC cfg_ahb | Low, verified |
| 3 | CX GDSC + AHB/SMMU vote clocks | Medium, verified |
| 4 | Powered SMMU globals + CB0/1 | Read-only, verified |
| 5 | GMU clock/reset snapshot | Read-only, verified |
| 6 | PLL/GMU/hub clocks, cold prerequisites | Medium, verified |
| 7 | cmd-db snapshot/resource lookup | Read-only, verified |
| **8a / 0x008a** | Snapshot all 24 SMR/S2CR pairs and all 22 CBs with CX on | Low additional risk: new register reads; preceding 2/3/6 still write |
| **8** | Validate firmware/prealloc extents, select unused context/slot/ASID, build 4K tables in fixed RAM, map SID5, FAL veto + TCM retention, load/read back ITCM/DTCM/icache | High: first SMMU programming and about 17 MiB of scratch writes; no CM3 start |
| **9** | Build cmd-db ARC votes, RSC/PDC init, HFI table, cold GMU config/start, init wait, HFI ready, PERF -> BW -> CORE_FW_START -> START ACKs | High: firmware runs and accesses RAM; permanent handoff lock |
| **10** | Blocking low-frequency vote, GPU_SET OOB, wait GX, read RBBM_STATUS, release OOB | High, device-verified |
| **11** | Snapshot/live ownership checks; separate GPU bank/ASID, SMRs for SIDs0/1, GPU page tables and buffers | High: new SMMU context, no SQE start; requires10 PASS |
| **12** | Hold GPU_SET, A740 CP/UCHE/GBIF/HWCG setup, SQE fetch, a7xx CP init, no-zap TRUST_CNTL fallback | Highest: first GPU DMA/CP execution and potentially protected writes; requires11 PASS |
| **13** | Append CP_MEM_WRITE, poll CPU fence, rptr/wptr, idle and faults | High: GPU write confined by mappings to scratch; requires12 PASS |

Request: type `0xc0`, request `0x56`, wValue `0x4753`. Stages1..13 and `0x8a`
run through the requested limit and return page0 (512 bytes). Invalid indices
stall. **Stage8a requires stage7 valid**, and 8–13 require its powered snapshot.
A failed stage never advances. After 8a the snapshot is frozen; each mutating
stage8..13 is attempted at most once. Later limits may advance the sequence;
repeating a failed stage requires a fresh boot. Requests for stages1..7 after
8a return the existing snapshot, avoiding PLL/TCM reinitialization.

Read-only report fetches: wIndex **0x0100..0x0107** select pages0..7. They only
copy RAM, do not run stages or refresh MMIO. All unused words are zero. Snapshot
pages2..5 are from immediately before the first stage8 write. Page1 live fields
are sampled at the end of each stage8a/8..13 request, not on page fetch.

**XNU request 0x49 is refused (USB stall) from immediately before CM3 reset is
released onward, including init timeout, ACK failure and subsequent requests.**
There is no unload/reset operation that clears this latch. Retain tables and
buffers until the normal loader reset. M3 adds GPU user SIDs, SQE, a fixed ring and a single fence command. No zap
shader, secure command submission, rendering or XNU GPU driver is included.
After stage12 obtains GPU_SET it is retained until reset, including errors: no
IFPC/power collapse is allowed to destroy CP state between stage12 and13.
Diagnostic snapshots are frozen at stage12/13 completion (or failure while GX
is confirmed on); report-page fetches never touch hardware.

Timeouts: TLB sync 1000 polls × 1 us; firmware/HFI/GX init each 1000 × 10 us;
each HFI ACK has one 50 ms counter deadline, including all receive handling.
MMIO overhead adds elapsed time; all loops remain bounded. Firmware is validated
in full before any buffer/context write, and every data dword is read back.
Malformed responses, unsolicited ACKs, firmware errors and queue corruption stop
startup rather than being drained without a bound.

## 3. Fixed loader-only DRAM ownership

Reserve **[0x8f0000000, 0x8f2000000)** (32 MiB), inside the already proven
`[0x8c0000000, 0x900000000)` XNU RAM window. The loader has not copied XNU into
this window while these probes run. The runtime checks this reservation against
`_head.._end` (including embedded payload/stacks/USB state) and the incoming FDT
before writing it. No storage or partition operation is involved. It is a fixed
loader scratch reservation, **not a Linux DT reserved-memory allocation**.

| Use | GMU VA | Physical base | Bytes |
|---|---:|---:|---:|
| SMMU table pool | not mapped | 0x8f0000000 | 0x100000 reserved; 13 pages used |
| icache (privileged RW/execute) | 0x00004000 | 0x8f0100000 | 0xffc000 |
| dummy (privileged RW) | 0x60000000 | 0x8f1100000 | 0x2000 |
| log (RW, unprivileged allowed) | 0x60002000 | 0x8f1102000 | 0x4000 |
| HFI (RW, unprivileged allowed) | 0x60006000 | 0x8f1106000 | 0x4000 |
| debug (privileged RW) | 0x60400000 | 0x8f110a000 | 0x7000 |

Unmapped holes stay unmapped. No large `.bss` buffers are added (the linker puts
`.bss` before `.payload`). The existing 128 KiB cmd-db copy remains in `.bss`.
Three-level 4K tables cover 32-bit IOVA/36-bit PA. Leaves use normal noncacheable
MAIR index0=0x44 with inner sharing and AF; walks are noncacheable/outer shared.
CBAR=0x1f300 (stage1 translation, stage2 bypass), CBA2R=1,
TCR2=0x38011, TCR=0x80802020, MAIR0=0xff44, SCTLR=0x1067.
Readback permits only the reference-observed TCR2 TBI bits5/6 reading one and
SCTLR ASIDPNE bit12 reading zero; all other configured bits must match.
TTBR0 = root PA | ASID<<48; TTBR1 disabled. TLBASID + bounded TLBSYNC precedes
M=1, then S2CR translate/default privilege and exact SMR SID5 are published.
GR0 and all unrelated banks/streams are preserved. Bank0 is excluded; the first
unreferenced disabled stage1 bank (starting at1), invalid SMR and unused nonzero
ASID are selected. Any valid SMR whose mask matches SID5 causes refusal.
The ID0 ATOSNS bit is set: no SMMU hardware translation-op test is attempted.
The actual DMA translation proof must come from firmware boot/HFI on the phone.

Firmware matches Linux `a6xx_gmu_fw_load`: 11 zero-size prealloc headers and
4 data blocks, ITCM 0/0x1dc, icache 0x4000/0x10210, DTCM
0x10007f98/0x28 and 0x10007fdc/0x18. Prealloc extents inside ITCM/DTCM/icache/
dummy are checked but do not copy payload. Total data bytes=0x1042c (66604).
No firmware bytes are tracked or copied into tracked paths.

## 4. New report word map

Each page has 128 little-endian u32. Page0 preserves every old word (below),
adding **[2] latest stage entered**, **[3] permanent GMU-start latch**.
Stage8a is encoded as 0x8a in page0[1]/[2].

Page1 (selector 0x101), indices local to that page:
```
[0] 0xa7400003 (report format v3; existing fields unchanged)
[1] snapshot status (1 complete) [2] stage8 result [3] stage9 result
[4] GMU-start/handoff-lock latch
[5] SMR count [6] CB count [7] register page stride [8] CB region offset
[9] pre-stage8 GFSR
[10] chosen SMR slot [11] CB [12] ASID [13:14] table PA low/high
[15] context programmed (configuration issued; not itself success)
[16] last TLBSTATUS [17] firmware bytes [18] data block count
[19] zero-size/prealloc count [20] data bytes [21] bad address [22] bad size
[23] CBAR [24] CBA2R [25] TCR2 [26] TCR [27:28] TTBR0 low/high
[29] MAIR0 [30] SCTLR [31] published SMR [32] published S2CR
[33] table pages used
[34] DTCM firmware version word [35] expected init result
[36] observed FW_INIT_RESULT [37] HFI_CTRL_STATUS [38] HFI_VERSION_INFO
[40:43] PERF_TABLE ACK record
[44:47] BW_TABLE ACK record
[48:51] CORE_FW_START ACK record
[52:55] START ACK record
[56:59] GX_BW_PERF_VOTE ACK record (stage10)
  each: sent header / response header / returned command header / error
[60] stage10 result [61] OOB interrupt sample [62] SPTPRAC status
[63] GX GDSCR [64] RBBM_STATUS [65] RBBM read valid
[84] GMU2HOST_INTR_INFO [85] AO_HOST_INTERRUPT_STATUS [86] AHB_FENCE_STATUS
[87] live global SMMU GFSR [88] chosen CB FSR [89:90] FAR low/high
[91:92] FSYNR0/1
[93:94] command queue read/write [95:96] response queue read/write
[100:109] GPU ARC votes, OFF then 220/295/348/401/475/550/615/680/719 MHz
[110:112] GMU ARC votes, OFF/200/500 MHz
```

Page2 (0x102): `[2*i] SMR(i), [2*i+1] S2CR(i)`, i<count (Fuxi24,
geometry limit32). Additional context controls: `[64+b] ACTLR(b)` and
`[88+b] CONTEXTIDR(b)`, b<CB count. ACTLR is observed and preserved.
Snapshot fault accounting lives in **page2[48..52]**, not page1 (where those
indices hold HFI ACKs): count, first ESR, first SMMU byte offset, stream-slot
fault mask, bank fault mask.

Pages3..5 (0x103..105): eight banks per page; bank b starts at
`page=3+b/8, word=16*(b%8)`, with these 16 fields:
```
CBAR, CBA2R, SCTLR, TCR2, TTBR0lo, TTBR0hi, TTBR1lo, TTBR1hi,
TCR, MAIR0, MAIR1, FSR, FARlo, FARhi, FSYNR0, FSYNR1
```

Healthy expectations for stages8–10 (now confirmed except FSR; see Device results):
- 8a: page1[1]=1, [5:8]=24/22/0x1000/0x20000. Review **all** streams and
  enabled banks before the first stage8 run. Existing SID5 match means stop.
- 8: [2]=1, firmware 66844 B/4 blocks/11 prealloc/66604 data B, 13 table pages;
  SMR=0x80000005, S2CR=selected CB, TTBR=0x8f0000000 | ASID<<48,
  TCR etc as above; chosen FSR has no defined fault bits. GMU latch remains zero.
- 9: [3]=1, [4]=1. Init is `0xbabeface` for DTCM version <=0x20010004,
  otherwise `(result & 0x1ff)==0x100`. HFI status bit0=1. Four ACKs must have
  id126 **or echoed command id**, type1, exact returned command headers and error0. Sent headers are
  `0x00103b04 / 0x0020a003 / 0x0030020e / 0x0040010a`.
- 10: [60]=1, vote ACK error0 (`sent=0x0050041e`), OOB sample bit31=1;
  `(SPTPRAC & 0xc3)==0` (Linux GX-off bits6/7 plus transition bits0/1),
  GX GDSCR bit31=1, [65]=1. RBBM_STATUS is evidence only: no exact idle
  value is promised before GPU hw_init. CB1 FSR=0x400 was observed, unexplained; defined fault bits stay clear.

Results: 0=not run, 1=success. Codes (hex):

| Code | Failure |
|---|---|
| 801 | Unsupported SMMU geometry/capability/client-disabled state |
| 802 | No externally embedded GMU firmware |
| 803 | Malformed/truncated/alignment/unmapped data or prealloc extent |
| 804 | Existing exact or masked SMR matches SID5 |
| 805 | No unused disabled CB/free SMR/unused ASID, or bank became active |
| 806 | Stage8 cold prerequisites not met |
| 807 | Scratch overlaps runtime image or incoming FDT |
| 808 | SMMU TLB sync timeout |
| 809 | SMMU context/stream readback mismatch |
| 80a | Firmware dword readback mismatch |
| 80b | Page-table pool exhausted/overlapping mapping |
| 80c | Chosen CB fault after firmware load |
| 901 / 902 | Firmware init / HFI init timeout or CM3 fault |
| 903 | HFI ACK deadline expired |
| 904 | Corrupt indices, queue full, bad/partial/oversize response |
| 905 | Firmware error packet or CM3 fault |
| 906 | ACK id/type/returned header/sequence mismatch |
| 907 | ACK nonzero error (record holds firmware error) |
| 908 | Missing/malformed cmd-db aux data, uncovered ARC level or missing BW resource |
| 909 | Cold state lost before stage9 start |
| a01 / a02 / a03 | GPU_SET timeout/fault / GX status timeout/fault / GX GDSC still off |
| a04 | Chosen CB fault before GX request |

HFI failures in stage10 use 903..907 in [60]. No destructive recovery is
attempted after failures; reset before changing firmware/tables or retrying.

### Legacy page0 word map (128 × u32, little-endian)
```
[0]  magic 0xa740     [1]  stage_limit
Stage 1/read-only:
[4] cfg_ahb [5] memnoc [6] snoc_dvm [7] gpll0_vote
[8] cx_gdscr [9] gx_gdscr [10] ahb_clk [11] smmu_vote [12] cx_gmu [13] gpucc_read
[14] gpu_read [15] smmu_read
[16] scr0 [17] id0 [18] id1 [19] id2 [20] gfsr
[21] smr_valid|(first<<16) [22] smr_first [23] s2cr_first [24] rbbm_status
Stage 2: [26] wrote_enable [27] cfg_ahb_after
Stage 3: [28] cx_gdscr_before [29] gds_hw_ctrl [30] cx_gdscr_after
         [31] ahb_clk [32] smmu_vote [33] smmu_on
Stage 4: [35] done [36] scr0 [37] id0 [38] id1 [39] id2
         [40..43] CB0 cbar/sctlr/fsr/tcr   [44..47] CB1 cbar/sctlr/fsr/tcr
Stage 5: [48] pll1_mode [49] pll1_opmode [50] pll1_L
         [51] gmu_rcg_cmd [52] gmu_rcg_cfg [53] hub_rcg_cmd [54] hub_rcg_cfg
         [55] cx_gmu [56] cxo [57] hub_cx_int [58] demet [59] axi [60] gpll0_vote
         [61] gmu_sysreset [62] cxmisc_tcm_ret [63] rscc_seq_busy [64] hfi_status
         [79] gmu_regs_read (1 = GMU core was readable in stage 5)
Stage 6: [65] result (0 not-run, 7 all-pass; 3 PLL1 lock t/o, 4 hub RCG t/o,
             5 gmu RCG t/o, 6 cx_gmu CLK_OFF t/o)
         [66] gpll0_voted [67] pll1_lock_polls [68] cx_gmu [69] cx_gmu_polls
         [70] pll1_mode [71] pll1_opmode [72] gmu_cfg(&0x7ff) [73] hub_cfg(&0x7ff)
         [74] gmu_sysreset(want 1) [75] tcm_ret(want 0) [76] rsc_busy(want 0)
         [77] hfi_status(want bit0=0)
Stage 7: [80] cmddb_version [81] cmddb_magic(want 0x0c0330db) [82] slaves
         [83..90] addresses: gfx.lvl, mx.lvl, cx.lvl, gmxc.lvl, SH0, MC0, ACV, CN0
```
A healthy stage-6 result is `[65]=7` with `[74..77] = 1/0/0/even`, i.e. the GMU
is powered, clocked and held in reset, RSC idle, HFI not yet ready — exactly the
cold-start precondition `ShengGmuLoadAndStartHfi` checks.

## 5. HFI and implementation assumptions

The supplied Linux uses **two** queues (command0, response1), not the three in
the old lane plan. HFI BO: 16 KiB, header at0 (six u32), two 12-u32 queue
headers at24/72 bytes, 4 KiB rings at+4096/+8192. Table `size=120` follows the
Linux implementation's bytes, despite its header comment saying dwords. Rings
have 1024 words, leave one slot empty, align packets to four words and pad
commands with 0xfafafafa. Header size counts unpadded words. Only matching
id126/type1 ACKs with error0 count as success.

The perf table contains OFF plus the nine requested live-DT GPU frequencies in
ascending order (719 MHz represented as 719000 kHz per task; handoff calls top
719.6 MHz). ARC levels = 0/0x34/0x38/0x3c/0x40/0x50/0x80/0x90/0xc0/0xe0.
GMU OFF/200/500 MHz with levels0/0x40/0x80 follows its Linux OPP table. Primary
indices and levels come from gfx.lvl/cx.lvl aux data; secondary from gmxc.lvl
if present, otherwise mx.lvl. Encoding is `primary_level<<16 | secondary_idx<<8
| primary_idx`, including Linux's highest-nonzero secondary fallback.
Every unpopulated GX ACD field is zero, each populated entry uses 0xffffffff,
as Linux does. A740 BW table has one DDR off entry for SH0/MC0/ACV and two CN0
entries off/on; stage10 requests **BW index0**, the only DDR entry, with GPU
perf index1 (220 MHz). It never requests the high GPU frequencies.

This remains the default. The later [GPU-CLOCK lane](gpu-clock.md) adds an
XNU-only `fuxigpulevel=1..6` override with active DDR votes and startup clock
diagnostics; the loader probe keeps this original policy.

Stage9 cold RSC/PDC sequence uses the A740 sequence-memory offset0x154,
hidden data0x80000021 and AOP-resident PDC (enable/start only). No warm-resume
path. GMU chip ID config=0x07020100. It sets FAL veto/TCM retention, boot2,
QTBL, fence range, CM3_CFG=0x4052 and log address as Linux fw_start does.
SYS_BUS/ICACHE/DCACHE config=1. GBIF QSB registers are **not** touched before
GX: Linux sets those during later GPU hw_init. IRQs are masked and polled.
No host GIC setup, zap, or secure-mode calls.

Unverified assumptions/limits: the generic GMU blob matches Fuxi; selected CBs
are writable in this boot's hypervisor environment; the fixed high RAM window
is reachable from the GMU SMMU; the documented GPU-pwrlevels-0 bin is the active
one. No QMP AOSS message to disable ACD is sent; firmware defaults and Linux's
ACD vote fields are used. No special IFPC override is added (Linux A7xx returns
early from power_config). These require device evidence, especially stage9
startup and stage10's live GX status. Offline mocks do not prove power or DMA.

## 6. Build, validation, and coordinator test order

Optional environment inputs: `FUXI_GMU_FIRMWARE=/absolute/local/path` and
`FUXI_SQE_FIRMWARE=/absolute/local/a740_sqe.fw`. The build
writes an `.incbin` assembly file **only in ignored shim/out/fuxi-loader**;
the input remains external. Omitting the variable emits empty symbols, builds
normally, and stage8 reports802 without touching scratch or the SMMU context.
With GMU embedded but SQE omitted, stage11 is available and stage12 returns
`c01` before GPU_SET/hw_init. Standalone fuxi-probe builds remain supported
without embedded firmware (stage8 returns802).

```sh
FUXI_GMU_FIRMWARE=$FW/gmu_gen70200.bin \
FUXI_SQE_FIRMWARE=$FW/a740_sqe.fw \
CLANG=$LLVM/clang \
LLD=$LLVM/ld.lld \
OBJCOPY=/usr/bin/llvm-objcopy-18 READELF=/usr/bin/llvm-readelf-18 \
NM=/usr/bin/llvm-nm-18 MKBOOTIMG=$MKBOOTIMG_DIR/mkbootimg.py \
bash shim/fuxi-loader/build.sh shim/out/fuxi-integ1-payload.bin
```

The M2 artifact/history remains in Device results and git history. Current M3
artifact, size, hash and ELF geometry are recorded in the final handoff below.
The supplied integ1 payload and firmware inputs remain external/read-only;
no firmware or boot image is tracked. Loader builds enforce `-Werror`.

Offline checks completed: firmware-enabled loader, no-firmware loader and
standalone probe all build with **zero warnings**; relocation/Image-size guards
pass. Native C memory model under UBSan and `-Wall -Wextra -Werror` passes with
synthetic firmware and the exact external blob; checks all icache PTEs and holes,
privilege attributes, firmware format bounds/readback, bank/ASID ownership,
masked SID collision, TLB timeout/readback failure, HFI startup order, ACK
errors/sequence/size, ring wrap/full/invalid index, bounded init/ACK timeouts,
and permanent GMU-start latch after init failure. M3 adds separate GPU table
leaves/permissions/holes, two-stream ownership, changed/unreadable ownership
refusal, TLB timeout/readback, CP packet contents, SQE header removal/readback,
rptr/idle/fence completion, fence guards, contained SECVID write abort, defined
FSR faults vs reserved bit10, and no retry/no next stage after failure. Python decodes all eight model
report pages offline. It does **not** execute firmware or emulate power/SMMU.

```sh
python3 tools/test-fuxi-gpu-probe.py
python3 tools/test-fuxi-gpu-probe.py --firmware /absolute/local/gmu_gen70200.bin \
  --sqe /absolute/local/a740_sqe.fw
python3 tools/fuxi-gpu-probe.py --decode /private/captured-report.bin
```

Coordinator test order is in the M3 handoff below. The main checkout's
`research/run-gpu.py` is read-only for this lane and unchanged. It mounts the
**main checkout's tool**, so integrate this lane's decoder before using that
runner, or use this worktree's tool against an already temporarily booted probe.
Pass the explicit M3 image path. The runner was not executed here.

## 7. Remaining work

Run coordinator tests 11, 12, 13 with fresh boots between the initial bisects,
then prove same-boot 11→12→13 advancement. Hardware outcomes are unknown.
Rendering, XNU DMA ownership, an XNU GPU driver and userspace acceleration remain
future work. The GMU-start handoff lock must stay: this scratch still overlaps
XNU's RAM window, and both processors retain DMA access until reset.

## Device results (2026-10-20, coordinator)
- 7 PASS (m2). 8a: first m2 boot reset the SoC (sync abort -> fatal -> PSCI reset), not
  reproduced on m2b, which reads the snapshot through `probe_safe_rd32` (0 faults; **page2[48..52]**, corrected from the old handoff).
  Every SMR invalid, S2CR 0x20000, CBAR 0, no bank enabled.
- 8 PASS (slot0 -> CB1, ASID1, 13 tables, fw read back).
- 9: m2b 0x906 - the gen70200 GMU ACK header echoes the command id (0x00010304), not 126.
  m2c accepts either (Linux checks only ret_header seq). m2c 9 PASS, all ACK errors 0.
- 0x49 after GMU start: STALL. 10 PASS: GX_GDSCR 0xf8222800, RBBM_STATUS 0x1.

## Milestone 3: layout, source choices and expected results

The following are implementation facts and predictions, **not new Fuxi device
observations**. The table pool helper is shared with M2 but receives an explicit
root/pool cursor; the native model checks that M3 leaves the GMU root unchanged.
The fixed reservation overlap check in stage8 is a prerequisite of stage11.

| Use | GPU VA | Physical base | Bytes / permission |
|---|---:|---:|---|
| GPU table pool | unmapped | `0x8f1200000` | 1 MiB reserved, 3 × 4K pages used |
| SQE code | `0x40000000` | `0x8f1300000` | 128 KiB, GPU read-only (both privilege levels) |
| Ring | `0x40020000` | `0x8f1320000` | 32 KiB, RW |
| BR/BV rptr shadows | `0x40028000` | `0x8f1328000` | 4 KiB, privileged RW; offsets 0/4 |
| Fence/scratch | `0x40029000` | `0x8f1329000` | 4 KiB, RW; magic at 0, guards at 4/0xffc |

All are inside `[0x8f0000000,0x8f2000000)`. GMU-owned memory ends at
`0x8f1111000`; M3 does not touch it. M3 uses TTBR0 only, 32-bit GPU VAs,
36-bit PA, 4K granules, NC memory and walks, AF/inner-shareable leaves.
All other GPU VAs are unmapped, including the GMU buffers/tables. SQE's leaf AP
is 3 (read-only); ring/fence AP 1; rptr AP 0. This is a small loader address space,
not Linux's split TTBR0/TTBR1 16 GiB process address-space design.

Stage11 checks **both** SID0 and SID1 for exact/masked collisions: supplied
`arch/arm64/boot/dts/qcom/sm8550.dtsi` attaches both to the GPU device. There is
no LPAC submission/start. Two invalid readable SMRs are selected. Snapshot and
live stream entries must agree, except the expected M2 SID5 entry. A snapshot
stream fault refuses selection because a hidden masked match cannot be ruled
out. Banks must be readable, disabled in the snapshot and live, unreferenced by
any live translating stream, at/after the stage1 banks, and neither bank0 nor
the GMU bank. ASIDs exclude the GMU ASID and every readable enabled bank's two
live TTBR ASIDs. Faulted banks are not candidates. ACTLR/CONTEXTIDR are retained
and reported; M3 does not guess a replacement implementation ACTLR value.
Stage11 writes/validates the same context format as verified stage8, invalidates
by the chosen ASID, waits for TLB sync, enables M, then publishes S2CR/SMR last.
Expected with the observed empty 8a: **slots1/2, CB2, ASID2**, root
`0x8f1200000`, TTBR0 `0x00020008f1200000`; this selection is not hardcoded.

Stage12 ports the CP/GBIF/UCHE subset of supplied `adreno/a6xx_gpu.c:hw_init`,
`a6xx_ucode_load`, `a7xx_cp_init`. `gpu-a740.h` holds source A740 HWCG values
and the A730 protection table used by Linux's A740 catalog (slots45/46 remain
untouched). It clears GBIF halts/trusted range, applies A740 HWCG/GMU CGC,
GBIF QoS, UCHE no-bypass/trap/GMEM range (16 MiB + 3 MiB), cache ways, AHB error
response, hang detection, UCHE queue/NC mode and expanded BR/BV/LPAC APRIV.
No GIC/interrupt delivery: `RBBM_INT_0_MASK=0`, raw status is polled. Graphics
pipeline UBWC setup, LLCC optimization, performance counter routing, preemption
and IFPC restore lists are omitted: no rendering or preemption is submitted.
Whether the remaining reset defaults suffice is a **device-test hypothesis**.

`FUXI_SQE_FIRMWARE` embeds a local SQE blob in the ignored output. Stage12
checks size/alignment/capacity and copies/readbacks all bytes **after dropping
the first dword**, as `adreno_fw_create_bo` does. The supplied file is 75380 B,
copy 75376 B / 18844 dwords. The 32 KiB ring uses `CP_RB_CNTL=0x20c`; BR rptr
shadow is at `0x40028000`, BV at `+4`. It requires reset rptr/wptr=0, starts
`CP_SQE_CNTL=1`, publishes a barrier then wptr 10, and waits for rptr=wptr=10
and `(RBBM_STATUS & ~1)==0`. Command dwords:
```
70970001 08000000                       CP_THREAD_CONTROL, disable binning
70c80007 0000014b 00000003 20000000       CP_ME_INIT (7 payload words)
00000002 00000000 00000000 00000000
```
No zap is loaded. Linux's A740 catalog specifies `a740_zap.mdt`; its generic
`hw_init` attempts zap **after** CP init, then handles `-ENODEV` by writing
`RBBM_SECVID_TRUST_CNTL=0`. This probe explicitly takes that no-zap fallback,
with safe read-before/write/read-after and raw A7xx TSB status. It sends no
`CP_SET_SECURE_MODE`, calls no SCM/PAS, and never maps secure SID2. A contained
permission fault is `c09`, nonzero trust readback is `c0a`; raw RBBM interrupt,
CP protection/HW fault, trust and TSB bits are retained. **Fuxi permission and
result bits are unknown until stage12 is run.** Safe writes only contain local
synchronous aborts; asynchronous violations/hangs can still reset the SoC.

Stage13 appends exactly four dwords (no wrap or buffer reuse):
```
703d8003 40029000 00000000 a740f00d       CP_MEM_WRITE, fence VA, magic
```
Expected wptr/rptr 14 and CPU fence 0→`0xa740f00d`, both guards `0x5ac3e17b`, idle
RBBM (`status & ~1` zero), CP interrupt error bits/global FSR/defined CB FSR
bits clear. Shadow rptr 10/14 is useful evidence but register rptr is the gate;
there is no guessed SQE_STAT[1] value. `CP_HW_FAULT`/protection should have no
active error indicated by CP_INTERRUPT_STATUS; raw values are reported.
Defined CB fault mask is `0xc00001fe` (same as M2). The observed CB1 FSR 0x400
is preserved in reports and ignored as a reserved SMMUv2 bit; its cause is still
unknown. Any defined fault in **either** bank stops submission/completion.

Every stage is attempted once and only after the previous PASS. There is no
rollback or recovery: partial tables/context/SQE state is retained for diagnosis.
GPU_SET ACK and GX transition waits are each 1000×10 us; GPU TLB sync 1000×1 us;
CP init and fence waits each 5000×10 us (50 ms nominal delay budget plus MMIO
cost), checking CP/RBBM/both-bank/global faults every 16 polls and at completion.
All loops are bounded. After stage12 obtains the OOB ACK, GPU_SET stays asserted
through later requests/errors until normal loader reset. Request0x49 still
stalls from stage9 onward, independent of any M3 failure or missing SQE blob.

### M3 report pages and failures

The v3 decoder accepts legacy 512-byte and v2 six-page 3072-byte captures too.
New captures have eight 512-byte pages (4096 B), selected by 0x100..0x107.
Pages0..5 retain their M2 fields; page1[0] becomes `0xa7400003`.
Page6 (0x106), each index a local little-endian u32:
```
[0] 0xa7400003 once 11 entered [1] stage11 attempted
[2] stage11 result [3] stage12 result [4] stage13 result
[5] stage12 attempted [6] stage13 attempted [7] GPU_SET ACK/held latch
[8] GMU CB [9] GMU ASID [10] SID0 slot [11] GPU CB [12] GPU ASID
[13:14] table PA low/high [15] context config issued [16] TLBSTATUS
[17] SQE file bytes [18] copied bytes [19] skipped file dword
[20] first copied dword [21] copied/readback dword count [22] bad file offset
[23] CBAR [24] CBA2R [25] TCR2 [26] TCR [27:28] TTBR0 low/high
[29] MAIR0 [30] SCTLR [31] SID0 SMR [32] SID0 S2CR [33] table pages
[34] SID1 slot [35] SID1 SMR [36] SID1 S2CR
[37:40] SQE/ring/rptr/fence GPU VAs [41] ring bytes [42] initial CP wptr
[43] TRUST_CNTL write completed [44] trust before [45] trust after
[46:47] A7xx TSB_STATUS low/high
[48] init rptr before [49] after [50] submitted/readback init wptr [51] polls
[52] CPU fence before [53] last polled value [54] expected magic
[55] fence rptr before [56] after [57] submitted/readback fence wptr [58] polls
[59] SQE start issued [60] stage12 substep (1 SQE,2 OOB,3 hw_init,
     4 SQE start/CP init,5 TRUST fallback,6 complete)
[61] OOB interrupt [62] SPTPRAC [63] GX_GDSCR
[64:71] SQE/ring/rptr/fence physical low/high pairs
[72] preserved GPU ACTLR [73] CONTEXTIDR
[74:75] GMU FSR before/after11 [76:77] GPU FSR before/after11
[80:89] ten init command dwords [90:93] four fence command dwords
[110:111] last faulting MMIO PA [112] ESR [113:114] FAR
[115] last operation (1 read,2 write) [116] safe MMIO fault count
[117:118] first faulting MMIO PA
```

Page7 (0x107): two frozen64-word records, stage12 at 0 and stage13 at 64.
Snapshot only runs while GX power-on/transition predicates were confirmed.
A failure before that leaves the record zero; page6's substep/OOB/GX/fault
fields remain available. Within each record:
```
[0] stage captured (12/13;0 absent) [1] CP rptr [2] wptr
[3] CPU BR shadow [4] BV shadow [5] CPU fence [6] RBBM_STATUS
[7] CP_SQE_CNTL [8] CP_SQE_STAT_DATA with STAT_ADDR=1
[9] CP_HW_FAULT [10] CP_INTERRUPT_STATUS [11] CP_PROTECT_STATUS
[12] RBBM_INT_0_STATUS [13] SECVID_TRUST_CNTL [14:15] TSB_STATUS
[16] global SMMU GFSR
[17] GPU CB [18] FSR [19:20] FAR [21:22] FSYNR0/1
[23] GMU CB [24] FSR [25:26] FAR [27:28] FSYNR0/1
[29] GMU2HOST_INTR_INFO [30] AO_HOST_INTERRUPT_STATUS
[31] cumulative safe MMIO fault count [32:33] fence guards
```

Results 0=not run, 1=PASS. M3 failure codes (hex):

| Code | Meaning |
|---|---|
| b01 | Safe SMMU MMIO fault or unreadable snapshot stream ownership |
| b02 | Snapshot/live ownership changed, bank became active, slot not free, or GMU stream changed |
| b03 | Existing exact/masked SID0/1 match |
| b04 | No free readable GPU bank, two free SMRs or unused nonzero ASID |
| b05 | GPU table capacity/overlap |
| b06 | GPU context TLB sync timeout |
| b07 | GPU context/stream readback mismatch |
| b08 | Defined fault on either bank at stage11 |
| c01/c02/c03 | Missing SQE / invalid size / payload copy readback mismatch |
| c04 | GPU_SET/GX timeout or MMIO fault |
| c05 | hw_init, CP or diagnostic MMIO fault |
| c06 | CP/RBBM/global SMMU/defined GPU or GMU CB fault |
| c07 | CP init rptr/wptr/idle timeout |
| c08 | Initial CP ring was not reset (rptr or wptr nonzero) |
| c09/c0a | SECVID/TSB access fault / TRUST_CNTL failed to clear |
| d01/d02/d03 | Fence MMIO fault / CP-RBBM-SMMU fault / fence-rptr-idle timeout |
| d04/d05 | Lost ring/fence precondition / guard overwritten |

Coordinator test order (serial, temporary boot only, save private binary/logs):

1. Fresh M3 boot stage7 regression, then fresh stage8a; review all streams/banks
   and page2 fault masks. Fresh stage10 regression, expect known M2 PASSs.
2. Fresh boot **stage11**. Require result 1, GPU CB≠0/GMU CB, different ASID,
   slots1/2 as predicted only if snapshot is unchanged, 3 table pages, context
   readbacks correct, both defined FSR masks zero, no safe MMIO faults. No SQE
   started; request0x49 must still stall because stage9 ran.
3. Fresh boot **stage12**. Require result 1, substep 6, copied 18844 dwords,
   rptr 0→10/wptr 10, OOB held, TRUST write_ok 1/readback 0, no active CP/RBBM/SMMU
   faults. Record actual TRUST-before and TSB status bits; no prior expectation
   of their reset values. Inspect page7's stage12 snapshot.
4. Fresh boot **stage13**. Require stage11/12 PASS and fence result 1,
   CPU0→magic, rptr 10→14/wptr 14 and guards intact. Inspect both frozen snapshots.
5. After fresh-boot bisects pass, same boot **11→12→13**, fetching reports between
   each and delaying between 12/13 to exercise the retained GPU_SET. Repeating 13
   must return existing results without a new submission; `--read-reports`
   returns RAM only. Keep the 0x49 refusal check.
6. Any failure/reset: stop climbing, capture available report pages, record exact
   boot/reset outcome, and wait for normal loader reset. No retries within a
   failed boot; no handoff, partition changes or firmware replacement on device.

Use `tools/fuxi-gpu-probe.py --stage 11` (then12/13 as appropriate), `--raw`,
`--save research/device/<private-run>/gpu.bin`. The new decoder fetches all
8 pages for v3. `--read-reports` never reruns hardware setup.

### M3 build handoff

Offline only: firmware-enabled loader, no-firmware loader, standalone probe
builds: **PASS, zero warnings**, linker/Image/relocation guards PASS. Native
UBSan C model and Python eight-page decode: PASS with synthetic and both exact
external firmware blobs. These checks model MMIO completion; they do not run
SQE/GMU code, translate real DMA, test protected-register permissions or prove
GPU power/CP execution. **No M3 device run, boot result or CI run exists yet.**

Image (this worktree, gitignored):
`shim/out/lane-gpu/fuxi-gpu-m3-boot.img`, **20,865,024 B**.
SHA256: `12b193ecf2ae55f26fe9ca553dca496528a8abcfb9ab047f41207baa856a4cdd`.
Payload SHA256:
`800b9ea17ce18f1872c0533d5ef13f916c490eb7b1524e677fc2555378f00d66`.
ELF `_gpu_fw_start/end=0x1028c/0x207a8` (66844 B),
`_sqe_fw_start/end=0x207a8/0x32e1c` (75380 B),
`_bss_start/end=0x34000/0x64000` (192 KiB),
`_payload_start=0x74000`, `_image_end=0x13e4050`.
No firmware bytes or dumps are committed. Next concrete action: coordinator
stage7/8a/10 regression with this image, then fresh-boot stage11.

### M3b build handoff (current artifact)

Offline firmware-enabled loader, explicitly empty-firmware loader and standalone
probe builds: **PASS, zero warnings** with clang-r547379. Loader `-Werror`,
relocation checks, Image size/header checks and link: PASS. Final native UBSan
model with the exact external sheng GMU/SQE inputs: PASS. Linux source register/
table audit: PASS. Ten-page decoder and client/interrupt labels: PASS; original
private M3 eight-page report still decodes offline. These checks do not execute
SQE code or prove the CP_INIT candidate fix. No phone access or M3b boot test.

Artifact in this worktree (gitignored):
`shim/out/lane-gpu/fuxi-gpu-m3b-boot.img`, **20,865,024 B**.
SHA256: `c645f6d7ffb8740aaead35699adc9c2ea7c5d2721d39db88ff364acd80768a53`.
Kernel Image: 20,856,912 B. Payload unchanged:
`800b9ea17ce18f1872c0533d5ef13f916c490eb7b1524e677fc2555378f00d66`.
Firmware hashes unchanged from the sheng table above. Build uses `$FW`, `$LLVM`,
`$MKBOOTIMG_DIR` and the supplied integ1 payload, per section6. The absolute
input payload can be passed as the build script argument; it need not be copied
into this worktree. No firmware, dump, boot image or local path is committed.

Firmware build ELF geometry: `_gpu_fw_start/end=0x10ec4/0x213e0` (66844 B),
`_sqe_fw_start/end=0x213e0/0x33a54` (75380 B),
`_bss_start/end=0x34000/0x64000`, `_payload_start=0x74000`,
`_image_end=0x13e4050`. Both extra report pages fit existing BSS alignment.
The lane image was copied before the empty-firmware verification build, so the
lane artifact contains **both** blobs. Commit: the commit containing this
handoff (`git log -1` on `codex-gpu-m3b`). Next action: coordinator fresh stage12
after regressions/stage11, using the new phase/PC/SID fields before further
pointer changes. The hardware root cause remains unconfirmed.

## Device results, milestone 3 (2026-10-20)
- m3 (first build): 11 PASS (SID0/1 -> CB2, ASID2). 12: CP instruction fetch at IOVA 0
  (FSYNR0 IND=1, CP_INT BR_ILLEGAL_INSTR). m3b removed the null CP_INIT list: same fault, now
  before the doorbell. m3c root cause: SQE_CNTL was set before CP_SQE_INSTR_BASE, so the SQE
  started fetching from 0. Linux cold ordering (bases verified, then RUN) fixes it.
- m3c 12: CP_INIT consumed (rptr 0 -> 7 = wptr), CP_INT 0, SMMU clean. RBBM_SECVID_TRUST_CNTL
  stays 0x1 after writing 0 (FACT; GUESS: TZ-owned on retail, needs the zap shader via PAS).
  m3d treats this as a report value, not a failure.
- **m3d 13 PASS: CP_MEM_WRITE of 0xa740f00d to the fence page (VA 0x40029000) landed, read by the
  CPU; rptr 7 -> 11, no faults, CP_INT 0, TRUST still 0x1.** The GPU executes PM4 from our ring
  through our SMMU context.
- Open: CB FSR bit 10 (0x400) on both banks from programming time; secure-mode exit; no draw yet.

## Device results, milestones 4–5 (2026-10-20, coordinator)

| Image | Run | Result |
| --- | --- | --- |
| m4 `00fc43eb` | fresh 7..14 | 7–13 PASS; 14 = `0xe06`: all 65536 dst pixels untouched (`0xdeadbeef`), ring consumed, post-blit fence landed, no faults |
| m4b `d5807c0d` | fresh 7..14 | 14 = `0xe06`; the source-free solid clear (`0xe0b`) also writes nothing. `RBBM_SECVID_TRUST_CNTL` = 1 |
| m5 `ec9f0953` | fresh 7..16 | 16 = `0xf06`: SCM reachable, but PAS `is_supported(13)` returns 0 and the stage stopped |
| m5b `eaabc9a4` | fresh 7..16 | 16 PASS: `init_image`, `mem_setup` (0x9b09a000 + 0x1000), `auth_and_reset` all return 0; `CP_SET_SECURE_MODE 0`; TRUST 1 → 0; clear and 256×256 copy PASS per pixel. GPU 67.7 µs, uncached CPU byte copy 53.2 ms |
| m5b `eaabc9a4` | 16 then 15, same boot | 15 PASS: 256×64 GPU blit into the live scanout at `0xb8000000` (pitch 4320), checksum match. GPU 217.9 µs, uncached CPU byte copy 13.5 ms |

Facts: without the zap shader the A740 stays in secure mode and only the CP reaches
normal memory; the 2D engine silently drops its writes. This firmware answers
`is_supported(13)` with 0 even though the real PAS calls succeed, so the query is
advisory only (Linux does not issue it either). The CB FSR bit 10 (0x400) on both
banks remains unexplained and harmless so far.

## Device results, milestone 6 in XNU (2026-10-20, coordinator)

All runs boot iOS 16.7 to SpringBoard with `fuxigpu=1` and the 768 MiB `--gpu-reserve` layout.

| Kernel | Result |
| --- | --- |
| 0096 / 0096b | Panic about 2 s after AppleCLCD: `kmem_alloc(…, 33554432, 0x100021): failed with 3`. Mapping the 32 MiB scratch through `ml_io_map` exhausts the 8 MiB IO submap, and the NOFAIL allocation panics instead of returning 0. (`mbuf_pool=64` in 0096b changed nothing.) |
| 0096c | No panic. Clocks, GMU, HFI, GX, SMMU, CP and the fence all pass inside XNU. The zap fails: the first SMC returns with x0 still equal to the function id. The GPU disables itself and iOS keeps presenting on the CPU. |
| 0096d | Root cause of 0096c: Apple's assembler treats `;` as a comment, so `"dsb sy; smc #0; dsb sy"` lost its `smc`. Fixed. All PAS calls return 0 on CPU0, the zap loads, TRUST goes to 0 and the self-test pixels pass. Every present goes through the GPU (600/600), but takes 13.6–14.0 ms per frame (38 fenced row batches, per-frame page mapping, busy polling), against about 1.25 ms for the CPU memcpy |
| 0096e | Four cached backings, one submission per frame, targeted cache clean, sleeping wait. 594/596 presents on the GPU, 1.58 ms wall time and **0.72 ms CPU time per present** (clean 0.60, submit 0.03; the 0.93 ms wait is spent asleep). Cache hit 592/594 |

The remaining CPU cost is cleaning the CPU-rendered source rows from the cache.
