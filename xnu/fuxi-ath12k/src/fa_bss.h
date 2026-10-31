/*
 * What a scan keeps of the networks it hears, and how it reads a beacon or a probe response off the air.
 * Nothing in a received frame is trusted: every length is checked against the frame, nothing here needs
 * more than <stdint.h>, <stdbool.h> and memcpy(), and tools/test-fuxi-ath12k.py runs it on malformed
 * frames under the sanitizers. Frame layout: IEEE 802.11-2020, 9.3.3.2 and 9.3.3.10.
 *
 * Adapted from heard() and element() of hackios wifi/lx/src/lx_80211.c (MIT), see compat/PROVENANCE.md.
 *
 * SPDX-License-Identifier: MIT
 */
#ifndef _FA_BSS_H_
#define _FA_BSS_H_

#define FA_BSS_MOST		64		/* networks kept */
#define FA_BSS_SAID_MOST	40		/* of them, lines in the log */
#define FA_SSID_MOST		32		/* IEEE 802.11-2020, 9.4.2.2 */
#define FA_SSID_ESCAPED_MOST	(FA_SSID_MOST * 4 + 1)	/* each byte as \xHH, and the end */
#define FA_RSSI_NONE		(-128)		/* the path it came by gave no signal value */
#define FA_FRAME_HEADER		24
#define FA_FRAME_HT_CONTROL	4		/* the header's +HTC field, when Order is set */
#define FA_FRAME_FIXED		12		/* time stamp, beacon interval, capabilities */
#define FA_EID_SSID		0
#define FA_EID_RATES		1
#define FA_EID_RATES_MORE	50
#define FA_RATES_IN_ONE		8		/* a Supported Rates element's most; the others go in the next */

struct fa_bss {
	uint8_t		address[6];		/* BSSID: the key, and never said in the log */
	uint8_t		ssid_length;
	uint8_t		ssid[FA_SSID_MOST];
	uint16_t	frequency;		/* MHz, where it was heard at its strongest */
	int8_t		rssi;			/* dBm, the strongest */
	unsigned	times;
};

struct fa_bss_table {
	unsigned	count;
	unsigned	beacons, responses;	/* frames taken */
	unsigned	malformed;		/* beacons and responses that were refused */
	unsigned	others;			/* frames that are neither */
	unsigned	lost;			/* networks left out, or pushed out by a stronger one */
	struct fa_bss	entry[FA_BSS_MOST];
};

enum fa_bss_result { FA_BSS_TAKEN, FA_BSS_OTHER, FA_BSS_MALFORMED, FA_BSS_LOST };

/*
 * The whole element of that number among `length` bytes of elements, or NULL. An element that claims more
 * than there is ends the walk: nothing after it can be told from noise.
 */
static inline const uint8_t *
fa_element(const uint8_t *elements, unsigned length, unsigned number)
{
	unsigned at = 0;

	while (length - at >= 2 && length - at - 2 >= elements[at + 1]) {
		if (elements[at] == number) {
			return (elements + at);
		}
		at += 2u + elements[at + 1];
	}
	return (NULL);
}

static inline bool
fa_ssid_hidden(const uint8_t *ssid, unsigned length)
{
	for (unsigned i = 0; i < length; i++) {
		if (ssid[i] != 0) {
			return (false);
		}
	}
	return (true);
}

/* The weakest of a full table, or -1 when none is weaker than `rssi`. */
static inline int
fa_bss_weakest(const struct fa_bss_table *table, int rssi)
{
	int weakest = -1, signal = rssi;

	for (unsigned i = 0; i < table->count; i++) {
		if (table->entry[i].rssi < signal) {
			signal = table->entry[i].rssi;
			weakest = (int)i;
		}
	}
	return (weakest);
}

/*
 * One received frame, `length` bytes. A beacon or probe response with a sound header and an SSID element
 * goes in the table, one entry per BSSID, the strongest hearing's frequency and signal kept. A real name
 * replaces a hidden one, never the other way round: a hidden network's beacon and its probe response.
 */
