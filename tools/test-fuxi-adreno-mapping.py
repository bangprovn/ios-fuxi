#!/usr/bin/env python3
"""Exercise 0096's actual mapping/startup functions with fallible VM/pmap stubs.

No device access. Models VA exhaustion and page-table shortages, not kernel
allocator internals, real page attributes, SMMU, caches or worker scheduling.
"""
from pathlib import Path
import importlib.util
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('submit', ROOT / 'tools/test-fuxi-adreno.py')
submit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(submit)

PREFIX = r'''
static unsigned fg_scanout_requested, fg_scanout_panel120_requested, fg_scanout_double_requested, fg_scanout_triple_requested;
static unsigned fg_scanout_paneldcs_requested;
#include <assert.h>
#define FG_GUARD
static unsigned fg_abi_enabled;
static void *fg_ring_lock;
#include <stdlib.h>
static void panic(const char *fmt, ...) { (void)fmt; abort(); }
static void *IOLockAlloc(void) { return (void *)1; }
#include <stdint.h>
#include <stdio.h>
#include <stdarg.h>
#include <string.h>
typedef uintptr_t vm_offset_t;
typedef uint64_t vm_size_t;
typedef int kern_return_t;
typedef void *thread_call_param_t;
typedef struct { void (*func)(void *,void *); } thread_call_data_t;
#define PAGE_SIZE 16384u
#define PAGE_MASK (PAGE_SIZE-1)
#define KERN_SUCCESS 0
#define KERN_INVALID_ARGUMENT 4
#define KERN_RESOURCE_SHORTAGE 6
#define KERN_NO_SPACE 3
#define KMA_VAONLY 1
#define KMA_KOBJECT 2
#define KMA_NOPAGEWAIT 4
#define VM_KERN_MEMORY_IOKIT 7
#define VM_PROT_READ 1
#define VM_PROT_WRITE 2
#define VM_PROT_NONE 0
#define VM_WIMG_IO 5
#define TRUE 1
#define PMAP_OPTIONS_NOWAIT 8
#define kernel_map 1
#define kernel_pmap 2
#define THREAD_CALL_PRIORITY_KERNEL 1
#define THREAD_CALL_OPTIONS_ONCE 1
#define NSEC_PER_SEC 1000000000u
#define NSEC_PER_MSEC 1000000u
static unsigned fg_dcvs_on, fg_dcvs_cap, fg_dcvs_hold_ms, dcvs_starts;
static uint64_t fg_dcvs_hold;
static void fg_dcvs_start(void) {dcvs_starts++;}
static void __attribute__((unused)) dcvs_use(void) {uint64_t r;(void)fgd_want(0,0,0,0,&r);}
static void nanoseconds_to_absolutetime(uint64_t ns, uint64_t *t) {*t=ns;}
#define kSuccess 0
typedef int DTEntry;
static uint64_t fg_faults;
static unsigned fg_state, fg_started, fg_limit, fg_force, fg_access_failed;
static unsigned fg_clock_measure, g_gpu_selected, clock_logs;
static uint64_t g_gpu_ddr_requested;
static void fg_clock_log(void) {clock_logs++;}
static int fuxi_gpu_ready(void) {return fg_state==1;}
static unsigned g_bank, g3_bank, g3_asid, g3_next, gpu_stage;
static uint32_t gpu_report[128], gpu_extra[16][128];
#define G3_PT (FG_SCRATCH+0x1200000)
#define G3_FENCE_PA (FG_SCRATCH+0x1329000)
#define G4_FB_PA 0xb8000000u
#define G4_FB_VA 0x41000000u
static unsigned valid_handoff, abi_inits, abi_publishes, abi_fail;
static int fg_abi_init(void) { assert(fg_state==0);abi_inits++;return !abi_fail; }
static void fuxi_gpu_abi_publish(void) { assert(fg_state==1 && abi_inits==1);abi_publishes++; }
static unsigned gpu_calls, alloc_calls, enter_calls, remove_calls, free_calls, enqueued;
static unsigned fail_alloc, fail_enter, invalid_reservation;
static size_t live, cursor, last_size, removed;
static uintptr_t last_va;
static uint64_t first_pa, next_pa;
static unsigned char arena[64*1024*1024] __attribute__((aligned(PAGE_SIZE)));
vm_offset_t gPhysBase = FG_RAM_BASE;
vm_size_t gPhysSize = FG_RAM_SIZE;
static uint64_t reservation[2] = {FG_SCRATCH,FG_SCRATCH_SIZE};
static char last_log[256];
static int model_printf(const char *fmt, ...)
{
 va_list ap; va_start(ap,fmt); vsnprintf(last_log,sizeof(last_log),fmt,ap); va_end(ap); return 0;
}
#define printf model_printf
static int kmem_alloc(int map,vm_offset_t *va,vm_size_t size,unsigned flags,unsigned tag)
{
 assert(map==kernel_map && flags==(KMA_VAONLY|KMA_KOBJECT|KMA_NOPAGEWAIT));
 assert(tag==VM_KERN_MEMORY_IOKIT && size && !(size&PAGE_MASK));
 if(++alloc_calls==fail_alloc) return KERN_NO_SPACE;
 assert(cursor+size<=sizeof(arena));
 *va=(uintptr_t)arena+cursor; last_va=*va; last_size=size;
 cursor+=size; live+=size; next_pa=first_pa=0;
 return KERN_SUCCESS;
}
static int pmap_enter_options_addr(int map,vm_offset_t va,uint64_t pa,unsigned prot,
 unsigned fault,unsigned flags,unsigned wired,unsigned options,void *arg)
{
 assert(map==kernel_pmap && prot==(VM_PROT_READ|VM_PROT_WRITE) && !fault);
 assert(flags==VM_WIMG_IO && wired==TRUE && options==PMAP_OPTIONS_NOWAIT && !arg);
 assert(va>=last_va && va<last_va+last_size && !(va&PAGE_MASK) && !(pa&PAGE_MASK));
 if(!first_pa) {
  next_pa=first_pa=pa;
  if(valid_handoff && pa==FG_SCRATCH) {
   struct fg_handoff *h=(struct fg_handoff *)(last_va+FG_HANDOFF-FG_SCRATCH);
   *h=(struct fg_handoff){.magic=FG_MAGIC,.version=1,.bytes=sizeof(*h),.base=FG_SCRATCH,.size=FG_SCRATCH_SIZE};
   for(unsigned i=0;i<3;i++) {
    uint8_t *fw=(uint8_t *)(last_va+FG_FW_BASE-FG_SCRATCH+i*FG_FW_SLOT);
    memset(fw,0x55,4);h->fw_size[i]=4;h->fw_hash[i]=fg_hash(fw,4);
   }
  }
 }
 assert(pa==next_pa && va-last_va==pa-first_pa); next_pa+=PAGE_SIZE;
 if(++enter_calls==fail_enter) return KERN_RESOURCE_SHORTAGE;
 return KERN_SUCCESS;
}
static void pmap_remove(int map,uintptr_t begin,uintptr_t end)
{
 assert(map==kernel_pmap && begin==last_va && end==begin+last_size);
 remove_calls++; removed=end-begin;
}
static void kmem_free(int map,uintptr_t va,size_t size)
{
 assert(map==kernel_map && va==last_va && size==last_size && removed==size);
 free_calls++; live-=size;
}
static int SecureDTLookupEntry(void *p,const char *s,DTEntry *e)
{ assert(!p && !strcmp(s,"/chosen")); *e=1; return 0; }
static int SecureDTGetProperty(DTEntry e,const char *s,const void **p,unsigned *n)
{ assert(e==1 && !strcmp(s,"fuxi-gpu-memory")); *p=reservation; *n=invalid_reservation?0:16; return 0; }
static void gpu_probe(unsigned stage) { (void)stage; gpu_calls++;if(valid_handoff) {gpu_extra[5][4]=1;gpu_extra[13][2]=1;} }
static unsigned g3_read(uint64_t a) {
 if(valid_handoff) {
  if(a==0xae05014) return G4_FB_PA;
  if(a==0xae05024) return 4320;
  if(a==0xae05000) return (2400u<<16)|1080;
  if(a==0xae05030) return 0x237ff;
 }
 return 0;
}
static unsigned g3_gr(unsigned a) { (void)a; return 0; }
static unsigned fbMaps,failFbMap;
static int g_map_at(uint64_t p,unsigned *n,unsigned va,unsigned pa,unsigned size,unsigned attr)
{ (void)p;(void)n;assert(size==((4320*2400+4095)&~4095u) && attr==(1u<<6));
 assert((va==G4_FB_VA && pa==G4_FB_PA) || (fg_scanout_double_requested && va==G4_FB_VA+0xa00000u && pa==0xb8a00000u) || (fg_scanout_triple_requested && va==G4_FB_VA+0x1400000u && pa==0xb9400000u));return ++fbMaps!=failFbMap; }
static int fg_source_tables(void) { return 1; }
static int g4_tlb(uint32_t *p) { (void)p;return 0; }
static void clock_interval_to_deadline(unsigned n,unsigned unit,uint64_t *d)
{ assert(n==2 && unit==NSEC_PER_SEC); *d=123; }
static void thread_call_setup_with_options(thread_call_data_t *c,void (*f)(void *,void *),void *a,unsigned pri,unsigned opt)
{ assert(!a && pri==THREAD_CALL_PRIORITY_KERNEL && opt==THREAD_CALL_OPTIONS_ONCE); c->func=f; }
static int thread_call_enter_delayed(thread_call_data_t *c,uint64_t d)
{ assert(c->func && d==123); enqueued++;return 0; }
static unsigned enable_arg=1;
static uint64_t level_arg, ddr_arg;
static unsigned level_present=1, ddr_present, ddr_parses;
static int PE_parse_boot_argn(const char *name,void *p,size_t n)
{
 if(!strcmp(name,"fuxigpulevel")) {assert(n==8);if(level_present)memcpy(p,&level_arg,n);return level_present;}
 if(!strcmp(name,"fuxigpuddr")) {assert(n==8);ddr_parses++;if(ddr_present)memcpy(p,&ddr_arg,n);return ddr_present;}
 assert(n==4); if(!strcmp(name,"fuxigpu")) memcpy(p,&enable_arg,n); return 0;
}
'''

