/*
 * FUXI ATH12K, the XNU side of the driver domain's calls (compat/include/lx_os.h): memory, time,
 * threads, one lock to sleep under, boot arguments. The DMA pool is fuxi_wlan.c's, which owns the
 * SMMU window. Only kernels built with patch 0117 have this file.
 *
 * Adapted from hackios drivers/wifi/hakios.lx.c (APSL 2.0, as the rest of this kernel's sources).
 */
#if defined(FUXI) && defined(FUXI_ATH12K)
#include <kern/clock.h>
#include <kern/kalloc.h>
#include <kern/locks.h>
#include <kern/sched_prim.h>
#include <kern/thread.h>
#include <libkern/libkern.h>
#include <mach/mach_types.h>
#include <pexpert/pexpert.h>
#include <sys/errno.h>
#include <sys/random.h>
#include <sys/systm.h>

#include "fa_xnu.h"
#include "fa_lx_os.h"

extern kern_return_t thread_terminate(thread_t thread);
extern void thread_set_thread_name(thread_t thread, const char *name);

static LCK_GRP_DECLARE(fa_lock_group, "fuxi-ath12k");
static LCK_MTX_DECLARE(fa_lock, &fa_lock_group);
static LCK_MTX_DECLARE(fa_mhi_lock, &fa_lock_group);

void *
lx_os_alloc(unsigned long size)
{
	return kalloc_data(size, Z_WAITOK | Z_ZERO);
}

void
lx_os_free(void *memory, unsigned long size)
{
	kfree_data(memory, size);
}

void
lx_os_print(const char *text)
{
	printf("%s", text);
}

void
lx_os_tell(const char *text)
{
	printf("%s", text);
}

void
lx_os_panic(const char *text)
{
	panic("%s", text);
}

unsigned long long
lx_os_nanoseconds(void)
{
	uint64_t nanoseconds;

	absolutetime_to_nanoseconds(mach_absolute_time(), &nanoseconds);
	return nanoseconds;
}

void *
lx_os_thread_self(void)
{
	return current_thread();
}

struct fa_thread {
	void (*function)(void *);
	void *argument;
};

static void
fa_thread_runs(void *parameter, __unused wait_result_t result)
{
	struct fa_thread start = *(struct fa_thread *)parameter;

	kfree_type(struct fa_thread, parameter);
	start.function(start.argument);
	(void)thread_terminate(current_thread());
	thread_block(THREAD_CONTINUE_NULL);
}

int
lx_os_thread(const char *name, void (*function)(void *), void *argument)
{
	struct fa_thread *start = kalloc_type(struct fa_thread, Z_WAITOK | Z_ZERO);
	thread_t thread;

	if (start == NULL) {
		return ENOMEM;
	}
	start->function = function;
	start->argument = argument;
	if (kernel_thread_start(fa_thread_runs, start, &thread) != KERN_SUCCESS) {
		kfree_type(struct fa_thread, start);
		return EAGAIN;
	}
	thread_set_thread_name(thread, name);
	thread_deallocate(thread);
	return 0;
}

void
lx_os_lock(void)
{
	lck_mtx_lock(&fa_lock);
}

void
lx_os_unlock(void)
{
	lck_mtx_unlock(&fa_lock);
}

void
lx_os_sleep(const void *address, unsigned long long nanoseconds)
{
	uint64_t deadline;

	nanoseconds_to_absolutetime(nanoseconds, &deadline);
	deadline += mach_absolute_time();
	(void)lck_mtx_sleep_deadline(&fa_lock, LCK_SLEEP_DEFAULT, (event_t)address, THREAD_UNINT, deadline);
}

void
lx_os_wakeup(const void *address)
{
	thread_wakeup((event_t)address);
}

void
lx_os_random(void *buffer, unsigned long size)
{
	read_random(buffer, (u_int)size);
}

unsigned
lx_os_option(const char *name)
{
	unsigned value = 0;

	return PE_parse_boot_argn(name, &value, sizeof(value)) ? value : 0;
}

void
fuxi_ath12k_mhi_lock(void)
{
	lck_mtx_lock(&fa_mhi_lock);
}

void
fuxi_ath12k_mhi_unlock(void)
{
	lck_mtx_unlock(&fa_mhi_lock);
}
#endif /* FUXI && FUXI_ATH12K */
