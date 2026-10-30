#!/usr/bin/env python3
"""Per-call ABI timing aggregation, its periodic log line, and wait-slice sleeps.

Compiles the actual stat helpers from fuxi_abi_wire.h and FuxiAdrenoUserClient.cpp
with minimal kernel stubs. No hardware or Apple SDK; device timing is not observed.
"""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]

MODEL = r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <stdarg.h>
#include <string.h>
#include <stdlib.h>
#include "fuxi_abi_wire.h"
struct IOLock {int held=0;};
static unsigned locks;
static void IOLockLock(IOLock *l) {assert(!l->held);l->held=1;locks++;}
static void IOLockUnlock(IOLock *l) {assert(l->held);l->held=0;}
static uint64_t now,tickScale=1;
static uint64_t mach_absolute_time() {return now;}
static void absolutetime_to_nanoseconds(uint64_t t,uint64_t *n) {*n=t*tickScale;}
static unsigned delays;static uint64_t delayUs;
static void delay(unsigned n) {delays++;delayUs+=n;now+=n*1000;}
static unsigned sleeps;
static void IOSleep(unsigned ms) {assert(ms==1);sleeps++;now+=1000000;}
static char logs[8][400];static unsigned logCount;
static void IOLog(const char *fmt,...) {va_list ap;va_start(ap,fmt);assert(logCount<8);vsnprintf(logs[logCount++],400,fmt,ap);va_end(ap);}
static uint32_t splitWhy[5]={1,2,3,4,5};
static IOLock *openLock;
enum { FG_IRQ_N, FG_IRQ_EMPTY, FG_IRQ_FOREIGN, FG_IRQ_WAKES, FG_IRQ_TIMEOUTS, FG_IRQ_RACES, FG_IRQ_MASKFIX, FG_IRQ_WRITTEN, FG_IRQ_STATS };
static unsigned irqStatCalls;
static unsigned fuxi_gpu_irq_stats(uint64_t s[FG_IRQ_STATS],unsigned *why) {assert(!openLock->held);irqStatCalls++;for(unsigned i=0;i<FG_IRQ_STATS;i++)s[i]=i+1;*why=4;return 3;}
static void fuxi_gpu_abi_submit_split(uint64_t ns[8],uint32_t why[5],uint32_t n[2],uint64_t *lm,uint64_t kib[2]) {for(int i=0;i<8;i++)ns[i]=(i+1)*600000;memcpy(why,splitWhy,sizeof(splitWhy));n[0]=900;n[1]=600;*lm=6400000;kib[0]=1200*600;kib[1]=0;}
static void fuxi_gpu_abi_clean_split(struct fuxi_gpu_clean_split *o) {memset(o,0,sizeof(*o));o->kib[0]=100*600;o->kib[1]=300*600;o->ns[0]=1000*600;o->ns[1]=2500*600;o->go_kib=50*600;o->imports=40;o->go_imports=9;o->mapped_kib=300<<10;
 o->bkt_kib[0]=10*600;o->bkt_ns[0]=100*600;o->bkt_kib[3]=390*600;o->bkt_ns[3]=3400*600;o->top_kib[0]=65536;o->top_perm[0]=3|16;o->top_kib[1]=16384;o->top_perm[1]=1;o->dty_state=2;o->dty_imports=7;o->submits=600;o->dty_checked=3300*600;o->dty_dirty=40*600;o->dty_kib=640*600;o->dty_full_kib=0;o->dty_check_ns=900000ull*600;o->dty_clean_ns=100000ull*600;o->ivac_ns=300000ull*600;}
