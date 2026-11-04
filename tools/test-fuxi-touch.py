#!/usr/bin/env python3
"""Run actual FTS report/FIFO and SweetHID event methods under ASan/UBSan.

Only local production code is compiled. IOKit allocation, dispatch and SPI are
host stand-ins; no upstream source, device, firmware or network is used.
"""
from pathlib import Path
import argparse
import re
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'xnu/fuxi-h1'


def function(source, name):
    match = re.search(r'^(?:static |extern "C" )?(?:void|int|bool|uint64_t)\n' +
                      re.escape(name) + r'\([^;]*?\)\n\{', source, re.M)
    assert match, name
    depth, end = 1, match.end()
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[match.start():end] + '\n'


MODEL = r'''
#include <cassert>
#include <cstdint>
#include <cerrno>
#include <cstdio>
#include <cstring>
#include <cstdarg>
#include <vector>
#include <map>
#include <algorithm>
#include "fuxi_key_state.h"
using UInt32 = uint32_t;
using IOFixed = int32_t;
using vm_offset_t = uintptr_t;
#define TRUE 1
#define SWEET_TOUCH_FINGERS 10
#define SWEET_TOUCH_EDGE_PX 48
int sweet_fb_fit; /* SweetFramebuffer: sweetfbfit accepted */
#define FT_MAX 10u
#define FT_EV_READY 0x03u
#define FT_EV_ENTER 0x13u
#define FT_EV_MOTION 0x23u
#define FT_EV_LEAVE 0x33u
#define FT_TYPE_HOVER 0x05u
// Field names model the IOHIDEvent accessor boundary; serialized ABI is not modeled.
enum {
 kIOHIDEventFieldDigitizerCollection, kIOHIDEventFieldDigitizerIndex,
 kIOHIDEventFieldDigitizerIdentity, kIOHIDEventFieldDigitizerTouch,
 kIOHIDEventFieldDigitizerEventMask, kIOHIDEventFieldDigitizerX,
 kIOHIDEventFieldDigitizerY, kIOHIDEventFieldDigitizerRange,
 kIOHIDEventFieldDigitizerButtonMask
};
enum {kIOHIDDigitizerTransducerTypeHand, kIOHIDDigitizerTransducerTypeFinger};
enum {
 kIOHIDDigitizerEventRange=1, kIOHIDDigitizerEventTouch=2,
 kIOHIDDigitizerEventPosition=4, kIOHIDDigitizerEventIdentity=32,
 kIOHIDDigitizerEventFromEdgeTip=0x800
};
static uint64_t clock_now=100000;
static uint64_t mach_absolute_time() { return clock_now++; }
static void absolutetime_to_nanoseconds(uint64_t abs, uint64_t *ns) { *ns=abs*1000; }
static unsigned perf, wake_calls, logs;
static void fuxi_perf_input() { perf++; }
static bool fuxi_keys_touch_wake() { wake_calls++;return true; }
static int model_printf(const char *format, ...) { (void)format;logs++;return 0; }
#define printf model_printf
#define IOLog model_printf
static int lock_depth;
static void IOLockLock(int *) { assert(!lock_depth);lock_depth++; }
static void IOLockUnlock(int *) { assert(lock_depth==1);lock_depth--; }
struct EventData {
 uint64_t time;
 UInt32 type, transducer;
 std::map<int,int> fields;
 std::vector<EventData> children;
};
static std::vector<EventData> dispatched;
static int fail_after=-1, append_fail_after=-1, live;
class IOHIDEvent;
struct OSArray {
 std::vector<IOHIDEvent *> objects;
 unsigned getCount() { return objects.size(); }
};
class IOHIDEvent {
 int refs=1;
 OSArray children;
public:
 EventData data;
 IOHIDEvent() { live++; }
 ~IOHIDEvent() { for(auto *c:children.objects)c->release();live--; }
 void release() { if(!--refs)delete this; }
 void appendChild(IOHIDEvent *child) {
  if(append_fail_after==0)return;
  if(append_fail_after>0)append_fail_after--;
  child->refs++;children.objects.push_back(child);
 }
 OSArray *getChildren() { return children.objects.empty()?nullptr:&children; }
 void setIntegerValue(int field, int value) { data.fields[field]=value; }
 void setFixedValue(int field, IOFixed value) { data.fields[field]=value; }
 EventData snapshot() {
  auto result=data;
  for(auto *c:children.objects)result.children.push_back(c->snapshot());
  return result;
 }
 static IOHIDEvent *digitizerEvent(uint64_t time, UInt32 id, UInt32 type,
                                 bool range, UInt32 buttons, IOFixed x, IOFixed y) {
  if(fail_after==0)return nullptr;
  if(fail_after>0)fail_after--;
  auto *event=new IOHIDEvent;
  event->data.time=time;event->data.type=type;event->data.transducer=id;
  event->setIntegerValue(kIOHIDEventFieldDigitizerRange,range);
  event->setIntegerValue(kIOHIDEventFieldDigitizerButtonMask,buttons);
  event->setFixedValue(kIOHIDEventFieldDigitizerX,x);
  event->setFixedValue(kIOHIDEventFieldDigitizerY,y);
  return event;
 }
};
class SweetHIDTouch {
public:
 int storage=0, *fLock=&storage;
 int fLogicalW=1125, fLogicalH=2436, fOffX=22, fOffY=18;
 bool fDown[SWEET_TOUCH_FINGERS]={}, fEdge[SWEET_TOUCH_FINGERS]={};
 IOFixed fX[SWEET_TOUCH_FINGERS]={}, fY[SWEET_TOUCH_FINGERS]={};
 void touch(int finger, bool down, int x, int y);
 bool report(UInt32 mask, const int *x, const int *y);
 void dispatchEvent(IOHIDEvent *e) { assert(!lock_depth);dispatched.push_back(e->snapshot()); }
};
static SweetHIDTouch service;
static SweetHIDTouch *gSweetTouch=&service;
uint64_t sweet_hid_last_input;
static std::vector<uint8_t> fifo;
static unsigned reads, fail_read;
static int ft_read(const uint8_t *cmd, unsigned nc, uint8_t *out, unsigned n) {
 assert(nc==1 && cmd[0]==0x87);reads++;
 if(reads==fail_read)return 5;
 assert(fifo.size()>=n);memcpy(out,fifo.data(),n);fifo.erase(fifo.begin(),fifo.begin()+n);
 return 0;
}
'''

