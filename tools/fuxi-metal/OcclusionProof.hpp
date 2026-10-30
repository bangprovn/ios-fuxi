/* Geometry proofs for FUXI_MTL_OCCLUDE: integer pixel rects, blend independence, vertex attribute decoding for the CPU vertex evaluator, and the screen coverage a draw provably produces.
   Everything here is conservative: any doubt (clipping, non-finite values, fractional edges, odd topology) returns false with a reason and the caller keeps the draw. */
#pragma once
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <string>
#include <vector>
namespace fuxi {
struct IRect {
 int32_t x0=0,y0=0,x1=0,y1=0; // half-open pixel rect
 bool empty() const {return x1<=x0 || y1<=y0;}
 uint64_t area() const {return empty()?0:uint64_t(x1-x0)*uint64_t(y1-y0);}
 bool contains(const IRect &o) const {return !o.empty() && x0<=o.x0 && y0<=o.y0 && x1>=o.x1 && y1>=o.y1;}
};
inline IRect intersect(const IRect &a,const IRect &b){return {std::max(a.x0,b.x0),std::max(a.y0,b.y0),std::min(a.x1,b.x1),std::min(a.y1,b.y1)};}
// R minus the union of covers is empty. Splits the remainder into at most a few dozen rects; gives up (returns false) beyond that.
inline bool coveredByUnion(const IRect &r,const std::vector<IRect> &covers){
 if(r.empty())return false;
 std::vector<IRect> left{r},next;
 for(const auto &c:covers){
  if(c.empty())continue;next.clear();
  for(const auto &p:left){
   auto i=intersect(p,c);if(i.empty()){next.push_back(p);continue;}
   if(p.y0<i.y0)next.push_back({p.x0,p.y0,p.x1,i.y0});
   if(i.y1<p.y1)next.push_back({p.x0,i.y1,p.x1,p.y1});
   if(p.x0<i.x0)next.push_back({p.x0,i.y0,i.x0,i.y1});
   if(i.x1<p.x1)next.push_back({i.x1,i.y0,p.x1,i.y1});
  }
  left.swap(next);if(left.empty())return true;if(left.size()>64)return false;
 }
 return left.empty();
}
// Vulkan blend enums as emitted in Translation::colors[].factors: 0 ZERO 1 ONE 2 SRC_COLOR 3 ONE_MINUS_SRC_COLOR 4 DST_COLOR 5 ONE_MINUS_DST_COLOR 6 SRC_ALPHA 7 ONE_MINUS_SRC_ALPHA 8 DST_ALPHA
// 9 ONE_MINUS_DST_ALPHA 10..13 constants 14 SRC_ALPHA_SATURATE 15..18 SRC1. Ops: 0 ADD 1 SUBTRACT 2 REVERSE_SUBTRACT 3 MIN 4 MAX. factors = srcRGB,dstRGB,rgbOp,srcAlpha,dstAlpha,alphaOp.
inline bool srcFactorIndependent(uint32_t f){return f<=3 || f==6 || f==7 || (f>=10 && f<=13);}
inline bool blendDstIndependent(uint32_t blend,const uint32_t f[6]){
 if(!blend)return true;
 return f[2]<=2 && f[5]<=2 && f[1]==0 && f[4]==0 && srcFactorIndependent(f[0]) && srcFactorIndependent(f[3]);
}
// Vertex attribute decode to the raw 32-bit lanes the shader input sees. Formats are VkFormat numbers; unsupported ones return false.
struct AttrFormat {uint32_t components,bits;char kind;bool bgra;}; // kind: f float, u unorm, s snorm, U uint, S sint
inline bool attrFormat(uint32_t vk,AttrFormat &o){
 switch(vk){
  case 9:o={1,8,'u',false};return true;case 10:o={1,8,'s',false};return true;case 13:o={1,8,'U',false};return true;case 14:o={1,8,'S',false};return true;
  case 16:o={2,8,'u',false};return true;case 17:o={2,8,'s',false};return true;case 20:o={2,8,'U',false};return true;case 21:o={2,8,'S',false};return true;
  case 37:o={4,8,'u',false};return true;case 38:o={4,8,'s',false};return true;case 41:o={4,8,'U',false};return true;case 42:o={4,8,'S',false};return true;case 44:o={4,8,'u',true};return true;
  case 70:o={1,16,'u',false};return true;case 71:o={1,16,'s',false};return true;case 74:o={1,16,'U',false};return true;case 75:o={1,16,'S',false};return true;case 76:o={1,16,'f',false};return true;
  case 77:o={2,16,'u',false};return true;case 78:o={2,16,'s',false};return true;case 81:o={2,16,'U',false};return true;case 82:o={2,16,'S',false};return true;case 83:o={2,16,'f',false};return true;
  case 91:o={4,16,'u',false};return true;case 92:o={4,16,'s',false};return true;case 95:o={4,16,'U',false};return true;case 96:o={4,16,'S',false};return true;case 97:o={4,16,'f',false};return true;
  case 98:o={1,32,'U',false};return true;case 99:o={1,32,'S',false};return true;case 100:o={1,32,'f',false};return true;
  case 101:o={2,32,'U',false};return true;case 102:o={2,32,'S',false};return true;case 103:o={2,32,'f',false};return true;
  case 104:o={3,32,'U',false};return true;case 105:o={3,32,'S',false};return true;case 106:o={3,32,'f',false};return true;
  case 107:o={4,32,'U',false};return true;case 108:o={4,32,'S',false};return true;case 109:o={4,32,'f',false};return true;
  default:return false;}
}
inline float halfToFloat(uint16_t h){
 uint32_t s=(h>>15)&1,e=(h>>10)&31,m=h&1023;float v;
 if(!e)v=std::ldexp(float(m),-24);else if(e==31)v=m?NAN:INFINITY;else v=std::ldexp(float(m|1024),int(e)-25);
 return s?-v:v;
}
inline bool decodeAttribute(uint32_t vk,const uint8_t *p,size_t avail,uint32_t out[4]){
 AttrFormat f;if(!attrFormat(vk,f))return false;size_t bytes=size_t(f.components)*f.bits/8;if(avail<bytes)return false;
 bool isFloatOut=f.kind=='f'||f.kind=='u'||f.kind=='s';
 auto bits=[](float v){uint32_t u;memcpy(&u,&v,4);return u;};
 uint32_t lane[4]={0,0,0,isFloatOut?bits(1.0f):1u};
 for(uint32_t c=0;c<f.components;++c){
  const uint8_t *q=p+size_t(c)*f.bits/8;uint64_t raw=0;memcpy(&raw,q,f.bits/8);
  switch(f.kind){
   case 'f':lane[c]=f.bits==32?uint32_t(raw):bits(halfToFloat(uint16_t(raw)));break;
   case 'u':lane[c]=bits(float(double(raw)/double((uint64_t(1)<<f.bits)-1)));break;
   case 's':{int64_t v=f.bits==8?int8_t(raw):f.bits==16?int16_t(raw):int32_t(raw);double d=double(v)/double((int64_t(1)<<(f.bits-1))-1);lane[c]=bits(float(std::max(d,-1.0)));break;}
   case 'U':lane[c]=uint32_t(raw);break;
   default:lane[c]=uint32_t(f.bits==8?int8_t(raw):f.bits==16?int16_t(raw):int32_t(raw));break;
  }
 }
 if(f.bgra)std::swap(lane[0],lane[2]);
 memcpy(out,lane,sizeof(lane));return true;
}
// What one draw provably paints, as pixel rects (exact two-triangle quads, inward rounded) plus raw triangles in screen space (tested per victim with an edge margin).
struct Cover {
 IRect clip;                                 // scissor ∩ inward viewport ∩ render area ∩ target
 std::vector<IRect> rects;                   // already intersected with clip
 struct Tri {double x[3],y[3];};std::vector<Tri> tris;
 bool covers(const IRect &r) const {
  if(r.empty() || !clip.contains(r))return false;
  if(coveredByUnion(r,rects))return true;
  for(const auto &t:tris)if(triContains(t,r))return true;
  return false;
 }
 // all four corners at least 0.1 px inside every edge
 static bool triContains(const Tri &t,const IRect &r){
  double area=(t.x[1]-t.x[0])*(t.y[2]-t.y[0])-(t.x[2]-t.x[0])*(t.y[1]-t.y[0]);if(!(std::fabs(area)>1e-3))return false;double sign=area>0?1:-1;
  const double cx[4]={double(r.x0),double(r.x1),double(r.x0),double(r.x1)},cy[4]={double(r.y0),double(r.y0),double(r.y1),double(r.y1)};
  for(int e=0;e<3;++e){int a=e,b=(e+1)%3;double dx=t.x[b]-t.x[a],dy=t.y[b]-t.y[a],len=std::sqrt(dx*dx+dy*dy);if(!(len>1e-6))return false;
   for(int k=0;k<4;++k){double d=sign*(dx*(cy[k]-t.y[a])-dy*(cx[k]-t.x[a]))/len;if(!(d>=0.1))return false;}}
  return true;
 }
};
struct ViewportF {double x=0,y=0,w=0,h=0;};
// Clip-space positions (one per vertex in draw order) -> Cover. topology: 0 triangle list, 1 triangle strip.
inline bool buildCover(const std::vector<std::array<float,4>> &clipPos,int topology,const ViewportF &vp,const IRect &clipRect,Cover &out,std::string &why){
 out={};out.clip=clipRect;
 if(clipRect.empty()){why="empty clip";return false;}
 if(!(vp.w>0) || !std::isfinite(vp.x) || !std::isfinite(vp.y) || !std::isfinite(vp.w) || !std::isfinite(vp.h) || !(std::fabs(vp.h)>0)){why="viewport";return false;}
 if(clipPos.size()<3 || clipPos.size()>96){why="vertex count";return false;}
 std::vector<std::array<double,2>> s(clipPos.size());
 for(size_t i=0;i<clipPos.size();++i){auto &c=clipPos[i];
  for(int k=0;k<4;++k)if(!std::isfinite(c[k])){why="non-finite position";return false;}
  double w=c[3];if(!(w>0)){why="w<=0";return false;}
  if(c[2]<0 || c[2]>w){why="z clip";return false;}
  double nx=c[0]/w,ny=c[1]/w;if(std::fabs(nx)>16 || std::fabs(ny)>16){why="guard band";return false;}
  s[i]={vp.x+(nx*0.5+0.5)*vp.w,vp.y+(ny*0.5+0.5)*vp.h};}
 std::vector<std::array<size_t,3>> tris;
 if(topology==0){if(clipPos.size()%3){why="triangle count";return false;}for(size_t i=0;i<clipPos.size();i+=3)tris.push_back({i,i+1,i+2});}
 else if(topology==1){for(size_t i=0;i+2<clipPos.size();++i)tris.push_back(i&1?std::array<size_t,3>{i+1,i,i+2}:std::array<size_t,3>{i,i+1,i+2});}
 else {why="topology";return false;}
 for(auto &t:tris){Cover::Tri c;for(int k=0;k<3;++k){c.x[k]=s[t[k]][0];c.y[k]=s[t[k]][1];}out.tris.push_back(c);}
 // exact axis-aligned rects from triangle pairs that share a diagonal
 const double tol=0.01;
 auto corner=[&](const Cover::Tri &t,double x0,double y0,double x1,double y1,int &mask)->bool {
  mask=0;for(int k=0;k<3;++k){bool lx=std::fabs(t.x[k]-x0)<tol,hx=std::fabs(t.x[k]-x1)<tol,ly=std::fabs(t.y[k]-y0)<tol,hy=std::fabs(t.y[k]-y1)<tol;
   if(!(lx||hx) || !(ly||hy))return false;int c=(hx?1:0)|(hy?2:0);if(mask&(1<<c))return false;mask|=1<<c;}
  return true;};
 for(size_t a=0;a<out.tris.size();++a)for(size_t b=a+1;b<out.tris.size();++b){
  auto &p=out.tris[a];auto &q=out.tris[b];double x0=1e300,y0=1e300,x1=-1e300,y1=-1e300;
  for(int k=0;k<3;++k){x0=std::min({x0,p.x[k],q.x[k]});x1=std::max({x1,p.x[k],q.x[k]});y0=std::min({y0,p.y[k],q.y[k]});y1=std::max({y1,p.y[k],q.y[k]});}
  if(!(x1-x0>0.5) || !(std::fabs(y1-y0)>0.5))continue;
  int mp,mq;if(!corner(p,x0,y0,x1,y1,mp) || !corner(q,x0,y0,x1,y1,mq))continue;
  int missP=(~mp)&15,missQ=(~mq)&15;if(__builtin_popcount(missP)!=1 || __builtin_popcount(missQ)!=1 || (__builtin_ctz(missP)^__builtin_ctz(missQ))!=3 || ((mp|mq)!=15))continue; // opposite corners missing: shared diagonal
  // pixels whose centers are at least 0.25 px inside
  double fx0=std::ceil(x0-0.25),fx1=std::floor(x1-0.75)+1,fy0=std::ceil(y0-0.25),fy1=std::floor(y1-0.75)+1;
  if(!(fx0>-1e9 && fx1<1e9 && fy0>-1e9 && fy1<1e9))continue;
  IRect r=intersect({int32_t(fx0),int32_t(fy0),int32_t(fx1),int32_t(fy1)},clipRect);if(!r.empty())out.rects.push_back(r);}
 if(out.rects.empty() && out.tris.empty()){why="no geometry";return false;}
 return true;
}
// FUXI_MTL_DRAW_AREA: a pixel rect that contains every fragment a triangle draw can produce. With w>0 at every vertex the screen image of each triangle is the triangle of the
// projected vertices (z/clip-distance clipping only removes parts), so the projected bounding box, rounded outward and widened by `margin` px (GPU snapping, fp16 slop), is a superset.
// Any w<=0, non-finite value or bad viewport fails: the caller then uses the scissor. The result is NOT clipped to the scissor or target.
inline bool outerBounds(const std::vector<std::array<float,4>> &clipPos,const ViewportF &vp,int32_t margin,IRect &out,std::string &why){
 if(!std::isfinite(vp.x) || !std::isfinite(vp.y) || !std::isfinite(vp.w) || !std::isfinite(vp.h) || !(std::fabs(vp.w)>0) || !(std::fabs(vp.h)>0)){why="viewport";return false;}
 if(clipPos.size()<3){why="vertex count";return false;}
 double x0=1e300,y0=1e300,x1=-1e300,y1=-1e300;
 for(auto &c:clipPos){for(int k=0;k<4;++k)if(!std::isfinite(c[k])){why="non-finite position";return false;}
  double w=c[3];if(!(w>1e-20)){why="w<=0";return false;}
  double sx=vp.x+(c[0]/w*0.5+0.5)*vp.w,sy=vp.y+(c[1]/w*0.5+0.5)*vp.h;if(!(std::fabs(sx)<1e8 && std::fabs(sy)<1e8)){why="guard band";return false;}
  x0=std::min(x0,sx);x1=std::max(x1,sx);y0=std::min(y0,sy);y1=std::max(y1,sy);}
 double m=double(std::max<int32_t>(margin,0));
 out={int32_t(std::floor(x0)-m),int32_t(std::floor(y0)-m),int32_t(std::ceil(x1)+m),int32_t(std::ceil(y1)+m)};
 return true;
}
}
