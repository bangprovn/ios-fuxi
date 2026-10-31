#!/usr/bin/env python3
"""Exercise the actual M4 backend on an external software ICD, ASan/UBSan."""
import argparse
import importlib.util
import os
import re
from pathlib import Path
import subprocess
import tempfile
ROOT=Path(__file__).resolve().parents[1]
TEST=r'''
#include <atomic>
#include <cstdint>
#include <ctime>
static std::atomic<unsigned> cpuReads{0};
static uint64_t countedCPU(){++cpuReads;timespec t{};if(clock_gettime(CLOCK_THREAD_CPUTIME_ID,&t))return UINT64_MAX;return uint64_t(t.tv_sec)*1000000000ULL+t.tv_nsec;}
#define FUXI_MTL_THREAD_CPU_NS countedCPU
#include "fuxi-metal/Backend.hpp"
#include <cassert>
#include <cstdio>
static void verify(fuxi::Buffer *b,size_t begin,size_t n,uint8_t value) {
 for(size_t i=begin;i<begin+n;i++)if(((uint8_t *)b->contents)[i]!=value)throw std::runtime_error("bad byte "+std::to_string(i));
}
int main() {
 try {
  for(unsigned reopen=0;reopen<2;reopen++) {
   fuxi::Backend backend;
   try {backend.allocate(0);assert(false);}catch(const std::runtime_error&) {}
   try {backend.allocate(fuxi::Backend::maxBufferLength+1);assert(false);}catch(const std::runtime_error&) {}
   auto *src=backend.allocate(65536),*dst=backend.allocate(65536);
   for(unsigned i=0;i<16;i++) {
    uint8_t value=uint8_t(0xa0+i);memset(src->contents,0x39,src->length);memset(dst->contents,0x52,dst->length);
    auto readsBefore=cpuReads.load();fuxi::FrameSample timing;
    {fuxi::FrameTraceScope scope(&timing);backend.submit({{nullptr,src,0,4,65528,value},{src,dst,4,8,65520,0}});}
    if(fuxi::systemTranslationPolicy().frameCPU)assert(timing.backendCPU && cpuReads==readsBefore+2);
    else assert(cpuReads==0 && !timing.backendCPU);
    assert(!timing.recordCPU && !timing.queueSubmitCPU && !timing.kernelIOCPU);
    verify(src,0,4,0x39);verify(src,4,65528,value);verify(src,65532,4,0x39);
    verify(dst,0,8,0x52);verify(dst,8,65520,value);verify(dst,65528,8,0x52);
   }
   // A lock holder sleeps on another thread: wall time must show that wait,
   // while CPU clocks on the submitting thread must not charge its sleep.
   std::atomic<bool> locked{false};
   std::thread holder([&]{std::lock_guard<std::mutex> guard(backend.mutex);locked=true;std::this_thread::sleep_for(std::chrono::milliseconds(40));});
   while(!locked.load())std::this_thread::yield();
   fuxi::FrameSample blocked;
   {fuxi::FrameTraceScope scope(&blocked);backend.submit({{nullptr,src,0,0,64,1}});}
   holder.join();
   if(fuxi::systemTranslationPolicy().frameTrace)assert(blocked.backendLock>20000000);
   if(fuxi::systemTranslationPolicy().frameCPU)assert(blocked.backendCPU && blocked.backendCPU<blocked.backendLock/2);
   if(!fuxi::systemTranslationPolicy().frameCPU)assert(cpuReads==0);
   // Event-segmented frames retain a fixed two-read backend CPU budget.
   fuxi::FrameSample segmented;auto before=cpuReads.load();
   {fuxi::FrameTraceScope scope(&segmented);for(unsigned i=0;i<120;++i)backend.submit({{nullptr,src,0,0,64,1}});}
   if(fuxi::systemTranslationPolicy().frameCPU)assert(cpuReads==before+2 && segmented.cpuSampledSubmits==1 && segmented.cpuOmittedSubmits==119);
   else assert(cpuReads==before);
   backend.destroy(src);backend.destroy(dst);
  }
  puts("PASS: actual backend 32 GPU fill/copy submissions, CPU cache transitions, sentinel edges, reuse, reopen");
 } catch(const std::exception &e) {fprintf(stderr,"FAIL %s\n",e.what());return 1;}
}
'''

