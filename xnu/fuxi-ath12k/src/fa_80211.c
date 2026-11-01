/*
 * Where ath12k meets 802.11 itself: under Linux that is mac80211 and cfg80211 (GPL, not compiled or
 * read), and here it will be the station layer of design section 4. As of M4 what is real: the radio
 * as ath12k allocates, registers and frees it, the regulatory domain, channel arithmetic, the
 * deferred work queues, and on stage 4 alone one interface in station mode and one scan, as mac80211
 * brings them up: ops.start, add_interface, hw_scan, then remove_interface, ops.stop. Stage 5 goes on to join
 * one open network named by fuxiwifissid, as mac80211 joins it: channel context, link configuration, the access
 * point as a station from none to authorized, its authentication and association frames, some seconds of
 * listening, and away again. Stage 6 joins one WPA2 network the same way, with its key from fuxi_wlan.c: between the
 * association and the authorized state comes the four-way handshake, then the two keys. The key itself and what is made
 * from it are never said, and are wiped on the way out. Stage 7 then asks for an address by DHCP, finds the router, sends
 * it and a host beyond it one echo, and gives the address back: all from here, nothing above this layer. With fuxiath12kq=0x20 it also
 * answers ARP and echoes for four minutes, so that a host on the network can load the receive path. Stage 8 does not
 * stop: it is the radio for iOS, whose side says what to scan and join and takes the frames. What stands in for mac80211 without
 * doing its work logs its name at runtime through STUB(). The probe request offers legacy rates only.
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
#include <linux/random.h>
#include <linux/skbuff.h>
#include <linux/slab.h>
#include <linux/workqueue.h>
#include <net/cfg80211.h>
#include <net/mac80211.h>

#include <lx_os.h>
#include "lx.h"
#include "fa_xnu.h"
#include "fa_bss.h"
#include "fa_join.h"
#include "fa_wpa.h"
#include "fa_net.h"
#include "fa_ios.h"

/* Says the name a few times and keeps going; each caller then gives its own documented answer. */
#define STUB()	lx_stub(__func__)

/* LinuxKPI's inline TODO() stubs say their name at runtime: they answer without doing the work. */
int linuxkpi_debug_80211 = D80211_TODO;

#define FA_INBOX		4

/* The account of a lost link, stage 8 only. */
#define FA_ASKS_MOST		16		/* the Apple80211 side's last sets */
#define FA_TX_STEP_NS		(5 * 1000000000ull)
#define FA_TX_STEPS		3		/* so that the last stands at least 10 s back */

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
	bool say_names;				/* stage 4 alone logs the networks it hears */
	/* the join of stage 5 */
	struct fa_target target;
	struct fa_width width;			/* of the channel of this join: everything about its width reads this */
	volatile bool joining;			/* received frames go to join_rx() */
	struct ieee80211_chanctx_conf *chanctx;	/* set for as long as the join has one */
	bool chanctx_added;			/* the driver took it */
	void *sta_memory;			/* kept until the wiphy goes: the data path's peer may still name it */
	struct ieee80211_sta *sta;		/* the access point */
	enum ieee80211_sta_state reached;
	volatile enum fa_answer answer;		/* its last word, and what it said */
	unsigned answer_code, aid, answer_length;
	unsigned reason;			/* when it showed us the door */
	uint8_t answer_kind;			/* the answer that is being waited for */
	uint8_t answer_elements[FA_TARGET_ELEMENTS_MOST];
	volatile bool lost, went_protected;
	unsigned beacon_losses;
	unsigned tx_sent, tx_done, tx_acked, tx_dropped;
	unsigned tx_sent_before, tx_done_before;	/* when the exchange that is going on began */
	unsigned link_data, link_beacons, link_others;	/* frames from the access point while joined */
	int rssi;				/* of the access point's last frame that had a value */
	/* the handshake of stage 6 */
	bool protect;
	struct fa_wpa wpa;
	struct cfg80211_bss bss;		/* what ath12k asks of the access point (mac.c, peer assoc) */
	struct cfg80211_bss_ies bss_elements;
	uint8_t eapol[FA_EAPOL_MOST];		/* one message on its way from the poll thread to the join */
	volatile unsigned eapol_length;
	unsigned eapol_heard, eapol_lost;
	unsigned eapol_sent;			/* our messages, and what the data path has finished */
	unsigned data_done, data_acked;
	struct ieee80211_key_conf *pairwise_key, *group_key;	/* set for as long as the driver has them */
	void *key_memory[3];			/* kept until used again or the wiphy goes, wiped: the driver's peer may still name them. 0 pairwise, 1 and 2 by turns the group's */
	bool door_seen;				/* what the last deauthentication or disassociation for us said, and how it came */
	struct fa_door door;
	int door_rssi, door_beacon_rssi;
	unsigned door_beacon_seq, door_other;	/* the beacon's number before it, and such frames with other addresses */
	unsigned long long door_at;
	unsigned beacon_seq;
	unsigned short action[128];		/* action frames from the access point, by category */
	unsigned action_ciphered;
	struct fa_tx_counts tx, tx_then[FA_TX_STEPS];
	unsigned tx_filtered;			/* since the join: what iOS was told went out, and did not */
	unsigned long long tx_then_at[FA_TX_STEPS], ios_joined_at;
	struct { int number, value; unsigned long long at; } asks[FA_ASKS_MOST];
	unsigned asks_count;
	unsigned rx_ok, rx_clear;		/* with the keys in: data the radio deciphered, and data it did not */
	/* the address of stage 7 */
	volatile bool net_on;			/* the poll thread keeps what fa_net_wanted() wants, once it was deciphered */
	volatile bool serving;			/* ... and what fa_net_served() wants: stage 7's load test */
	unsigned served_arp, served_echo;
	struct { unsigned type, length; uint8_t from[6]; uint8_t bytes[FA_NET_MOST]; } inbox[FA_INBOX];
	unsigned inbox_first, inbox_count, inbox_lost;
	bool leased;
	uint8_t own_address[4], server[4], server_hardware[6];
	uint32_t xid;
	unsigned net_sent;
	/* stage 8 */
	bool ios;
	volatile bool ios_joined;		/* frames pass, under the layer's lock */
	volatile unsigned ios_pending;		/* what the thread is to do: below */
	volatile bool ios_powered;		/* iOS's side has asked for the radio */
	volatile bool ios_wifid_asked;		/* it has left or joined: an auto join gives way for good */
	bool ios_auto_joined;			/* the network now joined is the auto join's */
	bool ios_rejoining;			/* it was lost, and ios_rejoin_step() goes after it. Under the gate */
	unsigned rejoin_tries;
	unsigned long long rejoin_at;
	struct fa_candidate *ios_candidates;	/* the access points of the name this join's scans heard */
	unsigned long long ios_up_at;
	volatile bool ios_gate;			/* one of station thread and ios_step() at a time */
	volatile unsigned ios_tx_busy;
	bool ios_dead;
	int ios_phase, ios_last_answer;
	struct fa_heard *ios_table;		/* what the last scan heard */
	unsigned ios_table_count;
	const uint8_t *ios_scan_name;
	unsigned ios_scan_name_length;
	const unsigned short *scan_only;	/* when not NULL, the only frequencies a scan goes to */
	unsigned scan_only_count;
	struct fa_heard ios_bss;
	struct fa_group_pn group_pn;
	unsigned group_slot;
	unsigned rx_replay, rx_no_key, rx_long;
	unsigned long long ios_said;
	/* block ack: the requests of each traffic class, from the poll thread to the stage's own */
	struct fa_ba_ask ba_asked[FA_BA_TIDS];
	unsigned char ba_want, ba_live;
	unsigned ba_started, ba_refused, ba_stopped;
};

enum { IOS_EAPOL = 1, IOS_LOSS = 2, IOS_DEAD = 4, IOS_BA = 8 };

/* What wifi7/dp_rx.c saw (tools/ath12k-edits.json): a descriptor with no buffer behind it, and a buffer that is not done. */
static volatile unsigned rx_stale, rx_not_done, rx_done_later;
#define FA_RX_STALE_SAID	16

void
fa_rx_stale(const char *where, int ring, u32 tp)
{
	unsigned count = __sync_add_and_fetch(&rx_stale, 1);

	if (count <= FA_RX_STALE_SAID) {
		lx_tell("FUXI ATH12K: rx descriptor with no buffer, %s, ring %d tp %u, number %u\n", where, ring, tp, count);
	}
}

void
fa_rx_msdu_note(bool came_up)
{
	__sync_add_and_fetch(&rx_not_done, 1);
	if (came_up) {
		__sync_add_and_fetch(&rx_done_later, 1);
	}
}

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

/*
 * Bounds of stage 5. Each frame is tried three times, half a second each. ath12k's own waits are bounded too: peer
 * create and delete, vdev start and stop, WMI commands and its flush each have a timeout of seconds (core.h, wmi.h, peer.c).
 */
#define FA_JOIN_SCANS		2
#define FA_JOIN_TRIES		3
#define FA_JOIN_ANSWER_NS	(500 * 1000000ull)
#define FA_JOIN_LOOK_NS		(20 * 1000000ull)
#define FA_JOIN_STAY_NS		(20 * 1000000000ull)
#define FA_JOIN_LOOK_STAY_NS	(100 * 1000000ull)
#define FA_JOIN_SAY_EVERY_NS	(5 * 1000000000ull)
#define FA_FRAME_MOST		768		/* what the association request can come to */
/* Bounds of stage 6. The access point begins within a second or so, and resends a message it hears no answer to. */
#define FA_HANDSHAKE_NS		(8 * 1000000000ull)
#define FA_HANDSHAKE_LOOK_NS	(20 * 1000000ull)
#define FA_LAST_MESSAGE_NS	(300 * 1000000ull)	/* message 4 on its way, unciphered, before there is a key to cipher it with */
/* Bounds of stage 7. */
#define FA_DHCP_TRIES		4
#define FA_DHCP_ANSWER_NS	(2 * 1000000000ull)
#define FA_NET_TRIES		3
#define FA_ARP_ANSWER_NS	(500 * 1000000ull)
#define FA_ECHO_ANSWER_NS	(1000 * 1000000ull)
#define FA_NET_LOOK_NS		(20 * 1000000ull)
#define FA_SERVE_NS		(600 * 1000000000ull)
#define FA_SERVE_SAY_NS		(10 * 1000000000ull)

/* The address of the network an interface has joined while it has joined none: mac.c compares beacons with it. */
static const uint8_t no_network[ETH_ALEN];

static unsigned char radio_mac[ETH_ALEN];

/* Stage 8: iOS's side, and the radio once it is up. Set before there is a radio, so not in it. */
static struct fa_ios_input ios_input;
static struct fa_radio *ios_radio;

/* The name of the network stage 5 joins, from fuxi_wlan.c. It is said by its length alone. */
static uint8_t target_name[FA_SSID_MOST];
static unsigned target_name_length;
static uint8_t target_key[32];			/* stage 6: wiped when the join is over */
static bool target_protected;
static bool auto_quiet;				/* fuxiwifiauto=2 */
static bool target_address;			/* stage 7 */
static bool target_serve;			/* stage 7, fuxiath12kq=0x20 */
static unsigned tx_filter;			/* stage 8, fuxiath12kq: FA_TX_DROP_ bits, none unless asked */
/* Stage 8, fuxiwifiauto=1: a network to join with nobody asking, once. The key is wiped when it is used or given up. */
static uint8_t auto_name[FA_SSID_MOST], auto_key[32];
static unsigned auto_name_length;
/* The network the auto join has joined, for joining it again. Kept while that is so, under the gate, and never said. */
static uint8_t rejoin_name[FA_SSID_MOST], rejoin_key[32];
static unsigned rejoin_name_length;
static unsigned target_most = 20;		/* stage 8, MHz: fuxiwifiwide=1 is 80, =2 is 40 */

/* find_sta has only the interface to go by, and there is one radio. */
static struct fa_radio *the_radio;

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

/* Before fa_firmware_ready(). Anything but 1 to 32 bytes is no name at all. */
void
fa_set_target_ssid(const unsigned char *name, unsigned length)
{
	target_protected = false;
	target_address = false;
	target_serve = false;
	target_name_length = length >= 1 && length <= FA_SSID_MOST ? length : 0;
	if (target_name_length != 0) {
		memcpy(target_name, name, target_name_length);
	}
}

/* Before fa_firmware_ready(), stage 6 only. The name is said by its length alone, and the key never. */
void
fa_set_target_join(const unsigned char *name, unsigned length, const unsigned char *key)
{
	fa_set_target_ssid(name, length);
	if (target_name_length != 0) {
		memcpy(target_key, key, sizeof(target_key));
		target_protected = true;
	}
}

/* From fa_hif.c on stage 7, after fa_set_target_join(): the join goes on to ask for an address. */
void
fa_m7_ask(void)
{
	target_address = true;
}

/* From fa_hif.c on stage 7, fuxiath12kq=0x20: with an address, it answers for a while before the stay. */
void
fa_m7_serve(void)
{
	target_serve = true;
}

/* From fa_hif.c on stage 8 alone, before the radio: what iOS sends that goes no further, to learn which of it upsets an access point. */
void
fa_m8_tx_filter(bool ip6, bool tcp, bool udp, bool group)
{
	tx_filter = (ip6 ? FA_TX_DROP_IP6 : 0) | (tcp ? FA_TX_DROP_TCP : 0) | (udp ? FA_TX_DROP_UDP : 0) | (group ? FA_TX_DROP_GROUP : 0);
	lx_tell("FUXI ATH12K: M8 tx filter: ipv6 %d tcp %d udp %d multicast %d\n", ip6, tcp, udp, group);
}

