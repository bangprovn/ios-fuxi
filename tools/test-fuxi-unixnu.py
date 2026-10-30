#!/usr/bin/env python3
"""P-UNIXNU production-code host models. No device, firmware or upstream execution."""
import argparse
import importlib.util
from pathlib import Path
import shlex
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'xnu/fuxi-unixnu'
PRESENT_TIMES = re.search(r'struct fg_present_times \{.*?\};',
    (ROOT / 'xnu/fuxi-adreno/fuxi_adreno.h').read_text(), re.S)[0] + '\n' + re.search(r'struct fg_copy_stamps \{.*?\};',
    (ROOT / 'xnu/fuxi-adreno/fuxi_adreno.h').read_text(), re.S)[0] + '\n' + re.search(r'enum \{ FG_DD_GEOMETRY.*?\n#define FG_DD_SLOT_PAGES[^\n]*\n',
    (ROOT / 'xnu/fuxi-adreno/fuxi_adreno.h').read_text(), re.S)[0]
TE_OBSERVER = re.search(r'struct fg_te_observer \{.*?\};',
    (ROOT / 'xnu/fuxi-adreno/fuxi_adreno.h').read_text(), re.S)[0]


def function(source, name):
    match = re.search(r'^(?:static (?:inline )?)?(?:int|void|bool|uint32_t|uint64_t|IOReturn)\s*\n?' +
                      re.escape(name) + r'\([^;]*?\)\s*\{', source, re.M)
    assert match, name
    depth, end = 1, match.end()
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[match.start():end] + '\n'


