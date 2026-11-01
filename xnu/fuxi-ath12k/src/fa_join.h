/*
 * The frames of joining a network, and how the answers are read off the air. Stage 5 joins open networks only: an
 * element that asks for protection ends the join before anything is sent to the access point. Stage 6 joins WPA2 with
 * a key only, and the absence of its RSN element ends the join the same way; what is here reads and builds frames and
 * holds no key. Nothing in a received frame is trusted: every length
 * is checked against the frame. Like fa_bss.h this needs only <stdint.h>, <stdbool.h> and memcpy(), and
 * tools/test-fuxi-ath12k.py runs it on malformed frames under the sanitizers.
 * Frame layout: IEEE 802.11-2020, 9.3.3 and 9.4.1; elements: 9.4.2.
 *
 * Adapted from management(), management_received(), station_abilities(), request_elements() and protection() of
 * hackios wifi/lx/src/lx_80211.c (MIT), see compat/PROVENANCE.md: reduced to open networks, kept apart from the
 * driver, the HE element added, every answer checked against both addresses.
 *
 * Include fa_bss.h first.
 *
 * SPDX-License-Identifier: MIT
 */
#ifndef _FA_JOIN_H_
#define _FA_JOIN_H_

#define FA_KIND_ASSOC_REQUEST	0x00
#define FA_KIND_ASSOC_RESPONSE	0x10
#define FA_KIND_PROBE_RESPONSE	0x50
#define FA_KIND_BEACON		0x80
#define FA_KIND_DISASSOC	0xa0
#define FA_KIND_AUTH		0xb0
#define FA_KIND_DEAUTH		0xc0

#define FA_CAP_ESS		0x0001
#define FA_CAP_PRIVACY		0x0010
#define FA_CAP_SHORT_PREAMBLE	0x0020
#define FA_CAP_SPECTRUM		0x0100
#define FA_CAP_SHORT_SLOT	0x0400

#define FA_EID_DS		3
#define FA_EID_TIM		5
#define FA_EID_POWER_CAP	33
#define FA_EID_CHANNELS		36
#define FA_EID_HT		45
#define FA_EID_RSN		48
#define FA_EID_HT_OPERATION	61
#define FA_EID_WAPI		68
#define FA_EID_VHT		191
#define FA_EID_VENDOR		221
#define FA_EID_EXTENSION	255
#define FA_EXT_HE		35
#define FA_EXT_HE_OPERATION	36

#define FA_HT_BODY		26
#define FA_VHT_BODY		12
#define FA_HT_CAP_40		0x0002		/* supported channel width: 20 and 40 MHz */
#define FA_HT_CAP_SGI_40	0x0040
#define FA_VHT_CAP_WIDTH	0x0000000cu	/* 160 and 80+80 MHz */
#define FA_VHT_CAP_SGI_160	0x00000040u
#define FA_HE_MAC		6
#define FA_HE_PHY		11
#define FA_HE_PHY0_WIDTH	0xfe		/* the channel width set: nothing set is 20 MHz only */
#define FA_HE_PHY0_160		0x08
#define FA_HE_PHY0_80_80	0x10
#define FA_HE_PHY6_PPE		0x80		/* PPE thresholds follow the MCS sets */
#define FA_HE_PPE_MOST		25
#define FA_HE_OFFER_MOST	(2 + 1 + FA_HE_MAC + FA_HE_PHY + 4)

#define FA_AUTH_LENGTH		30
#define FA_DEAUTH_LENGTH	26
#define FA_REASON_LEAVING	3
#define FA_LISTEN_INTERVAL	10
#define FA_AID_MOST		2007		/* 9.4.1.8 */
#define FA_RATES_MOST		16
#define FA_TARGET_ELEMENTS_MOST	1536

/* Whether `length` bytes of elements end exactly at the end of the last one. */
static inline bool
fa_elements_whole(const uint8_t *elements, unsigned length)
{
	unsigned at = 0;

	while (length - at >= 2 && length - at - 2 >= elements[at + 1]) {
		at += 2u + elements[at + 1];
	}
	return (at == length);
}

/* The whole extension element of that number, or NULL. Same walk as fa_element(). */
static inline const uint8_t *
fa_ext_element(const uint8_t *elements, unsigned length, unsigned extension)
{
	unsigned at = 0;

	while (length - at >= 2 && length - at - 2 >= elements[at + 1]) {
		if (elements[at] == FA_EID_EXTENSION && elements[at + 1] >= 1 && elements[at + 2] == extension) {
			return (elements + at);
		}
		at += 2u + elements[at + 1];
	}
	return (NULL);
}

