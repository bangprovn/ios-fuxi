/*
 * What the driver domain asks of the XNU side (bsd/dev/fuxi_ath12k_os.c, milestone M1). These
 * calls are plain C on both sides: no FreeBSD or Linux type crosses here. Same shape of boundary
 * as hackios bsd/sys/hakios.lx.h, written for this tree.
 *
 * SPDX-License-Identifier: MIT (see compat/PROVENANCE.md)
 */
#ifndef _LX_OS_H_
#define _LX_OS_H_

/* Wired zeroed kernel memory; the size is given back when freed. */
void		*lx_os_alloc(unsigned long size);
void		 lx_os_free(void *memory, unsigned long size);

/* One piece of Normal-NC memory that the chip reaches at *address (IOVA) through our SMMU window. */
void		*lx_os_radio_memory(unsigned long size, unsigned long long *address);

void		 lx_os_print(const char *text);		/* a line for the log */
void		 lx_os_tell(const char *text);		/* a line for the log that is always kept */
void		 lx_os_panic(const char *text) __dead2;

unsigned long long lx_os_nanoseconds(void);		/* monotonic */

void		*lx_os_thread_self(void);
int		 lx_os_thread(const char *name, void (*function)(void *), void *argument);

/* The one lock of the layer, and sleeping under it: wakes on lx_os_wakeup(address) or after the time. */
void		 lx_os_lock(void);
void		 lx_os_unlock(void);
void		 lx_os_sleep(const void *address, unsigned long long nanoseconds);
void		 lx_os_wakeup(const void *address);

void		 lx_os_random(void *buffer, unsigned long size);
unsigned	 lx_os_option(const char *name);		/* a boot argument as a number; 0 when absent */

#endif
