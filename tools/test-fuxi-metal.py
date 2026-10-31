#!/usr/bin/env python3
"""Offline NativeAdreno ABI/framing and actual C backend tests (ASan/UBSan).

Models page tables, register observations and scheduler callbacks. Does not
establish real CP/BR/BV execution, SMMU switching or DMA/cache coherency.
"""
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
HEADERS = ROOT/'xnu/fuxi-adreno'
WIRE = r'''
#include <assert.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "fuxi_abi_wire.h"
#define SZ(t,n) static_assert(sizeof(t)==n,#t)
SZ(NativeAdrenoAbiBoAllocReply,8); SZ(NativeAdrenoAbiBoMapReply,8);
SZ(NativeAdrenoAbiBoFree,8); SZ(NativeAdrenoAbiSubmitReply,8);
static_assert(offsetof(NativeAdrenoAbiSubmit,buffers)==32,"buffers offset");
static_assert(offsetof(NativeAdrenoAbiDeviceInfo,flags)==56,"flags offset");
static unsigned bits(uint32_t x) { return __builtin_popcount(x); }
static unsigned packet(const uint32_t *w,unsigned n,unsigned *op,unsigned *len) {
 unsigned type=w[0]>>28;
 assert(type==4 || type==7);
 *len=type==7 ? w[0]&0x3fff : w[0]&0x7f;
 *op=type==7 ? (w[0]>>16)&0x7f : (w[0]>>8)&0x3ffff;
 assert((bits(*len)+((w[0]>>(type==7?15:7))&1))%2==1);
 assert((bits(*op)+((w[0]>>(type==7?23:27))&1))%2==1);
 assert(*len<n); return type;
}
int main() {
 NativeAdrenoAbiParams query={};NativeAdrenoAbiDeviceInfo info={};
 assert(!deviceQuery(NativeAdrenoMethodQuery,nullptr,&query));
 assert(query.abi_version==NativeAdrenoAbiVersion && query.input_bits==31 && query.output_bits==36 &&
     query.flags==(NativeAdrenoParamsHostImport|NativeAdrenoParamsImportGpuOnly) && query.max_bo_bytes==(1u<<28));
 assert(!deviceQuery(NativeAdrenoMethodGetDeviceInfo,nullptr,&info));
 /* Decode the actual page-granular 2b readback, restoring inclusive end. */
 const uint32_t min=0x1000000,max=0x12ff000;
 assert((max|0xfff)-min+1==0x300000);
 assert(info.chip_id==0x43050a01 && info.gmem_base==min &&
     info.gmem_bytes==(max|0xfff)-min+1 && info.gmem_bytes==3u*1024*1024);
 /* fuxigpuncimport bit 29: the flag follows the backend verdict; an inconclusive self-test is asked twice more, 2 ms apart, then reported off. */
 assert(info.flags==NativeAdrenoDeviceClientIova && ccCalls==1 && !sleeps);
 static_assert(NativeAdrenoAllocContiguous==1 && NativeAdrenoAllocCachedCoherent==2,"alloc bits");
 const int seqs[4][3]={{1,1,1},{-1,-1,-1},{-1,1,1},{0,0,0}};
 for(unsigned c=0;c<4;c++) {
  memcpy(ccSeq,seqs[c],sizeof(ccSeq));ccCalls=sleeps=0;info={};
  assert(!deviceQuery(NativeAdrenoMethodGetDeviceInfo,nullptr,&info));
  assert(info.flags==(NativeAdrenoDeviceClientIova|((c==0||c==2)?NativeAdrenoDeviceCachedCoherent:0u)));
  assert(ccCalls==(c==1?3u:c==2?2u:1u) && sleeps==ccCalls-1);
 }
 const unsigned is[]={0,0,16,32,8,3104,16,0,0,80,80,8,8,8,16,0,16};
 const unsigned os[]={0,24,8,8,0,8,0,8,16,0,80,8,0,16,0,64,8};
 for(unsigned s=1;s<=16;s++) {assert(fg_abi_input_size(s)==is[s]);assert(fg_abi_output_size(s)==os[s]);}
 /* The 16-IB record (416 bytes) of an older Turnip is still a valid submit; nothing else is. */
 static_assert(NativeAdrenoAbiMaxBuffers==128 && sizeof(NativeAdrenoAbiSubmit)==3104,"128 IBs");
 assert(fg_abi_input_ok(5,3104) && fg_abi_input_ok(5,416) && !fg_abi_input_ok(5,417) && !fg_abi_input_ok(5,32) && !fg_abi_input_ok(5,0));
 assert(!fg_abi_input_ok(4,416) && fg_abi_input_ok(4,8));
 for(unsigned m=1;m<=16;m++) if(m!=5) assert(fg_abi_input_ok(m,is[m]) && !fg_abi_input_ok(m,is[m]+1));
 assert(fg_abi_submit_capacity(3104)==128 && fg_abi_submit_capacity(416)==16 && !fg_abi_submit_capacity(0) && !fg_abi_submit_capacity(31) && !fg_abi_submit_capacity(32));
 assert(FG_ABI_FRAME_WORDS==581 && FG_ABI_SUBMIT_V1_BYTES==32+24*16);
 assert(!fg_abi_range(0x1ffff000,4096)); assert(!fg_abi_range(0x80000000,4096));
 assert(fg_abi_range(0x7ffff000,4096)); assert(!fg_abi_range(0x7ffff000,8192));
 assert(!fg_abi_range(UINT64_MAX-4095,8192)); assert(!fg_abi_range(0x20000000,0));
 assert(!fg_abi_range(0x20000001,4096)); assert(!fg_abi_range(0x20000000,4095));
 for(unsigned align=0;align<32;align++)
  for(unsigned contig=0;contig<4;contig++)
   assert(fg_abi_alloc_valid(4096,align,contig)==(align>=12 && align<=24 && contig<=3)); /* bit1 cached-coherent, gated by the client */
 assert(!fg_abi_alloc_valid(0,12,0)); assert(!fg_abi_alloc_valid(4097,12,0));
 assert(fg_abi_alloc_valid(1u<<28,24,1)); assert(!fg_abi_alloc_valid((1u<<28)+4096,12,0));
 for(unsigned perm=0;perm<8;perm++) for(unsigned wb=0;wb<3;wb++) for(unsigned sh=0;sh<5;sh++)
  assert(fg_abi_map_valid(0x20000000,4096,perm,wb,sh,0)==((perm==1||perm==3)&&wb<=1&&(sh==2||sh==3)));
 assert(!fg_abi_map_valid(0x20000000,4096,3,0,2,1));
 assert(fg_abi_ib_valid(0x20000000,4096,0x20000004,4092));
 assert(!fg_abi_ib_valid(0x20000000,4096,0x20000004,4096));
 assert(!fg_abi_ib_valid(0x20000000,4096,0x20000000,0));
 assert(!fg_abi_ib_valid(0x20000000,4096,0x20000001,4));
 assert(!fg_abi_ib_valid(0x20000000,4096,UINT64_MAX-3,8));
 uint64_t va[128],bytes[128]; uint32_t w[FG_ABI_FRAME_WORDS];
 for(unsigned i=0;i<128;i++) {va[i]=0x20000000+i*4096;bytes[i]=4096;}
 for(unsigned reset=0;reset<2;reset++) for(unsigned count=1;count<=128;count++) {
  unsigned n=fg_abi_frame(w,va,bytes,count,0x10029080,13,14,reset);
  assert(n==11+26*reset+count*4+31+1 && n<=FG_ABI_FRAME_WORDS && (count<128 || reset==0 || n==FG_ABI_FRAME_WORDS));
  unsigned at=0,op=0,len=0,ibs=0,bv=0,br=0,sram=0,resets=0,ifpc=0,thread=0,waitBv=0;
  /* Independent decoder asserts ordering and address/value pairs. */
  while(at<n) {
   unsigned type=packet(w+at,n-at,&op,&len);
   const uint32_t *p=w+at+1;
   if(type==7 && op==0x17) thread=p[0]&3;
   if(type==7 && op==0x1f) {assert(p[0]==15 && thread==3);resets++;}
   if(type==7 && op==0x3c) {assert(len==6 && p[0]==3 && p[1]==0x50f && p[3]==1 && p[4]==1);sram++;}
   if(type==7 && op==0x65) {assert(p[0]==(ifpc?0x100u:0x101u));ifpc++;}
   if(type==7 && op==0x3f) {assert(ifpc==1 && thread==3 && sram==reset && resets==reset);assert(len==3 && p[0]==va[ibs] && !p[1] && p[2]==1024);ibs++;}
   if(type==7 && op==0x46 && len==4) {
    assert(p[3]==14 && !p[2]);
    if(thread==2) {assert(p[0]==0x08000031 && p[1]==0x1002908c);bv++;}
    else {assert(thread==1 && bv==1 && waitBv==1 && p[0]==0x88000031 && p[1]==0x10029084);br++;}
   }
   if(type==7 && op==0x14) {
    assert(thread==1 && len==4 && !p[0] && !p[2]);
    if(p[1]==0x1002908c) {assert(bv==1 && p[3]==14);waitBv++;}
    else {assert(reset && p[1]==0x10029084 && p[3]==13 && !ibs);}
   }
   at+=len+1;
  }
  assert(at==n && ibs==count && bv==1 && br==1 && waitBv==1 && ifpc==2 && thread==3);
  assert(w[n-1]==fg_abi_pkt7(0x26,0));
 }
 assert(!fg_abi_frame(w,va,bytes,0,0x10029080,0,1,1));
 assert(!fg_abi_frame(w,va,bytes,129,0x10029080,0,1,1));
 assert(!fg_abi_frame(w,va,bytes,1,0x10029080,0,0,1));
 bytes[0]=0; assert(!fg_abi_frame(w,va,bytes,1,0x10029080,0,1,1));
 puts("PASS: all 16 wire layouts/selectors, 128-IB and legacy 416-byte submit sizes, bounds/overflow/flags, independent PM4 decode of 1..128 IB frames, reset/SRAM/IFPC/BV/BR/IB framing");
}
'''
BACKEND_PREFIX = r'''
static unsigned fg_scanout_reserved,fg_scanout_owned,scanoutStops;
static unsigned fg_scanout_period_us=8333;
static unsigned dcvsKicks;static void fg_dcvs_kick(void) {dcvsKicks++;}
static int fg_scanout_stop(const char *reason) {(void)reason;scanoutStops++;return -1;}
#include <assert.h>
#include <stdint.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <stdarg.h>
#include "fuxi_abi_wire.h"
#define FG_GUARD
#define G_MEM 0x9fe000000ULL
#define G_MEM_SIZE 0x2000000
#define G3_PT (G_MEM+0x1200000)
#define G3_SQE_PA (G_MEM+0x1300000)
#define G3_FENCE_PA (G_MEM+0x1329000)
#define G3_SQE_VA 0x10000000u
#define G3_FENCE_VA 0x10029000u
#define G4_DST_PA (G_MEM+0x1501000)
#define G4_BYTES 0x40000
#define G5_META_PA (G_MEM+0x1600000)
#define G5_META_BYTES 0x2000
#define FG_HANDOFF (G_MEM+0x1800000)
#define FG_FW_BASE (FG_HANDOFF+0x4000)
#define FG_FW_SLOT 0x100000
#define G4_RB_NC 0x8e08
#define G4_TPL1_NC 0xb604
#define G4_SP_NC 0xae02
#define G4_GRAS_NC 0x8602
#define G3_APERTURE_HOST 0xa00
#define SMMU_ID2 0x28
#define PAGE_SHIFT 14
#define PAGE_MASK 16383u
#define NSEC_PER_MSEC 1000000
#define THREAD_CALL_PRIORITY_KERNEL 1
#define THREAD_CALL_OPTIONS_ONCE 1
#define kernel_pmap 0
static uint8_t scratch[G_MEM_SIZE] __attribute__((aligned(16384)));
#define G_PTR(pa) ((uintptr_t)(scratch+((pa)-G_MEM)))
#define G_PTR_RANGE(pa, bytes) G_PTR(pa)
typedef unsigned ppnum_t;
typedef void *thread_call_param_t;
typedef struct {void (*fn)(void *,void *);} thread_call_data_t;
static uint64_t ticks;
static uint64_t counter(const char *name) {return strcmp(name,"cntfrq_el0")?ticks:1000000;}
static uint64_t mach_absolute_time(void) {return ticks;}
#define rsr(r) counter(r)
static unsigned fg_abi_present_wait;
#define G4_FB_PA 0xb8000000ULL
#define G4_FB_MAP 10371072u
#define FG_CACHE_SLOTS 4u
#define FG_SOURCE_VA 0x50000000u
#define FG_SOURCE_L3 8u
#define FG_INL_SRC_VA 0x90000000u
#define FG_INL_FB_VA 0xa0000000u
static unsigned fg_abi_inline,fg_inl_ready,fg_root_client,fg_inl_busy,fg_inl_fbmask,fg_scanout_double_requested,fg_scanout_triple_requested;
static uint64_t presentL2[512];
static volatile uint64_t *fg_l2(void) {return presentL2;}
static unsigned fg_access_failed,fg_busy,fg_client_busy,g3_wptr,g3_bank=2,g3_asid=3;
enum { FG_PK_COPY, FG_PK_IDLE, FG_PK_CAP, FG_PK_SUBMIT, FG_PK_NOTDUE, FG_PK_OTHER, FG_PK_REASONS }; /* as fuxi_adreno.c */
static unsigned fg_pk_n;
static int fg_pk_flush(unsigned why);
static unsigned cacheClean,cacheInvalidate,ncimportArg;
static int PE_parse_boot_argn(const char *name,void *value,size_t bytes) {
 assert(!strcmp(name,"fuxigpuncimport") && bytes==sizeof(unsigned));
 memcpy(value,&ncimportArg,bytes);return ncimportArg!=0;
}
static void host_cache(uintptr_t p,int complete) { assert(!(p&63)); if(complete) cacheInvalidate++; else cacheClean++; }
static uint64_t fg_faults,fg_suspends;
static unsigned state=1,drains,drainGood=1,fault,tlbs,failTlb,doorbells,enqueued;
static uint32_t regs[0x10000],sregs[0x1000],ring[8192];
static unsigned highReads,rollover;
static unsigned readFault,sreadFault,writeFail,writeCount,logLines,dumpLines,hangLines;
static char lastLog[512],hangLog[16][256],stallLog[40][512];static unsigned stallLines;
static int trace(const char *fmt,...) {
 va_list ap;va_start(ap,fmt);vsnprintf(lastLog,sizeof(lastLog),fmt,ap);va_end(ap);
 logLines++;if(strstr(lastLog,"init regs"))dumpLines++;if(strstr(lastLog,"FUXI GPU HANG:")){assert(hangLines<16);strcpy(hangLog[hangLines++],lastLog);}
 if(strstr(lastLog,"FUXI GPU STALL:")){assert(stallLines<40);strcpy(stallLog[stallLines++],lastLog);}return 0;
}
#define printf trace
static unsigned g_cb(unsigned bank) {return bank*0x100;}
static uint32_t g3_gr(unsigned reg) {assert(reg<0x10000);if(reg==readFault)fg_access_failed=1;if(reg==0x981) {
  highReads++;
  if(rollover && (rollover==2 || highReads==1))return highReads;
 }
 return regs[reg];}
static uint32_t g3_sr(unsigned reg) {if(sreadFault)fg_access_failed=1;return sregs[reg/4];}
static int g3_sw(unsigned reg,uint32_t value) {assert(!fg_busy);sregs[reg/4]=value;return 1;}
static void g_zero(uint64_t pa,unsigned bytes) {assert(pa>=G_MEM && pa+bytes<=G_MEM+G_MEM_SIZE);memset((void *)G_PTR(pa),0,bytes);}
#define GPU_GMU_CORE 0x03d6a000ULL
static uint32_t gmuRegs[0x10000];
static unsigned fenceDrops,fenceStuck,gmuReads,gmuWrites;
static uint32_t g3_read(uint64_t pa) {
 if(pa>=GPU_GMU_CORE && pa<GPU_GMU_CORE+0x40000) {gmuReads++;return gmuRegs[(pa-GPU_GMU_CORE)/4];}
 return *(uint32_t *)G_PTR(pa);
}
static int g3_write(uint64_t pa,uint32_t v) {
 assert(pa>=GPU_GMU_CORE && pa<GPU_GMU_CORE+0x40000);gmuWrites++;
 if(pa==GPU_GMU_CORE+0x9314*4) {assert(v==7);gmuRegs[0x9313]=fenceStuck?1:0;return 1;}
 assert(0);return 0;
}
#define IBSRC 0x100000UL /* the pmap model maps any low address; reads of it are served from ibMemory */
static uint8_t ibMemory[16384];
static unsigned fuxi_gpu_access(uintptr_t va,uint64_t *v,unsigned wr,unsigned wide) {
 assert(!wr && !wide);
 if(va<IBSRC || va+4>IBSRC+sizeof(ibMemory)) return 1;
 uint32_t x;memcpy(&x,ibMemory+(va-IBSRC),4);*v=x;return 0;
}
static void g_wr(uint64_t pa,unsigned off,uint32_t value) {*(uint32_t *)G_PTR(pa+off)=value;}
static int g3_faulted(void) {return fault;}
static int g4_tlb(uint32_t *d) {(void)d;tlbs++;return failTlb;}
#define GPU_SMMU_BASE 0
static unsigned tlbiWrites,failTlbi,cohMode,delays;static uint64_t tlbiVals[1024];
static unsigned fg_access(uint64_t pa,uint64_t *v,unsigned wr,unsigned wide) {
 assert(wr && wide && !(pa&7) && pa==g_cb(g3_bank)+0x620 && tlbiWrites<1024);
 if(failTlbi) return fg_access_failed=1;
 tlbiVals[tlbiWrites++]=*v;return 0;
}
static void gpu_model(void);
static void delay(unsigned n) {(void)n;delays++;if(fg_client_busy && cohMode) gpu_model();}
static int g3_gw(unsigned reg,uint32_t value) {if(++writeCount==writeFail) {fg_access_failed=1;return 0;} regs[reg]=value; if(reg==0x807) {doorbells++;if(fenceDrops) {fenceDrops--;gmuRegs[0x9313]=1;} else if(fenceStuck) gmuRegs[0x9313]=1;} return 1;}
//FENCED_DOORBELL//
static int fg_isolate(void) {assert(hangLines==16);drains++;if(fg_suspends!=UINT64_MAX)fg_suspends++;return drainGood;}
static void fg_fail(const char *why,unsigned code) {(void)why;(void)code;if(state==1 && fg_faults!=UINT64_MAX)fg_faults++;state=2;}
static int fuxi_gpu_ready(void) {return state==1;}
static void g3_emit(uint32_t v) {ring[g3_wptr++&8191]=v;}
static unsigned pmapWalks;
static ppnum_t pmap_find_phys(unsigned map,uintptr_t va) {(void)map; pmapWalks++; return (unsigned)(0x200000+((va>>14)&0xfff)*3);}
static unsigned char modTab[4096];static unsigned pokes,pokeSets=1,invalidPages;
#define MODIDX(p) (((p)-0x200000)/3)
#define FG_ABI_DTY_POKE(p) do{pokes++;if(pokeSets)modTab[(((uintptr_t)(p))>>14)&0xfff]=1;}while(0)
static int pmap_valid_page(ppnum_t p) {(void)p;return !invalidPages;}
static int pmap_is_modified(ppnum_t p) {return modTab[MODIDX(p)];}
static void pmap_clear_modify(ppnum_t p) {modTab[MODIDX(p)]=0;}
static void clock_interval_to_deadline(unsigned n,unsigned scale,uint64_t *d) {*d=ticks+(uint64_t)n*scale/1000;}
static void thread_call_setup_with_options(thread_call_data_t *c,void (*fn)(void *,void *),void *arg,unsigned pri,unsigned opt) {(void)arg;(void)pri;(void)opt;c->fn=fn;}
static int thread_call_enter_delayed(thread_call_data_t *c,uint64_t d) {(void)c;(void)d;enqueued++;return 0;}
'''
BACKEND_TEST = r'''
static void seed(void) {
 memset(scratch,0x5a,sizeof(scratch)); memset(regs,0,sizeof(regs));memset(sregs,0,sizeof(sregs));
 regs[0xe07]=regs[0xe09]=0xfffff000;regs[0xe08]=regs[0xe0a]=0x1ffff;regs[0x534]=1;regs[0xe0b]=0x1000000;regs[0xe0d]=0x12ff000;regs[0xe01]=3u<<21;
 sregs[SMMU_ID2/4]=0x5511;
 state=drainGood=1;fg_busy=fg_client_busy=fg_access_failed=fg_abi_present_wait=0;g3_wptr=0;
 fg_scanout_owned=scanoutStops=0;
 fg_abi_live=fg_abi_seq=fg_abi_completed=fg_abi_poisoned=fg_abi_timed_out=0;fg_abi_hang_dumped=0;
 fg_faults=fg_suspends=highReads=rollover=0;
 ticks=tlbs=failTlb=doorbells=enqueued=drains=fault=cacheClean=cacheInvalidate=0;tlbiWrites=failTlbi=cohMode=delays=0;
 memset(fg_abi_imports,0,sizeof(fg_abi_imports));fg_abi_host_handoff=fg_abi_ncimport=ncimportArg=0;fg_abi_coh=fg_abi_coh_tries=0;memset(fg_abi_coh_page,0,sizeof(fg_abi_coh_page));
 fg_abi_cleaned=fg_abi_skipped=fg_abi_invalidated=0;
 readFault=sreadFault=writeFail=writeCount=logLines=hangLines=0;
 memset(gmuRegs,0,sizeof(gmuRegs));fenceDrops=fenceStuck=gmuReads=gmuWrites=stallLines=0;
 fg_wptr_fence=fg_abi_unstick=fg_abi_stage=fg_abi_stall_lines=0;memset(fg_wptr_stats,0,sizeof(fg_wptr_stats));
 memset(fg_abi_ibs,0,sizeof(fg_abi_ibs));memset(fg_abi_stall_stats,0,sizeof(fg_abi_stall_stats));
 memset(fg_abi_maps,0,sizeof(fg_abi_maps));fg_abi_map_seq=fg_abi_submit_map_seq=0;
}
static unsigned wait_us(void){unsigned k;return fuxi_gpu_abi_submit_wait_us(&k);}
static unsigned wakes;static void count_wake(void) {wakes++;}
static void reset(void) {
 seed();
 assert(fg_abi_init()); assert(fuxi_gpu_abi_available());
}
static void complete(void) {
 g_wr(G3_FENCE_PA,FG_ABI_FENCE_OFF+4,fg_abi_seq);
 regs[0x806]=regs[0x807]=g3_wptr;regs[0x210]=0;
}
/* Models the CP executing the self-test IB. cohMode 1: coherent (GPU sees the CPU's cached values and the CPU sees GPU writes);
 * 2: non-coherent (GPU reads stale DRAM, its writes never reach the CPU's cached lines); 3: GPU never retires; 4: fault. */
static void gpu_model(void) {
 if(cohMode==3) {ticks+=60000;return;}
 if(cohMode==4) {fault=1;regs[0x928]=1;return;}
 const uint64_t base=FG_ABI_VA_START+FG_ABI_VA_SIZE-0x10000;
 unsigned coh=cohMode==1 || (cohMode==6 && (*fg_abi_leaf(base+0x4000)&(1u<<2)) && ((*fg_abi_leaf(base+0x4000)>>8)&3)==3);
 uint32_t *ib=(uint32_t *)(scratch+(FG_ABI_COH_IB-G_MEM));
 uint32_t *res=(uint32_t *)(scratch+(FG_ABI_COH_RES-G_MEM));
 uint32_t *page=(uint32_t *)fg_abi_coh_page;
 unsigned at=0,m2m=0,wr=0;
 while(ib[at]) {
  unsigned op=(ib[at]>>16)&0x7f,n=ib[at]&0x3fff;
  if(op==0x73) {assert(n==5 && !ib[at+1]);uint64_t dst=ib[at+2],src=ib[at+4];
   assert(dst>=base+0x1000 && dst<base+0x1040 && src>=base+0x4000 && src<base+0x4200 && !(src&3));
   uint32_t v=coh || (cohMode==5 && dst!=base+0x1000+12) ? page[(src-base-0x4000)/4] : 0; res[(dst-base-0x1000)/4]=v;m2m++;}
  else if(op==0x3d) {assert(n==3);uint64_t dst=ib[at+1];assert(dst>=base+0x4200 && dst<base+0x4400 && !ib[at+2]);
   if(coh || cohMode==5) page[(dst-base-0x4000)/4]=ib[at+3];wr++;}
  else assert(op==0x12 || op==0x26);
  at+=1+n;
 }
 assert(m2m==8 && wr==8);
 complete();
}
int main(void) {
 assert(!fg_abi_live_for_scanout());
 /* Every mismatch, read failure and MMIO write failure uses the actual init.
  * The first failure still captures all registers; later attempts never dump.
  * Refusal must precede table construction and ABI publication. */
 const unsigned verified[]={0xe07,0xe08,0xe09,0xe0a,0xe0b,0xe0c,0xe0d,0xe0e,0xe01,0x534};
 const char *names[]={"UCHE_WRITE_THRU_BASE_LO","UCHE_WRITE_THRU_BASE_HI",
  "UCHE_TRAP_BASE_LO","UCHE_TRAP_BASE_HI","UCHE_GMEM_RANGE_MIN_LO",
  "UCHE_GMEM_RANGE_MIN_HI","UCHE_GMEM_RANGE_MAX_LO","UCHE_GMEM_RANGE_MAX_HI",
  "UCHE_MODE_CNTL_HBB","RBBM_NC_MODE_CNTL"};
 for(unsigned i=0;i<10;i++) {
  seed();regs[verified[i]]^=i==8 ? 1u<<21 : i<8 && !(i&1) ? 1u<<12 : 1;
  assert(!fg_abi_init() && !fg_abi_live && !writeCount);
  assert(strstr(lastLog,"init check") && strstr(lastLog,names[i]) && strstr(lastLog,"got 0x") && strstr(lastLog,"want 0x"));
  assert(dumpLines==11 && logLines==(i ? 1u : 12u));
  assert(*(uint64_t *)G_PTR(FG_ABI_PT)==0x5a5a5a5a5a5a5a5aULL);
 }
 seed();sregs[SMMU_ID2/4]=0x5501;
 assert(!fg_abi_init() && strstr(lastLog,"SMMU_ID2_OAS_min got 0x0 want 0x1"));
 for(unsigned i=0;i<10;i++) {
  seed();readFault=verified[i];
  assert(!fg_abi_init() && !writeCount && !fg_abi_live);
  assert(strstr(lastLog,"register_access got 0x1 want 0x0"));
 }
 seed();sreadFault=1;
 assert(!fg_abi_init() && !writeCount && !fg_abi_live);
 assert(strstr(lastLog,"register_access got 0x1 want 0x0") && logLines==1);
 const char *writes[]={"G4_RB_NC","G4_TPL1_NC","G4_SP_NC","G3_APERTURE_HOST_BR",
  "G4_GRAS_NC_BR","G3_APERTURE_HOST_BV","G4_GRAS_NC_BV","G3_APERTURE_HOST_DEFAULT"};
 for(unsigned i=0;i<8;i++) {
  seed();writeFail=i+1;
  assert(!fg_abi_init() && !fg_abi_live && writeCount==i+1);
  assert(strstr(lastLog,"init write") && strstr(lastLog,writes[i]) && strstr(lastLog,"failed"));
  assert(logLines==1 && dumpLines==11);
  assert(*(uint64_t *)G_PTR(FG_ABI_PT)==0x5a5a5a5a5a5a5a5aULL);
 }
 puts("PASS: actual init all register mismatches/read faults/eight write failures, no publication or tables, one-time bounded dump");
 /* Only address low words ignore offset bits; page-address corruption and
  * all high-word/control mismatches above still refuse publication. */
 for(unsigned i=0;i<8;i+=2) for(unsigned bit=0;bit<12;bit++) {
  seed();regs[verified[i]]^=1u<<bit;
  assert(fg_abi_init() && fuxi_gpu_abi_available() && writeCount==8 && !logLines);
 }
 seed();regs[0xe0d]=0x12fffff;assert(fg_abi_init()); /* unrounded model also valid */
 puts("PASS: observed A740 page-granular addresses, every ignored offset bit, inclusive GMEM end and exact high/control checks");
 reset();
 uint64_t timestamp=99,faults=99,suspends=99;
 assert(!fuxi_gpu_abi_status(&faults,&suspends) && !faults && !suspends);
 assert(fuxi_gpu_abi_timestamp(NULL)==-2);
 regs[0x981]=0x123;regs[0x980]=0xfedcba98;
 assert(!fuxi_gpu_abi_timestamp(&timestamp) && timestamp==0x123fedcba98ULL && highReads==2);
 rollover=1;highReads=0;
 assert(!fuxi_gpu_abi_timestamp(&timestamp) && timestamp==0x123fedcba98ULL && highReads==4);
 rollover=2;highReads=0;
 assert(fuxi_gpu_abi_timestamp(&timestamp)==-3 && !timestamp && highReads==16);
 rollover=0;readFault=0x980;
 assert(fuxi_gpu_abi_timestamp(&timestamp)==-2 && !timestamp);
 assert(!fuxi_gpu_abi_status(&faults,&suspends) && faults==1 && suspends==1);
 assert(fuxi_gpu_abi_timestamp(&timestamp)==-1 && !timestamp);
 fg_abi_live=0;assert(fuxi_gpu_abi_status(&faults,&suspends)==-1 && !faults && !suspends);
 reset();fault=1; // idle hardware fault also loses the context
 assert(!fuxi_gpu_abi_status(&faults,&suspends) && faults==1 && suspends==1 && drains==1);
 assert(!fuxi_gpu_abi_status(&faults,&suspends) && faults==1 && suspends==1 && drains==1);
 reset();fg_faults=fg_suspends=UINT64_MAX;fault=1;
 assert(!fuxi_gpu_abi_status(&faults,&suspends) && faults==UINT64_MAX && suspends==UINT64_MAX);
 reset();state=2;fg_faults=1; // present failure is visible without client poisoning
 assert(!fuxi_gpu_abi_status(&faults,&suspends) && faults==1 && !suspends && !drains);
 fault=1;fg_abi_watch(NULL,NULL);assert(!drains && fuxi_gpu_abi_poll(0)==-1);
 puts("PASS: CP ticks/rollover/retry bound/MMIO failure, idle faults, status after loss, monotonic counts and saturation");
 reset();
 assert(writeCount==8 && !regs[G3_APERTURE_HOST] && !logLines);
 assert(*(uint64_t *)G_PTR(G3_PT)==0x5a5a5a5a5a5a5a5aULL);
 for(uint64_t pa=G5_META_PA;pa<FG_FW_BASE+3*FG_FW_SLOT;pa+=8)
  assert(*(uint64_t *)G_PTR(pa)==0x5a5a5a5a5a5a5a5aULL);
 volatile uint64_t *root=(volatile uint64_t *)G_PTR(FG_ABI_PT);
 assert((root[0]&3)==3 && (root[1]&3)==3 && !root[2]);
 volatile uint64_t *runtime=(volatile uint64_t *)G_PTR(FG_ABI_PT+12288);
 assert((runtime[0]&~4095ULL)==G3_SQE_PA && (runtime[0]&0xc0)==0xc0);
 /* Deliberately cross a 1 GiB root boundary with scattered 16K pages. */
 uint64_t va=0x3fffc000,bytes=32768;uintptr_t source=0x100000;
 pmapWalks=0;assert(!fuxi_gpu_abi_map(va,source,bytes,3,0,2));
 assert(pmapWalks==bytes/16384); /* one pmap walk per 16K page, not per 4K leaf and not twice */
 for(unsigned off=0;off<bytes;off+=4096) {
  uint64_t want=((uint64_t)pmap_find_phys(0,source+off)<<14)|((source+off)&16383)|3|1u<<6|2u<<8|1u<<10;
  assert(*fg_abi_leaf(va+off)==want);
 }
 /* 4K-aligned but not 16K-aligned source: leaves still come from the right page. */
 pmapWalks=0;assert(!fuxi_gpu_abi_map(0x30000000,source+4096,16384,3,0,2) && pmapWalks==2);
 for(unsigned off=0;off<16384;off+=4096) {
  uint64_t want=((uint64_t)pmap_find_phys(0,source+4096+off)<<14)|((source+4096+off)&16383)|3|1u<<6|2u<<8|1u<<10;
  assert(*fg_abi_leaf(0x30000000+off)==want);
 }
 assert(!fuxi_gpu_abi_unmap(0x30000000,16384));
 /* A range spanning two 2 MiB chunks: the per-chunk leaf pointer must switch tables. */
 assert(!fuxi_gpu_abi_map(0x201ff000-0x1000,source,0x4000,3,0,2));
 for(unsigned off=0;off<0x4000;off+=4096) assert(*fg_abi_leaf(0x201ff000-0x1000+off));
 assert(*fg_abi_leaf(0x20200000)!=*fg_abi_leaf(0x201ff000) && !fuxi_gpu_abi_unmap(0x201ff000-0x1000,0x4000));
 assert(!*fg_abi_leaf(0x20200000) && !*fg_abi_leaf(0x201fe000));
 assert(fuxi_gpu_abi_map(va,source,bytes,3,0,2)==-1); /* no replacement */
 assert(!fuxi_gpu_abi_map(0x7ffff000,source,4096,1,1,3));
 assert((*fg_abi_leaf(0x7ffff000)&0xfff)==(3|3u<<6|1u<<2|3u<<8|1u<<10));
 assert(fuxi_gpu_abi_map(0x80000000,source,4096,3,0,2)==-1);
 assert(*(uint64_t *)G_PTR(G3_PT)==0x5a5a5a5a5a5a5a5aULL);
 uint64_t sizes[1]={4096},addresses[1]={va},fence=0;
 fg_busy=1;
 assert(fuxi_gpu_abi_submit(addresses,sizes,1,&fence)==1 && !doorbells);
 fg_busy=0;fg_scanout_reserved=1;
 assert(fuxi_gpu_abi_submit(addresses,sizes,1,&fence)==1 && !doorbells);
 fg_scanout_reserved=0;
 assert(!fuxi_gpu_abi_submit(addresses,sizes,1,&fence) && fence==1 && fg_client_busy && enqueued==1);
 assert(sregs[(g_cb(g3_bank)+0x20)/4]==(uint32_t)FG_ABI_PT);
 assert(fuxi_gpu_abi_submit(addresses,sizes,1,&fence)==1 && doorbells==1);
 assert(fuxi_gpu_abi_unmap(va,bytes)==1);
 /* A map of free VA does not wait for the running job: leaf and used bit published, no TLBI, doorbell or retirement. */
 { unsigned t0=tlbs,d0=doorbells; assert(!fuxi_gpu_abi_map(0x30000000,source,4096,3,0,2) && fg_client_busy && tlbs==t0 && doorbells==d0);
   assert(*fg_abi_leaf(0x30000000) && fg_abi_used_get(fg_abi_pg(0x30000000)));
   assert(fuxi_gpu_abi_map(0x30000000,source,4096,3,0,2)==-1 && fuxi_gpu_abi_map(va,source,bytes,3,0,2)==-1); /* overlap still refused while busy */
   assert(fuxi_gpu_abi_unmap(0x30000000,4096)==1 && *fg_abi_leaf(0x30000000)); } /* unmap keeps its idle rule */
 assert(fuxi_gpu_abi_poll(fence)==1);
 unsigned schedules=enqueued;fg_abi_watch(NULL,NULL);assert(enqueued==schedules+1);
 complete(); ticks=200000; /* completion wins despite a late watchdog */
 fg_abi_watch(NULL,NULL);
 assert(!fg_client_busy && !fuxi_gpu_abi_poll(fence) && !drains);
 assert(sregs[(g_cb(g3_bank)+0x20)/4]==(uint32_t)G3_PT);
 assert(!fuxi_gpu_abi_unmap(va,bytes));
 for(unsigned off=0;off<bytes;off+=4096) assert(!*fg_abi_leaf(va+off));
 assert(!fuxi_gpu_abi_map(va,source,bytes,3,0,2)); /* reuse after TLBI */
 /* Client never calls WaitFence: delayed callback must still stop DMA. */
 for(unsigned good=0;good<2;good++) {
  reset();drainGood=good;fg_scanout_owned=1;
  assert(!fuxi_gpu_abi_submit(addresses,sizes,1,&fence));
  regs[0x928]=0x23450000;regs[0x92a]=0x90;sregs[(g_cb(g3_bank)+0x58)/4]=0xc0000002;
  unsigned beforeWrites=writeCount;
  ticks=100001;fg_abi_watch(NULL,NULL);
  assert(state==2 && drains==1 && !fg_client_busy && !fuxi_gpu_abi_available());
  assert(fuxi_gpu_abi_poll(fence)==(good?-3:-4));
  assert(scanoutStops==!good);
  assert(!fuxi_gpu_abi_status(&faults,&suspends) && faults==1 && suspends==1);
  assert(hangLines==16 && writeCount==beforeWrites);
  assert(strstr(hangLog[3],"CP_IB1_BASE_LO [0x928]=0x23450000"));
  assert(strstr(hangLog[5],"CP_IB1_REM_SIZE [0x92a]=0x90"));
  assert(strstr(hangLog[14],"FSR 0xc0000002"));
  fg_abi_watch(NULL,NULL);assert(hangLines==16 && drains==1);
  assert(fuxi_gpu_abi_unmap(va,bytes)==(good?0:-2));
 }
 reset();assert(!fuxi_gpu_abi_submit(addresses,sizes,1,&fence));fault=1;
 assert(fuxi_gpu_abi_poll(fence)==-1 && drains==1 && state==2);
 reset();failTlb=1;assert(fuxi_gpu_abi_submit(addresses,sizes,1,&fence)==-1 && !doorbells && drains==1);
 // Fill every import slot, refuse overflow without publishing a PTE, reuse
 // a slot beyond the old 64-entry limit, then retire/cache and drain all 512.
 reset();
 for(unsigned i=0;i<FG_ABI_MAX_IMPORTS;i++)
  assert(!fuxi_gpu_abi_map_import(0x20000000+i*16384,source+i*16384,16384,3));
 assert(FG_ABI_MAX_IMPORTS>=512);
 assert(fuxi_gpu_abi_map_import(0x30000000,source,16384,3)==-1 && !*fg_abi_leaf(0x30000000));
 assert(!fuxi_gpu_abi_unmap(0x20000000+300*16384,16384));
 assert(!fuxi_gpu_abi_map_import(0x20000000+300*16384,source,16384,3));
 assert(!fuxi_gpu_abi_submit(addresses,sizes,1,&fence));
 assert(cacheClean==FG_ABI_MAX_IMPORTS*256);
 complete();assert(!fuxi_gpu_abi_poll(fence) && cacheInvalidate==cacheClean);
 for(unsigned i=0;i<FG_ABI_MAX_IMPORTS;i++) {
  assert(!fuxi_gpu_abi_unmap(0x20000000+i*16384,16384));
  assert(!fg_abi_imports[i].iova);
 }
 puts("PASS: all 512 import slots, overflow refusal, high-slot reuse, full cache retirement and unmap");
 // Indirectly referenced imported BOs must participate, even though the IB
 // itself lives in a different ordinary BO. Watchdog retires without WaitFence.
 reset();
 assert(!fuxi_gpu_abi_map_import(va,source,32768,3));
 assert(!cacheClean && !cacheInvalidate);
 assert(fuxi_gpu_abi_map_import(va+0x10000,source+1,16384,3)==-1);
 assert(fuxi_gpu_abi_map_import(va+0x10000,source,4096,3)==-1);
 assert(!fuxi_gpu_abi_submit(addresses,sizes,1,&fence));
 assert(cacheClean==512 && !cacheInvalidate);
 assert(fuxi_gpu_abi_unmap(va,32768)==1 && fg_abi_imports[0].iova==va);
 assert(!fuxi_gpu_abi_map_import(va+0x10000,source,16384,3) && fg_client_busy); /* mapped while the job runs */
 complete(); fg_abi_watch(NULL,NULL);
 assert(cacheInvalidate==768 && !fuxi_gpu_abi_poll(fence)); /* 512 + the 16K import mapped during the job */
 assert(!fuxi_gpu_abi_unmap(va,32768) && !fuxi_gpu_abi_unmap(va+0x10000,16384) && !fg_abi_imports[0].iova && !fg_abi_imports[1].iova);
 assert(!fuxi_gpu_abi_submit(addresses,sizes,1,&fence));
 complete();fg_abi_watch(NULL,NULL);assert(cacheClean==512 && cacheInvalidate==768);
 for(unsigned good=0;good<2;good++) {
  reset();drainGood=good;fg_scanout_owned=1;
  assert(!fuxi_gpu_abi_map_import(va,source,16384,3));
  assert(!fuxi_gpu_abi_submit(addresses,sizes,1,&fence));
  ticks=100001;fg_abi_watch(NULL,NULL);
  assert(cacheClean==256 && cacheInvalidate==good*256);
  assert(fuxi_gpu_abi_unmap(va,16384)==(good?0:-2));
  assert(!!fg_abi_imports[0].iova==!good); // unknown DMA keeps mapping owned
 }
 reset();assert(!fuxi_gpu_abi_map_import(va,source,16384,3));
 failTlb=1;assert(fuxi_gpu_abi_unmap(va,16384)==-1); // unmap keeps its TLB sync
 assert(!cacheClean && !cacheInvalidate); // no submit: preserve dirty CPU data
 // A read-only import is cleaned for the GPU but never invalidated at retirement; a writable one still is.
 reset();assert(!fuxi_gpu_abi_map_import(va,source,16384,1));
 assert(!fuxi_gpu_abi_submit(addresses,sizes,1,&fence) && cacheClean==256);
 complete();assert(!fuxi_gpu_abi_poll(fence) && !cacheInvalidate && fg_abi_imports[0].iova==va);
 assert(!fuxi_gpu_abi_unmap(va,16384) && !fg_abi_imports[0].iova);
 reset();assert(!fuxi_gpu_abi_map_import(va,source,16384,1) && !fuxi_gpu_abi_map_import(va+0x10000,source,16384,3));
 assert(!fuxi_gpu_abi_submit(addresses,sizes,1,&fence) && cacheClean==512);
 complete();fg_abi_watch(NULL,NULL);assert(cacheInvalidate==256); // only the writable range
 assert(!fuxi_gpu_abi_unmap(va,16384) && !fuxi_gpu_abi_unmap(va+0x10000,16384));
 // GPU_ONLY imports: cleaned once at map, never at submit or retirement, invalidated once at unmap; the read-only neighbour is unchanged.
 {struct fuxi_gpu_clean_split z;fuxi_gpu_abi_clean_split(&z);}
 reset();assert(!fuxi_gpu_abi_map_import(va,source,16384,3|FG_ABI_IMPORT_GPU_ONLY) && !fuxi_gpu_abi_map_import(va+0x10000,source,16384,1));
 assert(cacheClean==256 && fg_abi_imports[0].gpu_only && !fg_abi_imports[1].gpu_only && fg_abi_imports[0].permissions==3);
 cacheClean=0;
 assert(!fuxi_gpu_abi_submit(addresses,sizes,1,&fence) && cacheClean==256);
 {struct fuxi_gpu_clean_split cs;fuxi_gpu_abi_clean_split(&cs);
  assert(cs.kib[1]==16 && !cs.kib[0] && cs.go_kib==16 && cs.imports==2 && cs.go_imports==1 && cs.bkt_kib[0]==16 && cs.top_kib[0]==16 && cs.top_perm[0]==(3|16));
  fuxi_gpu_abi_clean_split(&cs);assert(!cs.kib[1] && !cs.go_kib && cs.imports==2);}
 complete();fg_abi_watch(NULL,NULL);assert(!cacheInvalidate);
 assert(!fuxi_gpu_abi_unmap(va,16384) && cacheInvalidate==256 && !fg_abi_imports[0].iova && !fg_abi_imports[0].gpu_only);
 assert(!fuxi_gpu_abi_unmap(va+0x10000,16384) && cacheInvalidate==256);
 // Per-page dirty tracking (bit 524288): clear then clean at map, only modified pages cleaned at submit, fail closed.
 {
  struct fuxi_gpu_clean_split cs;
  reset();fg_abi_dirty=1;fg_abi_dty_ok=0;memset(fg_abi_dty_used,0,sizeof(fg_abi_dty_used));memset(modTab,0,sizeof(modTab));pokeSets=1;pokes=0;invalidPages=0;fuxi_gpu_abi_clean_split(&cs);
  unsigned pg=(unsigned)((source>>14)&0xfff);if(pg>4090)pg=0;
  assert(!fuxi_gpu_abi_map_import(va,source,49152,3) && fg_abi_dty_ok==1 && pokes==1 && fg_abi_imports[0].dty_first==1 && fg_abi_imports[0].dty_pages==3 && fg_abi_dty_used[0]);
  assert(cacheClean==768 && !modTab[pg] && !modTab[pg+1] && !modTab[pg+2]);
  cacheClean=0;modTab[pg+1]=1; // a CPU store after the map
  assert(!fuxi_gpu_abi_submit(addresses,sizes,1,&fence) && cacheClean==256 && !modTab[pg+1]);
  fuxi_gpu_abi_clean_split(&cs);
  assert(cs.dty_state==2 && cs.dty_checked==3 && cs.dty_dirty==1 && cs.dty_kib==16 && cs.dty_imports==1 && !cs.dty_full_kib && cs.submits==1);
  complete();fg_abi_watch(NULL,NULL);cacheClean=0;
  assert(!fuxi_gpu_abi_submit(addresses,sizes,1,&fence) && !cacheClean); // nothing written since: nothing cleaned
  fuxi_gpu_abi_clean_split(&cs);assert(cs.dty_checked==3 && !cs.dty_dirty);
  complete();fg_abi_watch(NULL,NULL);
  assert(!fuxi_gpu_abi_unmap(va,49152) && !fg_abi_dty_used[0] && !fg_abi_imports[0].dty_first);
  // An unmanaged page, or a failed self-test, keeps the full clean (fail closed).
  invalidPages=1;cacheClean=0;assert(!fuxi_gpu_abi_map_import(va,source,49152,3) && !fg_abi_imports[0].dty_first && !cacheClean);
  assert(!fuxi_gpu_abi_submit(addresses,sizes,1,&fence) && cacheClean==768);fuxi_gpu_abi_clean_split(&cs);assert(cs.dty_full_kib==48 && !cs.dty_checked);
  complete();fg_abi_watch(NULL,NULL);assert(!fuxi_gpu_abi_unmap(va,49152));
  invalidPages=0;pokeSets=0;fg_abi_dty_ok=0;
  assert(!fuxi_gpu_abi_map_import(va,source,49152,3) && fg_abi_dty_ok==2 && !fg_abi_imports[0].dty_first);
  cacheClean=0;assert(!fuxi_gpu_abi_submit(addresses,sizes,1,&fence) && cacheClean==768);fuxi_gpu_abi_clean_split(&cs);assert(cs.dty_state==3);
  complete();fg_abi_watch(NULL,NULL);assert(!fuxi_gpu_abi_unmap(va,49152));
  // Retire IVAC is timed apart from the submit side.
  fg_abi_dirty=0;pokeSets=1;fg_abi_dty_ok=0;
 }
 // Submit wait hint: a held present gets the rest of the 4 ms hold, a running copy 1 ms, anything else 100 us.
 reset();fg_abi_hold=1;fg_abi_present_wait=1;fg_abi_hold_since=ticks=1000;assert(wait_us()==4000);
 ticks=1000+3000;assert(wait_us()==1001);ticks=1000+4000;assert(wait_us()==1);
 fg_abi_present_wait=0;fg_busy=1;assert(wait_us()==1000);fg_busy=0;assert(wait_us()==100);fg_abi_hold=0;
 // A client job retiring wakes the waiters.
 {wakes=0;fuxi_gpu_abi_wake_hook=count_wake;reset();assert(!fuxi_gpu_abi_map(va,source,bytes,3,0,2) && !fuxi_gpu_abi_submit(addresses,sizes,1,&fence));
  complete();fg_abi_watch(NULL,NULL);assert(wakes>=1);fuxi_gpu_abi_wake_hook=NULL;}
 // A retired job is seen by the next submit/map/unmap itself, without waiting for the 2 ms watchdog.
 reset();assert(!fuxi_gpu_abi_map(va,source,bytes,3,0,2));
 assert(!fuxi_gpu_abi_submit(addresses,sizes,1,&fence) && fg_client_busy);
 assert(fuxi_gpu_abi_submit(addresses,sizes,1,&fence)==1 && fuxi_gpu_abi_unmap(va,bytes)==1 && !fuxi_gpu_abi_map(0x30010000,source,4096,3,0,2));
 complete();tlbs=0;
 assert(!fuxi_gpu_abi_unmap(va,bytes) && !fg_client_busy && tlbs==2 && !drains); /* restore-switch + the unmap's own TLBI */
 assert(sregs[(g_cb(g3_bank)+0x20)/4]==(uint32_t)G3_PT && !*fg_abi_leaf(va));
 assert(!fuxi_gpu_abi_map(va,source,bytes,3,0,2) && !fuxi_gpu_abi_submit(addresses,sizes,1,&fence));
 complete();uint64_t second=0;
 assert(!fuxi_gpu_abi_submit(addresses,sizes,1,&second) && second==fence+1 && doorbells==3 && fg_client_busy);
 complete();assert(!fuxi_gpu_abi_map(0x30000000,source,4096,3,0,2) && !fg_client_busy);
 // Observation on the busy path still isolates a faulting job.
 reset();assert(!fuxi_gpu_abi_map(va,source,bytes,3,0,2) && !fuxi_gpu_abi_submit(addresses,sizes,1,&fence));
 fault=1;regs[0x928]=1;assert(fuxi_gpu_abi_submit(addresses,sizes,1,&second)==-1 && state==2 && !fg_client_busy);
 puts("PASS: imported BO cache handoff covers indirect resources, watchdog retirement, drained fault and undrained quarantine");
 // Optional writable-import lifetime handoff. NC SMMU attributes stay equal;
 // only initial map clean replaces per-submit clean. Completion still IVACs writable imports (not the read-only one).
 for(unsigned arg=0;arg<2;arg++) {
  seed();ncimportArg=arg;assert(fg_abi_init());
  assert(fg_abi_ncimport==(arg==1) && fg_wptr_fence==(arg==2) && !fg_abi_unstick && !fg_abi_cohtest && !fg_abi_tlbpages);
  assert(!fuxi_gpu_abi_map_import(va,source,32768,3));
  assert(!(*fg_abi_leaf(va)&(1u<<2)) && ((*fg_abi_leaf(va)>>8)&3)==2);
  assert(!fuxi_gpu_abi_map_import(va+0x10000,source+65536,16384,1));
  assert(cacheClean==(arg==1?512u:0u));
  // Rejected/overlapping and busy maps must never do a cache handoff.
  unsigned before=cacheClean;
  assert(fuxi_gpu_abi_map_import(va,source,16384,3)==-1 && cacheClean==before);
  for(unsigned frame=0;frame<2;frame++) {
   assert(!fuxi_gpu_abi_submit(addresses,sizes,1,&fence));
   before=cacheClean;
   assert(fuxi_gpu_abi_map_import(va,source,16384,3)==-1 && cacheClean==before);
   complete();assert(!fuxi_gpu_abi_poll(fence));
  }
  uint64_t cleaned,skipped,invalidated,mapped;unsigned imports;
  fuxi_gpu_import_stats(&cleaned,&skipped,&invalidated,&mapped,&imports);
  assert(imports==2 && mapped==49152 && invalidated==65536);
  assert(cleaned==(arg==1?65536u:98304u) && skipped==(arg==1?65536u:0u));
  assert(cacheClean==cleaned/64 && cacheInvalidate==invalidated/64);
  assert(!fuxi_gpu_abi_unmap(va,32768));
  fuxi_gpu_import_stats(&cleaned,&skipped,&invalidated,&mapped,&imports);
  assert(imports==1 && mapped==16384);
  // Default maps during a job: only the new range is handed off (one map-time clean with arg 1), the running job's imports are not walked.
  assert(!fuxi_gpu_abi_submit(addresses,sizes,1,&fence));before=cacheClean;
  assert(!fuxi_gpu_abi_map_import(va+0x20000,source,16384,3) && fg_client_busy && cacheClean==before+(arg==1?256u:0u));
  complete();assert(!fuxi_gpu_abi_poll(fence));
 }
 for(unsigned good=0;good<2;good++) {
  seed();ncimportArg=1;assert(fg_abi_init());drainGood=good;
  assert(!fuxi_gpu_abi_map_import(va,source,16384,3));
  assert(!fuxi_gpu_abi_submit(addresses,sizes,1,&fence));
  ticks=100001;fg_abi_watch(NULL,NULL);
  assert(cacheClean==256 && cacheInvalidate==good*256 && fg_abi_skipped==16384);
  assert(fuxi_gpu_abi_unmap(va,16384)==(good?0:-2));
 }
  puts("PASS: opt-in NC-import map handoff, indirect RW/RO policy, repeated submits, byte counters, map failure/busy and fault quarantine");
 /* fuxigpuncimport is a bitmask: 1 NC import, 64 coherent self-test, 128 ranged TLBI. Value 1 keeps its meaning;
  * the low bits 2..32 stay free for other lanes and are ignored here. */
 for(unsigned arg=0;arg<1024;arg++) {
  seed();ncimportArg=arg;assert(fg_abi_init());
  assert(fg_abi_ncimport==(arg&1) && fg_abi_cohtest==((arg>>6)&1) && fg_abi_tlbpages==((arg>>7)&1) && fg_abi_mapwait==((arg>>8)&1) && fg_abi_queue==((arg>>9)&1) && !fg_abi_coh && !delays);
 }
 /* Cached overlap mirror replaces the NC leaf read: exact 4K overlap refusal, nothing published on refusal, unmap/init clear. */
 reset();assert(!fuxi_gpu_abi_map(va,source,32768,3,0,2));
 assert(fuxi_gpu_abi_map(va+0x7000,source,8192,3,0,2)==-1 && fuxi_gpu_abi_map(va-4096,source,8192,3,0,2)==-1);
 assert(!*fg_abi_leaf(va-4096) && !*fg_abi_leaf(va+0x8000));
 assert(!fuxi_gpu_abi_map(va+0x8000,source,4096,3,0,2) && !fuxi_gpu_abi_map(va-4096,source,4096,3,0,2));
 assert(!fuxi_gpu_abi_unmap(va,32768) && !fg_abi_used_get(fg_abi_pg(va)) && fg_abi_used_get(fg_abi_pg(va+0x8000)));
 assert(!fuxi_gpu_abi_map(va,source,32768,3,0,2) && fuxi_gpu_abi_map(va,source,4096,3,0,2)==-1);
 assert(!fuxi_gpu_abi_unmap(va,32768) && !fuxi_gpu_abi_unmap(va+0x8000,4096) && !fuxi_gpu_abi_unmap(va-4096,4096));
 for(unsigned i=0;i<sizeof(fg_abi_used)/sizeof(fg_abi_used[0]);i++) assert(!fg_abi_used[i]);
 /* Word-wise bitmap: ranges that start/end mid-word, span several words, and fill an exact word. */
 { static const struct { uint64_t pg, n; } r[]={{0,64},{3,61},{60,10},{64,128},{100,300},{63,1},{1,200},{511,513}};
   for(unsigned i=0;i<sizeof(r)/sizeof(r[0]);i++) {
    uint64_t v=0x40000000+r[i].pg*4096,nb=r[i].n*4096;reset();
    assert(!fuxi_gpu_abi_map(v,source,nb,3,0,2));
    for(uint64_t k=r[i].pg>1?r[i].pg-2:0;k<r[i].pg+r[i].n+2;k++) assert(fg_abi_used_get(fg_abi_pg(0x40000000+k*4096))==(k>=r[i].pg && k<r[i].pg+r[i].n));
    assert(fuxi_gpu_abi_map(v+(r[i].n-1)*4096,source,4096,3,0,2)==-1 && fuxi_gpu_abi_map(v-(r[i].pg?4096:0),source,r[i].pg?8192:4096,3,0,2)==-1);
    assert(!fuxi_gpu_abi_unmap(v,nb));
    for(unsigned w=0;w<sizeof(fg_abi_used)/sizeof(fg_abi_used[0]);w++) assert(!fg_abi_used[w]);
   }
 }
 /* Queue mode (fuxigpuncimport 512): a submit behind a running client job appends to the in-order ring. */
 {
  uint64_t sz[1]={4096},ad[1]={va},f1=0,f2=0,f3=0,f4=0,v2=0x30100000;uint32_t q[6];
  uint32_t ttbrG=(uint32_t)G3_PT,ttbrC=(uint32_t)FG_ABI_PT;
  #define QSTART() do { seed();ncimportArg=512;assert(fg_abi_init() && fg_abi_queue && fuxi_gpu_abi_queue_enabled());assert(!fuxi_gpu_abi_map(va,source,bytes,3,0,2)); } while(0)
  #define DONE_UP_TO(n) g_wr(G3_FENCE_PA,FG_ABI_FENCE_OFF+4,(n))
  /* Default (no bit) is unchanged: a second submit while busy is refused. */
  reset();assert(!fuxi_gpu_abi_map(va,source,bytes,3,0,2) && !fuxi_gpu_abi_submit(ad,sz,1,&f1) && fuxi_gpu_abi_submit(ad,sz,1,&f2)==1 && !fuxi_gpu_abi_queue_enabled());
  /* Queued submit: same root, no TTBR switch or TLBI, doorbell rung again, one fence per job, in-order retirement. */
  QSTART();
  assert(!fuxi_gpu_abi_submit(ad,sz,1,&f1) && f1==1 && sregs[(g_cb(g3_bank)+0x20)/4]==ttbrC);
  unsigned t0=tlbs,d0=doorbells,w0=g3_wptr;
  assert(!fuxi_gpu_abi_submit(ad,sz,1,&f2) && f2==2 && tlbs==t0 && doorbells==d0+1 && ((g3_wptr-w0)&8191)>=40 && sregs[(g_cb(g3_bank)+0x20)/4]==ttbrC);
  assert(((ring[w0&8191]>>16)&0x7f)==0x17); /* the second frame opens like the first: sync, reset */
  fuxi_gpu_abi_queue_stats(q);assert(q[0]==1 && q[1]==2);
  assert(fuxi_gpu_abi_poll(1)==1 && fuxi_gpu_abi_poll(2)==1 && fuxi_gpu_abi_poll(3)==-1);
  DONE_UP_TO(1); /* job 1 finished, job 2 running */
  assert(!fuxi_gpu_abi_poll(1) && fuxi_gpu_abi_poll(2)==1 && fg_abi_completed==1 && fg_client_busy && sregs[(g_cb(g3_bank)+0x20)/4]==ttbrC);
  assert(fuxi_gpu_abi_unmap(va,bytes)==1 && *fg_abi_leaf(va)); /* unmap waits for every job, not just the first */
  assert(fuxi_gpu_abi_submit(ad,sz,1,&f3)==0 && f3==3); /* and the chain keeps accepting */
  complete(); /* fence 3, ring idle */
  assert(!fuxi_gpu_abi_poll(2) && !fuxi_gpu_abi_poll(3) && !fg_client_busy && fg_abi_completed==3 && sregs[(g_cb(g3_bank)+0x20)/4]==ttbrG);
  assert(!fuxi_gpu_abi_unmap(va,bytes) && !*fg_abi_leaf(va));
  /* Depth and ring room: at most FG_ABI_QUEUE_MAX jobs in flight, and never past the free space. */
  QSTART();
  for(unsigned i=0;i<FG_ABI_QUEUE_MAX;i++) assert(!fuxi_gpu_abi_submit(ad,sz,1,&f1));
  assert(fuxi_gpu_abi_submit(ad,sz,1,&f2)==1);fuxi_gpu_abi_queue_stats(q);assert(q[0]==FG_ABI_QUEUE_MAX-1 && q[1]==FG_ABI_QUEUE_MAX && q[3]==1);
  DONE_UP_TO(2);assert(fuxi_gpu_abi_poll(4)==1 && fg_abi_completed==2 && !fuxi_gpu_abi_submit(ad,sz,1,&f2) && f2==5);
  QSTART();
  assert(!fuxi_gpu_abi_submit(ad,sz,1,&f1));regs[0x806]=(g3_wptr-8100)&8191;
  assert(fuxi_gpu_abi_submit(ad,sz,1,&f2)==1);fuxi_gpu_abi_queue_stats(q);assert(q[3]==1 && fg_abi_seq==1);
  /* A map since the last submit drops the ASID translations once; a repeat does not. */
  QSTART();
  assert(!fuxi_gpu_abi_submit(ad,sz,1,&f1) && !fuxi_gpu_abi_map(v2,source,4096,3,0,2));t0=tlbs;
  assert(!fuxi_gpu_abi_submit(ad,sz,1,&f2) && tlbs==t0+1 && !fuxi_gpu_abi_submit(ad,sz,1,&f3) && tlbs==t0+1);
  failTlb=1;assert(!fuxi_gpu_abi_map(v2+0x10000,source,4096,3,0,2) && fuxi_gpu_abi_submit(ad,sz,1,&f4)==-1 && state==2);failTlb=0; /* fail closed */
  /* Present interleaving: a refused present stops queueing; the chain drains, the root is restored, the flag clears. */
  QSTART();
  assert(!fuxi_gpu_abi_submit(ad,sz,1,&f1));fg_abi_present_wait=1;
  assert(fuxi_gpu_abi_submit(ad,sz,1,&f2)==1);fuxi_gpu_abi_queue_stats(q);assert(q[2]==1 && fg_abi_seq==1);
  complete();assert(!fuxi_gpu_abi_poll(1) && !fg_client_busy && !fg_abi_present_wait && sregs[(g_cb(g3_bank)+0x20)/4]==ttbrG);
  assert(!fuxi_gpu_abi_submit(ad,sz,1,&f2) && f2==2);
  /* Present hold (bit 4096): the waiting present keeps the idle GPU after the chain retires; the copy ends the hold, 4 ms bounds it. */
  seed();ncimportArg=512|4096;assert(fg_abi_init() && fg_abi_queue && fg_abi_hold);assert(!fuxi_gpu_abi_map(va,source,bytes,3,0,2));
  assert(!fuxi_gpu_abi_submit(ad,sz,1,&f1) && !fuxi_gpu_abi_submit(ad,sz,1,&f2));fg_abi_present_wait=1;
  assert(fuxi_gpu_abi_submit(ad,sz,1,&f3)==1);fuxi_gpu_abi_queue_stats(q);assert(q[4]==1 && fg_abi_seq==2);
  DONE_UP_TO(1);assert(fuxi_gpu_abi_poll(2)==1 && fg_abi_present_wait);
  complete();assert(!fuxi_gpu_abi_poll(2) && !fg_client_busy && fg_abi_present_wait && sregs[(g_cb(g3_bank)+0x20)/4]==ttbrG); /* idle, hold kept */
  assert(fuxi_gpu_abi_submit(ad,sz,1,&f3)==1);fuxi_gpu_abi_queue_stats(q);assert(q[4]==2 && fg_abi_seq==2);
  fg_abi_present_wait=0;assert(!fuxi_gpu_abi_submit(ad,sz,1,&f3) && f3==3); /* the copy was submitted: clients flow again */
  complete();assert(!fuxi_gpu_abi_poll(3));
  fg_abi_present_wait=1;ticks+=1000;assert(fuxi_gpu_abi_submit(ad,sz,1,&f4)==1);
  ticks+=4001;assert(!fuxi_gpu_abi_submit(ad,sz,1,&f4) && f4==4 && !fg_abi_present_wait);fuxi_gpu_abi_queue_stats(q);assert(q[5]==1); /* the present went away */
  complete();assert(!fuxi_gpu_abi_poll(4));
  /* Without the bit the flag still clears at retirement. */
  QSTART();
  assert(!fuxi_gpu_abi_submit(ad,sz,1,&f1) && !fuxi_gpu_abi_submit(ad,sz,1,&f2));
  fg_busy=1;assert(fuxi_gpu_abi_submit(ad,sz,1,&f3)==1);fg_busy=0;fg_scanout_reserved=1;assert(fuxi_gpu_abi_submit(ad,sz,1,&f3)==1);fg_scanout_reserved=0;
  /* A fault in the second job: the first stays successful, the second and later fail, the GPU is isolated once. */
  QSTART();
  assert(!fuxi_gpu_abi_submit(ad,sz,1,&f1) && !fuxi_gpu_abi_submit(ad,sz,1,&f2) && !fuxi_gpu_abi_submit(ad,sz,1,&f3));
  DONE_UP_TO(1);fault=1;regs[0x928]=1;
  assert(fuxi_gpu_abi_poll(3)==-1 && state==2 && drains==1 && !fg_client_busy && fg_abi_completed==1);
  assert(!fuxi_gpu_abi_poll(1) && fuxi_gpu_abi_poll(2)<0 && fuxi_gpu_abi_poll(3)<0 && fuxi_gpu_abi_submit(ad,sz,1,&f4)==-1);
  /* Timeout of the running job after its predecessor retired: deadline restarts at the retirement, then 100 ms isolate. */
  QSTART();
  assert(!fuxi_gpu_abi_submit(ad,sz,1,&f1) && !fuxi_gpu_abi_submit(ad,sz,1,&f2));
  ticks=90000;DONE_UP_TO(1);assert(fuxi_gpu_abi_poll(2)==1 && fg_abi_completed==1); /* 90 ms of 100 ms spent, but job 2 got a fresh budget */
  ticks=90000+99000;assert(fuxi_gpu_abi_poll(2)==1);
  ticks=90000+100001;assert(fuxi_gpu_abi_poll(2)==-3 && fg_abi_timed_out && fg_abi_completed==1 && !fuxi_gpu_abi_poll(1));
  /* Imports: every queued job hands its imports to the GPU, and each retirement drops the CPU's lines. */
  QSTART();
  assert(!fuxi_gpu_abi_map_import(v2,source,16384,3));
  assert(!fuxi_gpu_abi_submit(ad,sz,1,&f1) && !fuxi_gpu_abi_submit(ad,sz,1,&f2) && cacheClean==512 && !cacheInvalidate);
  DONE_UP_TO(1);assert(fuxi_gpu_abi_poll(2)==1 && cacheInvalidate==256 && fg_abi_host_handoff);
  complete();assert(!fuxi_gpu_abi_poll(2) && cacheInvalidate==512 && !fg_abi_host_handoff);
  #undef QSTART
  #undef DONE_UP_TO
  puts("PASS: queue mode (bit 512): queued submits keep the client root, in-order retirement with per-job deadline, depth/ring limits, TLBI only after a map, present stops queueing, fault in job 2 keeps job 1, unmap/import handoff per job");
 }
 /* Inline root (bit 21, needs 512): the client root keeps its window and stays installed; a copy in flight never refuses or delays a client job. */
 {
  uint64_t sz[1]={4096},ad[1]={va},f1=0,f2=0,f3=0;
  uint32_t ttbrC=(uint32_t)FG_ABI_PT;
  #define DONE_UP_TO(n) g_wr(G3_FENCE_PA,FG_ABI_FENCE_OFF+4,(n))
  seed();ncimportArg=2097152;assert(fg_abi_init() && !fg_abi_inline); /* 512 is required */
  seed();ncimportArg=512|2097152;fg_scanout_double_requested=1;fg_scanout_triple_requested=0;memset(presentL2,0,sizeof presentL2);presentL2[128+3]=0x41000|3;
  assert(fg_abi_init() && fg_abi_inline && !fg_inl_ready && !fg_inl_usable());assert(!fuxi_gpu_abi_map(va,source,bytes,3,0,2));
  assert(!fuxi_gpu_abi_submit(ad,sz,1,&f1) && f1==1 && fg_root_client && fg_inl_ready==1 && fg_inl_usable() && sregs[(g_cb(g3_bank)+0x20)/4]==ttbrC && fg_inl_fbmask==3);
  { volatile uint64_t *root=(volatile uint64_t *)G_PTR(FG_ABI_PT),*l2=(volatile uint64_t *)G_PTR(FG_INL_L2);
    assert(root[2]==(FG_INL_L2|3) && !root[3] && l2[256]==(FG_INL_L3|3) && l2[256+9]==((FG_INL_L3+9u*4096)|3) && !l2[256+10]);
    volatile uint64_t *t0l3=(volatile uint64_t *)G_PTR(FG_INL_L3),*t5=(volatile uint64_t *)G_PTR(FG_INL_L3+5u*4096);
    assert(t0l3[0]==(G4_FB_PA|3|(3u<<8)|(1u<<10)|(1u<<6)) && t0l3[511]==((G4_FB_PA+511u*4096)|3|(3u<<8)|(1u<<10)|(1u<<6)));
    assert(t5[0]==(0xb8a00000ULL|3|(3u<<8)|(1u<<10)|(1u<<6)));
    assert(l2[128+3]==(0x41000|3) && !l2[128+4]);
    fg_inl_mirror(4,0x77000|1);assert(l2[132]==(0x77000|1));fg_inl_mirror(4,0);assert(!l2[132]); }
  /* The job retires on its fence alone, without restoring the present root. */
  complete();assert(!fuxi_gpu_abi_poll(1) && !fg_client_busy && fg_abi_completed==1 && sregs[(g_cb(g3_bank)+0x20)/4]==ttbrC);
  /* A copy is in flight (ring not empty, fg_busy): the client is not refused, switches nothing, and retires although the copy is still queued. */
  fg_busy=1;fg_inl_busy=1;regs[0x806]=(g3_wptr-100)&8191;regs[0x210]=0;
  unsigned t0=tlbs;
  assert(!fuxi_gpu_abi_submit(ad,sz,1,&f2) && f2==2 && tlbs==t0 && sregs[(g_cb(g3_bank)+0x20)/4]==ttbrC && fg_client_busy);
  DONE_UP_TO(2);assert(!fuxi_gpu_abi_poll(2) && !fg_client_busy && fg_abi_completed==2 && sregs[(g_cb(g3_bank)+0x20)/4]==ttbrC);
  fg_busy=0;fg_inl_busy=0;fg_scanout_reserved=1;assert(!fuxi_gpu_abi_submit(ad,sz,1,&f3) && f3==3);fg_scanout_reserved=0; /* no reservation exists in this mode */
  /* Back to the present root for a classic copy: needs an idle GPU. */
  assert(!fg_inl_to_present() && fg_root_client); /* the client job is running */
  DONE_UP_TO(3);regs[0x806]=regs[0x807]=g3_wptr;assert(!fuxi_gpu_abi_poll(3));
  assert(fg_inl_to_present() && !fg_root_client && sregs[(g_cb(g3_bank)+0x20)/4]==(uint32_t)G3_PT && !fg_inl_usable());
  /* The next client submit installs its root again (idle GPU) and keeps it. */
  assert(!fuxi_gpu_abi_submit(ad,sz,1,&f3) && f3==4 && fg_root_client && sregs[(g_cb(g3_bank)+0x20)/4]==ttbrC);
  /* Window setup refused when the root slot is taken: old path for the whole run. */
  seed();ncimportArg=512|2097152;assert(fg_abi_init());*(volatile uint64_t *)((uintptr_t)G_PTR(FG_ABI_PT)+16)=1;
  assert(!fuxi_gpu_abi_map(va,source,bytes,3,0,2) && !fuxi_gpu_abi_submit(ad,sz,1,&f1) && fg_inl_ready==2 && !fg_root_client && !fg_inl_usable());
  complete();assert(!fuxi_gpu_abi_poll(1) && !fg_client_busy && sregs[(g_cb(g3_bank)+0x20)/4]==(uint32_t)G3_PT);
  #undef DONE_UP_TO
  fg_abi_inline=0;fg_scanout_double_requested=0;
  puts("PASS: inline root (bit 21): client-root window (carveout A/B + mirrored sources), root kept after retirement, copy in flight never refuses or delays a client job, fence-only retire, return to the present root, setup failure keeps the old path");
 }
 /* Copy priority (bit 16, inline only): a submit is refused while a due surface's writer job is already in the ring; released by the copy
  * leaving the due set, bounded to one panel period per due generation; pending locks and unknown surfaces never hold. */
 {
  uint64_t sz[1]={4096},ad[1]={va},f=0;unsigned k=9;
  #define RET(n) do { g_wr(G3_FENCE_PA,FG_ABI_FENCE_OFF+4,(n));regs[0x806]=regs[0x807]=g3_wptr;assert(!fuxi_gpu_abi_poll(n)); } while (0)
  #define SUB() (fuxi_gpu_abi_submit(ad,sz,1,&f))
  const uint32_t s7=7,s8=8,s9=9,s10=10;
  seed();ncimportArg=512|16;assert(fg_abi_init() && !fg_abi_cf);fuxi_gpu_present_writer(7,1);assert(!fg_cf_w[0].id); /* needs inline */
  seed();ncimportArg=512|2097152|16;fg_scanout_double_requested=1;fg_scanout_triple_requested=0;memset(presentL2,0,sizeof presentL2);presentL2[128+3]=0x41000|3;
  assert(fg_abi_init() && fg_abi_inline && fg_abi_cf);assert(!fuxi_gpu_abi_map(va,source,bytes,3,0,2));
  assert(!SUB() && f==1 && fg_inl_usable());RET(1);
  wakes=0;fuxi_gpu_abi_wake_hook=count_wake;ticks=1000;
  fuxi_gpu_present_writer(7,1);fuxi_gpu_present_due(&s7,1);assert(fg_cf_gen==1); /* frame N locked at seq 1, swap N due */
  assert(!SUB() && f==2);RET(2); /* job N itself */
  assert(SUB()==1 && fg_cf_holding && fg_abi_seq==2);fuxi_gpu_abi_submit_wait_us(&k);assert(!k && wait_us()==8334); /* job N+1 waits for copy N */
  ticks+=1000;assert(SUB()==1 && wait_us()==7334);
  wakes=0;fuxi_gpu_present_due(&s7,1);assert(fg_cf_holding && !wakes); /* same set: no release (a retire also wakes) */
  fuxi_gpu_present_due(NULL,0);assert(!fg_cf_holding && wakes==1); /* copy N queued */
  assert(!SUB() && f==3);RET(3);
  /* Expiry: one period per generation; a second submit in the same generation is not held again. */
  fuxi_gpu_present_writer(7,1);fuxi_gpu_present_due(&s7,1);assert(fg_cf_gen==2);
  assert(!SUB() && f==4);RET(4);assert(SUB()==1);ticks+=8334;assert(!SUB() && f==5 && !fg_cf_holding);RET(5);assert(!SUB() && f==6);RET(6);
  /* Frame N+1 on another buffer: a new surface opens a new generation with a new bound. */
  fuxi_gpu_present_writer(8,1);fuxi_gpu_present_due(&s8,1);assert(fg_cf_gen==3);assert(!SUB() && f==7);RET(7);assert(SUB()==1);
  wakes=0;fuxi_gpu_present_due(NULL,0);assert(!fg_cf_holding && wakes==1);
  /* Pending (AvoidSync) lock and a surface without a writer record never hold. */
  fuxi_gpu_present_writer(9,0);fuxi_gpu_present_due(&s9,1);assert(!SUB() && f==8);RET(8);assert(!SUB() && f==9);RET(9);
  fuxi_gpu_present_due(&s10,1);assert(!SUB() && f==10);RET(10);
  assert(fg_cf_stats[0]==4 && fg_cf_stats[1]==3 && fg_cf_stats[2]==2 && fg_cf_stats[3]==1 && fg_cf_stats[5]>=1);
  fg_cf_log();assert(!fg_cf_stats[0] && !fg_cf_stats[4] && fg_abi_inflight()==0);
  #undef RET
  #undef SUB
  fuxi_gpu_abi_wake_hook=NULL;fg_abi_inline=fg_abi_cf=0;fg_scanout_double_requested=0;ticks=0;
  puts("PASS: copy priority (bit 16, inline only): refuse while the due frame's writer job is queued, wait hint and kind 0, release wake on copy, one-period bound per generation, pending/unknown locks never hold");
 }
 /* Park (bit 27, needs 16): the job copy priority would refuse is built and fenced but not rung; the submit returns at once (the caller's
  * backend mutex is free, so frame N's lease can end and its copy be queued); the copy rings it right behind itself. */
 {
  uint64_t sz[1]={4096},ad[1]={va},f=0;
  #define RET(n) do { g_wr(G3_FENCE_PA,FG_ABI_FENCE_OFF+4,(n));regs[0x806]=regs[0x807]=g3_wptr;assert(!fuxi_gpu_abi_poll(n)); } while (0)
  #define SUB() (fuxi_gpu_abi_submit(ad,sz,1,&f))
  #define POLL(n) (fuxi_gpu_abi_poll(n))
  const uint32_t s7=7,s8=8,s9=9,s10=10,s11=11;
  seed();ncimportArg=512|2097152|134217728;assert(fg_abi_init() && fg_abi_inline && !fg_abi_cf && !fg_abi_pk); /* needs 16 */
  seed();ncimportArg=512|2097152|16;assert(fg_abi_init() && fg_abi_cf && !fg_abi_pk); /* default off */
  seed();ncimportArg=512|2097152|16|134217728;fg_scanout_double_requested=1;fg_scanout_triple_requested=0;memset(presentL2,0,sizeof presentL2);presentL2[128+3]=0x41000|3;
  assert(fg_abi_init() && fg_abi_pk);assert(!fuxi_gpu_abi_map(va,source,bytes,3,0,2));
  assert(!SUB() && f==1);RET(1);ticks=1000;
  /* Copy release: job N rings, job N+1 parks (no wait hint, no refusal), the copy's words go first, then the parked frame. */
  fuxi_gpu_present_writer(7,1);fuxi_gpu_present_due(&s7,1);
  assert(!SUB() && f==2 && !fg_pk_n);
  unsigned w=g3_wptr,db=doorbells;assert(!SUB() && f==3 && fg_pk_n && g3_wptr==w && doorbells==db && fg_abi_seq==3 && !fg_cf_stats[0] && fg_abi_inflight()==2);
  uint32_t frame[FG_ABI_FRAME_WORDS];unsigned fn=fg_pk_n;memcpy(frame,fg_pk_words,fn*4);
  assert(POLL(2)==1 && POLL(3)==1 && fg_pk_n); /* not due to leave yet: the writer fence is unwritten */
  g3_emit(0x70000001u);g3_emit(0xC0FFEEu);w=g3_wptr; /* the copy */
  assert(!fg_pk_flush(FG_PK_COPY) && !fg_pk_n && g3_wptr==((w+fn)&8191) && doorbells==db+1);
  for (unsigned i=0;i<fn;i++) assert(ring[(w+i)&8191]==frame[i]);
  assert(!fg_pk_flush(FG_PK_COPY) && g3_wptr==((w+fn)&8191)); /* nothing parked: no-op */
  fuxi_gpu_present_due(NULL,0);RET(3);assert(fg_abi_completed==3 && !fg_client_busy);
  /* Idle release: the writer job's fence is written and no copy follows within FG_PK_IDLE_US; the generation then stops parking. */
  fuxi_gpu_present_writer(8,1);fuxi_gpu_present_due(&s8,1);assert(!SUB() && f==4 && !fg_pk_n);
  w=g3_wptr;assert(!SUB() && f==5 && fg_pk_n && g3_wptr==w);
  g_wr(G3_FENCE_PA,FG_ABI_FENCE_OFF+4,4);assert(POLL(5)==1 && fg_pk_n && fg_abi_completed==4);
  ticks+=FG_PK_IDLE_US-1;assert(POLL(5)==1 && fg_pk_n);ticks+=1;assert(POLL(5)==1 && !fg_pk_n && g3_wptr!=w);
  assert(!SUB() && f==6 && !fg_pk_n);RET(6); /* expired generation */
  /* Cap: the writer job still runs two periods after parking. */
  fuxi_gpu_present_writer(9,1);fuxi_gpu_present_due(&s9,1);assert(!SUB() && f==7);assert(!SUB() && f==8 && fg_pk_n);
  ticks+=2*fg_cf_bound()-1;assert(POLL(8)==1 && fg_pk_n);ticks+=1;assert(POLL(8)==1 && !fg_pk_n);RET(8);
  /* Next submit: the parked job rings first, the new one parks in its place; the due surface leaving the set (dropped swap) rings it. */
  fuxi_gpu_present_writer(10,1);fuxi_gpu_present_due(&s10,1);assert(!SUB() && f==9);assert(!SUB() && f==10 && fg_pk_n);
  w=g3_wptr;fn=fg_pk_n;assert(!SUB() && f==11 && fg_pk_n && g3_wptr==((w+fn)&8191));
  fuxi_gpu_present_due(NULL,0);w=g3_wptr;assert(POLL(11)==1 && !fg_pk_n && g3_wptr!=w);RET(11);
  /* Pending (AvoidSync) lock: nothing parks. */
  fuxi_gpu_present_writer(11,0);fuxi_gpu_present_due(&s11,1);assert(!SUB() && f==12 && !fg_pk_n);assert(!SUB() && f==13 && !fg_pk_n);RET(13);
  /* Counters: parked 5 (copy 1, idle 1, cap 1, submit 1, not_due 1), one parked after its writer job was already done: none here. */
  assert(fg_pk_stats[FG_PK_REASONS]==5 && fg_pk_stats[FG_PK_COPY]==1 && fg_pk_stats[FG_PK_IDLE]==1 && fg_pk_stats[FG_PK_CAP]==1 && fg_pk_stats[FG_PK_SUBMIT]==1 && fg_pk_stats[FG_PK_NOTDUE]==1);
  assert(fg_pk_idle_n==1 && fg_pk_idle_max==FG_PK_IDLE_US && !fg_pk_stats[FG_PK_REASONS+2]);
  fg_cf_log();assert(!fg_pk_stats[FG_PK_REASONS] && !fg_pk_idle_n);
  /* Fault: a parked job is discarded, never rung; its fence fails with the client. */
  fuxi_gpu_present_writer(7,1);fuxi_gpu_present_due(&s7,1);assert(!SUB() && f==14);assert(!SUB() && f==15 && fg_pk_n);
  w=g3_wptr;(void)fg_abi_fault("test");assert(!fg_pk_n && g3_wptr==w && POLL(15)<0 && SUB()<0);
  #undef RET
  #undef SUB
  #undef POLL
  fuxi_gpu_present_due(NULL,0);fg_abi_inline=fg_abi_cf=fg_abi_pk=0;fg_scanout_double_requested=0;ticks=0;
  puts("PASS: park (bit 27, needs 16, inline only): the refused job is fenced but not rung and returns at once; rung right behind the copy, or on writer-done idle 1.5 ms (generation expires), cap of two periods, next submit (order kept), due surface leaving; pending lock never parks; fault discards; counters and log reset");
 }
 /* Legacy opt-in (fuxigpuncimport 256): a map waits for an idle GPU again. */
 seed();ncimportArg=256;assert(fg_abi_init() && fg_abi_mapwait);
 { uint64_t sz[1]={4096},ad[1]={va},fn=0;
   assert(!fuxi_gpu_abi_map(va,source,bytes,3,0,2) && !fuxi_gpu_abi_submit(ad,sz,1,&fn) && fg_client_busy);
   assert(fuxi_gpu_abi_map(0x30000000,source,4096,3,0,2)==1 && !*fg_abi_leaf(0x30000000) && !fg_abi_used_get(fg_abi_pg(0x30000000)));
   complete();assert(!fuxi_gpu_abi_map(0x30000000,source,4096,3,0,2) && !fg_client_busy); }
 /* Default: a faulted job seen by the map's own observation is still isolated, and the map then refuses. */
 reset();
 { uint64_t sz[1]={4096},ad[1]={va},fn=0;
   assert(!fuxi_gpu_abi_map(va,source,bytes,3,0,2) && !fuxi_gpu_abi_submit(ad,sz,1,&fn));
   fault=1;regs[0x928]=1;assert(fuxi_gpu_abi_map(0x30000000,source,4096,3,0,2)==-1 && state==2 && !fg_client_busy && !*fg_abi_leaf(0x30000000)); }
 puts("PASS: map while a job runs (default) vs idle wait (256), word-wise overlap bitmap, cached overlap mirror (exact 4K overlap refusal, no partial publish, unmap clears, init clears)");
 /* Ranged invalidate: default keeps the whole-ASID TLBI; value 128 does one TLBIVAL per 4K page plus sync, up to 512 pages. */
 reset();assert(!fuxi_gpu_abi_map(va,source,32768,3,0,2));tlbs=0;
 assert(!fuxi_gpu_abi_unmap(va,32768) && tlbs==1 && !tlbiWrites);
 seed();ncimportArg=128;assert(fg_abi_init());assert(!fuxi_gpu_abi_map(va,source,32768,3,0,2));tlbs=0;
 assert(!fuxi_gpu_abi_unmap(va,32768) && !tlbs && tlbiWrites==8);
 for(unsigned i=0;i<8;i++) assert(tlbiVals[i]==(((va>>12)+i)|((uint64_t)g3_asid<<48)));
 assert(sregs[(g_cb(g3_bank)+0x7f0)/4]==0 && !*fg_abi_leaf(va));
 assert(!fuxi_gpu_abi_map(va,source,512*4096,3,0,2));tlbiWrites=0;
 assert(!fuxi_gpu_abi_unmap(va,512*4096) && !tlbs && tlbiWrites==512);
 assert(!fuxi_gpu_abi_map(va,source,513*4096,3,0,2));tlbiWrites=0;
 assert(!fuxi_gpu_abi_unmap(va,513*4096) && tlbs==1 && !tlbiWrites); /* above the page limit: whole ASID */
 /* A failed ranged invalidate or sync is a fault, never a silent skip. */
 assert(!fuxi_gpu_abi_map(va,source,16384,3,0,2));failTlbi=1;
 assert(fuxi_gpu_abi_unmap(va,16384)==-1 && fg_abi_poisoned && state==2);
 seed();ncimportArg=128;assert(fg_abi_init());assert(!fuxi_gpu_abi_map(va,source,16384,3,0,2));
 sregs[(g_cb(g3_bank)+0x7f4)/4]=1;delays=0;
 assert(fuxi_gpu_abi_unmap(va,16384)==-1 && fg_abi_poisoned && delays==1000); /* TLBSYNC never completes */
 puts("PASS: ranged TLBIVAL on small unmap (VA|ASID format, sync, 512-page limit, ASID fallback) and fail-closed on write/sync failure");
 /* Coherent-import self-test (value 64): only a fully matching run skips import cache maintenance. */
 for(unsigned mode=1;mode<=6;mode++) {
  seed();ncimportArg=64;assert(fg_abi_init());assert(fg_abi_cohtest && !fg_abi_ncimport && !fg_abi_coh);
  assert(!fuxi_gpu_abi_map(va-0x100000,source,4096,3,0,2) && !fuxi_gpu_abi_unmap(va-0x100000,4096)); /* no probe before an import */
  assert(!fg_abi_coh && !delays && !doorbells);
  cohMode=mode;
  int s=fuxi_gpu_abi_map_import(va,source,16384,3);
  assert(s==((mode==3||mode==4)?-1:0));
  assert(fg_abi_coh==((mode==1||mode==6)?1u:2u) && doorbells==((mode==1||mode==3||mode==4)?1u:2u) && fg_abi_coh_wb==(mode==6));
  assert(!strstr(lastLog,"retry") && strstr(lastLog,"coherent-import"));
  assert(cacheClean==((mode==1||mode==3||mode==4)?256u:512u) && cacheInvalidate==0); /* only the probe page's own cleanup */
  assert(fg_abi_cleaned==0); /* not accounted as import traffic */
  if(mode<3) assert(strstr(lastLog,mode==1?"pass cpu_to_gpu 8/8 gpu_to_cpu 8/8":"stale cpu_to_gpu 0/8 gpu_to_cpu 0/8"));
  if(mode==5) assert(strstr(lastLog,"stale cpu_to_gpu 7/8 gpu_to_cpu 8/8") && strstr(lastLog,"(wb-ish)"));
  if(mode==6) assert(strstr(lastLog,"coherent-import(wb-ish) pass cpu_to_gpu 8/8 gpu_to_cpu 8/8") && strstr(lastLog,"write-back inner-shareable"));
  if(mode==3||mode==4) {assert(state==2 && fg_abi_poisoned && strstr(lastLog,"self-test fault"));continue;}
  for(unsigned i=0;i<4;i++) assert(!*fg_abi_leaf(FG_ABI_COH_IOVA+4096*i) && !fg_abi_used_get(fg_abi_pg(FG_ABI_COH_IOVA+4096*i)));
  assert(!*fg_abi_leaf(FG_ABI_COH_IOVA+0x4000) && fg_abi_imports[0].iova==va && fg_abi_imports[0].source==source);
  cacheClean=0;cohMode=0;
  assert(!fuxi_gpu_abi_submit(addresses,sizes,1,&fence));
  complete();assert(!fuxi_gpu_abi_poll(fence));
  if(mode==6) {uint64_t lf=*fg_abi_leaf(va);assert((lf&(1u<<2)) && ((lf>>8)&3)==3);}
  else if(mode>1) {uint64_t lf=*fg_abi_leaf(va);assert(!(lf&(1u<<2)) && ((lf>>8)&3)==2);}
  if(mode==1||mode==6) assert(!cacheClean && !cacheInvalidate && fg_abi_skipped==16384 && !fg_abi_cleaned && !fg_abi_invalidated);
  else assert(cacheClean==256 && cacheInvalidate==256 && fg_abi_skipped==0);
  unsigned before=doorbells;
  assert(!fuxi_gpu_abi_map_import(va+0x10000,source+65536,16384,1) && doorbells==before); /* verdict is final, no re-probe */
  assert(!fuxi_gpu_abi_unmap(va,16384) && !fuxi_gpu_abi_unmap(va+0x10000,16384));
 }
 /* Inconclusive (GPU busy) runs retry, at most three times, then stay on the safe path. */
 seed();ncimportArg=64;assert(fg_abi_init());fg_busy=1;
 for(unsigned i=0;i<3;i++) {
  assert(!fuxi_gpu_abi_map_import(va+0x10000*i,source,16384,3));
  assert(fg_abi_coh==(i==2?2u:0u) && !doorbells && strstr(lastLog,"busy") && !!strstr(lastLog,"retry")==(i<2));
 }
 assert(!fuxi_gpu_abi_map_import(va+0x30000,source,16384,3) && fg_abi_coh==2);
 /* An occupied probe window (client mapped it) is inconclusive and leaves the client's mapping intact. */
 seed();ncimportArg=64;assert(fg_abi_init());
 assert(!fuxi_gpu_abi_map(FG_ABI_COH_IOVA+0x1000,source,4096,3,0,2));
 assert(!fuxi_gpu_abi_map_import(va,source,16384,3) && !fg_abi_coh && fg_abi_used_get(fg_abi_pg(FG_ABI_COH_IOVA+0x1000)) && !fg_abi_used_get(fg_abi_pg(FG_ABI_COH_IOVA)));
 /* With value 1 as well: a proven mapping needs no map-time clean either; an unproven one still gets it. */
 for(unsigned mode=1;mode<=2;mode++) {
  seed();ncimportArg=65;assert(fg_abi_init());cohMode=mode;
  assert(!fuxi_gpu_abi_map_import(va,source,16384,3) && cacheClean==(mode==1?256u:768u));cohMode=0;
 }
 puts("PASS: coherent self-test (pass skips maintenance; stale/partial/hang/fault/busy/occupied stay on the clean+invalidate path; probe window left unmapped)");
 /* Cached-coherent BOs (bit 29, needs 64): on only after the wb-ish variant passed; latched; never probes without both bits. */
 for(unsigned arg=0;arg<3;arg++) {
  seed();ncimportArg=arg==0?64u:arg==1?536870912u:0u;assert(fg_abi_init());cohMode=6;
  assert(fg_abi_cbo==(arg==1) && fuxi_gpu_abi_cached_coherent(1)==0 && !doorbells && !fg_abi_coh);
  assert(strstr(lastLog,arg==1?"cached-coherent BOs disabled reason=no-selftest-bit64":"cached-coherent BOs disabled reason=bit29-off"));
  unsigned lines=logLines;assert(fuxi_gpu_abi_cached_coherent(1)==0 && logLines==lines); /* one line per reason */
  cohMode=0;
 }
 for(unsigned mode=1;mode<=6;mode++) {
  if(mode==3||mode==4) continue;
  seed();ncimportArg=536870912u|64u;assert(fg_abi_init() && fg_abi_cbo && fg_abi_cohtest);
  assert(fuxi_gpu_abi_cached_coherent(0)==-1 && !doorbells && strstr(lastLog,"cached-coherent BOs disabled reason=selftest-pending"));
  cohMode=mode;
  int on=fuxi_gpu_abi_cached_coherent(1);
  assert(on==(mode==6) && fg_abi_coh==(mode==1||mode==6?1u:2u) && fg_abi_coh_wb==(mode==6));
  assert(strstr(lastLog,mode==6?"cached-coherent BOs enabled reason=selftest-wb-ish-pass":mode==1?"cached-coherent BOs disabled reason=selftest-not-wb":"cached-coherent BOs disabled reason=selftest-refused"));
  unsigned before=doorbells;cohMode=0;
  assert(fuxi_gpu_abi_cached_coherent(1)==on && fuxi_gpu_abi_cached_coherent(0)==on && doorbells==before); /* verdict final, no re-probe */
  assert(!fuxi_gpu_abi_map_import(va,source,16384,3) && doorbells==before);
  if(mode==6) {uint64_t lf=*fg_abi_leaf(va);assert((lf&(1u<<2)) && ((lf>>8)&3)==3);
   /* A cached BO maps through the ordinary allocated-BO path with the import's proven attributes. */
   assert(!fuxi_gpu_abi_map(va+0x10000,source,16384,3,1,3));lf=*fg_abi_leaf(va+0x10000);assert((lf&(1u<<2)) && ((lf>>8)&3)==3);
   cacheClean=cacheInvalidate=0;assert(!fuxi_gpu_abi_submit(addresses,sizes,1,&fence));complete();assert(!fuxi_gpu_abi_poll(fence));
   fg_abi_poisoned=1;assert(fuxi_gpu_abi_cached_coherent(0)==1);fg_abi_poisoned=0; /* latched */
   assert(!fuxi_gpu_abi_unmap(va+0x10000,16384));}
  assert(!fuxi_gpu_abi_unmap(va,16384));
 }
 /* GPU busy at GetDeviceInfo: inconclusive (-1) until the self-test's third try, then off for the boot. */
 seed();ncimportArg=536870912u|64u;assert(fg_abi_init());fg_busy=1;
 assert(fuxi_gpu_abi_cached_coherent(1)==-1 && fuxi_gpu_abi_cached_coherent(1)==-1 && fuxi_gpu_abi_cached_coherent(1)==0 && fg_abi_coh==2 && !doorbells);
 assert(strstr(lastLog,"cached-coherent BOs disabled reason=selftest-refused"));fg_busy=0;
 puts("PASS: cached-coherent BOs (bit 29 with 64): enabled only after the wb-ish self-test pass, latched, one log line per reason, no probe without both bits");


 /* One 128-IB frame is one submission: a single prologue, 128 IB packets, one doorbell, across the ring wrap. */
 {
  reset();
  uint64_t many[129],lens[129];for(unsigned i=0;i<129;i++) {many[i]=0x20000000+i*4096;lens[i]=4096;}
  unsigned start=8100;g3_wptr=regs[0x806]=regs[0x807]=start;
  assert(fuxi_gpu_abi_submit(many,lens,129,&fence)==-1 && !doorbells && fg_client_busy==0 && g3_wptr==start);
  assert(!fuxi_gpu_abi_submit(many,lens,128,&fence) && fence==1 && doorbells==1);
  assert(regs[0x807]==((start+FG_ABI_FRAME_WORDS)&8191) && g3_wptr==regs[0x807]);
  unsigned ibs=0,at=0;
  while(at<FG_ABI_FRAME_WORDS) {
   uint32_t h=ring[(start+at)&8191];unsigned type=h>>28,len=type==7?h&0x3fff:h&0x7f;
   if(type==7 && ((h>>16)&0x7f)==0x3f) {assert(len==3 && ring[(start+at+1)&8191]==many[ibs]);ibs++;}
   at+=len+1;
  }
  assert(at==FG_ABI_FRAME_WORDS && ibs==128);
  assert(fg_abi_ibs[0]==1 && fg_abi_ibs[1]==128 && fg_abi_ibs[2]==128 && fg_abi_ibs[3]==1);
  complete();assert(!fuxi_gpu_abi_poll(fence));
  assert(!fuxi_gpu_abi_submit(many,lens,3,&fence) && fg_abi_ibs[0]==2 && fg_abi_ibs[1]==131 && fg_abi_ibs[2]==128 && fg_abi_ibs[3]==1);
  uint32_t a[4],b[4],c[8];fuxi_gpu_abi_sched_stats(a,b,c);assert(a[0]==2 && a[1]==131 && b[0]==2 && !gmuReads);
  {uint64_t sn[8],lm,kib[2];uint32_t why[5],cn[2];fuxi_gpu_abi_submit_split(sn,why,cn,&lm,kib);assert(cn[1]>=2 && cn[0]>=cn[1] && fg_abi_ss_ok==0 && !fg_abi_ss_att);fuxi_gpu_abi_submit_split(sn,why,cn,&lm,kib);assert(!cn[0] && !sn[0] && !lm);}
  puts("PASS: 128-IB frame is one submission across the ring wrap, 129 refused before any doorbell, IB counters");
 }
 /* Fenced WPTR (fuxigpuncimport bit 1): off = one plain write and no GMU access; on = read the AHB fence status and write again while dropped. */
 for(unsigned mode=0;mode<5;mode++) {
  seed();ncimportArg=mode==0?0:2;assert(fg_abi_init());assert(fg_wptr_fence==(mode!=0));
  fenceDrops=mode==2?1:mode==3?3:0;fenceStuck=mode==4;
  assert(!fuxi_gpu_abi_submit(addresses,sizes,1,&fence));
  const unsigned want[5][3]={{0,0,0},{0,0,0},{1,1,0},{1,3,0},{0,200,1}}; /* dropped then acknowledged, repeated writes, still dropped */
  assert(doorbells==1+want[mode][1] && fg_wptr_stats[0]==1);
  assert(fg_wptr_stats[1]==want[mode][0] && fg_wptr_stats[2]==want[mode][1] && fg_wptr_stats[3]==want[mode][2]);
  assert(mode ? gmuReads>0 : (!gmuReads && !gmuWrites));
  assert(fg_client_busy && state==1 && !hangLines);
 }
 {
  seed();ncimportArg=2;assert(fg_abi_init());readFault=0;
  /* a failing GMU register read is a fault, not a retry */
  fg_access_failed=0;
  uint64_t one[1]={va},len[1]={4096};
  fenceDrops=0;assert(!fuxi_gpu_abi_submit(one,len,1,&fence));
  puts("PASS: fenced WPTR off/on, dropped write repeated until acknowledged, 200-try bound, GMU window offsets");
 }
 /* Stall recovery (fuxigpuncimport bit 2): 30 ms snapshot + TLB drop + WPTR kick, 60 ms bus drain, completion accounting; off = nothing. */
 for(unsigned on=0;on<2;on++) {
  seed();ncimportArg=on?4:0;assert(fg_abi_init());assert(fg_abi_unstick==on);
  (void)fuxi_gpu_abi_unmap(0x20000000,4096);assert(!fuxi_gpu_abi_map(0x20000000,IBSRC,4096,3,0,2));
  assert(fg_abi_map_at(0x20000800)->source==IBSRC && !fg_abi_map_at(0x20001000) && fg_abi_maps[0].seq==1);
  for(unsigned i=0;i<1024;i++) {uint32_t x=0x1000+i;memcpy(ibMemory+4*i,&x,4);}
  uint64_t ib[1]={0x20000000},ln[1]={4096};
  regs[0x3c46]=3;regs[0x928]=0x20000000;regs[0x929]=0;regs[0x943]=0x40;regs[0x92a]=0x30;regs[0xa6d]=0x20000200;regs[0xa71]=0x20000400;
  regs[0xa72]=0;regs[0x210]=0;
  assert(!fuxi_gpu_abi_submit(ib,ln,1,&fence));
  unsigned d0=doorbells,t0=tlbs;
  ticks=29000;fg_abi_watch(NULL,NULL);assert(doorbells==d0 && !stallLines && !fg_abi_stage);
  ticks=30000;fg_abi_watch(NULL,NULL);
  if(!on) {assert(doorbells==d0 && tlbs==t0 && !stallLines && !fg_abi_stall_stats[0] && !regs[0x3c45]);}
  else {
   assert(doorbells==d0+1 && tlbs==t0+1 && fg_abi_stage==1 && fg_abi_stall_stats[0]==1 && fg_abi_stall_stats[1]==1 && fg_abi_stall_stats[7]==1);
   assert(regs[0x807]==g3_wptr); /* the same value again */
   assert(stallLines>=7 && strstr(stallLog[0],"pending 30 ms") && strstr(stallLog[1],"30ms seq 1"));
   assert(strstr(stallLog[1],"rptr 0 wptr") && strstr(stallLog[2],"ib1 00000000:20000000 rem 30 init 40"));
   assert(strstr(stallLog[2],"bv rptr 0 ib1 lo 20000200 rem 0 ib2 00000000:20000400"));
   assert(strstr(stallLog[3],"smmu fsr"));
   /* reached = 0x40-0x30 = 16 words in: the last fetched word is 15, the window starts 12 words before it */
   assert(strstr(stallLog[4],"BR-IB1 at 0x2000003c from 0x2000000c map 0x20000000 seq 1/1 unread 0: 00001003 00001004"));
   assert(strstr(stallLog[5],"BV-IB1 at 0x20000200 from 0x200001d0") && strstr(stallLog[5],"unread 0: 00001074"));
   assert(strstr(stallLog[6],"BV-IB2 at 0x20000400 from 0x200003d0"));
   unsigned lines=stallLines;ticks=45000;fg_abi_watch(NULL,NULL);assert(doorbells==d0+1 && tlbs==t0+1 && stallLines==lines && fg_abi_stage==1);
   ticks=60000;fg_abi_watch(NULL,NULL);
   assert(fg_abi_stage==2 && fg_abi_stall_stats[2]==1 && fg_abi_stall_stats[3]==1 && fg_abi_stall_stats[4]==1 && !regs[0x3c45]);
   assert(state==1 && drains==0 && fg_client_busy && !hangLines);
   ticks=70000;fg_abi_watch(NULL,NULL);assert(fg_abi_stage==2 && doorbells==d0+1 && fg_abi_stall_stats[2]==1);
   complete();fg_abi_watch(NULL,NULL);
   assert(!fg_client_busy && fg_abi_stage==0 && fg_abi_stall_stats[6]==1 && fg_abi_stall_stats[5]==0 && strstr(stallLog[stallLines-1],"completed after bus drain"));
   /* A job that completes after only the kick. */
   assert(!fuxi_gpu_abi_submit(ib,ln,1,&fence));ticks+=30000;fg_abi_watch(NULL,NULL);assert(fg_abi_stage==1);
   complete();fg_abi_watch(NULL,NULL);assert(fg_abi_stall_stats[5]==1 && fg_abi_stage==0 && fg_abi_stall_stats[0]==2);
   /* A job past the 100 ms deadline is still fatal; its hang report gets the same CP and IB snapshot. */
   assert(!fuxi_gpu_abi_submit(ib,ln,1,&fence));regs[0x807]=g3_wptr;regs[0x806]=g3_wptr-1;
   ticks+=100001;stallLines=0;fg_abi_watch(NULL,NULL);
   assert(state==2 && hangLines==16 && drains==1 && fg_abi_stage==0 && strstr(stallLog[0],"hang seq"));
   assert(strstr(stallLog[3],"BR-IB1 at 0x2000003c"));
  }
 }
 /* IB words come from the kernel mapping and are bounded by the mapped range; unmapped addresses and faults are reported, not read. */
 reset();(void)fuxi_gpu_abi_unmap(0x20000000,4096);(void)fuxi_gpu_abi_unmap(0x20002000,8192);
 assert(!fuxi_gpu_abi_map(0x20000000,IBSRC,4096,3,0,2) && !fuxi_gpu_abi_map(0x20002000,0x200000,8192,3,0,2));
 stallLines=0;regs[0x928]=0x20000fc0;regs[0x943]=0x10;regs[0x92a]=0;regs[0xa6d]=0x20002000;regs[0xa71]=0x1000;
 fg_abi_snapshot("t");
 assert(strstr(stallLog[3],"BR-IB1 at 0x20000ffc from 0x20000fcc") && strstr(stallLog[3],"unread 3"));
 assert(strstr(stallLog[4],"BV-IB1 at 0x20002000 from 0x20002000") && strstr(stallLog[4],"unread 16: deadbeef"));
 assert(strstr(stallLog[5],"BV-IB2 IB 0x1000 not in a mapped range"));
 stallLines=0;regs[0x928]=0x20000ffc;regs[0x943]=0;regs[0x92a]=0;fg_abi_snapshot("t");
 assert(strstr(stallLog[3],"at 0x20000ffc from 0x20000fcc") && strstr(stallLog[3],"unread 3"));
 assert(!fuxi_gpu_abi_unmap(0x20000000,4096) && !fg_abi_map_at(0x20000000) && fg_abi_map_at(0x20002000));
 puts("PASS: stall recovery off/on (30 ms snapshot/TLB/kick, 60 ms drain, completion accounting), hang snapshot, IB word reads bounded by mapped ranges");

 puts("PASS: actual backend root isolation, 16K/4K scattered leaves, VA reuse/TLBI, present exclusion, late retirement, independent watchdog/fault/drain/quarantine");
}
'''


