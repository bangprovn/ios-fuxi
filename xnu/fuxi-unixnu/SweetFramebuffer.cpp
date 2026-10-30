/*
 * Built-in IOMobileFramebuffer for SWEET: the ABL's continuous-splash
 * framebuffer (1080x2400 a8r8g8b8 in RAM, the panel scans it out by
 * itself) as the main display, driven by CoreAnimation's CPU renderer.
 *
 * IOMobileFramebuffer.framework (iOS 16.7) counts /arm-io children with
 * device_type "display-subsystem", waits for that many services matching
 * IOServiceMatching("IOMobileFramebuffer"), opens each with type 0 and
 * reads canvas-width/canvas-height from the service's provider. The class
 * here (AppleCLCD, an IOMobileFramebuffer) matches the /arm-io/disp0 nub.
 *
 * User client selectors used by CoreAnimation:
 *   3  GetLayerDefaultSurface [layer, ?] -> [IOSurface port name]
 *   4  SwapBegin -> [swap id]
 *   5  SwapEnd, struct 0x46c: +0x50 swap id, +0x54 IOSurface id per layer
 *      (4 layers), +0x64 rect a / +0xc4 rect b per layer (int x,y,w,h),
 *      +0x104 enabled layer mask
 *   6  SwapWait, 7 GetID, 8 GetDisplaySize -> [w, h],
 *   12 RequestPowerChange [state]: 0 = display off (backboardd, power
 *      button / auto-lock), else on. Off fades the panel out over a few
 *      vsyncs (halving each pixel) and keeps it black until on again;
 *      the panel itself stays powered (AMOLED black pixels are off).
 *   18 IsMainDisplay,
 *   28 GetDotPitch, 72 Enable/DisableNotifications [func, refcon, type, 0]
 * Everything else is accepted and logged once.
 *
 * SwapEnd copies the enabled layers' surfaces into the panel memory (no
 * scaling, CPU copy). Vsync (notification type 5) is a 60 Hz timer that
 * sends (count, mach_absolute_time, 0) as 32-bit halves to clients that
 * enabled it, through the port they registered (IOConnectSetNotificationPort).
 *
 * The panel memory itself is published as an IOSurface and returned as the
 * default framebuffer surface. The kernel text console keeps drawing over
 * the GUI by default: boot-arg sweetfb_noconsole=1 stops drawing it from
 * the first swap on (it still reaches the persistent log; panics draw).
 *
 * FUXI (0093): frames are shown at up to 60 Hz by default (7 fast cores;
 * sweetfb_hz still overrides), the tick runs on absolute deadlines so the
 * copy time does not stretch the period, every 600 presented frames log
 * "SWEET FB: stats" (copy time, swaps, dropped swaps), and display off is a
 * single black fill (write-only: the panel memory is write-combined, the
 * read-modify-write fade read it back uncached). sweet_fb_display_off tells
 * the touch driver (bsd/dev/fuxi_touch.c) that the display is off.
 */
// Ported from UniXNU drivers/NativeMobileDisplay/NativeLegacyDisplay.cpp (BSD-2-Clause-Patent)
// Optional demand-driven timer policy; see LICENSE.NativeMobileDisplay.
#include "SweetSurface.h"
#include "fuxi_unixnu_copy.h"
#include "fuxi_unixnu_display.h"
#if defined(FUXI)
extern "C" {
#include "../../../bsd/dev/fuxi_adreno.h"
void fuxi_perf_start(void);
void fuxi_perf_swap(void);
}
#endif

extern "C" int sweet_console_muted;     /* osfmk/console/serial_console.c */
extern "C" { int sweet_fb_display_off; }        /* read by bsd/dev/fuxi_touch.c */

#if defined(SWEET)
#include <IOKit/IOService.h>
#include <IOKit/IOUserClient.h>
#include <IOKit/IOKitKeys.h>
#include <IOKit/IOLib.h>
#include <IOKit/IOLocks.h>
#include <IOKit/IOWorkLoop.h>
#include <IOKit/IOTimerEventSource.h>
#include <IOKit/system.h>
#include <pexpert/pexpert.h>
#include <kern/clock.h>
#include <kern/thread_call.h>
#include <sys/proc.h>

#if defined(FUXI)
extern bool sweet_surface_present_begin(SweetSurface **surfaces, unsigned count, uint64_t *ready, const uint64_t *epochs);
extern void sweet_surface_present_end(void);
extern void sweet_surface_present_notify(void (*wake)(void *), void *context);
extern void fuxi_backlight_publish(IOService *provider);
extern void fuxi_backlight_display_state(IOService *provider, bool on);
#endif

enum {
	kSweetFBGetLayerDefaultSurface  = 3,
	kSweetFBSwapBegin               = 4,
	kSweetFBSwapEnd                 = 5,
	kSweetFBSwapWait                = 6,
	kSweetFBGetID                   = 7,
	kSweetFBGetDisplaySize          = 8,
	kSweetFBRequestPowerChange      = 12,
	kSweetFBIsMainDisplay           = 18,
	kSweetFBGetDotPitch             = 28,
	kSweetFBNotifications           = 72,
};

#define SWEET_FB_SWAP_REC_SIZE          0x46c
#define SWEET_FB_LAYERS                 4
#define SWEET_FB_QUEUE_DEPTH            3
#define SWEET_FB_SWAP_HISTORY           64
#define SWEET_FB_FLOG                   128     /* fuxigpuncimport bit 8: frames in the one-shot present event log */
#define SWEET_FB_FLOG_WARMUP            3000    /* swaps before it starts */
#define SWEET_FB_NOTIFY_TYPES           8
#define SWEET_FB_NOTIFY_VSYNC           5
#define SWEET_FB_VSYNC_US               16667
#define SWEET_FB_DOT_PITCH              458     /* ppi, as the iPhone X panel */
/*
 * The iOS userland only has status bar / layout data for real iPhone
 * screens (UIKit: "No status bar visual provider for screen", the red
 * rdar://45025538 bar for 360x800 points). The display is therefore
 * presented as the iPhone X panel, 1125x2436 (375x812 points @3x), and the
 * centered 1080x2400 part of it is shown on the 1080x2400 panel.
 * Boot-arg sweetfb_native=1 presents the real panel size instead.
 */
#define SWEET_FB_LOGICAL_W              1125
#define SWEET_FB_LOGICAL_H              2436
#define SWEET_FB_FORMAT_BGRA            0x42475241

class IOMobileFramebuffer : public IOService {
	OSDeclareDefaultStructors(IOMobileFramebuffer);
};

OSDefineMetaClassAndStructors(IOMobileFramebuffer, IOService);

class IOMobileFramebufferUserClient;

class AppleCLCD : public IOMobileFramebuffer {
	OSDeclareDefaultStructors(AppleCLCD);

	IOWorkLoop         *fWorkLoop;
	IOTimerEventSource *fVsync;
	uint64_t            fVsyncCount;

