/* Fuxi audio lane, milestone 1: read-only audio hardware snapshot and a gated
 * ADSP (LPASS Q6) PAS boot. Single translation unit #included by probe.c after
 * gpu.c, so it shares probe.c's helpers (usb_rd, delay, rh_send) and gpu.c's
 * probe_safe_rd32. Facts vs guesses and every address source: docs-lanes/audio.md.
 *
 * Requests (vendor, device):
 *  IN  0x5a 'AS' (0x4153) wIndex=group 0..4: read-only snapshot page.
 *  IN  0x5a 'AU' (0x4155): firmware staging status.
 *  IN  0x5a 'AB' (0x4142): stage 2, boot the ADSP from staged firmware (once).
 *  IN  0x5a 'AR' (0x4152): re-read the stage-2 report without running it.
 *  IN  0x5a 'AX' (0x4158) wIndex: stage 2 with options, and only then: bit 0 creates the AP's outbound SMP2P item 429
 *      with entry master-kernel before anything starts, bit 1 also rings LPASS's SMP2P doorbell, bits 15:8 are the
 *      seconds to wait for ready (0 is the usual 5, at most 45). 'AB' is what it always was.
 *  IN  0x5a 'AZ' (0x415a): PAS shutdown of the ADSP, after a stage 2 that reached ready. Nothing else.
 *  IN  0x5a 'AG' (0x4147) wIndex: once, after a stage 2 that reached ready: GLINK over SMEM, polled, for bits 7:0 seconds
 *      (at most 45), bit 8 also completes the opening of channels (au_glink.h). No data goes out. Returns its summary.
 *  IN  0x5a 'AH' (0x4148) wIndex=page: its log, 128 words a page.
 *  OUT 0x5b wValue=chunk[15:0], wIndex[11:0]=chunk[27:16], wIndex bit15 =
 *      image (0 adsp_dtb, 1 adsp), bit14 = restart that image at chunk 0.
 *      Sequential 512-byte chunks of a squashed (single-file) MBN; a short
 *      chunk ends the image.
 * Nothing runs unless the host asks; stage 2 refuses to run after a launch,
 * twice, or when the ADSP already reports ready. All new MMIO/RAM reads use
 * probe_safe_rd32 (synchronous aborts are contained; an SError still resets).
 */
#define AU_GCC      0x00100000ULL  /* gcc kalama-v2 @100000 */
#define AU_TLMM     0x0f100000ULL  /* TLMM tile (touch lane ts-03), 0x1000/GPIO */
#define AU_SE0      0x00880000ULL  /* qupv3_2 i2c@880000: CS35L41 x2 @0x40/0x42 */
#define AU_QUP2     0x008c0000ULL  /* qupv3_2_geni_se@8c0000 wrapper common */
#define AU_QMP      0x0c300000ULL  /* power-controller@c300000 kalama-aoss-qmp, 0x400 */
#define AU_IPCC     0x00408000ULL  /* qcom,ipcc@408000; AOSS mbox = client 0 signal 0 */
#define AU_SMEM     0x81d00000ULL  /* smem_region@81d00000 */
#define AU_SMEM_SZ  0x00200000u
#define AU_MUTEX    0x01f43000ULL  /* tcsr_mutex@1f40000, lock 3 is SMEM's, stride 0x1000 (Linux, sheng) */
#define AU_IPCC_SMP2P ((3u << 16) | 2u) /* client LPASS, signal SMP2P (sm8550.dtsi) */
#define AU_IPCC_GLINK ((3u << 16) | 0u) /* client LPASS, signal GLINK_QMP (sm8550.dtsi glink-edge) */
#define AU_DTB_PA   0x9e980000ULL  /* q6_adsp_dtb_region, 0x80000, no-map */
#define AU_DTB_SZ   0x00080000u
#define AU_FW_PA    0x9ea00000ULL  /* adspslpi_region, 0x4680000, no-map */
#define AU_FW_SZ    0x04680000u
/* Staging/metadata live low in the upper RAM bank the loader gives XNU later
 * (0x8c0000000..; GPU scratch G_MEM sits at the top, 0x9fe000000). Nothing
 * uses them before MON_LAUNCH and staged bytes are dead once stage 2 copied
 * them into the carveouts, which are outside XNU's bank. */
#define AU_STAGE    0x8d0000000ULL
#define AU_CAP0     0x00100000u    /* adsp_dtb squashed MBN cap */
#define AU_CAP1     0x05000000u    /* adsp squashed MBN cap */
#define AU_META     0x8cff00000ULL /* 2 x 64 KiB PAS metadata buffers */
#define AU_META_SZ  0x10000u
#define AU_PAS_ADSP 1u             /* Linux sm8550_adsp_resource.pas_id */
#define AU_PAS_DTB  0x24u          /* .dtb_pas_id */
#define AU_SMC(cmd) (0x42000200u | (cmd)) /* SMC64 SIP, svc PIL */
#define AU_LCX_ADDR 0x30030u       /* cmd-db lcx.lvl, 8 levels, hlvl 7 = 384 */
#define AU_LMX_ADDR 0x30040u       /* cmd-db lmx.lvl, 5 levels, hlvl 4 = 384 */

static uint32_t au_rep[128];   /* last snapshot group */
static uint32_t au_boot[128];  /* stage-2 report, frozen after the one run */
static uint32_t au_faults;
static uint32_t au_len[2], au_hash[2], au_done[2];

static uint32_t au_rd(uint64_t pa)
{
 uint32_t f, v = probe_safe_rd32(pa, &f);
 if (f) au_faults++;
 return v;
}
static void au_wr(uint64_t pa, uint32_t v)
{
 __asm__ volatile("dmb oshst" ::: "memory");
 *(volatile uint32_t *)(uintptr_t)pa = v;
 __asm__ volatile("dsb osh" ::: "memory");
}
static bool au_on(uint32_t cbcr) { return !(cbcr & 0x80000000u); }

