/* Opt-in touch/compile boost for SM8550. One serialized worker owns MMIO.
 * Input/swap hooks only publish timestamps and queue work; no MMIO or sleeps.
 * No voltage, PLL, thermal-trip or power-collapse registers are programmed.
 */
#include <pexpert/arm64/board_config.h>
#if defined(FUXI)
#include <sys/param.h>
#include <sys/systm.h>
#include <kern/clock.h>
#include <machine/machine_routines.h>
#include <pexpert/pexpert.h>
#define MACH_KERNEL_PRIVATE 1
#include <kern/thread_call.h>
#undef MACH_KERNEL_PRIVATE
#include <sys/errno.h>
#include <sys/sysctl.h>
#include <sys/proc.h>
#include <sys/codesign.h>
#include <kern/locks.h>
#include <kern/thread.h>
#include "fuxi_perf_logic.h"
#include "fuxi_compile_abi.h"
/* Same abort-contained access primitive as the built-in GPU driver; available
 * even when fuxigpu=0. Each register access is naturally aligned. */
extern vm_offset_t ml_io_map(vm_offset_t, vm_size_t);
extern unsigned fuxi_gpu_access(uintptr_t, uint64_t *, unsigned, unsigned);
static uintptr_t fp_epss[3], fp_tsens[2];
static const uint64_t fp_epss_pa[3] = {0x17d91000,0x17d92000,0x17d93000};
static const uint64_t fp_tsens_pa[2] = {0xc271000,0xc272000};
static const char * const fp_domain[3] = {"silver","gold","prime"};
static const unsigned fp_cores[3] = {3,4,1};
static const unsigned fp_cool_cap[3] = {1228800,1401600,1363200};
static const unsigned fp_high_cap[3] = {1228800,2400000,2400000};
static unsigned fp_cool[3], fp_high[3], fp_cool_khz[3], fp_high_khz[3];
static unsigned fp_idle[3], fp_idle_khz[3], fp_idle_ms, fp_idle_floor;
static uint64_t fp_idle_after, fp_idle_start, fp_idle_entry;
static uint64_t fp_idle_time, fp_idle_entries, fp_idle_transitions;
/* Published only so a swap can queue the existing worker when leaving idle. */
static unsigned fp_idling;
static unsigned fp_percore[3], fp_domain_failed[3], fp_applied[3], fp_applied_valid[3];
static uint64_t fp_retry_interval, fp_retry_at, fp_retry_end;
static unsigned fp_attempted, fp_retries;
static struct {
 const char *step;
 unsigned domain, offset;
 uint64_t pa;
 uint32_t value;
 int access, row;
} fp_diagnostic;
static uint64_t fp_input, fp_swap, fp_hold, fp_last, fp_window, fp_window_start;
static uint64_t fp_boost_time, fp_boosts;
static int fp_max_temp, fp_boosted;
/* Temperatures are deci-Celsius, rise is dC/second. Only this worker owns
 * history and votes. The 68 C ceiling is the default; fuxiperflimit raises it
 * to at most 80 C, which the owner chose on 3 Nov 2026 for hand tests (stock
 * Android lets the SM8550 cores run far hotter and throttles on skin
 * temperature; LMh still limits in hardware). */
static unsigned fp_hot = 620, fp_limit = 680, fp_resume = 580;
static unsigned fp_rise = 100, fp_fast_ms = 20, fp_budget_ms = 200, fp_cap_mhz;
/* Slope window. TSENS reports 0.4 C steps: one step in 20 ms is 200 dC/s and
 * latched the rise guard above 55 C on every tick (observed 54.5 C refusals). */
static unsigned fp_rate_ms = 200;
static uint64_t fp_budget, fp_duty_window, fp_recovery;
static uint64_t fp_recover_since, fp_above_time, fp_cuts, fp_active_start;
static uint64_t fp_duty_begin[16], fp_duty_end[16];
static unsigned fp_duty_count;
static int fp_prev_temp, fp_prev_valid, fp_blocked;
static uint64_t fp_rate_elapsed;
static int fp_rate_temp, fp_rate_valid, fp_rate;
static unsigned fp_pressure_enabled;
static int fp_pressure = -1; /* No fabricated nominal before the first sample. */
static uint64_t fp_pressure_since, fp_pressure_dwell; /* fall hold, 10 s */
/* Actual (LMh-limited) EPSS domain state +0x20, L x 19.2 MHz, sampled per
 * worker tick. [0] while boosted, [1] otherwise; gold and prime only. */
static uint64_t fp_cur_sum[2][2], fp_cur_n[2];
static unsigned fp_cur_max[2][2];
/* fpk=MHz (1401 up to the boost cap, default off): a raised gold/prime base.
 * The cool vote becomes the fastest eligible row at/below MHz while the
 * thermal guards allow it (a valid sample and no hot/limit/slope latch);
 * otherwise it falls back to the compiled fp_cool_cap rows. Silver, idle and
 * the boost rows are unchanged. */
static unsigned fp_base_mhz, fp_base[3], fp_base_khz[3];
static int fp_base_on;
static uint64_t fp_base_time, fp_base_fallback_time;
/* fl3=1 (default off): EPSS L3 at 0x17d90000 ("qcom,epss-l3", Linux osm-l3.c
 * epss_l3_l3_vote, sheng b0ccf61): enable +0x0 bit 0, HLOS vote +0x90 (a LUT
 * row index), LUT +0x100. While any high vote runs, the vote is the fastest
 * non-turbo row (the stock launch hint's L3 1804.8 MHz); unboost restores the
 * boot vote read at ready. Only +0x90 is written, never before fp_l3_check
 * matched the boot vote to the running state; firmware owns L3 voltage. */
static uintptr_t fp_l3;
static const uint64_t fp_l3_pa = 0x17d90000;
static unsigned fp_l3_enabled, fp_l3_ready, fp_l3_boot, fp_l3_high, fp_l3_voted;
static unsigned fp_l3_boot_khz, fp_l3_high_khz, fp_l3_said, fp_l3_state_max;
static uint64_t fp_l3_samples, fp_l3_votes;
/* fla=MS (500..3000, default off): stock launch policy, gold/prime (and L3
 * with fl3) high for MS after the exec of an application's main executable
 * (kern_exec.c, patch 0121). Silver keeps cool: fuxicores=0x7c leaves the
 * A510s to interrupts and bound threads, and a silver boost also raises the
 * loader's monitor core (cpu@100, docs-lanes/thermal.md).
 * fsf=PCT (30..90, default off): stock-like scrolling, gold/prime at least
 * PCT % of their fastest non-turbo row while input is under 1.5 s old.
 * fsw=MS (100..1000, default off): the same floor while frames flow, i.e.
 * the last two AppleCLCD swaps are both at most MS old (scripted HID never
 * reaches the input hook; sheng 36ec562 floors on GPU submits). The level
 * is fsf's PCT, or 60 with fsw alone. All obey the thermal guards (start,
 * hot, limit, slope), not the touch duty. */
static unsigned fp_launch_ms, fp_floor_pct, fp_fsf, fp_frame_ms, fp_floor_touch;
static uint64_t fp_launch_hold, fp_launch, fp_launch_last, fp_launch_start;
static uint64_t fp_launch_time, fp_launches, fp_launch_refused, fp_floor_time;
static uint64_t fp_frame_hold, fp_swap_prev, fp_frame_time;
static unsigned fp_launch_seen, fp_launch_said, fp_floor[3], fp_floor_khz[3];
static int fp_launch_high, fp_floor_on, fp_floor_frames;
static unsigned fp_enabled, fp_started, fp_ready;
static thread_call_data_t fp_call;
/* Refusals are EBUSY, never EAGAIN: userland_sysctl() retries a handler's
 * EAGAIN in a loop, which spun backboardd in the kernel (observed). */
/* One lease globally: Backend serializes compiles. No allocations or user
 * supplied task/thread handles. References prevent thread-address reuse. */