	static void vsyncFired(OSObject *owner, IOTimerEventSource *sender);

public:
	uint64_t      fPhys;
	uint32_t      fWidth, fHeight, fRowBytes;
	uint32_t      fLogicalW, fLogicalH;     /* size reported to userland */
	int32_t       fOffX, fOffY;             /* logical origin of the panel */
	uintptr_t     fPixels;          /* kernel mapping of the panel memory */
	IOMemoryMap  *fMap;
	SweetSurface *fSurface;         /* the panel memory as an IOSurface */
	IOLock       *fLock;            /* swaps and client list */
	OSArray      *fClients;
	uint32_t      fSwapID;
	bool          fConsolePaused;
 struct SwapRecord { uint8_t bytes[SWEET_FB_SWAP_REC_SIZE]; SweetSurface *surfaces[SWEET_FB_LAYERS]; uint64_t swapped, ready, writers[SWEET_FB_LAYERS], epochs[SWEET_FB_LAYERS]; bool shown, consumed, reused, stale; unsigned uses; };
 SwapRecord    fPending[SWEET_FB_QUEUE_DEPTH], fLast;
 SwapRecord   *fPresentRecord;
 uint64_t      fReuseBeforeShow;
 struct SwapStatus { uint32_t id; unsigned state; } fSwapStatus[SWEET_FB_SWAP_HISTORY];
 uint32_t      fLatestSwap;
 enum { SwapQueued = 1, SwapConsumed, SwapShown, SwapDropped };
 uint64_t      fWaitPoll[2], fWaitEarly, fWaitLatest;
 unsigned      fOutstandingMax, fStageBlocks[3];
 unsigned      fPendingCount;
 bool          fLastValid, fReplay;
 uint32_t      fQueuedDefers, fOverflowDrops, fReadySkips;
	bool          fDirty;
	uint32_t      fFlushTicks;    /* vsync ticks between panel copies */
	bool          fPowerOff;      /* RequestPowerChange(0) */
#if defined(FUXI)
	bool          fBacklightPowerNotifications;
#endif
	uint32_t      fFadeStep;      /* fade-out steps done while off */
	uint64_t      fTickAbs;       /* vsync period, absolute time units */
	uint64_t      fNextTick;      /* next vsync deadline */
	uint64_t      fStatCopy, fStatCopyMax, fStatStart;
	uint32_t      fStatFrames, fStatSwaps, fStatDropped;
	uint32_t      fGpuFrames, fCpuFrames;
	uint64_t      fGpuTime, fCpuTime;
	bool          fUnixnuDiff, fUnixnuIdle, fTickArmed;
	bool          fShadowValid;
	void         *fShadow;
	uint64_t      fShadowBytes, fDiffStored, fDiffFull;
	uint32_t      fDiffFrames, fIdleStops, fIdleWakes;
	bool          fScanout, fScanoutHeld;
	uint8_t       fScanoutLog;  /* 1 summary, 2 also per frame (fuxigpuscanoutlog=1) */
 uint32_t      fScanoutFrames, fScanoutGpu, fScanoutCpu, fScanoutDefers;
 uint64_t      fScanoutTime, fScanoutMax, fScanoutBytes, fScanoutPrepare;
#if defined(FUXI)
 bool          fVsyncTE, fTeValid, fTeSent;
 struct fg_te_observer fTe;
 uint64_t      fTeOffset, fTeNext, fTeLastNotify, fTePeriodSum;
 unsigned      fTeSentFrame, fTeLocked, fTeFallback;
 uint16_t      fTePhase[256];  /* 128 us bins, locked notifications only */
 bool          fSwapHist;      /* disp0 fuxi,swap-hist=1: SwapEnd intervals in vsync periods */
 uint64_t      fSwapLast, fSwapIdle;
 uint32_t      fSwapBins[7];   /* 0..4, 5+ (<100 ms), idle */
 /* fuxigpuncimport bit 8: one present event log of SWEET_FB_FLOG consecutive swaps after the warmup (state 0 off, 1 warmup, 2 recording,
  * 3 printed); bit 16: the surfaces of swaps whose copy is not queued yet go to the kernel copy priority; bit 32: a ready swap with a
  * newer ready swap behind it is dropped before its copy (the newer one is copied instead). */
 struct FlogEntry { uint64_t swap, ready, submit, fence, start; uint32_t id, surface, s2s, blit, ahead, buf, te, xfer, pk; char fate; };
 FlogEntry    *fFlog; /* SWEET_FB_FLOG entries, allocated only with bit 8 (10 KB; the object is limited to 16 KB by kalloc_type) */
 unsigned      fFlogState, fFlogN, fFlogSwaps;
 uint32_t      fFlogGpuID, fSupersededSkips;
 bool          fCopyFirst, fSkipSuperseded;
 FlogEntry *flogFind(uint32_t id) { if (fFlogState != 2 || !id) return NULL; for (unsigned i = fFlogN; i--;) if (fFlog[i].id == id) return &fFlog[i]; return NULL; }
 void flogFate(uint32_t id, char fate) { FlogEntry *e = flogFind(id); if (e && !e->fate) e->fate = fate; }
 static uint32_t firstSurface(const SwapRecord &r) { for (unsigned i = 0; i < SWEET_FB_LAYERS; i++) if (r.surfaces[i]) return r.surfaces[i]->id; return 0; }
 void flogSwap(const SwapRecord &r, uint32_t id) {
  if (fFlogState == 1 && ++fFlogSwaps >= SWEET_FB_FLOG_WARMUP) fFlogState = 2;
  if (fFlogState != 2 || fFlogN >= SWEET_FB_FLOG) return;
  FlogEntry &e = fFlog[fFlogN++]; bzero(&e, sizeof(e)); e.id = id; e.swap = r.swapped; e.surface = firstSurface(r);
 }
 /* Ready to read without the reader slot (racy probe; present_begin re-checks under the writer lock). */
 static bool recordReady(const SwapRecord &r) {
  if (r.stale) return false;
  for (unsigned i = 0; i < SWEET_FB_LAYERS; i++) if (r.surfaces[i] && (r.surfaces[i]->gpuWriters || r.surfaces[i]->gpuQuarantined || r.surfaces[i]->gpuWriterEpoch != r.epochs[i])) return false;
  return true;
 }
 void publishDueLocked(void) {
  if (!fCopyFirst) return;
  uint32_t ids[SWEET_FB_QUEUE_DEPTH+1], id; unsigned n = 0;
  if (fPresentRecord && !fGpuPending && (id = firstSurface(*fPresentRecord))) ids[n++] = id;
  for (unsigned i = 0; i < fPendingCount && n < SWEET_FB_QUEUE_DEPTH+1; i++) if ((id = firstSurface(fPending[i]))) ids[n++] = id;
  fuxi_gpu_present_due(ids, n);
 }
 static long long flogUs(uint64_t t, uint64_t base) { uint64_t ns; if (!t || t < base) return -1; absolutetime_to_nanoseconds(t-base, &ns); return (long long)(ns/1000); }
 /* Once, after the last entry has a fate (or 2 s after its swap): one line per frame, then a summary. */
 void flogPrint(uint64_t now) {
  if (fFlogState != 2 || fFlogN < SWEET_FB_FLOG) return;
  bool open = false; for (unsigned i = 0; i < fFlogN; i++) if (!fFlog[i].fate) open = true;
  if (open && flogUs(now, fFlog[fFlogN-1].swap) < 2000000) return;
  fFlogState = 3;
  uint64_t period = fTeValid ? fTe.period : fTickAbs, base = fFlog[0].swap; long long pus = flogUs(base+period, base);
  IOLog("SWEET FB: flog begin frames=%u warmup_swaps=%u period_us=%lld fields: n id surface swap_us(from frame 0) then us after this swap: ready copy_submit fence start, "
      "gpu submit_to_copy_start blit, client_jobs_ahead buffer te_count prev_xfer_done_us fate(S shown, D dropped, P replaced while waiting START, - open), pk=1 a parked client job followed the copy (bit 27); -1 none\n",
      fFlogN, SWEET_FB_FLOG_WARMUP, pus);
  unsigned shown = 0, dropped = 0, replaced = 0, copied = 0, copiedDropped = 0, behind = 0, pairs = 0, longS2s = 0, parked = 0, gaps[3] = {0, 0, 0}, lastTe = 0; bool haveTe = false;
  for (unsigned i = 0; i < fFlogN; i++) {
   FlogEntry &e = fFlog[i]; char fate = e.fate ? e.fate : '-';
   IOLog("SWEET FB: flog %u %u %u %lld %lld %lld %lld %lld %d %d %u %u %u %u %c pk=%u\n", i, e.id, e.surface, flogUs(e.swap, base), flogUs(e.ready, e.swap), flogUs(e.submit, e.swap),
       flogUs(e.fence, e.swap), flogUs(e.start, e.swap), e.submit ? (int)e.s2s : -1, e.submit ? (int)e.blit : -1, e.ahead, e.buf, e.te, e.xfer, fate, e.pk);
   shown += fate == 'S'; dropped += fate == 'D'; replaced += fate == 'P';
   if (e.submit) { copied++; copiedDropped += fate != 'S'; behind += e.ahead != 0; longS2s += e.s2s != UINT32_MAX && e.s2s > 3000; parked += e.pk != 0; }
   if (i && e.fence && fFlog[i-1].fence && e.fence > fFlog[i-1].fence && e.fence-fFlog[i-1].fence < period/2) pairs++; /* two copies done within half a period */
   if (fate == 'S') { if (haveTe) { unsigned d = (e.te-lastTe) & 0xffffu; if (d) gaps[d >= 3 ? 2 : d-1]++; } lastTe = e.te; haveTe = true; }
  }
  IOLog("SWEET FB: flog end shown=%u dropped=%u replaced_waiting_start=%u copied=%u copied_not_shown=%u copies_behind_client_job=%u submit_to_start_over_3ms=%u"
      " fence_pairs_within_half_period=%u start_te_gaps 1/2/3+=%u/%u/%u superseded_skips=%u copies_releasing_parked_job=%u\n",
      shown, dropped, replaced, copied, copiedDropped, behind, longS2s, pairs, gaps[0], gaps[1], gaps[2], fSupersededSkips, parked);
 }
 bool teVsyncTick(uint64_t &now, bool fallback);
 void teVsyncLog(uint64_t now);
#else
 static const bool fSkipSuperseded = false;
 uint32_t      fSupersededSkips;
 static bool recordReady(const SwapRecord &) { return false; }
 void flogFate(uint32_t, char) {}
 void flogSwap(const SwapRecord &, uint32_t) {}
 void publishDueLocked(void) {}
#endif
 void scanoutLog(bool gpu, uint64_t bytes, uint64_t copy, uint64_t prepare);
 bool          fGpuPending, fPublicationPending;
 uint64_t      fPublicationTime, fPublicationStage[6];
 uint32_t      fPublicationID;
 uint64_t      fStage[6], fStageSum[6], fStageMax[6], fLastStart;
 unsigned      fStageN[6], fStageCallbacks[3];
 uint64_t      fWriterWakeQueued, fWakeSum[2], fWakeMax[2];
 unsigned      fWakeN[2];
 bool          fStageGpu;
 bool presentStep(bool flush);
 void stageStats(uint64_t now);
	uint32_t      fGpuBusyTicks, fGpuFailed;

	bool start(IOService *provider) APPLE_KEXT_OVERRIDE;
	IOReturn requestPowerChange(uint64_t state);
	static IOReturn powerChangeAction(OSObject *owner, void *state,
	    void *, void *, void *);
	void addClient(IOMobileFramebufferUserClient *client);
	void removeClient(IOMobileFramebufferUserClient *client);
	void wakePresentLocked(void);
	bool presentWantedLocked(void);
	void free(void) APPLE_KEXT_OVERRIDE;
	IOReturn swap(const uint8_t *rec);
 IOReturn swapWait(uint32_t id, uint32_t flags, uint64_t timeout);
 void recordDone(SwapRecord &record, bool shown);
 void consumeRecord(SwapRecord &record);
 void retireEarly(const SwapRecord &record);
 bool          fEarlyRetire;
 uint32_t      fEarlyRetires;
 void stopRecords(void);
	bool present(const SwapRecord &record);
 void presentNext(void);
 void observeRecord(SwapRecord &record, bool shown);
 void releaseRecord(SwapRecord &record);
 static void writerRetired(void *context);
#if defined(FUXI)
 thread_call_t fWriterWakeCall;
 static void writerWake(thread_call_param_t context, thread_call_param_t unused);
 static IOReturn writerWakeAction(OSObject *owner, void *, void *, void *, void *);
#endif
 void presentStats(uint64_t took);
 void completePresent(uint64_t took);
};

OSDefineMetaClassAndStructors(AppleCLCD, IOMobileFramebuffer);

class IOMobileFramebufferUserClient : public IOUserClient {
	OSDeclareDefaultStructors(IOMobileFramebufferUserClient);

public:
	AppleCLCD          *fFB;
	task_t              fTask;
	mach_port_t         fPort;
	io_user_reference_t fFunc[SWEET_FB_NOTIFY_TYPES];
	io_user_reference_t fRefcon[SWEET_FB_NOTIFY_TYPES];

	bool initWithTask(task_t owningTask, void *securityID, UInt32 type) APPLE_KEXT_OVERRIDE;
	bool start(IOService *provider) APPLE_KEXT_OVERRIDE;
	IOReturn clientClose(void) APPLE_KEXT_OVERRIDE;
	IOReturn registerNotificationPort(mach_port_t port, UInt32 type,
	    io_user_reference_t refCon) APPLE_KEXT_OVERRIDE;
	IOReturn externalMethod(uint32_t selector, IOExternalMethodArguments *args,
	    IOExternalMethodDispatch *dispatch, OSObject *target,
	    void *reference) APPLE_KEXT_OVERRIDE;
	void notify(uint32_t type, const uint64_t *values, uint32_t count);
};

OSDefineMetaClassAndStructors(IOMobileFramebufferUserClient, IOUserClient);
#if defined(FUXI)
extern "C" void fuxi_display_kick(void);
extern "C" void fuxi_gpu_abi_stats_log(void);
extern "C" void fuxi_gpu_abi_sched_log(void);
extern "C" int fuxi_gpu_abi_pace(void);
extern "C" int fuxi_gpu_abi_start(void);
#endif

/* NativeMetal c6d29ca (MIT): prioritize the path carrying frames to screen.
 * Here IOTimerEventSource executes vsyncFired/present on its thread-call,
 * under the work-loop gate; raising only the IOWorkLoop thread misses it.
 * See bsd/dev/LICENSE.NativeMetal. Keep scheduling changes opt-in.
 * Default is KERNEL_HIGH, not user default.
 */
static uint32_t sweet_fb_timer_options(void)
{
	uint32_t high = 0;
#if defined(FUXI)
	PE_parse_boot_argn("fuxifbprio", &high, sizeof(high));
#endif
	return high == 1 ? kIOTimerEventSourceOptionsPriorityHigh : kIOTimerEventSourceOptionsDefault;
}

#if defined(FUXI)
/* IOMFBDisplayRefresh.displayMinRefreshInterval: seconds in 0.32 fixed point, a 32-bit OSNumber
 * (CA reads it with CFNumberGetValue as int32; NativeMobileDisplay getRefreshAttributes uses 2^32/hz).
 * 120 Hz -> 35791394. displayRefreshStepMachTime is deliberately not published (VRR-only). */
static uint32_t sweet_fb_refresh_interval(uint32_t hz)
{
 return hz == 0 ? 0 : (uint32_t)(((1ULL << 32) + hz / 2) / hz);
}

/* Opt-in: fuxipanel120=1 only; an explicit sweetfb_hz<=60 keeps CA's 1/60 s heartbeat. */
static bool sweet_fb_refresh_wanted(uint32_t panel120, bool hzPresent, uint32_t hz)
{
 return panel120 == 1 && !(hzPresent && hz <= 60);
}

/* Opt-in disp0 ADT properties (the boot-arg line is nearly full): absent or not a u32 -> 0. */
static uint32_t sweet_fb_adt_u32(IOService *provider, const char *key)
{
 OSData *data = provider ? OSDynamicCast(OSData, provider->getProperty(key)) : NULL;
 uint32_t value = 0;
 if (data && data->getLength() == sizeof(value)) memcpy(&value, data->getBytesNoCopy(), sizeof(value));
 return value;
}

