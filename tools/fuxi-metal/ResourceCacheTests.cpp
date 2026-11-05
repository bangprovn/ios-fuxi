/* Original SDK-independent production limits/LRU checks; no GPU submissions. */
#include "ResourceLimits.hpp"
#include "ObjectCache.hpp"
#include "CACompare.h"
#include <cassert>
#include <vector>
#include <limits>
struct Object {size_t references=1;unsigned identity;};
int main(){
 fuxi::ResourceLimits limits(16384),small(2048);assert(limits.dimension==4096 && small.dimension==2048);
 assert(limits.extent(1080,2400,4) && limits.extent(4096,4096,8));
 assert(!small.extent(1080,2400,4) && !limits.extent(4097,1,4) && !limits.extent(0,1,4));
 size_t huge=std::numeric_limits<size_t>::max();
 assert(!limits.extent(huge,2,4) && !limits.extent(2,2,huge));
 assert(limits.rows(1080,2400,4,4352,256,4352*2400+256));
 assert(!limits.rows(1080,2400,4,4352,257,4352*2400+256));
 assert(!limits.rows(1080,2400,4,huge,0,huge) && !limits.rows(1080,2400,4,4316,0,huge));
 limits.reserve(limits.single);limits.reserve(limits.single);bool refused=false;
 try{limits.reserve(1);}catch(const std::runtime_error&){refused=true;}assert(refused);
 limits.release(limits.single);limits.release(limits.single);assert(!limits.used);
 // Imported client memory is its own account (v-imp2c): it leaves the own target whole; own past the target is an overdraft,
 // bounded with the imports by the wired ceiling, and every refusal is a BudgetRefusal (nothing latched).
 {fuxi::ResourceLimits split;split.reserveImport(213u<<20);assert(split.left()==split.target && split.used==0);
  split.reserve(split.single);split.reserve(split.single);bool budget=false;try{split.reserve(1);}catch(const fuxi::BudgetRefusal&){budget=true;}assert(budget);
  assert(split.overdraftFits(64u<<20) && !split.overdraftFits(split.single+1));
  split.overdraft(64u<<20);assert(!split.left() && split.wiredLeft()==split.ceiling-split.used-split.imported);
  budget=false;try{split.reserveImport(split.wiredLeft()+1);}catch(const fuxi::BudgetRefusal&){budget=true;}assert(budget);
  split.release(split.used);split.releaseImport(split.imported);assert(!split.used && !split.imported);}
 assert(fuxi::scissorFits(1000,2300,80,100,1080,2400));
 assert(!fuxi::scissorFits(1000,2300,81,100,1080,2400) && !fuxi::scissorFits(huge,0,1,1,1080,2400));
 assert(fuxi::viewportFits(0,0,1080,2400,0,1,1080,2400));
 assert(!fuxi::viewportFits(0,0,1080,2401,0,1,1080,2400) && !fuxi::viewportFits(NAN,0,1,1,0,1,1080,2400));
 fuxi::ObjectCache<Object> cache;unsigned destroyed=0;
 auto release=[&](Object *p){if(!--p->references){++destroyed;delete p;}};
 auto *first=new Object{1,7};cache.insert("input","product",first,1,release);
 auto *same=cache.acquire("input");assert(same==first && same->references==3);
 release(first);assert(same->identity==7 && same->references==2);
 assert(!cache.acquire("different"));auto *product=cache.acquire("product",true);assert(product==same);release(product);
 for(unsigned i=0;i<31;++i){auto *p=new Object{1,i};cache.insert(std::to_string(i),std::to_string(i),p,1,release);release(p);}
 // Touch the oldest, then evict its neighbour, proving access-order LRU.
 auto *touch=cache.acquire("input");assert(touch==same);release(touch);
 auto *extra=new Object{1,99};cache.insert("extra","extra",extra,1,release);release(extra);
 assert(cache.count()==32 && !cache.acquire("0"));
 auto *large=new Object{1,100};cache.insert("large","large",large,cache.maxBytes,release);release(large);
 assert(cache.count()==1 && same->identity==7 && same->references==1); // alive after eviction
 cache.reweigh(large,cache.maxBytes+1,release);assert(!cache.count());cache.clear(release);release(same);assert(destroyed==34);
 auto *uncached=new Object{1,0};cache.insert("oversize","oversize",uncached,cache.maxBytes+1,release);assert(!cache.count() && uncached->references==1);release(uncached);
 std::vector<uint8_t> cpu(1080*2400*4,128),gpu(4352*2400,128);
 assert(fuxiCAAcceptSize(fuxiCACompareSize(gpu.data(),4352,cpu.data(),1080,2400),1080,2400));
 gpu[(2399*4352)+1079*4]=255;assert(!fuxiCAAcceptSize(fuxiCACompareSize(gpu.data(),4352,cpu.data(),1080,2400),1080,2400));
 assert(!fuxiCAAcceptSize(fuxiCACompareSize(gpu.data(),4316,cpu.data(),1080,2400),1080,2400));
 assert(!fuxiCAAcceptSize(fuxiCACompareSize(nullptr,4352,cpu.data(),1080,2400),1080,2400));
}
