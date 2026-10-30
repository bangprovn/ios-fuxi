/* Built into fuxi_adreno.c, with access to its proven SMMU/ring helpers.
 * Single privileged diagnostic client, not a security boundary for PM4.
 * Its root has no scanout or surface-cache mappings. The kernel-only runtime
 * pages below va_start are identical in both roots; only idle TTBR switches
 * are allowed. Client leaves never modify the present root.
 */
#define FG_ABI_PT (G_MEM+0x1c00000)
#define FG_ABI_PT_BYTES 0x400000u
#define FG_ABI_FENCE_OFF 128u
_Static_assert(FG_ABI_PT >= G4_DST_PA+G4_BYTES+4096, "ABI table overlap");
_Static_assert(FG_ABI_PT >= G5_META_PA+G5_META_BYTES, "ABI/zap metadata overlap");
_Static_assert(FG_ABI_PT >= FG_FW_BASE+3*FG_FW_SLOT, "ABI/cold handoff or firmware overlap");
_Static_assert(FG_ABI_PT+FG_ABI_PT_BYTES <= G_MEM+G_MEM_SIZE, "ABI tables exceed scratch");
static unsigned fg_abi_live, fg_abi_seq, fg_abi_completed, fg_abi_poisoned, fg_abi_timed_out;
static int fg_abi_drained = 1;
static int fg_abi_live_for_scanout(void) { return fg_abi_live; }
static uint64_t fg_client_deadline;
static thread_call_data_t fg_client_watchdog;
/* Every mapped import can be referenced indirectly by PM4, not just the IB
 * list. Serialize cache handoff with the same GPU lock as submit/retirement.
 * The user client owns these prepared kernel mappings until successful unmap. */
static struct { uint64_t iova, bytes; uintptr_t source; unsigned permissions, gpu_only, dty_first, dty_pages; } fg_abi_imports[FG_ABI_MAX_IMPORTS];
static unsigned fg_abi_host_handoff, fg_abi_ncimport, fg_abi_unstick, fg_abi_cohtest, fg_abi_tlbpages, fg_abi_mapwait, fg_abi_queue, fg_abi_roimport, fg_abi_pace;
/* Queue mode (bit 512): jobs in flight are seq-completed, at most this many; stats: queued, deepest chain, refused for a waiting present, refused full. */
#define FG_ABI_QUEUE_MAX 4u
static uint32_t fg_abi_q_stats[6]; /* queued, deepest, refused for a present, full, refused by the hold, hold expired */
static unsigned fg_abi_hold;
static unsigned fg_abi_cf, fg_cf_holding; /* bit 16 copy priority, see fg_abi_copy_first */
static unsigned fg_abi_pk; /* bit 27 park instead of refuse, see fg_pk_park */
#define FG_PK_IDLE_US 1500u
static void fg_cf_reset(void), fg_pk_reset(void);
static int fg_pk_check(void);
/* Hook the userclient installs: wakes submits waiting out a refusal (NULL in host tests, where the macro is defined here). */
void (*fuxi_gpu_abi_wake_hook)(void);
#ifndef FG_ABI_WAKE
#define FG_ABI_WAKE() do { if (fuxi_gpu_abi_wake_hook) fuxi_gpu_abi_wake_hook(); } while (0)
#endif
static unsigned fg_abi_start;
static unsigned fg_abi_probe, fg_abi_spin, fg_abi_mid, fg_abi_ar, fg_abi_hard, fg_abi_dirty;
static uint64_t fg_abi_hold_since;
/* Ticks the last fuxi_gpu_abi_map spent after taking the GPU lock (the caller's total minus this is the wait for that lock). */
static uint64_t fg_abi_map_work;
/* fuxigpuncimport is a bitmask, default 0: 1 NC-import map-time clean; 2 fenced WPTR (fg_ring_doorbell); 4 stall recovery
 * (fg_abi_unstick_step); 8 per-frame present event log and 32 skip of superseded copies (both parsed by SweetFramebuffer); 16 inline-mode
 * copy priority (fg_abi_copy_first); 64 coherent-import self-test (skips the per-submit clean/retire invalidate only if it proves the GPU path
 * coherent); 128 ranged TLBIVAL for a small unmap; 256 map waits for an idle GPU first (the pre-N167 behaviour; default maps while a
 * job runs, see fg_abi_map_locked); 512 queue a submit behind a running job (see fg_abi_submit_locked). Parsed once in fg_abi_init. 1048576
 * (scanout copy-source pre-clean) is parsed by fuxi_gpu_scanout_configure. 67108864 (bit 26): WaitFence sleeps on the GPU fence interrupt (fuxi_gpu_irq.inc).
 * 134217728 (bit 27, needs 16): park the job copy priority would refuse and ring it behind the copy (fg_pk_park). 268435456 (bit 28, with 8):
 * the frame log starts at the first burst of not-shown frames (parsed by SweetFramebuffer). */
static unsigned fg_irq_want;
static unsigned fg_abi_coh_wb; /* the passing variant mapped the data page cacheable write-back, inner-shareable: imports are then mapped that way */
static unsigned fg_abi_coh, fg_abi_coh_tries; /* coh: 0 untested, 1 proven coherent, 2 refused */
/* The submit record holds up to NativeAdrenoAbiMaxBuffers IBs; one frame is then one ring prologue and one CP context reset. */
_Static_assert(FG_ABI_FRAME_WORDS+8 < 8192, "one frame must fit the idle ring");
static uint32_t fg_abi_words[FG_ABI_FRAME_WORDS];
/* IBs per submission: submissions, IBs, the most in one, submissions with more than 16 (one frame was several before the 128-IB record). */
static uint32_t fg_abi_ibs[4];
/* Stall recovery: kicks, fence of the last stalled job, bus drains, GBIF client/arbiter halt acks of the last drain, jobs that completed
 * after a kick, jobs that completed after a drain, snapshots taken. */
static uint32_t fg_abi_stall_stats[8];
static unsigned fg_abi_stage, fg_abi_stall_lines;
/* Kernel mapping of every mapped range (diagnostics only: the words under a stalled IB), and the map sequence of each. */
static struct { uint64_t iova, bytes; uintptr_t source; uint32_t seq; } fg_abi_maps[FG_ABI_MAX_BOS];
static uint32_t fg_abi_map_seq, fg_abi_submit_map_seq;
static uint64_t fg_abi_cleaned, fg_abi_skipped, fg_abi_invalidated;
/* Per-submit clean volume by class (0 GPU-writable, 1 read-only) and size bucket (<64K, <1M, <16M, more), bytes and ticks, plus the bytes
 * the GPU_ONLY imports skipped; taken and cleared by fuxi_gpu_abi_clean_split. */
static uint64_t fg_abi_cc_bytes[2], fg_abi_cc_ticks[2], fg_abi_cc_bb[4], fg_abi_cc_bt[4], fg_abi_cc_go;
static void fg_abi_cc_note(unsigned permissions, uint64_t bytes, uint64_t ticks)
{
 unsigned c=(permissions&2) ? 0 : 1, b=bytes<(1u<<16) ? 0 : bytes<(1u<<20) ? 1 : bytes<(1u<<24) ? 2 : 3;
 fg_abi_cc_bytes[c]+=bytes; fg_abi_cc_ticks[c]+=ticks; fg_abi_cc_bb[b]+=bytes; fg_abi_cc_bt[b]+=ticks;
}
static void fg_abi_cache_region(uintptr_t source, uint64_t bytes, int complete)
{
 uintptr_t end=source+bytes;
 for (uintptr_t p=source;p<end;p+=64) {
  if (complete) { __asm__ volatile("dc ivac, %0" :: "r"(p) : "memory"); }
  else { __asm__ volatile("dc civac, %0" :: "r"(p) : "memory"); }
 }
 if (complete) fg_abi_invalidated+=bytes;
 else fg_abi_cleaned+=bytes;
}
/* Per-page dirty tracking of import cleans (fuxigpuncimport bit 524288). The CPU-written pages of an import are found with the pmap modify
 * bit instead of cleaning every byte at every submit. Protocol per 16K page: pmap_clear_modify (write-protects every mapping and flushes
 * the TLB, so the next CPU store faults and arm_fast_fault sets the bit again) BEFORE the clean, so a store between the two is cleaned
 * now or flagged again for the next submit, never lost. Fail closed: an import whose pages are not all managed, whose page list does not
 * fit, or any import once the self-test refused, keeps the full clean at every submit. Kernel stores through a wired mapping or the
 * physical aperture do not set the bit; this driver writes no import pages (alloc zeroing is for allocated BOs, the coherency probe uses
 * its own page). */
