/*
 * Built-in IOSurface for SWEET.
 *
 * Apple's IOSurface kext is closed. IOSurface.framework (iOS 16.7) finds
 * IOServiceNameMatching("IOCoreSurfaceRoot"), opens it with type 0 and
 * talks to the user client below:
 *
 *   sel 13  limits: struct out 0x18, u16 at +0x16 must be 1 (ABI version),
 *           +0 offset alignment mask, +4 bytes-per-row alignment mask
 *   sel 0   create: scalar [parent id], struct in = serialized property
 *           dictionary (binary or XML), struct out = lock result (0xa68)
 *   sel 4   lookup by id: scalar [id], struct out = lock result
 *   sel 34  lookup by mach port: scalar [port name], struct out = lock result
 *   sel 35  create mach port: scalars [id, options], scalar out [port name]
 *   traps   0/1 use count +/- (id, category), 2 lock / 3 unlock
 *           (id, options, &seed), 4/5 release (id)
 *
 * The lock result tells the library where the surface lives in the
 * caller's address space: +0 pixels, +8 shared state page (registry id,
 * seed at +0xc), +0x10 timestamps, then id, sizes and geometry (layout in
 * sweet_surface_fill_result). Every surface is wired kernel memory mapped
 * into each task that creates or looks it up; mappings are counted per
 * client and dropped on release or close.
 *
 * Values, notifications, accelerators and shared events are accepted and
 * ignored (logged once per selector).
 */
#include "SweetSurface.h"

#if defined(SWEET)
#include <IOKit/IOService.h>
#include <IOKit/IOUserClient.h>
#include <IOKit/IOKitKeys.h>
#include <IOKit/IOLib.h>
#include <IOKit/IOLocks.h>
#include <IOKit/system.h>
#include <pexpert/pexpert.h>
#include <libkern/c++/OSArray.h>
#include <libkern/c++/OSDictionary.h>
#include <libkern/c++/OSNumber.h>
#include <libkern/c++/OSUnserialize.h>
#include <sys/proc.h>

enum {
	kSweetSurfaceCreate             = 0,
	kSweetSurfaceLookup             = 4,
	kSweetSurfaceLimits             = 13,
	kSweetSurfaceLookupFromPort     = 34,
	kSweetSurfaceCreatePort         = 35,
};

enum {
	kSweetSurfaceTrapIncrementUse   = 0,
	kSweetSurfaceTrapDecrementUse   = 1,
	kSweetSurfaceTrapLock           = 2,
	kSweetSurfaceTrapUnlock         = 3,
	kSweetSurfaceTrapRelease        = 4,
	kSweetSurfaceTrapReleaseFailed  = 5,
};

#define SWEET_SURFACE_RESULT_SIZE       0xa68
#define SWEET_SURFACE_MAX_ID            4096
#define SWEET_SURFACE_MAX_DIM           16384
#define SWEET_SURFACE_MAX_ALLOC         (256u << 20)
#define SWEET_SURFACE_MAX_PROPS         (64u << 10)
#define SWEET_SURFACE_ROW_ALIGN         64
/* backboardd's Metal renderer only ever wraps 4-byte single-plane surfaces whose
 * row is 256-aligned (all its own surfaces are). MaterialKit snapshot surfaces ask
 * for CGBitmapGetAlignedBytesPerRow (32-byte) rows, so CARenderServerSnapshot
 * left them unrendered and the opaque App Library background stayed black.
 * Explicit rows are a minimum (clients query the row back), and the allocation
 * covers the height padded to 4 rows that a Turnip linear import requires;
 * sweetsurfrow=0 keeps the requested layout. */
#define SWEET_SURFACE_GPU_ROW_ALIGN     256
#define SWEET_SURFACE_TIMESTAMPS        0x800   /* offset in the state page */
#define SWEET_SURFACE_STATE_USE         0x18
#define SWEET_SURFACE_USE_MAX           65535u
#define SWEET_SURFACE_STATE_SEED        0xc     /* u32 in the state page */
#define kIOSurfaceLockReadOnly          0x1

static IOLock       *sweet_surface_lock;
static SweetSurface *sweet_surface_table[SWEET_SURFACE_MAX_ID];
static uint32_t      sweet_surface_users[SWEET_SURFACE_MAX_ID];
static uint32_t      sweet_surface_next_id = 1;
static uint32_t      sweet_surface_creates_logged;
static SInt32        sweet_surface_bts_logged;
extern "C" void sweet_prof_user_bt(const char *tag);
static uint64_t      sweet_surface_wired;       /* bytes of buffer surfaces */
#define SWEET_SURFACE_WIRED_MAX         (1536ull << 20)

static void sweet_surface_use_publish(SweetSurface *s)
{
 uint64_t value = ((uint64_t)s->useCounts[1] << 32) | (s->useCounts[0] + s->displayUses + s->usePoisoned);
 __atomic_store_n((uint64_t *)((uint8_t *)s->state->getBytesNoCopy() + SWEET_SURFACE_STATE_USE), value, __ATOMIC_RELEASE);
}
#if defined(FUXI)
void sweet_surface_display_poison(SweetSurface *s)
{
 IOLockLock(sweet_surface_lock); s->usePoisoned = true; sweet_surface_use_publish(s); IOLockUnlock(sweet_surface_lock);
}
#endif
bool sweet_surface_display_hold(SweetSurface *s)
{
 IOLockLock(sweet_surface_lock);
 bool ok = s->displayUses < SWEET_SURFACE_USE_MAX;
 if (ok) { s->displayUses++; sweet_surface_use_publish(s); }
 IOLockUnlock(sweet_surface_lock);
 return ok;
}
void sweet_surface_display_drop(SweetSurface *s)
{
 IOLockLock(sweet_surface_lock);
 if (s->displayUses) { s->displayUses--; sweet_surface_use_publish(s); }
 IOLockUnlock(sweet_surface_lock);
}

OSDefineMetaClassAndStructors(SweetSurface, OSObject);

#if defined(FUXI)
extern "C" uint64_t thread_get_runtime_self(void);
struct sweet_gpu_backing {
 SweetSurface *surface; /* weak: free() removes the mapping before releasing pixels */
 IOMemoryDescriptor *memory; /* strong + prepared while cached */
 uintptr_t source;
 uint64_t length, pageGeneration, generation, age;
 bool quarantine;
};
/* Cache -> lease only; producer leases never take the cache lock. */
static IOLock *sweet_gpu_lock, *sweet_writer_lock;
static SweetSurface *sweet_gpu_submitting;
static sweet_gpu_backing sweet_gpu_cache[FG_CACHE_SLOTS];
static uint64_t sweet_gpu_generation, sweet_gpu_age;
static struct fg_times sweet_gpu_times;
static SweetSurface *sweet_gpu_inflight; /* strong until fence/drain */
#define kIOSurfaceLockAvoidSync 0x2
static SweetSurface *sweet_present_readers[4];
static unsigned sweet_present_count;
static void (*sweet_present_wake)(void *);
static void *sweet_present_context;
static unsigned sweet_gpu_slot;
static uint64_t sweet_gpu_begin;
/* fuxigpuncimport bit 20: pre-cleans, skips (no writer since the last clean) and the zero-copy
 * class of each submitted frame (sweet_surface_scanout_class), logged with the 600-frame split. */
static unsigned sweet_gpu_precleans, sweet_gpu_preclean_skips, sweet_gpu_zc[8];
/* fuxigpuncimport bit 24: the surfaces in the display's direct-scanout slots, strong while slotted (index = kernel slot; table under
 * sweet_writer_lock, changed under sweet_gpu_lock too). A writer of a slotted surface waits while the kernel reports the slot busy. */
