/*
 * FUXI VM watch (boot-arg fuxivmwatch=NAME, off without it): which code takes CoreAnimation
 * memory (VM tag 51) out of a user map, or drops its write permission.
 *
 * Why: MobilePhone dies with SIGBUS on the keypad once per boot (r-v22 onwards). The faulting
 * store is ShmemBitmap::fill_pixels clearing 129 rows of 128 bytes from 0x40 into a 32 KiB
 * Shmem (QuartzCore CA::Render::Shmem::new_shmem: vm_allocate VM_MAKE_TAG(51) | PURGABLE).
 * At the crash the map holds only the first 16 KiB of it; the next page belongs to a
 * CoreGraphics create_protected_copy (mmap tag 54, vm_copy, vm_protect max READ), "CG raster
 * data r--/r-- SM=COW", or (r-v25, r-v26) a 32 KiB tag-0 r--/rwx mapping. Something removed the
 * Shmem's second page while CoreAnimation still used it; these lines say what.
 *
 * Logged, "FUXI VMW" in the kernel log:
 *   - a removal or protection drop that splits a tag-51 entry (any process; a Shmem or a CA
 *     image buffer is only ever freed whole);
 *   - a removal or protection drop of tag-51 memory in another task's map (IOKit unmaps,
 *     task ports);
 *   - in processes named NAME (p_comm): every removal of tag-51 memory and every protection
 *     change that leaves it without write.
 * Each line: the operation, pid and name, own/foreign map, the range, the first tag-51 entry it
 * hits, that entry's protection and purgeable state, the calling thread's user pc and lr
 * (unslid with the boot's shared cache slide) and six kernel return addresses (unslid).
 * At most FUXI_VMW_ANY_LINES split/foreign lines and FUXI_VMW_WATCH_LINES others per boot.
 * Without the boot-arg each user-map deletion or protect costs one load and branch.
 */
#ifndef FUXI_VM_WATCH_H
#define FUXI_VM_WATCH_H

/* QuartzCore's tag: new_shmem(m) vm_allocate flags 0x33000003, aligned_malloc mmap fd 0x33000000. */
#define FUXI_VMW_TAG_CA         51
#define FUXI_VMW_ANY_LINES      64
#define FUXI_VMW_WATCH_LINES    600
#define FUXI_VMW_NAME_MAX       17      /* MAXCOMLEN + 1 */

#define FUXI_VMW_PARTIAL        0x1     /* the range covers only part of a tag-51 entry */
#define FUXI_VMW_FOREIGN        0x2     /* the map is not the calling thread's */
#define FUXI_VMW_WATCHED        0x4     /* the caller is the watched process, in its own map */

/* Whether [start, end) leaves part of the entry [es, ee) behind. Both overlap. */
static inline int
fuxi_vmw_partial(unsigned long long start, unsigned long long end,
    unsigned long long es, unsigned long long ee)
{
	return start > es || end < ee;
}

/* The line budget a hit counts against: 1 split or foreign, 2 watched, 0 not logged. */
static inline int
fuxi_vmw_budget(unsigned flags)
{
	if (flags & (FUXI_VMW_PARTIAL | FUXI_VMW_FOREIGN)) {
		return 1;
	}
	return (flags & FUXI_VMW_WATCHED) ? 2 : 0;
}

/* Whether the process name p_comm is the watched one (both NUL terminated, MAXCOMLEN at most). */
static inline int
fuxi_vmw_name_is(const char *comm, const char *watched)
{
	unsigned i;

	if (comm == 0 || watched == 0 || watched[0] == 0) {
		return 0;
	}
	for (i = 0; i < FUXI_VMW_NAME_MAX - 1; i++) {
		if (comm[i] != watched[i]) {
			return 0;
		}
		if (comm[i] == 0) {
			return 1;
		}
	}
	return 1;
}

#if !defined(FUXI_VMW_HOST)
#include <pexpert/pexpert.h>
#include <kern/thread.h>
#include <arm/thread.h>

static int fuxi_vmw_state = -1;         /* -1 unread, 0 off, 1 on */
static char fuxi_vmw_name[FUXI_VMW_NAME_MAX];
static uint32_t fuxi_vmw_lines[3];

