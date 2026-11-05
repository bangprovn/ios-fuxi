/*
 * FUXI: the user stack of a thread that hammers the built-in AppleKeyStore. fuxi_aks_log.h (SweetKeyStore.cpp)
 * calls fuxi_aks_user_bt on the calling thread, from AppleKeyStoreUserClient::externalMethod, with no lock held,
 * when one pid makes more than FUXI_AKS_BURST_CALLS device-state calls within one second (once per 30 s per pid).
 * A diagnostic: it reads the caller's memory with copyin and changes nothing.
 *
 * Lines, "SWEET AKS BT:" in the kernel log, NAME[PID] on each:
 *   SWEET AKS BT: NAME[PID] tid T "THREAD" sel S: N calls in 1 s; pc 0x.. lr 0x.. fp 0x..
 *   SWEET AKS BT: NAME[PID] shared region 0xBASE size 0xSIZE start 0xSTART slide 0xSLIDE main 0xMH
 *   SWEET AKS BT: NAME[PID] #NN 0xADDR                         an address in the shared region (dyld cache:
 *                                                               unslid = ADDR - SLIDE)
 *   SWEET AKS BT: NAME[PID] #NN 0xADDR img 0xLOAD PATH         outside it: the dyld image (all_image_infos) with
 *                                                               the highest load address at or below ADDR
 *   SWEET AKS BT: NAME[PID] #NN 0xADDR img ?                   outside it, no image list to look in
 *   SWEET AKS BT: NAME[PID] end: K frames, error E, truncated T, images I
 * #00 is the saved user pc (in the mach_msg trap); then XNU's backtrace_user walks the frame records from the
 * saved fp: one return address per record, at most FUXI_AKS_BT_FRAMES addresses. The walk stops at a copyin
 * fault (error E), a misaligned fp, an fp that does not grow, or the cap (truncated 1). lr, the return address
 * into the trap stub's caller, is not part of the walk and is on the first line.
 */
#include <pexpert/arm64/board_config.h>
#if defined(FUXI)
#include <kern/backtrace.h>
#include <kern/misc_protos.h>
#include <kern/task.h>
#include <kern/thread.h>
#include <arm/thread.h>
#include <vm/vm_shared_region.h>
#include <string.h>

#define FUXI_AKS_BT_FRAMES      32
#define FUXI_AKS_BT_IMAGES      4096    /* image list entries looked at, at most */
#define FUXI_AKS_BT_CHUNK       16      /* image list entries per copyin */
#define FUXI_AKS_BT_PATH        160

void fuxi_aks_user_bt(const char *name, int pid, unsigned int selector, unsigned int calls);

/* dyld's struct dyld_image_info for a 64-bit process. */
struct fuxi_aks_image {
	uint64_t load;
	uint64_t path;
	uint64_t mod_date;
};

/*
 * For every address outside [lo, hi): the load address and path pointer of the dyld image with the highest load
 * address at or below it, from the task's dyld_all_image_infos (version, count, infoArray at 0, 4 and 8).
 * Returns the number of list entries read, 0 when there is no list to read.
 */
static unsigned int
fuxi_aks_images(task_t task, const uintptr_t *bt, unsigned int n, uint64_t lo, uint64_t hi, uint64_t *load,
    uint64_t *path)
{
	struct fuxi_aks_image chunk[FUXI_AKS_BT_CHUNK];
	uint64_t infos = task_get_all_image_info_addr(task), array;
	uint32_t head[2];
	unsigned int count, done = 0;

	if (infos == 0 || copyin((user_addr_t)infos, head, sizeof(head)) != 0 ||
	    copyin((user_addr_t)(infos + 8), &array, sizeof(array)) != 0 || array == 0) {
		return 0;
	}
	count = head[1] < FUXI_AKS_BT_IMAGES ? head[1] : FUXI_AKS_BT_IMAGES;
	while (done < count) {
		unsigned int k = count - done < FUXI_AKS_BT_CHUNK ? count - done : FUXI_AKS_BT_CHUNK;
		if (copyin((user_addr_t)(array + (uint64_t)done * sizeof(chunk[0])), chunk, k * sizeof(chunk[0])) != 0) {
			break;
		}
		for (unsigned int i = 0; i < k; i++) {
			for (unsigned int j = 0; j < n; j++) {
				if ((bt[j] >= lo && bt[j] < hi) || chunk[i].load > bt[j] || chunk[i].load <= load[j]) {
					continue;
				}
				load[j] = chunk[i].load;
				path[j] = chunk[i].path;
			}
		}
		done += k;
	}
	return done;
}

