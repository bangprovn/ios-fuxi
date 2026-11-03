#!/usr/bin/env python3
"""Compile the patched Turnip GPU address allocator and BO cache (mesa-fuxi-bo-cache.patch) on Linux and exercise them.

Supply external Mesa and the hash-verified UniXNU text patches, as for test-fuxi-turnip-hostimport.py. Checks, on the actual
functions: random allocate/allocate-at/free never overlaps, never leaves the range, keeps free + used bytes equal to the range
and merges back to one hole; size classes; and that a cached BO is handed back only to a request with the same permissions and
cached-coherent attribute and at most a quarter smaller, while a flush returns every entry to the kernel.
"""
import argparse
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def function(source, name):
    at = source.index('\n'+name+'(')
    start = source.rfind('\n\n', 0, at)+2
    body = source.index('{', at)
    end, depth = body+1, 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]+'\n'


PREFIX = r'''
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <random>
#include <pthread.h>
#include <vulkan/vulkan_core.h>
#include "NativeAdrenoABI.h"
#include "util/rb_tree.h"
#define mesa_loge(...) ((void)0)
#define p_atomic_read(p) (*(p))
static uint64_t os_time_get_nano() {return 0;}
using kern_return_t=int;using mach_vm_address_t=uint64_t;
constexpr int KERN_SUCCESS=0,kIOReturnBusy=1;
struct tu_device {int vk;void *darwin_retire;};
static struct {int conn;} dw={0};
static unsigned bo_frees,cpu_unmaps,cpu_leases,iova_frees;
static VkResult vk_device_set_lost(int *,const char *,...) {return VK_ERROR_DEVICE_LOST;}
static int mach_task_self() {return 1;}
static int IOConnectUnmapMemory64(int,uint32_t,int,uint64_t) {cpu_unmaps++;return KERN_SUCCESS;}
static int dw_call(unsigned method,const void *,size_t,void *,size_t) {
 if(method==NativeAdrenoMethodBoCpuUnmap) {cpu_leases++;return KERN_SUCCESS;}
 assert(method==NativeAdrenoMethodBoFree);bo_frees++;return KERN_SUCCESS;
}
static void darwin_free_iova(tu_device *,uint64_t,uint64_t) {iova_frees++;}
'''

