#!/usr/bin/env python3
"""Build the Fuxi ADT for the iOS 27 kernel (xnu-12377, UniXNU machine layer).

  mkadt-fuxi27.py OUT.adt [--cpus 1|7] [--psci-method smc|hvc] [--product iphone12,3|board] [--dump]

The xnu-8796 kernel matched its own SweetDTPlatform/SweetCPU; the 12377 kernel
uses UniXNU's ArmDTPlatform and ArmCpu (docs-lanes/ios27.md), which read a
different minimal tree. Its shape follows UniXNU-sheng scripts/make-sheng-adt.py
(BSD-2-Clause-Patent, same SM8550 SoC; see xnu27/NOTICE), with Fuxi's measured
values from mkadt-fuxi.py:

  /chosen    dram-base/size (upper bank, as the 8796 kernel), random-seed (256
             bytes: 12377 bootseed_init reads 4 x SHA-512 sizes), an empty CHRP
             NVRAM bank (ArmDTPlatform refuses to configure without one),
             osenvironment, crypto-hash-method, the dev-fused security tuple,
             kernel-ctrr-to-be-enabled = 0, amfi-only-platform-code = 1 (the
             original AppleImage4/AMFI read these at K2), memory-map (mkpayload
             adds RAMDisk), manifest-properties (empty; AppleImage4 reads it)
  /product   product-name/model: the original AppleImage4 panics without the
             node. Default --product iphone12,3 names the 24A446 userland's
             device (iPhone 11 Pro, iPhone12,3 / D421AP); --product board keeps
             the K1 Xiaomi strings
  /cpus      boot core 0 first, MPIDR Aff1 numbering; with --cpus 7 also cores
             2-7 (cpu@100 keeps running the loader's USB monitor), plus /psci
  /arm-io    ranges <0 0x17000000 0x08000000>, intc = GICv3 0x17100000/0x10000,
             0x17180000/0x200000, redistributor-stride 0x40000
  /defaults  sleep-disabled, vmm-present, kern.maxvnodes; /options (empty)
  /sweet-fbcon  the splash framebuffer ABL leaves scanned out (pe_serial FUXI)

No chip-id/unique-chip-id at K1: IOPlatformExpert hashes them with SHA-1,
which only a corecrypto kext provides on this kernel (docs-lanes/ios27.md).
"""
import argparse
import importlib.util
import os
from pathlib import Path
import struct
import sys
import zlib

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import adt  # noqa: E402
from mkadt import cstr, node, u32, u64  # noqa: E402

spec = importlib.util.spec_from_file_location('fuxi_adt', TOOLS / 'mkadt-fuxi.py')
fuxi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fuxi)

ARMIO_BASE, ARMIO_SIZE = 0x17000000, 0x08000000
GICR_STRIDE = 0x40000
TIMEBASE = 19200000
FB_W, FB_H = 1080, 2400
CPU_REGS = (0x000, 0x200, 0x300, 0x400, 0x500, 0x600, 0x700)
PRODUCTS = {'iphone12,3': ('iPhone 11 Pro', 'iPhone12,3'), 'board': ('Xiaomi 13', 'xiaomi,fuxi')}


