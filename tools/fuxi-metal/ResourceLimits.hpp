/* Original shared resource bounds. No dimension/stride products before checks. */
#pragma once
#include <cstddef>
#include <cstdint>
#include <algorithm>
#include <cmath>
#include <stdexcept>
namespace fuxi {
// A refusal by FuxiMetal's own byte accounting: no Vulkan call failed and nothing is latched. The caller may shed and retry, fall
// back (a copy for an import), or fail that one resource softly (newTextureWithDescriptor: returns nil, Frontend.mm).
struct BudgetRefusal : std::runtime_error {using std::runtime_error::runtime_error;};
// Two accounts. `used`: memory FuxiMetal allocates itself (textures, buffers, slabs, pools, shadows, copies), aimed at `target`;
// optional features and the submit headroom are measured against it. `imported`: client memory backboardd only pins and maps (an
// IOSurface's pages, a no-copy buffer's), bounded apart by `importCap`. Before 5 Nov both were one 256 MiB budget: in v-imp2c
// (YouTube in Safari) 348 imports held ~213 MB of it, the optional memory was all shed, and CA's own 284x93 texture was refused,
// which latched backboardd onto the CPU. Both accounts together stay under `ceiling`: FuxiAdreno wires at most
// FG_ABI_MAX_WIRED_BYTES per client (xnu/fuxi-adreno/fuxi_abi_wire.h, 768 MiB), which also covers Turnip's own BOs (command
// streams, visibility streams, descriptor and query pools, shader code) that are not counted here; driverReserve is left for those.
// Their size was not measured (no log prints them), so the reserve is generous: a kernel refusal of an own allocation is still a
// device fault (check()), while a full import account only means a copy or one dropped image.
struct ResourceLimits {
 static constexpr size_t budget=256u*1024*1024;
 static constexpr size_t single=128u*1024*1024;
 static constexpr size_t kernelWired=768u*1024*1024,driverReserve=128u*1024*1024;
 static constexpr size_t wiredCeiling=kernelWired-driverReserve;
 static constexpr size_t importBudget=wiredCeiling-budget;
 static_assert(budget+importBudget+driverReserve==kernelWired,"own target, imports and Turnip's reserve fill FuxiAdreno's per-client quota");
 uint32_t dimension=4096;
 size_t used=0,imported=0;
 // Peaks since the last resetPeaks() (the cache period line prints and resets them).
 size_t usedPeak=0,importedPeak=0;
 // Instance bounds, the constants above unless a host test shrinks them.
 size_t target=budget,importCap=importBudget,ceiling=wiredCeiling;
 explicit ResourceLimits(uint32_t device=4096):dimension(std::min(device,4096u)){}
 bool extent(size_t w,size_t h,size_t pixel) const {
  return w && h && w<=dimension && h<=dimension && pixel && w<=single/pixel && h<=single/(w*pixel);
 }
 bool volume(size_t w,size_t h,size_t d,size_t pixel) const {
  return extent(w,h,pixel) && d && d<=dimension && d<=single/(w*h*pixel);
 }
 bool rows(size_t w,size_t h,size_t pixel,size_t row,size_t offset,size_t length) const {
  return extent(w,h,pixel) && row>=w*pixel && row%pixel==0 && offset<=length && row<=single/h && row*h<=length-offset;
 }
 // Buffer textures need only the used texels of the final padded row.
 bool linearRows(size_t w,size_t h,size_t pixel,size_t row,size_t offset,size_t length) const {
  return extent(w,h,pixel) && row>=w*pixel && row%pixel==0 && offset%pixel==0 &&
   offset<=length && row<=single/h && (h-1)*row+w*pixel<=length-offset;
 }
 // Own bytes left under the target (0 while over it).
 size_t left() const {return used<target?target-used:0;}
 // Bytes either account may still add before the wired ceiling.
 size_t wiredLeft() const {return used+imported<ceiling?ceiling-used-imported:0;}
 void reserve(size_t bytes) {if(!bytes || bytes>single || bytes>left() || bytes>wiredLeft())throw BudgetRefusal("resource byte budget");used+=bytes;usedPeak=std::max(usedPeak,used);}
 // Own bytes past the target, for an allocation the client cannot do without, after everything optional was shed: allowed while
 // FuxiAdreno can still wire them (own + imported under the ceiling).
 bool overdraftFits(size_t bytes) const {return bytes && bytes<=single && bytes<=wiredLeft();}
 void overdraft(size_t bytes) {if(!overdraftFits(bytes))throw BudgetRefusal("resource byte budget");used+=bytes;usedPeak=std::max(usedPeak,used);}
 void release(size_t bytes){used-=bytes;}
 bool importFits(size_t bytes) const {return bytes && imported<=importCap && bytes<=importCap-imported && bytes<=wiredLeft();}
 void reserveImport(size_t bytes) {if(!importFits(bytes))throw BudgetRefusal("import byte budget");imported+=bytes;importedPeak=std::max(importedPeak,imported);}
 void releaseImport(size_t bytes){imported-=bytes;}
 void resetPeaks(){usedPeak=used;importedPeak=imported;}
};
inline bool scissorFits(size_t x,size_t y,size_t w,size_t h,size_t targetW,size_t targetH) {
 return x<=targetW && y<=targetH && w<=targetW-x && h<=targetH-y;
}
inline bool viewportFits(double x,double y,double w,double h,double near,double far,size_t targetW,size_t targetH) {
 return std::isfinite(x) && std::isfinite(y) && std::isfinite(w) && std::isfinite(h) && std::isfinite(near) && std::isfinite(far) &&
  x>=0 && y>=0 && x<=targetW && y<=targetH && w>0 && h>0 && w<=targetW-x && h<=targetH-y && near>=0 && far<=1 && near<=far;
}
}
