/* Pure helpers of osfmk/arm64/fuxi_pac.c (patch 0114), shared with the host
 * test tools/test-fuxi-pac.py. No kernel headers: integers in, text out.
 */
#ifndef FUXI_PAC_LOGIC_H
#define FUXI_PAC_LOGIC_H

#define FP_MAX_CPUS     8
#define FP_ARG_BIT      0x2000000u      /* fuxigpuncimport bit 25 (value 33554432) */
#define FP_NKEYS        10              /* APIA, APIB, APDA, APDB, APGA; Lo then Hi */
#define FP_KEYS_ALL     0x3ffu
#define FP_NAUTH        4               /* IA, IB, DA, DB */

/* arm64e (patch 0115): fuxi_native_pac word offsets, mirrored from osfmk/arm64/pac_asm.h NATIVE_PAC_*. */
#define FP_NATIVE_READY         10
#define FP_NATIVE_MODE          11      /* 1 native, 2 hyp (the first CPU's key readback differed) */
#define FP_NATIVE_CPUS          12
#define FP_NATIVE_BAD           13
#define FP_NATIVE_BAD_MPIDR     14
#define FP_NATIVE_GA_REF        15
#define FP_NATIVE_GA_BAD        16
#define FP_NATIVE_GA_MPIDR      17
#define FP_NATIVE_WORDS         24

/* SCTLR_EL1 enables, in the order of the en= field: EnIA, EnIB, EnDA, EnDB. */
#define FP_SCTLR_ENIA   (1ULL << 31)
#define FP_SCTLR_ENIB   (1ULL << 30)
#define FP_SCTLR_ENDA   (1ULL << 27)
#define FP_SCTLR_ENDB   (1ULL << 13)
#define FP_SCTLR_EN     (FP_SCTLR_ENIA | FP_SCTLR_ENIB | FP_SCTLR_ENDA | FP_SCTLR_ENDB)

#define FP_EC_UNKNOWN   0x00u
#define FP_EC_PAC_FAIL  0x1cu

/* Steps, logged before each one runs, so a hang names the last step entered. */
#define FP_STEP_NONE    0u
#define FP_STEP_GUARD   1u      /* UDF under the trap guard: proves the sleh hook */
#define FP_STEP_ID      2u      /* ID_AA64ISAR1/2, SCTLR */
#define FP_STEP_KEYRD   3u      /* save the ten key halves */
#define FP_STEP_KEYWR   4u      /* write test keys, read back */
#define FP_STEP_SCTLR   5u      /* set EnIA/EnIB/EnDA/EnDB */
#define FP_STEP_AUTH    6u      /* PAC/AUT round trips and tampered AUTs */
#define FP_STEP_GA      7u      /* PACGA */
#define FP_STEP_RESTORE 8u      /* original keys, SCTLR */
#define FP_STEP_DONE    9u

static inline const char *
fp_step_name(unsigned step)
{
	static const char *const names[] = { "none", "guard", "id", "keyrd", "keywr", "sctlr", "auth", "ga", "restore", "done" };
	return step <= FP_STEP_DONE ? names[step] : "?";
}

/* The probe runs only when fuxigpuncimport is present and has bit 25 set. */
static inline int
fp_arg_enabled(int present, unsigned bits)
{
	return present && (bits & FP_ARG_BIT) != 0;
}

static inline unsigned
fp_field(uint64_t reg, unsigned shift)
{
	return (unsigned)((reg >> shift) & 0xfu);
}

/* ID_AA64ISAR1_EL1: APA [7:4], API [11:8], GPA [27:24], GPI [31:28]. ID_AA64ISAR2_EL1: GPA3 [11:8], APA3 [15:12]. */
static inline unsigned fp_apa(uint64_t isar1) { return fp_field(isar1, 4); }
static inline unsigned fp_api(uint64_t isar1) { return fp_field(isar1, 8); }
static inline unsigned fp_gpa(uint64_t isar1) { return fp_field(isar1, 24); }
static inline unsigned fp_gpi(uint64_t isar1) { return fp_field(isar1, 28); }
static inline unsigned fp_gpa3(uint64_t isar2) { return fp_field(isar2, 8); }
static inline unsigned fp_apa3(uint64_t isar2) { return fp_field(isar2, 12); }

