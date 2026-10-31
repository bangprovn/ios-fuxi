/* FUXI_MTL_FETCH_SPLIT pixel controls: CA-shaped passes that mix framebuffer-fetch draws (programmable blend with the destination) with ordinary
   SRC_ALPHA/ONE_MINUS_SRC_ALPHA draws. The test harness compares PIXELS lines with the knob off and on; FETCHSPLIT lines report the expected and
   issued in-pass barriers (a fetch draw that follows a non-fetch draw of the same merged pass) and the proxy's count of in-pass barriers. */
#include "Backend.hpp"
#include <cassert>
#include <cstdio>
int main(){
 setenv("FUXI_MTL_SHADOW_HASH","0",1);
 using namespace fuxi;Backend b;const bool merge=systemTranslationPolicy().merge,on=systemTranslationPolicy().fetchSplit;
 if(!b.supportsOrderedColorAccess()){puts("SKIP: lavapipe lacks enabled VK_EXT_rasterization_order_attachment_access; fetch split pixels");return 0;}
 auto *src=b.allocateTexture(80,5,16,16),*other=b.allocateTexture(80,5,16,16),*dst=b.allocateTexture(80,5,16,16,3);
 auto *bytes=b.allocate(1024),*read=b.allocate(1024);auto *normal=b.compilePipeline(nullptr,"control",nullptr,"control"),*fetch=b.compilePipeline(nullptr,"control",nullptr,"fetch");
 assert(!normal->fetchMask && fetch->fetchMask==1);auto *sam=b.allocateSampler(false,false,1);
 auto put=[&](Texture *t,uint8_t value){memset(bytes->contents,value,1024);for(unsigned i=3;i<1024;i+=4)((uint8_t *)bytes->contents)[i]=t==src?96:160;Operation op{};op.src=bytes;op.texture=t;op.upload=true;b.submit({op});};
 put(src,91);put(other,201);
 void *proxy=dlopen(FUXI_MTL_ICD,RTLD_NOW|RTLD_LOCAL);assert(proxy);
 auto counter=[&](const char *name){auto fn=reinterpret_cast<uint64_t (*)()>(dlsym(proxy,name));assert(fn);return fn();};
 Operation draw{};draw.encoderID=1;draw.texture=dst;draw.samples[0]=src;draw.sampleCount[0]=1;draw.samplers[0]=sam;draw.vertices=6;draw.viewport={0,16,16,-16,0,1};draw.scissor={{0,0},{16,16}};
 draw.clear[0]=.2f;draw.clear[1]=.4f;draw.clear[2]=.6f;draw.clear[3]=1;
 // pattern: F fetch, N normal; scissors vary so later draws overlap earlier ones only in part
 auto ops=[&](const char *pattern,uint32_t level){std::vector<Operation> v;for(unsigned i=0;pattern[i];++i){auto op=draw;op.load=i?1:2;op.pipeline=pattern[i]=='F'?fetch:normal;op.samples[0]=i%2?other:src;op.dstLevel=level;
   uint32_t size=16>>level;if(level)op.viewport={0,float(size),float(size),-float(size),0,1};op.scissor={{int32_t(i%3),int32_t((i*5)%4)},{size-i%3-(i%2),size-(i*5)%4}};v.push_back(op);}return v;};
 // barriers the knob must issue when the pattern is one merged pass: each fetch draw preceded (since the last such barrier) by a non-fetch draw
 auto expected=[&](const char *pattern){unsigned n=0;bool unordered=false;for(unsigned i=0;pattern[i];++i){if(pattern[i]=='F'){if(unordered)++n;unordered=false;}else unordered=true;}return n;};
 auto pixels=[&](const char *label,uint32_t level){Operation get{};get.texture=dst;get.dst=read;get.srcLevel=level;b.submit({get});printf("PIXELS %s ",label);for(size_t i=0;i<dst->levelWidth(level)*dst->levelHeight(level)*4;++i)printf("%02x",((uint8_t *)read->contents)[i]);puts("");};
 auto check=[&](const char *label,const char *pattern,uint32_t level,unsigned flights){
  auto issued=b.fetchSplitBarriers,inPass=counter("render_merge_pass_barriers"),passes=counter("render_merge_passes");auto v=ops(pattern,level);
  if(flights==1)b.submit(v);else{std::function<void()> first,second;b.submit(v,{},&first);b.submit(v,{},&second);first();second();}
  pixels(label,level);issued=b.fetchSplitBarriers-issued;inPass=counter("render_merge_pass_barriers")-inPass;passes=counter("render_merge_passes")-passes;
  unsigned want=on && merge?expected(pattern)*flights:0;
  printf("FETCHSPLIT %s on=%d merge=%d passes=%llu expected=%u issued=%llu in_pass=%llu\n",label,int(on),int(merge),(unsigned long long)passes,want,(unsigned long long)issued,(unsigned long long)inPass);
  assert(issued==inPass && (!merge || passes==flights) && issued==want);
 };
 check("interleaved","NFFNNFNFFNFNNNF",0,1);   // CA: content draws over a backdrop, programmable blend modes in between
 check("fetch-first","FFFNNN",0,1);            // no barrier: no non-fetch write precedes a fetch draw
 check("normal-then-fetch","NNNNF",0,1);
 check("all-fetch","FFFFF",0,1);
 check("all-normal","NNNNN",0,1);              // a fetch-free pass (FUXI_MTL_NOFETCH_RP picks the no-input-attachment variant)
 check("mip-view","FNFNNF",1,1);               // the fetch reads the current mip's attachment view
 check("two-flights","NFNFFN",0,2);
 b.destroy(fetch);
 puts("PASS: fetch split pixels and in-pass barrier counts");
}
