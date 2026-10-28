/* Linux host checks: typed constants cross the real C/Rust bridge; no submits. */
#include "fuxi-metal/Translator.hpp"
#include "fuxi-metal/CACompare.h"
#include <vulkan/vulkan.h>
#include <cassert>
#include <cstring>
#include <fstream>
#include <iterator>
#include <vector>
static_assert(sizeof(fuxi::Constant)==156 && offsetof(fuxi::CompileOptions,constants)==160);
static_assert(sizeof(fuxi::CompileOptions)==40364);
int main(int argc,char **argv) {
 assert(argc==3);
 for(auto base:{3u,16u,29u,33u,37u,41u,45u,49u,53u}) {
  unsigned width=base==3 || base==29 || base==33?4:base==16 || base==37 || base==41?2:1;
  for(unsigned i=0;i<4;++i) {
   assert(fuxi::constantPayloadSize(base+i)==width*(i+1));
   assert(fuxi::constantElementStride(base+i)==width*(i==2?4:i+1));
  }
 }
 assert(!fuxi::constantPayloadSize(0) && !fuxi::constantPayloadSize(7) && !fuxi::constantPayloadSize(60));
 std::vector<uint8_t> cpu(256*256*4,128),gpu(1088*256,128);
 assert(fuxiCAAccept(fuxiCACompare(gpu.data(),1088,cpu.data())));
 gpu[0]=193;assert(!fuxiCAAccept(fuxiCACompare(gpu.data(),1088,cpu.data())));gpu[0]=128;
 for(unsigned i=0;i<257;++i)gpu[(i/256)*1088+(i%256)*4]=160;
 assert(!fuxiCAAccept(fuxiCACompare(gpu.data(),1088,cpu.data())));
 std::fill(gpu.begin(),gpu.end(),130);assert(!fuxiCAAccept(fuxiCACompare(gpu.data(),1088,cpu.data())));
 std::fill(gpu.begin(),gpu.end(),129);assert(fuxiCAAccept(fuxiCACompare(gpu.data(),1088,cpu.data())));
 // A one-pixel edge shift passes; the same pattern flipped vertically does not.
 for(unsigned y=0;y<256;++y)for(unsigned x=0;x<256;++x){uint8_t v=y<100?200:20;cpu[(y*256+x)*4]=v;gpu[y*1088+x*4]=y<101?200:20;}
 for(unsigned i=0;i<256*256;++i)for(unsigned c=1;c<4;++c){cpu[i*4+c]=128;gpu[(i/256)*1088+(i%256)*4+c]=128;}
 assert(fuxiCAAccept(fuxiCACompare(gpu.data(),1088,cpu.data())));
 for(unsigned y=0;y<256;++y)for(unsigned x=0;x<256;++x)gpu[y*1088+x*4]=255-y<100?200:20;
 assert(!fuxiCAAccept(fuxiCACompare(gpu.data(),1088,cpu.data())));
 puts("PASS: scalar/vector payload and padded stride; pixel max/mean/outlier limits, padded rows, one-pixel edge tolerance, flip rejection");
 void *icd=dlopen(argv[2],RTLD_NOW|RTLD_LOCAL);assert(icd);
 auto gipa=(PFN_vkGetInstanceProcAddr)dlsym(icd,"vk_icdGetInstanceProcAddr");assert(gipa);
 auto create=(PFN_vkCreateInstance)gipa(nullptr,"vkCreateInstance");assert(create);
 VkApplicationInfo app{};app.sType=VK_STRUCTURE_TYPE_APPLICATION_INFO;app.apiVersion=VK_API_VERSION_1_2;
 VkInstanceCreateInfo info{};info.sType=VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO;info.pApplicationInfo=&app;
 VkInstance instance{};assert(create(&info,nullptr,&instance)==VK_SUCCESS);
 auto enumerate=(PFN_vkEnumeratePhysicalDevices)gipa(instance,"vkEnumeratePhysicalDevices");
 uint32_t n=1;VkPhysicalDevice physical{};assert(enumerate(instance,&n,&physical)==VK_SUCCESS && n);
 std::ifstream input(argv[1],std::ios::binary);std::vector<uint8_t> bytes{std::istreambuf_iterator<char>(input),{}};
 auto &tr=fuxi::Translator::get();char error[2048]{};void *library=tr.open(bytes.data(),bytes.size(),error,sizeof(error));assert(library);
 fuxi::CompileOptions o{};o.constantCount[1]=58;
 for(unsigned i=0;i<58;++i) {
  auto &c=o.constants[1][i];c.index=i;c.type=i==57?51:i%2?49:53;c.size=i==57?3:1;
  if(i%2)snprintf(c.name,sizeof(c.name),"c%u",i);
 }
 std::vector<uint32_t> previous;
 for(unsigned round=0;round<3;++round) {
  if(round==2)o.constants[1][57].bytes[0]=1;
  fuxi::Translation out{};
  int ok=tr.compile(library,"control_vertex",library,"control_fragment",(void *)gipa,(uint64_t)instance,(uint64_t)physical,&o,&out,error,sizeof(error));
  if(!ok){fprintf(stderr,"FAIL: %s\n",error);return 1;}
  std::vector<uint32_t> words(out.fragment,out.fragment+out.fragmentCount);
  if(round==1)assert(previous==words);
  if(round==2)assert(previous!=words);
  for(unsigned stage=0;stage<2;++stage) {
   std::ofstream spirv(std::string(argv[1])+std::to_string(round)+"-"+std::to_string(stage)+".spv",std::ios::binary);
   spirv.write((const char *)(stage?out.fragment:out.vertex),(std::streamsize)((stage?out.fragmentCount:out.vertexCount)*4));assert(spirv.good());
  }
  previous=words;tr.free(out.handle);
 }
 for(unsigned bad=0;bad<3;++bad) {
  if(bad==0)o.constantCount[1]=129;
  if(bad==1){o.constantCount[1]=58;o.constants[1][57].size=4;}
  if(bad==2){o.constants[1][57].size=3;o.constants[1][57].type=7;}
  fuxi::Translation out{};
  assert(!tr.compile(library,"control_vertex",library,"control_fragment",(void *)gipa,(uint64_t)instance,(uint64_t)physical,&o,&out,error,sizeof(error)));
  assert(!out.handle);
 }
 tr.close(library);
 auto destroy=(PFN_vkDestroyInstance)gipa(instance,"vkDestroyInstance");destroy(instance,nullptr);dlclose(icd);
 puts("PASS: ABI8 58 named/indexed bool/uchar/uchar3 constants, linked specialization, cache hit/change, overflow/type/size refusal; no GPU submits");
}