/* The implemented address-auth level: 1 PAuth, 2 EPAC, 3 PAuth2, 4 FPAC, 5 FPACCOMBINE (only one algorithm field is nonzero). */
static inline unsigned
fp_auth_level(uint64_t isar1, uint64_t isar2)
{
	unsigned a = fp_apa(isar1), b = fp_api(isar1), c = fp_apa3(isar2);
	unsigned m = a > b ? a : b;
	return m > c ? m : c;
}

static inline int
fp_generic(uint64_t isar1, uint64_t isar2)
{
	return fp_gpa(isar1) || fp_gpi(isar1) || fp_gpa3(isar2);
}

/* 0 no FPAC (a failed AUT returns a poisoned pointer), 1 FPAC, 2 FPACCOMBINE (a failed AUT faults). */
static inline unsigned
fp_fpac(uint64_t isar1, uint64_t isar2)
{
	unsigned level = fp_auth_level(isar1, isar2);
	return level >= 5 ? 2 : level == 4 ? 1 : 0;
}

/* The same test key on every CPU, so signatures must agree across cores. */
static inline uint64_t
fp_key_pattern(unsigned idx)
{
	return 0x0123456789abcdefULL ^ ((uint64_t)(idx + 1) * 0x9e3779b97f4a7c15ULL);
}

/* One CPU. Masks have one bit per key half in FP_NKEYS order. */
struct fp_result {
	unsigned done, step;
	uint64_t midr, isar1, isar2, sctlr, sctlr_set;
	unsigned guard;                 /* the UDF trapped and was skipped: probe faults are recoverable */
	unsigned id_trap;               /* an ID or SCTLR read trapped */
	unsigned key_rd, key_ok, key_ign, key_trap, key_back;   /* readable, test value read back, unchanged, trapped, restored */
	unsigned sctlr_trap;            /* the SCTLR write trapped */
	unsigned sctlr_back;            /* SCTLR read back equal to the original after the restore */
	int auth[FP_NAUTH];             /* PAC then AUT of the test pointer: 1 ok, 0 failed, -1 not run */
	char fail[FP_NAUTH];            /* tampered AUT: 'P' poisoned result, 'F' FPAC fault, 'E' other trap, 'X' accepted, '-' not run */
	int ga;                         /* PACGA: 1 ok, 0 failed, -1 not run */
	int key_used;                   /* PACIA differs between the test key and the original key: 1, 0, -1 not run */
	uint64_t sig[FP_NAUTH + 1];     /* signed test pointer per key with the test keys, then PACGA */
	unsigned traps, last_esr;
	unsigned live;                  /* arm64e kernel: the keys in use are never written (keywr, SCTLR write and restore skipped) */
};

static inline const char *
fp_keys_rw(const struct fp_result *r)
{
	if (r->key_trap) {
		return "trap";
	}
	if (r->live) {
		return r->key_rd == FP_KEYS_ALL ? "live" : "partial";
	}
	if (r->key_ok == FP_KEYS_ALL) {
		return "ok";
	}
	if (r->key_ign == FP_KEYS_ALL) {
		return "ignored";
	}
	return r->key_rd ? "partial" : "none";
}

static inline int
fp_pac_ok(const struct fp_result *r)
{
	for (unsigned k = 0; k < FP_NAUTH; k++) {
		if (r->auth[k] != 1) {
			return 0;
		}
	}
	return r->ga != 0;
}

/* 1 every tampered AUT was rejected, 0 one was accepted, -1 not run (FPAC without a working guard, or no PAC). */
static inline int
fp_auth_fail_ok(const struct fp_result *r)
{
	int ran = 0;
	for (unsigned k = 0; k < FP_NAUTH; k++) {
		if (r->fail[k] == 'X' || r->fail[k] == 'E') {
			return 0;
		}
		ran |= r->fail[k] == 'P' || r->fail[k] == 'F';
	}
	return ran ? 1 : -1;
}

static inline int
fp_restored(const struct fp_result *r)
{
	return r->key_back == r->key_rd && r->sctlr_back;
}

static inline void
fp_append(char *buf, unsigned size, unsigned *at, const char *s)
{
	while (*s && *at + 1 < size) {
		buf[(*at)++] = *s++;
	}
	buf[*at] = 0;
}

