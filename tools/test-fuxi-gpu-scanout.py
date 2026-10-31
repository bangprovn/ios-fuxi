#!/usr/bin/env python3
"""Offline production-code scanout models; no device or reference execution.

Checks display ownership, the CPU pixel oracle, real surface-cache reservation
and framebuffer integration. MMIO/clock/cache/DMA completion remain models.
"""
import argparse
import shlex
import importlib.util
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


PREFIX = r'''
static unsigned fg_abi_inline; static int fg_inl_usable(void) { return 0; }
#include <assert.h>
#include <stdint.h>
#include <stdlib.h>
#include <stdio.h>
#include <stdarg.h>
#include <string.h>
static char doubleStats[512],ssppLog[512],dpuLog[16384];
static int scanout_printf(const char *format,...) {
 va_list args;va_start(args,format);
 if(strstr(format,"double=1 flips="))vsnprintf(doubleStats,sizeof(doubleStats),format,args);
 if(strstr(format,"sspp vig0"))vsnprintf(ssppLog,sizeof(ssppLog),format,args);
 if(strstr(format,"dpu smmu") || strstr(format,"dpu stage") || strstr(format,"dpu direct")){size_t n=strlen(dpuLog);if(n<sizeof(dpuLog))vsnprintf(dpuLog+n,sizeof(dpuLog)-n,format,args);}
 va_end(args);return 0;
}
#define printf scanout_printf
#define FG_GUARD
#define FG_TRY_GUARD(fail) if(tryBusy){fail;}
static unsigned tryBusy;
static uint32_t abiFenceDone=~0u;static unsigned fencePolls;   // writer fence model: passed when fence <= abiFenceDone
static int fuxi_gpu_abi_fence_passed(uint32_t fence) {fencePolls++;return !fence || (int32_t)(abiFenceDone-fence)>=0;}
typedef int boolean_t;
#define FALSE 0
static int interruptsOn=1;
static boolean_t ml_set_interrupts_enabled(boolean_t value) {int old=interruptsOn;interruptsOn=value;return old;}
static unsigned fg_scanout_requested, fg_scanout_panel120_requested, fg_scanout_double_requested, fg_scanout_triple_requested, fg_busy, fg_client_busy, fg_abi_present_wait;
static unsigned ready=1,abiLive=1,abiResult,observations,reads,writes,starts,failAddress;
static uint64_t ticks=1000,machScale=1;
static uint64_t mach_absolute_time(void) {return ticks*machScale;}
static void absolutetime_to_nanoseconds(uint64_t t,uint64_t *ns) {*ns=t*1000/machScale;}
static void nanoseconds_to_absolutetime(uint64_t ns,uint64_t *t) {*t=ns*machScale/1000;}
static uint32_t ctl[1024],intf[1024],dsi[1024],sspp[1024];
static struct {unsigned pa,value;} trace[8192];
static unsigned traceCount,dsiReads,dmaStarts,pendingDma,noDone,busyAfterDone,staleDone,frozenClock,failWriteAddress;
static unsigned errorOnDone,tearReadbackMismatch;
static volatile uint32_t *fuxi_ctl0=ctl,*fuxi_intf1=intf;
static int fuxi_gpu_ready(void) {return ready;}
// apps_smmu model: fault offset, map/unmap balance; writes only through a read/write (map 2) mapping and only inside the one bank page
// smmuBank (asserted), so an SMR/S2CR/CBAR or any global (GR0 TLBIALLNSNH/TLBGSYNC) write fails the test. Bank semantics: FSR fault
// bits are write-one-to-clear and FORMAT (10:9) read only; RESUME, TLBIALL and TLBSYNC are write only (counted, not stored).
#define FG_DPU_SMMU_SIZE 0x100000u
static uint32_t smmu[0x100000/4];
static unsigned smmuMapped,smmuMaps,smmuFailMap,smmuFault=~0u,smmuReads,smmuWrites,smmuRwMaps,smmuBank=~0u,smmuIgnore,smmuTlbi,smmuResume,smmuSepRaz;
static int fg_dpu_smmu_map(unsigned map) {if(!map){smmuMapped=0;return 1;}smmuMaps++;if(smmuFailMap)return 0;smmuMapped=map;smmuRwMaps+=map==2;return 1;}
static int fg_dpu_smmu_read(unsigned off,unsigned *v) {
 assert(smmuMapped);if(off>=sizeof(smmu) || (off&3) || off==smmuFault)return 0;smmuReads++;*v=smmu[off/4];return 1;
}
static int fg_dpu_smmu_write(unsigned off,unsigned v) {
 assert(smmuMapped==2 && smmuBank!=~0u && off>=smmuBank && off<smmuBank+0x1000 && !(off&3));smmuWrites++;
 unsigned r=off-smmuBank;
 if(r==0x618 || r==0x7f0){smmuTlbi+=r==0x618;return 1;}
 if(r==0x8){smmuResume++;return 1;}
 if(r==0x58){smmu[off/4]&=~(v&0xc00001feu);return 1;}
 if(smmuIgnore && r!=0)return 1;
 if(r==0x10 && smmuSepRaz)v&=~(7u<<15);   // a bank without TCR2.SEP
 smmu[off/4]=v;return 1;
}
// Stage-2 memory model: allocations get fake physical addresses; the table pool is contiguous, the surface scattered (16 pages below
// 4 GiB, the rest above, every other page, descending).
struct dpuAlloc {uint8_t *host;unsigned bytes,contiguous;};
static struct dpuAlloc dpuAllocs[4];
static unsigned dpuAllocCount,dpuFrees,dpuAllocFail,dpuPageFail;
static int fg_dpu_mem(unsigned bytes,unsigned contiguous,uintptr_t *va) {
 if(dpuAllocFail && contiguous+1==dpuAllocFail)return 0;
 assert(dpuAllocCount<4);uint8_t *m=aligned_alloc(4096,(bytes+4095)&~4095u);assert(m);memset(m,0,bytes);
 dpuAllocs[dpuAllocCount++]=(struct dpuAlloc){m,bytes,contiguous};*va=(uintptr_t)m;return 1;
}
static void fg_dpu_mem_free(uintptr_t va,unsigned bytes) {
 if(!va)return;
 for(unsigned i=0;i<dpuAllocCount;i++)if(dpuAllocs[i].host==(uint8_t *)va && dpuAllocs[i].bytes==bytes){free(dpuAllocs[i].host);dpuAllocs[i].host=0;dpuFrees++;return;}
 assert(0);
}
static uint64_t dpuSurfacePa(unsigned i,unsigned n) {return i<16?0xa0000000ull+i*4096ull:0x900000000ull+(uint64_t)(n-1-i)*8192ull;}
static uint64_t fg_dpu_page_pa(uintptr_t va) {
 for(unsigned i=0;i<dpuAllocCount;i++){struct dpuAlloc *a=&dpuAllocs[i];
  if(!a->host || va<(uintptr_t)a->host || va>=(uintptr_t)a->host+a->bytes)continue;
  unsigned page=(unsigned)((va-(uintptr_t)a->host)/4096);
  if(dpuPageFail && !a->contiguous && page==dpuPageFail)return 0;
  return a->contiguous?0x880000000ull+(va-(uintptr_t)a->host):dpuSurfacePa(page,(a->bytes+4095)/4096)+(va&4095);}
 return 0;
}
static unsigned dpuCleans;
static void fg_dpu_clean(uintptr_t va,unsigned bytes) {(void)va;(void)bytes;dpuCleans++;}
static void (*startHook)(void);
static unsigned modelDrain,modelTe,modelPanel120,modelDouble,modelTriple,parseDouble,parseTriple,modelNc,modelEarly;
static uint8_t modelB[4320u*2400],modelC[4320u*2400];
static unsigned failBMap,failCMap;
static uintptr_t fuxi_gpu_nc_pointer(uint64_t pa,uint64_t size) {
 assert((pa==0xb8a00000u || pa==0xb9400000u) && size==4320u*2400);
 return pa==0xb9400000u?(failCMap?0:(uintptr_t)modelC):(failBMap?0:(uintptr_t)modelB);
}
static unsigned fg_scanout_owned_for_model(void);
static unsigned fg_scanout_double_for_model(void);
static int PE_parse_boot_argn(const char *name,void *value,int size) {
 if(!strcmp(name,"fuxiscandouble"))parseDouble++;if(!strcmp(name,"fuxiscantriple"))parseTriple++;
 unsigned v=!strcmp(name,"fuxiscante")?modelTe:!strcmp(name,"fuxiscandrain")?modelDrain:
     !strcmp(name,"fuxipanel120")?modelPanel120:!strcmp(name,"fuxiscandouble")?modelDouble:!strcmp(name,"fuxiscantriple")?modelTriple:!strcmp(name,"fuxigpuncimport")?modelNc:!strcmp(name,"fuxiearly")?modelEarly:0;
 if(!v || size<4)return 0;*(unsigned *)value=v;return 1;
}
static int fg_abi_live_for_scanout(void) {return abiLive;}
static int fg_abi_observe(void) {observations++;if(!abiResult)fg_client_busy=0;return abiResult;}
static uint64_t model_counter(const char *r) {return !strcmp(r,"cntfrq_el0")?1000000:ticks;}
#define rsr(r) model_counter(r)
static void (*probeScript)(void);
static int dsiTrigAnytime;
static void delay(unsigned us) {if(!frozenClock)ticks+=us;if(probeScript)probeScript();}
typedef void *thread_call_param_t;
typedef struct {void (*fn)(thread_call_param_t,thread_call_param_t);} thread_call_data_t;
enum {THREAD_CALL_PRIORITY_KERNEL=1,THREAD_CALL_OPTIONS_ONCE=1};
static unsigned probeKicks;
static void thread_call_setup_with_options(thread_call_data_t *c,void (*fn)(thread_call_param_t,thread_call_param_t),void *arg,unsigned pri,unsigned opt) {(void)arg;(void)pri;(void)opt;c->fn=fn;}
static int thread_call_enter(thread_call_data_t *c) {(void)c;probeKicks++;return 0;}
static unsigned spinKicks;static uint64_t spinDeadline;
static int thread_call_enter_delayed(thread_call_data_t *c,uint64_t d) {(void)c;spinKicks++;spinDeadline=d;return 0;}
static unsigned fg_access(uint64_t pa,uint64_t *value,unsigned wr,unsigned wide) {
 assert(!wide);
 if(pa==failAddress || (wr && pa==failWriteAddress))return 1;
 uint32_t *reg;
 if(pa>=0xae05000 && pa<0xae06000)reg=&sspp[(pa-0xae05000)/4];
 else if(pa>=0xae16000 && pa<0xae17000)reg=&ctl[(pa-0xae16000)/4];
 else if(pa>=0xae94000 && pa<0xae95000)reg=&dsi[(pa-0xae94000)/4];
 else {assert(pa>=0xae36000 && pa<0xae37000);reg=&intf[(pa-0xae36000)/4];}
 if(wr){
  writes++;assert(traceCount<8192);trace[traceCount].pa=pa;trace[traceCount++].value=*value;
  if(pa>=0xae94000 && pa<0xae95000 && !(dsiTrigAnytime && pa==0xae94084)) {
   assert(interruptsOn);
   assert(fg_scanout_owned_for_model());
   assert(!(intf[0x2b4/4]&0x80000000u) && !ctl[0x1c/4] && !fg_busy && !starts);
   assert(!intf[0x2b0/4] || intf[0x2b0/4]>=2400);
  }
  if(pa==0xae36288 || pa==0xae3628c)assert(!interruptsOn);
  if(pa==0xae94110) { // Odd bits enable interrupts; even bits are W1C status.
   uint32_t clear=staleDone?((uint32_t)*value&~1u):(uint32_t)*value;
   *reg=(*reg&0x55555555u&~clear)|((uint32_t)*value&0xaaaaaaaau);
  } else *reg=(uint32_t)*value;
  if(pa==0xae94090){assert(*value==1 && dsi[0x15c/4]==0x30006 && dsi[0x04c/4]==52);
   dmaStarts++;pendingDma=2;dsi[0x008/4]=3;}
  if(pa==0xae1601c){
   if(fg_scanout_double_for_model()) {
    assert(!intf[0x2b0/4] || intf[0x2b0/4]>=2400);
    assert(traceCount>=3 && trace[traceCount-2].pa==0xae16018 && trace[traceCount-3].pa==0xae05014);
    assert(ctl[0x18/4]&1);
   }
   assert(!fg_busy && !(intf[0x2b4/4]&0x80000000u));starts++;if(startHook)startHook();}
 } else {
  reads++;
  if(pa>=0xae94000 && pa<0xae95000)dsiReads++;
  if(pa==0xae94110 && pendingDma && !noDone && !--pendingDma){
   dsi[0x110/4]|=1;dsi[0x008/4]=busyAfterDone?4:0;
   if(errorOnDone)dsi[0x110/4]|=1u<<24;
  }
  *value=*reg;
  if(tearReadbackMismatch && pa==0xae36288 && dmaStarts)*value^=1;
 }
 return 0;
}
'''
TEST = r'''
static unsigned fg_scanout_owned_for_model(void) {return fg_scanout_owned;}
static unsigned fg_scanout_double_for_model(void) {return fg_scanout_double_active;}
static void reset(void) {
 memset(sspp,0,sizeof(sspp));sspp[0x14/4]=0xb8000000u;modelNc=fg_scanout_preclean=fg_scanout_dpu_dump=0;ssppLog[0]=dpuLog[0]=0;smmuMapped=smmuMaps=smmuFailMap=smmuReads=0;smmuFault=~0u;
 smmuWrites=smmuRwMaps=smmuIgnore=smmuTlbi=smmuResume=smmuSepRaz=0;smmuBank=~0u;startHook=0;probeScript=0;fg_scanout_dpu_stage2=0;
 fg_scanout_dpu_stage3=fg_dd_on=fg_dd_dead=fg_dd_geo=fg_dd_lost=fg_dd_logged=fg_dd_offers=fg_dd_ever=fg_dd_busy_mask=fg_dd_fetch_seen=0;
 fg_dd_next=fg_dd_fetch=~0u;fg_dd_rfence=fg_dd_qfence=0;fg_dd_stamp=fg_dd_fw=fg_dd_fw_sum=fg_dd_fw_max=fg_dd_fw_n=0;tryBusy=0;abiFenceDone=~0u;fencePolls=0;
 memset(fg_dd_key,0,sizeof(fg_dd_key));memset(fg_dd_used,0,sizeof(fg_dd_used));memset(fg_dd_va,0,sizeof(fg_dd_va));memset(fg_dd_st,0,sizeof(fg_dd_st));memset(fg_dd_why,0,sizeof(fg_dd_why));
 for(unsigned i=0;i<dpuAllocCount;i++)free(dpuAllocs[i].host);
 dpuAllocCount=dpuFrees=dpuAllocFail=dpuPageFail=dpuCleans=0;
 fg_scanout_double_requested=fg_scanout_double_active=fg_scanout_double_initialized=fg_scanout_front=0;
 parseDouble=parseTriple=0;
 modelEarly=fg_scanout_fifo=fg_scanout_fifo_q=fg_scanout_fifo_buf=0;memset(fg_scanout_fifo_st,0,sizeof(fg_scanout_fifo_st));
 fg_scanout_triple_requested=modelTriple=failCMap=fg_scanout_ready=fg_scanout_copy=0;
 memset(fg_scanout_buffer_flips,0,sizeof(fg_scanout_buffer_flips));
 fg_scanout_back_ready=fg_scanout_flips=fg_scanout_ready_drops=fg_scanout_retries=0;
 fg_scanout_current_match=fg_scanout_current_mismatch=modelDouble=failBMap=0;
 memset(ctl,0,sizeof(ctl));memset(intf,0,sizeof(intf));memset(dsi,0,sizeof(dsi));intf[0x2b4/4]=0x80000001u;
 fg_scanout_requested=fg_scanout_owned=fg_scanout_failed=fg_scanout_writable=0;
 fg_scanout_reserved=fg_busy=fg_client_busy=0;
 fg_scanout_checked=fg_scanout_checks=0;
 fg_scanout_te_mode=fg_scanout_wait_te=0;fg_scanout_drain_us=FG_SCAN_DRAIN_US;modelTe=modelDrain=0;
 fg_scanout_panel120_requested=fg_scanout_panel120_attempted=fg_scanout_panel120_active=0;
 fg_scanout_period_us=16667;fg_scanout_explicit_drain=modelPanel120=0;
 traceCount=dsiReads=dmaStarts=pendingDma=noDone=busyAfterDone=staleDone=frozenClock=failWriteAddress=0;
 interruptsOn=1;
 errorOnDone=tearReadbackMismatch=0;
 fg_scanout_last_start=0;fg_scanout_starts=0;memset(fg_scanout_iv,0,sizeof(fg_scanout_iv));
 ticks=1000;machScale=1;ready=abiLive=1;abiResult=observations=reads=writes=starts=failAddress=0;
}
static void panel120_setup(void) {
 modelPanel120=1;
 intf[0x284/4]=1;intf[0x288/4]=0x180085;intf[0x28c/4]=0xfff0;intf[0x294/4]=2400;
 intf[0x2b0/4]=2400;
 dsi[0x004/4]=0x1f5;dsi[0x03c/4]=0xc60000ab;dsi[0x084/4]=0x80011066;
 dsi[0x110/4]=0x100203; // stale DMA done, BTA done, DMA+MDP interrupt enables
 dsi[0x04c/4]=0x54;
}
static void panel120_drain(uint8_t *panel) {
 fuxi_gpu_scanout_configure((uintptr_t)panel,1,0);
 assert(!fuxi_gpu_scanout_acquire() && !dsiReads && !dmaStarts);
 ticks+=20000;
}
static uint64_t base;
static void probe_script(void) {uint64_t t=ticks-base;
 if(t>=1000 && !(intf[0x298/4]>>16))intf[0x298/4]+=1u<<16; // TE edge
 if(t>=1500 && t<8000)intf[0x2b0/4]=300; // active
 if(t>=8000){ctl[0x1c/4]=0;intf[0x2b0/4]=2400;}}
// TE-referenced spin model: the transfer starts at the edge phE (count phC -> phC+1), the line is active for 8000 us, the next edge is 8333 us later.
static void ctr_script(void) {intf[0x298/4]++;}
static void mid_script(void) {if(ticks-base>=50)intf[0x2b0/4]=300;}
static uint64_t phE,phSkew,phLine;static unsigned phC,phHold;
static void phase_script(void) {
 uint64_t t=ticks;unsigned c=phC+(t>=phE)+(t>=phE+8333);
 intf[0x298/4]=(c<<16)|7;ctl[0x1c/4]=0;intf[0x2b0/4]=(t>=phE+phLine && (phHold || t<phE+phLine+8000))?300:2400;}
static void phase_run(uint64_t e,unsigned c,unsigned hold,unsigned wakeOff) {
 phE=e+phSkew;phC=c;phHold=hold;ticks=e+wakeOff;fg_scanout_ph_e=e;fg_scanout_spin_sync=0;fg_scanout_spin_serial=fg_scanout_starts;
 fg_scanout_spin_c0=c;fg_scanout_ph_wake=ticks;fg_scanout_spin_busy=1;probeScript=phase_script;phase_script();
 fg_scanout_spin_run(0,0);probeScript=0;}
// Direct-scanout stage 2 (fuxigpuncimport bits 22+23): MDSS fetch model at each START (latch on flush, walk the bank's stage-1 table
// when SCTLR.M is set, check every fetched row's physical address and pixels, record a translation fault in FSR).
#define DPU_CB (0x80000u+3*0x1000u)
static uint32_t smmuSnap[0x100000/4],ssppSnap[1024];
static const uint64_t *dpuSlotPages[4];static unsigned dpuSlotFetches[4],dpuLastSrc,dpuTriple;
static unsigned dpuFetches,dpuTranslated,dpuWindowFetches,dpuBadFetch,dpuPixelChecks,dpuFaultAt,dpuFaultSs,dpuStuck,holdCalls,holdFault;
static uint64_t *dpuTable(uint64_t pa) {
 for(unsigned i=0;i<dpuAllocCount;i++){struct dpuAlloc *a=&dpuAllocs[i];
  if(a->host && a->contiguous && pa>=0x880000000ull && pa+4096<=0x880000000ull+a->bytes && !(pa&4095))return (uint64_t *)(a->host+(pa-0x880000000ull));}
 return 0;
}
// SMMUv2 AArch64 stage 1 (CBA2R.VA64), 4 KiB granule, as the X229 fault (far 0xb8000100, FSR TF, FSYNR0 0x630020 = PLVL 0, PNU) showed
// it: the 32-bit display address is sign-extended at the TCR2.SEP bit (0 -> bit 31; 7 UPSTREAM -> none), so with SEP 0 an address with
// bit 31 set falls in the TTBR1 half, which TCR.EPD1 disables: translation fault at level 0. Otherwise the walk starts at the level
// T0SZ gives (T0SZ 32: level 1, 2 index bits), table addresses beyond TCR2.PASize fault, and block/page leaves need AF.
// Returns 1 with *pa, or 0 with the fault level in *lvl.
static int dpu_walk_at(uint32_t va,uint64_t *pa,unsigned *lvl) {
 static const unsigned sepbit[8]={31,35,39,41,43,47,0,0},paw[8]={32,36,40,42,44,48,48,48};
 uint64_t ttbr=((uint64_t)smmu[(DPU_CB+0x24)/4]<<32)|smmu[(DPU_CB+0x20)/4];
 unsigned tcr=smmu[(DPU_CB+0x30)/4],tcr2=smmu[(DPU_CB+0x10)/4],sep=(tcr2>>15)&7,t0sz=tcr&63,bits=64-t0sz,levels=(bits-12+8)/9,level=4-levels;
 *lvl=0;
 if(sep<6 && ((uint64_t)va>>sepbit[sep]&1))return 0;        // upper half: TTBR1, EPD1 set
 if(t0sz<16 || t0sz>39 || (bits<64 && ((uint64_t)va>>bits)) || (tcr&0xc000) || smmu[(DPU_CB+0x38)/4]!=0x44)return 0;
 uint64_t limit=1ull<<paw[tcr2&7],a=ttbr&0xffffffff000ull;
 for(unsigned first=level;level<=3;level++){
  unsigned sh=12+9*(3-level),mask=level==first?(1u<<(bits-12-9*(levels-1)))-1:511;
  uint64_t *t=a<limit?dpuTable(a):0;
  *lvl=level;
  if(!t)return 0;
  uint64_t d=t[((uint64_t)va>>sh)&mask];
  if((d&3)==1 && level<3 && level>0){if(!(d&(1u<<10)))return 0;uint64_t m=(1ull<<sh)-1;*pa=(d&0xfffffffff000ull&~m)|(va&m);return 1;}
  if((d&3)!=3)return 0;
  if(level==3){if(!(d&(1u<<10)))return 0;*pa=(d&0xfffffffff000ull)|(va&4095);return 1;}
  a=d&0xfffffffff000ull;
 }
 return 0;
}
static unsigned dpuWalkLevel,dpuLeafLevel;
static int dpu_walk(uint32_t va,uint64_t *pa) {unsigned l;int ok=dpu_walk_at(va,pa,&l);if(ok)dpuLeafLevel=l>dpuLeafLevel?l:dpuLeafLevel;else dpuWalkLevel=l;return ok;}
static uint8_t *dpuSurfaceHost(uint64_t pa) {
 for(unsigned i=0;i<dpuAllocCount;i++){struct dpuAlloc *a=&dpuAllocs[i];unsigned n=(a->bytes+4095)/4096;
  if(!a->host || a->contiguous)continue;
  for(unsigned p=0;p<n;p++)if((pa&~4095ull)==dpuSurfacePa(p,n))return a->host+p*4096+(pa&4095);}
 return 0;
}
static void dpu_fetch(void) {
 if(ctl[0x18/4]&1){sspp[0xa4/4]=sspp[0x14/4];ctl[0x18/4]&=~1u;}
 dpuFetches++;
 if(dpuStuck && dpuFetches==dpuStuck){ctl[0x1c/4]=1;intf[0x2b0/4]=700;return;}
 ctl[0x1c/4]=0;intf[0x2b0/4]=0;
 uint32_t src=sspp[0xa4/4],xy=sspp[0x08/4],stride=sspp[0x24/4]&0xffff;
 unsigned sx=xy&0xffff,sy=xy>>16,on=smmu[DPU_CB/4]&1,fault=dpuFaultAt==dpuFetches;
 if(on)dpuTranslated++;
 unsigned slot=src>=0x10000000u && src<0x14000000u?(src-0x10000000u)>>24:9;const uint64_t *sp=slot<4?dpuSlotPages[slot]:0;
 if(slot<4){if(!on)dpuBadFetch++;else dpuWindowFetches++;}
 dpuLastSrc=src;
 if(sp){dpuSlotFetches[slot]++;if(sspp[0x04/4]!=0x9840465u || xy!=0x120016u || stride!=4608 || (src&0xffffff))dpuBadFetch++;}  // bit 24: slot geometry
 else if(slot>=4 && dpuTriple && (stride!=4320 || xy || sspp[0x04/4]))dpuBadFetch++;                                      // A geometry back
 for(unsigned y=0;y<2400 && !fault;y++)for(unsigned k=0;k<3 && !fault;k++){
  unsigned x=k==0?0:k==1?540:1079;uint32_t va=src+(sy+y)*stride+(sx+x)*4;uint64_t pa=va;
  if(on && !dpu_walk(va,&pa)){fault=1;smmu[(DPU_CB+0x60)/4]=va;smmu[(DPU_CB+0x68)/4]=dpuWalkLevel|0x20u;break;}
  if(sp){unsigned off=va-src;if(pa!=sp[off/4096]+(off&4095))dpuBadFetch++;else dpuPixelChecks++;}
  else if(src==0x10000000u){
   unsigned off=va-src;uint8_t *h=dpuSurfaceHost(pa);
   if(pa!=dpuSurfacePa(off/4096,2741)+(off&4095) || !h || *(uint32_t *)h!=fg_dpu_pattern(sx+x,sy+y))dpuBadFetch++;else dpuPixelChecks++;
  } else if(pa!=va || (src!=0xb8000000u && !(dpuTriple && (src==0xb8a00000u || src==0xb9400000u))))dpuBadFetch++;
 }
 if(fault && dpuFaultAt==dpuFetches){smmu[(DPU_CB+0x60)/4]=src+0x100;smmu[(DPU_CB+0x68)/4]=0x20u;}
 if(fault)smmu[(DPU_CB+0x58)/4]|=dpuFaultSs?0x40000002u:2u;
}
// X229 replay, checked on the first translated START with the table as installed: TCR2 0x61 (SEP 0, what X229 ran) faults
// 0xb8000100 at level 0; TCR2 0x38061 (SEP UPSTREAM, what stage 2 now writes) maps it identically through the level-1 block, and the
// empty window faults at level 3.
static unsigned dpuX229;
static void dpu_fetch_x229(void) {
 if(smmu[DPU_CB/4]&1 && !dpuX229++){
  uint64_t pa=0;unsigned l=9,tcr2=smmu[(DPU_CB+0x10)/4];
  assert(tcr2==0x38061u);
  smmu[(DPU_CB+0x10)/4]=0x61u;assert(!dpu_walk_at(0xb8000100u,&pa,&l) && l==0);
  assert(!dpu_walk_at(0x10000000u,&pa,&l) && l==3);           // bit 31 clear: TTBR0 even with SEP 0; window leaves still empty
  smmu[(DPU_CB+0x10)/4]=tcr2;
  assert(dpu_walk_at(0xb8000100u,&pa,&l) && pa==0xb8000100u && l==1);
  assert(dpu_walk_at(0xffffff00u,&pa,&l) && pa==0xffffff00u && l==1);
  assert(!dpu_walk_at(0x10000000u,&pa,&l) && l==3);
  assert(!dpu_walk_at(0x40000000u,&pa,&l) && l==1);           // level 1 [1] is empty
 }
 dpu_fetch();
}
// Clears the window's level-3 entry of the flip's first fetched row (offset 18*4608+22*4 = 0x14458, page 20) when the flip START comes.
static void dpu_fetch_hole(void) {
 if(ctl[0x18/4]&1 && sspp[0x14/4]==0x10000000u){uint64_t *t=dpuTable(0x880000000ull+4096);assert(t);t[20]=0;}
 dpu_fetch();
}
static void hold_fault(void) {if(sspp[0xa4/4]==0x10000000u && ++holdCalls==holdFault)smmu[(DPU_CB+0x58)/4]|=2u;}
static void dpu_setup(uint8_t *panel,unsigned bits) {
 reset();modelNc=327168|bits;memset(smmu,0,sizeof(smmu));modelDouble=modelTriple=dpuTriple;
 memset(dpuSlotPages,0,sizeof(dpuSlotPages));memset(dpuSlotFetches,0,sizeof(dpuSlotFetches));
 smmu[0]=0x2d0406u;smmu[0x20/4]=0x4c017e69u;smmu[0x24/4]=0x60000046u;smmu[0x28/4]=0x5111u; // S224: 105 SMRs, 70 banks, 4K pages, OAS 36
 smmu[(0x800+4*3)/4]=0x80021c00u;smmu[(0xc00+4*3)/4]=3;      // S224 display handoff <0x1c00 0x2> -> bank 3
 smmu[(0x800+4*4)/4]=0x80000400u;smmu[(0xc00+4*4)/4]=5;      // another master on bank 5 (stands for touch/WLAN/UFS): never touched
 smmu[(0x1000+4*3)/4]=0x1f000u;smmu[(0x1800+4*3)/4]=1;smmu[(0x1000+4*5)/4]=0x10000u;smmu[(0x1800+4*5)/4]=1;
 smmu[DPU_CB/4]=0xe0;smmu[(DPU_CB+0x10)/4]=0x60;smmu[(DPU_CB+0x58)/4]=0x400;
 smmu[(0x80000+5*0x1000)/4]=0x61;smmu[(0x80000+5*0x1000+0x20)/4]=0x12345000u;
 sspp[0]=sspp[0x0c/4]=0x09600438u;sspp[0x24/4]=4320;sspp[0x30/4]=0x237ff;sspp[0x34/4]=0x3020001;sspp[0xa4/4]=0xb8000000u;
 smmuBank=DPU_CB;startHook=dpu_fetch;
 dpuFetches=dpuTranslated=dpuWindowFetches=dpuBadFetch=dpuPixelChecks=dpuFaultAt=dpuFaultSs=dpuStuck=holdCalls=holdFault=0;
 dpuX229=dpuWalkLevel=dpuLeafLevel=0;
 fuxi_gpu_scanout_configure((uintptr_t)panel,1,0);
 memcpy(smmuSnap,smmu,sizeof(smmu));
 assert(!fuxi_gpu_scanout_acquire() && !smmuWrites); // takeover: AR off, bit-22 survey (read only)
 memcpy(ssppSnap,sspp,sizeof(sspp));ticks+=20000;
}
static void dpu_restored(void) {
 for(unsigned r=0x60;r<=0x68;r+=4)smmu[(DPU_CB+r)/4]=smmuSnap[(DPU_CB+r)/4]; // FAR/FSYNR0 keep the last fault
 assert(!memcmp(smmu,smmuSnap,sizeof(smmu)));                 // byte-exact bank, and nothing else in apps_smmu changed
 assert(!memcmp(sspp,ssppSnap,sizeof(sspp)));                 // A geometry, source and CURRENT_SRC0
 assert(!dpuBadFetch && !(ctl[0x18/4]&1) && !ctl[0x1c/4]);
 assert(dpuFrees==dpuAllocCount && !smmuMapped);
}
static void dpu_stage2_tests(uint8_t *panel) {
 // Success: identity STARTs, translated flip of the scattered surface, hold, restore; every bank write in bank 3, three bank TLBIALLs.
 dpu_setup(panel,0xc00000u);startHook=dpu_fetch_x229;
 assert(fg_scanout_dpu_stage2 && strstr(dpuLog,"dpu stage2 requested"));
 assert(!fuxi_gpu_scanout_acquire());
 assert(strstr(dpuLog,"dpu stage2 begin img=0 xy=0 ystride0=0x10e0 src0=0xb8000000 cur=0xb8000000 flush=0 start=0 line=0\n"));
 assert(strstr(dpuLog,"dpu stage2 bank cb=3 smr3 s2cr=0x3 cbar=0x1f000 cba2r=0x1 saved sctlr=0xe0 tcr2=0x60 ttbr0=0 tcr=0 mair0=0 fsr=0x400 oas_bits=36\n"));
 assert(strstr(dpuLog,"dpu stage2 memory tables=0x880000000 surface 1125x2436 stride=4608 pages=2741 above4g=2725 runs=2726 first=0xa0000000 last=0x900000000\n"));
 assert(strstr(dpuLog,"dpu stage2 enabled sctlr=0xe0->0x61 ttbr0=0x880000000 tcr=0x800020 tcr2=0x38061 mair0=0x44 tlbi=bank\n"));
 assert(strstr(dpuLog,"dpu stage2 live sctlr=0x61 ttbr0=0x880000000 tcr=0x800020 tcr2=0x38061 sep=7 pasize=1 mair0=0x44 fsr=0x400 l1[0]=0x880000003 l1[2]=0x80000441 l1[3]=0xc0000441\n"));
 assert(strstr(dpuLog,"dpu stage2 identity starts=60 fsr=0x400 cur=0xb8000000 xfer_us avg=18667 max=18667\n"));
 assert(strstr(dpuLog,"dpu stage2 mapped iova=0x10000000 pages=2741 window=0x2000000 tlbi=bank\n"));
 assert(strstr(dpuLog,"dpu stage2 flip src0=0x10000000 img=0x9840465 xy=0x120016 ystride0=0x1200 xfer_us=18667 fsr=0x400 cur=0x10000000 latched=1\n"));
 assert(strstr(dpuLog,"dpu stage2 hold_ms=2000 fsr=0x400\n"));
 assert(strstr(dpuLog,"dpu stage2 restored A img=0 xy=0 ystride0=0x10e0 src0=0xb8000000"));
 assert(strstr(dpuLog,"dpu stage2 bank restored exact=1 sctlr=0xe0 tcr2=0x60 ttbr0=0 tcr=0 mair0=0 fsr=0x400\n"));
 assert(strstr(dpuLog,"dpu stage2 done result=ok step=restore identity_starts=60 flip_latched=1 hold_ms=2000 exact=1\n"));
 assert(!strstr(dpuLog,"fault step") && !strstr(dpuLog,"memory kept"));
 assert(starts==62 && dpuTranslated==62 && dpuWindowFetches==1 && dpuPixelChecks==3*2400 && smmuTlbi==3 && !smmuResume && smmuRwMaps==1);
  assert(dpuX229==62 && dpuLeafLevel==3);                      // identity through level-1 blocks, the window through level-3 pages
 assert(dpuAllocCount==2 && dpuCleans==3 && !fg_scanout_dpu_stage2);
 dpu_restored();
 // The normal gates resume: one floor wait from the last START, then the ordinary writable/publish path; stage 2 never repeats.
 unsigned w=smmuWrites;assert(!fuxi_gpu_scanout_acquire());ticks+=20000;
 assert(fuxi_gpu_scanout_acquire()==1 && fuxi_gpu_scanout_publish()==1 && starts==63 && smmuWrites==w && !fg_scanout_failed);
 // Bits clear: bit 22 alone is the read-only survey; bit 23 alone is ignored; neither maps apps_smmu. No write, START or allocation.
 for(unsigned c=0;c<3;c++){
  dpu_setup(panel,c==0?0x400000u:c==1?0x800000u:0);
  assert(!fg_scanout_dpu_stage2 && fuxi_gpu_scanout_acquire()==1 && !smmuWrites && !smmuRwMaps && !starts && !dpuAllocCount);
  assert(!strstr(dpuLog,"stage2 begin") && (c==1?strstr(dpuLog,"dpu stage2 ignored: bit 23 needs bit 22")!=0:!strstr(dpuLog,"stage2")));
  assert(c?!smmuMaps:smmuMaps==1);
 }
 // Faults are undone: identity START 6 (with a stalled transaction: RESUME), the translated flip, and the hold.
 for(unsigned c=0;c<3;c++){
  dpu_setup(panel,0xc00000u);
  if(c==0){dpuFaultAt=6;dpuFaultSs=1;}else if(c==1)dpuFaultAt=61;else{holdFault=5;probeScript=hold_fault;}
  assert(!fuxi_gpu_scanout_acquire());probeScript=0;
  assert(strstr(dpuLog,c==0?"fault step=identity reason=fault starts=5 fsr=0x40000402":c==1?"fault step=flip reason=fault starts=60 fsr=0x402":
      "fault step=hold reason=fault starts=60 fsr=0x402"));
  assert(strstr(dpuLog,"bank restored exact=1") && strstr(dpuLog,"result=fault"));
  assert(strstr(dpuLog,c==0?"undone step=identity start=1":c==1?"undone step=flip start=1":"undone step=hold start=1"));
  assert(strstr(dpuLog,c==0?"walk far=0xb8000100 plvl=0 wnr=0 pnu=1 ind=0 ptwf=0 afr=0 fsynr0_hi=0 tcr2=0x38061 sep=7 upstream bit=0 sign=0 l0[0]=0x880000003 l1[2]=0x80000441 l2[448]=0 l3[0]=0\n":
      c==1?"walk far=0x10000100 plvl=0 wnr=0 pnu=1 ind=0 ptwf=0 afr=0 fsynr0_hi=0 tcr2=0x38061 sep=7 upstream bit=0 sign=0 l0[0]=0x880000003 l1[0]=0x880000003 l2[128]=0x880001003 l3[0]=0xa0000443\n":
      "fault step=hold"));
  assert(smmuResume==(c==0) && starts==(c==0?7u:62u) && dpuTranslated==(c==0?6u:61u) && sspp[0xa4/4]==0xb8000000u);
  assert(!strstr(dpuLog,"hold_ms=2000 fsr") && !fg_scanout_failed);
  dpu_restored();
 }
 // The bank ignores the table: undone before translation is enabled, no START at all.
 dpu_setup(panel,0xc00000u);smmuIgnore=1;assert(!fuxi_gpu_scanout_acquire());smmuIgnore=0;
 assert(strstr(dpuLog,"fault step=install reason=bank ignored the table") && strstr(dpuLog,"exact=1") && !starts && !strstr(dpuLog,"undone"));
 dpu_restored();
 // A bank whose TCR2 has no SEP field (reads 0, the X229 setting): refused at the readback, before translation, no START.
 dpu_setup(panel,0xc00000u);smmuSepRaz=1;assert(!fuxi_gpu_scanout_acquire());smmuSepRaz=0;
 assert(strstr(dpuLog,"fault step=install reason=bank ignored the table") && strstr(dpuLog,"exact=1") && !starts && !strstr(dpuLog,"walk far"));
 dpu_restored();
 // A real walk fault (not injected): the window's level-3 entry of the first row is cleared under the display; FAR, PLVL 3, entries.
 dpu_setup(panel,0xc00000u);startHook=dpu_fetch_hole;assert(!fuxi_gpu_scanout_acquire());
 assert(strstr(dpuLog,"fault step=flip reason=fault starts=60 fsr=0x402 far=0x10014458 fsynr0=0x23\n"));
 assert(strstr(dpuLog,"walk far=0x10014458 plvl=3 wnr=0 pnu=1 ind=0 ptwf=0 afr=0 fsynr0_hi=0 tcr2=0x38061 sep=7 upstream bit=0 sign=0 l0[0]=0x880000003 l1[0]=0x880000003 l2[128]=0x880001003 l3[20]=0\n"));
 assert(strstr(dpuLog,"undone step=flip start=1") && strstr(dpuLog,"bank restored exact=1") && !dpuBadFetch);
 dpu_restored();
 // A flip transfer that never finishes: translation stays on (identity + window), the memory is kept and the scanout stops.
 dpu_setup(panel,0xc00000u);dpuStuck=61;assert(fuxi_gpu_scanout_acquire()==-1 && fg_scanout_failed);
 assert(strstr(dpuLog,"fault step=flip reason=transfer timeout") && strstr(dpuLog,"translation kept on") && (smmu[DPU_CB/4]&1) && !dpuFrees);
 assert(sspp[0x14/4]==0xb8000000u && !smmuMapped);
 // Refusals before any SMMU write: translation already on, a recorded fault, CBAR type, two banks, VIG0 flush pending, source not A,
 // allocations and page lookups.
 for(unsigned c=0;c<9;c++){
  static const char *why[9]={"translation already on","fault already recorded","CBAR is not stage 1","display streams on two banks",
      "transfer or VIG0 flush pending","source is not A","step=memory reason=allocation","step=memory reason=allocation","surface page beyond OAS"};
  dpu_setup(panel,0xc00000u);
  if(c==0)smmu[DPU_CB/4]=0xe1;
  if(c==1)smmu[(DPU_CB+0x58)/4]=0x402;
  if(c==2)smmu[(0x1000+4*3)/4]=0x2f000u;
  if(c==3){smmu[(0x800+4*9)/4]=0x80001c02u;smmu[(0xc00+4*9)/4]=4;}
  if(c==4)ctl[0x18/4]=1;
  if(c==5)sspp[0x14/4]=0xb8a00000u;
  if(c==6)dpuAllocFail=1;
  if(c==7)dpuAllocFail=2;
  if(c==8)dpuPageFail=7;
  memcpy(smmuSnap,smmu,sizeof(smmu));
  assert(!fuxi_gpu_scanout_acquire() && strstr(dpuLog,why[c]) && strstr(dpuLog,"nothing written"));
  assert(!smmuWrites && !starts && !memcmp(smmu,smmuSnap,sizeof(smmu)) && dpuFrees==dpuAllocCount && !smmuMapped);
  ctl[0x18/4]=0;dpuAllocFail=dpuPageFail=0;
 }
 reset();
 puts("PASS: dpu stage 2 (bits 22+23): bank-3-only writes, byte-exact restore, bank TLBI only, identity + scattered-window SMMUv2 walk model (TCR2.SEP, T0SZ start level, X229 replay), walk/fault-entry log, fault/stall/timeout undo, refusals write nothing, bits clear inert");
}
// Direct scanout stage 3 (bits 22+23+24): CA surfaces through the kernel API as SweetSurface drives it (slot, queue, offer, START).
static uint64_t ddPages[8][2741];
static void dd_pages(void) {
 for(unsigned v=0;v<8;v++)for(unsigned i=0;i<2741;i++)   // scattered (a permutation), odd surfaces above 4 GiB
  ddPages[v][i]=(v&1?0x940000000ull:0x60000000ull)+v*0x1000000ull+(uint64_t)(i*7%2741)*4096ull;
}
static void dd_te(void) {intf[0x298/4]+=1u<<16;ticks+=16667;}
// One present of surface v (key v+1): slot (miss: the page list), queue with fence; the slot that got it, or -1 refused / -2 queue refused.
static int dd_queue(unsigned v,uint32_t fence) {
 unsigned slot=9;uint64_t old=0;
 assert(fuxi_gpu_scanout_acquire()==1);
 int r=fuxi_gpu_scanout_direct_slot(v+1,&slot,&old);
 if(!r)return -1;
 assert(slot<4 && (r==1)==(fg_dd_key[slot]==v+1));
 int q=fuxi_gpu_scanout_direct_queue(slot,v+1,r==2?ddPages[v]:0,2741,0x7000000+v*4096,fence);
 if(q!=1)return -2;
 if(r==2)dpuSlotPages[slot]=ddPages[v];
 assert(fg_scanout_copy==3+slot && (fuxi_gpu_scanout_direct_busy()&(1u<<slot)));
 return (int)slot;
}
// Offer and START it at the next TE; checks the fetch went to the slot's IOVA.
static int dd_show(unsigned v,uint32_t fence) {
 int slot=dd_queue(v,fence);if(slot<0)return slot;
 unsigned f=dpuSlotFetches[slot],st=starts;
 int p=fuxi_gpu_scanout_publish();
 for(unsigned i=0;p==2 && i<4;i++){dd_te();p=fuxi_gpu_scanout_retry();}
 assert(p==1 && starts==st+1 && dpuSlotFetches[slot]==f+1 && dpuLastSrc==0x10000000u+(unsigned)slot*0x1000000u && fg_scanout_front==3u+slot);
 dd_te();
 return slot;
}
static void dd_setup(uint8_t *panel,unsigned bits) {
 dpuTriple=1;dpu_setup(panel,bits);dpuTriple=1;
 assert(!fuxi_gpu_scanout_acquire());ticks+=20000;
}
static void dpu_stage3_tests(uint8_t *panel) {
 dd_pages();
 dd_setup(panel,0x1c00000u);
 assert(strstr(dpuLog,"dpu stage3 requested: stage2 keeps the translation, CA surfaces scan out through 4 IOVA slots\n"));
 assert(strstr(dpuLog,"dpu stage2 mapped iova=0x10000000 pages=2741 window=0x4000000 tlbi=bank\n"));
 assert(strstr(dpuLog,"dpu stage2 hold_ms=100 fsr=0x400\n"));
 assert(strstr(dpuLog,"dpu stage3 armed slots=4 iova=0x10000000 slot_bytes=0x1000000 tables=0x880000000 bank=cb3 fsr=0x400 A img=0 xy=0 ystride0=0x10e0\n"));
 assert(strstr(dpuLog,"dpu stage2 done result=ok step=arm identity_starts=60 flip_latched=1 hold_ms=100 translation=kept\n"));
 assert(!strstr(dpuLog,"bank restored") && (smmu[DPU_CB/4]&1) && smmuMapped==2 && fuxi_gpu_scanout_direct_on() && !dpuBadFetch);
 assert(smmu[(DPU_CB+0x10)/4]==0x38061u && dpuAllocCount==2 && dpuFrees==1);        // translation kept, test surface freed, table kept
 { uint64_t pa;unsigned l;assert(!dpu_walk_at(0x10000000u,&pa,&l) && l==3); }        // the test surface's leaves are empty again
 // Identity A/B/C keep working: the first GPU frame is a copy into B, translated 1:1.
 assert(fuxi_gpu_scanout_acquire()==1 && fuxi_gpu_scanout_triple() && fg_scanout_copy==1);
 unsigned tl=fg_dd_st[FG_DDS_TLBI];
 assert(fuxi_gpu_scanout_publish()==1 && fg_scanout_front==1 && dpuLastSrc==0xb8a00000u && !dpuBadFetch && fg_dd_st[FG_DDS_COPIES]==1);dd_te();
 // Four CA surfaces fill the four slots: each START fetches the slot's IOVA through exactly that surface's pages (every row, 3 columns).
 unsigned checks=dpuPixelChecks;
 for(unsigned v=0;v<4;v++)assert(dd_show(v,0)==(int)v);
 assert(dpuPixelChecks==checks+4*3*2400 && !dpuBadFetch && fg_dd_st[FG_DDS_MAPS]==4 && !fg_dd_st[FG_DDS_REMAPS] && fg_dd_st[FG_DDS_FLIPS]==4);
 assert(fg_dd_st[FG_DDS_TLBI]==tl+4 && strstr(dpuLog,"dpu direct map slot=1 iova=0x11000000 pages=2741 above4g=2741 runs=2741 first=0x941000000 tlbi=bank\n"));
 assert(strstr(dpuLog,"dpu direct flip slot=2 src0=0x12000000 img=0x9840465 xy=0x120016 ystride0=0x1200 te="));
 // Hits (CA alternates two surfaces): no page list, no map, no TLBI; the previous slot's fetch is released at the next START (late).
 unsigned late=fg_dd_st[FG_DDS_LATE];tl=fg_dd_st[FG_DDS_TLBI];
 for(unsigned i=0;i<6;i++)assert(dd_show(2+(i&1),0)==(int)(2+(i&1)));
 assert(fg_dd_st[FG_DDS_HITS]==6 && fg_dd_st[FG_DDS_MAPS]==4 && fg_dd_st[FG_DDS_TLBI]==tl && fg_dd_st[FG_DDS_LATE]>=late+6 && !dpuBadFetch);
 // On-screen release: the write line seen active after the START's TE, then idle with START clear (busy poll), no next START needed.
 assert(fuxi_gpu_scanout_direct_busy()&(1u<<3));
 unsigned on=fg_dd_st[FG_DDS_ONTIME];intf[0x2b0/4]=300;assert(fuxi_gpu_scanout_direct_busy()&(1u<<3));
 intf[0x2b0/4]=2400;assert(!(fuxi_gpu_scanout_direct_busy()&(1u<<3)) && fg_dd_st[FG_DDS_ONTIME]==on+1 && fg_scanout_front==6);
 // ... or 3 TEs after its START with the line idle: late. A busy try-lock returns the last mask without touching state.
 assert(dd_show(2,0)==2 && (fuxi_gpu_scanout_direct_busy()&4));
 tryBusy=1;intf[0x298/4]+=3u<<16;assert(fuxi_gpu_scanout_direct_busy()==4u);tryBusy=0;
 late=fg_dd_st[FG_DDS_LATE];assert(!fuxi_gpu_scanout_direct_busy() && fg_dd_st[FG_DDS_LATE]==late+1);
 // LRU: a fifth surface evicts the least recently queued slot, never the one on screen, fetched, pending or queued.
 for(unsigned k=0;k<4;k++)fg_dd_used[k]=10+k;
 fg_dd_used[2]=1;                                                         // the on-screen slot is the oldest
 { unsigned slot=9;uint64_t old=0;assert(fuxi_gpu_scanout_acquire()==1);
   assert(fuxi_gpu_scanout_direct_slot(5,&slot,&old)==2 && slot==0 && old==1 && fg_dd_st[FG_DDS_EVICTS]==1 && !fg_dd_key[0]);
   uint64_t pa;unsigned l;assert(!dpu_walk_at(0x10000000u+0x14458u,&pa,&l) && l==3);   // emptied before reuse (bank TLBIALL)
   assert(fuxi_gpu_scanout_direct_queue(0,5,ddPages[4],2741,0x7004000,0)==1);dpuSlotPages[0]=ddPages[4];
   assert(fuxi_gpu_scanout_publish()==2 || fg_scanout_front==3);       // pending (or STARTed at once)
 }
 if(fg_scanout_front!=3){for(unsigned i=0;i<4 && fg_scanout_front!=3;i++){dd_te();(void)fuxi_gpu_scanout_retry();}}
 assert(fg_scanout_front==3 && fg_dd_st[FG_DDS_REMAPS]==1 && dpuSlotFetches[0]==2);dd_te();
 // Pending: surface 6 queued and offered while the transfer runs (START blocked); a seventh surface cannot take its slot or the front.
 ctl[0x1c/4]=1;intf[0x2b0/4]=100;startHook=dpu_fetch;
 { int s6=dd_queue(5,0);assert(s6>=0 && s6!=0);assert(fuxi_gpu_scanout_publish()==2 && fg_scanout_back_ready && fg_scanout_ready==3u+(unsigned)s6);
   for(unsigned k=0;k<4;k++)fg_dd_used[k]=0;
   unsigned slot=9;uint64_t old=0;assert(fuxi_gpu_scanout_acquire()==1);
   int r=fuxi_gpu_scanout_direct_slot(7,&slot,&old);assert(r==2 && slot!=0 && slot!=(unsigned)s6);
   // All four busy (front, pending, fetched, queued): refused, the frame is a copy.
   fg_dd_key[slot]=8;assert(fuxi_gpu_scanout_direct_queue(slot,8,0,0,0x7000000,0)==1);
   unsigned other=0;while(other==0 || other==(unsigned)s6 || other==slot)other++;fg_dd_fetch=other;
   unsigned refused=fg_dd_why[FG_DD_SLOTS];assert(!fuxi_gpu_scanout_direct_slot(9,&slot,&old) && fg_dd_why[FG_DD_SLOTS]==refused+1);
   assert(fuxi_gpu_scanout_direct_busy()==14u);                           // the front slot 0 is not read after its transfer (panel RAM)
   fg_dd_fetch=~0u;fg_dd_next=~0u;fg_dd_key[slot]=0;
   // The offer of a copy replaces the pending slot frame (newest wins) and restores A geometry at its START.
   fg_scanout_copy=fg_scanout_target();assert(fg_scanout_copy<3);
   unsigned copies=fg_dd_st[FG_DDS_COPIES];assert(fuxi_gpu_scanout_publish()==2 && fg_scanout_ready<3 && fg_dd_st[FG_DDS_COPIES]==copies+1);
   ctl[0x1c/4]=0;intf[0x2b0/4]=2400;dd_te();
   assert(fuxi_gpu_scanout_retry()==1 && fg_scanout_front<3 && !fg_dd_geo && dpuLastSrc==0xb8000000u+fg_scanout_front*0xa00000u && !dpuBadFetch);
   assert(sspp[0x04/4]==0 && sspp[0x08/4]==0 && sspp[0x24/4]==4320);dd_te();
 }
 // The writer's fence: no START until it is written (publish 2, polled), then the slot STARTs.
 { abiFenceDone=49;int slot=dd_queue(1,50);assert(slot>=0);unsigned st=starts,polls=fg_dd_st[FG_DDS_FENCE_POLLS];
   assert(fuxi_gpu_scanout_publish()==2 && starts==st);dd_te();
   assert(fuxi_gpu_scanout_retry()==2 && starts==st && fg_dd_st[FG_DDS_FENCE_POLLS]==polls+2);
   ticks+=500;abiFenceDone=50;assert(fuxi_gpu_scanout_retry()==1 && starts==st+1 && fg_scanout_front==3u+(unsigned)slot && fg_dd_fw_max>=500);dd_te();
   abiFenceDone=~0u; }
 // Refusals before any table write: bad pages (zero, unaligned, beyond OAS, too few), wrong slot state.
 { unsigned slot=9;uint64_t old=0;static uint64_t bad[2741];assert(fuxi_gpu_scanout_acquire()==1);
   for(unsigned c=0;c<4;c++){
    assert(fuxi_gpu_scanout_direct_slot(20+c,&slot,&old)==2);memcpy(bad,ddPages[6],sizeof(bad));
    if(c==0)bad[100]=0;if(c==1)bad[5]|=8;if(c==2)bad[2740]=0x1000000000ull;
    unsigned pr=fg_dd_why[FG_DD_PAGES];assert(!fuxi_gpu_scanout_direct_queue(slot,20+c,bad,c==3?100:2741,0x7000000,0) && fg_dd_why[FG_DD_PAGES]==pr+1 && !fg_dd_key[slot]);
   }
   assert(!fuxi_gpu_scanout_direct_queue(slot,77,0,0,0x7000000,0));   // a hit for a key the slot does not hold
   fg_scanout_copy=fg_scanout_target();assert(fuxi_gpu_scanout_publish()>=1);dd_te();(void)fuxi_gpu_scanout_retry();dd_te(); }
 // The 600-present counters line.
 fg_dd_offers=599;assert(dd_show(3,0)>=0);
 assert(strstr(dpuLog,"FUXI SCANOUT: dpu direct flips=") && strstr(dpuLog," copies=") && strstr(dpuLog," refused(geometry/stride/offset/format/backing/layers/pages/slots/state)=0/0/0/0/0/0/4/1/"));
 assert(strstr(dpuLog," slot_evicts=") && strstr(dpuLog," release_late=") && strstr(dpuLog," fence_wait_us avg=") && strstr(dpuLog," on=1 dead=0 busy="));
 assert(fg_dd_st[FG_DDS_FLIPS]==1);
 assert(strstr(dpuLog,"FUXI SCANOUT: dpu direct flips=13 copies=3 refused(geometry/stride/offset/format/backing/layers/pages/slots/state)=0/0/0/0/0/0/4/1/1 maps=4 remaps=3 hits=8 tlbi=11 fsr=0 slot_evicts=4 release_late=11 release_on_time=1 wait_forced=0 fence_polls=2 fence_wait_us avg="));
 // A translation fault on a slot fetch: GPU copies for the rest of the boot, A geometry on a free buffer, identity kept, FSR cleared.
 { int slot=dd_queue(0,0);assert(slot>=0);dpuFaultAt=dpuFetches+1;
   unsigned f=dpuFetches;assert(fuxi_gpu_scanout_publish()>=1);for(unsigned i=0;i<4 && dpuFetches==f;i++){dd_te();(void)fuxi_gpu_scanout_retry();}
   assert(dpuFetches==f+1 && dpuLastSrc==0x10000000u+(unsigned)slot*0x1000000u);
   assert(smmu[(DPU_CB+0x58)/4]&2);dd_te();
   unsigned resumes=smmuResume;(void)fuxi_gpu_scanout_direct_busy();
   assert(!fuxi_gpu_scanout_direct_on() && fg_dd_dead && fg_dd_st[FG_DDS_FSR]==1 && !(smmu[(DPU_CB+0x58)/4]&FG_DPU_FAULTS) && smmuResume==resumes);
   char want[160];snprintf(want,sizeof(want),"dpu direct fault where=busy fsr=0x402 far=%#x fsynr0=0x20 front=%u",0x10000100u+(unsigned)slot*0x1000000u,3u+(unsigned)slot);
   assert(strstr(dpuLog,want) && strstr(dpuLog,"GPU copy for the rest of the boot, identity kept\n"));
   snprintf(want,sizeof(want),"walk far=%#x plvl=0",0x10000100u+(unsigned)slot*0x1000000u);assert(strstr(dpuLog,want));
   assert(strstr(dpuLog,"dpu direct fallback A geometry img=0 xy=0 ystride0=0x10e0 src0=0xb8"));
   assert(fg_scanout_front<3 && sspp[0x04/4]==0 && sspp[0x08/4]==0 && sspp[0x24/4]==4320 && (smmu[DPU_CB/4]&1));
   unsigned slot2=9;uint64_t old=0;assert(fuxi_gpu_scanout_acquire()==1 && !fuxi_gpu_scanout_direct_slot(1,&slot2,&old));
   assert(fg_scanout_copy<3 && fuxi_gpu_scanout_publish()>=1);
   for(unsigned i=0;i<4 && fg_scanout_back_ready;i++){dd_te();(void)fuxi_gpu_scanout_retry();}
   assert(!fg_scanout_back_ready && fg_scanout_front<3 && dpuLastSrc>=0xb8000000u && !fg_scanout_failed); }
 // A fault while a slot frame is pending: the frame is dropped, nothing STARTs from a slot, the next copy proceeds.
 dd_setup(panel,0x1c00000u);assert(fuxi_gpu_scanout_direct_on());
 assert(fuxi_gpu_scanout_acquire()==1 && fuxi_gpu_scanout_publish()==1);dd_te();
 { ctl[0x1c/4]=1;intf[0x2b0/4]=100;int slot=dd_queue(4,0);assert(slot>=0 && fuxi_gpu_scanout_publish()==2 && fg_scanout_ready==3u+(unsigned)slot);
   smmu[(DPU_CB+0x58)/4]|=0x40000002u;ctl[0x1c/4]=0;intf[0x2b0/4]=2400;dd_te();unsigned st=starts,resumes=smmuResume;
   assert(fuxi_gpu_scanout_retry()!=1 && starts==st && !fg_scanout_back_ready && smmuResume==resumes+1 && !fuxi_gpu_scanout_direct_on());
   assert(strstr(dpuLog,"dpu direct fault where=start fsr=0x40000402") && !(smmu[(DPU_CB+0x58)/4]&FG_DPU_FAULTS));
   assert(fuxi_gpu_scanout_acquire()==1 && fg_scanout_copy<3 && fuxi_gpu_scanout_publish()==1 && dpuLastSrc>=0xb8000000u && dpuLastSrc<0xc0000000u && !dpuBadFetch); }
 // Stage 3 needs triple buffering and stage 2: otherwise stage 2 runs alone and restores the bank.
 dpuTriple=0;dpu_setup(panel,0x1c00000u);assert(!fuxi_gpu_scanout_acquire());
 assert(strstr(dpuLog,"dpu stage3 not armed reason=needs fuxiscantriple=1; stage2 test only") && strstr(dpuLog,"hold_ms=2000 fsr") && !fuxi_gpu_scanout_direct_on());
 dpu_restored();
 // Bits off: bit 24 alone (or with 22 only) is ignored, nothing is armed, every call refuses or is inert.
 for(unsigned c=0;c<3;c++){
  dpuTriple=1;dpu_setup(panel,c==0?0x1000000u:c==1?0x1400000u:0xc00000u);
  if(c==2){assert(!fuxi_gpu_scanout_acquire());ticks+=20000;}
  assert(!fuxi_gpu_scanout_direct_on() && !fuxi_gpu_scanout_direct_busy() && (c==2?strstr(dpuLog,"bank restored exact=1")!=0:!smmuRwMaps));
  assert(c==2?!strstr(dpuLog,"stage3"):strstr(dpuLog,"dpu stage3 ignored: bit 24 needs bits 22+23")!=0);
  assert(fuxi_gpu_scanout_acquire()==1);unsigned slot=9;uint64_t old=0;
  assert(!fuxi_gpu_scanout_direct_slot(1,&slot,&old) && !fuxi_gpu_scanout_direct_queue(0,1,ddPages[0],2741,0x7000000,0));
  fuxi_gpu_scanout_direct_refused(FG_DD_GEOMETRY);assert(!fg_dd_why[FG_DD_GEOMETRY]);
  assert(fuxi_gpu_scanout_publish()==1 && dpuLastSrc==0xb8a00000u && !fg_dd_st[FG_DDS_COPIES] && !fg_dd_offers);
 }
 dpuTriple=0;reset();
 puts("PASS: dpu stage 3 (bits 22+23+24): armed after stage 2 (translation kept), slot IOVA/pages per flip through the walk model, hits without maps/TLBI, on-time/late release, LRU never evicts front/pending/fetched/queued, page refusals, writer fence before START, 600-present counters, FSR fault (on screen or pending) -> copies for the boot with A geometry and identity kept, needs triple, bits off inert");
}
static void panel120_tests(uint8_t *panel) {
 // Exact boot-arg admission; no ownership means no DSI read, write, or command.
 reset();panel120_setup();modelPanel120=0;panel120_drain(panel);
 assert(fuxi_gpu_scanout_acquire()==1 && !fg_scanout_panel120_requested && !dsiReads && !dmaStarts);
 reset();panel120_setup();modelPanel120=2;panel120_drain(panel);
 assert(fuxi_gpu_scanout_acquire()==1 && !fg_scanout_panel120_requested && !dsiReads && !dmaStarts);
 reset();panel120_setup();fuxi_gpu_scanout_configure((uintptr_t)panel,0,0);
 assert(fuxi_gpu_scanout_acquire()==1 && !reads && !writes);
 reset();panel120_setup();panel120_drain(panel);
 ctl[0x1c/4]=1;assert(!fuxi_gpu_scanout_acquire() && !dsiReads);
 ctl[0x1c/4]=0;intf[0x2b0/4]=2399;assert(!fuxi_gpu_scanout_acquire() && !dsiReads);
 intf[0x2b0/4]=2400;fg_busy=1;assert(!fuxi_gpu_scanout_acquire() && !dsiReads);fg_busy=0;
 assert(!fuxi_gpu_scanout_acquire() && fg_scanout_panel120_active && dmaStarts==1 && !starts);
 assert(fg_scanout_period_us==8333 && fg_scanout_drain_us==10000 && !fg_scanout_writable);
 // Literal embedded-packet oracle: exact DCS bytes/types, only final LAST,
 // ff payload padding, odd-DWORD zero padding outside the 52-byte length.
 static const uint32_t expected[]={
  0x40390003,0xff5a5af0,0x00150060,0x40390004,0xbd3000b0,0x001504bd,
  0x40390004,0xf21600b0,0x40390003,0xff7001f2,0x00150ff7,0xc0390003,0xffa5a5f0,0};
 unsigned fifo=0,triggerIndex=0,firstTear=0,lastDone=0;
 for(unsigned i=0;i<traceCount;i++){
  if(trace[i].pa==0xae9417c){assert(fifo<14 && trace[i].value==expected[fifo]);fifo++;}
  if(trace[i].pa==0xae94090)triggerIndex=i;
  if(trace[i].pa==0xae94110 && (trace[i].value&1))lastDone=i;
  if(trace[i].pa==0xae36288 && !firstTear)firstTear=i;
  assert(trace[i].pa!=0xae94094); // Never generate an MDP transfer to send DCS.
 }
 assert(fifo==14 && triggerIndex>14 && firstTear>lastDone && lastDone>triggerIndex);
 // Check ordering around FIFO setup/kickoff/cleanup, not just final register contents.
 unsigned t=1; // First write disabled AR.
 assert(trace[t].pa==0xae94084 && trace[t++].value==0x80000064);
 assert(trace[t].pa==0xae94110 && trace[t++].value==0x201);
 assert(trace[t].pa==0xae941ec && trace[t++].value==1);
 assert(trace[t].pa==0xae941ec && trace[t++].value==0);
 assert(trace[t].pa==0xae9415c && trace[t++].value==0x30006);
 t+=14;
 assert(trace[t].pa==0xae9403c && trace[t++].value==0x120000ab);
 assert(trace[t].pa==0xae9404c && trace[t++].value==52);
 assert(trace[t].pa==0xae94090 && trace[t++].value==1);
 assert(trace[t].pa==0xae94110 && trace[t++].value==0x201);
 assert(dsi[0x03c/4]==0xc60000ab && dsi[0x084/4]==0x80011066 && !dsi[0x15c/4] && dsi[0x04c/4]==0x54);
 assert(dsi[0x110/4]==0x100202); // Preserve unrelated W1C status, restore interrupt masks.
 // Tear-check counter enable is VSYNC bit19, not TEAR_CHECK_EN at +284.
 assert(trace[firstTear].value==0x100085);
 assert(trace[firstTear+1].pa==0xae36288 && trace[firstTear+1].value==0x100042);
 assert(trace[firstTear+2].pa==0xae3628c && trace[firstTear+2].value==0xfff0);
 assert(trace[firstTear+3].pa==0xae36288 && trace[firstTear+3].value==0x180042);
 assert(intf[0x288/4]==0x180042 && intf[0x284/4]==1 && intf[0x294/4]==2400);
 ticks=fg_scanout_after-1;assert(!fuxi_gpu_scanout_acquire());ticks++;
 assert(fuxi_gpu_scanout_acquire()==1 && fuxi_gpu_scanout_publish()==1 && starts==1);
 ctl[0x1c/4]=0;ticks+=10000;assert(fuxi_gpu_scanout_acquire()==1 && dmaStarts==1); // once
 // fuxigpuncimport 8192: the panel120 floor is 3/4 period (the 10 ms default exceeds one 8.33 ms period); explicit drain keeps its value.
 assert(fg_scanout_drain_us==10000 && !fg_abi_start);
 fg_scanout_arm(0);assert(fg_scanout_after-fg_scanout_started==10000);
 fg_abi_start=1;fg_scanout_arm(0);assert(fg_scanout_after-fg_scanout_started==6249);
 fg_scanout_explicit_drain=1;fg_scanout_arm(0);assert(fg_scanout_after-fg_scanout_started==10000);fg_scanout_explicit_drain=0;
 fg_scanout_te_mode=1;fg_scanout_arm(0);assert(fg_scanout_after-fg_scanout_started==10000);fg_scanout_te_mode=0;
 fg_abi_start=0;
 // Measurement: START-to-transfer-done is sampled inside the floor; busy and not-yet-idle samples record nothing.
 fg_scanout_xfer_n=fg_scanout_xfer_sum=fg_scanout_xfer_max=0;memset(fg_scanout_xfer_hist,0,sizeof(fg_scanout_xfer_hist));
 fg_scanout_arm(0);ctl[0x1c/4]=1;intf[0x2b0/4]=100;ticks+=3000;fg_scanout_xfer_sample();assert(!fg_scanout_xfer_n && fg_scanout_xfer_open);
 ctl[0x1c/4]=0;fg_scanout_xfer_sample();assert(!fg_scanout_xfer_n);intf[0x2b0/4]=2400;ticks+=4400;fg_scanout_xfer_sample();
 assert(fg_scanout_xfer_n==1 && fg_scanout_xfer_sum==7400 && fg_scanout_xfer_max==7400 && fg_scanout_xfer_hist[3]==1 && !fg_scanout_xfer_open);
 fg_scanout_xfer_sample();assert(fg_scanout_xfer_n==1); // one sample per START
 // ready-to-START split: each blocked poll is charged for the interval since the previous one.
 memset(fg_scanout_charge,0,sizeof(fg_scanout_charge));fg_scanout_ready_at=ticks;fg_scanout_charged_at=0;
 ticks+=100;fg_scanout_charge_to(FG_WHY_XFER);ticks+=50;fg_scanout_charge_to(FG_WHY_FLOOR);
 assert(fg_scanout_charge[FG_WHY_XFER]==100 && fg_scanout_charge[FG_WHY_FLOOR]==50 && fg_scanout_last_why==FG_WHY_FLOOR);
 fg_scanout_ready_at=fg_scanout_charged_at=0;
 // DSI transfer probe (bit 16384): TE edge, START clear and write-line end are stamped relative to the START write.
 {
  fg_abi_probe=1;fg_scanout_probe_busy=0;fg_scanout_starts=8;probeKicks=0;
  fg_scanout_probe_n=fg_scanout_probe_timeouts=0;memset(fg_scanout_probe_sum,0,sizeof(fg_scanout_probe_sum));
  fg_scanout_probe_kick(0);assert(probeKicks==1 && fg_scanout_probe_busy);
  fg_scanout_probe_kick(0);assert(probeKicks==1); // one at a time
  fg_scanout_starts=9;fg_scanout_probe_busy=0;fg_scanout_probe_kick(0);assert(probeKicks==1); // only every 8th START
  fg_scanout_starts=16;fg_scanout_probe_kick(0);assert(probeKicks==2);
  ctl[0x1c/4]=1;intf[0x298/4]=0;intf[0x2b0/4]=0;ticks+=5;base=ticks;fg_scanout_probe_t0=ticks;probeScript=probe_script;
  fg_scanout_probe_run(0,0);probeScript=0;
  fg_scanout_probe_account();assert(!fg_scanout_probe_busy && fg_scanout_probe_n==1 && !fg_scanout_probe_timeouts);
  assert(fg_scanout_probe_done[0]>=8000 && fg_scanout_probe_done[0]<=8020 && fg_scanout_probe_hist[1+(fg_scanout_probe_done[0]-6000)/250]==1);
  assert(fg_scanout_probe_te2d[0]>=6980 && fg_scanout_probe_te2d[0]<=7040 && fg_scanout_probe_edges==1);
  assert(fg_scanout_probe_sum[1]>=8000 && fg_scanout_probe_sum[2]>=8000);
  fg_scanout_probe_report();assert(!fg_scanout_probe_n && !fg_scanout_probe_edges);
  // A transfer that never completes times out after 20 ms and is counted, not accounted as a duration.
  fg_scanout_probe_busy=1;ctl[0x1c/4]=1;fg_scanout_probe_t0=ticks;uint64_t t1=ticks;fg_scanout_probe_run(0,0);
  assert(ticks-t1>=20000 && ticks-t1<=20100);fg_scanout_probe_account();assert(fg_scanout_probe_timeouts==1 && !fg_scanout_probe_n);
  fg_scanout_probe_timeouts=0;fg_abi_probe=0;fg_scanout_probe_busy=0;fg_scanout_starts=16;fg_scanout_probe_kick(0);assert(probeKicks==2); // off by default
  ctl[0x1c/4]=0;intf[0x2b0/4]=0;
 }
 // Gaps now count 8.333 ms periods, including 16.667ms as two periods.
 ticks=fg_scanout_last_start+8333;assert(fuxi_gpu_scanout_publish()==1);
 ctl[0x1c/4]=0;ticks+=16667;assert(fuxi_gpu_scanout_acquire()==1 && fuxi_gpu_scanout_publish()==1);
 assert(fg_scanout_iv[0]==1 && fg_scanout_iv[1]==1 && !fg_scanout_iv[2] && !fg_scanout_iv[3]);
 // Preflight busy: bounded abort, no FIFO/tear writes, no retry, 60 Hz retained.
 reset();panel120_setup();panel120_drain(panel);dsi[0x008/4]=4;
 unsigned before=writes;uint64_t began=ticks;
 assert(fuxi_gpu_scanout_acquire()==-1 && ticks-began==20000 && writes==before && !dmaStarts);
 assert(!fg_scanout_panel120_active && fg_scanout_period_us==16667 && intf[0x288/4]==0x180085);
 before=dsiReads;assert(fuxi_gpu_scanout_acquire()==-1 && dsiReads==before);
 // Invalid inherited state refuses the switch without trying panel initialization.
 reset();panel120_setup();panel120_drain(panel);intf[0x288/4]=0x100085;
 before=writes;assert(fuxi_gpu_scanout_acquire()==1 && writes==before && !dmaStarts);
 // Stale completion may not satisfy a new request. No command after clear timeout.
 reset();panel120_setup();panel120_drain(panel);staleDone=1;
 assert(fuxi_gpu_scanout_acquire()==-1 && !dmaStarts && !fg_scanout_panel120_active && !starts);
 assert(intf[0x288/4]==0x180085);
 // Lost done or stuck busy after done: hold, no cleanup/rollback/tear/START,
 // no second attempt. No assumption that a partially sent command can be undone.
 for(unsigned fault=0;fault<3;fault++){
  reset();panel120_setup();panel120_drain(panel);
  noDone=fault!=1;busyAfterDone=fault==1;frozenClock=fault==2;
  began=ticks;assert(fuxi_gpu_scanout_acquire()==-1 && dmaStarts==1 && !fg_scanout_panel120_active && !starts);
  assert(fg_scanout_period_us==16667 && fg_scanout_drain_us==20000 && intf[0x288/4]==0x180085);
  assert(trace[traceCount-1].pa==0xae94090 && dsiReads<2100);
  assert(frozenClock || ticks-began>=20000);
  before=writes;assert(fuxi_gpu_scanout_acquire()==-1 && fuxi_gpu_scanout_publish()==-1 && writes==before);
 }
 // MMIO failure during FIFO loading and tear programming also hold publication.
 for(unsigned fault=0;fault<3;fault++){
  reset();panel120_setup();panel120_drain(panel);failWriteAddress=fault==2?0xae3628c:fault?0xae36288:0xae9417c;
  assert(fuxi_gpu_scanout_acquire()==-1 && !fg_scanout_panel120_active && !starts && interruptsOn);
 }
 // Transfer error and tear-register readback mismatch cannot activate 120Hz.
 for(unsigned fault=0;fault<2;fault++){
  reset();panel120_setup();panel120_drain(panel);errorOnDone=fault==0;tearReadbackMismatch=fault==1;
  assert(fuxi_gpu_scanout_acquire()==-1 && dmaStarts==1 && !fg_scanout_panel120_active && !starts && interruptsOn);
 }
 // Explicit drain is in microseconds and survives a successful mode change.
 reset();panel120_setup();modelDrain=4000;panel120_drain(panel);
 assert(!fuxi_gpu_scanout_acquire() && fg_scanout_panel120_active && fg_scanout_drain_us==4000);
 // TE gate uses the same line margin, twice as fast counter, 20ms stall budget,
 // and a 10ms fallback. The inherited 60 Hz transfer was drained first.
 reset();panel120_setup();modelTe=1;panel120_drain(panel);
 intf[0x298/4]=(1u<<16)|2500;
 assert(!fuxi_gpu_scanout_acquire() && fg_scanout_panel120_active && !fg_scanout_drain_us);
 intf[0x298/4]=(2u<<16)|2410;assert(!fuxi_gpu_scanout_acquire());
 intf[0x298/4]=(2u<<16)|2464;assert(fuxi_gpu_scanout_acquire()==1 && fuxi_gpu_scanout_publish()==1);
 ctl[0x1c/4]=0;ticks=fg_scanout_started+19999;
 assert(!fuxi_gpu_scanout_acquire() && fg_scanout_te_mode);
 ticks++;assert(!fuxi_gpu_scanout_acquire() && !fg_scanout_te_mode && fg_scanout_drain_us==10000);
 assert(fuxi_gpu_scanout_acquire()==1);
 puts("PASS: panel120 opt-in, exact HS DCS/FIFO protocol, safe boundary, bounded abort/hold, tear math/order, 120Hz floor/TE/gaps");
}
static int te_sample(struct fg_te_observer *te,unsigned frame,uint64_t time) {
 ticks=time;intf[0x298/4]=((frame&0xffffu)<<16)|2500;
 return fuxi_gpu_scanout_te(te);
}
static void te_estimator_tests(void) {
 reset();struct fg_te_observer te={0};unsigned before=reads;
 assert(!fuxi_gpu_scanout_te(&te) && reads==before && !te.initialized);
 fg_scanout_owned=1;
 // Both mach timebase conversion and measured 60.4/120.8 Hz, with jitter
 // independent of the TE phase. Counter wrap is not a discontinuity.
 for(unsigned scale=1;scale<=3;scale+=2)for(unsigned mode=0;mode<2;mode++) {
  memset(&te,0,sizeof(te));machScale=scale;
  fg_scanout_period_us=mode?8333:16667;
  unsigned period=mode?8278:16556,frame=65530,valid=0;
  uint64_t origin=1000000;
  for(uint64_t t=origin+100;t<origin+period*160ull;t+=417+(t%113)) {
   unsigned n=(unsigned)((t-origin)/period);
   int available=te_sample(&te,frame+n,t);
   if(n<2)assert(!available);
   if(available) {
    valid++;
    uint64_t edge=(origin+(uint64_t)n*period)*scale;
    assert(te.frame==((frame+n)&0xffff));
    assert(te.edge<=t*scale && te.edge+600*scale>=edge && te.edge<=edge+600*scale);
    if(n>80)assert(te.period+100*scale>=period*scale && te.period<=(period+100)*scale);
   }
  }
  assert(valid>1000 && !writes && !observations && !starts);
  // A frozen count expires instead of extrapolating indefinitely.
  assert(!te_sample(&te,te.frame,ticks+period*2));
  assert(!te.samples);
 }
 machScale=1;fg_scanout_period_us=16667;memset(&te,0,sizeof(te));
 assert(!te_sample(&te,1,1000000));
 assert(!te_sample(&te,1,1016400));assert(!te_sample(&te,2,1016800));
 assert(!te_sample(&te,2,1033000));assert(te_sample(&te,3,1033400));
 // Established phase survives a copy spanning two confirmed edges without
 // teaching its uncertain bracket to the period filter.
 uint64_t lockedPeriod=te.period,lockedEdge=te.edge;
 assert(te_sample(&te,5,1066700) && te.edge==lockedEdge+lockedPeriod*2 && te.period==lockedPeriod);
 assert(te_sample(&te,5,1067000)); // same counter is still fresh
 // Skipped counter, backward/reset count, long polling gap and clock reversal
 // all force reacquisition. A read fault cannot expose the previous snapshot.
 assert(!te_sample(&te,7,1067100) && !te.samples);
 assert(!te_sample(&te,6,1067500) && !te.samples);
 assert(!te_sample(&te,7,1073300) && !te.samples);
 assert(!te_sample(&te,7,1000) && !te.samples);
 failAddress=FG_SCAN_INTF+0x298;assert(!fuxi_gpu_scanout_te(&te) && !te.initialized);
 failAddress=0;fg_scanout_owned=0;before=reads;
 assert(!fuxi_gpu_scanout_te(&te) && reads==before);
 // Ownership and a panel timing switch each start with fresh warmup.
 fg_scanout_owned=1;assert(!te_sample(&te,10,2000000));
 fg_scanout_period_us=8333;assert(!te_sample(&te,11,2008300) && te.nominal_us==8333 && !te.samples);
 puts("PASS: actual lock-free TE observer: jitter/period filter, mach units, 60/120Hz, wrap, stall/MMIO/ownership/gap/reset fallback");
}
static void double_setup(uint8_t *panel) {
 reset();modelDouble=1;modelTe=1;
 intf[0x294/4]=2400;intf[0x298/4]=(1u<<16)|2500;intf[0x2b0/4]=2400;
 memset(panel,0x5a,4320u*2400);memset(modelB,0xcc,sizeof(modelB));
 fuxi_gpu_scanout_configure((uintptr_t)panel,1,0);
 assert(!fuxi_gpu_scanout_acquire());
 intf[0x298/4]=(2u<<16)|2500;
 assert(fuxi_gpu_scanout_acquire()==1 && fuxi_gpu_scanout_double());
 assert(!memcmp(panel,modelB,sizeof(modelB)) && !fg_scanout_front);
}
static void triple_setup(uint8_t *panel) {
 reset();modelDouble=modelTriple=modelTe=1;intf[0x2b0/4]=2400;
 fuxi_gpu_scanout_configure((uintptr_t)panel,1,0);
 assert(!fuxi_gpu_scanout_acquire());intf[0x298/4]=(1u<<16)|2500;
 assert(fuxi_gpu_scanout_acquire()==1 && fuxi_gpu_scanout_triple());
 assert(!memcmp(panel,modelB,sizeof(modelB)) && !memcmp(panel,modelC,sizeof(modelC)));
}
static void fifo_setup(uint8_t *panel,unsigned early) {
 reset();modelDouble=modelTriple=modelTe=1;modelEarly=early;intf[0x2b0/4]=2400;
 fuxi_gpu_scanout_configure((uintptr_t)panel,1,0);
 assert(!fuxi_gpu_scanout_acquire());intf[0x298/4]=(1u<<16)|2500;
 assert(fuxi_gpu_scanout_acquire()==1 && fuxi_gpu_scanout_triple());
 assert(fg_scanout_copy==1 && fuxi_gpu_scanout_publish()==1 && fg_scanout_front==1);
}
// fuxiearly bit 4: a copy finishing while the previous frame waits for START queues behind it (bounded to one), START promotes it.
static void fifo_tests(uint8_t *panel) {
 reset();modelDouble=1;modelEarly=4;fuxi_gpu_scanout_configure((uintptr_t)panel,1,0);assert(!fuxi_gpu_scanout_fifo()); // needs triple
 fifo_setup(panel,3);assert(!fuxi_gpu_scanout_fifo());
 fifo_setup(panel,7);assert(fuxi_gpu_scanout_fifo());
 ctl[0x1c/4]=1;intf[0x2b0/4]=100; // a transfer is running: offers wait for START
 assert(fuxi_gpu_scanout_acquire()==1 && fg_scanout_copy==0 && fuxi_gpu_scanout_publish()==2 && fg_scanout_ready==0 && !fuxi_gpu_scanout_offer_fate());
 assert(fuxi_gpu_scanout_acquire()==1 && fg_scanout_copy==2);
 assert(fuxi_gpu_scanout_publish()==2 && fuxi_gpu_scanout_offer_fate()==3 && fg_scanout_fifo_q && fg_scanout_fifo_buf==2);
 assert(fg_scanout_ready==0 && fg_scanout_back_ready && !fg_scanout_ready_drops && fg_scanout_fifo_st[0]==1);
 assert(!fuxi_gpu_scanout_acquire() && fg_scanout_fifo_st[3]==1); // no free buffer: refused, bounded to one
 ctl[0x1c/4]=0;intf[0x2b0/4]=2400;intf[0x298/4]+=1u<<16;unsigned s0=starts;
 assert(fuxi_gpu_scanout_retry()==1 && starts==s0+1 && fg_scanout_front==0); // START of the waiting frame promotes the queued one
 assert(!fg_scanout_fifo_q && fg_scanout_ready==2 && fg_scanout_back_ready && fg_scanout_writable && fg_scanout_copy==1 && fg_scanout_fifo_st[1]==1);
 ctl[0x1c/4]=0;intf[0x298/4]+=1u<<16;assert(fuxi_gpu_scanout_retry()==1 && fg_scanout_front==2 && !fg_scanout_back_ready && starts==s0+2);
 // Ready for a panel period or longer: replaced as before (counted).
 ctl[0x1c/4]=1;intf[0x2b0/4]=100;
 assert(fuxi_gpu_scanout_acquire()==1 && fuxi_gpu_scanout_publish()==2 && fg_scanout_back_ready);unsigned r=fg_scanout_ready;
 ticks+=fg_scanout_period_us;
 assert(fuxi_gpu_scanout_acquire()==1 && fuxi_gpu_scanout_publish()==2 && fuxi_gpu_scanout_offer_fate()==2 && !fg_scanout_fifo_q);
 assert(fg_scanout_ready!=r && fg_scanout_ready_drops==1 && fg_scanout_fifo_st[5]==1);
 // The offer itself STARTs the waiting frame (fate 4): the queued copy is promoted and waits.
 ctl[0x1c/4]=0;intf[0x2b0/4]=2400;intf[0x298/4]+=1u<<16;r=fg_scanout_ready;s0=starts;
 assert(fuxi_gpu_scanout_acquire()==1);unsigned q=fg_scanout_copy;
 assert(fuxi_gpu_scanout_publish()==2 && fuxi_gpu_scanout_offer_fate()==4 && starts==s0+1 && fg_scanout_front==r);
 assert(fg_scanout_ready==q && fg_scanout_back_ready && !fg_scanout_fifo_q && fg_scanout_fifo_st[0]==2 && fg_scanout_fifo_st[1]==2);
 // A frame still queued at stop is dropped with it.
 ctl[0x1c/4]=1;intf[0x2b0/4]=100;
 assert(fuxi_gpu_scanout_acquire()==1 && fuxi_gpu_scanout_publish()==2 && fuxi_gpu_scanout_offer_fate()==3 && fg_scanout_fifo_q);
 assert(fg_scanout_stop("fifo test")==-1 && !fg_scanout_fifo_q && fg_scanout_fifo_st[2]==1);
 modelEarly=0;ctl[0x1c/4]=1;intf[0x2b0/4]=2400;
 puts("PASS: present FIFO (fuxiearly bit 4): triple only, queue behind a fresh waiting frame, full refusal, promotion at START, stale replaced, offer-STARTed, stop drop");
}
static void triple_tests(uint8_t *panel) {
 for(unsigned d=0;d<3;d++)for(unsigned t=0;t<3;t++) {
  reset();modelDouble=d;modelTriple=t;fuxi_gpu_scanout_configure((uintptr_t)panel,1,0);
  assert(fg_scanout_triple_requested==(d==1 && t==1) && parseDouble==1 && parseTriple==1);
 }
 reset();modelDouble=modelTriple=1;failCMap=1;fuxi_gpu_scanout_configure((uintptr_t)panel,1,0);
 assert(!fuxi_gpu_scanout_acquire());ticks+=20000;assert(fuxi_gpu_scanout_acquire()==-1 && !starts);
 triple_setup(panel);assert(fg_scanout_copy==1 && fuxi_gpu_scanout_publish()==1 && fg_scanout_front==1);
 // B transfers: ready A, then copy into C without waiting for A's START.
 ctl[0x1c/4]=1;intf[0x2b0/4]=100;
 assert(fuxi_gpu_scanout_acquire()==1 && fg_scanout_copy==0);
 assert(fuxi_gpu_scanout_publish()==2 && fg_scanout_ready==0 && !fg_scanout_ready_drops && !fuxi_gpu_scanout_offer_fate());
 assert(fuxi_gpu_scanout_acquire()==1 && fg_scanout_copy==2 && fg_scanout_front==1);
 fg_busy=1;assert(!fuxi_gpu_scanout_retry() && fuxi_gpu_scanout_publish()==-1 && starts==1);
 fg_busy=0;assert(fuxi_gpu_scanout_publish()==2 && fg_scanout_ready==2 && fg_scanout_ready_drops==1 && fuxi_gpu_scanout_offer_fate()==2); // A replaced unstarted
 assert(fuxi_gpu_scanout_acquire()==1 && fg_scanout_copy==0); // replaced A is free again
 assert(fuxi_gpu_scanout_retry()==2);intf[0x298/4]+=1u<<16;
 assert(fuxi_gpu_scanout_retry()==2);ctl[0x1c/4]=0;assert(fuxi_gpu_scanout_retry()==2);
 intf[0x2b0/4]=2400;unsigned before=traceCount;
 assert(fuxi_gpu_scanout_retry()==1 && fg_scanout_front==2 && starts==2);
 assert(trace[before].pa==0xae05014 && trace[before].value==0xb9400000u);
 assert(trace[before+1].pa==0xae16018 && trace[before+2].pa==0xae1601c);
 // Exercise every front/ready combination, replacement, and pixel oracle.
 for(unsigned n=0;n<12;n++) {
  assert(fuxi_gpu_scanout_acquire()==1);unsigned target=fg_scanout_copy;
  assert(target!=fg_scanout_front);
  fg_scanout_source=(uintptr_t)(target ? panel : modelB);fg_scanout_pitch=4320;fg_scanout_sx=fg_scanout_sy=fg_scanout_dx=fg_scanout_dy=0;
  fg_scanout_w=1080;fg_scanout_h=2400;fg_scanout_checks=4;fg_scanout_checked=0;
  memset(panel,0x51,sizeof(modelB));memset(modelB,0x51,sizeof(modelB));memset(modelC,0x51,sizeof(modelC));
  assert(fg_scanout_verify());((uint8_t *)fg_scanout_pixels_at(target))[0]^=1;assert(!fg_scanout_verify());
  assert(fuxi_gpu_scanout_publish()==2);
  assert(fuxi_gpu_scanout_acquire()==1 && fg_scanout_copy!=fg_scanout_front && fg_scanout_copy!=target);
  assert(fuxi_gpu_scanout_publish()==2 && fg_scanout_ready_drops==n+2);
  ctl[0x1c/4]=0;intf[0x2b0/4]=2400;intf[0x298/4]+=1u<<16;
  assert(fuxi_gpu_scanout_retry()==1);
 }
 assert(fuxi_gpu_scanout_acquire()==1 && fg_scanout_copy==0);assert(fuxi_gpu_scanout_publish()==2);
 ctl[0x1c/4]=0;intf[0x298/4]+=1u<<16;assert(fuxi_gpu_scanout_retry()==1);
 // fuxigpuncimport 32768: TE-referenced spin. The model predicts the edge that starts the transfer and the thread call wakes shortly before done.
 {
  unsigned keepStarts=fg_scanout_starts,keepMatch=fg_scanout_current_match,keepMismatch=fg_scanout_current_mismatch,keepTe=fg_scanout_te_frames,keepCount=intf[0x298/4];
  unsigned *st=fg_scanout_spin_stat;
  fg_abi_spin=1;fg_scanout_panel120_active=1;fg_scanout_period_us=8333;fg_scanout_spin_busy=0;spinKicks=0;memset(st,0,sizeof(fg_scanout_spin_stat));
  fg_scanout_ph_ok=0;fg_scanout_ph_p=0;fg_scanout_ph_done_us=0;fg_scanout_spin_err_sum=fg_scanout_spin_err_max=fg_scanout_spin_late_sum=fg_scanout_spin_late_max=0;
  fg_scanout_spin_err_n=fg_scanout_spin_late_n=0;
  // No model yet: one sync wake now, from the START's count.
  uint64_t T=ticks+100;ticks=T;fg_scanout_last_start=T;fg_scanout_last_count=5;
  fg_scanout_spin_kick(1);
  assert(spinKicks==1 && fg_scanout_spin_busy && fg_scanout_spin_sync && spinDeadline==T && fg_scanout_ph_p==8333 && fg_scanout_ph_done_us==7950);
  fg_scanout_spin_kick(1);assert(spinKicks==1); // one pending call
  phE=T+1000;phC=5;phHold=0;probeScript=phase_script;phase_script();
  fg_scanout_spin_run(0,0);probeScript=0;
  assert(st[FG_SPIN_SYNCS]==1 && fg_scanout_ph_ok && fg_scanout_ph_c==6 && fg_scanout_ph_t>=T+1000 && fg_scanout_ph_t<=T+1004);
  assert(spinKicks==2 && fg_scanout_spin_busy && !fg_scanout_spin_sync && fg_scanout_ph_e==fg_scanout_ph_t && spinDeadline==fg_scanout_ph_t+fg_scanout_ph_done_us-fg_scanout_spin_lead_us());
  // A waiting back buffer is STARTed at the first idle sample after the edge and the active line; the next edge resyncs the model and the chain re-arms.
  assert(fuxi_gpu_scanout_acquire()==1);ctl[0x1c/4]=1;intf[0x2b0/4]=100;assert(fuxi_gpu_scanout_publish()==2 && fg_scanout_back_ready);
  uint64_t E=fg_scanout_ph_t+8333;unsigned s0=starts;
  spinKicks=0;phase_run(E,6,0,7550);
  assert(starts==s0+1 && !fg_scanout_back_ready && fg_scanout_async_published && st[FG_SPIN_PUBLISHED]==1 && st[FG_SPIN_HIT]==1 && !st[FG_SPIN_LATE]);
  assert(fg_scanout_spin_err_n==1 && fg_scanout_spin_err_max<=6 && fg_scanout_ph_c==8 && fg_scanout_ph_t>=E+8333 && fg_scanout_ph_t<=E+8339);
  assert(fg_scanout_ph_done_us>=7955 && fg_scanout_ph_done_us<=7975 && fg_scanout_ph_p>=8300 && fg_scanout_ph_p<=8340);
  assert(spinKicks==0 && st[FG_SPIN_SKIPPED]==1 && !fg_scanout_spin_busy); // no frame queued: nothing armed
  assert(fuxi_gpu_scanout_retry()==1 && !fg_scanout_async_published && fuxi_gpu_scanout_retry()==0);
  // No back buffer at done: nothing is published, the edge still resyncs, and nothing is armed (the display path's next START re-arms).
  fg_scanout_spin_busy=0;spinKicks=0;E=fg_scanout_ph_t+8333;s0=starts;
  phase_run(E,8,0,7550);
  assert(starts==s0 && st[FG_SPIN_NO_BACK]==1 && st[FG_SPIN_PUBLISHED]==1 && fg_scanout_ph_c==10 && !spinKicks && !fg_scanout_spin_busy);
  // The wake came after transfer-done (W176: thread call ~0.5 ms late): the line is never seen active, but an edge, idle and the minimum transfer time suffice.
  assert(fuxi_gpu_scanout_acquire()==1);ctl[0x1c/4]=1;intf[0x2b0/4]=100;assert(fuxi_gpu_scanout_publish()==2 && fg_scanout_back_ready);
  fg_scanout_spin_busy=0;spinKicks=0;E=fg_scanout_ph_t+8333;s0=starts;unsigned done0=fg_scanout_ph_done_us;
  phase_run(E,10,0,8100);
  assert(starts==s0+1 && st[FG_SPIN_PUBLISHED]==2 && st[FG_SPIN_HIT]==2 && fg_scanout_ph_done_us==done0 && !st[FG_SPIN_NO_ACTIVE]);
  assert(fuxi_gpu_scanout_retry()==1);

  // Another START intervened (display path won the race): publish nothing, arm the newer START.
  assert(fuxi_gpu_scanout_acquire()==1);ctl[0x1c/4]=1;intf[0x2b0/4]=100;assert(fuxi_gpu_scanout_publish()==2);
  E=fg_scanout_ph_t+8333;s0=starts;spinKicks=0;
  fg_scanout_spin_busy=0;fg_scanout_spin_serial=fg_scanout_starts;
  fg_scanout_last_start=E-2000;phE=E;phC=10;phHold=0;ticks=E+7550;fg_scanout_ph_e=E;fg_scanout_spin_sync=0;fg_scanout_spin_c0=10;fg_scanout_ph_wake=ticks;fg_scanout_spin_busy=1;
  fg_scanout_starts++;s0=starts;probeScript=phase_script;phase_script();fg_scanout_spin_run(0,0);probeScript=0;
  assert(starts==s0 && fg_scanout_back_ready && st[FG_SPIN_RACED]==1 && spinKicks==1);
  // The START latch was missed (the transfer starts a period later): the line is never active before the window closes.
  fg_scanout_spin_busy=0;spinKicks=0;E=fg_scanout_ph_t+8333;s0=starts;
  phSkew=9000;phase_run(E,10,0,7550);phSkew=0;
  assert(starts==s0 && st[FG_SPIN_NO_ACTIVE]==1 && fg_scanout_back_ready);
  // The transfer never ends: give up shortly after the next TE.
  fg_scanout_spin_busy=0;E=fg_scanout_ph_t+8333;phase_run(E,10,1,7550);
  assert(starts==s0 && st[FG_SPIN_EXPIRED]==1 && ticks<=E+8333+8333*6/100+20 && !fg_scanout_spin_busy);
  // A START with a small margin to the edge (100 us): idle without an observed active line is not trusted, so a late wake publishes nothing.
  fg_scanout_spin_busy=0;spinKicks=0;fg_scanout_back_ready=0;ctl[0x1c/4]=0;intf[0x2b0/4]=2400;
  assert(fuxi_gpu_scanout_acquire()==1);ctl[0x1c/4]=1;intf[0x2b0/4]=100;assert(fuxi_gpu_scanout_publish()==2 && fg_scanout_back_ready);
  fg_scanout_spin_busy=0;spinKicks=0;E=ticks+1000;s0=starts;fg_scanout_spin_m=100;
  phase_run(E,20,0,8100);fg_scanout_spin_m=1000000;
  assert(starts==s0 && fg_scanout_back_ready && st[FG_SPIN_NO_ACTIVE]==2 && st[FG_SPIN_OFFERED]>=3);
  // The START missed the edge (the transfer starts a period later): idle in [1 ms, 7 ms) after the edge replans once for the following edge, then publishes at its done.
  fg_scanout_spin_busy=0;spinKicks=0;E=ticks+1000;s0=starts;unsigned hit0=st[FG_SPIN_HIT];
  phLine=8333;phase_run(E,20,0,6500);phLine=0;
  assert(starts==s0 && st[FG_SPIN_MISSED]==1 && fg_scanout_back_ready && spinKicks==1 && fg_scanout_spin_busy && fg_scanout_ph_e==E+fg_scanout_ph_p && fg_scanout_spin_replanned);
  fg_scanout_spin_busy=0;phE=E+8333;phC=21;phHold=0;phLine=0;ticks=E+8333+7550;fg_scanout_ph_wake=ticks;fg_scanout_spin_busy=1;fg_scanout_spin_c0=20;
  probeScript=phase_script;phase_script();fg_scanout_spin_run(0,0);probeScript=0;
  assert(starts==s0+1 && st[FG_SPIN_HIT]==hit0+1 && st[FG_SPIN_MISSED]==1);
  assert(fuxi_gpu_scanout_retry()==1);
  // A catch sample 700 us off the modelled phase does not move the model; the fourth in a row re-locks it.
  fg_scanout_back_ready=0;fg_scanout_ph_p=8333;fg_scanout_spin_rejects=0;st[FG_SPIN_PHASE_REJECT]=0;
  for(unsigned i=0;i<4;i++){
   fg_scanout_spin_busy=0;E=ticks+1000;fg_scanout_ph_t=E+8333-700;uint64_t before=fg_scanout_ph_t;
   phase_run(E,30,0,7550);
   if(i<3)assert(st[FG_SPIN_PHASE_REJECT]==i+1 && fg_scanout_ph_t==before && fg_scanout_spin_rejects==i+1);
   else assert(st[FG_SPIN_PHASE_REJECT]==3 && fg_scanout_ph_t!=before && !fg_scanout_spin_rejects);
  }
  // Y178 stall: the spin thread must not START while the display path holds the writable token for a copy (acquire ... offer); a stolen token is restored.
  fg_scanout_spin_busy=0;spinKicks=0;fg_scanout_back_ready=0;ctl[0x1c/4]=0;intf[0x2b0/4]=2400;ticks+=20000;
  assert(fuxi_gpu_scanout_acquire()==1);ctl[0x1c/4]=1;intf[0x2b0/4]=100;assert(fuxi_gpu_scanout_publish()==2 && fg_scanout_back_ready && !fg_scanout_copy_open);
  ctl[0x1c/4]=0;intf[0x2b0/4]=2400;ticks+=20000;assert(fuxi_gpu_scanout_acquire()==1 && fg_scanout_copy_open);
  fg_scanout_spin_busy=0;spinKicks=0;E=ticks+1000;s0=starts;st[FG_SPIN_COPY_OPEN]=0;
  phase_run(E,40,0,7550);
  assert(starts==s0 && fg_scanout_back_ready && st[FG_SPIN_COPY_OPEN]==1 && !fg_scanout_async_published && fg_scanout_writable);
  {
   unsigned r0=st[FG_SPIN_REFUSED],u0=st[FG_SPIN_UNSTOLEN];
   // The token was consumed anyway (stolen): the offer restores it instead of returning -1 (which would hold scanout for good).
   fg_scanout_writable=0;fg_scanout_spin_stolen=1;ctl[0x1c/4]=1;intf[0x2b0/4]=100;
   assert(fuxi_gpu_scanout_publish()==2 && st[FG_SPIN_UNSTOLEN]==u0+1 && st[FG_SPIN_REFUSED]==r0 && fg_scanout_writable && !fg_scanout_spin_stolen && !fg_scanout_copy_open);
   // A token lost for any other reason is still refused, and the refusal is logged.
   fg_scanout_writable=0;assert(fuxi_gpu_scanout_publish()==-1 && st[FG_SPIN_REFUSED]==r0+1);
   fg_scanout_writable=1;
  }
  // Watchdog: a frame waiting more than 4 periods is counted and logged by the display retry; retry still tries to START it.
  fg_scanout_back_ready=1;fg_scanout_ready_at=ticks-100000;st[FG_SPIN_STALL]=0;fg_scanout_async_published=0;ctl[0x1c/4]=1;intf[0x2b0/4]=100;
  assert(fuxi_gpu_scanout_retry()==2 && st[FG_SPIN_STALL]==1);
  // Arrival arming: a good model arms only when a frame is waiting; a START too close to the edge is not trusted; a finished transfer is not armed.
  fg_scanout_spin_busy=0;spinKicks=0;st[FG_SPIN_SKIPPED]=st[FG_SPIN_ARRIVAL]=st[FG_SPIN_NEAR_EDGE]=0;
  fg_scanout_ph_ok=1;fg_scanout_ph_p=8333;fg_scanout_ph_t=ticks-1000;fg_scanout_last_start=ticks-500;fg_scanout_back_ready=0;
  fg_scanout_spin_kick(1);assert(st[FG_SPIN_SKIPPED]==1 && !spinKicks);
  fg_scanout_spin_kick(0);assert(st[FG_SPIN_SKIPPED]==1 && !spinKicks);
  fg_scanout_back_ready=1;fg_scanout_spin_kick(0);
  assert(spinKicks==1 && st[FG_SPIN_ARRIVAL]==1 && fg_scanout_spin_busy && !fg_scanout_spin_sync && fg_scanout_ph_e==fg_scanout_ph_t+8333 && fg_scanout_spin_serial==fg_scanout_starts);
  st[FG_SPIN_ARR_BUSY]=0;fg_scanout_spin_kick(0);assert(st[FG_SPIN_ARR_BUSY]==1 && spinKicks==1);
  fg_scanout_spin_busy=0;spinKicks=0;fg_scanout_last_start=fg_scanout_ph_t+8333-30;fg_scanout_spin_kick(1);assert(st[FG_SPIN_NEAR_EDGE]==1 && !spinKicks);
  fg_scanout_ph_t=ticks-20000;fg_scanout_last_start=ticks-30000;st[FG_SPIN_ARR_PAST]=0;fg_scanout_spin_kick(0);assert(!spinKicks && st[FG_SPIN_ARR_PAST]==1);
  // Stale model: sync again. A sync that sees no edge fails and arms nothing.
  fg_scanout_ph_t=ticks-64*8333-1;fg_scanout_spin_busy=0;spinKicks=0;fg_scanout_last_start=ticks;fg_scanout_last_count=intf[0x298/4]>>16;
  fg_scanout_spin_kick(1);assert(spinKicks==1 && fg_scanout_spin_sync);
  phE=ticks+100000;phC=intf[0x298/4]>>16;phHold=0;probeScript=phase_script;uint64_t t0_=ticks;fg_scanout_spin_c0=phC;fg_scanout_spin_serial=fg_scanout_starts;
  fg_scanout_spin_run(0,0);probeScript=0;
  assert(st[FG_SPIN_SYNC_FAIL]==1 && spinKicks==1 && ticks-t0_>=8333 && ticks-t0_<=8333*5/4+10 && !fg_scanout_spin_busy);
  // Off by default and outside panel120.
  fg_abi_spin=0;spinKicks=0;fg_scanout_spin_kick(1);assert(!spinKicks);
  fg_abi_spin=1;fg_scanout_panel120_active=0;fg_scanout_spin_kick(1);assert(!spinKicks);
  fg_abi_spin=0;fg_scanout_ph_ok=0;fg_scanout_ph_p=0;fg_scanout_ph_done_us=0;ctl[0x1c/4]=1;intf[0x2b0/4]=2400;intf[0x298/4]=keepCount;
  fg_scanout_back_ready=0;fg_scanout_starts=keepStarts;fg_scanout_current_match=keepMatch;fg_scanout_current_mismatch=keepMismatch;fg_scanout_te_frames=keepTe;fg_scanout_period_us=16667;
 }
 assert(fg_scanout_buffer_flips[0] && fg_scanout_buffer_flips[1] && fg_scanout_buffer_flips[2] && parseDouble==1 && parseTriple==1);
 unsigned flips=fg_scanout_flips,drops=fg_scanout_ready_drops;
 assert(fuxi_gpu_scanout_acquire()==1);assert(fuxi_gpu_scanout_publish()==2);
 ctl[0x1c/4]=0;intf[0x298/4]+=1u<<16;fg_scanout_starts=599;
 sspp[0xa4/4]=fg_scanout_pa(fg_scanout_ready);
 assert(fuxi_gpu_scanout_retry()==1 && fg_scanout_current_match==1);
 char expected[80];snprintf(expected,sizeof(expected),"flips=%u back_ready_drops=%u",flips+1,drops);
 assert(strstr(doubleStats,expected) && strstr(doubleStats," triple=1 flips_a=") && strstr(doubleStats," flips_b=") && strstr(doubleStats," flips_c=") && strstr(doubleStats," drops="));
 assert(!fg_scanout_flips && !fg_scanout_ready_drops && !fg_scanout_buffer_flips[0] && !fg_scanout_buffer_flips[1] && !fg_scanout_buffer_flips[2]);
 // CPU preserves the newest ready image, including C, only after transfer drain.
 for(unsigned front=0;front<3;front++)for(unsigned ready=0;ready<4;ready++)if(front!=ready) {
  triple_setup(panel);fg_scanout_front=front;sspp[0x14/4]=fg_scanout_pa(front);
  fg_scanout_ready=ready;fg_scanout_back_ready=ready<3;
  memset((void *)fg_scanout_pixels_at(ready<3?ready:front),0x73,sizeof(modelB));
  fg_busy=1;assert(!fuxi_gpu_scanout_cpu_acquire());fg_busy=0;
  ctl[0x1c/4]=1;assert(!fuxi_gpu_scanout_cpu_acquire());ctl[0x1c/4]=0;
  assert(fuxi_gpu_scanout_cpu_acquire()==1 && panel[0]==0x73 && panel[sizeof(modelB)-1]==0x73);
  assert(!fuxi_gpu_scanout_triple() && !fuxi_gpu_scanout_pending() && sspp[0x14/4]==0xb8000000u);
  assert(fuxi_gpu_scanout_publish()==1);assert(fuxi_gpu_scanout_acquire()==1 && fuxi_gpu_scanout_triple() && !fg_scanout_front);
 }
 triple_setup(panel);fg_scanout_front=2;sspp[0x14/4]=0xb9400000u;
 unsigned saved=starts;assert(fg_scanout_stop("triple stop")==-1);
 assert(sspp[0x14/4]==0xb8000000u && starts==saved && fuxi_gpu_scanout_acquire()==-1);
 puts("PASS: triple opt-in, C mapping/init, free/ready/front rotation, newest-ready replacement at retire, fence/TE/transfer/flush/START order, pixel oracle, CPU preservation/re-entry and stop");
}
static void double_tests(uint8_t *panel) {
 for(unsigned option=0;option<3;option++) {
  reset();modelDouble=option;fuxi_gpu_scanout_configure((uintptr_t)panel,1,0);
  assert(fg_scanout_double_requested==(option==1));
 }
 reset();modelDouble=1;fuxi_gpu_scanout_configure((uintptr_t)panel,0,0);
 assert(!fg_scanout_double_requested && !fuxi_gpu_scanout_double() && !reads && !writes);
 // The production oracle must read back the GPU's back buffer, never front.
 double_setup(panel);fg_scanout_checks=4;fg_scanout_source=(uintptr_t)panel;
 fg_scanout_pitch=4320;fg_scanout_sx=fg_scanout_sy=fg_scanout_dx=fg_scanout_dy=0;
 fg_scanout_w=1080;fg_scanout_h=2400;
 assert(fg_scanout_verify());modelB[sizeof(modelB)-1]^=1;assert(!fg_scanout_verify());
 modelB[sizeof(modelB)-1]^=1;fg_scanout_front=1;fg_scanout_source=(uintptr_t)modelB;
 assert(fg_scanout_verify());panel[0]^=1;assert(!fg_scanout_verify());
 double_setup(panel);ctl[0x18/4]=0x80000800u;
 fg_busy=1;assert(fuxi_gpu_scanout_publish()==-1 && !fuxi_gpu_scanout_pending() && !starts);
 fg_busy=0;unsigned t=traceCount;
 assert(fuxi_gpu_scanout_publish()==1 && starts==1 && fg_scanout_front==1);
 assert(trace[t].pa==0xae05014 && trace[t].value==0xb8a00000u);
 assert(trace[t+1].pa==0xae16018 && trace[t+1].value==0x80000801u);
 assert(trace[t+2].pa==0xae1601c && trace[t+2].value==1);
 // Copy acquisition overlaps an active transfer. Publication waits for BOTH
 // transfer indicators and a later TE; ring/copy busy can never issue START.
 intf[0x2b0/4]=100;assert(fuxi_gpu_scanout_acquire()==1);
 fg_busy=1;assert(!fuxi_gpu_scanout_retry() && fuxi_gpu_scanout_publish()==-1 && starts==1);
 fg_busy=0;assert(fuxi_gpu_scanout_publish()==2 && fuxi_gpu_scanout_pending());
 assert(fuxi_gpu_scanout_retry()==2 && starts==1); // same TE
 assert(!fuxi_gpu_scanout_acquire() && fuxi_gpu_scanout_pending()); // do not overwrite the ready back
 intf[0x298/4]=(3u<<16)|2500;
 assert(fuxi_gpu_scanout_retry()==2 && starts==1); // START busy
 ctl[0x1c/4]=0;assert(fuxi_gpu_scanout_retry()==2 && starts==1); // write-line busy
 intf[0x2b0/4]=2400;t=traceCount;
 assert(fuxi_gpu_scanout_retry()==1 && starts==2 && !fg_scanout_front && !fuxi_gpu_scanout_pending());
 assert(trace[t].pa==0xae05014 && trace[t].value==0xb8000000u);
 assert(trace[t+1].pa==0xae16018 && trace[t+2].pa==0xae1601c);
 struct fg_present_times stamps;fuxi_gpu_scanout_stamps(&stamps);
 assert(stamps.start==mach_absolute_time() && stamps.previous_start && stamps.previous_start<=stamps.start);
 // Complete copies may be ready with no dirty swaps/subscribers: timer has
 // an independent pending query and a retry does not invent another frame.
 assert(!fuxi_gpu_scanout_retry() && starts==2 && fg_scanout_retries>=4);
 // Sample CURRENT_SRC0 read-only, at the 600-START cadence. It is unverified:
 // mismatches count without driving ownership decisions.
 for(unsigned i=0;i<2;i++) {
  ctl[0x1c/4]=0;intf[0x298/4]+=1u<<16;
  assert(fuxi_gpu_scanout_acquire()==1);fg_scanout_starts=599;
  sspp[0xa4/4]=i?0:0xb8a00000u;
  assert(fuxi_gpu_scanout_publish()==1);
 }
 assert(fg_scanout_current_match==1 && fg_scanout_current_mismatch==1);
 for(unsigned i=0;i<traceCount;i++)assert(trace[i].pa!=0xae050a4);
 // CPU fallback and black fill must first drain, keep fPixels=A and restore
 // its complete image even when front=B. The next GPU frame re-enters double
 // mode with A as the front, without waiting for A's transfer (B is idle).
 double_setup(panel);assert(fuxi_gpu_scanout_publish()==1);
 memset(modelB,0x39,sizeof(modelB));memset(panel,0xcc,4320u*2400);
 assert(!fuxi_gpu_scanout_cpu_acquire() && panel[0]==0xcc && fg_scanout_double_active);
 ctl[0x1c/4]=0;intf[0x298/4]+=1u<<16;intf[0x2b0/4]=100;
 assert(!fuxi_gpu_scanout_cpu_acquire() && panel[0]==0xcc);
 intf[0x2b0/4]=2400;fg_busy=1;assert(!fuxi_gpu_scanout_cpu_acquire());fg_busy=0;
 assert(fuxi_gpu_scanout_cpu_acquire()==1 && !fuxi_gpu_scanout_double() && !memcmp(panel,modelB,sizeof(modelB)));
 assert(sspp[0x14/4]==0xb8000000u && (ctl[0x18/4]&1));
 memset(panel,0,4320u*2400);assert(fuxi_gpu_scanout_publish()==1);
 assert(fuxi_gpu_scanout_acquire()==1 && fuxi_gpu_scanout_double() && !fg_scanout_front && sspp[0x14/4]==0xb8000000u);
 assert(fuxi_gpu_scanout_publish()==2);ctl[0x1c/4]=0;intf[0x2b0/4]=2400;intf[0x298/4]+=1u<<16;
 assert(fuxi_gpu_scanout_retry()==1 && fg_scanout_front && sspp[0x14/4]==0xb8a00000u);
 // Preserve a newer ready image, whether it is A or B, before a CPU
 // partial update discards that unstarted frame.
 for(unsigned front=0;front<2;front++) {
  double_setup(panel);
  if(front) {assert(fuxi_gpu_scanout_publish()==1);ctl[0x1c/4]=0;intf[0x298/4]+=1u<<16;}
  assert(fuxi_gpu_scanout_acquire()==1);
  memset(panel,front?0x77:0x11,4320u*2400);memset(modelB,front?0x11:0x77,sizeof(modelB));
  fg_scanout_back_ready=1;
  assert(fuxi_gpu_scanout_cpu_acquire()==1 && panel[0]==0x77 && panel[4320u*2400-1]==0x77);
  assert(!fuxi_gpu_scanout_pending() && fg_scanout_ready_drops==1);
 }
 // Supplied 120.8Hz/7.3ms transfer: copy 626..1246us into back overlaps
 // the transfer. Each next TE can START, without waiting to begin the copy.
 double_setup(panel);fg_scanout_panel120_active=1;fg_scanout_period_us=8333;
 assert(fuxi_gpu_scanout_publish()==1);
 for(unsigned frame=1;frame<600;frame++) {
  uint64_t began=ticks;intf[0x2b0/4]=100;
  assert(fuxi_gpu_scanout_acquire()==1);
  fg_busy=1;assert(fuxi_gpu_scanout_publish()==-1 && !fuxi_gpu_scanout_retry());
  ticks=began+(frame&1?626:1246);fg_busy=0;
  assert(fuxi_gpu_scanout_publish()==2);
  ticks=began+7300;ctl[0x1c/4]=0;intf[0x2b0/4]=2400;
  assert(fuxi_gpu_scanout_retry()==2); // transfer done, still same TE
  ticks=began+8274;intf[0x298/4]+=1u<<16;
  sspp[0xa4/4]=fg_scanout_front?0xb8000000u:0xb8a00000u;
  assert(fuxi_gpu_scanout_retry()==1);
  if(frame==598)assert(fg_scanout_iv[0]==598 && !fg_scanout_iv[1] && !fg_scanout_iv[2] && !fg_scanout_iv[3]);
 }
 assert(starts==600 && fg_scanout_current_match==1 && !fg_scanout_failed);
 // Every new register access failure holds writes; stop restores A shadow
 // before any future console handoff, with no speculative START.
 const unsigned failures[]={0xae05014,0xae16018,0xae050a4};
 for(unsigned i=0;i<3;i++) {
  double_setup(panel);assert(fuxi_gpu_scanout_publish()==1);
  ctl[0x1c/4]=0;intf[0x298/4]+=1u<<16;assert(fuxi_gpu_scanout_acquire()==1);
  if(i==2)fg_scanout_starts=599;
  if(i==0)failWriteAddress=failures[i];else failAddress=failures[i];
  assert(fuxi_gpu_scanout_publish()==-1 && fg_scanout_failed && !fg_scanout_writable && !fuxi_gpu_scanout_pending());
  if(i==2)assert(sspp[0x14/4]==0xb8000000u && (ctl[0x18/4]&1));
 }
 double_setup(panel);assert(fuxi_gpu_scanout_publish()==1);sspp[0x14/4]=0x12340000u;
 assert(fuxi_gpu_scanout_acquire()==-1 && sspp[0x14/4]==0xb8000000u && starts==1);
 double_setup(panel);assert(fuxi_gpu_scanout_publish()==1);failAddress=0xae05014;
 assert(fuxi_gpu_scanout_acquire()==-1 && !fg_scanout_writable);
 double_setup(panel);assert(fuxi_gpu_scanout_publish()==1);
 fuxi_gpu_scanout_configure((uintptr_t)panel,0,0);
 assert(fg_scanout_owned && fg_scanout_failed && sspp[0x14/4]==0xb8000000u && (ctl[0x18/4]&1));
 assert(fuxi_gpu_scanout_acquire()==-1 && !fuxi_gpu_scanout_pending() && starts==1);
 double_setup(panel);assert(fuxi_gpu_scanout_publish()==1);t=traceCount;
 assert(fg_scanout_stop("modeled stop")==-1);
 assert(trace[t].pa==0xae05014 && trace[t].value==0xb8000000u);
 assert(trace[t+1].pa==0xae16018 && starts==1 && fuxi_gpu_scanout_acquire()==-1);
 double_setup(panel);assert(fuxi_gpu_scanout_publish()==1);failAddress=0xae36298;
 struct fg_te_observer failedTe={0};
 assert(!fuxi_gpu_scanout_te(&failedTe) && fg_scanout_failed && sspp[0x14/4]==0xb8000000u);
 reset();modelDouble=1;sspp[0x14/4]=0xb8a00000u;
 fuxi_gpu_scanout_configure((uintptr_t)panel,1,0);assert(fuxi_gpu_scanout_acquire()==-1 && !starts);
 assert(fg_scanout_owned && sspp[0x14/4]==0xb8000000u && (ctl[0x18/4]&1));
 reset();modelDouble=1;failAddress=0xae05014;
 fuxi_gpu_scanout_configure((uintptr_t)panel,1,0);assert(fuxi_gpu_scanout_acquire()==-1 && !starts);
 reset();modelDouble=1;failBMap=1;fuxi_gpu_scanout_configure((uintptr_t)panel,1,0);
 assert(!fuxi_gpu_scanout_acquire());ticks+=20000;assert(fuxi_gpu_scanout_acquire()==-1 && !starts);
 puts("PASS: actual double ownership, initialization, overlap, deferred publish/TE/retirement, SRC0/flush/START order, sampling, CPU fallback and stop restoration");
}
static void mid_tests(uint8_t *panel) {
 triple_setup(panel);
 // fuxigpuncimport 65536: START anywhere in the tear-safe part of the period; the hardware start threshold is widened and the low part blocked in software.
 {
  unsigned keepP=fg_scanout_panel120_active,keepInit=fg_scanout_te_init,keepStarts=fg_scanout_starts,keepMatch=fg_scanout_current_match,keepMismatch=fg_scanout_current_mismatch,keepTe=fg_scanout_te_frames,keepCount=intf[0x298/4];
  fg_abi_mid=1;fg_scanout_panel120_active=1;fg_scanout_period_us=8333;fg_scanout_te_init=0x960;fg_scanout_mid_state=0;memset(fg_scanout_mid_vimm,0,sizeof(fg_scanout_mid_vimm));memset(fg_scanout_mid_vheld,0,sizeof(fg_scanout_mid_vheld));fg_scanout_mid_wait=fg_scanout_mid_forced=0;intf[0x284/4]=1;intf[0x2a0/4]=0x960;dsi[0x084/4]=0x80000004u;dsiTrigAnytime=1;
  memset(fg_scanout_mid_hist,0,sizeof(fg_scanout_mid_hist));memset(fg_scanout_mid_g1,0,sizeof(fg_scanout_mid_g1));fg_scanout_mid_block_t=0;
  intf[0x29c/4]=0x40004;intf[0x298/4]=(7u<<16)|0x960;
  fg_scanout_mid_setup();
  assert(fg_scanout_mid_state==1 && fg_scanout_mid_var==4 && fg_scanout_mid_old==0x40004 && intf[0x29c/4]==0x40004);
  #define SINCE(n) (intf[0x298/4]=(7u<<16)|(0x960u+(n)))
  SINCE(3);assert(fg_scanout_mid_allow()==1);SINCE(0);assert(fg_scanout_mid_allow()==1);
  SINCE(160);assert(fg_scanout_mid_allow()==1);SINCE(2420);assert(fg_scanout_mid_allow()==1);SINCE(4500);assert(fg_scanout_mid_allow()==1);
  SINCE(100);assert(fg_scanout_mid_allow()==0 && fg_scanout_mid_wait==1);SINCE(2421);assert(fg_scanout_mid_allow()==0 && fg_scanout_mid_wait==2);
  ticks+=2*8333+10;assert(fg_scanout_mid_allow()==1 && fg_scanout_mid_forced==1); // never blocks for more than two periods
  SINCE(1000);assert(fg_scanout_mid_allow()==1 && !fg_scanout_mid_block_t);
  // A START in the blocked part is refused before any register is written; in the window it is issued and verified.
  unsigned te=10;
  #define START_AT(n) (te++,ticks+=20000,ctl[0x1c/4]=0,intf[0x2b0/4]=2400,fg_scanout_writable=1,intf[0x298/4]=(te<<16)|(0x960u+(n)))
  START_AT(100);assert(fuxi_gpu_scanout_publish()==2 && fg_scanout_mid_wait>=3);
  START_AT(1000);base=ticks;probeScript=mid_script;
  assert(fuxi_gpu_scanout_publish()==1);probeScript=0;
  assert(fg_scanout_mid_vimm[4]==1 && fg_scanout_mid_vheld[4]==0 && dsi[0x084/4]==0x80000004u && fg_scanout_mid_hist[3]==1 && fg_scanout_mid_state==1);
  // dsi_te variant: TRIG_CTRL bit 31 is cleared for the START and restored afterwards.
  START_AT(1000);assert(fg_scanout_mid_allow()==1 && dsi[0x084/4]==4 && fg_scanout_mid_dirty);
  assert(fuxi_gpu_scanout_publish()==1 && dsi[0x084/4]==0x80000004u && !fg_scanout_mid_dirty);
  // The hardware keeps waiting for the TE (line does not go active): four such results restore the threshold and move to the next variant.
  for(unsigned i=0;i<3;i++){START_AT(1000);assert(fuxi_gpu_scanout_publish()==1);}
  assert(fg_scanout_mid_vheld[4]==4 && fg_scanout_mid_var==0 && fg_scanout_mid_state==1 && intf[0x29c/4]==(2420u<<16|4));
  for(unsigned i=0;i<4;i++){START_AT(1000);assert(fuxi_gpu_scanout_publish()==1);}
  assert(fg_scanout_mid_vheld[0]==4 && fg_scanout_mid_var==1 && fg_scanout_mid_state==1 && intf[0x29c/4]==(2420u<<16|2420));
  for(unsigned i=0;i<4;i++){START_AT(1000);assert(fuxi_gpu_scanout_publish()==1);}
  assert(fg_scanout_mid_vheld[1]==4 && fg_scanout_mid_var==2 && intf[0x29c/4]==0x40004);
  // start_pos variant: set just past the read line for the START, restored after it.
  START_AT(1000);assert(fg_scanout_mid_allow()==1 && intf[0x2a0/4]==((0x960u+1000+8)&0xffffu) && fg_scanout_mid_dirty);
  assert(fuxi_gpu_scanout_publish()==1 && intf[0x2a0/4]==0x960 && !fg_scanout_mid_dirty && fg_scanout_mid_vheld[2]==1);
  probeScript=ctr_script; // the read counter keeps running with the tear check disabled
  for(unsigned i=0;i<3;i++){START_AT(1000);assert(fuxi_gpu_scanout_publish()==1);}
  assert(fg_scanout_mid_vheld[2]==4 && fg_scanout_mid_var==3 && fg_scanout_mid_state==1 && intf[0x284/4]==1 && intf[0x2a0/4]==0x960);
  START_AT(1000);assert(fg_scanout_mid_allow()==1 && intf[0x284/4]==0 && fg_scanout_mid_dirty);
  assert(fuxi_gpu_scanout_publish()==1 && intf[0x284/4]==1 && !fg_scanout_mid_dirty);
  for(unsigned i=0;i<3;i++){START_AT(1000);assert(fuxi_gpu_scanout_publish()==1);}
  probeScript=0;
  assert(fg_scanout_mid_vheld[3]==4 && fg_scanout_mid_state==3 && intf[0x29c/4]==0x40004 && intf[0x284/4]==1 && intf[0x2a0/4]==0x960 && fg_scanout_mid_hist[3]==21);
  // A variant whose enable-cleared counter stands still is skipped without leaving the enable cleared.
  intf[0x284/4]=1;fg_scanout_mid_old=0x40004;assert(!fg_scanout_mid_apply(3) && intf[0x284/4]==1);
  #undef START_AT
  SINCE(100);assert(fg_scanout_mid_allow()==1); // off: no software block
  #undef SINCE
  dsiTrigAnytime=0;fg_abi_mid=0;fg_scanout_mid_state=0;fg_scanout_period_us=16667;fg_scanout_panel120_active=keepP;fg_scanout_te_init=keepInit;fg_scanout_back_ready=0;
  fg_scanout_starts=keepStarts;fg_scanout_current_match=keepMatch;fg_scanout_current_mismatch=keepMismatch;fg_scanout_te_frames=keepTe;intf[0x298/4]=keepCount;ctl[0x1c/4]=1;intf[0x2b0/4]=2400;
 }
}
static void ar_run(void) {fg_scanout_ar_busy=0;fg_scanout_ar_run(0,0);}
static void ar_manual_publish(unsigned te) { // a normal START at a safe line, then the monitor may arm autorefresh
 ctl[0x1c/4]=0;intf[0x2b0/4]=2400;ticks+=20000;fg_scanout_writable=1;fg_scanout_copy=1;intf[0x298/4]=(te<<16)|(0x960u+1000);
 assert(fuxi_gpu_scanout_publish()==1);ctl[0x1c/4]=0;
}
static void ar_tests(uint8_t *panel) {
 // fuxigpuncimport 131072: autorefresh scanout. Flips write SRC0 only; the latched address (0xae050a4) decides which buffers are free.
 triple_setup(panel);
 unsigned keepP=fg_scanout_panel120_active,keepInit=fg_scanout_te_init,keepStarts=fg_scanout_starts,keepMatch=fg_scanout_current_match,keepMismatch=fg_scanout_current_mismatch,keepTe=fg_scanout_te_frames,keepCount=intf[0x298/4];
 fg_abi_ar=1;fg_scanout_panel120_active=1;fg_scanout_period_us=8333;fg_scanout_te_init=0x960;fg_scanout_ar_state=0;fg_scanout_ar_confirmed=0;fg_scanout_ar_busy=0;
 memset(fg_scanout_ar_st,0,sizeof(fg_scanout_ar_st));memset(fg_scanout_ar_tpf,0,sizeof(fg_scanout_ar_tpf));memset(fg_scanout_ar_ttl,0,sizeof(fg_scanout_ar_ttl));memset(fg_scanout_ar_ll,0,sizeof(fg_scanout_ar_ll));
 fg_scanout_ar_flip_valid=0;
 sspp[0xa4/4]=0xb8000000u;
 ar_manual_publish(10);
 assert(fg_scanout_ar_state==4 && fg_scanout_front==1 && starts==1);
 // The monitor does not arm while a copy is open.
 fg_scanout_copy_open=1;ar_run();assert(fg_scanout_ar_state==4 && !(intf[0x2b4/4]&0x80000000u));fg_scanout_copy_open=0;
 ar_run();
 assert(fg_scanout_ar_state==1 && intf[0x2b4/4]==0x80000000u && fg_scanout_ar_idx==0 && fg_scanout_ar_fetch==0 && fg_scanout_ar_pending==1 && fg_scanout_ar_st[FG_AR_ON]==1);
 // Verification: the write line is active in most samples with no START; the address latches at line 300.
 unsigned te=10,lines=0;
 for(unsigned i=0;i<60;i++){
  ticks+=1000;intf[0x2b0/4]=300;if(i%8==7)te++;intf[0x298/4]=(te<<16)|(0x960u+1000);
  if(i==3)sspp[0xa4/4]=0xb8a00000u;
  ar_run();lines++;
 }
 assert(fg_scanout_ar_state==2 && fg_scanout_ar_confirmed && fg_scanout_ar_st[FG_AR_CONFIRMED]==1 && fg_scanout_ar_fetch==1 && !fg_scanout_ar_pending && !fg_scanout_ar_hold);
 assert(fg_scanout_ar_ll[2]==1 && !fg_scanout_ar_st[FG_AR_LATCH_MID] && starts==1);
 // A present is a flip: no START, SRC0 gets the free buffer, never the latched one.
 unsigned s0=starts;
 assert(fuxi_gpu_scanout_acquire()==1 && fg_scanout_copy==0);
 assert(fuxi_gpu_scanout_publish()==1 && starts==s0 && fg_scanout_front==0 && sspp[0x14/4]==0xb8000000u && fg_scanout_ar_pending==1 && fg_scanout_ar_st[FG_AR_FLIPS]==1);
 // A second flip before the first latched replaces it: buffer 0 was never fetched and is free again.
 assert(fuxi_gpu_scanout_acquire()==1 && fg_scanout_copy==2);
 assert(fuxi_gpu_scanout_publish()==1 && starts==s0 && fg_scanout_front==2 && sspp[0x14/4]==0xb9400000u && fg_scanout_ar_pending==1);
 assert(fuxi_gpu_scanout_acquire()==1 && fg_scanout_copy==0);
 assert(fuxi_gpu_scanout_publish()==1 && fg_scanout_front==0);
 // The latch is seen at the frame start (write line idle): the displaced buffer is held until a TE has passed.
 sspp[0xa4/4]=0xb8000000u;intf[0x2b0/4]=2400;ar_run();
 assert(fg_scanout_ar_fetch==0 && !fg_scanout_ar_pending && fg_scanout_ar_hold==2u && fg_scanout_ar_ll[0]==1);
 assert(fuxi_gpu_scanout_acquire()==1 && fg_scanout_copy==2);
 assert(fuxi_gpu_scanout_publish()==1 && fg_scanout_front==2);
 ticks+=8400;
 sspp[0xa4/4]=0xb9400000u;ar_run(); // latched, buffer 0 held until the next TE
 assert(fg_scanout_ar_fetch==2 && fg_scanout_ar_hold==1u);
 assert(fuxi_gpu_scanout_acquire()==1 && fg_scanout_copy==1);
 // With every other buffer busy (staged unlatched + latched + held) there is nothing to copy into.
 assert(fuxi_gpu_scanout_publish()==1 && fg_scanout_front==1 && fg_scanout_ar_pending);
 assert(!fuxi_gpu_scanout_acquire() && fg_scanout_ar_st[FG_AR_TARGET_WAIT]==1 && !fg_scanout_writable);
 ticks+=8400;ar_run();
 assert(!fg_scanout_ar_hold && fuxi_gpu_scanout_acquire()==1 && fg_scanout_copy==0);
 assert(fuxi_gpu_scanout_publish()==1 && fg_scanout_front==0);
 sspp[0xa4/4]=0xb8000000u;intf[0x2b0/4]=2400;ar_run();assert(!fg_scanout_ar_pending && fg_scanout_ar_fetch==0);
 // A latch seen mid-transfer is counted as a tear risk.
 assert(fuxi_gpu_scanout_acquire()==1 && fuxi_gpu_scanout_publish()==1 && fg_scanout_front==1);
 sspp[0xa4/4]=0xb8a00000u;intf[0x2b0/4]=1500;ar_run();assert(fg_scanout_ar_st[FG_AR_LATCH_MID]==1 && fg_scanout_ar_ll[4]==1);
 // Idle: 48 TEs without a flip disable autorefresh and the staged source is the latched one.
 intf[0x2b0/4]=2400;ticks+=50*8333;ar_run();
 assert(fg_scanout_ar_state==0 && !(intf[0x2b4/4]&0x80000000u) && fg_scanout_front==fg_scanout_ar_fetch && fg_scanout_ar_st[FG_AR_OFF_IDLE]==1);
 // The next present re-arms it without a new verification.
 fg_scanout_ar_arming();assert(fg_scanout_ar_state==4);
 ar_run();assert(fg_scanout_ar_state==2 && intf[0x2b4/4]==0x80000000u && fg_scanout_ar_st[FG_AR_ON]==2);
 // CPU fallback disables it first.
 (void)fuxi_gpu_scanout_cpu_acquire();
 assert(fg_scanout_ar_state==0 && !(intf[0x2b4/4]&0x80000000u) && fg_scanout_ar_st[FG_AR_OFF_CPU]==1);
 // A stop while it is on switches it off.
 fg_scanout_failed=0;fg_scanout_ar_arming();assert(fg_scanout_ar_state==4);ar_run();assert(fg_scanout_ar_state==2);
 (void)fg_scanout_stop("test");
 assert(fg_scanout_ar_state==3 && !(intf[0x2b4/4]&0x80000000u) && fg_scanout_ar_st[FG_AR_OFF_STOP]==1);
 // Hardware that does not repeat the transfer: the write line stays idle, autorefresh is switched off after 100 ms and the staged source restored.
 triple_setup(panel);
 fg_scanout_panel120_active=1;fg_scanout_period_us=8333;fg_scanout_te_init=0x960;fg_scanout_ar_state=0;fg_scanout_ar_confirmed=0;fg_scanout_ar_busy=0;fg_scanout_ar_flip_valid=0;memset(fg_scanout_ar_st,0,sizeof(fg_scanout_ar_st));
 sspp[0xa4/4]=0xb8000000u;
 ar_manual_publish(30);ar_run();
 assert(fg_scanout_ar_state==1 && fg_scanout_front==1);
 // Count 0 never repeats (line idle): after 100 ms count 1 is tried, then the mode is given up for the boot and the staged source restored.
 for(unsigned i=0;i<400 && (fg_scanout_ar_state==1 || fg_scanout_ar_state==4);i++){ticks+=1000;intf[0x2b0/4]=2400;ar_run();}
 assert(fg_scanout_ar_state==3 && fg_scanout_ar_idx==1 && !(intf[0x2b4/4]&0x80000000u) && fg_scanout_ar_st[FG_AR_OFF_FAIL]==2 && fg_scanout_front==0 && sspp[0x14/4]==0xb8000000u && fg_scanout_ar_st[FG_AR_DROPS]==1 && fg_scanout_ar_st[FG_AR_ON]==2);
 // Hardware that repeats every second TE (F185, count 1): count 0 shows 48% duty (too low for 95%), count 1 is confirmed at 48%.
 triple_setup(panel);
 fg_scanout_panel120_active=1;fg_scanout_period_us=8333;fg_scanout_te_init=0x960;fg_scanout_ar_state=0;fg_scanout_ar_idx=0;fg_scanout_ar_confirmed=0;fg_scanout_ar_busy=0;fg_scanout_ar_flip_valid=0;memset(fg_scanout_ar_st,0,sizeof(fg_scanout_ar_st));
 sspp[0xa4/4]=0xb8000000u;
 ar_manual_publish(35);ar_run();
 assert(fg_scanout_ar_state==1 && intf[0x2b4/4]==0x80000000u);
 for(unsigned i=0;i<400 && fg_scanout_ar_state!=2 && fg_scanout_ar_state!=3;i++){ticks+=1000;intf[0x2b0/4]=(i%21)<10?300:2400;ar_run();}
 assert(fg_scanout_ar_state==2 && fg_scanout_ar_idx==1 && fg_scanout_ar_confirmed && intf[0x2b4/4]==0x80000001u && fg_scanout_ar_st[FG_AR_ON]==2 && fg_scanout_ar_st[FG_AR_OFF_FAIL]==1);
 // The latched source is not one of A/B/C: autorefresh is never enabled.
 fg_scanout_ar_state=0;fg_scanout_ar_idx=0;
 triple_setup(panel);
 fg_scanout_panel120_active=1;fg_scanout_period_us=8333;fg_scanout_te_init=0x960;fg_scanout_ar_state=0;fg_scanout_ar_busy=0;sspp[0xa4/4]=0xb8000000u;
 ar_manual_publish(40);sspp[0xa4/4]=0x12345678u;ar_run();
 assert(fg_scanout_ar_state==3 && !(intf[0x2b4/4]&0x80000000u));
 fg_abi_ar=0;fg_scanout_ar_state=0;fg_scanout_ar_confirmed=0;fg_scanout_period_us=16667;fg_scanout_panel120_active=keepP;fg_scanout_te_init=keepInit;fg_scanout_back_ready=0;
 fg_scanout_starts=keepStarts;fg_scanout_current_match=keepMatch;fg_scanout_current_mismatch=keepMismatch;fg_scanout_te_frames=keepTe;intf[0x298/4]=keepCount;ctl[0x1c/4]=1;intf[0x2b0/4]=2400;(void)lines;
}
static unsigned maskedPolls,openPolls;
static void hard_script(void) {phase_script();if(interruptsOn)openPolls++;else maskedPolls++;}
static void hard_run(uint64_t e,unsigned c,unsigned hold,unsigned wakeOff) {
 phE=e+phSkew;phC=c;phHold=hold;ticks=e+wakeOff;fg_scanout_ph_e=e;fg_scanout_spin_sync=0;fg_scanout_spin_serial=fg_scanout_starts;
 fg_scanout_spin_c0=c;fg_scanout_ph_wake=ticks;fg_scanout_spin_busy=1;probeScript=hard_script;hard_script();
 fg_scanout_spin_run(0,0);probeScript=0;}
static void hard_tests(uint8_t *panel) {
 // fuxigpuncimport 262144: hard spin. Early wake, continuous polling with interrupts masked in short slices, START at the first done sample.
 triple_setup(panel);
 unsigned keepP=fg_scanout_panel120_active,keepStarts=fg_scanout_starts,keepMatch=fg_scanout_current_match,keepMismatch=fg_scanout_current_mismatch,keepTe=fg_scanout_te_frames,keepCount=intf[0x298/4],keepMode=fg_scanout_te_mode;
 unsigned *st=fg_scanout_spin_stat;
 fg_abi_spin=1;fg_abi_hard=1;fg_scanout_panel120_active=1;fg_scanout_period_us=8333;fg_scanout_spin_busy=0;spinKicks=0;memset(st,0,sizeof(fg_scanout_spin_stat));
 memset(fg_scanout_hard_d2s,0,sizeof(fg_scanout_hard_d2s));fg_scanout_hard_n=0;fg_scanout_hard_sum=fg_scanout_hard_max=fg_scanout_hard_total=0;
 fg_scanout_spin_err_sum=fg_scanout_spin_err_max=fg_scanout_spin_late_sum=fg_scanout_spin_late_max=0;fg_scanout_spin_err_n=fg_scanout_spin_late_n=0;
 maskedPolls=openPolls=0;
 ticks+=20000;uint64_t T=ticks;
 fg_scanout_ph_ok=1;fg_scanout_ph_p=8333;fg_scanout_ph_done_us=7950;fg_scanout_ph_late_us=500;fg_scanout_ph_t=T;fg_scanout_ph_c=5;fg_scanout_last_start=T-4000;fg_scanout_last_count=5;
 assert(fg_scanout_spin_lead_us()==1500);
 // A frame is waiting during a transfer: the wake is armed 1.5 ms before the predicted done (not later with the adaptive lead).
 assert(fuxi_gpu_scanout_acquire()==1);ctl[0x1c/4]=1;intf[0x2b0/4]=100;assert(fuxi_gpu_scanout_publish()==2 && fg_scanout_back_ready);
 assert(spinKicks==1 && fg_scanout_spin_busy && spinDeadline==T+7950-1500);
 unsigned s0=starts;
 hard_run(T,5,0,6550);
 assert(starts==s0+1 && !fg_scanout_back_ready && st[FG_SPIN_PUBLISHED]==1 && st[FG_SPIN_WAIT_DONE]==1 && st[FG_SPIN_IN_WINDOW]==1 && fg_scanout_hard_d2s[0]==1);
 assert(fg_scanout_hard_n==1 && fg_scanout_hard_max>=1000 && interruptsOn && maskedPolls>100 && openPolls>10 && !st[FG_SPIN_OVERRUN]);
 assert(fuxi_gpu_scanout_retry()==1);
 // The line never goes idle: the spin gives up after 2 ms (overrun) and leaves interrupts enabled; the frame stays waiting.
 assert(fuxi_gpu_scanout_acquire()==1);ctl[0x1c/4]=1;intf[0x2b0/4]=100;assert(fuxi_gpu_scanout_publish()==2 && fg_scanout_back_ready);
 fg_scanout_spin_busy=0;s0=starts;
 hard_run(fg_scanout_ph_t+8333,7,1,6550);
 assert(starts==s0 && fg_scanout_back_ready && st[FG_SPIN_OVERRUN]==1 && interruptsOn && st[FG_SPIN_WAIT_DONE]==1);
 // A copy still open at done: not started, counted as a missed reason.
 ticks+=20000;fg_scanout_ph_t=ticks;fg_scanout_ph_c=9;fg_scanout_last_start=ticks-4000;fg_scanout_last_count=9;
 fg_scanout_copy_open=1;fg_scanout_spin_busy=0;s0=starts;
 hard_run(fg_scanout_ph_t,9,0,6550);
 assert(starts==s0 && fg_scanout_back_ready && st[FG_SPIN_COPY_OPEN]==1 && st[FG_SPIN_WAIT_DONE]==2 && interruptsOn);
 // The display path starts a waiting frame while no spin runs: counted.
 fg_scanout_copy_open=0;fg_scanout_spin_busy=0;fg_scanout_te_mode=0;fg_scanout_after=0;ctl[0x1c/4]=0;intf[0x2b0/4]=2400;
 fg_scanout_offer_xfer=0;
 assert(fuxi_gpu_scanout_retry()==1 && st[FG_SPIN_IDLE_ARRIVAL]==1 && fg_scanout_miss_why[3]==0);
 // v6: a copy into X is open (token held) while Y is waiting: the spin STARTs Y, the token stays with the copy, X is offered afterwards.
 ticks+=20000;fg_scanout_ph_t=ticks;fg_scanout_ph_c=11;fg_scanout_last_start=ticks-4000;fg_scanout_last_count=11;ctl[0x1c/4]=1;intf[0x2b0/4]=100;
 fg_scanout_spin_busy=0;fg_scanout_te_mode=0;fg_scanout_after=0;fg_scanout_triple_requested=1;
 assert(fuxi_gpu_scanout_acquire()==1 && fg_scanout_copy_open && fg_scanout_writable);
 unsigned X=fg_scanout_copy,Y=0;while(Y==X || Y==fg_scanout_front) Y++;
 fg_scanout_ready=Y;fg_scanout_back_ready=1;fg_scanout_ready_at=ticks;
 st[FG_SPIN_COPY_STARTED]=st[FG_SPIN_COPY_SAME]=0;s0=starts;
 hard_run(fg_scanout_ph_t,11,0,6550);
 assert(starts==s0+1 && fg_scanout_front==Y && !fg_scanout_back_ready && st[FG_SPIN_COPY_STARTED]==1 && !st[FG_SPIN_COPY_SAME]);
 assert(fg_scanout_writable && fg_scanout_copy_open && fg_scanout_copy==X && !fg_scanout_spin_stolen);
 ticks+=20000;ctl[0x1c/4]=0;intf[0x2b0/4]=2400;
 assert(fg_scanout_async_published && fuxi_gpu_scanout_publish()>=0 && fg_scanout_ready==X);
 assert(fuxi_gpu_scanout_offer_fate()==1 && !fg_scanout_async_published && !fuxi_gpu_scanout_async_take()); // Y was STARTed during X's copy: reported once
 fg_scanout_async_published=1;assert(fuxi_gpu_scanout_async_take()==1 && !fg_scanout_async_published && !fuxi_gpu_scanout_async_take());
 // Same buffer as the copy: still refused and counted.
 fg_scanout_copy_open=1;fg_scanout_copy=fg_scanout_ready=2;fg_scanout_back_ready=1;fg_scanout_spin_busy=0;fg_scanout_writable=1;s0=starts;
 ticks+=20000;fg_scanout_ph_t=ticks;fg_scanout_ph_c=13;fg_scanout_last_start=ticks-4000;fg_scanout_last_count=13;ctl[0x1c/4]=1;intf[0x2b0/4]=100;
 hard_run(fg_scanout_ph_t,13,0,6550);
 assert(starts==s0 && st[FG_SPIN_COPY_SAME]==1);
 fg_scanout_copy_open=0;
 fg_abi_hard=0;fg_abi_spin=0;fg_scanout_te_mode=keepMode;fg_scanout_period_us=16667;fg_scanout_panel120_active=keepP;fg_scanout_back_ready=0;
 fg_scanout_starts=keepStarts;fg_scanout_current_match=keepMatch;fg_scanout_current_mismatch=keepMismatch;fg_scanout_te_frames=keepTe;intf[0x298/4]=keepCount;ctl[0x1c/4]=1;intf[0x2b0/4]=2400;
}
int main(void) {
 te_estimator_tests();
 reset();assert(fuxi_gpu_scanout_acquire()==1 && fuxi_gpu_scanout_publish()==0);
 assert(fuxi_gpu_scanout_reserve());fuxi_gpu_scanout_release();assert(!reads && !writes && !observations);
 uint8_t *source=malloc(4544u*2436),*panel=malloc(4320u*2400);assert(source && panel);
 // fuxigpuncimport bit 20 needs the lane; takeover then adds one read-only VIG0 survey and no write.
 reset();modelNc=327168;fuxi_gpu_scanout_configure((uintptr_t)panel,1,0);
 assert(!fuxi_gpu_scanout_preclean() && !fuxi_gpu_scanout_acquire() && writes==1 && !ssppLog[0]);
 reset();modelNc=327168|0x100000u;fuxi_gpu_scanout_configure((uintptr_t)panel,0,0);assert(!fuxi_gpu_scanout_preclean());
 reset();modelNc=327168|0x100000u;sspp[0]=0x09600438u;sspp[0x24/4]=4320;fuxi_gpu_scanout_configure((uintptr_t)panel,1,0);
 assert(fuxi_gpu_scanout_preclean() && !fuxi_gpu_scanout_acquire() && writes==1 && !starts);
 assert(strstr(ssppLog,"size=0x9600438 img=0 xy=0 ") && strstr(ssppLog,"src0=0xb8000000 ystride0=0x10e0 format=0 "));
 // fuxigpuncimport bit 22: the read-only DPU survey adds no MMIO write and an identical write trace; SMMU is never written (read-only map).
 reset();modelNc=327168;fuxi_gpu_scanout_configure((uintptr_t)panel,1,0);assert(!fuxi_gpu_scanout_acquire());
 unsigned baseWrites=writes,baseTrace=traceCount;assert(!dpuLog[0] && !smmuMaps);
 reset();modelNc=327168|0x400000u;memset(smmu,0,sizeof(smmu));
 smmu[0x20/4]=0x48000000u|64;smmu[0x24/4]=(6u<<28)|32; // 64 SMRs, 32 banks, 4K pages, CB base 128 pages
 smmu[(0x800+4*5)/4]=0x80021c00u;smmu[(0xc00+4*5)/4]=3;   // DT handoff <0x1c00 0x2> -> translate bank 3
 smmu[(0x800+4*6)/4]=0x80001c01u;                         // 0x1c01 matches neither display stream
 smmu[(0x800+4*7)/4]=0x00001c02u;                         // invalid SMR
 smmu[(0x800+4*9)/4]=0x80001c02u;smmu[(0xc00+4*9)/4]=0x10000u|3; // bypass S2CR: no bank read
 smmu[(0x1000+4*3)/4]=0x10003u;smmu[(0x1800+4*3)/4]=1;
 unsigned cb=0x80000+3*0x1000;smmu[cb/4]=0xe0;smmu[(cb+0x10)/4]=5;smmu[(cb+0x20)/4]=0x12345000u;smmu[(cb+0x24)/4]=0x30000u;
 smmu[(cb+0x30)/4]=0x80003520u;smmu[(cb+0x38)/4]=0x44;smmu[(cb+0x58)/4]=0x2;
 sspp[0xa4/4]=0xb8000000u;sspp[0x24/4]=4320;ctl[0x18/4]=0x40;
 fuxi_gpu_scanout_configure((uintptr_t)panel,1,0);assert(fg_scanout_dpu_dump && !fuxi_gpu_scanout_preclean());
 assert(!fuxi_gpu_scanout_acquire() && writes==baseWrites && traceCount==baseTrace && !smmuMapped && smmuMaps==1 && !fg_scanout_dpu_dump);
 assert(strstr(dpuLog,"dpu smmu scr0=0 idr0=0x48000040 idr1=0x60000020 idr2=0 smrs=64 cbs=32 page=0x1000 cb_base=0x80000\n"));
 assert(strstr(dpuLog,"dpu smmu smr5=0x80021c00 s2cr=0x3 cb=3 cbar=0x10003 cba2r=0x1\n"));
 assert(strstr(dpuLog,"dpu smmu cb3 sctlr=0xe0 ttbr0=0x3000012345000 tcr=0x80003520 tcr2=0x5 mair0=0x44 fsr=0x2\n"));
 assert(strstr(dpuLog,"dpu smmu smr9=0x80001c02 s2cr=0x10003 cb=3 cbar=0xffffffff cba2r=0xffffffff\n"));
 assert(!strstr(dpuLog,"smr6=") && !strstr(dpuLog,"smr7="));
 assert(strstr(dpuLog,"vig0 size=0 img=0 xy=0 out_size=0 out_xy=0 ystride0=0x10e0 format=0 unpack=0 src0=0xb8000000 cur=0xb8000000 flush=0x40 start=0"));
 assert(strstr(dpuLog,"done matches=2 faults=0 (read only)"));
 // Second takeover after a stop never repeats; a faulting bank read prints ~0 and is counted; a map failure still logs VIG0.
 reset();modelNc=0x400000u|327168;smmuFault=cb+0x58;fuxi_gpu_scanout_configure((uintptr_t)panel,1,0);
 assert(!fuxi_gpu_scanout_acquire() && strstr(dpuLog,"fsr=0xffffffff\n") && strstr(dpuLog,"matches=2 faults=1"));
 reset();modelNc=0x400000u|327168;smmuFault=0x20;fuxi_gpu_scanout_configure((uintptr_t)panel,1,0);
 assert(!fuxi_gpu_scanout_acquire() && strstr(dpuLog,"smrs=0 cbs=0") && strstr(dpuLog,"matches=0 faults=1"));
 reset();modelNc=0x400000u|327168;smmuFailMap=1;fuxi_gpu_scanout_configure((uintptr_t)panel,1,0);
 assert(!fuxi_gpu_scanout_acquire() && strstr(dpuLog,"dpu smmu map failed") && strstr(dpuLog,"vig0 size=") && writes==baseWrites);
 assert(!smmuWrites && !smmuRwMaps);
 dpu_stage2_tests(panel);
 dpu_stage3_tests(panel);
 reset();
 // Only exact option value 1 activates the lane/checker.
 fuxi_gpu_scanout_configure((uintptr_t)panel,2,1);assert(!fg_scanout_requested && !fg_scanout_checks);
 fuxi_gpu_scanout_configure((uintptr_t)panel,1,2);assert(fg_scanout_requested && !fg_scanout_checks);
 ready=0;assert(fuxi_gpu_scanout_acquire()==1 && !reads && !writes);ready=1;
 assert(!fuxi_gpu_scanout_acquire() && fuxi_gpu_scanout_owned() && !fg_scanout_writable);
 assert(intf[0x2b4/4]==1 && writes==1 && !starts);
 assert(fuxi_gpu_scanout_publish()==-1 && !starts);
 ticks+=19999;assert(!fuxi_gpu_scanout_acquire());
 ticks++;intf[0x2b0/4]=100;assert(!fuxi_gpu_scanout_acquire());
 intf[0x2b0/4]=2400;ctl[0x1c/4]=1;assert(!fuxi_gpu_scanout_acquire());
 // The read pointer keeps counting while idle; it must not block the drain.
 ctl[0x1c/4]=0;intf[0x298/4]=1200;assert(fuxi_gpu_scanout_acquire()==1 && fg_scanout_writable);
 // Existing console/display kick must not start an owned transfer, even idle.
 fuxi_display_kick();assert(!ctl[0x1c/4]);
 fg_client_busy=1;abiResult=1;assert(!fuxi_gpu_scanout_reserve() && !fg_scanout_reserved && fg_abi_present_wait); fg_abi_present_wait=0;
 abiResult=0;assert(fuxi_gpu_scanout_reserve() && !fg_client_busy && fg_scanout_reserved);
 assert(!fuxi_gpu_scanout_reserve());fuxi_gpu_scanout_release();assert(!fg_scanout_reserved);
 // A submitted copy cannot publish until the fence has retired.
 fg_busy=1;assert(!fuxi_gpu_scanout_acquire() && fuxi_gpu_scanout_publish()==-1 && !starts);
 fg_busy=0;assert(fuxi_gpu_scanout_publish()==1 && starts==1 && !fg_scanout_writable);
 assert(!fuxi_gpu_scanout_acquire());ctl[0x1c/4]=0;ticks+=20000;
 assert(fuxi_gpu_scanout_acquire()==1);
 // Drain timeout and unexpected AR are fail-closed, never an early CPU fallback.
 ctl[0x1c/4]=1;ticks=fg_scanout_timeout;assert(fuxi_gpu_scanout_acquire()==-1);
 assert(!fg_scanout_writable && fuxi_gpu_scanout_publish()==-1 && starts==1);
 reset();fuxi_gpu_scanout_configure((uintptr_t)panel,1,0);assert(!fuxi_gpu_scanout_acquire());
 intf[0x2b4/4]|=0x80000000u;assert(fuxi_gpu_scanout_acquire()==-1 && !starts);
 reset();fuxi_gpu_scanout_configure((uintptr_t)panel,1,0);failAddress=0xae362b4;
 assert(fuxi_gpu_scanout_acquire()==-1 && !writes && !starts);
 // CPU row-copy oracle: the exact centered crop, padding and every BGRA/alpha byte.
 reset();fuxi_gpu_scanout_configure((uintptr_t)panel,1,1);
 for(unsigned y=0;y<2436;y++)for(unsigned x=0;x<1136;x++)
  ((uint32_t *)source)[y*1136+x]=(y*0x314159u)^(x*0x1234567u);
 fg_scanout_source=(uintptr_t)source;fg_scanout_pitch=4544;
 fg_scanout_sx=22;fg_scanout_sy=18;fg_scanout_dx=fg_scanout_dy=0;
 fg_scanout_w=1080;fg_scanout_h=2400;
 for(unsigned y=0;y<2400;y++)memcpy(panel+y*4320,source+(y+18)*4544+22*4,4320);
 for(unsigned frame=0;frame<4;frame++)assert(fg_scanout_verify());
 assert(fg_scanout_checked==4);panel[0]^=1;assert(fg_scanout_verify()); // first four only
 fg_scanout_checked=0;assert(!fg_scanout_verify() && fg_scanout_checked==1);panel[0]^=1;
 panel[4320u*2400-1]^=0x80;assert(!fg_scanout_verify());panel[4320u*2400-1]^=0x80;
 fg_scanout_checked=0;fg_scanout_w=17;fg_scanout_h=3;fg_scanout_dx=3;fg_scanout_dy=5;
 memset(panel,0x5a,4320u*2400);
 for(unsigned y=0;y<3;y++)memcpy(panel+(y+5)*4320+3*4,source+(y+18)*4544+22*4,17*4);
 assert(fg_scanout_verify() && panel[0]==0x5a);
 // TE gate (fuxiscante=1): no floor; a TE after START plus a margin, then the
 // usual START/write-line checks. A TE racing START only delays.
 reset();modelTe=1;fuxi_gpu_scanout_configure((uintptr_t)panel,1,0);
 assert(fg_scanout_te_mode==1 && fg_scanout_drain_us==0);
 intf[0x294/4]=2400;intf[0x298/4]=(5u<<16)|3000;intf[0x2b0/4]=2400;
 assert(!fuxi_gpu_scanout_acquire() && fuxi_gpu_scanout_owned() && fg_scanout_te_init==2400);
 assert(!fuxi_gpu_scanout_acquire() && fg_scanout_wait_te==1);           // no TE yet
 intf[0x298/4]=(6u<<16)|2410;assert(!fuxi_gpu_scanout_acquire() && fg_scanout_wait_te==2); // just after TE
 unsigned xfer=fg_scanout_wait_xfer;
 intf[0x298/4]=(6u<<16)|2500;intf[0x2b0/4]=100;assert(!fuxi_gpu_scanout_acquire() && fg_scanout_wait_xfer==xfer+1);
 intf[0x2b0/4]=2400;assert(fuxi_gpu_scanout_acquire()==1);
 assert(fuxi_gpu_scanout_publish()==1 && starts==1 && fg_scanout_te_frame==6);
 ctl[0x1c/4]=0;assert(!fuxi_gpu_scanout_acquire());                       // same TE frame
 intf[0x298/4]=(8u<<16)|2401;assert(fuxi_gpu_scanout_acquire()==1);       // two TEs: done
 // A stalled TE count falls back to the default floor from that START.
 assert(fuxi_gpu_scanout_publish()==1 && starts==2);ctl[0x1c/4]=0;
 ticks+=FG_SCAN_TE_STALL_US-1;assert(!fuxi_gpu_scanout_acquire() && fg_scanout_te_mode==1);
 ticks++;assert(!fuxi_gpu_scanout_acquire() && !fg_scanout_te_mode && fg_scanout_drain_us==FG_SCAN_DRAIN_US);
 assert(fuxi_gpu_scanout_acquire()==1 && !fg_scanout_failed);
 // An explicit floor still applies with the TE gate.
 reset();modelTe=1;modelDrain=4000;fuxi_gpu_scanout_configure((uintptr_t)panel,1,0);
 assert(fg_scanout_te_mode==1 && fg_scanout_drain_us==4000);
 // Window summary: TE rate from the frame count, START gaps in 60 Hz periods.
 reset();fuxi_gpu_scanout_configure((uintptr_t)panel,1,0);intf[0x2b0/4]=2400;
 assert(!fuxi_gpu_scanout_acquire());
 for(unsigned i=0;i<599;i++){
  ticks+=i%4==3?33333:16667;intf[0x298/4]=((i*2u+2)<<16)|2500; // TE at 120 Hz
  fg_scanout_writable=1;ctl[0x1c/4]=0;assert(fuxi_gpu_scanout_publish()==1);
 }
 assert(fg_scanout_iv[0]==449 && fg_scanout_iv[1]==149 && !fg_scanout_iv[2] && !fg_scanout_iv[3]);
 ticks+=16667;intf[0x298/4]=(1200u<<16)|2500;fg_scanout_writable=1;ctl[0x1c/4]=0;
 assert(fuxi_gpu_scanout_publish()==1 && !fg_scanout_iv[0] && !fg_scanout_iv[1] && fg_scanout_starts%600==0);
 panel120_tests(panel);
 triple_tests(panel);
 fifo_tests(panel);
 double_tests(panel);
 mid_tests(panel);
 ar_tests(panel);
 hard_tests(panel);
 free(source);free(panel);
 puts("PASS: actual scanout gate, delayed transfer/fence/START ordering, busy reservation, timeout/MMIO/AR fail-close, TE gate/stall fallback/window summary, full crop/alpha/tail pixel oracle");
}
'''


