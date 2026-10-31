/*
 * Adapted from hackios wifi/lx/include/linux/of_reserved_mem.h (MIT), see compat/PROVENANCE.md.
 * No device tree exists in this domain, so a lookup finds nothing, which core.c handles.
 */
#ifndef _LX_LINUX_OF_RESERVED_MEM_H
#define _LX_LINUX_OF_RESERVED_MEM_H

#include <linux/of.h>
#include <linux/ioport.h>

struct reserved_mem {
	const char	*name;
	unsigned long	 base;
	unsigned long	 size;
};

static inline struct reserved_mem *
of_reserved_mem_lookup(struct device_node *np)
{
	return (NULL);
}

#endif
