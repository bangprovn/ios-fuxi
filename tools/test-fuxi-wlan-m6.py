#!/usr/bin/env python3
"""Exercise the actual M6 C block with fake MMIO, VFS and physical memory.

No hardware/USB access. Synthetic ELF bytes are generated in memory. This
checks sequence/containment and ABI bounds, not real SMMU or firmware behavior.
"""
from pathlib import Path
import importlib.util
import os
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PREAMBLE = r'''
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <assert.h>
#include <sys/types.h>
#include <stdarg.h>
static char logbuf[131072];
static size_t loglen;
static void test_printf(const char *fmt, ...) {
    va_list ap; va_start(ap,fmt);
    int n=vsnprintf(logbuf+loglen,sizeof(logbuf)-loglen,fmt,ap);
    va_end(ap); assert(n>=0 && (size_t)n<sizeof(logbuf)-loglen); loglen+=n;
}
#define printf test_printf
typedef uintptr_t vm_offset_t;
typedef size_t vm_size_t;
typedef unsigned ppnum_t;
typedef int kern_return_t;
typedef void *vnode_t;
typedef void *vfs_context_t;
typedef void *event_t;
#define PAGE_SHIFT 14
#define PAGE_SIZE (1u << PAGE_SHIFT)
#define PAGE_MASK (PAGE_SIZE - 1)
#define round_page(x) (((x) + PAGE_MASK) & ~(size_t)PAGE_MASK)
#define ptoa_64(x) ((uint64_t)(x) << PAGE_SHIFT)
#define MIN(a,b) ((a)<(b)?(a):(b))
#define kernel_map 0
#define kernel_pmap 0
#define KERN_SUCCESS 0
#define KMA_DATA 1
#define KMA_ZERO 2
#define KMA_NOPAGEWAIT 4
#define VM_KERN_MEMORY_IOKIT 0
#define VM_WIMG_DEFAULT 0
#define VM_WIMG_WCOMB 1
#define TRUE 1
#define THREAD_UNINT 0
#define THREAD_CONTINUE_NULL 0
#define NSEC_PER_MSEC 1000000
#define FREAD 1
#define O_NOFOLLOW 2
#define O_NONBLOCK 4
#define VREG 1
#define UIO_READ 0
#define UIO_SYSSPACE 0
#define WL_BAR0 0x60400000ull
#define WL_EPCFG 0x60100000ull
#define WL_DBI 0x60000000ull
#define WL_PARF 0x1c00000ull
#define WL_PHY 0x1c06000ull
static unsigned wl3_faults;
static int wl3_step(unsigned s) { abort(); }
static int wl3_phy_init(unsigned us) { abort(); }
static int wl4_step(unsigned s) { abort(); }
#define TS_TLMM 0xf100000ull
static uint32_t wl4_report[64], wl3_report[128], fw_faults;
static unsigned rh_send(unsigned t, unsigned addr, unsigned data);
static unsigned wl_vrm(unsigned addr, unsigned mv) { return rh_send(0,addr,mv) || rh_send(0,addr+4,1); }
static unsigned rh_send(unsigned t, unsigned addr, unsigned data) { return 0; }
static unsigned wl_icc_acks;
struct reg { uint64_t pa; uint32_t v; };
struct ev { char kind; uint64_t pa; uint32_t v; };
static struct reg regs[4096];
static struct ev events[8192];
static unsigned nr, ne, sleeps, frees, allocations;
static int old_iova_arg;
static bool old_iova_present;
static int clock_arg, iboff_arg;
static int aspm_arg, stop22_arg, keep_arg, qrtr_arg, mlo_arg;
static bool mlo_present;
static const char *probe_arg, *pre_arg, *bhi_arg, *vec_arg, *fw_arg, *cnss_arg, *seq_arg;
static unsigned cache_attrs[32][1024];
static bool cache_drop;
static unsigned clock_delays;
static bool track_reads;
static uint64_t reads[4096];
static unsigned nreads;
static void fw_delay(unsigned us) { clock_delays+=us; }
static bool PE_parse_boot_argn(const char *name, void *out, int size) {
    if(!strcmp(name,"fuxiwlanrails")) { assert(size==sizeof(int)); return false; }
    if(!strcmp(name,"fuxiwlanp18")) { assert(size==sizeof(int)); return false; }
    if(!strcmp(name,"fuxiwlanmlo")) { if(mlo_present)memcpy(out,&mlo_arg,size); return mlo_present; }
    if(!strcmp(name,"fuxiwlanw15")) { assert(size==sizeof(int)); return false; }
    if(!strcmp(name,"fuxiwlanw16")) { assert(size==sizeof(int)); return false; }
    if(!strcmp(name,"fuxiwlanprobe")) {
        if(probe_arg) { strncpy(out,probe_arg,size); return true; }
        return false;
    }
    const char *arg = !strcmp(name,"fuxiwlanpre") ? pre_arg :
                      !strcmp(name,"fuxiwlanbhi") ? bhi_arg :
                      !strcmp(name,"fuxiwlanfw") ? fw_arg :
                      !strcmp(name,"fuxiwlancnss") ? cnss_arg :
                      !strcmp(name,"fuxiwlanseq") ? seq_arg :
                      !strcmp(name,"fuxiwlanvec") ? vec_arg : NULL;
    if(!strcmp(name,"fuxiwlanpre") || !strcmp(name,"fuxiwlanbhi") || !strcmp(name,"fuxiwlanvec") || !strcmp(name,"fuxiwlanfw") || !strcmp(name,"fuxiwlancnss") || !strcmp(name,"fuxiwlanseq")) {
        if(arg) { strncpy(out,arg,size); return true; }
        return false;
    }
    assert(size==sizeof(int));
    if(!strcmp(name,"fuxiwlanaspm")) { memcpy(out,&aspm_arg,size); return true; }
    if(!strcmp(name,"fuxiwlanclk")) { memcpy(out,&clock_arg,size); return true; }
    if(!strcmp(name,"fuxiwlaniboff")) { memcpy(out,&iboff_arg,size); return true; }
    if(!strcmp(name,"fuxiwlanstop22")) { memcpy(out,&stop22_arg,size); return true; }
    if(!strcmp(name,"fuxiwlankeep")) { memcpy(out,&keep_arg,size); return true; }
    if(!strcmp(name,"fuxiwlanqrtr")) { memcpy(out,&qrtr_arg,size); return true; }
    assert(!strcmp(name,"fuxiwlanoldiova"));
    if(old_iova_present) memcpy(out,&old_iova_arg,size);
    return old_iova_present;
}
/* String API uses the same synthetic argument source, without numeric coercion. */
static bool PE_parse_boot_arg_str(const char *name, char *out, int size) {
    return PE_parse_boot_argn(name, out, size);
}
static bool progress = true, fail_bm, fail_perst, no_root;
static bool noncontig, high_memory, fail_alloc, tlb_busy, mismatch, inject_fault;
static uint64_t mismatch_pa, read_fault_pa, corrupt_write_pa;
static uint32_t corrupt_write_mask;
struct read_mask { uint64_t pa; uint32_t rao, raz; };
static struct read_mask read_masks[32];
static unsigned nread_masks;
static bool probe_block[128], probe_steal, probe_change_next;
static int open_error, read_resid;
static unsigned diag_repeat_kind;
static uint8_t fixture[0x80000 + 37];
static uint64_t file_size = sizeof(fixture);
struct alloc { uintptr_t va; size_t size; uint64_t pa; };
static struct alloc allocs[32];
static uint64_t next_pa = 0x8c0000000ull;
static void record(char k, uint64_t pa, uint32_t v) {
    assert(ne < 8192); events[ne++] = (struct ev){k,pa,v};
}
static bool wl_link_refused(uint64_t pa);
static uint32_t wl_prd(uint64_t pa) {
    if (wl_link_refused(pa)) {fw_faults++; return 0;}
    if(track_reads) { assert(nreads<4096); reads[nreads++]=pa; }
    if (read_fault_pa && pa==read_fault_pa) {fw_faults++; return ~0u;}
    uint32_t v=0;
    for (unsigned i=0;i<nr;i++) if(regs[i].pa==pa) {v=regs[i].v; break;}
    if (pa>=WL_BAR0+0x80000 && pa<WL_BAR0+0x100000) {
        uint64_t key=0x200000000ull+((wl_prd(WL_BAR0+0x310c)&63)<<19)+(pa-WL_BAR0-0x80000);
        for(unsigned i=0;i<nr;i++) if(regs[i].pa==key) {v=regs[i].v; break;}
    }
    for (unsigned i=0;i<nread_masks;i++) if(read_masks[i].pa==pa)
        v=(v | read_masks[i].rao) & ~read_masks[i].raz;
    return (tlb_busy && (pa & 0xfff)==0x7f4) ? 1 : v;
}
static void setreg(uint64_t pa, uint32_t v) {
    for (unsigned i=0;i<nr;i++) if(regs[i].pa==pa) {regs[i].v=v; return;}
    assert(nr < 4096); regs[nr++] = (struct reg){pa,v};
}
static void raw_pwr(uint64_t pa, uint32_t v) {
    record('W',pa,v);
    if (pa>=0x15080000ull && pa<0x15100000ull && (pa&0xfff)==0x10) {
        unsigned cb=(pa-0x15080000ull)>>12;
        if (probe_steal) setreg(0x15000c04ull,cb);
        if (probe_change_next) {
            setreg(0x15000c04ull,2); /* next bank becomes referenced */
            setreg(0x15083000ull,1); /* active */
            setreg(0x15085000ull,1); setreg(0x15085024ull,4u<<16); /* ASID4 busy */
        }
        if (probe_block[cb]) return;
    }
    if (pa==corrupt_write_pa) v^=corrupt_write_mask;
    if (fail_bm && pa==WL_EPCFG+4) return;
    if (fail_perst && pa==TS_TLMM+0x1000*94+4) return;
    if (mismatch && pa==mismatch_pa) return;
    if (pa>=0x15080000ull && pa<0x15100000ull && (pa&0xfff)==0x58) {setreg(pa,wl_prd(pa)&~v); return;}
    if (pa>=WL_BAR0+0x80000 && pa<WL_BAR0+0x100000) {
        uint32_t off=((wl_prd(WL_BAR0+0x310c)&63)<<19)+(pa-WL_BAR0-0x80000);
        record('S',off,v); setreg(0x200000000ull+off,v);
    }
    setreg(pa,v);
    if (inject_fault && pa==WL_BAR0+0x118) setreg(0x15081058ull,2);
    if (pa==WL_BAR0+0x38 && v==2) setreg(pa,0);
    if (!progress) return;
    if (pa==WL_BAR0+0x118) {
        setreg(WL_BAR0+0x12c, (2u<<30)|(v&0x3fffffff)); setreg(WL_BAR0+0x128,1); setreg(WL_BAR0+0x48, 0x101);
    }
    if (pa==WL_BAR0+0x38 && v==0x200) {
        setreg(WL_BAR0+0x128,1); setreg(WL_BAR0+0x48,0x200);
    }
    if (pa==WL_BAR0+0x260 && v) {
        setreg(WL_BAR0+0x268,(2u<<30)|(v & 0x3fffffff));
        setreg(WL_BAR0+0x128,2);
    }
}
static void wl_pwr(uint64_t pa, uint32_t v) { if (wl_link_refused(pa)) {fw_faults++; return;} if (!fw_faults) raw_pwr(pa,v); }
static uintptr_t fw_va(uint64_t pa) { return pa; }
static unsigned fuxi_gpu_access(uintptr_t pa, uint64_t *v, unsigned wr, unsigned unused) {
    if(wr) raw_pwr(pa,(uint32_t)*v); else *v=wl_prd(pa);
    return 0;
}
static const char *wl_mhi_ee(uint32_t e) { return "fake"; }
static const char *wl_mhi_state(uint32_t s) { return "fake"; }
static void assert_wait_timeout(event_t e, int intr, unsigned ms, unsigned ns) {
    sleeps+=ms;
    if(ms==10 && diag_repeat_kind) {
        if(diag_repeat_kind==1) { setreg(WL_DBI+0x80,0); setreg(WL_EPCFG,~0u); }
        if(diag_repeat_kind==2) { setreg(WL_BAR0+0x12c,~0u); setreg(WL_BAR0+0x130,0xef0e0003); setreg(WL_BAR0+0x128,0); }
        diag_repeat_kind=0;
    }
}
static void thread_block(int cont) { }
static uint32_t random_value = 0x12345;
static void read_random(void *p, unsigned bytes) { assert(bytes==4); memcpy(p,&random_value,4); }
static uint64_t mach_absolute_time(void) { return 0x12345; }
static void absolutetime_to_nanoseconds(uint64_t t, uint64_t *ns) { *ns=t; }
static int kmem_alloc(int map, uintptr_t *va, size_t size, int flags, int tag) {
    assert(flags & KMA_NOPAGEWAIT);
    if(fail_alloc) return 1;
    *va=(uintptr_t)aligned_alloc(PAGE_SIZE,size); assert(*va);
    memset((void *)*va,0,size);
    allocs[allocations++] = (struct alloc){*va,size,high_memory?0x1000000000ull:next_pa};
    next_pa+=size*2;
    return 0;
}
static ppnum_t pmap_find_phys(int pmap, uintptr_t va) {
    for(unsigned i=0;i<allocations;i++) if(va>=allocs[i].va && va<allocs[i].va+allocs[i].size)
        return (allocs[i].pa+((va-allocs[i].va)&~(uintptr_t)PAGE_MASK)*(noncontig?2:1)) >> PAGE_SHIFT;
    return 0;
}
static unsigned *cache_slot(ppnum_t pn) {
    for(unsigned i=0;i<allocations;i++)
        for(size_t off=0;off<allocs[i].size;off+=PAGE_SIZE)
            if(pmap_find_phys(0,allocs[i].va+off)==pn) return &cache_attrs[i][off/PAGE_SIZE];
    assert(0); return NULL;
}
static unsigned pmap_cache_attributes(ppnum_t pn) { return *cache_slot(pn); }
static void pmap_set_cache_attributes(ppnum_t pn, unsigned attr) {
    record('A',pn,attr); if(!cache_drop || attr==VM_WIMG_DEFAULT) *cache_slot(pn)=attr;
}
static void kmem_free(int map, uintptr_t va, size_t size) {
    record('F',va,0); frees++; free((void *)va);
}
static void flush_dcache64(uint64_t pa, unsigned size, int phys) { record('C',pa,size); }
struct vnode_attr { uint64_t va_data_size; };
#define VATTR_INIT(a) memset((a),0,sizeof(*(a)))
#define VATTR_WANTED(a,f) ((void)0)
#define VATTR_IS_SUPPORTED(a,f) 1
static vfs_context_t vfs_context_kernel(void) { return (void *)1; }
static vnode_t vfs_rootvnode(void) { return no_root?NULL:(void *)1; }
static void vnode_put(vnode_t root) { }
static int vnode_open(const char *p, int mode, int cmode, int flags, vnode_t *vp, vfs_context_t c) {
    assert(mode == (FREAD|O_NOFOLLOW|O_NONBLOCK)); *vp=(void *)1; return open_error;
}
static int vnode_getattr(vnode_t v, struct vnode_attr *a, vfs_context_t c) { a->va_data_size=file_size; return 0; }
static int vnode_vtype(vnode_t v) { return VREG; }
static void *vfs_context_ucred(vfs_context_t c) { return c; }
static void *vfs_context_proc(vfs_context_t c) { return c; }
static int vn_rdwr(int rw, vnode_t v, char *b, int len, off_t off, int seg, int io,
    void *cred, int *resid, void *proc) {
    assert((unsigned)len<=sizeof(fixture)); memcpy(b,fixture,len); *resid=read_resid; return 0;
}
static int vnode_close(vnode_t v, int mode, vfs_context_t c) { return 0; }
'''
TESTS = r'''
static void init(void) {
    nr=ne=sleeps=frees=allocations=0; random_value=0x12345; next_pa=0x8c0000000ull;
    progress=true; fail_bm=fail_perst=no_root=false;
    mismatch_pa=0x15001004ull; read_fault_pa=0;
    high_memory=noncontig=fail_alloc=tlb_busy=mismatch=inject_fault=false; open_error=read_resid=0;
    diag_repeat_kind=0; fw_faults=0; wl_mhi_valid=false; wl_smmu_mismatches=0; loglen=0; logbuf[0]=0;
    memset(wl_cb_unusable,0,sizeof(wl_cb_unusable));
    memset(probe_block,0,sizeof(probe_block));
    probe_steal=probe_change_next=false; corrupt_write_pa=0; corrupt_write_mask=1; nread_masks=0;
    memset(&wl_ipcr,0,sizeof(wl_ipcr)); memset(&wl_amss,0,sizeof(wl_amss)); memset(&wl_image,0,sizeof(wl_image)); memset(&wl_vectors,0,sizeof(wl_vectors));
    memset(&wl_tables,0,sizeof(wl_tables)); wl_owned=wl_enabled=wl_route_owned=wl_route_installed=false;
    old_iova_arg=0; old_iova_present=false; probe_arg=NULL; aspm_arg=stop22_arg=keep_arg=qrtr_arg=mlo_arg=0; mlo_present=false;
    pre_arg=bhi_arg=vec_arg=fw_arg=cnss_arg=seq_arg=NULL;
    wl_seq_undos=wl_eq_bank=wl_msi_cap=wl_pm_cap=0; wl_cycle_attempted=wl_cycle_powered=wl_msi_pvm=wl_link_off=wl_dbi_ready=false; wl3_faults=0;
    memset(&wl_msi_sink,0,sizeof(wl_msi_sink));
    wl_pdc_attempted=false; wl_pdc_acks=0;
    memset(&wl_rddm,0,sizeof(wl_rddm)); memset(&wl_rxvectors,0,sizeof(wl_rxvectors));
    cache_drop=false; memset(cache_attrs,0,sizeof(cache_attrs)); wl_iova_configure();
    clock_arg=iboff_arg=0; clock_delays=nreads=0; track_reads=false;
    memset(&wl_contexts,0,sizeof(wl_contexts)); memset(wl5_report,0,sizeof(wl5_report));
    memset(fixture,0,sizeof(fixture)); file_size=sizeof(fixture);
    memcpy(fixture,"\177ELF\1\1\1",7);
    fixture[20]=1; fixture[28]=52; fixture[40]=52; fixture[42]=32; fixture[44]=1;
    wl4_report[25]=0x100; wl4_report[26]=0x224;
    setreg(WL_SMMU,0x400); setreg(WL_SMMU+0x20,0x48001e04); setreg(WL_SMMU+0x24,0x60000046);
    setreg(WL_SMMU+0x28,0x1000|0x22);
    for(unsigned i=0;i<4;i++) setreg(WL_SMMU+0xc00+4*i,0x20000);
    setreg(WL_DBI+0x34,0x70); setreg(WL_DBI+0x70,0x10);
    setreg(WL_EPCFG+0x34,0x70); setreg(WL_EPCFG+0x70,0x10);
    setreg(WL_DBI+4,0x107); /* step 14 RC IO|MEM|MASTER|SERR */
    setreg(WL_DBI+0x80,1u<<29); setreg(WL_DBI+0xf28,0x400); setreg(WL_PARF+0x1b0,0x111);
    setreg(WL_EPCFG,0x110717cb); setreg(WL_EPCFG+0x10,WL_BAR0|4);
    setreg(WL_BAR0,0x100);
    setreg(WL_BAR0+0x28,0x100); setreg(WL_BAR0+0x2c,0x224);
    setreg(WL_BAR0+0x80000,0x201); setreg(WL_BAR0+0x10,0x800080);
    setreg(WL_BAR0+0x268,0xcab04cf5); /* undefined before first BHIe DB */
    setreg(WL_BAR0+0x18,0x300); setreg(WL_BAR0+0x20,0x700); setreg(WL_EPCFG+4,0x100002);
    setreg(TS_TLMM+0x1000*94+4,2);
    setreg(WL_PARF+0x2c00,1); /* bypass: table may contain undefined reset data */
}
static void release(void) { wl_dma_free(&wl_tables); wl_dma_free(&wl_contexts); wl_dma_free(&wl_vectors); wl_dma_free(&wl_image); }
static uint64_t walk(uint64_t addr) {
    if (addr >= (1ull<<32)) return 0;
    uint64_t *root=(void *)wl_tables.va;
    if (!root[addr>>30]) return 0;
    uint64_t *l2=(void *)(wl_tables.va+4096);
    if (!l2[(addr>>21)&511]) return 0;
    unsigned n=(addr-wl_iova_base)>>21;
    uint64_t *l3=(void *)(wl_tables.va+(2+n)*4096);
    return l3[(addr>>12)&511];
}
static void check_mapping(void) {
    struct wl_dma *bufs[]={&wl_image,&wl_vectors,&wl_contexts};
    uint64_t expected=wl_pt_pa(0)|((uint64_t)wl_cb<<48);
    assert(wl_prd(wl_cb_addr(wl_cb)+0x20)==(uint32_t)expected);
    assert(wl_prd(wl_cb_addr(wl_cb)+0x24)==expected>>32);
    assert(wl_prd(wl_cb_addr(wl_cb)+0x30)==0x80a03520);
    assert(wl_prd(wl_cb_addr(wl_cb)+0x10)==0x38011);
    assert(wl_prd(wl_gr1+0x800+4*wl_cb)==1);
    assert(wl_prd(wl_gr1+4*wl_cb)==0x1f300);
    assert(wl_prd(wl_cb_addr(wl_cb)+0x28)==0);
    assert(wl_prd(wl_cb_addr(wl_cb)+0x2c)==wl_cb<<16);
    assert(wl_prd(wl_cb_addr(wl_cb)+0x38)==0xff);
    assert(wl_prd(wl_cb_addr(wl_cb)+0x3c)==0);
    uint32_t sctlr=wl_prd(wl_cb_addr(wl_cb));
    assert((sctlr&0x1e1)==0x101); /* M/HUPCF; CFRE/CFIE/CFCFG clear */
    const uint64_t *root=(void *)wl_tables.va, *l2=(void *)(wl_tables.va+4096);
    for(unsigned i=0;i<512;i++) assert(root[i]==(i==(wl_iova_base>>30)?(wl_pt_pa(1)|3):0));
    unsigned count=(wl_iova_end-wl_iova_base)>>21, first=(wl_iova_base>>21)&511;
    for(unsigned i=0;i<512;i++)
        assert(l2[i]==(i>=first && i<first+count?(wl_pt_pa(2+i-first)|3):0));
    for(unsigned i=0;i<3;i++) for(size_t off=0;off<bufs[i]->size;off+=4096) {
        uint64_t pte=walk(bufs[i]->iova+off);
        assert((pte&0xfffffffff000ull)==wl_phys(bufs[i]->va+off));
        assert((pte&0x60000000000fffull)==0x60000000000e63ull);
    }
    assert(!walk(wl_iova_base-4096) && !walk(wl_iova_end) && !walk(wl_tables.iova));
    assert(!walk(wl_iova_base==0xa0000000ull?0x10000000ull:0xa0000000ull));
}
static int findwrite(uint64_t pa, uint32_t v, unsigned from) {
    for(unsigned i=from;i<ne;i++) if(events[i].kind=='W' && events[i].pa==pa && events[i].v==v) return i;
    return -1;
}
int main(void) {
    /* M6h snapshots are read-only, include the last IB slot and every SID
       slot, and decode aborts from the correct half of the PCI dwords. */
    init();
    setreg(WL_DBI+4,0x38000107); setreg(WL_DBI+0x1c,0x38000000);
    setreg(WL_DBI+0x20,0x60506040); setreg(WL_DBI+0x24,0xfff0);
    setreg(WL_DBI+0x100,0x10001); setreg(WL_DBI+0x104,0x1234);
    setreg(WL_DBI+0x130,0x5678); setreg(WL_DBI+0x80,0x20220000);
    setreg(WL_PARF+0x2054,0x01000100); setreg(WL_PARF+0x2c00,0);
    setreg(WL_GCC_PA+0x10030,0x80000000); setreg(WL_GCC_PA+0x1003c,0x80000002);
    setreg(WL_IB(7)+4,0xc0000000); setreg(WL_IB(7)+8,0xa0000000);
    setreg(WL_IB(7)+0x10,0xafffffff); setreg(WL_IB(7)+0x14,0x12300000);
    track_reads=true; wl_host_diag("fixture"); track_reads=false;
    assert(!ne && !fw_faults);
    assert(strstr(logbuf,"fixture RC cmd/status 0x38000107 BME 1 abort STA/RTA/RMA 1/1/1 SEC 0x3800 abort STA/RTA/RMA 1/1/1"));
    assert(strstr(logbuf,"MEM 0x60506040 PREF 0xfff0") && strstr(logbuf,"AER unc/root 0x1234/0x5678"));
    assert(strstr(logbuf,"hash 0x15 entry 0x1000100 BDF 0x100 SIDoff 0x1 NEXT 0 root 0 nonzero 1 mismatch 0"));
    assert(strstr(logbuf,"slots 0..7 enabled 0x80") && strstr(logbuf,"fixture iATU IB7"));
    assert(strstr(logbuf,"OFF 0x3 OFF_no_HWCG 0x2"));
    assert(strstr(logbuf,"ICC XNU_batches=0 Linux pcie-mem init 0/250000 link 0/1000000 kBps cpu-pcie 0/1 kBps Gen2 x2"));
    for(unsigned i=0;i<256;i++) {
        bool found=false;
        for(unsigned j=0;j<nreads;j++) if(reads[j]==WL_PARF+0x2000+4*i) found=true;
        assert(found);
    }
    for(unsigned i=0;i<8;i++) {
        bool found=false;
        for(unsigned j=0;j<nreads;j++) if(reads[j]==WL_IB(i)+4) found=true;
        assert(found);
    }
    for(unsigned j=0;j<nreads;j++) {
        assert(reads[j]!=WL_BAR0+0x140); /* never BHI serial number */
        if(reads[j]>=WL_DBI+0x1000 && reads[j]<WL_DBI+0x4000)
            assert(reads[j]<WL_DBI+0x2000); /* stay in DT iATU aperture */
    }

    /* Exactly 1 enables each fix; preserve unrelated shared vote bits,
       outbound iATU and all inbound fields except REGION_EN. */
    const struct { int clock, ib; } fix_args[]={
        {0,0}, {1,0}, {0,1}, {1,1}, {2,-1}, {-1,2}
    };
    for(unsigned a=0;a<sizeof(fix_args)/sizeof(*fix_args);a++) {
        init(); for(unsigned s=17;s<=20;s++) assert(wl5_step(s)==0);
        clock_arg=fix_args[a].clock; iboff_arg=fix_args[a].ib;
        bool clock_on=clock_arg==1, ib_on=iboff_arg==1;
        setreg(WL_GCC_PA+0x52000,0x80081000); setreg(WL_GCC_PA+0x52008,0x4000002f);
        setreg(WL_GCC_PA+0x10028,0x80000002); setreg(WL_GCC_PA+0x10030,0x80000002);
        setreg(WL_DBI+0x1204,0x80000000); /* outbound 1 */
        setreg(WL_IB(0)+4,0xc0000123); setreg(WL_IB(7)+4,0x80000002);
        setreg(WL_IB(7)+0x14,0xdead0000);
        unsigned start=ne;
        assert(wl5_step(21)==0);
        assert(wl_prd(WL_GCC_PA+0x52000)==(clock_on?0x80181000:0x80081000));
        assert(wl_prd(WL_GCC_PA+0x52008)==(clock_on?0x4000006f:0x4000002f));
        assert(wl_prd(WL_IB(0)+4)==(ib_on?0x40000123:0xc0000123));
        assert(wl_prd(WL_IB(7)+4)==(ib_on?2:0x80000002));
        assert(wl_prd(WL_IB(7)+0x14)==0xdead0000 && wl_prd(WL_DBI+0x1204)==0x80000000);
        assert(findwrite(WL_DBI+0x1204,0,start)<0 && clock_delays==0);
        assert(strstr(logbuf,"M6h preflight RC") && strstr(logbuf,"M6h preflight SID"));
        assert(strstr(logbuf,"M6h preflight GCC") && strstr(logbuf,"M6h preflight ICC"));
        if(ib_on) assert(findwrite(WL_IB(7)+4,2,start)<findwrite(WL_BAR0+0x118,0x12345,start));
        wl5_abort(21); assert(frees==4);
        assert(wl_prd(WL_GCC_PA+0x52008)==(clock_on?0x4000006f:0x4000002f));
    }

    /* Vote RAZ/WI, stopped clocks, stale-IB disable RAZ/WI, RC BME loss and
       snapshot aborts all block the first BHI doorbell; no retry. */
    for(unsigned kind=0;kind<6;kind++) {
        init(); for(unsigned s=17;s<=20;s++) assert(wl5_step(s)==0);
        if(kind<2) {
            clock_arg=1;
            if(!kind) { corrupt_write_pa=WL_GCC_PA+0x52008; corrupt_write_mask=1u<<6; }
            else setreg(WL_GCC_PA+0x10030,0x80000000);
        }
        if(kind==2) {
            iboff_arg=1; setreg(WL_IB(7)+4,0x80000000);
            mismatch=true; mismatch_pa=WL_IB(7)+4;
        }
        if(kind==3) setreg(WL_DBI+4,0x103);
        if(kind==4) read_fault_pa=WL_PARF+0x3d4;
        if(kind==5) read_fault_pa=WL_IB(7)+4;
        assert(wl5_step(21)==10);
        assert(findwrite(WL_BAR0+0x118,0x12345,0)<0);
        if(kind==1) assert(clock_delays==500);
        corrupt_write_pa=read_fault_pa=0; mismatch=false;
        wl5_abort(21); assert(frees==4);
    }
    init(); clock_arg=iboff_arg=1; wl_route_installed=true;
    setreg(WL_EPCFG+4,6);
    assert(wl_fabric_prepare()==1 && !ne); /* never alter active DMA */

    /* End-to-end fake device: ELF, image/table bounds, context ABI, DMA ordering. */
    init();
    for(unsigned s=17;s<=28;s++) assert(wl5_step(s)==0);
    check_mapping();
    assert(strstr(wl_fw_path,"amss20.bin"));
    assert(wl_fw_size==sizeof(fixture) && wl_image.pa>0x100000000ull && wl_image.iova==0xa0000000ull);
    struct wl_vector *v=(void *)wl_vectors.va;
    assert(v[0].size==WL_CHUNK && v[1].size==37 && v[1].addr==wl_image.iova+WL_CHUNK);
    assert(wl5_report[19]==32);
    struct wl_context *e=(void *)(wl_contexts.va+WL_ECTX);
    assert(e[0].len==512 && e[1].len==4096 && e[1].base==wl_contexts.iova+WL_ER1);
    for(unsigned i=0;i<128*sizeof(struct wl_context);i++) assert(((uint8_t *)wl_contexts.va)[i]==0);
    int bme=findwrite(WL_EPCFG+4,0x406,0);
    int bhi=findwrite(WL_BAR0+0x118,0x12345,0);
    assert(bme>0 && bhi>bme);
    assert(findwrite(0x15081000,0,0)>findwrite(WL_PARF+0x2c00,0,0));
    /* Every image and table backing page is cleaned before BME, not just one. */
    struct wl_dma *cleanbufs[]={&wl_image,&wl_vectors,&wl_contexts,&wl_tables};
    for(unsigned d=0;d<4;d++) for(size_t o=0;o<cleanbufs[d]->size;o+=PAGE_SIZE) {
        bool cleaned=false;
        for(int i=0;i<bme;i++) if(events[i].kind=='C' && events[i].pa==wl_phys(cleanbufs[d]->va+o) && events[i].v==PAGE_SIZE) cleaned=true;
        assert(cleaned);
    }
    assert(!memcmp((void *)wl_image.va,fixture,WL_CHUNK));
    assert(findwrite(WL_BAR0+0x12c,0,0)<bhi);
    assert(findwrite(WL_BAR0+0x110,WL_CHUNK,0)<bhi);
    assert(findwrite(WL_BAR0+0x120,0,0)<bhi);
    assert(strstr(logbuf,"CNSS kiwi major-version rule") && strstr(logbuf,"file offset 0"));
    assert(findwrite(WL_BAR0+0x10c,0,0)<findwrite(WL_BAR0+0x108,wl_image.iova,0));
    assert(findwrite(WL_BAR0+0x64,0,0)<findwrite(WL_BAR0+0x60,wl_contexts.iova+WL_ECTX,0));
    assert(findwrite(WL_BAR0+0x38,0x200,0)>findwrite(WL_BAR0+0x700,wl_contexts.iova+WL_ER0+496,0));
    wl5_status(28);
    assert(wl5_report[64]==2 && wl5_report[65]==0x200);
    /* Cleanup reset -> BME off -> PERST -> free, even with latched faults. */
    unsigned start=ne; fw_faults=1; wl5_abort(28);
    int rst=findwrite(WL_BAR0+0x38,2,start), off=findwrite(WL_EPCFG+4,0x402,start);
    int perst=findwrite(TS_TLMM+0x1000*94+4,0,start);
    assert(rst>=0 && off>rst && perst>off && frees==4);
    int invalid=findwrite(WL_SMMU+0x800,0x1401,start);
    int restore=findwrite(WL_SMMU+0xc00,0x20000,start);
    int disable=findwrite(0x15081000,0,start);
    int tlbi=findwrite(0x15081610,1,start);
    int sync=findwrite(0x150817f0,0,start);
    assert(invalid>perst && restore>invalid && disable>restore && tlbi>disable && sync>tlbi);
    assert(wl_prd(WL_PARF+0x2054)==0 && wl_prd(WL_SMMU+0x800)==0);
    for(unsigned i=start;i<ne;i++) if(events[i].kind=='F') assert(i>(unsigned)sync);
    assert(!wl_image.va && fw_faults==1);
    for(unsigned i=start;i<ne;i++) if(events[i].kind=='F') assert(i>(unsigned)perst);

    /* M6f: the live TTBR walk must see the same 128 physical 4-KiB pages,
       including noncontiguous 16-KiB XNU pages. Corruption never enables DMA. */
    for(unsigned nc=0;nc<2;nc++) {
        init(); noncontig=nc;
        for(unsigned s=17;s<=20;s++) assert(wl5_step(s)==0);
        for(unsigned i=0;i<WL_CHUNK;i++) ((uint8_t *)wl_image.va)[i]=(uint8_t)(i*31+(i>>12));
        assert(wl_bhi_mapping_check()==0 && strstr(logbuf,"pages 128/128 MATCH"));
        uint64_t *l3=(void *)(wl_tables.va+8192), saved=l3[0];
        l3[0]=l3[1]; assert(wl5_step(21)==8 && !(wl_prd(WL_EPCFG+4)&4));
        assert(strstr(logbuf,"FAIL reason 5")); l3[0]=saved;
        l3[0]=0; assert(wl_bhi_mapping_check()==1); l3[0]=saved;
        uint64_t *root=(void *)wl_tables.va, oldroot=root[wl_image.iova>>30];
        root[wl_image.iova>>30]=0x12345003; assert(wl_bhi_mapping_check()==1);
        root[wl_image.iova>>30]=oldroot;
        setreg(wl_cb_addr(wl_cb)+0x20,0x1000); assert(wl_bhi_mapping_check()==1);
        setreg(wl_cb_addr(wl_cb)+0x20,(uint32_t)wl_pt_pa(0)); release();
    }
    assert(wl_hash_bytes(0xcbf29ce484222325ull,(const uint8_t *)"hello",5)==0xa430d84680aabd0bull);
    for(unsigned i=0;i<2;i++) {
        init(); random_value=i?~0u:0;
        for(unsigned s=17;s<=21;s++) assert(wl5_step(s)==0);
        assert(wl_seq==(i?WL_SEQ_MASK:1) && wl_mrd(wl_bhi+0x18)==wl_seq); release();
    }
    /* Every clear is checked before BME; stale TX status cannot pass. */
    init(); for(unsigned s=17;s<=20;s++) assert(wl5_step(s)==0);
    corrupt_write_pa=WL_BAR0+wl_bhie+0x44;
    assert(wl5_step(21)==9 && !(wl_prd(WL_EPCFG+4)&4));
    corrupt_write_pa=0; release();
    /* Sliding window restore on mismatch/fault; no arbitrary address reads. */
    init(); uint32_t value;
    setreg(WL_BAR0+0x310c,0x12340000);
    assert(!wl_soc_read(0x1b000f8,&value) && wl_mrd(0x310c)==0x12340000);
    unsigned window_start=ne;
    assert(wl_soc_read(0x2000000,&value) && ne==window_start);
    assert(wl_soc_read(0x1b000f9,&value) && ne==window_start);
    read_fault_pa=WL_BAR0+0x800f8;
    assert(wl_soc_read(0x1b000f8,&value) && fw_faults && wl_mrd(0x310c)==0x12340000);
    assert(events[ne-1].pa==WL_BAR0+0x310c && events[ne-1].v==0x12340000);
    read_fault_pa=0;
    init(); wl_bhi=0x100;
    setreg(0x200000000ull+0x1e0e238,0x1400100);
    setreg(0x200000000ull+0x1e0e23c,0xffffffff);
    window_start=ne; wl_boot_logs();
    assert(strstr(logbuf,"SBL log [0x140012c]") && !strstr(logbuf,"SBL log [0x1400130]"));
    for(unsigned i=window_start;i<ne;i++) assert(events[i].pa==WL_BAR0+0x310c);
    assert(wl_mrd(0x310c)==0);
    loglen=0; logbuf[0]=0;
    setreg(0x200000000ull+0x1e0e238,0xfffffff0);
    wl_boot_logs(); assert(strstr(logbuf,"SBL log skipped invalid SRAM"));
    setreg(WL_BAR0+0x128,2); window_start=ne;
    wl_boot_logs(); assert(ne==window_start && strstr(logbuf,"boot logs skipped EE"));

    /* M6e: all-ones STATUS is not enough to diagnose a lost endpoint. */
    init(); for(unsigned s=17;s<=21;s++) assert(wl5_step(s)==0);
    setreg(WL_BAR0+0x12c,~0u); setreg(WL_BAR0+0x130,0xef0e0003);
    setreg(WL_BAR0+0x128,0);
    unsigned diag_start=ne, diag_sleeps=sleeps;
    assert(wl5_step(22)==1 && sleeps==diag_sleeps+10);
    assert(strstr(logbuf,"M6h failure RC") && strstr(logbuf,"M6h failure PARF ADDR"));
    assert(strstr(logbuf,"M6h failure SID") && strstr(logbuf,"M6h failure iATU"));
    assert(strstr(logbuf,"M6h failure GCC") && strstr(logbuf,"M6h failure ICC"));
    for(unsigned i=diag_start;i<ne;i++) assert(events[i].kind=='W' && events[i].pa==WL_BAR0+0x310c);
    assert(wl_mrd(0x310c)==0 && strstr(logbuf,"TCSR_PBL_LOGGING") && strstr(logbuf,"PBL log"));
    assert(wl5_report[109]==129 && wl5_report[126]==129 && wl5_report[124]==~0u);
    assert(wl5_report[125]==0xef0e0003 && wl5_report[127]==1);
    assert(strstr(logbuf,"PBL error evidence") && strstr(logbuf,"BHI sample 1"));
    wl5_abort(22); assert(frees==4);
    /* Independent whole-BAR loss, config loss, DLLLA loss, decode/BAR change,
       PERST assertion and D3 each classify separately and stop before BHIe. */
    const struct {uint64_t pa; uint32_t value, flag;} lost[]={
        {WL_BAR0,~0u,2}, {WL_EPCFG,~0u,4}, {WL_DBI+0x80,0,8}, {WL_DBI+0xf28,0,8},
        {WL_EPCFG+4,0x404,16}, {WL_EPCFG+0x10,0,16},
        {TS_TLMM+0x1000*94+4,0,32}, {WL_EPCFG+0x44,3,64},
    };
    for(unsigned i=0;i<sizeof(lost)/sizeof(*lost);i++) {
        init(); for(unsigned s=17;s<=21;s++) assert(wl5_step(s)==0);
        if(lost[i].flag==64) {setreg(WL_EPCFG+0x34,0x40); setreg(WL_EPCFG+0x40,1);}
        setreg(lost[i].pa,lost[i].value);
        assert(wl5_step(22)==5 && (wl5_report[109]&lost[i].flag));
        assert(!(wl5_report[109]&128)); release();
    }
    /* Optional capability traversal: PMCSR and both AERs, no status clears. */
    init(); for(unsigned s=17;s<=21;s++) assert(wl5_step(s)==0);
    setreg(WL_EPCFG+0x34,0x40); setreg(WL_EPCFG+0x40,1);
    setreg(WL_EPCFG+0x44,0x8000);
    setreg(WL_DBI+0x100,0x10001); setreg(WL_EPCFG+0x100,0x10001);
    setreg(WL_DBI+0x104,0x1000); setreg(WL_DBI+0x110,0x40); setreg(WL_DBI+0x130,0x4);
    setreg(WL_EPCFG+0x104,0x2000); setreg(WL_EPCFG+0x110,0x80);
    diag_start=ne; assert(wl5_step(22)==0 && ne==diag_start);
    assert(wl5_report[117]==0x8000 && wl5_report[118]==0x1000 && wl5_report[119]==0x40);
    assert(wl5_report[120]==4 && wl5_report[121]==0x2000 && wl5_report[122]==0x80);
    release();
    init(); for(unsigned s=17;s<=21;s++) assert(wl5_step(s)==0);
    setreg(WL_EPCFG+0x34,0x40); setreg(WL_EPCFG+0x40,0x4005); /* cycle */
    setreg(WL_DBI+0x100,0x10010002); /* extended cycle */
    diag_start=ne; assert(wl5_step(22)==0 && ne==diag_start && (wl5_report[109]&512)); release();
    /* Capability bodies must fit their own config-space extent. */
    init(); uint32_t cap_flags=0;
    setreg(WL_EPCFG+0x34,0xfc); setreg(WL_EPCFG+0xfc,1);
    assert(!wl_diag_cap(WL_EPCFG,1,false,&cap_flags) && cap_flags==512);
    setreg(WL_EPCFG+0x100,0xffc10002); setreg(WL_EPCFG+0xffc,0x10001);
    cap_flags=0;
    assert(!wl_diag_cap(WL_EPCFG,1,true,&cap_flags) && cap_flags==512);
    /* A repeat can detect a late link loss or a late PBL failure after success. */
    for(unsigned kind=1;kind<=2;kind++) {
        init(); for(unsigned s=17;s<=21;s++) assert(wl5_step(s)==0);
        diag_repeat_kind=kind;
        assert(wl5_step(22)==(kind==1?5:1));
        assert(wl5_report[126]==0 && wl5_report[109]); release();
    }
    /* Real MMIO fault remains latched; diagnostics never write hardware. */
    init(); for(unsigned s=17;s<=21;s++) assert(wl5_step(s)==0);
    read_fault_pa=WL_DBI+0xf28; diag_start=ne;
    assert(wl5_step(22)==5 && (wl5_report[109]&256) && ne==diag_start);
    read_fault_pa=0; fw_faults=0; wl5_abort(22); assert(frees==4);
    /* Reject corrupted setup readbacks before any BME/doorbell. */
    const uint32_t bhi_setup[]={0x108,0x10c,0x110,0x120,0x12c};
    for(unsigned i=0;i<sizeof(bhi_setup)/sizeof(*bhi_setup);i++) {
        init(); for(unsigned s=17;s<=20;s++) assert(wl5_step(s)==0);
        corrupt_write_pa=WL_BAR0+bhi_setup[i];
        assert(wl5_step(21)==7 && !(wl_prd(WL_EPCFG+4)&4));
        assert(findwrite(WL_BAR0+0x118,0x12345,0)<0);
        corrupt_write_pa=0; wl5_abort(21); assert(frees==4);
    }
    init(); setreg(WL_BAR0+0x80000,0x40170100);
    assert(wl5_step(17)==0 && wl5_step(18)==0 && strstr(wl_fw_path,"/amss.bin"));
    wl5_abort(18);

    /* Proven vacancies; existing valid streams and active/referenced CBs survive. */
    init(); setreg(WL_SMMU+0x800,0x80001200); setreg(WL_SMMU+0xc00,2);
    setreg(0x15081000,1); /* CB1 active, CB2 referenced: select CB3 */
    assert(wl5_step(17)==0 && wl_slot==1 && wl_cb==3);
    for(unsigned s=18;s<=20;s++) assert(wl5_step(s)==0);
    assert(wl_prd(WL_SMMU+0x800)==0x80001200 && wl_prd(0x15081000)==1);
    wl5_abort(20); assert(frees==4);
    init(); setreg(WL_SMMU+0x800,0x80001401); assert(wl5_step(17)==2 && ne==0);
    init(); setreg(WL_SMMU+0x800,0x80011400); assert(wl5_step(17)==2 && ne==0);
    init(); for(unsigned i=0;i<4;i++) setreg(WL_SMMU+0x800+4*i,0x80001000+i);
    assert(wl5_step(17)==3 && ne==0);
    init(); for(unsigned i=0;i<70;i++) setreg(0x15080000+4096*i,1);
    assert(wl5_step(17)==3 && ne==0);
    init(); setreg(WL_SMMU,1); assert(wl5_step(17)==1 && ne==0);
    init(); setreg(WL_SMMU,8); assert(wl5_step(17)==1 && ne==0);
    init(); setreg(WL_SMMU+0x28,0); assert(wl5_step(17)==1 && ne==0);
    init(); setreg(WL_PARF+0x234,1); assert(wl5_step(17)==5 && wl5_report[25]==1 && ne==0);
    /* M6b: bypassed garbage is not an active table, even duplicate RID bits. */
    init(); setreg(WL_PARF+0x2c00,0xa5);
    for(unsigned i=0;i<256;i++) setreg(WL_PARF+0x2000+4*i,0x0100ffff);
    assert(wl_rc_hash(0)==0 && wl_rc_hash(0x100)==0x15);
    assert(wl5_step(17)==0 && wl_route_installed && wl_prd(WL_PARF+0x2c00)==0xa4);
    assert(ne==258); /* 256 zero writes, endpoint mapping, then bypass clear */
    for(unsigned i=0;i<256;i++) {
        assert(events[i].pa==WL_PARF+0x2000+4*i && events[i].v==0);
        assert(wl_prd(WL_PARF+0x2000+4*i)==(i==0x15?0x01000100:0));
    }
    assert(events[256].pa==WL_PARF+0x2054 && events[256].v==0x01000100);
    assert(events[257].pa==WL_PARF+0x2c00 && events[257].v==0xa4);
    unsigned undo_start=ne; wl5_abort(17);
    int bypass=findwrite(WL_PARF+0x2c00,0xa5,undo_start);
    assert(bypass>findwrite(TS_TLMM+0x1000*94+4,0,undo_start));
    for(unsigned i=0;i<256;i++) {
        assert(findwrite(WL_PARF+0x2000+4*i,0,undo_start)>bypass);
        assert(wl_prd(WL_PARF+0x2000+4*i)==0);
    }
    assert(!wl_route_owned && !wl_route_installed);
    /* Never adopt or modify a table already in use, even a correct/empty one. */
    const uint32_t active_entries[]={0xddb37e73,0x01000200,0x01000100,0};
    for(unsigned i=0;i<4;i++) {
        init(); setreg(WL_PARF+0x2c00,0); setreg(WL_PARF+0x2054,active_entries[i]);
        assert(wl5_step(17)==5 && wl5_report[25]==2 && ne==0 && !wl_route_owned);
    }
    /* Existing MSI users or malformed capability chains forbid the scrub. */
    for(unsigned c=0;c<2;c++) for(unsigned msix=0;msix<2;msix++) {
        init(); uint64_t cfg=c?WL_EPCFG:WL_DBI;
        setreg(cfg+0x34,0x40); setreg(cfg+0x40,msix?0x80000011:0x10005);
        assert(wl5_step(17)==5 && wl5_report[25]==3 && ne==0);
    }
    for(unsigned i=0;i<8;i++) {
        init(); setreg(WL_DBI+0x828+12*i,1);
        assert(wl5_step(17)==5 && wl5_report[25]==3 && ne==0);
    }
    init(); setreg(WL_EPCFG+0x34,0x40); setreg(WL_EPCFG+0x40,0x4001);
    assert(wl5_step(17)==5 && wl5_report[25]==3 && ne==0);
    init(); setreg(WL_EPCFG+4,6); assert(wl5_step(17)==4 && ne==0);
    /* Scrub must verify every entry before installing or enabling lookup. */
    init(); mismatch=true; mismatch_pa=WL_PARF+0x23fc; setreg(mismatch_pa,0xdeadbeef);
    assert(wl5_step(17)==5 && wl5_report[25]==4 && !wl_route_installed);
    assert(findwrite(WL_PARF+0x2054,0x01000100,0)<0 && wl_prd(WL_PARF+0x2c00)==1);
    mismatch=false; wl5_abort(17); assert(!wl_route_owned && !wl_prd(mismatch_pa));
    const uint64_t route_mismatch[]={WL_PARF+0x2054,WL_PARF+0x2c00};
    for(unsigned i=0;i<2;i++) {
        init(); mismatch=true; mismatch_pa=route_mismatch[i];
        assert(wl5_step(17)==5 && wl5_report[25]==5 && !wl_route_installed && !wl_owned);
        mismatch=false; wl5_abort(17); assert(wl_prd(WL_PARF+0x2c00)==1 && !wl_prd(WL_PARF+0x2054));
    }
    /* Failed bypass restore must leave entries intact; retry completes cleanup. */
    init(); assert(wl5_step(17)==0); mismatch=true; mismatch_pa=WL_PARF+0x2c00;
    undo_start=ne; wl5_abort(17);
    assert(wl_route_owned && wl_prd(WL_PARF+0x2054)==0x01000100);
    assert(findwrite(WL_PARF+0x2000,0,undo_start)<0);
    mismatch=false; wl5_abort(17); assert(!wl_route_owned);
    /* No SMMU writes until a verified route exists, including late changes. */
    init(); assert(wl_smmu_enable()==3 && ne==0 && allocations==0);
    init(); for(unsigned s=17;s<20;s++) assert(wl5_step(s)==0);
    setreg(WL_PARF+0x2000,1); undo_start=ne;
    assert(wl5_step(20)==3 && !wl_owned && !wl_enabled);
    for(unsigned i=undo_start;i<ne;i++) assert(events[i].pa<WL_SMMU || events[i].pa>=WL_SMMU+0x100000);
    wl5_abort(20); assert(frees==3);
    /* Changed ownership, readback mismatch, timeout and latched DMA fault. */
    init(); for(unsigned s=17;s<20;s++) assert(wl5_step(s)==0);
    setreg(WL_SMMU+0x800,0x80001111); assert(wl5_step(20)==3); wl5_abort(20);
    init(); for(unsigned s=17;s<20;s++) assert(wl5_step(s)==0);
    mismatch=true; assert(wl5_step(20)==4 && !wl_enabled); mismatch=false; wl5_abort(20); assert(frees==4);
    init(); for(unsigned s=17;s<20;s++) assert(wl5_step(s)==0);
    tlb_busy=true; setreg(0x150817f4,1); assert(wl5_step(20)==5 && !wl_enabled);
    wl5_abort(20); assert(frees==0); tlb_busy=false; setreg(0x150817f4,0); wl5_abort(20); assert(frees==4);
    init(); for(unsigned s=17;s<=20;s++) assert(wl5_step(s)==0);
    inject_fault=true; assert(wl5_step(21)==0); assert(wl_smmu_fault(21)==1 && wl_poll(0)==3);
    assert(wl5_report[100]==2); wl5_abort(21); assert(frees==4);

    /* M6d: TBI reads one on every bank. The first candidate must pass both
       the masked-zero probe and nonzero programming, preserving raw logs. */
    init(); for(unsigned s=17;s<20;s++) assert(wl5_step(s)==0);
    for(unsigned cb=1;cb<=8;cb++) read_masks[nread_masks++]=(struct read_mask){wl_cb_addr(cb)+0x10,0x60,0};
    unsigned m6d_start=ne;
    assert(wl5_step(20)==0 && wl_cb==1 && wl_owned && wl_enabled);
    assert(!wl_cb_unusable[1] && !wl_smmu_mismatches);
    assert(strstr(logbuf,"CB_TCR2 probe offset 0x81010 wrote 0 read 0x60 mask 0x38017 faults 0"));
    assert(strstr(logbuf,"CB_TCR2 offset 0x81010 wrote 0x38011 read 0x38071 mask 0x38017 faults 0"));
    assert(findwrite(wl_cb_addr(2)+0x10,0,m6d_start)<0);
    assert(!(wl_prd(WL_EPCFG+4)&4));
    wl5_abort(20); assert(frees==4);

    /* Fixed architectural expectations, independent of the C mask constants:
       ignore only non-writable/irrelevant bits; writable address, ASID, type,
       cacheability and control bits must still reject, even for zero writes. */
    const struct {uint64_t pa; uint32_t ignored, checked;} masks[]={
        {0x15081000,0x80203806,0x7fdfc7f9}, /* reserved, ASIDPNE, AFE/TRE */
        {0x15001804,0xfffffffe,1}, /* VA64 only with VMID16 disabled */
        {0x15001004,0xff000000,0x00ffffff}, /* v2 IRPTNDX no storage */
        {0x15081010,0xfffc7fe8,0x00038017}, /* TBI + reserved/Secure/absent extensions */
        {0x15081030,0x40,0xffffffbf},
        {0x15081024,0x0000ff00,0xffff00ff}, /* fixture: 40-bit PA + 16-bit ASID */
        {0x15081020,0x1f,0xffffffe0}, /* root has 4 L1 entries: 32-byte alignment */
        {0x1508102c,0x0000ff00,0xffff00ff},
        {0x15081028,0x1f,0xffffffe0},
        {0x15081038,0,0xffffffff}, {0x1508103c,0,0xffffffff},
    };
    for(unsigned i=0;i<sizeof(masks)/sizeof(*masks);i++) {
        init(); read_masks[nread_masks++]=(struct read_mask){masks[i].pa,masks[i].ignored,0};
        for(unsigned s=17;s<20;s++) assert(wl5_step(s)==0);
        int masked_result=wl5_step(20);
        if(masked_result || !wl_enabled || wl_smmu_mismatches) fprintf(stderr,"mask case %u result %d\n%s",i,masked_result,logbuf);
        assert(masked_result==0 && wl_enabled && !wl_smmu_mismatches);
        /* Neither ACTLR nor CONTEXTIDR is touched in this AArch64 lane. */
        for(unsigned j=0;j<ne;j++) if(events[j].kind=='W')
            assert(events[j].pa!=0x15081004 && events[j].pa!=0x15081034);
        wl5_abort(20); assert(frees==4);
        /* Exercise every meaningful bit through actual programming, including
           the zero probe and zero-valued fields, rather than testing a model. */
        for(unsigned bit=0;bit<32;bit++) if(masks[i].checked & (1u<<bit)) {
            init(); for(unsigned s=17;s<20;s++) assert(wl5_step(s)==0);
            corrupt_write_pa=masks[i].pa; corrupt_write_mask=1u<<bit;
            int result=wl5_step(20);
            if(masks[i].pa==0x15081010) {
                assert(result==0 && wl_cb==2 && wl_cb_unusable[1]);
            } else { assert(result==4 && wl_cb==1 && !wl_enabled); }
            corrupt_write_pa=0; wl5_abort(20); assert(frees==4);
        }
    }
    /* A writable probe mismatch alongside TBI ones still excludes that bank. */
    init(); for(unsigned s=17;s<20;s++) assert(wl5_step(s)==0);
    probe_block[1]=true; setreg(0x15081010,0x61);
    assert(wl5_step(20)==0 && wl_cb==2 && wl_cb_unusable[1]);
    assert(strstr(logbuf,"CB_TCR2 probe offset 0x81010 wrote 0 read 0x61 mask 0x38017"));
    wl5_abort(20); assert(frees==4);
    /* Full-programming RAZ/WI with TBI=1 passes the probe, fails after claim. */
    init(); for(unsigned s=17;s<20;s++) assert(wl5_step(s)==0);
    probe_block[1]=true; setreg(0x15081010,0x60);
    assert(wl5_step(20)==4 && wl_owned && wl_cb==1 && !wl_enabled);
    assert(strstr(logbuf,"CB_TCR2 offset 0x81010 wrote 0x38011 read 0x60 mask 0x38017"));
    wl5_abort(20); assert(frees==4);

    /* SCTLR non-writable bits also survive the pre-DMA gate and undo;
       AFE/TRE may read zero despite Linux-style writes of one. */
    init(); read_masks[nread_masks++]=(struct read_mask){0x15081000,0x80202800,0x1006};
    for(unsigned step=17;step<=28;step++) assert(wl5_step(step)==0);
    assert(strstr(logbuf,"CB_SCTLR pre-DMA") && !wl_smmu_mismatches);
    wl5_abort(28); assert(frees==4);
    const uint32_t enable_bits[]={1,0x100}; /* M and HUPCF */
    for(unsigned i=0;i<2;i++) {
        init(); read_masks[nread_masks++]=(struct read_mask){0x15081000,0,enable_bits[i]};
        for(unsigned step=17;step<20;step++) assert(wl5_step(step)==0);
        assert(wl5_step(20)==5 && !wl_enabled && !(wl_prd(WL_EPCFG+4)&4));
        wl5_abort(20); assert(frees==4);
    }
    /* A late change to any CNSS fault-control bit must block the BHI DB. */
    const uint32_t fault_bits[]={0x20,0x40,0x80,0x100};
    for(unsigned i=0;i<4;i++) {
        init(); for(unsigned step=17;step<=20;step++) assert(wl5_step(step)==0);
        setreg(wl_cb_addr(wl_cb),wl_prd(wl_cb_addr(wl_cb))^fault_bits[i]);
        assert(wl5_step(21)==6 && !(wl_prd(WL_EPCFG+4)&4));
        assert(findwrite(WL_BAR0+0x118,0x12345,0)<0);
        wl5_abort(21); assert(frees==4);
    }
    init(); for(unsigned step=17;step<=20;step++) assert(wl5_step(step)==0);
    read_masks[nread_masks++]=(struct read_mask){0x15081000,1,0}; /* M stuck on */
    wl5_abort(20); assert(frees==0 && wl_owned);
    nread_masks=0; wl5_abort(20); assert(frees==4);

    /* Optional writable extensions/VMID16 are checked only when implemented. */
    init(); wl5_report[6]=1u<<24; wl5_report[7]=(1u<<28)|0x11;
    assert(wl_tcr2_mask()==0x38717);
    wl5_report[6]=3u<<24; assert(wl_tcr2_mask()==0x38f17);
    init(); setreg(WL_SMMU+0x24,0x62000046); /* reserved HAFDBS encoding */
    assert(wl5_step(17)==1 && ne==0);
    wl5_report[6]=0; wl5_report[7]=0x55; assert(wl_ttbr_hi_mask()==0xffffffff);
    for(unsigned bit=16;bit<32;bit++) {
        init(); setreg(WL_SMMU,0x80000400); setreg(WL_SMMU+0x28,0x9011);
        for(unsigned s=17;s<20;s++) assert(wl5_step(s)==0);
        corrupt_write_pa=0x15001804; corrupt_write_mask=1u<<bit;
        assert(wl5_step(20)==4 && !wl_enabled);
        corrupt_write_pa=0; wl5_abort(20); assert(frees==4);
    }

    /* M6c: blocked zero probe skips a bank without claiming/programming it. */
    init(); for(unsigned s=17;s<20;s++) assert(wl5_step(s)==0);
    setreg(0x15081000,0x2468); /* unknown reset bits with M=0 are not ownership */
    wl_old_sctlr=0x2468;
    probe_block[1]=true; setreg(0x15081010,0x1234);
    unsigned probe_start=ne;
    assert(wl5_step(20)==0 && wl_cb==2 && wl5_report[9]==2 && wl_cb_unusable[1]);
    assert(strstr(logbuf,"CB_TCR2 probe offset 0x81010 wrote 0 read 0x1234"));
    for(unsigned i=probe_start;i<ne;i++) if(events[i].pa>=0x15081000 && events[i].pa<0x15082000)
        assert(events[i].pa==0x15081010 && events[i].v==0);
    wl5_abort(20); assert(frees==4);
    assert(findwrite(0x15081000,0,probe_start)<0); /* rejected CB gets no undo */
    assert(findwrite(0x15082000,0,probe_start)>0);

    /* Exactly eight writes; a ninth accessible bank must remain untouched. */
    init(); for(unsigned s=17;s<20;s++) assert(wl5_step(s)==0);
    for(unsigned i=1;i<=8;i++) {probe_block[i]=true; setreg(wl_cb_addr(i)+0x10,1);}
    probe_start=ne;
    assert(wl5_step(20)==6 && !wl_owned && !wl_enabled);
    unsigned probes=0;
    for(unsigned i=probe_start;i<ne;i++) if(events[i].kind=='W' && events[i].pa>=WL_SMMU && events[i].pa<WL_SMMU+0x100000) {
        assert((events[i].pa&0xfff)==0x10 && events[i].v==0); probes++;
    }
    assert(probes==8 && findwrite(wl_cb_addr(9)+0x10,0,probe_start)<0);
    assert(strstr(logbuf,"probe exhausted 8 candidates"));
    wl_cb=1; probe_start=ne; /* the unusable set persists for this boot */
    assert(wl_smmu_claim_candidate()==0 && wl_cb==9);
    for(unsigned i=1;i<=8;i++) assert(findwrite(wl_cb_addr(i)+0x10,0,probe_start)<0);
    wl5_abort(20); assert(frees==4);

    /* Each retry rechecks references, M, and ASIDs; never write these banks. */
    init(); for(unsigned s=17;s<20;s++) assert(wl5_step(s)==0);
    probe_block[1]=true; setreg(0x15081010,1); probe_change_next=true;
    probe_start=ne;
    assert(wl5_step(20)==0 && wl_cb==6);
    for(unsigned i=2;i<=5;i++) assert(findwrite(wl_cb_addr(i)+0x10,0,probe_start)<0);
    probe_change_next=false; wl5_abort(20); assert(frees==4);
    init(); for(unsigned s=17;s<20;s++) assert(wl5_step(s)==0);
    probe_steal=true; probe_start=ne;
    assert(wl5_step(20)==3 && !wl_owned);
    assert(findwrite(wl_gr1+0x804,1,probe_start)<0);
    probe_steal=false; wl5_abort(20); assert(frees==4);
    init(); for(unsigned s=17;s<20;s++) assert(wl5_step(s)==0);
    read_fault_pa=0x15081010;
    assert(wl5_step(20)==6 && fw_faults && !wl_owned);
    assert(findwrite(0x15082010,0,0)<0); read_fault_pa=0;
    wl5_abort(20); assert(frees==4);

    /* A RAZ/WI zero probe may pass; full TCR2 programming must still fail,
       with NO fallback once claimed. Every register (including zero writes)
       must identify its own offset, written value and actual readback. */
    init(); for(unsigned s=17;s<20;s++) assert(wl5_step(s)==0);
    probe_block[1]=true;
    assert(wl5_step(20)==4 && wl_owned && wl_cb==1);
    assert(strstr(logbuf,"CB_TCR2 offset 0x81010 wrote 0x38011 read 0"));
    wl5_abort(20); assert(frees==4);
    const struct {uint64_t pa; const char *name; unsigned error; uint32_t bit;} badregs[]={
        {0x15081000,"CB_SCTLR disable",4}, {0x15001804,"GR1_CBA2R",4},
        {0x15001004,"GR1_CBAR",4}, {0x15081030,"CB_TCR",4},
        {0x15081024,"CB_TTBR0_HI",4}, {0x15081020,"CB_TTBR0_LO",4,0x20},
        {0x1508102c,"CB_TTBR1_HI",4}, {0x15081028,"CB_TTBR1_LO",4,0x20},
        {0x15081038,"CB_MAIR0",4}, {0x1508103c,"CB_MAIR1",4},
        {WL_SMMU+0xc00,"GR0_S2CR",5}, {WL_SMMU+0x800,"GR0_SMR",5},
    };
    for(unsigned i=0;i<sizeof(badregs)/sizeof(*badregs);i++) {
        init(); for(unsigned s=17;s<20;s++) assert(wl5_step(s)==0);
        corrupt_write_pa=badregs[i].pa; corrupt_write_mask=badregs[i].bit?badregs[i].bit:1;
        assert(wl5_step(20)==badregs[i].error && !wl_enabled && !(wl_prd(WL_EPCFG+4)&4));
        char expected[128]; snprintf(expected,sizeof(expected),"%s offset %#x wrote ",badregs[i].name,(unsigned)(badregs[i].pa-WL_SMMU));
        assert(strstr(logbuf,expected) && strstr(logbuf," read "));
        corrupt_write_pa=0; wl5_abort(20); assert(frees==4);
    }
    init(); for(unsigned s=17;s<20;s++) assert(wl5_step(s)==0);
    mismatch=true; mismatch_pa=0x15081000;
    assert(wl5_step(20)==5 && strstr(logbuf,"CB_SCTLR enable offset 0x81000 wrote 0x1107 read 0"));
    mismatch=false; wl5_abort(20); assert(frees==4);
    init(); for(unsigned s=17;s<20;s++) assert(wl5_step(s)==0);
    setreg(0x15081058,2); mismatch=true; mismatch_pa=0x15081058;
    assert(wl5_step(20)==5 && strstr(logbuf,"CB_FSR W1C offset 0x81058 wrote 0xc00001fe read 0x2"));
    mismatch=false; wl5_abort(20); assert(frees==4);
    init(); for(unsigned s=17;s<20;s++) assert(wl5_step(s)==0);
    setreg(0x15081058,0x400); /* format-only FSR is not a W1C/fault mismatch */
    assert(wl5_step(20)==0 && wl5_report[80]==0x400);
    wl5_abort(20); assert(frees==4);
    /* Diagnostic cap is independent of repeated callers. */
    init(); wl_cb=1;
    for(unsigned i=0;i<20;i++) assert(wl_smmu_verify("cap",WL_SMMU,1,0,1,~0u));
    unsigned lines=0; for(char *p=logbuf;(p=strstr(p,"SMMU mismatch"));p++) lines++;
    assert(lines==16);

    /* A fault-contained MMIO abort latches normal writes off; owned undo still runs. */
    init(); for(unsigned s=17;s<20;s++) assert(wl5_step(s)==0);
    read_fault_pa=0x15081030; assert(wl5_step(20)==4 && fw_faults);
    read_fault_pa=0; unsigned latched=fw_faults; wl5_abort(20);
    assert(frees==4 && fw_faults==latched);

    /* Shared global USF is read-only; a fault naming our SID blocks DMA. */
    init(); for(unsigned s=17;s<=20;s++) assert(wl5_step(s)==0);
    setreg(WL_SMMU+0x48,2); setreg(WL_SMMU+0x54,0x1401);
    assert(wl5_step(21)==6 && !(wl_prd(WL_EPCFG+4)&4)); wl5_abort(21); assert(frees==4);
    assert(wl_prd(WL_SMMU+0x48)==2);

    /* High noncontiguous XNU pages all translate correctly; no adjacent maps. */
    init(); noncontig=true; for(unsigned s=17;s<=20;s++) assert(wl5_step(s)==0);
    check_mapping(); wl5_abort(20); assert(frees==4);

    /* M6g: one kernel, both address windows, identical coherency/fault profile.
       Exercise the actual boot-arg parser helper; only exactly 1 opts out. */
    const int iova_args[]={0,1,2,-1};
    for(unsigned a=0;a<4;a++) {
        init(); old_iova_present=true; old_iova_arg=iova_args[a]; wl_iova_configure();
        uint64_t base=iova_args[a]==1?0x10000000ull:0xa0000000ull;
        uint64_t end=iova_args[a]==1?0x11000000ull:0xb0000000ull;
        assert(wl_iova_base==base && wl_iova_end==end && wl_iova_next==base);
        noncontig=true;
        for(unsigned s=17;s<=28;s++) assert(wl5_step(s)==0);
        check_mapping();
        assert(wl_image.iova==base && wl_vectors.iova>=base && wl_contexts.iova<end);
        assert(wl_bhi_mapping_check()==0 && strstr(logbuf,"pages 128/128 MATCH"));
        assert(findwrite(WL_BAR0+0x108,(uint32_t)base,0)>=0);
        assert(strstr(logbuf,"IRGN0=1 ORGN0=1 SH0=3") && strstr(logbuf,"HUPCF=1 CFCFG=0 CFRE=0"));
        wl5_abort(28); assert(frees==4);
    }
    /* Both allocation limits, including a cursor outside the chosen pool. */
    for(unsigned a=0;a<2;a++) {
        init(); old_iova_present=true; old_iova_arg=a; wl_iova_configure();
        struct wl_dma d={0};
        wl_iova_next=wl_iova_end-PAGE_SIZE;
        assert(wl_dma_alloc(&d,PAGE_SIZE+1)==1 && allocations==0);
        assert(wl_dma_alloc(&d,PAGE_SIZE)==0 && d.iova==wl_iova_end-PAGE_SIZE);
        wl_dma_free(&d); assert(wl_dma_alloc(&d,1)==1);
        wl_iova_next=wl_iova_base-1; assert(wl_dma_alloc(&d,1)==1);
        wl_iova_next=wl_iova_end+1; assert(wl_dma_alloc(&d,1)==1);
    }
    /* A leaf in the last 2-MiB slice proves high-pool L2/L3 indexing, while
       the unused pool still faults and tables remain inaccessible to DMA. */
    init(); noncontig=true;
    for(unsigned s=17;s<20;s++) assert(wl5_step(s)==0);
    assert(wl_dma_alloc(&wl_vectors,16384)==0 && wl_dma_alloc(&wl_contexts,16384)==0);
    wl_contexts.iova=0xb0000000ull-PAGE_SIZE;
    assert(wl_smmu_enable()==0); check_mapping();
    assert(!walk(0xa8000000ull) && !walk(0xafffb000ull));
    wl5_abort(20); assert(frees==4);
    /* Max-size firmware spans multiple L3s in either window. */
    for(unsigned a=0;a<2;a++) {
        init(); old_iova_present=true; old_iova_arg=a; wl_iova_configure(); noncontig=true;
        for(unsigned s=17;s<19;s++) assert(wl5_step(s)==0);
        assert(wl_dma_alloc(&wl_image,WL_FW_MAX)==0); wl_fw_size=WL_FW_MAX;
        assert(wl5_step(20)==0); check_mapping();
        struct wl_vector *vectors=(void *)wl_vectors.va;
        assert(wl5_report[19]==30*16 && vectors[29].addr==wl_image.iova+29*WL_CHUNK);
        assert(wl_bhi_mapping_check()==0); wl5_abort(20); assert(frees==4);
    }

    /* Every hardware poll terminates, error EEs are never success. */
    init(); assert(wl5_step(17)==0);
    assert(wl_poll(0)==2 && sleeps==5000);
    setreg(WL_BAR0+0x128,3); assert(wl_poll(0)==1);
    setreg(WL_BAR0+0x128,6); assert(wl_poll(4)==1);
    setreg(WL_BAR0+0x128,0); setreg(WL_BAR0+0x48,4); assert(wl_poll(1)==1);
    setreg(WL_BAR0+0x48,0); setreg(WL_BAR0+0x128,1); wl_bhie_seq=9;
    setreg(WL_BAR0+0x268,(2u<<30)|8); assert(wl_poll(3)==2);
    setreg(WL_BAR0+0x268,(2u<<30)|9); assert(wl_poll(3)==0);

    setreg(WL_BAR0+0x268,3u<<30); assert(wl_poll(3)==1);

    /* Root timeout, missing/short/oversize/non-ELF/truncated ELF, short reads. */
    init(); assert(wl5_step(17)==0 && wl5_step(18)==0);
    no_root=true; assert(wl5_step(19)==1 && sleeps==120000 && allocations==0);
    no_root=false; open_error=2; assert(wl5_step(19)==2); open_error=0;
    file_size=WL_CHUNK-1; assert(wl5_step(19)==3);
    file_size=WL_FW_MAX+1; assert(wl5_step(19)==3);
    file_size=sizeof(fixture); fixture[0]=0; assert(wl5_step(19)==6); release();
    fixture[0]=0x7f; fixture[30]=0xff; assert(wl5_step(19)==6); release();
    fixture[30]=0; read_resid=1; assert(wl5_step(19)==5); release();
    read_resid=0; high_memory=true; assert(wl5_step(19)==4); release();
    high_memory=false; noncontig=true; assert(wl5_step(19)==0); release();
    noncontig=false; fail_alloc=true; assert(wl5_step(19)==4); release();
    fail_alloc=false; setreg(WL_BAR0+0x80000,0x101); assert(wl5_step(18)==0);
    assert(strstr(wl_fw_path,"/amss.bin"));
    assert(wl5_step(19)==0); release();
    /* FBC metadata must fit the prefix even when the whole file is larger. */
    init(); assert(wl5_step(17)==0 && wl5_step(18)==0);
    uint32_t hash_flags=0x02200000, hash_offset=0x1000, hash_size=0x308;
    memcpy(fixture+52+24,&hash_flags,4); memcpy(fixture+52+4,&hash_offset,4);
    memcpy(fixture+52+16,&hash_size,4);
    assert(wl5_step(19)==0 && strstr(logbuf,"hash segments 1 offset 0x1000 size 0x308")); release();
    hash_offset=WL_CHUNK-4; memcpy(fixture+52+4,&hash_offset,4);
    assert(wl5_step(19)==6); release();
    hash_offset=WL_CHUNK-16; memcpy(fixture+28,&hash_offset,4);
    assert(wl5_step(19)==6); release();

    /* Both PCI interrupt mechanisms are disabled, malformed cycles bounded. */
    init(); setreg(WL_EPCFG+0x34,0x40);
    setreg(WL_EPCFG+0x40,(1u<<31)|0x5011); /* MSI-X -> 0x50 */
    setreg(WL_EPCFG+0x50,(1u<<16)|5); /* MSI */
    assert(wl_disable_irqs()==0);
    assert(!(wl_prd(WL_EPCFG+0x50)&(1u<<16)));
    assert((wl_prd(WL_EPCFG+0x40)&(3u<<30))==(1u<<30));
    setreg(WL_EPCFG+0x50,0x5005); assert(wl_disable_irqs()==3);

    /* Never free buffers when active DMA cannot be contained. */
    init(); for(unsigned s=17;s<=21;s++) assert(wl5_step(s)==0);
    fail_bm=fail_perst=true; wl5_abort(21);
    assert(frees==0 && wl_image.va && wl_contexts.va && wl_vectors.va);
    fail_bm=fail_perst=false; wl5_abort(21); assert(frees==4);
    puts("M6 fake-MMIO/VFS/DMA sequence and failure tests passed");
    return 0;
}
'''


