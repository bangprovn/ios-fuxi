#include "SpirvHalfRound.hpp"
#include <cassert>
#include <cstdio>
#include <fstream>
#include <iterator>
#include <string>
using namespace fuxi;
struct Mod {
 std::vector<uint32_t> w{0x07230203,0x10000,0,200,0};
 void add(uint32_t op,std::initializer_list<uint32_t> a){w.push_back(uint32_t(a.size()+1)<<16|op);w.insert(w.end(),a);}
 uint32_t next=100;
 // metal2vulkan's Device-form rounding of x (type t; zero/up/down constants of the same shape); swapped writes every commutative operand the other way round.
 uint32_t round(uint32_t t,uint32_t bt,uint32_t x,uint32_t zero,uint32_t up,uint32_t down,bool swapped=false){uint32_t q=next++,c=next++,s=next++,k=next++,d=next++,r=next++;
  add(116,{t,q,x});add(180,swapped?std::initializer_list<uint32_t>{bt,c,zero,q}:std::initializer_list<uint32_t>{bt,c,q,zero});add(133,swapped?std::initializer_list<uint32_t>{t,s,up,x}:std::initializer_list<uint32_t>{t,s,x,up});
  add(12,{t,k,1,2,s});add(133,swapped?std::initializer_list<uint32_t>{t,d,down,k}:std::initializer_list<uint32_t>{t,d,k,down});add(169,{t,r,c,d,q});return r;}
};
static uint32_t fbits(float f){uint32_t u;memcpy(&u,&f,4);return u;}
// The rewritten definition of id in out: returns the CopyObject source, 0 when id keeps its original instruction.
static uint32_t copied(const std::vector<uint32_t> &out,uint32_t id){for(size_t i=5;i<out.size();i+=out[i]>>16){uint32_t op=out[i]&0xffff;if(op==83 && out[i+2]==id)return out[i+3];}return 0;}
static std::vector<uint32_t> load(const char *path){std::ifstream in(path,std::ios::binary);std::vector<char> b{std::istreambuf_iterator<char>(in),{}};assert(b.size()>=20 && b.size()%4==0);std::vector<uint32_t> w(b.size()/4);memcpy(w.data(),b.data(),b.size());return w;}
int main(int argc,char **argv){
 assert(halfGridClass(0.5f)==2 && halfGridClass(65504.f)==2 && halfGridClass(65505.f)==0 && halfGridClass(0.1f)==0 && halfGridClass(1.0009765625f)==2 && halfGridClass(1.00048828125f)==0);
 assert(halfGridClass(std::ldexp(1.f,-20))==1 && halfGridClass(std::ldexp(3.f,-24))==1 && halfGridClass(std::ldexp(1.f,-25))==0 && halfGridClass(-0.f)==2 && halfGridClass(INFINITY)==2 && halfGridClass(NAN)==0);
 Mod m;
 m.add(11,{1,0x4c534c47,0x6474732e,0x3035342e,0}); // %1 = "GLSL.std.450"
 m.add(22,{2,32});m.add(20,{3});m.add(23,{4,2,2});m.add(23,{5,3,2});
 m.add(43,{2,10,0});m.add(43,{2,11,0x4B800000});m.add(43,{2,12,0x33800000});m.add(43,{2,13,fbits(.5f)});m.add(43,{2,14,fbits(.1f)});m.add(43,{2,16,fbits(std::ldexp(1.f,-20))});
 m.add(44,{4,40,10,10});m.add(44,{4,41,11,11});m.add(44,{4,42,12,12});m.add(46,{4,43});
 m.add(43,{2,17,fbits(1.f)});m.add(129,{2,20,14,17});                     // x = 0.1 + 1, unknown
 uint32_t a=m.round(2,3,20,10,11,12);                                      // A: kept / flushed / dropped
 uint32_t b=m.round(2,3,a,10,11,12);                                       // B: rounding of a rounding
 uint32_t c=m.round(2,3,13,10,11,12);                                      // C: grid constant
 uint32_t d=m.round(2,3,14,10,11,12);                                      // D: off-grid constant
 m.add(12,{2,21,1,8,a});uint32_t e=m.round(2,3,21,10,11,12);               // E: Floor of a rounding
 m.add(116,{2,22,20});m.add(116,{2,23,22});                               // F: bare Quantize(Quantize(x))
 m.add(116,{2,24,16});                                                     // G: bare quantize of a half denormal constant
 uint32_t h=m.round(2,3,16,10,11,12);                                      // H: idiom of the denormal constant
 uint32_t i=m.round(2,3,20,10,11,12,true);                                 // I: operands swapped
 m.add(169,{2,25,30,13,14});                                               // J: a plain select
 m.add(80,{4,26,a,20});uint32_t v=m.round(4,5,26,40,41,42);               // vector idiom (composite constants)
 m.add(80,{4,27,a,b});uint32_t vz=m.round(4,5,27,43,41,42);               // vector idiom of grid lanes, null zero
 m.add(43,{2,18,0x3c003c00});m.add(12,{4,28,1,62,18});uint32_t u=m.round(4,5,28,40,41,42); // UnpackHalf2x16
 m.add(245,{2,29,a,90,99,91});uint32_t ph=m.round(2,3,29,10,11,12);       // Phi with a back-edge value: unknown
 m.add(127,{2,31,c});m.add(12,{2,32,1,43,31,13,17});uint32_t cl=m.round(2,3,32,10,11,12); // FClamp(-c,0.5,1)
 std::vector<uint32_t> out;HalfRoundStats st;
 // Mode 1: exact rewrites only.
 assert(simplifyHalfRounding(m.w.data(),m.w.size(),out,st,1));
 assert(st.quantize==15 && st.idioms==12 && st.bare==3 && st.exact==7 && st.exactBare==1 && !st.flushed && !st.dropped && !st.droppedBare);
 assert(!copied(out,a) && copied(out,b)==a && copied(out,c)==13 && !copied(out,d) && copied(out,e)==21 && !copied(out,22) && copied(out,23)==22 && !copied(out,24) && copied(out,h)==16 && !copied(out,i));
 assert(!copied(out,25) && !copied(out,v) && copied(out,vz)==27 && copied(out,u)==28 && !copied(out,ph) && copied(out,cl)==32);
 assert(out.size()==m.w.size()-2*7 && st.before()==15 && st.afterIdioms()==5 && st.afterQuantize()==7);
 // Mode 2: the rest of the idioms keep only their quantize.
 assert(simplifyHalfRounding(m.w.data(),m.w.size(),out,st,2));
 assert(st.exact==7 && st.flushed==5 && !st.dropped && st.exactBare==1 && st.afterIdioms()==0);
 {auto q=[&](uint32_t r){for(size_t k=5;k<m.w.size();k+=m.w[k]>>16)if((m.w[k]&0xffff)==169 && m.w[k+2]==r)return m.w[k+5];return 0u;};
  assert(copied(out,a)==q(a) && copied(out,d)==q(d) && copied(out,i)==q(i) && copied(out,v)==q(v) && copied(out,b)==a && copied(out,ph)==q(ph));}
 // Mode 3: everything not exact computes in f32; roundings of dropped values are no longer exact (B, E, the vectors built on A).
 assert(simplifyHalfRounding(m.w.data(),m.w.size(),out,st,3));
 assert(st.dropped==8 && st.exact==4 && st.droppedBare==3 && !st.exactBare && st.afterQuantize()==0);
 assert(copied(out,a)==20 && copied(out,b)==a && copied(out,e)==21 && copied(out,22)==20 && copied(out,23)==22 && copied(out,24)==16 && copied(out,c)==13 && copied(out,vz)==27 && copied(out,cl)==32);
 // Allowlist miss: lossy modes fall back to the exact rewrites.
 std::vector<uint32_t> exact;assert(simplifyHalfRounding(m.w.data(),m.w.size(),exact,st,1));
 assert(simplifyHalfRounding(m.w.data(),m.w.size(),out,st,3,false) && out==exact && !st.dropped);
 assert(simplifyHalfRounding(m.w.data(),m.w.size(),out,st,2,false) && out==exact && !st.flushed);
 // Refusals: off, unknown mode, no quantize, malformed, nothing to rewrite.
 assert(!simplifyHalfRounding(m.w.data(),m.w.size(),out,st,0) && !simplifyHalfRounding(m.w.data(),m.w.size(),out,st,4));
 Mod none;none.add(22,{2,32});none.add(129,{2,20,14,17});assert(!simplifyHalfRounding(none.w.data(),none.w.size(),out,st,3) && !st.quantize);
 Mod keep;keep.add(22,{2,32});keep.add(116,{2,20,19});assert(!simplifyHalfRounding(keep.w.data(),keep.w.size(),out,st,1) && st.bare==1);
 auto bad=m.w;bad.push_back(9u<<16|17);assert(!simplifyHalfRounding(bad.data(),bad.size(),out,st,1));
 bad=m.w;bad[0]=0;assert(!simplifyHalfRounding(bad.data(),bad.size(),out,st,1));
 assert(!simplifyHalfRounding(nullptr,0,out,st,1));
 // Real modules (spirv-as output of the harness's metal2vulkan-shaped shader): each mode's output is written for spirv-val.
 for(int k=1;k+1<argc;k+=2){auto w=load(argv[k]);
  for(unsigned mode=1;mode<=3;++mode){assert(simplifyHalfRounding(w.data(),w.size(),out,st,mode));
   printf("module %s mode=%u quantize=%u->%u idioms=%u->%u bare=%u exact=%u exact_bare=%u flushed=%u dropped=%u dropped_bare=%u words=%zu->%zu\n",argv[k],mode,st.quantize,st.afterQuantize(),st.idioms,st.afterIdioms(),st.bare,st.exact,st.exactBare,st.flushed,st.dropped,st.droppedBare,w.size(),out.size());
   assert(st.idioms>=20 && (mode==3?st.exact>=2 && st.dropped+st.exact==st.idioms && !st.afterQuantize():st.exact>=4 && st.exactBare>=1));
   std::string path=std::string(argv[k+1])+"."+std::to_string(mode)+".spv";std::ofstream o(path,std::ios::binary);o.write((const char *)out.data(),std::streamsize(out.size()*4));}}
 puts("PASS: half rounding idiom match, exact grid propagation, flush/f32 lossy modes, allowlist fallback, refusals");
}
