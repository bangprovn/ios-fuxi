#!/usr/bin/env python3
"""Host checks for xnu/patches/0114-fuxi-pac-probe.patch (PAC self-test, fuxigpuncimport bit 25).

The patch snapshots must equal xnu/fuxi-pac, every hook must be behind FUXI, bit 25 must be free in the
GPU parsers, the pure formatter/verdict logic is compiled and run here, and when clang-18 and
llvm-objdump-18 are installed fuxi_pac.c is compiled for arm64 Mach-O with XNU's -Weverything -Werror set
(against tiny stand-in headers) and its probe table is disassembled in op order.
"""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATCH = (ROOT/'xnu/patches/0114-fuxi-pac-probe.patch').read_text()
SRC = ROOT/'xnu/fuxi-pac'


def block(path):
    m = re.search(r'^--- a/' + re.escape(path) + r'\n.*?(?=^--- a/|\Z)', PATCH, re.M | re.S)
    assert m, path
    return m[0]


def added(path):
    return ''.join(l[1:] for l in block(path).splitlines(True) if l.startswith('+') and not l.startswith('+++'))


subprocess.run(['python3', str(ROOT/'tools/sync-fuxi-pac.py'), '--check'], check=True, capture_output=True)
for name in ('fuxi_pac.c', 'fuxi_pac_logic.h'):
    assert added('osfmk/arm64/' + name) == (SRC/name).read_text(), name
assert added('osfmk/conf/files.arm64') == 'osfmk/arm64/fuxi_pac.c\tstandard\n'

# Every hook is FUXI-only; the sleh hook skips only what fuxi_pac_trap claims, from EL1.
for path in ('iokit/Kernel/arm/SweetCPU.cpp', 'osfmk/arm64/sleh.c'):
    text = added(path)
    assert text.count('#if defined(FUXI)\n') == text.count('#endif\n') >= 1, path
    for line in text.splitlines():
        assert line.startswith(('#if defined(FUXI)', '#endif', '/*', '\t', 'extern ')) or not line, (path, line)
sleh = block('osfmk/arm64/sleh.c')
assert '\tASSERT_CONTEXT_SANITY(context);\n \n+#if defined(FUXI)\n' in sleh
assert ('+\tif (PSR64_IS_KERNEL(get_saved_state_cpsr(state)) &&\n'
        '+\t    fuxi_pac_trap((uint64_t)get_saved_state_pc(state), esr)) {\n'
        '+\t\tadd_saved_state_pc(state, 4);\n+\t\treturn;\n+\t}\n') in sleh
cpu = added('iokit/Kernel/arm/SweetCPU.cpp')
for fn in ('fuxi_pac_probe_cpu(getCPUNumber());', 'fuxi_pac_log_begin(i);', 'fuxi_pac_log_cpu(i);',
           'fuxi_pac_log_summary(topology->num_cpus);'):
    assert fn in cpu, fn
assert cpu.count('fuxi_pac_log_cpu(i);') == 2  # came alive, and the 5 s timeout before its panic
init = block('iokit/Kernel/arm/SweetCPU.cpp')
assert '+\tfuxi_pac_probe_cpu(getCPUNumber());' in init.split('__atomic_store_n(&initialized, 1')[0].rsplit('@@', 1)[1]

