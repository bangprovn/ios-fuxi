/*
 * Where ath12k meets 802.11 itself: under Linux that is mac80211 and cfg80211 (GPL, not compiled or
 * read), and here it will be the station layer of design section 4. M0 is a link stub with the
 * parts that are plain: the radio's memory, the regulatory domain, channel arithmetic, the deferred
 * work queues. Everything that a station would do logs its name at runtime through STUB().
 *
 * Adapted from hackios wifi/lx/src/lx_80211.c (MIT), see compat/PROVENANCE.md and LICENSE.hackios.
 *
 * SPDX-License-Identifier: MIT
 */
#include <linux/kernel.h>
#include <linux/etherdevice.h>
#include <linux/skbuff.h>
#include <linux/slab.h>
#include <linux/workqueue.h>
#include <net/cfg80211.h>
#include <net/mac80211.h>

#include <lx_os.h>
#include "lx.h"
#include "fa_xnu.h"

/* Says the name a few times and keeps going; each caller then gives its own documented answer. */
#define STUB()	lx_stub(__func__)

/* LinuxKPI's inline TODO() stubs say their name at runtime: they answer without doing the work. */
int linuxkpi_debug_80211 = D80211_TODO;

struct fa_radio {
	struct ieee80211_hw hw;			/* what the driver knows of */
	void *memory;
	struct wiphy *wiphy;
	const struct ieee80211_ops *ops;
	struct workqueue_struct *work;
	struct linuxkpi_ieee80211_regdomain *regd;
	volatile int applying;			/* domains being applied: _unregister waits for them */
	bool registered;
	bool kept;				/* the channels' original limits are kept */
	volatile unsigned long stopped;		/* transmit queues mac80211 holds stopped */
	/* wiphy work: runs on the radio's queue with the wiphy lock held */
	struct list_head wiphy_works;
	struct work_struct wiphy_runner;
};

static unsigned char radio_mac[ETH_ALEN];

static struct fa_radio *
radio_of(struct ieee80211_hw *hw)
{
	return (__containerof(hw, struct fa_radio, hw));
}

/* ---- the address (design section 4, station.address) ----------------------------------------------- */

void
fa_set_radio_mac(const unsigned char *mac)
{
	memcpy(radio_mac, mac, ETH_ALEN);
}

/* tools/ath12k-edits.json, ath12k-mac-override. mac.c has no device-address fallback. */
void
fa_radio_mac(u8 *mac)
{
	if (is_zero_ether_addr(radio_mac)) {
		lx_tell("FUXI ATH12K: no station address given: the radio keeps the firmware's\n");
		return;
	}
	memcpy(mac, radio_mac, ETH_ALEN);
}

/* ---- the radio, as the driver makes and registers it ------------------------------------------------ */

static void wiphy_runner(struct work_struct *work);

struct ieee80211_hw *
linuxkpi_ieee80211_alloc_hw(size_t private, const struct ieee80211_ops *ops)
{
	size_t ours = roundup2(sizeof(struct fa_radio), 64);
	uint8_t *memory = kzalloc(64 + offsetof(struct wiphy, priv) + ours + private, GFP_KERNEL);
	struct fa_radio *radio;
	struct wiphy *wiphy;

	if (memory == NULL) {
		return (NULL);
	}
	wiphy = (struct wiphy *)roundup2((uintptr_t)memory, 64);
	radio = (struct fa_radio *)wiphy->priv;
	radio->memory = memory;
	radio->wiphy = wiphy;
	radio->ops = ops;
	radio->hw.wiphy = wiphy;
	radio->hw.priv = (uint8_t *)radio + ours;
	radio->work = alloc_ordered_workqueue("fa-80211", 0);
	INIT_LIST_HEAD(&radio->wiphy_works);
	INIT_WORK(&radio->wiphy_runner, wiphy_runner);
	mutex_init(&wiphy->mtx);
	return (&radio->hw);
}

struct ieee80211_hw *
linuxkpi_wiphy_to_ieee80211_hw(struct wiphy *wiphy)
{
	return (&((struct fa_radio *)wiphy->priv)->hw);
}

void
linuxkpi_set_ieee80211_dev(struct ieee80211_hw *hw)
{
}

/* BEHAVIOUR STUB, M2: nothing is registered with anything yet; ath12k takes this as done. */
int
linuxkpi_80211_wiphy_register(struct wiphy *wiphy)
{
	STUB();
	return (0);
}