def compile_run(tmp, name, source, cpp=False, includes=()):
    src = tmp / (name + ('.cpp' if cpp else '.c'))
    src.write_text(source)
    exe = tmp / name
    subprocess.run(['clang++' if cpp else 'clang', '-std=c++17' if cpp else '-std=gnu11',
                    '-O1', '-g', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined',
                    '-fno-omit-frame-pointer', *['-I'+str(p) for p in includes], str(src), '-o', str(exe)], check=True)
    subprocess.run([str(exe)], check=True)


def kernel_syntax(buildtree):
    """Compile only our sources against saved XNU headers; execute no tree scripts."""
    surface_header = (ROOT / 'xnu/fuxi-scanout/SweetSurface.h').read_text()
    config = buildtree / 'build/xnu.obj/DEVELOPMENT_ARM64_FUXI'
    if not config.exists():
        config = buildtree / 'build/xnu.obj/DEVELOPMENT_ARM64_SWEET'
    with tempfile.TemporaryDirectory(prefix='fuxi-scanout-syntax-') as temp:
        temp = Path(temp)
        dev, arm = temp / 'bsd/dev', temp / 'iokit/Kernel/arm'
        dev.mkdir(parents=True)
        arm.mkdir(parents=True)
        (arm / 'SweetSurface.h').write_text(surface_header)
        for path in (ROOT / 'xnu/fuxi-adreno').iterdir():
            if path.suffix in ('.h', '.inc', '.c'):
                (dev / path.name).write_bytes(path.read_bytes())
        for name in ('gpu.c', 'gpu-a740.h', 'gpu-zap.h', 'gpu-handoff.h', 'gpu-clock.h'):
            (dev / name).write_bytes((ROOT / 'shim/fuxi-probe' / name).read_bytes())
        (arm / 'SweetSurface.cpp').write_bytes((ROOT / 'xnu/fuxi-scanout/SweetSurface.cpp').read_bytes())
        # Check the modified console function against the complete preexisting TU.
        pe = temp / 'pexpert/arm/pe_serial.c'
        pe.parent.mkdir(parents=True)
        original = (buildtree / 'xnu/pexpert/arm/pe_serial.c').read_text()
        before = module('scanout_kick_sync', 'tools/sync-fuxi-scanout.py').KICK_BEFORE
        pe.write_text(original)
        if before not in original:
            # The saved 0081 tree predates Fuxi. Apply only pe_serial text deltas
            # in our temporary tree, never mutate or run the saved checkout.
            for patch in sorted((ROOT / 'xnu/patches').glob('*.patch')):
                if patch.name < '0082' or patch.name >= '0108':
                    continue
                for chunk in re.split(r'(?=^--- a/)', patch.read_text(), flags=re.M):
                    if chunk.startswith('--- a/pexpert/arm/pe_serial.c\n'):
                        check = subprocess.run(['git', 'apply', '--check', '-'], input=chunk,
                                               text=True, cwd=temp, capture_output=True)
                        if check.returncode:
                            subprocess.run(['git', 'apply', '--reverse', '--check', '-'], input=chunk,
                                           text=True, cwd=temp, check=True, capture_output=True)
                        else:
                            subprocess.run(['git', 'apply', '--whitespace=nowarn', '-'], input=chunk,
                                           text=True, cwd=temp, check=True, capture_output=True)
            original = pe.read_text()
        assert before in original
        pe.write_text(original.replace(before, (ROOT / 'xnu/fuxi-scanout/display-kick.c').read_text()))
        # Compile the new union delta against the complete local saved XNU TU.
        lookup = temp / 'bsd/vfs/vfs_lookup.c'
        lookup.parent.mkdir(parents=True)
        lookup.write_bytes((buildtree / 'xnu/bsd/vfs/vfs_lookup.c').read_bytes())
        (lookup.parent / 'fuxi_unixnu_lookup.h').write_bytes(
            (ROOT / 'xnu/fuxi-unixnu/fuxi_unixnu_lookup.h').read_bytes())
        subprocess.run(['git', 'apply', '--check', str(ROOT / 'xnu/patches/0110-fuxi-union-lower-directory.patch')],
                       cwd=temp, check=True)
        subprocess.run(['git', 'apply', str(ROOT / 'xnu/patches/0110-fuxi-union-lower-directory.patch')],
                       cwd=temp, check=True)
        for comp, path in [('bsd', dev / 'fuxi_adreno.c'), ('iokit', arm / 'SweetSurface.cpp'), ('pexpert', pe), ('bsd', lookup)]:
            cwd = config / comp / 'DEVELOPMENT'
            old_root = re.search(r'^SOURCE_DIR=(.+)/xnu$', (cwd / 'Makefile').read_text(), re.M)[1]
            raw = (cwd / '.CFLAGS').read_text().replace('\0', '')
            raw = re.sub(r'(-DCONFIG_NMBCLUSTERS=\(\(1024 \* 512\) / MCLBYTES\))', lambda m: shlex.quote(m[1]), raw)
            saved = shlex.split(raw)[1:]
            if comp == 'iokit':
                saved += shlex.split((cwd / '.CXXFLAGS').read_text().replace('\0', ''))[1:]
            cmd = ['clang++' if comp == 'iokit' else 'clang', '--target=arm64-apple-macos14',
                   '-fsyntax-only', '-DFUXI', '-mgeneral-regs-only', '-Werror', '-Wno-c99-designator',
                   '-Wno-reorder-init-list', '-D__builtin_xnu_type_signature(...)=""',
                   '-D__builtin_xnu_type_summary(...)=0', '-D__builtin_xnu_types_compatible(...)=1',
                   '-I'+str(dev), '-I'+str(arm)]
            skip = False
            for arg in saved:
                if skip:
                    skip = False
                    continue
                if arg == '-arch':
                    skip = True
                    continue
                arg = arg.replace(old_root, str(buildtree))
                arg = re.sub(r'-I.*?\.sdk/+System/Library/Frameworks/Kernel\.framework/Versions/A/PrivateHeaders/platform',
                             '-I'+str(buildtree / 'sdk-kernel-platform/platform'), arg)
                cmd.append(arg)
            subprocess.run(cmd + [str(path)], cwd=cwd, check=True)
            print('PASS: complete '+path.name+' syntax against saved XNU kernel headers/flags')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--xnu-buildtree', type=Path, help='optional saved XNU build tree for full-TU syntax checks')
    args = parser.parse_args()
    subprocess.run(['python3', str(ROOT / 'tools/sync-fuxi-scanout.py'), '--check'], check=True)
    inc = (ROOT / 'xnu/fuxi-adreno/fuxi_gpu_scanout.inc').read_text()
    inc = re.sub(r'__asm__ volatile\([^;]*\);', '(void)0;', inc)
    kick = (ROOT / 'xnu/fuxi-scanout/display-kick.c').read_text()
    kick = re.sub(r'__asm__ volatile\([^;]*\);', '(void)0;', kick)
    cache = module('scanout_cache', 'tools/test-fuxi-adreno-cache.py')
    display = module('scanout_display', 'tools/test-fuxi-unixnu.py')
    with tempfile.TemporaryDirectory(prefix='fuxi-scanout-test-') as tmp:
        tmp = Path(tmp)
        compile_run(tmp, 'gate', PREFIX + display.TE_OBSERVER + display.PRESENT_TIMES + inc + kick + TEST)
        # Compile the exact USB screenshot request body; intercept the physical
        # memcpy to assert its address and bounds without touching device memory.
        probe = (ROOT / 'shim/fuxi-probe/probe.c').read_text()
        body = probe.split('} else if (type == 0xc0 && req == 0x4e) {', 1)[1].split(
            '} else if (type == 0xc0 && req == 0x4f)', 1)[0]
        compile_run(tmp, 'screenshot', r"""
#include <assert.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
static uint32_t base,pitch=4320,format=0x237ff;
static uint64_t copiedFrom;static unsigned copiedBytes;
static uint32_t usb_rd(unsigned block,unsigned offset) {
 assert(block==0xae05000);return offset==0x14?base:offset==0x24?pitch:format;
}
static void mock_copy(void *dst,const void *src,unsigned n) {
 (void)dst;copiedFrom=(uintptr_t)src;copiedBytes=n;
}
#define memcpy mock_copy
static bool screenshot(uint64_t requestedOffset,unsigned length) {
 uint8_t st[6]={0},data[512];unsigned n=0;bool ok=true;
 uint16_t value=requestedOffset;st[4]=requestedOffset>>16;st[5]=requestedOffset>>24;
 copiedFrom=copiedBytes=0;
""" + body + r"""
 return ok;
}
int main(void) {
 const unsigned size=4320u*2400;
 for(unsigned b=0;b<3;b++) {
  base=0xb8000000u+b*0xa00000u;
  assert(screenshot(123,1024) && copiedFrom==(uint64_t)base+123 && copiedBytes==512);
  assert(screenshot(size-512,512) && copiedFrom==(uint64_t)base+size-512);
  assert(screenshot(size,0) && !copiedBytes);
  assert(!screenshot(size,1) && !copiedFrom && !copiedBytes);
  assert(!screenshot(size-511,512) && !copiedFrom);
  assert(!screenshot(size+1,0));
 }
 base=0xb9e00000u;assert(!screenshot(0,512) && !copiedFrom);
 base=0xb8000000u;pitch=1;assert(!screenshot(0,512));
 pitch=4320;format=0;assert(!screenshot(0,512));
 puts("PASS: actual USB screenshot request, current A/B/C base, one-frame bounds, pitch/format refusals and length cap");
 return 0;
}
""")
        # Compile final production SweetSurface, including the reservation changes.
        source = (ROOT / 'xnu/fuxi-scanout/SweetSurface.cpp').read_text()
        declarations = source[source.index('struct sweet_gpu_backing'):source.index('static bool sweet_gpu_init')]
        funcs = ''.join(display.function(source, n) for n in (
            'sweet_gpu_init', 'sweet_gpu_drop', 'sweet_gpu_release', 'sweet_surface_gpu_copy',
            'sweet_surface_gpu_poll', 'sweet_surface_present_begin', 'sweet_surface_present_end',
            'sweet_surface_writer_serial', 'sweet_surface_present_notify', 'sweet_surface_present_reading', 'sweet_direct_reading', 'sweet_direct_nap', 'sweet_gpu_writer', 'sweet_surface_direct', 'sweet_surface_gpu_stats',
            'IOSurfaceRootUserClient::trapLock', 'IOSurfaceRootUserClient::trapUnlock'))
        # fuxiearly bit 2: the writer lease's display wait (its statics too), ahead of sweet_gpu_writer.
        display_wait = source[source.index('#define SWEET_DISPLAY_WAIT_MS'):source.index('/* AvoidSync reserves a pending producer')]
        funcs = funcs.replace('static bool sweet_gpu_writer(', display_wait + 'static bool sweet_gpu_writer(', 1)
        stubs = '''
#include <cstdarg>
#define FUXI 1
static uint32_t cfWriterId; static int cfWriterActive=-1; static void fuxi_gpu_present_writer(uint32_t id,int active) {cfWriterId=id;cfWriterActive=active;}
#define FG_DD_SLOTS_N 4u
#define FG_DD_SLOT_PAGES 4096u
static constexpr unsigned FG_DD_WHY=9;
static uint32_t abiSeq;static unsigned directBusy,directRefused[10];static int directNaps;
static uint32_t fuxi_gpu_abi_seq() {return abiSeq;}
static unsigned fuxi_gpu_scanout_direct_busy() {return directBusy;}
static void fuxi_gpu_scanout_direct_refused(unsigned why) {directRefused[why]++;}
using IOBufferMemoryDescriptor=IOMemoryDescriptor;using addr64_t=uint64_t;using IOByteCount=uint64_t;
#define kIOMemoryMapperNone 0
enum { FG_DD_GEOMETRY, FG_DD_STRIDE, FG_DD_OFFSET, FG_DD_FORMAT, FG_DD_BACKING, FG_DD_LAYERS, FG_DD_PAGES, FG_DD_SLOTS, FG_DD_STATE };
static int directOn;static int directSlotResult=2;static unsigned directSlot=1,directQueues,directPagesN;static int directQueueResult=1;
static uint64_t directKey,directQueuedKey,directPages[4096];static uintptr_t directVa;static uint32_t directFence;static bool directNullPages;
static int fuxi_gpu_scanout_direct_on() {return directOn;}
static int fuxi_gpu_scanout_direct_slot(uint64_t key,unsigned *slot,uint64_t *old) {directKey=key;*slot=directSlot;*old=0;return directSlotResult;}
static int fuxi_gpu_scanout_direct_queue(unsigned slot,uint64_t key,const uint64_t *pages,unsigned n,uintptr_t va,uint32_t fence) {
 assert(slot==directSlot);directQueues++;directQueuedKey=key;directNullPages=!pages;directPagesN=n;directVa=va;directFence=fence;
 if(pages)memcpy(directPages,pages,n*8);return directQueueResult;}
static void nanoseconds_to_absolutetime(uint64_t ns,uint64_t *t) {*t=ns/1000;}
static SweetSurface *displayHeld;static void (*sleepHook)();
static int IOLockSleepDeadline(IOLock *,void *,uint64_t,int) {if(sleepHook){sleepHook();return 0;}directNaps++;if(directNaps>=3)directBusy=0;ticks+=300;return 0;}
#define kIOSurfaceLockReadOnly 1
#define kIOReturnNotFound 3
#define kIOReturnNotReady 4
using IOReturn=int;
struct IOSurfaceRootUserClient {
 IOLock *fLock=nullptr;void *fRefs=nullptr;
 IOReturn trapLock(void *,void *,void *,void *,void *,void *);
 IOReturn trapUnlock(void *,void *,void *,void *,void *,void *);
};
static SweetSurface *trapSurface;
static SweetSurface *sweet_surface_held(IOLock *,void *,uint32_t) {if(trapSurface)trapSurface->retain();return trapSurface;}
static IOReturn sweet_surface_seed_out(SweetSurface *,void *,bool) {return 0;}
static void sweet_surface_display_poison(SweetSurface *) {}
static uint64_t sweet_surface_writer_serial(SweetSurface *,uint64_t *epoch=nullptr);
static unsigned releaseWakes;
static void released(void *context) {assert(context==&releaseWakes);releaseWakes++;}
static bool reserved,busy,owned;
static SweetSurface *hookOther;
static int fuxi_gpu_scanout_owned() {return owned;}
static int fuxi_gpu_scanout_reserve() {if(busy)return 0;assert(!reserved);reserved=true;return 1;}
static void fuxi_gpu_scanout_release() {assert(reserved);reserved=false;}
static bool precleanOn;static unsigned precleans,zcClass=7,zcLogs;static uintptr_t precleanSource;static uint64_t precleanBytes;
static void (*precleanHook)();static char zcLog[512];
static int fuxi_gpu_scanout_preclean() {return precleanOn;}
static void fuxi_gpu_clean_source(uintptr_t source,uint64_t bytes) {assert(!reserved);if(precleanHook)precleanHook();precleans++;precleanSource=source;precleanBytes=bytes;}
unsigned sweet_surface_scanout_class(SweetSurface *s,uint64_t,uint64_t bytes) {assert(bytes==2399ull*4352+1080*4 || (directOn && bytes==2435ull*s->bytesPerRow+1125*4));return zcClass;}
static int IOLog(const char *format,...) {va_list args;va_start(args,format);vsnprintf(zcLog,sizeof(zcLog),format,args);va_end(args);zcLogs++;return 0;}
'''
        extra = '''
 ready=true;busy=true;owned=true;
 IOMemoryDescriptor pendingMemory;SweetSurface pendingSurface;pendingSurface.memory=&pendingMemory;
 unsigned beforeMaps=maps,beforeCopies=copies;
 assert(sweet_surface_gpu_copy(&pendingSurface,0x10000,0,0,0,0,1080,10)==3);
 assert(!reserved && maps==beforeMaps && copies==beforeCopies && pendingSurface.refs==1);
 busy=false;pendingSurface.gpuWriters=1;
 assert(sweet_surface_gpu_copy(&pendingSurface,0x10000,0,0,0,0,1080,10)==3 && !reserved);
 pendingSurface.gpuWriters=0;
 SweetSurface *surfaces[]={&pendingSurface};
 // Preparation/submission holds the cache lock, but unrelated producers take only the short lease lock.
 SweetSurface unrelated;IOMemoryDescriptor unrelatedMemory;unrelated.memory=&unrelatedMemory;hookOther=&unrelated;
 copyHook=[]() {
  assert(sweet_gpu_lock->held && !sweet_writer_lock->held && sweet_gpu_submitting);
  assert(sweet_gpu_writer(hookOther,true) && sweet_gpu_writer(hookOther,false));
  assert(!hookOther->gpuWriters && hookOther->gpuReadyTime);
 };
 // A reservation can overlap an old display copy, but a real writer waits for it.
 assert(sweet_surface_gpu_copy(&pendingSurface,0x10000,0,0,0,0,1080,10)==2);copyHook=nullptr;
 assert(sweet_gpu_writer(&pendingSurface,true,true) && sweet_gpu_inflight==&pendingSurface);
 assert(!sweet_surface_present_begin(surfaces,1));
 assert(sweet_gpu_writer(&pendingSurface,false));
 assert(sweet_gpu_writer(&pendingSurface,true) && !sweet_gpu_inflight);
 assert(!sweet_surface_present_begin(surfaces,1));
 assert(sweet_gpu_writer(&pendingSurface,false));
 // CPU fallback reads pin the source too. Commit never sleeps on this read.
 assert(sweet_surface_present_begin(surfaces,1));
 assert(sweet_gpu_writer(&pendingSurface,true,true) && sweet_present_count==1);
 assert(sweet_gpu_writer(&pendingSurface,false));sweet_surface_present_end();
 assert(sweet_surface_present_begin(surfaces,1));
 assert(sweet_gpu_writer(&pendingSurface,true) && !sweet_present_count);
 assert(sweet_gpu_writer(&pendingSurface,false));
 pendingSurface.gpuQuarantined=true;assert(!sweet_surface_present_begin(surfaces,1));
 pendingSurface.gpuQuarantined=false;assert(!sweet_surface_present_begin(surfaces,5));
 // Final producer retirement wakes once; pending and active leases both exclude reads.
 unsigned wakes=releaseWakes;sweet_surface_present_notify(released,&releaseWakes);
 assert(sweet_gpu_writer(&pendingSurface,true,true) && sweet_gpu_writer(&pendingSurface,true));
 assert(!sweet_surface_present_begin(surfaces,1));
 assert(sweet_gpu_writer(&pendingSurface,false) && releaseWakes==wakes);
 assert(sweet_gpu_writer(&pendingSurface,false) && releaseWakes==wakes+1);
 assert(sweet_gpu_writer(&pendingSurface,false) && releaseWakes==wakes+1); // unmatched unlock
 pendingSurface.id=77;assert(sweet_gpu_writer(&pendingSurface,true,true) && cfWriterId==77 && cfWriterActive==0);pendingSurface.gpuQuarantined=true;
 assert(!sweet_gpu_writer(&pendingSurface,false) && releaseWakes==wakes+1);
 pendingSurface.gpuQuarantined=false;sweet_surface_present_notify(nullptr,nullptr);
 assert(sweet_gpu_writer(&pendingSurface,true) && sweet_gpu_writer(&pendingSurface,false) && releaseWakes==wakes+1);
 // Two queued renders keep the source pending until the final completion.
 assert(sweet_gpu_writer(&pendingSurface,true,true) && sweet_gpu_writer(&pendingSurface,true,true));
 assert(!sweet_surface_present_begin(surfaces,1));assert(sweet_gpu_writer(&pendingSurface,false));
 assert(!sweet_surface_present_begin(surfaces,1));assert(sweet_gpu_writer(&pendingSurface,false));
 uint64_t retired=pendingSurface.gpuReadyTime,admitted=0;assert(retired);
 assert(sweet_surface_present_begin(surfaces,1,&admitted) && admitted==retired);
 // An unrelated surface remains writable even while the CPU/all-source guard is held.
 assert(sweet_gpu_writer(&unrelated,true) && sweet_gpu_writer(&unrelated,false) && sweet_present_count==1);
 sweet_surface_present_end();admitted=retired+100;
 assert(sweet_surface_present_begin(surfaces,1,&admitted) && admitted==retired+100);sweet_surface_present_end();owned=false;
 // Exercise real trap option wiring and rejection, not just the writer helper.
 owned=true;trapSurface=&pendingSurface;IOSurfaceRootUserClient client;
 assert(sweet_surface_gpu_copy(&pendingSurface,0x10000,0,0,0,0,1080,10)==2);
 assert(client.trapLock((void *)1,(void *)2,nullptr,nullptr,nullptr,nullptr)==0 && sweet_gpu_inflight==&pendingSurface && pendingSurface.gpuWriters==1);
 assert(client.trapUnlock((void *)1,(void *)2,nullptr,nullptr,nullptr,nullptr)==0 && !pendingSurface.gpuWriters);
 assert(client.trapLock((void *)1,nullptr,nullptr,nullptr,nullptr,nullptr)==0 && !sweet_gpu_inflight && pendingSurface.gpuWriters==1);
 assert(client.trapUnlock((void *)1,nullptr,nullptr,nullptr,nullptr,nullptr)==0);
 pendingSurface.gpuQuarantined=true;assert(client.trapLock((void *)1,(void *)2,nullptr,nullptr,nullptr,nullptr)==kIOReturnNotReady && !pendingSurface.gpuWriters);
 pendingSurface.gpuQuarantined=false;trapSurface=nullptr;assert(client.trapLock((void *)1,(void *)2,nullptr,nullptr,nullptr,nullptr)==kIOReturnNotFound);
 // Snapshot generations reject completed newer writers too, not only active leases.
 uint64_t epoch=0,serial=sweet_surface_writer_serial(&pendingSurface,&epoch),readable=0;
 assert(sweet_gpu_writer(&pendingSurface,true,true) && sweet_surface_writer_serial(&pendingSurface)==serial+1);
 assert(sweet_gpu_writer(&pendingSurface,false));
 assert(!sweet_surface_present_begin(surfaces,1,&readable,&epoch));
 serial=sweet_surface_writer_serial(&pendingSurface,&epoch);
 assert(sweet_surface_present_begin(surfaces,1,&readable,&epoch));sweet_surface_present_end();
 // The original pending reservation can become active after swap, without a new epoch.
 assert(sweet_gpu_writer(&pendingSurface,true,true));serial=sweet_surface_writer_serial(&pendingSurface,&epoch);
 assert(sweet_gpu_writer(&pendingSurface,true));uint64_t activeEpoch=0;
 assert(sweet_surface_writer_serial(&pendingSurface,&activeEpoch)==serial+1 && activeEpoch==epoch);
 assert(sweet_gpu_writer(&pendingSurface,false) && sweet_gpu_writer(&pendingSurface,false));
 assert(sweet_surface_present_begin(surfaces,1,&readable,&epoch));sweet_surface_present_end();
 SweetSurface overflow;overflow.gpuWriters=~0u;
 assert(!sweet_gpu_writer(&overflow,true) && overflow.gpuQuarantined && !overflow.gpuWriterSerial);
 // Bit 20: the copy source is cleaned before the ring reservation, with the cache lock but neither the
 // lease lock nor the reservation held, then skipped until a writer lease intervenes; counted and logged.
 precleanOn=true;owned=true;ready=true;copy_result=2;poll_result=1;fail_map=fail_unmap=false;
 precleanHook=[]() {assert(sweet_gpu_lock->held && !sweet_writer_lock->held && sweet_gpu_submitting);};
 {
  SweetSurface pre;IOMemoryDescriptor preMemory;pre.memory=&preMemory;pre.offset=64;uint64_t elapsed=0;fg_times st;
  busy=true;assert(sweet_surface_gpu_copy(&pre,0x10000,0,0,0,0,1080,2400)==3 && precleans==1);
  assert(precleanSource==0x10040 && precleanBytes==preMemory.length-64 && !reserved);
  busy=false;assert(sweet_surface_gpu_copy(&pre,0x10000,0,0,0,0,1080,2400)==2 && precleans==1);
  assert(sweet_surface_gpu_poll(&elapsed)==1);
  assert(sweet_gpu_writer(&pre,true) && sweet_gpu_writer(&pre,false));
  assert(sweet_surface_gpu_copy(&pre,0x10000,0,0,0,0,1080,2400)==2 && precleans==2);assert(sweet_surface_gpu_poll(&elapsed)==1);
  zcClass=1;assert(sweet_surface_gpu_copy(&pre,0x10000,0,0,0,0,1080,2399)==2 && precleans==2);assert(sweet_surface_gpu_poll(&elapsed)==1);
  unsigned logs=zcLogs;sweet_surface_gpu_stats(&st);
  assert(zcLogs==logs+1 && strstr(zcLog,"cleans=2 skips=2 zerocopy eligible=0 format=0 stride=0 size=1 ") && strstr(zcLog,"high=2 unknown=0"));
  sweet_surface_gpu_stats(&st);assert(strstr(zcLog,"cleans=0 skips=0 zerocopy eligible=0 format=0 stride=0 size=0 "));
  // Default off: no pre-clean, no survey, no log.
  precleanOn=false;logs=zcLogs;
  assert(sweet_gpu_writer(&pre,true) && sweet_gpu_writer(&pre,false));
  assert(sweet_surface_gpu_copy(&pre,0x10000,0,0,0,0,1080,2400)==2 && precleans==2);assert(sweet_surface_gpu_poll(&elapsed)==1);
  sweet_surface_gpu_stats(&st);assert(zcLogs==logs);
  sweet_gpu_release(&pre);precleanHook=nullptr;
 }
 owned=false;
 puts("PASS: production pending/reader guards, nonwaiting reservation, old-copy exclusion, two producers, quarantine, bit-20 pre-clean order/skip/survey");
'''
        extra += r'''
 // Bit 24: the production SweetSurface side of direct scanout (eligibility, slot ownership, page list, fence, display-read waits).
 {
 IOMemoryDescriptor dm[3];SweetSurface ds[3];
 for(unsigned i=0;i<3;i++){ds[i].memory=&dm[i];ds[i].width=1125;ds[i].height=2436;ds[i].bytesPerRow=4608;ds[i].pixelFormat=0x42475241;dm[i].length=2436*4608;dm[i].phys=0x880000000ull+i*0x10000000ull;}
 owned=true;zcClass=1;directOn=0;
 assert(!sweet_surface_direct(&ds[0],0x7000000,22,18,1080,2400) && !directQueues);  // off: nothing
 directOn=1;
 ds[0].bytesPerRow=4352;assert(!sweet_surface_direct(&ds[0],0x7000000,22,18,1080,2400) && directRefused[FG_DD_STRIDE]==1);ds[0].bytesPerRow=4608;
 zcClass=2;assert(!sweet_surface_direct(&ds[0],0x7000000,22,18,1080,2400) && directRefused[FG_DD_FORMAT]==1);zcClass=1;
 assert(!sweet_surface_direct(&ds[0],0x7000000,0,0,1080,2400) && directRefused[FG_DD_GEOMETRY]==1);
 ds[0].offset=64;assert(!sweet_surface_direct(&ds[0],0x7000000,22,18,1080,2400) && directRefused[FG_DD_OFFSET]==1);ds[0].offset=0;
 dm[0].buffer=false;assert(!sweet_surface_direct(&ds[0],0x7000000,22,18,1080,2400) && directRefused[FG_DD_BACKING]==1);dm[0].buffer=true;
 assert(!directQueues);
 // Writer with a GPU job: the fence is the job after the lock sequence; an active writer defers the present.
 abiSeq=40;assert(sweet_gpu_writer(&ds[0],true) && ds[0].gpuLockSeq==40);
 assert(sweet_surface_direct(&ds[0],0x7000000,22,18,1080,2400)==3 && !directQueues);
 abiSeq=41;assert(sweet_gpu_writer(&ds[0],false) && ds[0].gpuWriterFence==41);
 // Miss: every 4 KiB page of the surface, its kernel address and fence; the slot keeps one reference.
 unsigned before=precleans;directSlot=1;directSlotResult=2;
 assert(sweet_surface_direct(&ds[0],0x7000000,22,18,1080,2400)==2 && directQueues==1 && !directNullPages);
 assert(directPagesN==(2435u*4608+1125*4+4095)/4096 && directPagesN==2741 && directVa==0x7000000 && directFence==41 && precleans==before+1);
 for(unsigned i=0;i<directPagesN;i++) assert(directPages[i]==0x880000000ull+i*8192);
 assert(ds[0].refs==2 && sweet_direct_held==1 && sweet_direct_slots[1].surface==&ds[0] && directQueuedKey==directKey);
 // Hit: same key, no page list, no second reference, no clean without a new writer.
 uint64_t key=directKey;directSlotResult=1;
 assert(sweet_surface_direct(&ds[0],0x7000000,22,18,1080,2400)==2 && directNullPages && directKey==key && ds[0].refs==2 && precleans==before+1);
 // A new writer while the display reads the slot naps until the slot leaves the busy mask; no writer wakeup exists for it.
 directBusy=2;directNaps=0;assert(sweet_gpu_writer(&ds[0],true) && directNaps==3 && !directBusy);abiSeq=41;
 assert(sweet_gpu_writer(&ds[0],false) && !ds[0].gpuWriterFence); // no job submitted under the lock
 assert(sweet_surface_direct(&ds[0],0x7000000,22,18,1080,2400)==2 && precleans==before+2); // rewritten: cleaned again
 // A second surface takes slot 2; then a miss in slot 1 gives the old surface back.
 directSlot=2;directSlotResult=2;assert(sweet_surface_direct(&ds[1],0x7000000,22,18,1080,2400)==2 && ds[1].refs==2 && sweet_direct_held==2);
 for(unsigned i=0;i<directPagesN;i++) assert(directPages[i]==0x890000000ull+i*8192);
 directSlot=1;assert(sweet_surface_direct(&ds[2],0x7000000,22,18,1080,2400)==2 && ds[0].refs==1 && ds[2].refs==2 && sweet_direct_slots[1].surface==&ds[2]);
 // New physical pages while slotted: refused (copy), never reuses the old key.
 dm[2].pageGeneration++;directSlotResult=1;unsigned q=directQueues;
 assert(!sweet_surface_direct(&ds[2],0x7000000,22,18,1080,2400) && directQueues==q && directRefused[FG_DD_BACKING]==2);dm[2].pageGeneration--;
 // A queue refusal keeps the slot empty and the surface unreferenced.
 directSlot=3;directSlotResult=2;directQueueResult=0;
 assert(!sweet_surface_direct(&ds[0],0x7000000,22,18,1080,2400) && ds[0].refs==1 && !sweet_direct_slots[3].surface);directQueueResult=1;
 // A display read that never ends is bounded (100 ms) and counted.
 directBusy=4;directNaps=0;unsigned forced=directRefused[FG_DD_WHY];
 directNaps=-1000000;assert(sweet_gpu_writer(&ds[1],true) && directRefused[FG_DD_WHY]==forced+1);assert(sweet_gpu_writer(&ds[1],false));
 // Fault/off: slotted surfaces the display no longer reads are given back, a busy one stays.
 directOn=0;directBusy=4;assert(!sweet_surface_direct(&ds[0],0x7000000,22,18,1080,2400) && ds[2].refs==1 && ds[1].refs==2 && sweet_direct_held==1);
 directBusy=0;assert(!sweet_surface_direct(&ds[0],0x7000000,22,18,1080,2400) && ds[1].refs==1 && !sweet_direct_held);
 puts("PASS: bit 24 SweetSurface: eligibility refusals, writer fence, page list per miss, hit without pages/clean, display-read nap and 100 ms bound, LRU give-back, off/fault drain");
 }
'''
        extra += r'''
 // fuxiearly bit 2: a writer lease waits while a queued swap still holds the surface for display (not off, not with a writer outstanding),
 // bounded by 25 ms; a quarantine during the wait refuses; the display drop wakes only when someone waits.
 {
 IOMemoryDescriptor hm;SweetSurface held;held.memory=&hm;held.displayUses=1;displayHeld=&held;
 int naps=directNaps;unsigned wakes=lockWakes;uint32_t w=9,t=9;uint64_t sum=9,max=9;
 assert(sweet_gpu_writer(&held,true) && sweet_gpu_writer(&held,false) && directNaps==naps); // off: no wait
 sweet_surface_display_wait_stats(&w,&t,&sum,&max);assert(!w && !t && !sum && !max);
 sweet_surface_display_wait(true);sleepHook=[](){displayHeld->displayUses=0;};
 uint64_t epoch=held.gpuWriterEpoch;
 assert(sweet_gpu_writer(&held,true,true) && !held.displayUses && held.gpuWriterEpoch==epoch+1 && !sweet_display_waiters);
 held.displayUses=1;sleepHook=[](){assert(!"no wait with a writer outstanding");};
 assert(sweet_gpu_writer(&held,true) && sweet_gpu_writer(&held,false) && sweet_gpu_writer(&held,false));
 sweet_surface_display_wait_stats(&w,&t,&sum,&max);assert(w==1 && !t && sum && max==sum);
 sleepHook=nullptr;naps=directNaps;epoch=held.gpuWriterEpoch;uint64_t before=ticks;
 assert(sweet_gpu_writer(&held,true) && held.gpuWriterEpoch==epoch+1 && directNaps>naps && held.displayUses==1);
 sweet_surface_display_wait_stats(&w,&t,&sum,&max);assert(w==1 && t==1 && max>=25000 && ticks-before>=25000);
 assert(sweet_gpu_writer(&held,false));
 sleepHook=[](){displayHeld->gpuQuarantined=true;};assert(!sweet_gpu_writer(&held,true));sleepHook=nullptr;
 sweet_display_wake();assert(lockWakes==wakes);
 sweet_display_waiters=1;sweet_display_wake();assert(lockWakes==wakes+1);sweet_display_waiters=0;
 sweet_surface_display_wait(false);sweet_surface_display_wait_stats(&w,&t,&sum,&max);held.displayUses=0;
 puts("PASS: writer lease display wait (fuxiearly bit 2): off/no wait, wait until the display drop, none with a writer outstanding, 25 ms bound, quarantine, wake only with waiters");
 }
'''
        model = cache.MODEL.replace(' IOLockFree(sweet_gpu_lock);', extra + ' IOLockFree(sweet_gpu_lock);')
        model = model.replace('assert(sweet_surface_gpu_poll(&elapsed)==1);IOLockLock(p);', 'if(sweet_present_count)sweet_surface_present_end();else assert(sweet_surface_gpu_poll(&elapsed)==1);IOLockLock(p);')
        compile_run(tmp, 'cache', cache.PREFIX.replace('static void IOLockWakeup(IOLock *p,void *,bool) { assert(p->held); }', 'static unsigned lockWakes;static void IOLockWakeup(IOLock *p,void *,bool) { assert(p->held);lockWakes++; }').replace(' uint64_t getLength()', ' uint64_t phys=0;uint64_t getPhysicalSegment(uint64_t off,uint64_t *len,int){*len=4096;return phys+off/4096*8192;}\n uint64_t getLength()').replace('uint64_t gpuReadyTime=0;', 'uint32_t id=0,gpuLockSeq=0,gpuWriterFence=0,width=0,height=0;uint64_t gpuReadyTime=0,gpuWriterSerial=0,gpuWriterEpoch=0,gpuCleanSerial=0,gpuCleanEpoch=0;bool gpuCleanValid=false;uint32_t displayUses=0;') + stubs + declarations + funcs + model, cpp=True)
        source = (ROOT / 'xnu/fuxi-unixnu/SweetFramebuffer.cpp').read_text()
        declaration = source[source.index('class AppleCLCD :'):source.index('OSDefineMetaClassAndStructors(AppleCLCD')]
        declaration = declaration.replace(': public IOMobileFramebuffer', '').replace('OSDeclareDefaultStructors(AppleCLCD);', 'public:')
        methods = ''.join(display.function(source, n) for n in (
            'sweet_fb_swap_bin', 'sweet_fb_te_configure', 'AppleCLCD::teVsyncTick', 'AppleCLCD::teVsyncLog',
            'AppleCLCD::presentWantedLocked', 'AppleCLCD::wakePresentLocked', 'sweet_fb_rearm',
            'AppleCLCD::vsyncFired', 'AppleCLCD::scanoutLog', 'get32', 'AppleCLCD::observeRecord', 'AppleCLCD::recordDone', 'AppleCLCD::consumeRecord', 'AppleCLCD::retireEarly', 'AppleCLCD::releaseRecord',
            'AppleCLCD::swapWait', 'AppleCLCD::stopRecords',
            'AppleCLCD::writerRetired', 'AppleCLCD::writerWake', 'AppleCLCD::writerWakeAction',
            'AppleCLCD::presentStep', 'AppleCLCD::fifoDrop', 'AppleCLCD::stageStats', 'AppleCLCD::completePresent', 'AppleCLCD::presentStats', 'AppleCLCD::presentNext', 'AppleCLCD::swap', 'AppleCLCD::present'))
        methods = re.sub(r'__asm__ volatile\s*\([^;]*\);', '(void)0;', methods)
        extra = '''
 // Pending render: retain the exact swap through idle ticks, CPU fallback and failure.
 fb.fScanout=true;fb.fUnixnuIdle=true;scanOwned=true;gpuMode=0;pollMode=1;
 surface.gpuWriters=2;offer(fb,rec);uint64_t pendingPixel=pixels[0];unsigned publications=scanPublications,ends=presentEnds,offers=surfaceCopies;
 for(unsigned i=0;i<4;i++){fb.vsyncFired(&fb,&timer);assert(fb.fDirty && fb.fTickArmed && pixels[0]==pendingPixel && scanPublications==publications && surfaceCopies==offers && refs==recordRefs(fb));}
 surface.gpuWriters--;fb.vsyncFired(&fb,&timer);assert(fb.fDirty && presentEnds==ends);
 surface.gpuWriters=0;surface.gpuQuarantined=true;fb.vsyncFired(&fb,&timer);assert(fb.fDirty && pixels[0]==pendingPixel);
 surface.gpuQuarantined=false;gpuMode=2;fb.vsyncFired(&fb,&timer);
 assert(!fb.fDirty && fb.fGpuPending && presentEnds==ends+1 && surfaceCopies==offers+1 && refs==recordRefs(fb) && !presentReading);
 pollMode=0;fb.vsyncFired(&fb,&timer);assert(scanPublications==publications);
 pollMode=1;fb.vsyncFired(&fb,&timer);assert(!fb.fGpuPending && scanPublications==publications+1);
 // CPU fallback also waits for a producer, then reads the completed contents once.
 surface.gpuWriters=1;gpuMode=0;source[0]=0x12345678;offer(fb,rec);
 fb.vsyncFired(&fb,&timer);assert(fb.fDirty && pixels[0]==pendingPixel);
 surface.gpuWriters=0;fb.vsyncFired(&fb,&timer);assert(!fb.fDirty && pixels[0]==source[0] && refs==recordRefs(fb));
 // Check the whole record before writing any layer; a ready newer record skips it.
 uint64_t source2[128];for(unsigned i=0;i<128;i++)source2[i]=0xdeadbeef;
 SweetSurface secondSurface=surface;secondSurface.address=(uintptr_t)source2;secondSurface.gpuWriters=1;inputSurface2=&secondSurface;
 uint8_t multi[SWEET_FB_SWAP_REC_SIZE];memcpy(multi,rec,sizeof(multi));put32(multi,0x104,3);put32(multi,0x58,2);
 put32(multi,0xc4+16+8,18);put32(multi,0xc4+16+12,3);pendingPixel=pixels[0];publications=scanPublications;
 offer(fb,multi);fb.vsyncFired(&fb,&timer);assert(fb.fDirty && pixels[0]==pendingPixel && scanPublications==publications && refs==recordRefs(fb));
 offer(fb,rec);fb.vsyncFired(&fb,&timer);assert(!fb.fDirty && scanPublications==publications+1 && refs==recordRefs(fb));
 secondSurface.gpuWriters=0;offer(fb,multi);fb.vsyncFired(&fb,&timer);assert(!fb.fDirty && pixels[0]==source2[0] && refs==recordRefs(fb));
 inputSurface2=nullptr;
 // Gate deferral preserves dirty swap/pixels; no publication while waiting.
 fb.fScanout=true;fb.fScanoutLog=true;scanOwned=true;scanPublish=1;gpuMode=2;pollMode=1;
 offer(fb,rec);scanAcquire=0;
 unsigned prior=scanPublications;uint64_t saved=pixels[0];
 fb.vsyncFired(&fb,&timer);assert(fb.fDirty && !fb.fGpuPending && pixels[0]==saved && scanPublications==prior);
 scanAcquire=1;fb.vsyncFired(&fb,&timer);assert(fb.fGpuPending && scanPublications==prior);
 pollMode=0;fb.vsyncFired(&fb,&timer);assert(scanPublications==prior);
 pollMode=1;fb.vsyncFired(&fb,&timer);assert(!fb.fGpuPending && scanPublications==prior+1);
 // A writer wake sees the fence, STARTs immediately and submits the next FIFO record without a timer callback.
 IOWorkLoop directLoop;WakeCall directWake={&AppleCLCD::writerWake,&fb};
 fb.fWorkLoop=&directLoop;fb.fWriterWakeCall=&directWake;scanDouble=true;
 gpuMode=2;pollMode=1;offer(fb,rec);runWake(directWake);assert(fb.fGpuPending);
 offer(fb,rec);unsigned directOffers=surfaceCopies,directStarts=scanPublications;
 uint64_t directVsync=fb.fVsyncCount;runWake(directWake);
 assert(scanPublications==directStarts+1 && surfaceCopies==directOffers+1 && fb.fGpuPending && !fb.fPendingCount && !presentReading);
 assert(fb.fVsyncCount==directVsync && fb.fStageCallbacks[2]>=2);
 // TE refusal preserves the completed image and cannot admit the next writer/record.
 scanPublish=2;offer(fb,rec);directOffers=surfaceCopies;runWake(directWake);
 assert(fb.fPublicationPending && scanBackReady && surfaceCopies==directOffers && fb.fPendingCount==1);
 scanRetryResult=1;scanPublish=1;AppleCLCD::writerRetired(&fb);runWake(directWake);
 assert(!fb.fPublicationPending && fb.fGpuPending && surfaceCopies==directOffers+1);
 scanRetryResult=0;scanDouble=false;fb.vsyncFired(&fb,&timer);fb.fWriterWakeCall=nullptr;
 // Busy GPU ring keeps the oldest swap; no CPU copy or premature transfer.
 gpuMode=3;offer(fb,rec);prior=scanPublications;fb.vsyncFired(&fb,&timer);
 assert(fb.fDirty && !fb.fGpuPending && scanPublications==prior);
 gpuMode=-1;fb.vsyncFired(&fb,&timer);assert(fb.fDirty && scanPublications==prior);
 // Bit 24: a queued direct flip is offered with no copy and no CPU guard; 3 keeps the record; a refusal copies.
 gpuMode=2;directMode=3;unsigned dcalls=directCalls,dcopies=surfaceCopies,dguards=scanCpuAcquires;fb.vsyncFired(&fb,&timer);
 assert(fb.fDirty && directCalls==dcalls+1 && surfaceCopies==dcopies && scanPublications==prior);
 directMode=2;fb.vsyncFired(&fb,&timer);
 assert(!fb.fDirty && !fb.fGpuPending && surfaceCopies==dcopies && scanCpuAcquires==dguards && scanPublications==prior+1 && !presentReading);
 directMode=0;gpuMode=-1;offer(fb,rec);prior=scanPublications;fb.vsyncFired(&fb,&timer);assert(fb.fDirty && surfaceCopies==dcopies+1 && scanPublications==prior);
 // CPU fallback cannot write until its distinct drain/restore guard grants A.
 gpuMode=0;scanCpuAcquire=0;saved=pixels[0];unsigned cpuGuards=scanCpuAcquires;
 fb.vsyncFired(&fb,&timer);assert(fb.fDirty && pixels[0]==saved && scanPublications==prior && scanCpuAcquires==cpuGuards+1);
 scanCpuAcquire=1;fb.vsyncFired(&fb,&timer);assert(!fb.fDirty && scanPublications==prior+1);
 // Power-off is gated too; the completed black frame is explicitly sent once.
 fb.fPowerOff=true;fb.fFadeStep=0;scanAcquire=0;prior=scanPublications;
 pixels[0]=0x55;fb.vsyncFired(&fb,&timer);assert(pixels[0]==0x55 && !fb.fFadeStep && scanPublications==prior);
 scanAcquire=1;scanCpuAcquire=0;fb.vsyncFired(&fb,&timer);
 assert(pixels[0]==0x55 && !fb.fFadeStep && scanPublications==prior);
 scanCpuAcquire=1;fb.vsyncFired(&fb,&timer);assert(!pixels[0] && fb.fFadeStep==1 && scanPublications==prior+1);
 // Held paths stay fail-closed even on blank requests and retirement wakes.
 fb.fScanoutHeld=true;fb.fFadeStep=0;pixels[0]=0x55;prior=scanPublications;
 fb.vsyncFired(&fb,&timer);assert(pixels[0]==0x55 && !fb.fFadeStep && scanPublications==prior);
 fb.fScanoutHeld=false;fb.fFadeStep=1;
 // A ready back buffer alone keeps demand polling alive and gets retried,
 // even with no pending swap, GPU fence, blanking or vsync subscribers.
 fb.fPowerOff=false;fb.fDirty=false;fb.fGpuPending=false;scanBackReady=true;
 unsigned retries=scanRetries,beforeArms=arms;
 assert(fb.presentWantedLocked());sweet_fb_rearm(&fb,&timer);assert(arms==beforeArms+1);
 fb.vsyncFired(&fb,&timer);assert(scanRetries==retries+1);
 scanBackReady=false;
 // Fast ownership polls must not produce extra 60 Hz notifications/counts.
 fb.fPowerOff=false;fb.fDirty=false;client.fPort=1;
 uint64_t notifications=client.notifications,count=fb.fVsyncCount;
 fb.fNextTick=ticks+100;
 fb.vsyncFired(&fb,&timer);assert(client.notifications==notifications && fb.fVsyncCount==count);
 ticks=fb.fNextTick;fb.vsyncFired(&fb,&timer);
 assert(client.notifications==notifications+1 && fb.fVsyncCount==count+1);
 client.fPort=0;
 // Periodic diagnostic branch, including actual log formatting arguments.
 fb.fScanoutFrames=119;fb.scanoutLog(false,216,42000,0);assert(fb.fScanoutFrames==120);
'''
        model = display.DISPLAY_TEST.replace(' puts("PASS: actual framebuffer', extra + ' puts("PASS: actual framebuffer')
        # Run the same framebuffer methods with the real observer too: the
        # counter is derived from the modeled panel clock, independently of
        # scheduled software deadlines. Slow copies straddle edges at 120Hz.
        observer = display.function(inc, 'fuxi_gpu_scanout_te').replace(
            'int fuxi_gpu_scanout_te(', 'static int model_scanout_te(')
        # Compile the actual observer's integer/time arithmetic for the target.
        arm = tmp / 'te-arm.c'
        arm.write_text('#include <stdint.h>\n' + display.TE_OBSERVER + '''
#define FG_SCAN_INTF 0x0ae36000u
#define FG_GUARD
static unsigned fg_scanout_double_active;
static int fg_scanout_stop(const char *reason) {(void)reason;return -1;}
static unsigned fg_scanout_period_us=16667;
int fuxi_gpu_scanout_owned(void);
int fg_scanout_read(unsigned,unsigned *);
uint64_t mach_absolute_time(void);
void nanoseconds_to_absolutetime(uint64_t,uint64_t *);
''' + display.function(inc, 'fuxi_gpu_scanout_te'))
        subprocess.run(['clang', '--target=aarch64-none-elf', '-ffreestanding',
                        '-mgeneral-regs-only', '-Wall', '-Wextra', '-Werror',
                        '-c', str(arm), '-o', str(tmp / 'te-arm.o')], check=True)
        prefix = display.DISPLAY_PREFIX.replace('static bool teAvailable;',
            'static bool teAvailable,teUseEstimator;\nstatic int model_scanout_te(struct fg_te_observer *);')
        prefix = prefix.replace(' teReads++;te->edge=teEdge;',
            ' teReads++;if(teUseEstimator)return model_scanout_te(te);te->edge=teEdge;')
        prefix += r'''
#define FG_SCAN_INTF 0u
#define FG_GUARD
static unsigned fg_scanout_double_active;
static int fg_scanout_stop(const char *reason) {(void)reason;return -1;}
static unsigned fg_scanout_period_us=16667;
static uint64_t panelOrigin,panelPeriod;
static bool panelFrozen;
static unsigned frozenFrame;
static int fg_scanout_read(unsigned address,unsigned *count) {
 assert(address==0x298);
 unsigned frame=panelFrozen?frozenFrame:(unsigned)((ticks-panelOrigin)/panelPeriod);
 *count=(frame&0xffffu)<<16;return 1;
}
''' + observer
        integrated = r'''
 {
 teUseEstimator=true;scanOwned=true;client.fPort=1;
 for(uint64_t period:{16556000ull,8278000ull})for(uint64_t offset:{7800000ull,16000000ull}) {
  AppleCLCD timed={};IOTimerEventSource beat;
  timed.fLock=&lock;timed.fClients=&clients;timed.fVsync=&beat;
  timed.fFlushTicks=1;timed.fScanout=timed.fVsyncTE=true;
  timed.fTickAbs=16667000;timed.fTeOffset=offset;
  panelOrigin=ticks+1000000;panelPeriod=period;ticks=panelOrigin;
  fg_scanout_period_us=period<10000000?8333:16667;
  timed.fNextTick=beat.next=ticks+timed.fTickAbs;
  unsigned lastFrame=~0u,locked=0,fallback=0,iterations=0;
  while(timed.fVsyncCount<650) {
   assert(++iterations<25000);
   if(ticks<beat.next)ticks=beat.next;
   uint64_t count=timed.fVsyncCount;
   timed.vsyncFired(&timed,&beat);
   if(!timed.fTeValid)lastFrame=~0u;
   if(timed.fVsyncCount==count)continue;
   if(timed.fTeValid) {
    if(lastFrame!=~0u)assert(((timed.fTeSentFrame-lastFrame)&0xffffu)==1);
    lastFrame=timed.fTeSentFrame;locked++;
    uint64_t phase=(timed.fTeLastNotify-panelOrigin)%period;
    uint64_t target=offset%period;
    uint64_t error=phase>target?phase-target:target-phase;
    if(error>period/2)error=period-error;
    assert(error<=3000000); // uncertainty grows when a copy hides the edge
    // A 3ms completed copy crosses the next TE for late offsets.
    ticks+=3000000;
   } else fallback++;
  }
  assert(locked>640 && fallback<10);
  // Freeze a real modeled counter, then allow it to advance and reacquire.
  panelFrozen=true;frozenFrame=(unsigned)((ticks-panelOrigin)/period);
  for(unsigned i=0;i<200;i++) {
   if(ticks<beat.next)ticks=beat.next;
   timed.vsyncFired(&timed,&beat);
  }
  assert(!timed.fTeValid && timed.fTeFallback>0);
  panelFrozen=false;
  for(unsigned i=0;i<200;i++) {
   if(ticks<beat.next)ticks=beat.next;
   timed.vsyncFired(&timed,&beat);
  }
  assert(timed.fTeValid);
 }
 teUseEstimator=false;scanOwned=false;client.fPort=0;
 puts("PASS: real TE observer + framebuffer timer: independent 60.4/120.8Hz panel, late offsets, 3ms copies, stall and reacquisition");
 }
'''
        model = model.replace(' puts("PASS: actual framebuffer', integrated + ' puts("PASS: actual framebuffer')
        compile_run(tmp, 'framebuffer', prefix + declaration + methods + model,
                    cpp=True, includes=(ROOT / 'xnu/fuxi-unixnu',))
    print('PASS: production scanout integration; real panel completion/cache coherency still require device qualification')
    if args.xnu_buildtree:
        kernel_syntax(args.xnu_buildtree.resolve())


if __name__ == '__main__':
    main()