struct sweet_direct_slot { SweetSurface *surface; IOMemoryDescriptor *memory; uint64_t preparation, key; };
static sweet_direct_slot sweet_direct_slots[FG_DD_SLOTS_N];
static uint64_t sweet_direct_key, sweet_direct_pages[FG_DD_SLOT_PAGES];
static unsigned sweet_direct_held;
static bool sweet_gpu_init(void)
{
 if (!sweet_gpu_lock) {
  IOLock *lock = IOLockAlloc();
  if (!lock) return false;
  if (!OSCompareAndSwapPtr(NULL, lock, &sweet_gpu_lock)) IOLockFree(lock);
 }
 if (!sweet_writer_lock) {
  IOLock *lock = IOLockAlloc();
  if (!lock) return false;
  if (!OSCompareAndSwapPtr(NULL, lock, &sweet_writer_lock)) IOLockFree(lock);
 }
 return true;
}
static bool sweet_gpu_drop(unsigned slot)
{
 sweet_gpu_backing &c = sweet_gpu_cache[slot];
 if (!c.memory) return true;
 if (sweet_gpu_inflight && slot == sweet_gpu_slot) return false;
 if (c.quarantine || !fuxi_gpu_unmap(slot, &sweet_gpu_times)) {
  c.surface = NULL; c.quarantine = true;
  return false; /* preserve descriptor AND preparation until reset */
 }
 c.memory->complete(kIODirectionOut);
 c.memory->release();
 bzero(&c, sizeof(c));
 return true;
}
static void sweet_gpu_release(SweetSurface *s)
{
 if (!sweet_gpu_lock) return;
 uint64_t cpu = thread_get_runtime_self();
 IOLockLock(sweet_gpu_lock);
 for (unsigned i = 0; i < FG_CACHE_SLOTS; i++)
  if (sweet_gpu_cache[i].surface == s) (void)sweet_gpu_drop(i);
 sweet_gpu_times.cpu += thread_get_runtime_self()-cpu;
 IOLockUnlock(sweet_gpu_lock);
}
int sweet_surface_gpu_copy(SweetSurface *s, uintptr_t src, unsigned sx, unsigned sy,
    unsigned dx, unsigned dy, unsigned width, unsigned height)
{
 if (!fuxi_gpu_ready() || s->pixelFormat != 0x42475241u ||
     OSDynamicCast(IOBufferMemoryDescriptor, s->memory) == NULL || !sweet_gpu_init()) return 0;
 uint64_t cpu = thread_get_runtime_self(), start = mach_absolute_time();
 IOLockLock(sweet_gpu_lock);
 if (sweet_gpu_inflight) { IOLockUnlock(sweet_gpu_lock); return 2; }
 IOLockLock(sweet_writer_lock);
 if (s->gpuWriters || s->gpuQuarantined) {
  IOLockUnlock(sweet_writer_lock); IOLockUnlock(sweet_gpu_lock); return fuxi_gpu_scanout_owned() ? 3 : 0;
 }
 sweet_gpu_submitting = s;
 /* Bit 20: clean before the reservation, so client submits are not held across it. Writers
  * wait while submitting == s; one that arrives later bumps the serial and forces a clean. */
 bool preclean = fuxi_gpu_scanout_preclean(), clean = false;
 uint64_t cleanSerial = s->gpuWriterSerial, cleanEpoch = s->gpuWriterEpoch;
 if (preclean) clean = !s->gpuCleanValid || s->gpuCleanSerial != cleanSerial || s->gpuCleanEpoch != cleanEpoch;
 IOLockUnlock(sweet_writer_lock);
 if (clean) {
  uint64_t began = mach_absolute_time();
  fuxi_gpu_clean_source(src+s->offset, s->memory->getLength()-s->offset);
  sweet_gpu_times.clean += mach_absolute_time()-began;
  sweet_gpu_precleans++;
  IOLockLock(sweet_writer_lock);
  s->gpuCleanSerial = cleanSerial; s->gpuCleanEpoch = cleanEpoch; s->gpuCleanValid = true;
  IOLockUnlock(sweet_writer_lock);
 } else if (preclean) sweet_gpu_preclean_skips++;
 if (!fuxi_gpu_scanout_reserve()) {
  IOLockLock(sweet_writer_lock); sweet_gpu_submitting = NULL;
  IOLockWakeup(sweet_writer_lock, &sweet_gpu_inflight, false); IOLockUnlock(sweet_writer_lock);
  IOLockUnlock(sweet_gpu_lock); return 3;
 }
 unsigned slot = FG_CACHE_SLOTS, victim = 0, replacement = FG_CACHE_SLOTS;
 uint64_t length = s->memory->getLength()-s->offset;
 uintptr_t source = src+s->offset;
 uint64_t pageGeneration = s->memory->getPreparationID();
 for (unsigned i = 0; i < FG_CACHE_SLOTS; i++) {
  sweet_gpu_backing &c = sweet_gpu_cache[i];
  if (c.surface == s && c.memory == s->memory && c.source == source && c.length == length &&
      c.pageGeneration == pageGeneration) slot = i;
  if (c.surface == s) replacement = i;
  if (c.age < sweet_gpu_cache[victim].age) victim = i;
 }
 sweet_gpu_times.pin += mach_absolute_time()-start;
 sweet_gpu_times.attempts++;
 int result = 0;
 if (slot == FG_CACHE_SLOTS) {
  slot = replacement < FG_CACHE_SLOTS ? replacement : victim;
  if (!sweet_gpu_drop(slot) || !fuxi_gpu_ready()) goto out;
  start = mach_absolute_time();
  if (s->memory->prepare(kIODirectionOut) != kIOReturnSuccess) goto out;
  pageGeneration = s->memory->getPreparationID();
  if (pageGeneration < kIOPreparationIDAlwaysPrepared) {
   s->memory->complete(kIODirectionOut); goto out;
  }
  s->memory->retain();
  sweet_gpu_backing &c = sweet_gpu_cache[slot];
  c.surface = s; c.memory = s->memory; c.source = source; c.length = length;
  c.pageGeneration = pageGeneration; c.generation = ++sweet_gpu_generation;
  /* Wired kernel IOBuffer auto-prepares at creation. This extra matched
   * prepare holds its immutable physical page list across all cache hits.
   * XNU getPreparationID is the physical-list generation, checked on every
   * hit; replacement/reprepare forces unmap and a new slot generation. */
  sweet_gpu_times.pin += mach_absolute_time()-start;
  if (!fuxi_gpu_map(slot, c.generation, source, length, &sweet_gpu_times)) {
   (void)sweet_gpu_drop(slot); goto out;
  }
 } else sweet_gpu_times.hits++;
 sweet_gpu_cache[slot].age = ++sweet_gpu_age;
 result = fuxi_gpu_copy(slot, sweet_gpu_cache[slot].generation, source, length,
     s->bytesPerRow, sx, sy, dx, dy, width, height, &sweet_gpu_times);
 if (result == 2 && preclean) {
  unsigned c = width < 1080 || height < 2400 ? 4 : sweet_surface_scanout_class(s,
      s->offset+(uint64_t)sy*s->bytesPerRow+(uint64_t)sx*4, (uint64_t)(height-1)*s->bytesPerRow+(uint64_t)width*4);
  sweet_gpu_zc[c < 8 ? c : 0]++;
 }
 IOLockLock(sweet_writer_lock);
 if (result == 2) {
  s->retain();
  sweet_gpu_inflight = s;
  sweet_gpu_slot = slot;
  sweet_gpu_begin = mach_absolute_time();
 }
 if (result < 0) {
  sweet_gpu_cache[slot].quarantine = true;
  s->gpuQuarantined = true; sweet_surface_display_poison(s);
  s->retain(); /* failed submit/drain: reject writers, preserve until reset */
 }
 IOLockUnlock(sweet_writer_lock);
 out:
 IOLockLock(sweet_writer_lock); sweet_gpu_submitting = NULL;
 IOLockWakeup(sweet_writer_lock, &sweet_gpu_inflight, false); IOLockUnlock(sweet_writer_lock);
 fuxi_gpu_scanout_release();
 sweet_gpu_times.cpu += thread_get_runtime_self()-cpu;
 IOLockUnlock(sweet_gpu_lock);
 return result;
}
/* The display calls this before touching any framebuffer pixels, including
 * power-off clearing. Keep the strong surface ref across every busy tick. */
