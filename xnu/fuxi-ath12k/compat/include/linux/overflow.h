/*
 * Stand-in for LinuxKPI's linux/overflow.h, which is a copy of Linux's own and carries
 * "GPL-2.0 OR MIT". This domain compiles no text with GPL in its license line, so the
 * few names used are written here on the compiler's overflow builtins: an overflowing size
 * saturates at SIZE_MAX so that the allocation fails, as the Linux interface promises.
 *
 * SPDX-License-Identifier: MIT (see compat/PROVENANCE.md)
 */
#ifndef _FA_LINUX_OVERFLOW_H
#define _FA_LINUX_OVERFLOW_H

#include <linux/compiler.h>
#include <linux/limits.h>
#include <linux/const.h>

#define check_add_overflow(a, b, d)	__builtin_add_overflow((a), (b), (d))
#define check_sub_overflow(a, b, d)	__builtin_sub_overflow((a), (b), (d))
#define check_mul_overflow(a, b, d)	__builtin_mul_overflow((a), (b), (d))

static inline size_t
size_mul(size_t x, size_t y)
{
	size_t product;

	return (__builtin_mul_overflow(x, y, &product) ? SIZE_MAX : product);
}

static inline size_t
size_add(size_t x, size_t y)
{
	size_t sum;

	return (__builtin_add_overflow(x, y, &sum) ? SIZE_MAX : sum);
}

static inline size_t
size_sub(size_t x, size_t y)
{
	size_t difference;

	if (x == SIZE_MAX || y == SIZE_MAX ||
	    __builtin_sub_overflow(x, y, &difference))
		return (SIZE_MAX);
	return (difference);
}

#define array_size(a, b)		size_mul((a), (b))
#define array3_size(a, b, c)		size_mul(size_mul((a), (b)), (c))
#define flex_array_size(p, member, count) \
	size_mul((count), sizeof(*(p)->member))
#define struct_size(p, member, count) \
	size_add(sizeof(*(p)), flex_array_size((p), member, (count)))
#define struct_size_t(type, member, count) \
	struct_size((type *)NULL, member, (count))

/*
 * kzalloc_flex() sets a __counted_by() counter through this where the compiler can tell which
 * field it is (__builtin_counted_by_ref). Neither toolchain used here has that builtin, the
 * driver sets its own counters, and none of the compiled ath12k structures is annotated.
 */
#define __set_flex_counter(FAM, COUNT)	((void)(COUNT))

#endif
