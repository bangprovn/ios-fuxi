/*
 * FUXI: a rate limit for the "SWEET AKS:" call log of the built-in AppleKeyStore (SweetKeyStore.cpp), and the
 * user stack of a process that hammers its device state. A diagnostic: no selector's answer changes.
 *
 * Why (5 Nov): with the keyboard up on Settings' Wi-Fi password field, Preferences calls selector 17
 * (kAKSGetDeviceState) about 1,200 times a second for as long as the keyboard stays up, and every call printed
 * one IOLog line: about a million kernel log lines in 9 minutes, all copied into iOS's unified log.
 *
 * What the log says now:
 *   - the first FUXI_AKS_FIRST calls of each (pid, process name, selector), one line each as before:
 *       SWEET AKS: NAME sel S arg0 A in I/J out O/P -> 0xRC
 *   - then nothing per call. A thread call prints, FUXI_AKS_PERIOD_S seconds after the first call it has to
 *     count, one line for each pair that made calls since the last such line (rc: the latest call's result):
 *       SWEET AKS: NAME sel S: N calls in the last 10 s (rc 0xRC)
 *     If more than FUXI_AKS_SLOTS pairs are busy at once the oldest is dropped; its uncounted calls are said:
 *       SWEET AKS: N counted calls lost to slot reuse
 *   - when one pid makes more than FUXI_AKS_BURST_CALLS calls of selector FUXI_AKS_BURST_SEL within one second
 *     (a fixed window opened by the first call after the last one ended), the calling thread's user stack:
 *     "SWEET AKS BT:" lines from osfmk/kern/fuxi_aks_bt.c, at most once per FUXI_AKS_BT_GAP_S seconds per pid.
 *
 * The logic half below needs nothing from the kernel, so tools/test-fuxi-aksbt.py runs it on the host
 * (FUXI_AKS_HOST). The kernel half takes one IOLock per call; it prints and backtraces outside the lock.
 */
#ifndef FUXI_AKS_LOG_H
#define FUXI_AKS_LOG_H

#if !defined(FUXI_AKS_HOST)
#include <IOKit/IOLib.h>
#include <IOKit/IOLocks.h>
#include <kern/clock.h>
#include <kern/thread_call.h>
#include <sys/proc.h>
#define FUXI_AKS_PRINTF IOLog
#endif

#define FUXI_AKS_FIRST          8       /* calls printed one by one per (pid, name, selector) */
#define FUXI_AKS_PERIOD_S       10      /* then one count line per pair per this many seconds */
#define FUXI_AKS_BURST_SEL      17      /* kAKSGetDeviceState */
#define FUXI_AKS_BURST_CALLS    200     /* more calls of it than this within one second: a backtrace */
#define FUXI_AKS_BT_GAP_S       30      /* at most one backtrace per pid per this many seconds */
#define FUXI_AKS_SLOTS          64      /* pairs followed at once */
#define FUXI_AKS_NAME           32      /* proc_selfname's buffer, as SweetKeyStore.cpp had it */
#define FUXI_AKS_NSEC           1000000000ULL

/* fuxi_aks_note's answer. */
#define FUXI_AKS_PRINT          0x1     /* print this call's line */
#define FUXI_AKS_COUNTED        0x2     /* counted for the next count line instead: the thread call must run */
#define FUXI_AKS_BT             0x4     /* take the calling thread's user backtrace now */

struct fuxi_aks_slot {
	unsigned long long seen;        /* calls since the slot was taken */
	unsigned long long pending;     /* calls counted and not yet in a count line */
	unsigned long long last_ns;     /* the latest call: the slot least recently used goes first */
	unsigned long long burst_ns;    /* the start of the current one-second window */
	unsigned long long bt_ns;       /* the latest backtrace, if bt_done */
	unsigned int burst;             /* calls of FUXI_AKS_BURST_SEL in that window */
	unsigned int selector;
	int pid;
	int rc;                         /* the latest call's result */
	int used;
	int bt_done;
	char name[FUXI_AKS_NAME];
};

