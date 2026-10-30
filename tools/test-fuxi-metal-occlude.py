#!/usr/bin/env python3
"""FUXI_MTL_OCCLUDE: CPU vertex evaluator and proof unit tests, then lavapipe pixels with the knob off and on (byte-for-byte equal), no downloads."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
ROOT = Path(__file__).resolve().parents[1]
PUSH = 'layout(push_constant) uniform P {vec4 rect; vec4 rot; vec4 color;} pc;\n'
VERTEX = '#version 450\n' + PUSH + '''void main(){
 vec2 c[6]=vec2[6](vec2(0,0),vec2(1,0),vec2(0,1),vec2(1,0),vec2(1,1),vec2(0,1));
 vec2 p=mix(pc.rect.xy,pc.rect.zw,c[gl_VertexIndex%6]);
 vec2 ctr=(pc.rect.xy+pc.rect.zw)*0.5;vec2 d=p-ctr;float s=pc.rot.x,co=pc.rot.y;
 p=ctr+vec2(co*d.x-s*d.y,s*d.x+co*d.y);
 gl_Position=vec4(p,0.5,1.0);}
'''
VERTEX_ATTR = '#version 450\n' + PUSH + 'layout(location=0) in vec2 pos;\nvoid main(){gl_Position=vec4(pos,0.5,1.0);}\n'
FRAGMENT = '#version 450\n' + PUSH + 'layout(location=0) out vec4 outColor;\nvoid main(){outColor=pc.color;}\n'
KILL = FRAGMENT.replace('void main(){', 'void main(){if(gl_FragCoord.x<6.0)discard;')
STORAGE = FRAGMENT.replace('layout(location=0)', 'layout(set=0,binding=1,std430) buffer S {uint v;} s;\nlayout(location=0)').replace('void main(){', 'void main(){atomicAdd(s.v,1u);')
EVAL = '''#version 450
layout(constant_id=3) const int mode=0;
layout(push_constant) uniform P {vec4 rect;} pc;
layout(std140,set=0,binding=1) uniform U {mat4 m;vec4 k[2];} u;
layout(location=0) in vec2 pos;
layout(location=1) in uvec2 ids;
layout(location=0) out vec4 o;
void main(){
 vec2 c[6]=vec2[6](vec2(0,0),vec2(1,0),vec2(0,1),vec2(1,0),vec2(1,1),vec2(0,1));
 vec2 p;
 if(mode==1)p=pos; else p=mix(pc.rect.xy,pc.rect.zw,c[gl_VertexIndex%6]);
 vec4 q=u.m*vec4(p,0.5,1.0);
 for(int i=0;i<2;++i)q.xy+=u.k[i].xy*float(ids.x);
 if(q.x>100.0)q.w=-1.0;
 o=q;
 gl_Position=q;
}
'''

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--headers', type=Path)
    ap.add_argument('--icd', type=Path, help='existing local Vulkan ICD enables the pixel tests')
    ap.add_argument('--glslang', default=shutil.which('glslangValidator'))
    a = ap.parse_args()
    if not a.glslang:
        ap.error('supply existing glslangValidator with --glslang')
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        cc = ['xcrun', 'clang++'] if os.uname().sysname == 'Darwin' else ['clang++']
        common = cc+['-std=c++17', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined']+(['-I'+str(a.headers)] if a.headers else [])
        env = dict(os.environ, ASAN_OPTIONS='detect_leaks=0', LP_NUM_THREADS='2')
        def glsl(name, source, stage):
            src = d/(name+'.'+stage)
            src.write_text(source)
            out = d/(name+'.spv')
            subprocess.run([a.glslang, '-V', '--target-env', 'vulkan1.2', str(src), '-o', str(out)], check=True, capture_output=True)
            return out
        for name in ('OcclusionProofTests', 'SpirvEvalTests'):
            subprocess.run(common+[str(ROOT/'tools/fuxi-metal'/(name+'.cpp')), '-o', str(d/name)], check=True)
        subprocess.run([str(d/'OcclusionProofTests')], env=env, check=True, timeout=30)
        subprocess.run([str(d/'SpirvEvalTests'), str(glsl('eval', EVAL, 'vert'))], env=env, check=True, timeout=30)
        if not a.icd:
            print('SKIP: lavapipe pixels require --icd')
            return
        env['CONTROL_VERTEX'] = str(glsl('vertex', VERTEX, 'vert'))
        env['CONTROL_VERTEX_ATTR'] = str(glsl('vertex-attr', VERTEX_ATTR, 'vert'))
        env['CONTROL_FRAGMENT'] = str(glsl('fragment', FRAGMENT, 'frag'))
        env['CONTROL_FRAGMENT_KILL'] = str(glsl('kill', KILL, 'frag'))
        env['CONTROL_FRAGMENT_STORAGE'] = str(glsl('storage', STORAGE, 'frag'))
        subprocess.run(common+['-shared', '-fPIC', str(ROOT/'tools/fuxi-metal/OcclusionBridge.cpp'), '-o', str(d/'bridge.so')], check=True)
        subprocess.run(common+['-DFUXI_MTL_HOST_TEST', '-DFUXI_MTL_ICD="'+str(a.icd.resolve())+'"', '-DFUXI_MTL_TRANSLATOR="'+str(d/'bridge.so')+'"',
                               str(ROOT/'tools/fuxi-metal/OcclusionTests.cpp'), '-ldl', '-pthread', '-o', str(d/'pixels')], check=True)
        def run(args, occlude):
            p = subprocess.run([str(d/'pixels')], env=dict(env, FUXI_MTL_OCCLUDE='1' if occlude else '0', FUXI_MTL_OCCLUDE_MIN_PX='1', FUXI_MTL_TEST_BOOTARGS='fuxigpuframetrace=1 '+args),
                               text=True, capture_output=True, timeout=240)
            assert p.returncode == 0, (args, occlude, p.stdout[-2000:], p.stderr[-4000:])
            assert 'AddressSanitizer' not in p.stderr and 'runtime error:' not in p.stderr, p.stderr
            return p
        keep = lambda out: [l for l in out.splitlines() if l.startswith('PIXELS ') or l.startswith('STATE ')]
        for args in ('', 'fuxigpuoverlap=1 fuxigpusubmit=1', 'fuxigpumerge=1 fuxigpuperf=7', 'fuxigpumerge=1 fuxigpuoverlap=1 fuxigpusubmit=1 fuxigpuperf=7'):
            off, on = run(args, False), run(args, True)
            a_, b_ = keep(off.stdout), keep(on.stdout)
            assert len(a_) > 60 and a_ == b_, 'pixels differ with FUXI_MTL_OCCLUDE=1 for ' + repr(args)
            summary = [l for l in on.stdout.splitlines() if l.startswith('OCC ')]
            print('args=%r: %d pixel lines identical; %s' % (args, len(a_), ' '.join(l[4:] for l in summary if l.startswith('OCC fuzz') or 'stack' in l)))
            print([l for l in on.stderr.splitlines() if 'FUXI MTL: occlude' in l][-1:] or 'no occlude report line (reported on the periodic counter)')
        print('PASS: occlusion proofs and pixels')

if __name__ == '__main__':
    main()