/* disp0 fuxi,refresh-mhz: the panel's measured rate in mHz (120800 for TE 8.278 ms), so CA's
 * free-running display timer does not beat against TE. Outside 61..240 Hz: nominal 120 Hz. */
static uint32_t sweet_fb_refresh_interval_mhz(uint32_t mhz)
{
 if (mhz < 61000 || mhz > 240000) mhz = 120000;
 return (uint32_t)(((1ULL << 32) * 1000 + mhz / 2) / mhz);
}

/* A SwapEnd interval in vsync periods: 0..4, 5 = 5+ below idle, 6 = idle (at least `idle`). */
static uint32_t sweet_fb_swap_bin(uint64_t interval, uint64_t period, uint64_t idle)
{
 if (interval >= idle) return 6;
 uint64_t n = period ? (interval + period / 2) / period : 5;
 return n > 5 ? 5 : (uint32_t)n;
}

static void sweet_fb_refresh_publish(AppleCLCD *fb)
{
 uint32_t panel120 = 0, hz = 0;
 PE_parse_boot_argn("fuxipanel120", &panel120, sizeof(panel120));
 bool hzPresent = PE_parse_boot_argn("sweetfb_hz", &hz, sizeof(hz));
 if (!sweet_fb_refresh_wanted(panel120, hzPresent, hz)) return;
 uint32_t mhz = sweet_fb_adt_u32(fb->getProvider(), "fuxi,refresh-mhz");
 uint32_t value = mhz ? sweet_fb_refresh_interval_mhz(mhz) : sweet_fb_refresh_interval(120);
 OSDictionary *refresh = OSDictionary::withCapacity(1);
 OSNumber *interval = OSNumber::withNumber((uint64_t)value, 32);
 if (refresh && interval && refresh->setObject("displayMinRefreshInterval", interval) &&
     fb->setProperty("IOMFBDisplayRefresh", refresh)) {
  if (mhz) IOLog("SWEET FB: IOMFBDisplayRefresh displayMinRefreshInterval=%u (fuxi,refresh-mhz=%u)\n", value, mhz);
  else IOLog("SWEET FB: IOMFBDisplayRefresh displayMinRefreshInterval=%u (120 Hz)\n", value);
 }
 OSSafeReleaseNULL(interval);
 OSSafeReleaseNULL(refresh);
}

static void sweet_fb_te_configure(AppleCLCD *fb)
{
 unsigned enabled = 0, offset = 7800; /* 7.3 ms transfer + 0.5 ms margin */
 PE_parse_boot_argn("fuxivsyncte", &enabled, sizeof(enabled));
 fb->fVsyncTE = enabled == 1;
 if (!fb->fVsyncTE) return;
 unsigned requested = offset;
 if (PE_parse_boot_argn("fuxivsynctoff", &requested, sizeof(requested)) && requested <= 16000)
  offset = requested;
 nanoseconds_to_absolutetime((uint64_t)offset*1000, &fb->fTeOffset);
 IOLog("SWEET FB: vsync TE requested offset_us=%u\n", offset);
}

bool AppleCLCD::teVsyncTick(uint64_t &now, bool fallback)
{
 bool wasValid = fTeValid;
 fTeValid = fScanout && fuxi_gpu_scanout_te(&fTe);
 now = mach_absolute_time();
 if (!fTeValid) {
  if (wasValid && fTeLastNotify) {
   /* Resume the old period without a second tick just after the last TE. */
   fNextTick = fTeLastNotify + fTickAbs;
   return now >= fNextTick;
  }
  return fallback;
 }
 /* Offset is a phase: wrap offsets exceeding a faster panel's period.
  * Keep an armed deadline if the next TE is observed just before it fires:
  * replacing it with that TE's deadline would silently drop a notification.
  * Require a confirmed counter for every tick; skip long gaps, never burst. */
 if (!wasValid) fTeSent = false;
 uint64_t offset = fTeOffset % fTe.period;
 unsigned target = fTeSent ? (fTeSentFrame + 1) & 0xffffu : fTe.frame;
 unsigned delta = (fTe.frame - target) & 0xffffu;
 if (fTeSent && delta > 0x7fffu) {
  fTeNext = fTe.edge + fTe.period + offset;
  return false; /* the next target TE has not been observed yet */
 }
 if (!fTeSent || delta == 0 || delta > 1) {
  target = fTe.frame;
  fTeNext = fTe.edge + offset;
 }
 if (now < fTeNext) return false;
 fTeSent = true;
 fTeSentFrame = target;
 fTeNext = delta == 1 ? fTe.edge + offset : fTeNext + fTe.period;
 /* A fallback tick may have occurred just before lock acquisition. */
 if (fTeLastNotify && now - fTeLastNotify < fTe.period/2) return false;
 return true;
}

void AppleCLCD::teVsyncLog(uint64_t now)
{
 fTeLastNotify = now;
 if (fTeValid) {
  fTeLocked++;
  fTePeriodSum += fTe.period;
  uint64_t ns;
  absolutetime_to_nanoseconds((now - fTe.edge) % fTe.period, &ns);
  unsigned bin = (unsigned)(ns/128000);
  fTePhase[bin < 256 ? bin : 255]++;
 } else fTeFallback++;
 if (fTeLocked + fTeFallback != 600) return;
 unsigned p50 = 0, p90 = 0, cumulative = 0;
 for (unsigned i = 0; i < 256; i++) {
  cumulative += fTePhase[i];
  if (fTeLocked && !p50 && cumulative >= (fTeLocked+1)/2) p50 = i*128+64;
  if (fTeLocked && !p90 && cumulative >= (fTeLocked*9+9)/10) p90 = i*128+64;
  fTePhase[i] = 0;
 }
 uint64_t ns = 0;
 if (fTeLocked) absolutetime_to_nanoseconds(fTePeriodSum/fTeLocked, &ns);
 IOLog("SWEET FB: vsync te-locked=%u fallback=%u phase_us %u/%u period_us %llu\n",
     fTeLocked, fTeFallback, p50, p90, (unsigned long long)(ns/1000));
 fTeLocked = fTeFallback = 0;
 fTePeriodSum = 0;
}
#endif

bool
AppleCLCD::start(IOService *provider)
{
	PE_Video video;

	if (!IOService::start(provider)) {
		return false;
	}
	PE_current_console(&video);
	fPhys = video.v_baseAddr;
	fWidth = (uint32_t)video.v_width;
	fHeight = (uint32_t)video.v_height;
	fRowBytes = (uint32_t)video.v_rowBytes;
	if (fPhys == 0 || fWidth == 0 || fHeight == 0 || fRowBytes < fWidth * 4) {
		IOLog("SWEET FB: no boot framebuffer\n");
		return false;
	}
	int native = 0;
	PE_parse_boot_argn("sweetfb_native", &native, sizeof(native));
	fLogicalW = fWidth;
	fLogicalH = fHeight;
	if (!native && fWidth <= SWEET_FB_LOGICAL_W && fHeight <= SWEET_FB_LOGICAL_H) {
		fLogicalW = SWEET_FB_LOGICAL_W;
		fLogicalH = SWEET_FB_LOGICAL_H;
	}
	fOffX = (int32_t)(fLogicalW - fWidth) / 2;
	fOffY = (int32_t)(fLogicalH - fHeight) / 2;
	fLock = IOLockAlloc();
	fClients = OSArray::withCapacity(4);
	fSurface = sweet_surface_create_physical(fPhys, fWidth, fHeight, fRowBytes, SWEET_FB_FORMAT_BGRA);
	fPixels = fSurface != NULL ? fSurface->kernelAddress() : 0;
	fWorkLoop = IOWorkLoop::workLoop();
	if (fLock == NULL || fClients == NULL || fPixels == 0 || fWorkLoop == NULL) {
		IOLog("SWEET FB: setup failed\n");
		OSSafeReleaseNULL(fSurface);    /* stays published: also held by the table */
		OSSafeReleaseNULL(fWorkLoop);
		return false;
	}
#if defined(FUXI)
	uint32_t diff = 0, idle = 0;
	PE_parse_boot_argn("fuxi_unixnu_diff", &diff, sizeof(diff));
	PE_parse_boot_argn("fuxi_unixnu_idlefb", &idle, sizeof(idle));
	fUnixnuDiff = diff == 1;
	fUnixnuIdle = idle == 1;
	uint32_t early = 0;
	PE_parse_boot_argn("fuxiearly", &early, sizeof(early));
	fEarlyRetire = early == 1;
	if (fUnixnuDiff) {
		fShadowBytes = (uint64_t)fWidth * fHeight * 4;
		if (fWidth > 16384 || fHeight > 16384 || !fShadowBytes || fShadowBytes > (64ULL << 20) ||
		    (fWidth & 1) || (fPixels & 7) || (fRowBytes & 7)) {
			fUnixnuDiff = false;
		} else {
			fShadow = IOMallocAligned((vm_size_t)fShadowBytes, 64);
			if (!fShadow) fUnixnuDiff = false;
		}
		IOLog("FUXI UNIXNU FB: differential copy %s (CPU full-panel BGRA only)\n",
		    fUnixnuDiff ? "enabled" : "unavailable");
	}
	if (fUnixnuIdle) IOLog("FUXI UNIXNU FB: demand timer enabled, software 60 Hz, panel power inherited\n");
	sweet_fb_te_configure(this);
#endif
	uint32_t timerOptions = sweet_fb_timer_options();
	if (timerOptions == kIOTimerEventSourceOptionsPriorityHigh)
		IOLog("FUXI SCANOUT: present timer THREAD_CALL_PRIORITY_HIGH\n");	fVsync = IOTimerEventSource::timerEventSource(timerOptions, this, &AppleCLCD::vsyncFired);
	if (fVsync == NULL || fWorkLoop->addEventSource(fVsync) != kIOReturnSuccess) {
		IOLog("SWEET FB: no vsync timer\n");
		return false;
	}
#if defined(FUXI)
	uint32_t hz = 60;
#else
	uint32_t hz = 10;
#endif
	PE_parse_boot_argn("sweetfb_hz", &hz, sizeof(hz));
	fFlushTicks = (hz == 0 || hz >= 60) ? 1 : 60 / hz;
#if defined(FUXI)
	nanoseconds_to_absolutetime(SWEET_FB_VSYNC_US * 1000ULL, &fTickAbs);
	fNextTick = mach_absolute_time() + fTickAbs;
	fTickArmed = true;
	fStatStart = mach_absolute_time();
	fVsync->wakeAtTime(kIOTimeOptionsWithLeeway, fNextTick, 0);
	IOLog("SWEET FB: present up to %u Hz (every %u ticks)\n", 60 / fFlushTicks, fFlushTicks);
#else
	fVsync->setTimeoutUS(SWEET_FB_VSYNC_US);
#endif

	setProperty(kIOUserClientClassKey, "IOMobileFramebufferUserClient");
	setProperty("external", false);
	setProperty("DisplayPipePlaneBaseAlignment", 64ULL, 32);
#if defined(FUXI)
	int backlight = 0;
	PE_parse_boot_argn("fuxibacklight", &backlight, sizeof(backlight));
	fBacklightPowerNotifications = backlight == 2 || backlight == 3;
	fuxi_backlight_publish(this);
	sweet_fb_refresh_publish(this);
 fSwapHist = sweet_fb_adt_u32(provider, "fuxi,swap-hist") == 1;
 nanoseconds_to_absolutetime(100000000ULL, &fSwapIdle);
 if (fSwapHist) IOLog("SWEET FB: swap interval histogram on (fuxi,swap-hist)\n");
#endif
	registerService();
	IOLog("SWEET FB: AppleCLCD %ux%u stride %u at 0x%llx, default surface %u, presented as %ux%u\n",
	    fWidth, fHeight, fRowBytes, fPhys, fSurface->id, fLogicalW, fLogicalH);

#if defined(FUXI)
 uint32_t scanout = 0, check = 0, log = 0;
 PE_parse_boot_argn("fuxigpuscanout", &scanout, sizeof(scanout));
 PE_parse_boot_argn("fuxigpuscanoutcheck", &check, sizeof(check));
 PE_parse_boot_argn("fuxigpuscanoutlog", &log, sizeof(log));
 int quiet = 0;
 PE_parse_boot_argn("sweetfb_noconsole", &quiet, sizeof(quiet));
 fScanout = scanout == 1 && quiet == 1;
 fScanoutLog = log == 1 ? 2 : fScanout ? 1 : 0;
 if (scanout == 1 && !fScanout)
  IOLog("FUXI SCANOUT: unavailable, requires sweetfb_noconsole=1; existing present path\n");
 if (fScanout) { sweet_console_muted = 1; fConsolePaused = true; }
 fuxi_gpu_scanout_configure(fPixels, fScanout, check);
 uint32_t nc = 0;
 PE_parse_boot_argn("fuxigpuncimport", &nc, sizeof(nc));
 if (fScanout && (nc & 8u)) fFlog = IONewZeroData(FlogEntry, SWEET_FB_FLOG);
 fFlogState = fFlog ? 1 : 0; fCopyFirst = fScanout && (nc & 16u); fSkipSuperseded = fScanout && (nc & 32u);
 if (fScanout && (nc & (56u | 134217728u))) IOLog("SWEET FB: frame_log=%u copy_first=%u skip_superseded=%u park=%u (fuxigpuncimport 8/16/32/134217728)\n", fFlogState, fCopyFirst, fSkipSuperseded, fCopyFirst && (nc & 134217728u) ? 1u : 0u);
 if (fScanout) {
  fWriterWakeCall = thread_call_allocate(&AppleCLCD::writerWake, this);
  if (!fWriterWakeCall) return false;
  sweet_surface_present_notify(&AppleCLCD::writerRetired, this);
 }
 fuxi_gpu_start(fPhys, fWidth, fHeight, fRowBytes);
	fuxi_perf_start();
#endif
	return true;
}

