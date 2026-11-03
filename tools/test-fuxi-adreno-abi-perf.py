#!/usr/bin/env python3
"""Per-call ABI timing aggregation, its periodic log line, and wait-slice sleeps.

Compiles the actual stat helpers from fuxi_abi_wire.h and FuxiAdrenoUserClient.cpp
with minimal kernel stubs. No hardware or Apple SDK; device timing is not observed.
"""
from pathlib import Path
import re
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
static void nanoseconds_to_absolutetime(uint64_t n,uint64_t *t) {*t=n/tickScale;}
static unsigned delays;static uint64_t delayUs;
static void delay(unsigned n) {delays++;delayUs+=n;now+=n*1000;}
static unsigned sleeps;
static void IOSleep(unsigned ms) {assert(ms==1);sleeps++;now+=1000000;}
static char logs[10][400];static unsigned logCount;
static void IOLog(const char *fmt,...) {va_list ap;va_start(ap,fmt);assert(logCount<10);vsnprintf(logs[logCount++],400,fmt,ap);va_end(ap);}
static uint32_t splitWhy[5]={1,2,3,4,5};
static int ccOn;static int fuxi_gpu_abi_cached_coherent(int probe) {assert(!probe);return ccOn;}
static IOLock *openLock;
IRQ_ENUM
static unsigned irqStatCalls;
static unsigned fuxi_gpu_irq_stats(uint64_t s[FG_IRQ_STATS],unsigned *why) {assert(!openLock->held);irqStatCalls++;for(unsigned i=0;i<FG_IRQ_STATS;i++)s[i]=i+1;
 s[FG_IRQ_LAT_N]=4;s[FG_IRQ_LAT_SUM]=4*25300;s[FG_IRQ_LAT_MAX]=91000;*why=4;return 3;}
static void fuxi_gpu_abi_submit_split(uint64_t ns[8],uint32_t why[5],uint32_t n[2],uint64_t *lm,uint64_t kib[2]) {for(int i=0;i<8;i++)ns[i]=(i+1)*600000;memcpy(why,splitWhy,sizeof(splitWhy));n[0]=900;n[1]=600;*lm=6400000;kib[0]=1200*600;kib[1]=0;}
static void fuxi_gpu_abi_clean_split(struct fuxi_gpu_clean_split *o) {memset(o,0,sizeof(*o));o->kib[0]=100*600;o->kib[1]=300*600;o->ns[0]=1000*600;o->ns[1]=2500*600;o->go_kib=50*600;o->imports=40;o->go_imports=9;o->mapped_kib=300<<10;
 o->bkt_kib[0]=10*600;o->bkt_ns[0]=100*600;o->bkt_kib[3]=390*600;o->bkt_ns[3]=3400*600;o->top_kib[0]=65536;o->top_perm[0]=3|16;o->top_kib[1]=16384;o->top_perm[1]=1;o->dty_state=2;o->dty_imports=7;o->submits=600;o->dty_checked=3300*600;o->dty_dirty=40*600;o->dty_kib=640*600;o->dty_full_kib=0;o->dty_check_ns=900000ull*600;o->dty_clean_ns=100000ull*600;o->ivac_ns=300000ull*600;}
