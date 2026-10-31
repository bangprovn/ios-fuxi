/* Public Metal ordinals; bounded intersection of the pinned translator's exact
 * mappings and Turnip's fd6 vertex formats. No SDK or third-party source. */
#pragma once
#include <algorithm>
#include <cstdint>
#include <cstring>
namespace fuxi {
struct VertexFormatInfo {const char *name;uint32_t bytes;};
inline VertexFormatInfo vertexFormatInfo(uint64_t format) {
#define F(n,name,size) case n:return {#name,size}
 switch(format) {
 F(1,UChar2,2);F(2,UChar3,3);F(3,UChar4,4);
 F(4,Char2,2);F(5,Char3,3);F(6,Char4,4);
 F(7,UChar2Normalized,2);F(8,UChar3Normalized,3);F(9,UChar4Normalized,4);
 F(10,Char2Normalized,2);F(11,Char3Normalized,3);F(12,Char4Normalized,4);
 F(13,UShort2,4);F(14,UShort3,6);F(15,UShort4,8);
 F(16,Short2,4);F(17,Short3,6);F(18,Short4,8);
 F(19,UShort2Normalized,4);F(20,UShort3Normalized,6);F(21,UShort4Normalized,8);
 F(22,Short2Normalized,4);F(23,Short3Normalized,6);F(24,Short4Normalized,8);
 F(25,Half2,4);F(26,Half3,6);F(27,Half4,8);
 F(28,Float,4);F(29,Float2,8);F(30,Float3,12);F(31,Float4,16);
 F(32,Int,4);F(33,Int2,8);F(34,Int3,12);F(35,Int4,16);
 F(36,UInt,4);F(37,UInt2,8);F(38,UInt3,12);F(39,UInt4,16);
 F(40,Int1010102Normalized,4);F(41,UInt1010102Normalized,4);F(42,UChar4Normalized_BGRA,4);
 F(45,UChar,1);F(46,Char,1);F(47,UCharNormalized,1);F(48,CharNormalized,1);
 F(49,UShort,2);F(50,Short,2);F(51,UShortNormalized,2);F(52,ShortNormalized,2);
 F(53,Half,2);F(54,FloatRG11B10,4);
 // RGB9E5 maps exactly in m2v but Turnip has no vertex format for it.
 case 55:return {"FloatRGB9E5",0};
 default:return {format?"unknown":"Invalid",0};
 }
#undef F
}
inline const char *vertexAttributeRejection(uint64_t index,uint64_t format,uint64_t buffer,uint64_t offset) {
 if(index>=8)return "attribute index >= 8";
 uint32_t bytes=vertexFormatInfo(format).bytes;
 if(!bytes)return "format outside Turnip vertex profile";
 if(buffer>=4)return "bufferIndex >= 4";
 if(offset>256-bytes)return "offset + format bytes > 256";
 return nullptr;
}
inline const char *vertexLayoutRejection(uint64_t index,uint64_t stride,uint64_t step,uint64_t rate,bool referenced) {
 if(index>=4)return "layout index >= 4";
 if(stride>256 || (referenced && !stride))return "stride outside 1..256";
 // ABI 6 carries strides, not step state. Never silently turn instance/constant
 // input into per-vertex input, even when the configured stride is zero.
 if(step!=1)return "stepFunction requires PerVertex (instance/constant deferred)";
 if(rate!=1)return "stepRate requires 1 (vertex attribute divisor deferred)";
 return nullptr;
}
inline bool vertexFetchFits(uint64_t length,uint64_t boundOffset,uint32_t last,uint32_t stride,uint32_t offset,uint32_t format) {
 uint32_t bytes=vertexFormatInfo(format).bytes;
 if(!bytes || !stride || stride>256 || offset>256-bytes || boundOffset>length)return false;
 // Subtraction avoids wrapping a malformed bound offset; last*stride fits u64.
 return uint64_t(last)*stride+offset+bytes<=length-boundOffset;
}
// Largest index of count width-byte (2 or 4) little-endian indices at p, skipping the all-ones restart value when restart; 0 when none.
// Equal to reading each index with its own memcpy. Chunked bulk copies: the source may be uncached host-visible memory, where wide loads are far cheaper.
inline uint32_t indexMax(const uint8_t *p,uint32_t count,uint32_t width,bool restart) {
 uint32_t last=0;
 if(width==2){uint16_t v[1024];for(uint32_t i=0;i<count;){uint32_t n=std::min<uint32_t>(count-i,1024);memcpy(v,p+uint64_t(i)*2,size_t(n)*2);i+=n;for(uint32_t k=0;k<n;++k)if(!(restart && v[k]==UINT16_MAX))last=std::max<uint32_t>(last,v[k]);}}
 else {uint32_t v[512];for(uint32_t i=0;i<count;){uint32_t n=std::min<uint32_t>(count-i,512);memcpy(v,p+uint64_t(i)*4,size_t(n)*4);i+=n;for(uint32_t k=0;k<n;++k)if(!(restart && v[k]==UINT32_MAX))last=std::max(last,v[k]);}}
 return last;
}
}