void
AppleCLCD::free(void)
{
#if defined(FUXI)
 if (fScanout) sweet_surface_present_notify(NULL, NULL);
 if (fWriterWakeCall) { thread_call_cancel_wait(fWriterWakeCall); thread_call_free(fWriterWakeCall); }
#endif
 if (fLock) { IOLockLock(fLock); stopRecords(); IOLockUnlock(fLock); }
	if (fShadow) IOFreeAligned(fShadow, (vm_size_t)fShadowBytes);
#if defined(FUXI)
 if (fFlog) IODeleteData(fFlog, FlogEntry, SWEET_FB_FLOG);
#endif
	IOMobileFramebuffer::free();
}

/* fLock serializes sleep/wake decisions with swaps, notifications and power requests. */
bool
AppleCLCD::presentWantedLocked(void)
{
	bool notify = false;
	for (unsigned int i = 0; i < fClients->getCount(); i++) {
		IOMobileFramebufferUserClient *client =
		    (IOMobileFramebufferUserClient *)fClients->getObject(i);
		if (client->fFunc[SWEET_FB_NOTIFY_VSYNC] && client->fPort != MACH_PORT_NULL) notify = true;
	}
#if defined(FUXI)
 if (fScanout && (fPublicationPending || fuxi_gpu_scanout_pending())) return true;
#endif
	return fuxi_unixnu_tick_wanted(fPowerOff, fFadeStep == 0, fDirty, fGpuPending, notify);
}

void
AppleCLCD::wakePresentLocked(void)
{
#if defined(FUXI)
	if (!fUnixnuIdle || fTickArmed || !presentWantedLocked()) return;
	fTickArmed = true;
	fNextTick = mach_absolute_time() + fTickAbs;
	fVsync->wakeAtTime(kIOTimeOptionsWithLeeway, fNextTick, 0);
	if (++fIdleWakes == 1) IOLog("FUXI UNIXNU FB: timer wake\n");
#endif
}

static void
sweet_fb_rearm(AppleCLCD *fb, IOTimerEventSource *sender)
{
#if defined(FUXI)
 if (fb->fScanout && fuxi_gpu_scanout_owned()) {
  /* Poll ownership/fences at ~1 ms while active, independently of 60 Hz
   * notification deadlines. No extra notification or vsync count per poll. */
  IOLockLock(fb->fLock);
  if (fb->fUnixnuIdle && !fb->presentWantedLocked()) {
   fb->fTickArmed = false;
   IOLockUnlock(fb->fLock);
   return;
  }
  uint64_t now = mach_absolute_time();
  if (now >= fb->fNextTick)
   fb->fNextTick = fuxi_unixnu_next_tick(fb->fNextTick, fb->fTickAbs, now);
  uint64_t deadline = fb->fNextTick;
  bool active = fb->fGpuPending || fb->fPublicationPending || fuxi_gpu_scanout_pending() || (!fb->fScanoutHeld &&
      (fb->fPowerOff ? fb->fFadeStep == 0 : fb->fDirty));
  /* fuxigpuncimport bit 2048: poll the in-flight scanout copy at 1/64 tick so its surface lease is released at retire. */
  uint64_t poll = now + fb->fTickAbs/((fb->fGpuPending && fuxi_gpu_abi_pace()) || ((fb->fPublicationPending || fuxi_gpu_scanout_pending()) && fuxi_gpu_abi_start()) ? 64 : 16);
  if (fb->fVsyncTE) {
   if (fb->fTeValid) {
    deadline = fb->fTeNext;
    poll = now + fb->fTe.period/16;
   }
   /* Keep observing TE even with subscribers but no dirty swap. */
   if (poll < deadline || deadline <= now) deadline = poll;
  } else if (active && poll < deadline) deadline = poll;
  fb->fTickArmed = true;
  sender->wakeAtTime(kIOTimeOptionsWithLeeway, deadline, 0);
  IOLockUnlock(fb->fLock);
  return;
 }
	if (!fb->fUnixnuIdle) {
		/* Preserve the existing continuous-tick path when the option is absent. */
		uint64_t now = mach_absolute_time();
  if (!fb->fScanout || now >= fb->fNextTick)
   fb->fNextTick = fuxi_unixnu_next_tick(fb->fNextTick, fb->fTickAbs, now);
		sender->wakeAtTime(kIOTimeOptionsWithLeeway, fb->fNextTick, 0);
		return;
	}
	/* Keep scheduling and wakeups under the same lock: no lost idle->swap wake. */
	IOLockLock(fb->fLock);
	if (!fb->presentWantedLocked()) {
		fb->fTickArmed = false;
		if (++fb->fIdleStops == 1) IOLog("FUXI UNIXNU FB: timer idle\n");
		IOLockUnlock(fb->fLock);
		return;
	}
	uint64_t now = mach_absolute_time();
 if (!fb->fScanout || now >= fb->fNextTick)
  fb->fNextTick = fuxi_unixnu_next_tick(fb->fNextTick, fb->fTickAbs, now);
	sender->wakeAtTime(kIOTimeOptionsWithLeeway, fb->fNextTick, 0);
	IOLockUnlock(fb->fLock);
#else
	(void)fb;
	sender->setTimeoutUS(SWEET_FB_VSYNC_US);
#endif
}

void
AppleCLCD::vsyncFired(OSObject *owner, IOTimerEventSource *sender)
{
	AppleCLCD *fb = (AppleCLCD *)owner;
	uint64_t now = mach_absolute_time();
 bool vsyncTick = true;
#if defined(FUXI)
 if (fb->fScanout) vsyncTick = now >= fb->fNextTick;
 if (fb->fVsyncTE) {
  vsyncTick = fb->teVsyncTick(now, vsyncTick);
  if (vsyncTick) fb->teVsyncLog(now);
 }
#endif
 uint64_t count = vsyncTick ? ++fb->fVsyncCount : fb->fVsyncCount;
	uint64_t values[6] = {
		(uint32_t)count, (uint32_t)(count >> 32),
		(uint32_t)now, (uint32_t)(now >> 32),
		0, 0,
	};

	IOLockLock(fb->fLock);
	for (unsigned int i = 0; i < fb->fClients->getCount(); i++) {
		IOMobileFramebufferUserClient *client =
		    (IOMobileFramebufferUserClient *)fb->fClients->getObject(i);
		if (vsyncTick) client->notify(SWEET_FB_NOTIFY_VSYNC, values, 6);
	}
 IOLockUnlock(fb->fLock);
#if defined(FUXI)
 fb->fStageCallbacks[vsyncTick ? 0 : 1]++;
 bool flush = (count % fb->fFlushTicks) == 0 && (vsyncTick || fb->fFlushTicks == 1);
 if (!fb->presentStep(flush)) { sweet_fb_rearm(fb, sender); return; }
#endif
 IOLockLock(fb->fLock);
	if (fb->fPowerOff) {
		/* Fade out: halve every channel each vsync, then black, then idle. */
		uint32_t step = fb->fFadeStep;
		if (step <= 6) {
			fb->fFadeStep = step + 1;
		}
		IOLockUnlock(fb->fLock);
#if defined(FUXI)
		if (step == 0) {
			fb->fShadowValid = false;
			if (fb->fUnixnuDiff) {
				fuxi_unixnu_fill_pixels((volatile void *)fb->fPixels,
				    (uint64_t)fb->fRowBytes * fb->fHeight, fb->fRowBytes,
				    fb->fWidth, fb->fHeight, 0);
			} else {
				/* Plain stores, no DC ZVA (bzero) on the write-combined mapping. */
				for (uint32_t y = 0; y < fb->fHeight; y++) {
					volatile uint64_t *row = (volatile uint64_t *)(fb->fPixels + (uint64_t)y * fb->fRowBytes);
					for (uint32_t x = 0; x < fb->fWidth / 2; x++) {
						row[x] = 0;
					}
				}
			}
			__asm__ volatile ("dsb sy" ::: "memory");
            if (fb->fScanout && fuxi_gpu_scanout_publish() == 0) fuxi_display_kick();
		}
#else
		if (step <= 6) {
			for (uint32_t y = 0; y < fb->fHeight; y++) {
				volatile uint32_t *row = (volatile uint32_t *)(fb->fPixels + (uint64_t)y * fb->fRowBytes);
				for (uint32_t x = 0; x < fb->fWidth; x++) {
					row[x] = step < 6 ? (row[x] >> 1) & 0x7f7f7f7f : 0;
				}
			}
		}
#endif
		sweet_fb_rearm(fb, sender);
		return;
	}
#if !defined(FUXI)
 bool flush = fb->fDirty && (count % fb->fFlushTicks) == 0;
#endif
 IOLockUnlock(fb->fLock);
#if !defined(FUXI)
 if (flush) fb->presentNext();
#endif
	sweet_fb_rearm(fb, sender);
}

void
AppleCLCD::addClient(IOMobileFramebufferUserClient *client)
{
	IOLockLock(fLock);
	fClients->setObject(client);
	IOLockUnlock(fLock);
}

void
AppleCLCD::removeClient(IOMobileFramebufferUserClient *client)
{
	IOLockLock(fLock);
	unsigned int i = fClients->getNextIndexOfObject(client, 0);
	if (i != (unsigned int)-1) {
		fClients->removeObject(i);
	}
	IOLockUnlock(fLock);
}

static uint32_t
get32(const uint8_t *p, uint32_t off)
{
	uint32_t v;
	memcpy(&v, p + off, 4);
	return v;
}

static uint32_t sweet_fb_swaps_logged;
static uint64_t sweet_fb_swap_count;

