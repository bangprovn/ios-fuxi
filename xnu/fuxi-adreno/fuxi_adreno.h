#ifndef FUXI_ADRENO_H
#define FUXI_ADRENO_H
#define FG_CACHE_SLOTS 4u
/* Timings in mach absolute ticks, CPU uses scheduler runtime (excludes sleep).
 * Surface leases use a short lock separate from the copy/cache transaction.
 * The ABI shared mutex serializes ring and SMMU control. */
struct fg_times {
 uint64_t pin, lookup, map, clean, submit, wait, unmap, cpu;
 unsigned attempts, hits, misses, sleeps, blocks, groups;
};
/* Full-range lookup only; zero for unpublished, wrapping or unknown PAs. */
uintptr_t fuxi_gpu_nc_pointer(uint64_t pa, uint64_t bytes);
int fuxi_gpu_ready(void);
/* Cumulative ABI cache bytes (including initial map clean), plus live imports.
 * Snapshot under the GPU lock; no counter reset or ownership change. */
void fuxi_gpu_import_stats(uint64_t *cleaned, uint64_t *skipped, uint64_t *invalidated,
    uint64_t *mapped, unsigned *imports);
/* Default-off command-mode handoff. configure is called before GPU startup.
 * acquire: 1 GPU target writable, 0 deferred, -1 held. CPU writers must use
 * cpu_acquire, which drains/restores A until the next GPU frame.
 * publish: 1 transferred, 2 back ready/deferred, 0 old path (caller kicks), -1 held.
 * retry: same results, but 0 means no ready frame; never issue a generic kick.
 * reserve/release bracket a SweetSurface cache transaction; 0 means defer.
 */
void fuxi_gpu_scanout_configure(uintptr_t pixels, unsigned enabled, unsigned check);
int fuxi_gpu_scanout_owned(void);
/* One copy/ready frame at a time; timestamps are mach absolute units. */
struct fg_present_times { uint64_t submitted, fence, start, previous_start; };
void fuxi_gpu_scanout_stamps(struct fg_present_times *times);
/* Frame log detail (fuxigpuncimport bit 8): GPU always-on stamps of the last retired copy (doorbell to its start stamp at fence page
 * +192, i.e. the wait behind earlier ring work, and its blits; UINT32_MAX without stamps), client jobs in flight ahead of it at
 * submit (inline mode), its scanout buffer, the TE count read after the last START and the last transfer's done time in us. */
struct fg_copy_stamps { uint32_t s2s_us, blit_us, ahead, buffer, te, xfer_us, parked; }; /* parked: a parked client job (bit 27) went in behind this copy */
void fuxi_gpu_copy_stamps(struct fg_copy_stamps *stamps);
/* Copy priority (fuxigpuncimport bit 16, inline mode only; lock-free, no GPU lock): present_due publishes the surface ids of the swaps
 * whose present copy is not queued yet (callers serialize), present_writer the ABI sequence at a surface's writer lock (active 0: an
 * AvoidSync reservation, sequence unknown). A client submit is refused while a due surface's writer job is already in the ring. */
void fuxi_gpu_present_due(const uint32_t *ids, unsigned count);
void fuxi_gpu_present_writer(uint32_t id, int active);
/* Caller-owned, zero-initialized TE observer. Use from one serialized timer;
 * independent observers share no mutable estimator state. Healthy observation
 * takes no GPU lock or writes; double-mode MMIO failure stops/restores A.
 * On success frame (low16), edge and period describe the latest observed TE
 * in mach absolute units. On failure these outputs must not be used. */
struct fg_te_observer {
 uint64_t edge, period, poll, change, raw_edge;
 unsigned frame, raw_frame, samples, initialized, nominal_us;
};
int fuxi_gpu_scanout_te(struct fg_te_observer *te);
int fuxi_gpu_scanout_acquire(void);
int fuxi_gpu_scanout_publish(void);
int fuxi_gpu_scanout_retry(void);
/* Not-shown accounting: offer_fate is what became of the frame waiting at the last publish (0 none, 1 STARTed by the spin thread
 * during the copy, 2 replaced unstarted); async_take returns 1 once when the spin thread STARTed the waiting frame unreported. */
