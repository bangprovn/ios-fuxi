/* FUXI_MTL_DESC_REUSE (and FUXI_MTL_BIND_SKIP) A/B on the production backend with ReuseBindBridge on lavapipe. The caller compares every
   PIXELS line with the knobs off; COUNTS lines carry what the proxy recorded and are checked per knob. The scenes destroy and remake the
   texture, sampler, uniform buffer and pipeline a kept set names (lavapipe hands a new object the freed address, so the same handle value
   comes back), keep two flights pending across pool rotations, and mix vertex buffer offsets, index buffers and push values in one pass. */
#include "Backend.hpp"
#include <cassert>
#include <cstdio>
int main(int argc,char **argv){
 using namespace fuxi;Backend b;b.reuseForgetForTest=!(argc>1 && !strcmp(argv[1],"no-forget")); // negative control of the destroy-time rule
 void *proxy=dlopen(FUXI_MTL_ICD,RTLD_NOW|RTLD_LOCAL);assert(proxy);
 auto counter=[&](const char *name){auto fn=reinterpret_cast<uint64_t (*)()>(dlsym(proxy,name));assert(fn);return fn();};
 auto *bytes=b.allocate(1024),*read=b.allocate(1024);
 // A gradient, so nearest and linear sampling of the centre differ.
 auto upload=[&](Texture *t,uint8_t salt){for(unsigned i=0;i<256;++i){auto *px=(uint8_t *)bytes->contents+i*4;px[0]=uint8_t((i%16)*16+salt);px[1]=uint8_t((i/16)*16);px[2]=uint8_t(128+salt);px[3]=255;}
  Operation op{};op.src=bytes;op.texture=t;op.upload=true;b.submit({op});};
 auto *src=b.allocateTexture(80,5,16,16),*other=b.allocateTexture(80,5,16,16),*dst=b.allocateTexture(80,5,16,16);upload(src,0);upload(other,40);
 // Positions: a full quad at 0, the left half at 48. Indices: 0..5 at 0, the first triangle twice at 16.
 auto *verts=b.allocate(256);{float q[12]={-1,-1,1,-1,-1,1,-1,1,1,-1,1,1},h[12]={-1,-1,0,-1,-1,1,-1,1,0,-1,0,1};memcpy(verts->contents,q,48);memcpy((uint8_t *)verts->contents+48,h,48);}
 auto *idx=b.allocate(64);{uint16_t a[6]={0,1,2,3,4,5},c[6]={0,1,2,0,1,2};memcpy(idx->contents,a,12);memcpy((uint8_t *)idx->contents+16,c,12);}
 auto *knobs=b.allocate(4096); // push sources (ReuseBindBridge reads only the binding offsets)
 const unsigned tintSlots=1200;auto fillTints=[&](Buffer *t,unsigned salt){for(unsigned i=0;i<tintSlots;++i){float v[4]={1-float((i+salt)%5)/8,1-float((i+salt)%7)/16,1,1};memcpy((uint8_t *)t->contents+i*256,v,16);}};
 auto *tints=b.allocate(tintSlots*256);fillTints(tints,0);
 auto *p=b.compilePipeline(nullptr,"bind",nullptr,"bind"),*p2=b.compilePipeline(nullptr,"bind",nullptr,"bind2");auto *sam=b.allocateSampler(false,false,1);assert(p!=p2);
 auto draw=[&](Pipeline *pipe,Texture *sample,unsigned color,unsigned shift,unsigned tint,int index=-1,unsigned half=0){Operation op{};op.encoderID=1;op.pipeline=pipe;op.texture=dst;op.samples[0]=sample;op.sampleCount[0]=1;op.samplers[0]=sam;
  op.vertices=6;op.viewport={0,16,16,-16,0,1};op.scissor={{0,0},{16,16}};op.load=1;op.clear[0]=.2f;op.clear[1]=.4f;op.clear[2]=.6f;op.clear[3]=1;
  op.buffers[0][0]={verts,half*48,false};op.buffers[1][1]={knobs,color*16,false};op.buffers[0][2]={knobs,shift*16,false};op.buffers[1][3]={tints,tint*256,false};
  if(index>=0){op.indices=idx;op.indexWidth=2;op.indexOffset=uint64_t(index)*16;}return op;};
 // One CA-shaped encoder: repeated and changing push values, a second pipeline layout with the same values, indexed draws switching index
 // offsets, vertex offsets switching back and forth.
 auto frame=[&]{std::vector<Operation> f{draw(p,src,1,0,0),draw(p,src,1,0,0),draw(p,src,2,0,0),draw(p2,src,2,0,0),draw(p,other,2,1,1,0),draw(p,other,3,1,2,1,1),draw(p,other,3,1,2,0,1),
  draw(p,src,3,1,2,-1,1),draw(p,src,3,1,2,-1,0),draw(p,src,3,1,2,-1,1)};f[0].load=2;return f;};
 auto pixels=[&](const char *label){Operation get{};get.texture=dst;get.dst=read;b.submit({get});uint64_t h=0xcbf29ce484222325ull;for(size_t i=0;i<16*16*4;++i)h=(h^((uint8_t *)read->contents)[i])*0x100000001b3ull;
  printf("PIXELS %s %016llx\n",label,(unsigned long long)h);};
 const char *names[]={"render_merge_push_calls","render_merge_vertex_binds","render_merge_index_binds","render_merge_set_allocations","render_merge_set_binds","render_merge_indexed_draws","render_merge_draws"};
 uint64_t forgotMark=0; // forgotten= counts the kept sets dropped since the previous run, so it includes a scene's destroys
 auto run=[&](const char *label,const std::vector<Operation> &ops,std::function<void()> *retirement=nullptr){uint64_t before[7];for(unsigned i=0;i<7;++i)before[i]=counter(names[i]);
  auto f0=forgotMark;FrameSample s;{FrameTraceScope scope(&s);b.submit(ops,{},retirement);}forgotMark=descReuseForgotten.load();
  printf("COUNTS %s pushes=%llu vertex=%llu index=%llu set_allocs=%llu set_binds=%llu indexed=%llu draws=%llu reused=%llu made=%llu forgotten=%llu\n",label,
   (unsigned long long)(counter(names[0])-before[0]),(unsigned long long)(counter(names[1])-before[1]),(unsigned long long)(counter(names[2])-before[2]),(unsigned long long)(counter(names[3])-before[3]),
   (unsigned long long)(counter(names[4])-before[4]),(unsigned long long)(counter(names[5])-before[5]),(unsigned long long)(counter(names[6])-before[6]),
   (unsigned long long)s.descReuseHits,(unsigned long long)s.descReuseNew,(unsigned long long)(forgotMark-f0));};
 for(unsigned n=1;n<=24;++n){char l[32];snprintf(l,sizeof l,"steady-%u",n);run(l,frame());if(n==1 || n==24)pixels(l);}
 // The texture a kept set names goes, and a new one with other texels may get the same image view handle.
 {auto view=b.sampleViewForTest(other);assert(view);b.destroy(other);other=b.allocateTexture(80,5,16,16);upload(other,90);run("texture-swap",frame());pixels("texture-swap");
  printf("INFO view_handle_back=%d\n",int(b.sampleViewForTest(other)==view));} // 1: only the destroy-time forget keeps the old set out
 // The sampler goes; the new one filters linearly.
 {b.destroy(sam);sam=b.allocateSampler(true,true,1);run("sampler-swap",frame());pixels("sampler-swap");}
 // The uniform buffer goes (with fuxigpunoslab=1 its VkBuffer is destroyed, not pooled) and a new one holds other tints.
 {b.destroy(tints);tints=b.allocate(tintSlots*256);fillTints(tints,3);run("buffer-swap",frame());pixels("buffer-swap");}
 // Both pipelines (and their set layouts) go and are made again.
 {b.destroy(p);b.destroy(p2);b.dropPipelineCacheForTest();p=b.compilePipeline(nullptr,"bind",nullptr,"bind");p2=b.compilePipeline(nullptr,"bind",nullptr,"bind2");run("pipeline-swap",frame());pixels("pipeline-swap");}
 // More distinct sets than both reuse pools hold, recorded while an earlier flight is pending: the second rotation waits for that flight.
 {std::vector<Operation> many;for(unsigned i=0;i<tintSlots;++i)many.push_back(draw(i%2?p:p2,i%3?src:other,i%16,i%5,i,i%4==3?int(i%2):-1,i%2));many[0].load=2;
  auto r0=descReuseRotations.load(),b0=descReuseBlocked.load();std::function<void()> first,second;
  run("flight-first",frame(),&first);run("flight-many",many,&second);first();second();pixels("flight-many");
  run("after-flights",many);pixels("after-flights");run("after-flights-frame",frame());pixels("after-flights-frame");
  printf("ROTATIONS %llu BLOCKED %llu\n",(unsigned long long)(descReuseRotations.load()-r0),(unsigned long long)(descReuseBlocked.load()-b0));}
 // Several render passes in one submit (targets alternate), as with FUXI_MTL_SPLIT_SUBMIT chunks.
 {auto *dst2=b.allocateTexture(80,5,16,16);auto f=frame(),g=frame();for(auto &op:g)op.texture=dst2;std::vector<Operation> mixed;
  for(size_t i=0;i<f.size();++i){mixed.push_back(f[i]);mixed.push_back(g[i]);mixed.back().encoderID=2;}
  for(unsigned n=0;n<3;++n)run("passes",mixed);pixels("passes");
  Operation copy{};copy.sourceTexture=dst2;copy.texture=dst;b.submit({copy});pixels("passes-second-target");b.destroy(dst2);}
 printf("KEPT %llu\n",(unsigned long long)b.keptDescriptorSets());
 b.destroy(p);b.destroy(p2);b.destroy(sam);b.destroy(src);b.destroy(other);b.destroy(dst);b.destroy(bytes);b.destroy(read);b.destroy(verts);b.destroy(idx);b.destroy(knobs);b.destroy(tints);b.close();
 puts("PASS: reuse/bind fixture");
}
