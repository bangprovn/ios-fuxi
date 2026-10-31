/*
 * Where ath12k meets 802.11 itself: under Linux that is mac80211 and cfg80211 (GPL, not compiled or
 * read), and here it will be the station layer of design section 4. As of M4 what is real: the radio
 * as ath12k allocates, registers and frees it, the regulatory domain, channel arithmetic, the
 * deferred work queues, and on stage 4 alone one interface in station mode and one scan, as mac80211
 * brings them up: ops.start, add_interface, hw_scan, then remove_interface, ops.stop. There is no
 * station yet, so everything that a station would do logs its name at runtime through STUB(). Nothing
 * here authenticates or associates, and the probe request offers legacy rates only.
 *
 * What the scan hears comes here by both of ath12k's routes: linuxkpi_ieee80211_rx() below. wmi.c says
 * "Firmware is guaranteed to report all essential management frames via WMI while it can deliver some
 * extra via HTT", so beacons and probe responses are expected from WMI_MGMT_RX (no napi) and may also
 * come from the data path (napi, and no signal value). Each route is counted, and src/fa_bss.h takes both.
 *
 * EHT is not offered (design section 9): LinuxKPI's EHT constants are not the standard's bits. An
 * edit keeps ath12k from filling it in, and registration refuses a radio that offers it anyway.
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
#include "fa_bss.h"

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
	volatile unsigned long stopped;		/* transmit queues mac80211 holds stopped */
	/* wiphy work: runs on the radio's queue with the wiphy lock held */
	struct list_head wiphy_works;
	struct work_struct wiphy_runner;
	volatile unsigned regd_applied;		/* domains applied, and when the last one was: a scan waits for them */
	volatile unsigned long long regd_at;
	/* the interface and the scan of stage 4 */
	struct ieee80211_vif *vif;		/* set between add_interface and remove_interface */
	void *vif_memory;			/* kept until the wiphy goes: the poll thread may be iterating */
	bool started;				/* ops.start has run */
	volatile int scan;			/* enum below */
	volatile bool collecting;		/* received frames go in the table */
	unsigned rx_wmi, rx_data, rx_late;	/* frames by route, and those after the scan */
	struct cfg80211_ssid asked;		/* of no length: any network */
	uint8_t probe_rates[32];
	struct fa_bss_table heard;
};

enum { SCAN_NONE, SCAN_RUNNING, SCAN_OVER, SCAN_ABORTED };

/*
 * Bounds of stage 4, so that nothing waits for ever. ath12k's own scan timeout is max_scan_time 20 s + 200 ms
 * (wmi.c, mac.c), its abort takes up to 3 s more and the scan vdev's delete up to 5 s: its answer comes first.
 */
#define FA_SCAN_WAIT_NS		(30 * 1000000000ull)
#define FA_SCAN_LOOK_NS		(100 * 1000000ull)
/* add_interface makes ath12k build and apply the domain again, on its own queue, after a wait of up to 3 s. */
#define FA_REGD_WAIT_NS		(5 * 1000000000ull)
#define FA_REGD_QUIET_NS	(500 * 1000000ull)
#define FA_REGD_LOOK_NS		(50 * 1000000ull)

/* The address of the network an interface has joined while it has joined none: mac.c compares beacons with it. */
static const uint8_t no_network[ETH_ALEN];

static unsigned char radio_mac[ETH_ALEN];

static const char *const band_names[NUM_NL80211_BANDS] = {
	[NL80211_BAND_2GHZ] = "2 GHz", [NL80211_BAND_5GHZ] = "5 GHz", [NL80211_BAND_6GHZ] = "6 GHz",
};

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
	struct workqueue_struct *work;
	struct fa_radio *radio;
	struct wiphy *wiphy;

	if (memory == NULL) {
		return (NULL);
	}
	work = alloc_ordered_workqueue("fa-80211", 0);
	if (work == NULL) {
		kfree(memory);
		return (NULL);
	}
	wiphy = (struct wiphy *)roundup2((uintptr_t)memory, 64);
	radio = (struct fa_radio *)wiphy->priv;
	radio->memory = memory;
	radio->wiphy = wiphy;
	radio->ops = ops;
	radio->hw.wiphy = wiphy;
	radio->hw.priv = (uint8_t *)radio + ours;
	radio->work = work;
	INIT_LIST_HEAD(&radio->wiphy_works);
	INIT_WORK(&radio->wiphy_runner, wiphy_runner);
	mutex_init(&wiphy->mtx);
	/* What ath12k reads when an interface comes (M4), as a new wiphy has them: thresholds off. */
	wiphy->rts_threshold = (uint32_t)-1;
	wiphy->frag_threshold = (uint32_t)-1;
	wiphy->retry_short = 7;
	wiphy->retry_long = 4;
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

