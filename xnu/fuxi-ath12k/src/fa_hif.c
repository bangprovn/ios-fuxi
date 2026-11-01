/*
 * ath12k's bus, on the chip fuxi_wlan.c has already brought to FW_READY (design section 2 and
 * "HIF fa_hif.c" in section 3). Register access has the logic of Linux's pci.c without its wake
 * calls: MHI wake stays held for the whole conversation and ASPM stays off. All rings are polled
 * and no MSI is offered, so ce.c and dp.c skip ring MSI setup because get_user_msi_vector is NULL.
 *
 * After a failure or a stage stop, fa_stop() ends the poll thread and latches every later register
 * access to nothing, because ath12k's work queues and timers may still run. The rings stay: the
 * pool is never freed, and containment on fw_thread is the only other step. `ab` stays too, which
 * is the price of not unwinding what those workers may name; the radio needs a reboot anyway.
 *
 * Stages: 1 stops after HTC ready, 2 after WMI ready, hw_mode and mac registration. A stage stop goes
 * through ath12k's own error path. Stage 2 takes the data rings no further: hif_irq_enable and
 * ath12k_core_pdev_create() are after it, and no interface exists, so nothing can transmit or scan.
 * Stage 4 goes on through both, runs one scan from fa_80211.c, and stops at the end of the hardware
 * group's start through the same error path, which then also undoes the data rings. Stage 5 does the same
 * around a join of one open network.
 *
 * Firmware-death evidence, stages 4 and 5 only: every WMI command and event is noted, and a WMI timeout, an HTC
 * credit stall, a transport failure or a scan that does not end logs the credits and the last of both.
 * It also sets ATH12K_FLAG_CRASH_FLUSH, ath12k's own way of failing all further commands at once. fw_thread
 * then stops the rings and forces RDDM through the existing path, before any MODE off.
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
#define FA_WMI_EVENTS_SAID	64		/* of the first ones: which, and how long */

#define FA_STAGE_SCAN		4		/* stages 4 to 8 are the only ones that touch the radio's RF */
#define FA_STAGE_JOIN		5
#define FA_STAGE_WPA		6
#define FA_STAGE_ADDRESS	7
#define FA_STAGE_IOS		8		/* the one stage that does not stop: fa_firmware_ready() returns with the poll thread running */
#define FA_STAGE_USES_RF(stage)	((stage) == FA_STAGE_SCAN || (stage) == FA_STAGE_JOIN || (stage) == FA_STAGE_WPA || (stage) == FA_STAGE_ADDRESS || (stage) == FA_STAGE_IOS)
#define FA_IOS_WATCH_NS		(1000 * 1000000ull)

/* fuxiath12kq=MASK, stages 4 and 5 only, each one idea for hunting a dying firmware (design section 6). */
#define FA_KNOB_NO_IDLE_PS	0x01u		/* do not ask for idle power save in ops.start */
#define FA_KNOB_NO_HW_MODE	0x02u		/* do not send WMI_PDEV_SET_HW_MODE */
#define FA_KNOB_ONE_CREDIT	0x04u		/* the design's "one credit": not built, its bit kept */
#define FA_KNOB_SETTLE		0x08u		/* wait after the vdev create command, before its first set_param */
#define FA_KNOB_PASSIVE		0x10u		/* passive scan: nothing is sent over the air */
#define FA_KNOB_SERVE		0x20u		/* stage 7: answer ARP and echoes for a while, for a load from the network */
#define FA_SETTLE_MS		1000u

#define FA_CREDIT_STALL_NS	(10 * 1000000000ull)	/* of no WMI credit at all, while a scan runs and nothing else is asked */
#define FA_RECENT		16		/* commands and events kept for the evidence */

struct fa_recent {
	u32 id, value;				/* command and its result, or event and its bytes */
	unsigned long long at;
};

