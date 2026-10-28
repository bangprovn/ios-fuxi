/* M6 platform adapter. gpu.c/gpu-a740.h/gpu-zap.h are exact shared probe
 * sources, copied into patch 0096 by tools/sync-fuxi-adreno-shared.py.
 * Fixed memory is excluded from boot_args AND ADT RAM before VM bootstrap.
 * All mappings, firmware, power votes and failure records survive until reset.
 */
#include <pexpert/arm64/board_config.h>
#if defined(FUXI)
#include <sys/param.h>
#include <sys/systm.h>
#include <kern/clock.h>
#include <machine/machine_routines.h>
#include <pexpert/pexpert.h>
#include <pexpert/device_tree.h>
#include <vm/vm_kern.h>
#include <vm/pmap.h>
#include <arm/caches_internal.h>
/* Expose only the private APIs used by this built-in BSD platform adapter. */
#define MACH_KERNEL_PRIVATE 1
#include <kern/thread_call.h>
#undef MACH_KERNEL_PRIVATE
#include <kern/kern_types.h>
#include "fuxi_adreno.h"
#include "fuxi_gpu_dcvs.h"
#include "gpu-handoff.h"
#include <IOKit/IOLocks.h>
#include "fuxi_gpu_abi.h"
#include "fuxi_abi_wire.h"
extern ppnum_t pmap_find_phys(pmap_t, addr64_t);
/* The BSD header export omits sched_prim's private dependencies. These match
 * osfmk/kern/{processor,sched_prim}.h; processor_t remains opaque here. */
extern processor_t cpu_to_processor(int);
extern processor_t thread_bind(processor_t);
extern wait_result_t thread_block(thread_continue_t);
extern wait_result_t assert_wait_deadline(event_t, wait_interrupt_t, uint64_t);
extern uint64_t thread_get_runtime_self(void);
/* ARM64 pmap_paddr_t is uint64_t; the typedef is private to Mach headers. */
extern kern_return_t pmap_enter_options_addr(pmap_t, vm_map_offset_t,
    uint64_t, vm_prot_t, vm_prot_t, unsigned, boolean_t, unsigned, void *);
/* Fault-contained aligned 32/64-bit access in machine_routines_asm.s. */
extern unsigned fuxi_gpu_access(uintptr_t, uint64_t *, unsigned, unsigned);
#define rsr(r) ({ uint64_t v; __asm__ volatile("mrs %0, " r : "=r"(v)); v; })
#define delay fg_delay
static void fg_delay(unsigned us) { delay_for_interval(us, NSEC_PER_USEC); }
static unsigned fg_scanout_requested, fg_scanout_panel120_requested, fg_scanout_double_requested, fg_scanout_triple_requested;
struct fg_window { uint64_t pa, size; uintptr_t va; };
/* Disjoint apertures; framebuffer A is mapped by SweetFramebuffer.
 * Stage14 uses scratch, never stage15's panel readback/CPU comparator. */
static struct fg_window fg_windows[] = {
 {0x100000,0x80000,0}, {0x3d00000,0x40000,0},
 {0x3d50000,0x40000,0}, {0x3d90000,0x10000,0},
 {0x3da0000,0x40000,0},
 {0xb280000,0x8000,0}, {0x81c60000,0x20000,0},
 {FG_SCRATCH,FG_SCRATCH_SIZE,0}, {0x9b098000,0x4000,0},
 {0xae04000,0x4000,0},
 {0xae14000,0x4000,0}, {0xae34000,0x4000,0},
 {0xb8a00000,0x9e4000,0}, /* reserved splash buffer B, fuxiscandouble=1 only */
 {0xb9400000,0x9e4000,0}, /* reserved splash buffer C, fuxiscantriple=1 only */
 {0xae94000,0x4000,0}, /* DSI0 command FIFO, fuxipanel120=1 only */
};
static uint64_t fg_mapped_bytes;
/* io_map's 8 MiB submap and ALL ml_io_map variants use KMA_NOFAIL.
 * Use kernel_map VA only (no backing RAM) and fallible page-table expansion.
 * KOBJECT reuses kernel_object rather than allocating a fresh VM object.
 * Every window is kernel-page aligned, including the zap reservation's
 * enclosing 16K page. No early-boot/interrupt-context callers are permitted.
 * Keep completed mappings until reset, as the GPU may retain DMA ownership. */
static kern_return_t fg_map_window(struct fg_window *w)
{
 vm_offset_t va = 0;
 if (!w->size || (w->pa & PAGE_MASK) || (w->size & PAGE_MASK) ||
     w->pa > UINT64_MAX - w->size || w->va) return KERN_INVALID_ARGUMENT;
 kern_return_t kr = kmem_alloc(kernel_map, &va, w->size,
     KMA_VAONLY | KMA_KOBJECT | KMA_NOPAGEWAIT, VM_KERN_MEMORY_IOKIT);
 if (kr != KERN_SUCCESS) return kr;
 for (vm_size_t off = 0; off < w->size; off += PAGE_SIZE) {
  kr = pmap_enter_options_addr(kernel_pmap, va + off, w->pa + off,
      VM_PROT_READ | VM_PROT_WRITE, VM_PROT_NONE, VM_WIMG_IO, TRUE,
      PMAP_OPTIONS_NOWAIT, NULL);
  if (kr != KERN_SUCCESS) {
   /* Never publish a partial window; no GPU has been started yet. */
   pmap_remove(kernel_pmap, va, va + w->size);
   kmem_free(kernel_map, va, w->size);
   return kr;
  }
 }
 w->va = va;
 fg_mapped_bytes += w->size;
 return KERN_SUCCESS;
}
static kern_return_t fg_map_windows(void)
{
 for (unsigned i = 0; i < sizeof(fg_windows)/sizeof(fg_windows[0]); i++) {
  struct fg_window *w = &fg_windows[i];
  if (!fg_scanout_requested && (w->pa == 0xae14000 || w->pa == 0xae34000)) continue;
  if (!fg_scanout_double_requested && w->pa == 0xb8a00000) continue;
  if (!fg_scanout_triple_requested && w->pa == 0xb9400000) continue;
  if (!fg_scanout_panel120_requested && w->pa == 0xae94000) continue;
  kern_return_t kr = fg_map_window(w);
  if (kr != KERN_SUCCESS) {
   printf("FUXI GPU: map PA %#llx bytes %#llx failed %d, mapped %llu bytes, IO submap 0 bytes\n",
       w->pa, w->size, kr, fg_mapped_bytes);
   return kr;
  }
 }
 printf("FUXI GPU: mapped %llu bytes NC in kernel_map, IO submap 0 bytes\n", fg_mapped_bytes);
 return KERN_SUCCESS;
}
static unsigned fg_access_failed;
uint32_t probe_fault_esr;
uint64_t probe_fault_far;
uintptr_t fuxi_gpu_nc_pointer(uint64_t pa, uint64_t bytes)
{
 if (!bytes) return 0;
 for (unsigned i = 0; i < sizeof(fg_windows)/sizeof(fg_windows[0]); i++) {
  struct fg_window *w = &fg_windows[i];
  if (pa < w->pa || !w->va) continue;
  uint64_t offset = pa-w->pa;
  if (offset >= w->size || bytes > w->size-offset || offset > UINTPTR_MAX-w->va) continue;
  uintptr_t va = w->va + offset;
  if (bytes-1 <= UINTPTR_MAX-va) return va;
 }
 return 0;
}
/* No physmap fallback: reserved scratch, firmware and MMIO live in the
 * lifetime kernel_map NC windows. Reject an entire access crossing an edge. */
