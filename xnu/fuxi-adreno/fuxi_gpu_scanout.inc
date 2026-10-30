/* SPDX-License-Identifier: MIT
 * Copyright (c) 2026 NativeMetal contributors
 * Ported from UniXNU drivers/NativeMetal/GpuScanoutClient.h (MIT).
 * The CPU pixel oracle is adapted below; Fuxi display ownership is original.
 * Finished-frame copy/fence design: NativeMetal/GpuScanoutPresenter.h (MIT).
 * See LICENSE.NativeMetal.
 */
/* GPU-SCANOUT: Fuxi command-mode display ownership. Original implementation.
 * No panel/DSI reset or firmware dependency; optional reserved-buffer flips.
 * fuxipanel120=1 optionally sends the panel's timing-switch commands once,
 * only after ownership and inherited transfer drain. Default does no DSI IO.
 * Register meanings already used by the board's inherited-display probe:
 * INTF1 AUTOREFRESH_CONFIG +0x2b4 bit31; TEAR_LINE_COUNT +0x2b0 low16
 * write line; CTL START +0x1c bit0. Never copy into a buffer a transfer reads.
 * Tear-check block per the SM8550 SDE driver (sde_hw_intf.c): +0x280
 * MDP_VSYNC_SEL, +0x284 TEAR_CHECK_EN, +0x288 SYNC_CONFIG_VSYNC, +0x28c
 * SYNC_CONFIG_HEIGHT, +0x294 VSYNC_INIT_VAL, +0x298 INT_COUNT_VAL (read
 * pointer: high16 TE frame count, low16 line), +0x29c SYNC_THRESH, +0x2a0
 * START_POS, +0x2a8 WR_PTR_IRQ. +0x2a8 is a static setting (reads 1 on
 * device), not the read pointer; earlier logs labelled it rd.
 * fuxiscante=1 (opt-in) replaces the fixed floor with a TE gate: after each
 * START, wait for a TE (frame count change) and some lines past it before
 * the START/write-line checks. A frame count that never changes falls back
 * to the floor for the rest of the boot.
 */
#define FG_SCAN_CTL 0x0ae16000u
#define FG_SCAN_INTF 0x0ae36000u
#define FG_SCAN_DRAIN_US 20000u
#define FG_SCAN_TE_MARGIN 64u     /* lines after TE before testing the write line */
#define FG_SCAN_TE_STALL_US 40000u /* no TE this long after START: counter unusable */
#define FG_SCAN_DSI 0x0ae94000u
#define FG_SCAN_DSI_TIMEOUT_US 20000u
static struct fg_present_times fg_scanout_stamps;
static struct fg_copy_stamps fg_copy_log; /* fuxi_gpu_copy_stamps: frame log detail of the last copy and START */
static unsigned fg_scanout_panel120_attempted, fg_scanout_panel120_active;
static unsigned fg_scanout_period_us = 16667u, fg_scanout_explicit_drain;
static unsigned fg_scanout_owned, fg_scanout_failed, fg_scanout_writable;
static unsigned fg_scanout_reserved, fg_scanout_checks, fg_scanout_checked;
static uintptr_t fg_scanout_pixels, fg_scanout_source;
static unsigned fg_scanout_pitch, fg_scanout_sx, fg_scanout_sy;
static unsigned fg_scanout_dx, fg_scanout_dy, fg_scanout_w, fg_scanout_h;
static uint64_t fg_scanout_after, fg_scanout_timeout;
/* Floor after each START before the transfer status is tested. Boot-arg
 * fuxiscandrain=<us> (4000..20000) lowers it; the START/write-line checks
 * still gate every copy. Wait polls are counted per reason. */
static unsigned fg_scanout_drain_us = FG_SCAN_DRAIN_US;
static unsigned fg_scanout_wait_ring, fg_scanout_wait_floor, fg_scanout_wait_xfer, fg_scanout_starts;
static unsigned fg_scanout_te_mode, fg_scanout_te_frame, fg_scanout_te_init, fg_scanout_wait_te;
static unsigned fg_scanout_te_window, fg_scanout_te_frames, fg_scanout_iv[4];
static uint64_t fg_scanout_started, fg_scanout_last_start, fg_scanout_window_start;
static int fg_abi_observe(void);
static unsigned fg_abi_start; /* fuxigpuncimport bit 8192: panel120 START floor of 3/4 period; the framebuffer polls START retries at 1/64 tick */
/* Measurement only. xfer: START write to the first poll that sees CTL START clear and the write line idle (quantized by the poll
 * interval); sampled on every acquire, also inside the floor. r2s: newest back buffer ready to START, split by the reason each
 * blocked poll returned (the interval since the previous poll is charged to that poll's reason). */
enum { FG_WHY_RING, FG_WHY_FLOOR, FG_WHY_TE, FG_WHY_XFER };
static const unsigned fg_scanout_xfer_edges[7] = { 5000, 6000, 7000, 7500, 8000, 9000, 12000 };
static unsigned fg_scanout_xfer_open, fg_scanout_xfer_n, fg_scanout_xfer_hist[8], fg_scanout_last_why;
static uint64_t fg_scanout_xfer_sum, fg_scanout_xfer_max, fg_scanout_ready_at, fg_scanout_charged_at, fg_scanout_charge[4], fg_scanout_r2s, fg_scanout_r2s_n;
static void fg_scanout_charge_to(unsigned why)
{
 if (!fg_scanout_ready_at) return;
 uint64_t now = rsr("cntpct_el0");
 if (!fg_scanout_charged_at) fg_scanout_charged_at = fg_scanout_ready_at;
 if (now > fg_scanout_charged_at) fg_scanout_charge[why] += now - fg_scanout_charged_at;
 fg_scanout_charged_at = now; fg_scanout_last_why = why;
}
static int fg_scanout_read(unsigned pa, unsigned *value);
static void fg_scanout_xfer_sample(void)
{
 unsigned line, start;
 if (!fg_scanout_xfer_open || !fg_scanout_read(FG_SCAN_INTF+0x2b0, &line) || !fg_scanout_read(FG_SCAN_CTL+0x1c, &start)) return;
 if ((start & 1) || ((line & 65535) != 0 && (line & 65535) < 2400)) return;
 uint64_t hz = rsr("cntfrq_el0"), us = (rsr("cntpct_el0") - fg_scanout_started)*1000000/hz;
 unsigned bin = 0;
 while (bin < 7 && us >= fg_scanout_xfer_edges[bin]) bin++;
 fg_scanout_xfer_hist[bin]++; fg_scanout_xfer_n++; fg_scanout_xfer_sum += us;
 if (us > fg_scanout_xfer_max) fg_scanout_xfer_max = us;
 fg_scanout_xfer_open = 0; fg_copy_log.xfer_us = (uint32_t)us;
}
static unsigned fg_scanout_double_active, fg_scanout_double_initialized, fg_scanout_front;
static unsigned fg_scanout_back_ready, fg_scanout_flips, fg_scanout_ready_drops, fg_scanout_retries;
static unsigned fg_scanout_current_match, fg_scanout_current_mismatch, fg_scanout_reentries;
static uintptr_t fg_scanout_pixels_b, fg_scanout_pixels_c;
static unsigned fg_scanout_ready, fg_scanout_copy, fg_scanout_buffer_flips[3];
/* fuxigpuncimport bit 20: copy-source clean outside the ring hold (SweetSurface), SSPP survey. */
static unsigned fg_scanout_preclean;
static unsigned fg_scanout_dpu_dump; /* fuxigpuncimport bit 22: read-only DPU/apps_smmu survey at takeover (fg_scanout_dpu_survey) */
static unsigned fg_scanout_dpu_stage2; /* fuxigpuncimport bits 22+23: one translated display test after the drain (fg_scanout_dpu_stage2_run) */
static unsigned fg_scanout_target(void)
{
 for (unsigned i = 0; i < (fg_scanout_triple_requested ? 3u : 2u); i++)
  if (i != fg_scanout_front && (!fg_scanout_back_ready || i != fg_scanout_ready)) return i;
 return fg_scanout_front ^ 1u;
}
static unsigned fg_scanout_pa(unsigned buffer) { return 0xb8000000u + buffer*0xa00000u; }
static uintptr_t fg_scanout_pixels_at(unsigned buffer) { return buffer == 2 ? fg_scanout_pixels_c : buffer ? fg_scanout_pixels_b : fg_scanout_pixels; }
static int fg_scanout_acquire(unsigned publication);
static int fg_scanout_stop(const char *reason);

void fuxi_gpu_scanout_configure(uintptr_t pixels, unsigned enabled, unsigned check)
{
 /* AppleCLCD calls once, before fuxi_gpu_start publishes the GPU. */
 if (fg_scanout_double_initialized || (fg_scanout_double_requested && __atomic_load_n(&fg_scanout_owned, __ATOMIC_ACQUIRE))) {
  FG_GUARD;
  (void)fg_scanout_stop(enabled == 1 ? "reconfigure while owned" : "disabled");
  return;
 }
 fg_scanout_pixels = pixels;
 fg_scanout_requested = enabled == 1;
 fg_scanout_checks = fg_scanout_requested && check == 1 ? 4 : 0;
 if (fg_scanout_requested)
{
  unsigned drain = 0, te = 0, panel120 = 0, double_buffer = 0, triple_buffer = 0, nc = 0;
  fg_scanout_preclean = PE_parse_boot_argn("fuxigpuncimport", &nc, sizeof(nc)) && (nc & 0x100000u);
  fg_scanout_dpu_dump = (nc & 0x400000u) != 0; /* bit 22: read-only DPU/SMMU survey at takeover */
  fg_scanout_dpu_stage2 = (nc & 0xc00000u) == 0xc00000u; /* bit 23 needs bit 22 */
  fg_scanout_double_requested = PE_parse_boot_argn("fuxiscandouble", &double_buffer, sizeof(double_buffer)) && double_buffer == 1;
  unsigned triple = PE_parse_boot_argn("fuxiscantriple", &triple_buffer, sizeof(triple_buffer)) && triple_buffer == 1;
  fg_scanout_triple_requested = fg_scanout_double_requested && triple;
  fg_scanout_panel120_requested = PE_parse_boot_argn("fuxipanel120", &panel120, sizeof(panel120)) && panel120 == 1;
  fg_scanout_te_mode = PE_parse_boot_argn("fuxiscante", &te, sizeof(te)) && te == 1;
  /* The TE gate needs no floor unless one is given explicitly. */
  if (fg_scanout_te_mode) fg_scanout_drain_us = 0;
  if (PE_parse_boot_argn("fuxiscandrain", &drain, sizeof(drain)) && drain >= 4000 && drain <= FG_SCAN_DRAIN_US) {
   fg_scanout_drain_us = drain;
   fg_scanout_explicit_drain = 1;
  }
  printf("FUXI SCANOUT: requested, fence then manual transfer, drain_us=%u te_gate=%u check=%u\n",
      fg_scanout_drain_us, fg_scanout_te_mode, fg_scanout_checks);
  if (fg_scanout_preclean) printf("FUXI SCANOUT: preclean=1 copy source cleaned before the ring reservation\n");
  if (fg_scanout_dpu_dump) printf("FUXI SCANOUT: dpu smmu survey requested (read only)\n");
  if (fg_scanout_dpu_stage2) printf("FUXI SCANOUT: dpu stage2 requested: one translated display test after the inherited drain\n");
  else if (nc & 0x800000u) printf("FUXI SCANOUT: dpu stage2 ignored: bit 23 needs bit 22\n");
 }
}
int fuxi_gpu_scanout_preclean(void) { return fg_scanout_preclean; }
void fuxi_gpu_scanout_stamps(struct fg_present_times *times)
{
 FG_GUARD;
 *times = fg_scanout_stamps;
}
void fuxi_gpu_copy_stamps(struct fg_copy_stamps *stamps)
{
 FG_GUARD;
 *stamps = fg_copy_log;
}
int fuxi_gpu_scanout_owned(void)
{
 return __atomic_load_n(&fg_scanout_owned, __ATOMIC_ACQUIRE);
}
static int fg_scanout_read(unsigned pa, unsigned *value)
{
 uint64_t v = 0;
 if (fg_access(pa, &v, 0, 0)) return 0;
 *value = (unsigned)v;
 return 1;
}
static int fg_scanout_write(unsigned pa, unsigned value)
{
 uint64_t v = value;
 return !fg_access(pa, &v, 1, 0);
}
/* Opt-in measurement (fuxigpuncimport bit 16384): after every 8th START a thread call spins on CTL START and the INTF write line
 * (about 10 us apart, at most 20 ms) and stamps, relative to the START write: the first TE count change ("edge"), the first
 * START-bit-clear ("clear"), the first write-line idle after it was active ("lineend", 0 when never seen active) and "done" = the
 * later of clear and lineend, the same condition acquire uses. The worker only stores results; accounting runs under FG_GUARD at the
 * next publish, so a window is consistent. Read-only MMIO, no behaviour change. Not yet observed on device. */
static unsigned fg_abi_probe, fg_abi_spin, fg_abi_mid, fg_abi_ar, fg_abi_hard, fg_scanout_ar_state; /* ar_state: 0 manual, 1 verifying, 2 active, 3 disabled, 4 arming */
static int fg_scanout_ar_on(void) { return fg_scanout_ar_state == 1 || fg_scanout_ar_state == 2; }
/* Opt-in (fuxigpuncimport bit 32768), panel120 + double/triple only. The 1080x2400 transfer runs ~8.0 ms from the TE edge in an 8.3 ms
 * period, so a START catches the next TE only if written in the ~0.3 ms between transfer-done and that TE. The first version woke a
 * fixed 93% of a period after the START write, but the transfer begins at the next TE after the START, up to a period later, so it woke
 * at a random phase (about 10 of 570 hits). Now the wake is referenced to the TE edge: a phase model (last observed edge tick and
 * count, period refined from edge-count deltas) predicts the edge E that starts the transfer, the thread call wakes at E + done_est -
 * 400 us and spins on the write line. It publishes at the first idle sample only after an edge was seen and the line was seen active
 * (a missed START latch cannot look like done), then spins up to 600 us for the next edge, which resyncs the model, measures E to idle
 * (done_est) and the prediction error, and re-kicks itself for the new START so the chain continues without the display path. No model
 * (first use, or stale after 64 periods): one sync wake catches an edge from the START's count. Every gate in fg_scanout_publish() is
 * unchanged; with no back buffer or after a raced START nothing is published and the display path stays the fallback. */
#define FG_SCAN_SPIN_LEAD_US 300u
#define FG_SCAN_SPIN_LEAD_MAX_US 1500u
#define FG_SCAN_SPIN_LATE_US 500u
#define FG_SCAN_SPIN_MINXFER_US 7000u
#define FG_SCAN_SPIN_NEAR_US 150u
#define FG_SCAN_SPIN_SAFE_US 500u
#define FG_SCAN_SPIN_SEEN_US 1000u
#define FG_SCAN_SPIN_REJECT_US 500u
#define FG_SCAN_SPIN_DONE_US 7950u
#define FG_SCAN_SPIN_TAIL_PCT 6u
#define FG_SCAN_SPIN_CATCH_US 600u
/* Opt-in (fuxigpuncimport bit 262144, needs bit 32768): hard spin. The thread call is armed FG_SCAN_HARD_LEAD_US before the predicted done
 * (it wakes ~0.4 ms late on average, up to 3 ms), then polls the done condition with ~1 us steps, interrupts masked in 50 us slices (never
 * across the FG_GUARD lock), for at most FG_SCAN_HARD_MAX_US per frame (overrun counted); the START goes out at the first done sample.
 * The thread is not bound to a core (the scheduler binding helpers live in the perf module). Not yet observed on device. */
#define FG_SCAN_HARD_LEAD_US 1500u
#define FG_SCAN_HARD_MAX_US 2000u
#define FG_SCAN_HARD_SLICE_US 50u
/* near_edge (hard spin): a START within 150 us before the predicted edge is not unsafe (a START that misses the TE just waits one more
 * period, the TE gate is in hardware), it only made the model unsure which edge starts the transfer, so no wake was armed and the frame
 * was left to the display path's poll. The CTL START bit clears ~17 us after the write; with the spin's missed-edge replan a plan at 20 us
 * is the floor. */
#define FG_SCAN_HARD_NEAR_US 20u
enum {FG_SPIN_WAKES, FG_SPIN_PUBLISHED, FG_SPIN_NO_BACK, FG_SPIN_RACED, FG_SPIN_EXPIRED, FG_SPIN_HIT, FG_SPIN_LATE, FG_SPIN_NO_ACTIVE,
      FG_SPIN_SYNCS, FG_SPIN_SYNC_FAIL, FG_SPIN_CATCH_FAIL, FG_SPIN_SKIPPED, FG_SPIN_NEAR_EDGE, FG_SPIN_ARRIVAL, FG_SPIN_MISSED, FG_SPIN_PHASE_REJECT, FG_SPIN_DIRECT, FG_SPIN_OFFERED,
      FG_SPIN_ARR_BUSY, FG_SPIN_ARR_STALE, FG_SPIN_ARR_PAST, FG_SPIN_COPY_OPEN, FG_SPIN_UNSTOLEN, FG_SPIN_STALL, FG_SPIN_REFUSED, FG_SPIN_WAIT_DONE, FG_SPIN_IN_WINDOW, FG_SPIN_OVERRUN, FG_SPIN_IDLE_ARRIVAL, FG_SPIN_COPY_STARTED, FG_SPIN_COPY_SAME, FG_SPIN_N};
static thread_call_data_t fg_scanout_spin_call;
static unsigned fg_scanout_spin_setup, fg_scanout_spin_busy, fg_scanout_spin_serial, fg_scanout_async_published, fg_scanout_spin_sync, fg_scanout_spin_c0;
static unsigned fg_scanout_last_count, fg_scanout_ph_ok, fg_scanout_ph_c, fg_scanout_ph_done_us, fg_scanout_ph_late_us;
static uint64_t fg_scanout_ph_t, fg_scanout_ph_p, fg_scanout_ph_e, fg_scanout_ph_wake;
static unsigned fg_scanout_spin_stat[FG_SPIN_N]; /* FG_SPIN_* */
static unsigned fg_scanout_hard_d2s[5], fg_scanout_hard_n, fg_scanout_miss_why[5], fg_scanout_kick_why, fg_scanout_offer_xfer; /* kick_why: 0 armed, 1 thread busy, 2 no phase model, 3 near edge, 4 edge past */
static uint64_t fg_scanout_hard_sum, fg_scanout_hard_max, fg_scanout_hard_total;
static uint64_t fg_scanout_spin_err_sum, fg_scanout_spin_err_max, fg_scanout_spin_late_sum, fg_scanout_spin_late_max;
static unsigned fg_scanout_spin_err_n, fg_scanout_spin_late_n, fg_scanout_spin_replanned, fg_scanout_spin_rejects, fg_scanout_spin_mw[4], fg_scanout_spin_mm[4];
/* The display path owns the writable token from its acquire() until the copy is offered through fuxi_gpu_scanout_publish() (or the next
 * retry): a START by the spin thread in that window consumed the token, the offer then returned -1 and the framebuffer held scanout
 * for good (Y178: START count froze near 600). copy_open marks the window; stolen lets a stolen token be restored. */