# Default off: the single parse of the arg, bit 25 only, and no register is touched before it.
source = (SRC/'fuxi_pac.c').read_text()
logic = (SRC/'fuxi_pac_logic.h').read_text()
assert source.count('PE_parse_boot_argn(') == 1 and 'PE_parse_boot_argn("fuxigpuncimport", &bits, sizeof(bits))' in source
assert '#define FP_ARG_BIT      0x2000000u' in logic
probe = re.search(r'^fuxi_pac_probe_cpu\(unsigned int cpu\)\n\{\n(.*?)^\}', source, re.M | re.S)[1]
assert probe.index('!fuxi_pac_enabled()') < probe.index('ml_set_interrupts_enabled(FALSE)') < probe.index('fp_probe(r, cpu)')
assert probe.index('fp_probe(r, cpu)') < probe.index('ml_set_interrupts_enabled(istate)')
trap = re.search(r'^fuxi_pac_trap\(uint64_t pc, uint32_t esr\)\n\{\n(.*?)^\}', source, re.M | re.S)[1]
assert trap.index('pc < fp_tab_begin || pc >= fp_tab_end') < trap.index('cpu_number()') < trap.index('fp_armed[cpu]')
# Restore order: keys, then the PACIA with the original key, then SCTLR.
body = re.search(r'^fp_probe\(struct fp_result \*r, unsigned cpu\)\n\{\n(.*?)^\}', source, re.M | re.S)[1]
assert body.index('FP_STEP_RESTORE') < body.index('r->key_used =') < body.index('FP_OP_SCTLR_WR, r->sctlr, 0, 0')

# Bit 25 is not read by any GPU parser of fuxigpuncimport; 22 is the DPU survey, 23-24 are reserved for later DPU stages,
# 8/16/32 are the present-ordering lane.
used = set()
for f in ('xnu/fuxi-adreno/fuxi_gpu_abi_backend.inc', 'xnu/fuxi-adreno/fuxi_gpu_scanout.inc'):
    text = (ROOT/f).read_text()
    used |= {int(n) for n in re.findall(r'\(bits>>(\d+)\)', text)}
    used |= {int(v, 16).bit_length() - 1 for v in re.findall(r'nc & (0x[0-9a-f]+)u', text)}
    used |= {0} if re.search(r'=bits&1;', text) else set()
assert used and 25 not in used and {0, 1, 2, 6, 7, 9, 20, 21, 22} <= used, sorted(used)
assert 'fg_scanout_dpu_dump = (nc & 0x400000u) != 0;' in (ROOT/'xnu/fuxi-adreno/fuxi_gpu_scanout.inc').read_text()
assert (1 << 25) not in (1 << 22, 1 << 23, 1 << 24, 8, 16, 32) and 2948728 & (7 << 22) == 0 and 2948728 + (1 << 25) == 36503160

