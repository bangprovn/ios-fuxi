#!/usr/bin/env python3
"""FUXI_MTL_DESC_REUSE and FUXI_MTL_BIND_SKIP pixel A/B on the production backend (lavapipe, ReuseBindBridge); no downloads.

Every PIXELS line with a knob on must equal the knob-off line of the same boot arguments; the COUNTS lines show the kept sets doing the
work (no set made in a steady frame, sets dropped when a texture, sampler, buffer or pipeline they name is destroyed, the second pool
rotation refused while a flight that bound its sets is pending) and the skipped binds (same draws, fewer push/vertex/index calls)."""
import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
ROOT = Path(__file__).resolve().parents[1]
VERTEX = r'''#version 450
layout(location=0) in vec2 position;
layout(push_constant) uniform Constants {layout(offset=16) vec2 offset;} constants;
void main() {gl_Position=vec4(position+constants.offset,0.0,1.0);}
'''
FRAGMENT = r'''#version 450
layout(set=0,binding=0) uniform sampler2D image;
layout(set=0,binding=1) uniform Tint {vec4 tint;} tint;
layout(push_constant) uniform Constants {vec4 color;} constants;
layout(location=0) out vec4 color;
void main() {color=textureLod(image,vec2(0.5),0.0)*constants.color*tint.tint;}
'''


