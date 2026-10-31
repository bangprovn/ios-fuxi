/*
 * ath12k's bus, on the chip fuxi_wlan.c has already brought to FW_READY (design section 2 and
 * "HIF fa_hif.c" in section 3). Register access has the logic of Linux's pci.c without its wake
 * calls: MHI wake stays held for the whole conversation and ASPM stays off. All rings are polled
 * and no MSI is offered, so ce.c and dp.c skip ring MSI setup because get_user_msi_vector is NULL.
 *
 * Compile and link proof only so far (M0): none of this has run.
 *
 * SPDX-License-Identifier: MIT
 */
#include <linux/kernel.h>
#include <linux/spinlock.h>

#include <lx_os.h>
#include "lx.h"
#include "fa_xnu.h"

#include "core.h"
#include "ce.h"
#include "debug.h"
#include "dp.h"
#include "hal.h"
#include "hif.h"
#include "wifi7/core.h"
#include "wifi7/hw.h"

/* Design R2: the 512-entry cookie table pages are this size. Silent corruption otherwise. */
_Static_assert(ATH12K_PAGE_SIZE == 4096, "fuxi-ath12k R2: ATH12K_PAGE_SIZE must be 4096");
_Static_assert(ATH12K_MAX_SPT_ENTRIES * 8 == ATH12K_PAGE_SIZE, "fuxi-ath12k R2: one table page holds 512 entries");

/* The values of Linux's pci.c, whose text is not compiled here (they are the chip's facts). */
#define FA_WINDOW_ENABLE	0x40000000u
#define FA_WINDOW_VALUE_SHIFT	19
#define FA_WINDOW_VALUE_MASK	0x3fu
#define FA_WINDOW_START		0x80000u
#define FA_WINDOW_RANGE_MASK	0x7ffffu
#define FA_WINDOW_REGISTER	0x310cu
#define FA_MHI_REGION_FIRST	0x1e0e100u
#define FA_MHI_REGION_LAST	0x1e0effcu

#define FA_DATA_BUDGET		64
#define FA_ROUNDS_MOST		8		/* of data work between two looks at everything: W11 */
#define FA_ACTIVE_NS		(1 * 1000000ull)
#define FA_IDLE_NS		(10 * 1000000ull)
#define FA_IDLE_AFTER		100		/* quiet rounds before backing off */

static struct {
	struct ath12k_base *ab;
	spinlock_t window_lock;
	u32 window;
	volatile bool looking;			/* the poll thread should run */
	volatile bool looks;			/* it runs */
	volatile bool data;			/* DP rings join the poll: irq_enable */
} hif;

/* ---- registers: pci.c:1150-1242 without wake calls ------------------------------------------------- */

static void
window_select(u32 offset)
{
	u32 window = (offset >> FA_WINDOW_VALUE_SHIFT) & FA_WINDOW_VALUE_MASK;

	if (window != hif.window) {
		fuxi_ath12k_write32(FA_WINDOW_REGISTER, FA_WINDOW_ENABLE | window);
		(void)fuxi_ath12k_read32(FA_WINDOW_REGISTER);
		hif.window = window;
	}
}

static u32
hif_read32(struct ath12k_base *ab, u32 offset)
{
	u32 value;

	if (offset < FA_WINDOW_START) {
		return (fuxi_ath12k_read32(offset));
	}
	spin_lock_bh(&hif.window_lock);
	window_select(offset);
	if (offset >= FA_MHI_REGION_FIRST && offset <= FA_MHI_REGION_LAST) {
		value = fuxi_ath12k_read32((offset - FA_MHI_REGION_FIRST) & FA_WINDOW_RANGE_MASK);
	} else {
		value = fuxi_ath12k_read32(FA_WINDOW_START + (offset & FA_WINDOW_RANGE_MASK));
	}
	spin_unlock_bh(&hif.window_lock);
	return (value);
}

static void
hif_write32(struct ath12k_base *ab, u32 offset, u32 value)
{
	if (offset < FA_WINDOW_START) {
		fuxi_ath12k_write32(offset, value);
		return;
	}
	spin_lock_bh(&hif.window_lock);
	window_select(offset);
	if (offset >= FA_MHI_REGION_FIRST && offset <= FA_MHI_REGION_LAST) {
		fuxi_ath12k_write32((offset - FA_MHI_REGION_FIRST) & FA_WINDOW_RANGE_MASK, value);
	} else {
		fuxi_ath12k_write32(FA_WINDOW_START + (offset & FA_WINDOW_RANGE_MASK), value);
	}
	spin_unlock_bh(&hif.window_lock);
}