static uintptr_t fg_pointer(uint64_t pa) { return fuxi_gpu_nc_pointer(pa, 1); }
static uintptr_t fg_pointer_range(uint64_t pa, uint64_t bytes)
{
 uintptr_t va = fuxi_gpu_nc_pointer(pa, bytes);
 if (!va) panic("FUXI GPU: unmapped NC range PA %#llx bytes %#llx", pa, bytes);
 return va;
}
static unsigned fg_access(uint64_t pa, uint64_t *v, unsigned wr, unsigned wide)
{
 uintptr_t va = fuxi_gpu_nc_pointer(pa, wide ? 8 : 4);
 if (!va || (pa & (wide ? 7 : 3))) return fg_access_failed = 1;
 unsigned bad = fuxi_gpu_access(va, v, wr, wide);
 if (bad) {
  probe_fault_esr = rsr("esr_el1"); probe_fault_far = pa;
  if (!fg_access_failed) printf("FUXI GPU: access fault PA %#llx op %u ESR %#x\n", pa, wr, probe_fault_esr);
  fg_access_failed = 1;
 }
 return bad;
}
uint32_t probe_safe_rd32(uint64_t pa, uint32_t *faulted)
{
 uint64_t v = 0;
 *faulted = fg_access(pa, &v, 0, 0);
 return (uint32_t)v;
}
unsigned probe_safe_wr32(uint64_t pa, uint32_t value)
{
 uint64_t v = value;
 /* After an access fault, allow diagnostic reads and explicit bus isolation
  * only. No continuation of a partly programmed bring-up sequence. */
 if (fg_access_failed) return 1;
 return fg_access(pa, &v, 1, 0);
}
static void fg_store64(uint64_t pa, uint64_t value)
{
 if (!fg_access_failed) (void)fg_access(pa, &value, 1, 1);
}
static const uint8_t *fg_firmware(unsigned which, unsigned *size)
{
 const struct fg_handoff *h = (const struct fg_handoff *)fg_pointer_range(FG_HANDOFF, sizeof(struct fg_handoff));
 *size = h->fw_size[which];
 return (const uint8_t *)fg_pointer_range(FG_FW_BASE + which*FG_FW_SLOT, FG_FW_SLOT);
}
static unsigned fg_abi_enabled, fg_client_busy, fg_abi_present_wait; /* present_wait: a present/reserve was refused for a running client job (stops queueing behind it) */
static IOLock *fg_ring_lock;
/* Present callers already hold the surface lock. ABI never takes that lock;
 * neither client waits nor BO allocations hold this short ring/table mutex. */
struct fg_guard { IOLock *lock; };
static void fg_guard_exit(struct fg_guard *g) { if (g->lock) IOLockUnlock(g->lock); }
#define FG_GUARD struct fg_guard guard __attribute__((cleanup(fg_guard_exit))) = {fg_ring_lock}; if (guard.lock) IOLockLock(guard.lock)
#define FUXI_GPU_XNU 1
#define G_PTR(pa) fg_pointer(pa)
#define G_PTR_RANGE(pa, bytes) fg_pointer_range(pa, bytes)
#define G_RING_INDEX(w) ((w) & 8191u)
static void fg_scm_call(uint64_t ret[4], unsigned slot, unsigned step,
    uint32_t fn, uint32_t info, uint64_t a, uint64_t b, uint64_t c);
#include "gpu.c"
static unsigned fg_clock_measure;
/* Startup diagnostics have their own fault result: an optional readback must
 * not latch fg_access_failed and poison an otherwise working GPU. Only call
 * while the proven cold-start power/clock sequence and GPU_SET are retained. */
static int fg_clock_io(uint64_t pa, uint32_t *word, unsigned write)
{
 uintptr_t va = fuxi_gpu_nc_pointer(pa, 4);
 uint64_t value = *word;
 if (!va || fuxi_gpu_access(va, &value, write, 0)) return 0;
 __asm__ volatile("dsb sy" ::: "memory");
 if (!write) *word = (uint32_t)value;
 return 1;
}
static int fg_clock_counter(uint64_t *value)
{
 /* A7xx RBBM counter3 at 0x322/323, high/low/high. Public freedreno
  * a6xx.xml register documentation; ALWAYS_COUNT selector is zero. */
 for (unsigned i = 0; i < 8; i++) {
  uint32_t h = 0, l = 0, again = 0;
  if (!fg_clock_io(GPU_REG_BASE+0x323*4, &h, 0) ||
      !fg_clock_io(GPU_REG_BASE+0x322*4, &l, 0) ||
      !fg_clock_io(GPU_REG_BASE+0x323*4, &again, 0)) return 0;
  if (h == again) { *value = (uint64_t)h<<32 | l; return 1; }
 }
 return 0;
}
static void fg_clock_sample(void)
{
 uint32_t control = 0, selector = 0;
 uint64_t first = 0, last = 0, start = 0, end = 0;
 if (!fg_clock_io(GPU_REG_BASE+0x500*4, &control, 0) ||
     !fg_clock_io(GPU_REG_BASE+0x50a*4, &selector, 0)) goto unavailable;
 uint32_t enable = control | 1, always = 0;
 int ok = fg_clock_io(GPU_REG_BASE+0x50a*4, &always, 1) &&
     fg_clock_io(GPU_REG_BASE+0x500*4, &enable, 1) && fg_clock_counter(&first);
 if (ok) {
  start = rsr("cntpct_el0");
  delay(1000); /* one startup sample only, before ABI/display publication */
  ok = fg_clock_counter(&last);
  end = rsr("cntpct_el0");
 }
 /* Counter selection/global enable belong to the pre-publication state.
  * Restore both even after a failed access; never retry the sample. */
 int restored = fg_clock_io(GPU_REG_BASE+0x50a*4, &selector, 1);
 restored &= fg_clock_io(GPU_REG_BASE+0x500*4, &control, 1);
 uint64_t timer = rsr("cntfrq_el0");
 if (ok && restored && end > start && timer && last > first &&
     last-first <= 2000000 && end-start <= timer/10) {
  printf("FUXI GPU CLOCK: rbbm_count_khz=%llu sample_us=%llu (clock-gated sample)\n",
      (last-first)*timer/(end-start)/1000, (end-start)*1000000/timer);
  return;
 }
 unavailable:
 printf("FUXI GPU CLOCK: rbbm_count_khz=unavailable\n");
}
static void fg_clock_log(void)
{
 unsigned index = g_gpu_selected ? g_gpu_selected : 1, bw = g_gpu_bw_index(g_gpu_selected);
 printf("FUXI GPU CLOCK: level=%u gpu_index=%u requested_khz=%u gx_arc=%#x bw_index=%u ddr_kBps=%u ddr_requested=%llu ddr_clamped=%u hfi_ack=1\n",
     g_gpu_selected,index,g_gpu_freq[index],gpu_extra[0][100+index],bw,
     bw == 2 ? g_gpu_ddr_row2(g_gpu_selected,g_gpu_ddr_requested) : g_gpu_ddr[bw],
     g_gpu_ddr_requested,g_gpu_ddr_clamp(g_gpu_selected,g_gpu_ddr_requested));
 printf("FUXI GPU CLOCK: GMU table_khz=0/200000/500000 cx_arc=%#x/%#x/%#x (no host GMU perf-index vote)\n",
     gpu_extra[0][110],gpu_extra[0][111],gpu_extra[0][112]);
 static const uint32_t offsets[] = {CC_PLL0+PLL_MODE,CC_PLL0+PLL_L_VAL,CC_PLL0+0x14,
     CC_PLL1+PLL_MODE,CC_PLL1+PLL_OPMODE,CC_PLL1+PLL_L_VAL,CC_PLL1+0x14,CC_GMU_RCGR+4};
 uint32_t v[8] = {0};
 for (unsigned i = 0; i < 8; i++)
  if (!fg_clock_io(GPU_GPUCC_BASE+offsets[i], &v[i], 0)) {
   printf("FUXI GPU CLOCK: gpucc_readback=unavailable core_measured_khz=unavailable\n"); return;
  }
 /* PLL1 main -> GMU source3, HID divider=(field+1)/2, 19.2 MHz XO.
  * This is register-derived, not a measured frequency or a GPU core rate. */
 uint64_t pll = ((uint64_t)(v[5]&0xffff)*65536+(v[6]&0xffff))*19200/65536;
 unsigned gmu = (v[3]&PLL_OUTCTRL) && v[4] == PLL_RUN && ((v[7]>>8)&7) == 3 ?
     (unsigned)(pll*2/((v[7]&31)+1)) : 0;
 printf("FUXI GPU CLOCK: PLL0 mode=%#x l=%#x alpha=%#x PLL1 mode=%#x l=%#x alpha=%#x gmu_rcgr=%#x gmu_derived_khz=%u core_measured_khz=unavailable\n",
     v[0],v[1],v[2],v[3],v[5],v[6],v[7],gmu);
 if (fg_clock_measure) {
  uint32_t cx = 0, gx = 0;
  if (fg_clock_io(GPU_GPUCC_BASE+CC_CX_GDSCR, &cx, 0) && g_gdsc_on(cx) &&
      fg_clock_io(GPU_GPUCC_BASE+CC_GX_GDSCR, &gx, 0) && g_gdsc_on(gx)) fg_clock_sample();
  else printf("FUXI GPU CLOCK: rbbm_count_khz=unavailable\n");
 }
}
/* Retain BOTH physical attempts, independently of the shared logical-call
 * report slot. Static storage only; no allocation or firmware in diagnostics. */
