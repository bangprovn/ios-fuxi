#!/usr/bin/env python3
"""Offline production IOSurface use traps, shared ABI, cleanup and seed accounting; lock-result plane layout."""
import importlib.util
from pathlib import Path
import re
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

# The lock result as IOSurface.framework (iOS 16.7, dyld cache 20H392) reads it. IOSurfaceClient keeps the result at
# +0x68, so client offset - 0x68 = result offset. From the disassembly of IOSurfaceClientGet*:
#   PlaneCount ldr w [x0,#0xa0]; BytesPerRow (no planes) [#0x90]; height [#0x8c]; base offset [#0x94]; base [#0x68]
#   per plane i, at x0 + i*0x80: width +0xd0, height +0xd4, offset +0xdc (BaseAddressOfPlane = base + it),
#   BytesPerRowOfPlane +0xe0, SizeOfPlane +0xe4, BytesPerElementOfPlane +0xe8, ElementWidth/Height +0xea/+0xeb;
#   ldrb +0x124 == 1 (compressed) makes BaseAddressOfPlane and BytesPerRowOfPlane answer 0;
#   plane index >= PlaneCount: 0, except plane 0 of a planeless surface (the surface's own row and offset).
# CoreVideo's CVPixelBufferBacking::getBytesPerRowOfPlane tail-calls IOSurfaceGetBytesPerRowOfPlane.
PLANES_PREFIX = r'''
#include <cassert>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>
#define SWEET_SURFACE_RESULT_SIZE 0xa68
#define SWEET_SURFACE_TIMESTAMPS 0x800
#define strlcpy test_strlcpy
static size_t test_strlcpy(char *d,const char *s,size_t n) {size_t l=strlen(s);if(n){size_t c=l<n-1?l:n-1;memcpy(d,s,c);d[c]=0;}return l;}
#define bzero(p,n) memset((p),0,(n))
static int argMode=-1;   /* -1 boot-arg absent */
static bool PE_parse_boot_argn(const char *name,void *v,int size) {
 assert(!strcmp(name,"sweetsurfplanerow") && size==4);if(argMode<0)return false;*(uint32_t *)v=(uint32_t)argMode;return true;}
struct IOLock {bool held=false;};
static IOLock surfaceLock,*sweet_surface_lock=&surfaceLock;
static void IOLockLock(IOLock *p) {assert(!p->held);p->held=true;}
static void IOLockUnlock(IOLock *p) {assert(p->held);p->held=false;}
static const char *procName="mediaserverd";
static void proc_selfname(char *buf,int size) {test_strlcpy(buf,procName,(size_t)size);}
static std::vector<std::string> logs;
__attribute__((format(printf,1,2))) static void IOLog(const char *fmt,...) {
 char b[512];va_list ap;va_start(ap,fmt);vsnprintf(b,sizeof(b),fmt,ap);va_end(ap);logs.push_back(b);}
#define SWEET_SURFACE_MAX_PLANES 20
'''

PLANES_TEST = r'''
struct SweetSurface {
 uint32_t id=812,width=336,height=200,bytesPerRow=0,offset=0,pixelFormat=0x34323076,allocSize=0x1d000;
 uint16_t bytesPerElement=0;uint8_t elementWidth=1,elementHeight=1;
 uint32_t cacheMode=0,planeCount=2,parentID=0;
 sweet_surface_plane planes[SWEET_SURFACE_MAX_PLANES]={};
};
struct Map {uint64_t a;uint64_t getAddress(){return a;}};
struct SweetSurfaceRef {SweetSurface *surface;Map *data,*state;};
'''

