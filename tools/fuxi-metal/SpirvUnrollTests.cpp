#include "SpirvUnroll.hpp"
#include <cassert>
#include <cstdio>
#include <fstream>
#include <iterator>
#include <cstring>
using namespace fuxi;
struct Mod {
 std::vector<uint32_t> w{0x07230203,0x10000,0,64,0};
 size_t add(uint32_t op,std::initializer_list<uint32_t> a){w.push_back(uint32_t(a.size()+1)<<16|op);w.insert(w.end(),a);return w.size()-a.size();}
};
static std::vector<uint32_t> load(const char *path){std::ifstream in(path,std::ios::binary);std::vector<char> b{std::istreambuf_iterator<char>(in),{}};assert(b.size()>=20 && b.size()%4==0);std::vector<uint32_t> w(b.size()/4);memcpy(w.data(),b.data(),b.size());return w;}
int main(int argc,char **argv){
 Mod m;size_t ctl[5];
 m.add(248,{10});ctl[0]=m.add(246,{11,12,2})+2;m.add(248,{20});ctl[1]=m.add(246,{21,22,0})+2;m.add(248,{21});m.add(248,{11}); // A: DontUnroll, holds the plain loop B
 m.add(248,{30});ctl[2]=m.add(246,{31,32,2})+2;m.add(248,{33});m.add(87,{1,2,3,4});m.add(248,{31});                              // C: DontUnroll with a sample (anisotropic taps)
 m.add(248,{40});ctl[3]=m.add(246,{41,42,1})+2;m.add(248,{41});                                                                    // D: already Unroll
 m.add(248,{50});ctl[4]=m.add(246,{51,52,2|4})+2;m.add(248,{51});                                                                  // E: DontUnroll|DependencyInfinite
 m.add(248,{60});m.add(246,{61,62,0});m.add(248,{61});                                                                             // F: plain, not nested
 std::vector<uint32_t> out;UnrollStats st;
 assert(unrollRolledLoops(m.w.data(),m.w.size(),out,st,1));
 assert(st.loops==6 && st.dontUnroll==3 && st.flipped==2 && st.keptSampling==1 && st.nested==0 && out.size()==m.w.size());
 assert(out[ctl[0]]==1 && out[ctl[1]]==0 && out[ctl[2]]==2 && out[ctl[3]]==1 && out[ctl[4]]==(1|4));
 for(size_t i=0;i<out.size();++i)if(i!=ctl[0] && i!=ctl[4])assert(out[i]==m.w[i]); // only the two control words changed
 assert(unrollRolledLoops(m.w.data(),m.w.size(),out,st,2) && st.flipped==2 && st.nested==1 && out[ctl[1]]==1 && out[ctl[0]]==1);
 {size_t f=0;for(size_t i=5;i<out.size();i+=out[i]>>16)if((out[i]&0xffff)==246 && out[i+1]==61)f=i+3;assert(f && out[f]==0);} // the loop outside every flipped loop keeps its control
 assert(!unrollRolledLoops(m.w.data(),m.w.size(),out,st,0));
 Mod plain;plain.add(248,{10});plain.add(246,{11,12,0});plain.add(248,{11});
 assert(!unrollRolledLoops(plain.w.data(),plain.w.size(),out,st,2) && st.loops==1 && !st.flipped);
 Mod sampling;sampling.add(248,{10});sampling.add(246,{11,12,2});sampling.add(87,{1,2,3,4});sampling.add(248,{11});
 assert(!unrollRolledLoops(sampling.w.data(),sampling.w.size(),out,st,1) && st.keptSampling==1);
 auto bad=m.w;bad.push_back(5u<<16|17);assert(!unrollRolledLoops(bad.data(),bad.size(),out,st,1));
 bad=m.w;bad[0]=0;assert(!unrollRolledLoops(bad.data(),bad.size(),out,st,1));
 assert(!unrollRolledLoops(nullptr,0,out,st,1));
 if(argc>1){ // glslang output of [[dont_unroll]] loops: the flip is found in real modules
  auto w=load(argv[1]);assert(unrollRolledLoops(w.data(),w.size(),out,st,1) && st.flipped>=1 && out.size()==w.size());
  size_t diff=0;for(size_t i=0;i<w.size();++i)diff+=w[i]!=out[i];assert(diff==st.flipped);
  printf("module %s: loops=%u dont_unroll=%u flipped=%u\n",argv[1],st.loops,st.dontUnroll,st.flipped);}
 puts("PASS: DontUnroll -> Unroll on sample-free loops (mode 2 nests), control words only, refusals");
}
