/* FUXI_MTL_OCCLUDE pixel controls on the production backend and authored GLSL. The harness runs this binary with the knob off and on and requires identical PIXELS lines;
   with the knob on, each scenario also asserts exactly which draws were proven hidden. */
#include "Backend.hpp"
#include <cassert>
#include <cstdio>
#include <map>
using namespace fuxi;
static const uint32_t W=32;
int main(){
 Backend b;const bool on=systemTranslationPolicy().occlude;
 void *bridge=dlopen(FUXI_MTL_TRANSLATOR,RTLD_NOW|RTLD_LOCAL);assert(bridge);auto setDraw=reinterpret_cast<void (*)(uint32_t,const float *)>(dlsym(bridge,"occ_set"));assert(setDraw);
 auto *dst=b.allocateTexture(80,5,W,W);auto *read=b.allocate(W*W*4+64);auto *state=b.allocate(64);auto *vb=b.allocate(4096*6*8+64);
 CompileOptions plain,vopts;vopts.attributes[0]={29,0,0};vopts.strides[0]=8;
 std::map<std::string,Pipeline *> pipe;
 for(const char *name:{"solid","kill","over","replace","modulate","maxop","masked","fetch","storage"})pipe[name]=b.compilePipeline(nullptr,"control",nullptr,name,plain);
 pipe["vattr"]=b.compilePipeline(nullptr,"vattr",nullptr,"solid",vopts);
 uint32_t nextId=1;
 struct Rect {float x0,y0,x1,y1;};
 const Rect FULL{-1,-1,1,1},LEFT{-1,-1,0,1},RIGHT{0,-1,1,1},CENTER{-.5f,-.5f,.5f,.5f},BIG{-3,-3,3,3},SMALL{-.25f,-.25f,.25f,.25f};
 auto mk=[&](const char *name,Rect r,float angle,const float *color,uint32_t load,VkRect2D scissor={{0,0},{W,W}}){
  Operation op{};op.encoderID=1;op.pipeline=pipe.at(name);op.texture=dst;op.vertices=6;op.viewport={0,float(W),float(W),-float(W),0,1};op.scissor=scissor;op.load=load;
  uint32_t id=nextId++;op.first=id*6;float v[12]={r.x0,r.y0,r.x1,r.y1,std::sin(angle),std::cos(angle),0,0,color[0],color[1],color[2],color[3]};setDraw(id,v);
  if(!strcmp(name,"storage"))op.buffers[1][1]={state,0,false};
  if(op.pipeline==pipe["vattr"]){float *p=(float *)vb->contents+size_t(id)*12;const float c[6][2]={{0,0},{1,0},{0,1},{1,0},{1,1},{0,1}};for(int i=0;i<6;++i){p[i*2]=r.x0+(r.x1-r.x0)*c[i][0];p[i*2+1]=r.y0+(r.y1-r.y0)*c[i][1];}op.buffers[0][0]={vb,0,false};}
  if(load==2){op.clear[0]=.1f;op.clear[1]=.2f;op.clear[2]=.3f;op.clear[3]=1;}
  return op;};
 auto go=[&](const std::vector<Operation> &ops){std::function<void()> retire;b.submit(ops,{},systemTranslationPolicy().overlap?&retire:nullptr);if(retire)retire();};
 auto pixels=[&](const char *label){Operation get{};get.texture=dst;get.dst=read;go({get});printf("PIXELS %s ",label);for(size_t i=0;i<size_t(W)*W*4;++i)printf("%02x",((uint8_t *)read->contents)[i]);puts("");};
 auto scenario=[&](const char *label,const std::vector<Operation> &ops,int culled,int elideMin=0,int elideMax=99){
  auto c0=b.occCulled.load(),e0=b.occElided.load();go(ops);int c=int(b.occCulled.load()-c0),e=int(b.occElided.load()-e0);pixels(label);
  printf("OCC %s culled=%d elided=%d\n",label,c,e);fflush(stdout);
  if(on){if(c!=culled){fprintf(stderr,"%s: culled %d expected %d\n",label,c,culled);abort();}if(e<elideMin || e>elideMax){fprintf(stderr,"%s: elided %d expected %d..%d\n",label,e,elideMin,elideMax);abort();}}
  else assert(!c && !e);};
 const float RED[4]={1,0,0,1},GREEN[4]={0,1,0,1},BLUE[4]={0,0,1,1},HALF[4]={.8f,.4f,.2f,.5f},GREY[4]={.5f,.5f,.5f,1};
 scenario("stack",{mk("solid",FULL,0,RED,2),mk("solid",CENTER,0,GREEN,1),mk("solid",FULL,0,BLUE,1)},2);
 scenario("partial",{mk("solid",FULL,0,RED,2),mk("solid",CENTER,0,BLUE,1)},0);
 scenario("blend-over",{mk("solid",FULL,0,RED,2),mk("over",FULL,0,HALF,1)},0);
 scenario("blend-replace",{mk("solid",FULL,0,RED,2),mk("replace",FULL,0,HALF,1)},1);
 scenario("blend-dst-factor",{mk("solid",FULL,0,RED,2),mk("modulate",FULL,0,GREY,1)},0);
 scenario("blend-max",{mk("solid",FULL,0,RED,2),mk("maxop",FULL,0,GREY,1)},0);
 scenario("discard",{mk("solid",FULL,0,RED,2),mk("kill",FULL,0,BLUE,1)},0);
 scenario("mask-narrow-cover",{mk("solid",FULL,0,RED,2),mk("masked",FULL,0,BLUE,1)},0);
 scenario("mask-wide-cover",{mk("masked",FULL,0,RED,2),mk("solid",FULL,0,BLUE,1)},1);
 scenario("fetch-between",{mk("solid",FULL,0,RED,2),mk("fetch",CENTER,0,GREEN,1),mk("solid",FULL,0,BLUE,1)},0);
 scenario("rotated",{mk("solid",FULL,0,RED,2),mk("solid",BIG,.3f,BLUE,1)},0);
 scenario("oversize-axis-aligned",{mk("solid",FULL,0,RED,2),mk("solid",BIG,0,BLUE,1)},1);
 {memset(state->contents,0,64);scenario("storage-victim",{mk("storage",FULL,0,RED,2),mk("solid",FULL,0,BLUE,1)},0);printf("STATE %u\n",*(uint32_t *)state->contents);fflush(stdout);}
 scenario("vertex-attributes",{mk("vattr",FULL,0,RED,2),mk("vattr",FULL,0,BLUE,1)},1);
 scenario("scissor-covered",{mk("solid",FULL,0,RED,2),mk("solid",CENTER,0,GREEN,1,{{8,8},{16,16}}),mk("solid",CENTER,0,BLUE,1)},1);
 scenario("scissor-not-covered",{mk("solid",FULL,0,RED,2),mk("solid",FULL,0,GREEN,1,{{7,8},{16,16}}),mk("solid",CENTER,0,BLUE,1)},0);
 scenario("union-of-halves",{mk("solid",FULL,0,RED,2),mk("solid",LEFT,0,GREEN,1),mk("solid",RIGHT,0,BLUE,1)},1);
 scenario("footprint-is-the-scissor",{mk("solid",FULL,0,RED,2),mk("solid",SMALL,0,GREEN,1),mk("solid",SMALL,0,BLUE,1)},0);
 {auto again=std::vector<Operation>{mk("solid",FULL,0,RED,2),mk("solid",CENTER,0,GREEN,1),mk("solid",FULL,0,BLUE,1)};auto h0=b.occEvalHits.load();scenario("repeat-1",again,2);scenario("repeat-2",again,2);if(on)assert(b.occEvalHits.load()>h0);}
 // Load elision: the target holds the scenario above; a first op that loads and a coverer that paints everything may skip the load.
 scenario("elide-positive",{mk("solid",FULL,0,GREEN,1)},0,1,1);
 scenario("elide-blend-negative",{mk("over",FULL,0,HALF,1)},0,0,0);
 scenario("elide-partial-negative",{mk("solid",CENTER,0,GREEN,1)},0,0,0);
 scenario("elide-replace-positive",{mk("replace",FULL,0,HALF,1)},0,1,1);
 scenario("elide-masked-negative",{mk("masked",FULL,0,BLUE,1)},0,0,0);
 scenario("elide-fetch-first",{mk("fetch",CENTER,0,GREEN,1),mk("solid",FULL,0,BLUE,1)},0,0,1);
 // Randomized sequences: the pixels must match the knob-off run whatever was or was not proven.
 uint64_t seed=0x9e3779b97f4a7c15ull;auto rnd=[&](unsigned n){seed=seed*6364136223846793005ull+1442695040888963407ull;return unsigned((seed>>33)%n);};
 const Rect rects[]={FULL,LEFT,RIGHT,CENTER,BIG,SMALL,{-1,-1,1,0},{-.9f,-.9f,.9f,.9f}};const char *names[]={"solid","solid","solid","over","replace","modulate","maxop","masked","fetch","kill"};
 const VkRect2D scissors[]={{{0,0},{W,W}},{{8,8},{16,16}},{{0,0},{16,W}},{{16,0},{16,W}},{{0,0},{W,W}}};
 int fuzzCulled=0;
 for(int s=0;s<60;++s){
  std::vector<Operation> ops;unsigned n=2+rnd(6);
  for(unsigned i=0;i<n;++i){float color[4]={rnd(256)/255.f,rnd(256)/255.f,rnd(256)/255.f,.25f+rnd(4)/4.f};float angle=rnd(6)==0?.3f:0;
   ops.push_back(mk(names[rnd(10)],rects[rnd(8)],angle,color,i?1:(rnd(3)?2:1),scissors[rnd(5)]));}
  char label[32];snprintf(label,sizeof label,"fuzz-%d",s);auto c0=b.occCulled.load();go(ops);fuzzCulled+=int(b.occCulled.load()-c0);pixels(label);
 }
 printf("OCC fuzz culled=%d\n",fuzzCulled);
 if(on)assert(fuzzCulled>0);
 for(auto &p:pipe)b.destroy(p.second);b.destroy(dst);b.destroy(read);b.destroy(state);b.destroy(vb);
 puts("PASS: occlusion pixels");return 0;
}
