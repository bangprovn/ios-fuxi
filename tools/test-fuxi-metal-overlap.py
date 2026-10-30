#!/usr/bin/env python3
"""Local Vulkan overlap correctness and 120 sampled draw A/B benchmark. No downloads."""
import argparse
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
ROOT = Path(__file__).resolve().parents[1]

def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--headers', type=Path, default=Path('/usr/include'))
    ap.add_argument('--icd', type=Path, default=Path('/lib/x86_64-linux-gnu/libvulkan_lvp.so'))
    ap.add_argument('--glslang', default=shutil.which('glslangValidator'))
    a = ap.parse_args()
    if not a.glslang:
        ap.error('supply an existing glslangValidator with --glslang')
    m6 = module('m6', ROOT/'tools/test-fuxi-metal-m6.py')
    smoke = module('smoke', ROOT/'tools/test-fuxi-vk-smoke.py')
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        env = dict(os.environ, ASAN_OPTIONS='detect_leaks=0', LP_NUM_THREADS='2', CONTROL_LOD='0',
                   FUXI_MTL_TEST_BOOTARGS='fuxigpuframetrace=1')
        common = ['clang++', '-std=c++17', '-Wall', '-Wextra', '-Werror', '-I'+str(a.headers)]
        sanitized = ['-fsanitize=address,undefined']
        proxy = smoke.PROXY.replace('getenv("TU_DEBUG"),"nocb"', 'getenv("TU_DEBUG"),"sysmem,nocb"')
        proxy = proxy.replace('static PFN_vkMapMemory map;', 'static PFN_vkMapMemory map;\nstatic PFN_vkGetPhysicalDeviceMemoryProperties memoryProperties;')
        proxy = proxy.replace('extern "C" PFN_vkVoidFunction vk_icdGetInstanceProcAddr', '\nstatic void properties(VkPhysicalDevice d,VkPhysicalDeviceMemoryProperties *out){memoryProperties(d,out);if(getenv("NONCOHERENT"))for(uint32_t i=0;i<out->memoryTypeCount;++i)out->memoryTypes[i].propertyFlags&=~VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;}\nextern "C" PFN_vkVoidFunction vk_icdGetInstanceProcAddr')
        proxy = proxy.replace('PFN_vkVoidFunction f=gipa(i,name);', 'PFN_vkVoidFunction f=gipa(i,name);if(!strcmp(name,"vkGetPhysicalDeviceMemoryProperties")){memoryProperties=reinterpret_cast<PFN_vkGetPhysicalDeviceMemoryProperties>(f);return (PFN_vkVoidFunction)properties;}')
        (d/'proxy.cpp').write_text(proxy)
        subprocess.run(common+['-shared', '-fPIC', str(d/'proxy.cpp'), '-ldl', '-o', str(d/'proxy.so')], check=True)
        subprocess.run(common+sanitized+['-DFUXI_MTL_HOST_TEST', '-DFUXI_MTL_ICD="'+str(d/'proxy.so')+'"',
            str(ROOT/'tools/fuxi-metal/OverlapTests.cpp'), '-ldl', '-pthread', '-o', str(d/'overlap')], check=True)
        for extra in ({}, {'LOST': '1'}, {'TIMEOUT': '1'}, {'NONCOHERENT': '1'}):
            # Dedicated allocations: the pin test counts memory objects (slabs would keep them).
            p = subprocess.run([str(d/'overlap')]+(['host' if 'NONCOHERENT' in extra else 'fault'] if extra else []), env=dict(env, SOFTWARE_ICD=str(a.icd), FUXI_MTL_TEST_BOOTARGS='fuxigpuframetrace=1 fuxigpunoslab=1', **extra), text=True, capture_output=True, timeout=60)
            assert p.returncode == 0, p.stderr
            assert 'AddressSanitizer' not in p.stderr and 'runtime error:' not in p.stderr, p.stderr
            print(p.stdout, end='')
        fragment = '''#version 450
layout(set=0,binding=0) uniform sampler2D image;
layout(push_constant) uniform Constants { float lod; } constants;
layout(location=0) out vec4 color;
void main() {color=textureLod(image,vec2(0.5),constants.lod);}
'''
        for stage, source in [('vert', m6.VERTEX), ('frag', fragment)]:
            src = d/('control.'+stage)
            src.write_text(source)
            out = d/(stage+'.spv')
            subprocess.run([a.glslang, '-V', '--target-env', 'vulkan1.2', str(src), '-o', str(out)], check=True, capture_output=True)
            env['CONTROL_VERTEX' if stage == 'vert' else 'CONTROL_FRAGMENT'] = str(out)
        subprocess.run(common+['-O2', '-shared', '-fPIC', str(ROOT/'tools/fuxi-metal/MipFeedbackBridge.cpp'), '-o', str(d/'bridge.so')], check=True)
        subprocess.run(common+['-O2', '-DFUXI_MTL_HOST_TEST', '-DFUXI_MTL_ICD="'+str(a.icd.resolve())+'"',
            '-DFUXI_MTL_TRANSLATOR="'+str(d/'bridge.so')+'"', str(ROOT/'tools/fuxi-metal/SubmitBench.cpp'), '-ldl', '-pthread', '-o', str(d/'bench')], check=True)
        subprocess.run(common+['-O1', *sanitized, '-DFUXI_MTL_HOST_TEST', '-DFUXI_MTL_ICD="'+str(a.icd.resolve())+'"',
            '-DFUXI_MTL_TRANSLATOR="'+str(d/'bridge.so')+'"', str(ROOT/'tools/fuxi-metal/SubmitBench.cpp'), '-ldl', '-pthread', '-o', str(d/'sampled-check')], check=True)
        p = subprocess.run([str(d/'sampled-check')], env=dict(env, FUXI_MTL_TEST_BOOTARGS='fuxigpuframetrace=1 fuxigpusubmit=1'), text=True, capture_output=True, timeout=60)
        assert p.returncode == 0, p.stderr
        assert 'AddressSanitizer' not in p.stderr and 'runtime error:' not in p.stderr, p.stderr
        print('PASS: ASan/UBSan sampled descriptor reuse, pixels and two-flight resource lifetime')
        # Fresh processes in A/B/B/A order. Timings are evidence, never assertions.
        for args in ('', 'fuxigpusubmit=1', 'fuxigpusubmit=1', ''):
            p = subprocess.run([str(d/'bench')], env=dict(env, FUXI_MTL_TEST_BOOTARGS='fuxigpuframetrace=1 '+args), text=True, capture_output=True, timeout=60)
            assert p.returncode == 0, p.stderr
            print(p.stdout, end='')

if __name__ == '__main__':
    main()
