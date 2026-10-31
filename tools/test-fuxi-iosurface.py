#!/usr/bin/env python3
"""Offline production IOSurface use traps, shared ABI, cleanup and seed accounting."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('unixnu', ROOT / 'tools/test-fuxi-unixnu.py')
unixnu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(unixnu)

PREFIX = r'''
#include <cassert>
#include <cstdint>
#include <cstring>
#include <cstdio>
#include <vector>
#define FUXI 1
#define SWEET_SURFACE_STATE_USE 0x18
#define SWEET_SURFACE_STATE_SEED 0xc
#define SWEET_SURFACE_USE_MAX 65535u
#define kIOReturnSuccess 0
#define kIOReturnBadArgument 1
#define kIOReturnNotFound 2
#define OSSafeReleaseNULL(p) do {if(p){p->release();p=nullptr;}}while(0)
using IOReturn=int;using SInt32=int32_t;using user_addr_t=uintptr_t;
struct IOLock {bool held=false;};
static void IOLockLock(IOLock *p) {assert(!p->held);p->held=true;}
static void IOLockUnlock(IOLock *p) {assert(p->held);p->held=false;}
static IOLock surfaceLock,*sweet_surface_lock=&surfaceLock;
static unsigned displayWakes;static void sweet_display_wake() {assert(!surfaceLock.held);displayWakes++;} /* fuxiearly bit 2: the last display drop wakes lease waiters */
struct Buffer {alignas(8) uint8_t bytes[64]={};void *getBytesNoCopy(){return bytes;}};
struct OSObject {void free(){}};
struct SweetSurface : OSObject {
 uint32_t id=1,useCounts[2]={},displayUses=0;bool usePoisoned=false;Buffer *state;
 unsigned released=0,put=0;void release(){released++;}
};
struct Map {unsigned released=0;void release(){released++;}};
static void sweet_surface_put(SweetSurface *s) {s->put++;}
struct SweetSurfaceRef : OSObject {
 SweetSurface *surface;Map *data=nullptr,*state=nullptr;uint32_t count=1,uses[2]={};
 void free();void release(){free();}
};
struct OSArray {
 std::vector<SweetSurfaceRef *> values;
 unsigned getCount(){return values.size();}
 void *getObject(unsigned i){return values[i];}
 void flushCollection(){for(auto *r:values)r->release();values.clear();}
};
struct IOSurfaceRootUserClient {
 IOLock *fLock;OSArray *fRefs;bool terminated=false;
 void terminate(){terminated=true;}
 SweetSurfaceRef *findRef(uint32_t,unsigned *);
 IOReturn useCount(uint32_t,uint32_t,bool);
 IOReturn trapIncrementUse(void *,void *,void *,void *,void *,void *);
 IOReturn trapDecrementUse(void *,void *,void *,void *,void *,void *);
 IOReturn clientClose();
};
static int32_t OSIncrementAtomic(volatile int32_t *p) {return (*p)++;}
static int copyout(const void *src,user_addr_t dst,unsigned n) {if(dst==1)return 1;memcpy((void *)dst,src,n);return 0;}
'''

TEST = r'''
static uint64_t shared(Buffer &b) {return __atomic_load_n((uint64_t *)(b.bytes+0x18),__ATOMIC_ACQUIRE);}
static IOReturn trap(IOSurfaceRootUserClient &c,bool add,unsigned id,unsigned category) {
 return add?c.trapIncrementUse((void *)(uintptr_t)id,(void *)(uintptr_t)category,nullptr,nullptr,nullptr,nullptr):
     c.trapDecrementUse((void *)(uintptr_t)id,(void *)(uintptr_t)category,nullptr,nullptr,nullptr,nullptr);
}
int main() {
 Buffer b;SweetSurface s;s.state=&b;IOLock aLock,cLock;OSArray aRefs,cRefs;
 SweetSurfaceRef a,c;a.surface=c.surface=&s;aRefs.values.push_back(&a);cRefs.values.push_back(&c);
 IOSurfaceRootUserClient first{&aLock,&aRefs},second{&cLock,&cRefs};
 assert(trap(first,true,1,0)==0 && shared(b)==1);
 assert(trap(second,true,1,0)==0 && shared(b)==2);
 assert(trap(first,true,1,1)==0 && shared(b)==(1ull<<32)+2);
 assert(trap(first,false,1,0)==0 && shared(b)==(1ull<<32)+1);
 assert(trap(first,false,1,0)==kIOReturnBadArgument && shared(b)==(1ull<<32)+1);
 assert(trap(first,true,1,2)==kIOReturnBadArgument && trap(first,true,99,0)==kIOReturnNotFound);
 assert(first.trapIncrementUse((void *)(1ull<<32),(void *)0,nullptr,nullptr,nullptr,nullptr)==kIOReturnBadArgument);
 assert(first.trapDecrementUse((void *)1,(void *)(1ull<<32),nullptr,nullptr,nullptr,nullptr)==kIOReturnBadArgument);
 // User decrements cannot consume display references or another client's uses.
 for(unsigned i=0;i<4;i++)assert(sweet_surface_display_hold(&s));
 assert(trap(first,false,1,0)==kIOReturnBadArgument && s.displayUses==4 && shared(b)==(1ull<<32)+5);
 assert(first.clientClose()==0 && first.terminated && shared(b)==5 && !a.surface);
 assert(second.clientClose()==0 && second.terminated && shared(b)==4 && !c.surface && s.put==2 && s.released==2);
 for(unsigned i=0;i<4;i++){sweet_surface_display_drop(&s);assert(displayWakes==(i==3));}
 assert(shared(b)==0);sweet_surface_display_drop(&s);assert(shared(b)==0 && displayWakes==1);
 // Both aggregate counters saturate by refusal, never wrap or falsely report idle.
 a.surface=&s;a.uses[1]=0;a.uses[0]=s.useCounts[0]=SWEET_SURFACE_USE_MAX;aRefs.values.push_back(&a);
 sweet_surface_use_publish(&s);assert(trap(first,true,1,0)==kIOReturnBadArgument && shared(b)==SWEET_SURFACE_USE_MAX);
 s.displayUses=SWEET_SURFACE_USE_MAX;sweet_surface_use_publish(&s);
 assert(!sweet_surface_display_hold(&s) && shared(b)==2*SWEET_SURFACE_USE_MAX);
 assert(first.clientClose()==0 && shared(b)==SWEET_SURFACE_USE_MAX);
 s.displayUses=0;sweet_surface_use_publish(&s);
 // Failed/uncertain DMA remains in-use after every record reference is dropped.
 assert(sweet_surface_display_hold(&s));sweet_surface_display_poison(&s);sweet_surface_display_drop(&s);
 assert(shared(b)==1 && !s.displayUses && !s.useCounts[0] && !s.useCounts[1]);
 // Use accounting never changes seeds; writer seed operations never change use state.
 uint32_t seed=123;memcpy(b.bytes+0xc,&seed,4);
 assert(sweet_surface_display_hold(&s) && sweet_surface_seed_out(&s,&seed,false)==0 && seed==123);
 assert(sweet_surface_seed_out(&s,&seed,true)==0 && seed==124 && shared(b)==2);
 assert(sweet_surface_seed_out(&s,(void *)1,false)==kIOReturnBadArgument && shared(b)==2);
 sweet_surface_display_drop(&s);assert(shared(b)==1 && *sweet_surface_seed(&s)==124);
 puts("PASS: actual IOSurface category traps, aligned shared state ABI, cross-client isolation, close cleanup, caps/underflow, display holds, quarantine and independent seeds");
}
'''


def main():
    source = (ROOT / 'xnu/fuxi-scanout/SweetSurface.cpp').read_text()
    methods = ''.join(unixnu.function(source, name) for name in (
        'sweet_surface_use_publish', 'sweet_surface_display_poison', 'sweet_surface_display_hold',
        'sweet_surface_display_drop', 'SweetSurfaceRef::free', 'IOSurfaceRootUserClient::useCount',
        'IOSurfaceRootUserClient::trapIncrementUse', 'IOSurfaceRootUserClient::trapDecrementUse',
        'IOSurfaceRootUserClient::clientClose', 'sweet_surface_seed_out'))
    # findRef and seed return pointers, outside the shared extractor's scalar set.
    for name, signature in [('IOSurfaceRootUserClient::findRef', 'SweetSurfaceRef *'),
                            ('sweet_surface_seed', 'static uint32_t *')]:
        start = source.index(signature+'\n'+name+'(')
        end = source.index('\n}\n', start)+3
        methods = source[start:end]+'\n'+methods
    assert '[kSweetSurfaceTrapIncrementUse]  = { NULL, (IOTrap) & IOSurfaceRootUserClient::trapIncrementUse }' in source
    assert '[kSweetSurfaceTrapDecrementUse]  = { NULL, (IOTrap) & IOSurfaceRootUserClient::trapDecrementUse }' in source
    with tempfile.TemporaryDirectory(prefix='fuxi-iosurface-test-') as tmp:
        path = Path(tmp) / 'surface.cpp'
        path.write_text(PREFIX+methods+TEST)
        exe = Path(tmp) / 'surface'
        subprocess.run(['clang++', '-std=c++17', '-Wall', '-Wextra', '-Werror',
                        '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                        str(path), '-o', str(exe)], check=True)
        subprocess.run([str(exe)], check=True)


if __name__ == '__main__':
    main()