/* The whole vendor element that begins with these three bytes and this type, or NULL. */
static inline const uint8_t *
fa_vendor_element(const uint8_t *elements, unsigned length, uint8_t a, uint8_t b, uint8_t c, uint8_t type)
{
	unsigned at = 0;

	while (length - at >= 2 && length - at - 2 >= elements[at + 1]) {
		const uint8_t *e = elements + at;

		if (e[0] == FA_EID_VENDOR && e[1] >= 4 && e[2] == a && e[3] == b && e[4] == c && e[5] == type) {
			return (e);
		}
		at += 2u + e[1];
	}
	return (NULL);
}

#define fa_wmm_element(elements, length)	fa_vendor_element(elements, length, 0x00, 0x50, 0xf2, 0x02)

enum fa_protection { FA_OPEN, FA_PROTECT_RSN, FA_PROTECT_WPA, FA_PROTECT_WAPI, FA_PROTECT_PRIVACY, FA_PROTECT_UNREADABLE };

/*
 * What a network's beacon or probe response asks for, in the order that says the most. Elements that cannot be
 * read to their end count as protection: what is past a lying element cannot be told from an RSN element.
 */
static inline enum fa_protection
fa_protection(unsigned capability, const uint8_t *elements, unsigned length)
{
	if (fa_element(elements, length, FA_EID_RSN) != NULL) {
		return (FA_PROTECT_RSN);
	}
	if (fa_vendor_element(elements, length, 0x00, 0x50, 0xf2, 0x01) != NULL) {
		return (FA_PROTECT_WPA);
	}
	if (fa_element(elements, length, FA_EID_WAPI) != NULL) {
		return (FA_PROTECT_WAPI);
	}
	if (!fa_elements_whole(elements, length)) {
		return (FA_PROTECT_UNREADABLE);
	}
	return ((capability & FA_CAP_PRIVACY) != 0 ? FA_PROTECT_PRIVACY : FA_OPEN);
}

static inline const char *
fa_protection_name(enum fa_protection protection)
{
	switch (protection) {
	case FA_OPEN:
		return ("nothing");
	case FA_PROTECT_RSN:
		return ("an RSN element");
	case FA_PROTECT_WPA:
		return ("a WPA element");
	case FA_PROTECT_WAPI:
		return ("a WAPI element");
	case FA_PROTECT_PRIVACY:
		return ("the privacy bit");
	default:
		return ("elements that cannot be read to their end");
	}
}

/* What stage 6 asks in its association request: RSN version 1, CCMP for the group and for us, a key from a passphrase, no PMF. */
#define FA_RSN_LENGTH		22
static const uint8_t FA_RSN_OURS[FA_RSN_LENGTH] = { FA_EID_RSN, 20, 1, 0, 0x00, 0x0f, 0xac, 4, 1, 0, 0x00, 0x0f, 0xac, 4, 1, 0, 0x00, 0x0f, 0xac, 2, 0, 0 };

/* What an RSN element offers: a bit for each suite of the standard's own OUI, numbered as the standard numbers them. */
struct fa_rsn {
	unsigned	group;
	uint32_t	pairwise, akm;
	unsigned	capabilities;
};

#define FA_RSN_CCMP		4
#define FA_RSN_AKM_PSK		2
#define FA_RSN_MFP_REQUIRED	0x0040

static inline uint32_t
fa_rsn_suites(const uint8_t *at, unsigned count)
{
	uint32_t bits = 0;

	for (unsigned i = 0; i < count; i++, at += 4) {
		if (at[0] == 0x00 && at[1] == 0x0f && at[2] == 0xac && at[3] < 32) {
			bits |= 1u << at[3];
		}
	}
	return (bits);
}

/*
 * Whether we can join what this network's RSN element offers. NULL if so; else what stands in the way. Whatever could
 * be read is in *rsn, for the log: when a network is refused its offer is the first thing to know.
 */
