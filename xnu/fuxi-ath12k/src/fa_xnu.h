/*
 * The boundary between the driver domain (this object) and the XNU domain (fuxi_wlan.c and
 * bsd/dev/fuxi_ath12k_os.c), plain C on both sides so that no Linux or FreeBSD type crosses it.
 * Design section 2, hand-off sequence. Patch 0117 carries this file to bsd/dev/fa_xnu.h, and
 * tools/ath12k-undefined-allow.json lists what the object may ask for.
 *
 * SPDX-License-Identifier: MIT
 */
#ifndef _FA_XNU_H_
#define _FA_XNU_H_

#define FA_DEV_MEM_MOST		4
#define FA_BUILD_ID_MOST	128

/* The driver domain is compiled with hidden visibility: only these leave the object. */
#define FA_PUBLIC	__attribute__((__visibility__("default")))

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

/* What ath12k_qmi_firmware_start() hands to our INI, CFG and MODE: tables as ath12k keeps them. */
struct fa_wlan_cfg {
	const void	*target_ce;		/* struct ce_pipe_config[]: six little-endian u32 each */
	unsigned	 target_ce_count;
	const void	*service_map;		/* struct service_to_pipe[]: three little-endian u32 each */
	unsigned	 service_map_count;
	const unsigned	*shadow;		/* register addresses, shadow_reg_v3 */
	unsigned	 shadow_count;
};

/* The driver domain offers these. */
FA_PUBLIC int	fa_firmware_ready(const struct fa_caps *caps, unsigned stage);	/* fw_thread, once; 0 or -errno */
FA_PUBLIC void	fa_stop(void);			/* rings and registers left alone from now on; repeatable */
FA_PUBLIC void	fa_set_radio_mac(const unsigned char *mac);	/* before fa_firmware_ready(): station.address */
FA_PUBLIC void	fa_set_target_ssid(const unsigned char *name, unsigned length);	/* before it, stage 5: the open network, 1-32 bytes */
FA_PUBLIC void	fa_set_target_join(const unsigned char *name, unsigned length, const unsigned char *key);	/* before it, stage 6: the network and its 32-byte key */

/* fuxi_wlan.c (FUXI_ATH12K) owes these. */
void	*fuxi_ath12k_bar(unsigned long *length);	/* BAR0 mapped contiguously for 0x100000 */
unsigned fuxi_ath12k_read32(unsigned offset);		/* below 0x100000 of BAR0, fault contained */
void	 fuxi_ath12k_write32(unsigned offset, unsigned value);
void	 fuxi_ath12k_mhi_service(void);			/* MHI events and guard, at most every 10 ms */
int	 fuxi_ath12k_mhi_failed(void);			/* the transport or its guard has failed: firmware or link gone */
int	 fuxi_ath12k_firmware_start(const struct fa_wlan_cfg *cfg, unsigned mode);	/* INI, CFG, MODE */
void	 fuxi_ath12k_firmware_stop(void);		/* MODE off */
void	 fuxi_ath12k_contain(void);			/* containment is wanted when fw_thread gets back */
void	 fuxi_ath12k_force_rddm(void);			/* once: read RDDM if it is there, else wl18_force_rddm */

/* bsd/dev/fuxi_ath12k_os.c owes these, for fuxi_wlan.c: the MHI lock of design section 2. */
void	 fuxi_ath12k_mhi_lock(void);
void	 fuxi_ath12k_mhi_unlock(void);

#endif