static unsigned fg_scanout_copy_open, fg_scanout_spin_stolen, fg_scanout_stall_prints;
static uint64_t fg_scanout_spin_m; /* START-write to predicted-edge margin of the armed wake (ticks) */
#define FG_PROBE_MAX 128
static thread_call_data_t fg_scanout_probe_call;
static unsigned fg_scanout_probe_setup, fg_scanout_probe_busy, fg_scanout_probe_ready, fg_scanout_probe_te0;
static uint64_t fg_scanout_probe_t0, fg_scanout_probe_res[4]; /* edge, clear, lineend, done (ticks; 0 = not seen) */
static unsigned fg_scanout_probe_n, fg_scanout_probe_timeouts, fg_scanout_probe_edges, fg_scanout_probe_hist[18];
static unsigned fg_scanout_probe_done[FG_PROBE_MAX], fg_scanout_probe_te2d[FG_PROBE_MAX];
static uint64_t fg_scanout_probe_sum[4];
static void fg_scanout_probe_run(thread_call_param_t a, thread_call_param_t b)
{
 (void)a; (void)b;
 uint64_t hz = rsr("cntfrq_el0"), t0 = fg_scanout_probe_t0, limit = t0 + hz/50, res[4] = {0, 0, 0, 0};
 unsigned active = 0;
 for (;;) {
  unsigned count, line, start;
  uint64_t now = rsr("cntpct_el0");
  if (!fg_scanout_read(FG_SCAN_INTF+0x298, &count) || !fg_scanout_read(FG_SCAN_INTF+0x2b0, &line) ||
      !fg_scanout_read(FG_SCAN_CTL+0x1c, &start)) break;
  if (!res[0] && (count >> 16) != fg_scanout_probe_te0) res[0] = now - t0 + 1;
  if (!res[1] && !(start & 1)) res[1] = now - t0 + 1;
  if ((line & 65535) != 0 && (line & 65535) < 2400) active = 1;
  else if (active && !res[2]) res[2] = now - t0 + 1;
  /* START clears ~17 us after the write, before the MDP starts writing at the next TE: finishing on "clear and never active" ended
   * nearly every probe early. Wait for the TE edge, then 0.5 ms for the line to go active (otherwise the START was a no-op). */
  if (res[1] && res[0] && (res[2] || (!active && now - t0 >= res[0] - 1 + hz/2000))) { res[3] = res[1] > res[2] ? res[1] : res[2]; break; }
  if (now >= limit) break;
  delay(10);
 }
 for (unsigned i = 0; i < 4; i++) fg_scanout_probe_res[i] = res[i];
 __atomic_store_n(&fg_scanout_probe_ready, 1, __ATOMIC_RELEASE);
}
static int fg_scanout_publish(void);
static int fg_scanout_te_count(unsigned *count)
{
 unsigned v;
 if (!fg_scanout_read(FG_SCAN_INTF+0x298, &v)) return 0;
 *count = v >> 16;
 return 1;
}
/* First sample (cntpct) at which the TE count differs from c0, 0 on timeout or read failure. */
static uint64_t fg_scanout_te_catch(unsigned c0, uint64_t limit, unsigned *count)
{
 for (;;) {
  uint64_t now = rsr("cntpct_el0");
  unsigned v;
  if (!fg_scanout_te_count(&v)) return 0;
  if (v != c0) { *count = v; return now; }
  if (now >= limit) return 0;
  delay(2);
 }
}
static uint64_t fg_scanout_us_to_ticks(uint64_t us) { return us*rsr("cntfrq_el0")/1000000; }
static void fg_scanout_phase_set(uint64_t t, unsigned c)
{
 unsigned n = (c - fg_scanout_ph_c) & 0xffffu;
 if (fg_scanout_ph_ok && n >= 1 && n <= 200 && t > fg_scanout_ph_t) {
  uint64_t p = (t - fg_scanout_ph_t)/n, q = fg_scanout_ph_p;
  if (p > q - q/20 && p < q + q/20) fg_scanout_ph_p = q - q/8 + p/8;
 }
 fg_scanout_ph_t = t; fg_scanout_ph_c = c; fg_scanout_ph_ok = 1;
}
/* First modelled edge at or after "after" (an edge within the START latch time is still counted; the active check resolves it). */
static uint64_t fg_scanout_phase_next(uint64_t after)
{
 uint64_t t = fg_scanout_ph_t, p = fg_scanout_ph_p;
 return after <= t ? t : t + ((after - t)/p + 1)*p;
}
static void fg_scanout_spin_schedule(uint64_t wake)
{
 uint64_t now = rsr("cntpct_el0"), d;
 if (wake < now) wake = now;
 nanoseconds_to_absolutetime((wake - now)*1000000000ull/rsr("cntfrq_el0"), &d);
 fg_scanout_ph_wake = wake; fg_scanout_spin_busy = 1;
 (void)thread_call_enter_delayed(&fg_scanout_spin_call, mach_absolute_time() + d);
}
/* Wake before the predicted transfer-done of the transfer that starts at edge e: a base lead plus 1.5x the smoothed thread-call lateness
 * (W176: avg 0.5 ms, max 1.1 ms; a 0.4 ms lead woke after done and never saw the line active), capped. */
static unsigned fg_scanout_spin_lead_us(void)
{
 if (fg_abi_hard) return FG_SCAN_HARD_LEAD_US;
 unsigned lead = FG_SCAN_SPIN_LEAD_US + fg_scanout_ph_late_us*3/2;
 return lead > FG_SCAN_SPIN_LEAD_MAX_US ? FG_SCAN_SPIN_LEAD_MAX_US : lead;
}
static void fg_scanout_spin_plan(uint64_t e, uint64_t m)
{
 unsigned lead = fg_scanout_spin_lead_us();
 fg_scanout_spin_sync = 0; fg_scanout_ph_e = e; fg_scanout_spin_m = m;
 fg_scanout_spin_schedule(e + fg_scanout_us_to_ticks(fg_scanout_ph_done_us > lead ? fg_scanout_ph_done_us - lead : 0));
}
static void fg_scanout_spin_late(uint64_t late)
{
 unsigned us = (unsigned)(late*1000000/rsr("cntfrq_el0"));
 fg_scanout_ph_late_us = fg_scanout_ph_late_us - fg_scanout_ph_late_us/4 + us/4;
}
static void fg_scanout_spin_kick(int fresh);
/* v6 (hard spin): a copy into buffer X is open (token held by the display path, GPU copy job possibly busy) while the buffer Y it was
 * not asked to touch is already published and waiting. Under triple buffering X != Y by construction (the target skips front and ready),
 * so Y can go to the panel without waiting for the copy: the START needs neither the idle ring nor the token, X is offered later and
 * then replaces or follows Y. Same-buffer or double-buffer cases still refuse. */
static int fg_scanout_overlap_ok(void)
{
 return fg_scanout_triple_requested && fg_scanout_double_active && fg_scanout_back_ready && fg_scanout_copy_open &&
     fg_scanout_copy < 3 && fg_scanout_copy != fg_scanout_ready;
}
/* Done seen at idle_t, START written (fg_scanout_last_start): histogram of the gap against the ~360 us window. */
static void fg_scanout_hard_note(uint64_t idle_t)
{
 uint64_t d = fg_scanout_last_start > idle_t ? fg_scanout_last_start - idle_t : 0, us = d*1000000/rsr("cntfrq_el0");
 fg_scanout_hard_d2s[us < 50 ? 0 : us < 100 ? 1 : us < 200 ? 2 : us < 360 ? 3 : 4]++;
 if (us < 360) fg_scanout_spin_stat[FG_SPIN_IN_WINDOW]++;
}
static void fg_scanout_spin_run(thread_call_param_t a, thread_call_param_t b)
{
 (void)a; (void)b;
 uint64_t t0 = rsr("cntpct_el0"), p = fg_scanout_ph_p, e = fg_scanout_ph_e, late = t0 > fg_scanout_ph_wake ? t0 - fg_scanout_ph_wake : 0;
 unsigned serial = fg_scanout_spin_serial, c0 = fg_scanout_spin_c0, result = FG_SPIN_EXPIRED, active = 0, nedge = 0, c = c0, published = 0, hard = fg_abi_hard;
 uint64_t idle_t = 0, edge_t = 0;
 fg_scanout_spin_stat[FG_SPIN_WAKES]++;
 unsigned want = 1 + fg_scanout_spin_replanned, mb = fg_scanout_spin_m < fg_scanout_us_to_ticks(500) ? 0 : fg_scanout_spin_m < fg_scanout_us_to_ticks(1000) ? 1 : fg_scanout_spin_m < fg_scanout_us_to_ticks(2000) ? 2 : 3;
 if (!fg_scanout_spin_sync && !fg_scanout_spin_replanned) fg_scanout_spin_mw[mb]++;
 if (fg_scanout_spin_sync) {
  /* No usable phase: catch the edge that starts this START's transfer (c0 was read right after the START write). */
  edge_t = fg_scanout_te_catch(c0, t0 + p + p/4, &c);
  if (edge_t) fg_scanout_phase_set(edge_t, c);
  FG_GUARD;
  fg_scanout_spin_stat[edge_t ? FG_SPIN_SYNCS : FG_SPIN_SYNC_FAIL]++; fg_scanout_spin_busy = 0; fg_scanout_spin_late(late);
  if (fg_scanout_starts != serial) fg_scanout_spin_kick(1);
  else if (edge_t && ((c - c0) & 0xffffu) == 1 && !fg_scanout_failed) { fg_scanout_spin_replanned = 0; fg_scanout_spin_plan(edge_t, rsr("cntfrq_el0")); }
  return;
 }
 {
  uint64_t end = e + p + p*FG_SCAN_SPIN_TAIL_PCT/100, slice = 0, hard_limit = t0 + fg_scanout_us_to_ticks(FG_SCAN_HARD_MAX_US);
  unsigned masked = 0;
  boolean_t irq = FALSE;
  for (;;) {
   unsigned line, start, cnt;
   uint64_t now = rsr("cntpct_el0");
   if (hard && !masked) { irq = ml_set_interrupts_enabled(FALSE); masked = 1; slice = now + fg_scanout_us_to_ticks(FG_SCAN_HARD_SLICE_US); }
   if (!fg_scanout_te_count(&cnt) || !fg_scanout_read(FG_SCAN_INTF+0x2b0, &line) || !fg_scanout_read(FG_SCAN_CTL+0x1c, &start)) break;
   nedge = (cnt - c0) & 0xffffu; c = cnt; line &= 65535;
   if (nedge && line != 0 && line < 2400) active = 1;
   /* An idle line with the START bit clear between 1 ms and the minimum transfer time after the predicted edge means the transfer never
    * started there (the START missed that TE, W177: expired/catch_fail/raced ~25% of wakes): it starts one period later. */
   if (nedge && !active && !(start & 1) && (line == 0 || line >= 2400) && now >= e + fg_scanout_us_to_ticks(FG_SCAN_SPIN_SEEN_US) &&
       now < e + fg_scanout_us_to_ticks(FG_SCAN_SPIN_MINXFER_US)) { result = FG_SPIN_MISSED; break; }
   /* Done = an edge since the START, the write line idle and either seen active or (START margin to the edge at least 500 us, so the
    * edge was caught) the minimum transfer time elapsed since the edge: the thread call is often late enough to miss the active
    * period (W176). */
   if (nedge && (active || (fg_scanout_spin_m >= fg_scanout_us_to_ticks(FG_SCAN_SPIN_SAFE_US) && now >= e + fg_scanout_us_to_ticks(FG_SCAN_SPIN_MINXFER_US))) &&
       !(start & 1) && (line == 0 || line >= 2400)) {
    idle_t = now;
    if (masked) { (void)ml_set_interrupts_enabled(irq); masked = 0; }
    FG_GUARD;
    if (hard && fg_scanout_starts == serial && fg_scanout_back_ready) fg_scanout_spin_stat[FG_SPIN_WAIT_DONE]++;
    unsigned ov = hard && fg_scanout_overlap_ok();
    if (fg_scanout_starts != serial || fg_scanout_failed || !fg_scanout_back_ready || (fg_busy && !ov)) result = fg_scanout_back_ready ? FG_SPIN_RACED : FG_SPIN_NO_BACK;
    else if (fg_scanout_copy_open && !ov) { result = FG_SPIN_COPY_OPEN; if (hard) fg_scanout_spin_stat[FG_SPIN_COPY_SAME]++; }
    else if (fg_scanout_publish() == 1) {
     fg_scanout_async_published = 1; fg_scanout_spin_stolen = !ov; published = 1; result = FG_SPIN_PUBLISHED;
     if (hard) fg_scanout_hard_note(idle_t);
     if (ov) fg_scanout_spin_stat[FG_SPIN_COPY_STARTED]++;
    }
    else result = FG_SPIN_RACED;
    break;
   }
   if (hard && now >= hard_limit) { result = FG_SPIN_OVERRUN; break; }
   if (now >= end) { result = active ? FG_SPIN_EXPIRED : FG_SPIN_NO_ACTIVE; break; }
   if (hard) { if (now >= slice) { (void)ml_set_interrupts_enabled(irq); masked = 0; } delay(1); } else delay(2);
  }
  if (masked) (void)ml_set_interrupts_enabled(irq);
 }
 /* The next edge arrives within ~0.3 ms of an on-time idle: catch it to resync the model, measure E-to-idle and the prediction error. */
 if (idle_t) {
  unsigned c2 = 0;
  edge_t = fg_scanout_te_catch(c, idle_t + fg_scanout_us_to_ticks(FG_SCAN_SPIN_CATCH_US), &c2);
  uint64_t dm = edge_t > fg_scanout_ph_t ? (edge_t - fg_scanout_ph_t) % p : 0, off = dm < p - dm ? dm : p - dm;
  if (edge_t && edge_t > fg_scanout_ph_t && off > fg_scanout_us_to_ticks(FG_SCAN_SPIN_REJECT_US) && fg_scanout_spin_rejects < 3) {
   /* A sample far off the modelled phase (preempted between the clock read and the TE read) must not move the model; three in a row re-lock it. */
   fg_scanout_spin_rejects++; fg_scanout_spin_stat[FG_SPIN_PHASE_REJECT]++;
  } else if (edge_t) {
   fg_scanout_spin_rejects = 0;
   if (nedge == want) {
    uint64_t pred = e + p*want, err = edge_t > pred ? edge_t - pred : pred - edge_t, done = idle_t + p - edge_t;
    fg_scanout_spin_err_sum += err; fg_scanout_spin_err_n++; if (err > fg_scanout_spin_err_max) fg_scanout_spin_err_max = err;
    if (active && idle_t + p > edge_t && done > fg_scanout_us_to_ticks(5000) && done < p) {
     unsigned us = (unsigned)(done*1000000/rsr("cntfrq_el0"));
     fg_scanout_ph_done_us = fg_scanout_ph_done_us - fg_scanout_ph_done_us/4 + us/4;
    }
    fg_scanout_phase_set(edge_t, c2);
   } else if (((c2 - fg_scanout_ph_c) & 0xffffu) < 200) fg_scanout_phase_set(edge_t, c2);
  } else fg_scanout_spin_stat[FG_SPIN_CATCH_FAIL]++;
 }
 {
  FG_GUARD;
  fg_scanout_spin_stat[result]++;
  if (hard) {
   uint64_t d = rsr("cntpct_el0") - t0;
   fg_scanout_hard_sum += d; fg_scanout_hard_n++; fg_scanout_hard_total += d; if (d > fg_scanout_hard_max) fg_scanout_hard_max = d;
  }
  if (result == FG_SPIN_MISSED && !fg_scanout_spin_replanned) fg_scanout_spin_mm[mb]++;
  if (published) fg_scanout_spin_stat[nedge == want ? FG_SPIN_HIT : FG_SPIN_LATE]++;
  fg_scanout_spin_late_sum += late; fg_scanout_spin_late_n++; if (late > fg_scanout_spin_late_max) fg_scanout_spin_late_max = late;
  fg_scanout_spin_busy = 0; fg_scanout_spin_late(late);
  /* A newer START (ours, or the display path's while this call was busy) has no pending wake: arm it now. A missed edge is replanned
   * once for the following one. */
  if (fg_scanout_starts != serial) fg_scanout_spin_kick(1);
  else if (result == FG_SPIN_MISSED && !fg_scanout_spin_replanned && !fg_scanout_failed) { fg_scanout_spin_replanned = 1; fg_scanout_spin_plan(e + p, rsr("cntfrq_el0")); }
 }
}
/* Called under FG_GUARD right after a START (fresh) and when a back buffer becomes ready while that transfer is still running. With a
 * usable model it arms only when a frame is waiting (W176: 55% of wakes found none), so the wake follows the frame, not every START;
 * without a model only a fresh START can sync (its count is the reference). A START less than 60 us before the predicted edge may have
 * missed it, so it is not trusted. */
static void fg_scanout_spin_kick(int fresh)
{
 if (!fg_abi_spin || !fg_scanout_panel120_active || !fg_scanout_double_active || fg_scanout_ar_on()) return;
 if (fg_scanout_spin_busy) { if (!fresh) { fg_scanout_spin_stat[FG_SPIN_ARR_BUSY]++; fg_scanout_kick_why = 1; } return; }
 if (!fg_scanout_spin_setup) {
  thread_call_setup_with_options(&fg_scanout_spin_call, fg_scanout_spin_run, NULL, THREAD_CALL_PRIORITY_KERNEL, THREAD_CALL_OPTIONS_ONCE);
  fg_scanout_spin_setup = 1;
 }
 uint64_t now = rsr("cntpct_el0");
 if (!fg_scanout_ph_p) fg_scanout_ph_p = fg_scanout_us_to_ticks(fg_scanout_period_us);
 if (!fg_scanout_ph_done_us) fg_scanout_ph_done_us = FG_SCAN_SPIN_DONE_US;
 if (!fg_scanout_ph_late_us) fg_scanout_ph_late_us = FG_SCAN_SPIN_LATE_US;
 if (!fg_scanout_ph_ok || now - fg_scanout_ph_t > 64*fg_scanout_ph_p) {
  if (!fresh) { fg_scanout_spin_stat[FG_SPIN_ARR_STALE]++; fg_scanout_kick_why = 2; return; }
  fg_scanout_spin_replanned = 0;
  fg_scanout_spin_serial = fg_scanout_starts; fg_scanout_spin_c0 = fg_scanout_last_count;
  fg_scanout_spin_sync = 1; fg_scanout_spin_schedule(now);
  return;
 }
 if (!fg_scanout_back_ready) { if (fresh) fg_scanout_spin_stat[FG_SPIN_SKIPPED]++; return; }
 uint64_t e = fg_scanout_phase_next(fg_scanout_last_start);
 if (e - fg_scanout_last_start < fg_scanout_us_to_ticks(fg_abi_hard ? FG_SCAN_HARD_NEAR_US : FG_SCAN_SPIN_NEAR_US)) { fg_scanout_spin_stat[FG_SPIN_NEAR_EDGE]++; fg_scanout_kick_why = 3; return; }
 if (now >= e + fg_scanout_ph_p) { fg_scanout_spin_stat[FG_SPIN_ARR_PAST]++; fg_scanout_kick_why = 4; return; }
 if (!fresh) fg_scanout_spin_stat[FG_SPIN_ARRIVAL]++;
 fg_scanout_spin_replanned = 0;
 fg_scanout_spin_serial = fg_scanout_starts; fg_scanout_spin_c0 = fg_scanout_last_count;
 fg_scanout_spin_plan(e, e - fg_scanout_last_start);
}
/* Opt-in (fuxigpuncimport bit 65536), panel120 only: START anywhere in the tear-safe part of the TE period instead of only at the TE.
 * Facts used: SYNC_THRESH (+0x29c) low 16 bits are the tear-check start threshold (4 after ABL): a START waits for the next TE unless the
 * read pointer (INT_COUNT_VAL low 16, which reloads to VSYNC_INIT_VAL at each TE and counts the 2684 lines of a period) is within that
 * many lines of the reload (observed: START to transfer start averages ~4.4 ms). Tear model, line k read at r_k = k*b and written at
 * w_k = s + k*a after the TE, s = START delay, writer a = 7.92 ms/2400 lines, reader b between 7.3 ms/2400 and 8.28 ms/2400 (not
 * measured): the current frame is consistent (all old) when s >= max(0, 2399*(b-a)) (<= ~0.36 ms), and the next frame is all new when
 * s <= P - max(0, 2399*(a-b)) (>= ~7.7 ms). So with a margin the safe window is 160..2420 lines after the TE (0.5..7.5 ms). Raising the
 * threshold to 2420 lets the hardware itself hold a START outside the upper bound, and software blocks the lower part (5..159 lines,
 * ~0.5 ms) and anything above the upper bound; a START at <= 4 lines is the hardware's own case. Blocking never lasts more than two
 * periods. The first 8 in-window STARTs are verified: if the write line does not go active within 200 us of the START the hardware is
 * still waiting for the TE (gate not opened by the threshold), and after 4 such results the threshold is restored (state 3, off).
 * The reader/writer speeds and the threshold semantics are not measured: not yet observed on device. */
#define FG_SCAN_MID_LO 160u
#define FG_SCAN_MID_HI 2420u
#define FG_SCAN_MID_TRIALS 4u
#define FG_SCAN_MID_NVAR 5u
/* Variants of the way to open the hardware start gate, tried in order within one boot (device B181: low16 of SYNC_THRESH alone did not open it):
 * 0 high16 of SYNC_THRESH = 2420 (low16 kept); 1 both halves = 2420; 2 START_POS (+0x2a0) set just past the current read line before each
 * in-window START and restored after it; 3 TEAR_CHECK_EN (+0x284) cleared for each in-window START and restored after it (only tried if the
 * read counter keeps running with the enable cleared, checked once with interrupts masked). A variant ends after 4 trials that began writing at
 * once (confirmed) or 4 that waited for the TE (failed: registers restored, next variant). Device D183: variants 0-3 all held (the gate is not in
 * the INTF tear check); 4 is tried first: DSI TRIG_CTRL (+0x084) bit 31 (TE, public register naming: DMA_TRIGGER [2:0], MDP_TRIGGER [6:4],
 * STREAM [8], BLOCK_DMA_WITHIN_FRAME [12], TE [31]) is cleared for each in-window START and restored after the 200 us check. The DMA (DCS)
 * trigger select is the low 3 bits and the panel120 DMA sequences finish (and restore TRIG_CTRL) before this mode is set up, so they are not
 * touched. Not yet observed on device. */
