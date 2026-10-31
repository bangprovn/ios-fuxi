/*
 * FreeBSD's <sys/cdefs.h>, for objects that are linked into a Mach-O kernel: a section there is named
 * "segment,section" with at most sixteen characters each, so the ELF section names of FreeBSD's
 * headers cannot be used. The placement attributes (__read_mostly and the like) are dropped; linker
 * sets are made in <sys/linker_set.h>, ours. __GLOBL() is dropped too: sys/pcpu.h uses it to name the
 * ELF bounds of a set that has no counterpart here, which would leave two undefined symbols behind.
 */
#ifndef _LX_SYS_CDEFS_H_
#define _LX_SYS_CDEFS_H_
#include_next <sys/cdefs.h>
#undef __section
#define __section(x)
#undef __GLOBL
#define __GLOBL(sym)
#endif
