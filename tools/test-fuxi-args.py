#!/usr/bin/env python3
"""Patch 0122 (boot-args from /chosen fuxi-args) on the host.

1. xnu/fuxi-args/fuxi_boot_args.h: the merge, its bounds and every refusal, built with XNU's warnings as errors.
2. The kernel block of hooks.diff (bootargs.c), built against stub DT calls: what the parser scans and the
   FUXI ARGS lines, for a property that is there, absent, empty or refused; and the pe_init.c hooks in their places.
3. xnu/fuxi-args/adt-safe.txt: each name is read by kernel code and by no user-space program or host tool here.
4. With --xnu DIR (stock xnu-8796.141.3): the real parser over the merged line, so PE_parse_boot_argn and
   PE_parse_boot_arg_str see the property's words as if they were appended to the line.
"""
import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / 'xnu/fuxi-args'
HOOKS = (SRC / 'hooks.diff').read_text()

# XNU's makedefs/MakeInc.def: -Weverything -Werror with these off (and pexpert's -Wno-sign-conversion).
XNU_WARN = ('-Weverything -Werror -Wno-pedantic -Wno-bad-function-cast -Wno-c++-compat -Wno-c++98-compat '
            '-Wno-conditional-uninitialized -Wno-covered-switch-default -Wno-disabled-macro-expansion '
            '-Wno-documentation-unknown-command -Wno-extra-semi-stmt -Wno-format-non-iso -Wno-language-extension-token '
            '-Wno-missing-variable-declarations -Wno-packed -Wno-padded -Wno-partial-availability -Wno-reserved-id-macro '
            '-Wno-shift-sign-overflow -Wno-switch-enum -Wno-unaligned-access -Wno-undef -Wno-unused-macros '
            '-Wno-used-but-marked-unused -Wno-variadic-macros -Wno-vla -Wno-zero-length-array -Wno-unknown-warning-option '
            '-Wno-sign-compare -Wno-sign-conversion -Wno-declaration-after-statement '
            # host-only: the test's own libc calls and pointer arithmetic
            '-Wno-unsafe-buffer-usage -Wno-reserved-identifier -Wno-reserved-macro-identifier').split()

HEADER_TEST = r'''
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "fuxi_boot_args.h"

static char out[608 + FUXI_ARGS_MAX];
static unsigned int at, taken;

static int merge(const char *line, const void *prop, unsigned int size)
{
	memset(out, 'x', sizeof(out));
	return fuxi_args_merge(line, 608, prop, size, out, (unsigned int)sizeof(out), &at, &taken);
}

int main(void)
{
	static char big[4096], line[700];

	assert(merge("debug=0xc serial=3", "fuxiremote=1 fxinj=1", 21) == FUXI_ARGS_TAKEN);
	assert(!strcmp(out, "debug=0xc serial=3 fuxiremote=1 fxinj=1") && at == 19 && taken == 20);
	assert(!strcmp(out + at, "fuxiremote=1 fxinj=1"));
	/* bytes after the NUL are not looked at; an empty line still works */
	assert(merge("", "a=1\0\x01\xff", 6) == FUXI_ARGS_TAKEN && !strcmp(out, " a=1") && at == 1 && taken == 3);
	/* refusals leave out untouched and say nothing was taken */
	assert(merge("x=1", "", 1) == FUXI_ARGS_EMPTY && out[0] == 'x' && out[1] == 'x' && !at && !taken);
	assert(merge("x=1", "a=1", 3) == FUXI_ARGS_NO_NUL && out[0] == 'x' && !taken);
	assert(merge("x=1", "", 0) == FUXI_ARGS_NO_NUL);
	assert(merge("x=1", "a=1\tb=2", 8) == FUXI_ARGS_BYTE && !taken);
	assert(merge("x=1", "a=1\nb=2", 8) == FUXI_ARGS_BYTE);
	assert(merge("x=1", "a=\xc3\xa9", 5) == FUXI_ARGS_BYTE);
	assert(merge("x=1", "a=\x7f", 4) == FUXI_ARGS_BYTE);
	/* the longest text, FUXI_ARGS_MAX - 1, with the longest line */
	memset(line, 'L', sizeof(line));
	line[sizeof(line) - 1] = 0;                     /* not NUL-terminated within 608: 607 characters are taken */
	memset(big, 'a', sizeof(big));
	big[FUXI_ARGS_MAX - 1] = 0;
	assert(merge(line, big, FUXI_ARGS_MAX) == FUXI_ARGS_TAKEN && taken == FUXI_ARGS_MAX - 1 && at == 608);
	assert(strlen(out) == 607 + 1 + FUXI_ARGS_MAX - 1 && out[606] == 'L' && out[607] == ' ' && out[608] == 'a');
	big[FUXI_ARGS_MAX - 1] = 'a';
	big[FUXI_ARGS_MAX] = 0;
	assert(merge("x=1", big, FUXI_ARGS_MAX + 1) == FUXI_ARGS_LONG && !taken);
	/* a smaller buffer is refused, not overrun */
	{
		char small[8];
		memset(small, 'y', sizeof(small));
		assert(fuxi_args_merge("ab", 608, "cdef", 5, small, 8, &at, &taken) == FUXI_ARGS_TAKEN && !strcmp(small, "ab cdef"));
		memset(small, 'y', sizeof(small));
		assert(fuxi_args_merge("ab", 608, "cdefg", 6, small, 8, &at, &taken) == FUXI_ARGS_ROOM && small[0] == 'y');
		assert(fuxi_args_merge("ab", 608, "c", 2, small, 1, &at, &taken) == FUXI_ARGS_ROOM);
		assert(fuxi_args_merge("ab", 608, "c", 2, small, 0, &at, &taken) == FUXI_ARGS_ROOM);
	}
	for (int r = FUXI_ARGS_TAKEN; r <= FUXI_ARGS_ROOM + 1; r++) {
		assert(fuxi_args_why(r) && *fuxi_args_why(r));
	}
	puts("fuxi_boot_args.h ok");
	return 0;
}
'''

