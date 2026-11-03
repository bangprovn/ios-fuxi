/* Layout decisions for a linear texture over an MTLBuffer. No Vulkan calls: the host tests check them directly. */
#pragma once
#include <cstddef>
#include <cstdint>
#include <string>
namespace fuxi {
// Row alignment Turnip takes for an explicit DRM_FORMAT_MOD_LINEAR plane (fd6_layout with an explicit layout: 64 bytes,
// and 16 texels for texels wider than 4 bytes). Its own linear pitch is 64 texels: 256 bytes for BGRA8.
inline size_t explicitRowAlignment(size_t texel){return texel>4?16*texel:64;}
// A Metal row that can be the explicit row of the image: aligned and wide enough. QuartzCore's rows are 64-byte aligned.
inline bool explicitRowFits(size_t row,uint32_t width,size_t texel){return texel && width && row && row%explicitRowAlignment(texel)==0 && row>=size_t(width)*texel;}
// Bytes an explicit linear image at this row spans in Turnip: the height rounded up to 4 rows (its blits over-fetch in
// 16x4 blocks), 64-byte aligned. The plain linear layout also rounds the total up to 4 KiB; the explicit one does not.
inline uint64_t explicitLinearBytes(size_t row,uint32_t height){return ((uint64_t(height)+3)/4*4*row+63)&~uint64_t(63);}
// The kernel BO a Turnip allocation of this size gets (darwin_size_class, tools/patches/mesa-fuxi-bo-cache.patch):
// 4 KiB pages up to 16 KiB, then steps of a quarter of the power of two below. Bytes up to it cost no memory.
inline uint64_t turnipBoBytes(uint64_t size){
 size=(size+4095)&~uint64_t(4095);if(size<=16384)return size;
 const uint64_t step=(uint64_t(1)<<(63-__builtin_clzll(size-1)))/4;return (size+step-1)/step*step;
}
// Bytes allocated past a buffer of `length` (its Vulkan size `size`) so that a linear texture CA places at its end keeps
// the up to 3 padding rows inside the memory it is bound to: CA sizes its image buffers to the rows, and Turnip's image is
// 4-row aligned (d-ft-v27: 463 shadows were 64-1536 bytes short). 1/8 of the length covers any texture of 24 rows or more;
// at least 4 KiB, at most 16 KiB (three 1365-pixel BGRA8 rows). Buffers under 4 KiB get none; above 256 KiB only what the
// BO's size class leaves free, since one step more there costs a quarter of the buffer (a 1 MiB ring would take 256 KiB).
inline size_t linearTailPad(size_t length,size_t size){
 if(length<4096)return 0;
 size_t want=(length/8+4095)&~size_t(4095);want=want<4096?4096:want>16384?16384:want;
 const uint64_t slack=turnipBoBytes(size)-size;
 if(want<=slack || length<=(256u<<10))return want;
 return size_t(slack)&~size_t(63);
}
// The kind of a shadow fallback: its reason with every number replaced by N. Each kind is logged once.
inline std::string linearShadowKind(const std::string &reason){
 std::string kind;bool digits=false;
 for(char c:reason){if(c>='0' && c<='9'){if(!digits)kind+='N';digits=true;}else{kind+=c;digits=false;}}
 return kind;
}
}