static LCK_GRP_DECLARE(fp_group, "fuxi-perf");
static LCK_MTX_DECLARE(fp_lock, &fp_group);
static unsigned fp_compile_enabled, fp_compile_high, fp_compile_voted, fp_touch_high;
static unsigned fp_compile_refusals;
static thread_t fp_compile_thread;
static uint64_t fp_compile_deadline, fp_compile_cap, fp_compile_start;
static struct fuxi_compile_reply fp_compile_reply;
extern void sched_perfcontrol_update_recommended_cores(uint32_t);
/* fuxicores=MASK (logical CPUs): the clutch scheduler is homogeneous and
 * put UI threads mostly on the A510s (logical 0-1, observed). Unrecommended
 * cores keep interrupts and bound threads only. Applied once when ready. */
static unsigned fp_recommend;
static void fp_apply_cores(void)
{
 if (!fp_recommend) return;
 sched_perfcontrol_update_recommended_cores(fp_recommend);
 printf("FUXI PERF: recommended cores 0x%x\n",fp_recommend);
}
extern int fuxi_compile_bind(unsigned);
extern void fuxi_compile_migrate(void);
extern void fuxi_compile_unbind(thread_t, int);
extern int fuxi_compile_alive(thread_t);
extern int fuxi_compile_online(int);
static void fp_worker_locked(thread_call_param_t, thread_call_param_t);
static uint64_t fp_duty_used(uint64_t);

/* cpu -1: realtime/prebound callers (the CA render thread) are not bound;
 * their lease votes high on both gold and prime instead. */
static int fp_compile_domain(unsigned d)
{
 int cpu = fp_compile_reply.cpu;
 return cpu < 0 ? d == 1 || d == 2 : d == (cpu == 7 ? 2u : 1u);
}
static int fp_compile_failed(void)
{
 int cpu = fp_compile_reply.cpu;
 return cpu < 0 ? fp_domain_failed[1] && fp_domain_failed[2] :
     fp_domain_failed[cpu == 7 ? 2 : 1] || !fuxi_compile_online(cpu);
}
static void fp_compile_cut(unsigned reason, uint64_t now)
{
 if (fp_compile_high) {
  uint64_t elapsed = fp_compile_voted ? now-fp_compile_start : 0;
  absolutetime_to_nanoseconds(elapsed,&fp_compile_reply.boosted_ns);
  fp_compile_high = 0;
  fp_compile_voted = 0;
  fp_compile_reply.state = reason;
  if (fp_compile_reply.cpu >= 0)
   fuxi_compile_unbind(fp_compile_thread,fp_compile_reply.cpu);
  printf("FUXI PERF: compile end cpu=%d reason=%u voted_us=%llu\n",
      fp_compile_reply.cpu,reason,fp_compile_reply.boosted_ns/1000);
 }
}
static void fp_compile_forget(void)
{
 thread_t thread = fp_compile_thread;
 fp_compile_thread = THREAD_NULL;
 if (thread != THREAD_NULL) thread_deallocate(thread);
}
static void fp_compile_poll(uint64_t now, int safe, int duty_ok)
{
 if (fp_compile_thread == THREAD_NULL) return;
 int dead = !fuxi_compile_alive(fp_compile_thread);
 if (dead || now >= fp_compile_deadline) {
  fp_compile_cut(dead ? FC_DEAD : FC_TIMEOUT,now);
  fp_compile_forget();
 } else if (!safe || (!fp_compile_voted && fp_prev_temp > 600) ||
     !duty_ok || fp_compile_failed()) {
  fp_compile_cut(!safe || (!fp_compile_voted && fp_prev_temp > 600) ?
      FC_THERMAL : !duty_ok ? FC_DUTY : FC_OFFLINE,now);
 }
}

/* Match the existing GPU backboardd gate, narrowed to platform backboardd
 * even for root. proc_self() is the caller; no pid/task spoofing or UID gate. */
static int fp_compile_admitted(void)
{
 char name[32] = {};
 proc_selfname(name,sizeof(name));
 if (strcmp(name,"backboardd")) return 0;
 proc_t p = proc_self();
 if (!p) return 0;
 int platform = csproc_get_platform_binary(p) != 0;
 proc_rele(p);
 return platform;
}
static int fp_compile_command(int command, struct fuxi_compile_reply *reply)
{
 int error = 0, migrate = 0, refused = 0;
 if (!fp_compile_enabled || !__atomic_load_n(&fp_enabled,__ATOMIC_ACQUIRE)) return ENOTSUP;
 if (!fp_compile_admitted()) return EPERM;
 if (command != 0 && command != 1) return EINVAL;
 lck_mtx_lock(&fp_lock);
 /* Fresh sensors, thermal slope, expired/dead lease and duty accounting.
  * This is the same serialized worker as touch, never a second MMIO owner. */
 fp_worker_locked(NULL,NULL);
 uint64_t now = mach_absolute_time();
 thread_t self = current_thread();
 if (!command) {
  if (fp_compile_thread == self) {
   if (--fp_compile_reply.depth == 0) {
    fp_compile_cut(FC_LEFT,now);
    *reply = fp_compile_reply;
    fp_compile_forget();
    fp_worker_locked(NULL,NULL); /* Drop vote before returning. */
   } else *reply = fp_compile_reply;
  } else *reply = (struct fuxi_compile_reply){.version=1,.cpu=-1};
 } else if (fp_compile_thread == self) {
  /* Nested scopes neither renew a cut lease nor extend its deadline. */
  if (!fp_compile_high) error = EBUSY;
  else if (fp_compile_reply.depth == 16) error = EOVERFLOW;
  else { fp_compile_reply.depth++; *reply = fp_compile_reply; }
 } else if (fp_compile_thread != THREAD_NULL) refused = 1;
 else if (!fp_ready || !fp_prev_valid) refused = 2;
 else if (fp_prev_temp > 600 || fp_blocked) refused = 3;
 else if (fp_duty_used(now) >= fp_budget || (!fp_boosted && fp_duty_count == 16)) refused = 4;
 else {
  int cpu = fuxi_compile_bind((fp_domain_failed[1] ? 0 : 2) | (fp_domain_failed[2] ? 0 : 4));
  if (cpu == -1) refused = 5;
  else {
   if (cpu < 0) cpu = -1;
   thread_reference(self);
   fp_compile_thread = self;
   fp_compile_start = now;
   fp_compile_deadline = now+fp_compile_cap;
   fp_compile_reply = (struct fuxi_compile_reply){.version=1,.cpu=cpu,.state=FC_ACTIVE,.depth=1};
   fp_compile_high = 1;
   fp_worker_locked(NULL,NULL); /* Apply high with fresh thermal veto. */
   *reply = fp_compile_reply;
   migrate = cpu >= 0;
   printf("FUXI PERF: compile enter cpu=%d cap_ms=2000 duty_ms=%u/1000 state=%u\n",
       cpu,fp_budget_ms,reply->state);
  }
 }
 if (refused) {
  /* 1 busy, 2 not ready, 3 thermal, 4 duty, 5 no online fast core. */
  error = EBUSY;
  if (++fp_compile_refusals <= 16 || !(fp_compile_refusals & 63))
   printf("FUXI PERF: compile refused reason=%d temp=%d dC count=%u\n",
       refused,fp_prev_temp,fp_compile_refusals);
 }
 lck_mtx_unlock(&fp_lock);
 if (migrate) fuxi_compile_migrate();
 return error;
}
static int fp_compile_sysctl SYSCTL_HANDLER_ARGS
{
 if (!fp_compile_enabled || !__atomic_load_n(&fp_enabled,__ATOMIC_ACQUIRE)) return ENOTSUP;
 if (!fp_compile_admitted()) return EPERM;
 if (!req->newptr || req->newlen != sizeof(int32_t)) return EINVAL;
 if (req->oldptr && req->oldlen < sizeof(struct fuxi_compile_reply)) return ENOMEM;
 int32_t command;
 int error = SYSCTL_IN(req,&command,sizeof(command));
 if (error) return error;
 struct fuxi_compile_reply reply = {};
 error = fp_compile_command(command,&reply);
 if (error) return error;
 error = req->oldptr ? SYSCTL_OUT(req,&reply,sizeof(reply)) : 0;
 if (error && command == 1) (void)fp_compile_command(0,&reply);
 return error;
}
SYSCTL_PROC(_kern, OID_AUTO, fuxicompileboost,
    CTLTYPE_OPAQUE | CTLFLAG_RW | CTLFLAG_ANYBODY | CTLFLAG_LOCKED,
    0, 0, fp_compile_sysctl, "S,fuxi_compile_reply", "platform backboardd current-thread compile lease");
