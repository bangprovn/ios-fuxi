/*
 * What the files of compat/ and src/ know of one another.
 * Adapted from hackios wifi/lx/src/lx.h (MIT), see compat/PROVENANCE.md.
 */
#ifndef _LX_H_
#define _LX_H_

int	lx_base_init(void);		/* lx_base.c: the thread that counts the ticks and runs the timers */
int	lx_pool_init(void);		/* lx_base.c: the memory the radio reads and writes; 0, or an errno */
long	lx_stub(const char *name);	/* lx_base.c: logs that a function has no real work yet */
void	lx_tell(const char *format, ...) __printflike(1, 2);	/* lx_base.c: a line that is always said */
void	lx_memory_say(void);		/* lx_base.c: what the layer holds of memory */
void	lx_pause_ms(unsigned ms);	/* lx_base.c: sleep */
void	lx_mark(const char *what);	/* lx_base.c: a point reached, said and waited at if the kernel was asked to */

#endif