int sweet_surface_gpu_poll(uint64_t *elapsed)
{
 if (!sweet_gpu_lock) return 1;
 uint64_t cpu = thread_get_runtime_self();
 IOLockLock(sweet_gpu_lock);
 SweetSurface *release = NULL;
 int result = 1;
 if (sweet_gpu_inflight) {
  result = fuxi_gpu_poll(&sweet_gpu_times);
  if (result) {
   *elapsed = mach_absolute_time()-sweet_gpu_begin;
   IOLockLock(sweet_writer_lock);
   if (result == -2) {
    sweet_gpu_cache[sweet_gpu_slot].quarantine = true;
    sweet_gpu_inflight->gpuQuarantined = true; sweet_surface_display_poison(sweet_gpu_inflight);
   }
   else release = sweet_gpu_inflight;
   /* Failed-drain surface AND descriptor stay retained until reset. */
   sweet_gpu_inflight = NULL;
   IOLockWakeup(sweet_writer_lock, &sweet_gpu_inflight, false);
   IOLockUnlock(sweet_writer_lock);
  }
 }
 sweet_gpu_times.cpu += thread_get_runtime_self()-cpu;
 IOLockUnlock(sweet_gpu_lock);
 if (release) release->release(); /* free() takes the cache lock */
 return result;
}
bool sweet_surface_present_begin(SweetSurface **surfaces, unsigned count, uint64_t *ready = NULL, const uint64_t *epochs = NULL)
{
 if (count > 4 || !sweet_gpu_init()) return false;
 IOLockLock(sweet_writer_lock);
 bool ok = sweet_present_count == 0;
 for (unsigned i = 0; i < count; i++) if (surfaces[i]->gpuWriters || surfaces[i]->gpuQuarantined ||
     (epochs && surfaces[i]->gpuWriterEpoch != epochs[i])) ok = false;
 if (ok) {
  for (unsigned i = 0; i < count; i++) {
   sweet_present_readers[i] = surfaces[i];
   if (ready && surfaces[i]->gpuReadyTime > *ready) *ready = surfaces[i]->gpuReadyTime;
  }
  sweet_present_count = count;
 }
 IOLockUnlock(sweet_writer_lock);
 return ok;
}
void sweet_surface_present_end(void)
{
 IOLockLock(sweet_writer_lock);
 sweet_present_count = 0;
 IOLockWakeup(sweet_writer_lock, &sweet_gpu_inflight, false);
 IOLockUnlock(sweet_writer_lock);
}
void sweet_surface_present_notify(void (*wake)(void *), void *context)
{
 if (!sweet_gpu_init()) return;
 IOLockLock(sweet_writer_lock);
 sweet_present_wake = wake; sweet_present_context = context;
 IOLockUnlock(sweet_writer_lock);
}
static bool sweet_surface_present_reading(SweetSurface *s)
{
 for (unsigned i = 0; i < sweet_present_count; i++) if (sweet_present_readers[i] == s) return true;
 return false;
}
/* Under sweet_writer_lock: s sits in a direct slot the display may still read. A wait longer than 100 ms is counted and ends. */
static bool sweet_direct_reading(SweetSurface *s, uint64_t *since)
{
 unsigned k = 0;
 while (k < FG_DD_SLOTS_N && sweet_direct_slots[k].surface != s) k++;
 if (k == FG_DD_SLOTS_N || !(fuxi_gpu_scanout_direct_busy() & (1u << k))) return false;
 uint64_t now = mach_absolute_time(), bound = 0;
 nanoseconds_to_absolutetime(100000000ull, &bound);
 if (!*since) *since = now;
 else if (now - *since > bound) { fuxi_gpu_scanout_direct_refused(FG_DD_WHY); return false; }
 return true;
}
static void sweet_direct_nap(void)
{
 uint64_t deadline = 0;
 nanoseconds_to_absolutetime(250000ull, &deadline);
 (void)IOLockSleepDeadline(sweet_writer_lock, &sweet_gpu_inflight, mach_absolute_time() + deadline, THREAD_UNINT);
}
/* AvoidSync reserves a pending producer without waiting; it must take a
 * normal writer lease before touching pixels. Display reads never sleep. */
static bool sweet_gpu_writer(SweetSurface *s, bool lock, bool pending = false)
{
 if (!sweet_gpu_init()) return false;
 IOLockLock(sweet_writer_lock);
 if (lock) {
  if (s->gpuWriters == ~0u || s->gpuWriterSerial == ~0ull || s->gpuWriterEpoch == ~0ull) { s->gpuQuarantined = true; sweet_surface_display_poison(s); }
  if (s->gpuQuarantined) { IOLockUnlock(sweet_writer_lock); return false; }
  s->gpuWriterSerial++;
  /* Pending-to-active conversion stays in the outstanding writer epoch. */
  if (!s->gpuWriters) s->gpuWriterEpoch++;
  uint64_t since = 0; /* bit 24: the display may still fetch the surface itself; no wakeup comes for that, so nap */
  while (!pending && (sweet_gpu_submitting == s || sweet_gpu_inflight == s || sweet_surface_present_reading(s) || sweet_direct_reading(s, &since))) {
   if (since) sweet_direct_nap(); else IOLockSleep(sweet_writer_lock, &sweet_gpu_inflight, THREAD_UNINT);
  }
  if (s->gpuWriters == ~0u) { s->gpuQuarantined = true; sweet_surface_display_poison(s); }
  if (!s->gpuQuarantined) {
   s->gpuWriters++; fuxi_gpu_present_writer(s->id, !pending); /* bit 16: lock-free, no GPU lock */
   if (!pending) s->gpuLockSeq = fuxi_gpu_abi_seq();
  }
 } else if (s->gpuWriters && !--s->gpuWriters && !s->gpuQuarantined) {
  s->gpuReadyTime = mach_absolute_time();
  s->gpuWriterFence = fuxi_gpu_abi_seq() != s->gpuLockSeq ? s->gpuLockSeq + 1 : 0; /* bit 24: the writer job's fence, as copy priority */
  if (sweet_present_wake) sweet_present_wake(sweet_present_context);
 }
 bool ok = !s->gpuQuarantined;
 IOLockUnlock(sweet_writer_lock);
 return ok;
}
/* fuxigpuncimport bit 24: the display scans the surface out itself (fuxi_gpu_scanout_direct_*). The display path calls this between
 * present_begin and present_end (no writer holds s) for a one-layer frame cropped to the panel. Eligible: BGRA8 1125x2436, stride 4608,
 * crop 22,18 1080x2400, page-aligned offset, wired IOBuffer backing. A miss maps the surface's 4 KiB pages into the slot the kernel
 * chose and releases the surface that slot held (the kernel never picks a slot on screen, fetched, pending or queued). The pixels
 * are cleaned to PoC unless no writer came since the last clean (as bit 20). 2 queued, 3 retry, 0 copy. */
