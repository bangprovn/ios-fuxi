/* Fuxi standalone DWC3 probe. Control-endpoint state machine adapted from
 * ios-sweet patches 0073/0074 (dipa-studio). No filesystem or UFS access.
 * USB register base and clock offsets verified against Fuxi's stock DTB and
 * LineageOS gcc-kalama.c. PHY setup is inherited from ABL, not reprogrammed.
 */
#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include "../font.h"
#include "monitor.h"
#include "../../xnu/fuxi-ath12k/os/fuxi_remote.h"
#define printf(...) ((void)0)
#define rsr(r) ({ uint64_t v; __asm__ volatile("mrs %0, " r : "=r"(v)); v; })
static void *memset(void *dst, int c, size_t n)
{
 volatile uint8_t *d=dst;
 while (n--) *d++=(uint8_t)c;
 return dst;
}
static void *memcpy(void *dst, const void *src, size_t n)
{
 volatile uint8_t *d=dst; const volatile uint8_t *s=src;
 while(n--) *d++=*s++;
 return dst;
}
static void delay(unsigned us)
{
 uint64_t until=rsr("cntpct_el0")+rsr("cntfrq_el0")*us/1000000;
 while(rsr("cntpct_el0")<until) __asm__ volatile("yield");
}
static uint32_t usb_rd(uintptr_t base, uint32_t off)
{
 uint32_t v=*(volatile uint32_t *)(base+off);
 __asm__ volatile("dmb oshld" ::: "memory");
 return v;
}
static uint8_t dma_area[0x4000] __attribute__((aligned(0x4000)));
static uintptr_t probe_fdt;
static uint32_t probe_fdt_size;
uint64_t probe_fdt_address(void) { return probe_fdt; }
uint32_t probe_fdt_bytes(void) { return probe_fdt_size; }
static struct {
 uint64_t magic, version, midr, mpidr, mmfr0, mmfr1, current_el, cntfrq, sctlr;
 uint64_t fdt_address, fdt_size, gsnpsid, gctl_before, dctl_before, dsts_before;
 uint64_t gcc_master, gcc_cfg, configured, speed;
} probe_info;
_Static_assert(sizeof(probe_info)==19*8, "USB report format");

/* Read-only display snapshot, requested after basic USB/FDT capture. */
static uint32_t display_regs[10 * 9 + 6 * 3];
static uint32_t display_snapshot(void)
{
 static const uint32_t sspp[] = {0x5000,0x7000,0x9000,0xb000,
     0x25000,0x27000,0x29000,0x2b000,0x2d000,0x2f000};
 unsigned n = 0;
 for (unsigned i = 0; i < 10; i++) {
  uint32_t off = sspp[i];
  display_regs[n++] = 0x0ae00000 + off;
  display_regs[n++] = usb_rd(0x0ae00000, off + 0x00); /* source size */
  display_regs[n++] = usb_rd(0x0ae00000, off + 0x0c); /* output size */
  display_regs[n++] = usb_rd(0x0ae00000, off + 0x14); /* source address */
  display_regs[n++] = usb_rd(0x0ae00000, off + 0x24); /* stride */
  display_regs[n++] = usb_rd(0x0ae00000, off + 0x30); /* format */
  display_regs[n++] = usb_rd(0x0ae00000, off + 0x34); /* unpack pattern */
  display_regs[n++] = usb_rd(0x0ae00000, off + 0x38); /* operation mode */
  display_regs[n++] = usb_rd(0x0ae00000, off + 0x170); /* multirect */
 }
 for (unsigned i = 0; i < 6; i++) {
  uint32_t off = 0x16000 + i * 0x1000;
  display_regs[n++] = 0x0ae00000 + off;
  display_regs[n++] = usb_rd(0x0ae00000, off + 0x14); /* top */
  display_regs[n++] = usb_rd(0x0ae00000, off + 0x1c); /* start */
 }
 return n * sizeof(uint32_t);
}

static uint32_t display_paint(void)
{
 /* Only touch the exact linear scanout geometry observed in probe-03. */
 if (usb_rd(0xae05000, 0x14) != 0xb8000000 ||
     usb_rd(0xae05000, 0x24) != 4320 ||
     usb_rd(0xae05000, 0x00) != ((2400u << 16) | 1080u) ||
     usb_rd(0xae05000, 0x30) != 0x237ff) return 0;
 volatile uint32_t *fb = (volatile uint32_t *)0xb8000000UL;
 for (unsigned y = 0; y < 160; y++)
  for (unsigned x = 0; x < 1080; x++) fb[y * 1080 + x] = 0xff102040;
 const char *text = "FUXI NATIVE PROBE";
 for (unsigned c = 0; text[c]; c++) {
  const unsigned char *glyph = font8x16[(unsigned char)text[c] - 32];
  for (unsigned gy = 0; gy < 16; gy++)
   for (unsigned gx = 0; gx < 8; gx++)
    if (glyph[gy] & (0x80u >> gx))
     for (unsigned sy = 0; sy < 3; sy++)
      for (unsigned sx = 0; sx < 3; sx++)
       fb[(48 + gy * 3 + sy) * 1080 + 48 + c * 24 + gx * 3 + sx] = 0xffffffff;
 }
 __asm__ volatile("dsb sy" ::: "memory");
 *(volatile uint32_t *)0x0ae1601cUL = 1; /* CTL0 START: command-mode frame */
 __asm__ volatile("dsb sy" ::: "memory");
 return 1;
}

/* Autorefresh-collision test (0x53). [0] stage, [1] AR cfg before, [2] AR cfg after,
 * [3] wr line before, [4] wr line after wait, [5] poll iterations, [6] wr line after START,
 * [7] DSI status, [8] DSI FIFO status. Mode 'NS' paints with no START (autorefresh
 * delivers); 'AS' disables autorefresh, waits for the transfer to end, then STARTs;
 * 'RE' restores the saved autorefresh config. */
static uint32_t ar_report[10], ar_saved;
static void ar_paint(uint32_t color)
{
 volatile uint32_t *fb = (volatile uint32_t *)0xb8000000UL;
 for (unsigned i = 0; i < 1080 * 2400; i++) fb[i] = color;
 const char *text = "FUXI AUTOREFRESH TEST";
 for (unsigned c = 0; text[c]; c++) {
  const unsigned char *glyph = font8x16[(unsigned char)text[c] - 32];
  for (unsigned gy = 0; gy < 16; gy++)
   for (unsigned gx = 0; gx < 8; gx++)
    if (glyph[gy] & (0x80u >> gx))
     for (unsigned sy = 0; sy < 4; sy++)
      for (unsigned sx = 0; sx < 4; sx++)
       fb[(1100 + gy * 4 + sy) * 1080 + 48 + c * 32 + gx * 4 + sx] = 0xffffffff;
 }
 __asm__ volatile("dsb sy" ::: "memory");
}
static void ar_test(uint32_t mode)
{
 for (unsigned i = 0; i < 10; i++) ar_report[i] = 0;
 if (usb_rd(0xae05000, 0x14) != 0xb8000000 || usb_rd(0xae05000, 0x24) != 4320 ||
     usb_rd(0xae05000, 0x00) != ((2400u << 16) | 1080u) ||
     usb_rd(0xae05000, 0x30) != 0x237ff) { ar_report[0] = 0x100; return; }
 ar_report[1] = usb_rd(0xae36000, 0x2b4);
 ar_report[3] = usb_rd(0xae36000, 0x2a8) & 0xffff;
 if (mode == 0x4e53) {
  ar_paint(0xff0040c0);               /* blue, no START */
  ar_report[0] = 1;
 } else if (mode == 0x4153) {
  if (ar_report[1] & 0x80000000u) ar_saved = ar_report[1];
  *(volatile uint32_t *)0xae362b4UL = ar_report[1] & ~0x80000000u;
  __asm__ volatile("dsb sy" ::: "memory");
  /* Linux _dpu_encoder_phys_cmd_is_ongoing_pptx: wait for wr line 0 or >= vdisplay. */
  unsigned i;
  for (i = 0; i < 200000; i++) {
   uint32_t l = usb_rd(0xae36000, 0x2a8) & 0xffff;
   if (i > 20000 && (l == 0 || l >= 2400)) break;  /* >= 20 ms: one frame past */
   delay(1);
  }
  ar_report[4] = usb_rd(0xae36000, 0x2a8) & 0xffff; ar_report[5] = i;
  ar_paint(0xffc02000);               /* red, single manual START */
  *(volatile uint32_t *)0x0ae1601cUL = 1;
  __asm__ volatile("dsb sy" ::: "memory");
  delay(40000);
  ar_report[6] = usb_rd(0xae36000, 0x2a8) & 0xffff;
  ar_report[0] = 2;
 } else if (mode == 0x5245 && ar_saved) {
  *(volatile uint32_t *)0xae362b4UL = ar_saved;
  __asm__ volatile("dsb sy" ::: "memory");
  ar_report[0] = 3;
 }
 ar_report[2] = usb_rd(0xae36000, 0x2b4);
 ar_report[7] = usb_rd(0xae94000, 0x4);
 ar_report[8] = usb_rd(0xae94000, 0xc);
}

static uint64_t ram_report[8];
static void ram_probe(void)
{
 extern char _head[], _end[];
 const uint64_t first = 0x8c0000000ULL, size = 0x40000000;
 const uint64_t stride = 0x4000000;
 ram_report[0] = first; ram_report[1] = size;
 ram_report[2] = stride; ram_report[3] = size / stride;
 ram_report[4] = 0; ram_report[5] = 0;
 ram_report[6] = (uintptr_t)_head; ram_report[7] = (uintptr_t)_end;
 /* Only sample the advertised upper bank, restoring each original word. */
 for (unsigned page = 0; page < size / stride; page++) {
  bool ok = true;
  for (unsigned word = 0; word < 8; word++) {
   uint64_t pa = first + page * stride + 0x200000 + word * 8;
   volatile uint64_t *p = (volatile uint64_t *)(uintptr_t)pa;
   uint64_t saved = *p, pattern = 0x55aa33cc77ee9911ULL ^ pa;
   *p = pattern;
   __asm__ volatile("dsb sy" ::: "memory");
   if (*p != pattern) ok = false;
   *p = saved;
   __asm__ volatile("dsb sy" ::: "memory");
   if (*p != saved) ok = false;
   if (!ok && !ram_report[5]) ram_report[5] = pa;
  }
  if (ok) ram_report[4] |= 1ULL << page;
 }
}
static void usb_poll(void);
static void thermal_monitor_idle(void); /* thermal lane */
static __attribute__((noreturn)) void reboot(void);
volatile uint64_t monitor_report[MON_FIELDS] __attribute__((aligned(64)));
char monitor_error[128];
/* Fixed read-only UFS handoff snapshot; never probe a halted bus. */
static uint32_t ufs_report[24];
static void ufs_snapshot(void)
{
 static const uint32_t offsets[]={0x00,0x08,0x18,0x20,0x24,0x30,0x34,
  0x50,0x54,0x58,0x60,0x70,0x74,0x78,0x80,0x90};
 ufs_report[0]=0x55465846; ufs_report[1]=1;
 ufs_report[2]=usb_rd(0x100000,0x77024); /* AHB */
 ufs_report[3]=usb_rd(0x100000,0x77018); /* AXI */
 ufs_report[4]=usb_rd(0x100000,0x77068); /* UniPro */
 ufs_report[5]=usb_rd(0x100000,0x77004); /* GDSC */
 ufs_report[6]=0;
 if (!(ufs_report[5]&0x80000000) || ((ufs_report[2]|ufs_report[3]|ufs_report[4]) & 0x80000000)) return;
 for (unsigned i=0;i<sizeof(offsets)/sizeof(offsets[0]);i++)
  ufs_report[8+i]=usb_rd(0x1d84000,offsets[i]);
 ufs_report[6]=1;
}
/* Clear only the documented hardware-gate bit for a temporary AHB read. */
static void ufs_snapshot_awake(void)
{
 uint32_t old=usb_rd(0x100000,0x77024);
 if (!(old&1) || !(usb_rd(0x100000,0x77004)&0x80000000)) { ufs_snapshot(); return; }
 *(volatile uint32_t *)0x177024UL=old&~2u;
 __asm__ volatile("dsb sy" ::: "memory");
 for(unsigned i=0;i<500 && (usb_rd(0x100000,0x77024)&0x80000000);i++) delay(20);
 ufs_snapshot();
 *(volatile uint32_t *)0x177024UL=old;
 __asm__ volatile("dsb sy" ::: "memory");
}
static uint32_t display_clocks[14];
/* dispcc-kalama.c RCGs (CMD, CFG, M, N, D): mdp, pclk0, byte0, esc0, ahb;
 * then PLL0 and PLL1 register words 0x00-0x4c. Valid only if word 0 = 1. */