/* ---- the poll thread ------------------------------------------------------------------------------- */

static void
hif_poll(void *argument)
{
	struct ath12k_base *ab = argument;
	unsigned long long serviced = lx_os_nanoseconds();
	unsigned quiet = 0;

	hif.looks = true;
	while (hif.looking) {
		bool more = false;

		for (u32 i = 0; i < ab->hw_params->ce_count; i++) {
			ath12k_ce_per_engine_service(ab, (u16)i);
		}
		for (int round = 0; hif.data && round < FA_ROUNDS_MOST; round++) {
			more = false;
			for (int i = 0; i < ATH12K_EXT_IRQ_GRP_NUM_MAX; i++) {
				int done = ath12k_dp_service_srng(ab->dp, &ab->ext_irq_grp[i], FA_DATA_BUDGET);

				more |= done > 0;
				quiet = done > 0 ? 0 : quiet;
			}
			if (!more) {
				break;
			}
		}
		fuxi_ath12k_mhi_service();
		if (lx_os_nanoseconds() - serviced > 300000000000ull) {
			serviced = lx_os_nanoseconds();
			lx_memory_say();
		}
		if (!more) {
			quiet += quiet < FA_IDLE_AFTER;
			lx_pause_ms(quiet >= FA_IDLE_AFTER ? FA_IDLE_NS / 1000000u : FA_ACTIVE_NS / 1000000u);
		}
	}
	hif.looks = false;
}

static void
hif_quiesce(void)
{
	hif.data = false;
	hif.looking = false;
	while (hif.looks) {
		lx_pause_ms(1);
	}
}

/* ---- the operations --------------------------------------------------------------------------------- */

static int
hif_start(struct ath12k_base *ab)
{
	int error;

	ath12k_ce_rx_post_buf(ab);
	hif.looking = true;
	error = lx_os_thread("fa-rings", hif_poll, ab);
	if (error != 0) {
		hif.looking = false;
		ath12k_err(ab, "FUXI ATH12K: no poll thread: %d\n", error);
		return (-ENOMEM);
	}
	return (0);
}

static void
hif_stop(struct ath12k_base *ab)
{
	hif_quiesce();
	ath12k_ce_cleanup_pipes(ab);
}

static void
hif_irq_enable(struct ath12k_base *ab)
{
	hif.data = true;
}

static void
hif_irq_disable(struct ath12k_base *ab)
{
	hif.data = false;
}

/* The chip is already up: fuxi_wlan.c did all of it before fa_firmware_ready(). */
static int
hif_power_up(struct ath12k_base *ab)
{
	return (0);
}

static void
hif_power_down(struct ath12k_base *ab, bool is_suspend)
{
	hif_quiesce();
	fuxi_ath12k_contain();
}

/* Same table walk as pci.c:955. */
static int
hif_map_service_to_pipe(struct ath12k_base *ab, u16 service, u8 *ul_pipe, u8 *dl_pipe)
{
	bool ul_set = false, dl_set = false;

	for (u32 i = 0; i < ab->hw_params->svc_to_ce_map_len; i++) {
		const struct service_to_pipe *entry = &ab->hw_params->svc_to_ce_map[i];

		if (__le32_to_cpu(entry->service_id) != service) {
			continue;
		}
		switch (__le32_to_cpu(entry->pipedir)) {
		case PIPEDIR_IN:
			*dl_pipe = __le32_to_cpu(entry->pipenum);
			dl_set = true;
			break;
		case PIPEDIR_OUT:
			*ul_pipe = __le32_to_cpu(entry->pipenum);
			ul_set = true;
			break;
		case PIPEDIR_INOUT:
			*dl_pipe = *ul_pipe = __le32_to_cpu(entry->pipenum);
			dl_set = ul_set = true;
			break;
		default:
			break;
		}
	}
	return (ul_set && dl_set ? 0 : -ENOENT);
}

