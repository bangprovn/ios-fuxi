# Backboardd frame throughput

## Evidence and limits

The supplied device windows report roughly 48–144 draws, 4.9–11.3 ms median
GPU execution and 17–25 ms median `wall`, with two admitted flights. Panel and
CA tick periods are already about 8.27 ms. No device was accessed for this
change. The code identifies serialization and redundant recording, but does
not establish which dominates that workload or demonstrate 60 fps on A740.

`wall` in overlap mode is **commit entry to retirement after surface release**,
not a frame interval or an exclusive CPU cost. `completion_ms` additionally
includes waiting for scheduled-handler delivery. `interval` measures command
buffer creation cadence, excludes gaps >=100 ms, and includes client work.
Neither 1/median(wall) nor `gpu_exec / draws` is a throughput or shader-cost
measurement. The latter includes passes, image transitions, memory barriers,
clears, transfers, and GPU idle time between the two GPU timestamps.

## Why two flights need not double throughput

* `Frontend.mm` reserves a two-token semaphore in `reserveOverlap`, including
  on explicit `enqueue`, before placing work on the serial submit queue. Thus
  `overlap_depth=2` can mean one GPU flight and one worker waiting to submit.
  A third reservation blocks the caller. A queue created with a maximum count
  of one has only one token.
* `commit` copies each unique no-copy buffer into a CPU snapshot before it
  returns. `executeCommitted` copies the snapshot into its shared Vulkan
  buffer through `prepare`. **Any nonempty `prepare` drains both backend slots**,
  even if the pending flight touches unrelated buffers. Indexed asynchronous
  draws also drain both slots before CPU index validation. This is conservative
  ownership protection; simply deleting the drain races GPU reads/CPU writes.
* `Backend::submit` holds one device mutex across planning, translation/runtime
  variants, descriptor updates, recording, mapped-memory flushes and
  `vkQueueSubmit`. Conflicting noncoherent allocations or overlapping host
  ranges wait for retirement. `lease_lock` historically combines IOSurface
  lease acquisition and this entire backend lock/admission wait, so it was
  never just a mutex measurement. The supplied small lease times do not prove
  a large shadow drain in those particular windows.
* Deferred fence waits run on the serial retire queue **outside** the backend
  mutex. Retirement reacquires that mutex for query results, whole-allocation
  invalidation and destruction, releases IOSurface leases, then frees an
  admission token. Consequently CPU recording can delay retirement, and host
  cache maintenance can delay the next submit. The synchronous event-segment
  path still waits under the mutex. Signals/waits split a Metal command into
  synchronous segments; only its final segment gets deferred retirement.
* Every referenced IOSurface is leased. Surfaces whose textures were only
  sampled by draws take a read lease: a shared user-level hold (readers do not
  block readers, writers wait for readers and writers) and a ReadOnly
  IOSurfaceLock, which skips the kernel writer count and never sleeps behind a
  scanout copy. Any other use (target, blit, useResource, unmarked) is a
  writer: exclusive, kernel writer lock that waits for the scanout
  copy/present of that surface. The lease no longer holds its mutex across the
  kernel lock, so retirement can release other leases meanwhile. Device effect
  not yet observed; `lease_front/back/excl_wait/lock_write/lock_read` split it.
* Scheduled handlers run inline on the submit queue. Completed handlers run
  on a separate serial delivery queue, possibly targeted at CA's supplied
  completion queue. `presentDrawable*` is a completed handler: it presents only
  after successful retirement and can wait behind other handlers. Delivery
  does not hold the backend mutex or admission token, but CA can wait for a
  callback/presentation before producing the next frame. `waitUntilCompleted`
  also includes completed-handler delivery. Delaying that queue can therefore
  throttle the producer without filling the GPU queue.
* FuxiMetal supplies no CAMetalLayer/`nextDrawable` implementation. That call
  and drawable-pool throttling belong to the external client/framework. There
  is no trustworthy direct `nextDrawable` timer to add here without interposing
  an external implementation. `commit_gap` measures the return-boundary-to-next-
  commit interval, which includes drawable acquisition, CA scheduling and CPU
  preparation. It cannot distinguish them by itself.