static uint32_t display_rcg[1+25+40];
static void display_clock_snapshot(void)
{
 static const uint32_t offsets[]={0x9000,0x80a4,0x800c,0x8028,0x802c,0x8004,0x8038,0x8024,0xc004};
 display_clocks[0]=0x43445846; display_clocks[1]=1;
 display_clocks[2]=usb_rd(0x100000,0x27004);
 display_clocks[3]=usb_rd(0x100000,0x2700c);
 display_clocks[4]=0;
 if (display_clocks[2]&0x80000000) return;
 for(unsigned i=0;i<sizeof(offsets)/sizeof(offsets[0]);i++)
  display_clocks[5+i]=usb_rd(0xaf00000,offsets[i]);
 display_clocks[4]=1;
 static const uint32_t rcg[]={0x80d8,0x80a8,0x8108,0x8140,0x82e8};
 unsigned n=1;
 for(unsigned i=0;i<5;i++)
  for(unsigned j=0;j<5;j++) display_rcg[n++]=usb_rd(0xaf00000,rcg[i]+4*j);
 for(unsigned pll=0;pll<2;pll++)
  for(unsigned j=0;j<20;j++) display_rcg[n++]=usb_rd(0xaf00000,pll*0x1000+4*j);
 display_rcg[0]=1;
}
static void display_clock_snapshot_awake(void)
{
 uint32_t old=usb_rd(0x100000,0x27004);
 if (!(old&1)) { display_clock_snapshot(); return; }
 /* gcc-kalama.c: GCC_DISP_AHB_CLK has HWCG at bit 1, enable at bit 0. */
 *(volatile uint32_t *)0x127004UL=old&~2u;
 __asm__ volatile("dsb sy" ::: "memory");
 for(unsigned i=0;i<500 && (usb_rd(0x100000,0x27004)&0x80000000);i++) delay(20);
 display_clock_snapshot();
 *(volatile uint32_t *)0x127004UL=old;
 __asm__ volatile("dsb sy" ::: "memory");
}

/*
 * Apps RSC DRV2 (live FDT: 0x17a20000, tcs-offset 0xd00; ACTIVE 3, SLEEP 2,
 * WAKE 2, fast-path 1). RSC v3 TCS layout = Linux rpmh_rsc_reg_offset_ver_3_0.
 * Addresses from OrangeFox cmd-db: mmcx.lvl 0x30080, MC0 0x50000, SH0 0x50004,
 * MM0 0x50030, MM1 0x50034, ACV 0x50068.
 */
#define RH_DRV_PA 0x17a20000UL
#define RH_TCS_PA 0x17a20d00UL
#define RH_STRIDE 0x2a0u
/* ver_3_0: WAIT 0x20, CONTROL 0x24, STATUS 0x28, ENABLE 0x2c; cmd n at 0x34+0x18*n. */
#define RH_WAIT   0x20u
#define RH_CTRL   0x24u
#define RH_IDLE   0x28u
#define RH_EN     0x2cu
#define RH_MSGID  0x34u
#define RH_ADDR   0x38u
#define RH_DATA   0x3cu
#define RH_CSTAT  0x40u
static uint32_t rsc_report[5+8*8];
static uint32_t rh_rd(uint32_t off) { return usb_rd(RH_TCS_PA, off); }
static void rh_wr(uint32_t off, uint32_t v)
{
 __asm__ volatile("dmb oshst" ::: "memory");
 *(volatile uint32_t *)(RH_TCS_PA+off)=v;
 __asm__ volatile("dsb osh" ::: "memory");
}
static void rsc_snapshot(void)
{
 unsigned n=0;
 rsc_report[n++]=usb_rd(RH_DRV_PA,0);
 rsc_report[n++]=usb_rd(RH_DRV_PA,4);
 rsc_report[n++]=usb_rd(RH_DRV_PA,0xc);
 rsc_report[n++]=rh_rd(0); rsc_report[n++]=rh_rd(4);
 for (unsigned t=0;t<8;t++) {
  uint32_t b=t*RH_STRIDE;
  rsc_report[n++]=rh_rd(b+RH_CTRL); rsc_report[n++]=rh_rd(b+RH_IDLE);
  rsc_report[n++]=rh_rd(b+RH_EN); rsc_report[n++]=rh_rd(b+RH_WAIT);
  rsc_report[n++]=rh_rd(b+RH_MSGID); rsc_report[n++]=rh_rd(b+RH_ADDR);
  rsc_report[n++]=rh_rd(b+RH_DATA); rsc_report[n++]=rh_rd(b+RH_CSTAT);
 }
}
/* One write-with-response command in ACTIVE TCS t; 0 on proven completion. */
static uint32_t rh_send(unsigned t, uint32_t addr, uint32_t data)
{
 uint32_t b=t*RH_STRIDE, bit=1u<<t;
 if (rh_rd(b+RH_IDLE)==0 || rh_rd(b+RH_CTRL) || rh_rd(b+RH_EN) || rh_rd(b+RH_WAIT) ||
     (rh_rd(4)&bit)) return 1;
 uint32_t irq_en=rh_rd(0);
 rh_wr(0,irq_en&~bit);
 rh_wr(b+RH_MSGID,8u|(1u<<8)|(1u<<16));
 rh_wr(b+RH_ADDR,addr); rh_wr(b+RH_DATA,data);
 rh_wr(b+RH_WAIT,1); rh_wr(b+RH_EN,1);
 rh_wr(b+RH_CTRL,0); rh_wr(b+RH_CTRL,1u<<16);
 rh_wr(b+RH_CTRL,(1u<<16)|(1u<<24));
 uint32_t st=0;
 for (unsigned us=0;us<100000;us++) {
  st=rh_rd(b+RH_CSTAT);
  if (rh_rd(b+RH_IDLE) && (st&0x10100)==0x10100) break;
  delay(1);
 }
 if ((st&0x10100)!=0x10100) return 2|(st<<12); /* leave TCS quarantined */
 rh_wr(b+RH_CTRL,1u<<16); rh_wr(b+RH_CTRL,0);
 rh_wr(b+RH_EN,0); rh_wr(b+RH_WAIT,0);
 rh_wr(8,bit);
 rh_wr(0,(rh_rd(0)&~bit)|(irq_en&bit));
 return 0;
}
static uint32_t rcg_set(uint32_t rcgr, uint32_t cfg)
{
 *(volatile uint32_t *)(0xaf00000UL+rcgr+4)=cfg;
 __asm__ volatile("dsb sy" ::: "memory");
 *(volatile uint32_t *)(0xaf00000UL+rcgr)=usb_rd(0xaf00000,rcgr)|1;
 __asm__ volatile("dsb sy" ::: "memory");
 for (unsigned i=0;i<1000;i++) { if (!(usb_rd(0xaf00000,rcgr)&1)) return 0; delay(1); }
 return 1;
}
/* Results: [0] stage, [1..6] RPMh status, [7] PLL mode, [8] L, [9] MDP CFG, [10] latch mode. */
static uint32_t boost_report[12];
#define BOOST_CMDS 6
static void display_boost(void)
{
 static const uint32_t cmds[][2]={
  {0x30080,4},                                /* mmcx.lvl hlvl 4 = NOM (vlvl 256) */
  {0x50030,(3u<<29)|(0x3fffu<<14)|0x3fff},     /* MM0 commit|valid, max x/y */
  {0x50034,(3u<<29)|(0x3fffu<<14)|0x3fff},     /* MM1 */
  {0x50004,(3u<<29)|(0x3fffu<<14)|0x3fff},     /* SH0 */
  {0x50068,(3u<<29)|(8u<<14)|8u},             /* ACV HLOS enable mask */
  {0x50000,(3u<<29)|(0x3fffu<<14)|0x3fff}};    /* MC0 */
 for (unsigned i=0;i<12;i++) boost_report[i]=0;
 uint32_t id=usb_rd(RH_DRV_PA,0), cfg=usb_rd(RH_DRV_PA,0xc);
 if (((id>>16)&0xff)!=3 || ((cfg>>12)&0x3f)<3) { boost_report[0]=0x100|((id>>16)&0xff); return; }
 boost_report[0]=1;
 for (unsigned i=0;i<BOOST_CMDS;i++) {
  boost_report[1+i]=rh_send(0,cmds[i][0],cmds[i][1]);
  if (boost_report[1+i]) return;
 }
 boost_report[0]=2;
 uint32_t old=usb_rd(0x100000,0x27004);
 *(volatile uint32_t *)0x127004UL=old&~2u;
 __asm__ volatile("dsb sy" ::: "memory");
 for(unsigned i=0;i<500 && (usb_rd(0x100000,0x27004)&0x80000000);i++) delay(20);
 /* Park MDP on XO, retune PLL0 975 -> 1542 MHz (L 0x50, alpha 0x5000), back to /3. */
 if (rcg_set(0x80d8,0x0)) { boost_report[0]=0x200; goto out; }
 *(volatile uint32_t *)0xaf00010UL=(usb_rd(0xaf00000,0x10)&0xffff0000u)|0x50;
 *(volatile uint32_t *)0xaf00014UL=0x5000;
 __asm__ volatile("dsb sy" ::: "memory");
 *(volatile uint32_t *)0xaf00000UL=usb_rd(0xaf00000,0)|(1u<<14);
 __asm__ volatile("dsb sy" ::: "memory");
 delay(2);
 boost_report[10]=usb_rd(0xaf00000,0);
 *(volatile uint32_t *)0xaf00000UL=usb_rd(0xaf00000,0)&~(1u<<14);
 __asm__ volatile("dsb sy" ::: "memory");
 for (unsigned i=0;i<200 && !(usb_rd(0xaf00000,0)&0x80000000u);i++) delay(5);
 delay(100);
 boost_report[0]=3;
 if (!(usb_rd(0xaf00000,0)&0x80000000u) || !(boost_report[10]&(1u<<13))) { boost_report[0]=0x300; goto out; }
 if (rcg_set(0x80d8,0x105)) { boost_report[0]=0x400; goto out; }
 boost_report[0]=4;
out:
 boost_report[7]=usb_rd(0xaf00000,0); boost_report[8]=usb_rd(0xaf00000,0x10);
 boost_report[9]=usb_rd(0xaf00000,0x80dc);
 *(volatile uint32_t *)0x127004UL=old;
 __asm__ volatile("dsb sy" ::: "memory");
}
/*
 * Touch: ST FTS (FTM5) on QUP1 SE4 SPI, see research/touch/hw-report.md.
 * Live FDT: SE4 0xa90000, reset GPIO24, IRQ GPIO25 (active low), SPI pins
 * 44-47 func 1, supplies ldob12 (vdd 1.8 V) and ldom6 (avdd 3.2 V) on RPMh.
 * Request 0x55: 'TS' read-only snapshot, 'TP' power + reset + chip ID +
 * sense on, 'TE' read one FIFO batch (polled; no GIC/PDC interrupt work).
 */
