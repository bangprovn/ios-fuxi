#include "SpirvLoops.hpp"
#include <cassert>
#include <cstdio>
using namespace fuxi;
struct Mod {
 std::vector<uint32_t> w{0x07230203,0x10000,0,64,0};
 void add(uint32_t op,std::initializer_list<uint32_t> a){w.push_back(uint32_t(a.size()+1)<<16|op);w.insert(w.end(),a);}
};
int main(){
 Mod m;
 m.add(22,{3,32});m.add(20,{7});m.add(32,{4,2,3});m.add(59,{4,5,2});m.add(43,{3,6,4});m.add(59,{4,40,7});m.add(54,{1,10,0,2});
 // loop 0: constant exit, one sample site
 m.add(248,{11});m.add(249,{12});
 m.add(248,{12});m.add(246,{15,14,0});m.add(177,{7,20,22,6});m.add(250,{20,13,15});
 m.add(248,{13});m.add(87,{3,30,31,32});m.add(249,{14});
 m.add(248,{14});m.add(249,{12});
 // loop 1: exit compares against a value loaded from a Uniform variable, two sample sites
 m.add(248,{15});m.add(249,{16});
 m.add(248,{16});m.add(246,{19,18,0});m.add(61,{3,23,5});m.add(177,{7,24,22,23});m.add(250,{24,17,19});
 m.add(248,{17});m.add(87,{3,33,31,32});m.add(87,{3,34,31,32});m.add(249,{18});
 m.add(248,{18});m.add(249,{16});
 // loop 2 nested in nothing: exit depends on a function-local counter that is stored from a constant
 m.add(248,{19});m.add(249,{26});
 m.add(248,{26});m.add(246,{29,28,0});m.add(61,{3,25,40});m.add(177,{7,27,25,6});m.add(250,{27,41,29});
 m.add(248,{41});m.add(62,{40,6});m.add(249,{28});
 m.add(248,{28});m.add(249,{26});
 m.add(248,{29});m.add(253,{});m.add(56,{});
 auto loops=spirvLoopReport(m.w.data(),m.w.size());
 assert(loops.size()==3);
 assert(loops[0].bound=="const" && loops[0].samples==1 && loops[0].depth==0 && loops[0].blocks==3);
 assert(loops[1].bound=="uniform" && loops[1].samples==2 && loops[1].depth==0);
 assert(loops[2].bound=="local" && loops[2].samples==0);
 assert(spirvLoopReport(nullptr,0).empty());
 auto bad=m.w;bad.push_back(0);assert(spirvLoopReport(bad.data(),bad.size()).empty());
 puts("PASS: SPIR-V loop report (sample sites per loop, constant/uniform/local exit conditions, malformed refusal)");
}
