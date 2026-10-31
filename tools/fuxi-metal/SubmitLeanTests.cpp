/* FUXI_MTL_SUBMIT_LEAN A/B on the production backend: a CA-shaped submit (offscreen passes between main-target passes, a buffer
   writer, repeated resources, indexed draws) recorded more often than the hoist log samples. The caller compares stdout with the knob off and on. */
#include "Backend.hpp"
#include <cassert>
#include <cstdio>
int main(){
 using namespace fuxi;Backend b;
 auto *src=b.allocateTexture(80,5,16,16),*other=b.allocateTexture(80,5,16,16),*target=b.allocateTexture(80,5,16,16),*off=b.allocateTexture(80,5,16,16);
 auto *bytes=b.allocate(1024),*read=b.allocate(1024),*scratch=b.allocate(1024);auto *p=b.compilePipeline(nullptr,"control",nullptr,"control");auto *sam=b.allocateSampler(false,false,1);
 auto put=[&](Texture *t,uint8_t value){memset(bytes->contents,value,1024);for(unsigned i=3;i<1024;i+=4)((uint8_t *)bytes->contents)[i]=value/2;Operation op{};op.src=bytes;op.texture=t;op.upload=true;b.submit({op});};
 put(src,91);put(other,37);
 Operation base{};base.pipeline=p;base.sampleCount[0]=1;base.samplers[0]=sam;base.vertices=6;base.viewport={0,16,16,-16,0,1};base.clear[0]=.2f;base.clear[1]=.4f;base.clear[2]=.6f;base.clear[3]=1;
 auto draw=[&](Texture *t,Texture *sample,uint32_t load,unsigned quadrant,uint64_t encoder){Operation op=base;op.encoderID=encoder;op.texture=t;op.samples[0]=sample;op.load=load;op.scissor={{int32_t(quadrant%2*8),int32_t(quadrant/2*8)},{8,8}};return op;};
 {Operation clear=draw(target,src,2,0,1);clear.scissor={{0,0},{16,16}};b.submit({clear});}
 Operation copy{};copy.src=bytes;copy.dst=scratch;copy.size=256;
 // off is fully rewritten twice (rename candidate), sampled by the main target in between (hoist/reorder candidates); copy writes a buffer mid-submit.
 std::vector<Operation> ops{draw(off,src,2,0,10),draw(target,off,1,0,11),draw(off,other,2,1,12),draw(target,off,1,1,13),copy,draw(target,src,1,2,14),draw(off,src,0,2,15),draw(target,other,1,3,16),draw(target,off,1,3,16)};
 // Two draws are indexed (16-bit, and 32-bit at an offset), so the CPU index scan (bulk read with the knob) runs on every submit.
 auto *idx16=b.allocate(64),*idx32=b.allocate(64);for(uint16_t i=0;i<6;++i)((uint16_t *)idx16->contents)[i]=i;for(uint32_t i=0;i<6;++i)((uint32_t *)idx32->contents)[2+i]=5-i;
 ops[1].indices=idx16;ops[1].indexWidth=2;ops[5].indices=idx32;ops[5].indexWidth=4;ops[5].indexOffset=8;
 auto dump=[&](const char *label,Texture *t){Operation get{};get.texture=t;get.dst=read;b.submit({get});uint64_t h=0xcbf29ce484222325ull;for(size_t i=0;i<16*16*4;++i)h=(h^((uint8_t *)read->contents)[i])*0x100000001b3ull;printf("PIXELS %s %016llx\n",label,(unsigned long long)h);};
 for(unsigned frame=0;frame<32;++frame){
  FrameSample s;{FrameTraceScope scope(&s);b.submit(ops);}
  printf("COUNTS frame=%u draws=%llu passes=%llu merged=%llu image_barriers=%llu pipeline_barriers=%llu use_probes=%llu frame_probes=%llu reorders=%llu\n",frame,(unsigned long long)s.draws,(unsigned long long)s.passes,(unsigned long long)s.mergedDraws,(unsigned long long)s.imageBarriers,(unsigned long long)s.pipelineBarriers,(unsigned long long)s.scans[size_t(ScanKind::useProbes)],(unsigned long long)s.scans[size_t(ScanKind::frameProbes)],(unsigned long long)s.passReorders);
  if(frame%8==7){dump("target",target);dump("off",off);}
 }
 // A draw binding the buffer an earlier op of the same submit writes is refused in both planners.
 {Operation bad=draw(target,src,1,0,20);bad.buffers[1][3]={scratch,0,false};std::string why="accepted";try{b.submit({copy,draw(target,src,1,1,19),bad});}catch(const std::exception &e){why=e.what();}
  printf("REFUSAL %s\n",why.c_str());assert(why.find("GPU-written draw buffer requires separate submission")!=std::string::npos);}
 dump("after-refusal",target);
 printf("RENAME submits=%llu hoisted=%llu\n",(unsigned long long)b.rtRenameSubmits,(unsigned long long)b.rtHoisted);
 b.destroy(p);b.destroy(sam);b.destroy(src);b.destroy(other);b.destroy(target);b.destroy(off);b.destroy(bytes);b.destroy(read);b.destroy(scratch);b.destroy(idx16);b.destroy(idx32);b.close();
 puts("PASS: submit lean fixture");
}