#define TS_SE   0x00a90000UL
#define TS_GCC  0x00100000UL
#define TS_TLMM 0x0f100000UL /* DT node 0x0f000000 + downstream tile 0x100000 = upstream tlmm@f100000 (ts-03) */
static uint32_t touch_report[64];
static uint8_t touch_events[8*32];
static unsigned ts_depth;
static uint32_t ts_level;
static unsigned ts_stage;
static void ts_wr(uintptr_t base, uint32_t off, uint32_t v)
{
 __asm__ volatile("dmb oshst" ::: "memory");
 *(volatile uint32_t *)(base+off)=v;
 __asm__ volatile("dsb osh" ::: "memory");
}
/* TLMM CTL: pull [1:0] (1 down, 3 up), func [5:2], drive [8:6] = mA/2-1, OE [9]. */
static void ts_gpio(unsigned n, unsigned func, unsigned pull, unsigned ma, int out)
{
 uintptr_t c=TS_TLMM+0x1000u*n;
 if (out>=0) ts_wr(c,4,out?2u:0u);
 ts_wr(c,0,(usb_rd(c,0)&~0x3ffu)|pull|(func<<2)|((ma/2-1)<<6)|(out>=0?0x200u:0u));
}
/* Lane-gpu (milestone 1): staged A740 power/clock/cmd-db probe on request 0x56.
 * Kept in its own single-TU file so other lanes' probe.c edits stay separate. */
#include "gpu.c"
/* Lane-audio (milestone 1): snapshot, firmware staging and ADSP PAS boot on
 * requests 0x5a/0x5b; see audio.c and docs-lanes/audio.md. */
#include "audio.c"
/* Lane-wlan: read-only WCN7850/PCIe0/BT-UART snapshot on request 0x5c (wlan.c). */
#include "wlan.c"

/* SE4 and wrapper registers hang the bus (SoC reset) while wrapper1's AHB
 * clocks are gated: touch them only with m_ahb running (s_ahb is under
 * hardware clock gating, its CLK_OFF bit is not meaningful at idle). */
static bool ts_ahb_on(void) { return !(usb_rd(TS_GCC,0x2327c)&0x80000000u); }
static bool ts_s4_on(void) { return !(usb_rd(TS_GCC,0x184e4)&0x80000000u); }
/* Groups (ts-02 bisect; any abort resets the SoC): 0 GCC, 1 TLMM, 2 SE4 + QUP
 * version (only with the wrapper clocks on), 3 a single GCC word (index). */
static void touch_snapshot(unsigned group)
{
 static const uint16_t se[]={0x040,0x048,0x064,0x068,0x07c,0x258,0x25c,0x268,
  0x260,0x264,0x610,0x614,0x800,0x804,0xe24,0xe28};
 static const uint8_t g[]={24,25,44,45,46,47};
 unsigned n=1;
 for (unsigned i=0;i<64;i++) touch_report[i]=0;
 touch_report[0]=0x100|group;
 if (group==0) {
  touch_report[n++]=usb_rd(TS_GCC,0x52008);
  touch_report[n++]=usb_rd(TS_GCC,0x184e4);
  touch_report[n++]=usb_rd(TS_GCC,0x2327c);
  touch_report[n++]=usb_rd(TS_GCC,0x23280);
  touch_report[n++]=usb_rd(TS_GCC,0x184f0);
  touch_report[n++]=usb_rd(TS_GCC,0x184f4);
  touch_report[n++]=usb_rd(TS_GCC,0x18504);
  for (unsigned l=0;l<8;l++) touch_report[n++]=usb_rd(TS_GCC,0x1850c+4*l);
 } else if (group==1) {
  for (unsigned i=0;i<6;i++) {
   touch_report[n++]=usb_rd(TS_TLMM+0x1000u*g[i],0);
   touch_report[n++]=usb_rd(TS_TLMM+0x1000u*g[i],4);
  }
 } else if (group==2 && ts_ahb_on()) {
  for (unsigned i=0;i<16;i++) touch_report[n++]=usb_rd(TS_SE,se[i]);
  touch_report[n++]=usb_rd(0x00ac0000,4); /* QUP_HW_VER */
 } else if (group==4 && ts_ahb_on()) {
  /* GPII 0 (read-only): CH0, CH1, EV contexts, TYPE_IRQ, ERROR_LOG, CH0 SCRATCH_0. */
  static const uint32_t r[]={0xa10000,0xa10004,0xa10080,0xa11000,0xa11010,0xa13080,
   0xa13100,0xa13118,0xa13200,0xa10060};
  for (unsigned i=0;i<10;i++) touch_report[n++]=usb_rd(r[i],0);
 } else if (group==5) {
  /* apps-smmu@15000000 GR0, read-only: sCR0, sIDR0/1, sGFSR, sGFSYNR0/1, then
   * the SMR/S2CR pairs that match the GPI (0xb6), QUP1 wrapper (0xa3), UFS
   * (0x60) and DWC3 (0x40) streams: (index | sid<<16), SMR, S2CR. */
  static const uint16_t sid[]={0xb6,0xa3,0x60,0x40};
  const uintptr_t sm=0x15000000UL;
  uint32_t idr0=usb_rd(sm,0x20), nsmr=idr0&0xff, valid=0;
  touch_report[n++]=usb_rd(sm,0); touch_report[n++]=idr0; touch_report[n++]=usb_rd(sm,0x24);
  touch_report[n++]=usb_rd(sm,0x48); touch_report[n++]=usb_rd(sm,0x50); touch_report[n++]=usb_rd(sm,0x54);
  n++;
  for (unsigned i=0;i<nsmr && i<256;i++) {
   uint32_t smr=usb_rd(sm,0x800+4*i);
   if (!(smr&0x80000000u)) continue;
   valid++;
   uint32_t id=smr&0x7fff, mask=(smr>>16)&0x7fff;
   for (unsigned j=0;j<4 && n<61;j++) if (((sid[j]^id)&~mask&0x7fff)==0) {
    touch_report[n++]=i|(uint32_t)sid[j]<<16; touch_report[n++]=smr; touch_report[n++]=usb_rd(sm,0xc00+4*i);
   }
  }
  touch_report[7]=valid|n<<16;
  /* Context banks used by UFS (0) and DWC3 (4): CBAR, SCTLR, TTBR0 lo. */
  const uintptr_t cb=sm+((uintptr_t)1<<((((usb_rd(sm,0x24)>>28)&7)+1)))*((usb_rd(sm,0x24)>>31)?0x10000:0x1000);
  for (unsigned k=0;k<5;k+=4) if (n<61) {
   touch_report[n++]=usb_rd(sm,0x1000+4*k);
   touch_report[n++]=usb_rd(cb+0x1000*k,0); touch_report[n++]=usb_rd(cb+0x1000*k,0x20);
  }
 }
}
static void ts_clock_regs(unsigned at)
{
 static const uint32_t r[]={0x52008,0x184e4,0x2327c,0x23280,0x23284,0x23294,0x184f0};
 for (unsigned i=0;i<7;i++) touch_report[at+i]=usb_rd(TS_GCC,r[i]);
}
static uint32_t ts_clock(void)
{
 /* Vote wrapper1 core/core2x/m_ahb/s_ahb and SE4 only. */
 ts_wr(TS_GCC,0x52008,usb_rd(TS_GCC,0x52008)|(0xfu<<18)|(1u<<26));
 for (unsigned i=0;i<10000 && !ts_ahb_on();i++) delay(1);
 ts_clock_regs(20);
 if (!ts_ahb_on()) return 0x10;
 /* S4 is a DFS RCG under hardware control: its root comes up when the SE
  * requests a level. SE_GENI_CLK_SEL picks one sourced from XO, undivided
  * (19.2 MHz); the SE divides by 2: 9.6 MHz SCLK (driver max 12). */
 unsigned level=0;
 if (usb_rd(TS_GCC,0x18504)&1) {
  for (level=0;level<8 && (usb_rd(TS_GCC,0x1850c+4*level)&0x71fu)>1u;level++);
  if (level==8) return 0x11;
 } else if ((usb_rd(TS_GCC,0x184f4)&0x71fu)>1u) return 0x12;
 ts_level=level;
 ts_wr(TS_SE,0x07c,level); ts_wr(TS_SE,0x048,(2u<<4)|1u);
 for (unsigned i=0;i<10000 && !ts_s4_on();i++) delay(1);
 ts_clock_regs(27);
 touch_report[34]=usb_rd(TS_SE,0x048); touch_report[35]=usb_rd(TS_SE,0x07c);
 return ts_s4_on() ? 0 : 0x13;
}
/* SE4 is GSI-only (GENI_IF_DISABLE_RO=1, ts-05): SPI goes through QUP1 GPI
 * DMA, static GPII 0, TX channel 0, RX channel 1, one event ring
 * (research/touch/gpi-recipe.md). Polled; with the MMU off the rings and
 * buffers are uncached and VA == PA. */
#define GPI_EE      0x009f0000UL
#define GPI_CH(k)   (GPI_EE+0x20000UL+0x80UL*(k))
#define GPI_EV      (GPI_EE+0x21000UL)
#define GPI_CHDB(k) (GPI_EE+0x22000UL+8UL*(k))
#define GPI_EVDB    (GPI_EE+0x22100UL)
#define GPI_GPII    (GPI_EE+0x23000UL)
#define GPI_RING    64u
#define GPI_EVRING  256u
static uint32_t gpi_ring[2][GPI_RING*4] __attribute__((aligned(1024)));
static uint32_t gpi_evr[GPI_EVRING*4] __attribute__((aligned(4096)));
static uint8_t gpi_txb[64] __attribute__((aligned(64)));
static uint8_t gpi_rxb[8+1+8*32] __attribute__((aligned(64)));
static unsigned gpi_wp[2], gpi_evrp, gpi_nev;
static uint32_t lo32(const void *p) { return (uint32_t)(uintptr_t)p; }
static uint32_t gpi_state(uintptr_t ctx) { return (usb_rd(ctx,0)>>20)&0xf; }
static void gpi_diag(void)
{
 touch_report[40]=usb_rd(GPI_CH(0),0); touch_report[41]=usb_rd(GPI_CH(1),0);
 touch_report[42]=usb_rd(GPI_EV,0); touch_report[43]=usb_rd(GPI_GPII,0x80);
 touch_report[44]=usb_rd(GPI_GPII,0x200); touch_report[45]=usb_rd(GPI_GPII,0x100);
 touch_report[46]=usb_rd(GPI_GPII,0x118); touch_report[47]=gpi_nev;
 touch_report[53]=usb_rd(GPI_CH(0),0x10); touch_report[54]=usb_rd(GPI_CH(1),0x10);
 touch_report[55]=usb_rd(GPI_EV,0x10);
}
static bool gpi_cmd(uint32_t reg, uint32_t v, uintptr_t ctx, uint32_t want)
{
 ts_wr(GPI_GPII,reg,v);
 for (unsigned i=0;i<50000;i++) {
  uint32_t s=gpi_state(ctx);
  if (s==want) {
   ts_wr(GPI_GPII,0xa0,usb_rd(GPI_GPII,0x90)); ts_wr(GPI_GPII,0xa4,usb_rd(GPI_GPII,0x94));
   return true;
  }
  if (s==0xf) break;
  delay(1);
 }
 return false;
}
/* apps-smmu faults unmatched streams (sCR0.USFCFG, ts-10) and has no SMR
 * for GPI (0xb6) or the QUP1 wrapper (0xa3). The hypervisor turns an S2CR
 * bypass into fault (ts-11), so route both to context bank 4, which DWC3
 * uses with stage 1 disabled (SCTLR.M=0). */
