/* A failed pipeline must never become a translator call or Vulkan bind. */
#pragma once
#include <atomic>
#include <cstdint>
#include <cstdio>
namespace fuxi {
enum class Topology : uint32_t { Triangle, TriangleStrip, Line, LineStrip };
inline bool validTopology(Topology t){return uint32_t(t)<=uint32_t(Topology::LineStrip);}
inline bool primitiveRestart(Topology t){return t==Topology::TriangleStrip || t==Topology::LineStrip;}
inline uint32_t primitiveCount(Topology t,uint32_t n){return t==Topology::Line?n&~1u:t==Topology::LineStrip && n<2?0:n;}
inline bool primitiveBounds(Topology t,uint32_t start,uint32_t count){
 return validTopology(t) && count<=65536 && start<=65536-count &&
  ((t==Topology::Line || t==Topology::LineStrip) || (count>=3 && (t==Topology::TriangleStrip || count%3==0)));
}
inline std::atomic<uint64_t> droppedDraws{0};
inline void dropPipelineDraw(){
 if(!droppedDraws.fetch_add(1))fprintf(stderr,"FUXI MTL: draw dropped missing render pipeline (counter continues)\n");
}
// A draw whose shader declares a fragment texture at a slot with none bound (the client got nil for a refused texture and drew
// anyway) is left out, as Apple's driver would run it reading nothing useful: the bridge has no neutral image for an empty slot,
// so the draw would fail its whole submit and, in backboardd, the GPU path. The first draw of each slot mask prints a line;
// later ones only count (first eight, then powers of two).
inline std::atomic<uint64_t> unboundTextureDraws{0};
inline void dropUnboundTextureDraw(uint32_t missing,const char *pipeline){
 static std::atomic<uint32_t> seen[4]{};const uint64_t n=++unboundTextureDraws;bool fresh=false;
 for(auto &s:seen){uint32_t v=s.load();
  if(!v && s.compare_exchange_strong(v,missing+1)){fresh=true;break;}
  if(v==missing+1)break;}
 if(fresh || n<=8 || !(n&(n-1)))fprintf(stderr,"FUXI MTL: draw dropped unbound texture slots=0x%x count=%llu pipeline=%.160s (GPU path kept)\n",missing,(unsigned long long)n,pipeline?pipeline:"unknown");
}
}
