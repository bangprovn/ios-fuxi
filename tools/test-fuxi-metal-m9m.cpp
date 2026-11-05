/* Actual Vulkan surface-offset and draw-safety controls; no Apple inputs. */
#include "fuxi-metal/Backend.hpp"
#include <cassert>
#include <cstdio>
template<class F> static void refuses(F body){bool failed=false;try{body();}catch(const std::runtime_error&){failed=true;}assert(failed);}
int main(int argc,char **argv){
 fuxi::Backend backend;backend.slabsEnabled=false;backend.buffersPooled=false; // exact byte accounting: no slabs, no pooled buffers
 assert(!strcmp(getenv("TU_DEBUG"),argc==2?argv[1]:"sysmem,nocb"));
 if(argc==2){puts("PASS: Backend preserves the TU_DEBUG override");return 0;}
 auto baseline=backend.resourceBytes(),importBaseline=backend.importedBytes();
 // Even corrupt internal render records cannot load the translator or bind a
 // null Vulkan pipeline. A marked draw is distinct from a valid image transfer.
 fuxi::Operation missing{};missing.renderDraw=true;
 fuxi::Pipeline broken{};fuxi::Operation invalid{};invalid.pipeline=&broken;
 fuxi::Pipeline unbound{};unbound.translation=(void *)1;auto nullBind=invalid;nullBind.pipeline=&unbound;
 auto dropped=fuxi::droppedDraws.load(),submits=fuxi::Backend::submits.load(),empty=fuxi::Backend::emptySubmits.load();unsigned scheduled=0;
 backend.submit({missing,invalid,nullBind},[&]{++scheduled;});
 assert(scheduled==1 && fuxi::droppedDraws==dropped+3 && fuxi::Backend::submits==submits && fuxi::Backend::emptySubmits==empty+1);
 assert(backend.resourceBytes()==baseline);
 if(!backend.supportsHostImport()){puts("SKIP: M9m same-memory offset pixels require VK_EXT_external_memory_host");return 0;}
 constexpr size_t width=250,height=256,row=1024,offset=65536,pixels=width*height*4,bytes=offset+row*height+65536;
 void *host=nullptr;assert(!posix_memalign(&host,65536,bytes));memset(host,0xa7,bytes);
 for(size_t y=0;y<height;++y)for(size_t x=0;x<width*4;++x)((uint8_t *)host)[offset+y*row+x]=(uint8_t)((y*width*4+x)%251);
 for(auto bad:{size_t(1),bytes,bytes+1})refuses([&]{backend.importTexture(host,bytes,row,width,height,bad);});
 refuses([&]{backend.importTexture(host,bytes,2048,width,height,offset);});assert(backend.resourceBytes()==baseline);
 auto *texture=backend.importTexture(host,bytes,row,width,height,offset);
 assert(texture->imported && texture->mapped==host && texture->allocated==bytes && texture->charged==bytes);
 assert(backend.resourceBytes()==baseline && backend.importedBytes()==importBaseline+bytes); // client pages: the import account
 auto *read=backend.allocate(pixels);fuxi::Operation copy{};copy.texture=texture;copy.dst=read;
 backend.submit({copy});for(size_t y=0;y<height;++y)assert(!memcmp((uint8_t *)host+offset+y*row,(uint8_t *)read->contents+y*width*4,width*4));
 auto *upload=backend.allocate(pixels);memset(upload->contents,0x39,pixels);
 fuxi::Operation write{};write.texture=texture;write.src=upload;write.upload=true;
 backend.submit({missing,write,copy});assert(!memcmp(read->contents,upload->contents,pixels));
 for(size_t y=0;y<height;++y)assert(!memcmp((uint8_t *)host+offset+y*row,(uint8_t *)upload->contents+y*width*4,width*4));
 fuxi::Operation clear{};clear.texture=texture;clear.clearOnly=true;clear.clear[0]=1;clear.clear[3]=1;
 backend.submit({nullBind,clear,copy});
 for(size_t y=0;y<height;++y)for(size_t x=0;x<width;++x){const uint8_t red[]={0,0,255,255};
  assert(!memcmp((uint8_t *)host+offset+y*row+x*4,red,4) && !memcmp((uint8_t *)read->contents+(y*width+x)*4,red,4));}
 for(size_t i=0;i<offset;++i)assert(((uint8_t *)host)[i]==0xa7);
 for(size_t i=offset+row*height;i<bytes;++i)assert(((uint8_t *)host)[i]==0xa7);
 backend.destroy(texture);backend.destroy(read);backend.destroy(upload);free(host);assert(backend.resourceBytes()==baseline && backend.importedBytes()==importBaseline);
 puts("PASS: M9m null-pipeline draw drops/count/schedule, whole-surface HostImport at plane offset/rowbytes, read/upload/clear pixels, sentinels, layout/bounds refusal and budget rollback");
}
