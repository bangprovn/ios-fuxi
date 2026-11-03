#!/usr/bin/env python3
"""Compile the governor's actual policy with boundary/LUT tests (offline)."""
from pathlib import Path
import argparse
import ast
import struct
import importlib.util
import re
import subprocess
import tempfile
ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--xnu-tree', type=Path, help="also compile this tree's actual io_map with VM stubs")
args = parser.parse_args()
spec = importlib.util.spec_from_file_location('submit', ROOT/'tools/test-fuxi-adreno.py')
submit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(submit)
actual = (ROOT/'xnu/fuxi-perf/fuxi_perf.c').read_text()
register_functions = actual[actual.index('static uint64_t fp_duty_used(uint64_t now)'):
                            actual.index('static int fp_thermal_guard(')]
register_functions += ''.join(submit.function(actual, n) for n in (
    'fp_read','fp_vote','fp_temperature','fp_init_fail','fp_init_report',
    'fp_apply_cores','fp_initialize','fp_try_initialize','fp_update_votes','fp_thermal_guard',
    'fp_arg','fp_configure','fp_worker_locked','fp_worker','fuxi_perf_input','fuxi_perf_swap'))
register_functions = "static uint64_t fp_duty_used(uint64_t);\nstatic void fp_worker_locked(thread_call_param_t,thread_call_param_t);\n" + ''.join(
    submit.function(actual, n) for n in ('fp_compile_domain','fp_compile_failed','fp_compile_cut','fp_compile_forget','fp_compile_poll','fp_compile_admitted','fp_compile_command')) + register_functions
register_functions += actual[actual.index('static int fp_compile_sysctl SYSCTL_HANDLER_ARGS'):actual.index('SYSCTL_PROC(_kern')]
register_functions += actual[actual.index('static int fp_pressure_sysctl SYSCTL_HANDLER_ARGS'):actual.index('SYSCTL_PROC(_kern, OID_AUTO, fuxipressure')]
register_functions += submit.function(actual, 'fuxi_perf_start')
# Audit the loader's zero-based wire slots and execute the host decoder without
# importing its USB dependencies. The C model below records the governor reads.
probe = (ROOT/'shim/fuxi-probe/probe.c').read_text()
assert 'for (unsigned s=0;s<th_nsens[c];s++) w[4+s]=usb_rd(th_tm[c],0xa0+4*s);' in probe
host = ast.parse((ROOT/'tools/fuxi-xnu-monitor.py').read_text())
nodes = [n for n in host.body if
         (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'TSENS_NAMES' for t in n.targets)) or
         (isinstance(n, ast.FunctionDef) and n.name == 'thermal_decode')]
namespace = {'struct': struct}
exec(compile(ast.Module(body=nodes, type_ignores=[]), '<host thermal decoder>', 'exec'), namespace)
names = namespace['TSENS_NAMES']
expected = ['cpuss-%d' % i for i in range(4)] + ['cpu-1-%d' % i for i in range(11)] + ['cpu-0-%d' % i for i in range(3)]
assert names[0][1:16] + names[1][1:4] == expected
wire = [0] * 90
wire[:2] = [0x31524854, 3]
for d in range(2):
    wire[7 + 20*d] = 8  # Observed TRDY, bit 0 clear.
    for n in range(16):
        wire[8 + 20*d + n] = (1 << 21) | (400 + 16*d + n)
decoded = namespace['thermal_decode'](struct.pack('<90I', *wire))
for d in range(2):
    for n, name in enumerate(names[d]):
        assert decoded['temps'][name] == (400 + 16*d + n) / 10
