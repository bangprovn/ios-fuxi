/* Backlight policy helpers; no hardware access (fuxipaneldcs>=3 hands the DBV to the scanout side). */
#ifndef FUXI_BACKLIGHT_LOGIC_H
#define FUXI_BACKLIGHT_LOGIC_H
#include <stdint.h>
#define FUXI_BACKLIGHT_MAX 65536u
static inline int fbl_enabled(int present, int value)
{
	return present && (value == 1 || value == 2 || value == 3);
}
static inline int fbl_power_notifications(int present, int value)
{
	return present && (value == 2 || value == 3);
}
static inline int fbl_withhold_on(int present, int value, int on)
{
	/* Mode 3 isolates ON delivery; OFF and framebuffer ordering match mode 2. */
	return present && value == 3 && on;
}
static inline int fbl_startup_trace(int present, int value, int trace_present, int trace)
{
	/* Only the notification-free mode 1 is eligible for this experiment. */
	return present && value == 1 && trace_present && trace == 1;
}
static inline int fbl_read_log_this(uint32_t event, int trace)
{
	return event && (event <= (trace ? 512u : 64u) || (event & 255u) == 0);
}
static inline int fbl_log_this(uint32_t event)
{
	return event && (event <= 64 || (event & 255u) == 0);
}
static inline int fbl_valid(uint64_t value)
{
	return value <= FUXI_BACKLIGHT_MAX;
}
/* fuxipaneldcs>=3: a brightness of 0..65536 as a panel DBV, 4 + v*(cap-4)/65536, so 0 is still 4 (bl-min-level). The scanout
 * side clamps it again to [4, fuxipanelmaxdbv]. */
static inline uint32_t fbl_dbv(uint64_t value, uint32_t cap)
{
	if (cap < 4) cap = 4;
	if (value > FUXI_BACKLIGHT_MAX) value = FUXI_BACKLIGHT_MAX;
	return 4 + (uint32_t)(value * (cap - 4) / FUXI_BACKLIGHT_MAX);
}
#endif