static uint32_t ts_smmu(void)
{
 static const uint16_t sid[]={0xb6,0xa3};
 const uintptr_t sm=0x15000000UL;
 uint32_t nsmr=usb_rd(sm,0x20)&0xff;
 for (unsigned j=0;j<2;j++) {
  unsigned i, freei=~0u;
  for (i=0;i<nsmr;i++) {
   uint32_t smr=usb_rd(sm,0x800+4*i);
   if (!(smr&0x80000000u)) { if (freei==~0u) freei=i; continue; }
   if (((sid[j]^smr)&~(smr>>16)&0x7fff)==0) break;
  }
  if (i<nsmr) { touch_report[16+j]=0x1000|i; continue; }
  if (freei==~0u) return 0x38;
  ts_wr(sm,0xc00+4*freei,4);                     /* S2CR translate, CB4 */
  ts_wr(sm,0x800+4*freei,0x80000000u|sid[j]);
  touch_report[16+j]=freei|usb_rd(sm,0x800+4*freei)<<8;
  touch_report[18+j]=usb_rd(sm,0xc00+4*freei);
 }
 touch_report[15]=usb_rd(sm,0x48);
 return 0;
}
static uint32_t gpi_init(void)
{
 if (ts_stage>=2) { uint32_t e=ts_smmu(); if (e) return e; }
 touch_report[36]=gpi_state(GPI_CH(0))|gpi_state(GPI_CH(1))<<4|gpi_state(GPI_EV)<<8;
 touch_report[37]=lo32(gpi_ring[0]); touch_report[38]=lo32(gpi_evr);
 if ((uintptr_t)gpi_ring[0]>>32 || (uintptr_t)gpi_evr>>32) return 0x30;
 /* A channel left over from an earlier stage: stop, reset, free. */
 for (unsigned k=0;k<2;k++) {
  if (gpi_state(GPI_CH(k))==2) gpi_cmd(0x08,(2u<<24)|k,GPI_CH(k),3);
  if (gpi_state(GPI_CH(k))) { gpi_cmd(0x08,(9u<<24)|k,GPI_CH(k),1); ts_wr(GPI_GPII,0x08,(10u<<24)|k); delay(1000); }
 }
 if (gpi_state(GPI_EV)) { gpi_cmd(0x10,9u<<24,GPI_EV,1); ts_wr(GPI_GPII,0x10,10u<<24); delay(1000); }
 memset(gpi_ring,0,sizeof(gpi_ring)); memset(gpi_evr,0,sizeof(gpi_evr));
 /* Interrupt masks as gpi_config_interrupts(DEFAULT); the GIC line stays off. */
 if (ts_stage<3) return 0;
 ts_wr(GPI_GPII,0x88,0x4f); ts_wr(GPI_GPII,0xb8,1); ts_wr(GPI_GPII,0x98,3);
 ts_wr(GPI_GPII,0x9c,1); ts_wr(GPI_GPII,0x108,1); ts_wr(GPI_GPII,0x120,0xf);
 ts_wr(GPI_GPII,0x188,0); ts_wr(GPI_GPII,0x18c,0);
 ts_wr(GPI_GPII,0x400,0); ts_wr(GPI_GPII,0x404,0);
 ts_wr(GPI_GPII,0x180,1); ts_wr(GPI_GPII,0x200,0);
 if (ts_stage<4) return 0;
 if (!gpi_cmd(0x10,0,GPI_EV,1)) return 0x31;
 if (ts_stage<5) return 0;
 ts_wr(GPI_EV,0x00,0x10010002u); ts_wr(GPI_EV,0x04,sizeof(gpi_evr));
 ts_wr(GPI_EV,0x08,lo32(gpi_evr)); ts_wr(GPI_EV,0x0c,0); ts_wr(GPI_EVDB,4,0);
 for (unsigned o=0x20;o<=0x34;o+=4) ts_wr(GPI_EV,o,0);
 gpi_evrp=0; gpi_nev=0;
 if (ts_stage<6) return 0;
 ts_wr(GPI_EVDB,0,lo32(gpi_evr)+sizeof(gpi_evr)-16);
 if (ts_stage<7) return 0;
 for (unsigned k=0;k<2;k++) {
  if (!gpi_cmd(0x08,k,GPI_CH(k),1)) return 0x32+k;
  ts_wr(GPI_CH(k),0x00,k?0x10000002u:0x1000000au); ts_wr(GPI_CH(k),0x04,sizeof(gpi_ring[k]));
  ts_wr(GPI_CH(k),0x08,lo32(gpi_ring[k])); ts_wr(GPI_CH(k),0x0c,0); ts_wr(GPI_CHDB(k),4,0);
  ts_wr(GPI_CH(k),0x60,k?0x00000014u:0x00010014u);
  ts_wr(GPI_CH(k),0x64,0); ts_wr(GPI_CH(k),0x68,0); ts_wr(GPI_CH(k),0x6c,0);
  ts_wr(GPI_CH(k),0x5c,1);
  gpi_wp[k]=0;
 }
 if (ts_stage<8) return 0;
 for (unsigned k=0;k<2;k++) if (!gpi_cmd(0x08,(1u<<24)|k,GPI_CH(k),2)) return 0x34+k;
 return 0;
}
static void gpi_tre(unsigned k, uint32_t d0, uint32_t d1, uint32_t d2, uint32_t d3)
{
 volatile uint32_t *t=&gpi_ring[k][4*gpi_wp[k]];
 t[0]=d0; t[1]=d1; t[2]=d2; t[3]=d3;
 gpi_wp[k]=(gpi_wp[k]+1)%GPI_RING;
}
static void gpi_ring_db(unsigned k)
{
 __asm__ volatile("dsb sy" ::: "memory");
 ts_wr(GPI_CHDB(k),0,lo32(&gpi_ring[k][4*gpi_wp[k]]));
}
/* Drain events until one from chid with code (2 EOT) arrives. Returns its
 * length + 1, 0 on timeout, or ~0 on an error event. */
static uint32_t gpi_wait(unsigned chid, uint32_t *len)
{
 for (unsigned us=0;us<5000;us++) {
  uint32_t hw=usb_rd(GPI_EV,0x10);
  bool found=false, bad=false;
  while (hw!=lo32(&gpi_evr[4*gpi_evrp])) {
   volatile uint32_t *e=&gpi_evr[4*gpi_evrp];
   uint32_t d[4]={e[0],e[1],e[2],e[3]};
   if (!gpi_nev) memcpy(&touch_report[56],d,16);
   memcpy(&touch_report[48],d,16);
   gpi_nev++;
   gpi_evrp=(gpi_evrp+1)%GPI_EVRING;
   unsigned code=d[2]>>24, type=(d[3]>>16)&0xff, ch=d[3]>>24;
   if (code==16 || type==0xff || ch>=2) bad=true;
   else if (type==0x22 && ch==chid && code==2) { found=true; *len=d[2]&0xffffff; }
  }
  ts_wr(GPI_EVDB,0,lo32(&gpi_evr[4*((gpi_evrp+GPI_EVRING-1)%GPI_EVRING)]));
  ts_wr(GPI_GPII,0xc0,1);
  if (bad) return 2;
  if (found) return 0;
  delay(1);
 }
 return 1;
}
#define GPI_C0()        gpi_tre(0,0x03000004u,0,(ts_level<<16)|2u,0x00220001u)
#define GPI_GO(cmd,frag,rxl,d3) gpi_tre(0,((frag)?(1u<<26):0)|(cmd),0,(rxl),(d3))
#define GPI_DMA(k,buf,n,d3)     gpi_tre((k),lo32(buf),0,(n),(d3))
static uint32_t gpi_tx_only(const uint8_t *tx, unsigned nt, bool frag)
{
 uint32_t len=0, e;
 memcpy(gpi_txb,tx,nt);
 GPI_C0(); GPI_GO(1,frag,0,0x00200001u); GPI_DMA(0,gpi_txb,nt,0x00100200u);
 gpi_ring_db(0);
 if ((e=gpi_wait(0,&len))) return 0x40|e;
 return len==nt ? 0 : 0x48;
}
/* FTS read: opcode + address (CS held), then one dummy byte and n data bytes. */
static uint32_t ts_read(const uint8_t *cmd, unsigned nc, uint8_t *out, unsigned n)
{
 uint32_t len=0, e;
 if (nc>8 || n>8*32) return 3;
 if ((e=gpi_tx_only(cmd,nc,true))) { gpi_diag(); return e; }
 memset(gpi_rxb,0,n+1);
 GPI_DMA(1,gpi_rxb,n+1,0x00100200u);
 GPI_GO(2,false,n+1,0x00200900u);
 gpi_ring_db(1); gpi_ring_db(0);
 if ((e=gpi_wait(1,&len))) { gpi_diag(); return 0x50|e; }
 touch_report[52]=len;
 memcpy(out,gpi_rxb+1,n);
 return 0;
}
static uint32_t ts_cmd(const uint8_t *cmd, unsigned nc)
{
 uint32_t e=nc>8 ? 3 : gpi_tx_only(cmd,nc,false);
 if (e) gpi_diag();
 return e;
}
static uint32_t ts_se_init(void)
{
 uint32_t fw=usb_rd(TS_SE,0x068), hw=usb_rd(TS_SE,0xe24);
 if (((fw>>8)&0xff)!=1) return 0x20;            /* not SPI firmware */
 if (usb_rd(TS_SE,0x040)&1) return 0x22;
 ts_depth=(hw>>16)&0x3f;
 /* geni_se_select_mode(GENI_GPI_DMA): mode, word size, packing and clock
  * then come from the CONFIG0 TRE. */
 ts_wr(TS_SE,0xe18,0);
 ts_wr(TS_SE,0x618,0xffffffffu); ts_wr(TS_SE,0x648,0xffffffffu);
 ts_wr(TS_SE,0xc44,0xffffffffu); ts_wr(TS_SE,0xd44,0xffffffffu);
 ts_wr(TS_SE,0x028,usb_rd(TS_SE,0x028)|0x7f);
 ts_wr(TS_SE,0xe30,usb_rd(TS_SE,0xe30)|0xf);
 ts_wr(TS_SE,0x024,0x7f); ts_wr(TS_SE,0x020,1);
 ts_wr(TS_SE,0xe1c,0);
 ts_wr(TS_SE,0x614,usb_rd(TS_SE,0x614)&~((1u<<0)|(1u<<26)|(1u<<27)|(1u<<30)));
 ts_wr(TS_SE,0x644,usb_rd(TS_SE,0x644)&~1u);
 ts_wr(TS_SE,0x258,1);
 ts_wr(TS_SE,0xe18,usb_rd(TS_SE,0xe18)|0xf);
 touch_report[39]=usb_rd(TS_SE,0x064);
 return ts_stage<2 ? 0 : gpi_init();
}
static void touch_power(unsigned stage)
{
 static const uint8_t id_cmd[]={0xfb,0x20,0x00,0x00,0x00}, fifo[]={0x87}, sense[]={0xa0,0x00,0x01};
 for (unsigned i=0;i<64;i++) touch_report[i]=0;
 ts_stage=stage;
 touch_report[0]=1;
 uint32_t id=usb_rd(RH_DRV_PA,0), cfg=usb_rd(RH_DRV_PA,0xc);
 if (((id>>16)&0xff)!=3 || ((cfg>>12)&0x3f)<3) { touch_report[0]=0x100; return; }
 /* Hold the IC in reset (and IRQ low, as the vendor driver does) while the rails come up. */
 ts_gpio(24,0,3,8,0); ts_gpio(25,0,3,8,0);
 /* VRM: +0 voltage mV, +4 enable. vdd first, then avdd. */
 static const uint32_t cmds[][2]={{0x40900,1800},{0x40904,1},{0x41b00,3200},{0x41b04,1}};
 for (unsigned i=0;i<4;i++) {
  touch_report[1+i]=rh_send(0,cmds[i][0],cmds[i][1]);
  if (touch_report[1+i]) { touch_report[0]=0x200|i; return; }
 }
 delay(3000);
 ts_gpio(25,0,3,8,-1);
 ts_gpio(24,0,3,8,1);
 ts_gpio(44,1,1,6,-1); ts_gpio(45,1,1,6,-1); ts_gpio(46,1,1,6,-1); ts_gpio(47,1,3,6,-1);
 touch_report[0]=2;
 if ((touch_report[5]=ts_clock())) return;
 touch_report[0]=3;
 if ((touch_report[6]=ts_se_init())) return;
 touch_report[0]=4;
 touch_report[7]=ts_depth;
 if (ts_stage<9) { gpi_diag(); return; }
 /* fts_system_reset: reset low 10 ms, high, then poll the FIFO for 03 (ready). */
 ts_gpio(24,0,3,8,0); delay(10000); ts_gpio(24,0,3,8,1);
 uint8_t ev[8]={0};
 for (unsigned t=0;t<100;t++) {
  delay(2000);
  touch_report[8]=ts_read(fifo,1,ev,8);
  if (touch_report[8]) break;
  if (ev[0]==0x03) break;
 }
 memcpy(&touch_report[9],ev,8);
 uint8_t chip[2]={0};
 touch_report[11]=ts_read(id_cmd,5,chip,2);
 touch_report[12]=((uint32_t)chip[0]<<8)|chip[1];
 if (touch_report[8] || touch_report[11]) return;
 touch_report[0]=5;
 touch_report[13]=ts_cmd(sense,3);
 if (!touch_report[13]) touch_report[0]=6;
 touch_report[14]=usb_rd(TS_TLMM+0x19000,4);
}
/* One FIFO batch: first event, then the remaining count from byte 7. */
static unsigned touch_read_events(void)
{
 static const uint8_t fifo[]={0x87};
 if (touch_report[0]!=6) return 0;
 unsigned n=0;
 for (unsigned i=0;i<32;) {
  uint8_t *e=touch_events+8*i;
  if (ts_read(fifo,1,e,8) || e[0]==0) break;
  i++; n=i;
  if (!(e[7]&0x1f)) break;
 }
 return n*8;
}
/* SM8550 cpu@100 from the live FDT. CPU_ON is only requested by the host. */
static volatile uint64_t core_report[10] __attribute__((aligned(64)));
void probe_secondary_main(void)
{
 core_report[4]=rsr("midr_el1"); core_report[5]=rsr("mpidr_el1");
 core_report[6]=rsr("CurrentEL"); core_report[7]=rsr("sctlr_el1");
 core_report[3]=1;
 __asm__ volatile("dsb sy" ::: "memory");
 for (;;) {
  core_report[8]++;
  core_report[9]=rsr("cntpct_el0");
  __asm__ volatile("dsb sy" ::: "memory");
  if (monitor_report[MON_DELEGATE] == 2) {
   monitor_report[MON_ACTIVE]=1;
   __asm__ volatile("dsb sy" ::: "memory");
   usb_poll();
   thermal_monitor_idle(); /* thermal lane: no-op unless 0x57 'MI' selected a mode */
  } else delay(1000);
  if (monitor_report[MON_DEADLINE] && rsr("cntpct_el0") >= monitor_report[MON_DEADLINE]) reboot();
 }
}
static void core_probe(void)
{
 extern char probe_secondary_entry[];
 if(core_report[0]) return;
 core_report[0]=0x3145524f43495846ULL; /* FXICORE1 */
 core_report[1]=0x100;
 __asm__ volatile("dsb sy; ic iallu; dsb sy; isb" ::: "memory");
 register uint64_t fn __asm__("x0")=0xc4000003;
 register uint64_t target __asm__("x1")=0x100;
 register uint64_t entry __asm__("x2")=(uintptr_t)probe_secondary_entry;
 register uint64_t context __asm__("x3")=0;
 __asm__ volatile("smc #0" : "+r"(fn), "+r"(target), "+r"(entry), "+r"(context) ::
  "x4","x5","x6","x7","x8","x9","x10","x11","x12","x13","x14","x15","x16","x17","memory");
 core_report[2]=fn;
 delay(20000);
}
#define GCTL            0xc110
#define GEVNTADRLO      0xc400
#define GEVNTADRHI      0xc404
#define GEVNTSIZ        0xc408
#define GEVNTCOUNT      0xc40c
#define DCFG            0xc700
#define DCTL            0xc704
#define DEVTEN          0xc708
#define DSTS            0xc70c
#define DALEPENA        0xc720
#define DEPCMDPAR2(n)   (0xc800 + 0x10 * (n))
#define DEPCMDPAR1(n)   (0xc804 + 0x10 * (n))
#define DEPCMDPAR0(n)   (0xc808 + 0x10 * (n))
#define DEPCMD(n)       (0xc80c + 0x10 * (n))