static inline enum fa_bss_result
fa_bss_take(struct fa_bss_table *table, const uint8_t *frame, unsigned length, unsigned frequency, int rssi)
{
	const uint8_t *bssid, *ssid;
	struct fa_bss *network = NULL;
	unsigned header = FA_FRAME_HEADER, name_length;
	uint8_t kind;

	if (length < 2) {
		table->others++;
		return (FA_BSS_OTHER);
	}
	kind = frame[0];
	if (kind != 0x80 && kind != 0x50) {
		table->others++;
		return (FA_BSS_OTHER);
	}
	header += (frame[1] & 0x80) != 0 ? FA_FRAME_HT_CONTROL : 0;
	if (length < header + FA_FRAME_FIXED || (frame[1] & 0x40) != 0 || frequency == 0 || frequency > 0xffff) {
		table->malformed++;
		return (FA_BSS_MALFORMED);
	}
	bssid = frame + 16;
	if ((bssid[0] & 1) != 0 || (bssid[0] | bssid[1] | bssid[2] | bssid[3] | bssid[4] | bssid[5]) == 0) {
		table->malformed++;
		return (FA_BSS_MALFORMED);
	}
	ssid = fa_element(frame + header + FA_FRAME_FIXED, length - header - FA_FRAME_FIXED, FA_EID_SSID);
	if (ssid == NULL || ssid[1] > FA_SSID_MOST) {
		table->malformed++;
		return (FA_BSS_MALFORMED);
	}
	name_length = ssid[1];
	rssi = rssi < FA_RSSI_NONE ? FA_RSSI_NONE : rssi > 127 ? 127 : rssi;
	for (unsigned i = 0; i < table->count && network == NULL; i++) {
		if (memcmp(table->entry[i].address, bssid, 6) == 0) {
			network = &table->entry[i];
		}
	}
	if (network == NULL) {
		if (table->count < FA_BSS_MOST) {
			network = &table->entry[table->count++];
		} else {
			int weakest = fa_bss_weakest(table, rssi);

			table->lost++;
			if (weakest < 0) {
				return (FA_BSS_LOST);
			}
			network = &table->entry[weakest];
		}
		memcpy(network->address, bssid, 6);
		network->ssid_length = 0;
		network->frequency = (uint16_t)frequency;
		network->rssi = (int8_t)rssi;
		network->times = 0;
	} else if (rssi > network->rssi) {
		network->frequency = (uint16_t)frequency;
		network->rssi = (int8_t)rssi;
	}
	network->times++;
	if (!fa_ssid_hidden(ssid + 2, name_length) || network->ssid_length == 0) {
		network->ssid_length = (uint8_t)name_length;
		memcpy(network->ssid, ssid + 2, name_length);
	}
	*(kind == 0x80 ? &table->beacons : &table->responses) += 1;
	return (FA_BSS_TAKEN);
}

/*
 * An SSID as text for the log: bounded, ending in NUL, printable ASCII kept and everything else \xHH, with
 * the quote and the backslash escaped, so a name cannot forge or break a line. `size` of FA_SSID_ESCAPED_MOST
 * always holds it; a smaller one cuts at a whole escape. The length of the text.
 */
static inline unsigned
fa_ssid_escape(char *out, unsigned size, const uint8_t *ssid, unsigned length)
{
	static const char hex[] = "0123456789abcdef";
	unsigned at = 0;

	if (size == 0) {
		return (0);
	}
	length = length > FA_SSID_MOST ? FA_SSID_MOST : length;
	for (unsigned i = 0; i < length; i++) {
		uint8_t c = ssid[i];

		if (c == '"' || c == '\\') {
			if (size - at < 3) {
				break;
			}
			out[at++] = '\\';
			out[at++] = (char)c;
		} else if (c >= 0x20 && c < 0x7f) {
			if (size - at < 2) {
				break;
			}
			out[at++] = (char)c;
		} else {
			if (size - at < 5) {
				break;
			}
			out[at++] = '\\';
			out[at++] = 'x';
			out[at++] = hex[c >> 4];
			out[at++] = hex[c & 15];
		}
	}
	out[at] = 0;
	return (at);
}

/*
 * Which entries to say, the strongest first: at most `most`, their indices in order[]. Ties keep the order
 * they were heard in. The number of them.
 */
static inline unsigned
fa_bss_strongest(const struct fa_bss_table *table, uint8_t *order, unsigned most)
{
	bool used[FA_BSS_MOST] = { false };
	unsigned n = 0;

	while (n < most && n < table->count) {
		int best = -1;

		for (unsigned i = 0; i < table->count; i++) {
			if (!used[i] && (best < 0 || table->entry[i].rssi > table->entry[best].rssi)) {
				best = (int)i;
			}
		}
		used[best] = true;
		order[n++] = (uint8_t)best;
	}
	return (n);
}

/*
 * A probe request's rates: Supported Rates, and Extended Supported Rates for what does not fit, from a list
 * in units of 100 kb/s as a band's bitrate table has them. Without either element an access point may ignore
 * the request. Nothing else is offered: no HT, VHT, HE or EHT. The number of bytes, 0 when they do not fit.
 */
static inline unsigned
fa_probe_rates(uint8_t *out, unsigned size, const uint16_t *rates, unsigned count)
{
	unsigned at = 0, in_one = count < FA_RATES_IN_ONE ? count : FA_RATES_IN_ONE;

	if (count == 0 || count > 255 || size < 2 + in_one || size - 2 - in_one < (count > in_one ? 2 + count - in_one : 0)) {
		return (0);
	}
	out[at++] = FA_EID_RATES;
	out[at++] = (uint8_t)in_one;
	for (unsigned i = 0; i < count; i++) {
		if (i == FA_RATES_IN_ONE) {
			out[at++] = FA_EID_RATES_MORE;
			out[at++] = (uint8_t)(count - FA_RATES_IN_ONE);
		}
		out[at++] = (uint8_t)(rates[i] / 5);	/* the element's units are 500 kb/s */
	}
	return (at);
}

#endif
