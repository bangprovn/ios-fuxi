#!/usr/bin/env python3
"""Offline M6 submit tests, compiled from the actual 0096 C functions.

Models ring consumption, page translation and pixel copies, not SQE, real
cache coherency, secure firmware, bus-drain hardware or display visibility.
"""
from pathlib import Path
import importlib.util
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('sync', ROOT / 'tools/sync-fuxi-adreno-shared.py')
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


def function(source, name):
    match = re.search(r'^(?:static )?(?:unsigned|uint32_t|uintptr_t|kern_return_t|int|bool|void) ' + name + r'\([^;]*?\)\s*\{', source, re.M)
    assert match, name
    depth, end = 1, match.end()
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[match.start():end] + '\n'


PREFIX = r'''
#include <assert.h>
#define FG_GUARD
#include <stdint.h>
#define FG_ABI_WAKE() ((void)0)
static unsigned fg_abi_present_wait, fg_client_busy, fg_scanout_owned, fg_scanout_writable, fg_scanout_requested, fg_scanout_preclean;
static unsigned fg_scanout_double_active,fg_scanout_front,fg_scanout_back_ready,fg_scanout_ready_drops,fg_scanout_triple_requested,fg_scanout_double_requested,fg_scanout_ready,fg_scanout_copy;
static uintptr_t fg_scanout_source;
static unsigned fg_scanout_pitch,fg_scanout_sx,fg_scanout_sy,fg_scanout_dx,fg_scanout_dy,fg_scanout_w,fg_scanout_h;
static unsigned scanoutStops,verifyBad,dcvsKicks;
static void fg_dcvs_kick(void) {dcvsKicks++;}
static unsigned irqQuiesces;static void fg_irq_quiesce(void) {irqQuiesces++;}
static int fg_scanout_verify(void) {return !verifyBad;}
static int fg_scanout_stop(const char *reason) {(void)reason;scanoutStops++;fg_scanout_writable=0;return -1;}
#include <stdint.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
typedef unsigned ppnum_t;
#define PAGE_SHIFT 14
#define PAGE_SIZE (1u<<PAGE_SHIFT)
#define PAGE_MASK 16383u
#define FALSE 0
#define FG_SOURCE_VA 0x50000000u
#define FG_SOURCE_BYTES 0x1000000u
#define FG_SOURCE_TABLE 0x800000u
#define G3_FENCE_PA 0x10000u
#define G3_FENCE_VA 0x40029000u
#define G4_FB_VA 0x41000000u
#define GPU_REG_BASE 0x3d00000u
#define GPU_SMMU_BASE 0x3da0000u
#define SMMU_S2CR(i) (0xc00+4*(i))
#define FG_CACHE_SLOTS 4u
#define FG_SOURCE_L3 8u
struct fg_present_times {uint64_t submitted,fence,start,previous_start;};
static struct fg_present_times fg_scanout_stamps;
struct fg_times {
 uint64_t pin, lookup, map, clean, submit, wait, unmap, cpu;
 unsigned attempts, hits, misses, sleeps, blocks, groups;
};
static struct fg_times times;
static uint64_t fg_generation[4], model_l2[512];
#define FG_INL_SRC_VA 0x90000000u
#define FG_CP_STAMP 192u
static uint32_t fg_cp_submit_lo, fg_cp_n, fg_cp_bad, fg_cp_pitch, fg_cp_rects; static uint64_t fg_cp_sum[4], fg_cp_max[4];
static struct { uint32_t s2s_us, blit_us, ahead, buffer, te, xfer_us, parked; } fg_copy_log; static unsigned inflight_model, cf_logs;
static unsigned fg_abi_inflight(void) { return inflight_model; } static void fg_cf_log(void) { cf_logs++; }
enum { FG_PK_COPY, FG_PK_IDLE, FG_PK_CAP, FG_PK_SUBMIT, FG_PK_NOTDUE, FG_PK_OTHER, FG_PK_REASONS };
static unsigned fg_pk_n, pk_flushes, pk_flush_why, pk_wptr_at_flush; static unsigned g3_wptr; /* tentative; defined with the ring model below */
static int fg_pk_flush(unsigned why) { pk_flushes++; pk_flush_why=why; pk_wptr_at_flush=g3_wptr; fg_pk_n=0; return 0; } /* model: the backend rings it (test-fuxi-metal covers fg_pk_flush) */
#define FG_INL_FB_VA 0xa0000000u
static unsigned fg_abi_inline, fg_inl_ready, fg_root_client, fg_inl_busy, fg_inl_fbmask, inl_usable_model, inl_present_calls;
enum { FG_INL_NOROOT, FG_INL_WINDOW, FG_INL_RING, FG_INL_BUFFERS, FG_INL_CLIENT, FG_INL_SWITCH, FG_INL_REASONS };
static uint32_t fg_inl_n, fg_inl_fb[FG_INL_REASONS], fg_inl_lat_n;
static uint64_t fg_inl_lat, fg_inl_latmax, model_mirror[512];
static void fg_inl_mirror(unsigned idx, uint64_t v) { model_mirror[((FG_SOURCE_VA>>21)&511)+idx]=v; }
static int fg_inl_usable(void) { return fg_abi_inline && inl_usable_model; }
static int fg_inl_to_present(void) { inl_present_calls++; if(fg_client_busy) return 0; fg_root_client=0; inl_usable_model=0; return 1; }
static void absolutetime_to_nanoseconds(uint64_t a, uint64_t *n) { *n=a; }
static unsigned fg_abi_coh, fg_abi_coh_wb, fg_src_n_wb, fg_src_n_nc, fg_src_n_ncstale; static unsigned char fg_src_wb[4];
static ppnum_t fg_ppns[FG_SOURCE_BYTES/PAGE_SIZE+1];
static uint64_t model_leaves[16384];
static volatile uint64_t *fg_l2(void) { return model_l2; }
#define G_PTR(pa) ((uintptr_t)((uint8_t *)model_leaves+(pa)-FG_SOURCE_TABLE))
#define G_PTR_RANGE(pa, bytes) G_PTR(pa)
static uint64_t fg_faults,fg_suspends;
static unsigned fg_reset_held;
static unsigned fg_state=1, fg_force, fg_seq, fg_access_failed, g3_wptr;
static unsigned g3_slot[2]={1,2};
static uint64_t fg_deadline, fg_submit_time, ticks;
static unsigned fg_busy, fg_magic;
static uint64_t model_counter(const char *r) { return strcmp(r,"cntfrq_el0") ? ++ticks : 1000000; }
#define rsr(r) model_counter(r)
static int fg_expired(void) { return model_counter("ticks") >= fg_deadline; }
static void delay(unsigned n) { ticks+=n; }
static unsigned mode, fail_tlb, tlbs, isolated, doorbells, ring[8192], rptr, wptr, fence;
static uint64_t completion;
static unsigned cache_clean, pending, maxwords, wrap_count, checks, lookups, contiguous_pa;
static uint64_t clean_bytes;
static unsigned gbif, reset_held, faulted_sids;
static uint8_t *source_buf, *panel;
static unsigned expected_pages[4096], paddr_missing;
static unsigned pdst[2400*1080], pdata[2400*1080];
static unsigned fg_access(uint64_t pa, uint64_t *v, unsigned wr, unsigned wide)
{
 assert(!wide);
 if (pa==GPU_REG_BASE+0x3c45*4 && wr) gbif=*v;
 else if(pa==GPU_REG_BASE+0x3c46*4 && !wr) *v=mode==7 ? 0 : gbif;
 else if(pa==GPU_REG_BASE+0x43*4 && wr) reset_held=*v;
 else if(pa==GPU_SMMU_BASE+SMMU_S2CR(1) && wr) faulted_sids++;
 else if(pa==GPU_SMMU_BASE+SMMU_S2CR(2) && wr) faulted_sids++;
 else assert(0);
 isolated=1;
 return 0;
}
#define kernel_pmap 0
static ppnum_t pmap_find_phys(int map, uintptr_t va)
{
 (void)map; assert(va>=(uintptr_t)source_buf && va<(uintptr_t)source_buf+FG_SOURCE_BYTES);
 lookups++;
 if(paddr_missing) return 0;
 if(contiguous_pa) return 128+((va-(uintptr_t)source_buf)>>14);
 return 1+3*((va-(uintptr_t)source_buf)>>14); /* deliberately discontiguous 16K pages */
}
static void CleanPoC_DcacheRegion_Force(uint64_t va,uint64_t n)
{
 assert(va>=(uintptr_t)source_buf && va+n<=(uintptr_t)source_buf+FG_SOURCE_BYTES);
 cache_clean=1; clean_bytes+=n;
}
static int g4_tlb(uint32_t *d) { (void)d; return ++tlbs==fail_tlb ? 0xe03 : 0; }
static uint32_t model_stamp[3], stamp_clock=1000;
static unsigned g3_gr(unsigned reg) { return reg==0x806 ? rptr : reg==0x807 ? wptr : reg==0x210 && mode==6 ? 2 : reg==0x980 ? stamp_clock : 0; }
static unsigned g3_read(uint64_t pa) { if(pa>=G3_FENCE_PA+192 && pa<=G3_FENCE_PA+208) return model_stamp[(pa-G3_FENCE_PA-192)/8]; assert(pa==G3_FENCE_PA+64); return mode==8 && ticks<completion ? 0 : fence; }
static void g_wr(uint64_t base, uint32_t off, uint32_t v) { assert(base==G3_FENCE_PA && off>=192 && off<=208 && !v); model_stamp[(off-192)/8]=0; }
#define GPU_GMU_CORE 0x03d6a000ULL
#define FG_GMU_AHB_FENCE_STATUS (GPU_GMU_CORE+0x9313*4)
#define FG_GMU_AHB_FENCE_CLR (GPU_GMU_CORE+0x9314*4)
static unsigned fg_wptr_fence, fg_wptr_stats[4];
static int g3_write(uint64_t pa, uint32_t v) { (void)pa; (void)v; assert(0); return 0; } /* only the fenced doorbell (default off) writes the GMU */
static int g3_faulted(void) { return mode==4 && doorbells; }
static void g3_emit(uint32_t value)
{
 assert(((g3_wptr-rptr)&8191)<8190);
 ring[(g3_wptr++)&8191]=value;
}
static int g3_gw(unsigned reg, unsigned value);
static uint64_t mach_absolute_time(void) { return ++ticks; }
static int fuxi_gpu_ready(void) { return fg_state==1; }
'''
MODEL = r'''
/* Legacy pixel cases also test the split actual submit/poll boundary. */
static int model_copy(unsigned slot,uint64_t gen,uintptr_t src,uint64_t len,
 unsigned p,unsigned sx,unsigned sy,unsigned dx,unsigned dy,unsigned w,unsigned h,struct fg_times *t)
{
 int r=fuxi_gpu_copy(slot,gen,src,len,p,sx,sy,dx,dy,w,h,t);
 if(r!=2) return r;
 assert(fg_busy && fg_scanout_stamps.submitted==fg_submit_time && !fg_scanout_stamps.fence);
 unsigned before=doorbells;
 assert(!fuxi_gpu_copy(slot,gen,src,len,p,sx,sy,dx,dy,w,h,t));
 assert(doorbells==before && !fuxi_gpu_unmap(slot,t));
 for(unsigned i=0;i<200;i++) {
  r=fuxi_gpu_poll(t);
  if(r) { assert(!fg_busy);if(r==1)assert(fg_scanout_stamps.fence>=fg_scanout_stamps.submitted);return r==1 ? 1 : r==-2 ? -1 : 0; }
  ticks+=1000; /* time passes OUTSIDE the display callback */
 }
 assert(0);return 0;
}
static uint32_t translated_pixel(unsigned va)
{
 if(va>=FG_INL_SRC_VA) va=va-FG_INL_SRC_VA+FG_SOURCE_VA; /* the client-root window aliases the present tables */
 assert(va>=FG_SOURCE_VA && va<FG_SOURCE_VA+FG_SOURCE_BYTES);
 unsigned offva=va-FG_SOURCE_VA;
 uint64_t desc=model_l2[((FG_SOURCE_VA>>21)&511)+offva/0x200000];
 uint64_t pte=(desc&3)==1 ? (desc&0xffffff000ULL)+(offva&0x1ff000)+(desc&0xffc)+3 : model_leaves[offva/4096];
 assert((pte&0xffb)==(3|(3u<<6)|(3u<<8)|(1u<<10)));
 uint64_t pa=(pte&0xffffff000ULL)|(va&4095);
 unsigned pn=(pa>>14)-1;
 assert(contiguous_pa || pn%3==0);
 unsigned off=(contiguous_pa ? (pa>>14)-128 : pn/3)*16384+(pa&16383);
 assert(off<FG_SOURCE_BYTES);
 uint32_t got; memcpy(&got,source_buf+off,4); return got;
}
static int g3_gw(unsigned reg,unsigned value)
{
 assert(reg==0x807 && value<8192 && cache_clean);
 doorbells++;
 if(mode==8) completion=ticks+700;
 if(mode==2 || mode==7) { wptr=value; return 1; } /* missing fence/stall */
 if(mode==5) { fg_access_failed=1; return 0; }
 unsigned total=(value-rptr)&8191;
 if(value<rptr) wrap_count++;
 if(total>maxwords) maxwords=total;
 assert(total && total<16*32+128);
 uint32_t regs[0xc000]={0};
 unsigned pos=rptr, left=total, br=0, blits=0, fences=0;
 while(left) {
  unsigned hdr=ring[pos++&8191]; left--;
  if((hdr>>28)==4) {
   unsigned n=hdr&0x7f, r=(hdr>>8)&0x3ffff;
   assert(hdr==g4_pkt4(r,n) && r+n<0xc000 && left>=n);
   for(unsigned i=0;i<n;i++) regs[r+i]=ring[pos++&8191];
   left-=n;
  } else {
   unsigned n=hdr&0x3fff, op=(hdr>>16)&0x7f, a[8]={0};
   assert((hdr>>28)==7 && hdr==g3_pkt7(op,n) && n<=8 && n<=left);
   for(unsigned i=0;i<n;i++) a[i]=ring[pos++&8191];
   left-=n;
   if(op==0x17) { assert(n==1 && a[0]==((1u<<27)|1)); br=1; }
   else if(op==0x2c) {
    assert(br && n==1 && a[0]==3 && regs[G4_SP_SRC_INFO]==0x500430);
    assert(regs[G4_RB_BLIT]==0x10f03000 && regs[G4_GRAS_BLIT]==0x10f03000);
    unsigned sx=regs[0x8401]>>8, ex=regs[0x8402]>>8;
    unsigned dx=regs[0x8405]&65535, bx=regs[0x8406]&65535;
    unsigned h=(regs[0x8406]>>16)+1;
    assert(!regs[0x8403] && regs[0x8404]==(h-1)<<8 && bx>=dx && ex-sx==bx-dx);
    assert(regs[0xb2c1]==((ex+1)|h<<15));
    unsigned sp=(regs[0xb2c4]>>9)*64, dp=regs[0x8c1a]*64;
    assert(sp >= (ex+1)*4 && dp >= (bx+1)*4);
    assert(!((regs[0xb2c2]|regs[0x8c18])&63));
    for(unsigned y=0;y<h;y++) for(unsigned x=0;x<=bx-dx;x++) {
     assert(pending<2400*1080);
     unsigned dst=regs[0x8c18]+y*dp+(dx+x)*4-(regs[0x8c18]>=FG_INL_FB_VA ? FG_INL_FB_VA : G4_FB_VA);
     unsigned buffer=dst/0xa00000u;
     dst-=buffer*0xa00000u;
     assert(buffer<3 && (!fg_scanout_triple_requested || !fg_scanout_back_ready || buffer!=fg_scanout_ready));
     assert(dst<4320*2400 && (!fg_scanout_double_active || buffer!=fg_scanout_front));
     dst+=buffer*4320*2400;
     pdst[pending]=dst; pdata[pending++]=translated_pixel(regs[0xb2c2]+y*sp+(sx+x)*4);
    }
    blits++;
   } else if(op==0x46 && n==1 && a[0]==26) {
    for(unsigned i=0;i<pending;i++) memcpy(panel+pdst[i],&pdata[i],4);
    pending=0;
   } else if(op==0x46 && n==4) {
    assert(!pending && a[0]==(49u|1u<<27) && a[1]==G3_FENCE_VA+64 && !a[2]);
    fence=a[3]; fences++;
   } else if(op==0x3e) { assert(n==3 && a[0]==(0x980|(2u<<18)|(1u<<30)) && a[1]>=G3_FENCE_VA+192 && a[1]<=G3_FENCE_VA+208 && !a[2]); model_stamp[(a[1]-G3_FENCE_VA-192)/8]=(stamp_clock+=100);
   } else assert((op==0x26 && n==0) || (op==0x65 && n==1 && a[0]==12) || (op==0x46 && n==1));
  }
 }
 assert(blits && blits<=16 && fences==1);
 rptr=wptr=value;
 return 1;
}
static void reset(void)
{
 fg_scanout_owned=fg_scanout_writable=fg_scanout_requested=scanoutStops=verifyBad=0;
 fg_scanout_triple_requested=fg_scanout_ready=fg_scanout_copy=0;
 fg_scanout_double_active=fg_scanout_front=fg_scanout_back_ready=fg_scanout_ready_drops=0;
 fg_faults=fg_suspends=fg_reset_held=0;
 fg_state=1; fg_force=fg_seq=fg_access_failed=mode=fail_tlb=tlbs=isolated=doorbells=0;
 g3_wptr=rptr=wptr=8190; fence=cache_clean=pending=gbif=reset_held=faulted_sids=paddr_missing=0;
 fg_busy=fg_magic=0; ticks=lookups=contiguous_pa=0; clean_bytes=0; memset(&times,0,sizeof(times)); memset(fg_generation,0,sizeof(fg_generation)); memset(model_l2,0,sizeof(model_l2)); memset(model_leaves,0,sizeof model_leaves); memset(panel,0x6b,3*4320*2400+64);
}
static void page_fixture(unsigned off,unsigned pitch,unsigned sx,unsigned sy,unsigned w,unsigned h)
{
 memset(expected_pages,0,sizeof expected_pages);
 for(unsigned y=0;y<h;y++) {
  unsigned a=off+(sy+y)*pitch+sx*4,b=a+w*4;
  for(unsigned p=a/4096;p<=(b-1)/4096;p++) expected_pages[p]=1;
 }
}
static void copy_case(unsigned off,unsigned pitch,unsigned sx,unsigned sy,unsigned dx,unsigned dy,unsigned w,unsigned h)
{
 unsigned len=(sy+h)*pitch;
 page_fixture(off,pitch,sx,sy,w,h);
 assert(fuxi_gpu_map(0,1,(uintptr_t)source_buf+off,len,&times)==1);
 unsigned before=doorbells;
 assert(model_copy(0,1,(uintptr_t)source_buf+off,len,pitch,sx,sy,dx,dy,w,h,&times)==1);
 assert(doorbells==before+1);
 assert(fuxi_gpu_unmap(0,&times));
 for(unsigned y=0;y<2400;y++) for(unsigned x=0;x<1080;x++) {
  uint32_t got,expected=0x6b6b6b6b;
  memcpy(&got,panel+y*4320+x*4,4);
  if(y>=dy && y<dy+h && x>=dx && x<dx+w)
   memcpy(&expected,source_buf+off+(sy+y-dy)*pitch+(sx+x-dx)*4,4);
  assert(got==expected);
 }
 for(unsigned i=0;i<8;i++) assert(!model_l2[((FG_SOURCE_VA>>21)&511)+i]);
 for(unsigned i=0;i<64;i++) assert(panel[4320*2400+i]==0x6b);
 checks++;
}
int main(void)
{
 source_buf=aligned_alloc(16384,FG_SOURCE_BYTES); panel=malloc(3*4320*2400+64);
 assert(source_buf && panel);
 for(unsigned i=0;i<FG_SOURCE_BYTES;i++) source_buf[i]=(i*13u+(i>>12)*17u)&255;
 reset(); copy_case(0,4320,0,0,0,0,1080,2400); /* one submission, two strided rects */
 assert(maxwords==110);
 for(unsigned pitch=4324;pitch<=4336;pitch=(pitch==4324 ? 4328 : 4336)) {
  reset();copy_case(0,pitch,0,0,0,0,1080,2400);
  if(pitch==4336) break;
 }
 assert(maxwords==432);
 for(unsigned phase=0;phase<16;phase++) {
  reset(); copy_case(phase*4,5008,17,3,19,7,1057,2311);
 }
 reset(); copy_case(4092,4352,1,0,1079,2399,1,1); /* both page and panel edge */
 /* More than 600 frames with a continuously reused ring, changing phases. */
 reset();
 for(unsigned i=0;i<1000;i++) {
  memset(panel,0x6b,3*4320*2400+64);
  copy_case((i%16)*4,4320,3,2,7,6,65,70);
 }
 /* Contiguous physical backing exercises real 2M blocks and 64K hints. */
 reset(); contiguous_pa=1; copy_case(0,4352,0,0,0,0,1080,2400);
 assert(times.blocks>0 && times.groups>0);
 /* A thousand hot copies: no remapping, lookups, TLB sync or unmap. */
 reset(); assert(fuxi_gpu_map(0,42,(uintptr_t)source_buf,4352*2400,&times));
 unsigned hot_lookups=lookups, hot_tlbs=tlbs;
 /* A client owns the ring: defer only GPU copying; the caller uses CPU. */
 fg_client_busy=1;
 assert(!fuxi_gpu_copy(0,42,(uintptr_t)source_buf,4352*2400,4352,3,2,7,6,65,70,&times));
 assert(!doorbells && !fg_busy && fg_state==1 && fg_abi_present_wait);fg_abi_present_wait=0;
 fg_client_busy=0;
 for(unsigned i=0;i<1000;i++)
  assert(model_copy(0,42,(uintptr_t)source_buf,4352*2400,4352,3,2,7,6,65,70,&times)==1);
 assert(lookups==hot_lookups && tlbs==hot_tlbs && doorbells==1000);
 assert(!model_copy(0,41,(uintptr_t)source_buf,4352*2400,4352,0,0,0,0,1,1,&times));
 assert(!model_copy(0,42,(uintptr_t)source_buf,4096,4320,0,0,0,0,1080,2400,&times));
 assert(!model_copy(0,42,(uintptr_t)source_buf,FG_SOURCE_BYTES+1ULL,4320,0,0,0,0,1,1,&times));
 assert(fuxi_gpu_unmap(0,&times));
 reset(); paddr_missing=1;
 assert(!fuxi_gpu_map(0,1,(uintptr_t)source_buf,4320*10,&times) && fg_state==2 && !doorbells);
 for(unsigned m=1;m<=7;m++) {
  reset(); assert(fuxi_gpu_map(0,1,(uintptr_t)source_buf,4320*10,&times));
  if(m==1) { fail_tlb=2; assert(fuxi_gpu_unmap(0,&times)); assert(fg_state==2); continue; }
  if(m==3) continue;
  mode=m;
  int result=model_copy(0,1,(uintptr_t)source_buf,4320*10,4320,0,0,0,0,10,10,&times);
  assert(result==(m==7 ? -1 : 0) && fg_state==2 && fg_faults==1);
  fg_fail("repeat",0);assert(fg_faults==1);
  assert(fg_suspends==(unsigned)!!isolated);
  assert(ticks<120000); /* six tick deadline + bounded isolation */
  if(doorbells) assert(isolated && gbif==2 && reset_held==1 && faulted_sids==2);
  unsigned before=doorbells;
  assert(model_copy(0,1,(uintptr_t)source_buf,4320*10,4320,0,0,0,0,10,10,&times)==0 && doorbells==before);
 }
 reset(); assert(fuxi_gpu_map(0,1,(uintptr_t)source_buf,4320*10,&times)); fg_force=2;
 assert(!model_copy(0,1,(uintptr_t)source_buf,4320*10,4320,0,0,0,0,10,10,&times) && isolated);
 reset(); assert(fuxi_gpu_map(0,1,(uintptr_t)source_buf,4352*10,&times)); mode=8;
 assert(model_copy(0,1,(uintptr_t)source_buf,4352*10,4352,0,0,0,0,10,10,&times)==1);
 assert(times.sleeps==0 && times.wait>=700 && !isolated);
 /* Real split API: later tick completes even past deadline; no sleep/poll loop. */
 reset(); assert(fuxi_gpu_map(0,1,(uintptr_t)source_buf,4352*10,&times)); mode=8;
 assert(fuxi_gpu_copy(0,1,(uintptr_t)source_buf,4352*10,4352,0,0,0,0,10,10,&times)==2);
 assert(!fuxi_gpu_poll(&times) && !times.sleeps && fg_busy);
 ticks+=200000;
 assert(fuxi_gpu_poll(&times)==1 && !fg_busy && !isolated);
 /* fuxigpuncimport bit 20: SweetSurface pre-cleaned the source, so the locked copy path does not clean again. */
 reset(); assert(fuxi_gpu_map(0,1,(uintptr_t)source_buf,4352*10,&times)); fg_scanout_preclean=1;
 CleanPoC_DcacheRegion_Force((uintptr_t)source_buf,4352*10); { uint64_t cleaned=clean_bytes; /* the caller's pre-clean */
 assert(fuxi_gpu_copy(0,1,(uintptr_t)source_buf,4352*10,4352,0,0,0,0,10,10,&times)==2 && clean_bytes==cleaned); }
 ticks+=200000; assert(fuxi_gpu_poll(&times)==1); fg_scanout_preclean=0;
 /* Coherent write-back mode: the source is mapped AttrIndx 1 inner-shareable and not cleaned; a slot mapped NC before the probe still is. */
 reset(); memset(fg_src_wb,0,sizeof fg_src_wb); fg_abi_coh=1; fg_abi_coh_wb=1; assert(fuxi_gpu_map(0,1,(uintptr_t)source_buf,4352*10,&times) && fg_src_wb[0]);
 { unsigned n=0; for (unsigned i=0;i<11;i++) if (model_leaves[i] && (model_leaves[i]&(1u<<2)) && (model_leaves[i]&(3u<<8))==(3u<<8)) n++; assert(n>=10); }
 { uint64_t cleaned=clean_bytes; cache_clean=1; assert(fuxi_gpu_copy(0,1,(uintptr_t)source_buf,4352*10,4352,0,0,0,0,10,10,&times)==2 && clean_bytes==cleaned && fg_src_n_wb==1); }
 ticks+=200000; assert(fuxi_gpu_poll(&times)==1);
 reset(); fg_abi_coh=0; fg_abi_coh_wb=0; assert(fuxi_gpu_map(0,2,(uintptr_t)source_buf,4352*10,&times) && !fg_src_wb[0]); fg_abi_coh=1; fg_abi_coh_wb=1;
 { uint64_t cleaned=clean_bytes; assert(fuxi_gpu_copy(0,2,(uintptr_t)source_buf,4352*10,4352,0,0,0,0,10,10,&times)==2 && clean_bytes>cleaned && fg_src_n_ncstale==1); }
 ticks+=200000; assert(fuxi_gpu_poll(&times)==1); fg_abi_coh=0; fg_abi_coh_wb=0;
 /* Inline copy (bit 21): the client root is installed and a client job is running; the copy is queued, not refused, with the window VAs and its own fence. */
 reset(); memset(model_mirror,0,sizeof model_mirror); fg_abi_inline=1; fg_inl_ready=1; fg_inl_fbmask=1; fg_root_client=1; inl_usable_model=1; fg_inl_n=0; memset(fg_inl_fb,0,sizeof fg_inl_fb);
 assert(fuxi_gpu_map(0,1,(uintptr_t)source_buf,4352*10,&times) && !memcmp(model_mirror,model_l2,sizeof model_l2));
 fg_client_busy=1; mode=6; /* GPU reports busy for the whole test: only the copy's own fence may retire it */
 inflight_model=2; cache_clean=1; assert(fuxi_gpu_copy(0,1,(uintptr_t)source_buf,4352*10,4352,0,0,0,0,10,10,&times)==2 && fg_busy && fg_inl_busy && fg_inl_n==1 && !fg_abi_present_wait && fg_copy_log.ahead==2 && !fg_copy_log.parked && !pk_flushes); inflight_model=0;
 assert(fuxi_gpu_copy(0,1,(uintptr_t)source_buf,4352*10,4352,0,0,0,0,10,10,&times)==0); /* one present in flight */
 ticks+=200000; assert(fuxi_gpu_poll(&times)==1 && !fg_busy && !fg_inl_busy && fg_inl_lat_n==1 && (fg_copy_log.s2s_us!=0 || fg_copy_log.blit_us!=0)); /* frame log stamps: us or UINT32_MAX without stamps */
 /* Bit 27: a parked client job (counted in flight) is not ahead of the copy and is rung once, after the copy's words. */
 { unsigned w0=g3_wptr; inflight_model=2; fg_pk_n=40; cache_clean=1; assert(fuxi_gpu_copy(0,1,(uintptr_t)source_buf,4352*10,4352,0,0,0,0,10,10,&times)==2 && fg_copy_log.ahead==1 && fg_copy_log.parked==1);
   assert(pk_flushes==1 && pk_flush_why==FG_PK_COPY && !fg_pk_n && ((pk_wptr_at_flush-w0)&8191)>100); inflight_model=0; pk_flushes=0;
   ticks+=200000; assert(fuxi_gpu_poll(&times)==1 && !fg_busy); }
 { uint32_t px; memcpy(&px,panel,4); uint32_t sp; memcpy(&sp,source_buf,4); assert(px==sp); }
 /* No room in the ring, or an unmapped buffer set: classic path, which needs the present root and so refuses while the client runs. */
 g3_wptr=(rptr+8191-100)&8191; cache_clean=1; assert(fuxi_gpu_copy(0,1,(uintptr_t)source_buf,4352*10,4352,0,0,0,0,10,10,&times)==0 && fg_inl_fb[FG_INL_RING]==1 && fg_inl_fb[FG_INL_CLIENT]==1 && inl_present_calls==1);
 g3_wptr=rptr; fg_inl_fbmask=0; fg_scanout_double_requested=1; cache_clean=1; assert(fuxi_gpu_copy(0,1,(uintptr_t)source_buf,4352*10,4352,0,0,0,0,10,10,&times)==0 && fg_inl_fb[FG_INL_BUFFERS]==1);
 fg_scanout_double_requested=0; fg_client_busy=0; mode=0; fg_abi_inline=0; fg_root_client=0; inl_usable_model=0;
 reset();
 // Actual shared fault/reset accounting: saturate, count an asserted
 // containment reset once even if the bus drain is repeated.
 reset();fg_faults=fg_suspends=UINT64_MAX;
 fg_fail("saturated",0);assert(fg_faults==UINT64_MAX);
 assert(fg_isolate() && fg_suspends==UINT64_MAX && irqQuiesces);
 fg_suspends=5;assert(fg_isolate() && fg_suspends==5);
 // New handoff: an unknown DMA drain holds display ownership on both submit
 // and asynchronous retirement errors. No CPU reuse of the target is legal.
 for(unsigned submitFail=0;submitFail<2;submitFail++) {
  reset();assert(fuxi_gpu_map(0,1,(uintptr_t)source_buf,4352*10,&times));
  fg_scanout_owned=fg_scanout_requested=fg_scanout_writable=1;
  mode=7;if(submitFail)fg_force=2;
  assert(model_copy(0,1,(uintptr_t)source_buf,4352*10,4352,0,0,0,0,10,10,&times)==-1);
  assert(scanoutStops==1 && !fg_scanout_writable && fg_state==2);
 }
 reset();assert(fuxi_gpu_map(0,1,(uintptr_t)source_buf,4352*10,&times));
 fg_scanout_owned=fg_scanout_requested=fg_scanout_writable=verifyBad=1;
 assert(!model_copy(0,1,(uintptr_t)source_buf,4352*10,4352,0,0,0,0,10,10,&times));
 assert(isolated && !scanoutStops && fg_state==2); // confirmed drain permits CPU replay
 // Alternating real PM4 destinations always exclude front. Small damage
 // expands to the full 1080x2400 crop, including unchanged distant pixels.
 reset();assert(fuxi_gpu_map(0,1,(uintptr_t)source_buf,4544u*2436,&times));
 fg_scanout_owned=fg_scanout_requested=fg_scanout_writable=fg_scanout_double_active=1;
 for(unsigned frame=0;frame<4;frame++) {
  unsigned front=frame&1,back=front^1;
  fg_scanout_front=front;fg_scanout_back_ready=1;
  unsigned before=fg_scanout_ready_drops;
  // source origin 22,18, plus damage at 100,200.
  assert(model_copy(0,1,(uintptr_t)source_buf,4544u*2436,4544,122,218,100,200,17,3,&times)==1);
  assert(!fg_scanout_back_ready && fg_scanout_ready_drops==before+1);
  assert(fg_scanout_sx==22 && fg_scanout_sy==18 && !fg_scanout_dx && !fg_scanout_dy);
  assert(fg_scanout_w==1080 && fg_scanout_h==2400);
  for(unsigned y=0;y<2400;y++) for(unsigned x=0;x<1080;x++) {
   uint32_t got,expected;memcpy(&got,panel+back*4320*2400+y*4320+x*4,4);
   memcpy(&expected,source_buf+(y+18)*4544+(x+22)*4,4);assert(got==expected);
   if(!frame)assert(panel[front*4320*2400+y*4320+x*4]==0x6b);
  }
  source_buf[18*4544+22*4]^=0xff; // outside the next damage rect
 }
 fg_scanout_triple_requested=1;
 for(unsigned front=0;front<3;front++)for(unsigned ready=0;ready<3;ready++)if(front!=ready) {
  fg_scanout_front=front;fg_scanout_ready=ready;fg_scanout_back_ready=1;
  unsigned before=fg_scanout_ready_drops,freeBuffer=3-front-ready;
  memset(panel,0x6b,3*4320*2400+64);
  assert(model_copy(0,1,(uintptr_t)source_buf,4544u*2436,4544,122,218,100,200,17,3,&times)==1);
  assert(fg_scanout_copy==freeBuffer && fg_scanout_back_ready && fg_scanout_ready==ready && fg_scanout_ready_drops==before);
  for(unsigned y=0;y<2400;y++)for(unsigned x=0;x<1080;x++) {
   uint32_t got,expected;memcpy(&got,panel+freeBuffer*4320*2400+y*4320+x*4,4);
   memcpy(&expected,source_buf+(y+18)*4544+(x+22)*4,4);assert(got==expected);
   assert(panel[front*4320*2400+y*4320+x*4]==0x6b && panel[ready*4320*2400+y*4320+x*4]==0x6b);
  }
 }
 // A partial source that cannot supply a complete panel must fall back
 // before touching the ready buffer, cache or doorbell.
 unsigned before=doorbells,drops=fg_scanout_ready_drops;fg_scanout_back_ready=1;
 assert(!fuxi_gpu_copy(0,1,(uintptr_t)source_buf,4544u*2436,4544,0,0,10,10,17,3,&times));
 assert(!fuxi_gpu_copy(0,1,(uintptr_t)source_buf,4544u*100,4544,22,18,0,0,17,3,&times));
 assert(doorbells==before && fg_scanout_ready_drops==drops && fg_scanout_back_ready);
 puts("PASS: actual PM4 double/triple targets, full crops from damage, front/ready exclusion, submit preserves triple ready, incomplete-source refusal");
 /* Independent windows and generations; unmapping one preserves the others. */
 reset();
 for(unsigned slot=0;slot<4;slot++) {
  assert(fuxi_gpu_map(slot,slot+1,(uintptr_t)source_buf,4352*2400,&times));
  assert(!fuxi_gpu_map(slot,slot+9,(uintptr_t)source_buf,4352*2400,&times));
 }
 assert(fuxi_gpu_unmap(1,&times));
 assert(!fg_generation[1] && fg_generation[0]==1 && fg_generation[2]==3 && fg_generation[3]==4);
 for(unsigned slot=0;slot<4;slot++) for(unsigned i=0;i<8;i++)
  assert(!!model_l2[((FG_SOURCE_VA>>21)&511)+slot*8+i]==(slot!=1));
 /* Only cache lines intersecting the selected rows may be cleaned. */
 reset(); fg_clean((uintptr_t)source_buf,4352,0,0,1080,2400);
 assert(clean_bytes==4352ULL*2400);
 clean_bytes=0; fg_clean((uintptr_t)source_buf,4352,17,3,10,10);
 assert(clean_bytes==640);
 printf("PASS: %u pixel-exact copies, cropped phases, noncontiguous 16K/4K mappings, full panel, hot cache, blocks/hints, exact clean ranges, one submit/frame, %u wraps, async busy/deadline/quarantine\n",checks,wrap_count);
 free(source_buf);free(panel);return 0;
}
'''