/* ---- group 0: GCC QUPv3 wrapper-2 clocks + TLMM audio pins (gcc-kalama.c) ---- */
static const uint8_t au_gpio[] = {0, 1, 68, 98, 99, 121, 122, 123, 124, 182, 183};
static void au_group0(uint32_t *r)
{
 static const uint32_t g[] = {
  0x52010, /* APCS vote: b0 core b1 s_ahb b2 m_ahb b3 core2x b4 s0 */
  0x233cc, 0x233d0, 0x233d4, 0x23004, /* m_ahb, s_ahb, core, core2x CBCR */
  0x1e004, 0x1e010, 0x1e014, 0x1e024, /* s0 CBCR, RCG cmd, cfg, DFSR */
  0x1e02c, 0x1e030, 0x1e034, 0x1e038, 0x1e03c, 0x1e040, 0x1e044, 0x1e048};
 unsigned n = 4;
 for (unsigned i = 0; i < sizeof(g) / 4; i++) r[n++] = au_rd(AU_GCC + g[i]);
 n = 24;
 for (unsigned i = 0; i < sizeof(au_gpio); i++) {
  r[n++] = au_gpio[i];
  r[n++] = au_rd(AU_TLMM + 0x1000u * au_gpio[i]);     /* CTL */
  r[n++] = au_rd(AU_TLMM + 0x1000u * au_gpio[i] + 4); /* IO: b0 in, b1 out */
 }
}
/* ---- group 1: QUPv3 wrapper-2 SE0 (amp I2C). Only with m_ahb running: SE
 * registers hang the bus while the wrapper AHB is gated (touch lane ts-02). */