/*
 * BEHAVIOUR STUB until M7: nothing above this layer needs telling yet, so registering is checking what
 * ath12k has set up, and keeping each channel's own limits for the regulatory domain to start from.
 * Each band says what it holds. A radio without usable
 * bands or address is refused here, where it can be read, rather than at the first scan; and so is
 * one that offers EHT.
 */
int
linuxkpi_80211_wiphy_register(struct wiphy *wiphy)
{
	unsigned bands = 0;

	if (is_zero_ether_addr(wiphy->perm_addr)) {
		lx_tell("FUXI ATH12K: wiphy refused: no address\n");
		return (-EINVAL);
	}
	for (unsigned band = 0; band < NUM_NL80211_BANDS; band++) {
		struct ieee80211_supported_band *has = wiphy->bands[band];
		int he = 0, eht = 0;

		if (has == NULL) {
			continue;
		}
		if (has->channels == NULL || has->n_channels <= 0 || has->bitrates == NULL || has->n_bitrates <= 0) {
			lx_tell("FUXI ATH12K: wiphy refused: band %u has %d channels and %d rates\n", band, has->n_channels, has->n_bitrates);
			return (-EINVAL);
		}
		has->band = band;
		for (int i = 0; i < has->n_channels; i++) {
			has->channels[i].band = band;
			has->channels[i].orig_flags = has->channels[i].flags;
			has->channels[i].orig_mpwr = has->channels[i].max_power;
		}
		for (int i = 0; i < has->n_iftype_data; i++) {
			he += has->iftype_data[i].he_cap.has_he;
			eht += has->iftype_data[i].eht_cap.has_eht;
		}
		lx_tell("FUXI ATH12K: wiphy %s channels %d rates %d he %d eht %d\n", band_names[band], has->n_channels, has->n_bitrates, he, eht);
		if (eht != 0) {
			lx_tell("FUXI ATH12K: wiphy refused: EHT must not be offered (design section 9)\n");
			return (-EINVAL);
		}
		bands++;
	}
	if (bands == 0) {
		lx_tell("FUXI ATH12K: wiphy refused: no bands\n");
		return (-EINVAL);
	}
	return (0);
}

/* BEHAVIOUR STUB until M4: the radio exists from here on, and that is all. ath12k takes it as attached. */
int
linuxkpi_ieee80211_ifattach(struct ieee80211_hw *hw)
{
	radio_of(hw)->registered = true;
	lx_tell("FUXI ATH12K: radio registered; no interface, scan or station is brought up from it before stage 4\n");
	return (0);
}

/* tools/ath12k-edits.json, ath12k-no-eht: asked only when the firmware's services include 802.11be. */
bool
fa_refuse_eht(void)
{
	static bool said;

	if (!said) {
		said = true;
		lx_tell("FUXI ATH12K: the firmware has 802.11be; not offered, LinuxKPI's EHT constants are wrong (design section 9)\n");
	}
	return (true);
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
		cancel_work_sync(&radio->wiphy_runner);	/* its memory goes next, and the queue's thread stays */
		destroy_workqueue(radio->work);
		radio->work = NULL;
	}
}

void
linuxkpi_wiphy_free(struct wiphy *wiphy)
{
	struct fa_radio *radio = (struct fa_radio *)wiphy->priv;

	kfree(radio->regd);
	kfree(radio->vif_memory);
	kfree(radio->memory);
}

/* ---- the regulatory domain ------------------------------------------------------------------------- */