/* BEHAVIOUR STUB, M2: records the radio; no interface or station is brought up from it yet. */
int
linuxkpi_ieee80211_ifattach(struct ieee80211_hw *hw)
{
	STUB();
	radio_of(hw)->registered = true;
	return (0);
}

/* The driver takes its radio back, and frees the channels next: nobody may be at them when this returns. */
void
linuxkpi_ieee80211_unregister_hw(struct ieee80211_hw *hw)
{
	struct fa_radio *radio = radio_of(hw);

	lx_os_lock();
	radio->registered = false;
	while (radio->applying != 0) {
		lx_os_sleep(&radio->applying, 5 * 1000000ull);
	}
	lx_os_unlock();
}

void
linuxkpi_ieee80211_iffree(struct ieee80211_hw *hw)
{
	struct fa_radio *radio = radio_of(hw);

	if (radio->work != NULL) {
		destroy_workqueue(radio->work);
		radio->work = NULL;
	}
}

void
linuxkpi_wiphy_free(struct wiphy *wiphy)
{
	struct fa_radio *radio = (struct fa_radio *)wiphy->priv;

	kfree(radio->regd);
	kfree(radio->memory);
}

/* ---- the regulatory domain ------------------------------------------------------------------------- */

/*
 * A channel may be used if a rule covers its 20 MHz, and takes the rule's limits; otherwise it is
 * disabled. The driver reads the result off the channels when it tells the firmware which it may
 * use (ath12k_reg_update_chan_list()).
 */
static void
regd_apply(struct fa_radio *radio)
{
	const struct linuxkpi_ieee80211_regdomain *regd = radio->regd;

	for (unsigned band = 0; band < NUM_NL80211_BANDS; band++) {
		struct ieee80211_supported_band *has = radio->wiphy->bands[band];

		for (int i = 0; has != NULL && i < has->n_channels; i++) {
			struct linuxkpi_ieee80211_channel *channel = &has->channels[i];
			const struct ieee80211_reg_rule *rule = NULL;

			if (!radio->kept) {
				channel->orig_flags = channel->flags;
				channel->orig_mpwr = channel->max_power;
			}
			for (int r = 0; r < regd->n_reg_rules && rule == NULL; r++) {
				if (regd->reg_rules[r].freq_range.start_freq_khz <= ((int)channel->center_freq - 10) * 1000 &&
				    regd->reg_rules[r].freq_range.end_freq_khz >= ((int)channel->center_freq + 10) * 1000) {
					rule = &regd->reg_rules[r];
				}
			}
			channel->flags = channel->orig_flags & ~(IEEE80211_CHAN_DISABLED | IEEE80211_CHAN_NO_IR | IEEE80211_CHAN_RADAR);
			if (rule == NULL) {
				channel->flags |= IEEE80211_CHAN_DISABLED;
				continue;
			}
			if (rule->flags & NL80211_RRF_NO_IR) {
				channel->flags |= IEEE80211_CHAN_NO_IR;
			}
			if (rule->flags & NL80211_RRF_DFS) {
				channel->flags |= IEEE80211_CHAN_RADAR;
			}
			channel->max_reg_power = rule->power_rule.max_eirp / 100;	/* hundredths of a dBm */
			channel->max_antenna_gain = rule->power_rule.max_antenna_gain / 100;
			channel->max_power = channel->orig_mpwr != 0 && channel->orig_mpwr < channel->max_reg_power ?
			    channel->orig_mpwr : channel->max_reg_power;
		}
	}
	radio->kept = true;
}

/*
 * The driver keeps its own copy and may free it. It calls this from work of its own and may be waiting
 * for that work while it holds the radio's lock: so that lock is not taken here. A domain that comes
 * when the radio is no longer registered is not applied.
 */
int
linuxkpi_regulatory_set_wiphy_regd_sync(struct wiphy *wiphy, struct linuxkpi_ieee80211_regdomain *regd)
{
	struct fa_radio *radio = (struct fa_radio *)wiphy->priv;
	struct linuxkpi_ieee80211_regdomain *copy, *old;
	size_t size;

	if (regd == NULL) {
		return (-EINVAL);
	}
	size = sizeof(*regd) + regd->n_reg_rules * sizeof(regd->reg_rules[0]);
	copy = kmalloc(size, GFP_KERNEL);
	if (copy == NULL) {
		return (-ENOMEM);
	}
	memcpy(copy, regd, size);
	lx_os_lock();
	if (!radio->registered) {
		lx_os_unlock();
		kfree(copy);
		return (-ENODEV);
	}
	radio->applying++;
	lx_os_unlock();
	old = radio->regd;
	radio->regd = copy;
	wiphy->regd = copy;
	regd_apply(radio);
	lx_os_lock();
	radio->applying--;
	lx_os_wakeup(&radio->applying);
	lx_os_unlock();
	kfree(old);
	return (0);
}

