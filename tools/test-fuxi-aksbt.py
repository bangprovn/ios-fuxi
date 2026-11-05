#!/usr/bin/env python3
"""The AppleKeyStore call log's rate limit and burst backtrace (xnu/fuxi-aksbt/, patch 0125) on the host.

1. Patch 0125 matches its sources (tools/sync-fuxi-aksbt.py --check).
2. The limiter (fuxi_aks_log.h, logic half, built as C and as C++ under XNU's warning set): the first 8 calls of a
   (pid, name, selector) are printed, the rest counted, one count line per pair per period with the latest rc;
   a backtrace only after more than 200 selector-17 calls in one second, then not again for 30 s; other selectors
   never; slot reuse prefers free, then idle slots, and says the counted calls it drops.
3. The caps and the lines: the constants, the per-call line unchanged from 0038, the count line, every
   fuxi_aks_bt.c line prefixed "SWEET AKS BT:", backtrace_user capped at 32 frames, the shared region reference
   dropped, nothing written to user memory, printing and backtracing outside the lock.
4. SweetKeyStore.cpp after 0125 (with and without 0117) differs from before only by the include, the init and the
   one log call: no selector's answer changes.
"""
import importlib.util
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / 'xnu/fuxi-aksbt'
LOG_H = SRC / 'fuxi_aks_log.h'
BT_C = SRC / 'fuxi_aks_bt.c'
SYNC = ROOT / 'tools/sync-fuxi-aksbt.py'

# XNU's makedefs/MakeInc.def WARNFLAGS_STD (-Weverything -Werror and its exceptions), as test-fuxi-args.py uses them,
# plus CXXWARNFLAGS_STD's for C++.
XNU_WARN = ('-Weverything -Werror -Wno-pedantic -Wno-bad-function-cast -Wno-bitwise-instead-of-logical -Wno-c++-compat '
            '-Wno-c++98-compat -Wno-conditional-uninitialized -Wno-covered-switch-default -Wno-disabled-macro-expansion '
            '-Wno-documentation-unknown-command -Wno-extra-semi-stmt -Wno-format-non-iso -Wno-language-extension-token '
            '-Wno-missing-variable-declarations -Wno-packed -Wno-padded -Wno-partial-availability -Wno-reserved-id-macro '
            '-Wno-shift-sign-overflow -Wno-switch-enum -Wno-unaligned-access -Wno-undef -Wno-unused-macros '
            '-Wno-used-but-marked-unused -Wno-variadic-macros -Wno-vla -Wno-zero-length-array -Wno-unknown-warning-option '
            '-Wno-anon-enum-enum-conversion -Wno-error=enum-enum-conversion -Wno-error=c99-designator '
            '-Wno-error=reorder-init-list -Wno-error=declaration-after-statement -Wno-sign-compare -Wno-sign-conversion '
            '-Wpointer-arith '
            # host-only: newer clang than Xcode 15's; Apple clang's warning about /usr/local/include (owner's Mac)
            '-Wno-unsafe-buffer-usage -Wno-reserved-identifier -Wno-reserved-macro-identifier -Wno-pre-c2x-compat '
            '-Wno-declaration-after-statement -Wno-poison-system-directories').split()
XNU_CXX_WARN = '-Wno-c++98-compat-pedantic -Wno-exit-time-destructors -Wno-global-constructors -Wno-old-style-cast'.split()