* The kernel `fuxi_gpu_abi_backend.inc` admits one client submission while
  `fg_client_busy`, `fg_busy` or `fg_scanout_reserved` is set. It requires an
  idle ring, switches the SMMU root, hands off imports, and rings the doorbell.
  Completion requires the fence, ring pointers and GPU idle observation,
  restores the root and invalidates imports before publishing success. Scanout
  shares this exclusion. Two Vulkan command slots do not remove this hardware
  admission bottleneck; Turnip submission behavior is external and unchanged.

The approximately equal `gpu_wait` and `gpu_exec` are consistent with a retire
worker starting its wait shortly after submission and little queued GPU work.
They do not prove `vkQueueSubmit` waits for execution. The new `queue_submit`
and `slot_wait` distinguish time inside that call from frontend admission.

## Where the apparent per-draw GPU cost comes from

`Backend::submit` records **one Vulkan command buffer and one vkQueueSubmit per
nonempty event segment**, not one per encoder. Descriptors are allocated and
updated per draw unless `fuxigpusubmit=1` finds an identical set; unique buffer
offsets and resources can still yield one set per draw. These updates cost CPU,
not GPU shader execution. Mapped-memory flushes occur once per unique resource
per submit, not per draw.

Without merging each draw starts/ends a render pass. Existing
`fuxigpumerge=1` groups safe draws within a known Metal encoder, preserving the
first load/clear and final store/discard, but previously always split different
encoders. Every resulting run has a broad ALL_COMMANDS memory barrier. Each
image transition also emits a broad image barrier even when its layout is
unchanged; merged runs deduplicate these only within that run. Render passes
have broad external dependencies as well. Around 25–30 remaining passes plus
repeated sample barriers are plausible GPU stalls; the trace alone cannot
assign 70 microseconds to an individual draw.

Default Turnip setup explicitly requests `TU_DEBUG=sysmem,nocb` (preserving any
explicit environment override). `fuxigpugmem=1` already removes `sysmem`; it
allows Turnip's choice rather than forcing GMEM on every pass. This remains an
independent experiment: existing bring-up notes record A740 GMEM hangs. Neither
Turnip nor its sources are changed here.

Clears and loads reflect Metal attachment actions. Intermediate CLEAR,
DontCare or discarded contents must not disappear. Undefined partial writes
also need their existing initialization. This change does not remove those
operations or the general inter-operation memory barrier.