static void au_group1(uint32_t *r)
{
 static const uint16_t se[] = {0x040, 0x048, 0x064, 0x068, 0x07c, 0x258, 0x25c,
  0x268, 0x260, 0x264, 0x610, 0x614, 0x800, 0x804, 0xe24, 0xe28};
 r[4] = au_rd(AU_GCC + 0x233cc);
 if (!au_on(r[4])) { r[2] = 0x10; return; }
 for (unsigned i = 0; i < sizeof(se) / 2; i++) r[8 + i] = au_rd(AU_SE0 + se[i]);
 r[24] = au_rd(AU_QUP2 + 4); /* QUP_HW_VER */
 r[2] = 1;
}
/* ---- group 2: AOSS QMP descriptor (Linux qcom_aoss.c) ---- */
static void au_group2(uint32_t *r)
{
 for (unsigned i = 0; i < 15; i++) r[4 + i] = au_rd(AU_QMP + 4 * i);
 uint32_t off = r[4 + 14], size = r[4 + 13]; /* MCORE mbox offset / size */
 if (r[4] == 0x4d41494c && off >= 0x40 && off < 0x400 && size >= 8 && size <= 0x400 - off)
  for (unsigned i = 0; i < 4; i++) r[20 + i] = au_rd(AU_QMP + off + 4 * i);
}
/* ---- group 3: SMEM partitions + smp2p/glink items (Linux smem.c, smp2p.c) ---- */
struct au_part { uint64_t pa; uint32_t size, free_unc; };
static bool au_smem_part(unsigned h0, unsigned h1, struct au_part *p, uint32_t *nent)
{
 uint64_t pt = AU_SMEM + AU_SMEM_SZ - 0x1000;
 if (au_rd(pt) != 0x434f5424) return false; /* "$TOC" */
 uint32_t n = au_rd(pt + 8);
 if (nent) *nent = n;
 if (n > 64) return false;
 for (unsigned i = 0; i < n; i++) {
  uint64_t e = pt + 0x20 + 0x30 * i;
  uint32_t off = au_rd(e), size = au_rd(e + 4), hosts = au_rd(e + 12);
  unsigned a = hosts & 0xffff, b = hosts >> 16;
  if (!((a == h0 && b == h1) || (a == h1 && b == h0))) continue;
  if (!off || off >= AU_SMEM_SZ || size < 0x20 || size > AU_SMEM_SZ - off) return false;
  p->pa = AU_SMEM + off; p->size = size;
  if (au_rd(p->pa) != 0x54525024) return false; /* "$PRT" */
  p->free_unc = au_rd(p->pa + 12);
  return p->free_unc >= 0x20 && p->free_unc <= size;
 }
 return false;
}
/* Uncached private items only (smp2p and glink allocate uncached). */
static uint64_t au_smem_item(const struct au_part *p, unsigned item, uint32_t *size)
{
 uint64_t e = p->pa + 0x20, end = p->pa + p->free_unc;
 for (unsigned i = 0; i < 512 && e + 16 <= end; i++) {
  uint32_t w0 = au_rd(e), sz = au_rd(e + 4), w2 = au_rd(e + 8);
  if ((w0 & 0xffff) != 0xa5a5) return 0;
  uint64_t data = e + 16 + (w2 >> 16);
  if (sz > end - data) return 0;
  if ((w0 >> 16) == item) { if (size) *size = sz - (w2 & 0xffff); return data; }
  e = data + sz;
 }
 return 0;
}
/* smp2p item: magic, ver|features, pids, entries, flags, then 20-byte entries. */
static void au_smp2p(uint32_t *r, uint64_t it)
{
 for (unsigned i = 0; i < 5; i++) r[i] = au_rd(it + 4 * i);
 for (unsigned e = 0; e < 4; e++)
  for (unsigned w = 0; w < 5; w++) r[5 + e * 5 + w] = au_rd(it + 20 + 20 * e + 4 * w);
}
static uint32_t au_ready_bits(void)
{
 struct au_part p;
 if (!au_smem_part(0, 2, &p, 0)) return 0x80000000u;
 uint64_t in = au_smem_item(&p, 443, 0);
 if (!in) return 0x40000000u;
 if (au_rd(in) != 0x504d5324) return 0x20000000u;
 unsigned valid = au_rd(in + 12) >> 16;
 for (unsigned e = 0; e < valid && e < 16; e++) {
  uint64_t ent = in + 20 + 20 * e;
  /* "slave-kernel": fatal b0, ready b1, handover b2, stop-ack b3 (live DT). */
  if (au_rd(ent) == 0x76616c73 && au_rd(ent + 4) == 0x656b2d65)
   return au_rd(ent + 16) & 0xffff;
 }
 return 0x10000000u;
}
static void au_group3(uint32_t *r)
{
 struct au_part p; uint32_t n = 0;
 r[4] = au_rd(AU_SMEM + 0x40 + 7 * 4);   /* version[SBL] */
 r[5] = au_rd(AU_SMEM + 0x40 + 0x80);    /* initialized */
 r[6] = au_rd(AU_SMEM + AU_SMEM_SZ - 0x1000);
 r[7] = au_rd(AU_SMEM + AU_SMEM_SZ - 0x1000 + 4);
 if (!au_smem_part(0, 2, &p, &n)) { r[8] = n; r[2] = 0x30; return; }
 r[8] = n; r[9] = (uint32_t)p.pa; r[10] = p.size; r[11] = p.free_unc;
 static const uint16_t items[] = {443, 429, 478, 479, 480};
 for (unsigned i = 0; i < 5; i++) {
  uint32_t sz = 0; uint64_t pa = au_smem_item(&p, items[i], &sz);
  r[12 + 2 * i] = (uint32_t)pa; r[13 + 2 * i] = sz;
 }
 if (r[12]) au_smp2p(r + 32, r[12]); /* inbound (ADSP -> AP) */
 if (r[14]) au_smp2p(r + 64, r[14]); /* outbound (AP -> ADSP) */
 /* Global partition: crash reason item 423 (Linux crash_reason_smem). */
 struct au_part gp;
 if (au_smem_part(0xfffe, 0xfffe, &gp, 0)) {
  uint32_t sz = 0; uint64_t cr = au_smem_item(&gp, 423, &sz);
  r[22] = (uint32_t)gp.pa; r[23] = (uint32_t)cr; r[24] = sz;
  for (unsigned i = 0; cr && i < 8 && i * 4 < sz; i++) r[96 + i] = au_rd(cr + 4 * i);
 }
 r[25] = au_ready_bits();
 r[2] = 1;
}
/* ---- SCM (Linux qcom_scm-smc.c): x6 session is carried across INTERRUPTED. ---- */
static __attribute__((noinline)) void au_smc_raw(uint64_t ret[4], uint64_t *x6v, uint64_t fn,
                       uint64_t info, uint64_t a, uint64_t b, uint64_t c)
{
 register uint64_t x0 __asm__("x0") = fn, x1 __asm__("x1") = info;
 register uint64_t x2 __asm__("x2") = a, x3 __asm__("x3") = b;
 register uint64_t x4 __asm__("x4") = c, x5 __asm__("x5") = 0;
 register uint64_t x6 __asm__("x6") = *x6v, x7 __asm__("x7") = 0;
 __asm__ volatile("dsb sy\n\tsmc #0\n\tdsb sy"
  : "+r"(x0), "+r"(x1), "+r"(x2), "+r"(x3), "+r"(x4), "+r"(x5), "+r"(x6), "+r"(x7)
  :: "x8","x9","x10","x11","x12","x13","x14","x15","x16","x17","cc","memory");
 ret[0] = x0; ret[1] = x1; ret[2] = x2; ret[3] = x3; *x6v = x6;
}
/* Returns x0; *x1 receives the service result. WAITQ_SLEEP/EBUSY are failures. */
static uint64_t au_scm(uint32_t fn, unsigned info, uint64_t a, uint64_t b, uint64_t c, uint64_t *x1)
{
 uint64_t ret[4] = {0}, x6 = 0, f = fn;
 for (unsigned i = 0; i < 100000; i++) {
  au_smc_raw(ret, &x6, f, info, a, b, c);
  if (ret[0] != 1) break;       /* QCOM_SCM_INTERRUPTED: resume */
  f = 1;
 }
 if (x1) *x1 = ret[1];
 return ret[0];
}
static void au_group4(uint32_t *r)
{
 static const uint32_t avail[] = {0x02000201, 0x02000202, 0x02000205, 0x02000206, 0x02000207};
 uint64_t x1;
 for (unsigned i = 0; i < 5; i++) {
  r[4 + 2 * i] = (uint32_t)au_scm(0x42000601, 1, avail[i], 0, 0, &x1);
  r[5 + 2 * i] = (uint32_t)x1;
 }
 r[16] = (uint32_t)au_scm(AU_SMC(7), 1, AU_PAS_ADSP, 0, 0, &x1); r[17] = (uint32_t)x1;
 r[18] = (uint32_t)au_scm(AU_SMC(7), 1, AU_PAS_DTB, 0, 0, &x1); r[19] = (uint32_t)x1;
 r[2] = 1;
}
static void au_snapshot(unsigned group)
{
 for (unsigned i = 0; i < 128; i++) au_rep[i] = 0;
 au_rep[0] = 0xa0d10001; au_rep[1] = group;
 au_faults = 0;
 if (group == 0) { au_group0(au_rep); au_rep[2] = 1; }
 else if (group == 1) au_group1(au_rep);
 else if (group == 2) { au_group2(au_rep); au_rep[2] = 1; }
 else if (group == 3) au_group3(au_rep);
 else if (group == 4) au_group4(au_rep);
 else au_rep[2] = 0xff;
 au_rep[3] = au_faults;
}

