/*
 * The ath12k radio as iOS's Wi-Fi backend (fuxiwifiios=2), in XNU's domain: struct fuxi_wifi_backend, whose types the
 * driver domain does not know, forwarded to fa_xnu.h's plain C. A passphrase ends here: the key is made of it, it is wiped, and
 * the driver takes only the key. Nothing here says a name, an address or a key. Patch 0117 carries this file to
 * bsd/dev/fuxi_ath12k_ios.c, so that only kernel-metal4wifi has it.
 *
 * SPDX-License-Identifier: MIT
 */
#if defined(FUXI)
#include <sys/param.h>
#include <sys/systm.h>
#include <sys/errno.h>
#include <sys/fuxi_wifi_ios.h>
#include <sys/fuxi_wifi_wpa.h>
#include "fa_xnu.h"

_Static_assert((int)FUXI_WIFI_STA_OFF == FA_IOS_OFF && (int)FUXI_WIFI_STA_IDLE == FA_IOS_IDLE && (int)FUXI_WIFI_STA_SCANNING == FA_IOS_SCANNING &&
    (int)FUXI_WIFI_STA_JOINING == FA_IOS_JOINING && (int)FUXI_WIFI_STA_JOINED == FA_IOS_JOINED, "the phases are the same numbers");

const struct fuxi_wifi_backend *fuxi_ath12k_ios_backend(void);

static const struct fuxi_wifi_input *ios_input;

/* The two domains' errno differ: FreeBSD's EOPNOTSUPP is not XNU's. */
static int
ios_errno(int answer)
{
	switch (answer) {
	case FA_IOS_DONE:
		return 0;
	case FA_IOS_NO_AUTH:
		return EAUTH;
	case FA_IOS_NO_ANSWER:
		return ETIMEDOUT;
	case FA_IOS_NOT_HEARD:
		return ENOENT;
	case FA_IOS_REFUSED:
		return ECONNREFUSED;
	case FA_IOS_NOT_BUILT:
		return EOPNOTSUPP;
	case FA_IOS_NO_MEMORY:
		return ENOMEM;
	case FA_IOS_GONE:
		return ECONNRESET;
	case FA_IOS_RADIO_DEAD:
		return ENETDOWN;
	default:
		return EIO;
	}
}

static void
ios_rx(const unsigned char *frame, unsigned long length)
{
	ios_input->rx_frame(frame, length);
}

static void
ios_link(int up)
{
	ios_input->link_state(up != 0);
}

static void
ios_lost(int answer)
{
	fuxi_wifi_sta_lost(ios_errno(answer));
}

static void
ios_start(const struct fuxi_wifi_input *input)
{
	static const struct fa_ios_input driver = { .rx = ios_rx, .link = ios_link, .lost = ios_lost };

	ios_input = input;
	fa_ios_start(&driver);
}

static int
ios_power(bool on)
{
	return ios_errno(fa_ios_power(on));
}

static int
ios_scan(const struct fuxi_wifi_sta_scan *what)
{
	return ios_errno(fa_ios_scan(what->ssid, what->ssid_length, what->frequencies, what->frequency_count, what->passive));
}

static void (*ios_each)(const struct fuxi_wifi_sta_bss *, void *);

/* From a table of the driver's to the shape iOS's side reads. Called on the one station thread: `bss` can be kept between calls. */
static void
ios_copy(const struct fa_heard *from, struct fuxi_wifi_sta_bss *to)
{
	bzero(to, sizeof(*to));
	memcpy(to->bssid, from->bssid, sizeof(to->bssid));
	memcpy(to->ssid, from->ssid, sizeof(to->ssid));
	to->ssid_length = from->ssid_length;
	to->frequency = from->frequency;
	to->rssi = from->rssi;
	to->beacon_interval = from->interval;
	to->capability = from->capability;
	to->age_ms = from->age_ms;
	to->elements_length = (uint16_t)(from->elements_length < sizeof(to->elements) ? from->elements_length : sizeof(to->elements));
	memcpy(to->elements, from->elements, to->elements_length);
}

static void
ios_one(const struct fa_heard *from, void *context)
{
	static struct fuxi_wifi_sta_bss bss;

	ios_copy(from, &bss);
	ios_each(&bss, context);
}

static void
ios_heard(void (*each)(const struct fuxi_wifi_sta_bss *, void *), void *context)
{
	ios_each = each;
	fa_ios_heard(ios_one, context);
}