/* IEEE 802.11-2020, Annex E: a channel's centre, in MHz */
uint32_t
linuxkpi_ieee80211_channel_to_frequency(uint32_t channel, enum nl80211_band band)
{
	switch (band) {
	case NL80211_BAND_2GHZ:
		return (channel == 14 ? 2484 : 2407 + 5 * channel);
	case NL80211_BAND_5GHZ:
		return (channel >= 182 && channel <= 196 ? 4000 + 5 * channel : 5000 + 5 * channel);
	case NL80211_BAND_6GHZ:
		return (channel == 2 ? 5935 : 5950 + 5 * channel);
	default:
		return (0);
	}
}

uint32_t
linuxkpi_ieee80211_frequency_to_channel(uint32_t frequency, uint32_t flags)
{
	return (frequency == 2484 ? 14 : frequency < 2484 ? (frequency - 2407) / 5 : frequency < 5950 ?
	    (frequency - 5000) / 5 : (frequency - 5950) / 5);
}

/* The channel of that frequency among those the radio was registered with. */
struct linuxkpi_ieee80211_channel *
linuxkpi_ieee80211_get_channel(struct wiphy *wiphy, uint32_t frequency)
{
	for (unsigned band = 0; band < NUM_NL80211_BANDS; band++) {
		struct ieee80211_supported_band *has = wiphy->bands[band];

		for (int i = 0; has != NULL && i < has->n_channels; i++) {
			if (has->channels[i].center_freq == frequency) {
				return (&has->channels[i]);
			}
		}
	}
	return (NULL);
}

/* ---- deferred work ---------------------------------------------------------------------------------- */

void
linuxkpi_ieee80211_queue_delayed_work(struct ieee80211_hw *hw, struct delayed_work *work, int delay)
{
	queue_delayed_work(radio_of(hw)->work, work, delay);
}

/* One piece of wiphy work off the list, or NULL. */
static struct wiphy_work *
wiphy_next(struct fa_radio *radio)
{
	struct wiphy_work *work = NULL;

	lx_os_lock();
	if (!list_empty(&radio->wiphy_works)) {
		work = list_first_entry(&radio->wiphy_works, struct wiphy_work, entry);
		list_del_init(&work->entry);
	}
	lx_os_unlock();
	return (work);
}

static void
wiphy_runner(struct work_struct *runner)
{
	struct fa_radio *radio = __containerof(runner, struct fa_radio, wiphy_runner);
	struct wiphy_work *work;

	wiphy_lock(radio->wiphy);
	while ((work = wiphy_next(radio)) != NULL) {
		work->fn(radio->wiphy, work);
	}
	wiphy_unlock(radio->wiphy);
}

void
linuxkpi_wiphy_work_queue(struct wiphy *wiphy, struct wiphy_work *work)
{
	struct fa_radio *radio = (struct fa_radio *)wiphy->priv;

	lx_os_lock();
	if (list_empty(&work->entry)) {
		list_add_tail(&work->entry, &radio->wiphy_works);
	}
	lx_os_unlock();
	queue_work(radio->work, &radio->wiphy_runner);
}

/* The caller holds the wiphy lock, so the work is not running now. */
void
linuxkpi_wiphy_work_cancel(struct wiphy *wiphy, struct wiphy_work *work)
{
	lx_os_lock();
	if (!list_empty(&work->entry)) {
		list_del_init(&work->entry);
	}
	lx_os_unlock();
}

/* The caller holds the wiphy lock: what is pending runs here, up to and including `until`. */
void
linuxkpi_wiphy_work_flush(struct wiphy *wiphy, struct wiphy_work *until)
{
	struct fa_radio *radio = (struct fa_radio *)wiphy->priv;
	struct wiphy_work *work;

	if (until != NULL && list_empty(&until->entry)) {
		return;
	}
	while ((work = wiphy_next(radio)) != NULL) {
		work->fn(wiphy, work);
		if (work == until) {
			break;
		}
	}
}

/* ---- transmit queues -------------------------------------------------------------------------------- */

