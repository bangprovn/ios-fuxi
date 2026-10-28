/* Authored sampled textures through production repitch, snapshots and retirement. */
#include "Backend.hpp"
#include <cassert>
#include <cstdio>
using namespace fuxi;
static void aliasReadback(Backend &b){
 auto *buffer=b.allocate(65536);auto *t=b.linearTexture(buffer,80,5,16,8,0,64);assert(!t->shadow && t->borrowedMemory);
 for(unsigned load:{2u,1u}){Operation clear{};clear.texture=t;clear.clearOnly=true;clear.load=load;clear.clear[0]=.25f;clear.clear[1]=.5f;clear.clear[2]=.75f;clear.clear[3]=1;
  std::function<void()> retire;b.submit({clear},{},systemTranslationPolicy().overlap?&retire:nullptr);if(retire)retire();
  auto *bytes=(uint8_t *)buffer->contents;for(unsigned i=0;i<16*8;++i)assert(bytes[i*4]==191 && bytes[i*4+1]==128 && bytes[i*4+2]==64 && bytes[i*4+3]==255);
 }
 b.destroy(t);b.destroy(buffer);
}
// FUXI_MTL_RT_SHADOW: a 1M-pixel linear target renders through an optimal shadow; the linear bytes must match the direct path after every sync point.
static void rtShadowReadback(Backend &b){
 const bool on=getenv("FUXI_MTL_RT_SHADOW") && !strcmp(getenv("FUXI_MTL_RT_SHADOW"),"1"),reuse=on && getenv("FUXI_MTL_RT_SHADOW_REUSE") && !strcmp(getenv("FUXI_MTL_RT_SHADOW_REUSE"),"1");const uint32_t n=1024;
 auto *buffer=b.allocate(n*n*4),*other=b.allocate(n*n*4);auto *t=b.linearTexture(buffer,80,5,n,n,0,n*4),*t2=b.linearTexture(other,80,5,n,n,0,n*4);assert(t->borrowedMemory && t2->borrowedMemory);
 auto run=[&](std::vector<Operation> ops){std::function<void()> retire;b.submit(std::move(ops),{},systemTranslationPolicy().overlap?&retire:nullptr);if(retire)retire();};
 auto clear=[&](Texture *x,float r,float g,float bl,unsigned load,uint32_t area){Operation c{};c.texture=x;c.clearOnly=true;c.load=load;c.clear[0]=r;c.clear[1]=g;c.clear[2]=bl;c.clear[3]=1;c.areaWidth=c.areaHeight=area;return c;};
 auto px=[&](Buffer *x,uint32_t xx,uint32_t yy){return (const uint8_t *)x->contents+(size_t(yy)*n+xx)*4;};
 run({clear(t,.25f,.5f,.75f,2,0)});
 for(uint32_t y:{0u,511u,1023u})for(uint32_t x:{0u,700u,1023u}){auto *q=px(buffer,x,y);assert(q[0]==191 && q[1]==128 && q[2]==64 && q[3]==255);}
 if(on){assert(b.rtShadowOf(t) && b.rtShadowOf(t)->rtTarget && b.rtShadowOut>=1 && b.rtShadowIn==0 && b.rtShadowInSkipped>=1);}
 // partial clear over untouched linear bytes: the shadow must be seeded from the linear image first
 uint64_t inBefore=b.rtShadowIn;run({clear(t,1.f,0.f,0.f,2,512)});
 for(uint32_t y:{0u,511u,512u,1023u})for(uint32_t x:{0u,511u,512u,1023u}){auto *q=px(buffer,x,y);bool inside=x<512 && y<512;assert(inside?(q[0]==0 && q[1]==0 && q[2]==255):(q[0]==191 && q[1]==128 && q[2]==64));}
 if(on)assert(b.rtShadowIn==inBefore+(reuse?0:1)); // with reuse the previous submit left linear == shadow, so no copy-in
 // a copy reading the target in the same submit as the render that wrote it must see the new bytes (sync before the copy)
 uint64_t syncs=b.rtShadowSyncs,redirects=b.rtShadowRedirects;Operation copy{};copy.texture=t2;copy.sourceTexture=t;
 run({clear(t,0.f,1.f,0.f,2,0),copy});
 for(uint32_t y:{0u,1023u})for(uint32_t x:{0u,1023u}){auto *q=px(other,x,y);assert(q[0]==0 && q[1]==255 && q[2]==0);q=px(buffer,x,y);assert(q[0]==0 && q[1]==255 && q[2]==0);}
 if(on)assert(b.rtShadowSyncs==syncs && b.rtShadowRedirects==redirects+1); // the copy reads the shadow directly: no sync-out
 // a write to the linear image between two renders makes the shadow stale: the second render must reload it
 inBefore=b.rtShadowIn;Operation over{};over.texture=t;over.sourceTexture=t2;
 run({clear(t2,0.f,0.f,1.f,2,0),over,clear(t,1.f,1.f,1.f,2,512)});
 for(uint32_t y:{0u,511u,512u,1023u})for(uint32_t x:{0u,511u,512u,1023u}){auto *q=px(buffer,x,y);bool inside=x<512 && y<512;assert(inside?(q[0]==255 && q[1]==255 && q[2]==255):(q[0]==255 && q[1]==0 && q[2]==0));}
 if(on)assert(b.rtShadowIn==inBefore+1);
 // CA recreates the drawable texture around the same memory: the shadow is keyed by that memory and must survive
 if(on){uint64_t created=b.rtShadowCreated;b.destroy(t);t=b.linearTexture(buffer,80,5,n,n,0,n*4);run({clear(t,1.f,0.f,0.f,2,512)});assert(b.rtShadowCreated==created);for(uint32_t y:{0u,1023u})for(uint32_t x:{0u,1023u}){auto *q=px(buffer,x,y);bool inside=x<512 && y<512;assert(inside?(q[0]==0 && q[1]==0 && q[2]==255):(q[0]==255 && q[1]==0 && q[2]==0));}}
 b.destroy(t);b.destroy(t2);b.destroy(buffer);b.destroy(other);
}
// FUXI_MTL_RT_RENAME: an offscreen texture overwritten in turn between main-target passes is renamed and hoisted; the main-target bytes must be identical either way,
// and an offscreen pass that samples the main target (directly or through a copy) must never move above the pass that wrote it.
static void renameReplay(Backend &b){
 const bool on=getenv("FUXI_MTL_RT_RENAME") && !strcmp(getenv("FUXI_MTL_RT_RENAME"),"1");
 CompileOptions options;options.format=30;auto *pipeline=b.compilePipeline(nullptr,"control",nullptr,"control",options);auto *sampler=b.allocateSampler(false,false);
 auto *main=b.allocateTexture(30,5,1600,1400),*x=b.allocateTexture(30,5,16,16),*y=b.allocateTexture(30,5,16,16),*s1=b.allocateTexture(30,5,16,16),*s2=b.allocateTexture(30,5,16,16);
 auto *out=b.allocate(16*16*2);uint64_t encoder=0;
 auto clear=[&](Texture *t,float r,float g,float bl){Operation c{};c.texture=t;c.clearOnly=true;c.load=2;c.clear[0]=r;c.clear[1]=g;c.clear[2]=bl;c.clear[3]=1;return c;};
 auto draw=[&](Texture *target,Texture *src,uint32_t load,int32_t x0,uint32_t w){Operation d{};d.texture=target;d.pipeline=pipeline;d.samples[0]=src;d.samplers[0]=sampler;d.viewport={0,16,16,-16,0,1};d.scissor={{x0,0},{w,16}};d.load=load;d.encoderID=++encoder;return d;};
 FrameSample trace;auto run=[&](std::vector<Operation> ops){Operation read{};read.texture=main;read.dst=out;read.to=0;read.width=16;read.height=16;ops.push_back(read);std::function<void()> retire;trace=FrameSample{};{FrameTraceScope scope(&trace);b.submit(std::move(ops),{},systemTranslationPolicy().overlap?&retire:nullptr);}if(retire)retire();};
 const uint8_t A[2]={0,255},B[2]={255,0},Z[2]={0,0};
 auto px=[&](unsigned xx,unsigned yy){return (const uint8_t *)out->contents+(yy*16+xx)*2;};
 auto expect=[&](unsigned xx,unsigned yy,const uint8_t *c){assert(!memcmp(px(xx,yy),c,2));};
 // 1) main, off, main, off(same texture again), main ... twelve main passes split by eleven offscreen passes, every pass in its own encoder
 {uint64_t renamed=b.rtRenamed,hoisted=b.rtHoisted,before=b.rtMainBefore,after=b.rtMainAfter;
  std::vector<Operation> ops{clear(s1,0.f,1.f,0.f),clear(s2,1.f,0.f,0.f)};
  for(unsigned k=0;k<12;++k){if(k)ops.push_back(draw(x,k%2?s1:s2,2,0,16));ops.push_back(draw(main,k?x:s1,k?1:2,int32_t(k),1));}
  run(ops);printf("RENAME_REPLAY encoded render passes=%llu draws=%llu merge=%d perf=%u rename=%d\n",(unsigned long long)trace.passes,(unsigned long long)trace.draws,int(systemTranslationPolicy().merge),systemTranslationPolicy().perf,int(on));
  if(systemTranslationPolicy().merge && (systemTranslationPolicy().perf&2))assert(trace.passes==(on?12u:23u)); // twelve main-target passes and eleven offscreen ones; renaming + hoisting leaves one main pass
  for(unsigned k=0;k<12;++k)for(unsigned yy:{0u,15u})expect(k,yy,k==0?A:(k%2?A:B));
  expect(12,0,Z);expect(15,15,Z);
  if(on){printf("RENAME_REPLAY main_runs %llu -> %llu renamed=%llu hoisted=%llu\n",(unsigned long long)(b.rtMainBefore-before),(unsigned long long)(b.rtMainAfter-after),(unsigned long long)(b.rtRenamed-renamed),(unsigned long long)(b.rtHoisted-hoisted));
   assert(b.rtMainBefore-before==12 && b.rtMainAfter-after==1 && b.rtRenamed-renamed==10 && b.rtHoisted>hoisted);}
 }
 // 2) an offscreen pass sampling the main target directly stays below the pass that wrote it
 {uint64_t before=b.rtMainBefore;(void)before;
  std::vector<Operation> ops{clear(s1,0.f,1.f,0.f),clear(s2,1.f,0.f,0.f)};
  ops.push_back(draw(main,s1,2,0,8));ops.push_back(draw(x,main,2,0,16));ops.push_back(draw(main,x,1,0,1));ops.push_back(draw(x,s2,2,0,16));ops.push_back(draw(main,x,1,8,1));
  run(ops);
  expect(0,0,A);expect(0,1,Z);expect(1,0,A);expect(7,15,A);expect(8,0,B);expect(9,0,Z);
 }
 // 3) an offscreen pass sampling a copy of the main target stays below the copy
 {std::vector<Operation> ops{clear(s1,0.f,1.f,0.f),clear(s2,1.f,0.f,0.f)};
  ops.push_back(draw(main,s1,2,0,8));
  Operation copy{};copy.texture=y;copy.sourceTexture=main;copy.width=16;copy.height=16;ops.push_back(copy);
  ops.push_back(draw(x,y,2,0,16));ops.push_back(draw(main,x,1,0,16));ops.push_back(draw(x,s2,2,0,16));ops.push_back(draw(main,x,1,15,1));
  run(ops);
  expect(0,0,A);expect(7,15,A);expect(8,0,Z);expect(14,3,Z);expect(15,0,B);expect(15,15,B);
 }
 {Pipeline pl{};Operation op{};op.texture=main;op.pipeline=&pl;op.encoderID=1;Descriptor d{};d.type=VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;pl.descriptors.push_back(d);
  assert(Backend::barrierWhy(op,false)!=nullptr && Backend::barrierWhy(op,true)==nullptr); // storage-lowered buffer arguments that the shader only reads are not hazards
  pl.fetchMask=1;assert(Backend::barrierWhy(op,false)!=nullptr && Backend::barrierWhy(op,true)==nullptr); // fb fetch depends only on the pass's own target
  pl.writesStorage=true;assert(Backend::barrierWhy(op,true)!=nullptr);}
 if(on){ // budget scenario: the pool must give way to CA allocations instead of failing them, and optional allocations must stay below the headroom
  assert(b.renamePoolBytes()>0 || b.renamePool.empty());
  std::vector<Texture *> keep;uint64_t sheds=b.optionalShedCount();bool failed=false;
  for(int i=0;i<12 && !failed;++i){try{keep.push_back(b.allocateTexture(30,5,4096,4096));}catch(const std::exception &){failed=true;}}
  printf("RENAME_BUDGET allocated=%zu textures failed=%d pool_images=%zu sheds=%llu used_mb=%zu\n",keep.size(),int(failed),b.renamePool.size(),(unsigned long long)(b.optionalShedCount()-sheds),b.resourceBytes()>>20);
  assert(keep.size()>=6 && b.renamePool.empty()); // 6x32 MB fits only if the pool (and its pooled memory) was shed before refusing a CA texture
  for(auto *t:keep)b.destroy(t);b.trimMemory();
 }
 b.destroy(pipeline);b.dropPipelineCacheForTest();b.destroy(sampler);b.destroy(main);b.destroy(x);b.destroy(y);b.destroy(s1);b.destroy(s2);b.destroy(out);
}
int main(){
 Backend b;ShadowPool pool;auto baseline=b.resourceBytes();aliasReadback(b);rtShadowReadback(b);renameReplay(b);
 auto *targetBuffer=b.allocate(65536);auto *target=b.linearTexture(targetBuffer,80,5,16,16,0,64);assert(!target->shadow && target->borrowedMemory);auto *out=b.allocate(25*16*16*4);auto *sampler=b.allocateSampler(false,false);
 CompileOptions options;auto *pipeline=b.compilePipeline(nullptr,"control",nullptr,"control",options);
 struct Source {Buffer *buffer;Texture *texture;std::vector<uint8_t> cpu;};std::vector<Source> sources;
 // Four supplied mismatch shapes, repeated to 25 allocations; allocation sizes are synthetic.
 const uint32_t shapes[][4]={{621,358,768,1},{230,186,480,30},{138,138,256,1},{635,147,768,1}};
 uint64_t before=0,after=0,repitched=0;
 for(unsigned i=0;i<25;++i){auto &d=shapes[i%4];size_t length=638137+(i==24?19:0);auto *buffer=b.allocate(length);auto *texture=b.linearTexture(buffer,d[3],1,d[0],d[1],256,d[2]);assert(texture->shadow);
  sources.push_back({buffer,texture,std::vector<uint8_t>(length)});before+=length;ShadowRanges r(length);r.texture(256,d[2],d[0],d[1],b.texelBytes(d[3]));assert(!r.isFull());after+=r.bytes();repitched+=uint64_t(d[0])*d[1]*b.texelBytes(d[3]);
 }
 assert(before==15953444 && after==3346977 && repitched*5==13719600);
 auto run=[&](bool full,unsigned frame,bool fallback,bool repeat=false){
  ShadowSnapshot snapshot;uint64_t bytes=0;std::vector<Operation> ops;
  for(unsigned i=0;i<sources.size();++i){auto &s=sources[i];auto *t=s.texture;auto pixel=b.texelBytes(t->format);std::fill(s.cpu.begin(),s.cpu.end(),0xa7);if(!repeat)memset(s.buffer->contents,3,s.buffer->length);
   for(unsigned y=0;y<t->height;++y)for(unsigned x=0;x<t->width;++x)for(unsigned c=0;c<pixel;++c)s.cpu[256+y*t->backingRow+x*pixel+c]=uint8_t(i*13+frame*47+x*3+y*5+c*61);
   ShadowRanges r(s.buffer->length);r.texture(256,t->backingRow,t->width,t->height,pixel);if(full || fallback)r.full(fallback?ShadowFullReason::useResource:ShadowFullReason::other);bytes+=r.bytes();snapshot.add(s.buffer->contents,s.cpu.data(),r);
   Operation draw{};draw.texture=target;draw.pipeline=pipeline;draw.samples[0]=t;draw.samplers[0]=sampler;draw.viewport={0,16,16,-16,0,1};draw.scissor={{0,0},{16,16}};
   // Five draws per source model ~125 CA draws. Distinct encoder IDs prevent cross-pass merging.
   draw.encoderID=i+1;for(unsigned j=0;j<5;++j){draw.load=j?1:2;ops.push_back(draw);}
   Operation read{};read.texture=target;read.dst=out;read.to=i*16*16*4;ops.push_back(read);
  }
  snapshot.capture(pool);assert(snapshot.bytes()==bytes);for(auto &s:sources)std::fill(s.cpu.begin(),s.cpu.end(),255);
  FrameSample trace;std::function<void()> retire;uint64_t copied=0;
  {FrameTraceScope scope(&trace);b.submit(ops,{},systemTranslationPolicy().overlap?&retire:nullptr,[&]{copied=snapshot.apply(pool);});}if(retire)retire();
  // Check every source so equality cannot hide an earlier texture's corruption.
  auto *p=(uint8_t *)out->contents;for(size_t i=0;i<sources.size();++i){auto *t=sources[i].texture;
   for(unsigned y=0;y<16;++y)for(unsigned x=0;x<16;++x){unsigned sx=x*(t->width-1)/15,sy=y*(t->height-1)/15;uint8_t r=uint8_t(i*13+frame*47+sx*3+sy*5),g=t->format==30?uint8_t(r+61):0;size_t at=i*16*16*4+(y*16+x)*4;assert(p[at]==0 && p[at+1]==g && p[at+2]==r && p[at+3]==255);}
   assert(((uint8_t *)sources[i].buffer->contents)[0]==(full || fallback?0xa7:3));
  }
  assert(!memcmp(targetBuffer->contents,p+out->length-16*16*4,16*16*4));
  // Unchanged-backing skip: the first run of a frame (full) has new bytes and copies each texture once; later runs of that frame copy nothing, with identical pixels.
  bool hashOn=systemTranslationPolicy().shadowHash;
  assert(copied==bytes && trace.textureRepitchedBytes==(hashOn?(full?repitched:0):repitched*5));
  assert(hashOn?(full || (trace.shadowHashSkipped>0 && !trace.shadowHashCopied)):(!trace.shadowHashSkipped && !trace.shadowHashCopied));return std::vector<uint8_t>(p,p+out->length);
 };
 for(unsigned frame=0;frame<3;++frame){auto reference=run(true,frame,false);assert(run(false,frame,false)==reference);assert(run(false,frame,true)==reference);assert(run(false,frame,false)==reference);assert(run(false,frame,false,true)==reference);}
 printf("PASS: 25 linear textures/125 draws, CPU writes between frames, frozen post-commit bytes, sampled pitch mismatch full/bounded/fallback pixels; per-stage CPU bytes %llu -> %llu; GPU repitch bytes unchanged %llu (%s)\n",(unsigned long long)before,(unsigned long long)after,(unsigned long long)(repitched*5),systemTranslationPolicy().overlap?"overlap":"synchronous");
 for(auto &s:sources){b.destroy(s.texture);b.destroy(s.buffer);}b.destroy(pipeline);b.dropPipelineCacheForTest();b.destroy(sampler);b.destroy(out);b.destroy(target);b.destroy(targetBuffer);b.trimMemory();assert(b.resourceBytes()==baseline);
}
