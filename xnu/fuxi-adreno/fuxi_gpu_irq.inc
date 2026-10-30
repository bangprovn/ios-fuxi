/* GPU fence interrupt: WaitFence sleeps until the GPU says a fence landed (opt-in, fuxigpuncimport bit 26 = 67108864, default off).
 * Adapted from UniXNU-sheng package/GpuPowerProbe.cpp gpuIrq/ringIdle/irqAndTimer (b0ccf61, 36ec562) and UniXNU ArmCpu
 * armcpu_spi_attach (1951cc8), both BSD-2-Clause-Patent: Copyright (c) 2026, UniXNU contributors (LICENSE.UniXNU-sheng) and
 * Copyright (c) 2026, ArmCpu contributors (LICENSE.ArmCpu). Built into fuxi_adreno.c after the ABI backend.
 *
 * Routing. pe_fiq.c (patches 0004/0017) enables only the vtimer PPI and SGI1 in the redistributor and sets GICD_CTLR ARE_NS|Grp1;
 * no SPI is configured and nothing installs cpu_data's interrupt_handler, so sleh_irq's SWEET branch hands every other INTID >= 16
 * to a NULL handler and EOIs it. Here, once, at the first WaitFence that has to wait: ml_install_interrupt_handler on every CPU
 * (bound in turn, last CPU 0), then armcpu_spi_attach's distributor sequence: ICENABLER + RWP, IGROUPR group 1 NS, IPRIORITYR 0xa0
 * (the SGI/PPI priority), ICFGR level, IROUTER = CPU 0's MPIDR affinity (mode 0, never core 1: it runs the loader's USB monitor),
 * ISENABLER, readback. sleh EOIs after the handler (EOImode 0). Neither the redistributor nor any per-CPU startup is touched.
 *
 * Source. SPI 300 (kgsl_3d0_irq, level high) = INTID 332. Only RBBM_INT_0 bit 20, CP_CACHE_FLUSH_TS, is unmasked: a client frame's
 * BR fence is CP_EVENT_WRITE7 CACHE_CLEAN|WRITE|IRQ (fg_abi_frame, 0x88000031); the present copy's fence asks for no interrupt. The
 * handler clears only that bit (RBBM_INT_CLEAR_CMD) before the EOI, so the fault bits g3_faulted polls in RBBM_INT_0_STATUS stay.
 *
 * Storm protection. An interrupt that finds nothing to clear is empty; FG_IRQ_EMPTY_MAX in a row, or FG_IRQ_BURST_MAX within
 * 100 ms (a source that will not clear), mask the SPI for the rest of the boot. So do isolation (fg_isolate calls fg_irq_quiesce,
 * which also waits out a handler already reading the GPU: an abort on the interrupt stack panics) and 64 timed-out sleeps with no
 * interrupt ever seen. Any other SPI reaching the handler is masked (it has no owner to clear it). Waiters then poll as before. */
