/* FUXI_MTL_FRAG_RELAXED=1: the translator emulates Metal half math in f32 and rounds with OpQuantizeToF16 after each half operation. Marking the quantized
 * result and the arithmetic that feeds it RelaxedPrecision lets the driver evaluate that arithmetic in 16-bit ALUs (half registers) without changing what a
 * correctly rounded f16 operation produces. No ids are added: the pass only inserts OpDecorate <id> RelaxedPrecision before the first type declaration. */
#pragma once
#include <cstddef>
#include <cstdint>
#include <unordered_set>
#include <unordered_map>
#include <vector>
namespace fuxi {
// Returns true and fills out when at least one decoration was added; marked counts them, quantized counts the OpQuantizeToF16 sites seen.
inline bool relaxQuantizedChains(const uint32_t *w,size_t n,std::vector<uint32_t> &out,unsigned &marked,unsigned &quantized) {
 marked=quantized=0;if(!w || n<5 || w[0]!=0x07230203)return false;
 auto arithmetic=[](uint32_t op){return op==12 || op==127 || op==129 || op==131 || op==133 || op==136 || op==140 || op==141 || op==142 || op==143 || op==148;};
 std::unordered_set<uint32_t> relaxed;std::unordered_map<uint32_t,uint32_t> arithmeticDefs;std::vector<std::pair<uint32_t,uint32_t>> quantizes;size_t insertAt=0;
 for(size_t i=5;i<n;){uint32_t op=w[i]&0xffffu,len=w[i]>>16;if(!len || i+len>n)return false;
  if(!insertAt && op>=19 && op<=70)insertAt=i;
  if(op==71 && len>=3 && w[i+2]==0)relaxed.insert(w[i+1]);
  else if(op==116 && len==4){quantizes.push_back({w[i+2],w[i+3]});++quantized;}
  else if(arithmetic(op) && len>=3)arithmeticDefs[w[i+2]]=op;
  i+=len;}
 if(!insertAt || quantizes.empty())return false;
 std::vector<uint32_t> targets;std::unordered_set<uint32_t> seen;
 auto add=[&](uint32_t id){if(!relaxed.count(id) && seen.insert(id).second)targets.push_back(id);};
 for(auto &q:quantizes){add(q.first);if(arithmeticDefs.count(q.second))add(q.second);}
 if(targets.empty())return false;
 out.clear();out.reserve(n+targets.size()*3);out.insert(out.end(),w,w+insertAt);
 for(uint32_t id:targets){out.push_back(3u<<16|71u);out.push_back(id);out.push_back(0);}
 out.insert(out.end(),w+insertAt,w+n);marked=unsigned(targets.size());return true;
}
}
