/* Authored shared-buffer coherence and line controls on the production backend. */
#include "fuxi-metal/Backend.hpp"
#include "fuxi-metal/M7Reference.h"
#include <cassert>
#include <fstream>
#include <iterator>
#include <sys/mman.h>
#include <sys/wait.h>
#include <unistd.h>
template<class F> static std::string refuses(F f){try{f();}catch(const std::runtime_error &e){return e.what();}assert(false);return {};}
static void transfers(fuxi::Backend &b){
 // Exact byte accounting below: no slab sub-allocation (charged 0) and no pooled buffers (charged after destroy).
 b.slabsEnabled=false;b.buffersPooled=false;
 auto baseline=b.resourceBytes();auto memory=fuxi::liveMemoryObjects.load();
 VkMemoryRequirements req{};req.alignment=64;req.memoryTypeBits=2;req.size=1024;
 VkSubresourceLayout layout{};layout.offset=8;layout.rowPitch=256;
 auto mismatch=b.linearAliasMismatch(req,layout,0,512,3,480);
 for(const char *part:{"alignment offset=3 required=64","memory type buffer=0 allowedBits=2","size required=1024 available=509","layout.offset Vulkan=8 Metal=0","rowPitch Vulkan=256 Metal=480"})assert(mismatch.find(part)!=std::string::npos);
 req.alignment=1;req.memoryTypeBits=1;req.size=512;layout.offset=0;layout.rowPitch=480;assert(b.linearAliasMismatch(req,layout,0,512,0,480).empty());
 // An enclosing-page import binds at its pointer offset: the alignment test uses memory offset + texture offset.
 req.alignment=64;assert(b.linearAliasMismatch(req,layout,0,1024,0,480,64).empty() && b.linearAliasMismatch(req,layout,0,1024,64,480,128).empty());
 assert(b.linearAliasMismatch(req,layout,0,1024,0,480,8).find("alignment offset=8 required=64")!=std::string::npos);
 assert(b.linearAliasMismatch(req,layout,0,1024,32,480,32).empty());
 // No-copy import: null/empty refuse; with host import, aligned and enclosing-page pointers keep contents==caller pointer.
 {auto attempts=fuxi::HostImportStats::attempts.load();
  assert(!refuses([&]{b.importBuffer(nullptr,64);}).empty() && !refuses([&]{b.importBuffer((void *)0x10000,0);}).empty());
  if(b.supportsHostImport()){
   size_t pg=16384;void *m=mmap(nullptr,4*pg,PROT_READ|PROT_WRITE,MAP_ANON|MAP_PRIVATE,-1,0);assert(m!=MAP_FAILED);memset(m,0x5a,4*pg);
   for(size_t off:{size_t(0),size_t(256),pg-64}){size_t len=off==pg-64?200:pg;
    fuxi::Buffer *imported=nullptr;try{imported=b.importBuffer((uint8_t *)m+off,len);}catch(const std::runtime_error &e){assert(off!=0 && std::string(e.what()).find("no-copy buffer")!=std::string::npos);}
    if(imported){assert(imported->contents==(uint8_t *)m+off && imported->length==len && *(uint8_t *)imported->contents==0x5a);b.destroy(imported);}}
   munmap(m,4*pg);
  }
  assert(fuxi::HostImportStats::attempts.load()>=attempts+2);}
 for(uint32_t format:{1u,30u}){
  const size_t pixel=b.texelBytes(format),offset=3*pixel,row=16*pixel,w=7,h=5;
  // Only the used bytes of the last row exist. Odd binding offsets and padded
  // Metal rows deliberately cannot alias lavapipe's linear image layout.
  auto *buffer=b.allocate(offset+(h-1)*row+w*pixel),*out=b.allocate(256);
  for(size_t i=0;i<buffer->length;++i)((uint8_t *)buffer->contents)[i]=uint8_t(i*17);
  auto *t=b.linearTexture(buffer,format,1,w,h,offset,row);assert(t->shadow && t->charged && t->backingBuffer==buffer && buffer->references==2);
  auto charged=b.resourceBytes();assert(charged>=baseline+buffer->charged+out->charged+t->charged);
  fuxi::Operation read{};read.texture=t;read.dst=out;read.to=4;read.rowBytes=20*pixel;
  auto verify=[&]{memset(out->contents,0xa7,out->length);b.submit({read});for(size_t y=0;y<h;++y){
   assert(!memcmp((uint8_t *)out->contents+4+y*20*pixel,(uint8_t *)buffer->contents+offset+y*row,w*pixel));
   for(size_t x=w*pixel;x<20*pixel;++x)assert(((uint8_t *)out->contents)[4+y*20*pixel+x]==0xa7);
  }assert(((uint8_t *)out->contents)[3]==0xa7);assert(b.resourceBytes()==charged);};
  auto oldLayout=t->layout;auto oldDefined=t->defined;auto levels=t->definedLevels;
  b.failBeforeQueue=true;assert(refuses([&]{b.submit({read});}).find("injected")!=std::string::npos);
  assert(t->layout==oldLayout && t->defined==oldDefined && t->definedLevels==levels && !b.poisoned);verify();
  memset(buffer->contents,0x3b,buffer->length);verify(); // CPU write after creation / earlier completion.
  fuxi::Operation fill{};fill.dst=buffer;fill.size=buffer->length&~size_t(3);fill.value=0x71;
  b.submit({read,fill,read});for(size_t y=0;y<h;++y)assert(!memcmp((uint8_t *)out->contents+4+y*20*pixel,(uint8_t *)buffer->contents+offset+y*row,w*pixel));
  auto upload=read;upload.dst=nullptr;upload.src=out;upload.upload=true;upload.to=0;
  assert(refuses([&]{b.submit({upload});}).find("GPU write refused")!=std::string::npos);
  auto clear=read;clear.clearOnly=true;assert(refuses([&]{b.submit({clear});}).find("GPU write refused")!=std::string::npos);
  assert(refuses([&]{b.linearTexture(buffer,format,5,w,h,offset,row);}).size());
  assert(refuses([&]{b.linearTexture(buffer,format,2,w,h,offset,row);}).size());
  assert(refuses([&]{b.linearTexture(buffer,format,1,w,h,offset,w*pixel-1);}).size());
  assert(refuses([&]{b.linearTexture(buffer,format,1,w,h+1,offset,row);}).size());
  assert(b.resourceBytes()==charged);
  b.destroy(buffer);assert(t->backingBuffer->references==1);verify(); // texture owns backend backing too.
  b.destroy(t);b.destroy(out);b.trimMemory();assert(b.resourceBytes()==baseline && fuxi::liveMemoryObjects==memory);
 }
 // Survey-sized buffers use tight final rows, including strides that differ
 // from lavapipe's image pitch. Compare every texel and preserve the buffer.
 for(auto d:{std::array<uint32_t,5>{1,624,432,768,331632},{1,816,151,1024,154416},{30,230,186,480,89260},{30,23,23,256,5678},{30,32,40,256,10048}}){
  auto *source=b.allocate(d[4]),*out=b.allocate(size_t(d[1])*d[2]*b.texelBytes(d[0])+32);
  for(size_t i=0;i<source->length;++i)((uint8_t *)source->contents)[i]=uint8_t(i*13);
  std::vector<uint8_t> original((uint8_t *)source->contents,(uint8_t *)source->contents+source->length);
  auto *t=b.linearTexture(source,d[0],1,d[1],d[2],0,d[3]);fuxi::Operation read{};read.texture=t;read.dst=out;read.to=16;
  memset(out->contents,0xa7,out->length);b.submit({read});size_t tight=d[1]*b.texelBytes(d[0]);
  for(unsigned y=0;y<d[2];++y)assert(!memcmp((uint8_t *)out->contents+16+y*tight,original.data()+y*d[3],tight));
  for(unsigned i=0;i<16;++i)assert(((uint8_t *)out->contents)[i]==0xa7 && ((uint8_t *)out->contents)[out->length-1-i]==0xa7);
  assert(!memcmp(source->contents,original.data(),original.size()));b.destroy(t);b.destroy(source);b.destroy(out);
 }
 // Exact alias still borrows its allocation and retains the backing buffer.
 auto *buffer=b.allocate(65536);auto *alias=b.linearTexture(buffer,80,1,16,8,0,64);
 assert(!alias->shadow && alias->borrowedMemory && !alias->charged);b.destroy(buffer);b.destroy(alias);
 b.trimMemory();assert(b.resourceBytes()==baseline && fuxi::liveMemoryObjects==memory);
 // Force budget exhaustion only for the shadow, then prove rollback of the
 // attempted image and that the caller's existing buffer remains usable. The wired ceiling is set to the own target, so the
 // shadow (a required allocation) has no overdraft to fall back on either.
 b.setLimitsForTest(fuxi::ResourceLimits::budget,fuxi::ResourceLimits::importBudget,fuxi::ResourceLimits::budget);
 auto *small=b.allocate(128);std::vector<fuxi::Buffer *> ballast;
 while(b.resourceBytes()<fuxi::ResourceLimits::budget){size_t left=fuxi::ResourceLimits::budget-b.resourceBytes();ballast.push_back(b.allocate(std::min(left,fuxi::ResourceLimits::single)));}
 auto charged=b.resourceBytes();auto objects=fuxi::liveMemoryObjects.load();
 assert(refuses([&]{b.linearTexture(small,1,1,7,5,3,16);})=="resource byte budget");
 assert(b.resourceBytes()==charged && fuxi::liveMemoryObjects==objects && small->references==1 && !b.poisoned);
 for(auto *v:ballast)b.destroy(v);b.destroy(small);assert(b.resourceBytes()==baseline);
}
static void lines(fuxi::Backend &b,const char *path){
 std::ifstream in(path,std::ios::binary);std::vector<uint8_t> air{std::istreambuf_iterator<char>(in),{}};
 auto &tr=fuxi::Translator::get();char error[512]{};void *lib=tr.open(air.data(),air.size(),error,sizeof(error));assert(lib);
 fuxi::CompileOptions options{};options.attributes[0]={29,0,0};options.attributes[1]={29,8,0};options.attributes[2]={9,16,0};options.strides[0]=20;
 options.constantCount[1]=1;options.constants[1][0].type=53;options.constants[1][0].size=1;options.constants[1][0].bytes[0]=1;
 auto pipelineBefore=fuxi::frameCounts.pipelines;
 auto *p=b.compilePipeline(lib,"fuxi_layer_vertex",lib,"fuxi_layer_fragment",options);
 if(fuxi::systemTranslationPolicy().frameTrace)assert(fuxi::frameCounts.pipelines>pipelineBefore);assert(!p->linePipelines[0] && !p->linePipelines[1]);
 auto *vertices=b.allocate(sizeof(fuxiM7Vertices)),*ones=b.allocate(16),*indices=b.allocate(16),*out=b.allocate(16*16*4);
 memcpy(vertices->contents,fuxiM7Vertices,sizeof(fuxiM7Vertices));for(unsigned c=0;c<4;++c)((float *)ones->contents)[c]=1;
 // A single horizontal line through the target. UVs and vertex color are white.
 auto *v=(float *)vertices->contents;v[0]=-0.8f;v[1]=0;v[5]=0.8f;v[6]=0;for(unsigned i=0;i<2;++i)memset((uint8_t *)vertices->contents+i*20+16,255,4);
 auto *backing=b.allocate(128);memset(backing->contents,255,128);auto *sample=b.linearTexture(backing,1,1,7,5,3,16);assert(sample->shadow);
 auto *target=b.allocateTexture(80,5,16,16);auto *sam=b.allocateSampler(false,false);
 fuxi::Operation draw{};draw.texture=target;draw.pipeline=p;draw.vertices=2;draw.topology=fuxi::Topology::Line;draw.samples[0]=sample;draw.samplers[0]=sam;draw.buffers[0][0]={vertices,0,false};draw.buffers[0][2]={ones,0,true};draw.buffers[1][1]={ones,0,true};draw.viewport={0,16,16,-16,0,1};draw.scissor={{0,0},{16,16}};
 fuxi::Operation read{};read.texture=target;read.dst=out;
 auto render=[&](fuxi::Operation op){
  fuxi::FrameSample trace;auto descriptors=fuxi::frameCounts.descriptors;
  {fuxi::FrameTraceScope scope(fuxi::systemTranslationPolicy().frameTrace?&trace:nullptr);b.submit({op,read});}
  if(fuxi::systemTranslationPolicy().frameTrace)assert(trace.draws==1 && trace.passes==1 && trace.backendSubmit && trace.gpuWait && fuxi::frameCounts.descriptors==descriptors+1);
  auto *bytes=(uint8_t *)out->contents;return std::vector<uint8_t>(bytes,bytes+out->length);};
 auto expected=render(draw);assert(p->linePipelines[0] && !p->linePipelines[1]);size_t lit=0;for(size_t i=0;i<expected.size();i+=4)if(expected[i+3])++lit;assert(lit>=10 && lit<=16);
 auto list=p->linePipelines[0];auto odd=draw;odd.vertices=3;assert(render(odd)==expected && p->linePipelines[0]==list);
 // The unused trailing index must neither be fetched nor bounds checked.
 uint16_t indexData[]={0,1,65534};memcpy(indices->contents,indexData,sizeof(indexData));odd.indices=indices;assert(render(odd)==expected);
 auto strip=draw;strip.topology=fuxi::Topology::LineStrip;assert(render(strip)==expected);auto cached=p->linePipelines[1];assert(cached);assert(render(strip)==expected && p->linePipelines[1]==cached);
 strip.indices=indices;assert(render(strip)==expected);
 const uint16_t restart[]={0,1,65535,0,1};memcpy(indices->contents,restart,sizeof(restart));strip.vertices=5;assert(render(strip)==expected);
 // Incomplete lines do not create work or modify an existing attachment.
 auto empty=draw;empty.vertices=1;auto submits=fuxi::Backend::submits.load();b.submit({empty});empty.topology=fuxi::Topology::LineStrip;b.submit({empty});assert(fuxi::Backend::submits==submits);
 // CPU buffer changes before the next commit feed the actual sampled image.
 memset(backing->contents,0,128);auto black=render(draw);for(size_t i=0;i<black.size();i+=4)assert(black[i+3]==0);
 memset(backing->contents,255,128);assert(render(draw)==expected);
 auto bad=draw;bad.vertices=65537;assert(!refuses([&]{b.submit({bad});}).empty());bad=draw;bad.first=65535;assert(!refuses([&]{b.submit({bad});}).empty());bad=draw;bad.topology=(fuxi::Topology)99;assert(!refuses([&]{b.submit({bad});}).empty());
 b.destroy(p);b.dropPipelineCacheForTest();b.destroy(sample);b.destroy(backing);b.destroy(target);b.destroy(sam);b.destroy(vertices);b.destroy(ones);b.destroy(indices);b.destroy(out);tr.close(lib);
}
static void poison(){
 // Isolate intentional unknown-ownership retention from the parent controls.
 pid_t child=fork();assert(child>=0);if(!child){
  fuxi::Backend b;auto *buffer=b.allocate(128),*out=b.allocate(128);auto *t=b.linearTexture(buffer,1,1,7,5,3,16);
  fuxi::Operation read{};read.texture=t;read.dst=out;b.failQueueAttempt=true;
  assert(!refuses([&]{b.submit({read});}).empty() && b.poisoned);
  assert(!refuses([&]{b.allocate(16);}).empty());assert(!refuses([&]{b.linearTexture(buffer,1,1,7,5,3,16);}).empty());assert(!refuses([&]{b.submit({read});}).empty());
  auto retained=fuxi::liveMemoryObjects.load();assert(!b.destroy(t));b.destroy(buffer);b.destroy(out);assert(fuxi::liveMemoryObjects==retained);_exit(0);
 }int status=0;assert(waitpid(child,&status,0)==child && WIFEXITED(status) && WEXITSTATUS(status)==0);
}
int main(int argc,char **argv){
 poison();fuxi::Backend b;auto baseline=b.resourceBytes();transfers(b);
 assert(argc==1 || argc==2);
 if(argc==2){lines(b,argv[1]);puts("PASS: M9o actual bridge line/list/strip/index/restart pixels, odd/empty counts, lazy cache reuse and sampled shadow CPU coherence");}
 else puts("SKIP: M9o shader/line controls require --translator");
 b.trimMemory();assert(b.resourceBytes()==baseline);
 assert(fuxi::primitiveCount(fuxi::Topology::Line,598)==598 && fuxi::primitiveCount(fuxi::Topology::Line,599)==598);
 puts("PASS: M9o mismatch diagnostics, shadow rows/offset/tight bounds, CPU/GPU coherence, sentinels, ownership, budget/rollback and poison/requireLive");
}
