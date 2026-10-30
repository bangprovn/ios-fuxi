#!/usr/bin/env python3
"""FUXI_MTL_OCCLUDE / FUXI_MTL_NOFETCH_RP / FUXI_MTL_DRAW_AREA / FUXI_MTL_SPIRV_UNROLL / FUXI_MTL_HALF_ROUND: CPU vertex evaluator and proof unit tests, then lavapipe pixels with each knob off and on (byte-for-byte equal), no downloads."""
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
# FUXI_MTL_SPIRV_UNROLL control: the shape metal2vulkan's roll pass writes (a DontUnroll k-trip loop whose trip constants are OpSelects on the trip index,
# feeding a switch), with a plain nested loop for mode 2. The same push constants as FRAGMENT, so the occlusion bridge and scenarios run it unchanged.
ROLLED = '''#version 450
#extension GL_EXT_control_flow_attributes : require
''' + PUSH + '''layout(location=0) out vec4 outColor;
void main(){
 vec4 c=pc.color;vec2 f=fract(gl_FragCoord.xy*0.0625);
 [[dont_unroll]] for(int t=0;t<3;++t){
  uint op=t==0?5u:(t==1?1u:6u);float k=t==0?0.75:(t==1?0.5:0.25);
  for(int j=0;j<2;++j)c.rg+=f*k*0.125;
  switch(op){
   case 1u: c.rgb=c.rgb*vec3(0.9,1.1,0.8)+vec3(0.05*k); break;
   case 5u: c=vec4(c.b,c.r,c.g,c.a); break;
   case 6u: c.rgb=mix(c.rgb,vec3(dot(c.rgb,vec3(0.299,0.587,0.114))),k); break;
   default: c*=0.5; break;
  }
 }
 outColor=clamp(c,0.0,1.0);}
'''
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
EVAL_RT = '''#version 450
layout(std430,set=0,binding=2) readonly buffer B {vec4 head;vec2 pts[];} b;
void main(){int n=b.pts.length();vec2 p=b.pts[gl_VertexIndex%n];gl_Position=vec4(p*b.head.xy,float(n),1.0);}
'''

