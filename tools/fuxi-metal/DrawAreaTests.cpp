/* FUXI_MTL_DRAW_AREA pixel controls on the production backend with the OCCLUDE test bridge (OcclusionBridge.cpp). The harness runs this binary with the knob off and on (margins 0 and 16,
   a zero CPU budget, with OCCLUDE) and requires identical PIXELS lines. Every scenario starts from the same patterned target, then LOADs it; with the knob on, each scenario also asserts
   whether the render area had to shrink below the scissor union (S), must not shrink (N, refusals and full-coverage cases) or may do either (A). */
#include "Backend.hpp"
#include <cassert>
#include <cstdio>
#include <map>
using namespace fuxi;
static const uint32_t W=256;
int main(){
 Backend b;const auto &pol=systemTranslationPolicy();const bool on=pol.drawArea && pol.renderArea,starved=on && pol.drawAreaBudgetUs==0,loose=getenv("FUXI_MTL_DRAW_AREA_EXPECT") && !strcmp(getenv("FUXI_MTL_DRAW_AREA_EXPECT"),"any"); // loose: the CPU budget may run out, only pixels are checked
 void *bridge=dlopen(FUXI_MTL_TRANSLATOR,RTLD_NOW|RTLD_LOCAL);assert(bridge);auto setDraw=reinterpret_cast<void (*)(uint32_t,const float *)>(dlsym(bridge,"occ_set"));assert(setDraw);
 auto *dst=b.allocateTexture(80,5,W,W);auto *read=b.allocate(W*W*4+64);auto *state=b.allocate(64);auto *vb=b.allocate(4096*6*8+64);auto *gpuVb=b.allocate(W*W*4+64);
 CompileOptions plain,vopts;vopts.attributes[0]={29,0,0};vopts.strides[0]=8;
 std::map<std::string,Pipeline *> pipe;
 for(const char *name:{"solid","kill","over","replace","modulate","maxop","masked","fetch","storage"})pipe[name]=b.compilePipeline(nullptr,"control",nullptr,name,plain);
 pipe["vattr"]=b.compilePipeline(nullptr,"vattr",nullptr,"solid",vopts);
 uint32_t nextId=1;
 struct Rect {float x0,y0,x1,y1;};
 const Rect FULL{-1,-1,1,1},CENTER{-.5f,-.5f,.5f,.5f},BIG{-3,-3,3,3},SMALL{-.2f,-.1f,.1f,.25f},CORNER_A{-1,-1,-.8f,-.8f},CORNER_B{.8f,.8f,1,1},NEAR_A{-.7f,.3f,-.5f,.45f},OUTSIDE{1.5f,1.5f,2.5f,2.5f},EDGE{.93f,-.4f,1.4f,-.31f};
 auto mk=[&](const char *name,Rect r,float angle,const float *color,uint32_t load,VkRect2D scissor={{0,0},{W,W}},uint32_t encoder=1){
  Operation op{};op.encoderID=encoder;op.pipeline=pipe.at(name);op.texture=dst;op.vertices=6;op.viewport={0,float(W),float(W),-float(W),0,1};op.scissor=scissor;op.load=load;
  uint32_t id=nextId++%4000+1;op.first=id*6;float v[12]={r.x0,r.y0,r.x1,r.y1,std::sin(angle),std::cos(angle),0,0,color[0],color[1],color[2],color[3]};setDraw(id,v);
  if(!strcmp(name,"storage"))op.buffers[1][1]={state,0,false};
  if(op.pipeline==pipe["vattr"]){float *p=(float *)vb->contents+size_t(id)*12;const float c[6][2]={{0,0},{1,0},{0,1},{1,0},{1,1},{0,1}};for(int i=0;i<6;++i){p[i*2]=r.x0+(r.x1-r.x0)*c[i][0];p[i*2+1]=r.y0+(r.y1-r.y0)*c[i][1];}op.buffers[0][0]={vb,0,false};}
  if(load==2){op.clear[0]=.1f;op.clear[1]=.2f;op.clear[2]=.3f;op.clear[3]=1;}
  return op;};
 auto go=[&](const std::vector<Operation> &ops){std::function<void()> retire;b.submit(ops,{},pol.overlap?&retire:nullptr);if(retire)retire();};
 auto pixels=[&](const char *label){Operation get{};get.texture=dst;get.dst=read;go({get});uint64_t h=0xcbf29ce484222325ull;const uint8_t *px=(const uint8_t *)read->contents;
  for(size_t i=0;i<size_t(W)*W*4;++i)h=(h^px[i])*0x100000001b3ull;
  printf("PIXELS %s %016llx",label,(unsigned long long)h);for(uint32_t y=0;y<W;y+=37)for(uint32_t x=0;x<W;x+=29)printf(" %08x",((const uint32_t *)px)[y*W+x]);puts("");}; // hash of every byte plus a sparse sample for diagnosis
 // Base pattern (CLEAR pass, never eligible): a 4x4 grid of coloured quads, so a pixel lost outside the render area shows up.
 auto base=[&](){std::vector<Operation> ops;const float CL[4]={0,0,0,1};ops.push_back(mk("solid",FULL,0,CL,2));
  for(int j=0;j<4;++j)for(int i=0;i<4;++i){float c[4]={i/3.f,j/3.f,(i+j)/6.f,1};ops.push_back(mk("solid",Rect{-1+i*.5f,-1+j*.5f,-.6f+i*.5f,-.55f+j*.5f},0,c,1));}go(ops);};
 enum Expect {S,N,A,M};int failures=0; // M: N when ops of one encoder share a render pass (fuxigpumerge=1), S when every op is its own pass
 auto scenario=[&](const char *label,std::vector<Operation> ops,Expect e,bool mustFail=false,bool withBase=true){
  if(withBase)base();auto s0=b.daShrunk.load(),f0=b.daFailed.load();go(ops);int s=int(b.daShrunk.load()-s0),f=int(b.daFailed.load()-f0);pixels(label);
  printf("DA %s shrunk=%d failed=%d\n",label,s,f);fflush(stdout);
  if(e==M)e=pol.merge?N:S;if(loose && e==S)e=A;
  if(!on || starved){if(s){fprintf(stderr,"%s: shrunk %d with the knob %s\n",label,s,on?"starved":"off");abort();}return;}
  if((e==S && s<1) || (e==N && s) || (mustFail && f<1)){fprintf(stderr,"%s: shrunk=%d failed=%d expected %s%s\n",label,s,f,e==S?"S":e==N?"N":"A",mustFail?" with a failed bound":"");++failures;}};
 const float RED[4]={1,0,0,1},GREEN[4]={0,1,0,1},BLUE[4]={0,0,1,1},HALF[4]={.8f,.4f,.2f,.5f},GREY[4]={.5f,.5f,.5f,1};
 scenario("small",{mk("solid",SMALL,0,RED,1)},S);
 scenario("rotated",{mk("solid",SMALL,.7f,GREEN,1)},S);
 scenario("rotated-big-covers",{mk("solid",BIG,.3f,BLUE,1)},N);
 {auto o=mk("solid",SMALL,.4f,BLUE,1);o.topology=Topology::TriangleStrip;o.vertices=4;scenario("strip",{o},S);}
 {auto o=mk("solid",SMALL,0,BLUE,1);o.topology=Topology::Line;scenario("line-topology",{o},N,true);}
 scenario("scissor-inside-geometry",{mk("solid",CENTER,0,GREEN,1,{{100,100},{20,20}})},A);
 scenario("scissor-cuts-geometry",{mk("solid",CENTER,0,GREEN,1,{{0,0},{W,140}})},S);
 scenario("scissor-misses-geometry",{mk("solid",SMALL,0,GREEN,1,{{0,0},{40,40}}),mk("solid",NEAR_A,0,RED,1)},S);
 scenario("two-near",{mk("solid",SMALL,0,RED,1),mk("over",NEAR_A,.2f,HALF,1)},S);
 scenario("opposite-corners",{mk("solid",CORNER_A,0,RED,1),mk("solid",CORNER_B,0,GREEN,1)},M);
 scenario("blend-over",{mk("over",SMALL,0,HALF,1)},S);
 scenario("blend-dst-factor",{mk("modulate",SMALL,.5f,GREY,1)},S);
 scenario("blend-max",{mk("maxop",CENTER,.1f,GREY,1)},S);
 scenario("replace",{mk("replace",SMALL,0,HALF,1)},S);
 scenario("discard",{mk("kill",SMALL,0,BLUE,1)},S);
 scenario("masked",{mk("masked",SMALL,0,BLUE,1)},S);
 scenario("fetch",{mk("fetch",SMALL,0,GREEN,1),mk("solid",NEAR_A,0,BLUE,1)},S);
 {memset(state->contents,0,64);scenario("storage-writer",{mk("solid",SMALL,0,RED,1),mk("storage",NEAR_A,0,BLUE,1)},N);printf("STATE %u\n",*(uint32_t *)state->contents);fflush(stdout);}
 {memset(state->contents,0,64);auto st=mk("storage",CENTER,0,GREY,1);st.encoderID=7;scenario("storage-elsewhere-in-submit",{st,mk("solid",SMALL,0,RED,1,{{0,0},{W,W}},8)},N);printf("STATE %u\n",*(uint32_t *)state->contents);fflush(stdout);}
 scenario("vertex-attributes",{mk("vattr",SMALL,0,RED,1)},S);
 // A vertex buffer the GPU has written (the backend already splits such draws into a later submit): Buffer::daWritten is sticky, so the draw is never bounded from CPU contents.
 {base();auto o=mk("vattr",SMALL,0,GREEN,1);o.buffers[0][0]={gpuVb,0,false};Operation copy{};copy.texture=dst;copy.dst=gpuVb;go({copy});
  float *q=(float *)((uint8_t *)gpuVb->contents+o.first*8);const float c[6][2]={{0,0},{1,0},{0,1},{1,0},{1,1},{0,1}};for(int i=0;i<6;++i){q[i*2]=SMALL.x0+(SMALL.x1-SMALL.x0)*c[i][0];q[i*2+1]=SMALL.y0+(SMALL.y1-SMALL.y0)*c[i][1];}
  scenario("gpu-written-vertex-buffer",{o},N,true,false);}
 scenario("outside-only",{mk("solid",OUTSIDE,0,RED,1)},N);
 scenario("outside-plus-small",{mk("solid",OUTSIDE,0,RED,1),mk("solid",SMALL,0,GREEN,1)},S);
 scenario("edge-straddle",{mk("solid",EDGE,.25f,BLUE,1)},S);
 {auto o=mk("solid",SMALL,0,RED,1);o.viewport={40,40+128,128,-128,0,1};auto p=mk("solid",FULL,0,GREEN,1);p.viewport={180,250,60,-30,0,1};scenario("offset-viewports",{o,p},S);}
 {auto o=mk("solid",BIG,.6f,RED,1);o.viewport={200,float(W),56,-56,0,1};scenario("viewport-corner-oversized-geometry",{o},A);} // geometry beyond the viewport: the box is clamped by scissor and target only
 {auto o=mk("solid",SMALL,0,BLUE,1);o.vertices=1206;scenario("vertex-count-limit",{o},N,true);}
 scenario("separate-passes",{mk("solid",SMALL,0,RED,1,{{0,0},{W,W}},2),mk("over",NEAR_A,0,HALF,1,{{0,0},{W,W}},3),mk("solid",CORNER_B,0,GREEN,1,{{0,0},{W,W}},4)},S);
 scenario("clear-first-not-eligible",{mk("solid",SMALL,0,RED,2),mk("solid",NEAR_A,0,GREEN,1)},M);
 scenario("hidden-under-later",{mk("solid",SMALL,0,RED,1),mk("solid",SMALL,0,GREEN,1)},S);
 {auto again=std::vector<Operation>{mk("solid",SMALL,.3f,RED,1),mk("over",NEAR_A,0,HALF,1)};scenario("repeat-1",again,S);scenario("repeat-2",again,S);}
 // Randomized sequences over the patterned target: pixels must match the knob-off run whatever was or was not bounded.
 uint64_t seed=0x9e3779b97f4a7c15ull;auto rnd=[&](unsigned n){seed=seed*6364136223846793005ull+1442695040888963407ull;return unsigned((seed>>33)%n);};
 const Rect rects[]={FULL,CENTER,BIG,SMALL,CORNER_A,CORNER_B,NEAR_A,OUTSIDE,EDGE,{-.05f,-.05f,.05f,.05f},{.3f,-.9f,.34f,-.2f}};const char *names[]={"solid","solid","over","replace","modulate","maxop","masked","fetch","kill","vattr"};
 const VkRect2D scissors[]={{{0,0},{W,W}},{{0,0},{W,W}},{{64,32},{100,90}},{{0,0},{128,W}},{{130,0},{126,W}},{{0,200},{W,56}}};
 int fuzzShrunk=0;
 for(int s=0;s<80;++s){
  base();std::vector<Operation> ops;unsigned n=1+rnd(6);
  for(unsigned i=0;i<n;++i){float color[4]={rnd(256)/255.f,rnd(256)/255.f,rnd(256)/255.f,.25f+rnd(4)/4.f};float angle=rnd(4)==0?rnd(100)/31.f:0;Rect r=rects[rnd(11)];float dx=(int(rnd(9))-4)/10.f;r.x0+=dx;r.x1+=dx;
   auto o=mk(names[rnd(10)],r,angle,color,i?1:(rnd(6)?1:2),scissors[rnd(6)],1+rnd(3));if(rnd(5)==0 && o.pipeline!=pipe["vattr"]){o.topology=Topology::TriangleStrip;o.vertices=4;}ops.push_back(o);}
  char label[32];snprintf(label,sizeof label,"fuzz-%d",s);auto s0=b.daShrunk.load();go(ops);fuzzShrunk+=int(b.daShrunk.load()-s0);pixels(label);
 }
 printf("DA fuzz shrunk=%d\n",fuzzShrunk);
 printf("DA totals passes=%llu shrunk=%llu bounded=%llu failed=%llu budget=%llu retired=%llu\n",(unsigned long long)b.daPasses.load(),(unsigned long long)b.daShrunk.load(),(unsigned long long)b.daBounded.load(),(unsigned long long)b.daFailed.load(),(unsigned long long)b.daBudget.load(),(unsigned long long)b.daRetired.load());fflush(stdout);
 if(on && !starved && !loose){assert(fuzzShrunk>0);assert(!b.daRetired.load());}
 if(starved)assert(b.daBudget.load()>0 && !b.daBounded.load());
 if(!on)assert(!b.daPasses.load() && !b.daTries.load());
 b.reportDrawArea();
 for(auto &p:pipe)b.destroy(p.second);b.destroy(dst);b.destroy(read);b.destroy(state);b.destroy(vb);b.destroy(gpuVb);
 if(failures){fprintf(stderr,"%d draw area expectation(s) failed\n",failures);return 1;}
 puts("PASS: draw area pixels");return 0;
}