/* ---- firmware staging over control OUT ---- */
static uint32_t au_fnv(uint32_t h, const uint8_t *p, unsigned n)
{
 for (unsigned i = 0; i < n; i++) h = (h ^ p[i]) * 16777619u;
 return h;
}
static bool au_out_ok(uint16_t value, uint16_t index, uint16_t length)
{
 unsigned img = index >> 15;
 uint32_t chunk = value | (uint32_t)(index & 0xfff) << 16;
 uint32_t cap = img ? AU_CAP1 : AU_CAP0;
 extern char _head[], _end[];
 uint64_t lo = AU_STAGE, hi = AU_STAGE + AU_CAP0 + AU_CAP1;
 if (monitor_report[MON_LAUNCH] || au_boot[0] || !length || length > 512 ||
     (index & 0x3000)) return false;
 /* The loader image and the FDT must not sit in the staging window. */
 if ((uintptr_t)_head < hi && lo < (uintptr_t)_end) return false;
 if (probe_fdt_address() < hi && lo < probe_fdt_address() + probe_fdt_bytes()) return false;
 if (index & 0x4000) { if (chunk) return false; au_len[img] = au_hash[img] = au_done[img] = 0; }
 if (au_done[img] || chunk != au_len[img] / 512 || length > cap - au_len[img]) return false;
 return true;
}
static void au_out_data(uint16_t index, const volatile uint8_t *src, uint16_t length)
{
 unsigned img = index >> 15;
 uint64_t dst = AU_STAGE + (img ? AU_CAP0 : 0) + au_len[img];
 uint8_t tmp[512];
 if (!au_len[img]) au_hash[img] = 2166136261u;
 for (unsigned i = 0; i < length; i++) tmp[i] = src[i];
 for (unsigned i = 0; i < length; i++) *(volatile uint8_t *)(uintptr_t)(dst + i) = tmp[i];
 au_hash[img] = au_fnv(au_hash[img], tmp, length);
 au_len[img] += length;
 if (length < 512) au_done[img] = 1;
 __asm__ volatile("dsb sy" ::: "memory");
}
static uint32_t au_status(uint32_t *r)
{
 r[0] = 0xa0d1f0f0; r[1] = au_len[0]; r[2] = au_hash[0]; r[3] = au_done[0];
 r[4] = au_len[1]; r[5] = au_hash[1]; r[6] = au_done[1];
 r[7] = (uint32_t)AU_STAGE; r[8] = (uint32_t)(AU_STAGE >> 32);
 r[9] = AU_CAP0; r[10] = AU_CAP1; r[11] = au_boot[0] ? au_boot[1] : 0;
 return 12 * 4;
}

/* ---- stage 2: ADSP PAS boot (Linux 6.12 qcom_q6v5_pas.c adsp_load/adsp_start,
 * qcom_mdt_loader.c, sm8550_adsp_resource) ---- */