static int fp_pressure_sysctl SYSCTL_HANDLER_ARGS
{
 /* Reads never run the worker or touch MMIO. The lock snapshots readiness
  * and pressure together; SYSCTL_OUT runs after dropping it. */
 if (req->newptr) return EPERM;
 if (!__atomic_load_n(&fp_enabled,__ATOMIC_ACQUIRE)) return ENOTSUP;
 lck_mtx_lock(&fp_lock);
 int level = fp_pressure, enabled = fp_pressure_enabled;
 lck_mtx_unlock(&fp_lock);
 if (!enabled) return ENOTSUP;
 if (level < 0) return EBUSY;
 return SYSCTL_OUT(req,&level,sizeof(level));
}
SYSCTL_PROC(_kern, OID_AUTO, fuxipressure,
    CTLTYPE_INT | CTLFLAG_RD | CTLFLAG_LOCKED,
    0, 0, fp_pressure_sysctl, "I", "OSThermalPressureLevel (fuxiperf=1 fuxitp=1)");
static int fp_read(uintptr_t base, unsigned off, uint32_t *value)
{
 uint64_t v = 0;
 if (fuxi_gpu_access(base+off, &v, 0, 0)) return 0;
 *value = (uint32_t)v;
 return 1;
}
static int fp_vote(unsigned d, unsigned level)
{
 for (unsigned c = 0; c < (fp_percore[d] ? fp_cores[d] : 1); c++) {
  /* Power may disappear between samples or per-core votes. Never write a
   * domain after an unsuccessful/clear enable read; latch it at/below cool. */
  uint32_t enable;
  if (!fp_read(fp_epss[d],0,&enable) || !(enable & 1)) {
   fp_domain_failed[d] = 1;
   return 0;
  }
  if (fp_domain_failed[d] && level != fp_idle[d]) level = fp_cool[d];
  uint64_t v = level;
  unsigned off = 0x320+4*c;
  if (fuxi_gpu_access(fp_epss[d]+off, &v, 1, 0)) return 0;
  uint32_t got;
  if (!fp_read(fp_epss[d], off, &got) || got != level) return 0;
 }
 return 1;
}
/* CPU sensors: tsens0 cpuss-0..3 and cpu-1-0..10 (1..15),
 * tsens1 cpu-0-0..2 (1..3). Exclude aoss and non-CPU sensors.
 * Loader/host indices are zero-based: STATUS(s) is TM+0xa0+4*s.
 * Linux tsens-v2 uses per-sensor VALID, not a TM_TRDY bit-0 gate.
 * Count only valid, plausible sensors in this sample. At least 75%
 * (14 of 18) must be valid; never reuse an older temperature. */
static int fp_temperature(int *hottest)
{
 int hottest_cpu = -400;
 unsigned valid = 0;
 for (unsigned d = 0; d < 2; d++) {
  for (unsigned n = 1; n <= (d ? 3 : 15); n++) {
   uint32_t status;
   if (!fp_read(fp_tsens[d], 0xa0+4*n, &status) || !(status & (1u<<21))) continue;
   int temp = (int)(status & 0xfff);
   if (temp & 0x800) temp -= 4096;
   if (temp < -400 || temp > 1500) continue;
   valid++;
   if (temp > hottest_cpu) hottest_cpu = temp;
  }
 }
 if (valid*4 < 18*3) return 0;
 *hottest = hottest_cpu;
 return 1;
}
/* ml_io_map/io_map return VA + the PA's kernel-page offset. Do not add it
 * again: EPSS shares one 16K physical page, as do the TSENS TM windows.
 * A failed read leaves UINT32_MAX as a sentinel, distinguished by access=0. */
static int fp_init_fail(const char *step, unsigned d, uint64_t pa,
    unsigned off, uint32_t value, int access, int row)
{
 fp_diagnostic.step = step;
 fp_diagnostic.domain = d;
 fp_diagnostic.pa = pa;
 fp_diagnostic.offset = off;
 fp_diagnostic.value = value;
 fp_diagnostic.access = access;
 fp_diagnostic.row = row;
 return 0;
}
static void fp_init_report(const char *outcome)
{
 unsigned d = fp_diagnostic.domain;
 printf("FUXI PERF: %s after %u retries, step=%s domain=%s(%u) PA=%#llx offset=%#x value=%#x access=%d row=%d\n",
     outcome,fp_retries,fp_diagnostic.step,
     fp_diagnostic.step[0] == 't' ? "tsens" : fp_domain[d],d,
     fp_diagnostic.pa,fp_diagnostic.offset,fp_diagnostic.value,
     fp_diagnostic.access,fp_diagnostic.row);
}
static int fp_initialize(void)
{
 unsigned percore[3], cool_level[3], high_level[3], cool_freq[3], high_freq[3];
 unsigned idle_level[3], idle_freq[3], base_level[3], base_freq[3], floor_level[3], floor_freq[3];
 for (unsigned d = 0; d < 3; d++) {
  if (!fp_epss[d]) fp_epss[d] = ml_io_map(fp_epss_pa[d], 0x1000);
  if (!fp_epss[d]) return fp_init_fail("map",d,fp_epss_pa[d],0,0,0,-1);
  uint32_t enable = UINT32_MAX, dcvs = UINT32_MAX, rows[40];
  int access = fp_read(fp_epss[d],0,&enable);
  if (!access || !(enable & 1))
   return fp_init_fail("enable",d,fp_epss_pa[d],0,enable,access,-1);
  access = fp_read(fp_epss[d],0xb0,&dcvs);
  if (!access) return fp_init_fail("dcvs",d,fp_epss_pa[d],0xb0,dcvs,0,-1);
  percore[d] = dcvs & 1;
  /* Read the terminator, but never touch the remaining unused slots. A
   * protected tail must not disable an otherwise usable frequency table. */
  unsigned count;
  for (count = 0; count < 40; count++) {
   unsigned off = 0x100+4*count;
   rows[count] = UINT32_MAX;
   if (!fp_read(fp_epss[d],off,&rows[count]))
    return fp_init_fail("LUT",d,fp_epss_pa[d],off,rows[count],0,(int)count);
   if (count && fp_khz(rows[count]) == fp_khz(rows[count-1])) break;
  }
  unsigned cool_khz, high_khz;
  int cool = fp_level(rows,count,fp_cool_cap[d],&cool_khz);
  /* Cap boost at 2.4 GHz using eligible multi-core LUT rows.
   * Hardware owns CPR/LMh.
   * Silver stays cool: raising its shared rail also raises the monitor core. */
  int high = fp_level(rows,count,d && fp_cap_mhz ? fp_cap_mhz*1000 : fp_high_cap[d],&high_khz);
  if (cool < 0 || high < 0)
   return fp_init_fail("level selection",d,fp_epss_pa[d],0x100+4*(count-1),rows[count-1],1,(int)count-1);
  if (fp_idle_after) {
   int idle = fp_idle_level(rows,count,fp_idle_floor,cool_khz,&idle_freq[d]);
   if (idle < 0)
    return fp_init_fail("idle selection",d,fp_epss_pa[d],0x100,rows[0],1,0);
   idle_level[d] = (unsigned)idle;
  } else { idle_level[d] = (unsigned)cool; idle_freq[d] = cool_khz; }
  cool_level[d] = (unsigned)cool;
  high_level[d] = d ? (unsigned)high : (unsigned)cool;
  cool_freq[d] = cool_khz;
  high_freq[d] = d ? high_khz : cool_khz;
  /* fpk: never below cool, never above high (fpk is bounded by the cap). */
  int base = d && fp_base_mhz ? fp_level(rows,count,fp_base_mhz*1000,&base_freq[d]) : -1;
  if (base < 0 || base_freq[d] <= cool_khz) { base = cool; base_freq[d] = cool_khz; }
  base_level[d] = (unsigned)base;
  /* fsf: never above high, never on silver; no faster row means no floor. */
  int flr = d && fp_floor_pct ?
      fp_floor_level(rows,count,fp_floor_pct,cool_khz,high_khz,&floor_freq[d]) : -1;
  floor_level[d] = flr < 0 ? (unsigned)cool : (unsigned)flr;
  if (flr < 0) floor_freq[d] = cool_khz;
 }
 for (unsigned d = 0; d < 2; d++) {
  if (!fp_tsens[d]) fp_tsens[d] = ml_io_map(fp_tsens_pa[d],0x1000);
  if (!fp_tsens[d]) return fp_init_fail("tsens map",d,fp_tsens_pa[d],0,0,0,-1);
 }
 /* Commit policy only when every domain and sensor mapping passed. Failed
  * attempts are read-only, including domains discovered on earlier tries. */
 for (unsigned d = 0; d < 3; d++) {
  fp_idle[d] = idle_level[d]; fp_idle_khz[d] = idle_freq[d];
  fp_percore[d] = percore[d];
  fp_cool[d] = cool_level[d]; fp_high[d] = high_level[d];
  fp_cool_khz[d] = cool_freq[d]; fp_high_khz[d] = high_freq[d];
  fp_base[d] = base_level[d]; fp_base_khz[d] = base_freq[d];
  fp_floor[d] = floor_level[d]; fp_floor_khz[d] = floor_freq[d];
 }
 return 1;
}
/* fl3: once at ready, read-only. Any unreadable register, a clear enable or
 * a boot vote that does not name the running row leaves L3 untouched. */
