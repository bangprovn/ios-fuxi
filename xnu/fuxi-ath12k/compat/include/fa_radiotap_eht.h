/*
 * Radiotap EHT values that FreeBSD's net80211/ieee80211_radiotap.h does not define, from the public
 * field definition at radiotap.org/fields/EHT.html (read 2026-11-01), and the enumeration types
 * wifi7/dp_mon.c declares locals with. Only that file uses them, to build monitor headers; this
 * domain creates no monitor interface, so none of it runs yet.
 *
 * SPDX-License-Identifier: MIT (see compat/PROVENANCE.md)
 */
#ifndef _FA_RADIOTAP_EHT_H_
#define _FA_RADIOTAP_EHT_H_

#define IEEE80211_RADIOTAP_EHT_KNOWN_DISREGARD_O		0x00000100
#define IEEE80211_RADIOTAP_EHT_KNOWN_DISREGARD_S		0x00000200
#define IEEE80211_RADIOTAP_EHT_KNOWN_CRC1			0x00002000
#define IEEE80211_RADIOTAP_EHT_KNOWN_TAIL1			0x00004000
#define IEEE80211_RADIOTAP_EHT_KNOWN_RU_MRU_SIZE_OM		0x00400000
#define IEEE80211_RADIOTAP_EHT_KNOWN_RU_MRU_INDEX_OM		0x00800000

#define IEEE80211_RADIOTAP_EHT_DATA0_DISREGARD_S		0x000c0000
#define IEEE80211_RADIOTAP_EHT_DATA0_DISREGARD_O		0x003c0000
#define IEEE80211_RADIOTAP_EHT_DATA0_CRC1_O			0x03c00000

#define IEEE80211_RADIOTAP_EHT_DATA1_RU_SIZE			0x0000001f
#define IEEE80211_RADIOTAP_EHT_DATA1_RU_INDEX			0x00001fe0

#define IEEE80211_RADIOTAP_EHT_USER_INFO_SPATIAL_CONFIG_KNOWN_M	0x00000040
#define IEEE80211_RADIOTAP_EHT_USER_INFO_SPATIAL_CONFIG_M	0x3f000000

/* Only holders of the masks above and FreeBSD's: wide enough for all of them. */
enum ieee80211_radiotap_eht_data { FA_RADIOTAP_EHT_DATA_WIDTH = 0xffffffffu };
enum ieee80211_radiotap_eht_usig_mu { FA_RADIOTAP_USIG_MU_WIDTH = 0xffffffffu };
enum ieee80211_radiotap_eht_usig_tb { FA_RADIOTAP_USIG_TB_WIDTH = 0xffffffffu };

#endif