TEST = r'''
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <assert.h>
#include "fuxi_pac_logic.h"
static struct fp_result good(void)
{
	struct fp_result r;
	memset(&r, 0, sizeof(r));
	r.done = 1; r.step = FP_STEP_DONE; r.midr = 0x410fd461; r.isar1 = 0x0000000001000050ULL; r.isar2 = 0;
	r.sctlr = 0x30d0199d; r.guard = 1; r.key_rd = r.key_ok = r.key_back = FP_KEYS_ALL; r.sctlr_back = 1;
	for (unsigned k = 0; k < FP_NAUTH; k++) { r.auth[k] = 1; r.fail[k] = 'P'; r.sig[k] = 0x7f00fe0000001230ULL + k; }
	r.ga = 1; r.key_used = 1; r.sig[FP_NAUTH] = 0xabcd123400000000ULL;
	return r;
}
static int line_is(const char *got, const char *want)
{
	if (strcmp(got, want)) { printf("got  %s\nwant %s\n", got, want); return 0; }
	return 1;
}
int main(void)
{
	char line[400], small[24];
	assert(!fp_arg_enabled(0, FP_ARG_BIT) && !fp_arg_enabled(1, 2948728) && fp_arg_enabled(1, 36503160));
	assert(!fp_arg_enabled(1, 8 | 16 | 32 | 0x200000 | 0x400000 | 0x800000 | 0x1000000) && fp_arg_enabled(1, 0xffffffffu));
	/* ISAR fields: APA 7:4 API 11:8 GPA 27:24 GPI 31:28; ISAR2 GPA3 11:8 APA3 15:12. */
	assert(fp_apa(0x50) == 5 && fp_api(0x300) == 3 && fp_gpa(0x1000000) == 1 && fp_gpi(0x10000000) == 1);
	assert(fp_gpa3(0x100) == 1 && fp_apa3(0x4000) == 4);
	assert(fp_auth_level(0, 0) == 0 && !fp_generic(0, 0) && fp_fpac(0, 0) == 0);
	assert(fp_auth_level(0x10, 0) == 1 && fp_fpac(0x30, 0) == 0 && fp_fpac(0x40, 0) == 1 && fp_fpac(0x50, 0) == 2);
	assert(fp_fpac(0x400, 0) == 1 && fp_fpac(0, 0x5000) == 2 && fp_generic(0, 0x100) && fp_generic(0x10000000, 0));
	assert(fp_key_pattern(0) != fp_key_pattern(1) && fp_key_pattern(9) != 0);
	assert(!strcmp(fp_step_name(FP_STEP_GUARD), "guard") && !strcmp(fp_step_name(FP_STEP_DONE), "done") &&
	    !strcmp(fp_step_name(99), "?"));

	struct fp_result r[FP_MAX_CPUS];
	for (unsigned c = 0; c < FP_MAX_CPUS; c++) { r[c] = good(); }
	unsigned n = fp_format(line, sizeof(line), 3, &r[3]);
	if (!line_is(line, "FUXI PAC: cpu=3 midr=0x410fd461 isar1=0x1000050 isar2=0x0 apa=5 api=0 apa3=0 gpa=1 gpi=0"
	    " gpa3=0 en=0000 guard=1 keys_rw=ok keys=0x3ff/0x3ff/0x0/0x0 restored=1 pac_ok=1 auth=11111 key_used=1"
	    " auth_fail_ok=1 fail=PPPP fpac=2 sig=0x7f00fe0000001230 traps=0 esr=0x0")) return 1;
	assert(n == strlen(line) && n < sizeof(line) - 1);
	n = fp_format_summary(line, sizeof(line), r, 7);
	if (!line_is(line, "FUXI PAC: summary cpus=7 probed=7 pauth=7 keys_rw_ok=7 pac_ok=7 auth_fail_ok=7 fpac=7"
	    " restored=7 consistent=1 verdict=ready")) return 1;

	/* FPAC fault on cpu 1, an ignored key write elsewhere is keys; a different algorithm on one core is mixed. */
	r[1].fail[0] = 'F'; r[1].traps = 2; r[1].last_esr = 0x72000000u;
	fp_format_summary(line, sizeof(line), r, 7);
	assert(strstr(line, "auth_fail_ok=7 ") && strstr(line, "verdict=ready"));
	r[5].sig[2] ^= 1;
	fp_format_summary(line, sizeof(line), r, 7);
	assert(strstr(line, "consistent=0 verdict=mixed"));
	r[5] = good();
	r[4].fail[3] = 'X';
	fp_format(line, sizeof(line), 4, &r[4]);
	assert(strstr(line, " auth_fail_ok=0 fail=PPPX "));
	fp_format_summary(line, sizeof(line), r, 7);
	assert(strstr(line, "auth_fail_ok=6 ") && strstr(line, "verdict=auth"));
	r[4] = good();
	r[6].key_ok = 0; r[6].key_ign = FP_KEYS_ALL; r[6].key_used = -1;
	fp_format(line, sizeof(line), 6, &r[6]);
	assert(strstr(line, " keys_rw=ignored keys=0x0/0x3ff/0x3ff/0x0 ") && strstr(line, " key_used=- "));
	fp_format_summary(line, sizeof(line), r, 7);
	assert(strstr(line, "keys_rw_ok=6 ") && strstr(line, "consistent=1 verdict=keys"));
	r[6].key_trap = 0x300; assert(!strcmp(fp_keys_rw(&r[6]), "trap"));
	r[6] = good(); r[6].key_ok = 0x0ff; assert(!strcmp(fp_keys_rw(&r[6]), "partial"));
	r[6] = good(); r[6].key_back = 0x3fe;
	fp_format_summary(line, sizeof(line), r, 7);
	assert(strstr(line, "restored=6 ") && strstr(line, "verdict=restore"));
	r[6] = good(); r[6].ga = -1;                      /* no generic auth: PACGA is not required */
	assert(fp_pac_ok(&r[6]));
	r[6].auth[1] = 0; assert(!fp_pac_ok(&r[6]));
	fp_format_summary(line, sizeof(line), r, 7);
	assert(strstr(line, "pac_ok=6 ") && strstr(line, "verdict=pac"));
	for (unsigned k = 0; k < FP_NAUTH; k++) { r[6].fail[k] = '-'; }
	assert(fp_auth_fail_ok(&r[6]) == -1);
	fp_format(line, sizeof(line), 6, &r[6]);
	assert(strstr(line, " auth=1011- ") && strstr(line, " auth_fail_ok=- fail=---- "));
	r[6].fail[2] = 'E'; assert(fp_auth_fail_ok(&r[6]) == 0);

	/* A hung probe: startAll prints the last step before its panic. */
	r[2] = good(); r[2].done = 0; r[2].step = FP_STEP_KEYWR;
	fp_format(line, sizeof(line), 2, &r[2]);
	if (!line_is(line, "FUXI PAC: cpu=2 incomplete step=keywr")) return 1;
	fp_format_summary(line, sizeof(line), r, 7);
	assert(strstr(line, "probed=6 ") && strstr(line, "verdict=incomplete"));
	for (unsigned c = 0; c < FP_MAX_CPUS; c++) { r[c] = good(); r[c].isar1 = 0x11000000ULL; r[c].auth[0] = -1; }
	fp_format_summary(line, sizeof(line), r, 7);
	assert(strstr(line, "pauth=0 ") && strstr(line, "verdict=no-pauth"));
	fp_format_summary(line, sizeof(line), r, 0);
	assert(strstr(line, "verdict=incomplete"));

	/* arm64e live mode (patch 0115): keys read, never written; signatures under the boot's shared key set. */
	for (unsigned c = 0; c < FP_MAX_CPUS; c++) { r[c] = good(); r[c].live = 1; r[c].key_ok = 0; r[c].key_used = -1; }
	fp_format(line, sizeof(line), 0, &r[0]);
	assert(strstr(line, " keys_rw=live keys=0x0/0x3ff/0x0/0x0 restored=1 ") && strstr(line, " key_used=- "));
	assert(!strcmp(line + strlen(line) - 17, "esr=0x0 mode=live"));
	fp_format_summary(line, sizeof(line), r, 7);
	if (!line_is(line, "FUXI PAC: summary cpus=7 probed=7 pauth=7 keys_rw_ok=7 pac_ok=7 auth_fail_ok=7 fpac=7"
	    " restored=7 consistent=1 verdict=ready mode=live")) return 1;
	r[3].sig[0] ^= 1;
	fp_format_summary(line, sizeof(line), r, 7);
	assert(strstr(line, "consistent=0 verdict=mixed mode=live"));
	r[3].sig[0] ^= 1; r[3].key_rd = 0x3fe; r[3].key_back = 0x3fe;
	assert(!strcmp(fp_keys_rw(&r[3]), "partial"));
	fp_format_summary(line, sizeof(line), r, 7);
	assert(strstr(line, "keys_rw_ok=6 ") && strstr(line, "verdict=keys mode=live"));
	r[3].key_trap = 1; assert(!strcmp(fp_keys_rw(&r[3]), "trap"));

	/* start.s key-init words: never prints a key. */
	uint64_t w[FP_NATIVE_WORDS];
	memset(w, 0, sizeof(w));
	fp_format_native(line, sizeof(line), w, 7);
	assert(strstr(line, "FUXI PAC: native mode=unset cpus=0/7 ") && strstr(line, " verdict=missing"));
	for (unsigned i = 0; i < 10; i++) { w[i] = 0x1111111111111111ULL * (i + 1); }
	w[FP_NATIVE_READY] = 1; w[FP_NATIVE_MODE] = 1; w[FP_NATIVE_CPUS] = 7; w[FP_NATIVE_GA_REF] = 0xabcd123400000000ULL;
	n = fp_format_native(line, sizeof(line), w, 7);
	if (!line_is(line, "FUXI PAC: native mode=native cpus=7/7 bad=0 bad_mpidr=0x0 ga=1 ga_bad=0 ga_mpidr=0x0 verdict=ok"))
		return 1;
	assert(n == strlen(line) && !strstr(line, "1111111") && !strstr(line, "abcd"));
	w[FP_NATIVE_CPUS] = 6; fp_format_native(line, sizeof(line), w, 7); assert(strstr(line, " verdict=cpus"));
	w[FP_NATIVE_GA_REF] = 0; fp_format_native(line, sizeof(line), w, 7); assert(strstr(line, " ga=0 ") && strstr(line, " verdict=no-ga"));
	w[FP_NATIVE_BAD] = 1; w[FP_NATIVE_BAD_MPIDR] = 0x81000300ULL;
	fp_format_native(line, sizeof(line), w, 7); assert(strstr(line, " bad=1 bad_mpidr=0x81000300 ") && strstr(line, " verdict=readback"));
	w[FP_NATIVE_MODE] = 2; fp_format_native(line, sizeof(line), w, 7); assert(strstr(line, "mode=hyp ") && strstr(line, " verdict=hyp-keys"));
	w[FP_NATIVE_GA_BAD] = 1; w[FP_NATIVE_GA_MPIDR] = 0x81000700ULL;
	fp_format_native(line, sizeof(line), w, 7); assert(strstr(line, " ga_bad=1 ga_mpidr=0x81000700 verdict=mixed"));
	for (unsigned i = 10; i < FP_NATIVE_WORDS; i++) { w[i] = ~0ULL; }
	n = fp_format_native(line, 256, w, FP_MAX_CPUS);
	assert(n < 220 && strstr(line, "mode=unset"));
	assert(fp_format_native(small, 0, w, 7) == 0);

	/* Worst-case line fits the kernel buffer; truncation stays terminated. */
	r[0] = good(); r[0].midr = r[0].isar1 = r[0].isar2 = r[0].sctlr = ~0ULL; r[0].sig[0] = ~0ULL;
	r[0].traps = 0xffffffffu; r[0].last_esr = 0xffffffffu; r[0].key_trap = r[0].key_ign = FP_KEYS_ALL; r[0].live = 1;
	n = fp_format(line, sizeof(line), 7, &r[0]);
	assert(n == strlen(line) && n < 370 && strstr(line, " en=1111 ") && strstr(line, " keys_rw=trap "));
	n = fp_format(small, sizeof(small), 7, &r[0]);
	assert(n == sizeof(small) - 1 && strlen(small) == n);
	assert(fp_format(small, 0, 7, &r[0]) == 0 && fp_format_summary(small, 0, r, 7) == 0);
	n = fp_format_summary(line, 256, r, FP_MAX_CPUS);
	assert(n < 200);
	puts("ok");
	return 0;
}
'''