static inline const char *
fa_rsn_read(const uint8_t *elements, unsigned length, struct fa_rsn *rsn)
{
	const uint8_t *e = fa_element(elements, length, FA_EID_RSN);
	unsigned at, end, count;

	memset(rsn, 0, sizeof(*rsn));
	if (e == NULL) {
		return ("it has no RSN element");
	}
	at = 2;
	end = 2u + e[1];
	if (end < at + 8 || e[2] != 1 || e[3] != 0 || e[4] != 0x00 || e[5] != 0x0f || e[6] != 0xac) {
		return ("its RSN element is not one this reads");
	}
	rsn->group = e[7];
	count = e[8] | (unsigned)e[9] << 8;
	at += 8;
	if (end - at < 4 * count + 2) {
		return ("its list of ciphers does not fit its RSN element");
	}
	rsn->pairwise = fa_rsn_suites(e + at, count);
	at += 4 * count;
	count = e[at] | (unsigned)e[at + 1] << 8;
	at += 2;
	if (end - at < 4 * count) {
		return ("its list of key management does not fit its RSN element");
	}
	rsn->akm = fa_rsn_suites(e + at, count);
	at += 4 * count;
	if (end - at >= 2) {
		rsn->capabilities = e[at] | (unsigned)e[at + 1] << 8;
	}
	if (rsn->group != FA_RSN_CCMP) {
		return ("its group cipher is not CCMP");
	}
	if ((rsn->pairwise & 1u << FA_RSN_CCMP) == 0) {
		return ("it does not offer CCMP");
	}
	if ((rsn->akm & 1u << FA_RSN_AKM_PSK) == 0) {
		return ("it does not take a WPA2 passphrase's key (WPA3 only, or an enterprise network)");
	}
	if ((rsn->capabilities & FA_RSN_MFP_REQUIRED) != 0) {
		return ("it requires protected management frames");
	}
	return (NULL);
}

/* ---- the network that is wanted, from what a scan hears ------------------------------------------------- */

struct fa_target {
	bool		found;
	bool		fixed;			/* the join has chosen: only this access point's frames count now */
	enum fa_protection wanted;		/* FA_OPEN (stage 5), or FA_PROTECT_RSN (stage 6) */
	bool		only_one;		/* stage 8: only this access point was asked for ... */
	uint8_t		only[6];		/* ... this one */
	uint8_t		address[6];		/* BSSID, never said in the log */
	uint16_t	frequency;		/* MHz, where it was heard at its strongest */
	uint16_t	capability, interval;
	int8_t		rssi;
	unsigned	heard;
	unsigned	protected_frames;	/* frames of this name that asked for other than `wanted`: any one refuses the join */
	enum fa_protection why;			/* the first of them */
	unsigned	elements_length;
	uint8_t		elements[FA_TARGET_ELEMENTS_MOST];
};

enum fa_target_result { FA_TARGET_OTHER, FA_TARGET_ELSEWHERE, FA_TARGET_MALFORMED, FA_TARGET_TAKEN, FA_TARGET_PROTECTED };

/* How many of the elements fit in `most` bytes, cut at a whole element. */
static inline unsigned
fa_elements_cut(const uint8_t *elements, unsigned length, unsigned most)
{
	unsigned at = 0;

	while (length - at >= 2 && length - at - 2 >= elements[at + 1] && at + 2u + elements[at + 1] <= most) {
		at += 2u + elements[at + 1];
	}
	return (at);
}

/*
 * One received frame. A beacon or probe response of exactly this name is the wanted network, and the strongest
 * hearing is the one to join; one that asks for protection is noted and sticks, whichever access point it came
 * from and however open the others look. A hidden network's beacon is not of this name, its probe response is.
 * Once `fixed`, only that access point counts, and only whether it has begun to ask for protection: not its name,
 * which its beacon may hide, nor its elements, which the join has already acted on.
 */
