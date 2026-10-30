/* FUXI pointer-authentication self-test (patch 0114), stage 0 of arm64e. Off unless a boot-arg asks.
 *
 * fuxigpuncimport bit 25 (value 33554432; the other bits keep their GPU meaning, 22-24 are DPU scanout) runs, at EL1 on every CPU in
 * SweetCPU::initCPU (the boot CPU and each secondary as it comes up, IRQ/FIQ masked):
 *   - a UDF under the trap guard below: proves that a trapping probe instruction is skipped, not a panic;
 *   - ID_AA64ISAR1_EL1, ID_AA64ISAR2_EL1, SCTLR_EL1;
 *   - the ten APIA/APIB/APDA/APDB/APGA key halves: saved, written with a fixed test pattern, read back;
 *   - EnIA/EnIB/EnDA/EnDB set, PAC then AUT of a kernel pointer with a modifier for IA, IB, DA and DB, and two
 *     tampered AUTs each (other modifier, one PAC bit flipped), plus PACGA;
 *   - the original keys and SCTLR_EL1 written back and read back, so the arm64 (non-e) kernel runs on unchanged.
 * Every probe instruction is in one table (fuxi_pac_stub). While a CPU is armed, a synchronous exception at a
 * table address (hypervisor-injected UNDEF for a trapped key register, FPAC fault of a tampered AUT) is skipped
 * by sleh_synchronous and recorded; the C side checks the flag after each instruction. Each step is kprintf'd
 * before it runs and kept per CPU; startAll prints one line per CPU and a summary (fuxi_pac_logic.h).
 * Unset (or the bit clear): no register is touched, the sleh hook only compares the PC with an empty range.
 */
#include <pexpert/arm64/board_config.h>
#if defined(FUXI)
#include <stdint.h>
#include <kern/cpu_number.h>
#include <kern/misc_protos.h>
#include <machine/machine_routines.h>
#include <pexpert/pexpert.h>
#include "fuxi_pac_logic.h"

/* Hooks (patch 0114): iokit/Kernel/arm/SweetCPU.cpp, osfmk/arm64/sleh.c */
int fuxi_pac_enabled(void);
void fuxi_pac_probe_cpu(unsigned int cpu);
void fuxi_pac_log_begin(unsigned int cpu);
void fuxi_pac_log_cpu(unsigned int cpu);
void fuxi_pac_log_summary(unsigned int ncpu);
int fuxi_pac_trap(uint64_t pc, uint32_t esr);

/* op 0..33 runs table entry op (x1 = value or pointer, x2 = modifier) and returns x0 (reads) or x1 (PAC ops);
 * op 34 returns the table start, op 35 its end. */
uint64_t fuxi_pac_stub(uint64_t op, uint64_t a, uint64_t b);

#define FP_OP_ISAR1     0u
#define FP_OP_ISAR2     1u
#define FP_OP_SCTLR_RD  2u
#define FP_OP_SCTLR_WR  3u
#define FP_OP_UDF       4u
#define FP_OP_KEY_RD    5u      /* + key index */
#define FP_OP_KEY_WR    15u     /* + key index */
#define FP_OP_PAC       25u     /* + 2 * auth index: PAC, then + 1: AUT */
#define FP_OP_PACGA     33u
#define FP_OP_COUNT     34u
_Static_assert(FP_OP_KEY_RD + FP_NKEYS == FP_OP_KEY_WR && FP_OP_KEY_WR + FP_NKEYS == FP_OP_PAC &&
    FP_OP_PAC + 2 * FP_NAUTH == FP_OP_PACGA && FP_OP_PACGA + 1 == FP_OP_COUNT, "fuxi_pac_stub table layout");

