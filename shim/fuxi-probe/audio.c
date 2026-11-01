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
 *  IN  0x5a 'AM' (0x414d) wIndex: read only, what apps_smmu has for stream wIndex: its global state and each stream match that takes it.
 *  IN  0x5a 'AG' (0x4147) wIndex: once, after a stage 2 that reached ready: GLINK over SMEM, polled, for bits 7:0 seconds
 *      (at most 45), bit 8 also completes the opening of channels (au_glink.h), bit 9 with it also asks the APM whether it is
 *      up: APM_CMD_GET_SPF_STATE on adsp_apps, the only data that goes out, every 3 s until it says ready; bit 10 with both then
 *      also votes LPASS's macro and dcodec on and off again, a step each 1.5 s; bit 11 with all three, between the votes, opens the
 *      secondary TDM endpoint's graph, configures it and closes it, never starting it; bit 12 with all four, instead, opens that graph and a
 *      playback stream into it, starts them, gives them a second of zeros from AU_PLAY_PA, and takes it all down. No pin is muxed. Returns its summary.
 *  IN  0x5a 'AT' (0x4154) wIndex: a-play1 reset the phone with no report, so the parts of --play that the AP does, alone. Bit 0 the zeros, read back;
 *      bit 1 the bypass stream match, read back; bit 2 takes it away. Returns what it read.
 *  IN  0x5a 'AA' (0x4141) wIndex: reads the amplifiers named, bit 0 B at 0x40, bit 1 T at 0x42, one out of reset at a time (au_amp.h): who they are and
 *      their OTP boot. Writes nothing to them. Their reset lines and the I2C pins go back as they were. Returns the report.
 *  IN  0x5a 'AO' (0x414f) wIndex 0x4231 or, with the amplifier's own monitors coming back, 0x4232, once, after a stage 2 that reached ready, because the owner said so: the
 *      first tone (au_tone_stage()). Returns the summary as 'AG' does. 'AJ' (0x414a): what it said of itself. 'AV' (0x4156) wIndex: page of 512 bytes of what came back.
 *  IN  0x5a 'AC' (0x4143) wIndex 0x4d31, once, after a stage 2 that reached ready: the mic as far as the codec answering, with its bias and ADC1 on and read back, between the votes
 *      and the TX core clock, all let go at the end (au_mic.h). No capture graph, no amplifier, no apps_smmu. Returns the summary as 'AG' does. 'AE' (0x4145): its report.
 *  IN  0x5a 'AH' (0x4148) wIndex: page of the log. 'AI' (0x4149): what the last list's steps answered.
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
#include "../../xnu/fuxi-audio/fuxi_audioreach.h"
/* What --graph and --play send, in order. Values of the Xiaomi 14's port, which shares these pins: unproven here, and nothing depends on them yet with no pin muxed. */
static FuxiArFormat au_format = {48000, 32, 2};
static FuxiArTdm au_tdm = {.intf = 1, .slot_mask = 5, .slots = 4, .slot_width = 32, .sync_mode = 1, .invert_sync = 0, .sync_data_delay = 1, .lane = 1, .clock_attribute = 4};
#define AU_GRAPH_STEPS 3u
static uint32_t au_graph_build(uint32_t step, uint32_t *m, uint32_t capacity)
{
 uint8_t *out = (uint8_t *)(m + 2);
 if (step == 0) return fuxi_ar_open_speaker_tdm(out, capacity, 1, AU_GL_GRAPH_TOKEN);
 if (step == 1) return fuxi_ar_config_speaker_tdm(out, capacity, 1, AU_GL_GRAPH_TOKEN + 1, &au_format, &au_tdm);
 return fuxi_ar_graph_command(out, capacity, 1, AU_GL_GRAPH_TOKEN + 2, FUXI_AR_OP_GRAPH_CLOSE, FUXI_AR_SG_SPEAKER);
}
/* The zeros: AP memory the DSP reads. Below 4 GiB, as the word above it is the stream's. Checked against the phone's reserved-memory: research_notes/audio-next.md. */
#define AU_PLAY_PA 0xd0000000ull
#define AU_PLAY_SID 1ull            /* 0x1001 & 0xf, as Linux q6apm_dai */
#define AU_PLAY_BYTES 7680u         /* 20 ms */
static uint32_t au_period_bytes = AU_PLAY_BYTES;
static uint32_t au_map_bytes = AU_PLAY_BYTES * 4;
#define AU_PLAY_BUFFERS 50u
#define AU_PLAY_STEPS 15u
#define AU_PLAY_WRITES_AFTER 10u    /* the buffers come when the first ten steps are done */
static uint32_t au_play_build(uint32_t step, uint32_t t, uint32_t *m, uint32_t capacity, uint32_t handle)
{
 uint8_t *out = (uint8_t *)(m + 2);
 switch (step) {
 case 0: return fuxi_ar_open_speaker_tdm(out, capacity, 1, t);
 case 1: return fuxi_ar_config_speaker_tdm(out, capacity, 1, t, &au_format, &au_tdm);
 case 2: return fuxi_ar_open_playback_stream_tdm(out, capacity, 1, t);
 case 3: return fuxi_ar_config_playback_stream(out, capacity, 1, t, &au_format);
 case 4: return fuxi_ar_config_playback_shmem(out, capacity, 1, t, &au_format);
 case 5: return fuxi_ar_map(out, capacity, 1, t, AU_PLAY_PA | AU_PLAY_SID << 32, au_map_bytes);
 case 6: return fuxi_ar_graph_command(out, capacity, 1, t, FUXI_AR_OP_GRAPH_PREPARE, FUXI_AR_SG_SPEAKER);
 case 7: return fuxi_ar_graph_command(out, capacity, 1, t, FUXI_AR_OP_GRAPH_PREPARE, FUXI_AR_SG_PLAY);
 case 8: return fuxi_ar_graph_command(out, capacity, 1, t, FUXI_AR_OP_GRAPH_START, FUXI_AR_SG_SPEAKER);
 case 9: return fuxi_ar_graph_command(out, capacity, 1, t, FUXI_AR_OP_GRAPH_START, FUXI_AR_SG_PLAY);
 case 10: return fuxi_ar_graph_command(out, capacity, 1, t, FUXI_AR_OP_GRAPH_STOP, FUXI_AR_SG_PLAY);
 case 11: return fuxi_ar_graph_command(out, capacity, 1, t, FUXI_AR_OP_GRAPH_STOP, FUXI_AR_SG_SPEAKER);
 case 12: return fuxi_ar_graph_command(out, capacity, 1, t, FUXI_AR_OP_GRAPH_CLOSE, FUXI_AR_SG_PLAY);
 case 13: return fuxi_ar_graph_command(out, capacity, 1, t, FUXI_AR_OP_GRAPH_CLOSE, FUXI_AR_SG_SPEAKER);
 default: return fuxi_ar_unmap(out, capacity, 1, t, handle);
 }
}
/*
 * The tone's list is that one inside the secondary TDM bit clock, which Linux's card asks of the PRM before it opens anything (clk_set_rate, then enable): 6.144 MHz,
 * inverted, as the Xiaomi 14's port. Four channels on all four slots, for the slots' mapping is the weakest part of the research and this way any of them carries the tone.
 */