static inline void
fp_append_u(char *buf, unsigned size, unsigned *at, uint64_t v)
{
	char tmp[24];
	unsigned n = 0;
	do {
		tmp[n++] = (char)('0' + v % 10);
		v /= 10;
	} while (v);
	while (n && *at + 1 < size) {
		buf[(*at)++] = tmp[--n];
	}
	buf[*at] = 0;
}

static inline void
fp_append_x(char *buf, unsigned size, unsigned *at, uint64_t v)
{
	char tmp[24];
	unsigned n = 0;
	do {
		tmp[n++] = "0123456789abcdef"[v & 0xf];
		v >>= 4;
	} while (v);
	fp_append(buf, size, at, "0x");
	while (n && *at + 1 < size) {
		buf[(*at)++] = tmp[--n];
	}
	buf[*at] = 0;
}

static inline void
fp_append_i(char *buf, unsigned size, unsigned *at, int v)
{
	if (v < 0) {
		fp_append(buf, size, at, "-");
	} else {
		fp_append_u(buf, size, at, (uint64_t)v);
	}
}

static inline void
fp_append_c(char *buf, unsigned size, unsigned *at, char c)
{
	char s[2] = { c, 0 };
	fp_append(buf, size, at, s);
}

/*
 * FUXI PAC: cpu=N midr= isar1= isar2= apa= api= apa3= gpa= gpi= gpa3= en=<EnIA EnIB EnDA EnDB> guard=
 *   keys_rw=ok|ignored|trap|partial|none keys=<ok>/<rd>/<ign>/<trap> restored= pac_ok= auth=<IA IB DA DB GA>
 *   key_used= auth_fail_ok= fail=<IA IB DA DB> fpac=0|1|2 sig= traps= esr= [mode=live]
 * (arm64e: keys_rw=live, the keys start.s programmed are read, never written) or "FUXI PAC: cpu=N incomplete step=NAME" when the probe has not finished.
 * Returns the line length (the buffer is always terminated).
 */
