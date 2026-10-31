#!/usr/bin/env python3
"""Generate public packer inputs, or restore an existing ADT locally on Linux.

CI deliberately has no Apple /product data, live FDT or private root extents.
The hardware recipe is the measured map already in mkadt-fuxi.py. A local ADT
can preserve the integration identity and extent mapping without publishing it.
"""
import argparse
import importlib.util
import os
from pathlib import Path
import sys

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import adt
import mkadt

spec = importlib.util.spec_from_file_location("fuxi_adt", TOOLS / "mkadt-fuxi.py")
fuxi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fuxi)
spec27 = importlib.util.spec_from_file_location("fuxi_adt27", TOOLS / "mkadt-fuxi27.py")
fuxi27 = importlib.util.module_from_spec(spec27)
spec27.loader.exec_module(fuxi27)

BASE = (
    "debug=0xc serial=3 cpus=7 keepsyms=1 -enable_kprintf_spam -noprogress "
    "rd=ufsroot0 fuxi_ufs_probe=1 ufsroot=userdata ufsrw=1 rootrw=1 "
    "unentitled_ios_sim_launch=1 ignition_level=1 wdt_test=no_user_checkin "
    "launchd_unsecure_cache=1 wdt=-1 sweetlog=0 sweetfb_noconsole=1 "
    "sweetnoexec=fairplayd.H2 fuxigpu=1 fuxiperf=1 "
    "sweetsample=backboardd,Preferences,PosterBoard "
    "sweetscan=PosterBoard,Preferences fuxigpuabi=1 "
    "sweetexecswap=bluetoothd:/usr/local/bin/fuxi-gpu-abi-test"
)
# iOS 27 kernel K1 (docs-lanes/ios27.md): banner and early log over the USB
# monitor, then a clean stop at root mount (no root device yet). Boot core only;
# the cpus7 pair (fuxi27-cpus7.adt) opts in to ArmCpu secondary start.
IOS27 = ("-v debug=0x14e serial=3 keepsyms=1 -enable_kprintf_spam cpus=1 "
         "kt_fixed_zones=120 -no_slto_panic use_contiguous_hint=0")
BOOT_LINE = {"ios27": 1024}  # boot_args.CommandLine; 608 for the xnu-8796 variants


def ios27(out):
    # The ADT has no secrets or device data; seed it per build (the loader passes it as is).
    adts = {"fuxi27.adt": fuxi27.build(cpus=1), "fuxi27-cpus7.adt": fuxi27.build(cpus=7)}
    for name, root in adts.items():
        blob = adt.serialize(root)
        if len(blob) > 0x4000:
            raise SystemExit(f"{name} exceeds the loader's 16 KiB ADT limit")
        (out / name).write_bytes(blob)
    return {"cmdline.txt": IOS27,
            "cmdline-cpus7.txt": IOS27.replace(" cpus=1 ", " cpus=7 ") + " sheng-smp=1"}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--variant", choices=("metal4", "wlan6g", "metal4e", "ios27"), required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--local-adt", type=Path,
                    help="LOCAL ONLY: preserve this integration ADT, including root extents")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    if args.variant == "ios27":
        if args.local_adt:
            ap.error("--local-adt carries an xnu-8796 ADT; ios27 uses mkadt-fuxi27.py")
        cmdlines = ios27(args.out)
    elif args.local_adt:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            ap.error("--local-adt is forbidden in GitHub Actions")
        root, end = adt.parse(args.local_adt.read_bytes())
        if end != args.local_adt.stat().st_size:
            ap.error("local ADT has trailing bytes")
        chosen = next(n for n in root.children if n.name == "chosen")
        if not chosen.prop("fuxi-root-extents") or not chosen.prop("fuxi-root-blocks"):
            ap.error("local ADT must contain the existing pinned root-file mapping")
        root = fuxi.reserve_gpu(root)
    else:
        # Never inspect optional research inputs, even on a developer's Mac.
        mkadt.d22ap_product = lambda: None
        root = fuxi.build(ufs_readonly=True, smp=True, gpu_reserve=True)
    if args.variant != "ios27":
        (args.out / "fuxi.adt").write_bytes(adt.serialize(root))
    if args.variant == "ios27":
        pass
    elif args.variant in ("metal4", "metal4e"):
        line = BASE.replace("sweetnoexec=fairplayd.H2", "sweetnoexec=bluetoothd,fairplayd.H2")
        line = line.replace("bluetoothd:/usr/local/bin/fuxi-gpu-abi-test",
                            "peakpowermanagerd:/usr/local/bin/fuxi-metal-smoke")
        line += " sweetnull=backboardd,SpringBoard,fuxi-vk-smoke,peakpowermanagerd,fuxi-metal-smoke"
        cmdlines = {"cmdline.txt": line}
        if args.variant == "metal4e":
            # arm64e: the same boot, plus a one-core fallback (a CPU whose PACGA differs parks; docs-lanes/arm64e.md).
            cmdlines["cmdline-cpus1.txt"] = line.replace(" cpus=7 ", " cpus=1 ")
    else:
        cmdlines = {"cmdline.txt": BASE + " fuxiwlan=1",
                    "cmdline-s22.txt": BASE + " fuxiwlan=22",
                    "cmdline-s22-oldiova.txt": BASE + " fuxiwlan=22 fuxiwlanoldiova=1"}
    for name, line in cmdlines.items():
        if len(line.encode()) >= BOOT_LINE.get(args.variant, 608):
            ap.error(f"{name} exceeds the payload command-line limit")
        (args.out / name).write_text(line + "\n")
    print(f"Generated {args.variant} ADT(s) and {', '.join(cmdlines)}")


if __name__ == "__main__":
    main()
