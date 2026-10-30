/* Original host controls for bounded frame statistics and thread attribution. */
#define FUXI_MTL_HOST_TEST
#include "FrameTrace.hpp"
#include "PassTimes.hpp"
#include <cassert>
#include <cstring>
#include <thread>
int main(){
 using namespace fuxi;
 assert(frameCPUFromRegister(0xabcdef000ULL|6)==6);
 assert(frameCPUFromRegister(0xabc000ULL|0xfff)==4095);
 assert(frameCPU()==4096); // host has no Darwin register convention
 FrameTraceWindow window;assert(window.percentile(&FrameSample::wall,50)==0);
 for(unsigned i=1;i<=100;++i){FrameSample s;s.frontendDraw=i*100;s.leaseFront=i*1000;s.leaseBack=i*500;s.leaseExclWait=i*100;s.leaseLockWrite=i*200;s.leaseLockRead=i*60;s.leaseWrites=1;s.leaseReads=3;s.frontendDrawSamples=10;s.prepareConflictWaits=1;s.prepareUnknownDrains=1;s.backendCPU=i*10000;s.frontendCPU=i*20000;s.frontendWall=i*30000;s.pipe=i*40000;s.desc=i*50000;s.record=i*60000;s.kernelIO=i*70000;s.pipeCPU=i*8000;s.schedWait=i*3000;s.commitCall=i*1000;s.commitGap=i*2000;s.shadowScannedIndices=780;for(size_t j=0;j<s.shadowFullReasons.size();++j)s.shadowFullReasons[j]=j+1;s.textureShadowBytes=2048;s.textureRepitchedBytes=1024;s.textureShadowUses={25,2,1};s.snapshotBytes=33280;s.shadowCopyBytes=33280;s.planHits=119;s.planMisses=1;s.queueSubmits=2;s.imageBarriers=7;s.readBarriersSkipped=3;s.crossEncoderMerges=1;s.drainWaits=1;s.drainSubmits=2;s.prepareDrains=1;s.indexGPUWritableDrains=1;s.indexUploadSubmits=1;s.indexReadOnlySubmits=2;s.indexGPUWritableSubmits=1;s.shadowWaits=1;s.snapshotReusedBytes=8192;s.shadowSkippedBytes=4096;s.wall=i*1000000ULL;s.completionLatency=s.wall;s.inflight=i%2+1;s.cpu=i%7;s.draws=2;s.passes=1;s.mergedDraws=1;s.mergeSplits[size_t(MergeSplit::encoder)]=2;s.counts={1,2,3};window.add(s);}
 assert(window.size()==100 && window.percentile(&FrameSample::wall,50)==50000000 && window.percentile(&FrameSample::wall,90)==90000000);
 FILE *out=tmpfile();assert(out);window.report(out);rewind(out);char line[8192];assert(fgets(line,sizeof(line),out));
 assert(strstr(line,"frame trace n=100 samples=100 wall=50.000/90.000/100.000 ms"));
 assert(strstr(line,"snapshot_bytes/frame=33280.00 shadow_copy_bytes/frame=33280.00"));
 assert(strstr(line,"shadow_scanned_indices/frame=780.00 shadow_full_reasons=indexed:1.00,whole_window:2.00,use_resource:3.00,blit:4.00,bounds:5.00,other:6.00"));
 assert(strstr(line,"texture_shadow_bytes/frame=2048.00 texture_repitched_bytes/frame=1024.00 texture_shadow_uses=sampled:25.00,target:2.00,other:1.00"));
 assert(strstr(line,"drain_submits=200 drain_reasons=indices-upload:0,indices-gpu-writable:100,prepare:100,prepare-conflict:100,prepare-unknown:100 index_submits=upload:100,readonly:200,gpu_writable:100 shadow_waits=100 snapshot_reused_bytes/frame=8192.00 shadow_skipped_bytes/frame=4096.00"));
 assert(strstr(line,"plan_hits/frame=119.00 plan_misses/frame=1.00"));
 assert(strstr(line," lease_front=0.050/0.090 lease_back=0.025/0.045 lease_excl_wait=0.005/0.009 lease_lock_write=0.010/0.018 lease_lock_read=0.003/0.005 lease_surfaces/frame=write:1.00,read:3.00"));
 assert(strstr(line,"draws/frame=2.00 render_passes/frame=1.00 pipelines/frame=1.00 descriptors/frame=2.00 allocations/frame=3.00"));
 if(systemTranslationPolicy().overlap)assert(strstr(line,"overlap_depth=1/2 completion_ms=50.000/90.000"));else assert(!strstr(line,"overlap_depth="));
 if(systemTranslationPolicy().merge)assert(strstr(line,"merged_draws/frame=1.00 merged_splits=encoder:200,attachments:0"));else assert(!strstr(line,"merged_draws/frame="));
 assert(strstr(line,"submits/frame=2.00 image_barriers/frame=7.00 read_barriers_skipped/frame=3.00 cross_encoder_merges/frame=1.00 wait_reasons=drain:100,conflict:0,slots:0"));
 assert(strstr(line,"sched_wait=0.150/0.270"));
 assert(strstr(line,"commit_call=0.050/0.090 commit_gap=0.100/0.180"));
 assert(strstr(line,"frontend_draw_ns/frame=5000/9000 frontend_draw_samples/frame=10.00"));
 assert(strstr(line,"prior_wait_completed=0.000/0.000 backend_cpu=0.500/0.900 frontend_cpu=1.000/1.800 frontend_wall=1.500/2.700 frontend_draw_ns/frame=5000/9000 frontend_draw_samples/frame=10.00 pipe=2.000/3.600 desc=2.500/4.500 record=3.000/5.400 kernel_io=3.500/6.300"));
 assert(strstr(line,"pipe_cpu=0.400/0.720 desc_cpu=0.000/0.000 record_cpu=0.000/0.000 flush_cpu=0.000/0.000 kernel_io_cpu=0.000/0.000 queue_submit_cpu=0.000/0.000 cpu_sampled_submits/frame=0.00 cpu_omitted_submits/frame=0.00\n"));
 assert(!window.size());fclose(out);
 // Overflow retains the newest 1024 samples; window max includes evicted data.
 FrameSample s;s.wall=9999999999;window.add(s);
 for(unsigned i=0;i<2048;++i){s.wall=1000000;window.add(s);}
 assert(window.size()==2049 && window.percentile(&FrameSample::wall,90)==1000000);
 out=tmpfile();assert(out);window.report(out);rewind(out);assert(fgets(line,sizeof(line),out));fclose(out);
 assert(strstr(line,"snapshot_bytes/frame=0.00 shadow_copy_bytes/frame=0.00"));assert(strstr(line,"texture_shadow_bytes/frame=0.00 texture_repitched_bytes/frame=0.00 texture_shadow_uses=sampled:0.00,target:0.00,other:0.00"));
 assert(strstr(line,"shadow_scanned_indices/frame=0.00 shadow_full_reasons=indexed:0.00,whole_window:0.00,use_resource:0.00,blit:0.00,bounds:0.00,other:0.00"));
 assert(strstr(line,"plan_hits/frame=0.00 plan_misses/frame=0.00"));
 assert(strstr(line,"drain_submits=0 drain_reasons=indices-upload:0,indices-gpu-writable:0,prepare:0,prepare-conflict:0,prepare-unknown:0 index_submits=upload:0,readonly:0,gpu_writable:0 shadow_waits=0 snapshot_reused_bytes/frame=0.00 shadow_skipped_bytes/frame=0.00"));
 assert(strstr(line,"n=2049 samples=1024 wall=1.000/1.000/10000.000"));
 assert(strstr(line,"submits/frame=0.00 image_barriers/frame=0.00"));
 if(systemTranslationPolicy().merge)assert(strstr(line,"merged_draws/frame=0.00 merged_splits=encoder:0"));
 FrameSample first,second;assert(!currentFrame);
 {FrameTraceScope a(&first);assert(currentFrame==&first);
  {FrameTraceScope b(&second);assert(currentFrame==&second);}assert(currentFrame==&first);
  std::thread worker([]{assert(!currentFrame && frameCounts.allocations==0);frameCounts.allocations=3;});worker.join();
  assert(!frameCounts.allocations);
 }assert(!currentFrame);
 // Real host thread CPU distinguishes a sleep from work on this same thread.
 auto wallStart=FrameClock::now();auto cpuStart=frameThreadCPU();assert(cpuStart!=UINT64_MAX);
 std::this_thread::sleep_for(std::chrono::milliseconds(40));
 auto sleptCPU=frameCPUDelta(cpuStart,frameThreadCPU());assert(sleptCPU<frameNanos(wallStart)/2);
 cpuStart=frameThreadCPU();while(frameCPUDelta(cpuStart,frameThreadCPU())<2000000){}
 assert(frameCPUDelta(cpuStart,frameThreadCPU())>=2000000);
 { // exclusive nested phases, drained encode phases, bounded phase lines
  FrameSample s;auto wall=FrameClock::now();
  {FrameTraceScope scope(&s);PhaseTimer outer(SubPhase::other);std::this_thread::sleep_for(std::chrono::milliseconds(4));{PhaseTimer inner(SubPhase::cmdBegin);std::this_thread::sleep_for(std::chrono::milliseconds(2));}scanAdd(ScanKind::viewProbes,7);scanMax(ScanKind::boundSetMax,5);scanMax(ScanKind::boundSetMax,3);}
  assert(s.subCalls[size_t(SubPhase::cmdBegin)]==1 && s.subNs[size_t(SubPhase::cmdBegin)]>=2000000);
  assert(s.subCalls[size_t(SubPhase::other)]==1 && s.subNs[size_t(SubPhase::other)]>=4000000 && s.subNs[size_t(SubPhase::other)]+s.subNs[size_t(SubPhase::cmdBegin)]<=frameNanos(wall)); // inclusive nesting would exceed elapsed time
  assert(s.scans[size_t(ScanKind::viewProbes)]==7 && s.scans[size_t(ScanKind::boundSetMax)]==5);
  {PhaseTimer none(SubPhase::passes);} // no current frame: no accumulation, no crash
  if(systemTranslationPolicy().frameTrace){
   wall=FrameClock::now();{PhaseTimer a(EncPhase::draw);std::this_thread::sleep_for(std::chrono::milliseconds(3));{PhaseTimer b(EncPhase::state);std::this_thread::sleep_for(std::chrono::milliseconds(2));}}
   drainEncodePhases(s);assert(s.encCalls[size_t(EncPhase::draw)]==1 && s.encNs[size_t(EncPhase::draw)]>=3000000 && s.encNs[size_t(EncPhase::draw)]+s.encNs[size_t(EncPhase::state)]<=frameNanos(wall) && s.encNs[size_t(EncPhase::state)]>=2000000);
   assert(encAccum.calls[size_t(EncPhase::draw)]==0);
  }
  FrameTraceWindow w;for(unsigned i=0;i<4;++i)w.add(s);
  FILE *o=tmpfile();assert(o);w.report(o);rewind(o);char text[8192];bool sawSubmit=false,sawScan=false;
  while(fgets(text,sizeof(text),o)){if(strstr(text,"submit phases")){sawSubmit=true;assert(strlen(text)<=401 && strstr(text,"cmd_begin=") && strstr(text,"other="));}if(strstr(text,"scan counts")){sawScan=true;assert(strlen(text)<=401 && strstr(text,"view_probes=7.0") && strstr(text,"bound_set_max=5.0"));}}
  assert(sawSubmit && sawScan);fclose(o);
 }
 { // per-pass GPU times: grouping by shape, ordering by total time, window reset
  PassTimes times;PassInfo big{};big.width=1080;big.height=2400;big.colors=1;big.draws=40;big.load=1;big.store=1;PassInfo small{};small.width=64;small.height=16;small.colors=1;small.draws=2;small.copies=1;small.load=2;small.store=1;PassInfo copy{};copy.kind=2;
  for(int i=0;i<3;++i)times.add({small,big,small,copy},{100000,2000000,150000,50000},3000000);
  assert(times.submitCount()==3);char buffer[4096]{};FILE *o=fmemopen(buffer,sizeof buffer,"w");times.report(o);fclose(o);
  assert(strstr(buffer,"submits=3 stamps/submit=4.0") && strstr(buffer,"pass_sum_ms/submit=2.300") && strstr(buffer,"whole_top_of_pipe_ms/submit=3.000"));
  const char *first=strstr(buffer,"top1 ");assert(first && strstr(first,"pass 1080x2400 colors=1 load=L store=S") && strstr(first,"draws=40.0") && strstr(first,"avg_ms=2.000"));
  const char *second=strstr(buffer,"top2 ");assert(second && strstr(second,"pass 64x16 colors=1 load=C store=S") && strstr(second,"n/submit=2.00") && strstr(second,"copies=1.0") && strstr(second,"avg_ms=0.125"));
  assert(strstr(buffer,"top3 op 0x0") && !strstr(buffer,"top4 ") && times.submitCount()==0);
  assert(!strstr(buffer,"draw area per submit")); // only with FUXI_MTL_DRAW_AREA classes present
  PassInfo shr=big;shr.areaClass=2;shr.areaW=1080;shr.areaH=600;PassInfo kept=big;kept.areaClass=1;kept.areaW=1080;kept.areaH=2400;
  for(int i=0;i<2;++i)times.add({shr,kept,small},{500000,2000000,100000},3000000);
  char b2[4096]{};o=fmemopen(b2,sizeof b2,"w");times.report(o);fclose(o);
  const char *da=strstr(b2,"FUXI MTL: gpu pass draw area per submit: shrunk passes=1.00 ms=0.500 avg_ms=0.500 area_pct=25.0 | eligible not shrunk passes=1.00 ms=2.000 avg_ms=2.000 area_pct=100.0 | not eligible passes=1.00 ms=0.100");
  assert(da);
  times.add({big},{1000},1000);char b3[4096]{};o=fmemopen(b3,sizeof b3,"w");times.report(o);fclose(o);assert(!strstr(b3,"draw area per submit")); // window reset
 }
 { // per-draw GPU times: the pass with the largest draw sum wins, draws ranked, begin markers skipped; SPIR-V statistics
  std::vector<DrawInfo> infos;std::vector<uint64_t> ns;
  auto begin=[&](uint32_t w,uint32_t h,uint64_t t){DrawInfo m{};m.begin=true;m.passW=w;m.passH=h;infos.push_back(m);ns.push_back(t);};
  auto draw=[&](uint32_t pipe,uint64_t t,uint8_t kind){DrawInfo d{};d.pipeline=pipe;d.count=6;d.blend=1;d.texCount=1;d.tex[0]={1125,2436,80,kind};d.instr=12;infos.push_back(d);ns.push_back(t);};
  begin(64,16,900000);draw(1,50000,0);begin(1125,2436,400000);for(uint32_t i=0;i<7;++i)draw(0x100+i,100000+i*10000,i==3?1:0);
  char buffer[4096]{};FILE *o=fmemopen(buffer,sizeof buffer,"w");reportDraws(o,infos,ns);fclose(o);
  assert(strstr(buffer,"passes=2 top_pass=1 1125x2436 draws=7") && strstr(buffer,"top1 ms=0.160 pipe=000106") && strstr(buffer,"top5 ms=0.120") && !strstr(buffer,"top6 "));
  assert(strstr(buffer,"[1125x2436 fmt=80 linear]") && strstr(buffer,"frag_instr=12") && !strstr(buffer,"pipe=000001"));
  uint32_t code[18]={0x07230203,0x10000,0,10,0};code[5]=(4u<<16)|21;code[9]=(5u<<16)|87;code[14]=(3u<<16)|246;code[17]=(1u<<16)|252;
  auto st=spirvStats(code,sizeof code/4);assert(st.instr==4 && st.samples==1 && st.loops==1 && st.kills==1);
  {std::vector<DrawInfo> in;std::vector<uint64_t> t;
  auto begin=[&](){DrawInfo m{};m.begin=true;m.passW=1000;m.passH=2000;in.push_back(m);t.push_back(0);};
  auto opq=[&](int x,int y,uint32_t w,uint32_t h,uint64_t ns,bool blend=false){DrawInfo d{};d.count=6;d.blend=blend;d.mask=15;d.scX=x;d.scY=y;d.scW=w;d.scH=h;in.push_back(d);t.push_back(ns);};
  begin();opq(0,0,1000,2000,500,false);opq(100,100,300,300,200,false);opq(0,0,1000,2000,50,false);opq(100,100,10,10,7,true); // pass 1: the first two are hidden by the third; the blended one is not opaque
  begin();opq(0,0,500,500,40,false);opq(500,0,500,500,60,false); // pass 2: disjoint, none hidden, first draw does not cover the pass
  begin();opq(0,0,1000,2000,30,false);{DrawInfo k{};k.count=6;k.mask=15;k.kills=1;k.scW=1000;k.scH=2000;in.push_back(k);t.push_back(9);} // pass 3: a discarding draw never occludes
  auto o=drawOcclusion(in,t);assert(o.passes==3 && o.draws==8 && o.candidates==2 && o.candidateNs==700 && o.totalNs==896 && o.loadElide==2);
  auto m=drawOcclusion({},{});assert(!m.draws && !m.candidates);}
 std::vector<DrawInfo> none;std::vector<uint64_t> zero;char quiet[64]{};FILE *q=fmemopen(quiet,sizeof quiet,"w");reportDraws(q,none,zero);fclose(q);assert(!quiet[0]);
 }
 puts("PASS: bounded frame percentiles/reset/overflow/max, log fields, CPU mask and thread-local attribution");
}