void AppleCLCD::observeRecord(SwapRecord &record, bool shown)
{
#if defined(FUXI)
 if (!record.shown && !record.consumed) for (unsigned i = 0; i < SWEET_FB_LAYERS; i++) if (record.surfaces[i]) {
  uint64_t epoch = 0, serial = sweet_surface_writer_serial(record.surfaces[i], &epoch);
  if (epoch != record.epochs[i]) record.stale = true;
  if (!record.reused && serial != record.writers[i]) {
   record.reused = true; if (fReuseBeforeShow != ~0ull) fReuseBeforeShow++;
  }
 }
#endif
 record.shown |= shown;
}

void AppleCLCD::consumeRecord(SwapRecord &record)
{
 observeRecord(record, false); record.consumed = true;
 for (unsigned i = 0; i < SWEET_FB_LAYERS; i++) if (record.uses & (1u << i)) sweet_surface_display_drop(record.surfaces[i]);
 record.uses = 0;
 uint32_t id = get32(record.bytes, 0x50);
 SwapStatus &status = fSwapStatus[id % SWEET_FB_SWAP_HISTORY];
 if (id && status.id == id && status.state == SwapQueued) { status.state = SwapConsumed; IOLockWakeup(fLock, fSwapStatus, false); }
}

/* NativeMobileDisplay (BSD-2-Clause-Patent) early retire: the GPU copy owns the frame, so SwapWait(flags 0)
 * may return now. displayUses stay held (released at the fence) and the writer lock still blocks CA on the
 * surface until the copy retires. */
void AppleCLCD::retireEarly(const SwapRecord &record)
{
 uint32_t id = get32(record.bytes, 0x50);
 IOLockLock(fLock);
 SwapStatus &status = fSwapStatus[id % SWEET_FB_SWAP_HISTORY];
 if (id && status.id == id && status.state == SwapQueued) { status.state = SwapConsumed; fEarlyRetires++; IOLockWakeup(fLock, fSwapStatus, false); }
 IOLockUnlock(fLock);
}

void AppleCLCD::recordDone(SwapRecord &record, bool shown)
{
 consumeRecord(record); record.shown |= shown;
 uint32_t id = get32(record.bytes, 0x50);
 SwapStatus &status = fSwapStatus[id % SWEET_FB_SWAP_HISTORY];
 if (id && status.id == id && (shown || !fPublicationPending || id != fPublicationID)) {
  status.state = record.shown || status.state == SwapShown ? SwapShown : SwapDropped; IOLockWakeup(fLock, fSwapStatus, false);
  if (status.state == SwapDropped) flogFate(id, 'D');
 }
}

void AppleCLCD::releaseRecord(SwapRecord &record)
{
 recordDone(record, false);
 for (unsigned i = 0; i < SWEET_FB_LAYERS; i++) OSSafeReleaseNULL(record.surfaces[i]);
}

void AppleCLCD::writerRetired(void *context)
{
 #if defined(FUXI)
 AppleCLCD *fb = (AppleCLCD *)context;
 uint64_t empty = 0, now = mach_absolute_time();
 (void)__atomic_compare_exchange_n(&fb->fWriterWakeQueued, &empty, now, false, __ATOMIC_RELAXED, __ATOMIC_RELAXED);
 (void)thread_call_enter(fb->fWriterWakeCall);
#else
 (void)context;
#endif
}

#if defined(FUXI)
void AppleCLCD::writerWake(thread_call_param_t context, thread_call_param_t)
{
 AppleCLCD *fb = (AppleCLCD *)context;
 uint64_t stamps[2] = { __atomic_exchange_n(&fb->fWriterWakeQueued, 0, __ATOMIC_RELAXED), mach_absolute_time() };
 fb->fWorkLoop->runAction(&AppleCLCD::writerWakeAction, fb, stamps);
}
IOReturn AppleCLCD::writerWakeAction(OSObject *owner, void *arg, void *, void *, void *)
{
 AppleCLCD *fb = (AppleCLCD *)owner;
 if (arg) {
  uint64_t *stamps = (uint64_t *)arg, now = mach_absolute_time();
  for (unsigned i = 0; i < 2; i++) {
   uint64_t a = stamps[i], b = i ? now : stamps[1];
   if (!a || b < a) continue;
   uint64_t d = b-a; fb->fWakeSum[i] += d; if (d > fb->fWakeMax[i]) fb->fWakeMax[i] = d; fb->fWakeN[i]++;
  }
 }
 fb->fStageCallbacks[2]++;
 IOLockLock(fb->fLock);
 bool wanted = !fb->fPowerOff && !fb->fScanoutHeld && (fb->fDirty || fb->fGpuPending || fb->fPublicationPending);
 IOLockUnlock(fb->fLock);
 if (wanted) { (void)fb->presentStep(fb->fFlushTicks == 1); sweet_fb_rearm(fb, fb->fVsync); }
 return kIOReturnSuccess;
}
#endif

bool AppleCLCD::presentStep(bool flush)
{
#if defined(FUXI)
 uint64_t elapsed = 0;
 int completed = sweet_surface_gpu_poll(&elapsed);
 if (!completed) { fGpuBusyTicks++; fScanoutDefers++; return false; }
 if (fScanoutHeld) {
  fGpuPending = false;
  IOLockLock(fLock); stopRecords(); IOLockUnlock(fLock); return false;
 }
 if (fGpuPending) {
  fGpuPending = false;
  if (completed == 1) {
   struct fg_present_times times; fuxi_gpu_scanout_stamps(&times);
   fStage[4] = times.fence;
   if (FlogEntry *e = flogFind(fFlogGpuID)) { struct fg_copy_stamps cs; fuxi_gpu_copy_stamps(&cs); e->fence = times.fence; e->s2s = cs.s2s_us; e->blit = cs.blit_us; e->ahead = cs.ahead; e->buf = cs.buffer; e->pk = cs.parked; }
   fGpuFrames++; fGpuTime += elapsed;
   completePresent(elapsed + fScanoutPrepare);
   scanoutLog(true, fScanoutBytes, elapsed, fScanoutPrepare);
  } else {
   fGpuFailed++;
   IOLockLock(fLock); fReplay = fDirty = true; IOLockUnlock(fLock);
  }
 } else if (fScanout) {
  int published = fuxi_gpu_scanout_retry();
  if (published == 1 && fPublicationPending) presentStats(fPublicationTime);
  else if (published < 0) fScanoutHeld = true;
 }
 if (fScanoutHeld) { IOLockLock(fLock); stopRecords(); IOLockUnlock(fLock); return false; }
 if (fScanout && fuxi_gpu_scanout_pending() && (!fuxi_gpu_scanout_triple() || fPowerOff)) { fStageBlocks[0]++; fScanoutDefers++; return false; }
 IOLockLock(fLock);
 bool off = fPowerOff, wanted = !fScanoutHeld && (off ? fFadeStep == 0 : fDirty && flush);
 IOLockUnlock(fLock);
 if (!wanted) return off && !fScanoutHeld;
 if (fScanout) {
  int writable = off ? fuxi_gpu_scanout_cpu_acquire() : fuxi_gpu_scanout_acquire();
  if (writable != 1) { fStageBlocks[1]++; fScanoutHeld = writable < 0; fScanoutDefers++; if (fScanoutHeld) { IOLockLock(fLock); stopRecords(); IOLockUnlock(fLock); } return false; }
 }
 if (off) return true;
 presentNext();
#else
 (void)flush;
#endif
 return false;
}

void AppleCLCD::presentNext(void)
{
 SwapRecord record = {};
 IOLockLock(fLock);
 unsigned chosen = 0;
 bool replay = fReplay || !fPendingCount;
 if (fSkipSuperseded && !replay) for (unsigned j = fPendingCount; j-- > 1;) if (recordReady(fPending[j])) { chosen = j; fSupersededSkips += j; break; }
 for (;;) {
  if (replay && !fLastValid) { fReplay = false; fDirty = fPendingCount != 0; publishDueLocked(); IOLockUnlock(fLock); return; }
  SwapRecord &candidate = replay ? fLast : fPending[chosen];
  observeRecord(candidate, false);
  if (candidate.stale && !candidate.shown) {
   if (replay) { releaseRecord(fLast); fLastValid = fReplay = false; }
   else {
    releaseRecord(candidate); fPendingCount--;
    for (unsigned i = chosen; i < fPendingCount; i++) fPending[i] = fPending[i+1];
   }
   fReadySkips++; fStatDropped++; chosen = 0; replay = fPendingCount == 0;
   if (replay) { fDirty = false; publishDueLocked(); IOLockUnlock(fLock); return; }
   continue;
  }
#if defined(FUXI)
  SweetSurface *sources[SWEET_FB_LAYERS]; uint64_t epochs[SWEET_FB_LAYERS]; unsigned count = 0;
  uint64_t ready = candidate.swapped;
  for (unsigned i = 0; i < SWEET_FB_LAYERS; i++) if (candidate.surfaces[i]) { epochs[count] = candidate.epochs[i]; sources[count++] = candidate.surfaces[i]; }
  if (fScanout && !sweet_surface_present_begin(sources, count, &ready, replay && candidate.shown ? NULL : epochs)) {
   fQueuedDefers++;
   if (replay && fPendingCount) { replay = false; continue; }
   if (!replay && ++chosen < fPendingCount) continue;
   publishDueLocked(); IOLockUnlock(fLock); return;
  }
  candidate.ready = ready;
#endif
  if (replay && candidate.shown) {
   candidate.consumed = false;
   bool held = true;
   for (unsigned i = 0; i < SWEET_FB_LAYERS; i++) if (candidate.surfaces[i]) {
    if (sweet_surface_display_hold(candidate.surfaces[i])) candidate.uses |= 1u << i; else held = false;
#if defined(FUXI)
    candidate.writers[i] = sweet_surface_writer_serial(candidate.surfaces[i], &candidate.epochs[i]);
#endif
   }
   if (!held) {
#if defined(FUXI)
    if (fScanout) sweet_surface_present_end();
#endif
    recordDone(candidate, true); publishDueLocked(); IOLockUnlock(fLock); return;
   }
  }
  record = candidate;
  if (replay) {
   bzero(&fLast, sizeof(fLast)); fLastValid = false; fReplay = false;
  } else {
   for (unsigned i = 0; i < chosen; i++) releaseRecord(fPending[i]);
   if (fReplay) { fReplay = false; fReadySkips++; fStatDropped++; }
   fReadySkips += chosen; fStatDropped += chosen;
   fPendingCount -= chosen + 1;
   for (unsigned i = 0; i < fPendingCount; i++) fPending[i] = fPending[i + chosen + 1];
  }
  break;
 }
 fPresentRecord = &record;
 IOLockUnlock(fLock);
 fStage[0] = record.swapped; fStage[1] = record.ready; fStage[2] = mach_absolute_time();
 fStage[3] = fStage[4] = fStage[5] = 0; fStageGpu = false;
 bool accepted = present(record);
 IOLockLock(fLock);
 fPresentRecord = NULL;
 if (accepted) {
  releaseRecord(fLast); fLast = record; fLastValid = true;
 } else if (fPendingCount == SWEET_FB_QUEUE_DEPTH) {
  releaseRecord(record); fOverflowDrops++; fStatDropped++;
 } else {
  for (unsigned i = fPendingCount; i; i--) fPending[i] = fPending[i-1];
  fPending[0] = record; fPendingCount++;
 }
 fDirty = fPendingCount != 0 || fReplay;
 if (fScanoutHeld && !fGpuPending) stopRecords();
 publishDueLocked();
 IOLockUnlock(fLock);
}