#define FG_DTY_CHUNK 64u
#define FG_DTY_CHUNKS 512u
#ifndef FG_ABI_DTY_POKE
/* kernel build: the osfmk pmap reference/modify routines have no bsd/dev prototypes */
extern void pmap_clear_modify(ppnum_t pn);
extern boolean_t pmap_is_modified(ppnum_t pn);
extern boolean_t pmap_valid_page(ppnum_t pn);
#define FG_ABI_DTY_POKE(p) do { uint32_t v_; do { v_=*(volatile uint32_t *)(p); } while (!__sync_bool_compare_and_swap((volatile uint32_t *)(p),v_,v_)); } while (0)
#endif
static ppnum_t fg_abi_dty_ppn[FG_DTY_CHUNKS*FG_DTY_CHUNK];
static uint8_t fg_abi_dty_used[FG_DTY_CHUNKS];
static unsigned fg_abi_dty_ok; /* 0 untested, 1 the modify bit tracks stores, 2 refused */
/* window stats: pages checked, pages dirty, bytes cleaned, bytes of untracked full cleans, ticks checking, ticks cleaning, ticks of retire IVAC, submits, tracked imports mapped */
static uint64_t fg_abi_dty_st[9];
static int fg_abi_dty_selftest(uintptr_t source, ppnum_t ppn)
{
 pmap_clear_modify(ppn);
 if (pmap_is_modified(ppn)) return 1;
 FG_ABI_DTY_POKE(source); /* same-value atomic store: a user store racing it is not overwritten */
 if (!pmap_is_modified(ppn)) return 2;
 pmap_clear_modify(ppn);
 return pmap_is_modified(ppn) ? 3 : 0;
}
/* Track a freshly mapped import: 1 tracked (pages cleared and cleaned by the caller's clean), 0 not tracked. */
static void fg_abi_dty_attach(unsigned slot, uintptr_t source, uint64_t bytes)
{
 fg_abi_imports[slot].dty_first=fg_abi_imports[slot].dty_pages=0;
 if (!fg_abi_dirty || fg_abi_dty_ok==2 || fg_abi_coh==1) return;
 unsigned pages=(unsigned)(bytes>>PAGE_SHIFT), need=(pages+FG_DTY_CHUNK-1)/FG_DTY_CHUNK, first=FG_DTY_CHUNKS;
 if (!pages || need>FG_DTY_CHUNKS) return;
 for (unsigned c=0,run=0;c<FG_DTY_CHUNKS;c++) {
  run=fg_abi_dty_used[c] ? 0 : run+1;
  if (run==need) { first=c+1-need; break; }
 }
 if (first==FG_DTY_CHUNKS) return;
 ppnum_t *pp=&fg_abi_dty_ppn[first*FG_DTY_CHUNK];
 for (unsigned i=0;i<pages;i++) {
  ppnum_t ppn=pmap_find_phys(kernel_pmap,source+((uintptr_t)i<<PAGE_SHIFT));
  if (!ppn || !pmap_valid_page(ppn)) return;
  pp[i]=ppn;
 }
 if (!fg_abi_dty_ok) {
  int r=fg_abi_dty_selftest(source,pp[0]);
  fg_abi_dty_ok=r ? 2 : 1;
  printf("FUXI GPU ABI: import dirty tracking self-test %s (%d)\n",r ? "refused, full cleans stay" : "passed",r);
  if (r) return;
 }
 for (unsigned i=0;i<pages;i++) pmap_clear_modify(pp[i]);
 for (unsigned c=0;c<need;c++) fg_abi_dty_used[first+c]=1;
 fg_abi_imports[slot].dty_first=first+1; fg_abi_imports[slot].dty_pages=pages;
 fg_abi_dty_st[8]++;
}
static void fg_abi_dty_detach(unsigned slot)
{
 if (!fg_abi_imports[slot].dty_first) return;
 unsigned first=fg_abi_imports[slot].dty_first-1, need=(fg_abi_imports[slot].dty_pages+FG_DTY_CHUNK-1)/FG_DTY_CHUNK;
 for (unsigned c=0;c<need;c++) fg_abi_dty_used[first+c]=0;
 fg_abi_imports[slot].dty_first=fg_abi_imports[slot].dty_pages=0;
}
/* The submit clean of one tracked import: only pages whose modify bit is set. */
static void fg_abi_dty_clean(unsigned slot)
{
 const ppnum_t *pp=&fg_abi_dty_ppn[(fg_abi_imports[slot].dty_first-1)*FG_DTY_CHUNK];
 const uintptr_t source=fg_abi_imports[slot].source;
 const unsigned pages=fg_abi_imports[slot].dty_pages;
 uint64_t t0=rsr("cntpct_el0"), clean=0, dirty=0;
 for (unsigned i=0;i<pages;i++) {
  if (!pmap_is_modified(pp[i])) continue;
  uint64_t c0=rsr("cntpct_el0");
  pmap_clear_modify(pp[i]);
  fg_abi_cache_region(source+((uintptr_t)i<<PAGE_SHIFT),1u<<PAGE_SHIFT,0);
  clean+=rsr("cntpct_el0")-c0; dirty++;
 }
 uint64_t t1=rsr("cntpct_el0");
 fg_abi_dty_st[0]+=pages; fg_abi_dty_st[1]+=dirty; fg_abi_dty_st[2]+=dirty<<PAGE_SHIFT;
 fg_abi_dty_st[4]+=(t1-t0)-clean; fg_abi_dty_st[5]+=clean;
 fg_abi_skipped+=(uint64_t)(pages-dirty)<<PAGE_SHIFT;
 if (dirty) fg_abi_cc_note(fg_abi_imports[slot].permissions,dirty<<PAGE_SHIFT,clean);
}
static void fg_abi_import_cache(int complete)
{
 if (complete && !fg_abi_host_handoff) return;
 __asm__ volatile("dsb sy" ::: "memory");
 if (fg_abi_coh==1) { /* self-test proved CPU/GPU coherence for import-class mappings: no cache maintenance */
  for (unsigned i=0;i<FG_ABI_MAX_IMPORTS;i++) if (fg_abi_imports[i].iova) fg_abi_skipped+=fg_abi_imports[i].bytes;
  fg_abi_host_handoff=0;
  return;
 }
 /* A read-only PTE cannot be written by the GPU, so retirement has no stale
  * lines to drop for it; the submit-side clean still covers every import. */
 for (unsigned i=0;i<FG_ABI_MAX_IMPORTS;i++) if (fg_abi_imports[i].iova && (!complete || (fg_abi_imports[i].permissions&2))) {
  /* GPU_ONLY (NativeAdrenoMapGpuOnly): the client promised no CPU access for the mapped lifetime, the range was cleaned at map and is
   * invalidated at unmap, so neither the submit clean nor the retirement invalidate has anything to do. */
  if (fg_abi_imports[i].gpu_only) { fg_abi_skipped+=fg_abi_imports[i].bytes; if (!complete) fg_abi_cc_go+=fg_abi_imports[i].bytes; continue; }
  /* NativeAdreno b686b90 (MIT), adapted: GPU-written imports are already
   * NC in our SMMU. Opt-in requires NO CPU writes for their mapped lifetime,
   * not merely while a submission is in flight. Clean once at map, then
   * avoid re-cleaning unchanged CPU lines on every indirect PM4 use.
   * Permissions alone do NOT prove CPU immutability: keep default off.
   * Unlike upstream, retain completion IVAC: CPU readers (fallback/pixel
   * oracle) may have cached an older frame. Never clean over GPU writes.
   * Read-only imports may change on CPU between submissions; keep cleaning.
   * An IB list is not a resource list, so keep walking ALL mapped imports. */
  if (!complete && fg_abi_ncimport && (fg_abi_imports[i].permissions&2)) {
   fg_abi_skipped+=fg_abi_imports[i].bytes;
   continue;
  }
  if (!complete && fg_abi_imports[i].dty_first && fg_abi_dty_ok==1) { fg_abi_dty_clean(i); continue; }
  uint64_t c0=rsr("cntpct_el0");
  fg_abi_cache_region(fg_abi_imports[i].source,fg_abi_imports[i].bytes,complete);
  if (!complete) { fg_abi_cc_note(fg_abi_imports[i].permissions,fg_abi_imports[i].bytes,rsr("cntpct_el0")-c0); if (fg_abi_dirty) fg_abi_dty_st[3]+=fg_abi_imports[i].bytes; }
  else fg_abi_dty_st[6]+=rsr("cntpct_el0")-c0;
 }
 __asm__ volatile("dsb sy" ::: "memory");
 if (!complete) fg_abi_dty_st[7]++;
 fg_abi_host_handoff=!complete;
}
void fuxi_gpu_import_stats(uint64_t *cleaned, uint64_t *skipped, uint64_t *invalidated,
    uint64_t *mapped, unsigned *imports)
{
 FG_GUARD;
 *cleaned=fg_abi_cleaned; *skipped=fg_abi_skipped; *invalidated=fg_abi_invalidated;
 *mapped=0; *imports=0;
 for (unsigned i=0;i<FG_ABI_MAX_IMPORTS;i++) if (fg_abi_imports[i].iova) {
  *mapped+=fg_abi_imports[i].bytes; ++*imports;
 }
}
static void fg_abi_map_note(uint64_t iova, uint64_t bytes, uintptr_t source)
{
 ++fg_abi_map_seq;
 for (unsigned i=0;i<FG_ABI_MAX_BOS;i++) if (!fg_abi_maps[i].iova) {
  fg_abi_maps[i].iova=iova; fg_abi_maps[i].bytes=bytes; fg_abi_maps[i].source=source; fg_abi_maps[i].seq=fg_abi_map_seq;
  return;
 }
}
static void fg_abi_map_forget(uint64_t iova, uint64_t bytes)
{
 for (unsigned i=0;i<FG_ABI_MAX_BOS;i++) if (fg_abi_maps[i].iova==iova && fg_abi_maps[i].bytes==bytes) fg_abi_maps[i].iova=0;
}
/* The mapping that holds a GPU address, or NULL. */
static const typeof(fg_abi_maps[0]) *fg_abi_map_at(uint64_t iova)
{
 for (unsigned i=0;i<FG_ABI_MAX_BOS;i++)
  if (fg_abi_maps[i].iova && iova>=fg_abi_maps[i].iova && iova-fg_abi_maps[i].iova<fg_abi_maps[i].bytes) return &fg_abi_maps[i];
 return NULL;
}
static volatile uint64_t *fg_abi_leaf(uint64_t iova)
{
 unsigned chunk = (unsigned)((iova-FG_ABI_VA_START)>>21);
 return (volatile uint64_t *)G_PTR_RANGE(FG_ABI_PT+(4u+chunk)*4096,4096)+((iova>>12)&511);
}
/* Cached mirror of which client 4K leaves are live. Only map/unmap/init write client leaves, so the overlap test reads this
 * instead of NC page-table memory (UniXNU ArmSmmu4K: no existing-range pass for caller-owned pages). */
static uint64_t fg_abi_used[(FG_ABI_VA_SIZE>>12)/64];
static inline unsigned fg_abi_pg(uint64_t iova) { return (unsigned)((iova-FG_ABI_VA_START)>>12); }
static inline int fg_abi_used_get(unsigned pg) { return (fg_abi_used[pg>>6]>>(pg&63))&1; }
/* Whole-range forms, a word at a time: any live leaf in [pg,pg+n), and set/clear [pg,pg+n). */
static inline uint64_t fg_abi_used_mask(unsigned pg, unsigned n)
{
 unsigned lo=pg&63;
 return (n==64 ? ~UINT64_C(0) : ((UINT64_C(1)<<n)-1))<<lo;
}
static inline int fg_abi_used_any(unsigned pg, unsigned n)
{
 for (unsigned k;n;pg+=k,n-=k) { k=64-(pg&63); if (k>n) k=n; if (fg_abi_used[pg>>6]&fg_abi_used_mask(pg,k)) return 1; }
 return 0;
}
static inline void fg_abi_used_fill(unsigned pg, unsigned n, int on)
{
 for (unsigned k;n;pg+=k,n-=k) {
  k=64-(pg&63); if (k>n) k=n;
  if (on) fg_abi_used[pg>>6]|=fg_abi_used_mask(pg,k); else fg_abi_used[pg>>6]&=~fg_abi_used_mask(pg,k);
 }
}
/* Ranged invalidate of a small unmap (opt-in value 128): TLBIVAL (+0x620, AArch64 format VA[55:12]|ASID<<48) per removed
 * 4K leaf, then TLBSYNC. Leaf-only: the L1/L2 tables are fixed for the life of the root. Same status codes as g4_tlb. */
#define FG_ABI_TLB_PAGE_LIMIT 512u
static int g3_sw64(unsigned off, uint64_t v) { return !fg_access_failed && !fg_access(GPU_SMMU_BASE+off,&v,1,1); }
static int fg_abi_tlb_pages(uint64_t iova, uint64_t bytes)
{
 unsigned cb=g_cb(g3_bank);
 __asm__ volatile("dsb sy" ::: "memory");
 for (uint64_t off=0;off<bytes;off+=4096)
  if (!g3_sw64(cb+0x620,(((iova+off)>>12)&((UINT64_C(1)<<44)-1))|((uint64_t)g3_asid<<48))) return 0xe01;
 if (!g3_sw(cb+0x7f0,0)) return 0xe01;
 for (unsigned i=0;i<1000;i++) {
  uint32_t st=g3_sr(cb+0x7f4);
  if (fg_access_failed) return 0xe01;
  if (!(st&1)) return 0;
  delay(1);
 }
 return 0xe03;
}
static int fg_abi_switch(uint64_t root)
{
 /* Caller proves ring and both threads idle. Invalidate the common ASID
  * before permitting another doorbell; preserved ring/SQE PTEs are equal. */
 unsigned cb = g_cb(g3_bank);
 uint64_t ttbr = root | ((uint64_t)g3_asid<<48);
 __asm__ volatile("dsb sy" ::: "memory");
 if (!g3_sw(cb+0x20,(uint32_t)ttbr) || !g3_sw(cb+0x24,ttbr>>32)) return 0;
 uint32_t tlb[128] = {0};
 return !g4_tlb(tlb) && !fg_access_failed &&
     g3_sr(cb+0x20)==(uint32_t)ttbr && g3_sr(cb+0x24)==(uint32_t)(ttbr>>32);
}
/* 16 words of the mapped range that holds `at`, from `start` (clamped to the range): the CP stops on a bad word a few before the one it
 * reports (NativeAdreno peekIb/peekFetch, MIT). Fault-contained reads of the BO's kernel mapping; no register access. */
static void fg_abi_peek(const char *tag, uint64_t at, uint64_t start)
{
 const typeof(fg_abi_maps[0]) *m=fg_abi_map_at(at);
 if (!m) { printf("FUXI GPU STALL: %s IB %#llx not in a mapped range\n",tag,(unsigned long long)at); return; }
 if (start<m->iova || start>at) start=m->iova;
 uint64_t room=(m->iova+m->bytes-start)/4;
 uint32_t w[16]; unsigned bad=0;
 for (unsigned i=0;i<16;i++) {
  uint64_t v=0;
  if (i>=room || fuxi_gpu_access(m->source+(start-m->iova)+4*i,&v,0,0)) { w[i]=0xdeadbeef; bad++; } else w[i]=(uint32_t)v;
 }
 printf("FUXI GPU STALL: %s at %#llx from %#llx map %#llx seq %u/%u unread %u: %08x %08x %08x %08x %08x %08x %08x %08x %08x %08x %08x %08x %08x %08x %08x %08x\n",
     tag,(unsigned long long)at,(unsigned long long)start,(unsigned long long)m->iova,m->seq,fg_abi_submit_map_seq,bad,
     w[0],w[1],w[2],w[3],w[4],w[5],w[6],w[7],w[8],w[9],w[10],w[11],w[12],w[13],w[14],w[15]);
}
/* The CP's state for a job that stopped advancing (NativeAdreno NativeAdrenoSubmit::noteStall, MIT): RBBM_STATUS..3 and INT_0, CP_HW_FAULT,
 * CP_STATUS_1, CP_PROTECT_STATUS, the BR thread's RPTR/WPTR, IB1 base/remaining/initial size, IB2 base/remaining, the BV thread's RPTR,
 * IB1 base lo, IB1 and IB2 remaining and IB2 base; the SMMU context bank's fault record; and the words under the IBs. Read only. A
 * mapping whose sequence (seq a/b: a) is above the submission's (b) was mapped after it, so its memory was freed and reused under the
 * GPU. Offsets are NativeAdreno's; not checked on this device (not yet observed). */
