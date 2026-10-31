/*
 * Stand-in: reg.c takes the RTNL around regulatory_set_wiphy_regd_sync(). LinuxKPI defines
 * rtnl_lock() as nothing in linux/netdevice.h, and no other code in this domain relies on it.
 */
#ifndef _FA_LINUX_RTNETLINK_H
#define _FA_LINUX_RTNETLINK_H
#include <linux/netdevice.h>
#endif