static inline enum fa_target_result
fa_target_take(struct fa_target *t, const uint8_t *frame, unsigned length, unsigned frequency, int rssi, const uint8_t *name, unsigned name_length)
{
	const uint8_t *bssid, *elements, *ssid;
	unsigned header = FA_FRAME_HEADER, elements_length, capability;
	enum fa_protection protection;

	if (length < 2 || (frame[0] != FA_KIND_BEACON && frame[0] != FA_KIND_PROBE_RESPONSE)) {
		return (FA_TARGET_OTHER);
	}
	header += (frame[1] & 0x80) != 0 ? FA_FRAME_HT_CONTROL : 0;
	if (length < header + FA_FRAME_FIXED || (frame[1] & 0x40) != 0 || frequency == 0 || frequency > 0xffff) {
		return (FA_TARGET_MALFORMED);
	}
	bssid = frame + 16;
	if ((bssid[0] & 1) != 0 || (bssid[0] | bssid[1] | bssid[2] | bssid[3] | bssid[4] | bssid[5]) == 0) {
		return (FA_TARGET_MALFORMED);
	}
	elements = frame + header + FA_FRAME_FIXED;
	elements_length = length - header - FA_FRAME_FIXED;
	capability = frame[header + 10] | (unsigned)frame[header + 11] << 8;
	protection = fa_protection(capability, elements, elements_length);
	if (t->fixed) {
		if (memcmp(bssid, t->address, 6) != 0) {
			return (FA_TARGET_ELSEWHERE);
		}
		if (protection == t->wanted) {
			return (FA_TARGET_TAKEN);
		}
	} else {
		if (t->only_one && memcmp(bssid, t->only, 6) != 0) {
			return (FA_TARGET_ELSEWHERE);
		}
		ssid = fa_element(elements, elements_length, FA_EID_SSID);
		if (ssid == NULL) {
			/* Only a frame that cannot be read at all: without its name it may still be of this network. */
			return (protection == FA_PROTECT_UNREADABLE ? FA_TARGET_MALFORMED : FA_TARGET_ELSEWHERE);
		}
		if (ssid[1] != name_length || memcmp(ssid + 2, name, name_length) != 0) {
			return (FA_TARGET_ELSEWHERE);
		}
	}
	if (protection != t->wanted) {
		t->why = t->protected_frames == 0 ? protection : t->why;
		t->protected_frames++;
		return (FA_TARGET_PROTECTED);
	}
	rssi = rssi < FA_RSSI_NONE ? FA_RSSI_NONE : rssi > 127 ? 127 : rssi;
	t->heard++;
	if (!t->found || rssi > t->rssi) {
		t->found = true;
		memcpy(t->address, bssid, 6);
		t->frequency = (uint16_t)frequency;
		t->rssi = (int8_t)rssi;
		t->interval = (uint16_t)(frame[header + 8] | frame[header + 9] << 8);
		t->capability = (uint16_t)capability;
		t->elements_length = fa_elements_cut(elements, elements_length, sizeof(t->elements));
		memcpy(t->elements, elements, t->elements_length);
	}
	return (FA_TARGET_TAKEN);
}

/* ---- what we send ---------------------------------------------------------------------------------------- */

static inline void
fa_frame_header(uint8_t *out, uint8_t kind, const uint8_t *own, const uint8_t *bssid)
{
	memset(out, 0, FA_FRAME_HEADER);
	out[0] = kind;
	memcpy(out + 4, bssid, 6);
	memcpy(out + 10, own, 6);
	memcpy(out + 16, bssid, 6);
}

/* Open System authentication, the first of its two steps. The number of bytes, 0 when they do not fit. */
static inline unsigned
fa_auth_request(uint8_t *out, unsigned size, const uint8_t *own, const uint8_t *bssid)
{
	if (size < FA_AUTH_LENGTH) {
		return (0);
	}
	fa_frame_header(out, FA_KIND_AUTH, own, bssid);
	memset(out + FA_FRAME_HEADER, 0, 6);
	out[FA_FRAME_HEADER + 2] = 1;
	return (FA_AUTH_LENGTH);
}

static inline unsigned
fa_deauth(uint8_t *out, unsigned size, const uint8_t *own, const uint8_t *bssid, unsigned reason)
{
	if (size < FA_DEAUTH_LENGTH) {
		return (0);
	}
	fa_frame_header(out, FA_KIND_DEAUTH, own, bssid);
	out[FA_FRAME_HEADER] = (uint8_t)reason;
	out[FA_FRAME_HEADER + 1] = (uint8_t)(reason >> 8);
	return (FA_DEAUTH_LENGTH);
}

/* What the association request offers, each part already in the standard's bytes. */
struct fa_offer {
	uint16_t	capability;
	const uint8_t	*ssid;
	unsigned	ssid_length;
	uint8_t		rates[FA_RATES_MOST];	/* in units of 500 kb/s, the top bit for a basic rate */
	unsigned	rate_count;
	bool		spectrum;		/* the access point uses 802.11h: power and channels are said */
	uint8_t		power_most, channel;
	bool		has_ht, has_vht, wmm;
	const uint8_t	*rsn;			/* a whole element, stage 6 only */
	unsigned	rsn_length;
	uint8_t		ht[FA_HT_BODY];
	uint8_t		vht[FA_VHT_BODY];
	uint8_t		he[FA_HE_OFFER_MOST];	/* a whole element from fa_he_offer() */
	unsigned	he_length;
};