/* Raw encodings (no assembler pauth support needed). Keys: APIAKeyLo/Hi S3_0_C2_C1_0/1, APIBKey _2/_3,
 * APDAKey S3_0_C2_C2_0/1, APDBKey _2/_3, APGAKey S3_0_C2_C3_0/1. PAC ops on x1 with modifier x2:
 * PACIA 0xdac10041, AUTIA 0xdac11041, PACIB 0xdac10441, AUTIB 0xdac11441, PACDA 0xdac10841,
 * AUTDA 0xdac11841, PACDB 0xdac10c41, AUTDB 0xdac11c41, PACGA x1, x1, x2 0x9ac23021. UDF #0 is 0. */
__asm__(
	"	.text\n"
	"	.p2align 2\n"
	"	.globl _fuxi_pac_stub\n"
	"_fuxi_pac_stub:\n"
	"	cmp	x0, #34\n"
	"	b.hs	Lfp_range\n"
	"	adr	x9, Lfp_tab\n"
	"	add	x9, x9, x0, lsl #3\n"
	"	br	x9\n"
	"Lfp_range:\n"
	"	cmp	x0, #34\n"
	"	adr	x0, Lfp_tab\n"
	"	b.eq	Lfp_ret\n"
	"	adr	x0, Lfp_tab_end\n"
	"Lfp_ret:\n"
	"	ret\n"
	"Lfp_isb:\n"
	"	isb\n"
	"	ret\n"
	"Lfp_mov:\n"
	"	mov	x0, x1\n"
	"	ret\n"
	"Lfp_tab:\n"
	"	mrs	x0, S3_0_C0_C6_1\n	ret\n"
	"	mrs	x0, S3_0_C0_C6_2\n	ret\n"
	"	mrs	x0, S3_0_C1_C0_0\n	ret\n"
	"	msr	S3_0_C1_C0_0, x1\n	b	Lfp_isb\n"
	"	.inst	0x00000000\n	ret\n"
	"	mrs	x0, S3_0_C2_C1_0\n	ret\n"
	"	mrs	x0, S3_0_C2_C1_1\n	ret\n"
	"	mrs	x0, S3_0_C2_C1_2\n	ret\n"
	"	mrs	x0, S3_0_C2_C1_3\n	ret\n"
	"	mrs	x0, S3_0_C2_C2_0\n	ret\n"
	"	mrs	x0, S3_0_C2_C2_1\n	ret\n"
	"	mrs	x0, S3_0_C2_C2_2\n	ret\n"
	"	mrs	x0, S3_0_C2_C2_3\n	ret\n"
	"	mrs	x0, S3_0_C2_C3_0\n	ret\n"
	"	mrs	x0, S3_0_C2_C3_1\n	ret\n"
	"	msr	S3_0_C2_C1_0, x1\n	b	Lfp_isb\n"
	"	msr	S3_0_C2_C1_1, x1\n	b	Lfp_isb\n"
	"	msr	S3_0_C2_C1_2, x1\n	b	Lfp_isb\n"
	"	msr	S3_0_C2_C1_3, x1\n	b	Lfp_isb\n"
	"	msr	S3_0_C2_C2_0, x1\n	b	Lfp_isb\n"
	"	msr	S3_0_C2_C2_1, x1\n	b	Lfp_isb\n"
	"	msr	S3_0_C2_C2_2, x1\n	b	Lfp_isb\n"
	"	msr	S3_0_C2_C2_3, x1\n	b	Lfp_isb\n"
	"	msr	S3_0_C2_C3_0, x1\n	b	Lfp_isb\n"
	"	msr	S3_0_C2_C3_1, x1\n	b	Lfp_isb\n"
	"	.inst	0xdac10041\n	b	Lfp_mov\n"
	"	.inst	0xdac11041\n	b	Lfp_mov\n"
	"	.inst	0xdac10441\n	b	Lfp_mov\n"
	"	.inst	0xdac11441\n	b	Lfp_mov\n"
	"	.inst	0xdac10841\n	b	Lfp_mov\n"
	"	.inst	0xdac11841\n	b	Lfp_mov\n"
	"	.inst	0xdac10c41\n	b	Lfp_mov\n"
	"	.inst	0xdac11c41\n	b	Lfp_mov\n"
	"	.inst	0x9ac23021\n	b	Lfp_mov\n"
	"Lfp_tab_end:\n"
	);

