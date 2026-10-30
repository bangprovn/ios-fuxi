/* Test-only authored SPIR-V adapter for the FUXI_MTL_OCCLUDE pixel controls. The fragment function name picks the blend/mask/discard/fetch/storage behaviour; per-draw push constants
   (NDC rect, rotation, colour) come from a table the test fills, indexed by the draw's first vertex / 6, so every draw has a distinct, cacheable DrawInput. Never built into the production bridge. */
#include "Translator.hpp"
#include <cstring>
#include <fstream>
#include <iterator>
#include <stdexcept>
#include <string>
#include <vector>
struct Product {std::string vertexName,fragmentName;std::vector<uint32_t> vertex,fragment;bool storage=false,attr=false;};
static float table[4096][12];
extern "C" void occ_set(uint32_t id,const float *v){memcpy(table[id%4096],v,12*sizeof(float));}
static std::vector<uint32_t> shader(const char *key){
 const char *path=getenv(key);if(!path)throw std::runtime_error(std::string("missing ")+key);
 std::ifstream in(path,std::ios::binary);std::vector<char> bytes{std::istreambuf_iterator<char>(in),{}};
 if(bytes.size()<20 || bytes.size()%4)throw std::runtime_error("control SPIR-V");
 std::vector<uint32_t> words(bytes.size()/4);memcpy(words.data(),bytes.data(),bytes.size());return words;
}
static fuxi::Descriptor storageDescriptor(){fuxi::Descriptor d{};d.binding=1;d.type=7;d.stages=16;d.slot=1;d.range=16;return d;}
extern "C" uint32_t fuxi_m2v_abi(){return 8;}
extern "C" void *fuxi_m2v_open(const uint8_t *,size_t,char *,size_t){return nullptr;}
extern "C" void fuxi_m2v_close(void *){}
extern "C" int fuxi_m2v_function(void *,const char *,char *,size_t){return 0;}
extern "C" void fuxi_m2v_free(void *p){delete (Product *)p;}
extern "C" int fuxi_m2v_sampler(void *,uint32_t,fuxi::BakedSampler *out,char *,size_t){*out={};return 1;}
static uint8_t tag(const char *vn,const char *fn){unsigned h=17;for(const char *s=vn;*s;++s)h=h*31+uint8_t(*s);for(const char *s=fn;*s;++s)h=h*131+uint8_t(*s);return uint8_t(h|1);}
extern "C" int fuxi_m2v_identity(void *,const char *vn,void *,const char *fn,const fuxi::CompileOptions *options,uint8_t *out,char *,size_t){memset(out,tag(vn,fn),32);out[31]=options->orderedColorAccess;return 1;}
extern "C" int fuxi_m2v_product_hash(void *p,uint8_t *out){auto *x=(Product *)p;memset(out,tag(x->vertexName.c_str(),x->fragmentName.c_str()),32);return 1;}
extern "C" int fuxi_m2v_compile(void *,const char *vn,void *,const char *fn,void *,uint64_t,uint64_t,const fuxi::CompileOptions *options,fuxi::Translation *out,char *,size_t){
 auto *p=new Product{};p->vertexName=vn;p->fragmentName=fn;p->attr=p->vertexName=="vattr";p->storage=p->fragmentName=="storage";
 p->vertex=shader(p->attr?"CONTROL_VERTEX_ATTR":"CONTROL_VERTEX");p->fragment=shader(p->storage?"CONTROL_FRAGMENT_STORAGE":p->fragmentName=="kill"?"CONTROL_FRAGMENT_KILL":"CONTROL_FRAGMENT");
 *out={};out->handle=p;out->orderedColorAccess=options->orderedColorAccess;
 out->vertex=p->vertex.data();out->vertexCount=p->vertex.size();out->vertexEntry="main";out->fragment=p->fragment.data();out->fragmentCount=p->fragment.size();out->fragmentEntry="main";
 out->mask=out->colors[0].mask=p->fragmentName=="masked"?7:15;out->rangeCount=1;out->ranges[0]={0,48,17};out->pushOffset=0;out->pushSize=48;out->pushStages=17;
 auto &c=out->colors[0];const auto &f=p->fragmentName;
 using namespace fuxi;
 if(f=="over"){c.blend=1;c.factors[0]=c.factors[3]=6;c.factors[1]=c.factors[4]=7;}                    // SRC_ALPHA, ONE_MINUS_SRC_ALPHA: reads dst
 else if(f=="replace"){c.blend=1;c.factors[0]=c.factors[3]=1;c.factors[1]=c.factors[4]=0;}          // ONE, ZERO: dst independent
 else if(f=="modulate"){c.blend=1;c.factors[0]=c.factors[3]=4;c.factors[1]=c.factors[4]=0;}          // DST_COLOR * src: reads dst
 else if(f=="maxop"){c.blend=1;c.factors[0]=c.factors[3]=1;c.factors[1]=c.factors[4]=1;c.factors[2]=c.factors[5]=4;}
 if(f=="fetch")out->fetchMask=1; // flagged as a framebuffer reader without a descriptor: the shader itself never fetches, the analysis must still treat it as a reader
 if(p->attr){out->bindingCount=1;out->attributeCount=1;out->bindings[0]={0,0,8};out->attributes[0]={0,0,103,0};}
 out->descriptorCount=p->storage?1:0;if(p->storage)out->descriptors[0]=storageDescriptor();
 return 1;
}
extern "C" int fuxi_m2v_draw(void *product,const fuxi::DrawInput *in,fuxi::DrawOutput *out,char *,size_t){
 auto *p=(Product *)product;*out={};out->count=in->count;out->first=in->start;
 out->pushCount=1;out->pushes[0].offset=0;out->pushes[0].size=48;out->pushes[0].stages=17;memcpy(out->pushes[0].bytes,table[(in->start/6)%4096],48);
 if(p->attr){out->vertexCount=1;out->vertexBindings[0]=0;out->vertexSlots[0]=0;out->vertexOffsets[0]=in->buffers[0][0].offset;}
 if(p->storage){out->descriptorCount=1;out->descriptors[0]=storageDescriptor();}
 return 1;
}
extern "C" int fuxi_m2v_variant(void *,const fuxi::DrawInput *,fuxi::Translation *,char *,size_t){return 0;}
