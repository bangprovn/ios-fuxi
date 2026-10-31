#!/usr/bin/env bash
# Usage: apply.sh XNU_DIR — apply xnu27/series to a clean xnu-12377.121.6 tree and
# stage the UniXNU components. Fails on the first patch that does not apply.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
xnu=$(cd "${1:?Usage: apply.sh XNU_DIR}" && pwd)
n=0
while read -r p; do
 [[ -z $p || $p == \#* ]] && continue
 git -C "$xnu" apply --whitespace=nowarn "$here/patches/$p" || { echo "xnu27: $p does not apply" >&2; exit 1; }
 n=$((n+1))
done < "$here/series"
for c in ArmCpu ArmDTPlatform; do
 cp "$here/components/$c/$c.cpp" "$xnu/iokit/Kernel/arm/$c.cpp"
done
echo "xnu27: applied $n patches, staged ArmCpu ArmDTPlatform"