#define AU_TONE_STEPS 17u
#define AU_TONE_WRITES_AFTER 11u
#define AU_TONE_VI_STEPS 29u
#define AU_TONE_VI_WRITES_AFTER 19u
#define AU_TONE_CLOCK 0x202u
#define AU_VI_PA (AU_PLAY_PA + 0x10000u)    /* after the four periods played: what comes back, one period after another, never over itself */
#define AU_VI_PERIODS 300u
static bool au_tone_vi;
static const FuxiArTdm au_vi_tdm = {.intf = 1, .slot_mask = 0xf, .slots = 4, .slot_width = 32, .sync_mode = 1, .invert_sync = 0, .sync_data_delay = 1, .lane = 0, .clock_attribute = 4};  /* gpio122 */
static uint32_t au_tone_build(uint32_t step, uint32_t *m, uint32_t capacity, uint32_t handle)
{
 uint8_t *out = (uint8_t *)(m + 2);
 uint32_t t = AU_GL_GRAPH_TOKEN + step;
 uint32_t steps = au_tone_vi ? AU_TONE_VI_STEPS : AU_TONE_STEPS;
 if (step == 0) return fuxi_ar_prm_clock_attr(out, capacity, 2, t, AU_TONE_CLOCK, 6144000, 4);
 if (step == steps - 1) return fuxi_ar_prm_clock_attr(out, capacity, 2, t, AU_TONE_CLOCK, 0, 4);
 if (!au_tone_vi) return au_play_build(step - 1, t, m, capacity, handle);
 /* The way back opens after the way out, and closes before it. */
 if (step <= 10) return au_play_build(step - 1, t, m, capacity, handle);
 if (step >= 23) return au_play_build(step - 13, t, m, capacity, handle);
 switch (step) {
 case 11: return fuxi_ar_open_monitor_tdm(out, capacity, 1, t);
 case 12: return fuxi_ar_config_monitor_tdm(out, capacity, 1, t, &au_format, &au_vi_tdm);
 case 13: return fuxi_ar_open_capture_stream_tdm(out, capacity, 1, t);
 case 14: return fuxi_ar_config_capture_stream(out, capacity, 1, t, &au_format);
 case 15: return fuxi_ar_graph_command(out, capacity, 1, t, FUXI_AR_OP_GRAPH_PREPARE, FUXI_AR_SG_MIC);
 case 16: return fuxi_ar_graph_command(out, capacity, 1, t, FUXI_AR_OP_GRAPH_PREPARE, FUXI_AR_SG_CAPTURE);
 case 17: return fuxi_ar_graph_command(out, capacity, 1, t, FUXI_AR_OP_GRAPH_START, FUXI_AR_SG_MIC);
 case 18: return fuxi_ar_graph_command(out, capacity, 1, t, FUXI_AR_OP_GRAPH_START, FUXI_AR_SG_CAPTURE);
 case 19: return fuxi_ar_graph_command(out, capacity, 1, t, FUXI_AR_OP_GRAPH_STOP, FUXI_AR_SG_CAPTURE);
 case 20: return fuxi_ar_graph_command(out, capacity, 1, t, FUXI_AR_OP_GRAPH_STOP, FUXI_AR_SG_MIC);
 case 21: return fuxi_ar_graph_command(out, capacity, 1, t, FUXI_AR_OP_GRAPH_CLOSE, FUXI_AR_SG_CAPTURE);
 default: return fuxi_ar_graph_command(out, capacity, 1, t, FUXI_AR_OP_GRAPH_CLOSE, FUXI_AR_SG_MIC);
 }
}
static uint32_t au_tone_stage(struct au_gl *g, uint32_t *q, uint64_t t0, uint64_t hz);
/* ---- the mic, to proof step 7 (au_mic.h). Everything of LPASS by the safe accessors: a raw write the hypervisor refused reset this phone three times. ---- */
static uint32_t wl_cmddb_addr(const char *name);
/* The RSC checked as au_vote() does, then TCS 0 as every lane's rh_send() here. */
static uint32_t au_mic_rpmh(uint32_t addr, uint32_t data)
{
 uint32_t id = usb_rd(RH_DRV_PA, 0), cfg = usb_rd(RH_DRV_PA, 0xc);
 if (((id >> 16) & 0xff) != 3 || ((cfg >> 12) & 0x3f) < 3) return 0x130;
 return rh_send(0, addr, data);
}
#define AU_MIC_RD(pa, f) probe_safe_rd32((pa), (f))
#define AU_MIC_WR(pa, v) probe_safe_wr32((pa), (v))
#define AU_MIC_DELAY_US(us) delay(us)
#define AU_MIC_CMDDB(name) wl_cmddb_addr(name)
#define AU_MIC_RPMH(addr, data) au_mic_rpmh((addr), (data))
#include "au_mic.h"
static struct au_mic au_mic_state;
static uint32_t au_mic_rep[128];
#define AU_GL_RECORDS 96u
#define AU_GL_LOG_WORDS (AU_GL_RECORDS * AU_GL_REC_WORDS)
static uint32_t au_gl_rep[128], au_gl_rep2[128], au_gl_rep3[128], au_gl_log[AU_GL_LOG_WORDS];
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
/*
 * a-graph2: apps_smmu has no match for the DSP's streams and faults what it does not match, so the DSP cannot read AP memory as things stand.
 * The stream as Linux sm8550.dtsi's q6apm-dais has it: 0x1001 mask 0x80, and 0x1061. a-poke5: the hypervisor aborts a match for 0x1801, an older SoC's.
 * For --play alone, one free stream match into context bank 4, put back when the run ends. That is what the touch lane found (ts_smmu() in
 * probe.c): the hypervisor does not take a bypass, and a-poke2 and 3 reset the phone asking for one; bank 4 is DWC3's, with stage 1 off, so
 * the address the DSP says is the physical one. Order as Linux arm_smmu_write_sme(): the S2CR before the SMR makes it valid.
 */
