/*
 * What stage 8 reads off the air for iOS, as bytes: the table a scan fills, and the packet number of frames sent to
 * everybody (design W4), which the hardware does not check. Nothing in a received frame is trusted. Like fa_join.h this
 * needs only <stdint.h>, <stdbool.h> and memcpy(), and tools/test-fuxi-ath12k.py runs it on malformed frames under the
 * sanitizers. Include fa_bss.h, fa_join.h and fa_xnu.h first.
 *
 * SPDX-License-Identifier: MIT
 */
#ifndef _FA_IOS_H_
#define _FA_IOS_H_

#define FA_HEARD_MOST		128
#define FA_RSSI_UNKNOWN		(-127)

/*
 * One beacon or probe response into the table: an access point and a name once, at its strongest. With a name asked for
 * (`name_length`), only that name. True when it was taken. Elements past FA_IOS_ELEMENTS_MOST are cut at a whole element.
 */
static inline bool
fa_heard_take(struct fa_heard *table, unsigned *count, const uint8_t *frame, unsigned length, unsigned frequency, int rssi, unsigned long long now,
    const uint8_t *name, unsigned name_length)
{
	const uint8_t *bssid, *elements, *ssid;
	unsigned header = FA_FRAME_HEADER, elements_length;
	struct fa_heard *entry = NULL;

	if (length < 2 || (frame[0] != FA_KIND_BEACON && frame[0] != FA_KIND_PROBE_RESPONSE) || (frame[1] & 0x40) != 0) {
		return (false);
	}
	header += (frame[1] & 0x80) != 0 ? FA_FRAME_HT_CONTROL : 0;
	if (length < header + FA_FRAME_FIXED || frequency == 0 || frequency > 0xffff) {
		return (false);
	}
	bssid = frame + 16;
	if ((bssid[0] & 1) != 0 || (bssid[0] | bssid[1] | bssid[2] | bssid[3] | bssid[4] | bssid[5]) == 0) {
		return (false);
	}
	elements = frame + header + FA_FRAME_FIXED;
	elements_length = length - header - FA_FRAME_FIXED;
	ssid = fa_element(elements, elements_length, FA_EID_SSID);
	if (ssid == NULL || ssid[1] > FA_SSID_MOST) {
		return (false);
	}
	if (name_length != 0 && (ssid[1] != name_length || memcmp(ssid + 2, name, name_length) != 0)) {
		return (false);
	}
	for (unsigned i = 0; i < *count; i++) {
		if (memcmp(table[i].bssid, bssid, 6) == 0 && table[i].ssid_length == ssid[1] && memcmp(table[i].ssid, ssid + 2, ssid[1]) == 0) {
			entry = &table[i];
			break;
		}
	}
	rssi = rssi <= FA_RSSI_NONE ? FA_RSSI_UNKNOWN : rssi > 127 ? 127 : rssi;
	if (entry == NULL) {
		if (*count == FA_HEARD_MOST) {
			return (false);
		}
		entry = &table[(*count)++];
		memset(entry, 0, sizeof(*entry));
		entry->rssi = FA_RSSI_UNKNOWN;
		entry->frequency = 0;
	} else if (rssi < entry->rssi) {
		entry->heard_at = now;
		return (true);
	}
	memcpy(entry->bssid, bssid, 6);
	memcpy(entry->ssid, ssid + 2, ssid[1]);
	entry->ssid_length = ssid[1];
	entry->frequency = (unsigned short)frequency;
	entry->rssi = (short)rssi;
	entry->interval = (unsigned short)(frame[header + 8] | frame[header + 9] << 8);
	entry->capability = (unsigned short)(frame[header + 10] | frame[header + 11] << 8);
	entry->heard_at = now;
	entry->elements_length = (unsigned short)fa_elements_cut(elements, elements_length, sizeof(entry->elements));
	memcpy(entry->elements, elements, entry->elements_length);
	return (true);
}

struct fa_group_pn {
	unsigned long long	last[4];
	bool			known[4];
};

enum fa_group_answer { FA_GROUP_FRESH, FA_GROUP_REPLAY, FA_GROUP_NO_KEY };

/* The six bytes of a key's receive counter, as message 3 gives them: low byte first (IEEE 802.11-2020, 12.7.2). */
static inline unsigned long long
fa_rsc_read(const uint8_t *counter)
{
	unsigned long long value = 0;

	for (unsigned i = 0; i < 6; i++) {
		value |= (unsigned long long)counter[i] << (8 * i);
	}
	return (value);
}

/* Design W4: a frame to everybody, by the CCMP header in front of its payload. A number not beyond the last is a frame played again. */
static inline enum fa_group_answer
fa_group_check(struct fa_group_pn *group, const uint8_t *cipher)
{
	unsigned index = cipher[3] >> 6;
	unsigned long long number;

	if ((cipher[3] & 0x20) == 0 || !group->known[index]) {
		return (FA_GROUP_NO_KEY);
	}
	number = (unsigned long long)cipher[0] | (unsigned long long)cipher[1] << 8 | (unsigned long long)cipher[4] << 16 |
	    (unsigned long long)cipher[5] << 24 | (unsigned long long)cipher[6] << 32 | (unsigned long long)cipher[7] << 40;
	if (number <= group->last[index]) {
		return (FA_GROUP_REPLAY);
	}
	group->last[index] = number;
	return (FA_GROUP_FRESH);
}

#endif
