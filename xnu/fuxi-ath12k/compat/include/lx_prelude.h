/*
 * Included before every file of the driver domain. FreeBSD's 802.11 headers define the access
 * categories (WME_AC_BE ...) as macros and ath12k's headers declare them as an enumeration with
 * the same values: the headers are read first, here, and the macros dropped, so the driver's own
 * declaration stands.
 *
 * Adapted from hackios wifi/lx/include/lx_prelude.h (MIT), see compat/PROVENANCE.md.
 */
#ifndef _LX_PRELUDE_H_
#define _LX_PRELUDE_H_
#include <sys/param.h>
/*
 * Design R2: ATH12K_PAGE_SIZE is PAGE_SIZE and the hardware cookie conversion table is 512 entries
 * of 8 bytes in 4 KiB units. XNU on the phone runs on 16 KiB pages; this domain must not see that.
 */
_Static_assert(PAGE_SIZE == 4096, "fuxi-ath12k R2: the driver domain needs PAGE_SIZE 4096");
#include <net/mac80211.h>
#undef WME_AC_BE
#undef WME_AC_BK
#undef WME_AC_VI
#undef WME_AC_VO
#undef WME_NUM_AC
#include <lx_more.h>
#endif