TEST = r'''
static void reset(bool batch=true) {
 assert(!live && !lock_depth);
 ft={};ft.batch=batch;service=SweetHIDTouch();service.fLock=&service.storage;
 dispatched.clear();fifo.clear();reads=fail_read=0;
 fail_after=append_fail_after=-1;perf=logs=wake_calls=0;
}
static int field(const EventData &e, int key) { return e.fields.at(key); }
static const EventData &child(unsigned id) {
 for(auto &c:dispatched.back().children)
  if(field(c,kIOHIDEventFieldDigitizerIndex)==(int)id)return c;
 assert(false);return dispatched.back();
}
static void check(UInt32 down, UInt32 lifts) {
 assert(!dispatched.empty() && !live && !lock_depth);
 auto &e=dispatched.back();
 assert(e.type==kIOHIDDigitizerTransducerTypeHand);
 assert(field(e,kIOHIDEventFieldDigitizerCollection)==1);
 assert(field(e,kIOHIDEventFieldDigitizerTouch)==(down!=0));
 assert(field(e,kIOHIDEventFieldDigitizerRange)==(down!=0));
 assert(field(e,kIOHIDEventFieldDigitizerButtonMask)==(down!=0));
 UInt32 seen=0, merged=0;
 for(auto &c:e.children) {
  unsigned id=field(c,kIOHIDEventFieldDigitizerIndex);
  assert(id<10 && !(seen&(1u<<id)));seen|=1u<<id;
  assert(c.time==e.time && c.type==kIOHIDDigitizerTransducerTypeFinger);
  assert(c.transducer==id+2 && field(c,kIOHIDEventFieldDigitizerIdentity)==(int)id+2);
  assert(field(c,kIOHIDEventFieldDigitizerTouch)==bool(down&(1u<<id)));
  assert(field(c,kIOHIDEventFieldDigitizerRange)==bool(down&(1u<<id)));
  if(lifts&(1u<<id))assert(field(c,kIOHIDEventFieldDigitizerEventMask)&kIOHIDDigitizerEventTouch);
  merged|=field(c,kIOHIDEventFieldDigitizerEventMask);
 }
 assert(seen==(down|lifts));
 assert(field(e,kIOHIDEventFieldDigitizerEventMask)==(int)merged);
}
static void event(unsigned kind, unsigned id, unsigned type, unsigned x, unsigned y) {
 unsigned xx=x*10, yy=y*10;
 uint8_t e[8]={(uint8_t)kind,(uint8_t)((id<<4)|type),(uint8_t)xx,(uint8_t)(xx>>8),
               (uint8_t)yy,(uint8_t)(yy>>8),0,0};
 fifo.insert(fifo.end(),e,e+8);
}
static int poll(bool deliver=true) {
 if(fifo.empty())fifo.resize(8);
 fifo[7]=(uint8_t)(fifo.size()/8-1);reads=0;
 int error=ft_poll(deliver);
 if(!error)assert(fifo.empty());
 return error;
}
static void batching() {
 reset();
 ft.x[2]=400;ft.y[2]=700;ft.x[7]=600;ft.y[7]=900;
 ft_report((1u<<2)|(1u<<7));check((1u<<2)|(1u<<7),0);
 assert(dispatched.size()==1 && ft.c_multi==1 && ft.c_max==2 && !ft.c_lifts);
 // Both move: one report, with simultaneous new positions.
 ft.x[2]+=10;ft.x[7]+=20;ft_report(ft.active);check(ft.active,0);
 assert(dispatched.size()==2 && ft.c_multi==2);
 assert(field(child(2),kIOHIDEventFieldDigitizerX)==((410+22)*65536/1125));
 assert(field(child(7),kIOHIDEventFieldDigitizerX)==((620+22)*65536/1125));
 int oldx=field(child(2),kIOHIDEventFieldDigitizerX);
 ft.x[2]=0;ft.y[2]=0;ft_report(1u<<7);check(1u<<7,1u<<2);
 assert(field(child(2),kIOHIDEventFieldDigitizerX)==oldx);
 assert(!(field(child(7),kIOHIDEventFieldDigitizerEventMask)&kIOHIDDigitizerEventTouch));
 assert(ft.c_lifts==1 && ft.c_max==2);
 ft_report(0);check(0,1u<<7);
 assert(ft.contacts==1 && ft.c_lifts==2 && ft.c_reports==4);
 unsigned count=dispatched.size();ft_report(0);assert(dispatched.size()==count);
 // Same count, entirely different identities: lift old and introduce new.
 ft_report((1u<<1)|(1u<<9));check(ft.active,0);
 ft_report((1u<<4)|(1u<<8));check(ft.active,(1u<<1)|(1u<<9));
 assert(ft.c_lifts==2 && ft.c_multi==2 && ft.c_max==2);
 ft_report(0);check(0,(1u<<4)|(1u<<8));
 // Ten fingers, and ten simultaneous lifts, without a packed-report capacity limit.
 ft_report(0x3ff);check(0x3ff,0);
 assert(dispatched.back().children.size()==10 && ft.c_max==10);
 ft_report(0);check(0,0x3ff);assert(ft.c_lifts==10);
 // Half the hardware IDs replaced at a constant five fingers down.
 ft_report(0x155);ft_report(0x2aa);check(0x2aa,0x155);
 assert(dispatched.back().children.size()==10 && ft.c_lifts==5);
 ft_report(0);
 puts("PASS: batched movement, ID-based partial/final/replacement lifts, ten fingers and report counters");
}
static void coordinates_edges_and_legacy() {
 reset();ft.x[0]=0;ft.y[0]=2399;ft_report(1);check(1,0);
 assert(field(child(0),kIOHIDEventFieldDigitizerEventMask)&kIOHIDDigitizerEventFromEdgeTip);
 ft.x[0]=500;ft.y[0]=1000;ft_report(1);
 assert(field(child(0),kIOHIDEventFieldDigitizerEventMask)&kIOHIDDigitizerEventFromEdgeTip);
 ft_report(0);assert(field(child(0),kIOHIDEventFieldDigitizerEventMask)&kIOHIDDigitizerEventFromEdgeTip);
 ft_report(1);assert(!(field(child(0),kIOHIDEventFieldDigitizerEventMask)&kIOHIDDigitizerEventFromEdgeTip));
 unsigned count=dispatched.size();ft_report(1);assert(dispatched.size()==count); // unchanged suppression
 ft_report(0);
 service.fLogicalW=1080;service.fLogicalH=2400;service.fOffX=service.fOffY=0;
 ft.x[5]=-100;ft.y[5]=3000;ft_report(1u<<5);check(1u<<5,0);
 assert(field(child(5),kIOHIDEventFieldDigitizerX)==0);
 assert(field(child(5),kIOHIDEventFieldDigitizerY)==2399*65536/2400);
 ft_report(0);
 // Absent/zero option keeps per-finger dispatch, same ID state and counters.
 reset(false);assert(!ft.batch);
 ft.x[2]=400;ft.y[2]=700;ft.x[7]=600;ft.y[7]=900;
 ft_report((1u<<2)|(1u<<7));assert(dispatched.size()==2);check(ft.active,0);
 ft.x[2]++;ft.x[7]++;ft_report(ft.active);assert(dispatched.size()==4);
 ft_report(1u<<7);assert(dispatched.size()==5);check(1u<<7,1u<<2);
 ft_report(0);assert(dispatched.size()==6);check(0,1u<<7);
 assert(ft.c_multi==2 && ft.c_max==2 && ft.c_lifts==2);
 puts("PASS: centered/native mapping, clamp, edge lifetime, unchanged suppression and legacy default");
}
static void allocation_failures() {
 for(int failure=0;failure<4;failure++) {
  reset();ft_report(7);unsigned count=dispatched.size();
  fail_after=failure;ft_report(0);
  assert(dispatched.size()==count && !live && !lock_depth && ft.retry);
  for(int id=0;id<3;id++)assert(service.fDown[id]);
  unsigned reports=ft.c_reports, lifts=ft.c_lifts;
  ft_retry_report();assert(ft.retry && dispatched.size()==count);
  fail_after=-1;ft_retry_report();check(0,7);
  assert(!ft.retry && ft.c_reports==reports && ft.c_lifts==lifts);
 }
 for(int failure=0;failure<3;failure++) {
  reset();append_fail_after=failure;ft_report(7);
  assert(dispatched.empty() && !live && !lock_depth && ft.retry);
  for(bool down:service.fDown)assert(!down);
  append_fail_after=-1;ft_retry_report();check(7,0);assert(!ft.retry);
 }
 reset();gSweetTouch=nullptr;
 ft_report(1);assert(sweet_hid_last_input && dispatched.empty() && ft.retry);
 // A failed down superseded by up must not become a stale delayed down.
 gSweetTouch=&service;ft_report(0);ft_retry_report();assert(dispatched.empty() && !ft.retry);
 // A new ID reconciles a failed previous final-up against the HID ID state.
 reset();ft_report(1);fail_after=0;ft_report(0);assert(ft.retry);
 fail_after=-1;ft_report(1u<<7);check(1u<<7,1);assert(!ft.retry);
 ft_report(0);
 puts("PASS: allocation/append failures publish no partial set, retry final lifts, latest-set replacement and last-input entry point");
}
static void fifo_tests() {
 reset();
 event(FT_EV_ENTER,2,1,400,700);event(FT_EV_ENTER,7,1,600,900);
 assert(!poll() && reads==2 && dispatched.size()==1);check(ft.active,0);
 // A drain is a delta stream: no new events cannot lift stationary contacts.
 assert(!poll() && ft.active==((1u<<2)|(1u<<7)) && dispatched.size()==1);
 event(FT_EV_MOTION,7,1,650,950);event(FT_EV_LEAVE,2,0,0,0);
 assert(!poll() && dispatched.size()==2);check(1u<<7,1u<<2);
 // Hover becomes a lift; type zero without LEAVE and invalid IDs are ignored.
 event(FT_EV_MOTION,7,FT_TYPE_HOVER,0,0);assert(!poll());check(0,1u<<7);
 unsigned count=dispatched.size();
 event(FT_EV_ENTER,1,0,100,100);event(FT_EV_ENTER,15,1,100,100);
 assert(!poll() && dispatched.size()==count);
 event(FT_EV_ENTER,3,1,100,100);assert(!poll(false) && dispatched.size()==count);
 event(FT_EV_LEAVE,3,0,0,0);assert(!poll(false));
 event(FT_EV_ENTER,3,1,100,100);assert(!poll());check(1u<<3,0);
 event(FT_EV_READY,0,0,0,0);assert(!poll() && ft.ic_reset);
 ft_report(0);check(0,1u<<3); // thread's reset/display-off/error release call
 // Failed first/second reads do not apply half a batch.
 for(unsigned failure=1;failure<=2;failure++) {
  reset();event(FT_EV_ENTER,2,1,400,700);event(FT_EV_ENTER,7,1,600,900);
  fail_read=failure;assert(poll()==5 && !ft.active && dispatched.empty());
 }
 // Tap-to-wake's suppressed IDs remain hidden until they lift.
 reset();
 event(FT_EV_ENTER,4,1,500,1000);assert(!poll(false) && wake_calls==1 && ft.wake.woke);
 event(FT_EV_MOTION,4,1,500,900);assert(!poll() && dispatched.empty());
 event(FT_EV_LEAVE,4,0,0,0);assert(!poll() && dispatched.empty());
 event(FT_EV_ENTER,4,1,500,1000);assert(!poll());check(1u<<4,0);
 ft_report(0);
 puts("PASS: actual two-read FIFO decoder, ID/type/hover handling, reset release, read failures and wake suppression");
}
static void randomized_sets() {
 reset();uint32_t seed=12345, previous=0;
 for(unsigned n=0;n<2000;n++) {
  seed=seed*1664525+1013904223;uint32_t mask=(seed>>16)&0x3ff;
  for(unsigned id=0;id<10;id++) {
   seed=seed*1664525+1013904223;ft.x[id]=(int)(seed%1300)-100;
   seed=seed*1664525+1013904223;ft.y[id]=(int)(seed%2700)-100;
  }
  unsigned count=dispatched.size();ft_report(mask);
  if(dispatched.size()>count)check(mask,previous&~mask);
  for(unsigned id=0;id<10;id++)assert(service.fDown[id]==bool(mask&(1u<<id)));
  previous=mask;
 }
 ft_report(0);if(previous)check(0,previous);
 puts("PASS: 2,000 randomized full contact sets, ID uniqueness, masks, lifts and bounded coordinates");
}

// sweetfbfit: x/1080 and (y-31)/2338 of the frame (iOS x/0.96, (y-31)*2436/2338), bars clamp to the frame's first/last row,
// edges count from the frame's top/bottom; both delivery paths. Off again: the centred crop mapping, unchanged.
static void fit_mapping() {
 for(int batch=0;batch<2;batch++) {
  reset(batch);sweet_fb_fit=1;
  struct {int x,y;} pts[]={{0,31},{1079,2368},{540,1200},{100,0},{100,30},{100,2369},{100,2399},{-5,3000},{777,1500}};
  for(auto &q:pts) {
   ft.x[3]=q.x;ft.y[3]=q.y;ft_report(1u<<3);
   int x=q.x<0?0:q.x>1079?1079:q.x, y=q.y<31?31:q.y>2368?2368:q.y;
   assert(field(child(3),kIOHIDEventFieldDigitizerX)==(int)(((int64_t)x<<16)/1080));
   assert(field(child(3),kIOHIDEventFieldDigitizerY)==(int)(((int64_t)(y-31)<<16)/2338));
   double ix=field(child(3),kIOHIDEventFieldDigitizerX)/65536.0*1125, iy=field(child(3),kIOHIDEventFieldDigitizerY)/65536.0*2436;
   assert(ix>=x/0.96-0.02 && ix<=x/0.96+0.0001 && iy>=(y-31)*2436.0/2338-0.04 && iy<=(y-31)*2436.0/2338+0.0001);
   ft_report(0);
  }
  // Edges: from the frame's top/bottom (and through the bars), not from the panel's alone.
  struct {int y;bool edge;} rows[]={{0,true},{31+47,true},{31+48,false},{2368-48,false},{2368-47,true},{2399,true}};
  for(auto &r:rows) {
   ft.x[1]=540;ft.y[1]=r.y;ft_report(1u<<1);
   assert(!!(field(child(1),kIOHIDEventFieldDigitizerEventMask)&kIOHIDDigitizerEventFromEdgeTip)==r.edge);
   ft_report(0);
  }
 }
 // sweetfbfit=2: x/1080 and y/2400 of the frame (iOS x/0.96, y*2436/2400), no bars to clamp, the panel's own edges.
 for(int batch=0;batch<2;batch++) {
  reset(batch);sweet_fb_fit=2;
  struct {int x,y;} pts[]={{0,0},{1079,2399},{540,1200},{100,30},{100,2369},{-5,3000},{777,1500}};
  for(auto &q:pts) {
   ft.x[3]=q.x;ft.y[3]=q.y;ft_report(1u<<3);
   int x=q.x<0?0:q.x>1079?1079:q.x, y=q.y<0?0:q.y>2399?2399:q.y;
   assert(field(child(3),kIOHIDEventFieldDigitizerX)==(int)(((int64_t)x<<16)/1080));
   assert(field(child(3),kIOHIDEventFieldDigitizerY)==(int)(((int64_t)y<<16)/2400));
   double iy=field(child(3),kIOHIDEventFieldDigitizerY)/65536.0*2436;
   assert(iy>=y*2436.0/2400-0.04 && iy<=y*2436.0/2400+0.0001);
   ft_report(0);
  }
  struct {int y;bool edge;} rows[]={{0,true},{47,true},{48,false},{2399-48,false},{2399-47,true},{2399,true}};
  for(auto &r:rows) {
   ft.x[1]=540;ft.y[1]=r.y;ft_report(1u<<1);
   assert(!!(field(child(1),kIOHIDEventFieldDigitizerEventMask)&kIOHIDDigitizerEventFromEdgeTip)==r.edge);
   ft_report(0);
  }
 }
 sweet_fb_fit=0;reset();ft.x[2]=410;ft.y[2]=700;ft_report(1u<<2);
 assert(field(child(2),kIOHIDEventFieldDigitizerX)==((410+22)*65536/1125) && field(child(2),kIOHIDEventFieldDigitizerY)==((700+18)*65536/2436));
 ft_report(0);
 puts("PASS: sweetfbfit touch: mode 1 x/1080, (y-31)/2338, bars clamp, frame edges; mode 2 x/1080, y/2400, panel edges; batched and per-finger paths; off path unchanged");
}
int main() {
 batching();coordinates_edges_and_legacy();allocation_failures();fifo_tests();randomized_sets();fit_mapping();
 assert(!live && !lock_depth);
}
'''