static void fp_l3_init(void)
{
 uint32_t enable = UINT32_MAX, state = UINT32_MAX, vote = UINT32_MAX, rows[40];
 unsigned count, khz;
 if (!fp_l3) fp_l3 = ml_io_map(fp_l3_pa,0x1000);
 if (!fp_l3) { printf("FUXI PERF: L3 off: map %#llx failed\n",fp_l3_pa); return; }
 if (!fp_read(fp_l3,0,&enable) || !fp_read(fp_l3,0x20,&state) || !fp_read(fp_l3,0x90,&vote)) {
  printf("FUXI PERF: L3 off: unreadable (enable %#x state %#x vote %#x), no write\n",enable,state,vote);
  return;
 }
 for (count = 0; count < 40; count++) {
  rows[count] = UINT32_MAX;
  if (!fp_read(fp_l3,0x100+4*count,&rows[count])) {
   printf("FUXI PERF: L3 off: LUT row %u unreadable, no write\n",count);
   return;
  }
  if (count && fp_khz(rows[count]) == fp_khz(rows[count-1])) break;
 }
 int high = fp_l3_check(rows,count,enable,vote,state,&khz);
 if (high < 0) {
  printf("FUXI PERF: L3 off: enable %#x vote %u state %#x do not match the %u-row LUT, no write\n",
      enable,vote,state,count);
  return;
 }
 fp_l3_boot = fp_l3_voted = vote;
 fp_l3_boot_khz = fp_khz(rows[vote]);
 fp_l3_high = (unsigned)high;
 fp_l3_high_khz = khz;
 fp_l3_ready = 1;
 printf("FUXI PERF: L3 vote ready: boot row %u (%u kHz) state %#x, boost row %u (%u kHz, word %#x) of %u; writes +0x90 only\n",
     vote,fp_l3_boot_khz,state,fp_l3_high,khz,rows[high],count);
}
/* Called by the worker after the CPU votes. A lost enable, a write fault or a
 * readback mismatch restores the boot vote once and ends L3 voting. */
static void fp_l3_update(int high)
{
 if (!fp_l3_ready) return;
 unsigned want = high ? fp_l3_high : fp_l3_boot;
 if (want == fp_l3_voted) return;
 uint32_t enable, got = UINT32_MAX;
 if (!fp_read(fp_l3,0,&enable) || !(enable & 1)) {
  fp_l3_ready = 0;
  printf("FUXI PERF: L3 off: enable %#x lost before voting row %u, no write\n",enable,want);
  return;
 }
 uint64_t v = want;
 if (fuxi_gpu_access(fp_l3+0x90,&v,1,0) || !fp_read(fp_l3,0x90,&got) || got != want) {
  uint64_t boot = fp_l3_boot;
  (void)fuxi_gpu_access(fp_l3+0x90,&boot,1,0);
  fp_l3_ready = 0;
  printf("FUXI PERF: L3 off: row %u read back %#x, boot row %u rewritten\n",want,got,fp_l3_boot);
  return;
 }
 fp_l3_voted = want;
 if (high) { fp_l3_votes++; return; }
 /* Once: the state +0x20 sampled while the boost row was voted proves it ran. */
 if (!fp_l3_said++)
  printf("FUXI PERF: L3 first boost: row %u voted and read back, state L max %u (%u kHz) over %llu samples; boot row %u restored\n",
      fp_l3_high,fp_l3_state_max,fp_l3_state_max*19200,fp_l3_samples,fp_l3_boot);
}
/* Timestamp gate also applies to input-triggered calls, so input cannot
 * accelerate retries or extend the fixed 60-second initialization window. */
static int fp_try_initialize(uint64_t now)
{
 if (fp_attempted && now < fp_retry_at) return 0;
 if (fp_attempted && now > fp_retry_end) goto disabled;
 if (fp_attempted) fp_retries++;
 else fp_retry_end = now + 60*fp_retry_interval;
 if (fp_initialize()) {
  fp_ready = 1;
  fp_idle_start = now;
  fp_apply_cores();
  printf("FUXI PERF: ready, LUT cool %u/%u/%u kHz high %u/%u/%u kHz, hold 1500 ms after %u retries\n",
      fp_cool_khz[0],fp_cool_khz[1],fp_cool_khz[2],fp_high_khz[0],fp_high_khz[1],fp_high_khz[2],fp_retries);
  if (fp_idle_after)
   printf("FUXI PERF: ready idle LUT %u/%u/%u kHz after %u ms floor %u kHz (clamped to cool)\n",
       fp_idle_khz[0],fp_idle_khz[1],fp_idle_khz[2],fp_idle_ms,fp_idle_floor);
  if (fp_base_mhz)
   printf("FUXI PERF: ready base fpk=%u: gold/prime %u/%u kHz while thermally safe, else compiled cool %u/%u kHz\n",
       fp_base_mhz,fp_base_khz[1],fp_base_khz[2],fp_cool_khz[1],fp_cool_khz[2]);
  if (fp_launch_hold || fp_floor_pct)
   printf("FUXI PERF: ready policy launch %u ms high %u/%u kHz (silver cool), floor %u%% %u/%u kHz on touch %u frames %u ms (0 = off, cool %u/%u)\n",
       fp_launch_ms,fp_high_khz[1],fp_high_khz[2],fp_floor_pct,fp_floor_khz[1],fp_floor_khz[2],
       fp_floor_touch,fp_frame_ms,fp_cool_khz[1],fp_cool_khz[2]);
  if (fp_l3_enabled) fp_l3_init();
  return 1;
 }
 if (!fp_attempted) fp_init_report("init retry");
 fp_attempted = 1;
 if (now >= fp_retry_end || fp_retries == 60) goto disabled;
 fp_retry_at = now + fp_retry_interval;
 if (fp_retry_at > fp_retry_end) fp_retry_at = fp_retry_end;
 return 0;
disabled:
 fp_init_report("disabled");
 __atomic_store_n(&fp_enabled,0,__ATOMIC_RELEASE);
 return 0;
}
static void fp_update_votes(int state)
{
 for (unsigned d = 0; d < 3; d++) {
  unsigned failed = fp_domain_failed[d];
  uint32_t enable;
  /* Check even if the desired vote did not change. A recovered domain may
   * receive idle/cool, but never resumes boost during this boot. */
  if (!fp_read(fp_epss[d],0,&enable) || !(enable & 1)) {
   fp_domain_failed[d] = 1;
   fp_applied_valid[d] = 0;
  } else {
   int compile = fp_compile_high && fp_compile_domain(d);
   /* Cool is the fpk base when allowed; a floor applies only above it. */
   int raised = fp_base_on && !failed;
   unsigned base = raised ? fp_base[d] : fp_cool[d], base_khz = raised ? fp_base_khz[d] : fp_cool_khz[d];
   unsigned level = (state == FP_HIGH || compile || fp_launch_high) && !failed ? fp_high[d] :
       state == FP_IDLE ? fp_idle[d] :
       fp_floor_on && !failed && fp_floor_khz[d] > base_khz ? fp_floor[d] : base;
   if (!fp_applied_valid[d] || fp_applied[d] != level) {
    if (fp_vote(d,level)) {
     fp_applied[d] = fp_domain_failed[d] && state != FP_IDLE ? fp_cool[d] : level;
     fp_applied_valid[d] = 1;
    } else {
     fp_domain_failed[d] = 1;
     fp_applied_valid[d] = 0;
     /* A write/readback error may have left a high vote. fp_vote rechecks
      * enable before each attempted idle/cool write, including fallback. */
     (void)fp_vote(d,state == FP_IDLE ? fp_idle[d] : fp_cool[d]);
    }
   }
  }
  if (!failed && fp_domain_failed[d])
   printf("FUXI PERF: domain=%s(%u) enable/vote failed, boosting disabled for domain, cool when enabled\n",fp_domain[d],d);
 }
}
/* Exact rolling-window accounting, including a partially expired burst.
 * Retain completed intervals, not samples: frequent input cannot exhaust the
 * history. If all slots are occupied, deny entry until one expires. */
