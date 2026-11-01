/*
 * Key wrap and unwrap for the built-in AppleKeyStore (SweetKeyStore.cpp), so that securityd can write and read keychain
 * items on a test phone. THIS IS NOT ENCRYPTION AND PROTECTS NOTHING: there is no Secure Enclave here, the pattern is
 * fixed and anyone with the root file system can read every item. It keeps the keychain working, and that is all.
 *
 * The layout is ours, 8 bytes longer than the key as securityd asks (`in 2/32 out 1/40`): "FXAK", the key class little
 * endian, the key mixed with the pattern. Items stay readable across boots because nothing here changes. HackiOS answers
 * the same two selectors the same way; this is written from that description, no code of theirs.
 *
 * Plain C with nothing around it: tools/test-fuxi-ath12k.py runs it under the sanitizers.
 *
 * SPDX-License-Identifier: MIT
 */
#ifndef _FUXI_AKS_WRAP_H_
#define _FUXI_AKS_WRAP_H_

#define FUXI_AKS_KEY_MOST	256u
#define FUXI_AKS_OVERHEAD	8u
#define FUXI_AKS_WRAPPED_MOST	(FUXI_AKS_KEY_MOST + FUXI_AKS_OVERHEAD)

static inline unsigned char
fuxi_aks_pattern(unsigned at)
{
	return ((unsigned char)(0x5au ^ (at * 0x1fu)));
}

/* The wrapped form of `key` for this class. Its length, 0 when the key's size or the room is not right. */
static inline unsigned
fuxi_aks_wrap(const unsigned char *key, unsigned length, unsigned key_class, unsigned char *out, unsigned room)
{
	if (length == 0 || length > FUXI_AKS_KEY_MOST || room < length + FUXI_AKS_OVERHEAD) {
		return (0);
	}
	out[0] = 'F';
	out[1] = 'X';
	out[2] = 'A';
	out[3] = 'K';
	for (unsigned i = 0; i < 4; i++) {
		out[4 + i] = (unsigned char)(key_class >> (8 * i));
	}
	for (unsigned i = 0; i < length; i++) {
		out[FUXI_AKS_OVERHEAD + i] = key[i] ^ fuxi_aks_pattern(i);
	}
	return (length + FUXI_AKS_OVERHEAD);
}

/* The key back. Its length, 0 when this is not ours, is of another class, or the room is not right. */
static inline unsigned
fuxi_aks_unwrap(const unsigned char *wrapped, unsigned length, unsigned key_class, unsigned char *out, unsigned room)
{
	unsigned key_length;

	if (length <= FUXI_AKS_OVERHEAD || length > FUXI_AKS_WRAPPED_MOST || wrapped[0] != 'F' || wrapped[1] != 'X' || wrapped[2] != 'A' || wrapped[3] != 'K') {
		return (0);
	}
	key_length = length - FUXI_AKS_OVERHEAD;
	if (room < key_length) {
		return (0);
	}
	for (unsigned i = 0; i < 4; i++) {
		if (wrapped[4 + i] != (unsigned char)(key_class >> (8 * i))) {
			return (0);
		}
	}
	for (unsigned i = 0; i < key_length; i++) {
		out[i] = wrapped[FUXI_AKS_OVERHEAD + i] ^ fuxi_aks_pattern(i);
	}
	return (key_length);
}

#endif
