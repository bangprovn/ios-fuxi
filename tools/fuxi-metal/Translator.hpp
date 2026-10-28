/* Original dynamic boundary. The translator and all its dependencies stay external. */
#pragma once
#include <cstddef>
#include <cstring>
#include "ConstantLayout.hpp"
#include "ShadowRanges.hpp"
#include "CAState.hpp"
#include "TranslationPolicy.hpp"
#include <cstdint>
#include <dlfcn.h>
#include <stdexcept>
#include <string>
#include <chrono>
#include <cstdio>
#ifndef FUXI_MTL_TRANSLATOR
#define FUXI_MTL_TRANSLATOR "/usr/local/lib/libmetal2vulkan.dylib"
#endif
namespace fuxi {
// ABI 8. All ordinals in the input are public Metal values; outputs are Vulkan values.
struct Constant {uint32_t index{},type{},size{};uint8_t bytes[16]{};char name[128]{};};
struct Attribute {uint32_t format{},offset{},buffer{};};
struct ColorOptions {
 uint32_t format=0,blend=0,srcRGB=1,dstRGB=0,rgbOp=0,srcAlpha=1,dstAlpha=0,alphaOp=0,mask=15;
};
struct ColorBlend {uint32_t blend{},mask{},factors[6]{};};
// DeviceFeature bits: 1 shaderFloat16, 2 storageBuffer16BitAccess, 4 uniformAndStorageBuffer16BitAccess, 8 shaderInt16, 16 robustBufferAccess, 32 robustBufferAccess2, 64 robustImageAccess2.
enum DeviceFeature : uint32_t {featFloat16=1,featStorage16=2,featUniform16=4,featInt16=8,featRobustBuffer=16,featRobustBuffer2=32,featRobustImage2=64};
struct CompileOptions {
 uint32_t format=80,blend=0,srcRGB=1,dstRGB=0,rgbOp=0,srcAlpha=1,dstAlpha=0,alphaOp=0,mask=15,strip=0;
 Attribute attributes[8]{};uint32_t strides[4]{};uint32_t constantCount[2]{};Constant constants[2][maxFunctionConstants]{};
 // Actual VkDevice enables, supplied by Backend; never inferred from support.
 uint32_t orderedColorAccess{};
 uint32_t independentBlend{};uint32_t colorCount=1;ColorOptions colors[7]{};
 uint32_t deviceFeatures{}; // bitmask of the extra VkDevice enables below; must equal the VkFeature set the bridge gives metal2vulkan
 ColorOptions color(unsigned i) const {return i?colors[i-1]:ColorOptions{format,blend,srcRGB,dstRGB,rgbOp,srcAlpha,dstAlpha,alphaOp,mask};}
};
struct Descriptor {uint32_t binding{},type{},stages{},slot{},sampler{},format{},components[4]{};uint64_t offset{},range{};uint32_t set{},source{};};
struct VertexBinding {uint32_t binding{},slot{},stride{};};
struct VertexAttribute {uint32_t location{},binding{},format{},offset{};};
struct BoundBuffer {uint64_t length{},offset{};uint32_t bytes{};};
struct DrawInput {BoundBuffer buffers[2][resourceSlots]{};uint32_t formats[resourceSlots]{},filters[resourceSlots]{};float lodMin[resourceSlots]{},lodMax[resourceSlots]{};uint32_t count{},start{},indexed{},strip{};uint64_t indexLength{},indexOffset{};};
// Compare every ABI input field, without struct padding or float arithmetic.
// The draw planner reads only these values and the immutable product. Buffer
// contents, image layouts and resource identity are validated by Backend later.
inline bool sameDrawInput(const DrawInput &a,const DrawInput &b) {
 if(a.count!=b.count || a.start!=b.start || a.indexed!=b.indexed || a.strip!=b.strip || a.indexLength!=b.indexLength || a.indexOffset!=b.indexOffset)return false;
 for(unsigned i=0;i<resourceSlots;++i){
  if(a.formats[i]!=b.formats[i] || a.filters[i]!=b.filters[i] || memcmp(&a.lodMin[i],&b.lodMin[i],sizeof(float)) || memcmp(&a.lodMax[i],&b.lodMax[i],sizeof(float)))return false;
  for(unsigned stage=0;stage<2;++stage){auto &x=a.buffers[stage][i];auto &y=b.buffers[stage][i];if(x.length!=y.length || x.offset!=y.offset || x.bytes!=y.bytes)return false;}
 }
 return true;
}
struct Push {uint32_t offset{},size{},stages{};uint8_t bytes[128]{};};
struct DrawOutput {uint32_t descriptorCount{},pushCount{},vertexCount{},indexed{},count{},first{};uint64_t indexOffset{};Descriptor descriptors[16]{};Push pushes[2]{};uint32_t vertexBindings[4]{},vertexSlots[4]{};uint64_t vertexOffsets[4]{};};
// Optional ABI-8 extension, emitted by our bridge from public pipeline/plan metadata.
struct DrawBufferRebase {uint32_t active{},stage{},slot{},whole{};int64_t start{};uint64_t size{},alignment{},maxRange{};};
struct DrawPushRebase {uint32_t push{},offset{},kind{},source{};}; // residual descriptor, guest slot, draw base
struct DrawRebase {uint32_t enabled{},pushCount{};DrawBufferRebase buffers[16]{};DrawPushRebase pushes[64]{};};
static_assert(sizeof(DrawBufferRebase)==48 && sizeof(DrawRebase)==1800,"draw rebase ABI");
struct Specialization {uint32_t id{},offset{},size{};};
struct Range {uint32_t offset{},size{},stages{};};
struct BakedSampler {uint32_t enabled{},mag{},min{},mip{},address[3]{};float bias{},minLod{},maxLod{};uint32_t border{};};
struct Blob {const uint8_t *bytes{};size_t length{};uint8_t hash[32]{};};
struct Translation {
 void *handle{}; const uint32_t *vertex{}; size_t vertexCount{}; const char *vertexEntry{};
 const uint32_t *fragment{}; size_t fragmentCount{}; const char *fragmentEntry{};
 uint32_t pushOffset{},pushSize{},pushStages{};uint8_t pushBytes[128]{};uint32_t descriptorCount{};Descriptor descriptors[16]{};uint32_t specCount{},specSize{};Specialization specs[32]{};uint8_t specData[256]{};
 uint32_t bindingCount{},attributeCount{};VertexBinding bindings[4]{};VertexAttribute attributes[8]{};uint32_t format{},blend{},mask{},factors[6]{};uint32_t rangeCount{};Range ranges[2]{};uint32_t fetchMask{},orderedColorAccess{};uint32_t blobCount{};Blob blobs[2]{};ColorBlend colors[8]{};
};
class Translator {
 void *dylib{};
public:
 using CacheRead=size_t (*)(void *,const uint8_t *,const uint8_t **);
 using CacheReadVerified=size_t (*)(void *,const uint8_t *,const uint8_t **,uint32_t *);
 using CacheVerify=int (*)(const uint8_t *,const uint8_t *,size_t);
 using CacheWrite=void (*)(void *,const uint8_t *,const uint8_t *,size_t);
 using CacheOutcome=void (*)(void *,uint32_t);
 using CacheScope=void (*)(void *,CacheRead,CacheWrite,CacheOutcome);
 using CacheScopeVerified=void (*)(void *,CacheReadVerified,CacheWrite,CacheOutcome);
 CacheScopeVerified artifactScopeVerified{};CacheVerify artifactVerify{};
 using TranslationScope=void (*)(uint32_t);
 TranslationScope translationScope{}; // Optional ABI-8 Fuxi adapter extension, thread-local flags.
 struct OptionsScope {
  TranslationScope fn;
  explicit OptionsScope(Translator &tr):fn(tr.translationScope){auto p=systemTranslationPolicy();if(fn)fn((p.stages?1u:0u)|(p.trace?2u:0u));}
  ~OptionsScope(){if(fn)fn(0);}
  OptionsScope(const OptionsScope&)=delete;
 };
 CacheScope artifactScope{}; // Optional ABI-8 extension; synchronous thread-local callbacks.
 struct Scope {
  CacheScope fn;CacheScopeVerified verified;
  Scope(CacheScope callback,void *context,CacheRead read,CacheWrite write,CacheOutcome outcome,CacheScopeVerified v2=nullptr,CacheReadVerified readVerified=nullptr):fn(callback),verified(v2){if(verified)verified(context,readVerified,write,outcome);else if(fn)fn(context,read,write,outcome);}
  ~Scope(){if(verified)verified(nullptr,nullptr,nullptr,nullptr);else if(fn)fn(nullptr,nullptr,nullptr,nullptr);}
  Scope(const Scope&)=delete;
 };
 void *(*open)(const uint8_t *,size_t,char *,size_t){};
 void (*close)(void *){};
 int (*function)(void *,const char *,char *,size_t){};
 int (*compile)(void *,const char *,void *,const char *,void *,uint64_t,uint64_t,const CompileOptions *,Translation *,char *,size_t){};
 int (*draw)(void *,const DrawInput *,DrawOutput *,char *,size_t){};
 int (*draw_plan)(void *,const DrawInput *,DrawOutput *,DrawRebase *,char *,size_t){};
 int planDraw(void *p,const DrawInput *i,DrawOutput *o,DrawRebase *r,char *why,size_t cap){*r={};return draw_plan && !systemTranslationPolicy().trace?draw_plan(p,i,o,r,why,cap):draw(p,i,o,why,cap);}
 int (*variant)(void *,const DrawInput *,Translation *,char *,size_t){};
 int (*sampler)(void *,uint32_t,BakedSampler *,char *,size_t){};
 int (*identity)(void *,const char *,void *,const char *,const CompileOptions *,uint8_t *,char *,size_t){};
 int (*product_hash)(void *,uint8_t *){};
 int (*shadow_windows)(void *,ShadowWindow *){}; // Optional immutable access proof; absent means whole buffers.
 uint32_t (*texture_type)(void *,uint32_t){}; // Optional ABI-8 reflected Metal type; UINT32_MAX means unused.
 void (*free)(void *){};
 Translator() {
  dylib=dlopen(FUXI_MTL_TRANSLATOR,RTLD_NOW|RTLD_LOCAL);
  if(!dylib)throw std::runtime_error("dlopen external metal2vulkan failed");
  auto abi=(uint32_t (*)())dlsym(dylib,"fuxi_m2v_abi");
  if(!abi || abi()!=8) {dlclose(dylib);throw std::runtime_error("metal2vulkan C ABI mismatch");}
#define LOAD(n) n=(decltype(n))dlsym(dylib,"fuxi_m2v_" #n);if(!n) {dlclose(dylib);throw std::runtime_error("missing translator " #n);}
  LOAD(open) LOAD(close) LOAD(function) LOAD(compile) LOAD(draw) LOAD(variant) LOAD(sampler) LOAD(identity) LOAD(product_hash) LOAD(free)
#undef LOAD
  draw_plan=(decltype(draw_plan))dlsym(dylib,"fuxi_m2v_draw_plan_v1");
  shadow_windows=(decltype(shadow_windows))dlsym(dylib,"fuxi_m2v_shadow_windows_v1");
  texture_type=(decltype(texture_type))dlsym(dylib,"fuxi_m2v_texture_type");
  translationScope=(TranslationScope)dlsym(dylib,"fuxi_m2v_translation_scope_v1");
  auto policy=systemTranslationPolicy();if(policy.stages || policy.trace)fprintf(stderr,"FUXI MTL: translation settings stages=%u trace=%u bridge=%u\n",unsigned(policy.stages),unsigned(policy.trace),unsigned(translationScope!=nullptr));
  artifactScope=(CacheScope)dlsym(dylib,"fuxi_m2v_artifact_scope_v1");
  artifactScopeVerified=(CacheScopeVerified)dlsym(dylib,"fuxi_m2v_artifact_scope_v2");
  artifactVerify=(CacheVerify)dlsym(dylib,"fuxi_m2v_artifact_verify_v2");
  if(!artifactVerify)artifactScopeVerified=nullptr;
 }
 ~Translator() {if(dylib)dlclose(dylib);}
 Translator(const Translator&)=delete;Translator& operator=(const Translator&)=delete;
 static Translator &get() {static Translator translator;return translator;}
 static bool cacheIdentity(uint8_t *out) noexcept {
  try {
   auto &tr=get();auto fn=(int (*)(uint8_t *))dlsym(tr.dylib,"fuxi_m2v_cache_identity");
   return fn && fn(out)==1;
  }catch(...){return false;} // Older/unavailable bridges keep the memory-only path.
 }
};
}
