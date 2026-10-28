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
static unsigned fg_abi_coh, fg_abi_coh_wb, fg_abi_inline; static int fg_inl_usable(void) { return 0; }
#include <assert.h>
#include <stdint.h>
#include <stdlib.h>
#include <stdio.h>
#include <stdarg.h>
#include <string.h>
static char doubleStats[512],ssppLog[512];
static int scanout_printf(const char *format,...) {
 va_list args;va_start(args,format);
 if(strstr(format,"double=1 flips="))vsnprintf(doubleStats,sizeof(doubleStats),format,args);
 if(strstr(format,"sspp vig0"))vsnprintf(ssppLog,sizeof(ssppLog),format,args);
 va_end(args);return 0;
}
#define printf scanout_printf
#define FG_GUARD
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
static unsigned modelDrain,modelTe,modelPanel120,modelDouble,modelTriple,parseDouble,parseTriple,modelNc;
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
     !strcmp(name,"fuxipanel120")?modelPanel120:!strcmp(name,"fuxiscandouble")?modelDouble:!strcmp(name,"fuxiscantriple")?modelTriple:!strcmp(name,"fuxigpuncimport")?modelNc:0;
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
   assert(!fg_busy && !(intf[0x2b4/4]&0x80000000u));starts++;}
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
 memset(sspp,0,sizeof(sspp));sspp[0x14/4]=0xb8000000u;modelNc=fg_scanout_preclean=0;ssppLog[0]=0;
 fg_scanout_double_requested=fg_scanout_double_active=fg_scanout_double_initialized=fg_scanout_front=0;
 parseDouble=parseTriple=0;
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
 assert(fuxi_gpu_scanout_publish()==2 && fg_scanout_ready==0 && !fg_scanout_ready_drops);
 assert(fuxi_gpu_scanout_acquire()==1 && fg_scanout_copy==2 && fg_scanout_front==1);
 fg_busy=1;assert(!fuxi_gpu_scanout_retry() && fuxi_gpu_scanout_publish()==-1 && starts==1);
 fg_busy=0;assert(fuxi_gpu_scanout_publish()==2 && fg_scanout_ready==2 && fg_scanout_ready_drops==1);
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
 assert(fuxi_gpu_scanout_publish()>=0 && fg_scanout_ready==X);
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
            'sweet_surface_writer_serial', 'sweet_surface_present_notify', 'sweet_surface_present_reading', 'sweet_gpu_writer', 'sweet_surface_gpu_stats',
            'IOSurfaceRootUserClient::trapLock', 'IOSurfaceRootUserClient::trapUnlock'))
        stubs = '''
#include <cstdarg>
#define FUXI 1
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
unsigned sweet_surface_scanout_class(SweetSurface *,uint64_t,uint64_t bytes) {assert(bytes==2399ull*4352+1080*4);return zcClass;}
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
 assert(sweet_gpu_writer(&pendingSurface,true,true));pendingSurface.gpuQuarantined=true;
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
        model = cache.MODEL.replace(' IOLockFree(sweet_gpu_lock);', extra + ' IOLockFree(sweet_gpu_lock);')
        model = model.replace('assert(sweet_surface_gpu_poll(&elapsed)==1);IOLockLock(p);', 'if(sweet_present_count)sweet_surface_present_end();else assert(sweet_surface_gpu_poll(&elapsed)==1);IOLockLock(p);')
        compile_run(tmp, 'cache', cache.PREFIX.replace('uint64_t gpuReadyTime=0;', 'uint64_t gpuReadyTime=0,gpuWriterSerial=0,gpuWriterEpoch=0,gpuCleanSerial=0,gpuCleanEpoch=0;bool gpuCleanValid=false;') + stubs + declarations + funcs + model, cpp=True)
        source = (ROOT / 'xnu/fuxi-unixnu/SweetFramebuffer.cpp').read_text()
        declaration = source[source.index('class AppleCLCD :'):source.index('OSDefineMetaClassAndStructors(AppleCLCD')]
        declaration = declaration.replace(': public IOMobileFramebuffer', '').replace('OSDeclareDefaultStructors(AppleCLCD);', 'public:')
        methods = ''.join(display.function(source, n) for n in (
            'sweet_fb_swap_bin', 'sweet_fb_te_configure', 'AppleCLCD::teVsyncTick', 'AppleCLCD::teVsyncLog',
            'AppleCLCD::presentWantedLocked', 'AppleCLCD::wakePresentLocked', 'sweet_fb_rearm',
            'AppleCLCD::vsyncFired', 'AppleCLCD::scanoutLog', 'get32', 'AppleCLCD::observeRecord', 'AppleCLCD::recordDone', 'AppleCLCD::consumeRecord', 'AppleCLCD::retireEarly', 'AppleCLCD::releaseRecord',
            'AppleCLCD::swapWait', 'AppleCLCD::stopRecords',
            'AppleCLCD::writerRetired', 'AppleCLCD::writerWake', 'AppleCLCD::writerWakeAction',
            'AppleCLCD::presentStep', 'AppleCLCD::stageStats', 'AppleCLCD::completePresent', 'AppleCLCD::presentStats', 'AppleCLCD::presentNext', 'AppleCLCD::swap', 'AppleCLCD::present'))
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
