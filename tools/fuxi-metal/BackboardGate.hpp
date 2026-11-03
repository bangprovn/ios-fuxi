/* Boot-scoped crash recovery; no signal handler or mid-context CPU replay. */
#pragma once
#include "TransientMemory.hpp"
#include "SlabMemory.hpp"
#include "ClientStorage.hpp"
#include "FrameTrace.hpp"
#include "SamplingPolicy.hpp"
#include <array>
#include <string>
#include <chrono>
#include <atomic>
#include <memory>
#include <mutex>
#include <utility>
#include <sys/file.h>
namespace fuxi {
class BackboardGate {
 std::mutex mutex;
 int directory=-1,lease=-1;char boot[37]={};unsigned strikes=0;
 std::atomic<bool> active{false},failed{false};bool good=false;
 // completed() adds to frameTrace under the mutex. report() swaps in spareTrace there and formats the full window after
 // unlocking, so a retiring frame never waits for the sorting and printing (reportMutex: one report at a time).
 std::unique_ptr<FrameTraceWindow> frameTrace=std::make_unique<FrameTraceWindow>(),spareTrace;std::mutex reportMutex;
 LateFrameLog lateFrames; // FUXI_MTL_LATE_FRAMES: not swapped with the windows, so its rate limit and previous frame span reports
 uint64_t frames=0,late=0,failures=0,gpu=0,lastFrames=0,lastGPU=0;
 double began=0,lastReport=0;
 static double now(){return std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count();}
 bool store(const char *state) {
  // Fixed temp name is safe under the lifetime flock. Orphans are never read.
  unlinkat(directory,"gate.tmp",0);
  int fd=openat(directory,"gate.tmp",O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC|O_NOFOLLOW,0600);
  if(fd<0)return false;
  char record[96];int n=snprintf(record,sizeof(record),"FXBB1 %s %u %s\n",boot,strikes,state);
  bool ok=privateStorage(fd,false,geteuid()) && write(fd,record,size_t(n))==n && !fsync(fd);
  close(fd);if(ok)ok=!renameat(directory,"gate.tmp",directory,"gate.state");
  if(ok)ok=!fsync(directory);unlinkat(directory,"gate.tmp",0);return ok;
 }
 bool deny(const char *why) {
  fprintf(stderr,"FUXI MTL: backboardd gate deny reason=%s strikes=%u uid=%lu\n",why,strikes,(unsigned long)geteuid());return false;
 }
 bool persistDenied() {
  if(store("denied"))return true;
  // A full disk or blocked temp name must not leave an old 'good' reusable.
  // Invalidate the existing inode durably first; a partial rewrite fails closed.
  int fd=openat(directory,"gate.state",O_WRONLY|O_CLOEXEC|O_NOFOLLOW|O_NONBLOCK);
  if(fd<0)return false;
  char record[96];int n=snprintf(record,sizeof(record),"FXBB1 %s %u denied\n",boot,strikeLimit);
  bool ok=privateStorage(fd,false,geteuid()) && !ftruncate(fd,0) && !fsync(fd) &&
   write(fd,record,size_t(n))==n && !fsync(fd);
  close(fd);return ok;
 }
public:
 static constexpr unsigned goodFrames=120,strikeLimit=1;
 static bool validBoot(const char *s) {
  if(!s || strlen(s)!=36)return false;
  for(unsigned i=0;i<36;++i) {
   if(i==8 || i==13 || i==18 || i==23){if(s[i]!='-')return false;}
   else if(!((s[i]>='0' && s[i]<='9') || (s[i]>='a' && s[i]<='f') || (s[i]>='A' && s[i]<='F')))return false;
  }return true;
 }
 // Takes ownership of storage; separate argument permits host filesystem tests.
 bool admit(const char *uuid,int storage) {
  std::lock_guard<std::mutex> guard(mutex);directory=storage;
  if(!validBoot(uuid))return deny("boot-uuid-unavailable");
  memcpy(boot,uuid,37);for(char &c:boot)if(c>='A' && c<='F')c=char(c-'A'+'a');
  if(!privateStorage(directory,true,geteuid()))return deny("storage-permissions");
  lease=openat(directory,"gate.lock",O_RDWR|O_CREAT|O_CLOEXEC|O_NOFOLLOW|O_NONBLOCK,0600);
  if(!privateStorage(lease,false,geteuid()) || flock(lease,LOCK_EX|LOCK_NB))return deny("lease-unavailable");
  int fd=openat(directory,"gate.state",O_RDONLY|O_CLOEXEC|O_NOFOLLOW|O_NONBLOCK);
  if(fd<0 && errno!=ENOENT)return deny("record-open");
  if(fd>=0) {
   char data[128]={},oldBoot[37]={},state[16]={};unsigned previous=0;int end=0;
   ssize_t n=privateStorage(fd,false,geteuid())?read(fd,data,sizeof(data)-1):-1;close(fd);
   if(n<=0 || sscanf(data,"FXBB1 %36s %u %15s%n",oldBoot,&previous,state,&end)!=3 ||
      end+1!=n || data[end]!='\n' || !validBoot(oldBoot) || previous>strikeLimit ||
      (strcmp(state,"armed") && strcmp(state,"good") && strcmp(state,"denied")))return deny("record-invalid");
   if(!strcmp(oldBoot,boot)) {
    strikes=previous;
    if(!strcmp(state,"armed") && strikes<strikeLimit)++strikes;
    if(!strcmp(state,"denied"))strikes=strikeLimit;
   }else fprintf(stderr,"FUXI MTL: backboardd gate new-boot reset\n");
  }
  if(strikes>=strikeLimit){store("denied");return deny("strikes");}
  if(!store("armed"))return deny("record-write");
  active=true;began=lastReport=now();
  fprintf(stderr,"FUXI MTL: backboardd gate allow armed strikes=%u uid=%lu good_frames=%u\n",strikes,(unsigned long)geteuid(),goodFrames);return true;
 }
 bool admit() {char uuid[64]={};systemString("kern.bootsessionuuid",uuid,sizeof(uuid));return admit(uuid,openClientStorage("backboardd"));}
 bool blocked() const {return active.load() && failed.load();}
 // Survey mode keeps allocating after the latch so later refusals still surface.
 std::atomic<bool> surveying{false};
 bool refusesResources() const {return blocked() && !surveying.load();}
 void fault() {
  if(!active.load())return;
  std::lock_guard<std::mutex> guard(mutex);if(!active)return;++failures;if(failed)return;
  failed=true;strikes=strikeLimit;bool saved=persistDenied();
  fprintf(stderr,"FUXI MTL: backboardd gate runtime-failure next-respawn=CPU persisted=%u\n",unsigned(saved));
 }
 void completed(uint64_t gpuNanos,double wallMS,const FrameSample *sample=nullptr) {
  if(!active.load())return;
  char lateText[1536];bool lateLine=false;
  {std::lock_guard<std::mutex> guard(mutex);if(!active || failed)return;
  if(sample){uint64_t interval=frameTrace->add(*sample);if(systemTranslationPolicy().lateFrames)lateLine=lateFrames.note(*sample,interval,lateText,sizeof lateText);}
  ++frames;gpu+=gpuNanos;if(wallMS>1000.0/60.0)++late;
  if(frames==1)fprintf(stderr,"FUXI MTL: backboardd first_frame_ms=%.3f gpu_ms=%.3f\n",(now()-began)*1000,gpuNanos/1e6);
  if(!good && frames>=goodFrames) {
   if(store("good")){good=true;fprintf(stderr,"FUXI MTL: backboardd gate good frames=%llu\n",(unsigned long long)frames);}
   else {failed=true;strikes=strikeLimit;bool saved=persistDenied();fprintf(stderr,"FUXI MTL: backboardd gate deny reason=good-write next-respawn=CPU persisted=%u\n",unsigned(saved));}
  }
  }
  // After the gate lock, as report() prints: a retiring frame never waits for stderr's lock under it. Pieces of 200 chars, as the window line.
  if(lateLine){flockfile(stderr);wrapLine(stderr,lateText,"FUXI MTL: late frame+");funlockfile(stderr);}
 }
 // log2 bucket:count pairs for non-zero buckets, or "-".
 static std::string classCounts(const std::array<std::atomic<uint64_t>,40> &a){std::string r;char b[40];for(size_t i=0;i<a.size();++i)if(auto v=a[i].load()){snprintf(b,sizeof b,"%s%zu:%llu",r.empty()?"":",",i,(unsigned long long)v);r+=b;}return r.empty()?"-":r;}
 // name:count pairs for non-zero pool-miss causes, or "-".
 static std::string causeCounts(){std::string r;char b[48];for(unsigned i=0;i<missCauses;++i)if(auto v=texturePoolMissCause[i].load()){snprintf(b,sizeof b,"%s%s:%llu",r.empty()?"":",",missCauseNames[i],(unsigned long long)v);r+=b;}return r.empty()?"-":r;}
 void report() {
  if(!active.load())return;
  std::lock_guard<std::mutex> serial(reportMutex);
  const bool trace=systemTranslationPolicy().frameTrace;
  if(trace && !spareTrace)spareTrace=std::make_unique<FrameTraceWindow>(); // allocated before taking the gate lock
  uint64_t count,gpuDelta,frameCount,lateCount,failureCount;double span;
  {std::lock_guard<std::mutex> guard(mutex);double t=now();if(!active || t-lastReport<5)return;
   count=frames-lastFrames;gpuDelta=gpu-lastGPU;span=t-lastReport;frameCount=frames;lateCount=late;failureCount=failures;
   if(trace){std::swap(frameTrace,spareTrace);frameTrace->continueFrom(*spareTrace);}
   lastReport=t;lastFrames=frames;lastGPU=gpu;}
  // Reuse completed-frame GPU timestamps. Completion-window attribution can
  // straddle report boundaries; overlapping work can exceed one wall interval.
  // This is compositor timestamp occupancy, not a whole-engine busy counter.
  double busy=100.0*double(gpuDelta)/1e9/span;if(busy>100.0)busy=100.0;
  if(count && !gpuDelta)busy=-1.0; // completed frames without timestamp data
  // Opt-in counters, cumulative like the pool counts; empty with their knobs off. FUXI_MTL_DESC_REUSE and FUXI_MTL_BIND_SKIP: FrameTrace.hpp.
  char extra[384]="";int extraLen=0;
  if(systemTranslationPolicy().descReuse)extraLen+=snprintf(extra,sizeof extra," desc_reuse=hits:%llu,new:%llu,fallbacks:%llu,forgotten:%llu,rotations:%llu,blocked:%llu",
   (unsigned long long)descReuseHits.load(),(unsigned long long)descReuseNew.load(),(unsigned long long)descReuseFallbacks.load(),(unsigned long long)descReuseForgotten.load(),(unsigned long long)descReuseRotations.load(),(unsigned long long)descReuseBlocked.load());
  // skipped/asked per kind
  if(systemTranslationPolicy().bindSkip && extraLen>=0 && size_t(extraLen)<sizeof extra)snprintf(extra+extraLen,sizeof extra-size_t(extraLen)," bind_skip=push:%llu/%llu,vertex:%llu/%llu,index:%llu/%llu",
   (unsigned long long)bindPushSkips.load(),(unsigned long long)bindPushCalls.load(),(unsigned long long)bindVertexSkips.load(),(unsigned long long)bindVertexCalls.load(),(unsigned long long)bindIndexSkips.load(),(unsigned long long)bindIndexCalls.load());
  fprintf(stderr,"FUXI MTL: backboardd frames=%llu gpu_ms=%.3f late=%llu failures=%llu sampling_skipped=%llu hz=%.2f window_frames=%llu gpu_busy_est_pct=%.1f tex_pool_hits=%llu tex_pool_misses=%llu tex_pool_trims=%llu tex_pool_bytes=%llu tex_pool_wasted=%llu tex_pool_idle_trims=%llu tex_pool_pressure_trims=%llu tex_pool_reject_full=%llu tex_pool_reject_budget=%llu tex_pool_class_hits=%s tex_pool_class_misses=%s tex_pool_miss_causes=%s slab_hits=%llu slab_grows=%llu slab_grow_cold=%llu slab_grow_frag=%llu slab_grow_full=%llu slab_grow_refused=%llu slab_frees=%llu slab_trims=%llu slab_bytes=%llu slab_free_bytes=%llu buf_pool_hits=%llu buf_pool_misses=%llu buf_pool_trims=%llu buf_pool_bytes=%llu surface_import_hits=%llu surface_import_misses=%llu%s\n",
   (unsigned long long)frameCount,count?gpuDelta/1e6/double(count):0.0,(unsigned long long)lateCount,
   (unsigned long long)failureCount,(unsigned long long)skippedSamplingDraws.load(),double(count)/span,(unsigned long long)count,busy,(unsigned long long)texturePoolHits.load(),(unsigned long long)texturePoolMisses.load(),(unsigned long long)texturePoolTrims.load(),(unsigned long long)texturePoolBytes.load(),(unsigned long long)texturePoolWasted.load(),(unsigned long long)texturePoolIdleTrims.load(),(unsigned long long)texturePoolPressureTrims.load(),(unsigned long long)texturePoolRejectFull.load(),(unsigned long long)texturePoolRejectBudget.load(),classCounts(texturePoolClassHits).c_str(),classCounts(texturePoolClassMisses).c_str(),causeCounts().c_str(),(unsigned long long)slabHits.load(),(unsigned long long)slabGrows.load(),(unsigned long long)slabGrowCold.load(),(unsigned long long)slabGrowFrag.load(),(unsigned long long)slabGrowFull.load(),(unsigned long long)slabGrowRefused.load(),(unsigned long long)slabFrees.load(),(unsigned long long)slabTrims.load(),(unsigned long long)slabBytes.load(),(unsigned long long)slabFreeBytes.load(),(unsigned long long)bufferPoolHits.load(),(unsigned long long)bufferPoolMisses.load(),(unsigned long long)bufferPoolTrims.load(),(unsigned long long)bufferPoolBytes.load(),(unsigned long long)surfaceImportHits.load(),(unsigned long long)surfaceImportMisses.load(),extra);
  if(trace)spareTrace->report();
 }
 BackboardGate()=default;
 BackboardGate(const BackboardGate&)=delete;BackboardGate& operator=(const BackboardGate&)=delete;
 ~BackboardGate(){if(lease>=0)close(lease);if(directory>=0)close(directory);}
};
// The dispatch reporter and lease live until process death, including during
// static teardown. Local test instances still close their descriptors normally.
inline BackboardGate &backboardGate=*new BackboardGate;
}
