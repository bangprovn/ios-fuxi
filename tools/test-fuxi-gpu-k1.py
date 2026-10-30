#!/usr/bin/env python3
"""K1 actual user-client dispatch/allocation tests with ASan/UBSan IOKit models.

The backend register/cache/containment model lives in test-fuxi-metal.py.
No Apple SDK or hardware is used here.
"""
import importlib.util
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('hostimport', ROOT/'tools/test-fuxi-hostimport.py')
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)

BUFFER = r'''
static unsigned segMode,segCalls;
constexpr unsigned kIOMemoryKernelUserShared=64,kIOMapWriteCombineCache=128,kIOMemoryPhysicallyContiguous=256;
struct IOBufferMemoryDescriptor: IOMemoryDescriptor {
 void *data=nullptr;
 ~IOBufferMemoryDescriptor() override {free(data);}
 static IOBufferMemoryDescriptor *inTaskWithPhysicalMask(task_t task,unsigned options,uint64_t n,uint64_t mask) {
  assert(task==kernel_task && mask && (options&kIOMapWriteCombineCache));
  if(fail==15 || (fail==16 && (options&kIOMemoryPhysicallyContiguous)))return nullptr;
  auto *m=new IOBufferMemoryDescriptor;m->length=n;m->data=calloc(1,n);assert(m->data);return m;
 }
 void *getBytesNoCopy() {return data;}
 // 0 keeps the one-16K-segment-per-page scatter model; others return real runs.
 uint64_t getPhysicalSegment(uint64_t off,IOByteCount *n,unsigned flags) {
  segCalls++;
  if(!segMode)return IOMemoryDescriptor::getPhysicalSegment(off,n,flags);
  assert(wired && flags==kIOMemoryMapperNone && !(off&16383));
  uint64_t half=length/2;
  if(segMode==1){*n=length-off;return 0x40000000+off;}              // one contiguous run
  if(segMode==2){*n=off<half?half-off:length-off;return (off<half?0x40000000ULL:0x50000000ULL)+(off<half?off:off-half);}
  *n=length-off;return (UINT64_C(1)<<36)-16384+off;                 // ends at/after 2^36
 }
};
struct FuxiAdrenoBoView {
 IOBufferMemoryDescriptor *backing=nullptr;
 bool bind(IOBufferMemoryDescriptor *m) {backing=m;m->retain();return true;}
 void release() {if(backing){backing->complete();backing->release();}delete this;}
};
'''
EXTRA = r'''
constexpr int kIOReturnNotReady=6,kIOReturnTimeout=7,kIOReturnUnsupported=8,kIOReturnNotOpen=9;
static bool available=true;
static int timeRc,statusRc;
static uint64_t gpuTicks=0xabcdef0123456789ULL,gpuFaults=7,gpuSuspends=3;
static int fuxi_gpu_abi_available() {return available;}
static int fuxi_gpu_abi_timestamp(uint64_t *t) {*t=0;if(!timeRc)*t=gpuTicks;return timeRc;}
static int fuxi_gpu_abi_status(uint64_t *f,uint64_t *s) {*f=*s=0;if(!statusRc){*f=gpuFaults;*s=gpuSuspends;}return statusRc;}
static int fuxi_gpu_abi_poll(uint64_t) {return 0;}
static unsigned lastMapPerms;static int fuxi_gpu_abi_map(uint64_t,uintptr_t,uint64_t,unsigned p,unsigned,unsigned) {lastMapPerms=p;return 0;}
static int fuxi_gpu_abi_map_import(uint64_t,uintptr_t,uint64_t,unsigned) {return 0;}
static uint64_t fuxi_gpu_abi_map_work() {return 0;}
static bool queueOn;static int fuxi_gpu_abi_queue_enabled() {return queueOn;}
static int submitRc=-1,submitBusy;static unsigned submitCount,submitCalls;static uint64_t submitLast;
static int fuxi_gpu_abi_submit(const uint64_t *,const uint64_t *b,unsigned n,uint64_t *f) {submitCalls++;submitCount=n;submitLast=b[n-1];*f=submitCalls;if(submitBusy>0){submitBusy--;return 1;}return submitRc;}
static uint64_t mach_absolute_time() {return 0;}
static constexpr unsigned NSEC_PER_MSEC=1000000;
static void clock_interval_to_deadline(unsigned,unsigned,uint64_t *d) {*d=1;}
static unsigned statCount[FG_ABI_STATS],spins,pauses;
static int lastSpinDepth=-1;
static bool *spinKill;
static void abiStat(unsigned slot,uint64_t) {assert(slot<FG_ABI_STATS);statCount[slot]++;}
static void abiPause() {pauses++;}
static unsigned pends;static void pendStat(uint64_t,uint64_t,bool) {pends++;}
static void abiSplit(unsigned,uint64_t) {}
static unsigned waitHint=100,eventWaits,lastWaitSeq;static uint32_t eventSeq;static bool eventWoke;
static uint32_t abiEventSeq() {return eventSeq;}
static int lastWaitDepth=-1;static bool abiEventWait(uint32_t seq0,unsigned) {eventWaits++;lastWaitSeq=seq0;lastWaitDepth=lockDepth;return eventWoke;}
static unsigned fuxi_gpu_abi_submit_wait_us(unsigned *k) {*k=1;return waitHint;}
static const char *abiStep(unsigned) {return "test";}
using OSObject=int;using IOExternalMethodDispatch=int;
struct IOExternalMethodArguments {
 unsigned scalarInputCount=0,scalarOutputCount=0,asyncWakePort=0;
 void *structureInputDescriptor=nullptr,*structureOutputDescriptor=nullptr;
 unsigned structureInputSize=0,structureOutputSize=0;
 const void *structureInput=nullptr;void *structureOutput=nullptr;
};
'''
TEST = r'''
};
static unsigned inputOverride;
static IOReturn call(Client &c,unsigned method,const void *in,void *out) {
 IOExternalMethodArguments a;
 a.structureInputSize=inputOverride?inputOverride:fg_abi_input_size(method);a.structureInput=in;
 a.structureOutputSize=fg_abi_output_size(method);a.structureOutput=out;
 return c.externalMethod(method,&a,nullptr,nullptr,nullptr);
}
int main() {
 Client c;
 NativeAdrenoAbiTimestamp ts{};NativeAdrenoAbiStatus status{};
 assert(!call(c,7,nullptr,&ts) && ts.ticks==gpuTicks);
 assert(!call(c,8,nullptr,&status) && status.faults==7 && status.suspends==3);
 available=false; // status bypasses availability so Turnip can see device loss
 assert(!call(c,8,nullptr,&status) && status.faults==7);
 for(timeRc=-1;timeRc>=-3;timeRc--) {
  ts.ticks=99;
  assert(call(c,7,nullptr,&ts)==(timeRc==-1?kIOReturnNotReady:timeRc==-2?kIOReturnIOError:kIOReturnTimeout));
  assert(!ts.ticks);
 }
 timeRc=0;statusRc=-1;status={99,99};
 assert(call(c,8,nullptr,&status)==kIOReturnNotReady && !status.faults && !status.suspends);
 statusRc=0;available=true;
 // Exact struct lengths, no scalar/async/descriptor ABI, no input on 7/8.
 for(unsigned m=7;m<=8;m++) {
  IOExternalMethodArguments a;a.structureOutput=&status;
  a.structureOutputSize=fg_abi_output_size(m)-1;
  assert(c.externalMethod(m,&a,nullptr,nullptr,nullptr)==kIOReturnBadArgument);
  a.structureOutputSize=fg_abi_output_size(m)+1;
  assert(c.externalMethod(m,&a,nullptr,nullptr,nullptr)==kIOReturnBadArgument);
  a.structureOutputSize=fg_abi_output_size(m);a.structureInputSize=1;a.structureInput=&ts;
  assert(c.externalMethod(m,&a,nullptr,nullptr,nullptr)==kIOReturnBadArgument);
  a.structureInputSize=0;a.scalarInputCount=1;
  assert(c.externalMethod(m,&a,nullptr,nullptr,nullptr)==kIOReturnBadArgument);
  a.scalarInputCount=0;a.structureOutput=nullptr;
  assert(c.externalMethod(m,&a,nullptr,nullptr,nullptr)==kIOReturnBadArgument);
 }
 c.live_=false;status={99,99};
 assert(call(c,8,nullptr,&status)==kIOReturnNotOpen && !status.faults && !status.suspends);
 c.live_=true;
 NativeAdrenoAbiBoAlloc alloc{4096,12,0};NativeAdrenoAbiBoAllocReply out{};
 // Allocate 1024 real model-backed objects (16 MiB after 16K rounding).
 for(unsigned i=0;i<FG_ABI_MAX_BOS;i++) {
  assert(!call(c,2,&alloc,&out) && out.handle);
  assert(c.bos_[i].handle==out.handle && c.bos_[i].allocated==16384);
 }
 assert(c.wired_==FG_ABI_MAX_BOS*16384 && descriptors==FG_ABI_MAX_BOS);
 assert(call(c,2,&alloc,&out)==kIOReturnNoResources && !out.handle);
 NativeAdrenoAbiBoFree release{c.bos_[700].handle};
 assert(!call(c,4,&release,nullptr));
 assert(!call(c,2,&alloc,&out) && out.handle==c.bos_[700].handle);
 NativeAdrenoAbiMetadata meta{};meta.handle=out.handle;meta.bytes=64;
 for(unsigned i=0;i<64;i++)meta.text[i]=(char)i;
 assert(!call(c,9,&meta,nullptr));
 NativeAdrenoAbiMetadata reply;
 for(unsigned n=0;n<64;n++) {
  meta.bytes=n;memset(&reply,0xff,sizeof(reply));
  assert(call(c,10,&meta,&reply)==kIOReturnBadArgument);
  NativeAdrenoAbiMetadata zero{};assert(!memcmp(&reply,&zero,sizeof(reply)));
 }
 meta.bytes=64;assert(!call(c,10,&meta,&reply));
 assert(reply.handle==meta.handle && reply.bytes==64 && !memcmp(meta.text,reply.text,64));
 meta.bytes=65;assert(call(c,10,&meta,&reply)==kIOReturnBadArgument);
 meta.bytes=64;meta.reserved=1;assert(call(c,10,&meta,&reply)==kIOReturnBadArgument);meta.reserved=0;
 meta.bytes=0;assert(!call(c,9,&meta,nullptr) && !call(c,10,&meta,&reply) && !reply.bytes);
 // Keep invalid-token and no-lease errors: Turnip HEAD only retries BoFree.
 NativeAdrenoAbiBoCpuMap cpu{meta.handle};NativeAdrenoAbiBoCpuMapReply view{};
 assert(!call(c,13,&cpu,&view));
 release.handle=meta.handle;assert(call(c,4,&release,nullptr)==kIOReturnBusy);
 NativeAdrenoAbiBoCpuUnmap undo{meta.handle,view.token,0};
 undo.token++;assert(call(c,14,&undo,nullptr)==kIOReturnBadArgument);undo.token--;
 assert(!call(c,14,&undo,nullptr));
 assert(call(c,14,&undo,nullptr)==kIOReturnBadArgument);
 assert(!c.clientClose() && !c.wired_ && !descriptors && !maps);
 // Rounded allocation shares the wired quota with imported BOs; refusal
 // occurs before allocating backing storage, including corrupt accounting.
 c.live_=true;c.wired_=FG_ABI_MAX_WIRED_BYTES-16384;
 assert(!call(c,2,&alloc,&out) && c.wired_==FG_ABI_MAX_WIRED_BYTES);
 assert(call(c,2,&alloc,&out)==kIOReturnNoResources);
 assert(!c.drop(c.bos_[0]));c.wired_=FG_ABI_MAX_WIRED_BYTES-4096;
 assert(call(c,2,&alloc,&out)==kIOReturnNoResources);
 c.wired_=FG_ABI_MAX_WIRED_BYTES+1;assert(call(c,2,&alloc,&out)==kIOReturnNoResources);c.wired_=0;
 c.nextToken_=UINT32_MAX;assert(call(c,2,&alloc,&out)==kIOReturnNoResources);c.nextToken_=1;
 c.nextHandle_=UINT64_MAX;assert(call(c,2,&alloc,&out)==kIOReturnNoResources);c.nextHandle_=1;
 fail=15;assert(call(c,2,&alloc,&out)==kIOReturnNoMemory && !c.wired_ && !descriptors);
 fail=16;assert(!call(c,2,&alloc,&out));assert(!c.drop(c.bos_[0]));
 alloc.contiguous=1;assert(call(c,2,&alloc,&out)==kIOReturnNoMemory && !descriptors);
 // Physical-segment walk: one lookup per run, same acceptance as the per-page walk.
 fail=0;c.live_=true;NativeAdrenoAbiBoAlloc big{65536,12,0};
 segMode=1;segCalls=0;assert(!call(c,2,&big,&out) && segCalls==1);assert(!c.drop(*c.find(out.handle)));
 big.contiguous=1;segCalls=0;assert(!call(c,2,&big,&out) && segCalls==1);assert(!c.drop(*c.find(out.handle)));
 segMode=2;segCalls=0;assert(call(c,2,&big,&out)==kIOReturnNoMemory && segCalls==2 && !descriptors && !c.wired_); // required contiguous, split runs
 big.contiguous=0;segCalls=0;assert(!call(c,2,&big,&out) && segCalls==2);assert(!c.drop(*c.find(out.handle)));
 segMode=3;NativeAdrenoAbiBoAlloc edge{32768,12,0};
 assert(call(c,2,&edge,&out)==kIOReturnNoMemory && !descriptors && !c.wired_); // second page crosses 2^36
 edge.bytes=16384;assert(!call(c,2,&edge,&out));assert(!c.drop(*c.find(out.handle)));
 segMode=0;big.contiguous=0;fail=4;assert(call(c,2,&big,&out)==kIOReturnNoMemory && !descriptors);fail=0; // 4K runs still refused
 assert(!descriptors && !c.wired_);
 // WaitFence and a submit-side wait sleep with the client lock released and refuse stale numbers.
 NativeAdrenoAbiWaitFence wf{3,1000};c.firstFence_=1;c.lastFence_=5;spins=0;
 assert(!call(c,6,&wf,nullptr) && spins==1 && lastSpinDepth==0 && lockDepth==0 && pends==1);
 wf.fence=6;assert(call(c,6,&wf,nullptr)==kIOReturnBadArgument && spins==1);
 wf.fence=0;assert(call(c,6,&wf,nullptr)==kIOReturnBadArgument && spins==1);
 wf.fence=3;spinKill=&c.live_;assert(call(c,6,&wf,nullptr)==kIOReturnNotOpen && lockDepth==0);spinKill=nullptr;c.live_=true;
 // Every accepted call is timed once (lock wait + selector); phases only where they ran.
 unsigned calls=0;for(unsigned i=1;i<=16;i++)calls+=statCount[i];
 assert(calls && calls==statCount[FG_ABI_STAT_LOCK] && statCount[6]>=2 && statCount[2]>1024);
 assert(statCount[FG_ABI_STAT_WALK]>=statCount[FG_ABI_STAT_ZERO] && statCount[FG_ABI_STAT_ZERO]>1024);
 // Alloc stages: descriptor+prepare and view bind run once per accepted alloc (never more than the segment walk); owned maps record lock and work together, imports neither.
 assert(statCount[FG_ABI_STAT_ALLOCMEM]>=statCount[FG_ABI_STAT_WALK] && statCount[FG_ABI_STAT_BIND]==statCount[FG_ABI_STAT_ZERO] && statCount[FG_ABI_STAT_MAPLOCK]==statCount[FG_ABI_STAT_MAPWORK] && !statCount[FG_ABI_STAT_MAPWAIT]);
 // Handle index: every live handle resolves to its slot after churn, stale and zero handles do not, freed slots are reused lowest first.
 {
  Client d;d.live_=true;NativeAdrenoAbiBoAlloc one{4096,12,0};NativeAdrenoAbiBoAllocReply r[40];
  for(unsigned i=0;i<40;i++) {assert(!call(d,2,&one,&r[i]));assert(d.find(r[i].handle)==&d.bos_[i]);}
  NativeAdrenoAbiBoFree rel{};
  for(unsigned i=0;i<40;i+=3) {rel.handle=r[i].handle;assert(!call(d,4,&rel,nullptr));assert(!d.find(r[i].handle));}
  for(unsigned i=0;i<40;i++) assert(!!d.find(r[i].handle)==(i%3!=0));
  assert(!d.find(0) && !d.find(UINT64_MAX) && !d.find(r[39].handle+100));
  NativeAdrenoAbiBoAllocReply again;assert(!call(d,2,&one,&again) && d.find(again.handle)==&d.bos_[0] && d.liveBos_==27);
  for(unsigned i=0;i<FG_ABI_MAX_BOS;i++) { NativeAdrenoAbiBoAllocReply x;IOReturn s=call(d,2,&one,&x);if(s) {assert(s==kIOReturnNoResources && d.liveBos_==FG_ABI_MAX_BOS);break;}assert(d.find(x.handle));}
  for(unsigned i=0;i<FG_ABI_MAX_BOS;i++) if(d.bos_[i].handle) assert(d.find(d.bos_[i].handle)==&d.bos_[i]);
  assert(peakBos>=FG_ABI_MAX_BOS);
  assert(!d.clientClose() && !d.liveBos_);
 }
 // A frame of 128 IBs is one accepted record; the 416-byte record of the 16-IB layout still works for up to 16.
 {
  Client d;d.live_=true;NativeAdrenoAbiBoAlloc one{65536,12,0};NativeAdrenoAbiBoAllocReply h;
  assert(!call(d,2,&one,&h));Client::Bo *b=d.find(h.handle);b->iova=0x20000000;
  static union {NativeAdrenoAbiSubmit s;char raw[sizeof(NativeAdrenoAbiSubmit)];} big;
  memset(&big,0,sizeof(big));
  for(unsigned i=0;i<NativeAdrenoAbiMaxBuffers;i++) big.s.buffers[i]={h.handle,0x20000000+i*256,256};
  NativeAdrenoAbiSubmitReply sr{};submitRc=0;submitCalls=0;
  big.s.buffer_count=128;assert(!call(d,5,&big,&sr) && submitCount==128 && submitLast==256 && sr.fence==1);
  big.s.buffer_count=129;assert(call(d,5,&big,&sr)==kIOReturnBadArgument && submitCalls==1);
  big.s.buffer_count=0;assert(call(d,5,&big,&sr)==kIOReturnBadArgument);
  inputOverride=FG_ABI_SUBMIT_V1_BYTES;
  big.s.buffer_count=16;assert(!call(d,5,&big,&sr) && submitCount==16 && submitCalls==2);
  big.s.buffer_count=17;assert(call(d,5,&big,&sr)==kIOReturnBadArgument && submitCalls==2); /* the record holds 16: never read past it */
  inputOverride=FG_ABI_SUBMIT_V1_BYTES+1;big.s.buffer_count=1;assert(call(d,5,&big,&sr)==kIOReturnBadArgument);
  inputOverride=sizeof(NativeAdrenoAbiSubmit)-1;assert(call(d,5,&big,&sr)==kIOReturnBadArgument);
  inputOverride=FG_ABI_SUBMIT_V1_BYTES;NativeAdrenoAbiBoFree rel{h.handle};assert(call(d,4,&rel,nullptr)==kIOReturnBadArgument); /* method 4 is not a submit: only 5 takes 416 */
  // Queue mode: a wait on a fence of this client's own chain is skipped (the ring is in order); other fences still wait.
  inputOverride=0;submitRc=0;d.firstFence_=1;d.lastFence_=5;big.s.buffer_count=1;big.s.wait_fence=3;big.s.wait_timeout_ns=1000;
  unsigned s0=spins;queueOn=false;assert(!call(d,5,&big,&sr) && spins==s0+1);
  queueOn=true;d.firstFence_=1;d.lastFence_=5;assert(!call(d,5,&big,&sr) && spins==s0+1);
  d.firstFence_=1;d.lastFence_=5;big.s.wait_fence=9;assert(call(d,5,&big,&sr)==kIOReturnBadArgument && spins==s0+1); // not a fence of the chain: the normal refusal
  queueOn=false;big.s.wait_fence=0;
  // A refused submit sleeps on the event with lock_ dropped (not polling), re-validates its IBs and goes through when admitted.
  submitRc=0;submitBusy=2;eventWaits=0;eventWoke=true;eventSeq=7;waitHint=1234;big.s.buffer_count=1;pauses=0;
  assert(!call(d,5,&big,&sr) && eventWaits==2 && lastWaitSeq==7 && lastWaitDepth==0 && !pauses && lockDepth==0);
  submitBusy=0;
  // BoMap.reserved: bit 0 (GPU_ONLY) is accepted, any other bit refused; zero stays valid.
  {NativeAdrenoAbiBoMap mp{h.handle,0x20010000,3,0,2,2};NativeAdrenoAbiBoMapReply mr{};
   assert(call(d,3,&mp,&mr)==kIOReturnBadArgument);mp.reserved=1;mp.iova=0x20010000;
   NativeAdrenoAbiBoAlloc al2{65536,12,0};NativeAdrenoAbiBoAllocReply h2;assert(!call(d,2,&al2,&h2));mp.handle=h2.handle;
   assert(!call(d,3,&mp,&mr) && mr.iova==0x20010000 && !(lastMapPerms&FG_ABI_IMPORT_GPU_ONLY));
   NativeAdrenoAbiBoFree fr{h2.handle};d.find(h2.handle)->iova=0;assert(!call(d,4,&fr,nullptr));}
  NativeAdrenoAbiParams qp{};assert(!call(d,1,nullptr,&qp) && qp.flags==(NativeAdrenoParamsHostImport|NativeAdrenoParamsImportGpuOnly));
  inputOverride=0;submitRc=-1;b->iova=0;assert(!d.clientClose());
 }
 puts("PASS: actual methods 7/8 dispatch/errors/lengths, 1024 BO allocation/refusal/reuse/close, handle index after churn, 128-IB and legacy 416-byte submit records, shared wired quota, metadata refusal and CPU leases");
}
'''