static void fg_abi_snapshot(const char *why)
{
 static const unsigned regs[]={0x210,0x211,0x212,0x213,0x201,0x821,0x825,0x824,0x806,0x807,0x928,0x929,0x92a,0x943,0x92b,0x92c,0x92d,
     0xa66,0xa6d,0xa70,0xa74,0xa71,0xa72};
 uint32_t v[sizeof(regs)/sizeof(regs[0])];
 for (unsigned i=0;i<sizeof(regs)/sizeof(regs[0]);i++) v[i]=g3_gr(regs[i]);
 unsigned cb=g_cb(g3_bank);
 printf("FUXI GPU STALL: %s seq %u rbbm %08x %08x %08x %08x int %08x hwfault %08x cpst1 %08x prot %08x rptr %x wptr %x\n",why,fg_abi_seq,
     v[0],v[1],v[2],v[3],v[4],v[5],v[6],v[7],v[8],v[9]);
 printf("FUXI GPU STALL: ib1 %08x:%08x rem %x init %x ib2 %08x:%08x rem %x bv rptr %x ib1 lo %08x rem %x ib2 %08x:%08x rem %x\n",
     v[11],v[10],v[12],v[13],v[15],v[14],v[16],v[17],v[18],v[19],v[22],v[21],v[20]);
 printf("FUXI GPU STALL: smmu fsr %08x far %08x:%08x fsynr0 %08x\n",g3_sr(cb+0x58),g3_sr(cb+0x64),g3_sr(cb+0x60),g3_sr(cb+0x68));
 const uint64_t ib1=(uint64_t)v[11]<<32|v[10], bv1=(uint64_t)v[11]<<32|v[18], bv2=(uint64_t)v[22]<<32|v[21];
 const uint32_t reached=v[13]>=v[12] ? v[13]-v[12] : 0;
 const uint64_t br=ib1+4ull*(reached ? reached-1 : 0);
 fg_abi_peek("BR-IB1",br,br-48);
 fg_abi_peek("BV-IB1",bv1,bv1-48);
 fg_abi_peek("BV-IB2",bv2,bv2-48);
}
/* One bounded, read-only snapshot before isolation destroys CP/SMMU evidence.
 * GPU offsets are A6XX/A7XX dwords from Mesa a6xx.xml; SMMU offsets are
 * bytes (same context-bank registers as g3_diagnostics). No indexed-port
 * walks, retry loops, status clears or fault-resume writes on this path. */
static unsigned fg_abi_hang_dumped;
static void fg_abi_hang_dump(const char *why)
{
 if (fg_abi_hang_dumped) return;
 fg_abi_hang_dumped=1;
 printf("FUXI GPU HANG: %s seq %u completed %u busy %u expected-wptr 0x%x\n",
     why,fg_abi_seq,fg_abi_completed,fg_client_busy,g3_wptr);
 static const struct { const char *name; unsigned reg; } regs[] = {
  {"CP_RB_RPTR",0x806},{"CP_RB_WPTR",0x807},
  {"CP_IB1_BASE_LO",0x928},{"CP_IB1_BASE_HI",0x929},
  {"CP_IB1_REM_SIZE",0x92a},{"CP_IB1_INIT_SIZE",0x943},
  {"CP_STATUS_1",0x825},{"CP_HW_FAULT",0x821},
  {"CP_INTERRUPT_STATUS",0x823},{"CP_PROTECT_STATUS",0x824},
  {"RBBM_STATUS",0x210},{"RBBM_STATUS3",0x213},{"RBBM_INT_0_STATUS",0x201},
 };
 for (unsigned i=0;i<sizeof(regs)/sizeof(regs[0]);i++)
  printf("FUXI GPU HANG: %s [0x%x]=0x%x\n",regs[i].name,regs[i].reg,g3_gr(regs[i].reg));
 unsigned cb=g_cb(g3_bank);
 printf("FUXI GPU HANG: SMMU GPU CB %u TTBR 0x%x:%08x FSR 0x%x FAR 0x%x:%08x FSYNR0 0x%x FSYNR1 0x%x GFSR 0x%x\n",
     g3_bank,g3_sr(cb+0x24),g3_sr(cb+0x20),g3_sr(cb+0x58),
     g3_sr(cb+0x64),g3_sr(cb+0x60),g3_sr(cb+0x68),g3_sr(cb+0x6c),g3_sr(0x48));
 printf("FUXI GPU HANG: MMIO-access-failed %u; isolating\n",fg_access_failed);
 fg_abi_snapshot("hang");
}
static int fg_abi_fault(const char *why)
{
 /* Queue mode: jobs the CP already finished (their fence is in memory; the ring is in order) stay successful, so a fault in a later
  * job fails only the jobs from it on. */
 if (fg_abi_queue && fg_client_busy && !fg_access_failed) {
  uint32_t f=g3_read(G3_FENCE_PA+FG_ABI_FENCE_OFF+4);
  if (!fg_access_failed && f>fg_abi_completed && f<=fg_abi_seq) fg_abi_completed=f;
 }
 fg_abi_hang_dump(why);
 fg_abi_poisoned = 1; fg_pk_n = 0; /* a parked job never reaches the ring; its fence fails with the client */
 fg_fail(why,0x701);
 fg_abi_drained = fg_isolate();
 if (!fg_abi_drained && fg_scanout_owned) (void)fg_scanout_stop("GPU DMA drain failed");
 if (fg_abi_drained) fg_abi_import_cache(1);
 fg_client_busy = 0;
 return fg_abi_drained ? -1 : -2;
}
/* Inline present copy (fuxigpuncimport bit 21, needs 512): VA 0x90000000.. and 0xa0000000.. are above the client's allocatable range
 * (it ends at 0x80000000), so the client root gets its own L1 entry [2] for a window holding the present sources (the very L3 tables
 * and 2 MiB blocks of the present root, mirrored descriptor by descriptor by fuxi_gpu_map/unmap) and the A/B/C carveout destinations
 * (NC, as in the present root). The client root then stays installed after its jobs retire, so a present copy is emitted into the same
 * in-order ring with no TTBR switch and no wait for the client, and a client submit is not refused for a copy. One client root exists
 * (the user client admits one connection), so there is no other client to fall back for. The copy has its own fence slot (+64). */
#define FG_INL_L2 (FG_ABI_PT+0x304000)
#define FG_INL_L3 (FG_ABI_PT+0x305000)
#define FG_INL_TABLES 15u
_Static_assert(FG_INL_L2 >= FG_ABI_PT+(4u+768u)*4096 && FG_INL_L3+FG_INL_TABLES*4096 <= FG_ABI_PT+0x3f0000, "inline window tables overlap");
_Static_assert(FG_INL_FB_VA >= FG_ABI_VA_START+FG_ABI_VA_SIZE && FG_INL_SRC_VA >= FG_ABI_VA_START+FG_ABI_VA_SIZE && (FG_INL_SRC_VA>>30) == 2 && (FG_INL_FB_VA>>30) == 2, "inline window inside client VA");
static void fg_inl_mirror(unsigned idx, uint64_t v)
{
 if (fg_inl_ready != 1) return;
 ((volatile uint64_t *)G_PTR_RANGE(FG_INL_L2,4096))[((FG_INL_SRC_VA>>21)&511)+idx] = v;
}
static int fg_inl_usable(void) { return fg_abi_inline && fg_inl_ready == 1 && fg_root_client && fg_abi_live && !fg_abi_poisoned; }
/* Under the GPU lock, before the first client root install (idle GPU, present root installed). Idempotent; 2 = failed, old path. */
static int fg_inl_setup(void)
{
 if (fg_inl_ready) return fg_inl_ready == 1;
 fg_inl_ready = 2;
 if (!fg_abi_inline || !fg_abi_queue) return 0;
 volatile uint64_t *root = (volatile uint64_t *)G_PTR_RANGE(FG_ABI_PT,4096);
 volatile uint64_t *l2 = (volatile uint64_t *)G_PTR_RANGE(FG_INL_L2,4096);
 if (root[2] || !fg_l2()) return 0;
 g_zero(FG_INL_L2, (1+FG_INL_TABLES)*4096);
 static const uint64_t pa[3] = {G4_FB_PA, 0xb8a00000ULL, 0xb9400000ULL};
 unsigned want = 1u | (fg_scanout_double_requested ? 2u : 0) | (fg_scanout_triple_requested ? 4u : 0);
 _Static_assert(G4_FB_MAP <= 0xa00000u, "framebuffer exceeds its 10 MiB window");
 for (unsigned b = 0; b < 3; b++) {
  if (!(want & (1u<<b))) continue;
  for (unsigned pg = 0; pg < G4_FB_MAP/4096; pg++) {
   unsigned t = b*5+(pg>>9);
   volatile uint64_t *l3 = (volatile uint64_t *)G_PTR_RANGE(FG_INL_L3+(uint64_t)t*4096,4096);
   l2[((FG_INL_FB_VA>>21)&511)+t] = (FG_INL_L3+(uint64_t)t*4096) | 3;
   l3[pg&511] = (pa[b]+(uint64_t)pg*4096) | 3 | (3u<<8) | (1u<<10) | (1u<<6);
  }
 }
 volatile uint64_t *present = fg_l2();
 for (unsigned i = 0; i < FG_CACHE_SLOTS*FG_SOURCE_L3; i++)
  l2[((FG_INL_SRC_VA>>21)&511)+i] = present[((FG_SOURCE_VA>>21)&511)+i];
 __asm__ volatile("dsb sy" ::: "memory");
 root[2] = FG_INL_L2 | 3;
 __asm__ volatile("dsb sy" ::: "memory");
 fg_inl_fbmask = want; fg_inl_ready = 1;
 printf("FUXI GPU: inline copy window ready (sources %#x, carveout %#x, buffers %#x)\n", FG_INL_SRC_VA, FG_INL_FB_VA, want);
 return 1;
}
/* A classic (present root) copy or a failed window: back to the present root. Needs an idle GPU; 0 means not now. */
static int fg_inl_to_present(void)
{
 if (!fg_root_client) return 1;
 if (fg_pk_n) { (void)fg_pk_flush(FG_PK_OTHER); return 0; } /* a parked job needs the client root: ring it, switch when idle */
 if (fg_client_busy || fg_busy) return 0;
 if (g3_faulted() || g3_gr(0x806) != g3_wptr || g3_gr(0x807) != g3_wptr || (g3_gr(0x210) & ~1u)) return 0;
 if (!fg_abi_switch(G3_PT)) { (void)fg_abi_fault("inline restore present TTBR"); return 0; }
 fg_root_client = 0;
 return 1;
}
static void fg_abi_stall_log(const char *what)
{
 if (fg_abi_stall_lines<24) { fg_abi_stall_lines++; printf("FUXI GPU STALL: %s seq %u\n",what,fg_abi_seq); }
}
/* The job that stalled completed after a recovery stage (1 kick, 2 bus drain). */
static void fg_abi_stall_done(void)
{
 fg_abi_stall_stats[fg_abi_stage==1 ? 5 : 6]++;
 fg_abi_stall_log(fg_abi_stage==1 ? "completed after WPTR kick" : "completed after bus drain");
 fg_abi_stage=0;
}
/* Opt-in stall recovery (fuxigpuncimport bit 2; NativeAdreno noteStall, MIT), staged by how long the job has been pending (ordinary jobs
 * take about 5 ms; the 100 ms deadline below still ends the job): at 30 ms snapshot the CP, drop the ASID's translations and give WPTR
 * again (harmless when the CP already saw it); at 60 ms drain the bus the way a6xx does before a reset, then release the halt (the
 * isolation path keeps its halt). Returns nonzero when a register access failed. Not yet observed on device. */
