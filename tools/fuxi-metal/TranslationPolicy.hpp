/* Original opt-in translation policy; duplicate/malformed boot keys disable. */
#pragma once
#include "ClientGate.hpp"
#include <cstdint>
#include <cstdlib>
namespace fuxi {
inline bool translationOption(const char *args,const char *key,bool *present=nullptr) {
 unsigned count=0;bool enabled=false;size_t length=strlen(key);
 if(args)for(const char *p=args;*p;){
  p+=strspn(p," \t\r\n");size_t n=strcspn(p," \t\r\n");
  if(n>=length && !strncmp(p,key,length)){++count;enabled=n==length+1 && p[length]=='1';}p+=n;
 }
 if(present)*present=count!=0;return count==1 && enabled;
}
// One decimal digit, exact token, no duplicates; unknown bits fail closed.
// bit 0: omit read-only same-layout image barriers; bit 1: merge across encoders; bit 2: boundary barriers/areas.
inline unsigned translationLevel(const char *args,const char *key,unsigned maximum) {
 unsigned count=0,value=0;size_t length=strlen(key);
 if(args)for(const char *p=args;*p;){
  p+=strspn(p," \t\r\n");size_t n=strcspn(p," \t\r\n");
  if(n>=length && !strncmp(p,key,length)){++count;value=n==length+1 && p[length]>='0' && p[length]<=char('0'+maximum)?unsigned(p[length]-'0'):0;}p+=n;
 }
 return count==1?value:0;
}
inline unsigned translationPerf(const char *args){return translationLevel(args,"fuxigpuperf=",7);}
inline unsigned translationFrameTrace(const char *args){return translationLevel(args,"fuxigpuframetrace=",2);}
struct TranslationPolicy {bool stages=false,trace=false,async=false,compileBoost=false,frameTrace=false,pipelined=false,skipSampling=false,allowFeedback=false,overlap=false,submitFast=false,gmem=false,merge=false;unsigned perf=0;bool frameCPU=false,noSlab=false,shadowRename=false,shadowHash=true,passTimes=false,renderArea=true,reorder=true,drawTimes=false,linearShadow=false,pipeStats=false,f16=false,robust=false,rtShadow=false,rtReuse=false,rtRename=false,fragRelaxed=false,sampleShadow=false,occlude=false,orderFetchOnly=false,nofetchRp=false;unsigned sampleMinPx=65536,occludeMinPx=4096;bool drawArea=false;unsigned drawAreaMinPx=1u<<20,drawAreaMargin=16,drawAreaBudgetUs=1000;bool earlyRelease=false;unsigned spirvUnroll=0,halfRound=0,halfPipeCount=0;bool halfPipesSet=false;uint32_t halfPipes[16]{};bool submitLean=false,splitSubmit=false;unsigned submitLeanLevel=0,splitSubmitUs=400,splitSubmitMax=3;bool bufferCached=false,fetchSplit=false;unsigned passTimesEvery=1;};
inline TranslationPolicy translationPolicy(const char *args) {
 return {translationOption(args,"fuxigpustages="),translationOption(args,"fuxigputrace="),translationOption(args,"fuxigpuasync="),translationOption(args,"fuxicompileboost="),translationFrameTrace(args)!=0,translationOption(args,"fuxigpupipelined="),translationOption(args,"fuxigpuskip="),translationOption(args,"fuxigpufeedback="),translationOption(args,"fuxigpuoverlap="),translationOption(args,"fuxigpusubmit="),translationOption(args,"fuxigpugmem="),translationOption(args,"fuxigpumerge="),translationPerf(args),translationFrameTrace(args)==2};
}
inline bool earlyScheduledWait(const TranslationPolicy &p,bool scanout,bool quiet,const char *process) {return p.overlap && p.submitFast && scanout && quiet && process && !strcmp(process,"backboardd");}
inline TranslationPolicy systemTranslationPolicy() {
 static const auto policy=[] {
  char args[4096]={};
#ifdef FUXI_MTL_HOST_TEST
  const char *test=getenv("FUXI_MTL_TEST_BOOTARGS");if(test)snprintf(args,sizeof(args),"%s",test);
#else
  systemString("kern.bootargs",args,sizeof(args));
#endif
  bool stages=false,trace=false,async=false;
  TranslationPolicy p{translationOption(args,"fuxigpustages=",&stages),translationOption(args,"fuxigputrace=",&trace),translationOption(args,"fuxigpuasync=",&async),translationOption(args,"fuxicompileboost="),translationFrameTrace(args)!=0,translationOption(args,"fuxigpupipelined="),translationOption(args,"fuxigpuskip="),translationOption(args,"fuxigpufeedback="),translationOption(args,"fuxigpuoverlap="),translationOption(args,"fuxigpusubmit="),translationOption(args,"fuxigpugmem="),translationOption(args,"fuxigpumerge="),translationPerf(args),translationFrameTrace(args)==2};
  p.noSlab=translationOption(args,"fuxigpunoslab=");bool renameKey=false;p.shadowRename=translationOption(args,"fuxigpurename=",&renameKey);
  // Explicit boot keys, including =0/invalid/duplicates, take precedence.
  if(!stages){const char *v=getenv("FUXI_MTL_STAGE_CACHE");p.stages=v && !strcmp(v,"1");}
  if(!trace){const char *v=getenv("FUXI_MTL_TRANSLATION_TRACE");p.trace=v && !strcmp(v,"1");}
  if(!async){const char *v=getenv("FUXI_MTL_ASYNC_PIPELINES");p.async=v && !strcmp(v,"1");}
  bool renameEnv=false;if(!renameKey){const char *v=getenv("FUXI_MTL_SHADOW_RENAME");p.shadowRename=renameEnv=v && !strcmp(v,"1");}
  {const char *v=getenv("FUXI_MTL_SHADOW_HASH");if(v && !strcmp(v,"0"))p.shadowHash=false;v=getenv("FUXI_MTL_GPU_PASS_TIMES");p.passTimes=v && !strcmp(v,"1");
   v=getenv("FUXI_MTL_RENDER_AREA");if(v && !strcmp(v,"0"))p.renderArea=false;v=getenv("FUXI_MTL_PASS_REORDER");if(v && !strcmp(v,"0"))p.reorder=false;v=getenv("FUXI_MTL_GPU_DRAW_TIMES");p.drawTimes=v && !strcmp(v,"1");v=getenv("FUXI_MTL_LINEAR_SHADOW");p.linearShadow=v && !strcmp(v,"1");v=getenv("FUXI_MTL_PIPELINE_STATS");p.pipeStats=v && !strcmp(v,"1");v=getenv("FUXI_MTL_F16");p.f16=v && !strcmp(v,"1");v=getenv("FUXI_MTL_ROBUST");p.robust=v && !strcmp(v,"1");v=getenv("FUXI_MTL_RT_SHADOW");p.rtShadow=v && !strcmp(v,"1");v=getenv("FUXI_MTL_SAMPLE_SHADOW");if(v && !strcmp(v,"1")){p.sampleShadow=true;p.linearShadow=true;}v=getenv("FUXI_MTL_SAMPLE_SHADOW_MIN_PX");if(v){long m=atol(v);if(m>=1 && m<=(1<<26))p.sampleMinPx=unsigned(m);}v=getenv("FUXI_MTL_FRAG_RELAXED");p.fragRelaxed=v && !strcmp(v,"1");v=getenv("FUXI_MTL_OCCLUDE");p.occlude=v && !strcmp(v,"1");v=getenv("FUXI_MTL_ORDER_FETCH_ONLY");p.orderFetchOnly=v && !strcmp(v,"1");v=getenv("FUXI_MTL_NOFETCH_RP");p.nofetchRp=v && !strcmp(v,"1");v=getenv("FUXI_MTL_OCCLUDE_MIN_PX");if(v){long m=atol(v);if(m>=1 && m<=(1<<26))p.occludeMinPx=unsigned(m);}v=getenv("FUXI_MTL_RT_SHADOW_REUSE");p.rtReuse=v && !strcmp(v,"1");v=getenv("FUXI_MTL_RT_RENAME");p.rtRename=v && !strcmp(v,"1");
   fprintf(stderr,"FUXI MTL: policy shadow_hash=%d gpu_pass_times=%d render_area=%d pass_reorder=%d gpu_draw_times=%d linear_shadow=%d pipeline_stats=%d f16=%d robust=%d rt_shadow=%d rt_shadow_reuse=%d rt_rename=%d frag_relaxed=%d sample_shadow=%d sample_min_px=%u occlude=%d occlude_min_px=%u order_fetch_only=%d nofetch_rp=%d\n",int(p.shadowHash),int(p.passTimes),int(p.renderArea),int(p.reorder),int(p.drawTimes),int(p.linearShadow),int(p.pipeStats),int(p.f16),int(p.robust),int(p.rtShadow),int(p.rtReuse),int(p.rtRename),int(p.fragRelaxed),int(p.sampleShadow),p.sampleMinPx,int(p.occlude),p.occludeMinPx,int(p.orderFetchOnly),int(p.nofetchRp));}
  {auto num=[](const char *key,unsigned lo,unsigned hi,unsigned &out){const char *v=getenv(key);if(v){long m=atol(v);if(m>=long(lo) && m<=long(hi))out=unsigned(m);}};
   const char *v=getenv("FUXI_MTL_DRAW_AREA");p.drawArea=v && !strcmp(v,"1");num("FUXI_MTL_DRAW_AREA_MIN_PX",1,1u<<26,p.drawAreaMinPx);num("FUXI_MTL_DRAW_AREA_MARGIN",0,256,p.drawAreaMargin);num("FUXI_MTL_DRAW_AREA_BUDGET_US",0,100000,p.drawAreaBudgetUs);
   fprintf(stderr,"FUXI MTL: policy draw_area=%d draw_area_min_px=%u draw_area_margin=%u draw_area_budget_us=%u\n",int(p.drawArea),p.drawAreaMinPx,p.drawAreaMargin,p.drawAreaBudgetUs);}
  {const char *v=getenv("FUXI_MTL_EARLY_RELEASE");p.earlyRelease=v && !strcmp(v,"1");fprintf(stderr,"FUXI MTL: policy early_release=%d\n",int(p.earlyRelease));}
  {const char *v=getenv("FUXI_MTL_HALF_ROUND");p.halfRound=v && v[0]>='1' && v[0]<='3' && !v[1]?unsigned(v[0]-'0'):0; // FUXI_MTL_HALF_ROUND_PIPES: comma-separated fh= hashes that may take a lossy mode (unset: all)
   v=getenv("FUXI_MTL_HALF_ROUND_PIPES");char list[16*9+1]="";p.halfPipesSet=v && *v;
   if(p.halfPipesSet)for(const char *q=v;*q && p.halfPipeCount<16;){char *end=nullptr;unsigned long h=strtoul(q,&end,16);if(end==q)break;p.halfPipes[p.halfPipeCount++]=uint32_t(h);q=end;while(*q==',' || *q==' ')++q;}
   for(unsigned i=0;i<p.halfPipeCount;++i)snprintf(list+strlen(list),sizeof list-strlen(list),"%s%08x",i?",":"",p.halfPipes[i]);
   fprintf(stderr,"FUXI MTL: policy half_round=%u half_round_pipes=%s%s\n",p.halfRound,p.halfPipesSet?"":"all",list);}
  // FUXI_MTL_SUBMIT_LEAN=1: same recorded commands, less submit CPU (per-submit stamps instead of hash-set dedupe of planned resources,
  // earlier-writer scans only when an earlier op writes a buffer, hoist reason strings only on submits that log them, one bulk read per index range,
  // rt-shadow and pass reorders applied in place). =2 also drops the CPU index scan of indexed draws when the device enforces robustBufferAccess.
  {const char *v=getenv("FUXI_MTL_SUBMIT_LEAN");p.submitLeanLevel=v && (!strcmp(v,"1") || !strcmp(v,"2"))?unsigned(v[0]-'0'):0;p.submitLean=p.submitLeanLevel!=0;fprintf(stderr,"FUXI MTL: policy submit_lean=%u\n",p.submitLeanLevel);}
  // FUXI_MTL_SPLIT_SUBMIT=1: a submit with 2+ render passes is recorded into chunks; a chunk is queued at a pass end once its recording took
  // FUXI_MTL_SPLIT_SUBMIT_US (0..100000, default 400, 0 = every pass end), at most FUXI_MTL_SPLIT_SUBMIT_MAX (2..8, default 3: FuxiAdreno queue mode admits
  // 4 jobs in flight) chunks; only the last chunk signals the fence.
  {const char *v=getenv("FUXI_MTL_SPLIT_SUBMIT");p.splitSubmit=v && !strcmp(v,"1");v=getenv("FUXI_MTL_SPLIT_SUBMIT_US");if(v){char *e=nullptr;long m=strtol(v,&e,10);if(e!=v && !*e && m>=0 && m<=100000)p.splitSubmitUs=unsigned(m);}
   v=getenv("FUXI_MTL_SPLIT_SUBMIT_MAX");if(v){char *e=nullptr;long m=strtol(v,&e,10);if(e!=v && !*e && m>=2 && m<=8)p.splitSubmitMax=unsigned(m);}
   fprintf(stderr,"FUXI MTL: policy split_submit=%d split_submit_us=%u split_submit_max=%u\n",int(p.splitSubmit),p.splitSubmitUs,p.splitSubmitMax);}
  // FUXI_MTL_FETCH_SPLIT=1: inside a pass with framebuffer-fetch draws only fetch pipelines carry the rasterization-order blend flag (Turnip: per-overlap
  // flush); a fetch draw that follows a non-fetch draw of the same pass first gets a by-region COLOR_ATTACHMENT_WRITE -> INPUT_ATTACHMENT_READ self-dependency barrier.
  {const char *v=getenv("FUXI_MTL_FETCH_SPLIT");p.fetchSplit=v && !strcmp(v,"1");fprintf(stderr,"FUXI MTL: policy fetch_split=%d\n",int(p.fetchSplit));}
  // FUXI_MTL_GPU_PASS_TIMES_EVERY=N (1..100000, 0 = 1): with FUXI_MTL_GPU_PASS_TIMES=1 only 1 submit in N records the per-pass BOTTOM_OF_PIPE stamps (each is a WFI in Turnip);
  // the 'gpu pass' lines average over the sampled submits. The whole-submit gpu_exec stamps stay on every submit.
  {const char *v=getenv("FUXI_MTL_GPU_PASS_TIMES_EVERY");if(v){char *e=nullptr;long m=strtol(v,&e,10);if(e!=v && !*e && m>=0 && m<=100000)p.passTimesEvery=m?unsigned(m):1u;}fprintf(stderr,"FUXI MTL: policy gpu_pass_times_every=%u\n",p.passTimesEvery);}
  // FUXI_MTL_BUFFER_CACHED=1: MTLBuffer memory prefers a HOST_VISIBLE|HOST_COHERENT|HOST_CACHED type (logged with the type list at the first buffer).
  {const char *v=getenv("FUXI_MTL_BUFFER_CACHED");p.bufferCached=v && !strcmp(v,"1");}
  {const char *v=getenv("FUXI_MTL_SPIRV_UNROLL");p.spirvUnroll=v && (!strcmp(v,"1") || !strcmp(v,"2"))?unsigned(v[0]-'0'):0;fprintf(stderr,"FUXI MTL: policy spirv_unroll=%u\n",p.spirvUnroll);}
  fprintf(stderr,"FUXI MTL: policy shadow_rename=%d source=%s\n",int(p.shadowRename),renameKey?"boot":renameEnv?"env":"none");
  return p;
 }();return policy;
}
// Lossy FUXI_MTL_HALF_ROUND modes (2, 3) apply to every pipeline, or with FUXI_MTL_HALF_ROUND_PIPES only to the listed fragment hashes (others get mode 1; a malformed
// entry ends the list, so it never widens lossy to every pipeline).
inline bool halfRoundLossy(const TranslationPolicy &p,uint32_t fragHash){if(!p.halfPipesSet)return true;for(unsigned i=0;i<p.halfPipeCount;++i)if(p.halfPipes[i]==fragHash)return true;return false;}
}
