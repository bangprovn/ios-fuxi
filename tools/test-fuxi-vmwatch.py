#!/usr/bin/env python3
"""xnu/fuxi-vmwatch/fuxi_vm_watch.h on the host: which removals of CoreAnimation memory (tag 51) it logs, the
line it prints, the lock it takes, and that patch 0123 hooks vm_map_delete and vm_map_protect where it should."""
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / 'xnu/fuxi-vmwatch'
HEADER = SRC / 'fuxi_vm_watch.h'

# Just enough of the kernel for the !FUXI_VMW_HOST half: a map is a sorted ring of entries.
KERNEL = r'''
#include <assert.h>
#include <stdarg.h>
#include <stdlib.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint64_t vm_map_offset_t;
typedef int vm_prot_t;
struct vm_object { int purgable; };
typedef struct vm_object *vm_object_t;
#define VM_OBJECT_NULL ((vm_object_t)0)
struct vm_map_entry {
	struct vm_map_entry *vme_next;
	uint64_t vme_start, vme_end;
	int is_sub_map, alias;
	vm_prot_t protection, max_protection;
	vm_object_t object;
};
typedef struct vm_map_entry *vm_map_entry_t;
struct vm_map { struct vm_map_entry hdr; int readers; };
typedef struct vm_map *vm_map_t;
#define vm_map_to_entry(m) (&(m)->hdr)
#define VME_ALIAS(e) ((e)->alias)
#define VME_OBJECT(e) (assert(!(e)->is_sub_map), (e)->object)
static bool
vm_map_lookup_entry(vm_map_t m, vm_map_offset_t a, vm_map_entry_t *out)
{
	vm_map_entry_t prev = &m->hdr;
	for (vm_map_entry_t e = m->hdr.vme_next; e != &m->hdr && e->vme_start <= a; e = e->vme_next) {
		if (a < e->vme_end) {
			*out = e;
			return true;
		}
		prev = e;
	}
	*out = prev;
	return false;
}
static void vm_map_lock_read(vm_map_t m) { m->readers++; }
static void vm_map_unlock_read(vm_map_t m) { m->readers--; }
static vm_map_t cur_map;
static vm_map_t current_map(void) { return cur_map; }
typedef void *task_t;
typedef void *thread_t;
static task_t current_task(void) { return (task_t)1; }
static thread_t current_thread(void) { return (thread_t)1; }
static const char *cur_comm = "MobilePhone";
static void *get_bsdtask_info(task_t t) { (void)t; return (void *)cur_comm; }
static char *proc_name_address(void *p) { return (char *)p; }
static int proc_selfpid(void) { return 341; }
typedef struct { uint64_t pc, lr; } arm_saved_state_t;
static arm_saved_state_t regs = { 0x18f60d374, 0x18f603de8 };
static arm_saved_state_t *find_user_regs(thread_t t) { (void)t; return &regs; }
static bool is_saved_state64(arm_saved_state_t *s) { (void)s; return true; }
static uint64_t get_saved_state_pc(arm_saved_state_t *s) { return s->pc; }
static uint64_t get_saved_state_lr(arm_saved_state_t *s) { return s->lr; }
static unsigned
backtrace(uintptr_t *bt, unsigned n, void *ctl, void *info)
{
	(void)ctl; (void)info;
	for (unsigned i = 0; i < n; i++) {
		bt[i] = 0xfffffff007004000ull + 16 * i;
	}
	return n;
}
#define VM_KERNEL_UNSLIDE(x) ((x) - 0xfffffff000000000ull)
static const char *bootarg = "MobilePhone";
static bool
PE_parse_boot_arg_str(const char *n, char *out, int size)
{
	if (strcmp(n, "fuxivmwatch") || bootarg == NULL) {
		return false;
	}
	snprintf(out, size, "%s", bootarg);
	return true;
}
#define os_atomic_load(p, m) (*(p))
#define os_atomic_cmpxchg(p, o, n, m) __sync_bool_compare_and_swap((p), (o), (n))
#define os_atomic_inc_orig(p, m) __sync_fetch_and_add((p), 1)
#define strlcpy(d, s, n) snprintf((d), (n), "%s", (s))
static char out[1 << 16];
static size_t outlen;
static int lines;
static int
vmw_printf(const char *fmt, ...)
{
	va_list ap;
	va_start(ap, fmt);
	int n = vsnprintf(out + outlen, sizeof(out) - outlen, fmt, ap);
	va_end(ap);
	outlen += n;
	lines++;
	return n;
}
#define printf vmw_printf
#include "fuxi_vm_watch.h"
#undef printf
#undef assert   /* the same check, printing what was logged */
#define assert(c) do { if (!(c)) { fprintf(stderr, "line %d: %s\nlog:\n%s\n", __LINE__, #c, out); abort(); } } while (0)

static struct vm_map map, other;
static struct vm_object purg = { 1 }, plain = { 0 };
/* MobilePhone at the crash, before the hole: CA's 32 KiB Shmem, a CG raster copy, another CA buffer. */
static struct vm_map_entry ca = { 0, 0x10dc00000, 0x10dc08000, 0, 51, 3, 7, &purg };
static struct vm_map_entry cg = { 0, 0x10dc08000, 0x10dc3c000, 0, 54, 1, 1, &plain };
static struct vm_map_entry ca2 = { 0, 0x10dc40000, 0x10dc44000, 0, 51, 3, 7, &purg };

static void
reset(const char *comm)
{
	map.hdr.vme_next = &ca; ca.vme_next = &cg; cg.vme_next = &ca2; ca2.vme_next = &map.hdr;
	other.hdr.vme_next = &other.hdr;
	map.readers = 0;
	cur_map = &map;
	cur_comm = comm;
	outlen = 0; out[0] = 0; lines = 0;
}

int
main(void)
{
	/* pure parts */
	assert(fuxi_vmw_partial(0x4000, 0x8000, 0, 0x8000) && fuxi_vmw_partial(0, 0x4000, 0, 0x8000));
	assert(!fuxi_vmw_partial(0, 0x8000, 0, 0x8000) && !fuxi_vmw_partial(0, 0x10000, 0x4000, 0x8000));
	assert(fuxi_vmw_budget(FUXI_VMW_PARTIAL) == 1 && fuxi_vmw_budget(FUXI_VMW_FOREIGN) == 1);
	assert(fuxi_vmw_budget(FUXI_VMW_PARTIAL | FUXI_VMW_WATCHED) == 1 && fuxi_vmw_budget(FUXI_VMW_WATCHED) == 2);
	assert(fuxi_vmw_budget(0) == 0);
	assert(fuxi_vmw_name_is("MobilePhone", "MobilePhone") && !fuxi_vmw_name_is("MobilePhon", "MobilePhone"));
	assert(!fuxi_vmw_name_is("MobilePhone", "Mobile") && !fuxi_vmw_name_is("x", "") && !fuxi_vmw_name_is(NULL, "x"));
	assert(fuxi_vmw_name_is("0123456789abcdef", "0123456789abcdef"));

	/* the Shmem's second page goes, in the watched process: one SPLIT line, unlocked scan balanced */
	reset("MobilePhone");
	fuxi_vmw_scan(&map, 0x10dc04000, 0x10dc08000, "del", false);
	assert(map.readers == 0);
	assert(strstr(out, "FUXI VMW: watching MobilePhone") == out);
	assert(strstr(out, "FUXI VMW del 341 MobilePhone own [0x10dc04000,0x10dc08000) tag51 "
	    "[0x10dc00000,0x10dc08000) 3/7 purg 1 SPLIT upc 0x18f60d374 ulr 0x18f603de8 kbt 0x7004010 0x7004020"));
	assert(lines == 2 && out[outlen - 1] == '\n' && strstr(out, "0x7004060\n"));

	/* whole Shmem freed by the watched process itself (caller holds the lock: none taken) */
	reset("MobilePhone");
	map.readers = 5;
	fuxi_vmw_scan(&map, 0x10dc00000, 0x10dc08000, "del", true);
	assert(map.readers == 5 && lines == 1 && strstr(out, "del 341 MobilePhone own [0x10dc00000,0x10dc08000)"));
	assert(!strstr(out, "SPLIT"));

	/* other processes: whole frees are normal and stay quiet, splits are not */
	reset("SpringBoard");
	fuxi_vmw_scan(&map, 0x10dc00000, 0x10dc08000, "del", false);
	assert(lines == 0);
	fuxi_vmw_scan(&map, 0x10dc00000, 0x10dc04000, "del", false);
	assert(lines == 1 && strstr(out, "SpringBoard own [0x10dc00000,0x10dc04000)") && strstr(out, " SPLIT "));

	/* another task's map (IOKit unmap, task port): logged, marked foreign, even when whole */
	reset("kernel_task");
	cur_map = &other;
	fuxi_vmw_scan(&map, 0x10dc40000, 0x10dc44000, "del", true);
	assert(lines == 1 && strstr(out, "kernel_task foreign [0x10dc40000,0x10dc44000) tag51 [0x10dc40000,0x10dc44000)"));

	/* a range over CG memory only, or over nothing: no line */
	reset("MobilePhone");
	fuxi_vmw_scan(&map, 0x10dc08000, 0x10dc3c000, "del", false);
	fuxi_vmw_scan(&map, 0x20000000, 0x20004000, "del", false);
	assert(lines == 0 && map.readers == 0);

	/* a range over CG and part of CA: the split entry is the one shown */
	reset("MobilePhone");
	fuxi_vmw_scan(&map, 0x10dc08000, 0x10dc42000, "protmax", false);
	assert(lines == 1 && strstr(out, "protmax 341 MobilePhone own [0x10dc08000,0x10dc42000) tag51 [0x10dc40000,0x10dc44000) 3/7 purg 1 SPLIT"));

	/* budgets: splits and foreign maps share FUXI_VMW_ANY_LINES */
	reset("SpringBoard");
	for (int i = 0; i < 100; i++) {
		fuxi_vmw_scan(&map, 0x10dc00000, 0x10dc04000, "del", false);
	}
	assert(lines == FUXI_VMW_ANY_LINES - 4);    /* four used above */

	/* off without the boot-arg: nothing, not even the banner */
	fuxi_vmw_state = -1;
	bootarg = NULL;
	reset("MobilePhone");
	fuxi_vmw_scan(&map, 0x10dc04000, 0x10dc08000, "del", false);
	assert(lines == 0 && fuxi_vmw_state == 0 && map.readers == 0);
	puts("vmwatch model ok");
	return 0;
}
'''