# Stubs for the kernel block: one DT property, chosen by the test, and the real line.
BLOCK_STUBS = r'''
#include <assert.h>
#include <stdio.h>
#include <string.h>
#define FUXI 1
#define BOOT_LINE_LENGTH 608
#define kSuccess 0
#define kError (-1)
typedef struct fuxi_test_dt *DTEntry;
static char test_line[BOOT_LINE_LENGTH];
static const void *test_prop;
static unsigned int test_size;
static int test_chosen = 1;
static char *PE_boot_args(void) { return test_line; }
static int SecureDTLookupEntry(const DTEntry from, const char *path, DTEntry *found)
{
	(void)from;
	assert(!strcmp(path, "/chosen"));
	*found = (DTEntry)&test_chosen;
	return test_chosen ? kSuccess : kError;
}
static int SecureDTGetProperty(const DTEntry entry, const char *name, void const **value, unsigned int *size)
{
	assert(entry == (DTEntry)&test_chosen && !strcmp(name, "fuxi-args"));
	if (!test_prop) {
		return kError;
	}
	*value = test_prop;
	*size = test_size;
	return kSuccess;
}
'''

BLOCK_TEST = r'''
static void reset(const char *line, const void *prop, unsigned int size)
{
	snprintf(test_line, sizeof(test_line), "%s", line);
	test_prop = prop;
	test_size = size;
	fuxi_args_at = fuxi_args_taken = fuxi_args_size = 0;
	fuxi_args_result = -1;
	memset(fuxi_boot_line, 0, sizeof(fuxi_boot_line));
}

int main(void)
{
	/* none: the line itself, and one FUXI ARGS line saying so */
	reset("debug=0xc serial=3", NULL, 0);
	fuxi_boot_args_adopt();
	assert(pe_parse_args() == test_line);
	fuxi_boot_args_say();
	/* no /chosen at all */
	reset("debug=0xc", NULL, 0);
	test_chosen = 0;
	fuxi_boot_args_adopt();
	test_chosen = 1;
	assert(pe_parse_args() == test_line && fuxi_args_result == -1);
	/* taken */
	reset("debug=0xc serial=3", "fuxiremote=1 fxinj=1 fuxisbext=1", 33);
	fuxi_boot_args_adopt();
	assert(!strcmp(pe_parse_args(), "debug=0xc serial=3 fuxiremote=1 fxinj=1 fuxisbext=1"));
	assert(pe_parse_args() == fuxi_boot_line && fuxi_args_taken == 32 && fuxi_args_size == 33);
	assert(!strcmp(test_line, "debug=0xc serial=3"));       /* PE_boot_args() is still the real line */
	fuxi_boot_args_say();
	/* refused and empty: the line alone */
	reset("debug=0xc", "a=1\nb=2", 8);
	fuxi_boot_args_adopt();
	assert(pe_parse_args() == test_line && fuxi_args_result == FUXI_ARGS_BYTE);
	fuxi_boot_args_say();
	reset("debug=0xc", "", 1);
	fuxi_boot_args_adopt();
	assert(pe_parse_args() == test_line && fuxi_args_result == FUXI_ARGS_EMPTY);
	fuxi_boot_args_say();
	return 0;
}
'''

