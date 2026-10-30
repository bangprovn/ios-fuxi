/* FUXI_MTL_SPIRV_UNROLL: ask the driver to unroll the loops metal2vulkan's roll pass wrote. Above 24576 inlined instructions the translator writes k identical
   regions once as a k-trip loop marked DontUnroll, choosing each trip's constants by OpSelect on the trip index. In CA's uber shader those constants are the
   colour program's opcodes, so the per-trip switch can no longer fold: Turnip keeps the loop and every switch arm (6 pipelines of ~116 compile to 1553-1784 ir3
   instructions with 3 loops, the rest to 43-568 without). Turning DontUnroll into Unroll lets NIR unroll the fixed-trip loop and fold each trip's switch.
   Only the LoopControl word changes (Unroll and DontUnroll take no parameters), so the module keeps its size and its meaning; a driver that cannot find the trip
   count keeps the loop. Mode 1 flips DontUnroll loops without image instructions (metal2vulkan's anisotropic tap loops sample and stay as they are); mode 2 also
   marks the loops nested in a flipped loop that carry no control, since NIR unrolls an outer loop only once its inner loops are gone. */
#pragma once
#include <cstddef>
#include <cstdint>
#include <unordered_map>
#include <vector>
namespace fuxi {
struct UnrollStats {unsigned loops=0,dontUnroll=0,flipped=0,nested=0,keptSampling=0;};
inline bool unrollRolledLoops(const uint32_t *w,size_t n,std::vector<uint32_t> &out,UnrollStats &st,unsigned mode) {
 st={};if(!w || n<5 || w[0]!=0x07230203 || !mode)return false;
 struct Loop {size_t control,header,merge;uint32_t mergeId;bool images=false,flip=false;};
 std::vector<Loop> loops;std::unordered_map<uint32_t,size_t> blockOf;std::vector<size_t> imageBlocks;size_t block=0;
 for(size_t i=5;i<n;){uint32_t op=w[i]&0xffffu,len=w[i]>>16;if(!len || i+len>n)return false;
  if(op==248 && len>=2){block=blockOf.size()+1;blockOf[w[i+1]]=block;}
  else if(op==246 && len>=4)loops.push_back({i+3,block,0,w[i+1]});
  else if((op>=87 && op<=99) || (op>=305 && op<=320))imageBlocks.push_back(block); // OpImageSample*..OpImageWrite, OpImageSparse*
  i+=len;}
 for(auto &l:loops){auto m=blockOf.find(l.mergeId);l.merge=m==blockOf.end()?0:m->second;
  if(l.merge>l.header)for(size_t b:imageBlocks)if(b>=l.header && b<l.merge){l.images=true;break;}}
 for(auto &l:loops){++st.loops;uint32_t c=w[l.control];if(!(c&2u))continue;++st.dontUnroll;if(l.images){++st.keptSampling;continue;}l.flip=true;++st.flipped;}
 if(mode>=2)for(auto &l:loops)if(!l.flip && !(w[l.control]&3u))for(auto &o:loops)if(o.flip && o.merge>o.header && l.header>o.header && l.header<o.merge){l.flip=true;++st.nested;break;}
 if(!st.flipped)return false;
 out.assign(w,w+n);for(auto &l:loops)if(l.flip)out[l.control]=(out[l.control]&~2u)|1u;
 return true;
}
}
