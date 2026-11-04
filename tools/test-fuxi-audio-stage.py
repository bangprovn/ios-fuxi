#!/usr/bin/env python3
"""shim/fuxi-probe/audio.c's firmware staging (OUT 0x5b), on the host: au_out_ok() and au_out_data() taken from the file as they are,
fed as tools/fuxi-audio-probe.py upload feeds them, in 512-byte chunks (every loader) and 4 KiB ones (fuxi-audio2 on). Both must stage
the same bytes with the same FNV-1a hash and close the image on its last chunk; anything out of order, oversized or after the end
is refused."""
import os
from pathlib import Path
import random
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]

HARNESS = r"""
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define AU_CAP0 0x00100000u
#define AU_CAP1 0x05000000u
static uint8_t *stage;
#define AU_STAGE ((uint64_t)(uintptr_t)stage)
enum { MON_LAUNCH = 0 };
static uint32_t monitor_report[4], au_boot[128];
static uint32_t au_len[2], au_hash[2], au_done[2];
static uint64_t au_emb[2];
char _head[1], _end[1];
static uint64_t probe_fdt_address(void) { return 1; }
static uint64_t probe_fdt_bytes(void) { return 1; }
#include "stage.c"
static uint32_t fnv(const uint8_t *p, size_t n) { uint32_t h = 2166136261u; for (size_t i = 0; i < n; i++) h = (h ^ p[i]) * 16777619u; return h; }
/* the host tool's loop: pad, then chunks numbered by their offset in 512-byte units */
static int upload(unsigned img, const uint8_t *data, size_t len, unsigned size) {
 for (size_t off = 0; off < len; ) {
  size_t n = len - off < size ? len - off : size;
  uint32_t c = (uint32_t)(off / 512);
  uint16_t index = (uint16_t)(img << 15 | (off == 0 ? 0x4000 : 0) | (c >> 16));
  if (!au_out_ok((uint16_t)c, index, (uint16_t)n)) return -1;
  au_out_data(index, data + off, (uint16_t)n);
  off += n;
 }
 return 0;
}
int main(void) {
 stage = calloc(1, AU_CAP0 + AU_CAP1);
 size_t sizes[] = { 16184, 39721400, 1 + 512 * 3, 4096 * 3 + 7 };
 for (unsigned k = 0; k < 4; k++) {
  size_t len = sizes[k];
  /* odd k: a source one byte off, so the byte path stages it; even k: the 8-byte path */
  uint8_t *raw = malloc(len + 2), *data = raw + (k & 1);
  srand(k + 1);
  for (size_t i = 0; i < len; i++) data[i] = (uint8_t)rand();
  if (len % 512 == 0) data[len++] = 0;
  unsigned img = len > AU_CAP0 ? 1 : 0;
  uint8_t *at = stage + (img ? AU_CAP0 : 0);
  for (unsigned size = 512; size <= 4096; size *= 8) {
   memset(at, 0xee, len);
   if (upload(img, data, len, size)) { printf("refused: %zu bytes in %u\n", len, size); return 1; }
   if (!au_done[img] || au_len[img] != len || au_hash[img] != fnv(data, len) || memcmp(at, data, len)) { printf("wrong: %zu in %u\n", len, size); return 1; }
   /* after the end, nothing more */
   if (au_out_ok((uint16_t)(len / 512), (uint16_t)(img << 15), 512)) { printf("taken after the end\n"); return 1; }
  }
  free(raw);
 }
 /* a restart drops the built-in image ('AL'): what follows is staged again */
 au_emb[1] = 0x1000;
 if (!au_out_ok(0, 0x8000 | 0x4000, 512) || au_emb[1] || au_len[1] || au_done[1]) { puts("restart kept the built-in image"); return 1; }
 /* refusals: too long, out of order, a ragged middle chunk, a restart that is not at 0 */
 uint8_t junk[8192] = { 0 };
 if (au_out_ok(0, 0x4000, 4097) || au_out_ok(0, 0x4000, 0)) { puts("size not refused"); return 1; }
 if (!au_out_ok(0, 0x4000, 1024)) { puts("restart refused"); return 1; }
 au_out_data(0x4000, junk, 1024);
 if (au_out_ok(1, 0, 512) || !au_out_ok(2, 0, 4096) || au_out_ok(0, 0x4000 | 0, 0) || au_out_ok(1, 0x4000, 512)) { puts("order not kept"); return 1; }
 au_out_data(0, junk, 4096);
 if (au_done[0] || au_len[0] != 5120) { puts("closed too soon"); return 1; }
 au_out_data(0, junk, 700);
 if (!au_done[0] || au_out_ok(11, 0, 512)) { puts("not closed by a ragged chunk"); return 1; }
 monitor_report[MON_LAUNCH] = 1;
 if (au_out_ok(0, 0x4000, 512)) { puts("taken after the launch"); return 1; }
 puts("staging ok: 512 and 4096-byte chunks, aligned and not, the same bytes and hash; refusals; a restart drops a built-in image");
 return 0;
}
"""


def main():
    text = (ROOT / 'shim/fuxi-probe/audio.c').read_text()
    staging = text[text.index('static uint32_t au_fnv('):text.index('static uint32_t au_status(')]
    assert '#define AU_CHUNK_MAX 4096u' in staging and 'length > AU_CHUNK_MAX' in staging and 'if (length & 511u) au_done[img] = 1;' in staging
    with tempfile.TemporaryDirectory(prefix='fuxi-stage-') as tmp:
        tmp = Path(tmp)
        barrier = '__asm__ volatile("dsb sy" ::: "memory");'
        assert staging.count(barrier) == 1 and staging.count('__asm__') == 1
        (tmp / 'stage.c').write_text(staging.replace(barrier, '__sync_synchronize();'))  # the host's own full barrier
        (tmp / 'run.c').write_text(HARNESS)
        subprocess.run([os.environ.get('CC', 'cc'), '-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined',
                        str(tmp / 'run.c'), '-o', str(tmp / 'run')], check=True)
        out = subprocess.run([str(tmp / 'run')], capture_output=True, text=True)
        assert out.returncode == 0, out.stdout + out.stderr
        print('PASS: ' + out.stdout.strip())
    # The host tool numbers chunks by offset in 512-byte units and falls back to 512 when an older loader refuses its first chunk.
    tool = (ROOT / 'tools/fuxi-audio-probe.py').read_text()
    assert "c = off // 512" in tool and "size = 512" in tool and "'--chunk'" in tool


main()
