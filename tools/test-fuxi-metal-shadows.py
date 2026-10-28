#!/usr/bin/env python3
"""Shadow snapshot host sanitizers and optional production lavapipe pixels; no downloads."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
ROOT = Path(__file__).resolve().parents[1]

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--headers', type=Path)
    ap.add_argument('--icd', type=Path, help='existing local Vulkan ICD enables pixel tests')
    ap.add_argument('--translator', type=Path, help='optional existing production adapter for authored AIR reflection')
    ap.add_argument('--llvm-as', default=shutil.which('llvm-as-18'))
    ap.add_argument('--glslang', default=shutil.which('glslangValidator'))
    a = ap.parse_args()
    if a.translator and (not a.icd or not a.llvm_as):
        ap.error('--translator requires --icd and an existing --llvm-as')
    if a.icd and not a.glslang:
        ap.error('supply existing glslangValidator with --glslang for pixel tests')
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        cc = ['xcrun', 'clang++'] if os.uname().sysname == 'Darwin' else ['clang++']
        common = cc+['-std=c++17', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined']+(['-I'+str(a.headers)] if a.headers else [])
        env = dict(os.environ, ASAN_OPTIONS='detect_leaks=0', LP_NUM_THREADS='2')
        subprocess.run(common+[str(ROOT/'tools/fuxi-metal/ShadowRangesTests.cpp'), '-pthread', '-o', str(d/'ranges')], check=True)
        subprocess.run([str(d/'ranges')], env=env, check=True, timeout=30)
        if not a.icd:
            print('SKIP: lavapipe pixels require --icd and --glslang')
            return
        vertex = '''#version 450
layout(location=0) in vec2 position;
void main(){gl_Position=vec4(position,0,1);}
'''
        fragment = '''#version 450
layout(set=0,binding=0) uniform Color {vec4 value;} c;
layout(location=0) out vec4 color;
void main(){color=c.value;}
'''
        for stage, source in [('vert', vertex), ('frag', fragment)]:
            src = d/('control.'+stage)
            src.write_text(source)
            out = d/(stage+'.spv')
            subprocess.run([a.glslang, '-V', '--target-env', 'vulkan1.2', str(src), '-o', str(out)], check=True, capture_output=True)
            env['CONTROL_VERTEX' if stage == 'vert' else 'CONTROL_FRAGMENT'] = str(out)
        if a.translator:
            air = d/'control.bc'
            subprocess.run([a.llvm_as, str(ROOT/'tools/shaders/metal-m7-control.ll'), '-o', str(air)], check=True)
            subprocess.run(common+['-DFUXI_MTL_HOST_TEST', '-DFUXI_MTL_ICD="'+str(a.icd.resolve())+'"',
                '-DFUXI_MTL_TRANSLATOR="'+str(a.translator.resolve())+'"', str(ROOT/'tools/fuxi-metal/ShadowReflectionTests.cpp'), '-ldl', '-pthread', '-o', str(d/'reflection')], check=True)
            shared = d/'shared.ll'
            shared_source = (ROOT/'tools/shaders/metal-m7-control.ll').read_text()
            original = '!22 = !{i32 3,!"air.buffer",!"air.location_index",i32 2'
            assert original in shared_source
            shared.write_text(shared_source.replace(original, original[:-1]+'0'))
            shared_air = d/'shared.bc'
            subprocess.run([a.llvm_as, str(shared), '-o', str(shared_air)], check=True)
            subprocess.run([str(d/'reflection'), str(air), str(shared_air)], env=env, check=True, timeout=60)
        # Texture fixture uses the same production packer/prepare path and real sampling.
        texture_vertex = '#version 450\nvoid main(){vec2 p=vec2((gl_VertexIndex<<1)&2,gl_VertexIndex&2);gl_Position=vec4(p*2.0-1.0,0,1);}\n'
        texture_fragment = '#version 450\nlayout(set=0,binding=0) uniform sampler2D image;\nlayout(location=0) out vec4 color;\nvoid main(){ivec2 p=ivec2(gl_FragCoord.xy)*(textureSize(image,0)-1)/15;color=texelFetch(image,p,0);}\n'
        texture_env = dict(env, CONTROL_LOD='0')
        storage_fragment = texture_fragment.replace('layout(location=0)', 'layout(set=0,binding=1) buffer Indices {uint value[];} indices;\nlayout(location=0)').replace('void main(){', 'void main(){if(gl_FragCoord.x<1.0 && gl_FragCoord.y<1.0){indices.value[0]=65536u;indices.value[1]=2u;}')
        for stage, source in [('vert', texture_vertex), ('frag', texture_fragment), ('storage.frag', storage_fragment)]:
            src = d/('texture.'+stage)
            src.write_text(source)
            out = d/('texture.'+stage+'.spv')
            subprocess.run([a.glslang, '-V', '--target-env', 'vulkan1.2', str(src), '-o', str(out)], check=True, capture_output=True)
            texture_env[{'vert':'CONTROL_VERTEX','frag':'CONTROL_FRAGMENT','storage.frag':'CONTROL_STORAGE_FRAGMENT'}[stage]] = str(out)
        subprocess.run(common+['-shared', '-fPIC', str(ROOT/'tools/fuxi-metal/TextureShadowBridge.cpp'), '-o', str(d/'texture-bridge.so')], check=True)
        subprocess.run(common+['-DFUXI_MTL_HOST_TEST', '-DFUXI_MTL_ICD="'+str(a.icd.resolve())+'"',
            '-DFUXI_MTL_TRANSLATOR="'+str(d/'texture-bridge.so')+'"', str(ROOT/'tools/fuxi-metal/TextureShadowCopyTests.cpp'), '-ldl', '-pthread', '-o', str(d/'texture-pixels')], check=True)
        for args in ('', 'fuxigpuoverlap=1 fuxigpusubmit=1'):
            for rt in ('0', '1'):
                subprocess.run([str(d/'texture-pixels')], env=dict(texture_env, FUXI_MTL_RT_SHADOW=rt, FUXI_MTL_TEST_BOOTARGS='fuxigpuframetrace=1 '+args), check=True, timeout=60)
        subprocess.run(common+['-DFUXI_MTL_HOST_TEST', '-DFUXI_MTL_ICD="'+str(a.icd.resolve())+'"',
            '-DFUXI_MTL_TRANSLATOR="'+str(d/'texture-bridge.so')+'"', str(ROOT/'tools/fuxi-metal/AdmissionTests.cpp'), '-ldl', '-pthread', '-o', str(d/'admission')], check=True)
        subprocess.run([str(d/'admission')], env=dict(texture_env, FUXI_MTL_TEST_BOOTARGS='fuxigpuframetrace=1 fuxigpuoverlap=1 fuxigpusubmit=1'), check=True, timeout=60)
        for old in (False, True):
            extra = ['-DSHADOW_OLD_ADAPTER'] if old else []
            subprocess.run(common+extra+['-shared', '-fPIC', str(ROOT/'tools/fuxi-metal/ShadowBridge.cpp'), '-o', str(d/'bridge.so')], check=True)
            subprocess.run(common+['-DFUXI_MTL_HOST_TEST', '-DFUXI_MTL_ICD="'+str(a.icd.resolve())+'"',
                '-DFUXI_MTL_TRANSLATOR="'+str(d/'bridge.so')+'"', str(ROOT/'tools/fuxi-metal/ShadowCopyTests.cpp'), '-ldl', '-pthread', '-o', str(d/'pixels')], check=True)
            for args in ('', 'fuxigpuoverlap=1 fuxigpusubmit=1'):
                runenv = dict(env, FUXI_MTL_TEST_BOOTARGS='fuxigpuframetrace=1 '+args)
                runenv.pop('SHADOW_OLD_ADAPTER', None)
                if old:
                    runenv['SHADOW_OLD_ADAPTER'] = '1'
                subprocess.run([str(d/'pixels')], env=runenv, check=True, timeout=60)

if __name__ == '__main__':
    main()