#define FP_EC(esr)      (((esr) >> 26) & 0x3fu)
#define FP_MODIFIER     0x5eedfacecafe1234ULL

static unsigned fp_state;               /* 0 unparsed, 1 off, 2 on */
static uint64_t fp_tab_begin, fp_tab_end;       /* stay 0 (empty) unless on */
static unsigned fp_armed[FP_MAX_CPUS], fp_hit[FP_MAX_CPUS], fp_esr[FP_MAX_CPUS];
static struct fp_result fp_res[FP_MAX_CPUS];

int
fuxi_pac_enabled(void)
{
	unsigned state = __atomic_load_n(&fp_state, __ATOMIC_ACQUIRE);
	if (!state) {
		unsigned bits = 0;
		int present = PE_parse_boot_argn("fuxigpuncimport", &bits, sizeof(bits)) ? 1 : 0;
		state = fp_arg_enabled(present, bits) ? 2 : 1;
		if (state == 2) {
			fp_tab_begin = fuxi_pac_stub(FP_OP_COUNT, 0, 0);
			fp_tab_end = fuxi_pac_stub(FP_OP_COUNT + 1, 0, 0);
		}
		__atomic_store_n(&fp_state, state, __ATOMIC_RELEASE);
	}
	return state == 2;
}

/* sleh_synchronous, EL1 only: 1 = this CPU is armed and the PC is a probe instruction; the caller skips it. */
int
fuxi_pac_trap(uint64_t pc, uint32_t esr)
{
	int cpu;
	if (pc < fp_tab_begin || pc >= fp_tab_end) {
		return 0;
	}
	cpu = cpu_number();
	if (cpu < 0 || cpu >= FP_MAX_CPUS || !__atomic_load_n(&fp_armed[cpu], __ATOMIC_RELAXED)) {
		return 0;
	}
	fp_esr[cpu] = esr;
	__atomic_store_n(&fp_hit[cpu], 1u, __ATOMIC_RELAXED);
	return 1;
}

/* One table entry; 0 if it trapped (then nothing is stored). */
static int
fp_do(struct fp_result *r, unsigned cpu, unsigned op, uint64_t a, uint64_t b, uint64_t *out)
{
	uint64_t v;
	__atomic_store_n(&fp_hit[cpu], 0u, __ATOMIC_RELAXED);
	v = fuxi_pac_stub(op, a, b);
	if (__atomic_load_n(&fp_hit[cpu], __ATOMIC_RELAXED)) {
		r->traps++;
		r->last_esr = fp_esr[cpu];
		return 0;
	}
	if (out) {
		*out = v;
	}
	return 1;
}

static void
fp_step(struct fp_result *r, unsigned cpu, unsigned step)
{
	r->step = step;
	kprintf("FUXI PAC: cpu=%u step=%s\n", cpu, fp_step_name(step));
}

/* A tampered AUT must not return the original pointer. */
static char
fp_tamper(struct fp_result *r, unsigned cpu, unsigned op, uint64_t s, uint64_t mod, uint64_t p)
{
	uint64_t t;
	if (fp_do(r, cpu, op, s, mod, &t)) {
		return t != p ? 'P' : 'X';
	}
	return FP_EC(r->last_esr) == FP_EC_PAC_FAIL ? 'F' : 'E';
}

static char
fp_worse(char a, char b)
{
	static const char order[] = "PFEX";     /* best to worst */
	unsigned ia = 0, ib = 0;
	while (order[ia] && order[ia] != a) {
		ia++;
	}
	while (order[ib] && order[ib] != b) {
		ib++;
	}
	return ia >= ib ? a : b;
}