#define AU_SMMU 0x15000000ull
static int au_smmu_slot = -1;
static uint32_t au_smmu_old[2];
static uint32_t au_smmu_step;
unsigned probe_safe_wr32(uint64_t pa, uint32_t value);
static void au_smmu_bypass(uint32_t sid, uint32_t mask, bool only_s2cr)
{
 uint32_t count = au_rd(AU_SMMU + 0x20) & 0xff;
 for (int i = 0; i < (int)count; i++) {
  uint32_t smr = au_rd(AU_SMMU + 0x800 + 4 * (uint32_t)i);
  if (au_faults || (smr >> 31)) continue;
  au_smmu_old[0] = smr; au_smmu_old[1] = au_rd(AU_SMMU + 0xc00 + 4 * (uint32_t)i);
  if (au_smmu_old[1] != 2u << 16) continue;        /* only one set to fault, as nobody's */
  au_smmu_slot = i;
  au_smmu_step = 1;
  if (probe_safe_wr32(AU_SMMU + 0xc00 + 4 * (uint32_t)i, 4)) { au_smmu_slot = -1; return; }
  au_smmu_step = 2;
  if (only_s2cr) return;
  if (probe_safe_wr32(AU_SMMU + 0x800 + 4 * (uint32_t)i, 1u << 31 | mask << 16 | sid)) { au_smmu_slot = -1; return; }
  au_smmu_step = 3;
  return;
 }
}
static void au_smmu_back(void)
{
 if (au_smmu_slot < 0) return;
 (void)probe_safe_wr32(AU_SMMU + 0x800 + 4 * (uint32_t)au_smmu_slot, au_smmu_old[0]);
 (void)probe_safe_wr32(AU_SMMU + 0xc00 + 4 * (uint32_t)au_smmu_slot, au_smmu_old[1]);
 au_smmu_slot = -1;
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
 uint32_t bits = 0, next_ask = 2000, graph_until = 0, writes_until = 0, second_old[2] = {0, 0};
 int second_slot = -1;
 bool tone_done = false;
 g.graph_steps = (options & AU_GL_MIC) != 0 ? AU_MIC_STEPS : (options & AU_GL_TONE) != 0 ? (au_tone_vi ? AU_TONE_VI_STEPS : AU_TONE_STEPS) : (options & AU_GL_PLAY) != 0 ? AU_PLAY_STEPS : AU_GRAPH_STEPS;
 if ((options & AU_GL_TONE) != 0) {
  au_format.channels = 4;
  au_tdm.slot_mask = 0xf;
  au_period_bytes = 960u * 4u * 4u;
  au_map_bytes = 0x10000u + AU_VI_PERIODS * au_period_bytes;
 }
 if ((options & AU_GL_PLAY) != 0) {
  /* Not over the code that is running here. */
  uint64_t here = (uint64_t)(uintptr_t)&au_gl_rep2[0], stack = (uint64_t)(uintptr_t)&bits;
  if ((here >= AU_PLAY_PA - 0x200000 && here < AU_PLAY_PA + 0x200000) || (stack >= AU_PLAY_PA - 0x200000 && stack < AU_PLAY_PA + 0x200000)) return 0x205;
  /* Zeros, whatever was there. Device-like writes, and the data barrier of au_wr(): the DSP does not snoop anything here. */
  for (uint32_t i = 0; i < au_period_bytes; i++) au_wr(AU_PLAY_PA + 4 * i, 0);
  /* a-tone3 read back what looked like untouched DRAM: a pattern over the way back's buffers says whether the DSP writes there at all. */
  if (au_tone_vi) for (uint32_t i = 0; i < AU_VI_PERIODS * au_period_bytes / 4; i++) au_wr(AU_VI_PA + 4 * i, 0x5a5a5a5au);
  au_gl_rep2[40] = au_rd(AU_SMMU + 0x48);
  au_smmu_bypass(0x1061, 0, false);
  if (au_smmu_slot < 0) return 0x206;
  second_slot = au_smmu_slot; second_old[0] = au_smmu_old[0]; second_old[1] = au_smmu_old[1];
  au_smmu_bypass(0x1001, 0x80, false);
  au_gl_rep2[41] = (uint32_t)(au_smmu_slot + 1);
  if (au_smmu_slot < 0) return 0x206;
 }
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
  if (g.ms >= next_ask) {
   if (!g.gpr_ready) { if (au_gl_gpr_ask(&g)) next_ask = g.ms + 3000; }
   else if ((options & AU_GL_GRAPH) != 0 && g.prm_sent == 2 && g.graph_sent < g.graph_steps && g.ms < graph_until) {
    /* Each packet into the long buffer; if the DSP gives none, on to the votes coming off. */
    static uint32_t m[2 + AU_GL_GPR_BUFFER / 4 + 2];
    const bool play = (options & AU_GL_PLAY) != 0, tone = (options & AU_GL_TONE) != 0, mic = (options & AU_GL_MIC) != 0;
    if (mic && au_mic_turn(&g, &au_mic_state)) next_ask = g.ms + 20;
    else if (tone && g.graph_sent == (au_tone_vi ? AU_TONE_VI_WRITES_AFTER : AU_TONE_WRITES_AFTER) && !tone_done) {
     tone_done = true;
     au_gl_rep3[72] = g.map_handle != 0 ? au_tone_stage(&g, au_gl_rep3, t0, hz) : 0x400;
     next_ask = g.ms + 20;
    }
    else if (!tone && play && g.graph_sent == AU_PLAY_WRITES_AFTER && g.map_handle != 0 && (g.write_sent < AU_PLAY_BUFFERS || (g.write_done < AU_PLAY_BUFFERS && g.ms < writes_until))) {
     /* Two buffers ahead of what it has played, so that it does not run dry and neither does the wait stretch. */
     next_ask = g.ms + 2;
     if (g.write_sent < AU_PLAY_BUFFERS && g.write_sent < g.write_done + 2) {
      uint32_t i = g.write_sent % 4;
      uint32_t bytes = fuxi_ar_write((uint8_t *)(m + 2), AU_GL_GPR_BUFFER, 1, AU_GL_WRITE_TOKEN + g.write_sent, (AU_PLAY_PA + i * AU_PLAY_BYTES) | AU_PLAY_SID << 32, g.map_handle, AU_PLAY_BYTES);
      if (bytes != 0 && au_gl_data_send(&g, m, bytes)) writes_until = g.ms + 3000;
     }
    }
    else if (!g.big_have) { (void)au_gl_big_ask(&g); next_ask = g.ms + 100; }
    else {
     uint32_t bytes = mic ? au_mic_build(g.graph_sent, m, AU_GL_GPR_BUFFER) : tone ? au_tone_build(g.graph_sent, m, AU_GL_GPR_BUFFER, g.map_handle) :
                      play ? au_play_build(g.graph_sent, AU_GL_GRAPH_TOKEN + g.graph_sent, m, AU_GL_GPR_BUFFER, g.map_handle) : au_graph_build(g.graph_sent, m, AU_GL_GPR_BUFFER);
     if (bytes != 0 && au_gl_graph_send(&g, m, bytes)) next_ask = g.ms + (play ? 300 : 1500);
     else if (bytes == 0) graph_until = 0;
    }
   }
   else if (au_gl_prm_step(&g)) { next_ask = g.ms + 1500; graph_until = g.ms + ((options & (AU_GL_PLAY | AU_GL_MIC)) != 0 ? 25000 : 8000); }
  }
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
 r[72] = g.gpr_asked; r[73] = g.gpr_heard; r[74] = g.gpr_other; r[75] = g.gpr_ready; r[76] = g.gpr_asked_ms; r[77] = g.gpr_first_ms; r[78] = g.gpr_last_ms;
 r[79] = g.gpr_lcid | g.gpr_rcid << 16; r[80] = g.gpr_acked | g.gpr_offers << 8; r[81] = g.gpr_riid_n; r[82] = g.gpr_first_bytes; r[83] = g.gpr_last_bytes;
 for (unsigned i = 0; i < AU_GL_GPR_WORDS; i++) r[84 + i] = g.gpr_first[i];
 for (unsigned i = 0; i < 8; i++) r[100 + i] = g.gpr_last[i];
 for (unsigned i = 0; i < AU_GL_GRAPH_MOST; i++) { au_gl_rep2[i] = g.graph_status[i]; au_gl_rep2[48 + i] = g.graph_heard_ms[i]; }
 au_gl_rep2[32] = g.write_sent; au_gl_rep2[33] = g.write_done; au_gl_rep2[34] = g.write_first_ms; au_gl_rep2[35] = g.write_last_ms;
 au_gl_rep2[36] = g.write_status; au_gl_rep2[37] = g.write_bad; au_gl_rep2[38] = g.map_handle; au_gl_rep2[39] = g.graph_steps;
 if ((options & AU_GL_PLAY) != 0) {
  au_gl_rep2[42] = au_rd(AU_SMMU + 0x48);    /* sGFSR: a stream it could not place, or a fault */
  au_gl_rep2[43] = au_rd(AU_SMMU + 0x50);    /* sGFSYNR0 */
  au_gl_rep2[44] = au_rd(AU_SMMU + 0x54);    /* sGFSYNR1: the stream */
  au_gl_rep2[45] = au_rd(AU_PLAY_PA);        /* still the zeros */
  au_smmu_back();
  au_smmu_slot = second_slot; au_smmu_old[0] = second_old[0]; au_smmu_old[1] = second_old[1];
  au_smmu_back();
 }
 r[114] = g.big_size; r[115] = g.big_refused; r[126] = g.graph_sent; r[127] = g.big_have | g.big_asked << 8;
 for (unsigned i = 0; i < AU_GL_PRM_STEPS; i++) { r[116 + i] = g.prm_status[i]; r[120 + i] = g.prm_heard_ms[i]; }
 r[124] = g.prm_sent; r[125] = g.prm_sent_ms[0];
 return g.stuck ? 0x210 + g.stuck : 1;
}
/*
 * Read only: what apps_smmu would do with a stream, an LPASS one being what the DSP's shared memory reaches the AP's DRAM by. Layout as
 * xnu/fuxi-wlan's wl_smmu_probe() reads it (arm-smmu v2). r: magic, sid, faults, sCR0, IDR0, IDR1, matches found, then for each smr, s2cr,
 * and when it translates its context bank's SCTLR, TCR and CBAR.
 */
