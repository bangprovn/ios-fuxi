/* Original process/device-only LRU. Caller serializes access and releases refs.
 * Cache owns one reference; acquire owns another. Eviction never revokes users. */
#pragma once
#include <list>
#include <unordered_set>
#include <string>
#include <cstddef>
#include <utility>
namespace fuxi {
template<class T,size_t Entries=32,size_t Bytes=16*1024*1024> class ObjectCache {
 struct Entry {std::string input,product;T *object;size_t bytes;};
 std::list<Entry> entries;
 size_t bytes=0;
 // Input keys recently pushed out by the LRU: lets a later miss say whether it is a first sight or a rebuild of an evicted object.
 std::unordered_set<std::string> evictedKeys;
public:
 static constexpr size_t maxEntries=Entries,maxBytes=Bytes;
 size_t evictions=0,byteLimit=Bytes;
 bool wasEvicted(const std::string &input) const {return evictedKeys.count(input)!=0;}
 T *acquire(const std::string &key,bool product=false) {
  for(auto i=entries.begin();i!=entries.end();++i)if((product?i->product:i->input)==key){T *p=i->object;++p->references;entries.splice(entries.end(),entries,i);return p;}
  return nullptr;
 }
 template<class Release> void insert(std::string input,std::string product,T *p,size_t size,Release release) {
  if(size>byteLimit)return;
  while(!entries.empty() && (entries.size()>=maxEntries || size>byteLimit-bytes)) {
   auto old=entries.front();entries.pop_front();bytes-=old.bytes;++evictions;if(evictedKeys.size()>=4096)evictedKeys.clear();evictedKeys.insert(old.input);release(old.object);
  }
  evictedKeys.erase(input);entries.push_back({std::move(input),std::move(product),p,size});bytes+=size;++p->references;
 }
 template<class Release> void reweigh(T *p,size_t size,Release release) {
  for(auto &e:entries)if(e.object==p){bytes-=e.bytes;e.bytes=size;bytes+=size;break;}
  while(!entries.empty() && bytes>byteLimit){auto old=entries.front();entries.pop_front();bytes-=old.bytes;release(old.object);}
 }
 template<class Release> void clear(Release release){for(auto &e:entries)release(e.object);entries.clear();bytes=0;}
 size_t count() const{return entries.size();}
 size_t totalBytes() const{return bytes;}
};
}