static volatile struct {
 uint32_t fn, info, attempts, cpu[2];
 uint64_t ret[2][4];
} fg_scm_records[8];
static void fg_scm_call(uint64_t ret[4], unsigned slot, unsigned step,
    uint32_t fn, uint32_t info, uint64_t a, uint64_t b, uint64_t c)
{
 /* thread_bind changes eligibility; explicitly block to force migration.
  * This is a sleepable thread-call worker, never an interrupt cross-call.
  * Restore its previous binding on every returned path. */
 processor_t previous = thread_bind(cpu_to_processor(0));
 (void)thread_block(THREAD_CONTINUE_NULL);
 fg_scm_records[slot].fn = fn;
 fg_scm_records[slot].info = info;
 for (unsigned attempt = 0; attempt < 2; attempt++) {
  printf("FUXI GPU: PAS step %u fn %#x attempt %u (enter CPU0 bound)\n",
      step, fn, attempt+1);
  boolean_t interrupts = ml_set_interrupts_enabled(FALSE);
  unsigned cpu = ml_get_cpu_number_local();
  if (cpu == 0) g5_smc_raw(ret, fn, info, a, b, c);
  else { ret[0] = UINT64_MAX; ret[1] = ret[2] = ret[3] = 0; }
  /* Capture all results before interrupt restoration can reschedule us.
   * Neither logging nor the retry delay executes with interrupts masked. */
  fg_scm_records[slot].cpu[attempt] = cpu;
  for (unsigned i = 0; i < 4; i++) fg_scm_records[slot].ret[attempt][i] = ret[i];
  fg_scm_records[slot].attempts = attempt+1;
  (void)ml_set_interrupts_enabled(interrupts);
  printf("FUXI GPU: PAS fn %#x CPU%u attempt %u x0=%#llx x1=%#llx x2=%#llx x3=%#llx\n",
      fn, cpu, attempt+1, ret[0], ret[1], ret[2], ret[3]);
  if (cpu != 0) { printf("FUXI GPU: PAS CPU0 bind failed; SMC skipped\n"); break; }
  if (ret[0] != fn) break;
  printf("FUXI GPU: PAS fn %#x not serviced (x0 == fn); %s\n", fn,
      attempt ? "bounded retry exhausted" : "retry 1/1 after 100 us");
  if (!attempt) delay(100);
 }
 (void)thread_bind(previous);
}

#define FG_SOURCE_VA 0x50000000u
#define FG_SOURCE_BYTES 0x01000000u
#define FG_SOURCE_TABLE (G3_PT + 0x20000) /* general tables use slots below 32 */
#define FG_SOURCE_L3 8u
_Static_assert(FG_SOURCE_TABLE + FG_CACHE_SLOTS*FG_SOURCE_L3*4096 <= G3_PT+0x100000,
    "source tables exceed reserved GPU table pool");
/* Four disjoint 16 MiB windows. 4K SMMU granules support 2 MiB blocks or
 * 64 KiB contiguous leaf groups, NOT 16 KiB block descriptors. */
static uint64_t fg_generation[FG_CACHE_SLOTS];
static unsigned fg_abi_coh, fg_abi_coh_wb; /* defined by the ABI backend below */
static unsigned fg_scanout_preclean; /* bit 20; with a passing wb coherence probe it forces the old NC copy source (A/B knob), see fuxi_gpu_map */
static unsigned char fg_src_wb[FG_CACHE_SLOTS]; /* slot mapped write-back inner-shareable (AttrIndx 1) */
static unsigned fg_src_n_wb, fg_src_n_nc, fg_src_n_ncstale;
/* fuxigpuncimport bit 21 (2097152, needs queue mode 512): the present copy runs in the ABI client's address space (see fg_inl_setup). */
static unsigned fg_abi_inline, fg_inl_ready, fg_root_client, fg_inl_busy, fg_inl_fbmask; /* ready: 0 untried 1 mapped 2 failed; root_client: the client root stays installed */
enum { FG_INL_NOROOT, FG_INL_WINDOW, FG_INL_RING, FG_INL_BUFFERS, FG_INL_CLIENT, FG_INL_SWITCH, FG_INL_REASONS };
static uint32_t fg_inl_n, fg_inl_fb[FG_INL_REASONS], fg_inl_lat_n;
static uint64_t fg_inl_lat, fg_inl_latmax;
static void fg_inl_mirror(unsigned idx, uint64_t v);
static int fg_inl_usable(void), fg_inl_to_present(void);
#define FG_INL_SRC_VA 0x90000000u
#define FG_INL_FB_VA 0xa0000000u
/* One transient PFN list, serialized by the surface cache lock. Keep lookup
 * and actual NC SMMU table stores in separate timing buckets. */
static ppnum_t fg_ppns[FG_SOURCE_BYTES/PAGE_SIZE+1];
static volatile uint64_t *fg_l2(void)
{
 volatile uint64_t *root = (volatile uint64_t *)G_PTR_RANGE(G3_PT,4096);
 return (volatile uint64_t *)G_PTR_RANGE(root[1] & ~4095ULL,4096);
}
static volatile unsigned fg_state; /* 0 disabled/starting, 1 ready, 2 permanently failed */
/* Serialized with the ring after startup. Counts survive client close/open;
 * failure before ready is not a lost userspace context. No recovery yet. */
static uint64_t fg_faults, fg_suspends;
static unsigned fg_reset_held;
static unsigned fg_started, fg_limit, fg_force, fg_seq;
int fuxi_gpu_ready(void) { return __atomic_load_n(&fg_state, __ATOMIC_ACQUIRE) == 1; }
static uint64_t fg_deadline, fg_submit_time;
static unsigned fg_busy, fg_magic;
static int fg_expired(void) { return rsr("cntpct_el0") >= fg_deadline; }
static void fg_fail(const char *reason, unsigned code)
{
 if (fuxi_gpu_ready() && fg_faults!=UINT64_MAX) ++fg_faults;
 __atomic_store_n(&fg_state, 2, __ATOMIC_RELEASE);
 printf("FUXI GPU: disabled permanently: %s (%#x), CPU present\n", reason, code);
}
/* Linux6.12 a6xx_bus_clear_pending_transactions + sw_reset: stop client
 * requests, drain AXI arbiter, then hold reset. Unlike recovery, never clear
 * halt or restart. Mask SID0/1 to FAULT as a second containment barrier.
 * If ACK is missing, keep the source prepared/retained rather than freeing
 * pages still reachable by DMA; CPU fallback resumes with isolation asserted. */
