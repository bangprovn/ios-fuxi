#!/usr/bin/env python3
"""Local frame tracing and opt-in scratch reuse controls; no external translator."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile
ROOT = Path(__file__).resolve().parents[1]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--headers', type=Path, default=Path('/usr/include'))
    ap.add_argument('--icd', type=Path, default=Path('/lib/x86_64-linux-gnu/libvulkan_lvp.so'))
    a = ap.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        binary = Path(tmp)/'test'
        subprocess.run(['clang++', '-std=c++17', '-Wall', '-Wextra', '-Werror',
                        '-fsanitize=address,undefined', '-DFUXI_MTL_HOST_TEST',
                        '-DFUXI_MTL_ICD="'+str(a.icd.resolve())+'"', '-I'+str(a.headers),
                        str(ROOT/'tools/fuxi-metal/FrameReuseTests.cpp'), '-ldl', '-pthread', '-o', str(binary)], check=True)
        for flag in ('', 'fuxigpupipelined=1', 'fuxigpupipelined=1 fuxigpupipelined=1',
                     'fuxigpupipelined=invalid', 'fuxigpuframetrace=2'):
            p = subprocess.run([str(binary)], env=dict(os.environ, ASAN_OPTIONS='detect_leaks=0',
                LP_NUM_THREADS='2', FUXI_MTL_TEST_BOOTARGS=flag if flag.startswith('fuxigpuframetrace=') else 'fuxigpuframetrace=1 '+flag),
                capture_output=True, text=True, timeout=60)
            assert p.returncode == 0, p.stderr
            assert 'AddressSanitizer' not in p.stderr and 'runtime error:' not in p.stderr, p.stderr
            print(p.stdout, end='')
        # fmtl bits 16-21: the GPU pass and draw timers, RT_SHADOW, its reuse, RT_RENAME and the 128 MB shadow cap without a root write.
        src = Path(tmp)/'fmtl.cpp'
        src.write_text('#include "TranslationPolicy.hpp"\n#include <cstdio>\nint main(){fuxi::applyBootOptions("debug=0xc fmtl=350021 x=1");'
                       'const char *k[]={"FUXI_MTL_ORDER_FETCH_ONLY","FUXI_MTL_SAMPLE_SHADOW","FUXI_MTL_GPU_PASS_TIMES","FUXI_MTL_GPU_DRAW_TIMES","FUXI_MTL_RT_SHADOW","FUXI_MTL_RT_SHADOW_REUSE","FUXI_MTL_RT_RENAME","FUXI_MTL_RT_SHADOW_MB"};'
                       'for(auto *s:k){const char *v=getenv(s);printf("%s=%s\\n",s,v?v:"-");}}\n')
        subprocess.run(['clang++', '-std=c++17', '-Wall', '-Wextra', '-Werror', '-I'+str(a.headers), '-I'+str(ROOT/'tools/fuxi-metal'),
                        str(src), '-o', str(Path(tmp)/'fmtl')], check=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith('FUXI_MTL_')}
        out = subprocess.run([str(Path(tmp)/'fmtl')], env=env, capture_output=True, text=True, timeout=30, check=True).stdout
        assert out.split() == ['FUXI_MTL_ORDER_FETCH_ONLY=1', 'FUXI_MTL_SAMPLE_SHADOW=1', 'FUXI_MTL_GPU_PASS_TIMES=1',
                               'FUXI_MTL_GPU_DRAW_TIMES=-', 'FUXI_MTL_RT_SHADOW=1',
                               'FUXI_MTL_RT_SHADOW_REUSE=-', 'FUXI_MTL_RT_RENAME=1', 'FUXI_MTL_RT_SHADOW_MB=128'], out


if __name__ == '__main__':
    main()