static inline unsigned
fp_format(char *buf, unsigned size, unsigned cpu, const struct fp_result *r)
{
	unsigned at = 0;
	if (!size) {
		return 0;
	}
	buf[0] = 0;
	fp_append(buf, size, &at, "FUXI PAC: cpu=");
	fp_append_u(buf, size, &at, cpu);
	if (!r->done) {
		fp_append(buf, size, &at, " incomplete step=");
		fp_append(buf, size, &at, fp_step_name(r->step));
		return at;
	}
	fp_append(buf, size, &at, " midr=");
	fp_append_x(buf, size, &at, r->midr);
	fp_append(buf, size, &at, " isar1=");
	fp_append_x(buf, size, &at, r->isar1);
	fp_append(buf, size, &at, " isar2=");
	fp_append_x(buf, size, &at, r->isar2);
	fp_append(buf, size, &at, " apa=");
	fp_append_u(buf, size, &at, fp_apa(r->isar1));
	fp_append(buf, size, &at, " api=");
	fp_append_u(buf, size, &at, fp_api(r->isar1));
	fp_append(buf, size, &at, " apa3=");
	fp_append_u(buf, size, &at, fp_apa3(r->isar2));
	fp_append(buf, size, &at, " gpa=");
	fp_append_u(buf, size, &at, fp_gpa(r->isar1));
	fp_append(buf, size, &at, " gpi=");
	fp_append_u(buf, size, &at, fp_gpi(r->isar1));
	fp_append(buf, size, &at, " gpa3=");
	fp_append_u(buf, size, &at, fp_gpa3(r->isar2));
	fp_append(buf, size, &at, " en=");
	fp_append_c(buf, size, &at, (r->sctlr & FP_SCTLR_ENIA) ? '1' : '0');
	fp_append_c(buf, size, &at, (r->sctlr & FP_SCTLR_ENIB) ? '1' : '0');
	fp_append_c(buf, size, &at, (r->sctlr & FP_SCTLR_ENDA) ? '1' : '0');
	fp_append_c(buf, size, &at, (r->sctlr & FP_SCTLR_ENDB) ? '1' : '0');
	fp_append(buf, size, &at, " guard=");
	fp_append_u(buf, size, &at, r->guard);
	fp_append(buf, size, &at, " keys_rw=");
	fp_append(buf, size, &at, fp_keys_rw(r));
	fp_append(buf, size, &at, " keys=");
	fp_append_x(buf, size, &at, r->key_ok);
	fp_append(buf, size, &at, "/");
	fp_append_x(buf, size, &at, r->key_rd);
	fp_append(buf, size, &at, "/");
	fp_append_x(buf, size, &at, r->key_ign);
	fp_append(buf, size, &at, "/");
	fp_append_x(buf, size, &at, r->key_trap);
	fp_append(buf, size, &at, " restored=");
	fp_append_u(buf, size, &at, (uint64_t)fp_restored(r));
	fp_append(buf, size, &at, " pac_ok=");
	fp_append_u(buf, size, &at, (uint64_t)fp_pac_ok(r));
	fp_append(buf, size, &at, " auth=");
	for (unsigned k = 0; k < FP_NAUTH; k++) {
		fp_append_c(buf, size, &at, (char)(r->auth[k] < 0 ? '-' : '0' + r->auth[k]));
	}
	fp_append_c(buf, size, &at, (char)(r->ga < 0 ? '-' : '0' + r->ga));
	fp_append(buf, size, &at, " key_used=");
	fp_append_i(buf, size, &at, r->key_used);
	fp_append(buf, size, &at, " auth_fail_ok=");
	fp_append_i(buf, size, &at, fp_auth_fail_ok(r));
	fp_append(buf, size, &at, " fail=");
	for (unsigned k = 0; k < FP_NAUTH; k++) {
		fp_append_c(buf, size, &at, r->fail[k]);
	}
	fp_append(buf, size, &at, " fpac=");
	fp_append_u(buf, size, &at, fp_fpac(r->isar1, r->isar2));
	fp_append(buf, size, &at, " sig=");
	fp_append_x(buf, size, &at, r->sig[0]);
	fp_append(buf, size, &at, " traps=");
	fp_append_u(buf, size, &at, r->traps);
	fp_append(buf, size, &at, " esr=");
	fp_append_x(buf, size, &at, r->last_esr);
	if (r->live) {
		fp_append(buf, size, &at, " mode=live");
	}
	return at;
}

/*
 * FUXI PAC: summary cpus= probed= pauth= keys_rw_ok= pac_ok= auth_fail_ok= fpac= restored= consistent= verdict=
 * verdict: ready (every CPU: keys read back, all round trips, tampering rejected, state restored, identical
 * signatures), else the first failing class: incomplete, no-pauth, keys, pac, auth, restore, mixed.
 * " mode=live" when any CPU ran live (arm64e): its signatures use the boot's shared key set.
 */
