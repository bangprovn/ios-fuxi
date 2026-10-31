/* Stand-in: FreeBSD's kernel keeps the same predicates in sys/ctype.h; two are not there. */
#ifndef _FA_LINUX_CTYPE_H
#define _FA_LINUX_CTYPE_H
#include <sys/ctype.h>
#ifndef isascii
#define isascii(c)	(((unsigned)(c)) <= 0x7f)
#endif
#endif