MODEL = r'''
#define COUNT (sizeof(fg_windows)/sizeof(fg_windows[0]))
static void reset(void)
{
 for(unsigned i=0;i<COUNT;i++) fg_windows[i].va=0;
 fg_mapped_bytes=fg_state=fg_started=fg_access_failed=0;
 gpu_calls=alloc_calls=enter_calls=remove_calls=free_calls=enqueued=0;
 fail_alloc=fail_enter=invalid_reservation=0;
 live=cursor=removed=0;
 fg_scanout_requested=fg_scanout_panel120_requested=fg_scanout_double_requested=fg_scanout_triple_requested=1;
 fg_scanout_paneldcs_requested=0;
 fg_limit=17; enable_arg=1; gPhysSize=FG_RAM_SIZE;
 valid_handoff=abi_inits=abi_publishes=abi_fail=fg_abi_enabled=fbMaps=failFbMap=0;
 level_arg=clock_logs=g_gpu_selected=fg_clock_measure=0;
 ddr_arg=g_gpu_ddr_requested=ddr_present=ddr_parses=0;level_present=1;last_log[0]=0;
}
int main(void)
{
 (void)g_gpu_bw_packet;(void)g_gpu_bw_index;(void)g_gpu_level;
 uint64_t total=0;
 for(unsigned i=0;i<COUNT;i++) total+=fg_windows[i].size;
 assert(total==0x3554000); /* Existing windows + optional DPU, DSI0 and buffers B/C windows. */
 reset();fg_scanout_requested=fg_scanout_panel120_requested=fg_scanout_double_requested=fg_scanout_triple_requested=0;assert(fg_map_windows()==KERN_SUCCESS);
 assert(live==total-49152-2*0x9e4000 && fg_mapped_bytes==total-49152-2*0x9e4000 && alloc_calls==COUNT-5);
 assert(!fuxi_gpu_nc_pointer(0xb8a00000,4320u*2400));
 assert(!fuxi_gpu_nc_pointer(0xb9400000,4320u*2400));
 assert(!fg_pointer(0xae16000) && !fg_pointer(0xae36000) && !fg_pointer(0xae94000));
 reset();fg_scanout_double_requested=fg_scanout_triple_requested=0;assert(fg_map_windows()==KERN_SUCCESS);
 assert(!fuxi_gpu_nc_pointer(0xb8a00000,4320u*2400) && live==total-2*0x9e4000);
 reset();fg_scanout_panel120_requested=0;assert(fg_map_windows()==KERN_SUCCESS);
 assert(live==total-16384 && fg_mapped_bytes==total-16384 && alloc_calls==COUNT-1);
 assert(fg_pointer(0xae16000) && fg_pointer(0xae36000) && !fg_pointer(0xae94000));
 /* fuxipaneldcs alone also maps DSI0 (its DCS sends use the same command FIFO). */
 reset();fg_scanout_panel120_requested=0;fg_scanout_paneldcs_requested=1;assert(fg_map_windows()==KERN_SUCCESS);
 assert(live==total && fg_pointer(0xae94000));
 reset(); assert(fg_map_windows()==KERN_SUCCESS);
 assert(live==total && fg_mapped_bytes==total && alloc_calls==COUNT && !free_calls);
 assert(enter_calls==total/PAGE_SIZE && strstr(last_log,"IO submap 0 bytes"));
 for(unsigned i=0;i<COUNT;i++) {
  struct fg_window *w=&fg_windows[i];
  assert(fg_pointer(w->pa)==w->va && fg_pointer(w->pa+w->size-1)==w->va+w->size-1);
  assert(fg_map_window(w)==KERN_INVALID_ARGUMENT); /* never map twice */
 }
 assert(!fg_pointer(FG_SCRATCH+FG_SCRATCH_SIZE));
 /* Fail VA reservation for EACH window, including MMIO, scratch and zap.
  * Invoke actual bring-up entry: failure must permanently select CPU before
  * handoff reads or a single hardware/probe operation. */
 for(unsigned i=1;i<=COUNT;i++) {
  reset(); fail_alloc=i; fg_thread(NULL,NULL);
  assert(fg_state==2 && !gpu_calls && alloc_calls==i && !free_calls);
  assert(!fg_windows[i-1].va && live==fg_mapped_bytes);
 }
 /* Fail EVERY physical page insertion, from first MMIO to final display
  * page. Partial mapping must be unpublished, fully removed and VA freed. */
 for(unsigned i=1;i<=total/PAGE_SIZE;i++) {
  reset(); fail_enter=i; fg_thread(NULL,NULL);
  assert(fg_state==2 && !gpu_calls && enter_calls==i);
  assert(remove_calls==1 && free_calls==1 && live==fg_mapped_bytes);
  assert(!fg_windows[alloc_calls-1].va);
 }
 reset(); invalid_reservation=1; fg_thread(NULL,NULL);
 assert(fg_state==2 && !alloc_calls && !gpu_calls);
 reset(); gPhysSize=FG_RAM_SIZE+PAGE_SIZE; fg_thread(NULL,NULL);
 assert(fg_state==2 && !alloc_calls && !gpu_calls);
 reset(); fg_thread(NULL,NULL); /* invalid cold handoff after all maps */
 assert(fg_state==2 && !gpu_calls && live==total);
 struct fg_window bad={1,PAGE_SIZE,0};
 reset(); assert(fg_map_window(&bad)==KERN_INVALID_ARGUMENT && !alloc_calls);
 bad=(struct fg_window){FG_SCRATCH,0,0};
 assert(fg_map_window(&bad)==KERN_INVALID_ARGUMENT && !alloc_calls);
 bad=(struct fg_window){UINT64_MAX-PAGE_MASK,PAGE_SIZE,0};
 assert(fg_map_window(&bad)==KERN_INVALID_ARGUMENT && !alloc_calls);
 reset(); enable_arg=0; fuxi_gpu_start(G4_FB_PA,1080,2400,4320); assert(!enqueued && !ddr_parses);
 reset(); fuxi_gpu_start(G4_FB_PA,1080,2400,4320);
 assert(enqueued==1 && !alloc_calls && !gpu_calls);
 fuxi_gpu_start(G4_FB_PA,1080,2400,4320); assert(enqueued==1);
 const uint64_t levels[]={0,1,2,6,7,8,9,UINT64_MAX,0x100000001ULL};
 for(unsigned i=0;i<sizeof(levels)/sizeof(levels[0]);i++) {
  reset();level_arg=levels[i];fuxi_gpu_start(G4_FB_PA,1080,2400,4320);
  assert(g_gpu_selected==(level_arg<=8?level_arg:0) && enqueued==1);
  if(level_arg>8)assert(strstr(last_log,"rejected fuxigpulevel=") && strstr(last_log,"allowed 0..8"));
 }
 // Absent and explicit zero DDR preserve all preexisting levels; overrides
 // parse at full width, reject index 9/negative/wide and respect bus-max.
 const uint64_t ddrs[]={0,0,1,2,3,4,5,6,7,8,9,UINT64_MAX,0x100000001ULL};
 const unsigned maxima[]={0,0,3,3,4,5,7,7,8};
 const uint32_t defaults[]={6074218,6074218,6074218,6074218,6074218,6074218,6074218,10687500,12449218};
 for(unsigned level=0;level<=8;level++)
 for(unsigned i=0;i<sizeof(ddrs)/sizeof(ddrs[0]);i++) {
  reset();level_arg=level;ddr_arg=ddrs[i];ddr_present=i!=0;
  fuxi_gpu_start(G4_FB_PA,1080,2400,4320);
  assert(g_gpu_selected==level && g_gpu_ddr_requested==ddr_arg && ddr_parses==1);
  unsigned expected=level<2 || ddr_arg>8?0:ddr_arg>maxima[level]?maxima[level]:ddr_arg;
  assert(g_gpu_ddr_clamp(level,g_gpu_ddr_requested)==expected);
  if(!expected)assert(g_gpu_ddr_row2(level,g_gpu_ddr_requested)==defaults[level]);
  if(ddr_arg>8)assert(strstr(last_log,"rejected fuxigpuddr=") && strstr(last_log,"allowed 0..8"));
 }
 reset();level_present=0;ddr_present=1;ddr_arg=8;
 fuxi_gpu_start(G4_FB_PA,1080,2400,4320);
 assert(!g_gpu_selected && !g_gpu_ddr_clamp(g_gpu_selected,g_gpu_ddr_requested));
 reset(); fuxi_gpu_start(G4_FB_PA,1080,2400,1); assert(fg_state==2 && !enqueued);
 /* The combined ready line must identify the selected request as a vote,
  * including legacy zero and every bounded GPU-CLOCK selection. */
 const unsigned ready_khz[]={220000,220000,295000,348000,401000,475000,550000,615000,680000};
 for(unsigned level=0;level<9;level++) {
  reset();valid_handoff=1;level_arg=level;
  fuxi_gpu_start(G4_FB_PA,1080,2400,4320);fg_thread(NULL,NULL);
  char expected[96];
  snprintf(expected,sizeof(expected),"requested %u kHz GPU vote (not measured)",ready_khz[level]);
  assert(fg_state==1 && clock_logs==1 && fbMaps==3 && strstr(last_log,expected));
 }
 for(unsigned doubleMode=0;doubleMode<2;doubleMode++) {
  reset();valid_handoff=1;fg_scanout_triple_requested=0;fg_scanout_double_requested=doubleMode;
  fuxi_gpu_start(G4_FB_PA,1080,2400,4320);fg_thread(NULL,NULL);
  assert(fg_state==1 && fbMaps==doubleMode+1);
 }
 reset();fg_scanout_triple_requested=0;assert(fg_map_windows()==KERN_SUCCESS);
 assert(live==total-0x9e4000 && fuxi_gpu_nc_pointer(0xb8a00000,4320u*2400) && !fuxi_gpu_nc_pointer(0xb9400000,4320u*2400));
 reset();valid_handoff=1;failFbMap=3;
 fuxi_gpu_start(G4_FB_PA,1080,2400,4320);fg_thread(NULL,NULL);
 assert(fg_state==2 && fbMaps==3 && !abi_publishes); // C mapping fails closed
 assert(0xb9400000u+0x9e4000u<=0xbab00000u && 0xb8a00000u+4320u*2400<=0xb9400000u);
 reset();valid_handoff=1;failFbMap=2;
 fuxi_gpu_start(G4_FB_PA,1080,2400,4320);fg_thread(NULL,NULL);
 assert(fg_state==2 && fbMaps==2 && !abi_publishes); // B mapping fails closed
 assert(0xb8a00000u+0x9e4000u<=0xbab00000u && 0xb8000000u+4320u*2400<=0xb8a00000u);
 /* ABI MMIO/table setup must precede publishing ready to the display;
  * service publication must follow it. Failure only withholds the ABI. */
 for(unsigned fail=0;fail<2;fail++) {
  reset();valid_handoff=fg_abi_enabled=1;abi_fail=fail;
  fg_thread(NULL,NULL);
  assert(fg_state==1 && abi_inits==1 && abi_publishes==!fail && clock_logs==1);
 }
#undef printf
 printf("PASS: %zu windows, %llu mapped bytes, all %llu page-insertion failures + VA failures, partial unwind, CPU fallback, static delayed startup\n",
  COUNT,(unsigned long long)total,(unsigned long long)(total/PAGE_SIZE));
 return 0;
}
'''