def counts(out):
    rows = {}
    for line in out.splitlines():
        if line.startswith('COUNTS '):
            label, *fields = line.split()[1:]
            rows.setdefault(label, []).append(dict(f.split('=') for f in fields))
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--headers', type=Path, default=Path('/usr/include'))
    ap.add_argument('--icd', type=Path, default=Path('/lib/x86_64-linux-gnu/libvulkan_lvp.so'))
    ap.add_argument('--glslang', default=shutil.which('glslangValidator'))
    a = ap.parse_args()
    if not a.glslang:
        ap.error('supply an existing glslangValidator with --glslang')
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        env = dict(os.environ, ASAN_OPTIONS='detect_leaks=0', LP_NUM_THREADS='2', SOFTWARE_ICD=str(a.icd.resolve()))
        for k in [k for k in env if k.startswith('FUXI_MTL_')]:
            del env[k]
        for stage, source, key in (('vert', VERTEX, 'BIND_VERTEX'), ('frag', FRAGMENT, 'BIND_FRAGMENT')):
            src = d/('bind.'+stage)
            src.write_text(source)
            subprocess.run([a.glslang, '-V', '--target-env', 'vulkan1.2', str(src), '-o', str(d/(stage+'.spv'))], check=True, capture_output=True)
            env[key] = str(d/(stage+'.spv'))
        common = ['clang++', '-std=c++17', '-Wall', '-Wextra', '-Werror', '-I'+str(a.headers)]
        sanitized = ['-O1', '-fsanitize=address,undefined']
        subprocess.run(common+['-shared', '-fPIC', str(ROOT/'tools/fuxi-metal/RenderMergeProxy.cpp'), '-ldl', '-o', str(d/'proxy.so')], check=True)
        subprocess.run(common+sanitized+['-shared', '-fPIC', str(ROOT/'tools/fuxi-metal/ReuseBindBridge.cpp'), '-o', str(d/'bridge.so')], check=True)
        definitions = ['-DFUXI_MTL_HOST_TEST', '-DFUXI_MTL_ICD="'+str(d/'proxy.so')+'"', '-DFUXI_MTL_TRANSLATOR="'+str(d/'bridge.so')+'"']
        subprocess.run(common+sanitized+definitions+[str(ROOT/'tools/fuxi-metal/ReuseBindTests.cpp'), '-ldl', '-pthread', '-o', str(d/'test')], check=True)

        def invoke(args, cli=(), **knobs):
            p = subprocess.run([str(d/'test'), *cli], env=dict(env, FUXI_MTL_TEST_BOOTARGS='fuxigpuframetrace=1 '+args, **knobs), text=True, capture_output=True, timeout=600)
            assert p.returncode == 0, (args, knobs, p.stdout[-2000:], p.stderr[-4000:])
            assert 'AddressSanitizer' not in p.stderr and 'runtime error:' not in p.stderr, p.stderr
            assert 'PASS: reuse/bind fixture' in p.stdout, p.stdout
            return p

        def pixels(out):
            return [line for line in out.splitlines() if line.startswith('PIXELS ')]
        boots = ('fuxigpusubmit=1', 'fuxigpusubmit=1 fuxigpumerge=1', 'fuxigpusubmit=1 fuxigpumerge=1 fuxigpuperf=7 fuxigpuoverlap=1',
                 'fuxigpusubmit=1 fuxigpunoslab=1', 'fuxigpusubmit=1 fuxigpumerge=1 fuxigpunoslab=1')
        for args in boots:
            for extra in ({}, {'FUXI_MTL_SPLIT_SUBMIT': '1', 'FUXI_MTL_SPLIT_SUBMIT_US': '0'}):
                ref = invoke(args, **extra)
                c = counts(ref.stdout)
                assert all(r['reused'] == '0' and r['made'] == '0' for rows in c.values() for r in rows), 'knob off keeps no set'
                assert 'ROTATIONS 0 BLOCKED 0' in ref.stdout and 'KEPT 0' in ref.stdout
                on = invoke(args, FUXI_MTL_DESC_REUSE='1', **extra)
                assert pixels(on.stdout) == pixels(ref.stdout), (args, extra, pixels(ref.stdout), pixels(on.stdout))
                assert 'FUXI MTL: policy desc_reuse=1 submit_fast=1 set_limit=512' in on.stderr, on.stderr[-2000:]
                c = counts(on.stdout)
                steady = [r for label, rows in c.items() if label.startswith('steady-') and label != 'steady-1' for r in rows]
                assert len(steady) == 23 and all(r['set_allocs'] == '0' and r['made'] == '0' and int(r['reused']) > 0 for r in steady), steady
                assert int(c['steady-1'][0]['made']) > 0 and c['steady-1'][0]['set_allocs'] == c['steady-1'][0]['made'], c['steady-1']
                for label in ('texture-swap', 'sampler-swap', 'pipeline-swap'):
                    r = c[label][0]
                    assert int(r['forgotten']) > 0 and int(r['made']) > 0, (label, r)
                if 'noslab' in args:
                    assert int(c['buffer-swap'][0]['forgotten']) > 0, c['buffer-swap']
                rot = re.search(r'ROTATIONS (\d+) BLOCKED (\d+)', on.stdout)
                assert int(rot.group(1)) >= 2 and int(rot.group(2)) >= 1, rot.group(0)
                # Without the per-submit dedupe (fuxigpusubmit=1) there is no set to keep: the knob changes nothing.
                if not extra:
                    plain = invoke(args.replace('fuxigpusubmit=1', '').strip())
                    plain_on = invoke(args.replace('fuxigpusubmit=1', '').strip(), FUXI_MTL_DESC_REUSE='1')
                    assert pixels(plain_on.stdout) == pixels(plain.stdout) == pixels(ref.stdout)
                    assert 'needs fuxigpusubmit=1' in plain_on.stderr and 'KEPT 0' in plain_on.stdout
                info = [line for line in on.stdout.splitlines() if line.startswith(('INFO', 'ROTATIONS', 'KEPT'))]
                print('PASS: desc reuse pixels identical %r %r; %s; steady reused/frame=%s' % (args, extra, ' '.join(info), steady[-1]['reused']))
                # FUXI_MTL_BIND_SKIP, alone and with the kept sets: the same pixels and draws, fewer push/vertex/index calls.
                skip = invoke(args, FUXI_MTL_BIND_SKIP='1', **extra)
                both = invoke(args, FUXI_MTL_BIND_SKIP='1', FUXI_MTL_DESC_REUSE='1', **extra)
                for run in (skip, both):
                    assert pixels(run.stdout) == pixels(ref.stdout), (args, extra, pixels(ref.stdout), pixels(run.stdout))
                    assert 'FUXI MTL: policy bind_skip=1' in run.stderr
                cr, cs = counts(ref.stdout), counts(skip.stdout)
                assert cr.keys() == cs.keys()
                for label in cr:
                    for r, s in zip(cr[label], cs[label]):
                        assert r['draws'] == s['draws'] and r['indexed'] == s['indexed'] and r['set_binds'] == s['set_binds'], (label, r, s)
                        assert int(s['pushes']) <= int(r['pushes']) and int(s['vertex']) <= int(r['vertex']) and int(s['index']) <= int(r['index']), (label, r, s)
                r, s = cr['steady-2'][0], cs['steady-2'][0]
                assert int(s['pushes']) < int(r['pushes']) and int(s['vertex']) < int(r['vertex']) and int(s['index']) < int(r['index']) and int(s['index']) > 0, (r, s)
                print('PASS: bind skip pixels identical %r %r; steady frame pushes %s->%s vertex %s->%s index %s->%s'
                      % (args, extra, r['pushes'], s['pushes'], r['vertex'], s['vertex'], r['index'], s['index']))
        # Without ASan's quarantine a destroyed object's address (lavapipe's handle) goes to the next object of that size at once, so the
        # swaps above hand a kept set's handles to new objects. The destroy-time rule must keep the pixels; with it disabled (negative
        # control) a stale set draws the old sampler or buffer.
        for args in ('fuxigpusubmit=1 fuxigpunoslab=1', 'fuxigpusubmit=1 fuxigpumerge=1'):
            fast = dict(ASAN_OPTIONS='detect_leaks=0:quarantine_size_mb=0')
            ref = invoke(args, **fast)
            on = invoke(args, FUXI_MTL_DESC_REUSE='1', **fast)
            assert pixels(on.stdout) == pixels(ref.stdout), (args, pixels(ref.stdout), pixels(on.stdout))
            stale = invoke(args, cli=('no-forget',), FUXI_MTL_DESC_REUSE='1', **fast)
            back = 'INFO view_handle_back=1' in on.stdout
            if pixels(stale.stdout) != pixels(ref.stdout):
                print('PASS: desc reuse with handles given back %r (view_handle_back=%d); without the destroy-time rule %d of %d pixel lines differ'
                      % (args, back, sum(x != y for x, y in zip(pixels(stale.stdout), pixels(ref.stdout))), len(pixels(ref.stdout))))
            else:
                print('SKIP: negative control %r: this allocator gave no handle back (view_handle_back=%d)' % (args, back))


if __name__ == '__main__':
    main()
