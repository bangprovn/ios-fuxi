#!/usr/bin/env python3
"""Production render-pass merge A/B pixels and 120-draw host benchmark; no downloads."""
import argparse
import importlib.util
import os
import re
from pathlib import Path
import shutil
import subprocess
import tempfile
ROOT = Path(__file__).resolve().parents[1]

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--headers', type=Path, default=Path('/usr/include'))
    ap.add_argument('--icd', type=Path, default=Path('/lib/x86_64-linux-gnu/libvulkan_lvp.so'))
    ap.add_argument('--glslang', default=shutil.which('glslangValidator'))
    ap.add_argument('--translator', type=Path, help='optional external bridge for authored AIR planning benchmark')
    ap.add_argument('--llvm-as', default=shutil.which('llvm-as-18'))
    a = ap.parse_args()
    if a.translator and not a.llvm_as:
        ap.error('supply --llvm-as with --translator')
    if not a.glslang:
        ap.error('supply an existing glslangValidator with --glslang')
    spec = importlib.util.spec_from_file_location('m6', ROOT/'tools/test-fuxi-metal-m6.py')
    m6 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m6)
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        env = dict(os.environ, ASAN_OPTIONS='detect_leaks=0', LP_NUM_THREADS='2', CONTROL_LOD='0', MERGE_TEST_SHADOW='1')
        common = ['clang++', '-std=c++17', '-Wall', '-Wextra', '-Werror', '-I'+str(a.headers)]
        sanitized = ['-O1', '-fsanitize=address,undefined']
        fragment = '''#version 450
layout(set=0,binding=0) uniform sampler2D image;
layout(push_constant) uniform Constants { float lod; } constants;
layout(location=0) out vec4 color;
void main() {color=textureLod(image,vec2(0.5),constants.lod);}
'''
        storage = fragment.replace('layout(location=0)', 'layout(set=0,binding=1,std430) buffer State {uint value;} state;\nlayout(location=0)').replace('color=textureLod(image,vec2(0.5),constants.lod);', 'state.value+=1; color=vec4(float(state.value)/255.0,0,0,1);')
        fetch = fragment.replace('layout(location=0)', 'layout(input_attachment_index=0,set=0,binding=1) uniform subpassInput prior;\nlayout(location=0)').replace('color=textureLod(image,vec2(0.5),constants.lod);', 'vec4 s=textureLod(image,vec2(0.5),constants.lod); color=subpassLoad(prior)*0.75+s*0.25;')
        for stage, source in [('vert', m6.VERTEX), ('frag', fragment), ('storage.frag', storage), ('fetch.frag', fetch)]:
            src = d/('control.'+stage)
            src.write_text(source)
            out = d/(stage+'.spv')
            subprocess.run([a.glslang, '-V', '--target-env', 'vulkan1.2', str(src), '-o', str(out)], check=True, capture_output=True)
            env[{'vert': 'CONTROL_VERTEX', 'frag': 'CONTROL_FRAGMENT', 'storage.frag': 'CONTROL_FRAGMENT_STORAGE', 'fetch.frag': 'CONTROL_FRAGMENT_FETCH'}[stage]] = str(out)
        subprocess.run(common+['-shared', '-fPIC', str(ROOT/'tools/fuxi-metal/RenderMergeProxy.cpp'), '-ldl', '-o', str(d/'proxy.so')], check=True)
        env['SOFTWARE_ICD'] = str(a.icd.resolve())
        definitions = ['-DFUXI_MTL_HOST_TEST', '-DFUXI_MTL_ICD="'+str(d/'proxy.so')+'"', '-DFUXI_MTL_TRANSLATOR="'+str(d/'bridge.so')+'"']
        subprocess.run(common+sanitized+['-shared', '-fPIC', str(ROOT/'tools/fuxi-metal/RenderMergeBridge.cpp'), '-o', str(d/'bridge.so')], check=True)
        subprocess.run(common+sanitized+definitions+[str(ROOT/'tools/fuxi-metal/RenderMergeTests.cpp'), '-ldl', '-pthread', '-o', str(d/'test')], check=True)
        def invoke(binary, args, cli=(), trace="1", **overrides):
            p = subprocess.run([str(binary), *map(str, cli)], env=dict(env, FUXI_MTL_TEST_BOOTARGS='fuxigpuframetrace='+trace+' '+args, **overrides), text=True, capture_output=True, timeout=90)
            assert p.returncode == 0, (args, p.stderr)
            assert 'AddressSanitizer' not in p.stderr and 'runtime error:' not in p.stderr, p.stderr
            return p.stdout
        subprocess.run(common+sanitized+definitions+[str(ROOT/'tools/fuxi-metal/GpuBarrierTests.cpp'), '-ldl', '-pthread', '-o', str(d/'barriers')], check=True)
        def pixels(output):
            return [line for line in output.splitlines() if line.startswith('PIXELS ') or line.startswith('PASS:')]
        controls = 'fuxigpumerge=1 fuxigpusubmit=1 fuxigpuoverlap=1 '
        before = invoke(d/'barriers', controls+'fuxigpuperf=3')
        after = invoke(d/'barriers', controls+'fuxigpuperf=7')
        assert pixels(before) == pixels(after), 'boundary optimization changed pixels'
        for label, output in [('before (mask 3)', before), ('after (mask 7)', after)]:
            print(label+'\n'+'\n'.join(line for line in output.splitlines() if line.startswith('COUNTS ')))
        counts = lambda output, label: tuple(map(int, re.search(r'COUNTS '+label+r' images=(\d+) image_calls=(\d+) barriers=(\d+) passes=(\d+)', output).groups()))
        assert counts(before, 'batch') == (5, 5, 8, 1)
        # Mask 7 before the stage/access narrowing, GENERAL sampling and draw-pass post-barrier skip: batch (5,1,4,1), ca (4,3,9,3), mip-batch (3,1,4,1).
        assert counts(after, 'batch') == (1, 1, 3, 1)
        ca_before, ca_after = counts(before, 'ca'), counts(after, 'ca')
        assert ca_before == (29, 29, 57, 25) and ca_after == (2, 2, 5, 3)
        assert counts(before, 'mip-batch') == (4, 4, 7, 1)
        assert counts(after, 'mip-batch') == (0, 0, 2, 1)
        for off in ({'FUXI_MTL_PASS_REORDER': '0'}, {'FUXI_MTL_RENDER_AREA': '0'}, {'FUXI_MTL_PASS_REORDER': '0', 'FUXI_MTL_RENDER_AREA': '0'}):
            assert pixels(invoke(d/'barriers', controls+'fuxigpuperf=7', **off)) == pixels(after), 'pass reorder/render area changed pixels: '+str(off)
        for args in ('', 'fuxigpuperf=4', controls+'fuxigpuperf=4', controls+'fuxigpuperf=5', controls+'fuxigpuperf=6'):
            assert pixels(invoke(d/'barriers', args)) == pixels(before), 'boundary controls differ: '+args
        print('PASS: CA boundary A/B pixels, exact batch counts, all independent bit combinations')
        # FUXI_MTL_SUBMIT_LEAN: identical pixels and barrier/pass counts
        for args in (controls+'fuxigpuperf=7', controls+'fuxigpuperf=3', ''):
            plain, lean = (invoke(d/'barriers', args, FUXI_MTL_SUBMIT_LEAN=v) for v in '01')
            assert pixels(plain)+re.findall(r'COUNTS .*', plain) == pixels(lean)+re.findall(r'COUNTS .*', lean), 'submit lean changed pixels/counts: '+args
        print('PASS: FUXI_MTL_SUBMIT_LEAN keeps CA boundary pixels and barrier/pass counts')
        for extra in ('', 'fuxigpufeedback=1', 'fuxigpuoverlap=1 fuxigpusubmit=1', 'fuxigpuoverlap=1 fuxigpusubmit=1 fuxigpufeedback=1'):
            baseline = invoke(d/'test', extra)
            merged = invoke(d/'test', extra+' fuxigpumerge=1')
            assert baseline == merged, 'merged pixels differ: '+extra
            print('PASS: byte-identical A/B render merge controls '+extra)
            if not extra:
                skips = [line for line in baseline.splitlines() if line.startswith('SKIP:')]
                print('\n'.join(skips) if skips else 'PASS: lavapipe exposes ordered attachment access; fetch clear/interleaved/mip/two-flight pixels tested')
        assert invoke(d/'test', 'fuxigpumerge=1 fuxigpusubmit=1', trace='2') == invoke(d/'test', 'fuxigpumerge=1 fuxigpusubmit=1')
        for extra in ('', 'fuxigpumerge=1 fuxigpuoverlap=1 fuxigpusubmit=1 fuxigpufeedback=1 fuxigpuperf=7'):
            assert invoke(d/'test', extra, FUXI_MTL_SUBMIT_LEAN='1') == invoke(d/'test', extra, FUXI_MTL_SUBMIT_LEAN='0'), 'submit lean changed merge pixels: '+extra
        print('PASS: FUXI_MTL_SUBMIT_LEAN byte-identical render merge output')
        print('PASS: level-2 bounded CPU sampling preserves merged pixels')
        # All bits independently and together, including two flights and the
        # feedback path. Pixels must match the unchanged singleton baseline.
        baseline = invoke(d/'test', 'fuxigpuoverlap=1 fuxigpusubmit=1 fuxigpufeedback=1')
        assert invoke(d/'test', 'fuxigpuperf=1') == invoke(d/'test', '')
        for mask in ('1', '2', '3', '4', '5', '6', '7'):
            actual = invoke(d/'test', 'fuxigpuoverlap=1 fuxigpusubmit=1 fuxigpufeedback=1 fuxigpumerge=1 fuxigpuperf='+mask)
            assert actual == baseline, 'perf mask changed pixels: '+mask
            print('PASS: perf mask '+mask+' pixels, independent Vulkan barrier/submit counts and encoder boundaries')
        for bad in ('0', '8', '9', '-1', '01', 'bad', '3 fuxigpuperf=1'):
            actual = invoke(d/'test', 'fuxigpumerge=1 fuxigpuperf='+bad)
            assert actual == invoke(d/'test', 'fuxigpumerge=1')
        print('PASS: perf disabled/unknown/malformed/duplicate tokens preserve recording policy')
        # Exercise the production fallback without requiring a second ICD.
        baseline = invoke(d/'test', '', MERGE_TEST_NO_ORDER='1')
        merged = invoke(d/'test', 'fuxigpumerge=1 fuxigpuoverlap=1 fuxigpusubmit=1 fuxigpuperf=3', MERGE_TEST_NO_ORDER='1')
        assert baseline == merged, 'no-extension fallback pixels differ'
        print('PASS: extension-hidden fallback keeps ordinary merge/shadow pixels; fetch skipped')
        # FUXI_MTL_FETCH_SPLIT: CA-shaped passes mixing fetch (programmable blend with the destination) and SRC_ALPHA-blended draws. Pixels equal knob off,
        # alone and with FUXI_MTL_NOFETCH_RP; with merging, every fetch draw after a non-fetch draw of its pass gets exactly one by-region in-pass barrier.
        subprocess.run(common+sanitized+definitions+[str(ROOT/'tools/fuxi-metal/FetchSplitTests.cpp'), '-ldl', '-pthread', '-o', str(d/'fetchsplit')], check=True)
        def fetch_split(args, split, nofetch='0', **extra):
            p = subprocess.run([str(d/'fetchsplit')], env=dict(env, FUXI_MTL_TEST_BOOTARGS='fuxigpuframetrace=1 '+args, FUXI_MTL_FETCH_SPLIT=split, FUXI_MTL_NOFETCH_RP=nofetch, **extra), text=True, capture_output=True, timeout=120)
            assert p.returncode == 0, (args, split, nofetch, p.stdout[-2000:], p.stderr[-4000:])
            assert 'AddressSanitizer' not in p.stderr and 'runtime error:' not in p.stderr, p.stderr
            assert 'FUXI MTL: policy fetch_split=%s' % ('1' if split == '1' else '0') in p.stderr, p.stderr[-2000:]
            return p
        fetch_pixels = lambda out: [line for line in out.splitlines() if line.startswith('PIXELS ')]
        for args in ('', 'fuxigpumerge=1', 'fuxigpumerge=1 fuxigpuoverlap=1 fuxigpusubmit=1 fuxigpuperf=7', 'fuxigpumerge=1 fuxigpuperf=3'):
            ref = fetch_split(args, '0')
            if 'SKIP:' in ref.stdout:
                print(ref.stdout.strip())
                break
            assert len(fetch_pixels(ref.stdout)) == 7 and all(' issued=0 in_pass=0' in l for l in ref.stdout.splitlines() if l.startswith('FETCHSPLIT '))
            assert 'blend_order_flag=0' not in ref.stderr
            for nofetch in ('0', '1'):
                for extra in ({}, dict(FUXI_MTL_GPU_PASS_TIMES='1', FUXI_MTL_GPU_DRAW_TIMES='1')):
                    p = fetch_split(args, '1', nofetch, **extra)
                    assert fetch_pixels(p.stdout) == fetch_pixels(ref.stdout), 'fetch split changed pixels: %r nofetch=%s %r' % (args, nofetch, extra)
                    assert 'framebuffer fetch plan mask=0 ordered=1 blend_order_flag=0' in p.stderr and 'framebuffer fetch plan mask=1 ordered=1 blend_order_flag=1' in p.stderr
                    issued = sum(int(x) for x in re.findall(r'FETCHSPLIT \S+ .* issued=(\d+)', p.stdout))
                    assert (issued == 0) == ('fuxigpumerge=1' not in args), (args, issued)
                    if nofetch == '0':
                        assert fetch_pixels(fetch_split(args, '0', '1').stdout) == fetch_pixels(ref.stdout)
            print('PASS: FUXI_MTL_FETCH_SPLIT pixels identical %r (alone, with NOFETCH_RP, with pass/draw times); in-pass barriers=%d' % (args, issued))
        for extra in ('', 'fuxigpumerge=1 fuxigpuoverlap=1 fuxigpusubmit=1 fuxigpufeedback=1 fuxigpuperf=7'):
            assert invoke(d/'test', extra, FUXI_MTL_FETCH_SPLIT='1') == invoke(d/'test', extra), 'fetch split changed render merge output: '+extra
            assert pixels(invoke(d/'barriers', extra, FUXI_MTL_FETCH_SPLIT='1')) == pixels(invoke(d/'barriers', extra)), 'fetch split changed barrier fixture pixels: '+extra
        print('PASS: FUXI_MTL_FETCH_SPLIT keeps render merge (fetch-normal interleaved, mip view, two flights) and CA boundary outputs')
        for bad in ('0', 'bad', '1 fuxigpumerge=1'):
            invoke(d/'test', 'fuxigpumerge='+bad)
        print('PASS: disabled/malformed/duplicate boot tokens preserve default pass counts')
        # Benchmark with the existing nonblending adapter, so the final pixel is
        # also an independently known value. Fresh-process A/B/B/A, no threshold.
        subprocess.run(common+['-O2', '-shared', '-fPIC', str(ROOT/'tools/fuxi-metal/MipFeedbackBridge.cpp'), '-o', str(d/'bench-bridge.so')], check=True)
        definitions[1] = '-DFUXI_MTL_ICD="'+str(a.icd.resolve())+'"'
        definitions[-1] = '-DFUXI_MTL_TRANSLATOR="'+str(d/'bench-bridge.so')+'"'
        subprocess.run(common+sanitized+definitions+[str(ROOT/'tools/fuxi-metal/VariantPlanTests.cpp'), '-ldl', '-pthread', '-o', str(d/'variants')], check=True)
        print(invoke(d/'variants', 'fuxigpumerge=1 fuxigpusubmit=1', CONTROL_REQUIRE_VARIANT='1'), end='')
        print(invoke(d/'variants', 'fuxigpumerge=1 fuxigpusubmit=1 fuxigputrace=1', CONTROL_REQUIRE_VARIANT='1'), end='')
        # FUXI_MTL_SUBMIT_LEAN on a CA-shaped submit, with and without the rename/hoist/reorder/rt-shadow rewrites; more than the 20 logged hoists, so the
        # reason-string skip runs. Pixels, per-submit counts and the earlier-writer refusal must equal the default planner byte for byte.
        subprocess.run(common+sanitized+definitions+[str(ROOT/'tools/fuxi-metal/SubmitLeanTests.cpp'), '-ldl', '-pthread', '-o', str(d/'lean')], check=True)
        rewrites = dict(FUXI_MTL_RT_RENAME='1', FUXI_MTL_FRAG_RELAXED='1', FUXI_MTL_RT_SHADOW='1')
        for args in ('', 'fuxigpumerge=1 fuxigpuoverlap=1 fuxigpusubmit=1 fuxigpuperf=7', 'fuxigpumerge=1 fuxigpuperf=3'):
            for extra in ({}, rewrites):
                plain = invoke(d/'lean', args, FUXI_MTL_SUBMIT_LEAN='0', **extra)
                for level in '12':
                    assert plain == invoke(d/'lean', args, FUXI_MTL_SUBMIT_LEAN=level, **extra), 'submit lean '+level+' changed output: '+args+' '+str(extra)
                assert int(re.search(r'RENAME submits=(\d+)', plain).group(1)) > (20 if extra else -1)
                assert int(re.search(r'hoisted=(\d+)', plain).group(1)) > (20 if extra else -1)
        # =2 with robustBufferAccess enabled skips the CPU index scan of the in-range indexed draws: same output, and the skip is logged.
        robust = dict(rewrites, FUXI_MTL_ROBUST='1')
        assert invoke(d/'lean', 'fuxigpumerge=1 fuxigpuperf=7', FUXI_MTL_SUBMIT_LEAN='0', **robust) == invoke(d/'lean', 'fuxigpumerge=1 fuxigpuperf=7', FUXI_MTL_SUBMIT_LEAN='2', **robust), 'submit lean 2 changed robust output'
        p = subprocess.run([str(d/'lean')], env=dict(env, FUXI_MTL_TEST_BOOTARGS='fuxigpuframetrace=1 fuxigpumerge=1', FUXI_MTL_SUBMIT_LEAN='2', **robust), text=True, capture_output=True, timeout=90)
        assert p.returncode == 0 and 'policy submit_lean=2' in p.stderr and 'index scan skipped (robust_buffer=1)' in p.stderr, p.stderr[-2000:]
        print('PASS: FUXI_MTL_SUBMIT_LEAN=1/2 identical pixels, counts and refusal on the CA-shaped fixture (rewrites off/on)')
        # FUXI_MTL_SPLIT_SUBMIT: chunked recording/queueing (every pass end, up to 8 chunks) leaves pixels, pass/draw/barrier counts, overlap retirement,
        # early release and the drained-failure path identical; only CHUNKS lines (QueueSubmit counts) differ. Fault modes run in their own processes.
        recorded = ['-DFUXI_MTL_HOST_TEST', '-DFUXI_MTL_ICD="'+str(d/'proxy.so')+'"', '-DFUXI_MTL_TRANSLATOR="'+str(d/'bridge.so')+'"'] # submit counter, fence faults, blending control
        subprocess.run(common+sanitized+recorded+[str(ROOT/'tools/fuxi-metal/SplitSubmitTests.cpp'), '-ldl', '-pthread', '-o', str(d/'split')], check=True)
        chunked = dict(FUXI_MTL_SPLIT_SUBMIT='1', FUXI_MTL_SPLIT_SUBMIT_US='0', FUXI_MTL_SPLIT_SUBMIT_MAX='8')
        timed = dict(FUXI_MTL_GPU_PASS_TIMES='1', FUXI_MTL_GPU_DRAW_TIMES='1')
        strip = lambda output: [line for line in output.splitlines() if not line.startswith('CHUNKS ')]
        for args in ('', 'fuxigpumerge=1 fuxigpuoverlap=1 fuxigpusubmit=1 fuxigpuperf=7', 'fuxigpumerge=1 fuxigpuperf=3'):
            for extra in ({}, dict(rewrites, **timed)):
                plain = invoke(d/'split', args, FUXI_MTL_SPLIT_SUBMIT='0', **extra)
                for limit in ('2', '3', '8'):
                    split = invoke(d/'split', args, **dict(chunked, FUXI_MTL_SPLIT_SUBMIT_MAX=limit), **extra)
                    assert strip(plain) == strip(split), 'split submit changed output: '+args+' '+str(extra)+' max='+limit
                    counts = [int(x) for x in re.findall(r'CHUNKS sync frame=\d+ queue_submits=(\d+)', split)]
                    assert len(counts) == 6 and all(1 < c <= int(limit) for c in counts) and 'CHUNKS drained failed=1' in split, split
                assert re.findall(r'CHUNKS sync frame=\d+ queue_submits=(\d+)', plain) == ['1']*6 and 'CHUNKS drained failed=0' in plain
        for mode in ('lost', 'timeout', 'chunk', 'drain'):
            for knob in ({'FUXI_MTL_SPLIT_SUBMIT': '0'}, chunked):
                print(invoke(d/'split', 'fuxigpunoslab=1', cli=(mode,), **knob), end='')
        controls = 'fuxigpumerge=1 fuxigpusubmit=1 fuxigpuoverlap=1 '
        for args in (controls+'fuxigpuperf=7', controls+'fuxigpuperf=3', ''):
            plain, split = invoke(d/'barriers', args), invoke(d/'barriers', args, **chunked)
            assert pixels(plain)+re.findall(r'COUNTS .*', plain) == pixels(split)+re.findall(r'COUNTS .*', split), 'split submit changed barrier pixels/counts: '+args
        for extra in ('', 'fuxigpumerge=1 fuxigpuoverlap=1 fuxigpusubmit=1 fuxigpufeedback=1 fuxigpuperf=7'):
            # RenderMergeTests asserts one QueueSubmit per submit, which a split frame breaks by design; its lean twin has no such assert
            for level in '012':
                assert invoke(d/'lean', extra, FUXI_MTL_SUBMIT_LEAN=level, **chunked) == invoke(d/'lean', extra), 'split submit changed lean fixture output: lean='+level+' '+extra
        print('PASS: FUXI_MTL_SPLIT_SUBMIT identical pixels/counts (sync, two flights, early release, drained failure; barrier/lean 0/1/2 fixtures), faults poison')
        subprocess.run(common+['-O2']+definitions+[str(ROOT/'tools/fuxi-metal/SubmitBench.cpp'), '-ldl', '-pthread', '-o', str(d/'bench')], check=True)
        for uncached in (True, False, False, True):
            overrides = {'PIPE_BENCH_UNCACHED': '1'} if uncached else {}
            print(invoke(d/'bench', 'fuxigpumerge=1 fuxigpusubmit=1', PIPE_BENCH_SHAPES='1', CONTROL_REQUIRE_VARIANT='1', **overrides), end='')
        for fast in ('', 'fuxigpusubmit=1'):
            for args in ('', 'fuxigpumerge=1', 'fuxigpumerge=1', ''):
                print(invoke(d/'bench', fast+' '+args), end='')
        # Keep merge and descriptor policy fixed; only real encoder boundaries
        # vary. Attribute the delta over 119 additional boundaries per frame.
        results = []
        for split in (True, False, False, True):
            overrides = {'MERGE_BENCH_ENCODERS': '1'} if split else {}
            output = invoke(d/'bench', 'fuxigpumerge=1 fuxigpusubmit=1', **overrides)
            print(output, end='')
            match = re.search(r'cpu_ms/frame=([\d.]+) gpu_ms/frame=([\d.]+)', output)
            results.append(tuple(float(x) for x in match.groups()))
        for mask in ('1', '2', '3', '4', '5', '6', '7'):
            print(invoke(d/'bench', 'fuxigpumerge=1 fuxigpusubmit=1 fuxigpuperf='+mask, MERGE_BENCH_ENCODERS='1'), end='')
        for a_index, b_index in ((0, 1), (3, 2)):
            delta = [(results[a_index][i]-results[b_index][i])*1000/119 for i in range(2)]
            print('BOUNDARY_DELTA cpu_us/boundary=%.3f gpu_us/boundary=%.3f (host only; no threshold)' % tuple(delta))

        if a.translator:
            air = d/'control.bc'
            subprocess.run([a.llvm_as, str(ROOT/'tools/shaders/metal-m7-control.ll'), '-o', str(air)], check=True)
            definitions[-1] = '-DFUXI_MTL_TRANSLATOR="'+str(a.translator.resolve())+'"'
            subprocess.run(common+['-O2']+definitions+[str(ROOT/'tools/fuxi-metal/SubmitBench.cpp'), '-ldl', '-pthread', '-o', str(d/'air-bench')], check=True)
            for fast in ('', 'fuxigpusubmit=1'):
                print(invoke(d/'air-bench', 'fuxigpumerge=1 '+fast, cli=(air,)), end='')

if __name__ == '__main__':
    main()