int sweet_surface_direct(SweetSurface *s, uintptr_t src, unsigned sx, unsigned sy, unsigned width, unsigned height)
{
 SweetSurface *drop[FG_DD_SLOTS_N] = {};
 unsigned drops = 0, why = FG_DD_WHY, have = FG_DD_SLOTS_N, slot = 0, n = 0;
 if (!sweet_gpu_init()) return 0;
 if (!fuxi_gpu_scanout_direct_on()) {
  /* Off (fault, or never armed): give back the slotted surfaces the display no longer reads. */
  if (!__atomic_load_n(&sweet_direct_held, __ATOMIC_RELAXED)) return 0;
  unsigned busy = fuxi_gpu_scanout_direct_busy();
  IOLockLock(sweet_gpu_lock); IOLockLock(sweet_writer_lock);
  for (unsigned k = 0; k < FG_DD_SLOTS_N; k++) if (sweet_direct_slots[k].surface && !(busy & (1u << k))) {
   drop[drops++] = sweet_direct_slots[k].surface; bzero(&sweet_direct_slots[k], sizeof(sweet_direct_slots[k])); sweet_direct_held--;
  }
  IOLockUnlock(sweet_writer_lock); IOLockUnlock(sweet_gpu_lock);
  for (unsigned i = 0; i < drops; i++) drop[i]->release();
  return 0;
 }
 IOBufferMemoryDescriptor *buffer = OSDynamicCast(IOBufferMemoryDescriptor, s->memory);
 uint64_t bytes = s->height ? (uint64_t)(s->height - 1)*s->bytesPerRow + (uint64_t)s->width*4 : 0;
 unsigned c = sweet_surface_scanout_class(s, s->offset, bytes);
 if (c == 2) why = FG_DD_FORMAT;
 else if (c == 3 || s->bytesPerRow != 4608) why = FG_DD_STRIDE;
 else if (c == 5 || !c || !buffer || !src) why = FG_DD_BACKING;
 else if (s->width != 1125 || s->height != 2436 || sx != 22 || sy != 18 || width != 1080 || height != 2400) why = FG_DD_GEOMETRY;
 else if (s->offset & 4095) why = FG_DD_OFFSET;
 if (why != FG_DD_WHY) { fuxi_gpu_scanout_direct_refused(why); return 0; }
 uint64_t preparation = s->memory->getPreparationID(), key = 0;
 IOLockLock(sweet_gpu_lock);
 if (sweet_gpu_inflight) { IOLockUnlock(sweet_gpu_lock); fuxi_gpu_scanout_direct_refused(FG_DD_STATE); return 0; }
 IOLockLock(sweet_writer_lock);
 bool writer = s->gpuWriters || s->gpuQuarantined;
 uint32_t fence = s->gpuWriterFence;
 uint64_t serial = s->gpuWriterSerial, epoch = s->gpuWriterEpoch;
 bool clean = !s->gpuCleanValid || s->gpuCleanSerial != serial || s->gpuCleanEpoch != epoch;
 for (unsigned k = 0; k < FG_DD_SLOTS_N; k++) if (sweet_direct_slots[k].surface == s) have = k;
 if (have < FG_DD_SLOTS_N) {
  sweet_direct_slot &d = sweet_direct_slots[have];
  if (d.memory == s->memory && d.preparation == preparation) key = d.key;
 } else key = ++sweet_direct_key;
 IOLockUnlock(sweet_writer_lock);
 if (writer) { IOLockUnlock(sweet_gpu_lock); return 3; }
 if (!key) { IOLockUnlock(sweet_gpu_lock); fuxi_gpu_scanout_direct_refused(FG_DD_BACKING); return 0; } /* new physical list while slotted */
 uint64_t old = 0;
 int r = fuxi_gpu_scanout_direct_slot(key, &slot, &old), q = 0;
 if (r == 1 && sweet_direct_slots[slot].surface != s) { r = 0; fuxi_gpu_scanout_direct_refused(FG_DD_STATE); }
 if (r == 2) {
  /* The kernel emptied the slot: its surface (if any) is no longer read. */
  IOLockLock(sweet_writer_lock);
  if (sweet_direct_slots[slot].surface) { drop[drops++] = sweet_direct_slots[slot].surface; sweet_direct_held--; }
  bzero(&sweet_direct_slots[slot], sizeof(sweet_direct_slots[slot]));
  IOLockUnlock(sweet_writer_lock);
  uint64_t off = s->offset & ~4095ull, end = s->offset + bytes;
  for (; off < end && n < FG_DD_SLOT_PAGES; off += 4096) {
   IOByteCount length = 0;
   addr64_t pa = buffer->getPhysicalSegment(off, &length, kIOMemoryMapperNone);
   sweet_direct_pages[n++] = length >= 4096 ? pa : 0; /* the kernel refuses 0 and unaligned pages */
  }
  if (off < end) n = 0;
 }
 if (r) {
  if (clean) {
   fuxi_gpu_clean_source(src + s->offset, bytes);
   IOLockLock(sweet_writer_lock); s->gpuCleanSerial = serial; s->gpuCleanEpoch = epoch; s->gpuCleanValid = true; IOLockUnlock(sweet_writer_lock);
  }
  q = fuxi_gpu_scanout_direct_queue(slot, key, r == 2 ? sweet_direct_pages : NULL, n, src + s->offset, fence);
  if (q == 1 && r == 2) {
   s->retain();
   IOLockLock(sweet_writer_lock);
   sweet_direct_slots[slot] = sweet_direct_slot{s, s->memory, preparation, key}; sweet_direct_held++;
   IOLockUnlock(sweet_writer_lock);
  }
 }
 IOLockUnlock(sweet_gpu_lock);
 for (unsigned i = 0; i < drops; i++) drop[i]->release(); /* free() takes the cache lock */
 return q == 1 ? 2 : 0;
}
uint64_t sweet_surface_writer_serial(SweetSurface *s, uint64_t *epoch)
{
 if (!sweet_gpu_init()) { if (epoch) *epoch = ~0ull; return ~0ull; }
 IOLockLock(sweet_writer_lock);
 uint64_t serial = s->gpuWriterSerial; if (epoch) *epoch = s->gpuWriterEpoch;
 IOLockUnlock(sweet_writer_lock);
 return serial;
}
void sweet_surface_gpu_stats(struct fg_times *t)
{
 bzero(t, sizeof(*t));
 if (!sweet_gpu_lock) return;
 IOLockLock(sweet_gpu_lock);
 *t = sweet_gpu_times;
 bzero(&sweet_gpu_times, sizeof(sweet_gpu_times));
 if (fuxi_gpu_scanout_preclean()) {
  IOLog("FUXI SCANOUT: preclean cleans=%u skips=%u zerocopy eligible=%u format=%u stride=%u size=%u backing=%u scattered=%u high=%u unknown=%u\n",
      sweet_gpu_precleans, sweet_gpu_preclean_skips, sweet_gpu_zc[1], sweet_gpu_zc[2], sweet_gpu_zc[3], sweet_gpu_zc[4],
      sweet_gpu_zc[5], sweet_gpu_zc[6], sweet_gpu_zc[7], sweet_gpu_zc[0]);
  sweet_gpu_precleans = sweet_gpu_preclean_skips = 0; bzero(sweet_gpu_zc, sizeof(sweet_gpu_zc));
 }
 IOLockUnlock(sweet_gpu_lock);
}
/* Zero-copy survey, read only: could VIG0 fetch [offset, offset+bytes) directly? 1 yes (one
 * physical run below 4 GiB: SRC0 is a 32-bit PA, the display SMMU is untouched), 2 format,
 * 3 stride (64-byte multiple, 16-bit YSTRIDE0), 5 backing, 6 scattered pages, 7 above 4 GiB.
 * Size (4) is the caller's crop test. A wired IOBuffer's pages never move: cache the class. */
unsigned sweet_surface_scanout_class(SweetSurface *s, uint64_t offset, uint64_t bytes)
{
 if (s->pixelFormat != 0x42475241u || s->bytesPerElement != 4 || s->elementWidth != 1 || s->elementHeight != 1) return 2;
 if ((s->bytesPerRow & 63) || s->bytesPerRow > 0xffffu) return 3;
 IOBufferMemoryDescriptor *buffer = OSDynamicCast(IOBufferMemoryDescriptor, s->memory);
 if (!buffer || !bytes || offset+bytes > buffer->getLength()) return 5;
 if (!s->scanoutClass) {
  IOByteCount length = 0;
  addr64_t pa = buffer->getPhysicalSegment(offset, &length, kIOMemoryMapperNone);
  s->scanoutClass = !pa ? 5 : pa+bytes > (1ull << 32) ? 7 : length < bytes ? 6 : 1;
 }
 return s->scanoutClass;
}
#endif

void
SweetSurface::free()
{
#if defined(FUXI)
 sweet_gpu_release(this);
#endif
	if (OSDynamicCast(IOBufferMemoryDescriptor, memory) != NULL) {
		OSAddAtomic64(-(SInt64)allocSize, (volatile SInt64 *)&sweet_surface_wired);
	}
	OSSafeReleaseNULL(kernelMap);
	OSSafeReleaseNULL(memory);
	OSSafeReleaseNULL(state);
	OSObject::free();
}

uintptr_t
SweetSurface::kernelAddress()
{
	IOBufferMemoryDescriptor *buffer = OSDynamicCast(IOBufferMemoryDescriptor, memory);
	if (buffer != NULL) {
		return (uintptr_t)buffer->getBytesNoCopy();
	}
	IOLockLock(sweet_surface_lock);
	if (kernelMap == NULL && memory != NULL) {
		kernelMap = memory->map(kIOMapWriteCombineCache);
	}
	IOLockUnlock(sweet_surface_lock);
	return kernelMap != NULL ? (uintptr_t)kernelMap->getVirtualAddress() : 0;
}

static uint32_t *
sweet_surface_seed(SweetSurface *s)
{
	return (uint32_t *)((uint8_t *)s->state->getBytesNoCopy() + SWEET_SURFACE_STATE_SEED);
}

/*
 * Give the surface an id and a table slot; the table holds one retain and a
 * user count (creators, client mappings, kernel holders). Lock held.
 */
static bool
sweet_surface_insert_locked(SweetSurface *s)
{
	for (uint32_t n = 0; n < SWEET_SURFACE_MAX_ID - 1; n++) {
		uint32_t id = sweet_surface_next_id;
		sweet_surface_next_id = id + 1 < SWEET_SURFACE_MAX_ID ? id + 1 : 1;
		if (sweet_surface_table[id] == NULL) {
			s->id = id;
			s->retain();
			sweet_surface_table[id] = s;
			sweet_surface_users[id] = 1;
			*(uint64_t *)s->state->getBytesNoCopy() = id;   /* registry id */
			return true;
		}
	}
	return false;
}