/*
 * The association request. Elements in the order of Table 9-35, vendor elements last. An RSN element only when one is
 * offered. No EHT element (design section 9). The number of bytes, 0 when they do not
 * fit or when what is offered is not something a station may ask with.
 */
static inline unsigned
fa_assoc_request(uint8_t *out, unsigned size, const uint8_t *own, const uint8_t *bssid, const struct fa_offer *offer)
{
	static const uint8_t wmm[9] = { FA_EID_VENDOR, 7, 0x00, 0x50, 0xf2, 0x02, 0x00, 0x01, 0x00 };
	unsigned in_one = offer->rate_count < FA_RATES_IN_ONE ? offer->rate_count : FA_RATES_IN_ONE;
	unsigned more = offer->rate_count - in_one;
	unsigned need = FA_FRAME_HEADER + 4u + 2 + offer->ssid_length + 2 + in_one + (more != 0 ? 2 + more : 0) +
	    (offer->spectrum ? 8 : 0) + offer->rsn_length + (offer->has_ht ? 2 + FA_HT_BODY : 0) + (offer->has_vht ? 2 + FA_VHT_BODY : 0) +
	    offer->he_length + (offer->wmm ? sizeof(wmm) : 0);
	unsigned at = FA_FRAME_HEADER;

	if (size < need || offer->ssid_length == 0 || offer->ssid_length > FA_SSID_MOST || offer->rate_count == 0 ||
	    offer->rate_count > FA_RATES_MOST || offer->he_length > FA_HE_OFFER_MOST) {
		return (0);
	}
	fa_frame_header(out, FA_KIND_ASSOC_REQUEST, own, bssid);
	out[at++] = (uint8_t)offer->capability;
	out[at++] = (uint8_t)(offer->capability >> 8);
	out[at++] = FA_LISTEN_INTERVAL;
	out[at++] = 0;
	out[at++] = FA_EID_SSID;
	out[at++] = (uint8_t)offer->ssid_length;
	memcpy(out + at, offer->ssid, offer->ssid_length);
	at += offer->ssid_length;
	out[at++] = FA_EID_RATES;
	out[at++] = (uint8_t)in_one;
	memcpy(out + at, offer->rates, in_one);
	at += in_one;
	if (more != 0) {
		out[at++] = FA_EID_RATES_MORE;
		out[at++] = (uint8_t)more;
		memcpy(out + at, offer->rates + in_one, more);
		at += more;
	}
	if (offer->spectrum) {
		out[at++] = FA_EID_POWER_CAP;
		out[at++] = 2;
		out[at++] = 0;
		out[at++] = offer->power_most;
		out[at++] = FA_EID_CHANNELS;
		out[at++] = 2;
		out[at++] = offer->channel;
		out[at++] = 1;
	}
	if (offer->rsn_length != 0) {
		memcpy(out + at, offer->rsn, offer->rsn_length);
		at += offer->rsn_length;
	}
	if (offer->has_ht) {
		out[at++] = FA_EID_HT;
		out[at++] = FA_HT_BODY;
		memcpy(out + at, offer->ht, FA_HT_BODY);
		at += FA_HT_BODY;
	}
	if (offer->has_vht) {
		out[at++] = FA_EID_VHT;
		out[at++] = FA_VHT_BODY;
		memcpy(out + at, offer->vht, FA_VHT_BODY);
		at += FA_VHT_BODY;
	}
	memcpy(out + at, offer->he, offer->he_length);
	at += offer->he_length;
	if (offer->wmm) {
		memcpy(out + at, wmm, sizeof(wmm));
		at += sizeof(wmm);
	}
	return (at);
}

/*
 * Our HE capabilities for a 20 MHz channel, a whole element: the radio's MAC and PHY bytes with the channel width set
 * empty and without PPE thresholds, and the one MCS set that is always there. The number of bytes, 0 when they do not fit.
 */