IOReturn
AppleCLCD::swap(const uint8_t *rec)
{
#if defined(FUXI)
 fuxi_perf_swap();
#endif
 IOLockLock(fLock);
 uint32_t id = get32(rec, 0x50);
 SwapStatus &status = fSwapStatus[id % SWEET_FB_SWAP_HISTORY];
 if (!id || id > fSwapID || status.id == id || status.state == SwapQueued || status.state == SwapConsumed || fScanoutHeld) {
  IOLockUnlock(fLock); return kIOReturnNotReady;
 }
 SwapRecord record = {};
 memcpy(record.bytes, rec, SWEET_FB_SWAP_REC_SIZE); record.swapped = mach_absolute_time();
 bool held = true;
 for (unsigned i = 0; i < SWEET_FB_LAYERS; i++) {
  uint32_t surfaceID = get32(rec, 0x54 + 4*i);
  if (!(get32(rec, 0x104) & (1u << i)) || !surfaceID) continue;
  record.surfaces[i] = sweet_surface_copy(surfaceID);
  if (record.surfaces[i] == fSurface) OSSafeReleaseNULL(record.surfaces[i]);
  else if (!record.surfaces[i]) held = false;
  if (record.surfaces[i]) {
   if (sweet_surface_display_hold(record.surfaces[i])) record.uses |= 1u << i; else held = false;
#if defined(FUXI)
   record.writers[i] = sweet_surface_writer_serial(record.surfaces[i], &record.epochs[i]);
#endif
  }
 }
 if (!held) { releaseRecord(record); IOLockUnlock(fLock); return kIOReturnNoResources; }
 if (fPendingCount == SWEET_FB_QUEUE_DEPTH) {
  releaseRecord(fPending[0]);
  for (unsigned i = 1; i < fPendingCount; i++) fPending[i-1] = fPending[i];
  fPendingCount--; fOverflowDrops++; fStatDropped++;
 }
 status.id = id; status.state = SwapQueued; fLatestSwap = id;
 unsigned outstanding = 0;
 for (unsigned i = 0; i < SWEET_FB_SWAP_HISTORY; i++) if (fSwapStatus[i].state == SwapQueued || fSwapStatus[i].state == SwapConsumed) outstanding++;
 if (outstanding > fOutstandingMax) fOutstandingMax = outstanding;
 fPending[fPendingCount++] = record;
 fStatSwaps++; fDirty = true; wakePresentLocked();
 flogSwap(record, id); publishDueLocked();
#if defined(FUXI)
 /* Diagnostic only: fTe is the timer's last observation, read without its ordering. */
 if (fSwapHist) {
  if (fSwapLast) fSwapBins[sweet_fb_swap_bin(record.swapped - fSwapLast, fTeValid ? fTe.period : fTickAbs, fSwapIdle)]++;
  fSwapLast = record.swapped;
 }
#endif
 IOLockUnlock(fLock);
#if defined(FUXI)
 if (fScanout && fWriterWakeCall && fFlushTicks == 1) writerRetired(this);
#endif
 if ((++sweet_fb_swap_count % 1000) == 0) IOLog("SWEET FB: %llu swaps\n", sweet_fb_swap_count);
 return kIOReturnSuccess;
}

IOReturn AppleCLCD::swapWait(uint32_t id, uint32_t flags, uint64_t timeout)
{
 if (flags & ~0x80000003u) return kIOReturnBadArgument;
 uint64_t duration = 0;
 nanoseconds_to_absolutetime(timeout && timeout < 100000000ull ? timeout : 100000000ull, &duration);
 uint64_t deadline = mach_absolute_time() + duration;
 IOLockLock(fLock);
 fWaitPoll[flags & 1]++;
 if (!id) fWaitLatest++;
 if (!id && !(flags & 2)) id = fLatestSwap;
 IOReturn result;
 for (;;) {
  result = kIOReturnBusy;
  if (fScanoutHeld) result = kIOReturnNotReady;
  else if (flags & 2) {
   bool pending = fPresentRecord != NULL;
   for (unsigned i = 0; i < SWEET_FB_SWAP_HISTORY; i++) if (fSwapStatus[i].state == SwapQueued || ((flags & 1) && fSwapStatus[i].state == SwapConsumed)) pending = true;
   if (!pending) result = kIOReturnSuccess;
  } else {
   SwapStatus &status = fSwapStatus[id % SWEET_FB_SWAP_HISTORY];
   if (!id || status.id != id) result = kIOReturnNotFound;
   else if (status.state == SwapShown) result = kIOReturnSuccess;
   else if (status.state == SwapConsumed && !(flags & 1)) { result = kIOReturnSuccess; fWaitEarly++; }
   else if (status.state == SwapDropped) result = flags & 1 ? kIOReturnAborted : kIOReturnSuccess;
  }
  if (result != kIOReturnBusy || (flags & 0x80000000u)) break;
  if (mach_absolute_time() >= deadline) { result = kIOReturnTimeout; break; }
  int slept = IOLockSleepDeadline(fLock, fSwapStatus, deadline, THREAD_ABORTSAFE);
  if (slept == THREAD_INTERRUPTED) { result = kIOReturnAborted; break; }
 }
 IOLockUnlock(fLock);
 return result;
}

void AppleCLCD::stopRecords(void)
{
#if defined(FUXI)
 if (fGpuPending) fScanoutHeld = true;
 if (fGpuPending) for (unsigned i = 0; i < SWEET_FB_LAYERS; i++) if (fLast.surfaces[i]) sweet_surface_display_poison(fLast.surfaces[i]);
#endif
 for (unsigned i = 0; i < fPendingCount; i++) releaseRecord(fPending[i]);
 if (fPublicationPending) {
  SwapStatus &status = fSwapStatus[fPublicationID % SWEET_FB_SWAP_HISTORY];
  if (status.id == fPublicationID && status.state != SwapShown) { status.state = SwapDropped; flogFate(fPublicationID, 'P'); }
  fPublicationPending = false;
 }
 fPendingCount = 0; releaseRecord(fLast); fLastValid = fReplay = fDirty = false; publishDueLocked();
 IOLockWakeup(fLock, fSwapStatus, false);
}

bool
AppleCLCD::present(const SwapRecord &record)
{
 const uint8_t *rec = record.bytes;
	uint32_t mask = get32(rec, 0x104);
	uint64_t t0 = mach_absolute_time();
	bool gpuFrame = false, cpuFrame = false;
#if defined(FUXI)
 struct PresentRead { bool held; ~PresentRead() { if (held) sweet_surface_present_end(); } } read = { fScanout };
#endif
 uint64_t cpuBytes = 0, cpuTicks = 0;

	if (!fConsolePaused) {
		int quiet = 0;
		fConsolePaused = true;
		if (PE_parse_boot_argn("sweetfb_noconsole", &quiet, sizeof(quiet)) && quiet != 0) {
			/* Not PE_initialize_console(kPEDisableScreen): its console switch
			 * writes the read-only-late cons_ops_index and panics. */
			sweet_console_muted = 1;
		}
	}

	for (uint32_t layer = 0; layer < SWEET_FB_LAYERS; layer++) {
		uint32_t id = get32(rec, 0x54 + 4 * layer);
		const uint8_t *a = rec + 0x64 + 16 * layer;
		const uint8_t *b = rec + 0xc4 + 16 * layer;
		int32_t sx = (int32_t)get32(a, 0), sy = (int32_t)get32(a, 4);
		int32_t dx = (int32_t)get32(b, 0), dy = (int32_t)get32(b, 4);
		int32_t w = (int32_t)get32(b, 8), h = (int32_t)get32(b, 12);

		if (!(mask & (1u << layer)) || id == 0) {
			continue;
		}
		SweetSurface *s = record.surfaces[layer]; if (s) s->retain();
		if (sweet_fb_swaps_logged < 16) {
			sweet_fb_swaps_logged++;
			IOLog("SWEET FB: swap %u layer %u surface %u %ux%u fmt 0x%x bpr %u a %d,%d %ux%u b %d,%d %dx%d\n",
			    get32(rec, 0x50), layer, id, s ? s->width : 0, s ? s->height : 0,
			    s ? s->pixelFormat : 0, s ? s->bytesPerRow : 0, sx, sy, get32(a, 8),
			    get32(a, 12), dx, dy, w, h);
		}
		if (s == NULL || s == fSurface) {
			fShadowValid = false;
			OSSafeReleaseNULL(s);
			continue;
		}
		uintptr_t src = s->kernelAddress();
		uint32_t bpe = s->bytesPerElement ? s->bytesPerElement : 4;
		if (w <= 0 || h <= 0) {
			w = (int32_t)s->width;
			h = (int32_t)s->height;
		}
		if (sx < 0 || sy < 0) {
			sx = sy = 0;
		}
		if (dx < 0 || dy < 0) {
			dx = dy = 0;
		}
		/* Logical (presented) display coordinates -> panel coordinates. */
		dx -= fOffX;
		dy -= fOffY;
		if (dx < 0) {
			sx -= dx;
			w += dx;
			dx = 0;
		}
		if (dy < 0) {
			sy -= dy;
			h += dy;
			dy = 0;
		}
		if (w > (int32_t)s->width - sx) {
			w = (int32_t)s->width - sx;
		}
		if (h > (int32_t)s->height - sy) {
			h = (int32_t)s->height - sy;
		}
		if (w > (int32_t)fWidth - dx) {
			w = (int32_t)fWidth - dx;
		}
		if (h > (int32_t)fHeight - dy) {
			h = (int32_t)fHeight - dy;
		}
		uint64_t last = (uint64_t)s->offset + (uint64_t)(sy + h - 1) * s->bytesPerRow +
		    (uint64_t)(sx + w) * 4;
		if (w <= 0 || h <= 0 || s->elementWidth != 1 || s->elementHeight != 1 ||
		    s->memory == NULL || last > s->memory->getLength()) {
			src = 0;
		}
		if (src != 0 && bpe == 4) {
			int copied = 0;
#if defined(FUXI)
            /* Keep CPU replay complete too if a full composed crop is available. */
            if (fuxi_gpu_scanout_double() && (mask & (mask-1)) == 0 && sx >= dx && sy >= dy &&
                (uint64_t)(sx-dx)+fWidth <= s->width && (uint64_t)(sy-dy)+fHeight <= s->height &&
                ((uint64_t)(sx-dx)+fWidth)*4 <= s->bytesPerRow &&
                (uint64_t)s->offset + ((uint64_t)(sy-dy)+fHeight-1)*s->bytesPerRow +
                ((uint64_t)(sx-dx)+fWidth)*4 <= s->memory->getLength()) {
             sx -= dx; sy -= dy; dx = dy = 0; w = fWidth; h = fHeight;
            }
			uint64_t copyStart = mach_absolute_time();
			/* CA's composed frame has one layer. Preserve the CPU path for
			 * multi-layer records rather than submit multiple fences per frame. */
			if ((mask & (mask-1)) == 0)
				copied = sweet_surface_gpu_copy(s, src, sx, sy, dx, dy, w, h);
            if (copied == 3 || (copied < 0 && fScanout)) {
             fStageBlocks[2]++;
             fScanoutDefers++;
             s->release();
             return false; /* busy ring: keep the record for retry */
            }
			if (copied == 2) {
             fScanoutPrepare = mach_absolute_time() - copyStart;
             struct fg_present_times times; fuxi_gpu_scanout_stamps(&times);
             fStageGpu = true; fStage[3] = times.submitted;
             fFlogGpuID = get32(rec, 0x50);
             if (FlogEntry *e = flogFind(fFlogGpuID)) { e->ready = record.ready; e->submit = times.submitted; }
             if (read.held) { sweet_surface_present_end(); read.held = false; }
             if (fEarlyRetire) retireEarly(record);
             fScanoutBytes = fuxi_gpu_scanout_double() ? (uint64_t)1080 * 2400 * 4 : (uint64_t)w * h * 4;
				fShadowValid = false;
				gpuFrame = true;
				fGpuPending = true;
			}
            if (copied != 2 && fScanout) {
             int writable = fuxi_gpu_scanout_cpu_acquire();
             if (writable != 1) {
              fScanoutHeld = writable < 0;
              fScanoutDefers++;
              s->release();
              return false;
             }
            }
			copyStart = mach_absolute_time();
#endif
			bool diffCopied = false;
#if defined(FUXI)
			if (copied != 2 && fUnixnuDiff && sweet_console_muted &&
			    s->pixelFormat == SWEET_FB_FORMAT_BGRA && (mask & (mask - 1)) == 0 &&
			    dx == 0 && dy == 0 && w == (int32_t)fWidth && h == (int32_t)fHeight) {
				uint64_t offset = (uint64_t)s->offset + (uint64_t)sy * s->bytesPerRow + (uint64_t)sx * 4;
				uint64_t stored = fuxi_unixnu_copy_diff((volatile void *)fPixels,
				    (uint64_t)fRowBytes * fHeight, fRowBytes, (const void *)(src + offset),
				    s->memory->getLength() - offset, s->bytesPerRow, fWidth, fHeight,
				    fShadow, fShadowBytes, fShadowValid);
				if (stored != UINT64_MAX) {
					diffCopied = fShadowValid = true;
					fDiffStored += stored;
					fDiffFull += (uint64_t)fWidth * fHeight * 4;
					if (++fDiffFrames == 1 || fDiffFrames % 600 == 0)
						IOLog("FUXI UNIXNU FB: diff %u frames stored %llu of %llu bytes\n",
						    fDiffFrames, fDiffStored, fDiffFull);
				}
			}
#endif
			if (copied != 2 && !diffCopied) {
				fShadowValid = false;
#if defined(FUXI)
				if (fUnixnuDiff) {
					uint64_t offset = (uint64_t)s->offset + (uint64_t)sy * s->bytesPerRow + (uint64_t)sx * 4;
					uint64_t dest = (uint64_t)dy * fRowBytes + (uint64_t)dx * 4;
					fuxi_unixnu_copy_pixels((volatile void *)(fPixels + dest),
					    (uint64_t)fRowBytes * fHeight - dest, fRowBytes, (const void *)(src + offset),
					    s->memory->getLength() - offset, s->bytesPerRow, w, h);
				} else
#endif
				for (int32_t y = 0; y < h; y++) {
					memcpy((void *)(fPixels + (uint64_t)(dy + y) * fRowBytes + (uint64_t)dx * 4),
					    (const void *)(src + s->offset + (uint64_t)(sy + y) * s->bytesPerRow + (uint64_t)sx * 4),
					    (size_t)w * 4);
				}
			}
#if defined(FUXI)
			if (copied != 2) {
				cpuFrame = true;
                uint64_t duration = mach_absolute_time() - copyStart;
				fCpuTime += duration;
                cpuTicks += duration;
                cpuBytes += (uint64_t)w * h * 4;
			}
#endif
		}
		s->release();
	}
#if defined(FUXI)
 fCpuFrames += cpuFrame;
 if (!gpuFrame) {
  fStage[3] = fStage[4] = mach_absolute_time();
  if (read.held) { sweet_surface_present_end(); read.held = false; }
  completePresent(mach_absolute_time() - t0);
  if (cpuFrame) scanoutLog(false, cpuBytes, cpuTicks, 0);
 }
#endif
 return true;
}

