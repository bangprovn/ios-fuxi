/* Production Vulkan imports of single planes of a two-plane (420v/420f) surface, on a software ICD with host import. */
#include "Backend.hpp"
#include <cassert>
#include <cstdio>
#include <vector>
using namespace fuxi;
template<class F> static void refuses(F body){bool failed=false;try{body();}catch(const std::runtime_error&){failed=true;}assert(failed);}
// One plane as the IOSurface lays it out: rows of `row` bytes from `offset` of the allocation.
struct Plane {uint32_t width,height,texel,format;size_t row,offset;};
static uint8_t pattern(const Plane &p,size_t x,size_t y,unsigned frame){return uint8_t((x*7+y*13+p.offset/64+frame*29)%251);}
static void fill(uint8_t *host,const Plane &p,unsigned frame){
 for(size_t y=0;y<p.height;++y)for(size_t x=0;x<p.width*p.texel;++x)host[p.offset+y*p.row+x]=pattern(p,x,y,frame);
}
// The texture's texels read back through the GPU (a copy op, as a blit or a shadow refresh reads them) equal the plane's rows.
static void readBack(Backend &b,Texture *t,const Plane &p,unsigned frame){
 auto *out=b.allocate(size_t(p.width)*p.height*p.texel);memset(out->contents,0x5c,out->length);
 Operation read{};read.texture=t;read.dst=out;b.submit({read});
 for(size_t y=0;y<p.height;++y)for(size_t x=0;x<p.width*p.texel;++x)assert(((uint8_t *)out->contents)[y*p.width*p.texel+x]==pattern(p,x,y,frame));
 b.destroy(out);
}
static void sentinels(const uint8_t *host,size_t bytes,const std::vector<Plane> &planes){
 std::vector<bool> used(bytes);
 for(auto &p:planes)for(size_t y=0;y<p.height;++y)for(size_t x=0;x<p.width*p.texel;++x)used[p.offset+y*p.row+x]=true;
 for(size_t i=0;i<bytes;++i)if(!used[i])assert(host[i]==0xa7);
}
int main(){
 Backend b;b.slabsEnabled=false;auto baseline=b.resourceBytes();
 if(!b.supportsHostImport()){puts("SKIP: plane imports require VK_EXT_external_memory_host");return 0;}
 // h-daily2's mediaserverd H.264 frame, 'v024' 332x198: plane 0 luma R8 at 64 with 384-byte rows, plane 1 CbCr RG8 166x99 at
 // 79936 with 384-byte rows, in a 131072-byte allocation. Both planes import the whole allocation and bind at their offset.
 {
  constexpr size_t bytes=131072;void *host=nullptr;assert(!posix_memalign(&host,65536,bytes));memset(host,0xa7,bytes);
  const Plane luma{332,198,1,10,384,64},chroma{166,99,2,30,384,79936};
  fill((uint8_t *)host,luma,0);fill((uint8_t *)host,chroma,0);
  auto hits=surfaceImportHits.load();
  auto *y=b.importTexture(host,bytes,luma.row,luma.width,luma.height,luma.offset,24,luma.format,true);
  auto *c=b.importTexture(host,bytes,chroma.row,chroma.width,chroma.height,chroma.offset,24,chroma.format,true);
  assert(y!=c && y->imported && c->imported && !y->shadow && !c->shadow && y->tiling==VK_IMAGE_TILING_LINEAR && c->tiling==VK_IMAGE_TILING_LINEAR);
  assert(y->vkFormat==VK_FORMAT_R8_UNORM && c->vkFormat==VK_FORMAT_R8G8_UNORM && y->format==10 && c->format==30 && y->mapped==host && c->mapped==host);
  // One import for the surface, shared by both planes (FuxiAdreno maps at most 512 imports at once).
  assert(y->allocated==bytes && c->allocated==bytes && y->memory==c->memory && y->surfaceShared && c->surfaceShared && b.surfaceMemory.size()==1 && b.resourceBytes()==baseline+bytes);
  // Core Animation wraps the same planes every frame: one live import per plane.
  auto *again=b.importTexture(host,bytes,chroma.row,chroma.width,chroma.height,chroma.offset,24,chroma.format,true);
  assert(again==c && c->references==2 && surfaceImportHits==hits+1);b.destroy(again);
  readBack(b,y,luma,0);readBack(b,c,chroma,0);
  // The next decoded frame lands in the same memory and is read without any copy.
  fill((uint8_t *)host,luma,1);fill((uint8_t *)host,chroma,1);readBack(b,y,luma,1);readBack(b,c,chroma,1);
  sentinels((const uint8_t *)host,bytes,{luma,chroma});
  // A plane's own rows end inside the allocation; one that would run past it is refused before Vulkan.
  refuses([&]{b.importTexture(host,bytes,384,166,99,bytes-384*98,24,30,true);});
  assert(!b.poisoned && !backboardGate.blocked());
  b.destroy(y);assert(b.surfaceMemory.size()==1);b.destroy(c);assert(b.surfaceMemory.empty());b.trimMemory();assert(b.resourceBytes()==baseline);free(host);
  puts("PASS: 420v planes R8 332x198 at 64 and RG8 166x99 at 79936 (rows 384) import in place into one shared surface import, share per plane, read both frames");
 }
 // SpringBoard's 420f snapshots have 800-byte rows (788x1706, plane 1 at 1364800), which no linear image can alias in place (Turnip
 // wants explicit rows in multiples of 64 bytes). Same shape, smaller: rows of 40 bytes. A sampled-only plane is read through an
 // optimal shadow refreshed from the whole surface imported as a buffer; any other use is refused, and neither latches anything.
 {
  constexpr size_t bytes=65536;void *host=nullptr;assert(!posix_memalign(&host,65536,bytes));memset(host,0xa7,bytes);
  const Plane luma{20,9,1,10,40,0},chroma{10,5,2,30,40,40*16};
  fill((uint8_t *)host,luma,0);fill((uint8_t *)host,chroma,0);
  refuses([&]{b.importTexture(host,bytes,luma.row,luma.width,luma.height,luma.offset,31,luma.format,false);});
  b.trimMemory();assert(!b.poisoned && !backboardGate.blocked() && b.resourceBytes()==baseline);
  auto *y=b.importTexture(host,bytes,luma.row,luma.width,luma.height,luma.offset,31,luma.format,true);
  auto *c=b.importTexture(host,bytes,chroma.row,chroma.width,chroma.height,chroma.offset,31,chroma.format,true);
  assert(y->shadow && c->shadow && y->backingBuffer && y->backingBuffer->imported && y->backingOffset==0 && y->backingRow==40);
  assert(c->backingOffset==640 && c->backingRow==40 && c->vkFormat==VK_FORMAT_R8G8_UNORM && c->tiling==VK_IMAGE_TILING_OPTIMAL);
  auto *again=b.importTexture(host,bytes,luma.row,luma.width,luma.height,luma.offset,31,luma.format,true);assert(again==y && y->references==2);b.destroy(again);
  readBack(b,y,luma,0);readBack(b,c,chroma,0);
  fill((uint8_t *)host,luma,2);fill((uint8_t *)host,chroma,2);readBack(b,y,luma,2);readBack(b,c,chroma,2);
  sentinels((const uint8_t *)host,bytes,{luma,chroma});
  // A shadow is read only: a GPU write to it is refused (and the submit with it), as for any linear texture shadow.
  auto *up=b.allocate(256);Operation write{};write.texture=y;write.src=up;write.upload=true;refuses([&]{b.submit({write});});b.destroy(up);
  b.destroy(y);b.destroy(c);b.trimMemory();assert(b.resourceBytes()==baseline);free(host);
  puts("PASS: 420f-shaped planes with 40-byte rows: refused for rendering, sampled through a refreshed shadow, shared, both frames");
 }
 // v-yuv1 (YouTube in Safari): FuxiAdreno's 512 imports were all live, so BoMap failed and Turnip answered VK_ERROR_OUT_OF_DEVICE_MEMORY
 // for WebKit.GPU's 640x360 420v frames (rows 640, plane 1 at 235584) and Safari's BGRA layers (66x45, rows 320). A refused sampled
 // import is copied by the CPU into an owned buffer and refreshed into an optimal image when its rows change. The refusal starts a
 // backoff in which sampled surfaces and no-copy buffers are copied without trying, while render targets still import.
 {
  constexpr size_t bytes=360448,tileBytes=16384;void *host=nullptr,*tile=nullptr;
  assert(!posix_memalign(&host,65536,bytes) && !posix_memalign(&tile,65536,tileBytes));memset(host,0xa7,bytes);memset(tile,0xa7,tileBytes);
  const Plane luma{640,360,1,10,640,64},chroma{320,180,2,30,640,235584},bgra{66,45,4,80,320,0};
  fill((uint8_t *)host,luma,0);fill((uint8_t *)host,chroma,0);fill((uint8_t *)tile,bgra,0);
  auto failures=b.hostImportFailures;assert(!b.importBackoff());
  b.failHostImportsForTest=1;
  auto *y=b.importTexture(host,bytes,luma.row,luma.width,luma.height,luma.offset,50,luma.format,true);
  assert(!b.failHostImportsForTest && b.hostImportFailures==failures+1 && b.importBackoff() && b.surfaceMemory.empty());
  assert(y->hostSource==(uint8_t *)host+64 && y->hostRow==640 && y->shadow && !y->imported && y->tiling==VK_IMAGE_TILING_OPTIMAL && y->backingRow==640 && y->vkFormat==VK_FORMAT_R8_UNORM);
  auto *c=b.importTexture(host,bytes,chroma.row,chroma.width,chroma.height,chroma.offset,50,chroma.format,true);
  assert(c->hostSource==(uint8_t *)host+235584 && c->backingRow==640 && c->vkFormat==VK_FORMAT_R8G8_UNORM && !c->imported);
  auto *t=b.importTexture(tile,tileBytes,bgra.row,bgra.width,bgra.height,bgra.offset,51,bgra.format,true);
  assert(t->hostSource==tile && t->backingRow==320 && t->vkFormat==VK_FORMAT_B8G8R8A8_UNORM && !t->imported);
  auto *again=b.importTexture(host,bytes,luma.row,luma.width,luma.height,luma.offset,50,luma.format,true);assert(again==y && y->references==2);b.destroy(again);
  // A render target cannot be a copy: it still imports during the backoff (and still fails alone when the import is refused).
  auto *rt=b.importTexture(tile,tileBytes,bgra.row,bgra.width,bgra.height,bgra.offset,51,bgra.format,false);assert(rt->imported && !rt->hostSource);b.destroy(rt);
  b.failHostImportsForTest=1;refuses([&]{b.importTexture(tile,tileBytes,bgra.row,bgra.width,bgra.height,bgra.offset,52,bgra.format,false);});
  assert(!b.poisoned && !backboardGate.blocked() && b.hostImportFailures==failures+2);
  // No-copy buffers take their copy at once during the backoff.
  refuses([&]{b.importBuffer(tile,tileBytes);});
  auto copies=b.hostCopies;
  readBack(b,y,luma,0);readBack(b,c,chroma,0);readBack(b,t,bgra,0);assert(b.hostCopies==copies+3);
  readBack(b,y,luma,0);readBack(b,t,bgra,0);assert(b.hostCopies==copies+3); // unchanged rows are not copied again
  // The next video frame and a redrawn layer are copied before the next read: video does not freeze on its first frame.
  fill((uint8_t *)host,luma,3);fill((uint8_t *)host,chroma,3);fill((uint8_t *)tile,bgra,3);
  readBack(b,y,luma,3);readBack(b,c,chroma,3);readBack(b,t,bgra,3);assert(b.hostCopies==copies+6);
  sentinels((const uint8_t *)host,bytes,{luma,chroma});sentinels((const uint8_t *)tile,tileBytes,{bgra});
  b.destroy(y);b.destroy(c);b.destroy(t);b.trimMemory();assert(b.resourceBytes()==baseline);
  // A plane with 40-byte rows whose buffer import (for the shadow) is refused is copied too.
  b.importBackoffUntil=0;b.failHostImportsForTest=1;const Plane odd{20,9,1,10,40,0};fill((uint8_t *)tile,odd,4);
  auto *o=b.importTexture(tile,tileBytes,odd.row,odd.width,odd.height,odd.offset,54,odd.format,true);
  assert(!b.failHostImportsForTest && o->hostSource==tile && o->backingRow==64 && b.importBackoff() && b.hostImportFailures==failures+3);
  readBack(b,o,odd,4);b.destroy(o);b.trimMemory();assert(b.resourceBytes()==baseline);
  // After the backoff a sampled import is tried again, both planes in one shared import.
  b.importBackoffUntil=0;auto objects=liveMemoryObjects.load();
  y=b.importTexture(host,bytes,luma.row,luma.width,luma.height,luma.offset,53,luma.format,true);
  c=b.importTexture(host,bytes,chroma.row,chroma.width,chroma.height,chroma.offset,53,chroma.format,true);
  assert(y->imported && c->imported && !y->hostSource && y->memory==c->memory && b.surfaceMemory.size()==1 && liveMemoryObjects==objects+1 && b.resourceBytes()==baseline+bytes);
  readBack(b,y,luma,3);readBack(b,c,chroma,3);
  b.destroy(y);b.destroy(c);assert(b.surfaceMemory.empty());b.trimMemory();assert(b.resourceBytes()==baseline && liveMemoryObjects==objects);
  free(host);free(tile);
  puts("PASS: refused host imports: 640x360 420v planes and a 66x45 BGRA layer copied and refreshed per changed frame, render targets still imported, backoff, shared import after it");
 }
 // A pipeline's declared fragment textures (bit per slot) come from the translator; an unknown adapter declares none.
 {Pipeline p{};assert(!b.declaredTextures(&p) && !b.declaredTextures(nullptr));p.textureSlots=0x3;assert(b.declaredTextures(&p)==0x3);}
 b.close();puts("PASS: plane imports");
}