static uint64_t fp_duty_used(uint64_t now)
{
 uint64_t edge = now > fp_duty_window ? now-fp_duty_window : 0;
 unsigned keep = 0;
 uint64_t used = 0;
 for (unsigned i = 0; i < fp_duty_count; i++) {
  if (fp_duty_end[i] <= edge) continue;
  fp_duty_begin[keep] = fp_duty_begin[i];
  fp_duty_end[keep] = fp_duty_end[i];
  uint64_t begin = fp_duty_begin[i] > edge ? fp_duty_begin[i] : edge;
  used += fp_duty_end[i]-begin;
  keep++;
 }
 fp_duty_count = keep;
 if (fp_boosted) {
  uint64_t begin = fp_active_start > edge ? fp_active_start : edge;
  used += now-begin;
 }
 return used;
}
static int fp_thermal_guard(int valid, int temp, uint64_t now, uint64_t elapsed_ns)
{
 /* Aggregate at least one fast period before updating slope. Touch callbacks
  * can arrive much sooner than a TSENS conversion; differentiating each one
  * would turn a quantized sensor update into a false extreme rise rate. */
 if (!valid) {
  fp_rate_valid = fp_rate = 0;
  fp_rate_elapsed = 0;
 } else if (!fp_rate_valid) {
  fp_rate_temp = temp;
  fp_rate_elapsed = 0;
  fp_rate_valid = 1;
  fp_rate = 0;
 } else {
  fp_rate_elapsed += elapsed_ns;
  if (fp_rate_elapsed >= (uint64_t)fp_rate_ms*NSEC_PER_MSEC) {
   uint64_t slope = temp > fp_rate_temp ?
       (uint64_t)(temp-fp_rate_temp)*NSEC_PER_SEC/fp_rate_elapsed : 0;
   fp_rate = slope > 1000000 ? 1000000 : (int)slope;
   fp_rate_temp = temp;
   fp_rate_elapsed = 0;
  }
 }
 int rate = fp_rate;
 /* Predict 100 ms ahead to cover timer/thermal lag. A fast rise above
  * 55 C also cuts before reaching the soft threshold. Invalid quorum
  * latches cool; no derivative is taken across a missing sample. */
 int trip = !valid || temp >= (int)fp_hot || temp >= (int)fp_limit ||
     (valid && temp+rate/10 >= (int)fp_limit) ||
     (valid && temp >= (int)fp_hot-70 && rate >= (int)fp_rise);
 if (trip) {
  fp_blocked = 1;
  fp_recover_since = 0;
 } else if (fp_blocked) {
  if (temp <= (int)fp_resume && rate < (int)fp_rise) {
   if (!fp_recover_since) fp_recover_since = now;
   if (now-fp_recover_since >= fp_recovery) fp_blocked = 0;
  } else fp_recover_since = 0;
 }
 fp_prev_temp = temp;
 fp_prev_valid = valid;
 return !fp_blocked;
}
/* Reject out-of-range boot args rather than wrapping or disabling a guard. */
static void fp_arg(const char *name, unsigned *value, unsigned low, unsigned high)
{
 unsigned candidate = *value;
 if (PE_parse_boot_argn(name,&candidate,sizeof(candidate)) &&
     candidate >= low && candidate <= high) *value = candidate;
}
static void fp_configure(void)
{
 fp_arg("fuxiperfidle",&fp_idle_ms,1000,60000);
 fp_arg("fuxiperfidlekhz",&fp_idle_floor,100000,3000000);
 nanoseconds_to_absolutetime((uint64_t)fp_idle_ms*NSEC_PER_MSEC,&fp_idle_after);
 fp_arg("fuxiperflimit",&fp_limit,450,800);
 fp_boost_max = fp_limit;
 fp_arg("fuxiperfhot",&fp_hot,400,fp_limit);
 if (fp_hot > fp_limit) fp_hot = fp_limit;
 fp_arg("fuxiperfresume",&fp_resume,350,fp_hot-10);
 if (fp_resume > fp_hot-10) fp_resume = fp_hot-10;
 fp_arg("fuxiperfstart",&fp_start,400,fp_limit);
 fp_arg("fuxiperfrise",&fp_rise,10,1000);
 fp_arg("fuxiperffast",&fp_fast_ms,5,25);
 fp_arg("fuxiperfrate",&fp_rate_ms,fp_fast_ms,1000);
 fp_arg("fuxiperfbudget",&fp_budget_ms,20,500);
 /* Short names for a full boot line: fpb=MS boost budget per second, fpc=MHz gold/prime boost cap.
  * The thermal hot, limit and slope guards are unchanged. */
 fp_arg("fpb",&fp_budget_ms,20,1000);
 fp_arg("fpc",&fp_cap_mhz,1402,3200);
 /* fpk=MHz raised gold/prime base, 1401 up to the boost cap (fpc or 2400). */
 fp_arg("fpk",&fp_base_mhz,1401,fp_cap_mhz ? fp_cap_mhz : fp_high_cap[1]/1000);
 /* fla=MS launch boost (stock 2000), fsf=PCT touch floor (sheng 60),
  * fsw=MS frame floor at fsf's PCT or 60. */
 fp_arg("fla",&fp_launch_ms,500,3000);
 fp_arg("fsf",&fp_fsf,30,90);
 fp_arg("fsw",&fp_frame_ms,100,1000);
 fp_floor_touch = fp_fsf != 0;
 fp_floor_pct = fp_fsf ? fp_fsf : fp_frame_ms ? 60 : 0;
 nanoseconds_to_absolutetime((uint64_t)fp_launch_ms*NSEC_PER_MSEC,&fp_launch_hold);
 nanoseconds_to_absolutetime((uint64_t)fp_frame_ms*NSEC_PER_MSEC,&fp_frame_hold);
 nanoseconds_to_absolutetime((uint64_t)fp_budget_ms*NSEC_PER_MSEC,&fp_budget);
 nanoseconds_to_absolutetime(NSEC_PER_SEC,&fp_duty_window);
 nanoseconds_to_absolutetime(500ULL*NSEC_PER_MSEC,&fp_recovery);
 printf("FUXI PERF: thermal hot %u limit %u resume %u start %u dC rise %u dC/s fast %u ms slope %u ms duty %u/1000 ms recovery 500 ms predict 100 ms\n",
     fp_hot,fp_limit,fp_resume,fp_start,fp_rise,fp_fast_ms,fp_rate_ms,fp_budget_ms);
}
/* fla: one worker sample of the launch window. A new exec that cannot start
 * (start, hot, limit or slope guard) counts as refused. The first 8 ends and
 * refusals are logged with the reason and the hottest CPU sensor. */