void AppleCLCD::completePresent(uint64_t took)
{
#if defined(FUXI)
 IOLockLock(fLock);
 SwapRecord &record = fPresentRecord ? *fPresentRecord : fLast;
 consumeRecord(record);
 if (fPublicationPending) {
  SwapStatus &status = fSwapStatus[fPublicationID % SWEET_FB_SWAP_HISTORY];
  if (status.id == fPublicationID && status.state != SwapShown) { status.state = SwapDropped; flogFate(fPublicationID, 'P'); }
  fPublicationPending = false; fStatDropped++;
  IOLockWakeup(fLock, fSwapStatus, false);
 }
 IOLockUnlock(fLock);
 int published = fuxi_gpu_scanout_publish();
 if (!published) fuxi_display_kick();
 if (published == 2) {
  IOLockLock(fLock);
  fPublicationPending = true; fPublicationTime = took; fPublicationID = get32(record.bytes, 0x50);
  memcpy(fPublicationStage, fStage, sizeof(fStage));
  IOLockUnlock(fLock);
 }
 else if (published >= 0) presentStats(took);
 else if (fScanout) fScanoutHeld = true;
#else
 (void)took;
#endif
}

void AppleCLCD::stageStats(uint64_t now)
{
#if defined(FUXI)
 struct fg_present_times times; fuxi_gpu_scanout_stamps(&times);
 bool owned = fScanout && fuxi_gpu_scanout_owned();
 fStage[5] = owned ? times.start : now;
 if (owned) fLastStart = times.previous_start;
 for (unsigned i = 0; i < 6; i++) {
  uint64_t a = i < 5 ? fStage[i] : fLastStart, b = i < 5 ? fStage[i+1] : fStage[5];
  if (!a || !b || b < a) continue;
  uint64_t d = b-a;
  fStageSum[i] += d; if (d > fStageMax[i]) fStageMax[i] = d; fStageN[i]++;
 }
 fLastStart = fStage[5];
 if (fStatFrames != 599) return;
 uint64_t avg[6], max[6];
 for (unsigned i = 0; i < 6; i++) {
  absolutetime_to_nanoseconds(fStageN[i] ? fStageSum[i]/fStageN[i] : 0, &avg[i]); avg[i] /= 1000;
  absolutetime_to_nanoseconds(fStageMax[i], &max[i]); max[i] /= 1000;
 }
 IOLog("SWEET FB: stages 600 shown swap_ready_us=%llu/%llu ready_present_us=%llu/%llu present_submit_us=%llu/%llu "
     "submit_fence_us=%llu/%llu fence_start_us=%llu/%llu start_start_us=%llu/%llu samples=%u/%u/%u/%u/%u/%u callbacks_vsync=%u poll=%u wake=%u callbacks_per_shown_x1000=%llu/%llu/%llu\n",
     avg[0], max[0], avg[1], max[1], avg[2], max[2], avg[3], max[3], avg[4], max[4], avg[5], max[5],
     fStageN[0], fStageN[1], fStageN[2], fStageN[3], fStageN[4], fStageN[5], fStageCallbacks[0], fStageCallbacks[1], fStageCallbacks[2],
     (uint64_t)fStageCallbacks[0]*1000/600, (uint64_t)fStageCallbacks[1]*1000/600, (uint64_t)fStageCallbacks[2]*1000/600);
 bzero(fStageSum, sizeof(fStageSum)); bzero(fStageMax, sizeof(fStageMax)); bzero(fStageN, sizeof(fStageN)); bzero(fStageCallbacks, sizeof(fStageCallbacks));
 uint64_t wakeAvg[2], wakeMax[2];
 for (unsigned i = 0; i < 2; i++) {
  absolutetime_to_nanoseconds(fWakeN[i] ? fWakeSum[i]/fWakeN[i] : 0, &wakeAvg[i]); wakeAvg[i] /= 1000;
  absolutetime_to_nanoseconds(fWakeMax[i], &wakeMax[i]); wakeMax[i] /= 1000;
 }
 IOLog("SWEET FB: pacing wake_dispatch_us=%llu/%llu wake_gate_us=%llu/%llu wake_samples=%u/%u blocked_publication=%u acquire=%u ring=%u\n",
     wakeAvg[0], wakeMax[0], wakeAvg[1], wakeMax[1], fWakeN[0], fWakeN[1], fStageBlocks[0], fStageBlocks[1], fStageBlocks[2]);
 bzero(fWakeSum, sizeof(fWakeSum)); bzero(fWakeMax, sizeof(fWakeMax)); bzero(fWakeN, sizeof(fWakeN)); bzero(fStageBlocks, sizeof(fStageBlocks));
#else
 (void)now;
#endif
}

void AppleCLCD::presentStats(uint64_t took)
{
#if defined(FUXI)
 uint64_t activeStage[6]; bool deferred = fPublicationPending;
 memcpy(activeStage, fStage, sizeof(fStage));
 IOLockLock(fLock);
 uint32_t shownID = deferred ? fPublicationID : get32((fPresentRecord ? *fPresentRecord : fLast).bytes, 0x50);
 if (deferred) {
  SwapStatus &status = fSwapStatus[fPublicationID % SWEET_FB_SWAP_HISTORY];
  if (status.id == fPublicationID) status.state = SwapShown;
  if (fLastValid && get32(fLast.bytes, 0x50) == fPublicationID) fLast.shown = true;
  fPublicationPending = false; IOLockWakeup(fLock, fSwapStatus, false);
  memcpy(fStage, fPublicationStage, sizeof(fStage));
 } else recordDone(fPresentRecord ? *fPresentRecord : fLast, true);
 IOLockUnlock(fLock);
 uint64_t t1 = mach_absolute_time();
 stageStats(t1);
 if (deferred) memcpy(fStage, activeStage, sizeof(fStage));
 if (fFlogState == 2) {
  if (FlogEntry *e = flogFind(shownID)) {
   struct fg_present_times pt; fuxi_gpu_scanout_stamps(&pt); struct fg_copy_stamps cs; fuxi_gpu_copy_stamps(&cs);
   e->start = fScanout && fuxi_gpu_scanout_owned() ? pt.start : t1; e->te = cs.te; e->xfer = cs.xfer_us; e->fate = 'S';
  }
  flogPrint(t1);
 }
	fStatCopy += took;
	fStatCopyMax = took > fStatCopyMax ? took : fStatCopyMax;
	if (++fStatFrames >= 600) {
		uint64_t avg_ns, max_ns, span_ns;
		absolutetime_to_nanoseconds(fStatCopy / fStatFrames, &avg_ns);
		absolutetime_to_nanoseconds(fStatCopyMax, &max_ns);
		absolutetime_to_nanoseconds(t1 - fStatStart, &span_ns);
		IOLockLock(fLock);
		uint32_t swaps = fStatSwaps, dropped = fStatDropped;
  uint32_t pending = fQueuedDefers, overflow = fOverflowDrops, skips = fReadySkips;
  uint64_t reuse = fReuseBeforeShow;
  uint64_t waits[2] = { fWaitPoll[0], fWaitPoll[1] }, early = fWaitEarly, latest = fWaitLatest;
  unsigned outstanding = fOutstandingMax;
		fStatSwaps = fStatDropped = 0;
#if defined(FUXI)
  uint32_t bins[7]; memcpy(bins, fSwapBins, sizeof(bins)); bzero(fSwapBins, sizeof(fSwapBins));
  uint64_t period_ns = 0; absolutetime_to_nanoseconds(fTeValid ? fTe.period : fTickAbs, &period_ns);
#endif
		IOLockUnlock(fLock);
		IOLog("SWEET FB: stats %u frames in %llu ms, copy avg %llu us max %llu us, "
		    "%u swaps, %u not shown queued_pending=%u fifo_overflow=%u ready_skips=%u ownership_defers=%u reuse_before_show=%llu wait_mode0=%llu wait_mode1=%llu wait_early=%llu wait_id0=%llu outstanding_max=%u early_retires=%u\n", fStatFrames, span_ns / 1000000, avg_ns / 1000,
		    max_ns / 1000, swaps, dropped, pending, overflow, skips, fScanoutDefers, reuse, waits[0], waits[1], early, latest, outstanding, fEarlyRetires);
#if defined(FUXI)
  if (fSwapHist)
   IOLog("SWEET FB: swap_interval period_us=%llu vsyncs 0/1/2/3/4/5+/idle=%u/%u/%u/%u/%u/%u/%u\n", period_ns / 1000,
       bins[0], bins[1], bins[2], bins[3], bins[4], bins[5], bins[6]);
#endif
		uint64_t gpu_ns, cpu_ns;
		absolutetime_to_nanoseconds(fGpuTime, &gpu_ns);
		absolutetime_to_nanoseconds(fCpuTime, &cpu_ns);
		struct fg_times split;
		uint64_t cleaned, skipped, invalidated, mapped;
		unsigned imports;
		fuxi_gpu_import_stats(&cleaned, &skipped, &invalidated, &mapped, &imports);
		sweet_surface_gpu_stats(&split);
		uint64_t ticks[] = {split.pin, split.lookup, split.map, split.clean,
		    split.submit, split.wait, split.unmap, split.cpu};
		uint64_t us[8];
		unsigned attempts = split.attempts;
		for (unsigned i = 0; i < 8; i++) {
			uint64_t ns;
			absolutetime_to_nanoseconds(ticks[i], &ns);
			us[i] = attempts ? ns / 1000 / attempts : 0;
		}
		IOLog("FUXI GPU: presents GPU %u CPU %u, GPU retire avg %llu us CPU avg %llu us, "
		    "split pin %llu lookup %llu map+TLB %llu clean %llu submit %llu wait %llu unmap %llu us, "
		    "GPU CPU-time %llu us, cache hit %u miss %u, sleeps %u, 2M %u 64K %u, busy ticks %u failed %u, "
		    "imports %u mapped %llu cache cleaned %llu skipped %llu invalidated %llu bytes cumulative\n",
		    fGpuFrames, fCpuFrames, fGpuFrames ? gpu_ns / 1000 / fGpuFrames : 0ULL,
		    fCpuFrames ? cpu_ns / 1000 / fCpuFrames : 0ULL,
		    us[0], us[1], us[2], us[3], us[4], us[5], us[6], us[7],
		    split.hits, split.misses, split.sleeps, split.blocks, split.groups, fGpuBusyTicks, fGpuFailed,
		    imports, mapped, cleaned, skipped, invalidated);
		fuxi_gpu_abi_stats_log();
		fuxi_gpu_abi_sched_log();
  if (fSkipSuperseded) { IOLog("SWEET FB: superseded_skips=%u (ready swaps dropped before their copy, a newer ready swap copied instead)\n", fSupersededSkips); fSupersededSkips = 0; }
		fGpuBusyTicks = fGpuFailed = 0;
		fGpuFrames = fCpuFrames = 0;
		fGpuTime = fCpuTime = 0;
		fStatFrames = 0;
		fStatCopy = fStatCopyMax = 0;
		fStatStart = t1;
	}
#else
 (void)took;
#endif
}