static int fg_abi_unstick_step(void)
{
 const uint64_t freq=rsr("cntfrq_el0"), pending=rsr("cntpct_el0")-(fg_client_deadline-freq/10);
 if (fg_abi_stage==0 && pending>=freq*30/1000) {
  fg_abi_stage=1; fg_abi_stall_stats[0]++; fg_abi_stall_stats[1]=fg_abi_seq;
  fg_abi_stall_log("pending 30 ms: snapshot, TLB drop, WPTR kick");
  if (fg_abi_stall_stats[7]<8) { fg_abi_stall_stats[7]++; fg_abi_snapshot("30ms"); }
  uint32_t tlb[128]={0};
  if (g4_tlb(tlb) || fg_access_failed) return 1;
  if (!fg_ring_doorbell() || fg_access_failed) return 1;
 }
 if (fg_abi_stage==1 && pending>=freq*60/1000) {
  uint64_t ack[2]={0,0};
  fg_abi_stage=2; fg_abi_stall_stats[2]++;
  fg_abi_stall_log("pending 60 ms: bus drain");
  if (!g3_gw(0x3c45,1)) return 1;
  for (unsigned i=0;i<1000 && !(ack[0]&1);i++) { ack[0]=g3_gr(0x3c46); if (!(ack[0]&1)) delay(1); }
  if (!g3_gw(0x3c45,3)) return 1;
  for (unsigned i=0;i<1000 && !(ack[1]&2);i++) { ack[1]=g3_gr(0x3c46); if (!(ack[1]&2)) delay(1); }
  if (!g3_gw(0x3c45,0) || fg_access_failed) return 1;
  fg_abi_stall_stats[3]=(uint32_t)ack[0]&1; fg_abi_stall_stats[4]=((uint32_t)ack[1]>>1)&1;
 }
 return 0;
}
/* No spinning: one observation. The watchdog runs even if the client exits,
 * never waits, or asks WaitFence(timeout_ns=0) indefinitely. */
static int fg_abi_observe(void)
{
 if (fg_abi_poisoned) return fg_abi_timed_out ? (fg_abi_drained ? -3 : -4) : (fg_abi_drained ? -1 : -2);
 if (!fuxi_gpu_ready()) return -1;
 if (fg_access_failed || g3_faulted()) return fg_abi_fault("client GPU fault");
 if (fg_pk_n) { int r=fg_pk_check(); if (r) return r; } /* bit 27: release a parked job whose copy is not coming */
 if (!fg_client_busy) return 0;
 /* Inline root (bit 21): the root stays, and a present copy may sit behind this job, so its fence alone retires it. */
 if (g3_read(G3_FENCE_PA+FG_ABI_FENCE_OFF+4)==fg_abi_seq &&
     (fg_root_client || (g3_gr(0x806)==g3_wptr && g3_gr(0x807)==g3_wptr && !(g3_gr(0x210)&~1u)))) {
  if (!fg_root_client && !fg_abi_switch(G3_PT)) return fg_abi_fault("client restore TTBR");
  fg_abi_import_cache(1); /* before fence success, even without a user wait */
  if (fg_abi_stage) fg_abi_stall_done();
  fg_abi_completed = fg_abi_seq;
  FG_ABI_WAKE(); fg_client_busy = 0; if (!fg_abi_hold) fg_abi_present_wait = 0; /* hold: the waiting present, not the next client job, takes the idle GPU */
  return 0;
 }
 /* Queue mode: the ring is in order with a WFI after every frame, so a fence below the newest job's means every job up to it has
  * finished while the next runs. Retire those (cache handoff included) and give the next job its own 100 ms. */
 { uint32_t f;
   if (fg_abi_queue && (f=g3_read(G3_FENCE_PA+FG_ABI_FENCE_OFF+4))>fg_abi_completed && f<fg_abi_seq) {
    fg_abi_import_cache(1); fg_abi_host_handoff=1; /* the newer job's handoff is still open */
    if (fg_abi_stage) fg_abi_stall_done();
    fg_abi_completed=f; fg_client_deadline=rsr("cntpct_el0")+rsr("cntfrq_el0")/10;
    return 1;
   } }
 if (rsr("cntpct_el0") >= fg_client_deadline) {
  fg_abi_timed_out=1; (void)fg_abi_fault("client fence deadline (100 ms)");
  return fg_abi_drained ? -3 : -4;
 }
 if (fg_abi_unstick && fg_abi_unstick_step()) return fg_abi_fault("client stall recovery access");
 return 1;
}
static void fg_abi_watch(thread_call_param_t arg, thread_call_param_t unused)
{
 (void)arg; (void)unused;
 FG_GUARD;
 if (fg_abi_observe()==1 || fg_pk_n) { /* a parked job keeps the watchdog: fg_pk_check runs from here when no one waits */
  uint64_t deadline;
  clock_interval_to_deadline(2,NSEC_PER_MSEC,&deadline);
  (void)thread_call_enter_delayed(&fg_client_watchdog,deadline);
 }
}
static int fg_abi_init(void)
{
 unsigned bits=0;
 (void)PE_parse_boot_argn("fuxigpuncimport",&bits,sizeof(bits));
 fg_abi_ncimport=bits&1; fg_wptr_fence=(bits>>1)&1; fg_abi_unstick=(bits>>2)&1; fg_abi_cohtest=(bits>>6)&1; fg_abi_tlbpages=(bits>>7)&1; fg_abi_mapwait=(bits>>8)&1; fg_abi_queue=(bits>>9)&1; fg_abi_roimport=(bits>>10)&1; fg_abi_pace=(bits>>11)&1; fg_abi_hold=(bits>>12)&1; fg_abi_start=(bits>>13)&1; fg_abi_probe=(bits>>14)&1; fg_abi_spin=(bits>>15)&1; fg_abi_mid=(bits>>16)&1; fg_abi_ar=(bits>>17)&1; fg_abi_hard=(bits>>18)&1; fg_abi_dirty=(bits>>19)&1; fg_abi_inline=((bits>>21)&1) && ((bits>>9)&1); fg_abi_cf=fg_abi_inline && ((bits>>4)&1); fg_abi_pk=fg_abi_cf && ((bits>>27)&1); fg_irq_want=(bits>>26)&1; fg_cf_reset(); fg_pk_reset(); fg_inl_ready=fg_root_client=fg_inl_busy=fg_inl_fbmask=0; fg_abi_hold_since=0; fg_abi_present_wait=0; memset(fg_abi_q_stats,0,sizeof(fg_abi_q_stats));
 if (fg_abi_pk) printf("FUXI GPU: copy_first_park=1 idle_us=%u cap=2 periods (fuxigpuncimport 134217728 with 16, inline mode)\n", FG_PK_IDLE_US);
 fg_abi_coh=fg_abi_coh_tries=0;
 for (unsigned i=0;i<sizeof(fg_abi_used)/sizeof(fg_abi_used[0]);i++) fg_abi_used[i]=0;
 /* 772 4K pages: root, two L2s, kernel runtime L3, 768 client L3s.
  * Allocate all table storage from unused reserved NC scratch, never VM.
  * No existing GMU or present PTE is reused as mutable client storage. */
 /* Snapshot once before checking: do not short-circuit away the evidence.
  * GPU offsets are dwords; SMMU_ID2 is a byte offset. The UCHE address
  * registers are adjacent 32-bit low/high pairs, including on A740.
  * Address low words have 4K granularity: A740 reads GMEM MAX as
  * 0x12ff000 after writing inclusive end 0x12fffff. Compare page addresses;
  * high words, NC mode and the extracted HBB field remain exact. */
 static const struct { const char *name; unsigned reg; uint32_t want, mask; } checks[] = {
  {"UCHE_WRITE_THRU_BASE_LO",0xe07,0xfffff000u,0xfffff000u},
  {"UCHE_WRITE_THRU_BASE_HI",0xe08,0x1ffff,UINT32_MAX},
  {"UCHE_TRAP_BASE_LO",0xe09,0xfffff000u,0xfffff000u},
  {"UCHE_TRAP_BASE_HI",0xe0a,0x1ffff,UINT32_MAX},
  {"UCHE_GMEM_RANGE_MIN_LO",0xe0b,0x1000000,0xfffff000u},
  {"UCHE_GMEM_RANGE_MIN_HI",0xe0c,0,UINT32_MAX},
  {"UCHE_GMEM_RANGE_MAX_LO",0xe0d,0x12fffff,0xfffff000u},
  {"UCHE_GMEM_RANGE_MAX_HI",0xe0e,0,UINT32_MAX},
  {"UCHE_MODE_CNTL_HBB",0xe01,3,UINT32_MAX},
  {"RBBM_NC_MODE_CNTL",0x534,1,UINT32_MAX},
 };
 uint32_t values[sizeof(checks)/sizeof(checks[0])];
 for (unsigned i=0;i<sizeof(checks)/sizeof(checks[0]);i++) values[i]=g3_gr(checks[i].reg);
 uint32_t id2=g3_sr(SMMU_ID2);
 static unsigned dumped;
 if (!dumped) {
  dumped=1;
  for (unsigned i=0;i<sizeof(checks)/sizeof(checks[0]);i++)
   printf("FUXI GPU ABI: init regs %s [0x%x]=0x%x\n",checks[i].name,checks[i].reg,values[i]);
  printf("FUXI GPU ABI: init regs SMMU_ID2 [0x%x]=0x%x\n",SMMU_ID2,id2);
 }
 if (fg_access_failed) {
  printf("FUXI GPU ABI: init check register_access got 0x%x want 0x0\n",fg_access_failed);
  return 0;
 }
 for (unsigned i=0;i<sizeof(checks)/sizeof(checks[0]);i++) {
  uint32_t got=checks[i].reg==0xe01 ? (values[i]>>21)&3 : values[i];
  if ((got&checks[i].mask)!=(checks[i].want&checks[i].mask)) {
   printf("FUXI GPU ABI: init check %s got 0x%x want 0x%x\n",checks[i].name,got,checks[i].want);
   return 0;
  }
 }
 if (((id2>>4)&15)<1) {
  printf("FUXI GPU ABI: init check SMMU_ID2_OAS_min got 0x%x want 0x1\n",(id2>>4)&15);
  return 0;
 }
 /* SM8550 UBWC4 configuration, same NC values as stage14 (also run by
  * stage16). GX OOB stays held; fg_state is not ready, so no present can
  * race these CPU MMIO writes. CP protection applies to packets, not AHB.
  * Host PIPE bits 13:12 select BR=1/BV=2; restore default aperture last.
  * g3_gw checks access faults, not value readback, so NC/aperture writes
  * impose no exact-read assumption. Tables below are NC scratch stores,
  * not register readback. TTBR switching checks an aligned PA plus ASID. */
 static const struct { const char *name; unsigned reg; uint32_t value; } writes[] = {
  {"G4_RB_NC",G4_RB_NC,0x816},
  {"G4_TPL1_NC",G4_TPL1_NC,6},
  {"G4_SP_NC",G4_SP_NC,0x26},
  {"G3_APERTURE_HOST_BR",G3_APERTURE_HOST,1u<<12},
  {"G4_GRAS_NC_BR",G4_GRAS_NC,0x60},
  {"G3_APERTURE_HOST_BV",G3_APERTURE_HOST,2u<<12},
  {"G4_GRAS_NC_BV",G4_GRAS_NC,0x60},
  {"G3_APERTURE_HOST_DEFAULT",G3_APERTURE_HOST,0},
 };
 for (unsigned i=0;i<sizeof(writes)/sizeof(writes[0]);i++) {
  if (!g3_gw(writes[i].reg,writes[i].value)) {
   printf("FUXI GPU ABI: init write %s [0x%x]=0x%x failed\n",writes[i].name,writes[i].reg,writes[i].value);
   return 0;
  }
 }
 g_zero(FG_ABI_PT,FG_ABI_PT_BYTES);
 volatile uint64_t *root = (volatile uint64_t *)G_PTR_RANGE(FG_ABI_PT,4096);
 volatile uint64_t *l20 = (volatile uint64_t *)G_PTR_RANGE(FG_ABI_PT+4096,4096);
 volatile uint64_t *l21 = (volatile uint64_t *)G_PTR_RANGE(FG_ABI_PT+8192,4096);
 volatile uint64_t *runtime = (volatile uint64_t *)G_PTR_RANGE(FG_ABI_PT+12288,4096);
 root[0]=(FG_ABI_PT+4096)|3; root[1]=(FG_ABI_PT+8192)|3;
 l20[G3_SQE_VA>>21]=(FG_ABI_PT+12288)|3;
 for (unsigned i=0;i<0x2a000/4096;i++) {
  uint64_t pa = G3_SQE_PA+(uint64_t)i*4096;
  unsigned ap = i<0x20 ? 3u<<6 : i==0x28 ? 0 : 1u<<6;
  runtime[i]=pa|3|(3u<<8)|(1u<<10)|ap;
 }
 for (unsigned i=0;i<768;i++) {
  uint64_t va=FG_ABI_VA_START+(uint64_t)i*0x200000;
  volatile uint64_t *l2=va>>30 ? l21 : l20;
  l2[(va>>21)&511]=(FG_ABI_PT+(4u+i)*4096)|3;
 }
 g_wr(G3_FENCE_PA,FG_ABI_FENCE_OFF+4,0);
 g_wr(G3_FENCE_PA,FG_ABI_FENCE_OFF+12,0);
 __asm__ volatile("dsb sy" ::: "memory");
 thread_call_setup_with_options(&fg_client_watchdog,fg_abi_watch,NULL,
     THREAD_CALL_PRIORITY_KERNEL,THREAD_CALL_OPTIONS_ONCE);
 fg_abi_live=1;
 return 1;
}
int fuxi_gpu_abi_available(void)
{
 FG_GUARD;
 return fg_abi_live && fuxi_gpu_ready() && !fg_abi_poisoned;
}
int fuxi_gpu_abi_timestamp(uint64_t *ticks)
{
 FG_GUARD;
 if (!ticks) return -2;
 *ticks=0;
 if (!fg_abi_live || !fuxi_gpu_ready() || fg_abi_poisoned) return -1;
 /* A6XX/A7XX CP_ALWAYS_ON_COUNTER, dword indices (not CPU timer ticks).
  * High/low/high avoids torn rollover; NativeAdrenoTimer uses eight tries. */
 for (unsigned attempt=0;attempt<8;attempt++) {
  uint32_t high=g3_gr(0x981), low=g3_gr(0x980), again=g3_gr(0x981);
  if (fg_access_failed) { (void)fg_abi_fault("client timestamp MMIO"); return -2; }
  if (high==again) { *ticks=((uint64_t)high<<32)|low; return 0; }
 }
 return -3;
}
int fuxi_gpu_abi_status(uint64_t *faults, uint64_t *suspends)
{
 FG_GUARD;
 if (!faults || !suspends) return -1;
 *faults=*suspends=0;
 if (!fg_abi_live) return -1;
 /* Observe even without WaitFence; after isolation never touch powered-down
  * MMIO. Returning success with the changed count is Turnip's device loss. */
 if (fuxi_gpu_ready()) (void)fg_abi_observe();
 *faults=fg_faults; *suspends=fg_suspends;
 return 0;
}
/* 16K XNU pages: one pmap walk per page (not per 4K leaf), kept for the write pass. */
static ppnum_t fg_abi_ppns[(FG_ABI_MAX_BYTES>>PAGE_SHIFT)+2];
/* Idle test for map/unmap/submit. A pending job is observed once instead of
 * waiting for the 2 ms watchdog, so a caller that polls sees retirement at
 * once. 0 idle, 1 still running, -1 failed/poisoned. */
