#!/usr/bin/env python3
"""Generate/check the 0098 NativeAdreno patch; 0096 remains standalone.

0096 remains the baseline. 0098 carries the current native ABI client;
0100 contains its tracing/guard delta, checked by sync-fuxi-metal2.py.
0098 contains the delta to canonical driver/shared
sources plus ABI files and IOKit registration; 0097 is orthogonal.
"""
import argparse
import difflib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / 'xnu/fuxi-adreno'
FILES = {
    'bsd/dev/gpu-clock.h': '../../shim/fuxi-probe/gpu-clock.h',
    'bsd/dev/LICENSE.AdrenoFirmware': '../../shim/fuxi-probe/LICENSE.AdrenoFirmware',
    'iokit/Kernel/arm/FuxiAdrenoUserClient.cpp': 'FuxiAdrenoUserClient.cpp',
    'bsd/dev/fuxi_gpu_scanout.inc': 'fuxi_gpu_scanout.inc',
    'bsd/dev/LICENSE.NativeMetal': 'LICENSE.NativeMetal',
    'bsd/dev/fuxi_gpu_abi_backend.inc': 'fuxi_gpu_abi_backend.inc',
    'bsd/dev/fuxi_gpu_irq.inc': 'fuxi_gpu_irq.inc',
    'bsd/dev/LICENSE.ArmCpu': 'LICENSE.ArmCpu',
    'bsd/dev/LICENSE.UniXNU-sheng': 'LICENSE.UniXNU-sheng',
    'bsd/dev/LICENSE.ShengUfs': 'LICENSE.ShengUfs',
    'bsd/dev/LICENSE.GopFramebuffer': 'LICENSE.GopFramebuffer',
    'bsd/dev/fuxi_gpu_dcvs.h': 'fuxi_gpu_dcvs.h',
    'osfmk/arm/fuxi_adreno_host.c': 'fuxi_adreno_host.c',
}
for folder in ('bsd/dev', 'iokit/Kernel/arm'):
    for name in ('NativeAdrenoABI.h', 'fuxi_abi_wire.h', 'fuxi_gpu_abi.h', 'LICENSE.NativeAdreno'):
        FILES[f'{folder}/{name}'] = name


def baseline(name):
    patch = (ROOT/'xnu/patches/0096-fuxi-adreno.patch').read_text()
    block = next(p for p in re.split(r'(?=^--- a/)', patch, flags=re.M)
                 if p.startswith(f'--- a/bsd/dev/{name}\n'))
    return ''.join(line[1:] for line in block.splitlines(True)
                   if line.startswith('+') and not line.startswith('+++'))


CONFIG_BASE = '\n'*13 + 'iokit/Kernel/arm/SweetKeyStore.cpp                               optional iokitcpp\niokit/Kernel/arm/SweetWatchdog.cpp                               optional iokitcpp\niokit/Kernel/arm/SweetSurface.cpp                                optional iokitcpp\niokit/Kernel/arm/SweetFramebuffer.cpp                            optional iokitcpp\niokit/Kernel/arm/SweetBattery.cpp                                optional iokitcpp\niokit/Kernel/arm/hid/SweetHID.cpp                                optional iokitcpp\niokit/Kernel/arm/hid/IOHIDEvent.cpp                              optional iokitcpp\n'


OSFMK_CONFIG_BASE = 'OPTIONS/fb\t\t\toptional fb\n\nOPTIONS/debug\t\t\toptional debug\n\nosfmk/vm/vm_apple_protect.c\t standard\nosfmk/vm/vm_fourk_pager.c\t standard\n\nosfmk/arm64/hi_res_clock_map.c \toptional hi_res_clock\n\n'


def metal1_client():
    patch = (ROOT/'xnu/patches/0098-fuxi-native-adreno-abi.patch').read_text()
    block = next(p for p in re.split(r'(?=^--- a/)', patch, flags=re.M)
                 if p.startswith('--- a/iokit/Kernel/arm/FuxiAdrenoUserClient.cpp\n'))
    return ''.join(line[1:] for line in block.splitlines(True)
                   if line.startswith('+') and not line.startswith('+++'))


def content():
    line = 'iokit/Kernel/arm/SweetFramebuffer.cpp                            optional iokitcpp\n'
    changed = CONFIG_BASE.replace(line, line+'iokit/Kernel/arm/FuxiAdrenoUserClient.cpp                         optional iokitcpp\n')
    patch = ''.join(difflib.unified_diff(CONFIG_BASE.splitlines(True), changed.splitlines(True),
        fromfile='a/iokit/conf/files.arm64', tofile='b/iokit/conf/files.arm64'))
    line = 'osfmk/vm/vm_apple_protect.c\t standard\n'
    changed = OSFMK_CONFIG_BASE.replace(line, line+'osfmk/arm/fuxi_adreno_host.c\tstandard\n')
    patch += ''.join(difflib.unified_diff(OSFMK_CONFIG_BASE.splitlines(True), changed.splitlines(True),
        fromfile='a/osfmk/conf/files.arm64', tofile='b/osfmk/conf/files.arm64', n=1))
    for name in ('gpu.c', 'gpu-a740.h', 'gpu-zap.h', 'gpu-handoff.h', 'fuxi_adreno.c', 'fuxi_adreno.h'):
        folder = BASE if name.startswith('fuxi_') else ROOT/'shim/fuxi-probe'
        patch += ''.join(difflib.unified_diff(baseline(name).splitlines(True),
            (folder/name).read_text().splitlines(True), fromfile=f'a/bsd/dev/{name}', tofile=f'b/bsd/dev/{name}'))
    for target, source in FILES.items():
        patch += ''.join(difflib.unified_diff([], (metal1_client() if source == 'FuxiAdrenoUserClient.cpp' else (BASE/source).read_text()).splitlines(True),
            fromfile=f'a/{target}', tofile=f'b/{target}'))
    return patch


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--check', action='store_true')
    args = ap.parse_args()
    path = ROOT/'xnu/patches/0098-fuxi-native-adreno-abi.patch'
    expected = content()
    if args.check:
        assert path.read_text() == expected, '0098 differs; run sync-fuxi-metal.py'
    else:
        path.write_text(expected)
    print('PASS: 0098 ABI sources match canonical files')


if __name__ == '__main__':
    main()