def half_shader(loop=True):
    """metal2vulkan-shaped fragment shader (spirv-as text): f32 math where every half result takes the Device-form rounding
    Select(FOrdEqual(QuantizeToF16(x),0), RoundEven(x*2^24)*2^-24, QuantizeToF16(x)), plus roundings that are identities (of a rounding, of a grid
    constant, of Floor of a rounding, of UnpackHalf2x16), a bare double quantize, a value in the half denormal range and a DontUnroll loop for SPIRV_UNROLL."""
    consts, body, n = {}, [], [0]
    def new():
        n[0] += 1
        return '%%t%d' % n[0]
    def const(t, v):
        key = (t, v)
        if key not in consts:
            consts[key] = '%%c%d' % len(consts)
        return consts[key]
    def f(v): return const('%f', ('f', float(v)))
    def vec(t, vals): return const(t, ('v', tuple(f(v) for v in vals)))
    lanes = {'%f': 1, '%v2': 2, '%v3': 3, '%v4': 4}
    bools = {'%f': '%b', '%v2': '%b2', '%v3': '%b3', '%v4': '%b4'}
    def splat(t, v): return f(v) if t == '%f' else vec(t, [v]*lanes[t])
    def op(t, name, *args):
        r = new(); body.append('%s = %s %s %s' % (r, name, t, ' '.join(args))); return r
    def ext(t, name, *args): return op(t, 'OpExtInst', '%glsl', name, *args)
    def R(t, x):
        q = op(t, 'OpQuantizeToF16', x)
        c = op(bools[t], 'OpFOrdEqual', q, splat(t, 0.0))
        s = op(t, 'OpFMul', x, splat(t, 16777216.0))
        k = ext(t, 'RoundEven', s)
        d = op(t, 'OpFMul', k, splat(t, 1.0/16777216.0))
        return op(t, 'OpSelect', c, d, q)
    mul = lambda t, a, b: R(t, op(t, 'OpFMul', a, b))
    add = lambda t, a, b: R(t, op(t, 'OpFAdd', a, b))
    sub = lambda t, a, b: R(t, op(t, 'OpFSub', a, b))
    vts = lambda t, a, s: R(t, op(t, 'OpVectorTimesScalar', a, s))
    w = f(1.0)
    if loop:
        body.append('OpBranch %head')
        body.append('%head = OpLabel')
        wp, ip = new(), new()
        body.append('__PHI__')
        cmp = op('%b', 'OpSLessThan', ip, const('%i', ('i', 3)))
        body.append('OpLoopMerge %merge %cont DontUnroll')
        body.append('OpBranchConditional %s %%body %%merge' % cmp)
        body.append('%body = OpLabel')
        wn = op('%f', 'OpFAdd', op('%f', 'OpFMul', wp, f(0.75)), f(0.125))
        body.append('OpBranch %cont')
        body.append('%cont = OpLabel')
        inext = op('%i', 'OpIAdd', ip, const('%i', ('i', 1)))
        body.append('OpBranch %head')
        body.append('%merge = OpLabel')
        body[body.index('__PHI__')] = '%s = OpPhi %%f %s %%entry %s %%cont\n%s = OpPhi %%i %s %%entry %s %%cont' % (wp, f(1.0), wn, ip, const('%i', ('i', 0)), inext)
        w = wp
    fcv = op('%v4', 'OpLoad', '%fc')
    fxy = op('%v2', 'OpVectorShuffle', fcv, fcv, '0', '1')
    ptr = op('%pv4', 'OpAccessChain', '%pc', const('%i', ('i', 2)))
    c = R('%v4', op('%v4', 'OpLoad', ptr))
    p = mul('%v2', fxy, splat('%v2', 0.03125))
    fr = R('%v2', ext('%v2', 'Fract', p))
    gx = mul('%f', op('%f', 'OpCompositeExtract', fr, '0'), f(0.7))
    gy = mul('%f', op('%f', 'OpCompositeExtract', fr, '1'), f(0.3))
    g = add('%f', gx, gy)
    rgb = op('%v3', 'OpVectorShuffle', c, c, '0', '1', '2')
    ca = op('%f', 'OpCompositeExtract', c, '3')
    rgb1 = add('%v3', vts('%v3', rgb, g), vts('%v3', splat('%v3', 0.05), ca))
    rgb2 = add('%v3', mul('%v3', rgb1, vec('%v3', [0.9, 1.1, 0.8])), splat('%v3', 0.02))
    ch = [op('%f', 'OpCompositeExtract', rgb2, str(i)) for i in range(3)]
    l = add('%f', add('%f', mul('%f', ch[0], f(0.299)), mul('%f', ch[1], f(0.587))), mul('%f', ch[2], f(0.114)))
    lv = op('%v3', 'OpCompositeConstruct', l, l, l)
    rgb3 = add('%v3', rgb2, vts('%v3', sub('%v3', lv, rgb2), f(0.5)))
    acc = vts('%v3', rgb3, w)
    z = R('%v3', R('%v3', acc))                                   # rounding of a rounding
    k = R('%f', f(0.5))                                            # grid constant
    m = R('%f', ext('%f', 'Floor', mul('%f', g, f(4.0))))          # Floor of a rounding
    u = R('%v2', ext('%v2', 'UnpackHalf2x16', const('%u', ('u', 0x3a003400))))
    tiny = mul('%f', ca, f(3e-6))                                  # half denormal range
    bq = op('%f', 'OpQuantizeToF16', op('%f', 'OpQuantizeToF16', g))
    scale = sub('%f', f(1.0), mul('%f', k, f(0.1)))
    extra = op('%v3', 'OpCompositeConstruct', mul('%f', m, f(0.01)), mul('%f', op('%f', 'OpCompositeExtract', u, '0'), f(0.01)), mul('%f', bq, f(0.02)))
    rgbf = add('%v3', vts('%v3', z, scale), extra)
    alpha = add('%f', ca, mul('%f', tiny, f(1000.0)))
    o = op('%v4', 'OpCompositeConstruct', rgbf, alpha)
    o = ext('%v4', 'FClamp', o, splat('%v4', 0.0), splat('%v4', 1.0))
    body.append('OpStore %out ' + o)
    decl = []
    for (t, v), name in consts.items():
        if v[0] == 'f': decl.append('%s = OpConstant %s %r' % (name, t, v[1]))
        elif v[0] in ('i', 'u'): decl.append('%s = OpConstant %s %d' % (name, t, v[1]))
        else: decl.append('%s = OpConstantComposite %s %s' % (name, t, ' '.join(v[1])))
    return '''OpCapability Shader
%glsl = OpExtInstImport "GLSL.std.450"
OpMemoryModel Logical GLSL450
OpEntryPoint Fragment %main "main" %fc %out %pc
OpExecutionMode %main OriginUpperLeft
OpDecorate %fc BuiltIn FragCoord
OpDecorate %out Location 0
OpMemberDecorate %P 0 Offset 0
OpMemberDecorate %P 1 Offset 16
OpMemberDecorate %P 2 Offset 32
OpDecorate %P Block
%void = OpTypeVoid
%fn = OpTypeFunction %void
%f = OpTypeFloat 32
%v2 = OpTypeVector %f 2
%v3 = OpTypeVector %f 3
%v4 = OpTypeVector %f 4
%b = OpTypeBool
%b2 = OpTypeVector %b 2
%b3 = OpTypeVector %b 3
%b4 = OpTypeVector %b 4
%i = OpTypeInt 32 1
%u = OpTypeInt 32 0
%P = OpTypeStruct %v4 %v4 %v4
%pP = OpTypePointer PushConstant %P
%pc = OpVariable %pP PushConstant
%pv4 = OpTypePointer PushConstant %v4
%pin = OpTypePointer Input %v4
%fc = OpVariable %pin Input
%pout = OpTypePointer Output %v4
%out = OpVariable %pout Output
''' + '\n'.join(decl) + '''
%main = OpFunction %void None %fn
%entry = OpLabel
''' + '\n'.join(body) + '''
OpReturn
OpFunctionEnd
'''