WARN = ('-Weverything -Werror -Wno-pedantic -Wno-bad-function-cast -Wno-bitwise-instead-of-logical -Wno-c++-compat '
        '-Wno-c++98-compat -Wno-conditional-uninitialized -Wno-covered-switch-default -Wno-disabled-macro-expansion '
        '-Wno-documentation-unknown-command -Wno-extra-semi-stmt -Wno-format-non-iso -Wno-language-extension-token '
        '-Wno-missing-variable-declarations -Wno-packed -Wno-padded -Wno-partial-availability -Wno-reserved-id-macro '
        '-Wno-shift-sign-overflow -Wno-switch-enum -Wno-unaligned-access -Wno-undef -Wno-unused-macros '
        '-Wno-used-but-marked-unused -Wno-variadic-macros -Wno-vla -Wno-zero-length-array -Wno-unknown-warning-option '
        '-Wno-anon-enum-enum-conversion -Wno-error=declaration-after-statement -Wno-sign-compare -Wno-sign-conversion '
        '-Wpointer-arith -Wshorten-64-to-32 -Wno-unsafe-buffer-usage').split()  # XNU MakeInc.def + osfmk Makefile.arm64
MOCK = {
    'pexpert/arm64/board_config.h': '#define FUXI 1\n',
    'kern/cpu_number.h': 'extern int cpu_number(void);\n',
    'kern/misc_protos.h': 'extern int printf(const char *, ...) __attribute__((format(printf, 1, 2)));\n',
    'machine/machine_routines.h': 'typedef int boolean_t;\n#define FALSE 0\nboolean_t ml_set_interrupts_enabled(boolean_t enable);\n',
    'pexpert/pexpert.h': 'extern void kprintf(const char *, ...) __attribute__((format(printf, 1, 2)));\n'
                         'extern boolean_t PE_parse_boot_argn(const char *, void *, int);\n',
}
# fuxi_pac.c on the host with its asm table replaced by a model of the CPU (S224 ID registers, FPAC):
# the arm64e (live) build must never write a key or SCTLR, the arm64 build must write and restore both.
SIM = r'''
#include <string.h>
#define FS_TAB 0x10000ULL
#define FS_SCTLR 0x30d0199dULL
static int fs_cpu;
static uint64_t fs_keys[FP_NKEYS], fs_sctlr;
static unsigned fs_key_writes, fs_sctlr_writes;
int cpu_number(void) { return fs_cpu; }
void kprintf(const char *fmt, ...) { (void)fmt; }
boolean_t ml_set_interrupts_enabled(boolean_t enable) { return enable; }
boolean_t PE_parse_boot_argn(const char *name, void *v, int size)
{
	(void)size;
	if (strcmp(name, "fuxigpuncimport")) return 0;
	*(unsigned *)v = 36503160u;
	return 1;
}
static uint64_t fs_mac(unsigned k, uint64_t p, uint64_t m)
{
	uint64_t h = (p ^ m ^ fs_keys[2 * k] ^ (fs_keys[2 * k + 1] << 1)) * 0x9e3779b97f4a7c15ULL;
	return (h >> 40) | 1;
}
static uint64_t fs_fault(unsigned op, unsigned esr)
{
	(void)fuxi_pac_trap(FS_TAB + 8 * op, esr);
	return 0;
}
uint64_t fuxi_pac_stub(uint64_t op, uint64_t a, uint64_t b)
{
	if (op == FP_OP_COUNT) return FS_TAB;
	if (op == FP_OP_COUNT + 1) return FS_TAB + 8 * FP_OP_COUNT;
	if (op == FP_OP_ISAR1) return 0x10111101211052ULL;
	if (op == FP_OP_ISAR2) return 0;
	if (op == FP_OP_SCTLR_RD) return fs_sctlr;
	if (op == FP_OP_SCTLR_WR) { fs_sctlr = a; fs_sctlr_writes++; return 0; }
	if (op == FP_OP_UDF) return fs_fault((unsigned)op, 0);
	if (op < FP_OP_KEY_WR) return fs_keys[op - FP_OP_KEY_RD];
	if (op < FP_OP_PAC) { fs_keys[op - FP_OP_KEY_WR] = a; fs_key_writes++; return 0; }
	if (op == FP_OP_PACGA) return (((a ^ b ^ fs_keys[8] ^ fs_keys[9]) * 0x94d049bb133111ebULL) | (1ULL << 63)) & ~0xffffffffULL;
	unsigned k = (unsigned)(op - FP_OP_PAC) / 2;
	if (!((op - FP_OP_PAC) & 1)) return a | (fs_mac(k, a, b) << 48);
	uint64_t p = a & 0xffffffffffffULL;
	return (a >> 48) == (fs_mac(k, p, b) & 0xffff) ? p : fs_fault((unsigned)op, 0x1cu << 26);
}
#if FP_LIVE
uint64_t fuxi_native_pac[FP_NATIVE_WORDS] = { [FP_NATIVE_READY] = 1, [FP_NATIVE_MODE] = 1, [FP_NATIVE_CPUS] = 7,
	[FP_NATIVE_GA_REF] = 0x1234000000000000ULL };
#endif
int main(void)
{
	for (unsigned i = 0; i < FP_NKEYS; i++) fs_keys[i] = 0xfeedf00d00000000ULL + i;
	fs_sctlr = FP_LIVE ? FS_SCTLR | FP_SCTLR_EN : FS_SCTLR;
	for (fs_cpu = 0; fs_cpu < 7; fs_cpu++) {
		fuxi_pac_probe_cpu((unsigned)fs_cpu);
		fuxi_pac_log_cpu((unsigned)fs_cpu);
		for (unsigned i = 0; i < FP_NKEYS; i++) if (fs_keys[i] != 0xfeedf00d00000000ULL + i) return 2;
		if (fs_sctlr != (FP_LIVE ? FS_SCTLR | FP_SCTLR_EN : FS_SCTLR)) return 3;
	}
	fuxi_pac_log_summary(7);
	printf("writes keys=%u sctlr=%u\n", fs_key_writes, fs_sctlr_writes);
	return 0;
}
'''

