#include "SpirvRelax.hpp"
#include <cassert>
#include <cstdio>
using namespace fuxi;
struct Mod {
 std::vector<uint32_t> w{0x07230203,0x10000,0,32,0};
 void add(uint32_t op,std::initializer_list<uint32_t> a){w.push_back(uint32_t(a.size()+1)<<16|op);w.insert(w.end(),a);}
};
int main(){
 Mod m;m.add(17,{1});m.add(5,{9,0x61});m.add(71,{9,0});m.add(22,{1,32});m.add(129,{1,10,3,4});m.add(116,{1,11,10});m.add(133,{1,12,11,11});m.add(116,{1,9,12});m.add(129,{1,13,3,4});
 std::vector<uint32_t> out;unsigned marked=0,quantized=0;
 assert(relaxQuantizedChains(m.w.data(),m.w.size(),out,marked,quantized));
 assert(quantized==2 && marked==3 && out.size()==m.w.size()+9);
 // Decorations land after the existing one and before the first type; id 9 is not decorated twice; the unrelated FAdd 13 stays unmarked.
 bool seenType=false;std::vector<uint32_t> dec;
 for(size_t i=5;i<out.size();){uint32_t op=out[i]&0xffff,len=out[i]>>16;if(op==22)seenType=true;if(op==71){assert(!seenType);if(out[i+2]==0)dec.push_back(out[i+1]);}i+=len;}
 assert(dec.size()==4 && dec[0]==9 && dec[1]==11 && dec[2]==10 && dec[3]==12);
 // Everything except the inserted words is untouched.
 size_t first=5;while((m.w[first]&0xffff)!=22)first+=m.w[first]>>16;
 for(size_t i=0;i<first;++i)assert(out[i]==m.w[i]);
 for(size_t i=first;i<m.w.size();++i)assert(out[i+9]==m.w[i]);
 Mod none;none.add(17,{1});none.add(22,{1,32});none.add(129,{1,10,3,4});
 assert(!relaxQuantizedChains(none.w.data(),none.w.size(),out,marked,quantized) && marked==0);
 auto bad=m.w;bad.back()=0;bad.push_back(0);assert(!relaxQuantizedChains(bad.data(),bad.size(),out,marked,quantized));
 assert(!relaxQuantizedChains(nullptr,0,out,marked,quantized));
 puts("PASS: relaxed-precision marking of quantized f16 chains (placement, dedupe, untouched words, refusal)");
}