BLOCK_SAID = [
    'FUXI ARGS: no /chosen fuxi-args, the boot line alone',
    'FUXI ARGS: 32 bytes from /chosen fuxi-args: fuxiremote=1 fxinj=1 fuxisbext=1',
    'FUXI ARGS: /chosen fuxi-args (8 bytes) not taken, a byte in it is not printable ASCII: the boot line alone',
    'FUXI ARGS: /chosen fuxi-args (1 bytes) not taken, it is empty: the boot line alone',
]

# The stock parser (--xnu): stub headers for bootargs.c, then PE_parse_boot_argn over the merged line.
STOCK_PEXPERT = r'''
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef int boolean_t;
#define TRUE 1
#define FALSE 0
#define BOOT_LINE_LENGTH 608
char *PE_boot_args(void);
boolean_t PE_parse_boot_argn(const char *arg_string, void *arg_ptr, int max_len);
boolean_t PE_parse_boot_arg_str(const char *arg_string, char *arg_ptr, int strlen);
'''
STOCK_DT = r'''
#define kSuccess 0
#define kError (-1)
typedef struct fuxi_test_dt *DTEntry;
int SecureDTLookupEntry(const DTEntry from, const char *path, DTEntry *found);
int SecureDTGetProperty(const DTEntry entry, const char *name, void const **value, unsigned int *size);
'''
STOCK_TEST = r'''
#include <assert.h>
#include <stdint.h>
#include <string.h>
#include <pexpert/pexpert.h>
#include <pexpert/device_tree.h>
void fuxi_boot_args_adopt(void);
static char line[BOOT_LINE_LENGTH];
static const char *prop;
char *PE_boot_args(void) { return line; }
int IODTGetDefault(const char *key, void *infoAddr, unsigned int infoSize);
int IODTGetDefault(const char *key, void *infoAddr, unsigned int infoSize) { (void)key; (void)infoAddr; (void)infoSize; return 1; }
int SecureDTLookupEntry(const DTEntry from, const char *path, DTEntry *found) { (void)from; (void)path; *found = (DTEntry)&line; return kSuccess; }
int SecureDTGetProperty(const DTEntry entry, const char *name, void const **value, unsigned int *size)
{
	(void)entry;
	if (!prop || strcmp(name, "fuxi-args")) {
		return kError;
	}
	*value = prop;
	*size = (unsigned int)strlen(prop) + 1;
	return kSuccess;
}

int main(void)
{
	uint32_t u = 0;
	uint64_t q = 0;
	int flag = 0;
	char s[64];

	strcpy(line, "debug=0xc serial=3 fuxigpulevel=4 -enable_kprintf_spam _name=line sweetnull=backboardd");
	/* before adopt, and with no property: the line alone */
	assert(PE_parse_boot_argn("fuxigpulevel", &u, sizeof(u)) && u == 4);
	assert(!PE_parse_boot_argn("fxinj", &u, sizeof(u)));
	prop = "fxinj=1 fuxigpulevel=8 fuxigpuncimport=202161776 -fuxiflag _name=prop sweetnull=x,y fuxicores=0x7c "
	    "debug=0x4 sweetnoexec=bluetoothd,fairplayd.H2 big=0x100000000";
	fuxi_boot_args_adopt();
	/* words only in the property: numbers, hex, 64-bit, flags, strings */
	assert(PE_parse_boot_argn("fxinj", &u, sizeof(u)) && u == 1);
	assert(PE_parse_boot_argn("fuxigpuncimport", &u, sizeof(u)) && u == 202161776);
	assert(PE_parse_boot_argn("fuxicores", &u, sizeof(u)) && u == 0x7c);
	assert(PE_parse_boot_argn("big", &q, sizeof(q)) && q == 0x100000000ULL);
	assert(PE_parse_boot_argn("-fuxiflag", &flag, sizeof(flag)) && flag == 1);
	assert(PE_parse_boot_argn("-fuxiflag", NULL, 0));
	assert(PE_parse_boot_arg_str("sweetnoexec", s, sizeof(s)) && !strcmp(s, "bluetoothd,fairplayd.H2"));
	/* in both: a value is the property's, as if appended (the parser keeps the last) */
	assert(PE_parse_boot_argn("fuxigpulevel", &u, sizeof(u)) && u == 8);
	assert(PE_parse_boot_argn("debug", &u, sizeof(u)) && u == 4);
	assert(PE_parse_boot_arg_str("sweetnull", s, sizeof(s)) && !strcmp(s, "x,y"));
	/* a _name is the first one's, the line's */
	assert(PE_parse_boot_argn("_name", s, sizeof(s)) && !strcmp(s, "line"));
	/* the line's own words, and names that are nowhere */
	assert(PE_parse_boot_argn("serial", &u, sizeof(u)) && u == 3);
	assert(PE_parse_boot_argn("-enable_kprintf_spam", &flag, sizeof(flag)));
	assert(!PE_parse_boot_argn("fuxiremote", &u, sizeof(u)) && !PE_parse_boot_argn("-nothere", NULL, 0));
	/* a name that ends one word and starts the next is not glued across the joining space */
	assert(!PE_parse_boot_argn("backboarddfxinj", &u, sizeof(u)));
	/* PE_boot_args() is still the real line */
	assert(!strstr(PE_boot_args(), "fxinj"));
	puts("stock parser over the merged line ok");
	return 0;
}
'''


