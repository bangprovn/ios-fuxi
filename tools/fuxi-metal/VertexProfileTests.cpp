/* SDK-independent production bounds: compile/run with the frontend harness. */
#include "VertexProfile.hpp"
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <vector>
#include <algorithm>
int main() {
 unsigned accepted=0;
 for(unsigned format=0;format<256;++format) {
  auto info=fuxi::vertexFormatInfo(format);
  bool expected=(format>=1 && format<=42) || (format>=45 && format<=54);
  assert((info.bytes!=0)==expected);
  if(!expected){assert(fuxi::vertexAttributeRejection(0,format,0,0));continue;}
  ++accepted;assert(!fuxi::vertexAttributeRejection(7,format,3,256-info.bytes));
  assert(fuxi::vertexAttributeRejection(8,format,3,0));
  assert(fuxi::vertexAttributeRejection(7,format,4,0));
  assert(fuxi::vertexAttributeRejection(7,format,3,257-info.bytes));
  assert(fuxi::vertexFetchFits(256,0,0,256,256-info.bytes,format));
  assert(!fuxi::vertexFetchFits(255,0,0,256,256-info.bytes,format));
 }
 assert(accepted==52);
 assert(fuxi::vertexFormatInfo(27).bytes==8 && fuxi::vertexFormatInfo(30).bytes==12);
 assert(!fuxi::vertexLayoutRejection(3,256,1,1,true));
 assert(fuxi::vertexLayoutRejection(4,16,1,1,false));
 assert(fuxi::vertexLayoutRejection(3,257,1,1,true));
 assert(fuxi::vertexLayoutRejection(3,0,1,1,true));
 assert(fuxi::vertexLayoutRejection(3,0,0,0,false));
 assert(fuxi::vertexLayoutRejection(3,16,2,1,true));
 assert(fuxi::vertexLayoutRejection(3,16,1,2,true));
 // Last half4 in a tightly sized compact quad: the old 16-byte guess overread.
 assert(fuxi::vertexFetchFits(64,0,3,16,8,27));
 assert(!fuxi::vertexFetchFits(63,0,3,16,8,27));
 assert(fuxi::vertexFetchFits(80,16,3,16,8,27));
 assert(!fuxi::vertexFetchFits(UINT64_MAX,UINT64_MAX-2,0,16,0,27));
 assert(!fuxi::vertexFetchFits(64,UINT64_MAX,0,16,0,27));
 assert(!fuxi::vertexFetchFits(64,0,0,0,0,27));
 assert(!fuxi::vertexFetchFits(64,0,0,16,0,55));
 // indexMax equals the per-index memcpy scan (FUXI_MTL_SUBMIT_LEAN), across chunk edges, both widths, restart on/off, unaligned bases
 {std::vector<uint8_t> bytes(4*3000+8);uint32_t seed=7;auto rnd=[&]{seed=seed*1664525u+1013904223u;return seed;};
  auto reference=[&](const uint8_t *p,uint32_t count,uint32_t width,bool restart){uint32_t last=0;for(uint32_t i=0;i<count;++i){uint32_t index=0;memcpy(&index,p+uint64_t(width)*i,width);if(restart && index==(width==2?UINT16_MAX:UINT32_MAX))continue;last=std::max(last,index);}return last;};
  unsigned cases=0;for(int round=0;round<200;++round){for(auto &b:bytes)b=uint8_t(rnd()%5==0?0xff:rnd());
   for(uint32_t width:{2u,4u})for(bool restart:{false,true})for(uint32_t count:{0u,1u,6u,511u,512u,513u,1023u,1024u,1025u,2049u,uint32_t(rnd()%3000)}){uint32_t base=rnd()%8;
    assert(fuxi::indexMax(bytes.data()+base,count,width,restart)==reference(bytes.data()+base,count,width,restart));++cases;}}
  std::vector<uint8_t> ones(64,0xff);assert(fuxi::indexMax(ones.data(),16,2,true)==0 && fuxi::indexMax(ones.data(),16,2,false)==UINT16_MAX && fuxi::indexMax(ones.data(),16,4,true)==0 && fuxi::indexMax(ones.data(),16,4,false)==UINT32_MAX);
  printf("PASS: indexMax equals the per-index scan (%u cases)\n",cases);}
 puts("PASS: 52 exact vertex widths, descriptor and fetch bounds, half4 quad and offset overflow");
}