UFS_PREFIX = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <errno.h>
#include <sys/types.h>
#include "fuxi_unixnu_ufs.h"
#define FUXI 1
#define TRUE 1
#define MIN(a,b) ((a)<(b)?(a):(b))
#define NSEC_PER_SEC 1000ull
#define NSEC_PER_USEC 1ull
#define THREAD_UNINT 0
#define TIMEOUT_URGENCY_SYS_CRITICAL 0
#define THREAD_WAITING 1
#define THREAD_CONTINUE_NULL 0
#define __unused __attribute__((unused))
#define B_READ 1
#define EROFS 30
#define LCK_GRP_DECLARE(n,g) int n
#define LCK_MTX_DECLARE(n,g) int n
#define __builtin_arm_dsb(x) ((void)(x))
typedef uintptr_t vm_offset_t;
typedef uint64_t addr64_t;
typedef void *event_t;
static bool fx_root=true;
static int sweet_ufs_lock;
static bool fx_write_allowed(uint64_t lba,uint64_t n) {return lba>=10 && n && lba+n<=100;}
static bool fx_map(uint64_t lba,uint64_t *dev,uint64_t *run) {*dev=10+lba;*run=100-lba;return true;}
struct buffer {char data[4096]; unsigned flags,count,resid;int error,done;};
typedef struct buffer *buf_t;
static unsigned buf_blkno(buf_t b) {(void)b;return 0;}
static unsigned buf_count(buf_t b) {return b->count;}
static unsigned buf_flags(buf_t b) {return b->flags;}
static void buf_setresid(buf_t b,unsigned n) {b->resid=n;}
static int buf_map(buf_t b,char **p) {*p=b->data;return 0;}
static void buf_unmap(buf_t b) {(void)b;}
static void buf_seterror(buf_t b,int n) {b->error=n;}
static void buf_biodone(buf_t b) {b->done++;}
static void lck_mtx_lock(int *p) {(void)p;}
static void lck_mtx_unlock(int *p) {(void)p;}
'''

UFS_MODEL = r'''
static unsigned mode,reads,rings,clears,logs,invalidates,flushes,sleeps;
static uint32_t doorbell,status,registers[64];
static unsigned mmioWrites;
static bool submitted;
static uint64_t ticks;
static void response(void) {
 struct utrd *d=(void *)(su.dma+DMA_UTRL);struct ucd *u=(void *)(su.dma+DMA_UCD);
 d->dw2=mode==12?2:0;
 u->resp[0]=0x21;u->resp[2]=su.lun;u->resp[3]=su.tag;
 if(mode==6)u->resp[3]++;
 if(mode==7)u->resp[2]++;
 if(mode==8)u->resp[15]=1;
 if(mode==9){u->resp[10]=1;u->resp[11]=0xff;}
 if(mode==10)u->resp[8]=1;
 if(mode==11)u->resp[7]=2; /* check condition is a completed command, not DMA uncertainty */
 memset((void *)(su.dma+DMA_BOUNCE),0x5a,4096);
 doorbell=0;status|=1;
}
static uint32_t ufs_rd(uint32_t off) {
 if(off==REG_UTRLDBR) {
  if(submitted) {
   reads++;
   if(mode==4)status|=0x10000;
   else if(mode==1){if(reads>=3)response();}
   else if(mode==2){doorbell=0;if(reads>=3)response();}
   else if(mode==3)doorbell=1;
   else if(mode==5)doorbell=0; /* descriptor remains invalid */
   else response();
  }
  return doorbell;
 }
 if(off==REG_IS)return status;
 assert(off/4<64);return registers[off/4];
}
static void ufs_wr(uint32_t off,uint32_t v) {
 mmioWrites++;
 if(off==REG_UTRLDBR){assert(v==1);submitted=true;rings++;doorbell=1;return;}
 if(off==REG_UTRLCLR){clears++;doorbell=0;return;}
 if(off==REG_IS)status&=~v;else {assert(off/4<64);registers[off/4]=v;}
}
static void dma_sync(uint32_t off,uint32_t len) {(void)off;(void)len;flushes++;}
static void dma_clean(uint32_t off,uint32_t len) {(void)off;(void)len;flushes++;}
static void fuxi_ufs_cache(uint32_t off,uint32_t len,bool inv) {
 assert(off+len<=DMA_SIZE && !su.poisoned);
 if(inv)invalidates++;else flushes++;
}
static uint64_t mach_absolute_time(void) {return ticks++;}
static void nanoseconds_to_absolutetime(uint64_t n,uint64_t *p) {*p=n;}
static bool ml_get_interrupts_enabled(void) {return true;}
static bool ml_at_interrupt_context(void) {return false;}
static int get_preemption_level(void) {return 0;}
static int assert_wait_deadline_with_leeway(event_t e,int a,int b,uint64_t t,int l) {
 (void)e;(void)a;(void)b;(void)l;assert(t<=5002);ticks=t;sleeps++;return THREAD_WAITING;
}
static void thread_block(int n) {(void)n;}
static void delay(int n) {ticks+=n;}
static int model_printf(const char *fmt,...) {if(strstr(fmt,"quarantined"))logs++;return 0;}
#define printf model_printf
'''

UFS_TEST = r'''
static unsigned char dma[DMA_SIZE] __attribute__((aligned(64)));
static void reset(unsigned m,bool enabled) {
 memset(&su,0,sizeof(su));memset(dma,0xcc,sizeof(dma));su.dma=(uintptr_t)dma;
 su.dma_pa=0x100000000ull;su.unixnu=enabled;su.ready=true;su.writable=true;
 su.secsize=512;su.nblocks=80;mode=m;reads=rings=clears=logs=invalidates=flushes=sleeps=0;
 doorbell=status=mmioWrites=0;submitted=false;ticks=0;
 memset(registers,0,sizeof(registers));registers[REG_HCE/4]=1;registers[REG_HCS/4]=HCS_DP|HCS_UTRLRDY;registers[REG_CAP/4]=CAP_64AS;
}
int main(void) {
 uint8_t read[10]={0x28}, write[10]={0x2a,0,0,0,0,10,0,0,1};
 for(unsigned cache=0;cache<2;cache++)for(unsigned wait=0;wait<2;wait++)for(unsigned m=0;m<=12;m++) {
  reset(m,true);su.cache=cache;su.wait=wait;
  int rc=ufs_scsi(read,10,4096,false);
  bool poison=m>=3 && m<=10;
  if(m<=2)assert(!rc && !su.poisoned && invalidates>=3 && !clears);
  else if(m==11 || m==12)assert(rc==EIO && !su.poisoned);
  else {
   assert(rc==(m==3 || m==5?ETIMEDOUT:EIO) && su.poisoned && logs==1 && !clears);
   unsigned char saved[DMA_SIZE];memcpy(saved,dma,sizeof(dma));
   unsigned previous=rings, previousCache=flushes+invalidates;
   assert(ufs_scsi(write,10,4096,true)==EIO);
   assert(ufs_scsi(read,10,4096,false)==EIO);
   struct buffer b={.flags=0,.count=4096};memset(b.data,0x9c,4096);
   sweet_ufs_strategy(&b);assert(b.error==EIO && b.resid==4096 && b.done==1);
   assert(!memcmp(saved,dma,sizeof(dma)) && rings==previous && logs==1 && flushes+invalidates==previousCache);
  }
  if(poison && (m==3 || m==5) && wait)assert(sleeps);
 }
 for(unsigned fatal=0;fatal<2;fatal++) {
  reset(0,true);if(fatal)status=0x800;else doorbell=2;
  unsigned char saved[DMA_SIZE];memcpy(saved,dma,sizeof(dma));
  struct buffer b={.count=4096};sweet_ufs_strategy(&b);
  assert(b.error==EIO && su.poisoned && !rings && !memcmp(saved,dma,sizeof(dma)));
 }
 reset(0,true);status=0x800;assert(ufs_take_over_host()==EIO && su.poisoned && !mmioWrites);
 reset(0,true);status=0x401;assert(!ufs_take_over_host() && status==0x400);
 reset(0,false);status=0x401;assert(!ufs_take_over_host() && status==0);
 reset(0,true);assert(!ufs_scsi(write,10,4096,true)); /* valid root extent */
 write[5]=1;assert(ufs_scsi(write,10,4096,true)==EROFS); /* outside extents */
 assert(ufs_scsi(NULL,10,0,false)==EINVAL);
 assert(ufs_scsi(read,17,0,false)==EINVAL);
 reset(3,false);assert(ufs_scsi(read,10,4096,false)==ETIMEDOUT && clears==1 && !su.poisoned);
 reset(0,false);assert(!ufs_scsi(read,10,4096,false) && !invalidates && !logs);
 // Pure completion/response boundaries in addition to real transport fault injection.
 assert(fuxi_unixnu_ufs_end(0,0,0,1)==FUXI_UFS_COMPLETE);
 assert(fuxi_unixnu_ufs_end(0,0x800,0,0)==FUXI_UFS_FATAL);
 uint8_t r[512]={0x21,0,3,7};assert(fuxi_unixnu_ufs_response(r,3,7));
 r[11]=1;assert(!fuxi_unixnu_ufs_response(r,3,7));
 r[11]=2;assert(fuxi_unixnu_ufs_response(r,3,7));r[33]=1;assert(!fuxi_unixnu_ufs_response(r,3,7));
 r[10]=1;r[11]=224;r[32]=1;r[33]=222;assert(fuxi_unixnu_ufs_response(r,3,7));
 r[11]=225;assert(!fuxi_unixnu_ufs_response(r,3,7));
 puts("PASS: actual UFS submit/strategy: fast/delayed/phantom completion, fatal, timeout, response validation, no DMA reuse, default path");
}
'''

COPY_TEST = r'''
#include <assert.h>
#include <stdint.h>
#include <string.h>
#include <stdio.h>
#include "fuxi_unixnu_copy.h"
int main(void) {
 uint64_t src[128],dst[128],shadow[128];
 for(unsigned i=0;i<128;i++){src[i]=0x33221100ull+i;dst[i]=0xccccccccccccccccull;shadow[i]=0;}
 // 18 visible pixels (72 bytes): one 64-byte block and one 8-byte tail; padded rows.
 assert(fuxi_unixnu_copy_diff(dst,sizeof(dst),96,src,sizeof(src),104,18,3,shadow,sizeof(shadow),0)==216);
 assert(fuxi_unixnu_copy_diff(dst,sizeof(dst),96,src,sizeof(src),104,18,3,shadow,sizeof(shadow),1)==0);
 src[1]^=42;assert(fuxi_unixnu_copy_diff(dst,sizeof(dst),96,src,sizeof(src),104,18,3,shadow,sizeof(shadow),1)==64);
 src[8]^=42;assert(fuxi_unixnu_copy_diff(dst,sizeof(dst),96,src,sizeof(src),104,18,3,shadow,sizeof(shadow),1)==8);
 for(unsigned y=0;y<3;y++){assert(!memcmp((char *)dst+y*96,(char *)src+y*104,72));assert(dst[y*12+9]==0xccccccccccccccccull);}
 assert(fuxi_unixnu_copy_diff(dst,sizeof(dst),96,src,sizeof(src),104,18,3,shadow,215,1)==UINT64_MAX);
 assert(fuxi_unixnu_copy_diff(dst,sizeof(dst),96,src,sizeof(src),104,17,3,shadow,sizeof(shadow),1)==UINT64_MAX);
 assert(!fuxi_unixnu_copy_pixels((char *)dst+1,sizeof(dst)-1,96,src,sizeof(src),104,18,3));
 assert(!fuxi_unixnu_copy_pixels(dst,263,96,src,sizeof(src),104,18,3)); // last pixel missing
 assert(!fuxi_unixnu_copy_pixels(dst,sizeof(dst),96,src,279,104,18,3));
 assert(!fuxi_unixnu_copy_pixels(dst,sizeof(dst),96,src,sizeof(src),104,0,3));
 assert(!fuxi_unixnu_surface_bounds(18,3,UINT64_MAX,UINT64_MAX,UINT64_MAX));
 assert(!fuxi_unixnu_surface_bounds(18,3,64,512,512));
 assert(!fuxi_unixnu_surface_bounds(18,3,96,512,511));
 // 32-bit destination alignment and odd width use scalar tail stores; last row needs visible bytes only.
 memset(dst,0xcc,sizeof(dst));
 assert(fuxi_unixnu_copy_pixels((char *)dst+4,268,96,src,276,104,17,3));
 for(unsigned y=0;y<3;y++)assert(!memcmp((char *)dst+4+y*96,(char *)src+y*104,68));
 assert(fuxi_unixnu_fill_pixels(dst,sizeof(dst),96,18,3,0));
 for(unsigned y=0;y<3;y++)for(unsigned x=0;x<9;x++)assert(dst[y*12+x]==0);
 puts("PASS: actual bounded/differential copy: padding, crop, dirty blocks/tails, alignment, size and overflow refusal");
}
'''


DISPLAY_PREFIX = r'''
#include <assert.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <stdarg.h>
#include <vector>
#include "fuxi_unixnu_copy.h"
#include "fuxi_unixnu_display.h"
#define FUXI 1
#define OSDeclareDefaultStructors(n)
#define APPLE_KEXT_OVERRIDE
#define MACH_PORT_NULL 0
#define SWEET_FB_SWAP_REC_SIZE 0x46c
#define SWEET_FB_LAYERS 4
#define SWEET_FB_QUEUE_DEPTH 3
#define SWEET_FB_SWAP_HISTORY 64
#define SWEET_FB_FLOG 128
#define SWEET_FB_FLOG_WARMUP 3000
#define SWEET_FB_FLOG_TRIGGER_FLOOR 300
#define SWEET_FB_FLOG_TRIGGER_SWAPS 60
#define SWEET_FB_FLOG_TRIGGER_DROPS 3
#define SWEET_FB_NOTIFY_TYPES 8
#define SWEET_FB_NOTIFY_VSYNC 5
#define SWEET_FB_FORMAT_BGRA 0x42475241
#define kIOTimeOptionsWithLeeway 1
#define kIOReturnSuccess 0
#define kIOReturnBusy 1
#define kIOReturnNotReady 2
#define kIOReturnBadArgument 3
#define kIOReturnNotFound 4
#define kIOReturnAborted 5
#define kIOReturnTimeout 6
#define kIOReturnNoResources 7
#define THREAD_ABORTSAFE 0
#define THREAD_INTERRUPTED 1
#define OSSafeReleaseNULL(p) do {if(p){p->release();p=nullptr;}}while(0)
typedef void OSObject;
typedef int IOReturn;
struct IOService {};
struct IOMemoryMap {};
static void advanceGate(uint64_t);
struct IOWorkLoop {
 bool gated=false;
 uint64_t gateDelay=0;
 IOReturn runAction(IOReturn (*action)(OSObject *,void *,void *,void *,void *),OSObject *owner,void *arg=nullptr) {
  assert(!gated);advanceGate(gateDelay);gated=true;auto r=action(owner,arg,nullptr,nullptr,nullptr);gated=false;return r;
 }
};
using thread_call_param_t=void *;
struct WakeCall {void (*action)(void *,void *);void *context;bool pending=false;};
using thread_call_t=WakeCall *;
static bool thread_call_enter(thread_call_t call) {bool was=call->pending;call->pending=true;return was;}
static void runWake(WakeCall &call) {assert(call.pending);call.pending=false;call.action(call.context,nullptr);}
struct IOLock {bool held=false;};
static void IOLockLock(IOLock *p) {assert(!p->held);p->held=true;}
static void IOLockUnlock(IOLock *p) {assert(p->held);p->held=false;}
static uint64_t ticks=1000;
static void advanceGate(uint64_t delay) {ticks+=delay;}
static unsigned waitSleeps;static bool interruptWait;static void (*waitHook)();
static void IOLockWakeup(IOLock *p,void *,bool) {assert(p->held);}
static int IOLockSleepDeadline(IOLock *p,void *,uint64_t deadline,int) {
 assert(p->held);waitSleeps++;IOLockUnlock(p);if(waitHook)waitHook();else ticks=deadline;IOLockLock(p);return interruptWait?THREAD_INTERRUPTED:0;
}
static unsigned arms,kicks,pollMode=1,gpuMode=0,refs,logs;
static uint64_t mach_absolute_time(void) {return ticks++;}
static void absolutetime_to_nanoseconds(uint64_t n,uint64_t *out) {*out=n;}
static unsigned teOption,teOffsetOption=7800,teReads;
static bool teOffsetPresent=true;
static bool PE_parse_boot_argn(const char *name,void *n,unsigned) {
 if(!strcmp(name,"fuxivsynctoff") && !teOffsetPresent)return false;
 unsigned value=!strcmp(name,"fuxivsyncte")?teOption:!strcmp(name,"fuxivsynctoff")?teOffsetOption:1;
 *(unsigned *)n=value;return true;
}
static void nanoseconds_to_absolutetime(uint64_t n,uint64_t *out) {*out=n;}
static void fuxi_perf_swap(void) {}
static void fuxi_display_kick(void) {kicks++;}
static int scanAcquire=1,scanPublish=0;
static bool scanOwned=false,scanBackReady=false,scanDouble=false,scanTriple=false;
static unsigned scanRetries,scanCpuAcquires;
static fg_present_times scanTimes;
static void fuxi_gpu_scanout_stamps(fg_present_times *t) {*t=scanTimes;}
static fg_copy_stamps copyStamps;
static void fuxi_gpu_copy_stamps(fg_copy_stamps *t) {*t=copyStamps;}
static uint32_t dueIds[8];static unsigned dueN,duePublishes;
static void fuxi_gpu_present_due(const uint32_t *ids,unsigned n) {assert(n<=8);memcpy(dueIds,ids,n*sizeof(*ids));dueN=n;duePublishes++;}
static int scanCpuAcquire=1;
static int fuxi_gpu_scanout_triple(void) {return scanTriple;}
static int fuxi_gpu_scanout_double(void) {return scanDouble;}
static int fuxi_gpu_scanout_pending(void) {return scanBackReady;}
static int scanRetryResult;
static int fuxi_gpu_scanout_retry(void) {scanRetries++;if(scanRetryResult==1){scanBackReady=false;scanTimes.previous_start=scanTimes.start;scanTimes.start=mach_absolute_time();}return scanRetryResult;}
static int fuxi_gpu_scanout_cpu_acquire(void) {scanCpuAcquires++;return scanCpuAcquire==1?scanAcquire:scanCpuAcquire;}
static int fuxi_gpu_scanout_owned(void) {return scanOwned;}
static bool teAvailable;
static uint64_t teEdge,tePeriod=16667000;
static unsigned teFrame;
static int fuxi_gpu_scanout_te(struct fg_te_observer *te) {
 teReads++;te->edge=teEdge;te->period=tePeriod;te->frame=teFrame;
 return scanOwned && teAvailable;
}
static unsigned scanPublications;
static int fuxi_gpu_scanout_acquire(void) {return scanAcquire;}
static unsigned scanFate;static int scanAsync,scanAsyncLate;
static int fuxi_gpu_scanout_async_take(void) {int a=scanAsync;scanAsync=0;return a;}
static unsigned fuxi_gpu_scanout_offer_fate(void) {return scanFate;}
static int fuxi_gpu_scanout_publish(void) {scanFate=scanAsyncLate?1:scanBackReady?2:0;scanAsyncLate=0;scanPublications++;if(scanPublish==1){scanTimes.previous_start=scanTimes.start;scanTimes.start=mach_absolute_time();}if(scanPublish==2)scanBackReady=true;return scanPublish;}
static char importStatsLog[2048],teStatsLog[2048],frameStatsLog[2048],stageStatsLog[2048],pacingStatsLog[2048];
static char flogEndLog[1024],unshownLog[1024];static unsigned flogLines;
static int IOLog(const char *format,...) {
 logs++;
 if(!strncmp(format,"SWEET FB: flog %u",17))flogLines++;
 if(strstr(format,"SWEET FB: flog end")) {
  va_list args;va_start(args,format);vsnprintf(flogEndLog,sizeof(flogEndLog),format,args);va_end(args);
 }
 if(strstr(format,"SWEET FB: unshown paths")) {
  va_list args;va_start(args,format);vsnprintf(unshownLog,sizeof(unshownLog),format,args);va_end(args);
 }
 if(strstr(format,"SWEET FB: pacing")) {
  va_list args;va_start(args,format);vsnprintf(pacingStatsLog,sizeof(pacingStatsLog),format,args);va_end(args);
 }
 if(strstr(format,"SWEET FB: stages")) {
  va_list args;va_start(args,format);vsnprintf(stageStatsLog,sizeof(stageStatsLog),format,args);va_end(args);
 }
 if(strstr(format,"SWEET FB: stats")) {
  va_list args;va_start(args,format);vsnprintf(frameStatsLog,sizeof(frameStatsLog),format,args);va_end(args);
 }
 if(strstr(format,"vsync te-locked=")) {
  va_list args;va_start(args,format);vsnprintf(teStatsLog,sizeof(teStatsLog),format,args);va_end(args);
 }
 if(strstr(format,"bytes cumulative")) {
  va_list args;va_start(args,format);vsnprintf(importStatsLog,sizeof(importStatsLog),format,args);va_end(args);
 }
 return 0;
}
struct IOTimerEventSource {uint64_t next=0;void wakeAtTime(int,uint64_t n,int){next=n;arms++;}void setTimeoutUS(unsigned n){next=ticks+n*1000;arms++;}};
static unsigned abiStatsLogs;
static void fuxi_gpu_abi_stats_log(void) {abiStatsLogs++;}
static unsigned abiSchedLogs;
static void fuxi_gpu_abi_sched_log(void) {abiSchedLogs++;}
static int abiPace;
static int fuxi_gpu_abi_pace(void) {return abiPace;}
static int fuxi_gpu_abi_start(void) {return abiPace;}
struct OSArray {
 std::vector<void *> objects;
 unsigned getCount(){return objects.size();}
 void *getObject(unsigned i){return objects[i];}
};
struct Memory {uint64_t size;uint64_t getLength(){return size;}};
struct SweetSurface {
 uint32_t id=1,width=18,height=3,pixelFormat=SWEET_FB_FORMAT_BGRA,bytesPerRow=104;
 uint32_t bytesPerElement=4,elementWidth=1,elementHeight=1,offset=0;
 unsigned gpuWriters=0;bool gpuQuarantined=false;uint64_t gpuReadyTime=0,gpuWriterSerial=0,gpuWriterEpoch=0;unsigned displayUses=0;bool usePoisoned=false;
 uintptr_t address;Memory *memory;
 uintptr_t kernelAddress(){return address;}
 void retain(){refs++;}
 void release(){assert(refs);refs--;}
};
static void sweet_surface_display_poison(SweetSurface *s) {s->usePoisoned=true;}
static bool holdFails;
static bool sweet_surface_display_hold(SweetSurface *s) {if(holdFails)return false;s->displayUses++;return true;}
static void sweet_surface_display_drop(SweetSurface *s) {assert(s->displayUses);s->displayUses--;}
static uint64_t sweet_surface_writer_serial(SweetSurface *s,uint64_t *epoch=nullptr) {if(epoch)*epoch=s->gpuWriterEpoch;return s->gpuWriterSerial;}
static unsigned presentBegins,presentEnds;static bool presentReading;
static bool sweet_surface_present_begin(SweetSurface **surfaces,unsigned count,uint64_t *ready,const uint64_t *epochs) {
 presentBegins++;assert(!presentReading);for(unsigned i=0;i<count;i++)if(surfaces[i]->gpuWriters || surfaces[i]->gpuQuarantined || (epochs && surfaces[i]->gpuWriterEpoch!=epochs[i]))return false;
 for(unsigned i=0;i<count;i++)if(surfaces[i]->gpuReadyTime>*ready)*ready=surfaces[i]->gpuReadyTime;
 presentReading=true;return true;
}
static void sweet_surface_present_end() {assert(presentReading);presentReading=false;presentEnds++;}
static SweetSurface *inputSurface,*inputSurface2;
static SweetSurface *sweet_surface_copy(uint32_t id) {assert(id==1 || (id==2 && inputSurface2));refs++;return id==1?inputSurface:inputSurface2;}
static int sweet_console_muted=1;
static unsigned surfaceCopies;
static void (*duringCopy)();
static int sweet_surface_gpu_copy(SweetSurface *,uintptr_t,int,int,int,int,int,int){surfaceCopies++;if(duringCopy)duringCopy();if(gpuMode==2){scanTimes.submitted=mach_absolute_time();scanTimes.fence=0;}return gpuMode;}
static int directMode,directOnStub;static unsigned directCalls,directLayerRefusals;
static int sweet_surface_direct(SweetSurface *,uintptr_t,unsigned,unsigned,unsigned,unsigned){directCalls++;return directMode;}
static int fuxi_gpu_scanout_direct_on(){return directOnStub;}
static void fuxi_gpu_scanout_direct_refused(unsigned why){directLayerRefusals+=why==FG_DD_LAYERS;}
static int sweet_surface_gpu_poll(uint64_t *elapsed) {*elapsed=42;if(pollMode==1)scanTimes.fence=mach_absolute_time();return pollMode;}
struct fg_times {uint64_t pin,lookup,map,clean,submit,wait,unmap,cpu;unsigned attempts,hits,misses,sleeps,blocks,groups;};
static void fuxi_gpu_import_stats(uint64_t *cleaned,uint64_t *skipped,uint64_t *invalidated,
 uint64_t *mapped,unsigned *imports) {*cleaned=64;*skipped=128;*invalidated=64;*mapped=16384;*imports=1;}