static int fg_abi_wait_idle(void)
{
 if (!fg_client_busy) return 0;
 int s=fg_abi_observe();
 return s<0 ? -1 : s>0 ? 1 : 0;
}
static int fg_abi_map_locked(uint64_t iova, uintptr_t source, uint64_t bytes,
    unsigned permissions, unsigned wb, unsigned sh)
{
 if (!fg_abi_live || !fuxi_gpu_ready() || fg_abi_poisoned) return -1;
 /* A map only turns invalid leaves of never-referenced VA into valid ones (see the used-bit check below), so a running job is
  * neither affected nor needs to be waited for: wait_idle made every map during a ~9 ms job (about 40% of the time) sleep out the
  * rest of it. One observation still retires a finished job and still isolates a faulted one. Unmap and submit keep the idle rule.
  * Invariant: a VA with its used bit clear has no TLB entry (only unmap clears it, and unmap invalidates the ASID, or each
  * removed leaf, before returning; invalid leaves are not cached) and every submit switches TTBR with a full ASID invalidate
  * before its doorbell, so no TLBI is needed here. fuxigpuncimport 256 restores the wait. */
 if (fg_abi_mapwait) { int idle=fg_abi_wait_idle(); if (idle) return idle; }
 else if (fg_client_busy && fg_abi_observe()<0) return -1;
 if (!fg_abi_map_valid(iova,bytes,permissions,wb,sh,0) || !source || (source&4095) || bytes>FG_ABI_MAX_BYTES) return -1;
 /* Validate the entire range before publishing any leaf. 16K XNU pages
  * provide four distinct 4K SMMU pages, including noncontiguous BOs. A 2 MiB
  * chunk's 512 leaves are contiguous, so the table pointer is built per chunk. */
 uintptr_t base=source>>PAGE_SHIFT;
 volatile uint64_t *leaf=NULL;
 for (uint64_t off=0;off<bytes;off+=4096) {
  if (!off || !((source+off)&PAGE_MASK)) {
   ppnum_t ppn=pmap_find_phys(kernel_pmap,source+off);
   if (!ppn || ((uint64_t)ppn<<PAGE_SHIFT)>=(1ULL<<FG_ABI_PA_BITS)) return -1;
   fg_abi_ppns[((source+off)>>PAGE_SHIFT)-base]=ppn;
  }
 }
 if (fg_abi_used_any(fg_abi_pg(iova),(unsigned)(bytes>>12))) return -1;
 for (uint64_t off=0;off<bytes;off+=4096) {
  if (!leaf || !((iova+off)&0x1fffff)) leaf=fg_abi_leaf(iova+off); else leaf++;
  uint64_t pa=((uint64_t)fg_abi_ppns[((source+off)>>PAGE_SHIFT)-base]<<PAGE_SHIFT)|((source+off)&PAGE_MASK);
  *leaf=pa|3|(permissions==3 ? 1u<<6 : 3u<<6)|(wb ? 1u<<2 : 0)|sh<<8|1u<<10;
 }
 fg_abi_used_fill(fg_abi_pg(iova),(unsigned)(bytes>>12),1);
 __asm__ volatile("dsb sy" ::: "memory");
 fg_abi_map_note(iova,bytes,source);
 /* Root is inactive; TLBI is mandatory before its next doorbell. */
 return 0;
}
int fuxi_gpu_abi_map(uint64_t iova, uintptr_t source, uint64_t bytes,
    unsigned permissions, unsigned wb, unsigned sh)
{
 FG_GUARD;
 uint64_t t=mach_absolute_time();
 int s=fg_abi_map_locked(iova,source,bytes,permissions,wb,sh);
 fg_abi_map_work=mach_absolute_time()-t;
 return s;
}
uint64_t fuxi_gpu_abi_map_work(void) { return fg_abi_map_work; }
static void fg_abi_coh_probe(void);
int fuxi_gpu_abi_map_import(uint64_t iova, uintptr_t source, uint64_t bytes, unsigned permissions)
{
 FG_GUARD;
 unsigned gpu_only=permissions&FG_ABI_IMPORT_GPU_ONLY; /* internal flag of the userclient, not a PTE permission */
 permissions&=~FG_ABI_IMPORT_GPU_ONLY;
 if (!source || !bytes || ((source|bytes)&(NativeAdrenoAbiHostPageBytes-1)) ||
     bytes>FG_ABI_MAX_BYTES || source+bytes<source) return -1;
 if (fg_abi_cohtest && !fg_abi_coh) fg_abi_coh_probe(); /* first import = first use; runs before this range is mapped */
 unsigned slot;
 for (slot=0;slot<FG_ABI_MAX_IMPORTS && fg_abi_imports[slot].iova;slot++) {}
 if (slot==FG_ABI_MAX_IMPORTS) return -1;
 int s=fg_abi_map_locked(iova,source,bytes,permissions,fg_abi_coh_wb,fg_abi_coh_wb ? 3 : 2); /* write-back only after the wb-ish self-test passed */
 if (!s) {
  fg_abi_imports[slot].iova=0; /* not live until filled below */
  fg_abi_dty_attach(slot,source,bytes); /* clears the modify bits before the clean below */
  if ((gpu_only || fg_abi_imports[slot].dty_first || (fg_abi_ncimport && (permissions&2))) && fg_abi_coh!=1) {
   /* Before the first GPU use: publish preexisting CPU pixels/dirty lines.
    * The GPU lock prevents any submission until this handoff has finished. */
   __asm__ volatile("dsb sy" ::: "memory");
   fg_abi_cache_region(source,bytes,0);
   __asm__ volatile("dsb sy" ::: "memory");
  }
  fg_abi_imports[slot].permissions=permissions; fg_abi_imports[slot].gpu_only=gpu_only;
  fg_abi_imports[slot].source=source; fg_abi_imports[slot].bytes=bytes; fg_abi_imports[slot].iova=iova;
 }
 return s;
}
static void fg_abi_import_remove(uint64_t iova, uint64_t bytes)
{
 for (unsigned i=0;i<FG_ABI_MAX_IMPORTS;i++)
  if (fg_abi_imports[i].iova==iova && fg_abi_imports[i].bytes==bytes) {
   /* A GPU_ONLY range was never touched by the CPU after the map-time clean, but a speculative CPU fetch may have cached lines the GPU then
    * overwrote: drop them before the pages go back to the kernel. */
   if (fg_abi_imports[i].gpu_only && fg_abi_coh!=1) {
    __asm__ volatile("dsb sy" ::: "memory");
    fg_abi_cache_region(fg_abi_imports[i].source,bytes,1);
    __asm__ volatile("dsb sy" ::: "memory");
   }
   fg_abi_dty_detach(i);
   fg_abi_imports[i].iova=0; fg_abi_imports[i].gpu_only=0;
  }
}
static int fg_abi_unmap_locked(uint64_t iova, uint64_t bytes)
{
 if (fg_abi_live && !fg_abi_poisoned && fg_abi_range(iova,bytes)) (void)fg_abi_wait_idle();
 if (fg_abi_poisoned) {
  if (!fg_abi_drained) return -2;
  fg_abi_import_remove(iova,bytes); fg_abi_map_forget(iova,bytes); return 0;
 }
 if (!fg_abi_live || !fg_abi_range(iova,bytes)) return -1;
 if (fg_client_busy) return 1;
 volatile uint64_t *leaf=NULL;
 for (uint64_t off=0;off<bytes;off+=4096) {
  if (!leaf || !((iova+off)&0x1fffff)) leaf=fg_abi_leaf(iova+off); else leaf++;
  *leaf=0;
 }
 fg_abi_used_fill(fg_abi_pg(iova),(unsigned)(bytes>>12),0);
 __asm__ volatile("dsb sy" ::: "memory");
 /* Remove stale ASID translations before backing pages can be released; never skipped. Opt-in value 128 drops a small
  * unmap page by page instead of the whole ASID. Safe with an in-flight present: only the inactive root was changed. */
 uint32_t tlb[128]={0};
 int terr=fg_abi_tlbpages && bytes/4096<=FG_ABI_TLB_PAGE_LIMIT ? fg_abi_tlb_pages(iova,bytes) : g4_tlb(tlb);
 if (terr || fg_access_failed) return fg_abi_fault("client unmap TLB sync");
 fg_abi_import_remove(iova,bytes);
 fg_abi_map_forget(iova,bytes);
 return 0;
}
/* Opt-in (fuxigpuncimport 4096): a refused present keeps the client out until its copy is submitted (fuxi_gpu_copy clears the flag),
 * instead of until the client chain retires, where the client's next submit won the race for the idle GPU. Bounded: after 4 ms
 * the hold drops (the present went away) and clients are admitted again. */
static int fg_abi_present_held(void)
{
 if (!fg_abi_hold || !fg_abi_present_wait) { fg_abi_hold_since=0; return 0; }
 const uint64_t now=rsr("cntpct_el0");
 if (!fg_abi_hold_since) fg_abi_hold_since=now;
 if (now-fg_abi_hold_since>rsr("cntfrq_el0")*4/1000) { fg_abi_present_wait=0; fg_abi_hold_since=0; fg_abi_q_stats[5]++; return 0; }
 return 1;
}
/* Copy priority (fuxigpuncimport bit 4, value 16; needs inline mode 512|2097152). In inline mode a present copy is queued only after
 * its frame's writer lease ends in userland, by which time the next frame's client job is usually already in the ring, so copy N runs
 * behind job N+1 (bit 4096 orders this on the classic path only). SweetFramebuffer publishes the surfaces of the swaps whose copy is
 * not queued yet (fuxi_gpu_present_due); SweetSurface records the ABI sequence at each surface's active writer lock (fuxi_gpu_present_writer;
 * a pending AvoidSync reservation records none). A client submit is refused while a due surface's writer job is already in the ring
 * (a submit followed that lock), so frame N's copy lands ahead of job N+1. Copy path, scanout target choice and fences are unchanged.
 * Bounded: one panel period per due generation (each swap that adds a surface opens one). Lock-free publishers; not yet observed. */
