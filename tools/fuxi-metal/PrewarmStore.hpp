/* Pipeline recipes for startup prewarm. A recipe names the two libraries (by content hash), the two function names and the
 * compile options of a pipeline this process compiled before, so a later boot can recompile it in the background as soon as
 * the same libraries are loaded. Pure data: no Vulkan, no Apple API. */
#pragma once
#include "Translator.hpp"
#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <string>
#include <vector>
namespace fuxi {
struct LibraryHash {uint64_t a=0,b=0;bool operator==(const LibraryHash &o) const {return a==o.a && b==o.b;}};
// Two independent 64-bit multiply-xor lanes over 8-byte words plus the tail. Change detection and matching only, not security.
inline LibraryHash libraryHash(const void *data,size_t n) {
 const uint8_t *p=(const uint8_t *)data;uint64_t a=0xcbf29ce484222325ull^n,b=0x9e3779b97f4a7c15ull+n*0x100000001b3ull;size_t i=0;
 for(;i+8<=n;i+=8){uint64_t w;memcpy(&w,p+i,8);a=(a^w)*0x100000001b3ull;a^=a>>29;b=(b+w)*0xff51afd7ed558ccdull;b^=b>>32;}
 for(;i<n;++i){a=(a^p[i])*0x100000001b3ull;b=(b+p[i])*0xc4ceb9fe1a85ec53ull;b^=b>>31;}
 return {a,b};
}
inline const uint8_t *recipeKeyPrefix() {static const uint8_t k[8]={'F','X','R','C','P','0','0','1'};return k;}
inline bool isRecipeKey(const uint8_t *key) {return !memcmp(key,recipeKeyPrefix(),8);}
// 32-byte cache key for a recipe, derived from the translator's 32-byte pipeline identity.
inline std::string recipeKey(const uint8_t *identity) {std::string k(32,'\0');memcpy(&k[0],recipeKeyPrefix(),8);memcpy(&k[8],identity,24);return k;}
// Options without the unused tail of the 2x128 constant tables.
inline void packOptions(const CompileOptions &o,std::vector<uint8_t> &out) {
 const size_t prefix=offsetof(CompileOptions,constants),suffix=offsetof(CompileOptions,orderedColorAccess);
 const uint8_t *p=(const uint8_t *)&o;out.insert(out.end(),p,p+prefix);
 for(unsigned s=0;s<2;++s){size_t n=std::min<size_t>(o.constantCount[s],maxFunctionConstants);const uint8_t *c=(const uint8_t *)&o.constants[s][0];out.insert(out.end(),c,c+n*sizeof(Constant));}
 out.insert(out.end(),p+suffix,p+sizeof(CompileOptions));
}
inline bool unpackOptions(const uint8_t *b,size_t n,CompileOptions &o) {
 const size_t prefix=offsetof(CompileOptions,constants),suffix=offsetof(CompileOptions,orderedColorAccess),tail=sizeof(CompileOptions)-suffix;
 if(n<prefix+tail)return false;o=CompileOptions{};uint8_t *p=(uint8_t *)&o;memcpy(p,b,prefix);
 uint32_t counts[2]={o.constantCount[0],o.constantCount[1]};if(counts[0]>maxFunctionConstants || counts[1]>maxFunctionConstants)return false;
 if(n!=prefix+(counts[0]+size_t(counts[1]))*sizeof(Constant)+tail)return false;
 size_t at=prefix;for(unsigned s=0;s<2;++s){memcpy(&o.constants[s][0],b+at,counts[s]*sizeof(Constant));at+=counts[s]*sizeof(Constant);}
 memcpy(p+suffix,b+at,tail);return true;
}
struct Recipe {LibraryHash vertex,fragment;std::string vertexName,fragmentName;std::vector<uint8_t> options;bool queued=false;};
inline std::vector<uint8_t> encodeRecipe(const Recipe &r) {
 std::vector<uint8_t> out;auto u32=[&](uint32_t v){for(unsigned i=0;i<4;++i)out.push_back(uint8_t(v>>(8*i)));};auto u64=[&](uint64_t v){for(unsigned i=0;i<8;++i)out.push_back(uint8_t(v>>(8*i)));};
 out.insert(out.end(),{'F','X','R','C','0','0','0','1'});u64(r.vertex.a);u64(r.vertex.b);u64(r.fragment.a);u64(r.fragment.b);
 u32(uint32_t(r.vertexName.size()));u32(uint32_t(r.fragmentName.size()));u32(uint32_t(r.options.size()));
 out.insert(out.end(),r.vertexName.begin(),r.vertexName.end());out.insert(out.end(),r.fragmentName.begin(),r.fragmentName.end());out.insert(out.end(),r.options.begin(),r.options.end());
 return out;
}
inline bool decodeRecipe(const uint8_t *b,size_t n,Recipe &r) {
 if(n<8+32+12 || memcmp(b,"FXRC0001",8))return false;auto u32=[&](size_t at){return uint32_t(b[at])|uint32_t(b[at+1])<<8|uint32_t(b[at+2])<<16|uint32_t(b[at+3])<<24;};
 auto u64=[&](size_t at){return uint64_t(u32(at))|uint64_t(u32(at+4))<<32;};
 r.vertex={u64(8),u64(16)};r.fragment={u64(24),u64(32)};size_t vn=u32(40),fn=u32(44),on=u32(48);
 if(vn==0 || fn==0 || vn>256 || fn>256 || on>sizeof(CompileOptions) || n!=52+vn+fn+on)return false;
 r.vertexName.assign((const char *)b+52,vn);r.fragmentName.assign((const char *)b+52+vn,fn);r.options.assign(b+52+vn+fn,b+n);return true;
}
}