static void sweet_surface_gpu_stats(fg_times *t){memset(t,0,sizeof(*t));}
static unsigned sweet_fb_swaps_logged=16;
static uint64_t sweet_fb_swap_count;
class IOMobileFramebufferUserClient {
 public:uint64_t fFunc[8]={},fPort=0,notifications=0;
 void notify(unsigned type,const uint64_t *,unsigned){if(fFunc[type] && fPort)notifications++;}
};
'''

DISPLAY_PREFIX = DISPLAY_PREFIX.replace('#define FUXI 1', TE_OBSERVER + '\n' + PRESENT_TIMES + '\n#define FUXI 1')

REFRESH_TEST = r'''
int main(void) {
 assert(sweet_fb_refresh_interval(120)==35791394 && sweet_fb_refresh_interval(60)==71582788 && sweet_fb_refresh_interval(0)==0);
 assert(!sweet_fb_refresh_wanted(0,false,0) && sweet_fb_refresh_wanted(1,false,0) && !sweet_fb_refresh_wanted(2,false,0));
 assert(sweet_fb_refresh_wanted(1,true,120) && !sweet_fb_refresh_wanted(1,true,60) && !sweet_fb_refresh_wanted(1,true,30));
 assert(sweet_fb_refresh_interval_mhz(120000)==sweet_fb_refresh_interval(120) && sweet_fb_refresh_interval_mhz(120800)==35554365);
 assert(sweet_fb_refresh_interval_mhz(0)==35791394 && sweet_fb_refresh_interval_mhz(60000)==35791394 && sweet_fb_refresh_interval_mhz(240001)==35791394);
 assert(sweet_fb_refresh_interval_mhz(61000)==70409300 && sweet_fb_refresh_interval_mhz(240000)==17895697);
 const uint64_t te=8274,idle=100000;
 assert(sweet_fb_swap_bin(0,te,idle)==0 && sweet_fb_swap_bin(4136,te,idle)==0 && sweet_fb_swap_bin(4137,te,idle)==1 && sweet_fb_swap_bin(8274,te,idle)==1);
 assert(sweet_fb_swap_bin(16548,te,idle)==2 && sweet_fb_swap_bin(16667,te,idle)==2 && sweet_fb_swap_bin(24822,te,idle)==3 && sweet_fb_swap_bin(33096,te,idle)==4);
 assert(sweet_fb_swap_bin(45000,te,idle)==5 && sweet_fb_swap_bin(99999,te,idle)==5 && sweet_fb_swap_bin(100000,te,idle)==6 && sweet_fb_swap_bin(50,0,idle)==5);
 assert(sweet_fb_swap_bin(16667,16667,idle)==1 && sweet_fb_swap_bin(8333,16667,idle)==0);
 puts("PASS: IOMFBDisplayRefresh interval, fuxi,refresh-mhz and gating; swap-interval bins");return 0;
}
'''

DISPLAY_TEST = r'''
static IOReturn offer(AppleCLCD &fb,const uint8_t *rec) {
 uint8_t next[SWEET_FB_SWAP_REC_SIZE];memcpy(next,rec,sizeof(next));uint32_t id=get32(next,0x50);
 if(!id || fb.fSwapStatus[id%SWEET_FB_SWAP_HISTORY].id==id)id=fb.fSwapID+1;
 if(id>fb.fSwapID)fb.fSwapID=id;memcpy(next+0x50,&id,4);return fb.swap(next);
}
static unsigned recordRefs(const AppleCLCD &fb) {
 unsigned n=0;for(unsigned i=0;i<fb.fPendingCount;i++)for(auto *s:fb.fPending[i].surfaces)n+=s!=nullptr;
 if(fb.fLastValid)for(auto *s:fb.fLast.surfaces)n+=s!=nullptr;return n;
}
static void put32(uint8_t *r,unsigned offset,uint32_t v){memcpy(r+offset,&v,4);}
static AppleCLCD *arrivingFB;static uint8_t arrivingRecord[SWEET_FB_SWAP_REC_SIZE];
static AppleCLCD *waitFB;
static void finishWait() {waitFB->presentNext();}
static void arrivals() {for(unsigned id=22;id<=24;id++){put32(arrivingRecord,0x50,id);offer(*arrivingFB,arrivingRecord);}}

