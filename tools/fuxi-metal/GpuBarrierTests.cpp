/* CA-like ordering and boundary costs on the production backend, authored GLSL. */
#include "Backend.hpp"
#include <cassert>
#include <cstdio>
int main(){
 setenv("FUXI_MTL_SHADOW_HASH","0",1); // this test counts the copies of every refresh; the unchanged-backing skip has its own test
 using namespace fuxi;Backend b;bool merge=systemTranslationPolicy().merge,perf=systemTranslationPolicy().perf&4,cross=systemTranslationPolicy().perf&2;
 auto *target=b.allocateTexture(80,5,16,16),*backdrop=b.allocateTexture(80,5,16,16),*blur=b.allocateTexture(80,5,16,16);
 Texture *layers[4];for(auto &t:layers)t=b.allocateTexture(80,5,16,16);
 auto *bytes=b.allocate(1024),*read=b.allocate(1024);auto *p=b.compilePipeline(nullptr,"control",nullptr,"control");auto *sam=b.allocateSampler(false,false,1);
 void *proxy=dlopen(FUXI_MTL_ICD,RTLD_NOW|RTLD_LOCAL);assert(proxy);
 auto counter=[&](const char *name){auto fn=reinterpret_cast<uint64_t (*)()>(dlsym(proxy,name));assert(fn);return fn();};
 auto put=[&](Texture *t,uint8_t value,uint32_t level=0){memset(bytes->contents,value,1024);for(unsigned i=3;i<1024;i+=4)((uint8_t *)bytes->contents)[i]=41;Operation op{};op.src=bytes;op.texture=t;op.upload=true;op.dstLevel=level;b.submit({op});};
 for(unsigned i=0;i<4;++i)put(layers[i],39+i*47);
 Operation draw{};draw.encoderID=1;draw.pipeline=p;draw.texture=target;draw.samples[0]=layers[0];draw.sampleCount[0]=1;draw.samplers[0]=sam;draw.vertices=6;
 draw.viewport={0,16,16,-16,0,1};draw.scissor={{0,0},{16,16}};draw.clear[0]=.2f;draw.clear[1]=.4f;draw.clear[2]=.6f;draw.clear[3]=1;
 auto run=[&](const char *label,const std::vector<Operation> &ops,unsigned passes){
  auto before=counter("render_merge_barrier_calls"),images=counter("render_merge_image_barriers"),calls=counter("render_merge_image_barrier_calls"),renders=counter("render_merge_passes");
  FrameSample s;{FrameTraceScope scope(&s);b.submit(ops);}assert(s.passes==passes && s.passes==counter("render_merge_passes")-renders && s.imageBarriers==counter("render_merge_image_barriers")-images);
  printf("COUNTS %s images=%llu image_calls=%llu barriers=%llu passes=%llu area=%llu non_draw=%llu\n",label,(unsigned long long)s.imageBarriers,(unsigned long long)(counter("render_merge_image_barrier_calls")-calls),(unsigned long long)(counter("render_merge_barrier_calls")-before),(unsigned long long)s.passes,(unsigned long long)s.mergeSplits[size_t(MergeSplit::area)],(unsigned long long)s.mergeSplits[size_t(MergeSplit::nonDraw)]);
  return s;
 };
 auto pixels=[&](const char *label,Texture *t){Operation get{};get.texture=t;get.dst=read;b.submit({get});printf("PIXELS %s ",label);for(unsigned i=0;i<1024;++i)printf("%02x",((uint8_t *)read->contents)[i]);puts("");};
 auto multi=draw;for(unsigned i=0;i<4;++i){multi.samples[i]=layers[i];multi.sampleCount[i]=1;}
 auto s=run("batch",{multi},1);assert(s.imageBarriers==(perf?1u:5u));pixels("batch",target);
 auto *mips=b.allocateTexture(80,5,16,16,3);for(unsigned l=0;l<3;++l)put(mips,71+l*31,l);auto mipDraw=draw;mipDraw.samples[0]=mips;mipDraw.sampleCount[0]=3;
 s=run("mip-batch",{mipDraw},1);assert(s.imageBarriers==(perf?0u:4u));pixels("mip-batch",target);b.destroy(mips);
 std::vector<Operation> ops;for(unsigned i=0;i<12;++i){auto d=draw;d.encoderID=i+1;d.load=i?1:2;d.areaWidth=i%2?8:16;d.areaHeight=d.areaWidth;d.scissor.extent={d.areaWidth,d.areaHeight};d.samples[0]=layers[i%4];ops.push_back(d);}
 Operation copy{};copy.sourceTexture=target;copy.texture=backdrop;ops.push_back(copy);
 auto sampled=draw;sampled.texture=blur;sampled.samples[0]=backdrop;sampled.encoderID=20;ops.push_back(sampled);
 for(unsigned i=0;i<12;++i){auto d=draw;d.encoderID=30+i;d.load=1;d.areaWidth=i%2?8:16;d.areaHeight=d.areaWidth;d.scissor.extent={d.areaWidth,d.areaHeight};d.samples[0]=i%2?blur:layers[i%4];ops.push_back(d);}
 s=run("ca",ops,merge && perf && cross?3:25);if(merge)assert(s.mergeSplits[size_t(MergeSplit::nonDraw)]==2);pixels("ca",target);pixels("backdrop",backdrop);pixels("sampled-blur",blur);
 // LOAD may expand the union; a partial CLEAR/DontCare load must never expand.
 put(target,173);auto small=draw,large=draw;small.areaWidth=8;small.areaHeight=8;small.scissor.extent={8,8};small.load=1;large.load=1;
 run("load-expand",{small,large,small},merge && perf?1:3);pixels("load-expand",target);
 small.load=2;run("clear-expand",{small,large},2);pixels("clear-expand",target);
 small.load=0;run("discard-load-expand",{small,large},2); // DontCare pixels have no comparison contract.
 // The growing rectangle must not clear the upper-right corner of its union.
 put(target,173);small.load=2;large=small;large.areaWidth=4;large.areaHeight=16;large.scissor.extent={4,16};large.load=1;
 run("crossed-clear",{small,large},2);pixels("crossed-clear",target);
 for(unsigned y=0;y<16;++y)for(unsigned x=8;x<16;++x)for(unsigned q=0;q<4;++q)assert(((uint8_t *)read->contents)[(y*16+x)*4+q]==(q==3?41:173));
 small.load=1;large=draw;large.load=1;small.store=0;s=run("partial-discard-store",{large,small},2);assert(!target->isDefined(0));
 // A producer copy and a sample upload remain between their consumers.
 put(target,173);small=draw;small.load=1;copy.texture=layers[1];auto consumer=small;consumer.samples[0]=layers[1];run("producer-copy",{small,copy,consumer},2);pixels("producer-copy",target);
 Operation upload{};upload.src=bytes;upload.texture=layers[1];upload.upload=true;run("sample-upload",{small,upload,consumer},2);pixels("sample-upload",target);
 // Linear-texture shadows: one sample of an unchanged backing per submit under bit 4; a GPU write to the backing forces a fresh copy.
 {auto *sb=b.allocate(4096);auto *sh=b.linearTexture(sb,80,1,16,16,0,64);assert(sh->shadow);memset(sb->contents,77,4096);
  std::vector<Operation> many(6,draw);for(unsigned i=0;i<many.size();++i){many[i].encoderID=i+1;many[i].load=i?1:2;many[i].samples[0]=sh;}
  auto copies=counter("render_merge_copies");bool merged=merge && cross;s=run("shadow-dedupe",many,merged?1:6);pixels("shadow-dedupe",target);
  assert(counter("render_merge_copies")-copies==(merged || perf?1u:6u));assert(s.shadowRefreshSkipped==(perf && !merged?5u:0u));
  // Bit 4 samples in GENERAL: no SHADER_READ_ONLY transition in either direction, one TRANSFER_DST in and one out for the shadow.
  if(perf){for(unsigned k=0;k<4;++k)assert(!s.layoutKinds[2*4+k] && !s.layoutKinds[k*4+2]);assert(s.layoutKinds[1*4+3]+s.layoutKinds[0*4+3]==1 && s.layoutKinds[3*4+1]==1);}
  Operation fill{};fill.dst=sb;fill.size=4096;fill.value=199;auto lo=many[0];lo.load=1;copies=counter("render_merge_copies");
  s=run("shadow-fill",{many[0],fill,lo},2);pixels("shadow-fill",target);assert(counter("render_merge_copies")-copies==2 && s.shadowRefreshSkipped==0);
  s=run("shadow-transitions",{lo,lo},merge?1:2);if(perf)assert(s.imageBarriers<=2);
  b.destroy(sh);b.destroy(sb);}
 // Independent groups hop so equal-attachment groups merge; dependent groups (RAW, WAR) keep their order. Pixels must match with the hop disabled.
 {bool hop=systemTranslationPolicy().reorder && merge && cross;put(target,173);put(blur,91);put(backdrop,55);
  auto fa=draw;fa.load=1;fa.encoderID=50;auto fb=fa;fb.encoderID=51;fb.samples[0]=layers[2];auto fc=fa;fc.encoderID=52;fc.samples[0]=layers[3];
  auto gb=draw;gb.texture=blur;gb.load=2;gb.encoderID=53;gb.samples[0]=layers[1];auto gc=draw;gc.texture=backdrop;gc.load=2;gc.encoderID=54;gc.samples[0]=layers[3];
  run("reorder-independent",{fa,gb,fb,gc,fc},hop?3:5);pixels("reorder-independent",target);pixels("reorder-independent-blur",blur);pixels("reorder-independent-backdrop",backdrop);
  put(target,173);put(blur,91);auto raw=fa;raw.encoderID=55;raw.samples[0]=blur; // reads what gb wrote: must stay after gb
  run("reorder-raw",{fa,gb,raw},hop?3:3);pixels("reorder-raw",target);
  put(target,173);put(blur,91);auto early=fa;early.encoderID=56;early.samples[0]=blur; // reads blur before gb overwrites it (WAR)
  run("reorder-war",{early,gb,fb},hop?2:3);pixels("reorder-war",target);pixels("reorder-war-blur",blur);}
 // Render area = scissor box when every attachment LOADs; pixels outside the box keep their value, and a CLEAR load never shrinks.
 {bool areaOn=systemTranslationPolicy().renderArea;put(target,173);auto sm=draw;sm.load=1;sm.encoderID=60;sm.scissor={{4,4},{6,5}};
  auto s2=run("render-area",{sm},1);assert(s2.renderAreaShrunk==(areaOn?1u:0u) && s2.renderAreaPasses==1 && (!areaOn || s2.renderAreaPixels==30));pixels("render-area",target);
  for(unsigned y=0;y<16;++y)for(unsigned x=0;x<16;++x)if(x<4||x>=10||y<4||y>=9)for(unsigned q=0;q<4;++q)assert(((uint8_t *)read->contents)[(y*16+x)*4+q]==(q==3?41:173));
  auto two=sm;two.encoderID=61;two.scissor={{9,2},{3,3}};two.samples[0]=layers[1];put(target,173);auto s3=run("render-area-union",{sm,two},merge&&cross?1:2);pixels("render-area-union",target);
  assert(!merge || !cross || !areaOn || s3.renderAreaPixels==uint64_t(8)*7);
  sm.load=2;put(target,173);auto s4=run("render-area-clear",{sm},1);assert(!s4.renderAreaShrunk);pixels("render-area-clear",target);}
 // Recording failure restores the layouts and defined bits despite queued batches.
 auto layout=target->layout,sourceLayout=layers[0]->layout;auto defined=target->definedLevels;b.failBeforeQueue=true;bool failed=false;
 try{b.submit({draw,consumer});}catch(...){failed=true;}assert(failed && !b.poisoned && target->layout==layout && layers[0]->layout==sourceLayout && target->definedLevels==defined);
 std::function<void()> first,second;b.submit({draw,consumer},{},&first);auto other=draw;other.texture=backdrop;b.submit({other},{},&second);first();second();pixels("two-flights",target);
 b.destroy(p);b.destroy(sam);b.destroy(bytes);b.destroy(read);b.destroy(target);b.destroy(backdrop);b.destroy(blur);for(auto *t:layers)b.destroy(t);dlclose(proxy);
 puts("PASS: boundary batches, area unions, backdrop ordering, upload hazards, rollback and two flights");
}
