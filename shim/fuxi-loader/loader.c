/* Fuxi XNU handoff: bounded embedded payload, upper RAM and measured splash.
 * No storage access. Only the CPU used by ABL is started.
 */
#include <stdint.h>
#include "console.h"
#include "../fuxi-probe/monitor.h"
#include "../fuxi-probe/gpu-handoff.h"
void probe_main(uintptr_t fdt);
uint64_t probe_fdt_address(void);
uint32_t probe_fdt_bytes(void);

/* boot_args.CommandLine: 608 bytes up to xnu-8796 (payload v1), 1024 in xnu-12377 (v2) */
#define BOOT_LINE_V1		608
#define BOOT_LINE_V2		1024
#define PAGE_16K		0x4000UL

struct payload_seg {
	uint64_t vmaddr, vmsize, file_off, file_size;
};

struct payload {
	char magic[8];
	uint32_t version, nsegs;
	uint64_t virt_base, entry_va, adt_off, adt_size, kernel_end;
	uint64_t phys_base, mem_size;
	char cmdline[];		/* BOOT_LINE_V1 or _V2 bytes, then struct payload_seg[nsegs] */
};

/* pexpert/pexpert/arm64/boot.h, Revision/Version 2 */
struct boot_video {
	uint64_t v_baseAddr, v_display, v_rowBytes, v_width, v_height, v_depth;
};

struct boot_args {
	uint16_t Revision, Version;
	uint64_t virtBase, physBase, memSize, topOfKernelData;
	struct boot_video Video;
	uint32_t machineType;
	uint64_t deviceTreeP;
	uint32_t deviceTreeLength;
	char CommandLine[];	/* line bytes, then u64 bootFlags, memSizeActual (8-aligned) */
};

_Static_assert(__builtin_offsetof(struct payload, cmdline) == 0x48, "payload header layout");
_Static_assert(__builtin_offsetof(struct boot_args, Video) == 40, "boot_args layout");
_Static_assert(__builtin_offsetof(struct boot_args, CommandLine) == 108, "boot_args layout");
/* v1: bootFlags at 720, sizeof 736 (xnu-8796); v2: 1136, 1152 (xnu-12377) */
#define BOOT_ARGS_FLAGS(line)	((108 + (line) + 7) & ~7UL)
_Static_assert(BOOT_ARGS_FLAGS(BOOT_LINE_V1) + 16 == 736, "boot_args v1 layout");
_Static_assert(BOOT_ARGS_FLAGS(BOOT_LINE_V2) + 16 == 1152, "boot_args v2 layout");

/* MMU off: everything is Device memory, so copy in aligned 8-byte words. */
static void copy64(uintptr_t dst, uintptr_t src, uint64_t n)
{
	volatile uint64_t *d = (volatile uint64_t *)dst;
	volatile const uint64_t *s = (volatile const uint64_t *)src;
	for (uint64_t i = 0; i < (n + 7) / 8; i++)
		d[i] = s[i];
}

static void zero64(uintptr_t dst, uint64_t n)
{
	volatile uint64_t *d = (volatile uint64_t *)dst;
	for (uint64_t i = 0; i < n / 8; i++)
		d[i] = 0;
}

/* No GPU initialization here. The top-of-bank reservation is outside every accepted XNU window.
 * Publishing magic last makes a missing/partial firmware set fail closed. */
static void stage_gpu_firmware(void)
{
 extern const uint8_t _gpu_fw_start[], _gpu_fw_end[];
 extern const uint8_t _sqe_fw_start[], _sqe_fw_end[];
 extern const uint8_t _zap_fw_start[], _zap_fw_end[];
 volatile struct fg_handoff *h = (volatile struct fg_handoff *)FG_HANDOFF;
 zero64(FG_HANDOFF, PAGE_16K);
 h->version = 1; h->bytes = sizeof(*h);
 h->base = FG_SCRATCH; h->size = FG_SCRATCH_SIZE;
 for (unsigned i = 0; i < 3; i++) {
  const uint8_t *start = i == 0 ? _gpu_fw_start : i == 1 ? _sqe_fw_start : _zap_fw_start;
  uintptr_t end = i == 0 ? (uintptr_t)_gpu_fw_end : i == 1 ? (uintptr_t)_sqe_fw_end : (uintptr_t)_zap_fw_end;
  unsigned n = end - (uintptr_t)start;
  if (!n || n > FG_FW_SLOT) return;
  /* Firmware is 4-byte aligned, not necessarily 8-byte padded. */
  volatile uint32_t *dst = (volatile uint32_t *)(FG_FW_BASE + i * FG_FW_SLOT);
  for (unsigned off = 0; off < n / 4; off++) dst[off] = ((const uint32_t *)start)[off];
  h->fw_size[i] = n;
  h->fw_hash[i] = fg_hash((const uint8_t *)dst, n);
 }
 __asm__ volatile("dsb sy" ::: "memory");
 h->magic = FG_MAGIC;
}

