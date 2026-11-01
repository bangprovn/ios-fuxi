/*
 * WPA2 with a key that is already made, as a station sees it (RSN, AKM "PSK", key descriptor version 2: HMAC-SHA1 and
 * the AES key wrap). fa_wpa.c is the arithmetic and nothing else, so that it runs on the host under the sanitizers.
 *
 * SPDX-License-Identifier: APSL-2.0
 */
#ifndef _FA_WPA_H_
#define _FA_WPA_H_

struct fa_wpa {
	unsigned char pmk[32];
	unsigned char own[6], peer[6];		/* the station's address and the access point's */
	unsigned char own_nonce[32];		/* fresh for every handshake */
	const unsigned char *rsn;		/* the station's RSN element as its association request had it ... */
	unsigned rsn_length;			/* ... which goes into message 2 */
	const unsigned char *peer_rsn;		/* the access point's, as its beacon had it: message 3 must say the same */
	unsigned peer_rsn_length;
	/* from here on: fa_wpa.c's */
	unsigned char peer_nonce[32];
	unsigned char kck[16], kek[16];		/* the keys the handshake's own messages are signed and ciphered with */
	unsigned char replay[8];		/* the counter of the last message accepted */
	int have_keys, replay_known;
	/* what comes out of it */
	unsigned char pairwise[16];		/* the key for frames between the two (CCMP) */
	unsigned char group[32];		/* the key the access point sends to everybody with ... */
	unsigned group_length, group_index;	/* ... its length, and its number (1 to 3) */
	unsigned char group_counter[8];		/* ... and the packet number its frames have got to */
};

/* What a message of the handshake (what follows the type 0x888e) calls for. `reply` has room for FA_WPA_REPLY_MOST bytes. */
#define FA_WPA_REPLY_MOST	256
enum {
	FA_WPA_BAD = -1,		/* not a message that can be answered: *why says what was wrong with it */
	FA_WPA_NOTHING = 0,		/* not for us to answer (not a key message) */
	FA_WPA_ANSWER = 1,		/* message 1: send the reply (message 2) */
	FA_WPA_KEYS = 2,		/* message 3: send the reply (message 4), then both keys are to be used */
	FA_WPA_GROUP = 3,		/* a new group key later on: send the reply, then use the new group key */
};
int fa_wpa_message(struct fa_wpa *wpa, const unsigned char *message, unsigned length, unsigned char *reply, unsigned *reply_length,
    const char **why);

/* Gone, and the compiler may not decide otherwise. */
static inline void
fa_wipe(void *memory, unsigned long size)
{
	volatile unsigned char *at = memory;

	while (size-- != 0) {
		*at++ = 0;
	}
}

#endif
