#pragma once
#include <atomic>
#include <cstdint>
#include <cstdio>
#include <string>
// Memory-type choice for MTLBuffer-backed VkBuffers. Today: the first allowed HOST_VISIBLE type (Turnip type 0, write-combine).
// FUXI_MTL_BUFFER_CACHED=1 prefers the first allowed HOST_VISIBLE|HOST_COHERENT|HOST_CACHED type (Turnip exposes it only when the
// kernel reported IO-coherent write-back BOs), else falls back to today's choice. Flags are VkMemoryPropertyFlagBits values.
namespace fuxi {
enum : uint32_t {MemDeviceLocal=1,MemHostVisible=2,MemHostCoherent=4,MemHostCached=8,MemLazy=16,MemProtected=32};
template<class Flags> inline uint32_t bufferMemoryType(uint32_t count,uint32_t allowed,bool preferCached,Flags flagsOf) {
 const uint32_t cached=MemHostVisible|MemHostCoherent|MemHostCached;
 if(preferCached)for(uint32_t t=0;t<count && t<32;++t)if((allowed>>t&1) && (uint32_t(flagsOf(t))&cached)==cached)return t;
 for(uint32_t t=0;t<count && t<32;++t)if((allowed>>t&1) && (uint32_t(flagsOf(t))&MemHostVisible))return t;
 return count;
}
inline std::string memoryFlagsText(uint32_t f) {
 static const char *names[]={"DL","HV","HC","HCA","LAZY","PROT"};std::string s;
 for(unsigned i=0;i<6;++i)if(f>>i&1){if(!s.empty())s+='|';s+=names[i];}
 if(f>>6){char x[16];snprintf(x,sizeof x,"%s0x%x",s.empty()?"":"|",f>>6<<6);s+=x;}
 return s.empty()?"0":s;
}
// 'FUXI MTL: policy buffer_cached=N memory_types=0:DL|HV|HC,1:...' once per process, then 'buffer memory type=T flags=... cached=N'
// whenever the chosen type differs from the last one logged (normally once).
template<class Flags> inline void logBufferMemoryType(FILE *out,bool preferCached,uint32_t count,Flags flagsOf,uint32_t chosen) {
 static std::atomic<bool> policy{false};static std::atomic<uint32_t> last{~0u};
 if(!policy.exchange(true)){std::string list;for(uint32_t t=0;t<count && t<32;++t)list+=(t?",":"")+std::to_string(t)+":"+memoryFlagsText(uint32_t(flagsOf(t)));
  fprintf(out,"FUXI MTL: policy buffer_cached=%d memory_types=%s\n",int(preferCached),list.empty()?"none":list.c_str());}
 if(chosen<count && last.exchange(chosen)!=chosen){const uint32_t f=uint32_t(flagsOf(chosen));
  fprintf(out,"FUXI MTL: buffer memory type=%u flags=%s cached=%d\n",chosen,memoryFlagsText(f).c_str(),int((f&MemHostCached)!=0));}
}
}