static const char *const fg_scanout_mid_name[FG_SCAN_MID_NVAR] = {"thresh_hi16", "thresh_both", "start_pos", "tear_en", "dsi_te"};
static const unsigned char fg_scanout_mid_order[FG_SCAN_MID_NVAR] = {4, 0, 1, 2, 3};
static unsigned fg_scanout_mid_state, fg_scanout_mid_var, fg_scanout_mid_seq, fg_scanout_mid_old, fg_scanout_mid_new, fg_scanout_mid_pos, fg_scanout_mid_en, fg_scanout_mid_dirty;
static unsigned fg_scanout_mid_trig, fg_scanout_mid_wval, fg_scanout_mid_rval;
static unsigned fg_scanout_mid_vimm[FG_SCAN_MID_NVAR], fg_scanout_mid_vheld[FG_SCAN_MID_NVAR];
static unsigned fg_scanout_mid_wait, fg_scanout_mid_forced, fg_scanout_mid_hist[7], fg_scanout_mid_g1[7]; /* state: 0 off, 1 verify, 2 confirmed, 3 disabled */
static uint64_t fg_scanout_mid_block_t;
static unsigned fg_scanout_mid_since(unsigned count) { return ((count & 0xffffu) - fg_scanout_te_init) & 0xffffu; }
static unsigned fg_scanout_mid_bucket(unsigned since)
{
 return since <= 4 ? 0 : since < FG_SCAN_MID_LO ? 1 : since < 600 ? 2 : since < 1200 ? 3 : since < 1800 ? 4 : since <= FG_SCAN_MID_HI ? 5 : 6;
}
static void fg_scanout_mid_undirty(void)
{
 if (!fg_scanout_mid_dirty) return;
 if (fg_scanout_mid_var == 2) (void)fg_scanout_write(FG_SCAN_INTF+0x2a0, fg_scanout_mid_pos);
 if (fg_scanout_mid_var == 3) (void)fg_scanout_write(FG_SCAN_INTF+0x284, fg_scanout_mid_en);
 if (fg_scanout_mid_var == 4) (void)fg_scanout_write(FG_SCAN_DSI+0x084, fg_scanout_mid_trig);
 fg_scanout_mid_dirty = 0;
}
static int fg_scanout_mid_apply(unsigned v)
{
 unsigned want = fg_scanout_mid_old, back;
 if (v < 2) {
  want = v ? (FG_SCAN_MID_HI << 16) | FG_SCAN_MID_HI : (FG_SCAN_MID_HI << 16) | (fg_scanout_mid_old & 0xffffu);
  if (!fg_scanout_write(FG_SCAN_INTF+0x29c, want) || !fg_scanout_read(FG_SCAN_INTF+0x29c, &back) || back != want) {
   (void)fg_scanout_write(FG_SCAN_INTF+0x29c, fg_scanout_mid_old);
   printf("FUXI SCANOUT: mid_start variant %u (%s) threshold write refused old=%#x want=%#x; skipped\n", v, fg_scanout_mid_name[v], fg_scanout_mid_old, want);
   return 0;
  }
 } else if (v == 2) {
  if (!fg_scanout_read(FG_SCAN_INTF+0x2a0, &fg_scanout_mid_pos)) return 0;
 } else if (v == 4) {
  unsigned trig;
  if (!fg_scanout_read(FG_SCAN_DSI+0x084, &trig) || !(trig & 0x80000000u)) {
   printf("FUXI SCANOUT: mid_start variant %u (%s) skipped: DSI TRIG_CTRL=%#x has no TE bit\n", v, fg_scanout_mid_name[v], trig);
   return 0;
  }
  fg_scanout_mid_trig = trig;
 } else {
  unsigned c1 = 0, c2 = 0, ok;
  boolean_t interrupts = ml_set_interrupts_enabled(FALSE);
  ok = fg_scanout_read(FG_SCAN_INTF+0x284, &fg_scanout_mid_en) && fg_scanout_write(FG_SCAN_INTF+0x284, 0) &&
      fg_scanout_read(FG_SCAN_INTF+0x298, &c1);
  if (ok) { delay(100); ok = fg_scanout_read(FG_SCAN_INTF+0x298, &c2); }
  (void)fg_scanout_write(FG_SCAN_INTF+0x284, fg_scanout_mid_en);
  (void)ml_set_interrupts_enabled(interrupts);
  if (!ok || c1 == c2) {
   printf("FUXI SCANOUT: mid_start variant %u (%s) skipped: read counter %#x -> %#x with the tear check disabled\n", v, fg_scanout_mid_name[v], c1, c2);
   return 0;
  }
 }
 fg_scanout_mid_new = want;
 printf("FUXI SCANOUT: mid_start variant %u (%s) trying: thresh %#x -> %#x start_pos=%#x tear_en=%#x (START window %u..%u lines after TE)\n",
     v, fg_scanout_mid_name[v], fg_scanout_mid_old, want, fg_scanout_mid_pos, fg_scanout_mid_en, FG_SCAN_MID_LO, FG_SCAN_MID_HI);
 return 1;
}
static void fg_scanout_mid_from(unsigned seq)
{
 for (; seq < FG_SCAN_MID_NVAR; seq++) if (fg_scanout_mid_apply(fg_scanout_mid_order[seq])) {
  fg_scanout_mid_var = fg_scanout_mid_order[seq]; fg_scanout_mid_seq = seq; fg_scanout_mid_state = 1; return;
 }
 fg_scanout_mid_state = 3;
 printf("FUXI SCANOUT: mid_start no variant opens the gate; off, threshold %#x\n", fg_scanout_mid_old);
}
static void fg_scanout_mid_setup(void)
{
 unsigned old;
 if (!fg_abi_mid || fg_scanout_mid_state || !fg_scanout_panel120_active) return;
 if (!fg_scanout_read(FG_SCAN_INTF+0x29c, &old)) return;
 fg_scanout_mid_old = old; fg_scanout_mid_pos = 0; fg_scanout_mid_en = 0; fg_scanout_mid_dirty = 0;
 fg_scanout_mid_from(0);
}
/* 1 = a START may be issued now. */
static int fg_scanout_mid_allow(void)
{
 unsigned count, since;
 if (fg_scanout_mid_state != 1 && fg_scanout_mid_state != 2) return 1;
 if (!fg_scanout_read(FG_SCAN_INTF+0x298, &count)) return 1;
 since = fg_scanout_mid_since(count);
 if (since <= 4 || since >= 4000 || (since >= FG_SCAN_MID_LO && since <= FG_SCAN_MID_HI)) {
  fg_scanout_mid_block_t = 0;
  if (since >= FG_SCAN_MID_LO && since <= FG_SCAN_MID_HI && !fg_scanout_mid_dirty && fg_scanout_mid_var >= 2) {
   unsigned reg = fg_scanout_mid_var == 2 ? FG_SCAN_INTF+0x2a0 : fg_scanout_mid_var == 3 ? FG_SCAN_INTF+0x284 : FG_SCAN_DSI+0x084, val = 0;
   if (fg_scanout_mid_var == 2) val = (count + 8) & 0xffffu;
   if (fg_scanout_mid_var == 4) { if (!fg_scanout_read(reg, &val)) return 1; fg_scanout_mid_trig = val; val &= ~0x80000000u; }
   if (fg_scanout_write(reg, val)) {
    fg_scanout_mid_dirty = 1; fg_scanout_mid_wval = val;
    if (!fg_scanout_read(reg, &fg_scanout_mid_rval)) fg_scanout_mid_rval = 0xdead0000u;
   }
  }
  return 1;
 }
 uint64_t now = rsr("cntpct_el0");
 if (!fg_scanout_mid_block_t) fg_scanout_mid_block_t = now;
 else if (now - fg_scanout_mid_block_t > 2*rsr("cntfrq_el0")*fg_scanout_period_us/1000000) { fg_scanout_mid_forced++; fg_scanout_mid_block_t = 0; return 1; }
 fg_scanout_mid_wait++;
 return 0;
}
/* After a START: histogram of the read line, the verification of in-window STARTs, and the restore of START_POS / TEAR_CHECK_EN. */
static void fg_scanout_mid_account(unsigned count, unsigned gap1)
{
 unsigned since, b, v = fg_scanout_mid_var;
 if (!fg_abi_mid || !fg_scanout_panel120_active) return;
 since = fg_scanout_mid_since(count); b = fg_scanout_mid_bucket(since);
 fg_scanout_mid_hist[b]++; if (gap1) fg_scanout_mid_g1[b]++;
 if (fg_scanout_mid_state != 1 && !fg_scanout_mid_dirty) return;
 if (since < FG_SCAN_MID_LO || since > FG_SCAN_MID_HI) { fg_scanout_mid_undirty(); return; }
 uint64_t limit = rsr("cntpct_el0") + rsr("cntfrq_el0")*200/1000000;
 unsigned line, immediate = 0;
 while (fg_scanout_read(FG_SCAN_INTF+0x2b0, &line)) {
  line &= 0xffffu;
  if (line != 0 && line < 2400) { immediate = 1; break; }
  if (rsr("cntpct_el0") >= limit) break;
  delay(5);
 }
 if (fg_scanout_mid_dirty && !fg_scanout_mid_vimm[v] && !fg_scanout_mid_vheld[v])
  printf("FUXI SCANOUT: mid_start variant %u (%s) first trial: wrote %#x read back %#x, read line since TE %u, write line %s\n",
      v, fg_scanout_mid_name[v], fg_scanout_mid_wval, fg_scanout_mid_rval, since, immediate ? "active" : "idle");
 fg_scanout_mid_undirty();
 if (immediate) fg_scanout_mid_vimm[v]++; else fg_scanout_mid_vheld[v]++;
 if (fg_scanout_mid_state != 1) return;
 if (fg_scanout_mid_vheld[v] >= FG_SCAN_MID_TRIALS) {
  printf("FUXI SCANOUT: mid_start variant %u (%s) failed: hardware still waits for the TE (immediate=%u held=%u); registers restored\n",
      v, fg_scanout_mid_name[v], fg_scanout_mid_vimm[v], fg_scanout_mid_vheld[v]);
  (void)fg_scanout_write(FG_SCAN_INTF+0x29c, fg_scanout_mid_old);
  fg_scanout_mid_from(fg_scanout_mid_seq + 1);
 } else if (fg_scanout_mid_vimm[v] >= FG_SCAN_MID_TRIALS) {
  fg_scanout_mid_state = 2;
  printf("FUXI SCANOUT: mid_start variant %u (%s) confirmed: in-window STARTs begin writing at once (immediate=%u held=%u)\n",
      v, fg_scanout_mid_name[v], fg_scanout_mid_vimm[v], fg_scanout_mid_vheld[v]);
 }
}
static void fg_scanout_probe_kick(unsigned te_frame)
{
 if (!fg_abi_probe || fg_scanout_probe_busy || (fg_scanout_starts & 7)) return;
 if (!fg_scanout_probe_setup) {
  thread_call_setup_with_options(&fg_scanout_probe_call, fg_scanout_probe_run, NULL, THREAD_CALL_PRIORITY_KERNEL, THREAD_CALL_OPTIONS_ONCE);
  fg_scanout_probe_setup = 1;
 }
 fg_scanout_probe_busy = 1; fg_scanout_probe_te0 = te_frame;
 (void)thread_call_enter(&fg_scanout_probe_call);
}
static unsigned fg_scanout_probe_us(uint64_t ticks) { return (unsigned)(ticks ? (ticks - 1)*1000000/rsr("cntfrq_el0") : 0); }
static void fg_scanout_probe_account(void)
{
 if (!fg_scanout_probe_busy || !__atomic_load_n(&fg_scanout_probe_ready, __ATOMIC_ACQUIRE)) return;
 if (!fg_scanout_probe_res[3]) fg_scanout_probe_timeouts++;
 else if (fg_scanout_probe_n < FG_PROBE_MAX) {
  unsigned done = fg_scanout_probe_us(fg_scanout_probe_res[3]), edge = fg_scanout_probe_us(fg_scanout_probe_res[0]);
  unsigned bin = done < 6000 ? 0 : done >= 10000 ? 17 : 1 + (done - 6000)/250;
  fg_scanout_probe_hist[bin]++;
  fg_scanout_probe_done[fg_scanout_probe_n] = done;
  fg_scanout_probe_te2d[fg_scanout_probe_n] = fg_scanout_probe_res[0] && done >= edge ? done - edge : 0;
  fg_scanout_probe_edges += fg_scanout_probe_res[0] != 0;
  fg_scanout_probe_n++;
  for (unsigned i = 0; i < 4; i++) fg_scanout_probe_sum[i] += fg_scanout_probe_us(fg_scanout_probe_res[i]);
 }
 __atomic_store_n(&fg_scanout_probe_ready, 0, __ATOMIC_RELEASE);
 fg_scanout_probe_busy = 0;
}
static unsigned fg_scanout_probe_pct(unsigned *v, unsigned n, unsigned pct)
{
 for (unsigned i = 1; i < n; i++) { unsigned x = v[i], j = i; while (j && v[j-1] > x) { v[j] = v[j-1]; j--; } v[j] = x; }
 return n ? v[(n - 1)*pct/100] : 0;
}
static void fg_scanout_probe_report(void)
{
 if (!fg_abi_probe) return;
 unsigned n = fg_scanout_probe_n, d[FG_PROBE_MAX], t[FG_PROBE_MAX], dmax = 0, tmax = 0, tn = 0;
 uint64_t tsum = 0;
 for (unsigned i = 0; i < n; i++) {
  d[i] = fg_scanout_probe_done[i]; if (d[i] > dmax) dmax = d[i];
  if (fg_scanout_probe_te2d[i]) { t[tn++] = fg_scanout_probe_te2d[i]; tsum += fg_scanout_probe_te2d[i]; if (t[tn-1] > tmax) tmax = t[tn-1]; }
 }
 uint64_t div = n ? n : 1, tdiv = tn ? tn : 1, ediv = fg_scanout_probe_edges ? fg_scanout_probe_edges : 1;
 printf("FUXI SCANOUT: dsi_xfer n=%u timeouts=%u start_done_us avg=%llu p50=%u p90=%u max=%u clear_avg=%llu lineend_avg=%llu"
     " te_edge_avg=%llu te_edges=%u te_done_us avg=%llu p50=%u p90=%u max=%u hist<6/6.25../9.75/>=10ms=%u",
     n, fg_scanout_probe_timeouts, (unsigned long long)(fg_scanout_probe_sum[3]/div), fg_scanout_probe_pct(d, n, 50), fg_scanout_probe_pct(d, n, 90), dmax,
     (unsigned long long)(fg_scanout_probe_sum[1]/div), (unsigned long long)(fg_scanout_probe_sum[2]/div),
     (unsigned long long)(fg_scanout_probe_sum[0]/ediv), fg_scanout_probe_edges, (unsigned long long)(tsum/tdiv),
     fg_scanout_probe_pct(t, tn, 50), fg_scanout_probe_pct(t, tn, 90), tmax, fg_scanout_probe_hist[0]);
 for (unsigned i = 1; i < 18; i++) printf("/%u", fg_scanout_probe_hist[i]);
 printf("\n");
 fg_scanout_probe_n = fg_scanout_probe_timeouts = fg_scanout_probe_edges = 0;
 for (unsigned i = 0; i < 18; i++) fg_scanout_probe_hist[i] = 0;
 for (unsigned i = 0; i < 4; i++) fg_scanout_probe_sum[i] = 0;
}
/* Poll brackets the edge between the last old count and the first new count.
 * Two narrow, consecutive brackets establish a measured period. An IIR (1/8
 * period, 1/4 phase) rejects polling jitter without following the software
 * timer's phase. Never extrapolate an advancing counter through a stall.
 * Estimator writes belong to the caller. Only a double-mode MMIO failure
 * takes FG_GUARD to stop and restore A; healthy observation remains lock-free. */
int fuxi_gpu_scanout_te(struct fg_te_observer *te)
{
 unsigned count;
 if (!fuxi_gpu_scanout_owned() || !fg_scanout_read(FG_SCAN_INTF+0x298, &count)) {
  te->initialized = te->samples = 0;
  if (fg_scanout_double_active && fuxi_gpu_scanout_owned()) {
   FG_GUARD;
   (void)fg_scanout_stop("read TE observer");
  }
  return 0;
 }
 uint64_t now = mach_absolute_time();
 unsigned frame = count >> 16, nominal = fg_scanout_period_us;
 unsigned delta = (frame - te->frame) & 0xffffu;
 if (!te->initialized || te->nominal_us != nominal || now < te->poll ||
     (!delta && now - te->change > te->period + te->period/4)) {
  nanoseconds_to_absolutetime((uint64_t)nominal*1000, &te->period);
  te->nominal_us = nominal;
  te->frame = frame;
  te->poll = te->change = now;
  te->raw_edge = te->edge = 0;
  te->initialized = 1;
  te->samples = 0;
  return 0;
 }
 uint64_t previousPoll = te->poll;
 uint64_t bracket = now - te->poll;
 uint64_t raw = te->poll + bracket/2;
 te->poll = now;
 if (!delta) return te->samples >= 2;
 te->frame = frame;
 te->change = now;
 if (delta != 1 || bracket > te->period/4) {
  /* A slow copy can straddle TE. Retain an established phase only when
   * the counter confirms up to four edges and the prediction fits the
   * observation bracket (allow one polling interval of phase error).
   * Do not feed this uncertain sample back into the period filter. */
  uint64_t predicted = te->edge + delta*te->period;
  uint64_t tolerance = te->period/16;
  if (te->samples >= 2 && delta <= 4 &&
      predicted + tolerance >= previousPoll && predicted <= now + tolerance) {
   te->edge = predicted < now ? predicted : now;
   return 1;
  }
  /* Reset, implausible jump or ambiguous acquisition: reacquire. */
  te->samples = 0;
  te->raw_edge = 0;
  return 0;
 }
 if (te->samples) {
  unsigned frames = (frame - te->raw_frame) & 0xffffu;
  uint64_t measured = frames ? (raw - te->raw_edge)/frames : 0;
  if (frames > 8 || measured < te->period*3/4 || measured > te->period*5/4) {
   te->samples = 0;
  } else {
   int64_t error = (int64_t)measured - (int64_t)te->period;
   te->period += error/8;
  }
 }
 if (te->samples >= 2) {
  uint64_t predicted = te->edge + te->period;
  int64_t error = (int64_t)raw - (int64_t)predicted;
  te->edge = predicted + error/4;
  if (te->edge > now) te->edge = now;
 } else te->edge = raw;
 te->raw_edge = raw;
 te->raw_frame = frame;
 if (te->samples < 2) te->samples++;
 return te->samples >= 2;
}
/* Arm the per-START gates. Called with the current read pointer value. */
static void fg_scanout_arm(unsigned count)
{
 uint64_t now = rsr("cntpct_el0"), hz = rsr("cntfrq_el0");
 fg_scanout_te_frame = count >> 16;
 unsigned drain = fg_scanout_drain_us;
 /* panel120 default floor (10 ms) exceeds the 8.33 ms period: a START finishing its transfer just before a TE waited out the floor
  * and missed that TE. Bit 8192 lowers it to 3/4 period (the transfer-done and TE checks still gate every START). */
 if (fg_abi_start && fg_scanout_panel120_active && !fg_scanout_explicit_drain && !fg_scanout_te_mode && drain == FG_SCAN_DRAIN_US/2)
  drain = fg_scanout_period_us*3/4;
 fg_scanout_after = now + hz*drain/1000000;
 fg_scanout_timeout = fg_scanout_after + hz/10;
 fg_scanout_started = now; fg_scanout_xfer_open = 1;
}
/* Preserve other pending flush bits; VIG0 is CTL_FLUSH bit0. */
static int fg_scanout_address(unsigned pa)
{
 unsigned flush;
 if (!fg_scanout_read(FG_SCAN_CTL+0x18, &flush) ||
     !fg_scanout_write(0xae05014, pa) || !fg_scanout_write(FG_SCAN_CTL+0x18, flush | 1u)) return 0;
 __asm__ volatile("dsb sy" ::: "memory");
 return 1;
}
/* fuxigpuncimport bit 22 (direct-scanout stage 1), READ only, once at takeover: apps_smmu state for the display streams 0x1c00/0x1c02
 * (DT handoff SMR <0x1c00 0x2>), then VIG0 geometry, current address and CTL0 flush/START. Writes nothing. Unreadable registers print
 * ~0 (0xffffffff); cbar/cba2r are ~0 when S2CR is not a translating S2CR with a valid bank. SMMUv2 layout (no EXID): GR0 sCR0 0,
 * IDR0-2 0x20-0x28, SMR 0x800+4n (valid 31, mask 30:16, id 14:0), S2CR 0xc00+4n (type 17:16, CBNDX 7:0); GR1 one page up: CBAR 4n,
 * CBA2R 0x800+4n; bank n at 2^(NUMPAGENDXB+1)+n pages: SCTLR 0, TCR2 0x10, TTBR0 0x20, TCR 0x30, MAIR0 0x38, FSR 0x58. */
