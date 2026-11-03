/* Actual Vulkan images, fence pinning, bounded cache, pressure and loss. */
#include "Backend.hpp"
#include <cassert>
#include <cstdio>
#include <future>
int main(int argc,char **){
 using namespace fuxi;Backend backend;backend.slabsEnabled=false;auto objects=liveMemoryObjects.load();
 if(argc>1){
  auto *idle=backend.allocateTexture(80,5,16,16);backend.destroy(idle);auto cached=texturePoolBytes.load();assert(cached);
  auto *t=backend.allocateTexture(80,5,64,64);auto *buffer=backend.allocate(16384);
  Operation upload{};upload.texture=t;upload.src=buffer;upload.upload=true;std::function<void()> retire;backend.submit({upload},{},&retire);
  bool failed=false;try{retire();}catch(const std::runtime_error&){failed=true;}assert(failed && backend.poisoned);
  auto held=liveMemoryObjects.load();backend.trimMemory();assert(texturePoolBytes==cached);
  failed=false;try{backend.allocateTexture(80,5,16,16);}catch(const std::runtime_error&){failed=true;}assert(failed);
  backend.destroy(t);backend.destroy(buffer);backend.close();assert(liveMemoryObjects==held);
  puts("PASS: loss/timeout retains both idle pool and unknown DMA, refuses reuse/trim");return 0;
 }
 auto *a=backend.allocateTexture(80,5,16,16);auto memory=a->memory;auto bytes=a->charged;auto *up=backend.allocate(1024),*out=backend.allocate(1024);memset(up->contents,0x43,1024);
 Operation upload{};upload.texture=a;upload.src=up;upload.upload=true;std::function<void()> retire;
 auto before=frameCounts.allocations;backend.submit({upload},{},&retire);
 Operation clear{};clear.texture=a;clear.clearOnly=true;clear.clear[3]=1;std::function<void()> lastRetire;backend.submit({clear},{},&lastRetire);backend.destroy(a);
 auto misses=texturePoolMisses.load();auto *b=backend.allocateTexture(80,5,16,16);assert(b->memory!=memory && texturePoolMisses==misses+1);
 retire();auto *stillLive=backend.allocateTexture(80,5,16,16);assert(stillLive->memory!=memory);
 lastRetire();auto hits=texturePoolHits.load();auto allocated=frameCounts.allocations;auto *c=backend.allocateTexture(80,5,16,16);assert(c->memory==memory && texturePoolHits==hits+1 && frameCounts.allocations==allocated && !c->defined && c->layout==VK_IMAGE_LAYOUT_UNDEFINED);
 upload.texture=c;Operation read{};read.texture=c;read.dst=out;backend.submit({upload,read});assert(!memcmp(up->contents,out->contents,1024));
 backend.destroy(b);backend.destroy(c);backend.destroy(stillLive);assert(backend.resourceBytes()==bytes*3+up->charged+out->charged);
 backend.trimMemoryForTest(UINT64_MAX);assert(backend.resourceBytes()==up->charged+out->charged);
 std::vector<Texture *> textures;for(unsigned i=0;i<TransientMemory<VkDeviceMemory>::maxEntries+2;++i)textures.push_back(backend.allocateTexture(80,5,16,16));for(auto *t:textures)backend.destroy(t);
 assert(texturePoolBytes==bytes*TransientMemory<VkDeviceMemory>::maxEntries);backend.trimMemory();assert(!texturePoolBytes);
 // Nearby sizes share a class: a 17x16 texture reuses the pooled block without a new allocation.
 {auto *p=backend.allocateTexture(80,5,16,16);auto *q=backend.allocateTexture(80,5,17,16);auto m=p->memory;auto classBytes=p->charged;backend.destroy(p);auto allocs=frameCounts.allocations,h2=texturePoolHits.load();auto *r=backend.allocateTexture(80,5,17,16);
  assert(r->memory==m && texturePoolHits==h2+1 && frameCounts.allocations==allocs && r->charged==classBytes && classBytes>=r->allocated);backend.destroy(q);backend.destroy(r);backend.trimMemory();}
 // Idle storage still counts toward the device budget; imports must never donate memory.
 auto *cached=backend.allocateTexture(80,5,16,16);backend.destroy(cached);assert(texturePoolBytes==bytes);
 backend.trimMemory();backend.destroy(up);backend.destroy(out);
 // Small host buffers: exact-size reuse is zeroed and skips vkAllocateMemory; pooled storage stays charged until trimmed; the pool is bounded.
 backend.trimMemory();{auto pooled=Backend::bufferPoolEntries;auto h0=bufferPoolHits.load(),m0=bufferPoolMisses.load();auto a0=frameCounts.allocations;
  auto *x=backend.allocate(1000);memset(x->contents,0x5a,1000);auto *xp=x->contents;backend.destroy(x);assert(backend.resourceBytes()>0 && bufferPoolBytes==1008);
  auto *y=backend.allocate(1003);assert(y->contents==xp && bufferPoolHits==h0+1 && bufferPoolMisses==m0+1 && frameCounts.allocations==a0+1 && y->length==1003);for(size_t i=0;i<1008;++i)assert(((uint8_t *)y->contents)[i]==0);
  auto *z=backend.allocate(2000);backend.destroy(y);backend.destroy(z);assert(bufferPoolBytes==1008+2000);
  backend.trimMemory();assert(!bufferPoolBytes && !backend.resourceBytes());
  std::vector<Buffer *> many;for(size_t i=0;i<pooled+3;++i)many.push_back(backend.allocate(64));for(auto *b:many)backend.destroy(b);assert(bufferPoolBytes==pooled*64);backend.trimMemory();
  auto *big=backend.allocate(Backend::bufferPoolMaxBuffer+16);backend.destroy(big);assert(!bufferPoolBytes);
  backend.buffersPooled=false;auto *np=backend.allocate(64);backend.destroy(np);assert(!bufferPoolBytes);backend.buffersPooled=true;
 }
 assert(!backend.resourceBytes());backend.close();assert(liveMemoryObjects==objects);
 // Identity-keyed surface imports share one live texture per mapping/size/layout and die with their last holder.
 if(Backend backend2;backend2.supportsHostImport()){
  constexpr size_t row=4352,w=1080,h=64;size_t allocation=(row*h+65535)&~size_t(65535);void *host=nullptr;assert(!posix_memalign(&host,65536,allocation));memset(host,0,allocation);
  auto hits=surfaceImportHits.load(),misses=surfaceImportMisses.load();
  auto *s1=backend2.importTexture(host,allocation,row,w,h,0,77);auto allocs=frameCounts.allocations;auto *s2=backend2.importTexture(host,allocation,row,w,h,0,77);
  assert(s1==s2 && s1->references==2 && surfaceImportHits==hits+1 && surfaceImportMisses==misses+1 && frameCounts.allocations==allocs);
  auto *other=backend2.importTexture(host,allocation,row,w,h,0,78);assert(other!=s1);
  backend2.destroy(s2);assert(s1->references==1);backend2.destroy(s1);backend2.destroy(other);
  auto *again=backend2.importTexture(host,allocation,row,w,h,0,77);assert(again->references==1 && surfaceImportMisses==misses+3);
  auto *shrunk=backend2.importTexture(host,allocation,row,w,h-1,0,77);assert(shrunk!=again);auto *plain=backend2.importTexture(host,allocation,row,w,h);assert(plain!=again && plain->references==1);
  backend2.destroy(again);backend2.destroy(shrunk);backend2.destroy(plain);assert(!backend2.resourceBytes());free(host);puts("PASS: surface import reuse");
 }else puts("SKIP: surface import reuse (no host import)");

 // Slab sub-allocation: small images share one kernel allocation at distinct, aligned, non-overlapping offsets.
 {
  Backend sb;auto live0=liveMemoryObjects.load();auto grows=slabGrows.load(),hitsBefore=slabHits.load();auto allocs0=frameCounts.allocations;
  auto *s1=sb.allocateTexture(80,5,16,16);assert(s1->slabbed && slabGrows==grows+1 && sb.resourceBytes()==SlabMemory<VkDeviceMemory>::slabSize && liveMemoryObjects==live0+1);
  auto allocs1=frameCounts.allocations;auto *s2=sb.allocateTexture(80,5,16,16);auto *s3=sb.allocateTexture(80,5,64,64);
  assert(s2->memory==s1->memory && s3->memory==s1->memory && frameCounts.allocations==allocs1 && slabHits==hitsBefore+3 && liveMemoryObjects==live0+1 && allocs1==allocs0+1);
  for(auto *t:{s1,s2,s3})assert(t->memExtent>=t->allocated && !t->charged && t->memOffset+t->memExtent<=SlabMemory<VkDeviceMemory>::slabSize);
  assert(!Backend::memoryAlias(s1,s2) && !Backend::memoryAlias(s2,s3) && !Backend::memoryAlias(s1,s3) && Backend::memoryAlias(s1,s1));
  // Distinct textures in one allocation keep independent pixels.
  auto *up1=sb.allocate(1024),*up2=sb.allocate(1024),*o1=sb.allocate(1024),*o2=sb.allocate(1024);memset(up1->contents,0x11,1024);memset(up2->contents,0x22,1024);
  Operation u1{};u1.texture=s1;u1.src=up1;u1.upload=true;Operation u2{};u2.texture=s2;u2.src=up2;u2.upload=true;Operation r1{};r1.texture=s1;r1.dst=o1;Operation r2{};r2.texture=s2;r2.dst=o2;
  sb.submit({u1,u2,r1,r2});assert(!memcmp(o1->contents,up1->contents,1024) && !memcmp(o2->contents,up2->contents,1024));
  // Fence safety: a destroyed texture's range is not handed out until the submit that pinned it retires.
  Operation pin{};pin.texture=s1;pin.src=up1;pin.upload=true;std::function<void()> retire;sb.submit({pin},{},&retire);
  auto off=s1->memOffset;sb.destroy(s1);auto *during=sb.allocateTexture(80,5,16,16);assert(during->memOffset!=off || during->memory!=s2->memory);
  retire();auto *after=sb.allocateTexture(80,5,16,16);assert(after->memOffset==off && after->memory==s2->memory);
  // Steady-state frames: allocate/free cycles never reach the kernel again.
  auto steady=frameCounts.allocations;for(unsigned i=0;i<200;++i){auto *a=sb.allocateTexture(80,5,100+i%7,100);auto *b=sb.allocateTexture(80,5,256,64+i%5);sb.destroy(a);sb.destroy(b);}
  assert(frameCounts.allocations==steady);
  // Budget: demand beyond one slab grows by whole slabs, stays within the cap, and falls back to dedicated blocks past it.
  std::vector<Texture *> many;auto maxBytes=sb.resourceBytes();
  for(unsigned i=0;i<12;++i){many.push_back(sb.allocateTexture(80,5,1024,1024));maxBytes=std::max(maxBytes,sb.resourceBytes());assert(sb.resourceBytes()<=ResourceLimits::budget);}
  size_t slabbed=0;for(auto *t:many)slabbed+=t->slabbed;assert(slabbed==12 && sb.resourceBytes()>SlabMemory<VkDeviceMemory>::slabSize && slabBytes<=SlabMemory<VkDeviceMemory>::maxSlabs*SlabMemory<VkDeviceMemory>::slabSize);
  for(unsigned i=0;i<28;++i)many.push_back(sb.allocateTexture(80,5,1024,1024)); // 40 x 4 MiB: slabs cap at 8 x 16 MiB, the rest take dedicated blocks
  slabbed=0;for(auto *t:many)slabbed+=t->slabbed;assert(slabbed>=28 && slabbed<40 && slabBytes==SlabMemory<VkDeviceMemory>::maxSlabs*SlabMemory<VkDeviceMemory>::slabSize && sb.resourceBytes()<=ResourceLimits::budget);
  auto *big=sb.allocateTexture(80,5,2048,1024);assert(!big->slabbed);
  for(auto *t:many)sb.destroy(t);sb.destroy(big);sb.destroy(during);sb.destroy(after);sb.destroy(s2);sb.destroy(s3);
  sb.trimMemoryForTest(0);assert(sb.resourceBytes()>=SlabMemory<VkDeviceMemory>::slabSize); // idle trim keeps the newest slab warm
  sb.destroy(up1);sb.destroy(up2);sb.destroy(o1);sb.destroy(o2);sb.trimMemory();assert(!sb.resourceBytes() && liveMemoryObjects==live0);sb.close();
  puts("PASS: slab sub-allocation (shared allocation, aligned disjoint offsets, pixels, fence-gated reuse, steady state, budget)");
 }

 // Shadow-storage rename: a prepare write that conflicts with a flight reading the shadow's backing buffer gets fresh storage instead of waiting.
 {
  Backend rb;auto *buf=rb.allocate(1024);memset(buf->contents,0x11,1024);auto *shadow=rb.allocateTexture(80,1,16,16);shadow->shadow=true;buf->shadowBacking=true;shadow->backingBuffer=buf;++buf->references;shadow->backingRow=64;
  auto *dst=rb.allocateTexture(80,5,16,16);Operation copy{};copy.texture=dst;copy.sourceTexture=shadow;copy.width=16;copy.height=16;
  auto *oldContents=(uint8_t *)buf->contents;std::vector<PrepareWrite> writes{{buf,0,64}};
  auto flight=[&](bool expectWait,FrameSample &trace){
   std::function<void()> a,b;rb.submit({copy},{},&a);bool prepared=false;std::promise<void> started;
   auto f=std::async(std::launch::async,[&]{FrameTraceScope scope(&trace);started.set_value();rb.submit({copy},{},&b,[&]{prepared=true;memset(buf->contents,0x22,64);},&writes);});
   started.get_future().wait();assert(f.wait_for(std::chrono::milliseconds(expectWait?100:5000))==(expectWait?std::future_status::timeout:std::future_status::ready));
   a();f.get();b();assert(prepared);
  };
  FrameSample renamed;flight(false,renamed);
  assert(renamed.shadowRenames==1 && renamed.shadowRenameBytes==1024 && !renamed.conflictWaits && buf->contents!=oldContents && buf->spare && buf->spare->contents==oldContents);
  assert(((uint8_t *)buf->contents)[0]==0x22 && ((uint8_t *)buf->contents)[64]==0x11 && oldContents[0]==0x11); // new storage carries the old bytes plus the write; the old one is untouched
  assert(buf->shadowKey()==oldContents);
  // Bound directly (possible GPU writes): never renamed, the prepare waits and the conflicting kind is reported.
  buf->directUse=true;FrameSample waited;flight(true,waited);assert(!waited.shadowRenames && waited.conflictWaits==1 && waited.conflictKinds[1]==1 && !waited.conflictKinds[0]);
  // CA's sampled linear view of a shadowed buffer takes the shadow-copy path (renameable); an in-place alias pins the buffer and is reported as a texture decline.
  {auto *sb2=rb.allocate(16384);auto *viaShadow=rb.linearTexture(sb2,80,1,16,16,0,64,true);assert(viaShadow->shadow && sb2->shadowBacking && !sb2->aliased);
   auto *sb3=rb.allocate(16384);auto *viaAlias=rb.linearTexture(sb3,80,1,16,16,0,64,false);assert(!viaAlias->shadow && sb3->aliased);
   Operation read{};read.texture=dst;read.sourceTexture=viaAlias;read.width=16;read.height=16;std::function<void()> a,b2;rb.submit({read},{},&a);
   std::vector<PrepareWrite> aliasWrites{{sb3,0,64}};FrameSample trace;std::promise<void> started;
   auto f=std::async(std::launch::async,[&]{FrameTraceScope scope(&trace);started.set_value();rb.submit({read},{},&b2,[&]{},&aliasWrites);});
   started.get_future().wait();assert(f.wait_for(std::chrono::milliseconds(100))==std::future_status::timeout);a();f.get();b2();
   assert(trace.renameDeclined[3]==1 && trace.conflictKinds[2]==1 && !trace.shadowRenames);
   rb.destroy(viaShadow);rb.destroy(viaAlias);rb.destroy(sb2);rb.destroy(sb3);}
  rb.destroy(copy.sourceTexture);rb.destroy(dst);rb.destroy(buf);rb.close();
  puts("PASS: shadow storage rename (no wait, bytes preserved, old storage untouched, direct use refused, conflict kind reported)");
 }
 // A client buffer's memory runs past it (linearTailPad): a linear texture ending at the buffer's rows (d-ft-v27's A8 labels,
 // 256x61 at offset 64 in 16 KiB) keeps the image's 4-row padding inside that memory and aliases; an internal buffer shadows.
 {
  Backend lb;
  auto *inner=lb.allocate(16384),*client=lb.allocate(16384,true);
  assert(inner->memoryBytes==inner->allocated && client->allocated==inner->allocated && client->memoryBytes==client->allocated+4096 && lb.tailPadBytes==4096);
  for(size_t i=0;i<16384;++i)((uint8_t *)client->contents)[i]=uint8_t(i*7+3);
  auto *shadowed=lb.linearTexture(inner,1,1,256,61,64,256),*aliased=lb.linearTexture(client,1,1,256,61,64,256);
  assert(shadowed->shadow && !aliased->shadow && aliased->borrowedMemory && lb.linearTailUses==1 && lb.linearShadows==1 && lb.linearShadowKinds.size()==1);
  auto *copy=lb.allocateTexture(1,1,256,61);auto *out=lb.allocate(256*61);
  Operation c{};c.texture=copy;c.sourceTexture=aliased;c.width=256;c.height=61;Operation r{};r.texture=copy;r.dst=out;lb.submit({c,r});
  for(unsigned y=0;y<61;++y)for(unsigned x=0;x<256;++x)assert(((uint8_t *)out->contents)[y*256+x]==uint8_t((64+y*256+x)*7+3));
  // Pooled, the padded memory goes back only to a client buffer of its class.
  lb.destroy(shadowed);lb.destroy(aliased);lb.destroy(inner);lb.destroy(client);auto hits=bufferPoolHits.load();
  auto *again=lb.allocate(16384);assert(bufferPoolHits==hits+1 && again->memoryBytes==again->allocated);
  auto *clientAgain=lb.allocate(16384,true);assert(bufferPoolHits==hits+2 && clientAgain->memoryBytes==clientAgain->allocated+4096);
  lb.destroy(again);lb.destroy(clientAgain);lb.destroy(copy);lb.destroy(out);lb.trimMemory();assert(!lb.tailPadBytes && !lb.resourceBytes());lb.close();
  puts("PASS: client buffer tail pad (256x61 A8 at offset 64 in 16 KiB aliases with its pixels, internal buffers unpadded, pool keeps the pad)");
 }
 assert(frameCounts.allocations>before);
 printf("PASS: image memory reuse after fence only, new-image layout/defined state, pixels, cap/idle/pressure/charge/close; Operation=%zu BufferBind=%zu bindings=%zu\n",sizeof(Operation),sizeof(BufferBind),sizeof(Operation::buffers));
}