print('PASS: loader STATUS(s)=0xa0+4*s from s=0; host names/decoder zero-based CPU mapping')
TEST = r'''
#include <stdint.h>
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <stdarg.h>
#include <errno.h>
#include "fuxi_compile_abi.h"
#include "fuxi-epss-0097.h"
#include "fuxi_perf_logic.h"
#define R(l,n) ((1u<<30)|((n)<<16)|(l))
#define REGISTER_FUNCTIONS
int main(void) {
 reset();
 uint32_t rows[40] = {R(26,4),R(73,4),R(60,4),R(146,1),R(140,4),R(140,4)};
 unsigned khz;
 /* Idle searches by frequency, including nonmonotonic/turbo/terminator cases. */
 assert(fp_idle_level(rows,40,0,1401600,&khz)==0 && khz==499200);
 assert(fp_idle_level(rows,40,500000,1401600,&khz)==2 && khz==1152000);
 assert(fp_idle_level(rows,40,3000000,1401600,&khz)==1 && khz==1401600);
 assert(fp_idle_level(rows,40,0,100000,&khz)==-1 && !khz);
 uint32_t idle_rows[]={R(70,1),0,R(60,4),R(60,4),R(10,4)};
 assert(fp_idle_level(idle_rows,5,0,1401600,&khz)==1 && khz==300000);
 assert(fp_idle_level(idle_rows,5,400000,1401600,&khz)==2 && khz==1152000);
 /* Quiet time starts at ready, even with no hooks yet. Exact MS boundary. */
 assert(fp_state(0,1,450,1,1,1099,0,0,1500,1000,100,0)==FP_COOL);
 assert(fp_state(0,1,450,1,1,1100,0,50,1500,1000,100,0)==FP_IDLE);
 /* No idle before the first swap (boot-time shader compiles). */
 assert(fp_state(0,1,450,1,1,1100,0,0,1500,1000,100,0)==FP_COOL);
 assert(fp_state(0,1,450,1,1,1101,1101,0,1500,1000,100,1100)==FP_HIGH);
 assert(fp_state(0,1,450,1,1,1101,100,1101,1500,1000,100,1100)==FP_COOL);
 assert(fp_state(0,1,450,1,1,1102,100,1101,1500,1000,100,1100)==FP_COOL);
 assert(fp_state(1,1,450,1,1,1100,100,50,1500,1000,100,0)==FP_IDLE);
 assert(fp_state(0,1,700,0,1,1101,1101,0,1500,1000,100,1100)==FP_COOL);
 assert(fp_state(0,0,-400,0,1,1101,1101,0,1500,1000,100,1100)==FP_COOL);
 assert(fp_state(0,1,450,1,0,1101,1101,0,1500,1000,100,1100)==FP_COOL);
 assert(fp_state(0,1,700,0,1,1100,0,50,1500,1000,100,0)==FP_IDLE);
 assert(fp_state(0,1,450,1,1,1100,1101,0,1500,1000,100,0)==FP_COOL);
 /* Disabled policy is exactly the legacy policy across the guard boundaries. */
 for(int b=0;b<2;b++)for(int v=0;v<2;v++)for(int t=599;t<=681;t++)
 for(int safe=0;safe<2;safe++)for(int duty=0;duty<2;duty++)
 for(unsigned n=1999;n<=3501;n++)
  assert(fp_state(b,v,t,safe,duty,n,2000,2001,1500,0,100,2100)==
      (safe && duty && fp_want_boost(b,v,t,n,2000,2001,1500)?FP_HIGH:FP_COOL));
 assert(fp_level(rows,40,1401600,&khz)==1 && khz==1401600);
 assert(fp_level(rows,40,UINT32_MAX,&khz)==4 && khz==2688000);
 assert(fp_level(rows,40,100000,&khz)==-1 && !khz);
 rows[0]=0; assert(fp_level(rows,40,300000,&khz)==0 && khz==300000);
 rows[0]=R(26,1);rows[1]=R(26,1);
 assert(fp_level(rows,40,UINT32_MAX,&khz)==-1); /* turbo only */
 /* Exact temperature and inactivity boundaries, in arbitrary time units. */
 assert(!fp_want_boost(0,1,300,2000,0,1999,1500)); /* swaps cannot start boost */
 assert(fp_want_boost(0,1,600,2000,1999,0,1500));
 assert(!fp_want_boost(0,1,601,2000,1999,0,1500));
 assert(fp_want_boost(1,1,680,2000,1999,0,1500));
 assert(!fp_want_boost(1,1,681,2000,1999,0,1500));
 assert(!fp_want_boost(1,0,400,2000,1999,0,1500)); /* invalid fails cool */
 assert(fp_want_boost(0,1,400,2000,500,0,1500));
 assert(!fp_want_boost(0,1,400,2001,500,0,1500));
 assert(fp_want_boost(1,1,400,4000,500,3999,1500)); /* swaps extend */
 assert(!fp_want_boost(1,1,400,5500,500,3999,1500));
 assert(!fp_want_boost(0,1,400,2000,2001,0,1500));
 fp_start=650; /* fuxiperfstart raises only the start threshold */
 assert(fp_want_boost(0,1,650,2000,1999,0,1500) && !fp_want_boost(0,1,651,2000,1999,0,1500));
 assert(!fp_want_boost(1,1,681,2000,1999,0,1500));
 fp_start=600;
 for(unsigned d=0;d<3;d++) {
  fp_percore[d]=1; assert(fp_vote(d,7)); assert(writes[d]==fp_cores[d]);
  fp_percore[d]=0; assert(fp_vote(d,2)); assert(writes[d]==fp_cores[d]+1);
 }
 for(unsigned d=0;d<2;d++) {
  regs[d+3][0xe4/4]=8;
  for(unsigned n=1;n<=(d?3:15);n++) regs[d+3][(0xa0+4*n)/4]=(1u<<21)|450;
 }
 int temp;
 regs[3][(0xa0+4*15)/4]=(1u<<21)|681;
 assert(fp_temperature(&temp) && temp==681);
 regs[4][(0xa0+4*3)/4]=450; assert(fp_temperature(&temp) && temp==681);
 regs[4][(0xa0+4*3)/4]=(1u<<21)|0xff6; /* -1 C, signed */
 assert(fp_temperature(&temp) && temp==681);
 regs[3][0xe4/4]=0;assert(fp_temperature(&temp) && temp==681);
 /* Full init uses real frequency words/counts and stops after the first
  * repeated frequency. A fault at every later tail row must be irrelevant. */
 reset_init(); assert(fp_try_initialize(ticks) && log_count==1 && strstr(last_log,"ready"));
 for(unsigned d=0;d<3;d++) {
  assert(lut_reads[d]==observed_count[d]+1);
  assert(fp_cool[d]==(d==2?6:8));
  unsigned capped_khz;
  assert(fp_high[d]==(unsigned)fp_level(observed_rows[d],observed_count[d],fp_high_cap[d],&capped_khz));
  assert(fp_high_khz[d]==capped_khz && capped_khz<=fp_high_cap[d]);
 }
 for(unsigned d=0;d<3;d++) for(unsigned i=observed_count[d]+1;i<40;i++) {
  reset_init(); fault_block=d;fault_off=0x100+4*i;
  assert(fp_try_initialize(ticks) && log_count==1 && strstr(last_log,"ready"));
 }
 /* Every init failure emits exactly one line with the failed domain,
  * offset, value and access status. No init path may raise a vote. */
 for(unsigned d=0;d<5;d++) {
  reset_init(); fail_map=d+1;
  assert(!fp_try_initialize(ticks) && log_count==1 && strstr(last_log,d<3?"step=map":"step=tsens map"));
  assert(strstr(last_log,"value=0 access=0"));
 }
 for(unsigned d=0;d<3;d++) {
  reset_init();regs[d][0]=2;
  assert(!fp_try_initialize(ticks) && log_count==1 && strstr(last_log,"step=enable") && strstr(last_log,"value=0x2 access=1"));
  reset_init();fault_block=d;fault_off=0;
  assert(!fp_try_initialize(ticks) && log_count==1 && strstr(last_log,"step=enable") && strstr(last_log,"value=0xffffffff access=0"));
  reset_init();fault_block=d;fault_off=0xb0;
  assert(!fp_try_initialize(ticks) && log_count==1 && strstr(last_log,"step=dcvs"));
  for(unsigned i=0;i<=observed_count[d];i++) {
   reset_init();fault_block=d;fault_off=0x100+4*i;
   assert(!fp_try_initialize(ticks) && log_count==1 && strstr(last_log,"step=LUT"));
   char expect[64];snprintf(expect,sizeof(expect),"row=%u",i);assert(strstr(last_log,expect));
   snprintf(expect,sizeof(expect),"offset=%#x",fault_off);assert(strstr(last_log,expect));
   snprintf(expect,sizeof(expect),"domain=%s(%u)",fp_domain[d],d);assert(strstr(last_log,expect));
  }
  reset_init();regs[d][0x100/4]=regs[d][0x104/4]=R(26,1);
  assert(!fp_try_initialize(ticks) && log_count==1 && strstr(last_log,"step=level selection"));
 }
 reset_init();for(unsigned d=0;d<3;d++) {
  /* A full 40-row table needs no duplicate or read of slot 40. */
  for(unsigned i=0;i<40;i++) regs[d][0x100/4+i]=R(16+i,3);
  observed_tail_fault=0;
 }
 assert(fp_try_initialize(ticks));
 unsigned khz2;assert(fp_level(rows,0,UINT32_MAX,&khz2)==-1 && !khz2);
 /* Quorum, read errors, signed/plausible limits and no stale hottest value. */
 for(unsigned invalid=0;invalid<=18;invalid++) {
  reset();unsigned index=0;
  for(unsigned d=0;d<2;d++) for(unsigned n=1;n<=(d?3:15);n++,index++)
   regs[d+3][(0xa0+4*n)/4]=(index<invalid?0:1u<<21)|550;
  int good=fp_temperature(&temp);
  assert(good==(invalid<=4));
  if(good) { assert(temp==550);assert(fp_want_boost(0,good,temp,2000,1999,0,1500)); }
  else assert(!fp_want_boost(1,good,temp,2000,1999,0,1500));
 }
 reset();regs[4][0xe4/4]=0;assert(fp_temperature(&temp) && temp==450); /* TRDY is irrelevant */
 reset();regs[3][0xe4/4]=0;assert(fp_temperature(&temp) && temp==450);
 reset();fault_block=4;fault_off=0xe4;assert(fp_temperature(&temp));
 reset();fault_block=3;fault_off=0xe4;assert(fp_temperature(&temp));
 reset();fault_block=3;fault_off=0xa4;assert(fp_temperature(&temp) && temp==450);
 for(unsigned d=0;d<2;d++) for(unsigned n=1;n<=(d?3:15);n++) {
  reset();regs[d+3][(0xa0+4*n)/4]=450;
  assert(fp_temperature(&temp) && temp==450);
  assert(fp_want_boost(0,1,temp,2000,1999,0,1500));
  assert(fp_want_boost(1,1,temp,2000,1999,0,1500));
  regs[d+3][(0xa0+4*n)/4]=(1u<<21)|681;
  assert(fp_temperature(&temp) && temp==681 && !fp_want_boost(1,1,temp,2000,1999,0,1500));
 }
 reset();for(unsigned d=0;d<2;d++) for(unsigned n=1;n<=(d?3:15);n++)
  regs[d+3][(0xa0+4*n)/4]=(1u<<21)|0xff6;
 assert(fp_temperature(&temp) && temp==-10); /* all sensors -1 C, signed */
 reset();regs[3][0xa4/4]=(1u<<21)|1501;assert(fp_temperature(&temp) && temp==450);
 regs[3][0xa4/4]=(1u<<21)|((-401)&0xfff);assert(fp_temperature(&temp) && temp==450);
 regs[3][0xa4/4]=(1u<<21)|1500;assert(fp_temperature(&temp) && temp==1500);
 regs[3][0xa4/4]=(1u<<21)|((-400)&0xfff);assert(fp_temperature(&temp) && temp==450);
 /* 0097c loader sample: TRDY=0x8 and decoded CPU temperatures (dC).
  * Reconstruct VALID+temperature; these are not captured full status words. */
 reset();
 const unsigned observed_temp[2][15]={
  {477,477,473,469,465,465,473,457,465,469,457,461,481,469,477},
  {475,475,479}
 };
 for(unsigned d=0;d<2;d++) {
  regs[d+3][0xe4/4]=8;
  for(unsigned n=1;n<=(d?3:15);n++)regs[d+3][(0xa0+4*n)/4]=(1u<<21)|observed_temp[d][n-1];
 }
 assert(fp_temperature(&temp) && temp==481);
 assert(tsens_reads[0]==0xfffe && tsens_reads[1]==0xe);
 assert(fp_want_boost(0,1,temp,2000,1999,0,1500));
 /* Exercise actual worker entry, extension, hot cutoff, quorum loss and stats
  * with TRDY bit 0 clear throughout. No input still cannot enter high. */
 fp_worker(0,0);assert(fp_ready && !fp_boosted);
 ticks=101;fuxi_perf_input();fp_worker(0,0);
 assert(fp_boosted && fp_boosts==1 && fp_max_temp==481 && scheduled==121);
 ticks=121;fuxi_perf_swap();fp_worker(0,0);assert(fp_boosted);
 regs[3][(0xa0+4*13)/4]=(1u<<21)|681;
 ticks=141;fp_worker(0,0);assert(!fp_boosted && fp_cuts==1 && fp_blocked && scheduled==161);
 regs[3][(0xa0+4*13)/4]=(1u<<21)|481;
 ticks=161;fuxi_perf_input();fp_worker(0,0);assert(!fp_boosted);
 ticks=660;fp_worker(0,0);assert(!fp_boosted);
 ticks=661;fp_worker(0,0);assert(fp_boosted);
 for(unsigned n=1;n<=5;n++)regs[3][(0xa0+4*n)/4]=480;
 ticks=681;fp_worker(0,0);assert(!fp_boosted && fp_blocked && fp_cuts==2);
 for(unsigned n=1;n<=5;n++)regs[3][(0xa0+4*n)/4]=(1u<<21)|480;
 ticks=10100;fp_worker(0,0);
 assert(strstr(last_log,"stats boosts 2") && strstr(last_log,"max temp 681 dC above 620 dC 40 ms cuts 2 valid 1 state cool"));
 /* Each included CPU sensor must independently control the hottest sample.
  * Every excluded AOSS/non-CPU sensor remains irrelevant, even at 150 C. */
 for(unsigned d=0;d<2;d++) for(unsigned n=0;n<16;n++) {
  reset();regs[d+3][(0xa0+4*n)/4]=(1u<<21)|1500;
  assert(fp_temperature(&temp));
  assert(temp==((n>=1 && n<=(d?3:15))?1500:450));
  assert(tsens_reads[0]==0xfffe && tsens_reads[1]==0xe);
 }
 /* Actual worker: reproduce 0097b's gold enable=2 during AP startup.
  * Frequent input/swaps must neither write votes nor accelerate retries. */
 reset_init();regs[1][0]=2;fp_worker(0,0);
 assert(!fp_ready && fp_enabled && log_count==1 && strstr(last_log,"step=enable"));
 assert(fp_retry_at==1100 && scheduled==1100 && maps==2);
 for(unsigned t=101;t<1100;t++) {
  ticks=t;fuxi_perf_input();fuxi_perf_swap();fp_worker(0,0);
  assert(!fp_ready && !fp_retries && log_count==1 && maps==2);
  assert(!writes[0] && !writes[1] && !writes[2]);
  for(unsigned d=0;d<3;d++) assert(!fp_cool_khz[d]);
 }
 ticks=1100;fp_worker(0,0);assert(fp_retries==1 && maps==2 && log_count==1);
 regs[1][0]=3;ticks=2100;fp_worker(0,0);
 assert(fp_ready && fp_enabled && fp_retries==2 && maps==5 && log_count==2);
 assert(strstr(last_log,"ready") && strstr(last_log,"after 2 retries"));
 for(unsigned d=0;d<3;d++) assert(writes[d]);
 /* Every init failure class recovers without a vote on the failed attempt. */
 for(unsigned kind=0;kind<6;kind++) {
  reset_init();
  if(kind==0)fail_map=1;
  if(kind==1)regs[2][0]=2;
  if(kind==2){fault_block=2;fault_off=0xb0;}
  if(kind==3){fault_block=1;fault_off=0x104;}
  if(kind==4)regs[1][0x100/4]=regs[1][0x104/4]=R(26,1);
  if(kind==5)fail_map=5;
  fp_worker(0,0);assert(!fp_ready && log_count==1);
  for(unsigned d=0;d<3;d++)assert(!writes[d] && !fp_cool_khz[d]);
  fail_map=0;fault_block=-1;regs[2][0]=3;
  memcpy(&regs[1][0x100/4],observed_rows[1],sizeof(observed_rows[1]));
  ticks=1100;fp_worker(0,0);
  assert(fp_ready && fp_retries==1 && log_count==2 && strstr(last_log,"after 1 retries"));
 }
 /* Exhaustion: initial + exactly 60 retries, two diagnostic lines total.
  * The final diagnostic describes the latest failure, not the first one. */
 reset_init();regs[1][0]=2;fp_worker(0,0);regs[1][0]=3;regs[2][0]=2;
 for(unsigned n=1;n<=60;n++) {
  ticks=100+1000*n;fp_worker(0,0);
  assert(fp_retries==n && !fp_ready && log_count==(n==60?2:1));
  assert(!writes[0] && !writes[1] && !writes[2]);
 }
 assert(!fp_enabled && maps==3 && strstr(last_log,"disabled after 60 retries"));
 assert(strstr(last_log,"domain=prime(2)") && strstr(last_log,"value=0x2 access=1"));
 regs[2][0]=3;ticks+=1000;fp_worker(0,0);fuxi_perf_input();
 assert(!fp_ready && log_count==2 && !writes[0]);
 /* Success on the last permitted retry; delayed calls cannot extend 60 s. */
 reset_init();regs[1][0]=2;fp_worker(0,0);
 for(unsigned n=1;n<60;n++){ticks=100+1000*n;fp_worker(0,0);}
 regs[1][0]=3;ticks=60100;fp_worker(0,0);
 assert(fp_ready && fp_retries==60 && log_count==2 && strstr(last_log,"after 60 retries"));
 reset_init();regs[1][0]=2;fp_worker(0,0);
 regs[1][0]=3;ticks=60101;fp_worker(0,0);
 assert(!fp_enabled && !fp_ready && !fp_retries && log_count==2 && !writes[0]);
 /* Runtime unchanged high policy must still notice a dropped enable.
  * Only that domain loses boost; recovered domains receive cool only. */
 reset_init();assert(fp_try_initialize(ticks));fp_update_votes(1);
 unsigned old_writes[3];memcpy(old_writes,writes,sizeof(writes));
 regs[1][0]=2;fp_update_votes(1);
 assert(fp_domain_failed[1] && !fp_domain_failed[0] && !fp_domain_failed[2]);
 assert(!memcmp(old_writes,writes,sizeof(writes)) && log_count==2);
 fp_update_votes(1);assert(log_count==2 && writes[1]==old_writes[1]);
 regs[1][0]=3;fp_update_votes(1);
 assert(regs[1][0x320/4]==fp_cool[1] && writes[1]>old_writes[1]);
 assert(regs[2][0x320/4]==fp_high[2] && log_count==2);
 /* Failed enable reads also skip all writes, independently for each domain. */
 for(unsigned d=0;d<3;d++) {
  reset_init();assert(fp_try_initialize(ticks));fp_update_votes(1);
  unsigned before=writes[d];fault_block=d;fault_off=0;fp_update_votes(1);
  assert(fp_domain_failed[d] && writes[d]==before);
  fault_block=-1;fp_update_votes(1);assert(regs[d][0x320/4]==fp_cool[d]);
 }
 /* Enable can fall between per-core writes; later writes must be skipped. */
 reset_init();assert(fp_try_initialize(ticks));fp_percore[1]=1;drop_block=1;drop_after=1;
 assert(!fp_vote(1,fp_high[1]) && writes[1]==1 && fp_domain_failed[1]);
 assert(!(regs[1][0]&1));
 /* A returned write/readback failure fails just this domain cool. */
 for(unsigned wr=0;wr<2;wr++) {
  reset_init();assert(fp_try_initialize(ticks));fault_block=1;fault_off=0x320;
  fault_write_only=wr;fp_update_votes(1);
  assert(fp_domain_failed[1] && !fp_domain_failed[0] && !fp_domain_failed[2]);
  fault_block=-1;fp_update_votes(1);assert(regs[1][0x320/4]==fp_cool[1]);
 }
 /* Boot arguments: invalid values preserve safety bounds, and lowering the
  * ceiling clamps dependent thresholds. Units match the actual timer API. */
 reset();arg_name="fuxiperflimit";arg_value=900;fp_configure();assert(fp_limit==680);
 arg_value=800;fp_configure();assert(fp_limit==800 && fp_boost_max==800);
 assert(fp_want_boost(1,1,790,2000,1999,0,1500) && !fp_want_boost(1,1,801,2000,1999,0,1500));
 reset();arg_name="fuxiperflimit";arg_value=900;fp_configure();assert(fp_limit==680 && fp_boost_max==680);
 arg_value=550;fp_configure();assert(fp_limit==550 && fp_hot==550 && fp_resume==540);
 reset();arg_name="fuxiperfhot";arg_value=610;fp_configure();assert(fp_hot==610);
 arg_name="fuxiperfresume";arg_value=570;fp_configure();assert(fp_resume==570);
 arg_name="fuxiperfrise";arg_value=200;fp_configure();assert(fp_rise==200);
 arg_name="fuxiperffast";arg_value=25;fp_configure();assert(fp_fast_ms==25);
 arg_value=100;fp_configure();assert(fp_fast_ms==25);
 arg_name="fuxiperfbudget";arg_value=123;fp_configure();assert(fp_budget==123);
 /* Idle boot-arg bounds and optional floor do not implicitly enable idle. */
 reset();arg_name="fuxiperfidle";arg_value=999;fp_configure();assert(!fp_idle_after);
 arg_value=60001;fp_configure();assert(!fp_idle_after);
 arg_value=(unsigned)-1;fp_configure();assert(!fp_idle_after);
 arg_value=1000;fp_configure();assert(fp_idle_after==1000);
 arg_value=60000;fp_configure();assert(fp_idle_after==60000);
 reset();arg_name="fuxiperfidlekhz";arg_value=99999;fp_configure();assert(!fp_idle_floor);
 arg_value=3000001;fp_configure();assert(!fp_idle_floor);
 arg_value=100000;fp_configure();assert(fp_idle_floor==100000 && !fp_idle_after);
 arg_value=3000000;fp_configure();assert(fp_idle_floor==3000000 && !fp_idle_after);
 /* Actual LUTs and worker: no new periodic wakes; hook wakeups do no MMIO. */
 reset_init();fp_swap=50;fp_idle_ms=1000;fp_configure();log_count=0;fp_worker(0,0);
 assert(fp_ready && fp_idle_start==100 && !fp_idling && scheduled==200);
 assert(strstr(last_log,"ready idle LUT 307200/499200/595200 kHz"));
 for(unsigned d=0;d<3;d++)assert(fp_idle[d]==0 && fp_idle_khz[d]<=fp_cool_khz[d]);
 fp_swap=50; /* the first frame was already published before start */
 ticks=1099;fp_worker(0,0);assert(!fp_idling);
 ticks=1100;fp_worker(0,0);assert(fp_idling && !fp_boosted && scheduled==1200);
 assert(fp_idle_entries==1 && fp_idle_transitions==1);
 for(unsigned d=0;d<3;d++)assert(regs[d][0x320/4]==fp_idle[d]);
 unsigned before_idle_writes[3];memcpy(before_idle_writes,writes,sizeof(writes));
 ticks=1200;fuxi_perf_swap();assert(scheduled==1200 && fp_swap==1200);
 assert(!memcmp(before_idle_writes,writes,sizeof(writes)));
 fp_worker(0,0);assert(!fp_idling && !fp_boosted && fp_idle_time==100 && scheduled==1300);
 for(unsigned d=0;d<3;d++)assert(regs[d][0x320/4]==fp_cool[d]);
 for(unsigned n=0;n<100;n++) { /* ~58 Hz shimmer never starts high or idle. */
  ticks=1217+17*n;fuxi_perf_swap();fp_worker(0,0);assert(!fp_idling && !fp_boosted);
 }
 ticks=fp_swap+1000;fp_worker(0,0);assert(fp_idling);
 memcpy(before_idle_writes,writes,sizeof(writes));
 ticks++;fuxi_perf_input();assert(scheduled==ticks);
 assert(!memcmp(before_idle_writes,writes,sizeof(writes)));
 fp_worker(0,0);assert(!fp_idling && fp_boosted && scheduled==ticks+20);
 /* The old input is still within hold when a swap wakes a 1000ms idle. */
 reset_init();fp_swap=50;fp_idle_ms=1000;fp_configure();fp_worker(0,0);
 ticks=101;fuxi_perf_input();fp_worker(0,0);assert(fp_boosted);
 ticks=1101;fp_worker(0,0);assert(fp_idling);
 ticks=1102;fuxi_perf_swap();fp_worker(0,0);assert(!fp_idling && !fp_boosted);
 ticks=1103;fp_worker(0,0);assert(!fp_boosted);
 /* Hot/invalid samples still cut high; an already lower idle stays lower.
  * A recovered failed domain may also take idle, never high. */
 reset_init();fp_swap=50;fp_idle_ms=1000;fp_configure();fp_worker(0,0);
 ticks=1100;fp_worker(0,0);assert(fp_idling);
 regs[3][0xa4/4]=(1u<<21)|770;
 ticks=1200;fp_worker(0,0);assert(fp_idling && fp_blocked && scheduled==1220);
 for(unsigned d=0;d<3;d++)assert(regs[d][0x320/4]==fp_idle[d]);
 ticks=1201;fuxi_perf_input();fp_worker(0,0);assert(!fp_idling && !fp_boosted && fp_blocked);
 ticks=2201;fp_worker(0,0);assert(fp_idling);
 regs[1][0]=2;ticks++;fp_worker(0,0);assert(fp_domain_failed[1]);
 regs[1][0]=3;ticks++;fp_worker(0,0);assert(regs[1][0x320/4]==fp_idle[1]);
 ticks=10100;fp_worker(0,0);
 assert(strstr(last_log,"state idle idle 8000 ms entries 2"));
 assert(!fp_idle_time && !fp_idle_entries && fp_idle_transitions==3);
 ticks=10200;fp_worker(0,0);assert(fp_idle_time==100);
 /* Floor rounds up per domain and clamps at cool even for a huge floor. */
 for(unsigned floor=500000;floor<=3000000;floor+=2500000) {
  reset_init();fp_swap=50;fp_idle_ms=1000;fp_idle_floor=floor;fp_configure();fp_worker(0,0);
  for(unsigned d=0;d<3;d++) {
   assert(fp_idle_khz[d]<=fp_cool_khz[d]);
   assert(fp_idle_khz[d]>=(floor<fp_cool_khz[d]?floor:fp_cool_khz[d]));
  }
 }
 /* First eight idle transitions and every 64th only; counters span windows. */
 reset_init();fp_swap=50;fp_idle_ms=1000;fp_configure();fp_worker(0,0);fp_window=1000000;
 for(unsigned n=1;n<=66;n++) {
  ticks+=1000;unsigned logs=log_count;fp_worker(0,0);assert(fp_idling);
  assert(log_count==logs+(fp_idle_transitions<=8 || !(fp_idle_transitions&63)));
  ticks++;logs=log_count;fuxi_perf_swap();fp_worker(0,0);assert(!fp_idling && !fp_boosted);
  assert(log_count==logs+(fp_idle_transitions<=8 || !(fp_idle_transitions&63)));
 }
 /* Feature off leaves both log format and swap scheduling unchanged. */
 reset_init();fp_worker(0,0);ticks=100000;fp_worker(0,0);
 assert(!fp_idling && !strstr(last_log," idle "));
 uint64_t old_deadline=scheduled;ticks++;fuxi_perf_swap();assert(scheduled==old_deadline);
 /* 77 C open-loop burst profile: force all 18 sensors to the trace. No
  * thermal model is assumed; assert votes, not an impossible lowering of an
  * externally supplied temperature. Every ceiling breach cuts in <=20 ms. */
 const int burst[]={480,490,520,550,620,680,770,776,650,590,580,570};
 reset_init();ticks=81;fp_worker(0,0);
 for(unsigned i=0;i<sizeof(burst)/sizeof(burst[0]);i++) {
  ticks=101+20*i;
  for(unsigned d=0;d<2;d++)for(unsigned n=1;n<=(d?3:15);n++)
   regs[d+3][(0xa0+4*n)/4]=(1u<<21)|burst[i];
  fuxi_perf_input();fp_worker(0,0);
  if(i==0)assert(fp_boosted && scheduled==ticks+20);
  if(i>=3)assert(!fp_boosted);
  if(burst[i]>=680)for(unsigned d=1;d<3;d++)assert(regs[d][0x320/4]==fp_cool[d]);
 }
 assert(fp_cuts==1 && fp_max_temp==776);
 /* Abrupt jump at each possible phase after a fast sample: the existing
  * timer must remove high at the first sample, at most one fast period. */
 for(unsigned phase=1;phase<=20;phase++) {
  reset_init();fp_worker(0,0);ticks=101;fuxi_perf_input();fp_worker(0,0);
  assert(fp_boosted && scheduled==121);
  unsigned crossing=101+phase;ticks=scheduled;
  regs[3][0xa4/4]=(1u<<21)|770;fp_worker(0,0);
  assert(!fp_boosted && ticks-crossing<=20 && fp_cuts==1);
  for(unsigned d=1;d<3;d++)assert(regs[d][0x320/4]==fp_cool[d]);
 }
 /* Sustained 48..70 C ramp: obey timer deadlines and keep touch/swaps live.
  * Every sample at/above 62 C stays cool, without cycling near the guard. */
 reset_init();fp_worker(0,0);unsigned ramp_high=0;
 for(unsigned ms=0;ms<=22000;) {
  ticks=101+ms;int t=480+(int)(ms/100);
  for(unsigned d=0;d<2;d++)for(unsigned n=1;n<=(d?3:15);n++)
   regs[d+3][(0xa0+4*n)/4]=(1u<<21)|t;
  fuxi_perf_input();fuxi_perf_swap();fp_worker(0,0);
  if(fp_boosted)ramp_high++;
  if(t>=620)assert(fp_blocked && !fp_boosted);
  if(t>=680)for(unsigned d=1;d<3;d++)assert(regs[d][0x320/4]==fp_cool[d]);
  ms+=(unsigned)(scheduled-ticks);
 }
 assert(ramp_high);
 /* At a sustained 51..52 C the rate/temperature guards do not latch. The
  * rolling budget still bounds high time in EVERY trailing second, even
  * across nominal window boundaries and with a fractional final sample. */
 for(unsigned budget=123;budget<=200;budget+=77) {
  reset_init();fp_budget_ms=budget;arg_name=0;fp_configure();
  for(unsigned d=0;d<2;d++)for(unsigned n=1;n<=(d?3:15);n++)
   regs[d+3][(0xa0+4*n)/4]=(1u<<21)|510;
  fp_worker(0,0);
  unsigned char high[6000]={0};unsigned rolling=0,on=0;
  for(unsigned ms=0;ms<6000;ms++) {
   ticks=101+ms;
   if(!ms || ticks>=scheduled) {
    for(unsigned d=0;d<2;d++)for(unsigned n=1;n<=(d?3:15);n++)
     regs[d+3][(0xa0+4*n)/4]=(1u<<21)|(510+(ms/1000)%2*10);
    fuxi_perf_input();fuxi_perf_swap();fp_worker(0,0);
    assert(!fp_blocked);
   }
   high[ms]=!!fp_boosted;rolling+=high[ms];on+=high[ms];
   if(ms>=1000)rolling-=high[ms-1000];
   assert(rolling<=budget);
  }
  assert(on>=5*budget && on<=6*budget && fp_cuts>=5);
 }
 /* Hysteresis requires 500 continuous ms <=58 C; jitter, invalid quorum and
  * renewals do not clear it. A fast rise alone and prediction each trip. */
 reset();fp_rate_valid=1;fp_rate_temp=580;
 assert(!fp_thermal_guard(1,582,100,20000000)); /* 10 C/s, below 62 C */
 reset();fp_rate_temp=500;fp_rate_valid=1;
 assert(!fp_thermal_guard(1,540,120,20000000)); /* predicted 74 C */
 assert(!fp_thermal_guard(1,580,140,20000000));
 assert(!fp_thermal_guard(1,581,639,499000000));
 assert(!fp_thermal_guard(1,580,640,1000000));
 assert(!fp_thermal_guard(0,-400,1000,360000000));
 assert(!fp_thermal_guard(1,580,1020,20000000));
 assert(!fp_thermal_guard(1,580,1519,499000000));
 assert(fp_thermal_guard(1,580,1520,1000000));
 reset();fp_hot=680;
 assert(fp_thermal_guard(1,679,100,0));
 assert(!fp_thermal_guard(1,680,120,20000000)); /* ceiling is inclusive */
 /* 1ms input callbacks must neither invent a slope from one quantized tick
  * nor hide a real rise by continually resetting the derivative history. */
 reset();assert(fp_thermal_guard(1,510,100,0));
 for(unsigned i=1;i<=20;i++)assert(fp_thermal_guard(1,514,100+i,1000000));
 assert(fp_rate==200 && !fp_blocked); /* 0.4 C / 20 ms, still below 55 C */
 reset();assert(fp_thermal_guard(1,550,100,0));
 for(unsigned i=1;i<20;i++)assert(fp_thermal_guard(1,550+i,100+i,1000000));
 assert(!fp_thermal_guard(1,570,120,1000000));
 /* fuxicores: applied once at ready, only when parsed as a valid mask. */
 recommend_calls=0;fp_recommend=0;fp_apply_cores();assert(!recommend_calls);
 fp_recommend=0x7c;fp_apply_cores();assert(recommend_calls==1 && recommended==0x7c);fp_recommend=0;
 /* Default 200 ms slope window: one 0.4 C TSENS step at 56 C never trips,
  * a sustained 1 C/s rise above 55 C still does. */
 reset();fp_rate_ms=200;assert(fp_thermal_guard(1,560,100,0));
 for(unsigned i=1;i<=10;i++)assert(fp_thermal_guard(1,i<5?560:564,100+20*i,20000000));
 assert(fp_rate==20 && !fp_blocked);
 for(unsigned i=1;i<=10;i++)fp_thermal_guard(1,564+2*i,300+20*i,20000000);
 assert(fp_rate==100 && fp_blocked);
 /* Raw ABI: disabled/authorization precede payload parsing, exact width,
  * copy faults, optional reply, and failed copyout rolls back admission. */
 struct fuxi_compile_reply wire_reply;
 int32_t command=1;
 struct sysctl_req req={&command,&wire_reply,sizeof(command),sizeof(wire_reply)};
 reset();assert(fp_compile_sysctl(&req)==ENOTSUP);
 fp_compile_enabled=1;platform=0;assert(fp_compile_sysctl(&req)==EPERM);platform=1;
 req.newptr=0;assert(fp_compile_sysctl(&req)==EINVAL);req.newptr=&command;
 req.newlen=8;assert(fp_compile_sysctl(&req)==EINVAL);req.newlen=sizeof(command);
 req.oldlen=1;assert(fp_compile_sysctl(&req)==ENOMEM);req.oldlen=sizeof(wire_reply);
 copyin_error=EFAULT;assert(fp_compile_sysctl(&req)==EFAULT && !refs);copyin_error=0;
 copyout_error=EFAULT;assert(fp_compile_sysctl(&req)==EFAULT && !refs && bound==-1);copyout_error=0;
 assert(!fp_compile_sysctl(&req) && refs==1 && wire_reply.version==1 && wire_reply.cpu==7);
 command=0;req.oldptr=0;assert(!fp_compile_sysctl(&req) && !refs && bound==-1);
 /* Compile lease: actual command, shared worker/thermal/duty, current-thread
  * ownership, balanced references, nested expiry and failed admissions. */
 struct fuxi_compile_reply reply;
 reset();assert(fp_compile_command(1,&reply)==ENOTSUP && bound==-1);
 fp_compile_enabled=1;fp_enabled=0;assert(fp_compile_command(1,&reply)==ENOTSUP);fp_enabled=1;
 platform=0;assert(fp_compile_command(1,&reply)==EPERM);platform=1;
 process_name="other";assert(fp_compile_command(1,&reply)==EPERM);process_name="backboardd";
 proc_present=0;assert(fp_compile_command(1,&reply)==EPERM);proc_present=1;
 assert(fp_compile_command(2,&reply)==EINVAL);
 assert(!fp_compile_command(0,&reply) && reply.state==FC_NONE && !refs);
 assert(!fp_compile_command(1,&reply) && reply.cpu==7 && reply.state==FC_ACTIVE);
 assert(fp_compile_high && fp_boosted && refs==1 && bound==7 && migrations==1);
 assert(regs[2][0x320/4]==fp_high[2] && regs[1][0x320/4]==fp_cool[1]);
 uint64_t original_deadline=fp_compile_deadline;
 ticks+=50;assert(!fp_compile_command(1,&reply) && reply.depth==2 && fp_compile_deadline==original_deadline);
 assert(!fp_compile_command(0,&reply) && reply.depth==1 && bound==7);
 caller=2;assert(fp_compile_command(1,&reply)==EBUSY);
 assert(!fp_compile_command(0,&reply) && reply.state==FC_NONE && refs==1 && bound==7);caller=1;
 ticks+=50;assert(!fp_compile_command(0,&reply) && reply.state==FC_LEFT && reply.boosted_ns==100*NSEC_PER_MSEC);
 assert(!refs && bound==-1 && !fp_boosted && regs[2][0x320/4]==fp_cool[2]);
 assert(!fp_compile_command(0,&reply) && reply.state==FC_NONE);
 /* Realtime caller (the CA render thread): unbound lease, no migration,
  * high on both gold and prime; leave never unbinds. */
 realtime=1;assert(!fp_compile_command(1,&reply) && reply.cpu==-1 && reply.state==FC_ACTIVE);
 assert(fp_compile_high && refs==1 && bound==-1 && migrations==1);
 assert(regs[2][0x320/4]==fp_high[2] && regs[1][0x320/4]==fp_high[1]);
 ticks+=50;assert(!fp_compile_command(0,&reply) && reply.state==FC_LEFT && !refs && bound==-1);
 assert(regs[2][0x320/4]==fp_cool[2] && regs[1][0x320/4]==fp_cool[1]);realtime=0;
 fuxi_perf_swap();fp_worker(0,0);assert(!fp_boosted); /* compile cannot seed touch's swap extension */
 reset();fp_compile_enabled=1;fuxi_perf_input();fp_worker(0,0);
 assert(!fp_compile_command(1,&reply) && fp_touch_high);
 assert(!fp_compile_command(0,&reply) && fp_boosted && fp_touch_high && bound==-1);
 assert(regs[1][0x320/4]==fp_high[1] && regs[2][0x320/4]==fp_high[2]);
 /* Default rolling duty ends the vote and binding at 200 ms, keeps a bounded
  * reply for leave, and cannot be renewed by a nested enter. */
 reset();fp_compile_enabled=1;assert(!fp_compile_command(1,&reply));
 ticks+=199;fp_worker(0,0);assert(fp_compile_high && scheduled==300);
 ticks++;fp_worker(0,0);assert(!fp_compile_high && bound==-1 && !fp_boosted);
 assert(fp_compile_reply.state==FC_DUTY && refs==1);
 assert(fp_compile_command(1,&reply)==EBUSY);
 assert(!fp_compile_command(0,&reply) && reply.state==FC_DUTY && reply.boosted_ns==200*NSEC_PER_MSEC && !refs);
 assert(fp_compile_command(1,&reply)==EBUSY); /* no reset by leave */
 ticks+=1000;assert(!fp_compile_command(1,&reply));
 assert(!fp_compile_command(0,&reply));
 /* Timeout independent of duty, even with nested scopes and no leave. */
 reset();fp_compile_enabled=1;fp_budget=10000;
 assert(!fp_compile_command(1,&reply));
 for(unsigned i=1;i<16;i++)assert(!fp_compile_command(1,&reply));
 assert(fp_compile_command(1,&reply)==EOVERFLOW && fp_compile_reply.depth==16);
 ticks=2099;fp_worker(0,0);assert(bound==7 && scheduled==2100);
 ticks=2100;fp_worker(0,0);assert(!refs && bound==-1 && !fp_compile_high);
 assert(!fp_compile_command(0,&reply) && reply.state==FC_NONE);
 /* Terminated owner is referenced until the worker unbinds/deallocates. */
 reset();fp_compile_enabled=1;assert(!fp_compile_command(1,&reply));
 alive=0;ticks+=20;fp_worker(0,0);
 assert(!refs && !fp_compile_thread && bound==-1 && !fp_compile_high && !fp_boosted);
 /* Gold fallback when prime EPSS failed; no silver or monitor vote. */
 reset();fp_compile_enabled=1;fp_domain_failed[2]=1;
 assert(!fp_compile_command(1,&reply) && reply.cpu==6);
 assert(regs[1][0x320/4]==fp_high[1] && regs[2][0x320/4]==fp_cool[2]);
 assert(!fp_compile_command(0,&reply));
 reset();fp_compile_enabled=1;assert(!fp_compile_command(1,&reply));
 regs[2][0]=2;ticks+=20;fp_worker(0,0);
 assert(fp_domain_failed[2] && !fp_compile_high && bound==-1);
 assert(!fp_compile_command(0,&reply) && reply.state==FC_OFFLINE && !refs);
 for(unsigned reason=FC_THERMAL;reason<=FC_OFFLINE;reason++) {
  if(reason!=FC_THERMAL && reason!=FC_OFFLINE)continue;
  reset();fp_compile_enabled=1;assert(!fp_compile_command(1,&reply));
  if(reason==FC_THERMAL)regs[3][0xa4/4]=(1u<<21)|620;else online=0;
  ticks+=20;fp_worker(0,0);assert(bound==-1 && !fp_compile_high);
  assert(!fp_compile_command(0,&reply) && reply.state==reason && !refs);
 }
 for(unsigned temp=600;temp<=601;temp++) {
  reset();fp_compile_enabled=1;
  for(unsigned d=0;d<2;d++)for(unsigned n=1;n<=(d?3:15);n++)regs[d+3][(0xa0+4*n)/4]=(1u<<21)|temp;
  int result=fp_compile_command(1,&reply);
  assert(result==(temp==600?0:EBUSY));
  if(!result)assert(!fp_compile_command(0,&reply));
 }
 reset();fp_compile_enabled=1;bind_temp=601;
 assert(!fp_compile_command(1,&reply) && reply.state==FC_THERMAL && !fp_compile_high && bound==-1);
 assert(!fp_compile_command(0,&reply) && reply.boosted_ns==0 && !refs); /* fresh pre-vote sample wins */
 /* Lease overrides idle only in its own domain, and touch still votes both
  * fast domains. Thermal/quorum veto applies to both sources together. */
 reset();fp_idle_ms=1000;fp_idle_floor=300000;fp_configure();fp_compile_enabled=1;
 fp_worker(0,0);fp_swap=100; ticks=1200;fp_worker(0,0);assert(fp_idling);
 assert(!fp_compile_command(1,&reply) && !fp_idling && fp_compile_high);
 ticks+=20;fuxi_perf_input();fp_worker(0,0);assert(fp_touch_high && regs[1][0x320/4]==fp_high[1]);
 for(unsigned n=1;n<=5;n++)regs[3][(0xa0+4*n)/4]=450;
 ticks+=20;fp_worker(0,0);assert(!fp_boosted && !fp_compile_high && bound==-1);
 assert(!fp_compile_command(0,&reply) && reply.state==FC_THERMAL && !refs);
 /* Pressure policy: inclusive rising trips, exact falling hysteresis,
  * invalid quorum, predictive latch, lowered thresholds and no sleeping. */
 assert(FP_TP_NOMINAL==0 && FP_TP_MODERATE==1 && FP_TP_HEAVY==2 && FP_TP_TRAPPING==3 && FP_TP_SLEEPING==4);
 assert(fp_pressure_level(0,1,619,0,620,680,580)==0);
 assert(fp_pressure_level(0,1,620,1,620,680,580)==1);
 assert(fp_pressure_level(0,1,550,1,620,680,580)==1);
 assert(fp_pressure_level(1,1,581,0,620,680,580)==1);
 assert(fp_pressure_level(1,1,580,0,620,680,580)==0);
 assert(fp_pressure_level(0,1,679,1,620,680,580)==1);
 assert(fp_pressure_level(0,1,680,1,620,680,580)==2);
 assert(fp_pressure_level(2,1,641,1,620,680,580)==2);
 assert(fp_pressure_level(2,1,640,1,620,680,580)==1);
 assert(fp_pressure_level(0,1,769,1,620,680,580)==2);
 assert(fp_pressure_level(0,1,770,1,620,680,580)==3);
 assert(fp_pressure_level(3,1,731,1,620,680,580)==3);
 assert(fp_pressure_level(3,1,730,1,620,680,580)==2);
 { uint64_t since=0;
  assert(fp_pressure_hold(-1,0,5,&since,10)==0 && since==5);
  assert(fp_pressure_hold(0,1,6,&since,10)==1 && since==6);
  assert(fp_pressure_hold(1,0,15,&since,10)==1 && since==6);
  assert(fp_pressure_hold(1,2,15,&since,10)==2 && since==15);
  assert(fp_pressure_hold(2,0,24,&since,10)==2);
  assert(fp_pressure_hold(2,0,25,&since,10)==0 && since==25);
  assert(fp_pressure_hold(0,0,26,&since,10)==0 && since==25); }
 for(int prev=-1;prev<=3;prev++) {
  assert(fp_pressure_level(prev,0,-400,1,620,680,580)==(prev==3?3:2));
  assert(fp_pressure_level(prev,1,450,0,620,680,580)==0);
 }
 assert(fp_pressure_level(0,1,450,1,400,450,350)==2);
 assert(fp_pressure_level(2,1,410,1,400,450,350)==1);
 assert(fp_pressure_level(1,1,350,0,400,450,350)==0);
 for(int temp=-400;temp<=1500;temp++)for(int prev=-1;prev<=3;prev++)
  assert(fp_pressure_level(prev,1,temp,temp>=620,620,680,580)<FP_TP_SLEEPING);
 /* Actual worker leaves pressure untouched with opt-in off. Early reads
  * return EBUSY, disabled reads ENOTSUP, and no sysctl performs MMIO. */
 reset();int pressure_out=-99;
 req=(struct sysctl_req){.oldptr=&pressure_out,.oldlen=sizeof(pressure_out)};
 assert(fp_pressure_sysctl(&req)==ENOTSUP);
 fp_worker(0,0);assert(fp_pressure==-1);
 fp_pressure_enabled=1;assert(fp_pressure_sysctl(&req)==EBUSY);
 fp_enabled=0;assert(fp_pressure_sysctl(&req)==ENOTSUP);fp_enabled=1;
 req.newptr=&command;assert(fp_pressure_sysctl(&req)==EPERM);req.newptr=0;
 fp_worker(0,0);assert(!fp_pressure_sysctl(&req) && pressure_out==0);
 copyout_error=EFAULT;assert(fp_pressure_sysctl(&req)==EFAULT);copyout_error=0;
 unsigned reads0=tsens_reads[0],reads1=tsens_reads[1];
 assert(!fp_pressure_sysctl(&req) && reads0==tsens_reads[0] && reads1==tsens_reads[1]);
 regs[3][0xa4/4]=(1u<<21)|620;ticks+=20;fp_worker(0,0);
 assert(fp_blocked && fp_pressure==1);
 regs[3][0xa4/4]=(1u<<21)|680;ticks+=20;fp_worker(0,0);assert(fp_pressure==2);
 regs[3][0xa4/4]=(1u<<21)|770;ticks+=20;fp_worker(0,0);assert(fp_pressure==3);
 for(unsigned n=1;n<=5;n++)regs[3][(0xa0+4*n)/4]=450;
 ticks+=20;fp_worker(0,0);assert(fp_pressure==3); /* loss never relaxes */
 for(unsigned n=1;n<=5;n++)regs[3][(0xa0+4*n)/4]=(1u<<21)|450;
 ticks+=20;fp_worker(0,0);assert(fp_pressure==1 && fp_blocked);
 ticks+=499;fp_worker(0,0);assert(fp_pressure==1);
 ticks++;fp_worker(0,0);assert(fp_pressure==0 && !fp_blocked);
 /* Boot arguments: only exact 1 enables; fuxitp alone starts no worker. */
 reset();fp_enabled=0;arg_name="fuxitp";arg_value=1;fuxi_perf_start();
 assert(!fp_enabled && !fp_started && !fp_pressure_enabled);
 reset();fp_enabled=0;arg_name="fuxiperf";arg_value=1;fuxi_perf_start();
 assert(fp_enabled && fp_started && !fp_pressure_enabled);
 const unsigned tp_args[]={0,1,2,UINT32_MAX};
 for(unsigned i=0;i<4;i++) {
  reset();fp_enabled=0;arg_name="fuxiperf";arg_value=1;
  arg_name2="fuxitp";arg_value2=tp_args[i];fuxi_perf_start();
  assert(fp_enabled && fp_started && fp_pressure_enabled==(tp_args[i]==1));
  assert(fp_pressure==-1); /* start never fabricates a sample */
  fp_worker(0,0);assert(fp_pressure==(tp_args[i]==1?0:-1));
 }
 /* fpk: bounds 1401 up to the cap (fpc, else 2400); rows on the observed
  * LUTs; the raised base only with a valid sample and no thermal latch,
  * compiled cool when hot or without quorum and again after the 500 ms
  * recovery; touch boost, silver and the window line. */
 const unsigned fpk_args[6][3]={{1400,0,0},{1401,1401,0},{2400,2400,0},{2401,0,0},{1900,0,1800},{1800,1800,1800}};
 for(unsigned i=0;i<6;i++) {
  reset();arg_name="fpk";arg_value=fpk_args[i][0];
  arg_name2=fpk_args[i][2]?"fpc":0;arg_value2=fpk_args[i][2];fp_configure();
  assert(fp_base_mhz==fpk_args[i][1]);
 }
 arg_name=arg_name2=0;
 const unsigned fpk_rows[3][5]={{1401,8,1401600,6,1363200},{1800,11,1785600,9,1708800},{2400,15,2323200,14,2342400}};
 for(unsigned i=0;i<3;i++) {
  reset_init();arg_name="fpk";arg_value=fpk_rows[i][0];fp_configure();arg_name=0;fp_worker(0,0);
  assert(fp_base[1]==fpk_rows[i][1] && fp_base_khz[1]==fpk_rows[i][2]);
  assert(fp_base[2]==fpk_rows[i][3] && fp_base_khz[2]==fpk_rows[i][4] && fp_base[0]==fp_cool[0]);
  assert(fp_base[1]<=fp_high[1] && fp_base[2]<=fp_high[2]);
 }
 reset_init();arg_name="fpk";arg_value=1800;fp_configure();arg_name=0;log_count=0;fp_worker(0,0);
 assert(log_count==2 && strstr(last_log,"ready base fpk=1800: gold/prime 1785600/1708800 kHz while thermally safe, else compiled cool 1401600/1363200 kHz"));
 assert(fp_base_on && regs[1][0x320/4]==11 && regs[2][0x320/4]==9 && regs[0][0x320/4]==fp_cool[0] && scheduled==200);
 ticks=101;fuxi_perf_input();fp_worker(0,0);assert(fp_boosted && regs[1][0x320/4]==fp_high[1]);
 ticks=301;fp_worker(0,0);assert(!fp_boosted && fp_base_on && regs[1][0x320/4]==11 && scheduled==321);
 ticks=1700;fp_worker(0,0);assert(!fp_boosted && regs[1][0x320/4]==11 && regs[2][0x320/4]==9 && scheduled==1800);
 all_temp(620);ticks=1720;fp_worker(0,0);
 assert(fp_blocked && !fp_base_on && regs[1][0x320/4]==fp_cool[1] && regs[2][0x320/4]==fp_cool[2] && scheduled==1740);
 all_temp(570);ticks=1740;fp_worker(0,0);assert(fp_blocked && regs[1][0x320/4]==fp_cool[1]);
 ticks=2239;fp_worker(0,0);assert(fp_blocked && !fp_base_on);
 ticks=2240;fp_worker(0,0);assert(!fp_blocked && fp_base_on && regs[1][0x320/4]==11 && regs[2][0x320/4]==9);
 for(unsigned n=1;n<=5;n++)regs[3][(0xa0+4*n)/4]=570; /* 13 of 18 valid: no quorum */
 ticks=2340;fp_worker(0,0);assert(!fp_base_on && regs[1][0x320/4]==fp_cool[1] && regs[2][0x320/4]==fp_cool[2]);
 ticks=10100;fp_worker(0,0);
 assert(strstr(last_log,"base fpk=1800 raised 1720 ms compiled 8280 ms (hot, latched or no valid sample)"));
 arg_name=arg_name2=0;
#undef printf
 puts("PASS: fpk default off, 1401..cap bounds (fpc), base rows 1401/1800/2400 on real LUTs, raised only with valid sample and no thermal latch, compiled cool when hot/no quorum, 500 ms recovery, boost/silver unchanged, fast samples only while active, window line");
 puts("PASS: thermal pressure opt-in, read-only sysctl/readiness/no-MMIO, guard/quorum integration, Apple wire values, rising trips/falling hysteresis and no sleeping");
 puts("PASS: compile gate/default off, prime/gold votes, idle/touch precedence, thermal/quorum/duty cuts, cap, nested limit/no renewal, leave without enter, cross-thread isolation, dead-thread cleanup and balanced references");
 puts("PASS: opt-in idle boundaries, LUT floor/clamp, legacy equivalence, hook wakeups, thermal/duty precedence, stats and transition rate limit");
 puts("PASS: predictive/rise/62C cutoff, 68C ceiling votes within 20ms of 77C step at every phase, sustained ramp, 51-52C load, 500ms hysteresis, rolling 123/200ms duty, boot-arg bounds and actual timer deadlines");
 puts("PASS: observed TRDY=8/48.1 C worker boost/hot cutoff/quorum/stats; exact CPU sensor read masks; actual worker 60s retries/input gating/no-init-votes/domain enable loss and recovery; real 0097 LUTs/init failures/16K mappings/14-of-18 sensor quorum; LUT decode/duplicates/turbo/nonmonotonic selection, legacy eligibility 60/68 boundaries, input-only entry, swap extension, 1500 ms expiry, per-core vote/readback and signed/invalid CPU sensors");
}
'''
REGISTERS = r'''
static uint32_t regs[5][1024], writes[3], lut_reads[3], tsens_reads[2];
static uintptr_t fp_epss[3]={0x1000,0x2000,0x3000},fp_tsens[2]={0x4000,0x5000};
static const uint64_t fp_epss_pa[3]={0x17d91000,0x17d92000,0x17d93000};
static const uint64_t fp_tsens_pa[2]={0xc271000,0xc272000};
static const char * const fp_domain[3]={"silver","gold","prime"};
static unsigned fp_percore[3],fp_cool[3],fp_high[3],fp_cool_khz[3],fp_high_khz[3];
POLICY_CAPS
static unsigned maps,fail_map,log_count,observed_tail_fault;
static unsigned fp_domain_failed[3],fp_applied[3],fp_applied_valid[3];
static uint64_t ticks,fp_retry_interval,fp_retry_at,fp_retry_end;
static unsigned fp_attempted,fp_retries,fp_enabled,fp_ready,fp_started;
static struct { const char *step; unsigned domain,offset; uint64_t pa; uint32_t value; int access,row; } fp_diagnostic;
static uint64_t fp_input,fp_swap,fp_hold,fp_last,fp_window,fp_window_start,fp_boost_time,fp_boosts;
static int fp_max_temp,fp_boosted;
IDLE_GLOBALS
THERMAL_GLOBALS
static const char *arg_name, *arg_name2;
static unsigned arg_value, arg_value2;
static int PE_parse_boot_argn(const char *name,void *value,unsigned size) {
 assert(size==sizeof(unsigned));
 if(arg_name && !strcmp(name,arg_name)) { *(unsigned *)value=arg_value;return 1; }
 if(arg_name2 && !strcmp(name,arg_name2)) { *(unsigned *)value=arg_value2;return 1; }
 return 0;
}
typedef void *thread_call_param_t;
struct sysctl_req {void *newptr,*oldptr;size_t newlen,oldlen;};
#define SYSCTL_HANDLER_ARGS (struct sysctl_req *req)
static int copyin_error,copyout_error;
static int model_sysctl_in(struct sysctl_req *req,void *out,size_t n){if(copyin_error)return copyin_error;memcpy(out,req->newptr,n);return 0;}
static int model_sysctl_out(struct sysctl_req *req,const void *in,size_t n){if(copyout_error)return copyout_error;memcpy(req->oldptr,in,n);return 0;}
#define SYSCTL_IN model_sysctl_in
#define SYSCTL_OUT model_sysctl_out
static int fp_call;
#define THREAD_CALL_PRIORITY_KERNEL 0
#define THREAD_CALL_OPTIONS_ONCE 1
static void thread_call_setup_with_options(int *call,void (*worker)(thread_call_param_t,thread_call_param_t),void *arg,int priority,int options) {
 assert(call==&fp_call && worker && !arg && !priority && options==1);
}
typedef uintptr_t thread_t;
#define THREAD_NULL 0
static unsigned fp_compile_enabled,fp_compile_high,fp_compile_voted,fp_touch_high,fp_compile_refusals;
static thread_t fp_compile_thread;
static uint64_t fp_compile_deadline,fp_compile_cap,fp_compile_start;
static struct fuxi_compile_reply fp_compile_reply;
static int fp_lock,locked,platform=1,proc_present=1,alive=1,online=1,bound=-1,refs,migrations,bind_temp,realtime;
static thread_t caller=1;
static const char *process_name="backboardd";
typedef int proc_t;
static void proc_selfname(char *out,unsigned n){snprintf(out,n,"%s",process_name);}
static proc_t proc_self(void){return proc_present;}
static int csproc_get_platform_binary(proc_t p){assert(p==1);return platform;}
static void proc_rele(proc_t p){assert(p==1);}
static void lck_mtx_lock(int *p){assert(p==&fp_lock && !locked);locked=1;}
static void lck_mtx_unlock(int *p){assert(p==&fp_lock && locked);locked=0;}
static thread_t current_thread(void){return caller;}
static void thread_reference(thread_t t){assert(t);refs++;}
static void thread_deallocate(thread_t t){assert(t && refs>0);refs--;}
static int fuxi_compile_alive(thread_t t){assert(t);return alive;}
static int fuxi_compile_online(int cpu){assert(cpu>=3 && cpu<=7);return online;}
static int fuxi_compile_bind(unsigned domains){if(realtime)return -2;if(!online || bound!=-1 || !domains)return -1;bound=domains&4?7:6;if(bind_temp)regs[3][0xa4/4]=(1u<<21)|bind_temp;return bound;}
static void fuxi_compile_migrate(void){assert(!locked);migrations++;}
static unsigned fp_recommend,recommended,recommend_calls;
static void sched_perfcontrol_update_recommended_cores(uint32_t m){recommended=m;recommend_calls++;}
static void fuxi_compile_unbind(thread_t t,int cpu){assert(t && bound==cpu);bound=-1;}

static uint64_t scheduled;
#define NSEC_PER_MSEC 1000000ULL
#define NSEC_PER_SEC 1000000000ULL
static uint64_t mach_absolute_time(void) { return ticks; }
static void absolutetime_to_nanoseconds(uint64_t t,uint64_t *ns) { *ns=t*NSEC_PER_MSEC; }
static void nanoseconds_to_absolutetime(uint64_t ns,uint64_t *t) { *t=ns/NSEC_PER_MSEC; }
static void clock_interval_to_deadline(unsigned n,unsigned unit,uint64_t *t) { *t=ticks+(uint64_t)n*unit/NSEC_PER_MSEC; }
static int thread_call_enter_delayed(int *call,uint64_t t) { (void)call;scheduled=t;return 0; }
static int thread_call_enter(int *call) { (void)call;scheduled=ticks;return 0; }
static int fault_block=-1;
static unsigned fault_off,fault_write_only,drop_after;
static int drop_block=-1;
static char last_log[512];
/* Model exactly io_map's trunc_page / round_page / start+offset contract;
 * use separate virtual 16K pages, even for aliases of the same physical page. */
static uintptr_t ml_io_map(uint64_t pa,unsigned size) {
 assert(size==0x1000);unsigned d;
 for(d=0;d<5;d++) if(pa==(d<3?fp_epss_pa[d]:fp_tsens_pa[d-3])) break;
 assert(d<5);
 unsigned offset=pa&0x3fff;
 assert(((size+offset+0x3fff)&~0x3fff)==0x4000);
 if(++maps==fail_map)return 0;
 return 0x100000+0x4000*d+offset;
}
static int model_printf(const char *fmt,...) {
 va_list ap;va_start(ap,fmt);vsnprintf(last_log,sizeof(last_log),fmt,ap);va_end(ap);log_count++;return 0;
}
#define printf model_printf
static unsigned fuxi_gpu_access(uintptr_t address,uint64_t *v,unsigned wr,unsigned wide) {
 assert(!wide);unsigned block,off;
 if(address<0x100000) {block=(address>>12)-1;off=address&0xfff;}
 else {
  block=(address-0x100000)/0x4000;
  assert(block<5);
  unsigned offset=(block<3?fp_epss_pa[block]:fp_tsens_pa[block-3])&0x3fff;
  assert((address&0x3fff)>=offset);
  off=(address&0x3fff)-offset;
 }
 assert(block<5 && off<0x1000 && !(off&3));
 if(block>=3) {
  assert(!wr && off>=0xa0 && off<0xe0); /* Never read TRDY, including on faults. */
  tsens_reads[block-3]|=1u<<((off-0xa0)/4);
 }
 if((int)block==fault_block && off==fault_off && (!fault_write_only || wr))return 1;
 if(block<3 && off>=0x100 && off<0x1a0) {
  unsigned row=(off-0x100)/4;lut_reads[block]++;
  if(observed_tail_fault && row>observed_count[block])return 1;
 }
 if(wr) { assert(block<3 && (regs[block][0]&1));writes[block]++;regs[block][off/4]=*v; if((int)block==drop_block && writes[block]==drop_after)regs[block][0]&=~1u; }
 else *v=regs[block][off/4];
 return 0;
}
static void fp_configure(void);
static void reset(void) {
 assert(!refs && !locked);
 fp_compile_enabled=fp_compile_high=fp_compile_voted=fp_touch_high=0;fp_compile_thread=THREAD_NULL;
 fp_compile_cap=2000;fp_compile_deadline=fp_compile_start=0;
 memset(&fp_compile_reply,0,sizeof(fp_compile_reply));
 platform=proc_present=alive=online=1;bound=-1;caller=1;process_name="backboardd";migrations=bind_temp=0;
 fp_hot=620;fp_limit=680;fp_boost_max=680;fp_resume=580;fp_rise=100;fp_fast_ms=20;fp_rate_ms=20;fp_budget_ms=200;
 fp_idle_ms=fp_idle_floor=fp_idling=0;
 fp_idle_start=fp_idle_entry=fp_idle_time=fp_idle_entries=fp_idle_transitions=0;
 fp_cap_mhz=fp_base_mhz=0;fp_base_on=0;fp_base_time=fp_base_fallback_time=0;
 memset(fp_base,0,sizeof(fp_base));memset(fp_base_khz,0,sizeof(fp_base_khz));
 arg_name=arg_name2=0;fp_configure();
 fp_recover_since=fp_above_time=fp_cuts=fp_active_start=0;
 fp_duty_count=0;fp_prev_temp=fp_prev_valid=fp_blocked=0;
 fp_pressure_enabled=fp_started=0;fp_pressure=-1;
 fp_rate_elapsed=0;fp_rate_temp=fp_rate_valid=fp_rate=0;
 memset(tsens_reads,0,sizeof(tsens_reads));
 memset(regs,0,sizeof(regs));memset(writes,0,sizeof(writes));memset(lut_reads,0,sizeof(lut_reads));
 memset(fp_cool_khz,0,sizeof(fp_cool_khz));memset(fp_high_khz,0,sizeof(fp_high_khz));
 memset(fp_domain_failed,0,sizeof(fp_domain_failed));memset(fp_applied_valid,0,sizeof(fp_applied_valid));
 fp_attempted=fp_retries=fp_ready=0;fp_enabled=1;fp_retry_interval=1000;fp_retry_at=fp_retry_end=0;ticks=100;
 fp_input=fp_swap=fp_last=fp_window_start=fp_boost_time=fp_boosts=0;
 fp_hold=1500;fp_window=10000;fp_max_temp=-400;fp_boosted=0;scheduled=0;
 for(unsigned d=0;d<3;d++) fp_epss[d]=0x1000*(d+1);
 drop_block=-1;drop_after=fault_write_only=0;
 maps=fail_map=log_count=0;fault_block=-1;fault_off=0;observed_tail_fault=1;
 for(unsigned d=0;d<3;d++) {
  regs[d][0]=3;regs[d][0xb0/4]=0x9b30;
  memcpy(&regs[d][0x100/4],observed_rows[d],sizeof(observed_rows[d]));
 }
 for(unsigned d=0;d<2;d++) {
  fp_tsens[d]=0x100000+0x4000*(d+3)+(fp_tsens_pa[d]&0x3fff);
  regs[d+3][0xe4/4]=8;
  for(unsigned n=1;n<=(d?3:15);n++)regs[d+3][(0xa0+4*n)/4]=(1u<<21)|450;
 }
}
static void reset_init(void) {
 reset();memset(fp_epss,0,sizeof(fp_epss));memset(fp_tsens,0,sizeof(fp_tsens));
}
static void all_temp(unsigned t) {
 for(unsigned d=0;d<2;d++)for(unsigned n=1;n<=(d?3:15);n++)regs[d+3][(0xa0+4*n)/4]=(1u<<21)|t;
}
'''
idle_globals = actual[actual.index('static unsigned fp_idle[3]'):actual.index('static unsigned fp_percore[3]')]
REGISTERS = REGISTERS.replace('IDLE_GLOBALS', idle_globals)
thermal_globals = actual[actual.index('static unsigned fp_hot ='):actual.index('static unsigned fp_enabled, fp_started')]
REGISTERS = REGISTERS.replace('THERMAL_GLOBALS', thermal_globals)
policy_caps = '\n'.join(re.findall(r'^static const unsigned fp_(?:cores|cool_cap|high_cap)\[3\].*$', actual, re.M))
assert len(policy_caps.splitlines()) == 3
REGISTERS = REGISTERS.replace('POLICY_CAPS', policy_caps)
TEST = TEST.replace('#define REGISTER_FUNCTIONS', REGISTERS + register_functions)
# Verify patch snapshots are exact and hooks are present in the final patch.
patch = (ROOT/'xnu/patches/0097-fuxi-touch-boost.patch').read_text()
for name in ('fuxi_perf.c','fuxi_perf_logic.h','fuxi_compile_abi.h'):
    block = re.search(r'^--- a/bsd/dev/'+name+r'\n.*?(?=^--- a/|\Z)',patch,re.M|re.S)[0]
    added = ''.join(l[1:] for l in block.splitlines(True) if l.startswith('+') and not l.startswith('+++'))
    assert added == (ROOT/'xnu/fuxi-perf'/name).read_text()
