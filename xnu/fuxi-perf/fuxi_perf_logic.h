/* Pure policy shared with offline tests. Frequencies always come from EPSS LUT. */
#ifndef FUXI_PERF_LOGIC_H
#define FUXI_PERF_LOGIC_H
/* OSThermalPressureLevel wire values used by OSThermalNotification.h:
 * nominal=0, moderate=1, heavy=2, trapping=3, sleeping=4. This is not
 * NSProcessInfoThermalState's four-value scale. No sleeping is emitted:
 * this governor has no platform sleep/shutdown policy.
 */
enum { FP_TP_NOMINAL, FP_TP_MODERATE, FP_TP_HEAVY, FP_TP_TRAPPING, FP_TP_SLEEPING };
/* Rises publish at once; a fall waits until the level has held for `hold`
 * ticks. Without it the rise latch toggled the level 11 times in the first
 * 90 s of a boot (observed), and each change wakes every notify client. */
static int fp_pressure_hold(int previous, int next, uint64_t now, uint64_t *since,
    uint64_t hold)
{
 if (next < previous && previous >= 0 && now-*since < hold) return previous;
 if (next != previous) *since = now;
 return next;
}
static int fp_pressure_level(int previous, int valid, int temp, int blocked,
    unsigned hot, unsigned limit, unsigned resume)
{
 /* Missing quorum is at least heavy; never relax a prior trapping state
  * on missing data. The guard independently cuts all high votes. */
 if (!valid) return previous >= FP_TP_TRAPPING ? FP_TP_TRAPPING : FP_TP_HEAVY;
 /* Inclusive entry, 4 C hysteresis on heavy/trapping; moderate follows
  * the governor's rise/predictive/recovery latch as well as its hot trip.
  * limit/hot/resume may be lowered by the existing governor boot args. */
 if (temp >= 770 || (previous >= FP_TP_TRAPPING && temp > 730)) return FP_TP_TRAPPING;
 if (temp >= (int)limit || (previous >= FP_TP_HEAVY && temp > (int)limit-40)) return FP_TP_HEAVY;
 if (blocked || temp >= (int)hot ||
     (previous >= FP_TP_MODERATE && temp > (int)resume)) return FP_TP_MODERATE;
 return FP_TP_NOMINAL;
}
static unsigned fp_khz(uint32_t row)
{
 return row >> 30 ? (row & 255u)*19200u : 300000u;
}
/* Exclude one-core turbo rows. Duplicate frequencies terminate the table.
 * Pick the fastest eligible row at/below cap; never infer level ordering. */
static int fp_level(const uint32_t *rows, unsigned count, unsigned cap, unsigned *khz)
{
 int best = -1;
 unsigned freq = 0, prev = 0;
 for (unsigned i = 0; i < count; i++) {
  unsigned f = fp_khz(rows[i]);
  if (i && f == prev) break;
  prev = f;
  if (((rows[i] >> 16) & 7u) == 1 || !f || f > cap) continue;
  if (best < 0 || f > freq) { best = i; freq = f; }
 }
 *khz = freq;
 return best;
}
/* A new boost may start at or below fp_start dC (fuxiperfstart, default 600);
 * a running one continues to fp_boost_max (the limit, 680 unless fuxiperflimit
 * raises it). The hot/limit trip still vetoes both. */
static unsigned fp_start = 600, fp_boost_max = 680;
static int fp_want_boost(int boosted, int valid, int temp,
    uint64_t now, uint64_t input, uint64_t swap, uint64_t hold)
{
 if (!valid || temp > (int)fp_boost_max || (!boosted && temp > (int)fp_start)) return 0;
 int recent_input = input && now >= input && now-input <= hold;
 uint64_t activity = input > swap ? input : swap;
 int recent_activity = activity && now >= activity && now-activity <= hold;
 return recent_activity && (boosted || recent_input);
}
/* K is a floor, bounded by cool so idle can never raise a domain's vote.
 * As with cool/high, skip turbo rows and do not assume level ordering. */