def main():
    subprocess.run(['python3', str(ROOT/'tools/sync-fuxi-adreno-shared.py'), '--check'], check=True)
    with tempfile.TemporaryDirectory() as temp:
        out = Path(temp)
        wire = out/'wire.cpp'
        client=(HEADERS/'FuxiAdrenoUserClient.cpp').read_text()
        query=client[client.index(' IOReturn run('):client.index('  if (method==NativeAdrenoMethodWaitFence)')]
        query=query.replace(' IOReturn run(', 'static int deviceQuery(')+' (void)in;return -1;\n}\n'
        stubs=('static constexpr int kIOReturnSuccess=0;\nstatic int ccSeq[3]={0,0,0};static unsigned ccCalls,sleeps;\n'
               'static int fuxi_gpu_abi_cached_coherent(int probe){assert(probe==1 && ccCalls<3);return ccSeq[ccCalls++];}\n'
               'static void IOSleep(unsigned ms){assert(ms==2);sleeps++;}\n')
        wire.write_text(WIRE.replace('int main() {', stubs+query+'\nint main() {'))
        backend = (HEADERS/'fuxi_gpu_abi_backend.inc').read_text()
        backend = re.sub(r'__asm__ volatile\("dsb sy" ::: "memory"\);', '(void)0;', backend)
        backend = backend.replace('__asm__ volatile("dc ivac, %0" :: "r"(p) : "memory");', 'host_cache(p,1);')
        backend = backend.replace('__asm__ volatile("dc civac, %0" :: "r"(p) : "memory");', 'host_cache(p,0);')
        adapter = (HEADERS/'fuxi_adreno.c').read_text()
        doorbell = adapter[adapter.index('#define FG_GMU_AHB_FENCE_STATUS'):adapter.index('\n}\n', adapter.index('static int fg_ring_doorbell'))+3]
        doorbell = re.sub(r'__asm__ volatile\("dsb sy" ::: "memory"\);', '(void)0;', doorbell)
        model = out/'backend.c'
        model.write_text(BACKEND_PREFIX.replace('//FENCED_DOORBELL//', doorbell) + backend + BACKEND_TEST)
        for compiler, source in [('clang++', wire), ('clang', model)]:
            binary = source.with_suffix('')
            subprocess.run([compiler, '-std=c++17' if compiler=='clang++' else '-std=gnu11',
                '-O1', '-g', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined',
                '-I', str(HEADERS), str(source), '-o', str(binary)], check=True)
            subprocess.run([str(binary)], check=True)
        # Apply 0098 to baseline snapshots with a real patch engine, checking
        # exact native source bytes (no Apple source/binary fixture in git).
        import importlib.util
        spec = importlib.util.spec_from_file_location('metal_sync', ROOT/'tools/sync-fuxi-metal.py')
        sync = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sync)
        tree=out/'patchtree'
        for name in ('gpu.c','gpu-a740.h','gpu-zap.h','gpu-handoff.h','fuxi_adreno.c','fuxi_adreno.h'):
            path=tree/'bsd/dev'/name; path.parent.mkdir(parents=True,exist_ok=True);path.write_text(sync.baseline(name))
        conf=tree/'iokit/conf/files.arm64';conf.parent.mkdir(parents=True,exist_ok=True)
        conf.write_text(sync.CONFIG_BASE)
        conf=tree/'osfmk/conf/files.arm64';conf.parent.mkdir(parents=True,exist_ok=True)
        conf.write_text(sync.OSFMK_CONFIG_BASE)
        subprocess.run(['patch','-s','-p1','--fuzz=0','-i', str(ROOT/'xnu/patches/0098-fuxi-native-adreno-abi.patch')],cwd=tree,check=True)
        delta=(ROOT/'xnu/patches/0100-sweet-execswap-gpu-abi-trace.patch').read_text()
        delta=delta[delta.index('--- a/bsd/kern/sweet_execswap.h'):]
        subprocess.run(['patch','-s','-p1','--fuzz=0'],input=delta,text=True,cwd=tree,check=True)
        for path, name in sync.FILES.items():
            assert (tree/path).read_bytes()==(HEADERS/name).read_bytes(),path
        for name in ('gpu.c','fuxi_adreno.c','fuxi_adreno.h'):
            folder=HEADERS if name.startswith('fuxi_') else ROOT/'shim/fuxi-probe'
            assert (tree/'bsd/dev'/name).read_bytes()==(folder/name).read_bytes(),name
        print('PASS: clean 0098 + 0100 ABI/helper patch application and canonical source bytes')
        subprocess.run(['patch','-s','-p1','--fuzz=0','-i', str(ROOT/'xnu/patches/0102-fuxi-gpu-backboardd-open.patch')],cwd=tree,check=True)
        admitted=(tree/'iokit/Kernel/arm/FuxiAdrenoUserClient.cpp').read_text()
        assert '!backboardClient(task)' in admitted and '!connected && fuxi_gpu_abi_available()' in admitted
        print('PASS: unchanged 0102 admission patch applies after K1; single-connection guard retained')


if __name__ == '__main__':
    main()
