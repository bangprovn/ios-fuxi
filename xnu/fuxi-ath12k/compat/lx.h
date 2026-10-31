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
void	lx_pool_peaks(unsigned *rings_kb, unsigned *slots);	/* lx_base.c: the most of the radio's memory out so far */
void	lx_pause_ms(unsigned ms);	/* lx_base.c: sleep */
void	lx_mark(const char *what);	/* lx_base.c: a point reached, said and waited at if the kernel was asked to */

/* Stage 4, between fa_80211.c, which knows the station, and fa_hif.c, which knows ath12k. */
struct ieee80211_hw;
int	fa_m4_station(struct ieee80211_hw *hw, bool passive);	/* fa_80211.c: up, one scan, down; 0 or -errno */
void	fa_m4_credits(const char *when);	/* fa_hif.c: the HTC credit level, said */
void	fa_m4_rx_ring(const char *when);	/* fa_hif.c: design R6, the refill ring's level, said */
void	fa_m4_scan_started(void);		/* fa_hif.c: per-frame HTC lines off from here on */
void	fa_m4_evidence(const char *why);	/* fa_hif.c: the firmware is taken as dead; credits and last commands, events */
bool	fa_m4_trouble(void);			/* fa_hif.c: has that happened; also looks for a stall or a transport failure */
void	fa_m4_firmware_dead(void);		/* fa_hif.c: fw_thread only: rings stopped, RDDM forced, once */
/* Stage 5: the same evidence, and these. */
int	fa_m5_station(struct ieee80211_hw *hw, bool passive);	/* fa_80211.c: up, scan, join, stay, leave, down; 0 or -errno */
void	fa_m5_rx_ring(const char *when);	/* fa_hif.c: design R6, buffers reaped against buffers put back, said */

#endif