static const struct ath12k_hif_ops fa_hif_ops = {
	.read32 = hif_read32,
	.write32 = hif_write32,
	.start = hif_start,
	.stop = hif_stop,
	.irq_enable = hif_irq_enable,
	.irq_disable = hif_irq_disable,
	.power_up = hif_power_up,
	.power_down = hif_power_down,
	.map_service_to_pipe = hif_map_service_to_pipe,
	/* get_user_msi_vector, get_msi_address and the rest stay NULL: hif.h answers for them. */
};

/* ---- the hand-off: pci.c's probe order, without PCI (design section 2) ------------------------------ */

void	fa_qmi_take_caps(struct ath12k_base *ab, const struct fa_caps *caps);

int
fa_firmware_ready(const struct fa_caps *caps)
{
	static struct device device;
	struct ath12k_base *ab;
	unsigned long length = 0;
	void *bar;
	int error;

	if (hif.ab != NULL) {
		return (-EBUSY);
	}
	error = lx_base_init();
	if (error == 0) {
		error = lx_pool_init();
	}
	if (error != 0) {
		lx_tell("FUXI ATH12K: layer not up: %d\n", error);
		return (-ENOMEM);
	}
	bar = fuxi_ath12k_bar(&length);
	if (bar == NULL || length < 0x100000) {
		lx_tell("FUXI ATH12K: BAR0 not mapped contiguously: %lu bytes\n", length);
		return (-ENODEV);
	}
	spin_lock_init(&hif.window_lock);
	hif.window = 0;

	ab = ath12k_core_alloc(&device, 0, ATH12K_BUS_PCI);
	if (ab == NULL) {
		return (-ENOMEM);
	}
	ab->dev = &device;
	ab->mem = bar;
	ab->mem_len = length;
	ab->mem_ce = bar;
	ab->hif.ops = &fa_hif_ops;
	ab->fw_mode = ATH12K_FIRMWARE_MODE_NORMAL;
	ab->hw_rev = ATH12K_HW_WCN7850_HW20;
	ab->static_window_map = false;
	ab->target_mem_mode = ATH12K_QMI_MEMORY_MODE_DEFAULT;
	for (int i = 0; i < ATH12K_EXT_IRQ_GRP_NUM_MAX; i++) {
		ab->ext_irq_grp[i].ab = ab;
		ab->ext_irq_grp[i].grp_id = i;
	}
	/* Design R3: tools/ath12k-edits.json clears the pin; say so once, the radio's fate may hang on it. */
	lx_tell("FUXI ATH12K: rfkill pin forced to 0, no switch assumed (design R3)\n");

	error = ath12k_wifi7_hw_init(ab);
	if (error == 0) {
		error = ath12k_core_pre_init(ab);
	}
	if (error == 0) {
		error = ath12k_hal_srng_init(ab);
	}
	if (error == 0) {
		error = ath12k_ce_alloc_pipes(ab);
	}
	if (error != 0) {
		lx_tell("FUXI ATH12K: early init failed: %d\n", error);
		ath12k_core_free(ab);
		return (error);
	}
	ab->qmi.ce_cfg.tgt_ce = ab->hw_params->target_ce_config;
	ab->qmi.ce_cfg.tgt_ce_len = ab->hw_params->target_ce_count;
	ab->qmi.ce_cfg.svc_to_ce_map = ab->hw_params->svc_to_ce_map;
	ab->qmi.ce_cfg.svc_to_ce_map_len = ab->hw_params->svc_to_ce_map_len;
	ab->qmi.service_ins_id = ab->hw_params->qmi_service_ins_id;

	error = ath12k_wifi7_arch_init(ab);
	if (error == 0) {
		error = ath12k_core_init(ab);
	}
	if (error != 0) {
		lx_tell("FUXI ATH12K: core init failed: %d\n", error);
		return (error);
	}
	hif.ab = ab;
	fa_qmi_take_caps(ab, caps);

	error = ath12k_core_qmi_firmware_ready(ab);
	if (error != 0) {
		lx_tell("FUXI ATH12K: firmware ready path failed: %d\n", error);
		return (error);
	}
	set_bit(ATH12K_FLAG_QMI_FW_READY_COMPLETE, &ab->dev_flags);
	return (0);
}

void
fa_stop(void)
{
	hif_quiesce();
}
