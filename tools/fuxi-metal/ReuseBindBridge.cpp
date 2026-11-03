/* Test-only authored SPIR-V adapter for the FUXI_MTL_DESC_REUSE / FUXI_MTL_BIND_SKIP pixel A/B. Never built into the production bridge.
   A vertex buffer (binding 0, vec2), a sampled texture and a uniform buffer (tint), two push ranges (fragment color at 0, vertex offset at
   16) and indexed draws. Push values come from the offsets of two otherwise unused bindings (fragment slot 1, vertex slot 2), so a test
   sets them per draw through ordinary buffer binds; the plan cache keys on those offsets as on any input. */
#include <vulkan/vulkan.h>
#include "Translator.hpp"
#include <cstring>
#include <fstream>
#include <iterator>
#include <vector>
struct Product {std::vector<uint32_t> vertex,fragment;};
static std::vector<uint32_t> shader(const char *key){
 std::ifstream in(getenv(key),std::ios::binary);std::vector<char> bytes{std::istreambuf_iterator<char>(in),{}};
 if(bytes.size()<20 || bytes.size()%4)throw std::runtime_error("reuse/bind SPIR-V");
 std::vector<uint32_t> words(bytes.size()/4);memcpy(words.data(),bytes.data(),bytes.size());return words;
}
static void descriptors(fuxi::Descriptor *out,uint64_t tintOffset){
 out[0]={0,VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER,16,0,0,44};
 out[1]={};out[1].binding=1;out[1].type=VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER;out[1].stages=16;out[1].slot=3;out[1].offset=tintOffset;out[1].range=16;
}
extern "C" uint32_t fuxi_m2v_abi(){return 8;}
extern "C" void *fuxi_m2v_open(const uint8_t *,size_t,char *,size_t){return nullptr;}
extern "C" void fuxi_m2v_close(void *){}
extern "C" int fuxi_m2v_function(void *,const char *,char *,size_t){return 0;}
extern "C" void fuxi_m2v_free(void *p){delete (Product *)p;}
extern "C" int fuxi_m2v_sampler(void *,uint32_t,fuxi::BakedSampler *out,char *,size_t){*out={};return 1;}
// "bind2" is a second pipeline (own pipeline layout) with the same shaders.
extern "C" int fuxi_m2v_identity(void *,const char *,void *,const char *name,const fuxi::CompileOptions *,uint8_t *out,char *,size_t){memset(out,!strcmp(name,"bind2")?12:11,32);return 1;}
extern "C" int fuxi_m2v_product_hash(void *,uint8_t *out){memset(out,13,32);return 1;}
extern "C" int fuxi_m2v_compile(void *,const char *,void *,const char *,void *,uint64_t,uint64_t,const fuxi::CompileOptions *options,fuxi::Translation *out,char *,size_t){
 auto *p=new Product{shader("BIND_VERTEX"),shader("BIND_FRAGMENT")};
 *out={};out->handle=p;out->orderedColorAccess=options->orderedColorAccess;
 out->vertex=p->vertex.data();out->vertexCount=p->vertex.size();out->vertexEntry="main";
 out->fragment=p->fragment.data();out->fragmentCount=p->fragment.size();out->fragmentEntry="main";
 // Blending makes order, a missing draw and a repeated draw visible.
 out->mask=out->colors[0].mask=15;out->colors[0].blend=1;auto *f=out->colors[0].factors;f[0]=f[3]=VK_BLEND_FACTOR_SRC_ALPHA;f[1]=f[4]=VK_BLEND_FACTOR_ONE_MINUS_SRC_ALPHA;f[2]=f[5]=VK_BLEND_OP_ADD;
 out->rangeCount=2;out->ranges[0]={0,16,16};out->ranges[1]={16,8,1};
 out->bindingCount=1;out->bindings[0]={0,0,8};out->attributeCount=1;out->attributes[0]={0,0,VK_FORMAT_R32G32_SFLOAT,0};
 out->descriptorCount=2;descriptors(out->descriptors,0);return 1;
}
extern "C" int fuxi_m2v_draw(void *,const fuxi::DrawInput *in,fuxi::DrawOutput *out,char *,size_t){
 if(!in->formats[0] || !in->filters[0])return 0;
 *out={};out->count=in->count;out->first=in->indexed?0:in->start;out->indexed=in->indexed;out->indexOffset=in->indexed?in->indexOffset:0;
 out->descriptorCount=2;descriptors(out->descriptors,in->buffers[1][3].offset);
 out->vertexCount=1;out->vertexBindings[0]=0;out->vertexSlots[0]=0;out->vertexOffsets[0]=in->buffers[0][0].bytes?0:in->buffers[0][0].offset;
 uint64_t c=in->buffers[1][1].offset/16,o=in->buffers[0][2].offset/16;
 float color[4]={1.0f,float(c%8)/8,float(c/8%8)/8,0.25f+float(c%3)/8},offset[2]={float(o%4)/8,float(o/4%4)/8};
 out->pushCount=2;out->pushes[0].offset=0;out->pushes[0].size=16;out->pushes[0].stages=16;memcpy(out->pushes[0].bytes,color,16);
 out->pushes[1].offset=16;out->pushes[1].size=8;out->pushes[1].stages=1;memcpy(out->pushes[1].bytes,offset,8);
 return 1;
}
extern "C" int fuxi_m2v_variant(void *,const fuxi::DrawInput *,fuxi::Translation *,char *,size_t){return 0;}