static int fg_isolate(void)
{
 uint64_t v = 1;
 int good = !fg_access(GPU_REG_BASE + 0x3c45*4, &v, 1, 0);
 for (unsigned step = 1; step <= 2; step++) {
  unsigned i;
  for (i = 0; i < 1000; i++) {
   v = 0;
   if (fg_access(GPU_REG_BASE + 0x3c46*4, &v, 0, 0)) { good = 0; break; }
   if (v & step) break;
   delay(1);
  }
  if (i == 1000) good = 0;
  if (step == 1) {
   v = 2;
   if (fg_access(GPU_REG_BASE + 0x3c45*4, &v, 1, 0)) good = 0;
  }
 }
 v = 1;
 if (!fg_access(GPU_REG_BASE + 0x43*4, &v, 1, 0) && !fg_reset_held) {
  fg_reset_held=1;
  if (fg_suspends!=UINT64_MAX) ++fg_suspends;
 }
 delay(100);
 for (unsigned i = 0; i < 2; i++) {
  v = 2u << 16; (void)fg_access(GPU_SMMU_BASE + SMMU_S2CR(g3_slot[i]), &v, 1, 0);
 }
 __asm__ volatile("dsb sy" ::: "memory");
 printf("FUXI GPU: GBIF drain %s, reset held, SID0/1 faulted%s\n",
     good ? "ACK" : "FAILED", good ? "" : "; source quarantined until reset");
 return good;
}
static int fg_source_tables(void)
{
 volatile uint64_t *l2 = fg_l2();
 if (!l2 || g3_next > 32) return 0;
 for (unsigned i = 0; i < FG_CACHE_SLOTS*FG_SOURCE_L3; i++) {
  unsigned slot = ((FG_SOURCE_VA >> 21) & 511) + i;
  if (l2[slot]) return 0;
  /* Leave L2 invalid: first map and eviction obey break-before-make. */
 }
 g_zero(FG_SOURCE_TABLE, FG_CACHE_SLOTS*FG_SOURCE_L3*4096);
 return 1;
}
/* The SweetSurface cache lock serializes map/copy/unmap, including destruction.
 * Caller holds a retained, prepared IOBuffer for this mapping generation.
 * IOBuffer pages cannot migrate while prepared; reprepare/replacement gets a
 * new generation. No PFN scan or TLB invalidation on a cache hit. */
int fuxi_gpu_unmap(unsigned slot, struct fg_times *t)
{
 FG_GUARD;
 if (slot >= FG_CACHE_SLOTS || fg_busy) return 0;
 uint64_t start = mach_absolute_time();
 volatile uint64_t *l2 = fg_l2();
 unsigned first = ((FG_SOURCE_VA >> 21) & 511) + slot*FG_SOURCE_L3;
 for (unsigned i = 0; i < FG_SOURCE_L3; i++) {
  l2[first+i] = 0; fg_inl_mirror(slot*FG_SOURCE_L3+i, 0);
 }
 uint32_t tlb[128] = {0};
 int err = g4_tlb(tlb);
 fg_generation[slot] = 0;
 fg_src_wb[slot] = 0;
 if (t) t->unmap += mach_absolute_time()-start;
 if (err || fg_access_failed) {
  fg_fail("cached unmap TLB sync", err);
  return fg_isolate();
 }
 return 1;
}
int fuxi_gpu_map(unsigned slot, uint64_t generation, uintptr_t source,
    uint64_t length, struct fg_times *t)
{
 FG_GUARD;
 if (!fuxi_gpu_ready() || slot >= FG_CACHE_SLOTS || !generation || fg_generation[slot] || !length ||
     length > FG_SOURCE_BYTES || length+(source&4095) > FG_SOURCE_BYTES) return 0;
 uint64_t start = mach_absolute_time();
 volatile uint64_t *leaves = (volatile uint64_t *)G_PTR_RANGE(FG_SOURCE_TABLE+slot*32768,32768);
 volatile uint64_t *l2 = fg_l2();
 unsigned first = ((FG_SOURCE_VA >> 21) & 511) + slot*FG_SOURCE_L3;
 unsigned pages = (length+(source&4095)+4095)/4096;
 uintptr_t base = source & ~(uintptr_t)4095;
 uintptr_t first_page = base & ~(uintptr_t)PAGE_MASK;
 for (uintptr_t va = first_page; va < base+(uintptr_t)pages*4096; va += PAGE_SIZE) {
  ppnum_t ppn = pmap_find_phys(kernel_pmap, va);
  if (!ppn) {
   t->lookup += mach_absolute_time()-start;
   fg_fail("unmapped prepared source page", 7); return 0;
  }
  fg_ppns[(va-first_page)>>PAGE_SHIFT] = ppn;
 }
 t->lookup += mach_absolute_time()-start;
 start = mach_absolute_time();
 unsigned wb = fg_abi_coh == 1 && fg_abi_coh_wb && !fg_scanout_preclean; /* proven coherent: GPU snoops, no CPU clean; bit 20 keeps NC + clean */
 for (unsigned i = 0; i < pages; i++) {
  uintptr_t va = base+(uintptr_t)i*4096;
  ppnum_t ppn = fg_ppns[(va-first_page)>>PAGE_SHIFT];
  uint64_t pa = ((uint64_t)ppn << PAGE_SHIFT) | (va & PAGE_MASK);
  if (pa >= (1ULL<<36)) return 0;
  leaves[i] = pa | 3 | (3u<<6) | (wb ? 1u<<2 : 0) | (3u<<8) | (1u<<10);
 }
 for (unsigned i = pages; i < 4096; i++) leaves[i] = 0;
 for (unsigned i = 0; i < FG_SOURCE_L3; i++) {
  unsigned at = i*512;
  uint64_t pa = leaves[at] & 0x0000000ffffff000ULL;
  unsigned contiguous = at+512 <= pages && !(pa & 0x1fffff);
  for (unsigned j = 1; contiguous && j < 512; j++)
   if ((leaves[at+j] & 0x0000000ffffff000ULL) != pa+(uint64_t)j*4096) contiguous = 0;
  if (contiguous) {
   l2[first+i] = (leaves[at] & ~3ULL) | 1; /* 2 MiB block */
   fg_inl_mirror(slot*FG_SOURCE_L3+i, l2[first+i]);
   t->blocks++;
  } else {
   /* Architected 16-entry contiguous hint only for aligned, complete groups. */
   for (unsigned j = at; j+16 <= pages && j < at+512; j += 16) {
    uint64_t p = leaves[j] & 0x0000000ffffff000ULL;
    unsigned ok = !(p & 65535);
    for (unsigned k = 1; ok && k < 16; k++)
     if ((leaves[j+k] & 0x0000000ffffff000ULL) != p+(uint64_t)k*4096) ok = 0;
    if (ok) { for (unsigned k = 0; k < 16; k++) leaves[j+k] |= 1ULL<<52; t->groups++; }
   }
   l2[first+i] = (FG_SOURCE_TABLE+slot*32768+i*4096) | 3;
   fg_inl_mirror(slot*FG_SOURCE_L3+i, l2[first+i]);
  }
 }
 uint32_t tlb[128] = {0};
 int err = g4_tlb(tlb);
 t->map += mach_absolute_time()-start;
 if (err || fg_access_failed) { fg_fail("cached source TLB sync", err); return 0; }
 fg_generation[slot] = generation;
 fg_src_wb[slot] = wb;
 t->misses++;
 return 1;
}
static int fg_abi_live_for_scanout(void);
#include "fuxi_gpu_scanout.inc"
static int fg_abi_init(void);
static thread_call_data_t fg_init_call;
/* fuxigpulevel bit 4: governor. The boot level is the cap, so this can only
 * lower the vote; the doorbell paths call fg_dcvs_kick(), nothing polls at the
 * floor. Votes reuse the proven GX_BW_PERF_VOTE through the HFI queue; one
 * thread call owns it after startup. Not yet observed on the device. */