/* From fa_hif.c on stage 8 alone, before the radio, fuxiwifiwide: the channel may be as wide as the access point's, up to this. */
void
fa_m8_wide(unsigned most)
{
	target_most = most;
	lx_tell("FUXI ATH12K: M8 wide channels are asked for, up to %u MHz\n", most);
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
	kfree(radio->key_memory[0]);
	kfree(radio->key_memory[1]);
	kfree(radio->key_memory[2]);
	kfree(radio->ios_table);
	kfree(radio->ios_candidates);
	kfree(radio->sta_memory);
	if (the_radio == radio) {
		the_radio = NULL;
	}
	if (ios_radio == radio) {
		ios_radio = NULL;
	}
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

/* A management frame's end comes here (wmi.c, wmi_process_mgmt_tx_comp()): counted, because a join reads them. */
void
linuxkpi_ieee80211_tx_status(struct ieee80211_hw *hw, struct sk_buff *skb)
{
	struct fa_radio *radio = radio_of(hw);
	bool acked = (IEEE80211_SKB_CB(skb)->flags & IEEE80211_TX_STAT_ACK) != 0;

	lx_os_lock();
	radio->tx_done++;
	radio->tx_acked += acked;
	lx_os_unlock();
	dev_kfree_skb_any(skb);
}

void
linuxkpi_ieee80211_tx_status_ext(struct ieee80211_hw *hw, struct ieee80211_tx_status *status)
{
	struct fa_radio *radio = radio_of(hw);

	lx_os_lock();
	radio->data_done++;
	radio->data_acked += status->info != NULL && (status->info->flags & IEEE80211_TX_STAT_ACK) != 0;
	lx_os_unlock();
	if (status->skb != NULL) {
		dev_kfree_skb_any(status->skb);
	}
}

void
linuxkpi_ieee80211_free_txskb(struct ieee80211_hw *hw, struct sk_buff *skb, int status)
{
	struct fa_radio *radio = radio_of(hw);

	lx_os_lock();
	radio->tx_dropped++;
	lx_os_unlock();
	dev_kfree_skb_any(skb);
}

/* ---- what mac80211 keeps for the driver ---------------------------------------------------------------- */

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

/* The access point, the one station there can be, for as long as the driver knows of it. */
void
linuxkpi_ieee80211_iterate_stations_atomic(struct ieee80211_hw *hw, void (*function)(void *, struct ieee80211_sta *), void *argument)
{
	struct ieee80211_sta *sta = radio_of(hw)->sta;

	if (sta != NULL) {
		function(argument, sta);
	}
}

/* The channel context of the join, the one there can be. */
void
linuxkpi_ieee80211_iterate_chan_contexts(struct ieee80211_hw *hw,
    void (*function)(struct ieee80211_hw *, struct ieee80211_chanctx_conf *, void *), void *argument)
{
	struct ieee80211_chanctx_conf *chanctx = radio_of(hw)->chanctx;

	if (chanctx != NULL) {
		function(hw, chanctx, argument);
	}
}

/* The access point if that is its address. NULL is the true answer for any other. */
struct ieee80211_sta *
linuxkpi_ieee80211_find_sta(struct ieee80211_vif *vif, const u8 *address)
{
	struct fa_radio *radio = the_radio;

	if (radio == NULL || radio->vif != vif || radio->sta == NULL || memcmp(radio->sta->addr, address, ETH_ALEN) != 0) {
		return (NULL);
	}
	return (radio->sta);
}

/*
 * What ath12k asks in the peer's association (mac.c): only of the access point stage 6 has chosen, with the elements it
 * heard, or the firmware will not expect the handshake. An open network stays without any: there is nothing to expect.
 */
struct cfg80211_bss *
linuxkpi_cfg80211_get_bss(struct wiphy *wiphy, struct linuxkpi_ieee80211_channel *channel, const uint8_t *bssid,
    const uint8_t *ssid, size_t ssid_length, enum ieee80211_bss_type type, enum ieee80211_privacy privacy)
{
	struct fa_radio *radio = the_radio;

	if (radio == NULL || !radio->protect || !radio->target.fixed || bssid == NULL || memcmp(bssid, radio->target.address, ETH_ALEN) != 0) {
		return (NULL);
	}
	radio->bss_elements.data = radio->target.elements;
	radio->bss_elements.len = radio->target.elements_length;
	radio->bss.ies = radio->bss.beacon_ies = &radio->bss_elements;
	radio->bss.signal = radio->target.rssi * 100;
	return (&radio->bss);
}

void
linuxkpi_cfg80211_put_bss(struct wiphy *wiphy, struct cfg80211_bss *bss)
{
}

/* BEHAVIOUR STUB: nothing is queued above the driver, so nothing is pulled. Stage 5 sends management frames only. */
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
 * A message of the handshake is copied for the join to answer, on its own thread: no ciphering and no sending on the poll
 * thread. There is room for one, and the access point sends again what it hears no answer to. Under the layer's lock.
 */
static bool
join_eapol(struct fa_radio *radio, const struct sk_buff *skb, const struct ieee80211_rx_status *status)
{
	const uint8_t *body;
	unsigned length;

	if (!radio->protect || !fa_eapol_read(skb->data, skb->len, (status->flag & RX_FLAG_8023) != 0, (status->flag & RX_FLAG_IV_STRIPPED) != 0,
	    radio->vif->addr, radio->target.address, &body, &length)) {
		return (false);
	}
	radio->eapol_heard++;
	if (radio->eapol_length != 0 || length > sizeof(radio->eapol)) {
		radio->eapol_lost++;
	} else {
		memcpy(radio->eapol, body, length);
		radio->eapol_length = length;
		lx_os_wakeup(&radio->answer);
	}
	return (true);
}

/*
 * Design W3: nothing goes up from here, and what came unciphered once there are keys is counted as what it is and never
 * kept for stage 7. What was deciphered and is wanted is copied for the join, on its own thread. Under the layer's lock.
 */
static void
join_data_count(struct fa_radio *radio, const struct sk_buff *skb, const struct ieee80211_rx_status *status)
{
	struct fa_data data;

	radio->link_data++;
	if ((status->flag & (RX_FLAG_8023 | RX_FLAG_DECRYPTED)) == 0) {
		radio->rx_clear++;
		return;
	}
	radio->rx_ok++;
	if (!radio->net_on || !fa_data_read(skb->data, skb->len, (status->flag & RX_FLAG_8023) != 0, (status->flag & RX_FLAG_IV_STRIPPED) != 0,
	    radio->vif->addr, radio->target.address, &data) || !(fa_net_wanted(data.type, data.body, data.length) ||
	    (radio->serving && fa_net_served(data.type, data.body, data.length, radio->own_address)))) {
		return;
	}
	if (radio->inbox_count == FA_INBOX) {
		radio->inbox_lost++;
		return;
	}
	{
		unsigned slot = (radio->inbox_first + radio->inbox_count++) % FA_INBOX;

		radio->inbox[slot].type = data.type;
		radio->inbox[slot].length = data.length;
		memcpy(radio->inbox[slot].from, data.from, 6);
		memcpy(radio->inbox[slot].bytes, data.body, data.length);
	}
	lx_os_wakeup(&radio->answer);
}

/*
 * Stage 8, once frames pass: what iOS gets, in Ethernet form in the frame's own bytes, for the caller to hand over outside
 * the lock. Design W3: on a protected link nothing that was not deciphered. Design W4: a frame to everybody is checked by its
 * packet number, which the hardware does not do (dp_rx.c), and needs that number to be there. Under the layer's lock.
 */
static void
ios_data(struct fa_radio *radio, struct sk_buff *skb, const struct ieee80211_rx_status *status, uint8_t **frame, unsigned *length)
{
	struct fa_data data;
	uint8_t *ethernet;

	radio->link_data++;
	if (radio->protect && (status->flag & (RX_FLAG_8023 | RX_FLAG_DECRYPTED)) == 0) {
		radio->rx_clear++;
		return;
	}
	if (!fa_data_read(skb->data, skb->len, (status->flag & RX_FLAG_8023) != 0, (status->flag & RX_FLAG_IV_STRIPPED) != 0, radio->vif->addr,
	    radio->target.address, &data) || data.type < 0x0600) {
		radio->link_others++;
		return;
	}
	if (radio->protect && (data.to[0] & 1) != 0 && (status->flag & RX_FLAG_8023) == 0) {
		enum fa_group_answer answer = data.cipher != NULL ? fa_group_check(&radio->group_pn, data.cipher) : FA_GROUP_NO_KEY;

		if (answer != FA_GROUP_FRESH) {
			*(answer == FA_GROUP_REPLAY ? &radio->rx_replay : &radio->rx_no_key) += 1;
			return;
		}
	}
	if (data.length + 14 > FA_NET_MOST + 14) {
		radio->rx_long++;
		return;
	}
	ethernet = (uint8_t *)(uintptr_t)data.body - 14;
	memcpy(ethernet, data.to, 6);
	memcpy(ethernet + 6, data.from, 6);
	fa_put16(ethernet + 12, data.type);
	radio->rx_ok++;
	*frame = ethernet;
	*length = data.length + 14;
}

/* What the join reads of a frame from the air: under the layer's lock, on the poll thread. Stage 8 may leave a frame in *frame for iOS. */
static void
join_rx(struct fa_radio *radio, struct sk_buff *skb, const struct ieee80211_rx_status *status, uint8_t **frame, unsigned *length)
{
	struct fa_reply reply;

	lx_os_lock();
	if (join_eapol(radio, skb, status)) {
		if (radio->ios_joined) {
			radio->ios_pending |= IOS_EAPOL;
			lx_os_wakeup(&radio->ios_pending);
		}
		lx_os_unlock();
		return;
	}
	if ((status->flag & RX_FLAG_8023) != 0) {
		if (radio->ios_joined) {
			ios_data(radio, skb, status, frame, length);
		} else {
			join_data_count(radio, skb, status);
		}
		lx_os_unlock();
		return;
	}
	switch (fa_rx_kind(skb->data, skb->len, radio->target.address)) {
	case FA_RX_DATA:
		if (radio->ios_joined) {
			ios_data(radio, skb, status, frame, length);
		} else {
			join_data_count(radio, skb, status);
		}
		break;
	case FA_RX_BEACON:
		radio->link_beacons++;
		radio->beacon_seq = fa_frame_seq(skb->data, skb->len);
		if ((status->flag & RX_FLAG_NO_SIGNAL_VAL) == 0) {
			radio->rssi = status->signal;
		}
		if (fa_target_take(&radio->target, skb->data, skb->len, status->freq, status->signal, target_name, target_name_length) ==
		    FA_TARGET_PROTECTED) {
			radio->went_protected = true;
			lx_os_wakeup(&radio->answer);
			if (radio->ios_joined) {
				radio->ios_pending |= IOS_LOSS;
				lx_os_wakeup(&radio->ios_pending);
			}
		}
		break;
	case FA_RX_MANAGEMENT:
		if (radio->ios && skb->data[0] == FA_KIND_ACTION) {
			struct fa_ba_ask ask;
			int category = fa_action_category(skb->data, skb->len);

			if (skb->len >= FA_FRAME_HEADER && memcmp(skb->data + 4, radio->vif->addr, ETH_ALEN) == 0 && memcmp(skb->data + 10, radio->target.address, ETH_ALEN) == 0) {
				if (category < 0) {
					radio->action_ciphered++;
				} else if (radio->action[category] != 0xffff) {
					radio->action[category]++;
				}
			}
			if (radio->sta != NULL && fa_ba_read(skb->data, skb->len, radio->vif->addr, radio->target.address, &ask)) {
				radio->ba_asked[ask.tid] = ask;
				radio->ba_want |= (unsigned char)(1u << ask.tid);
				radio->ios_pending |= IOS_BA;
				lx_os_wakeup(&radio->ios_pending);
			} else {
				radio->link_others++;
			}
			break;
		}
		fa_reply_read(&reply, skb->data, skb->len, radio->vif->addr, radio->target.address);
		if (reply.answer == FA_ANSWER_NONE) {
			struct fa_door other;

			radio->link_others++;
			radio->door_other += radio->ios && fa_door_read(skb->data, skb->len, &other);
		} else if (reply.answer == FA_ANSWER_GONE) {
			radio->answer = FA_ANSWER_GONE;
			radio->reason = reply.code;
			if (radio->ios && fa_door_read(skb->data, skb->len, &radio->door)) {
				radio->door_seen = true;
				radio->door_rssi = (status->flag & RX_FLAG_NO_SIGNAL_VAL) == 0 ? status->signal : FA_RSSI_UNKNOWN;
				radio->door_beacon_rssi = radio->rssi;
				radio->door_beacon_seq = radio->beacon_seq;
				radio->door_at = lx_os_nanoseconds();
			}
			lx_os_wakeup(&radio->answer);
			if (radio->ios_joined) {
				radio->ios_pending |= IOS_LOSS;
				lx_os_wakeup(&radio->ios_pending);
			}
		} else if (reply.kind == radio->answer_kind && radio->answer != FA_ANSWER_GONE) {
			/* What is not being waited for moves nothing: an answer to an earlier step is not the one asked for. */
			radio->answer = reply.answer;
			radio->answer_code = reply.code;
			if (reply.answer == FA_ANSWER_ASSOC) {
				radio->aid = reply.aid;
				radio->answer_length = fa_elements_cut(reply.elements, reply.elements_length, sizeof(radio->answer_elements));
				memcpy(radio->answer_elements, reply.elements, radio->answer_length);
			}
			lx_os_wakeup(&radio->answer);
		}
		break;
	default:
		radio->link_others++;
		break;
	}
	lx_os_unlock();
}

/*
 * A frame the radio received, with the driver's description behind it. During a join its frames go to join_rx().
 * BEHAVIOUR STUB otherwise: only beacons and probe responses during the scan are looked at, and every other frame
 * is dropped: counted, and said by name when no scan has run. No frame goes further up than this layer.
 */
void
linuxkpi_ieee80211_rx(struct ieee80211_hw *hw, struct sk_buff *skb, struct ieee80211_sta *sta, struct napi_struct *napi,
    struct list_head *list)
{
	struct fa_radio *radio = radio_of(hw);
	const struct ieee80211_rx_status *status = IEEE80211_SKB_RXCB(skb);

	if (radio->joining) {
		uint8_t *frame = NULL;
		unsigned length = 0;

		join_rx(radio, skb, status, &frame, &length);
		if (frame != NULL) {
			ios_input.rx(frame, length);
		}
		dev_kfree_skb_any(skb);
		return;
	}
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
		int signal = (status->flag & RX_FLAG_NO_SIGNAL_VAL) != 0 ? FA_RSSI_NONE : status->signal;

		(void)fa_bss_take(&radio->heard, skb->data, skb->len, status->freq, signal);
		if (radio->ios_table != NULL) {
			(void)fa_heard_take(radio->ios_table, &radio->ios_table_count, skb->data, skb->len, status->freq, signal, lx_os_nanoseconds(),
			    radio->ios_scan_name, radio->ios_scan_name_length);
		}
		if (!radio->say_names && target_name_length != 0) {
			(void)fa_target_take(&radio->target, skb->data, skb->len, status->freq, signal, target_name, target_name_length);
		}
	}
	lx_os_unlock();
	dev_kfree_skb_any(skb);
}

/*
 * BEHAVIOUR STUB: mac80211 would probe the access point; here the firmware's word is counted, and ath12k's own timer
 * turns it into connection loss if no beacon follows (mac.c, ath12k_mac_handle_beacon_miss()).
 */
void
linuxkpi_ieee80211_beacon_loss(struct ieee80211_vif *vif)
{
	struct fa_radio *radio = the_radio;

	if (radio != NULL && radio->vif == vif) {
		lx_os_lock();
		radio->beacon_losses++;
		lx_os_unlock();
	}
	lx_tell("FUXI ATH12K: M5 beacon loss from the firmware\n");
}

/* The link is gone by the driver's account: the stay ends, and the way down goes on without a deauthentication. */
void
linuxkpi_ieee80211_connection_loss(struct ieee80211_vif *vif)
{
	struct fa_radio *radio = the_radio;

	if (radio != NULL && radio->vif == vif) {
		lx_os_lock();
		radio->lost = true;
		lx_os_wakeup(&radio->answer);
		if (radio->ios_joined) {
			radio->ios_pending |= IOS_LOSS;
			lx_os_wakeup(&radio->ios_pending);
		}
		lx_os_unlock();
	}
	lx_tell("FUXI ATH12K: M5 connection loss from the driver\n");
}

/* ---- stage 4: one interface, one scan, and down again ------------------------------------------------ */