TEST = r'''
static uint64_t free_bytes(darwin_va *va) {
 uint64_t n=0,last_end=0;bool first=true;
 for(rb_node *x=rb_tree_first(&va->address);x;x=rb_node_next(x)) {
  const darwin_va_hole *h=rb_node_data(struct darwin_va_hole,x,by_address);
  assert(h->size && (first || h->start>last_end));  // sorted, disjoint and never touching (merged)
  first=false;last_end=h->start+h->size;n+=h->size;
 }
 return n;
}
int main() {
 const uint64_t base=0x100000,range=uint64_t(1)<<30;
 darwin_va va={};rb_tree_init(&va.address);rb_tree_init(&va.size);
 assert(darwin_va_add(&va,base,range) && va.holes==1);
 std::map<uint64_t,uint64_t> live;uint64_t used=0;
 std::mt19937_64 rng(7);
 auto overlaps=[&](uint64_t a,uint64_t n) {
  auto it=live.upper_bound(a);
  if(it!=live.end() && it->first<a+n) return true;
  if(it!=live.begin() && (--it)->first+it->second>a) return true;
  return false;
 };
 unsigned refused=0;
 for(unsigned op=0;op<200000;op++) {
  unsigned what=rng()%10;
  if(what<5 || live.empty()) {
   uint64_t n=(1+rng()%2048)*4096;bool high=rng()&1;
   uint64_t a=darwin_va_alloc(&va,n,16384,high);
   if(!a) {refused++;continue;}
   assert(!(a%16384) && a>=base && a+n<=base+range && !overlaps(a,n));
   live[a]=n;used+=n;
  } else if(what<6) {
   uint64_t a=base+(rng()%(range/16384))*16384,n=(1+rng()%64)*4096;
   if(a+n>base+range) continue;
   bool ok=darwin_va_alloc_at(&va,a,n);
   assert(ok==!overlaps(a,n));
   if(ok) {live[a]=n;used+=n;}
  } else {
   auto it=live.begin();std::advance(it,rng()%live.size());
   darwin_va_free(&va,it->first,it->second);used-=it->second;live.erase(it);
  }
  if(!(op%997)) assert(free_bytes(&va)+used==range);
 }
 // A double free is refused and changes nothing.
 if(!live.empty()) {uint64_t before=free_bytes(&va);auto it=live.begin();darwin_va_free(&va,it->first,it->second);
  darwin_va_free(&va,it->first,it->second);assert(free_bytes(&va)==before+it->second);used-=it->second;live.erase(it);}
 for(auto &r:live) darwin_va_free(&va,r.first,r.second);
 assert(va.holes==1 && free_bytes(&va)==range);
 rb_tree_foreach_safe(struct darwin_va_hole,hole,&va.address,by_address) free(hole);

 assert(darwin_size_class(1)==4096 && darwin_size_class(16384)==16384 && darwin_size_class(16385)==20480);
 assert(darwin_size_class((1<<20)+1)==(1<<20)+(1<<18) && darwin_size_class(1<<20)==(1<<20));
 for(uint64_t s=1;s<(uint64_t(64)<<20);s+=s/7+4093) {uint64_t c=darwin_size_class(s);assert(c>=s && !(c%4096) && (s<=16384 || c-s<s/4));}

 darwin_retire_state retire={};tu_device dev={0,&retire};
 auto put=[&](uint64_t h,uint64_t n,bool ro,bool cached,void *map) {
  darwin_retired_bo bo={h,0x200000*h,n,map,map?7u:0u,0,true,ro,cached};
  assert(darwin_cache_put(&dev,&retire,&bo));
 };
 put(1,65536,false,false,nullptr);put(2,65536,false,true,(void *)0x1000);put(3,65536,true,false,nullptr);
 darwin_cached_bo hit;
 (void)darwin_memory_pressure;
 assert(darwin_cache_get(&retire,65536,false,true,true,&hit) && hit.handle==2);   // cached-coherent request: only BO 2
 put(hit.handle,hit.size,hit.read_only,hit.cached,hit.mapping);
 assert(darwin_cache_get(&retire,65536,false,false,false,&hit) && hit.handle==1 && !hit.cached);
 assert(!darwin_cache_get(&retire,65536,false,false,false,&hit));     // the write-combine BO is gone; the cached one is not handed out
 assert(darwin_cache_get(&retire,65536,false,true,false,&hit) && hit.handle==2 && hit.cached && hit.mapping);
 assert(darwin_cache_get(&retire,53000,true,false,false,&hit) && hit.handle==3);  // up to a quarter larger is taken
 put(3,65536,true,false,nullptr);
 assert(!darwin_cache_get(&retire,52000,true,false,false,&hit));     // more than a quarter larger is not
 assert(!darwin_cache_get(&retire,65536,true,false,true,&hit));      // a zeroed reuse needs a CPU view
 put(4,1<<20,false,true,(void *)0x2000);
 assert(retire.cache_count==2 && retire.cache_bytes==65536+(1<<20));
 assert(darwin_cache_flush(&dev,&retire) && !retire.cache_count && !retire.cache_bytes && bo_frees==2 && iova_frees==2);
 assert(cpu_unmaps==1 && cpu_leases==1 && retire.cache_stats.flushes==1 && retire.cache_stats.evictions==2);
 free(retire.cache);
 printf("PASS: GPU address trees (200000 random operations, %u refused for room, merged back to one hole), size classes, "
        "BO cache keyed by permissions and cached-coherent attribute, quarter-size rule, flush\n",refused);
}
'''


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--mesa', type=Path, required=True)
    ap.add_argument('--patches', type=Path, required=True)
    args = ap.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        subprocess.run(['python3', str(ROOT/'tools/build-fuxi-turnip.py'), '--mesa', str(args.mesa.resolve()),
                        '--patches', str(args.patches.resolve()), '--build', str(out/'unused-build'), '--verify-inputs-only'], check=True)
        source = (args.mesa/'src/freedreno/vulkan/tu_knl_darwin.cc').read_text()
        start = source.index('struct darwin_retired_bo {')
        model = PREFIX + source[start:source.index('\nstatic kern_return_t', start)]
        model += ''.join(function(source, name) for name in (
            'darwin_va_cmp_address', 'darwin_va_cmp_size', 'darwin_va_smallest_fit', 'darwin_va_hole_at_or_below', 'darwin_va_add',
            'darwin_va_del', 'darwin_va_carve', 'darwin_va_alloc', 'darwin_va_alloc_at', 'darwin_va_free', 'darwin_release_native',
            'darwin_size_class', 'darwin_cache_log', 'darwin_cache_evict', 'darwin_cache_put', 'darwin_cache_get', 'darwin_cache_flush'))
        model += TEST
        path = out/'bocache.cpp'
        path.write_text(model)
        flags = ['-O1', '-g', '-Wall', '-Wextra', '-Werror', '-Wno-missing-field-initializers', '-Wno-unused-parameter',
                 '-fsanitize=address,undefined', '-I'+str(ROOT/'xnu/fuxi-adreno'), '-I'+str(args.mesa/'include'),
                 '-I'+str(args.mesa/'src'), '-I'+str(args.mesa/'src/util')]
        subprocess.run(['clang', '-std=c11', '-c', *flags, str(args.mesa/'src/util/rb_tree.c'), '-o', str(out/'rb_tree.o')], check=True)
        subprocess.run(['clang++', '-std=c++17', *flags, str(path), str(out/'rb_tree.o'), '-o', str(out/'bocache')], check=True)
        subprocess.run([str(out/'bocache')], check=True, timeout=120)


if __name__ == '__main__':
    main()