static struct {
	struct ath12k_base *ab;
	spinlock_t window_lock;
	u32 window;
	volatile bool looking;			/* the poll thread should run */
	volatile bool looks;			/* it runs */
	volatile bool data;			/* DP rings join the poll: irq_enable */
	volatile bool dead;			/* fa_stop(): no more register access */
	unsigned stage;
	bool stage_hit;
	volatile bool ios_up;			/* stage 8 has come up: the poll thread looks out for the firmware */
	int stage_error;			/* what stage 4 came to */
	unsigned knobs;
	volatile bool trouble;			/* the firmware is taken as dead */
	bool rddm_asked;
	unsigned long long credits_gone;	/* since when the WMI endpoint has had none; 0: it has some */
	struct fa_recent commands[FA_RECENT], events[FA_RECENT];
	unsigned command_count, event_count, mgmt_count;
	unsigned rx_head_before;		/* design R6: the refill ring's head before the radio started */
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

	if (hif.dead) {
		return (~0u);
	}
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
	if (hif.dead) {
		return;
	}
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
	unsigned long long serviced = lx_os_nanoseconds(), watched = serviced;
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
		/* Stage 8 lives for as long as the phone does, and nobody else is there to see the firmware go. */
		if (hif.ios_up && lx_os_nanoseconds() - watched > FA_IOS_WATCH_NS) {
			watched = lx_os_nanoseconds();
			if (fa_m4_trouble()) {
				fa_m8_firmware_gone();
			}
		}
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

void
fa_hif_quiesce(void)
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
	fa_hif_quiesce();
	ath12k_ce_cleanup_pipes(ab);
}

