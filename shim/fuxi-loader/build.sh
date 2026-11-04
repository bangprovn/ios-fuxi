#!/usr/bin/env bash
set -euo pipefail
payload=$(realpath "${1:?Usage: build.sh FUXI_PAYLOAD}")
cd "$(dirname "$0")"
: "${CLANG:?Set CLANG to an AArch64-capable clang}"
: "${LLD:?Set LLD to ld.lld}"
: "${OBJCOPY:?Set OBJCOPY to llvm-objcopy}"
: "${READELF:?Set READELF to llvm-readelf}"
: "${MKBOOTIMG:?Set MKBOOTIMG to AOSP mkbootimg.py}"
# GPU-bearing payloads must never silently produce a GPU-disabled image.
: "${FUXI_GMU_FIRMWARE:?Set the pinned GMU firmware absolute path}"
: "${FUXI_SQE_FIRMWARE:?Set the pinned sheng-firmware/a740_sqe.fw absolute path}"
: "${FUXI_GPU_ZAP_MBN:?Set the pinned ZAP MBN absolute path}"
for input in "$MKBOOTIMG" "$FUXI_GMU_FIRMWARE" "$FUXI_SQE_FIRMWARE" "$FUXI_GPU_ZAP_MBN"; do
 [[ $input == /* && -f $input ]] || { echo 'Packing inputs must be existing absolute file paths' >&2; exit 1; }
done
out=../out/fuxi-loader
mkdir -p "$out"
# Reject a payload made from a different board, truncated segments, or unsafe RAM.
python3 - "$payload" <<'PY'
import struct,sys
b=open(sys.argv[1],'rb').read()
assert len(b)>=680 and b[:8]==b'SWEETXNU'
# Native Mac builds embed their DEVELOPMENT_ARM64_FUXI object path; hosted CI
# (darwin-xnu-build) builds do not, so accept an xnu-8796 kernel with Fuxi drivers.
assert b'DEVELOPMENT_ARM64_FUXI' in b or (b'xnu-8796' in b and b'FUXI KEYS:' in b), 'Payload is not a FUXI kernel'
version,n=struct.unpack_from('<II',b,8)
assert version==1 and 0<n<=32 and len(b)>=680+32*n
assert struct.unpack_from('<Q',b,56)[0]==0x8c0000000
assert struct.unpack_from('<Q',b,64)[0] in (0x40000000,0x13e000000)
for i in range(n):
 va,vs,off,sz=struct.unpack_from('<4Q',b,680+32*i)
 assert off<=len(b) and sz<=len(b)-off and sz<=vs
PY
# Required pinned firmware input; only the ignored build directory receives bytes.
python3 - "$payload" "${FUXI_GMU_FIRMWARE:-}" "${FUXI_SQE_FIRMWARE:-}" "${FUXI_GPU_ZAP_MBN:-}" "$out/gpu-fw.S" <<'FWPY'
import hashlib, json, pathlib, struct, sys
payload, gmu, sqe, zap, dst = sys.argv[1:]
reserved = True  # Both supported windows exclude top-of-bank scratch.
pinned = {
 "gpu": "1a2a419c39046d3141fc5fed5aa7f971de2db40cc7a1d89693c3e26fad64dd98",
 "sqe": "6d38046654333a6a5a4ee77ab67abc24e3c0c5c68237513afe98e94da8fc9fcc",
 "zap": "0c2c2013835603ff0f93d9b21eba55e37d42a82189690ae0d579eac819842176",
}
lines = ['.section .gpu_fw,"a"']
for src, name in ((gmu, 'gpu'), (sqe, 'sqe'), (zap, 'zap')):
    lines += ['.balign 4', f'.global _{name}_fw_start', f'_{name}_fw_start:']
    if src:
        path = pathlib.Path(src).resolve(strict=True)
        size = path.stat().st_size
        assert path.is_file() and 4 < size <= 0x100000 and size % 4 == 0, 'invalid firmware size'
        if reserved:
            assert hashlib.sha256(path.read_bytes()).hexdigest() == pinned[name], 'M6 requires the device-verified firmware'
        if name == 'sqe':
            assert size - 4 <= 0x20000, 'SQE exceeds reserved buffer'
        if name == 'zap':
            assert size <= 0x10000, 'zap MBN exceeds parser bound'
        lines.append('.incbin ' + json.dumps(str(path)))
    lines += [f'.global _{name}_fw_end', f'_{name}_fw_end:']
pathlib.Path(dst).write_text('\n'.join(lines) + '\n')
FWPY
# Optional: the ADSP's squashed firmware inside the Image (FUXI_ADSP_DTB and FUXI_ADSP_FW, both or neither), after the payload.
# ABL loads it with the rest over fastboot (SuperSpeed), and the probe's 'AL' request hands it to stage 2 in place, instead of
# 39.7 MB over EP0 (about 42 s). Without them the section holds two zero CRCs and nothing else. Private firmware: only the
# ignored build directory receives bytes; the .S names the files.
python3 - "${FUXI_ADSP_DTB:-}" "${FUXI_ADSP_FW:-}" "$out/adsp-fw.S" <<'ADSPPY'
import json, pathlib, sys, zlib
dtb, fw, dst = sys.argv[1:]
if bool(dtb) != bool(fw):
    sys.exit('FUXI_ADSP_DTB and FUXI_ADSP_FW go together')
found = []
for src, cap in ((dtb, 0x100000), (fw, 0x5000000)):  # audio.c AU_CAP0, AU_CAP1
    if not src:
        continue
    path = pathlib.Path(src).resolve(strict=True)
    size, crc = path.stat().st_size, 0
    with path.open('rb') as f:
        head = f.read(52)
        assert head[:4] == b'\x7fELF' and head[4:6] == b'\x01\x01', f'{path}: not a squashed ELF32 LE MBN'
        crc = zlib.crc32(head)
        for block in iter(lambda: f.read(1 << 20), b''):
            crc = zlib.crc32(block, crc)
    assert 52 <= size <= cap, f'{path}: {size} bytes, over the stage cap {cap:#x}'
    found.append((path, size, crc))
lines = ['.section .adsp_fw,"a"', '.balign 8', '.global _adsp_crc', '_adsp_crc:',
         '.long ' + (', '.join(f'{crc:#x}' for _, _, crc in found) if found else '0, 0')]
for name, entry in zip(('dtb', 'fw'), found or (None, None)):
    if entry:
        lines.append('.balign 4096')
    lines += [f'.global _adsp_{name}_start', f'_adsp_{name}_start:']
    if entry:
        lines.append('.incbin ' + json.dumps(str(entry[0])))
    lines += [f'.global _adsp_{name}_end', f'_adsp_{name}_end:']
pathlib.Path(dst).write_text('\n'.join(lines) + '\n')
if found:
    print('Verified ADSP firmware built in: ' + ', '.join(f'{p.name} {size} bytes crc {crc:#010x}' for p, size, crc in found))
ADSPPY
# incbin avoids C absolute pointers in an Image loaded at an arbitrary PA.
printf '.section .payload,"a"\n.incbin "%s"\n' "$payload" > "$out/payload.S"
flags=(--target=aarch64-none-elf -O2 -ffreestanding -fno-builtin -nostdlib
       -fno-pic -fno-stack-protector -mgeneral-regs-only -mstrict-align -Wall -Wextra -Werror -DFUXI=1 -DFUXI_XNU_LOADER=1 -I..)
for source in start.S loader.c ../console.c ../fuxi-probe/probe.c ../../xnu/fuxi-audio/fuxi_audioreach.c ../../xnu/fuxi-audio/fuxi_gpr.c; do
 name=$(basename "${source%.*}")
 "$CLANG" "${flags[@]}" -c "$source" -o "$out/$name.o"
 if [[ $source == *.c ]] && "$READELF" -r "$out/$name.o" | grep -q R_AARCH64_ABS; then
  echo "Absolute C relocation in $source" >&2; exit 1
 fi
done
"$CLANG" "${flags[@]}" -c "$out/payload.S" -o "$out/payload.o"
"$CLANG" "${flags[@]}" -c "$out/gpu-fw.S" -o "$out/gpu-fw.o"
"$CLANG" "${flags[@]}" -c "$out/adsp-fw.S" -o "$out/adsp-fw.o"
"$LLD" -T loader.ld --build-id=none "$out/start.o" "$out/loader.o" "$out/console.o" "$out/probe.o" "$out/fuxi_audioreach.o" "$out/fuxi_gpr.o" "$out/gpu-fw.o" "$out/payload.o" "$out/adsp-fw.o" -o "$out/loader.elf"
python3 - "$out/loader.elf" "$READELF" <<'GPUCHECK'
import re, subprocess, sys
sections = subprocess.check_output([sys.argv[2], '-SW', sys.argv[1]], text=True)
match = re.search(r'\.gpu_fw\s+PROGBITS\s+[0-9a-fA-F]+\s+[0-9a-fA-F]+\s+([0-9a-fA-F]+)', sections)
assert match and int(match[1], 16) == 0x25ac8, 'loader .gpu_fw must be exactly 0x25ac8 bytes'
print('Verified loader .gpu_fw size 0x25ac8')
GPUCHECK
"$OBJCOPY" -O binary "$out/loader.elf" "$out/Image"
python3 - "$out/Image" <<'PY'
import struct,sys
from pathlib import Path
b=Path(sys.argv[1]).read_bytes()
assert struct.unpack_from('<Q',b,16)[0]==len(b), 'Image size must cover embedded payload'
assert b[56:60]==b'ARM\x64'
PY
python3 "$MKBOOTIMG" --header_version 4 --kernel "$out/Image" \
 --os_version 16.0.0 --os_patch_level 2026-08 -o "$out/fuxi-xnu-boot.img"
ls -lh "$out/Image" "$out/fuxi-xnu-boot.img"
