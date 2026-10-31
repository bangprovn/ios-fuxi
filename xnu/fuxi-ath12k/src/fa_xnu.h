/*
 * The boundary between the driver domain (this object) and the XNU domain (fuxi_wlan.c), plain C on
 * both sides so that no Linux or FreeBSD type crosses it. Design section 2, hand-off sequence.
 * The fa_ functions are all this object shows; the fuxi_ath12k_ functions are what milestone M1 must
 * add to fuxi_wlan.c (tools/ath12k-undefined-allow.json lists them).
 *
 * SPDX-License-Identifier: MIT
 */
#ifndef _FA_XNU_H_
#define _FA_XNU_H_

#define FA_DEV_MEM_MOST		4
#define FA_BUILD_ID_MOST	128

/* What our QMI learnt from the capability response before FW_READY. */
struct fa_caps {
	unsigned	chip_id;
	unsigned	chip_family;
	unsigned	board_id;		/* 0xff when the firmware does not say */
	unsigned	soc_id;
	unsigned	fw_version;
	char		fw_build_id[FA_BUILD_ID_MOST];
	struct {
		unsigned long long start;
		unsigned long long size;
	} dev_mem[FA_DEV_MEM_MOST];		/* [0] is the CMEM ath12k's dp.c uses as a base */
};

/* What ath12k_qmi_firmware_start() hands to our INI, CFG and MODE: tables as the firmware reads them. */
struct fa_wlan_cfg {
	const void	*target_ce;		/* struct ce_pipe_config[] */
	unsigned	 target_ce_count;
	const void	*service_map;		/* struct service_to_pipe[] */
	unsigned	 service_map_count;
	const unsigned	*shadow;		/* register addresses, shadow_reg_v3 */
	unsigned	 shadow_count;
};

/* The driver domain offers these. */
int	fa_firmware_ready(const struct fa_caps *caps);	/* on fw_thread, once, after FW_READY; 0 or -errno */
void	fa_stop(void);					/* the rings are looked at no more; before containment */
void	fa_set_radio_mac(const unsigned char *mac);	/* before fa_firmware_ready(): station.address */

/* The XNU domain owes these (fuxi_wlan.c, milestone M1). */
void	*fuxi_ath12k_bar(unsigned long *length);	/* BAR0 mapped contiguously for 0x100000 */
unsigned fuxi_ath12k_read32(unsigned offset);		/* below 0x80000 of BAR0, fault contained */
void	 fuxi_ath12k_write32(unsigned offset, unsigned value);
void	 fuxi_ath12k_mhi_service(void);			/* wl_w11_events under the MHI lock, guard at 1 Hz */
int	 fuxi_ath12k_firmware_start(const struct fa_wlan_cfg *cfg, unsigned mode);	/* INI, CFG, MODE */
void	 fuxi_ath12k_firmware_stop(void);		/* MODE off */
void	 fuxi_ath12k_contain(void);			/* wl5_abort containment */
void	 fuxi_ath12k_force_rddm(void);			/* wl18_force_rddm */

#endif