static int fg_scanout_smr_dpu(unsigned smr)
{
 unsigned id = smr & 0x7fffu, mask = (smr >> 16) & 0x7fffu;
 return (smr & 0x80000000u) && (!((0x1c00u ^ id) & ~mask) || !((0x1c02u ^ id) & ~mask));
}
static unsigned fg_scanout_smmu_rd(unsigned off, unsigned *faults)
{
 unsigned v;
 if (fg_dpu_smmu_read(off, &v)) return v;
 ++*faults;
 return ~0u;
}
static void fg_scanout_dpu_survey(void)
{
 unsigned faults = 0, hits = 0;
 if (!fg_dpu_smmu_map(1)) printf("FUXI SCANOUT: dpu smmu map failed\n");
 else {
  unsigned scr0 = fg_scanout_smmu_rd(0, &faults), id0 = fg_scanout_smmu_rd(0x20, &faults), id1 = fg_scanout_smmu_rd(0x24, &faults);
  unsigned id2 = fg_scanout_smmu_rd(0x28, &faults), good = !faults;
  unsigned smrs = good ? ((id0 & 0xffu) > 128 ? 128 : id0 & 0xffu) : 0, cbs = good ? id1 & 0xffu : 0, shift = id1 >> 31 ? 16 : 12;
  unsigned gr1 = 1u << shift, cb_base = (1u << (((id1 >> 28) & 7) + 1)) << shift;
  printf("FUXI SCANOUT: dpu smmu scr0=%#x idr0=%#x idr1=%#x idr2=%#x smrs=%u cbs=%u page=%#x cb_base=%#x\n",
      scr0, id0, id1, id2, smrs, cbs, gr1, cb_base);
  for (unsigned i = 0; i < smrs && hits < 4; i++) {
   unsigned before = faults, smr = fg_scanout_smmu_rd(0x800 + 4*i, &faults);
   if (faults != before) break;
   if (!fg_scanout_smr_dpu(smr)) continue;
   unsigned s2cr = fg_scanout_smmu_rd(0xc00 + 4*i, &faults), cb = s2cr & 0xffu;
   unsigned bank = s2cr != ~0u && !((s2cr >> 16) & 3) && cb < cbs;
   unsigned cbar = bank ? fg_scanout_smmu_rd(gr1 + 4*cb, &faults) : ~0u, cba2r = bank ? fg_scanout_smmu_rd(gr1 + 0x800 + 4*cb, &faults) : ~0u;
   hits++;
   printf("FUXI SCANOUT: dpu smmu smr%u=%#x s2cr=%#x cb=%u cbar=%#x cba2r=%#x\n", i, smr, s2cr, cb, cbar, cba2r);
   if (!bank) continue;
   unsigned c = cb_base + (cb << shift);
   unsigned sctlr = fg_scanout_smmu_rd(c, &faults), tcr2 = fg_scanout_smmu_rd(c + 0x10, &faults);
   unsigned lo = fg_scanout_smmu_rd(c + 0x20, &faults), hi = fg_scanout_smmu_rd(c + 0x24, &faults);
   unsigned tcr = fg_scanout_smmu_rd(c + 0x30, &faults), mair0 = fg_scanout_smmu_rd(c + 0x38, &faults), fsr = fg_scanout_smmu_rd(c + 0x58, &faults);
   printf("FUXI SCANOUT: dpu smmu cb%u sctlr=%#x ttbr0=%#llx tcr=%#x tcr2=%#x mair0=%#x fsr=%#x\n",
       cb, sctlr, (unsigned long long)hi << 32 | lo, tcr, tcr2, mair0, fsr);
  }
  (void)fg_dpu_smmu_map(0);
 }
 /* VIG0 SRC_SIZE, SRC_IMG_SIZE, SRC_XY, OUT_SIZE, OUT_XY, YSTRIDE0, FORMAT, UNPACK, SRC0, CURRENT_SRC0; CTL0 FLUSH, START. */
 static const unsigned reg[12] = {0xae05000, 0xae05004, 0xae05008, 0xae0500c, 0xae05010, 0xae05024, 0xae05030, 0xae05034,
     0xae05014, 0xae050a4, FG_SCAN_CTL+0x18, FG_SCAN_CTL+0x1c};
 unsigned v[12];
 for (unsigned i = 0; i < 12; i++) if (!fg_scanout_read(reg[i], &v[i])) v[i] = ~0u;
 printf("FUXI SCANOUT: dpu smmu vig0 size=%#x img=%#x xy=%#x out_size=%#x out_xy=%#x ystride0=%#x format=%#x unpack=%#x src0=%#x cur=%#x flush=%#x start=%#x\n",
     v[0], v[1], v[2], v[3], v[4], v[5], v[6], v[7], v[8], v[9], v[10], v[11]);
 printf("FUXI SCANOUT: dpu smmu done matches=%u faults=%u (read only)\n", hits, faults);
}
/* fuxigpuncimport bits 22+23 (value 12582912; direct-scanout stage 2), default off, once, after the inherited drain and the panel120
 * switch, before B/C exist: one test of a stage-1 table on the display's own apps_smmu bank. The table layout, enable, fault watch,
 * undo and bank TLB flush are adapted from ShengUfs NativeUfs.cpp (f2d6a4a) dpuEnable/dpuTlbFlush/native_ufs_dpu_map and the 4 KiB
 * page list from GopFramebuffer NativeLegacyDisplay.cpp (33e941a) directSlot. ShengUfs: SPDX BSD-2-Clause-Patent in the source, LICENSE
 * file BSD-2-Clause, Copyright (c) 2026, blu-sharky (LICENSE.ShengUfs). GopFramebuffer: BSD-2-Clause-Patent, Copyright (c) 2026,
 * ArmVirtPlatform contributors (LICENSE.GopFramebuffer). Fuxi changes: the bank is the one
 * the SMRs already route 0x1c00/0x1c02 to (S224: SMR3 -> S2CR translate -> bank 3, CBAR type 1 = stage 1 with stage 2 bypass, CBA2R VA64,
 * SCTLR 0xe0 = M clear, CFRE|CFIE|CFCFG); TCR2 PASize is IDR2.OAS (S224: 36 bits) instead of 48 and TCR2.SEP is UPSTREAM (7, as Linux
 * arm-smmu sets it; concluded from X229, not yet confirmed: SEP 0 sign-extends every address with bit 31 set, 0xb8000100 included, into the TTBR1 half that EPD1
 * disables: translation fault, FSYNR0 PLVL 0; sheng i564's level-0 fault was the same TCR2 5); the window's leaves start empty so a
 * stray IOVA fetch faults; translation is switched only while CTL START is clear and the write line idle, and is switched off only
 * after A (identity) is latched again. Never written: SMRs, S2CRs, CBAR/CBA2R, any global register or global TLB invalidate (apps_smmu
 * also serves touch, WLAN and UFS); bank writes are limited to the offsets in fg_dpu_cb_wr. Steps, each logged as "FUXI SCANOUT: dpu
 * stage2 ...": check idle and SRC0 = CURRENT_SRC0 = A; find the bank; allocate the table and a wired 1125x2436 test surface (stride 4608,
 * scattered pages); install TTBR0/TCR2/TCR/MAIR0, bank TLBIALL, SCTLR M|CFRE|CFIE; FG_DPU_STARTS STARTs on A through the identity blocks
 * with FSR checked after each; map the surface into the window (bank TLBIALL); point VIG0 at it with the logical-to-panel crop and START
 * once; hold 2 s watching FSR; restore A's geometry and START (still translated); restore the bank registers and compare them with the
 * saved values; bank TLBIALL. On a fault: A geometry, wait idle, bank restored (RESUME for a stalled transaction, FSR fault bits
 * cleared), one untranslated START on A. FG_GUARD is held throughout (about 3.2 s at 60 Hz). Not yet observed on device. */
#define FG_DPU_IOVA 0x10000000u
#define FG_DPU_WINDOW (32u << 20)
#define FG_DPU_L3 (FG_DPU_WINDOW >> 21)                  /* level-3 tables of the window */
#define FG_DPU_TABLE_BYTES ((1u + FG_DPU_L3) * 4096u)     /* page 0 serves levels 0-2 (sheng i564), then the level-3 tables */
#define FG_DPU_FAULTS 0xc00001feu                         /* FSR MULTI, SS, UUT..TF; bits 10:9 are FORMAT (S224 idle FSR 0x400) */
#define FG_DPU_ATTRS ((1ull << 10) | (1ull << 6))         /* AF, AP[1]; AttrIndx 0 (MAIR0 0x44 normal non-cacheable), not shareable */
#define FG_DPU_W 1125u
#define FG_DPU_H 2436u
#define FG_DPU_STRIDE 4608u
#define FG_DPU_X 22u                                      /* SweetFramebuffer fOffX = (1125-1080)/2 */
#define FG_DPU_Y 18u                                      /* fOffY = (2436-2400)/2 */
#define FG_DPU_BYTES ((FG_DPU_STRIDE*FG_DPU_H + 4095u) & ~4095u)
#define FG_DPU_STARTS 60u
#define FG_DPU_HOLD_US 2000000u
#define FG_DPU_SEP (7u << 15)                             /* TCR2.SEP UPSTREAM: no sign extension at bit 31 */
#define FG_DPU_TCR2_MASK (FG_DPU_SEP | 7u)
#define FG_DPU_XFER_US 100000u
static uint64_t fg_dpu_pages[FG_DPU_WINDOW/4096];
/* Test pattern: red outside the 1080x2400 crop, a 4 pixel white frame on its edge, eight vertical bars inside. */
static uint32_t fg_dpu_pattern(unsigned x, unsigned y)
{
 static const uint32_t bars[8] = {0xffff0000u, 0xff00ff00u, 0xff0000ffu, 0xffffff00u, 0xff00ffffu, 0xffff00ffu, 0xff808080u, 0xff000000u};
 if (x < FG_DPU_X || x >= FG_DPU_X + 1080u || y < FG_DPU_Y || y >= FG_DPU_Y + 2400u) return 0xffff0000u;
 x -= FG_DPU_X; y -= FG_DPU_Y;
 if (x < 4 || x >= 1076 || y < 4 || y >= 2396) return 0xffffffffu;
 return bars[x/135];
}
/* The only bank offsets ever written: SCTLR, RESUME, TCR2, TTBR0, TCR, MAIR0, FSR, TLBIALL, TLBSYNC. */
static int fg_dpu_cb_wr(unsigned cbo, unsigned off, unsigned value)
{
 static const unsigned ok[10] = {0x0, 0x8, 0x10, 0x20, 0x24, 0x30, 0x38, 0x58, 0x618, 0x7f0};
 unsigned allowed = 0;
 for (unsigned i = 0; i < 10; i++) allowed |= off == ok[i];
 return allowed && fg_dpu_smmu_write(cbo + off, value);
}
static int fg_dpu_tlbi(unsigned cbo)
{
 unsigned st = 1;
 if (!fg_dpu_cb_wr(cbo, 0x618, 0) || !fg_dpu_cb_wr(cbo, 0x7f0, 0)) return 0;
 __asm__ volatile("dsb sy" ::: "memory");
 for (unsigned i = 0; i < 1000; i++) {
  if (!fg_dpu_smmu_read(cbo + 0x7f4, &st)) return 0;
  if (!(st & 1)) return 1;
  delay(1);
 }
 return 0;
}
static int fg_dpu_idle(void)
{
 unsigned start, line;
 return fg_scanout_read(FG_SCAN_CTL+0x1c, &start) && fg_scanout_read(FG_SCAN_INTF+0x2b0, &line) && !(start & 1) &&
     (!(line & 65535) || (line & 65535) >= 2400);
}
/* Poll for START clear and the write line idle, up to FG_DPU_XFER_US after t0. */
static int fg_dpu_wait(uint64_t t0, unsigned *us)
{
 uint64_t hz = rsr("cntfrq_el0");
 for (;;) {
  uint64_t el = (rsr("cntpct_el0") - t0)*1000000/hz;
  *us = (unsigned)el;
  if (fg_dpu_idle()) return 1;
  if (el >= FG_DPU_XFER_US) return 0;
  delay(250);
 }
}
/* START and wait for its transfer, which begins at the next TE (at most a period later): the first poll is a period plus 2 ms after. */
static int fg_dpu_start(unsigned *us)
{
 uint64_t t0 = rsr("cntpct_el0");
 *us = 0;
 if (!fg_scanout_write(FG_SCAN_CTL+0x1c, 1)) return 0;
 __asm__ volatile("dsb sy" ::: "memory");
 delay(fg_scanout_period_us + 2000u);
 return fg_dpu_wait(t0, us);
}
static const unsigned fg_dpu_geo_reg[4] = {0xae05004, 0xae05008, 0xae05024, 0xae05014}; /* SRC_IMG_SIZE, SRC_XY, SRC_YSTRIDE0, SRC0_ADDR */
/* VIG0 geometry and source, then CTL0 FLUSH bit0 (VIG0) keeping other pending bits; latched at the next START. */
static int fg_dpu_geometry(const unsigned g[4])
{
 unsigned flush;
 for (unsigned i = 0; i < 4; i++) if (!fg_scanout_write(fg_dpu_geo_reg[i], g[i])) return 0;
 if (!fg_scanout_read(FG_SCAN_CTL+0x18, &flush) || !fg_scanout_write(FG_SCAN_CTL+0x18, flush | 1u)) return 0;
 __asm__ volatile("dsb sy" ::: "memory");
 return 1;
}
/* 0 = finished, skipped or undone (the caller re-arms the START gates), -1 = scanout stopped. */
/* A table entry the walk reads: descriptor d's next-level table at index i, 0 with *in = 0 when that table is not ours. */
static uint64_t fg_dpu_desc(volatile uint64_t *t, uint64_t tpa, uint64_t d, unsigned i, unsigned *in)
{
 uint64_t pa = (d & 0xfffffffff000ull) + 8ull*i;
 *in = (d & 3) == 3 && pa >= tpa && pa < tpa + FG_DPU_TABLE_BYTES;
 return *in ? t[(pa - tpa)/8] : 0;
}
/* Fault diagnosis: FSYNR0 fields (PLVL 1:0, WNR 4, PNU 5, IND 6, PTWF 10, AFR 11), the sign bit TCR2.SEP picks, and the entries an
 * L0-start and the T0SZ-32 L1-start walk read for FAR from the table in memory. */