def kernel_syntax(buildtree):
    """Check our full TUs with saved 8796 headers/flags; run no tree scripts."""
    config = buildtree / 'build/xnu.obj/DEVELOPMENT_ARM64_FUXI'
    if not config.exists():
        config = buildtree / 'build/xnu.obj/DEVELOPMENT_ARM64_SWEET'
    with tempfile.TemporaryDirectory(prefix='fuxi-touch-headers-') as temp:
        temp = Path(temp)
        for name in ('fuxi_touch.c', 'fuxi_key_state.h', 'SweetHID.cpp'):
            (temp / name).write_bytes((SRC / name).read_bytes())
        for component, name in (('bsd', 'fuxi_touch.c'), ('iokit', 'SweetHID.cpp')):
            cwd = config / component / 'DEVELOPMENT'
            old_root = re.search(r'^SOURCE_DIR=(.+)/xnu$', (cwd / 'Makefile').read_text(), re.M)[1]
            raw = (cwd / '.CFLAGS').read_text().replace('\0', '')
            raw = re.sub(r'(-DCONFIG_NMBCLUSTERS=\(\(1024 \* 512\) / MCLBYTES\))',
                         lambda m: shlex.quote(m[1]), raw)
            saved = shlex.split(raw)[1:]
            if component == 'iokit':
                saved += shlex.split((cwd / '.CXXFLAGS').read_text().replace('\0', ''))[1:]
            command = ['clang++' if component == 'iokit' else 'clang',
                       '--target=arm64-apple-macos14', '-fsyntax-only', '-DFUXI',
                       '-mgeneral-regs-only', '-Werror', '-Wno-c99-designator', '-Wno-reorder-init-list',
                       '-D__builtin_xnu_type_signature(...)=""', '-D__builtin_xnu_type_summary(...)=0',
                       '-D__builtin_xnu_types_compatible(...)=1',
                       '-I' + str(buildtree / 'xnu/iokit/Kernel/arm/hid')]
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
                             '-I' + str(buildtree / 'sdk-kernel-platform/platform'), arg)
                command.append(arg)
            subprocess.run(command + [str(temp / name)], cwd=cwd, check=True)
            print('PASS: complete ' + name + ' arm64 syntax against saved XNU headers/flags')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--xnu-buildtree', type=Path, help='optional saved XNU tree for full-TU syntax checks')
    args = parser.parse_args()
    subprocess.run(['python3', str(ROOT / 'tools/sync-fuxi-h1.py'), '--check'], check=True)
    hid = (SRC / 'SweetHID.cpp').read_text()
    touch = (SRC / 'fuxi_touch.c').read_text()
    state = touch[touch.index('static struct {'):touch.index('} ft;') + len('} ft;')]
    fit = '\n' + '\n'.join(re.findall(r'^#define SWEET_FIT_\w+ [^\n]*', hid, re.M)) + '\n'  # state ends without a newline
    assert fit.count('#define') == 3
    methods = fit + ''.join(function(hid, name) for name in (
        'SweetHIDTouch::touch', 'SweetHIDTouch::report',
        'sweet_hid_touch', 'sweet_hid_touch_report'))
    driver = ''.join(function(touch, name) for name in (
        'ft_us', 'ft_contact_end', 'ft_report', 'ft_retry_report', 'ft_poll'))
    assert 'unsigned batch;' in state
    assert 'PE_parse_boot_argn("fuxitbatch", &ft.batch, sizeof(ft.batch))' in touch
    assert 'ft.batch =' not in touch  # static-zero default; only boot parsing enables it
    assert 'multi %u max %u lifts %u' in touch
    assert function(touch, 'ft_thread').count('ft_retry_report();') == 1
    with tempfile.TemporaryDirectory(prefix='fuxi-touch-test-') as tmp:
        folder = Path(tmp)
        (folder / 'fuxi_key_state.h').write_text((SRC / 'fuxi_key_state.h').read_text())
        source, binary = folder / 'touch.cpp', folder / 'touch'
        source.write_text(MODEL + state + methods + driver + TEST)
        subprocess.run(['clang++', '-std=c++17', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                        '-fsanitize=address,undefined', str(source), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True)
    if args.xnu_buildtree:
        kernel_syntax(args.xnu_buildtree.resolve())


if __name__ == '__main__':
    main()