static thread_call_data_t fg_dcvs_call;
static unsigned fg_dcvs_on, fg_dcvs_cap, fg_dcvs_level, fg_dcvs_hold_ms;
static uint64_t fg_dcvs_last, fg_dcvs_hold, fg_dcvs_floor_since;
static uint64_t fg_dcvs_floor_ns, fg_dcvs_ups, fg_dcvs_downs, fg_dcvs_fails;
static unsigned fg_dcvs_streak;
static void fg_dcvs_kick(void)
{
 if (!fg_dcvs_on) return;
 __atomic_store_n(&fg_dcvs_last, mach_absolute_time(), __ATOMIC_RELEASE);
 unsigned have = __atomic_load_n(&fg_dcvs_level, __ATOMIC_ACQUIRE); /* 0 until the call is set up */
 if (have && have != fg_dcvs_cap) (void)thread_call_enter(&fg_dcvs_call);
}
static int fg_dcvs_vote(unsigned level)
{
 if (!fuxi_gpu_ready() || fg_access_failed) return 0x9ff;
 uint32_t keep[4], vote[4] = {0,1,level,g_gpu_bw_index(level)};
 for (unsigned i = 0; i < 4; i++) keep[i] = gpu_extra[0][56+i];
 int err = g_hfi_send(30, vote, 4, 4);
 for (unsigned i = 0; i < 4; i++) gpu_extra[0][56+i] = keep[i]; /* boot GX vote record */
 return err;
}
static void fg_dcvs_worker(thread_call_param_t a, thread_call_param_t b)
{
 (void)a; (void)b;
 uint64_t now = mach_absolute_time(), rearm, ns;
 unsigned have = __atomic_load_n(&fg_dcvs_level, __ATOMIC_ACQUIRE);
 unsigned want = fgd_want(now, __atomic_load_n(&fg_dcvs_last, __ATOMIC_ACQUIRE), fg_dcvs_hold, fg_dcvs_cap, &rearm);
 if (want != have) {
  int err = fg_dcvs_vote(want);
  if (!err) {
   if (want == FGD_FLOOR) { fg_dcvs_floor_since = now; fg_dcvs_downs++; }
   else { absolutetime_to_nanoseconds(now-fg_dcvs_floor_since, &ns); fg_dcvs_floor_ns += ns; fg_dcvs_ups++; }
   __atomic_store_n(&fg_dcvs_level, want, __ATOMIC_RELEASE);
   fg_dcvs_streak = 0;
   have = want;
   unsigned n = (unsigned)(fg_dcvs_ups+fg_dcvs_downs);
   if (!(n & (n-1)))
    printf("FUXI GPU DCVS: level %u cap %u ups %llu downs %llu fails %llu floor_ms %llu\n",
        want,fg_dcvs_cap,fg_dcvs_ups,fg_dcvs_downs,fg_dcvs_fails,fg_dcvs_floor_ns/1000000);
  } else {
   fg_dcvs_fails++;
   if (!(fg_dcvs_fails & (fg_dcvs_fails-1)))
    printf("FUXI GPU DCVS: vote level %u failed, HFI err %#x, fails %llu\n",want,err,fg_dcvs_fails);
   /* Eight failures in a row: stop governing and put the cap back if possible. */
   if (++fg_dcvs_streak >= 8) {
    fg_dcvs_on = 0;
    if (have != fg_dcvs_cap && !fg_dcvs_vote(fg_dcvs_cap)) __atomic_store_n(&fg_dcvs_level, fg_dcvs_cap, __ATOMIC_RELEASE);
    printf("FUXI GPU DCVS: disabled after %u failed votes, level %u\n",fg_dcvs_streak,fg_dcvs_level);
    return;
   }
  }
 }
 /* At the cap a timer runs until the hold ends; a failed vote retries after a
  * hold or on the next doorbell, never in a loop. At the floor nothing runs. */
 if (have != FGD_FLOOR) {
  if (!rearm) rearm = fg_dcvs_hold;
  uint64_t deadline = now + rearm;
  (void)thread_call_enter_delayed(&fg_dcvs_call, deadline);
 }
}
static void fg_dcvs_start(void)
{
 if (!fg_dcvs_on) return;
 thread_call_setup_with_options(&fg_dcvs_call, fg_dcvs_worker, NULL,
     THREAD_CALL_PRIORITY_KERNEL, THREAD_CALL_OPTIONS_ONCE);
 __atomic_store_n(&fg_dcvs_last, mach_absolute_time(), __ATOMIC_RELEASE);
 __atomic_store_n(&fg_dcvs_level, fg_dcvs_cap, __ATOMIC_RELEASE);
 uint64_t deadline = mach_absolute_time() + fg_dcvs_hold;
 (void)thread_call_enter_delayed(&fg_dcvs_call, deadline);
 printf("FUXI GPU DCVS: cap level %u floor %u hold %u ms (not yet observed)\n",
     fg_dcvs_cap,FGD_FLOOR,fg_dcvs_hold_ms);
}
static void fg_thread(thread_call_param_t arg, thread_call_param_t result)
{
 (void)arg; (void)result;
 /* Sleepable thread-call worker, delayed until after AppleCLCD registers. */
 DTEntry chosen; const void *prop; unsigned bytes;
 if (SecureDTLookupEntry(NULL, "/chosen", &chosen) != kSuccess ||
     SecureDTGetProperty(chosen, "fuxi-gpu-memory", &prop, &bytes) != kSuccess ||
     bytes != 16 || ((uint64_t *)prop)[0] != FG_SCRATCH || ((uint64_t *)prop)[1] != FG_SCRATCH_SIZE) {
  fg_fail("missing loader/ADT reservation (use --gpu-reserve)", 1); goto out;
 }
 /* Verify actual VM bootstrap bound as well as the DT declaration. */
 extern vm_offset_t gPhysBase; extern vm_size_t gPhysSize;
 if (gPhysBase != FG_RAM_BASE || gPhysSize > FG_RAM_SIZE) {
  fg_fail("scratch is inside XNU RAM", 2); goto out;
 }
 kern_return_t kr = fg_map_windows();
 if (kr != KERN_SUCCESS) { fg_fail("uncached mapping failed", kr); goto out; }
 const struct fg_handoff *h = (const struct fg_handoff *)G_PTR_RANGE(FG_HANDOFF,sizeof(struct fg_handoff));
 if (h->magic != FG_MAGIC || h->version != 1 || h->bytes != sizeof(*h) || h->reserved ||
     h->base != FG_SCRATCH || h->size != FG_SCRATCH_SIZE) {
  fg_fail("missing/invalid cold firmware handoff", 3); goto out;
 }
 for (unsigned i = 0; i < 3; i++) {
  unsigned n = h->fw_size[i];
  if (!n || n > FG_FW_SLOT || (n & 3) ||
      fg_hash((const uint8_t *)G_PTR_RANGE(FG_FW_BASE+i*FG_FW_SLOT,n), n) != h->fw_hash[i]) {
   fg_fail("firmware size/checksum", i); goto out;
  }
 }
 printf("FUXI GPU: cold bring-up, 32 MiB reserved NC, limit %u\n", fg_limit);
 gpu_probe(fg_limit == 13 ? 13 : 16);
 if (fg_access_failed || gpu_extra[5][4] != 1 ||
     (fg_limit != 13 && gpu_extra[13][2] != 1)) {
  printf("FUXI GPU: stage %u results clock=%#x GMU=%#x HFI=%#x GX=%#x "
      "CB=%#x CP=%#x fence=%#x zap=%#x clear/copy=%#x\n", gpu_stage,
      gpu_report[65], gpu_extra[0][2], gpu_extra[0][3], gpu_extra[0][60],
      gpu_extra[5][2], gpu_extra[5][3], gpu_extra[5][4], gpu_extra[13][2], gpu_extra[11][2]);
  fg_fail("bring-up/self-test", gpu_stage); goto out;
 }
 printf("FUXI GPU: through stage %u PASS, GMU CB%u GPU CB%u ASID%u, "
     "fence=%#x TRUST=%#x zap=%#x pixels=%#x\n", fg_limit == 13 ? 13 : 16,
     g_bank, g3_bank, g3_asid, g3_read(G3_FENCE_PA), g3_gr(0xf400),
     gpu_extra[13][2], gpu_extra[11][2]);
 fg_clock_log();
 if (fg_limit < 17 || fg_force == 1) { fg_fail("diagnostic stop after self-test", fg_limit); goto out; }
 /* Double mode validates SRC0 under display ownership, failing closed there. */
 if ((!fg_scanout_double_requested && g3_read(0xae05014) != G4_FB_PA) || g3_read(0xae05024) != 4320 ||
     g3_read(0xae05000) != (2400u<<16 | 1080u) || g3_read(0xae05030) != 0x237ff ||
     !g_map_at(G3_PT, &g3_next, G4_FB_VA, G4_FB_PA, (4320*2400+4095)&~4095u, 1u<<6) ||
     (fg_scanout_double_requested && !g_map_at(G3_PT, &g3_next, G4_FB_VA+0xa00000u, 0xb8a00000u, (4320*2400+4095)&~4095u, 1u<<6)) ||
     (fg_scanout_triple_requested && !g_map_at(G3_PT, &g3_next, G4_FB_VA+0x1400000u, 0xb9400000u, (4320*2400+4095)&~4095u, 1u<<6)) ||
     !fg_source_tables()) { fg_fail("scanout geometry/mapping", 4); goto out; }
 uint32_t tlb[128] = {0};
 if (g4_tlb(tlb) || fg_access_failed) { fg_fail("initial TLB sync", 5); goto out; }
 printf("FUXI GPU: ready, BGRA strided rects + one fence, 4 cached backings, requested %u kHz GPU vote (not measured), async one-in-flight, no timer sleep\n", g_gpu_freq[g_gpu_selected ? g_gpu_selected : 1]);
 /* Finish all ABI-only MMIO/table setup before the display can submit.
  * Publication may race userspace, so it follows the release of ready. */
 int publish_abi = fg_abi_enabled && fg_abi_init();
 if (fg_abi_enabled && !publish_abi)
  printf("FUXI GPU ABI: initialization failed, service withheld\n");
 __atomic_store_n(&fg_state, 1, __ATOMIC_RELEASE);
 fg_dcvs_start();
 if (publish_abi) fuxi_gpu_abi_publish();
 out: return;
}
void fuxi_gpu_start(uint64_t fb, unsigned width, unsigned height, unsigned pitch)
{
 unsigned enable = 0;
 (void)PE_parse_boot_argn("fuxigpu", &enable, sizeof(enable));
 if (enable != 1 || fg_started) return;
 fg_started = 1; fg_limit = 17;
 uint64_t requested_level = 0;
 (void)PE_parse_boot_argn("fuxigpulevel", &requested_level, sizeof(requested_level));
 unsigned dcvs_hold_ms = 0;
 fg_dcvs_on = fgd_decode(requested_level, G_GPU_LEVEL_MAX, &requested_level, &fg_dcvs_cap, &dcvs_hold_ms);
 if (fg_dcvs_on) {
  fg_dcvs_hold_ms = dcvs_hold_ms;
  nanoseconds_to_absolutetime((uint64_t)dcvs_hold_ms*NSEC_PER_MSEC, &fg_dcvs_hold);
 }
 g_gpu_selected = g_gpu_select(requested_level);
 if (requested_level > G_GPU_LEVEL_MAX)
  printf("FUXI GPU CLOCK: rejected fuxigpulevel=%llu (allowed 0..%u); legacy level=0\n", requested_level, G_GPU_LEVEL_MAX);
 (void)PE_parse_boot_argn("fuxigpuddr", &g_gpu_ddr_requested, sizeof(g_gpu_ddr_requested));
 if (g_gpu_ddr_requested > G_GPU_DDR_MAX)
  printf("FUXI GPU CLOCK: rejected fuxigpuddr=%llu (allowed 0..%u); level bandwidth default\n", g_gpu_ddr_requested, G_GPU_DDR_MAX);
 (void)PE_parse_boot_argn("fuxigpuclock", &fg_clock_measure, sizeof(fg_clock_measure));
 fg_clock_measure = fg_clock_measure == 1;
 (void)PE_parse_boot_argn("fuxigpuabi", &fg_abi_enabled, sizeof(fg_abi_enabled));
 fg_abi_enabled = fg_abi_enabled == 1;
 if (fg_abi_enabled && !(fg_ring_lock = IOLockAlloc())) {
  fg_abi_enabled = 0;
  printf("FUXI GPU ABI: no ring mutex, service withheld\n");
 }
 (void)PE_parse_boot_argn("fuxigpu_stage", &fg_limit, sizeof(fg_limit));
 (void)PE_parse_boot_argn("fuxigpu_fail", &fg_force, sizeof(fg_force));
 if ((fg_limit != 13 && fg_limit != 16 && fg_limit != 17) ||
     fb != G4_FB_PA || width != 1080 || height != 2400 || pitch != 4320) {
  fg_fail("unsupported boot framebuffer/stage", fg_limit); return;
 }
 /* Static storage avoids kernel_thread_start's NOFAIL thread/stack allocation
  * and thread_call_func_delayed's panic-on-exhaustion fixed call pool. */
 uint64_t deadline;
 thread_call_setup_with_options(&fg_init_call, fg_thread, NULL,
     THREAD_CALL_PRIORITY_KERNEL, THREAD_CALL_OPTIONS_ONCE);
 clock_interval_to_deadline(2, NSEC_PER_SEC, &deadline);
 (void)thread_call_enter_delayed(&fg_init_call, deadline);
}

