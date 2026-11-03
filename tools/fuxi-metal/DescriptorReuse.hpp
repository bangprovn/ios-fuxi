/* FUXI_MTL_DESC_REUSE: descriptor sets kept across submits. The index and the pool-reset rule; Backend owns the Vulkan objects. */
#pragma once
#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <unordered_map>
#include <vector>
namespace fuxi {
// A set is written once, when it is made, and never updated, so binding it again in a later command buffer only reads descriptors.
// Three rules keep that read the one a fresh set would give:
//  - identity: an entry matches only the same set layout handle, binding count and written values (per binding: binding, type, sampler,
//    image view, image layout, buffer, offset, range), compared in full; the hash only picks candidates;
//  - lifetime: every Vulkan handle an entry names (layout, samplers, views, buffers) is indexed, and Backend's destroy paths call forget()
//    before the handle is destroyed, so no later object that the driver gives the same handle value can match a set written for the old one;
//  - pending use: sets live in two pools taken in turn; a pool is reset only when idle(), i.e. its last bind is older than every pending
//    command buffer (the busy flights and the one being recorded). Rotating to the other pool drops every entry first.
template<class Value,class Set,size_t N=16> class DescriptorReuse {
public:
 struct Entry {uint64_t layout;unsigned count;uint64_t hash;std::array<Value,N> values;Set set;bool dead;};
 struct Pool {bool made=false;uint64_t lastUse=0;uint32_t sets=0;};
 std::array<Pool,2> pools;unsigned current=0;
 std::vector<Entry> entries;std::unordered_multimap<uint64_t,size_t> index,byHandle;
 // The live entry with exactly these values, or nullptr; probes counts the entries compared.
 const Entry *find(uint64_t layout,unsigned count,uint64_t hash,const Value *values,uint64_t &probes) const {
  if(count>N)return nullptr;
  auto range=index.equal_range(hash);
  for(auto it=range.first;it!=range.second;++it){++probes;const auto &e=entries[it->second];
   if(!e.dead && e.layout==layout && e.count==count && std::equal(values,values+count,e.values.begin()))return &e;}
  return nullptr;
 }
 // A set made in the current pool; handles lists what it names (zeros skipped, duplicates indexed once).
 void insert(uint64_t layout,unsigned count,uint64_t hash,const Value *values,Set set,const uint64_t *handles,size_t handleCount) {
  size_t at=entries.size();Entry e{layout,count,hash,{},set,false};std::copy(values,values+std::min<size_t>(count,N),e.values.begin());
  entries.push_back(e);index.emplace(hash,at);++pools[current].sets;
  for(size_t i=0;i<handleCount;++i){if(!handles[i] || std::find(handles,handles+i,handles[i])!=handles+i)continue;byHandle.emplace(handles[i],at);}
 }
 // Drops every entry that names handle (called before the handle is destroyed); returns how many.
 unsigned forget(uint64_t handle) {
  auto range=byHandle.equal_range(handle);if(range.first==range.second)return 0;unsigned n=0;
  for(auto it=range.first;it!=range.second;++it){auto &e=entries[it->second];if(e.dead)continue;e.dead=true;++n;
   auto r=index.equal_range(e.hash);for(auto j=r.first;j!=r.second;++j)if(j->second==it->second){index.erase(j);break;}}
  byHandle.erase(range.first,range.second);return n;
 }
 size_t live() const {return index.size();}
 // No command buffer that may have bound one of p's sets is pending: oldestPending is the serial of the oldest busy flight, or of the
 // command buffer being recorded when none is busy. A bind from p stamps lastUse with the recording serial.
 static bool idle(const Pool &p,uint64_t oldestPending){return p.lastUse<oldestPending;}
 void clear(){entries.clear();index.clear();byHandle.clear();}
};
}