MODEL = r'''
#define FUXI_AKS_HOST 1
#include <stdarg.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#if defined(__cplusplus)
#define NOPTR nullptr
#else
#define NOPTR ((void *)0)
#endif

static char out[1 << 16];
static size_t outlen;
static unsigned int printed;

static int cap(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static int
cap(const char *fmt, ...)
{
	va_list ap;
	va_start(ap, fmt);
	int n = vsnprintf(out + outlen, sizeof(out) - outlen, fmt, ap);
	va_end(ap);
	outlen += (size_t)n;
	printed++;
	return n;
}
#define FUXI_AKS_PRINTF cap
#include "fuxi_aks_log.h"

#define CHECK(c) do { if (!(c)) { fprintf(stderr, "line %d: %s\nlog:\n%s\n", __LINE__, #c, out); abort(); } } while (0)
#define SEC 1000000000ULL

static struct fuxi_aks_table table;
static unsigned int n_print, n_counted, n_bt, last_burst;
static unsigned long long bt_at[8];

static void
reset_log(void)
{
	outlen = 0;
	out[0] = '\0';
	printed = 0;
}

static void
call(int pid, const char *name, unsigned int sel, int rc, unsigned long long now)
{
	unsigned int burst = 0;
	unsigned int what = fuxi_aks_note(&table, pid, name, sel, rc, now, &burst);
	CHECK((what & (FUXI_AKS_PRINT | FUXI_AKS_COUNTED)) == FUXI_AKS_PRINT ||
	    (what & (FUXI_AKS_PRINT | FUXI_AKS_COUNTED)) == FUXI_AKS_COUNTED);
	n_print += (what & FUXI_AKS_PRINT) != 0;
	n_counted += (what & FUXI_AKS_COUNTED) != 0;
	if (what & FUXI_AKS_BT) {
		CHECK(sel == FUXI_AKS_BURST_SEL && burst > FUXI_AKS_BURST_CALLS);
		if (n_bt < 8) {
			bt_at[n_bt] = now;
		}
		n_bt++;
		last_burst = burst;
	}
}

static void
zero(void)
{
	memset(&table, 0, sizeof(table));
	n_print = n_counted = n_bt = last_burst = 0;
	reset_log();
}

int
main(void)
{
	unsigned long long now = 5 * SEC;

	/* Preferences on the Wi-Fi password field: 12,034 calls of selector 17 in 10 s, rc 0. */
	zero();
	for (unsigned int i = 0; i < 12034; i++) {
		call(341, "Preferences", 17, 0, now + (unsigned long long)i * (10 * SEC / 12034));
	}
	CHECK(n_print == FUXI_AKS_FIRST && n_counted == 12034 - FUXI_AKS_FIRST);
	CHECK(n_bt == 1 && last_burst == FUXI_AKS_BURST_CALLS + 1);
	CHECK(bt_at[0] - now < SEC);
	CHECK(fuxi_aks_report(&table) == 1);
	CHECK(strcmp(out, "SWEET AKS: Preferences sel 17: 12026 calls in the last 10 s (rc 0x0)\n") == 0);
	reset_log();
	CHECK(fuxi_aks_report(&table) == 0 && printed == 0);
	/* 40 s more at 1,200 a second: one more backtrace, 30 s after the first; nothing printed one by one. */
	for (unsigned int i = 0; i < 48000; i++) {
		call(341, "Preferences", 17, 0, now + 10 * SEC + (unsigned long long)i * (SEC / 1200));
	}
	CHECK(n_print == FUXI_AKS_FIRST && n_bt == 2);
	CHECK(bt_at[1] - bt_at[0] >= FUXI_AKS_BT_GAP_S * SEC && bt_at[1] - bt_at[0] < (FUXI_AKS_BT_GAP_S + 1) * SEC);
	CHECK(fuxi_aks_report(&table) == 1);
	CHECK(strcmp(out, "SWEET AKS: Preferences sel 17: 48000 calls in the last 10 s (rc 0x0)\n") == 0);

	/* Exactly 200 in every second: no backtrace; 201 within one second: one. */
	zero();
	for (unsigned int s = 0; s < 5; s++) {
		for (unsigned int i = 0; i < 200; i++) {
			call(400, "Spotlight", 17, 0, now + s * SEC + i * (SEC / 200));
		}
	}
	CHECK(n_bt == 0);
	for (unsigned int i = 0; i < 201; i++) {
		call(400, "Spotlight", 17, 0, now + 10 * SEC + i * (SEC / 250));
	}
	CHECK(n_bt == 1 && last_burst == 201);

	/* Other selectors are counted but never backtraced, and keep their own first 8 lines and count. */
	zero();
	for (unsigned int i = 0; i < 3000; i++) {
		call(500, "securityd", 7, 0, now + i * (SEC / 3000));
		call(500, "securityd", 35, 0, now + i * (SEC / 3000));
	}
	CHECK(n_bt == 0 && n_print == 2 * FUXI_AKS_FIRST && n_counted == 2 * (3000 - FUXI_AKS_FIRST));
	call(500, "securityd", 7, (int)0xe00002c7, now + SEC);
	CHECK(fuxi_aks_report(&table) == 2);
	CHECK(strstr(out, "SWEET AKS: securityd sel 7: 2993 calls in the last 10 s (rc 0xe00002c7)\n") != NOPTR);
	CHECK(strstr(out, "SWEET AKS: securityd sel 35: 2992 calls in the last 10 s (rc 0x0)\n") != NOPTR);

	/* Two processes in a burst at once: their own slots, counts and backtraces. */
	zero();
	for (unsigned int i = 0; i < 1000; i++) {
		call(341, "Preferences", 17, 0, now + i * (SEC / 1000));
		call(342, "Spotlight", 17, 0, now + i * (SEC / 1000));
	}
	CHECK(n_bt == 2 && n_print == 2 * FUXI_AKS_FIRST);
	/* A new image under the same pid (exec) is a new pair: its first 8 calls are printed. */
	for (unsigned int i = 0; i < 10; i++) {
		call(341, "Preferences2", 17, 0, now + SEC + i);
	}
	CHECK(n_print == 3 * FUXI_AKS_FIRST);

	/* 70 busy pairs in 64 slots: the 6 oldest are taken over and their 2 counted calls each are said. */
	zero();
	for (int pid = 1; pid <= 70; pid++) {
		for (unsigned int i = 0; i < 10; i++) {
			call(pid, "proc", 0, 0, now + (unsigned long long)pid * 1000 + i);
		}
	}
	CHECK(n_counted == 70 * 2);
	CHECK(fuxi_aks_report(&table) == FUXI_AKS_SLOTS + 1);
	CHECK(strstr(out, "SWEET AKS: 12 counted calls lost to slot reuse\n") != NOPTR);
	unsigned long long said = 0;
	for (const char *p = out; (p = strstr(p, " calls in the last ")) != NOPTR; p++) {
		const char *q = p;
		while (q > out && q[-1] >= '0' && q[-1] <= '9') {
			q--;
		}
		said += strtoull(q, NOPTR, 10);
	}
	CHECK(said == 64 * 2);
	/* With nothing counted, a new pair takes the slot least recently used and loses nothing. */
	reset_log();
	call(71, "proc", 0, 0, now + 1000 * SEC);
	CHECK(table.lost == 0 && fuxi_aks_report(&table) == 0);
	int seven = 0, seventy_one = 0;
	for (unsigned int i = 0; i < FUXI_AKS_SLOTS; i++) {
		seven += table.slot[i].used && table.slot[i].pid == 7;
		seventy_one += table.slot[i].used && table.slot[i].pid == 71;
	}
	CHECK(seven == 0 && seventy_one == 1);
	/* An idle slot goes before an older one with calls still to count. */
	zero();
	for (unsigned int i = 0; i < 10; i++) {
		call(1, "busy", 0, 0, now + i);
	}
	for (int pid = 2; pid <= FUXI_AKS_SLOTS; pid++) {
		for (unsigned int i = 0; i < FUXI_AKS_FIRST; i++) {
			call(pid, "quiet", 0, 0, now + 1000 * (unsigned long long)pid + i);
		}
	}
	call(FUXI_AKS_SLOTS + 1, "new", 0, 0, now + SEC);
	CHECK(table.lost == 0);
	int busy = 0, quiet = 0;
	for (unsigned int i = 0; i < FUXI_AKS_SLOTS; i++) {
		busy += table.slot[i].used && table.slot[i].pid == 1 && table.slot[i].pending == 2;
		quiet += table.slot[i].used && table.slot[i].pid == 2;
	}
	CHECK(busy == 1 && quiet == 0);
	CHECK(fuxi_aks_report(&table) == 1 && strcmp(out, "SWEET AKS: busy sel 0: 2 calls in the last 10 s (rc 0x0)\n") == 0);
	/* Names are kept to FUXI_AKS_NAME - 1 characters. */
	zero();
	call(9, "a-process-name-longer-than-thirty-one-characters", 17, 0, now);
	for (unsigned int i = 0; i < 9; i++) {
		call(9, "a-process-name-longer-than-thirty-one-characters", 17, 0, now + 1 + i);
	}
	CHECK(fuxi_aks_report(&table) == 1);
	CHECK(strcmp(out, "SWEET AKS: a-process-name-longer-than-thir sel 17: 2 calls in the last 10 s (rc 0x0)\n") == 0);
	printf("model ok\n");
	return 0;
}
'''


