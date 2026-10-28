/* Linux-only qualifier: the production Backend and dynamic bridge, local inputs.
 * No Apple-derived shader products are serialized. stdout is consumed privately
 * by the Python driver, which emits only bounded reasons/counts/digests. */
#include "fuxi-metal/Backend.hpp"
#include <fstream>
#include <iostream>
#include <iterator>
#include <memory>
static_assert(sizeof(fuxi::CompileOptions)==40364);
static_assert(sizeof(fuxi::Descriptor)==64 && sizeof(fuxi::BakedSampler)==44);
static_assert(sizeof(fuxi::DrawInput)==1056 && sizeof(fuxi::Translation)==2488);
int main(int argc,char **argv) {
 std::cout.setf(std::ios::unitbuf);
 if(argc!=5 && argc!=6)return 2;
 const size_t arenaOffset=argc==6?256:0,arenaLength=argc==6?256*1024:4096;
 const char *phase="open";
 try {
  std::ifstream in(argv[1],std::ios::binary);
  std::vector<uint8_t> bytes{std::istreambuf_iterator<char>(in),{}};
  auto &tr=fuxi::Translator::get();char why[512]{};
  auto lib=std::unique_ptr<void,void(*)(void *)>(tr.open(bytes.data(),bytes.size(),why,sizeof(why)),tr.close);
  if(!lib)throw std::runtime_error(why);
  std::ifstream fi(argv[4],std::ios::binary);std::vector<uint8_t> fragmentBytes{std::istreambuf_iterator<char>(fi),{}};
  auto fragmentLib=std::unique_ptr<void,void(*)(void *)>(tr.open(fragmentBytes.data(),fragmentBytes.size(),why,sizeof(why)),tr.close);
  if(!fragmentLib)throw std::runtime_error(why);
  fuxi::CompileOptions options{};const bool generic=strstr(argv[2],"_gen");
  if(!generic){options.strides[1]=48;options.attributes[0]={31,0,1};options.attributes[1]={29,16,1};options.attributes[2]={29,24,1};options.attributes[3]={27,32,1};}
  // Text input: stage, index (or name), public type, size, decimal packed value.
  unsigned stage,type,size;uint64_t constants[2][128]{};std::string index;uint64_t value;
  while(std::cin>>stage>>index>>type>>size>>value){
   if(stage>1 || options.constantCount[stage]>=128 || !size || size>8)throw std::runtime_error("constant input bounds");
   auto &c=options.constants[stage][options.constantCount[stage]++];c.type=type;c.size=size;
   if(index[0]>='0' && index[0]<='9')c.index=std::stoul(index);
   else {if(index.size()>=sizeof(c.name))throw std::runtime_error("constant name bound");memcpy(c.name,index.data(),index.size());}
   for(unsigned i=0;i<size;++i)c.bytes[i]=uint8_t(value>>(8*i));
   if(c.name[0]==0 && c.index<128)constants[stage][c.index]=value;
  }
  phase="device";fuxi::Backend backend;
  phase="compile";auto *p=backend.compilePipeline(lib.get(),argv[2],fragmentLib.get(),argv[3],options);
  auto *cached=backend.compilePipeline(lib.get(),argv[2],fragmentLib.get(),argv[3],options);
  if(cached!=p || p->references<3 || cached->blobHashes!=p->blobHashes || cached->constantBlobs.size()!=p->constantBlobs.size())throw std::runtime_error("cached blob identity mismatch");
  for(size_t i=0;i<p->constantBlobs.size();++i){auto *a=p->constantBlobs[i],*b=cached->constantBlobs[i];
   if(a!=b || a->length!=b->length || memcmp(a->contents,b->contents,a->length))throw std::runtime_error("cached blob ownership/content mismatch");}
  backend.destroy(p);p=cached;
  std::cout<<"counts "<<p->descriptors.size()<<' '<<p->constantBlobs.size()<<' '<<p->fetchMask<<'\n';
  for(const auto &d:p->descriptors)if(d.source==1)std::cout<<"blob_binding "<<d.set<<' '<<d.binding<<' '<<d.type<<' '<<d.stages<<' '<<d.range<<'\n';
  unsigned immutableCount=0;for(auto sampler:p->bakedSamplers)immutableCount+=sampler!=VK_NULL_HANDLE;std::cout<<"immutable_count "<<immutableCount<<'\n';
  for(const auto &hash:p->blobHashes){std::cout<<"blob_sha256 ";for(auto b:hash){const char hex[]="0123456789abcdef";std::cout<<hex[b>>4]<<hex[b&15];}std::cout<<'\n';}
  auto *vertices=backend.allocate(argc==6?arenaLength:192),*read=backend.allocate(256*256*4);
  auto *indices=backend.allocate(12);const uint16_t quad[]={0,1,2,2,1,3};memcpy(indices->contents,quad,sizeof(quad));
  // Position, two UV pairs, half4 color, 8 bytes of CA Surface padding.
  for(unsigned i=0;i<4;++i){auto *v=(uint8_t *)vertices->contents+arenaOffset+48*i;
   float pos[]={i&1?1.f:-1.f,i&2?1.f:-1.f,0.f,1.f};memcpy(v,pos,16);
   float uv[]={i&1?1.f:0.f,i&2?1.f:0.f};memcpy(v+16,uv,8);memcpy(v+24,uv,8);
   const uint16_t color[]={0x3800,0x3400,0x3000,0x3c00};memcpy(v+32,color,8);
  }
  fuxi::Buffer *bindings[2][fuxi::resourceSlots]{};
  for(auto &s:bindings)for(auto &b:s)b=backend.allocate(arenaLength);
  // MVP and texture matrix at their real slots. Buffers unused by a recipe are
  // intentionally present for diagnostic sweeps, with no invented heap/address.
  for(unsigned j=0;j<4;++j)((float *)((uint8_t *)bindings[0][2]->contents+arenaOffset))[j*5]=1;
  for(unsigned j=0;j<4;++j)((float *)((uint8_t *)bindings[0][3]->contents+arenaOffset))[j*5]=1;
  // Generic vertex config: Surface layout, no texcoords, base color enabled.
  auto *vertexConfig=(uint8_t *)bindings[0][0]->contents+arenaOffset;
  vertexConfig[0]=uint8_t(constants[0][52]);vertexConfig[1]=uint8_t(constants[0][53]);vertexConfig[2]=uint8_t(constants[0][3]);vertexConfig[3]=1;
  auto *config=(uint8_t *)bindings[1][0]->contents+arenaOffset;
  for(unsigned image=0;image<2;++image){auto *c=config+16*image;
   const unsigned configIndices[]={46,48,62,58,56,60,50,52,54};const unsigned offsets[]={0,1,2,4,8,9,10,11,12};
   for(unsigned i=0;i<9;++i){auto value=constants[1][configIndices[i]+image];c[offsets[i]]=uint8_t(value);if(i==3){c[5]=uint8_t(value>>8);c[6]=uint8_t(value>>16);}}
  }
  const unsigned configIndices[]={3,29,30,32,33,35,36,37};
  for(unsigned i=0;i<8;++i)config[32+i]=uint8_t(constants[1][configIndices[i]]);
  config[40]=1;for(unsigned i=0;i<8;++i)config[41+i]=uint8_t(constants[1][38+i]);
  auto *target=backend.allocateTexture(80,5),*sample=backend.allocateTexture(80,1);
  auto *sampler=backend.allocateSampler(false,false);
  auto *upload=backend.allocate(256*256*4);memset(upload->contents,255,upload->length);
  fuxi::Operation up{};up.texture=sample;up.src=upload;up.upload=true;
  fuxi::Operation draw{};draw.texture=target;draw.pipeline=p;draw.indices=indices;draw.vertices=6;draw.clear[3]=1;
  for(unsigned s=0;s<2;++s)for(unsigned i=0;i<fuxi::resourceSlots;++i)draw.buffers[s][i]={bindings[s][i],arenaOffset,false};
  draw.buffers[0][1]={vertices,arenaOffset,false};draw.buffers[0][4]={}; // Generic unused chroma matrix: exercise planned neutral buffer.
  for(unsigned i=0;i<fuxi::resourceSlots;++i){draw.samples[i]=sample;draw.samplers[i]=sampler;}
  fuxi::Operation rb{};rb.texture=target;rb.dst=read;
  phase="draw";backend.submit({up,draw,rb});
  // A real readback oracle for the ordinary solid recipes; sweep cases report
  // coverage and determinism, without pretending arbitrary constants mean solid.
  const uint8_t want[]={32,64,128,255};unsigned matches=0,changed=0;
  const auto *raw=(const uint8_t *)read->contents;
  for(unsigned i=0;i<65536;++i){matches+=memcmp(raw+4*i,want,4)==0;changed+=raw[4*i] || raw[4*i+1] || raw[4*i+2];}
  // Emit only a pixel digest (FNV-1a, diagnostic, not cache/security identity).
  uint64_t hash=14695981039346656037ULL;for(unsigned i=0;i<262144;++i){hash^=raw[i];hash*=1099511628211ULL;}
  std::cout<<"PASS "<<matches<<' '<<changed<<' '<<std::hex<<hash<<'\n';
  backend.destroy(upload);backend.destroy(sampler);backend.destroy(sample);backend.destroy(target);
  for(auto &s:bindings)for(auto *b:s)backend.destroy(b);
  backend.destroy(indices);backend.destroy(read);backend.destroy(vertices);backend.destroy(p);
  return 0;
 }catch(const std::exception &e){std::cout<<"FAIL "<<phase<<' '<<e.what()<<'\n';return 1;}
}
