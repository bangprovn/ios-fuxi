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
 uint32_t store=0,areaW=0,areaH=0;uint8_t kind=0; // store: 1 bit per attachment; kind 0 draw pass, 1 empty pass, 2 transfer/other op
};
class PassTimes {
 struct Row {PassInfo info;uint64_t count=0,totalNs=0,maxNs=0,draws=0,copies=0,barriers=0,areaPixels=0,fullPixels=0;};
 std::mutex mutex;std::unordered_map<uint64_t,Row> rows;uint64_t submits=0,passes=0,passNs=0,wholeNs=0;
 static uint64_t key(const PassInfo &p){uint64_t h=0xcbf29ce484222325ull;for(uint64_t x:{uint64_t(p.width),uint64_t(p.height),uint64_t(p.colors),uint64_t(p.load),uint64_t(p.store),uint64_t(p.kind)})h=(h^x)*0x100000001b3ull;return h;}
 static void text(const PassInfo &p,char *out,size_t size){
  char load[17]{},store[9]{};unsigned n=std::min(p.colors,8u);for(unsigned i=0;i<n;++i){unsigned v=(p.load>>(i*2))&3;load[i]=v==2?'C':v==1?'L':'D';store[i]=(p.store>>i)&1?'S':'-';}
  snprintf(out,size,"%s %ux%u colors=%u load=%s store=%s",p.kind==0?"pass":p.kind==1?"empty":"op",p.width,p.height,p.colors,load,store);
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
  for(size_t i=0;i<top && i<order.size();++i){auto &r=*order[i];char t[96];text(r.info,t,sizeof t);
   fprintf(out,"FUXI MTL: gpu pass top%zu %s n/submit=%.2f draws=%.1f copies=%.1f barriers=%.1f render_area_pct=%.1f avg_ms=%.3f max_ms=%.3f ms/submit=%.3f share_pct=%.1f\n",i+1,t,r.count/n,double(r.draws)/r.count,double(r.copies)/r.count,double(r.barriers)/r.count,r.fullPixels?100.0*r.areaPixels/r.fullPixels:100.0,r.totalNs/double(r.count)/1e6,r.maxNs/1e6,r.totalNs/n/1e6,passNs?100.0*r.totalNs/passNs:0.0);}
  rows.clear();submits=passes=passNs=wholeNs=0;
 }
};
}