def main():
    header = HEADER.read_text()
    assert '\r' not in header
    # Under the map lock: no user memory, no sleeping allocation.
    kernel_half = header[header.index('#if !defined(FUXI_VMW_HOST)'):]
    for banned in ('copyin', 'vm_map_read_user', 'kalloc', 'IOMalloc', 'sweet_prof_user_bt', 'vm_map_lock(map)'):
        assert banned not in kernel_half, banned

    with tempfile.TemporaryDirectory(prefix='fuxi-vmwatch-') as tmp:
        tmp = Path(tmp)
        for stub in ('pexpert/pexpert.h', 'kern/thread.h', 'arm/thread.h'):
            (tmp / stub).parent.mkdir(parents=True, exist_ok=True)
            (tmp / stub).write_text('')
        (tmp / 'host.c').write_text('#define FUXI_VMW_HOST 1\n#include "fuxi_vm_watch.h"\nint main(void) { return '
                                    'fuxi_vmw_partial(1, 2, 0, 2) && fuxi_vmw_budget(FUXI_VMW_WATCHED) == 2 ? 0 : 1; }\n')
        (tmp / 'kernel.c').write_text(KERNEL)
        for name in ('host', 'kernel'):
            subprocess.run(['clang', '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function',
                            '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                            '-I', str(SRC), '-I', str(tmp), str(tmp / (name + '.c')), '-o', str(tmp / name)], check=True)
            subprocess.run([str(tmp / name)], check=True)

    hooks = (SRC / 'hooks.diff').read_text()
    added = '\n'.join(l[1:] for l in hooks.splitlines() if l.startswith('+') and not l.startswith('+++'))
    assert added.count('#if defined(ARM64_BOARD_CONFIG_SWEET)') == 3 and added.count('#endif') == 3
    assert '#include "fuxi_vm_watch.h"' in added
    # vm_map_delete: after GAPS_OK is known, skipping the kernel pmap and maps being torn down; map already locked.
    delete = re.search(r'state \|= VMDS_GAPS_OK;\n \t}\n \n((?:\+.*\n)+)', hooks)
    assert delete and '(state & (VMDS_KERNEL_PMAP | VMDS_GAPS_OK)) == 0' in delete[1] and \
        'fuxi_vmw_scan(map, start, end, "del", true);' in delete[1]
    # vm_map_protect: write drops only, user maps only, before vm_map_protect takes the lock.
    protect = re.search(r'return KERN_INVALID_ARGUMENT;\n \t}\n \n((?:\+.*\n)+) \tif \(new_prot & VM_PROT_COPY\)', hooks)
    assert protect and '(new_prot & VM_PROT_WRITE) == 0' in protect[1] and 'vm_map_pmap(map) != kernel_pmap' in protect[1] \
        and 'fuxi_vmw_scan(map, start, end, set_max ? "protmax" : "prot", false);' in protect[1]
    # both cheap when off
    assert added.count('os_atomic_load(&fuxi_vmw_state, relaxed) != 0') == 2

    patch = (ROOT / 'xnu/patches/0123-fuxi-vm-watch.patch').read_text()
    assert patch.startswith(hooks) and '+++ b/osfmk/vm/fuxi_vm_watch.h' in patch
    print('PASS: fuxi-vmwatch model (splits, foreign maps, the watched process, budgets, off) and 0123 hooks')


if __name__ == '__main__':
    main()