static int fp_idle_level(const uint32_t *rows, unsigned count,
    unsigned floor, unsigned cool, unsigned *khz)
{
 int best = -1;
 unsigned freq = 0, prev = 0;
 if (floor > cool) floor = cool;
 for (unsigned i = 0; i < count; i++) {
  unsigned f = fp_khz(rows[i]);
  if (i && f == prev) break;
  prev = f;
  if (((rows[i] >> 16) & 7u) == 1 || !f || f < floor || f > cool) continue;
  if (best < 0 || f < freq) { best = i; freq = f; }
 }
 *khz = freq;
 return best;
}
/* fl3=1: EPSS L3 ("qcom,epss-l3", Linux osm-l3.c epss_l3_l3_vote). The HLOS
 * vote +0x90 is a LUT row index. Before any write, the boot vote must name a
 * row and the block's state +0x20 must run that row's L value (0097 boots:
 * vote 12, state 0x2dc00c52, L 82 = row 12 = 1574.4 MHz). Returns the fastest
 * non-turbo row to vote while boosted, or -1 to stay read-only. */
static int fp_l3_check(const uint32_t *rows, unsigned count, uint32_t enable,
    uint32_t vote, uint32_t state, unsigned *khz)
{
 *khz = 0;
 if (!(enable & 1) || vote >= count || (state & 255u) != (rows[vote] & 255u)) return -1;
 return fp_level(rows,count,UINT32_MAX,khz);
}
/* fla=MS: launch boost. The latest exec of an application's main executable
 * holds gold/prime high for MS (Xiaomi perfboostsconfig.xml launch hint
 * 0x1081: every cluster at max for 2 s). Entry obeys fp_start and a running
 * launch fp_boost_max, as touch; the hot/limit/slope latch (safe) vetoes both.
 * The touch duty budget does not apply: the thermal guards are its budget. */
static int fp_want_launch(int high, int valid, int temp, int safe,
    uint64_t now, uint64_t launch, uint64_t hold)
{
 if (!hold || !launch || !valid || !safe || temp > (int)fp_boost_max ||
     (!high && temp > (int)fp_start)) return 0;
 return now >= launch && now-launch <= hold;
}
/* fsf=PCT: scroll floor. While the last input is at most hold old, a cool
 * gold/prime vote rises to the floor (sheng 36ec562 holds 60 % of the top
 * level; stock scrolling holds DDR/L3 floors and sched_boost 2). The thermal
 * latch and the ceiling veto it, as for a running boost; no duty budget. */
static int fp_want_floor(int valid, int temp, int safe,
    uint64_t now, uint64_t input, uint64_t hold)
{
 if (!valid || !safe || temp > (int)fp_boost_max) return 0;
 return input && now >= input && now-input <= hold;
}
/* Floor row: the slowest eligible row at/above pct % of the fastest
 * non-turbo row, else the fastest at/below high; -1 unless faster than cool. */
static int fp_floor_level(const uint32_t *rows, unsigned count, unsigned pct,
    unsigned cool, unsigned high, unsigned *khz)
{
 unsigned top, freq = 0, prev = 0;
 int best = -1;
 *khz = 0;
 if (fp_level(rows,count,UINT32_MAX,&top) < 0) return -1;
 uint64_t want = (uint64_t)top*pct/100;
 for (unsigned i = 0; i < count; i++) {
  unsigned f = fp_khz(rows[i]);
  if (i && f == prev) break;
  prev = f;
  if (((rows[i] >> 16) & 7u) == 1 || !f || f < want || f > high) continue;
  if (best < 0 || f < freq) { best = (int)i; freq = f; }
 }
 if (best < 0) best = fp_level(rows,count,high,&freq);
 if (best < 0 || freq <= cool) return -1;
 *khz = freq;
 return best;
}
enum { FP_COOL, FP_HIGH, FP_IDLE };
static int fp_state(int boosted, int valid, int temp, int safe, int duty_ok,
    uint64_t now, uint64_t input, uint64_t swap, uint64_t hold,
    uint64_t idle_after, uint64_t start, uint64_t idle_entry)
{
 if (idle_after) {
  uint64_t activity = input > swap ? input : swap;
  if (activity < start) activity = start;
  /* Idle is below cool, so thermal cuts may also use it. The thermal and
   * duty guards still veto every high vote, including input wakeups. */
  /* No idle before the first swap: boot-time shader compiles run with no
   * input or swap, and 307 MHz silver made the first frame 6 s later. */
  if (swap && now >= activity && now-activity >= idle_after) return FP_IDLE;
  /* An input preceding idle must not let a swap wake into high, even when
   * the idle timeout is shorter than the legacy boost hold. */
  if (input <= idle_entry) input = 0;
 }
 return safe && duty_ok && fp_want_boost(boosted,valid,temp,now,input,swap,hold)
     ? FP_HIGH : FP_COOL;
}
#endif
