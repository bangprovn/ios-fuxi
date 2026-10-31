# GPU-SCANOUT: finished IOSurface to command-mode panel

2026-10-23. Offline implementation and host qualification. No phone access,
firmware, device log, deployment, push or CI run. Rebuild **both the kernel and
FuxiMetal** for this option. NativeAdreno ABI 2, the external translator pin and
external Mesa build remain unchanged. No Mesa/translator/GPL code is added.

## Exact current path and cost

The relevant final sources are `tools/fuxi-metal/Frontend.mm`,
`tools/fuxi-metal/Backend.hpp`, `xnu/fuxi-adreno/fuxi_gpu_abi_backend.inc`,
`xnu/fuxi-scanout/SweetSurface.cpp` and
`xnu/fuxi-unixnu/SweetFramebuffer.cpp`. The framebuffer/surface baseline comes
from patches 0068, 0079, 0082, 0087, 0093, 0096, 0098 and 0106.

1. CA/backboardd wraps its composed BGRA IOSurface with
   `newTextureWithDescriptor:iosurface:plane:`. FuxiMetal retains it and imports
   its pages as a Vulkan linear image. This path has **no full-frame upload or
   readback memcpy**. No-copy *input* buffers can separately use M9 shadows;
   those are not the composed IOSurface-to-panel copy.
2. `FuxiCommandBuffer::commit` synchronously calls `Backend::submit`, including
   `vkQueueSubmit`, a bounded `vkWaitForFences` and host invalidation. Drawable
   presentation callbacks run after completion. Turnip submits through ABI 2.
   Imported mappings are prepared/wired. The kernel uses CIVAC over every mapped
   import before submit, then IVAC after CP fence + both-thread idle, before
   returning fence success; watchdog retirement performs the same handoff.
3. CLCD selector 5 copies the 0x46c-byte swap record into a **three-record FIFO**
   (2026-10-27 correction below). Selector 6 SwapWait is still a stub, not a
   display fence. A nominal 60 Hz workloop tick consumes the oldest ready record; `sweetfb_hz` can reduce this rate.
   It crops logical 1125x2436 to panel 1080x2400, source origin **22,18** for a
   full logical frame. Pitch comes from the surface; no scaling or swizzle.
4. The existing `sweet_surface_gpu_copy` already supports a one-layer BGRA
   blit, with four prepared backing-cache entries and a retained in-flight
   source. However, the default Metal wrapper holds a writable IOSurface lock
   for its **entire texture lifetime**. `gpuWriters != 0` rejects that blit.
   A busy Turnip client also rejects it. Both cases normally reach the CPU
   path. Therefore “backboardd rendered on GPU” alone does not establish GPU
   presentation; the per-frame/summary path counters below establish the copy.
5. CPU fallback runs **2400 row memcpys of 4320 bytes** for a full panel. Source
   is ordinary cached IOSurface RAM; destination is the existing firmware
   framebuffer at physical 0xb8000000, mapped write-combined/Normal NC by
   SweetSurface. A full copy reads and writes **10,368,000 bytes each**:
   at least **20,736,000 bytes of traffic/frame**, excluding cache/padding and
   other rendering work. That is 62.208–559.872 MB/s at 3–27 frames/s, or
   1.24416 GB/s at 60. A 1125x2436 source has 10,962,000 visible bytes before
   cropping; row/allocation padding is not copied. CPU cost cannot be derived
   from CPU GHz alone. For *illustrative effective aggregate bandwidth* of
   2/5/10 GB/s, that traffic alone costs 10.368/4.147/2.074 ms. These are
   estimates, not measured Fuxi bandwidth or device copy times.
6. The old GPU path maps prepared source pages into four 16 MiB SMMU windows,
   reuses generation-checked mappings, force-cleans intersecting CPU cache
   lines to PoC, then emits one batch. Pitch units require interleaved row
   classes (at least two, at most sixteen); both pitches must become 64-byte
   multiples. A normal aligned full-panel pitch needs two rectangle blits.
   Resolve/CCU/UCHE clean events precede its timestamp fence. Poll observes
   fence, RPTR/WPTR and idle; 100 ms failure uses the existing isolation and
   quarantine policy. Its old wait measurement includes up to a 16.667 ms
   display-tick observation delay. The existing 600-frame split statistics
   already report pin, PFN lookup, map/TLBI, clean, submit, wait and CPU runtime.
7. ABL leaves INTF1 command-mode tear autorefresh on. The generic
   `fuxi_display_kick` skips manual CTL START in that case (0087). Thus an old
   in-place CPU **or GPU** copy can be read by the display before completion;
   merely delaying that kick does not establish display ownership. NC scanout
   needs no CPU writeback; DSB orders writes but cannot stop autorefresh.

`fuxi_unixnu_diff=1` can replace eligible CPU copies with the already documented
shadow-diff stores; it is a separate default-off option. Neither that option
nor the new frame logs converts the SwapWait stub into a retirement protocol.

## New option and ownership sequence

`fuxigpuscanout=1`, with **`sweetfb_noconsole=1`**, enables the new handoff.
Continue using the existing loader GPU reservation, `fuxigpu=1 fuxigpuabi=1`,
backboardd admission/configuration and `fuxibbgpu=1` (or survey mode 2). No root
image, userland policy, GPU vote, firmware or build pin is changed here.
Missing quiet-console opt-in logs `unavailable` and keeps the existing path.
Before GPU readiness the old path remains available. With the option absent,
all existing presentation behavior and lifetime surface locking remain.

The new FuxiMetal wrapper unlocks an imported surface after successful import,
while retaining its use count, memory and object. It takes a deduplicated writable
IOSurface lease for every participating imported texture/view across each
synchronous submission, including completion/cache retirement. CPU texture
transfers take the same lease. Normal completion and pre-submit failure release
it **before callbacks**. Unknown GPU ownership preserves the lock and allocation.
The surface writer trap waits on the producer thread for any in-flight display
copy; the display callback never sleeps. A retained texture is consequently
eligible for display copying between submissions. A matching new bundle matters:
an older bundle's permanent writable locks can defer this lane indefinitely.

After GPU readiness, CLCD claims the inherited display:

- Suppress generic console/manual kicks, clear INTF1 AUTOREFRESH_CONFIG bit 31,
  preserve the other bits, and allow **20 ms** for the inherited transfer.
- Only acquire the framebuffer when CTL START is idle and the INTF1 write-line
  value is zero or at least 2400. Check that autorefresh remains disabled.
  This uses the same register facts as the existing Fuxi inherited-display
  probe; no GPL implementation is copied. There is **no new DSI command,
  panel reset, address switch or display SMMU takeover**.
- Under the surface lock reserve the idle GPU ring across cache lookup,
  preparation, mapping/TLBI and submission. Observe a completed client first,
  including its cache retirement/root restoration. A live producer/client
  defers the latest swap rather than triggering a CPU scan copy. Client submit
  returns busy while the short reservation is held, then the normal in-flight
  copy flag excludes client work until the copy fence retires.
- GPU blit the existing prepared pages into the unchanged scanout buffer using
  the existing BGRA/stride/crop encoder, PoC handoff and clean-before-fence
  epilogue. Retain the exact source through retirement and optional comparison.
- After successful copy-fence retirement, DSB and issue **one manual CTL START**.
  The display is allowed to read only now. Wait another 20 ms plus idle-status
  observation before any next framebuffer write. CPU fallback and power-off
  black fill use the same acquire/publish guard. Black is explicitly sent once.

While copying/draining, poll at roughly 1.04 ms (nominal vsync period / 16).
Vsync counts/notifications retain their separate 60 Hz deadlines; fast polls do
not manufacture extra vsyncs. The demand-timer option still disarms when idle.
An uncertain display drain/change/error holds the panel and stops framebuffer
writes. Failed GPU DMA drain also holds it; no CPU fallback over uncertain DMA.
A confirmed GPU drain can replay through CPU. These paths do not restore AR or
retry panel initialization; a reboot without the new option restores baseline.

The 20 ms guard is deliberately conservative, **not a qualified TE/done IRQ**.
Even with a very fast copy, it limits throughput to below 50 presentations/s;
GPU completion/poll and scheduling reduce that further. It must be timed and
visually qualified on Fuxi. This source increment establishes no FPS, latency or
power improvement. Direct scanout of the composed allocation is not selected:
its pages can be noncontiguous, its pitch/crop differs, and there is no qualified
DPU buffer-address/mapping/latch/retirement API in this driver.

The physical default framebuffer IOSurface remains exported for ABI compatibility.
This lane assumes CA uses its offscreen composed surface and that no other client
writes directly to scanout. It does not revoke existing user mappings or control
uncooperative direct writers; those remain outside this ownership contract.

## Diagnostics and exact log forms

- `fuxigpuscanoutlog=1`: per-copy logs (only with this boot-arg; `fuxigpuscanout=1`
  alone prints just the summary) and a cumulative summary every **120**
  completed copies, independently of the new handoff. Use this alone for the
  existing-path baseline. The handoff enables these logs automatically.
- `fuxigpuscanoutcheck=1`: with the handoff, compare **all pixels of the first
  four successful GPU copies** against the exact CPU row-copy oracle. It reads
  the retained source at its crop/pitch and scanout with aligned volatile 32-bit
  loads, including alpha. It does not overwrite scanout with a CPU result.
  Mismatch disables the GPU, drains/quarantines and never publishes that GPU
  result; a confirmed drain permits CPU replay. No other check value enables it.

Successful full-panel forms (`N`/`T`/`P`/`A`/`M`/`D` are runtime numbers):

```text
FUXI MTL: GPU scanout surface locks per submission
FUXI SCANOUT: requested, fence then manual transfer, drain_us=20000 check=4
FUXI SCANOUT: owned, autorefresh disabled; draining inherited transfer
FUXI SCANOUT: pixel frame=1 pixels=2592000 mismatches=0 first=0,0 check_us=T
FUXI SCANOUT: frame=N path=GPU bytes=10368000 copy_us=T prepare_us=P
FUXI SCANOUT: summary frames=120 GPU=N CPU=N copy_avg_us=A copy_max_us=M deferred=D
```

Pixel lines repeat for frames 2, 3 and 4 only. Without the checker the request
line ends `check=0` and there are no pixel lines. Baseline/valid fallback copies
log `path=CPU ... prepare_us=0`. `frame` and summary count completed copies,
not proof that the panel displayed them. GPU `copy_us` is elapsed wall time
from doorbell return through observed retirement (including observation delay
and optional comparison); `prepare_us` is the preceding surface/map/clean/submit
call. CPU `copy_us` measures the CPU scan copy/diff operation. The guard wait,
compositor GPU render time, FIFO queue delay and transfer duration are
not included in copy time. `deferred` counts callback deferrals, not dropped
frames; consult `SWEET FB: stats` for FIFO overflow drops and ready skips. `check_us` exposes
the optional slow device-memory readback cost; disable checks for benchmarks.

The ready line labels the programmed GPU vote and uses GPU-CLOCK’s selected
rate in kHz. With the default level:

```text
FUXI GPU: ready, BGRA strided rects + one fence, 4 cached backings, requested 220000 kHz GPU vote (not measured), async one-in-flight, no timer sleep
```

Refusal/failure forms:

```text
FUXI SCANOUT: unavailable, requires sweetfb_noconsole=1; existing present path
FUXI SCANOUT: stopped reason=transfer drain timeout; panel held, no framebuffer writes
FUXI SCANOUT: stopped reason=autorefresh changed; panel held, no framebuffer writes
FUXI SCANOUT: stopped reason=GPU DMA drain failed; panel held, no framebuffer writes
FUXI GPU: disabled permanently: scanout pixel mismatch (0x604), CPU present
```

Read/disable/status/START access failures also print the same stopped form with
`read autorefresh`, `disable autorefresh`, `read transfer status`,
`publish transfer status`, or `start transfer`. Do not treat a completed-copy
line or a zero-mismatch readback alone as panel/DSC qualification.

## Provenance, patch ownership and checks

Read-only reference: UniXNU superproject
`fb6aad6ab1f50199a4d99d2da1d0ca403b067633` and its populated NativeAdreno and
NativeMetal sources. `GpuScanoutPresenter.h` imports a finished BGRA frame and
scanout, GPU-copies matching/strided rows and waits for completion;
`GpuScanoutClient.h` verifies pixels and completes/abandons the offered frame.
Those files declare **MIT**, despite the superproject's BSD-2-Clause-Patent
licence. The adapted comparator retains its SPDX/copyright/provenance, with the
MIT licence in `xnu/fuxi-adreno/LICENSE.NativeMetal` and generated kernel patch.
The local NativeMobileDisplay directory is empty; its earlier pinned source
contract is recorded in [P-UNIXNU](unixnu-port.md), whose BSD notices remain.
No new upstream display source was fetched or executed. NativeAdreno's existing
ABI/fence/import machinery is reused. Fuxi's panel gate and SurfaceLease are
original implementation; no sheng register ownership or private ABI is assumed.

Canonical GPU sources and new include/licence are synchronized into **0098**
by `sync-fuxi-metal.py` (0096 remains standalone); **0100** remains synchronized.
Canonical framebuffer changes regenerate **0106** with `sync-fuxi-unixnu.py`.
**0108** contains the final SweetSurface changes and guarded console kick,
regenerated/checked by `sync-fuxi-scanout.py`. `--check` validates their generated
bytes and patch application. There is no Mesa patch to regenerate.

Host commands (warnings as errors and ASan/UBSan where applicable):

```sh
python3 tools/sync-fuxi-adreno-shared.py --check
python3 tools/sync-fuxi-unixnu.py --check
python3 tools/sync-fuxi-scanout.py --check
python3 tools/test-fuxi-gpu-scanout.py
python3 tools/test-fuxi-adreno.py
python3 tools/test-fuxi-metal.py
python3 tools/test-fuxi-hostimport.py
python3 tools/test-fuxi-gpu-k1.py
python3 tools/test-fuxi-unixnu.py
python3 tools/test-fuxi-metal-frontend.py
```

The scanout suite compiles the actual gate, final surface cache and framebuffer
methods: first/subsequent transfer drain, no START before fence, no generic kick
while owned, busy/reservation exclusion, no write during deferral/blanking,
fail-closed access/timeout/AR changes, full 1080x2400 centered CPU pixel oracle,
alpha/first/last mismatch, padded small rectangles, first-four bound and summary.
The existing submit suite independently decodes actual PM4 and checks 1022
pixel-exact copies, pitches/phases, noncontiguous mappings, cache ranges,
fence/deadline and quarantine. Portable SurfaceLease tests cover opt-in parsing,
deduplication, rollback, cap and poison retention. Mac `ScanoutFrontendTests.mm`
is wired into both normal and compile-only harness modes; it covers actual
import/view/transfer/commit/callback selectors with modeled surface locks.

Optional `--xnu-buildtree "$XNU_BUILD"` on the scanout suite checks complete
`fuxi_adreno.c`, `SweetSurface.cpp` and `pe_serial.c` against saved XNU
headers/flags, applying only required console text deltas in a temporary tree.
The unixnu suite's same option checks the full framebuffer. These checks passed
on Linux with the saved 8796 tree; no saved-tree script or reference was run.
Native Mac frontend compilation/execution, complete kernel link/build, real
GPU/cache/DPU/TE/DSC behavior and timing remain unverified.

## Maintainer device qualification plan (not performed here)

1. Run the complete Mac frontend harness normally and with `--compile-only`,
   build/sign the matching kernel and FuxiMetal, retain existing ABI-2 Turnip
   and external translator. Keep the existing backboardd admission, strike
   handling and survey-first policy; do not admit SpringBoard.
2. Baseline boot with the new handoff **absent** and
   `fuxigpuscanoutlog=1`, normal existing GPU/Metal arguments and quiet console.
   Record copy path counts/times, rendering `gpu_ms`, late frames, dropped swaps,
   CPU/power/thermal behavior over equivalent interaction. Confirm the default
   lifetime-lock path still works. Do not interpret CPU GHz as GPU MHz.
3. Fresh boot with `fuxigpuscanout=1 fuxigpuscanoutcheck=1
   sweetfb_noconsole=1` and existing GPU/backboardd arguments. Require the Metal
   per-submission lock line, ownership line, **four zero-mismatch full-panel
   comparisons**, increasing `path=GPU` copies and 120-copy summaries. Inspect
   text/alpha/gradients, animation, rotation/crop edges and static DSC stability.
   A stuck/mismatched/held panel, timeout, permanent deferral or any GPU fault
   fails qualification. Reboot without the option; do not blindly re-enable AR
   or repeat manual START while a transfer may be active.
4. Reboot with the checker absent for sustained timing/power comparison. Account
   for the 20 ms guard and roughly 1 ms active polling. Require a meaningful CPU
   cost reduction and acceptable cadence; do not promise 60 fps with this guard.
   Validate 60 Hz notification counts separately from active polling. Exercise
   lock/unlock, power-off black transfer, rapid wake/swap replacement, idle
   demand timer, texture reuse/destruction and producer/display contention.
