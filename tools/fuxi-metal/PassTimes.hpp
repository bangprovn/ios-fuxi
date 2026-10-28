#pragma once
#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <mutex>
#include <unordered_map>
#include <vector>
// Per-render-pass GPU time (FUXI_MTL_GPU_PASS_TIMES=1). Each stamp is written at BOTTOM_OF_PIPE after the pass and
// its trailing barrier, so a duration covers everything recorded since the previous stamp: the shadow copies and
// layout barriers that precede the pass, the pass itself, and its closing barrier. Passes are grouped by shape.
namespace fuxi {
struct PassInfo {
 uint32_t width=0,height=0,colors=0,draws=0,copies=0,barriers=0;uint32_t load=0; // load: 2 bits per attachment (0 dont-care, 1 load, 2 clear)
 uint32_t store=0,areaW=0,areaH=0;uint8_t kind=0,tiling=0; // tiling: 0 optimal, 1 linear, 2 optimal render-target shadow of a linear image // store: 1 bit per attachment; kind 0 draw pass, 1 empty pass, 2 transfer/other op
};
// Per-draw GPU time inside render passes (FUXI_MTL_GPU_DRAW_TIMES=1, one submit in 30). Stamp 0 is the submit start; every entry owns the next
// stamp. A begin marker closes a pass-opening stamp (its delta holds the preceding copies/barriers and is not a draw); the report picks the pass with
// the largest summed draw time and prints its 5 most expensive draws.
struct DrawTex {uint32_t width=0,height=0,format=0;uint8_t kind=0;}; // kind 0 optimal, 1 linear alias, 2 optimal shadow of a buffer
struct DrawInfo {
 bool begin=false;uint32_t pipeline=0,count=0,blend=0,srcRGB=0,dstRGB=0,srcAlpha=0,dstAlpha=0,mask=0,instr=0,samples=0,loops=0,kills=0,texCount=0,passW=0,passH=0;DrawTex tex[4];
};
// Counts SPIR-V instructions, image samples, loop merges and discards in a fragment shader (word stream; header is 5 words).
struct SpirvStats {uint32_t words=0,instr=0,samples=0,loops=0,kills=0,fetches=0,specConsts=0,switches=0,condBranches=0,selections=0,calls=0,phis=0,converts=0,f16Types=0,f32Types=0,relaxed=0,accessChains=0,quantize=0,loopsDontUnroll=0,loopsNone=0;};
inline SpirvStats spirvStats(const uint32_t *w,size_t n){
 SpirvStats s;s.words=uint32_t(n);if(!w || n<5)return s;
 for(size_t i=5;i<n;){uint32_t op=w[i]&0xffffu,len=w[i]>>16;if(!len || i+len>n)break;++s.instr;
  if(op>=87 && op<=94)++s.samples;else if(op==95)++s.fetches;else if(op==246){++s.loops;if(len>=4){if(w[i+3]&2u)++s.loopsDontUnroll;else if(!w[i+3])++s.loopsNone;}}else if(op==116)++s.quantize;else if(op==252 || op==4416 || op==5380)++s.kills;
  else if(op>=48 && op<=52)++s.specConsts;else if(op==251)++s.switches;else if(op==250)++s.condBranches;else if(op==247)++s.selections;else if(op==57)++s.calls;else if(op==245)++s.phis;
  else if(op==115)++s.converts;else if(op==65)++s.accessChains;else if(op==22 && len>=3){if(w[i+2]==16)++s.f16Types;else if(w[i+2]==32)++s.f32Types;}else if(op==71 && len>=3 && w[i+2]==0)++s.relaxed;i+=len;}
 return s;
}
inline void reportDraws(FILE *out,const std::vector<DrawInfo> &infos,const std::vector<uint64_t> &ns,size_t top=5){
 std::vector<uint64_t> passNs;std::vector<size_t> first;int cur=-1;
 for(size_t i=0;i<infos.size() && i<ns.size();++i){if(infos[i].begin){passNs.push_back(0);first.push_back(i+1);cur=int(passNs.size())-1;}else if(cur>=0)passNs[cur]+=ns[i];}
 if(passNs.empty())return;size_t best=0;for(size_t i=1;i<passNs.size();++i)if(passNs[i]>passNs[best])best=i;
 size_t b=first[best],e=b;while(e<infos.size() && e<ns.size() && !infos[e].begin)++e;
 std::vector<size_t> order;for(size_t i=b;i<e;++i)order.push_back(i);
 std::sort(order.begin(),order.end(),[&](size_t x,size_t y){return ns[x]>ns[y];});
 const auto &h=infos[b-1];
 fprintf(out,"FUXI MTL: gpu draw times passes=%zu top_pass=%zu %ux%u draws=%zu sum_ms=%.3f\n",passNs.size(),best,h.passW,h.passH,e-b,passNs[best]/1e6);
 for(size_t k=0;k<top && k<order.size();++k){const auto &d=infos[order[k]];
  fprintf(out,"FUXI MTL: gpu draw top%zu ms=%.3f pipe=%06x verts=%u blend=%u rgb=%u/%u a=%u/%u mask=%u frag_instr=%u samples=%u loops=%u kills=%u tex=%u",k+1,ns[order[k]]/1e6,d.pipeline,d.count,d.blend,d.srcRGB,d.dstRGB,d.srcAlpha,d.dstAlpha,d.mask,d.instr,d.samples,d.loops,d.kills,d.texCount);
  for(unsigned t=0;t<d.texCount && t<4;++t)fprintf(out," [%ux%u fmt=%u %s]",d.tex[t].width,d.tex[t].height,d.tex[t].format,d.tex[t].kind==1?"linear":d.tex[t].kind==2?"shadow":"optimal");
  fprintf(out,"\n");}
}
class PassTimes {
 struct Row {PassInfo info;uint64_t count=0,totalNs=0,maxNs=0,draws=0,copies=0,barriers=0,areaPixels=0,fullPixels=0;};
 std::mutex mutex;std::unordered_map<uint64_t,Row> rows;uint64_t submits=0,passes=0,passNs=0,wholeNs=0;
 static uint64_t key(const PassInfo &p){uint64_t h=0xcbf29ce484222325ull;for(uint64_t x:{uint64_t(p.width),uint64_t(p.height),uint64_t(p.colors),uint64_t(p.load),uint64_t(p.store),uint64_t(p.kind),uint64_t(p.tiling)})h=(h^x)*0x100000001b3ull;return h;}
 static void text(const PassInfo &p,char *out,size_t size){
  char load[17]{},store[9]{};unsigned n=std::min(p.colors,8u);for(unsigned i=0;i<n;++i){unsigned v=(p.load>>(i*2))&3;load[i]=v==2?'C':v==1?'L':'D';store[i]=(p.store>>i)&1?'S':'-';}
  snprintf(out,size,"%s %ux%u colors=%u load=%s store=%s tiling=%s",p.kind==0?"pass":p.kind==1?"empty":"op",p.width,p.height,p.colors,load,store,p.tiling==1?"linear":p.tiling==2?"rt-shadow":"optimal");
 }
public:
 // stamps[0] is the start of the submit; stamps[i+1] closes infos[i]. whole is the main (top-of-pipe) elapsed time.
 void add(const std::vector<PassInfo> &infos,const std::vector<uint64_t> &deltaNs,uint64_t whole){
  std::lock_guard<std::mutex> guard(mutex);++submits;wholeNs+=whole;
  for(size_t i=0;i<infos.size() && i<deltaNs.size();++i){auto &r=rows[key(infos[i])];if(!r.count)r.info=infos[i];++r.count;r.totalNs+=deltaNs[i];r.maxNs=std::max(r.maxNs,deltaNs[i]);r.draws+=infos[i].draws;r.copies+=infos[i].copies;r.barriers+=infos[i].barriers;{uint64_t full=uint64_t(infos[i].width)*infos[i].height;r.fullPixels+=full;r.areaPixels+=infos[i].areaW?uint64_t(infos[i].areaW)*infos[i].areaH:full;}++passes;passNs+=deltaNs[i];}
 }
 uint64_t submitCount(){std::lock_guard<std::mutex> guard(mutex);return submits;}
 // Prints the summary and the top rows by total time, then starts a new window.
 void report(FILE *out,size_t top=5){
  std::lock_guard<std::mutex> guard(mutex);if(!submits)return;
  std::vector<const Row *> order;for(auto &e:rows)order.push_back(&e.second);
  std::sort(order.begin(),order.end(),[](const Row *a,const Row *b){return a->totalNs>b->totalNs;});
  double n=double(submits);
  fprintf(out,"FUXI MTL: gpu pass times submits=%llu stamps/submit=%.1f pass_sum_ms/submit=%.3f whole_top_of_pipe_ms/submit=%.3f\n",(unsigned long long)submits,passes/n,passNs/n/1e6,wholeNs/n/1e6);
  for(size_t i=0;i<top && i<order.size();++i){auto &r=*order[i];char t[112];text(r.info,t,sizeof t);
   fprintf(out,"FUXI MTL: gpu pass top%zu %s n/submit=%.2f draws=%.1f copies=%.1f barriers=%.1f render_area_pct=%.1f avg_ms=%.3f max_ms=%.3f ms/submit=%.3f share_pct=%.1f\n",i+1,t,r.count/n,double(r.draws)/r.count,double(r.copies)/r.count,double(r.barriers)/r.count,r.fullPixels?100.0*r.areaPixels/r.fullPixels:100.0,r.totalNs/double(r.count)/1e6,r.maxNs/1e6,r.totalNs/n/1e6,passNs?100.0*r.totalNs/passNs:0.0);}
  rows.clear();submits=passes=passNs=wholeNs=0;
 }
};
}