HANDLE_STUBS = r'''
#include <new>
#include <cstddef>
#define IONewZero(T,n) (new (std::nothrow) T[n]())
#define IODelete(p,T,n) delete[] (p)
static unsigned peakBos;
#include "FuxiHandleIndex.h"
'''


def main():
    source = (ROOT/'xnu/fuxi-adreno/FuxiAdrenoUserClient.cpp').read_text()
    prefix = host.PREFIX.replace('#include <cstdio>', '#include <cstdio>\n#include <cstdlib>')
    start = prefix.index('struct IOBufferMemoryDescriptor {')
    end = prefix.index('static int fuxi_gpu_abi_unmap', start)
    prefix = prefix[:start]+BUFFER+prefix[end:]
    helper = (ROOT/'xnu/fuxi-adreno/fuxi_adreno_host.c').read_text()
    helper = helper[helper.index('bool fuxi_adreno_host_page_ok('):helper.index('#endif')]
    bo = source[source.index(' enum {MaxBos='):source.index(' task_t task_')]
    close = host.method(source, 'clientClose').replace(' override', '').replace('openLock);', '&openLock);')
    external = host.method(source, 'externalMethod').replace(' override', '')
    # find() is pointer-returning, so use its adjacent source boundaries.
    find = source[source.index(' Bo *find('):source.index(' IOReturn wait(')]
    wait = source[source.index(' IOReturn wait('):source.index(' IOReturn spin(')]
    # The real waitUnlocked must release the client lock around the sleep loop.
    wait += ' IOReturn spin(uint64_t,uint64_t,uint64_t *,bool *) {spins++;waits++;lastSpinDepth=lockDepth;if(spinKill)*spinKill=false;return 0;}\n'
    prefix = prefix.replace('static void IOLockLock(int *){}','static int lockDepth;static void IOLockLock(int *){lockDepth++;}').replace('static void IOLockUnlock(int *){}','static void IOLockUnlock(int *){lockDepth--;}')
    model = prefix+EXTRA+HANDLE_STUBS+helper+'struct Client {\n'+bo+r'''
 /* The kernel allocates the table in initWithTask; the model owns it inline. */
 Bo storage_[MaxBos]={};
 Client(){bos_={storage_,storage_+MaxBos};assert(index_.allocate());}
 ~Client(){index_.release();}
 task_t task_=7;uint64_t nextHandle_=1,wired_=0,lastFence_=0,firstFence_=0,submitAt_[16]={};
 uint32_t nextToken_=1;bool queues_[8]={true};
 int *lock_=&clientLock;bool live_=true;int pid_=0;
 void terminate(){} void result(){}
 '''+find+wait+''.join(host.method(source, n) for n in ('allocate', 'importHost', 'drop', 'run'))+close+external+TEST
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)/'k1.cpp';path.write_text(model)
        exe = path.with_suffix('')
        subprocess.run(['clang++', '-std=c++17', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                        '-fsanitize=address,undefined', '-I'+str(ROOT/'xnu/fuxi-adreno'), str(path), '-o', str(exe)], check=True)
        subprocess.run([str(exe)], check=True)
        # Parse the complete device helper on Linux, without pretending these
        # minimal Mach declarations constitute an iOS SDK or linked build.
        mach = Path(tmp)/'mach';mach.mkdir()
        (mach/'mach.h').write_text('''#include <stdint.h>
typedef uint32_t mach_port_t;
typedef mach_port_t task_t;
typedef int kern_return_t;
typedef uint64_t mach_vm_address_t, mach_vm_size_t;
#define MACH_PORT_NULL 0
extern "C" task_t mach_task_self(void);
''')
        subprocess.run(['clang++', '-std=c++17', '-Wall', '-Wextra', '-Werror',
                        '-fsyntax-only', '-I'+tmp, str(ROOT/'tools/fuxi-gpu-abi-test.cpp')], check=True)
        print('PASS: complete device helper C++ syntax with minimal Mach declarations (no SDK/link)')


if __name__ == '__main__':
    main()