5. In a separately controlled fault run, verify ordinary confirmed GPU failures
   replay safely on CPU, unknown DMA/display ownership holds writes, existing
   failure/strike CPU recovery remains effective, and the next no-option boot
   restores the inherited path. No device fault test was run for this change.

## Drain floor and wait counters (`fuxiscandrain=<us>`)

OBSERVED (scripted gestures, backboardd on FuxiMetal with two frames in flight):
- CA swaps at about 34/s, but the panel shows about 24 frames/s, and about 30%
  of swaps are superseded before they are shown.
- The scanout summary counts about 8.5 deferred acquire polls per shown frame,
  at about 1 ms each.
- After every START, acquire waits a fixed 20 ms floor (`FG_SCAN_DRAIN_US`)
  before it tests CTL START and the write line. It also waits for an idle GPU
  ring (`fg_busy`).

GUESS: the floor and the ring wait, not CA rendering, set the shown frame rate.

`fuxiscandrain=<us>` (4000..20000; the default stays 20000) lowers the floor.
The START bit and write-line checks still gate every copy. Every 600 starts,
`FUXI SCANOUT: waits per 600 starts ring=… floor=… transfer=…` reports the
polls each gate held. A large `transfer` count at a low floor means the panel
transfer itself is still running; tearing would show there first.

## Tear-check registers, TE rate and opt-in TE gate (`fuxiscante=1`)

OBSERVED (code + logs): the `rd=` value in earlier `owned` lines came from
INTF+0x2a8. The SM8550 SDE driver (`sde_hw_intf.c`) names that register
`INTF_TEAR_WR_PTR_IRQ`, a static setting that `sde_encoder_phys_cmd.c` programs
to 1. Both M114 and P117 logged `rd=0x1`, which matches. The read pointer is
`INTF_TEAR_INT_COUNT_VAL` at +0x298: the high 16 bits count TE frames and the
low 16 bits are the line, which reloads to `VSYNC_INIT_VAL` (+0x294) at each TE.

New log lines:
- At ownership: `FUXI SCANOUT: tear sel= en= vsync= height= init= thresh=
  start_pos= wr_irq=`. These are the raw ABL tear-check settings.
  `vsync & 0x7ffff` is `vsync_count`, from which the configured rate is
  `19.2 MHz / (vtotal * vsync_count)`, with vtotal 2684 for this panel's DTB
  timing. This was an estimate from Android's nominal geometry, not a
  calibration of ABL's inherited counter; see the measured baseline below.
- Every 600 STARTs: `te=` (TE-gate polls), `te_hz=` (TE frames per second
  measured over the window) and `gaps=a/b/c/d`. The gaps are START-to-START
  intervals of at most 1, 2 or 3 sixty-Hz periods, or longer (idle).

With `fuxiscante=1`, acquire skips the fixed floor. After each START it waits
until the TE frame count changes and the line is at least 64 past the reload
value. Then the usual START-bit and write-line checks run. The count is read
after the START write, so a TE that races the START only adds a wait. If no
usable TE arrives within 40 ms of a START, the gate logs `te counter stalled`
and uses the default 20 ms floor for the rest of the boot. An explicit
`fuxiscandrain` still applies on top of the TE gate.

GUESS: TE counting and reload behaviour follow the SDE driver's use of these
registers. This has not been checked on the device. Boot with the default
first, and only enable the gate after `te_hz` reads 60 or 120.

## Opt-in panel 120 Hz (`fuxipanel120=1`, default off)

The supplied device baseline is **`te_hz=60.4`, `vsync=0x180085`,
`init=0x960`**: ABL leaves this panel at about 60 Hz. A fast DSI link or a
120 Hz timing in the Android tree does not establish the current panel rate.
This change has host-model coverage only; no device, full kernel build or
DSI/DSC qualification was performed for it.

Enable with `fuxipanel120=1 fuxigpuscanout=1 sweetfb_noconsole=1` plus the
existing GPU/ABI boot arguments. Only the exact value 1 enables it; it is
ignored without the scanout handoff. With the option absent or zero, DSI0
is not mapped or accessed, no switch commands or tear-check writes occur,
and the existing 60 Hz gate, floor, histogram and logs retain their behavior.
The compositor's existing nominal 60 Hz notification/workloop schedule is
unchanged; panel refresh is separate from rendered or presented frame rate.

