/* Bounded, opt-in compositor timing; no per-frame heap storage. */
#pragma once
#include "TranslationPolicy.hpp"
#include "ShadowRanges.hpp"
#include <algorithm>
#include <array>
#include <atomic>
#include <ctime>
#include <thread>
#if defined(__APPLE__)
#include <mach/mach.h>
#include <mach/mach_time.h>
#include <pthread.h>
#endif
#include <chrono>
#include <cstdint>
#include <cstdio>
namespace fuxi {
using FrameClock=std::chrono::steady_clock;
inline uint64_t frameNanos(FrameClock::time_point start) {
 return uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(FrameClock::now()-start).count());
}
// Failure is a sentinel, never a wall-clock fallback. The hook also exercises
// unsupported clocks and deterministic CPU deltas in Linux host tests.
inline uint64_t frameThreadCPU() {
#ifdef FUXI_MTL_THREAD_CPU_NS
 return FUXI_MTL_THREAD_CPU_NS();
#elif defined(__APPLE__)
 thread_basic_info_data_t info{};mach_msg_type_number_t count=THREAD_BASIC_INFO_COUNT;
 if(thread_info(pthread_mach_thread_np(pthread_self()),THREAD_BASIC_INFO,
    reinterpret_cast<thread_info_t>(&info),&count)!=KERN_SUCCESS)return UINT64_MAX;
 return (uint64_t(info.user_time.seconds)+uint64_t(info.system_time.seconds))*1000000000ULL+
        (uint64_t(info.user_time.microseconds)+uint64_t(info.system_time.microseconds))*1000ULL;
#else
 timespec time{};if(clock_gettime(CLOCK_THREAD_CPUTIME_ID,&time))return UINT64_MAX;
 return uint64_t(time.tv_sec)*1000000000ULL+uint64_t(time.tv_nsec);
#endif
}
inline uint64_t frameCPUDelta(uint64_t start,uint64_t end) {
 return start!=UINT64_MAX && end!=UINT64_MAX && end>=start?end-start:0;
}
inline unsigned frameCPUFromRegister(uint64_t value){return unsigned(value&0xfff);}
inline unsigned frameCPU() {
#if defined(__APPLE__) && defined(__aarch64__) && !defined(FUXI_MTL_HOST_TEST)
 // XNU 8796 machine_machdep.h and libsyscall/os/tsd.h: TPIDR_EL0 low 12 bits.
 uint64_t value;__asm__ __volatile__("mrs %0, TPIDR_EL0":"=r"(value));return frameCPUFromRegister(value);
#else
 return 4096;
#endif
}
struct FrameCounts {uint64_t pipelines=0,descriptors=0,allocations=0;};
// Count successful Vulkan pipeline/set/memory creations on the calling thread.
inline thread_local FrameCounts frameCounts;
// Pipelines compiled on the draw path (not by the background prewarm) and the time they blocked the caller; process-wide.
inline std::atomic<uint64_t> syncCompiles{0},syncCompileNs{0};
enum class MergeSplit : size_t {none,encoder,attachments,area,actions,attachmentSample,storage,shadow,fetch,nonDraw,bufferAlias,count};
inline constexpr const char *mergeSplitNames[]={"none","encoder","attachments","area","actions","attachment_sample","storage","shadow","fetch","non_draw","buffer_alias"};
// Exclusive per-phase wall time (nested phases pause their parent). Encode phases cover the frontend entry-point families on the calling thread and
// are drained into the sample at commit; submit phases accumulate into the current sample. Disabled (no clock reads) unless frame tracing is on.
enum class EncPhase : size_t {encBegin,encChecks,encEnd,pipeline,texSamp,bufBytes,state,draw,blit,newBuffer,newTexture,iosurface,newOther,cbMisc,count};
inline constexpr const char *encPhaseNames[]={"enc_begin","enc_checks","enc_end","set_pipeline","set_tex_samp","set_buf_bytes","set_state","draw","blit","new_buffer","new_texture","iosurface","new_other","cb_misc"};
// Finer submit attribution (exclusive like the rest): rewrite = pass rename/hoist/reorder/rt-shadow before planning; plan_lookup = draw-plan cache/translator;
// plan_mip = sampled-attachment access proof; plan_bounds = index/vertex/earlier-writer checks; analyze = occlusion/merge/neutral; refresh = shadow refresh + hash;
// pass_setup = render pass/framebuffer selection; area = render-area bounding; bind = per-draw pipeline/dynamic state/descriptor values/set bind; retire_build = flight use lists + retirement closure.
enum class SubPhase : size_t {other,cmdBegin,cmdEnd,objects,descriptors,passes,barriers,record,queueSubmit,fence,wait,prepare,flush,uses,plan,pin,rewrite,planLookup,planMip,planBounds,analyze,refresh,passSetup,area,bind,retireBuild,rwHoist,rwReorder,rwShadow,setup,recLoop,tail,count};
inline constexpr const char *subPhaseNames[]={"other","cmd_begin","cmd_end","objects","descriptors","passes","barriers","record","queue_submit","fence","wait","prepare","flush","uses","plan","pin","rewrite","plan_lookup","plan_mip","plan_bounds","analyze","refresh","pass_setup","area","bind","retire_build","rw_hoist","rw_reorder","rw_shadow","setup","rec_loop","tail"};
static_assert(sizeof(subPhaseNames)/sizeof(*subPhaseNames)==size_t(SubPhase::count),"submit phase names");
// Per-frame probe counts of per-draw linear structures: bound-set entries visited, full binding compares, view-cache entries visited, use-list entries visited, frame-cache entries visited.
enum class ScanKind : size_t {boundSetProbes,boundSetCompares,viewProbes,useProbes,frameProbes,boundSetMax,count};
inline constexpr const char *scanKindNames[]={"bound_set_probes","bound_set_compares","view_probes","use_list_probes","frame_probes","bound_set_max"};
inline uint64_t phaseNow() {
#if defined(__APPLE__) && !defined(FUXI_MTL_HOST_TEST)
 static const mach_timebase_info_data_t base=[]{mach_timebase_info_data_t b{};mach_timebase_info(&b);return b;}();
 return mach_absolute_time()*base.numer/base.denom;
#else
 return uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now().time_since_epoch()).count());