/* The radio started and an interface in station mode on it, as mac80211 brings one up. 0, or -errno. */
static int
station_up(struct fa_radio *radio)
{
	struct ieee80211_hw *hw = &radio->hw;
	struct ieee80211_vif *vif;
	int error;

	the_radio = radio;
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
	unsigned said = radio->say_names ? fa_bss_strongest(heard, order, FA_BSS_SAID_MOST) : 0;

	for (unsigned i = 0; i < said; i++) {
		const struct fa_bss *network = &heard->entry[order[i]];
		char name[FA_SSID_ESCAPED_MOST];

		(void)fa_ssid_escape(name, sizeof(name), network->ssid, network->ssid_length);
		lx_tell("FUXI ATH12K: M4 bss %u %d \"%s\"\n", network->frequency, network->rssi, name);
	}
	if (radio->say_names && heard->count > said) {
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
 * any network with an SSID of no length (stage 5: the one network, so that a hidden one answers too); passive gives
 * ath12k no SSID, which it passes on as WMI_SCAN_FLAG_PASSIVE, and nothing is sent at all. 0 when the driver has said
 * the scan is over. By then ath12k has deleted the vdev it scanned on.
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
			bool asked = radio->scan_only == NULL;

			for (unsigned k = 0; !asked && k < radio->scan_only_count; k++) {
				asked = radio->scan_only[k] == has->channels[i].center_freq;
			}
			if ((has->channels[i].flags & IEEE80211_CHAN_DISABLED) == 0 && asked) {
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
		/* Stage 8: a scan the driver will not start now is no dead firmware: fa_m4_trouble() has its own ways of knowing that. */
		if (!radio->ios) {
			fa_m4_evidence("hw_scan failed");
		}
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
		if (!radio->ios) {
			fa_m4_evidence(trouble ? "scan given up" : state == SCAN_ABORTED ? "scan aborted by ath12k's timeout" : "scan timeout");
		}
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

	radio->say_names = true;
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

/* ---- stage 5: one open network, and away again ------------------------------------------------------------- */

/* A frame for the driver to send: `length` bytes at the returned place, in a buffer the radio can read. */
static struct sk_buff *
frame_new(struct fa_radio *radio, unsigned length, uint8_t **bytes)
{
	struct sk_buff *skb = dev_alloc_skb(length + 256);
	struct ieee80211_tx_info *info;

	if (skb == NULL) {
		return (NULL);
	}
	skb_reserve(skb, 128);			/* room in front for what the driver puts there */
	*bytes = skb_put(skb, length);
	info = IEEE80211_SKB_CB(skb);
	memset(info, 0, sizeof(*info));
	info->control.vif = radio->vif;
	info->band = radio->chanctx != NULL ? radio->chanctx->def.chan->band : NL80211_BAND_2GHZ;
	return (skb);
}

static void
frame_send(struct fa_radio *radio, struct sk_buff *skb)
{
	struct ieee80211_tx_control control = { .sta = radio->sta };

	lx_os_lock();
	radio->tx_sent++;
	lx_os_unlock();
	radio->ops->tx(&radio->hw, &control, skb);
}

/* One step of the station's state as the driver is told of it (mac80211: drv_sta_state()). */
static int
station_state(struct fa_radio *radio, enum ieee80211_sta_state from, enum ieee80211_sta_state to)
{
	int error;

	wiphy_lock(radio->wiphy);
	error = radio->ops->sta_state(&radio->hw, radio->vif, radio->sta, from, to);
	wiphy_unlock(radio->wiphy);
	if (error == 0) {
		radio->reached = to;
	}
	return (error);
}

static void
link_changed(struct fa_radio *radio, u64 what)
{
	wiphy_lock(radio->wiphy);
	radio->ops->link_info_changed(&radio->hw, radio->vif, &radio->vif->bss_conf, what);
	wiphy_unlock(radio->wiphy);
}

static void
vif_changed(struct fa_radio *radio, u64 what)
{
	wiphy_lock(radio->wiphy);
	radio->ops->vif_cfg_changed(&radio->hw, radio->vif, what);
	wiphy_unlock(radio->wiphy);
}

/* The band's HE abilities for a station, or NULL. */
static const struct ieee80211_sta_he_cap *
station_he(const struct ieee80211_supported_band *band)
{
	const struct ieee80211_sta_he_cap *he = ieee80211_get_he_iftype_cap(band, NL80211_IFTYPE_STATION);

	return (he != NULL && he->has_he ? he : NULL);
}

/*
 * What the access point says it can do, into the form the driver reads when it tells the firmware of the peer: rates,
 * HT, VHT, HE as far as both sides have them and radio->width lets them, and WMM. Its width is the channel's, as far as it says it
 * can. `elements` are the association response's, or the beacon's before that. EHT stays out (design section 9).
 */
static void
station_abilities(struct fa_radio *radio, const uint8_t *elements, unsigned length)
{
	struct ieee80211_supported_band *band = radio->wiphy->bands[radio->chanctx->def.chan->band];
	struct ieee80211_link_sta *link = &radio->sta->deflink;
	struct ieee80211_bss_conf *conf = &radio->vif->bss_conf;
	const uint8_t *ht = fa_element(elements, length, FA_EID_HT);
	const uint8_t *vht = fa_element(elements, length, FA_EID_VHT);
	const uint8_t *he_element = fa_ext_element(elements, length, FA_EXT_HE);
	const uint8_t *e;
	const struct ieee80211_sta_he_cap *own_he = station_he(band);
	uint8_t ht_ours[2 + FA_HT_BODY], vht_ours[2 + FA_VHT_BODY], own_map[10];
	struct fa_he he;
	bool has_he = false;
	uint16_t bitrates[32];
	uint32_t basic = 0, rates;
	unsigned count = 0;

	for (int i = 0; i < band->n_bitrates && count < nitems(bitrates); i++) {
		bitrates[count++] = (uint16_t)band->bitrates[i].bitrate;
	}
	rates = fa_rates_mask(bitrates, count, elements, length, &basic);
	/* A response that says nothing of rates leaves the beacon's standing. */
	if (rates != 0) {
		link->supp_rates[band->band] = rates;
		conf->basic_rates = basic;
	}
	link->smps_mode = IEEE80211_SMPS_OFF;
	radio->sta->wme = fa_wmm_element(elements, length) != NULL;
	conf->qos = radio->sta->wme;
	memset(&link->ht_cap, 0, sizeof(link->ht_cap));
	memset(&link->vht_cap, 0, sizeof(link->vht_cap));
	memset(&link->he_cap, 0, sizeof(link->he_cap));
	if (ht != NULL && ht[1] >= FA_HT_BODY && band->ht_cap.ht_supported) {
		memcpy(ht_ours, ht, sizeof(ht_ours));
		ht = ht_ours;
		memcpy(own_map, &band->ht_cap.mcs, 10);		/* its rx_mask comes first */
		fa_ht_limit(ht_ours, band->ht_cap.cap, own_map);
		link->ht_cap.ht_supported = true;
		link->ht_cap.cap = (uint16_t)((ht[2] | ht[3] << 8) & (radio->width.mhz > 20 ? 0xffffu : ~(unsigned)(FA_HT_CAP_40 | FA_HT_CAP_SGI_40)));
		link->ht_cap.ampdu_factor = ht[4] & 3;
		link->ht_cap.ampdu_density = (ht[4] >> 2) & 7;
		memcpy(&link->ht_cap.mcs, ht + 5, sizeof(link->ht_cap.mcs));
	} else {
		ht = NULL;
	}
	if (vht != NULL && vht[1] >= FA_VHT_BODY && band->vht_cap.vht_supported && radio->width.vht) {
		memcpy(vht_ours, vht, sizeof(vht_ours));
		vht = vht_ours;
		memcpy(own_map, &band->vht_cap.vht_mcs, 8);	/* rx map, rx highest, tx map, tx highest */
		fa_vht_limit(vht_ours, band->vht_cap.cap & ~(FA_VHT_CAP_WIDTH | FA_VHT_CAP_SGI_160), (unsigned)own_map[0] | (unsigned)own_map[1] << 8, (unsigned)own_map[4] | (unsigned)own_map[5] << 8);
		link->vht_cap.vht_supported = true;
		link->vht_cap.cap = (uint32_t)vht[2] | (uint32_t)vht[3] << 8 | (uint32_t)vht[4] << 16 | (uint32_t)vht[5] << 24;
		memcpy(&link->vht_cap.vht_mcs, vht + 6, sizeof(link->vht_cap.vht_mcs));
	} else {
		vht = NULL;
	}
	switch (fa_width_peer(&radio->width, ht != NULL && (ht[2] & FA_HT_CAP_40) != 0, vht != NULL)) {
	case 80:
		link->bandwidth = IEEE80211_STA_RX_BW_80;
		break;
	case 40:
		link->bandwidth = IEEE80211_STA_RX_BW_40;
		break;
	default:
		link->bandwidth = IEEE80211_STA_RX_BW_20;
		break;
	}
	if (he_element != NULL && own_he != NULL && radio->width.he && fa_he_read(he_element, &he)) {
		has_he = true;
		link->he_cap.has_he = true;
		memcpy(link->he_cap.he_cap_elem.mac_cap_info, he.mac, FA_HE_MAC);
		memcpy(link->he_cap.he_cap_elem.phy_cap_info, he.phy, FA_HE_PHY);
		link->he_cap.he_mcs_nss_supp.rx_mcs_80 = he.rx_mcs_80;
		link->he_cap.he_mcs_nss_supp.tx_mcs_80 = he.tx_mcs_80;
		link->he_cap.he_mcs_nss_supp.rx_mcs_160 = he.rx_mcs_160;
		link->he_cap.he_mcs_nss_supp.tx_mcs_160 = he.tx_mcs_160;
		link->he_cap.he_mcs_nss_supp.rx_mcs_80p80 = he.rx_mcs_80_80;
		link->he_cap.he_mcs_nss_supp.tx_mcs_80p80 = he.tx_mcs_80_80;
		memcpy(link->he_cap.ppe_thres, he.ppe, sizeof(he.ppe));
	}
	conf->he_support = has_he;
	if ((e = fa_ext_element(elements, length, FA_EXT_HE_OPERATION)) != NULL && has_he && e[1] >= 5) {
		/* Three bytes of parameters and the BSS colour's byte, the word ath12k hands the firmware. */
		conf->he_oper.params = (uint32_t)e[3] | (uint32_t)e[4] << 8 | (uint32_t)e[5] << 16 | (uint32_t)e[6] << 24;
	}
	link->rx_nss = (uint8_t)fa_peer_streams(ht, vht, has_he ? &he : NULL);
	if ((e = fa_element(elements, length, FA_EID_HT_OPERATION)) != NULL && e[1] >= 22) {
		conf->ht_operation_mode = (uint16_t)(e[4] | e[5] << 8);
	}
	if ((e = fa_element(elements, length, FA_EID_TIM)) != NULL && e[1] >= 2 && e[3] != 0) {
		conf->dtim_period = e[3];
	}
}

/* Our association request, as far as both sides have what it offers, at the width of radio->width. Its length, 0 when it does not fit. */
static unsigned
request_build(struct fa_radio *radio, uint8_t *frame, unsigned size)
{
	const struct fa_target *target = &radio->target;
	struct linuxkpi_ieee80211_channel *channel = radio->chanctx->def.chan;
	struct ieee80211_supported_band *band = radio->wiphy->bands[channel->band];
	const struct ieee80211_sta_he_cap *own_he = station_he(band);
	struct fa_offer offer;

	memset(&offer, 0, sizeof(offer));
	offer.capability = (uint16_t)(FA_CAP_ESS | (target->capability & (FA_CAP_SHORT_PREAMBLE | FA_CAP_SHORT_SLOT | FA_CAP_SPECTRUM)));
	if (radio->protect) {
		offer.capability |= FA_CAP_PRIVACY;
		offer.rsn = FA_RSN_OURS;
		offer.rsn_length = sizeof(FA_RSN_OURS);
	}
	offer.ssid = target_name;
	offer.ssid_length = target_name_length;
	for (int i = 0; i < band->n_bitrates && offer.rate_count < FA_RATES_MOST; i++) {
		offer.rates[offer.rate_count++] = (uint8_t)(band->bitrates[i].bitrate / 5 |
		    ((radio->vif->bss_conf.basic_rates & (1u << i)) != 0 ? 0x80 : 0));
	}
	if ((target->capability & FA_CAP_SPECTRUM) != 0) {
		offer.spectrum = true;
		offer.power_most = (uint8_t)channel->max_power;
		offer.channel = (uint8_t)linuxkpi_ieee80211_frequency_to_channel(channel->center_freq, 0);
	}
	if (fa_element(target->elements, target->elements_length, FA_EID_HT) != NULL && band->ht_cap.ht_supported) {
		unsigned cap = band->ht_cap.cap & (radio->width.mhz > 20 ? 0xffffu : ~(unsigned)(FA_HT_CAP_40 | FA_HT_CAP_SGI_40));

		offer.has_ht = true;
		offer.ht[0] = (uint8_t)cap;
		offer.ht[1] = (uint8_t)(cap >> 8);
		offer.ht[2] = (uint8_t)(band->ht_cap.ampdu_factor | band->ht_cap.ampdu_density << 2);
		memcpy(offer.ht + 3, &band->ht_cap.mcs, sizeof(band->ht_cap.mcs));
	}
	if (fa_element(target->elements, target->elements_length, FA_EID_VHT) != NULL && band->vht_cap.vht_supported && radio->width.vht) {
		uint32_t cap = band->vht_cap.cap & ~(FA_VHT_CAP_WIDTH | FA_VHT_CAP_SGI_160);

		offer.has_vht = true;
		offer.vht[0] = (uint8_t)cap;
		offer.vht[1] = (uint8_t)(cap >> 8);
		offer.vht[2] = (uint8_t)(cap >> 16);
		offer.vht[3] = (uint8_t)(cap >> 24);
		memcpy(offer.vht + 4, &band->vht_cap.vht_mcs, sizeof(band->vht_cap.vht_mcs));
	}
	if (fa_ext_element(target->elements, target->elements_length, FA_EXT_HE) != NULL && own_he != NULL && radio->width.he) {
		offer.he_length = fa_he_offer(offer.he, sizeof(offer.he), own_he->he_cap_elem.mac_cap_info, own_he->he_cap_elem.phy_cap_info,
		    own_he->he_mcs_nss_supp.rx_mcs_80, own_he->he_mcs_nss_supp.tx_mcs_80);
	}
	offer.wmm = fa_wmm_element(target->elements, target->elements_length) != NULL;
	return (fa_assoc_request(frame, size, radio->vif->addr, target->address, &offer));
}

/*
 * One exchange with the access point: `kind` is the answer that is being waited for, and the frame goes out up to three
 * times, half a second each. Its answer, or FA_ANSWER_NONE when the time is up or the firmware is taken as dead.
 */
static enum fa_answer
exchange(struct fa_radio *radio, uint8_t kind, unsigned (*build)(struct fa_radio *, uint8_t *, unsigned), const char *what)
{
	enum fa_answer answer = FA_ANSWER_NONE;
	unsigned tries = 0;

	lx_os_lock();
	radio->tx_sent_before = radio->tx_sent;
	radio->tx_done_before = radio->tx_done;
	lx_os_unlock();
	while (tries < FA_JOIN_TRIES && answer == FA_ANSWER_NONE && !fa_m4_trouble()) {
		unsigned long long began = lx_os_nanoseconds();
		struct sk_buff *skb;
		uint8_t *bytes;
		unsigned length;

		lx_os_lock();
		radio->answer_kind = kind;
		if (radio->answer != FA_ANSWER_GONE) {
			radio->answer = FA_ANSWER_NONE;
		}
		lx_os_unlock();
		if ((skb = frame_new(radio, FA_FRAME_MOST, &bytes)) == NULL) {
			lx_tell("FUXI ATH12K: M5 no buffer for the %s frame\n", what);
			break;
		}
		length = build(radio, bytes, FA_FRAME_MOST);
		if (length == 0) {
			lx_tell("FUXI ATH12K: M5 the %s frame does not fit\n", what);
			dev_kfree_skb_any(skb);
			break;
		}
		skb_trim(skb, length);
		tries++;
		frame_send(radio, skb);
		for (;;) {
			bool over;

			lx_os_lock();
			answer = radio->answer;
			over = answer != FA_ANSWER_NONE || lx_os_nanoseconds() - began >= FA_JOIN_ANSWER_NS;
			if (!over) {
				lx_os_sleep(&radio->answer, FA_JOIN_LOOK_NS);
			}
			lx_os_unlock();
			if (over || fa_m4_trouble()) {
				break;
			}
		}
	}
	lx_tell("FUXI ATH12K: M5 %s: answer %d code %u after %u tries; frames sent %u done %u acked %u dropped %u\n", what, answer, radio->answer_code,
	    tries, radio->tx_sent, radio->tx_done, radio->tx_acked, radio->tx_dropped);
	return (answer);
}

static unsigned
build_auth(struct fa_radio *radio, uint8_t *frame, unsigned size)
{
	return (fa_auth_request(frame, size, radio->vif->addr, radio->target.address));
}

/* A key for the driver to give the firmware: CCMP, for frames between us and the access point or for the group's. 0, or what it said. */
static int
key_give(struct fa_radio *radio, const uint8_t *key, unsigned index, bool pairwise, unsigned slot, struct ieee80211_key_conf **given)
{
	struct ieee80211_key_conf *conf = kzalloc(sizeof(*conf) + 16, GFP_KERNEL);
	int error;

	*given = NULL;
	if (conf == NULL) {
		return (-ENOMEM);
	}
	kfree(radio->key_memory[slot]);
	radio->key_memory[slot] = conf;
	conf->cipher = WLAN_CIPHER_SUITE_CCMP;
	conf->keyidx = (uint8_t)index;
	conf->keylen = 16;
	conf->iv_len = 8;
	conf->icv_len = 8;
	conf->flags = pairwise ? IEEE80211_KEY_FLAG_PAIRWISE : 0;
	conf->link_id = -1;
	memcpy(conf->key, key, 16);
	wiphy_lock(radio->wiphy);
	error = radio->ops->set_key(&radio->hw, SET_KEY, radio->vif, pairwise ? radio->sta : NULL, conf);
	wiphy_unlock(radio->wiphy);
	if (error != 0) {
		fa_wipe(conf->key, 16);
		return (error);
	}
	*given = conf;
	return (0);
}

/* What is live goes before the peer does, without a DELBA: the deauthentication has said it all (mac80211, sta_info.c). */
static void
ba_stop_all(struct fa_radio *radio)
{
	if (radio->ba_live == 0) {
		return;
	}
	wiphy_lock(radio->wiphy);
	for (unsigned tid = 0; tid < FA_BA_TIDS; tid++) {
		if ((radio->ba_live & (1u << tid)) != 0) {
			struct ieee80211_ampdu_params params = { .sta = radio->sta, .action = IEEE80211_AMPDU_RX_STOP, .tid = (uint8_t)tid };

			(void)radio->ops->ampdu_action(&radio->hw, radio->vif, &params);
			radio->ba_stopped++;
		}
	}
	wiphy_unlock(radio->wiphy);
	radio->ba_live = 0;
}

static void
key_take_back(struct fa_radio *radio, struct ieee80211_key_conf **given, bool pairwise)
{
	if (*given != NULL) {
		int error;

		wiphy_lock(radio->wiphy);
		error = radio->ops->set_key(&radio->hw, DISABLE_KEY, radio->vif, pairwise ? radio->sta : NULL, *given);
		wiphy_unlock(radio->wiphy);
		lx_tell("FUXI ATH12K: M6 %s key taken back %d\n", pairwise ? "pairwise" : "group", error);
		fa_wipe((*given)->key, 16);
		*given = NULL;
	}
}

/* Our answer to a message of the handshake: 802.11 data, which the driver takes to the firmware unciphered until there are keys, and ciphered once there are. */
static bool
eapol_send(struct fa_radio *radio, const uint8_t *body, unsigned length, bool ciphered)
{
	bool qos = radio->sta->wme;
	struct sk_buff *skb;
	uint8_t *bytes;
	unsigned total = (qos ? FA_QOS_HEADER : FA_FRAME_HEADER) + FA_LLC_LENGTH + length;

	if ((skb = frame_new(radio, total, &bytes)) == NULL) {
		lx_tell("FUXI ATH12K: M6 no buffer for our message\n");
		return (false);
	}
	(void)fa_eapol_frame(bytes, total, radio->vif->addr, radio->target.address, qos, body, length);
	if (ciphered) {
		bytes[1] |= 0x40;
		IEEE80211_SKB_CB(skb)->control.hw_key = radio->pairwise_key;
	}
	skb->protocol = cpu_to_be16(0x888e);
	skb->priority = FA_TID_CONTROL;
	radio->eapol_sent++;
	frame_send(radio, skb);
	return (true);
}

/*
 * The four-way handshake: the access point begins it, its messages come through join_eapol(), and each is answered here.
 * A message that is not good does not end the wait: anyone in range can send one, and the genuine one can still come.
 * True when message 3 was good and message 4 went. Bounded by the clock alone. The result is said, and never a key.
 */
static bool
station_handshake(struct fa_radio *radio)
{
	unsigned long long began = lx_os_nanoseconds();
	uint8_t reply[FA_WPA_REPLY_MOST];
	static uint8_t message[FA_EAPOL_MOST];
	const char *last_why = "";
	int last = FA_WPA_NOTHING;
	bool keys = false;

	while (!keys && lx_os_nanoseconds() - began < FA_HANDSHAKE_NS && !fa_m4_trouble()) {
		unsigned length, reply_length = 0;
		const char *why = "";
		bool gone;
		int said;

		lx_os_lock();
		length = radio->eapol_length;
		gone = radio->answer == FA_ANSWER_GONE;
		if (length != 0) {
			memcpy(message, radio->eapol, length);
			radio->eapol_length = 0;
		} else if (!gone) {
			lx_os_sleep(&radio->answer, FA_HANDSHAKE_LOOK_NS);
		}
		lx_os_unlock();
		if (gone) {
			break;
		}
		if (length == 0) {
			continue;
		}
		said = fa_wpa_message(&radio->wpa, message, length, reply, &reply_length, &why);
		lx_tell("FUXI ATH12K: M6 handshake: %u bytes from the access point after %llu ms: %s%s\n", length,
		    (lx_os_nanoseconds() - began) / 1000000ull, said == FA_WPA_ANSWER ? "message 1, answered" : said == FA_WPA_KEYS ? "message 3, good, answered" :
		    said == FA_WPA_GROUP ? "a new group key, left unanswered at this stage" : said == FA_WPA_BAD ? "not good: " : "not one to answer", why);
		if (said == FA_WPA_ANSWER || said == FA_WPA_KEYS) {
			if (!eapol_send(radio, reply, reply_length, false)) {
				break;
			}
		}
		if (said != FA_WPA_NOTHING) {
			last = said;
			last_why = why;
		}
		keys = said == FA_WPA_KEYS;
	}
	fa_wipe(message, sizeof(message));
	fa_wipe(reply, sizeof(reply));
	if (!keys) {
		lx_tell("FUXI ATH12K: M6 not proven: the handshake did not come through in %llu ms: %s%s; messages heard %u lost %u; data sent %u done %u acked %u\n",
		    (lx_os_nanoseconds() - began) / 1000000ull, radio->answer == FA_ANSWER_GONE ? "the access point has shown us the door (a wrong passphrase gives this)" :
		    last == FA_WPA_BAD ? "a message was not good: " : last == FA_WPA_ANSWER ? "our message 2 was not answered (a wrong passphrase gives this)" :
		    "the access point did not begin it", last == FA_WPA_BAD ? last_why : "", radio->eapol_heard, radio->eapol_lost, radio->eapol_sent,
		    radio->data_done, radio->data_acked);
	}
	return (keys);
}

/* After message 3: message 4 given time to leave unciphered, the two keys to the driver. 0, or what it said. */
static int
station_keys(struct fa_radio *radio)
{
	unsigned long long began = lx_os_nanoseconds();
	int error;

	while (radio->data_done < radio->eapol_sent && lx_os_nanoseconds() - began < FA_LAST_MESSAGE_NS && !fa_m4_trouble()) {
		lx_os_lock();
		lx_os_sleep(&radio->answer, FA_HANDSHAKE_LOOK_NS);
		lx_os_unlock();
	}
	lx_tell("FUXI ATH12K: M6 message 4 after %llu ms: data done %u acked %u\n", (lx_os_nanoseconds() - began) / 1000000ull, radio->data_done, radio->data_acked);
	error = key_give(radio, radio->wpa.pairwise, 0, true, 0, &radio->pairwise_key);
	if (error == 0) {
		error = key_give(radio, radio->wpa.group, radio->wpa.group_index, false, 1, &radio->group_key);
		radio->group_slot = 1;
	}
	lx_tell("FUXI ATH12K: M6 keys to the driver: %d, group number %u\n", error, radio->wpa.group_index);
	return (error);
}

/*
 * Stage 8: on a channel with radar rules, what will not start there is that channel's answer and not the firmware's death, and another
 * access point may still be tried. True, and said, when that is so. What really is the firmware's shows in fa_m4_trouble() by its own ways.
 */
static bool
join_radar_refused(struct fa_radio *radio, const char *what, int error)
{
	const struct linuxkpi_ieee80211_channel *channel = radio->chanctx->def.chan;

	if (!radio->ios || (channel->flags & (IEEE80211_CHAN_RADAR | IEEE80211_CHAN_NO_IR)) == 0 || fa_m4_trouble()) {
		return (false);
	}
	lx_tell("FUXI ATH12K: M8 %s not taken on %u MHz, a channel with radar rules: %d. Not the firmware's doing\n", what, channel->center_freq, error);
	return (true);
}

/*
 * No answer is either the air's doing or the firmware's. When frames went to the firmware and none of them came back, it is
 * the firmware's: that goes the way of any other death. Otherwise it is only a join that did not happen.
 */
static int
no_answer(struct fa_radio *radio, const char *what)
{
	lx_tell("FUXI ATH12K: M5 not proven: no answer to %s\n", what);
	if (radio->tx_sent != radio->tx_sent_before && radio->tx_done == radio->tx_done_before && !fa_m4_trouble() && !join_radar_refused(radio, "the frames", -ETIMEDOUT)) {
		fa_m4_evidence("management frames never came back from the firmware");
		return (-EIO);
	}
	return (0);
}

/*
 * Everything a join has made, undone in mac80211's order, as far as it got. The deauthentication goes first and is waited
 * for, unless the access point has shown us the door or the firmware is taken as dead. Once it is, each command here
 * fails at once (ATH12K_FLAG_CRASH_FLUSH), so this touches only the host.
 */
static void
station_leave(struct fa_radio *radio)
{
	struct ieee80211_hw *hw = &radio->hw;
	struct ieee80211_vif *vif = radio->vif;
	enum ieee80211_sta_state state = radio->reached;

	if (radio->sta != NULL && state >= IEEE80211_STA_AUTH && radio->answer != FA_ANSWER_GONE && !radio->lost && !fa_m4_trouble()) {
		struct sk_buff *skb;
		uint8_t *bytes;

		if ((skb = frame_new(radio, FA_DEAUTH_LENGTH, &bytes)) != NULL) {
			(void)fa_deauth(bytes, FA_DEAUTH_LENGTH, vif->addr, radio->target.address, FA_REASON_LEAVING);
			frame_send(radio, skb);
			wiphy_lock(hw->wiphy);
			radio->ops->flush(hw, vif, ~0u, false);
			wiphy_unlock(hw->wiphy);
			lx_tell("FUXI ATH12K: M5 deauthentication sent; frames sent %u done %u acked %u dropped %u\n", radio->tx_sent, radio->tx_done,
			    radio->tx_acked, radio->tx_dropped);
		}
	}
	if (radio->sta != NULL) {
		ba_stop_all(radio);
		key_take_back(radio, &radio->group_key, false);
		key_take_back(radio, &radio->pairwise_key, true);
	}
	for (; radio->sta != NULL && state > IEEE80211_STA_NOTEXIST; state--) {
		int error = station_state(radio, state, state - 1);

		if (error != 0) {
			lx_tell("FUXI ATH12K: M5 station state %d to %d: %d\n", state, state - 1, error);
		}
	}
	radio->reached = IEEE80211_STA_NOTEXIST;
	radio->sta = NULL;
	if (vif->cfg.assoc) {
		vif->cfg.assoc = false;
		vif->cfg.aid = 0;
		vif_changed(radio, BSS_CHANGED_ASSOC);
	}
	wiphy_lock(hw->wiphy);
	if (vif->bss_conf.chanctx_conf != NULL) {
		radio->ops->unassign_vif_chanctx(hw, vif, &vif->bss_conf, radio->chanctx);
		vif->bss_conf.chanctx_conf = NULL;
	}
	if (radio->chanctx_added) {
		radio->ops->remove_chanctx(hw, radio->chanctx);
		radio->chanctx_added = false;
	}
	wiphy_unlock(hw->wiphy);
	vif->bss_conf.bssid = no_network;
	lx_os_lock();
	radio->joining = false;
	lx_os_unlock();
	kfree(radio->chanctx);
	radio->chanctx = NULL;
	lx_tell("FUXI ATH12K: M5 left\n");
}

/*
 * The channel the access point says it is on: a beacon can be heard on the one next to its own. NULL when it may not be
 * used: out of the domain, or at 6 GHz, which this stage does not build.
 */
static struct linuxkpi_ieee80211_channel *
target_channel(struct fa_radio *radio)
{
	const struct fa_target *target = &radio->target;
	struct linuxkpi_ieee80211_channel *channel = linuxkpi_ieee80211_get_channel(radio->wiphy, target->frequency);
	const uint8_t *ds = fa_element(target->elements, target->elements_length, FA_EID_DS);

	if (channel != NULL && ds != NULL && ds[1] == 1) {
		struct linuxkpi_ieee80211_channel *said = linuxkpi_ieee80211_get_channel(radio->wiphy,
		    linuxkpi_ieee80211_channel_to_frequency(ds[2], channel->band));

		channel = said != NULL ? said : channel;
	}
	return (channel != NULL && (channel->flags & IEEE80211_CHAN_DISABLED) == 0 && channel->band != NL80211_BAND_6GHZ ? channel : NULL);
}

/*
 * Whether the radio may tune to all of this: every 20 MHz of it allowed here, on the primary's band, and under a rule of the domain that
 * lets it be that wide. mac80211 has cfg80211 work this out; the domain here is read in regd_apply() alone.
 */
static bool
width_allowed(struct fa_radio *radio, const struct linuxkpi_ieee80211_channel *primary, unsigned center, unsigned mhz)
{
	const struct linuxkpi_ieee80211_regdomain *regd = radio->regd;

	if (regd == NULL) {
		return (false);
	}
	for (unsigned frequency = center - mhz / 2 + 10; frequency < center + mhz / 2; frequency += 20) {
		const struct linuxkpi_ieee80211_channel *channel = linuxkpi_ieee80211_get_channel(radio->wiphy, frequency);
		bool covered = false;

		if (channel == NULL || channel->band != primary->band || (channel->flags & IEEE80211_CHAN_DISABLED) != 0) {
			return (false);
		}
		for (int r = 0; r < regd->n_reg_rules && !covered; r++) {
			covered = regd->reg_rules[r].freq_range.start_freq_khz <= ((int)frequency - 10) * 1000 &&
			    regd->reg_rules[r].freq_range.end_freq_khz >= ((int)frequency + 10) * 1000 &&
			    regd->reg_rules[r].freq_range.max_bandwidth_khz >= (int)mhz * 1000;
		}
		if (!covered) {
			return (false);
		}
	}
	return (true);
}

/*
 * How wide this join's channel is. Stages 5 to 7 are 20 MHz whatever is said, and so is stage 8 unless fuxiwifiwide=1 asks; but stage 8
 * alone can come to 5 GHz by its own choice, so it reads what the access point says even at 20 MHz: see fa_width_choose().
 */
static void
station_width(struct fa_radio *radio, const struct linuxkpi_ieee80211_channel *channel)
{
	const struct fa_target *target = &radio->target;
	const struct ieee80211_supported_band *band = radio->wiphy->bands[channel->band];
	struct fa_width *width = &radio->width;
	unsigned most = target_most;

	fa_width_narrow(width, channel->center_freq);
	if (!radio->ios) {
		return;
	}
	for (;;) {
		fa_width_choose(width, target->elements, target->elements_length, channel->center_freq, most, band->ht_cap.ht_supported && (band->ht_cap.cap & FA_HT_CAP_40) != 0,
		    band->vht_cap.vht_supported, station_he(band) != NULL);
		if (width->mhz == 20 || width_allowed(radio, channel, width->center, width->mhz)) {
			break;
		}
		lx_tell("FUXI ATH12K: M8 width: %u MHz around %u is not allowed here\n", width->mhz, width->center);
		most = width->mhz == 80 ? 40 : 20;
	}
	lx_tell("FUXI ATH12K: M8 width: %u MHz around %u, primary %u; the access point works at %u; offering VHT %d HE %d%s\n", width->mhz, width->center, channel->center_freq,
	    width->said, width->vht, width->he, target_most > 20 ? "" : "; fuxiwifiwide is off");
}

/* The width as the driver's channel has it. Only station_width() at stage 8 makes it more than 20 MHz. */
static enum nl80211_chan_width
width_of_channel(const struct fa_width *width)
{
	return (width->mhz == 80 ? NL80211_CHAN_WIDTH_80 : width->mhz == 40 ? NL80211_CHAN_WIDTH_40 : NL80211_CHAN_WIDTH_20);
}

/* WMM's parameters, for each of the four classes how long to wait and how long to hold the air. */
static void
wmm_parameters(struct fa_radio *radio)
{
	static const uint8_t classes[4] = { IEEE80211_AC_BE, IEEE80211_AC_BK, IEEE80211_AC_VI, IEEE80211_AC_VO };
	const uint8_t *e = fa_wmm_element(radio->answer_elements, radio->answer_length);

	if (e == NULL || e[1] < 24 || e[6] != 1) {
		return;
	}
	wiphy_lock(radio->wiphy);
	for (unsigned i = 0; i < 4; i++) {
		const uint8_t *record = e + 10 + 4 * i;
		struct ieee80211_tx_queue_params params;

		memset(&params, 0, sizeof(params));
		params.aifs = record[0] & 0x0f;
		params.acm = (record[0] >> 4) & 1;
		params.cw_min = (uint16_t)((1u << (record[1] & 0x0f)) - 1);
		params.cw_max = (uint16_t)((1u << (record[1] >> 4)) - 1);
		params.txop = (uint16_t)(record[2] | record[3] << 8);
		radio->ops->conf_tx(&radio->hw, radio->vif, 0, classes[(record[0] >> 5) & 3], &params);
	}
	wiphy_unlock(radio->wiphy);
}

/* Said for an answer that ends the join. It is the access point's doing, not the firmware's: nothing was proven. */
static void
join_refused(struct fa_radio *radio, enum fa_answer answer, const char *what)
{
	lx_tell("FUXI ATH12K: M5 not proven: %s %s, code %u\n", what, answer == FA_ANSWER_GONE ? "ended by the access point" : "refused",
	    answer == FA_ANSWER_GONE ? radio->reason : radio->answer_code);
}

/*
 * Join the one open network that a scan has heard, in the order mac80211 goes through (ieee80211_prep_connection(), then
 * authentication and association): the channel taken, the link told of the access point, the access point made a station
 * of the driver's, which creates the peer and starts the vdev (vdev_start_delay), then the frames. A network without
 * control-port protection is authorized before the association is told, which is when ath12k brings the vdev up and sets
 * the peer authorized on the firmware (mac.c, ath12k_bss_assoc()). With protection the association is told first, the
 * handshake and the keys follow, and only then authorized. 0 with *joined when it stands; 0 without when the air
 * or the access point stopped it; -errno when the driver or the firmware did.
 */
static int
station_join(struct fa_radio *radio, bool *joined)
{
	struct ieee80211_hw *hw = &radio->hw;
	struct ieee80211_vif *vif = radio->vif;
	struct fa_target *target = &radio->target;
	struct linuxkpi_ieee80211_channel *channel = target_channel(radio);
	enum fa_answer answer;
	int error;

	*joined = false;
	if (channel == NULL) {
		lx_tell("FUXI ATH12K: M5 not proven: the access point's channel may not be used here (heard on %u MHz)\n", target->frequency);
		return (0);
	}
	radio->chanctx = kzalloc(64 + sizeof(*radio->chanctx) + hw->chanctx_data_size, GFP_KERNEL);
	/* Used again by a later join of stage 8: the driver's station is gone by then, and its data path's may still name this. */
	if (radio->sta_memory != NULL) {
		memset(radio->sta_memory, 0, 64 + sizeof(*radio->sta) + hw->sta_data_size);
	} else {
		radio->sta_memory = kzalloc(64 + sizeof(*radio->sta) + hw->sta_data_size, GFP_KERNEL);
	}
	if (radio->chanctx == NULL || radio->sta_memory == NULL) {
		lx_tell("FUXI ATH12K: M5 no memory for the join\n");
		kfree(radio->chanctx);
		kfree(radio->sta_memory);
		radio->chanctx = NULL;
		radio->sta_memory = NULL;
		return (-ENOMEM);
	}
	lx_os_lock();
	target->fixed = true;
	radio->joining = true;
	radio->answer = FA_ANSWER_NONE;
	lx_os_unlock();

	/* the channel, as wide as station_width() said: 20 MHz, on every stage but 8 */
	station_width(radio, channel);
	radio->chanctx->def.chan = channel;
	radio->chanctx->def.width = width_of_channel(&radio->width);
	radio->chanctx->def.center_freq1 = radio->width.center;
	radio->chanctx->min_def = radio->chanctx->def;
	radio->chanctx->rx_chains_static = 2;
	radio->chanctx->rx_chains_dynamic = 2;
	vif->bss_conf.chanreq.oper = radio->chanctx->def;
	wiphy_lock(hw->wiphy);
	error = radio->ops->add_chanctx(hw, radio->chanctx);
	if (error == 0) {
		radio->chanctx_added = true;
		vif->bss_conf.chanctx_conf = radio->chanctx;
		error = radio->ops->assign_vif_chanctx(hw, vif, &vif->bss_conf, radio->chanctx);
		if (error != 0) {
			vif->bss_conf.chanctx_conf = NULL;
		}
	}
	wiphy_unlock(hw->wiphy);
	lx_tell("FUXI ATH12K: M5 channel %u MHz, %u MHz wide, chanctx %d\n", channel->center_freq, radio->width.mhz, error);
	if (error != 0) {
		if (join_radar_refused(radio, "the channel", error)) {
			return (0);
		}
		fa_m4_evidence("channel context refused");
		return (error);
	}

	/* the access point, to the link */
	vif->bss_conf.bssid = target->address;
	vif->bss_conf.ht_operation_mode = 0;	/* a stage 8 join after another: not what the last access point said */
	vif->bss_conf.he_oper.params = 0;
	vif->bss_conf.beacon_int = target->interval;
	vif->bss_conf.assoc_capability = target->capability;
	vif->bss_conf.dtim_period = 1;
	vif->bss_conf.use_short_slot = (target->capability & FA_CAP_SHORT_SLOT) != 0 || channel->band != NL80211_BAND_2GHZ;
	vif->bss_conf.use_short_preamble = (target->capability & FA_CAP_SHORT_PREAMBLE) != 0;
	memcpy(vif->cfg.ap_addr, target->address, ETH_ALEN);
	memcpy(vif->cfg.ssid, target_name, target_name_length);
	vif->cfg.ssid_len = target_name_length;
	hw->conf.listen_interval = FA_LISTEN_INTERVAL;
	radio->sta = (struct ieee80211_sta *)roundup2((uintptr_t)radio->sta_memory, 64);
	memcpy(radio->sta->addr, target->address, ETH_ALEN);
	memcpy(radio->sta->deflink.addr, target->address, ETH_ALEN);
	radio->sta->deflink.sta = radio->sta;
	radio->sta->link[0] = &radio->sta->deflink;
	station_abilities(radio, target->elements, target->elements_length);
	link_changed(radio, BSS_CHANGED_BSSID | BSS_CHANGED_BASIC_RATES | BSS_CHANGED_BEACON_INT);

	/* the access point, to the driver: the firmware learns of the peer, and the radio goes to its channel */
	error = station_state(radio, IEEE80211_STA_NOTEXIST, IEEE80211_STA_NONE);
	lx_tell("FUXI ATH12K: M5 peer and vdev start %d\n", error);
	fa_m4_credits("after the peer and vdev start");
	if (error != 0) {
		radio->sta = NULL;		/* the driver has taken its station away again */
		if (join_radar_refused(radio, "the peer or the vdev start", error)) {
			return (0);
		}
		fa_m4_evidence("peer create or vdev start failed");
		return (error);
	}

	answer = exchange(radio, FA_KIND_AUTH, build_auth, "authentication");
	if (answer == FA_ANSWER_NONE) {
		return (no_answer(radio, "authentication"));
	}
	if (answer != FA_ANSWER_AUTH) {
		join_refused(radio, answer, "authentication");
		return (0);
	}
	error = station_state(radio, IEEE80211_STA_NONE, IEEE80211_STA_AUTH);
	if (error != 0) {
		fa_m4_evidence("station state: authenticated");
		return (error);
	}
	if (radio->went_protected) {
		lx_tell("FUXI ATH12K: M5 refused: the access point now asks for protection\n");
		return (0);
	}

	answer = exchange(radio, FA_KIND_ASSOC_RESPONSE, request_build, "association");
	if (answer == FA_ANSWER_NONE) {
		return (no_answer(radio, "association"));
	}
	if (answer != FA_ANSWER_ASSOC) {
		join_refused(radio, answer, "association");
		return (0);
	}
	lx_tell("FUXI ATH12K: M5 association response: aid %u, %u bytes of elements\n", radio->aid, radio->answer_length);
	if (radio->ios) {
		static char elements[512];

		(void)fa_say_elements(elements, sizeof(elements), radio->answer_elements, radio->answer_length);
		lx_tell("FUXI ATH12K: M8 association elements: %s\n", elements);
	}
	fa_m4_credits("after the association response");

	station_abilities(radio, radio->answer_elements, radio->answer_length);
	radio->sta->aid = (uint16_t)radio->aid;
	error = station_state(radio, IEEE80211_STA_AUTH, IEEE80211_STA_ASSOC);
	if (error == 0 && !radio->protect) {
		error = station_state(radio, IEEE80211_STA_ASSOC, IEEE80211_STA_AUTHORIZED);
	}
	if (error != 0) {
		fa_m4_evidence("station state: associated or authorized");
		return (error);
	}
	vif->cfg.assoc = true;
	vif->cfg.aid = (uint16_t)radio->aid;
	vif_changed(radio, BSS_CHANGED_ASSOC);
	wmm_parameters(radio);
	link_changed(radio, BSS_CHANGED_BEACON_INT | BSS_CHANGED_BEACON_INFO | BSS_CHANGED_ERP_SLOT | BSS_CHANGED_ERP_PREAMBLE | BSS_CHANGED_QOS);
	fa_m4_credits("after the association was told");
	if (fa_m4_trouble()) {
		return (-EIO);
	}
	if (radio->protect) {
		if (!station_handshake(radio)) {
			return (fa_m4_trouble() ? -EIO : 0);
		}
		error = station_keys(radio);
		if (error == 0) {
			error = station_state(radio, IEEE80211_STA_ASSOC, IEEE80211_STA_AUTHORIZED);
		}
		if (error != 0) {
			fa_m4_evidence("keys refused or peer not authorized");
			return (error);
		}
		lx_tell("FUXI ATH12K: M6 4-way ok pairwise group installed\n");
		fa_m4_credits("after the keys");
		if (fa_m4_trouble()) {
			return (-EIO);
		}
	}
	lx_tell("FUXI ATH12K: M5 peer rates %#x, HT %d VHT %d HE %d, %u streams, QoS %d\n", radio->sta->deflink.supp_rates[channel->band],
	    radio->sta->deflink.ht_cap.ht_supported, radio->sta->deflink.vht_cap.vht_supported, radio->sta->deflink.he_cap.has_he,
	    radio->sta->deflink.rx_nss, radio->sta->wme);
	lx_tell("FUXI ATH12K: M5 auth ok assoc ok aid %u freq %u\n", radio->aid, channel->center_freq);
	*joined = true;
	return (0);
}

/* Stay for a while, counting what comes. Ends early when the link goes, and never hangs: the clock alone ends it. */
static void
station_stay(struct fa_radio *radio)
{
	unsigned long long began = lx_os_nanoseconds(), said = began;

	lx_os_lock();
	radio->link_data = radio->link_beacons = radio->link_others = 0;
	radio->rx_ok = radio->rx_clear = 0;
	radio->rssi = radio->target.rssi;
	lx_os_unlock();
	fa_m5_rx_ring("after the association");
	while (lx_os_nanoseconds() - began < FA_JOIN_STAY_NS) {
		bool over;

		lx_os_lock();
		over = radio->answer == FA_ANSWER_GONE || radio->lost || radio->went_protected;
		if (!over) {
			lx_os_sleep(&radio->answer, FA_JOIN_LOOK_STAY_NS);
		}
		lx_os_unlock();
		if (over || fa_m4_trouble()) {
			break;
		}
		if (lx_os_nanoseconds() - said >= FA_JOIN_SAY_EVERY_NS) {
			said = lx_os_nanoseconds();
			lx_tell("FUXI ATH12K: M5 after %llu s: rx data %u beacons %u others %u rssi %d\n", (said - began) / 1000000000ull, radio->link_data,
			    radio->link_beacons, radio->link_others, radio->rssi);
			fa_m4_credits("during the stay");
			fa_m5_rx_ring("during the stay");
		}
	}
	if (radio->answer == FA_ANSWER_GONE) {
		lx_tell("FUXI ATH12K: M5 the access point has shown us the door, reason %u\n", radio->reason);
	}
	if (radio->went_protected) {
		lx_tell("FUXI ATH12K: M5 the access point now asks for %s: leaving\n", radio->protect ? "something else" : "protection");
	}
	lx_tell("FUXI ATH12K: M5 stayed %llu ms\n", (lx_os_nanoseconds() - began) / 1000000ull);
	lx_tell("FUXI ATH12K: M5 rx data %u beacons %u rssi %d\n", radio->link_data, radio->link_beacons, radio->rssi);
	lx_tell("FUXI ATH12K: M5 beacon losses %u others %u\n", radio->beacon_losses, radio->link_others);
	if (radio->protect) {
		lx_tell("FUXI ATH12K: M6 rx ok %u dropped unprotected %u\n", radio->rx_ok, radio->rx_clear);
	}
	fa_m4_credits("after the stay");
	fa_m5_rx_ring("after the stay");
}

/* ---- stage 7: an address, the router, and beyond ---------------------------------------------------------- */

/* A message for the network, ciphered by the firmware with the key we gave it. */
static bool
net_send(struct fa_radio *radio, const uint8_t *to, unsigned type, const uint8_t *body, unsigned length)
{
	bool qos = radio->sta->wme;
	unsigned total = (qos ? FA_QOS_HEADER : FA_FRAME_HEADER) + FA_LLC_LENGTH + length;
	struct sk_buff *skb;
	uint8_t *bytes;

	if ((skb = frame_new(radio, total, &bytes)) == NULL) {
		lx_tell("FUXI ATH12K: M7 no buffer for a message\n");
		return (false);
	}
	(void)fa_data_frame(bytes, total, radio->vif->addr, radio->target.address, to, qos, type, body, length);
	if (radio->protect) {
		IEEE80211_SKB_CB(skb)->control.hw_key = radio->pairwise_key;
	} else {
		bytes[1] &= (uint8_t)~0x40;
	}
	skb->protocol = cpu_to_be16(type);
	skb->priority = 0;
	radio->net_sent++;
	frame_send(radio, skb);
	return (true);
}

/* The next message kept for us, into `out` of FA_NET_MOST bytes. Its length, 0 when the time is up or the link is gone. */
static unsigned
net_next(struct fa_radio *radio, unsigned long long until, unsigned *type, uint8_t *from, uint8_t *out)
{
	while (lx_os_nanoseconds() < until && !fa_m4_trouble()) {
		unsigned length = 0;
		bool gone;

		lx_os_lock();
		gone = radio->answer == FA_ANSWER_GONE || radio->lost || radio->went_protected;
		if (radio->inbox_count != 0) {
			unsigned slot = radio->inbox_first;

			*type = radio->inbox[slot].type;
			length = radio->inbox[slot].length;
			memcpy(from, radio->inbox[slot].from, 6);
			memcpy(out, radio->inbox[slot].bytes, length);
			radio->inbox_first = (slot + 1) % FA_INBOX;
			radio->inbox_count--;
		} else if (!gone) {
			lx_os_sleep(&radio->answer, FA_NET_LOOK_NS);
		}
		lx_os_unlock();
		if (length != 0) {
			return (length);
		}
		if (gone) {
			break;
		}
	}
	return (0);
}

#define IP_SAID(a)	(a)[0], (a)[1], (a)[2], (a)[3]

/* Tries of one DHCP message, until one of `wanted` (and always NAK) comes. The answer in *reply and who sent it in `from`. */
static bool
dhcp_ask(struct fa_radio *radio, unsigned kind, unsigned wanted, unsigned tries, const uint8_t *requested, const uint8_t *server, struct fa_dhcp_reply *reply,
    uint8_t *from, unsigned *tried)
{
	static const uint8_t everybody[ETH_ALEN] = { 0xff, 0xff, 0xff, 0xff, 0xff, 0xff };
	static uint8_t message[FA_DHCP_LENGTH], in[FA_NET_MOST];

	for (*tried = 1; *tried <= tries && !fa_m4_trouble(); (*tried)++) {
		/* Half of the tries ask for an answer to all: an answer that comes only then says which key works. */
		unsigned length = fa_dhcp_build(message, sizeof(message), kind, radio->vif->addr, radio->xid, *tried > tries / 2, requested, server, NULL);
		unsigned long long until = lx_os_nanoseconds() + FA_DHCP_ANSWER_NS;
		unsigned got, type;

		if (!net_send(radio, everybody, FA_TYPE_IP, message, length)) {
			return (false);
		}
		while ((got = net_next(radio, until, &type, from, in)) != 0) {
			if (type == FA_TYPE_IP && fa_dhcp_read(in, got, radio->vif->addr, radio->xid, reply) && (reply->kind == wanted || reply->kind == FA_DHCP_NAK)) {
				return (true);
			}
		}
		if (radio->answer == FA_ANSWER_GONE || radio->lost || radio->went_protected) {
			return (false);
		}
	}
	return (false);
}

/* One echo to `to`, through `hardware`, up to three times. */
static void
echo_ask(struct fa_radio *radio, const uint8_t *hardware, const uint8_t *to, const char *what)
{
	static uint8_t message[FA_ECHO_LENGTH], in[FA_NET_MOST];
	unsigned long long began = lx_os_nanoseconds();
	unsigned id = radio->xid & 0xffff;

	for (unsigned sequence = 1; sequence <= FA_NET_TRIES && !fa_m4_trouble(); sequence++) {
		unsigned long long until = lx_os_nanoseconds() + FA_ECHO_ANSWER_NS;
		unsigned length = fa_echo_request(message, sizeof(message), radio->own_address, to, id, sequence), got, type;
		uint8_t from[6];

		if (!net_send(radio, hardware, FA_TYPE_IP, message, length)) {
			return;
		}
		while ((got = net_next(radio, until, &type, from, in)) != 0) {
			if (type == FA_TYPE_IP && fa_echo_read(in, got, to, radio->own_address, id, sequence)) {
				lx_tell("FUXI ATH12K: M7 ping %s %u.%u.%u.%u ok, try %u, %llu ms\n", what, IP_SAID(to), sequence, (lx_os_nanoseconds() - began) / 1000000ull);
				return;
			}
		}
	}
	lx_tell("FUXI ATH12K: M7 ping %s %u.%u.%u.%u no answer to %u tries\n", what, IP_SAID(to), FA_NET_TRIES);
}

/* What an address came to, and what the air said about it. */
static void
net_counts(struct fa_radio *radio, const char *when)
{
	lx_tell("FUXI ATH12K: M7 %s: sent %u; data done %u acked %u; rx ok %u dropped unprotected %u; kept lost %u\n", when, radio->net_sent, radio->data_done, radio->data_acked,
	    radio->rx_ok, radio->rx_clear, radio->inbox_lost);
}

/* An address from the network, then the router and a host beyond it. Bounded by the clock, and whatever it comes to, the stay follows. */
static void
station_address(struct fa_radio *radio)
{
	static const uint8_t beyond[4] = { 1, 1, 1, 1 };
	static const uint8_t everybody[ETH_ALEN] = { 0xff, 0xff, 0xff, 0xff, 0xff, 0xff };
	static uint8_t message[FA_ARP_LENGTH], in[FA_NET_MOST];
	unsigned long long began = lx_os_nanoseconds();
	struct fa_dhcp_reply offer, ack;
	uint8_t from[6], router[6];
	bool found = false;
	unsigned tried;

	get_random_bytes(&radio->xid, sizeof(radio->xid));
	lx_os_lock();
	radio->rx_ok = radio->rx_clear = 0;
	radio->net_on = true;
	lx_os_unlock();
	if (!dhcp_ask(radio, FA_DHCP_DISCOVER, FA_DHCP_OFFER, FA_DHCP_TRIES, NULL, NULL, &offer, from, &tried) || offer.kind != FA_DHCP_OFFER) {
		lx_tell("FUXI ATH12K: M7 not proven: no DHCP offer after %u discovers\n", tried > FA_DHCP_TRIES ? FA_DHCP_TRIES : tried);
		net_counts(radio, "no offer");
		return;
	}
	lx_tell("FUXI ATH12K: M7 DHCP offer server %u.%u.%u.%u yiaddr %u.%u.%u.%u, try %u, %llu ms\n", IP_SAID(offer.server), IP_SAID(offer.address), tried,
	    (lx_os_nanoseconds() - began) / 1000000ull);
	if (!dhcp_ask(radio, FA_DHCP_REQUEST, FA_DHCP_ACK, FA_DHCP_TRIES, offer.address, offer.server, &ack, from, &tried) || ack.kind != FA_DHCP_ACK) {
		lx_tell("FUXI ATH12K: M7 not proven: %s\n", ack.kind == FA_DHCP_NAK ? "the server said no to our request" : "no DHCP ack to our request");
		net_counts(radio, "no ack");
		return;
	}
	memcpy(radio->own_address, ack.address, 4);
	memcpy(radio->server, ack.server, 4);
	memcpy(radio->server_hardware, from, 6);
	radio->leased = true;
	lx_tell("FUXI ATH12K: M7 DHCP ack server %u.%u.%u.%u yiaddr %u.%u.%u.%u, lease %u s, %llu ms\n", IP_SAID(ack.server), IP_SAID(ack.address), ack.lease,
	    (lx_os_nanoseconds() - began) / 1000000ull);
	lx_tell("FUXI ATH12K: M7 network mask %u.%u.%u.%u router %u.%u.%u.%u name server %u.%u.%u.%u\n", IP_SAID(ack.mask), IP_SAID(ack.router), IP_SAID(ack.name_server));
	net_counts(radio, "with the address");
	if (!ack.has_router) {
		lx_tell("FUXI ATH12K: M7 the network names no router\n");
		return;
	}
	for (unsigned attempt = 0; attempt < FA_NET_TRIES && !found && !fa_m4_trouble(); attempt++) {
		unsigned long long until = lx_os_nanoseconds() + FA_ARP_ANSWER_NS;
		unsigned length = fa_arp_request(message, sizeof(message), radio->vif->addr, radio->own_address, ack.router), got, type;

		if (!net_send(radio, everybody, FA_TYPE_ARP, message, length)) {
			return;
		}
		while (!found && (got = net_next(radio, until, &type, from, in)) != 0) {
			found = type == FA_TYPE_ARP && fa_arp_read(in, got, radio->own_address, ack.router, router);
		}
	}
	if (!found) {
		lx_tell("FUXI ATH12K: M7 not proven: the router does not answer ARP\n");
		net_counts(radio, "no router");
		return;
	}
	lx_tell("FUXI ATH12K: M7 router found\n");
	echo_ask(radio, router, ack.router, "router");
	echo_ask(radio, router, beyond, "beyond");
	net_counts(radio, "after the echoes");
}

/* What the load test says every so often, so that a panic has recent numbers before it. */
static void
serve_counts(struct fa_radio *radio)
{
	lx_tell("FUXI ATH12K: M7 serving: rx ok %u sent %u done %u dropped %u, stale %u, msdu not done %u, inbox lost %u; arp %u echo %u\n", radio->rx_ok, radio->net_sent,
	    radio->data_done, radio->tx_dropped, rx_stale, rx_not_done, radio->inbox_lost, radio->served_arp, radio->served_echo);
	fa_m4_credits("while serving");
	fa_m5_rx_ring("while serving");
}

/*
 * Stage 7's load test: for a bounded time, ARP and echoes that are for us are answered from here, and what else comes is only
 * counted by join_data_count(). Ends early when the access point shows us the door, the link goes, or the firmware is in trouble.
 */
static void
station_serve(struct fa_radio *radio)
{
	static uint8_t in[FA_NET_MOST], out[FA_NET_MOST];
	unsigned long long began = lx_os_nanoseconds(), said = began;

	lx_os_lock();
	radio->serving = true;
	lx_os_unlock();
	lx_tell("FUXI ATH12K: M7 serving for %llu s\n", FA_SERVE_NS / 1000000000ull);
	for (;;) {
		unsigned long long now = lx_os_nanoseconds(), until;
		uint8_t from[6];
		unsigned got, type, length;

		if (now - began >= FA_SERVE_NS || radio->answer == FA_ANSWER_GONE || radio->lost || radio->went_protected || fa_m4_trouble()) {
			break;
		}
		if (now - said >= FA_SERVE_SAY_NS) {
			said = now;
			serve_counts(radio);
		}
		until = said + FA_SERVE_SAY_NS < began + FA_SERVE_NS ? said + FA_SERVE_SAY_NS : began + FA_SERVE_NS;
		if ((got = net_next(radio, until, &type, from, in)) == 0) {
			continue;
		}
		if (type == FA_TYPE_ARP && (length = fa_arp_reply(out, sizeof(out), radio->vif->addr, radio->own_address, in, got)) != 0) {
			radio->served_arp++;
			(void)net_send(radio, in + 8, FA_TYPE_ARP, out, length);
		} else if (type == FA_TYPE_IP && (length = fa_echo_reply(out, sizeof(out), radio->own_address, in, got)) != 0) {
			radio->served_echo++;
			(void)net_send(radio, from, FA_TYPE_IP, out, length);
		}
	}
	lx_os_lock();
	radio->serving = false;
	lx_os_unlock();
	serve_counts(radio);
	lx_tell("FUXI ATH12K: M7 served %llu ms\n", (lx_os_nanoseconds() - began) / 1000000ull);
}

/* The address goes back: a lease on somebody's network is not left behind by a test. */
static void
station_address_back(struct fa_radio *radio)
{
	static uint8_t message[FA_DHCP_LENGTH];

	radio->net_on = false;
	if (radio->leased && radio->answer != FA_ANSWER_GONE && !radio->lost && !fa_m4_trouble()) {
		unsigned length = fa_dhcp_build(message, sizeof(message), FA_DHCP_RELEASE, radio->vif->addr, radio->xid, false, NULL, radio->server, radio->own_address);

		if (net_send(radio, radio->server_hardware, FA_TYPE_IP, message, length)) {
			lx_tell("FUXI ATH12K: M7 the address given back\n");
		}
	}
	radio->leased = false;
}

/* The handshake's beginning, from what the chosen access point offers. False, and said, when it is not to be joined. */
static bool
handshake_prepare(struct fa_radio *radio)
{
	const struct fa_target *target = &radio->target;
	struct fa_rsn rsn;
	const char *why = fa_rsn_read(target->elements, target->elements_length, &rsn);
	const uint8_t *element = fa_element(target->elements, target->elements_length, FA_EID_RSN);
	uint8_t any = 0;

	lx_tell("FUXI ATH12K: M6 target offers group %u pairwise %#x akm %#x capabilities %#x\n", rsn.group, rsn.pairwise, rsn.akm, rsn.capabilities);
	if (why != NULL) {
		lx_tell("FUXI ATH12K: M6 refused: %s\n", why);
		return (false);
	}
	memset(&radio->wpa, 0, sizeof(radio->wpa));
	memcpy(radio->wpa.pmk, target_key, sizeof(radio->wpa.pmk));
	memcpy(radio->wpa.own, radio->vif->addr, ETH_ALEN);
	memcpy(radio->wpa.peer, target->address, ETH_ALEN);
	get_random_bytes(radio->wpa.own_nonce, sizeof(radio->wpa.own_nonce));
	for (unsigned i = 0; i < sizeof(radio->wpa.own_nonce); i++) {
		any |= radio->wpa.own_nonce[i];
	}
	if (any == 0) {
		lx_tell("FUXI ATH12K: M6 refused: the random source gave only zeros\n");
		return (false);
	}
	radio->wpa.rsn = FA_RSN_OURS;
	radio->wpa.rsn_length = sizeof(FA_RSN_OURS);
	radio->wpa.peer_rsn = element;
	radio->wpa.peer_rsn_length = 2u + element[1];
	return (true);
}

/* Scans until the network is heard or refused, at most FA_JOIN_SCANS. 0, or what a scan came to. */
static int
join_find(struct fa_radio *radio, bool passive)
{
	int error = 0;

	for (unsigned scan = 0; scan < FA_JOIN_SCANS && !radio->target.found && radio->target.protected_frames == 0 && error == 0; scan++) {
		error = station_scan(radio, passive);
	}
	return (error);
}

/*
 * Stage 5, from fa_hif.c on fw_thread. With no name given nothing happens, the radio least of all. A network that asks for
 * protection is refused before a frame is sent to it: only its name went out, in the scan's probe requests. Trouble takes
 * the same way as on stage 4: the firmware's own account first, then the way down. Not being heard, refused, or an access
 * point that does not answer are not the firmware's fault: they return 0 and say that nothing was proven.
 */
int
fa_m5_station(struct ieee80211_hw *hw, bool passive)
{
	struct fa_radio *radio = radio_of(hw);
	unsigned regd_before = radio->regd_applied;
	struct fa_target *target = &radio->target;
	bool joined = false;
	int error;

	if (target_name_length == 0) {
		lx_tell("FUXI ATH12K: M5 not run: no usable fuxiwifissid, so no radio work\n");
		return (0);
	}
	lx_tell("FUXI ATH12K: M5 target ssid length %u\n", target_name_length);
	radio->protect = target_protected;
	target->wanted = target_protected ? FA_PROTECT_RSN : FA_OPEN;
	radio->asked.ssid_len = (uint8_t)target_name_length;
	memcpy(radio->asked.ssid, target_name, target_name_length);
	error = station_up(radio);
	if (error != 0) {
		lx_tell("FUXI ATH12K: M4 not proven: bring-up %d\n", error);
		fa_m4_evidence("bring-up failed");
	} else if (!fa_m4_trouble()) {
		regd_wait(radio, regd_before);
		if (!fa_m4_trouble()) {
			error = join_find(radio, passive);
		}
	}
	if (error == 0 && !fa_m4_trouble()) {
		if (target->protected_frames != 0 && radio->protect) {
			lx_tell("FUXI ATH12K: M6 refused: the target has %s (%u frames), only WPA2 with a key is built\n",
			    fa_protection_name(target->why), target->protected_frames);
		} else if (target->protected_frames != 0) {
			lx_tell("FUXI ATH12K: M5 refused: the target has %s (%u frames), only open networks are allowed at this stage\n",
			    fa_protection_name(target->why), target->protected_frames);
		} else if (!target->found) {
			lx_tell("FUXI ATH12K: M5 not proven: the target was not heard in %u scans\n", FA_JOIN_SCANS);
		} else {
			lx_tell("FUXI ATH12K: M5 target heard %u times, %u MHz at %d dBm, capability %#x, beacon every %u, HT %d VHT %d HE %d WMM %d\n",
			    target->heard, target->frequency, target->rssi, target->capability, target->interval,
			    fa_element(target->elements, target->elements_length, FA_EID_HT) != NULL,
			    fa_element(target->elements, target->elements_length, FA_EID_VHT) != NULL,
			    fa_ext_element(target->elements, target->elements_length, FA_EXT_HE) != NULL,
			    fa_wmm_element(target->elements, target->elements_length) != NULL);
			if (!radio->protect || handshake_prepare(radio)) {
				error = station_join(radio, &joined);
				if (joined) {
					if (target_address) {
						station_address(radio);
						if (target_serve && radio->leased) {
							station_serve(radio);
						}
					}
					station_stay(radio);
					station_address_back(radio);
				}
			}
		}
	}
	if (fa_m4_trouble()) {
		fa_m4_firmware_dead();
	}
	if (radio->chanctx != NULL) {
		station_leave(radio);
	}
	station_down(radio);
	fa_wipe(&radio->wpa, sizeof(radio->wpa));
	fa_wipe(radio->eapol, sizeof(radio->eapol));
	fa_wipe(target_key, sizeof(target_key));
	if (fa_m4_trouble()) {
		fa_m4_firmware_dead();
		return (error != 0 ? error : -EIO);
	}
	return (error);
}

/* ---- stage 8: the radio for iOS ---------------------------------------------------------------------------- */

#define FA_IOS_READY_NS		(60 * 1000000000ull)
#define FA_IOS_LOOK_NS		(100 * 1000000ull)
#define FA_AUTO_WAIT_NS		(30 * 1000000000ull)
#define FA_IOS_TX_DONE_NS	(1000 * 1000000ull)
#define FA_IOS_SAY_EVERY_NS	(60 * 1000000000ull)
#define FA_JOIN_CANDIDATES	4		/* access points one join goes to: each can take seconds, under the gate */

void
fa_ios_start(const struct fa_ios_input *input)
{
	ios_input = *input;
}

/* Before fa_firmware_ready(), fuxiwifiauto=1 only. Anything but 1 to 32 bytes of name is no network. */
void
fa_set_auto_quiet(void)
{
	auto_quiet = true;
}

void
fa_set_auto_join(const unsigned char *name, unsigned length, const unsigned char *key)
{
	auto_name_length = length >= 1 && length <= FA_SSID_MOST ? length : 0;
	if (auto_name_length != 0) {
		memcpy(auto_name, name, auto_name_length);
		memcpy(auto_key, key, sizeof(auto_key));
	}
}

static void
auto_forget(void)
{
	fa_wipe(auto_key, sizeof(auto_key));
	auto_name_length = 0;
}

/* Nothing kept for joining again, and nothing going after it. Holding the gate. */
static void
rejoin_forget(struct fa_radio *radio)
{
	fa_wipe(rejoin_key, sizeof(rejoin_key));
	fa_wipe(rejoin_name, sizeof(rejoin_name));
	rejoin_name_length = 0;
	radio->ios_rejoining = false;
	radio->rejoin_tries = 0;
	radio->rejoin_at = 0;
}

/* iOS's side has said what it wants: from here its word is the only one. Holding the gate. */
static void
auto_gives_way(struct fa_radio *radio, const char *what)
{
	radio->ios_auto_joined = false;
	if (auto_name_length != 0) {
		auto_forget();
		lx_tell("FUXI ATH12K: M8 auto join: given up, wifid asked for %s\n", what);
	}
	if (rejoin_name_length != 0) {
		lx_tell("FUXI ATH12K: M8 rejoin: %s, wifid asked for %s\n", radio->ios_rejoining ? "stopped" : "will not happen", what);
		rejoin_forget(radio);
	}
}

/* One of the station thread and ios_step() at a time: a loss must not meet a join halfway. Each holds it for a bounded time. */
static void
ios_gate_in(struct fa_radio *radio)
{
	lx_os_lock();
	while (radio->ios_gate) {
		lx_os_sleep(&radio->ios_gate, FA_IOS_LOOK_NS);
	}
	radio->ios_gate = true;
	lx_os_unlock();
}

static void
ios_gate_out(struct fa_radio *radio)
{
	lx_os_lock();
	radio->ios_gate = false;
	lx_os_wakeup(&radio->ios_gate);
	lx_os_unlock();
}

/* The radio, waiting for it to come up: its firmware needs the root, and iOS asks as soon as there is a wifid. NULL when it did not come. */
static struct fa_radio *
ios_wait(void)
{
	unsigned long long began = lx_os_nanoseconds();
	struct fa_radio *radio;

	lx_os_lock();
	while ((radio = ios_radio) == NULL && lx_os_nanoseconds() - began < FA_IOS_READY_NS) {
		lx_os_sleep(&ios_radio, FA_IOS_LOOK_NS);
	}
	lx_os_unlock();
	return (radio);
}

static void
ios_phase(struct fa_radio *radio, int phase, int answer)
{
	lx_os_lock();
	radio->ios_phase = phase;
	radio->ios_last_answer = answer;
	lx_os_unlock();
}

/* From here frames do not pass: said to iOS's side, and whoever is sending finished, before the driver's station goes. */
static void
ios_frames_stop(struct fa_radio *radio)
{
	unsigned long long began = lx_os_nanoseconds();
	bool was;

	lx_os_lock();
	was = radio->ios_joined;
	radio->ios_joined = false;		/* design W9: before anything is taken away */
	while (radio->ios_tx_busy != 0 && lx_os_nanoseconds() - began < FA_IOS_TX_DONE_NS) {
		lx_os_sleep(&radio->ios_tx_busy, FA_NET_LOOK_NS);
	}
	lx_os_unlock();
	if (was) {
		ios_input.link(0);
	}
}

/* What a join has made, gone, and nothing of the key left. Ready for the next one. */
static void
ios_clean(struct fa_radio *radio)
{
	if (radio->chanctx != NULL) {
		station_leave(radio);
	}
	lx_os_lock();
	radio->eapol_length = 0;
	radio->ba_want = 0;
	radio->ba_live = 0;
	radio->ios_pending &= IOS_DEAD;
	memset(&radio->group_pn, 0, sizeof(radio->group_pn));
	lx_os_unlock();
	fa_wipe(&radio->wpa, sizeof(radio->wpa));
	fa_wipe(radio->eapol, sizeof(radio->eapol));
	fa_wipe(target_key, sizeof(target_key));
	target_name_length = 0;
	target_protected = false;
}

/* The firmware is gone, and with it everything this stage could do: iOS hears it once, the dump follows, then only the host is touched. */
static void
ios_dies(struct fa_radio *radio)
{
	lx_tell("FUXI ATH12K: M8 the firmware is gone\n");
	radio->ios_dead = true;
	auto_forget();
	rejoin_forget(radio);
	ios_frames_stop(radio);
	ios_phase(radio, FA_IOS_OFF, FA_IOS_RADIO_DEAD);
	ios_input.lost(FA_IOS_RADIO_DEAD);
	fa_m4_firmware_dead();
	ios_clean(radio);
}

/* A message after the handshake. Only a new group key is taken: a new four-way is refused (W1), and the access point's going follows as loss. */
static void
ios_eapol(struct fa_radio *radio)
{
	static uint8_t message[FA_EAPOL_MOST];
	uint8_t reply[FA_WPA_REPLY_MOST];
	unsigned length, reply_length = 0, index;
	const char *why = "";
	struct ieee80211_key_conf *old, *fresh = NULL;
	int said, error;

	lx_os_lock();
	length = radio->eapol_length;
	if (length != 0) {
		memcpy(message, radio->eapol, length);
		radio->eapol_length = 0;
	}
	lx_os_unlock();
	if (length == 0) {
		return;
	}
	said = fa_wpa_message(&radio->wpa, message, length, reply, &reply_length, &why);
	fa_wipe(message, sizeof(message));
	if (said != FA_WPA_GROUP) {
		lx_tell("FUXI ATH12K: M8 handshake message after the keys, %u bytes: %s%s\n", length, said == FA_WPA_BAD ? "not good: " : "not one to answer", why);
		fa_wipe(reply, sizeof(reply));
		return;
	}
	(void)eapol_send(radio, reply, reply_length, true);
	fa_wipe(reply, sizeof(reply));
	index = radio->wpa.group_index;
	old = radio->group_key;
	/* Design W10: the old key stays until the new one is in. A key kept under the same number is replaced by giving it. */
	error = key_give(radio, radio->wpa.group, index, false, radio->group_slot == 1 ? 2 : 1, &fresh);
	lx_tell("FUXI ATH12K: M8 new group key, number %u: %d\n", index, error);
	if (error != 0) {
		return;
	}
	radio->group_slot = radio->group_slot == 1 ? 2 : 1;
	lx_os_lock();
	radio->group_pn.last[index] = fa_rsc_read(radio->wpa.group_counter);
	radio->group_pn.known[index] = true;
	if (old != NULL && old->keyidx != index) {
		radio->group_pn.known[old->keyidx] = false;
	}
	radio->group_key = fresh;
	lx_os_unlock();
	if (old != NULL) {
		if (old->keyidx != index) {
			key_take_back(radio, &old, false);
		} else {
			fa_wipe(old->key, 16);
		}
	}
}

/*
 * The access point's block ack requests, on this thread: the driver's call sends WMI commands, whose answers the poll thread
 * must be free to see (mac80211: agg-rx.c). One for a live class stops that first. A delayed policy is declined.
 */
static void
ios_ba(struct fa_radio *radio)
{
	for (unsigned tid = 0; tid < FA_BA_TIDS; tid++) {
		struct ieee80211_ampdu_params params = { .sta = radio->sta, .tid = (uint8_t)tid };
		struct sk_buff *skb;
		struct fa_ba_ask ask;
		uint8_t *bytes;
		unsigned window = 0;
		bool wanted;
		int result = -EOPNOTSUPP;

		lx_os_lock();
		wanted = (radio->ba_want & (1u << tid)) != 0;
		ask = radio->ba_asked[tid];
		radio->ba_want &= (unsigned char)~(1u << tid);
		lx_os_unlock();
		if (!wanted || radio->sta == NULL) {
			continue;
		}
		wiphy_lock(radio->wiphy);
		if ((radio->ba_live & (1u << tid)) != 0) {
			params.action = IEEE80211_AMPDU_RX_STOP;
			(void)radio->ops->ampdu_action(&radio->hw, radio->vif, &params);
			radio->ba_live &= (unsigned char)~(1u << tid);
			radio->ba_stopped++;
		}
		if (ask.start && ask.immediate) {
			window = fa_ba_size(ask.size, radio->sta->deflink.he_cap.has_he ? FA_BA_SIZE_HE : FA_BA_SIZE_HT, radio->hw.max_rx_aggregation_subframes);
			params.action = IEEE80211_AMPDU_RX_START;
			params.buf_size = (uint16_t)window;
			params.timeout = ask.timeout;
			params.ssn = ask.ssn;
			params.amsdu = ask.amsdu;
			result = radio->ops->ampdu_action(&radio->hw, radio->vif, &params);
			if (result == 0) {
				radio->ba_live |= (unsigned char)(1u << tid);
			}
		}
		wiphy_unlock(radio->wiphy);
		if (!ask.start) {
			lx_tell("FUXI ATH12K: M8 block-ack tid %u: stopped by the access point\n", tid);
			continue;
		}
		radio->ba_started += result == 0;
		radio->ba_refused += result != 0;
		lx_tell("FUXI ATH12K: M8 block-ack tid %u: size %u, result %d\n", tid, window, result);
		if ((skb = frame_new(radio, FA_ADDBA_RESPONSE_LENGTH, &bytes)) == NULL) {
			lx_tell("FUXI ATH12K: M8 no buffer for the answer to block-ack tid %u\n", tid);
			continue;
		}
		(void)fa_addba_response(bytes, FA_ADDBA_RESPONSE_LENGTH, radio->vif->addr, radio->target.address, &ask, result == 0 ? 0 : FA_STATUS_DECLINED, result == 0 ? window : ask.size);
		frame_send(radio, skb);
	}
}

static void ios_auto_step(struct fa_radio *radio);

/* The account of what went out, then and now. */
static void
ios_tx_say(const char *what, const struct fa_tx_counts *now, const struct fa_tx_counts *then)
{
	lx_tell("FUXI ATH12K: M8 %s: tx arp %u ip6 %u tcp %u udp %u icmp %u ip other %u eapol %u other %u\n", what, now->arp - then->arp, now->ip6 - then->ip6,
	    now->tcp - then->tcp, now->udp - then->udp, now->icmp - then->icmp, now->ip_other - then->ip_other, now->eapol - then->eapol, now->other - then->other);
}

/* What was heard in numbers: next to the joined line, and once more at a loss. */
static void
ios_heard_say(struct fa_radio *radio)
{
	static const struct fa_tx_counts none;
	static char actions[256];
	unsigned rings, slots, at = 0;

	actions[0] = 0;
	for (unsigned category = 0; category < 128; category++) {
		if (radio->action[category] != 0) {
			at = fa_say_number(actions, sizeof(actions), at, category, ':');
			at = fa_say_number(actions, sizeof(actions), at, radio->action[category], ' ');
		}
	}
	lx_pool_peaks(&rings, &slots);
	lx_tell("FUXI ATH12K: M8 heard: eapol %u lost %u others %u, door others %u, slots peak %u, msdu done later %u; action ciphered %u, by category: %s\n", radio->eapol_heard,
	    radio->eapol_lost, radio->link_others, radio->door_other, slots, rx_done_later, radio->action_ciphered, actions);
	ios_tx_say("since the join", &radio->tx, &none);
	lx_tell("FUXI ATH12K: M8 filtered %u\n", radio->tx_filtered);
}

/* A loss's account, on the thread, before the link's state goes. Numbers alone. */
static void
ios_loss_say(struct fa_radio *radio)
{
	unsigned long long now = lx_os_nanoseconds();
	struct fa_tx_counts then = radio->tx_then[FA_TX_STEPS - 1];
	unsigned long long then_at = radio->tx_then_at[FA_TX_STEPS - 1];
	unsigned first = radio->asks_count > FA_ASKS_MOST ? radio->asks_count - FA_ASKS_MOST : 0;

	if (radio->door_seen) {
		lx_tell("FUXI ATH12K: M8 door: kind %#x reason %u seq %u retry %d protected %d, %u bytes, rssi %d, beacons rssi %d seq %u, %llu ms after the join\n", radio->door.kind,
		    radio->door.reason, radio->door.seq, radio->door.retry, radio->door.protect, radio->door.length, radio->door_rssi, radio->door_beacon_rssi, radio->door_beacon_seq,
		    (radio->door_at - radio->ios_joined_at) / 1000000ull);
	}
	ios_heard_say(radio);
	if (then_at == 0) {
		then_at = radio->ios_joined_at;
		memset(&then, 0, sizeof(then));
	}
	lx_tell("FUXI ATH12K: M8 the last %llu s:\n", (now - then_at) / 1000000000ull);
	ios_tx_say("then", &radio->tx, &then);
	for (unsigned i = first; i < radio->asks_count; i++) {
		unsigned slot = i % FA_ASKS_MOST;
		unsigned long long at = radio->asks[slot].at;

		lx_tell("FUXI ATH12K: M8 before the loss: wifid set %d value %d, %lld s from the join\n", radio->asks[slot].number, radio->asks[slot].value,
		    (long long)(at - radio->ios_joined_at) / 1000000000ll);
	}
}

/* Every few seconds, what has gone out so far, so that a loss can say what its last seconds were. */
static void
ios_tx_step(struct fa_radio *radio)
{
	unsigned long long now = lx_os_nanoseconds();

	if (radio->ios_joined && now - radio->tx_then_at[0] >= FA_TX_STEP_NS) {
		lx_os_lock();
		for (unsigned i = FA_TX_STEPS - 1; i > 0; i--) {
			radio->tx_then[i] = radio->tx_then[i - 1];
			radio->tx_then_at[i] = radio->tx_then_at[i - 1];
		}
		radio->tx_then[0] = radio->tx;
		radio->tx_then_at[0] = now;
		lx_os_unlock();
	}
}

/* From the Apple80211 side, on whatever thread asks: the last few are kept for an account of a loss. */
void
fa_ios_note_request(int number, int value)
{
	struct fa_radio *radio = ios_radio;

	if (radio == NULL) {
		return;
	}
	lx_os_lock();
	radio->asks[radio->asks_count % FA_ASKS_MOST].number = number;
	radio->asks[radio->asks_count % FA_ASKS_MOST].value = value;
	radio->asks[radio->asks_count % FA_ASKS_MOST].at = lx_os_nanoseconds();
	radio->asks_count++;
	lx_os_unlock();
}

/* What the thread does each time it wakes. Apart from the thread itself so that the host test can run it. */
static void
ios_step(struct fa_radio *radio)
{
	unsigned pending;

	ios_gate_in(radio);
	ios_tx_step(radio);
	lx_os_lock();
	pending = radio->ios_pending;
	radio->ios_pending = 0;
	lx_os_unlock();
	if ((pending & IOS_DEAD) != 0 && !radio->ios_dead) {
		ios_dies(radio);
	} else if ((pending & IOS_LOSS) != 0 && radio->ios_joined) {
		int answer = FA_IOS_GONE;
		bool again = radio->ios_auto_joined && (radio->answer == FA_ANSWER_GONE || radio->lost);

		if (radio->ios_auto_joined && !again) {
			/* It asks for something else now: nobody joins that behind anyone's back. */
			radio->ios_auto_joined = false;
			rejoin_forget(radio);
			lx_tell("FUXI ATH12K: M8 auto join: the network is lost, not joining again\n");
		}
		lx_tell("FUXI ATH12K: M8 the network is lost: door %d, reason %u, connection %d, changed %d\n", radio->answer == FA_ANSWER_GONE, radio->reason, radio->lost, radio->went_protected);
		ios_loss_say(radio);
		ios_frames_stop(radio);
		ios_phase(radio, FA_IOS_IDLE, again ? FA_IOS_DONE : answer);
		ios_input.lost(answer);
		ios_clean(radio);
		if (again) {
			radio->ios_auto_joined = false;
			radio->ios_rejoining = true;
			radio->rejoin_tries = 0;
			radio->rejoin_at = lx_os_nanoseconds() + fa_rejoin_wait_ms(0) * 1000000ull;
			lx_tell("FUXI ATH12K: M8 rejoin: lost, the first try in %u ms\n", fa_rejoin_wait_ms(0));
		}
	} else if ((pending & IOS_EAPOL) != 0 && radio->ios_joined) {
		ios_eapol(radio);
	}
	if ((pending & IOS_BA) != 0 && radio->ios_joined) {
		ios_ba(radio);
	}
	if (radio->ios_joined && lx_os_nanoseconds() - radio->ios_said >= FA_IOS_SAY_EVERY_NS) {
		radio->ios_said = lx_os_nanoseconds();
		lx_tell("FUXI ATH12K: M8 joined: rx ok %u dropped unprotected %u, played again %u, no key %u, too long %u; sent %u, done %u acked %u dropped %u; beacons %u losses %u; block-ack %u refused %u stopped %u; stale %u, msdu not done %u\n",
		    radio->rx_ok, radio->rx_clear, radio->rx_replay, radio->rx_no_key, radio->rx_long, radio->net_sent, radio->data_done, radio->data_acked, radio->tx_dropped,
		    radio->link_beacons, radio->beacon_losses, radio->ba_started, radio->ba_refused, radio->ba_stopped, rx_stale, rx_not_done);
		ios_heard_say(radio);
		fa_m5_rx_ring("while joined");
	}
	ios_gate_out(radio);
}

static void ios_rejoin_step(struct fa_radio *radio);

static void
ios_runs(void *argument)
{
	struct fa_radio *radio = argument;

	for (;;) {
		unsigned long long rest = 10 * FA_IOS_LOOK_NS;

		lx_os_lock();
		if (radio->ios_rejoining) {
			unsigned long long clock = lx_os_nanoseconds();

			rest = radio->rejoin_at <= clock ? 1 : radio->rejoin_at - clock < rest ? radio->rejoin_at - clock : rest;
		}
		if (radio->ios_pending == 0) {
			lx_os_sleep(&radio->ios_pending, rest);
		}
		lx_os_unlock();
		ios_step(radio);
		ios_auto_step(radio);
		ios_rejoin_step(radio);
	}
}

static int ios_join_run(struct fa_radio *radio, const unsigned char *ssid, unsigned ssid_length, const unsigned char *bssid, const unsigned char *key, bool alone);

/* The auto join's own turn, on this thread with nothing else held. It joins as wifid would, and only once. */
static void
ios_auto_step(struct fa_radio *radio)
{
	uint8_t name[FA_SSID_MOST], key[32];
	unsigned length;
	int answer;

	ios_gate_in(radio);
	if (auto_name_length == 0 || (!radio->ios_powered && lx_os_nanoseconds() - radio->ios_up_at < FA_AUTO_WAIT_NS)) {
		ios_gate_out(radio);
		return;
	}
	length = auto_name_length;
	memcpy(name, auto_name, length);
	memcpy(key, auto_key, sizeof(key));
	auto_forget();
	ios_gate_out(radio);
	lx_tell("FUXI ATH12K: M8 auto join: name of %u bytes\n", length);
	answer = ios_join_run(radio, name, length, NULL, key, true);
	fa_wipe(key, sizeof(key));
	lx_tell("FUXI ATH12K: M8 auto join: answer %d\n", answer);
	if (auto_quiet) {
		lx_tell("FUXI ATH12K: M8 auto join: not told to the Apple80211 side, fuxiwifiauto=2\n");
	} else {
		ios_input.joined_alone(answer);
	}
}

/*
 * The network the auto join had, lost by the access point's word or the driver's: scan and join again, on this thread with nothing else
 * held, for as long as nobody says otherwise. iOS's side saw the link go down once and sees it come up once, and of the tries in
 * between only a line in the log: no answer of a join that it did not ask for, which it could take for its own.
 */
static void
ios_rejoin_step(struct fa_radio *radio)
{
	uint8_t name[FA_SSID_MOST], key[32];
	unsigned length, tries;
	int answer;

	ios_gate_in(radio);
	if (!radio->ios_rejoining || radio->ios_dead || lx_os_nanoseconds() < radio->rejoin_at) {
		ios_gate_out(radio);
		return;
	}
	length = rejoin_name_length;
	memcpy(name, rejoin_name, length);
	memcpy(key, rejoin_key, sizeof(key));
	tries = ++radio->rejoin_tries;
	ios_gate_out(radio);
	lx_tell("FUXI ATH12K: M8 rejoin: try %u\n", tries);
	answer = ios_join_run(radio, name, length, NULL, key, true);
	fa_wipe(key, sizeof(key));
	fa_wipe(name, sizeof(name));
	if (answer == FA_IOS_DONE) {
		lx_tell("FUXI ATH12K: M8 rejoin: joined at try %u\n", tries);
		if (auto_quiet) {
			lx_tell("FUXI ATH12K: M8 auto join: not told to the Apple80211 side, fuxiwifiauto=2\n");
		} else if (radio->ios_auto_joined) {
			ios_input.joined_alone(answer);
		}
		return;
	}
	ios_gate_in(radio);
	if (answer == FA_IOS_RADIO_DEAD) {
		lx_tell("FUXI ATH12K: M8 rejoin: try %u answer %d, the firmware is gone: no more\n", tries, answer);
		rejoin_forget(radio);
	} else if (radio->ios_rejoining && !radio->ios_wifid_asked) {
		radio->rejoin_at = lx_os_nanoseconds() + fa_rejoin_wait_ms(tries) * 1000000ull;
		lx_tell("FUXI ATH12K: M8 rejoin: try %u answer %d, the next in %u ms\n", tries, answer, fa_rejoin_wait_ms(tries));
	}
	ios_gate_out(radio);
}

/* From the poll thread, which may not wait for the firmware's dump: it notes it. */
void
fa_m8_firmware_gone(void)
{
	struct fa_radio *radio = ios_radio;

	if (radio != NULL && !radio->ios_dead) {
		lx_os_lock();
		radio->ios_pending |= IOS_DEAD;
		lx_os_wakeup(&radio->ios_pending);
		lx_os_unlock();
	}
}

/*
 * Stage 8, from fa_firmware_ready() when core.c is through: the radio started and the interface on it, and from here it
 * stays. Not inside fa_stage_up() as the others are: that one runs with core.c's hardware group locked. 0, or -errno, and then
 * the firmware's account follows as for any failed hand-off.
 */
int
fa_m8_up(struct ieee80211_hw *hw)
{
	struct fa_radio *radio = radio_of(hw);
	unsigned regd_before = radio->regd_applied;
	int error;

	radio->ios = true;
	radio->ios_phase = FA_IOS_IDLE;
	radio->ios_table = kzalloc(FA_HEARD_MOST * sizeof(*radio->ios_table), GFP_KERNEL);
	radio->ios_candidates = kzalloc(FA_CANDIDATES_MOST * sizeof(*radio->ios_candidates), GFP_KERNEL);
	if (radio->ios_table == NULL || radio->ios_candidates == NULL) {
		return (-ENOMEM);
	}
	error = station_up(radio);
	if (error != 0) {
		lx_tell("FUXI ATH12K: M8 not proven: bring-up %d\n", error);
		fa_m4_evidence("bring-up failed");
		return (error);
	}
	regd_wait(radio, regd_before);
	if (fa_m4_trouble()) {
		return (-EIO);
	}
	radio->ios_up_at = lx_os_nanoseconds();
	error = lx_os_thread("fa-ios", ios_runs, radio);
	if (error != 0) {
		lx_tell("FUXI ATH12K: M8 no thread: %d\n", error);
		return (-ENOMEM);
	}
	lx_os_lock();
	ios_radio = radio;
	lx_os_wakeup(&ios_radio);
	lx_os_unlock();
	lx_tell("FUXI ATH12K: M8 radio up\n");
	return (0);
}

int
fa_ios_power(int on)
{
	struct fa_radio *radio = ios_wait();

	if (radio == NULL) {
		lx_tell("FUXI ATH12K: M8 power %d: the radio is not up\n", on);
		return (FA_IOS_NO_ANSWER);
	}
	if (on) {
		radio->ios_powered = true;
	} else {
		radio->ios_wifid_asked = true;
	}
	ios_gate_in(radio);
	if (!on) {
		auto_gives_way(radio, "the radio off");
		ios_frames_stop(radio);
		ios_clean(radio);
	}
	if (!radio->ios_dead) {
		ios_phase(radio, on ? FA_IOS_IDLE : FA_IOS_OFF, FA_IOS_DONE);
	}
	lx_tell("FUXI ATH12K: M8 power %d, firmware %s\n", on, radio->ios_dead ? "gone" : "up");
	ios_gate_out(radio);
	return (radio->ios_dead ? FA_IOS_RADIO_DEAD : FA_IOS_DONE);
}

/*
 * A scan for iOS. While joined nothing goes on the air: what the last scan heard is what there is. mac80211 would leave the
 * channel, and that has not been tried on this firmware: a list a little old costs less than a link dropped by a scan.
 */
int
fa_ios_scan(const unsigned char *ssid, unsigned ssid_length, const unsigned short *frequencies, unsigned frequency_count, int passive)
{
	struct fa_radio *radio = ios_wait();
	int error, answer = FA_IOS_DONE;

	if (radio == NULL) {
		return (FA_IOS_NO_ANSWER);
	}
	ios_gate_in(radio);
	if (radio->ios_dead || fa_m4_trouble()) {
		ios_gate_out(radio);
		return (FA_IOS_RADIO_DEAD);
	}
	if (radio->ios_joined) {
		lx_tell("FUXI ATH12K: M8 scan while joined: none, %u kept\n", radio->ios_table_count);
		ios_gate_out(radio);
		return (FA_IOS_DONE);
	}
	ios_phase(radio, FA_IOS_SCANNING, FA_IOS_DONE);
	memset(&radio->heard, 0, sizeof(radio->heard));
	lx_os_lock();
	radio->ios_table_count = 0;
	radio->ios_scan_name = ssid;
	radio->ios_scan_name_length = ssid_length > FA_SSID_MOST ? 0 : ssid_length;
	lx_os_unlock();
	target_name_length = 0;
	radio->asked.ssid_len = (uint8_t)radio->ios_scan_name_length;
	if (radio->ios_scan_name_length != 0) {
		memcpy(radio->asked.ssid, ssid, radio->ios_scan_name_length);
	}
	radio->scan_only = frequency_count != 0 ? frequencies : NULL;
	radio->scan_only_count = frequency_count;
	error = station_scan(radio, passive != 0);
	if (radio->scan == SCAN_RUNNING) {
		wiphy_lock(radio->wiphy);
		radio->ops->cancel_hw_scan(&radio->hw, radio->vif);
		wiphy_unlock(radio->wiphy);
		radio->scan = SCAN_NONE;
	}
	radio->scan_only = NULL;
	lx_os_lock();
	radio->ios_scan_name = NULL;
	radio->ios_scan_name_length = 0;
	lx_os_unlock();
	if (error != 0 && error != -EINVAL) {	/* (asked only for channels out of the domain: an empty list) */
		answer = fa_m4_trouble() ? FA_IOS_RADIO_DEAD : FA_IOS_NO_ANSWER;
	}
	lx_tell("FUXI ATH12K: M8 scan: %d, %u networks\n", error, radio->ios_table_count);
	ios_phase(radio, FA_IOS_IDLE, FA_IOS_DONE);
	ios_gate_out(radio);
	return (answer);
}

void
fa_ios_heard(void (*each)(const struct fa_heard *, void *), void *context)
{
	struct fa_radio *radio = ios_radio;
	unsigned long long now = lx_os_nanoseconds();

	for (unsigned i = 0; radio != NULL && i < radio->ios_table_count; i++) {
		struct fa_heard *one = &radio->ios_table[i];

		one->age_ms = (unsigned)((now - one->heard_at) / 1000000ull);
		each(one, context);
	}
}

/* What a join that did not come to anything amounted to, for wifid. */
static int
ios_why(struct fa_radio *radio)
{
	if (radio->protect && radio->reached >= IEEE80211_STA_ASSOC) {
		return (radio->eapol_heard != 0 || radio->answer == FA_ANSWER_GONE ? FA_IOS_NO_AUTH : FA_IOS_NO_ANSWER);
	}
	if (radio->answer == FA_ANSWER_NONE) {
		return (radio->chanctx != NULL ? FA_IOS_NO_ANSWER : FA_IOS_NOT_BUILT);
	}
	return (FA_IOS_REFUSED);
}

/* What one try at one access point leaves behind, so that the next starts from nothing. The name and the key of the join stay. */
static void
ios_join_counts_fresh(struct fa_radio *radio)
{
	lx_os_lock();
	radio->answer = FA_ANSWER_NONE;
	radio->answer_code = radio->reason = 0;
	radio->lost = radio->went_protected = false;
	radio->eapol_length = radio->eapol_heard = radio->eapol_lost = 0;
	radio->eapol_sent = radio->data_done = radio->data_acked = 0;
	radio->ba_want = 0;
	radio->ba_live = 0;
	radio->ios_pending &= IOS_DEAD;
	lx_os_unlock();
}

/*
 * The access points the scans heard, the best first, each checked as any other: its RSN element by handshake_prepare(). One that does not
 * come to anything is left as a join is left, and the next is tried: a channel with radar rules must not trap the join. Not after the
 * handshake has said no, for that is the key and it is the same key everywhere, nor when iOS's side has begun to wait for us.
 */
static int
ios_join_best(struct fa_radio *radio, bool protect, bool alone)
{
	struct fa_target *target = &radio->target;
	unsigned tries = 0;
	int answer = FA_IOS_NOT_BUILT;
	bool more;

	while (tries < FA_JOIN_CANDIDATES) {
		bool joined = false;
		int error;

		lx_os_lock();
		more = fa_target_next(target);
		lx_os_unlock();
		if (!more) {
			break;
		}
		lx_tell("FUXI ATH12K: M8 choice%s: %u MHz at %d dBm, score %d, of %u heard\n", tries != 0 ? ", falling back to the next best" : "", target->frequency, target->rssi,
		    fa_band_score(target->frequency, target->rssi), target->candidate_count);
		if (protect && !handshake_prepare(radio)) {
			continue;
		}
		tries++;
		error = station_join(radio, &joined);
		if (fa_m4_trouble() || error != 0) {
			return (FA_IOS_RADIO_DEAD);
		}
		if (joined) {
			return (FA_IOS_DONE);
		}
		answer = ios_why(radio);
		lx_tell("FUXI ATH12K: M8 join at %u MHz came to nothing: answer %d\n", target->frequency, answer);
		if (answer == FA_IOS_NO_AUTH || (alone && radio->ios_wifid_asked)) {
			break;
		}
		if (radio->chanctx != NULL) {
			station_leave(radio);
		}
		ios_join_counts_fresh(radio);
		fa_wipe(&radio->wpa, sizeof(radio->wpa));
		fa_wipe(radio->eapol, sizeof(radio->eapol));
	}
	return (answer);
}

/* A join, from iOS's side or from the auto join, which gives way if iOS's side has spoken in the meantime. */
static int
ios_join_run(struct fa_radio *radio, const unsigned char *ssid, unsigned ssid_length, const unsigned char *bssid, const unsigned char *key, bool alone)
{
	struct fa_target *target;
	int error = 0, answer = FA_IOS_DONE;

	ios_gate_in(radio);
	if (alone && radio->ios_wifid_asked) {
		lx_tell("FUXI ATH12K: M8 auto join: given up, wifid has spoken\n");
		ios_gate_out(radio);
		return (FA_IOS_REFUSED);
	}
	if (!alone) {
		auto_gives_way(radio, "a network");
	}
	if (radio->ios_dead || fa_m4_trouble()) {
		ios_gate_out(radio);
		return (FA_IOS_RADIO_DEAD);
	}
	ios_frames_stop(radio);
	ios_clean(radio);
	ios_phase(radio, FA_IOS_JOINING, FA_IOS_DONE);
	target = &radio->target;
	lx_os_lock();
	memset(target, 0, sizeof(*target));
	target->candidates = radio->ios_candidates;
	target->candidates_most = FA_CANDIDATES_MOST;
	target->wanted = key != NULL ? FA_PROTECT_RSN : FA_OPEN;
	if (bssid != NULL) {
		target->only_one = true;
		memcpy(target->only, bssid, sizeof(target->only));
	}
	radio->protect = key != NULL;
	radio->rx_ok = radio->rx_clear = radio->rx_replay = radio->rx_no_key = radio->rx_long = radio->net_sent = 0;
	radio->link_data = radio->link_beacons = radio->link_others = 0;
	radio->beacon_losses = 0;
	radio->door_seen = false;
	radio->door_other = radio->action_ciphered = radio->beacon_seq = 0;
	memset(radio->action, 0, sizeof(radio->action));
	memset(&radio->tx, 0, sizeof(radio->tx));
	radio->tx_filtered = 0;
	memset(radio->tx_then, 0, sizeof(radio->tx_then));
	memset(radio->tx_then_at, 0, sizeof(radio->tx_then_at));
	lx_os_unlock();
	ios_join_counts_fresh(radio);
	memset(&radio->heard, 0, sizeof(radio->heard));
	fa_set_target_ssid(ssid, ssid_length);
	if (target_name_length == 0) {
		ios_phase(radio, FA_IOS_IDLE, FA_IOS_NOT_HEARD);
		ios_gate_out(radio);
		return (FA_IOS_NOT_HEARD);
	}
	target_protected = key != NULL;
	if (key != NULL) {
		memcpy(target_key, key, sizeof(target_key));
	}
	lx_tell("FUXI ATH12K: M8 join: name of %u bytes, %s, %s access point\n", target_name_length, key != NULL ? "a key" : "open", bssid != NULL ? "one" : "any");
	radio->asked.ssid_len = (uint8_t)target_name_length;
	memcpy(radio->asked.ssid, target_name, target_name_length);
	error = join_find(radio, false);
	if (radio->scan == SCAN_RUNNING) {
		wiphy_lock(radio->wiphy);
		radio->ops->cancel_hw_scan(&radio->hw, radio->vif);
		wiphy_unlock(radio->wiphy);
		radio->scan = SCAN_NONE;
	}
	if (fa_m4_trouble()) {
		answer = FA_IOS_RADIO_DEAD;
	} else if (target->protected_frames != 0) {
		lx_tell("FUXI ATH12K: M8 refused: the target has %s (%u frames)\n", fa_protection_name(target->why), target->protected_frames);
		answer = FA_IOS_NOT_BUILT;
	} else if (!target->found) {
		lx_tell("FUXI ATH12K: M8 not heard in %u scans: %d\n", FA_JOIN_SCANS, error);
		answer = FA_IOS_NOT_HEARD;
	} else {
		answer = ios_join_best(radio, key != NULL, alone);
	}
	fa_wipe(target_key, sizeof(target_key));
	if (answer != FA_IOS_DONE) {
		lx_tell("FUXI ATH12K: M8 join over: answer %d\n", answer);
		if (answer == FA_IOS_RADIO_DEAD) {
			ios_dies(radio);
		} else {
			ios_clean(radio);
			ios_phase(radio, FA_IOS_IDLE, radio->ios_rejoining ? FA_IOS_DONE : answer);
		}
		ios_gate_out(radio);
		return (answer);
	}
	lx_os_lock();
	memset(&radio->ios_bss, 0, sizeof(radio->ios_bss));
	memcpy(radio->ios_bss.bssid, target->address, sizeof(radio->ios_bss.bssid));
	memcpy(radio->ios_bss.ssid, target_name, target_name_length);
	radio->ios_bss.ssid_length = (unsigned char)target_name_length;
	radio->ios_bss.frequency = target->frequency;
	radio->ios_bss.interval = target->interval;
	radio->ios_bss.capability = target->capability;
	radio->ios_bss.elements_length = (unsigned short)(target->elements_length < sizeof(radio->ios_bss.elements) ? target->elements_length : sizeof(radio->ios_bss.elements));
	memcpy(radio->ios_bss.elements, target->elements, radio->ios_bss.elements_length);
	memset(&radio->group_pn, 0, sizeof(radio->group_pn));
	if (radio->protect) {
		radio->group_pn.last[radio->wpa.group_index] = fa_rsc_read(radio->wpa.group_counter);
		radio->group_pn.known[radio->wpa.group_index] = true;
	}
	radio->ios_phase = FA_IOS_JOINED;
	radio->ios_last_answer = FA_IOS_DONE;
	radio->ios_joined = true;		/* the keys are in: station_keys() said so */
	radio->ios_said = lx_os_nanoseconds();
	radio->ios_joined_at = radio->ios_said;
	radio->rssi = target->rssi;
	if (radio->answer == FA_ANSWER_GONE || radio->lost) {
		/* Its word came before frames passed, and nothing else would hear of it. */
		radio->ios_pending |= IOS_LOSS;
		lx_os_wakeup(&radio->ios_pending);
	}
	lx_os_unlock();
	if (alone && key != NULL) {
		radio->ios_auto_joined = true;
		radio->ios_rejoining = false;
		radio->rejoin_tries = 0;
		memmove(rejoin_name, ssid, ssid_length);
		rejoin_name_length = ssid_length;
		memcpy(rejoin_key, key, sizeof(rejoin_key));
	}
	lx_tell("FUXI ATH12K: M8 joined %u MHz at %d dBm, %u MHz wide\n", target->frequency, target->rssi, radio->width.mhz);
	ios_gate_out(radio);
	ios_input.link(1);
	return (FA_IOS_DONE);
}

int
fa_ios_join(const unsigned char *ssid, unsigned ssid_length, const unsigned char *bssid, const unsigned char *key)
{
	struct fa_radio *radio = ios_wait();

	if (radio == NULL) {
		return (FA_IOS_NO_ANSWER);
	}
	radio->ios_wifid_asked = true;
	return (ios_join_run(radio, ssid, ssid_length, bssid, key, false));
}

int
fa_ios_leave(void)
{
	struct fa_radio *radio = ios_radio;

	if (radio == NULL) {
		return (FA_IOS_DONE);
	}
	radio->ios_wifid_asked = true;
	ios_gate_in(radio);
	auto_gives_way(radio, "no network");
	ios_frames_stop(radio);
	ios_clean(radio);
	if (!radio->ios_dead) {
		ios_phase(radio, FA_IOS_IDLE, FA_IOS_DONE);
	}
	lx_tell("FUXI ATH12K: M8 left when asked\n");
	ios_gate_out(radio);
	return (FA_IOS_DONE);
}

void
fa_ios_state(struct fa_ios_state *state)
{
	struct fa_radio *radio = ios_radio;

	memset(state, 0, sizeof(*state));
	state->phase = FA_IOS_IDLE;
	if (radio == NULL) {
		return;
	}
	lx_os_lock();
	state->phase = radio->ios_phase;
	state->protect = radio->protect;
	state->last_answer = radio->ios_last_answer;
	if (radio->ios_phase == FA_IOS_JOINED) {
		state->bss = radio->ios_bss;
		state->bss.rssi = (short)radio->rssi;
	}
	lx_os_unlock();
}

/* An Ethernet frame from iOS. ath12k's own TX does not sleep (mac80211 calls it with bottom halves off). Not its handshake's: those are ours. */
int
fa_ios_tx(const unsigned char *frame, unsigned long length)
{
	struct fa_radio *radio = ios_radio;
	unsigned type;
	bool sent, filtered;

	if (radio == NULL || length < 14 || length > 14 + FA_NET_MOST) {
		return (0);
	}
	type = fa_be16(frame + 12);
	if (type < 0x0600 || type == 0x888e) {
		return (0);
	}
	filtered = fa_tx_filtered(tx_filter, frame, (unsigned)length);
	lx_os_lock();
	if (!radio->ios_joined || memcmp(frame + 6, radio->vif->addr, ETH_ALEN) != 0) {
		lx_os_unlock();
		return (0);
	}
	if (filtered) {
		radio->tx_filtered++;
		lx_os_unlock();
		return (1);		/* as if sent: nothing is to tell iOS that it was not */
	}
	radio->ios_tx_busy++;
	fa_tx_note(&radio->tx, type, frame + 14, (unsigned)length - 14);
	lx_os_unlock();
	sent = net_send(radio, frame, type, frame + 14, (unsigned)length - 14);
	lx_os_lock();
	radio->ios_tx_busy--;
	lx_os_wakeup(&radio->ios_tx_busy);
	lx_os_unlock();
	return (sent);
}

/* Design section 2: recovery is not supported at first. The dump is all that follows. */
void
linuxkpi_ieee80211_restart_hw(struct ieee80211_hw *hw)
{
	lx_tell("FUXI ATH12K: the driver asks for a hardware restart: not supported, forcing RDDM\n");
	fuxi_ath12k_force_rddm();
}
