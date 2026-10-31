/* FUXI_MTL_HALF_ROUND: metal2vulkan emulates Metal `half` in f32. Without shaderFloat16 (our default) every half result is rounded by its Device form
   r = Select(FOrdEqual(Quantize(x),0), RoundEven(x*2^24)*2^-24, Quantize(x)): OpQuantizeToF16 flushes half denormals, the scaled path keeps them. ir3 lowers that
   to two covs plus cmp, two muls, rnd and sel (7 instructions); on K240 every fragment pipeline has cat3 >= COV/2 and cat2 >= 2*COV, consistent with most COVs being
   this idiom. The pass rewrites the Select (or a bare OpQuantizeToF16) as an OpCopyObject of another id with the same type; the dead pieces are left to the
   driver's DCE, no id is added and only rewritten instructions change length.
   Mode 1 (exact): a rounding of a value already on the half grid is the identity: results of other roundings, half-grid float constants, UnpackHalf2x16,
   and Negate/FAbs/Min/Max/Clamp/Select/Phi/composite moves of such values, Floor/Ceil/Trunc/Round/RoundEven/FSign of them (integers). A bare
   quantize is removed only for values that are never half denormals (its own results, normal constants, and moves of them).
   Mode 2 (flush, lossy where allowed): mode 1 and every other idiom becomes its Quantize(x): differs only where |x| < 2^-14 (0 instead of the denormal grid).
   Mode 3 (f32, lossy where allowed): mode 1 and every idiom and bare quantize becomes x: half arithmetic runs in f32 (more precise than Metal's half, no
   overflow to infinity at 65520). Lossy modes apply to every pipeline or, when lossy is false (allowlist miss), fall back to mode 1. */