int main(void) {
 AppleCLCD fb={};IOLock lock;OSArray clients;IOTimerEventSource timer;
 uint64_t pixels[128]={},source[128]={},shadow[128]={};Memory memory={sizeof(source)};
 SweetSurface surface;surface.address=(uintptr_t)source;surface.memory=&memory;inputSurface=&surface;
 fb.fLock=&lock;fb.fClients=&clients;fb.fVsync=&timer;fb.fTickAbs=100;fb.fNextTick=ticks+100;
 fb.fWidth=18;fb.fHeight=3;fb.fRowBytes=96;fb.fPixels=(uintptr_t)pixels;
 fb.fShadow=shadow;fb.fShadowBytes=sizeof(shadow);fb.fUnixnuDiff=true;fb.fFlushTicks=1;
 fb.fConsolePaused=true;
 uint8_t rec[SWEET_FB_SWAP_REC_SIZE]={};put32(rec,0x104,1);put32(rec,0x54,1);
 put32(rec,0xc4+8,18);put32(rec,0xc4+12,3);
 for(unsigned i=0;i<128;i++)source[i]=0xabcdef00ull+i;
 // Default continuous timer; then opt-in idle, wake on swap and return to idle after present.
 sweet_fb_rearm(&fb,&timer);assert(arms==1);
 fb.fUnixnuIdle=true;fb.fTickArmed=true;sweet_fb_rearm(&fb,&timer);assert(!fb.fTickArmed && arms==1);
 offer(fb,rec);assert(fb.fTickArmed && fb.fDirty && arms==2);
 fb.vsyncFired(&fb,&timer);assert(!fb.fTickArmed && !fb.fDirty && fb.fShadowValid && fb.fDiffStored==216 && refs==recordRefs(fb));
 offer(fb,rec);fb.vsyncFired(&fb,&timer);assert(fb.fDiffStored==216 && fb.fDiffFull==432);
 // GPU offer invalidates the shadow, polling keeps the timer alive until retirement.
 gpuMode=2;offer(fb,rec);fb.vsyncFired(&fb,&timer);assert(fb.fGpuPending && fb.fTickArmed && !fb.fShadowValid);
 pollMode=0;unsigned before=arms;fb.vsyncFired(&fb,&timer);assert(fb.fGpuPending && fb.fTickArmed && arms==before+1);
 memset(pixels,0x55,sizeof(pixels));pollMode=1;fb.vsyncFired(&fb,&timer);assert(!fb.fGpuPending && !fb.fTickArmed);
 // fuxiearly: the swap is Consumed when the GPU copy is submitted, before its fence; off by default.
 {gpuMode=2;offer(fb,rec);uint32_t eid=fb.fSwapID;fb.vsyncFired(&fb,&timer);
  assert(fb.fGpuPending && fb.fEarlyRetires==0 && fb.fSwapStatus[eid%SWEET_FB_SWAP_HISTORY].state==AppleCLCD::SwapQueued);
  pollMode=1;fb.vsyncFired(&fb,&timer);assert(!fb.fGpuPending && fb.fSwapStatus[eid%SWEET_FB_SWAP_HISTORY].state!=AppleCLCD::SwapQueued);
  fb.fEarlyRetire=true;offer(fb,rec);eid=fb.fSwapID;fb.vsyncFired(&fb,&timer);
  assert(fb.fGpuPending && fb.fEarlyRetires==1 && fb.fSwapStatus[eid%SWEET_FB_SWAP_HISTORY].state==AppleCLCD::SwapConsumed);
  assert(fb.swapWait(eid,0,0)==kIOReturnSuccess && fb.fWaitEarly>=1 && fb.swapWait(eid,0x80000001u,0)==kIOReturnBusy);
  pollMode=1;fb.vsyncFired(&fb,&timer);assert(!fb.fGpuPending && fb.fEarlyRetires==1);fb.fEarlyRetire=false;}
 gpuMode=0;offer(fb,rec);fb.vsyncFired(&fb,&timer);assert(fb.fDiffStored==432 && fb.fShadowValid);
 // Blanking invalidates the shadow; swaps while off remain pending without waking a cleared black screen.
 fb.fPowerOff=true;fb.fFadeStep=0;fb.fDirty=true;
 IOLockLock(&lock);fb.wakePresentLocked();IOLockUnlock(&lock);
 fb.vsyncFired(&fb,&timer);assert(!fb.fTickArmed && !fb.fShadowValid);
 for(unsigned y=0;y<3;y++)for(unsigned x=0;x<9;x++)assert(pixels[y*12+x]==0);
 offer(fb,rec);assert(!fb.fTickArmed && fb.fDirty);
 fb.fPowerOff=false;
 IOLockLock(&lock);fb.wakePresentLocked();IOLockUnlock(&lock);
 fb.vsyncFired(&fb,&timer);assert(fb.fShadowValid && fb.fDiffStored==648);
 // A callback during an armed tick coalesces; rearm sees pending work. A callback after idle explicitly arms.
 fb.fTickArmed=true;offer(fb,rec);before=arms;sweet_fb_rearm(&fb,&timer);assert(arms==before+1);
 fb.vsyncFired(&fb,&timer);fb.fDirty=false;sweet_fb_rearm(&fb,&timer);assert(!fb.fTickArmed);
 offer(fb,rec);assert(fb.fTickArmed);
 // Enabled notifications keep pacing even without dirty swaps; removing the last subscriber stops it.
 IOMobileFramebufferUserClient client;client.fFunc[5]=1;client.fPort=1;clients.objects.push_back(&client);
 fb.vsyncFired(&fb,&timer);fb.fDirty=false;client.notifications=0;fb.vsyncFired(&fb,&timer);assert(fb.fTickArmed && client.notifications==1);
 client.fPort=0;fb.vsyncFired(&fb,&timer);assert(!fb.fTickArmed && client.notifications==1);
 // A failed GPU copy replays through CPU and reinitializes the shadow after confirmed retirement.
 fb.fGpuPending=true;fb.fShadowValid=false;pollMode=-1;fb.vsyncFired(&fb,&timer);
 assert(fb.fGpuFailed==1 && !fb.fGpuPending && fb.fShadowValid && refs==recordRefs(fb));
 // The GPU ABI timing window is printed with, and only with, the 600-frame stats line.
 fb.fStatFrames=599;pollMode=1;gpuMode=0;fb.fDirty=true;fb.swap(rec);fb.vsyncFired(&fb,&timer);
 assert(abiStatsLogs==1 && abiSchedLogs==1 && !fb.fStatFrames);
 fb.swap(rec);fb.vsyncFired(&fb,&timer);assert(abiStatsLogs==1);
 assert(fuxi_unixnu_next_tick(10,100,50)==110);
 assert(fuxi_unixnu_next_tick(10,100,110)==210);
 for(int off=0;off<2;off++)for(int black=0;black<2;black++)for(int dirty=0;dirty<2;dirty++)
 for(int gpu=0;gpu<2;gpu++)for(int notify=0;notify<2;notify++)
  assert(!!fuxi_unixnu_tick_wanted(off,black,dirty,gpu,notify)==!!(gpu||notify||(off?black:dirty)));
 // The periodic production diagnostic includes actual import snapshots in
 // the right columns; values are cumulative, not reset with present counters.
 fb.fStatFrames=599;pollMode=1;gpuMode=0;
 offer(fb,rec);fb.vsyncFired(&fb,&timer);
 assert(!fb.fStatFrames && strstr(importStatsLog,"imports 1 mapped 16384 cache cleaned 64 skipped 128 invalidated 64 bytes cumulative"));
 { // TE opt-in admission and bounds use the production boot-arg parser.
 AppleCLCD tefb={};
 for(unsigned option:{0u,2u,~0u}) {
  teOption=option;unsigned oldLogs=logs;sweet_fb_te_configure(&tefb);
  assert(!tefb.fVsyncTE && logs==oldLogs);
 }
 teOption=1;
 teOffsetPresent=false;sweet_fb_te_configure(&tefb);
 assert(tefb.fVsyncTE && tefb.fTeOffset==7800000);
 teOffsetPresent=true;
 for(unsigned offset:{0u,7800u,16000u,16001u,~0u}) {
  teOffsetOption=offset;sweet_fb_te_configure(&tefb);
  assert(tefb.fVsyncTE && tefb.fTeOffset==(offset<=16000?offset:7800)*1000ull);
 }
 tefb.fLock=&lock;tefb.fClients=&clients;tefb.fVsync=&timer;
 tefb.fFlushTicks=1;tefb.fTickAbs=16667000;tefb.fScanout=true;
 client.fPort=1;scanOwned=true;teAvailable=true;
 // Exact phase deadlines at both measured rates, including counter wrap.
 for(uint64_t period:{16556000ull,8278000ull})for(uint64_t offset:{0ull,7800000ull,16000000ull}) {
  tefb.fTeOffset=offset;tefb.fTeSent=false;tefb.fTeLastNotify=0;
  tefb.fTeLocked=tefb.fTeFallback=0;memset(tefb.fTePhase,0,sizeof(tefb.fTePhase));
  tePeriod=period;
  for(unsigned i=0;i<600;i++) {
   teFrame=(65530+i)&0xffff;teEdge=1000000000ull+i*period;
   uint64_t deadline=teEdge+offset%period;
   uint64_t prior=tefb.fVsyncCount,notified=client.notifications;
   // With offset zero the first observation is necessarily just after TE.
   if(offset%period) {
    ticks=deadline-1;uint64_t now=ticks;
    assert(!tefb.teVsyncTick(now,false) && tefb.fTeNext==deadline);
   }
   ticks=deadline;tefb.vsyncFired(&tefb,&timer);
   assert(tefb.fVsyncCount==prior+1 && client.notifications==notified+1);
   assert(timer.next>ticks && timer.next<=ticks+period/16);
   ticks=deadline+1000;tefb.vsyncFired(&tefb,&timer);
   assert(tefb.fVsyncCount==prior+1 && client.notifications==notified+1);
  }
  assert(strstr(teStatsLog,"te-locked=600 fallback=0 phase_us"));
  unsigned locked,fallback,p50,p90;unsigned long long measured;
  assert(sscanf(teStatsLog,"SWEET FB: vsync te-locked=%u fallback=%u phase_us %u/%u period_us %llu",
    &locked,&fallback,&p50,&p90,&measured)==5);
  assert(locked==600 && !fallback && measured==period/1000);
  unsigned phase=(offset%period)/1000;
  assert(p50+64>=phase && p50<=phase+64 && p90==p50);
 }
 // A new TE just before the old deadline cannot supersede an armed tick.
 tefb.fTeSent=true;tefb.fTeValid=true;tefb.fTeSentFrame=99;
 tefb.fTeLastNotify=0;tefb.fTeOffset=16000000;tePeriod=16556000;
 teFrame=100;teEdge=50000000000ull;tefb.fTeNext=teEdge+16000000;
 ticks=tefb.fTeNext-1000;uint64_t pendingNow=ticks;
 assert(!tefb.teVsyncTick(pendingNow,false));
 uint64_t pending=tefb.fTeNext;teFrame=101;teEdge=pending-500;
 ticks=pending;pendingNow=ticks;
 assert(tefb.teVsyncTick(pendingNow,false) && tefb.fTeSentFrame==100);
 assert(tefb.fTeNext==teEdge+16000000);
 pendingNow=ticks;assert(!tefb.teVsyncTick(pendingNow,false));
 ticks=tefb.fTeNext;pendingNow=ticks;
 assert(tefb.teVsyncTick(pendingNow,false) && tefb.fTeSentFrame==101);
 // A late callback skips missed frames, without issuing a burst.
 uint64_t prior=tefb.fVsyncCount;
 teFrame+=4;teEdge+=tePeriod*4;ticks=teEdge+tefb.fTeOffset%tePeriod;
 tefb.vsyncFired(&tefb,&timer);assert(tefb.fVsyncCount==prior+1);
 tefb.vsyncFired(&tefb,&timer);assert(tefb.fVsyncCount==prior+1);
 // Loss/stall falls back to the old period with no immediate double tick.
 teAvailable=false;tefb.vsyncFired(&tefb,&timer);
 assert(!tefb.fTeValid && tefb.fVsyncCount==prior+1);
 assert(tefb.fNextTick==tefb.fTeLastNotify+tefb.fTickAbs);
 ticks=tefb.fNextTick;tefb.vsyncFired(&tefb,&timer);
 assert(tefb.fVsyncCount==prior+2 && tefb.fTeFallback==1);
 // No ownership/scanout: old continuous-timer path; no TE MMIO API call.
 scanOwned=false;tefb.fScanout=false;unsigned readsBefore=teReads;
 tefb.vsyncFired(&tefb,&timer);assert(teReads==readsBefore && tefb.fTeFallback==2);
 // Default off makes no TE call or log, and preserves old absolute deadlines.
 tefb.fVsyncTE=false;scanOwned=true;tefb.fScanout=true;
 ticks=tefb.fNextTick;prior=tefb.fVsyncCount;unsigned logsBefore=logs;
 tefb.vsyncFired(&tefb,&timer);
 assert(teReads==readsBefore && logs==logsBefore && tefb.fVsyncCount==prior+1);
 assert(timer.next==tefb.fNextTick && tefb.fNextTick>ticks);
 AppleCLCD stats={};
 for(unsigned i=0;i<600;i++)stats.teVsyncLog(ticks+i);
 assert(strstr(teStatsLog,"te-locked=0 fallback=600 phase_us 0/0 period_us 0"));
 scanOwned=false;client.fPort=0;
 puts("PASS: TE offset admission, 60/120Hz phase/count/wrap, duplicate/late suppression, logs and fallback/default path");
 }

 {
 // Actual production FIFO: order, pending retention, overflow, ready skips and timer wake.
 unsigned baselineRefs=refs;bool oldOwned=scanOwned;int oldPublish=scanPublish;
 scanOwned=true;scanAcquire=scanCpuAcquire=1;scanPublish=1;gpuMode=0;pollMode=1;
 AppleCLCD queued={};IOLock qlock;OSArray qclients;IOTimerEventSource qtimer;IOWorkLoop qloop;
 WakeCall wake={&AppleCLCD::writerWake,&queued};queued.fWriterWakeCall=&wake;queued.fWorkLoop=&qloop;
 queued.fLock=&qlock;queued.fClients=&qclients;queued.fVsync=&qtimer;
 queued.fTickAbs=100000;queued.fNextTick=ticks+queued.fTickAbs;queued.fFlushTicks=1;
 queued.fWidth=18;queued.fHeight=3;queued.fRowBytes=96;queued.fPixels=(uintptr_t)pixels;
 queued.fScanout=queued.fUnixnuIdle=queued.fConsolePaused=true;
 uint8_t frame[SWEET_FB_SWAP_REC_SIZE];memcpy(frame,rec,sizeof(frame));
 for(unsigned id=1;id<=3;id++){put32(frame,0x50,id);offer(queued,frame);}
 assert(queued.fPendingCount==3 && !queued.fStatDropped);
 for(unsigned id=1;id<=3;id++) {
  queued.vsyncFired(&queued,&qtimer);
  assert(get32(queued.fLast.bytes,0x50)==id && queued.fPendingCount==3-id);
  assert(refs==baselineRefs+recordRefs(queued));
 }
 unsigned complete=queued.fStatFrames,publications=scanPublications;
 surface.gpuWriters=2;put32(frame,0x50,4);offer(queued,frame);
 for(unsigned i=0;i<4;i++)queued.vsyncFired(&queued,&qtimer);
 assert(queued.fPendingCount==1 && queued.fDirty && !queued.fStatDropped);
 assert(queued.fStatFrames==complete && scanPublications==publications && queued.fQueuedDefers==4);
 surface.gpuWriters--;queued.vsyncFired(&queued,&qtimer);
 assert(queued.fPendingCount==1 && queued.fQueuedDefers==5);
 // Unlock only queues a coalesced call; the gated worker presents before any timer tick.
 IOMobileFramebufferUserClient subscriber;subscriber.fFunc[5]=1;subscriber.fPort=1;qclients.objects.push_back(&subscriber);
 queued.fNextTick=ticks+queued.fTickAbs;uint64_t nextTick=queued.fNextTick,count=queued.fVsyncCount;
 unsigned oldArms=arms;surface.gpuWriters=0;surface.gpuReadyTime=mach_absolute_time();AppleCLCD::writerRetired(&queued);
 assert(wake.pending && arms==oldArms && queued.fStatFrames==complete && scanPublications==publications);
 uint64_t wakeQueued=queued.fWriterWakeQueued;
 ticks+=3000;AppleCLCD::writerRetired(&queued);assert(queued.fWriterWakeQueued==wakeQueued);
 uint64_t dispatchDelay=ticks-wakeQueued;qloop.gateDelay=2000;runWake(wake);qloop.gateDelay=0;
 assert(queued.fWakeN[0]==1 && queued.fWakeN[1]==1 && queued.fWakeSum[0]==dispatchDelay && queued.fWakeSum[1]>=2000);
 assert(!queued.fWriterWakeQueued);
 assert(!wake.pending && arms==oldArms+1 && qtimer.next==nextTick && !qloop.gated);
 assert(queued.fStage[1]==surface.gpuReadyTime && queued.fStage[2]>=surface.gpuReadyTime);
 assert(get32(queued.fLast.bytes,0x50)==4 && !queued.fDirty && queued.fStatFrames==complete+1);
 assert(queued.fVsyncCount==count && !subscriber.notifications && queued.fNextTick==nextTick);
 subscriber.fPort=0;
 // Four waiting records drop precisely the oldest, even when it is still pending.
 surface.gpuWriters=1;
 for(unsigned id=5;id<=8;id++){put32(frame,0x50,id);offer(queued,frame);}
 assert(queued.fPendingCount==3 && queued.fOverflowDrops==1 && queued.fStatDropped==1);
 assert(get32(queued.fPending[0].bytes,0x50)==6);
 queued.vsyncFired(&queued,&qtimer);assert(queued.fPendingCount==3 && queued.fOverflowDrops==1);
 surface.gpuWriters=0;
 for(unsigned id=6;id<=8;id++){queued.vsyncFired(&queued,&qtimer);assert(get32(queued.fLast.bytes,0x50)==id);}
 // A ready newer surface can skip the unready head, but cannot reorder ready heads.
 SweetSurface newer=surface;SweetSurface *savedSecond=inputSurface2;inputSurface2=&newer;
 surface.gpuWriters=1;put32(frame,0x50,9);offer(queued,frame);
 put32(frame,0x54,2);put32(frame,0x50,10);offer(queued,frame);
 queued.vsyncFired(&queued,&qtimer);
 assert(!queued.fDirty && get32(queued.fLast.bytes,0x50)==10 && queued.fReadySkips==1 && queued.fStatDropped==2);
 surface.gpuWriters=0;inputSurface2=savedSecond;put32(frame,0x54,1);
 // A GPU offer is neither a completed swap nor permission to reuse a ready back buffer.
 put32(frame,0x50,11);offer(queued,frame);gpuMode=2;complete=queued.fStatFrames;
 queued.vsyncFired(&queued,&qtimer);assert(queued.fGpuPending && queued.fStatFrames==complete);
 pollMode=0;queued.vsyncFired(&queued,&qtimer);assert(queued.fStatFrames==complete);
 pollMode=1;scanPublish=2;queued.vsyncFired(&queued,&qtimer);
 assert(queued.fPublicationPending && scanBackReady && queued.fStatFrames==complete);
 put32(frame,0x50,12);offer(queued,frame);unsigned offers=surfaceCopies;
 queued.vsyncFired(&queued,&qtimer);
 assert(surfaceCopies==offers && queued.fPendingCount==1 && queued.fStatFrames==complete);
 scanRetryResult=1;scanPublish=1;queued.vsyncFired(&queued,&qtimer);
 assert(queued.fStatFrames==complete+1 && queued.fGpuPending && get32(queued.fLast.bytes,0x50)==12);
 scanRetryResult=0;queued.vsyncFired(&queued,&qtimer);assert(queued.fStatFrames==complete+2);
 // Failed render-copy replay uses that exact record, ahead of a newer queued swap.
 put32(frame,0x50,13);offer(queued,frame);queued.vsyncFired(&queued,&qtimer);
 put32(frame,0x50,14);offer(queued,frame);pollMode=-1;gpuMode=0;complete=queued.fStatFrames;
 queued.vsyncFired(&queued,&qtimer);
 assert(queued.fStatFrames==complete+1 && get32(queued.fLast.bytes,0x50)==13 && queued.fPendingCount==1);
 pollMode=1;queued.vsyncFired(&queued,&qtimer);assert(get32(queued.fLast.bytes,0x50)==14);
 // An unreadable failed-copy replay also yields to a newer ready record.
 put32(frame,0x50,15);offer(queued,frame);gpuMode=2;queued.vsyncFired(&queued,&qtimer);
 inputSurface2=&newer;put32(frame,0x54,2);put32(frame,0x50,16);offer(queued,frame);
 surface.gpuWriters=1;pollMode=-1;gpuMode=0;queued.vsyncFired(&queued,&qtimer);
 assert(get32(queued.fLast.bytes,0x50)==16 && !queued.fReplay && !queued.fPendingCount && queued.fReadySkips==2);
 surface.gpuWriters=0;pollMode=1;inputSurface2=savedSecond;put32(frame,0x54,1);
 // Swaps during copying do not hold fLock; a deferred older offer is the overflow victim.
 put32(frame,0x50,21);offer(queued,frame);arrivingFB=&queued;memcpy(arrivingRecord,frame,sizeof(frame));
 duringCopy=arrivals;gpuMode=3;queued.vsyncFired(&queued,&qtimer);duringCopy=nullptr;
 assert(queued.fPendingCount==3 && get32(queued.fPending[0].bytes,0x50)==22);
 assert(queued.fOverflowDrops==2 && queued.fStatDropped==4 && queued.fScanoutDefers);
 gpuMode=0;
 for(unsigned id=22;id<=24;id++){queued.vsyncFired(&queued,&qtimer);assert(get32(queued.fLast.bytes,0x50)==id);}
 queued.fStatFrames=599;put32(frame,0x50,25);offer(queued,frame);queued.vsyncFired(&queued,&qtimer);
 assert(!queued.fStatFrames && strstr(frameStatsLog,"fifo_overflow=2 ready_skips=2 ownership_defers="));
 // Every not-shown frame above has exactly one path: 5 fifo overflow, 9 not ready, 15 failed copy, 21 refused with a full queue.
 assert(strstr(unshownLog,"stale=0 not_ready=1 superseded=0 failed_copy=1 fifo=1 refused_full=1 replaced=0 lost=0 sum=4 of 4 not shown;"));
 assert(strstr(unshownLog,"stop=0 rejected=0; counted shown spin_started=0 spin_race=0") && !queued.fUnshown[AppleCLCD::UnFifo]);
 assert(strstr(frameStatsLog,"queued_pending="));
 // Black/held/idle paths ignore release wakes; no display work or timer churn.
 for(unsigned mode=0;mode<3;mode++) {
  queued.fPowerOff=mode==0;queued.fScanoutHeld=mode==1;queued.fDirty=mode!=2;
  oldArms=arms;AppleCLCD::writerRetired(&queued);runWake(wake);assert(arms==oldArms);
 }
 IOLockLock(queued.fLock);queued.releaseRecord(queued.fLast);queued.fLastValid=false;IOLockUnlock(queued.fLock);
 assert(refs==baselineRefs);
 scanOwned=oldOwned;scanPublish=oldPublish;
 puts("PASS: production FIFO order, pending/active exclusion, final-retire wake, oldest overflow, ready skip, publication accounting, exact replay and concurrent swaps");
 }
 {
 // Actual SwapWait contract and per-record display use holds.
 AppleCLCD held={};IOLock hlock;OSArray hclients;IOTimerEventSource htimer;
 held.fLock=&hlock;held.fClients=&hclients;held.fVsync=&htimer;held.fScanout=true;
 held.fTickAbs=100;held.fFlushTicks=1;held.fConsolePaused=true;
 held.fWidth=18;held.fHeight=3;held.fRowBytes=96;held.fPixels=(uintptr_t)pixels;
 scanOwned=true;scanPublish=1;scanAcquire=1;gpuMode=0;pollMode=1;
 unsigned baseUses=surface.displayUses,baseRefs=refs;
 uint8_t frame[SWEET_FB_SWAP_REC_SIZE];memcpy(frame,rec,sizeof(frame));
 held.fSwapID=1000;put32(frame,0x50,1);
 assert(held.swap(frame)==0 && surface.displayUses==baseUses+1);
 assert(held.swapWait(1,0x80000000u,0)==kIOReturnBusy && held.swapWait(1,0x80000001u,0)==kIOReturnBusy);
 assert(held.swap(frame)!=0 && held.swapWait(2,0x80000000u,0)==kIOReturnNotFound);
 unsigned slept=waitSleeps;assert(held.swapWait(1,0,7)==kIOReturnTimeout && waitSleeps==slept+1);
 interruptWait=true;assert(held.swapWait(1,0,7)==kIOReturnAborted);interruptWait=false;
 held.presentNext();assert(surface.displayUses==baseUses && held.fLast.shown);
 assert(held.swapWait(1,0x80000000u,0)==0 && held.swapWait(1,0x80000001u,0)==0);
 // A writer that was already active at swap is not reuse; only a newer lease is.
 surface.gpuWriters=1;put32(frame,0x50,2);assert(held.swap(frame)==0);held.presentNext();
 assert(!held.fReuseBeforeShow && held.swapWait(2,0x80000000u,0)==kIOReturnBusy);
 surface.gpuWriterSerial++;held.presentNext();
 assert(held.fReuseBeforeShow==1 && held.fPendingCount==1 && surface.displayUses==baseUses+1);
 surface.gpuWriterSerial++;surface.gpuWriterEpoch++;held.presentNext();
 assert(held.fReuseBeforeShow==1 && !held.fPendingCount && surface.displayUses==baseUses);
 assert(held.swapWait(2,0x80000000u,0)==0 && held.swapWait(2,0x80000001u,0)==kIOReturnAborted);
 surface.gpuWriters=0;
 // Overflow and ready skip release every layer's hold, counting a record once.
 put32(frame,0x50,3);assert(held.swap(frame)==0);surface.gpuWriterSerial++;surface.gpuWriterEpoch++;
 for(unsigned id=4;id<=6;id++){put32(frame,0x50,id);assert(held.swap(frame)==0);}
 assert(held.fReuseBeforeShow==2 && held.fOverflowDrops==1 && surface.displayUses==baseUses+3);
 assert(held.swapWait(3,0x80000000u,0)==0 && held.swapWait(3,0x80000001u,0)==kIOReturnAborted);
 for(unsigned i=0;i<3;i++)held.presentNext();assert(surface.displayUses==baseUses);
 // Source consumption precedes START only after successful copy retirement.
 put32(frame,0x50,7);assert(held.swap(frame)==0);gpuMode=2;held.presentNext();
 pollMode=0;held.presentStep(true);assert(surface.displayUses==baseUses+1 && held.swapWait(7,0x80000000u,0)==kIOReturnBusy);
 unsigned beforePublication=held.fStatFrames;pollMode=1;scanPublish=2;held.presentStep(true);
 assert(held.fPublicationPending && surface.displayUses==baseUses && held.swapWait(7,0x80000001u,0)==kIOReturnBusy);
 assert(held.swapWait(7,0x80000000u,0)==0 && held.swapWait(0,0x80000000u,0)==0);
 assert(held.fWaitEarly==2 && held.fWaitLatest==1 && held.fWaitPoll[0] && held.fWaitPoll[1] && held.fOutstandingMax==3);
 assert(held.swapWait(0,0x80000002u,0)==0 && held.swapWait(0,0x80000003u,0)==kIOReturnBusy);
 // Reuse the consumed page and queue another frame while the captured image waits for TE.
 surface.gpuWriterSerial++;surface.gpuWriterEpoch++;surface.gpuWriters=1;
 put32(frame,0x50,15);assert(held.swap(frame)==0 && held.fPendingCount==1);
 put32(frame,0x50,71);assert(held.swap(frame)!=0); // consumed/unshown history cannot be evicted
 assert(held.swapWait(0,0x80000000u,0)==kIOReturnBusy && held.swapWait(7,0x80000000u,0)==0);
 held.presentStep(true);assert(held.fPublicationPending && held.fPendingCount==1 && held.fStatFrames==beforePublication);
 scanPublish=1;scanRetryResult=1;held.presentStep(true);scanRetryResult=0;
 assert(held.fLast.shown && !held.fLast.stale && held.fPendingCount==1);
 assert(held.swapWait(7,0x80000001u,0)==0 && held.swapWait(15,0x80000000u,0)==kIOReturnBusy);
 surface.gpuWriters=0;IOLockLock(held.fLock);held.releaseRecord(held.fPending[0]);held.fPendingCount=0;held.fDirty=false;IOLockUnlock(held.fLock);
 assert(surface.displayUses==baseUses && held.swapWait(7,0x80000001u,0)==0);
 // Later writers on a shown record do not increment reuse_before_show.
 surface.gpuWriterSerial++;held.fStatFrames=599;held.presentStats(1);
 assert(held.fReuseBeforeShow==2 && strstr(frameStatsLog,"reuse_before_show=2"));
 // Sleeping waits drop the lock, and a completed copy wakes them.
 gpuMode=0;put32(frame,0x50,8);assert(held.swap(frame)==0);waitFB=&held;waitHook=finishWait;
 assert(held.swapWait(8,0,100)==0);waitHook=nullptr;
 assert(held.swapWait(0,0x80000002u,0)==0 && held.swapWait(0,4,0)==kIOReturnBadArgument);
 // Repeated layers on the same surface own and release matched independent uses.
 put32(frame,0x50,14);put32(frame,0x104,3);put32(frame,0x58,1);
 assert(held.swap(frame)==0 && surface.displayUses==baseUses+2);held.presentNext();
 assert(surface.displayUses==baseUses && held.swapWait(14,0x80000001u,0)==0);put32(frame,0x104,1);
 // History cannot evict a live record; missing/expired IDs never return success.
 put32(frame,0x50,9);assert(held.swap(frame)==0);put32(frame,0x50,73);assert(held.swap(frame)!=0);
 assert(held.swapWait(73,0x80000000u,0)==kIOReturnNotFound);
 holdFails=true;put32(frame,0x50,10);assert(held.swap(frame)==kIOReturnNoResources);holdFails=false;
 assert(surface.displayUses==baseUses+1);
 IOLockLock(held.fLock);held.stopRecords();IOLockUnlock(held.fLock);
 assert(surface.displayUses==baseUses && refs==baseRefs && held.swapWait(9,0x80000000u,0)==0);
 // Held scanout retires the DMA before dropping active and waiting references.
 put32(frame,0x50,11);assert(held.swap(frame)==0);gpuMode=2;held.presentNext();
 put32(frame,0x50,12);assert(held.swap(frame)==0);held.fScanoutHeld=true;pollMode=0;
 held.presentStep(true);assert(surface.displayUses==baseUses+2);
 pollMode=1;scanPublish=-1;held.presentStep(true);
 assert(surface.displayUses==baseUses && refs==baseRefs && held.swapWait(11,0x80000000u,0)==kIOReturnNotReady);
 held.fScanoutHeld=false;put32(frame,0x50,13);assert(held.swap(frame)==0);gpuMode=2;held.presentNext();
 assert(surface.displayUses==baseUses+1);IOLockLock(held.fLock);held.stopRecords();IOLockUnlock(held.fLock);
 assert(surface.usePoisoned && surface.displayUses==baseUses && refs==baseRefs);
 held.fStatFrames=0;scanPublish=0;scanOwned=false;gpuMode=0;
 puts("PASS: production reuse counter, stale admission refusal, display use accounting, drop/show/stop/DMA retirement, bounded SwapWait/history, wake/timeout/interrupt");
 }
 {
 OSArray triClients;AppleCLCD tri={};tri.fClients=&triClients;tri.fLock=&lock;tri.fScanout=true;tri.fConsolePaused=true;tri.fFlushTicks=1;
 tri.fWidth=18;tri.fHeight=3;tri.fRowBytes=96;tri.fPixels=(uintptr_t)pixels;
 tri.fSwapID=1000;scanOwned=scanDouble=scanTriple=true;scanAcquire=1;scanPublish=2;scanRetryResult=0;gpuMode=2;pollMode=1;
 unsigned baseUses=surface.displayUses,baseRefs=refs,blocked=tri.fStageBlocks[0];
 uint8_t frame[SWEET_FB_SWAP_REC_SIZE];memcpy(frame,rec,sizeof(frame));
 put32(frame,0x50,20);assert(!tri.swap(frame));tri.presentStep(true);
 assert(tri.fGpuPending && surface.displayUses==baseUses+1);
 put32(frame,0x50,21);assert(!tri.swap(frame));pollMode=0;tri.presentStep(true);
 assert(tri.swapWait(20,0x80000000u,0)==kIOReturnBusy && tri.fPendingCount==1);
 pollMode=1;tri.presentStep(true); // retire 20, leave it ready, submit 21 in the same step
 assert(tri.fGpuPending && tri.fPublicationPending && tri.fPublicationID==20 && !tri.fPendingCount);
 assert(surface.displayUses==baseUses+1 && tri.swapWait(20,0x80000000u,0)==0);
 assert(tri.swapWait(20,0x80000001u,0)==kIOReturnBusy && tri.swapWait(21,0x80000000u,0)==kIOReturnBusy);
 pollMode=0;tri.presentStep(true);assert(tri.fPublicationID==20 && !tri.fStatDropped);
 pollMode=1;tri.presentStep(true); // replace only at 21 retirement
 assert(tri.fPublicationID==21 && !tri.fGpuPending && tri.fStatDropped==1 && surface.displayUses==baseUses);
 assert(tri.swapWait(20,0x80000000u,0)==0 && tri.swapWait(20,0x80000001u,0)==kIOReturnAborted);
 assert(tri.swapWait(21,0x80000000u,0)==0 && tri.swapWait(21,0x80000001u,0)==kIOReturnBusy);
 assert(tri.swapWait(0,0x80000002u,0)==0 && tri.swapWait(0,0x80000003u,0)==kIOReturnBusy);
 // A failed newer copy preserves the ready swap and its original stage stamps.
 uint64_t savedStage[6];memcpy(savedStage,tri.fPublicationStage,sizeof(savedStage));
 put32(frame,0x50,22);assert(!tri.swap(frame));tri.presentStep(true);assert(tri.fGpuPending);
 pollMode=-1;tri.presentStep(false);assert(tri.fReplay && tri.fPublicationID==21);
 assert(tri.swapWait(22,0x80000000u,0)==kIOReturnBusy && surface.displayUses==baseUses+1);
 pollMode=1;scanRetryResult=1;tri.presentStep(false);scanRetryResult=0;
 assert(!tri.fPublicationPending && tri.fStatFrames==1 && tri.swapWait(21,0x80000001u,0)==0);
 for(unsigned i=0;i<4;i++)assert(tri.fStageSum[i]==savedStage[i+1]-savedStage[i]);
 assert(tri.fStageBlocks[0]==blocked && tri.swapWait(22,0x80000000u,0)==kIOReturnBusy);
 scanDouble=scanTriple=false;gpuMode=0;scanPublish=1;tri.presentStep(true); // exact CPU replay
 assert(tri.swapWait(22,0x80000001u,0)==0 && surface.displayUses==baseUses);
 // Redrawing a previously shown ID must not revoke its successful mode-1 status.
 scanDouble=scanTriple=true;gpuMode=2;scanPublish=2;tri.fReplay=tri.fDirty=true;
 tri.presentStep(true);assert(tri.fGpuPending && surface.displayUses==baseUses+1);
 tri.presentStep(true);assert(tri.fPublicationID==22 && tri.swapWait(22,0x80000001u,0)==0);
 put32(frame,0x50,25);assert(!tri.swap(frame));tri.presentStep(true);tri.presentStep(true);
 assert(tri.fPublicationID==25 && tri.swapWait(22,0x80000001u,0)==0);
 IOLockLock(tri.fLock);tri.stopRecords();IOLockUnlock(tri.fLock);assert(refs==baseRefs);
 assert(tri.swapWait(25,0x80000000u,0)==0 && tri.swapWait(25,0x80000001u,0)==kIOReturnAborted);
 // Stopping with a ready image and a newer live DMA retains source quarantine.
 scanDouble=scanTriple=true;gpuMode=2;scanPublish=2;
 put32(frame,0x50,23);assert(!tri.swap(frame));tri.presentStep(true);
 put32(frame,0x50,24);assert(!tri.swap(frame));tri.presentStep(true);
 assert(tri.fPublicationID==23 && tri.fGpuPending && surface.displayUses==baseUses+1);
 tri.fScanoutHeld=true;pollMode=0;tri.presentStep(true);assert(tri.fPublicationPending);
 pollMode=1;tri.presentStep(true);assert(!tri.fPublicationPending && surface.displayUses==baseUses && refs==baseRefs);
 assert(tri.fSwapStatus[23%SWEET_FB_SWAP_HISTORY].state==AppleCLCD::SwapDropped);
 // Low-level stop can clear ready first; pending metadata must still wake cleanup.
 tri.fScanoutHeld=false;tri.fPublicationPending=true;tri.fPublicationID=23;scanBackReady=false;
 IOLockLock(tri.fLock);assert(tri.presentWantedLocked());IOLockUnlock(tri.fLock);
 scanRetryResult=-1;tri.presentStep(true);scanRetryResult=0;
 assert(tri.fScanoutHeld && !tri.fPublicationPending);
 scanDouble=scanTriple=scanOwned=scanBackReady=false;scanPublish=0;gpuMode=0;
 puts("PASS: triple same-step copy admission, source holds/mode0 at retire, newest-ready mode1 drop, failed-copy replay, saved publication stages, CPU and stop");
 }
 {
 // fuxigpuncimport 8/16/32: frame log fates and summary, due-surface publication, superseded ready swap dropped before its copy.
 OSArray logClients;AppleCLCD lf={};lf.fClients=&logClients;lf.fLock=&lock;lf.fScanout=true;lf.fConsolePaused=true;lf.fFlushTicks=1;
 lf.fWidth=18;lf.fHeight=3;lf.fRowBytes=96;lf.fPixels=(uintptr_t)pixels;lf.fTickAbs=8333;
 lf.fSwapID=3000;scanOwned=scanDouble=scanTriple=true;scanAcquire=1;scanPublish=1;scanRetryResult=0;gpuMode=2;pollMode=1;
 unsigned baseRefs=refs;
 uint8_t frame[SWEET_FB_SWAP_REC_SIZE];memcpy(frame,rec,sizeof(frame));
 static AppleCLCD::FlogEntry flogBuf[SWEET_FB_FLOG];lf.fFlog=flogBuf;lf.fCopyFirst=true;lf.fFlogState=1;lf.fFlogSwaps=SWEET_FB_FLOG_WARMUP-2;
 put32(frame,0x50,2001);assert(!lf.swap(frame) && lf.fFlogState==1 && !lf.fFlogN); // last warmup swap
 assert(dueN==1 && dueIds[0]==surface.id);
 lf.presentStep(true);assert(lf.fGpuPending && dueN==0); // copy queued: no longer due
 lf.presentStep(true);assert(!lf.fGpuPending && lf.fSwapStatus[2001%SWEET_FB_SWAP_HISTORY].state==AppleCLCD::SwapShown);
 copyStamps.s2s_us=6800;copyStamps.blit_us=560;copyStamps.ahead=1;copyStamps.buffer=2;
 unsigned beforeLines=flogLines;
 for(unsigned i=0;i<SWEET_FB_FLOG;i++) {
  copyStamps.te=(uint32_t)(10+2*i);copyStamps.parked=i%4==0; // bit 27: every fourth copy released a parked client job
  put32(frame,0x50,2002+i);assert(!lf.swap(frame));
  if(i==0)assert(lf.fFlogState==2 && lf.fFlogN==1 && lf.fFlog[0].id==2002 && lf.fFlog[0].surface==surface.id);
  if(i==5) { scanPublish=2;lf.presentStep(true);lf.presentStep(true);assert(lf.fPublicationPending);scanPublish=1;continue; } // waits for START
  lf.presentStep(true);assert(lf.fGpuPending);lf.presentStep(true); // the retire of 6 replaces 5, then 6 STARTs
 }
 assert(lf.fFlog[0].fate=='S' && lf.fFlog[0].submit && lf.fFlog[0].fence>=lf.fFlog[0].submit && lf.fFlog[0].start && lf.fFlog[0].ahead==1 && lf.fFlog[0].buf==2 && lf.fFlog[0].s2s==6800);
 assert(lf.fFlog[5].fate=='P' && lf.fFlog[6].fate=='S' && lf.fFlogState==3 && flogLines==beforeLines+SWEET_FB_FLOG);
 assert(strstr(flogEndLog,"shown=127 dropped=0 replaced_waiting_start=1 copied=128 copied_not_shown=1 copies_behind_client_job=128 submit_to_start_over_3ms=128"));
 assert(strstr(flogEndLog,"start_te_gaps 1/2/3+=0/125/1"));
 assert(lf.fFlog[0].pk==1 && lf.fFlog[1].pk==0 && strstr(flogEndLog,"copies_releasing_parked_job=32"));copyStamps.parked=0;
 // Drop path letters: 5 was replaced while still ready in the kernel (offer fate 2).
 assert(lf.fFlog[5].path=='p' && !lf.fFlog[6].path && !lf.fFlog[0].path && lf.fUnshown[AppleCLCD::UnReplaced]==1);
 assert(strstr(flogEndLog,"drop_paths s/r/u/f/o/c/p/l/h/a/A=0/0/0/0/0/0/1/0/0/0/0"));
 // Bit 32: two ready swaps behind a copy in flight; the older is dropped before its copy.
 lf.fSkipSuperseded=true;
 put32(frame,0x50,2200);assert(!lf.swap(frame));lf.presentStep(true);assert(lf.fGpuPending && dueN==0);
 put32(frame,0x50,2201);assert(!lf.swap(frame));put32(frame,0x50,2202);assert(!lf.swap(frame));
 assert(dueN==2 && lf.fPendingCount==2);
 pollMode=0;lf.presentStep(true);assert(lf.fPendingCount==2 && lf.fGpuPending);
 pollMode=1;unsigned copiesBefore=surfaceCopies;lf.presentStep(true); // 2200 retires and STARTs, then 2202 is copied, 2201 dropped
 assert(surfaceCopies==copiesBefore+1 && lf.fSupersededSkips==1 && !lf.fPendingCount && lf.fGpuPending && dueN==0);
 assert(lf.swapWait(2201,0x80000001u,0)==kIOReturnAborted && lf.swapWait(2202,0x80000001u,0)==kIOReturnBusy);
 lf.presentStep(true);assert(lf.swapWait(2202,0x80000001u,0)==0);
 // Not ready (writer active): no skip, both wait; once ready the newest goes.
 put32(frame,0x50,2203);assert(!lf.swap(frame));lf.presentStep(true);assert(lf.fGpuPending);
 put32(frame,0x50,2204);assert(!lf.swap(frame));put32(frame,0x50,2205);assert(!lf.swap(frame));
 surface.gpuWriters=1;lf.presentStep(true);assert(!lf.fGpuPending && lf.fPendingCount==2 && lf.fSupersededSkips==1 && dueN==2);
 surface.gpuWriters=0;lf.presentStep(true);assert(lf.fGpuPending && !lf.fPendingCount && lf.fSupersededSkips==2);
 lf.presentStep(true);assert(lf.swapWait(2205,0x80000001u,0)==0 && lf.swapWait(2204,0x80000001u,0)==kIOReturnAborted);
 assert(lf.fUnshown[AppleCLCD::UnSuperseded]==2 && !lf.fUnshown[AppleCLCD::UnNotReady]);
 // A frame waiting for START that the TE spin thread STARTed while the next copy ran is shown, not replaced (take before the offer);
 // STARTed between the take and the offer (offer fate 1) it is the race path; waiting with no kernel frame ready (fate 0) it is lost.
 unsigned dropsBefore=lf.fStatDropped,framesBefore=lf.fStatFrames;
 scanPublish=2;put32(frame,0x50,2210);assert(!lf.swap(frame));lf.presentStep(true);lf.presentStep(true);assert(lf.fPublicationPending && lf.fPublicationID==2210);
 scanPublish=1;put32(frame,0x50,2211);assert(!lf.swap(frame));lf.presentStep(true);assert(lf.fGpuPending);
 scanAsync=1;lf.presentStep(true);
 assert(!lf.fPublicationPending && !scanAsync && lf.swapWait(2210,0x80000001u,0)==0 && lf.swapWait(2211,0x80000001u,0)==0);
 assert(lf.fStatDropped==dropsBefore && lf.fStatFrames==framesBefore+2 && lf.fUnshown[AppleCLCD::UnStarted]==1);
 scanPublish=2;put32(frame,0x50,2212);assert(!lf.swap(frame));lf.presentStep(true);lf.presentStep(true);assert(lf.fPublicationID==2212);
 scanPublish=1;put32(frame,0x50,2213);assert(!lf.swap(frame));lf.presentStep(true);scanAsyncLate=1;lf.presentStep(true);
 assert(!lf.fPublicationPending && lf.swapWait(2212,0x80000001u,0)==0 && lf.fStatDropped==dropsBefore && lf.fUnshown[AppleCLCD::UnStartedRace]==1);
 scanPublish=2;put32(frame,0x50,2214);assert(!lf.swap(frame));lf.presentStep(true);lf.presentStep(true);assert(lf.fPublicationID==2214);
 scanBackReady=false;scanPublish=1;put32(frame,0x50,2215);assert(!lf.swap(frame));lf.presentStep(true);lf.presentStep(true);
 assert(lf.swapWait(2214,0x80000001u,0)==kIOReturnAborted && lf.swapWait(2215,0x80000001u,0)==0 && lf.fStatDropped==dropsBefore+1);
 assert(lf.fUnshown[AppleCLCD::UnLost]==1 && lf.fUnshown[AppleCLCD::UnReplaced]==1);
 // Bit 28: the log starts at the first burst of not-shown frames (3 in a 60-swap window after the 300-swap floor), not after the warmup.
 lf.fFlogState=1;lf.fFlogN=0;lf.fFlogSwaps=SWEET_FB_FLOG_TRIGGER_FLOOR+1;lf.fFlogTrigger=true;lf.fTrigSwaps=SWEET_FB_FLOG_TRIGGER_SWAPS-1;lf.fTrigDrops=2;
 surface.gpuWriters=1;put32(frame,0x50,2220);assert(!lf.swap(frame));
 assert(lf.fFlogState==1 && !lf.fTrigSwaps && !lf.fTrigDrops); // a window with only two ended
 for(unsigned id=2221;id<=2224;id++){put32(frame,0x50,id);assert(!lf.swap(frame));}
 assert(lf.fFlogState==1 && lf.fTrigDrops==2 && lf.fUnshown[AppleCLCD::UnFifo]==2);
 put32(frame,0x50,2225);assert(!lf.swap(frame));
 assert(lf.fFlogState==2 && lf.fFlogN==1 && lf.fFlog[0].id==2225 && lf.fTrigDrops==3);
 IOLockLock(lf.fLock);lf.stopRecords();IOLockUnlock(lf.fLock);assert(refs==baseRefs && dueN==0);
 assert(lf.fFlog[0].path=='h' && lf.fUnshown[AppleCLCD::UnStop]==3);
 surface.gpuWriters=0;lf.fFlogState=3;lf.fFlogTrigger=false;
 scanDouble=scanTriple=scanOwned=scanBackReady=false;scanPublish=0;gpuMode=0;copyStamps={};
 static_assert(sizeof(AppleCLCD)<=14*1024,"the kernel object (with its IOService base) is limited to 16 KB by kalloc_type; large buffers go on the heap"); // CI 41046931721
 puts("PASS: frame log (bit 8): warmup, per-frame fates S/P and drop paths, GPU detail, one-shot summary and TE gaps; spin-STARTed waiting frames shown, lost/replaced split; not-shown trigger (bit 28); due surfaces (bit 16) follow the queue and the copy; superseded ready swap dropped before its copy (bit 32), not while a writer holds it");
 }
 {
 // Exact production stage aggregation, mach-to-us conversion, 600-frame cadence and reset.
 AppleCLCD staged={};staged.fLock=&lock;staged.fScanout=true;staged.fStageGpu=true;
 bool previousOwned=scanOwned;scanOwned=true;
 unsigned beforeLogs=logs;
 for(unsigned i=0;i<1200;i++) {
  uint64_t b=100000000ull+i*100000;
  staged.fStage[0]=b;staged.fStage[1]=b+(i==599?3000:1000);staged.fStage[2]=staged.fStage[1]+2000;
  scanTimes.submitted=staged.fStage[2]+3000;scanTimes.fence=scanTimes.submitted+4000;scanTimes.previous_start=i?scanTimes.start:0;scanTimes.start=scanTimes.fence+5000;
  staged.fStage[3]=scanTimes.submitted;staged.fStage[4]=scanTimes.fence;
  staged.fWakeSum[0]+=6000;staged.fWakeMax[0]=6000;staged.fWakeN[0]++;
  staged.fWakeSum[1]+=2000;staged.fWakeMax[1]=2000;staged.fWakeN[1]++;
  staged.fStageBlocks[0]++;staged.fStageBlocks[1]+=2;staged.fStageBlocks[2]+=3;
  staged.fStageCallbacks[0]+=2;staged.fStageCallbacks[1]+=3;staged.fStageCallbacks[2]++;
  staged.presentStats(12000);
  if(i==598)assert(logs==beforeLogs);
  if(i==599) {
   assert(strstr(stageStatsLog,"swap_ready_us=1/3 ready_present_us=2/2 present_submit_us=3/3 submit_fence_us=4/4 fence_start_us=5/5 start_start_us=100/102"));
   assert(strstr(stageStatsLog,"samples=600/600/600/600/600/599 callbacks_vsync=1200 poll=1800 wake=600 callbacks_per_shown_x1000=2000/3000/1000"));
   assert(strstr(pacingStatsLog,"wake_dispatch_us=6/6 wake_gate_us=2/2 wake_samples=600/600 blocked_publication=600 acquire=1200 ring=1800"));
   for(unsigned j=0;j<2;j++)assert(!staged.fWakeN[j] && !staged.fWakeSum[j] && !staged.fWakeMax[j]);
   for(unsigned j=0;j<3;j++)assert(!staged.fStageBlocks[j]);
   for(unsigned j=0;j<6;j++)assert(!staged.fStageN[j] && !staged.fStageSum[j] && !staged.fStageMax[j]);
   assert(!staged.fStatFrames && !staged.fStageCallbacks[0] && !staged.fStageCallbacks[1] && !staged.fStageCallbacks[2]);
  }
 }
 assert(strstr(stageStatsLog,"samples=600/600/600/600/600/600 callbacks_vsync=1200 poll=1800 wake=600 callbacks_per_shown_x1000=2000/3000/1000"));
 // Missing or reversed stamps are omitted, never underflow into large latency.
 memset(staged.fStage,0,sizeof(staged.fStage));staged.fStageGpu=false;staged.fStage[0]=10;staged.fStage[1]=5;
 staged.stageStats(1);assert(!staged.fStageN[0]);scanOwned=previousOwned;
 puts("PASS: stage stamps, exact avg/max us, missing/reversed samples, callbacks, 600-shown cadence and window reset");
 }
 puts("PASS: actual framebuffer present/tick/swap: demand wake/idle, GPU retirement/fallback, blanking and shadow invalidation");
}
'''


def kernel_syntax(buildtree):
    """Optional real 8796 header check using saved flags; never run its make/scripts."""
    surface_header = (ROOT / 'xnu/fuxi-scanout/SweetSurface.h').read_text()
    config = buildtree / 'build/xnu.obj/DEVELOPMENT_ARM64_FUXI'
    if not config.exists():
        config = buildtree / 'build/xnu.obj/DEVELOPMENT_ARM64_SWEET'
    with tempfile.TemporaryDirectory(prefix='fuxi-unixnu-headers-') as tmp:
        tmp = Path(tmp)
        arm = tmp / 'iokit/Kernel/arm'
        dev = tmp / 'bsd/dev'
        arm.mkdir(parents=True)
        dev.mkdir(parents=True)
        (arm / 'SweetSurface.h').write_text(surface_header)
        (dev / 'fuxi_adreno.h').write_bytes((ROOT / 'xnu/fuxi-adreno/fuxi_adreno.h').read_bytes())
        for path in SRC.iterdir():
            if path.suffix in ('.c', '.cpp', '.h'):
                (arm / path.name).write_bytes(path.read_bytes())
        for comp, name in [('bsd', 'sweet_ufs.c'), ('iokit', 'SweetFramebuffer.cpp')]:
            cwd = config / comp / 'DEVELOPMENT'
            old_root = re.search(r'^SOURCE_DIR=(.+)/xnu$', (cwd / 'Makefile').read_text(), re.M)[1]
            raw = (cwd / '.CFLAGS').read_text().replace('\0', '')
            # REPLACECONTENTS saved this single macro without its shell quoting.
            raw = re.sub(r'(-DCONFIG_NMBCLUSTERS=\(\(1024 \* 512\) / MCLBYTES\))',
                         lambda m: shlex.quote(m[1]), raw)
            saved = shlex.split(raw)[1:]
            if comp == 'iokit':
                saved += shlex.split((cwd / '.CXXFLAGS').read_text().replace('\0', ''))[1:]
            command = ['clang++' if comp == 'iokit' else 'clang',
                       '--target=arm64-apple-macos14', '-fsyntax-only', '-DFUXI',
                       '-mgeneral-regs-only', '-Werror', '-Wno-c99-designator', '-Wno-reorder-init-list',
                       '-D__builtin_xnu_type_signature(...)=""', '-D__builtin_xnu_type_summary(...)=0',
                       '-D__builtin_xnu_types_compatible(...)=1', '-I' + str(arm)]
            skip = False
            for arg in saved:
                if skip:
                    skip = False
                    continue
                if arg == '-arch':
                    skip = True
                    continue
                arg = arg.replace(old_root, str(buildtree))
                arg = re.sub(r'-I.*?\.sdk/+System/Library/Frameworks/Kernel\.framework/Versions/A/PrivateHeaders/platform',
                             '-I' + str(buildtree / 'sdk-kernel-platform/platform'), arg)
                command.append(arg)
            subprocess.run(command + [str(arm / name)], cwd=cwd, check=True)
            print('PASS: complete ' + name + ' syntax against saved XNU kernel headers/flags')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--xnu-buildtree', type=Path,
                        help='optional extracted XNU build tree with saved component .CFLAGS/.CXXFLAGS')
    args = parser.parse_args()
    subprocess.run(['python3', str(ROOT / 'tools/sync-fuxi-unixnu.py'), '--check'], check=True)
    source = (SRC / 'sweet_ufs.c').read_text()
    # The source has paired FUXI/SWEET write guards with duplicate opening braces.
    # Select FUXI before extracting complete functions; the compiled target also defines it.
    active, selected = [True], []
    for line in source.splitlines(True):
        if line.startswith('#if '):
            assert line.strip() in ('#if defined(FUXI)', '#if defined(ARM64_BOARD_CONFIG_SWEET)')
            active.append(active[-1])
        elif line.startswith('#else'):
            active[-1] = active[-2] and not active[-1]
        elif line.startswith('#endif'):
            active.pop()
        elif active[-1]:
            selected.append(line)
    source = ''.join(selected)
    constants = source[source.index('#define REG_CAP'):source.index('/*\n * Fuxi root')]
    funcs = ''.join(function(source, n) for n in (
        'fuxi_ufs_poison', 'fuxi_ufs_check_idle', 'ufs_scsi', 'ufs_read_blocks',
        'ufs_write_blocks', 'ufs_take_over_host', 'sweet_ufs_strategy'))
    # Replace only the ARM store barrier in the host model. Cache/MMIO/clock are modeled above.
    funcs = re.sub(r'__asm__ volatile\("dsb sy" ::: "memory"\);', '(void)0;', funcs)
    with tempfile.TemporaryDirectory(prefix='fuxi-unixnu-test-') as tmp:
        tmp = Path(tmp)
        for name, text in [('ufs', UFS_PREFIX + constants + UFS_MODEL + funcs + UFS_TEST),
                           ('copy', COPY_TEST)]:
            path = tmp / (name + '.c')
            path.write_text(text)
            exe = tmp / name
            subprocess.run(['clang', '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                            '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                            '-I', str(SRC), str(path), '-o', str(exe)], check=True)
            subprocess.run([str(exe)], check=True)
        display = (SRC / 'SweetFramebuffer.cpp').read_text()
        refresh = tmp / 'refresh.c'
        refresh.write_text('#include <assert.h>\n#include <stdbool.h>\n#include <stdint.h>\n#include <stdio.h>\n' +
            function(display, 'sweet_fb_refresh_interval') + function(display, 'sweet_fb_refresh_wanted') +
            function(display, 'sweet_fb_refresh_interval_mhz') + function(display, 'sweet_fb_swap_bin') + REFRESH_TEST)
        subprocess.run(['clang', '-std=gnu11', '-Wall', '-Wextra', '-Werror', str(refresh), '-o', str(tmp / 'refresh')], check=True)
        subprocess.run([str(tmp / 'refresh')], check=True)
        assert re.search(r'setObject\("displayMinRefreshInterval", interval\)', display) and \
            display.index('sweet_fb_refresh_publish(this);') < display.index('\tregisterService();\n\tIOLog("SWEET FB: AppleCLCD')
        assert 'displayRefreshStepMachTime' not in display.replace('deliberately not published', '').split('sweet_fb_refresh_publish(AppleCLCD')[1]
        declaration = display[display.index('class AppleCLCD :'):display.index('OSDefineMetaClassAndStructors(AppleCLCD')]
        declaration = declaration.replace(': public IOMobileFramebuffer', '').replace(
            'OSDeclareDefaultStructors(AppleCLCD);', 'public:')
        methods = ''.join(function(display, name) for name in (
            'sweet_fb_swap_bin', 'sweet_fb_te_configure', 'AppleCLCD::teVsyncTick', 'AppleCLCD::teVsyncLog',
            'AppleCLCD::presentWantedLocked', 'AppleCLCD::wakePresentLocked', 'sweet_fb_rearm',
            'AppleCLCD::vsyncFired', 'AppleCLCD::scanoutLog', 'get32', 'AppleCLCD::observeRecord', 'AppleCLCD::recordDone', 'AppleCLCD::consumeRecord', 'AppleCLCD::retireEarly', 'AppleCLCD::releaseRecord',
            'AppleCLCD::swapWait', 'AppleCLCD::stopRecords',
            'AppleCLCD::writerRetired', 'AppleCLCD::writerWake', 'AppleCLCD::writerWakeAction',
            'AppleCLCD::presentStep', 'AppleCLCD::stageStats', 'AppleCLCD::completePresent', 'AppleCLCD::presentStats', 'AppleCLCD::presentNext', 'AppleCLCD::swap', 'AppleCLCD::present'))
        methods = methods.replace('__asm__ volatile ("dsb sy" ::: "memory");', '(void)0;')
        cpp = tmp / 'display.cpp'
        cpp.write_text(DISPLAY_PREFIX + declaration + methods + DISPLAY_TEST)
        subprocess.run(['clang++', '-std=c++17', '-Wall', '-Wextra', '-Werror',
                        '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                        '-I', str(SRC), str(cpp), '-o', str(tmp / 'display')], check=True)
        subprocess.run([str(tmp / 'display')], check=True)
        # Compile our ARM cache primitive as real AArch64 code (never run reference code).
        arm = tmp / 'cache.c'
        arm.write_text('typedef __UINTPTR_TYPE__ uintptr_t; typedef unsigned uint32_t;\n'
                       'typedef _Bool bool; static struct {uintptr_t dma;} su;\n' +
                       function(source, 'fuxi_ufs_cache') +
                       '\nvoid check(void) {fuxi_ufs_cache(0,64,1);fuxi_ufs_cache(0,64,0);}\n')
        subprocess.run(['clang', '--target=aarch64-none-elf', '-ffreestanding', '-mgeneral-regs-only',
                        '-Wall', '-Wextra', '-Werror', '-c', str(arm), '-o', str(tmp / 'cache.o')], check=True)
        copy_arm = tmp / 'copy-arm.c'
        copy_arm.write_text('#include "fuxi_unixnu_copy.h"\n'
                            'uint64_t check(void *d,const void *s,void *p) {return '
                            'fuxi_unixnu_copy_diff(d,512,96,s,512,104,18,3,p,512,0); }\n')
        subprocess.run(['clang', '--target=aarch64-none-elf', '-ffreestanding', '-mgeneral-regs-only',
                        '-Wall', '-Wextra', '-Werror', '-I', str(SRC), '-c', str(copy_arm),
                        '-o', str(tmp / 'copy-arm.o')], check=True)
    print('PASS: actual AArch64 cache/copy primitives compile; full XNU/macOS build and hardware remain untested')
    if args.xnu_buildtree:
        kernel_syntax(args.xnu_buildtree.resolve())


if __name__ == '__main__':
    main()