static void
fp_auth(struct fp_result *r, unsigned cpu)
{
	uint64_t p = (uint64_t)(uintptr_t)&fp_res[0], s, t, d;
	unsigned fpac = fp_fpac(r->isar1, r->isar2);
	for (unsigned k = 0; k < FP_NAUTH; k++) {
		unsigned op = FP_OP_PAC + 2 * k;
		int ok = fp_do(r, cpu, op, p, FP_MODIFIER, &s) && s != p;
		if (ok) {
			r->sig[k] = s;
			ok = fp_do(r, cpu, op + 1, s, FP_MODIFIER, &t) && t == p;
		}
		r->auth[k] = ok;
		/* With FPAC a rejected AUT faults: run it only under a proven guard. */
		if (ok && (r->guard || !fpac)) {
			d = s ^ p;
			r->fail[k] = fp_worse(fp_tamper(r, cpu, op + 1, s, FP_MODIFIER ^ 1, p),
			    fp_tamper(r, cpu, op + 1, s ^ (d & (~d + 1)), FP_MODIFIER, p));
		}
	}
}

static void
fp_generic_auth(struct fp_result *r, unsigned cpu)
{
	uint64_t p = (uint64_t)(uintptr_t)&fp_res[0], g1, g2, g3;
	if (!fp_do(r, cpu, FP_OP_PACGA, p, FP_MODIFIER, &g1) || !fp_do(r, cpu, FP_OP_PACGA, p, FP_MODIFIER, &g2) ||
	    !fp_do(r, cpu, FP_OP_PACGA, p, FP_MODIFIER ^ 1, &g3)) {
		r->ga = 0;
		return;
	}
	r->sig[FP_NAUTH] = g1;
	r->ga = g1 == g2 && g1 != g3 && g1 != 0 && (g1 & 0xffffffffULL) == 0;
}

static void
fp_probe(struct fp_result *r, unsigned cpu)
{
	uint64_t orig[FP_NKEYS], v;
	unsigned level, wrote_sctlr = 0;

	r->sctlr_back = 1;      /* nothing changed yet; the final SCTLR read decides */
	fp_step(r, cpu, FP_STEP_GUARD);
	r->guard = !fp_do(r, cpu, FP_OP_UDF, 0, 0, 0) && FP_EC(r->last_esr) == FP_EC_UNKNOWN;
	if (!r->guard) {
		return;
	}
	r->traps = 0;
	r->last_esr = 0;

	fp_step(r, cpu, FP_STEP_ID);
	r->id_trap = !fp_do(r, cpu, FP_OP_ISAR1, 0, 0, &r->isar1);
	r->id_trap |= !fp_do(r, cpu, FP_OP_ISAR2, 0, 0, &r->isar2);
	if (!fp_do(r, cpu, FP_OP_SCTLR_RD, 0, 0, &r->sctlr)) {
		r->id_trap = 1;
		return;         /* nothing was changed */
	}
	level = fp_auth_level(r->isar1, r->isar2);
	if (!level && !fp_generic(r->isar1, r->isar2)) {
		return;
	}

	fp_step(r, cpu, FP_STEP_KEYRD);
	for (unsigned i = 0; i < FP_NKEYS; i++) {
		if (fp_do(r, cpu, FP_OP_KEY_RD + i, 0, 0, &orig[i])) {
			r->key_rd |= 1u << i;
		} else {
			r->key_trap |= 1u << i;
		}
	}
	fp_step(r, cpu, FP_STEP_KEYWR);
	for (unsigned i = 0; i < FP_NKEYS; i++) {
		if (!(r->key_rd & (1u << i))) {
			continue;
		}
		if (!fp_do(r, cpu, FP_OP_KEY_WR + i, fp_key_pattern(i), 0, 0) || !fp_do(r, cpu, FP_OP_KEY_RD + i, 0, 0, &v)) {
			r->key_trap |= 1u << i;
		} else if (v == fp_key_pattern(i)) {
			r->key_ok |= 1u << i;
		} else if (v == orig[i]) {
			r->key_ign |= 1u << i;
		}
	}

	if (level) {
		fp_step(r, cpu, FP_STEP_SCTLR);
		if (!fp_do(r, cpu, FP_OP_SCTLR_WR, r->sctlr | FP_SCTLR_EN, 0, 0)) {
			r->sctlr_trap = 1;
		} else {
			wrote_sctlr = 1;
			(void)fp_do(r, cpu, FP_OP_SCTLR_RD, 0, 0, &r->sctlr_set);
		}
		if ((r->sctlr_set & FP_SCTLR_EN) == FP_SCTLR_EN) {
			fp_step(r, cpu, FP_STEP_AUTH);
			fp_auth(r, cpu);
		} else {
			for (unsigned k = 0; k < FP_NAUTH; k++) {
				r->auth[k] = 0;
			}
		}
	}
	if (fp_generic(r->isar1, r->isar2)) {
		fp_step(r, cpu, FP_STEP_GA);
		fp_generic_auth(r, cpu);
	}

	fp_step(r, cpu, FP_STEP_RESTORE);
	for (unsigned i = 0; i < FP_NKEYS; i++) {
		if ((r->key_rd & (1u << i)) && fp_do(r, cpu, FP_OP_KEY_WR + i, orig[i], 0, 0) &&
		    fp_do(r, cpu, FP_OP_KEY_RD + i, 0, 0, &v) && v == orig[i]) {
			r->key_back |= 1u << i;
		}
	}
	/* The test key must change the signature: the hardware uses the key that read back. */
	if (r->key_ok == FP_KEYS_ALL && r->auth[0] == 1) {
		r->key_used = fp_do(r, cpu, FP_OP_PAC, (uint64_t)(uintptr_t)&fp_res[0], FP_MODIFIER, &v) ? v != r->sig[0] : 0;
	}
	if (wrote_sctlr) {
		(void)fp_do(r, cpu, FP_OP_SCTLR_WR, r->sctlr, 0, 0);
	}
	r->sctlr_back = fp_do(r, cpu, FP_OP_SCTLR_RD, 0, 0, &v) && v == r->sctlr;
}