#define FG_CF_DUE 4u
#define FG_CF_WRITERS 8u
static uint32_t fg_cf_due[FG_CF_DUE], fg_cf_due_n, fg_cf_gen, fg_cf_held_gen, fg_cf_expired_gen, fg_cf_w_next;
static struct { uint32_t id, seq; } fg_cf_w[FG_CF_WRITERS];
static uint64_t fg_cf_since;
static uint32_t fg_cf_stats[6]; /* refused attempts, episodes, released by the copy, expired, due publishes, due surface with no writer record */
#define FG_CF_LD(x) __atomic_load_n(&(x), __ATOMIC_RELAXED)
#define FG_CF_ST(x, v) __atomic_store_n(&(x), (v), __ATOMIC_RELAXED)
static void fg_cf_reset(void) { fg_cf_holding=fg_cf_due_n=fg_cf_gen=fg_cf_held_gen=fg_cf_expired_gen=fg_cf_w_next=0; fg_cf_since=0; memset(fg_cf_w,0,sizeof(fg_cf_w)); memset(fg_cf_stats,0,sizeof(fg_cf_stats)); }
void fuxi_gpu_present_writer(uint32_t id, int active)
{
 if (!fg_abi_cf || !id) return;
 uint32_t seq = active ? FG_CF_LD(fg_abi_seq) : UINT32_MAX, slot = FG_CF_WRITERS;
 for (unsigned i = 0; i < FG_CF_WRITERS; i++) if (FG_CF_LD(fg_cf_w[i].id) == id) slot = i;
 if (slot == FG_CF_WRITERS) { slot = __atomic_fetch_add(&fg_cf_w_next, 1, __ATOMIC_RELAXED) % FG_CF_WRITERS; FG_CF_ST(fg_cf_w[slot].id, id); }
 FG_CF_ST(fg_cf_w[slot].seq, seq);
}
/* Callers serialize (SweetFramebuffer's fLock). A surface not in the previous set opens a generation; a removed one (its copy queued or
 * the swap dropped) wakes held submits. */
void fuxi_gpu_present_due(const uint32_t *ids, unsigned count)
{
 if (!fg_abi_cf) return;
 if (count > FG_CF_DUE) count = FG_CF_DUE;
 unsigned old = FG_CF_LD(fg_cf_due_n), added = 0, removed = 0;
 for (unsigned i = 0; i < count; i++) { unsigned f = 0; for (unsigned j = 0; j < old; j++) f |= FG_CF_LD(fg_cf_due[j]) == ids[i]; added |= !f; }
 for (unsigned j = 0; j < old; j++) { unsigned f = 0; for (unsigned i = 0; i < count; i++) f |= FG_CF_LD(fg_cf_due[j]) == ids[i]; removed |= !f; }
 FG_CF_ST(fg_cf_due_n, 0);
 for (unsigned i = 0; i < count; i++) FG_CF_ST(fg_cf_due[i], ids[i]);
 FG_CF_ST(fg_cf_due_n, count);
 if (added) { uint32_t g = FG_CF_LD(fg_cf_gen)+1; FG_CF_ST(fg_cf_gen, g ? g : 1); }
 fg_cf_stats[4]++;
 if (removed && FG_CF_LD(fg_cf_holding)) { fg_cf_stats[2]++; FG_CF_ST(fg_cf_holding, 0); FG_ABI_WAKE(); }
}
static uint64_t fg_cf_bound(void) { return rsr("cntfrq_el0")*(fg_scanout_period_us ? fg_scanout_period_us : 16667u)/1000000; }
/* Under the GPU lock. 1 when a due surface's writer job is in the ring (a submit followed its active lock; with before != 0 only a
 * writer job with a fence below before counts); *wfence = the earliest such writer job's fence (lock sequence + 1). */
static int fg_cf_due_writer(uint32_t before, uint32_t *wfence, int count)
{
 const uint32_t n = FG_CF_LD(fg_cf_due_n);
 unsigned due = 0;
 for (unsigned i = 0; i < n && i < FG_CF_DUE; i++) {
  uint32_t id = FG_CF_LD(fg_cf_due[i]), seq = UINT32_MAX, known = 0;
  for (unsigned k = 0; k < FG_CF_WRITERS; k++) if (FG_CF_LD(fg_cf_w[k].id) == id) { seq = FG_CF_LD(fg_cf_w[k].seq); known = 1; }
  if (!known && count) fg_cf_stats[5]++;
  if (seq == UINT32_MAX || seq == fg_abi_seq || (before && seq+1 >= before)) continue;
  if (!due || seq+1 < *wfence) *wfence = seq+1;
  due = 1;
 }
 return due;
}
/* Under the GPU lock, inline submits only. 1: refuse this submit (the caller waits for a wake or the rest of the bound). */
static int fg_abi_copy_first(void)
{
 if (!fg_abi_cf) return 0;
 const uint32_t gen = FG_CF_LD(fg_cf_gen);
 uint32_t wfence = 0;
 const unsigned due = fg_cf_due_writer(0, &wfence, 1);
 if (!due || gen == fg_cf_expired_gen) { FG_CF_ST(fg_cf_holding, 0); return 0; }
 const uint64_t now = rsr("cntpct_el0");
 if (fg_cf_held_gen != gen) { fg_cf_held_gen = gen; fg_cf_since = now; fg_cf_stats[1]++; }
 if (now-fg_cf_since > fg_cf_bound()) { fg_cf_expired_gen = gen; fg_cf_stats[3]++; FG_CF_ST(fg_cf_holding, 0); return 0; }
 fg_cf_stats[0]++; FG_CF_ST(fg_cf_holding, 1);
 return 1;
}
static unsigned fg_abi_inflight(void) { return fg_abi_seq-fg_abi_completed; }
/* Park (fuxigpuncimport bit 27, value 134217728; needs copy priority 16, so inline 512|2097152). Device R223 (16 alone): copy_first
 * refused ~850 submits per 600 copies, released 0, expired ~300, copies behind a client job 108/128, client swaps 60 Hz. Cause: FuxiMetal's
 * Backend::submit holds the backend mutex across vkQueueSubmit, so the refused (sleeping) submit of job N+1 holds it; job N's retirement
 * waits for its fence without the mutex, then takes it before the surface lease ends, so the writer unlock that queues copy N cannot
 * happen until the refusal times out, and job N+1 is then in the ring ahead of copy N anyway. Parking breaks the cycle in the kernel:
 * the refused job is built (frame words, TLB, import clean, fence = ++fg_abi_seq) but not written to the ring and the submit returns
 * at once, so the mutex is free, job N retires, the lease ends, SweetFramebuffer queues copy N and fuxi_gpu_copy rings the parked job
 * right behind it. One job at most. Released by: the copy (FG_PK_COPY); FG_PK_IDLE_US after the due writer job's fence is written
 * (the copy is not coming: several submits per frame, a deferred present); 2 panel periods after parking (cap; job N may still have
 * been running then); the next submit (order kept: the parked job first); the due writer leaving the due set; a classic copy's root
 * switch or a fault (discarded, the client is poisoned). An idle or cap release ends that due generation's parks, like the hold's
 * expiry. The GPU idles from job N's end to copy N's submit (device Q222: ~0.2 ms writer retire, then ~0.05 ms, or ~0.7 ms while the
 * previous copy still runs). Ring room for the parked frame plus one copy is reserved at park time (only the copy is written before the
 * parked job). Not yet observed on device. */
static uint32_t fg_pk_words[FG_ABI_FRAME_WORDS];
static uint32_t fg_pk_fence, fg_pk_wfence, fg_pk_gen, fg_pk_stats[FG_PK_REASONS+3], fg_pk_idle_n; /* stats: releases per reason, parked, no ring room, not after writer */
static uint64_t fg_pk_since, fg_pk_retired, fg_pk_sum, fg_pk_max, fg_pk_idle_sum, fg_pk_idle_max;
static void fg_pk_stats_reset(void) { fg_pk_idle_n=0; fg_pk_sum=fg_pk_max=fg_pk_idle_sum=fg_pk_idle_max=0; memset(fg_pk_stats,0,sizeof(fg_pk_stats)); }
static void fg_pk_reset(void) { fg_pk_n=fg_pk_fence=fg_pk_wfence=fg_pk_gen=0; fg_pk_since=fg_pk_retired=0; fg_pk_stats_reset(); }
static int fg_pk_written(uint32_t fence) { uint32_t f=g3_read(G3_FENCE_PA+FG_ABI_FENCE_OFF+4); return !fg_access_failed && (int32_t)(f-fence)>=0; }
/* Under the GPU lock: ring the parked job (GPU order: after whatever was written since it was parked, i.e. the due copy). 0 or a fault. */
static int fg_pk_flush(unsigned why)
{
 if (!fg_pk_n) return 0;
 const unsigned n=fg_pk_n; fg_pk_n=0;
 const uint64_t now=rsr("cntpct_el0"), d=now-fg_pk_since;
 fg_pk_stats[why<FG_PK_REASONS ? why : FG_PK_OTHER]++; fg_pk_sum+=d; if (d>fg_pk_max) fg_pk_max=d;
 if (fg_pk_retired) { uint64_t i=now-fg_pk_retired; fg_pk_idle_sum+=i; fg_pk_idle_n++; if (i>fg_pk_idle_max) fg_pk_idle_max=i; }
 if (!fg_inl_usable()) { fg_abi_poisoned=1; return -1; } /* the window went away: the job's fence fails with the client */
 if (g3_faulted()) return fg_abi_fault("client parked pre-ring fault");
 const unsigned queued=fg_client_busy;
 for (unsigned i=0;i<n;i++) g3_emit(fg_pk_words[i]);
 g3_wptr &= 8191;
 __asm__ volatile("dsb sy" ::: "memory");
 if (!queued) { fg_client_deadline=now+rsr("cntfrq_el0")/10; fg_abi_stage=0; }
 fg_client_busy=1;
 fg_dcvs_kick();
 if (!fg_ring_doorbell() || fg_access_failed) return fg_abi_fault("client parked doorbell");
 if (!queued) { uint64_t deadline; clock_interval_to_deadline(2,NSEC_PER_MSEC,&deadline); (void)thread_call_enter_delayed(&fg_client_watchdog,deadline); }
 else { fg_abi_q_stats[0]++; if (fg_abi_seq-fg_abi_completed>fg_abi_q_stats[1]) fg_abi_q_stats[1]=fg_abi_seq-fg_abi_completed; }
 return 0;
}
/* Under the GPU lock, from fg_abi_observe (WaitFence polls, the 2 ms watchdog): release a parked job whose copy is not coming. */
static int fg_pk_check(void)
{
 const uint64_t now=rsr("cntpct_el0");
 uint32_t wf=0;
 if (!fg_pk_retired && fg_pk_written(fg_pk_wfence)) fg_pk_retired=now;
 unsigned why=!fg_cf_due_writer(fg_pk_fence,&wf,0) ? FG_PK_NOTDUE :
     fg_pk_retired && now-fg_pk_retired>=rsr("cntfrq_el0")*FG_PK_IDLE_US/1000000 ? FG_PK_IDLE : now-fg_pk_since>=2*fg_cf_bound() ? FG_PK_CAP : FG_PK_REASONS;
 if (why==FG_PK_REASONS) return 0;
 if (why!=FG_PK_NOTDUE) fg_cf_expired_gen=fg_pk_gen;
 return fg_pk_flush(why);
}
/* Under the GPU lock, inline submits: 1 when this job (n words, already built with fence fg_abi_seq+1) should be parked. */
static int fg_pk_want(unsigned n, uint32_t *wfence)
{
 if (!fg_abi_pk || fg_pk_n) return 0;
 const uint32_t gen=FG_CF_LD(fg_cf_gen);
 if (gen==fg_cf_expired_gen || !fg_cf_due_writer(0,wfence,1)) return 0;
 uint32_t used=(g3_wptr-g3_gr(0x806))&8191;
 if (fg_access_failed || used+n+1024+64>8191) { fg_pk_stats[FG_PK_REASONS+1]++; return 0; } /* room for this frame and one copy (fuxi_gpu_copy wants 1024) */
 return 1;
}
static void fg_pk_park(const uint32_t *words, unsigned n, uint32_t wfence)
{
 memcpy(fg_pk_words,words,n*sizeof(words[0]));
 fg_pk_n=n; fg_pk_fence=fg_abi_seq+1; fg_pk_wfence=wfence; fg_pk_gen=FG_CF_LD(fg_cf_gen);
 fg_pk_since=rsr("cntpct_el0"); fg_pk_retired=fg_pk_written(wfence) ? fg_pk_since : 0;
 fg_pk_stats[FG_PK_REASONS]++; if (fg_pk_retired) fg_pk_stats[FG_PK_REASONS+2]++;
 if (!fg_client_busy) { uint64_t deadline; clock_interval_to_deadline(2,NSEC_PER_MSEC,&deadline); (void)thread_call_enter_delayed(&fg_client_watchdog,deadline); }
}
static void fg_cf_log(void)
{
 if (!fg_abi_cf) return;
 printf("FUXI GPU: copy_first refused=%u episodes=%u released=%u expired=%u due_publishes=%u no_writer=%u bound_us=%u\n",
     fg_cf_stats[0], fg_cf_stats[1], fg_cf_stats[2], fg_cf_stats[3], fg_cf_stats[4], fg_cf_stats[5], fg_scanout_period_us);
 memset(fg_cf_stats, 0, sizeof(fg_cf_stats));
 if (!fg_abi_pk) return;
 const uint64_t f=rsr("cntfrq_el0") ? rsr("cntfrq_el0") : 1, r=fg_pk_stats[FG_PK_COPY]+fg_pk_stats[FG_PK_IDLE]+fg_pk_stats[FG_PK_CAP]+fg_pk_stats[FG_PK_SUBMIT]+fg_pk_stats[FG_PK_NOTDUE]+fg_pk_stats[FG_PK_OTHER];
 printf("FUXI GPU: copy_first_park parked=%u released_by copy=%u idle=%u cap=%u submit=%u not_due=%u other=%u no_room=%u writer_done_at_park=%u"
     " parked_us avg=%llu max=%llu writer_done_to_release_us n=%u avg=%llu max=%llu idle_us=%u\n",
     fg_pk_stats[FG_PK_REASONS], fg_pk_stats[FG_PK_COPY], fg_pk_stats[FG_PK_IDLE], fg_pk_stats[FG_PK_CAP], fg_pk_stats[FG_PK_SUBMIT], fg_pk_stats[FG_PK_NOTDUE],
     fg_pk_stats[FG_PK_OTHER], fg_pk_stats[FG_PK_REASONS+1], fg_pk_stats[FG_PK_REASONS+2],
     (unsigned long long)(r ? fg_pk_sum*1000000/f/r : 0), (unsigned long long)(fg_pk_max*1000000/f), fg_pk_idle_n,
     (unsigned long long)(fg_pk_idle_n ? fg_pk_idle_sum*1000000/f/fg_pk_idle_n : 0), (unsigned long long)(fg_pk_idle_max*1000000/f), FG_PK_IDLE_US);
 fg_pk_stats_reset();
}
/* submit_split (always on, a few counter reads per submit): ticks per phase of a successful backend submit, refused attempts as one lump
 * with their reasons, the wait for the GPU lock, and the cache clean volume. fg_abi_ss_cur is filled by one attempt under the lock and
 * committed by fuxi_gpu_abi_submit only when it succeeded. Taken and cleared by fuxi_gpu_abi_submit_split every 600 submits. */
