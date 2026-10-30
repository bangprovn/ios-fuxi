/* Host-only model for Objective-C ownership/state tests; never linked into iOS. */
#pragma once
#include "FrontendEncoding.hpp"
#include <algorithm>
#include <mutex>
#include <cassert>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <stdexcept>
#include <vector>
#include <functional>
#include <unistd.h>
#include <atomic>
#include "ResourceLimits.hpp"
#include "TextureLevels.hpp"
#include "PixelFormats.hpp"
#include "ObjectCache.hpp"
#include "Translator.hpp"
#include "PrewarmStore.hpp"
#include "DrawSafety.hpp"
#include "BackboardGate.hpp"
namespace fuxi {
struct Buffer {void *contents;size_t length,allocated;bool upload=false;void *shadowKey() const{return contents;}};
struct PrepareWrite {Buffer *buffer;uint64_t offset,bytes;};
struct Texture : TextureLevels {size_t allocated{},charged{};uint32_t format=80;Buffer *backingBuffer{};size_t backingOffset{},backingRow{};bool shadow=false;std::vector<uint8_t> pixels{};
 size_t offset(uint32_t level) const {size_t o=0;for(uint32_t l=0;l<level;++l)o+=levelWidth(l)*levelHeight(l)*levelDepth(l)*metalTexelBytes(format);return o;}
};
struct Pipeline {ShadowWindow shadowWindows[2][resourceSlots]{};CompileOptions options{};bool sampled=false;size_t references=1;};
struct Sampler {};
struct Viewport {float x,y,width,height,minDepth,maxDepth;};
struct Rect {struct {int32_t x,y;}offset;struct {uint32_t width,height;}extent;};
struct BufferBind {Buffer *buffer{};uint64_t offset{};bool bytes=false;uint32_t end{};
 uint64_t length() const{return end?end:buffer?buffer->length:0;}};
struct ColorTarget {Texture *texture{};uint32_t level{},load=2,store=1;float clear[4]{};};
struct Operation {
 Buffer *src, *dst; size_t from, to, size; uint8_t value;
 Texture *texture{};Pipeline *pipeline{};uint64_t encoderID{};float clear[4]{};bool renderDraw=false;
 Texture *sourceTexture{};Sampler *sampler{};uint32_t vertices=3;bool upload=false,clearOnly=false;
 uint32_t srcX{},srcY{},dstX{},dstY{},width{},height{};size_t rowBytes{};uint32_t srcZ{},dstZ{},depth{};size_t imageBytes{};
 Texture *samples[resourceSlots]{};Sampler *samplers[resourceSlots]{};BufferBind buffers[2][resourceSlots]{};Buffer *indices{};uint64_t indexOffset{};uint32_t first{},load=2,store=1,indexWidth=2;Topology topology=Topology::Triangle;
 Viewport viewport{0,256,256,-256,0,1};Rect scissor{{0,0},{256,256}};
 uint32_t areaWidth{},areaHeight{};
 uint32_t srcLevel{},dstLevel{},sampleBase[resourceSlots]{},sampleCount[resourceSlots]{};bool generateMips=false;uint32_t mipBase{},mipCount{};
 OperationDiagnostic diagnostic{};uint32_t colorCount=1,colorMask=1;ColorTarget colors[7]{};
 ColorTarget color(unsigned i) const {if(!(colorMask&(1u<<i)))return {};if(i)return colors[i-1];ColorTarget c{texture,dstLevel,load,store,{}};memcpy(c.clear,clear,sizeof(clear));return c;}
 void setColor(unsigned i,const ColorTarget &c){if(i){colors[i-1]=c;return;}texture=c.texture;dstLevel=c.level;load=c.load;store=c.store;memcpy(clear,c.clear,sizeof(clear));}

};
class Backend {
public:
 std::mutex mutex;
 ResourceLimits limits;ObjectCache<Pipeline> objects;
 static constexpr size_t maxBufferLength=ResourceLimits::single;
 static inline std::atomic<uint64_t> pipelineHits{0},pipelineMisses{0},gpuNanos{0},gpuSamples{0};
 static inline std::atomic<uint64_t> pipelineCacheLoadResult{0},pipelineCacheLoadBytes{0},pipelineCacheSaveResult{0},pipelineCacheSaveBytes{0},pipelineCacheSaveCount{0},pipelineCacheSaveFailures{0};
 static inline std::atomic<uint64_t> submitObjectsNanos{0},submitDescriptorsNanos{0},submitRecordNanos{0},shadowCopyNanos{0},submitFlushNanos{0},submitInvalidateNanos{0},flushCalls{0},invalidateCalls{0},emptySubmits{0},submitObjectCreates{0},submitPoolCreates{0},submitFrameCreates{0},submitViewCreates{0};
 static inline std::atomic<uint64_t> submits{0},submitPrepareNanos{0},submitWaitNanos{0},submitRetireNanos{0},submitSlotWaitNanos{0},submitBlocked{0},submitBlockedFull{0},submitBlockedDrain{0},submitBlockedConflict{0},submitEntered{0};
 static inline std::function<void()> beforeSubmit;
 static inline bool modelPipelines=false;static inline std::function<void(const Operation &)> modelDraw;
 void registerLibrary(void *,LibraryHash){}
 void unregisterLibrary(void *){}
 void requireLive(){if(poisoned || backboardGate.refusesResources())throw std::runtime_error("device poisoned or backboardd failure latched");}
 uint32_t maxTextureDimension() const{return limits.dimension;}
 size_t resourceBytes() const{return limits.used;}
 ~Backend(){objects.clear([&](Pipeline *p){destroy(p);});}
 static inline bool modelTextures=false;
 static inline int live=0,submissions=0,constructed=0;std::atomic<bool> poisoned{false};
 explicit Backend(const char * ="unknown") {++constructed;}
 Buffer *allocate(size_t n) {
  if(!n || n>maxBufferLength)throw std::runtime_error("length");
  limits.reserve(n);auto *b=new Buffer{calloc(1,n),n,n};++live;return b;
 }
 void destroy(Buffer *b) {if(b){limits.release(b->allocated);free(b->contents);delete b;--live;}}
 static size_t texelBytes(uint32_t f){return metalTexelBytes(f);}
 bool supportsOrderedColorAccess() const {return false;}
 static inline bool modelHostImport=false;
 static inline void *importBase=nullptr;static inline size_t importBytes=0,importRow=0,importOffset=0;
 bool supportsHostImport() const {return modelHostImport;}
 Texture *importTexture(void *base,size_t bytes,size_t row,uint32_t width=256,uint32_t height=256,size_t offset=0,uint64_t=0) {
  if(!modelHostImport || !base || !limits.rows(width,height,4,row,offset,bytes))throw std::runtime_error("modeled host import bounds");
  importBase=base;importBytes=bytes;importRow=row;importOffset=offset;
  limits.reserve(bytes);auto *t=new Texture{};t->width=width;t->height=height;t->allocated=t->charged=bytes;t->defined=true;return t;
 }
 Buffer *importBuffer(void *,size_t) {throw std::runtime_error("no modeled host buffer import");}
 Texture *linearTexture(Buffer *b,uint32_t format,uint32_t usage,uint32_t width,uint32_t height,size_t offset,size_t row,bool=false) {
  requireLive();auto pixel=texelBytes(format);
  if(!b || usage!=1 || !limits.linearRows(width,height,pixel,row,offset,b->length))throw std::runtime_error("linear texture descriptor/extent/stride");
  auto *t=allocateTexture(format,usage,width,height);t->shadow=true;t->backingBuffer=b;t->backingOffset=offset;t->backingRow=row;return t;
 }
 Sampler *allocateSampler(bool,bool,uint32_t=0,float=0,float=0,const uint32_t * =nullptr,uint32_t=0,uint32_t=1) {return new Sampler{};}
 void destroy(Sampler *s) {delete s;}
 Texture *allocateTexture(uint32_t format=80,uint32_t usage=5,uint32_t width=256,uint32_t height=256,uint32_t levels=1,uint32_t type=2,uint32_t depth=1) {
  if(!modelTextures)throw std::runtime_error("M4 CPU model has no rendering");
  if(!textureShapeAccepted(type,height,usage,depth,levels))throw std::runtime_error("texture dimension/usage");
  if(!limits.extent(width,height,texelBytes(format)) || !limits.volume(width,height,depth,texelBytes(format)))throw std::runtime_error("extent");
  auto *t=new Texture{};t->format=format;t->width=width;t->height=height;t->depth=depth;t->levels=levels;t->textureType=type;t->allocated=t->offset(levels);limits.reserve(t->allocated);t->charged=t->allocated;t->pixels.resize(t->allocated);return t;
 }
 Pipeline *compilePipeline(void *,const char *v,void *,const char *f,const CompileOptions &options=CompileOptions{}) {
  if(!modelPipelines)throw std::runtime_error("M4 CPU model has no translation");
  // The ABI input consists only of initialized 32-bit fields/byte arrays.
  static_assert(sizeof(CompileOptions)==40364);
  std::string key=std::string(v)+"/"+f+"/";key.append((const char *)&options,sizeof(options));
  if(auto *p=objects.acquire(key)){++pipelineHits;return p;}
  ++pipelineMisses;auto *p=new Pipeline{};p->options=options;objects.insert(key,key,p,sizeof(Pipeline),[&](Pipeline *old){destroy(old);});return p;
 }
 bool destroy(Texture *t) {if(t){limits.release(t->charged);delete t;}return !poisoned;}
 void destroy(Pipeline *p) {if(p && !--p->references)delete p;}
 bool requiresSampling(const Pipeline *p) const {return p->sampled;}
 void submit(const std::vector<Operation>& ops,const std::function<void()> &scheduled={},std::function<void()> *retirement=nullptr,const std::function<void()> &prepare={},const std::vector<PrepareWrite> * =nullptr) {
  if(retirement && beforeSubmit)beforeSubmit();
  if(prepare)prepare();
  if(retirement){auto copy=ops;submit(copy,scheduled);*retirement=[this]{requireLive();};return;}
  if(getenv("FUXI_TEST_LOST")) {poisoned=true;throw std::runtime_error("modeled device loss");}
  if(getenv("FUXI_TEST_HANG"))for(;;)pause();
  if(ops.empty()){++emptySubmits;if(scheduled)scheduled();return;}
  ++submissions;if(scheduled)scheduled();
  for(auto &op:ops) {
   if(op.texture){auto *t=op.texture;
    if(op.pipeline){if(!modelDraw)throw std::runtime_error("CPU model has no shaders");modelDraw(op);continue;}
    uint32_t l=op.upload || op.sourceTexture || op.clearOnly?op.dstLevel:op.srcLevel;
    size_t w=t->levelWidth(l),h=t->levelHeight(l),pixel=texelBytes(t->format);auto *data=t->pixels.data()+t->offset(l);
    if(op.generateMips)throw std::runtime_error("model does not generate mips");
    if(op.clearOnly){if(op.load==2){for(size_t i=0;i<w*h*pixel;i+=4){data[i]=(uint8_t)(op.clear[2]*255);data[i+1]=(uint8_t)(op.clear[1]*255);data[i+2]=(uint8_t)(op.clear[0]*255);data[i+3]=(uint8_t)(op.clear[3]*255);}}
     t->setDefined(l,op.store && (op.load==2 || (op.load==1 && t->isDefined(l))));
    }else {
     size_t width=op.width?op.width:w,height=op.height?op.height:h,depth=op.depth?op.depth:t->levelDepth(l),row=op.rowBytes?op.rowBytes:width*pixel,image=op.imageBytes?op.imageBytes:row*height;
     auto at=[&](Texture *x,uint32_t level,size_t px,size_t py,size_t pz){return x->pixels.data()+x->offset(level)+((pz*x->levelHeight(level)+py)*x->levelWidth(level)+px)*pixel;};
     if(op.upload){for(size_t z=0;z<depth;++z)for(size_t y=0;y<height;++y)memcpy(at(t,l,op.dstX,op.dstY+y,op.dstZ+z),(uint8_t *)op.src->contents+op.from+z*image+y*row,width*pixel);t->setDefined(l);}
     else if(op.sourceTexture){auto *src=op.sourceTexture;assert(src->isDefined(op.srcLevel));std::vector<uint8_t> snapshot(width*height*depth*pixel);for(size_t z=0;z<depth;++z)for(size_t y=0;y<height;++y)memcpy(snapshot.data()+(z*height+y)*width*pixel,at(src,op.srcLevel,op.srcX,op.srcY+y,op.srcZ+z),width*pixel);for(size_t z=0;z<depth;++z)for(size_t y=0;y<height;++y)memcpy(at(t,l,op.dstX,op.dstY+y,op.dstZ+z),snapshot.data()+(z*height+y)*width*pixel,width*pixel);t->setDefined(l);}
     else {if(!t->isDefined(l))throw std::runtime_error("undefined model readback");for(size_t z=0;z<depth;++z)for(size_t y=0;y<height;++y)memcpy((uint8_t *)op.dst->contents+op.to+z*image+y*row,at(t,l,op.srcX,op.srcY+y,op.srcZ+z),width*pixel);}
    }
    continue;
   }
   auto *dst=(uint8_t *)op.dst->contents+op.to;
   if(op.src)memcpy(dst,(uint8_t *)op.src->contents+op.from,op.size);
   else memset(dst,op.value,op.size);
  }
  if(getenv("FUXI_TEST_BAD_DATA") && !ops.empty()) ((uint8_t *)ops.back().dst->contents)[ops.back().dst->length-1]^=1;
 }
};
}
