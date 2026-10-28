#include "MipAccess.hpp"
#include <cassert>
#include <cstdio>
using namespace fuxi;
struct Module {
 std::vector<uint32_t> words{0x07230203,0x10000,0,128,0};
 void add(uint32_t op,std::initializer_list<uint32_t> args){words.push_back(uint32_t(args.size()+1)<<16|op);words.insert(words.end(),args);}
 Module(){
  add(27,{4,60});add(22,{1,32});add(21,{2,32,0});add(32,{3,0,4});add(59,{3,5,0});add(71,{5,34,0});add(71,{5,33,7});
  add(61,{4,6,5});add(43,{1,10,0});add(43,{1,11,0x3f800000});add(43,{1,12,0x40000000});
 }
 MipRange range(uint32_t mip,float lo,float hi,const std::function<bool(uint32_t,uint64_t,uint32_t &,bool)> &read={}){
  return MipAccess(words.data(),words.size()).range(7,10,[&](uint32_t b){assert(b==7);return MipSampler{true,mip,lo,hi};},read?read:[](uint32_t,uint64_t,uint32_t &,bool){return false;});
 }
};
int main(){
 Module implicit;implicit.add(87,{20,21,6,30});auto r=implicit.range(0,3,4);assert(r.first==0 && r.last==0);
 r=implicit.range(2,2,3);assert(r.first==2 && r.last==3 && !r.contains(1));
 assert(implicit.range(1,3,2).contains(1));
 r=implicit.range(1,0,1000);assert(r.first==0 && r.last==9);
 Module explicitLod;explicitLod.add(88,{20,21,6,30,2,12});r=explicitLod.range(2,0,1000);assert(r.first==2 && r.last==2);
 explicitLod.add(88,{20,22,6,30,2,10});r=explicitLod.range(1,0,1000);assert(r.first==0 && r.last==2 && r.contains(1));
 // Unknown LOD/control flow, fetch (sampler clamps do not apply), malformed
 // streams and unknown image provenance cannot prove disjoint access.
 Module unknown;unknown.add(88,{20,21,6,30,2,99});assert(unknown.range(1,0,1000).contains(1));
 Module fetch;fetch.add(95,{20,21,6,30,2,12});assert(fetch.range(0,0,0).contains(1));
 Module opaque;opaque.add(87,{20,21,99,30});assert(opaque.range(0,0,0).contains(1));
 auto malformed=implicit.words;malformed.back()=0;malformed.push_back(0);auto invalid=MipAccess(malformed.data(),malformed.size()).range(7,10,[](uint32_t){return MipSampler{};},{});assert(invalid.contains(1));
 // Exact draw-time uniform and push scalar addresses; unknown data widens.
 for(bool push:{false,true}){Module uniform;uint32_t storage=push?9:2;
  uniform.add(30,{40,1});uniform.add(72,{40,0,35,16});uniform.add(32,{41,storage,40});uniform.add(32,{42,storage,1});uniform.add(59,{41,43,storage});
  uniform.add(71,{43,34,0});uniform.add(71,{43,33,8});uniform.add(43,{2,44,0});uniform.add(65,{42,45,43,44});uniform.add(61,{1,46,45});uniform.add(88,{20,21,6,30,2,46});
  r=uniform.range(1,0,1000,[&](uint32_t b,uint64_t offset,uint32_t &bits,bool p){assert(p==push && b==(push?0u:8u) && offset==16);bits=0x40000000;return true;});assert(r.first==2 && r.last==2);
  assert(uniform.range(1,0,1000).contains(1));
 }
 // Sampler provenance survives copies of a separate OpSampledImage. The
 // texture slot must not accidentally supply another sampler's clamp range.
 Module separate;separate.add(26,{60});separate.add(32,{61,0,60});separate.add(59,{61,62,0});separate.add(71,{62,34,0});separate.add(71,{62,33,8});separate.add(61,{60,63,62});separate.add(86,{4,64,6,63});separate.add(83,{4,65,64});separate.add(87,{20,21,65,30});
 r=MipAccess(separate.words.data(),separate.words.size()).range(7,10,[](uint32_t b){assert(b==8);return MipSampler{true,0,0,0};},{});assert(r.first==0 && r.last==0);
 Module literal;literal.add(54,{50,51,0,52});literal.add(87,{20,21,6,30});literal.add(12,{1,40,41,6,10});literal.add(56,{});assert(!literal.range(0,0,0).contains(1));
 // An actual opaque resource operand of an unknown extension widens, whereas
 // the ExtInst instruction-number literal above (also 6) is not an ID.
 literal.add(54,{50,53,0,52});literal.add(5283,{1,42,6});literal.add(56,{});assert(literal.range(0,0,0).contains(1));
 Module query;query.add(106,{2,20,6});assert(query.range(1,0,1000).empty());
 Module vector;vector.add(23,{60,1,4});vector.add(30,{40,60});vector.add(72,{40,0,35,16});vector.add(32,{41,2,40});vector.add(32,{42,2,60});vector.add(59,{41,43,2});vector.add(71,{43,34,0});vector.add(71,{43,33,8});vector.add(43,{2,44,0});vector.add(65,{42,45,43,44});vector.add(61,{60,46,45});vector.add(81,{1,47,46,1});vector.add(88,{20,21,6,30,2,47});
 r=vector.range(1,0,1000,[](uint32_t b,uint64_t off,uint32_t &bits,bool push){assert(b==8 && off==20 && !push);bits=0x40000000;return true;});assert(r.first==2 && r.last==2);
 // Nonzero texture view bases are added by the caller exactly once.
 r=implicit.range(0,0,0);MipRange absolute;absolute.add(3+r.first,3+r.last);assert(absolute.contains(3) && !absolute.contains(2));
 // Read-only storage declarations are stage/binding exact and fail closed.
 Module storage;storage.add(30,{70,2});storage.add(32,{71,12,70});storage.add(59,{71,72,12});storage.add(71,{72,34,0});storage.add(71,{72,33,9});
 auto readonly=[&]{return MipAccess(storage.words.data(),storage.words.size()).readOnlyStorage(9);};assert(!readonly());
 storage.add(72,{70,0,24});assert(!readonly()); // member-only is conservatively unknown
 storage.add(71,{72,24});assert(readonly());assert(!MipAccess(storage.words.data(),storage.words.size()).readOnlyStorage(10));
 auto wrongSet=storage;
 wrongSet.add(71,{72,34,1});assert(!MipAccess(wrongSet.words.data(),wrongSet.words.size()).readOnlyStorage(9));
 storage.add(59,{71,73,12});storage.add(71,{73,34,0});storage.add(71,{73,33,9});assert(!readonly()); // another writable variable at the same binding
 storage.words.push_back(0);assert(!readonly());assert(!MipAccess().readOnlyStorage(9));
 // May-write analysis: loads and access chains do not write; stores, atomics, copies and calls through a derived pointer do; absent bindings are untouched.
 {Module rw;rw.add(30,{70,2});rw.add(32,{71,12,70});rw.add(59,{71,72,12});rw.add(71,{72,34,0});rw.add(71,{72,33,9});
  auto may=[&](uint32_t b){return MipAccess(rw.words.data(),rw.words.size()).mayWriteBinding(b);};
  assert(!may(9));rw.add(61,{4,80,72});assert(!may(9));rw.add(65,{71,81,72,10});rw.add(61,{4,82,81});assert(!may(9));assert(!may(10));
  auto reads=rw;rw.add(62,{81,82});assert(may(9));
  rw=reads;rw.add(233,{4,83,81,1,0,82});assert(may(9)); // atomic through the chain
  rw=reads;rw.add(57,{4,84,60,81});assert(may(9)); // pointer passed to a function
  rw=reads;rw.add(63,{81,85});assert(may(9)); // copy memory
  assert(MipAccess().mayWriteBinding(9));}
 puts("PASS: mip access constants/uniforms/push/clamps, unions, rebased views, unknown/fetch/malformed refusal");
}
