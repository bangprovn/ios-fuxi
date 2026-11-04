#!/usr/bin/env python3
"""fuxipaneldcs host test: the production fuxi_gpu_scanout.inc under a DSI/MDP/TE model, no device.

Checks: off by default and exact boot-arg values; refusal without scanout ownership, with autorefresh on or an unexpected DSI
state (nothing written); the one clamp and the fuxipanelmaxdbv hard cap; the 39 51 MM LL format; one 0x51 per 16 ms coalesced to
the newest value; sends only between transfers and never during a START; the E1/E2 scripts; the sleep/wake state machine with
the 120 ms rule under repeated power presses, no START while a sequence runs or the panel sleeps, the restore after one frame;
panelwake=2 refused; DMA error and timeout stop scanout with no retry. Python then decodes every FIFO word the model saw and
compares each packet byte for byte with the panel's DTB command blocks (tools/fixtures/fuxi-panel-m3-dcs.json, and with
--dtb the phone's live.dtb itself), checks the DT waits and LP states, and that every F0 5A 5A is closed by F0 A5 A5.
"""
import argparse
import hashlib
import importlib.util
import json
import re
import struct
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'tools/fixtures/fuxi-panel-m3-dcs.json'
SEQ_KEYS = ('qcom,mdss-dsi-on-command', 'qcom,mdss-dsi-off-command')


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def dtb_nodes(blob):
    magic, _, off_struct, off_str = struct.unpack('>4I', blob[:16])
    assert magic == 0xd00dfeed
    p, path, nodes = off_struct, [], {}
    while True:
        t = struct.unpack('>I', blob[p:p + 4])[0]; p += 4
        if t == 1:
            e = blob.index(b'\0', p); path.append(blob[p:e].decode('latin1')); p = (e + 4) & ~3
            nodes['/'.join(path)] = {}
        elif t == 2:
            path.pop()
        elif t == 3:
            ln, no = struct.unpack('>II', blob[p:p + 8]); p += 8
            e = blob.index(b'\0', off_str + no)
            nodes['/'.join(path)][blob[off_str + no:e].decode('latin1')] = blob[p:p + ln]; p = (p + ln + 3) & ~3
        elif t == 9:
            return nodes


def check_fixture_against_dtb(fix, path):
    blob = Path(path).read_bytes()
    assert hashlib.sha256(blob).hexdigest() == fix['sha256'], 'live.dtb differs from the fixture source'
    nodes = dtb_nodes(blob)
    P = nodes[fix['node']]
    for k, v in fix['panel'].items():
        assert P[k].hex() == v, k
    for k in fix['absent']:
        assert k not in P, k
    for t in ('timing@0', 'timing@1'):
        T = nodes[fix['node'] + '/qcom,mdss-dsi-display-timings/' + t]
        for k, v in fix[t].items():
            assert (T[k].rstrip(b'\0').decode() if k.endswith('-state') else T[k].hex()) == v, (t, k)
    print('PASS: fixture matches live.dtb byte for byte (sha256 %s)' % fix['sha256'][:16])


def dt_packets(hexstr):
    """DT descriptors: type, last, vc, flags (40 = batch), wait ms, len (2 bytes), payload."""
    b = bytes.fromhex(hexstr)
    out, i = [], 0
    while i < len(b):
        n = (b[i + 5] << 8) | b[i + 6]
        out.append({'type': b[i], 'flags': b[i + 3], 'wait': b[i + 4], 'data': b[i + 7:i + 7 + n]})
        i += 7 + n
    assert i == len(b)
    return out