PLANES_MAIN = r'''
static uint32_t rd32(const uint8_t *c,uint32_t o) {uint32_t v;memcpy(&v,c+o,4);return v;}
static uint64_t rd64(const uint8_t *c,uint32_t o) {uint64_t v;memcpy(&v,c+o,8);return v;}
/* IOSurfaceClient as the framework lays it out: the result copied to +0x68. */
struct Client {uint8_t b[0x68+SWEET_SURFACE_RESULT_SIZE]={};};
static uint32_t planeCount(const Client &c) {return rd32(c.b,0xa0);}
static uint64_t bytesPerRowOfPlane(const Client &c,uint32_t i) {
 if(planeCount(c)<=i)return i==0?rd32(c.b,0x90):0;
 if(c.b[i*0x80+0x124]==1)return 0;return rd32(c.b,i*0x80+0xe0);}
static uint64_t baseAddressOfPlane(const Client &c,uint32_t i) {
 if(planeCount(c)<=i)return i==0?rd64(c.b,0x68)+rd32(c.b,0x94):0;
 if(c.b[i*0x80+0x124]==1)return 0;return rd64(c.b,0x68)+rd32(c.b,i*0x80+0xdc);}
static uint32_t widthOfPlane(const Client &c,uint32_t i) {return planeCount(c)>i?rd32(c.b,i*0x80+0xd0):0;}
static uint32_t heightOfPlane(const Client &c,uint32_t i) {return planeCount(c)>i?rd32(c.b,i*0x80+0xd4):i?0:rd32(c.b,0x8c);}
static uint32_t sizeOfPlane(const Client &c,uint32_t i) {return planeCount(c)>i?rd32(c.b,i*0x80+0xe4):0;}
static Client lockResult(SweetSurface &s,uint64_t base) {
 Map data{base},state{0x2000000};SweetSurfaceRef ref{&s,&data,&state};Client c;
 sweet_surface_fill_result(&s,&ref,c.b+0x68);return c;}
/* H264SW.videocodec +0x13894: the destination advances by (2*row - width) as a 32-bit value added zero-extended. */
static uint64_t h264RowPairStep(uint64_t row,uint32_t width) {return width+(uint64_t)(uint32_t)(2*(uint32_t)row-width);}

int main(int argc,char **argv) {
 argMode=argc>1?atoi(argv[1]):-1;
 /* mediaserverd's decoder output (r-tour-k0ee, h-fit2b): 420v 336x200, planes at +0x40 and +0x13840, row 384. */
 SweetSurface s;s.planes[0]={336,208,384,0x40,384*208,1,1,1};s.planes[1]={168,104,384,0x13840,384*104,2,1,1};
 Client c=lockResult(s,0x106b4c000);
 assert(planeCount(c)==2 && widthOfPlane(c,0)==336 && heightOfPlane(c,1)==104 && sizeOfPlane(c,1)==384*104);
 assert(baseAddressOfPlane(c,0)==0x106b4c040 && baseAddressOfPlane(c,1)==0x106b5f840);   /* the crash's x7 and plane 1 */
 for(uint32_t i=0;i<2;i++)assert(c.b[i*0x80+0x124]==0);   /* never "compressed" */
 uint16_t bpe;memcpy(&bpe,c.b+0x80+0xe8,2);assert(bpe==2 && c.b[0xea]==1 && c.b[0xeb]==1);
 if(argMode==0){
  /* The layout before the fix: the row sat at +8, every plane row reads 0, and H264SW's step is
   * (uint32)(0 - 336) + 336 = 4 GiB: the crash's x10 = x7 + 0x100000000. */
  assert(rd32(c.b,0xd8)==384 && bytesPerRowOfPlane(c,0)==0 && bytesPerRowOfPlane(c,1)==0);
  assert(h264RowPairStep(bytesPerRowOfPlane(c,0),336)==0x100000000ull);
  puts("PASS: sweetsurfplanerow=0 keeps the old plane layout (row 0, the 4 GiB H264SW step)");return 0;
 }
 assert(rd32(c.b,0xd8)==0 && rd32(c.b,0x80+0xd8)==0);   /* +8 of a plane stays reserved */
 assert(bytesPerRowOfPlane(c,0)==384 && bytesPerRowOfPlane(c,1)==384);
 assert(h264RowPairStep(bytesPerRowOfPlane(c,0),336)==2*384);   /* two rows down, inside the plane */
 /* A planeless surface still answers its own row and offset for plane 0. */
 SweetSurface flat;flat.planeCount=0;flat.bytesPerRow=4608;flat.offset=0;flat.pixelFormat=0x42475241;flat.width=1125;flat.height=2436;
 Client f=lockResult(flat,0x200000000);
 assert(planeCount(f)==0 && bytesPerRowOfPlane(f,0)==4608 && bytesPerRowOfPlane(f,1)==0 && baseAddressOfPlane(f,0)==0x200000000);
 /* Each process gets at most 4 plane lines, 16 processes at most; lines read the rows back from the result. */
 logs.clear();
 for(int k=0;k<6;k++)sweet_surface_plane_log(&s,c.b+0x68,k==5);
 assert(logs.size()==4 && !surfaceLock.held);
 assert(logs[0]=="SWEET SURF: planes mediaserverd id 812 fmt 0x34323076 336x200 n 2 p0 row 384 off 64 p1 row 384 off 79936\n");
 static char names[20][16];
 for(int p=0;p<20;p++){snprintf(names[p],sizeof(names[p]),"proc%d",p);procName=names[p];sweet_surface_plane_log(&s,c.b+0x68,true);}
 assert(logs.size()==4+15);   /* mediaserverd took the first of 16 slots */
 assert(logs.back().find(" cached\n")!=std::string::npos);
 for(auto &l:logs)assert(l.size()<200);
 puts(argMode<0?"PASS: plane rows at +0x10 where IOSurface.framework reads them (default), H264SW step two rows, planeless plane 0, bounded plane lines":
      "PASS: sweetsurfplanerow=1 plane rows at +0x10");
 return 0;
}
'''


def planes_model(source, header, tmp):
    plane = re.search(r'struct sweet_surface_plane \{.*?\};', header, re.S)[0]
    methods = ''.join(unixnu.function(source, name) for name in (
        'put32', 'put64', 'sweet_surface_plane_row_fixed', 'sweet_surface_fill_result'))
    start = source.index('#define SWEET_SURFACE_PLANE_PROCS')
    end = source.index('\n}\n', source.index('sweet_surface_plane_log(SweetSurface *s')) + 3
    methods += source[start:end] + '\n'
    assert re.search(r'if \(s->planeCount > 1\) \{\n\t+sweet_surface_plane_log\(s, out, existing\);', source)
    assert source.count('sweet_surface_fill_result(s, ref, out);') == 1
    path = Path(tmp) / 'planes.cpp'
    path.write_text(PLANES_PREFIX + plane + '\n' + PLANES_TEST + methods + PLANES_MAIN)
    exe = Path(tmp) / 'planes'
    subprocess.run(['clang++', '-std=c++17', '-Wall', '-Wextra', '-Werror',
                    '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                    str(path), '-o', str(exe)], check=True)
    for mode in ([], ['1'], ['0']):
        subprocess.run([str(exe)] + mode, check=True)


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
        planes_model(source, (ROOT / 'xnu/fuxi-scanout/SweetSurface.h').read_text(), tmp)


if __name__ == '__main__':
    main()
