/*
 * Stand-in for LinuxKPI's linux/acpi.h, which pulls in ACPICA: its acenv.h selects a platform by
 * __APPLE__ before __FreeBSD__ and has no such platform. CONFIG_ACPI is off in this domain and
 * ath12k's acpi.h carries stubs of its own, so nothing of ACPI is needed.
 */
#ifndef _FA_LINUX_ACPI_H
#define _FA_LINUX_ACPI_H
#include <linux/device.h>
#include <linux/uuid.h>
#endif