/* Add a user to a surface that may have left the table (port lookups). */
static bool
sweet_surface_get(SweetSurface *s)
{
	bool ok = true;
	IOLockLock(sweet_surface_lock);
	if (sweet_surface_table[s->id] == s) {
		sweet_surface_users[s->id]++;
	} else if (sweet_surface_table[s->id] == NULL) {
		s->retain();
		sweet_surface_table[s->id] = s;
		sweet_surface_users[s->id] = 1;
	} else {
		ok = false;
	}
	IOLockUnlock(sweet_surface_lock);
	return ok;
}

/* Drop a user; the last one removes the surface from the table. */
static void
sweet_surface_put(SweetSurface *s)
{
	bool drop = false;
	IOLockLock(sweet_surface_lock);
	if (sweet_surface_table[s->id] == s && --sweet_surface_users[s->id] == 0) {
		sweet_surface_table[s->id] = NULL;
		drop = true;
	}
	IOLockUnlock(sweet_surface_lock);
	if (drop) {
		s->release();
	}
}

SweetSurface *
sweet_surface_copy(uint32_t id)
{
	SweetSurface *s = NULL;
	if (id == 0 || id >= SWEET_SURFACE_MAX_ID || sweet_surface_lock == NULL) {
		return NULL;
	}
	IOLockLock(sweet_surface_lock);
	s = sweet_surface_table[id];
	if (s != NULL) {
		s->retain();
	}
	IOLockUnlock(sweet_surface_lock);
	return s;
}

/* The table lock; the framebuffer may publish a surface before the root starts. */
static void
sweet_surface_init_lock(void)
{
	if (sweet_surface_lock == NULL) {
		IOLock *lock = IOLockAlloc();
		if (!OSCompareAndSwapPtr(NULL, lock, &sweet_surface_lock)) {
			IOLockFree(lock);
		}
	}
}

static SweetSurface *
sweet_surface_alloc(void)
{
	sweet_surface_init_lock();
	SweetSurface *s = OSTypeAlloc(SweetSurface);
	if (s == NULL || !s->init()) {
		OSSafeReleaseNULL(s);
		return NULL;
	}
	s->state = IOBufferMemoryDescriptor::inTaskWithOptions(kernel_task,
	    kIODirectionInOut | kIOMemoryKernelUserShared, page_size, page_size);
	if (s->state == NULL) {
		s->release();
		return NULL;
	}
	bzero(s->state->getBytesNoCopy(), page_size);
	return s;
}

static bool
sweet_surface_publish(SweetSurface *s)
{
	bool ok;
	IOLockLock(sweet_surface_lock);
	ok = sweet_surface_insert_locked(s);
	IOLockUnlock(sweet_surface_lock);
	return ok;
}

SweetSurface *
sweet_surface_create_physical(uint64_t phys, uint32_t width, uint32_t height,
    uint32_t bytesPerRow, uint32_t pixelFormat)
{
	SweetSurface *s = sweet_surface_alloc();
	if (s == NULL) {
		return NULL;
	}
	s->width = width;
	s->height = height;
	s->bytesPerRow = bytesPerRow;
	s->bytesPerElement = 4;
	s->elementWidth = s->elementHeight = 1;
	s->pixelFormat = pixelFormat;
	s->allocSize = (uint32_t)round_page(bytesPerRow * height);
	s->memory = IOMemoryDescriptor::withPhysicalAddress(phys, s->allocSize, kIODirectionInOut);
	s->mapCache = kIOMapWriteCombineCache;
	if (s->memory == NULL || !sweet_surface_publish(s)) {
		s->release();
		return NULL;
	}
	return s;
}

mach_port_name_t
sweet_surface_port_in_task(SweetSurface *s, task_t task)
{
	mach_port_name_t name = 0;
	IOUserClient::copyPortNameForObjectInTask(task, s, &name);
	return name;
}

static uint32_t
sweet_dict_u32(OSDictionary *dict, const char *key, uint32_t def)
{
	OSNumber *n = OSDynamicCast(OSNumber, dict->getObject(key));
	return n != NULL ? n->unsigned32BitValue() : def;
}

static uint32_t
sweet_div_up(uint32_t a, uint32_t b)
{
	return b > 1 ? (a + b - 1) / b : a;
}

static uint32_t
sweet_row_bytes(uint32_t width, uint32_t ew, uint32_t bpe)
{
	uint64_t row = (uint64_t)sweet_div_up(width, ew) * bpe;
	return (uint32_t)((row + SWEET_SURFACE_ROW_ALIGN - 1) & ~(uint64_t)(SWEET_SURFACE_ROW_ALIGN - 1));
}

static bool
sweet_surface_gpu_rows(void)
{
	static int enabled = -1;
	if (enabled < 0) {
		uint32_t v = 1;
		(void)PE_parse_boot_argn("sweetsurfrow", &v, sizeof(v));
		enabled = v != 0;
	}
	return enabled != 0;
}

/* Geometry from the creation properties (IOSurface.h keys). */
static IOReturn
sweet_surface_layout(SweetSurface *s, OSDictionary *props)
{
	uint64_t end;

	s->width = sweet_dict_u32(props, "IOSurfaceWidth", 0);
	s->height = sweet_dict_u32(props, "IOSurfaceHeight", 0);
	s->bytesPerElement = (uint16_t)sweet_dict_u32(props, "IOSurfaceBytesPerElement", 4);
	s->elementWidth = (uint8_t)sweet_dict_u32(props, "IOSurfaceElementWidth", 1);
	s->elementHeight = (uint8_t)sweet_dict_u32(props, "IOSurfaceElementHeight", 1);
	s->pixelFormat = sweet_dict_u32(props, "IOSurfacePixelFormat", 0);
	s->cacheMode = sweet_dict_u32(props, "IOSurfaceCacheMode", 0);
	s->offset = sweet_dict_u32(props, "IOSurfaceOffset", 0);
	if (s->width > SWEET_SURFACE_MAX_DIM || s->height > SWEET_SURFACE_MAX_DIM ||
	    s->bytesPerElement > 64 || s->elementWidth == 0 || s->elementHeight == 0) {
		return kIOReturnBadArgument;
	}
	s->bytesPerRow = sweet_dict_u32(props, "IOSurfaceBytesPerRow",
	    sweet_row_bytes(s->width, s->elementWidth, s->bytesPerElement));
	if (s->bytesPerRow < (uint64_t)sweet_div_up(s->width, s->elementWidth) * s->bytesPerElement) {
		return kIOReturnBadArgument;
	}
	uint32_t rows = sweet_div_up(s->height, s->elementHeight);
	if (s->bytesPerElement == 4 && props->getObject("IOSurfacePlaneInfo") == NULL && sweet_surface_gpu_rows()) {
		if ((s->bytesPerRow & (SWEET_SURFACE_GPU_ROW_ALIGN - 1)) != 0) {
			s->bytesPerRow = (s->bytesPerRow + SWEET_SURFACE_GPU_ROW_ALIGN - 1) & ~(uint32_t)(SWEET_SURFACE_GPU_ROW_ALIGN - 1);
		}
		/* Turnip sizes a linear image's last level for a height padded to 4 rows
		 * (blit over-fetch), so a 1065x282 import needs 284 rows of memory. */
		rows = (rows + 3) & ~3u;
	}
	end = (uint64_t)s->offset + (uint64_t)s->bytesPerRow * rows;

	OSArray *planes = OSDynamicCast(OSArray, props->getObject("IOSurfacePlaneInfo"));
	s->planeCount = 0;
	if (planes != NULL) {
		uint64_t next = s->offset;
		if (planes->getCount() > SWEET_SURFACE_MAX_PLANES) {
			return kIOReturnBadArgument;
		}
		for (unsigned int i = 0; i < planes->getCount(); i++) {
			OSDictionary *pd = OSDynamicCast(OSDictionary, planes->getObject(i));
			struct sweet_surface_plane *p = &s->planes[i];
			if (pd == NULL) {
				return kIOReturnBadArgument;
			}
			p->width = sweet_dict_u32(pd, "IOSurfacePlaneWidth", s->width);
			p->height = sweet_dict_u32(pd, "IOSurfacePlaneHeight", s->height);
			p->bytesPerElement = (uint16_t)sweet_dict_u32(pd, "IOSurfacePlaneBytesPerElement", 1);
			p->elementWidth = (uint8_t)sweet_dict_u32(pd, "IOSurfacePlaneElementWidth", 1);
			p->elementHeight = (uint8_t)sweet_dict_u32(pd, "IOSurfacePlaneElementHeight", 1);
			if (p->width > SWEET_SURFACE_MAX_DIM || p->height > SWEET_SURFACE_MAX_DIM ||
			    p->bytesPerElement > 64 || p->elementWidth == 0 || p->elementHeight == 0) {
				return kIOReturnBadArgument;
			}
			p->bytesPerRow = sweet_dict_u32(pd, "IOSurfacePlaneBytesPerRow",
			    sweet_row_bytes(p->width, p->elementWidth, p->bytesPerElement));
			p->offset = sweet_dict_u32(pd, "IOSurfacePlaneOffset", (uint32_t)next);
			p->size = sweet_dict_u32(pd, "IOSurfacePlaneSize",
			    p->bytesPerRow * sweet_div_up(p->height, p->elementHeight));
			next = round_page((uint64_t)p->offset + p->size);
			if ((uint64_t)p->offset + p->size > end) {
				end = (uint64_t)p->offset + p->size;
			}
		}
		s->planeCount = planes->getCount();
	}

	uint64_t alloc = sweet_dict_u32(props, "IOSurfaceAllocSize", 0);
	if (alloc < end) {
		alloc = end;
	}
	alloc = round_page(alloc);
	if (alloc == 0 || alloc > SWEET_SURFACE_MAX_ALLOC) {
		return kIOReturnNoMemory;
	}
	s->allocSize = (uint32_t)alloc;
	return kIOReturnSuccess;
}