enum { FG_SS_IDLE, FG_SS_FRAME, FG_SS_TLB, FG_SS_CACHE, FG_SS_RING, FG_SS_BELL, FG_SS_LOCK, FG_SS_REFUSED, FG_SS_N };
static uint64_t fg_abi_ss_t[FG_SS_N], fg_abi_ss_cur[FG_SS_N], fg_abi_ss_lockmax, fg_abi_ss_clean, fg_abi_ss_skip, fg_abi_ss_cur_clean, fg_abi_ss_cur_skip;
static uint32_t fg_abi_ss_att, fg_abi_ss_ok, fg_abi_ss_why[5]; /* why: busy/reserved, hold, idle without queue, present waits, chain or ring full */
#define FG_SS(i) do { uint64_t n_=rsr("cntpct_el0"); fg_abi_ss_cur[i]+=n_-m; m=n_; } while (0)
#define FG_REFUSE(k) do { fg_abi_ss_why[k]++; return 1; } while (0)
static int fg_abi_submit_locked(const uint64_t *iovas, const uint64_t *sizes,
    unsigned count, uint64_t *fence)
{
 uint64_t m=rsr("cntpct_el0");
 if (!fg_abi_live || !fuxi_gpu_ready() || fg_abi_poisoned || fg_abi_seq==UINT32_MAX) return -1;
 const unsigned inl=fg_inl_usable(); /* bit 21: client root installed for good; a copy never refuses or delays a client job */
 if (!inl && (fg_busy || fg_scanout_reserved)) FG_REFUSE(0);
 if (!inl && fg_abi_present_held()) { fg_abi_q_stats[4]++; FG_REFUSE(1); }
 if (fg_pk_n) { int r=fg_pk_flush(FG_PK_SUBMIT); if (r) return r; } /* bit 27: the parked job keeps its place, ahead of this one */
 if (inl && !fg_abi_pk && fg_abi_copy_first()) FG_REFUSE(1); /* bit 16: the due copy goes first (bit 27 parks below instead) */
 /* Why a submit needed an idle GPU: (1) it installs the client root with a full-ASID invalidate, which would pull the page tables from
  * under a running present job; (2) the ring was assumed empty (RPTR==WPTR) and one fence tracked one job; (3) a frame opens with a
  * CP context reset, which is safe only after the previous frame's WFI. Behind a running *client* job (1) is moot, (3) is ordered by
  * the in-order ring, and (2) becomes seq/completed. Queue mode (bit 512) therefore appends the frame and re-rings the doorbell.
  * It refuses (busy, the caller retries) while a present waits, with FG_ABI_QUEUE_MAX jobs in flight or without ring room. */
 int queued=0, idle=fg_abi_wait_idle();
 if (idle<0) return -1;
 if (idle || inl) {
  if (!fg_abi_queue) FG_REFUSE(2);
  if (!inl && fg_abi_present_wait) { fg_abi_q_stats[2]++; FG_REFUSE(3); }
  uint32_t used=(g3_wptr-g3_gr(0x806))&8191;
  if (fg_access_failed) return fg_abi_fault("client queue ring read");
  if (fg_abi_seq-fg_abi_completed>=FG_ABI_QUEUE_MAX || used+FG_ABI_FRAME_WORDS+64>8191) { fg_abi_q_stats[3]++; FG_REFUSE(4); }
  if (g3_faulted()) return fg_abi_fault("client pre-queue fault");
  queued=idle>0;
 } else if (g3_faulted() || g3_gr(0x806)!=g3_wptr || g3_gr(0x807)!=g3_wptr || (g3_gr(0x210)&~1u))
  return fg_abi_fault("client pre-submit idle/fault");
 FG_SS(FG_SS_IDLE);
 uint32_t *words=fg_abi_words; /* serialized by the GPU lock; too large for the kernel stack at 128 IBs */
 unsigned n=fg_abi_frame(words,iovas,sizes,count,G3_FENCE_VA+FG_ABI_FENCE_OFF,fg_abi_seq,fg_abi_seq+1,1);
 if (!n) return -1;
 FG_SS(FG_SS_FRAME);
 fg_abi_ibs[0]++; fg_abi_ibs[1]+=count; if (count>fg_abi_ibs[2]) fg_abi_ibs[2]=count; if (count>16) fg_abi_ibs[3]++;
 uint32_t pk_writer=0;
 const unsigned park=inl && fg_pk_want(n,&pk_writer); /* bit 27: a due writer job is in the ring, its copy is not: park this one */
 if (queued || inl) {
  /* Same root, nothing to switch. Leaves are only added to never-used VA (fg_abi_map_locked), but if any map happened since the last
   * submit drop the ASID's translations anyway: fail closed on the no-negative-caching assumption. */
  uint32_t tlb[128]={0};
  if (fg_abi_map_seq!=fg_abi_submit_map_seq && (g4_tlb(tlb) || fg_access_failed)) return fg_abi_fault("client queue TLB");
 } else {
  if (fg_abi_inline) (void)fg_inl_setup(); /* before the install, so the window is part of the root the GPU walks */
  if (!fg_abi_switch(FG_ABI_PT)) return fg_abi_fault("client install TTBR");
  fg_root_client=fg_abi_inline && fg_inl_ready==1;
 }
 FG_SS(FG_SS_TLB);
 /* Ordinary BOs are NC on both sides. Imports preserve CPU attributes and
  * need an explicit PoC handoff; IO-coherent XNU helpers may be no-ops here.
  * No CPU access is allowed until retirement, per external synchronization.
  * PM4 prologue/epilogue handle GPU cache invalidate/writeback. */
 fg_abi_ss_cur_clean=fg_abi_cleaned; fg_abi_ss_cur_skip=fg_abi_skipped;
 fg_abi_import_cache(0);
 fg_abi_ss_cur_clean=fg_abi_cleaned-fg_abi_ss_cur_clean; fg_abi_ss_cur_skip=fg_abi_skipped-fg_abi_ss_cur_skip;
 __asm__ volatile("dsb sy" ::: "memory");
 FG_SS(FG_SS_CACHE);
 if (park) { fg_abi_submit_map_seq=fg_abi_map_seq; fg_pk_park(words,n,pk_writer); *fence=++fg_abi_seq; FG_SS(FG_SS_BELL); return 0; } /* rung by fg_pk_flush */
 for (unsigned i=0;i<n;i++) g3_emit(words[i]);
 g3_wptr &= 8191;
 __asm__ volatile("dsb sy" ::: "memory");
 FG_SS(FG_SS_RING);
 if (!queued) { fg_client_deadline=rsr("cntpct_el0")+rsr("cntfrq_el0")/10; fg_abi_stage=0; }
 fg_client_busy=1;
 fg_dcvs_kick();
 fg_abi_submit_map_seq=fg_abi_map_seq;
 if (!fg_ring_doorbell() || fg_access_failed) return fg_abi_fault("client doorbell");
 *fence=++fg_abi_seq;
 FG_SS(FG_SS_BELL);
 if (queued) {
  fg_abi_q_stats[0]++;
  if (fg_abi_seq-fg_abi_completed>fg_abi_q_stats[1]) fg_abi_q_stats[1]=fg_abi_seq-fg_abi_completed;
  return 0; /* the watchdog already re-arms itself while the chain runs */
 }
 uint64_t deadline;
 clock_interval_to_deadline(2,NSEC_PER_MSEC,&deadline);
 (void)thread_call_enter_delayed(&fg_client_watchdog,deadline);
 return 0;
}
int fuxi_gpu_abi_unmap(uint64_t iova, uint64_t bytes)
{
 FG_GUARD;
 return fg_abi_unmap_locked(iova,bytes);
}
int fuxi_gpu_abi_submit(const uint64_t *iovas, const uint64_t *sizes,
    unsigned count, uint64_t *fence)
{
 uint64_t t0=rsr("cntpct_el0");
 FG_GUARD;
 uint64_t t1=rsr("cntpct_el0");
 memset(fg_abi_ss_cur,0,sizeof(fg_abi_ss_cur));
 int s=fg_abi_submit_locked(iovas,sizes,count,fence);
 fg_abi_ss_att++; fg_abi_ss_t[FG_SS_LOCK]+=t1-t0; if (t1-t0>fg_abi_ss_lockmax) fg_abi_ss_lockmax=t1-t0;
 if (!s) {
  fg_abi_ss_ok++; fg_abi_ss_clean+=fg_abi_ss_cur_clean; fg_abi_ss_skip+=fg_abi_ss_cur_skip;
  for (unsigned i=0;i<FG_SS_BELL+1;i++) fg_abi_ss_t[i]+=fg_abi_ss_cur[i];
 } else fg_abi_ss_t[FG_SS_REFUSED]+=rsr("cntpct_el0")-t1;
 return s;
}
/* Microseconds a refused submit should wait before trying again: the rest of the present hold (the copy submit wakes it earlier), up to
 * 1 ms for a running copy (its retirement wakes it), 100 us otherwise (nothing signals those; they stay polled). */