TABLE = ['mrs x0, ID_AA64ISAR1_EL1', 'mrs x0, ID_AA64ISAR2_EL1', 'mrs x0, SCTLR_EL1', 'msr SCTLR_EL1, x1', 'udf #0x0'] + \
    ['mrs x0, AP%sKey%s_EL1' % (k, h) for k in ('IA', 'IB', 'DA', 'DB', 'GA') for h in ('Lo', 'Hi')] + \
    ['msr AP%sKey%s_EL1, x1' % (k, h) for k in ('IA', 'IB', 'DA', 'DB', 'GA') for h in ('Lo', 'Hi')] + \
    ['%s%s x1, x2' % (o, k) for k in ('ia', 'ib', 'da', 'db') for o in ('pac', 'aut')] + ['pacga x1, x1, x2']

with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    (tmp/'t.c').write_text(TEST)
    subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-I', str(SRC), str(tmp/'t.c'), '-o', str(tmp/'t')],
                   check=True)
    run = subprocess.run([str(tmp/'t')], capture_output=True, text=True)
    assert run.returncode == 0 and run.stdout == 'ok\n', run.stdout + run.stderr
    cc, dis = shutil.which('clang-18'), shutil.which('llvm-objdump-18')
    if cc and dis:
        for name, text in MOCK.items():
            (tmp/'mock'/name).parent.mkdir(parents=True, exist_ok=True)
            (tmp/'mock'/name).write_text(text)
        res = subprocess.run([cc, '-print-resource-dir'], check=True, capture_output=True, text=True).stdout.strip()
        subprocess.run([cc, '-target', 'arm64-apple-macos13', '-ffreestanding', '-nostdlibinc', '-isystem', res + '/include',
                        '-I', str(tmp/'mock'), '-std=gnu11', '-Os', *WARN, '-c', str(SRC/'fuxi_pac.c'), '-o', str(tmp/'p.o')],
                       check=True)
        out = subprocess.run([dis, '-d', '--mattr=+v8.3a,+pauth', '--no-show-raw-insn', str(tmp/'p.o')], check=True,
                             capture_output=True, text=True).stdout
        insns = [re.sub(r'\s+', ' ', l.split(':', 1)[1].strip()) for l in out.splitlines() if re.match(r'\s+[0-9a-f]+:', l)]
        start = insns.index(TABLE[0])
        for i, want in enumerate(TABLE):  # entry i of FP_OP_* is 8 bytes at table + 8 * i
            assert insns[start + 2 * i] == want, (i, insns[start + 2 * i], want)
            assert insns[start + 2 * i + 1] == 'ret' or insns[start + 2 * i + 1].startswith('b '), insns[start + 2 * i + 1]
        print('PASS: fuxi_pac.c compiles for arm64 Mach-O under XNU -Weverything -Werror; %d-entry probe table in op order'
              % len(TABLE))
        # arm64e: the live path and the native line compile under the same warnings (host clang has no ptrauth codegen).
        subprocess.run([cc, '-target', 'arm64e-apple-macos13', '-D__arm64e__=1', '-ffreestanding', '-nostdlibinc',
                        '-isystem', res + '/include', '-I', str(tmp/'mock'), '-std=gnu11', '-Os', *WARN,
                        '-c', str(SRC/'fuxi_pac.c'), '-o', str(tmp/'pe.o')], check=True)
        syms = subprocess.run([shutil.which('llvm-nm-18') or 'nm', str(tmp/'pe.o')], check=True, capture_output=True,
                              text=True).stdout
        assert re.search(r'\bU _fuxi_native_pac$', syms, re.M), syms
        src = (SRC/'fuxi_pac.c').read_text()
        body = re.sub(r'\n__asm__\(\n.*?\n\t\);\n', '\n', src, count=1, flags=re.S)
        assert '_fuxi_pac_stub:' in src and '_fuxi_pac_stub:' not in body and 'Lfp_tab' not in body
        (tmp/'sim_src.c').write_text(body)
        (tmp/'sim.c').write_text('#include "sim_src.c"\n#include <stdio.h>\n' + SIM)
        lines = {}
        for live in (0, 1):
            flags = ['-D__arm64e__=1'] if live else []
            subprocess.run([cc, '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function', *flags,
                            '-D__builtin_arm_rsr64(r)=0x411fd4e0ULL', '-I', str(tmp/'mock'), '-I', str(SRC), '-I', str(tmp),
                            str(tmp/'sim.c'), '-o', str(tmp/'sim')], check=True)
            run = subprocess.run([str(tmp/'sim')], capture_output=True, text=True)
            assert run.returncode == 0, (live, run.returncode, run.stdout)
            lines[live] = run.stdout.splitlines()
        arm64, arm64e = lines[0], lines[1]
        assert arm64[-1] == 'writes keys=140 sctlr=14', arm64[-1]          # 7 CPUs x (10 test + 10 restore), (set + restore)
        assert arm64[-2] == ('FUXI PAC: summary cpus=7 probed=7 pauth=7 keys_rw_ok=7 pac_ok=7 auth_fail_ok=7 fpac=7'
                             ' restored=7 consistent=1 verdict=ready'), arm64[-2]
        assert all(' keys_rw=ok ' in l and ' key_used=1 ' in l and ' fail=FFFF fpac=2 ' in l and 'mode=live' not in l
                   for l in arm64[:7]), arm64[:7]
        assert not any('native' in l for l in arm64)
        assert arm64e[-1] == 'writes keys=0 sctlr=0', arm64e[-1]
        assert arm64e[-3] == ('FUXI PAC: native mode=native cpus=7/7 bad=0 bad_mpidr=0x0 ga=1 ga_bad=0 ga_mpidr=0x0'
                              ' verdict=ok'), arm64e[-3]
        assert arm64e[-2] == ('FUXI PAC: summary cpus=7 probed=7 pauth=7 keys_rw_ok=7 pac_ok=7 auth_fail_ok=7 fpac=7'
                              ' restored=7 consistent=1 verdict=ready mode=live'), arm64e[-2]
        assert all(' en=1111 ' in l and ' keys_rw=live keys=0x0/0x3ff/0x0/0x0 ' in l and ' key_used=- ' in l and
                   l.endswith(' mode=live') for l in arm64e[:7]), arm64e[:7]
        print('PASS: arm64e build compiles; host model: live probe writes no key/SCTLR (arm64 writes 140/14), both ready')
    else:
        print('SKIP: clang-18/llvm-objdump-18 not installed; strict arm64 compile not checked')
print('PASS: 0114 matches xnu/fuxi-pac; hooks FUXI-only; bit 25 free (22-24 DPU); formatter and verdicts')