def compiler():
    for cc in (os.environ.get('CC'), 'clang', 'cc'):
        if cc and shutil.which(cc):
            return cc
    raise SystemExit('no C compiler')


def is_clang(cc):
    return 'clang' in subprocess.run([cc, '--version'], capture_output=True, text=True).stdout


def build_run(cc, tmp, name, source, flags, includes=()):
    path = Path(tmp) / (name + '.c')
    path.write_text(source)
    exe = Path(tmp) / name
    cmd = [cc, '-std=gnu11', '-O1', '-o', str(exe), str(path)] + [f'-I{i}' for i in includes] + flags
    subprocess.run(cmd, check=True)
    return subprocess.run([str(exe)], check=True, capture_output=True, text=True).stdout


def kernel_block():
    """The FUXI block hooks.diff adds to bootargs.c, as C."""
    lines = HOOKS.split('--- a/pexpert/arm/pe_init.c\n')[0].splitlines()
    added = [l[1:] for l in lines if l.startswith('+') and not l.startswith('+++ ')]
    text = '\n'.join(added) + '\n'
    start = text.index('#if defined(FUXI)\n#include <pexpert/boot.h>\n')
    end = text.index('#endif /* FUXI */\n') + len('#endif /* FUXI */\n')
    block = text[start:end]
    assert block.count('#include') == 2
    return block.replace('#include <pexpert/boot.h>\n', '')


def check_hooks():
    b, p = HOOKS.split('--- a/pexpert/arm/pe_init.c\n')
    # both parser entry points scan pe_parse_args(), the stock call kept for other boards
    assert b.count('+\treturn PE_parse_boot_argn_internal(pe_parse_args(), ') == 2
    assert b.count(' \treturn PE_parse_boot_argn_internal(PE_boot_args(), ') == 2
    assert '-\treturn' not in b
    # adopt sits between SecureDTInit and pe_identify_machine in PE_init_platform; say after "iBoot version"
    assert re.search(r'^ \t\tSecureDTInit\(PE_state\.deviceTreeHead, PE_state\.deviceTreeSize\);\n\+#if defined\(FUXI\)\n'
                     r'\+.*\n\+\t\tfuxi_boot_args_adopt\(\);\n\+#endif\n \t\tpe_identify_machine\(boot_args_ptr\);\n', p, re.M)
    assert re.search(r'^ #endif /\* defined\(TARGET_OS_OSX\) && defined\(__arm64__\) \*/\n\+#if defined\(FUXI\)\n'
                     r'\+\tfuxi_boot_args_say\(\);\n\+#endif\n', p, re.M)
    assert p.count('fuxi_boot_args_adopt();') == 1 and p.count('fuxi_boot_args_say();') == 1
    assert not re.search(r'^-', p.split('\n', 1)[1], re.M)