def load_sync():
    spec = importlib.util.spec_from_file_location('sync_fuxi_aksbt', SYNC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def function_body(text, signature):
    start = text.index(signature)
    end = text.index('\n}\n', start)
    return text[start:end]


def code_only(text):
    """The C with comments blanked; string literals kept (the lines are checked through them)."""
    return re.sub(r'/\*.*?\*/', lambda m: re.sub(r'[^\n]', ' ', m[0]), text, flags=re.S)


def check_static(sync):
    log_h = LOG_H.read_text()
    bt_c = BT_C.read_text()
    assert '\r' not in log_h and '\r' not in bt_c
    for name, value in (('FUXI_AKS_FIRST', '8'), ('FUXI_AKS_PERIOD_S', '10'), ('FUXI_AKS_BURST_SEL', '17'),
                        ('FUXI_AKS_BURST_CALLS', '200'), ('FUXI_AKS_BT_GAP_S', '30'), ('FUXI_AKS_SLOTS', '64'),
                        ('FUXI_AKS_NAME', '32')):
        assert re.search(r'^#define %s\s+%s\b' % (name, value), log_h, re.M), name
    assert re.search(r'^#define FUXI_AKS_BT_FRAMES\s+32\b', bt_c, re.M)
    assert re.search(r'^#define FUXI_AKS_BURST_SEL\s+17\s+/\* kAKSGetDeviceState \*/', log_h, re.M)
    assert re.search(r'^\+\tkAKSGetDeviceState\s+= 17,', (ROOT / 'xnu/patches/0038-sweet-applekeystore.patch').read_text(),
                     re.M)

    # The per-call line is 0038's, word for word; the count and lost lines are the documented ones.
    old_line = sync.REMOVED[2].strip()
    old_fmt = re.search(r'IOLog\(("[^"]*")', old_line)[1]
    assert old_fmt == '"SWEET AKS: %s sel %u arg0 %llu in %u/%u out %u/%u -> 0x%x\\n"', old_fmt
    log = function_body(log_h, 'fuxi_aks_log(uint32_t selector')
    assert log.count('IOLog(') == 1 and ('IOLog(' + old_fmt + ', name, selector,') in log
    assert '\t\t    arg0, in_scalars, in_struct, out_scalars, out_struct, ret);' in log
    assert '"SWEET AKS: %s sel %u: %llu calls in the last %u s (rc 0x%x)\\n"' in log_h
    assert '"SWEET AKS: %llu counted calls lost to slot reuse\\n"' in log_h
    assert re.search(r'^#define FUXI_AKS_PRINTF IOLog$', log_h, re.M)

    # Locking: the hot path takes the lock once and prints, arms and backtraces only after letting it go; the thread
    # call takes it around the count lines. No allocation per call.
    code = code_only(log)
    assert code.count('IOLockLock(fuxi_aks_lock)') == 1 and code.count('IOLockUnlock(fuxi_aks_lock)') == 1
    unlock = code.index('IOLockUnlock(fuxi_aks_lock)')
    for after in ('thread_call_enter_delayed(', 'IOLog(', 'fuxi_aks_user_bt('):
        assert code.index(after) > unlock, after
    assert code.index('fuxi_aks_note(') < unlock
    assert not re.search(r'\b(IOMalloc|kalloc|IOLockAlloc|thread_call_allocate|OSString|new)\b', code)
    flush = code_only(function_body(log_h, 'fuxi_aks_flush('))
    assert flush.index('IOLockLock') < flush.index('fuxi_aks_report(') < flush.index('IOLockUnlock')
    init = code_only(function_body(log_h, 'fuxi_aks_log_init(void)'))
    assert 'IOLockAlloc()' in init and 'thread_call_allocate(fuxi_aks_flush, NULL)' in init
    assert 'clock_interval_to_deadline(FUXI_AKS_PERIOD_S, kSecondScale, &deadline)' in code

    # The backtrace: XNU's backtrace_user on the calling thread, capped; every line prefixed; read-only.
    bt = code_only(bt_c)
    assert 'backtrace_user(bt, FUXI_AKS_BT_FRAMES, NULL, &info)' in bt and 'uintptr_t bt[FUXI_AKS_BT_FRAMES]' in bt
    prints = re.findall(r'printf\("([^"]*)"', bt)
    assert len(prints) == 7 and all(p.startswith('SWEET AKS BT: %s[%d] ') for p in prints), prints
    assert bt.count('vm_shared_region_get(task)') == 1 and bt.count('vm_shared_region_deallocate(sr)') == 1
    assert 'vm_shared_region_start_address(sr, &start, NULL)' in bt
    assert bt.count('task_lock(task)') == 1 and bt.count('task_unlock(task)') == 1
    assert not re.search(r'\b(copyout\w*|vm_map_write_user|vm_map_copy_overwrite|suword\w*|IOLog)\b', bt)
    assert 'count = head[1] < FUXI_AKS_BT_IMAGES ? head[1] : FUXI_AKS_BT_IMAGES;' in bt
    assert re.search(r'^#define FUXI_AKS_BT_CHUNK\s+16\b', bt_c, re.M)
    assert 'struct fuxi_aks_image chunk[FUXI_AKS_BT_CHUNK];' in bt
    assert 'k * sizeof(chunk[0])' in bt and 'unsigned int k = count - done < FUXI_AKS_BT_CHUNK' in bt
    # One declaration on each side, the same.
    decl = 'void fuxi_aks_user_bt(const char *name, int pid, unsigned int selector, unsigned int calls);'
    assert ('extern "C" ' + decl) in log_h and ('\n' + decl + '\n') in bt_c
    assert bt_c.startswith('/*') and '#include <pexpert/arm64/board_config.h>\n#if defined(FUXI)\n' in bt_c


def check_applied(sync):
    """SweetKeyStore.cpp before and after 0125: only the hook lines differ, in both kernels."""
    text = sync.expected()
    added = [l for l in sync.ADDED if not l.startswith('osfmk/')]
    for with_0117 in (True, False):
        with tempfile.TemporaryDirectory(prefix='fuxi-aksbt-test-') as tmp:
            tmp = Path(tmp)
            paths = sync.baseline(tmp, None, with_0117)
            before = (tmp / paths[0]).read_text()
            hooks = ''.join(sync.chunks(text, paths))
            subprocess.run(['git', 'apply', '-'], input=hooks, text=True, cwd=tmp, check=True)
            after = (tmp / paths[0]).read_text()
        b = [l for l in before.splitlines() if l not in sync.REMOVED]
        a = [l for l in after.splitlines() if l not in added]
        assert a == b, 'SweetKeyStore.cpp changed beyond the hooks (%s 0117)' % ('with' if with_0117 else 'without')
        assert len(before.splitlines()) - len(sync.REMOVED) + len(added) == len(after.splitlines())
        assert 'SWEET AKS: %s sel %u arg0' not in after
        assert after.count('fuxi_aks_log(') == 1 and after.count('fuxi_aks_log_init();') == 1
        start = after.index('AppleKeyStore::start(IOService *provider)')
        assert start < after.index('fuxi_aks_log_init();') < after.index('registerService();', start)
        method = after[after.index('AppleKeyStoreUserClient::externalMethod('):]
        assert method.index('fuxi_aks_log(') > method.rindex('\tswitch (selector) {')
        assert re.search(r'fuxi_aks_log\(selector, arg0, in_scalars, in_struct, out_scalars, out_struct, ret\);\n'
                         r'\treturn ret;\n\}\n#endif /\* SWEET \*/\n$', after)


def main():
    subprocess.run(['python3', str(SYNC), '--check'], check=True)
    ci = (ROOT / 'tools/ci/build.sh').read_text()
    kernel = ci[ci.index('\nkernel() {'):ci.index('\nmesa_source() {')]
    assert 'python3 tools/test-fuxi-aksbt.py\n' in kernel and 'python3 tools/sync-fuxi-aksbt.py --check\n' in kernel
    sync = load_sync()
    check_static(sync)
    check_applied(sync)
    with tempfile.TemporaryDirectory(prefix='fuxi-aksbt-') as tmp:
        tmp = Path(tmp)
        (tmp / 'model.c').write_text(MODEL)
        for lang, std, extra in (('c', '-std=gnu11', []), ('c++', '-std=gnu++17', XNU_CXX_WARN)):
            exe = tmp / ('model-' + lang.replace('+', 'x'))
            subprocess.run(['clang', '-x', lang, std, *XNU_WARN, *extra, '-Wno-unused-function', '-Wno-format-nonliteral',
                            '-Wno-missing-prototypes', '-I', str(SRC), str(tmp / 'model.c'), '-o', str(exe)], check=True)
            subprocess.run([str(exe)], check=True)
    print('PASS: fuxi-aksbt limiter model (first 8, counts per 10 s, burst > 200/s once per 30 s, sel 17 only, '
          'slot reuse), caps and lines, lock order, read-only backtrace, SweetKeyStore.cpp answers unchanged')


if __name__ == '__main__':
    main()
