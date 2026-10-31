/*
 * What ath12k asks of Linux's qmi.c, which is not compiled here: our QMI in fuxi_wlan.c has already
 * done its work when ath12k starts (design section 2). Outside qmi.c only core.c calls these six,
 * and outside it ab->qmi.* is read for ce_cfg, dev_mem (dp.c:1304, wifi7/hal.c:644), target.* and
 * num_radios. MLO stays off: ag->mlo_capable is set only on qmi.c's server-arrive path.
 *
 * SPDX-License-Identifier: MIT
 */
#include <linux/kernel.h>
#include <linux/string.h>

#include <lx_os.h>
#include "lx.h"
#include "fa_xnu.h"

#include "core.h"
#include "qmi.h"

_Static_assert(FA_DEV_MEM_MOST >= ATH12K_QMI_WLFW_MAX_DEV_MEM_NUM_V01,
    "fuxi-ath12k: struct fa_caps cannot hold what ath12k's qmi.h keeps");

/* Design section 2, step 4: "reported only" until now, and dp.c:1304 uses dev_mem as the CMEM base. */
void
fa_qmi_take_caps(struct ath12k_base *ab, const struct fa_caps *caps)
{
	ab->qmi.target.chip_id = caps->chip_id;
	ab->qmi.target.chip_family = caps->chip_family;
	ab->qmi.target.board_id = caps->board_id;
	ab->qmi.target.soc_id = caps->soc_id;
	ab->qmi.target.fw_version = caps->fw_version;
	strlcpy(ab->qmi.target.fw_build_id, caps->fw_build_id, sizeof(ab->qmi.target.fw_build_id));
	for (int i = 0; i < ATH12K_QMI_WLFW_MAX_DEV_MEM_NUM_V01; i++) {
		ab->qmi.dev_mem[i].start = caps->dev_mem[i].start;
		ab->qmi.dev_mem[i].size = caps->dev_mem[i].size;
	}
	if (caps->dev_mem[0].size == 0) {
		lx_tell("FUXI ATH12K: the firmware reported no CMEM; dp.c will use base 0\n");
	}
}

/* Nothing to set up: the service is ours and is already past its work. */
int
ath12k_qmi_init_service(struct ath12k_base *ab)
{
	lx_tell("FUXI ATH12K: qmi shim: QMI stays in fuxi_wlan.c, no service started here\n");
	return (0);
}

void
ath12k_qmi_deinit_service(struct ath12k_base *ab)
{
}

/* Design section 2, step 5: our INI, CFG and MODE, with ath12k's own values. */
int
ath12k_qmi_firmware_start(struct ath12k_base *ab, u32 mode)
{
	const struct ath12k_qmi_ce_cfg *ce = &ab->qmi.ce_cfg;
	struct fa_wlan_cfg cfg = {
		.target_ce = ce->tgt_ce,
		.target_ce_count = ce->tgt_ce_len,
		.service_map = ce->svc_to_ce_map,
		.service_map_count = ce->svc_to_ce_map_len,
		.shadow = ce->shadow_reg_v3,
		.shadow_count = ce->shadow_reg_v3_len,
	};

	return (fuxi_ath12k_firmware_start(&cfg, mode));
}

void
ath12k_qmi_firmware_stop(struct ath12k_base *ab)
{
	fuxi_ath12k_firmware_stop();
}

/* Only on the MLO path of a hardware group, which is not taken here. */
void
ath12k_qmi_trigger_host_cap(struct ath12k_base *ab)
{
	lx_tell("FUXI ATH12K: qmi shim: host capability trigger ignored, HOST_CAP was sent by fuxi_wlan.c\n");
}

/* No MLO memory was ever handed out: ag->mlo_mem.init_done stays false. */
void
ath12k_qmi_reset_mlo_mem(struct ath12k_hw_group *ag)
{
}
