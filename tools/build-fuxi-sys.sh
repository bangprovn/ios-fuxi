#!/bin/bash
# Build the P-SYS userland pieces on a Mac with Xcode (iPhoneOS SDK):
#   OUT/FuxiAudio.driver   AudioServerPlugIn bundle for mediaserverd's HAL
#   OUT/libfuxi-sb.dylib   SpringBoard (inserted by the kernel, fxinj bit 1): snapshot format, frame rates, no CoreTelephony
#                          reconnect loop without CommCenter
#   OUT/libfuxi-app.dylib  every application (inserted by the kernel, fxinj=1): CADisableMinimumFrameDurationOnPhone = YES
# Install them with tools/fuxi-sys-root.py (see docs-lanes/sys-port.md).
set -euo pipefail
cd "$(dirname "$0")/.."
[[ $(uname -s) == Darwin ]] || { echo 'requires a Mac with Xcode' >&2; exit 1; }
out=${1:-tools/out/sys}
sdk=$(xcrun --sdk iphoneos --show-sdk-path)
cc=$(xcrun --sdk iphoneos --find clang)
flags=(-target arm64-apple-ios16.0 -isysroot "$sdk" -O2 -Wall -Wextra -Werror -fstack-protector-strong)
bundle=$out/FuxiAudio.driver
rm -rf "$bundle"
mkdir -p "$bundle"
cp tools/fuxi-audio/Info.plist "$bundle/Info.plist"
"$cc" "${flags[@]}" -bundle tools/fuxi-audio/FuxiAudio.c -framework CoreFoundation -o "$bundle/FuxiAudio"
"$cc" "${flags[@]}" -dynamiclib tools/fuxi-sb/FuxiSnapshots.c tools/fuxi-sb/FuxiRates.c tools/fuxi-sb/FuxiTelephony.c -lobjc \
 -install_name /usr/local/lib/fuxi/libfuxi-sb.dylib -o "$out/libfuxi-sb.dylib"
"$cc" "${flags[@]}" -dynamiclib tools/fuxi-sb/FuxiAppRate.c -framework CoreFoundation \
 -install_name /usr/local/lib/fuxi/libfuxi-app.dylib -o "$out/libfuxi-app.dylib"
for binary in "$bundle" "$out/libfuxi-sb.dylib" "$out/libfuxi-app.dylib"; do
 codesign -f -s - "$binary"
 codesign --verify --strict "$binary"
done
shasum -a 256 "$bundle/FuxiAudio" "$bundle/Info.plist" "$out/libfuxi-sb.dylib" "$out/libfuxi-app.dylib" > "$out/SYS-SHA256SUMS"
cat "$out/SYS-SHA256SUMS"
