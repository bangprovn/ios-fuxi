/*
 * Remote processors: only ahb.c uses these and it is not compiled, but core.c includes this header.
 * Adapted from hackios wifi/lx/include/linux/remoteproc.h (MIT), see compat/PROVENANCE.md.
 */
#ifndef _LX_LINUX_REMOTEPROC_H
#define _LX_LINUX_REMOTEPROC_H

#include <linux/types.h>
#include <linux/of.h>

struct rproc {
	const char *name;
	phandle phandle;
};

struct rproc *rproc_get_by_phandle(phandle handle);
int rproc_boot(struct rproc *rproc);
int rproc_shutdown(struct rproc *rproc);

#endif
