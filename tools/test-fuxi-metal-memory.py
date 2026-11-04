#!/usr/bin/env python3
"""Portable production memory pool tests with a local software Vulkan ICD."""
import argparse
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
ROOT=Path(__file__).resolve().parents[1]


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--headers',type=Path,default=Path('/usr/include'))
    ap.add_argument('--icd',type=Path,default=Path('/lib/x86_64-linux-gnu/libvulkan_lvp.so'))
    args=ap.parse_args()
    spec=importlib.util.spec_from_file_location('smoke',ROOT/'tools/test-fuxi-vk-smoke.py')
    smoke=importlib.util.module_from_spec(spec);spec.loader.exec_module(smoke)
    with tempfile.TemporaryDirectory() as tmp:
        d=Path(tmp)
        (d/'proxy.cpp').write_text(smoke.PROXY.replace('getenv("TU_DEBUG"),"nocb"','getenv("TU_DEBUG"),"sysmem,nocb"'))
        common=['clang++','-std=c++17','-Wall','-Wextra','-Werror','-I'+str(args.headers)]
        subprocess.run(common+['-shared','-fPIC',str(d/'proxy.cpp'),'-ldl','-o',str(d/'proxy.so')],check=True)
        subprocess.run(common+['-fsanitize=address,undefined','-DFUXI_MTL_HOST_TEST',
            '-DFUXI_MTL_ICD="'+str(d/'proxy.so')+'"',str(ROOT/'tools/fuxi-metal/MemoryReuseTests.cpp'),
            '-ldl','-pthread','-o',str(d/'test')],check=True)
        for extra in ({},{'LOST':'1'},{'TIMEOUT':'1'}):
            env=dict(os.environ,ASAN_OPTIONS='detect_leaks=0',LP_NUM_THREADS='2',
                     FUXI_MTL_TEST_BOOTARGS='fuxigpuframetrace=1 fuxigpurename=1',SOFTWARE_ICD=str(args.icd),**extra)
            p=subprocess.run([str(d/'test')]+(['fault'] if extra else []),env=env,text=True,capture_output=True,timeout=60)
            assert p.returncode==0,p.stderr
            assert 'AddressSanitizer' not in p.stderr and 'runtime error:' not in p.stderr,p.stderr
            print(p.stdout,end='')
        # Single planes of 420v/420f surfaces (in place at their offsets and rows, or through a shadow), with and without the shadow hash.
        subprocess.run(common+['-fsanitize=address,undefined','-DFUXI_MTL_HOST_TEST',
            '-DFUXI_MTL_ICD="'+str(d/'proxy.so')+'"',str(ROOT/'tools/fuxi-metal/PlaneImportTests.cpp'),
            '-ldl','-pthread','-o',str(d/'planes')],check=True)
        for extra in ({},{'FUXI_MTL_SHADOW_HASH':'0'}):
            env=dict(os.environ,ASAN_OPTIONS='detect_leaks=0',LP_NUM_THREADS='2',SOFTWARE_ICD=str(args.icd),**extra)
            p=subprocess.run([str(d/'planes')],env=env,text=True,capture_output=True,timeout=60)
            assert p.returncode==0,p.stderr
            assert 'AddressSanitizer' not in p.stderr and 'runtime error:' not in p.stderr,p.stderr
            assert 'FUXI MTL: IOSurface plane import via shadow format=10 20x9 offset=0 row=40' in p.stderr,p.stderr
            assert 'FUXI MTL: host import refused surface (Vulkan rc -2' in p.stderr and 'FUXI MTL: IOSurface copied, not imported: format=10 640x360 offset=64 row=640' in p.stderr,p.stderr
            assert 'backboardd gate' not in p.stderr,p.stderr
            print(p.stdout,end='')


if __name__=='__main__':main()