def pixel_diff(ref, out):
    """(max byte difference, differing bytes, bytes) between two runs' PIXELS lines, label by label."""
    worst = changed = total = 0
    for x, y in zip(ref, out):
        assert x.split()[:2] == y.split()[:2], (x[:40], y[:40])
        if not x.startswith('PIXELS '):
            assert x == y, (x, y)
            continue
        a, b = bytes.fromhex(x.split()[2]), bytes.fromhex(y.split()[2])
        diffs = [abs(i - j) for i, j in zip(a, b)]
        worst, changed, total = max([worst] + diffs), changed + sum(1 for v in diffs if v), total + len(a)
    assert len(ref) == len(out)
    return worst, changed, total

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--headers', type=Path)
    ap.add_argument('--icd', type=Path, help='existing local Vulkan ICD enables the pixel tests')
    ap.add_argument('--glslang', default=shutil.which('glslangValidator'))
    ap.add_argument('--spirv-tools', type=Path, help='directory with spirv-as/spirv-val (default: next to --glslang) for the FUXI_MTL_HALF_ROUND shader')
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
        for name in ('OcclusionProofTests', 'SpirvEvalTests', 'SpirvUnrollTests', 'SpirvHalfRoundTests'):
            subprocess.run(common+[str(ROOT/'tools/fuxi-metal'/(name+'.cpp')), '-o', str(d/name)], check=True)
        subprocess.run([str(d/'OcclusionProofTests')], env=env, check=True, timeout=30)
        subprocess.run([str(d/'SpirvEvalTests'), str(glsl('eval', EVAL, 'vert')), str(glsl('eval-rt', EVAL_RT, 'vert'))], env=env, check=True, timeout=60)
        rolled = glsl('rolled', ROLLED, 'frag')
        subprocess.run([str(d/'SpirvUnrollTests'), str(rolled)], env=env, check=True, timeout=30)
        # FUXI_MTL_HALF_ROUND: a metal2vulkan-shaped shader (Device-form half roundings, see half_shader) assembled with spirv-as; each mode's output must validate.
        tools = a.spirv_tools or Path(a.glslang).resolve().parent
        half = None
        if (tools/'spirv-as').exists() and (tools/'spirv-val').exists():
            (d/'half.spvasm').write_text(half_shader())
            half = d/'half.spv'
            subprocess.run([str(tools/'spirv-as'), '--target-env', 'vulkan1.2', str(d/'half.spvasm'), '-o', str(half)], check=True)
            subprocess.run([str(tools/'spirv-val'), '--target-env', 'vulkan1.2', str(half)], check=True)
            p = subprocess.run([str(d/'SpirvHalfRoundTests'), str(half), str(d/'half-out')], env=env, check=True, timeout=30, text=True, capture_output=True)
            print(p.stdout.strip())
            for mode in (1, 2, 3):
                subprocess.run([str(tools/'spirv-val'), '--target-env', 'vulkan1.2', str(d/('half-out.%d.spv' % mode))], check=True)
        else:
            subprocess.run([str(d/'SpirvHalfRoundTests')], env=env, check=True, timeout=30)
            print('SKIP: FUXI_MTL_HALF_ROUND module tests need spirv-as/spirv-val (--spirv-tools)')
        if not a.icd:
            print('SKIP: lavapipe pixels require --icd')
            return
        env['CONTROL_VERTEX'] = str(glsl('vertex', VERTEX, 'vert'))
        env['CONTROL_VERTEX_ATTR'] = str(glsl('vertex-attr', VERTEX_ATTR, 'vert'))
        env['CONTROL_FRAGMENT'] = str(glsl('fragment', FRAGMENT, 'frag'))
        env['CONTROL_FRAGMENT_KILL'] = str(glsl('kill', KILL, 'frag'))
        env['CONTROL_FRAGMENT_STORAGE'] = str(glsl('storage', STORAGE, 'frag'))
        subprocess.run(common+['-shared', '-fPIC', str(ROOT/'tools/fuxi-metal/OcclusionBridge.cpp'), '-o', str(d/'bridge.so')], check=True)
        # 60 s fences: lavapipe compiles a fragment variant at its first draw, and the HALF_ROUND shader's (quantizes plus a loop) takes seconds when uncached
        subprocess.run(common+['-DFUXI_MTL_HOST_TEST', '-DFUXI_MTL_HOST_FENCE_NS=60000000000ULL', '-DFUXI_MTL_ICD="'+str(a.icd.resolve())+'"', '-DFUXI_MTL_TRANSLATOR="'+str(d/'bridge.so')+'"',
                               str(ROOT/'tools/fuxi-metal/OcclusionTests.cpp'), '-ldl', '-pthread', '-o', str(d/'pixels')], check=True)
        subprocess.run(common+['-DFUXI_MTL_HOST_TEST', '-DFUXI_MTL_ICD="'+str(a.icd.resolve())+'"', '-DFUXI_MTL_TRANSLATOR="'+str(d/'bridge.so')+'"',
                               str(ROOT/'tools/fuxi-metal/DrawAreaTests.cpp'), '-ldl', '-pthread', '-o', str(d/'draw-area')], check=True)
        def run(args, occlude, nofetch=False):
            p = subprocess.run([str(d/'pixels')], env=dict(env, FUXI_MTL_OCCLUDE='1' if occlude else '0', FUXI_MTL_NOFETCH_RP='1' if nofetch else '0', FUXI_MTL_OCCLUDE_MIN_PX='1', FUXI_MTL_TEST_BOOTARGS='fuxigpuframetrace=1 '+args),
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
            assert 'nofetch render pass pipeline' not in off.stderr, 'nofetch variant built with the knob off'
            nf, both = run(args, False, True), run(args, True, True)
            assert keep(nf.stdout) == a_ and keep(both.stdout) == a_, 'pixels differ with FUXI_MTL_NOFETCH_RP=1 for ' + repr(args)
            built = sum(1 for l in nf.stderr.splitlines() if 'nofetch render pass pipeline' in l)
            assert built > 0, 'FUXI_MTL_NOFETCH_RP=1 never used a no-input-attachment render pass'
            print('  nofetch rp: pixels identical (alone and with occlude), %d variant pipelines built' % built)
        # FUXI_MTL_SPIRV_UNROLL: the rolled control shader stands in for every fragment function; modes 1 and 2 must flip its loops and match mode 0 byte for byte.
        roll = dict(env, CONTROL_FRAGMENT=str(rolled), CONTROL_FRAGMENT_KILL=str(glsl('rolled-kill', ROLLED.replace('void main(){', 'void main(){if(gl_FragCoord.x<6.0)discard;'), 'frag')),
                    CONTROL_FRAGMENT_STORAGE=str(glsl('rolled-storage', ROLLED.replace('layout(location=0) out', 'layout(set=0,binding=1,std430) buffer S {uint v;} s;\nlayout(location=0) out').replace('void main(){', 'void main(){atomicAdd(s.v,1u);'), 'frag')))
        def unroll(args, mode, occlude=False):
            p = subprocess.run([str(d/'pixels')], env=dict(roll, FUXI_MTL_SPIRV_UNROLL=mode, FUXI_MTL_OCCLUDE='1' if occlude else '0', FUXI_MTL_OCCLUDE_MIN_PX='1', FUXI_MTL_PIPELINE_STATS='1', FUXI_MTL_TEST_BOOTARGS='fuxigpuframetrace=1 '+args),
                               text=True, capture_output=True, timeout=240)
            assert p.returncode == 0, (args, mode, p.stdout[-2000:], p.stderr[-4000:])
            assert 'AddressSanitizer' not in p.stderr and 'runtime error:' not in p.stderr, p.stderr
            assert 'FUXI MTL: policy spirv_unroll=%s' % (mode if mode in ('1', '2') else '0') in p.stderr, p.stderr[-2000:]
            return p
        for args in ('', 'fuxigpumerge=1 fuxigpuoverlap=1 fuxigpusubmit=1 fuxigpuperf=7'):
            ref = unroll(args, '0')
            r = keep(ref.stdout)
            assert len(r) > 60 and 'FUXI MTL: spirv unroll' not in ref.stderr
            for mode, occlude in (('1', False), ('2', False), ('2', True)):
                p = unroll(args, mode, occlude)
                assert keep(p.stdout) == r, 'pixels differ with FUXI_MTL_SPIRV_UNROLL=%s occlude=%d for %r' % (mode, occlude, args)
                lines = [l for l in p.stderr.splitlines() if l.startswith('FUXI MTL: spirv unroll pipe=')]
                assert len(lines) >= 10 and all(' flipped=1 ' in l and ' rc=0 ' in l and ' kept_sampling=0 ' in l for l in lines), lines[:3]
                assert all((' nested=1 ' if mode == '2' else ' nested=0 ') in l for l in lines), lines[:3]
                assert all(' fh=' in l for l in p.stderr.splitlines() if l.startswith('FUXI MTL: pipeline stats pipe=') and ' frag_words=' in l)
                ex = [l for l in p.stderr.splitlines() if 'pipeline exec pipe=' in l][:2]
                print('spirv unroll mode=%s occlude=%d args=%r: %d pixel lines identical, %d pipelines flipped; %s' % (mode, occlude, args, len(r), len(lines), lines[0][:200]))
                if ex: print('  driver stats: ' + ex[-1][:300])
        # FUXI_MTL_HALF_ROUND: the metal2vulkan-shaped shader stands in for CONTROL_FRAGMENT. Mode 1 (exact) must match mode 0 byte for byte, alone and with
        # SPIRV_UNROLL=2 (which flips its DontUnroll loop); lossy modes 2/3 report the measured difference; an allowlist miss is mode 1, a hit is the lossy mode.
        if half:
            hr = dict(env, CONTROL_FRAGMENT=str(half))
            def halfrun(args, mode, **extra):
                e = dict(hr, FUXI_MTL_HALF_ROUND=mode, FUXI_MTL_OCCLUDE='0', FUXI_MTL_PIPELINE_STATS='1', FUXI_MTL_TEST_BOOTARGS='fuxigpuframetrace=1 '+args, **extra)
                p = subprocess.run([str(d/'pixels')], env=e, text=True, capture_output=True, timeout=240)
                assert p.returncode == 0, (args, mode, extra, p.stdout[-2000:], p.stderr[-4000:])
                assert 'AddressSanitizer' not in p.stderr and 'runtime error:' not in p.stderr, p.stderr
                assert 'FUXI MTL: policy half_round=%s ' % (mode if mode in ('1', '2', '3') else '0') in p.stderr, p.stderr[-2000:]
                lines = [l for l in p.stderr.splitlines() if l.startswith('FUXI MTL: half round pipe=')]
                return keep(p.stdout), lines, p.stderr
            for args in ('', 'fuxigpumerge=1 fuxigpuoverlap=1 fuxigpusubmit=1 fuxigpuperf=7'):
                r, lines, _ = halfrun(args, '0')
                assert len(r) > 60 and not lines
                for mode, extra in (('1', {}), ('1', {'FUXI_MTL_SPIRV_UNROLL': '2'}), ('2', {}), ('3', {}), ('3', {'FUXI_MTL_SPIRV_UNROLL': '2'})):
                    out, lines, err = halfrun(args, mode, **extra)
                    assert len(lines) >= 5 and all(' rc=0 ' in l and ' fh=' in l and ' lossy=1 ' in l for l in lines), lines[:3]
                    want = {'1': ' idioms=36->31 quantize_live=38->32 ', '2': ' idioms=36->0 quantize_live=38->31 ', '3': ' idioms=36->0 quantize_live=38->0 '}[mode]
                    assert all(want in l for l in lines), lines[:2]
                    if extra:
                        assert any(l.startswith('FUXI MTL: spirv unroll pipe=') and ' flipped=1 ' in l for l in err.splitlines()), 'SPIRV_UNROLL=2 did not flip the loop'
                    worst, changed, total = pixel_diff(r, out)
                    if mode == '1':
                        assert out == r, 'pixels differ with FUXI_MTL_HALF_ROUND=1 %r for %r' % (extra, args)
                    else:
                        assert worst <= 2, 'FUXI_MTL_HALF_ROUND=%s max byte difference %d' % (mode, worst)
                    print('half round mode=%s%s args=%r: %d pixel lines, max_byte_diff=%d changed_bytes=%d/%d; %s' % (mode, ' +unroll2' if extra else '', args, len(r), worst, changed, total, lines[0][:230]))
                    if mode == '3' and not extra:
                        lossy3, fh = out, lines[0].split(' fh=')[1].split()[0]
                miss, lines, err = halfrun(args, '3', FUXI_MTL_HALF_ROUND_PIPES='deadbeef,0')
                assert miss == r and lines and all(' lossy=0 ' in l and ' idioms=36->31 ' in l for l in lines), lines[:2]
                assert 'half_round_pipes=deadbeef,00000000' in err
                hit, lines, _ = halfrun(args, '3', FUXI_MTL_HALF_ROUND_PIPES='deadbeef,'+fh)
                assert hit == lossy3 and all(' lossy=1 ' in l for l in lines), lines[:2]
                print('half round allowlist args=%r: miss = mode 1 pixels, hit fh=%s = mode 3 pixels' % (args, fh))
        # FUXI_MTL_DRAW_AREA: knob off is the reference; margins 0 and 16 must shrink (asserted per scenario in DrawAreaTests.cpp), a zero CPU budget must never shrink,
        # and the knob composes with OCCLUDE and GPU pass times. Every run's PIXELS/STATE lines must equal the reference.
        def area(args, extra):
            e = dict(env, FUXI_MTL_DRAW_AREA_MIN_PX='1', FUXI_MTL_OCCLUDE_MIN_PX='1', FUXI_MTL_TEST_BOOTARGS='fuxigpuframetrace=1 '+args, **extra)
            p = subprocess.run([str(d/'draw-area')], env=e, text=True, capture_output=True, timeout=400)
            assert p.returncode == 0, (args, extra, p.stdout[-2000:], p.stderr[-4000:])
            assert 'AddressSanitizer' not in p.stderr and 'runtime error:' not in p.stderr, p.stderr
            return p
        for args in ('', 'fuxigpuoverlap=1 fuxigpusubmit=1', 'fuxigpumerge=1 fuxigpuperf=7', 'fuxigpumerge=1 fuxigpuoverlap=1 fuxigpusubmit=1 fuxigpuperf=7'):
            ref = area(args, {'FUXI_MTL_DRAW_AREA': '0'})
            r = keep(ref.stdout)
            assert len(r) > 100, ref.stdout[-2000:]
            assert 'draw_area=0' in ref.stderr and 'FUXI MTL: draw area submits=0 ' in ref.stderr
            # sanitized evaluation is ~30x slower than on device, so the scenario runs lift the per-submit CPU budget; 'default-budget' keeps 1000 us (pixels only)
            big = {'FUXI_MTL_DRAW_AREA': '1', 'FUXI_MTL_DRAW_AREA_BUDGET_US': '100000'}
            variants = [('margin0', dict(big, FUXI_MTL_DRAW_AREA_MARGIN='0')), ('margin16', big)]
            if not args:
                variants += [('budget0', {'FUXI_MTL_DRAW_AREA': '1', 'FUXI_MTL_DRAW_AREA_BUDGET_US': '0'}), ('occlude', dict(big, FUXI_MTL_OCCLUDE='1', FUXI_MTL_DRAW_AREA_MARGIN='0')),
                             ('pass-times', dict(big, FUXI_MTL_GPU_PASS_TIMES='1')), ('render-area-off', dict(big, FUXI_MTL_RENDER_AREA='0')), ('default-budget', {'FUXI_MTL_DRAW_AREA': '1', 'FUXI_MTL_DRAW_AREA_EXPECT': 'any'})]
            for name, extra in variants:
                p = area(args, extra)
                if keep(p.stdout) != r:
                    bad = [(x, y) for x, y in zip(r, keep(p.stdout)) if x != y][:3]
                    raise AssertionError('pixels differ with FUXI_MTL_DRAW_AREA %s for %r: %r' % (name, args, bad))
                total = [l for l in p.stdout.splitlines() if l.startswith('DA totals') or l.startswith('DA fuzz')]
                line = [l for l in p.stderr.splitlines() if l.startswith('FUXI MTL: draw area submits=')][-1]
                assert 'FUXI MTL: policy draw_area=1 draw_area_min_px=1 ' in p.stderr, name
                if name in ('budget0', 'render-area-off'):
                    assert ' shrunk=0 ' in line, line
                else:
                    assert ' shrunk=0 ' not in line, line
                print('draw area %s args=%r: %d pixel lines identical; %s' % (name, args, len(r), ' '.join(l[3:] for l in total)))
                print('  ' + line[:400])
                if name == 'pass-times':
                    rows = [l for l in p.stderr.splitlines() if 'gpu pass draw area per submit' in l]
                    print('  ' + (rows[-1] if rows else 'no gpu pass draw area line (no pass timestamps on this ICD)'))
        print('PASS: occlusion proofs and pixels')

if __name__ == '__main__':
    main()