struct fuxi_aks_table {
	struct fuxi_aks_slot slot[FUXI_AKS_SLOTS];
	unsigned long long lost;        /* pending calls of slots taken over, said with the next count lines */
};

/* Whether a slot's name (kept to FUXI_AKS_NAME - 1 characters) is the caller's. */
static inline int
fuxi_aks_same_name(const char *a, const char *b)
{
	for (unsigned int i = 0; i < FUXI_AKS_NAME - 1; i++) {
		if (a[i] != b[i]) {
			return 0;
		}
		if (a[i] == '\0') {
			return 1;
		}
	}
	return 1;
}

/* The slot of (pid, name, selector): its own, else a free one, else the oldest idle one, else the oldest. */
static inline struct fuxi_aks_slot *
fuxi_aks_slot_for(struct fuxi_aks_table *t, int pid, const char *name, unsigned int selector)
{
	struct fuxi_aks_slot *s, *free_slot = NULL, *idle = NULL, *oldest = NULL;

	for (unsigned int i = 0; i < FUXI_AKS_SLOTS; i++) {
		s = &t->slot[i];
		if (!s->used) {
			if (free_slot == NULL) {
				free_slot = s;
			}
			continue;
		}
		if (s->pid == pid && s->selector == selector && fuxi_aks_same_name(s->name, name)) {
			return s;
		}
		if (s->pending == 0 && (idle == NULL || s->last_ns < idle->last_ns)) {
			idle = s;
		}
		if (oldest == NULL || s->last_ns < oldest->last_ns) {
			oldest = s;
		}
	}
	s = free_slot != NULL ? free_slot : idle != NULL ? idle : oldest;
	t->lost += s->pending;
	__builtin_memset(s, 0, sizeof(*s));
	s->used = 1;
	s->pid = pid;
	s->selector = selector;
	for (unsigned int i = 0; i < FUXI_AKS_NAME - 1 && name[i] != '\0'; i++) {
		s->name[i] = name[i];
	}
	return s;
}

/* One call: what to do with it (FUXI_AKS_PRINT or FUXI_AKS_COUNTED, maybe FUXI_AKS_BT with *burst its count). */
static inline unsigned int
fuxi_aks_note(struct fuxi_aks_table *t, int pid, const char *name, unsigned int selector, int rc,
    unsigned long long now_ns, unsigned int *burst)
{
	struct fuxi_aks_slot *s = fuxi_aks_slot_for(t, pid, name, selector);
	unsigned int what;

	s->seen++;
	s->last_ns = now_ns;
	s->rc = rc;
	if (s->seen <= FUXI_AKS_FIRST) {
		what = FUXI_AKS_PRINT;
	} else {
		s->pending++;
		what = FUXI_AKS_COUNTED;
	}
	if (selector != FUXI_AKS_BURST_SEL) {
		return what;
	}
	if (s->burst == 0 || now_ns - s->burst_ns >= FUXI_AKS_NSEC) {
		s->burst_ns = now_ns;
		s->burst = 0;
	}
	s->burst++;
	if (s->burst > FUXI_AKS_BURST_CALLS &&
	    (!s->bt_done || now_ns - s->bt_ns >= FUXI_AKS_BT_GAP_S * FUXI_AKS_NSEC)) {
		s->bt_done = 1;
		s->bt_ns = now_ns;
		*burst = s->burst;
		what |= FUXI_AKS_BT;
	}
	return what;
}

/* The count lines (from the thread call): one per pair with calls counted, then the lost ones. */
static inline unsigned int
fuxi_aks_report(struct fuxi_aks_table *t)
{
	unsigned int lines = 0;

	for (unsigned int i = 0; i < FUXI_AKS_SLOTS; i++) {
		struct fuxi_aks_slot *s = &t->slot[i];
		if (!s->used || s->pending == 0) {
			continue;
		}
		FUXI_AKS_PRINTF("SWEET AKS: %s sel %u: %llu calls in the last %u s (rc 0x%x)\n", s->name, s->selector,
		    s->pending, (unsigned int)FUXI_AKS_PERIOD_S, (unsigned int)s->rc);
		s->pending = 0;
		lines++;
	}
	if (t->lost != 0) {
		FUXI_AKS_PRINTF("SWEET AKS: %llu counted calls lost to slot reuse\n", t->lost);
		t->lost = 0;
		lines++;
	}
	return lines;
}