#define FG_IRQ_INTID 332u
#define FG_IRQ_BITS (1u<<20)
#define FG_IRQ_GICD 0x17100000ULL /* mkadt-fuxi.py refuses any other GIC layout */
#define FG_IRQ_EMPTY_MAX 1000u
#define FG_IRQ_BURST_MAX 5000u
#define FG_IRQ_NODELIVERY 64u
extern kern_return_t thread_wakeup_prim(event_t, boolean_t, wait_result_t);
static struct fg_window fg_irq_gicd = {FG_IRQ_GICD, 0x10000, 0};
static volatile uint32_t *fg_irq_status, *fg_irq_clear;
static unsigned fg_irq_state, fg_irq_ok, fg_irq_inside, fg_irq_why; /* state: 0 untried, 1 armed, 2 attach failed, 3 masked, 4 attaching */
static uint32_t fg_irq_seq, fg_irq_empty_run, fg_irq_burst, fg_irq_mask_was;
static uint64_t fg_irq_window, fg_irq_stats[FG_IRQ_STATS];
static int fg_irq_event;
static volatile uint32_t *fg_gicd(unsigned off) { return (volatile uint32_t *)(fg_irq_gicd.va + off); }
static void fg_spi_mask(unsigned id)
{
 if (!fg_irq_gicd.va || id < 32 || id >= 1020) return;
 *fg_gicd(0x180 + (id/32)*4) = 1u << (id%32);
 __asm__ volatile("dsb sy" ::: "memory");
}
static void fg_irq_dead(unsigned why)
{
 fg_spi_mask(FG_IRQ_INTID);
 if (!fg_irq_why) fg_irq_why = why;
 __atomic_store_n(&fg_irq_state, 3, __ATOMIC_RELEASE);
}
static void fg_irq_wake(void) { (void)thread_wakeup_prim((event_t)&fg_irq_event, FALSE, THREAD_AWAKENED); }
/* Interrupt context, interrupts masked, before sleh's EOI. One SPI is active on one CPU at a time, so the run counters have one writer. */
static void fg_irq_handler(void *target, void *refcon, void *nub, int source)
{
 (void)target; (void)refcon; (void)nub;
 unsigned id = (unsigned)source;
 if (id != FG_IRQ_INTID) { fg_spi_mask(id); __atomic_fetch_add(&fg_irq_stats[FG_IRQ_FOREIGN], 1, __ATOMIC_RELAXED); return; }
 uint32_t st = 0;
 __atomic_fetch_add(&fg_irq_inside, 1, __ATOMIC_SEQ_CST);
 if (__atomic_load_n(&fg_irq_ok, __ATOMIC_SEQ_CST) && !fg_access_failed) {
  st = *fg_irq_status & FG_IRQ_BITS;
  if (st) { *fg_irq_clear = st; __asm__ volatile("dsb sy" ::: "memory"); }
 }
 __atomic_fetch_sub(&fg_irq_inside, 1, __ATOMIC_SEQ_CST);
 uint64_t now = mach_absolute_time(), window;
 nanoseconds_to_absolutetime(100*NSEC_PER_MSEC, &window);
 if (now - fg_irq_window > window) { fg_irq_window = now; fg_irq_burst = 0; }
 __atomic_fetch_add(&fg_irq_stats[FG_IRQ_N], 1, __ATOMIC_RELAXED);
 if (st) fg_irq_empty_run = 0;
 else { __atomic_fetch_add(&fg_irq_stats[FG_IRQ_EMPTY], 1, __ATOMIC_RELAXED); ++fg_irq_empty_run; }
 if (fg_irq_empty_run >= FG_IRQ_EMPTY_MAX) fg_irq_dead(1);
 else if (++fg_irq_burst >= FG_IRQ_BURST_MAX) fg_irq_dead(2);
 __atomic_fetch_add(&fg_irq_seq, 1, __ATOMIC_RELEASE);
 fg_irq_wake();
}
/* fg_isolate: from here on nothing may read the GPU from interrupt context. Store-then-load on both sides (seq_cst), so either the
 * handler sees fg_irq_ok clear or this sees it inside and waits it out (a few MMIO accesses). */
