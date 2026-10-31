/* NativeAdrenoABI platform bridge. All operations serialize with presents. */
#ifndef FUXI_GPU_ABI_H
#define FUXI_GPU_ABI_H
#include <stdint.h>
#include <stdbool.h>
#ifdef __cplusplus
extern "C" {
#endif
/* Managed 16K host page with Normal WB or NC cache policy; osfmk owns pmap APIs. */
bool fuxi_adreno_host_page_ok(uint64_t pa);
/* 0 success, 1 ring busy, -1 disabled/drained, -2 failed DMA drain.
 * poll also returns -3 on watchdog timeout/drained, -4 timeout/quarantined. */
int fuxi_gpu_abi_map(uint64_t iova, uintptr_t source, uint64_t bytes,
    unsigned permissions, unsigned writeback, unsigned shareability);
/* Persistent kernel mapping of a prepared task descriptor; lifetime through unmap. */
int fuxi_gpu_abi_map_import(uint64_t iova, uintptr_t source, uint64_t bytes, unsigned permissions);
/* Ticks the last fuxi_gpu_abi_map spent holding the GPU lock (after taking it). */
uint64_t fuxi_gpu_abi_map_work(void);
int fuxi_gpu_abi_unmap(uint64_t iova, uint64_t bytes);
int fuxi_gpu_abi_submit(const uint64_t *iovas, const uint64_t *sizes,
    unsigned count, uint64_t *fence);
int fuxi_gpu_abi_poll(uint64_t fence);
/* CP always-on ticks: 0 success, -1 not ready, -2 MMIO error, -3 rollover timeout. */
int fuxi_gpu_abi_timestamp(uint64_t *ticks);
/* Monotonic boot-wide counts remain readable after permanent GPU failure. */
int fuxi_gpu_abi_status(uint64_t *faults, uint64_t *suspends);
int fuxi_gpu_abi_available(void);
/* Snapshot of the backend's IBs-per-submit [4], WPTR fence [4] and stall recovery [8] counters; see fuxi_gpu_abi_backend.inc. */
void fuxi_gpu_abi_sched_stats(uint32_t ibs[4], uint32_t wptr[4], uint32_t stall[8]);
/* fuxigpuncimport bit 512: submits queue behind a running job (in-order ring, one TTBR switch per chain). */
int fuxi_gpu_abi_queue_enabled(void);
int fuxi_gpu_abi_roimport(void);
int fuxi_gpu_abi_pace(void);
int fuxi_gpu_abi_start(void);
/* fuxigpuncimport bit 29 (536870912, needs 64): 1 cached-coherent BOs on (sticky for the boot), 0 off, -1 self-test not conclusive yet.
 * probe=1 may run the coherent-import self-test now (GetDeviceInfo); probe=0 only reads the verdict (BoAlloc). */
int fuxi_gpu_abi_cached_coherent(int probe);
/* Queued submits, deepest chain, queue refusals for a waiting present, refusals for a full chain/ring. */
void fuxi_gpu_abi_queue_stats(uint32_t q[6]);
/* submit_split window (see fuxi_gpu_abi_backend.inc), taken and cleared by the caller's 600-submit report. */
void fuxi_gpu_abi_submit_split(uint64_t ns[8], uint32_t why[5], uint32_t n[2], uint64_t *lockmax_ns, uint64_t kib[2]);
/* Wake hook (set by the userclient): called when a refused submit may now be admitted. Safe to call with it NULL. */
extern void (*fuxi_gpu_abi_wake_hook)(void);
#define FG_ABI_WAKE() do { if (fuxi_gpu_abi_wake_hook) fuxi_gpu_abi_wake_hook(); } while (0)
unsigned fuxi_gpu_abi_submit_wait_us(unsigned *kind); /* kind: 0 rest of a present hold, 1 running copy, 2 other */
struct fuxi_gpu_clean_split;
void fuxi_gpu_abi_clean_split(struct fuxi_gpu_clean_split *out);
void fuxi_gpu_abi_publish(void);
/* GPU fence interrupt (fuxigpuncimport bit 26, fuxi_gpu_irq.inc). setup attaches once and returns 1 while armed; sleep blocks up to us
 * microseconds unless the interrupt count has moved past seq0: 1 woken, 0 timed out, -1 not armed, -2 fence value already written
 * (the ring is draining; poll instead). */
int fuxi_gpu_irq_setup(void);
uint32_t fuxi_gpu_irq_seq(void);
int fuxi_gpu_irq_sleep(uint32_t seq0, uint64_t fence, unsigned us);
enum { FG_IRQ_N, FG_IRQ_EMPTY, FG_IRQ_FOREIGN, FG_IRQ_WAKES, FG_IRQ_TIMEOUTS, FG_IRQ_RACES, FG_IRQ_MASKFIX, FG_IRQ_WRITTEN, FG_IRQ_STATS };
/* Counters and why it was masked (1 empty run, 2 burst, 3 isolation, 4 no delivery); returns 0 off, 1 armed, 2 attach failed, 3 masked, 4 attaching. */
unsigned fuxi_gpu_irq_stats(uint64_t s[FG_IRQ_STATS], unsigned *why);
#ifdef __cplusplus
}
#endif
#endif