/* Per-client mapping of one surface. */
class SweetSurfaceRef : public OSObject {
	OSDeclareDefaultStructors(SweetSurfaceRef);

public:
	SweetSurface *surface;
	IOMemoryMap  *data;
	IOMemoryMap  *state;
	uint32_t      count;
 uint32_t uses[2];

	bool init() APPLE_KEXT_OVERRIDE { return OSObject::init(); }
	void free() APPLE_KEXT_OVERRIDE;
};

OSDefineMetaClassAndStructors(SweetSurfaceRef, OSObject);

void
SweetSurfaceRef::free()
{
	OSSafeReleaseNULL(data);
	OSSafeReleaseNULL(state);
	if (surface != NULL) {
  IOLockLock(sweet_surface_lock);
  for (unsigned i = 0; i < 2; i++) surface->useCounts[i] -= uses[i];
  sweet_surface_use_publish(surface);
  IOLockUnlock(sweet_surface_lock);
		sweet_surface_put(surface);
		surface->release();
		surface = NULL;
	}
	OSObject::free();
}

class IOCoreSurfaceRoot : public IOService {
	OSDeclareDefaultStructors(IOCoreSurfaceRoot);

public:
	bool start(IOService *provider) APPLE_KEXT_OVERRIDE;
};

OSDefineMetaClassAndStructors(IOCoreSurfaceRoot, IOService);

bool
IOCoreSurfaceRoot::start(IOService *provider)
{
	if (!IOService::start(provider)) {
		return false;
	}
	sweet_surface_init_lock();
	setProperty(kIOUserClientClassKey, "IOSurfaceRootUserClient");
	registerService();
	IOLog("SWEET SURF: IOCoreSurfaceRoot registered\n");
	return true;
}

class IOSurfaceRootUserClient : public IOUserClient {
	OSDeclareDefaultStructors(IOSurfaceRootUserClient);

	task_t   fTask;
	IOLock  *fLock;
	OSArray *fRefs;

	SweetSurfaceRef *findRef(uint32_t id, unsigned int *index);
	IOReturn addRef(SweetSurface *s, SweetSurfaceRef **refOut, bool *existing);
	IOReturn returnSurface(SweetSurface *s, IOExternalMethodArguments *args);
	IOReturn create(IOExternalMethodArguments *args);

public:
	bool initWithTask(task_t owningTask, void *securityID, UInt32 type) APPLE_KEXT_OVERRIDE;
	void free() APPLE_KEXT_OVERRIDE;
	IOReturn clientClose(void) APPLE_KEXT_OVERRIDE;
	IOReturn externalMethod(uint32_t selector, IOExternalMethodArguments *args,
	    IOExternalMethodDispatch *dispatch, OSObject *target,
	    void *reference) APPLE_KEXT_OVERRIDE;
	IOExternalTrap *getTargetAndTrapForIndex(IOService **targetP, UInt32 index) APPLE_KEXT_OVERRIDE;

	IOReturn useCount(uint32_t id, uint32_t category, bool increment);
 IOReturn trapIncrementUse(void *id, void *category, void *, void *, void *, void *);
 IOReturn trapDecrementUse(void *id, void *category, void *, void *, void *, void *);
	IOReturn trapLock(void *id, void *options, void *seedOut, void *, void *, void *);
	IOReturn trapUnlock(void *id, void *options, void *seedOut, void *, void *, void *);
	IOReturn trapRelease(void *id, void *, void *, void *, void *, void *);
};

OSDefineMetaClassAndStructors(IOSurfaceRootUserClient, IOUserClient);

static uint64_t sweet_surf_logged;      /* selectors / traps already logged (< 64) */

static void
sweet_surf_log_once(const char *what, uint32_t n)
{
	uint32_t bit = (what[0] == 't' ? 48 : 0) + n;
	if (bit >= 64 || (sweet_surf_logged & (1ULL << bit))) {
		return;
	}
	sweet_surf_logged |= 1ULL << bit;
	char name[32];
	proc_selfname(name, sizeof(name));
	IOLog("SWEET SURF: %s %s %u\n", name, what, n);
}

bool
IOSurfaceRootUserClient::initWithTask(task_t owningTask, void *securityID, UInt32 type)
{
	if (!IOUserClient::initWithTask(owningTask, securityID, type)) {
		return false;
	}
	fTask = owningTask;
	fLock = IOLockAlloc();
	fRefs = OSArray::withCapacity(16);
	return fLock != NULL && fRefs != NULL;
}

void
IOSurfaceRootUserClient::free()
{
	OSSafeReleaseNULL(fRefs);
	if (fLock != NULL) {
		IOLockFree(fLock);
		fLock = NULL;
	}
	IOUserClient::free();
}

IOReturn
IOSurfaceRootUserClient::clientClose(void)
{
	IOLockLock(fLock);
	fRefs->flushCollection();
	IOLockUnlock(fLock);
	terminate();
	return kIOReturnSuccess;
}

/* Lock held. */
SweetSurfaceRef *
IOSurfaceRootUserClient::findRef(uint32_t id, unsigned int *index)
{
	for (unsigned int i = 0; i < fRefs->getCount(); i++) {
		SweetSurfaceRef *ref = (SweetSurfaceRef *)fRefs->getObject(i);
		if (ref->surface->id == id) {
			if (index != NULL) {
				*index = i;
			}
			return ref;
		}
	}
	return NULL;
}

/*
 * Count one more create/lookup of s by this client, mapping it into the
 * task the first time. Takes over the caller's user count on s.
 *
 * A lookup of a surface this client already has maps nothing and counts
 * nothing (*existing): IOSurface.framework then gets base address -1 in the
 * lock result and reuses its cached IOSurfaceClient. Counting it instead
 * made the framework build a second client for the same id; releasing that
 * one (traps 4 + 5) unmapped the memory under the first (backboardd
 * snapshot crash in CGDataProviderCreateWithData).
 */
IOReturn
IOSurfaceRootUserClient::addRef(SweetSurface *s, SweetSurfaceRef **refOut, bool *existing)
{
	*existing = false;
	IOLockLock(fLock);
	SweetSurfaceRef *ref = findRef(s->id, NULL);
	if (ref != NULL && ref->surface == s) {
		IOLockUnlock(fLock);
		sweet_surface_put(s);
		*refOut = ref;
		*existing = true;
		return kIOReturnSuccess;
	}
	IOLockUnlock(fLock);

	ref = OSTypeAlloc(SweetSurfaceRef);
	if (ref == NULL || !ref->init()) {
		OSSafeReleaseNULL(ref);
		sweet_surface_put(s);
		return kIOReturnNoMemory;
	}
	s->retain();
	ref->surface = s;
	ref->count = 1;
	ref->data = s->memory->createMappingInTask(fTask, 0, kIOMapAnywhere | s->mapCache);
	ref->state = s->state->createMappingInTask(fTask, 0, kIOMapAnywhere | kIOMapReadOnly);
	if (ref->data == NULL || ref->state == NULL) {
		ref->release();
		return kIOReturnVMError;
	}
	IOLockLock(fLock);
	SweetSurfaceRef *raced = findRef(s->id, NULL);
	if (raced != NULL && raced->surface == s) {
		/* Another thread mapped it meanwhile: count there, drop ours. */
		raced->count++;
		IOLockUnlock(fLock);
		ref->release();
		*refOut = raced;
		return kIOReturnSuccess;
	}
	fRefs->setObject(ref);
	IOLockUnlock(fLock);
	ref->release();         /* fRefs holds it */
	*refOut = ref;
	return kIOReturnSuccess;
}