/*
 * Qualcomm wrapper registers ("QSCRATCH", core base + 0xf8800). The
 * controller only connects (pull-up on D+) while it sees a valid VBUS
 * session; Linux dwc3-qcom forces that in software when the charger
 * reports a cable (dwc3_qcom_vbus_override_enable).
 */
#define QSCRATCH_PA             0x0a6f8800
#define QSCRATCH_HS_PHY_CTRL    0x10
#define   UTMI_OTG_VBUS_VALID   (1u << 20)
#define   SW_SESSVLD_SEL        (1u << 28)
#define QSCRATCH_SS_PHY_CTRL    0x30
#define   LANE0_PWR_PRESENT     (1u << 24)

#define DCTL_RUN_STOP   (1u << 31)
#define DCTL_CSFTRST    (1u << 30)
#define DSTS_DEVCTRLHLT (1u << 22)

#define DEPCMD_SETEPCONFIG      1
#define DEPCMD_SETTRANSFRESOURCE 2
#define DEPCMD_SETSTALL         4
#define DEPCMD_STARTTRANSFER    6
#define DEPCMD_DEPSTARTCFG      9
#define DEPCMD_CMDACT           (1u << 10)

#define TRB_HWO         (1u << 0)
#define TRB_LST         (1u << 1)
#define TRB_ISP_IMI     (1u << 10)
#define TRB_IOC         (1u << 11)
#define TRBCTL_SETUP    (2u << 4)
#define TRBCTL_STATUS2  (3u << 4)
#define TRBCTL_STATUS3  (4u << 4)
#define TRBCTL_DATA     (5u << 4)

/* DMA area layout */
#define UD_EVT          0x0000          /* event buffer, 4 KiB */
#define UD_EVT_SIZE     0x1000
#define UD_TRB_OUT      0x1000          /* ep0 OUT TRB */
#define UD_TRB_IN       0x1010          /* ep0 IN TRB */
#define UD_SETUP        0x1040          /* 8-byte setup packet */
#define UD_DATA         0x1100          /* ep0 data stage, 512 bytes */
#define UD_SIZE         0x4000

static struct {
	uintptr_t dwc;
	uintptr_t dma;
	uint64_t    dma_pa;
	uint32_t    evt_pos;
	enum { EP0_SETUP, EP0_DATA, EP0_WAIT_STATUS2, EP0_WAIT_STATUS3, EP0_STATUS } state;
	bool        data_in;        /* data stage direction */
	bool        audio_out;      /* OUT data stage carries an audio staging chunk */
	bool        remote_out;     /* OUT data stage carries one remote command */
	uint64_t    remote_off;     /* where in the XNU window the host says the kernel's mailbox is */
	uint16_t    out_index, out_length;
	uint32_t    events;
} ud;

static inline void
dwc_wr(uint32_t off, uint32_t v)
{
	*(volatile uint32_t *)(ud.dwc + off) = v;
	__asm__ volatile ("dsb sy" ::: "memory");
}

static inline uint32_t
dwc_rd(uint32_t off)
{
	return usb_rd(ud.dwc, off);
}

/* ===== Thermal lane (request 0x57): TSENS readout, EPSS CPU level, monitor idle =====
 * Addresses from the live FDT (probe-09 live.dtb):
 *  tsens0/1/2 (qcom,tsens-v2): TM 0xc271000/0xc272000/0xc273000, SROT 0xc222000/
 *  0xc223000/0xc224000 (0x1ff each), 16/16/9 sensors. Linux tsens-v2 layout:
 *  TM_Sn_STATUS 0xa0+4n (bits 11:0 signed deci-degC, bit 21 VALID), TM_TRDY 0xe4,
 *  SROT 0x0 HW version, 0x4 CTRL (bit 0 enable, bits 18:3 sensor enable).
 *  qcom,cpufreq-epss freq-domain0/1/2 at 0x17d91000/0x17d92000/0x17d93000 (0x1000):
 *  domain 0 = cpu@0-200 (A510), 1 = cpu@300-600 (A715/A710), 2 = cpu@700 (X3).
 *  Linux epss_soc_data: 0x0 enable, 0x20 domain state (bits 7:0 L value),
 *  0xb0 DCVS ctrl (bit 0 per-core votes), 0x100 freq LUT, 0x200 volt LUT,
 *  0x320 perf state (+4*core with per-core votes). LUT row: 31:30 src, 18:16 core
 *  count (1 = turbo), 7:0 L (x 19.2 MHz); src 0 = GPLL0/2 300 MHz.
 * Read-only except 'CL' (perf-state index write, the same register Linux writes) and
 * 'MI' (monitor-idle mode of cpu@100). Nothing here touches state XNU uses.
 *  'TH' (0x5448) wIndex mask: 1/2/4 TM of tsens0/1/2, 8 SROT of all three, 0x10 EPSS state.
 *  'LU' (0x4c55) wIndex domain: 40 freq + 40 volt LUT rows.
 *  'CL' (0x434c) wIndex = domain | level<<8 (level 0xff: report only).
 *  'MI' (0x4d49) wIndex mode: 0 busy poll (default), 1 delay 500 us, 2 WFE + event stream.
 */