static bool
fuxi_vmw_on(void)
{
	int state = os_atomic_load(&fuxi_vmw_state, acquire);
	char name[FUXI_VMW_NAME_MAX] = "";

	if (state >= 0) {
		return state > 0;
	}
	/* First user-map call (the kernel map never gets here): boot-args are parsed long before. */
	if (!PE_parse_boot_arg_str("fuxivmwatch", name, (int)sizeof(name))) {
		name[0] = 0;
	}
	name[sizeof(name) - 1] = 0;
	strlcpy(fuxi_vmw_name, name, sizeof(fuxi_vmw_name));
	state = name[0] ? 1 : 0;
	if (os_atomic_cmpxchg(&fuxi_vmw_state, -1, state, release) && state) {
		printf("FUXI VMW: watching %s: tag-%d removals and write drops (splits and foreign maps in every process)\n",
		    name, FUXI_VMW_TAG_CA);
	}
	return state > 0;
}

/*
 * One removal (op "del", map locked by the caller) or protection change ("prot"/"protmax", map
 * unlocked) of [start, end) in a user map. Reads entries and the caller's saved registers only:
 * no user memory, so it is safe under the map lock.
 */
static void
fuxi_vmw_scan(vm_map_t map, vm_map_offset_t start, vm_map_offset_t end, const char *op, bool locked)
{
	vm_map_entry_t entry;
	vm_map_offset_t es = 0, ee = 0;
	vm_prot_t prot = 0, maxprot = 0;
	int purgeable = -1, hit = 0, partial = 0;
	unsigned flags = 0;

	if (!fuxi_vmw_on()) {
		return;
	}
	if (!locked) {
		vm_map_lock_read(map);
	}
	if (!vm_map_lookup_entry(map, start, &entry)) {
		entry = entry->vme_next;
	}
	for (; entry != vm_map_to_entry(map) && entry->vme_start < end; entry = entry->vme_next) {
		if (entry->is_sub_map || VME_ALIAS(entry) != FUXI_VMW_TAG_CA) {
			continue;
		}
		int split = fuxi_vmw_partial(start, end, entry->vme_start, entry->vme_end);
		if (!hit || (split && !partial)) {
			vm_object_t object = VME_OBJECT(entry);
			es = entry->vme_start;
			ee = entry->vme_end;
			prot = entry->protection;
			maxprot = entry->max_protection;
			purgeable = object != VM_OBJECT_NULL ? (int)object->purgable : -1;
		}
		hit = 1;
		partial |= split;
	}
	if (!locked) {
		vm_map_unlock_read(map);
	}
	if (!hit) {
		return;
	}

	void *bsd = get_bsdtask_info(current_task());
	const char *comm = bsd != NULL ? proc_name_address(bsd) : "?";
	if (partial) {
		flags |= FUXI_VMW_PARTIAL;
	}
	if (map != current_map()) {
		flags |= FUXI_VMW_FOREIGN;
	} else if (fuxi_vmw_name_is(comm, fuxi_vmw_name)) {
		flags |= FUXI_VMW_WATCHED;
	}
	int budget = fuxi_vmw_budget(flags);
	if (budget == 0) {
		return;
	}
	uint32_t n = os_atomic_inc_orig(&fuxi_vmw_lines[budget], relaxed);
	if (n >= (uint32_t)(budget == 1 ? FUXI_VMW_ANY_LINES : FUXI_VMW_WATCH_LINES)) {
		return;
	}

	uint64_t upc = 0, ulr = 0;
	arm_saved_state_t *ss = find_user_regs(current_thread());
	if (ss != NULL && is_saved_state64(ss)) {
		upc = get_saved_state_pc(ss);
		ulr = get_saved_state_lr(ss);
	}
	uintptr_t kbt[7] = { 0 };
	unsigned depth = backtrace(kbt, 7, NULL, NULL);
	char line[400];
	int len = snprintf(line, sizeof(line),
	    "FUXI VMW %s %d %s %s [0x%llx,0x%llx) tag%d [0x%llx,0x%llx) %d/%d purg %d%s upc 0x%llx ulr 0x%llx kbt",
	    op, proc_selfpid(), comm, (flags & FUXI_VMW_FOREIGN) ? "foreign" : "own",
	    (unsigned long long)start, (unsigned long long)end, FUXI_VMW_TAG_CA,
	    (unsigned long long)es, (unsigned long long)ee, prot, maxprot, purgeable,
	    partial ? " SPLIT" : "", (unsigned long long)upc, (unsigned long long)ulr);
	for (unsigned i = 1; i < depth && len > 0 && len < (int)sizeof(line); i++) {
		len += snprintf(line + len, sizeof(line) - len, " 0x%lx", (unsigned long)VM_KERNEL_UNSLIDE(kbt[i]));
	}
	printf("%s\n", line);   /* one call: lines from other CPUs do not interleave */
}
#endif /* !FUXI_VMW_HOST */
#endif /* FUXI_VM_WATCH_H */