static inline unsigned
fp_format_summary(char *buf, unsigned size, const struct fp_result *r, unsigned ncpu)
{
	unsigned at = 0, probed = 0, pauth = 0, keys = 0, pac = 0, fail = 0, fpac = 0, back = 0, consistent = 1, live = 0;
	const struct fp_result *first = 0;
	const char *verdict;
	if (!size) {
		return 0;
	}
	if (ncpu > FP_MAX_CPUS) {
		ncpu = FP_MAX_CPUS;
	}
	for (unsigned c = 0; c < ncpu; c++) {
		if (!r[c].done) {
			continue;
		}
		probed++;
		pauth += fp_auth_level(r[c].isar1, r[c].isar2) != 0;
		keys += fp_keys_rw(&r[c])[0] == 'o' || fp_keys_rw(&r[c])[0] == 'l';
		live |= r[c].live;
		pac += (unsigned)fp_pac_ok(&r[c]);
		fail += fp_auth_fail_ok(&r[c]) == 1;
		fpac += fp_fpac(r[c].isar1, r[c].isar2) != 0;
		back += (unsigned)fp_restored(&r[c]);
		if (!fp_pac_ok(&r[c]) || (r[c].key_ok != FP_KEYS_ALL && !r[c].live)) {
			continue;       /* signatures are comparable only under the test keys (or the shared live keys) */
		}
		if (!first) {
			first = &r[c];
		} else {
			for (unsigned k = 0; k <= FP_NAUTH; k++) {
				consistent &= first->sig[k] == r[c].sig[k];
			}
		}
	}
	verdict = probed < ncpu || !ncpu ? "incomplete" : !pauth ? "no-pauth" : keys < ncpu ? "keys" : pac < ncpu ? "pac" :
	    fail < ncpu ? "auth" : back < ncpu ? "restore" : !consistent ? "mixed" : "ready";
	buf[0] = 0;
	fp_append(buf, size, &at, "FUXI PAC: summary cpus=");
	fp_append_u(buf, size, &at, ncpu);
	fp_append(buf, size, &at, " probed=");
	fp_append_u(buf, size, &at, probed);
	fp_append(buf, size, &at, " pauth=");
	fp_append_u(buf, size, &at, pauth);
	fp_append(buf, size, &at, " keys_rw_ok=");
	fp_append_u(buf, size, &at, keys);
	fp_append(buf, size, &at, " pac_ok=");
	fp_append_u(buf, size, &at, pac);
	fp_append(buf, size, &at, " auth_fail_ok=");
	fp_append_u(buf, size, &at, fail);
	fp_append(buf, size, &at, " fpac=");
	fp_append_u(buf, size, &at, fpac);
	fp_append(buf, size, &at, " restored=");
	fp_append_u(buf, size, &at, back);
	fp_append(buf, size, &at, " consistent=");
	fp_append_u(buf, size, &at, consistent);
	fp_append(buf, size, &at, " verdict=");
	fp_append(buf, size, &at, verdict);
	if (live) {
		fp_append(buf, size, &at, " mode=live");
	}
	return at;
}

/*
 * arm64e (patch 0115), from the fuxi_native_pac words start.s wrote (keys are never printed):
 * FUXI PAC: native mode=native|hyp|unset cpus=<passes>/<ncpu> bad= bad_mpidr= ga= ga_bad= ga_mpidr= verdict=
 * verdict: ok, else the first of: missing (no key set), mixed (a CPU computed another PACGA and parked),
 * hyp-keys (the first CPU's keys did not read back: kernel and processes use the keys found),
 * readback (a later pass did not read back), no-ga (no PACGA reference), cpus (fewer passes than CPUs).
 */
static inline unsigned
fp_format_native(char *buf, unsigned size, const uint64_t *w, unsigned ncpu)
{
	unsigned at = 0;
	const char *verdict;
	if (!size) {
		return 0;
	}
	verdict = w[FP_NATIVE_READY] != 1 ? "missing" : w[FP_NATIVE_GA_BAD] ? "mixed" : w[FP_NATIVE_MODE] == 2 ? "hyp-keys" :
	    w[FP_NATIVE_BAD] ? "readback" : !w[FP_NATIVE_GA_REF] ? "no-ga" : w[FP_NATIVE_CPUS] < ncpu ? "cpus" : "ok";
	buf[0] = 0;
	fp_append(buf, size, &at, "FUXI PAC: native mode=");
	fp_append(buf, size, &at, w[FP_NATIVE_MODE] == 1 ? "native" : w[FP_NATIVE_MODE] == 2 ? "hyp" : "unset");
	fp_append(buf, size, &at, " cpus=");
	fp_append_u(buf, size, &at, w[FP_NATIVE_CPUS]);
	fp_append(buf, size, &at, "/");
	fp_append_u(buf, size, &at, ncpu);
	fp_append(buf, size, &at, " bad=");
	fp_append_u(buf, size, &at, w[FP_NATIVE_BAD]);
	fp_append(buf, size, &at, " bad_mpidr=");
	fp_append_x(buf, size, &at, w[FP_NATIVE_BAD_MPIDR]);
	fp_append(buf, size, &at, " ga=");
	fp_append_u(buf, size, &at, w[FP_NATIVE_GA_REF] != 0);
	fp_append(buf, size, &at, " ga_bad=");
	fp_append_u(buf, size, &at, w[FP_NATIVE_GA_BAD]);
	fp_append(buf, size, &at, " ga_mpidr=");
	fp_append_x(buf, size, &at, w[FP_NATIVE_GA_MPIDR]);
	fp_append(buf, size, &at, " verdict=");
	fp_append(buf, size, &at, verdict);
	return at;
}

#endif /* FUXI_PAC_LOGIC_H */