static int magic_ok(const char *m)
{
	static const char want[] = "SWEETXNU";
	for (int i = 0; i < 8; i++)
		if (m[i] != want[i])
			return 0;
	return 1;
}

typedef void (*xnu_entry_t)(uint64_t boot_args_pa);

static const char *loader_boot(void)
{

 extern const unsigned char _payload_start[], _payload_end[];
 uint64_t rd_start = (uintptr_t)_payload_start, rd_end = (uintptr_t)_payload_end;
 uint64_t length = rd_end - rd_start;
 const struct payload *p = (const struct payload *)rd_start;
 if (length < sizeof(*p) || !magic_ok(p->magic) || (p->version != 1 && p->version != 2))
  return "bad embedded payload header";
 const uint64_t line = p->version == 1 ? BOOT_LINE_V1 : BOOT_LINE_V2;
 const struct payload_seg *segs = (const struct payload_seg *)(rd_start + sizeof(*p) + line);
 if (p->nsegs == 0 || p->nsegs > 32 ||
     length < sizeof(*p) + line + p->nsegs * sizeof(struct payload_seg))
  return "bad embedded payload header";
 if (p->phys_base != 0x8c0000000ULL || (p->mem_size != 0x40000000ULL && p->mem_size != FG_RAM_SIZE) ||
     (p->virt_base & (0x2000000-1)) || (p->kernel_end & (PAGE_16K-1)) ||
     p->kernel_end < p->virt_base ||
     p->kernel_end - p->virt_base > p->mem_size - 2*PAGE_16K ||
     !p->adt_size || p->adt_size > PAGE_16K || (p->adt_off & 7) ||
     p->adt_off > length || p->adt_size > length-p->adt_off ||
     p->cmdline[line-1])
  return "unsafe Fuxi memory layout";
 int entry_found = 0;
 for (uint32_t i=0; i<p->nsegs; i++) {
  const struct payload_seg *s = &segs[i];
  if (s->vmaddr < p->virt_base || s->vmaddr > p->kernel_end ||
      s->vmsize > p->kernel_end-s->vmaddr || s->file_size > s->vmsize ||
      (s->vmaddr & 7) || (s->vmsize & 7) || (s->file_off & 7) ||
      s->file_off > length || s->file_size > length-s->file_off ||
      ((s->file_size+7)&~7ULL) > length-s->file_off)
   return "unsafe payload segment";
  for (uint32_t j=0;j<i;j++) {
   const struct payload_seg *t=&segs[j];
   if (s->vmaddr < t->vmaddr+t->vmsize && t->vmaddr < s->vmaddr+s->vmsize)
    return "overlapping segments";
  }
  if (p->entry_va >= s->vmaddr && p->entry_va-s->vmaddr < s->file_size)
   entry_found=1;
 }
 if (!entry_found) return "entry outside kernel segments";

 /* Reject all loader/FDT overlap with the reservation before writing it. */
 { /* All supported layouts exclude the same top-of-bank scratch. */
  if ((rd_start < FG_SCRATCH + FG_SCRATCH_SIZE && FG_SCRATCH < rd_end) ||
      (probe_fdt_address() < FG_SCRATCH + FG_SCRATCH_SIZE &&
       FG_SCRATCH < probe_fdt_address() + probe_fdt_bytes()))
   return "GPU reservation overlaps loader/FDT";
  stage_gpu_firmware();
 }
 monitor_report[MON_STAGE]=5;
	uint64_t pa_of_va_base = p->phys_base - p->virt_base;	/* pa = va + this */
	con_hex2("PHYS", "base=", p->phys_base, "size=", p->mem_size);
	con_hex("VIRT", p->virt_base);
	con_hex("ENTRY", p->entry_va);

	/* The payload must not overlap where the kernel goes. */
	if (rd_end > p->phys_base && rd_start < p->phys_base + p->mem_size)
		return "ramdisk overlaps kernel memory";

	for (uint32_t i = 0; i < p->nsegs; i++) {
		const struct payload_seg *s = &segs[i];
		uintptr_t dst = s->vmaddr + pa_of_va_base;
		zero64(dst, s->vmsize);
		copy64(dst, rd_start + s->file_off, s->file_size);
		if (s->vmsize > s->file_size)
			zero64(dst + ((s->file_size + 7) & ~7UL), s->vmsize - ((s->file_size + 7) & ~7UL));
	}
	con_hex2("SEGS", "n=", p->nsegs, "end=", p->kernel_end + pa_of_va_base);

 monitor_report[MON_STAGE]=6;
	/* ADT, then boot_args, behind the kernel; everything below topOfKernelData is ours. */
	uintptr_t adt_pa = p->kernel_end + pa_of_va_base;
	copy64(adt_pa, rd_start + p->adt_off, p->adt_size);
	uintptr_t ba_pa = (adt_pa + p->adt_size + PAGE_16K - 1) & ~(PAGE_16K - 1);
	uintptr_t top = ba_pa + PAGE_16K;

	zero64(ba_pa, PAGE_16K);
	volatile struct boot_args *ba = (volatile struct boot_args *)ba_pa;
	ba->Revision = 2;
	ba->Version = 2;
	ba->virtBase = p->virt_base;
	ba->physBase = p->phys_base;
	ba->memSize = p->mem_size;
	ba->topOfKernelData = top;
	ba->Video.v_baseAddr = FB_BASE;
	ba->Video.v_display = 1;
	ba->Video.v_rowBytes = FB_STRIDE;
	ba->Video.v_width = FB_W;
	ba->Video.v_height = FB_H;
	ba->Video.v_depth = 32;
	ba->deviceTreeP = adt_pa - pa_of_va_base;	/* xnu reads it with the MMU on: a VA */
	ba->deviceTreeLength = (uint32_t)p->adt_size;
	for (uint64_t i = 0; i < line; i++)
		ba->CommandLine[i] = p->cmdline[i];
	/* bootFlags stays 0 (zeroed page) */
	*(volatile uint64_t *)(ba_pa + BOOT_ARGS_FLAGS(line) + 8) = p->mem_size;	/* memSizeActual */
	con_hex2("BOOTARG", "pa=", ba_pa, "top=", top);

 monitor_report[MON_BOOT_ARGS]=ba_pa;
 monitor_report[MON_STAGE]=7;
	uintptr_t entry_pa = p->entry_va + pa_of_va_base;
 monitor_report[MON_ENTRY]=entry_pa;
	con_hex("JUMP", entry_pa);
	con_str("XNU", "jumping (fbcon clears screen)");

	/* Everything was written with the MMU and caches off; drop any stale I-cache lines too. */
	__asm__ volatile("dsb sy; ic iallu; dsb sy; isb" ::: "memory");
 monitor_report[MON_STAGE]=8;
 __asm__ volatile("dsb sy" ::: "memory");
	((xnu_entry_t)entry_pa)(ba_pa);
	return "xnu returned";
}

void loader_main(uintptr_t fdt)
{
 probe_main(fdt);
 monitor_report[MON_STAGE]=4;
 /* Refuse to draw unless ABL left the measured SSPP0/CTL0 splash active. */
 volatile uint32_t *sspp=(volatile uint32_t *)0xae05000;
 if (sspp[0x14/4] != 0xb8000000 || sspp[0x24/4] != 4320 ||
     sspp[0] != ((2400u<<16)|1080u) || sspp[0x30/4] != 0x237ff)
  con_fb=0; /* USB diagnostics also work without a usable inherited scanout. */
 con_clear(BLUE);
 con_str("FUXI", "XNU loader + USB monitor");
 const char *error=loader_boot();
 con_str("ERROR", error);
 for (unsigned i=0;i<sizeof(monitor_error)-1 && error[i];i++) monitor_error[i]=error[i];
 monitor_report[MON_STAGE]=99;
 __asm__ volatile("dsb sy" ::: "memory");
 for (;;) __asm__ volatile("wfe");
}
