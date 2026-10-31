#!/usr/bin/env python3
"""Adapt the pinned blacktop builder without changing the local build scripts."""
import argparse
from pathlib import Path
import shutil

ap = argparse.ArgumentParser(description=__doc__)
ap.add_argument("builder", type=Path)
ap.add_argument("--ath12k", action="store_true",
                help="kernel-metal4wifi only: also apply patch 0117, which adds the ath12k domain to the kernel")
args = ap.parse_args()
root = Path(__file__).resolve().parents[2]
builder = args.builder
for source in sorted((root / "xnu/patches").iterdir()):
    if source.suffix not in (".sh", ".patch"):
        continue
    # Every other kernel keeps exactly its old inputs: no extra file, flag or link object.
    if source.name.startswith("0117-") and not args.ath12k:
        continue
    dest = builder / "patches" / source.name
    if source.suffix == ".sh":
        # Historical staging scripts run only for SWEET; FUXI uses the same
        # open-source pthread/HFS/BearSSL providers and SDK headers.
        text = source.read_text().replace('[[ "${MACHINE_CONFIG:-}" == SWEET ]]',
                                          '[[ "${MACHINE_CONFIG:-}" == FUXI ]]')
        dest.write_text(text)
    else:
        shutil.copyfile(source, dest)
        Path(str(dest) + ".fuxi-required").touch()

p = builder / "build.sh"
text = p.read_text()


def replace(old, new):
    global text
    if text.count(old) != 1:
        raise SystemExit("pinned builder changed: " + old)
    text = text.replace(old, new)


replace('ARCHS="x86_64 arm64e" VALID_ARCHS="x86_64 arm64e"',
        'ARCHS="x86_64 arm64e arm64" VALID_ARCHS="x86_64 arm64e arm64"')
replace('warning "Skipping patch ${PATCH}',
        'if [ -e "${PATCH}.fuxi-required" ]; then\n'
        '                    error "Required Fuxi patch failed: ${PATCH}: ${APPLY_CHECK}"\n'
        '                    exit 1\n'
        '                fi\n'
        '                warning "Skipping patch ${PATCH}')
p.write_text(text)