static void fg_dpu_walk_log(volatile uint64_t *t, uint64_t tpa, uint64_t far, unsigned fsynr0, unsigned tcr2)
{
 static const unsigned sepbit[8] = {31, 35, 39, 41, 43, 47, 0, 0};
 unsigned sep = (tcr2 >> 15) & 7, in2 = 0, in3 = 0, i1 = (unsigned)(far >> 30) & 3, i2 = (unsigned)(far >> 21) & 511, i3 = (unsigned)(far >> 12) & 511;
 uint64_t l0 = t[(far >> 39) & 511], l1 = t[i1], l2 = (l1 & 3) == 3 ? fg_dpu_desc(t, tpa, l1, i2, &in2) : 0, l3;
 l3 = in2 && (l2 & 3) == 3 ? fg_dpu_desc(t, tpa, l2, i3, &in3) : 0;
 printf("FUXI SCANOUT: dpu stage2 walk far=%#llx plvl=%u wnr=%u pnu=%u ind=%u ptwf=%u afr=%u fsynr0_hi=%#x tcr2=%#x sep=%u%s%u sign=%u "
     "l0[%u]=%#llx l1[%u]=%#llx l2[%u]=%#llx%s l3[%u]=%#llx%s\n", (unsigned long long)far, fsynr0 & 3, (fsynr0 >> 4) & 1, (fsynr0 >> 5) & 1,
     (fsynr0 >> 6) & 1, (fsynr0 >> 10) & 1, (fsynr0 >> 11) & 1, fsynr0 >> 16, tcr2, sep, sep == 7 ? " upstream bit=" : " bit=",
     sep == 7 ? 0 : sepbit[sep], sep < 6 && (far >> sepbit[sep]) & 1, (unsigned)(far >> 39) & 511, (unsigned long long)l0, i1,
     (unsigned long long)l1, i2, (unsigned long long)l2, (l1 & 3) == 3 && !in2 ? "(not ours)" : "", i3, (unsigned long long)l3,
     in2 && (l2 & 3) == 3 && !in3 ? "(not ours)" : "");
}
static int fg_scanout_dpu_stage2_run(void)
{
 static const unsigned paw[6] = {32, 36, 40, 42, 44, 48};
 unsigned g[4], want[4], cur = ~0u, flush = ~0u, start = ~0u, line = ~0u, faults = 0, us = 0, cb = ~0u, smr_at = ~0u, s2cr = 0;
 unsigned id0 = 0, id1 = 0, id2 = 0, smrs = 0, cbs = 0, shift = 12, gr1 = 0, cb_base = 0, oas = 0, cbar = 0, cba2r = 0;
 unsigned sctlr = 0, tcr2 = 0, lo = 0, hi = 0, tcr = 0, mair0 = 0, fsr0 = 0, fsr = 0, cbo = 0, pages = FG_DPU_BYTES/4096, above = 0, runs = 0;
 unsigned ntcr2 = 0, ntcr = 32u | (1u << 23), nsctlr = 0, rb[8] = {0};
 unsigned translating = 0, moved = 0, n = 0, max_us = 0, sum_us = 0, latched = 0, exact = 0, held = 0;
 uintptr_t tv = 0, sv = 0;
 uint64_t tpa = 0, limit = 0;
 volatile uint64_t *l1 = 0, *l3 = 0;
 const char *why = 0, *step = "precheck";
 for (unsigned i = 0; i < 4; i++) if (!fg_scanout_read(fg_dpu_geo_reg[i], &g[i])) g[i] = ~0u;
 (void)fg_scanout_read(0xae050a4, &cur); (void)fg_scanout_read(FG_SCAN_CTL+0x18, &flush);
 (void)fg_scanout_read(FG_SCAN_CTL+0x1c, &start); (void)fg_scanout_read(FG_SCAN_INTF+0x2b0, &line);
 printf("FUXI SCANOUT: dpu stage2 begin img=%#x xy=%#x ystride0=%#x src0=%#x cur=%#x flush=%#x start=%#x line=%u\n",
     g[0], g[1], g[2], g[3], cur, flush, start, line & 65535);
 want[0] = (FG_DPU_H << 16) | FG_DPU_W; want[1] = (FG_DPU_Y << 16) | FG_DPU_X; want[2] = (g[2] & 0xffff0000u) | FG_DPU_STRIDE; want[3] = FG_DPU_IOVA;
 if (g[0] == ~0u || g[1] == ~0u || g[2] == ~0u || g[3] == ~0u || cur == ~0u || flush == ~0u || start == ~0u || line == ~0u) why = "MMIO read";
 else if ((start & 1) || (flush & 1) || ((line & 65535) && (line & 65535) < 2400)) why = "transfer or VIG0 flush pending";
 else if (g[3] != 0xb8000000u || cur != 0xb8000000u) why = "source is not A";
 else if (fg_busy) why = "GPU busy";
 else if (!fg_dpu_smmu_map(2)) why = "smmu map failed";
 if (why) goto skip;
 id0 = fg_scanout_smmu_rd(0x20, &faults); id1 = fg_scanout_smmu_rd(0x24, &faults); id2 = fg_scanout_smmu_rd(0x28, &faults);
 if (faults) { why = "IDR read"; goto skip; }
 smrs = (id0 & 0xffu) > 128 ? 128 : id0 & 0xffu; cbs = id1 & 0xffu; shift = id1 >> 31 ? 16 : 12;
 gr1 = 1u << shift; cb_base = (1u << (((id1 >> 28) & 7) + 1)) << shift;
 oas = (id2 >> 4) & 15; if (oas > 5) oas = 5;
 limit = 1ull << paw[oas];
 for (unsigned i = 0; i < smrs && !why; i++) {
  unsigned smr = fg_scanout_smmu_rd(0x800 + 4*i, &faults), s;
  if (faults) { why = "SMR read"; break; }
  if (!fg_scanout_smr_dpu(smr)) continue;
  s = fg_scanout_smmu_rd(0xc00 + 4*i, &faults);
  if (faults || ((s >> 16) & 3)) why = "display stream not translating";
  else if (cb != ~0u && (s & 0xffu) != cb) why = "display streams on two banks";
  else if (cb == ~0u) { cb = s & 0xffu; smr_at = i; s2cr = s; }
 }
 if (!why && cb == ~0u) why = "no display SMR";
 if (!why && (cb >= cbs || cb_base + ((cb + 1) << shift) > FG_DPU_SMMU_SIZE)) why = "bank index";
 if (why) goto skip;
 cbar = fg_scanout_smmu_rd(gr1 + 4*cb, &faults); cba2r = fg_scanout_smmu_rd(gr1 + 0x800 + 4*cb, &faults);
 cbo = cb_base + (cb << shift);
 sctlr = fg_scanout_smmu_rd(cbo, &faults); tcr2 = fg_scanout_smmu_rd(cbo + 0x10, &faults);
 lo = fg_scanout_smmu_rd(cbo + 0x20, &faults); hi = fg_scanout_smmu_rd(cbo + 0x24, &faults);
 tcr = fg_scanout_smmu_rd(cbo + 0x30, &faults); mair0 = fg_scanout_smmu_rd(cbo + 0x38, &faults); fsr0 = fg_scanout_smmu_rd(cbo + 0x58, &faults);
 printf("FUXI SCANOUT: dpu stage2 bank cb=%u smr%u s2cr=%#x cbar=%#x cba2r=%#x saved sctlr=%#x tcr2=%#x ttbr0=%#llx tcr=%#x mair0=%#x fsr=%#x oas_bits=%u\n",
     cb, smr_at, s2cr, cbar, cba2r, sctlr, tcr2, (unsigned long long)hi << 32 | lo, tcr, mair0, fsr0, paw[oas]);
 if (faults) why = "bank read";
 else if (((cbar >> 16) & 3) != 1) why = "CBAR is not stage 1 with stage 2 bypass";
 else if (!(cba2r & 1)) why = "bank is not AArch64";
 else if (sctlr & 1) why = "translation already on";
 else if (fsr0 & FG_DPU_FAULTS) why = "fault already recorded";
 if (why) goto skip;
 /* Memory before any SMMU write: a failure here changes nothing. */
 step = "memory";
 if (!fg_dpu_mem(FG_DPU_TABLE_BYTES, 1, &tv) || !fg_dpu_mem(FG_DPU_BYTES, 0, &sv)) { why = "allocation"; goto skip; }
 tpa = fg_dpu_page_pa(tv);
 for (unsigned i = 0; i < FG_DPU_TABLE_BYTES/4096u && !why; i++)
  if (!tpa || (tpa & 4095) || fg_dpu_page_pa(tv + i*4096u) != tpa + i*4096u || tpa + FG_DPU_TABLE_BYTES > limit) why = "table not contiguous or beyond OAS";
 for (unsigned i = 0; i < pages && !why; i++) {
  uint64_t pa = fg_dpu_page_pa(sv + (uintptr_t)i*4096u);
  if (!pa || (pa & 4095) || pa >= limit) why = "surface page beyond OAS";
  fg_dpu_pages[i] = pa;
  above += pa >= (1ull << 32);
  runs += !i || pa != fg_dpu_pages[i-1] + 4096u;
 }
 if (why) goto skip;
 for (unsigned y = 0; y < FG_DPU_H; y++)
  for (unsigned x = 0; x < FG_DPU_W; x++) ((uint32_t *)sv)[y*(FG_DPU_STRIDE/4) + x] = fg_dpu_pattern(x, y);
 /* One page for levels 0-2 (sheng i564: the walk was seen starting at level 0 with T0SZ 32): [0] points back at itself, [2] and [3] are the
  * 1 GiB identity blocks for 0x80000000..0xffffffff (A, B, C at 0xb8000000..), [IOVA>>21 + t] the window's level-3 tables (left empty). */
 l1 = (volatile uint64_t *)tv; l3 = l1 + 512;
 l1[0] = tpa | 3;
 l1[2] = 0x80000000ull | FG_DPU_ATTRS | 1;
 l1[3] = 0xc0000000ull | FG_DPU_ATTRS | 1;
 for (unsigned t = 0; t < FG_DPU_L3; t++) l1[(FG_DPU_IOVA >> 21) + t] = (tpa + (1 + t)*4096ull) | 3;
 fg_dpu_clean(tv, FG_DPU_TABLE_BYTES); fg_dpu_clean(sv, FG_DPU_BYTES);
 printf("FUXI SCANOUT: dpu stage2 memory tables=%#llx surface %ux%u stride=%u pages=%u above4g=%u runs=%u first=%#llx last=%#llx\n",
     (unsigned long long)tpa, FG_DPU_W, FG_DPU_H, FG_DPU_STRIDE, pages, above, runs, (unsigned long long)fg_dpu_pages[0],
     (unsigned long long)fg_dpu_pages[pages-1]);
 if (!fg_dpu_idle()) { why = "transfer pending"; goto skip; }
 /* Install: TTBR0, TCR2 (PASize = OAS, SEP UPSTREAM, other bits kept), TCR (T0SZ 32, 4 KiB granule, non-cacheable walks, EPD1), MAIR0 attribute 0. */
 step = "install";
 ntcr2 = (tcr2 & ~FG_DPU_TCR2_MASK) | FG_DPU_SEP | oas; nsctlr = (sctlr & ~0x80u) | 0x61u;
 if (!fg_dpu_cb_wr(cbo, 0x20, (unsigned)tpa) || !fg_dpu_cb_wr(cbo, 0x24, (unsigned)(tpa >> 32)) || !fg_dpu_cb_wr(cbo, 0x10, ntcr2) ||
     !fg_dpu_cb_wr(cbo, 0x30, ntcr) || !fg_dpu_cb_wr(cbo, 0x38, 0x44)) { why = "bank write"; goto undo; }
 rb[0] = fg_scanout_smmu_rd(cbo + 0x20, &faults); rb[1] = fg_scanout_smmu_rd(cbo + 0x24, &faults);
 rb[2] = fg_scanout_smmu_rd(cbo + 0x30, &faults); rb[3] = fg_scanout_smmu_rd(cbo + 0x10, &faults);
 if (faults || rb[0] != (unsigned)tpa || rb[1] != (unsigned)(tpa >> 32) || rb[2] != ntcr || (rb[3] & FG_DPU_TCR2_MASK) != (ntcr2 & FG_DPU_TCR2_MASK)) { why = "bank ignored the table"; goto undo; }
 if (!fg_dpu_tlbi(cbo)) { why = "bank TLB sync"; goto undo; }
 /* SCTLR: M, CFRE, CFIE; CFCFG cleared so a fault terminates the fetch instead of stalling the display. */
 if (!fg_dpu_cb_wr(cbo, 0x0, nsctlr) || fg_scanout_smmu_rd(cbo, &faults) != nsctlr) { why = "SCTLR"; goto undo; }
 translating = 1;
 __asm__ volatile("dsb sy" ::: "memory"); __asm__ volatile("isb" ::: "memory");
 printf("FUXI SCANOUT: dpu stage2 enabled sctlr=%#x->%#x ttbr0=%#llx tcr=%#x tcr2=%#x mair0=0x44 tlbi=bank\n",
     sctlr, nsctlr, (unsigned long long)tpa, ntcr, ntcr2);
 /* Identity: STARTs on A through the 1 GiB blocks. */
 step = "identity";
 /* What the bank holds right before the first translated START. */
 rb[0] = fg_scanout_smmu_rd(cbo, &faults); rb[1] = fg_scanout_smmu_rd(cbo + 0x20, &faults); rb[2] = fg_scanout_smmu_rd(cbo + 0x24, &faults);
 rb[3] = fg_scanout_smmu_rd(cbo + 0x30, &faults); rb[4] = fg_scanout_smmu_rd(cbo + 0x10, &faults); rb[5] = fg_scanout_smmu_rd(cbo + 0x38, &faults);
 rb[6] = fg_scanout_smmu_rd(cbo + 0x58, &faults);
 printf("FUXI SCANOUT: dpu stage2 live sctlr=%#x ttbr0=%#llx tcr=%#x tcr2=%#x sep=%u pasize=%u mair0=%#x fsr=%#x l1[0]=%#llx l1[2]=%#llx l1[3]=%#llx\n",
     rb[0], (unsigned long long)rb[2] << 32 | rb[1], rb[3], rb[4], (rb[4] >> 15) & 7, rb[4] & 7, rb[5], rb[6],
     (unsigned long long)l1[0], (unsigned long long)l1[2], (unsigned long long)l1[3]);
 if (faults) { why = "bank read"; goto undo; }
 for (n = 0; n < FG_DPU_STARTS; n++) {
  if (!fg_dpu_start(&us)) { why = "transfer timeout"; goto undo; }
  fsr = fg_scanout_smmu_rd(cbo + 0x58, &faults);
  if (faults || (fsr & FG_DPU_FAULTS)) { why = "fault"; goto undo; }
  sum_us += us; if (us > max_us) max_us = us;
 }
 if (!fg_scanout_read(0xae050a4, &cur) || cur != 0xb8000000u) { why = "identity CURRENT_SRC0"; goto undo; }
 printf("FUXI SCANOUT: dpu stage2 identity starts=%u fsr=%#x cur=%#x xfer_us avg=%u max=%u\n", n, fsr, cur, sum_us/n, max_us);
 /* Map the surface into the window while the display is idle and fetches A. */
 step = "map";
 for (unsigned i = 0; i < pages; i++) l3[i] = fg_dpu_pages[i] | FG_DPU_ATTRS | 3;
 fg_dpu_clean(tv + 4096u, FG_DPU_L3*4096u);
 if (!fg_dpu_tlbi(cbo)) { why = "bank TLB sync"; goto undo; }
 printf("FUXI SCANOUT: dpu stage2 mapped iova=%#x pages=%u window=%#x tlbi=bank\n", FG_DPU_IOVA, pages, FG_DPU_WINDOW);
 /* Flip: VIG0 crops the centred 1080x2400 of the 1125x2436 surface; SRC_SIZE, OUT_SIZE/OUT_XY, format and DSC are unchanged. */
 step = "flip"; moved = 1;
 if (!fg_dpu_geometry(want)) { why = "flip MMIO"; goto undo; }
 if (!fg_dpu_start(&us)) { why = "transfer timeout"; goto undo; }
 fsr = fg_scanout_smmu_rd(cbo + 0x58, &faults);
 (void)fg_scanout_read(0xae050a4, &cur);
 latched = cur == FG_DPU_IOVA;
 printf("FUXI SCANOUT: dpu stage2 flip src0=%#x img=%#x xy=%#x ystride0=%#x xfer_us=%u fsr=%#x cur=%#x latched=%u\n",
     want[3], want[0], want[1], want[2], us, fsr, cur, latched);
 if (faults || (fsr & FG_DPU_FAULTS)) { why = "fault"; goto undo; }
 /* Hold: the panel shows the translated frame from its own memory; no transfer runs. */
 step = "hold";
 for (held = 0; held < FG_DPU_HOLD_US; held += 50000u) {
  delay(50000);
  fsr = fg_scanout_smmu_rd(cbo + 0x58, &faults);
  if (faults || (fsr & FG_DPU_FAULTS)) { why = "fault"; goto undo; }
 }
 printf("FUXI SCANOUT: dpu stage2 hold_ms=%u fsr=%#x\n", held/1000, fsr);
 /* Back to A while still translated (identity), so A is latched before translation goes off. */
 step = "restore";
 if (!fg_dpu_geometry(g)) { why = "restore MMIO"; goto undo; }
 if (!fg_dpu_start(&us)) { why = "transfer timeout"; goto undo; }
 fsr = fg_scanout_smmu_rd(cbo + 0x58, &faults);
 if (!fg_scanout_read(0xae050a4, &cur) || cur != 0xb8000000u) { why = "restore CURRENT_SRC0"; goto undo; }
 if (faults || (fsr & FG_DPU_FAULTS)) { why = "fault"; goto undo; }
 moved = 0;
 printf("FUXI SCANOUT: dpu stage2 restored A img=%#x xy=%#x ystride0=%#x src0=%#x xfer_us=%u fsr=%#x cur=%#x\n", g[0], g[1], g[2], g[3], us, fsr, cur);
undo:
 if (why) {
  unsigned f = 0, far_lo = fg_scanout_smmu_rd(cbo + 0x60, &f), far_hi = fg_scanout_smmu_rd(cbo + 0x64, &f), fsynr0 = fg_scanout_smmu_rd(cbo + 0x68, &f);
  fsr = fg_scanout_smmu_rd(cbo + 0x58, &f);
  printf("FUXI SCANOUT: dpu stage2 fault step=%s reason=%s starts=%u fsr=%#x far=%#llx fsynr0=%#x\n", step, why, n, fsr,
      (unsigned long long)far_hi << 32 | far_lo, fsynr0);
  if (translating && !f) fg_dpu_walk_log(l1, tpa, (uint64_t)far_hi << 32 | far_lo, fsynr0, fg_scanout_smmu_rd(cbo + 0x10, &f));
  if (moved && !fg_dpu_geometry(g)) printf("FUXI SCANOUT: dpu stage2 undo: A geometry write failed\n");
  /* Translation goes off only with no transfer running (the latched source may still be the window). */
  if (!fg_dpu_wait(rsr("cntpct_el0"), &us)) {
   printf("FUXI SCANOUT: dpu stage2 undo incomplete: transfer not idle; translation kept on (identity + window), memory kept\n");
   (void)fg_dpu_smmu_map(0);
   return fg_scanout_stop("dpu stage2 transfer not idle");
  }
 }
 {
  /* SCTLR first (translation off), then the table registers, the fault record and the bank TLB. */
  unsigned f = 0, now = fg_scanout_smmu_rd(cbo + 0x58, &f), ok = fg_dpu_cb_wr(cbo, 0x0, sctlr), r[7];
  __asm__ volatile("dsb sy" ::: "memory"); __asm__ volatile("isb" ::: "memory");
  ok &= fg_dpu_cb_wr(cbo, 0x20, lo) & fg_dpu_cb_wr(cbo, 0x24, hi) & fg_dpu_cb_wr(cbo, 0x10, tcr2) & fg_dpu_cb_wr(cbo, 0x30, tcr) &
      fg_dpu_cb_wr(cbo, 0x38, mair0);
  if (!f && (now & (1u << 30))) ok &= fg_dpu_cb_wr(cbo, 0x8, 1);  /* RESUME: terminate a stalled transaction */
  if (!f && (now & FG_DPU_FAULTS)) ok &= fg_dpu_cb_wr(cbo, 0x58, now & FG_DPU_FAULTS); /* write one to clear */
  ok &= fg_dpu_tlbi(cbo);
  r[0] = fg_scanout_smmu_rd(cbo, &f); r[1] = fg_scanout_smmu_rd(cbo + 0x10, &f); r[2] = fg_scanout_smmu_rd(cbo + 0x20, &f);
  r[3] = fg_scanout_smmu_rd(cbo + 0x24, &f); r[4] = fg_scanout_smmu_rd(cbo + 0x30, &f); r[5] = fg_scanout_smmu_rd(cbo + 0x38, &f);
  r[6] = fg_scanout_smmu_rd(cbo + 0x58, &f);
  exact = ok && !f && r[0] == sctlr && r[1] == tcr2 && r[2] == lo && r[3] == hi && r[4] == tcr && r[5] == mair0 && r[6] == fsr0;
  printf("FUXI SCANOUT: dpu stage2 bank restored exact=%u sctlr=%#x tcr2=%#x ttbr0=%#llx tcr=%#x mair0=%#x fsr=%#x\n", exact, r[0], r[1],
      (unsigned long long)r[3] << 32 | r[2], r[4], r[5], r[6]);
  if (why && translating) {
   /* After a fault the display may hold the window address or a bad frame: one untranslated START on A. */
   unsigned ok2 = fg_dpu_start(&us);
   (void)fg_scanout_read(0xae050a4, &cur);
   printf("FUXI SCANOUT: dpu stage2 undone step=%s start=%u xfer_us=%u cur=%#x\n", step, ok2, us, cur);
   if (!ok2) { (void)fg_dpu_smmu_map(0); return fg_scanout_stop("dpu stage2 undo transfer"); }
  }
 }
 (void)fg_dpu_smmu_map(0);
 if (exact && cur == 0xb8000000u) { fg_dpu_mem_free(tv, FG_DPU_TABLE_BYTES); fg_dpu_mem_free(sv, FG_DPU_BYTES); }
 else printf("FUXI SCANOUT: dpu stage2 memory kept (bank not restored exactly or source not A)\n");
 printf("FUXI SCANOUT: dpu stage2 done result=%s step=%s identity_starts=%u flip_latched=%u hold_ms=%u exact=%u\n",
     why ? "fault" : "ok", step, n, latched, held/1000, exact);
 return 0;
skip:
 (void)fg_dpu_smmu_map(0);
 fg_dpu_mem_free(tv, FG_DPU_TABLE_BYTES); fg_dpu_mem_free(sv, FG_DPU_BYTES);
 printf("FUXI SCANOUT: dpu stage2 skipped step=%s reason=%s; nothing written\n", step, why);
 return 0;
}
/* DO NOT ENABLE (G186): leave bit 131072 off. Count 0 does not repeat the transfer and count 1 only every second TE (60 Hz), so this can
 * never reach 120 Hz; and after an autorefresh disable the CTL START bit was seen stuck (start=0x1, write line 0), so the drain timed out
 * ("transfer drain timeout") and the scanout stopped for the boot. Kept only for reference.
 * Opt-in (fuxigpuncimport bit 131072), panel120 + triple only: pingpong autorefresh scanout. The bootloader left INTF1 AUTOREFRESH_CONFIG
 * (+0x2b4) = 0x80000001 (bit31 enable, low16 frame count 1) and ownership clears bit31. After a normal START the monitor thread sets bit31
 * again (count 1): the hardware then repeats the transfer of the latched SSPP source on every TE with no software START. A present is
 * then only a source flip: SRC0 (0xae05014) + CTL flush bit0 (the pipe registers are latched by the hardware at a frame start; this is
 * not assumed but measured). SSPP_CURRENT_SRC0_ADDR (0xae050a4) shows the latched address: it tells which of A/B/C the hardware has
 * latched. The staged (front) buffer may be unlatched, the latched one (fetch) may be read at any time, and the buffer a latch displaced is
 * held until a TE after the observation, so a copy never targets front, fetch or a held buffer; a flip over an unlatched buffer simply
 * replaces it. Verification: within 100 ms the write line must be active in >=60% of the samples with no software START (a transfer that
 * runs on every TE is active ~95% of the time), else autorefresh is disabled and manual START resumes for the boot (state 3). Idle: 48 TEs
 * without a flip disable autorefresh (the staged source is already latched), the next present uses a START and re-arms it. CPU fallback and
 * stop disable it first. Counters: latch line (write line when the address change was first seen) shows whether the latch happens at a
 * frame start (idle/<200) or mid-transfer (>=600, "latch_mid", a tear risk). Not yet observed on device. */
#define FG_SCAN_AR_REG (FG_SCAN_INTF+0x2b4)
#define FG_SCAN_AR_VERIFY_US 100000u
#define FG_SCAN_AR_MIN_US 40000u
#define FG_SCAN_AR_IDLE_TES 48u
enum {FG_AR_FLIPS, FG_AR_ON, FG_AR_OFF_IDLE, FG_AR_OFF_FAIL, FG_AR_OFF_CPU, FG_AR_OFF_STOP, FG_AR_LATCH_MID, FG_AR_BAD_CUR, FG_AR_DROPS, FG_AR_WAKES,
      FG_AR_TARGET_WAIT, FG_AR_CONFIRMED, FG_AR_N};