unsigned fuxi_gpu_scanout_offer_fate(void);
int fuxi_gpu_scanout_async_take(void);
int fuxi_gpu_scanout_pending(void);
int fuxi_gpu_scanout_double(void);
int fuxi_gpu_scanout_triple(void);
int fuxi_gpu_scanout_cpu_acquire(void);
int fuxi_gpu_scanout_reserve(void);
/* fuxigpuncimport bit 20 (1048576, default off): SweetSurface cleans the copy source to PoC
 * before the ring reservation, outside the GPU lock, and fuxi_gpu_copy then skips its own
 * clean; also a read-only VIG0 SSPP survey at takeover. clean_source takes no lock. */
int fuxi_gpu_scanout_preclean(void);
void fuxi_gpu_clean_source(uintptr_t source, uint64_t length);
void fuxi_gpu_scanout_release(void);
/* fuxigpuncimport bits 22+23+24 (direct scanout stage 3, default off): VIG0 fetches a CA surface through the display bank's IOVA slots
 * instead of a GPU copy. on: armed and no fault (lock-free). slot: 1 key mapped in *slot, 2 miss (*slot chosen and emptied, *old its
 * previous key, 0 none), 0 refused (copy). queue: map (pages, n 4 KiB pages; NULL on a hit), queue the slot for the next offer; va is
 * the surface's kernel address for a CPU fallback, fence the writer's ABI fence (0 none); 1 queued, 0 refused, -1 fault (copy for
 * the boot). busy: slots the display may still read (transfer, pending START, queued), lock-free try. refused: SweetSurface/framebuffer
 * refusal reasons. abi_seq: submitted ABI sequence, lock-free; abi_fence_passed: fence memory reached fence (0 always). */
enum { FG_DD_GEOMETRY, FG_DD_STRIDE, FG_DD_OFFSET, FG_DD_FORMAT, FG_DD_BACKING, FG_DD_LAYERS, FG_DD_PAGES, FG_DD_SLOTS, FG_DD_STATE, FG_DD_WHY };
#define FG_DD_SLOTS_N 4u
#define FG_DD_SLOT_PAGES 4096u /* 16 MiB of 4 KiB pages per slot */
int fuxi_gpu_scanout_direct_on(void);
int fuxi_gpu_scanout_direct_slot(uint64_t key, unsigned *slot, uint64_t *old);
int fuxi_gpu_scanout_direct_queue(unsigned slot, uint64_t key, const uint64_t *pages, unsigned n, uintptr_t va, uint32_t fence);
unsigned fuxi_gpu_scanout_direct_busy(void);
void fuxi_gpu_scanout_direct_refused(unsigned why);
uint32_t fuxi_gpu_abi_seq(void);
int fuxi_gpu_abi_fence_passed(uint32_t fence);
void fuxi_gpu_start(uint64_t framebuffer, unsigned width, unsigned height, unsigned pitch);
int fuxi_gpu_map(unsigned slot, uint64_t generation, uintptr_t source,
    uint64_t length, struct fg_times *t);
/* False means failed DMA drain: keep backing prepared/retained until reset. */
int fuxi_gpu_unmap(unsigned slot, struct fg_times *t);
/* copy: 2 submitted, 0 fallback, -1 quarantine. poll: 0 busy, 1 done,
 * -1 failed/drained, -2 failed/quarantined. Serialized by the surface cache lock. */
int fuxi_gpu_poll(struct fg_times *t);
int fuxi_gpu_copy(unsigned slot, uint64_t generation, uintptr_t source, uint64_t length,
    unsigned pitch, unsigned sx, unsigned sy, unsigned dx, unsigned dy,
    unsigned width, unsigned height, struct fg_times *t);
#endif