/* SweetCPU::initCPU, on the CPU itself, once. */
void
fuxi_pac_probe_cpu(unsigned int cpu)
{
	struct fp_result *r;
	boolean_t istate;

	if (cpu >= FP_MAX_CPUS || !fuxi_pac_enabled() || __atomic_load_n(&fp_res[cpu].done, __ATOMIC_ACQUIRE)) {
		return;
	}
	r = &fp_res[cpu];
	for (unsigned k = 0; k < FP_NAUTH; k++) {
		r->auth[k] = -1;
		r->fail[k] = '-';
	}
	r->ga = -1;
	r->key_used = -1;
	istate = ml_set_interrupts_enabled(FALSE);
	r->midr = __builtin_arm_rsr64("MIDR_EL1");
	__atomic_store_n(&fp_armed[cpu], 1u, __ATOMIC_RELAXED);
	fp_probe(r, cpu);
	__atomic_store_n(&fp_armed[cpu], 0u, __ATOMIC_RELAXED);
	r->step = FP_STEP_DONE;
	__atomic_store_n(&r->done, 1u, __ATOMIC_RELEASE);
	(void)ml_set_interrupts_enabled(istate);
	kprintf("FUXI PAC: cpu=%u step=done\n", cpu);
}

void
fuxi_pac_log_begin(unsigned int cpu)
{
	if (cpu < FP_MAX_CPUS && fuxi_pac_enabled()) {
		printf("FUXI PAC: cpu=%u probe begin\n", cpu);
	}
}

void
fuxi_pac_log_cpu(unsigned int cpu)
{
	char line[400];
	if (cpu >= FP_MAX_CPUS || !fuxi_pac_enabled()) {
		return;
	}
	(void)__atomic_load_n(&fp_res[cpu].done, __ATOMIC_ACQUIRE);
	(void)fp_format(line, (unsigned)sizeof(line), cpu, &fp_res[cpu]);
	printf("%s\n", line);
}

void
fuxi_pac_log_summary(unsigned int ncpu)
{
	char line[256];
	if (!fuxi_pac_enabled()) {
		return;
	}
	(void)fp_format_summary(line, (unsigned)sizeof(line), fp_res, ncpu);
	printf("%s\n", line);
}
#endif /* FUXI */