static void
hif_irq_enable(struct ath12k_base *ab)
{
	hif.data = true;
	lx_tell("FUXI ATH12K: data rings join the poll\n");
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
	fa_hif_quiesce();
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

/* tools/ath12k-edits.json, ath12k-stage-htc: core.c asks right after HTC ready (design M1). */
bool
fa_stage_htc_ready(struct ath12k_base *ab)
{
	lx_tell("FUXI ATH12K: M1 HTC ready credits %d size %d\n", ab->htc.total_transmit_credits,
	    ab->htc.target_credit_size);
	lx_memory_say();
	if (hif.stage != 1) {
		return (false);
	}
	hif.stage_hit = true;
	return (true);
}

/* ---- stage 4: what the firmware did last, and what follows when it stops --------------------------------- */

static void
recent_note(struct fa_recent *recent, unsigned *count, u32 id, u32 value)
{
	unsigned long long now = lx_os_nanoseconds();

	lx_os_lock();
	recent[*count % FA_RECENT] = (struct fa_recent){ .id = id, .value = value, .at = now };
	*count += 1;
	lx_os_unlock();
}

static void
recent_say(const char *what, const struct fa_recent *from, unsigned count)
{
	struct fa_recent recent[FA_RECENT];
	unsigned long long now = lx_os_nanoseconds();
	unsigned first = count > FA_RECENT ? count - FA_RECENT : 0;

	lx_os_lock();
	memcpy(recent, from, sizeof(recent));
	lx_os_unlock();
	for (unsigned i = first; i < count; i++) {
		const struct fa_recent *one = &recent[i % FA_RECENT];

		lx_tell("FUXI ATH12K: M4 last %s %#x value %d, %llu ms ago\n", what, one->id, (int)one->value, (now - one->at) / 1000000ull);
	}
}

static int
wmi_credits(struct ath12k_base *ab)
{
	return (ab->htc.endpoint[ab->wmi_ab.wmi[0].eid].tx_credits);
}

void
fa_m4_credits(const char *when)
{
	struct ath12k_base *ab = hif.ab;

	lx_tell("FUXI ATH12K: M4 credits %s: wmi %d of %d, commands %u events %u mgmt rx %u\n", when, wmi_credits(ab),
	    ab->htc.total_transmit_credits, hif.command_count, hif.event_count, hif.mgmt_count);
}

/* Design R6: what the data path has left to receive into, against what the ring holds. */
void
fa_m4_rx_ring(const char *when)
{
	struct ath12k_base *ab = hif.ab;
	struct dp_rxdma_ring *ring = &ath12k_ab_to_dp(ab)->rx_refill_buf_ring;
	struct hal_srng *srng = &ab->hal.srng_list[ring->refill_buf_ring.ring_id];
	unsigned rings, slots;
	int free;

	spin_lock_bh(&srng->lock);
	free = ath12k_hal_srng_src_num_free(ab, srng, true);
	spin_unlock_bh(&srng->lock);
	lx_pool_peaks(&rings, &slots);
	lx_tell("FUXI ATH12K: M4 rx refill ring %s: %d free of %u entries, bufs max %d, pool peak %u/%u\n", when, free, srng->num_entries,
	    ring->bufs_max, rings, slots);
}

/*
 * Design R6, from stage 5 on. How many entries are free says nothing of a leak on this chip: ath12k_dp_rx_bufs_replenish() fills the
 * ring once, the firmware moves what it can to the rings of its own, and the ring then reads nearly empty while the firmware
 * holds every buffer (dp_htt.c: "host fills rx buffer to fw and fw fills to rxbuf ring for each rxdma"). WCN7850 has no
 * host2rxdma interrupt in its ring mask, so nothing else refills: each buffer reaped is put back, one for one, or it is
 * lost. So this says what came back against how far the head moved. Reaped is the sum of ath12k's counters for the three
 * places that reap. All of it is memory, no register, and only reads: these counters are not kept under one lock.
 */
void
fa_m5_rx_ring(const char *when)
{
	struct ath12k_base *ab = hif.ab;
	struct ath12k_dp *dp = ath12k_ab_to_dp(ab);
	struct hal_srng *srng = &ab->hal.srng_list[dp->rx_refill_buf_ring.refill_buf_ring.ring_id];
	unsigned entry = srng->entry_size, head, reo = 0, err = dp->device_stats.err_ring_pkts, wbm = 0, free;

	spin_lock_bh(&srng->lock);
	head = srng->u.src_ring.hp / entry;
	free = ath12k_hal_srng_src_num_free(ab, srng, true);
	spin_unlock_bh(&srng->lock);
	for (int i = 0; i < DP_REO_DST_RING_MAX; i++) {
		reo += dp->device_stats.reo_rx[i][ab->device_id];
	}
	for (int i = 0; i < HAL_WBM_REL_SRC_MODULE_MAX; i++) {
		wbm += dp->device_stats.rx_wbm_rel_source[i][ab->device_id];
	}
	lx_tell("FUXI ATH12K: M5 rx refill %s: put back %u, reaped reo %u err %u wbm %u, ring free %u of %u\n", when,
	    (head + srng->num_entries - hif.rx_head_before) % srng->num_entries, reo, err, wbm, free, srng->num_entries);
	for (int i = 0; i < ab->hw_params->num_rxdma_per_pdev && ab->hw_params->rx_mac_buf_ring; i++) {
		struct hal_srng *mac = &ab->hal.srng_list[dp->rx_mac_buf_ring[i].ring_id];

		lx_tell("FUXI ATH12K: M5 firmware's rx ring %d: head %u tail %u of %u\n", i, *mac->u.src_ring.hp_addr / mac->entry_size,
		    *mac->u.src_ring.tp_addr / mac->entry_size, mac->num_entries);
	}
}

/* The first reason gets the whole account; the others a line. From any thread: nothing here waits. */
void
fa_m4_evidence(const char *why)
{
	struct ath12k_base *ab = hif.ab;
	bool first;

	lx_os_lock();
	first = !hif.trouble;
	hif.trouble = true;
	lx_os_unlock();
	if (!first) {
		lx_tell("FUXI ATH12K: M4 trouble again: %s\n", why);
		return;
	}
	set_bit(ATH12K_FLAG_CRASH_FLUSH, &ab->dev_flags);
	lx_tell("FUXI ATH12K: M4 trouble: %s\n", why);
	fa_m4_credits("at the trouble");
	recent_say("command", hif.commands, hif.command_count);
	recent_say("event", hif.events, hif.event_count);
}

/* Asked between the steps and while waiting, on fw_thread. True once the firmware is taken as dead. */
bool
fa_m4_trouble(void)
{
	if (hif.trouble) {
		return (true);
	}
	if (fuxi_ath12k_mhi_failed() != 0) {
		fa_m4_evidence("MHI transport failure");
	} else if (wmi_credits(hif.ab) > 0) {
		hif.credits_gone = 0;
	} else if (hif.credits_gone == 0) {
		hif.credits_gone = lx_os_nanoseconds();
	} else if (lx_os_nanoseconds() - hif.credits_gone >= FA_CREDIT_STALL_NS) {
		fa_m4_evidence("HTC credit stall");
	}
	return (hif.trouble);
}

/* While the scan runs, each frame would be two lines: the credits have been said by now. */
void
fa_m4_scan_started(void)
{
	ath12k_debug_mask &= ~ATH12K_DBG_HTC;
}

/*
 * On fw_thread, once: rings first, because SYS_ERR would have the poll thread read a chip that is going away,
 * and no more register access afterwards. fuxi_ath12k_force_rddm() reads a firmware that is in RDDM already, or
 * asks for it, and scans the upload for the assert text. Nothing is sent to it after this: no MODE off either.
 */
void
fa_m4_firmware_dead(void)
{
	if (hif.rddm_asked) {
		return;
	}
	hif.rddm_asked = true;
	lx_tell("FUXI ATH12K: M4 firmware taken as dead: rings stopped, RDDM next\n");
	fa_hif_quiesce();
	hif.dead = true;
	fuxi_ath12k_force_rddm();
}

static bool
knob(unsigned bit, const char *what)
{
	if ((hif.knobs & bit) == 0) {
		return (false);
	}
	lx_tell("FUXI ATH12K: M4 knob: %s\n", what);
	return (true);
}

/* tools/ath12k-edits.json, ath12k-knob-hw-mode. */
bool
fa_knob_no_hw_mode(void)
{
	return (knob(FA_KNOB_NO_HW_MODE, "no WMI_PDEV_SET_HW_MODE"));
}

/* tools/ath12k-edits.json, ath12k-knob-idle-ps. */
bool
fa_knob_no_idle_ps(void)
{
	return (knob(FA_KNOB_NO_IDLE_PS, "no idle power save"));
}

/* tools/ath12k-edits.json, ath12k-no-11d: only the one scan asked for, not the firmware's periodic country scan. */
bool
fa_no_11d_scan(void)
{
	lx_tell("FUXI ATH12K: M4 no 11d scan: the firmware's country scan is left off\n");
	return (true);
}

/* tools/ath12k-edits.json, ath12k-vdev-created: right after WMI_VDEV_CREATE, before its burst of parameters. */
void
fa_vdev_created(struct ath12k_base *ab, unsigned vdev)
{
	lx_tell("FUXI ATH12K: M4 vdev %u created\n", vdev);
	fa_m4_credits("after vdev create");
	if (knob(FA_KNOB_SETTLE, "settle after vdev create")) {
		lx_pause_ms(FA_SETTLE_MS);
		fa_m4_credits("after the settle");
	}
}

/* tools/ath12k-edits.json, ath12k-scan-vdev-ready: its parameters went through too. */
void
fa_scan_vdev_ready(struct ath12k_base *ab, unsigned vdev, unsigned link)
{
	lx_tell("FUXI ATH12K: M4 scan vdev %u ready on link %u\n", vdev, link);
	fa_m4_credits("after the vdev's parameters");
}

/*
 * tools/ath12k-edits.json, ath12k-scan-link. Upstream scans on link 15 (design section 4), which has no link
 * configuration; without this service mac.c's ath12k_mac_setup_vdev_params_mbssid() then refuses it with -ENOLINK.
 * So only then the scan goes on the interface's own link 0, where the same vdev has what it asks for.
 */
bool
fa_scan_on_link_zero(struct ath12k_base *ab)
{
	bool lacks = !test_bit(WMI_TLV_SERVICE_MBSS_PARAM_IN_VDEV_START_SUPPORT, ab->wmi_ab.svc_map);

	lx_tell("FUXI ATH12K: M4 firmware %s service 253, scan on link %s\n", lacks ? "lacks" : "has", lacks ? "0" : "15 as upstream");
	return (lacks);
}

/* tools/ath12k-edits.json, ath12k-wmi-command: at the end of ath12k_wmi_cmd_send(). Stages 1 and 2 keep nothing. */
void
fa_wmi_command(struct ath12k_base *ab, u32 id, int result)
{
	if (!FA_STAGE_USES_RF(hif.stage)) {
		return;
	}
	recent_note(hif.commands, &hif.command_count, id, (u32)result);
	if (result == -EAGAIN) {
		fa_m4_evidence("WMI command timeout");
	}
}

/*
 * tools/ath12k-edits.json, ath12k-stage-up: core.c asks at the end of the hardware group's start, with the data
 * rings set up and joined to the poll. The rings stop after the scan, as at stage 2, before the radio goes.
 */
bool
fa_stage_up(struct ath12k_base *ab)
{
	struct ath12k *ar = ath12k_ah_to_ar(ath12k_ag_to_ah(ab->ag, 0), 0);
	struct hal_srng *srng;

	if (!FA_STAGE_USES_RF(hif.stage)) {
		return (false);
	}
	lx_tell("FUXI ATH12K: M4 firmware services 11d %d eth offload %d reg ext %d\n", test_bit(WMI_TLV_SERVICE_11D_OFFLOAD, ab->wmi_ab.svc_map),
	    test_bit(WMI_TLV_SERVICE_ETH_OFFLOAD, ab->wmi_ab.svc_map), ath12k_wmi_supports_6ghz_cc_ext(ar));
	fa_m4_rx_ring("before the scan");
	srng = &ab->hal.srng_list[ath12k_ab_to_dp(ab)->rx_refill_buf_ring.refill_buf_ring.ring_id];
	hif.rx_head_before = srng->u.src_ring.hp / srng->entry_size;
	if (hif.stage == FA_STAGE_IOS) {
		return (false);			/* it stays: fa_firmware_ready() brings the interface up when core.c is through */
	}
	if (hif.stage == FA_STAGE_ADDRESS) {
		fa_m7_ask();
		if ((hif.knobs & FA_KNOB_SERVE) != 0) {
			fa_m7_serve();
		}
	}
	hif.stage_error = hif.stage == FA_STAGE_JOIN || hif.stage == FA_STAGE_WPA || hif.stage == FA_STAGE_ADDRESS ? fa_m5_station(ath12k_ar_to_hw(ar), (hif.knobs & FA_KNOB_PASSIVE) != 0) :
	    fa_m4_station(ath12k_ar_to_hw(ar), (hif.knobs & FA_KNOB_PASSIVE) != 0);
	lx_memory_say();
	fa_hif_quiesce();
	hif.stage_hit = true;
	return (true);
}

/* tools/ath12k-edits.json, ath12k-wmi-event-log. Nothing else shows how far a silent firmware got. */
void
fa_wmi_event(u32 id, u32 length)
{
	static unsigned said;			/* (one poll thread) */

	if (FA_STAGE_USES_RF(hif.stage)) {
		recent_note(hif.events, &hif.event_count, id, length);
		if (id == WMI_MGMT_RX_EVENTID) {
			hif.mgmt_count++;	/* one for each frame heard: only counted */
			return;
		}
		if (id == WMI_SCAN_EVENTID || id == WMI_CHAN_INFO_EVENTID) {
			return;			/* several per channel, each with its ATH12K_DBG_WMI line */
		}
	}
	if (said < FA_WMI_EVENTS_SAID) {
		said++;
		lx_tell("FUXI ATH12K: wmi event %#x bytes %u%s\n", id, length, said == FA_WMI_EVENTS_SAID ? " (said for the last time)" : "");
	}
}

/*
 * tools/ath12k-edits.json, ath12k-stage-registered: core.c asks once ath12k_mac_register() has finished,
 * which needs WMI ready and hw_mode behind it. The address is reported from the radio, after the edit
 * that sets it; the firmware's own is the phone's, so only whether it gave one is said. The rings stop
 * here so that no event finds the radio half gone while it is taken down.
 */
bool
fa_stage_registered(struct ath12k_base *ab)
{
	struct ath12k_hw *ah = ath12k_ag_to_ah(ab->ag, 0);
	struct ath12k *ar = ath12k_ah_to_ar(ah, 0);
	const u8 *mac = ar->mac_addr;
	unsigned rings, slots;

	lx_pool_peaks(&rings, &slots);
	lx_tell("FUXI ATH12K: M2 firmware init status %u address %s radios %d phys %u hw mode %d max macs %u 11be %d eth offload %d regd %s\n",
	    ab->wlan_init_status, is_zero_ether_addr(ab->mac_addr) ? "zero" : "given", ab->num_radios, ab->fw_pdev_count,
	    ab->wmi_ab.preferred_hw_mode, ab->target_caps.max_supported_macs, test_bit(WMI_TLV_SERVICE_11BE, ab->wmi_ab.svc_map),
	    test_bit(WMI_TLV_SERVICE_ETH_OFFLOAD, ab->wmi_ab.svc_map), ab->default_regd[ar->pdev_idx] != NULL ? "firmware" : "world");
	if (ab->wmi_ready && ab->wlan_init_status == 0) {
		lx_tell("FUXI ATH12K: M2 WMI ready mac %02x:%02x:%02x:%02x:%02x:%02x macs %d %s pool peak %u/%u\n",
		    mac[0], mac[1], mac[2], mac[3], mac[4], mac[5], ab->num_radios,
		    (hif.knobs & FA_KNOB_NO_HW_MODE) != 0 ? "hw_mode skipped" :
		    ab->hw_params->single_pdev_only ? "hw_mode sent" : "hw_mode none", rings, slots);
	} else {
		lx_tell("FUXI ATH12K: M2 not proven: ready event %d init status %u\n", ab->wmi_ready, ab->wlan_init_status);
	}
	lx_memory_say();
	if (FA_STAGE_USES_RF(hif.stage)) {
		return (false);			/* it goes on: fa_stage_up() */
	}
	fa_hif_quiesce();
	hif.stage_hit = true;
	return (true);
}

int
fa_firmware_ready(const struct fa_caps *caps, unsigned stage)
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
	hif.window = ~0u;			/* unknown: fuxi_wlan.c's diagnostics move and restore it too */
	hif.stage = stage;
	ath12k_debug_mask |= ATH12K_DBG_HTC;	/* design M1: "Target ready!" is part of the proof */
	if (stage >= 2 && stage != FA_STAGE_IOS) {
		/* All one-time at this stage, no data path: what WMI_INIT and the regulatory domain did. */
		ath12k_debug_mask |= ATH12K_DBG_WMI | ATH12K_DBG_REG | ATH12K_DBG_BOOT;
	}
	if (FA_STAGE_USES_RF(stage)) {
		/* Vdev and scan steps. ATH12K_DBG_MGMT stays off: it would be a line for each frame heard. Stage 8 lives long and joins real networks: none. */
		if (stage != FA_STAGE_IOS) {
			ath12k_debug_mask |= ATH12K_DBG_MAC;
		}
		hif.knobs = lx_os_option("fuxiath12kq");
		lx_tell("FUXI ATH12K: M4 knobs %#x: no idle ps %d no hw mode %d settle %d passive %d serve %d%s\n", hif.knobs,
		    (hif.knobs & FA_KNOB_NO_IDLE_PS) != 0, (hif.knobs & FA_KNOB_NO_HW_MODE) != 0, (hif.knobs & FA_KNOB_SETTLE) != 0,
		    (hif.knobs & FA_KNOB_PASSIVE) != 0, (hif.knobs & FA_KNOB_SERVE) != 0, (hif.knobs & FA_KNOB_ONE_CREDIT) != 0 ? "; one credit is not built" : "");
	}

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
	ab->id.vendor = 0x17cb;
	ab->id.device = 0x1107;			/* WCN7850, as the phone's endpoint reports */
	ab->id.bdf_search = ATH12K_BDF_SEARCH_BUS_AND_BOARD;
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
	if (error == -ECANCELED && hif.stage_hit) {
		/* core.c went down its normal error path: HIF stopped, DP down, mode off unless the firmware is dead. */
		return (hif.stage_error);
	}
	if (error != 0) {
		lx_tell("FUXI ATH12K: firmware ready path failed: %d\n", error);
		return (error);
	}
	set_bit(ATH12K_FLAG_QMI_FW_READY_COMPLETE, &ab->dev_flags);
	if (stage == FA_STAGE_IOS) {
		error = fa_m8_up(ath12k_ar_to_hw(ath12k_ah_to_ar(ath12k_ag_to_ah(ab->ag, 0), 0)));
		hif.ios_up = error == 0;
		return (error);
	}
	return (0);
}

void
fa_stop(void)
{
	fa_hif_quiesce();
	hif.dead = true;
}