def main():
    driver = (ROOT / 'xnu/fuxi-wlan/fuxi_wlan.c').read_text()
    patch = (ROOT / 'xnu/patches/0099-fuxi-wlan.patch').read_text()
    added = patch.split('+++ b/bsd/dev/fuxi_wlan.c\n', 1)[1].split('\n--- a/', 1)[0]
    count = re.match(r'@@ -0,0 \+1,(\d+) @@\n', added)
    assert count and int(count[1]) == len(driver.splitlines()), 'WLAN new-file hunk count differs'
    reconstructed = '\n'.join(line[1:] for line in added.splitlines() if line.startswith('+')) + '\n'
    assert reconstructed == driver, 'integration patch and standalone WLAN C differ'
    included = patch.split('+++ b/bsd/dev/wlan.c\n', 1)[1]
    reconstructed = '\n'.join(line[1:] for line in included.splitlines() if line.startswith('+')) + '\n'
    assert reconstructed == (ROOT / 'shim/fuxi-probe/wlan.c').read_text(), 'included M3 source differs'
    block = driver.split('/* ---- milestone 5:', 1)[1].split('static void\nfw_dump(', 1)[0]
    block = '/* ---- milestone 5:' + block
    block = re.sub(r'__asm__ volatile\([^;]+;', '/* host memory barrier */;', block)
    with tempfile.TemporaryDirectory(prefix='fuxi-wlan-m6-') as tmp:
        cfile, binary = Path(tmp) / 'm6.c', Path(tmp) / 'm6'
        cfile.write_text(PREAMBLE + block + TESTS)
        subprocess.run([os.environ.get('CC', 'cc'), '-std=c11', '-D_DEFAULT_SOURCE', '-g',
                        '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                        str(cfile), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True)
    # Packing must fail before reading firmware when any required path is absent/relative.
    with tempfile.TemporaryDirectory(prefix='fuxi-wlan-pack-') as tmp:
        payload = Path(tmp) / 'payload'
        payload.write_bytes(b'fixture')
        required = ['MKBOOTIMG', 'FUXI_GMU_FIRMWARE', 'FUXI_SQE_FIRMWARE', 'FUXI_GPU_ZAP_MBN']
        env = dict(os.environ, CLANG='unused', LLD='unused', OBJCOPY='unused', READELF='unused')
        env.update({k: str(payload) for k in required})
        for key in required:
            missing = dict(env); missing.pop(key, None)
            result = subprocess.run(['bash', str(ROOT / 'shim/fuxi-loader/build.sh'), str(payload)],
                                    env=missing, capture_output=True, text=True)
            assert result.returncode and key in result.stderr
        for key in required:
            relative = dict(env); relative[key] = 'relative/input'
            result = subprocess.run(['bash', str(ROOT / 'shim/fuxi-loader/build.sh'), str(payload)],
                                    env=relative, capture_output=True, text=True)
            assert result.returncode and 'absolute file paths' in result.stderr
    print('M6 packing rejects unset/relative required inputs')
    spec = importlib.util.spec_from_file_location('wlan_probe', ROOT / 'tools/fuxi-wlan-probe.py')
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    with tempfile.TemporaryDirectory(prefix='fuxi-wlan-log-') as tmp:
        log = Path(tmp) / 'console.log'
        r = [0] * 128
        r[0:4] = [0x574c0006, 28, 28, 0]
        for step in range(17, 29):
            r[32 + step] = 0x1000 | step
        r[16], r[64], r[65] = 0x201, 2, 0x200
        log.write_text('\n'.join('prefix FUXI WLAN: m6 r[%3d] ' % i +
                                  ' '.join(f'{v:08x}' for v in r[i:i+8]) for i in range(0,128,8)))
        reports, seen = probe.from_xnu_log(log)
        assert seen == {'m6'} and reports['m6'] == r
        decoded = probe.decode_m6(r)
        assert 'step 28 OK' in decoded and 'amss20.bin' in decoded and 'state=M0' in decoded
        r[108:110] = [0x4d366500, 129]
        r[67:69] = [0xffffffff, 0xef0e0003]
        r[124:128] = [0xffffffff, 0xef0e0003, 129, 1]
        decoded = probe.decode_m6(r)
        assert 'M6e sample 0: STATUS=0xffffffff field=3' in decoded
        assert 'PBL-error-evidence' in decoded and 'STATUS-all-ones' in decoded
        r[109] = 14
        assert 'config-lost, link-lost' in probe.decode_m6(r)
        failed = [0] * 128
        failed[2], failed[49] = 17, (17 << 8) | 1
        assert 'FAIL reason 1' in probe.decode_m6(failed)
        failed[49], failed[25] = 0x1105, 2
        assert 'RC route: table in use (bypass clear)' in probe.decode_m6(failed)
        failed[25] = 4
        assert 'RC route: table zero readback' in probe.decode_m6(failed)
        r[0], r[2], r[3], r[49] = 0x574c0005, 17, 0x1103, 0x1103
        r[11] = r[15] = 2
        assert 'SID 0x1401 type=FAULT' in probe.decode_m5(r)
        print('M6 offline log reconstruction/decoder and historical M5 decoder passed')


if __name__ == '__main__':
    main()