def main():
    adapter = submit.sync.added_file('fuxi_adreno.c')
    # No NOFAIL mapping/thread-creation primitive may be reintroduced.
    code = re.sub(r'/\*.*?\*/', '', adapter, flags=re.S)
    assert not re.search(r'\b(ml_io_map\w*|io_map|pmap_map\w*|kernel_thread_start\w*|thread_call_allocate\w*|thread_call_func_delayed|KMA_NOFAIL)\b', code)
    declarations = re.search(r'struct fg_window.*?static uint64_t fg_mapped_bytes;', adapter, re.S)[0]
    windows = [(int(a, 16), int(b, 16)) for a, b in re.findall(r'\{(0x[0-9a-f]+),(0x[0-9a-f]+),0\}', adapter)]
    windows.append((0x8f0000000, 0x2000000))
    for i, (a, n) in enumerate(windows):
        assert not ((a | n) & 16383)
        for b, m in windows[i+1:]:
            assert a+n <= b or b+m <= a, 'overlapping platform mappings'
    functions = ''.join(submit.function(adapter, n) for n in
                        ('fg_map_window', 'fg_map_windows', 'fuxi_gpu_nc_pointer', 'fg_pointer', 'fg_pointer_range', 'fg_fail'))
    functions += 'static thread_call_data_t fg_init_call;\n#define G_PTR(pa) fg_pointer(pa)\n#define G_PTR_RANGE(pa, bytes) fg_pointer_range(pa, bytes)\n'
    functions += ''.join(submit.function(adapter, n) for n in ('fg_thread', 'fuxi_gpu_start'))
    handoff = (ROOT / 'shim/fuxi-probe/gpu-handoff.h').read_text()
    clock = (ROOT / 'shim/fuxi-probe/gpu-clock.h').read_text() + (ROOT / 'xnu/fuxi-adreno/fuxi_gpu_dcvs.h').read_text()
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / 'mapping.c'
        path.write_text('#include <stdint.h>\n' + handoff + clock + PREFIX + declarations + functions + MODEL)
        exe = Path(d) / 'mapping'
        subprocess.run(['clang', '-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                        '-fsanitize=address,undefined', str(path), '-o', str(exe)], check=True)
        subprocess.run([str(exe)], check=True)


if __name__ == '__main__':
    main()