def fifo_packets(words):
    """Inverse of the FIFO layout: long (flag 40) header len | type << 16, payload LSB first; short d0 | d1 << 8 | type << 16."""
    out, i = [], 0
    while i < len(words):
        w = words[i]; i += 1
        flags, typ = w >> 24, (w >> 16) & 0xff
        if flags & 0x40:
            n = w & 0xffff
            raw = b''.join(struct.pack('<I', x) for x in words[i:i + (n + 3) // 4])
            assert all(x == 0xff for x in raw[n:]), 'long payload pad is ff'
            out.append({'type': typ, 'data': raw[:n], 'last': bool(flags & 0x80)}); i += (n + 3) // 4
        else:
            assert flags in (0, 0x80)
            out.append({'type': typ, 'data': bytes([w & 0xff, (w >> 8) & 0xff]), 'last': bool(flags & 0x80)})
    assert [p['last'] for p in out] == [False] * (len(out) - 1) + [True], 'LAST on the final packet only'
    return out


PREFIX_EXTRA = r'''
static int extra_bootarg(const char *name,unsigned *v);
'''

TEST = r'''
static unsigned pd_state_for_model(void) {return fg_pd_state;}
static unsigned fg_scanout_owned_for_model(void) {return fg_scanout_owned;}
static uint8_t panel[64];
static unsigned bootDcs,bootMax,bootWake,bootBl,hasDcs,hasMax,hasWake,hasBl;
static int extra_bootarg(const char *name,unsigned *v) {
 if(!strcmp(name,"fuxipaneldcs")){*v=bootDcs;return hasDcs;}
 if(!strcmp(name,"fuxipanelmaxdbv")){*v=bootMax;return hasMax;}
 if(!strcmp(name,"fuxipanelwake")){*v=bootWake;return hasWake;}
 if(!strcmp(name,"fuxibacklight")){*v=bootBl;return hasBl;}
 return 0;
}
static const char *scenario="none";
static void mark(const char *what) {fprintf(stdout,"MARK %s %s t=%llu\n",scenario,what,(unsigned long long)ticks);}
static void reset(const char *name) {
 scenario=name;fprintf(stdout,"SCENARIO %s\n",name);
 memset(ctl,0,sizeof(ctl));memset(intf,0,sizeof(intf));memset(dsi,0,sizeof(dsi));memset(sspp,0,sizeof(sspp));sspp[0x14/4]=0xb8000000u;
 intf[0x2b4/4]=0x80000001u;intf[0x294/4]=2400;intf[0x2b0/4]=2400;
 dsi[0x004/4]=0x1f5;dsi[0x03c/4]=0x14000000u;dsi[0x084/4]=0x80000004u;dsi[0x110/4]=0x500;dsi[0x04c/4]=0x54;
 fg_scanout_requested=fg_scanout_owned=fg_scanout_failed=fg_scanout_writable=fg_scanout_paneldcs_requested=0;
 fg_scanout_double_requested=fg_scanout_triple_requested=fg_scanout_double_active=fg_scanout_double_initialized=0;
 fg_scanout_panel120_requested=fg_scanout_panel120_attempted=fg_scanout_panel120_active=0;
 fg_scanout_te_mode=0;fg_scanout_drain_us=FG_SCAN_DRAIN_US;fg_scanout_explicit_drain=0;fg_scanout_period_us=16667;
 fg_scanout_starts=0;fg_scanout_last_start=0;fg_scanout_last_count=0;fg_scanout_back_ready=0;fg_busy=0;
 fg_pd_mode=0;fg_pd_max=FG_PD_DBV_DEFAULT_MAX;fg_pd_wake_mode=1;fg_pd_dead=0;fg_pd_state=FG_PD_AWAKE;fg_pd_step=0;
 fg_pd_want=~0u;fg_pd_want_zero=0;fg_pd_sent=~0u;fg_pd_req=0;fg_pd_dirty=0;fg_pd_n51=0;fg_pd_wanted=0;fg_pd_refusals=0;
 fg_pd_want_off=fg_pd_black=fg_pd_off_refused=fg_pd_wake_pending=fg_pd_wake_starts=fg_pd_script_i=fg_pd_holds=0;
 fg_pd_te_pending=fg_pd_owner_logged=fg_pd_always=0;fg_pd_last51=fg_pd_t_step=fg_pd_t_seq=fg_pd_t_off=fg_pd_t_idle=0;
 fg_pd_t_sleepin=fg_pd_owned_t=fg_pd_te_t0=fg_pd_sleep_t=0;
 bootDcs=bootMax=bootWake=bootBl=hasDcs=hasMax=hasWake=hasBl=0;
 ticks=1000000;machScale=1;ready=1;interruptsOn=1;noDone=0;pendingDma=0;dsiReads=0;dmaStarts=0;starts=0;writes=0;reads=0;
 dsiWrites=0;dmaErrorAt=~0u;dmaHangAt=~0u;teStopped=0;teOrigin=0;startT=0;xferStart=xferEnd=~0ull;nsends=0;fifoN=0;
 probeScript=model_sync;model_sync();
}
// One frame through the single-buffer path: acquire until writable (time and TE run meanwhile), then START. 1 started, 2 publish
// deferred, 0 no grant within 300 ms, -1 held.
static int frame(void) {
 for(unsigned i=0;i<600;i++){
  int w=fuxi_gpu_scanout_acquire();
  if(w<0)return -1;
  if(w==1){int p=fuxi_gpu_scanout_publish();return p==1?1:p==2?2:-1;}
  advance(500);
 }
 return 0;
}
static int cpu_frame(void) {
 for(unsigned i=0;i<600;i++){
  int w=fuxi_gpu_scanout_cpu_acquire();
  if(w<0)return -1;
  if(w==1){int p=fuxi_gpu_scanout_publish();return p==1?1:p==2?2:-1;}
  advance(500);
 }
 return 0;
}
static uint64_t run_worker(void) {
 unsigned k=spinKicks;fg_pd_run(0,0);
 return spinKicks==k?0:(spinDeadline-mach_absolute_time())/machScale;
}
// Run the thread call as XNU would: at each scheduled deadline, until idle or until.
static void drive(uint64_t until) {
 while(ticks<until){uint64_t d=run_worker();if(!d)break;advance(d<until-ticks?d:until-ticks);}
}
static void own(void) {
 fuxi_gpu_scanout_configure((uintptr_t)panel,1,0);
 model_sync();
 assert(!fuxi_gpu_scanout_acquire() && fuxi_gpu_scanout_owned());
 assert(frame()==1);
}
static void dcs_boot(unsigned mode) {hasDcs=1;bootDcs=mode;}
static void quiet(uint64_t us) {advance(us);}
static unsigned sent_value(unsigned i) {assert(i<nsends && sends[i].n==2 && sends[i].w[0]==0xc0390003u && (sends[i].w[1]&0xff0000ffu)==0xff000051u);
 return ((sends[i].w[1]>>8)&0xff)<<8|((sends[i].w[1]>>16)&0xff);}
int main(void) {
 // --- Off by default: no mode, no DSI mapping, no access, the public calls do nothing.
 reset("default");own();
 assert(!fg_pd_mode && !fg_scanout_paneldcs_requested && !fuxi_gpu_panel_dcs_max());
 {unsigned k=probeKicks;fuxi_gpu_panel_dbv(0x200);fuxi_gpu_panel_power(0);fuxi_gpu_panel_power(1);assert(probeKicks==k && !fg_pd_want_off);}
 for(unsigned i=0;i<5;i++)assert(frame()==1);
 fg_pd_run(0,0);
 assert(!nsends && !dsiWrites && !dsiReads && !fg_pd_holds);
 // --- Exact values only; scanout must be requested.
 static const unsigned bad[]={0,5,7,16,0xffffffffu};
 for(unsigned i=0;i<5;i++){reset("badvalue");dcs_boot(bad[i]);fuxi_gpu_scanout_configure((uintptr_t)panel,1,0);assert(!fg_pd_mode && !fg_scanout_paneldcs_requested);}
 for(unsigned m=1;m<=4;m++){reset("goodvalue");dcs_boot(m);fuxi_gpu_scanout_configure((uintptr_t)panel,1,0);
  assert(fg_pd_mode==m && fg_scanout_paneldcs_requested && fuxi_gpu_panel_dcs_max()==(m>=3?0x400u:0u));}
 reset("noscanout");dcs_boot(3);fuxi_gpu_scanout_configure((uintptr_t)panel,0,0);
 assert(!fg_pd_mode && !fg_scanout_paneldcs_requested && !fuxi_gpu_panel_dcs_max());
 // --- The cap: fuxipanelmaxdbv is clamped to [4, 0x7ff]; every DBV to [4, cap], 0 only when allowed.
 static const unsigned maxIn[]={0,2,4,0xa0,0x400,0x7ff,0x800,0xfff,0xffffffffu},maxOut[]={4,4,4,0xa0,0x400,0x7ff,0x7ff,0x7ff,0x7ff};
 for(unsigned i=0;i<9;i++){
  reset("cap");dcs_boot(3);hasMax=1;bootMax=maxIn[i];fuxi_gpu_scanout_configure((uintptr_t)panel,1,0);
  assert(fuxi_gpu_panel_dcs_max()==maxOut[i]);
  for(unsigned d=0;d<0x1100;d++){
   unsigned c=fg_pd_clamp(d,0),z=fg_pd_clamp(d,1);
   assert(c>=4 && c<=maxOut[i] && c<=0x7ff && (d<4?c==4:d>maxOut[i]?c==maxOut[i]:c==d));
   assert(d?z==c:z==0);
  }
 }
 fg_pd_max=0xffffffffu;assert(fg_pd_clamp(0xffff,0)==0x7ff); // even a corrupt cap cannot pass 0x7ff
 // --- Byte format: 39 51 MM LL, MSB first, one long packet, LAST set, ff pad.
 {uint32_t w[16];const struct fg_pd_pkt a={0x39,3,{0x51,0x01,0x33}},b={0x39,3,{0x51,0x07,0xff}};
  assert(fg_pd_words(&a,1,w)==2 && w[0]==0xc0390003u && w[1]==0xff330151u);
  assert(fg_pd_words(&b,1,w)==2 && w[1]==0xffff0751u);
  const struct fg_pd_pkt bad1={0x29,2,{0,0}},bad2={0x39,5,{1,2,3,4}},bad3={0x15,3,{1,2,3}};
  assert(!fg_pd_words(&bad1,1,w) && !fg_pd_words(&bad2,1,w) && !fg_pd_words(&bad3,1,w));
  for(unsigned i=0;i<3;i++)assert(fg_pd_words(fg_pd_off[i].p,fg_pd_off[i].n,w));
  for(unsigned i=0;i<4;i++)assert(fg_pd_words(fg_pd_wake[i].p,fg_pd_wake[i].n,w));}
 // --- Refusals: not owned; autorefresh on; DSI engine not as ABL left it. Nothing is written to the DSI.
 reset("unowned");dcs_boot(3);fuxi_gpu_scanout_configure((uintptr_t)panel,1,0);
 fuxi_gpu_panel_dbv(0x100);fg_pd_run(0,0);fg_pd_run(0,0);
 assert(!nsends && !dsiReads && !dsiWrites && fg_pd_want==~0u && fg_pd_req);
 reset("autorefresh");dcs_boot(3);own();quiet(40000);intf[0x2b4/4]|=0x80000000u;
 fuxi_gpu_panel_dbv(0x100);drive(ticks+100000);
 assert(!nsends && !dsiWrites && !dsiReads && fg_pd_dead && !fuxi_gpu_panel_dcs_max());
 reset("tpg");dcs_boot(3);own();quiet(40000);dsi[0x15c/4]=1;
 fuxi_gpu_panel_dbv(0x100);drive(ticks+100000);
 assert(!nsends && !dsiWrites && fg_pd_dead && !fg_scanout_failed);
 // --- Brightness from the thread call (static screen): HS, the clamp, 16 ms, coalescing to the newest value.
 reset("rate");dcs_boot(3);own();quiet(40000);mark("begin");
 fuxi_gpu_panel_dbv(0x133);drive(ticks+5000);
 assert(nsends==1 && !sends[0].lp && sent_value(0)==0x133);
 fuxi_gpu_panel_dbv(0x200);fuxi_gpu_panel_dbv(0x300);fuxi_gpu_panel_dbv(0x250);drive(ticks+60000);
 assert(nsends==2 && sent_value(1)==0x250 && sends[1].t-sends[0].t>=16000);
 fuxi_gpu_panel_dbv(0x7ff);drive(ticks+60000);fuxi_gpu_panel_dbv(1);drive(ticks+60000);
 assert(nsends==4 && sent_value(2)==0x400 && sent_value(3)==4);
 for(unsigned i=1;i<nsends;i++)assert(sends[i].t-sends[i-1].t>=16000);
 assert(dsi[0x03c/4]==0x14000000u && dsi[0x04c/4]==0x54 && dsi[0x084/4]==0x80000004u && dsi[0x110/4]==0x500 && !dsi[0x15c/4]);
 mark("end");
 // --- Brightness while frames stream: sent from acquire between transfers (the model asserts START clear and the line idle at
 // every DSI write, and no START during a send), newest value wins, frames keep coming.
 reset("stream");dcs_boot(3);own();mark("begin");
 {unsigned startsBefore=starts;
  for(unsigned f=0;f<120;f++){fuxi_gpu_panel_dbv(0x100+f);int r=frame();assert(r==1);}
  assert(starts-startsBefore==120 && nsends>=10 && !fg_scanout_failed);
  for(unsigned i=1;i<nsends;i++)assert(sends[i].t-sends[i-1].t>=16000);
  fuxi_gpu_panel_dbv(0x3ff);for(unsigned f=0;f<4;f++)assert(frame()==1);
  assert(sent_value(nsends-1)==0x3ff);}
 mark("end");
 // --- E1: 60 s after ownership, 0x133, 0x400, 0x0a0, 0x133, ten seconds apart; E2 then 4, 0, 0x133; a lower cap applies.
 for(unsigned m=1;m<=2;m++){
  reset(m==1?"e1":"e2");dcs_boot(m);own();uint64_t t0=fg_pd_owned_t;mark("begin");
  drive(t0+120000000ull);
  static const unsigned v1[]={0x133,0x400,0x0a0,0x133},t1[]={60,70,80,90},v2[]={0x133,0x400,0x0a0,4,0,0x133},t2[]={60,70,80,90,95,100};
  const unsigned *v=m==1?v1:v2,*t=m==1?t1:t2,n=m==1?4:6;
  assert(nsends==n);
  for(unsigned i=0;i<n;i++){assert(sent_value(i)==v[i] && !sends[i].lp);
   assert(sends[i].t>=t0+t[i]*1000000ull && sends[i].t<t0+t[i]*1000000ull+100000);}
  mark("end");
 }
 reset("e1cap");dcs_boot(1);hasMax=1;bootMax=0x200;own();drive(fg_pd_owned_t+95000000ull);
 assert(nsends==4 && sent_value(1)==0x200 && sent_value(0)==0x133);
 // --- Sleep and wake (fuxipaneldcs=4, soft wake).
 reset("sleep");dcs_boot(4);hasBl=1;bootBl=1;own();quiet(40000);
 fuxi_gpu_panel_dbv(0x180);drive(ticks+5000);assert(nsends==1 && sent_value(0)==0x180);
 assert(frame()==1);quiet(5000);
 mark("power-off");
 fuxi_gpu_panel_power(0);                                   /* SweetFramebuffer: before fPowerOff and the black frame */
 assert(run_worker()>0 && fg_pd_state==FG_PD_AWAKE && nsends==1); /* waits for the black frame */
 assert(cpu_frame()==1 && fg_pd_black==2);
 {unsigned startsBlack=starts,pressed=0;
  // Repeated presses while the sequence runs: on, off, on right after 10; 11 must still come >= 120 ms after 10.
  for(unsigned i=0;i<400 && fg_pd_state!=FG_PD_WAKE_SEND;i++){
   uint64_t d=run_worker();
   if(fg_pd_state==FG_PD_OFF_SEND && fg_pd_step==3 && !pressed){
    pressed=1;mark("presses");fuxi_gpu_panel_power(1);fuxi_gpu_panel_power(0);fuxi_gpu_panel_power(1);
    advance(1000);(void)run_worker();advance(1000);d=run_worker();
    assert(fg_pd_state==FG_PD_OFF_SEND && !fg_pd_want_off);
   }
   if(fg_pd_state!=FG_PD_AWAKE)assert(fuxi_gpu_scanout_acquire()==0 && fuxi_gpu_scanout_cpu_acquire()==0); /* no grant during a sequence */
   advance(d?d:1000);
  }
  assert(pressed && fg_pd_state==FG_PD_WAKE_SEND && starts==startsBlack);
  // The wake was already wanted: it began as soon as 120 ms after 10 passed; then one frame, then the restore.
  drive(ticks+400000);
  assert(fg_pd_state==FG_PD_AWAKE && fg_pd_wake_pending && starts==startsBlack);
  mark("wake-commands-done");
  unsigned before=nsends;
  assert(frame()==1 && starts==startsBlack+1);           /* the redraw */
  for(unsigned i=0;i<10 && nsends==before;i++){assert(frame()==1);}
  assert(nsends==before+1 && sent_value(before)==0x180 && !sends[before].lp && !fg_pd_wake_pending);
  mark("restored");
  drive(ticks+1500000);                                  /* the TE rate one second after the wake */
  assert(!fg_pd_te_pending);}
 // Sleep with TE stopped while asleep: no TE stall fallback, no grant, no START; wake still works.
 fuxi_gpu_panel_power(0);assert(cpu_frame()==1);mark("sleep2");drive(ticks+600000);assert(fg_pd_state==FG_PD_ASLEEP);
 teFrozen=te_count();teStopped=1;
 {unsigned st=starts,mode=fg_scanout_te_mode;for(unsigned i=0;i<200;i++){assert(fuxi_gpu_scanout_acquire()==0);advance(5000);}
  assert(starts==st && fg_scanout_te_mode==mode && fg_pd_holds>0);}
 teStopped=0;teOrigin=ticks-(uint64_t)teFrozen*8333;
 fuxi_gpu_panel_power(1);mark("wake2");drive(ticks+400000);assert(fg_pd_state==FG_PD_AWAKE);
 for(unsigned i=0;i<10 && fg_pd_wake_pending;i++)assert(frame()==1);
 assert(!fg_pd_wake_pending && !fg_scanout_failed);
 mark("end");
 // --- fuxipanelwake=2 is refused: no sleep, nothing sent, the black frame stays.
 reset("wake2");dcs_boot(4);hasWake=1;bootWake=2;own();quiet(40000);
 fuxi_gpu_panel_power(0);assert(cpu_frame()==1);drive(ticks+2000000);
 assert(!nsends && fg_pd_state==FG_PD_AWAKE && !dsiWrites);
 // --- A DMA error in the second off batch stops scanout; no retry, no further send.
 reset("dmaerror");dcs_boot(4);own();quiet(40000);
 fuxi_gpu_panel_power(0);assert(cpu_frame()==1);dmaErrorAt=dmaStarts+2;
 drive(ticks+1000000);
 assert(fg_scanout_failed && fg_pd_dead && nsends==2);
 {unsigned n=nsends,w=dsiWrites;fuxi_gpu_panel_power(1);drive(ticks+1000000);assert(fuxi_gpu_scanout_acquire()==-1 && nsends==n && dsiWrites==w);}
 // --- A lost DMA done on a 0x51 stops scanout after the 20 ms poll; no retry.
 reset("dmatimeout");dcs_boot(3);own();quiet(40000);dmaHangAt=dmaStarts+1;
 fuxi_gpu_panel_dbv(0x150);drive(ticks+200000);
 assert(fg_scanout_failed && fg_pd_dead && nsends==1);
 {unsigned w=dsiWrites;fuxi_gpu_panel_dbv(0x160);drive(ticks+200000);assert(dsiWrites==w && nsends==1);}
 fprintf(stdout,"DONE\n");
 return 0;
}
'''

MODEL = r'''
// DSI/MDP/TE model. TE every 8333 us from teOrigin (frozen while teStopped); a START latches 20 us and its transfer runs
// 8000 us from the next TE; the write line is active only then. Every DSI write requires ownership, interrupts on, AR off,
// START clear and an idle write line; every START requires the panel state AWAKE.
static unsigned dsiWrites,dmaErrorAt=~0u,dmaHangAt=~0u,teStopped,teFrozen;
static uint64_t teOrigin,startT,xferStart=~0ull,xferEnd=~0ull;
static uint32_t fifo[64];static unsigned fifoN;
struct sendrec {uint64_t t;unsigned lp,len,n;uint32_t w[16];};
static struct sendrec sends[512];static unsigned nsends;
static unsigned fg_scanout_owned_for_model(void);
static unsigned pd_state_for_model(void);
static unsigned te_count(void) {return teStopped?teFrozen:(unsigned)((ticks-teOrigin)/8333);}
static void model_sync(void) {
 unsigned c=te_count();uint64_t edge=teOrigin+(uint64_t)c*8333;
 unsigned lines=teStopped?2400:2400+(unsigned)((ticks-edge)*2684/8333);
 intf[0x298/4]=(c<<16)|(lines&0xffffu);
 ctl[0x1c/4]=startT && ticks<startT+20?1:0;
 intf[0x2b0/4]=ticks>=xferStart && ticks<xferEnd?300+(unsigned)((ticks-xferStart)*1900/8000):2400;
}
static void advance(uint64_t us) {uint64_t end=ticks+us;while(ticks<end){uint64_t s=end-ticks<50?end-ticks:50;ticks+=s;model_sync();}}
static unsigned fg_access(uint64_t pa,uint64_t *value,unsigned wr,unsigned wide) {
 assert(!wide);
 uint32_t *reg;
 if(pa>=0xae05000 && pa<0xae06000)reg=&sspp[(pa-0xae05000)/4];
 else if(pa>=0xae16000 && pa<0xae17000)reg=&ctl[(pa-0xae16000)/4];
 else if(pa>=0xae94000 && pa<0xae95000)reg=&dsi[(pa-0xae94000)/4];
 else {assert(pa>=0xae36000 && pa<0xae37000);reg=&intf[(pa-0xae36000)/4];}
 if(wr){
  writes++;
  if(pa>=0xae94000 && pa<0xae95000){
   assert(interruptsOn && fg_scanout_owned_for_model() && fg_scanout_paneldcs_requested);
   assert(!(intf[0x2b4/4]&0x80000000u) && !(ctl[0x1c/4]&1) && !fg_busy);
   assert(!(intf[0x2b0/4]&0xffffu) || (intf[0x2b0/4]&0xffffu)>=2400);
   assert(pa!=0xae94094);
   dsiWrites++;
  }
  if(pa==0xae94110){uint32_t clear=(uint32_t)*value;*reg=(*reg&0x55555555u&~clear)|((uint32_t)*value&0xaaaaaaaau);}
  else *reg=(uint32_t)*value;
  if(pa==0xae941ec && *value==1)fifoN=0;
  if(pa==0xae9417c){assert(fifoN<16);fifo[fifoN++]=(uint32_t)*value;}
  if(pa==0xae94090){
   unsigned len=dsi[0x04c/4];
   assert(*value==1 && dsi[0x15c/4]==0x30006 && (dsi[0x03c/4]&0x10000000u) && !(dsi[0x03c/4]&0xc0000000u));
   assert(len && !(len&3) && len<=64 && fifoN==len/4+((len/4)&1) && (!((len/4)&1) || !fifo[len/4]));
   assert(nsends<512);struct sendrec *s=&sends[nsends++];s->t=ticks;s->lp=!!(dsi[0x03c/4]&0x04000000u);s->len=len;s->n=len/4;
   for(unsigned i=0;i<s->n;i++)s->w[i]=fifo[i];
   fprintf(stdout,"SEND %s t=%llu lp=%u words=",scenario,(unsigned long long)ticks,s->lp);
   for(unsigned i=0;i<s->n;i++)fprintf(stdout,"%08x%s",s->w[i],i+1<s->n?",":"\n");
   dmaStarts++;pendingDma=2;dsi[0x008/4]=2;noDone=dmaStarts==dmaHangAt;
  }
  if(pa==0xae1601c && (*value&1)){
   assert(pd_state_for_model()==0 && !(intf[0x2b4/4]&0x80000000u));
   starts++;startT=ticks;
   if(teStopped)xferStart=xferEnd=~0ull;
   else{uint64_t k=(ticks+20-teOrigin)/8333+1;xferStart=teOrigin+k*8333;xferEnd=xferStart+8000;}
   model_sync();
  }
 } else {
  reads++;
  if(pa>=0xae94000 && pa<0xae95000)dsiReads++;
  if(pa==0xae94110 && pendingDma && !noDone && !--pendingDma){dsi[0x110/4]|=1;dsi[0x008/4]=0;if(dmaStarts==dmaErrorAt)dsi[0x110/4]|=1u<<24;}
  *value=*reg;
 }
 return 0;
}
'''


def build_prefix(base):
    prefix = base[:base.index('static unsigned fg_access(')]
    prefix = prefix.replace('static int PE_parse_boot_argn(const char *name,void *value,int size) {',
                            PREFIX_EXTRA + 'static int PE_parse_boot_argn(const char *name,void *value,int size) {\n'
                            ' {unsigned ev=0;if(extra_bootarg(name,&ev)){if(size<4)return 0;*(unsigned *)value=ev;return 1;}}', 1)
    assert 'extra_bootarg(name,&ev)' in prefix
    prefix = prefix.replace('va_list args;va_start(args,format);',
                            'va_list args;va_start(args,format);{va_list c2;va_copy(c2,args);fputs("LOG ",stdout);vfprintf(stdout,format,c2);va_end(c2);}', 1)
    assert 'fputs("LOG "' in prefix
    assert 'static unsigned fg_scanout_paneldcs_requested;' in prefix
    return prefix + 'static const char *scenario;\n' + MODEL.replace('static unsigned fg_scanout_owned_for_model(void);', '')


def check_sends(out, fix):
    t1 = fix['timing@1']
    on, off = dt_packets(t1['qcom,mdss-dsi-on-command']), dt_packets(t1['qcom,mdss-dsi-off-command'])
    assert t1['qcom,mdss-dsi-on-command-state'] == 'dsi_lp_mode' and t1['qcom,mdss-dsi-off-command-state'] == 'dsi_lp_mode'
    assert fix['timing@0']['qcom,mdss-dsi-off-command'] == t1['qcom,mdss-dsi-off-command']
    assert 'qcom,bl-dcs-cmd-state' in fix['absent']          # brightness in HS
    assert fix['panel']['qcom,mdss-dsi-bl-inverted-dbv'] == ''  # MSB first
    assert int(fix['panel']['qcom,mdss-dsi-bl-min-level'], 16) == 4 and int(fix['panel']['mi,mdss-dsi-fac-bl-max-level'], 16) == 0x7ff
    assert int(fix['panel']['qcom,mdss-brightness-init-level'], 16) == 0x133

    def batch(pkts, first, last):  # DT packets first..last (1-based) as one batch: only the last may end a DT batch or wait
        sel = pkts[first - 1:last]
        assert all(p['wait'] == 0 for p in sel[:-1])
        return [(p['type'], bytes(p['data'])) for p in sel], sel[-1]['wait']
    off_batches = [batch(off, 1, 2), batch(off, 3, 8), batch(off, 9, 9)]
    assert [b[1] for b in off_batches] == [20, 100, 120] and all(off[i - 1]['flags'] == 0 for i in (2, 8, 9))
    assert all(off[i - 1]['flags'] == 0x40 for i in (1, 3, 4, 5, 6, 7))
    wake_batches = [batch(on, 27, 27), batch(on, 3, 3), batch(on, 56, 61), batch(on, 62, 63)]
    assert [b[1] for b in wake_batches] == [0, 120, 20, 0] and all(on[i - 1]['flags'] == 0 for i in (3, 61, 63))
    assert on[0]['data'][0] == 0x9e and len(on[0]['data']) + 4 > 64, 'on-command packet 1 (PPS) does not fit the FIFO: =2 refused'
    dt_all = {(p['type'], bytes(p['data'])) for p in on + off}

    sends, marks, logs = [], [], []
    for line in out.splitlines():
        if line.startswith('SEND '):
            m = re.match(r'SEND (\S+) t=(\d+) lp=(\d) words=(\S+)', line)
            sends.append({'sc': m[1], 't': int(m[2]), 'lp': int(m[3]), 'pk': fifo_packets([int(x, 16) for x in m[4].split(',')])})
        elif line.startswith('MARK '):
            marks.append(line.split()[1:3])
        elif line.startswith('LOG '):
            logs.append(line[4:])
    assert sends and out.rstrip().endswith('DONE')

    # Every packet: a brightness 39 51 MM LL in [4, 0x7ff] (0 only in E2 / the wake's on 27), or a DTB packet byte for byte.
    for s in sends:
        for p in s['pk']:
            if p['type'] == 0x39 and p['data'][0] == 0x51:
                assert len(p['data']) == 3
                v = p['data'][1] << 8 | p['data'][2]
                assert v <= 0x7ff and (v >= 4 or s['sc'] in ('e2', 'sleep') ), (s, v)
            else:
                assert (p['type'], bytes(p['data'])) in dt_all, p
    # Brightness alone in its own HS DMA (except the wake's LP on 27).
    for s in sends:
        if any(p['data'][0] == 0x51 for p in s['pk']):
            assert len(s['pk']) == 1 and (s['lp'] == 0 or bytes(s['pk'][0]['data']) == b'\x51\x00\x00')

    # Sleep/wake sequences: the exact DT batches, LP, DT waits, unlock closed, 11 >= 120 ms after 10 and 10 >= 120 ms after 11.
    seq = [s for s in sends if s['sc'] == 'sleep']
    i, nsleep, nwake = 0, 0, 0
    last10 = last11 = None
    while i < len(seq):
        pk = [(p['type'], bytes(p['data'])) for p in seq[i]['pk']]
        if pk == off_batches[0][0]:
            got = seq[i:i + 3]
            assert [[(p['type'], bytes(p['data'])) for p in s['pk']] for s in got] == [b[0] for b in off_batches]
            assert all(s['lp'] == 1 for s in got)
            assert got[1]['t'] - got[0]['t'] >= 20000 and got[2]['t'] - got[1]['t'] >= 100000
            if last11 is not None:
                assert got[2]['t'] - last11 >= 120000
            last10 = got[2]['t']; nsleep += 1; i += 3
        elif pk == wake_batches[0][0]:
            got = seq[i:i + 4]
            assert [[(p['type'], bytes(p['data'])) for p in s['pk']] for s in got] == [b[0] for b in wake_batches]
            assert all(s['lp'] == 1 for s in got)
            assert last10 is not None and got[1]['t'] - last10 >= 120000, ('11 too soon after 10', got[1]['t'] - last10)
            assert got[2]['t'] - got[1]['t'] >= 120000 and got[3]['t'] - got[2]['t'] >= 20000
            last11 = got[1]['t']; nwake += 1; i += 4
        else:
            assert pk[0][1][0] == 0x51, pk   # brightness (including the restore after the wake's frame)
            i += 1
    assert nsleep == 2 and nwake == 2, (nsleep, nwake)
    # F0 5A 5A is always closed by F0 A5 A5 within the same sequence (sleep: batches 1-3; wake: batches 1-4).
    for name, batches in (('off', off_batches), ('wake', wake_batches)):
        state = 0
        for b in batches:
            for t, d in b[0]:
                if d == b'\xf0\x5a\x5a':
                    state += 1
                elif d == b'\xf0\xa5\xa5':
                    assert state == 1; state -= 1
        assert state == 0, name
    for s in sends:  # and nowhere else is an unlock sent
        for p in s['pk']:
            if p['data'][:1] == b'\xf0':
                assert s['sc'] in ('sleep', 'dmaerror') and bytes(p['data']) in (b'\xf0\x5a\x5a', b'\xf0\xa5\xa5')
    # The DMA error run sent batch 1 and stopped at batch 2: no retry, nothing after.
    err = [s for s in sends if s['sc'] == 'dmaerror']
    assert len(err) == 2 and [(p['type'], bytes(p['data'])) for p in err[0]['pk']] == off_batches[0][0]
    assert not [s for s in sends if s['sc'] in ('default', 'unowned', 'autorefresh', 'tpg', 'wake2', 'noscanout', 'badvalue', 'goodvalue', 'cap')]
    # Logs the device run greps for.
    need = ['dcs mode=4', 'dcs armed mode=1, script starts in 60 s', 'dcs script mode=2 step=5/6 at_s=95 dbv=0',
            'dcs sleep begin black_frame=1', 'dcs sleep step=1/3 off 1-2', 'dcs sleep step=3/3 off 9', 'dcs asleep total_ms=',
            'dcs wake begin mode=soft', 'dcs wake step=2/4 on 3', 'dcs wake commands done', 'dcs wake done 51 0180',
            'dcs wake te_hz=', 'dcs fuxipanelwake=2 refused', 'dcs refused fuxipaneldcs=5', 'dcs refused fuxipaneldcs=3: needs fuxigpuscanout=1',
            'dcs waiting: scanout not owned', 'refused (autorefresh', 'dcs off 3-8 aborted', 'stopped reason=dcs send incomplete',
            'dcs #1 51 0133 lp=0 elapsed_us=']
    text = '\n'.join(logs)
    for n in need:
        assert n in text, n
    print('PASS: %d DMA sends decoded; sleep x%d / wake x%d match the live DTB off-command and on-command 27, 3, 56-61, 62-63 byte for byte '
          'with DT waits and LP; F0 unlock always closed; 11 >= 120 ms after 10' % (len(sends), nsleep, nwake))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--dtb', help="the phone's live.dtb (boot-logs/a-spk2-probe/live.dtb) to check the fixture against")
    ap.add_argument('--show', metavar='SCENARIO', help='also print the model run (sends, marks, logs) of one scenario')
    args = ap.parse_args()
    fix = json.loads(FIXTURE.read_text())
    if args.dtb:
        check_fixture_against_dtb(fix, args.dtb)
    scanout = module('panel_dcs_scanout', 'tools/test-fuxi-gpu-scanout.py')
    display = module('panel_dcs_display', 'tools/test-fuxi-unixnu.py')
    inc = (ROOT / 'xnu/fuxi-adreno/fuxi_gpu_scanout.inc').read_text()
    inc = re.sub(r'__asm__ volatile\([^;]*\);', '(void)0;', inc)
    # Static facts of the production source the model cannot see.
    assert '!fg_scanout_panel120_requested && !fg_scanout_paneldcs_requested && w->pa == 0xae94000' in (ROOT / 'xnu/fuxi-adreno/fuxi_adreno.c').read_text()
    acq = inc[inc.index('static int fg_scanout_acquire(unsigned publication)\n{'):]
    acq = acq[:acq.index('\n}\n')]
    assert acq.index('if (fg_pd_state != FG_PD_AWAKE) { fg_pd_holds++; return 0; }') < acq.index('if (fg_busy && !(fg_abi_hard')
    assert acq.index('if (fg_pd_state != FG_PD_AWAKE)') < acq.index('if (fg_scanout_te_mode) {')
    assert acq.index('if (fg_scanout_panel120_requested && !fg_scanout_panel120_attempted)') < acq.index('int sent = fg_pd_inline();')
    run = inc[inc.index('static void fg_pd_run(thread_call_param_t a'):]
    run = run[:run.index('\n}\n')]
    assert run.index('FG_GUARD;') < run.index('next = fg_pd_run_locked();') < run.index('fg_pd_schedule(next)')
    assert 'delay(' not in inc[inc.index('#define FG_PD_DBV_HARD'):inc.index('static void fg_pd_configure(void)\n{')].replace('fg_dsi_poll', '')
    for forbidden in ('0xae94094', 'regulator', 'gpio', 'GPIO'):
        block = inc[inc.index('#define FG_PD_DBV_HARD'):inc.index('/* 1 writable, 0 wait, -1 held after error.')]
        assert forbidden not in block.replace('No\n * regulator, GPIO or reset', '').replace('regulator, GPIO or reset', ''), forbidden
    source = build_prefix(scanout.PREFIX) + display.TE_OBSERVER + display.PRESENT_TIMES + inc + TEST
    with tempfile.TemporaryDirectory(prefix='fuxi-panel-dcs-') as tmp:
        src, exe = Path(tmp) / 'dcs.c', Path(tmp) / 'dcs'
        src.write_text(source)
        subprocess.run(['clang', '-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function', '-Wno-unused-variable',
                        '-Wno-unused-but-set-variable',
                        '-fsanitize=address,undefined', '-fno-omit-frame-pointer', str(src), '-o', str(exe)], check=True)
        run = subprocess.run([str(exe)], capture_output=True, text=True)
        if run.returncode:
            print('\n'.join(run.stdout.splitlines()[-40:]))
            print(run.stderr[-3000:])
            raise SystemExit('FAIL: the model run aborted (%d)' % run.returncode)
        out = run.stdout
    if args.show:
        current = None
        for line in out.splitlines():
            if line.startswith('SCENARIO '):
                current = line.split()[1]
            if current == args.show:
                print(line)
    check_sends(out, fix)
    print('PASS: fuxipaneldcs off by default, exact values, refusals without ownership/AR/DSI state, clamp and 0x7ff cap, 39 51 MM LL, '
          '16 ms rate and coalescing, E1/E2 timing, sleep/wake state machine (120 ms rule, no START, TE stop, restore after one frame), '
          'panelwake=2 refused, DMA error/timeout stop without retry')


if __name__ == '__main__':
    main()