static void fg_irq_quiesce(void)
{
 if (!__atomic_load_n(&fg_irq_ok, __ATOMIC_SEQ_CST)) return;
 __atomic_store_n(&fg_irq_ok, 0, __ATOMIC_SEQ_CST);
 fg_irq_dead(3);
 for (unsigned i = 0; i < 1000000 && __atomic_load_n(&fg_irq_inside, __ATOMIC_SEQ_CST); i++) __asm__ volatile("yield");
 fg_irq_wake();
}
/* armcpu_spi_attach's sequence for one SPI: level, group 1 NS, priority 0xa0, routed to one affinity; readback grp|en<<1|edge<<2|prio<<8. */
static const char *fg_spi_on(unsigned id, uint64_t affinity, uint32_t *readback, uint64_t *router)
{
 unsigned word = (id/32)*4, cfg = 0xc00 + (id/16)*4, shift = (id%16)*2 + 1, n = 0;
 uint32_t bit = 1u << (id%32);
 *fg_gicd(0x180 + word) = bit;
 while ((*fg_gicd(0) & 0x80000000u) && n < 100000) { n++; delay(1); }
 if (n == 100000) return "GICD_CTLR.RWP stuck";
 *fg_gicd(0x80 + word) |= bit;
 *(volatile uint8_t *)(fg_irq_gicd.va + 0x400 + id) = 0xa0;
 *fg_gicd(cfg) &= ~(1u << shift);
 *(volatile uint64_t *)(fg_irq_gicd.va + 0x6000 + 8*id) = affinity;
 __asm__ volatile("dsb sy" ::: "memory");
 *fg_gicd(0x100 + word) = bit;
 __asm__ volatile("dsb sy" ::: "memory");
 *readback = ((*fg_gicd(0x80 + word) & bit) ? 1u : 0) | ((*fg_gicd(0x100 + word) & bit) ? 2u : 0) |
     (((*fg_gicd(cfg) >> shift) & 1u) << 2) | ((uint32_t)*(volatile uint8_t *)(fg_irq_gicd.va + 0x400 + id) << 8);
 *router = *(volatile uint64_t *)(fg_irq_gicd.va + 0x6000 + 8*id);
 return (*readback & 7u) != 3u || *router != affinity ? "GICD readback" : NULL;
}
static const char *fg_irq_attach(void)
{
 unsigned id = FG_IRQ_INTID, cpus = ml_get_cpu_count();
 uint32_t readback = 0;
 uint64_t mpidr = UINT64_MAX, router = UINT64_MAX;
 const char *fail = NULL;
 if (fg_map_window(&fg_irq_gicd) != KERN_SUCCESS) return "GICD map";
 if (!(*fg_gicd(0) & 0x10u)) return "GICD_CTLR.ARE_NS clear";
 if (((*fg_gicd(4) & 31u) + 1)*32 <= id) return "GICD_TYPER lines";
 fg_irq_status = (volatile uint32_t *)fuxi_gpu_nc_pointer(GPU_REG_BASE + 0x201*4, 4);
 fg_irq_clear = (volatile uint32_t *)fuxi_gpu_nc_pointer(GPU_REG_BASE + 0x37*4, 4);
 if (!fg_irq_status || !fg_irq_clear) return "RBBM window";
 {
  FG_GUARD;
  if (!fg_abi_live || !fuxi_gpu_ready() || fg_abi_poisoned || fg_access_failed) fail = "GPU not live";
  else {
   fg_irq_mask_was = g3_gr(0x38);
   if (!g3_gw(0x37, FG_IRQ_BITS) || !g3_gw(0x38, FG_IRQ_BITS) || fg_access_failed) fail = "RBBM_INT_0_MASK write";
   else __atomic_store_n(&fg_irq_ok, 1, __ATOMIC_SEQ_CST);
  }
 }
 if (fail) return fail;
 /* The handler on every CPU, so a line that lands elsewhere is still cleared; CPU 0 last, whose affinity the line is routed to. */
 for (unsigned i = cpus; i-- > 0;) {
  processor_t p = cpu_to_processor((int)i);
  if (!p) continue;
  processor_t previous = thread_bind(p);
  (void)thread_block(THREAD_CONTINUE_NULL);
  ml_install_interrupt_handler(NULL, (int)id, NULL, fg_irq_handler, NULL);
  if (!i) mpidr = rsr("mpidr_el1") & 0xff00ffffffULL;
  (void)thread_bind(previous);
 }
 fail = mpidr == UINT64_MAX ? "no CPU 0" : fg_spi_on(id, mpidr, &readback, &router);
 printf("FUXI GPU IRQ: attach INTID %u cpus %u mpidr %#llx router %#llx readback %#x (grp|en<<1|edge<<2|prio<<8) int0_mask %#x->%#x %s\n",
     id, cpus, mpidr, router, readback, fg_irq_mask_was, FG_IRQ_BITS, fail ? fail : "armed");
 if (fail) {
  fg_spi_mask(id); __atomic_store_n(&fg_irq_ok, 0, __ATOMIC_SEQ_CST);
  FG_GUARD;
  if (fg_abi_live && fuxi_gpu_ready() && !fg_access_failed) (void)g3_gw(0x38, fg_irq_mask_was); /* the poll-only mask it found */
 }
 return fail;
}
int fuxi_gpu_irq_setup(void)
{
 if (!fg_irq_want) return 0;
 unsigned state = 0;
 if (!__atomic_compare_exchange_n(&fg_irq_state, &state, 4, 0, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) return state == 1;
 const char *fail = fg_irq_attach();
 if (fail && !fg_irq_why) printf("FUXI GPU IRQ: not attached (%s), WaitFence keeps polling\n", fail);
 __atomic_store_n(&fg_irq_state, fail ? 2u : 1u, __ATOMIC_RELEASE);
 return !fail;
}
uint32_t fuxi_gpu_irq_seq(void) { return __atomic_load_n(&fg_irq_seq, __ATOMIC_ACQUIRE); }
/* A client fence retires once its value is written and (non-root client) the ring has drained, and a present copy queued behind it
 * raises no interrupt: once the value is there the caller polls rather than sleep for an interrupt that may not come. */
int fuxi_gpu_irq_sleep(uint32_t seq0, uint64_t fence, unsigned us)
{
 if (__atomic_load_n(&fg_irq_state, __ATOMIC_ACQUIRE) != 1) return -1;
 if (!fg_abi_live || (int32_t)(g3_read(G3_FENCE_PA+FG_ABI_FENCE_OFF+4) - (uint32_t)fence) >= 0) {
  __atomic_fetch_add(&fg_irq_stats[FG_IRQ_WRITTEN], 1, __ATOMIC_RELAXED);
  return -2;
 }
 uint64_t deadline;
 clock_interval_to_deadline(us, NSEC_PER_USEC, &deadline);
 (void)assert_wait_deadline((event_t)&fg_irq_event, THREAD_UNINT, deadline);
 if (__atomic_load_n(&fg_irq_seq, __ATOMIC_ACQUIRE) != seq0) {
  /* An interrupt between the caller's seq read and the assert: wake the asserted wait so the block returns at once. */
  fg_irq_wake(); (void)thread_block(THREAD_CONTINUE_NULL);
  __atomic_fetch_add(&fg_irq_stats[FG_IRQ_RACES], 1, __ATOMIC_RELAXED);
  return 1;
 }
 if (thread_block(THREAD_CONTINUE_NULL) != THREAD_TIMED_OUT) { __atomic_fetch_add(&fg_irq_stats[FG_IRQ_WAKES], 1, __ATOMIC_RELAXED); return 1; }
 if (__atomic_add_fetch(&fg_irq_stats[FG_IRQ_TIMEOUTS], 1, __ATOMIC_RELAXED) >= FG_IRQ_NODELIVERY && !__atomic_load_n(&fg_irq_stats[FG_IRQ_N], __ATOMIC_RELAXED) &&
     __atomic_load_n(&fg_irq_state, __ATOMIC_ACQUIRE) == 1) {
  fg_irq_dead(4);
  printf("FUXI GPU IRQ: %u timed-out sleeps and no interrupt, masked; WaitFence polls\n", FG_IRQ_NODELIVERY);
 }
 return 0;
}
/* Snapshot for the user client's 600-wait report; also restores RBBM_INT_0_MASK if something rewrote it (counted). */
unsigned fuxi_gpu_irq_stats(uint64_t s[FG_IRQ_STATS], unsigned *why)
{
 unsigned state = __atomic_load_n(&fg_irq_state, __ATOMIC_ACQUIRE);
 if (state == 1) {
  FG_GUARD;
  if (__atomic_load_n(&fg_irq_ok, __ATOMIC_SEQ_CST) && fg_abi_live && fuxi_gpu_ready() && !fg_access_failed && g3_gr(0x38) != FG_IRQ_BITS &&
      g3_gw(0x38, FG_IRQ_BITS)) __atomic_fetch_add(&fg_irq_stats[FG_IRQ_MASKFIX], 1, __ATOMIC_RELAXED);
 }
 for (unsigned i = 0; i < FG_IRQ_STATS; i++) s[i] = __atomic_load_n(&fg_irq_stats[i], __ATOMIC_RELAXED);
 *why = fg_irq_why;
 return fg_irq_want ? state : 0;
}
