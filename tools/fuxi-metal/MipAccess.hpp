/* Original conservative SPIR-V mip access analysis. No shader names or CA ABI guesses. */
#pragma once
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <functional>
#include <limits>
#include <vector>
namespace fuxi {
struct MipRange {
 uint32_t first=UINT32_MAX,last=0;
 bool empty() const{return first==UINT32_MAX;}
 void add(uint32_t lo,uint32_t hi){first=std::min(first,lo);last=std::max(last,hi);}
 bool contains(uint32_t level) const{return !empty() && first<=level && level<=last;}
};
struct MipSampler {bool known=false;uint32_t mip=0;float lo=0,hi=0;};
// SPIR-V IDs/operands stay private to this bounded interpreter. Unknown data,
// control flow, image provenance or instruction forms widen the result; they
// never select a mip based on attachment level, texture dimensions or names.
class MipAccess {
 // Instructions are views into one flat copy of the module (about 2-3x smaller than a heap vector per instruction); the view is why copying is deleted.
 struct Span {const uint32_t *p=nullptr;size_t n=0;size_t size() const{return n;}const uint32_t &operator[](size_t i) const{return p[i];}const uint32_t *begin() const{return p;}const uint32_t *end() const{return p+n;}};
 struct Inst {Span w;uint32_t op=0;};
 std::vector<uint32_t> store;std::vector<Inst> code;std::vector<uint32_t> defs;std::vector<uint32_t> bindings;
 bool valid=false;
 const Inst *def(uint32_t id) const {return id<defs.size() && defs[id]<code.size()?&code[defs[id]]:nullptr;}
 uint32_t decoration(uint32_t id,uint32_t kind,uint32_t member=UINT32_MAX) const {
  for(auto &i:code){auto &w=i.w;
   if(member==UINT32_MAX && i.op==71 && w.size()>=4 && w[1]==id && w[2]==kind)return w[3];
   if(member!=UINT32_MAX && i.op==72 && w.size()>=5 && w[1]==id && w[2]==member && w[3]==kind)return w[4];
  }return UINT32_MAX;
 }
 uint32_t root(uint32_t id,unsigned depth=0) const {
  if(depth>32)return 0;auto *i=def(id);if(!i)return 0;auto &w=i->w;
  if(i->op==59)return id;
  if((i->op==61 || i->op==83 || i->op==86 || i->op==100) && w.size()>=4)return root(w[3],depth+1);
  return 0;
 }
 uint32_t samplerBinding(uint32_t id,unsigned depth=0) const {
  if(depth>32)return UINT32_MAX;auto *i=def(id);if(!i)return UINT32_MAX;auto &w=i->w;
  if(i->op==86 && w.size()==5)return binding(w[4]);
  if(i->op==83 && w.size()==4)return samplerBinding(w[3],depth+1);
  if(i->op==61 && w.size()>=4){auto *t=def(w[1]);if(t && t->op==27)return binding(id);}
  return UINT32_MAX;
 }
 uint32_t binding(uint32_t id) const {auto r=root(id);return r<bindings.size()?bindings[r]:UINT32_MAX;}
 struct Pointer {uint32_t variable=0,type=0;uint64_t offset=0;};
 using Read=std::function<bool(uint32_t,uint64_t,uint32_t &,bool)>;
 Pointer pointer(uint32_t id,const Read &read,unsigned depth) const {
  if(depth>32)return {};auto *i=def(id);if(!i)return {};auto &w=i->w;
  if(i->op==59 && w.size()>=4){auto *t=def(w[1]);if(t && t->op==32 && t->w.size()==4)return {id,t->w[3],0};return {};}
  if((i->op!=65 && i->op!=66) || w.size()<4)return {};
  auto p=pointer(w[3],read,depth+1);if(!p.variable)return {};
  for(size_t k=4;k<w.size();++k){double index=value(w[k],read,depth+1);if(!std::isfinite(index) || index<0 || index>UINT32_MAX || floor(index)!=index)return {};
   auto *t=def(p.type);if(!t)return {};uint64_t offset=0;uint32_t next=0;
   if(t->op==30){if(index>=t->w.size()-2)return {};offset=decoration(p.type,35,(uint32_t)index);if(offset==UINT32_MAX)return {};next=t->w[2+(uint32_t)index];}
   else if((t->op==28 || t->op==29) && t->w.size()>=3){auto stride=decoration(p.type,6);if(stride==UINT32_MAX)return {};offset=uint64_t(index)*stride;next=t->w[2];}
   else if(t->op==23 && t->w.size()==4){if(index>=t->w[3])return {};auto *scalar=def(t->w[2]);if(!scalar || scalar->w.size()<3 || scalar->w[2]!=32)return {};offset=uint64_t(index)*4;next=t->w[2];}
   else return {};
   if(offset>UINT32_MAX || p.offset>UINT32_MAX-offset)return {};p.offset+=offset;p.type=next;
  }return p;
 }
 double scalar(uint32_t type,uint32_t bits) const {
  auto *t=def(type);if(!t || t->w.size()<3 || t->w[2]!=32)return NAN;
  if(t->op==22){float f;memcpy(&f,&bits,4);return f;}
  if(t->op==21 && t->w.size()==4)return t->w[3]?double(int32_t(bits)):double(bits);
  return NAN;
 }
 double loadValue(const Pointer &p,uint32_t type,const Read &read) const {
  auto *v=def(p.variable);uint32_t bits=0;
  if(!v || v->w.size()<4 || (v->w[3]!=2 && v->w[3]!=9))return NAN;
  bool push=v->w[3]==9;auto b=push?0:binding(p.variable);
  return b!=UINT32_MAX && read && read(b,p.offset,bits,push)?scalar(type,bits):NAN;
 }
 double value(uint32_t id,const Read &read,unsigned depth=0) const {
  if(depth>32)return NAN;auto *i=def(id);if(!i)return NAN;auto &w=i->w;
  if(i->op==43 && w.size()==4)return scalar(w[1],w[3]);
  // Only immutable uniform/push data. Storage buffers, locals, specialization
  // constants, dynamic indexing and unrecognized expressions remain unknown.
  if(i->op==61 && w.size()>=4)return loadValue(pointer(w[3],read,depth+1),w[1],read);
  if(i->op==83 && w.size()==4)return value(w[3],read,depth+1);
  if(i->op==81 && w.size()==5){auto *c=def(w[3]);
   if(c && (c->op==44 || c->op==80) && w[4]<c->w.size()-3)return value(c->w[3+w[4]],read,depth+1);
   if(c && c->op==61 && c->w.size()>=4){auto *t=def(c->w[1]);
    if(t && t->op==23 && t->w.size()==4 && t->w[2]==w[1] && w[4]<t->w[3]){auto p=pointer(c->w[3],read,depth+1);auto *scalarType=def(w[1]);
     if(scalarType && scalarType->w.size()>=3 && scalarType->w[2]==32){p.offset+=uint64_t(w[4])*4;return loadValue(p,w[1],read);}
    }
   }
  }
  return NAN;
 }
public:
 size_t retainedBytes() const {return code.capacity()*sizeof(Inst)+defs.capacity()*sizeof(uint32_t)+bindings.capacity()*sizeof(uint32_t)+store.capacity()*sizeof(uint32_t);}
 MipAccess()=default;MipAccess(const MipAccess &)=delete;MipAccess &operator=(const MipAccess &)=delete;MipAccess(MipAccess &&)=default;MipAccess &operator=(MipAccess &&)=default;
 MipAccess(const uint32_t *words,size_t count){
  if(!words || count<5 || count>4*1024*1024 || words[0]!=0x07230203 || !words[3] || words[3]>1024*1024)return;
  defs.assign(words[3],UINT32_MAX);store.assign(words,words+count);
  for(size_t at=5;at<count;){uint32_t n=words[at]>>16,op=words[at]&65535;if(!n || n>count-at)return;
   Inst i{Span{store.data()+at,n},op};auto &w=i.w;uint32_t result=0;
   if((op>=19 && op<=39) || op==7 || op==11) {if(n>=2)result=w[1];}
   else if((op>=41 && op<=52) || op==59 || op==61 || op==65 || op==66 || (op>=79 && op<=200) || op==245){if(n>=3)result=w[2];}
   if(result){if(result>=defs.size() || defs[result]!=UINT32_MAX)return;defs[result]=uint32_t(code.size());}
   code.push_back(std::move(i));at+=n;
  }
  code.shrink_to_fit();bindings.assign(defs.size(),UINT32_MAX);std::vector<uint32_t> sets(defs.size(),UINT32_MAX);
  for(auto &i:code)if(i.op==71 && i.w.size()>=4 && i.w[1]<defs.size()){
   if(i.w[2]==33)bindings[i.w[1]]=i.w[3];if(i.w[2]==34)sets[i.w[1]]=i.w[3];
  }
  for(size_t id=0;id<bindings.size();++id)if(sets[id]!=0)bindings[id]=UINT32_MAX;
  valid=true;
 }
 // A storage descriptor is read-only only with an explicit NonWritable
 // variable contract in this final module. Missing bindings, malformed modules,
 // decoration groups/member-only contracts and unfamiliar forms stay unknown.
 // This is a declaration proof, not a guess based on observed OpLoad uses.
 bool readOnlyStorage(uint32_t descriptorBinding) const {
  if(!valid)return false;bool found=false;
  for(uint32_t id=0;id<bindings.size();++id)if(bindings[id]==descriptorBinding){
   auto *v=def(id);if(!v || v->op!=59 || v->w.size()<4 || (v->w[3]!=12 && v->w[3]!=2))return false;
   auto *type=def(v->w[1]);if(!type || type->op!=32 || type->w.size()!=4 || type->w[2]!=v->w[3])return false;
   bool nonWritable=false;for(auto &i:code)if(i.op==71 && i.w.size()==3 && i.w[1]==id && i.w[2]==24)nonWritable=true;
   if(!nonWritable)return false;found=true;
  }
  return found;
 }
 // Resolve sampler metadata by SPIR-V binding. Read callback addresses descriptor
 // bytes, or push bytes when its last argument is true, from this exact draw.
 MipRange range(uint32_t imageBinding,uint32_t count,const std::function<MipSampler(uint32_t)> &sampler,const Read &read) const {
  MipRange out;if(!count)return out;auto unknown=[&]{out.add(0,count-1);};
  if(!valid){unknown();return out;}
  bool function=false;
  for(auto &i:code){auto &w=i.w;
   if(i.op==54)function=true;if(i.op==56)function=false;
   // Extensions/function calls/opaque-value stores are not assumed harmless.
   // Detect direct uses of this resource outside supported image/plumbing ops.
   bool imageOp=(i.op>=87 && i.op<=107) || (i.op>=305 && i.op<=320);
   if(function && !imageOp && i.op!=61 && i.op!=83 && i.op!=86 && i.op!=100){
    for(size_t k=1;k<w.size();++k){
     // These operands are literal numbers, not IDs (an ExtInst enumerant can
     // numerically equal the ID of an OpImage in metal2vulkan's output).
     if((i.op==12 && k==4) || (i.op==79 && k>=5) || (i.op==81 && k>=4) || (i.op==82 && k>=5) || (i.op==250 && k>=4) || (i.op==251 && k>=3 && k%2))continue;
     if(binding(w[k])==imageBinding)unknown();
    }
   }
   // Image queries do not read texels. Original views preserve query semantics.
   if(i.op>=101 && i.op<=107)continue;
   if(!((i.op>=87 && i.op<=99) || (i.op>=305 && i.op<=320)))continue;
   if(w.size()<4){unknown();continue;}auto b=binding(w[3]);
   if(b!=imageBinding && b!=UINT32_MAX)continue;
   if(b==UINT32_MAX || i.op<87 || i.op>88 || w.size()<5){unknown();continue;}
   uint32_t sb=samplerBinding(w[3]);
   auto s=sampler(sb);if(!s.known || s.mip>2 || !std::isfinite(s.lo) || !std::isfinite(s.hi) || s.lo<0 || s.hi<s.lo){unknown();continue;}
   double lo=0,hi=0;
   if(s.mip){lo=s.lo;hi=s.hi;
    // OpImageSampleExplicitLod: only the plain Lod form is narrowed by the
    // scalar interpreter. Bias/Grad/MinLod/offset combinations use sampler bounds.
    if(i.op==88 && w.size()==7 && w[5]==2){double lod=value(w[6],read);if(std::isfinite(lod))lo=hi=std::clamp(lod,double(s.lo),double(s.hi));}
   }
   if(!std::isfinite(lo) || !std::isfinite(hi) || lo<0 || hi<lo){unknown();continue;}
   // Both neighboring levels conservatively cover linear and nearest filtering,
   // including implementation rounding at a nearest-level boundary.
   lo=std::min(lo,double(count-1));hi=std::min(hi,double(count-1));
   out.add((uint32_t)floor(lo),(uint32_t)ceil(hi));
  }return out;
 }
};
}