static void
put32(uint8_t *p, uint32_t off, uint32_t v)
{
	memcpy(p + off, &v, 4);
}

static void
put64(uint8_t *p, uint32_t off, uint64_t v)
{
	memcpy(p + off, &v, 8);
}

/* IOSurface.framework's lock result (copied to IOSurfaceClient + 0x68). */
static void
sweet_surface_fill_result(SweetSurface *s, SweetSurfaceRef *ref, uint8_t *out)
{
	bzero(out, SWEET_SURFACE_RESULT_SIZE);
	put64(out, 0x00, ref->data->getAddress());
	put64(out, 0x08, ref->state->getAddress());
	put64(out, 0x10, ref->state->getAddress() + SWEET_SURFACE_TIMESTAMPS);
	put32(out, 0x18, s->id);
	put32(out, 0x1c, s->allocSize);
	put32(out, 0x20, s->width);
	put32(out, 0x24, s->height);
	put32(out, 0x28, s->bytesPerRow);
	put32(out, 0x2c, s->offset);
	put32(out, 0x30, s->pixelFormat);
	put32(out, 0x34, s->allocSize);
	put32(out, 0x38, s->planeCount);
	memcpy(out + 0x40, &s->bytesPerElement, 2);
	out[0x42] = s->elementWidth;
	out[0x43] = s->elementHeight;
	put32(out, 0x44, s->cacheMode);
	put32(out, 0x50, s->parentID);
	put32(out, 0x54, 1);
	for (uint32_t i = 0; i < s->planeCount; i++) {
		uint8_t *p = out + 0x68 + i * 0x80;
		const struct sweet_surface_plane *pl = &s->planes[i];
		put32(p, 0x00, pl->width);
		put32(p, 0x04, pl->height);
		put32(p, 0x08, pl->bytesPerRow);
		put32(p, 0x0c, pl->offset);
		put32(p, 0x14, pl->size);
		memcpy(p + 0x18, &pl->bytesPerElement, 2);
		p[0x1a] = pl->elementWidth;
		p[0x1b] = pl->elementHeight;
	}
}

static IOReturn
sweet_struct_out(IOExternalMethodArguments *args, const void *buf, uint32_t len)
{
	if (args->structureOutputDescriptor != NULL) {
		if (len > args->structureOutputDescriptor->getLength()) {
			return kIOReturnNoSpace;
		}
		args->structureOutputDescriptor->writeBytes(0, buf, len);
		args->structureOutputDescriptorSize = len;
		return kIOReturnSuccess;
	}
	if (args->structureOutput == NULL || len > args->structureOutputSize) {
		return kIOReturnNoSpace;
	}
	memcpy(args->structureOutput, buf, len);
	args->structureOutputSize = len;
	return kIOReturnSuccess;
}

/* Map s for this client and answer with its lock result. Takes s's user count. */
IOReturn
IOSurfaceRootUserClient::returnSurface(SweetSurface *s, IOExternalMethodArguments *args)
{
	SweetSurfaceRef *ref = NULL;
	bool existing = false;
	IOReturn ret = addRef(s, &ref, &existing);
	if (ret != kIOReturnSuccess) {
		return ret;
	}
	uint8_t *out = (uint8_t *)IOMallocZeroData(SWEET_SURFACE_RESULT_SIZE);
	if (out == NULL) {
		ret = kIOReturnNoMemory;
	} else {
		sweet_surface_fill_result(s, ref, out);
		if (existing) {
			put64(out, 0x00, ~0ULL);        /* "already yours": use the cached client */
		}
		ret = sweet_struct_out(args, out, SWEET_SURFACE_RESULT_SIZE);
		IOFreeData(out, SWEET_SURFACE_RESULT_SIZE);
	}
	if (ret != kIOReturnSuccess && !existing) {
		/* The caller never learns the id: undo this create/lookup. */
		trapRelease((void *)(uintptr_t)s->id, NULL, NULL, NULL, NULL, NULL);
	}
	return ret;
}

IOReturn
IOSurfaceRootUserClient::create(IOExternalMethodArguments *args)
{
	const char *buf = (const char *)args->structureInput;
	uint32_t len = args->structureInputSize;
	char *copy = NULL;
	IOReturn ret;

	if (args->structureInputDescriptor != NULL) {
		len = (uint32_t)args->structureInputDescriptor->getLength();
		if (len == 0 || len > SWEET_SURFACE_MAX_PROPS) {
			return kIOReturnBadArgument;
		}
		copy = (char *)IOMallocData(len);
		if (copy == NULL) {
			return kIOReturnNoMemory;
		}
		args->structureInputDescriptor->readBytes(0, copy, len);
		buf = copy;
	}
	OSObject *obj = (buf != NULL && len > 0) ? OSUnserializeXML(buf, len) : NULL;
	if (copy != NULL) {
		IOFreeData(copy, len);
	}
	OSDictionary *props = OSDynamicCast(OSDictionary, obj);
	if (props == NULL) {
		OSSafeReleaseNULL(obj);
		IOLog("SWEET SURF: create: bad property dictionary (%u bytes)\n", len);
		return kIOReturnBadArgument;
	}

	SweetSurface *s = sweet_surface_alloc();
	if (s == NULL) {
		props->release();
		return kIOReturnNoMemory;
	}
	s->parentID = args->scalarInputCount > 0 ? (uint32_t)args->scalarInput[0] : 0;
	ret = sweet_surface_layout(s, props);
	if (ret == kIOReturnSuccess) {
		IOBufferMemoryDescriptor *mem = NULL;
		if ((uint64_t)OSAddAtomic64(s->allocSize, (volatile SInt64 *)&sweet_surface_wired) +
		    s->allocSize <= SWEET_SURFACE_WIRED_MAX) {
			mem = IOBufferMemoryDescriptor::inTaskWithOptions(kernel_task,
			    kIODirectionInOut | kIOMemoryKernelUserShared, s->allocSize, page_size);
		}
		if (mem == NULL) {
			OSAddAtomic64(-(SInt64)s->allocSize, (volatile SInt64 *)&sweet_surface_wired);
			ret = kIOReturnNoMemory;
		} else {
			bzero(mem->getBytesNoCopy(), s->allocSize);
			s->memory = mem;
			s->mapCache = kIOMapDefaultCache;
			if (!sweet_surface_publish(s)) {
				ret = kIOReturnNoResources;
			}
		}
	}
	if (ret != kIOReturnSuccess || sweet_surface_creates_logged < 64) {
		char name[32];
		proc_selfname(name, sizeof(name));
		sweet_surface_creates_logged++;
		IOLog("SWEET SURF: %s create id %u %ux%u fmt 0x%x bpe %u bpr %u planes %u size %u keys %u -> 0x%x\n",
		    name, s->id, s->width, s->height, s->pixelFormat, s->bytesPerElement,
		    s->bytesPerRow, s->planeCount, s->allocSize, props->getCount(), ret);
	}
	/* Which client code asks for multi-plane non-BGRA layouts (App Library 'b3a8'): first 8, with a user backtrace. */
	if (ret == kIOReturnSuccess && s->planeCount > 1 && s->pixelFormat != 0x42475241u &&
	    OSIncrementAtomic(&sweet_surface_bts_logged) < 8) {
		char name[32], tag[96];
		proc_selfname(name, sizeof(name));
		snprintf(tag, sizeof(tag), "SWEET SURF: bt %s id %u fmt 0x%x", name, s->id, s->pixelFormat);
		sweet_prof_user_bt(tag);
	}
	props->release();
	if (ret != kIOReturnSuccess) {
		s->release();
		return ret;
	}
	ret = returnSurface(s, args);   /* takes the creator's user count */
	s->release();
	return ret;
}

