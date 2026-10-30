/* Original Fuxi diagnostic frontend. Vulkan headers/source remain external. */
#pragma once
#include "FrontendEncoding.hpp"
#define VK_NO_PROTOTYPES
#include <vulkan/vulkan.h>
#include <dlfcn.h>
#include <algorithm>
#include <deque>
#include <array>
#include <map>
#include <set>
#include <tuple>
#include <sys/stat.h>
#include <unistd.h>
#include <cerrno>
#include <cstdlib>
#include <cfloat>
#include <cmath>
#include <cstring>
#include <mutex>
#include <atomic>
#include <chrono>
#include <thread>
#include <condition_variable>
#include <sys/resource.h>
#ifdef __APPLE__
#include <dispatch/dispatch.h>
#endif
#include "ResourceLimits.hpp"
#include "TransientMemory.hpp"
#include "SlabMemory.hpp"
#include "PassTimes.hpp"
#include "ClientSetup.hpp"
#include "DrawSafety.hpp"
#include "TextureLevels.hpp"
#include "MipAccess.hpp"
#include "SamplingPolicy.hpp"
#include "PixelFormats.hpp"
#include "ObjectCache.hpp"
#include "HostImportRange.hpp"
#include <functional>
#include <memory>
#include <optional>
#include <stdexcept>
#include <vector>
#include <unordered_map>
#include <unordered_set>
#include "Translator.hpp"
#include "DrawPlanCache.hpp"
#include "CompileBoost.hpp"
#include "PipelineCache.hpp"
#include "ArtifactCache.hpp"
#include "SpirvRelax.hpp"
#include "SpirvLoops.hpp"
#include "SpirvEval.hpp"
#include "OcclusionProof.hpp"
#include "PrewarmStore.hpp"
#include "BackboardGate.hpp"
#include "FramebufferFetch.hpp"
#include "VertexProfile.hpp"
#include "../../xnu/fuxi-adreno/NativeAdrenoABI.h"
static_assert(NativeAdrenoAbiVersion==2,"FuxiMetal HostImport requires NativeAdreno ABI 2");
#ifndef FUXI_MTL_ICD
#define FUXI_MTL_ICD "/usr/local/lib/libvulkan_freedreno.dylib"
#endif
namespace fuxi {
inline bool hostRangesOverlap(const void *a,size_t an,const void *b,size_t bn){
 if(!a || !b || !an || !bn)return false;auto x=(uintptr_t)a,y=(uintptr_t)b;return x<=y?y-x<an:x-y<bn;
}
// Live Vulkan memory objects of this process: each is one kernel BO, a per-client-limited resource.
inline std::atomic<int64_t> liveMemoryObjects{0},liveMemoryBytes{0};
// The line names the failing call; the counters tell a BO quota refusal from real exhaustion.
inline void check(VkResult r,int line=__builtin_LINE()) {
 if(r!=VK_SUCCESS)backboardGate.fault();
 if(r!=VK_SUCCESS) throw std::runtime_error("Vulkan rc "+std::to_string(r)+" at Backend.hpp:"+std::to_string(line)+" (memory objects "+std::to_string(liveMemoryObjects.load())+", "+std::to_string(liveMemoryBytes.load()>>10)+" KiB)");
}
// A host-pointer import the caller falls back from (to a shadow copy) is not a GPU failure:
// throw without latching backboardd onto the CPU.
inline void importCheck(VkResult r,int line=__builtin_LINE()) {
 if(r!=VK_SUCCESS) throw std::runtime_error("Vulkan rc "+std::to_string(r)+" at Backend.hpp:"+std::to_string(line)+" (host import)");
}
#define FUXI_VK_FUNCTIONS(X) \
 X(CreateQueryPool) X(DestroyQueryPool) X(GetQueryPoolResults) X(CmdResetQueryPool) X(CmdWriteTimestamp) \
 X(DestroyDevice) X(GetDeviceQueue) X(CreateBuffer) X(DestroyBuffer) X(GetBufferMemoryRequirements) \
 X(AllocateMemory) X(FreeMemory) X(BindBufferMemory) X(MapMemory) X(UnmapMemory) \
 X(FlushMappedMemoryRanges) X(InvalidateMappedMemoryRanges) X(CreateCommandPool) X(DestroyCommandPool) \
 X(ResetCommandPool) X(ResetFences) X(ResetDescriptorPool) X(AllocateCommandBuffers) X(BeginCommandBuffer) X(EndCommandBuffer) X(CmdFillBuffer) \
 X(CmdCopyBuffer) X(CmdClearColorImage) X(CreateImage) X(DestroyImage) X(GetImageMemoryRequirements) X(BindImageMemory) \
 X(GetImageSubresourceLayout) X(CmdCopyBufferToImage) X(CmdBlitImage) X(CmdCopyImage) X(CreateSampler) X(DestroySampler) \
 X(CreateDescriptorSetLayout) X(DestroyDescriptorSetLayout) X(CreateDescriptorPool) X(DestroyDescriptorPool) \
 X(AllocateDescriptorSets) X(UpdateDescriptorSets) X(CmdBindDescriptorSets) X(CreateImageView) X(DestroyImageView) X(CreateRenderPass) X(DestroyRenderPass) \
 X(CreateFramebuffer) X(DestroyFramebuffer) X(CreateShaderModule) X(DestroyShaderModule) \
 X(CreatePipelineLayout) X(DestroyPipelineLayout) X(CreateGraphicsPipelines) X(DestroyPipeline) \
 X(CreatePipelineCache) X(DestroyPipelineCache) X(GetPipelineCacheData) \
 X(CmdBindVertexBuffers) X(CmdBindIndexBuffer) X(CmdDrawIndexed) X(CmdSetViewport) X(CmdSetScissor) X(CmdPushConstants) X(CmdBeginRenderPass) X(CmdEndRenderPass) X(CmdBindPipeline) X(CmdDraw) X(CmdCopyImageToBuffer) X(CmdPipelineBarrier) X(CreateFence) X(DestroyFence) X(QueueSubmit) X(WaitForFences)
struct Buffer { VkBuffer buffer{}; VkDeviceMemory memory{}; void *contents{};void *key{};Buffer *spare{};void *shadowKey() const{return key?key:contents;}bool shadowBacking=false,directUse=false,spareFailed=false,aliased=false; size_t length{}, allocated{},charged{},memoryBytes{},memoryOffset{},references=1;uint32_t memoryType{};bool imported=false,upload=false,poolable=false,gpuWritten=false; };
// Host ranges a submit's prepare callback will write; absent means unknown (full drain).
struct PrepareWrite {Buffer *buffer;uint64_t offset,bytes;};
struct Texture : TextureLevels {VkImage image{};VkDeviceMemory memory{};VkImageView view{};size_t allocated{},charged{},memoryBytes{},references=1;
 uint32_t memoryType{};VkImageUsageFlags usage{};uint32_t format=80;VkFormat vkFormat=VK_FORMAT_B8G8R8A8_UNORM;
 // layout is level zero; levelLayouts[1..levels) track the remaining mips.
 bool rtTarget=false; // FUXI_MTL_RT_SHADOW: this is the persistent optimal-tiled render target of a large linear image (owned by Backend::rtShadows, keyed by the linear memory)
 VkImageTiling tiling=VK_IMAGE_TILING_OPTIMAL;VkImageLayout layout=VK_IMAGE_LAYOUT_UNDEFINED;std::array<VkImageLayout,32> levelLayouts{};void *mapped{};bool imported=false,borrowedMemory=false;
 MemoryKey recycleKey{};bool recyclable=false,slabbed=false;uint64_t memOffset{},memExtent{};SlabMemory<VkDeviceMemory>::Block slabBlock{};std::array<uint64_t,7> importKey{};bool importCached=false;
 Buffer *backingBuffer{};size_t backingOffset{},backingRow{};bool shadow=false;uint64_t shadowHash{};bool shadowHashed=false; // hash of the backing rows the last recorded refresh copied
 std::vector<VkImageView> attachmentViews;bool canGenerate=false,filterable=true,blendable=false;};
struct Sampler {size_t references=1;VkSampler sampler{};uint32_t filters=1;float lodMin=0,lodMax=0;};
struct VariantKey {
 std::array<uint32_t,resourceSlots*8> words{};
 explicit VariantKey(const DrawInput &input){for(unsigned i=0;i<resourceSlots;++i){auto *w=&words[i*8];w[0]=input.formats[i];w[1]=input.filters[i];memcpy(w+2,&input.lodMin[i],4);memcpy(w+3,&input.lodMax[i],4);for(unsigned stage=0;stage<2;++stage){w[4+stage*2]=input.buffers[stage][i].length!=0;w[5+stage*2]=input.buffers[stage][i].bytes;}}}
 bool operator==(const VariantKey &other) const {return words==other.words;}
};
struct Pipeline {uint32_t id{},fragInstr{},fragSamples{},fragLoops{},fragKills{};ShadowWindow shadowWindows[2][resourceSlots]{};std::array<uint32_t,resourceSlots> textureTypes{};size_t references=1,retainedBytes=0;bool writesStorage=false;void *translation{};CompileOptions options{};uint32_t fetchMask{};bool orderedColorAccess=false;std::vector<std::pair<uint32_t,VkRenderPass>> renders;VkPipeline pipeline{},stripPipeline{};std::array<VkPipeline,2> linePipelines{};Translation recipe{};VkPipelineLayout layout{};VkRenderPass render{};uint32_t pushOffset{},pushSize{},pushStages{};uint8_t pushBytes[128]{};VkDescriptorSetLayout setLayout{};VkSampler bakedSamplers[16]{};BakedSampler bakedSamplerInfo[16]{};MipAccess mipAccess[2];uint32_t readOnlyStorage[2]{};std::vector<Descriptor> descriptors;std::vector<Buffer *> constantBlobs;std::vector<std::string> blobKeys;std::vector<std::array<uint8_t,32>> blobHashes;std::vector<std::pair<VariantKey,Pipeline *>> variants;
};
struct BufferBind {Buffer *buffer{};uint64_t offset{};bool bytes=false;uint32_t end{};
 uint64_t length() const{return end?end:buffer?buffer->length:0;}};
struct ColorTarget {Texture *texture{};uint32_t level{},load=2,store=1;float clear[4]{};};
struct Operation {
 Buffer *src, *dst; size_t from, to, size; uint8_t value;
 Texture *texture{};Pipeline *pipeline{};float clear[4]{};bool renderDraw=false;
 Texture *sourceTexture{};Sampler *sampler{};uint32_t vertices=3;bool upload=false,clearOnly=false;
 uint32_t srcX{},srcY{},dstX{},dstY{},width{},height{};size_t rowBytes{};uint32_t srcZ{},dstZ{},depth{};size_t imageBytes{};
 Texture *samples[resourceSlots]{};Sampler *samplers[resourceSlots]{};BufferBind buffers[2][resourceSlots]{};Buffer *indices{};uint64_t indexOffset{};uint32_t first{},load=2,store=1,indexWidth=2;Topology topology=Topology::Triangle;
 VkViewport viewport{0,256,256,-256,0,1};VkRect2D scissor{{0,0},{256,256}};
 // Metal renderTargetWidth/Height: the pass's top-left render area; 0 means the whole target.
 uint32_t areaWidth{},areaHeight{};
 uint32_t srcLevel{},dstLevel{},sampleBase[resourceSlots]{},sampleCount[resourceSlots]{};bool generateMips=false;uint32_t mipBase{},mipCount{};
 OperationDiagnostic diagnostic{};// texture/dstLevel also name the first present target when slot 0 is absent.
 uint32_t colorCount=1,colorMask=1;ColorTarget colors[7]{};
 // Command-local Metal encoder identity. Zero is unknown and never merges.
 // The frontend rejects nonzero slices/depth planes and multisampling.
 uint64_t encoderID{};
 ColorTarget color(unsigned i) const {if(!(colorMask&(1u<<i)))return {};if(i)return colors[i-1];ColorTarget c{texture,dstLevel,load,store,{}};memcpy(c.clear,clear,sizeof(clear));return c;}
 void setColor(unsigned i,const ColorTarget &c){if(i){colors[i-1]=c;return;}texture=c.texture;dstLevel=c.level;load=c.load;store=c.store;memcpy(clear,c.clear,sizeof(clear));}

};
class Backend {
 // A device loss from another API must poison flights before its cleanup runs.
 void check(VkResult result,int line=__builtin_LINE()) {
  if(result==VK_ERROR_DEVICE_LOST)poisoned=true;
  fuxi::check(result,line);
 }
 void *library{};
 VkInstance instance{}; VkPhysicalDevice physical{}; VkDevice device{}; VkQueue queue{};
 bool independentBlend=false;uint32_t maxColorAttachments=1;float maxAnisotropy=1;
 PipelineCache pipelineCache;
 ArtifactCache artifactCache;CacheMode cacheMode=systemCacheMode();
 Translator::CacheScope artifactCallbacks{};Translator::CacheScopeVerified artifactCallbacksVerified{};
 bool creationFeedback=false,cacheStopping=false;
 std::thread cacheWorker;std::condition_variable cacheWake;
 // Startup prewarm. Recipes of pipelines compiled in earlier runs (stored beside the translation cache) are recompiled on a low-priority thread as soon as
 // both of their libraries exist; a draw that needs one meanwhile waits for that single compile (compiling set), never starting a second.
 struct PrewarmLib {void *lib;LibraryHash hash;unsigned pins;};
 std::mutex prewarmMutex;std::condition_variable prewarmWake;std::vector<PrewarmLib> prewarmLibs;std::vector<Recipe> prewarmRecipes;std::deque<size_t> prewarmQueue;
 std::thread prewarmThread;bool prewarmStopping=false,prewarmRunning=false;uint64_t prewarmDone=0,prewarmHitCount=0,prewarmFailed=0;double prewarmMs=0;
 std::unordered_set<std::string> recipeKeys,compiling;std::condition_variable compileDone;
 static inline thread_local bool prewarmWorker=false,unlockForCompile=false;static inline thread_local int lastCompileWasHit=0;
#ifdef __APPLE__
 dispatch_source_t memoryPressure{};dispatch_semaphore_t pressureStopped{};
 void startMemoryPressure(){
  pressureStopped=dispatch_semaphore_create(0);
  if(!pressureStopped)return;
  memoryPressure=dispatch_source_create(DISPATCH_SOURCE_TYPE_MEMORYPRESSURE,0,DISPATCH_MEMORYPRESSURE_WARN|DISPATCH_MEMORYPRESSURE_CRITICAL,dispatch_get_global_queue(DISPATCH_QUEUE_PRIORITY_LOW,0));
  if(!memoryPressure){dispatch_release(pressureStopped);pressureStopped=nullptr;return;}
  dispatch_set_context(memoryPressure,this);
  dispatch_source_set_event_handler_f(memoryPressure,[](void *p){static_cast<Backend *>(p)->trimMemory();});
  dispatch_source_set_cancel_handler_f(memoryPressure,[](void *p){dispatch_semaphore_signal(static_cast<Backend *>(p)->pressureStopped);});dispatch_resume(memoryPressure);
 }
 void stopMemoryPressure(){
  if(!memoryPressure)return;
  dispatch_source_cancel(memoryPressure);dispatch_semaphore_wait(pressureStopped,DISPATCH_TIME_FOREVER);
  dispatch_release(memoryPressure);dispatch_release(pressureStopped);memoryPressure=nullptr;pressureStopped=nullptr;
 }
#endif
 using CacheClock=std::chrono::steady_clock;
 CacheClock::time_point cacheActivity{},cacheCheckpoint=CacheClock::now();
 struct CacheActivity {CacheClock::time_point *last;~CacheActivity(){if(last)*last=CacheClock::now();}};
 uint64_t driverHits=0,driverMisses=0,driverUnknown=0,objectHits=0,objectMisses=0;
 bool cachePeriodUnlocked(bool closing=false) noexcept {
  if(cacheMode!=CacheMode::persistent || !device || poisoned || backboardGate.blocked())return false;
  auto now=CacheClock::now();
  // Prefer a quiet render lane. Continuous animation still gets a bounded
  // checkpoint every 30s; Vulkan cache access remains serialized.
  bool checkpoint=false;
  if(closing || now-cacheActivity>=std::chrono::milliseconds(250) || now-cacheCheckpoint>=std::chrono::seconds(30)){
   pipelineCache.checkpoint();
   if(backboardGate.blocked())return false;
   checkpoint=true;cacheCheckpoint=CacheClock::now();
  }
  artifactCache.report();
  fprintf(stderr,"FUXI MTL: cache period driver hits=%llu misses=%llu unknown=%llu objects hits=%llu misses=%llu live=%zu evictions=%zu bytes=%zu blobs=%zu blob_shared=%llu mesa_cache=%s\n",
   (unsigned long long)driverHits,(unsigned long long)driverMisses,(unsigned long long)driverUnknown,
   (unsigned long long)objectHits,(unsigned long long)objectMisses,objects.count(),objects.evictions,objects.totalBytes(),blobShare.size(),(unsigned long long)blobSharedHits,mesaStatus[0]?mesaStatus:"off");
  {size_t variants=0,largest=0,largestVariants=0;objects.each([&](Pipeline *p,size_t n){variants+=p->variants.size();if(n>largest){largest=n;largestVariants=p->variants.size();}});
   fprintf(stderr,"FUXI MTL: cache period object bytes live=%zu variants=%zu avg_kb=%zu largest_kb=%zu largest_variants=%zu (entry bytes include runtime variants: each variant is a full pipeline, up to 8 per entry)\n",objects.count(),variants,objects.count()?objects.totalBytes()/objects.count()/1024:0,largest/1024,largestVariants);}
  driverHits=driverMisses=driverUnknown=objectHits=objectMisses=0;return checkpoint;
 }
 char mesaStatus[96]{};std::map<std::string,std::pair<Buffer *,size_t>> blobShare;uint64_t blobSharedHits=0;
 // 256 pipelines: the old 32-entry LRU evicted every prewarmed pipeline before its first draw
 ObjectCache<Pipeline,256,192u*1024*1024> objects;ResourceLimits limits;DrawPlanCache<Pipeline> drawPlans;
 TransientMemory<VkDeviceMemory> textureMemory;SlabMemory<VkDeviceMemory> slabMemory;
public:
 bool slabsEnabled=!systemTranslationPolicy().noSlab;
 static constexpr size_t bufferPoolEntries=64,bufferPoolLimit=16u<<20,bufferPoolMaxBuffer=1u<<20;static constexpr uint64_t bufferPoolIdleNs=30000000000ull;
 bool buffersPooled=!systemTranslationPolicy().noSlab;
 // Two textures overlap in memory only if they share an allocation and (for sub-allocations) an offset range.
 static bool memoryAlias(const Texture *a,const Texture *b){return a->memory==b->memory && (!a->slabbed || !b->slabbed || (a->memOffset<b->memOffset+b->memExtent && b->memOffset<a->memOffset+a->memExtent));}
private:
 static uint64_t memoryNow(){return std::chrono::duration_cast<std::chrono::nanoseconds>(CacheClock::now().time_since_epoch()).count();}
 // Small plain host buffers are kept for exact-size reuse (CA recreates the same vertex/uniform sizes every frame); bounded, idle-trimmed, charged against the budget while pooled.
 struct PooledBuffer {VkBuffer buffer;VkDeviceMemory memory;void *contents;size_t classSize,allocated,memoryBytes,charged;uint32_t memoryType;uint64_t idleSince;};
 std::vector<PooledBuffer> bufferPool;size_t bufferPoolUsed=0;
 void freePooledBuffer(const PooledBuffer &e){frameKernelCall([&]{return UnmapMemory(device,e.memory);});DestroyBuffer(device,e.buffer,nullptr);frameKernelCall([&]{FreeMemory(device,e.memory,nullptr);});--liveMemoryObjects;liveMemoryBytes-=e.memoryBytes;limits.release(e.charged);bufferPoolUsed-=e.classSize;bufferPoolBytes=bufferPoolUsed;++bufferPoolTrims;}
 size_t trimBuffers(uint64_t now,bool all,size_t want=SIZE_MAX){
  size_t freed=0;for(size_t i=0;i<bufferPool.size() && freed<want;){if(all || want!=SIZE_MAX || now-bufferPool[i].idleSince>bufferPoolIdleNs){freed+=bufferPool[i].charged;auto e=bufferPool[i];bufferPool.erase(bufferPool.begin()+i);freePooledBuffer(e);}else ++i;}
  return freed;
 }
 void trimMemoryUnlocked(bool all=false,uint64_t now=memoryNow()){if(all){releaseRtShadows();releaseRenamePool();releaseSampleShadows();}
  if(poisoned)return;
  trimBuffers(now,all);
  textureMemory.trim(now,all,[&](VkDeviceMemory h,size_t bytes){freePooledUnlocked(h,bytes);});
  slabMemory.trim(now,all,[&](VkDeviceMemory h,size_t bytes){freePooledUnlocked(h,bytes);});
 }
 void freePooledUnlocked(VkDeviceMemory h,size_t bytes){frameKernelCall([&]{FreeMemory(device,h,nullptr);});--liveMemoryObjects;liveMemoryBytes-=bytes;limits.release(bytes);}
 // Optional features (rename pool, render target shadows) allocate with optionalAlloc set: they may only use budget minus optionalHeadroom, so CA's own textures always keep room.
 // A CA allocation that would be rejected first sheds the pool, then the shadows (never while a submit is rewriting ops that point at them), and only then fails.
 static constexpr size_t optionalHeadroom=64u<<20;
 bool optionalAlloc=false,inSubmit=false;uint64_t optionalRefusals=0,optionalSheds=0;
 void reserveMemoryUnlocked(size_t bytes){
  const size_t cap=optionalAlloc?ResourceLimits::budget-optionalHeadroom:ResourceLimits::budget;
  auto shortBy=[&]()->size_t{return limits.used+bytes>cap?limits.used+bytes-cap:0;};
  auto reclaim=[&](){
   if(shortBy() && !poisoned)trimBuffers(0,false,shortBy());
   if(shortBy() && !poisoned){textureMemory.reclaim(shortBy(),[&](VkDeviceMemory h,size_t n){freePooledUnlocked(h,n);});
    if(shortBy())slabMemory.reclaim(shortBy(),[&](VkDeviceMemory h,size_t n){freePooledUnlocked(h,n);});}};
  reclaim();
  if(shortBy() && !optionalAlloc && !inSubmit && !poisoned){
   if(!sampleShadows.empty()){releaseSampleShadows();++optionalSheds;reclaim();}
   if(!renamePool.empty()){releaseRenamePool();++optionalSheds;reclaim();}
   if(shortBy() && !rtShadows.empty()){releaseRtShadows();++optionalSheds;reclaim();}
   if(optionalSheds)fprintf(stderr,"FUXI MTL: budget pressure shed optional memory (sheds=%llu used=%zu need=%zu)\n",(unsigned long long)optionalSheds,limits.used,bytes);
  }
  if(optionalAlloc && shortBy()){++optionalRefusals;throw std::runtime_error("optional feature headroom");}
  limits.reserve(bytes);
 }
 struct OptionalScope {Backend &b;bool old;explicit OptionalScope(Backend &x):b(x),old(x.optionalAlloc){b.optionalAlloc=true;}~OptionalScope(){b.optionalAlloc=old;}};
 // Two independently resettable lanes. A busy lane belongs to its retirement.
 // kind bits name the resource for conflict diagnostics: 1 buffer, 2 shadow backing, 4 texture, 8 surface, 16 index.
 struct HostUse {VkDeviceMemory memory;const void *address;size_t bytes;uint8_t kind=1;Buffer *buffer=nullptr;};
 struct SubmitSlot {
  VkCommandPool commandPool{};VkCommandBuffer command{};VkFence submitFence{};VkQueryPool queryPool{},passPool{},drawPool{};
  Buffer *aliasScratch=nullptr;VkDescriptorPool descriptorPool{};uint32_t descriptorSets{};
  std::vector<VkDescriptorPoolSize> descriptorCapacity;std::vector<HostUse> hostUses,writes,uses;bool busy=false;
 } slots[2];
 std::condition_variable submitWake;
 static constexpr size_t aliasScratchLimit=1024*1024;
 struct FrameEntry {std::array<Texture *,8> textures;VkRenderPass render;std::array<uint32_t,8> levels;VkFramebuffer frame;};
 struct ViewEntry {Texture *texture;VkFormat format;std::array<uint32_t,4> components;uint32_t base,count;VkImageView view;};
 std::vector<FrameEntry> frameCache;std::unordered_multimap<uint64_t,size_t> frameIndex;std::vector<ViewEntry> viewCache;std::unordered_multimap<uint64_t,size_t> viewIndex; // key: texture/format/components/range hash -> viewCache position
 static constexpr size_t frameCacheLimit=512,viewCacheLimit=512;
 static uint64_t frameKey(const std::array<Texture *,8> &t,VkRenderPass render,const std::array<uint32_t,8> &levels){uint64_t h=0xcbf29ce484222325ull;for(size_t i=0;i<8;++i)h=((h^uint64_t(uintptr_t(t[i])))*0x100000001b3ull^levels[i])*0x100000001b3ull;return (h^uint64_t(uintptr_t(render)))*0x100000001b3ull;}
 static uint64_t viewKey(const Texture *t,VkFormat format,const std::array<uint32_t,4> &c,uint32_t base,uint32_t count){uint64_t h=0xcbf29ce484222325ull;for(uint64_t x:{uint64_t(uintptr_t(t)),uint64_t(format),uint64_t(c[0])|uint64_t(c[1])<<8|uint64_t(c[2])<<16|uint64_t(c[3])<<24,uint64_t(base)<<32|count})h=(h^x)*0x100000001b3ull;return h;}
 static constexpr uint32_t passStampLimit=255;static inline PassTimes passTimes;uint64_t drawSampleSerial=0;
 void readDrawTimes(VkQueryPool pool,const std::vector<DrawInfo> &infos){
  if(!pool || infos.empty())return;std::vector<uint64_t> ticks(infos.size()+1),delta(infos.size());
  if(GetQueryPoolResults(device,pool,0,uint32_t(ticks.size()),ticks.size()*sizeof(uint64_t),ticks.data(),sizeof(uint64_t),VK_QUERY_RESULT_64_BIT)!=VK_SUCCESS)return;
  uint64_t mask=timestampBits>=64?UINT64_MAX:(uint64_t(1)<<timestampBits)-1;
  for(size_t i=0;i<infos.size();++i)delta[i]=uint64_t(double((ticks[i+1]-ticks[i])&mask)*timestampPeriod);
  reportDraws(stderr,infos,delta);if(systemTranslationPolicy().occlude)occNoteDrawTimes(infos,delta);
 }
 void readPassTimes(VkQueryPool pool,const std::vector<PassInfo> &infos,uint64_t whole){
  if(!pool || infos.empty())return;std::vector<uint64_t> ticks(infos.size()+1),delta(infos.size());
  if(GetQueryPoolResults(device,pool,0,uint32_t(ticks.size()),ticks.size()*sizeof(uint64_t),ticks.data(),sizeof(uint64_t),VK_QUERY_RESULT_64_BIT)!=VK_SUCCESS)return;
  uint64_t mask=timestampBits>=64?UINT64_MAX:(uint64_t(1)<<timestampBits)-1;
  for(size_t i=0;i<infos.size();++i)delta[i]=uint64_t(double((ticks[i+1]-ticks[i])&mask)*timestampPeriod);
  passTimes.add(infos,delta,whole);if(passTimes.submitCount()>=300){reportRtCounters();passTimes.report(stderr);}
 }
 // Fast 4-lane multiply-xor hash over a pitched rectangle of bytes (change detection only, not security).
 static uint64_t hashRows(const uint8_t *base,size_t rows,size_t pitch,size_t rowBytes){
  uint64_t a=0x9E3779B97F4A7C15ull,b=0xC2B2AE3D27D4EB4Full,c=0x165667B19E3779F9ull,d=0x27D4EB2F165667C5ull;
  auto mix=[](uint64_t h,uint64_t x){h=(h^x)*0x9E3779B97F4A7C15ull;return h^(h>>29);};
  for(size_t r=0;r<rows;++r){const uint8_t *p=base+r*pitch;size_t n=rowBytes;
   while(n>=32){uint64_t x[4];memcpy(x,p,32);a=mix(a,x[0]);b=mix(b,x[1]);c=mix(c,x[2]);d=mix(d,x[3]);p+=32;n-=32;}
   if(n){uint64_t x[4]{};memcpy(x,p,n);a=mix(a,x[0]);b=mix(b,x[1]);c=mix(c,x[2]);d=mix(d,x[3]^n);}
  }
  return mix(mix(mix(mix(a,b),c),d),uint64_t(rows)<<32^rowBytes);
 }
 void clearSubmitObjects(SubmitSlot &slot) {
  auto &descriptorPool=slot.descriptorPool;auto &queryPool=slot.queryPool;auto &submitFence=slot.submitFence;auto &commandPool=slot.commandPool;auto &command=slot.command;auto &descriptorSets=slot.descriptorSets;auto &descriptorCapacity=slot.descriptorCapacity;
  if(descriptorPool)DestroyDescriptorPool(device,descriptorPool,nullptr);
  if(queryPool)DestroyQueryPool(device,queryPool,nullptr);if(slot.passPool)DestroyQueryPool(device,slot.passPool,nullptr);slot.passPool={};if(slot.drawPool)DestroyQueryPool(device,slot.drawPool,nullptr);slot.drawPool={};
  if(submitFence)DestroyFence(device,submitFence,nullptr);
  if(commandPool)DestroyCommandPool(device,commandPool,nullptr);
  descriptorPool={};queryPool={};submitFence={};commandPool={};command={};descriptorSets=0;descriptorCapacity.clear();
 }
 void clearFrames(Texture *texture=nullptr,VkRenderPass render=VK_NULL_HANDLE) {
  for(auto i=frameCache.begin();i!=frameCache.end();) {
   if((!texture && !render) || (texture && std::find(i->textures.begin(),i->textures.end(),texture)!=i->textures.end()) || (render && i->render==render)){DestroyFramebuffer(device,i->frame,nullptr);i=frameCache.erase(i);}else ++i;
  }
  frameIndex.clear();for(size_t n=0;n<frameCache.size();++n)frameIndex.emplace(frameKey(frameCache[n].textures,frameCache[n].render,frameCache[n].levels),n);
 }
 void clearViews(Texture *texture=nullptr) {
  for(auto i=viewCache.begin();i!=viewCache.end();) {
   if(!texture || i->texture==texture){DestroyImageView(device,i->view,nullptr);i=viewCache.erase(i);}else ++i;
  }
  viewIndex.clear();for(size_t n=0;n<viewCache.size();++n)viewIndex.emplace(viewKey(viewCache[n].texture,viewCache[n].format,viewCache[n].components,viewCache[n].base,viewCache[n].count),n);
 }
 bool coherent(uint32_t type) const {return memory.memoryTypes[type].propertyFlags&VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;}

 uint32_t timestampBits=0;float timestampPeriod=0;
 VkPhysicalDeviceMemoryProperties memory{}; uint32_t family{};
 bool orderedColorAccess=false;bool hostImport=false;VkDeviceSize hostAlignment{};VkFormatFeatureFlags linearFeatures{},optimalFeatures{};
 PFN_vkGetPhysicalDeviceFormatProperties GetPhysicalDeviceFormatProperties{};
 PFN_vkGetPhysicalDeviceExternalBufferProperties GetPhysicalDeviceExternalBufferProperties{};
 PFN_vkGetMemoryHostPointerPropertiesEXT GetMemoryHostPointerPropertiesEXT{};
#ifdef VK_KHR_pipeline_executable_properties
 PFN_vkGetPipelineExecutablePropertiesKHR GetPipelineExecutablePropertiesKHR{};PFN_vkGetPipelineExecutableStatisticsKHR GetPipelineExecutableStatisticsKHR{};
#endif
 bool executableStats=false;uint32_t deviceFeatures=0;
 PFN_vkGetPhysicalDeviceImageFormatProperties2 GetPhysicalDeviceImageFormatProperties2{};
 PFN_vkDestroyInstance DestroyInstance{};PFN_vkGetInstanceProcAddr gipa{};
#define FIELD(n) PFN_vk##n n{};
 FUXI_VK_FUNCTIONS(FIELD)
#undef FIELD
public:
 mutable std::mutex mutex;
 std::atomic<bool> poisoned{false};
 static constexpr size_t maxBufferLength=ResourceLimits::single;
 static inline std::atomic<uint64_t> pipelineHits{0},pipelineMisses{0},gpuNanos{0},gpuSamples{0};
 static inline auto &pipelineCacheLoadResult=PipelineCache::loadResult,&pipelineCacheLoadBytes=PipelineCache::loadBytes;
 static inline auto &pipelineCacheSaveResult=PipelineCache::saveResult,&pipelineCacheSaveBytes=PipelineCache::saveBytes;
 static inline auto &pipelineCacheSaveCount=PipelineCache::saveCount,&pipelineCacheSaveFailures=PipelineCache::saveFailures;
 // CPU cost of submit(): entry to QueueSubmit (incl. the lock), the fence wait, and retirement.
 static inline std::atomic<uint64_t> submits{0},submitPrepareNanos{0},submitWaitNanos{0},submitRetireNanos{0},submitSlotWaitNanos{0},submitBlocked{0},submitBlockedFull{0},submitBlockedDrain{0},submitBlockedConflict{0},submitEntered{0};
 static inline std::atomic<uint64_t> submitObjectsNanos{0},submitDescriptorsNanos{0},submitRecordNanos{0},shadowCopyNanos{0},submitFlushNanos{0},submitInvalidateNanos{0};
 static inline std::atomic<uint64_t> flushCalls{0},invalidateCalls{0},emptySubmits{0},submitObjectCreates{0},submitPoolCreates{0},submitFrameCreates{0},submitViewCreates{0};
 std::map<std::array<uint64_t,7>,Texture *> imports;
 struct ReuseCounts {uint64_t commandPools{},commandBuffers{},fences{},queries{},descriptors{},frames{},views{};} reuseCounts;
#ifdef FUXI_MTL_HOST_TEST
 // Test-only reference mode and deterministic recording failure; no production environment switches.
 bool reuseSubmitObjects=true,reuseDrawPlans=true,captureDrawPlans=false,failBeforeQueue=false,failQueueAttempt=false;
 std::vector<std::pair<Pipeline *,DrawOutput>> drawPlansForTest;
 void loseWhileInFlightForTest(){std::lock_guard<std::mutex> lock(mutex);check(VK_ERROR_DEVICE_LOST);}
 struct ImageTransition {Texture *texture;uint32_t level;VkImageLayout before,after;};
 struct SampleViewUse {Texture *texture;uint32_t base,count;VkImageLayout layout;};
 std::vector<ImageTransition> imageTransitions;std::vector<SampleViewUse> sampleViewUses;
 void trimMemoryForTest(uint64_t now){std::lock_guard<std::mutex> guard(mutex);trimMemoryUnlocked(false,now);}
 size_t cachedFrames() const{return frameCache.size();}
 size_t cachedViews() const{return viewCache.size();}
 void dropPipelineCacheForTest(){std::lock_guard<std::mutex> guard(mutex);requireLive();objects.clear([&](Pipeline *p){drawPlans.invalidate(p);destroyPipelineUnlocked(p);});}
 void discardReuseForTest(){std::lock_guard<std::mutex> guard(mutex);requireLive();for(auto &slot:slots)clearSubmitObjects(slot);clearFrames();clearViews();}
#endif
 void trimMemory(){std::lock_guard<std::mutex> guard(mutex);trimMemoryUnlocked(true);}
 uint32_t maxTextureDimension() const{return limits.dimension;}
 uint64_t optionalShedCount() const{return optionalSheds;}
 size_t resourceBytes() const{std::lock_guard<std::mutex> guard(mutex);return limits.used;}
 bool supportsTimestamps() const{return timestampBits && timestampPeriod>0;}
 explicit Backend(const char *client="unknown") {
  setenv("TU_DARWIN_SERVICE","NativeAdrenoGpuPublished",1);
  if(const char *mb=getenv("FUXI_MTL_PIPELINE_CACHE_MB")){long v=atol(mb);if(v>=8 && v<=1024)objects.byteLimit=size_t(v)<<20;} // host heap bound of the pipeline object cache (default 192)
  setenv("TU_DARWIN_OPEN_TYPE","0",1);setupTurnip();
  {const char *mesa=getenv("FUXI_MTL_MESA_DISK_CACHE"); // experiment: let Mesa/Turnip keep its own shader cache under the fuxi-metal directory (any uid; status is repeated in the cache period line because boot-time lines can be lost)
   if(mesa && !strcmp(mesa,"1")){ // same private per-client directory as the translation and pipeline caches (the parent is root-owned, so /private/var/db itself is not writable for uid 501)
    int dir=openClientStorage(client),made=-1,ok=0,e=0;char path[160]{};
    if(dir>=0){made=mkdirat(dir,"mesa",0700);e=made?errno:0;snprintf(path,sizeof path,"/private/var/db/fuxi-metal/%s-%lu/mesa",client,(unsigned long)geteuid());struct stat st{};ok=(!made||e==EEXIST)&&!stat(path,&st)&&access(path,W_OK)==0;::close(dir);}
    unsetenv("MESA_SHADER_CACHE_DISABLE");unsetenv("MESA_GLSL_CACHE_DISABLE");
    if(ok){setenv("MESA_SHADER_CACHE_DIR",path,1);setenv("MESA_GLSL_CACHE_DIR",path,1);setenv("MESA_SHADER_CACHE_MAX_SIZE","64M",0);}
    snprintf(mesaStatus,sizeof mesaStatus,"dir_ok=%d uid=%d storage_fd=%d mkdir=%d/%d",ok,int(geteuid()),dir,made,e);fprintf(stderr,"FUXI MTL: mesa disk cache %s\n",mesaStatus);}}setenv("MESA_LOG_LEVEL","warning",0);  // info logs a line per submit; env may raise it
  try {
   library=dlopen(FUXI_MTL_ICD,RTLD_NOW|RTLD_LOCAL);
   if(!library) throw std::runtime_error("dlopen Turnip failed");
   gipa=(PFN_vkGetInstanceProcAddr)dlsym(library,"vk_icdGetInstanceProcAddr");
   if(!gipa) throw std::runtime_error("missing ICD entrypoint");
#define INSTANCE(n) auto n=(PFN_vk##n)gipa(instance,"vk" #n); if(!n) throw std::runtime_error("missing vk" #n)
   INSTANCE(CreateInstance);
   VkApplicationInfo app{};app.sType=VK_STRUCTURE_TYPE_APPLICATION_INFO;app.pApplicationName="fuxi-metal";app.apiVersion=VK_API_VERSION_1_2;
   VkInstanceCreateInfo ci{};ci.sType=VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO;ci.pApplicationInfo=&app;
   check(CreateInstance(&ci,nullptr,&instance));
   DestroyInstance=(PFN_vkDestroyInstance)gipa(instance,"vkDestroyInstance");
   if(!DestroyInstance) throw std::runtime_error("missing vkDestroyInstance");
   INSTANCE(EnumeratePhysicalDevices);INSTANCE(GetPhysicalDeviceMemoryProperties);
   INSTANCE(GetPhysicalDeviceProperties);INSTANCE(GetPhysicalDeviceQueueFamilyProperties);GetPhysicalDeviceFormatProperties=(PFN_vkGetPhysicalDeviceFormatProperties)gipa(instance,"vkGetPhysicalDeviceFormatProperties");if(!GetPhysicalDeviceFormatProperties)throw std::runtime_error("missing format query");
   INSTANCE(GetPhysicalDeviceFeatures2);INSTANCE(CreateDevice);INSTANCE(GetDeviceProcAddr);INSTANCE(EnumerateDeviceExtensionProperties);INSTANCE(GetPhysicalDeviceProperties2);
   GetPhysicalDeviceExternalBufferProperties=(PFN_vkGetPhysicalDeviceExternalBufferProperties)gipa(instance,"vkGetPhysicalDeviceExternalBufferProperties");
   GetPhysicalDeviceImageFormatProperties2=(PFN_vkGetPhysicalDeviceImageFormatProperties2)gipa(instance,"vkGetPhysicalDeviceImageFormatProperties2");
   uint32_t n=0;check(EnumeratePhysicalDevices(instance,&n,nullptr));
   if(n!=1) throw std::runtime_error("expected one physical device");
   check(EnumeratePhysicalDevices(instance,&n,&physical));
   VkPhysicalDeviceProperties props{};GetPhysicalDeviceProperties(physical,&props);
#ifndef FUXI_MTL_HOST_TEST
   if(props.apiVersion<VK_API_VERSION_1_2)throw std::runtime_error("Vulkan 1.2 required");
   if(props.vendorID!=0x5143 || props.deviceID!=0x43050a01) throw std::runtime_error("unexpected GPU");
#endif
#ifdef FUXI_MTL_CA_OFFLINE
   if(props.deviceType!=VK_PHYSICAL_DEVICE_TYPE_CPU || strncmp(props.deviceName,"llvmpipe",8))throw std::runtime_error("offline qualifier requires lavapipe CPU device");
#endif
   limits=ResourceLimits(props.limits.maxImageDimension2D);timestampPeriod=props.limits.timestampPeriod;
   fprintf(stderr,"FUXI MTL: texture limits dimension=%u budget=%zu MiB\n",limits.dimension,ResourceLimits::budget/(1024*1024));
   GetPhysicalDeviceMemoryProperties(physical,&memory);
   VkFormatProperties format{};GetPhysicalDeviceFormatProperties(physical,VK_FORMAT_B8G8R8A8_UNORM,&format);
   linearFeatures=format.linearTilingFeatures;optimalFeatures=format.optimalTilingFeatures;
   n=0;GetPhysicalDeviceQueueFamilyProperties(physical,&n,nullptr);
   if(!n || n>64) throw std::runtime_error("invalid queue family count");
   std::vector<VkQueueFamilyProperties> families(n);GetPhysicalDeviceQueueFamilyProperties(physical,&n,families.data());
   while(family<n && (!families[family].queueCount || !(families[family].queueFlags&VK_QUEUE_GRAPHICS_BIT))) ++family;
   if(family==n) throw std::runtime_error("no transfer queue");
   timestampBits=families[family].timestampValidBits;
   float priority=1;VkDeviceQueueCreateInfo qi{};qi.sType=VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO;qi.queueFamilyIndex=family;qi.queueCount=1;qi.pQueuePriorities=&priority;
   VkDeviceCreateInfo di{};di.sType=VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO;di.queueCreateInfoCount=1;di.pQueueCreateInfos=&qi;
   uint32_t extCount=0;check(EnumerateDeviceExtensionProperties(physical,nullptr,&extCount,nullptr));
   if(extCount>4096)throw std::runtime_error("extension count");
   std::vector<VkExtensionProperties> extensions(extCount);check(EnumerateDeviceExtensionProperties(physical,nullptr,&extCount,extensions.data()));
   for(const auto &ext:extensions)if(!strcmp(ext.extensionName,VK_EXT_EXTERNAL_MEMORY_HOST_EXTENSION_NAME))hostImport=true;
   std::vector<const char *> enabledExtensions;
   if(cacheMode==CacheMode::persistent)for(const auto &ext:extensions)if(!strcmp(ext.extensionName,VK_EXT_PIPELINE_CREATION_FEEDBACK_EXTENSION_NAME)){
    creationFeedback=true;enabledExtensions.push_back(VK_EXT_PIPELINE_CREATION_FEEDBACK_EXTENSION_NAME);break;
   }
   for(const auto &ext:extensions)if(!strcmp(ext.extensionName,VK_EXT_RASTERIZATION_ORDER_ATTACHMENT_ACCESS_EXTENSION_NAME))orderedColorAccess=true;
   VkPhysicalDeviceRasterizationOrderAttachmentAccessFeaturesEXT order{};
   order.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_RASTERIZATION_ORDER_ATTACHMENT_ACCESS_FEATURES_EXT;
   if(orderedColorAccess) {
    VkPhysicalDeviceFeatures2 supported{};supported.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2;supported.pNext=&order;
    GetPhysicalDeviceFeatures2(physical,&supported);orderedColorAccess=order.rasterizationOrderColorAttachmentAccess;
    order.rasterizationOrderDepthAttachmentAccess=order.rasterizationOrderStencilAttachmentAccess=VK_FALSE;
    if(orderedColorAccess){enabledExtensions.push_back(VK_EXT_RASTERIZATION_ORDER_ATTACHMENT_ACCESS_EXTENSION_NAME);di.pNext=&order;}
   }
#ifdef VK_KHR_pipeline_executable_properties
   VkPhysicalDevicePipelineExecutablePropertiesFeaturesKHR exec{};exec.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PIPELINE_EXECUTABLE_PROPERTIES_FEATURES_KHR;
   if(systemTranslationPolicy().pipeStats){
    bool have=false;for(const auto &ext:extensions)if(!strcmp(ext.extensionName,VK_KHR_PIPELINE_EXECUTABLE_PROPERTIES_EXTENSION_NAME))have=true;
    if(have){VkPhysicalDeviceFeatures2 supported{};supported.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2;supported.pNext=&exec;GetPhysicalDeviceFeatures2(physical,&supported);
     if(exec.pipelineExecutableInfo){exec.pNext=const_cast<void *>(di.pNext);di.pNext=&exec;enabledExtensions.push_back(VK_KHR_PIPELINE_EXECUTABLE_PROPERTIES_EXTENSION_NAME);executableStats=true;}}
    fprintf(stderr,"FUXI MTL: pipeline executable statistics %s\n",executableStats?"enabled":"unavailable");
   }
#endif
   // 16-bit float/storage, int16 and robustness: enable what the GPU reports (kill switches FUXI_MTL_F16=0 / FUXI_MTL_ROBUST=0). The same mask goes to the translator.
   VkPhysicalDeviceVulkan11Features have11{};have11.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_1_FEATURES;VkPhysicalDeviceVulkan12Features have12{};have12.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_2_FEATURES;
   VkPhysicalDeviceRobustness2FeaturesEXT haveRob{};haveRob.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_ROBUSTNESS_2_FEATURES_EXT;bool robExt=false;
   for(const auto &ext:extensions)if(!strcmp(ext.extensionName,VK_EXT_ROBUSTNESS_2_EXTENSION_NAME))robExt=true;
   VkPhysicalDeviceFeatures2 core{};core.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2;core.pNext=&have11;have11.pNext=&have12;if(robExt)have12.pNext=&haveRob;GetPhysicalDeviceFeatures2(physical,&core);
   const auto &policy=systemTranslationPolicy();
   if(policy.f16){if(have12.shaderFloat16)deviceFeatures|=featFloat16;if(have11.storageBuffer16BitAccess)deviceFeatures|=featStorage16;if(have11.uniformAndStorageBuffer16BitAccess)deviceFeatures|=featUniform16;if(core.features.shaderInt16)deviceFeatures|=featInt16;}
   if(policy.robust){if(core.features.robustBufferAccess)deviceFeatures|=featRobustBuffer;if(robExt && haveRob.robustBufferAccess2)deviceFeatures|=featRobustBuffer2;if(robExt && haveRob.robustImageAccess2)deviceFeatures|=featRobustImage2;}
   VkPhysicalDeviceVulkan11Features en11{};en11.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_1_FEATURES;VkPhysicalDeviceVulkan12Features en12{};en12.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_2_FEATURES;
   VkPhysicalDeviceRobustness2FeaturesEXT enRob{};enRob.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_ROBUSTNESS_2_FEATURES_EXT;
   en12.shaderFloat16=(deviceFeatures&featFloat16)?VK_TRUE:VK_FALSE;en11.storageBuffer16BitAccess=(deviceFeatures&featStorage16)?VK_TRUE:VK_FALSE;en11.uniformAndStorageBuffer16BitAccess=(deviceFeatures&featUniform16)?VK_TRUE:VK_FALSE;
   enRob.robustBufferAccess2=(deviceFeatures&featRobustBuffer2)?VK_TRUE:VK_FALSE;enRob.robustImageAccess2=(deviceFeatures&featRobustImage2)?VK_TRUE:VK_FALSE;
   {const void *chain=di.pNext;if(deviceFeatures&(featRobustBuffer2|featRobustImage2)){enRob.pNext=const_cast<void *>(chain);chain=&enRob;enabledExtensions.push_back(VK_EXT_ROBUSTNESS_2_EXTENSION_NAME);}
    en12.pNext=const_cast<void *>(chain);en11.pNext=&en12;di.pNext=&en11;}
   fprintf(stderr,"FUXI MTL: device features mask=0x%x float16=%d storage16=%d uniform16=%d int16=%d robust_buffer=%d robust_buffer2=%d robust_image2=%d (supported: f16=%d s16=%d u16=%d i16=%d rb=%d rb2=%d ri2=%d)\n",deviceFeatures,!!(deviceFeatures&featFloat16),!!(deviceFeatures&featStorage16),!!(deviceFeatures&featUniform16),!!(deviceFeatures&featInt16),!!(deviceFeatures&featRobustBuffer),!!(deviceFeatures&featRobustBuffer2),!!(deviceFeatures&featRobustImage2),
    int(have12.shaderFloat16),int(have11.storageBuffer16BitAccess),int(have11.uniformAndStorageBuffer16BitAccess),int(core.features.shaderInt16),int(core.features.robustBufferAccess),int(robExt && haveRob.robustBufferAccess2),int(robExt && haveRob.robustImageAccess2));
   VkPhysicalDeviceFeatures enables{};enables.independentBlend=core.features.independentBlend;enables.samplerAnisotropy=core.features.samplerAnisotropy;enables.shaderInt16=(deviceFeatures&featInt16)?VK_TRUE:VK_FALSE;enables.robustBufferAccess=(deviceFeatures&featRobustBuffer)?VK_TRUE:VK_FALSE;di.pEnabledFeatures=&enables;
   // Without the feature an anisotropic Metal sampler degrades to plain filtering.
   maxAnisotropy=enables.samplerAnisotropy?std::max(1.f,props.limits.maxSamplerAnisotropy):1;
   independentBlend=enables.independentBlend;maxColorAttachments=std::min(8u,props.limits.maxColorAttachments);
   fprintf(stderr,"FUXI MTL: ordered framebuffer fetch %s (input attachments)\n",orderedColorAccess?"enabled":"unavailable");
   if(hostImport) {
    VkPhysicalDeviceExternalMemoryHostPropertiesEXT hp{};hp.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_EXTERNAL_MEMORY_HOST_PROPERTIES_EXT;
    VkPhysicalDeviceProperties2 p2{};p2.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2;p2.pNext=&hp;GetPhysicalDeviceProperties2(physical,&p2);
    hostAlignment=hp.minImportedHostPointerAlignment;
#ifndef FUXI_MTL_HOST_TEST
    if(hostAlignment!=NativeAdrenoAbiHostPageBytes)throw std::runtime_error("NativeAdreno ABI 2 HostImport requires 16K pages");
#endif
    enabledExtensions.push_back(VK_EXT_EXTERNAL_MEMORY_HOST_EXTENSION_NAME);
   }
   di.enabledExtensionCount=enabledExtensions.size();di.ppEnabledExtensionNames=enabledExtensions.data();
   check(CreateDevice(physical,&di,nullptr,&device));
   GetMemoryHostPointerPropertiesEXT=(PFN_vkGetMemoryHostPointerPropertiesEXT)GetDeviceProcAddr(device,"vkGetMemoryHostPointerPropertiesEXT");
#ifdef VK_KHR_pipeline_executable_properties
   if(executableStats){GetPipelineExecutablePropertiesKHR=(PFN_vkGetPipelineExecutablePropertiesKHR)GetDeviceProcAddr(device,"vkGetPipelineExecutablePropertiesKHR");GetPipelineExecutableStatisticsKHR=(PFN_vkGetPipelineExecutableStatisticsKHR)GetDeviceProcAddr(device,"vkGetPipelineExecutableStatisticsKHR");if(!GetPipelineExecutablePropertiesKHR || !GetPipelineExecutableStatisticsKHR)executableStats=false;}
#endif
#define LOAD(n) n=(PFN_vk##n)GetDeviceProcAddr(device,"vk" #n);if(!n) throw std::runtime_error("missing vk" #n);
   FUXI_VK_FUNCTIONS(LOAD)
#undef LOAD
#undef INSTANCE
   VkPhysicalDeviceDriverProperties driver{};driver.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_DRIVER_PROPERTIES;
   VkPhysicalDeviceIDProperties id{};id.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_ID_PROPERTIES;driver.pNext=&id;
   VkPhysicalDeviceProperties2 identity{};identity.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2;identity.pNext=&driver;
   GetPhysicalDeviceProperties2(physical,&identity);
   uint8_t translator[32]{};bool identified=Translator::cacheIdentity(translator);
   pipelineCache.open(device,CreatePipelineCache,DestroyPipelineCache,GetPipelineCacheData,props,driver,id,identified?translator:nullptr,client,cacheMode);
   if(cacheMode==CacheMode::persistent){
    if(identified)artifactCallbacks=Translator::get().artifactScope;
    artifactCallbacksVerified=Translator::get().artifactScopeVerified;
    if(artifactCallbacksVerified)artifactCache.setVerifier(Translator::get().artifactVerify);
    if(artifactCallbacks){artifactCache.open(props,driver,id,translator,client);
     for(auto &blob:artifactCache.recipes()){Recipe r;if(decodeRecipe(blob.data(),blob.size(),r))prewarmRecipes.push_back(std::move(r));}
     fprintf(stderr,"FUXI MTL: prewarm recipes loaded=%zu\n",prewarmRecipes.size());}
    else fprintf(stderr,"FUXI MTL: translation cache unavailable (bridge capability)\n");
    fprintf(stderr,"FUXI MTL: persistent caches enabled period=5s driver_feedback=%u\n",unsigned(creationFeedback));
   }
   requireLive();
   GetDeviceQueue(device,family,0,&queue);if(!queue) throw std::runtime_error("null queue");
#ifdef __APPLE__
   startMemoryPressure();
#endif
   try {
    cacheWorker=std::thread([this]{std::unique_lock<std::mutex> lock(mutex);auto lastPeriod=CacheClock::now();
     while(!cacheWake.wait_for(lock,std::chrono::seconds(1),[this]{return cacheStopping;})){
      trimMemoryUnlocked();bool checkpoint=false;
      if(CacheClock::now()-lastPeriod>=std::chrono::seconds(5)){checkpoint=cachePeriodUnlocked();lastPeriod=CacheClock::now();}
      lock.unlock();if(checkpoint)artifactCache.checkpoint();lock.lock();
     }});
   }catch(...){fprintf(stderr,"FUXI MTL: maintenance timer unavailable; trim on allocation/shutdown\n");}
  } catch(...) {close();throw;}
 }
 void close() {
#ifdef __APPLE__
  stopMemoryPressure();
#endif
  // Join before device/cache destruction, including poisoned contexts. A sleeping
  // worker owns no GPU resource and wakes immediately on shutdown.
  {{std::lock_guard<std::mutex> guard(prewarmMutex);prewarmStopping=true;}prewarmWake.notify_all();if(prewarmThread.joinable())prewarmThread.join();}
  if(cacheWorker.joinable()){{std::lock_guard<std::mutex> guard(mutex);cacheStopping=true;}cacheWake.notify_all();cacheWorker.join();}
  {std::unique_lock<std::mutex> lock(mutex);submitWake.wait(lock,[&]{return !slots[0].busy && !slots[1].busy;});}
  if(poisoned) return; // Unknown GPU ownership: leave reclamation to process death/KMD containment.
  cachePeriodUnlocked(true);
  if(!backboardGate.blocked())artifactCache.checkpoint();
  artifactCache.close();
  for(auto &slot:slots)clearSubmitObjects(slot);clearFrames();clearViews();for(auto &slot:slots){destroyUnlocked(slot.aliasScratch);slot.aliasScratch=nullptr;}
  objects.clear([&](Pipeline *p){drawPlans.invalidate(p);destroyPipelineUnlocked(p);});
  releaseRtShadows();releaseRenamePool();releaseSampleShadows();
  pipelineCache.close();trimMemoryUnlocked(true);reportRtCounters();passTimes.report(stderr);
  if(device && DestroyDevice) DestroyDevice(device,nullptr);
  if(instance && DestroyInstance) DestroyInstance(instance,nullptr);
  if(library) dlclose(library);
  device={};instance={};library=nullptr;
 }
 ~Backend() {close();}
 void requireLive() {if(poisoned || backboardGate.refusesResources()) throw std::runtime_error("device poisoned or backboardd failure latched");}
 Buffer *allocate(size_t length) {
  std::lock_guard<std::mutex> guard(mutex);requireLive();
  return allocateUnlocked(length);
 }
 Buffer *allocateUnlocked(size_t length) {
  if(!length || length>maxBufferLength) throw std::runtime_error("buffer length outside resource limit");
  size_t classSize=(length+15)&~size_t(15);
  if(buffersPooled && classSize<=bufferPoolMaxBuffer){
   for(size_t i=bufferPool.size();i-->0;)if(bufferPool[i].classSize==classSize){
    auto e=bufferPool[i];bufferPool.erase(bufferPool.begin()+i);bufferPoolUsed-=e.classSize;bufferPoolBytes=bufferPoolUsed;++bufferPoolHits;
    auto *b=new Buffer{};b->length=length;b->buffer=e.buffer;b->memory=e.memory;b->contents=e.contents;b->key=e.contents;b->allocated=e.allocated;b->memoryBytes=e.memoryBytes;b->charged=e.charged;b->memoryType=e.memoryType;b->poolable=true;
    memset(b->contents,0,classSize);return b;
   }
   ++bufferPoolMisses;
  }
  auto *b=new Buffer{};b->length=length;b->poolable=buffersPooled && classSize<=bufferPoolMaxBuffer;
  try {
   VkBufferCreateInfo ci{};ci.sType=VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;ci.size=(length+15)&~size_t(15);
   ci.usage=VK_BUFFER_USAGE_TRANSFER_SRC_BIT|VK_BUFFER_USAGE_TRANSFER_DST_BIT|VK_BUFFER_USAGE_VERTEX_BUFFER_BIT|VK_BUFFER_USAGE_INDEX_BUFFER_BIT|VK_BUFFER_USAGE_UNIFORM_BUFFER_BIT|VK_BUFFER_USAGE_STORAGE_BUFFER_BIT;
   check(CreateBuffer(device,&ci,nullptr,&b->buffer));
   VkMemoryRequirements req{};GetBufferMemoryRequirements(device,b->buffer,&req);b->allocated=req.size;
   uint32_t type=0;while(type<memory.memoryTypeCount && (!(req.memoryTypeBits&(1u<<type)) || !(memory.memoryTypes[type].propertyFlags&VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT))) ++type;
   if(type==memory.memoryTypeCount) throw std::runtime_error("no host-visible memory");
   VkMemoryAllocateInfo ai{};ai.sType=VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;ai.allocationSize=req.size;ai.memoryTypeIndex=type;b->memoryType=type;
   reserveMemoryUnlocked(ai.allocationSize);b->charged=ai.allocationSize;
   check(frameKernelCall([&]{return AllocateMemory(device,&ai,nullptr,&b->memory);}));if(systemTranslationPolicy().frameTrace)++frameCounts.allocations;++liveMemoryObjects;liveMemoryBytes+=ai.allocationSize;b->memoryBytes=ai.allocationSize;check(frameKernelCall([&]{return BindBufferMemory(device,b->buffer,b->memory,0);}));
   check(frameKernelCall([&]{return MapMemory(device,b->memory,0,VK_WHOLE_SIZE,0,&b->contents);}));b->key=b->contents;memset(b->contents,0,(length+15)&~size_t(15));return b;
  } catch(...) {destroyUnlocked(b);throw;}
 }
 // NativeMetal's no-copy buffer/linear-image design, using Fuxi HostImport's
 // page contract. Sample-only layouts may use a refreshed optimal shadow.
 Buffer *importBuffer(void *base,size_t bytes) {
  std::lock_guard<std::mutex> guard(mutex);requireLive();
  ++HostImportStats::attempts;
  if(!supportsHostImport())throw std::runtime_error("no-copy buffer host import unsupported");
  // Import the enclosing host pages and bind the buffer at the pointer's offset: CA's CGImage bytes are rarely page aligned.
  auto plan=planHostImport(uintptr_t(base),bytes,hostAlignment,0,maxBufferLength);
  if(!bytes || bytes>maxBufferLength || !plan.ok()){++HostImportStats::refusedPlan;throw std::runtime_error(std::string("no-copy buffer host range (")+hostImportWhyText(plan.why)+") ptr%16K="+std::to_string(uintptr_t(base)%hostAlignment)+" len%16K="+std::to_string(bytes%hostAlignment));}
  if(plan.offset)++HostImportStats::enclosed;else ++HostImportStats::aligned;
  auto *b=new Buffer{};b->length=bytes;b->imported=true;b->memoryOffset=plan.offset;
  try {
   VkExternalMemoryBufferCreateInfo ext{};ext.sType=VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_BUFFER_CREATE_INFO;ext.handleTypes=VK_EXTERNAL_MEMORY_HANDLE_TYPE_HOST_ALLOCATION_BIT_EXT;
   VkBufferCreateInfo ci{};ci.sType=VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;ci.pNext=&ext;ci.size=bytes;
   ci.usage=VK_BUFFER_USAGE_TRANSFER_SRC_BIT|VK_BUFFER_USAGE_TRANSFER_DST_BIT|VK_BUFFER_USAGE_VERTEX_BUFFER_BIT|VK_BUFFER_USAGE_INDEX_BUFFER_BIT|VK_BUFFER_USAGE_UNIFORM_BUFFER_BIT|VK_BUFFER_USAGE_STORAGE_BUFFER_BIT;
   if(!GetPhysicalDeviceExternalBufferProperties)throw std::runtime_error("missing external buffer query");
   VkPhysicalDeviceExternalBufferInfo info{};info.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_EXTERNAL_BUFFER_INFO;info.usage=ci.usage;info.handleType=VK_EXTERNAL_MEMORY_HANDLE_TYPE_HOST_ALLOCATION_BIT_EXT;
   VkExternalBufferProperties properties{};properties.sType=VK_STRUCTURE_TYPE_EXTERNAL_BUFFER_PROPERTIES;GetPhysicalDeviceExternalBufferProperties(physical,&info,&properties);
   auto externalFeatures=properties.externalMemoryProperties.externalMemoryFeatures;
   if(!(externalFeatures&VK_EXTERNAL_MEMORY_FEATURE_IMPORTABLE_BIT) || (externalFeatures&VK_EXTERNAL_MEMORY_FEATURE_DEDICATED_ONLY_BIT) || !(properties.externalMemoryProperties.compatibleHandleTypes&info.handleType))throw std::runtime_error("no-copy buffer external import unavailable");
   check(CreateBuffer(device,&ci,nullptr,&b->buffer));VkMemoryRequirements req{};GetBufferMemoryRequirements(device,b->buffer,&req);
   if(req.size>bytes)throw std::runtime_error("no-copy buffer allocation size");
   if(!planHostImport(uintptr_t(base),bytes,hostAlignment,req.alignment,maxBufferLength).ok() || plan.offset+req.size>plan.bytes)throw std::runtime_error("no-copy buffer offset alignment "+std::to_string(plan.offset)+" required="+std::to_string(req.alignment));
   VkMemoryHostPointerPropertiesEXT hp{};hp.sType=VK_STRUCTURE_TYPE_MEMORY_HOST_POINTER_PROPERTIES_EXT;importCheck(GetMemoryHostPointerPropertiesEXT(device,VK_EXTERNAL_MEMORY_HANDLE_TYPE_HOST_ALLOCATION_BIT_EXT,(void *)plan.base,&hp));
   uint32_t type=0;while(type<memory.memoryTypeCount && (!(req.memoryTypeBits&hp.memoryTypeBits&(1u<<type)) || !(memory.memoryTypes[type].propertyFlags&VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT)))++type;
   if(type==memory.memoryTypeCount)throw std::runtime_error("no-copy buffer memory type");
   VkImportMemoryHostPointerInfoEXT hi{};hi.sType=VK_STRUCTURE_TYPE_IMPORT_MEMORY_HOST_POINTER_INFO_EXT;hi.handleType=VK_EXTERNAL_MEMORY_HANDLE_TYPE_HOST_ALLOCATION_BIT_EXT;hi.pHostPointer=(void *)plan.base;
   VkMemoryAllocateInfo ai{};ai.sType=VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;ai.pNext=&hi;ai.allocationSize=plan.bytes;ai.memoryTypeIndex=type;
   reserveMemoryUnlocked(ai.allocationSize);b->charged=ai.allocationSize;
   importCheck(frameKernelCall([&]{return AllocateMemory(device,&ai,nullptr,&b->memory);}));if(systemTranslationPolicy().frameTrace)++frameCounts.allocations;++liveMemoryObjects;liveMemoryBytes+=ai.allocationSize;b->memoryBytes=ai.allocationSize;b->allocated=bytes;b->memoryType=type;check(frameKernelCall([&]{return BindBufferMemory(device,b->buffer,b->memory,plan.offset);}));
   check(frameKernelCall([&]{return MapMemory(device,b->memory,0,VK_WHOLE_SIZE,0,&b->contents);}));b->contents=(uint8_t *)b->contents+plan.offset;return b;
  }catch(...){++HostImportStats::refusedVulkan;destroyUnlocked(b);throw;}
 }
 static std::string linearAliasMismatch(const VkMemoryRequirements &req,const VkSubresourceLayout &layout,uint32_t memoryType,size_t allocated,size_t offset,size_t row,size_t memoryOffset=0) {
   std::string mismatch;
   auto failed=[&](bool condition,const std::string &text){if(condition){if(!mismatch.empty())mismatch+=", ";mismatch+=text;}};
   failed(!req.alignment || (memoryOffset+offset)%req.alignment,"alignment offset="+std::to_string(memoryOffset+offset)+" required="+std::to_string(req.alignment));
   failed(!(req.memoryTypeBits&(1u<<memoryType)),"memory type buffer="+std::to_string(memoryType)+" allowedBits="+std::to_string(req.memoryTypeBits));
   failed(offset>allocated || req.size>allocated-std::min(offset,allocated),"size required="+std::to_string(req.size)+" available="+std::to_string(allocated-std::min(offset,allocated)));
   failed(layout.offset!=0,"layout.offset Vulkan="+std::to_string(layout.offset)+" Metal=0");
   failed(layout.rowPitch!=row,"rowPitch Vulkan="+std::to_string(layout.rowPitch)+" Metal="+std::to_string(row));
   return mismatch;
 }
 Texture *linearTexture(Buffer *b,uint32_t format,uint32_t usage,uint32_t width,uint32_t height,size_t offset,size_t row,bool shadowed=false) {
  std::unique_lock<std::mutex> guard(mutex);requireLive();
  if(!b || !limits.linearRows(width,height,texelBytes(format),row,offset,b->length) || !usage || usage&~5u || format==81 || (format==1 && (usage&4)))throw std::runtime_error("linear texture descriptor/extent/stride");
  auto *t=new Texture{};t->format=format;t->vkFormat=pixelFormat(format);t->width=width;t->height=height;t->tiling=VK_IMAGE_TILING_LINEAR;
  try {
   VkImageCreateInfo ci{};ci.sType=VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO;ci.imageType=VK_IMAGE_TYPE_2D;ci.format=t->vkFormat;
   ci.flags=VK_IMAGE_CREATE_ALIAS_BIT;ci.extent={width,height,1};ci.mipLevels=ci.arrayLayers=1;ci.samples=VK_SAMPLE_COUNT_1_BIT;ci.tiling=VK_IMAGE_TILING_LINEAR;ci.initialLayout=VK_IMAGE_LAYOUT_PREINITIALIZED;
   ci.usage=VK_IMAGE_USAGE_TRANSFER_SRC_BIT|VK_IMAGE_USAGE_TRANSFER_DST_BIT|(usage&1?VK_IMAGE_USAGE_SAMPLED_BIT:0)|(usage&4?VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT:0)|(orderedColorAccess && (usage&4)?VK_IMAGE_USAGE_INPUT_ATTACHMENT_BIT:0);
   VkExternalMemoryImageCreateInfo external{};external.sType=VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_IMAGE_CREATE_INFO;external.handleTypes=VK_EXTERNAL_MEMORY_HANDLE_TYPE_HOST_ALLOCATION_BIT_EXT;
   VkFormatProperties formatProperties{};GetPhysicalDeviceFormatProperties(physical,ci.format,&formatProperties);
   auto required=VK_FORMAT_FEATURE_TRANSFER_SRC_BIT|VK_FORMAT_FEATURE_TRANSFER_DST_BIT|(usage&1?VK_FORMAT_FEATURE_SAMPLED_IMAGE_BIT:0)|(usage&4?VK_FORMAT_FEATURE_COLOR_ATTACHMENT_BIT:0);
   auto fallback=[&](const std::string &reason)->Texture *{
    if(usage!=1)throw std::runtime_error(reason);
    if(t->image)DestroyImage(device,t->image,nullptr);delete t;t=nullptr;
    auto *shadow=allocateTextureUnlocked(format,1,width,height);
    shadow->shadow=true;b->shadowBacking=true;shadow->backingBuffer=b;++b->references;shadow->backingOffset=offset;shadow->backingRow=row;
    if(reason[0]!='s')fprintf(stderr,"FUXI MTL: linear texture shadow format=%u %ux%u offset=%zu row=%zu reason=%s\n",format,width,height,offset,row,reason.c_str());return shadow;
   };
   if((formatProperties.linearTilingFeatures&required)!=(uint32_t)required)return fallback("linear texture format features unavailable");
   // A shadowed buffer's bytes are rewritten each frame; sampling it in place would pin the buffer to every flight reading it. A copy through a shadow image lets the storage be renamed.
   if(usage==1 && systemTranslationPolicy().sampleShadow && uint64_t(width)*height>=systemTranslationPolicy().sampleMinPx){ // FUXI_MTL_SAMPLE_SHADOW: optional, so it may only use budget below the headroom; refused -> the texture samples the linear memory in place
    // CA creates a new MTLTexture over the same buffer range every frame (J215: 1271 shadows, 0 hits), so the shadow is keyed by (buffer, offset, pitch, extent, format) and shared by reference; hash staleness is per shadow.
    std::array<uint64_t,7> key{uint64_t(uintptr_t(b)),offset,row,width,height,format,0};expireSampleShadows(60);
    auto hit=sampleShadows.find(key);
    if(hit!=sampleShadows.end()){hit->second.used=submits;++hit->second.t->references;++sampleShadowReused;delete t;t=nullptr;return hit->second.t;}
    uint64_t need=uint64_t(width)*height*texelBytes(format);
    while(sampleShadows.size()>=sampleShadowEntryCap || sampleShadowLiveBytes()+need>sampleShadowByteCap){if(!evictIdleSampleShadow(30))break;}
    Texture *copy=nullptr;
    if(sampleShadows.size()<sampleShadowEntryCap && sampleShadowLiveBytes()+need<=sampleShadowByteCap){try{OptionalScope optional(*this);copy=allocateTextureUnlocked(format,1,width,height);}catch(const std::exception &){++sampleShadowRefused;}}
    else ++sampleShadowCapped;
    if(copy){delete t;t=nullptr;copy->shadow=true;b->shadowBacking=true;copy->backingBuffer=b;++b->references;copy->backingOffset=offset;copy->backingRow=row;++sampleShadowCreated;sampleShadowBytes+=copy->allocated;++copy->references;sampleShadows[key]={copy,uint64_t(submits)};return copy;}
   }
   if(usage==1 && systemTranslationPolicy().linearShadow && uint64_t(width)*height>=systemTranslationPolicy().sampleMinPx)return fallback("sampled linear texture copied to optimal tiling");
   if(shadowed && usage==1 && systemTranslationPolicy().shadowRename && !b->imported)return fallback("shadowed buffer samples through a copy (rename)");
   if(b->imported){
    ci.pNext=&external;
    VkPhysicalDeviceExternalImageFormatInfo extInfo{};extInfo.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_EXTERNAL_IMAGE_FORMAT_INFO;extInfo.handleType=VK_EXTERNAL_MEMORY_HANDLE_TYPE_HOST_ALLOCATION_BIT_EXT;
    VkPhysicalDeviceImageFormatInfo2 info{};info.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_IMAGE_FORMAT_INFO_2;info.pNext=&extInfo;info.format=ci.format;info.type=ci.imageType;info.tiling=ci.tiling;info.usage=ci.usage;info.flags=ci.flags;
    VkExternalImageFormatProperties extProps{};extProps.sType=VK_STRUCTURE_TYPE_EXTERNAL_IMAGE_FORMAT_PROPERTIES;VkImageFormatProperties2 props{};props.sType=VK_STRUCTURE_TYPE_IMAGE_FORMAT_PROPERTIES_2;props.pNext=&extProps;
    auto result=GetPhysicalDeviceImageFormatProperties2(physical,&info,&props);
    if(result==VK_ERROR_FORMAT_NOT_SUPPORTED)return fallback("linear texture external format unavailable");check(result);auto flags=extProps.externalMemoryProperties.externalMemoryFeatures;
    if(!(flags&VK_EXTERNAL_MEMORY_FEATURE_IMPORTABLE_BIT) || (flags&VK_EXTERNAL_MEMORY_FEATURE_DEDICATED_ONLY_BIT) || !(extProps.externalMemoryProperties.compatibleHandleTypes&extInfo.handleType))return fallback("linear texture external import unavailable");
   }
   auto result=CreateImage(device,&ci,nullptr,&t->image);if(result==VK_ERROR_FORMAT_NOT_SUPPORTED)return fallback("linear texture image format unavailable");check(result);VkMemoryRequirements req{};GetImageMemoryRequirements(device,t->image,&req);
   VkImageSubresource sub{VK_IMAGE_ASPECT_COLOR_BIT,0,0};VkSubresourceLayout layout{};GetImageSubresourceLayout(device,t->image,&sub,&layout);
   auto mismatch=linearAliasMismatch(req,layout,b->memoryType,b->allocated,offset,row,b->memoryOffset);
   if(!mismatch.empty())return fallback("linear texture Vulkan memory/layout mismatch: "+mismatch);
   check(frameKernelCall([&]{return BindImageMemory(device,t->image,b->memory,b->memoryOffset+offset);}));t->memory=b->memory;t->memoryType=b->memoryType;t->borrowedMemory=true;b->aliased=true;t->allocated=req.size;
   t->mapped=(uint8_t *)b->contents+offset;t->layout=ci.initialLayout;t->defined=true;t->usage=ci.usage;
   VkImageViewCreateInfo vi{};vi.sType=VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO;vi.image=t->image;vi.viewType=VK_IMAGE_VIEW_TYPE_2D;vi.format=t->vkFormat;vi.subresourceRange={VK_IMAGE_ASPECT_COLOR_BIT,0,1,0,1};
   check(CreateImageView(device,&vi,nullptr,&t->view));t->blendable=formatProperties.linearTilingFeatures&VK_FORMAT_FEATURE_COLOR_ATTACHMENT_BLEND_BIT;t->filterable=formatProperties.linearTilingFeatures&VK_FORMAT_FEATURE_SAMPLED_IMAGE_FILTER_LINEAR_BIT;t->backingBuffer=b;++b->references;return t;
  }catch(...){destroyTextureUnlocked(t);throw;}
 }
 void destroyUnlocked(Buffer *b) {
  if(!b || --b->references) return;
  rtForget(b->shadowKey());
  if(b->spare){auto *s=b->spare;b->spare=nullptr;destroyUnlocked(s);}
  if(!poisoned && b->poolable && !b->imported && b->buffer && b->memory && b->contents && b->charged){
   size_t classSize=(b->length+15)&~size_t(15);
   if(bufferPool.size()<bufferPoolEntries && bufferPoolUsed+classSize<=bufferPoolLimit){
    bufferPool.push_back({b->buffer,b->memory,b->contents,classSize,b->allocated,b->memoryBytes,b->charged,b->memoryType,memoryNow()});
    bufferPoolUsed+=classSize;bufferPoolBytes=bufferPoolUsed;delete b;return;
   }
  }
  if(!poisoned) {
   if(b->contents) frameKernelCall([&]{return UnmapMemory(device,b->memory);});
   if(b->buffer) DestroyBuffer(device,b->buffer,nullptr);
   if(b->memory) {frameKernelCall([&]{return FreeMemory(device,b->memory,nullptr);});--liveMemoryObjects;liveMemoryBytes-=b->memoryBytes;}
  }
  limits.release(b->charged);delete b;
 }
 void destroy(Buffer *b) {std::lock_guard<std::mutex> guard(mutex);destroyUnlocked(b);}
 bool destroy(Texture *t) {
  std::lock_guard<std::mutex> guard(mutex);return destroyTextureUnlocked(t);
 }
 bool destroyTextureUnlocked(Texture *t) {
  if(!t || --t->references)return !poisoned;
  if(t->importCached)imports.erase(t->importKey);
  if(!poisoned) {clearFrames(t);clearViews(t);if(t->mapped && !t->borrowedMemory)frameKernelCall([&]{return UnmapMemory(device,t->memory);});for(auto v:t->attachmentViews)if(v)DestroyImageView(device,v,nullptr);if(t->view)DestroyImageView(device,t->view,nullptr);if(t->image)DestroyImage(device,t->image,nullptr);if(t->memory && !t->borrowedMemory){
   if(t->slabbed)slabMemory.release(t->slabBlock,memoryNow());
   else if(t->recyclable && textureMemory.put(t->recycleKey,t->memory,memoryNow()))t->charged=0;
   else {frameKernelCall([&]{return FreeMemory(device,t->memory,nullptr);});--liveMemoryObjects;liveMemoryBytes-=t->memoryBytes;}
  }}limits.release(t->charged);destroyUnlocked(t->backingBuffer);if(!t->backingBuffer)rtForget(t);delete t;return !poisoned;
 }
 bool supportsHostImport() const {return hostImport && hostAlignment && GetMemoryHostPointerPropertiesEXT && GetPhysicalDeviceImageFormatProperties2;}
 void destroySamplerUnlocked(Sampler *s) {if(s && !--s->references){if(!poisoned && s->sampler)DestroySampler(device,s->sampler,nullptr);delete s;}}
 void destroy(Sampler *s) {std::lock_guard<std::mutex> guard(mutex);destroySamplerUnlocked(s);}
 // address: Metal MTLSamplerAddressMode per axis (0 clamp-to-edge, 2 repeat, 3 mirror repeat, 4 clamp-to-zero,
 // 5 clamp-to-border with border 0/1/2 = transparent black/opaque black/opaque white). Mirror clamp-to-edge (1) is refused.
 Sampler *allocateSampler(bool minLinear,bool magLinear,uint32_t mip=0,float minLod=0,float maxLod=FLT_MAX,const uint32_t *address=nullptr,uint32_t border=0,uint32_t anisotropy=1) {
  std::lock_guard<std::mutex> guard(mutex);requireLive();auto *s=new Sampler{};s->lodMin=minLod;s->lodMax=maxLod;s->filters=1|(minLinear?2:0)|(magLinear?4:0)|(mip<<3);
  if(mip>2 || !std::isfinite(minLod) || !std::isfinite(maxLod) || minLod<0 || maxLod<minLod){delete s;throw std::runtime_error("sampler mip/LOD");}
  VkSamplerCreateInfo ci{};ci.sType=VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO;ci.minFilter=minLinear?VK_FILTER_LINEAR:VK_FILTER_NEAREST;
  ci.magFilter=magLinear?VK_FILTER_LINEAR:VK_FILTER_NEAREST;ci.mipmapMode=mip==2?VK_SAMPLER_MIPMAP_MODE_LINEAR:VK_SAMPLER_MIPMAP_MODE_NEAREST;
  ci.addressModeU=ci.addressModeV=ci.addressModeW=VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE;
  if(address) {VkSamplerAddressMode *modes[3]={&ci.addressModeU,&ci.addressModeV,&ci.addressModeW};bool clampBorder=false;
   for(unsigned i=0;i<3;++i)switch(address[i]) {
    case 0:break;case 2:*modes[i]=VK_SAMPLER_ADDRESS_MODE_REPEAT;break;case 3:*modes[i]=VK_SAMPLER_ADDRESS_MODE_MIRRORED_REPEAT;break;
    case 4:*modes[i]=VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_BORDER;break;
    case 5:*modes[i]=VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_BORDER;clampBorder=true;break;
    default:delete s;throw std::runtime_error("sampler address mode");}
   if(clampBorder && border>2){delete s;throw std::runtime_error("sampler border color");}
   // Clamp-to-zero reads transparent black; Metal's border colors map to the float borders.
   ci.borderColor=!clampBorder||border==0?VK_BORDER_COLOR_FLOAT_TRANSPARENT_BLACK:border==1?VK_BORDER_COLOR_FLOAT_OPAQUE_BLACK:VK_BORDER_COLOR_FLOAT_OPAQUE_WHITE;}
  if(!anisotropy || anisotropy>16){delete s;throw std::runtime_error("sampler anisotropy");}
  if(anisotropy>1 && maxAnisotropy>1){ci.anisotropyEnable=VK_TRUE;ci.maxAnisotropy=std::min(float(anisotropy),maxAnisotropy);}
  ci.minLod=mip?std::min(minLod,VK_LOD_CLAMP_NONE):0;ci.maxLod=mip?std::min(maxLod,VK_LOD_CLAMP_NONE):0;
  try {check(CreateSampler(device,&ci,nullptr,&s->sampler));return s;}catch(...) {delete s;throw;}
 }
 // The caller retains/locks the IOSurface; ABI 2 pins its task pages and handles
 // non-coherent cache handoff inside kernel submit/retirement.
 // No staging copy: only an external host allocation with exactly matching linear layout.
 // A nonzero identity (the IOSurface ID) shares one live import per surface mapping, size and plane layout: Core Animation re-wraps the
 // same surfaces every frame, and each wrap otherwise allocates and maps a fresh host-pointer memory object. The cache holds no reference;
 // an entry lives exactly as long as some wrapper or pending command holds the texture.
 Texture *importTexture(void *base,size_t bytes,size_t row,uint32_t width=256,uint32_t height=256,size_t offset=0,uint64_t identity=0) {
  std::unique_lock<std::mutex> guard(mutex);requireLive();
  std::array<uint64_t,7> key{identity,uint64_t(uintptr_t(base)),bytes,row,offset,width,height};
  if(identity){auto hit=imports.find(key);if(hit!=imports.end()){++hit->second->references;++surfaceImportHits;return hit->second;}++surfaceImportMisses;}
  if(!supportsHostImport())throw std::runtime_error("IOSurface same-memory import requires VK_EXT_external_memory_host / NativeAdreno HostImport");
  constexpr VkFormatFeatureFlags required=VK_FORMAT_FEATURE_COLOR_ATTACHMENT_BIT|VK_FORMAT_FEATURE_SAMPLED_IMAGE_BIT|
   VK_FORMAT_FEATURE_SAMPLED_IMAGE_FILTER_LINEAR_BIT|VK_FORMAT_FEATURE_TRANSFER_SRC_BIT|VK_FORMAT_FEATURE_TRANSFER_DST_BIT;
  if((linearFeatures&required)!=required)throw std::runtime_error("IOSurface linear BGRA8 format features unavailable");
  if(!base || !bytes || bytes>maxBufferLength || (uintptr_t(base)%hostAlignment) || bytes%hostAlignment || !limits.rows(width,height,4,row,offset,bytes))
   throw std::runtime_error("IOSurface host alignment/length/stride");
  VkPhysicalDeviceExternalImageFormatInfo external{};external.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_EXTERNAL_IMAGE_FORMAT_INFO;
  external.handleType=VK_EXTERNAL_MEMORY_HANDLE_TYPE_HOST_ALLOCATION_BIT_EXT;
  VkPhysicalDeviceImageFormatInfo2 info{};info.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_IMAGE_FORMAT_INFO_2;info.pNext=&external;
  info.format=VK_FORMAT_B8G8R8A8_UNORM;info.type=VK_IMAGE_TYPE_2D;info.tiling=VK_IMAGE_TILING_LINEAR;
  info.usage=(orderedColorAccess?VK_IMAGE_USAGE_INPUT_ATTACHMENT_BIT:0)|VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT|VK_IMAGE_USAGE_SAMPLED_BIT|VK_IMAGE_USAGE_TRANSFER_SRC_BIT|VK_IMAGE_USAGE_TRANSFER_DST_BIT;
  VkExternalImageFormatProperties ep{};ep.sType=VK_STRUCTURE_TYPE_EXTERNAL_IMAGE_FORMAT_PROPERTIES;
  VkImageFormatProperties2 props{};props.sType=VK_STRUCTURE_TYPE_IMAGE_FORMAT_PROPERTIES_2;props.pNext=&ep;
  check(GetPhysicalDeviceImageFormatProperties2(physical,&info,&props));
  auto features=ep.externalMemoryProperties.externalMemoryFeatures;
  if(!(features&VK_EXTERNAL_MEMORY_FEATURE_IMPORTABLE_BIT) || (features&VK_EXTERNAL_MEMORY_FEATURE_DEDICATED_ONLY_BIT) ||
     !(ep.externalMemoryProperties.compatibleHandleTypes&external.handleType) || props.imageFormatProperties.maxExtent.width<width || props.imageFormatProperties.maxExtent.height<height)
   throw std::runtime_error("IOSurface linear BGRA8 sampled/render/transfer import unsupported");
  auto *t=new Texture{};t->blendable=linearFeatures&VK_FORMAT_FEATURE_COLOR_ATTACHMENT_BLEND_BIT;t->width=width;t->height=height;t->usage=info.usage;t->tiling=VK_IMAGE_TILING_LINEAR;t->imported=true;t->defined=true;t->layout=VK_IMAGE_LAYOUT_PREINITIALIZED;
  try {
   VkExternalMemoryImageCreateInfo ei{};ei.sType=VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_IMAGE_CREATE_INFO;ei.handleTypes=external.handleType;
   VkImageCreateInfo ii{};ii.sType=VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO;ii.pNext=&ei;ii.imageType=info.type;ii.format=info.format;
   ii.extent={width,height,1};ii.mipLevels=1;ii.arrayLayers=1;ii.samples=VK_SAMPLE_COUNT_1_BIT;ii.tiling=info.tiling;ii.usage=info.usage;ii.initialLayout=t->layout;
   check(CreateImage(device,&ii,nullptr,&t->image));VkMemoryRequirements req{};GetImageMemoryRequirements(device,t->image,&req);
   VkImageSubresource sub{VK_IMAGE_ASPECT_COLOR_BIT,0,0};VkSubresourceLayout layout{};GetImageSubresourceLayout(device,t->image,&sub,&layout);
   if(!req.alignment || offset%req.alignment || layout.offset || layout.rowPitch!=row || req.size>bytes-offset || layout.size>bytes-offset)throw std::runtime_error("IOSurface/Vulkan linear image layout mismatch");
   VkMemoryHostPointerPropertiesEXT hp{};hp.sType=VK_STRUCTURE_TYPE_MEMORY_HOST_POINTER_PROPERTIES_EXT;
   check(GetMemoryHostPointerPropertiesEXT(device,external.handleType,base,&hp));
   uint32_t type=0;while(type<memory.memoryTypeCount && (!(req.memoryTypeBits&hp.memoryTypeBits&(1u<<type)) || !(memory.memoryTypes[type].propertyFlags&VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT)))++type;
   if(type==memory.memoryTypeCount)throw std::runtime_error("IOSurface host memory type mismatch");
   VkImportMemoryHostPointerInfoEXT hi{};hi.sType=VK_STRUCTURE_TYPE_IMPORT_MEMORY_HOST_POINTER_INFO_EXT;hi.handleType=external.handleType;hi.pHostPointer=base;
   VkMemoryAllocateInfo ai{};ai.sType=VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;ai.pNext=&hi;ai.allocationSize=bytes;ai.memoryTypeIndex=type;t->memoryType=type;
   reserveMemoryUnlocked(ai.allocationSize);t->charged=ai.allocationSize;
   // Import/charge the whole surface, binding the image at its plane's offset.
   check(frameKernelCall([&]{return AllocateMemory(device,&ai,nullptr,&t->memory);}));if(systemTranslationPolicy().frameTrace)++frameCounts.allocations;++liveMemoryObjects;liveMemoryBytes+=ai.allocationSize;t->memoryBytes=ai.allocationSize;t->allocated=bytes;check(frameKernelCall([&]{return BindImageMemory(device,t->image,t->memory,offset);}));
   check(frameKernelCall([&]{return MapMemory(device,t->memory,0,VK_WHOLE_SIZE,0,&t->mapped);}));
   VkImageViewCreateInfo vi{};vi.sType=VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO;vi.image=t->image;vi.viewType=VK_IMAGE_VIEW_TYPE_2D;vi.format=info.format;
   vi.subresourceRange={VK_IMAGE_ASPECT_COLOR_BIT,0,1,0,1};check(CreateImageView(device,&vi,nullptr,&t->view));if(identity){t->importKey=key;t->importCached=true;imports.emplace(key,t);}return t;
  }catch(...) {guard.unlock();destroy(t);throw;}
 }
 static size_t texelBytes(uint32_t f){return metalTexelBytes(f);}
 // MTLTextureType 0 is 1D, 2 is 2D and 7 is 3D; the sampled view must match the SPIR-V Dim the translator declares.
 static VkImageType imageTypeFor(uint32_t type){return type==0?VK_IMAGE_TYPE_1D:type==7?VK_IMAGE_TYPE_3D:VK_IMAGE_TYPE_2D;}
 static VkImageViewType viewTypeFor(uint32_t type){return type==0?VK_IMAGE_VIEW_TYPE_1D:type==7?VK_IMAGE_VIEW_TYPE_3D:VK_IMAGE_VIEW_TYPE_2D;}
 static VkFormat pixelFormat(uint32_t f) {
  switch(f){case 23:return VK_FORMAT_R16_UINT;case 25:return VK_FORMAT_R16_SFLOAT;case 43:return VK_FORMAT_A1R5G5B5_UNORM_PACK16;case 55:return VK_FORMAT_R32_SFLOAT;case 105:return VK_FORMAT_R32G32_SFLOAT;case 94:return VK_FORMAT_A2R10G10B10_UNORM_PACK32;case 80:return VK_FORMAT_B8G8R8A8_UNORM;case 81:return VK_FORMAT_B8G8R8A8_SRGB;case 70:return VK_FORMAT_R8G8B8A8_UNORM;case 115:return VK_FORMAT_R16G16B16A16_SFLOAT;case 10:case 1:return VK_FORMAT_R8_UNORM;case 30:return VK_FORMAT_R8G8_UNORM;default:throw std::runtime_error("unsupported pixel format");}
 }
 Texture *allocateTexture(uint32_t format=80,uint32_t usage=5,uint32_t width=256,uint32_t height=256,uint32_t levels=1,uint32_t type=2,uint32_t depth=1) {
  std::unique_lock<std::mutex> guard(mutex);requireLive();
  return allocateTextureUnlocked(format,usage,width,height,levels,type,depth);
 }
 // Binds `t` to a slab range when the request is small; false means use a dedicated (pooled) block.
 bool trySlabUnlocked(Texture *t,const VkMemoryRequirements &req,uint32_t memoryType){
  SlabMemory<VkDeviceMemory>::Block blk{};auto align=req.alignment?req.alignment:1;
  auto r=slabMemory.alloc(req.size,align,req.memoryTypeBits,blk);
  if(r==SlabMemory<VkDeviceMemory>::ineligible)return false;
  if(r==SlabMemory<VkDeviceMemory>::grow){
   constexpr uint64_t slab=SlabMemory<VkDeviceMemory>::slabSize;
   if(!slabMemory.canGrow())return false;
   if(slab>ResourceLimits::budget-limits.used){textureMemory.reclaim(slab-(ResourceLimits::budget-limits.used),[&](VkDeviceMemory h,size_t n){freePooledUnlocked(h,n);});}
   if(slab>ResourceLimits::budget-limits.used)slabMemory.reclaim(slab-(ResourceLimits::budget-limits.used),[&](VkDeviceMemory h,size_t n){freePooledUnlocked(h,n);});
   if(slab>ResourceLimits::budget-limits.used)return false;
   VkMemoryAllocateInfo ai{};ai.sType=VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;ai.allocationSize=slab;ai.memoryTypeIndex=memoryType;VkDeviceMemory m{};
   auto result=frameKernelCall([&]{return AllocateMemory(device,&ai,nullptr,&m);});
   if(result!=VK_SUCCESS)return false;
   limits.reserve(slab);if(systemTranslationPolicy().frameTrace)++frameCounts.allocations;++liveMemoryObjects;liveMemoryBytes+=slab;
   if(!slabMemory.add(m,memoryType,memoryNow())){frameKernelCall([&]{FreeMemory(device,m,nullptr);});--liveMemoryObjects;liveMemoryBytes-=slab;limits.release(slab);return false;}
   if(slabMemory.alloc(req.size,align,req.memoryTypeBits,blk)!=SlabMemory<VkDeviceMemory>::ok)return false;
  }
  t->memory=blk.memory;t->memOffset=blk.offset;t->memExtent=blk.size;t->slabbed=true;t->slabBlock=blk;t->memoryType=memoryType;t->charged=0;return true;
 }
 Texture *allocateTextureUnlocked(uint32_t format,uint32_t usage,uint32_t width,uint32_t height,uint32_t levels=1,uint32_t type=2,uint32_t depth=1) {
  if(!textureShapeAccepted(type,height,usage,depth,levels))throw std::runtime_error("texture dimension/usage");
  if(!levels || levels>mipCount(width,height))throw std::runtime_error("texture mip count");
  if(!limits.extent(width,height,texelBytes(format)) || !limits.volume(width,height,depth,texelBytes(format)))throw std::runtime_error("texture extent/bytes outside device limit");
  if((usage&~21u) || format==81 || (format==1 && ((usage&4) || !usage)))throw std::runtime_error("unsupported texture usage/sRGB/A8 attachment");
  usage=(usage&5)?usage&5:(format==1?1:5);
  if(format==1 && (usage&4))throw std::runtime_error("A8 attachment");
  VkFormatProperties features{};GetPhysicalDeviceFormatProperties(physical,pixelFormat(format),&features);
  VkFormatFeatureFlags needed=VK_FORMAT_FEATURE_TRANSFER_SRC_BIT|VK_FORMAT_FEATURE_TRANSFER_DST_BIT;
  if(usage&1)needed|=VK_FORMAT_FEATURE_SAMPLED_IMAGE_BIT;
  if(usage&4)needed|=VK_FORMAT_FEATURE_COLOR_ATTACHMENT_BIT;
  if((features.optimalTilingFeatures&needed)!=needed)throw std::runtime_error("texture format features unavailable");
  auto *t=new Texture{};t->width=width;t->height=height;t->depth=depth;t->levels=levels;t->textureType=type;t->format=format;t->vkFormat=pixelFormat(format);
  t->blendable=features.optimalTilingFeatures&VK_FORMAT_FEATURE_COLOR_ATTACHMENT_BLEND_BIT;t->filterable=features.optimalTilingFeatures&VK_FORMAT_FEATURE_SAMPLED_IMAGE_FILTER_LINEAR_BIT;
  t->canGenerate=(features.optimalTilingFeatures&(VK_FORMAT_FEATURE_BLIT_SRC_BIT|VK_FORMAT_FEATURE_BLIT_DST_BIT|VK_FORMAT_FEATURE_SAMPLED_IMAGE_FILTER_LINEAR_BIT))==(VK_FORMAT_FEATURE_BLIT_SRC_BIT|VK_FORMAT_FEATURE_BLIT_DST_BIT|VK_FORMAT_FEATURE_SAMPLED_IMAGE_FILTER_LINEAR_BIT);
  try {
   VkImageCreateInfo ii{};ii.sType=VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO;ii.imageType=imageTypeFor(type);ii.format=t->vkFormat;
   ii.extent={width,height,depth};ii.mipLevels=levels;ii.arrayLayers=1;ii.samples=VK_SAMPLE_COUNT_1_BIT;ii.tiling=VK_IMAGE_TILING_OPTIMAL;
   ii.usage=(orderedColorAccess && (usage&4)?VK_IMAGE_USAGE_INPUT_ATTACHMENT_BIT:0)|VK_IMAGE_USAGE_TRANSFER_SRC_BIT|VK_IMAGE_USAGE_TRANSFER_DST_BIT|(usage&4?VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT:0)|(usage&1?VK_IMAGE_USAGE_SAMPLED_BIT:0);
   if(GetPhysicalDeviceImageFormatProperties2){
    VkPhysicalDeviceImageFormatInfo2 query{};query.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_IMAGE_FORMAT_INFO_2;query.format=ii.format;query.type=ii.imageType;query.tiling=ii.tiling;query.usage=ii.usage;
    VkImageFormatProperties2 supported{};supported.sType=VK_STRUCTURE_TYPE_IMAGE_FORMAT_PROPERTIES_2;
    check(GetPhysicalDeviceImageFormatProperties2(physical,&query,&supported));const auto &limit=supported.imageFormatProperties;
    if(width>limit.maxExtent.width || height>limit.maxExtent.height || depth>limit.maxExtent.depth || levels>limit.maxMipLevels || !(limit.sampleCounts&VK_SAMPLE_COUNT_1_BIT))throw std::runtime_error("texture image format limits unavailable");
   }
   t->usage=ii.usage;check(CreateImage(device,&ii,nullptr,&t->image));VkMemoryRequirements req{};GetImageMemoryRequirements(device,t->image,&req);t->allocated=req.size;
   uint32_t memoryType=0;while(memoryType<memory.memoryTypeCount && (!(req.memoryTypeBits&(1u<<memoryType)) || !(memory.memoryTypes[memoryType].propertyFlags&VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT)))++memoryType;
   if(memoryType==memory.memoryTypeCount)throw std::runtime_error("no device-local texture memory");
   VkMemoryAllocateInfo ai{};ai.sType=VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;ai.allocationSize=memorySizeClass(req.size);if(ai.allocationSize>ResourceLimits::single)ai.allocationSize=req.size;ai.memoryTypeIndex=memoryType;t->memoryType=memoryType;
   trimMemoryUnlocked();MemoryKey got{};
   // Small images are sub-allocated from shared device-local slabs: a miss costs a free-list search, not a kernel allocation.
   // Frees come only from destroyTextureUnlocked, i.e. after the submit that pinned the texture has retired.
   if(slabsEnabled && trySlabUnlocked(t,req,memoryType)){
    t->memoryBytes=0;check(frameKernelCall([&]{return BindImageMemory(device,t->image,t->memory,t->memOffset);}));
    VkImageViewCreateInfo vi{};vi.sType=VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO;vi.image=t->image;vi.viewType=viewTypeFor(t->textureType);vi.format=ii.format;
    vi.subresourceRange={VK_IMAGE_ASPECT_COLOR_BIT,0,levels,0,1};check(CreateImageView(device,&vi,nullptr,&t->view));t->recyclable=true;return t;
   }
   // Pooled blocks are plain device-local allocations (no dedicated/imported); offset 0 satisfies any alignment.
   if(textureMemory.take(req.size,req.memoryTypeBits,t->memory,got)){t->recycleKey=got;t->memoryType=got.type;ai.allocationSize=got.bytes;t->charged=got.bytes;}
   else {
    t->recycleKey={ai.allocationSize,memoryType};texturePoolWasted+=ai.allocationSize-req.size;
    reserveMemoryUnlocked(ai.allocationSize);t->charged=ai.allocationSize;
    auto result=frameKernelCall([&]{return AllocateMemory(device,&ai,nullptr,&t->memory);});
    if(result==VK_ERROR_OUT_OF_DEVICE_MEMORY || result==VK_ERROR_OUT_OF_HOST_MEMORY){trimMemoryUnlocked(true);result=frameKernelCall([&]{return AllocateMemory(device,&ai,nullptr,&t->memory);});}
    check(result);if(systemTranslationPolicy().frameTrace)++frameCounts.allocations;++liveMemoryObjects;liveMemoryBytes+=ai.allocationSize;
   }
   t->memoryBytes=ai.allocationSize;check(frameKernelCall([&]{return BindImageMemory(device,t->image,t->memory,0);}));
   VkImageViewCreateInfo vi{};vi.sType=VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO;vi.image=t->image;vi.viewType=viewTypeFor(t->textureType);vi.format=ii.format;
   vi.subresourceRange={VK_IMAGE_ASPECT_COLOR_BIT,0,levels,0,1};check(CreateImageView(device,&vi,nullptr,&t->view));t->recyclable=true;return t;
  }catch(...) {destroyTextureUnlocked(t);throw;}
 }
 VkImageView attachmentView(Texture *t,uint32_t level) {
  if(t->levels==1)return t->view;
  if(t->attachmentViews.empty())t->attachmentViews.resize(t->levels);
  auto &view=t->attachmentViews[level];if(view)return view;
  VkImageViewCreateInfo vi{};vi.sType=VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO;vi.image=t->image;
  vi.viewType=VK_IMAGE_VIEW_TYPE_2D;vi.format=t->vkFormat;vi.subresourceRange={VK_IMAGE_ASPECT_COLOR_BIT,level,1,0,1};
  check(CreateImageView(device,&vi,nullptr,&view));return view;
 }
 void destroyPipelineUnlocked(Pipeline *p) {
  if(!p || --p->references)return;drawPlans.invalidate(p);for(auto &v:p->variants)destroyPipelineUnlocked(v.second);
  if(!poisoned) {
   if(p->pipeline)DestroyPipeline(device,p->pipeline,nullptr);
   if(p->stripPipeline)DestroyPipeline(device,p->stripPipeline,nullptr);
   for(auto line:p->linePipelines)if(line)DestroyPipeline(device,line,nullptr);
   if(p->layout)DestroyPipelineLayout(device,p->layout,nullptr);
   if(p->setLayout)DestroyDescriptorSetLayout(device,p->setLayout,nullptr);
   for(auto sampler:p->bakedSamplers)if(sampler)DestroySampler(device,sampler,nullptr);
   for(size_t i=0;i<p->constantBlobs.size();++i){auto *b=p->constantBlobs[i];if(i<p->blobKeys.size()){auto it=blobShare.find(p->blobKeys[i]);if(it!=blobShare.end() && it->second.first==b){if(--it->second.second==0){blobShare.erase(it);destroyUnlocked(b);}continue;}}destroyUnlocked(b);}
   for(auto entry:p->renders)if(auto render=entry.second){clearFrames(nullptr,render);DestroyRenderPass(device,render,nullptr);}
  }
  if(p->translation)Translator::get().free(p->translation);delete p;
 }
 void destroy(Pipeline *p){std::lock_guard<std::mutex> guard(mutex);destroyPipelineUnlocked(p);}

 bool supportsOrderedColorAccess() const {return orderedColorAccess;}
 bool requiresSampling(const Pipeline *p) const {return std::any_of(p->descriptors.begin(),p->descriptors.end(),[](const Descriptor &d){return d.type==VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER || d.type==VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE;});}
 // Called with the backend lock held on a compile miss/product-hit: remember how to rebuild this pipeline in a later run.
 void recordRecipe(const uint8_t *identity,void *v,const char *vn,void *f,const char *fn,const CompileOptions &options) {
  try {
   if(cacheMode!=CacheMode::persistent || !artifactCache.available())return;
   std::string key=recipeKey(identity);if(!recipeKeys.insert(key).second)return;
   Recipe r;{std::lock_guard<std::mutex> lock(prewarmMutex);bool gv=false,gf=false;for(auto &l:prewarmLibs){if(l.lib==v){r.vertex=l.hash;gv=true;}if(l.lib==f){r.fragment=l.hash;gf=true;}}if(!gv || !gf)return;}
   r.vertexName=vn;r.fragmentName=fn;packOptions(options,r.options);auto blob=encodeRecipe(r);
   ArtifactCache::Access access(artifactCache);ArtifactCache::write(&access,(const uint8_t *)key.data(),blob.data(),blob.size());
  }catch(...){}
 }
 // Frontend hooks: a library with these contents now exists (or is about to be closed).
 void registerLibrary(void *lib,LibraryHash hash) {
  try {
   std::lock_guard<std::mutex> lock(prewarmMutex);prewarmLibs.push_back({lib,hash,0});
   bool queued=false;
   for(size_t i=0;i<prewarmRecipes.size();++i){auto &r=prewarmRecipes[i];if(r.queued)continue;PrewarmLib *vl=nullptr,*fl=nullptr;
    for(auto &l:prewarmLibs){if(l.hash==r.vertex)vl=&l;if(l.hash==r.fragment)fl=&l;}
    if(vl && fl){r.queued=true;prewarmQueue.push_back(i);queued=true;}}
   if(queued && !prewarmThread.joinable() && !prewarmStopping)prewarmThread=std::thread([this]{prewarmLoop();});
   if(queued)prewarmWake.notify_all();
  }catch(...){}
 }
 void unregisterLibrary(void *lib) {
  std::unique_lock<std::mutex> lock(prewarmMutex);
  prewarmWake.wait(lock,[&]{for(auto &l:prewarmLibs)if(l.lib==lib && l.pins)return false;return true;});
  prewarmLibs.erase(std::remove_if(prewarmLibs.begin(),prewarmLibs.end(),[&](const PrewarmLib &l){return l.lib==lib;}),prewarmLibs.end());
 }
 void prewarmLoop() {
  prewarmWorker=true;
#ifdef __APPLE__
  {const char *q=getenv("FUXI_MTL_PREWARM_QOS"); // utility (default) runs on efficiency cores and can lose every race with the first draws; "initiated" or "default" trade that for CPU contention
   pthread_set_qos_class_self_np(q && !strcmp(q,"initiated")?QOS_CLASS_USER_INITIATED:q && !strcmp(q,"default")?QOS_CLASS_DEFAULT:QOS_CLASS_UTILITY,0);}
#endif
  std::unique_lock<std::mutex> lock(prewarmMutex);uint64_t batchStart=0;auto began=std::chrono::steady_clock::now();
  while(true){
   prewarmWake.wait(lock,[&]{return prewarmStopping || !prewarmQueue.empty();});
   if(prewarmStopping)return;
   if(!prewarmRunning){prewarmRunning=true;began=std::chrono::steady_clock::now();batchStart=prewarmDone+prewarmFailed;}
   size_t index=prewarmQueue.front();prewarmQueue.pop_front();
   Recipe r=prewarmRecipes[index];PrewarmLib *vl=nullptr,*fl=nullptr;
   for(auto &l:prewarmLibs){if(l.hash==r.vertex)vl=&l;if(l.hash==r.fragment)fl=&l;}
   if(!vl || !fl){++prewarmFailed;continue;}
   void *vlib=vl->lib,*flib=fl->lib;++vl->pins;if(fl!=vl)++fl->pins;
   lock.unlock();
   bool hit=false,ok=false;
   try {
    auto options=std::make_unique<CompileOptions>();
    if(unpackOptions(r.options.data(),r.options.size(),*options)){
     lastCompileWasHit=0;auto *p=compilePipeline(vlib,r.vertexName.c_str(),flib,r.fragmentName.c_str(),*options);hit=lastCompileWasHit==1;destroy(p);ok=true;}
   }catch(...){}
   lock.lock();
   for(auto &l:prewarmLibs)if(l.lib==vlib || l.lib==flib)--l.pins;
   if(ok){++prewarmDone;if(hit)++prewarmHitCount;}else ++prewarmFailed;
   prewarmWake.notify_all();
   if(prewarmQueue.empty()){
    prewarmMs=std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-began).count();
    fprintf(stderr,"FUXI MTL: prewarm n=%llu ms=%.1f hits=%llu failed=%llu\n",(unsigned long long)(prewarmDone+prewarmFailed-batchStart),prewarmMs,(unsigned long long)prewarmHitCount,(unsigned long long)prewarmFailed);
    prewarmRunning=false;
   }
  }
 }
 Pipeline *compilePipeline(void *v,const char *vn,void *f,const char *fn,const CompileOptions &options=CompileOptions{}) {
  auto requested=std::chrono::steady_clock::now();
  std::unique_lock<std::mutex> guard(mutex);auto acquired=std::chrono::steady_clock::now();requireLive();Translation translated{};auto &tr=Translator::get();char error[2048]{};
  // This scope is destroyed while the backend lock is held, including failures.
  CacheActivity activity{cacheMode==CacheMode::persistent?&cacheActivity:nullptr};
  Translator::OptionsScope translationScope(tr);
  ArtifactCache::Access artifactAccess(artifactCache);
  Translator::Scope cacheScope(artifactCallbacks,artifactCache.available()?&artifactAccess:nullptr,ArtifactCache::read,ArtifactCache::write,ArtifactCache::outcome,artifactCallbacksVerified,ArtifactCache::readVerified);
  auto start=std::chrono::steady_clock::now();
  struct Timing {
   bool enabled;decltype(start) requested,acquired,last;const char *result="fail";
   double identity=0,compile=0,hash=0,create=0,relock=0;struct rusage cpu{};
   static double cpuMs(const struct rusage &r){return (r.ru_utime.tv_sec+r.ru_stime.tv_sec)*1000.0+(r.ru_utime.tv_usec+r.ru_stime.tv_usec)/1000.0;}
   double lap(){auto now=std::chrono::steady_clock::now();double ms=std::chrono::duration<double,std::milli>(now-last).count();last=now;return ms;}
   ~Timing(){if(enabled){struct rusage end{};getrusage(RUSAGE_SELF,&end);
#ifdef __APPLE__
    auto rss=end.ru_maxrss/1024;
#else
    auto rss=end.ru_maxrss;
#endif
    fprintf(stderr,"FUXI MTL: translation timing result=%s wait_ms=%.3f identity_ms=%.3f compile_ms=%.3f hash_ms=%.3f create_ms=%.3f total_ms=%.3f process_cpu_ms=%.3f peak_rss_kib=%ld relock_ms=%.3f\n",result,std::chrono::duration<double,std::milli>(acquired-requested).count(),identity,compile,hash,create,std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-requested).count(),cpuMs(end)-cpuMs(cpu),long(rss),relock);}}
  } timing{systemTranslationPolicy().trace,requested,acquired,start};
  if(timing.enabled)getrusage(RUSAGE_SELF,&timing.cpu);
  auto enabledOptions=options;enabledOptions.orderedColorAccess=orderedColorAccess;enabledOptions.independentBlend=independentBlend;enabledOptions.deviceFeatures=deviceFeatures;
  uint8_t identity[32]{};
  if(!tr.identity(v,vn,f,fn,&enabledOptions,identity,error,sizeof(error)))throw std::runtime_error(error);
  timing.identity=timing.lap();
  if(timing.enabled)fprintf(stderr,"FUXI MTL: translation request id=%02x%02x%02x%02x vertex=%.96s fragment=%.96s objects=%zu\n",identity[0],identity[1],identity[2],identity[3],vn,fn,objects.count());
  std::string inputKey((const char *)identity,32);
  // Another thread (prewarm or the draw path) may already be compiling this exact pipeline: wait for it, then take its object.
  while(compiling.count(inputKey))compileDone.wait(guard);
  requireLive();
  if(auto *cached=objects.acquire(inputKey)){
   timing.result="object-hit";lastCompileWasHit=1;++pipelineHits;++objectHits;fprintf(stderr,"FUXI MTL: pipeline cache HIT %.3f ms\n",std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-start).count());return cached;
  }
  struct Inflight {Backend &b;std::string key;Inflight(Backend &x,std::string k):b(x),key(std::move(k)){b.compiling.insert(key);}~Inflight(){b.compiling.erase(key);b.compileDone.notify_all();}} inflight(*this,inputKey);
  CompileBoostScope compileBoost("pipeline");
  int ok;
  { // Immutable library/options and instance queries need no device/queue lock.
   // Caller owns Backend/library lifetime for the duration of this method.
   guard.unlock();
   struct Relock {std::unique_lock<std::mutex> &lock;~Relock(){lock.lock();}} relock{guard};
   ok=tr.compile(v,vn,f,fn,(void *)gipa,(uint64_t)instance,(uint64_t)physical,&enabledOptions,&translated,error,sizeof(error));
   timing.compile=timing.lap();
  }
  timing.relock=timing.lap();
  // A submission may poison the backend while translation runs. Never leak the
  // returned product or pass it to Vulkan after a concurrent fault.
  try {requireLive();}catch(...){if(translated.handle)tr.free(translated.handle);throw;}
  double ms=std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-start).count();
  if(ok)fprintf(stderr,"FUXI MTL: translation PASS %.3f ms (bounded translator cache)\n",ms);
  else fprintf(stderr,"FUXI MTL: translation FAIL %.160s\n",error);
  if(!ok)throw std::runtime_error(error[0]?error:"translation failed");
  uint8_t hash[32]{};
  if(!tr.product_hash(translated.handle,hash)){tr.free(translated.handle);throw std::runtime_error("pipeline product hash");}
  timing.hash=timing.lap();
  std::string productKey=inputKey+std::string((const char *)hash,32);
  if(auto *cached=objects.acquire(productKey,true)){
   timing.result="product-hit";lastCompileWasHit=1;tr.free(translated.handle);++pipelineHits;++objectHits;recordRecipe(identity,v,vn,f,fn,options);
   fprintf(stderr,"FUXI MTL: pipeline cache HIT %.3f ms\n",std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-start).count());return cached;
  }
  ++pipelineMisses;++objectMisses;lastCompileWasHit=0;
  {static std::atomic<unsigned> reasons{0};if(auto rn=reasons.fetch_add(1);rn<10||rn%25==0)fprintf(stderr,"FUXI MTL: pipeline miss reason=%s input=%02x%02x%02x%02x product=%02x%02x%02x%02x objects=%zu evictions=%zu prewarm=%d\n",objects.wasEvicted(inputKey)?"lru-evicted-rebuild":"first-sight-or-key-differs",
   (unsigned)(uint8_t)inputKey[0],(unsigned)(uint8_t)inputKey[1],(unsigned)(uint8_t)inputKey[2],(unsigned)(uint8_t)inputKey[3],(unsigned)(uint8_t)productKey[32],(unsigned)(uint8_t)productKey[33],(unsigned)(uint8_t)productKey[34],(unsigned)(uint8_t)productKey[35],objects.count(),objects.evictions,int(prewarmWorker));}
  struct Unlock {Unlock(){unlockForCompile=true;}~Unlock(){unlockForCompile=false;}} unlockScope;
  auto *p=createPipeline(translated,enabledOptions);timing.create=timing.lap();timing.result="miss";
  compileBoost.finish();
  try {objects.insert(inputKey,productKey,p,p->retainedBytes,[&](Pipeline *old){drawPlans.invalidate(old);destroyPipelineUnlocked(old);});}
  catch(...){destroyPipelineUnlocked(p);throw;}
  {double missMs=std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-start).count();
   fprintf(stderr,"FUXI MTL: pipeline cache MISS %.3f ms%s identity=%.1f translate=%.1f create=%.1f translation_hits=%llu misses=%llu\n",missMs,prewarmWorker?" (prewarm)":"",timing.identity,timing.compile,timing.create,(unsigned long long)artifactCache.hitCount(),(unsigned long long)artifactCache.missCount());
   if(!prewarmWorker){syncCompiles.fetch_add(1);syncCompileNs.fetch_add(uint64_t(missMs*1e6));}
   recordRecipe(identity,v,vn,f,fn,options);}
  return p;
 }
 VkRenderPass renderPass(Pipeline *p,const Operation *op) {
  unsigned count=p->options.colorCount;uint32_t key=0;
  for(unsigned i=0;i<count;++i){auto c=op?op->color(i):ColorTarget{};key|=(c.load*2+c.store)<<(i*3);}
  for(auto e:p->renders)if(e.first==key)return e.second;
  if(p->renders.size()>=256)throw std::runtime_error("render pass action variant bound 256");
  VkAttachmentDescription attachments[8]{};VkAttachmentReference refs[8]{};unsigned used=0;
  for(unsigned i=0;i<count;++i){if(!p->options.color(i).format){refs[i]={VK_ATTACHMENT_UNUSED,VK_IMAGE_LAYOUT_GENERAL};continue;}auto &a=attachments[used];auto c=op?op->color(i):ColorTarget{};
   a.format=pixelFormat(p->options.color(i).format);a.samples=VK_SAMPLE_COUNT_1_BIT;
   a.loadOp=c.load==2?VK_ATTACHMENT_LOAD_OP_CLEAR:c.load==1?VK_ATTACHMENT_LOAD_OP_LOAD:VK_ATTACHMENT_LOAD_OP_DONT_CARE;
   a.storeOp=c.store?VK_ATTACHMENT_STORE_OP_STORE:VK_ATTACHMENT_STORE_OP_DONT_CARE;
   a.stencilLoadOp=VK_ATTACHMENT_LOAD_OP_DONT_CARE;a.stencilStoreOp=VK_ATTACHMENT_STORE_OP_DONT_CARE;
   a.initialLayout=a.finalLayout=VK_IMAGE_LAYOUT_GENERAL;
   refs[i]={used++,VK_IMAGE_LAYOUT_GENERAL};
  }
  VkSubpassDescription sub{};sub.pipelineBindPoint=VK_PIPELINE_BIND_POINT_GRAPHICS;sub.colorAttachmentCount=count;sub.pColorAttachments=refs;
  // One device-wide layout for both fetch and ordinary pipelines, including
  // runtime variants: input references participate in pass compatibility.
  // orderedColorAccess is already in CompileOptions / translator identity;
  // variants inherit those options. Merge changes recording only, not this ABI.
  if(orderedColorAccess){sub.flags=VK_SUBPASS_DESCRIPTION_RASTERIZATION_ORDER_ATTACHMENT_COLOR_ACCESS_BIT_EXT;sub.inputAttachmentCount=count;sub.pInputAttachments=refs;}
  VkSubpassDependency deps[2]{};deps[0].srcSubpass=VK_SUBPASS_EXTERNAL;deps[0].dstSubpass=0;
  deps[0].srcStageMask=VK_PIPELINE_STAGE_ALL_COMMANDS_BIT;deps[0].dstStageMask=VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT;
  deps[0].srcAccessMask=VK_ACCESS_MEMORY_READ_BIT|VK_ACCESS_MEMORY_WRITE_BIT;deps[0].dstAccessMask=VK_ACCESS_COLOR_ATTACHMENT_READ_BIT|VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT;
  deps[1].srcSubpass=0;deps[1].dstSubpass=VK_SUBPASS_EXTERNAL;deps[1].srcStageMask=VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT;
  deps[1].dstStageMask=VK_PIPELINE_STAGE_ALL_COMMANDS_BIT;deps[1].srcAccessMask=VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT;deps[1].dstAccessMask=VK_ACCESS_MEMORY_READ_BIT|VK_ACCESS_MEMORY_WRITE_BIT;
  if(systemTranslationPolicy().perf&4){ // narrow external dependencies to the stages and accesses this backend uses
   const VkPipelineStageFlags producers=VK_PIPELINE_STAGE_VERTEX_SHADER_BIT|VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT|VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT|VK_PIPELINE_STAGE_TRANSFER_BIT;
   deps[0].srcStageMask=producers;deps[0].srcAccessMask=VK_ACCESS_SHADER_WRITE_BIT|VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT|VK_ACCESS_TRANSFER_WRITE_BIT;
   deps[1].dstStageMask=VK_PIPELINE_STAGE_VERTEX_INPUT_BIT|producers;deps[1].dstAccessMask=VK_ACCESS_SHADER_READ_BIT|VK_ACCESS_SHADER_WRITE_BIT|VK_ACCESS_COLOR_ATTACHMENT_READ_BIT|VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT|VK_ACCESS_TRANSFER_READ_BIT|VK_ACCESS_TRANSFER_WRITE_BIT|VK_ACCESS_INPUT_ATTACHMENT_READ_BIT|VK_ACCESS_UNIFORM_READ_BIT|VK_ACCESS_VERTEX_ATTRIBUTE_READ_BIT|VK_ACCESS_INDEX_READ_BIT;
  }
  if(orderedColorAccess){deps[0].dstStageMask|=VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT;deps[0].dstAccessMask|=VK_ACCESS_INPUT_ATTACHMENT_READ_BIT;deps[1].srcStageMask|=VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT;deps[1].srcAccessMask|=VK_ACCESS_INPUT_ATTACHMENT_READ_BIT;}
  VkRenderPassCreateInfo ri{};ri.sType=VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO;ri.attachmentCount=used;ri.pAttachments=attachments;ri.subpassCount=1;ri.pSubpasses=&sub;ri.dependencyCount=2;ri.pDependencies=deps;
  VkRenderPass render{};check(CreateRenderPass(device,&ri,nullptr,&render));
  try{p->renders.push_back({key,render});}catch(...){DestroyRenderPass(device,render,nullptr);throw;}return render;
 }
 Pipeline *createPipeline(Translation translated,const CompileOptions &options) {
  auto &tr=Translator::get();auto *p=new Pipeline{};p->options=options;{static std::atomic<uint32_t> serial{0};p->id=++serial;auto st=spirvStats(translated.fragment,translated.fragmentCount);p->fragInstr=st.instr;p->fragSamples=st.samples;p->fragLoops=st.loops;p->fragKills=st.kills;}
  if(tr.shadow_windows){ShadowWindow windows[2][resourceSlots]{};if(tr.shadow_windows(translated.handle,&windows[0][0])==1)memcpy(p->shadowWindows,windows,sizeof(windows));}
  p->textureTypes.fill(2);if(tr.texture_type)for(unsigned slot=0;slot<resourceSlots;++slot)p->textureTypes[slot]=tr.texture_type(translated.handle,slot);
  auto cleanup=[&]{if(translated.handle)tr.free(translated.handle);};
  try {
   p->retainedBytes=sizeof(Pipeline)+(translated.vertexCount+translated.fragmentCount)*4;
   if(!validFetchContract(orderedColorAccess,translated.orderedColorAccess,translated.fetchMask))throw std::runtime_error("ordered framebuffer fetch plan/device mismatch");
   uint32_t presentMask=0;for(unsigned i=0;i<options.colorCount && i<8;++i)if(options.color(i).format)presentMask|=1u<<i;
   if(translated.fetchMask&~presentMask)throw std::runtime_error("fetch references absent color attachment");
   p->fetchMask=translated.fetchMask;p->orderedColorAccess=orderedColorAccess;
   if(!options.colorCount || options.colorCount>maxColorAttachments)throw std::runtime_error("color attachment count exceeds device");
   for(unsigned i=0;i<options.colorCount;++i){auto c=options.color(i);if(!c.format)continue;VkFormatProperties features{};GetPhysicalDeviceFormatProperties(physical,pixelFormat(c.format),&features);
    auto needed=VK_FORMAT_FEATURE_COLOR_ATTACHMENT_BIT|(translated.colors[i].blend?VK_FORMAT_FEATURE_COLOR_ATTACHMENT_BLEND_BIT:0);
    if((features.optimalTilingFeatures&needed)!=(uint32_t)needed)throw std::runtime_error("attachment["+std::to_string(i)+"] format/blend unavailable");
   }
   p->render=renderPass(p,nullptr);
   p->pushOffset=translated.pushOffset;p->pushSize=translated.pushSize;p->pushStages=translated.pushStages;memcpy(p->pushBytes,translated.pushBytes,128);
   // ABI 3 exposes both stage ranges. The translator limits each to the 128-byte Vulkan minimum.
   VkPushConstantRange ranges[2]{};if(translated.rangeCount>2)throw std::runtime_error("push ranges");
   for(unsigned i=0;i<translated.rangeCount;++i){auto r=translated.ranges[i];ranges[i]={r.stages,r.offset,r.size};}
   if(translated.blobCount>2)throw std::runtime_error("constant blob count");
   for(unsigned i=0;i<translated.blobCount;++i){const auto &blob=translated.blobs[i];
    if(!blob.bytes || !blob.length || blob.length>4*1024*1024)throw std::runtime_error("constant blob bounds");
    // Identical constant tables (same content hash and length) are shared across pipelines: they are immutable, and a 3 MB table per pipeline capped the object cache at about 20 entries.
    std::string bkey((const char *)blob.hash,32);bkey.append((const char *)&blob.length,sizeof(blob.length));
    auto shared=blobShare.find(bkey);Buffer *buffer;
    if(shared!=blobShare.end() && memcmp(shared->second.first->contents,blob.bytes,blob.length)==0){buffer=shared->second.first;++shared->second.second;++blobSharedHits;}
    else{buffer=allocateUnlocked((blob.length+15)&~size_t(15));memcpy(buffer->contents,blob.bytes,blob.length);if(shared==blobShare.end())blobShare[bkey]={buffer,1};p->retainedBytes+=blob.length;}
    p->blobKeys.push_back(bkey);p->constantBlobs.push_back(buffer);
    std::array<uint8_t,32> hash{};std::copy(blob.hash,blob.hash+32,hash.begin());p->blobHashes.push_back(hash);
   }
   if(translated.descriptorCount>16)throw std::runtime_error("descriptor count>16");
   std::vector<VkDescriptorSetLayoutBinding> bindings;
   for(uint32_t i=0;i<translated.descriptorCount;++i) {
    auto d=translated.descriptors[i];
    if((d.type!=VK_DESCRIPTOR_TYPE_SAMPLER && d.type!=VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE && d.type!=VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER && d.type!=VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER && d.type!=VK_DESCRIPTOR_TYPE_STORAGE_BUFFER && d.type!=VK_DESCRIPTOR_TYPE_INPUT_ATTACHMENT) || !d.stages || (d.stages&~17u))
     throw std::runtime_error("unsupported translator descriptor");
    if(d.type==VK_DESCRIPTOR_TYPE_INPUT_ATTACHMENT && (!translated.fetchMask || d.stages!=VK_SHADER_STAGE_FRAGMENT_BIT))throw std::runtime_error("input attachment layout without fetch plan");
    if(d.set || d.source>1)throw std::runtime_error("descriptor set/source");
    if(d.source==1 && ((d.type!=VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER && d.type!=VK_DESCRIPTOR_TYPE_STORAGE_BUFFER) || d.slot>=p->constantBlobs.size() || d.offset || d.range!=p->constantBlobs[d.slot]->length))throw std::runtime_error("constant blob layout mismatch");
    if(d.binding>=16)throw std::runtime_error("descriptor binding limit");
    BakedSampler baked{};char why[256]{};
    if(!tr.sampler(translated.handle,d.binding,&baked,why,sizeof(why)))throw std::runtime_error(why);
    p->bakedSamplerInfo[d.binding]=baked;
    if(baked.enabled){VkSamplerCreateInfo si{};si.sType=VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO;
     si.magFilter=(VkFilter)baked.mag;si.minFilter=(VkFilter)baked.min;si.mipmapMode=(VkSamplerMipmapMode)baked.mip;
     si.addressModeU=(VkSamplerAddressMode)baked.address[0];si.addressModeV=(VkSamplerAddressMode)baked.address[1];si.addressModeW=(VkSamplerAddressMode)baked.address[2];
     si.mipLodBias=baked.bias;si.minLod=baked.minLod;si.maxLod=baked.maxLod;si.borderColor=(VkBorderColor)baked.border;
     check(CreateSampler(device,&si,nullptr,&p->bakedSamplers[d.binding]));
    }
    bindings.push_back({d.binding,(VkDescriptorType)d.type,1,d.stages,baked.enabled?&p->bakedSamplers[d.binding]:nullptr});p->descriptors.push_back(d);
   }
   if(!bindings.empty()) {
    VkDescriptorSetLayoutCreateInfo si{};si.sType=VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO;si.bindingCount=bindings.size();si.pBindings=bindings.data();
    check(CreateDescriptorSetLayout(device,&si,nullptr,&p->setLayout));
   }
   VkPipelineLayoutCreateInfo li{};li.setLayoutCount=p->setLayout?1:0;li.pSetLayouts=&p->setLayout;li.sType=VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO;li.pushConstantRangeCount=translated.rangeCount;li.pPushConstantRanges=ranges;check(CreatePipelineLayout(device,&li,nullptr,&p->layout));
   p->recipe=translated;
   p->mipAccess[0]=MipAccess(translated.vertex,translated.vertexCount);p->mipAccess[1]=MipAccess(translated.fragment,translated.fragmentCount);
   for(auto &d:p->descriptors)if(d.type==VK_DESCRIPTOR_TYPE_STORAGE_BUFFER)for(unsigned stage=0;stage<2;++stage)
    if(d.binding<16 && (d.stages&(stage?16u:1u)) && p->mipAccess[stage].readOnlyStorage(d.binding))p->readOnlyStorage[stage]|=1u<<d.binding;
   p->retainedBytes+=p->mipAccess[0].retainedBytes()+p->mipAccess[1].retainedBytes();
   for(auto &d:p->descriptors){if(d.type==VK_DESCRIPTOR_TYPE_STORAGE_IMAGE || d.type==VK_DESCRIPTOR_TYPE_STORAGE_TEXEL_BUFFER)p->writesStorage=true;
    else if(d.type==VK_DESCRIPTOR_TYPE_STORAGE_BUFFER && d.binding<16)for(unsigned stage=0;stage<2;++stage)if((d.stages&(stage?16u:1u)) && !(p->readOnlyStorage[stage]&(1u<<d.binding)) && p->mipAccess[stage].mayWriteBinding(d.binding))p->writesStorage=true;}
   {static std::atomic<unsigned> br{0};unsigned n=br.fetch_add(1);if(n<5||n%25==0)fprintf(stderr,"FUXI MTL: pipeline bytes n=%u total=%zu struct=%zu spirv=%zu mip_vertex=%zu mip_fragment=%zu blobs_new_or_shared=%u\n",n,p->retainedBytes,sizeof(Pipeline),size_t(translated.vertexCount+translated.fragmentCount)*4,p->mipAccess[0].retainedBytes(),p->mipAccess[1].retainedBytes(),translated.blobCount);}
   auto began=std::chrono::steady_clock::now();
   try {
    p->pipeline=buildTopologyPipeline(p,Topology::Triangle);
    p->stripPipeline=buildTopologyPipeline(p,Topology::TriangleStrip);
   }catch(...){pipelineCache.finishedPair(std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-began).count());throw;}
   auto buildMs=std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-began).count();pipelineCache.finishedPair(buildMs);
   fprintf(stderr,"FUXI MTL: Vulkan pipelines PASS %.3f ms cache=%u\n",buildMs,pipelineCache.get()!=VK_NULL_HANDLE);
   fprintf(stderr,"FUXI MTL: framebuffer fetch plan mask=%u ordered=%u\n",p->fetchMask,p->orderedColorAccess);
   p->translation=translated.handle;translated.handle=nullptr;cleanup();return p;
  }catch(...) {cleanup();destroyPipelineUnlocked(p);throw;}
 }
 // Recipe pointers belong to the retained translation product; inline arrays are copied.
 VkPipeline buildTopologyPipeline(Pipeline *p,Topology topology) {
  requireLive();if(!validTopology(topology))throw std::runtime_error("invalid pipeline topology");
  CompileBoostScope compileBoost("topology");
  const auto &translated=p->recipe;VkShaderModule modules[2]{};
  auto cleanup=[&]{for(auto m:modules)if(m)DestroyShaderModule(device,m,nullptr);};
  try {
   const uint32_t *words[]={translated.vertex,translated.fragment};size_t counts[]={translated.vertexCount,translated.fragmentCount};
   std::vector<uint32_t> relaxedFragment;
   for(unsigned i=0;i<2;++i) {
    if(!words[i] || counts[i]<5 || counts[i]>1024*1024 || words[i][0]!=0x07230203)throw std::runtime_error("invalid translator SPIR-V");
    if(i==1 && systemTranslationPolicy().fragRelaxed){unsigned marked=0,quantized=0; // FUXI_MTL_FRAG_RELAXED: f16-quantized chains may run in half precision ALUs
     if(relaxQuantizedChains(words[i],counts[i],relaxedFragment,marked,quantized)){words[i]=relaxedFragment.data();counts[i]=relaxedFragment.size();++relaxedPipelines;relaxedMarks+=marked;}
     static std::atomic<unsigned> lines{0};if(lines.fetch_add(1)<20 || lines.load()%50==0)fprintf(stderr,"FUXI MTL: frag relaxed pipe=%06x quantize_sites=%u marked=%u words=%zu->%zu pipelines=%llu total_marked=%llu\n",p->id,quantized,marked,size_t(translated.fragmentCount),counts[i],(unsigned long long)relaxedPipelines,(unsigned long long)relaxedMarks);}
    VkShaderModuleCreateInfo mi{};mi.sType=VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO;mi.codeSize=counts[i]*4;mi.pCode=words[i];check(CreateShaderModule(device,&mi,nullptr,&modules[i]));
   }
   if(translated.specCount>32 || translated.specSize>256)throw std::runtime_error("specialization bounds");
   std::vector<VkSpecializationMapEntry> specEntries;
   for(uint32_t i=0;i<translated.specCount;++i) {
    auto e=translated.specs[i];if(e.offset>translated.specSize || e.size>translated.specSize-e.offset || !(e.size==1 || e.size==2 || e.size==4 || e.size==8))throw std::runtime_error("specialization entry");
    specEntries.push_back({e.id,e.offset,e.size});
   }
   VkSpecializationInfo specialization{(uint32_t)specEntries.size(),specEntries.data(),translated.specSize,translated.specData};
   VkPipelineShaderStageCreateInfo stages[2]{};
   for(unsigned i=0;i<2;++i) {stages[i].sType=VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO;stages[i].module=modules[i];stages[i].pSpecializationInfo=&specialization;}
   stages[0].stage=VK_SHADER_STAGE_VERTEX_BIT;stages[0].pName=translated.vertexEntry;stages[1].stage=VK_SHADER_STAGE_FRAGMENT_BIT;stages[1].pName=translated.fragmentEntry;
   VkPipelineVertexInputStateCreateInfo vertex{};vertex.sType=VK_STRUCTURE_TYPE_PIPELINE_VERTEX_INPUT_STATE_CREATE_INFO;
   std::vector<VkVertexInputBindingDescription> vbindings;std::vector<VkVertexInputAttributeDescription> vattrs;
   if(translated.bindingCount>4 || translated.attributeCount>8)throw std::runtime_error("vertex input bounds");
   for(unsigned i=0;i<translated.bindingCount;++i){auto b=translated.bindings[i];vbindings.push_back({b.binding,b.stride,VK_VERTEX_INPUT_RATE_VERTEX});}
   for(unsigned i=0;i<translated.attributeCount;++i){auto a=translated.attributes[i];
    VkFormatProperties support{};GetPhysicalDeviceFormatProperties(physical,(VkFormat)a.format,&support);
    if(!(support.bufferFeatures&VK_FORMAT_FEATURE_VERTEX_BUFFER_BIT))throw std::runtime_error("vertex attribute["+std::to_string(a.location)+"] Vulkan format="+std::to_string(a.format)+" lacks VERTEX_BUFFER");
    vattrs.push_back({a.location,a.binding,(VkFormat)a.format,a.offset});}
   vertex.vertexBindingDescriptionCount=vbindings.size();vertex.pVertexBindingDescriptions=vbindings.data();vertex.vertexAttributeDescriptionCount=vattrs.size();vertex.pVertexAttributeDescriptions=vattrs.data();
   VkPipelineInputAssemblyStateCreateInfo assembly{};assembly.sType=VK_STRUCTURE_TYPE_PIPELINE_INPUT_ASSEMBLY_STATE_CREATE_INFO;static constexpr VkPrimitiveTopology topologies[]={VK_PRIMITIVE_TOPOLOGY_TRIANGLE_LIST,VK_PRIMITIVE_TOPOLOGY_TRIANGLE_STRIP,VK_PRIMITIVE_TOPOLOGY_LINE_LIST,VK_PRIMITIVE_TOPOLOGY_LINE_STRIP};
   assembly.topology=topologies[uint32_t(topology)];assembly.primitiveRestartEnable=primitiveRestart(topology);
   // Metal's viewport has a top-left origin. Vulkan 1.2 supports negative height.
   VkViewport viewport{0,256,256,-256,0,1};VkRect2D scissor{{0,0},{256,256}};
   VkPipelineViewportStateCreateInfo vp{};vp.sType=VK_STRUCTURE_TYPE_PIPELINE_VIEWPORT_STATE_CREATE_INFO;vp.viewportCount=1;vp.pViewports=&viewport;vp.scissorCount=1;vp.pScissors=&scissor;
   VkPipelineRasterizationStateCreateInfo raster{};raster.sType=VK_STRUCTURE_TYPE_PIPELINE_RASTERIZATION_STATE_CREATE_INFO;raster.polygonMode=VK_POLYGON_MODE_FILL;raster.cullMode=VK_CULL_MODE_NONE;raster.lineWidth=1;
   VkPipelineMultisampleStateCreateInfo ms{};ms.sType=VK_STRUCTURE_TYPE_PIPELINE_MULTISAMPLE_STATE_CREATE_INFO;ms.rasterizationSamples=VK_SAMPLE_COUNT_1_BIT;
   VkPipelineColorBlendAttachmentState blends[8]{};
   for(unsigned i=0;i<p->options.colorCount;++i){auto &blend=blends[i];auto c=translated.colors[i];blend.colorWriteMask=c.mask;blend.blendEnable=c.blend;
    blend.srcColorBlendFactor=(VkBlendFactor)c.factors[0];blend.dstColorBlendFactor=(VkBlendFactor)c.factors[1];blend.colorBlendOp=(VkBlendOp)c.factors[2];blend.srcAlphaBlendFactor=(VkBlendFactor)c.factors[3];blend.dstAlphaBlendFactor=(VkBlendFactor)c.factors[4];blend.alphaBlendOp=(VkBlendOp)c.factors[5];}
   VkPipelineColorBlendStateCreateInfo bs{};bs.sType=VK_STRUCTURE_TYPE_PIPELINE_COLOR_BLEND_STATE_CREATE_INFO;bs.attachmentCount=p->options.colorCount;bs.pAttachments=blends;if(orderedColorAccess)bs.flags=VK_PIPELINE_COLOR_BLEND_STATE_CREATE_RASTERIZATION_ORDER_ATTACHMENT_ACCESS_BIT_EXT;

   VkGraphicsPipelineCreateInfo gi{};gi.sType=VK_STRUCTURE_TYPE_GRAPHICS_PIPELINE_CREATE_INFO;gi.stageCount=2;gi.pStages=stages;gi.pVertexInputState=&vertex;gi.pInputAssemblyState=&assembly;
   gi.pViewportState=&vp;gi.pRasterizationState=&raster;gi.pMultisampleState=&ms;gi.pColorBlendState=&bs;gi.layout=p->layout;gi.renderPass=p->render;
   VkDynamicState dynamicStates[]={VK_DYNAMIC_STATE_VIEWPORT,VK_DYNAMIC_STATE_SCISSOR};VkPipelineDynamicStateCreateInfo dynamic{};dynamic.sType=VK_STRUCTURE_TYPE_PIPELINE_DYNAMIC_STATE_CREATE_INFO;dynamic.dynamicStateCount=2;dynamic.pDynamicStates=dynamicStates;gi.pDynamicState=&dynamic;
   auto cache=pipelineCache.get();
   VkPipelineCreationFeedback feedback{},stageFeedback[2]{};VkPipelineCreationFeedbackCreateInfo feedbackInfo{};
   if(creationFeedback){feedbackInfo.sType=VK_STRUCTURE_TYPE_PIPELINE_CREATION_FEEDBACK_CREATE_INFO;feedbackInfo.pPipelineCreationFeedback=&feedback;feedbackInfo.pipelineStageCreationFeedbackCount=2;feedbackInfo.pPipelineStageCreationFeedbacks=stageFeedback;gi.pNext=&feedbackInfo;}
   auto createStart=std::chrono::steady_clock::now();
#ifdef VK_KHR_pipeline_executable_properties
   if(executableStats && topology==Topology::Triangle)gi.flags|=VK_PIPELINE_CREATE_CAPTURE_STATISTICS_BIT_KHR;
#endif
   VkPipeline result{};VkResult rc;
   if(unlockForCompile){ // Turnip's compile can take hundreds of ms: do not hold the backend lock (submits, allocations) across it. The pipeline is not yet published.
    mutex.unlock();rc=CreateGraphicsPipelines(device,cache,1,&gi,nullptr,&result);mutex.lock();
    if(poisoned){if(rc==VK_SUCCESS && result)DestroyPipeline(device,result,nullptr);throw std::runtime_error("backend poisoned during pipeline compile");}
   }else rc=CreateGraphicsPipelines(device,cache,1,&gi,nullptr,&result);
   if(rc==VK_SUCCESS){static std::atomic<unsigned> diag{0};unsigned n=diag.fetch_add(1);
    if(n<10||n%25==0){size_t cacheBytes=0;if(cache)GetPipelineCacheData(device,cache,&cacheBytes,nullptr);
     fprintf(stderr,"FUXI MTL: pipeline create diag n=%u topology=%d ms=%.2f feedback_valid=%d cache_hit=%d stage_ms=%.2f/%.2f stage_hit=%d/%d cache_bytes=%zu flags=0x%x\n",n,int(topology),std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-createStart).count(),
      int(!!(feedback.flags&VK_PIPELINE_CREATION_FEEDBACK_VALID_BIT)),int(!!(feedback.flags&VK_PIPELINE_CREATION_FEEDBACK_APPLICATION_PIPELINE_CACHE_HIT_BIT)),stageFeedback[0].duration/1e6,stageFeedback[1].duration/1e6,
      int(!!(stageFeedback[0].flags&VK_PIPELINE_CREATION_FEEDBACK_APPLICATION_PIPELINE_CACHE_HIT_BIT)),int(!!(stageFeedback[1].flags&VK_PIPELINE_CREATION_FEEDBACK_APPLICATION_PIPELINE_CACHE_HIT_BIT)),cacheBytes,unsigned(gi.flags));}
   }
   if(rc==VK_SUCCESS && systemTranslationPolicy().frameTrace)++frameCounts.pipelines;
   if(rc!=VK_SUCCESS){if(result)DestroyPipeline(device,result,nullptr);check(rc);}
   if(systemTranslationPolicy().pipeStats && topology==Topology::Triangle)logPipelineStats(p,translated,result);
   if(cacheMode==CacheMode::persistent){
    if(!(feedback.flags&VK_PIPELINE_CREATION_FEEDBACK_VALID_BIT))++driverUnknown;
    else if(feedback.flags&VK_PIPELINE_CREATION_FEEDBACK_APPLICATION_PIPELINE_CACHE_HIT_BIT)++driverHits;else ++driverMisses;
   }
   cleanup();return result;
  }catch(...){cleanup();throw;}
 }
 // FUXI_MTL_PIPELINE_STATS=1: what the translator produced for this pipeline (entry points, bound function constants, SPIR-V shape) and, when the driver
 // exposes VK_KHR_pipeline_executable_properties, the post-optimization statistics (ir3 instruction counts, registers, spills).
 void logPipelineStats(Pipeline *p,const Translation &t,VkPipeline pipeline) {
  auto st=spirvStats(t.fragment,t.fragmentCount);auto vs=spirvStats(t.vertex,t.vertexCount);
  fprintf(stderr,"FUXI MTL: pipeline stats pipe=%06x frag=%s vert=%s frag_words=%u frag_instr=%u samples=%u fetches=%u loops=%u kills=%u spec_consts=%u switches=%u cond_br=%u selections=%u calls=%u phis=%u converts=%u f16_types=%u f32_types=%u relaxed=%u access_chains=%u quantize_f16=%u loops_dontunroll=%u loops_none=%u vert_instr=%u vk_specs=%u fn_constants=%u/%u\n",
   p->id,t.fragmentEntry?t.fragmentEntry:"?",t.vertexEntry?t.vertexEntry:"?",st.words,st.instr,st.samples,st.fetches,st.loops,st.kills,st.specConsts,st.switches,st.condBranches,st.selections,st.calls,st.phis,st.converts,st.f16Types,st.f32Types,st.relaxed,st.accessChains,st.quantize,st.loopsDontUnroll,st.loopsNone,vs.instr,t.specCount,p->options.constantCount[0],p->options.constantCount[1]);
  if(st.instr>=5000 && t.fragment){auto loops=spirvLoopReport(t.fragment,t.fragmentCount);unsigned bounds[5]{},sampleLoops=0,maxDepth=0;const char *names[]={"const","uniform","texel","local","unknown"};
   for(auto &l:loops){for(unsigned k=0;k<5;++k)if(l.bound==names[k])++bounds[k];if(l.samples)++sampleLoops;if(l.depth>maxDepth)maxDepth=l.depth;}
   fprintf(stderr,"FUXI MTL: pipeline loops pipe=%06x n=%zu const=%u uniform=%u texel=%u local=%u unknown=%u loops_with_samples=%u max_depth=%u\n",p->id,loops.size(),bounds[0],bounds[1],bounds[2],bounds[3],bounds[4],sampleLoops,maxDepth);
   std::string line;char b[96];for(size_t i=0;i<loops.size() && i<48;++i){auto &l=loops[i];snprintf(b,sizeof b," L%zu[d%u,blk%u,i%u,s%u,%s]",i,l.depth,l.blocks,l.insts,l.samples,l.bound.c_str());line+=b;if(i%6==5 || i+1==loops.size() || i==47){fprintf(stderr,"FUXI MTL: pipeline loops pipe=%06x%s\n",p->id,line.c_str());line.clear();}}}
  if(const char *dir=getenv("FUXI_MTL_SPIRV_DUMP_DIR")){ // raw SPIR-V of heavy pipelines for offline spirv-dis; the directory must already exist
   if(st.instr>=5000 && t.fragment){char path[512];snprintf(path,sizeof path,"%s/pipe%06x.frag.spv",dir,p->id);if(FILE *f=fopen(path,"wb")){fwrite(t.fragment,4,t.fragmentCount,f);fclose(f);fprintf(stderr,"FUXI MTL: pipeline stats pipe=%06x dumped fragment SPIR-V\n",p->id);}}}
  for(unsigned stage=0;stage<2;++stage){unsigned n=std::min<unsigned>(p->options.constantCount[stage],maxFunctionConstants);if(!n)continue;
   std::string line;char b[96];for(unsigned i=0;i<n && i<24;++i){const auto &c=p->options.constants[stage][i];std::string hex;for(unsigned k=0;k<c.size && k<16;++k){snprintf(b,sizeof b,"%02x",c.bytes[k]);hex+=b;}
    snprintf(b,sizeof b," %s#%u:t%u=",c.name[0]?c.name:"-",c.index,c.type);line+=b;line+=hex;}
   fprintf(stderr,"FUXI MTL: pipeline stats pipe=%06x %s constants%s\n",p->id,stage?"fragment":"vertex",line.c_str());}
  for(unsigned i=0;i<t.specCount && i<32;++i){const auto &e=t.specs[i];uint64_t v=0;if(e.size<=8 && e.offset+e.size<=t.specSize)memcpy(&v,t.specData+e.offset,e.size);fprintf(stderr,"FUXI MTL: pipeline stats pipe=%06x vk_spec id=%u size=%u value=%llu\n",p->id,e.id,e.size,(unsigned long long)v);}
#ifdef VK_KHR_pipeline_executable_properties
  if(!executableStats || !pipeline)return;
  VkPipelineInfoKHR info{};info.sType=VK_STRUCTURE_TYPE_PIPELINE_INFO_KHR;info.pipeline=pipeline;uint32_t count=0;
  if(GetPipelineExecutablePropertiesKHR(device,&info,&count,nullptr)!=VK_SUCCESS || !count || count>16)return;
  std::vector<VkPipelineExecutablePropertiesKHR> props(count);for(auto &x:props){x={};x.sType=VK_STRUCTURE_TYPE_PIPELINE_EXECUTABLE_PROPERTIES_KHR;}
  if(GetPipelineExecutablePropertiesKHR(device,&info,&count,props.data())<0)return;
  for(uint32_t e=0;e<count;++e){VkPipelineExecutableInfoKHR ei{};ei.sType=VK_STRUCTURE_TYPE_PIPELINE_EXECUTABLE_INFO_KHR;ei.pipeline=pipeline;ei.executableIndex=e;uint32_t n=0;
   if(GetPipelineExecutableStatisticsKHR(device,&ei,&n,nullptr)!=VK_SUCCESS || !n || n>64)continue;
   std::vector<VkPipelineExecutableStatisticKHR> stats(n);for(auto &x:stats){x={};x.sType=VK_STRUCTURE_TYPE_PIPELINE_EXECUTABLE_STATISTIC_KHR;}
   if(GetPipelineExecutableStatisticsKHR(device,&ei,&n,stats.data())<0)continue;
   std::string line;char b[160];
   for(uint32_t k=0;k<n;++k){const auto &s=stats[k];
    switch(s.format){case VK_PIPELINE_EXECUTABLE_STATISTIC_FORMAT_BOOL32_KHR:snprintf(b,sizeof b," %s=%u",s.name,unsigned(s.value.b32));break;case VK_PIPELINE_EXECUTABLE_STATISTIC_FORMAT_INT64_KHR:snprintf(b,sizeof b," %s=%lld",s.name,(long long)s.value.i64);break;
     case VK_PIPELINE_EXECUTABLE_STATISTIC_FORMAT_UINT64_KHR:snprintf(b,sizeof b," %s=%llu",s.name,(unsigned long long)s.value.u64);break;default:snprintf(b,sizeof b," %s=%.3f",s.name,s.value.f64);}
    for(char *q=b+1;*q && *q!='=';++q)if(*q==' ')*q='_';line+=b;}
   fprintf(stderr,"FUXI MTL: pipeline exec pipe=%06x stage=0x%x name=%s%s\n",p->id,unsigned(props[e].stages),props[e].name,line.c_str());}
#endif
 }
 VkPipeline topologyPipeline(Pipeline *p,Topology topology) {
  requireLive();if(!validTopology(topology))throw std::runtime_error("invalid pipeline topology");
  if(topology==Topology::Triangle)return p->pipeline;
  if(topology==Topology::TriangleStrip)return p->stripPipeline;
  auto &cached=p->linePipelines[uint32_t(topology)-2];
  if(!cached){auto began=std::chrono::steady_clock::now();cached=buildTopologyPipeline(p,topology);
   auto ms=std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-began).count();pipelineCache.finishedPair(ms);
   fprintf(stderr,"FUXI MTL: Vulkan pipelines PASS %.3f ms cache=%u topology=%u\n",ms,pipelineCache.get()!=VK_NULL_HANDLE,uint32_t(topology));
  }return cached;
 }
 // The caller must invoke retirement exactly once, before Backend destruction.
 // Slots pin every referenced object, including resources released by the client.
 // Hoist independent render groups so passes on the same attachments become adjacent (and then merge). A group is a run of draws with the
 // same attachments; anything that is not a plain draw (copies, uploads, clears, mips, storage or fetch shaders) is a fixed barrier no group
 // crosses. Group B hops over earlier groups only when no target of one is a target, sample or aliasing buffer of the other.
 struct PassGroup {size_t first,last;bool barrier;unsigned count,mask;std::array<Texture *,8> targets{};std::array<uint32_t,8> levels{};std::vector<Texture *> samples;std::vector<Buffer *> buffers;
  bool sameTargets(const PassGroup &o) const{return !barrier && !o.barrier && count==o.count && mask==o.mask && targets==o.targets && levels==o.levels;}};
 static bool textureTouch(const Texture *a,const Texture *b){return a==b || a->image==b->image || memoryAlias(a,b) || hostRangesOverlap(a->mapped,a->allocated,b->mapped,b->allocated);}
 static bool bufferTouchesTexture(const Buffer *b,const Texture *t){return b->memory==t->memory || hostRangesOverlap(b->contents,b->length,t->mapped,t->allocated);}
 static bool groupsConflict(const PassGroup &a,const PassGroup &b){
  auto writes=[](const PassGroup &w,const PassGroup &r){
   for(auto *t:w.targets){if(!t)continue;
    for(auto *u:r.targets)if(u && textureTouch(t,u))return true;
    for(auto *s:r.samples){if(textureTouch(t,s))return true;if(s->shadow && s->backingBuffer && bufferTouchesTexture(s->backingBuffer,t))return true;}
    for(auto *x:r.buffers)if(bufferTouchesTexture(x,t))return true;}
   return false;};
  return writes(a,b) || writes(b,a);
 }
uint64_t rtShadowIn=0,rtShadowInSkipped=0,rtShadowOut=0,rtShadowSyncs=0,rtShadowReused=0,rtShadowRedirects=0,rtShadowCreated=0,rtEligibleSeen=0,rtLinearLargeSeen=0,rtSubmits=0,opSequenceSubmits=0;std::unordered_set<uint64_t> targetLogged;
 // Shadows are keyed by the linear memory (the backing buffer's host key), not by the texture object: CA recreates the drawable texture around the same IOSurface.
 struct RtEntry {Texture *s;uint32_t w,h,format,sampled;size_t offset,row;uint64_t used;bool clean;};
 std::unordered_map<const void *,RtEntry> rtShadows;std::unordered_set<const void *> rtRefused;
 // FUXI_MTL_OCCLUDE (default off). A draw D is skipped only when a LATER kept draw C of the same Metal encoder and target provably paints every pixel D can touch: D's footprint is bounded by its scissor (no geometry needed),
 // C's coverage is proven from its real vertex positions (CPU evaluation of the translated vertex shader, SpirvEval + OcclusionProof). C must be dst-independent (no blend or zero dst factors on a unorm target), write every
 // channel D writes, have no discard and no fb-fetch/storage side effects. Anything between D and C that could observe D's pixels (sampling or fetching the target, storage writers, non-draw ops) forgets the coverers.
 // Culled draws stay in `ops` (pass load/store semantics untouched) and are only not recorded. Load elision turns LOAD into DONT_CARE when a kept dst-independent coverer paints the whole render area and nothing before it reads the target.
 struct OccPlan {std::vector<uint8_t> culled,hasCover,reads,mask;std::vector<Cover> cover;};
 std::atomic<uint64_t> occSubmits{0},occVictims{0},occCoverTries{0},occCoverOk{0},occCulled{0},occElided{0},occEvalNs{0},occEstNs{0},occUnpriced{0},occCulledKpx{0};
 std::mutex occMutex;std::map<std::string,uint64_t> occWhy;std::unordered_map<uint32_t,double> occRate;double occRateAll=0;std::vector<uint8_t> occZero;struct OccEval {bool ok=false;std::string why;std::vector<std::array<float,4>> pos;};std::map<std::array<uint64_t,2>,OccEval> occEvalCache;std::atomic<uint64_t> occEvalHits{0};
 void occFail(const std::string &why){std::lock_guard<std::mutex> g(occMutex);++occWhy[why.substr(0,40)];}
 void occNoteDrawTimes(const std::vector<DrawInfo> &infos,const std::vector<uint64_t> &ns){
  std::lock_guard<std::mutex> g(occMutex);
  for(size_t i=0;i<infos.size() && i<ns.size();++i){auto &d=infos[i];uint64_t px=uint64_t(d.scW)*d.scH;if(d.begin || !d.pipeline || px<1024)continue;double rate=double(ns[i])/(double(px)/1000.0);
   auto it=occRate.find(d.pipeline);if(it==occRate.end())occRate[d.pipeline]=rate;else it->second=it->second*0.8+rate*0.2;occRateAll=occRateAll?occRateAll*0.9+rate*0.1:rate;}
 }
 static IRect occFoot(const Operation &o){
  int64_t W=o.texture->levelWidth(o.dstLevel),H=o.texture->levelHeight(o.dstLevel);int64_t aw=o.areaWidth?std::min<int64_t>(o.areaWidth,W):W,ah=o.areaHeight?std::min<int64_t>(o.areaHeight,H):H;
  int64_t x0=std::max<int64_t>(0,o.scissor.offset.x),y0=std::max<int64_t>(0,o.scissor.offset.y),x1=std::min<int64_t>(aw,int64_t(o.scissor.offset.x)+o.scissor.extent.width),y1=std::min<int64_t>(ah,int64_t(o.scissor.offset.y)+o.scissor.extent.height);
  if(x1<=x0 || y1<=y0)return {};return {int32_t(x0),int32_t(y0),int32_t(x1),int32_t(y1)};
 }
 bool occVertices(const Operation &op,const DrawOutput &draw,const std::vector<Texture *> &targets,std::vector<std::array<float,4>> &pos,std::string &why){
  auto &r=op.pipeline->recipe;
  if(!r.vertex || r.vertexCount<5 || !r.vertexEntry){why="no vertex spirv";return false;}
  if(draw.count<3 || draw.count>96){why="vertex count";return false;}
  auto touches=[&](const Buffer *b){for(auto *t:targets)if(bufferTouchesTexture(b,t))return true;return false;};
  VsInputs in;
  for(uint32_t i=0;i<r.specCount && i<32;++i){auto e=r.specs[i];if(e.offset>r.specSize || e.size>r.specSize-e.offset){why="spec";return false;}uint32_t v=0;memcpy(&v,r.specData+e.offset,std::min<uint32_t>(e.size,4));in.spec[e.id]=v;}
  for(uint32_t i=0;i<draw.descriptorCount;++i){const auto &d=draw.descriptors[i];
   if(!(d.stages&1) || (d.type!=VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER && d.type!=VK_DESCRIPTOR_TYPE_STORAGE_BUFFER))continue;
   VsInputs::Region reg{};
   if(d.source==2){if(d.offset || d.range>(1u<<20)){why="neutral buffer";return false;}if(occZero.size()<d.range)occZero.assign(size_t(d.range),0);reg={occZero.data(),size_t(d.range)};}
   else if(d.source==1){if(d.slot>=op.pipeline->constantBlobs.size() || d.offset){why="constant blob";return false;}auto *b=op.pipeline->constantBlobs[d.slot];if(!b || !b->contents || d.range>b->length){why="constant blob";return false;}reg={(const uint8_t *)b->contents,size_t(d.range)};}
   else {if(d.slot>=resourceSlots){why="buffer slot";return false;}auto b=op.buffers[d.stages==1?0:1][d.slot];if(!b.buffer || !b.buffer->contents || touches(b.buffer)){why="vertex buffer";return false;}
    if(d.offset>=b.buffer->length){why="buffer range";return false;}reg={(const uint8_t *)b.buffer->contents+d.offset,size_t(std::min<uint64_t>(d.range,b.buffer->length-d.offset))};}
   in.buffers[{d.set,d.binding}]=reg;
  }
  for(uint32_t i=0;i<draw.pushCount && i<2;++i){const auto &w=draw.pushes[i];if(!(w.stages&1))continue;if(w.offset>128 || w.size>128-w.offset){why="push range";return false;}memcpy(in.push+w.offset,w.bytes,w.size);in.pushSize=std::max(in.pushSize,w.offset+w.size);}
  auto fetchAttr=[&](uint32_t loc,uint32_t vi,uint32_t out[4])->bool {
   const VertexAttribute *a=nullptr;for(uint32_t i=0;i<r.attributeCount && i<8;++i)if(r.attributes[i].location==loc)a=&r.attributes[i];if(!a)return false;
   int k=-1;for(uint32_t i=0;i<draw.vertexCount && i<4;++i)if(draw.vertexBindings[i]==a->binding)k=int(i);if(k<0)return false;
   uint32_t stride=0;bool found=false;for(uint32_t i=0;i<r.bindingCount && i<4;++i)if(r.bindings[i].binding==a->binding){stride=r.bindings[i].stride;found=true;}if(!found)return false;
   auto slot=draw.vertexSlots[k];if(slot>=resourceSlots)return false;auto *buf=op.buffers[0][slot].buffer;if(!buf || !buf->contents || touches(buf))return false;
   uint64_t addr=draw.vertexOffsets[k]+uint64_t(vi)*stride+a->offset;if(addr>=buf->length)return false;
   return decodeAttribute(a->format,(const uint8_t *)buf->contents+addr,size_t(buf->length-addr),out);};
  std::vector<uint32_t> index;
  if(draw.indexed){auto *ib=op.indices;if(!ib || !ib->contents || touches(ib) || (op.indexWidth!=2 && op.indexWidth!=4)){why="index buffer";return false;}
   for(uint32_t i=0;i<draw.count;++i){uint64_t at=draw.indexOffset+uint64_t(draw.first+i)*op.indexWidth;if(at+op.indexWidth>ib->length){why="index range";return false;}
    uint32_t v=0;memcpy(&v,(const uint8_t *)ib->contents+at,op.indexWidth);if(v==(op.indexWidth==2?0xffffu:0xffffffffu)){why="primitive restart";return false;}index.push_back(v);}}
  else for(uint32_t i=0;i<draw.count;++i)index.push_back(draw.first+i);
  // Attributes are decoded up front so the whole input can be hashed: CA redraws mostly identical vertex streams every frame, and the evaluation result is a pure function of these inputs.
  std::map<std::pair<uint32_t,uint32_t>,std::array<uint32_t,4>> attrs;
  for(uint32_t ai=0;ai<r.attributeCount && ai<8;++ai)for(uint32_t vi:index){std::array<uint32_t,4> lane{};if(fetchAttr(r.attributes[ai].location,vi,lane.data()))attrs[{r.attributes[ai].location,vi}]=lane;}
  in.attribute=[&](uint32_t loc,uint32_t vi,uint32_t out[4])->bool {auto it=attrs.find({loc,vi});if(it==attrs.end())return false;memcpy(out,it->second.data(),16);return true;};
  uint64_t h1=0xcbf29ce484222325ull,h2=0x84222325cbf29ce4ull;
  auto mix=[&](const void *q,size_t n){auto *b=(const uint8_t *)q;for(size_t i=0;i<n;++i){h1=(h1^b[i])*0x100000001b3ull;h2=(h2+b[i]+1)*0x9e3779b97f4a7c15ull;h2^=h2>>29;}};
  auto mix64=[&](uint64_t v){mix(&v,8);};
  mix64(op.pipeline->id);mix64(r.vertexCount);mix64(index.size());for(auto v:index)mix64(v);
  for(auto &e:in.spec){mix64(e.first);mix64(e.second);}
  for(auto &e:in.buffers){if(e.second.size>(1u<<16)){why="uniform range too large";return false;}mix64(e.first.first);mix64(e.first.second);mix64(e.second.size);mix(e.second.data,e.second.size);}
  mix64(in.pushSize);mix(in.push,in.pushSize);
  for(auto &e:attrs){mix64(e.first.first);mix64(e.first.second);mix(e.second.data(),16);}
  std::array<uint64_t,2> key{h1,h2};
  auto hit=occEvalCache.find(key);
  if(hit!=occEvalCache.end()){++occEvalHits;if(!hit->second.ok){why=hit->second.why;return false;}pos=hit->second.pos;return true;}
  auto t0=std::chrono::steady_clock::now();SpirvEval ev(r.vertex,r.vertexCount);auto res=ev.run(r.vertexEntry,in,index);
  occEvalNs+=uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now()-t0).count());
  if(occEvalCache.size()>=256)occEvalCache.clear();
  auto &slot=occEvalCache[key];slot.ok=res.ok && res.position.size()==index.size();slot.why=res.ok?"eval size":"eval:"+res.why;if(slot.ok)slot.pos=res.position;
  if(!slot.ok){why=slot.why;return false;}
  pos=std::move(res.position);return true;
 }
 static bool occUnorm(VkFormat f){return f==VK_FORMAT_B8G8R8A8_UNORM || f==VK_FORMAT_R8G8B8A8_UNORM || f==VK_FORMAT_B8G8R8A8_SRGB || f==VK_FORMAT_R8G8B8A8_SRGB || f==VK_FORMAT_A2B10G10R10_UNORM_PACK32 || f==VK_FORMAT_R8_UNORM || f==VK_FORMAT_R8G8_UNORM;}
 void occludeAnalyze(const std::vector<Operation> &ops,const std::vector<DrawOutput> &draws,OccPlan &plan){
  const size_t N=ops.size();if(occSubmits%600==599)reportOcclude();plan.culled.assign(N,0);plan.hasCover.assign(N,0);plan.reads.assign(N,0);plan.mask.assign(N,0);plan.cover.assign(N,Cover{});++occSubmits;
  const uint64_t minPx=systemTranslationPolicy().occludeMinPx;
  std::vector<Texture *> targets;for(auto &o:ops)for(unsigned i=0;i<o.colorCount;++i)if(auto *t=o.color(i).texture)targets.push_back(t);
  auto plain=[&](const Operation &o){return o.texture && o.pipeline && !o.clearOnly && !o.upload && !o.generateMips && !o.src && !o.dst;};
  auto storageWriter=[&](size_t n){auto &o=ops[n];if(o.pipeline->writesStorage)return true;
   for(unsigned j=0;j<draws[n].descriptorCount;++j){auto &d=draws[n].descriptors[j];if(d.type==VK_DESCRIPTOR_TYPE_STORAGE_IMAGE || d.type==VK_DESCRIPTOR_TYPE_STORAGE_TEXEL_BUFFER)return true;
    if(d.type==VK_DESCRIPTOR_TYPE_STORAGE_BUFFER)for(unsigned stage=0;stage<2;++stage)if((d.stages&(stage?16u:1u)) && (d.binding>=16 || !(o.pipeline->readOnlyStorage[stage]&(1u<<d.binding))))return true;}
   return false;};
  auto readsTexture=[&](const Operation &o,const Texture *t){
   for(unsigned slot=0;slot<resourceSlots;++slot){auto *s=o.samples[slot];if(!slot && !s)s=o.sourceTexture;if(!s)continue;if(textureTouch(s,t))return true;if(s->shadow && s->backingBuffer && bufferTouchesTexture(s->backingBuffer,t))return true;}
   for(auto &stage:o.buffers)for(auto &b:stage)if(b.buffer && bufferTouchesTexture(b.buffer,t))return true;
   if(o.indices && bufferTouchesTexture(o.indices,t))return true;
   return false;};
  auto key=[&](const Operation &o){return std::make_tuple(o.texture,o.dstLevel,o.encoderID);};
  auto victimOk=[&](size_t n){auto &o=ops[n];return plain(o) && o.colorCount==1 && o.encoderID && !o.pipeline->fetchMask && !storageWriter(n) && occFoot(o).area()>=minPx;};
  std::vector<uint8_t> gate(N,0);
  {std::set<std::tuple<Texture *,uint32_t,uint64_t>> victims,loads;
   for(size_t n=0;n<N;++n){auto &o=ops[n];if(!plain(o) || o.colorCount!=1 || !o.encoderID)continue;auto k=key(o);if(o.load==1)loads.insert(k);gate[n]=victims.count(k) || loads.count(k);if(victimOk(n))victims.insert(k);}}
  struct Entry {Texture *tex;uint32_t level;uint64_t enc;std::vector<std::pair<uint32_t,const Cover *>> covers;};
  std::vector<Entry> st;unsigned evals=0;
  for(size_t n=N;n-->0;){
   auto &o=ops[n];
   if(!plain(o)){st.clear();continue;}
   Entry *own=nullptr;for(auto &e:st)if(e.tex==o.texture && e.level==o.dstLevel && e.enc==o.encoderID)own=&e;
   if(own && !own->covers.empty() && victimOk(n)){
    ++occVictims;uint32_t dm=o.pipeline->recipe.colors[0].mask&15;IRect R=occFoot(o);std::vector<const Cover *> use;std::vector<IRect> rects;
    for(auto &c:own->covers)if((c.first&dm)==dm){use.push_back(c.second);rects.insert(rects.end(),c.second->rects.begin(),c.second->rects.end());}
    if(use.empty())occFail("victim:mask not covered");
    else {bool ok=coveredByUnion(R,rects);for(auto *c:use)if(!ok && c->covers(R))ok=true;
     if(!ok)occFail("victim:footprint not covered");
     else {plan.culled[n]=1;++occCulled;double kpx=double(R.area())/1000.0,rate=0;{std::lock_guard<std::mutex> g(occMutex);auto it=occRate.find(o.pipeline->id);rate=it!=occRate.end()?it->second:occRateAll;}
      occCulledKpx+=uint64_t(kpx);if(rate>0)occEstNs+=uint64_t(kpx*rate);else ++occUnpriced;continue;}}
   }
   // kept draw: forget coverers whose pixels it can observe
   bool storage=storageWriter(n),fetch=o.pipeline->fetchMask!=0,multi=o.colorCount!=1;
   for(size_t i=0;i<st.size();){auto &e=st[i];bool rd=storage || fetch || multi || readsTexture(o,e.tex);
    if(!rd)for(unsigned k=0;k<o.colorCount;++k){auto *t=o.color(k).texture;if(t && t!=o.texture && textureTouch(t,e.tex))rd=true;}
    if(rd)st.erase(st.begin()+i);else ++i;}
   plan.reads[n]=storage || fetch || multi || readsTexture(o,o.texture);
   if(multi || fetch || storage || !o.encoderID || !gate[n] || plan.reads[n])continue;
   auto &p=o.pipeline;auto c=p->recipe.colors[0];uint32_t mask=c.mask&15;
   IRect clip=occFoot(o);
   if(o.pipeline->fragKills){occFail("cover:discard");continue;}
   if(!mask){continue;}
   if(!blendDstIndependent(c.blend,c.factors)){occFail("cover:dst-dependent blend");continue;}
   if(c.blend && !occUnorm(o.texture->vkFormat)){occFail("cover:blend on non-unorm");continue;}
   if(o.topology!=Topology::Triangle && o.topology!=Topology::TriangleStrip){occFail("cover:topology");continue;}
   {const auto &vp=o.viewport;double ax=std::min<double>(vp.x,double(vp.x)+vp.width),bx=std::max<double>(vp.x,double(vp.x)+vp.width),ay=std::min<double>(vp.y,double(vp.y)+vp.height),by=std::max<double>(vp.y,double(vp.y)+vp.height);
    if(!(std::fabs(ax)<1e7 && std::fabs(bx)<1e7 && std::fabs(ay)<1e7 && std::fabs(by)<1e7)){occFail("cover:viewport");continue;}
    clip=intersect(clip,{int32_t(std::ceil(ax)),int32_t(std::ceil(ay)),int32_t(std::floor(bx)),int32_t(std::floor(by))});}
   if(clip.area()<minPx)continue;
   if(evals>=64){occFail("cover:eval cap");continue;}
   ++evals;++occCoverTries;
   std::vector<std::array<float,4>> pos;std::string why;
   if(!occVertices(o,draws[n],targets,pos,why)){occFail("cover:"+why);continue;}
   ViewportF vf{o.viewport.x,o.viewport.y,o.viewport.width,o.viewport.height};
   if(!buildCover(pos,o.topology==Topology::Triangle?0:1,vf,clip,plan.cover[n],why)){occFail("cover:"+why);continue;}
   ++occCoverOk;plan.hasCover[n]=1;plan.mask[n]=uint8_t(mask);
   Entry *dst=nullptr;for(auto &e:st)if(e.tex==o.texture && e.level==o.dstLevel)dst=&e;
   if(dst && dst->enc!=o.encoderID){dst->enc=o.encoderID;dst->covers.clear();}
   if(!dst){st.push_back({o.texture,o.dstLevel,o.encoderID,{}});dst=&st.back();}
   dst->covers.push_back({mask,&plan.cover[n]});
  }
 }
 // The pass [from,to] may skip loading its attachment when a kept coverer paints the whole render area and no kept draw before it can read the old contents.
 bool occElide(const std::vector<Operation> &ops,const OccPlan &plan,size_t from,size_t to,uint32_t aw,uint32_t ah) const {
  auto &first=ops[from];if(first.colorCount!=1 || first.load!=1 || !first.texture || !aw || !ah)return false;IRect area{0,0,int32_t(aw),int32_t(ah)};
  for(size_t k=from;k<=to && k<ops.size();++k){auto &o=ops[k];
   if(!(o.texture==first.texture && o.dstLevel==first.dstLevel && o.pipeline && !o.clearOnly && !o.upload && !o.generateMips && !o.src && !o.dst && o.colorCount==1))return false;
   if(plan.culled[k])continue;
   if(plan.hasCover[k] && plan.mask[k]==15 && plan.cover[k].covers(area))return true;
   if(plan.reads[k])return false;}
  return false;
 }
 const void *rtKey(Texture *t) {return t->backingBuffer?(const void *)t->backingBuffer->shadowKey():(const void *)t;}
 Texture *rtShadowOf(Texture *t) {auto i=rtShadows.find(rtKey(t));return i==rtShadows.end()?nullptr:i->second.s;}
 void rtForget(const void *key) {auto i=rtShadows.find(key);if(i!=rtShadows.end())i->second.clean=false;} // the linear memory is going away or changing hands: the shadow no longer provably matches
 void rtForget(Texture *t) {rtForget((const void *)t);}
 void releaseRtShadows() {for(auto &e:rtShadows)destroyTextureUnlocked(e.second.s);rtShadows.clear();rtRefused.clear();}
 void reportOcclude() {
  {std::string why;{std::lock_guard<std::mutex> g(occMutex);std::vector<std::pair<uint64_t,std::string>> v;for(auto &e:occWhy)v.push_back({e.second,e.first});std::sort(v.rbegin(),v.rend());for(size_t i=0;i<v.size() && i<10;++i)why+=" "+v[i].second+"="+std::to_string(v[i].first);}
   fprintf(stderr,"FUXI MTL: occlude submits=%llu victims=%llu cover_tries=%llu cover_ok=%llu culled=%llu culled_kpx=%llu culled_est_ms=%.3f unpriced=%llu loads_elided=%llu eval_ms=%.3f eval_cache_hits=%llu min_px=%u why:%s\n",(unsigned long long)occSubmits,(unsigned long long)occVictims,(unsigned long long)occCoverTries,(unsigned long long)occCoverOk,(unsigned long long)occCulled,(unsigned long long)occCulledKpx,double(occEstNs)/1e6,(unsigned long long)occUnpriced,(unsigned long long)occElided,double(occEvalNs)/1e6,(unsigned long long)occEvalHits,systemTranslationPolicy().occludeMinPx,why.c_str());}
 }
 void reportRtCounters() {
  {uint64_t n=submitEntered,s=submits;if(n && s)fprintf(stderr,"FUXI MTL: submit pipeline submits=%llu encode_ms=%.3f slot_wait_ms=%.3f blocked_pct=%.1f blocked_full=%llu blocked_drain=%llu blocked_conflict=%llu gpu_latency_ms=%.3f retire_host_ms=%.3f (per submit; encode includes slot waits; gpu_latency is queue submit to fence)\n",(unsigned long long)n,submitPrepareNanos/1e6/n,submitSlotWaitNanos/1e6/n,100.0*submitBlocked/n,(unsigned long long)submitBlockedFull,(unsigned long long)submitBlockedDrain,(unsigned long long)submitBlockedConflict,submitWaitNanos/1e6/s,submitRetireNanos/1e6/s);}
  if(systemTranslationPolicy().occlude)reportOcclude();
  if(systemTranslationPolicy().sampleShadow)fprintf(stderr,"FUXI MTL: sample shadow min_px=%u created=%llu created_mb=%.2f reused=%llu capped=%llu evicted=%llu live=%zu live_mb=%.2f refused=%llu copies=%llu hits=%llu stale_invalidations=%llu copied_mb=%.1f hashed_mb=%.1f hash_ms=%.2f\n",systemTranslationPolicy().sampleMinPx,(unsigned long long)sampleShadowCreated,sampleShadowBytes/1048576.0,(unsigned long long)sampleShadowReused,(unsigned long long)sampleShadowCapped,(unsigned long long)sampleShadowEvicted,sampleShadows.size(),sampleShadowLiveBytes()/1048576.0,(unsigned long long)sampleShadowRefused,(unsigned long long)sampleCopies,(unsigned long long)sampleHits,(unsigned long long)sampleInvalidations,sampleCopiedBytes/1048576.0,sampleHashedBytes/1048576.0,sampleHashNs/1e6);
  if(systemTranslationPolicy().rtRename){uint64_t mb=0;for(auto &e:renamePool)mb+=uint64_t(e.w)*e.h*texelBytes(e.format);
   fprintf(stderr,"FUXI MTL: rt rename submits=%llu renamed=%llu hoisted=%llu merged_main_passes=%llu main_runs_before=%llu main_runs_after=%llu pool_images=%zu pool_mb=%.2f\n",(unsigned long long)rtRenameSubmits,(unsigned long long)rtRenamed,(unsigned long long)rtHoisted,(unsigned long long)rtMergedMain,(unsigned long long)rtMainBefore,(unsigned long long)rtMainAfter,renamePool.size(),double(mb)/1048576.0);}
  fprintf(stderr,"FUXI MTL: rt shadow enabled=%d reuse=%d shadows=%zu created=%llu in=%llu in_skipped=%llu reused=%llu out=%llu syncs=%llu copy_src_redirects=%llu eligible_uses=%llu linear_large_uses=%llu sync_compiles=%llu sync_compile_ms=%.1f\n",
   int(systemTranslationPolicy().rtShadow),int(systemTranslationPolicy().rtReuse),rtShadows.size(),(unsigned long long)rtShadowCreated,(unsigned long long)rtShadowIn,(unsigned long long)rtShadowInSkipped,(unsigned long long)rtShadowReused,(unsigned long long)rtShadowOut,(unsigned long long)rtShadowSyncs,(unsigned long long)rtShadowRedirects,(unsigned long long)rtEligibleSeen,(unsigned long long)rtLinearLargeSeen,(unsigned long long)syncCompiles.load(),double(syncCompileNs.load())/1e6);
 }
 // One compact line describing the op stream of a submit: P(passes: WxH tiling#id d=draws), C(copy src->dst), U(upload), M(mips), B(buffer op). L=linear O=optimal S=rt-shadow.
 void logOpSequence(const std::vector<Operation> &ops,const char *tag) {
  auto tl=[](Texture *t){return t->rtTarget?'S':t->tiling==VK_IMAGE_TILING_LINEAR?'L':'O';};
  auto id=[](Texture *t){return unsigned((uintptr_t(t)>>4)&0xfff);};
  std::string line;char tmp[200];size_t i=0;std::string last;unsigned repeat=0;
  auto emit=[&](const std::string &tok){if(tok==last){++repeat;return;}if(repeat>1){snprintf(tmp,sizeof tmp,"x%u",repeat);line+=tmp;}if(!last.empty())line+=' ';line+=tok;last=tok;repeat=1;};
  while(i<ops.size() && line.size()<3500){const auto &op=ops[i];
   if(op.texture && (op.pipeline || op.clearOnly)){size_t j=i,draws=0;
    while(j<ops.size() && ops[j].texture==op.texture && (ops[j].pipeline || ops[j].clearOnly) && (j==i || (op.encoderID && ops[j].encoderID==op.encoderID))){if(ops[j].pipeline)++draws;++j;}
    snprintf(tmp,sizeof tmp,"P(%ux%u %c#%03x d=%zu load=%u)",op.texture->width,op.texture->height,tl(op.texture),id(op.texture),draws,op.load);emit(tmp);i=j;continue;}
   if(op.texture && op.sourceTexture && !op.upload){snprintf(tmp,sizeof tmp,"C(%ux%u %c#%03x -> %ux%u %c#%03x rect=%ux%u)",op.sourceTexture->width,op.sourceTexture->height,tl(op.sourceTexture),id(op.sourceTexture),op.texture->width,op.texture->height,tl(op.texture),id(op.texture),op.width?op.width:op.texture->levelWidth(op.dstLevel),op.height?op.height:op.texture->levelHeight(op.dstLevel));emit(tmp);}
   else if(op.texture && op.upload){snprintf(tmp,sizeof tmp,"U(%ux%u %c)",op.width?op.width:op.texture->levelWidth(op.dstLevel),op.height?op.height:op.texture->levelHeight(op.dstLevel),tl(op.texture));emit(tmp);}
   else if(op.texture && op.generateMips)emit("M");
   else if(op.texture && op.dst){snprintf(tmp,sizeof tmp,"R(%ux%u %c)",op.texture->width,op.texture->height,tl(op.texture));emit(tmp);}
   else emit("B");
   ++i;}
  if(repeat>1){snprintf(tmp,sizeof tmp,"x%u",repeat);line+=tmp;}
  fprintf(stderr,"FUXI MTL: op sequence%s ops=%zu %s%s\n",tag,ops.size(),line.c_str(),i<ops.size()?" ...":"");
 }
 // FUXI_MTL_RT_SHADOW=1. A large LINEAR color target (an IOSurface-backed swap buffer) renders into a persistent OPTIMAL shadow image so the GPU can tile/compress it.
 // Per submit, in op order: the first render use copies linear->shadow unless the pass fully overwrites it; any non-render use of the linear image, or of anything else
 // on its backing buffer, first copies shadow->linear (a sync); whatever is still newer in the shadow at the end is copied back last. The ops stay ordinary ops.
 void rtShadowRewrite(std::vector<Operation> &ops) {
  ++rtSubmits;const bool reuse=systemTranslationPolicy().rtReuse;
  auto eligible=[&](Texture *t){return t && !t->shadow && !t->rtTarget && t->tiling==VK_IMAGE_TILING_LINEAR && (t->usage&VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT) && t->textureType==2 && t->levels==1 && uint64_t(t->width)*t->height>=(1u<<20);};
  // A shadow flagged clean (linear == shadow after an earlier submit) stays clean only while nothing else writes the linear memory: drop the flag on any such write.
  if(reuse)for(auto &kv:rtShadows){auto &re=kv.second;if(!re.clean)continue;const void *key=kv.first;
   auto aliasT=[&](Texture *t){return t && (rtKey(t)==key);};auto aliasB=[&](Buffer *b){return b && b->shadowKey()==key;};bool written=false;
   for(const auto &op:ops){const bool render=op.pipeline || op.clearOnly;
    if(render){for(unsigned i=0;i<std::min(op.colorCount,8u);++i){auto *t=op.color(i).texture;if(aliasT(t) && !eligible(t))written=true;}}
    else if(aliasT(op.texture) && (op.upload || op.sourceTexture || op.generateMips))written=true;
    if(aliasB(op.dst))written=true;
    for(unsigned s=0;s<2;++s)for(unsigned k=0;k<resourceSlots;++k)if(aliasB(op.buffers[s][k].buffer))written=true;
    if(written)break;}
   if(written)re.clean=false;}
  bool any=false;for(const auto &op:ops){if(!(op.pipeline || op.clearOnly))continue;for(unsigned i=0;i<std::min(op.colorCount,8u);++i){auto *t=op.color(i).texture;
   if(t && t->tiling==VK_IMAGE_TILING_LINEAR && uint64_t(t->width)*t->height>=(1u<<20)){++rtLinearLargeSeen;if(currentFrame)++currentFrame->rtLinearLarge;}
   if(eligible(t)){any=true;++rtEligibleSeen;if(currentFrame)++currentFrame->rtEligible;}}}
  if(!any)return;
  struct Entry {Texture *l,*s;bool newer,valid;const void *key;};std::vector<Entry> entries;entries.reserve(4);
  auto entryFor=[&](Texture *l)->int{
   for(size_t i=0;i<entries.size();++i)if(entries[i].l==l)return int(i);
   const void *key=rtKey(l);for(auto &e:entries)if(e.key==key)return -1; // a second texture object over the same memory in one submit stays on the direct path
   const uint32_t sampled=(l->usage&VK_IMAGE_USAGE_SAMPLED_BIT)?1u:0u;
   auto it=rtShadows.find(key);
   if(it!=rtShadows.end() && (it->second.w!=l->width || it->second.h!=l->height || it->second.format!=l->format || it->second.sampled!=sampled || it->second.offset!=l->backingOffset || it->second.row!=l->backingRow)){destroyTextureUnlocked(it->second.s);rtShadows.erase(it);it=rtShadows.end();}
   if(it==rtShadows.end()){if(rtRefused.count(key))return -1;
    while(!rtShadows.empty() && (rtShadows.size()>=8 || rtShadowBytes()+uint64_t(l->width)*l->height*texelBytes(l->format)>shadowBytesCap)){auto victim=rtShadows.begin();for(auto v=rtShadows.begin();v!=rtShadows.end();++v)if(v->second.used<victim->second.used)victim=v;destroyTextureUnlocked(victim->second.s);rtShadows.erase(victim);}
    try {OptionalScope optionalScope(*this);auto *s=allocateTextureUnlocked(l->format,sampled|4u,l->width,l->height);s->rtTarget=true;
     it=rtShadows.emplace(key,RtEntry{s,l->width,l->height,l->format,sampled,l->backingOffset,l->backingRow,0,false}).first;++rtShadowCreated;
     fprintf(stderr,"FUXI MTL: render target shadow created %ux%u format=%u (optimal, %zu bytes) key=%03x total_created=%llu live=%zu\n",l->width,l->height,l->format,s->allocated,unsigned((uintptr_t(key)>>4)&0xfff),(unsigned long long)rtShadowCreated,rtShadows.size());}
    catch(const std::exception &e){rtRefused.insert(key);fprintf(stderr,"FUXI MTL: render target shadow refused: %s\n",e.what());return -1;}}
   it->second.used=rtSubmits;
   entries.push_back({l,it->second.s,false,reuse && it->second.clean,key});return int(entries.size()-1);};
  std::vector<Operation> out;out.reserve(ops.size()+8);
  auto copyOp=[](Texture *dst,Texture *src){Operation c{};c.texture=dst;c.sourceTexture=src;return c;};
  auto bytesOf=[&](Texture *t){return uint64_t(t->width)*t->height*texelBytes(t->format);};
  auto syncOut=[&](Entry &e,bool final){if(!e.newer)return;out.push_back(copyOp(e.l,e.s));e.newer=false;e.valid=true;++rtShadowOut;if(!final)++rtShadowSyncs;
   if(currentFrame){++currentFrame->rtOut;if(!final)++currentFrame->rtSyncs;currentFrame->rtBytes+=bytesOf(e.l);}};
  for(const auto &original:ops){
   Operation op=original;const bool render=op.pipeline || op.clearOnly;
   // Anything aliasing a shadowed image's memory other than its own target/sample role needs the linear bytes current first.
   for(auto &e:entries){
    Buffer *buf=e.l->backingBuffer;auto aliases=[&](Texture *t){return t && (t==e.l || (t->backingBuffer && t->backingBuffer==buf));};
    bool hit=false,write=false;
    if(render){
     for(unsigned i=0;i<std::min(op.colorCount,8u);++i){auto *t=op.color(i).texture;if(aliases(t) && t!=e.l){hit=write=true;}}
     for(auto *t:op.samples)if(aliases(t) && t!=e.l)hit=true;
     if(op.sourceTexture && aliases(op.sourceTexture) && op.sourceTexture!=e.l)hit=true;
    }else{
     if(aliases(op.texture)){hit=true;if(op.upload || op.sourceTexture || op.generateMips)write=true;}
     // A plain image copy that reads the linear target itself reads the up-to-date shadow instead: no sync-out, the copy engine does not care about tiling.
     if(aliases(op.sourceTexture)){if(op.sourceTexture==e.l && e.newer && op.texture && !aliases(op.texture) && !op.upload && !op.generateMips && !op.src && !op.dst){op.sourceTexture=e.s;++rtShadowRedirects;}else hit=true;}
    }
    if(buf){if(op.src==buf)hit=true;if(op.dst==buf)hit=write=true;if(op.indices==buf)hit=true;
     for(unsigned s=0;s<2;++s)for(unsigned k=0;k<resourceSlots;++k)if(op.buffers[s][k].buffer==buf)hit=write=true;}
    if(hit){syncOut(e,false);if(write)e.valid=false;}
   }
   if(render){
    for(unsigned i=0;i<std::min(op.colorCount,8u);++i){auto c=op.color(i);if(!eligible(c.texture))continue;int ei=entryFor(c.texture);if(ei<0)continue;auto &e=entries[ei];
     if(!e.newer){bool full=(!op.areaWidth || op.areaWidth==c.texture->width) && (!op.areaHeight || op.areaHeight==c.texture->height);
      if(!e.valid && (c.load==1 || !full)){out.push_back(copyOp(e.s,e.l));++rtShadowIn;if(currentFrame){++currentFrame->rtIn;currentFrame->rtBytes+=bytesOf(e.l);}}
      else if(!e.valid){++rtShadowInSkipped;if(currentFrame)++currentFrame->rtInSkipped;}
      else if(c.load==1 || !full){++rtShadowReused;}
      e.newer=true;e.valid=false;}
     c.texture=e.s;op.setColor(i,c);}
    for(auto &e:entries)if(e.newer){for(auto &s:op.samples)if(s==e.l)s=e.s;if(op.sourceTexture==e.l)op.sourceTexture=e.s;}
   }else for(auto &e:entries){ // a non-render use of the linear image itself (copy, blit, readback, mip) was synced above; writes made the shadow stale
    if(op.texture==e.l && (op.upload || op.sourceTexture || op.generateMips))e.valid=false;}
   out.push_back(std::move(op));
  }
  for(auto &e:entries){syncOut(e,true);auto it=rtShadows.find(e.key);if(it!=rtShadows.end())it->second.clean=reuse && e.valid;}
  for(auto it=rtShadows.begin();it!=rtShadows.end();){if(it->second.used+900<rtSubmits){destroyTextureUnlocked(it->second.s);it=rtShadows.erase(it);}else ++it;}
  ops.swap(out);
 }

 // Why an op is not a reorderable plain draw (nullptr when it is one). Also used to explain blocked hoists.
 // relaxed (RT_RENAME): metal2vulkan lowers every Metal buffer argument to a storage buffer and fb-fetch passes only depend on their own targets, so a pass is a barrier only when its shader may write storage.
 static const char *barrierWhy(const Operation &op,bool relaxed=false) {
  if(!op.texture)return op.src||op.dst?"copy":"no-target";if(!op.pipeline)return "no-pipeline";if(op.clearOnly)return "clear-only";if(op.upload)return "upload";if(op.generateMips)return "mips";
  if(op.src||op.dst)return "copy";if(!op.encoderID)return "no-encoder";
  if(relaxed)return op.pipeline->writesStorage?"storage-write":nullptr;
  if(op.pipeline->fetchMask)return "framebuffer-fetch";
  for(const auto &d:op.pipeline->descriptors)if(d.type==VK_DESCRIPTOR_TYPE_STORAGE_BUFFER||d.type==VK_DESCRIPTOR_TYPE_STORAGE_IMAGE||d.type==VK_DESCRIPTOR_TYPE_STORAGE_TEXEL_BUFFER)return "storage-resource";else if(d.type==VK_DESCRIPTOR_TYPE_INPUT_ATTACHMENT)return "input-attachment";
  return nullptr;
 }
 // Which dependency makes a and b unorderable (mirrors groupsConflict), nullptr when independent.
 static const char *conflictWhy(const PassGroup &a,const PassGroup &b) {
  auto writes=[](const PassGroup &w,const PassGroup &r,const char *tt,const char *ts,const char *tb)->const char * {
   for(auto *t:w.targets){if(!t)continue;
    for(auto *u:r.targets)if(u && textureTouch(t,u))return tt;
    for(auto *s:r.samples){if(textureTouch(t,s))return ts;if(s->shadow && s->backingBuffer && bufferTouchesTexture(s->backingBuffer,t))return ts;}
    for(auto *x:r.buffers)if(bufferTouchesTexture(x,t))return tb;}
   return nullptr;};
  if(auto *r=writes(a,b,"target-vs-target","earlier-writes-what-later-samples","earlier-writes-memory-of-later-buffer"))return r;
  return writes(b,a,"target-vs-target","later-writes-what-earlier-samples","later-writes-memory-of-earlier-buffer");
 }
 bool buildGroups(const std::vector<Operation> &ops,std::vector<PassGroup> &groups,bool relaxed=false) {
  auto plainDraw=[relaxed](const Operation &op){return barrierWhy(op,relaxed)==nullptr;};
  for(size_t i=0;i<ops.size();++i){const auto &op=ops[i];PassGroup g{i,i,!plainDraw(op),op.colorCount,op.colorMask,{},{},{},{}};
   if(!g.barrier){if(op.colorCount>8)return false;for(unsigned k=0;k<op.colorCount;++k){auto c=op.color(k);g.targets[k]=c.texture;g.levels[k]=c.level;}}
   if(!g.barrier && !groups.empty() && groups.back().sameTargets(g)){groups.back().last=i;}else groups.push_back(std::move(g));
   if(groups.back().barrier)continue;auto &cur=groups.back();
   auto addTexture=[&](Texture *t){if(t && std::find(cur.samples.begin(),cur.samples.end(),t)==cur.samples.end())cur.samples.push_back(t);};
   auto addBuffer=[&](Buffer *b){if(b && std::find(cur.buffers.begin(),cur.buffers.end(),b)==cur.buffers.end())cur.buffers.push_back(b);};
   for(auto *s:op.samples)addTexture(s);addTexture(op.sourceTexture);addBuffer(op.indices);
   for(unsigned stage=0;stage<2;++stage)for(unsigned k=0;k<resourceSlots;++k)addBuffer(op.buffers[stage][k].buffer);
  }
  return true;
 }
 // FUXI_MTL_RT_RENAME=1. A small private texture that several passes in one submit fully overwrite in turn (load clear/dont-care, whole area) keeps one real image only for its
 // last version; every earlier version is renamed onto a pooled transient image. That removes the write-after-read hazards that pinned the passes between two main-target passes,
 // so hoistOffscreen can move them ahead and the main-target passes become adjacent (and merge). Dependencies are still judged by groupsConflict, so a pass that samples or
 // copies from the main target never moves above its writer.
 struct RenameImage {Texture *t;uint32_t w,h,format,usage;uint64_t used;};
 std::vector<RenameImage> renamePool;uint64_t rtRenamed=0,rtHoisted=0,rtMergedMain=0,rtMainBefore=0,rtMainAfter=0,rtRenameSubmits=0;
 bool smallOffscreen(Texture *t) {return t && t->tiling==VK_IMAGE_TILING_OPTIMAL && !t->imported && !t->backingBuffer && !t->shadow && !t->rtTarget && !t->mapped && !t->borrowedMemory && t->textureType==2 && t->levels==1 && t->depth==1 && !(t->usage&VK_IMAGE_USAGE_STORAGE_BIT) && uint64_t(t->width)*t->height<=(2u<<20);}
 static constexpr uint64_t renamePoolCap=20u<<20,shadowBytesCap=48u<<20;
 uint64_t renamePoolBytes() const {uint64_t n=0;for(auto &e:renamePool)n+=uint64_t(e.w)*e.h*texelBytes(e.format);return n;}
 uint64_t rtShadowBytes() const {uint64_t n=0;for(auto &e:rtShadows)n+=uint64_t(e.second.w)*e.second.h*texelBytes(e.second.format);return n;}
 void expireRenamePool() {for(size_t i=0;i<renamePool.size();){auto &e=renamePool[i];if(e.t->references==1 && rtRenameSubmits-e.used>300){destroyTextureUnlocked(e.t);renamePool.erase(renamePool.begin()+i);}else ++i;}} // idle for 300 submits
 Texture *renameAcquire(Texture *like,std::vector<Texture *> &held) {
  uint32_t usage=(like->usage&VK_IMAGE_USAGE_SAMPLED_BIT?1u:0u)|(like->usage&VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT?4u:0u);if(!usage)return nullptr;
  for(auto &e:renamePool)if(e.t->references==1 && e.used!=rtRenameSubmits && e.w==like->width && e.h==like->height && e.format==like->format && e.usage==usage){e.used=rtRenameSubmits;++e.t->references;held.push_back(e.t);return e.t;}
  if(renamePool.size()>=32)return nullptr;
  if(renamePoolBytes()+uint64_t(like->width)*like->height*texelBytes(like->format)>renamePoolCap)return nullptr; // bounded pool: bigger passes still hoist, they just are not renamed
  try {OptionalScope optionalScope(*this);auto *t=allocateTextureUnlocked(like->format,usage,like->width,like->height);renamePool.push_back({t,like->width,like->height,like->format,usage,rtRenameSubmits});++t->references;held.push_back(t);return t;}catch(const std::exception &) {return nullptr;}
 }
 void releaseRenamePool() {for(auto &e:renamePool)destroyTextureUnlocked(e.t);renamePool.clear();}
 size_t countMainRuns(const std::vector<Operation> &ops) {
  size_t runs=0;Texture *last=nullptr;
  for(const auto &op:ops){if(!(op.texture && (op.pipeline || op.clearOnly))){last=nullptr;continue;}
   if(smallOffscreen(op.texture)){last=nullptr;continue;} // an offscreen pass between two main passes splits them
   if(op.texture!=last)++runs;last=op.texture;}
  return runs;
 }
 void renameOffscreen(std::vector<Operation> &ops,std::vector<Texture *> &held) {
  expireRenamePool();std::vector<Texture *> candidates;
  for(const auto &op:ops){if(!(op.pipeline || op.clearOnly))continue;for(unsigned i=0;i<std::min(op.colorCount,8u);++i){auto *t=op.color(i).texture;if(smallOffscreen(t) && std::find(candidates.begin(),candidates.end(),t)==candidates.end())candidates.push_back(t);}}
  auto refers=[](const Operation &op,Texture *t){if(op.texture==t || op.sourceTexture==t)return true;for(auto *s:op.samples)if(s==t)return true;for(unsigned i=0;i<std::min(op.colorCount,8u);++i)if(op.color(i).texture==t)return true;return false;};
  auto replace=[](Operation &op,Texture *from,Texture *to){if(op.texture==from)op.texture=to;if(op.sourceTexture==from)op.sourceTexture=to;for(auto &s:op.samples)if(s==from)s=to;
   for(unsigned i=0;i<std::min(op.colorCount,8u);++i){auto c=op.color(i);if(c.texture==from){c.texture=to;op.setColor(i,c);}}};
  for(auto *t:candidates){
   std::vector<size_t> starts;bool bad=false;
   for(size_t n=0;n<ops.size() && !bad;++n){const auto &op=ops[n];if(!(op.pipeline || op.clearOnly))continue;bool begins=false;
    for(unsigned i=0;i<std::min(op.colorCount,8u);++i){auto c=op.color(i);if(c.texture!=t)continue;if(c.level)bad=true;
     const bool full=(!op.areaWidth || op.areaWidth==t->width) && (!op.areaHeight || op.areaHeight==t->height);if(c.load!=1 && full)begins=true;}
    if(begins){for(auto *s:op.samples)if(s==t)bad=true;if(op.sourceTexture==t)bad=true;starts.push_back(n);}}
   if(bad || starts.size()<2)continue;
   for(size_t k=0;k+1<starts.size();++k){auto *tmp=renameAcquire(t,held);if(!tmp)break;
    for(size_t n=starts[k];n<starts[k+1];++n)if(refers(ops[n],t))replace(ops[n],t,tmp);
    ++rtRenamed;}
  }
 }
 struct SampleShadow {Texture *t;uint64_t used;};std::map<std::array<uint64_t,7>,SampleShadow> sampleShadows;static constexpr size_t sampleShadowEntryCap=48;static constexpr uint64_t sampleShadowByteCap=16u<<20;
 uint64_t sampleShadowLiveBytes() const {uint64_t n=0;for(auto &e:sampleShadows)n+=uint64_t(e.second.t->width)*e.second.t->height*texelBytes(e.second.t->format);return n;}
 bool evictIdleSampleShadow(uint64_t idle) {auto best=sampleShadows.end();for(auto i=sampleShadows.begin();i!=sampleShadows.end();++i)if(i->second.t->references==1 && submits-i->second.used>=idle && (best==sampleShadows.end() || i->second.used<best->second.used))best=i;if(best==sampleShadows.end())return false;auto *t=best->second.t;sampleShadows.erase(best);destroyTextureUnlocked(t);++sampleShadowEvicted;return true;}
 void expireSampleShadows(uint64_t idle) {if(sampleShadows.empty())return;for(;;){bool any=false;for(auto &e:sampleShadows)if(e.second.t->references==1 && submits-e.second.used>=idle*5){any=true;break;}if(!any || !evictIdleSampleShadow(idle*5))return;}}
 void releaseSampleShadows() {auto all=std::move(sampleShadows);sampleShadows.clear();for(auto &e:all)destroyTextureUnlocked(e.second.t);}
 std::atomic<uint64_t> sampleShadowReused{0},sampleShadowCapped{0},sampleShadowEvicted{0},sampleShadowCreated{0},sampleShadowRefused{0},sampleShadowBytes{0},sampleHits{0},sampleCopies{0},sampleInvalidations{0},sampleHashedBytes{0},sampleCopiedBytes{0},sampleHashNs{0};unsigned hoistLogged=0;std::atomic<uint64_t> relaxedPipelines{0},relaxedMarks{0};
 void hoistOffscreen(std::vector<Operation> &ops) {
  std::vector<PassGroup> groups;if(!buildGroups(ops,groups,true))return;
  std::vector<size_t> order;order.reserve(groups.size());unsigned moved=0,offs=0;std::map<std::string,unsigned> reasons;std::map<std::string,std::string> examples;
  struct HoistLog {Backend &b;std::vector<PassGroup> &groups;unsigned &offs;unsigned &moved;std::map<std::string,unsigned> &reasons;std::map<std::string,std::string> &examples;
   ~HoistLog(){if(!(b.hoistLogged<20 || b.rtRenameSubmits%300==1))return;++b.hoistLogged;std::string line;for(auto &r:reasons){line+=" "+r.first+"="+std::to_string(r.second);auto e=examples.find(r.first);if(e!=examples.end())line+="["+e->second+"]";}
    fprintf(stderr,"FUXI MTL: rt hoist n=%u groups=%zu offscreen=%u moved=%u reasons:%s\n",b.hoistLogged,groups.size(),offs,moved,line.c_str());}} hoistLog{*this,groups,offs,moved,reasons,examples};
  for(size_t gi=0;gi<groups.size();++gi){auto &g=groups[gi];size_t insertAt=order.size();
   if(g.barrier)reasons[std::string("barrier:")+barrierWhy(ops[g.first],true)]++;
   bool offscreen=!g.barrier;if(offscreen)for(auto *t:g.targets)if(t && !smallOffscreen(t)){offscreen=false;reasons["main-or-large-group"]++;break;}
   if(offscreen){++offs;size_t scanned=0;const char *why=nullptr;bool stoppedAtEnd=false;const PassGroup *blocker=nullptr;
    for(size_t k=order.size();k-->0 && scanned<256;++scanned){auto &o=groups[order[k]];if(o.barrier){why=barrierWhy(ops[o.first],true);blocker=&o;break;}if(auto *c=conflictWhy(o,g)){why=c;blocker=&o;break;}insertAt=k;}
    if(!why)stoppedAtEnd=true;
    if(insertAt==order.size()){const char *key=why?why:"nothing-before";reasons[key]++;if(blocker&&examples.find(key)==examples.end()&&g.targets[0])examples[key]=(blocker->targets[0]?std::to_string(blocker->targets[0]->width)+"x"+std::to_string(blocker->targets[0]->height):std::string("barrier-op"))+"->"+std::to_string(g.targets[0]->width)+"x"+std::to_string(g.targets[0]->height);}
    else if(stoppedAtEnd)reasons["moved-to-start"]++;else reasons["moved-partly"]++;}
   if(insertAt!=order.size())++moved;
   order.insert(order.begin()+insertAt,gi);}
  if(!moved)return;
  std::vector<Operation> sorted;sorted.reserve(ops.size());
  for(auto gi:order)for(size_t i=groups[gi].first;i<=groups[gi].last;++i)sorted.push_back(ops[i]);
  ops.swap(sorted);rtHoisted+=moved;
 }
 void reorderPasses(std::vector<Operation> &ops) {
  std::vector<PassGroup> groups;if(!buildGroups(ops,groups,systemTranslationPolicy().rtRename))return;
  // Groups merged above may hold a barrier-free duplicate signature elsewhere; hop each plain group back to the last equal-target group.
  std::vector<size_t> order;order.reserve(groups.size());unsigned hops=0;
  for(size_t gi=0;gi<groups.size();++gi){auto &g=groups[gi];size_t insertAt=order.size();
   if(!g.barrier){size_t scanned=0;
    for(size_t k=order.size();k-->0 && scanned<64;++scanned){auto &o=groups[order[k]];
     if(o.barrier)break;
     if(o.sameTargets(g)){insertAt=k+1;break;}
     if(groupsConflict(o,g))break;}
   }
   if(insertAt!=order.size())hops+=unsigned(order.size()-insertAt);
   order.insert(order.begin()+insertAt,gi);
  }
  if(!hops)return;
  std::vector<Operation> sorted;sorted.reserve(ops.size());
  for(auto gi:order)for(size_t i=groups[gi].first;i<=groups[gi].last;++i)sorted.push_back(ops[i]);
  ops.swap(sorted);if(currentFrame)currentFrame->passReorders+=hops;
 }
 void submit(std::vector<Operation> ops,const std::function<void()> &scheduled={},std::function<void()> *retirement=nullptr,const std::function<void()> &prepare={},const std::vector<PrepareWrite> *prepareWrites=nullptr) {
  struct InSubmit {bool &f;explicit InSubmit(bool &x):f(x){f=true;}~InSubmit(){f=false;}} inSubmitScope(inSubmit);
  PhaseTimer submitPhase(SubPhase::other);
  auto submitStart=std::chrono::steady_clock::now();auto submitQueued=submitStart;
  auto since=[](std::chrono::steady_clock::time_point t){return (uint64_t)std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now()-t).count();};
  auto *timing=systemTranslationPolicy().frameTrace?currentFrame:nullptr;
  std::unique_lock<std::mutex> guard(mutex,std::defer_lock);{PhaseTimer phase(SubPhase::wait);guard.lock();}
  if(currentFrame)currentFrame->backendLock+=since(submitStart);
  requireLive();
  const bool sequenceLog=systemTranslationPolicy().passTimes && (opSequenceSubmits++%300)==1;
  struct HoldRelease {Backend &b;std::vector<Texture *> held;~HoldRelease(){for(auto *t:held)b.destroyTextureUnlocked(t);}} renameHold{*this,{}};
  const bool rtRenaming=systemTranslationPolicy().rtRename && ops.size()>2;size_t mainBefore=0;
  if(rtRenaming){if(sequenceLog)logOpSequence(ops," pre");++rtRenameSubmits;mainBefore=countMainRuns(ops);renameOffscreen(ops,renameHold.held);hoistOffscreen(ops);}
  if(systemTranslationPolicy().reorder && systemTranslationPolicy().merge && ops.size()>2)reorderPasses(ops);
  if(rtRenaming){size_t after=countMainRuns(ops);rtMainBefore+=mainBefore;rtMainAfter+=after;if(mainBefore>after)rtMergedMain+=mainBefore-after;}
  if(systemTranslationPolicy().rtShadow)rtShadowRewrite(ops);
  if(sequenceLog)logOpSequence(ops,"");
  std::vector<HostUse> hostUses,writes,uses;
  auto addUse=[](std::vector<HostUse> &list,VkDeviceMemory memory,const void *address,size_t bytes,uint8_t kind=1,Buffer *buffer=nullptr){for(auto &old:list)if(old.memory==memory && old.address==address && old.bytes==bytes){old.kind|=kind;return;}list.push_back({memory,address,bytes,kind,buffer});};
  auto bufferUse=[&](std::vector<HostUse> &list,Buffer *b,uint8_t kind=1){if(b)addUse(list,b->memory,b->contents,b->length,kind,b);};
  auto textureUse=[&](std::vector<HostUse> &list,Texture *t){if(t){addUse(list,t->memory,t->mapped,t->allocated);bufferUse(list,t->backingBuffer);}};
  const bool renaming=systemTranslationPolicy().shadowRename;
  auto collectUses=[&]{ // uses: every host-mapped allocation, coherent too, for prepare conflicts
   PhaseTimer phase(SubPhase::uses);
   uses.clear();hostUses.clear();
   auto buffer=[&](Buffer *b,uint8_t kind=1){if(b){bufferUse(uses,b,kind);if(!coherent(b->memoryType))bufferUse(hostUses,b,kind);}};
   auto direct=[&](Buffer *b,uint8_t kind=1){if(b)b->directUse=true;buffer(b,kind);};
   auto texture=[&](Texture *t){if(t){if(t->mapped){uint8_t kind=t->imported?8:4;addUse(uses,t->memory,t->mapped,t->allocated,kind);if(!coherent(t->memoryType))addUse(hostUses,t->memory,t->mapped,t->allocated,kind);}buffer(t->backingBuffer,2);}};
   for(const auto &op:ops){direct(op.src);direct(op.dst);direct(op.indices,16);texture(op.texture);texture(op.sourceTexture);for(auto *t:op.samples)texture(t);for(unsigned i=0;i<std::min(op.colorCount,8u);++i)texture(op.color(i).texture);for(auto &stage:op.buffers)for(auto binding:stage)direct(binding.buffer);if(op.pipeline)for(auto *blob:op.pipeline->constantBlobs)direct(blob);}
  };
  if(retirement || prepare || systemTranslationPolicy().overlap)collectUses();
  auto aliases=[](HostUse a,HostUse b){return (a.memory && a.memory==b.memory) || hostRangesOverlap(a.address,a.bytes,b.address,b.bytes);};
  auto conflicts=[&]{for(auto &pending:slots)if(pending.busy)for(auto old:pending.hostUses)for(auto use:hostUses)if(aliases(old,use))return true;return false;};
  // A declared prepare waits only for flights using the memory it writes; undeclared drains.
  std::vector<HostUse> prepared;const bool declared=prepare && prepareWrites;
  auto collectPrepared=[&]{PhaseTimer phase(SubPhase::uses);prepared.clear();if(declared)for(auto w:*prepareWrites)if(w.buffer && w.bytes)prepared.push_back({w.buffer->memory,(const uint8_t *)w.buffer->contents+w.offset,w.bytes,1,w.buffer});};
  collectPrepared();
  // A shadow backing buffer only the GPU reads (never bound directly) can be renamed to a spare storage instead of waiting for the flights reading it.
  auto renameable=[&](Buffer *b){return renaming && b && b->shadowBacking && !b->directUse && !b->imported && !b->upload && !b->spareFailed && b->contents;};
  auto busyUse=[&](HostUse use){for(auto &pending:slots)if(pending.busy)for(auto old:pending.uses)if(aliases(old,use))return true;return false;};
  auto spareBusy=[&](Buffer *b){return b->spare && busyUse({b->spare->memory,b->spare->contents,b->spare->length});};
  auto prepareConflicts=[&]{for(auto &pending:slots)if(pending.busy)for(auto old:pending.uses)for(auto write:prepared)if(aliases(old,write)){if(renameable(write.buffer) && write.buffer->spare && !spareBusy(write.buffer))continue;return true;}return false;};
  auto conflictKinds=[&]{unsigned m=0;for(auto &pending:slots)if(pending.busy){for(auto old:pending.uses)for(auto write:prepared)if(aliases(old,write))m|=old.kind;for(auto old:pending.hostUses)for(auto use:hostUses)if(aliases(old,use))m|=old.kind;}return m;};
  unsigned declinedSeen=0;if(currentFrame)for(auto &w:prepared)if(busyUse(w)){ // why a conflicting write cannot be renamed, once per submit and cause
   auto *x=w.buffer;unsigned c=!renaming?0:x->aliased?3:!x->shadowBacking?1:x->directUse?2:x->spareFailed?4:5;if(c==5 && renameable(x))continue;if(!(declinedSeen>>c&1)){declinedSeen|=1u<<c;++currentFrame->renameDeclined[c];}}
  if(renaming)for(auto &w:prepared)if(renameable(w.buffer) && !w.buffer->spare && busyUse(w)){ // first conflict: create the spare storage
   try{w.buffer->spare=allocateUnlocked(w.buffer->length);}catch(const std::exception &){w.buffer->spareFailed=true;}}
  auto indexHazard=[&](bool uploadOnly=false){for(auto &op:ops)if(op.indices && (!uploadOnly || op.indices->upload))for(auto &pending:slots)if(pending.busy)for(auto old:pending.writes)if(aliases(old,{op.indices->memory,op.indices->contents,op.indices->length}))return true;return false;};
  // Coherent index reads need only exclude real GPU writers. Noncoherent cache
  // maintenance still excludes every shared allocation; undeclared prepare drains.
  unsigned kindSeen=0;bool sawDrain=false,sawIndex=false,sawUpload=false,sawPrepare=false,sawUnknown=false,sawPrepConflict=false,sawDrainWait=false,sawConflict=false,sawSlots=false,otherIndices=false;
  if(currentFrame){bool upload=false;for(auto &op:ops)if(op.indices)(op.indices->upload?upload:otherIndices)=true;currentFrame->indexUploadSubmits+=upload;}
  bool blockFull=false,blockDrain=false,blockConflict=false;auto slotStart=FrameClock::now();auto waitPhase=std::make_unique<PhaseTimer>(SubPhase::wait);
  submitWake.wait(guard,[&]{if(poisoned)return true;bool index=indexHazard(),unknown=prepare && !declared,prepConflict=declared && prepareConflicts(),drain=unknown || index,busy=slots[0].busy || slots[1].busy,conflict=conflicts() || prepConflict,full=slots[0].busy && (!retirement || slots[1].busy);
   if(currentFrame){auto once=[](bool value,bool &seen,uint64_t &count){if(value && !seen){++count;seen=true;}};
    once(drain,sawDrain,currentFrame->drainSubmits);once(index,sawIndex,currentFrame->indexGPUWritableDrains);once(indexHazard(true),sawUpload,currentFrame->indexUploadDrains);once(bool(prepare),sawPrepare,currentFrame->prepareDrains);once(unknown,sawUnknown,currentFrame->prepareUnknownDrains);once(prepConflict,sawPrepConflict,currentFrame->prepareConflictWaits);
    once(drain && busy,sawDrainWait,currentFrame->drainWaits);once(conflict,sawConflict,currentFrame->conflictWaits);once(full,sawSlots,currentFrame->slotWaits);
    if(conflict || index){unsigned m=conflictKinds()|(index?16u:0u);for(unsigned k=0;k<5;++k)if((m>>k&1) && !(kindSeen>>k&1)){kindSeen|=1u<<k;++currentFrame->conflictKinds[k];}}}
   bool ready=!full && (!drain || !busy) && !conflict;if(!ready){blockFull|=full;blockDrain|=drain && busy;blockConflict|=conflict;}return ready;
  });
  waitPhase.reset();++submitEntered;if(blockFull || blockDrain || blockConflict){++submitBlocked;submitBlockedFull+=blockFull;submitBlockedDrain+=blockDrain;submitBlockedConflict+=blockConflict;submitSlotWaitNanos+=uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(FrameClock::now()-slotStart).count());}
  if(currentFrame){currentFrame->indexGPUWritableSubmits+=sawIndex;currentFrame->indexReadOnlySubmits+=otherIndices && !sawIndex;}
  if(currentFrame)currentFrame->slotWait+=since(slotStart);
  requireLive();
  if(renaming && declared){std::vector<Buffer *> renamed;
   for(auto &w:prepared)if(renameable(w.buffer) && w.buffer->spare && busyUse(w) && std::find(renamed.begin(),renamed.end(),w.buffer)==renamed.end()){
    auto *b=w.buffer,*sp=b->spare;if(spareBusy(b))continue;
    memcpy(sp->contents,b->contents,b->length);
    std::swap(b->buffer,sp->buffer);std::swap(b->memory,sp->memory);std::swap(b->contents,sp->contents);std::swap(b->allocated,sp->allocated);std::swap(b->charged,sp->charged);std::swap(b->memoryBytes,sp->memoryBytes);std::swap(b->memoryType,sp->memoryType);
    renamed.push_back(b);if(currentFrame){++currentFrame->shadowRenames;currentFrame->shadowRenameBytes+=b->length;}}
   if(!renamed.empty()){collectUses();collectPrepared();}}
  auto &slot=slots[slots[0].busy?1:0];
  auto &commandPool=slot.commandPool;auto &command=slot.command;auto &submitFence=slot.submitFence;auto &queryPool=slot.queryPool;
  auto &descriptorPool=slot.descriptorPool;auto &descriptorSets=slot.descriptorSets;auto &descriptorCapacity=slot.descriptorCapacity;auto &aliasScratch=slot.aliasScratch;
#ifdef FUXI_MTL_HOST_TEST
  imageTransitions.clear();sampleViewUses.clear();
#endif
  auto locked=currentFrame?FrameClock::now():FrameClock::time_point{};if(currentFrame){auto ns=uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(locked-submitStart).count());currentFrame->leaseLock+=ns;currentFrame->leaseBack+=ns;}
  if(prepare){{PhaseTimer phase(SubPhase::prepare);prepare();}if(currentFrame)locked=FrameClock::now();}
  auto *cpuTiming=systemTranslationPolicy().frameCPU && timing && !timing->cpuSampledSubmits?timing:nullptr;
  if(cpuTiming)++cpuTiming->cpuSampledSubmits;
  else if(timing && systemTranslationPolicy().frameCPU)++timing->cpuOmittedSubmits;
  auto backendCPU=cpuTiming?frameThreadCPU():UINT64_MAX;
  CacheActivity activity{cacheMode==CacheMode::persistent?&cacheActivity:nullptr};
  ArtifactCache::Access artifactAccess(artifactCache);
  Translator::Scope cacheScope(artifactCallbacks,artifactCache.available()?&artifactAccess:nullptr,ArtifactCache::read,ArtifactCache::write,ArtifactCache::outcome,artifactCallbacksVerified,ArtifactCache::readVerified);
  ops.erase(std::remove_if(ops.begin(),ops.end(),[](const Operation &op){
   bool missing=op.pipeline?(!op.pipeline->translation || !op.pipeline->pipeline || (op.topology==Topology::TriangleStrip && !op.pipeline->stripPipeline)):op.renderDraw;
   if(missing)dropPipelineDraw();return missing;
  }),ops.end());
  ops.erase(std::remove_if(ops.begin(),ops.end(),[](const Operation &op){
   return op.pipeline && primitiveBounds(op.topology,op.first,op.vertices) &&
    !primitiveCount(op.topology,op.vertices);
  }),ops.end());
  // Empty completion participates in the same lock/order, but creates no BO or GPU fence.
  if(ops.empty()){if(cpuTiming)cpuTiming->backendCPU+=frameCPUDelta(backendCPU,frameThreadCPU());if(currentFrame)currentFrame->backendSubmit+=frameNanos(locked);++emptySubmits;if(scheduled)scheduled();return;}
  bool submitted=false,completed=false;std::vector<VkImageView> sampleViews;std::vector<VkFramebuffer> frames;std::vector<VkRenderPass> emptyRenders;std::vector<Texture *> textures;
  struct TextureState {Texture *texture;VkImageLayout layout;std::array<VkImageLayout,32> levelLayouts;bool defined;uint32_t definedLevels;uint64_t shadowHash;bool shadowHashed;};std::vector<TextureState> oldTextures;
  size_t diagnosticOp=0;Buffer *neutralBuffer=nullptr,*aliasBuffer=nullptr;size_t aliasBytes=0;
  std::vector<bool> aliasCopies(ops.size(),false);
  auto cleanup=[&] {if(aliasBuffer!=aliasScratch)destroyUnlocked(aliasBuffer);destroyUnlocked(neutralBuffer);for(auto v:sampleViews)DestroyImageView(device,v,nullptr);for(auto frame:frames)DestroyFramebuffer(device,frame,nullptr);for(auto render:emptyRenders)DestroyRenderPass(device,render,nullptr);};
  // Exclusive prepare phases: object API/cache time is subtracted from recording.
  uint64_t objectNanos=0,descriptorNanos=0;
  struct Measure {
   uint64_t &total;FrameClock::time_point start=FrameClock::now();PhaseTimer phase;
   Measure(uint64_t &t,SubPhase p):total(t),phase(p){}
   ~Measure(){total+=frameNanos(start);}
  };
  try {
   std::vector<DrawOutput> draws(ops.size());
#ifdef FUXI_MTL_HOST_TEST
   if(captureDrawPlans)drawPlansForTest.clear();
#endif
   std::vector<std::array<MipRange,resourceSlots>> accessed(ops.size());
   std::vector<std::array<bool,resourceSlots>> generalSamples(ops.size());
   std::vector<Buffer *> buffers;
   std::unordered_set<Buffer *> bufferSeen;std::unordered_set<Texture *> textureSeen;bufferSeen.reserve(ops.size()*2+8);textureSeen.reserve(ops.size()*2+8);
   auto addBuffer=[&](Buffer *b){if(b){scanAdd(ScanKind::useProbes,1);if(bufferSeen.insert(b).second)buffers.push_back(b);}};
   auto addTexture=[&](Texture *t){if(t){scanAdd(ScanKind::useProbes,1);if(textureSeen.insert(t).second)textures.push_back(t);}if(t && t->backingBuffer)addBuffer(t->backingBuffer);};
   for(size_t n=0;n<ops.size();++n){PhaseTimer planPhase(SubPhase::plan);diagnosticOp=n;auto &op=ops[n];addBuffer(op.src);addBuffer(op.dst);addBuffer(op.indices);addTexture(op.texture);addTexture(op.sourceTexture);
    if(op.texture && op.texture->shadow && (op.pipeline || op.clearOnly || op.upload || op.sourceTexture || op.generateMips))throw std::runtime_error("read-only linear shadow GPU write refused");
    if(op.texture && op.dstLevel>=op.texture->levels)throw std::runtime_error("destination mip level");
    if(op.texture && op.srcLevel>=(op.sourceTexture?op.sourceTexture:op.texture)->levels)throw std::runtime_error("source mip level");
    if(op.generateMips){if(!op.texture || op.texture->textureType==7 || !op.mipCount || op.mipBase>=op.texture->levels || op.mipCount>op.texture->levels-op.mipBase || !op.texture->canGenerate)throw std::runtime_error("mipmap generation range/format");continue;}
    if(!op.pipeline && op.texture && !op.clearOnly){
     auto *src=op.sourceTexture?op.sourceTexture:op.texture;auto *dst=op.texture;
     uint32_t sw=src->levelWidth(op.srcLevel),sh=src->levelHeight(op.srcLevel),dw=dst->levelWidth(op.dstLevel),dh=dst->levelHeight(op.dstLevel);
     uint32_t sd=src->levelDepth(op.srcLevel),dd=dst->levelDepth(op.dstLevel);
     size_t width=op.width?op.width:(op.upload||op.sourceTexture?dw:sw),height=op.height?op.height:(op.upload||op.sourceTexture?dh:sh),depth=op.depth?op.depth:(op.upload||op.sourceTexture?dd:sd),pixel=texelBytes(dst->format);
     size_t row=op.rowBytes?op.rowBytes:width*pixel;
     if(!width || !height || row<width*pixel || row%pixel || row>maxBufferLength/height ||
        ((op.upload || op.sourceTexture) && (op.dstX>dw || width>dw-op.dstX || op.dstY>dh || height>dh-op.dstY)) ||
        (!op.upload && (op.srcX>sw || width>sw-op.srcX || op.srcY>sh || height>sh-op.srcY)))throw std::runtime_error("texture transfer region/stride bounds");
     // Depth slices exist only on 3D images; 2D transfers keep depth 1 and z 0.
     if(!depth || (op.sourceTexture && src->textureType!=dst->textureType) || ((op.upload || op.sourceTexture) && (op.dstZ>dd || depth>dd-op.dstZ)) || (!op.upload && (op.srcZ>sd || depth>sd-op.srcZ)) ||
        (dst->textureType!=7 && (depth!=1 || op.dstZ || op.srcZ)) || (op.imageBytes && (dst->textureType!=7 || !op.rowBytes || op.imageBytes>maxBufferLength || op.imageBytes%row || op.imageBytes/row<height)))throw std::runtime_error("texture transfer depth/image stride");
     size_t imageStride=op.imageBytes?op.imageBytes:row*height;
     size_t bytes=(depth-1)*imageStride+(height-1)*row+width*pixel;
     if(op.sourceTexture){
      if(src->format!=dst->format)throw std::runtime_error("texture copy format mismatch");
      // Snapshot all same-image/memory copies, including disjoint rectangles and
      // view-relative mips. Never issue an overlapping vkCmdCopyImage.
      aliasCopies[n]=src==dst || memoryAlias(src,dst) || hostRangesOverlap(src->mapped,src->allocated,dst->mapped,dst->allocated);
      if(aliasCopies[n])aliasBytes=std::max(aliasBytes,width*height*depth*pixel);
     }
     auto *transferBuffer=op.upload?op.src:op.sourceTexture?nullptr:op.dst;
     if(transferBuffer && (transferBuffer->memory==dst->memory || hostRangesOverlap(transferBuffer->contents,transferBuffer->length,dst->mapped,dst->allocated)))throw std::runtime_error("image/buffer transfer alias");
     if(op.upload && (!op.src || op.from%4 || op.from%pixel || op.from>op.src->length || bytes>op.src->length-op.from))throw std::runtime_error("upload bounds/alignment");
     if(!op.upload && !op.sourceTexture && (!op.dst || op.to%4 || op.to%pixel || op.to>op.dst->length || bytes>op.dst->length-op.to))throw std::runtime_error("readback bounds/alignment");
    }
    if(op.pipeline || op.clearOnly){
     if(!op.colorCount || op.colorCount>maxColorAttachments || !op.colorMask || (op.colorMask&~((1u<<op.colorCount)-1)) || (op.pipeline && op.colorCount!=op.pipeline->options.colorCount))throw std::runtime_error("draw/pass color count mismatch");
     for(unsigned i=0;i<op.colorCount;++i){auto c=op.color(i);auto *t=c.texture;
      if(!t){if(op.pipeline && op.pipeline->options.color(i).format)throw std::runtime_error("missing render attachment");continue;}addTexture(t);
      if(!t || c.level>=t->levels || c.load>2 || c.store>1 || (op.pipeline && t->format!=op.pipeline->options.color(i).format))throw std::runtime_error("attachment["+std::to_string(i)+"] target/level/format/actions"+(t?" format="+std::to_string(t->format)+" level="+std::to_string(c.level)+" load="+std::to_string(c.load)+" store="+std::to_string(c.store)+(op.pipeline?" pipeline="+std::to_string(op.pipeline->options.color(i).format):std::string()):std::string(" null target")));
      if(op.areaWidth>t->levelWidth(c.level) || op.areaHeight>t->levelHeight(c.level))throw std::runtime_error("render area exceeds attachment");
      if(t->levelWidth(c.level)!=op.texture->levelWidth(op.dstLevel) || t->levelHeight(c.level)!=op.texture->levelHeight(op.dstLevel))throw std::runtime_error("attachment size mismatch");
      if(orderedColorAccess && op.pipeline && !(t->usage&VK_IMAGE_USAGE_INPUT_ATTACHMENT_BIT))throw std::runtime_error("ordered target lacks INPUT_ATTACHMENT");
      if(op.pipeline && (op.pipeline->fetchMask&(1u<<i)) && c.load==0)throw std::runtime_error("fetch from DontCare attachment");
      if(!(t->usage&VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT) || t->textureType!=2)throw std::runtime_error("texture is not a 2D render attachment");
      if(op.pipeline && op.pipeline->options.color(i).blend && !t->blendable)throw std::runtime_error("attachment blending unavailable");
      for(unsigned j=0;j<i;++j){auto other=op.color(j);if(!other.texture)continue;if(memoryAlias(t,other.texture) || hostRangesOverlap(t->mapped,t->allocated,other.texture->mapped,other.texture->allocated))throw std::runtime_error("render attachment alias");}
      for(unsigned stage=0;stage<2;++stage)for(auto binding:op.buffers[stage])if(binding.buffer && (binding.buffer->memory==t->memory || hostRangesOverlap(binding.buffer->contents,binding.buffer->length,t->mapped,t->allocated)))throw std::runtime_error("attachment/buffer feedback");
     }
    }
    if(op.clearOnly && (!op.texture || op.pipeline || op.load>2 || op.store>1))throw std::runtime_error("invalid empty render pass");
    if(!op.pipeline)continue;
    if(!primitiveBounds(op.topology,op.first,op.vertices))throw std::runtime_error("invalid primitive count/start");
    op.vertices=primitiveCount(op.topology,op.vertices);
    if(!op.vertices)throw std::runtime_error("empty line draw must be omitted");
    if(!op.texture || op.load>2 || op.store>1)throw std::runtime_error("invalid bounded draw target/actions");
    const auto &v=op.viewport;const auto &r=op.scissor;
    if(!viewportFits(v.x,v.y+v.height,v.width,-v.height,v.minDepth,v.maxDepth,op.texture->levelWidth(op.dstLevel),op.texture->levelHeight(op.dstLevel)) || r.offset.x<0 || r.offset.y<0 ||
       op.areaWidth>op.texture->levelWidth(op.dstLevel) || op.areaHeight>op.texture->levelHeight(op.dstLevel) ||
       !scissorFits(r.offset.x,r.offset.y,r.extent.width,r.extent.height,op.areaWidth?op.areaWidth:op.texture->levelWidth(op.dstLevel),op.areaHeight?op.areaHeight:op.texture->levelHeight(op.dstLevel)))throw std::runtime_error("viewport/scissor target bounds");
    if(op.indices && !shadowIndexBytes(op.indices->length,op.indexOffset,op.vertices,op.indexWidth))throw std::runtime_error("index buffer bounds/type");
    DrawInput input{};input.count=op.vertices;input.start=op.first;input.strip=primitiveRestart(op.topology);input.indexed=op.indices!=nullptr;input.indexLength=op.indices?op.indices->length:0;input.indexOffset=op.indexOffset;
    for(unsigned stage=0;stage<2;++stage)for(unsigned i=0;i<resourceSlots;++i){auto b=op.buffers[stage][i];if(b.buffer){if(b.buffer->memory==op.texture->memory || hostRangesOverlap(b.buffer->contents,b.buffer->length,op.texture->mapped,op.texture->allocated))throw std::runtime_error("attachment/buffer feedback");addBuffer(b.buffer);if(b.length()>b.buffer->length || b.offset>b.length())throw std::runtime_error("binding slice bounds");input.buffers[stage][i]={b.length(),b.offset,b.bytes?1u:0u};}}
    for(unsigned i=0;i<resourceSlots;++i){auto *t=i==0 && !op.samples[i]?op.sourceTexture:op.samples[i];auto *sam=i==0 && !op.samplers[i]?op.sampler:op.samplers[i];
     if(t){if(op.pipeline->textureTypes[i]!=UINT32_MAX && op.pipeline->textureTypes[i]!=t->textureType)throw std::runtime_error("sampled texture dimension mismatch or old bridge adapter");
      uint32_t base=op.sampleBase[i],count=op.sampleCount[i]?op.sampleCount[i]:t->levels-base;
      if(base>=t->levels || !count || count>t->levels-base)throw std::runtime_error("sample mip range");
      addTexture(t);input.formats[i]=t->format;}
     if(t && sam && !t->filterable && (sam->filters&(2u|4u|16u)))throw std::runtime_error("texture format linear filtering unavailable");
     if(sam){input.filters[i]=sam->filters;input.lodMin[i]=sam->lodMin;input.lodMax[i]=sam->lodMax;}
    }
    FrameStageTimer pipelineTimer(timing,&FrameSample::pipe);
    auto *requestedPipeline=op.pipeline;bool cacheDrawPlan=!systemTranslationPolicy().trace;
#ifdef FUXI_MTL_HOST_TEST
    cacheDrawPlan=cacheDrawPlan && reuseDrawPlans;
#endif
    bool planHit=cacheDrawPlan && drawPlans.find(requestedPipeline,input,op.pipeline,draws[n]);
    if(timing){if(planHit)++timing->planHits;else ++timing->planMisses;}
    if(!planHit){
     char why[512]{};DrawRebase rebase{};auto &translator=Translator::get();
     Translator::OptionsScope translationScope(translator);
     auto *original=op.pipeline;std::optional<VariantKey> key;
     auto findVariant=[&]{if(!key)key.emplace(input);return std::find_if(original->variants.begin(),original->variants.end(),[&](const auto &v){return v.first==*key;});};
     // The packed key is deliberately coarse: a candidate must still pass the
     // translator's complete draw validation. On rejection retry the baseline,
     // which can accept a different length/offset/specialization of this shape.
     bool ready=false;
     if(!systemTranslationPolicy().trace && !original->variants.empty()){
      auto found=findVariant();
      if(found!=original->variants.end() && translator.planDraw(found->second->translation,&input,&draws[n],&rebase,why,sizeof(why))){op.pipeline=found->second;ready=true;}
     }
     if(!ready && !translator.planDraw(original->translation,&input,&draws[n],&rebase,why,sizeof(why))){
      if(strcmp(why,"draw requires runtime variant") && strcmp(why,"draw requires different specialization"))throw std::runtime_error(why);
      auto found=findVariant();
      if(found!=original->variants.end())op.pipeline=found->second;
      else {if(original->variants.size()>=8)throw std::runtime_error("runtime variant bound");Translation variant{};
       CompileBoostScope compileBoost("variant");
       if(!translator.variant(original->translation,&input,&variant,why,sizeof(why)))throw std::runtime_error(why);
       auto *pipeline=createPipeline(variant,original->options);try{original->variants.push_back({*key,pipeline});}catch(...){destroyPipelineUnlocked(pipeline);throw;}original->retainedBytes+=pipeline->retainedBytes;drawPlans.invalidate(original);objects.reweigh(original,original->retainedBytes,[&](Pipeline *old){drawPlans.invalidate(old);destroyPipelineUnlocked(old);});op.pipeline=pipeline;
      }
      if(!translator.planDraw(op.pipeline->translation,&input,&draws[n],&rebase,why,sizeof(why)))throw std::runtime_error(why);
     }
     if(cacheDrawPlan)drawPlans.insert(requestedPipeline,input,op.pipeline,draws[n],rebase);
    }
    pipelineTimer.stop();
#ifdef FUXI_MTL_HOST_TEST
    if(captureDrawPlans){drawPlansForTest.emplace_back(op.pipeline,DrawOutput{});memcpy(&drawPlansForTest.back().second,&draws[n],sizeof(DrawOutput));}
#endif
    // Analyze the final runtime variant and this draw's descriptors, not the
    // baseline shader or the order in which Metal bindings were set.
    auto &plan=draws[n];bool skipDraw=false;
    for(unsigned slot=0;slot<resourceSlots;++slot){auto *sample=op.samples[slot];if(!slot && !sample)sample=op.sourceTexture;if(!sample)continue;
     auto base=op.sampleBase[slot],count=op.sampleCount[slot]?op.sampleCount[slot]:sample->levels-base;
     bool aliases=false,viewOverlap=false;for(unsigned k=0;k<op.colorCount;++k){auto *target=op.color(k).texture;if(target && (sample==target || sample->image==target->image || memoryAlias(sample,target) || hostRangesOverlap(sample->mapped,sample->allocated,target->mapped,target->allocated))){aliases=true;auto l=op.color(k).level;if(l>=base && l-base<count)viewOverlap=true;}}
     auto &range=accessed[n][slot];
     // Nonalias draws retain their established conservative definition checks.
     if(!aliases){range.add(base,base+count-1);continue;}
     bool unsafe=false;
     for(unsigned stage=0;stage<2;++stage){auto stages=stage?16u:1u;
      auto sampler=[&](uint32_t binding)->MipSampler {
       for(unsigned j=0;j<plan.descriptorCount;++j){auto &d=plan.descriptors[j];if(d.binding!=binding || !(d.stages&stages))continue;
        if(d.sampler==UINT32_MAX){if(binding>=16)return {};auto b=op.pipeline->bakedSamplerInfo[binding];return {bool(b.enabled),b.mip?2u:1u,b.minLod,b.maxLod};}
        auto index=d.sampler;
        if(index>=resourceSlots)return {};auto *s=index==0 && !op.samplers[0]?op.sampler:op.samplers[index];
        if(s)return {true,(s->filters>>3)&3,s->lodMin,s->lodMax};
       }return {};
      };
      auto read=[&](uint32_t binding,uint64_t offset,uint32_t &bits,bool push){
       if(push){for(unsigned j=0;j<plan.pushCount;++j){auto &p=plan.pushes[j];if((p.stages&stages) && offset>=p.offset && offset-p.offset<=p.size && p.size-(offset-p.offset)>=4){memcpy(&bits,p.bytes+offset-p.offset,4);return true;}}return false;}
       for(unsigned j=0;j<plan.descriptorCount;++j){auto &d=plan.descriptors[j];if(d.binding!=binding || !(d.stages&stages) || d.type!=VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER || offset>d.range || d.range-offset<4)continue;
        if(d.source==2){bits=0;return true;}
        Buffer *b=nullptr;if(d.source==1){if(d.slot<op.pipeline->constantBlobs.size())b=op.pipeline->constantBlobs[d.slot];}
        else if(d.slot<resourceSlots)b=op.buffers[stage][d.slot].buffer;
        if(!b || d.offset>b->length || offset>b->length-d.offset || b->length-d.offset-offset<4)return false;
        for(size_t previous=0;previous<n;++previous)if(auto *written=ops[previous].dst)if(written->memory==b->memory || hostRangesOverlap(written->contents,written->length,b->contents,b->length))return false;
        // A preceding storage-buffer shader may have changed these bytes.
        // Descriptor reflection does not prove read-only storage accesses.
        for(size_t previous=0;previous<n;++previous)if(ops[previous].pipeline)for(unsigned q=0;q<draws[previous].descriptorCount;++q){auto &written=draws[previous].descriptors[q];
         if(written.source || written.type!=VK_DESCRIPTOR_TYPE_STORAGE_BUFFER || written.slot>=resourceSlots)continue;
         for(unsigned ws=0;ws<2;++ws)if(written.stages&(ws?16u:1u))if(auto *other=ops[previous].buffers[ws][written.slot].buffer)if(other->memory==b->memory || hostRangesOverlap(other->contents,other->length,b->contents,b->length))return false;
        }
        // An earlier in-flight storage write could make host uniform bytes stale.
        if(slots[0].busy || slots[1].busy)return false;
        memcpy(&bits,(uint8_t *)b->contents+d.offset+offset,4);return true;
       }return false;
      };
      for(unsigned j=0;j<plan.descriptorCount;++j){auto &d=plan.descriptors[j];if(d.source || d.slot!=slot || !(d.stages&stages) || (d.type!=VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE && d.type!=VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER))continue;
       auto relative=op.pipeline->mipAccess[stage].range(d.binding,count,sampler,read);
       if(!relative.empty())range.add(base+relative.first,base+relative.last);
      }
     }
     bool sameImage=false,alias=false;
     for(unsigned k=0;k<op.colorCount;++k){auto c=op.color(k);auto *target=c.texture;if(!target)continue;
      if(sample==target || sample->image==target->image){if(range.contains(c.level)){unsafe=true;sameImage=true;}}
      else if(memoryAlias(sample,target) || hostRangesOverlap(sample->mapped,sample->allocated,target->mapped,target->allocated)){unsafe=true;alias=true;}
     }
     if(unsafe){
      auto reason="attachmentFeedback slot="+std::to_string(slot)+" view="+std::to_string(base)+"+"+std::to_string(count)+" possibleRead="+(range.empty()?std::string("none"):std::to_string(range.first)+".."+std::to_string(range.last));
      if(sameImage && !alias && allowFeedbackDraw(reason.c_str())){generalSamples[n][slot]=true;continue;}
      if(skipSamplingDraw(reason.c_str())){skipDraw=true;break;}throw std::runtime_error("sampling reject "+reason);
     }
     // Keep the original Metal view and relative LOD/query semantics. GENERAL
     // covers its complete descriptor range, including the disjoint attachment.
     generalSamples[n][slot]=viewOverlap;
    }
    if(skipDraw){
     // Omit the shader draw, but preserve Metal's pass load/store actions so
     // later draws in this encoder can still load the attachment normally.
     op.pipeline=nullptr;op.renderDraw=false;op.clearOnly=true;op.sourceTexture=nullptr;
     std::fill(std::begin(op.samples),std::end(op.samples),nullptr);draws[n]={};
     generalSamples[n]={};continue;
    }
    // A descriptor layout covers its entire view, even mips it does not read.
    // Promote other views of this image too so descriptors cannot disagree.
    for(unsigned i=0;i<resourceSlots;++i)if(generalSamples[n][i]){
     auto *image=op.samples[i];if(!i && !image)image=op.sourceTexture;
     for(unsigned j=0;j<resourceSlots;++j){auto *other=op.samples[j];if(!j && !other)other=op.sourceTexture;if(other && other->image==image->image)generalSamples[n][j]=true;}
    }
    topologyPipeline(op.pipeline,op.topology);
    for(auto *blob:op.pipeline->constantBlobs)addBuffer(blob);
    // CPU bounds check the fixed-function vertex fetches, including every index.
    uint32_t last=op.first+op.vertices-1;
    if(op.indices){last=0;for(unsigned i=0;i<op.vertices;++i){uint32_t index=0;memcpy(&index,(uint8_t *)op.indices->contents+op.indexOffset+uint64_t(op.indexWidth)*i,op.indexWidth);if(primitiveRestart(op.topology) && index==(op.indexWidth==2?UINT16_MAX:UINT32_MAX))continue;last=std::max(last,uint32_t(index));}}
    for(auto &a:op.pipeline->options.attributes)if(a.format){auto slot=a.buffer;
     if(slot>=4 || !op.buffers[0][slot].buffer)throw std::runtime_error("unbound vertex buffer");
     auto bound=op.buffers[0][slot];
     if(!vertexFetchFits(bound.length(),bound.offset,last,op.pipeline->options.strides[slot],a.offset,a.format))throw std::runtime_error("vertex fetch bounds");
    }
    // CPU index bounds are valid only for quiescent host input. A preceding GPU
    // write in this command needs readback before its values can be checked.
    for(size_t previous=0;previous<n;++previous)if(ops[previous].dst){
     if(ops[previous].dst==op.indices)throw std::runtime_error("GPU-written index input requires separate submission");
     for(auto &stage:op.buffers)for(auto &bound:stage)if(bound.buffer==ops[previous].dst)throw std::runtime_error("GPU-written draw buffer requires separate submission");
    }
   }
   std::optional<OccPlan> occPlan;
   if(systemTranslationPolicy().occlude){occPlan.emplace();try{occludeAnalyze(ops,draws,*occPlan);}catch(const std::exception &e){occFail(std::string("analysis:")+e.what());occPlan.reset();}}
   // Plan only after runtime variants, access proofs and skipped draws are final.
   // Every admitted shader writes attachments only. Storage buffers require
   // a NonWritable contract in every referencing stage; unknown writers split.
   bool boundaryPerf=systemTranslationPolicy().perf&4;
   std::vector<size_t> mergeEnds;std::vector<VkExtent2D> mergeAreas;
   if(systemTranslationPolicy().merge){
    mergeEnds.resize(ops.size());if(boundaryPerf)mergeAreas.resize(ops.size());
    auto area=[&](size_t n){auto &op=ops[n];return VkExtent2D{op.areaWidth?op.areaWidth:op.texture->levelWidth(op.dstLevel),op.areaHeight?op.areaHeight:op.texture->levelHeight(op.dstLevel)};};
    size_t first=0;VkExtent2D runArea{};
    auto isolated=[&](size_t n)->MergeSplit {
     auto &op=ops[n];if(!op.texture || !op.pipeline || op.clearOnly || op.upload || op.generateMips || op.src || op.dst)return MergeSplit::nonDraw;
     if(op.pipeline->fetchMask && !orderedColorAccess)return MergeSplit::fetch;
     if(op.indices)for(unsigned i=0;i<op.colorCount;++i){auto *t=op.color(i).texture;auto *b=op.indices;
      if(t && (b->memory==t->memory || hostRangesOverlap(b->contents,b->length,t->mapped,t->allocated)))return MergeSplit::bufferAlias;
     }
     for(unsigned j=0;j<draws[n].descriptorCount;++j){auto &d=draws[n].descriptors[j];auto type=d.type;
      if(type==VK_DESCRIPTOR_TYPE_INPUT_ATTACHMENT && !orderedColorAccess)return MergeSplit::fetch;
      if(type==VK_DESCRIPTOR_TYPE_STORAGE_IMAGE || type==VK_DESCRIPTOR_TYPE_STORAGE_TEXEL_BUFFER)return MergeSplit::storage;
      if(type==VK_DESCRIPTOR_TYPE_STORAGE_BUFFER)for(unsigned stage=0;stage<2;++stage)
       if((d.stages&(stage?16u:1u)) && (d.binding>=16 || !(op.pipeline->readOnlyStorage[stage]&(1u<<d.binding))))return MergeSplit::storage;
     }
     for(unsigned slot=0;slot<resourceSlots;++slot){auto *sample=op.samples[slot];if(!slot && !sample)sample=op.sourceTexture;if(!sample)continue;
      for(unsigned i=0;i<op.colorCount;++i){auto *t=op.color(i).texture;if(t && (sample==t || sample->image==t->image || memoryAlias(sample,t) || hostRangesOverlap(sample->mapped,sample->allocated,t->mapped,t->allocated)))return MergeSplit::attachmentSample;}
     }
     return MergeSplit::none;
    };
    auto boundary=[&](size_t a,size_t b)->MergeSplit {
     auto &x=ops[a];auto &y=ops[b];auto reason=isolated(a);if(reason!=MergeSplit::none)return reason;
     reason=isolated(b);if(reason!=MergeSplit::none)return reason;
     if(!x.encoderID || !y.encoderID || (x.encoderID!=y.encoderID && !(systemTranslationPolicy().perf&2)))return MergeSplit::encoder;
     if(x.colorCount!=y.colorCount || x.colorMask!=y.colorMask)return MergeSplit::attachments;
     if(!boundaryPerf && (x.areaWidth!=y.areaWidth || x.areaHeight!=y.areaHeight))return MergeSplit::area;
     if(boundaryPerf){auto initial=area(first),next=area(b);VkExtent2D joined{std::max(runArea.width,next.width),std::max(runArea.height,next.height)};
      for(unsigned i=0;i<x.colorCount;++i)if(x.color(i).texture){
       if((joined.width>initial.width || joined.height>initial.height) && ops[first].color(i).load!=1)return MergeSplit::area;
       if((joined.width>next.width || joined.height>next.height) && y.color(i).store!=1)return MergeSplit::area;
      }
     }
     for(unsigned i=0;i<x.colorCount;++i){auto c=x.color(i),d=y.color(i);
      if(c.texture!=d.texture || c.level!=d.level || x.pipeline->options.color(i).format!=y.pipeline->options.color(i).format)return MergeSplit::attachments;
      // Intermediate STORE then LOAD preserves attachment contents across encoders too.
      if(c.texture && (c.store!=1 || d.load!=1))return MergeSplit::actions;
     }
     // A refresh reads backing storage, not the optimal sample image. Any
     // earlier draw in this run writes these same attachments, so only a
     // consumer on the right needs a split if its backing storage aliases
     // them. A refresh on the first draw can still move before the whole run.
     // Shader storage writers and explicit transfers are isolated above.
     for(unsigned slot=0;slot<resourceSlots;++slot){auto *sample=y.samples[slot];if(!slot && !sample)sample=y.sourceTexture;
      if(!sample || !sample->shadow)continue;auto *backing=sample->backingBuffer;
      for(unsigned i=0;i<x.colorCount;++i){auto *t=x.color(i).texture;
       if(t && backing && (backing->memory==t->memory || hostRangesOverlap(backing->contents,backing->length,t->mapped,t->allocated)))return MergeSplit::shadow;
      }
     }
     return MergeSplit::none;
    };
    for(size_t n=1;n<=ops.size();++n){if(boundaryPerf && n==first+1 && ops[first].pipeline)runArea=area(first);auto reason=n==ops.size()?MergeSplit::none:boundary(n-1,n);
     if(n<ops.size() && reason==MergeSplit::none){if(boundaryPerf){auto next=area(n);runArea={std::max(runArea.width,next.width),std::max(runArea.height,next.height)};}if(currentFrame && ops[n-1].encoderID!=ops[n].encoderID)++currentFrame->crossEncoderMerges;continue;}
     for(size_t i=first;i<n;++i){mergeEnds[i]=n-1;if(boundaryPerf)mergeAreas[i]=runArea;}
     if(currentFrame && n<ops.size() && (ops[n-1].pipeline || ops[n].pipeline))++currentFrame->mergeSplits[size_t(reason)];
     first=n;
    }
   }
   size_t neutralSize=0;for(const auto &draw:draws)for(unsigned i=0;i<draw.descriptorCount;++i){const auto &d=draw.descriptors[i];if(d.source==2){
    if((d.type!=VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER && d.type!=VK_DESCRIPTOR_TYPE_STORAGE_BUFFER) || d.offset || !d.range || d.range>maxBufferLength)throw std::runtime_error("planned neutral buffer bounds/type");
    neutralSize=std::max(neutralSize,size_t(d.range));}}
   if(aliasBytes){Measure measure{objectNanos,SubPhase::objects};
    // Single synchronous lane: prior fence is complete before scratch is reused.
    // Larger requests keep the transient path; retained memory is quota charged.
    if(systemTranslationPolicy().pipelined && aliasBytes<=aliasScratchLimit){
     if(aliasScratch && aliasScratch->length<aliasBytes){destroyUnlocked(aliasScratch);aliasScratch=nullptr;}
     if(!aliasScratch)aliasScratch=allocateUnlocked(aliasBytes);
     aliasBuffer=aliasScratch;
    }else aliasBuffer=allocateUnlocked(aliasBytes);
    addBuffer(aliasBuffer);
   }
   if(neutralSize){Measure measure{objectNanos,SubPhase::objects};neutralBuffer=allocateUnlocked(neutralSize);addBuffer(neutralBuffer);}
   for(auto *t:textures)oldTextures.push_back({t,t->layout,t->levelLayouts,t->defined,t->definedLevels,t->shadowHash,t->shadowHashed});
   auto flushStart=std::chrono::steady_clock::now();auto flushPhase=std::make_unique<PhaseTimer>(SubPhase::flush);
   // Direct shared host writes are not tracked. Only Vulkan HOST_COHERENT permits omission.
   for(auto *t:textures)if(t->mapped && !coherent(t->memoryType)) {VkMappedMemoryRange r{};r.sType=VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE;r.memory=t->memory;r.size=VK_WHOLE_SIZE;++flushCalls;check(FlushMappedMemoryRanges(device,1,&r));}
   for(auto *b:buffers)if(!coherent(b->memoryType)) {VkMappedMemoryRange r{};r.sType=VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE;r.memory=b->memory;r.size=VK_WHOLE_SIZE;++flushCalls;check(FlushMappedMemoryRanges(device,1,&r));}
   flushPhase.reset();
   {auto ns=since(flushStart);submitFlushNanos+=ns;if(currentFrame)currentFrame->flush+=ns;}
   {Measure measure{objectNanos,SubPhase::objects};
    if(!commandPool){VkCommandPoolCreateInfo pi{};pi.sType=VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO;pi.queueFamilyIndex=family;check(CreateCommandPool(device,&pi,nullptr,&commandPool));++reuseCounts.commandPools;++submitObjectCreates;++submitPoolCreates;
     VkCommandBufferAllocateInfo ai{};ai.sType=VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO;ai.commandPool=commandPool;ai.level=VK_COMMAND_BUFFER_LEVEL_PRIMARY;ai.commandBufferCount=1;
     check(AllocateCommandBuffers(device,&ai,&command));++reuseCounts.commandBuffers;++submitObjectCreates;++submitPoolCreates;
    }else check(ResetCommandPool(device,commandPool,0));
    if(!submitFence){VkFenceCreateInfo fi{};fi.sType=VK_STRUCTURE_TYPE_FENCE_CREATE_INFO;check(CreateFence(device,&fi,nullptr,&submitFence));++reuseCounts.fences;++submitObjectCreates;++submitPoolCreates;}
    else check(ResetFences(device,1,&submitFence));
    if(systemTranslationPolicy().passTimes && supportsTimestamps() && !slot.passPool){VkQueryPoolCreateInfo pi{};pi.sType=VK_STRUCTURE_TYPE_QUERY_POOL_CREATE_INFO;pi.queryType=VK_QUERY_TYPE_TIMESTAMP;pi.queryCount=passStampLimit+1;check(CreateQueryPool(device,&pi,nullptr,&slot.passPool));}
    if(systemTranslationPolicy().drawTimes && supportsTimestamps() && !slot.drawPool){VkQueryPoolCreateInfo pi{};pi.sType=VK_STRUCTURE_TYPE_QUERY_POOL_CREATE_INFO;pi.queryType=VK_QUERY_TYPE_TIMESTAMP;pi.queryCount=passStampLimit+1;check(CreateQueryPool(device,&pi,nullptr,&slot.drawPool));}
    if(supportsTimestamps() && !queryPool){VkQueryPoolCreateInfo qi{};qi.sType=VK_STRUCTURE_TYPE_QUERY_POOL_CREATE_INFO;qi.queryType=VK_QUERY_TYPE_TIMESTAMP;qi.queryCount=2;check(CreateQueryPool(device,&qi,nullptr,&queryPool));++reuseCounts.queries;++submitObjectCreates;++submitPoolCreates;}
   }
   VkCommandBuffer cmd=command;VkQueryPool timestamps=queryPool;VkFence fence=submitFence;VkQueryPool passQ=timestamps && systemTranslationPolicy().passTimes?slot.passPool:VkQueryPool{};
   VkQueryPool drawQ=timestamps && systemTranslationPolicy().drawTimes && drawSampleSerial++%30==0?slot.drawPool:VkQueryPool{};std::vector<DrawInfo> drawInfos;
   auto recordStart=std::chrono::steady_clock::now();uint64_t recordObjects=objectNanos;
   VkCommandBufferBeginInfo bi{};bi.sType=VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO;bi.flags=VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;{PhaseTimer phase(SubPhase::cmdBegin);check(BeginCommandBuffer(cmd,&bi));}
   if(timestamps){CmdResetQueryPool(cmd,timestamps,0,2);CmdWriteTimestamp(cmd,VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,timestamps,0);}
   if(passQ){CmdResetQueryPool(cmd,passQ,0,passStampLimit+1);CmdWriteTimestamp(cmd,VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT,passQ,0);}
   if(drawQ){CmdResetQueryPool(cmd,drawQ,0,passStampLimit+1);CmdWriteTimestamp(cmd,VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT,drawQ,0);}
   uint32_t stampCopies=0,stampBarriers=0;std::vector<PassInfo> passInfos;PassInfo cur{};
   auto pb=[&](auto &&...a){++stampBarriers;if(currentFrame)++currentFrame->pipelineBarriers;{PhaseTimer phase(SubPhase::barriers);CmdPipelineBarrier(cmd,a...);}};
   VkMemoryBarrier barrier{};barrier.sType=VK_STRUCTURE_TYPE_MEMORY_BARRIER;barrier.srcAccessMask=VK_ACCESS_HOST_WRITE_BIT;barrier.dstAccessMask=VK_ACCESS_MEMORY_READ_BIT|VK_ACCESS_MEMORY_WRITE_BIT;
   pb(VK_PIPELINE_STAGE_HOST_BIT,VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,0,1,&barrier,0,nullptr,0,nullptr);
   // Merged runs contain only read-only nonattachment samples, so a mip can
   // request only SHADER_READ_ONLY here, or GENERAL for a shared attachment.
   // Deduplicate these pre-pass barriers; singleton hazards keep every barrier.
   bool hoisting=false;std::unordered_map<VkImage,uint32_t> hoisted;
   // Image-barrier policy (perf bit 4): only these stages touch images, so layout barriers need not name ALL_COMMANDS.
   const VkPipelineStageFlags barrierProducers=VK_PIPELINE_STAGE_VERTEX_SHADER_BIT|VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT|VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT|VK_PIPELINE_STAGE_TRANSFER_BIT;
   std::vector<VkImageMemoryBarrier> pendingImages;std::vector<Texture *> pendingTextures;
   auto flushImages=[&]{if(pendingImages.empty())return;VkPipelineStageFlags src=VK_PIPELINE_STAGE_ALL_COMMANDS_BIT|VK_PIPELINE_STAGE_HOST_BIT,dst=VK_PIPELINE_STAGE_ALL_COMMANDS_BIT;
    if(boundaryPerf){src=barrierProducers|VK_PIPELINE_STAGE_HOST_BIT;dst=VK_PIPELINE_STAGE_TRANSFER_BIT;for(auto &ib:pendingImages)if(ib.newLayout!=VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL)dst=barrierProducers;}
    pb(src,dst,0,0,nullptr,0,nullptr,pendingImages.size(),pendingImages.data());pendingImages.clear();pendingTextures.clear();};
   // Perf bit 4: sampled images stay GENERAL (a sample-only layout buys nothing on linear/sysmem Turnip images and costs a ping-pong transition per use).
   auto sampleGeneral=[&](size_t opIndex,unsigned slot){return generalSamples[opIndex][slot] || boundaryPerf;};
   auto transition=[&](Texture *t,VkImageLayout next,uint32_t base=0,uint32_t count=0) {
    if(!count)count=t->levels-base;
    for(uint32_t l=base;l<base+count;++l){
     if(hoisting){auto &mips=hoisted[t->image];uint32_t bit=1u<<l;if(mips&bit)continue;mips|=bit;}
     auto &layout=l?t->levelLayouts[l]:t->layout;
     // GENERAL reuse relies on the entry HOST and inter-op memory dependencies.
     // Real layout changes and shadow transfer destinations retain barriers.
     if(layout==next && (((systemTranslationPolicy().perf&1) && next==VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL) || (boundaryPerf && (next==VK_IMAGE_LAYOUT_GENERAL || next==VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL)))){if(currentFrame && (next==VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL || boundaryPerf))++currentFrame->readBarriersSkipped;continue;}
     if(currentFrame)++currentFrame->imageBarriers;
     VkImageMemoryBarrier ib{};ib.sType=VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;ib.oldLayout=layout;ib.newLayout=next;
     ib.srcAccessMask=layout==VK_IMAGE_LAYOUT_UNDEFINED?0:VK_ACCESS_MEMORY_READ_BIT|VK_ACCESS_MEMORY_WRITE_BIT|VK_ACCESS_HOST_WRITE_BIT;
     ib.dstAccessMask=VK_ACCESS_MEMORY_READ_BIT|VK_ACCESS_MEMORY_WRITE_BIT;
     if(boundaryPerf){ // reads need only the execution dependency; writes become available, and only the accesses that follow are made visible
      if(layout!=VK_IMAGE_LAYOUT_UNDEFINED)ib.srcAccessMask=VK_ACCESS_SHADER_WRITE_BIT|VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT|VK_ACCESS_TRANSFER_WRITE_BIT|VK_ACCESS_HOST_WRITE_BIT;
      ib.dstAccessMask=next==VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL?VK_ACCESS_TRANSFER_WRITE_BIT:VK_ACCESS_SHADER_READ_BIT|VK_ACCESS_SHADER_WRITE_BIT|VK_ACCESS_COLOR_ATTACHMENT_READ_BIT|VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT|VK_ACCESS_TRANSFER_READ_BIT|VK_ACCESS_TRANSFER_WRITE_BIT|VK_ACCESS_INPUT_ATTACHMENT_READ_BIT;
     }
     if(currentFrame){auto kind=[](VkImageLayout l){return l==VK_IMAGE_LAYOUT_GENERAL?1u:l==VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL?2u:l==VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL?3u:0u;};++currentFrame->layoutKinds[kind(layout)*4+kind(next)];}ib.srcQueueFamilyIndex=ib.dstQueueFamilyIndex=VK_QUEUE_FAMILY_IGNORED;
     ib.image=t->image;ib.subresourceRange={VK_IMAGE_ASPECT_COLOR_BIT,l,1,0,1};
#ifdef FUXI_MTL_HOST_TEST
     imageTransitions.push_back({t,l,layout,next});
#endif
     if(boundaryPerf){
      for(size_t i=0;i<pendingTextures.size();++i){auto *old=pendingTextures[i];if(old->image==t->image?pendingImages[i].subresourceRange.baseMipLevel==l:memoryAlias(old,t) || hostRangesOverlap(old->mapped,old->allocated,t->mapped,t->allocated)){flushImages();break;}}
      pendingImages.push_back(ib);pendingTextures.push_back(t);
     }else pb(VK_PIPELINE_STAGE_ALL_COMMANDS_BIT|VK_PIPELINE_STAGE_HOST_BIT,VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,0,0,nullptr,0,nullptr,1,&ib);layout=next;
    }
   };
   // Bound each descriptor type and sets independently; grow geometrically, retain one pool.
   VkDescriptorPool submitPool{};
   {Measure measure{objectNanos,SubPhase::objects};std::vector<VkDescriptorPoolSize> sizes;uint32_t sets=0;
    for(size_t i=0;i<ops.size();++i){if(!ops[i].texture || !ops[i].pipeline || !draws[i].descriptorCount)continue;if(sets==4096)throw std::runtime_error("submit descriptor set bound");++sets;
     for(unsigned j=0;j<draws[i].descriptorCount;++j){auto ty=(VkDescriptorType)draws[i].descriptors[j].type;auto it=std::find_if(sizes.begin(),sizes.end(),[&](const VkDescriptorPoolSize &z){return z.type==ty;});if(it==sizes.end())sizes.push_back({ty,1});else {if(it->descriptorCount==65536)throw std::runtime_error("submit descriptor type bound");++it->descriptorCount;}}}
    if(sets>4096)throw std::runtime_error("submit descriptor set bound");
    bool grow=sets>descriptorSets;
    for(auto z:sizes){if(z.descriptorCount>65536)throw std::runtime_error("submit descriptor type bound");auto it=std::find_if(descriptorCapacity.begin(),descriptorCapacity.end(),[&](auto c){return c.type==z.type;});if(it==descriptorCapacity.end() || it->descriptorCount<z.descriptorCount)grow=true;}
    if(grow){
     auto capacity=descriptorCapacity;
     auto round=[](uint32_t n){uint32_t c=1;while(c<n)c*=2;return c;};
     for(auto z:sizes){auto it=std::find_if(capacity.begin(),capacity.end(),[&](auto c){return c.type==z.type;});if(it==capacity.end())capacity.push_back({z.type,round(z.descriptorCount)});else it->descriptorCount=std::max(it->descriptorCount,round(z.descriptorCount));}
     // Destroy the quiescent old pool first: avoid temporarily doubling kernel BOs.
     if(descriptorPool)DestroyDescriptorPool(device,descriptorPool,nullptr);descriptorPool={};
     VkDescriptorPoolCreateInfo pci{};pci.sType=VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO;pci.maxSets=std::max(descriptorSets,round(sets));pci.poolSizeCount=capacity.size();pci.pPoolSizes=capacity.data();
     check(CreateDescriptorPool(device,&pci,nullptr,&descriptorPool));++reuseCounts.descriptors;++submitObjectCreates;++submitPoolCreates;descriptorSets=pci.maxSets;descriptorCapacity=std::move(capacity);
    }else if(descriptorPool)check(ResetDescriptorPool(device,descriptorPool,0));
    submitPool=descriptorPool;
   }
   struct BindingValue {
    uint32_t binding{},type{};VkDescriptorImageInfo image{};VkDescriptorBufferInfo buffer{};
    bool operator==(const BindingValue &b) const {return binding==b.binding && type==b.type && image.sampler==b.image.sampler && image.imageView==b.image.imageView && image.imageLayout==b.image.imageLayout && buffer.buffer==b.buffer.buffer && buffer.offset==b.buffer.offset && buffer.range==b.buffer.range;}
   };
   struct BoundSet {VkDescriptorSetLayout layout;unsigned count;uint64_t hash;std::array<BindingValue,16> bindings;VkDescriptorSet set;};
   std::vector<BoundSet> boundSets;std::unordered_multimap<uint64_t,size_t> boundIndex;static constexpr size_t boundSetLimit=4096;if(systemTranslationPolicy().submitFast){boundSets.reserve(std::min<size_t>(ops.size(),boundSetLimit));boundIndex.reserve(std::min<size_t>(ops.size(),boundSetLimit)*2);}
   auto copyShadow=[&](Texture *t){
    ++stampCopies;VkBufferImageCopy region{};region.bufferOffset=t->backingOffset;region.bufferRowLength=t->backingRow/texelBytes(t->format);
    region.imageSubresource={VK_IMAGE_ASPECT_COLOR_BIT,0,0,1};region.imageExtent={t->width,t->height,1};
    CmdCopyBufferToImage(cmd,t->backingBuffer->buffer,t->image,VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,1,&region);t->setDefined(0);if(timing)timing->textureRepitchedBytes+=uint64_t(t->width)*t->height*texelBytes(t->format);
   };
   auto refresh=[&](Texture *t){if(!t || !t->shadow)return;transition(t,VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL);flushImages();copyShadow(t);};
   std::vector<Texture *> refreshed,refreshedSubmit,collected;
   // Perf bit 4: a shadow is copied from its backing buffer once per submit when nothing in the submit can change that backing
   // (no buffer destination, no storage-buffer/image shader, no written texture overlapping it). The CPU snapshot is applied before submit.
   bool shadowStable=boundaryPerf;
   if(boundaryPerf)for(size_t n=0;n<ops.size() && shadowStable;++n)if(ops[n].pipeline)for(unsigned j=0;j<draws[n].descriptorCount;++j){auto t=draws[n].descriptors[j].type;if(t==VK_DESCRIPTOR_TYPE_STORAGE_BUFFER || t==VK_DESCRIPTOR_TYPE_STORAGE_IMAGE || t==VK_DESCRIPTOR_TYPE_STORAGE_TEXEL_BUFFER){shadowStable=false;break;}}
   auto backingStable=[&](Texture *t){
    if(!shadowStable)return false;auto *backing=t->backingBuffer;if(!backing)return false;
    auto hits=[&](Texture *w){return w && (backing->memory==w->memory || hostRangesOverlap(backing->contents,backing->length,w->mapped,w->allocated));};
    for(auto &op:ops){
     if(op.dst && (op.dst==backing || op.dst->memory==backing->memory || hostRangesOverlap(backing->contents,backing->length,op.dst->contents,op.dst->length)))return false;
     if(op.pipeline || op.clearOnly){for(unsigned i=0;i<std::min(op.colorCount,8u);++i)if(hits(op.color(i).texture))return false;}
     else if(op.texture && (op.upload || op.sourceTexture || op.generateMips) && hits(op.texture))return false;
    }
    return true;
   };
   // Unchanged-backing skip: a linear-texture shadow is re-copied only when a hash of its backing rows differs from the hash at the last recorded copy.
   // Never when this or an earlier submit may write the backing on the GPU (sticky Buffer::gpuWritten); a failed submit restores the hash with the layouts.
   std::unordered_map<Buffer *,bool> backingWrites;std::unordered_map<Texture *,uint64_t> submitHashes;
   bool anyStorage=false;if(systemTranslationPolicy().shadowHash)for(size_t n=0;n<ops.size() && !anyStorage;++n)if(ops[n].pipeline)for(unsigned j=0;j<draws[n].descriptorCount;++j){auto t=draws[n].descriptors[j].type;if(t==VK_DESCRIPTOR_TYPE_STORAGE_BUFFER || t==VK_DESCRIPTOR_TYPE_STORAGE_IMAGE || t==VK_DESCRIPTOR_TYPE_STORAGE_TEXEL_BUFFER){anyStorage=true;break;}}
   auto writesBacking=[&](Buffer *backing){
    auto cached=backingWrites.find(backing);if(cached!=backingWrites.end())return cached->second;
    bool w=anyStorage || backing->gpuWritten;
    auto hits=[&](Texture *x){return x && (backing->memory==x->memory || hostRangesOverlap(backing->contents,backing->length,x->mapped,x->allocated));};
    for(auto &op:ops){if(w)break;
     if(op.dst && (op.dst==backing || op.dst->memory==backing->memory || hostRangesOverlap(backing->contents,backing->length,op.dst->contents,op.dst->length)))w=true;
     else if(op.pipeline || op.clearOnly){for(unsigned i=0;i<std::min(op.colorCount,8u);++i)if(hits(op.color(i).texture))w=true;}
     else if(op.texture && (op.upload || op.sourceTexture || op.generateMips) && hits(op.texture))w=true;
    }
    if(w)backing->gpuWritten=true;backingWrites[backing]=w;return w;
   };
   auto shadowUnchanged=[&](Texture *t)->bool{
    if(!systemTranslationPolicy().shadowHash || !t->shadow)return false;
    auto *backing=t->backingBuffer;size_t pixel=texelBytes(t->format),rowBytes=size_t(t->width)*pixel;
    bool eligible=backing && backing->contents && t->height && rowBytes && t->backingRow>=rowBytes && t->backingOffset<=backing->length && (size_t(t->height)-1)*t->backingRow+rowBytes<=backing->length-t->backingOffset;
    if(!eligible || writesBacking(backing)){if(t->shadowHashed)++sampleInvalidations;t->shadowHashed=false;return false;}
    uint64_t h;auto known=submitHashes.find(t);
    if(known!=submitHashes.end())h=known->second;
    else {auto start=FrameClock::now();h=hashRows((const uint8_t *)backing->contents+t->backingOffset,t->height,t->backingRow,rowBytes);submitHashes[t]=h;sampleHashNs+=uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(FrameClock::now()-start).count());sampleHashedBytes+=rowBytes*t->height;if(currentFrame)currentFrame->shadowHashNs+=frameNanos(start);}
    if(t->shadowHashed && t->shadowHash==h && t->isDefined(0)){++sampleHits;if(currentFrame){++currentFrame->shadowHashSkipped;currentFrame->shadowHashBytes+=rowBytes*t->height;}return true;}
    t->shadowHash=h;t->shadowHashed=true;++sampleCopies;sampleCopiedBytes+=rowBytes*t->height;if(currentFrame)++currentFrame->shadowHashCopied;return false;
   };
   auto refreshImages=[&](size_t opIndex){diagnosticOp=opIndex;const auto &op=ops[opIndex];
    // Once per run, after preceding writes and before any sample transitions.
    // Never deduplicate the transfer-destination transition with a read layout.
    auto readShadow=[&](Texture *t){if(!t || !t->shadow || std::find(refreshed.begin(),refreshed.end(),t)!=refreshed.end())return;
     if(shadowUnchanged(t)){refreshed.push_back(t);return;}
     if(boundaryPerf){refreshed.push_back(t);
      if(std::find(refreshedSubmit.begin(),refreshedSubmit.end(),t)!=refreshedSubmit.end() && backingStable(t)){if(currentFrame)++currentFrame->shadowRefreshSkipped;return;}
      refreshedSubmit.push_back(t);collected.push_back(t);return;}
     refresh(t);refreshed.push_back(t);};
    for(auto *sample:op.samples)readShadow(sample);readShadow(op.sourceTexture);
    if(!op.pipeline && !op.upload && !op.sourceTexture && !op.clearOnly)readShadow(op.texture);
   };
   auto prepareImages=[&](size_t opIndex){PhaseTimer phase(SubPhase::barriers);diagnosticOp=opIndex;const auto &op=ops[opIndex];
    for(unsigned slot=0;slot<resourceSlots;++slot)if(auto *t=op.samples[slot]){
     uint32_t base=op.sampleBase[slot],count=op.sampleCount[slot]?op.sampleCount[slot]:t->levels-base;
     if(base>=t->levels || !count || count>t->levels-base)throw std::runtime_error("sample mip range");
     auto range=accessed[opIndex][slot];
     for(uint32_t l=range.first;!range.empty() && l<=range.last;++l)if(!t->isDefined(l)){if(allowUndefinedRead())break;throw std::runtime_error("undefined sampled mip");}
     transition(t,sampleGeneral(opIndex,slot)?VK_IMAGE_LAYOUT_GENERAL:VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,base,count);
    }
    if(op.sourceTexture && !op.pipeline && !op.sourceTexture->isDefined(op.srcLevel))throw std::runtime_error("undefined source texture mip");
    if(op.sourceTexture && !op.pipeline)transition(op.sourceTexture,VK_IMAGE_LAYOUT_GENERAL,op.srcLevel,1);
    if(op.pipeline && op.sourceTexture && !op.samples[0]){
     auto *t=op.sourceTexture;auto range=accessed[opIndex][0];
     for(uint32_t l=range.first;!range.empty() && l<=range.last;++l)if(!t->isDefined(l)){if(allowUndefinedRead())break;throw std::runtime_error("undefined sampled mip");}
     transition(t,sampleGeneral(opIndex,0)?VK_IMAGE_LAYOUT_GENERAL:VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,op.sampleBase[0],op.sampleCount[0]);
    }
    if(op.texture && !op.pipeline && !op.clearOnly)transition(op.texture,VK_IMAGE_LAYOUT_GENERAL,op.generateMips?op.mipBase:(!op.upload && !op.sourceTexture?op.srcLevel:op.dstLevel),op.generateMips?op.mipCount:1);
    if(op.pipeline || op.clearOnly)for(unsigned i=0;i<op.colorCount;++i){auto c=op.color(i);if(c.texture)transition(c.texture,VK_IMAGE_LAYOUT_GENERAL,c.level,1);}
   };
   VkPipeline boundPipeline{};VkPipelineLayout boundLayout{};VkDescriptorSet boundSet{};
   VkViewport boundViewport{};VkRect2D boundScissor{};bool dynamicBound=false;
   for(size_t opIndex=0;opIndex<ops.size();++opIndex) {diagnosticOp=opIndex;const auto &op=ops[opIndex];const auto &draw=draws[opIndex];
    size_t passEnd=mergeEnds.empty()?opIndex:mergeEnds[opIndex];const bool culledOp=occPlan && occPlan->culled[opIndex];
    bool continuation=opIndex && !mergeEnds.empty() && mergeEnds[opIndex-1]>=opIndex;
    if(!continuation){boundPipeline={};boundLayout={};boundSet={};dynamicBound=false;refreshed.clear();for(size_t i=opIndex;i<=passEnd;++i)refreshImages(i);
     // One transfer-destination barrier for every shadow in the run, then the copies.
     if(!collected.empty()){for(auto *t:collected)transition(t,VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL);flushImages();for(auto *t:collected)copyShadow(t);collected.clear();}
     hoisting=passEnd>opIndex;hoisted.clear();for(size_t i=opIndex;i<=passEnd;++i)prepareImages(i);hoisting=false;flushImages();diagnosticOp=opIndex;}
    if(op.generateMips){auto *t=op.texture;
     if(!t->isDefined(op.mipBase))throw std::runtime_error("undefined mip generation source");
     for(uint32_t l=op.mipBase+1;l<op.mipBase+op.mipCount;++l){
      VkImageBlit region{};region.srcSubresource={VK_IMAGE_ASPECT_COLOR_BIT,l-1,0,1};region.dstSubresource={VK_IMAGE_ASPECT_COLOR_BIT,l,0,1};
      region.srcOffsets[1]={(int32_t)t->levelWidth(l-1),(int32_t)t->levelHeight(l-1),1};region.dstOffsets[1]={(int32_t)t->levelWidth(l),(int32_t)t->levelHeight(l),1};
      CmdBlitImage(cmd,t->image,VK_IMAGE_LAYOUT_GENERAL,t->image,VK_IMAGE_LAYOUT_GENERAL,1,&region,VK_FILTER_LINEAR);
      barrier.srcAccessMask=VK_ACCESS_TRANSFER_WRITE_BIT;barrier.dstAccessMask=VK_ACCESS_TRANSFER_READ_BIT|VK_ACCESS_TRANSFER_WRITE_BIT;
      pb(VK_PIPELINE_STAGE_TRANSFER_BIT,VK_PIPELINE_STAGE_TRANSFER_BIT,0,1,&barrier,0,nullptr,0,nullptr);t->setDefined(l);
     }
    }else
    if(op.texture && op.pipeline) {
     std::array<Texture *,8> targets{};std::array<uint32_t,8> levels{};VkImageView targetViews[8]{},compactViews[8]{};VkClearValue clears[8]{};unsigned used=0;
     for(unsigned i=0;i<op.colorCount;++i){auto c=op.color(i);if(!c.texture)continue;if(c.load==1 && !c.texture->isDefined(c.level))throw std::runtime_error("load undefined attachment["+std::to_string(i)+"]");
      c.texture->setDefined(c.level,c.store!=0);targets[i]=c.texture;levels[i]=c.level;targetViews[i]=attachmentView(c.texture,c.level);compactViews[used]=targetViews[i];memcpy(clears[used++].color.float32,c.clear,sizeof(c.clear));}
     bool loadElided=false;
     if(!continuation){
     std::optional<Operation> passOp;
     if(passEnd>opIndex){passOp=op;for(unsigned i=0;i<op.colorCount;++i){auto c=passOp->color(i);if(!c.texture)continue;c.store=ops[passEnd].color(i).store;passOp->setColor(i,c);}}
     if(occPlan){uint32_t aw=boundaryPerf && passEnd>opIndex?mergeAreas[opIndex].width:(op.areaWidth?op.areaWidth:op.texture->levelWidth(op.dstLevel)),ah=boundaryPerf && passEnd>opIndex?mergeAreas[opIndex].height:(op.areaHeight?op.areaHeight:op.texture->levelHeight(op.dstLevel));
      if(occElide(ops,*occPlan,opIndex,passEnd,aw,ah)){if(!passOp)passOp=op;auto c=passOp->color(0);c.load=0;passOp->setColor(0,c);loadElided=true;++occElided;}}
     if(passQ || systemTranslationPolicy().rtShadow || systemTranslationPolicy().pipeStats){auto *t=op.texture;uint8_t tl=t->rtTarget?2:t->tiling==VK_IMAGE_TILING_LINEAR?1:0;
      uint64_t key=0xcbf29ce484222325ull;for(uint64_t x:{uint64_t(t->width),uint64_t(t->height),uint64_t(t->format),uint64_t(tl),uint64_t(t->backingBuffer && t->backingBuffer->imported),uint64_t(t->usage)})key=(key^x)*0x100000001b3ull;
      if(targetLogged.insert(key).second){
       bool imported=t->backingBuffer && t->backingBuffer->imported;bool wide=t->vkFormat==VK_FORMAT_B8G8R8A8_UNORM || t->vkFormat==VK_FORMAT_R8G8B8A8_UNORM || t->vkFormat==VK_FORMAT_A2B10G10R10_UNORM_PACK32 || t->vkFormat==VK_FORMAT_R16G16B16A16_SFLOAT;
       fprintf(stderr,"FUXI MTL: render target %ux%u format=%u vk=%d tiling=%s imported=%d borrowed=%d usage=0x%x storage=%d input_attachment=%d ubwc=%s (derived from tiling/format/usage; not queried)\n",t->width,t->height,t->format,int(t->vkFormat),tl==1?"linear":tl==2?"optimal(rt-shadow)":"optimal",int(imported),int(t->borrowedMemory),unsigned(t->usage),!!(t->usage&VK_IMAGE_USAGE_STORAGE_BIT),!!(t->usage&VK_IMAGE_USAGE_INPUT_ATTACHMENT_BIT),
        tl==1?"no(linear)":(t->usage&VK_IMAGE_USAGE_STORAGE_BIT)?"no(storage)":wide?"possible":"unknown(format)");}}
     if(passQ){cur={};cur.width=op.texture->levelWidth(op.dstLevel);cur.tiling=op.texture->rtTarget?2:op.texture->tiling==VK_IMAGE_TILING_LINEAR?1:0;cur.height=op.texture->levelHeight(op.dstLevel);cur.colors=used;
      for(unsigned i=0,u=0;i<op.colorCount && u<8;++i){auto c=op.color(i);if(!c.texture)continue;unsigned st=passEnd>opIndex?ops[passEnd].color(i).store:c.store;cur.load|=uint32_t(c.load&3u)<<(u*2);cur.store|=(st?1u:0u)<<u;++u;}}
     auto render=renderPass(op.pipeline,passOp?&*passOp:&op);
     VkFramebufferCreateInfo fi{};fi.sType=VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO;fi.renderPass=render;
     fi.attachmentCount=used;fi.pAttachments=compactViews;fi.width=op.texture->levelWidth(op.dstLevel);fi.height=op.texture->levelHeight(op.dstLevel);fi.layers=1;VkFramebuffer frame{};
     {Measure measure{objectNanos,SubPhase::objects};uint64_t frameProbes=0;auto found=frameCache.end();{auto range=frameIndex.equal_range(frameKey(targets,render,levels));for(auto it=range.first;it!=range.second;++it){++frameProbes;auto &c=frameCache[it->second];if(c.textures==targets && c.render==render && c.levels==levels){found=frameCache.begin()+it->second;break;}}}
      scanAdd(ScanKind::frameProbes,frameProbes);if(found!=frameCache.end())frame=found->frame;
      else {check(CreateFramebuffer(device,&fi,nullptr,&frame));++reuseCounts.frames;++submitObjectCreates;++submitFrameCreates;
       try {if(frameCache.size()<frameCacheLimit){frameCache.push_back({targets,render,levels,frame});frameIndex.emplace(frameKey(targets,render,levels),frameCache.size()-1);}else frames.push_back(frame);}catch(...){if(!frameCache.empty() && frameCache.back().frame==frame)frameCache.pop_back();DestroyFramebuffer(device,frame,nullptr);throw;}}
     }
     VkRenderPassBeginInfo ri{};ri.sType=VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO;ri.renderPass=render;ri.framebuffer=frame;
     ri.renderArea={{0,0},boundaryPerf && passEnd>opIndex?mergeAreas[opIndex]:VkExtent2D{op.areaWidth?op.areaWidth:op.texture->levelWidth(op.dstLevel),op.areaHeight?op.areaHeight:op.texture->levelHeight(op.dstLevel)}};ri.clearValueCount=used;ri.pClearValues=clears;
     { // Render area = bounding box of the pass's scissors (a draw can never write outside its scissor). Only when every attachment LOADs:
       // a CLEAR or DONT_CARE load covers the whole attachment in Metal, and LOAD+STORE leaves untouched pixels as they were, so a smaller area is exact.
      uint64_t fullPixels=uint64_t(ri.renderArea.extent.width)*ri.renderArea.extent.height;bool shrunk=false;
      if(systemTranslationPolicy().renderArea && used){
       bool allLoad=!loadElided;for(unsigned i=0;i<op.colorCount;++i){auto c=op.color(i);if(c.texture && c.load!=1)allLoad=false;}
       uint64_t x0=UINT64_MAX,y0=UINT64_MAX,x1=0,y1=0;
       if(allLoad)for(size_t i=opIndex;i<=passEnd;++i){const auto &r=ops[i].scissor;if(!r.extent.width || !r.extent.height || r.offset.x<0 || r.offset.y<0)continue;
        x0=std::min<uint64_t>(x0,uint64_t(r.offset.x));y0=std::min<uint64_t>(y0,uint64_t(r.offset.y));x1=std::max<uint64_t>(x1,uint64_t(r.offset.x)+r.extent.width);y1=std::max<uint64_t>(y1,uint64_t(r.offset.y)+r.extent.height);}
       x1=std::min<uint64_t>(x1,ri.renderArea.extent.width);y1=std::min<uint64_t>(y1,ri.renderArea.extent.height);
       if(allLoad && x0<x1 && y0<y1 && (x1-x0)*(y1-y0)<fullPixels){ri.renderArea={{int32_t(x0),int32_t(y0)},{uint32_t(x1-x0),uint32_t(y1-y0)}};shrunk=true;}
      }
      if(currentFrame){++currentFrame->renderAreaPasses;currentFrame->renderAreaFullPixels+=fullPixels;currentFrame->renderAreaPixels+=uint64_t(ri.renderArea.extent.width)*ri.renderArea.extent.height;if(shrunk)++currentFrame->renderAreaShrunk;}
      if(passQ){cur.areaW=ri.renderArea.extent.width;cur.areaH=ri.renderArea.extent.height;}
     }
     if(currentFrame)++currentFrame->passes;{PhaseTimer phase(SubPhase::passes);CmdBeginRenderPass(cmd,&ri,VK_SUBPASS_CONTENTS_INLINE);}
     if(drawQ && drawInfos.size()<passStampLimit){DrawInfo m{};m.begin=true;m.passW=ri.renderArea.extent.width;m.passH=ri.renderArea.extent.height;drawInfos.push_back(m);CmdWriteTimestamp(cmd,VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT,drawQ,uint32_t(drawInfos.size()));}
     }else if(currentFrame)++currentFrame->mergedDraws;
     if(!culledOp){
     auto pipeline=topologyPipeline(op.pipeline,op.topology);
     if(pipeline!=boundPipeline){CmdBindPipeline(cmd,VK_PIPELINE_BIND_POINT_GRAPHICS,pipeline);boundPipeline=pipeline;}
     if(currentFrame)++currentFrame->draws;++cur.draws;
     if(!dynamicBound || memcmp(&boundViewport,&op.viewport,sizeof(op.viewport))){CmdSetViewport(cmd,0,1,&op.viewport);boundViewport=op.viewport;}
     if(!dynamicBound || memcmp(&boundScissor,&op.scissor,sizeof(op.scissor))){CmdSetScissor(cmd,0,1,&op.scissor);boundScissor=op.scissor;}
     dynamicBound=true;
     if(draw.descriptorCount) {
      VkDescriptorSetAllocateInfo sai{};sai.sType=VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO;sai.descriptorPool=submitPool;sai.descriptorSetCount=1;sai.pSetLayouts=&op.pipeline->setLayout;
      bool fast=systemTranslationPolicy().submitFast;
      std::optional<std::array<BindingValue,16>> bindings;if(fast)bindings.emplace();std::array<VkWriteDescriptorSet,16> writes;
      VkDescriptorSet set{};if(!fast){Measure measure{descriptorNanos,SubPhase::descriptors};check(AllocateDescriptorSets(device,&sai,&set));if(systemTranslationPolicy().frameTrace)++frameCounts.descriptors;}
      for(unsigned i=0;i<draw.descriptorCount;++i){const auto &d=draw.descriptors[i];
       VkWriteDescriptorSet write{};write.sType=VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;write.dstSet=set;write.dstBinding=d.binding;write.descriptorCount=1;write.descriptorType=(VkDescriptorType)d.type;
       VkDescriptorImageInfo image{};VkDescriptorBufferInfo buffer{};
       if(d.source==2){
        if(!neutralBuffer || d.offset || d.range>neutralBuffer->length)throw std::runtime_error("planned neutral buffer mismatch");
        buffer={neutralBuffer->buffer,0,d.range};write.pBufferInfo=&buffer;
       }else if(d.source==1){
        auto it=std::find_if(op.pipeline->descriptors.begin(),op.pipeline->descriptors.end(),[&](const Descriptor &layout){return layout.set==d.set && layout.binding==d.binding && layout.type==d.type && layout.stages==d.stages && layout.source==1 && layout.slot==d.slot && layout.range==d.range;});
        if(it==op.pipeline->descriptors.end() || d.slot>=op.pipeline->constantBlobs.size() || d.offset)throw std::runtime_error("planned constant blob mismatch");
        auto *b=op.pipeline->constantBlobs[d.slot];buffer={b->buffer,0,d.range};write.pBufferInfo=&buffer;
       }else if(d.type==VK_DESCRIPTOR_TYPE_INPUT_ATTACHMENT){
        if(!op.pipeline->fetchMask || !op.pipeline->orderedColorAccess || d.slot>=op.colorCount || !(op.pipeline->fetchMask&(1u<<d.slot)) || d.format!=(uint32_t)op.color(d.slot).texture->vkFormat || d.stages!=VK_SHADER_STAGE_FRAGMENT_BIT)throw std::runtime_error("planned input attachment mismatch");
        image={VK_NULL_HANDLE,targetViews[d.slot],VK_IMAGE_LAYOUT_GENERAL};write.pImageInfo=&image;
       }else if(d.type==VK_DESCRIPTOR_TYPE_SAMPLER){
        if(d.sampler>=resourceSlots)throw std::runtime_error("planned sampler slot");
        auto *sam=d.sampler==0 && !op.samplers[0]?op.sampler:op.samplers[d.sampler];if(!sam)throw std::runtime_error("planned unbound sampler");
        image.sampler=sam->sampler;write.pImageInfo=&image;
       }else if(d.type==VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER || d.type==VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE){
        if(d.slot>=resourceSlots || (d.sampler>=resourceSlots && d.sampler!=UINT32_MAX))throw std::runtime_error("planned sample slot");auto *t=d.slot==0 && !op.samples[0]?op.sourceTexture:op.samples[d.slot];auto *sam=d.sampler==UINT32_MAX?nullptr:d.sampler==0 && !op.samplers[0]?op.sampler:op.samplers[d.sampler];
        if(d.sampler==UINT32_MAX && (d.binding>=16 || !op.pipeline->bakedSamplers[d.binding]))throw std::runtime_error("planned constexpr sampler mismatch");
        if(!t || (d.type==VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER && d.sampler!=UINT32_MAX && !sam) || d.format!=(uint32_t)t->vkFormat)throw std::runtime_error("planned texture format mismatch");
        VkImageViewCreateInfo vi{};vi.sType=VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO;vi.image=t->image;vi.viewType=viewTypeFor(t->textureType);vi.format=(VkFormat)d.format;vi.components={(VkComponentSwizzle)d.components[0],(VkComponentSwizzle)d.components[1],(VkComponentSwizzle)d.components[2],(VkComponentSwizzle)d.components[3]};uint32_t base=op.sampleBase[d.slot],count=op.sampleCount[d.slot]?op.sampleCount[d.slot]:t->levels-base;vi.subresourceRange={VK_IMAGE_ASPECT_COLOR_BIT,base,count,0,1};
        VkImageView view{};
        {Measure measure{objectNanos,SubPhase::objects};std::array<uint32_t,4> components{d.components[0],d.components[1],d.components[2],d.components[3]};
         uint64_t viewProbes=0;auto found=viewCache.end();{auto range=viewIndex.equal_range(viewKey(t,vi.format,components,base,count));for(auto it=range.first;it!=range.second;++it){++viewProbes;auto &c=viewCache[it->second];if(c.texture==t && c.format==vi.format && c.components==components && c.base==base && c.count==count){found=viewCache.begin()+it->second;break;}}}
         scanAdd(ScanKind::viewProbes,viewProbes);if(found!=viewCache.end())view=found->view;
         else {check(CreateImageView(device,&vi,nullptr,&view));++reuseCounts.views;++submitObjectCreates;++submitViewCreates;
          try {if(viewCache.size()<viewCacheLimit){viewCache.push_back({t,vi.format,components,base,count,view});viewIndex.emplace(viewKey(t,vi.format,components,base,count),viewCache.size()-1);}else sampleViews.push_back(view);}catch(...){if(!viewCache.empty() && viewCache.back().view==view)viewCache.pop_back();DestroyImageView(device,view,nullptr);throw;}}
         }image={d.type==VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER && sam?sam->sampler:VK_NULL_HANDLE,view,sampleGeneral(opIndex,d.slot)?VK_IMAGE_LAYOUT_GENERAL:VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL};write.pImageInfo=&image;
#ifdef FUXI_MTL_HOST_TEST
        sampleViewUses.push_back({t,base,count,image.imageLayout});
#endif
       }else {auto b=op.buffers[d.stages==1?0:1][d.slot];if(!b.buffer || !d.range || d.offset>((b.length()+15)&~size_t(15)) || d.range>((b.length()+15)&~size_t(15))-d.offset)throw std::runtime_error("planned buffer range mismatch");buffer={b.buffer->buffer,d.offset,d.range};write.pBufferInfo=&buffer;}
       if(fast){auto &value=(*bindings)[i];value={d.binding,d.type,image,buffer};writes[i]=write;writes[i].pImageInfo=write.pImageInfo?&value.image:nullptr;writes[i].pBufferInfo=write.pBufferInfo?&value.buffer:nullptr;}
       else {Measure measure{descriptorNanos,SubPhase::descriptors};UpdateDescriptorSets(device,1,&write,0,nullptr);}
      }
      if(fast){Measure measure{descriptorNanos,SubPhase::descriptors};uint64_t probes=0,compares=0,hash=0xcbf29ce484222325ull;for(unsigned i=0;i<draw.descriptorCount;++i){auto &v=(*bindings)[i];for(uint64_t x:{uint64_t(v.binding)<<32|v.type,uint64_t(uintptr_t(v.image.sampler)),uint64_t(uintptr_t(v.image.imageView))^uint64_t(v.image.imageLayout)<<56,uint64_t(uintptr_t(v.buffer.buffer)),uint64_t(v.buffer.offset),uint64_t(v.buffer.range)})hash=(hash^x)*0x100000001b3ull;}hash^=uint64_t(uintptr_t(op.pipeline->setLayout));
       auto found=boundSets.end();{auto range=boundIndex.equal_range(hash);for(auto it=range.first;it!=range.second;++it){++probes;auto &entry=boundSets[it->second];++compares;if(entry.layout==op.pipeline->setLayout && entry.count==draw.descriptorCount && std::equal(bindings->begin(),bindings->begin()+draw.descriptorCount,entry.bindings.begin())){found=boundSets.begin()+it->second;break;}}}
       scanAdd(ScanKind::boundSetProbes,probes);scanAdd(ScanKind::boundSetCompares,compares);scanMax(ScanKind::boundSetMax,boundSets.size());
       if(found!=boundSets.end())set=found->set;
       else {check(AllocateDescriptorSets(device,&sai,&set));if(systemTranslationPolicy().frameTrace)++frameCounts.descriptors;
        for(unsigned i=0;i<draw.descriptorCount;++i)writes[i].dstSet=set;UpdateDescriptorSets(device,draw.descriptorCount,writes.data(),0,nullptr);
        if(boundSets.size()<boundSetLimit){boundIndex.emplace(hash,boundSets.size());boundSets.push_back({op.pipeline->setLayout,draw.descriptorCount,hash,*bindings,set});}
       }
      }
      if(set!=boundSet || op.pipeline->layout!=boundLayout){CmdBindDescriptorSets(cmd,VK_PIPELINE_BIND_POINT_GRAPHICS,op.pipeline->layout,0,1,&set,0,nullptr);boundSet=set;boundLayout=op.pipeline->layout;}
     }
     DrawInfo di{};di.scW=op.scissor.extent.width;di.scH=op.scissor.extent.height;di.scX=op.scissor.offset.x;di.scY=op.scissor.offset.y;
     if(currentFrame || drawQ){const auto &po=op.pipeline->options;di.pipeline=op.pipeline->id;di.count=draw.count;di.blend=po.blend;di.srcRGB=po.srcRGB;di.dstRGB=po.dstRGB;di.srcAlpha=po.srcAlpha;di.dstAlpha=po.dstAlpha;di.mask=po.mask;di.instr=op.pipeline->fragInstr;di.samples=op.pipeline->fragSamples;di.loops=op.pipeline->fragLoops;di.kills=op.pipeline->fragKills;
      for(unsigned i=0;i<draw.descriptorCount;++i){const auto &d=draw.descriptors[i];if(d.source || (d.type!=VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER && d.type!=VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE) || d.slot>=resourceSlots)continue;
       auto *t=d.slot==0 && !op.samples[0]?op.sourceTexture:op.samples[d.slot];if(!t)continue;uint8_t kind=t->tiling==VK_IMAGE_TILING_LINEAR?1:t->shadow?2:0;uint64_t bytes=uint64_t(t->width)*t->height*texelBytes(t->format);
       if(currentFrame){if(kind==1){++currentFrame->sampleLinear;currentFrame->sampleLinearBytes+=bytes;}else if(kind==2){++currentFrame->sampleShadow;currentFrame->sampleShadowBytes+=bytes;}else{++currentFrame->sampleOptimal;currentFrame->sampleOptimalBytes+=bytes;}}
       if(di.texCount<4){di.tex[di.texCount++]={t->width,t->height,t->format,kind,uint8_t(t->backingBuffer!=nullptr),uint8_t(t->imported || (t->backingBuffer && t->backingBuffer->imported))};}}}
     {PhaseTimer phase(SubPhase::record);for(unsigned i=0;i<draw.pushCount;++i){auto &w=draw.pushes[i];CmdPushConstants(cmd,op.pipeline->layout,w.stages,w.offset,w.size,w.bytes);}
     for(unsigned i=0;i<draw.vertexCount;++i){auto b=op.buffers[0][draw.vertexSlots[i]].buffer;VkDeviceSize offset=draw.vertexOffsets[i];CmdBindVertexBuffers(cmd,draw.vertexBindings[i],1,&b->buffer,&offset);}
     if(draw.indexed){CmdBindIndexBuffer(cmd,op.indices->buffer,draw.indexOffset,op.indexWidth==2?VK_INDEX_TYPE_UINT16:VK_INDEX_TYPE_UINT32);CmdDrawIndexed(cmd,draw.count,1,draw.first,0,0);}else CmdDraw(cmd,draw.count,1,draw.first,0);}
     if(drawQ && drawInfos.size()<passStampLimit){drawInfos.push_back(di);CmdWriteTimestamp(cmd,VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT,drawQ,uint32_t(drawInfos.size()));}
     }
     if(opIndex==passEnd){PhaseTimer phase(SubPhase::passes);CmdEndRenderPass(cmd);}
    } else if(op.texture && op.clearOnly && ((op.areaWidth && op.areaWidth!=op.texture->levelWidth(op.dstLevel)) || (op.areaHeight && op.areaHeight!=op.texture->levelHeight(op.dstLevel)))) {
     // Empty partial passes still execute Metal load/store over the chosen area.
     // A render pass CLEAR obeys renderArea; vkCmdClearColorImage cannot do so.
     Pipeline pass{};pass.options.format=0;pass.options.colorCount=op.colorCount;
     VkImageView views[8]{};VkClearValue clears[8]{};unsigned used=0;
     for(unsigned i=0;i<op.colorCount;++i){auto c=op.color(i);if(!c.texture)continue;
      if(c.load==1 && !c.texture->isDefined(c.level))throw std::runtime_error("load undefined partial empty attachment");
      if(i)pass.options.colors[i-1].format=c.texture->format;else pass.options.format=c.texture->format;
      views[used]=attachmentView(c.texture,c.level);memcpy(clears[used++].color.float32,c.clear,sizeof(c.clear));
     }
     auto render=renderPass(&pass,&op);try{emptyRenders.push_back(render);}catch(...){DestroyRenderPass(device,render,nullptr);throw;}
     VkFramebufferCreateInfo fi{};fi.sType=VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO;fi.renderPass=render;fi.attachmentCount=used;fi.pAttachments=views;fi.width=op.texture->levelWidth(op.dstLevel);fi.height=op.texture->levelHeight(op.dstLevel);fi.layers=1;
     VkFramebuffer frame{};check(CreateFramebuffer(device,&fi,nullptr,&frame));try{frames.push_back(frame);}catch(...){DestroyFramebuffer(device,frame,nullptr);throw;}
     ++reuseCounts.frames;++submitObjectCreates;++submitFrameCreates;
     VkRenderPassBeginInfo ri{};ri.sType=VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO;ri.renderPass=render;ri.framebuffer=frame;ri.renderArea={{0,0},{op.areaWidth?op.areaWidth:fi.width,op.areaHeight?op.areaHeight:fi.height}};ri.clearValueCount=used;ri.pClearValues=clears;
     if(currentFrame)++currentFrame->passes;{PhaseTimer phase(SubPhase::passes);CmdBeginRenderPass(cmd,&ri,VK_SUBPASS_CONTENTS_INLINE);CmdEndRenderPass(cmd);}
     for(unsigned i=0;i<op.colorCount;++i){auto c=op.color(i);if(c.texture)c.texture->setDefined(c.level,c.store && (c.load==2 || (c.load==1 && c.texture->isDefined(c.level))));}
    } else if(op.texture && op.clearOnly) {
     for(unsigned i=0;i<op.colorCount;++i){auto c=op.color(i);auto *t=c.texture;if(!t)continue;
      if((op.areaWidth && op.areaWidth!=t->levelWidth(c.level)) || (op.areaHeight && op.areaHeight!=t->levelHeight(c.level)))throw std::runtime_error("partial render area on an empty pass");
      if(c.load==1 && !t->isDefined(c.level))throw std::runtime_error("load undefined empty attachment");
      if(c.load==2){VkClearColorValue clear{};memcpy(clear.float32,c.clear,sizeof(c.clear));VkImageSubresourceRange range{VK_IMAGE_ASPECT_COLOR_BIT,c.level,1,0,1};CmdClearColorImage(cmd,t->image,VK_IMAGE_LAYOUT_GENERAL,&clear,1,&range);}
      t->setDefined(c.level,c.store && (c.load==2 || (c.load==1 && t->isDefined(c.level))));
     }
    } else if(op.texture && op.sourceTexture) {
     uint32_t copyWidth=op.width?op.width:op.texture->levelWidth(op.dstLevel),copyHeight=op.height?op.height:op.texture->levelHeight(op.dstLevel),copyDepth=op.depth?op.depth:op.texture->levelDepth(op.dstLevel);
     if(aliasCopies[opIndex]){
      VkBufferImageCopy snapshot{};snapshot.imageOffset={(int32_t)op.srcX,(int32_t)op.srcY,(int32_t)op.srcZ};snapshot.imageSubresource={VK_IMAGE_ASPECT_COLOR_BIT,op.srcLevel,0,1};snapshot.imageExtent={copyWidth,copyHeight,copyDepth};
      CmdCopyImageToBuffer(cmd,op.sourceTexture->image,VK_IMAGE_LAYOUT_GENERAL,aliasBuffer->buffer,1,&snapshot);
      // The source read must finish before any aliased destination clear/write.
      barrier.srcAccessMask=VK_ACCESS_TRANSFER_READ_BIT|VK_ACCESS_TRANSFER_WRITE_BIT;barrier.dstAccessMask=VK_ACCESS_TRANSFER_READ_BIT|VK_ACCESS_TRANSFER_WRITE_BIT;
      pb(VK_PIPELINE_STAGE_TRANSFER_BIT,VK_PIPELINE_STAGE_TRANSFER_BIT,0,1,&barrier,0,nullptr,0,nullptr);
     }
     if(!op.texture->isDefined(op.dstLevel) && (op.dstX || op.dstY || op.dstZ || (op.width && op.width!=op.texture->levelWidth(op.dstLevel)) || (op.height && op.height!=op.texture->levelHeight(op.dstLevel)) || (op.depth && op.depth!=op.texture->levelDepth(op.dstLevel)))){VkClearColorValue zero{};VkImageSubresourceRange range{VK_IMAGE_ASPECT_COLOR_BIT,op.dstLevel,1,0,1};CmdClearColorImage(cmd,op.texture->image,VK_IMAGE_LAYOUT_GENERAL,&zero,1,&range);
      barrier.srcAccessMask=VK_ACCESS_TRANSFER_WRITE_BIT;barrier.dstAccessMask=VK_ACCESS_TRANSFER_WRITE_BIT;pb(VK_PIPELINE_STAGE_TRANSFER_BIT,VK_PIPELINE_STAGE_TRANSFER_BIT,0,1,&barrier,0,nullptr,0,nullptr);}
     op.texture->setDefined(op.dstLevel);
     VkImageCopy region{};region.srcOffset={(int32_t)op.srcX,(int32_t)op.srcY,(int32_t)op.srcZ};region.dstOffset={(int32_t)op.dstX,(int32_t)op.dstY,(int32_t)op.dstZ};region.srcSubresource={VK_IMAGE_ASPECT_COLOR_BIT,op.srcLevel,0,1};region.dstSubresource={VK_IMAGE_ASPECT_COLOR_BIT,op.dstLevel,0,1};region.extent={op.width?op.width:op.texture->levelWidth(op.dstLevel),op.height?op.height:op.texture->levelHeight(op.dstLevel),copyDepth};
     if(aliasCopies[opIndex]){
      VkBufferImageCopy snapshot{};snapshot.imageOffset=region.dstOffset;snapshot.imageSubresource=region.dstSubresource;snapshot.imageExtent=region.extent;
      CmdCopyBufferToImage(cmd,aliasBuffer->buffer,op.texture->image,VK_IMAGE_LAYOUT_GENERAL,1,&snapshot);
     }else CmdCopyImage(cmd,op.sourceTexture->image,VK_IMAGE_LAYOUT_GENERAL,op.texture->image,VK_IMAGE_LAYOUT_GENERAL,1,&region);
    } else if(op.texture && op.upload) {
     if(!op.texture->isDefined(op.dstLevel) && (op.dstX || op.dstY || op.dstZ || (op.width && op.width!=op.texture->levelWidth(op.dstLevel)) || (op.height && op.height!=op.texture->levelHeight(op.dstLevel)) || (op.depth && op.depth!=op.texture->levelDepth(op.dstLevel)))){VkClearColorValue zero{};VkImageSubresourceRange range{VK_IMAGE_ASPECT_COLOR_BIT,op.dstLevel,1,0,1};CmdClearColorImage(cmd,op.texture->image,VK_IMAGE_LAYOUT_GENERAL,&zero,1,&range);
      barrier.srcAccessMask=VK_ACCESS_TRANSFER_WRITE_BIT;barrier.dstAccessMask=VK_ACCESS_TRANSFER_WRITE_BIT;pb(VK_PIPELINE_STAGE_TRANSFER_BIT,VK_PIPELINE_STAGE_TRANSFER_BIT,0,1,&barrier,0,nullptr,0,nullptr);}
     op.texture->setDefined(op.dstLevel);
     VkBufferImageCopy region{};region.bufferOffset=op.from;region.imageOffset={(int32_t)op.dstX,(int32_t)op.dstY,(int32_t)op.dstZ};region.imageSubresource={VK_IMAGE_ASPECT_COLOR_BIT,op.dstLevel,0,1};region.imageExtent={op.width?op.width:op.texture->levelWidth(op.dstLevel),op.height?op.height:op.texture->levelHeight(op.dstLevel),op.depth?op.depth:op.texture->levelDepth(op.dstLevel)};region.bufferRowLength=op.rowBytes?op.rowBytes/texelBytes(op.texture->format):0;region.bufferImageHeight=op.imageBytes?uint32_t(op.imageBytes/op.rowBytes):0;
     CmdCopyBufferToImage(cmd,op.src->buffer,op.texture->image,VK_IMAGE_LAYOUT_GENERAL,1,&region);
    } else if(op.texture) {
     if(!op.texture->isDefined(op.srcLevel))throw std::runtime_error("undefined readback texture");
     VkBufferImageCopy region{};region.bufferOffset=op.to;region.imageOffset={(int32_t)op.srcX,(int32_t)op.srcY,(int32_t)op.srcZ};region.imageSubresource={VK_IMAGE_ASPECT_COLOR_BIT,op.srcLevel,0,1};region.imageExtent={op.width?op.width:op.texture->levelWidth(op.srcLevel),op.height?op.height:op.texture->levelHeight(op.srcLevel),op.depth?op.depth:op.texture->levelDepth(op.srcLevel)};region.bufferRowLength=op.rowBytes?op.rowBytes/texelBytes(op.texture->format):0;region.bufferImageHeight=op.imageBytes?uint32_t(op.imageBytes/op.rowBytes):0;
     CmdCopyImageToBuffer(cmd,op.texture->image,VK_IMAGE_LAYOUT_GENERAL,op.dst->buffer,1,&region);
    } else if(op.src) {VkBufferCopy region{op.from,op.to,op.size};CmdCopyBuffer(cmd,op.src->buffer,op.dst->buffer,1,&region);}
    else CmdFillBuffer(cmd,op.dst->buffer,op.to,op.size,uint32_t(op.value)*0x01010101u);
    if(opIndex==passEnd){
     // Perf bit 4. A draw pass whose shaders write only attachments is already closed by the render pass external dependency
     // (color writes -> later shader/transfer/attachment accesses); only a pass with storage descriptors, or any transfer op, needs the global barrier.
     bool attachmentsOnly=boundaryPerf && !op.generateMips && op.texture && op.pipeline && !op.src && !op.dst;
     if(attachmentsOnly)for(unsigned j=0;j<draw.descriptorCount;++j){auto t=draw.descriptors[j].type;if(t==VK_DESCRIPTOR_TYPE_STORAGE_BUFFER || t==VK_DESCRIPTOR_TYPE_STORAGE_IMAGE || t==VK_DESCRIPTOR_TYPE_STORAGE_TEXEL_BUFFER)attachmentsOnly=false;}
     if(attachmentsOnly){if(currentFrame)++currentFrame->postBarriersSkipped;}
     else if(boundaryPerf){barrier.srcAccessMask=VK_ACCESS_MEMORY_WRITE_BIT;barrier.dstAccessMask=VK_ACCESS_MEMORY_READ_BIT|VK_ACCESS_MEMORY_WRITE_BIT;
      pb(barrierProducers|VK_PIPELINE_STAGE_HOST_BIT,VK_PIPELINE_STAGE_VERTEX_INPUT_BIT|barrierProducers,0,1,&barrier,0,nullptr,0,nullptr);}
     else {barrier.srcAccessMask=VK_ACCESS_MEMORY_WRITE_BIT;barrier.dstAccessMask=VK_ACCESS_MEMORY_READ_BIT|VK_ACCESS_MEMORY_WRITE_BIT;
     pb(VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,0,1,&barrier,0,nullptr,0,nullptr);}}
    if(passQ && opIndex==passEnd && passInfos.size()<passStampLimit){ // stamp after the pass and its closing barrier
     PassInfo info{};if(op.texture && op.pipeline)info=cur;
     else {info.kind=op.clearOnly?1:2;if(op.texture){info.width=op.texture->levelWidth(op.dstLevel);info.height=op.texture->levelHeight(op.dstLevel);}if(op.clearOnly)info.colors=op.colorCount;}
     info.copies=stampCopies;info.barriers=stampBarriers;stampCopies=stampBarriers=0;passInfos.push_back(info);CmdWriteTimestamp(cmd,VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT,passQ,uint32_t(passInfos.size()));
    }
   }
   barrier.srcAccessMask=VK_ACCESS_MEMORY_WRITE_BIT;barrier.dstAccessMask=VK_ACCESS_HOST_READ_BIT;
   pb(VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,VK_PIPELINE_STAGE_HOST_BIT,0,1,&barrier,0,nullptr,0,nullptr);
   if(timestamps)CmdWriteTimestamp(cmd,VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT,timestamps,1);
   {PhaseTimer phase(SubPhase::cmdEnd);check(EndCommandBuffer(cmd));}
   auto recordNS=since(recordStart)-(objectNanos-recordObjects)-descriptorNanos;
   submitRecordNanos+=recordNS;
   if(timing){timing->record+=recordNS;timing->desc+=descriptorNanos;}
   submitObjectsNanos+=objectNanos;submitDescriptorsNanos+=descriptorNanos;
#ifdef FUXI_MTL_HOST_TEST
   if(failBeforeQueue){failBeforeQueue=false;throw std::runtime_error("injected pre-submit failure");}
#endif
   VkSubmitInfo si{};si.sType=VK_STRUCTURE_TYPE_SUBMIT_INFO;si.commandBufferCount=1;si.pCommandBuffers=&cmd;
   // Any failure after attempting submission poisons ownership and avoids unsafe teardown.
   requireLive(); // A concurrent frontend refusal must cancel prepared work too.
   submitted=true;submitQueued=std::chrono::steady_clock::now();submitPrepareNanos+=(uint64_t)std::chrono::duration_cast<std::chrono::nanoseconds>(submitQueued-submitStart).count();
#ifdef FUXI_MTL_HOST_TEST
   if(failQueueAttempt){failQueueAttempt=false;check(VK_ERROR_DEVICE_LOST);}
#endif
   auto submitToQueue=[&]{
    auto start=FrameClock::now();
    {PhaseTimer phase(SubPhase::queueSubmit);check(frameKernelCall([&]{return QueueSubmit(queue,1,&si,fence);}));}
    if(currentFrame){++currentFrame->queueSubmits;currentFrame->queueSubmit+=frameNanos(start);}
    if(cpuTiming)cpuTiming->backendCPU+=frameCPUDelta(backendCPU,frameThreadCPU());
    if(currentFrame)currentFrame->backendSubmit+=frameNanos(locked);
   };
   if(!retirement){
   submitToQueue();if(scheduled)scheduled();
   auto waitStart=currentFrame?FrameClock::now():FrameClock::time_point{}; // Lavapipe can JIT a generic CA shader on its submission worker. The
   // offline qualifier may extend only its host wait; production stays 2 s.
#if defined(FUXI_MTL_HOST_TEST) && defined(FUXI_MTL_HOST_FENCE_NS)
   {PhaseTimer phase(SubPhase::fence);check(WaitForFences(device,1,&fence,VK_TRUE,FUXI_MTL_HOST_FENCE_NS));}
#else
   {PhaseTimer phase(SubPhase::fence);check(WaitForFences(device,1,&fence,VK_TRUE,2000000000ULL));}
#endif
   if(currentFrame)currentFrame->gpuWait+=frameNanos(waitStart);
   submitted=false;completed=true;submitWaitNanos+=since(submitQueued);auto submitDone=std::chrono::steady_clock::now();
   if(timestamps){uint64_t ticks[2]{};check(GetQueryPoolResults(device,timestamps,0,2,sizeof(ticks),ticks,sizeof(uint64_t),VK_QUERY_RESULT_64_BIT));
    uint64_t mask=timestampBits>=64?UINT64_MAX:(uint64_t(1)<<timestampBits)-1;
    uint64_t elapsed=(uint64_t)(double((ticks[1]-ticks[0])&mask)*timestampPeriod);
    gpuNanos.fetch_add(elapsed);if(currentFrame){currentFrame->gpuExec+=elapsed;++currentFrame->gpuSamples;}++gpuSamples;readPassTimes(passQ,passInfos,elapsed);readDrawTimes(drawQ,drawInfos);
   }
   auto invalidateStart=std::chrono::steady_clock::now();
   for(auto *t:textures)if(t->mapped && !coherent(t->memoryType)) {VkMappedMemoryRange r{};r.sType=VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE;r.memory=t->memory;r.size=VK_WHOLE_SIZE;++invalidateCalls;check(InvalidateMappedMemoryRanges(device,1,&r));}
   for(auto *b:buffers)if(!coherent(b->memoryType)) {VkMappedMemoryRange r{};r.sType=VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE;r.memory=b->memory;r.size=VK_WHOLE_SIZE;++invalidateCalls;check(InvalidateMappedMemoryRanges(device,1,&r));}
   {auto ns=since(invalidateStart);submitInvalidateNanos+=ns;if(currentFrame)currentFrame->invalidate+=ns;}
   cleanup();
#ifdef FUXI_MTL_HOST_TEST
   if(!reuseSubmitObjects){clearSubmitObjects(slot);clearFrames();clearViews();}
#endif
   {auto ns=since(submitDone);++submits;submitRetireNanos+=ns;if(currentFrame)currentFrame->retireHost+=ns;}
   return;
   }
   for(size_t n=0;n<ops.size();++n){auto &op=ops[n];bufferUse(writes,op.dst);
    if(op.pipeline || op.clearOnly){for(unsigned i=0;i<std::min(op.colorCount,8u);++i)textureUse(writes,op.color(i).texture);}
    else if(op.texture && (op.upload || op.sourceTexture || op.generateMips))textureUse(writes,op.texture);
    if(op.pipeline)for(unsigned j=0;j<draws[n].descriptorCount;++j){auto &d=draws[n].descriptors[j];if(d.type!=VK_DESCRIPTOR_TYPE_STORAGE_BUFFER)continue;
     for(unsigned stage=0;stage<2;++stage)if((d.stages&(stage?16u:1u)) && (d.binding>=16 || !(op.pipeline->readOnlyStorage[stage]&(1u<<d.binding)))){
      if(d.source==1){if(d.slot<op.pipeline->constantBlobs.size())bufferUse(writes,op.pipeline->constantBlobs[d.slot]);}else if(!d.source && d.slot<resourceSlots)bufferUse(writes,op.buffers[stage][d.slot].buffer);
     }
    }
   }
   // Build/pin retirement before QueueSubmit: allocation failure cannot abandon DMA.
   auto pinPhase=std::make_unique<PhaseTimer>(SubPhase::pin);auto pinnedBuffers=buffers;auto pinnedTextures=textures;
   std::vector<Pipeline *> pinnedPipelines;std::vector<Sampler *> pinnedSamplers;
   for(const auto &op:ops){if(op.pipeline && std::find(pinnedPipelines.begin(),pinnedPipelines.end(),op.pipeline)==pinnedPipelines.end())pinnedPipelines.push_back(op.pipeline);
    auto add=[&](Sampler *s){if(s && std::find(pinnedSamplers.begin(),pinnedSamplers.end(),s)==pinnedSamplers.end())pinnedSamplers.push_back(s);};add(op.sampler);for(auto *s:op.samplers)add(s);}
   pinPhase.reset();auto retired=std::make_shared<bool>(false);
   std::function<void()> finish=[this,&slot,fence,timestamps,passQ,passInfos,drawQ,drawInfos,pinnedBuffers,pinnedTextures,pinnedPipelines,pinnedSamplers,sampleViews,frames,emptyRenders,aliasBuffer,neutralBuffer,submitQueued,retired] {
    // Fence waits never hold the device/recording mutex. One owner retires each ticket.
    if(*retired)return;*retired=true;
    auto waitStart=FrameClock::now();VkResult result;
#if defined(FUXI_MTL_HOST_TEST) && defined(FUXI_MTL_HOST_FENCE_NS)
    {PhaseTimer phase(SubPhase::fence);result=WaitForFences(device,1,&fence,VK_TRUE,FUXI_MTL_HOST_FENCE_NS);}
#else
    {PhaseTimer phase(SubPhase::fence);result=WaitForFences(device,1,&fence,VK_TRUE,2000000000ULL);}
#endif
    if(currentFrame)currentFrame->gpuWait+=frameNanos(waitStart);
    auto lockStart=FrameClock::now();std::lock_guard<std::mutex> lock(mutex);
    if(currentFrame)currentFrame->retireLock+=frameNanos(lockStart);
    auto releaseSlot=[&]{slot.hostUses.clear();slot.writes.clear();slot.uses.clear();slot.busy=false;submitWake.notify_all();};
    try {
     if(result!=VK_SUCCESS){poisoned=true;check(result);}
     // Another in-flight failure also prohibits host invalidation and teardown.
     if(poisoned)throw std::runtime_error("device poisoned while in flight");
     submitWaitNanos+=frameNanos(submitQueued);auto done=FrameClock::now();
     if(timestamps){uint64_t ticks[2]{};check(GetQueryPoolResults(device,timestamps,0,2,sizeof(ticks),ticks,sizeof(uint64_t),VK_QUERY_RESULT_64_BIT));
      uint64_t mask=timestampBits>=64?UINT64_MAX:(uint64_t(1)<<timestampBits)-1;
      uint64_t elapsed=uint64_t(double((ticks[1]-ticks[0])&mask)*timestampPeriod);
      gpuNanos+=elapsed;if(currentFrame){currentFrame->gpuExec+=elapsed;++currentFrame->gpuSamples;}++gpuSamples;readPassTimes(passQ,passInfos,elapsed);readDrawTimes(drawQ,drawInfos);
     }
     auto invalidateStart=FrameClock::now();
     for(auto *t:pinnedTextures)if(t->mapped && !coherent(t->memoryType)){VkMappedMemoryRange r{};r.sType=VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE;r.memory=t->memory;r.size=VK_WHOLE_SIZE;++invalidateCalls;check(InvalidateMappedMemoryRanges(device,1,&r));}
     for(auto *b:pinnedBuffers)if(!coherent(b->memoryType)){VkMappedMemoryRange r{};r.sType=VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE;r.memory=b->memory;r.size=VK_WHOLE_SIZE;++invalidateCalls;check(InvalidateMappedMemoryRanges(device,1,&r));}
     {auto ns=frameNanos(invalidateStart);submitInvalidateNanos+=ns;if(currentFrame)currentFrame->invalidate+=ns;}
     if(aliasBuffer!=slot.aliasScratch)destroyUnlocked(aliasBuffer);destroyUnlocked(neutralBuffer);
     for(auto v:sampleViews)DestroyImageView(device,v,nullptr);for(auto f:frames)DestroyFramebuffer(device,f,nullptr);for(auto r:emptyRenders)DestroyRenderPass(device,r,nullptr);
     for(auto *b:pinnedBuffers)destroyUnlocked(b);for(auto *t:pinnedTextures)destroyTextureUnlocked(t);for(auto *p:pinnedPipelines)destroyPipelineUnlocked(p);for(auto *s:pinnedSamplers)destroySamplerUnlocked(s);
#ifdef FUXI_MTL_HOST_TEST
     if(!reuseSubmitObjects){clearSubmitObjects(slot);clearFrames();clearViews();}
#endif
     {auto ns=frameNanos(done);++submits;submitRetireNanos+=ns;if(currentFrame)currentFrame->retireHost+=ns;}releaseSlot();
    }catch(...){poisoned=true;releaseSlot();throw;} // Quarantine all pinned GPU objects on uncertain ownership.
   };
   for(auto *b:pinnedBuffers)++b->references;for(auto *t:pinnedTextures)++t->references;for(auto *p:pinnedPipelines)++p->references;for(auto *s:pinnedSamplers)++s->references;
   slot.hostUses=std::move(hostUses);slot.uses=std::move(uses);slot.writes=std::move(writes);slot.busy=true;
   submitToQueue();if(scheduled)scheduled();
   submitted=false;completed=true;
   *retirement=std::move(finish);
  } catch(...) {if(submitted){poisoned=true;slot.busy=false;submitWake.notify_all();}else if(!completed){for(auto old:oldTextures){old.texture->layout=old.layout;old.texture->levelLayouts=old.levelLayouts;old.texture->defined=old.defined;old.texture->definedLevels=old.definedLevels;old.texture->shadowHash=old.shadowHash;old.texture->shadowHashed=old.shadowHashed;}clearSubmitObjects(slot);cleanup();}
   try{throw;}catch(const std::exception &e){if(diagnosticOp<ops.size() && !ops[diagnosticOp].diagnostic.empty())throw std::runtime_error(std::string(e.what())+" desc="+ops[diagnosticOp].diagnostic);throw;}
  }
 }
};
}