unsigned fuxi_gpu_abi_submit_wait_us(unsigned *kind)
{
 FG_GUARD;
 *kind=(fg_abi_hold && fg_abi_present_wait) || fg_cf_holding ? 0 : fg_busy ? 1 : 2;
 const uint64_t now=rsr("cntpct_el0"), f=rsr("cntfrq_el0");
 if (fg_cf_holding) { uint64_t end=fg_cf_since+fg_cf_bound(); if (now>=end || !f) return 1; uint64_t us=(end-now)*1000000/f+1; return us>16667 ? 16667 : (unsigned)us; }
 if (fg_abi_hold && fg_abi_present_wait && fg_abi_hold_since && f) {
  uint64_t end=fg_abi_hold_since+f*4/1000;
  if (now>=end) return 1;
  uint64_t us=(end-now)*1000000/f+1;
  return us>4000 ? 4000 : (unsigned)us;
 }
 return fg_busy ? 1000 : 100;
}
/* Clean volume of the submits since the last call (see fg_abi_cc_note) and the biggest mapped imports now. */
void fuxi_gpu_abi_clean_split(struct fuxi_gpu_clean_split *o)
{
 FG_GUARD;
 uint64_t f=rsr("cntfrq_el0"); if (!f) f=1;
 memset(o,0,sizeof(*o));
 for (unsigned i=0;i<2;i++) { o->kib[i]=fg_abi_cc_bytes[i]>>10; o->ns[i]=fg_abi_cc_ticks[i]*1000000000ull/f; }
 for (unsigned i=0;i<4;i++) { o->bkt_kib[i]=fg_abi_cc_bb[i]>>10; o->bkt_ns[i]=fg_abi_cc_bt[i]*1000000000ull/f; }
 o->go_kib=fg_abi_cc_go>>10;
 o->dty_state=fg_abi_dirty ? fg_abi_dty_ok+1 : 0; o->dty_checked=fg_abi_dty_st[0]; o->dty_dirty=fg_abi_dty_st[1]; o->dty_kib=fg_abi_dty_st[2]>>10; o->dty_full_kib=fg_abi_dty_st[3]>>10;
 o->dty_check_ns=fg_abi_dty_st[4]*1000000000ull/f; o->dty_clean_ns=fg_abi_dty_st[5]*1000000000ull/f; o->ivac_ns=fg_abi_dty_st[6]*1000000000ull/f; o->submits=(uint32_t)fg_abi_dty_st[7];
 o->dty_imports=0; for (unsigned i=0;i<FG_ABI_MAX_IMPORTS;i++) if (fg_abi_imports[i].iova && fg_abi_imports[i].dty_first) o->dty_imports++;
 memset(fg_abi_dty_st,0,sizeof(fg_abi_dty_st)-sizeof(fg_abi_dty_st[0]));
 for (unsigned i=0;i<FG_ABI_MAX_IMPORTS;i++) if (fg_abi_imports[i].iova) {
  o->imports++; o->mapped_kib+=fg_abi_imports[i].bytes>>10; if (fg_abi_imports[i].gpu_only) o->go_imports++;
  for (unsigned k=0;k<3;k++) if (fg_abi_imports[i].bytes>o->top_kib[k]<<10 || !o->top_kib[k]) {
   for (unsigned j=2;j>k;j--) { o->top_kib[j]=o->top_kib[j-1]; o->top_perm[j]=o->top_perm[j-1]; }
   o->top_kib[k]=fg_abi_imports[i].bytes>>10; o->top_perm[k]=fg_abi_imports[i].permissions|(fg_abi_imports[i].gpu_only ? 16 : 0);
   break;
  }
 }
 memset(fg_abi_cc_bytes,0,sizeof(fg_abi_cc_bytes)); memset(fg_abi_cc_ticks,0,sizeof(fg_abi_cc_ticks));
 memset(fg_abi_cc_bb,0,sizeof(fg_abi_cc_bb)); memset(fg_abi_cc_bt,0,sizeof(fg_abi_cc_bt)); fg_abi_cc_go=0;
}
/* Window totals in ns, then cleared: ns[] = idle-check, frame, tlb, cache, ring, bell, gpu-lock wait, refused attempts (all attempts);
 * why[5]; n[0] attempts, n[1] successes; the longest lock wait; KiB cleaned and skipped by the successful submits. */
void fuxi_gpu_abi_submit_split(uint64_t ns[8], uint32_t why[5], uint32_t n[2], uint64_t *lockmax_ns, uint64_t kib[2])
{
 FG_GUARD;
 uint64_t f=rsr("cntfrq_el0"); if (!f) f=1;
 for (unsigned i=0;i<FG_SS_N;i++) ns[i]=fg_abi_ss_t[i]*1000000000ull/f;
 *lockmax_ns=fg_abi_ss_lockmax*1000000000ull/f;
 memcpy(why,fg_abi_ss_why,sizeof(fg_abi_ss_why)); n[0]=fg_abi_ss_att; n[1]=fg_abi_ss_ok;
 kib[0]=fg_abi_ss_clean>>10; kib[1]=fg_abi_ss_skip>>10;
 memset(fg_abi_ss_t,0,sizeof(fg_abi_ss_t)); memset(fg_abi_ss_why,0,sizeof(fg_abi_ss_why));
 fg_abi_ss_att=fg_abi_ss_ok=0; fg_abi_ss_lockmax=fg_abi_ss_clean=fg_abi_ss_skip=0;
}
/* Opt-in (fuxigpuncimport value 64) fail-closed CPU/GPU coherency self-test, run once under the GPU lock at the first import map.
 * It maps a cacheable kernel page with the exact import leaf attributes (Normal NC, outer-shareable) and runs one tiny IB:
 * up: the CPU leaves 8 lines dirty in its cache and the GPU must copy those values (no clean); down: the CPU caches 8 old
 * lines and the GPU must overwrite them and the CPU must read the new values (no invalidate). All 16 must match, otherwise
 * (stale, timeout, fault, anything unexpected) the normal per-submit clean and retire invalidate stay. Not yet observed on device. */
#define FG_ABI_COH_IOVA (FG_ABI_VA_START+FG_ABI_VA_SIZE-0x10000)
#define FG_ABI_COH_IB (FG_ABI_PT+0x3f0000)
#define FG_ABI_COH_RES (FG_ABI_PT+0x3f1000)
_Static_assert(FG_ABI_COH_IB >= FG_ABI_PT+(4u+768u)*4096 && FG_ABI_COH_RES+4096 <= FG_ABI_PT+FG_ABI_PT_BYTES, "coherency test pages overlap tables");
static uint8_t fg_abi_coh_page[16384] __attribute__((aligned(16384)));
static const char *coh_why; static unsigned coh_up, coh_down, coh_def;
static void fg_abi_coh_try(unsigned wbmode)
{
 coh_why="not idle"; coh_up=coh_down=coh_def=0;
 if (!fg_abi_live || !fuxi_gpu_ready() || fg_abi_poisoned || fg_abi_wait_idle()) return; /* its maps and unmaps need an idle GPU */
 const uint64_t ib=FG_ABI_COH_IOVA, res=ib+0x1000, data=ib+0x4000;
 const char *why="inconclusive";
 unsigned up=0, down=0, mapped=0, definitive=0;
 volatile uint32_t *page=(volatile uint32_t *)fg_abi_coh_page;
 volatile uint32_t *rp=(volatile uint32_t *)G_PTR_RANGE(FG_ABI_COH_RES,4096);
 uint32_t *ibw=(uint32_t *)G_PTR_RANGE(FG_ABI_COH_IB,4096);
 uint64_t fence=0, ibytes=0, cleaned=fg_abi_cleaned;
 unsigned n=0;
 if (fg_abi_map_locked(ib,(uintptr_t)ibw,4096,3,0,2)) { why="map ib"; goto out; }
 mapped|=1;
 if (fg_abi_map_locked(res,(uintptr_t)rp,4096,3,0,2)) { why="map result"; goto out; }
 mapped|=2;
 if (fg_abi_map_locked(data,(uintptr_t)fg_abi_coh_page,sizeof(fg_abi_coh_page),3,wbmode,wbmode ? 3 : 2)) { why="map data"; goto out; }
 mapped|=4;
 for (unsigned k=0;k<16;k++) rp[k]=0;
 for (unsigned k=0;k<8;k++) { page[k*16]=0xC0DE0000u|k; page[(8+k)*16]=0x51A1E000u|k; (void)page[(8+k)*16]; }
 for (unsigned k=0;k<8;k++) {
  ibw[n++]=fg_abi_pkt7(0x73,5); ibw[n++]=0; ibw[n++]=(uint32_t)(res+4*k); ibw[n++]=0; ibw[n++]=(uint32_t)(data+64*k); ibw[n++]=0;
 }
 for (unsigned k=0;k<8;k++) {
  ibw[n++]=fg_abi_pkt7(0x3d,3); ibw[n++]=(uint32_t)(data+(8+k)*64); ibw[n++]=0; ibw[n++]=0x6E57A000u|k;
 }
 ibw[n++]=fg_abi_pkt7(0x12,0); ibw[n++]=fg_abi_pkt7(0x26,0);
 ibytes=4u*n;
 __asm__ volatile("dsb sy" ::: "memory");
 { int s=fg_abi_submit_locked(&ib,&ibytes,1,&fence); if (s) { why=s>0 ? "busy" : "submit"; goto out; } }
 { int o; while ((o=fg_abi_observe())==1) delay(20);
   if (o) { why="fault"; definitive=1; goto out; } }
 for (unsigned k=0;k<8;k++) { up+=rp[k]==(0xC0DE0000u|k); down+=page[(8+k)*16]==(0x6E57A000u|k); }
 definitive=1;
 why=up==8 && down==8 ? "pass" : "stale";
out:
 if (mapped&4) fg_abi_cache_region((uintptr_t)fg_abi_coh_page,sizeof(fg_abi_coh_page),0);
 fg_abi_cleaned=cleaned;
 if (mapped&4) (void)fg_abi_unmap_locked(data,sizeof(fg_abi_coh_page));
 if (mapped&2) (void)fg_abi_unmap_locked(res,4096);
 if (mapped&1) (void)fg_abi_unmap_locked(ib,4096);
 coh_why=why; coh_up=up; coh_down=down; coh_def=definitive;
}
/* Variant 1: the exact import attributes of today (Normal NC, outer-shareable): the GPU side cannot snoop, so a fail there is expected.
 * Variant 2 ("wb-ish"): the test page is mapped through MAIR index 1 (0xff, Normal write-back) with inner-shareable leaves, the attributes
 * an IO-coherent client would use (Linux IOMMU_CACHE). A pass makes every later import map that way and skips all import maintenance. */
static void fg_abi_coh_probe(void)
{
 for (unsigned wbmode=0;wbmode<2;wbmode++) {
  fg_abi_coh_try(wbmode);
  int pass=coh_up==8 && coh_down==8 && coh_def && !fg_abi_poisoned;
  if (pass && wbmode) for (unsigned i=0;i<FG_ABI_MAX_IMPORTS;i++) if (fg_abi_imports[i].iova) { pass=0; coh_why="imports already mapped NC"; }
  const char *tag=wbmode ? "(wb-ish)" : "";
  if (pass) {
   fg_abi_coh=1; fg_abi_coh_wb=wbmode;
   printf("FUXI GPU ABI: coherent-import%s pass cpu_to_gpu %u/8 gpu_to_cpu %u/8 -> skip import clean/invalidate%s\n",tag,coh_up,coh_down,wbmode ? ", imports mapped write-back inner-shareable" : "");
   return;
  }
  if (!coh_def && ++fg_abi_coh_tries<3) {
   printf("FUXI GPU ABI: coherent-import%s self-test %s cpu_to_gpu %u/8 gpu_to_cpu %u/8 -> keep import cache maintenance (retry)\n",tag,coh_why,coh_up,coh_down);
   return;
  }
  printf("FUXI GPU ABI: coherent-import%s self-test %s cpu_to_gpu %u/8 gpu_to_cpu %u/8 -> %s\n",tag,coh_why,coh_up,coh_down,
      wbmode || !coh_def ? "keep import cache maintenance (final)" : "trying write-back inner-shareable");
  if (!coh_def || fg_abi_poisoned) break;
 }
 fg_abi_coh=2;
}
int fuxi_gpu_abi_poll(uint64_t fence)
{
 FG_GUARD;
 if (!fg_abi_live || fence>fg_abi_seq) return -1;
 if (fg_abi_queue && fence && fence<=fg_abi_completed) return 0; /* retired before any later job's fault: stays successful */
 int err=fg_abi_observe();
 if (err<0) return err;
 return fence<=fg_abi_completed ? 0 : 1;
}
int fuxi_gpu_abi_queue_enabled(void) { FG_GUARD; return fg_abi_queue; }
int fuxi_gpu_abi_roimport(void) { FG_GUARD; return fg_abi_roimport; }
int fuxi_gpu_abi_pace(void) { FG_GUARD; return fg_abi_pace; }
int fuxi_gpu_abi_start(void) { FG_GUARD; return fg_abi_start; }
void fuxi_gpu_abi_queue_stats(uint32_t q[6]) { FG_GUARD; memcpy(q,fg_abi_q_stats,sizeof(fg_abi_q_stats)); }
/* Window counters for the SWEET FB stats line (fuxi_gpu_abi_stats_log); a snapshot, not reset. */
void fuxi_gpu_abi_sched_stats(uint32_t ibs[4], uint32_t wptr[4], uint32_t stall[8])
{
 FG_GUARD;
 memcpy(ibs,fg_abi_ibs,sizeof(fg_abi_ibs)); memcpy(wptr,fg_wptr_stats,sizeof(fg_wptr_stats)); memcpy(stall,fg_abi_stall_stats,sizeof(fg_abi_stall_stats));
}
