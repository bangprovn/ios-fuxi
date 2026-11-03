/* Explicit-row and tail-pad decisions for linear textures over a buffer, on the shapes d-ft-v27 shadowed. */
#include "LinearLayout.hpp"
#include <cassert>
#include <cstdio>
using namespace fuxi;
int main(){
 // Turnip's explicit linear rows: 64 bytes up to 4-byte texels, 16 texels above.
 assert(explicitRowAlignment(1)==64 && explicitRowAlignment(4)==64 && explicitRowAlignment(8)==128 && explicitRowAlignment(16)==256);
 assert(explicitRowFits(832,194,4) && explicitRowFits(64,1,4) && explicitRowFits(192,68,2) && explicitRowFits(256,256,1));
 assert(explicitRowFits(320,80,4) && !explicitRowFits(800,194,4) && !explicitRowFits(768,194,4) && !explicitRowFits(64,17,4) && !explicitRowFits(0,1,4) && !explicitRowFits(64,0,4) && !explicitRowFits(64,1,0));
 assert(explicitRowFits(1024,128,8) && !explicitRowFits(960,120,8));
 assert(explicitLinearBytes(256,61)==16384 && explicitLinearBytes(320,47)==15360 && explicitLinearBytes(4544,2436)==11069184 && explicitLinearBytes(100,1)==448);
 // BO size classes of mesa-fuxi-bo-cache.patch.
 assert(turnipBoBytes(1)==4096 && turnipBoBytes(16384)==16384 && turnipBoBytes(16385)==20480 && turnipBoBytes(32768)==32768 && turnipBoBytes(36864)==40960);
 assert(turnipBoBytes(147456)==163840 && turnipBoBytes(1<<20)==(1<<20) && turnipBoBytes((1<<20)+1)==1310720 && turnipBoBytes(1851392)==2097152);
 // Tail pad: none under 4 KiB; 4-16 KiB up to 256 KiB; above, only the BO's free slack.
 assert(linearTailPad(4095,4096)==0 && linearTailPad(4096,4096)==4096 && linearTailPad(16384,16384)==4096 && linearTailPad(65536,65536)==8192 && linearTailPad(147456,147456)==16384);
 assert(linearTailPad(256u<<10,256u<<10)==16384 && linearTailPad(1<<20,1<<20)==0 && linearTailPad(1851392,1851392)==16384 && linearTailPad((1<<20)+64,(1<<20)+64)==16384);
 assert(linearTailPad(4u<<20,(4u<<20)-4032)==4032);
 // d-ft-v27's shadows (texel bytes, width x height, offset, Metal row, buffer size from the logged "available"): each must now
 // alias at its own row within the buffer's memory plus the tail pad. The plain layout's 4 KiB rounding made 778x174 short;
 // the explicit one fits it. The marked ones are short by their 4-row padding and need the pad.
 struct Case {size_t texel;uint32_t w,h;size_t offset,row,buffer;bool needsPad;} cases[]={
  {1,256,61,64,256,16384,true},{1,497,61,64,512,32768,true},{1,778,174,64,832,147456,false},{1,293,47,64,320,16384,false},{1,254,134,13824,256,49152,false},
  {4,987,465,64,3968,1851392,true},{4,1125,2436,0,4544,11075584,false},{4,787,1705,0,3200,5472256,false},{4,87,87,8192,384,49152,false},
  {4,465,465,64,1920,901120,false},{4,1,366,0,64,32768,false},{2,68,66,0,192,16384,false},{4,165,183,0,704,131072,false}};
 unsigned padded=0;
 for(auto &c:cases){
  assert(explicitRowFits(c.row,c.w,c.texel));
  const uint64_t need=explicitLinearBytes(c.row,c.h),bare=c.buffer-c.offset,withPad=bare+linearTailPad(c.buffer,c.buffer);
  assert((need>bare)==c.needsPad && need<=withPad);padded+=c.needsPad;
 }
 assert(padded==3);
 // One log line per kind of fallback: numbers do not make a new kind.
 assert(linearShadowKind("size required=16384 available=16320")==linearShadowKind("size required=32768 available=32704"));
 assert(linearShadowKind("size required=16384 available=16320")=="size required=N available=N");
 assert(linearShadowKind("rowPitch Vulkan=512 Metal=384")!=linearShadowKind("size required=1 available=0"));
 puts("PASS: explicit linear rows, BO size classes, tail pad, d-ft-v27 shadow shapes alias, shadow kinds");
}