static inline unsigned
fa_he_offer(uint8_t *out, unsigned size, const uint8_t *mac, const uint8_t *phy, unsigned rx_mcs_80, unsigned tx_mcs_80)
{
	unsigned at = 0;

	if (size < FA_HE_OFFER_MOST) {
		return (0);
	}
	out[at++] = FA_EID_EXTENSION;
	out[at++] = FA_HE_OFFER_MOST - 2;
	out[at++] = FA_EXT_HE;
	memcpy(out + at, mac, FA_HE_MAC);
	at += FA_HE_MAC;
	memcpy(out + at, phy, FA_HE_PHY);
	out[at] &= (uint8_t)~FA_HE_PHY0_WIDTH;
	out[at + 6] &= (uint8_t)~FA_HE_PHY6_PPE;
	at += FA_HE_PHY;
	out[at++] = (uint8_t)rx_mcs_80;
	out[at++] = (uint8_t)(rx_mcs_80 >> 8);
	out[at++] = (uint8_t)tx_mcs_80;
	out[at++] = (uint8_t)(tx_mcs_80 >> 8);
	return (at);
}

/* ---- what the access point says -------------------------------------------------------------------------- */

struct fa_he {
	uint8_t		mac[FA_HE_MAC], phy[FA_HE_PHY];
	uint16_t	rx_mcs_80, tx_mcs_80, rx_mcs_160, tx_mcs_160, rx_mcs_80_80, tx_mcs_80_80;
	uint8_t		ppe[FA_HE_PPE_MOST];
};

/* A whole HE capabilities element, from fa_ext_element(). False when it does not hold what it says it has. */
static inline bool
fa_he_read(const uint8_t *element, struct fa_he *he)
{
	unsigned at = 3, end = 2u + element[1], extra;
	const uint8_t *e = element;

	memset(he, 0, sizeof(*he));
	if (end < at + FA_HE_MAC + FA_HE_PHY + 4) {
		return (false);
	}
	memcpy(he->mac, e + at, FA_HE_MAC);
	at += FA_HE_MAC;
	memcpy(he->phy, e + at, FA_HE_PHY);
	at += FA_HE_PHY;
	he->rx_mcs_80 = (uint16_t)(e[at] | e[at + 1] << 8);
	he->tx_mcs_80 = (uint16_t)(e[at + 2] | e[at + 3] << 8);
	at += 4;
	extra = ((he->phy[0] & FA_HE_PHY0_160) != 0 ? 4 : 0) + ((he->phy[0] & FA_HE_PHY0_80_80) != 0 ? 4 : 0);
	if (end - at < extra) {
		return (false);
	}
	if ((he->phy[0] & FA_HE_PHY0_160) != 0) {
		he->rx_mcs_160 = (uint16_t)(e[at] | e[at + 1] << 8);
		he->tx_mcs_160 = (uint16_t)(e[at + 2] | e[at + 3] << 8);
		at += 4;
	}
	if ((he->phy[0] & FA_HE_PHY0_80_80) != 0) {
		he->rx_mcs_80_80 = (uint16_t)(e[at] | e[at + 1] << 8);
		he->tx_mcs_80_80 = (uint16_t)(e[at + 2] | e[at + 3] << 8);
		at += 4;
	}
	if ((he->phy[6] & FA_HE_PHY6_PPE) != 0) {
		unsigned ppe = end - at < FA_HE_PPE_MOST ? end - at : FA_HE_PPE_MOST;

		if (ppe == 0) {
			return (false);
		}
		memcpy(he->ppe, e + at, ppe);
	}
	return (true);
}

/* Streams in a two-bits-a-stream MCS map, where 3 is "not supported". */
static inline unsigned
fa_mcs_map_streams(unsigned map)
{
	for (unsigned streams = 8; streams > 0; streams--) {
		if (((map >> (2 * (streams - 1))) & 3) != 3) {
			return (streams);
		}
	}
	return (0);
}

/*
 * The most streams the access point says it receives, at least one. Each of HT, VHT and HE may be NULL.
 * mac80211 works this out in ieee80211_sta_init_nss().
 */
static inline unsigned
fa_peer_streams(const uint8_t *ht, const uint8_t *vht, const struct fa_he *he)
{
	unsigned most = 1;

	if (ht != NULL && ht[1] >= FA_HT_BODY) {
		for (unsigned i = 0; i < 4; i++) {
			most = ht[5 + i] != 0 && i + 1 > most ? i + 1 : most;
		}
	}
	if (vht != NULL && vht[1] >= FA_VHT_BODY) {
		unsigned streams = fa_mcs_map_streams((unsigned)vht[6] | (unsigned)vht[7] << 8);

		most = streams > most ? streams : most;
	}
	if (he != NULL) {
		unsigned streams = fa_mcs_map_streams(he->rx_mcs_80);

		most = streams > most ? streams : most;
	}
	return (most);
}