void
fuxi_aks_user_bt(const char *name, int pid, unsigned int selector, unsigned int calls)
{
	thread_t thread = current_thread();
	task_t task = current_task();
	arm_saved_state_t *ss = find_user_regs(thread);
	uintptr_t bt[FUXI_AKS_BT_FRAMES];
	uint64_t load[FUXI_AKS_BT_FRAMES], path[FUXI_AKS_BT_FRAMES];
	uint64_t base = 0, size = 0, slide = 0, mh;
	mach_vm_offset_t start = 0;
	vm_shared_region_t sr;
	vm_size_t got;
	struct backtrace_user_info info;
	char tname[64];
	char where[FUXI_AKS_BT_PATH];
	unsigned int n, images;

	if (ss == NULL || !is_saved_state64(ss)) {
		printf("SWEET AKS BT: %s[%d] no 64-bit user state\n", name, pid);
		return;
	}
	memset(tname, 0, sizeof(tname));
	if (get_bsdthread_info(thread) != NULL) {
		thread_get_thread_name(thread, tname);
	}
	tname[sizeof(tname) - 1] = '\0';
	printf("SWEET AKS BT: %s[%d] tid %llu \"%s\" sel %u: %u calls in 1 s; pc 0x%llx lr 0x%llx fp 0x%llx\n", name, pid,
	    thread_tid(thread), tname, selector, calls, (unsigned long long)get_saved_state_pc(ss),
	    (unsigned long long)get_saved_state_lr(ss), (unsigned long long)get_saved_state_fp(ss));

	sr = vm_shared_region_get(task);
	if (sr != NULL) {
		base = sr->sr_base_address;
		size = sr->sr_size;
		slide = sr->sr_slide;
		if (vm_shared_region_start_address(sr, &start, NULL) != KERN_SUCCESS) {
			start = 0;
		}
		vm_shared_region_deallocate(sr);
	}
	task_lock(task);
	mh = task->mach_header_vm_address;
	task_unlock(task);
	printf("SWEET AKS BT: %s[%d] shared region 0x%llx size 0x%llx start 0x%llx slide 0x%llx main 0x%llx\n", name, pid,
	    (unsigned long long)base, (unsigned long long)size, (unsigned long long)start, (unsigned long long)slide,
	    (unsigned long long)mh);

	memset(&info, 0, sizeof(info));
	n = backtrace_user(bt, FUXI_AKS_BT_FRAMES, NULL, &info);
	memset(load, 0, sizeof(load));
	memset(path, 0, sizeof(path));
	images = fuxi_aks_images(task, bt, n, base, base + size, load, path);
	for (unsigned int i = 0; i < n; i++) {
		if (bt[i] >= base && bt[i] < base + size) {
			printf("SWEET AKS BT: %s[%d] #%02u 0x%llx\n", name, pid, i, (unsigned long long)bt[i]);
			continue;
		}
		if (load[i] == 0) {
			printf("SWEET AKS BT: %s[%d] #%02u 0x%llx img ?\n", name, pid, i, (unsigned long long)bt[i]);
			continue;
		}
		/* A path longer than the buffer is printed cut (copyinstr fails but has filled it). */
		got = 0;
		memset(where, 0, sizeof(where));
		if (path[i] != 0) {
			(void)copyinstr((user_addr_t)path[i], where, sizeof(where), &got);
		}
		where[sizeof(where) - 1] = '\0';
		if (where[0] == '\0') {
			where[0] = '?';
		}
		printf("SWEET AKS BT: %s[%d] #%02u 0x%llx img 0x%llx %s\n", name, pid, i, (unsigned long long)bt[i],
		    (unsigned long long)load[i], where);
	}
	printf("SWEET AKS BT: %s[%d] end: %u frames, error %d, truncated %d, images %u\n", name, pid, n, info.btui_error,
	    (info.btui_info & BTI_TRUNCATED) != 0, images);
}
#endif /* FUXI */