/* Inclusive nearest coordinates. Splitting is required by the destination's
 * 64-byte pitch unit: 4320 is not representable but 2*4320 is. Interleave row
 * classes, with enough classes to align BOTH pitches. Full screen uses two
 * blits, arbitrary dword pitches need at most sixteen. No per-row WFI/setup. */
static void fg_rect(unsigned src, unsigned dst, unsigned pitch,
    unsigned width, unsigned height, unsigned stride)
{
 unsigned sx = (src & 63)/4, dx = (dst & 63)/4;
 g3_emit(g4_pkt4(G4_SP_SRC_INFO, 5));
 g3_emit(0x00500430); g3_emit((width+sx) | height<<15);
 g3_emit(src & ~63u); g3_emit(0); g3_emit((pitch*stride/64) << 9);
 g3_emit(g4_pkt4(G4_RB_DST_INFO, 4));
 g3_emit(0x430); g3_emit(dst & ~63u); g3_emit(0); g3_emit(4320*stride/64);
 g3_emit(g4_pkt4(G4_GRAS_SRC_X, 6));
 g3_emit(sx<<8); g3_emit((sx+width-1)<<8); g3_emit(0); g3_emit((height-1)<<8);
 g3_emit(dx); g3_emit((dx+width-1) | (height-1)<<16);
 g4_event(63); g3_emit(g3_pkt7(0x2c, 1)); g3_emit(3); g4_wfi();
}
/* Use XNU's forced PoC clean (CVAC on generic ARM64), never invalidate CPU
 * rendered data. Merge touching cache-line intervals; typical full-width
 * 4352-byte surfaces have only 32 bytes padding and clean as ONE range. */
static void fg_clean(uintptr_t source, unsigned pitch, unsigned sx, unsigned sy,
    unsigned width, unsigned height)
{
 uintptr_t begin = 0, end = 0;
 for (unsigned y = 0; y < height; y++) {
  uintptr_t row = source+(uint64_t)(sy+y)*pitch+sx*4;
  uintptr_t a = row & ~(uintptr_t)63, b = (row+width*4+63) & ~(uintptr_t)63;
  if (end && a > end) { CleanPoC_DcacheRegion_Force(begin, end-begin); end = 0; }
  if (!end) begin = a;
  end = b;
 }
 if (end) CleanPoC_DcacheRegion_Force(begin, end-begin);
}
/* fuxigpuncimport bit 20: the whole copy source, by SweetSurface before reserve. Only
 * cache maintenance on a wired kernel mapping, so no GPU lock and no ring hold. */