Panel command provenance: the requested local **`dtb/` decompiled DTB is
unavailable in this worktree**, and the main checkout's `dtb/` is empty.
Consequently a source DTB filename and device-specific byte comparison cannot
be claimed. The corroborating source is
[`qcom/display/display/dsi-panel-m3-38-0c-0a-dsc-cmd.dtsi`](https://github.com/LineageOS/android_kernel_xiaomi_sm8550-devicetrees/blob/43b5ada5654ed84c25f74789bc8f6326b7bacd20/qcom/display/display/dsi-panel-m3-38-0c-0a-dsc-cmd.dtsi),
node `qcom,mdss_dsi_m3_38_0c_0a_dsc_cmd`, child
`qcom,mdss-dsi-display-timings/timing@1`, property
**`qcom,mdss-dsi-timing-switch-command`**. It matches the task's eight
120 Hz writes exactly. Its sibling
`qcom,mdss-dsi-timing-switch-command-state` is `dsi_hs_mode`.
The seven-byte descriptors are `type, legacy-last, VC, flags, wait-ms,
length-hi, length-lo`, followed by the payload:

```text
39 00 00 40 00 00 03  F0 5A 5A
15 00 00 40 00 00 02  60 00
39 00 00 40 00 00 04  B0 00 30 BD
15 00 00 40 00 00 02  BD 04
39 00 00 40 00 00 04  B0 00 16 F2
39 00 00 40 00 00 03  F2 01 70
15 00 00 40 00 00 02  F7 0F
39 00 00 00 00 00 03  F0 A5 A5
```

`0x39` is DCS long write, `0x15` is DCS short write with one parameter.
Every switch delay is **zero**. Flags `0x40` batch the first seven packets;
the final flags `0x00` terminate the batch. The 60 Hz `timing@0` property
differs only at `60 08`; `timing@2` is 90 Hz. All three timings specify four
lanes at 1,100,000,000 bit/s and `qcom,mdss-mdp-transfer-time-us = 7300`.
The per-timing **`qcom,mdss-dsi-on-command`** also contains the same frequency
select block, but includes full initialization: `0x05` DCS short writes
`11 00` (sleep out, wait byte `0x78` = 120 ms) and `29 00` (display on,
zero delay), as well as a later `0x39` `F7 0F` with wait `0x14` = 20 ms.
These initialization commands and delays are **not** replayed by this mode
switch. The timing-switch property is the source for the implemented batch.

The original implementation uses register facts from Qualcomm
[`dsi_ctrl_hw_cmn.c`](https://github.com/LineageOS/android_kernel_qcom_sm8550-modules/blob/d00477fbff4a83babbee37b2ea56fb0ded793eec/qcom/opensource/display-drivers/msm/dsi/dsi_ctrl_hw_cmn.c)
(`kickoff_fifo_command`, `reset_cmd_fifo`, DMA-done status),
`dsi_ctrl_reg.h`, `dsi_ctrl_hw_2_2.c` (trigger selection), `dsi_ctrl.c`
(embedded packet layout), `dsi_panel.c` (descriptor flags), and
[`sde_hw_intf.c`](https://github.com/LineageOS/android_kernel_qcom_sm8550-modules/blob/d00477fbff4a83babbee37b2ea56fb0ded793eec/qcom/opensource/display-drivers/msm/sde/sde_hw_intf.c)
(tear-counter sequencing). No GPL implementation is copied or vendored.

After disabling autorefresh and claiming START, acquire waits for the inherited
transfer's existing floor/TE gate, CTL START idle and drained write line. Before
the first manual START, it attempts the switch **once** under the GPU/display
lock. DSI0 (`0xae94000`) must also become idle: STATUS at +0x008 includes
command DMA busy bit1 and command MDP busy bit2. The inherited command engine
must already be on, video mode off, tear counter enabled and TPG unused.
The code clears stale DMA-done status, selects software DMA trigger 4 in
+0x084 while preserving MDP/TE fields, resets the command FIFO, writes
TPG_CTRL +0x15c = `0x30006`, and streams the embedded batch to +0x17c.
There are **52 bytes / 13 DWORDs**, plus the hardware-required zero DWORD
for an odd word count; DMA length +0x04c stays 52. The embedded headers carry
LONG and final-LAST flags; three-byte long payloads pad with `ff`.
DMA_CTRL +0x03c selects embedded, unicast HS. A barrier precedes the
**DMA trigger +0x090**; **MDP trigger +0x094 is never used for DCS**.
The code polls INT_CTRL +0x110 DMA_DONE bit0 and then DSI idle before cleaning
the FIFO and restoring the inherited DMA, length, trigger and interrupt masks.
Only DMA_DONE is acknowledged; unrelated W1C status is preserved.

Each poll is bounded by **20 ms and 2001 samples**. A timeout logs
`FUXI SCANOUT: panel120 timeout phase=... offset=... status=...`, stops the
attempt, holds scanout and never retries or issues START. Before commands are
sent the panel and tear configuration remain at 60 Hz. **After a trigger,
a timeout cannot guarantee that the panel stayed at 60 Hz:** it might already
have received `60 00`. The code leaves 60 Hz accounting, logs that panel state
is uncertain, and requires a reboot instead of sending a speculative rollback.
MMIO errors, controller error status after transmission and tear-check readback
failures also hold publication. A refused unsupported setup sends no commands and continues
with the inherited 60 Hz mode.

After a successful command transfer, tear-check programming disables
**VSYNC_COUNTER_EN, bit19 of INTF+0x288**, writes the new count and preserved
height (+0x28c), and re-enables the counter. Interrupts are masked only around
these four MMIO writes to meet SDE's single-buffer update constraint. The
TEAR_CHECK_EN register (+0x284), external TE bit20, init, thresholds and line
geometry remain intact. With the clock and geometry unchanged, line period is
proportional to the low-19-bit count, so doubling refresh uses
`floor(133 * 60 / 120) = 66`: **`0x180085 -> 0x180042`**, with `init=0x960`
unchanged. Truncation produces a local counter rate about 2.015 times the old
one; external TE supplies frame reloads. The earlier estimate of 119/59 from
19.2 MHz and nominal DTB vtotal 2684 does not match the observed ABL count 133.
This implementation uses the measured inherited count ratio, not that estimate;
the `te_hz` measurement remains the device qualification criterion.

Only after successful register readback does the active period become
**8333 us**. The default 20 ms drain/fallback floor becomes **10 ms**, keeping
the same 1.2-period margin; an explicit `fuxiscandrain=<us>` retains its units
and value across the mode switch. `fuxiscante=1` keeps its 64-line margin (half the time with the
adjusted counter) and uses a **20 ms** stall deadline instead of 40 ms. Gap
buckets become at most 1/2/3 actual panel periods, then longer. The TE-rate
window restarts after the switch so it does not mix 60 Hz and 120 Hz samples.
The new floor alone still limits presentation below 100 frames/s; this feature
does not promise 120 rendered/presented frames/s.

Verification on device, to be performed separately by the maintainer: first
record the no-option `te_hz` baseline. Boot with the opt-in arguments and look
for `FUXI SCANOUT: panel120 begin ...` followed by
`FUXI SCANOUT: panel120 switched vsync=0x180085->0x180042 ... period_us=8333`.
Require the subsequent 600-START summaries to show **`te_hz` around 120**,
then qualify DSC stability, image quality, TE gating, wake/blanking, cadence,
power and thermals. A controller-done log alone proves no physical TE rate.
An incorrect sequence can **hang DSI or blank the panel and needs a reboot**;
boot without `fuxipanel120` to recover the ABL baseline. Do not retry in place.

Host checks: `python3 tools/test-fuxi-gpu-scanout.py`,
`python3 tools/sync-fuxi-metal.py --check`,
`python3 tools/test-fuxi-unixnu.py`, and
`python3 tools/test-fuxi-adreno-mapping.py`. These exercise production C with
modeled MMIO: exact packet bytes and FIFO order/padding, boundary admission,
one attempt, stale completion, frozen-clock and busy/done timeouts, MMIO
failure, tear-counter disable/update/enable and readback, 60/120 Hz timing,
and DSI mapping only with opt-in. They cannot qualify actual hardware.

## TE-locked compositor heartbeat (`fuxivsyncte=1`, default off)

The supplied 60 Hz device observation is `te_hz=60.4`, while CLCD's existing
16,667 us notification timer has an unrelated phase. Backboardd's roughly
60 Hz swaps can therefore miss the upcoming TE: the supplied 600-START sample
had gaps of 1/2/3/4+ ticks = 225/287/62/26, about 29 displayed frames/s. The
phase explanation is a hypothesis; host qualification does not establish a
device improvement.

`fuxivsyncte=1` opts CLCD into the panel clock while scanout is owned. The
scanout API `fuxi_gpu_scanout_te` reads INTF+0x298's high16 frame counter and
maintains a **caller-owned** observer in mach-absolute units. There is no GPU
mutex, allocation, wait, START or register write in the observer. Polls bracket
each counter change; the midpoint estimates TE. Two consecutive narrow
brackets establish validity. The measured interval feeds a 1/8 period filter;
a 1/4 phase correction smooths polling jitter. Counter wrap is handled modulo
65,536. The inherited mode's period seeds acquisition; the successful
`fuxipanel120=1` switch changes that seed and forces fresh acquisition. The
notification period then follows measured TE rather than assuming 60 Hz.

A copy can block the timer across an edge. An established estimate may bridge
up to four confirmed counter increments when its predicted edge fits the poll
bracket, allowing one poll interval of error. Such a wide bracket does not
update the period filter; subsequent narrow brackets measure elapsed periods
across the gap. An implausible jump, reset, clock reversal, mode change or MMIO
failure invalidates the estimate. An unchanged counter expires after 1.25
estimated periods since its last observed change. No ownership means no TE
register read. Invalid data uses the existing absolute notification timer and
can reacquire automatically; there is no prediction through a frozen counter.

CLCD schedules notifications at **estimated TE + offset**, with a default
**7,800 us**: the supplied 7.3 ms transfer time plus 0.5 ms margin. Tune with
`fuxivsynctoff=<us>`, inclusive range **0..16000**; missing or invalid values
retain 7,800 us. Offset zero fires on the first poll that confirms TE. Offsets
longer than a measured period wrap modulo that period, preserving one phase
per panel cycle. Each confirmed TE gets at most one count increment and
notification; fence/ownership polls add none. An armed notification survives
observation of the following TE just before its deadline. Long missed intervals
are skipped without catch-up bursts; entering or leaving fallback avoids an
immediate duplicate tick.

With subscribers, the owned timer polls at about period/16 even without a
dirty swap (about 1 ms at 60 Hz, 0.5 ms at 120 Hz). Existing demand-idle behavior
still stops when nothing needs the timer. **With `fuxivsyncte` absent, zero or
any value other than 1, the old deadlines, notification counts, polling and
presentation path remain unchanged, with no new TE reads or diagnostics.**

For device qualification, add these arguments to the existing GPU setup:

```
fuxigpuscanout=1 sweetfb_noconsole=1 fuxiscante=1 fuxivsyncte=1
```

The heartbeat option does not enable the separate `fuxiscante` gate or lower
`fuxiscandrain`. The default 20 ms scanout floor itself prevents sustained
one-TE START spacing at 60 Hz, so retain the TE gate when evaluating cadence.
First compare the same workload with the heartbeat option off. With it on,
look for `SWEET FB: vsync TE requested offset_us=7800`. Every 600 heartbeat
notifications (including fallback), CLCD emits:

```
SWEET FB: vsync te-locked=<n> fallback=<n> phase_us <p50>/<p90> period_us <period>
```

The two counts sum to 600. After acquisition, expect predominantly locked
notifications, period around **16,556 us** for the supplied 60.4 Hz baseline,
and phase percentiles near the chosen offset. Phase is elapsed time modulo
period relative to the latest **estimated** edge, with 128 us histogram bins;
period is the mean measured period over locked ticks in that window. An
all-fallback window reports zero phase/period. Also compare the existing
`FUXI SCANOUT: waits per 600 starts ... te_hz=... gaps=...` lines: the success
criterion under sustained animation is **START gaps mostly 1 tick**, fewer
2/3/4+ gaps, and stable images. A good heartbeat log alone does not demonstrate
that CA rendering, copying and START met the next panel deadline.

With `fuxipanel120=1`, first require the separate panel-switch success and
`te_hz` near 120; expect roughly half the measured period. The unchanged
7,800 us default leaves very little preparation time before the next TE at
that rate. Measure transfer completion and CA/copy time before choosing an
earlier safe offset. A faster heartbeat alone cannot guarantee 120 fps.

Risks include timer dispatch jitter, estimator uncertainty while copies hide
edges, extra polling cost, a transfer time that varies with panel mode, and
CA/copy work exceeding the remaining budget. An early offset can encounter a
still-busy single buffer; a late offset can miss TE. Existing ownership,
transfer and fence guards still gate every write. Validate animation, idle,
blank/wake and fallback/relock as well as cadence; remove `fuxivsyncte` to
restore the prior heartbeat. No device qualification was performed here.

Offline coverage in `tools/test-fuxi-gpu-scanout.py` exercises the actual observer
with counter jitter, wrap, mach timebase scaling, mode changes, stalls, faults
and missed polls. Its combined panel-clock/framebuffer model covers 60.4 and
120.8 Hz, copies crossing TE, loss and reacquisition. `tools/test-fuxi-unixnu.py`
covers offset parsing/bounds/defaults, exact deadlines, pending-tick retention,
count/notification suppression, diagnostics and the unchanged opt-out path.
The kernel build receives the API/header through generated patch **0098** and
CLCD through **0106**; regenerate with `tools/sync-fuxi-metal.py` and
`tools/sync-fuxi-unixnu.py` and keep both `--check` modes passing.

## Opt-in double scanout (`fuxiscandouble=1`, default off)

Supplied device observations, before this change: with `fuxigpuscanout=1
fuxiscante=1 fuxipanel120=1 fuxivsyncte=1`, panel switching succeeds, TE measures
120.8 Hz (8274 us), and CLCD notifications follow TE. CA swaps reach 120 Hz,
but the single-buffer path reports 600 frames in 16187 ms, 798 swaps and 197
not shown. The supplied 600-START window has `ring=0 floor=0 transfer=5369
te=2000`, `gaps=0/313/167/120`, GPU `copy_avg_us=626`, maximum 1246 us.
The same RAM cannot be copied until its approximately 7.3 ms transfer ends;
copy plus polling then misses the next 8.274 ms period. These are the user's
observations of the preceding implementation, **not results of double mode**.

Add `fuxiscandouble=1` to that setup, retaining `sweetfb_noconsole=1` and the
existing GPU/ABI arguments. Only exact value 1 enables it, parsed once during
CLCD configuration, and only with the scanout handoff enabled. No option means
no buffer-B mapping, address flips, flush writes or double-mode diagnostics.
The existing panel120, TE gate, explicit drain floor and heartbeat controls
remain independent. In particular, leaving the default drain floor enabled
still prevents one-TE publication at 120 Hz.

A remains PA **0xb8000000**, GPU VA `G4_FB_VA` (0x41000000). B is PA
**0xb8a00000**, GPU VA `G4_FB_VA+0xa00000` (0x41a00000). Both contain
4320*2400 = **0x9e3400 bytes**. B ends at 0xb93e3400 exclusive, inside the
existing splash reservation 0xb8000000..0xbaafffff, outside XNU-managed RAM.
No allocator RAM or new ADT reservation is used. The GPU maps B with the
same `1u<<6` attributes as A; the fallible kernel NC window rounds B to
0x9e4000 bytes (16 KiB pages). CPU initialization/readback uses aligned
volatile 32-bit accesses, not cached memcpy/DC ZVA into that mapping.

Ownership and copying:

- Claim manual START, disable autorefresh, require initial VIG0 SRC0 (+0x14)
  equal to A, drain the inherited transfer with the existing floor/TE,
  CTL_START and write-line checks, and finish the optional panel120 switch.
  Copy A into B before allowing its first use. Unexpected SRC0/MMIO/mapping
  state holds writes. Subsequent acquisitions check that SRC0 still matches
  the tracked front buffer.
- GPU copies always target the other buffer. Every double-mode GPU copy is
  **full 1080x2400**, expanding the damage coordinates to their full source
  crop (usually 22,18). A source that cannot supply the complete panel is
  refused before submission. CLCD also expands an eligible composed crop
  before calling the GPU, so a confirmed GPU failure can replay a complete
  frame on CPU. No damage union/history is maintained. Existing copy-time
  logs measure the cost; reported GPU bytes reflect the full copy.
- A retired copy becomes back-ready. Publication retries the existing TE,
  explicit floor, CTL_START-bit0-clear and write-line-idle checks, without
  spinning. Then write VIG0 SRC0, write **CTL_FLUSH = read(CTL_FLUSH) | 1**,
  DSB, and CTL_START=1. This preserves all other pending flush bits and adds
  only VIG0 bit0, using the supplied DPU register facts, not Linux code.
  The front/back indices change only after successful START. The old front
  can immediately receive the next GPU copy while the new front transfers.
- CLCD retries publication on timer polls even without another swap; a ready
  frame keeps the demand timer active. The FIFO correction below prevents a
  newer GPU copy from replacing a ready unstarted frame. Copy-in-flight
  exclusion prevents both another copy and START until retirement. The
  existing source retention, cache-clean fence, producer reservation and
  DMA quarantine checks remain in force.

**CPU option: single-buffer fallback to A until the next GPU frame.** CPU copy
and power-off black-fill callers use a distinct acquisition API. It first
waits for GPU retirement and the same transfer/TE gates. With the display
idle, it preserves the newest completed image in A (the ready image if one
exists, otherwise the front), restores SRC0=A with the same flush, discards
any ready frame, and disables flips. Existing `fPixels` writers, partial CPU
copies, diff shadow handling and black publication then use the original
single-buffer guard. The next GPU acquisition re-enters double mode with A as
the front: GPU copies are full frames, so B needs no preparation and is not
scanned while SRC0=A. OBSERVED on chain146: boot itself blanks once
(`SWEET FB: display off` after the first GPU frame), so a permanent fallback
disabled double mode for the whole boot. Transitions log for the first four
re-entries only (`FUXI SCANOUT: double re-entered after CPU frame (N)`).

Every stop after double ownership attempts SRC0=A plus flush before any
possible fixed-address consumer could regain the display. A restore access
failure logs separately. The existing stop policy still holds ownership,
suppresses generic kicks and forbids writes/START until reboot; restoration
alone is not permission to use RAM while GPU/display ownership is uncertain.
Healthy TE observation stays lock-free; an observer MMIO failure in double
mode takes the GPU guard only to run this fail-closed stop/restore path.
A defensive configure/disable request while double ownership is established
also stops/restores and keeps the panel held. There is no runtime
display-release/re-enable operation.

The USB screenshot request **0x4e** now snapshots SRC0 once, accepts A or B,
and reads `SRC0+offset`, bounded to exactly one framebuffer. Pitch/format
checks and the 512-byte request cap are unchanged. No monitor protocol/tool
change is needed. A screenshot assembled from many requests can still span
flips; this is not an atomic whole-frame capture.

New exact log forms (`N`, `P`, `M` and addresses below are runtime values):

```text
FUXI SCANOUT: double=1 A=0xb8000000 B=0xb8a00000 full_copy=1 flush=RMW|1
FUXI SCANOUT: double=1 flips=N back_ready_drops=N publish_retries=N current_src0=P expected=P match=N mismatch=N
FUXI SCANOUT: double CPU fallback, drained; single buffer A until the next GPU frame
FUXI SCANOUT: restore A failed; panel held, reboot required
```

The second line accompanies each existing `waits per 600 starts` line while
double mode remains active. Flips, ready drops and deferred publication polls
reset per window; match/mismatch are cumulative **sample counts**. One
read-only **SSPP_CURRENT_SRC0_ADDR (+0xa4)** sample follows that window's
last START and is compared with the expected new front PA. This register and
its latch timing remain **unverified on device**; a mismatch is diagnostic,
not a reason to guess another address or bypass the transfer guard. A failed
read stops scanout. Existing `copy_avg_us`/`copy_max_us`, `te_hz`, gap buckets
and SWEET FB swap statistics remain the timing comparison points. Ready drops
are distinct from swaps replaced before copying; a completed copy alone is
still not proof of a shown frame.

Host coverage compiles production code with ASan/UBSan and MMIO/PM4 models:
optional/fallible B mapping and identical GPU attributes, initial contents,
alternating non-front copy destinations, full crops from small damage,
refusal of incomplete sources, copy retirement/transfer/TE exclusion,
SRC0/flush/START ordering and preserved flush bits, ready replacement,
600-start sampling, CPU/black guards and newest-image preservation, stop
restoration and faults, ready-only demand polling, and screenshot A/B bounds.
The modeled 120.8 Hz panel with 7.3 ms transfers and 626..1246 us overlapping
copies sustains 600 STARTs with one-period gaps. This is a scheduling model,
**not a device throughput measurement**.

Generated CI inputs: `sync-fuxi-metal.py` regenerates **0098** for the GPU
source/include/header, and `sync-fuxi-unixnu.py` regenerates **0106** for CLCD.
Run their `--check` modes, `sync-fuxi-adreno-shared.py --check` (also checks
0100), and `sync-fuxi-scanout.py --check` (0108 remains unchanged). No H1
source/0103 change is involved. The loader builds the canonical probe source.
Run the related test commands above, including
`python3 tools/test-fuxi-adreno-mapping.py`; the optional saved-XNU syntax
recipes also check the complete GPU and framebuffer translation units.

**Double-mode device results: not yet observed.** Qualify actual SSPP address
latching, the DPU's access to B, cache visibility, DSC/image integrity,
START gaps predominantly one period under animation, dropped-ready counts,
copy timing, CPU fallback, blank/wake and stop restoration. Full-frame copies
increase work for small damage; polling, compositor contention and timer
jitter can still miss TE. Direct clients writing the exported physical
framebuffer remain outside this ownership contract. Bad address/latch facts
can blank or corrupt the panel and require a reboot without the option. No
phone access, deployment, full kernel link, push or CI run was performed.

## FIFO and writer-retirement wake correction

2026-10-27. The [scheduled-wait FIFO correction](metal.md#presentation-fifo-correction)
replaces the newest-wins CLCD mailbox with three retained waiting swap records.
The oldest readable record is selected; pending/active writers leave records
queued, a ready newer record can skip unreadable predecessors, and full-queue
arrival drops the oldest waiting record. The existing all-source read guard is
acquired before dequeue, including CPU/multilayer fallback. Final writer release
queues a coalesced thread call whose workloop-gated action runs the full
presentation step (stage-latency correction below); the IOSurface unlock trap
does no display work. Vsync/poll retries remain.

`fg_scanout_acquire(0)` now refuses a back buffer with a completed image waiting
for publication. CLCD retries that image's TE/transfer-gated START before
selecting the next queued record. CPU/black-fill acquire, double re-entry,
console suppression and fail-closed stop/drain behavior are preserved. A failed
copy replays its exact retained record, not the newest swap. The waiting FIFO,
last accepted frame and one active admission/copy use bounded record storage;
this does not bound Metal's command backlog or promise a device frame rate.

The unchanged 600-frame stats cadence now reports:

```text
SWEET FB: stats N frames in T ms, copy avg A us max M us, S swaps, D not shown queued_pending=P fifo_overflow=O ready_skips=K ownership_defers=H
```

`P/O/K/H` are cumulative. `P` counts refused source read admissions (pending or
active writers, quarantine or failed admission), `O` oldest FIFO overflow drops,
`K` unreadable records skipped for a ready newer record, and `H` fence/ring,
ownership/drain or deferred-publication polls. `D` is only overflow plus ready
skips in that 600-frame reporting interval. Completed frames count successful
publication START/legacy kick, including retry, rather than GPU submission;
copy timing includes preparation plus observed GPU copy retirement, excluding
FIFO and TE wait. The 120-copy `FUXI SCANOUT: summary` cadence is unchanged and
still counts copies; its `deferred` now excludes source-admission attempts,
reported separately as `queued_pending`. START is not optical panel completion.

Both production-code sanitizer suites cover FIFO order, pending retention then
retirement, oldest overflow, ready skip, no read with active writers, wakeup
coalescing/workloop gating, arrivals during copy, exact failed-copy replay and
no back-ready overwrite before TE publication. Related host tests and all
`tools/sync-fuxi-*.py --check` are required. Regenerated patches are **0098**
(GPU acquisition), **0106** (CLCD FIFO/wake/accounting) and **0108** (surface
retirement notification). No frontend or external Mesa/translator changes are
needed. Native frontend execution, kernel link and real panel/cache/latency
qualification remain unperformed; no phone access or deployment occurred.

## Stage latency and direct presentation wakes

2026-10-27. **Device results for this change: not yet observed.** The supplied
`a72b285` observations are 600 shown frames in 22,731 ms (about 26.4/s),
1,216 swaps, 614 not shown, copy average 1,259 us versus about 630 us before
the FIFO correction, TE 120.8 Hz/8.27 ms and a roughly 7.3 ms transfer.
The supplied wait window has ring/floor zero, transfer 3,227 and TE 1,785;
5,012 deferred publications and zero dropped-ready frames. These are baseline
observations, not measurements of the changes below. Cumulative FIFO counters
cannot be divided by one 600-frame interval to derive that interval's rates.

Code audit found no fixed 37 ms/27 fps limiter or mandatory later-vsync hop at
every stage. The actual waits and changes are:

- `AppleCLCD::swap` previously only woke the demand timer, whose first deadline
  is one nominal 16.667 ms tick away; an already armed owned timer polls sooner.
  Eligible scanout swaps now also queue the coalesced presentation worker, so
  a source already retired before swap arrival need not wait for that timer.
- `writerRetired` still queues a thread call and `writerWake` still enters the
  workloop gate. The unlock trap never copies, polls the GPU or touches DPU.
  Previously `writerWakeAction` scheduled another timer callback with a nominal
  1 us timeout. It now runs `presentStep` directly and rearms only the remaining
  work. This removes that second dispatch, not the safe workloop serialization.
- `sweet_surface_gpu_poll` remains nonblocking. Submission waits for a later
  poll/wake to observe the cache-clean fence, idle and ring pointers. The owned
  timer remains period/16 (about 0.52 ms after a valid 120 Hz estimate). There
  is no new fence spin or per-copy thread call. Dispatch delay is still possible.
- Fence observation already reached `completePresent` in the same callback;
  successful START already reached the next FIFO admission in that callback
  with `fFlushTicks == 1`. Both now live in `presentStep`, shared by timer and
  worker. A successful fence immediately attempts publication, and a successful
  publication immediately attempts the next ready FIFO record. Neither waits
  for the next heartbeat. An asynchronous new copy still waits for observation.
- `fg_scanout_acquire(1)`/`fg_scanout_publish` must still defer for TE, any
  explicit drain floor, CTL START or write-line activity. Back-ready exclusion,
  ring/client reservation, failed DMA quarantine, MMIO/AR failure and stopped
  ownership are unchanged. A deferred START preserves its frame and stamps.
  `sweet_surface_present_begin` still rejects every pending/active writer and
  quarantined source; the FIFO may skip unreadable predecessors as before.
- The all-source reader guard was already surface-specific, but producer lease
  operations shared the long cache lock across prepare/map/PoC clean/submit.
  They now use a separate short lease lock. Cache transactions may briefly take
  the lease lock; producer leases never take the cache lock. A submitting-source
  marker closes the gap before the retained in-flight source takes over. Normal
  writers of that exact source wait through retirement; unrelated surfaces do
  not wait behind preparation or copying. AvoidSync remains a nonwriting pending
  reservation. CLCD releases its admission reader immediately after successful
  GPU submission, or after all synchronous CPU reads, before publication/logging.

The apparent doubling of `SWEET FB: stats` copy time is an accounting change in
`a72b285`: before it, GPU frames recorded `present` entry through asynchronous
submission/return, predominantly preparation and PoC cleaning. It now records
preparation **plus observed fence retirement**, including polling/dispatch and
optional pixel verification. Neither the blit encoder nor its full-copy size
changed in that commit. There is no evidence here that the hardware blit itself
doubled. Keep the broader copy metric; use the separate stages below rather
than dropping retirement from the reported time. Actual GPU execution versus
fence-observation delay still cannot be separated without a GPU timestamp.

Each FIFO record carries swap time and admitted-ready time. Under the lease
lock, each final writer release stamps `gpuReadyTime` using
`mach_absolute_time`, including when no worker runs promptly. Admission takes
`max(swap, latest final retirement of every source)`: already readable sources
have zero swap-to-ready delay. New pending writers still prevent admission;
failed admission, overflow and ready skips produce no shown-stage sample.
Replay uses the same record's original swap and rechecks current readiness.
The active copy retains these stamps through asynchronous retirement and TE
retry. GPU submission and observed clean-fence stamps come from
`fuxi_gpu_copy`/`fuxi_gpu_poll`; START is stamped immediately after its successful
MMIO write, before subsequent status reads or summary logging. A previous START
stamp also includes black transfers, so spacing uses consecutive actual STARTs.
For legacy unowned presentation, the publication completion time substitutes
for START. CPU copies use synchronous read completion for both submit and
fence, giving zero submit-to-fence time; existing GPU/CPU counts identify those
windows. START remains a software handoff, not optical completion.

One sibling line accompanies each existing 600-shown-frame stats report. No
per-frame IOLog is added. Exact format (`A/M` are average/maximum microseconds,
`N` sample counts and `V/P/W` callback counts):

```text
SWEET FB: stages 600 shown swap_ready_us=A/M ready_present_us=A/M present_submit_us=A/M submit_fence_us=A/M fence_start_us=A/M start_start_us=A/M samples=N/N/N/N/N/N callbacks_vsync=V poll=P wake=W callbacks_per_shown_x1000=V1/P1/W1
```

The six fields respectively measure swap-to-ready, ready-to-present entry,
present-to-submit, submit-to-observed-fence, observed-fence-to-START and previous
START-to-START. Samples follow the same order; missing/reversed stamps are
omitted, never subtracted unsigned. Usually the first window has 599 spacing
samples, then 600; an earlier black START can supply the first predecessor.
Counters and averages/maxima reset with each 600-frame window. `V` counts timer
callbacks producing a heartbeat, `P` timer callbacks producing none, and `W`
executed coalesced workers (including idle/refused wakes). Divide each by 600
for callbacks per shown frame; `V1/P1/W1` report those ratios multiplied by
1,000 (integer, rounded down). GPU fence polls within a callback are not extra
callbacks. Wake workers never increment vsync counts or send notifications.
`sweetfb_hz` throttling remains respected: workers only admit frames directly
when `fFlushTicks == 1`, but can still retire/publish outstanding work.

A large swap-to-ready value attributes delay to producer leases/queued renders;
a large ready-to-present value includes ready FIFO backlog, ring admission and
worker/timer dispatch. Present-to-submit includes mapping/cache preparation;
submit-to-fence includes actual GPU work and observation delay; fence-to-START
includes publication gating/dispatch and any optional pixel verification after
the fence stamp. Compare their maxima, spacing and callback
counts with the unchanged transfer/TE wait counters. These measurements are
needed to establish which stage dominates the supplied 37 ms cadence; this
offline change does not prove a new device frame rate or a unique baseline cause.

Host suites exercise the actual shared worker/timer step, ready-before-swap
wake, coalescing, immediate fence/START/next-copy progress without notifications,
TE refusal and exact retry, pending/active/quarantined writer exclusion, unrelated
writers during cache-locked submission and CPU reader holds, exact stamp/avg/max
conversion, omitted bad stamps, consecutive STARTs and two 600-frame resets.
Both suites also syntax-check complete translation units against saved XNU
headers when `--xnu-buildtree` is supplied; the surface header now comes from
its canonical scanout snapshot. Regenerated patches are exactly **0098**
(`sync-fuxi-metal.py`), **0106** (`sync-fuxi-unixnu.py`) and **0108**
(`sync-fuxi-scanout.py`, now owning `SweetSurface.h` as well as its source).

Risks still include workloop/thread-call jitter, extra lock/timestamp overhead,
producer backlog, the unchanged TE/transfer safety gates and unqualified real
GPU/cache/DPU timing. Validate the new stage windows, animation, idle,
blank/wake and fault/stop behavior on a separately authorized device run.
No device access, deployment, full kernel link, push or CI run was performed.

## Honest IOSurface use and SwapWait retirement

2026-10-27. Read-only local iOS 16.7 shared-cache interoperability audit:

- `IOSurfaceClientIsInUse` reads the shared state pointer at client +0x70,
  then tests the aligned 64-bit word at state **+0x18**. Category queries select
  its low/high 32-bit halves. `IOSurfaceClientGetUseCount` instead returns the
  **client-local** count; it is not the global busy query. Increment/decrement
  category functions call traps **0/1**, with `(surface ID, category 0/1)`,
  only on their local zero-to-one/one-to-zero transitions.
- QuartzCore `CA::WindowServer::IOMFBDisplay::can_update_` calls
  `IOMobileFramebufferSwapWait` at **0x1882beb38** and, on success, clears the
  page's swap ID at **0x1882beb40** before releasing its buffer for reuse.
  Its `swap_wait_timeout` helper polls that call with 100 us sleeps.
  `finish_update` also polls at **0x1882d1138**. `IOMFBFlipBook::
  swapped_frame_displayed_p` tests success of flags **0x80000001** at
  **0x18843eb48**. The framework's `kern_SwapWait` forwards selector **6**
  with three scalars `(swap ID, flags, timeout)`; ordinary SwapWait passes
  timeout zero. This reuse path uses return status, not a swap-complete callback.
- QuartzCore's `buffer_is_unused(CABackingStoreBuffer*)` calls
  `IOSurfaceIsInUse` at **0x188335e74** and returns its inverse; the HDR surface
  pool also skips in-use surfaces at **0x188465cec**. These are distinct from
  the display page's SwapWait gate. No Apple implementation or binary is
  included here.

The supplied HEAD timings, ~54 swaps/s, ~27 shown/s and FIFO overflow establish
backlog, not its unique cause. The guess identifies real ABI defects: selector
6 returned immediate success and shared +0x18 stayed zero. Missing asynchronous
swap-complete alone is not the demonstrated cause. No device run proves how
much these defects contributed to those timings.

Traps now account each client's two categories separately, reject invalid IDs,
categories, underflow and overflow, and subtract that client's outstanding uses
on mapping release/close. The shared word includes independent kernel display
holds in category 0, published with one aligned release store. Each category
and the display count are bounded to 65,535. Seeds remain independent. Unknown
DMA retirement sets a sticky in-use indication alongside existing quarantine.

Every accepted layer holds a display use from swap admission through successful
publication (after all source reads/copy retirement), or until its waiting record
is dropped. Duplicate layers have matched independent holds. The copied panel
image does not need the source after publication; retaining `fLast` for redraw
therefore retains the object but no display use. Redraw reacquires holds and
reader admission. Stop drops waiting/last records after DMA observation; an
uncertain source remains quarantined/in-use. Teardown with an unfinished copy
also preserves a sticky in-use indication. Failed admission rolls back only
its own holds. Three waiting records plus one active record remain the bound.

Selector 6 now returns Busy for queued/copying/unpublished records. Bit 31 polls;
blocking calls sleep interruptibly on completion/drop, bounded to 100 ms or a
shorter nonzero timeout. Mode 0 reports safe source release (shown or dropped),
mode 1 requires shown, and modes 2/3 drain accepted outstanding swaps. Dropped
mode-1 queries return Aborted. Unknown/expired IDs and stopped ownership never
return success. A 64-entry status history refuses collisions with live records;
SwapBegin refuses ID wrap. VSYNC notification format/cadence is unchanged.

The existing 600-shown-frame `SWEET FB: stats` line appends
**`reuse_before_show=N`**, a cumulative saturating count of records observed to
have any newer pending or active writer lease before shown/drop accounting.
It counts each record once, across every layer and retry. Existing writers at
swap and later writers after shown accounting do not increment it. A pending
reservation's later active lease can legitimately increment this diagnostic
for the **same render**; a nonzero count alone does not prove a later CA frame.
A separate internal writer epoch advances only when a lease starts with no
outstanding writers. Admission rejects changed epochs under the reader lock,
and drops stale records; pending-to-active transitions within one outstanding
render stay eligible after all writers retire. Existing writer/read exclusion,
fence/cache retirement and TE/transfer publication gates remain mandatory.

Host validation adds `tools/test-fuxi-iosurface.py` and extends the production
framebuffer/surface sanitizer models for category isolation, shared ABI, seeds,
cleanup, caps, reuse/stale detection, FIFO drops, deferred publication, replay,
DMA/stop release and SwapWait status/history/wake/timeout/interruption. Generated
patches are **0106** (framebuffer) and **0108** (surface source/header); no GPU
encoder or frontend change. Real CA pacing, pool pressure, panel/cache behavior
and performance remain unqualified. No device access or deployment occurred.

Validation for this revision: eleven related host suites passed (IOSurface,
GPU scanout, unixnu, adreno, adreno-cache, adreno-mapping, Metal, hostimport,
GPU K1, Metal frontend's portable tests and Metal backend with a software ICD).
All twelve `tools/sync-fuxi-*.py --check` commands passed. Complete framebuffer,
surface and GPU translation units passed syntax checks against saved XNU headers.
Native Objective-C frontend execution requires macOS and was skipped. The two
optional external Turnip harnesses did not start without their required external
patch/tree arguments; no external Mesa tree was patched or modified.

## Source consumption before START and retirement queue isolation

2026-10-27. **New device results: not yet observed.** The supplied HEAD run
shows 600/600 swaps in 13,740 ms (43.7 shown/s), zero overflow/not-shown,
copy average 1,294 us, TE 120.8 Hz and approximately 7.3 ms transfers. Stage
averages/maxima in us are swap-ready 16098/56362, ready-present 5073/31859,
present-submit 647/1656, submit-fence 646/1213, fence-START 5194/18660 and
START-START 22901/476934. The supplied Metal p50s are wall 16.18 ms,
cpu-before-commit 6.04, backend-submit 3.09, pipeline 0.14, GPU execution 6.40,
commit-gap 15.38 and scheduled-wait zero; GPU busy is 22–30%. These describe
the preceding implementation, not this revision. Averages, medians, different
sampling windows and idle gaps must not be subtracted as an exact latency budget.

Short factual conclusions from a read-only iOS 16.7 shared-cache audit:

- Ordinary `can_update_` at 0x1882beb2c..0x1882beb40 loads the candidate
  **page's nonzero swap ID**, passes mode 0 (blocking or bit-31 poll depending
  on caller/configuration), and clears that page ID only on success. It does
  not replace that ID with zero or unconditionally wait on the newest swap.
  `finish_update` at 0x1882d1120..0x1882d1138 polls individual page IDs with
  0x80000000. The displayed-frame checks use 0x80000001, including
  0x1882bed04..0x1882bed10; an optional global gate uses mode 1 as well.
- Pages are 0x30 bytes apart. `finish_update_` at
  0x1882ed374..0x1882ed388 advances the current/next indices modulo the page
  count; `can_update_` scans at most two candidates per invocation. Thus this
  is per-page reuse, not inherently a single global outstanding-swap gate.
  The cache does not establish this device's runtime page count/configuration.
- `kern_SwapWait` forwards the supplied ID/flags unchanged to selector 6 and
  supplies timeout zero. The cache contains the userspace caller, not Apple's
  kernel implementation: it does **not** prove that mere queue acceptance is
  sufficient, nor Apple's special handling of ID zero. Our existing ID-zero
  behavior snapshots the latest accepted ID at wait entry (except drain modes).
  No Apple implementation or binary is included here.

The compatibility conclusion is to distinguish the ordinary source-reuse gate
from the displayed-frame test. `AppleCLCD::consumeRecord` now completes mode 0
only when the **entire source copy has retired**, including cache handoff, or
when the record is dropped. It never completes on queue admission or GPU
submission. `completePresent` calls it before attempting START. CPU/multilayer
copies first finish all reads and release their reader guard; GPU copies first
pass `sweet_surface_gpu_poll`. At that point the self-contained A/B image can
wait for TE without retaining an IOSurface display use. Mode 1 still requires
successful START; dropped records still abort it. Modes 2/3 retain the respective
source/display drain distinction. Unknown IDs, live history collisions, failed
DMA/quarantine, stopped ownership and bounded interruptible waits stay closed.
This supersedes the preceding section's mode-0-until-START timing.

CA can now reuse a consumed page and submit another frame while the preceding
captured image waits for publication. The implementation still bounds storage
to **three waiting records plus one active/unpublished record**, and protects
both queued and consumed/unshown history entries. It grants no unretired-source
reuse. Actual CA depth depends on its page pool and flags; `outstanding_max`
measures live queued plus consumed/unshown swaps on the next run. The new
production test advances the source writer epoch, queues another render before
START, verifies that mode 0 succeeds only for the consumed ID, ID zero selects
the newer ID, mode 1 remains busy, and START publishes the captured older image.
Post-consumption source writes no longer count as stale/reuse-before-show;
redraw reacquires display holds, writer epochs and reader admission.

Latency explanations and changes, from code rather than new device timing:

- `swap_ready` ends at the last `sweet_gpu_writer` decrement, not the hardware
  render fence. It includes commit/worker admission, queueing, exclusive leases,
  backend recording/submission, Vulkan fence observation, backend retirement
  mutex/cache cleanup and frontend lease destruction. Metal's overlap `wall`
  likewise runs from commit through lease release; `cpu_before_commit` is a
  separate earlier interval. The approximately 3 ms submit plus 6.4 ms GPU
  execution do not include those other waits. There is no evidence identifying
  the missing approximately 6 ms as one specific scheduler or fence-poll delay.
- `FuxiCommandBuffer::retireCommitted` previously kept the serial retire queue
  through scheduled-group waits, gate/trace reporting and command-resource
  cleanup. Later fences/lease unlocks could wait behind that tail. It now keeps
  fence/cache retirement, poison handling, lease release and flight-slot release
  on that queue, then dispatches `finishCommitted` to a separate serial queue.
  Scheduled-before-completed and completed-handler ordering remain; GPU slots
  remain bounded. Completed command resources can remain retained behind slow
  finish work. The backend's safety drains and external fence poll are unchanged.
- `ready_present` includes waiting for the previous ready image's START,
  back-buffer acquisition, busy client/ring reservations and wake dispatch.
  `presentStep` already proceeds from successful START to next-copy admission
  in the same workloop action. `fg_scanout_acquire(0)` already permits a copy
  into the old front while the new front transfers. Its back-ready refusal is
  necessary: both A/B images are occupied until publication. Removing it would
  overwrite an unshown frame. The TE/transfer checks remain unchanged.
- Completing mode 0 and dropping display holds at copy retirement removes the
  preceding frame's **fence-to-START wait (about 5.2 ms in the supplied run)**
  from the source-reuse dependency. The next render may overlap that wait and
  be ready sooner when START frees the back buffer. Copy summary logging now
  follows `completePresent`, so it cannot delay source release or the first
  publication attempt. No extra copy, fence spin, DPU write or timer hop is added.

New cheap aggregated diagnostics, with no per-frame logging:

- The existing stats line appends cumulative `wait_mode0`, `wait_mode1`
  (query counts, including drain modes by low bit), `wait_early` (successful
  ordinary consumed/unshown queries, not frames), `wait_id0` (all zero-ID
  queries, including drains), and lifetime `outstanding_max`.
- One `SWEET FB: pacing` line accompanies each 600-shown window:
  `wake_dispatch_us=A/M wake_gate_us=A/M wake_samples=N/N
  blocked_publication=P acquire=A ring=R`. Wake timings measure the earliest
  coalesced request to thread-call entry, then entry to workloop action. Both
  swap and writer-release requests use this call. P/A/R count blocked attempts
  at pending publication, scanout acquisition and copy/ring admission; these
  are not frame drops or disjoint time budgets. Timing totals/maxima/sample
  counts and blocked counts reset with each 600-frame window.
- `fuxigpuframetrace` adds `retire_queue=p50/p90` ms, from retirement dispatch
  to execution. Compare it with existing `submit_queue`, `admission_wait`,
  `gpu_wait`, `gpu_exec`, `retire_lock`, `invalidate`, `retire_host` and
  `surface_release`. It can overlap GPU execution; it is not additive GPU time.

Expected effect: overlap the next CA render with deferred TE publication and
remove completion bookkeeping from subsequent lease retirement. Stable 60/s
is the qualification target, **not an observed result or guaranteed bound**;
120/s still requires the complete render/copy path to sustain an 8.27 ms period.
Qualify matching kernel/FuxiMetal builds with the existing overlap, early-wait,
TE, double-buffer and panel120 options. Compare shown/swaps, START gaps,
stage averages/maxima, new counters, image integrity, blank/wake and quarantine.
No firmware, boot defaults, external Mesa/translator code or GPU vote changes.

Validation: 13 host suites pass: unixnu, GPU scanout, IOSurface, portable Metal
frontend, Metal backend, Metal overlap, Metal frame trace, Adreno, host import,
GPU K1, Metal ABI, Adreno mapping and Adreno cache. Sanitizers/software-ICD
checks pass where enabled. Complete framebuffer, surface, Adreno, console,
vfs and UFS translation units pass syntax with saved XNU headers. All twelve
`tools/sync-fuxi-*.py --check` commands pass. Regenerated patch **0106** is the
only changed patch; regenerated **0098**/**0108** and checked **0100** are
unchanged. Native `.mm` queue/lease tests are inspected but cannot compile/run
on Linux; macOS compilation/execution, full kernel link and device performance
remain unverified. No device access, deployment, push or workflow run occurred.

## Opt-in triple scanout (`fuxiscantriple=1`, default off)

2026-10-27. **Triple-buffer device results: not yet observed.** The supplied
HEAD double-buffer run reports TE 120.8 Hz (8.27 ms), approximately 7.3 ms
command-mode transfers and 650 us full-frame GPU copy retirement. Its stage
line is `swap_ready_us=16889 ready_present_us=9740/48050
present_submit_us=663 submit_fence_us=649 fence_start_us=5694
start_start_us=21368`; pacing reports `blocked_publication=5592 acquire=0
ring=261` per 600 shown frames. About nine publication refusals per shown
frame, approximately 43 shown/s with all frames shown, and approximately 25%
GPU busy describe that preceding kernel. They are not results of this option.

Enable **`fuxiscantriple=1 fuxiscandouble=1`**, retaining the existing
`fuxigpuscanout=1 sweetfb_noconsole=1` and GPU/ABI setup. Only exact value 1
is admitted, parsed once at configuration; triple is ineffective without
exact double opt-in. With triple absent, single/double behavior and log forms
stay unchanged. TE, panel120, drain floor and notification options keep their
existing meanings; triple does not bypass any START gate.

C is PA **0xb9400000**, GPU VA **`G4_FB_VA+0x1400000`** (0x42400000).
Its 0x9e3400-byte image ends at **0xb9de3400** exclusive, within the existing
44 MiB splash reservation 0xb8000000..0xbaafffff, outside XNU RAM. The
fallible NC window is 0x9e4000 bytes, and GPU page-table attributes are the
same `1u<<6` as A/B. C is mapped only for triple; after inherited transfer
drain it is initialized from A with aligned volatile words like B. All GPU
copies still expand to complete panel crops.

The last STARTed buffer is front, including throughout its transfer. A
retired copy is ready until START. The remaining buffer is free. A new GPU
copy can target free while another image is ready, so copying N+1 no longer
waits for N's START. The target excludes both front and ready. If the new
copy retires first, it replaces the older ready image, returns that image's
buffer to free, and counts one drop. Submission or failed copy retirement
never replaces the ready image. There remains only one GPU copy in flight;
START is excluded throughout it. Once retired, publication still requires
previous-transfer idle, the unchanged TE/floor gate, SRC0 then RMW flush then
DSB before START. Only successful START changes front. This favors the newest
completed image and can intentionally drop copied frames under load.

CLCD attempts publication first and, in triple mode with the display on,
continues to next-copy admission while an image is ready. The existing
`blocked_publication` counter counts actual admission refusals, so this overlap
no longer increments it; acquisition/ring refusals keep their existing counters.
The waiting FIFO remains three records; there can additionally be one active
copy and one consumed/unshown image. That ready image retains only its swap ID,
copy duration and stage stamps, independently of the active/redraw record.
Source display holds and mode 0 complete only after CPU reads or GPU copy/cache
retirement. Mode 1 completes at START, or returns Aborted for a replaced ready
swap; previously shown IDs retain their successful status during redraw.
Drain modes, live history protection, stopped ownership and quarantine remain.
Pending publication metadata keeps the demand timer active through low-level
stop cleanup. Shown-stage timing uses the ready image's saved copy stamps even
if a newer copy has failed; the current GPU's stamps cannot replace them.

CPU fallback and black fill first drain both GPU and display with the existing
single-buffer gate, preserve the newest retired image (ready, otherwise front)
in A, restore SRC0=A plus flush, and suspend flips. The next GPU acquisition
re-enters with A front and B/C available. Stop, disable and MMIO failures keep
the existing hold-until-reboot policy and attempt A restoration, including when
C was front. Restoring the address does not permit framebuffer writes while
DMA/transfer ownership is uncertain. Probe request **0x4e** accepts A/B/C from
one SRC0 snapshot and retains its pitch/format, 512-byte and one-frame bounds;
a multi-request screenshot can still span flips.

The initialization line appends `triple=1 C=0xb9400000`. The existing double
summary, at each 600-START sample while flips are active, appends exactly:

```text
triple=1 flips_a=N flips_b=N flips_c=N drops=N
```

`flips_a`, `flips_b`, `flips_c` count successful STARTs targeting each buffer;
`drops` equals `back_ready_drops` (newest-ready replacement or CPU discard).
They reset alongside the existing flip/drop/retry counters. There is no new
per-frame logging. Existing `current_src0`, `expected`, `match`, `mismatch`,
stage, pacing, copy and TE diagnostics retain their meanings.

Host coverage extends `test-fuxi-gpu-scanout.py`,
`test-fuxi-adreno-mapping.py`, `test-fuxi-adreno.py` and
`test-fuxi-unixnu.py`: exact opt-in/dependency, optional/fallible C mappings,
all front/ready target combinations decoded from actual PM4, full-frame pixels,
replacement only at retirement, transfer/TE/fence/SRC0/flush/START ordering,
per-buffer summary/reset, CPU newest-image preservation/re-entry, stop/restore,
A/B/C screenshot bounds, source holds and SwapWait drop/show/drain semantics,
failed-copy replay and publication-stage isolation. Generated patches changed
are **0098** (GPU sources/header/include) and **0106** (framebuffer); regenerated
**0108** is unchanged. The loader uses the canonical probe source directly.

Risks still require device qualification: C access and SSPP address latching,
cache visibility and DSC/image integrity, CPU/blank/wake/fault transitions,
latest-ready drops and mode-1 callers, polling jitter and compositor/ring
contention. Removing the publication dependency does not guarantee 60 or 120
shown frames/s. No phone access, deployment, firmware download, external
Mesa/translator modification, push or workflow run is part of this revision.

Validation: scanout, Adreno mapping/PM4/cache, unixnu, IOSurface, Metal ABI,
host import, GPU K1, portable Metal frontend, Metal frame trace, software-ICD
Metal backend, loader GPU probe and unixnu port2 host suites pass, with
ASan/UBSan where enabled. All twelve `tools/sync-fuxi-*.py --check` commands
pass. Complete GPU, surface, framebuffer, console, vfs and UFS translation
units pass syntax checks against saved XNU headers. The additional Metal
overlap runner cannot start without an installed `glslangValidator`; no shader
compiler was downloaded. Native Objective-C frontend execution is skipped on
Linux. Full kernel link, native frontend and device performance remain
unverified.

## Display-thread CPU cost audit

2026-10-27. **Device results: not yet observed.** The
[Metal CPU audit](metal.md#display-thread-cpu-allocation-and-encoding-cost)
records the supplied profile, bounded texture-memory recycling, operation and
inline-upload storage reuse, exact source-level copy sizes, registry attribution
limit and synchronous fence candidates. The inspected framebuffer property
lookup uses `IOService` with options 0; the supplied work-interval caller offset
does not identify its key in the inspected cache. No framebuffer property or
SwapWait/source/TE/transfer guard changes. Kernel patches regenerate unchanged.

## IOMFBDisplayRefresh, early SwapWait retire, direct scanout delta (UniXNU port)

Source: NativeMobileDisplay (BSD-2-Clause-Patent), `NativeLegacyDisplay.cpp`,
origin/main `8666a99`. Read only. Nothing below has been observed on a device.

### IOMFBDisplayRefresh (opt-in `fuxipanel120=1`)

CoreAnimation's heartbeat falls back to 1/60 s when AppleCLCD publishes no
`IOMFBDisplayRefresh`, even with 120 Hz vsync callbacks. NativeMobileDisplay's
`getRefreshAttributes` reports one fixed rate as 32.32 seconds (`2^32/hz`) and
the original family publishes the dictionary. Ours publishes it directly:

- Property `IOMFBDisplayRefresh` on AppleCLCD, set before `registerService()`:
  an `OSDictionary` with one key, `displayMinRefreshInterval`, an `OSNumber`
  of 32 bits = round(2^32/120) = **35791394**.
- `displayRefreshStepMachTime` is not published (CA zeroes it without the VRR
  MobileGestalt flag, which must stay false on this image).
- Gate: `fuxipanel120=1` (exact value, same as the DSI switch). An explicit
  `sweetfb_hz<=60` suppresses it (that arg is otherwise inert at >=60, so no new
  boot-arg). The rate published is 120 regardless of whether the DSI switch
  later succeeds; if it fails the panel stays at 60 Hz and CA targets 120.
  Log: `SWEET FB: IOMFBDisplayRefresh displayMinRefreshInterval=35791394 (120 Hz)`.
- CA precedence (code reading of the local cache): env `CA_HEARTBEAT_RATE`,
  then step mach time, then this value, then 1/60 s. A mode-derived override
  applies if the display's current-mode word (+0x230) is not -1; it is filled
  from IOMFB digital-mode data. **Unverified risk:** our user client answers
  unknown selectors with success and zeroed outputs, so a mode query could
  yield a zero refresh rate and override the property. The once-per-selector
  `SWEET FB: <proc> sel N` lines show which selectors backboardd issues; check
  them before blaming the property.
- Fallback without a kernel change: `CA_HEARTBEAT_RATE=120` in backboardd's
  environment. Frame cost is about 22 ms median, so CA will miss most 8.3 ms
  deadlines until frames are cheap: effective fps not yet observed.

### Early SwapWait retire (`fuxiearly=1`, default off)

NativeMobileDisplay retires the swap when the GPU client takes the frame
(`sheng-early-retire`), not when its copy ends, because render and copy were
serialized. Our SwapWait already returns for `SwapConsumed` (flags without bit
0, counted as `wait_early`), but `SwapConsumed` was set only after the copy
fence in `completePresent`. With `fuxiearly=1` a GPU-copied record is set to
`SwapConsumed` when `sweet_surface_gpu_copy` returns 2 (`retireEarly`). Display
uses stay held until the fence and the writer lock still blocks CA writes to
the surface during the copy, so ownership is unchanged; only SwapWait(flags 0)
waiters wake about one copy (about 1.3 ms) earlier. A failed copy replays the
record as before. Waiters with flags bit 0 still wait for Shown. New stats
field: `early_retires=N` on the `SWEET FB: stats` line. Not yet observed.

### Direct scanout: why it is not ported

NativeMobileDisplay (`sheng-dpu-scanout=2`) programs VIG0/VIG1 source address
(+0x14) and stride (+0x24) with the compositor surface's own stride, flushes
CTL0 (0x15018, bits 0 and 1), and polls the flush bit from a 500 us timer for
up to 50 ms before retiring the swap. These are the same DPU 9.2 registers as
ours (SRC0 = 0xae01000 + 0x4014). Allocation is the part that does not map:

- It makes arbitrary compositor pages reachable by pinning the surface and
  mapping its physical segments into the display's IOVA window via
  `native_ufs_dpu_map` (4 LRU slots, never the shown or pending slot). Our
  tree has no qualified display-SMMU mapping API, and our SRC0 is a 32-bit PA,
  so non-contiguous RAM above 4 GiB is unreachable.
- Stride: they pass the surface stride unchanged and scan the whole surface.
  We need stride 4608 (not 4320), a (22,18) crop of 1125x2436 into 1080x2400
  and SSPP SRC_XY/IMG_SIZE writes whose live values were never dumped.
- Ownership: they hold the surface by reference, serialize one latch at a
  time, and rely on the compositor not drawing before the swap retires. We
  have SwapShown at START plus A/B/C triple buffers and a writer lock; a
  direct design needs `scanoutOwned` in `sweet_gpu_writer`, a bounded sleep,
  newest-wins collapse, and no CPU reads of write-combined pool pages.

Precise delta for our design (carve three 0xAB8000 drawables out of the
44 MiB splash reservation at 0xb8a00000, replacing B and C; opt-in
`fuxiscandirect=1`, exclusive with double/triple):

1. Instrument first: log swapped surface ids, SSPP +0x00..+0x30 and CTL flush
   at takeover; qualify display reach of D1/D2 (0xb94b8000, 0xb9f70000, end
   0xbaa28000) by CPU fill and kick.
2. `fg_scanout_desc {pa, stride, xy, img}` programmed in one flush; every stop
   path restores A {0xb8000000, 4320, 0, 1080x2400}.
3. Allocator hook in IOSurface create (backboardd BGRA 1125x2436, bpr 4608
   only) returning a physical-address descriptor with a pool slot; free
   returns the slot.
4. `fuxi_adreno_host_page_ok` must accept pool PAs, else FuxiMetal imports
   fail and fall back to the shadow copy, which is strictly worse.
5. Writer block plus hold-until-replaced release, deferring (never
   CPU-reading) when direct publication is refused; probe 0x4e learns stride
   4608 and the crop.

Expected gain is 0.6-1.3 ms GPU time per frame and about 1-3 ms present
latency (estimates); the fps effect is unobserved and the prior is small. Not
started.

## CA frame pacing and opt-in disp0 display properties (default off)

Observed in the ios-0098-m9j A180/C182/I188/L191 boots (all `fuxipanel120=1 fuxivsyncte=1`):
backboardd never calls selector 72, so no client has the type-5 vsync notification
enabled and the TE-locked heartbeat (`callbacks_vsync` about 120/s, `period_us`
8266..8280) reaches no CA client. CA paces on its own timer from
`IOMFBDisplayRefresh.displayMinRefreshInterval` (35791394 = 1/120 s, logged at start):
FuxiMetal `frame trace interval` p50 is 8.28..8.36 ms in light windows and 8.5..10.2 ms
under load (p90 17..33 ms), so CA targets about 120 Hz and is throughput-limited (wall p50
15..24 ms), not capped at 60. `late` in `backboardd frames=` counts wall > 16.67 ms and
`cadence_ticks` uses 16.67 ms ticks, so neither separates 8.3 from 16.7 ms intervals.

Two opt-in `/arm-io/disp0` u32 ADT properties (no boot argument), written by
`tools/mkadt-fuxi.py --refresh-mhz N` and `--swap-hist`:

- `fuxi,refresh-mhz` (61000..240000, else nominal 120 Hz): with `fuxipanel120=1`, publish
  `displayMinRefreshInterval = round(2^32*1000/N)`, e.g. 120800 -> 35554365, so CA's
  free-running 8.333 ms timer does not slip one TE about every 1.2 s against the
  measured 8.274 ms panel. Logs `SWEET FB: IOMFBDisplayRefresh displayMinRefreshInterval=V (fuxi,refresh-mhz=N)`.
- `fuxi,swap-hist=1`: after each `SWEET FB: stats` line, log
  `SWEET FB: swap_interval period_us=P vsyncs 0/1/2/3/4/5+/idle=...`, SwapEnd-to-SwapEnd
  intervals rounded to TE periods (60 Hz fallback period without TE), idle at 100 ms.

No maximum-interval or TimingElements key is published: their names are not established
from our own observations. Device effect: not yet observed.

### Zero-copy verdict (2026-10-28) and copy-source pre-clean (`fuxigpuncimport` bit 20)

Verdict, derived from code: VIG0 cannot fetch CA's swap surfaces in place.

- CA's swap surfaces are backboardd BGRA 1125x2436 surfaces with bpr 4608 and
  0xab8000 bytes (observed at L191: ids 2/3/9). Our own `SweetSurface` create
  allocates them with `IOBufferMemoryDescriptor::inTaskWithOptions(kernel_task,
  InOut|KernelUserShared, size, page_size)`. Those are scattered 16 KiB XNU pages.
- XNU RAM starts at 0x8c0000000: FuxiAdreno refuses to start unless
  gPhysBase == FG_RAM_BASE 0x8c0000000. So every surface page is above 4 GiB. SRC0 (+0x14) takes a 32-bit address. The display SMMU is never
  programmed: A/B/C are splash-carveout PAs written as is, and only
  0xb8000000..0xb9de3400 has been observed to display.
- Contiguous RAM below 4 GiB cannot be allocated: XNU owns none. The only
  reachable pool is the splash carveout. Three 0xab8000 slots fit only if they
  replace B and C (0xb8a00000..0xbaa28000). That route needs all of these:
  - an IOSurface allocator hook;
  - `fuxi_adreno_host_page_ok` accepting non-pmap pages, plus a device-pager
    prepare in `importHost`, which is untested;
  - write-combined user mappings;
  - SRC_XY/IMG_SIZE/YSTRIDE0 writes for the (22,18) crop, whose live values
    were never dumped;
  - a writer block until the next START.
- The other route is translating MDSS through the apps SMMU, as
  NativeMobileDisplay does. That means taking a context bank and SMR/S2CR away
  from the bootloader state, on the same SMMU that carries UFS. It would need
  its own qualification.

Bit 20 of `fuxigpuncimport` (value 1048576, so 327168 becomes 1375744) is the
cheap alternative plus the survey. It needs `fuxigpuscanout=1`:

- `sweet_surface_gpu_copy` cleans the whole copy source to PoC before
  `fuxi_gpu_scanout_reserve`. It holds the surface-cache lock but neither the
  GPU lock nor the ring reservation, and writers are already excluded by
  `sweet_gpu_submitting`. `fuxi_gpu_copy` then skips its locked `fg_clean`.
- The clean is skipped while the surface's writer serial and epoch are
  unchanged since the last clean, for example on reserve retries.
- The about 550 us clean (L191 split) therefore leaves the window in which
  client submits are refused or blocked. The GPU copy itself (about 650 us)
  still holds the ring.
- At takeover, one read-only VIG0 dump is logged:
  `FUXI SCANOUT: sspp vig0 size= img= xy= out_size= out_xy= src0= ystride0=
  format= unpack= op_mode=`.
- Every 600 frames, `FUXI SCANOUT: preclean cleans= skips= zerocopy eligible=
  format= stride= size= backing= scattered= high= unknown=` classifies each
  submitted frame. Expected (derived from code): every frame is `high`.
- The bit-64 coherent mode (from 144de2d) does not skip this clean. Its pass
  maps ABI imports write-back inner-shareable (MAIR index 1, 0xff). The copy
  source is mapped separately by `fuxi_gpu_map` in the kernel present table,
  with AttrIndx 0 (0x44, Normal non-cacheable), inner-shareable. A
  non-cacheable GPU read does not snoop CPU caches, so dirty CPU lines must
  still reach PoC. Skipping would need `fuxi_gpu_map` to use AttrIndx 1 under
  `fg_abi_coh_wb`. That is a separate change, and the self-test did not
  exercise the present table's mapping.

Not yet observed.

## Inline-mode present ordering: frame log, copy priority, superseded skip (fuxigpuncimport 8/16/32)

Inferred from the O220 log (inline mode, `fuxigpuncimport=2948672`): the panel
runs at about 84 Hz and the client swaps about 1.75 times per shown frame.
Copies complete in pairs within one TE, and START gaps are 62% one TE, 33% two
and 5% three or more. In inline mode a present copy is queued only after its
frame's writer lease ends in userland (writer retire, then `presentNext`). By
then the next frame's client job is usually in the ring already, so copy N
runs behind job N+1. The bit-4096 hold does not apply in inline mode. Three
unused bits of the same boot argument (formerly reserved, see metal.md) add
opt-in tools. With all three set, `2948672` becomes `2948728`. That value has
the same number of digits, so the command line does not grow. Each bit is
independent, and with none set the behavior is unchanged.

- **8, frame log** (`SweetFramebuffer`). After 3000 warmup swaps it records
  128 consecutive swaps, prints them once, then stops. Each line has the swap
  id, the first surface, and these times: swap (us from frame 0), then ready
  (writer retire), copy submit, copy fence and START (us after that swap).
  It also has the GPU stamps of that copy: submit-to-copy-start from the
  always-on counter at fence page +192 (taken after the copy's WFI, so it
  includes every job ahead in the ring), and the blit time. Then come the
  client jobs in flight when the copy was queued (`fg_abi_seq -
  fg_abi_completed`), the target buffer, the TE count at publish, the last
  transfer-done time, and the fate (`S` shown, `D` dropped, `P` replaced
  while waiting for START, `-` open). The summary line counts
  `copies_behind_client_job`, `submit_to_start_over_3ms`,
  `fence_pairs_within_half_period` and `start_te_gaps 1/2/3+`, which confirm
  or refute the pairing directly. The 10 KB log is heap-allocated only when
  bit 8 is set, because kalloc_type limits the `AppleCLCD` object to 16 KB.
  An inline array broke CI run 41046931721 with a static assertion.
- **16, copy priority** (needs inline, 512|2097152; `fg_abi_copy_first`).
  `SweetFramebuffer` publishes the surfaces of queued swaps whose copy is not
  queued yet (`fuxi_gpu_present_due`, under its lock). `sweet_gpu_writer`
  records `fg_abi_seq` at each active writer lock (`fuxi_gpu_present_writer`).
  A pending AvoidSync reservation records "unknown" and never holds. A client
  submit is refused (existing refuse/retry/wake path, wait kind 0) while a due
  surface's recorded sequence differs from `fg_abi_seq`, meaning a submit
  followed that lock and the writer job is in the ring. The hold is released
  with a wake as soon as that surface leaves the due set (its copy is queued
  or the swap is dropped). It is bounded to one panel period
  (`fg_scanout_period_us`, or 16.7 ms when unknown) per due generation. Each
  swap that adds a surface opens a new generation, and an expired generation
  never holds again.
- **32, skip superseded** (`SweetFramebuffer::presentNext`). When a newer
  queued swap is already ready (no writer, not quarantined, epoch unchanged,
  a racy probe that `sweet_surface_present_begin` re-checks), the older ready
  swaps are dropped before their copy (about 0.56 ms of GPU each). Counted in
  `SWEET FB: superseded_skips=` every 600 frames and in the frame-log summary.

Why option A (hold client submits) and not option B (append the copy right
after frame N's job): the kernel has no link between an ABI submit and an
IOSurface. Option B would mean copying a surface whose writer lease is still
active, which breaks the writer/reader invariant that keeps DSI from reading a
half-written frame. Option A only delays client submits through the existing
path. The copy path, the triple-buffer target choice (`fg_scanout_target`,
never the front or the ready buffer), the single `fg_busy`/`fg_magic` fence
and the ring are untouched.

Log lines: `SWEET FB: frame_log= copy_first= skip_superseded= (fuxigpuncimport
8/16/32)` at start; `SWEET FB: flog begin ...`, 128 `SWEET FB: flog N ...`,
`SWEET FB: flog end shown= dropped= replaced_waiting_start= copied=
copied_not_shown= copies_behind_client_job= submit_to_start_over_3ms=
fence_pairs_within_half_period= start_te_gaps 1/2/3+= superseded_skips=`;
`FUXI GPU: copy_first refused= episodes= released= expired= due_publishes=
no_writer= bound_us=` next to the 600-inline-copies line.

Risks:

- If the thread that ends the writer lease is the one blocked in submit, the
  hold lasts until its bound (`expired` counts this).
- Frames with several submits, or unrelated clients submitting between the
  lock and the frame's job, can be held. This is bounded and visible as
  `refused`.
- The GPU may idle between job N's end and copy N's submit. That is the
  intended trade.
- A stale due entry lasts until the next framebuffer publish.

Tests: `tools/test-fuxi-metal.py` (copy priority),
`tools/test-fuxi-unixnu.py` (frame log, due set, superseded skip),
`tools/test-fuxi-adreno.py` (copy stamps),
`tools/test-fuxi-gpu-scanout.py` (writer record). Not yet observed.

## Copy priority park (fuxigpuncimport bit 27, value 134217728, default off)

Measured with `tools/fuxi-flog.py` (host-only parser for the bit-8 frame log; it
prints the per-stage percentiles, the swap-to-START split and a replay of every
START gap longer than one period). Two device logs, both inline mode:
Q222 (`fuxigpuncimport=2948680`, no copy priority) and R223 (`2948728`,
frame log + copy priority 16 + superseded skip 32).

| | Q222 | R223 |
|---|---|---|
| client swaps, p50 interval | 74.4 Hz, 8.24 ms | 43.3 Hz, 15.6 ms |
| shown | 32.8 Hz (56/128, 72 `P`) | 38.9 Hz (114/128, 14 `P`) |
| swap to ready (writer retire), p50 | 13.4 ms | 34.4 ms |
| ready to copy submit, p50 / p90 | 0.69 / 0.73 ms | 0.06 / 0.73 ms |
| copy submit to copy start, behind a client job | 88/128, p50 6.5 ms | 108/128, p50 8.1 ms |
| copy submit to copy start, ring idle | 40/128, 2 us | 20/128, 2 us |
| blit | 0.56 ms | 0.55 ms |
| fence to START (shown), p50 / avg | 38 us / 2.15 ms | 32 us / 1.39 ms |
| copies fenced in pairs (within half a period) | 38 | 5 |
| START gaps longer than one period: copy late / display missed / client idle | 28 / 25 / 1 | 88 / 14 / 3 |
| copy-late cases behind a client job | 26/28 | 87/88 |

Where the time goes (Q222, shown frames, average of swap to START 19.8 ms):
render (swap to ready) 68%, ready to submit 2%, queue behind a client job 16%,
blit 4%, fence to START 11%. The ready-to-submit time has two clusters:
about 50 us, and about 0.7 ms when the previous copy is still running. Once a
copy is queued it waits behind one whole client job (about 6.5 ms) in two
thirds of the frames. That job is frame N+1, already in the in-order ring
before frame N's writer lease ends. The copy then finishes in the same TE as
the next one (38 pairs), and one of the two is replaced while it waits for
START (72 `P`).

Why copy priority (16) did not help, and why `released=0` (R223: per 600
copies `refused=781 episodes=398 released=0 expired=252`, and 640/297/1/253,
621/321/0/200, 859/419/2/313). This is a design flaw, not a counter bug, read
from the code:

- FuxiMetal `Backend::submit` takes the backend `mutex` and holds it through
  `QueueSubmit` (tools/fuxi-metal/Backend.hpp:1662 to 2442).
- The kernel refusal sleeps inside that ioctl, so the refused submit of job
  N+1 holds the mutex while it sleeps.
- Job N's retirement closure waits for its fence without the mutex, then
  takes it (Backend.hpp:2499). The surface lease ends only after that closure
  returns (Frontend.mm `retireCommitted`, `_lease.reset()`).
- So the writer unlock that queues copy N cannot happen until the refusal
  times out (one period, `expired`). Then job N+1 enters the ring ahead of
  copy N anyway: 108/128 copies still went behind a client job.
- The client is throttled meanwhile. Swaps fell to 43 Hz, and swap-to-ready
  rose from 13 to 34 ms.

Park removes the wait instead of bounding it. With bit 27 (and 16; inline
512|2097152), the submit that copy priority would refuse is fully built:
frame words, TLB invalidate after a map, import clean, and fence
`++fg_abi_seq`. It is not written to the ring, and the ioctl returns at once.
The mutex is free, so job N retires, its lease ends, SweetFramebuffer queues
copy N, and `fuxi_gpu_copy` writes the parked frame right after the copy's
words, then rings the doorbell. At most one job is parked. The other release
paths keep the client from stalling:

- **idle:** 1.5 ms (`FG_PK_IDLE_US`) after the due writer job's fence is
  written, with no copy. This covers several submits per frame or a deferred
  present.
- **cap:** two panel periods after parking.
- **submit:** the next submit rings the parked job first, so order is kept.
- **not_due:** the due surface leaves the set, for example a dropped swap.
- **other:** a classic-copy root switch.

An idle or cap release expires that due generation, as the hold's expiry did.
A fault discards the parked job, and its fence fails with the client. The
2 ms watchdog stays armed while a job is parked, and `fg_abi_observe` (every
WaitFence poll) runs the release check. Ring room for the parked frame plus
one copy is reserved at park time (`no_room` counts the jobs that ran
unparked instead). The kernel has no link between a submit and a surface,
so the writer record of bit 16 remains the only trigger: a submit that
follows an active writer lock of a due surface whose job is in the ring.

Expected effect (a guess, not yet observed):

- `copies_behind_client_job` near 0, with ready-to-copy-start about 2 us
  plus the blit.
- Fewer fence pairs and `P` fates.
- The client no longer throttled (swap interval back near 8.3 ms).
- The GPU idles from job N's end to copy N's submit, about 0.2 ms of writer
  retire plus 0.05 ms, or about 0.7 ms while the previous copy still runs
  (Q222 clusters).
- The display-side misses in Q222 (25 gaps with a newer copy fenced in
  time, 12 of them with another copy in flight) are not addressed.

Boot value: `2948728 + 134217728 = 137166456` (frame log, copy priority,
superseded skip, park; 9 digits). Without the frame log and skip, use
`2948680 + 16 + 134217728 = 137166424`.

Log lines:

- At init: `FUXI GPU: copy_first_park=1 idle_us=1500 cap=2 periods
  (fuxigpuncimport 134217728 with 16, inline mode)`.
- At start: `SWEET FB: frame_log= copy_first= skip_superseded= park=
  flog_trigger= (fuxigpuncimport 8/16/32/134217728/268435456)`.
- Every 600 inline copies, after the copy_first line: `FUXI GPU:
  copy_first_park parked= released_by copy= idle= cap= submit= not_due=
  other= no_room= writer_done_at_park= parked_us avg= max=
  writer_done_to_release_us n= avg= max= idle_us=`.
- Each frame-log line ends in ` pk=0|1` (the copy rang a parked job). The
  `flog end` line adds `copies_releasing_parked_job=`.
- `copy_first refused=` stays 0 with park on.

Success criteria: `released_by copy` dominates, `idle + cap` are small, and
`copies_behind_client_job` and `copy_late` fall.

Risks:

- If one frame makes several submits after its writer lock, each later
  submit releases the parked job (`submit`) and parks itself. Only the last
  one stays behind the copy.
- A parked job delays the client's own next frame by up to 1.5 ms after its
  writer job ends when no copy comes.
- `fg_client_busy` and the 100 ms deadline are unchanged; the parked job
  counts as in flight.

Tests:

- `tools/test-fuxi-metal.py` (park): needs 16, default off, no wait and no
  refusal, copy words before the parked frame and one doorbell, idle at
  1.5 ms then an expired generation, cap at two periods, next-submit order,
  due surface leaving, a pending lock never parks, fault discard, counters.
- `tools/test-fuxi-adreno.py`: a parked job is not counted ahead of the copy
  and is flushed once after the copy's words.
- `tools/test-fuxi-unixnu.py`: the `pk=` field and the end-line count.

Not yet observed.

## Not-shown paths and the not-shown frame-log trigger (fuxigpuncimport bit 28, value 268435456, default off)

V227 (`137166456`): 801 of 11602 swaps (6.9%) were never shown. The known
counters do not explain them: `superseded_skips` is 0-6 per window,
`ready_skips` (cumulative) grows by about 2 per window and `fifo_overflow` is
0, while about 50 per window are "not shown". In that build the only
`fStatDropped` increment without a counter of its own was the `P` path in
`completePresent`: a copy retires while the previous frame still waits for
START. Its frame-log window (a light scene) had no drops at all.

`P` mixed two different outcomes:

1. **Replaced**. The waiting frame was still ready in the kernel when the
   next copy was offered (`fg_scanout_back_ready`, counted again as the
   kernel's `back_ready_drops`/`drops=`, about 27-29 per 600 STARTs in
   V227). It was never STARTed.
2. **STARTed by the TE spin thread during the next copy**. With hard spin
   (bit 262144) the spin thread STARTs a waiting buffer while a copy into
   another buffer is still open (`copy_open_but_started`, 330-410 per 600
   in V227). The display path only learns of it through `retry`, and retry
   does not run while a copy is in flight. So when that copy retired,
   `completePresent` counted the frame as `P`. The panel had shown it, but
   `SwapWait(flags 1)` still returned aborted to CA. The kernel's
   `fuxi_gpu_scanout_publish` had already noticed this ("this offer
   replaces its record"), but it did not report it. INFERRED: the gap of
   about 20 per window between "not shown" and the kernel's drops is this
   case. Not yet observed.

The change has two parts. The kernel reports what happened to the waiting
frame: `fuxi_gpu_scanout_async_take()` returns 1 once if the spin thread
STARTed it and no retry has reported it, and `fuxi_gpu_scanout_offer_fate()`
gives the fate seen by the last offer (0 nothing waiting, 1 STARTed, 2
replaced unstarted). `completePresent` calls the take *before* the next
offer, so the START stamps are still that frame's own. A frame that was
STARTed is then counted shown through the normal `presentStats` path
(status `Shown`, fate `S`, its stages, one shown frame). Only frames the
kernel replaced, or lost, are counted not shown. This changes the "not
shown" and "600 frames" figures compared with V227: the old "not shown"
equals the new "not shown" plus `spin_started + spin_race`.

Every way a queued swap ends without START, with its counter and the
frame-log letter (`dp=`):

| path | letter | where | in "not shown" |
|---|---|---|---|
| `stale` | s | `presentNext`: the writer epoch changed after the swap (record stale and unshown), also for a stale replay | yes |
| `not_ready` | r | `presentNext`: an older queued swap was released because a later one could be read first (`present_begin` refused the older one) | yes |
| `superseded` | u | `presentNext`, bit 32: older ready swaps dropped for a newer ready one | yes |
| `failed_copy` | f | `presentNext`: a failed GPU copy's replay yielded to a newer swap | yes |
| `fifo` | o | `swap`: a fourth swap pushed out the oldest queued one | yes |
| `refused_full` | c | `presentNext`: `present` refused (busy ring or ownership) while the queue had refilled to 3 | yes |
| `replaced` | p | `completePresent`: waiting for START, still ready in the kernel at the next offer (fate 2) | yes |
| `lost` | l | `completePresent`: waiting for START, but the kernel had neither STARTed it nor kept it ready (fate 0) | yes |
| `stop` | h | `stopRecords`: queued or waiting when scanout stopped | no |
| `rejected` | (none) | `swap` returned no-resources (surface hold failed) | no (never queued) |
| `spin_started` | a | `completePresent`: STARTed by the spin thread during the next copy, counted shown | no (shown) |
| `spin_race` | A | the same, but STARTed between the take and the offer (offer fate 1). Its START stamp may be the next offer's | no (shown) |

A copy refused while the queue has room (`present` returns false: busy ring
`copied == 3`, a `cpu_acquire` refusal, or a quarantine) is not a drop. The
record goes back to the head of the queue and is retried. A late copy or a
late fence is not a drop either: the frame is shown one TE later.

Log line (always on, every 600 shown frames, after the `superseded_skips`
line, a handful of increments under `fLock` per drop):

`SWEET FB: unshown paths stale= not_ready= superseded= failed_copy= fifo=
refused_full= replaced= lost= sum= of N not shown; outside it stop=
rejected=; counted shown spin_started= spin_race=`

`sum` should equal N.

**Bit 28** (needs bit 8, the frame log) changes when the one-shot 128-frame
log starts. Without it, the log starts after 3000 warmup swaps. With it,
the log starts at the first not-shown burst after a 300-swap floor: 3
not-shown frames (paths up to `lost`) in a 60-swap window. A window that
ends with fewer than 3 resets the count. The swap that sees the third drop
becomes frame 0. `flog begin` prints `warmup_swaps=` (the swaps actually
waited), `trigger=` and `trigger_drops=`. Each `flog N` line ends in
` dp=<letter>` (`-` for none). `flog end` adds
`drop_paths s/r/u/f/o/c/p/l/h/a/A=`. `tools/fuxi-flog.py` parses `dp=` and
`trigger=`, prints a `drop_paths` line for the window and sums every
`unshown paths` line in the log.

Boot value: `137166456 + 67108864 (bit 26, fence IRQ) + 268435456 =
472710776` (9 digits, so the command line grows by nothing).

Tests: `tools/test-fuxi-unixnu.py` (one path per not-shown frame in the
FIFO test, with the exact log line; replaced/lost/spin_started/spin_race in
the frame-log block; trigger window reset and start; `h` at stop) and
`tools/test-fuxi-gpu-scanout.py` (offer fate 0/2 in the triple test, fate 1
and a single report after an overlapped spin START, async take). Not yet
observed.

### Direct-DPU-scanout stage 1: read-only display SMMU survey (`fuxigpuncimport` bit 22)

Bit 22 (value 4194304, so 2948728 becomes 7143032, same length) needs
`fuxigpuscanout=1`. Once, at takeover (after the bit-20 VIG0 line if bit 20 is also set), the driver
maps apps_smmu (0x15000000, 1 MiB) read-only and temporarily. It reads through
the fault-safe accessor, which does not latch `fg_access_failed`, and logs:

- `FUXI SCANOUT: dpu smmu scr0= idr0= idr1= idr2= smrs= cbs= page= cb_base=`
  (SMR count from IDR0, bank count, page size and bank base from IDR1).
- Up to 4 lines `dpu smmu smr<n>= s2cr= cb= cbar= cba2r=`, one for each valid
  SMR whose id/mask covers stream 0x1c00 or 0x1c02. The DT handoff is
  `<0x1c00 0x2>`.
- For each translating S2CR whose bank index is valid,
  `dpu smmu cb<n> sctlr= ttbr0= tcr= tcr2= mair0= fsr=`. A bypass or fault
  S2CR prints cbar/cba2r as 0xffffffff and has no bank line.
- `dpu smmu vig0 size= img= xy= out_size= out_xy= ystride0= format= unpack=
  src0= cur= flush= start=`. cur is CURRENT_SRC0 at +0xa4; flush and start are
  CTL0 +0x18/+0x1c.
- `dpu smmu done matches= faults= (read only)`. A register that cannot be read
  prints 0xffffffff and is counted in faults.

It writes nothing to the SMMU or MDSS. The host test checks this: the write
trace is identical with and without bit 22, and the survey maps apps_smmu
read-only (the model fails any SMMU write that is not made through the
stage-2 read/write mapping). The decoding assumes SMMUv2 without EXID (sCR0
bit 3 clear). Boot S224 observed the survey (EL1 reads work, `faults=0`); the
values are decoded in the stage-2 section below.

### Direct-DPU-scanout stage 2: one translated flip (`fuxigpuncimport` bits 22+23)

Bits 22+23 (4194304 + 8388608), default off. From the current base 2948680 the
boot value is **15531592** (7 to 8 digits). Bit 23 without bit 22 is ignored
and logs `dpu stage2 ignored: bit 23 needs bit 22`. Bit 22 alone stays the
read-only survey. The code is `fg_scanout_dpu_stage2_run` in
`xnu/fuxi-adreno/fuxi_gpu_scanout.inc`, with the apps_smmu read/write map and
memory helpers in `fuxi_adreno.c`. It is adapted from ShengUfs `NativeUfs.cpp`
(f2d6a4a): `dpuEnable` (table layout, register values, the 50 ms FSR watch,
undo), `dpuTlbFlush` and `native_ufs_dpu_map`. The 4 KiB page list comes from
GopFramebuffer `NativeLegacyDisplay.cpp` (33e941a) `directSlot`. ShengUfs is
SPDX BSD-2-Clause-Patent in its sources and BSD-2-Clause in its LICENSE file;
GopFramebuffer is BSD-2-Clause-Patent. Both licenses ship next to the code as
`LICENSE.ShengUfs` and `LICENSE.GopFramebuffer`, and the 0098 patch carries them.

#### S224 survey, decoded (observed values; field names from the SMMUv2 layout)

| Register | Value | Meaning |
|---|---|---|
| sCR0 | 0x2d0406 | CLIENTPD 0 (SMR matching active), GFRE/GFIE set, EXIDENABLE 0 (15-bit SMR ids, as the code assumes), USFCFG 1 (unmatched streams fault). The bits above 16 were not decoded. |
| IDR0 | 0x4c017e69 | NUMSMRG 105; S1TS 1, S2TS 0 (EL1 sees no stage 2), SMS 1, ATOSNS 1 (no ATOS, so there is no translation self-check, as on sheng); NUMSIDB 15; CTTW and BTM set; NUMIRPT 1 |
| IDR1 | 0x60000046 | 4 KiB register pages; NUMPAGENDXB 6, so banks start at 128 pages = 0x80000; NUMCB 70; NUMS2CB 0 |
| IDR2 | 0x5111 | IAS 1 and OAS 1 (both 36 bits), UBS 1; 4 KiB and 64 KiB granules |
| SMR3 | 0x80021c00 | valid, id 0x1c00, mask 0x2: both display streams 0x1c00 and 0x1c02 |
| S2CR3 | 0x3 | type 0 (translate) into bank 3 |
| CBAR3 | 0x1f000 | TYPE 1, meaning stage 1 with stage 2 bypass; VMID 0; bits 15:12 = 0xf (GUESS: bypass memory attributes), IRPTNDX 0 |
| CBA2R3 | 0x1 | VA64: AArch64 descriptors |
| bank 3 SCTLR | 0xe0 | M 0 (translation off, so the display fetches physical addresses); CFRE, CFIE, CFCFG set (with M set, a fault would stall) |
| TTBR0/TCR/MAIR0 | 0 | never programmed |
| TCR2 | 0x60 | PASize 0; bits 5-6 kept as found (GUESS: TBI0/TBI1) |
| FSR | 0x400 | FORMAT (bits 10:9) = 2, AArch64; no fault bits |

This is the same bank-3 / SCTLR 0xe0 handoff sheng recorded (its i562). An idle
FSR is not 0, so every "FSR clean" check in stage 2 tests the fault bits
(MULTI, SS, UUT..TF; mask 0xc00001fe), not FSR == 0.

#### Geometry

A/B/C (observed) are 1080x2400 BGRA with stride 4320 (YSTRIDE0 0x10e0), 10 MiB
apart from 0xb8000000. VIG0 SRC_SIZE = OUT_SIZE = 0x09600438, SRC_IMG_SIZE 0,
SRC_XY 0, OUT_XY 0, format 0x237ff, unpack 0x3020001.

The test surface is CoreAnimation's logical size: 1125x2436, stride 4608. That
is 11225088 bytes, or 2741 4 KiB pages. The panel shows the centred
1080x2400 part, at the same origin SweetFramebuffer's copy uses (`fOffX` =
(1125-1080)/2 = 22, `fOffY` = (2436-2400)/2 = 18). Stage 2 writes:

| Register | A | Test surface |
|---|---|---|
| SRC_IMG_SIZE +0x04 | 0 | 0x09840465 (2436<<16 \| 1125) |
| SRC_XY +0x08 | 0 | 0x00120016 (18<<16 \| 22) |
| SRC_YSTRIDE0 +0x24 | 0x10e0 | 0x1200 (the high half is kept) |
| SRC0 +0x14 | 0xb8000000 | 0x10000000 (IOVA) |
| SRC_SIZE, OUT_SIZE, OUT_XY, format, unpack | | unchanged, so the output and DSC stay 1080x2400 |

The first byte fetched is at offset 83032 (18 x 4608 + 22 x 4). The last is at
11141943, inside the surface. Two things here are GUESSES: that this SSPP needs
SRC_IMG_SIZE when SRC_XY is not 0, and that x = 22 has no alignment rule for a
32-bit format. An SRC0 offset of 83032 was rejected for the same reason:
the alignment rule is unknown.

#### Sequence

The sequence runs once. It starts at the first acquire after the inherited
transfer has drained (after the floor, with START clear and the write line
idle) and after the panel120 switch. It runs before B and C are set up, under
FG_GUARD. Every step logs a `FUXI SCANOUT: dpu stage2 ...` line.

1. `begin img= xy= ystride0= src0= cur= flush= start= line=`. The run is
   refused unless START, the VIG0 flush bit and the write line are idle,
   SRC0 = CURRENT_SRC0 = A, and the GPU ring is idle.
2. apps_smmu is mapped read/write, and the bank is found by the stage-1 SMR
   scan. Every SMR covering 0x1c00/0x1c02 must translate into one bank. CBAR
   must be type 1 and CBA2R must be VA64. SCTLR.M must be clear and no fault
   may be recorded. Logged as `bank cb= smrN s2cr= cbar= cba2r= saved sctlr=
   tcr2= ttbr0= tcr= mair0= fsr= oas_bits=`.
3. Memory, allocated before any SMMU write:
   - The table is 17 pages, physically contiguous. Page 0 serves levels 0-2
     (sheng's i564 layout): [0] points to itself, [2] and [3] are 1 GiB
     identity blocks for 0x80000000-0xffffffff, and [128..143] point to the
     window's 16 level-3 tables.
   - The surface is wired, zeroed `kmem_alloc` memory with its pages scattered.
   - The leaves are AF|AP[1], AttrIndx 0, not shareable. Every PA must be
     below the OAS (36 bits).
   - The pattern is red outside the crop, a 4-pixel white frame on the crop
     edge, and 8 vertical bars. It is cleaned to the PoC.
   - Logged as `memory tables= surface 1125x2436 stride=4608 pages=2741
     above4g= runs= first= last=`. above4g counts pages that need the window
     because they are beyond the 32-bit SRC0. Placement is up to the VM
     allocator: memory above 4 GiB cannot be requested.
4. Install:
   - TTBR0 = table, TCR2 = PASize from IDR2.OAS plus SEP = 7 (UPSTREAM),
     other bits kept. S224 gives 0x38061. Sheng uses 48-bit (5); here PASize
     equals the reported OAS. SEP was 0 in X229 and caused its fault (see
     below). The readback must show both PASize and SEP, or the install is
     undone before SCTLR is touched.
   - TCR = 0x800020 (T0SZ 32, 4 KiB granule, non-cacheable walks, EPD1), MAIR0 = 0x44.
   - The values are read back, then a bank TLBIALL and TLBSYNC follow.
   - SCTLR = (old & ~CFCFG) | M|CFRE|CFIE, which is 0x61. A fault then
     terminates instead of stalling.
   - Logged as `enabled sctlr=0xe0->0x61 ttbr0= tcr=0x800020 tcr2=0x38061
     mair0=0x44 tlbi=bank`.
   - Right before the first START, the bank is read back again:
     `live sctlr= ttbr0= tcr= tcr2= sep= pasize= mair0= fsr= l1[0]= l1[2]=
     l1[3]=`. The last three are the table entries in memory.
5. `identity starts=60 fsr= cur= xfer_us avg= max=`: 60 STARTs on A through
   the identity block. Each one waits a period plus 2 ms, then polls (up to
   100 ms) for START clear and the write line idle, then checks FSR.
6. `mapped iova=0x10000000 pages=2741 window=0x2000000 tlbi=bank`: the
   window's leaves, which were empty until now (so a stray IOVA fetch would
   fault), are written, cleaned and followed by a bank TLBIALL.
7. `flip src0=0x10000000 img=0x9840465 xy=0x120016 ystride0=0x1200 xfer_us=
   fsr= cur= latched=`: the geometry is written, then CTL0 FLUSH |= 1 and one
   START. `latched=1` means CURRENT_SRC0 read back as the IOVA. 0 is only
   logged, not treated as a failure.
8. `hold_ms=2000 fsr=`: the panel keeps showing the frame from its own RAM.
   FSR is read every 50 ms.
9. `restored A img=0 xy=0 ystride0=0x10e0 src0=0xb8000000 xfer_us= fsr= cur=`:
   A's geometry is written back, flushed and STARTed while translation is
   still on (identity). CURRENT_SRC0 must be A again before translation goes
   off.
10. `bank restored exact= sctlr= tcr2= ttbr0= tcr= mair0= fsr=`:
    - SCTLR is restored first, then TTBR0/TCR2/TCR/MAIR0. FSR fault bits are
      cleared (write one to clear), RESUME is written for a stalled
      transaction (SS), and a bank TLBIALL follows.
    - `exact=1` means every register reads back exactly as saved, FSR included.
    - The memory is freed only when `exact=1` and the source is A. Otherwise
      `memory kept` is logged.
11. `done result=ok|fault step= identity_starts= flip_latched= hold_ms= exact=`.
    The START gates are then re-armed from the current TE count, and the
    normal path continues (double or triple setup, copies).

On a fault, an MMIO failure or a register that ignores writes, the log has
`fault step= reason= starts= fsr= far= fsynr0=`. If translation was on, a
`walk far= plvl= wnr= pnu= ind= ptwf= afr= fsynr0_hi= tcr2= sep= ... sign=
l0[i]= l1[i]= l2[i]= l3[i]=` line follows. It holds:

- the FSYNR0 fields;
- the bit that TCR2.SEP sign-extends at, and whether FAR has it set;
- the entries for FAR that a level-0-start walk and the T0SZ-32 level-1-start
  walk read from the table in memory. `(not ours)` marks a descriptor that
  points outside the table.

The undo then runs:

- A's geometry is rewritten (with a flush).
- The driver waits for idle.
- Step 10 runs.
- If translation was on, one untranslated START on A follows:
  `undone step= start= xfer_us= cur=`.

If the transfer never goes idle, `undo incomplete: transfer not idle;
translation kept on (identity + window), memory kept` is logged. The table
stays live, because the latched source may still be the window, and the
scanout stops. A refusal before any SMMU write logs `skipped step= reason=;
nothing written`.

Writes that never happen:

- SMRs, S2CRs, CBAR/CBA2R, any GR0/GR1 register, and the global TLB
  invalidates. apps_smmu also serves touch, WLAN and UFS.
- `fg_dpu_cb_wr` accepts only the bank offsets SCTLR 0x0, RESUME 0x8, TCR2
  0x10, TTBR0 0x20/0x24, TCR 0x30, MAIR0 0x38, FSR 0x58, TLBIALL 0x618 and
  TLBSYNC 0x7f0.

The FG_GUARD lock is held for about 3.2 s at 60 Hz (60 x 18.7 ms + 2 s hold),
or about 2.7 s at 120 Hz. The GPU ring and presents wait for that time once
per boot.

Host test (`tools/test-fuxi-gpu-scanout.py`, `dpu_stage2_tests`). The model has
apps_smmu with the S224 values plus another live bank for a different master,
and an MDSS fetch model. At each START the fetch model latches SRC0 on a
flush. When SCTLR.M is set, it walks the bank's table for three pixels of every
row. It checks the physical address of each walk: identity for A, the exact
scattered page for the window. It also checks the pattern pixels, and sets FSR
TF on a failed walk. The test proves:

- Every SMMU write lands in bank 3's page. So there are no SMR/S2CR/global
  writes, and the only invalidate is the bank TLBIALL (3 on success).
- After success and after every undo, the whole 1 MiB apps_smmu model is
  byte-identical to before, and so are the VIG0 registers.
- Faults at identity START 6 (with SS, so RESUME), at the flip and during the
  hold are undone; the restore is exact, A is shown again and the memory is freed.
- A bank that ignores the table, or whose TCR2 drops SEP, is undone with no
  START.
- The walk model follows SMMUv2 AArch64 stage 1: the TCR2.SEP sign extension
  with EPD1, the start level from T0SZ (level 1 with 2 index bits for T0SZ
  32), the PASize limit on table addresses, and AF on leaves. A replay on the
  first translated START checks the installed table under the X229 TCR2
  (0x61): it faults at level 0 for 0xb8000100. Under 0x38061 it maps
  identically through the level-1 block, and the empty window faults at level 3.
- A real walk fault (a window level-3 entry cleared under the flip) logs FAR,
  PLVL 3 and the entries `l1[0] l2[128] l3[20]=0`.
- A stuck flip transfer keeps translation and memory and stops the scanout.
- Each refusal writes nothing: translation already on, a recorded fault, the
  CBAR type, streams on two banks, a pending flush, a source that is not A,
  either allocation failing, a page lookup failing.
- Bits clear: none set, bit 22 alone or bit 23 alone causes no SMMU write,
  START or allocation.

`--xnu-buildtree` checks the kernel syntax of `fuxi_adreno.c`
(`kmem_alloc_contig`, `CleanPoC_DcacheRegion_Force`).

Risks (none of this is observed yet):

- The hypervisor may ignore or abort EL1 writes to the bank. A fault-safe
  write, or the readback check, turns that into an undo with no START.
- The handoff SMR also routes 0x1c02. Any display fetch outside 2-4 GiB
  (GUESS: none from the bootloader configuration) would fault and be undone.
- A walk or terminate fault gives one garbage or black frame. CFCFG is
  cleared, so the display should not stall. That is a GUESS, but it matches
  the SMMUv2 rule.
- Sheng i564 recorded a level-0 fault for T0SZ 32, so the one-page L0-L2
  layout is kept. It works whether the walk starts at level 0 or level 1.
  The X229 decode below suggests that level 0 was the sign-extension fault,
  not a level-0 start.
- The 3 s lock hold at boot. The SRC_IMG_SIZE/SRC_XY requirements and the
  x-alignment above.

Expected lines with bits 22+23 on a good run, in order: the stage-1 `dpu smmu`
lines, `begin`, `bank cb=3 smr3 s2cr=0x3 cbar=0x1f000 cba2r=0x1 saved
sctlr=0xe0 tcr2=0x60 ttbr0=0 tcr=0 mair0=0 fsr=0x400 oas_bits=36`, `memory`,
`enabled sctlr=0xe0->0x61 ... tcr=0x800020 tcr2=0x38061 mair0=0x44`, `live
sctlr=0x61 ... tcr2=0x38061 sep=7 pasize=1 mair0=0x44 fsr=0x400`, `identity
starts=60`, `mapped`, `flip ... latched=1`, `hold_ms=2000`, `restored A`,
`bank restored exact=1 sctlr=0xe0 tcr2=0x60 ttbr0=0 tcr=0 mair0=0 fsr=0x400`,
`done result=ok`. The panel should show the bars with a white frame and no red
edge for about 2 s, then the boot image again. Stage 3 (bit 24) would
remap CA surfaces per frame using the same window.

#### X229 result (kernel 9565b45, fuxigpuncimport=15531592) and the SEP fix

Observed:

```
dpu stage2 bank cb=3 smr3 s2cr=0x3 cbar=0x1f000 cba2r=0x1 saved sctlr=0xe0 tcr2=0x60 ttbr0=0 tcr=0 mair0=0 fsr=0x400 oas_bits=36
dpu stage2 memory tables=0x8c59e8000 surface 1125x2436 stride=4608 pages=2741 above4g=2741 runs=686 first=0x9b44f8000 last=0x9b3444000
dpu stage2 enabled sctlr=0xe0->0x61 ttbr0=0x8c59e8000 tcr=0x800020 tcr2=0x61 mair0=0x44 tlbi=bank
dpu stage2 fault step=identity reason=fault starts=0 fsr=0x402 far=0xb8000100 fsynr0=0x630020
dpu stage2 bank restored exact=1 ... fsr=0x400
dpu stage2 undone step=identity start=1 xfer_us=11355 cur=0xb8000000
dpu stage2 done result=fault step=identity identity_starts=0 flip_latched=0 hold_ms=0 exact=1
```

The undo worked: the bank was restored exactly, A was shown again, and the boot
went on normally.

Decode, using the SMMUv2 field layout:

- FSR 0x402 is TF (translation fault) with FORMAT 2.
- FSYNR0 0x630020:
  - PLVL is 0, so the fault was at level 0.
  - WNR 0 (a read), PNU 1 (unprivileged), IND 0 (data), PTWF 0 (no external
    abort on the table fetch), AFR 0.
  - Bits 23:16 hold 0x63. That is S1CBNDX, which is only meaningful for a
    nested stage-2 fault. GUESS: an implementation value; it is not bank 3.
- FAR 0xb8000100 is A plus 0x100, the first fetch.

Cause (the conclusion; not yet confirmed on device):

- TCR2.SEP (bits 17:15) was 0, kept from the bootloader's 0x60.
- In the SMMUv2 AArch64 format, SEP 0 means the input address is
  sign-extended from bit 31. Every display address with bit 31 set, which
  means all of 0x80000000-0xffffffff and so A, B and C, therefore becomes an
  upper-half address.
- That address goes to TTBR1, which TCR.EPD1 disables. The result is a
  translation fault at level 0, before any table entry is read. That is
  exactly PLVL 0 with PTWF 0.
- The table itself was never involved, so the self-referencing page did not
  help.
- Linux arm-smmu writes SEP = UPSTREAM (7) in every AArch64 context bank for
  this reason.
- Sheng's i564 used TCR2 5, which also has SEP 0, and saw the same level-0
  fault. Its self-referencing page fixes a level-0 start, not this.
- The window at 0x10000000 has bit 31 clear and would have walked.

Candidates ruled out by the decode:

- Walk start level: a wrong start level would read the table, and the entry
  for FAR would be a valid descriptor.
- TCR IRGN/ORGN and the cache clean, and the table placement above 4 GiB
  (0x8c59e8000 < 2^36 = PASize 36): either would show as PTWF, an external or
  address size fault, or a fault at level 1 or deeper, not at level 0 with no
  walk.
- TTBR0 ASID (0) and CBA2R VA64 (1, so the AArch64 format applies, and SEP
  with it).
- TLBI ordering: TLBIALL plus TLBSYNC run after every table register write
  and before M.
- The L1 block format: 0x80000441 is block, AF, AP[1] and AttrIndx 0 at
  level 1, which the 4K granule allows. It is never read in this fault.
- Tables below 4 GiB are not needed.

Fix:

- TCR2 = (old & ~(SEP|PASize)) | SEP 7 | PASize, which is 0x38061 on S224.
- The readback checks both fields. A bank that drops SEP is undone before
  SCTLR, with no START.

New diagnostics:

- the `live` line, read back right before the first START;
- the `walk` line after a translated fault: FSYNR0 fields, SEP sign bit,
  and the L0/L1/L2/L3 entries for FAR.

Same boot value, 15531592. The expected lines on a good run are listed above.
If it still faults, the `walk` line tells the cases apart:

- `sign=0` with `plvl=0` would point at the walk start or TTBR;
- `plvl=1` with `l1[2]=0x80000441` would point at block support or
  attributes, and the next try would be level-2 2 MiB blocks;
- PTWF 1 would point at the table memory or cacheability.

### Direct-DPU-scanout stage 3: per-frame direct scanout of CA surfaces (`fuxigpuncimport` bits 22+23+24)

Stage 2 worked on device (X232, observed): 60 identity STARTs, the scattered
2741-page surface at IOVA 0x10000000, flip latched, 2 s hold, exact restore.
Stage 3 keeps that translation for the boot and lets VIG0 fetch the
CoreAnimation surface itself, with no GPU copy. Everything below is host-tested
only; no device effect has been observed yet.

Boot value: the current base 472710776 plus bits 22 (4194304), 23 (8388608)
and 24 (16777216) is `fuxigpuncimport=502070904` (9 digits). Bit 24 without
22+23 logs `dpu stage3 ignored: bit 24 needs bits 22+23`. Stage 3 also needs
`fuxiscantriple=1` and no autorefresh scanout (bit 131072). Otherwise stage 2
runs alone as before and logs `dpu stage3 not armed reason=...`.

Arming (`fg_scanout_dpu_stage2_run`):

- The window grows to 4 slots of 16 MiB (64 MiB, 32 level-3 tables). Slot k
  is scanout buffer 3+k at IOVA 0x10000000 + k*16 MiB. Stage 2's test surface
  uses slot 0. The hold is 100 ms.
- After A is latched again, the bank is not restored. The test surface's
  leaves are cleared (bank TLBIALL) and the surface is freed. The table, the
  identity 1 GiB blocks and the apps_smmu mapping stay. A, B, C and the boot
  framebuffer still scan out 1:1.

Per present (`SweetFramebuffer.cpp` present, then `sweet_surface_direct` in
`SweetSurface.cpp`, then `fuxi_gpu_scanout_direct_*`):

1. Eligibility:
   - one layer;
   - `sweet_surface_scanout_class` BGRA8 with wired IOBuffer backing;
   - 1125x2436, stride 4608, crop 22,18 1080x2400;
   - page-aligned offset.

   A failure is counted under its reason and that frame is copied.
2. Active writer: 3, the record is kept and retried, as a busy ring is.
3. Slot choice (`direct_slot`): the slot already holding the surface (a hit:
   no page list, no map, no TLBI). On a miss, the least recently queued slot
   that is not the front, fetched, pending (ready) or queued. Its leaves are
   emptied (bank TLBIALL, `slot_evicts`) and SweetSurface drops its reference
   to the old surface.
4. A miss maps every 4 KiB page of the surface (2741 for this geometry) into
   the slot's leaves. Pages that are 0, unaligned or beyond OAS are refused,
   and so is a count below 2741 or above 4096. Then a bank TLBIALL. The pixels
   are cleaned to PoC unless no writer came since the last clean (as bit 20).
   SweetSurface holds one reference per slotted surface.
5. `direct_queue` sets `fg_scanout_copy = 3+slot`, records the writer fence
   and returns. The offer (`fuxi_gpu_scanout_publish`), the START gates, the
   TE spin thread and retry then run unchanged.
6. A slot START writes SRC_IMG_SIZE 0x9840465, SRC_XY 0x120016, YSTRIDE0
   (A's high half | 4608) and SRC0 = the slot IOVA, then FLUSH and START,
   exactly like the stage-2 flip. The first A/B/C START after a slot writes
   A's saved geometry back with the address (`fg_scanout_address`).
7. Writer fence: the ABI sequence at the writer's lock plus 1. This is the
   job submitted under the lock, the same convention as copy priority's
   wfence; it is 0 when no job was submitted. Publish returns 2 (retry) until
   the fence memory reaches it (`fence_polls`, `fence_wait_us`).

Release of the on-screen surface. CA alternates two surfaces, so holding one
until the next present would throttle it. The display stops reading a slot
(its fetch ends) at whichever comes first:

- `release_on_time`: the write line was seen active after the START's TE and
  is now idle with START clear. The spin thread sees this, or a busy poll
  from a waiting writer does.
- the next START (counted late);
- 3 TEs or 3 periods after its START, with START clear and the line idle
  (counted late).

A writer of a slotted surface that the display may still read naps in 250 us
steps, because no wakeup comes for a display read. The wait is bounded at
100 ms and counted as `wait_forced`.

A command-mode panel keeps the image in its own RAM, so the front slot is not
read after its transfer. It is still never evicted.

Fault policy: FSR is read before every START, in busy polls and in every slot
or queue call. Any fault bit, or an unreadable FSR, causes all of this:

- `dpu direct fault where=...` with FAR/FSYNR0 and the walk line;
- RESUME for a stalled transaction and the FSR fault bits cleared;
- a bank TLBIALL;
- a pending slot frame dropped;
- the front moved to a buffer that is neither ready nor being copied, with
  A's geometry (`dpu direct fallback A geometry ...`);
- the identity translation kept;
- GPU copies for the rest of the boot;
- slotted surfaces given back once the display no longer reads them.

Expected log lines on a good run (values from the host model; device
addresses differ):

```
FUXI SCANOUT: dpu stage3 requested: stage2 keeps the translation, CA surfaces scan out through 4 IOVA slots
FUXI SCANOUT: dpu stage2 mapped iova=0x10000000 pages=2741 window=0x4000000 tlbi=bank
FUXI SCANOUT: dpu stage2 hold_ms=100 fsr=0x400
FUXI SCANOUT: dpu stage2 restored A img=... xy=... ystride0=0x10e0 src0=0xb8000000 ...
FUXI SCANOUT: dpu stage3 armed slots=4 iova=0x10000000 slot_bytes=0x1000000 tables=0x... bank=cb3 fsr=0x400 A img=... xy=... ystride0=0x10e0
FUXI SCANOUT: dpu stage2 done result=ok step=arm identity_starts=60 flip_latched=1 hold_ms=100 translation=kept
FUXI SCANOUT: dpu direct map slot=0 iova=0x10000000 pages=2741 above4g=... runs=... first=0x... tlbi=bank      (first 8 maps)
FUXI SCANOUT: dpu direct flip slot=0 src0=0x10000000 img=0x9840465 xy=0x120016 ystride0=0x...1200 te=...    (first 8 flips)
FUXI SCANOUT: dpu direct flips= copies= refused(geometry/stride/offset/format/backing/layers/pages/slots/state)=a/b/c/d/e/f/g/h/i maps= remaps= hits= tlbi= fsr= slot_evicts= release_late= release_on_time= wait_forced= fence_polls= fence_wait_us avg= max= on=1 dead=0 busy=0x..   (every 600 offers)
```

The first 8 map lines and the first 8 flip lines share one budget, so a log
holds 8 of these lines in total.

Host tests:

- `tools/test-fuxi-gpu-scanout.py`, `dpu_stage3_tests`, on the stage-2
  SMMUv2 walk and MDSS fetch model:
  - armed state and kept translation;
  - per flip, every fetched row walks to that surface's own scattered pages
    at the slot IOVA, with slot geometry, and A/B/C fetches have A geometry;
  - hits with no maps or TLBI;
  - on-time and late release;
  - LRU never takes the front, fetched, pending or queued slot, and all four
    busy is refused;
  - page refusals;
  - the fence holds START;
  - the 600-offer line;
  - a fault on a slot fetch or with a pending slot frame leads to copies with
    A geometry and identity kept;
  - stage 3 needs triple;
  - bits off are inert.
- The production `sweet_surface_direct` and writer nap are compiled into the
  cache model.
- The framebuffer test covers present: direct 2 is offered with no copy and no
  CPU guard, 3 keeps the record, and 0 copies.

Risks (guesses, none observed):

- **TLBIALL while a slot is fetched.** It is bank-local and only empties
  leaves the display is not reading, but it does flush the identity walk
  caches mid-transfer.
- **The fence convention.** "Lock sequence + 1" assumes the writer's job is
  the next ABI submission after the lock. A misattributed fence could START a
  half-written frame (tearing, not corruption of other memory).
- **Release timing** relies on START and write-line semantics observed for
  copies. If the line is idle between rows, an on-time release could come
  early and a writer could overwrite pixels still being sent (tearing on one
  frame).
- **Throttling.** With only two CA surfaces, a writer waits for the fetch to
  end, at most about one transfer. The forced bound is 100 ms.
- **Park interplay** (bit 27): unchanged. A direct frame never queues a copy,
  so there is nothing to park.
- **After a fault,** the panel keeps the faulted frame until the next copy.

## Cached-coherent BOs (`fuxigpuncimport` bit 29, value 536870912, default off)

MTLBuffer memory is Turnip type 0: CPU write-combine, GPU non-cacheable. CPU
reads of it (index scans, readbacks) are uncached. Bit 29, together with the
coherent-import self-test (bit 64), lets the kernel hand out IO-coherent
write-back BOs:

- Kernel: `fuxi_gpu_abi_cached_coherent()` latches "enabled" for the boot only
  when bit 29 and 64 are set, the GPU is not poisoned and the self-test passed
  with the wb-ish variant (`fg_abi_coh==1 && fg_abi_coh_wb`). GetDeviceInfo
  runs the self-test early if needed (it would otherwise wait for the first
  import), retrying twice 2 ms apart while busy, and then reports
  `NativeAdrenoDeviceCachedCoherent`. BoAlloc takes
  `NativeAdrenoAllocCachedCoherent` (bit 1 of `contiguous`) only after that:
  the BO is allocated without write-combine, must be BoMap'ed writeback=1
  shareability=3 (the self-test's attributes), and its CPU token maps
  `kIOMapDefaultCache`. Otherwise every path is unchanged.
- Turnip (`tools/patches/mesa-fuxi-cached-coherent.patch`): with the flag, the
  existing DEVICE_LOCAL|HOST_VISIBLE|HOST_COHERENT|HOST_CACHED type is backed
  by such BOs. Query pools (`tu_bo_init_new_cached`) also use it.
- FuxiMetal: `FUXI_MTL_BUFFER_CACHED=1` (default off) picks that type for
  MTLBuffer-backed buffers when allowed, else the first host-visible type as
  before. Textures and host imports are unchanged.

Boot value: 502070904 + 536870912 = `fuxigpuncimport=1038941816` (10 digits,
one more character). Bits 0-28 are all taken and the current bits plus bit 29
exceed 999999999, so 9 digits needs dropping a current bit.

Logs: `FUXI GPU ABI: cached-coherent BOs enabled reason=selftest-wb-ish-pass`
or `... disabled reason=bit29-off|no-selftest-bit64|gpu-poisoned|selftest-pending|selftest-refused|selftest-not-wb`
(each reason once); every 600 submits while enabled or active
`FUXI GPU ABI: cached BOs per 600 submits enabled E allocs A frees F refused R live L live_kb K`;
Turnip `FUXI KMD: cached-coherent memory type exposed (...)`; FuxiMetal
`FUXI MTL: policy buffer_cached=N memory_types=0:DL|HV|HC,1:DL|HV|HC|HCA` and
`FUXI MTL: buffer memory type=T flags=... cached=N`.

Risks (guesses, none observed): the self-test proves CP reads/writes of a
write-back inner-shareable page. Shader (UCHE/TP) reads of such pages are
inferred from imports already in use, not proven. If coherency fails, the GPU
reads stale vertex/index/uniform bytes: visual corruption, not memory
corruption. CPU write-back mappings of GPU-written buffers rely on the same
snooping in the other direction.
