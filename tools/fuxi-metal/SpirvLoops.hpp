/* Static description of the loops in a SPIR-V module for the pipeline stats log: where the image samples sit and what feeds each loop's exit condition. */
#pragma once
#include <cstddef>
#include <cstdint>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>
namespace fuxi {
struct LoopInfo {unsigned depth=0,insts=0,samples=0,blocks=0;std::string bound;};
// bound: const (only constants), uniform (a load through a Uniform/StorageBuffer/PushConstant variable reaches the exit condition), texel (an image result does),
// local (only function-local state, e.g. a counter), unknown (no conditional exit found). Assumes structured control flow with the merge block after the loop body.
inline std::vector<LoopInfo> spirvLoopReport(const uint32_t *w,size_t n) {
 struct Inst {size_t at;uint32_t op,len;size_t block;};
 std::vector<LoopInfo> loops;if(!w || n<5 || w[0]!=0x07230203)return loops;
 std::vector<Inst> insts;std::unordered_map<uint32_t,size_t> def,blockOf;std::unordered_map<uint32_t,uint32_t> varClass;std::unordered_map<uint32_t,std::vector<uint32_t>> stores;size_t block=0;
 for(size_t i=5;i<n;){uint32_t op=w[i]&0xffffu,len=w[i]>>16;if(!len || i+len>n)return {};
  if(op==248 && len>=2){block=blockOf.size()+1;blockOf[w[i+1]]=block;}
  insts.push_back({i,op,len,block});
  bool hasResult=(op>=19 && op<=39) || (op>=41 && op<=52) || op==54 || op==55 || op==57 || op==59 || op==61 || op==65 || op==66 || op==245 || (op>=79 && op<=200) || op==12;
  if(hasResult){size_t k=(op>=19 && op<=39)?1:2;if(len>k)def[w[i+k]]=insts.size()-1;}
  if(op==59 && len>=4)varClass[w[i+2]]=w[i+3];
  if(op==62 && len>=3)stores[w[i+1]].push_back(w[i+2]);
  i+=len;}
 struct Loop {size_t headerInst,headerBlock,mergeBlock;};std::vector<Loop> found;
 for(size_t k=0;k<insts.size();++k)if(insts[k].op==246 && insts[k].len>=3){auto m=blockOf.find(w[insts[k].at+1]);found.push_back({k,insts[k].block,m==blockOf.end()?SIZE_MAX:m->second});}
 auto rootVar=[&](uint32_t id){for(int d=0;d<16;++d){auto it=def.find(id);if(it==def.end())return uint32_t(0);auto &x=insts[it->second];if(x.op==59)return id;if((x.op==65 || x.op==66) && x.len>=4)id=w[x.at+3];else return uint32_t(0);}return uint32_t(0);};
 for(auto &lp:found){LoopInfo info;if(lp.mergeBlock==SIZE_MAX || lp.mergeBlock<=lp.headerBlock){info.bound="unknown";loops.push_back(info);continue;}
  uint32_t cond=0;
  for(size_t k=lp.headerInst;k<insts.size() && insts[k].block<=lp.headerBlock+2;++k)if(insts[k].op==250 && insts[k].len>=4){cond=w[insts[k].at+1];break;}
  for(auto &x:insts)if(x.block>=lp.headerBlock && x.block<lp.mergeBlock){++info.insts;if(x.op>=87 && x.op<=94)++info.samples;}
  info.blocks=unsigned(lp.mergeBlock-lp.headerBlock);
  for(auto &o:found)if(&o!=&lp && o.headerBlock<lp.headerBlock && lp.headerBlock<o.mergeBlock)++info.depth;
  bool uniform=false,texel=false,local=false,any=false;std::unordered_set<uint32_t> seen;std::vector<std::pair<uint32_t,int>> work;if(cond)work.push_back({cond,0});
  while(!work.empty()){auto [id,d]=work.back();work.pop_back();if(d>12 || !seen.insert(id).second)continue;auto it=def.find(id);if(it==def.end())continue;auto &x=insts[it->second];any=true;
   if(x.op==61 && x.len>=4){uint32_t v=rootVar(w[x.at+3]);if(!v)continue;uint32_t sc=varClass.count(v)?varClass[v]:0;
    if(sc==2 || sc==12 || sc==9)uniform=true;else if(sc==7 || sc==6){local=true;for(uint32_t s:stores[v])work.push_back({s,d+1});}
    continue;}
   if(x.op>=87 && x.op<=95){texel=true;continue;}
   if(x.op==43 || x.op==44 || x.op==48 || x.op==49 || x.op==50 || x.op==51 || x.op==52)continue;
   if(x.op==59)continue;
   for(uint32_t k=1;k<x.len;++k){uint32_t operand=w[x.at+k];if(def.count(operand))work.push_back({operand,d+1});}}
  info.bound=!cond?"unknown":uniform?"uniform":texel?"texel":local?"local":any?"const":"unknown";loops.push_back(info);}
 return loops;
}
}