void
linuxkpi_ieee80211_stop_queues(struct ieee80211_hw *hw)
{
	radio_of(hw)->stopped = ~0ul;
}

void
linuxkpi_ieee80211_wake_queues(struct ieee80211_hw *hw)
{
	radio_of(hw)->stopped = 0;
}

int
ieee80211_queue_stopped(struct ieee80211_hw *hw, int queue)
{
	return ((radio_of(hw)->stopped >> queue) & 1);
}

/* ---- buffers the driver is done with --------------------------------------------------------------- */

void
linuxkpi_ieee80211_tx_status(struct ieee80211_hw *hw, struct sk_buff *skb)
{
	dev_kfree_skb_any(skb);
}

void
linuxkpi_ieee80211_tx_status_ext(struct ieee80211_hw *hw, struct ieee80211_tx_status *status)
{
	if (status->skb != NULL) {
		dev_kfree_skb_any(status->skb);
	}
}

void
linuxkpi_ieee80211_free_txskb(struct ieee80211_hw *hw, struct sk_buff *skb, int status)
{
	dev_kfree_skb_any(skb);
}

/* ---- the station that is not here yet: each says its name ------------------------------------------- */

/* BEHAVIOUR STUB, M5: there are no interfaces, so nothing is visited. */
void
linuxkpi_ieee80211_iterate_interfaces(struct ieee80211_hw *hw, enum ieee80211_iface_iter flags,
    void (*function)(void *, uint8_t *, struct ieee80211_vif *), void *argument)
{
	STUB();
}

/* BEHAVIOUR STUB, M5: there are no stations, so nothing is visited. */
void
linuxkpi_ieee80211_iterate_stations_atomic(struct ieee80211_hw *hw, void (*function)(void *, struct ieee80211_sta *), void *argument)
{
	STUB();
}

/* BEHAVIOUR STUB, M5: there are no channel contexts, so nothing is visited. */
void
linuxkpi_ieee80211_iterate_chan_contexts(struct ieee80211_hw *hw,
    void (*function)(struct ieee80211_hw *, struct ieee80211_chanctx_conf *, void *), void *argument)
{
	STUB();
}

/* BEHAVIOUR STUB, M5: no station is known, which ath12k treats as a peer that has gone. */
struct ieee80211_sta *
linuxkpi_ieee80211_find_sta(struct ieee80211_vif *vif, const u8 *address)
{
	STUB();
	return (NULL);
}

/* BEHAVIOUR STUB, M5: no scan results are kept, so ath12k sees an AP without RSN (mac.c peer_assoc). */
struct cfg80211_bss *
linuxkpi_cfg80211_get_bss(struct wiphy *wiphy, struct linuxkpi_ieee80211_channel *channel, const uint8_t *bssid,
    const uint8_t *ssid, size_t ssid_length, enum ieee80211_bss_type type, enum ieee80211_privacy privacy)
{
	STUB();
	return (NULL);
}

void
linuxkpi_cfg80211_put_bss(struct wiphy *wiphy, struct cfg80211_bss *bss)
{
	STUB();
}

/* BEHAVIOUR STUB, M5: nothing is queued above the driver, so nothing is pulled. */
void
linuxkpi_ieee80211_handle_wake_tx_queue(struct ieee80211_hw *hw, struct ieee80211_txq *queue)
{
	STUB();
}

/* BEHAVIOUR STUB, M4: no scan is waited for yet. */
void
linuxkpi_ieee80211_scan_completed(struct ieee80211_hw *hw, struct cfg80211_scan_info *info)
{
	STUB();
}

/* BEHAVIOUR STUB, M5: every received frame is dropped here. */
void
linuxkpi_ieee80211_rx(struct ieee80211_hw *hw, struct sk_buff *skb, struct ieee80211_sta *sta, struct napi_struct *napi,
    struct list_head *list)
{
	STUB();
	dev_kfree_skb_any(skb);
}

void
linuxkpi_ieee80211_beacon_loss(struct ieee80211_vif *vif)
{
	STUB();
}

void
linuxkpi_ieee80211_connection_loss(struct ieee80211_vif *vif)
{
	STUB();
}

/* Design section 2: recovery is not supported at first. The dump is all that follows. */
void
linuxkpi_ieee80211_restart_hw(struct ieee80211_hw *hw)
{
	lx_tell("FUXI ATH12K: the driver asks for a hardware restart: not supported, forcing RDDM\n");
	fuxi_ath12k_force_rddm();
}