IOReturn
IOSurfaceRootUserClient::externalMethod(uint32_t selector, IOExternalMethodArguments *args,
    __unused IOExternalMethodDispatch *dispatch, __unused OSObject *target,
    __unused void *reference)
{
	uint64_t arg0 = args->scalarInputCount > 0 ? args->scalarInput[0] : 0;

	switch (selector) {
	case kSweetSurfaceLimits: {
		uint8_t limits[0x18] = { 0 };
		put32(limits, 0x0, SWEET_SURFACE_ROW_ALIGN - 1);
		put32(limits, 0x4, SWEET_SURFACE_ROW_ALIGN - 1);
		limits[0x16] = 1;
		sweet_surf_log_once("sel", selector);
		return sweet_struct_out(args, limits, sizeof(limits));
	}
	case kSweetSurfaceCreate:
		return create(args);
	case kSweetSurfaceLookup: {
		SweetSurface *s = sweet_surface_copy((uint32_t)arg0);
		IOReturn ret = kIOReturnNotFound;
		if (s != NULL) {
			ret = sweet_surface_get(s) ? returnSurface(s, args) : kIOReturnNotFound;
			s->release();
		}
		sweet_surf_log_once("sel", selector);
		return ret;
	}
	case kSweetSurfaceLookupFromPort: {
		OSObject *obj = NULL;
		IOReturn ret = IOUserClient::copyObjectForPortNameInTask(fTask, (mach_port_name_t)arg0, &obj);
		SweetSurface *s = OSDynamicCast(SweetSurface, obj);
		if (ret == kIOReturnSuccess) {
			ret = (s != NULL && sweet_surface_get(s)) ? returnSurface(s, args) : kIOReturnBadArgument;
		}
		OSSafeReleaseNULL(obj);
		sweet_surf_log_once("sel", selector);
		return ret;
	}
	case kSweetSurfaceCreatePort: {
		SweetSurface *s = sweet_surface_copy((uint32_t)arg0);
		if (s == NULL || args->scalarOutputCount < 1) {
			OSSafeReleaseNULL(s);
			return kIOReturnNotFound;
		}
		args->scalarOutput[0] = sweet_surface_port_in_task(s, fTask);
		args->scalarOutputCount = 1;
		s->release();
		sweet_surf_log_once("sel", selector);
		return kIOReturnSuccess;
	}
	default:
		/* Values, notifications, ownership, timestamps...: accept and ignore. */
		for (uint32_t i = 0; i < args->scalarOutputCount; i++) {
			args->scalarOutput[i] = 0;
		}
		if (args->structureOutput != NULL) {
			bzero(args->structureOutput, args->structureOutputSize);
		}
		sweet_surf_log_once("sel", selector);
		return kIOReturnSuccess;
	}
}

IOReturn IOSurfaceRootUserClient::useCount(uint32_t id, uint32_t category, bool increment)
{
 if (category >= 2) return kIOReturnBadArgument;
 IOLockLock(fLock);
 SweetSurfaceRef *ref = findRef(id, NULL);
 IOReturn ret = kIOReturnNotFound;
 if (ref) {
  IOLockLock(sweet_surface_lock);
  uint32_t &local = ref->uses[category], &total = ref->surface->useCounts[category];
  ret = kIOReturnBadArgument;
  if (increment ? total < SWEET_SURFACE_USE_MAX : local != 0) {
   if (increment) { local++; total++; } else { local--; total--; }
   sweet_surface_use_publish(ref->surface); ret = kIOReturnSuccess;
  }
  IOLockUnlock(sweet_surface_lock);
 }
 IOLockUnlock(fLock);
 return ret;
}
IOReturn IOSurfaceRootUserClient::trapIncrementUse(void *id, void *category, void *, void *, void *, void *)
{
 if ((uintptr_t)id > UINT32_MAX || (uintptr_t)category >= 2) return kIOReturnBadArgument;
 return useCount((uint32_t)(uintptr_t)id, (uint32_t)(uintptr_t)category, true);
}
IOReturn IOSurfaceRootUserClient::trapDecrementUse(void *id, void *category, void *, void *, void *, void *)
{
 if ((uintptr_t)id > UINT32_MAX || (uintptr_t)category >= 2) return kIOReturnBadArgument;
 return useCount((uint32_t)(uintptr_t)id, (uint32_t)(uintptr_t)category, false);
}

static IOReturn
sweet_surface_seed_out(SweetSurface *s, void *seedOut, bool bump)
{
	uint32_t seed = bump ? OSIncrementAtomic((volatile SInt32 *)sweet_surface_seed(s)) + 1 :
	    *sweet_surface_seed(s);
	if (seedOut != NULL && copyout(&seed, (user_addr_t)seedOut, sizeof(seed)) != 0) {
		return kIOReturnBadArgument;
	}
	return kIOReturnSuccess;
}

/* Retained surface this client created or looked up, or NULL. */
static SweetSurface *
sweet_surface_held(IOLock *lock, OSArray *refs, uint32_t id)
{
	SweetSurface *s = NULL;
	IOLockLock(lock);
	for (unsigned int i = 0; i < refs->getCount(); i++) {
		SweetSurfaceRef *ref = (SweetSurfaceRef *)refs->getObject(i);
		if (ref->surface->id == id) {
			s = ref->surface;
			s->retain();
			break;
		}
	}
	IOLockUnlock(lock);
	return s;
}

IOReturn
IOSurfaceRootUserClient::trapLock(void *id, void *options, void *seedOut,
    void *, void *, void *)
{
	SweetSurface *s = sweet_surface_held(fLock, fRefs, (uint32_t)(uintptr_t)id);
	if (s == NULL) {
		return kIOReturnNotFound;
	}
#if defined(FUXI)
 if (!((uintptr_t)options & kIOSurfaceLockReadOnly) && !sweet_gpu_writer(s, true, ((uintptr_t)options & kIOSurfaceLockAvoidSync) != 0)) {
  s->release(); return kIOReturnNotReady;
 }
#endif
	IOReturn ret = sweet_surface_seed_out(s, seedOut, false);
	s->release();
	return ret;
}

IOReturn
IOSurfaceRootUserClient::trapUnlock(void *id, void *options, void *seedOut,
    void *, void *, void *)
{
	SweetSurface *s = sweet_surface_held(fLock, fRefs, (uint32_t)(uintptr_t)id);
	if (s == NULL) {
		return kIOReturnNotFound;
	}
#if defined(FUXI)
 if (!((uintptr_t)options & kIOSurfaceLockReadOnly)) (void)sweet_gpu_writer(s, false);
#endif
	IOReturn ret = sweet_surface_seed_out(s, seedOut,
	    !((uintptr_t)options & kIOSurfaceLockReadOnly));
	s->release();
	return ret;
}

IOReturn
IOSurfaceRootUserClient::trapRelease(void *id, void *, void *, void *, void *, void *)
{
	unsigned int index;
	IOLockLock(fLock);
	SweetSurfaceRef *ref = findRef((uint32_t)(uintptr_t)id, &index);
	if (ref != NULL && --ref->count == 0) {
		fRefs->removeObject(index);     /* unmaps and drops the user count */
	}
	IOLockUnlock(fLock);
	return ref != NULL ? kIOReturnSuccess : kIOReturnNotFound;
}

static IOExternalTrap sweet_surface_traps[] = {
	[kSweetSurfaceTrapIncrementUse]  = { NULL, (IOTrap) & IOSurfaceRootUserClient::trapIncrementUse },
	[kSweetSurfaceTrapDecrementUse]  = { NULL, (IOTrap) & IOSurfaceRootUserClient::trapDecrementUse },
	[kSweetSurfaceTrapLock]          = { NULL, (IOTrap) & IOSurfaceRootUserClient::trapLock },
	[kSweetSurfaceTrapUnlock]        = { NULL, (IOTrap) & IOSurfaceRootUserClient::trapUnlock },
	[kSweetSurfaceTrapRelease]       = { NULL, (IOTrap) & IOSurfaceRootUserClient::trapRelease },
	[kSweetSurfaceTrapReleaseFailed] = { NULL, (IOTrap) & IOSurfaceRootUserClient::trapRelease },
};

IOExternalTrap *
IOSurfaceRootUserClient::getTargetAndTrapForIndex(IOService **targetP, UInt32 index)
{
	if (index >= sizeof(sweet_surface_traps) / sizeof(sweet_surface_traps[0])) {
		sweet_surf_log_once("trap", index);
		return NULL;
	}
	sweet_surf_log_once("trap", index);
	*targetP = this;
	return &sweet_surface_traps[index];
}
#endif /* SWEET */