#pragma once
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <unordered_map>
#include <unordered_set>
#include <vector>
namespace fuxi {
struct HalfRoundStats {unsigned quantize=0,idioms=0,bare=0,exact=0,exactBare=0,flushed=0,dropped=0,droppedBare=0;
 unsigned before() const {return idioms+bare;} unsigned afterIdioms() const {return idioms-exact-flushed-dropped;} unsigned afterQuantize() const {return afterIdioms()+flushed+bare-exactBare-droppedBare;}};
// 0: not on the half grid; 1: on the grid, maybe a half denormal (the idiom keeps it); 2: on the grid and never a denormal (OpQuantizeToF16 keeps it too).
inline unsigned halfGridClass(float f) {
 if(std::isnan(f))return 0;if(f==0 || std::isinf(f))return 2;float a=std::fabs(f);if(a>65504.f)return 0;
 if(a<6.103515625e-05f){double s=double(a)*16777216.0;return s==std::floor(s)?1:0;}
 int e=0;double m=std::frexp(double(a),&e);double s=std::ldexp(m,11);return s==std::floor(s)?2:0;
}
inline bool simplifyHalfRounding(const uint32_t *w,size_t n,std::vector<uint32_t> &out,HalfRoundStats &st,unsigned mode,bool lossy=true) {
 st={};if(!w || n<5 || w[0]!=0x07230203 || !mode || mode>3)return false;
 std::unordered_map<uint32_t,size_t> def;std::unordered_map<uint32_t,uint32_t> constClass;std::unordered_set<uint32_t> f32,f32Vec;uint32_t glsl=0;
 auto isFloatConst=[&](uint32_t id,uint32_t bits)->bool{auto d=def.find(id);if(d==def.end())return false;size_t i=d->second;uint32_t op=w[i]&0xffffu,len=w[i]>>16;
  if(op==43)return len==4 && f32.count(w[i+1]) && w[i+3]==bits;
  if(op==46)return bits==0 && (f32.count(w[i+1]) || f32Vec.count(w[i+1]));
  if(op!=44 || !f32Vec.count(w[i+1]) || len<4)return false;
  for(size_t k=3;k<len;++k){auto c=def.find(w[i+k]);if(c==def.end())return false;size_t j=c->second;if((w[j]&0xffffu)==46 && bits==0)continue;if((w[j]&0xffffu)!=43 || (w[j]>>16)!=4 || w[j+3]!=bits)return false;}
  return true;};
 for(size_t i=5;i<n;){uint32_t op=w[i]&0xffffu,len=w[i]>>16;if(!len || i+len>n)return false;
  if(op==11 && len>=3 && !memcmp(&w[i+2],"GLSL.std.450",12))glsl=w[i+1];
  else if(op==22 && len>=3){if(w[i+2]==32)f32.insert(w[i+1]);}
  else if(op==23 && len>=4){if(f32.count(w[i+2]))f32Vec.insert(w[i+1]);}
  else if((op==43 || op==44 || op==46 || op==116 || op==180 || op==133 || op==12) && len>=3){def[w[i+2]]=i;if(op==116)++st.quantize;}
  i+=len;}
 if(!st.quantize)return false;
 // Constant classes (f32 scalars, composites and null of f32 types).
 for(auto &d:def){size_t i=d.second;uint32_t op=w[i]&0xffffu,len=w[i]>>16;
  if(op==43 && len==4 && f32.count(w[i+1])){float f;memcpy(&f,&w[i+3],4);constClass[w[i+2]]=halfGridClass(f);}
  else if(op==46 && (f32.count(w[i+1]) || f32Vec.count(w[i+1])))constClass[w[i+2]]=2;}
 for(auto &d:def){size_t i=d.second;uint32_t op=w[i]&0xffffu,len=w[i]>>16;if(op!=44 || !f32Vec.count(w[i+1]))continue;unsigned c=2;
  for(size_t k=3;k<len;++k){auto e=constClass.find(w[i+k]);c=e==constClass.end()?0:std::min(c,unsigned(e->second));}constClass[w[i+2]]=c;}
 // Idiom match on the Select at offset i: returns x or 0; q receives the Quantize(x) id used as the Select's false operand.
 auto quantizeOf=[&](uint32_t id)->uint32_t{auto d=def.find(id);if(d==def.end())return 0;size_t i=d->second;return (w[i]&0xffffu)==116 && (w[i]>>16)==4?w[i+3]:0;};
 auto fmulBy=[&](uint32_t id,uint32_t bits)->uint32_t{auto d=def.find(id);if(d==def.end())return 0;size_t i=d->second;if((w[i]&0xffffu)!=133 || (w[i]>>16)!=5)return 0;
  if(isFloatConst(w[i+4],bits))return w[i+3];if(isFloatConst(w[i+3],bits))return w[i+4];return 0;};
 auto idiom=[&](size_t i,uint32_t &q)->uint32_t{if((w[i]>>16)!=6)return 0;uint32_t cond=w[i+3],dv=w[i+4];q=w[i+5];uint32_t x=quantizeOf(q);if(!x)return 0;
  auto c=def.find(cond);if(c==def.end())return 0;size_t ci=c->second;if((w[ci]&0xffffu)!=180 || (w[ci]>>16)!=5)return 0;
  uint32_t a=w[ci+3],b=w[ci+4];if(!(quantizeOf(a)==x && isFloatConst(b,0)) && !(quantizeOf(b)==x && isFloatConst(a,0)))return 0;
  uint32_t k=fmulBy(dv,0x33800000u);if(!k)return 0;auto kd=def.find(k);if(kd==def.end())return 0;size_t ki=kd->second;
  if((w[ki]&0xffffu)!=12 || (w[ki]>>16)!=6 || w[ki+3]!=glsl || !glsl || w[ki+4]!=2)return 0;
  return fmulBy(w[ki+5],0x4B800000u)==x?x:0;};
 // Idioms first (they depend on definitions only), so a quantize is known to be bare or an idiom's before the forward walk reaches it.
 std::unordered_map<size_t,std::pair<uint32_t,uint32_t>> idioms;std::unordered_set<uint32_t> idiomQuantize;
 for(size_t i=5;i<n;i+=w[i]>>16)if((w[i]&0xffffu)==169){uint32_t q=0,x=idiom(i,q);if(x){idioms[i]={x,q};idiomQuantize.insert(q);uint32_t a=w[def[w[i+3]]+3],b=w[def[w[i+3]]+4];idiomQuantize.insert(quantizeOf(a)?a:b);}}
 st.idioms=unsigned(idioms.size());
 // Forward walk: class of each id (absent/0 unknown, 1 grid, 2 grid without denormals) and the rewrites (offset -> source id). A Phi's back-edge value is unknown.
 std::unordered_map<uint32_t,unsigned> cls(constClass.begin(),constClass.end());std::unordered_map<size_t,uint32_t> rewrite;
 auto C=[&](uint32_t id)->unsigned{auto e=cls.find(id);return e==cls.end()?0:e->second;};
 for(size_t i=5;i<n;){uint32_t op=w[i]&0xffffu,len=w[i]>>16;
  if(op==169 && len==6){auto m=idioms.find(i);uint32_t r=w[i+2];
   if(m!=idioms.end()){uint32_t x=m->second.first,q=m->second.second;
    if(C(x)){rewrite[i]=x;++st.exact;cls[r]=C(x);}
    else if(lossy && mode==2){rewrite[i]=q;++st.flushed;cls[r]=2;}
    else if(lossy && mode==3){rewrite[i]=x;++st.dropped;}
    else cls[r]=1;}
   else{unsigned c=std::min(C(w[i+4]),C(w[i+5]));if(c)cls[r]=c;}}
  else if(op==116 && len==4){uint32_t x=w[i+3],r=w[i+2];
   if(idiomQuantize.count(r))cls[r]=2;
   else{++st.bare;if(C(x)==2){rewrite[i]=x;++st.exactBare;cls[r]=2;}else if(lossy && mode==3){rewrite[i]=x;++st.droppedBare;}else cls[r]=2;}}
  else if((op==127 || op==83) && len==4){if(C(w[i+3]))cls[w[i+2]]=C(w[i+3]);}
  else if(op==81 && len>=5){if(C(w[i+3]))cls[w[i+2]]=C(w[i+3]);}
  else if(op==80 && len>=4){unsigned c=2;for(size_t k=3;k<len;++k)c=std::min(c,C(w[i+k]));if(c)cls[w[i+2]]=c;}
  else if(op==79 && len>=5){bool undef=false;for(size_t k=5;k<len;++k)undef|=w[i+k]==0xffffffffu;unsigned c=undef?0:std::min(C(w[i+3]),C(w[i+4]));if(c)cls[w[i+2]]=c;}
  else if(op==245 && len>=5){unsigned c=2;for(size_t k=3;k+1<len;k+=2)c=std::min(c,C(w[i+k]));if(c)cls[w[i+2]]=c;}
  else if(op==12 && len>=6 && glsl && w[i+3]==glsl){uint32_t e=w[i+4],r=w[i+2];unsigned c=2;for(size_t k=5;k<len;++k)c=std::min(c,C(w[i+k]));
   if(e==62)cls[r]=1;                                                  // UnpackHalf2x16
   else if(c && (e==1 || e==2 || e==3 || e==6 || e==8 || e==9))cls[r]=2; // Round/RoundEven/Trunc/FSign/Floor/Ceil of grid values: integers or zero
   else if(c && (e==4 || e==37 || e==40 || e==43 || e==79 || e==80 || e==81))cls[r]=c;} // FAbs, FMin/FMax/FClamp, NMin/NMax/NClamp
  i+=len;}
 if(rewrite.empty())return false;
 out.clear();out.reserve(n);out.insert(out.end(),w,w+5);
 for(size_t i=5;i<n;){uint32_t len=w[i]>>16;auto r=rewrite.find(i);
  if(r==rewrite.end())out.insert(out.end(),w+i,w+i+len);else{out.push_back(4u<<16|83u);out.push_back(w[i+1]);out.push_back(w[i+2]);out.push_back(r->second);}
  i+=len;}
 return true;
}
}