def main():
    subprocess.run(['python3', str(ROOT / 'tools/sync-fuxi-adreno-shared.py'), '--check'], check=True)
    subprocess.run(['python3', str(ROOT / 'tools/test-fuxi-adreno-mapping.py')], check=True)
    subprocess.run(['python3', str(ROOT / 'tools/test-fuxi-gpu-clock.py')], check=True)
    subprocess.run(['python3', str(ROOT / 'tools/test-fuxi-scm.py')], check=True)
    subprocess.run(['python3', str(ROOT / 'tools/test-fuxi-adreno-cache.py')], check=True)
    probe = sync.added_file('gpu.c')
    adapter = sync.added_file('fuxi_adreno.c')
    for n in ('fuxi_gpu_copy', 'fuxi_gpu_poll'):
        assert not any(x in function(adapter, n) for x in ('thread_block', 'assert_wait', 'fg_sleep', 'delay('))
    windows = [(int(a, 16), int(b, 16)) for a, b in re.findall(r'\{(0x[0-9a-f]+),(0x[0-9a-f]+),0\}', adapter)]
    for pa, size in ((0x100000, 0x80000), (0x3d00000, 0x40000), (0x3d50000, 0x40000),
                     (0x3d90000, 0x10000), (0x3da0000, 0x40000), (0xb280000, 0x8000),
                     (0x81c60000, 0x20000), (0x9b098000, 0x4000), (0xae04000, 0x4000)):
        assert any(a <= pa and pa + size <= a + n for a, n in windows), hex(pa)
    for i, (a, n) in enumerate(windows):
        assert not (a & 16383) and not (n & 16383)
        for b, m in windows[i+1:]:
            assert a + n <= b or b + m <= a, 'overlapping platform mappings'
    functions = ''.join(function(probe, n) for n in ('g3_parity', 'g3_pkt7', 'g4_pkt4', 'g4_reg', 'g4_event', 'g4_wfi', 'g4_setup', 'g4_end'))
    functions += function((ROOT / 'xnu/fuxi-adreno/fuxi_gpu_scanout.inc').read_text(), 'fg_scanout_target')
    functions += ''.join(function(adapter, n) for n in ('fg_fail', 'fg_isolate', 'fuxi_gpu_unmap', 'fuxi_gpu_map', 'fg_rect', 'fg_clean', 'fg_ring_doorbell', 'fg_cp_stamp', 'fg_cp_retire', 'fuxi_gpu_poll', 'fuxi_gpu_copy'))
    functions = re.sub(r'__asm__ volatile\([^;]*\);', ';', functions)
    constants = '\n'.join(line for line in sync.added_file('gpu-a740.h').splitlines() if line.startswith('#define G4_'))
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / 'test.c'
        path.write_text(PREFIX + constants + '\n' + functions + MODEL)
        exe = Path(d) / 'test'
        subprocess.run(['clang', '-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror', '-fsanitize=undefined,address', str(path), '-o', str(exe)], check=True)
        subprocess.run([str(exe)], check=True)


if __name__ == '__main__':
    main()