static const uintptr_t th_tm[3]={0xc271000,0xc272000,0xc273000};
static const uintptr_t th_srot[3]={0xc222000,0xc223000,0xc224000};
static const uint32_t th_nsens[3]={16,16,9};
static const uintptr_t th_epss[3]={0x17d91000,0x17d92000,0x17d93000};
static const uint32_t th_ncpu[3]={3,4,1};
static uint32_t th_report[128];
static volatile uint32_t th_idle_mode, th_idle_evnt;
static uint32_t th_idle_events;
static uint64_t th_idle_busy_until;
/* Number of distinct LUT rows (Linux: two equal frequencies end the table). */
static uint32_t th_lut_count(uintptr_t b)
{
 uint32_t prev=0, i;
 for (i=0;i<40;i++) {
  uint32_t row=usb_rd(b,0x100+4*i);
  uint32_t khz=(row>>30) ? 19200*(row&0xff) : 300000;
  if (i && khz==prev) break;
  prev=khz;
 }
 return i;
}
static uint32_t thermal_request(uint16_t value, uint16_t index)
{
 uint32_t n=0;
 memset(th_report,0,sizeof(th_report));
 if (value==0x5448) {
  uint64_t t=rsr("cntpct_el0");
  th_report[n++]=0x31524854; /* THR1 */
  th_report[n++]=index; th_report[n++]=(uint32_t)t; th_report[n++]=(uint32_t)(t>>32);
  for (unsigned c=0;c<3;c++) {          /* 3 x 20 words */
   uint32_t *w=&th_report[n]; n+=20;
   if (index&(8u)) { w[0]=usb_rd(th_srot[c],0x0); w[1]=usb_rd(th_srot[c],0x4); }
   if (index&(1u<<c)) {
    w[2]=usb_rd(th_tm[c],0x4); w[3]=usb_rd(th_tm[c],0xe4);
    for (unsigned s=0;s<th_nsens[c];s++) w[4+s]=usb_rd(th_tm[c],0xa0+4*s);
   }
  }
  for (unsigned d=0;d<3;d++) {          /* 3 x 8 words */
   uint32_t *w=&th_report[n]; n+=8;
   if (index&0x10) {
    w[0]=usb_rd(th_epss[d],0x0); w[1]=usb_rd(th_epss[d],0x20); w[2]=usb_rd(th_epss[d],0xb0);
    for (unsigned c=0;c<((w[2]&1) ? th_ncpu[d] : 1);c++) w[3+c]=usb_rd(th_epss[d],0x320+4*c);
   }
  }
  th_report[n++]=th_idle_mode; th_report[n++]=th_idle_evnt;
  return n*4;                           /* 4+60+24+2 = 90 words */
 }
 if (value==0x4c55 && index<3) {
  th_report[n++]=0x3155554c; /* LUU1 */
  th_report[n++]=index; th_report[n++]=th_lut_count(th_epss[index]); th_report[n++]=0;
  for (unsigned i=0;i<40;i++) th_report[n++]=usb_rd(th_epss[index],0x100+4*i);
  for (unsigned i=0;i<40;i++) th_report[n++]=usb_rd(th_epss[index],0x200+4*i);
  return n*4;                           /* 84 words */
 }
 if (value==0x434c && (index&0xff)<3) {
  uintptr_t b=th_epss[index&0xff];
  uint32_t level=index>>8, en=usb_rd(b,0x0), dcvs=usb_rd(b,0xb0), count=th_lut_count(b);
  th_report[n++]=0x314c5043; /* CPL1 */
  th_report[n++]=index&0xff; th_report[n++]=level; th_report[n++]=count;
  th_report[n++]=en; th_report[n++]=dcvs;
  th_report[n++]=usb_rd(b,0x20); th_report[n++]=usb_rd(b,0x320);
  uint32_t rc=0;
  if (level!=0xff) {
   if (!(en&1)) rc=1;
   else if (level>=count) rc=2;
   else {
    *(volatile uint32_t *)(b+0x320)=level;
    if (dcvs&1)
     for (unsigned c=1;c<th_ncpu[index&0xff];c++) *(volatile uint32_t *)(b+0x320+4*c)=level;
    __asm__ volatile("dsb sy" ::: "memory");
    delay(2000);
   }
  }
  th_report[n++]=rc;
  th_report[n++]=usb_rd(b,0x20);
  for (unsigned c=0;c<4;c++) th_report[n++]=c<((dcvs&1) ? th_ncpu[index&0xff] : 1) ? usb_rd(b,0x320+4*c) : 0;
  return n*4;                           /* 14 words */
 }
 if (value==0x4d49 && index<=2) {
  th_idle_mode=index;
  __asm__ volatile("dsb sy" ::: "memory");
  th_report[n++]=0x31444d49; /* IMD1 */
  th_report[n++]=th_idle_mode; th_report[n++]=th_idle_evnt;
  th_report[n++]=(uint32_t)rsr("mpidr_el1");
  th_report[n++]=(uint32_t)rsr("clidr_el1"); /* LoUIS 23:21: cost of XNU idle FlushPoU */
  return n*4;
 }
 return 0;
}
/* Called by cpu@100 after each usb_poll. Stays busy for 20 ms after any USB event so
 * console pulls (many 512-byte reads back to back) keep full speed; otherwise waits
 * ~0.43 ms (WFE woken by the generic-timer event stream, bit 12 of CNTVCT) or 0.5 ms. */
static void thermal_monitor_idle(void)
{
 uint32_t mode=th_idle_mode;
 if (!mode) return;
 uint64_t now=rsr("cntpct_el0");
 if (ud.events!=th_idle_events) {
  th_idle_events=ud.events;
  th_idle_busy_until=now+rsr("cntfrq_el0")/50;
  return;
 }
 if (now<th_idle_busy_until) return;
 if (mode==2) {
  if (!th_idle_evnt) {
   uint64_t k=rsr("cntkctl_el1");
   k=(k&~0xf8ULL)|(12ULL<<4)|(1ULL<<2);   /* EVNTI 12, EVNTDIR 0: every 2^13 ticks */
   __asm__ volatile("msr cntkctl_el1, %0; isb" :: "r"(k));
   th_idle_evnt=(uint32_t)rsr("cntkctl_el1")|0x80000000u;
  }
  __asm__ volatile("wfe" ::: "memory");
 } else delay(500);
}
/* ===== end thermal lane ===== */

static void ud_sync(uint32_t off, uint32_t len)
{
 (void)off; (void)len;
 /* Linux boot ABI: MMU/data cache off; DMA sees the same physical memory. */
 __asm__ volatile("dsb sy" ::: "memory");
}

static int
ud_epcmd(unsigned ep, uint32_t cmd, uint32_t p0, uint32_t p1, uint32_t p2)
{
	dwc_wr(DEPCMDPAR0(ep), p0);
	dwc_wr(DEPCMDPAR1(ep), p1);
	dwc_wr(DEPCMDPAR2(ep), p2);
	dwc_wr(DEPCMD(ep), cmd | DEPCMD_CMDACT);
	for (int i = 0; i < 1000; i++) {
		uint32_t v = dwc_rd(DEPCMD(ep));
		if (!(v & DEPCMD_CMDACT)) {
			uint32_t status = (v >> 12) & 0xf;
			if (status) {
				printf("SWEET USB: ep%u cmd 0x%x status %u\n", ep, cmd & 0xf, status);
				return -1;
			}
			return 0;
		}
		delay(1);
	}
	printf("SWEET USB: ep%u cmd 0x%x timeout\n", ep, cmd & 0xf);
	return -1;
}

/* One TRB on physical endpoint ep (0 = ep0 OUT, 1 = ep0 IN). */
static void
ud_start(unsigned ep, uint32_t buf, uint32_t len, uint32_t ctl)
{
	uint32_t trb = ep ? UD_TRB_IN : UD_TRB_OUT;
	volatile uint32_t *t = (volatile uint32_t *)(ud.dma + trb);
	uint64_t bpa = ud.dma_pa + buf;

	t[0] = (uint32_t)bpa;
	t[1] = (uint32_t)(bpa >> 32);
	t[2] = len;
	t[3] = ctl | TRB_HWO | TRB_LST | TRB_ISP_IMI | TRB_IOC;
	ud_sync(trb, 16);
	if (len) {
		ud_sync(buf, len);
	}
	uint64_t tpa = ud.dma_pa + trb;
	ud_epcmd(ep, DEPCMD_STARTTRANSFER, (uint32_t)(tpa >> 32), (uint32_t)tpa, 0);
}

static void
ud_start_setup(void)
{
	ud.state = EP0_SETUP;
	ud_start(0, UD_SETUP, 8, TRBCTL_SETUP);
}

static void
ud_stall(void)
{
	ud_epcmd(0, DEPCMD_SETSTALL, 0, 0, 0);
	ud_start_setup();
}

/* Descriptors. VID 0x1209 / PID 0x0001 = pid.codes test ID, not a product. */
static const uint8_t ud_dev_desc[18] = {
	18, 1, 0x00, 0x02, 0x00, 0x00, 0x00, 64,
	0x09, 0x12, 0x01, 0x00, 0x00, 0x01, 1, 2, 3, 1,
};
static const uint8_t ud_cfg_desc[18] = {
	9, 2, 18, 0, 1, 1, 0, 0x80, 250,                /* config: 1 interface, 500 mA */
	9, 4, 0, 0, 0, 0xff, 0x00, 0x00, 0,             /* vendor interface, no endpoints */
};
static uint32_t
ud_string(unsigned idx, uint8_t *out)
{
	if (idx == 0) {
		out[0] = 4; out[1] = 3; out[2] = 0x09; out[3] = 0x04;   /* en-US */
		return 4;
	}
	if (idx > 3) {
		return 0;
	}
	const char *str = idx == 1 ? "fuxi-ios" : idx == 2 ? "Fuxi hardware probe" : "probe";
	uint32_t n = 2;
	for (; *str && n < 126; str++) {
		out[n++] = (uint8_t)*str;
		out[n++] = 0;
	}
	out[0] = (uint8_t)n;
	out[1] = 3;
	return n;
}

/*
 * Requests 0x5c and 0x5d (boot/phone.py through fuxi-xnu-monitor.py --remote). Not a memory write: what is written is one
 * checked command, and only over a mailbox that the running kernel has marked as one, which it does with fuxiremote=1.
 */
static uint32_t remote_result = FUXI_REMOTE_OFF;

static void
remote_post(uint64_t off, const volatile uint8_t *data)
{
	const volatile struct fuxi_remote_box *seen;
	volatile struct fuxi_remote_box *box;
	struct fuxi_remote_cmd cmd;

	if ((off & 63) != 0 || off > 0x13e000000ULL - sizeof(*box)) {
		remote_result = FUXI_REMOTE_OFF;
		return;
	}
	for (uint32_t i = 0; i < sizeof(cmd); i++) {
		((uint8_t *)&cmd)[i] = data[i];
	}
	box = (volatile struct fuxi_remote_box *)(uintptr_t)(0x8c0000000ULL + off);
	seen = box;
	remote_result = fuxi_remote_may_post(seen->magic, seen->version, seen->done, seen->cmd.seq, &cmd);
	if (remote_result != FUXI_REMOTE_OK) {
		return;
	}
	box->cmd.kind = cmd.kind;
	box->cmd.x0 = cmd.x0;
	box->cmd.y0 = cmd.y0;
	box->cmd.x1 = cmd.x1;
	box->cmd.y1 = cmd.y1;
	box->cmd.ms = cmd.ms;
	box->cmd.check = cmd.check;
	__asm__ volatile("dsb sy" ::: "memory");
	box->cmd.seq = cmd.seq;		/* last: this is what the kernel looks at */
	__asm__ volatile("dsb sy" ::: "memory");
}