static void fp_launch_update(int valid, int temp, int safe, uint64_t now)
{
 uint64_t launch = __atomic_load_n(&fp_launch,__ATOMIC_ACQUIRE);
 int fresh = launch != fp_launch_last;
 int high = fp_want_launch(fp_launch_high,valid,temp,safe,now,launch,fp_launch_hold);
 fp_launch_last = launch;
 if (high && !fp_launch_high) { fp_launches++; fp_launch_start = now; }
 if (fresh && !high) fp_launch_refused++;
 if (fp_launch_high && !high && fp_launch_said < 8) {
  uint64_t ns;
  absolutetime_to_nanoseconds(now-fp_launch_start,&ns);
  fp_launch_said++;
  printf("FUXI PERF: launch boost %llu ms, ended by %s at %d dC\n",ns/1000000,
      valid && safe && temp <= (int)fp_boost_max ? "hold" : "thermal",temp);
 } else if (fresh && !high && !fp_launch_high && fp_launch_said < 8) {
  fp_launch_said++;
  printf("FUXI PERF: launch boost refused at %d dC (valid %d blocked %d start %u)\n",
      temp,valid,fp_blocked,fp_start);
 }
 fp_launch_high = high;
}
static void fp_worker_locked(thread_call_param_t arg, thread_call_param_t result)
{
 (void)arg; (void)result;
 if (!__atomic_load_n(&fp_enabled,__ATOMIC_ACQUIRE)) return;
 if (!fp_ready) {
  if (!fp_try_initialize(mach_absolute_time())) {
   if (__atomic_load_n(&fp_enabled,__ATOMIC_ACQUIRE))
    (void)thread_call_enter_delayed(&fp_call,fp_retry_at);
   return;
  }
 }
 uint64_t now = mach_absolute_time();
 uint64_t elapsed = fp_last ? now-fp_last : 0, elapsed_ns;
 absolutetime_to_nanoseconds(elapsed,&elapsed_ns);
 if (fp_boosted) fp_boost_time += elapsed;
 if (fp_base_on) fp_base_time += elapsed;
 else if (fp_base_mhz) fp_base_fallback_time += elapsed;
 if (fp_launch_high) fp_launch_time += elapsed;
 if (fp_floor_on && fp_floor_frames) fp_frame_time += elapsed;
 else if (fp_floor_on) fp_floor_time += elapsed;
 unsigned was_idle = __atomic_load_n(&fp_idling,__ATOMIC_RELAXED);
 if (was_idle) fp_idle_time += elapsed;
 int temp = -400;
 int valid = fp_temperature(&temp);
 if (valid && temp > fp_max_temp) fp_max_temp = temp;
 /* Charge a threshold-crossing interval conservatively to either endpoint.
  * This is sampled exposure, not an interpolated sensor measurement. */
 if ((valid && temp >= (int)fp_hot) ||
     (fp_prev_valid && fp_prev_temp >= (int)fp_hot)) fp_above_time += elapsed;
 int safe = fp_thermal_guard(valid,temp,now,elapsed_ns);
 /* fpk: the raised base needs a valid sample and no thermal latch (hot,
  * limit, predicted limit, slope; 500 ms at/below resume to clear). */
 fp_base_on = fp_base_mhz && valid && safe;
 if (fp_pressure_enabled)
  fp_pressure = fp_pressure_hold(fp_pressure,
      fp_pressure_level(fp_pressure,valid,temp,fp_blocked,fp_hot,fp_limit,fp_resume),
      now,&fp_pressure_since,fp_pressure_dwell);
 uint64_t used = fp_duty_used(now);
 int duty_ok = used < fp_budget && (fp_boosted || fp_duty_count < 16);
 fp_compile_poll(now,safe,duty_ok);
 int state = fp_state(fp_touch_high,valid,temp,safe,duty_ok,now,
     __atomic_load_n(&fp_input,__ATOMIC_ACQUIRE),
     __atomic_load_n(&fp_swap,__ATOMIC_ACQUIRE),fp_hold,
     fp_idle_after,fp_idle_start,fp_idle_entry);
 fp_touch_high = state == FP_HIGH;
 fp_launch_update(valid,temp,safe,now);
 if ((fp_compile_high || fp_launch_high) && state == FP_IDLE) state = FP_COOL;
 /* fsf/fsw raise only a cool vote: high is higher, idle (fuxiperfidle) lower.
  * The time counts as touch whenever an input also asks for the floor. */
 int touch_floor = fp_floor_touch &&
     fp_want_floor(valid,temp,safe,now,__atomic_load_n(&fp_input,__ATOMIC_ACQUIRE),fp_hold);
 int frame_floor = fp_frame_hold &&
     fp_want_floor(valid,temp,safe,now,__atomic_load_n(&fp_swap_prev,__ATOMIC_ACQUIRE),fp_frame_hold);
 fp_floor_on = fp_floor_pct && state == FP_COOL && !fp_launch_high && (touch_floor || frame_floor);
 fp_floor_frames = fp_floor_on && !touch_floor;
 /* boost is the touch-duty-accounted vote; a launch is outside the duty. */
 int boost = fp_touch_high || fp_compile_high;
 /* First sample also establishes cool votes. No boost without fresh TSENS. */
 fp_update_votes(state);
 if (fp_compile_high && fp_compile_failed()) {
  fp_compile_cut(FC_OFFLINE,now);
  boost = fp_touch_high;
  fp_update_votes(state);
 }
 if (fp_compile_high) fp_compile_voted = 1;
 fp_l3_update(boost || fp_launch_high);
 unsigned idle = state == FP_IDLE;
 if (idle != was_idle) {
  if (idle) { fp_idle_entries++; fp_idle_entry = now; }
  fp_idle_transitions++;
  if (fp_idle_transitions <= 8 || !(fp_idle_transitions & 63))
   printf("FUXI PERF: idle %s state %s transition %llu\n",
       idle ? "enter" : "leave",idle ? "idle" : boost ? "high" : "cool",fp_idle_transitions);
 }
 __atomic_store_n(&fp_idling,idle,__ATOMIC_RELEASE);
 if (boost && !fp_boosted) {
  fp_boosts++;
  fp_active_start = now;
 }
 if (!boost && fp_boosted) {
  if (!safe || !duty_ok) fp_cuts++;
  /* Entry reserves a slot; completed history is never silently discarded. */
  fp_duty_begin[fp_duty_count] = fp_active_start;
  fp_duty_end[fp_duty_count++] = now;
 }
 fp_boosted = boost;
 for (unsigned d = 1; d < 3; d++) {
  uint32_t cur;
  if (!fp_epss[d] || fp_domain_failed[d] || !fp_read(fp_epss[d],0x20,&cur)) continue;
  unsigned mhz = (cur & 0xff)*192/10, b = boost || fp_launch_high ? 0 : 1;
  fp_cur_sum[b][d-1] += mhz;
  if (d == 1) fp_cur_n[b]++;
  if (mhz > fp_cur_max[b][d-1]) fp_cur_max[b][d-1] = mhz;
 }
 if (fp_l3_ready && fp_l3_voted == fp_l3_high && fp_l3_high != fp_l3_boot) {
  uint32_t cur;
  if (fp_read(fp_l3,0x20,&cur)) {
   fp_l3_samples++;
   if ((cur & 255) > fp_l3_state_max) fp_l3_state_max = cur & 255;
  }
 }
 fp_last = now;
 if (!fp_window_start) fp_window_start = now;
 if (now-fp_window_start >= fp_window) {
  uint64_t ns, above_ns;
  absolutetime_to_nanoseconds(fp_boost_time,&ns);
  absolutetime_to_nanoseconds(fp_above_time,&above_ns);
  for (unsigned b = 0; b < 2; b++) if (fp_cur_n[b])
   printf("FUXI PERF: freq %s gold %llu/%u prime %llu/%u MHz avg/max samples %llu\n",
       b ? "unboosted" : "boosted",fp_cur_sum[b][0]/fp_cur_n[b],fp_cur_max[b][0],
       fp_cur_sum[b][1]/fp_cur_n[b],fp_cur_max[b][1],fp_cur_n[b]);
  if (!fp_idle_after) printf("FUXI PERF: stats boosts %llu boosted %llu ms max temp %d dC above %u dC %llu ms cuts %llu valid %d state %s\n",
      fp_boosts,ns/1000000,fp_max_temp,fp_hot,above_ns/1000000,fp_cuts,
      valid,fp_boosted ? "high" : "cool");
  else {
   uint64_t idle_ns;
   absolutetime_to_nanoseconds(fp_idle_time,&idle_ns);
   printf("FUXI PERF: stats boosts %llu boosted %llu ms max temp %d dC above %u dC %llu ms cuts %llu valid %d state %s idle %llu ms entries %llu\n",
       fp_boosts,ns/1000000,fp_max_temp,fp_hot,above_ns/1000000,fp_cuts,
       valid,idle ? "idle" : fp_boosted ? "high" : "cool",idle_ns/1000000,fp_idle_entries);
  }
  if (fp_base_mhz) {
   uint64_t base_ns, fallback_ns;
   absolutetime_to_nanoseconds(fp_base_time,&base_ns);
   absolutetime_to_nanoseconds(fp_base_fallback_time,&fallback_ns);
   printf("FUXI PERF: base fpk=%u raised %llu ms compiled %llu ms (hot, latched or no valid sample)\n",
       fp_base_mhz,base_ns/1000000,fallback_ns/1000000);
  }
  fp_base_time = fp_base_fallback_time = 0;
  if (fp_launch_hold || fp_floor_pct) {
   /* By source: touch boosts (and compile leases) are the stats line's;
    * launch and the floor run outside the touch duty. */
   uint64_t launch_ns, floor_ns, frame_ns;
   absolutetime_to_nanoseconds(fp_launch_time,&launch_ns);
   absolutetime_to_nanoseconds(fp_floor_time,&floor_ns);
   absolutetime_to_nanoseconds(fp_frame_time,&frame_ns);
   printf("FUXI PERF: policy touch boosts %llu %llu ms, launches %llu %llu ms refused %llu, floor touch %llu ms frames %llu ms, L3 votes %llu\n",
       fp_boosts,ns/1000000,fp_launches,launch_ns/1000000,fp_launch_refused,
       floor_ns/1000000,frame_ns/1000000,fp_l3_votes);
  }
  fp_frame_time = 0;
  fp_launches = fp_launch_time = fp_launch_refused = fp_floor_time = fp_l3_votes = 0;
  for (unsigned b = 0; b < 2; b++) {
   fp_cur_n[b] = 0;
   fp_cur_sum[b][0] = fp_cur_sum[b][1] = 0;
   fp_cur_max[b][0] = fp_cur_max[b][1] = 0;
  }
  fp_idle_time = fp_idle_entries = 0;
  fp_boosts = fp_boost_time = fp_above_time = fp_cuts = 0;
  fp_max_temp = -400;
  fp_window_start = now;
 }
 /* A raised base under load samples as fast as a boost; idle stays at 100 ms. */
 uint64_t activity = __atomic_load_n(&fp_input,__ATOMIC_ACQUIRE);
 uint64_t swapped = __atomic_load_n(&fp_swap,__ATOMIC_ACQUIRE);
 if (swapped > activity) activity = swapped;
 int active = activity && now >= activity && now-activity <= fp_hold;
 uint64_t deadline;
 clock_interval_to_deadline(fp_boosted || fp_launch_high || fp_floor_on || fp_blocked ||
     (fp_base_on && active) ? fp_fast_ms : 100,NSEC_PER_MSEC,&deadline);
 /* Do not overrun a fractional remaining duty budget by a full sample. */
 if (fp_boosted && fp_budget-used < deadline-now) deadline = now+fp_budget-used;
 if (fp_compile_thread != THREAD_NULL && fp_compile_deadline < deadline)
  deadline = fp_compile_deadline;
 (void)thread_call_enter_delayed(&fp_call,deadline);
}
static void fp_worker(thread_call_param_t arg, thread_call_param_t result)
{
 lck_mtx_lock(&fp_lock);
 fp_worker_locked(arg,result);
 lck_mtx_unlock(&fp_lock);
}
/* fwi=1 (default off, independent of fuxiperf): a census of work_interval_ctl
 * (bsd/kern/sys_work_interval.c hooks in 0097), diagnostic only. Each call is
 * counted by caller process and operation; CREATE2 and NOTIFY also record the
 * interval type, NOTIFY whether it carries a deadline (deadline > start), the
 * mean deadline-start and finish-start and how many finished late. A 60 s
 * thread call prints and clears it; backboardd keeps row 0. Arguments,
 * results and scheduling are unchanged. Ops 6/7/8 are the os_workgroup
 * instance start/update/finish (UniXNU 8b6a0f5); xnu-8796 refuses them with
 * ENOTSUP before reading their argument, so only their count is known.
 * types: bit N for WORK_INTERVAL_TYPE N<<28 (0x1 default, 0x2 CoreAudio,
 * 0x4 CA render server, 0x8 CA client, 0x10 HID delivery, 0x20 CoreMedia,
 * 0x40 ARKit, 0x80 frame compositor). */
