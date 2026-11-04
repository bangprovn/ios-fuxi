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
  assert(y->allocated==bytes && c->allocated==bytes && b.resourceBytes()==baseline+2*bytes);
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
  b.destroy(y);b.destroy(c);b.trimMemory();assert(b.resourceBytes()==baseline);free(host);
  puts("PASS: 420v planes R8 332x198 at 64 and RG8 166x99 at 79936 (rows 384) import in place, share per plane, read both frames");
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
 // A pipeline's declared fragment textures (bit per slot) come from the translator; an unknown adapter declares none.
 {Pipeline p{};assert(!b.declaredTextures(&p) && !b.declaredTextures(nullptr));p.textureSlots=0x3;assert(b.declaredTextures(&p)==0x3);}
 b.close();puts("PASS: plane imports");
}