static thread_call_data_t fg_scanout_ar_call;
static unsigned fg_scanout_ar_setup, fg_scanout_ar_busy, fg_scanout_ar_pending, fg_scanout_ar_fetch, fg_scanout_ar_hold;
static unsigned fg_scanout_ar_confirmed, fg_scanout_ar_flip_valid, fg_scanout_ar_samples, fg_scanout_ar_active, fg_scanout_ar_idx, fg_scanout_ar_prints;
static const unsigned fg_scanout_ar_counts[2] = {0, 1}; /* frame counts tried in order (F185: count 1 repeated every 2nd TE); the TE count (INTF+0x298 high16) does not advance while autorefresh runs, so all timing here is the clock */
static uint64_t fg_scanout_ar_flip_t, fg_scanout_ar_hold_t;
static unsigned fg_scanout_ar_st[FG_AR_N], fg_scanout_ar_tpf[5], fg_scanout_ar_ttl[4], fg_scanout_ar_ll[5];
static uint64_t fg_scanout_ar_t0;
static void fg_scanout_ar_run(thread_call_param_t a, thread_call_param_t b);
static void fg_scanout_ar_schedule(unsigned us)
{
 uint64_t d;
 if (fg_scanout_ar_busy) return;
 if (!fg_scanout_ar_setup) {
  thread_call_setup_with_options(&fg_scanout_ar_call, fg_scanout_ar_run, NULL, THREAD_CALL_PRIORITY_KERNEL, THREAD_CALL_OPTIONS_ONCE);
  fg_scanout_ar_setup = 1;
 }
 nanoseconds_to_absolutetime((uint64_t)us*1000, &d);
 fg_scanout_ar_busy = 1;
 (void)thread_call_enter_delayed(&fg_scanout_ar_call, mach_absolute_time() + d);
}
static int fg_scanout_ar_buffer(unsigned cur, unsigned *b)
{
 if (cur < 0xb8000000u || (cur - 0xb8000000u) % 0xa00000u || (cur - 0xb8000000u)/0xa00000u > 2) return 0;
 *b = (cur - 0xb8000000u)/0xa00000u;
 return 1;
}
/* Read the latched source, the TE count and (on a change) the write line. Called under FG_GUARD; 0 = MMIO failure. */
static int fg_scanout_ar_sync(void)
{
 unsigned cur, line = 0, b;
 uint64_t now = rsr("cntpct_el0"), period = fg_scanout_us_to_ticks(fg_scanout_period_us);
 if (!fg_scanout_read(0xae050a4, &cur)) return 0;
 if (!fg_scanout_ar_buffer(cur, &b)) fg_scanout_ar_st[FG_AR_BAD_CUR]++;
 else if (b != fg_scanout_ar_fetch) {
  unsigned bucket;
  (void)fg_scanout_read(FG_SCAN_INTF+0x2b0, &line);
  line &= 0xffffu;
  bucket = line == 0 || line >= 2400 ? 0 : line < 200 ? 1 : line < 600 ? 2 : line < 1200 ? 3 : 4;
  fg_scanout_ar_ll[bucket]++;
  if (bucket >= 3) fg_scanout_ar_st[FG_AR_LATCH_MID]++;
  if (b == fg_scanout_front) {
   if (fg_scanout_ar_pending && fg_scanout_ar_flip_valid) { uint64_t d = (now - fg_scanout_ar_flip_t + period/2)/period; fg_scanout_ar_ttl[d > 3 ? 3 : d]++; }
   fg_scanout_ar_pending = 0;
  }
  fg_scanout_ar_hold = 1u << fg_scanout_ar_fetch; fg_scanout_ar_hold_t = now;
  fg_scanout_ar_fetch = b;
 }
 if (fg_scanout_ar_hold && now - fg_scanout_ar_hold_t >= period) fg_scanout_ar_hold = 0;
 return 1;
}
/* A buffer that is neither the staged one, the latched one nor displaced since the last TE; 3 = none. */
static unsigned fg_scanout_ar_target(void)
{
 for (unsigned i = 0; i < 3; i++)
  if (i != fg_scanout_front && i != fg_scanout_ar_fetch && !(fg_scanout_ar_hold & (1u << i))) return i;
 return 3;
}
/* After a flip (count read right after the SRC0 write). */
static void fg_scanout_ar_flipped(void)
{
 uint64_t now = rsr("cntpct_el0"), period = fg_scanout_us_to_ticks(fg_scanout_period_us);
 if (fg_scanout_ar_flip_valid) { uint64_t d = (now - fg_scanout_ar_flip_t + period/2)/period; fg_scanout_ar_tpf[d > 4 ? 4 : d]++; }
 fg_scanout_ar_flip_t = now; fg_scanout_ar_flip_valid = 1;
 fg_scanout_ar_pending = fg_scanout_front != fg_scanout_ar_fetch;
 fg_scanout_ar_st[FG_AR_FLIPS]++;
 fg_scanout_ar_schedule(500);
}
/* Disable autorefresh; the staged source goes back to what the hardware has latched. newstate 0 = manual until the next present re-arms it, 3 = off for the boot. */
static int fg_scanout_ar_disable(unsigned newstate, unsigned why, const char *name)
{
 unsigned ar, count = 0;
 if (!fg_scanout_ar_on()) { if (fg_scanout_ar_state == 4) fg_scanout_ar_state = newstate; return 0; }
 if (!fg_scanout_read(FG_SCAN_AR_REG, &ar) || !fg_scanout_write(FG_SCAN_AR_REG, ar & ~0x80000000u)) { fg_scanout_ar_state = 3; return fg_scanout_stop("autorefresh disable"); }
 __asm__ volatile("dsb sy" ::: "memory");
 fg_scanout_ar_state = newstate;
 if (!fg_scanout_ar_sync()) return fg_scanout_stop("autorefresh sync");
 if (fg_scanout_front != fg_scanout_ar_fetch) {
  if (!fg_scanout_address(fg_scanout_pa(fg_scanout_ar_fetch))) return fg_scanout_stop("autorefresh restore source");
  fg_scanout_front = fg_scanout_ar_fetch; fg_scanout_ar_st[FG_AR_DROPS]++;
 }
 fg_scanout_ar_pending = 0; fg_scanout_ar_hold = 0; fg_scanout_back_ready = 0;
 if (!fg_scanout_read(FG_SCAN_INTF+0x298, &count)) return fg_scanout_stop("autorefresh read te");
 fg_scanout_arm(count);
 fg_scanout_ar_st[why]++;
 if (fg_scanout_ar_prints < 24) { fg_scanout_ar_prints++; printf("FUXI SCANOUT: ar off (%s) state=%u te=%u front=%u\n", name, newstate, count >> 16, fg_scanout_front); }
 return 0;
}
/* From the monitor thread: 1 = state changed (enabled or given up), 0 = try again later. */
static int fg_scanout_ar_enable(void)
{
 unsigned ar, want, back = 0, cur, b, start, count;
 if (fg_scanout_writable || fg_scanout_copy_open || fg_scanout_back_ready || fg_busy) return 0;
 if (!fg_scanout_read(FG_SCAN_CTL+0x1c, &start) || (start & 1)) return 0;
 if (!fg_scanout_read(FG_SCAN_AR_REG, &ar) || !fg_scanout_read(0xae050a4, &cur) || !fg_scanout_read(FG_SCAN_INTF+0x298, &count)) {
  fg_scanout_ar_state = 3; (void)fg_scanout_stop("autorefresh read"); return 1;
 }
 if (!fg_scanout_ar_buffer(cur, &b)) {
  fg_scanout_ar_st[FG_AR_BAD_CUR]++; fg_scanout_ar_state = 3;
  printf("FUXI SCANOUT: ar not enabled: latched source %#x is not A/B/C; manual START for the boot\n", cur);
  return 1;
 }
 want = (ar & ~0xffffu) | 0x80000000u | fg_scanout_ar_counts[fg_scanout_ar_idx];
 if (!fg_scanout_write(FG_SCAN_AR_REG, want) || !fg_scanout_read(FG_SCAN_AR_REG, &back) || back != want) {
  (void)fg_scanout_write(FG_SCAN_AR_REG, ar & ~0x80000000u); fg_scanout_ar_state = 3;
  printf("FUXI SCANOUT: ar not enabled: write %#x read back %#x; manual START for the boot\n", want, back);
  return 1;
 }
 fg_scanout_ar_fetch = b; fg_scanout_ar_hold = 0; fg_scanout_ar_pending = fg_scanout_front != b;
 fg_scanout_ar_state = fg_scanout_ar_confirmed ? 2 : 1;
 fg_scanout_ar_t0 = fg_scanout_ar_flip_t = rsr("cntpct_el0"); fg_scanout_ar_samples = fg_scanout_ar_active = 0;
 fg_scanout_ar_flip_valid = 1;
 fg_scanout_ar_st[FG_AR_ON]++;
 if (fg_scanout_ar_prints < 24) { fg_scanout_ar_prints++; printf("FUXI SCANOUT: ar on state=%u count=%u ar=%#x -> %#x te=%u front=%u latched=%u\n", fg_scanout_ar_state, fg_scanout_ar_counts[fg_scanout_ar_idx], ar, want, count >> 16, fg_scanout_front, b); }
 return 1;
}
static void fg_scanout_ar_run(thread_call_param_t a, thread_call_param_t b)
{
 (void)a; (void)b;
 FG_GUARD;
 unsigned us = 4000, line;
 fg_scanout_ar_busy = 0;
 if (fg_scanout_failed || !fg_abi_ar) return;
 fg_scanout_ar_st[FG_AR_WAKES]++;
 if (fg_scanout_ar_state == 4) {
  if (!fg_scanout_ar_enable()) fg_scanout_ar_schedule(1000);
  else if (fg_scanout_ar_on()) fg_scanout_ar_schedule(1000);
  return;
 }
 if (!fg_scanout_ar_on()) return;
 if (!fg_scanout_ar_sync() || !fg_scanout_read(FG_SCAN_INTF+0x2b0, &line)) {
  (void)fg_scanout_stop("autorefresh monitor MMIO"); return;
 }
 line &= 0xffffu;
 uint64_t now = rsr("cntpct_el0"), hz = rsr("cntfrq_el0"), elapsed = (now - fg_scanout_ar_t0)*1000000/hz;
 if (fg_scanout_ar_state == 1) {
  fg_scanout_ar_samples++;
  if (line != 0 && line < 2400) fg_scanout_ar_active++;
  /* Duty = share of samples with the write line active. One transfer per TE is 7.92/8.28 = 95.6%, one per N+1 TEs a 1/(N+1) of that. */
  unsigned cnt = fg_scanout_ar_counts[fg_scanout_ar_idx], expect = 956/(cnt + 1), duty = fg_scanout_ar_samples ? fg_scanout_ar_active*1000/fg_scanout_ar_samples : 0;
  if (elapsed >= FG_SCAN_AR_MIN_US && fg_scanout_ar_samples >= 20 && duty*10 >= expect*8) {
   fg_scanout_ar_state = 2; fg_scanout_ar_confirmed = 1; fg_scanout_ar_st[FG_AR_CONFIRMED]++;
   printf("FUXI SCANOUT: ar count=%u duty %u.%u%% (%u of %u samples, %llu us) expected %u.%u%%: confirmed%s\n", cnt, duty/10, duty%10,
       fg_scanout_ar_active, fg_scanout_ar_samples, (unsigned long long)elapsed, expect/10, expect%10, cnt ? " (every second TE: about 60 Hz refresh)" : "");
  } else if (elapsed >= FG_SCAN_AR_VERIFY_US) {
   printf("FUXI SCANOUT: ar count=%u duty %u.%u%% (%u of %u samples, %llu us) expected %u.%u%%: too low%s\n", cnt, duty/10, duty%10,
       fg_scanout_ar_active, fg_scanout_ar_samples, (unsigned long long)elapsed, expect/10, expect%10,
       fg_scanout_ar_idx + 1 < 2 ? "; trying the next count" : "; manual START for the boot");
   if (fg_scanout_ar_idx + 1 < 2) {
    fg_scanout_ar_idx++;
    (void)fg_scanout_ar_disable(4, FG_AR_OFF_FAIL, "count rejected");
    if (fg_scanout_ar_state == 4) fg_scanout_ar_schedule(1000);
   } else (void)fg_scanout_ar_disable(3, FG_AR_OFF_FAIL, "verify failed");
   return;
  }
  us = 1000;
 } else if (!fg_scanout_ar_pending && !fg_scanout_writable && !fg_scanout_copy_open && now - fg_scanout_ar_flip_t >= FG_SCAN_AR_IDLE_TES*fg_scanout_us_to_ticks(fg_scanout_period_us)) {
  (void)fg_scanout_ar_disable(0, FG_AR_OFF_IDLE, "idle");
  return;
 }
 if (fg_scanout_ar_pending) us = 500;
 fg_scanout_ar_schedule(us);
}
/* After a manual START: ask the monitor to enable autorefresh once the transfer trigger has settled. */
static void fg_scanout_ar_arming(void)
{
 if (!fg_abi_ar || fg_scanout_ar_state != 0 || fg_scanout_failed || !fg_scanout_panel120_active || !fg_scanout_double_active || !fg_scanout_triple_requested) return;
 fg_scanout_ar_state = 4;
 fg_scanout_ar_schedule(1000);
}
static int fg_scanout_stop(const char *reason)
{
 if (fg_scanout_ar_on() || fg_scanout_ar_state == 4) {
  unsigned ar;
  fg_scanout_ar_state = 3;
  if (fg_scanout_read(FG_SCAN_AR_REG, &ar)) (void)fg_scanout_write(FG_SCAN_AR_REG, ar & ~0x80000000u);
  fg_scanout_ar_st[FG_AR_OFF_STOP]++;
 }
 if (!fg_scanout_failed)
  printf("FUXI SCANOUT: stopped reason=%s; panel held, no framebuffer writes\n", reason);
 /* Restore the fixed-address shadow even on failure; never hand back ownership
  * or START if restoration/transfer/DMA state is uncertain. */
 if (!fg_scanout_failed && (fg_scanout_double_initialized || (fg_scanout_double_requested && fuxi_gpu_scanout_owned())) &&
     !fg_scanout_address(0xb8000000u))
  printf("FUXI SCANOUT: restore A failed; panel held, reboot required\n");
 fg_scanout_back_ready = 0;
 fg_scanout_failed = 1;
 fg_scanout_writable = 0;
 return -1;
}
/* Original register-mode implementation from hardware facts described by
 * Qualcomm dsi_ctrl_hw_cmn.c (kickoff_fifo_command, reset_cmd_fifo),
 * dsi_ctrl_reg.h, dsi_ctrl_hw_2_2.c (DMA trigger selection), and
 * sde_hw_intf.c (tear-check setup). No implementation text is imported.
 * DSI0 offsets: STATUS 008 (DMA busy bit1, MDP busy bit2), DMA_CTRL 03c,
 * LENGTH 04c, TRIG_CTRL 084 (DMA low nibble, MDP bits6:4), DMA_SW_TRIGGER
 * 090, INT_CTRL 110 (DMA_DONE W1C bit0; enables at odd bits), TPG_CTRL 15c,
 * FIFO_DATA 17c, FIFO_RESET 1ec. 094 is MDP_SW_TRIGGER: never use it for DCS.
 */
static int fg_panel120_poll(unsigned offset, unsigned mask, unsigned want, const char *phase)
{
 uint64_t deadline = rsr("cntpct_el0") + rsr("cntfrq_el0")*FG_SCAN_DSI_TIMEOUT_US/1000000;
 unsigned status = 0;
 /* Both a wall-clock deadline and a finite sample count, even if the clock stalls. */
 for (unsigned n = 0; n <= FG_SCAN_DSI_TIMEOUT_US/10; n++) {
  if (!fg_scanout_read(FG_SCAN_DSI+offset, &status)) {
   printf("FUXI SCANOUT: panel120 MMIO phase=%s offset=%#x\n", phase, offset);
   return 0;
  }
  if ((status & mask) == want) return 1;
  if (rsr("cntpct_el0") >= deadline) break;
  delay(10);
 }
 printf("FUXI SCANOUT: panel120 timeout phase=%s offset=%#x status=%#x\n", phase, offset, status);
 return 0;
}
/* One embedded FIFO batch, VC0, HS, no BTA, no per-packet delays. Long
 * header: length, 0, data type, flags; short: DCS, parameter, type, flags.
 * Flag 6 marks a long packet, flag 7 ends the batch; payload pads with ff.
 * DTB timing@1 timing-switch-command, unlike on-command, has no 05 packets.
 * DT descriptors have BATCH_COMMAND (flags 40) on the first seven packets,
 * flags 00 on the last: one 52-byte batch, not eight separate triggers.
 */
static unsigned fg_panel120_packet_words(uint32_t words[14])
{
 static const struct { uint8_t type, length, data[4]; } packets[] = {
  {0x39, 3, {0xf0, 0x5a, 0x5a}},
  {0x15, 2, {0x60, 0x00}},
  {0x39, 4, {0xb0, 0x00, 0x30, 0xbd}},
  {0x15, 2, {0xbd, 0x04}},
  {0x39, 4, {0xb0, 0x00, 0x16, 0xf2}},
  {0x39, 3, {0xf2, 0x01, 0x70}},
  {0x15, 2, {0xf7, 0x0f}},
  {0x39, 3, {0xf0, 0xa5, 0xa5}},
 };
 unsigned used = 0;
 for (unsigned i = 0; i < sizeof(packets)/sizeof(packets[0]); i++) {
  unsigned flags = i == sizeof(packets)/sizeof(packets[0])-1 ? 0x80u : 0;
  if (packets[i].type == 0x39) {
   words[used++] = packets[i].length | ((unsigned)packets[i].type << 16) | ((flags | 0x40u) << 24);
   uint32_t payload = 0xffffffffu;
   for (unsigned j = 0; j < packets[i].length; j++)
    payload = (payload & ~(0xffu << (8*j))) | ((unsigned)packets[i].data[j] << (8*j));
   words[used++] = payload;
  } else {
   words[used++] = packets[i].data[0] | ((unsigned)packets[i].data[1] << 8) |
       ((unsigned)packets[i].type << 16) | (flags << 24);
  }
 }
 /* Hardware fetches pairs of DWORDs; this word is outside DMA_CMD_LENGTH. */
 words[used] = 0;
 return used;
}
static int fg_panel120_tear(unsigned vsync, unsigned height, unsigned adjusted)
{
 /* SDE requires the single-buffer update within 2^16 vsync-clock cycles.
  * Mask interrupts only for these four MMIO writes, never for DSI polling. */
 boolean_t interrupts = ml_set_interrupts_enabled(FALSE);
 int good = fg_scanout_write(FG_SCAN_INTF+0x288, vsync & ~(1u << 19));
 __asm__ volatile("dsb sy" ::: "memory");
 if (good) good = fg_scanout_write(FG_SCAN_INTF+0x288, adjusted) &&
     fg_scanout_write(FG_SCAN_INTF+0x28c, height);
 __asm__ volatile("dsb sy" ::: "memory");
 if (good) good = fg_scanout_write(FG_SCAN_INTF+0x288, adjusted | (1u << 19));
 __asm__ volatile("dsb sy" ::: "memory");
 (void)ml_set_interrupts_enabled(interrupts);
 return good;
}
/* Return 0 for a refusal before transmission (keep 60 Hz), 1 on success,
 * -1 on timeout/MMIO/uncertain state (hold until reboot).
 * Called under FG_GUARD, with AR off, CTL START idle and write-line drained.
 */
static int fg_panel120_switch(void)
{
 unsigned dma, trigger, irq, tpg, length, ctrl, vsync, height, init, enable;
 fg_scanout_panel120_attempted = 1; /* Never retry a partial sequence. */
 if (!fg_panel120_poll(0x008, 0x1f, 0, "idle before switch")) {
  printf("FUXI SCANOUT: panel120 aborted before commands; keeping 60 Hz\n");
  return fg_scanout_stop("panel120 controller not idle");
 }
 if (!fg_scanout_read(FG_SCAN_DSI+0x004, &ctrl) ||
     !fg_scanout_read(FG_SCAN_DSI+0x03c, &dma) ||
     !fg_scanout_read(FG_SCAN_DSI+0x084, &trigger) ||
     !fg_scanout_read(FG_SCAN_DSI+0x110, &irq) ||
     !fg_scanout_read(FG_SCAN_DSI+0x15c, &tpg) ||
     !fg_scanout_read(FG_SCAN_DSI+0x04c, &length) ||
     !fg_scanout_read(FG_SCAN_INTF+0x288, &vsync) ||
     !fg_scanout_read(FG_SCAN_INTF+0x28c, &height) ||
     !fg_scanout_read(FG_SCAN_INTF+0x294, &init) ||
     !fg_scanout_read(FG_SCAN_INTF+0x284, &enable)) {
  printf("FUXI SCANOUT: panel120 aborted reading setup; keeping 60 Hz\n");
  return fg_scanout_stop("panel120 setup MMIO");
 }
 /* Require the inherited command engine, enabled tear counter, and idle TPG.
  * Do not enable/reset a controller or guess at an unrelated panel setup. */
 if ((ctrl & 7) != 5 || tpg || (irq & (1u << 24)) || !(vsync & (1u << 19)) ||
     (vsync & 0x7ffffu) < 2 || !enable) {
  printf("FUXI SCANOUT: panel120 refused ctrl=%#x tpg=%#x irq=%#x vsync=%#x en=%#x; keeping 60 Hz\n",
      ctrl, tpg, irq, vsync, enable);
  return 0;
 }
 uint32_t words[14];
 unsigned used = fg_panel120_packet_words(words);
 unsigned masks = irq & 0xaaaaaaaau;
 printf("FUXI SCANOUT: panel120 begin dma=%#x trigger=%#x irq=%#x vsync=%#x height=%#x init=%#x\n",
     dma, trigger, irq, vsync, height, init);
 /* Select software DMA trigger 4, preserving the MDP/TE trigger fields.
  * Clear DMA_TRG_MUX bit16 and BLOCK_DMA_WITHIN_FRAME bit12 while idle. */
 if (!fg_scanout_write(FG_SCAN_DSI+0x084, (trigger & ~0x1100fu) | 4) ||
     !fg_scanout_write(FG_SCAN_DSI+0x110, (masks & ~2u) | 1u) ||
     !fg_panel120_poll(0x110, 1, 0, "clear stale done") ||
     !fg_scanout_write(FG_SCAN_DSI+0x1ec, 1)) goto uncertain;
 __asm__ volatile("dsb sy" ::: "memory");
 delay(1);
 if (!fg_scanout_write(FG_SCAN_DSI+0x1ec, 0) ||
     !fg_scanout_write(FG_SCAN_DSI+0x15c, 0x00030006)) goto uncertain;
 for (unsigned i = 0; i < used + (used & 1); i++)
  if (!fg_scanout_write(FG_SCAN_DSI+0x17c, words[i])) goto uncertain;
 /* Embedded register FIFO, unicast HS: broadcast/master/LP all clear. */
 if (!fg_scanout_write(FG_SCAN_DSI+0x03c, (dma & ~0xc4000000u) | 0x10000000u) ||
     !fg_scanout_write(FG_SCAN_DSI+0x04c, used*4)) goto uncertain;
 __asm__ volatile("dsb sy" ::: "memory");
 if (!fg_scanout_write(FG_SCAN_DSI+0x090, 1) ||
     !fg_panel120_poll(0x110, 1, 1, "DMA done") ||
     !fg_panel120_poll(0x008, 0x1f, 0, "idle after commands")) goto uncertain;
 unsigned done;
 if (!fg_scanout_read(FG_SCAN_DSI+0x110, &done) || (done & (1u << 24))) goto uncertain;
 /* Completion is certain: reset only the command FIFO and restore ABL's
  * command settings before any MDP START. No MDP/frame trigger is issued. */
 if (!fg_scanout_write(FG_SCAN_DSI+0x110, (masks & ~2u) | 1u) ||
     !fg_scanout_write(FG_SCAN_DSI+0x15c, 0) ||
     !fg_scanout_write(FG_SCAN_DSI+0x1ec, 1)) goto uncertain;
 __asm__ volatile("dsb sy" ::: "memory");
 delay(1);
 if (!fg_scanout_write(FG_SCAN_DSI+0x1ec, 0) ||
     !fg_scanout_write(FG_SCAN_DSI+0x03c, dma) ||
     !fg_scanout_write(FG_SCAN_DSI+0x04c, length) ||
     !fg_scanout_write(FG_SCAN_DSI+0x084, trigger) ||
     !fg_scanout_write(FG_SCAN_DSI+0x110, masks)) goto uncertain;
 /* Same vsync clock and line geometry: count120=floor(count60*60/120).
  * Observed 0x180085: count=133 -> 66, resulting config 0x180042.
  * Bit19 is VSYNC_COUNTER_EN, bit20 external TE; preserve other flags.
  * SDE sequence: stop the counter before writing single-buffer registers. */
 unsigned disabled = vsync & ~(1u << 19);
 unsigned adjusted = (disabled & ~0x7ffffu) | ((vsync & 0x7ffffu)/2);
 if (!fg_panel120_tear(vsync, height, adjusted)) goto uncertain;
 unsigned after, height_after, init_after;
 if (!fg_scanout_read(FG_SCAN_INTF+0x288, &after) || after != (adjusted | (1u << 19)) ||
     !fg_scanout_read(FG_SCAN_INTF+0x28c, &height_after) || height_after != height ||
     !fg_scanout_read(FG_SCAN_INTF+0x294, &init_after) || init_after != init) goto uncertain;
 fg_scanout_panel120_active = 1;
 fg_scanout_period_us = 8333;
 if (!fg_scanout_explicit_drain && !fg_scanout_te_mode) fg_scanout_drain_us = FG_SCAN_DRAIN_US/2;
 fg_scanout_te_init = init & 0xffff;
 printf("FUXI SCANOUT: panel120 switched vsync=%#x->%#x height=%#x->%#x init=%#x->%#x period_us=%u drain_us=%u\n",
     vsync, after, height, height_after, init, init_after, fg_scanout_period_us, fg_scanout_drain_us);
 return 1;
uncertain:
 /* A timed-out DMA may have sent 60 00 already. Keep 60 Hz accounting,
  * never claim success or send rollback/START into an uncertain transfer. */
 printf("FUXI SCANOUT: panel120 aborted; panel state uncertain, reboot required\n");
 return fg_scanout_stop("panel120 switch incomplete");
}
/* 1 writable, 0 wait, -1 held after error. Normal gates do not spin or sleep;
 * the one-time panel120 switch uses bounded polling before granting writes.
 * First
 * disable AR, then allow a complete 60 Hz interval plus margin before testing
 * the write-line and CTL idle indicators. The same guard follows every START.
 * This deliberately conservative policy is not a TE/vblank interrupt driver;
 * fuxiscante=1 polls the TE frame count instead of the fixed floor.
 */