%s
%s
%s
int main() {
 // Aggregator: counts, totals, running max, and out-of-range slots ignored.
 fg_abi_stat raw[FG_ABI_STATS]={};
 fg_abi_stat_add(raw,5,10);fg_abi_stat_add(raw,5,30);fg_abi_stat_add(raw,5,20);
 assert(raw[5].count==3 && raw[5].total==60 && raw[5].max==30);
 fg_abi_stat_add(raw,FG_ABI_STATS,1);fg_abi_stat_add(raw,~0u,1);
 for(unsigned i=0;i<FG_ABI_STATS;i++)assert(i==5 || !raw[i].count);
 static_assert(FG_ABI_STAT_LOCK==0 && FG_ABI_STAT_WALK==17 && FG_ABI_STAT_SLEEP==19 && FG_ABI_STAT_MAPWAIT==20 && FG_ABI_STAT_BIND==24 && FG_ABI_STATS==25,"slot layout");
 // No lock yet (allocation failure) or no calls: silent, no crash.
 abiStat(5,10);fuxi_gpu_abi_stats_log();assert(!logCount && !locks);
 static IOLock storage;statLock=&storage;
 fuxi_gpu_abi_stats_log();assert(!logCount);
 // One window: selector 5 (submit) 2 calls, avg 501.0 us, max 1000 us; lock wait 1 call 0.4 us.
 tickScale=1;abiStat(5,1500);abiStat(5,1000500);abiStat(0,400);
 fuxi_gpu_abi_stats_log();
 assert(logCount==1);
 assert(!strcmp(logs[0],"FUXI GPU ABI: calls n/avg/max us lock 1/0.4/0 submit 2/501.0/1000\n"));
 fuxi_gpu_abi_stats_log();assert(logCount==1); // window restarted
 // Tick conversion applies, and every slot fits in bounded lines naming each exactly once.
 tickScale=42;logCount=0;
 for(unsigned i=0;i<FG_ABI_STATS;i++)abiStat(i,1000000+i);
 fuxi_gpu_abi_stats_log();
 assert(logCount>=3 && logCount<=4);
 // Selectors and phases never share a line: "calls" lines hold slots 0..16, "stages" lines the phases.
 for(unsigned l=0;l<logCount;l++){bool st=strstr(logs[l],"ABI: stages ")!=nullptr;assert(st!=(strstr(logs[l],"ABI: calls ")!=nullptr));
  assert(st==(!!strstr(logs[l]," walk ") || !!strstr(logs[l]," bind ") || !!strstr(logs[l]," mapwork ") || !!strstr(logs[l]," sleep ")));assert(!st || (!strstr(logs[l]," alloc ") && !strstr(logs[l]," map ")));}
 static const char *names[]={"lock","query","alloc","map","free","submit","wait","timestamp","status",
  "set-metadata","get-metadata","create-queue","close-queue","cpu-token","cpu-unmap","device-info","import",
  "walk","zero","sleep","mapwait","maplock","mapwork","allocmem","bind"};
 for(unsigned i=0;i<FG_ABI_STATS;i++) {
  char key[64];snprintf(key,sizeof(key)," %%s 1/",names[i]);unsigned seen=0;
  for(unsigned l=0;l<logCount;l++){assert(strlen(logs[l])<300);for(const char *p=logs[l];(p=strstr(p,key));p++)seen++;}
  assert(seen==1);
 }
 assert(strstr(logs[0],"lock 1/42000.0/42000"));
 // Sleeps: fuxigpuwaitus slice (delay) or the 1 ms IOSleep when 0, each recorded as a sleep.
 tickScale=1;now=1000;logCount=0;delays=0;delayUs=0;
 abiPause();abiPause();assert(delays==2 && delayUs==200 && sleeps==0);
 waitPollUs=0;abiPause();assert(sleeps==1 && delays==2);waitPollUs=100;
 tickScale=1;logCount=0;fuxi_gpu_abi_stats_log();
 assert(logCount==1 && strstr(logs[0]," sleep 3/"));
 // submit_split: a 600-submit window is one report; a lock_ wait over 500 us is blamed on the selector that released lock_ last.
 logCount=0;tickScale=1;
 abiSplit(FG_SPLIT_RELEASE,5);abiSplit(FG_SPLIT_LOCKED,6400000);abiSplit(FG_SPLIT_RELEASE,6);abiSplit(FG_SPLIT_LOCKED,100000);
 for(unsigned i=0;i<FG_SPLIT_WINDOW;i++){
  assert(!logCount);
  abiSplit(FG_SPLIT_ARGS,1000);abiSplit(FG_SPLIT_FENCE,0);abiSplit(FG_SPLIT_LOOKUP,2000);abiSplit(FG_SPLIT_BACKEND,3000000);abiSplit(FG_SPLIT_SLEEP,500000);
  abiSplit(FG_SPLIT_TOTAL,3600000);abiSplit(FG_SPLIT_BOS,2);abiSplit(FG_SPLIT_LIVE,406);abiSplit(FG_SPLIT_ATT,3);abiSplit(FG_SPLIT_WAITS,2);abiSplit(FG_SPLIT_EVENTS,1);abiSplit(FG_SPLIT_DONE,0);
 }
 assert(logCount==8);
 assert(strstr(logs[0],"submit_split n 600 us/submit total 3600.0 args 1.0 fence_wait 0.0 lookup 2.0 backend 3000.0 retry_sleep 500.0 rest 97.0 attempts 3.0 waits 2.0 events 1.0 ibs 2.0 live_bos 406 lookup_ns_per_ib 1000"));
 assert(strstr(logs[1],"att 900 ok 600 ref 300 (busy/hold/noq/pw/full 1/2/3/4/5)") && strstr(logs[1],"lockwait 4.6 max 6.4") && strstr(logs[1],"refused_us 16.0"));
 assert(strstr(logs[2],"waits/submit by kind: hold 0.0 (0 us each) copy 0.0 (0 us each) other 0.0 (0 us each); event wakes 600 of 1200"));
 assert(strstr(logs[3],"dirty state 2 (2 active 3 refused) tracked_imports 7 submits 600 per submit: pages_checked 3300 dirty 40 kb_cleaned 640 full_kb 0 check_us 900.0 clean_us 100.0 per_page_ns 272 ivac_us 300.0"));
 assert(strstr(logs[4],"clean/ok kb rw 100 ro 300 go_skip 50 us rw 1.0 ro 2.5 imports 40 gpu_only 9 mapped_mb 300"));
 assert(strstr(logs[5],"buckets kb/us <64K 10/0.1 <1M 0/0.0 <16M 0/0.0 big 390/3.4") && strstr(logs[6],"top3 kb/perm 65536/19 16384/1 0/0"));
 for(unsigned l=0;l<8;l++)assert(strlen(logs[l])<300);
 assert(strstr(logs[7],"lock_ max 6.4 ms") && strstr(logs[7]," submit 1/6.4") && !strstr(logs[7]," wait "));
 logCount=0;abiSplit(FG_SPLIT_DONE,0);assert(!logCount); // restarted
 // Fence pending histogram: submit-to-retire bins, untimed (no submit time), ready (retired at the first poll); IRQ counters read outside openLock.
 static IOLock pendLock;openLock=&pendLock;logCount=0;tickScale=1;
 static const uint64_t pendNs[6]={100000,300000,700000,3000000,40000000,0};
 for(unsigned i=0;i<600;i++){assert(!logCount);pendStat(pendNs[i%%6]?5:0,5+pendNs[i%%6],i%%6==2 && i<300);}
 assert(logCount==1 && irqStatCalls==1 && !pendLock.held);
 assert(!strcmp(logs[0],"FUXI ABI: fence pending per 600 waits us <250 100 <500 100 <1000 100 <2000 0 <4000 100 <8000 0 <16000 0 <32000 0 more 100 untimed 100 ready 50"
  " irq state 3 why 4 n 1 empty 2 foreign 3 wakes 4 timeouts 5 races 6 written 8 maskfix 7\n"));
 pendStat(5,4,false);assert(logCount==1); // window restarted; a retire stamp before the submit stamp counts as untimed
 puts("PASS: ABI stat aggregation/window reset/log format, sleep slices and sleep accounting, submit_split report, fence pending histogram");
}
'''


def main():
    src = (ROOT/'xnu/fuxi-adreno/FuxiAdrenoUserClient.cpp').read_text()
    stats = src[src.index('static IOLock *statLock;'):src.index('static bool connected;')]
    step = src[src.index('static const char *abiStep('):src.index('class FuxiAdrenoUserClient :')]
    log = src[src.index('extern "C" void fuxi_gpu_abi_stats_log(void)'):src.index('extern "C" void fuxi_gpu_abi_publish(void)')]
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)/'perf.cpp'
        path.write_text(MODEL % (stats, step, log))
        exe = path.with_suffix('')
        subprocess.run(['clang++', '-std=c++17', '-O1', '-g', '-Wall', '-Wextra', '-Werror', '-Wno-format',
                        '-fsanitize=address,undefined', '-I'+str(ROOT/'xnu/fuxi-adreno'), str(path), '-o', str(exe)], check=True)
        subprocess.run([str(exe)], check=True)


if __name__ == '__main__':
    main()