void
AppleCLCD::scanoutLog(bool gpu, uint64_t bytes, uint64_t copy, uint64_t prepare)
{
#if defined(FUXI)
 if (!fScanoutLog) return;
 uint64_t ns, prep;
 absolutetime_to_nanoseconds(copy, &ns);
 absolutetime_to_nanoseconds(prepare, &prep);
 ++fScanoutFrames;
 if (gpu) ++fScanoutGpu; else ++fScanoutCpu;
 fScanoutTime += ns;
 if (ns > fScanoutMax) fScanoutMax = ns;
 /* Per-frame console lines cost CPU on every swap; summary only by default. */
 if (fScanoutLog == 2)
  IOLog("FUXI SCANOUT: frame=%u path=%s bytes=%llu copy_us=%llu prepare_us=%llu\n",
      fScanoutFrames, gpu ? "GPU" : "CPU", bytes, ns/1000, prep/1000);
 if (fScanoutFrames % 120 == 0)
  IOLog("FUXI SCANOUT: summary frames=%u GPU=%u CPU=%u copy_avg_us=%llu copy_max_us=%llu deferred=%u\n",
      fScanoutFrames, fScanoutGpu, fScanoutCpu, fScanoutTime/1000/fScanoutFrames,
      fScanoutMax/1000, fScanoutDefers);
#else
 (void)gpu; (void)bytes; (void)copy; (void)prepare;
#endif
}

bool
IOMobileFramebufferUserClient::initWithTask(task_t owningTask, void *securityID, UInt32 type)
{
	if (!IOUserClient::initWithTask(owningTask, securityID, type)) {
		return false;
	}
	fTask = owningTask;
	return true;
}

bool
IOMobileFramebufferUserClient::start(IOService *provider)
{
	fFB = OSDynamicCast(AppleCLCD, provider);
	if (fFB == NULL || !IOUserClient::start(provider)) {
		return false;
	}
	fFB->addClient(this);
	return true;
}

IOReturn
IOMobileFramebufferUserClient::clientClose(void)
{
	if (fFB != NULL) {
		fFB->removeClient(this);
		IOLockLock(fFB->fLock);
		mach_port_t old = fPort;
		fPort = MACH_PORT_NULL;
		IOLockUnlock(fFB->fLock);
		if (old != MACH_PORT_NULL) {
			releaseNotificationPort(old);
		}
	}
	terminate();
	return kIOReturnSuccess;
}

IOReturn
IOMobileFramebufferUserClient::registerNotificationPort(mach_port_t port, __unused UInt32 type,
    __unused io_user_reference_t refCon)
{
	mach_port_t old;
	IOLockLock(fFB->fLock);
	old = fPort;
	fPort = port;
	fFB->wakePresentLocked();
	IOLockUnlock(fFB->fLock);
	if (old != MACH_PORT_NULL) {
		releaseNotificationPort(old);
	}
	return kIOReturnSuccess;
}

/* Called with the framebuffer lock held. */
void
IOMobileFramebufferUserClient::notify(uint32_t type, const uint64_t *values, uint32_t count)
{
	OSAsyncReference64 ref;

	if (type >= SWEET_FB_NOTIFY_TYPES || fFunc[type] == 0 || fPort == MACH_PORT_NULL) {
		return;
	}
	bzero(ref, sizeof(ref));
	setAsyncReference64(ref, fPort, (mach_vm_address_t)fFunc[type], fRefcon[type], fTask);
	sendAsyncResult64WithOptions(ref, kIOReturnSuccess, (io_user_reference_t *)values, count,
	    kIOUserNotifyOptionCanDrop);
}

static uint64_t sweet_fb_logged;        /* selectors already logged (< 64) */

IOReturn
AppleCLCD::requestPowerChange(uint64_t state)
{
	void *argument = (void *)(uintptr_t)state;
#if defined(FUXI)
	/* Serialize modes 2/3 requests and interest delivery with each other.
	 * Never call arbitrary interest clients holding fLock. */
	if (fBacklightPowerNotifications)
		return fWorkLoop->runAction(&AppleCLCD::powerChangeAction, this, argument);
#endif
	return powerChangeAction(this, argument, NULL, NULL, NULL);
}

IOReturn
AppleCLCD::powerChangeAction(OSObject *owner, void *state, void *, void *, void *)
{
	AppleCLCD *fb = (AppleCLCD *)owner;
	bool off = state == NULL;
	IOLockLock(fb->fLock);
	if (off != fb->fPowerOff) {
		fb->fPowerOff = off;
		fb->fFadeStep = 0;
		fb->fDirty = true;     /* on: redraw the last accepted frame if the FIFO is empty */
	}
	sweet_fb_display_off = off ? 1 : 0;
	fb->wakePresentLocked();
	IOLockUnlock(fb->fLock);
	IOLog("SWEET FB: display %s\n", off ? "off" : "on");
#if defined(FUXI)
	if (fb->fBacklightPowerNotifications) fuxi_backlight_display_state(fb, !off);
#endif
	return kIOReturnSuccess;
}

IOReturn
IOMobileFramebufferUserClient::externalMethod(uint32_t selector, IOExternalMethodArguments *args,
    __unused IOExternalMethodDispatch *dispatch, __unused OSObject *target,
    __unused void *reference)
{
	IOReturn ret = kIOReturnSuccess;
	uint64_t out[2] = { 0, 0 };
	uint32_t outCount = 0;

	switch (selector) {
	case kSweetFBGetLayerDefaultSurface:
		out[0] = sweet_surface_port_in_task(fFB->fSurface, fTask);
		outCount = 1;
		break;
	case kSweetFBSwapBegin:
		IOLockLock(fFB->fLock);
		if (fFB->fSwapID == UINT32_MAX) ret = kIOReturnNoResources; else out[0] = ++fFB->fSwapID;
		IOLockUnlock(fFB->fLock);
		outCount = 1;
		break;
	case kSweetFBSwapEnd: {
		uint8_t rec[SWEET_FB_SWAP_REC_SIZE];
		if (args->structureInputDescriptor != NULL ||
		    args->structureInputSize < SWEET_FB_SWAP_REC_SIZE) {
			ret = kIOReturnBadArgument;
			break;
		}
		memcpy(rec, args->structureInput, sizeof(rec));
		ret = fFB->swap(rec);
		break;
	}
	case kSweetFBGetID:
		out[0] = 0;
		outCount = 1;
		break;
	case kSweetFBGetDisplaySize:
		out[0] = fFB->fLogicalW;
		out[1] = fFB->fLogicalH;
		outCount = 2;
		break;
	case kSweetFBIsMainDisplay:
		out[0] = 1;
		outCount = 1;
		break;
	case kSweetFBGetDotPitch:
		out[0] = SWEET_FB_DOT_PITCH;
		outCount = 1;
		break;
	case kSweetFBNotifications:
		if (args->scalarInputCount >= 3 && args->scalarInput[2] < SWEET_FB_NOTIFY_TYPES) {
			uint32_t type = (uint32_t)args->scalarInput[2];
			IOLockLock(fFB->fLock);
			fFunc[type] = args->scalarInput[0];
			fRefcon[type] = args->scalarInput[1];
			fFB->wakePresentLocked();
			IOLockUnlock(fFB->fLock);
			char name[32];
			proc_selfname(name, sizeof(name));
			IOLog("SWEET FB: %s notifications type %u %s\n", name, type,
			    args->scalarInput[0] ? "on" : "off");
		}
		break;
	case kSweetFBRequestPowerChange:
		if (args->scalarInputCount >= 1) {
			ret = fFB->requestPowerChange(args->scalarInput[0]);
		}
		break;
	case kSweetFBSwapWait:
  ret = args->scalarInputCount == 3 && args->scalarInput[0] <= UINT32_MAX && args->scalarInput[1] <= UINT32_MAX ?
      fFB->swapWait((uint32_t)args->scalarInput[0], (uint32_t)args->scalarInput[1], args->scalarInput[2]) : kIOReturnBadArgument;
  break;
	default:
		break;
	}

	for (uint32_t i = 0; i < args->scalarOutputCount; i++) {
		args->scalarOutput[i] = i < outCount ? out[i] : 0;
	}
	if (args->structureOutputDescriptor == NULL && args->structureOutput != NULL) {
		bzero(args->structureOutput, args->structureOutputSize);
	}
	if (selector >= 64 || !(sweet_fb_logged & (1ULL << selector))) {
		if (selector < 64) {
			sweet_fb_logged |= 1ULL << selector;
		}
		char name[32];
		proc_selfname(name, sizeof(name));
		IOLog("SWEET FB: %s sel %u in %u/%u out %u/%u -> 0x%x\n", name, selector,
		    args->scalarInputCount, args->structureInputSize,
		    args->scalarOutputCount, args->structureOutputSize, ret);
	}
	return ret;
}
#endif /* SWEET */