static void
ud_setup(void)
{
	const uint8_t *st = (const uint8_t *)(ud.dma + UD_SETUP);
	uint8_t *data = (uint8_t *)(ud.dma + UD_DATA);
	uint8_t type = st[0], req = st[1];
	uint16_t value = (uint16_t)(st[2] | st[3] << 8);
	uint16_t length = (uint16_t)(st[6] | st[7] << 8);
	uint32_t n = 0;
	bool ok = true;

	if (req != 0x5b || value == 0)  /* audio staging sends ~10^5 chunks: log the first only */
		printf("SWEET USB: setup %02x %02x %04x len %u\n", type, req, value, length);
	ud.data_in = (type & 0x80) != 0;
	ud.audio_out = false;
	ud.remote_out = false;
	if (type == 0xc0 && (req == 0x40 || req == 0x41)) {
        uint32_t offset = (uint32_t)value | ((uint32_t)st[4] << 16) | ((uint32_t)st[5] << 24);
        const uint8_t *src = req == 0x40 ? (const uint8_t *)&probe_info : (const uint8_t *)probe_fdt;
        uint32_t size = req == 0x40 ? sizeof(probe_info) : probe_fdt_size;
        n = length > 512 ? 512 : length;
        if (offset > size || n > size - offset) ok = false;
        else memcpy(data, src + offset, n);
    } else if (type == 0xc0 && req == 0x42 && value == 0 &&
               st[4] == 0 && st[5] == 0) {
        n = display_snapshot();
        memcpy(data, display_regs, n);
    } else if (type == 0xc0 && req == 0x43 && !monitor_report[MON_LAUNCH] && value == 0 &&
               st[4] == 0 && st[5] == 0) {
        uint32_t painted = display_paint();
        memcpy(data, &painted, sizeof(painted)); n = sizeof(painted);
    } else if (type == 0xc0 && req == 0x44 && !monitor_report[MON_LAUNCH] && value == 0 &&
               st[4] == 0 && st[5] == 0) {
        ram_probe();
        memcpy(data, ram_report, sizeof(ram_report)); n = sizeof(ram_report);
    } else if (type == 0xc0 && req == 0x45) {
        core_probe();
        memcpy(data, (const void *)core_report, sizeof(core_report)); n=sizeof(core_report);
    } else if (type == 0xc0 && req == 0x46) {
        if (value == 1 && core_report[3] && !core_report[2] && !monitor_report[MON_DELEGATE])
            monitor_report[MON_DELEGATE]=1;
        monitor_report[MON_USB_MPIDR]=rsr("mpidr_el1");
        memcpy(data, (const void *)monitor_report, sizeof(monitor_report)); n=sizeof(monitor_report);
    } else if (type == 0xc0 && (req == 0x47 || req == 0x58)) {
        uint32_t index=(uint32_t)st[4] | ((uint32_t)st[5]<<8);
        uint64_t off=(uint64_t)value | ((uint64_t)index<<16);
        if (req == 0x58) off += 0x100000000ULL; /* upper 4 GiB bank */
        n=length > 512 ? 512 : length;
        if (off > 0x13e000000ULL || n > 0x13e000000ULL-off) ok=false;
        else memcpy(data, (const void *)(uintptr_t)(0x8c0000000ULL+off), n);
    } else if (type == 0xc0 && req == 0x4e) {
        uint32_t index=(uint32_t)st[4] | ((uint32_t)st[5]<<8);
        uint64_t off=(uint64_t)value | ((uint64_t)index<<16);
        const uint64_t size=1080ULL*2400*4;
        n=length > 512 ? 512 : length;
        uint32_t base=usb_rd(0xae05000,0x14);
        if (off>size || n>size-off || (base!=0xb8000000 && base!=0xb8a00000 && base!=0xb9400000) ||
            usb_rd(0xae05000,0x24)!=4320 || usb_rd(0xae05000,0x30)!=0x237ff)
            ok=false;
        else memcpy(data,(const void *)(uintptr_t)((uint64_t)base+off),n);
    } else if (type == 0xc0 && req == 0x4f) {
        /* Read-only display pipeline windows from live FDT: MDP top, CTL0,
         * INTF1 (+tear irq), DSC0 enc/ctl, PP0/PP1, DSI0 controller. MDSS top
         * (0xae00000) is excluded: reading it with non-GDSC AHB off reset the SoC. */
        static const uint32_t win[][2]={{0xae01000,0x400},
            {0xae16000,0x290},{0xae36000,0x300},{0xae36800,0x10},{0xae81000,0x4},
            {0xae81100,0x9c},{0xae81f00,0x24},{0xae6a000,0x4},{0xae6b000,0x4},
            {0xae94000,0x300}};
        uint32_t index=(uint32_t)st[4] | ((uint32_t)st[5]<<8);
        n=length > 512 ? 512 : length;
        if (value >= sizeof(win)/sizeof(win[0]) || (index|n)&3 ||
            index > win[value][1] || n > win[value][1]-index) ok=false;
        else for (uint32_t i=0;i<n;i+=4) {
            uint32_t v=usb_rd(win[value][0],index+i);
            memcpy(data+i,&v,4);
        }
    } else if (type == 0xc0 && req == 0x4b) {
        ufs_snapshot();
        memcpy(data, ufs_report, sizeof(ufs_report)); n=sizeof(ufs_report);
    } else if (type == 0xc0 && req == 0x4c) {
        if (value==1 && !monitor_report[MON_LAUNCH]) display_clock_snapshot_awake();
        else display_clock_snapshot();
        memcpy(data, display_clocks, sizeof(display_clocks)); n=sizeof(display_clocks);
    } else if (type == 0xc0 && req == 0x51) {
        rsc_snapshot();
        memcpy(data, rsc_report, sizeof(rsc_report)); n=sizeof(rsc_report);
    } else if (type == 0xc0 && req == 0x52 && value == 0x4253 && !monitor_report[MON_LAUNCH]) {
        display_boost();
        memcpy(data, boost_report, sizeof(boost_report)); n=sizeof(boost_report);
    } else if (type == 0xc0 && req == 0x53 && !monitor_report[MON_LAUNCH]) {
        ar_test(value);
        memcpy(data, ar_report, sizeof(ar_report)); n=sizeof(ar_report);
    } else if (type == 0xc0 && req == 0x55 && !monitor_report[MON_LAUNCH] &&
               (value == 0x5453 || value == 0x5450 || value == 0x5445)) {
        if (value == 0x5445) {
            n=touch_read_events();
            memcpy(data, touch_events, n);
        } else {
            if (value == 0x5450) touch_power(st[4]); else touch_snapshot(st[4]);
            memcpy(data, touch_report, sizeof(touch_report)); n=sizeof(touch_report);
        }
    } else if (type == 0xc0 && req == 0x56 && value == 0x4753 && !monitor_report[MON_LAUNCH]) {
        /* 'GS': stages1..16 / 0x8a snapshot; 0x100..10f read frozen pages. */
        unsigned index = st[4] | (unsigned)st[5] << 8;
        if (index >= 0x100 && index < 0x100 + G_PAGES) {
            const uint32_t *page = index == 0x100 ? gpu_report : gpu_extra[index - 0x101];
            memcpy(data, page, sizeof(gpu_report)); n=sizeof(gpu_report);
        } else if ((index >= 1 && index <= 16) || index == G_STAGE_8A) {
            gpu_probe(index);
            memcpy(data, gpu_report, sizeof(gpu_report)); n=sizeof(gpu_report);
        } else ok = false;
    } else if (type == 0xc0 && req == 0x5c && value == 0x574c && !monitor_report[MON_LAUNCH]) {
        /* 'WL': 1 = read-only snapshot, 0x100 = re-read it; 0x301..0x30d run
         * M3 steps 1..n (gated power-up/PCIe0/endpoint ID), 0x3ff = M3 report. */
        unsigned index = st[4] | (unsigned)st[5] << 8;
        if (index == 1) wlan_snapshot();
        if (index == 1 || index == 0x100) {
            memcpy(data, wlan_report, sizeof(wlan_report)); n=sizeof(wlan_report);
        } else if ((index > 0x300 && index <= 0x300 + WL3_STEPS) || index == 0x3ff) {
            if (index != 0x3ff) wlan_m3(index - 0x300);
            memcpy(data, wl3_report, sizeof(wl3_report)); n=sizeof(wl3_report);
        } else ok = false;
    } else if (type == 0xc0 && req == 0x54 && value >= 1 && value <= 60) {
        /* Host-requested reset deadline: now + value minutes (bounded, never disabled). */
        monitor_report[MON_DEADLINE]=rsr("cntpct_el0")+rsr("cntfrq_el0")*60*value;
        __asm__ volatile("dsb sy" ::: "memory");
        memcpy(data, (const void *)&monitor_report[MON_DEADLINE], 8); n=8;
    } else if (type == 0xc0 && req == 0x50) {
        display_rcg[0]=0;
        if (!monitor_report[MON_LAUNCH]) display_clock_snapshot_awake();
        else display_clock_snapshot();
        memcpy(data, display_rcg, sizeof(display_rcg)); n=sizeof(display_rcg);
    } else if (type == 0xc0 && req == 0x4d && value == 0x5546 && !monitor_report[MON_LAUNCH]) {
        ufs_snapshot_awake();
        memcpy(data, ufs_report, sizeof(ufs_report)); n=sizeof(ufs_report);
    } else if (type == 0xc0 && req == 0x57 && (n = thermal_request(value, (uint16_t)(st[4] | st[5] << 8))) != 0) {
        /* thermal lane: TSENS / EPSS / monitor idle (see thermal_request) */
        memcpy(data, th_report, n);
    } else if (type == 0xc0 && req == 0x5a && (n = audio_request(value, (uint16_t)(st[4] | st[5] << 8), data)) != 0) {
        /* audio lane: 'AS' snapshot, 'AU' staging status, 'AB' ADSP boot, 'AR' report */
    } else if (type == 0x40 && req == 0x5b && au_out_ok(value, (uint16_t)(st[4] | st[5] << 8), length)) {
        ud.audio_out = true; ud.out_index = (uint16_t)(st[4] | st[5] << 8); ud.out_length = length;
    } else if (type == 0x40 && req == 0x5c && length == sizeof(struct fuxi_remote_cmd) && monitor_report[MON_LAUNCH]) {
        ud.remote_out = true; ud.remote_off = (uint64_t)value | ((uint64_t)st[4] << 16) | ((uint64_t)st[5] << 24);
    } else if (type == 0xc0 && req == 0x5d) {
        memcpy(data, &remote_result, sizeof(remote_result)); n=sizeof(remote_result);
    } else if (type == 0xc0 && req == 0x48) {
        memcpy(data, monitor_error, sizeof(monitor_error)); n=sizeof(monitor_error);
#if defined(FUXI_XNU_LOADER)
    } else if (type == 0xc0 && req == 0x49 && value == 0x584e && monitor_report[MON_ACTIVE] && !gpu_gmu_started) {
        monitor_report[MON_LAUNCH]=1;
        memcpy(data, (const void *)monitor_report, sizeof(monitor_report)); n=sizeof(monitor_report);
#endif
    } else if ((type & 0x60) != 0) {
        ok = false;
	} else if (req == 6) {          /* GET_DESCRIPTOR */
		switch (value >> 8) {
		case 1: memcpy(data, ud_dev_desc, sizeof(ud_dev_desc)); n = sizeof(ud_dev_desc); break;
		case 2: memcpy(data, ud_cfg_desc, sizeof(ud_cfg_desc)); n = sizeof(ud_cfg_desc); break;
		case 3: n = ud_string(value & 0xff, data); ok = n != 0; break;
		default: ok = false; break;
		}
	} else if (req == 5) {          /* SET_ADDRESS: takes effect now (DWC3 handles the timing) */
		uint32_t dcfg = dwc_rd(DCFG) & ~(0x7fu << 3);
		dwc_wr(DCFG, dcfg | ((uint32_t)(value & 0x7f) << 3));
		printf("SWEET USB: address %u\n", value & 0x7f);
	} else if (req == 9) {          /* SET_CONFIGURATION */
		probe_info.configured = value;
	} else if (req == 8) {          /* GET_CONFIGURATION */
		data[0] = 1; n = 1;
	} else if (req == 0) {          /* GET_STATUS */
		data[0] = 0; data[1] = 0; n = 2;
	} else if (req == 11) {         /* SET_INTERFACE */
	} else {
		ok = false;
	}
	if (!ok) {
		ud_stall();
		return;
	}
	if (length == 0) {
		ud.state = EP0_WAIT_STATUS2;    /* status stage on XferNotReady */
		return;
	}
	ud.state = EP0_DATA;
	if (ud.data_in) {
		if (n > length) {
			n = length;
		}
		ud_start(1, UD_DATA, n, TRBCTL_DATA);
	} else {
		/* OUT TRB length is a max-packet multiple; only audio chunks are used. */
		ud_start(0, UD_DATA, ud.audio_out ? ((uint32_t)length + 63u) & ~63u : 64u, TRBCTL_DATA);
	}
}