static uint32_t au_le32(uint64_t pa) { return au_rd(pa); }
static uint32_t au_le16(uint64_t pa)
{
 uint32_t w = au_rd(pa & ~3ULL);
 return (pa & 2) ? w >> 16 : w & 0xffff;
}
struct au_plan {
 uint64_t img; uint32_t bytes, phoff, phnum, hdr, hash_off, hash_size, relocate, loads;
 uint64_t min, max;
};
/* Single-file ELF32 LE (squashed .mdt + .bNN). All bounds checked in 64 bits. */
static uint32_t au_plan_mbn(struct au_plan *p, uint64_t region_pa, uint32_t region_sz)
{
 uint64_t f = p->img;
 if (p->bytes < 52 || au_le32(f) != 0x464c457f || (au_le32(f + 4) & 0xffffff) != 0x010101 ||
     au_le16(f + 16) != 2 || au_le16(f + 40) != 52 || au_le16(f + 42) != 32) return 0x101;
 p->phoff = au_le32(f + 28); p->phnum = au_le16(f + 44);
 if (p->phnum < 2 || p->phnum > 128 || p->phoff < 52 ||
     (uint64_t)p->phoff + p->phnum * 32u > p->bytes) return 0x102;
 uint64_t ph0 = f + p->phoff;
 p->hdr = au_le32(ph0 + 16);
 if (au_le32(ph0) == 1 || p->hdr < p->phoff + p->phnum * 32u || p->hdr > p->bytes) return 0x103;
 p->hash_size = p->loads = p->relocate = 0; p->min = ~0ULL; p->max = 0;
 for (unsigned i = 1; i < p->phnum; i++) {
  uint64_t ph = ph0 + 32u * i;
  uint32_t type = au_le32(ph), off = au_le32(ph + 4), pa = au_le32(ph + 12);
  uint32_t file = au_le32(ph + 16), mem = au_le32(ph + 20), flags = au_le32(ph + 24);
  if (file && (uint64_t)off + file > p->bytes) return 0x104; /* squash incomplete */
  if ((flags & 0x07000000u) == 0x02000000u) {
   if (!p->hash_size) { p->hash_off = off; p->hash_size = file; }
   continue;
  }
  if (type != 1 || !mem) continue;
  if (file > mem) return 0x105;
  p->loads++; p->relocate |= !!(flags & (1u << 27));
  if (pa < p->min) p->min = pa;
  uint64_t end = ((uint64_t)pa + mem + 4095) & ~4095ULL;
  if (end > p->max) p->max = end;
 }
 if (!p->loads || !p->hash_size || (uint64_t)p->hdr + p->hash_size > AU_META_SZ) return 0x106;
 if (p->relocate) { if (p->max - p->min > region_sz) return 0x107; }
 else if (p->min < region_pa || p->max > region_pa + region_sz) return 0x107;
 return 0;
}
static void au_copy(uint64_t dst, uint64_t src, uint32_t n, uint32_t zero)
{
 uint32_t i = 0;
 if (!((dst | src) & 7))
  for (; i + 8 <= n; i += 8)
   *(volatile uint64_t *)(uintptr_t)(dst + i) = *(volatile uint64_t *)(uintptr_t)(src + i);
 for (; i < n; i++)
  *(volatile uint8_t *)(uintptr_t)(dst + i) = *(volatile uint8_t *)(uintptr_t)(src + i);
 for (i = n; i < n + zero && ((dst + i) & 7); i++) *(volatile uint8_t *)(uintptr_t)(dst + i) = 0;
 for (; i + 8 <= n + zero; i += 8) *(volatile uint64_t *)(uintptr_t)(dst + i) = 0;
 for (; i < n + zero; i++) *(volatile uint8_t *)(uintptr_t)(dst + i) = 0;
 __asm__ volatile("dsb sy" ::: "memory");
}
/* qcom_mdt_pas_init + qcom_mdt_load_no_init. d: 16 report words. */
static uint32_t au_pas_load(uint32_t *d, struct au_plan *p, unsigned pas, uint64_t region,
                            uint32_t region_sz, uint64_t meta)
{
 uint64_t x1, x0;
 uint32_t err = au_plan_mbn(p, region, region_sz);
 d[0] = p->bytes; d[1] = p->phnum; d[2] = p->hdr; d[3] = p->hash_size;
 d[4] = p->loads; d[5] = p->relocate; d[6] = (uint32_t)p->min; d[7] = (uint32_t)p->max;
 if (err) return err;
 au_copy(meta, 0, 0, AU_META_SZ);
 au_copy(meta, p->img, p->hdr, 0);
 au_copy(meta + p->hdr, p->img + p->hash_off, p->hash_size, 0);
 x0 = au_scm(AU_SMC(1), 0x82, pas, meta, 0, &x1); /* init_image(pas, RW meta) */
 d[8] = (uint32_t)x0; d[9] = (uint32_t)x1;
 if (x0 || x1) return 0x110;
 if (p->relocate) {
  x0 = au_scm(AU_SMC(2), 3, pas, region, p->max - p->min, &x1);
  d[10] = (uint32_t)x0; d[11] = (uint32_t)x1;
  if (x0 || x1) return 0x111;
 }
 uint64_t reloc = p->relocate ? p->min : region;
 for (unsigned i = 1; i < p->phnum; i++) {
  uint64_t ph = p->img + p->phoff + 32u * i;
  uint32_t off = au_le32(ph + 4), pa = au_le32(ph + 12), file = au_le32(ph + 16);
  uint32_t mem = au_le32(ph + 20), flags = au_le32(ph + 24);
  if (au_le32(ph) != 1 || !mem || (flags & 0x07000000u) == 0x02000000u) continue;
  uint64_t o = (uint64_t)pa - reloc;
  if (pa < reloc || o + mem > region_sz) return 0x112;
  au_copy(region + o, p->img + off, file, mem - file);
  d[12] += file; d[13] += mem - file;
 }
 return au_faults ? 0x113 : 0;
}
static uint32_t au_qmp_wait(uint32_t off, uint32_t want, bool eq)
{
 for (unsigned i = 0; i < 100000; i++) {
  uint32_t v = au_rd(AU_QMP + off);
  if (eq ? v == want : v != want) return 0;
  delay(10);
 }
 return 1;
}
/* qcom_aoss qmp_open (only if the link is not already up) + qmp_send. */
static uint32_t au_qmp_load_state(uint32_t *d)
{
 static const char msg[] = "{class: image, res: load_state, name: adsp, val: on}";
 if (au_rd(AU_QMP) != 0x4d41494c || au_rd(AU_QMP + 4) != 1) return 0x120;
 uint32_t off = au_rd(AU_QMP + 0x38), size = au_rd(AU_QMP + 0x34);
 d[0] = off; d[1] = size;
 if (off < 0x40 || off >= 0x400 || size < 68 || size > 0x400 - off) return 0x121;
 d[2] = au_rd(AU_QMP + 0x28); d[3] = au_rd(AU_QMP + 0x30); /* mcore link/ch acks */
 if (d[2] != 0xffff || d[3] != 0xffff) {
  au_wr(AU_QMP + 0x10, au_rd(AU_QMP + 0xc));     /* ack ucore link state */
  au_wr(AU_QMP + 0x24, 0xffff);                  /* mcore link up */
  au_wr(AU_IPCC + 0xc, 0);
  if (au_qmp_wait(0x28, 0xffff, true)) return 0x122;
  au_wr(AU_QMP + 0x2c, 0xffff);                  /* mcore channel up */
  au_wr(AU_IPCC + 0xc, 0);
  if (au_qmp_wait(0x14, 0xffff, true)) return 0x123;
  au_wr(AU_QMP + 0x18, 0xffff);                  /* ack ucore channel */
  au_wr(AU_IPCC + 0xc, 0);
  if (au_qmp_wait(0x30, 0xffff, true)) return 0x124;
  d[4] = 1;
 }
 if (au_rd(AU_QMP + off)) return 0x125;          /* previous message pending */
 uint32_t w[16] = {0};
 for (unsigned i = 0; i < sizeof(msg); i++) w[i / 4] |= (uint32_t)(uint8_t)msg[i] << (8 * (i % 4));
 for (unsigned i = 0; i < 16; i++) au_wr(AU_QMP + off + 4 + 4 * i, w[i]);
 au_wr(AU_QMP + off, 64);
 (void)au_rd(AU_QMP + off);
 au_wr(AU_IPCC + 0xc, 0);
 if (au_qmp_wait(off, 0, true)) return 0x126;
 d[5] = 1;
 return 0;
}
static uint32_t au_vote(uint32_t lcx, uint32_t lmx, uint32_t *d)
{
 uint32_t id = usb_rd(RH_DRV_PA, 0), cfg = usb_rd(RH_DRV_PA, 0xc);
 if (((id >> 16) & 0xff) != 3 || ((cfg >> 12) & 0x3f) < 3) return 0x130;
 d[0] = rh_send(0, AU_LCX_ADDR, lcx);
 if (d[0]) return 0x131;
 d[1] = rh_send(0, AU_LMX_ADDR, lmx);
 return d[1] ? 0x132 : 0;
}
/*
 * Options of 'AX'. Each processor makes its own outgoing item: Linux smp2p.c does at probe, before the DSP starts, and
 * sheng's AdspProbe.cpp does it with a comment that the DSP looks for it at boot. a-adsp showed the DSP's item 443 appear
 * with no slave-kernel entry, and 429 never existed. That this is what the DSP waits for is an inference; this finds out.
 */