/* The rates in a frame's elements that the band has, as bits in the band's own order, and the basic ones. */
static inline uint32_t
fa_rates_mask(const uint16_t *bitrates, unsigned count, const uint8_t *elements, unsigned length, uint32_t *basic)
{
	static const uint8_t kinds[2] = { FA_EID_RATES, FA_EID_RATES_MORE };
	uint32_t has = 0;

	*basic = 0;
	count = count > 32 ? 32 : count;
	for (unsigned k = 0; k < 2; k++) {
		const uint8_t *e = fa_element(elements, length, kinds[k]);

		for (unsigned i = 0; e != NULL && i < e[1]; i++) {
			for (unsigned r = 0; r < count; r++) {
				if (bitrates[r] == (e[2 + i] & 0x7fu) * 5u) {	/* half megabits there, tenths here */
					has |= 1u << r;
					*basic |= (e[2 + i] & 0x80) != 0 ? 1u << r : 0;
				}
			}
		}
	}
	return (has);
}

enum fa_answer { FA_ANSWER_NONE, FA_ANSWER_AUTH, FA_ANSWER_ASSOC, FA_ANSWER_REFUSED, FA_ANSWER_GONE };

struct fa_reply {
	enum fa_answer	answer;
	uint16_t	code;			/* its status or its reason */
	uint16_t	aid;
	const uint8_t	*elements;		/* of an association response, in the frame */
	unsigned	elements_length;
	uint8_t		kind;			/* of the frame */
};

/*
 * A management frame, if it is the access point's answer to this station: both addresses are checked, so that nobody
 * else's frame moves the join. FA_ANSWER_NONE for anything else, including an answer that cannot be read, an
 * association number the standard does not have, and a protected frame: PMF is not built, so there are none.
 */
static inline void
fa_reply_read(struct fa_reply *reply, const uint8_t *frame, unsigned length, const uint8_t *own, const uint8_t *bssid)
{
	unsigned header = FA_FRAME_HEADER, fixed;
	const uint8_t *at;

	memset(reply, 0, sizeof(*reply));
	if (length < FA_FRAME_HEADER || (frame[0] & 0x0f) != 0 || (frame[1] & 0x40) != 0) {
		return;
	}
	header += (frame[1] & 0x80) != 0 ? FA_FRAME_HT_CONTROL : 0;
	if (length < header || memcmp(frame + 4, own, 6) != 0 || memcmp(frame + 10, bssid, 6) != 0 || memcmp(frame + 16, bssid, 6) != 0) {
		return;
	}
	at = frame + header;
	fixed = length - header;
	reply->kind = frame[0];
	switch (frame[0]) {
	case FA_KIND_AUTH:				/* algorithm, step, status */
		if (fixed < 6 || (at[0] | at[1] << 8) != 0 || (at[2] | at[3] << 8) != 2) {
			return;
		}
		reply->code = (uint16_t)(at[4] | at[5] << 8);
		reply->answer = reply->code == 0 ? FA_ANSWER_AUTH : FA_ANSWER_REFUSED;
		return;
	case FA_KIND_ASSOC_RESPONSE:			/* capabilities, status, association number, elements */
		if (fixed < 6) {
			return;
		}
		reply->code = (uint16_t)(at[2] | at[3] << 8);
		reply->aid = (uint16_t)((at[4] | at[5] << 8) & 0x3fff);
		if (reply->code != 0) {
			reply->answer = FA_ANSWER_REFUSED;
			reply->aid = 0;
			return;
		}
		if (reply->aid == 0 || reply->aid > FA_AID_MOST) {
			reply->aid = 0;
			return;
		}
		reply->elements = at + 6;
		reply->elements_length = fixed - 6;
		reply->answer = FA_ANSWER_ASSOC;
		return;
	case FA_KIND_DEAUTH:
	case FA_KIND_DISASSOC:				/* the reason */
		if (fixed < 2) {
			return;
		}
		reply->code = (uint16_t)(at[0] | at[1] << 8);
		reply->answer = FA_ANSWER_GONE;
		return;
	default:
		return;
	}
}

/* ---- the handshake's frames ------------------------------------------------------------------------------- */

#define FA_LLC_LENGTH		8
#define FA_EAPOL_MOST		640		/* what fa_wpa.c reads at most */
#define FA_QOS_HEADER		26
#define FA_TID_CONTROL		7		/* mac80211 sends the control port's frames at the highest priority */

