/* FUXI_MTL_SPLIT_SUBMIT A/B on the production backend. A CA-shaped frame (offscreen -> main -> offscreen of main -> main, a texture copy between
   passes) is submitted synchronously, as two overlapping flights with early-release hooks, and with a failure after a queued chunk. Lines starting
   with CHUNKS differ by design; every other stdout line must be identical with the knob off and on. Fault modes run in their own process. */
#include "Backend.hpp"
#include <cassert>
#include <cstdio>
#include <future>
int main(int argc,char **argv){
 using namespace fuxi;Backend b;const bool split=systemTranslationPolicy().splitSubmit;
 void *proxy=dlopen(FUXI_MTL_ICD,RTLD_NOW|RTLD_LOCAL);assert(proxy);
 auto submits=[&]{return reinterpret_cast<uint64_t (*)()>(dlsym(proxy,"render_merge_submits"))();};
 auto fenceFault=reinterpret_cast<void (*)(int)>(dlsym(proxy,"render_merge_fence_fault"));assert(fenceFault);
 Texture *src[3];for(auto &t:src)t=b.allocateTexture(80,5,16,16);auto *target=b.allocateTexture(80,5,16,16),*backdrop=b.allocateTexture(80,5,16,16);Texture *off[3];for(auto &t:off)t=b.allocateTexture(80,5,16,16);
 auto *bytes=b.allocate(1024),*read=b.allocate(1024);auto *p=b.compilePipeline(nullptr,"control",nullptr,"control");auto *sam=b.allocateSampler(false,false,1);
 auto put=[&](Texture *t,uint8_t value){memset(bytes->contents,value,1024);for(unsigned i=3;i<1024;i+=4)((uint8_t *)bytes->contents)[i]=value/2;Operation op{};op.src=bytes;op.texture=t;op.upload=true;b.submit({op});};
 Operation base{};base.pipeline=p;base.sampleCount[0]=1;base.samplers[0]=sam;base.vertices=6;base.viewport={0,16,16,-16,0,1};base.clear[0]=.2f;base.clear[1]=.4f;base.clear[2]=.6f;base.clear[3]=1;
 auto draw=[&](Texture *t,Texture *sample,uint32_t load,unsigned quadrant,uint64_t encoder){Operation op=base;op.encoderID=encoder;op.texture=t;op.samples[0]=sample;op.load=load;op.scissor={{int32_t(quadrant%2*8),int32_t(quadrant/2*8)},{8,8}};return op;};
 Operation copy{};copy.sourceTexture=target;copy.texture=backdrop;
 // The shader samples the centre texel, i.e. quadrant 3 of its source, so the colours below follow every offscreen->main, main->offscreen and
 // copy dependency; the salt rotates the three source colours per frame. Every target is cleared or fully copied before it is loaded, so a frame is
 // idempotent: a partially executed frame followed by the whole one gives the whole frame's pixels.
 auto frame=[&](unsigned salt){auto *s1=src[salt%3],*s2=src[(salt+1)%3],*s3=src[(salt+2)%3];
  return std::vector<Operation>{draw(off[0],s1,2,3,1),draw(target,off[0],2,3,2),draw(off[1],target,2,3,3),draw(target,s2,1,0,4),copy,draw(target,s3,1,3,5),
   draw(off[2],backdrop,2,3,6),draw(target,off[2],1,1,7),draw(off[0],target,1,3,8),draw(target,off[1],1,2,9)};};
 auto dump=[&](const char *label,Texture *t){Operation get{};get.texture=t;get.dst=read;b.submit({get});uint64_t h=0xcbf29ce484222325ull;for(size_t i=0;i<16*16*4;++i)h=(h^((uint8_t *)read->contents)[i])*0x100000001b3ull;printf("PIXELS %s %016llx\n",label,(unsigned long long)h);};
 auto dumpAll=[&](const char *label){char name[64];snprintf(name,sizeof name,"%s-target",label);dump(name,target);snprintf(name,sizeof name,"%s-off",label);dump(name,off[0]);snprintf(name,sizeof name,"%s-backdrop",label);dump(name,backdrop);
  snprintf(name,sizeof name,"%s-off1",label);dump(name,off[1]);snprintf(name,sizeof name,"%s-off2",label);dump(name,off[2]);};
 put(src[0],91);put(src[1],37);put(src[2],160);
 if(argc>1){ // fault modes: lost/timeout fence of a chunked flight, or a lost chunk QueueSubmit
  unsigned fired=0;auto refused=Backend::earlyRefused.load();std::function<void()> ticket;auto held=liveMemoryObjects.load();
  if(!strcmp(argv[1],"chunk")){
#ifdef FUXI_MTL_HOST_TEST
   b.failChunkAttempt=true;
#endif
   bool failed=false;try{b.submit(frame(0),{},&ticket,{},nullptr,[&]{++fired;});}catch(const std::runtime_error &){failed=true;}
   assert(failed==split && b.poisoned==split && !fired);if(!split){ticket();b.failChunkAttempt=false;}
   puts(split?"PASS: lost chunk QueueSubmit poisons the backend before the last chunk":"PASS: chunk fault hook unused without split submit");return 0;}
  if(!strcmp(argv[1],"drain")){ // the frame fails after a queued chunk and the drain's fence is lost: poison instead of freeing in-flight objects
#ifdef FUXI_MTL_HOST_TEST
   b.failAfterChunk=true;
#endif
   fenceFault(1);bool failed=false;try{b.submit(frame(0),{},&ticket,{},nullptr,[&]{++fired;});}catch(const std::runtime_error &){failed=true;}
   if(!split){assert(!failed);try{ticket();}catch(const std::runtime_error &){failed=true;}}
   assert(failed && b.poisoned && !fired && Backend::splitDrained==0);puts("PASS: a lost drain fence after a queued chunk poisons; nothing is freed");return 0;}
  fenceFault(!strcmp(argv[1],"lost")?1:2);b.submit(frame(0),{},&ticket,{},nullptr,[&]{++fired;});
  bool failed=false;try{ticket();}catch(const std::runtime_error &){failed=true;}
  assert(failed && b.poisoned && !fired && Backend::earlyRefused==refused+1); // a lost/timed-out fence never releases leases early
  b.destroy(target);b.destroy(off[0]);assert(liveMemoryObjects>=held); // pinned by the failed flight: quarantined, never freed
  printf("PASS: %s fence of a %s flight fails its ticket, poisons, no early release, DMA quarantined\n",argv[1],split?"chunked":"single");return 0;}
 // 1. Synchronous submits.
 for(unsigned n=0;n<6;++n){auto before=submits();FrameSample s;{FrameTraceScope scope(&s);b.submit(frame(n));}
  printf("COUNTS sync frame=%u draws=%llu passes=%llu merged=%llu image_barriers=%llu pipeline_barriers=%llu gpu=%d\n",n,(unsigned long long)s.draws,(unsigned long long)s.passes,(unsigned long long)s.mergedDraws,(unsigned long long)s.imageBarriers,(unsigned long long)s.pipelineBarriers,int(s.gpuSamples==1 && s.gpuExec>0));
  printf("CHUNKS sync frame=%u queue_submits=%llu\n",n,(unsigned long long)(submits()-before));}
 dumpAll("sync");
 // The control pipeline blends at source alpha, so each quadrant keeps its own history: frame 5 leaves q0 (s2 over clear), q1/q2 (s1 via backdrop and
 // off1) and q3 (s3 over s1 over clear) distinct; a reordered or dropped dependency changes these hashes.
 {Operation get{};get.texture=target;get.dst=read;b.submit({get});auto *px=(const uint8_t *)read->contents;auto at=[&](unsigned q){uint32_t v;memcpy(&v,px+((q/2*8+4)*16+q%2*8+4)*4,4);return v;};
  assert(at(0)!=at(1) && at(1)!=at(3) && at(0)!=at(3));}
 // 2. Two overlapping flights with early-release hooks; a third submit waits for a free slot.
 {std::atomic<unsigned> fired1{0},fired2{0};std::function<void()> t1,t2;auto before=submits();
  b.submit(frame(1),{},&t1,{},nullptr,[&]{++fired1;});b.submit(frame(2),{},&t2,{},nullptr,[&]{++fired2;});
  printf("CHUNKS overlap queue_submits=%llu\n",(unsigned long long)(submits()-before));
  std::promise<void> started;auto third=std::async(std::launch::async,[&]{started.set_value();b.submit(frame(3));});
  started.get_future().wait();assert(third.wait_for(std::chrono::milliseconds(30))==std::future_status::timeout);
  assert(!fired1 && !fired2);t1();assert(fired1==1 && !fired2);third.get();t2();assert(fired1==1 && fired2==1);
  printf("OVERLAP early_release=%u/%u\n",fired1.load(),fired2.load());}
 dumpAll("overlap");
 // 3. A failure after a queued chunk, while the other slot is in flight: the queue drains, the backend stays live, the next frame is whole.
 {std::function<void()> t1;b.submit(frame(4),{},&t1);auto drained=Backend::splitDrained.load();
#ifdef FUXI_MTL_HOST_TEST
  b.failAfterChunk=true;
#endif
  std::function<void()> t2;bool failed=false;try{b.submit(frame(5),{},&t2);}catch(const std::runtime_error &e){failed=true;assert(strstr(e.what(),"injected failure after a queued chunk"));}
  assert(failed==split && !b.poisoned && Backend::splitDrained==drained+(split?1:0) && bool(t2)!=split);b.failAfterChunk=false;
  printf("CHUNKS drained failed=%d\n",int(failed));t1();if(t2)t2();b.submit(frame(5));printf("DRAIN poisoned=%d\n",int(b.poisoned.load()));}
 dumpAll("drain");
 b.destroy(p);b.destroy(sam);b.destroy(target);b.destroy(backdrop);for(auto *t:off)b.destroy(t);for(auto *t:src)b.destroy(t);b.destroy(bytes);b.destroy(read);b.close();
 puts("PASS: split submit fixture");
}