static int
ios_join(const struct fuxi_wifi_sta_join *what)
{
	static const uint8_t none[6];
	unsigned char key[32];
	int answer;

	if (what->security == FUXI_WIFI_STA_OPEN) {
		answer = fa_ios_join(what->ssid, what->ssid_length, memcmp(what->bssid, none, sizeof(none)) != 0 ? what->bssid : NULL, NULL);
		return ios_errno(answer);
	}
	if (what->security != FUXI_WIFI_STA_WPA2_PSK) {
		return EOPNOTSUPP;
	}
	if (what->passphrase_length != 0) {
		fuxi_wifi_wpa_pmk(what->passphrase, what->passphrase_length, what->ssid, what->ssid_length, key);
	} else {
		memcpy(key, what->pmk, sizeof(key));
	}
	answer = fa_ios_join(what->ssid, what->ssid_length, memcmp(what->bssid, none, sizeof(none)) != 0 ? what->bssid : NULL, key);
	bzero(key, sizeof(key));
	return ios_errno(answer);
}

static int
ios_leave(void)
{
	return ios_errno(fa_ios_leave());
}

static void
ios_state(struct fuxi_wifi_sta_state *state)
{
	static struct fa_ios_state from;	/* (its bytes are a beacon's: nothing secret, and the driver's lock guards the copy) */

	fa_ios_state(&from);
	bzero(state, sizeof(*state));
	state->phase = (enum fuxi_wifi_sta_phase)from.phase;
	state->security = from.protect ? FUXI_WIFI_STA_WPA2_PSK : FUXI_WIFI_STA_OPEN;
	state->last_error = ios_errno(from.last_answer);
	if (from.phase == FA_IOS_JOINED) {
		ios_copy(&from.bss, &state->bss);
	}
}

/*
 * What the radio may tune to, asked only at attach, before the radio is up: the CN domain the firmware has given in every
 * run so far (w-m2), on the phone's two bands. A channel out of the domain is left out of the scan by the driver anyway.
 */
static unsigned
ios_channels(struct fuxi_wifi_sta_channel *out, unsigned most)
{
	static const struct fuxi_wifi_sta_channel known[] = {
		{ 2412, 0 }, { 2417, 0 }, { 2422, 0 }, { 2427, 0 }, { 2432, 0 }, { 2437, 0 }, { 2442, 0 }, { 2447, 0 }, { 2452, 0 }, { 2457, 0 }, { 2462, 0 }, { 2467, 0 }, { 2472, 0 },
		{ 5180, 0 }, { 5200, 0 }, { 5220, 0 }, { 5240, 0 },
		{ 5260, FUXI_WIFI_STA_CHANNEL_PASSIVE | FUXI_WIFI_STA_CHANNEL_RADAR }, { 5280, FUXI_WIFI_STA_CHANNEL_PASSIVE | FUXI_WIFI_STA_CHANNEL_RADAR },
		{ 5300, FUXI_WIFI_STA_CHANNEL_PASSIVE | FUXI_WIFI_STA_CHANNEL_RADAR }, { 5320, FUXI_WIFI_STA_CHANNEL_PASSIVE | FUXI_WIFI_STA_CHANNEL_RADAR },
		{ 5745, 0 }, { 5765, 0 }, { 5785, 0 }, { 5805, 0 }, { 5825, 0 },
	};
	unsigned count = sizeof(known) / sizeof(known[0]);

	count = count < most ? count : most;
	memcpy(out, known, count * sizeof(*out));
	return count;
}

static bool
ios_tx(const uint8_t *frame, size_t length)
{
	return fa_ios_tx(frame, length) != 0;
}

static struct fuxi_wifi_backend ios_backend = {
	.station = { .power = ios_power, .scan = ios_scan, .heard = ios_heard, .join = ios_join, .leave = ios_leave,
		.state = ios_state, .channels = ios_channels },
	.tx_frame = ios_tx, .start = ios_start,
};

/* From fuxi_wifi_ios_init(), before the root is there and long before the radio: the address is the one the radio will be given. */
const struct fuxi_wifi_backend *
fuxi_ath12k_ios_backend(void)
{
	fuxi_ath12k_station_address(ios_backend.station.address);
	return &ios_backend;
}
#endif /* FUXI */