/*
 * The EAPOL frame in what the driver received, if this is one from the access point to this station: in Ethernet form
 * (`ethernet`) or 802.11, with the cipher's 8 bytes when the frame is protected and they were left there. False for
 * anything else: every address is checked, and every length against the frame.
 */
static inline bool
fa_eapol_read(const uint8_t *frame, unsigned length, bool ethernet, bool cipher_header_gone, const uint8_t *own, const uint8_t *bssid,
    const uint8_t **body, unsigned *body_length)
{
	static const uint8_t llc[FA_LLC_LENGTH] = { 0xaa, 0xaa, 0x03, 0x00, 0x00, 0x00, 0x88, 0x8e };
	unsigned header = FA_FRAME_HEADER;

	if (ethernet) {
		if (length <= 14 || memcmp(frame, own, 6) != 0 || memcmp(frame + 6, bssid, 6) != 0 || frame[12] != 0x88 || frame[13] != 0x8e) {
			return (false);
		}
		*body = frame + 14;
		*body_length = length - 14;
		return (true);
	}
	/* data with a payload, from the distribution system and not to it */
	if (length < FA_FRAME_HEADER || (frame[0] & 0x0f) != 0x08 || (frame[0] & 0x40) != 0 || (frame[1] & 0x03) != 0x02) {
		return (false);
	}
	if (memcmp(frame + 4, own, 6) != 0 || memcmp(frame + 10, bssid, 6) != 0 || memcmp(frame + 16, bssid, 6) != 0) {
		return (false);
	}
	if ((frame[0] & 0x80) != 0) {
		header += 2 + ((frame[1] & 0x80) != 0 ? FA_FRAME_HT_CONTROL : 0);
	}
	header += (frame[1] & 0x40) != 0 && !cipher_header_gone ? 8 : 0;
	if (length <= header + FA_LLC_LENGTH || memcmp(frame + header, llc, FA_LLC_LENGTH) != 0) {
		return (false);
	}
	*body = frame + header + FA_LLC_LENGTH;
	*body_length = length - header - FA_LLC_LENGTH;
	return (true);
}

/* An EAPOL frame as 802.11 data for the access point, QoS data when it does QoS. The number of bytes, 0 when they do not fit. */
static inline unsigned
fa_eapol_frame(uint8_t *out, unsigned size, const uint8_t *own, const uint8_t *bssid, bool qos, const uint8_t *body, unsigned body_length)
{
	static const uint8_t llc[FA_LLC_LENGTH] = { 0xaa, 0xaa, 0x03, 0x00, 0x00, 0x00, 0x88, 0x8e };
	unsigned header = qos ? FA_QOS_HEADER : FA_FRAME_HEADER;

	if (size < header + FA_LLC_LENGTH + body_length) {
		return (0);
	}
	memset(out, 0, header);
	out[0] = qos ? 0x88 : 0x08;
	out[1] = 0x01;				/* to the distribution system */
	memcpy(out + 4, bssid, 6);
	memcpy(out + 10, own, 6);
	memcpy(out + 16, bssid, 6);
	if (qos) {
		out[FA_FRAME_HEADER] = FA_TID_CONTROL;
	}
	memcpy(out + header, llc, FA_LLC_LENGTH);
	memcpy(out + header + FA_LLC_LENGTH, body, body_length);
	return (header + FA_LLC_LENGTH + body_length);
}

enum fa_rx_kind { FA_RX_ELSE, FA_RX_BEACON, FA_RX_MANAGEMENT, FA_RX_DATA };

/*
 * What a received 802.11 frame is to the link: the access point's beacon, some management frame, or data from
 * the access point. Frames of no payload (null data, 0x04 in the subtype) are not data here.
 */
static inline enum fa_rx_kind
fa_rx_kind(const uint8_t *frame, unsigned length, const uint8_t *bssid)
{
	if (length < FA_FRAME_HEADER || (frame[0] & 0x03) != 0) {
		return (FA_RX_ELSE);
	}
	switch (frame[0] & 0x0c) {
	case 0x00:
		return (frame[0] == FA_KIND_BEACON ? (memcmp(frame + 16, bssid, 6) == 0 ? FA_RX_BEACON : FA_RX_ELSE) : FA_RX_MANAGEMENT);
	case 0x08:
		return ((frame[0] & 0x40) == 0 && memcmp(frame + 10, bssid, 6) == 0 ? FA_RX_DATA : FA_RX_ELSE);
	default:
		return (FA_RX_ELSE);
	}
}

#endif