def nvram_proxy():
    """One empty volatile CHRP bank (nvram/common/free partitions) for IODTNVRAM."""
    def header(name, size):
        raw = bytearray(struct.pack('<BBH12s', 0x70, 0, size // 16, name))
        total = sum(raw)
        while total > 255:
            total = (total & 255) + (total >> 8)
        raw[1] = total
        return raw
    image = bytearray(8192)
    image[:16] = header(b'nvram', 32)
    image[32:48] = header(b'common', 16 + 2048)
    free_at = 32 + 16 + 2048
    image[free_at:free_at + 16] = header(b'free', len(image) - free_at)
    struct.pack_into('<I', image, 16, zlib.adler32(bytes(image[20:])))
    return [('nvram-bank-size', u32(len(image))), ('nvram-bank-count', u32(1)),
            ('nvram-current-bank', u32(0)), ('nvram-proxy-data', bytes(image))]


def build(cpus=1, psci_method='smc', seed=None, mem_size=fuxi.MEM_SIZE, product='iphone12,3'):
    if cpus not in (1, len(CPU_REGS)):
        raise ValueError('cpus must be 1 or 7')
    seed = os.urandom(256) if seed is None else seed
    if len(seed) != 256:
        raise ValueError('random-seed must be 256 bytes')
    chosen = node('chosen', [
        ('dram-base', u64(fuxi.PHYS_BASE)),
        ('dram-size', u64(mem_size)),
        ('random-seed', seed),
        ('debug-enabled', u32(1)),
        ('firmware-version', cstr('fuxi-shim')),
        ('system-firmware-version', cstr('fuxi-shim')),
        ('osenvironment', cstr('main')),
        ('crypto-hash-method', cstr('sha2-384')),
        ('kernel-ctrr-to-be-enabled', u32(0)),
        ('certificate-production-status', u32(0)),
        ('effective-production-status-ap', u32(0)),
        ('certificate-security-mode', u32(1)),
        ('effective-security-mode-sep', u32(1)),
        ('amfi-only-platform-code', u32(1)),
    ] + nvram_proxy(), [node('memory-map'), node('manifest-properties')])
    cpu_nodes = [node(f'cpu{i}', [
        ('state', cstr('running' if i == 0 else 'waiting')),
        ('reg', u32(reg)),
        ('device_type', cstr('cpu')),
        ('compatible', cstr('qcom,sm8550-cpu') + cstr('ARM,v8')),
        ('timebase-frequency', u32(TIMEBASE)),
    ]) for i, reg in enumerate(CPU_REGS[:cpus])]
    children = [chosen, node('cpus', children=cpu_nodes)]
    if cpus > 1:
        children.append(node('psci', [('compatible', cstr('arm,psci-1.0')), ('method', cstr(psci_method))]))
    children += [
        node('defaults', [('sleep-disabled', u32(1)), ('vmm-present', u32(1)),
                          ('kern.maxvnodes', u32(78848))]),
        node('options'),
        node('product', [('product-name', cstr(PRODUCTS[product][0])), ('model', cstr(PRODUCTS[product][1]))]),
        node('arm-io', [
            ('device_type', cstr('arm-io')),
            ('#address-cells', u32(2)),
            ('#size-cells', u32(2)),
            ('ranges', u64(0, ARMIO_BASE, ARMIO_SIZE)),
        ], [node('intc', [
            ('compatible', cstr('arm,gic-v3')),
            ('reg', u64(fuxi.GICD - ARMIO_BASE, 0x10000, fuxi.GICR - ARMIO_BASE, fuxi.GICR_SIZE)),
            ('redistributor-stride', u64(GICR_STRIDE)),
        ])]),
        node('sweet-fbcon', [
            ('reg', u64(fuxi.FB, fuxi.FB_SIZE)),
            ('width', u32(FB_W)),
            ('height', u32(FB_H)),
            ('stride', u32(FB_W * 4)),
        ]),
    ]
    return node('device-tree', [
        ('model', cstr('fuxi-first-boot')),
        ('compatible', cstr('xiaomi,fuxi') + cstr('qcom,sm8550')),
        ('#address-cells', u32(2)),
        ('#size-cells', u32(2)),
    ], children)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('out', type=Path)
    ap.add_argument('--cpus', type=int, choices=(1, len(CPU_REGS)), default=1)
    ap.add_argument('--psci-method', choices=('smc', 'hvc'), default='smc')
    ap.add_argument('--product', choices=sorted(PRODUCTS), default='iphone12,3',
                    help='/product identity: the userland device (default) or the K1 board strings')
    ap.add_argument('--dump', action='store_true')
    args = ap.parse_args()
    blob = adt.serialize(build(args.cpus, args.psci_method, product=args.product))
    if len(blob) > 0x4000:
        ap.error(f'ADT is {len(blob)} bytes; the loader accepts at most 16 KiB')
    args.out.write_bytes(blob)
    if args.dump:
        adt.dump(adt.parse(blob)[0])
    print(f'{args.out}: {len(blob)} bytes, {args.cpus} cpu(s)', file=sys.stderr)


if __name__ == '__main__':
    main()