/*
 * A channel may be used if a rule covers its 20 MHz, and takes the rule's limits; otherwise it is
 * disabled. The driver reads the result off the channels when it tells the firmware which it may
 * use (ath12k_reg_update_chan_list()). What ath12k itself set before registering stays, such as a
 * channel disabled because the radio's range does not reach it. Says what came of it.
 */
static void
regd_apply(struct fa_radio *radio)
{
	const struct linuxkpi_ieee80211_regdomain *regd = radio->regd;
	int usable[NUM_NL80211_BANDS] = { 0 };

	for (unsigned band = 0; band < NUM_NL80211_BANDS; band++) {
		struct ieee80211_supported_band *has = radio->wiphy->bands[band];

		for (int i = 0; has != NULL && i < has->n_channels; i++) {
			struct linuxkpi_ieee80211_channel *channel = &has->channels[i];
			const struct ieee80211_reg_rule *rule = NULL;

			for (int r = 0; r < regd->n_reg_rules && rule == NULL; r++) {
				if (regd->reg_rules[r].freq_range.start_freq_khz <= ((int)channel->center_freq - 10) * 1000 &&
				    regd->reg_rules[r].freq_range.end_freq_khz >= ((int)channel->center_freq + 10) * 1000) {
					rule = &regd->reg_rules[r];
				}
			}
			channel->flags = channel->orig_flags;
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
			channel->psd = rule->psd;
			usable[band] += (channel->flags & IEEE80211_CHAN_DISABLED) == 0;
		}
	}
	lx_tell("FUXI ATH12K: regd %c%c rules %d: usable channels 2 GHz %d, 5 GHz %d, 6 GHz %d\n", regd->alpha2[0], regd->alpha2[1],
	    regd->n_reg_rules, usable[NL80211_BAND_2GHZ], usable[NL80211_BAND_5GHZ], usable[NL80211_BAND_6GHZ]);
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
	/* As cfg80211 tells a self-managed driver that asked. ath12k returns at once while the radio is off. */
	if ((wiphy->flags & WIPHY_FLAG_NOTIFY_REGDOM_BY_DRIVER) != 0 && wiphy->reg_notifier != NULL) {
		struct regulatory_request request = { .initiator = NL80211_REGDOM_SET_BY_DRIVER };

		memcpy(request.alpha2, copy->alpha2, sizeof(request.alpha2));
		wiphy->reg_notifier(wiphy, &request);
	}
	lx_os_lock();
	radio->applying--;
	radio->regd_applied++;
	radio->regd_at = lx_os_nanoseconds();
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

/* The one interface there can be, for as long as it is there. The poll thread comes here for each beacon. */
void
linuxkpi_ieee80211_iterate_interfaces(struct ieee80211_hw *hw, enum ieee80211_iface_iter flags,
    void (*function)(void *, uint8_t *, struct ieee80211_vif *), void *argument)
{
	struct ieee80211_vif *vif = radio_of(hw)->vif;

	if (vif != NULL) {
		function(argument, vif->addr, vif);
	}
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

/* The driver's word that the scan is over. It holds its own data lock here, so this only marks and wakes. */
void
linuxkpi_ieee80211_scan_completed(struct ieee80211_hw *hw, struct cfg80211_scan_info *info)
{
	struct fa_radio *radio = radio_of(hw);

	lx_os_lock();
	if (radio->scan == SCAN_RUNNING) {
		radio->scan = info != NULL && info->aborted ? SCAN_ABORTED : SCAN_OVER;
	}
	lx_os_wakeup(&radio->scan);
	lx_os_unlock();
}

/*
 * A frame the radio received, with the driver's description behind it. BEHAVIOUR STUB, M5: only beacons and
 * probe responses during the scan are looked at. Every other frame is dropped: counted, and said by name
 * when no scan has run.
 */
void
linuxkpi_ieee80211_rx(struct ieee80211_hw *hw, struct sk_buff *skb, struct ieee80211_sta *sta, struct napi_struct *napi,
    struct list_head *list)
{
	struct fa_radio *radio = radio_of(hw);
	const struct ieee80211_rx_status *status = IEEE80211_SKB_RXCB(skb);

	if (!radio->collecting) {
		if (radio->vif == NULL) {
			STUB();
		}
		radio->rx_late++;
		dev_kfree_skb_any(skb);
		return;
	}
	lx_os_lock();
	if (napi != NULL) {
		radio->rx_data++;
	} else {
		radio->rx_wmi++;
	}
	if ((status->flag & RX_FLAG_8023) != 0) {
		radio->heard.others++;
	} else {
		(void)fa_bss_take(&radio->heard, skb->data, skb->len, status->freq,
		    (status->flag & RX_FLAG_NO_SIGNAL_VAL) != 0 ? FA_RSSI_NONE : status->signal);
	}
	lx_os_unlock();
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

/* ---- stage 4: one interface, one scan, and down again ------------------------------------------------ */

/* The radio started and an interface in station mode on it, as mac80211 brings one up. 0, or -errno. */
static int
station_up(struct fa_radio *radio)
{
	struct ieee80211_hw *hw = &radio->hw;
	struct ieee80211_vif *vif;
	int error;

	radio->vif_memory = kzalloc(64 + sizeof(*vif) + hw->vif_data_size, GFP_KERNEL);
	if (radio->vif_memory == NULL) {
		return (-ENOMEM);
	}
	vif = (struct ieee80211_vif *)roundup2((uintptr_t)radio->vif_memory, 64);
	vif->type = NL80211_IFTYPE_STATION;
	memcpy(vif->addr, hw->wiphy->perm_addr, ETH_ALEN);
	memcpy(vif->bss_conf.addr, hw->wiphy->perm_addr, ETH_ALEN);
	vif->bss_conf.vif = vif;
	vif->bss_conf.bssid = no_network;
	vif->link_conf[0] = &vif->bss_conf;
	if (ieee80211_hw_check(hw, SUPPORTS_TX_ENCAP_OFFLOAD)) {
		vif->offload_flags |= IEEE80211_OFFLOAD_ENCAP_ENABLED;
	}
	if (ieee80211_hw_check(hw, SUPPORTS_RX_DECAP_OFFLOAD)) {
		vif->offload_flags |= IEEE80211_OFFLOAD_DECAP_ENABLED;
	}
	wiphy_lock(hw->wiphy);
	error = radio->ops->start(hw);
	lx_tell("FUXI ATH12K: M4 ops.start %d\n", error);
	fa_m4_credits("after ops.start");
	if (error == 0) {
		radio->started = true;
		error = radio->ops->add_interface(hw, vif);
		lx_tell("FUXI ATH12K: M4 add_interface station %d offload %#x\n", error, vif->offload_flags);
		if (error == 0) {
			radio->vif = vif;
		}
	}
	wiphy_unlock(hw->wiphy);
	return (error);
}

/*
 * ath12k's add_interface builds the domain again and applies it from its own queue, then sends the channels to
 * the firmware. A scan must not read the channels in the middle of that, where a channel out of the domain may
 * look usable, and should not race the channel list. Bounded: when nothing comes, this is the domain of M2.
 */
static void
regd_wait(struct fa_radio *radio, unsigned before)
{
	unsigned long long began = lx_os_nanoseconds();

	while (lx_os_nanoseconds() - began < FA_REGD_WAIT_NS && !fa_m4_trouble()) {
		bool quiet;

		lx_os_lock();
		quiet = radio->regd_applied != before && radio->applying == 0 && lx_os_nanoseconds() - radio->regd_at >= FA_REGD_QUIET_NS;
		if (!quiet) {
			lx_os_sleep(&radio->regd_applied, FA_REGD_LOOK_NS);
		}
		lx_os_unlock();
		if (quiet) {
			break;
		}
	}
	lx_tell("FUXI ATH12K: M4 regd %s after %llu ms\n", radio->regd_applied != before ? "applied again" : "unchanged",
	    (lx_os_nanoseconds() - began) / 1000000ull);
}

/* What was heard, the strongest first. Names of networks, and only on this stage; never their addresses. */
static void
say_heard(struct fa_radio *radio)
{
	const struct fa_bss_table *heard = &radio->heard;
	uint8_t order[FA_BSS_SAID_MOST];
	unsigned said = fa_bss_strongest(heard, order, FA_BSS_SAID_MOST);

	for (unsigned i = 0; i < said; i++) {
		const struct fa_bss *network = &heard->entry[order[i]];
		char name[FA_SSID_ESCAPED_MOST];

		(void)fa_ssid_escape(name, sizeof(name), network->ssid, network->ssid_length);
		lx_tell("FUXI ATH12K: M4 bss %u %d \"%s\"\n", network->frequency, network->rssi, name);
	}
	if (heard->count > said) {
		lx_tell("FUXI ATH12K: M4 bss %u weaker ones not said\n", heard->count - said);
	}
	lx_tell("FUXI ATH12K: M4 rx wmi %u data %u beacons %u responses %u malformed %u others %u lost %u\n", radio->rx_wmi,
	    radio->rx_data, heard->beacons, heard->responses, heard->malformed, heard->others, heard->lost);
}

/* The probe request's rates, from the first band the radio has: 0 bytes when none fit, and that is said. */
static unsigned
probe_rates(struct fa_radio *radio)
{
	for (unsigned band = 0; band < NUM_NL80211_BANDS; band++) {
		const struct ieee80211_supported_band *has = radio->wiphy->bands[band];
		uint16_t rates[16];
		unsigned count = 0;

		if (has == NULL) {
			continue;
		}
		for (int i = 0; i < has->n_bitrates && count < nitems(rates); i++) {
			rates[count++] = has->bitrates[i].bitrate;
		}
		return (fa_probe_rates(radio->probe_rates, sizeof(radio->probe_rates), rates, count));
	}
	return (0);
}

/*
 * One scan of every channel the regulatory domain allows. Channels it marks no-IR or radar are in the list too:
 * the firmware has them as passive from ath12k's channel list (reg.c), so it sends nothing there. Active asks for
 * any network with an SSID of no length; passive gives ath12k no SSID, which it passes on as WMI_SCAN_FLAG_PASSIVE,
 * and nothing is sent at all. 0 when the driver has said the scan is over.
 */
static int
station_scan(struct fa_radio *radio, bool passive)
{
	struct ieee80211_hw *hw = &radio->hw;
	struct ieee80211_scan_request *request;
	unsigned long long began;
	unsigned rates = 0;
	int channels = 0, no_ir = 0, error, state;
	bool trouble = false;

	for (unsigned band = 0; band < NUM_NL80211_BANDS; band++) {
		channels += hw->wiphy->bands[band] != NULL ? hw->wiphy->bands[band]->n_channels : 0;
	}
	request = kzalloc(sizeof(*request) + channels * sizeof(request->req.channels[0]), GFP_KERNEL);
	if (request == NULL) {
		return (-ENOMEM);
	}
	request->req.wiphy = hw->wiphy;
	for (unsigned band = 0; band < NUM_NL80211_BANDS; band++) {
		struct ieee80211_supported_band *has = hw->wiphy->bands[band];

		for (int i = 0; has != NULL && i < has->n_channels; i++) {
			if ((has->channels[i].flags & IEEE80211_CHAN_DISABLED) == 0) {
				request->req.channels[request->req.n_channels++] = &has->channels[i];
				no_ir += (has->channels[i].flags & (IEEE80211_CHAN_NO_IR | IEEE80211_CHAN_RADAR)) != 0;
			}
		}
	}
	if (request->req.n_channels == 0) {
		lx_tell("FUXI ATH12K: M4 no usable channel in the domain\n");
		kfree(request);
		return (-EINVAL);
	}
	if (!passive) {
		rates = probe_rates(radio);
		request->req.ssids = &radio->asked;
		request->req.n_ssids = 1;
		request->req.ie = radio->probe_rates;
		request->req.ie_len = (int)rates;
	}
	lx_tell("FUXI ATH12K: M4 scan %s channels %d of them passive by regd %d, probe rates %u bytes\n", passive ? "passive" : "active",
	    request->req.n_channels, no_ir, rates);
	lx_os_lock();
	radio->scan = SCAN_RUNNING;
	radio->collecting = true;
	lx_os_unlock();
	began = lx_os_nanoseconds();
	wiphy_lock(hw->wiphy);
	error = radio->ops->hw_scan(hw, radio->vif, request);
	wiphy_unlock(hw->wiphy);
	lx_tell("FUXI ATH12K: M4 hw_scan %d\n", error);
	fa_m4_credits("after hw_scan");
	fa_m4_scan_started();
	if (error != 0) {
		lx_os_lock();
		radio->scan = SCAN_NONE;
		radio->collecting = false;
		lx_os_unlock();
		fa_m4_evidence("hw_scan failed");
		kfree(request);
		return (error);
	}
	while (!trouble && lx_os_nanoseconds() - began < FA_SCAN_WAIT_NS) {
		lx_os_lock();
		if (radio->scan != SCAN_RUNNING) {
			lx_os_unlock();
			break;
		}
		lx_os_sleep(&radio->scan, FA_SCAN_LOOK_NS);
		lx_os_unlock();
		trouble = fa_m4_trouble();
	}
	lx_os_lock();
	state = radio->scan;
	radio->collecting = false;
	lx_os_unlock();
	lx_tell("FUXI ATH12K: M4 scan %s after %llu ms\n", state == SCAN_OVER ? "over" : state == SCAN_ABORTED ? "aborted" :
	    "not over", (lx_os_nanoseconds() - began) / 1000000ull);
	fa_m4_credits("at scan end");
	fa_m4_rx_ring("at scan end");
	if (state == SCAN_OVER) {
		lx_tell("FUXI ATH12K: M4 scan done channels %d bss %u\n", request->req.n_channels, radio->heard.count);
	} else {
		lx_tell("FUXI ATH12K: M4 not proven: channels %d bss %u heard before the end\n", request->req.n_channels, radio->heard.count);
		fa_m4_evidence(trouble ? "scan given up" : state == SCAN_ABORTED ? "scan aborted by ath12k's timeout" : "scan timeout");
	}
	say_heard(radio);
	kfree(request);
	return (state == SCAN_OVER ? 0 : -ETIMEDOUT);
}

/* In mac80211's order: remove_interface needs the radio started, because stop takes it off the list events look in. */
static void
station_down(struct fa_radio *radio)
{
	struct ieee80211_hw *hw = &radio->hw;

	wiphy_lock(hw->wiphy);
	if (radio->scan == SCAN_RUNNING && radio->vif != NULL) {
		radio->ops->cancel_hw_scan(hw, radio->vif);
		lx_tell("FUXI ATH12K: M4 cancel_hw_scan done\n");
	}
	if (radio->vif != NULL) {
		radio->ops->remove_interface(hw, radio->vif);
		radio->vif = NULL;
		lx_tell("FUXI ATH12K: M4 remove_interface done\n");
	}
	if (radio->started) {
		radio->ops->stop(hw, false);
		radio->started = false;
		lx_tell("FUXI ATH12K: M4 ops.stop done\n");
	}
	wiphy_unlock(hw->wiphy);
	lx_tell("FUXI ATH12K: M4 rx after the scan %u frames dropped\n", radio->rx_late);
}

/*
 * Stage 4, from fa_hif.c on fw_thread. On any failure the firmware's own account comes before anything else is
 * asked of it: fa_m4_firmware_dead() stops the rings and forces RDDM, and what follows of the way down then
 * touches only the host. 0, or -errno.
 */
int
fa_m4_station(struct ieee80211_hw *hw, bool passive)
{
	struct fa_radio *radio = radio_of(hw);
	unsigned regd_before = radio->regd_applied;
	int error;

	error = station_up(radio);
	if (error != 0) {
		lx_tell("FUXI ATH12K: M4 not proven: bring-up %d\n", error);
		fa_m4_evidence("bring-up failed");
	} else if (!fa_m4_trouble()) {
		regd_wait(radio, regd_before);
		if (!fa_m4_trouble()) {
			error = station_scan(radio, passive);
		}
	}
	if (fa_m4_trouble()) {
		fa_m4_firmware_dead();
	}
	station_down(radio);
	if (fa_m4_trouble()) {
		fa_m4_firmware_dead();
		return (error != 0 ? error : -EIO);
	}
	return (error);
}

/* Design section 2: recovery is not supported at first. The dump is all that follows. */
void
linuxkpi_ieee80211_restart_hw(struct ieee80211_hw *hw)
{
	lx_tell("FUXI ATH12K: the driver asks for a hardware restart: not supported, forcing RDDM\n");
	fuxi_ath12k_force_rddm();
}
