/* FuxiMetal's two byte accounts (ResourceLimits) on a software ICD with host import: the own target with its optional headroom and
 * overdraft, and the imported client memory (IOSurfaces, no-copy buffers), as the v-imp2c YouTube boot needed them (5 Nov). */
#include "Backend.hpp"
#include "../../xnu/fuxi-adreno/fuxi_abi_wire.h"
#include <cassert>
#include <cstdio>
#include <string>
#include <vector>
using namespace fuxi;
static_assert(ResourceLimits::kernelWired==FG_ABI_MAX_WIRED_BYTES,"FuxiMetal's ceiling follows FuxiAdreno's per-client wired quota");
static constexpr size_t MiB=size_t(1)<<20;
// The reason of a BudgetRefusal, "other: ..." for any other exception, "" when nothing was thrown.
template<class F> static std::string refusal(F body){
 try{body();}catch(const BudgetRefusal &e){return e.what();}catch(const std::exception &e){return std::string("other: ")+e.what();}return "";
}
static uint8_t pattern(size_t x,size_t y,unsigned seed){return uint8_t((x*5+y*11+seed*17)%251);}
// A BGRA surface 512 texels wide (rows of 2048 bytes), `bytes` long, filled with the pattern.
static void *surface(size_t bytes,unsigned seed){
 void *p=nullptr;assert(!posix_memalign(&p,65536,bytes));
 for(size_t y=0;y<bytes/2048;++y)for(size_t x=0;x<2048;++x)((uint8_t *)p)[y*2048+x]=pattern(x,y,seed);
 return p;
}
static Texture *import(Backend &b,void *host,size_t bytes,uint64_t id,bool sampled){return b.importTexture(host,bytes,2048,512,uint32_t(bytes/2048),0,id,80,sampled);}
// The texture's texels read back through the GPU (a copied surface is refreshed from its rows first) equal the surface's.
static void readBack(Backend &b,Texture *t,size_t bytes,unsigned seed){
 auto *out=b.allocate(bytes);memset(out->contents,0x5c,out->length);
 Operation read{};read.texture=t;read.dst=out;b.submit({read});
 for(size_t y=0;y<bytes/2048;++y)for(size_t x=0;x<2048;++x)assert(((uint8_t *)out->contents)[y*2048+x]==pattern(x,y,seed));
 b.destroy(out);
}
static void limitsOnly(){
 ResourceLimits l;
 constexpr size_t ceiling=ResourceLimits::wiredCeiling,cap=ResourceLimits::importBudget;
 assert(l.target==256*MiB && cap==384*MiB && ceiling==640*MiB && l.importCap==cap && l.ceiling==ceiling && l.left()==l.target && l.wiredLeft()==ceiling);
 // v-imp2c: ~213 MB of Safari's surfaces were in the one 256 MiB budget. In the import account they leave the own target whole.
 l.reserveImport(213*MiB);assert(l.left()==256*MiB && l.wiredLeft()==ceiling-213*MiB && l.importedPeak==213*MiB);
 l.reserve(125*MiB);l.reserve(125*MiB);assert(l.left()==6*MiB);
 assert(refusal([&]{l.reserve(8*MiB);})=="resource byte budget" && l.used==250*MiB);
 // Past the target only by overdraft, bounded by what can still be wired.
 assert(l.overdraftFits(8*MiB));l.overdraft(8*MiB);assert(l.used==258*MiB && !l.left() && l.usedPeak==258*MiB);
 assert(refusal([&]{l.reserve(1);})=="resource byte budget");
 // Own and imported together stop at the ceiling.
 const size_t room=l.wiredLeft();assert(room==ceiling-(213+258)*MiB);
 assert(l.importFits(room) && !l.importFits(room+1));
 l.reserveImport(room-8*MiB);assert(l.overdraftFits(8*MiB) && !l.overdraftFits(8*MiB+1) && !l.importFits(8*MiB+1) && l.importCap-l.imported>8*MiB);
 assert(refusal([&]{l.overdraft(8*MiB+1);})=="resource byte budget" && refusal([&]{l.reserveImport(8*MiB+1);})=="import byte budget");
 l.releaseImport(room-8*MiB);
 // The import cap holds on its own too.
 l.release(258*MiB);assert(l.importFits(cap-213*MiB) && !l.importFits(cap-213*MiB+1));
 l.resetPeaks();assert(l.usedPeak==0 && l.importedPeak==213*MiB);
 l.releaseImport(213*MiB);assert(!l.used && !l.imported);
 puts("PASS: ResourceLimits own target, overdraft to the wired ceiling, import cap, peaks");
}
int main(){
 limitsOnly();
 Backend b;b.slabsEnabled=false;
 if(!b.supportsHostImport()){puts("SKIP: budget imports require VK_EXT_external_memory_host");b.close();return 0;}
 const size_t own0=b.resourceBytes(),imported0=b.importedBytes();const auto objects0=liveMemoryObjects.load();
 // Small accounts so a host test reaches them: own target 96 MiB (optional allocations stop 64 MiB below it, at 32), imports 8 MiB,
 // both under a 112 MiB ceiling.
 b.setLimitsForTest(96*MiB,8*MiB,112*MiB);
 const auto counts0=b.budgetCounts();const auto failures0=b.hostImportFailures;
 // 1. Imports charge the import account, never the own one (a shared import once per surface).
 void *s1=surface(2*MiB,1),*s2=surface(2*MiB,2),*s3=surface(2*MiB,3),*s4=surface(4*MiB,4),*s5=surface(1*MiB,5);
 auto *t1=import(b,s1,2*MiB,101,true),*t2=import(b,s2,2*MiB,102,false),*t3=import(b,s3,2*MiB,103,true);
 assert(t1->imported && t2->imported && t3->imported && b.resourceBytes()==own0 && b.importedBytes()==imported0+6*MiB);
 auto *again=import(b,s1,2*MiB,101,true);assert(again==t1 && b.importedBytes()==imported0+6*MiB);b.destroy(again);
 // 2. The import account full (6 of 8 MiB, a 4 MiB surface): a sampled surface is copied into owned memory (and reads right), a
 // render target and a no-copy buffer fail alone, nothing latches, and no backoff starts: a 1 MiB surface still imports.
 auto *copy=import(b,s4,4*MiB,104,true);
 assert(!copy->imported && copy->hostSource==s4 && copy->shadow && b.importedBytes()==imported0+6*MiB && b.resourceBytes()>=own0+8*MiB);
 assert(b.budgetCounts().importRefused==counts0.importRefused+1 && !b.importBackoff() && b.hostImportFailures==failures0);
 readBack(b,copy,4*MiB,4);
 assert(refusal([&]{import(b,s4,4*MiB,106,false);})=="import byte budget");
 assert(refusal([&]{b.importBuffer(s4,4*MiB);})=="import byte budget");
 assert(!b.importBackoff() && b.hostImportFailures==failures0 && !b.poisoned && !backboardGate.blocked());
 auto *t5=import(b,s5,1*MiB,105,true);assert(t5->imported && b.importedBytes()==imported0+7*MiB);
 readBack(b,t5,1*MiB,5);
 // 3. A copy is optional memory: with own at 31 of the 32 MiB optional allocations may use, the next one is refused (that texture
 // only) while a CA texture still allocates.
 auto *ballast=b.allocate(31*MiB-b.resourceBytes());assert(b.resourceBytes()<=31*MiB+64*1024);
 const auto optional0=b.budgetCounts().optionalRefused;
 assert(refusal([&]{import(b,s4,4*MiB,107,true);})=="optional feature headroom" && b.budgetCounts().optionalRefused==optional0+1);
 auto *caTexture=b.allocateTexture(80,5,1024,1024);assert(caTexture && b.resourceBytes()>32*MiB);
 // 4. v-imp2c: own at its target with imports live and nothing optional left, CA's 284x93 texture (refused there, which latched
 // backboardd onto the CPU) still allocates: under the target first, then past it as an overdraft.
 auto *fill=b.allocate(96*MiB-b.resourceBytes()-512*1024);
 auto *small=b.allocateTexture(80,5,284,93);assert(small && b.resourceBytes()<=96*MiB && b.budgetCounts().overdrafts==counts0.overdrafts);
 auto *past=b.allocateTexture(80,5,1024,1024);
 assert(past && b.resourceBytes()>96*MiB && b.budgetCounts().overdrafts==counts0.overdrafts+1 && b.budgetCounts().refused==counts0.refused);
 // While over the target: optional memory is refused, a submit (and its shed before it) still runs.
 assert(refusal([&]{import(b,s4,4*MiB,108,true);})=="optional feature headroom");
 readBack(b,t1,2*MiB,1);readBack(b,copy,4*MiB,4);
 // 5. The ceiling (own + imported, 112 MiB here, FuxiAdreno's quota less Turnip's reserve on the phone): a texture past it is
 // refused alone, with nothing charged or latched, and an import within its cap but past the ceiling is refused too.
 b.trimMemory();const size_t own=b.resourceBytes();
 assert(own>96*MiB && own+b.importedBytes()+16*MiB>112*MiB);
 assert(refusal([&]{b.allocateTexture(80,5,2048,2048);})=="resource byte budget");
 assert(b.resourceBytes()==own && b.budgetCounts().refused==counts0.refused+1 && !b.poisoned && !backboardGate.blocked());
 b.setLimitsForTest(96*MiB,64*MiB,112*MiB);
 void *s9=surface(8*MiB,9);assert(112*MiB-own-b.importedBytes()<8*MiB);
 assert(refusal([&]{import(b,s9,8*MiB,109,false);})=="import byte budget" && b.resourceBytes()==own);
 // 6. Everything given back: both accounts at their baselines.
 for(auto *t:{t1,t2,t3,t5,copy,caTexture,small,past})b.destroy(t);
 b.destroy(ballast);b.destroy(fill);b.trimMemory();
 assert(b.resourceBytes()==own0 && b.importedBytes()==imported0 && liveMemoryObjects==objects0 && !b.poisoned && !backboardGate.blocked());
 for(void *p:{s1,s2,s3,s4,s5,s9})free(p);
 b.close();
 puts("PASS: imports charge their own account; full, a sampled surface is copied as optional memory, others fail alone, no backoff; CA textures overdraft the target up to the wired ceiling, then fail alone");
}