static int fg_scanout_acquire(unsigned publication)
{
 if (!fg_scanout_requested) return 1;
 if (fg_scanout_failed) return -1;
 if (!fuxi_gpu_scanout_owned()) {
  if (!fuxi_gpu_ready()) return 1; /* startup/unsupported GPU keeps old path */
  unsigned ar;
  if (!fg_scanout_read(FG_SCAN_INTF+0x2b4, &ar)) return fg_scanout_stop("read autorefresh");
  /* Block all generic START callers before changing display ownership. */
  __atomic_store_n(&fg_scanout_owned, 1, __ATOMIC_RELEASE);
  if (!fg_scanout_write(FG_SCAN_INTF+0x2b4, ar & ~0x80000000u))
   return fg_scanout_stop("disable autorefresh");
  __asm__ volatile("dsb sy" ::: "memory");
  if (fg_scanout_double_requested) {
   unsigned src;
   if (!fg_scanout_read(0xae05014, &src) || src != 0xb8000000u)
    return fg_scanout_stop("double initial SRC0");
  }
  unsigned rd = 0, wr = 0, st = 0, tc[8] = {0};
  int status = fg_scanout_read(FG_SCAN_INTF+0x298, &rd) &&
      fg_scanout_read(FG_SCAN_INTF+0x2b0, &wr) && fg_scanout_read(FG_SCAN_CTL+0x1c, &st);
  if (!status && fg_scanout_double_requested) return fg_scanout_stop("double inherited status");
  static const unsigned tear[8] = {0x280, 0x284, 0x288, 0x28c, 0x294, 0x29c, 0x2a0, 0x2a8};
  for (unsigned i = 0; i < 8; i++) {
   int good = fg_scanout_read(FG_SCAN_INTF+tear[i], &tc[i]);
   if (!good && fg_scanout_double_requested) return fg_scanout_stop("double tear setup");
  }
  fg_scanout_te_init = tc[4] & 0xffff;
  fg_scanout_arm(rd);
  fg_scanout_window_start = rsr("cntpct_el0");
  fg_scanout_te_window = rd >> 16;
  printf("FUXI SCANOUT: owned, autorefresh disabled; draining inherited transfer ar=%#x rd=%#x wr=%#x start=%#x\n",
      ar, rd, wr, st);
  printf("FUXI SCANOUT: tear sel=%#x en=%#x vsync=%#x height=%#x init=%#x thresh=%#x start_pos=%#x wr_irq=%#x\n",
      tc[0], tc[1], tc[2], tc[3], tc[4], tc[5], tc[6], tc[7]);
  if (fg_scanout_preclean) {
   /* Zero-copy prerequisite, read only: VIG0 SRC_SIZE, SRC_IMG_SIZE, SRC_XY, OUT_SIZE, OUT_XY,
    * SRC0_ADDR, SRC_YSTRIDE0, SRC_FORMAT, SRC_UNPACK_PATTERN, SRC_OP_MODE (sde_hw_sspp.c offsets). */
   static const unsigned vig[10] = {0x00, 0x04, 0x08, 0x0c, 0x10, 0x14, 0x24, 0x30, 0x34, 0x38};
   unsigned sv[10] = {0};
   for (unsigned i = 0; i < 10; i++) if (!fg_scanout_read(0xae05000u+vig[i], &sv[i])) sv[i] = ~0u;
   printf("FUXI SCANOUT: sspp vig0 size=%#x img=%#x xy=%#x out_size=%#x out_xy=%#x src0=%#x ystride0=%#x format=%#x unpack=%#x op_mode=%#x\n",
       sv[0], sv[1], sv[2], sv[3], sv[4], sv[5], sv[6], sv[7], sv[8], sv[9]);
  }
  if (fg_scanout_dpu_dump) { fg_scanout_dpu_dump = 0; fg_scanout_dpu_survey(); }
  return 0;
 }
 unsigned ar, line, start;
 if (!fg_scanout_read(FG_SCAN_INTF+0x2b4, &ar) || !(ar & 0x80000000u) != !fg_scanout_ar_on())
  return fg_scanout_stop("autorefresh changed");
 if (fg_scanout_double_active) {
  unsigned src;
  if (!fg_scanout_read(0xae05014, &src) || src != fg_scanout_pa(fg_scanout_front))
   return fg_scanout_stop("double SRC0 changed");
 }
 if (fg_busy && !(fg_abi_hard && fg_scanout_overlap_ok())) { fg_scanout_wait_ring++; if (publication) fg_scanout_charge_to(FG_WHY_RING); return 0; }
 if (fg_scanout_double_active && fg_scanout_ar_on()) {
  unsigned t;
  if (!fg_scanout_ar_sync()) return fg_scanout_stop("autorefresh sync");
  t = fg_scanout_ar_target();
  if (t > 2) { fg_scanout_ar_st[FG_AR_TARGET_WAIT]++; return 0; }
  fg_scanout_copy = t;
  fg_scanout_writable = 1;
  return 1;
 }
 if (fg_scanout_double_active && !publication) {
  if (fg_scanout_back_ready && !fg_scanout_triple_requested) return 0;
  fg_scanout_copy = fg_scanout_target();
  fg_scanout_writable = 1;
  return 1;
 }
 fg_scanout_xfer_sample();
 if (rsr("cntpct_el0") < fg_scanout_after) { fg_scanout_wait_floor++; if (publication) fg_scanout_charge_to(FG_WHY_FLOOR); return 0; }
 if (fg_scanout_te_mode) {
  unsigned count;
  if (!fg_scanout_read(FG_SCAN_INTF+0x298, &count)) return fg_scanout_stop("read te count");
  unsigned since = ((count >> 16) - fg_scanout_te_frame) & 0xffffu;
  unsigned past = (count & 0xffffu) - fg_scanout_te_init;
  if (since == 0 || (since == 1 && (count & 0xffffu) >= fg_scanout_te_init && past < FG_SCAN_TE_MARGIN)) {
   uint64_t hz = rsr("cntfrq_el0");
   unsigned stall_us = fg_scanout_panel120_active ? FG_SCAN_TE_STALL_US/2 : FG_SCAN_TE_STALL_US;
   if (rsr("cntpct_el0") >= fg_scanout_started + hz*stall_us/1000000) {
    /* No usable TE count: the default floor (from this START) for the boot. */
    unsigned fallback = fg_scanout_panel120_active ? FG_SCAN_DRAIN_US/2 : FG_SCAN_DRAIN_US;
    printf("FUXI SCANOUT: te counter stalled count=%#x since=%u; floor fallback drain_us=%u\n",
        count, since, fallback);
    fg_scanout_te_mode = 0;
    fg_scanout_drain_us = fallback;
    fg_scanout_after = fg_scanout_started + hz*fallback/1000000;
    fg_scanout_timeout = fg_scanout_after + hz/10;
   }
   fg_scanout_wait_te++; if (publication) fg_scanout_charge_to(FG_WHY_TE);
   return 0;
  }
 }
 if (!fg_scanout_read(FG_SCAN_INTF+0x2b0, &line) ||
     !fg_scanout_read(FG_SCAN_CTL+0x1c, &start)) return fg_scanout_stop("read transfer status");
 if ((start & 1) || ((line & 65535) != 0 && (line & 65535) < 2400)) {
  if (rsr("cntpct_el0") >= fg_scanout_timeout) {
   unsigned rd = 0;
   (void)fg_scanout_read(FG_SCAN_INTF+0x298, &rd);
   printf("FUXI SCANOUT: drain timeout wr=%#x rd=%#x start=%#x\n", line, rd, start);
   return fg_scanout_stop("transfer drain timeout");
  }
  fg_scanout_wait_xfer++; if (publication) fg_scanout_charge_to(FG_WHY_XFER);
  return 0;
 }
 if (fg_scanout_panel120_requested && !fg_scanout_panel120_attempted) {
  int switched = fg_panel120_switch();
  if (switched < 0) return -1;
  if (switched) {
   unsigned count;
   if (!fg_scanout_read(FG_SCAN_INTF+0x298, &count)) return fg_scanout_stop("panel120 read te count");
   fg_scanout_arm(count);
   fg_scanout_mid_setup();
   /* Drop the mixed-rate diagnostic window and wait with the new gates. */
   fg_scanout_window_start = rsr("cntpct_el0");
   fg_scanout_te_window = count >> 16;
   return 0;
  }
 }
 if (fg_scanout_dpu_stage2) {
  /* Bits 22+23: the inherited transfer has drained (and panel120 switched): run the one translated test, then wait with fresh gates. */
  unsigned count;
  fg_scanout_dpu_stage2 = 0;
  if (fg_scanout_dpu_stage2_run() < 0) return -1;
  if (!fg_scanout_read(FG_SCAN_INTF+0x298, &count)) return fg_scanout_stop("dpu stage2 read te count");
  fg_scanout_arm(count);
  return 0;
 }
 if (fg_scanout_double_requested && !fg_scanout_double_initialized) {
  fg_scanout_pixels_b = fuxi_gpu_nc_pointer(0xb8a00000u, 4320u*2400);
  if (!fg_scanout_pixels || !fg_scanout_pixels_b) return fg_scanout_stop("double buffer mapping");
  for (unsigned i = 0; i < 1080u*2400; i++)
   ((volatile uint32_t *)fg_scanout_pixels_b)[i] = ((const volatile uint32_t *)fg_scanout_pixels)[i];
  if (fg_scanout_triple_requested) {
   fg_scanout_pixels_c = fuxi_gpu_nc_pointer(0xb9400000u, 4320u*2400);
   if (!fg_scanout_pixels_c) return fg_scanout_stop("triple buffer mapping");
   for (unsigned i = 0; i < 1080u*2400; i++)
    ((volatile uint32_t *)fg_scanout_pixels_c)[i] = ((const volatile uint32_t *)fg_scanout_pixels)[i];
  }
  fg_scanout_double_initialized = fg_scanout_double_active = 1;
  printf("FUXI SCANOUT: double=1 A=0xb8000000 B=0xb8a00000 full_copy=1 flush=RMW|1%s\n",
      fg_scanout_triple_requested ? " triple=1 C=0xb9400000" : "");
 }
 __asm__ volatile("dsb sy" ::: "memory");
 if (!publication && fg_scanout_double_active) fg_scanout_copy = fg_scanout_target();
 fg_scanout_writable = 1;
 return 1;
}
int fuxi_gpu_scanout_acquire(void)
{
 FG_GUARD;
 /* A CPU frame left A as the front: GPU frames are complete, so B is free. */
 if (fg_scanout_double_initialized && !fg_scanout_double_active && !fg_scanout_failed && fuxi_gpu_scanout_owned()) {
  fg_scanout_double_active = 1; fg_scanout_front = fg_scanout_back_ready = 0;
  if (++fg_scanout_reentries <= 4) printf("FUXI SCANOUT: double re-entered after CPU frame (%u)\n", fg_scanout_reentries);
 }
 int writable = fg_scanout_acquire(0);
 if (writable == 1 && fg_scanout_double_active) fg_scanout_copy_open = 1;
 return writable;
}
int fuxi_gpu_scanout_double(void)
{
 return fg_scanout_double_active && !fg_scanout_failed;
}
int fuxi_gpu_scanout_triple(void)
{
 return fg_scanout_double_active && fg_scanout_triple_requested && !fg_scanout_failed;
}
int fuxi_gpu_scanout_pending(void)
{
 return fg_scanout_back_ready && !fg_scanout_failed;
}
/* CPU writers retain fPixels=A. Drain first, preserve the newest image and
 * use the single-buffer guard until the next GPU frame re-enters double mode. */
int fuxi_gpu_scanout_cpu_acquire(void)
{
 FG_GUARD;
 if ((fg_scanout_ar_on() || fg_scanout_ar_state == 4) && fg_scanout_ar_disable(0, FG_AR_OFF_CPU, "cpu fallback") < 0) return -1;
 int writable = fg_scanout_acquire(1);
 if (writable != 1 || !fg_scanout_double_active) return writable;
 unsigned newest = fg_scanout_back_ready ? (fg_scanout_triple_requested ? fg_scanout_ready : fg_scanout_front ^ 1u) : fg_scanout_front;
 if (newest) {
  for (unsigned i = 0; i < 1080u*2400; i++)
   ((volatile uint32_t *)fg_scanout_pixels)[i] = ((const volatile uint32_t *)fg_scanout_pixels_at(newest))[i];
 }
 if (!fg_scanout_address(0xb8000000u)) return fg_scanout_stop("CPU restore A");
 if (fg_scanout_back_ready) fg_scanout_ready_drops++;
 fg_scanout_back_ready = fg_scanout_double_active = fg_scanout_front = 0;
 if (fg_scanout_reentries < 4) printf("FUXI SCANOUT: double CPU fallback, drained; single buffer A until the next GPU frame\n");
 return 1;
}
/* The caller has acquired scanout and finished ALL CPU writes or observed the
 * GPU copy's cache-clean fence. Generic console/display kicks are suppressed
 * while owned. Autorefresh stays off; only this function starts transfers.
 */