static void
ud_event(uint32_t ev)
{
	if (ev & 1) {                   /* device event */
		uint32_t type = (ev >> 8) & 0xf;
		if (type == 1) {        /* USB reset */
			dwc_wr(DCFG, dwc_rd(DCFG) & ~(0x7fu << 3));
			probe_info.configured = 0;
		} else if (type == 2) { /* connect done */
			uint32_t speed = dwc_rd(DSTS) & 7;
            probe_info.speed = speed;
			/* ep0 max packet: 64 at high/full speed (modify action) */
			ud_epcmd(0, DEPCMD_SETEPCONFIG, (0u << 1) | (64u << 3) | (2u << 30),
			    (1u << 8) | (1u << 10) | (0u << 25), 0);
			ud_epcmd(1, DEPCMD_SETEPCONFIG, (0u << 1) | (64u << 3) | (2u << 30),
			    (1u << 8) | (1u << 10) | (1u << 25), 0);
			printf("SWEET USB: connected, speed %u\n", speed);
		} else if (type == 0) {
			printf("SWEET USB: disconnect\n");
		}
		return;
	}
	unsigned ep = (ev >> 1) & 0x1f;
	unsigned type = (ev >> 6) & 0xf;
	unsigned status = (ev >> 12) & 0xf;
	if (ep > 1) {
		return;
	}
	/*
	 * Control transfer state machine (Linux dwc3 ep0.c): SETUP completes ->
	 * request handled; the data stage (if any) is started right away; the
	 * status stage is started when the controller reports XferNotReady
	 * with status "control status"; its completion re-arms SETUP.
	 */
	if (type == 1) {                /* XferComplete */
		switch (ud.state) {
		case EP0_SETUP:
			ud_sync(UD_SETUP, 8);
			ud_setup();
			break;
		case EP0_DATA:
			if (ud.audio_out) {
				/* TRB size field holds the untransferred remainder. */
				uint32_t want = ((uint32_t)ud.out_length + 63u) & ~63u;
				uint32_t left = *(volatile uint32_t *)(ud.dma + UD_TRB_OUT + 8) & 0xffffff;
				ud_sync(UD_DATA, want);
				if (want - left == ud.out_length)
					au_out_data(ud.out_index, (const volatile uint8_t *)(ud.dma + UD_DATA), ud.out_length);
				ud.audio_out = false;
			} else if (ud.remote_out) {
				uint32_t left = *(volatile uint32_t *)(ud.dma + UD_TRB_OUT + 8) & 0xffffff;
				ud_sync(UD_DATA, 64);
				if (64 - left == sizeof(struct fuxi_remote_cmd))
					remote_post(ud.remote_off, (const volatile uint8_t *)(ud.dma + UD_DATA));
				else
					remote_result = FUXI_REMOTE_BAD;
				ud.remote_out = false;
			}
			ud.state = EP0_WAIT_STATUS3;
			break;
		case EP0_STATUS:
			ud_start_setup();
			break;
		default:
			break;
		}
		return;
	}
	if (type == 3 && status == 2) { /* XferNotReady, control status phase */
		if (ud.state == EP0_WAIT_STATUS2 || ud.state == EP0_WAIT_STATUS3) {
			uint32_t ctl = ud.state == EP0_WAIT_STATUS3 ? TRBCTL_STATUS3 : TRBCTL_STATUS2;
			ud.state = EP0_STATUS;
			ud_start(ep, UD_DATA, 0, ctl);
		}
	}
}

static void usb_poll(void)
{
 uint32_t count = dwc_rd(GEVNTCOUNT) & 0xffff;
 if (count > UD_EVT_SIZE || (count & 3)) return;
 ud_sync(UD_EVT, UD_EVT_SIZE);
 for (uint32_t done = 0; done < count; done += 4) {
  uint32_t ev = *(volatile uint32_t *)(ud.dma + UD_EVT + ud.evt_pos);
  ud.evt_pos = (ud.evt_pos + 4) % UD_EVT_SIZE;
  ud.events++;
  ud_event(ev);
 }
 if (count) dwc_wr(GEVNTCOUNT, count);
}

static void
usb_start(uintptr_t dwc)
{
 ud.dwc = dwc;
 ud.dma = (uintptr_t)dma_area;
 ud.dma_pa = ud.dma;

	/* Device controller soft reset (PHY and global setup stay as ABL left them). */
	dwc_wr(DCTL, DCTL_CSFTRST);
	int i;
	for (i = 0; i < 500 && (dwc_rd(DCTL) & DCTL_CSFTRST); i++) {
		delay(1000);
	}
	printf("SWEET USB: soft reset %s after %d ms, GCTL 0x%08x DCFG 0x%08x\n",
	    (dwc_rd(DCTL) & DCTL_CSFTRST) ? "STUCK" : "done", i, dwc_rd(GCTL), dwc_rd(DCFG));
	if (dwc_rd(DCTL) & DCTL_CSFTRST) {
		return;
	}

	dwc_wr(GEVNTADRLO, (uint32_t)(ud.dma_pa + UD_EVT));
	dwc_wr(GEVNTADRHI, (uint32_t)((ud.dma_pa + UD_EVT) >> 32));
	dwc_wr(GEVNTSIZ, (1u << 31) | UD_EVT_SIZE);     /* interrupt masked: polled */
	dwc_wr(GEVNTCOUNT, dwc_rd(GEVNTCOUNT) & 0xffff);
	ud.evt_pos = 0;

	/* High speed, address 0 */
	uint32_t dcfg = dwc_rd(DCFG) & ~(7u | (0x7fu << 3));
	dwc_wr(DCFG, dcfg | 0);
	/* Events: disconnect, USB reset, connect done */
	dwc_wr(DEVTEN, 0x7);

	/* Endpoint 0 OUT/IN: start config, control type, max packet 512 until connect done. */
	if (ud_epcmd(0, DEPCMD_DEPSTARTCFG, 0, 0, 0) ||
	    ud_epcmd(0, DEPCMD_SETEPCONFIG, (0u << 1) | (512u << 3), (1u << 8) | (1u << 10) | (0u << 25), 0) ||
	    ud_epcmd(0, DEPCMD_SETTRANSFRESOURCE, 1, 0, 0) ||
	    ud_epcmd(1, DEPCMD_SETEPCONFIG, (0u << 1) | (512u << 3), (1u << 8) | (1u << 10) | (1u << 25), 0) ||
	    ud_epcmd(1, DEPCMD_SETTRANSFRESOURCE, 1, 0, 0)) {
		printf("SWEET USB: endpoint 0 setup failed\n");
		return;
	}
	dwc_wr(DALEPENA, 0x3);
	ud_start_setup();

	uintptr_t qs = (uintptr_t)QSCRATCH_PA;
	if (qs != 0) {
		uint32_t hs = usb_rd(qs, QSCRATCH_HS_PHY_CTRL), ss = usb_rd(qs, QSCRATCH_SS_PHY_CTRL);
		printf("SWEET USB: QSCRATCH HS_PHY_CTRL 0x%08x SS_PHY_CTRL 0x%08x GENERAL_CFG 0x%08x\n",
		    hs, ss, usb_rd(qs, 0x08));
		*(volatile uint32_t *)(qs + QSCRATCH_HS_PHY_CTRL) = hs | UTMI_OTG_VBUS_VALID | SW_SESSVLD_SEL;
		*(volatile uint32_t *)(qs + QSCRATCH_SS_PHY_CTRL) = ss | LANE0_PWR_PRESENT;
		__asm__ volatile ("dsb sy" ::: "memory");
		printf("SWEET USB: VBUS override on: HS_PHY_CTRL 0x%08x SS_PHY_CTRL 0x%08x\n",
		    usb_rd(qs, QSCRATCH_HS_PHY_CTRL), usb_rd(qs, QSCRATCH_SS_PHY_CTRL));
	}

	dwc_wr(DCTL, dwc_rd(DCTL) | DCTL_RUN_STOP);
	for (i = 0; i < 500 && (dwc_rd(DSTS) & DSTS_DEVCTRLHLT); i++) {
		delay(1000);
	}
	printf("SWEET USB: running (DSTS 0x%08x after %d ms), DMA at 0x%llx\n", dwc_rd(DSTS), i, ud.dma_pa);

}

static __attribute__((noreturn)) void reboot(void)
{
 register uint64_t fn __asm__("x0")=0x84000009;
 __asm__ volatile("dsb sy; smc #0" : "+r"(fn) :: "x1","x2","x3","x4","x5","x6","x7","x8","x9","x10","x11","x12","x13","x14","x15","x16","x17","memory");
 for(;;) __asm__ volatile("wfe");
}
void probe_main(uintptr_t fdt)
{
 extern char _head[], _end[];
 monitor_report[MON_MAGIC]=0x31304e4f4d495846ULL; /* FXIMON01 */
 monitor_report[MON_IMAGE]=(uintptr_t)_head; monitor_report[MON_END]=(uintptr_t)_end;
 monitor_report[MON_DEADLINE]=rsr("cntpct_el0")+rsr("cntfrq_el0")*90;
 probe_info.magic=0x45424f5250495846ULL; /* little-endian FXIPROBE */
 probe_info.version=1;
 probe_info.midr=rsr("midr_el1"); probe_info.mpidr=rsr("mpidr_el1");
 probe_info.mmfr0=rsr("id_aa64mmfr0_el1"); probe_info.mmfr1=rsr("id_aa64mmfr1_el1");
 probe_info.current_el=rsr("CurrentEL"); probe_info.cntfrq=rsr("cntfrq_el0");
 probe_info.sctlr=rsr("sctlr_el1");
 /* Refuse DMA if ABL violates the Linux entry ABI. */
 if(probe_info.sctlr & 5) reboot();
 probe_fdt=fdt;
 if(fdt && !(fdt&3) && usb_rd(fdt,0)==0xedfe0dd0) {
  probe_fdt_size=__builtin_bswap32(usb_rd(fdt,4));
  if(probe_fdt_size<40 || probe_fdt_size>0x1000000) probe_fdt_size=0;
 }
 probe_info.fdt_address=fdt; probe_info.fdt_size=probe_fdt_size;
 probe_info.gcc_master=usb_rd(0x100000,0x39018);
 probe_info.gcc_cfg=usb_rd(0x100000,0x39088);
 if((probe_info.gcc_master|probe_info.gcc_cfg)&0x80000000) reboot();
 probe_info.gsnpsid=usb_rd(0xa600000,0xc120);
 if((probe_info.gsnpsid&0xffff0000)!=0x55330000 && (probe_info.gsnpsid&0xffff0000)!=0x33310000) reboot();
 probe_info.gctl_before=usb_rd(0xa600000,GCTL);
 probe_info.dctl_before=usb_rd(0xa600000,DCTL);
 probe_info.dsts_before=usb_rd(0xa600000,DSTS);
 usb_start(0xa600000);
 monitor_report[MON_STAGE]=1;
 while(rsr("cntpct_el0")<monitor_report[MON_DEADLINE]) {
  if (!monitor_report[MON_ACTIVE] && monitor_report[MON_DELEGATE]!=2) {
   usb_poll();
   if (monitor_report[MON_DELEGATE]==1 && ud.state==EP0_SETUP) {
    __asm__ volatile("dsb sy" ::: "memory");
    monitor_report[MON_DELEGATE]=2;
   }
  }
#if defined(FUXI_XNU_LOADER)
  if (monitor_report[MON_ACTIVE] && monitor_report[MON_LAUNCH]) return;
#endif
 }

 reboot();
}