#endif
}
struct FrameSample {
 std::array<uint64_t,size_t(EncPhase::count)> encNs{},encCalls{};std::array<uint64_t,size_t(SubPhase::count)> subNs{},subCalls{};std::array<uint64_t,size_t(ScanKind::count)> scans{};
 uint64_t backendCPU=0,frontendCPU=0,frontendWall=0,frontendDraw=0,frontendDrawSamples=0;
 uint64_t cpuSampledSubmits=0,cpuOmittedSubmits=0;
 uint64_t pipe=0,desc=0,record=0,kernelIO=0;
 uint64_t backendLockCPU=0,slotWaitCPU=0,pipeCPU=0,descCPU=0,recordCPU=0,flushCPU=0,kernelIOCPU=0,queueSubmitCPU=0;
 uint64_t wall=0,cpuBeforeCommit=0,shadowCopy=0,leaseLock=0,backendSubmit=0,gpuWait=0,gpuExec=0;
 // lease_lock split: frontend lease vs backend lock/slot; exclusion wait vs kernel lock by role; surfaces by role.
 uint64_t leaseFront=0,leaseBack=0,leaseExclWait=0,leaseLockWrite=0,leaseLockRead=0,leaseWrites=0,leaseReads=0;
 uint64_t inflight=0,completionLatency=0,workerDepth=0;
 uint64_t schedWait=0,commitCall=0,commitGap=0,admissionWait=0,snapshot=0,submitQueue=0;
 uint64_t backendLock=0,slotWait=0,flush=0,queueSubmit=0,retireQueue=0,retireLock=0,invalidate=0,retireHost=0,surfaceRelease=0,scheduledHandlers=0;
 uint64_t textureShadowBytes=0,textureRepitchedBytes=0;std::array<uint64_t,3> textureShadowUses{};
 uint64_t snapshotBytes=0,shadowCopyBytes=0,shadowScannedIndices=0;std::array<uint64_t,size_t(ShadowFullReason::count)> shadowFullReasons{};
 uint64_t planHits=0,planMisses=0;
 uint64_t queueSubmits=0,imageBarriers=0,readBarriersSkipped=0,crossEncoderMerges=0,pipelineBarriers=0,shadowRefreshSkipped=0,postBarriersSkipped=0;std::array<uint64_t,16> layoutKinds{}; // old*4+new over undefined,general,shader-read,transfer-dst
 uint64_t drainWaits=0,conflictWaits=0,slotWaits=0,shadowWaits=0;
 // Submits that waited on a flight for each resource kind (buffer, shadow, texture, surface, index) and shadow storage renames that avoided the wait.
 std::array<uint64_t,5> conflictKinds{};std::array<uint64_t,6> renameDeclined{};uint64_t shadowRenames=0,shadowRenameBytes=0; // declined: off, not_backing, direct_use, texture, budget, other
 uint64_t drainSubmits=0,prepareDrains=0,prepareUnknownDrains=0,prepareConflictWaits=0,indexUploadDrains=0,indexGPUWritableDrains=0,indexUploadSubmits=0,indexReadOnlySubmits=0,indexGPUWritableSubmits=0;
 uint64_t snapshotReusedBytes=0,shadowSkippedBytes=0,shadowHashSkipped=0,shadowHashCopied=0,shadowHashBytes=0,shadowHashNs=0,renderAreaPasses=0,renderAreaShrunk=0,renderAreaPixels=0,renderAreaFullPixels=0,passReorders=0,sampleOptimal=0,sampleLinear=0,sampleShadow=0,sampleLinearBytes=0,sampleShadowBytes=0,sampleOptimalBytes=0,rtIn=0,rtInSkipped=0,rtOut=0,rtSyncs=0,rtBytes=0,rtEligible=0,rtLinearLarge=0;
 // Last observed handler batch / explicit wait, sampled on the following commit.
 uint64_t priorDelivery=0,priorHandlers=0,priorWaitCompleted=0;
 // Command buffer creation (steady clock); the window derives frame-to-frame cadence.
 uint64_t createdNs=0,interval=0;
 uint64_t mergedDraws=0;std::array<uint64_t,size_t(MergeSplit::count)> mergeSplits{};
 uint64_t draws=0,passes=0,gpuSamples=0;FrameCounts counts{};unsigned cpu=4096;bool encodeCountsUnknown=false;
};
inline thread_local FrameSample *currentFrame=nullptr;
struct EncAccum {std::array<uint64_t,size_t(EncPhase::count)> ns{},calls{};};
inline thread_local EncAccum encAccum;
inline void scanAdd(ScanKind k,uint64_t n){if(currentFrame)currentFrame->scans[size_t(k)]+=n;}
inline void scanMax(ScanKind k,uint64_t n){if(currentFrame && currentFrame->scans[size_t(k)]<n)currentFrame->scans[size_t(k)]=n;}
class PhaseTimer {
 uint64_t *ns=nullptr,*calls=nullptr;PhaseTimer *previous=nullptr;uint64_t resume=0;
 inline static thread_local PhaseTimer *active=nullptr;
 void begin(uint64_t *n,uint64_t *c){
#ifndef FUXI_MTL_NO_PHASES
  ns=n;calls=c;uint64_t now=phaseNow();previous=active;if(previous)*previous->ns+=now-previous->resume;resume=now;active=this;
#else
  (void)n;(void)c;
#endif
 }
public:
 explicit PhaseTimer(EncPhase p){if(systemTranslationPolicy().frameTrace)begin(&encAccum.ns[size_t(p)],&encAccum.calls[size_t(p)]);}
 explicit PhaseTimer(SubPhase p){if(auto *f=currentFrame)begin(&f->subNs[size_t(p)],&f->subCalls[size_t(p)]);}
 ~PhaseTimer(){
#ifndef FUXI_MTL_NO_PHASES
  if(!ns)return;uint64_t now=phaseNow();*ns+=now-resume;++*calls;active=previous;if(previous)previous->resume=now;
#endif
 }
 PhaseTimer(const PhaseTimer &)=delete;
 PhaseTimer &operator=(const PhaseTimer &)=delete;
};
inline void drainEncodePhases(FrameSample &s){s.encNs=encAccum.ns;s.encCalls=encAccum.calls;encAccum={};}
struct FrameTraceScope {
 FrameSample *previous;
 explicit FrameTraceScope(FrameSample *sample):previous(currentFrame){currentFrame=sample;}
 ~FrameTraceScope(){currentFrame=previous;}
};
// Disabled scopes read no clocks, touch no atomics and allocate nothing.
class FrameStageTimer {
 FrameSample *sample;uint64_t FrameSample::*wall;
 FrameClock::time_point start{};
public:
 FrameStageTimer(FrameSample *s,uint64_t FrameSample::*w):sample(s),wall(w) {
  if(sample)start=FrameClock::now();
 }
 void stop(){if(sample){sample->*wall+=frameNanos(start);sample=nullptr;}}
 ~FrameStageTimer(){stop();}
 FrameStageTimer(const FrameStageTimer &)=delete;
 FrameStageTimer &operator=(const FrameStageTimer &)=delete;
};
// These are API envelopes: the external Vulkan driver owns its actual GPU ABI
// syscalls. Do not interpret this as kernel-only execution or add it to submit.
template<class Call> decltype(auto) frameKernelCall(Call &&call) {
 FrameStageTimer timer(systemTranslationPolicy().frameTrace?currentFrame:nullptr,&FrameSample::kernelIO);
 return call();
}
// CPU sampling brackets the first frontend method through commit preparation.
// This includes client work between methods; never subtract clocks from different
// threads. Valid Metal encoding serializes access to a command buffer's encoders.
struct FrontendTimes {
 std::atomic<uint64_t> wall{0};uint64_t drawWall=0,drawCalls=0,drawSamples=0;
 uint64_t cpuStart=UINT64_MAX,cpu=0;bool started=false,migrated=false,finished=false;
 std::thread::id thread;
 void begin(){
  if(finished)return;
  if(!started){started=true;thread=std::this_thread::get_id();cpuStart=frameThreadCPU();}
  else if(thread!=std::this_thread::get_id())migrated=true;
 }
 void copyTo(FrameSample &s) {
  s.frontendWall=wall.load(std::memory_order_relaxed);s.frontendDraw=drawSamples?drawWall*drawCalls/drawSamples:0;s.frontendDrawSamples=drawSamples;
  if(started && !finished){finished=true;
   if(!migrated && thread==std::this_thread::get_id())cpu=frameCPUDelta(cpuStart,frameThreadCPU());
  }
  s.frontendCPU=cpu;
 }
};
// Sample one in sixteen draw calls at level 2; report an estimated total, no CPU clock reads.
class FrontendDrawTimer {
 FrontendTimes *times=nullptr;FrameClock::time_point start{};
public:
 explicit FrontendDrawTimer(FrontendTimes *t){if(t && systemTranslationPolicy().frameCPU && (t->drawCalls++&15u)==0){times=t;start=FrameClock::now();}}
 ~FrontendDrawTimer(){if(times){times->drawWall+=frameNanos(start);++times->drawSamples;}}
};
// Only level 2 enables frontend method wall timing. Nesting counts once; the
// CPU clock is read at most twice per command buffer, never once per method.
class FrontendTimer {
 FrontendTimes *times;FrontendTimer *previous=nullptr;
 inline static thread_local FrontendTimer *active=nullptr;
 FrameClock::time_point start{};
public:
 explicit FrontendTimer(FrontendTimes *t):times(systemTranslationPolicy().frameCPU?t:nullptr) {
  if(!times)return;
  for(auto *p=active;p;p=p->previous)if(p->times==times){times=nullptr;return;}
  times->begin();previous=active;active=this;start=FrameClock::now();
 }
 void stop(){if(times){times->wall.fetch_add(frameNanos(start),std::memory_order_relaxed);active=previous;times=nullptr;}}
 ~FrontendTimer(){stop();}
 FrontendTimer(const FrontendTimer &)=delete;
 FrontendTimer &operator=(const FrontendTimer &)=delete;
};
// Only BackboardGate accesses this, under its existing reporter/completion lock.
template<class Lease> inline void addLeaseTrace(FrameSample &t,const Lease &l,uint64_t frontNs){
 t.leaseLock+=frontNs;t.leaseFront+=frontNs;t.leaseExclWait+=l.stats.waitNs;t.leaseLockWrite+=l.stats.lockNs[1];t.leaseLockRead+=l.stats.lockNs[0];t.leaseWrites+=l.stats.count[1];t.leaseReads+=l.stats.count[0];
}
class FrameTraceWindow {
 static constexpr size_t capacity=1024;
 std::array<FrameSample,capacity> samples{};
 std::array<uint64_t,capacity> scratch{};
 uint64_t count=0,wallMax=0,draws=0,passes=0,encodeUnknown=0,gpuUnavailable=0;
 uint64_t mergedDraws=0;std::array<uint64_t,size_t(MergeSplit::count)> mergeSplits{};
 FrameCounts counts{};FrameSample totals{};std::array<uint64_t,9> cpus{};
 // Frame cadence vs 60 Hz ticks: <=1, <=2, <=3, >3 intervals of 16.7 ms.
 uint64_t lastCreated=0;std::array<uint64_t,4> cadence{};
public:
 void add(const FrameSample &s) {
  auto &stored=samples[count%capacity];stored=s;
  // Gaps over 100 ms are idle pauses, not cadence; they do not enter the histogram.
  if(s.createdNs && lastCreated && s.createdNs>lastCreated && s.createdNs-lastCreated<100000000){stored.interval=s.createdNs-lastCreated;uint64_t ticks=(stored.interval+8333333)/16666667;++cadence[ticks<=1?0:ticks==2?1:ticks==3?2:3];}
  if(s.createdNs)lastCreated=s.createdNs;++count;wallMax=std::max(wallMax,s.wall);
  draws+=s.draws;passes+=s.passes;counts.pipelines+=s.counts.pipelines;counts.descriptors+=s.counts.descriptors;counts.allocations+=s.counts.allocations;
  mergedDraws+=s.mergedDraws;for(size_t i=1;i<mergeSplits.size();++i)mergeSplits[i]+=s.mergeSplits[i];
  totals.cpuSampledSubmits+=s.cpuSampledSubmits;totals.cpuOmittedSubmits+=s.cpuOmittedSubmits;
  totals.textureShadowBytes+=s.textureShadowBytes;totals.textureRepitchedBytes+=s.textureRepitchedBytes;for(size_t i=0;i<totals.textureShadowUses.size();++i)totals.textureShadowUses[i]+=s.textureShadowUses[i];
  totals.snapshotBytes+=s.snapshotBytes;totals.shadowCopyBytes+=s.shadowCopyBytes;totals.shadowScannedIndices+=s.shadowScannedIndices;for(size_t i=0;i<totals.shadowFullReasons.size();++i)totals.shadowFullReasons[i]+=s.shadowFullReasons[i];
  totals.frontendDrawSamples+=s.frontendDrawSamples;totals.leaseWrites+=s.leaseWrites;totals.leaseReads+=s.leaseReads;
  totals.planHits+=s.planHits;totals.planMisses+=s.planMisses;
  totals.queueSubmits+=s.queueSubmits;totals.imageBarriers+=s.imageBarriers;totals.readBarriersSkipped+=s.readBarriersSkipped;totals.crossEncoderMerges+=s.crossEncoderMerges;totals.pipelineBarriers+=s.pipelineBarriers;totals.shadowRefreshSkipped+=s.shadowRefreshSkipped;totals.postBarriersSkipped+=s.postBarriersSkipped;for(size_t i=0;i<totals.layoutKinds.size();++i)totals.layoutKinds[i]+=s.layoutKinds[i];
  for(size_t i=0;i<totals.conflictKinds.size();++i)totals.conflictKinds[i]+=s.conflictKinds[i];for(size_t i=0;i<totals.renameDeclined.size();++i)totals.renameDeclined[i]+=s.renameDeclined[i];totals.shadowRenames+=s.shadowRenames;totals.shadowRenameBytes+=s.shadowRenameBytes;totals.drainWaits+=s.drainWaits;totals.conflictWaits+=s.conflictWaits;totals.slotWaits+=s.slotWaits;
  totals.shadowWaits+=s.shadowWaits;totals.drainSubmits+=s.drainSubmits;totals.prepareDrains+=s.prepareDrains;totals.prepareUnknownDrains+=s.prepareUnknownDrains;totals.prepareConflictWaits+=s.prepareConflictWaits;totals.indexUploadDrains+=s.indexUploadDrains;totals.indexGPUWritableDrains+=s.indexGPUWritableDrains;
  totals.indexUploadSubmits+=s.indexUploadSubmits;totals.indexReadOnlySubmits+=s.indexReadOnlySubmits;totals.indexGPUWritableSubmits+=s.indexGPUWritableSubmits;
  totals.snapshotReusedBytes+=s.snapshotReusedBytes;totals.shadowSkippedBytes+=s.shadowSkippedBytes;totals.shadowHashSkipped+=s.shadowHashSkipped;totals.shadowHashCopied+=s.shadowHashCopied;totals.shadowHashBytes+=s.shadowHashBytes;totals.shadowHashNs+=s.shadowHashNs;totals.renderAreaPasses+=s.renderAreaPasses;totals.renderAreaShrunk+=s.renderAreaShrunk;totals.renderAreaPixels+=s.renderAreaPixels;totals.renderAreaFullPixels+=s.renderAreaFullPixels;totals.passReorders+=s.passReorders;totals.sampleOptimal+=s.sampleOptimal;totals.sampleLinear+=s.sampleLinear;totals.sampleShadow+=s.sampleShadow;totals.sampleLinearBytes+=s.sampleLinearBytes;totals.sampleShadowBytes+=s.sampleShadowBytes;totals.sampleOptimalBytes+=s.sampleOptimalBytes;totals.rtIn+=s.rtIn;totals.rtInSkipped+=s.rtInSkipped;totals.rtOut+=s.rtOut;totals.rtSyncs+=s.rtSyncs;totals.rtEligible+=s.rtEligible;totals.rtLinearLarge+=s.rtLinearLarge;totals.rtBytes+=s.rtBytes;
  gpuUnavailable+=!s.gpuSamples;++cpus[s.cpu<8?s.cpu:8];encodeUnknown+=s.encodeCountsUnknown;
 }
 uint64_t size() const{return count;}
 uint64_t percentile(uint64_t FrameSample::*field,unsigned percent) {
  size_t n=size_t(std::min<uint64_t>(count,capacity));if(!n)return 0;
  for(size_t i=0;i<n;++i)scratch[i]=samples[i].*field;
  std::sort(scratch.begin(),scratch.begin()+n);return scratch[(n*percent+99)/100-1];
 }
 template<size_t N> void phaseLine(FILE *out,const char *title,const char *const (&names)[N],std::array<uint64_t,N> FrameSample::*ns,std::array<uint64_t,N> FrameSample::*calls) {
  size_t n=size_t(std::min<uint64_t>(count,capacity));if(!n)return;
  char line[640];int len=0;bool started=false,pending=false;
  auto head=[&](bool cont){len=snprintf(line,sizeof line,"FUXI MTL: %s%s n=%zu (avg/max ms/frame/calls)",title,cont?" cont":"",n);pending=false;};
  auto flush=[&]{if(pending){if(len>399)len=399;line[len]=0;fprintf(out,"%s\n",line);}pending=false;};
  for(size_t i=0;i<N;++i){
   uint64_t sum=0,max=0,c=0;for(size_t k=0;k<n;++k){uint64_t v=(samples[k].*ns)[i];sum+=v;max=std::max(max,v);c+=(samples[k].*calls)[i];}
   if(!c)continue;if(!started){head(false);started=true;}else if(len>200){flush();head(true);} // the device logger splits lines near 240 chars and drops bytes at the split
   len+=snprintf(line+len,sizeof line-size_t(len)," %s=%.2f/%.2f/%.1f",names[i],sum/double(n)/1e6,max/1e6,c/double(n));pending=true;
  }
  flush();
 }
 void reportPhases(FILE *out) {
  phaseLine(out,"encode phases",encPhaseNames,&FrameSample::encNs,&FrameSample::encCalls);
  phaseLine(out,"submit phases",subPhaseNames,&FrameSample::subNs,&FrameSample::subCalls);
  size_t n=size_t(std::min<uint64_t>(count,capacity));if(!n)return;
  char line[400];int len=snprintf(line,sizeof line,"FUXI MTL: scan counts n=%zu (avg/frame, max for bound_set_max)",n);
  for(size_t i=0;i<size_t(ScanKind::count) && len>0 && len<360;++i){uint64_t sum=0,max=0;for(size_t k=0;k<n;++k){sum+=samples[k].scans[i];max=std::max(max,samples[k].scans[i]);}
   len+=snprintf(line+len,sizeof line-size_t(len)," %s=%.1f",scanKindNames[i],i==size_t(ScanKind::boundSetMax)?double(max):sum/double(n));}
  if(len>399)len=399;line[len]=0;fprintf(out,"%s\n",line);
 }
 void report(FILE *out=stderr) {
  auto p=[&](uint64_t FrameSample::*field,unsigned percent){return percentile(field,percent)/1e6;};
  double n=count?double(count):1;
  flockfile(out); // Keep the histogram on the same line as its percentiles.
  fprintf(out,"FUXI MTL: frame trace n=%llu samples=%llu wall=%.3f/%.3f/%.3f ms cpu_before_commit=%.3f/%.3f shadow_copy=%.3f/%.3f lease_lock=%.3f/%.3f backend_submit=%.3f/%.3f gpu_wait=%.3f/%.3f gpu_exec=%.3f/%.3f draws/frame=%.2f render_passes/frame=%.2f pipelines/frame=%.2f descriptors/frame=%.2f allocations/frame=%.2f cpu_hist=",
   (unsigned long long)count,(unsigned long long)std::min<uint64_t>(count,capacity),
   p(&FrameSample::wall,50),p(&FrameSample::wall,90),wallMax/1e6,
   p(&FrameSample::cpuBeforeCommit,50),p(&FrameSample::cpuBeforeCommit,90),
   p(&FrameSample::shadowCopy,50),p(&FrameSample::shadowCopy,90),
   p(&FrameSample::leaseLock,50),p(&FrameSample::leaseLock,90),
   p(&FrameSample::backendSubmit,50),p(&FrameSample::backendSubmit,90),
   p(&FrameSample::gpuWait,50),p(&FrameSample::gpuWait,90),
   p(&FrameSample::gpuExec,50),p(&FrameSample::gpuExec,90),draws/n,passes/n,counts.pipelines/n,counts.descriptors/n,counts.allocations/n);
  for(unsigned i=0;i<8;++i)fprintf(out,"%s%u:%llu",i?",":"",i,(unsigned long long)cpus[i]);
  fprintf(out,",unknown:%llu encode_counts_unknown=%llu gpu_exec_unavailable=%llu",(unsigned long long)cpus[8],(unsigned long long)encodeUnknown,(unsigned long long)gpuUnavailable);
  {static uint64_t prevCompiles=0,prevNs=0;uint64_t c=syncCompiles.load(),ns=syncCompileNs.load();fprintf(out," sync_compiles=%llu sync_compile_ms=%.1f",(unsigned long long)(c-prevCompiles),double(ns-prevNs)/1e6);prevCompiles=c;prevNs=ns;}
  if(systemTranslationPolicy().overlap)fprintf(out," overlap_depth=%.0f/%.0f completion_ms=%.3f/%.3f worker_depth=%.0f/%.0f",double(percentile(&FrameSample::inflight,50)),double(percentile(&FrameSample::inflight,90)),p(&FrameSample::completionLatency,50),p(&FrameSample::completionLatency,90),double(percentile(&FrameSample::workerDepth,50)),double(percentile(&FrameSample::workerDepth,90)));
  if(systemTranslationPolicy().merge){fprintf(out," merged_draws/frame=%.2f merged_splits=",mergedDraws/n);
   for(size_t i=1;i<mergeSplits.size();++i)fprintf(out,"%s%s:%llu",i==1?"":",",mergeSplitNames[i],(unsigned long long)mergeSplits[i]);}
  fprintf(out," interval=%.3f/%.3f ms cadence_ticks=1:%llu,2:%llu,3:%llu,4+:%llu",p(&FrameSample::interval,50),p(&FrameSample::interval,90),(unsigned long long)cadence[0],(unsigned long long)cadence[1],(unsigned long long)cadence[2],(unsigned long long)cadence[3]);
  fprintf(out," submits/frame=%.2f image_barriers/frame=%.2f read_barriers_skipped/frame=%.2f cross_encoder_merges/frame=%.2f wait_reasons=drain:%llu,conflict:%llu,slots:%llu",totals.queueSubmits/n,totals.imageBarriers/n,totals.readBarriersSkipped/n,totals.crossEncoderMerges/n,(unsigned long long)totals.drainWaits,(unsigned long long)totals.conflictWaits,(unsigned long long)totals.slotWaits);
  fprintf(out," drain_submits=%llu drain_reasons=indices-upload:%llu,indices-gpu-writable:%llu,prepare:%llu,prepare-conflict:%llu,prepare-unknown:%llu index_submits=upload:%llu,readonly:%llu,gpu_writable:%llu shadow_waits=%llu snapshot_reused_bytes/frame=%.2f shadow_skipped_bytes/frame=%.2f",(unsigned long long)totals.drainSubmits,(unsigned long long)totals.indexUploadDrains,(unsigned long long)totals.indexGPUWritableDrains,(unsigned long long)totals.prepareDrains,(unsigned long long)totals.prepareConflictWaits,(unsigned long long)totals.prepareUnknownDrains,(unsigned long long)totals.indexUploadSubmits,(unsigned long long)totals.indexReadOnlySubmits,(unsigned long long)totals.indexGPUWritableSubmits,(unsigned long long)totals.shadowWaits,totals.snapshotReusedBytes/n,totals.shadowSkippedBytes/n);
  fprintf(out," conflict_kinds=buffer:%llu,shadow:%llu,texture:%llu,surface:%llu,index:%llu conflict_wait_for=gpu_flight display_lease_wait_ms/frame=%.3f shadow_renames/frame=%.2f shadow_rename_bytes/frame=%.0f rename_declined=off:%llu,not_backing:%llu,direct_use:%llu,texture:%llu,budget:%llu,other:%llu",(unsigned long long)totals.conflictKinds[0],(unsigned long long)totals.conflictKinds[1],(unsigned long long)totals.conflictKinds[2],(unsigned long long)totals.conflictKinds[3],(unsigned long long)totals.conflictKinds[4],totals.leaseExclWait/n/1e6,totals.shadowRenames/n,totals.shadowRenameBytes/n,(unsigned long long)totals.renameDeclined[0],(unsigned long long)totals.renameDeclined[1],(unsigned long long)totals.renameDeclined[2],(unsigned long long)totals.renameDeclined[3],(unsigned long long)totals.renameDeclined[4],(unsigned long long)totals.renameDeclined[5]);
  fprintf(out," pipeline_barriers/frame=%.2f shadow_refresh_skipped/frame=%.2f post_barriers_skipped/frame=%.2f layout_transitions/frame=",totals.pipelineBarriers/n,totals.shadowRefreshSkipped/n,totals.postBarriersSkipped/n);{static const char *k[]={"und","gen","ro","dst"};bool any=false;for(size_t i=0;i<16;++i)if(totals.layoutKinds[i]){fprintf(out,"%s%s>%s:%.2f",any?",":"",k[i/4],k[i%4],totals.layoutKinds[i]/n);any=true;}if(!any)fputc('-',out);}
  fprintf(out," plan_hits/frame=%.2f plan_misses/frame=%.2f",totals.planHits/n,totals.planMisses/n);
  fprintf(out," snapshot_bytes/frame=%.2f shadow_copy_bytes/frame=%.2f",totals.snapshotBytes/n,totals.shadowCopyBytes/n);
  fprintf(out," shadow_hash_skipped/frame=%.2f shadow_hash_copied/frame=%.2f shadow_hash_skipped_bytes/frame=%.0f shadow_hash_ms/frame=%.3f",totals.shadowHashSkipped/n,totals.shadowHashCopied/n,totals.shadowHashBytes/n,totals.shadowHashNs/n/1e6);
  fprintf(out," rt_shadow_in/frame=%.2f rt_shadow_in_skipped/frame=%.2f rt_shadow_out/frame=%.2f rt_shadow_syncs/frame=%.2f rt_shadow_mb/frame=%.2f rt_shadow_eligible/frame=%.2f rt_linear_large_targets/frame=%.2f",totals.rtIn/n,totals.rtInSkipped/n,totals.rtOut/n,totals.rtSyncs/n,totals.rtBytes/n/1048576.0,totals.rtEligible/n,totals.rtLinearLarge/n);
  fprintf(out," sampled/frame=optimal:%.2f,linear:%.2f,shadow:%.2f sampled_mb/frame=optimal:%.2f,linear:%.2f,shadow:%.2f",totals.sampleOptimal/n,totals.sampleLinear/n,totals.sampleShadow/n,totals.sampleOptimalBytes/n/1048576.0,totals.sampleLinearBytes/n/1048576.0,totals.sampleShadowBytes/n/1048576.0);
  fprintf(out," render_area_pct=%.1f render_area_shrunk/frame=%.2f pass_reorders/frame=%.2f",totals.renderAreaFullPixels?100.0*totals.renderAreaPixels/totals.renderAreaFullPixels:100.0,totals.renderAreaShrunk/n,totals.passReorders/n);
  fprintf(out," shadow_scanned_indices/frame=%.2f shadow_full_reasons=",totals.shadowScannedIndices/n);for(size_t i=0;i<totals.shadowFullReasons.size();++i)fprintf(out,"%s%s:%.2f",i?",":"",shadowFullReasonNames[i],totals.shadowFullReasons[i]/n);
  fprintf(out," texture_shadow_bytes/frame=%.2f texture_repitched_bytes/frame=%.2f texture_shadow_uses=sampled:%.2f,target:%.2f,other:%.2f",totals.textureShadowBytes/n,totals.textureRepitchedBytes/n,totals.textureShadowUses[0]/n,totals.textureShadowUses[1]/n,totals.textureShadowUses[2]/n);
  auto stage=[&](const char *name,uint64_t FrameSample::*field){fprintf(out," %s=%.3f/%.3f",name,p(field,50),p(field,90));};
  stage("sched_wait",&FrameSample::schedWait);stage("commit_call",&FrameSample::commitCall);stage("commit_gap",&FrameSample::commitGap);stage("admission_wait",&FrameSample::admissionWait);stage("snapshot",&FrameSample::snapshot);stage("submit_queue",&FrameSample::submitQueue);
  stage("backend_lock",&FrameSample::backendLock);stage("slot_wait",&FrameSample::slotWait);stage("flush",&FrameSample::flush);stage("queue_submit",&FrameSample::queueSubmit);stage("retire_queue",&FrameSample::retireQueue);stage("retire_lock",&FrameSample::retireLock);stage("invalidate",&FrameSample::invalidate);stage("retire_host",&FrameSample::retireHost);stage("surface_release",&FrameSample::surfaceRelease);stage("scheduled_handlers",&FrameSample::scheduledHandlers);
  stage("lease_front",&FrameSample::leaseFront);stage("lease_back",&FrameSample::leaseBack);stage("lease_excl_wait",&FrameSample::leaseExclWait);stage("lease_lock_write",&FrameSample::leaseLockWrite);stage("lease_lock_read",&FrameSample::leaseLockRead);
  fprintf(out," lease_surfaces/frame=write:%.2f,read:%.2f",totals.leaseWrites/n,totals.leaseReads/n);
  stage("prior_delivery",&FrameSample::priorDelivery);stage("prior_handlers",&FrameSample::priorHandlers);stage("prior_wait_completed",&FrameSample::priorWaitCompleted);
  stage("backend_cpu",&FrameSample::backendCPU);stage("frontend_cpu",&FrameSample::frontendCPU);stage("frontend_wall",&FrameSample::frontendWall);
  fprintf(out," frontend_draw_ns/frame=%.0f/%.0f frontend_draw_samples/frame=%.2f",double(percentile(&FrameSample::frontendDraw,50)),double(percentile(&FrameSample::frontendDraw,90)),totals.frontendDrawSamples/n);
  stage("pipe",&FrameSample::pipe);stage("desc",&FrameSample::desc);stage("record",&FrameSample::record);stage("kernel_io",&FrameSample::kernelIO);
  stage("backend_lock_cpu",&FrameSample::backendLockCPU);stage("slot_wait_cpu",&FrameSample::slotWaitCPU);
  stage("pipe_cpu",&FrameSample::pipeCPU);stage("desc_cpu",&FrameSample::descCPU);stage("record_cpu",&FrameSample::recordCPU);stage("flush_cpu",&FrameSample::flushCPU);stage("kernel_io_cpu",&FrameSample::kernelIOCPU);stage("queue_submit_cpu",&FrameSample::queueSubmitCPU);
  fprintf(out," cpu_sampled_submits/frame=%.2f cpu_omitted_submits/frame=%.2f",totals.cpuSampledSubmits/n,totals.cpuOmittedSubmits/n);
  fputc('\n',out);
  reportPhases(out);
  funlockfile(out);
  count=wallMax=draws=passes=encodeUnknown=gpuUnavailable=mergedDraws=0;mergeSplits.fill(0);counts={};totals={};cpus.fill(0);cadence.fill(0);
 }
};
}