static int fg_scanout_publish(void)
{
 unsigned ov = fg_abi_hard && fg_scanout_overlap_ok(); /* a copy into another buffer is open: the token stays with it */
 if (fg_scanout_failed || (fg_busy && !ov) || !fg_scanout_writable) return -1;
 unsigned arm_on = fg_scanout_ar_on();
 if (arm_on && !fg_scanout_ar_sync()) return fg_scanout_stop("autorefresh sync");
 if (fg_scanout_double_active && !arm_on) {
  int writable = fg_scanout_acquire(1);
  if (writable != 1) {
   if (!writable) fg_scanout_retries++;
   return writable < 0 ? -1 : 2;
  }
 }
 unsigned ar, start;
 fg_scanout_probe_account();
 if (!arm_on && (!fg_scanout_read(FG_SCAN_INTF+0x2b4, &ar) || (ar & 0x80000000u) ||
     !fg_scanout_read(FG_SCAN_CTL+0x1c, &start) || (start & 1)))
  return fg_scanout_stop("publish transfer status");
 if (fg_abi_mid && !arm_on && !fg_scanout_mid_allow()) return 2; /* outside the tear-safe part of the period: the caller retries */
 fg_scanout_charge_to(fg_scanout_last_why); /* the last wait ran until this poll */
 __asm__ volatile("dsb sy" ::: "memory");
 if (!ov) fg_scanout_writable = 0;
 if (fg_scanout_double_active && !fg_scanout_address(fg_scanout_pa(fg_scanout_triple_requested ? fg_scanout_ready : fg_scanout_front ^ 1u)))
  return fg_scanout_stop("double SRC0 flush");
 if (!arm_on && !fg_scanout_write(FG_SCAN_CTL+0x1c, 1)) return fg_scanout_stop("start transfer");
 fg_scanout_probe_t0 = rsr("cntpct_el0");
 fg_scanout_stamps.previous_start = fg_scanout_stamps.start;
 fg_scanout_stamps.start = mach_absolute_time();
 if (fg_scanout_double_active) {
  fg_scanout_front = fg_scanout_triple_requested ? fg_scanout_ready : fg_scanout_front ^ 1u;
  fg_scanout_buffer_flips[fg_scanout_front]++;
  fg_scanout_back_ready = 0;
  fg_scanout_flips++;
 }
 /* Read pointer after START: the TE gate then waits for a TE that follows
  * this read, so a TE racing the START write only makes it wait longer. */
 unsigned count = 0;
 __asm__ volatile("dsb sy" ::: "memory");
 if (!fg_scanout_read(FG_SCAN_INTF+0x298, &count)) return fg_scanout_stop("read te count");
 fg_scanout_probe_kick(count >> 16);
 if (arm_on) fg_scanout_ar_flipped();
 uint64_t now = rsr("cntpct_el0"), hz = rsr("cntfrq_el0");
 /* START-to-START gaps in actual panel periods: <=1, <=2, <=3, longer. */
 unsigned gap1 = 0;
 if (fg_scanout_last_start) {
  uint64_t gap = now - fg_scanout_last_start, tick = hz*fg_scanout_period_us/1000000;
  gap1 = gap <= tick + tick/8;
  fg_scanout_iv[gap <= tick + tick/8 ? 0 : gap <= 2*tick + tick/8 ? 1 : gap <= 3*tick + tick/8 ? 2 : 3]++;
 }
 fg_scanout_last_start = now;
 if (fg_scanout_ready_at) { fg_scanout_r2s += now - fg_scanout_ready_at; fg_scanout_r2s_n++; }
 fg_scanout_ready_at = fg_scanout_charged_at = 0;
 fg_scanout_te_frames = fg_scanout_last_count = count >> 16;
 fg_copy_log.te = count >> 16;
 if (++fg_scanout_starts % 600 == 0) {
  uint64_t span = now - fg_scanout_window_start;
  unsigned frames = (fg_scanout_te_frames - fg_scanout_te_window) & 0xffffu;
  /* TE rate in 0.1 Hz over the window (the 16-bit count wraps after ~9 min at 120 Hz). */
  unsigned te_dhz = span ? (unsigned)((uint64_t)frames*hz*10/span) : 0;
  printf("FUXI SCANOUT: waits per 600 starts ring=%u floor=%u transfer=%u te=%u drain_us=%u te_gate=%u"
      " te_hz=%u.%u gaps=%u/%u/%u/%u\n",
      fg_scanout_wait_ring, fg_scanout_wait_floor, fg_scanout_wait_xfer, fg_scanout_wait_te,
      fg_scanout_drain_us, fg_scanout_te_mode, te_dhz/10, te_dhz%10,
      fg_scanout_iv[0], fg_scanout_iv[1], fg_scanout_iv[2], fg_scanout_iv[3]);
  fg_scanout_probe_report();
  if (fg_abi_ar) {
   unsigned *st = fg_scanout_ar_st, *tf = fg_scanout_ar_tpf, *tl = fg_scanout_ar_ttl, *ll = fg_scanout_ar_ll;
   printf("FUXI SCANOUT: ar state=%u confirmed=%u flips=%u on=%u off_idle=%u off_fail=%u off_cpu=%u off_stop=%u drops=%u target_wait=%u wakes=%u"
       " te_per_flip(0/1/2/3/4+)=%u/%u/%u/%u/%u te_to_latch(0/1/2/3+)=%u/%u/%u/%u latch_line(idle/<200/<600/<1200/more)=%u/%u/%u/%u/%u"
       " latch_mid=%u bad_cur=%u fetch=%u front=%u hold=%#x\n",
       fg_scanout_ar_state, fg_scanout_ar_confirmed, st[FG_AR_FLIPS], st[FG_AR_ON], st[FG_AR_OFF_IDLE], st[FG_AR_OFF_FAIL], st[FG_AR_OFF_CPU], st[FG_AR_OFF_STOP],
       st[FG_AR_DROPS], st[FG_AR_TARGET_WAIT], st[FG_AR_WAKES], tf[0], tf[1], tf[2], tf[3], tf[4], tl[0], tl[1], tl[2], tl[3], ll[0], ll[1], ll[2], ll[3], ll[4],
       st[FG_AR_LATCH_MID], st[FG_AR_BAD_CUR], fg_scanout_ar_fetch, fg_scanout_front, fg_scanout_ar_hold);
   memset(st, 0, sizeof(fg_scanout_ar_st)); memset(tf, 0, sizeof(fg_scanout_ar_tpf)); memset(tl, 0, sizeof(fg_scanout_ar_ttl)); memset(ll, 0, sizeof(fg_scanout_ar_ll));
  }
  if (fg_abi_mid) {
   unsigned *h = fg_scanout_mid_hist, *g = fg_scanout_mid_g1;
   unsigned *vi = fg_scanout_mid_vimm, *vh = fg_scanout_mid_vheld;
   printf("FUXI SCANOUT: mid_start state=%u var=%u(%s) thresh=%#x->%#x imm=%u/%u/%u/%u/%u held=%u/%u/%u/%u/%u blocked_polls=%u forced=%u"
       " read_line_since_te<=4/<160/<600/<1200/<1800/<=2420/more starts=%u/%u/%u/%u/%u/%u/%u gap1=%u/%u/%u/%u/%u/%u/%u\n",
       fg_scanout_mid_state, fg_scanout_mid_var, fg_scanout_mid_name[fg_scanout_mid_var], fg_scanout_mid_old, fg_scanout_mid_new,
       vi[0], vi[1], vi[2], vi[3], vi[4], vh[0], vh[1], vh[2], vh[3], vh[4], fg_scanout_mid_wait, fg_scanout_mid_forced,
       h[0], h[1], h[2], h[3], h[4], h[5], h[6], g[0], g[1], g[2], g[3], g[4], g[5], g[6]);
   for (unsigned i = 0; i < 7; i++) h[i] = g[i] = 0;
   fg_scanout_mid_wait = fg_scanout_mid_forced = 0;
  }
  if (fg_abi_spin) {
   unsigned *st = fg_scanout_spin_stat, pus = (unsigned)(fg_scanout_ph_p*1000000/hz);
   printf("FUXI SCANOUT: te_start spin wakes=%u published=%u no_back_buffer=%u expired_or_raced=%u\n",
       st[FG_SPIN_WAKES], st[FG_SPIN_PUBLISHED], st[FG_SPIN_NO_BACK], st[FG_SPIN_RACED] + st[FG_SPIN_EXPIRED] + st[FG_SPIN_NO_ACTIVE]);
   printf("FUXI SCANOUT: te_phase hit=%u late=%u raced=%u expired=%u no_active=%u syncs=%u sync_fail=%u catch_fail=%u period_us=%u done_us=%u"
       " phase_err_us avg=%llu max=%llu wake_late_us avg=%llu max=%llu skipped=%u near_edge=%u armed_on_arrival=%u lead_us=%u\n",
       st[FG_SPIN_HIT], st[FG_SPIN_LATE], st[FG_SPIN_RACED], st[FG_SPIN_EXPIRED], st[FG_SPIN_NO_ACTIVE], st[FG_SPIN_SYNCS],
       st[FG_SPIN_SYNC_FAIL], st[FG_SPIN_CATCH_FAIL], pus, fg_scanout_ph_done_us,
       (unsigned long long)(fg_scanout_spin_err_n ? fg_scanout_spin_err_sum*1000000/hz/fg_scanout_spin_err_n : 0),
       (unsigned long long)(fg_scanout_spin_err_max*1000000/hz),
       (unsigned long long)(fg_scanout_spin_late_n ? fg_scanout_spin_late_sum*1000000/hz/fg_scanout_spin_late_n : 0),
       (unsigned long long)(fg_scanout_spin_late_max*1000000/hz),
       st[FG_SPIN_SKIPPED], st[FG_SPIN_NEAR_EDGE], st[FG_SPIN_ARRIVAL], fg_scanout_spin_lead_us());
   printf("FUXI SCANOUT: te_arm copy_open=%u unstolen=%u stalls=%u refused=%u offered=%u direct=%u armed_on_arrival=%u arr_busy=%u arr_stale=%u arr_past=%u near_edge=%u skipped=%u missed=%u phase_reject=%u"
       " margin_us<500/1000/2000/more wakes=%u/%u/%u/%u missed=%u/%u/%u/%u\n",
       st[FG_SPIN_COPY_OPEN], st[FG_SPIN_UNSTOLEN], st[FG_SPIN_STALL], st[FG_SPIN_REFUSED],
       st[FG_SPIN_OFFERED], st[FG_SPIN_DIRECT], st[FG_SPIN_ARRIVAL], st[FG_SPIN_ARR_BUSY], st[FG_SPIN_ARR_STALE], st[FG_SPIN_ARR_PAST],
       st[FG_SPIN_NEAR_EDGE], st[FG_SPIN_SKIPPED], st[FG_SPIN_MISSED], st[FG_SPIN_PHASE_REJECT],
       fg_scanout_spin_mw[0], fg_scanout_spin_mw[1], fg_scanout_spin_mw[2], fg_scanout_spin_mw[3],
       fg_scanout_spin_mm[0], fg_scanout_spin_mm[1], fg_scanout_spin_mm[2], fg_scanout_spin_mm[3]);
   if (fg_abi_hard) {
    unsigned *d = fg_scanout_hard_d2s;
    printf("FUXI SCANOUT: hard_spin waiting_at_done=%u started_in_window=%u done_to_start_us<50/<100/<200/<360/more=%u/%u/%u/%u/%u spin_us avg=%llu max=%llu"
        " cpu_spin_ms_per_s=%llu missed copy_open_same_buffer=%u copy_open_but_started=%u near_edge=%u overrun=%u"
        " retry_started idle_arrival=%u not_armed_busy/no_model/near_edge/edge_past=%u/%u/%u/%u armed_lost=%u\n",
        st[FG_SPIN_WAIT_DONE], st[FG_SPIN_IN_WINDOW], d[0], d[1], d[2], d[3], d[4],
        (unsigned long long)(fg_scanout_hard_n ? fg_scanout_hard_sum*1000000/hz/fg_scanout_hard_n : 0), (unsigned long long)(fg_scanout_hard_max*1000000/hz),
        (unsigned long long)(span ? fg_scanout_hard_total*1000/span : 0), st[FG_SPIN_COPY_SAME], st[FG_SPIN_COPY_STARTED], st[FG_SPIN_NEAR_EDGE], st[FG_SPIN_OVERRUN],
        st[FG_SPIN_IDLE_ARRIVAL], fg_scanout_miss_why[1], fg_scanout_miss_why[2], fg_scanout_miss_why[3], fg_scanout_miss_why[4], fg_scanout_miss_why[0]);
    for (unsigned i = 0; i < 5; i++) d[i] = fg_scanout_miss_why[i] = 0;
    fg_scanout_hard_n = 0; fg_scanout_hard_sum = fg_scanout_hard_max = fg_scanout_hard_total = 0;
   }
   for (unsigned i = 0; i < 4; i++) fg_scanout_spin_mw[i] = fg_scanout_spin_mm[i] = 0;
   for (unsigned i = 0; i < FG_SPIN_N; i++) st[i] = 0;
   fg_scanout_spin_err_sum = fg_scanout_spin_err_max = fg_scanout_spin_late_sum = fg_scanout_spin_late_max = 0;
   fg_scanout_spin_err_n = fg_scanout_spin_late_n = 0;
  }
  {
   uint64_t n = fg_scanout_xfer_n ? fg_scanout_xfer_n : 1, r = fg_scanout_r2s_n ? fg_scanout_r2s_n : 1, cum = 0;
   unsigned p50 = 0;
   for (unsigned i = 0; i < 8; i++) { cum += fg_scanout_xfer_hist[i]; if (cum*2 >= n) { p50 = i < 7 ? fg_scanout_xfer_edges[i] : 0; break; } }
   printf("FUXI SCANOUT: xfer_done_us n=%u avg=%llu p50<%u max=%llu hist<5/6/7/7.5/8/9/12ms/more=%u/%u/%u/%u/%u/%u/%u/%u"
       " ready_to_start_us n=%llu avg=%llu floor=%llu xfer=%llu te=%llu ring=%llu start_bit=%u\n",
       fg_scanout_xfer_n, (unsigned long long)(fg_scanout_xfer_sum/n), p50, (unsigned long long)fg_scanout_xfer_max,
       fg_scanout_xfer_hist[0], fg_scanout_xfer_hist[1], fg_scanout_xfer_hist[2], fg_scanout_xfer_hist[3],
       fg_scanout_xfer_hist[4], fg_scanout_xfer_hist[5], fg_scanout_xfer_hist[6], fg_scanout_xfer_hist[7],
       (unsigned long long)fg_scanout_r2s_n, (unsigned long long)(fg_scanout_r2s*1000000/hz/r),
       (unsigned long long)(fg_scanout_charge[FG_WHY_FLOOR]*1000000/hz/r), (unsigned long long)(fg_scanout_charge[FG_WHY_XFER]*1000000/hz/r),
       (unsigned long long)(fg_scanout_charge[FG_WHY_TE]*1000000/hz/r), (unsigned long long)(fg_scanout_charge[FG_WHY_RING]*1000000/hz/r), fg_abi_start);
   fg_scanout_xfer_n = 0; fg_scanout_xfer_sum = fg_scanout_xfer_max = 0; fg_scanout_r2s = fg_scanout_r2s_n = 0;
   for (unsigned i = 0; i < 8; i++) fg_scanout_xfer_hist[i] = 0;
   for (unsigned i = 0; i < 4; i++) fg_scanout_charge[i] = 0;
  }
  if (fg_scanout_double_active) {
   unsigned current, expected = fg_scanout_pa(fg_scanout_front);
   if (!fg_scanout_read(0xae050a4, &current)) return fg_scanout_stop("read current SRC0");
   if (current == expected) fg_scanout_current_match++; else fg_scanout_current_mismatch++;
   char triple[128] = {0};
   if (fg_scanout_triple_requested) snprintf(triple, sizeof(triple), " triple=1 flips_a=%u flips_b=%u flips_c=%u drops=%u",
       fg_scanout_buffer_flips[0], fg_scanout_buffer_flips[1], fg_scanout_buffer_flips[2], fg_scanout_ready_drops);
   printf("FUXI SCANOUT: double=1 flips=%u back_ready_drops=%u publish_retries=%u current_src0=%#x expected=%#x match=%u mismatch=%u%s\n",
       fg_scanout_flips, fg_scanout_ready_drops, fg_scanout_retries, current, expected,
       fg_scanout_current_match, fg_scanout_current_mismatch, triple);
   fg_scanout_buffer_flips[0] = fg_scanout_buffer_flips[1] = fg_scanout_buffer_flips[2] = 0;
   fg_scanout_flips = fg_scanout_ready_drops = fg_scanout_retries = 0;
  }
  fg_scanout_wait_ring = fg_scanout_wait_floor = fg_scanout_wait_xfer = fg_scanout_wait_te = 0;
  fg_scanout_iv[0] = fg_scanout_iv[1] = fg_scanout_iv[2] = fg_scanout_iv[3] = 0;
  fg_scanout_window_start = now;
  fg_scanout_te_window = fg_scanout_te_frames;
 }
 __asm__ volatile("dsb sy" ::: "memory");
 if (!arm_on) {
  fg_scanout_mid_account(count, gap1);
  fg_scanout_arm(count);
  fg_scanout_spin_kick(1);
  fg_scanout_ar_arming();
 }
 return 1;
}
/* A token the spin thread consumed while no copy was open is restored for the caller that still holds a finished copy. */
static void fg_scanout_unsteal(void)
{
 if (!fg_scanout_writable && fg_scanout_spin_stolen && !fg_scanout_failed && !fg_busy) {
  fg_scanout_writable = 1; fg_scanout_spin_stat[FG_SPIN_UNSTOLEN]++;
 }
 fg_scanout_spin_stolen = 0;
}
static void fg_scanout_refused(const char *where)
{
 fg_scanout_spin_stat[FG_SPIN_REFUSED]++;
 if (fg_scanout_stall_prints < 8) {
  fg_scanout_stall_prints++;
  printf("FUXI SCANOUT: %s refused failed=%u busy=%u writable=%u copy_open=%u stolen=%u spin_busy=%u back_ready=%u starts=%u\n", where,
      fg_scanout_failed, fg_busy, fg_scanout_writable, fg_scanout_copy_open, fg_scanout_spin_stolen, fg_scanout_spin_busy, fg_scanout_back_ready, fg_scanout_starts);
 }
}
/* What became of the frame that was waiting when the last offer came (read by the display path right after fuxi_gpu_scanout_publish,
 * for its not-shown accounting): 0 none waiting, 1 STARTed by the spin thread during the copy (shown), 2 replaced before any START. */
static unsigned fg_scanout_offer_fate;
unsigned fuxi_gpu_scanout_offer_fate(void) { return fg_scanout_offer_fate; }
/* The spin thread STARTed the waiting frame and no retry has reported it yet (1, cleared): the display path then counts it shown. */
int fuxi_gpu_scanout_async_take(void)
{
 FG_GUARD;
 unsigned a = fg_scanout_async_published; fg_scanout_async_published = 0;
 return a != 0;
}
int fuxi_gpu_scanout_publish(void)
{
 if (!fuxi_gpu_scanout_owned()) { fg_scanout_offer_fate = 0; return 0; }
 FG_GUARD;
 fg_scanout_copy_open = 0;
 fg_scanout_offer_fate = fg_scanout_async_published ? 1 : fg_scanout_back_ready ? 2 : 0;
 fg_scanout_unsteal();
 if (fg_scanout_failed || fg_busy || !fg_scanout_writable) { fg_scanout_refused("publish"); return -1; }
 if (fg_scanout_async_published) fg_scanout_async_published = 0; /* the waiting frame was STARTed during this copy (fate 1): this offer replaces its record */
 if (fg_scanout_double_active) {
  if (fg_scanout_back_ready && fg_scanout_triple_requested) fg_scanout_ready_drops++;
  fg_scanout_ready = fg_scanout_triple_requested ? fg_scanout_copy : fg_scanout_front ^ 1u;
  fg_scanout_back_ready = 1;
  fg_scanout_ready_at = rsr("cntpct_el0"); fg_scanout_charged_at = 0;
  if (fg_abi_spin) fg_scanout_spin_stat[FG_SPIN_OFFERED]++;
 }
 int r = fg_scanout_publish();
 if (r == 1 && fg_abi_spin) fg_scanout_spin_stat[FG_SPIN_DIRECT]++;
 if (r == 2) {
  fg_scanout_kick_why = 0;
  if (fg_abi_hard) { unsigned line, start; fg_scanout_offer_xfer = fg_scanout_read(FG_SCAN_CTL+0x1c, &start) && fg_scanout_read(FG_SCAN_INTF+0x2b0, &line) && ((start & 1) || ((line & 65535) != 0 && (line & 65535) < 2400)); }
  fg_scanout_spin_kick(0);
 }
 return r;
}
int fuxi_gpu_scanout_retry(void)
{
 FG_GUARD;
 if (fg_scanout_failed) return -1;
 fg_scanout_copy_open = 0; /* retry runs only with no copy in flight */
 if (fg_scanout_async_published) { fg_scanout_async_published = 0; return 1; } /* the spin thread already STARTed the waiting buffer */
 if (!fg_scanout_back_ready || fg_busy) return 0;
 /* Watchdog: a frame waiting more than 4 periods is visible in the log (first 8, then every 256th). */
 uint64_t now = rsr("cntpct_el0"), hz = rsr("cntfrq_el0");
 if (fg_scanout_ready_at && now - fg_scanout_ready_at > 4*hz*fg_scanout_period_us/1000000) {
  unsigned n = ++fg_scanout_spin_stat[FG_SPIN_STALL];
  if (n <= 8 || !(n & 255))
   printf("FUXI SCANOUT: stall frame waiting %llu us writable=%u spin_busy=%u stolen=%u copy_open=%u starts=%u start_age_us=%llu\n",
       (unsigned long long)((now - fg_scanout_ready_at)*1000000/hz), fg_scanout_writable, fg_scanout_spin_busy, fg_scanout_spin_stolen,
       fg_scanout_copy_open, fg_scanout_starts, (unsigned long long)((now - fg_scanout_last_start)*1000000/hz));
 }
 fg_scanout_unsteal();
 int r = fg_scanout_publish();
 if (r == 1 && fg_abi_hard && !fg_scanout_spin_busy) { if (!fg_scanout_offer_xfer) fg_scanout_spin_stat[FG_SPIN_IDLE_ARRIVAL]++; else fg_scanout_miss_why[fg_scanout_kick_why]++; }
 if (r < 0) fg_scanout_refused("retry");
 return r;
}
/* Reserve the idle ring across the surface-cache transaction. Client submit
 * returns busy until release; map/TLBI/copy cannot race a client TTBR switch.
 * Called under SweetSurface's lock, matching the existing surface -> GPU order.
 */
int fuxi_gpu_scanout_reserve(void)
{
 if (!fg_scanout_requested) return 1;
 FG_GUARD;
 if (!fuxi_gpu_ready()) return 1;
 if (fg_scanout_failed || fg_scanout_reserved || fg_busy) return 0;
 if (fg_abi_inline && fg_inl_usable()) return 1; /* bit 21: no reservation, the copy queues behind client jobs in their root */
 if (fg_abi_live_for_scanout() && fg_abi_observe() != 0) { if (fg_client_busy) fg_abi_present_wait = 1; return 0; }
 if (fg_client_busy) { fg_abi_present_wait = 1; return 0; }
 fg_scanout_reserved = 1;
 return 1;
}
void fuxi_gpu_scanout_release(void)
{
 if (!fg_scanout_requested) return;
 FG_GUARD;
 fg_scanout_reserved = 0;
}
/* Compare the whole copied rectangle to exactly the
 * CPU row-copy oracle, after the fence and before releasing the source pin.
 * Scalar aligned device loads only; no CPU framebuffer writes in this check.
 */
static int fg_scanout_verify(void)
{
 if (!fg_scanout_checks || fg_scanout_checked >= fg_scanout_checks) return 1;
 uint64_t began = mach_absolute_time(), ns;
 unsigned mismatch = 0, first_x = 0, first_y = 0;
 __asm__ volatile("dsb sy" ::: "memory");
 for (unsigned y = 0; y < fg_scanout_h; y++) {
  const uint32_t *src = (const uint32_t *)(fg_scanout_source +
      (uint64_t)(fg_scanout_sy+y)*fg_scanout_pitch + fg_scanout_sx*4);
  const volatile uint32_t *dst = (const volatile uint32_t *)(fg_scanout_pixels_at(fg_scanout_double_active ? (fg_scanout_triple_requested ? fg_scanout_copy : fg_scanout_front ^ 1u) : 0) +
      (uint64_t)(fg_scanout_dy+y)*4320 + fg_scanout_dx*4);
  for (unsigned x = 0; x < fg_scanout_w; x++) if (src[x] != dst[x]) {
   if (!mismatch) { first_x = x; first_y = y; }
   mismatch++;
  }
 }
 absolutetime_to_nanoseconds(mach_absolute_time()-began, &ns);
 printf("FUXI SCANOUT: pixel frame=%u pixels=%u mismatches=%u first=%u,%u check_us=%llu\n",
     ++fg_scanout_checked, fg_scanout_w*fg_scanout_h, mismatch, first_x, first_y,
     (unsigned long long)(ns/1000));
 return !mismatch;
}