The kernel already walks **all mapped imports**, since the IB list is not a
resource list, cleaning before submit and invalidating before successful fence
publication. Those CPU CMOs contribute to submit/wait latency, not directly to
the GPU timestamp range (although GPU/CPU contention can affect it). Existing
`fuxigpuncimport=1` cleans GPU-writable imports once at map and skips subsequent
submission cleans, while retaining completion invalidation and read-only
cleans. It requires no CPU writes for the entire mapped lifetime, including
aliases and fallback rendering. Permissions do not prove that contract; it is
not safe to enable automatically or drop all completion CMOs. See
[cache ownership](unixnu-port.md#cache-ownership-and-diagnostics-fuxigpuncimport1).
The existing `FUXI GPU: presents` cleaned/skipped/invalidated byte deltas expose
the volume. No kernel ownership change is made in this patch.

## Opt-in reductions

One new key, `fuxigpuperf=N`, accepts an exact decimal digit 0–3. Missing,
duplicate, malformed and unknown values disable it. Defaults are unchanged.
A space plus `fuxigpuperf=3` consumes **14 command-line bytes**; check the actual
608-byte budget rather than appending a collection of additional keys.

| Value/bit | Change | Retained protection |
| --- | --- | --- |
| 1 / bit 0 | Skip SHADER_READ_ONLY_OPTIMAL → same-layout image barriers | Real layout changes, GENERAL/attachment/feedback barriers, initial host barrier and every inter-operation barrier remain. Transfers/alias writers still have the global dependency. |
| 2 / bit 1 | Allow safe runs across different known encoder IDs, with `fuxigpumerge=1` | Identical attachments/mips/area and STORE→LOAD; existing storage-write, alias, shadow-producer, attachment-sample, transfer and unknown-encoder splits remain. First clear and last store/discard survive. |
| 3 | Both reductions | Same restrictions as the individual bits. |

Compare absent/0, 1, 2, 3 with the existing workload and existing keys fixed.
Bit 0 also works without merge. Bit 1 does nothing without merge. These are
recording optimizations, not permission to overlap host writes with GPU reads.

## Reading the next device trace

All appended timing pairs are p50/p90 **milliseconds**. Existing fields and
window bounds stay intact. The existing commit/queue frontend timings describe
overlap mode; the new frontend CPU/wall pair also supports synchronous commits.
Zero means unavailable/not observed where appropriate.

| Fields | Interpretation |
| --- | --- |
| `commit_call`, `admission_wait`, `snapshot` | Commit entry to its return boundary; semaphore wait (also measured on explicit enqueue); CPU snapshot work. Admission on an earlier enqueue need not lie within commit_call. |
| `commit_gap` | Previous commit's return boundary to this commit's entry on the same queue; idle gaps >=100 ms and overlapping callers yield zero. Boundary is immediately before releasing commitReady, since recording after return is impossible. |
| `submit_queue` | Return boundary to execution on the serial submit worker. Explicit enqueue/commit order can enlarge this. |
| `backend_lock`, `slot_wait`, `wait_reasons=drain,conflict,slots` | Device mutex acquisition vs backend condition-variable wait. Reason counters describe conditions observed before waiting, can overlap, and are not separate durations. |
| `flush`, `queue_submit` | Vulkan mapped-memory flush wall time vs actual successful vkQueueSubmit call wall time. Both are included in backend_submit. |
| `gpu_exec`, `gpu_wait`, `submits/frame` | GPU timestamps, host fence wait, and successful Vulkan submission count. More than one submit suggests event segmentation. |
| `retire_lock`, `invalidate`, `retire_host`, `surface_release` | Reacquiring the backend mutex after the deferred fence, mapped-memory invalidation, host retirement including invalidation/query/destruction (excluding lock wait), and IOSurface lease release. These overlap; do not add invalidate to retire_host. |
| `scheduled_handlers` | Inline scheduled-handler batch on the submit worker. A slow batch stalls later submissions and completion publication. |
| `prior_delivery`, `prior_handlers`, `prior_wait_completed` | Last observed queue delivery delay, completed-handler runtime (including drawable presentation), and explicit waitUntilCompleted duration, sampled at commit entry. These are atomic queue observations, can repeat, and are **not** attributed to the current frame. Zero can mean no observation yet. |
| `image_barriers/frame`, `read_barriers_skipped/frame`, `cross_encoder_merges/frame` | Actual image barriers emitted, read barriers omitted by bit 0, and encoder boundaries removed by bit 1. Global memory barriers are not part of image_barriers. |
| `render_passes/frame`, `merged_draws/frame`, `merged_splits`, `interval` | Check that passes and encoder splits fall without losing draws, then compare actual frame cadence, not reciprocal wall latency. |

`fuxigpuframetrace=1` uses only wall timers, with no thread CPU clock
reads or frontend method timing. `fuxigpuframetrace=2` adds bounded thread CPU
sampling and frontend method wall timing. Unknown values and duplicate tokens
disable tracing. The existing fields retain their positions in both modes;
unavailable CPU measurements are zero, never substituted with wall time.

| Appended fields | Interpretation |
| --- | --- |
| `backend_cpu` | Submit thread user + system CPU, after mutex/slot admission and any `prepare` shadow refresh through successful `vkQueueSubmit`. Two clock reads sample the first submit in a frame only; empty submits stop at empty completion. Fence waits, retirement and scheduled callbacks are excluded. |
| `frontend_cpu` | Two clock reads bracket the first traced frontend method through commit preparation, before backend submission or releasing the overlap worker. This **includes client CPU work between methods**, unlike the previous per-method sum. Interleaved command buffers can have overlapping spans. Zero if encoding migrates between threads or a clock fails. |
| `frontend_wall` | Level 2 only: summed residence inside render/blit encoder and command-buffer preparation methods. Nested convenience calls count once. Gaps between methods are excluded, so this is not the wall counterpart of the new `frontend_cpu` span. |
| `pipe`, `desc`, `record` | Wall time for translation/variant lookup, descriptor allocation/update/cache lookup, and recording excluding separately measured object/cache and descriptor API work. Present at both trace levels. |
| `kernel_io` | Wall envelopes around Vulkan memory APIs, queue submission and IOSurface leases. Includes userspace driver work; does not isolate actual kernel execution. Overlaps containing stages and must not be added to them. |
| `pipe_cpu`, `desc_cpu`, `record_cpu`, `kernel_io_cpu`, `backend_lock_cpu`, `slot_wait_cpu`, `flush_cpu`, `queue_submit_cpu` | Retained for append-only parser compatibility; zero in both modes. Detailed CPU attribution required expensive per-draw clock reads and is no longer collected. Use the corresponding wall stages. |
| `cpu_sampled_submits/frame`, `cpu_omitted_submits/frame` | Newly appended counters. Event-segmented command buffers still use at most two backend CPU reads: later segments contribute wall timers but no CPU samples. Compare `backend_cpu` to `backend_submit` only when no segments were omitted. |

An ordinary level-2 frame uses at most four CPU clock reads, independent of its
draw/encoder count. Frontend migration can invalidate its CPU sample; no clocks
from different threads are subtracted. Linux uses
`clock_gettime(CLOCK_THREAD_CPUTIME_ID)`; Darwin uses
`thread_info(THREAD_BASIC_INFO)` without creating a Mach send right. Mode 1
restores the low-overhead wall-only diagnostic path that preceded commit
`4e892d2`. No device timing improvement is claimed from host tests.

The reported device measurements with per-method CPU instrumentation roughly
doubled the frontend/backend costs. They identify planning and descriptors as
useful targets, but should not be treated as the uninstrumented frame budget.

### CPU work removed

* Exact `DrawInput` metadata and its value-only `DrawOutput` are cached once per
  submission. Repeated draws with the same pipeline and all input fields reuse
  the selected variant and plan. No heap allocation or resource ownership is
  added. Every draw still performs resource, alias, mip-access and vertex/index
  validation; changes to any buffer length/offset/bytes flag, format, filter,
  LOD bits, draw count/start or index metadata invalidate the cache. Translation
  diagnostic mode bypasses it. Each submission starts fresh, including after
  rollback, avoiding stale plans from mutable test-adapter controls.
* Runtime variant keys always use packed integer/float bits, eliminating decimal
  conversion and string growth even without `fuxigpusubmit=1`. When exact draw
  inputs differ, try an existing variant before paying for a failed baseline
  plan. The candidate still undergoes complete translator validation; rejection
  retries the baseline because packed shape keys omit lengths and offsets.
  A forced-collision test verifies this fallback, and 120 changing draw inputs
  verify that warmed variants avoid repeated baseline failures.
* Texture creation/import caches blend support for the actual tiling, replacing
  a physical-device format query on every blended draw.
* Merged passes omit unchanged pipeline, viewport, scissor and descriptor-set
  binds. Descriptor reuse compares only active bindings and still requires the
  exact set-layout handle; bind suppression requires the exact pipeline layout.
  The existing `fuxigpusubmit=1` cache remains bounded and submit-local, with
  batched descriptor writes. The external ABI exposes descriptor metadata and
  draw instantiation, not descriptor update templates; no translator changes
  or assumptions about its private structures are needed.
* Frontend resource ownership is deduplicated by object identity while retaining
  first-use order. Repeated bindings/draws no longer add duplicate array retains
  or cause repeated synchronous shadow copies at commit. Pipeline diagnostic
  text is formatted on state changes and moved into the operation snapshot;
  identical texture/sampler/pipeline
  setters avoid retain/release churn after validation.

Large commit_gap with small admission/submit-queue/backend waits points outside
submission (CA work, drawable availability, callback or tick scheduling).
Large prior_wait_completed or prior_handlers supports a completion-driven
producer. Large slot_wait plus drain/conflict reasons supports serialization
inside the frontend. Large queue_submit supports driver/kernel admission or
cache handoff cost. Reduced barriers/passes without reduced gpu_exec points
toward shader/bandwidth workload or unchanged pass boundaries; improvement in
gpu_exec without cadence improvement points back to CPU/presentation pacing.

## Host verification

The CI metal target invokes `test-fuxi-metal-frontend.py`. On Linux this builds
and runs portable C++ models under ASan/UBSan and explicitly skips native
Foundation/Metal Objective-C runtime tests; `tools/ci/build.sh` requires macOS
for its signed iOS bundle build. Native overlap tests also assert frontend
CPU/wall and commit/admission/handler attribution, but cannot execute on this
Linux host.

The frame tests cover a real sleep versus CPU work, bounded appended fields,
nested method wall attribution, frontend CPU spans including gaps, migration
refusal, failed clocks, zero CPU reads at level 1, and a fixed CPU-read budget
for 120 methods and 120 event segments. Backend controls
also exercise contended mutex acquisition and deferred retirement without
extending the submit CPU interval.

`test-fuxi-metal-merge.py` compares exact authored pixels against the unchanged
path for each bit independently and together, with overlap/feedback, blending,
load/clear/discard, CPU shadows, transfer and mip hazards, rollback and two
flights. Its separate Vulkan proxy counts actual image barriers/submissions,
checks render-pass compatibility and counts encoder merges. It also hides the
ordered-access extension to exercise fallback. Bad/duplicate values retain the
old policy. The benchmark keeps shader/descriptor/merge settings fixed and
varies encoder boundaries and the new mask. Host timings are observations,
not A740 predictions or pass thresholds.

The CPU optimization controls cover frontend/model, backend (levels 0/1/2,
contention and fault injection), frametrace/reuse (including deferred
retirement), merge, overlap, mipfeedback, M6 and local M9m/M9n/M9o suites,
plus the portable metal/metal2/metal2g/metal2h ABI controls and
`sync-fuxi-metal.py --check`. Native Objective-C and Apple-input suites require
separate macOS/CI inputs.
Authored LLVM AIR controls exercise the external pinned translator on Linux.

Throughput-change Linux verification commands:

* `python3 tools/test-fuxi-metal-frontend.py`
* `python3 tools/test-fuxi-metal-backend.py --headers <vulkan-headers>`
* `python3 tools/test-fuxi-metal-frametrace.py`
* `python3 tools/test-fuxi-metal-merge.py --glslang <host-validator>`
* `python3 tools/test-fuxi-metal-overlap.py --glslang <host-validator>`
* `python3 tools/test-fuxi-metal-mipfeedback.py --glslang <host-validator>`
* `python3 tools/test-fuxi-metal-m6.py --headers <vulkan-headers> --glslang <host-validator>`
* `python3 tools/test-fuxi-metal-m9m.py`, `test-fuxi-metal-m9n.py`,
  `test-fuxi-metal-m9o.py --translator <host-bridge>` (authored shader controls).
* `python3 tools/test-fuxi-metal.py`, `test-fuxi-metal2.py`,
  `test-fuxi-metal2g.py`, `test-fuxi-metal2h.py`.
* `python3 tools/test-fuxi-gpu-k1.py`, `test-fuxi-gpu-clock.py`,
  `test-fuxi-gpu-probe.py`, `test-fuxi-gpu-scanout.py`.
* `python3 tools/sync-fuxi-metal.py --check` and `git diff --check`.

The Linux run also executes M9a/b/g/h/i/k/l, translation, cache and cache-hit
suites against a pristine external checkout at the recipe pin. The M7 authored
AIR runner can execute directly with `metal-m7-control.ll` assembled by LLVM;
it does not qualify the Xcode metallib wrapper. M6b, pipeline and CA-offline
runners compile on Linux, but execution needs CI metallibs or Apple-extracted
inputs. Artifact qualification needs signed bundles/boot inputs. Those inputs
are intentionally not fetched or used here; defaults and native Objective-C
tests remain for macOS.

For the real-translator 120-draw benchmark, add
`--translator <host-bridge> --llvm-as <host-llvm-as>` to the merge test command.
It compiles the checked-in authored AIR control, warms three frames, measures
40 frames, checks every output byte, and checks two-flight object lifetime.
`CPUOPT_BENCH` reports median submit preparation and planning wall time; the
fence wait and cold shader compilation are excluded. Use identical descriptor,
merge and encoder-boundary flags when comparing revisions.

### 120-draw host results

Compared the pre-change headers at `6a46556` with the same extended benchmark
harness on both sides: clang++ `-O2`, the pinned external bridge built with
Cargo `--release`, lavapipe with two worker threads, one encoder, two alternating
sampled images, and `fuxigpumerge=1 fuxigpusubmit=1`. These are nonblending
16x16 full-quad draws through the real translator. Cold compilation and GPU
fence waits are outside the measured submit-preparation interval.

Each process warms three frames and measures 40. Fresh-process A/B/B/A gives
two p50 observations per version; the table reports their median, with their
range in parentheses. Host scheduling noise is visible in those ranges.

| Timing mode | Before submit ms/frame | After submit ms/frame |
| --- | --- | --- |
| Trace off | 1.824 (1.530–2.118) | 0.548 (0.347–0.748) |
| `fuxigpuframetrace=1` | 2.472 (2.114–2.829) | 0.522 (0.345–0.698) |

With level 1, the median of the planning p50s falls from 1.697 to 0.103 ms.
The new level 2 measures 0.595 ms submit preparation (0.512–0.678); its backend
CPU clock budget is two reads per sampled frame. Both versions allocate two
descriptor sets per traced frame with this workload. Every measured process
checks exact pixels and deferred two-flight ownership. These numbers establish
host CPU-work reduction, not achievement of the device's 16.6 ms/heat target.

## Linear samples split, and three more `fmtl` bits

The frame trace's `sampled_mb/frame` linear figure counts each binding at the
texture's full size, per draw, so it is an upper bound, not traffic. On the
phone (`d-ft1`) it read 97.4 MB per frame against 13.5 MB optimal. A new field,
`linear_split/frame=imported:N,imported_mb:M,large:L`, says how much of it is
IOSurface or other imports (the rest are textures over an `MTLBuffer`), and how
many of those samples are of a million pixels or more.

`fmtl` (hex, on the boot line) gains bit 16 `FUXI_MTL_GPU_PASS_TIMES`, bit 17
`FUXI_MTL_GPU_DRAW_TIMES`, bit 18 `FUXI_MTL_RT_SHADOW`, bit 19
`FUXI_MTL_RT_SHADOW_REUSE` and bit 20 `FUXI_MTL_RT_RENAME`, so each can be tried
without a root write. On the phone (root v10, scripted, alternated with `fmtl=1`)
`RT_SHADOW` (`fmtl=40001`) gave 91.6-92.0 % one-period frames against
78.5-83.9 %, and GPU p90 about 5.8 ms against 8.4 ms; per-pass times had put
77 % of GPU time in passes drawing into the linear 1125x2436 drawable. Not yet
checked by eye. With it on, the copy into the shadow each frame is about 0.4 ms,
which `RT_SHADOW_REUSE` is meant to skip. Test:
`tools/test-fuxi-metal-frametrace.py` checks `applyBootOptions` sets these.

### Device results after `RT_SHADOW` (root v11, 2026-11-02, scripted)

All runs below: kernel m8t, `fmtl=40001` base, no `submit FAIL` or gate
failure. Script runs only compare; a hand decides the limit.

- `RT_SHADOW_REUSE` (`c0001`) removes the copy into the shadow (about one per
  frame to about zero) but GPU p90 stays 5.3-5.7 ms; `RT_RENAME` (`140001`) and
  both (`1c0001`) also land in 87-91 %, inside the spread of two plain `40001`
  runs. `40001` stays the setting.
- GPU level 7 (615 MHz) and 6 (550 MHz) against 8 (680): 90.2 and 87.7-88.6 %
  against 91.9-92.0 %, GPU p90 6.3-7.0 against 5.8-6.1 ms. The CPU stays at
  67-69 C either way, so the touch boost (new boost at or below 60 C, cut at
  62 C) hardly runs (50-200 ms in five minutes) and gold sits at 1.4 GHz while
  scrolling. Level 8 stays.
- Present FIFO off (`fuxiearly=1` against 5, frame log): swap to START drops
  from about 27 ms to 9-16 ms, but one-period frames fall from 92 % to 82 %,
  with about 950 copies replaced by a newer one per run. Copies finish in pairs
  within one period (CA swap interval p10 7.2, p90 9.3 ms), and the FIFO is what
  absorbs that. `fuxiearly=5` stays.

## `fmtl` tokens OR together; late-frame lines (bit 22)

Before 4 Nov `applyBootOptions` stopped at the first `fmtl=` token, so a test
that appended its own token to the base one changed nothing (d-lean-b1/b2 logged
`boot options fmtl=40001` and `submit_lean=0` with `fmtl=40811` on the line).
All tokens now OR together, and the line says `fmtl=<mask> tokens=N` when there
was more than one. A test can add `fmtl=400000` beside the base `fmtl=40001`.

Bit 22 (`400000`) is `FUXI_MTL_LATE_FRAMES=1`. With the frame trace on
(`fuxigpuframetrace=1` or `2`; without it there are no samples and the policy
line says so), every backboardd surface frame whose interval (its command
buffer's creation after the previous surface frame's) is over 12.5 ms gets one
line of its own stages, at most 8 per 5 s (`FUXI_MTL_LATE_FRAMES_US`,
`FUXI_MTL_LATE_FRAMES_MAX`):

    FUXI MTL: late frame n=4 interval=16.67 wall=7.30 cpu_before_commit=1.50 ...
    FUXI MTL: late frame+ ... gpu_exec=6.38 gpu_wait=6.23 retire_queue=... retire_lock=1.81 ...
    FUXI MTL: late frame+ ... top=plan_bounds:0.81,record:0.40,descriptors:0.30 prev=wall:7.20,... late=1/4 dropped=0

The fields carry the window line's names, in ms, for this one frame:
`cpu_before_commit` (CoreAnimation's encode, creation to commit),
`submit_queue`, `backend_lock`, `slot_wait`, `backend_submit` with `record`,
`desc`, `pipe`, `queue_submit`, `gpu_exec`, `gpu_wait`, `retire_queue`,
`retire_lock`, `surface_release`, and counts (`draws`, `passes`,
`pipeline_barriers`, `image_barriers`, `plan_misses`, `pipelines`,
`descriptors`, `allocations`, process-wide `sync_compiles` since the previous
frame). `top=` names the frame's three longest submit phases (the fence wait
left out; it is `gpu_wait`). A frame mostly starts late because the one before
it ended late, so `prev=` gives that frame's wall, encode, submit, GPU, retire
lock and surface release. `late=L/F` counts late and all frames since start,
`dropped=` the late frames the rate limit left out since the line before. The
line is cut at 200 characters like the window line: delete every
`\nFUXI MTL: late frame+` to rejoin it. Printed after the gate lock.