/* The EPSS L3 page for abiL3Report: any write fails the test; one offset can abort. */
typedef uintptr_t vm_offset_t;typedef uintptr_t vm_size_t;
static uint32_t l3regs[0x400];static unsigned l3MapFail,l3FaultOff=~0u,l3Writes;static uint64_t l3MappedPa;
extern "C" vm_offset_t ml_io_map(vm_offset_t pa,vm_size_t n) {assert(n==0x1000);l3MappedPa=pa;return l3MapFail?0:(vm_offset_t)l3regs;}
extern "C" unsigned fuxi_gpu_access(uintptr_t va,uint64_t *v,unsigned wr,unsigned wide) {
 assert(!wide);if(wr){l3Writes++;return 1;}
 uintptr_t off=va-(uintptr_t)l3regs;assert(off<0x1000 && !(off&3));
 if(off==l3FaultOff)return 1;*v=l3regs[off/4];return 0;
}
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
 // Cached-coherent BOs (fuxigpuncimport bit 29): silent while off with no activity; once on, one line per window; window counts reset, live kept.
 cboStat(1,65536);cboStat(1,16384);cboStat(0,16384);cboStat(-1,0);ccOn=1;
 for(unsigned i=1;i<FG_SPLIT_WINDOW;i++)abiSplit(FG_SPLIT_DONE,0);
 assert(logCount==9 && !strcmp(logs[7],"FUXI GPU ABI: cached BOs per 600 submits enabled 1 allocs 2 frees 1 refused 1 live 1 live_kb 64\n") && strstr(logs[8],"lock_ max"));
 logCount=0;for(unsigned i=0;i<FG_SPLIT_WINDOW;i++)abiSplit(FG_SPLIT_DONE,0);
 assert(logCount==9 && !strcmp(logs[7],"FUXI GPU ABI: cached BOs per 600 submits enabled 1 allocs 0 frees 0 refused 0 live 1 live_kb 64\n"));
 ccOn=0;cboStat(0,65536);logCount=0;for(unsigned i=0;i<FG_SPLIT_WINDOW;i++)abiSplit(FG_SPLIT_DONE,0);assert(logCount==8); /* off, nothing live, no allocs: silent */
 // Fence pending histogram: submit-to-retire bins, untimed (no submit time), ready (retired at the first poll); IRQ counters read outside openLock.
 static IOLock pendLock;openLock=&pendLock;logCount=0;tickScale=1;
 static const uint64_t pendNs[6]={100000,300000,700000,3000000,40000000,0};
 for(unsigned i=0;i<600;i++){assert(!logCount);pendStat(pendNs[i%%6]?5:0,5+pendNs[i%%6],i%%6==2 && i<300);}
 assert(logCount==1 && irqStatCalls==1 && !pendLock.held);
 assert(!strcmp(logs[0],"FUXI ABI: fence pending per 600 waits us <250 100 <500 100 <1000 100 <2000 0 <4000 100 <8000 0 <16000 0 <32000 0 more 100 untimed 100 ready 50"
  " irq state 3 why 4 n 1 empty 2 foreign 3 wakes 4 timeouts 5 races 6 written 8 maskfix 7 away 9 wake n 4 avg_us 25.3 max_us 91\n"));
 pendStat(5,4,false);assert(logCount==1); // window restarted; a retire stamp before the submit stamp counts as untimed
 // Outside lock_ (BoAlloc pages before it, BoFree pages after it) and contiguous searches: silent with none, one line per window.
 logCount=0;tickScale=1;fuxi_gpu_abi_stats_log();assert(!logCount);
 abiOutside(0,1500);abiOutside(0,2500);abiOutside(1,700);abiOutside(2,1);
 now=10;assert(contigAsk(131072) && !contigAsk(147456));contigDone(131072,false);
 assert(!contigAsk(131072) && contigAsk(65536));contigDone(65536,true);
 now=10+5000000000ull;assert(contigAsk(131072)); // 5 s after the failure
 fuxi_gpu_abi_stats_log();
 assert(logCount==1 && !strcmp(logs[0],"FUXI GPU ABI: outside lock_ n/avg/max us alloc 2/2.0/2 free 1/0.7/0 contig tried 2 served 1 failed 1 backoff_skips 1\n"));
 fuxi_gpu_abi_stats_log();assert(logCount==1); // window restarted
 // CPU L3 report: reads only (no write ever), the LUT up to its repeated row, the vote's row, the raw state; an abort or a failed map
 // is reported instead.
 #define L3R(l) ((1u<<30)|(l))
 const uint32_t lut[]={0,L3R(20),L3R(32),L3R(52),L3R(84),L3R(84),L3R(99)};
 memcpy(&l3regs[0x100/4],lut,sizeof(lut));l3regs[0]=1;l3regs[0x20/4]=0x20;l3regs[0x90/4]=2;
 logCount=0;abiL3Report();
 assert(l3MappedPa==0x17d90000 && !l3Writes && logCount==1);
 assert(!strcmp(logs[0],"FUXI L3: read-only enable 1 vote 2 (614 MHz) state 0x20 (L 32 = 614 MHz?) top 1612 MHz, 5 LUT rows MHz: 300 384 614 998 1612\n"));
 l3regs[0x90/4]=9;logCount=0;abiL3Report();assert(logCount==1 && strstr(logs[0]," vote 9 (no such row) "));
 l3FaultOff=0x90;logCount=0;abiL3Report();
 assert(logCount==1 && !strcmp(logs[0],"FUXI L3: read-only: unreadable at +0x90 (enable 0x1 state 0x20 vote 0, 0 rows read)\n"));
 l3FaultOff=0x108;logCount=0;abiL3Report();assert(logCount==1 && strstr(logs[0],"unreadable at +0x108") && strstr(logs[0],"vote 9, 2 rows read"));
 l3FaultOff=~0u;l3MapFail=1;logCount=0;abiL3Report();assert(logCount==1 && !strcmp(logs[0],"FUXI L3: read-only: map 0x17d90000 failed\n"));
 assert(!l3Writes);
 puts("PASS: ABI stat aggregation/window reset/log format, sleep slices and sleep accounting, submit_split report (cached-BO line), fence pending histogram, outside-lock_/contiguous line, read-only CPU L3 report");
}
'''


def main():
    src = (ROOT/'xnu/fuxi-adreno/FuxiAdrenoUserClient.cpp').read_text()
    stats = src[src.index('static IOLock *statLock;'):src.index('static bool connected;')]
    step = src[src.index('static const char *abiStep('):src.index('class FuxiAdrenoUserClient :')]
    log = src[src.index('extern "C" void fuxi_gpu_abi_stats_log(void)'):src.index('extern "C" void fuxi_gpu_abi_publish(void)')]
    # The interrupt counter slots, exactly as the kernel declares them.
    header = (ROOT/'xnu/fuxi-adreno/fuxi_gpu_abi.h').read_text()
    irq_enum = re.search(r'^enum \{ FG_IRQ_N,.*?\};\n', header, re.M | re.S)[0]
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)/'perf.cpp'
        path.write_text(MODEL.replace('IRQ_ENUM', irq_enum) % (stats, step, log))
        exe = path.with_suffix('')
        subprocess.run(['clang++', '-std=c++17', '-O1', '-g', '-Wall', '-Wextra', '-Werror', '-Wno-format',
                        '-fsanitize=address,undefined', '-I'+str(ROOT/'xnu/fuxi-adreno'), str(path), '-o', str(exe)], check=True)
        subprocess.run([str(exe)], check=True)


if __name__ == '__main__':
    main()