static uint32_t au_smmu_rep[128];
static void au_smmu_look(uint32_t *r, uint32_t sid)
{
 const uint64_t base = 0x15000000ull;
 uint32_t idr1, shift, pages, count, found = 0;
 uint64_t cbs;
 au_faults = 0;
 r[0] = 0xa0d10004; r[1] = sid;
 r[3] = au_rd(base); r[4] = au_rd(base + 0x20); r[5] = idr1 = au_rd(base + 0x24);
 shift = (idr1 >> 31) ? 16 : 12;
 pages = 1u << (((idr1 >> 28) & 7) + 1);
 cbs = base + ((uint64_t)pages << shift);
 count = r[4] & 0xff;
 for (uint32_t i = 0; i < count && i < 128 && 7 + 6 * found + 5 < 128; i++) {
  uint32_t smr = au_rd(base + 0x800 + 4 * i), id = smr & 0xffff, mask = (smr >> 16) & 0x7fff;
  if (!(smr >> 31) || ((id ^ sid) & ~mask & 0x7fff)) continue;
  uint32_t s2cr = au_rd(base + 0xc00 + 4 * i), *m = r + 7 + 6 * found;
  m[0] = i; m[1] = smr; m[2] = s2cr;
  if (((s2cr >> 16) & 3) == 0) {
   uint32_t cb = s2cr & 0xff;
   m[3] = au_rd(cbs + ((uint64_t)cb << shift));
   m[4] = au_rd(cbs + ((uint64_t)cb << shift) + 0x30);
   m[5] = au_rd(base + (1ull << shift) + 4 * cb);
  }
  found++;
 }
 r[6] = found;
 r[2] = au_faults;
}
/* ---- the amplifiers' identities (au_amp.h) ---- */
#define AU_AMP_RD(pa) au_rd(pa)
#define AU_AMP_WR(pa, v) probe_safe_wr32((pa), (v))
#define AU_AMP_DELAY_US(us) delay(us)
#include "au_amp.h"
static uint32_t au_amp_rep[128];
/* TLMM: CTL pull [1:0] (1 down, 3 up), func [5:2], drive [8:6] = mA/2-1, OE [9], eGPIO present [11], owned by the TLMM [12]. IO: bit 1 out, bit 0 in. */
static void au_pin(unsigned n, uint32_t ctl, uint32_t out)
{
 uint64_t c = AU_TLMM + 0x1000u * n;
 uint32_t was = au_rd(c);
 ctl |= was & (1u << 11);
 if (was & (1u << 11)) ctl |= 1u << 12;     /* as Linux msm_pinmux_set_mux(): claimed from LPASS when it can be */
 if (ctl & (1u << 9)) (void)probe_safe_wr32(c + 4, out << 1);
 (void)probe_safe_wr32(c, ctl);
}
#define AU_AMP_RESET_LOW(n)  au_pin((n), 1u | 1u << 9, 0)
#define AU_AMP_RESET_HIGH(n) au_pin((n), 1u | 1u << 9, 1)
static uint32_t au_amp_run(uint32_t *r, uint32_t which)
{
 static const struct { uint8_t address, reset; } part[2] = {{0x40, 183}, {0x42, 182}};
 static const uint8_t pins[4] = {0, 1, 182, 183};
 static struct au_amp a[2];
 uint32_t saved[4][2], vote;
 au_faults = 0;
 /* Wrapper 2's clocks, as APCS votes them: core, its AHBs, core2x, SE0. Only with them may the engine be touched: gated, it hangs the bus (touch lane ts-02). */
 vote = au_rd(AU_GCC + 0x52010);
 r[4] = vote;
 if (probe_safe_wr32(AU_GCC + 0x52010, vote | 0x1fu)) return 0x301;
 /* s_ahb is under hardware gating (bit 1, as Linux's hwcg_reg), so its off bit says nothing while idle: a-amp1. Read as ON or that. a-snap read this engine in that state. */
 for (unsigned i = 0; i < 10000 && !(au_on(au_rd(AU_GCC + 0x233cc)) && (au_on(au_rd(AU_GCC + 0x233d0)) || (au_rd(AU_GCC + 0x233d0) & 2u))); i++) delay(10);
 r[5] = au_rd(AU_GCC + 0x233cc); r[6] = au_rd(AU_GCC + 0x233d0); r[3] = au_rd(AU_GCC + 0x52010);
 if (!au_on(r[5]) || !(au_on(r[6]) || (r[6] & 2u))) return 0x302;
 r[7] = au_rd(AU_SE0 + AU_SE_FW_REV); r[8] = au_rd(AU_SE0 + AU_SE_IF_DISABLE); r[9] = au_rd(AU_SE0 + AU_SE_STATUS);
 if (au_faults) return 0x303;
 if (!au_amp_engine(AU_SE0)) return 0x304;
 /* The SE's own clock is a DFS root: it comes up once the engine asks for a level, as ts_clock's S4 does. */
 for (unsigned i = 0; i < 10000 && !au_on(au_rd(AU_GCC + 0x1e004)); i++) delay(10);
 r[10] = au_rd(AU_GCC + 0x1e004);
 if (!au_on(r[10])) return 0x305;
 for (unsigned i = 0; i < 4; i++) { saved[i][0] = au_rd(AU_TLMM + 0x1000u * pins[i]); saved[i][1] = au_rd(AU_TLMM + 0x1000u * pins[i] + 4); }
 r[11] = saved[0][0]; r[12] = saved[1][0]; r[13] = saved[2][0]; r[14] = saved[3][0];
 /* Both held in reset before the I2C pins come up, and the line idle high with its pull-ups before any command: 2 mA, pull up, func 2 qup2_se0_mirb. */
 AU_AMP_RESET_LOW(182); AU_AMP_RESET_LOW(183);
 r[15] = au_rd(AU_TLMM + 0x1000u * 182); r[16] = au_rd(AU_TLMM + 0x1000u * 183);
 au_pin(0, 3u | 2u << 2, 0); au_pin(1, 3u | 2u << 2, 0);
 delay(3000);
 r[17] = au_rd(AU_TLMM + 4); r[18] = au_rd(AU_TLMM + 0x1000 + 4);
 for (unsigned p = 0; p < 2; p++) {
  uint32_t *m = r + 24 + 12 * p;
  a[p].address = part[p].address;
  if (!(which & (1u << p))) continue;
  AU_AMP_RESET_LOW(part[p].reset);
  delay(3000);
  AU_AMP_RESET_HIGH(part[p].reset);
  delay(3000);
  m[0] = au_rd(AU_TLMM + 0x1000u * part[p].reset + 4);
  au_amp_identify(AU_SE0, &a[p]);
  AU_AMP_RESET_LOW(part[p].reset);
  delay(3000);
  m[1] = a[p].address; m[2] = a[p].result; m[3] = a[p].boot_ms; m[4] = a[p].boot_status; m[5] = a[p].boot_error;
  m[6] = a[p].devid; m[7] = a[p].revid; m[8] = a[p].otpid; m[9] = a[p].irq_seen; m[10] = 1;
 }
 /* Everything back: the pins as they were found, so reset lines to their pull-down, the amplifiers held. */
 for (unsigned i = 0; i < 4; i++) {
  (void)probe_safe_wr32(AU_TLMM + 0x1000u * pins[i] + 4, saved[i][1] & 2u);
  (void)probe_safe_wr32(AU_TLMM + 0x1000u * pins[i], saved[i][0]);
 }
 r[19] = au_rd(AU_TLMM + 0x1000u * 182); r[20] = au_rd(AU_TLMM + 0x1000u * 183);
 r[21] = au_faults;
 return 1;
}
/* ---- the first tone. The owner's word, 1 November: unattended, bottom speaker alone, the lowest of the levels, a second. ---- */
#include "au_amp_on.h"
static const int32_t au_sine[24] = {0, 55580967, 107374182, 151850025, 185977539, 207430992, 214748365, 207430992, 185977539, 151850025, 107374182, 55580967, 0, -55580967, -107374182, -151850025, -185977539, -207430992, -214748365, -207430992, -185977539, -151850025, -107374182, -55580967};
#define AU_TONE_FRAMES 48000u       /* a second */
#define AU_TONE_FADE 1920u          /* 40 ms each way */
static uint32_t au_tone_frame;
/* A period into the DSP's next buffer: zeros, or the tone while there are frames of it left. tone is 2 kHz at -20 dBFS: 24 frames a cycle. The same on all four slots. */
static void au_tone_fill(uint32_t index, bool tone)
{
 uint64_t at = AU_PLAY_PA + index * au_period_bytes;
 for (uint32_t i = 0; i < 960; i++) {
  int32_t v = 0;
  if (tone && au_tone_frame < AU_TONE_FRAMES) {
   uint32_t left = AU_TONE_FRAMES - au_tone_frame, gain = au_tone_frame < AU_TONE_FADE ? au_tone_frame : left < AU_TONE_FADE ? left : AU_TONE_FADE;
   v = (int32_t)((int64_t)au_sine[au_tone_frame % 24] * gain / AU_TONE_FADE);
   au_tone_frame++;
  }
  for (uint32_t c = 0; c < 4; c++) au_wr(at + 16 * i + 4 * c, (uint32_t)v);
 }
}
/* GLINK and the DSP's buffers kept going for ms, two periods ahead. */
static void au_tone_pump(struct au_gl *g, uint64_t t0, uint64_t hz, uint32_t ms, bool tone)
{
 static uint32_t m[2 + AU_GL_GPR_BUFFER / 4 + 2];
 uint32_t until = g->ms + ms;
 while (g->ms < until && !g->stuck) {
  g->ms = (uint32_t)((rsr("cntpct_el0") - t0) * 1000 / hz);
  while (au_gl_step(g)) { }
  if (au_tone_vi && g->read_sent < AU_VI_PERIODS && g->read_sent < g->read_done + 3) {
   uint32_t bytes = fuxi_ar_read((uint8_t *)(m + 2), AU_GL_GPR_BUFFER, 1, AU_GL_READ_TOKEN + g->read_sent, (AU_VI_PA + g->read_sent * au_period_bytes) | AU_PLAY_SID << 32, g->map_handle, au_period_bytes);
   if (bytes != 0) (void)au_gl_read_send(g, m, bytes);
  }
  if (g->write_sent < g->write_done + 2) {
   uint32_t index = g->write_sent % 4;
   au_tone_fill(index, tone);
   uint32_t bytes = fuxi_ar_write((uint8_t *)(m + 2), AU_GL_GPR_BUFFER, 1, AU_GL_WRITE_TOKEN + (g->write_sent & 0xffffu), (AU_PLAY_PA + index * au_period_bytes) | AU_PLAY_SID << 32, g->map_handle, au_period_bytes);
   if (bytes != 0) (void)au_gl_data_send(g, m, bytes);
  }
  delay(200);
 }
}
/* What the pads themselves do: of 2000 reads of each of the TDM's four, how many high, and how many changed from the read before. Read only. */
static void au_tone_pads(uint32_t *q)
{
 for (unsigned p = 0; p < 4; p++) {
  uint32_t high = 0, changes = 0, last = 0;
  for (unsigned i = 0; i < 2000; i++) {
   uint32_t now = au_rd(AU_TLMM + 0x1000u * (121u + p) + 4) & 1u;
   high += now;
   changes += i != 0 && now != last;
   last = now;
  }
  q[2 * p] = high; q[2 * p + 1] = changes;
 }
}
static uint32_t au_tone_stage(struct au_gl *g, uint32_t *q, uint64_t t0, uint64_t hz)
{
 static const uint8_t pins[8] = {0, 1, 182, 183, 121, 122, 123, 124};
 static struct au_on o;
 uint32_t saved[8][2], result = 1;
 int r = AU_ON_OK;
 /* Every step of the list so far said yes, and zeros are being played, or the amplifier is not touched at all. */
 for (uint32_t i = 0; i < AU_TONE_WRITES_AFTER; i++) { if (g->graph_status[i] != 0) return 0x401 + (i << 8); }
 au_tone_pump(g, t0, hz, 300, false);
 q[73] = g->write_done; q[74] = g->write_bad;
 if (g->write_done < 5 || g->write_bad != 0) return 0x402;
 /* The bus, as au_amp_run() brings it up. */
 au_faults = 0;
 if (probe_safe_wr32(AU_GCC + 0x52010, au_rd(AU_GCC + 0x52010) | 0x1fu)) return 0x403;
 for (unsigned i = 0; i < 10000 && !au_on(au_rd(AU_GCC + 0x233cc)); i++) delay(10);
 if (!au_on(au_rd(AU_GCC + 0x233cc)) || au_faults) return 0x404;
 if (!au_amp_engine(AU_SE0)) return 0x405;
 for (unsigned i = 0; i < 10000 && !au_on(au_rd(AU_GCC + 0x1e004)); i++) delay(10);
 if (!au_on(au_rd(AU_GCC + 0x1e004))) return 0x406;
 for (unsigned i = 0; i < 8; i++) { saved[i][0] = au_rd(AU_TLMM + 0x1000u * pins[i]); saved[i][1] = au_rd(AU_TLMM + 0x1000u * pins[i] + 4); }
 /* Both in reset, whatever comes; I2C and the TDM's clocks and data out, 8 mA as the Xiaomi 14's port. gpio68, the top amplifier's switch, is not touched. */
 AU_AMP_RESET_LOW(182); AU_AMP_RESET_LOW(183);
 au_pin(0, 3u | 2u << 2, 0); au_pin(1, 3u | 2u << 2, 0);
 au_pin(121, 1u << 2 | 3u << 6, 0); au_pin(123, 1u << 2 | 3u << 6, 0); au_pin(124, 1u << 2 | 3u << 6, 0);
 if (au_tone_vi) au_pin(122, 1u << 2, 0);
 au_tone_pump(g, t0, hz, 50, false);
 q[75] = au_rd(AU_TLMM + 0x1000u * 121); q[76] = au_rd(AU_TLMM + 0x1000u * 124);
 au_tone_pads(q + 40);
 AU_AMP_RESET_HIGH(183);
 delay(3000);
 for (unsigned i = 0; i < sizeof(o); i++) ((volatile uint8_t *)&o)[i] = 0;
 o.se = AU_SE0; o.address = 0x40;
 r = au_on_prepare(&o, true, au_tone_vi);
 q[77] = (uint32_t)r; q[78] = o.step;
 if (r == AU_ON_OK) {
  au_tone_pump(g, t0, hz, 100, false);
  r = au_on_power_up(&o);
  q[79] = (uint32_t)r; q[80] = o.step; q[81] = o.pup_ms;
 }
 if (r == AU_ON_OK) {
  au_tone_pump(g, t0, hz, 200, false);
  r = au_on_check(&o);
  q[82] = (uint32_t)r; for (unsigned i = 0; i < 4; i++) q[83 + i] = o.status[i];
  au_tone_pads(q + 48);
 }
 if (r == AU_ON_OK) r = au_on_unmute(&o, 0);
 if (r == AU_ON_OK) {
  q[87] = g->write_done; q[88] = g->ms; q[114] = g->read_done;
  au_tone_frame = 0;
  /* Looked at ten times in the second; a thing wrong ends it, and the fade with it. */
  while (au_tone_frame < AU_TONE_FRAMES && r == AU_ON_OK) {
   au_tone_pump(g, t0, hz, 100, true);
   r = au_on_check(&o);
   if (au_tone_frame >= AU_TONE_FRAMES / 2 && q[56] == 0) { au_tone_pads(q + 56); q[56] |= 0x80000000u; }
  }
  q[89] = g->write_done; q[90] = g->ms; q[91] = au_tone_frame; q[115] = g->read_done;
  q[92] = (uint32_t)r; for (unsigned i = 0; i < 4; i++) q[93 + i] = o.status[i];
  au_tone_pump(g, t0, hz, 200, false);
  if (r == AU_ON_OK) r = au_on_check(&o);
  q[97] = (uint32_t)r; for (unsigned i = 0; i < 4; i++) q[98 + i] = o.status[i];
 }
 q[102] = o.failed_reg; q[103] = o.failed_want; q[104] = o.failed_got; q[105] = o.otp_fields;
 if (r != AU_ON_OK) result = 0x410 + (uint32_t)r;
 /* However it went: down, into reset, and the pins as they were found: the reset lines' pull-downs hold it there. */
 au_on_down(&o);
 AU_AMP_RESET_LOW(183);
 delay(3000);
 au_tone_pump(g, t0, hz, 40, false);
 for (unsigned i = 0; i < 8; i++) {
  (void)probe_safe_wr32(AU_TLMM + 0x1000u * pins[i] + 4, saved[i][1] & 2u);
  (void)probe_safe_wr32(AU_TLMM + 0x1000u * pins[i], saved[i][0]);
 }
 q[106] = au_rd(AU_TLMM + 0x1000u * 183); q[107] = au_rd(AU_TLMM + 0x1000u * 182);
 q[108] = g->read_sent; q[109] = g->read_done; q[110] = g->read_status; q[111] = g->read_bad; q[112] = g->read_bytes; q[113] = g->read_offset;
 q[116] = (uint32_t)AU_VI_PA; q[117] = au_period_bytes;
 q[118] = au_rd(AU_SMMU + 0x48); q[119] = au_rd(AU_SMMU + 0x50); q[120] = au_rd(AU_SMMU + 0x54);
 /* apps_smmu's own record, if a stream of the DSP's was refused, and context bank 4's. */
 { uint64_t cb4 = AU_SMMU + 0x80000 + 4 * 0x1000; q[121] = au_rd(cb4 + 0x58); q[122] = au_rd(cb4 + 0x60); q[123] = au_rd(cb4 + 0x68); }
 return result;
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
 if (value == 0x4154 && !monitor_report[MON_LAUNCH]) {
  static uint32_t r[16];
  for (unsigned i = 0; i < 16; i++) r[i] = 0;
  r[0] = 0xa0d10005; r[1] = index;
  au_faults = 0;
  r[2] = (uint32_t)(uintptr_t)&au_gl_rep2[0]; r[3] = (uint32_t)(uintptr_t)__builtin_frame_address(0);
  if (index & 1) {
   r[4] = au_rd(AU_PLAY_PA);
   for (uint32_t i = 0; i < AU_PLAY_BYTES; i++) au_wr(AU_PLAY_PA + 4 * i, 0);
   r[5] = au_rd(AU_PLAY_PA); r[6] = au_rd(AU_PLAY_PA + 4 * (AU_PLAY_BYTES - 1));
  }
  if (index & 2) {
   r[7] = au_rd(AU_SMMU + 0x48);
   au_smmu_bypass(index & 16 ? 0x1061 : 0x1001, index & 16 ? 0 : 0x80, (index & 8) != 0);
   r[8] = (uint32_t)(au_smmu_slot + 1);
   r[14] = au_smmu_step;
   if (au_smmu_slot >= 0) { r[9] = au_rd(AU_SMMU + 0x800 + 4 * (uint32_t)au_smmu_slot); r[10] = au_rd(AU_SMMU + 0xc00 + 4 * (uint32_t)au_smmu_slot); }
   r[11] = au_rd(AU_SMMU + 0x48);
  }
  if (index & 4) { au_smmu_back(); r[12] = au_rd(AU_SMMU + 0x48); }
  r[13] = au_faults;
  memcpy(out, r, sizeof(r)); return sizeof(r);
 }
 if (value == 0x4141 && !monitor_report[MON_LAUNCH]) {
  for (unsigned i = 0; i < 128; i++) au_amp_rep[i] = 0;
  au_amp_rep[0] = 0xa0d10006; au_amp_rep[2] = index;
  au_amp_rep[1] = au_amp_run(au_amp_rep, index & 3u);
  memcpy(out, au_amp_rep, sizeof(au_amp_rep)); return sizeof(au_amp_rep);
 }
 if (value == 0x414d && !monitor_report[MON_LAUNCH]) {
  au_smmu_look(au_smmu_rep, index);
  memcpy(out, au_smmu_rep, sizeof(au_smmu_rep)); return sizeof(au_smmu_rep);
 }
 if (value == 0x4147 && !monitor_report[MON_LAUNCH] && au_boot[1] == 1) {
  if (!au_gl_rep[0]) {
   unsigned seconds = index & 0xff;
   au_gl_rep[0] = 0xa0d10003;
   au_gl_rep[2] = seconds = seconds == 0 ? 10 : seconds > 45 ? 45 : seconds;
   au_gl_rep[3] = (index >> 8) & 31;
   au_gl_rep[1] = au_glink_run(au_gl_rep, seconds, au_gl_rep[3] == 31 ? AU_GL_ANSWER | AU_GL_GPR | AU_GL_PRM | AU_GL_GRAPH | AU_GL_PLAY :
                               au_gl_rep[3] == 15 ? AU_GL_ANSWER | AU_GL_GPR | AU_GL_PRM | AU_GL_GRAPH : au_gl_rep[3] == 7 ? AU_GL_ANSWER | AU_GL_GPR | AU_GL_PRM :
                               au_gl_rep[3] == 3 ? AU_GL_ANSWER | AU_GL_GPR : au_gl_rep[3] & 1 ? AU_GL_ANSWER : 0);
   au_gl_rep[4] = au_faults;
  }
  memcpy(out, au_gl_rep, sizeof(au_gl_rep)); return sizeof(au_gl_rep);
 }
 if (value == 0x414a && au_gl_rep[0]) { memcpy(out, au_gl_rep3, sizeof(au_gl_rep3)); return sizeof(au_gl_rep3); }
 if (value == 0x4156 && au_gl_rep[0] && au_tone_vi && (uint64_t)index * 512u + 512u <= AU_VI_PERIODS * au_period_bytes) {
  for (uint32_t i = 0; i < 128; i++) ((uint32_t *)(void *)out)[i] = au_rd(AU_VI_PA + (uint64_t)index * 512u + 4 * i);
  return 512;
 }
 if (value == 0x414f && (index == 0x4231 || index == 0x4232) && !monitor_report[MON_LAUNCH] && au_boot[1] == 1 && !au_gl_rep[0]) {
  au_tone_vi = index == 0x4232;
  au_gl_rep[0] = 0xa0d10003;
  au_gl_rep[2] = 45;
  au_gl_rep[3] = 15 | 16 | 32;
  au_gl_rep[1] = au_glink_run(au_gl_rep, 45, AU_GL_ANSWER | AU_GL_GPR | AU_GL_PRM | AU_GL_GRAPH | AU_GL_PLAY | AU_GL_TONE);
  au_gl_rep[4] = au_faults;
  memcpy(out, au_gl_rep, sizeof(au_gl_rep)); return sizeof(au_gl_rep);
 }
 if (value == 0x4143 && index == 0x4d31 && !monitor_report[MON_LAUNCH] && au_boot[1] == 1 && !au_gl_rep[0]) {
  au_mic_begin(&au_mic_state, au_mic_rep);
  au_gl_rep[0] = 0xa0d10003;
  au_gl_rep[2] = 40;
  au_gl_rep[3] = 15 | 64;
  au_gl_rep[1] = au_glink_run(au_gl_rep, 40, AU_GL_ANSWER | AU_GL_GPR | AU_GL_PRM | AU_GL_GRAPH | AU_GL_MIC);
  au_gl_rep[4] = au_faults;
  memcpy(out, au_gl_rep, sizeof(au_gl_rep)); return sizeof(au_gl_rep);
 }
 if (value == 0x4145 && au_mic_rep[0]) { memcpy(out, au_mic_rep, sizeof(au_mic_rep)); return sizeof(au_mic_rep); }
 if (value == 0x4149 && au_gl_rep[0]) { memcpy(out, au_gl_rep2, sizeof(au_gl_rep2)); return sizeof(au_gl_rep2); }
 if (value == 0x4148 && au_gl_rep[0] && index < AU_GL_LOG_WORDS / 128) {
  memcpy(out, au_gl_log + 128u * index, 512); return 512;
 }
 return 0;
}