#define FWI_PROCS 24
#define FWI_OPS 12
static const char * const fwi_op[FWI_OPS] = {"other","create","destroy","notify","create2",
    "join","start","update","finish","flags","name","wlid"};
struct fwi_proc {
 char name[MAXCOMLEN+1];
 uint32_t ops[FWI_OPS], types, deadlines, missed;
 uint64_t budget, used;
};
static struct fwi_proc fwi_table[FWI_PROCS], fwi_copy[FWI_PROCS];
static uint32_t fwi_dropped;
static unsigned fwi_on, fwi_started;
static uint64_t fwi_window_start;
static thread_call_data_t fwi_call;
static LCK_MTX_DECLARE(fwi_lock, &fp_group);
static void fwi_clear_locked(void)
{
 memset(fwi_table,0,sizeof(fwi_table));
 snprintf(fwi_table[0].name,sizeof(fwi_table[0].name),"%s","backboardd");
 fwi_dropped = 0;
}
/* The caller's row, or -1 with the table full (counted as dropped). */
static int fwi_row_locked(const char *name)
{
 for (unsigned i = 0; i < FWI_PROCS; i++) {
  if (!fwi_table[i].name[0]) {
   snprintf(fwi_table[i].name,sizeof(fwi_table[i].name),"%s",name);
   return (int)i;
  }
  if (!strcmp(fwi_table[i].name,name)) return (int)i;
 }
 fwi_dropped++;
 return -1;
}
static void fwi_self(char *name, int size)
{
 proc_selfname(name,size);
 if (!name[0]) snprintf(name,(size_t)size,"%s","?");
}
void fuxi_wi_census(uint32_t operation)
{
 if (!__atomic_load_n(&fwi_on,__ATOMIC_ACQUIRE)) return;
 char name[MAXCOMLEN+1] = {};
 fwi_self(name,sizeof(name));
 lck_mtx_lock(&fwi_lock);
 int row = fwi_row_locked(name);
 if (row >= 0) fwi_table[row].ops[operation < FWI_OPS ? operation : 0]++;
 lck_mtx_unlock(&fwi_lock);
}
void fuxi_wi_census_flags(uint32_t create_flags)
{
 if (!__atomic_load_n(&fwi_on,__ATOMIC_ACQUIRE)) return;
 char name[MAXCOMLEN+1] = {};
 fwi_self(name,sizeof(name));
 lck_mtx_lock(&fwi_lock);
 int row = fwi_row_locked(name);
 if (row >= 0) fwi_table[row].types |= 1u << (create_flags >> 28);
 lck_mtx_unlock(&fwi_lock);
}
void fuxi_wi_census_notify(uint32_t create_flags, uint64_t start, uint64_t finish, uint64_t deadline)
{
 if (!__atomic_load_n(&fwi_on,__ATOMIC_ACQUIRE)) return;
 char name[MAXCOMLEN+1] = {};
 fwi_self(name,sizeof(name));
 lck_mtx_lock(&fwi_lock);
 int row = fwi_row_locked(name);
 if (row >= 0) {
  struct fwi_proc *p = &fwi_table[row];
  p->types |= 1u << (create_flags >> 28);
  if (deadline > start) {
   p->deadlines++;
   p->budget += deadline-start;
   if (finish > start) p->used += finish-start;
   if (finish > deadline) p->missed++;
  }
 }
 lck_mtx_unlock(&fwi_lock);
}
static unsigned long long fwi_us(uint64_t sum, uint32_t n)
{
 uint64_t ns;
 absolutetime_to_nanoseconds(sum,&ns);
 return n ? ns/n/1000 : 0;
}
static void fwi_report(thread_call_param_t arg, thread_call_param_t result)
{
 (void)arg; (void)result;
 lck_mtx_lock(&fwi_lock);
 memcpy(fwi_copy,fwi_table,sizeof(fwi_copy));
 uint32_t dropped = fwi_dropped;
 fwi_clear_locked();
 lck_mtx_unlock(&fwi_lock);
 uint64_t now = mach_absolute_time(), span_ns, calls = 0;
 unsigned procs = 0;
 absolutetime_to_nanoseconds(now-fwi_window_start,&span_ns);
 fwi_window_start = now;
 for (unsigned i = 0; i < FWI_PROCS; i++) {
  uint64_t n = 0;
  for (unsigned o = 0; o < FWI_OPS; o++) n += fwi_copy[i].ops[o];
  procs += n != 0;
  calls += n;
 }
 printf("FUXI WI: census %llu ms: %llu calls from %u processes, %u dropped (table %u)\n",
     span_ns/1000000,calls,procs,dropped,FWI_PROCS);
 for (unsigned i = 0; i < FWI_PROCS; i++) {
  struct fwi_proc *p = &fwi_copy[i];
  char line[320];
  size_t k = (size_t)snprintf(line,sizeof(line),"FUXI WI: %s",p->name);
  int any = 0;
  for (unsigned o = 0; o < FWI_OPS; o++) if (p->ops[o] && k < sizeof(line)) {
   k += (size_t)snprintf(line+k,sizeof(line)-k," %s %u",fwi_op[o],p->ops[o]);
   any = 1;
  }
  if (!any && i) continue; /* backboardd prints every window, even idle */
  if (!any && k < sizeof(line)) k += (size_t)snprintf(line+k,sizeof(line)-k," none");
  if (p->types && k < sizeof(line)) k += (size_t)snprintf(line+k,sizeof(line)-k," types %#x",p->types);
  if (p->deadlines && k < sizeof(line))
   k += (size_t)snprintf(line+k,sizeof(line)-k," deadline %u budget_us %llu used_us %llu missed %u",
       p->deadlines,fwi_us(p->budget,p->deadlines),fwi_us(p->used,p->deadlines),p->missed);
  printf("%s\n",line);
 }
 uint64_t deadline;
 clock_interval_to_deadline(60,NSEC_PER_SEC,&deadline);
 (void)thread_call_enter_delayed(&fwi_call,deadline);
}
static void fp_wi_start(void)
{
 unsigned on = 0;
 if (fwi_started || !PE_parse_boot_argn("fwi",&on,sizeof(on)) || on != 1) return;
 fwi_started = 1;
 lck_mtx_lock(&fwi_lock);
 fwi_clear_locked();
 lck_mtx_unlock(&fwi_lock);
 thread_call_setup_with_options(&fwi_call,fwi_report,NULL,
     THREAD_CALL_PRIORITY_LOW,THREAD_CALL_OPTIONS_ONCE);
 fwi_window_start = mach_absolute_time();
 uint64_t deadline;
 clock_interval_to_deadline(60,NSEC_PER_SEC,&deadline);
 (void)thread_call_enter_delayed(&fwi_call,deadline);
 __atomic_store_n(&fwi_on,1,__ATOMIC_RELEASE);
 printf("FUXI WI: census on (fwi=1): work_interval_ctl by process and op every 60 s; calls and scheduling unchanged\n");
}
void fuxi_perf_start(void)
{
 fp_wi_start();
 unsigned enable = 0;
 (void)PE_parse_boot_argn("fuxiperf",&enable,sizeof(enable));
 if (enable != 1 || fp_started) return;
 fp_started = 1;
 (void)PE_parse_boot_argn("fuxitp",&fp_pressure_enabled,sizeof(fp_pressure_enabled));
 fp_pressure_enabled = fp_pressure_enabled == 1;
 nanoseconds_to_absolutetime(10*NSEC_PER_SEC,&fp_pressure_dwell);
 (void)PE_parse_boot_argn("fuxicompileboost",&fp_compile_enabled,sizeof(fp_compile_enabled));
 fp_compile_enabled = fp_compile_enabled == 1;
 (void)PE_parse_boot_argn("fl3",&fp_l3_enabled,sizeof(fp_l3_enabled));
 fp_l3_enabled = fp_l3_enabled == 1;
 /* Keep at least two of logical 2-6 (gold/prime); otherwise ignore. */
 if (!PE_parse_boot_argn("fuxicores",&fp_recommend,sizeof(fp_recommend)) ||
     (fp_recommend & ~0x7fu) || __builtin_popcount(fp_recommend & 0x7cu) < 2) fp_recommend = 0;
 nanoseconds_to_absolutetime(2000ULL*NSEC_PER_MSEC,&fp_compile_cap);
 fp_configure();
 nanoseconds_to_absolutetime(1500ULL*NSEC_PER_MSEC,&fp_hold);
 nanoseconds_to_absolutetime(10ULL*NSEC_PER_SEC,&fp_window);
 nanoseconds_to_absolutetime(NSEC_PER_SEC,&fp_retry_interval);
 fp_max_temp = -400;
 thread_call_setup_with_options(&fp_call,fp_worker,NULL,
     THREAD_CALL_PRIORITY_KERNEL,THREAD_CALL_OPTIONS_ONCE);
 __atomic_store_n(&fp_enabled,1,__ATOMIC_RELEASE);
 (void)thread_call_enter(&fp_call);
}
void fuxi_perf_input(void)
{
 if (!__atomic_load_n(&fp_enabled,__ATOMIC_ACQUIRE)) return;
 __atomic_store_n(&fp_input,mach_absolute_time(),__ATOMIC_RELEASE);
 (void)thread_call_enter(&fp_call);
}
void fuxi_perf_swap(void)
{
 if (__atomic_load_n(&fp_enabled,__ATOMIC_ACQUIRE)) {
  uint64_t now = mach_absolute_time();
  uint64_t prev = __atomic_exchange_n(&fp_swap,now,__ATOMIC_ACQ_REL);
  __atomic_store_n(&fp_swap_prev,prev,__ATOMIC_RELEASE);
  /* fsw: the second swap of a burst starts the frame floor at once. Once it
   * is on, while a higher vote runs or the thermal latch holds, swaps wake
   * nothing (the worker samples at its own period). */
  int frame_wake = fp_frame_hold && prev && now >= prev && now-prev <= fp_frame_hold &&
      !__atomic_load_n(&fp_floor_on,__ATOMIC_RELAXED) && !__atomic_load_n(&fp_blocked,__ATOMIC_RELAXED) &&
      !__atomic_load_n(&fp_boosted,__ATOMIC_RELAXED) && !__atomic_load_n(&fp_launch_high,__ATOMIC_RELAXED);
  if (__atomic_load_n(&fp_idling,__ATOMIC_ACQUIRE) || frame_wake)
   (void)thread_call_enter(&fp_call);
 }
}
/* Exec hook (bsd/kern/kern_exec.c, patch 0121): the main executable of an
 * application (".../X.app/X") is starting. Publishes a timestamp and wakes
 * the worker, like input; no MMIO, no sleep. name is the last path part. */
void fuxi_perf_launch(const char *name)
{
 if (!__atomic_load_n(&fp_enabled,__ATOMIC_ACQUIRE) || !fp_launch_hold) return;
 unsigned n = __atomic_add_fetch(&fp_launch_seen,1,__ATOMIC_RELAXED);
 __atomic_store_n(&fp_launch,mach_absolute_time(),__ATOMIC_RELEASE);
 (void)thread_call_enter(&fp_call);
 if (n <= 8 || !(n & 63)) printf("FUXI PERF: launch exec %u %s\n",n,name ? name : "?");
}
#endif