void fuxi_gpu_clean_source(uintptr_t source, uint64_t length)
{
 if (source && length) CleanPoC_DcacheRegion_Force(source & ~(uintptr_t)63, ((source & 63)+length+63) & ~(uint64_t)63);
}
/* CP_RB_WPTR as a fenced register write (NativeAdreno Adreno740 NativeAdrenoRing::writeWptr, MIT). The GMU drops a CPU write to a GPU
 * register that arrives while the GPU domain is not ready and flags it in GMU_AHB_FENCE_STATUS (0x23b13, bit 0; _CLR at 0x23b14, written
 * 7). Their i491 saw a fence stall on nearly every submission until WPTR was written again. Opt-in (fuxigpuncimport bit 1): one GMU
 * read per doorbell, then the same value again (at most 200 times). Default off: the plain write, as before. Not yet observed on device. */
#define FG_GMU_AHB_FENCE_STATUS (GPU_GMU_CORE+0x9313*4)
#define FG_GMU_AHB_FENCE_CLR (GPU_GMU_CORE+0x9314*4)
static unsigned fg_wptr_fence;
static unsigned fg_wptr_stats[4]; /* writes, writes dropped at least once, repeated writes, writes still dropped after 200 tries */
static int fg_ring_doorbell(void)
{
 fg_wptr_stats[0]++;
 if (!g3_gw(0x807, g3_wptr)) return 0;
 if (!fg_wptr_fence) return 1;
 for (unsigned tries = 0;; tries++) {
  __asm__ volatile("dsb sy" ::: "memory");
  uint32_t status = g3_read(FG_GMU_AHB_FENCE_STATUS);
  if (fg_access_failed) return 0;
  if (!(status & 1)) { if (tries) fg_wptr_stats[1]++; return 1; }
  if (!g3_write(FG_GMU_AHB_FENCE_CLR, 7)) return 0;
  if (tries == 200) { fg_wptr_stats[3]++; return 1; }
  fg_wptr_stats[2]++;
  if (!g3_gw(0x807, g3_wptr)) return 0;
 }
}
/* Per-copy GPU timestamps (always on): CP_REG_TO_MEM of the 64-bit always-on counter (19.2 MHz, reg 0x980) into the fence page at
 * +192 (copy start, after a WFI), +200 (blits done, before the cache flush) and +208 (after the flush and the fence). The CPU reads the
 * counter's low word at the doorbell and at the retiring poll: submit_to_start is the wait behind earlier ring work, blit and flush
 * the copy's own execution, poll_late the time from the GPU finishing to the display tick noticing. Not yet observed on device. */
#define FG_CP_STAMP 192u
static uint32_t fg_cp_submit_lo, fg_cp_n, fg_cp_bad, fg_cp_pitch, fg_cp_rects;
static uint64_t fg_cp_sum[4], fg_cp_max[4]; /* submit_to_start, blit, flush, poll_late */
static void fg_cp_stamp(unsigned off)
{
 g3_emit(g3_pkt7(0x3e, 3)); g3_emit(0x980 | (2u<<18) | (1u<<30)); g3_emit(G3_FENCE_VA+off); g3_emit(0);
}
static void fg_cp_retire(void)
{
 uint32_t s0 = g3_read(G3_FENCE_PA+FG_CP_STAMP), s1 = g3_read(G3_FENCE_PA+FG_CP_STAMP+8), s2 = g3_read(G3_FENCE_PA+FG_CP_STAMP+16), now = g3_gr(0x980);
 fg_cp_n++;
 if (fg_access_failed || !s0 || !s1 || !s2) fg_cp_bad++;
 else {
  uint32_t v[4] = {s0-fg_cp_submit_lo, s1-s0, s2-s1, now-s2};
  for (unsigned i = 0; i < 4; i++) { fg_cp_sum[i] += v[i]; if (v[i] > fg_cp_max[i]) fg_cp_max[i] = v[i]; }
 }
 if (!(fg_cp_n % 600)) {
  #define FG_US(t) ((unsigned long long)((t)*10/192))
  printf("FUXI GPU: copy_exec blit avg %llu max %llu us, flush avg %llu max %llu us; submit_to_start avg %llu max %llu us; poll_late avg %llu max %llu us (copies %u no_stamp %u pitch %u rect_classes %u)\n",
      FG_US(fg_cp_sum[1]/600), FG_US(fg_cp_max[1]), FG_US(fg_cp_sum[2]/600), FG_US(fg_cp_max[2]), FG_US(fg_cp_sum[0]/600), FG_US(fg_cp_max[0]),
      FG_US(fg_cp_sum[3]/600), FG_US(fg_cp_max[3]), fg_cp_n, fg_cp_bad, fg_cp_pitch, fg_cp_rects);
  #undef FG_US
  memset(fg_cp_sum, 0, sizeof fg_cp_sum); memset(fg_cp_max, 0, sizeof fg_cp_max);
 }
}
/* Called once per display tick; never sleeps, spins for completion or submits.
 * A completed fence wins over a late timer/deadline. */
