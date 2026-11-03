/* FUXI_MTL_FETCH_SPLIT pixel controls: CA-shaped passes that mix framebuffer-fetch draws (programmable blend with the destination) with ordinary
   SRC_ALPHA/ONE_MINUS_SRC_ALPHA draws. The test harness compares PIXELS lines with the knob off and on; FETCHSPLIT lines report the expected and
   issued in-pass barriers (a fetch draw that follows a non-fetch draw of the same merged pass) and the proxy's count of in-pass barriers.
   With FUXI_MTL_ORDER_FETCH_ONLY=1 and the knob off, such a fetch draw instead begins a new pass (refused=), and in every mode the proxy finds
   no fetch draw that reads an unflagged write of its own pass without a barrier (hazards=0). */
#include "Backend.hpp"
#include <cassert>
#include <cstdio>
int main(){
 setenv("FUXI_MTL_SHADOW_HASH","0",1);
 using namespace fuxi;Backend b;const bool merge=systemTranslationPolicy().merge,on=systemTranslationPolicy().fetchSplit;
 // ordinary pipelines lack the rasterization-order flag and no barrier makes their writes visible: a fetch draw after one starts a pass
 const bool refuse=merge && systemTranslationPolicy().orderFetchOnly && !on;
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
 // passes of one encoder's pattern when merged: one, plus one per refused fetch draw (a fetch draw after a non-fetch draw of the current pass)
 auto runs=[&](const char *pattern){unsigned n=1;bool unordered=false;for(unsigned i=0;pattern[i];++i){if(pattern[i]=='F'){if(unordered && refuse){++n;unordered=false;}}else unordered=true;}return n;};
 auto pixels=[&](const char *label,Texture *t,uint32_t level){Operation get{};get.texture=t;get.dst=read;get.srcLevel=level;b.submit({get});printf("PIXELS %s ",label);for(size_t i=0;i<t->levelWidth(level)*t->levelHeight(level)*4;++i)printf("%02x",((uint8_t *)read->contents)[i]);puts("");};
 struct Counts {uint64_t issued,inPass,passes,hazards,refused;};
 auto counts=[&]{return Counts{b.fetchSplitBarriers,counter("render_merge_pass_barriers"),counter("render_merge_passes"),counter("render_merge_fetch_hazards"),b.fetchMergeSplits};};
 auto report=[&](const char *label,const Counts &before,unsigned want,unsigned wantPasses,unsigned wantRefused){auto now=counts();
  auto issued=now.issued-before.issued,inPass=now.inPass-before.inPass,passes=now.passes-before.passes,hazards=now.hazards-before.hazards,refused=now.refused-before.refused;
  printf("FETCHSPLIT %s on=%d merge=%d passes=%llu expected=%u issued=%llu in_pass=%llu hazards=%llu refused=%llu\n",label,int(on),int(merge),(unsigned long long)passes,want,(unsigned long long)issued,(unsigned long long)inPass,(unsigned long long)hazards,(unsigned long long)refused);fflush(stdout);
  assert(issued==inPass && issued==want && passes==wantPasses && !hazards && refused==wantRefused);
 };
 auto check=[&](const char *label,const char *pattern,uint32_t level,unsigned flights){
  auto before=counts();auto v=ops(pattern,level);
  if(flights==1)b.submit(v);else{std::function<void()> first,second;b.submit(v,{},&first);b.submit(v,{},&second);first();second();}
  unsigned want=on && merge?expected(pattern)*flights:0,wantPasses=(merge?runs(pattern):unsigned(strlen(pattern)))*flights,wantRefused=merge?(runs(pattern)-1)*flights:0;
  report(label,before,want,wantPasses,wantRefused);pixels(label,dst,level);
 };
 check("interleaved","NFFNNFNFFNFNNNF",0,1);   // CA: content draws over a backdrop, programmable blend modes in between
 check("fetch-first","FFFNNN",0,1);            // no barrier: no non-fetch write precedes a fetch draw
 check("normal-then-fetch","NNNNF",0,1);
 check("all-fetch","FFFFF",0,1);
 check("all-normal","NNNNN",0,1);              // a fetch-free pass (FUXI_MTL_NOFETCH_RP picks the no-input-attachment variant)
 check("mip-view","FNFNNF",1,1);               // the fetch reads the current mip's attachment view
 check("two-flights","NFNFFN",0,2);
 // CoreAnimation's backdrop material in one submit (Control Center, Today, App Library): the content under the material (encoder 1), the
 // backdrop captured by a draw that samples it (2) and a copy of it (blit), a blur pass that samples the capture (3), then the material in the
 // same target (4): the blurred backdrop, a vibrancy fetch draw that blends with it, a glyph, two more fetch draws. Encoders never merge with
 // each other here; inside encoder 4 the first fetch draw reads the backdrop draw's write and the third reads the glyph's.
 {auto *capture=b.allocateTexture(80,5,16,16),*copied=b.allocateTexture(80,5,16,16),*blur=b.allocateTexture(80,5,8,8);
  std::vector<Operation> v;auto add=[&](Operation op,uint64_t encoder,bool firstInEncoder){op.encoderID=encoder;op.load=firstInEncoder?2:1;v.push_back(op);};
  auto content=ops("NNN",0);for(unsigned i=0;i<content.size();++i)add(content[i],1,!i);
  auto grab=draw;grab.pipeline=normal;grab.texture=capture;grab.samples[0]=dst;add(grab,2,true);
  Operation blit{};blit.texture=copied;blit.sourceTexture=dst;v.push_back(blit);
  auto down=draw;down.pipeline=normal;down.texture=blur;down.samples[0]=capture;down.viewport={0,8,8,-8,0,1};down.scissor={{0,0},{8,8}};add(down,3,true);
  auto material=ops("NFNFF",0);material[0].samples[0]=blur;material[2].samples[0]=copied;for(unsigned i=0;i<material.size();++i)add(material[i],4,false);
  auto before=counts();b.submit(v);
  // 3 + 1 + 1 passes, the blit, then encoder 4: one pass, or one per refused fetch draw
  unsigned want=on && merge?expected("NFNFF"):0,material4=merge?runs("NFNFF"):5u,wantPasses=(merge?1u:3u)+2+material4,wantRefused=merge?runs("NFNFF")-1:0;
  report("ca-backdrop",before,want,wantPasses,wantRefused);pixels("ca-backdrop",dst,0);
  b.destroy(capture);b.destroy(copied);b.destroy(blur);}
 b.destroy(fetch);
 puts("PASS: fetch split pixels, in-pass barrier counts, fetch merge refusals and no unordered fetch reads");
}
