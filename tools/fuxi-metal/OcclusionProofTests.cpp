/* Occlusion proof helpers: rect unions, blend independence, attribute decode, quad / triangle coverage. */
#include "OcclusionProof.hpp"
#include <cassert>
#include <cstdio>
using namespace fuxi;
using P=std::array<float,4>;
static std::vector<P> quad(float x0,float y0,float x1,float y1){return {P{x0,y0,.5f,1},P{x1,y0,.5f,1},P{x0,y1,.5f,1},P{x1,y0,.5f,1},P{x1,y1,.5f,1},P{x0,y1,.5f,1}};}
int main(){
 assert(coveredByUnion({0,0,10,10},{{0,0,10,5},{0,5,10,10}}));
 assert(!coveredByUnion({0,0,10,10},{{0,0,10,5},{0,6,10,10}}));
 assert(coveredByUnion({2,2,8,8},{{0,0,5,10},{5,0,10,10}}));
 assert(!coveredByUnion({0,0,10,10},{}));
 uint32_t opaque[6]={1,0,0,1,0,0},over[6]={1,7,0,1,7,0},dst[6]={4,0,0,1,0,0},mn[6]={1,0,3,1,0,0},satur[6]={14,0,0,1,0,0},add[6]={6,1,0,1,1,0};
 assert(blendDstIndependent(0,over) && blendDstIndependent(1,opaque) && !blendDstIndependent(1,over) && !blendDstIndependent(1,dst) && !blendDstIndependent(1,mn) && !blendDstIndependent(1,satur) && !blendDstIndependent(1,add));
 uint32_t l[4];uint8_t b[16]={0,0,0x80,0x3f,0,0,0,0x40,0,0,0,0};
 assert(decodeAttribute(103,b,8,l) && l[0]==0x3f800000u && l[1]==0x40000000u && l[3]==0x3f800000u);assert(!decodeAttribute(103,b,7,l));assert(!decodeAttribute(1,b,8,l));
 uint8_t c8[4]={255,0,128,255};assert(decodeAttribute(44,c8,4,l));float f;memcpy(&f,&l[0],4);assert(std::fabs(f-128/255.f)<1e-6);memcpy(&f,&l[2],4);assert(f==1.0f);
 uint8_t h[4]={0,0x3c,0,0xc0};assert(decodeAttribute(83,h,4,l));memcpy(&f,&l[0],4);assert(f==1.0f);memcpy(&f,&l[1],4);assert(f==-2.0f);
 ViewportF vp{0,100,100,-100}; // Metal style: negative height, 100x100 target
 IRect clip{0,0,100,100};Cover c;std::string why;
 assert(buildCover(quad(-1,-1,1,1),0,vp,clip,c,why) && c.rects.size()==1 && c.covers({0,0,100,100}) && c.covers({10,10,90,90}));
 assert(buildCover(quad(-0.5f,-0.5f,0.5f,0.5f),0,vp,clip,c,why) && c.covers({25,25,75,75}) && !c.covers({24,25,75,75}) && !c.covers({0,0,100,100}));
 // fractional edge: rect [25.3,74.6] covers pixel centers 25.5..74.5 -> pixels 26..74 safe with the 0.25 margin
 assert(buildCover(quad(-0.494f,-0.492f,0.492f,0.494f),0,vp,clip,c,why) && c.covers({26,26,73,73}) && !c.covers({25,25,75,75}));
 // clip narrower than the quad
 assert(buildCover(quad(-1,-1,1,1),0,vp,{0,0,50,100},c,why) && !c.covers({0,0,100,100}) && c.covers({0,0,50,100}));
 // rotated quad: no exact rect, an inscribed rect is still inside one triangle only if small enough
 std::vector<P> rot={P{0,-.9f,.5f,1},P{.9f,0,.5f,1},P{0,.9f,.5f,1},P{0,-.9f,.5f,1},P{0,.9f,.5f,1},P{-.9f,0,.5f,1}};
 assert(buildCover(rot,0,vp,clip,c,why) && c.rects.empty() && !c.covers({0,0,100,100}) && !c.covers({10,10,90,90}));
 // single big triangle covers the whole target with margin
 std::vector<P> big={P{-2,-2,.5f,1},P{8,-2,.5f,1},P{-2,8,.5f,1}};
 assert(buildCover(big,0,vp,clip,c,why) && c.covers({0,0,100,100}));
 // strip form of a quad
 std::vector<P> strip={P{-1,-1,.5f,1},P{1,-1,.5f,1},P{-1,1,.5f,1},P{1,1,.5f,1}};
 assert(buildCover(strip,1,vp,clip,c,why) && c.rects.size()==1 && c.covers({0,0,100,100}));
 // failures: w<=0, z out of range, non-finite, guard band, wrong triangle count
 auto q=quad(-1,-1,1,1);q[2][3]=-1;assert(!buildCover(q,0,vp,clip,c,why) && why=="w<=0");
 q=quad(-1,-1,1,1);q[1][2]=2;assert(!buildCover(q,0,vp,clip,c,why) && why=="z clip");
 q=quad(-1,-1,1,1);q[1][0]=NAN;assert(!buildCover(q,0,vp,clip,c,why));
 q=quad(-100,-1,1,1);assert(!buildCover(q,0,vp,clip,c,why) && why=="guard band");
 q=quad(-1,-1,1,1);q.pop_back();assert(!buildCover(q,0,vp,clip,c,why));
 // two quads side by side are two rects and their union covers the target
 auto a=quad(-1,-1,0,1),bq=quad(0,-1,1,1);a.insert(a.end(),bq.begin(),bq.end());
 assert(buildCover(a,0,vp,clip,c,why) && c.rects.size()>=2 && c.covers({0,0,100,100}));
 // a diagonal that is not shared (overlapping triangles) is not an exact rect
 std::vector<P> bad={P{-1,-1,.5f,1},P{1,-1,.5f,1},P{-1,1,.5f,1},P{-1,-1,.5f,1},P{-1,1,.5f,1},P{1,1,.5f,1}};
 assert(buildCover(bad,0,vp,clip,c,why) && c.rects.empty());
 // FUXI_MTL_DRAW_AREA outer bounds: every pixel whose center a triangle covers (brute force, in double, perspective-correct screen positions) lies inside the box,
 // with margin 0, for random triangles with w in [0.25,4], fractional edges, flipped and offset viewports; the box is also tight (within 1 px of the hull).
 {uint64_t seed=0x2545f4914f6cdd1dull;auto rnd=[&](){seed^=seed<<13;seed^=seed>>7;seed^=seed<<17;return double(seed%1000003)/1000003.0;};
  const ViewportF vps[]={{0,100,100,-100},{0,0,100,100},{7.5,3.25,80,90},{-20,120,140,-130}};
  for(int iter=0;iter<4000;++iter){const ViewportF &v=vps[iter%4];std::vector<P> t(3);double sx[3],sy[3];
   for(int k=0;k<3;++k){double w=0.25+rnd()*3.75,nx=rnd()*2.6-1.3,ny=rnd()*2.6-1.3;t[k]={float(nx*w),float(ny*w),float(.5*w),float(w)};
    sx[k]=v.x+(double(t[k][0])/t[k][3]*0.5+0.5)*v.w;sy[k]=v.y+(double(t[k][1])/t[k][3]*0.5+0.5)*v.h;}
   IRect box;assert(outerBounds(t,v,0,box,why));
   double area=(sx[1]-sx[0])*(sy[2]-sy[0])-(sx[2]-sx[0])*(sy[1]-sy[0]);
   for(int py=-30;py<140;++py)for(int px=-30;px<140;++px){double cx=px+.5,cy=py+.5;bool in=true;
    for(int e=0;e<3 && in;++e){int a_=e,b_=(e+1)%3;double d=(sx[b_]-sx[a_])*(cy-sy[a_])-(sy[b_]-sy[a_])*(cx-sx[a_]);if(area>0?d<0:d>0)in=false;}
    if(in && std::fabs(area)>0)assert(px>=box.x0 && px<box.x1 && py>=box.y0 && py<box.y1);}
   double mnx=std::min({sx[0],sx[1],sx[2]}),mxx=std::max({sx[0],sx[1],sx[2]});assert(box.x0>=mnx-1.001 && box.x1<=mxx+1.001);}
  IRect box;std::vector<P> q=quad(-.5f,-.5f,.5f,.5f);
  assert(outerBounds(q,vp,0,box,why) && box.x0==25 && box.x1==75 && box.y0==25 && box.y1==75);
  assert(outerBounds(q,vp,16,box,why) && box.x0==9 && box.x1==91 && box.y0==9 && box.y1==91);
  q[4][3]=0;assert(!outerBounds(q,vp,0,box,why) && why=="w<=0");q[4][3]=-2;assert(!outerBounds(q,vp,0,box,why) && why=="w<=0");
  q=quad(-.5f,-.5f,.5f,.5f);q[0][1]=INFINITY;assert(!outerBounds(q,vp,0,box,why) && why=="non-finite position");
  q=quad(-.5f,-.5f,.5f,.5f);assert(!outerBounds(q,ViewportF{0,0,0,100},0,box,why) && why=="viewport");
  q.resize(2);assert(!outerBounds(q,vp,0,box,why));}
 puts("OcclusionProofTests ok");return 0;}