assert '#include "fuxi_compile_sched.h"' in patch
assert 'CTLFLAG_ANYBODY | CTLFLAG_LOCKED' in actual
assert 'CTLTYPE_INT | CTLFLAG_RD | CTLFLAG_LOCKED' in actual
assert patch.count('+\tfuxi_perf_input();') == 1 and '+\t\t\t\tfuxi_perf_input();' in patch
with tempfile.TemporaryDirectory() as d:
    source, exe = Path(d)/'perf.c', Path(d)/'perf'
    source.write_text(TEST)
    subprocess.run(['clang','-std=c11','-Wall','-Wextra','-Werror','-fsanitize=address,undefined',
                    '-I',str(ROOT/'xnu/fuxi-perf'),'-I',str(ROOT/'tools/fixtures'),str(source),'-o',str(exe)],check=True)
    subprocess.run([str(exe)],check=True)
subprocess.run(['python3',str(ROOT/'tools/test-fuxi-compile-sched.py')],check=True)

if args.xnu_tree:
    io_source = (args.xnu_tree/'osfmk/arm/io_map.c').read_text()
    # Keep the entire function including both early and VM-running branches.
    start = io_source.index('vm_offset_t\nio_map(')
    io_function = io_source[start:]
    assert io_function.rstrip().endswith('}'), 'io_map must be the final function'
    io_model = r'''
#include <stdint.h>
#include <assert.h>
#include <stdio.h>
typedef uintptr_t vm_offset_t, vm_map_offset_t, vm_size_t;
typedef int vm_prot_t, kma_flags_t, bool;
#define STARTUP_SUB_KMEM 1
#define VM_WIMG_WCOMB 2
#define VM_WIMG_IO 3
#define PMAP_MAP_BD_WCOMB 0
#define KMA_NOFAIL 1
#define KMA_PAGEABLE 2
#define KMA_DATA 4
#define KMA_PERMANENT 8
#define VM_KERN_MEMORY_IOKIT 1
#define round_page(x) (((x)+16383)&~(vm_offset_t)16383)
#define trunc_page(x) ((x)&~(vm_offset_t)16383)
static unsigned startup_phase, maps;
static vm_offset_t virtual_space_start, last_va, last_pa, last_end, allocation;
static int kernel_map=1,io_submap=2,kasan_notify_address;
static void pmap_map(intptr_t va,uintptr_t pa,uintptr_t end,int prot,unsigned flags) {
 assert(prot==3 && flags==VM_WIMG_IO);last_va=va;last_pa=pa;last_end=end;maps++;
}
static void pmap_map_bd(uintptr_t va,uintptr_t pa,uintptr_t end,int prot) {pmap_map(va,pa,end,prot,VM_WIMG_IO);}
static void pmap_map_bd_with_options(uintptr_t va,uintptr_t pa,uintptr_t end,int prot,int opts) {(void)opts;pmap_map_bd(va,pa,end,prot);}
static void kmem_alloc(int map,uintptr_t *va,uintptr_t size,int flags,int tag) {
 assert(map==io_submap && flags==(KMA_NOFAIL|KMA_PAGEABLE|KMA_PERMANENT) && tag==VM_KERN_MEMORY_IOKIT);
 allocation=size;*va=0x800000;
}
'''
    io_cases = r'''
int main(void) {
 uintptr_t addresses[]={0x17d91000,0x17d92000,0x17d93000,0xc271000,0xc272000,0xae36000};
 for(startup_phase=0;startup_phase<=1;startup_phase++) for(unsigned i=0;i<6;i++) {
  uintptr_t pa=addresses[i];virtual_space_start=0x800000;
  uintptr_t va=io_map(pa,0x1000,VM_WIMG_IO,3,0);
  assert(last_pa==(pa&~(uintptr_t)16383) && last_end-last_pa==16384);
  assert(va==last_va+(pa&16383));
  if(startup_phase)assert(allocation==16384);
  else assert(virtual_space_start==0x804000);
 }
 assert(maps==12);(void)kasan_notify_address;
 puts("PASS: actual XNU io_map, early/VM paths, all EPSS/TSENS/known display offsets on 16K pages");
}
'''
    with tempfile.TemporaryDirectory() as d:
        source, exe = Path(d)/'io.c', Path(d)/'io'
        source.write_text(io_model + io_function + io_cases)
        subprocess.run(['clang','-std=c11','-Wall','-Wextra','-Werror',
                        '-fsanitize=address,undefined',str(source),'-o',str(exe)],check=True)
        subprocess.run([str(exe)],check=True)