#if !defined(FUXI_AKS_HOST)
/* osfmk/kern/fuxi_aks_bt.c: the calling thread's user stack, "SWEET AKS BT:" lines. */
extern "C" void fuxi_aks_user_bt(const char *name, int pid, unsigned int selector, unsigned int calls);

static struct fuxi_aks_table fuxi_aks_slots;
static IOLock *fuxi_aks_lock;
static thread_call_t fuxi_aks_call;
static bool fuxi_aks_armed;     /* fuxi_aks_call is queued; under fuxi_aks_lock */

static void
fuxi_aks_flush(thread_call_param_t p0 __unused, thread_call_param_t p1 __unused)
{
	IOLockLock(fuxi_aks_lock);
	fuxi_aks_armed = false;
	(void)fuxi_aks_report(&fuxi_aks_slots);
	IOLockUnlock(fuxi_aks_lock);
}

/* From AppleKeyStore::start, before any user client can call. */
static void
fuxi_aks_log_init(void)
{
	if (fuxi_aks_lock != NULL) {
		return;
	}
	fuxi_aks_call = thread_call_allocate(fuxi_aks_flush, NULL);
	fuxi_aks_lock = IOLockAlloc();
	IOLog("SWEET AKS: call log: the first %u calls per process and selector, then a count every %u s%s; "
	    "user backtrace when a process makes over %u calls of sel %u in 1 s (once per %u s)\n",
	    (unsigned int)FUXI_AKS_FIRST, (unsigned int)FUXI_AKS_PERIOD_S,
	    fuxi_aks_call != NULL ? "" : " (no thread call: counts are not printed)", (unsigned int)FUXI_AKS_BURST_CALLS,
	    (unsigned int)FUXI_AKS_BURST_SEL, (unsigned int)FUXI_AKS_BT_GAP_S);
	if (fuxi_aks_lock == NULL) {
		IOLog("SWEET AKS: no lock for the call log: calls are not logged\n");
	}
}

/* Every call, from AppleKeyStoreUserClient::externalMethod after the answer is made. */
static void
fuxi_aks_log(uint32_t selector, uint64_t arg0, uint32_t in_scalars, uint32_t in_struct, uint32_t out_scalars,
    uint32_t out_struct, IOReturn ret)
{
	char name[FUXI_AKS_NAME];
	uint64_t now_ns, deadline;
	unsigned int what, burst = 0;
	bool arm = false;
	int pid = proc_selfpid();

	if (fuxi_aks_lock == NULL) {
		return;
	}
	proc_selfname(name, sizeof(name));
	absolutetime_to_nanoseconds(mach_absolute_time(), &now_ns);
	IOLockLock(fuxi_aks_lock);
	what = fuxi_aks_note(&fuxi_aks_slots, pid, name, selector, (int)ret, now_ns, &burst);
	if ((what & FUXI_AKS_COUNTED) != 0 && !fuxi_aks_armed && fuxi_aks_call != NULL) {
		fuxi_aks_armed = true;
		arm = true;
	}
	IOLockUnlock(fuxi_aks_lock);
	if (arm) {
		clock_interval_to_deadline(FUXI_AKS_PERIOD_S, kSecondScale, &deadline);
		thread_call_enter_delayed(fuxi_aks_call, deadline);
	}
	if ((what & FUXI_AKS_PRINT) != 0) {
		IOLog("SWEET AKS: %s sel %u arg0 %llu in %u/%u out %u/%u -> 0x%x\n", name, selector,
		    arg0, in_scalars, in_struct, out_scalars, out_struct, ret);
	}
	if ((what & FUXI_AKS_BT) != 0) {
		fuxi_aks_user_bt(name, pid, selector, burst);
	}
}
#endif /* !FUXI_AKS_HOST */

#endif /* FUXI_AKS_LOG_H */
