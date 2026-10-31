#include "MemoryTypes.hpp"
#include <cassert>
#include <cstring>
#include <vector>
using namespace fuxi;
static std::string logged(bool cached,const std::vector<uint32_t> &types,const std::vector<uint32_t> &chosen) {
 char *buf=nullptr;size_t n=0;FILE *f=open_memstream(&buf,&n);
 for(uint32_t c:chosen)logBufferMemoryType(f,cached,uint32_t(types.size()),[&](uint32_t t){return types[t];},c);
 fclose(f);std::string s(buf,n);free(buf);return s;
}
int main() {
 const uint32_t wc=MemDeviceLocal|MemHostVisible|MemHostCoherent,cc=wc|MemHostCached,nc=MemDeviceLocal|MemHostVisible|MemHostCached,dl=MemDeviceLocal;
 auto pick=[](std::vector<uint32_t> t,uint32_t allowed,bool cached){return bufferMemoryType(uint32_t(t.size()),allowed,cached,[&](uint32_t i){return t[i];});};
 // Turnip without the kernel flag: one type, cached or not.
 assert(pick({wc},1,false)==0 && pick({wc},1,true)==0);
 // Turnip with NativeAdrenoDeviceCachedCoherent: off keeps type 0, on takes type 1.
 assert(pick({wc,cc},3,false)==0 && pick({wc,cc},3,true)==1);
 // A buffer whose requirements exclude the cached type falls back to today's choice; cached-non-coherent never qualifies.
 assert(pick({wc,cc},1,true)==0 && pick({wc,nc},3,true)==0 && pick({dl,nc,cc},7,true)==2 && pick({dl,nc},3,true)==1);
 // Today's choice is the first allowed host-visible type; nothing host visible returns count.
 assert(pick({dl,wc,cc},6,false)==1 && pick({dl},1,true)==1 && pick({wc,cc},0,true)==2 && pick({},~0u,true)==0);
 assert(memoryFlagsText(cc)=="DL|HV|HC|HCA" && memoryFlagsText(0)=="0" && memoryFlagsText(MemLazy|MemDeviceLocal)=="DL|LAZY" && memoryFlagsText(0x100|MemHostVisible)=="HV|0x100");
 // Policy line once per process with every type; the chosen-type line again only when the choice changes.
 std::string s=logged(true,{wc,cc},{1,1,1});
 assert(s=="FUXI MTL: policy buffer_cached=1 memory_types=0:DL|HV|HC,1:DL|HV|HC|HCA\nFUXI MTL: buffer memory type=1 flags=DL|HV|HC|HCA cached=1\n");
 s=logged(true,{wc,cc},{0,0,2,1});
 assert(s=="FUXI MTL: buffer memory type=0 flags=DL|HV|HC cached=0\nFUXI MTL: buffer memory type=1 flags=DL|HV|HC|HCA cached=1\n");
 puts("PASS: buffer memory type preference (cached-coherent only when preferred and allowed, fallback to first host-visible), flag names, once-only logs");
}