def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--headers',type=Path,required=True)
    ap.add_argument('--icd',type=Path,default=Path('/lib/x86_64-linux-gnu/libvulkan_lvp.so'));a=ap.parse_args()
    spec=importlib.util.spec_from_file_location('m3',ROOT/'tools/test-fuxi-vk-smoke.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    with tempfile.TemporaryDirectory() as tmp:
        d=Path(tmp);proxy=m.PROXY.replace('getenv("TU_DEBUG"),"nocb"','getenv("TU_DEBUG"),"sysmem,nocb"').replace('static bool compute;','static bool compute,corrupted;').replace(
            'if(getenv("BAD_FILL") || (compute && getenv("BAD_COMPUTE")))static_cast<uint32_t *>(pointer)[16383]^=1;',
            '').replace('return wait(d,n,f,all,ns);',
            'VkResult result=wait(d,n,f,all,ns);if(result==VK_SUCCESS && !corrupted && getenv("BAD_FILL")){static_cast<uint32_t *>(pointer)[16383]^=1;corrupted=true;}return result;')
        (d/'proxy.cpp').write_text(proxy);(d/'test.cpp').write_text(TEST)
        flags=['clang++','-std=c++17','-Wall','-Wextra','-Werror','-I'+str(a.headers)]
        subprocess.run(flags+['-shared','-fPIC',str(d/'proxy.cpp'),'-ldl','-o',str(d/'proxy.so')],check=True)
        subprocess.run(flags+['-fsanitize=address,undefined','-DFUXI_MTL_HOST_TEST','-DFUXI_MTL_ICD="'+str(d/'proxy.so')+'"',
            '-I'+str(ROOT/'tools'),str(d/'test.cpp'),'-ldl','-pthread','-o',str(d/'test')],check=True)
        env=dict(os.environ,SOFTWARE_ICD=str(a.icd),ASAN_OPTIONS='detect_leaks=0',FUXI_MTL_TEST_BOOTARGS='');env.pop('FUXI_MTL_BUFFER_CACHED',None)
        for extra,expected in (({},0),({'FUXI_MTL_BUFFER_CACHED':'1'},0),({'FUXI_MTL_TEST_BOOTARGS':'fuxigpuframetrace=1'},0),({'FUXI_MTL_TEST_BOOTARGS':'fuxigpuframetrace=2'},0),({'TIMEOUT':'1'},1),({'LOST':'1'},1),({'BAD_FILL':'1'},1)):
            p=subprocess.run([str(d/'test')],env=dict(env,**extra),text=True,capture_output=True,timeout=30)
            assert p.returncode==expected,(extra,p.stdout,p.stderr)
            assert 'AddressSanitizer' not in p.stderr and 'runtime error:' not in p.stderr,p.stderr
            if expected==0:
                print(p.stdout,end='')
                # Buffer memory-type choice logged once (lavapipe: every type is host cached, so both settings pick the first host-visible type).
                cached='1' if extra.get('FUXI_MTL_BUFFER_CACHED')=='1' else '0'
                assert p.stderr.count('FUXI MTL: policy buffer_cached=')==1 and 'FUXI MTL: policy buffer_cached='+cached+' memory_types=0:' in p.stderr,p.stderr
                assert re.search(r'FUXI MTL: buffer memory type=\d+ flags=\S*HV\S* cached=[01]\n',p.stderr),p.stderr
            else:assert 'FAIL' in p.stderr,p.stderr
        print('PASS: injected fence timeout, device loss, last-byte corruption stop execution')
if __name__=='__main__':main()