def check_safe_names():
    names = []
    for line in (SRC / 'adt-safe.txt').read_text().splitlines():
        if line.strip() and not line.startswith('#'):
            names.append(line.split()[0])
    assert len(names) == len(set(names)) and names
    kernel = ''.join(l + '\n' for p in sorted((ROOT / 'xnu/patches').glob('*.patch'))
                     for l in p.read_text(errors='replace').splitlines() if l.startswith('+'))
    kernel += ''.join(p.read_text(errors='replace') for p in (ROOT / 'xnu/fuxi-ath12k').rglob('*.c'))
    user = {}
    for d in ('tools/fuxi-metal', 'tools/fuxi-sb', 'tools/fuxi-app', 'shim'):
        for p in (ROOT / d).rglob('*') if (ROOT / d).is_dir() else ():
            # host tests (ClientGateTests.cpp feeds "fuxigpu=1" as a line that must not arm backboardd) read nothing
            if p.is_file() and p.suffix in ('.c', '.m', '.mm', '.cpp', '.cc', '.hpp', '.h', '.rs', '.swift') and \
                    'Test' not in p.name and not p.name.startswith('test'):
                user[str(p.relative_to(ROOT))] = p.read_text(errors='replace')
    host = [l for t in ('tools/fuxi-xnu-monitor.py', 'tools/fuxi-probe-pull.py')
            for l in (ROOT / t).read_text().splitlines() if 'cmdline' in l]
    for name in names:
        q = re.escape(name)
        read = re.search(r'(PE_parse_boot_argn|PE_parse_boot_arg_str|lx_os_option)\(\s*"' + q + r'"', kernel) or \
            re.search(r'TUNABLE[A-Z_]*\([^;]*"' + q + r'"', kernel)
        assert read, f'{name}: no kernel code reads it'
        for path, text in user.items():
            assert not re.search(r'"' + q + r'=?"|"' + q + r'=', text), f'{name}: {path} reads it in user space'
        assert not any(re.search(r'\b' + q + r'=', l) for l in host), f'{name}: a host tool reads it from the payload'
    return names


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--xnu', type=Path, help='stock xnu-8796.141.3: also run its parser over the merged line')
    args = ap.parse_args()
    cc = compiler()
    warn = XNU_WARN if is_clang(cc) else ['-Wall', '-Wextra', '-Werror', '-Wno-unused-function']
    check_hooks()
    names = check_safe_names()
    with tempfile.TemporaryDirectory(prefix='fuxi-args-') as tmp:
        out = build_run(cc, tmp, 'header', HEADER_TEST, warn + ['-Wno-format-nonliteral'], [SRC])
        assert out == 'fuxi_boot_args.h ok\n', out
        said = build_run(cc, tmp, 'block', BLOCK_STUBS + kernel_block() + BLOCK_TEST,
                         warn + ['-Wno-unused-function', '-Wno-cast-qual'], [SRC])
        assert said.splitlines() == BLOCK_SAID, said
        if args.xnu:
            inc = Path(tmp) / 'inc/pexpert'
            inc.mkdir(parents=True)
            (inc / 'pexpert.h').write_text(STOCK_PEXPERT)
            (inc / 'device_tree.h').write_text(STOCK_DT)
            (inc / 'boot.h').write_text('')
            stock = Path(tmp) / 'gen'
            stock.mkdir()
            shutil.copy(args.xnu / 'pexpert/gen/bootargs.c', stock / 'bootargs.c')
            shutil.copy(SRC / 'fuxi_boot_args.h', stock / 'fuxi_boot_args.h')
            subprocess.run(['patch', '-s', '-p3', '-d', str(stock)], input=HOOKS.split('--- a/pexpert/arm/pe_init.c\n')[0],
                           text=True, check=True)
            main_c = Path(tmp) / 'stock-main.c'
            main_c.write_text(STOCK_TEST)
            exe = Path(tmp) / 'stock'
            subprocess.run([cc, '-std=gnu11', '-DFUXI=1', '-w', f'-I{Path(tmp) / "inc"}', '-o', str(exe),
                            str(stock / 'bootargs.c'), str(main_c)], check=True)
            out = subprocess.run([str(exe)], check=True, capture_output=True, text=True).stdout
            assert out == 'stock parser over the merged line ok\n', out
    print(f'PASS: fuxi-args merge, kernel block, hooks, {len(names)} ADT-safe names' + (', stock parser' if args.xnu else ''))


if __name__ == '__main__':
    main()
