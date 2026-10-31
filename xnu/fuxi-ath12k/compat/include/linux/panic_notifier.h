/*
 * Stand-in: XNU has no panic chain for this domain. ath12k's only use is a callback into
 * hif.panic_handler, which fa_hif.c leaves NULL. Registration is recorded by nobody, so
 * no notifier ever runs; ath12k does not depend on it for anything but a panic dump.
 */
#ifndef _FA_LINUX_PANIC_NOTIFIER_H
#define _FA_LINUX_PANIC_NOTIFIER_H
#include <linux/notifier.h>
struct atomic_notifier_head {
	int unused;
};
extern struct atomic_notifier_head panic_notifier_list;
int atomic_notifier_chain_register(struct atomic_notifier_head *head, struct notifier_block *nb);
int atomic_notifier_chain_unregister(struct atomic_notifier_head *head, struct notifier_block *nb);
#endif