static uint32_t au_opts, au_wait_s = 5;
static bool au_smem_lock(void)
{
 for (unsigned i = 0; i < 10000; i++) {
  au_wr(AU_MUTEX, 1);
  if (au_rd(AU_MUTEX) == 1) return true;
  delay(10);
 }
 return false;
}
/* Linux qcom_smem_alloc_private(): a zeroed uncached item at the free offset, then the offset moves. Under the lock. */
static uint64_t au_smem_new(const struct au_part *p, unsigned item, uint32_t size)
{
 uint32_t rounded = (size + 7) & ~7u, room = 16 + rounded;
 uint32_t free_unc = au_rd(p->pa + 12), free_cached = au_rd(p->pa + 16);
 if (free_unc > free_cached || free_cached - free_unc < room) return 0;
 uint64_t e = p->pa + free_unc, data = e + 16;
 for (uint32_t i = 0; i < rounded; i += 4) au_wr(data + i, 0);
 au_wr(e, 0xa5a5u | item << 16);
 au_wr(e + 4, rounded);
 au_wr(e + 8, rounded - size);    /* padding_data; padding_hdr 0 */
 au_wr(e + 12, 0);
 au_wr(p->pa + 12, free_unc + room);
 return data;
}
/* Linux qcom_smp2p_alloc_outbound_item(): the version last, once the rest is there. Then one entry, as sheng adds. */
static uint32_t au_outbound(uint32_t *d)
{
 struct au_part p;
 d[0] = au_opts;
 if (!au_smem_part(0, 2, &p, 0)) return 0x171;
 d[1] = au_rd(p.pa + 12);
 if (!au_smem_lock()) return 0x170;
 uint32_t sz = 0;
 uint64_t it = au_smem_item(&p, 429, &sz);
 d[2] = it ? 1 : 0;               /* it was there already: an earlier owner's */
 if (!it) it = au_smem_new(&p, 429, 20 + 16 * 20);
 au_wr(AU_MUTEX, 0);
 if (!it) return 0x172;
 d[3] = (uint32_t)it;
 d[4] = au_rd(p.pa + 12);
 if (d[2]) return 0;
 au_wr(it, 0x504d5324);           /* "$SMP" */
 au_wr(it + 8, 0u | 2u << 16);    /* local pid 0, remote pid 2 */
 au_wr(it + 12, 16u);             /* total entries; valid 0 */
 au_wr(it + 16, 0);               /* flags */
 au_wr(it + 4, 1u << 8);          /* features: SSR ack; version still 0 */
 au_wr(it + 4, 1u | 1u << 8);     /* version 1 */
 static const uint32_t name[4] = {0x7473616d, 0x6b2d7265, 0x656e7265, 0x0000006c}; /* "master-kernel" */
 for (unsigned i = 0; i < 4; i++) au_wr(it + 20 + 4 * i, name[i]);
 au_wr(it + 36, 0);
 au_wr(it + 12, 16u | 1u << 16);  /* valid 1 */
 if (au_opts & 2) { au_wr(AU_IPCC + 0xc, AU_IPCC_SMP2P); d[5] = 1; }
 return 0;
}
static uint32_t au_boot_run(uint32_t *r)
{
 uint64_t x0, x1;
 struct au_plan dtb = {.img = AU_STAGE, .bytes = au_len[0]};
 struct au_plan fw = {.img = AU_STAGE + AU_CAP0, .bytes = au_len[1]};
 au_faults = 0;
 r[3] = 1; /* step: prechecks */
 if (!au_done[0] || !au_done[1]) return 0x100;
 r[4] = au_ready_bits();
 if (!(r[4] & 0xf0000000u) && (r[4] & 2)) return 0x1ff; /* already running */
 if (au_faults) return 0x113;
 if (au_opts & 1) {
  r[3] = 11; /* the AP's outbound item, before anything starts */
  uint32_t out = au_outbound(r + 64);
  if (out) return out;
 }
 r[3] = 2; /* dtb init_image / mem_setup / load (adsp_load) */
 uint32_t err = au_pas_load(r + 16, &dtb, AU_PAS_DTB, AU_DTB_PA, AU_DTB_SZ, AU_META);
 if (err) return err;
 r[3] = 3; /* q6v5_prepare: AOSS load_state */
 err = au_qmp_load_state(r + 32);
 if (err) return err;
 r[3] = 4; /* proxy LCX/LMX votes at max (TURBO) */
 err = au_vote(7, 4, r + 40);
 if (err) return err;
 r[5] = 1;
 r[3] = 5; /* dtb auth_and_reset */
 x0 = au_scm(AU_SMC(5), 1, AU_PAS_DTB, 0, 0, &x1);
 r[6] = (uint32_t)x0; r[7] = (uint32_t)x1;
 if (x0 || x1) return 0x140;
 r[3] = 6; /* main image */
 err = au_pas_load(r + 48, &fw, AU_PAS_ADSP, AU_FW_PA, AU_FW_SZ, AU_META + AU_META_SZ);
 if (err) return err;
 r[3] = 7;
 x0 = au_scm(AU_SMC(5), 1, AU_PAS_ADSP, 0, 0, &x1);
 r[8] = (uint32_t)x0; r[9] = (uint32_t)x1;
 if (x0 || x1) return 0x150;
 r[3] = 8; /* qcom_q6v5_wait_for_start: 5 s */
 uint64_t t0 = rsr("cntpct_el0"), hz = rsr("cntfrq_el0");
 uint32_t bits = 0;
 bool rang = false;
 for (unsigned i = 0; i < 10000 * au_wait_s; i++) {
  bits = au_ready_bits();
  if (!(bits & 0xf0000000u) && (bits & 3)) break;
  if (rsr("cntpct_el0") - t0 > au_wait_s * hz) break;
  /* The DSP's item has appeared: Linux's interrupt handler would negotiate and kick here. */
  if ((au_opts & 2) && !rang && bits == 0x10000000u) { au_wr(AU_IPCC + 0xc, AU_IPCC_SMP2P); rang = true; r[70] = i; }
  delay(100);
 }
 r[10] = bits; r[11] = (uint32_t)((rsr("cntpct_el0") - t0) * 1000 / hz);
 if (au_opts) {           /* what the DSP had said, before a shutdown changes it */
  struct au_part p; uint64_t in;
  if (au_smem_part(0, 2, &p, 0) && (in = au_smem_item(&p, 443, 0)) != 0) au_smp2p(r + 72, in);
 }
 if ((bits & 0xf0000000u) || !(bits & 2) || (bits & 1)) {
  x0 = au_scm(AU_SMC(6), 1, AU_PAS_ADSP, 0, 0, &x1); /* Linux: shutdown on timeout */
  r[12] = (uint32_t)x0; r[13] = (uint32_t)x1;
  return (bits & 1) ? 0x161 : 0x160;
 }
 r[3] = 9; /* handover: drop proxy votes (Linux adsp_handover) */
 for (unsigned i = 0; i < 10000 && !(au_ready_bits() & 4); i++) delay(100);
 r[14] = au_ready_bits();
 if (!(r[14] & 0xf0000000u) && (r[14] & 4)) { err = au_vote(0, 0, r + 42); r[15] = err ? err : 1; }
 r[3] = 10;
 return 1;
}
static void au_boot_stage(void)
{
 if (au_boot[0]) return; /* one attempt per loader boot, including failures */
 au_boot[0] = 0xa0d10002;
 au_boot[1] = au_boot_run(au_boot);
 au_boot[2] = au_faults;
}
/* ---- GLINK over SMEM with the DSP that came up (au_glink.h) ---- */
#define AU_GL_RD(pa) au_rd(pa)
#define AU_GL_WR(pa, v) au_wr((pa), (v))
#define AU_GL_WMB() __asm__ volatile("dsb osh" ::: "memory")
#define AU_GL_KICK() au_wr(AU_IPCC + 0xc, AU_IPCC_GLINK)
#include "au_glink.h"
#define AU_GL_RECORDS 96u
#define AU_GL_LOG_WORDS (AU_GL_RECORDS * AU_GL_REC_WORDS)
static uint32_t au_gl_rep[128], au_gl_log[AU_GL_LOG_WORDS];
/* A cached private item, at the partition's far end (Linux qcom_smem_get_private()); the DSP may allocate its FIFO there. */
static uint64_t au_smem_item_cached(const struct au_part *p, unsigned item, uint32_t *size)
{
 uint64_t pt = AU_SMEM + AU_SMEM_SZ - 0x1000;
 uint32_t line = 0, n = au_rd(pt + 8);
 for (unsigned i = 0; i < n && i < 64; i++) {
  uint64_t e = pt + 0x20 + 0x30 * i;
  if (AU_SMEM + au_rd(e) == p->pa) { line = au_rd(e + 16); break; }
 }
 if (!line || line > 0x1000 || (line & (line - 1))) return 0;
 uint32_t step = (16 + line - 1) & ~(line - 1), free_cached = au_rd(p->pa + 16);
 if (free_cached > p->size || free_cached < p->free_unc || p->size < step) return 0;
 uint64_t e = p->pa + p->size - step, end = p->pa + free_cached;
 for (unsigned i = 0; i < 512 && e >= end; i++) {
  uint32_t w0 = au_rd(e), sz = au_rd(e + 4), w2 = au_rd(e + 8);
  if ((w0 & 0xffff) != 0xa5a5 || sz > e - end) return 0;
  if ((w0 >> 16) == item) { if (size) *size = sz - (w2 & 0xffff); return e - sz; }
  if (sz + step > e - end) return 0;
  e -= sz + step;
 }
 return 0;
}
static uint32_t au_glink_run(uint32_t *r, unsigned seconds, uint32_t options)
{
 static struct au_gl g;
 struct au_part p;
 uint32_t sz = 0, desc_sz = 0;
 au_faults = 0;
 if (!au_smem_part(0, 2, &p, 0)) return 0x201;
 uint64_t desc = au_smem_item(&p, 478, &desc_sz), tx = au_smem_item(&p, 479, &sz);
 r[8] = (uint32_t)desc; r[9] = desc_sz; r[10] = (uint32_t)tx; r[11] = sz;
 /* Items 478 and 479 are the AP's to make, as Linux does. On this phone they have been there before anything starts. */
 if (!desc || !tx) {
  if (!au_smem_lock()) return 0x202;
  if (!desc) { desc = au_smem_new(&p, 478, 32); desc_sz = 32; }
  if (!tx) { tx = au_smem_new(&p, 479, 16384); sz = 16384; }
  au_wr(AU_MUTEX, 0);
  r[12] = 1;
 }
 if (!desc || desc_sz != 32 || !tx || sz < 1024 || sz > 0x100000 || (sz & 7)) return 0x203;
 g.desc = desc; g.tx_fifo = tx; g.tx_len = sz; g.options = options;
 g.log = au_gl_log; g.log_most = AU_GL_RECORDS;
 uint64_t t0 = rsr("cntpct_el0"), hz = rsr("cntfrq_el0"), limit = seconds * hz;
 if (!au_gl_start(&g)) return 0x204;
 uint32_t bits = 0;
 for (uint64_t now = 0; now < limit; now = rsr("cntpct_el0") - t0) {
  g.ms = (uint32_t)(now * 1000 / hz);
  if (!g.rx_fifo) {
   uint32_t in = 0;
   uint64_t rx = 0;
   if (au_smem_part(0, 2, &p, 0)) {
    rx = au_smem_item(&p, 480, &in);
    if (!rx) { rx = au_smem_item_cached(&p, 480, &in); r[17] = rx != 0; }
   }
   if (rx && in >= 1024 && in <= 0x100000 && !(in & 7)) { g.rx_fifo = rx; g.rx_len = in; r[13] = (uint32_t)rx; r[14] = in; r[15] = g.ms; }
  }
  bits = au_ready_bits();
  if (!(bits & 0xf0000000u) && (bits & 1)) { r[16] = bits; break; }   /* the DSP says it has fallen over */
  if (!au_gl_step(&g)) delay(200);
  if (g.stuck) break;
 }
 r[18] = g.rx_msgs; r[19] = g.tx_msgs; r[20] = g.tx_full; r[21] = g.stuck; r[22] = g.version_seen; r[23] = g.version_acked;
 r[24] = g.features; r[25] = g.opened; r[26] = g.acked_by_dsp; r[27] = g.log_n; r[28] = g.log_lost; r[29] = g.ms;
 for (unsigned i = 0; i < 4; i++) r[30 + i] = au_rd(desc + 4 * i);
 r[34] = bits;
 struct au_part gp;
 if (au_smem_part(0xfffe, 0xfffe, &gp, 0)) {
  uint32_t csz = 0; uint64_t cr = au_smem_item(&gp, 423, &csz);
  r[35] = (uint32_t)cr; r[36] = csz;
  for (unsigned i = 0; cr && i < 32 && i * 4 < csz; i++) r[40 + i] = au_rd(cr + 4 * i);
 }
 return g.stuck ? 0x210 + g.stuck : 1;
}
/* USB entry: returns bytes placed in out (0 = stall). */
static uint32_t audio_request(uint16_t value, uint16_t index, uint8_t *out)
{
 if (value == 0x4153 && index <= 4 && !monitor_report[MON_LAUNCH]) {
  au_snapshot(index);
  memcpy(out, au_rep, sizeof(au_rep)); return sizeof(au_rep);
 }
 if (value == 0x4155) { uint32_t r[12]; uint32_t n = au_status(r); memcpy(out, r, n); return n; }
 if (value == 0x4142 && !monitor_report[MON_LAUNCH]) {
  au_boot_stage();
  memcpy(out, au_boot, sizeof(au_boot)); return sizeof(au_boot);
 }
 if (value == 0x4152 && au_boot[0]) { memcpy(out, au_boot, sizeof(au_boot)); return sizeof(au_boot); }
 if (value == 0x4158 && !monitor_report[MON_LAUNCH]) {
  unsigned wait = index >> 8;
  au_opts = index & 3;
  au_wait_s = wait == 0 ? 5 : wait > 45 ? 45 : wait;
  au_boot_stage();
  memcpy(out, au_boot, sizeof(au_boot)); return sizeof(au_boot);
 }
 if (value == 0x415a && !monitor_report[MON_LAUNCH] && au_boot[1] == 1) {
  uint64_t x1, x0 = au_scm(AU_SMC(6), 1, AU_PAS_ADSP, 0, 0, &x1);
  uint32_t r[2] = {(uint32_t)x0, (uint32_t)x1};
  memcpy(out, r, sizeof(r)); return sizeof(r);
 }
 if (value == 0x4147 && !monitor_report[MON_LAUNCH] && au_boot[1] == 1) {
  if (!au_gl_rep[0]) {
   unsigned seconds = index & 0xff;
   au_gl_rep[0] = 0xa0d10003;
   au_gl_rep[2] = seconds = seconds == 0 ? 10 : seconds > 45 ? 45 : seconds;
   au_gl_rep[3] = (index >> 8) & 1;
   au_gl_rep[1] = au_glink_run(au_gl_rep, seconds, au_gl_rep[3] ? AU_GL_ANSWER : 0);
   au_gl_rep[4] = au_faults;
  }
  memcpy(out, au_gl_rep, sizeof(au_gl_rep)); return sizeof(au_gl_rep);
 }
 if (value == 0x4148 && au_gl_rep[0] && index < AU_GL_LOG_WORDS / 128) {
  memcpy(out, au_gl_log + 128u * index, 512); return 512;
 }
 return 0;
}
