/* CPU vertex evaluator against a glslang-compiled shader (argv[1] = SPIR-V file): push-constant quad, uniform matrix, spec-constant branch, attributes, loop, clip-w branch. */
#include "SpirvEval.hpp"
#include <cassert>
#include <cstdio>
#include <fstream>
#include <iterator>
using namespace fuxi;
static float F(uint32_t u){float f;memcpy(&f,&u,4);return f;}
static uint32_t U(float f){uint32_t u;memcpy(&u,&f,4);return u;}
int main(int argc,char **argv){
 assert(argc>1);std::ifstream in(argv[1],std::ios::binary);std::vector<char> raw((std::istreambuf_iterator<char>(in)),{});assert(raw.size()%4==0 && raw.size()>20);
 std::vector<uint32_t> w(raw.size()/4);memcpy(w.data(),raw.data(),raw.size());
 float m[16]={2,0,0,0, 0,3,0,0, 0,0,1,0, -1,-1,0,1}; // column-major
 float k[8]={0.5f,0.25f,0,0, 0,0,0,0};
 std::vector<uint8_t> ub(sizeof m+sizeof k);memcpy(ub.data(),m,sizeof m);memcpy(ub.data()+sizeof m,k,sizeof k);
 VsInputs vi;vi.buffers[{0,1}]={ub.data(),ub.size()};float rect[4]={0.1f,0.2f,0.6f,0.7f};memcpy(vi.push,rect,16);vi.pushSize=16;
 vi.attribute=[](uint32_t loc,uint32_t v,uint32_t o[4]){if(loc==0){o[0]=U(float(v)*10);o[1]=U(1);return true;}if(loc==1){o[0]=2;o[1]=0;return true;}return false;};
 {SpirvEval ev(w.data(),w.size());auto r=ev.run("main",vi,{0,1,2,3,4,5});assert(r.ok);assert(r.position.size()==6);
  // vertex 4 -> corner (1,1): p=(0.6,0.7); q = (2*0.6-1, 3*0.7-1) + 2*(0.5,0.25)
  auto &p=r.position[4];printf("%f %f %f %f\n",p[0],p[1],p[2],p[3]);
  assert(std::fabs(p[0]-(1.2f-1+1.0f))<1e-5 && std::fabs(p[1]-(2.1f-1+0.5f))<1e-5 && p[2]==0.5f && p[3]==1.0f);
  auto &q=r.position[0];assert(std::fabs(q[0]-(0.2f-1+1.0f))<1e-5 && std::fabs(q[1]-(0.6f-1+0.5f))<1e-5);}
 {VsInputs v2=vi;v2.spec[3]=1;SpirvEval ev(w.data(),w.size());auto r=ev.run("main",v2,{6});assert(r.ok);auto &p=r.position[0];printf("spec %f %f %f\n",p[0],p[1],p[3]);
  assert(p[3]==-1.0f);} 
 {VsInputs v2=vi;v2.spec[3]=1;SpirvEval ev(w.data(),w.size());auto r=ev.run("main",v2,{2});assert(r.ok);assert(std::fabs(r.position[0][0]-(40.f-1+1))<1e-4 && r.position[0][3]==1.0f);}
 {VsInputs v2=vi;v2.attribute=nullptr;v2.spec[3]=1;SpirvEval ev(w.data(),w.size());auto r=ev.run("main",v2,{0});assert(!r.ok && r.why.find("attribute")!=std::string::npos);}
 {VsInputs v2=vi;v2.buffers.clear();SpirvEval ev(w.data(),w.size());auto r=ev.run("main",v2,{0});assert(!r.ok);} // missing uniform bytes must fail, not read zeros
 {SpirvEval ev(w.data(),w.size());auto r=ev.run("nope",vi,{0});assert(!r.ok);}
 {SpirvEval ev(w.data(),8);auto r=ev.run("main",vi,{0});assert(!r.ok);}
 (void)F;puts("SpirvEvalTests ok");return 0;}