int fuxi_gpu_poll(struct fg_times *t)
{
 FG_GUARD;
 if (!fg_busy) return 1;
 int err = fg_access_failed || g3_faulted();
 /* Inline copy (bit 21): client jobs may be queued behind it, so only its own fence says it retired. */
 if (!err && g3_read(G3_FENCE_PA+64) == fg_magic && (fg_inl_busy || (g3_gr(0x806) == g3_wptr &&
     g3_gr(0x807) == g3_wptr && !(g3_gr(0x210) & ~1u)))) {
  fg_scanout_stamps.fence = mach_absolute_time();
  t->wait += fg_scanout_stamps.fence-fg_submit_time;
  fg_cp_retire();
  if (fg_inl_busy) { uint64_t d = fg_scanout_stamps.fence-fg_submit_time; fg_inl_lat += d; fg_inl_lat_n++; if (d > fg_inl_latmax) fg_inl_latmax = d; }
  if (fg_scanout_requested && !fg_scanout_verify()) {
   fg_fail("scanout pixel mismatch", 0x604);
   int drained = fg_isolate();
   fg_busy = fg_inl_busy = 0; FG_ABI_WAKE();
   if (!drained && fg_scanout_owned) (void)fg_scanout_stop("GPU DMA drain failed");
   return drained ? -1 : -2;
  }
  fg_busy = fg_inl_busy = 0;
  FG_ABI_WAKE(); /* the copy retired: a client submit refused for it may go */
  return 1;
 }
 if (!err && !fg_expired()) return 0;
 fg_fail("async present fence/fault/deadline", 0x603);
 /* Only fault containment has bounded register polling; no scheduler wait. */
 int drained = fg_isolate();
 fg_busy = fg_inl_busy = 0; FG_ABI_WAKE();
 if (!drained && fg_scanout_owned) (void)fg_scanout_stop("GPU DMA drain failed");
 t->wait += mach_absolute_time()-fg_submit_time;
 return drained ? -1 : -2;
}
int fuxi_gpu_copy(unsigned slot, uint64_t generation, uintptr_t source, uint64_t length,
    unsigned pitch, unsigned sx, unsigned sy, unsigned dx, unsigned dy,
    unsigned width, unsigned height, struct fg_times *t)
{
 FG_GUARD;
 if (fg_scanout_owned && !fg_scanout_writable) return 0;
 unsigned inl = fg_abi_inline && fg_inl_usable(); /* the client root is installed and stays: queue the copy in it */
 if (!inl && fg_client_busy) fg_abi_present_wait = 1;
 if (!fuxi_gpu_ready() || fg_busy || (!inl && fg_client_busy) || slot >= FG_CACHE_SLOTS || fg_generation[slot] != generation) return 0;
 if (!width || !height || width > 1080 || height > 2400 || dx > 1080-width || dy > 2400-height ||
     (source & 3) || (pitch & 3) || pitch > 131008 || ((uint64_t)sx+width)*4 > pitch ||
     length+(source&4095) > FG_SOURCE_BYTES ||
     ((uint64_t)sy+height-1)*pitch+((uint64_t)sx+width)*4 > length) return 0;
 if (fg_scanout_double_active) {
  /* CA supplies a complete surface; expand damage to the complete panel. */
  if (sx < dx || sy < dy) return 0;
  sx -= dx; sy -= dy; dx = dy = 0; width = 1080; height = 2400;
  if (((uint64_t)sx+width)*4 > pitch ||
      ((uint64_t)sy+height-1)*pitch+((uint64_t)sx+width)*4 > length) return 0;
 }
 if (inl) {
  /* Never refused for a client job: it lands in the same in-order ring. Room for the whole copy (at most about 450 words), else fall back. */
  unsigned used = (g3_wptr-g3_gr(0x806)) & 8191, want = 1u | (fg_scanout_double_requested ? 2u : 0) | (fg_scanout_triple_requested ? 4u : 0);
  unsigned why = fg_access_failed ? FG_INL_RING : used > 8191-1024 ? FG_INL_RING : (fg_inl_fbmask & want) != want ? FG_INL_BUFFERS : FG_INL_REASONS;
  if (why != FG_INL_REASONS) { fg_inl_fb[why]++; inl = 0; }
 } else if (fg_abi_inline) fg_inl_fb[fg_root_client ? FG_INL_WINDOW : fg_inl_ready == 2 ? FG_INL_WINDOW : FG_INL_NOROOT]++;
 if (!inl && fg_root_client && !fg_inl_to_present()) { fg_inl_fb[fg_client_busy ? FG_INL_CLIENT : FG_INL_SWITCH]++; return 0; }
 uint64_t start = mach_absolute_time();
 /* Skip the source clean when the slot is mapped write-back inner-shareable (coherent-import self-test
  * bit 64 passed in wb mode and bit 20 is off) or the caller pre-cleaned it (bit 20, NC slot). A slot
  * mapped NC before the probe passed keeps AttrIndx 0 and is still cleaned. The destination stays NC. */
 unsigned coh = fg_abi_coh == 1 && fg_abi_coh_wb;
 if (fg_src_wb[slot]) fg_src_n_wb++;
 else if (fg_scanout_preclean) fg_src_n_nc++; /* NC slot, whole source cleaned by SweetSurface */
 else { fg_src_n_ncstale += coh; fg_clean(source, pitch, sx, sy, width, height); }
 if (!((fg_src_n_wb+fg_src_n_nc+fg_src_n_ncstale) % 600)) printf("FUXI GPU: copy source wb_skip %u pre_cleaned %u cleaned_after_coh %u\n", fg_src_n_wb, fg_src_n_nc, fg_src_n_ncstale);
 t->clean += mach_absolute_time()-start;
 start = mach_absolute_time();
 int err = 0;
 if (g3_faulted()) {
  fg_fail("before present", 0x601);
  if (fg_scanout_owned && !fg_isolate()) {
   (void)fg_scanout_stop("GPU DMA drain failed"); return -1;
  }
  return 0;
 }
 unsigned stride = 2;
 while ((pitch*stride) & 63) stride *= 2;
 /* XML has 15-bit dimensions; coordinates here are <2416 and strided pitch
  * <2^21. At most 16*32+128 <8192 words; the retired ring cannot be lapped. */
 if (fg_scanout_double_active) {
  if (fg_scanout_back_ready && !fg_scanout_triple_requested) { fg_scanout_back_ready = 0; fg_scanout_ready_drops++; }
  fg_scanout_copy = fg_scanout_target();
 }
 g_wr(G3_FENCE_PA, FG_CP_STAMP, 0); g_wr(G3_FENCE_PA, FG_CP_STAMP+8, 0); g_wr(G3_FENCE_PA, FG_CP_STAMP+16, 0);
 fg_cp_pitch = pitch; fg_cp_rects = stride < height ? stride : height;
 g4_wfi(); fg_cp_stamp(FG_CP_STAMP);
 g4_setup();
 for (unsigned y = 0; y < stride && y < height; y++)
  fg_rect((inl ? FG_INL_SRC_VA : FG_SOURCE_VA)+slot*FG_SOURCE_BYTES+(source&4095)+(sy+y)*pitch+sx*4,
      (inl ? FG_INL_FB_VA : G4_FB_VA)+(fg_scanout_double_active ? fg_scanout_copy*0xa00000u : 0)+(dy+y)*4320+dx*4, pitch, width, (height-1-y)/stride+1, stride);
 unsigned magic = ++fg_seq;
 if (!magic) magic = ++fg_seq;
 fg_cp_stamp(FG_CP_STAMP+8); /* fg_rect ends with a WFI: every blit is done */
 g4_end(G3_FENCE_VA+64, magic);
 fg_cp_stamp(FG_CP_STAMP+16);
 g3_wptr &= 8191;
 __asm__ volatile("dsb sy" ::: "memory");
 fg_deadline = rsr("cntpct_el0") + rsr("cntfrq_el0")/10; /* six ticks */
 fg_dcvs_kick();
 fg_cp_submit_lo = g3_gr(0x980);
 if (!fg_ring_doorbell()) err = 0x602;
 if (fg_force == 2) err = 0x6ff;
 t->submit += mach_absolute_time()-start;
 if (err || fg_access_failed) {
  fg_fail("present submit", err);
  int drained = fg_isolate();
  if (!drained && fg_scanout_owned) (void)fg_scanout_stop("GPU DMA drain failed");
  return drained ? 0 : -1;
 }
 if (fg_scanout_requested) {
  fg_scanout_source = source; fg_scanout_pitch = pitch;
  fg_scanout_sx = sx; fg_scanout_sy = sy; fg_scanout_dx = dx; fg_scanout_dy = dy;
  fg_scanout_w = width; fg_scanout_h = height;
 }
 fg_magic = magic;
 fg_submit_time = mach_absolute_time();
 fg_scanout_stamps.submitted = fg_submit_time; fg_scanout_stamps.fence = 0;
 fg_busy = 1; fg_abi_present_wait = 0; /* the copy owns the GPU now; the client hold (bit 4096) ends */
 fg_inl_busy = inl;
 if (inl) {
  fg_inl_n++;
  if (!(fg_inl_n % 600)) {
   uint64_t ns = 0, mx = 0;
   if (fg_inl_lat_n) absolutetime_to_nanoseconds(fg_inl_lat/fg_inl_lat_n, &ns);
   absolutetime_to_nanoseconds(fg_inl_latmax, &mx);
   printf("FUXI GPU: inline_copies %u fallback_copies %u (no_root %u window %u ring %u buffers %u client_busy %u switch %u) queue_to_retire avg %llu us max %llu us\n", fg_inl_n,
       fg_inl_fb[0]+fg_inl_fb[1]+fg_inl_fb[2]+fg_inl_fb[3]+fg_inl_fb[4]+fg_inl_fb[5], fg_inl_fb[0], fg_inl_fb[1], fg_inl_fb[2], fg_inl_fb[3], fg_inl_fb[4], fg_inl_fb[5], (unsigned long long)(ns/1000), (unsigned long long)(mx/1000));
   fg_inl_lat = fg_inl_latmax = 0; fg_inl_lat_n = 0;
  }
 }
 FG_ABI_WAKE(); /* a held client submit re-checks: the hold ended, the GPU is busy with the copy */
 return 2; /* submitted; source is owned until poll retires this fence */
}
#include "fuxi_gpu_abi_backend.inc"
#endif
