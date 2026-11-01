/*
 * What stage 7 says on a network once the keys are in: one address by DHCP (RFC 2131, 2132), the router's hardware
 * address by ARP (RFC 826), and an echo (RFC 792), each built and read here as bytes. Nothing in a received frame is
 * trusted: every length is checked against the frame. Like fa_join.h this needs only <stdint.h>, <stdbool.h> and memcpy(),
 * and tools/test-fuxi-ath12k.py runs it on malformed frames under the sanitizers. Include fa_join.h first.
 *
 * SPDX-License-Identifier: MIT
 */
#ifndef _FA_NET_H_
#define _FA_NET_H_

#define FA_TYPE_IP		0x0800
#define FA_TYPE_ARP		0x0806
#define FA_NET_MOST		1500
#define FA_IP_HEADER		20
#define FA_UDP_HEADER		8
#define FA_BOOTP_FIXED		236
#define FA_BOOTP_LENGTH		300		/* relays of old want at least this */
#define FA_DHCP_LENGTH		(FA_IP_HEADER + FA_UDP_HEADER + FA_BOOTP_LENGTH)
#define FA_ARP_LENGTH		28
#define FA_ECHO_DATA		32
#define FA_ECHO_LENGTH		(FA_IP_HEADER + 8 + FA_ECHO_DATA)
#define FA_PROTO_ICMP		1
#define FA_PROTO_UDP		17
#define FA_PORT_SERVER		67
#define FA_PORT_CLIENT		68

enum { FA_DHCP_DISCOVER = 1, FA_DHCP_OFFER = 2, FA_DHCP_REQUEST = 3, FA_DHCP_ACK = 5, FA_DHCP_NAK = 6, FA_DHCP_RELEASE = 7 };

static inline unsigned
fa_be16(const uint8_t *at)
{
	return ((unsigned)at[0] << 8 | at[1]);
}

static inline uint32_t
fa_be32(const uint8_t *at)
{
	return ((uint32_t)at[0] << 24 | (uint32_t)at[1] << 16 | (uint32_t)at[2] << 8 | at[3]);
}

static inline void
fa_put16(uint8_t *at, unsigned value)
{
	at[0] = (uint8_t)(value >> 8);
	at[1] = (uint8_t)value;
}

/* The Internet checksum (RFC 1071): over a whole message this comes to 0 when it is right. */
static inline unsigned
fa_ip_sum(const uint8_t *at, unsigned length)
{
	uint32_t sum = 0;

	for (; length >= 2; at += 2, length -= 2) {
		sum += fa_be16(at);
	}
	if (length != 0) {
		sum += (uint32_t)at[0] << 8;
	}
	while ((sum >> 16) != 0) {
		sum = (sum & 0xffff) + (sum >> 16);
	}
	return (~sum & 0xffff);
}

/* ---- what goes out, counted ---------------------------------------------------------------------------------- */

struct fa_tx_counts {
	unsigned	arp, ip6, tcp, udp, icmp, ip_other, eapol, other;
};

/* One frame of this type going out, with what follows its Ethernet header. Reads nothing past `length`. */
static inline void
fa_tx_note(struct fa_tx_counts *counts, unsigned type, const uint8_t *body, unsigned length)
{
	if (type == FA_TYPE_ARP) {
		counts->arp++;
	} else if (type == 0x86dd) {
		counts->ip6++;
	} else if (type == 0x888e) {
		counts->eapol++;
	} else if (type != FA_TYPE_IP) {
		counts->other++;
	} else if (length < FA_IP_HEADER) {
		counts->ip_other++;
	} else if (body[9] == FA_PROTO_UDP) {
		counts->udp++;
	} else if (body[9] == 6) {
		counts->tcp++;
	} else if (body[9] == FA_PROTO_ICMP) {
		counts->icmp++;
	} else {
		counts->ip_other++;
	}
}

/* ---- a test's filter on what iOS sends: stage 8, fuxiath12kq ------------------------------------------------ */

#define FA_TX_DROP_IP6		0x01u		/* IPv6 */
#define FA_TX_DROP_TCP		0x02u		/* IPv4 TCP */
#define FA_TX_DROP_UDP		0x04u		/* IPv4 UDP but DHCP */
#define FA_TX_DROP_GROUP	0x08u		/* to many, multicast or broadcast, but ARP and DHCP */

/*
 * Whether this, after the Ethernet header, is DHCP: a whole IPv4 UDP datagram with port 67 or 68 at either end. Not a fragment, whose
 * ports cannot be read, nor one that goes past the frame. Its checksums are not asked after: filling them may be left to others.
 */
static inline bool
fa_tx_is_dhcp(const uint8_t *body, unsigned length)
{
	unsigned header, total, source, target;

	if (length < FA_IP_HEADER || (body[0] >> 4) != 4 || body[9] != FA_PROTO_UDP || (fa_be16(body + 6) & 0x3fff) != 0) {
		return (false);
	}
	header = (body[0] & 0x0fu) * 4;
	total = fa_be16(body + 2);
	if (header < FA_IP_HEADER || total < header + FA_UDP_HEADER || total > length) {
		return (false);
	}
	source = fa_be16(body + header);
	target = fa_be16(body + header + 2);
	return (source == FA_PORT_SERVER || source == FA_PORT_CLIENT || target == FA_PORT_SERVER || target == FA_PORT_CLIENT);
}

/*
 * Whether `filter`, of FA_TX_DROP_ bits, takes this Ethernet frame. ARP and DHCP always pass, and so does the handshake's type. An IPv4
 * header that cannot be one says neither TCP nor UDP. Reads nothing past `length`, and with no bits nothing at all.
 */
static inline bool
fa_tx_filtered(unsigned filter, const uint8_t *frame, unsigned length)
{
	const uint8_t *body;
	unsigned type, size;

	if (filter == 0 || length < 14) {
		return (false);
	}
	body = frame + 14;
	type = fa_be16(frame + 12);
	size = length - 14;
	if (type == FA_TYPE_ARP || type == 0x888e || (type == FA_TYPE_IP && fa_tx_is_dhcp(body, size))) {
		return (false);
	}
	if ((filter & FA_TX_DROP_GROUP) != 0 && (frame[0] & 1) != 0) {
		return (true);
	}
	if (type == 0x86dd) {
		return ((filter & FA_TX_DROP_IP6) != 0);
	}
	if (type != FA_TYPE_IP || size < FA_IP_HEADER || (body[0] >> 4) != 4 || (body[0] & 0x0fu) < FA_IP_HEADER / 4) {
		return (false);
	}
	return (body[9] == 6 ? (filter & FA_TX_DROP_TCP) != 0 : body[9] == FA_PROTO_UDP && (filter & FA_TX_DROP_UDP) != 0);
}

/* ---- data frames ------------------------------------------------------------------------------------------- */

struct fa_data {
	unsigned	type;
	const uint8_t	*body;
	unsigned	length;
	uint8_t		from[6], to[6];
	const uint8_t	*cipher;		/* the cipher's 8 bytes, when they are there */
};

/*
 * What a received data frame carries, if it is for this station or for everybody, and came from the access point: in
 * Ethernet form (`ethernet`) or 802.11, with the cipher's 8 bytes when the frame is protected and they were left there.
 * What this station sent itself, heard back from the access point, is not taken.
 */
static inline bool
fa_data_read(const uint8_t *frame, unsigned length, bool ethernet, bool cipher_header_gone, const uint8_t *own, const uint8_t *bssid,
    struct fa_data *data)
{
	unsigned header = FA_FRAME_HEADER;
	const uint8_t *to, *from;

	data->cipher = NULL;
	if (ethernet) {
		if (length < 14) {
			return (false);
		}
		to = frame;
		from = frame + 6;
		header = 14;
	} else {
		if (length < FA_FRAME_HEADER || (frame[0] & 0x0f) != 0x08 || (frame[0] & 0x40) != 0 || (frame[1] & 0x03) != 0x02 ||
		    memcmp(frame + 10, bssid, 6) != 0) {
			return (false);
		}
		to = frame + 4;
		from = frame + 16;
		if ((frame[0] & 0x80) != 0) {
			header += 2 + ((frame[1] & 0x80) != 0 ? FA_FRAME_HT_CONTROL : 0);
		}
		if ((frame[1] & 0x40) != 0 && !cipher_header_gone) {
			if (length < header + 8) {
				return (false);
			}
			data->cipher = frame + header;
			header += 8;
		}
	}
	if (((to[0] & 1) == 0 && memcmp(to, own, 6) != 0) || memcmp(from, own, 6) == 0) {
		return (false);
	}
	if (ethernet) {
		data->type = fa_be16(frame + 12);
	} else {
		static const uint8_t llc[6] = { 0xaa, 0xaa, 0x03, 0x00, 0x00, 0x00 };

		if (length < header + FA_LLC_LENGTH || memcmp(frame + header, llc, sizeof(llc)) != 0) {
			return (false);
		}
		data->type = fa_be16(frame + header + 6);
		header += FA_LLC_LENGTH;
	}
	data->body = frame + header;
	data->length = length - header;
	memcpy(data->from, from, 6);
	memcpy(data->to, to, 6);
	return (true);
}

/*
 * A frame for the air through the access point, to `to`: 802.11 data with the protected bit, QoS data when it does QoS.
 * The firmware ciphers it and writes the cipher's header (mac.c: for CCMP mac80211 is told to make one for management
 * frames only). The number of bytes, 0 when they do not fit.
 */
static inline unsigned
fa_data_frame(uint8_t *out, unsigned size, const uint8_t *own, const uint8_t *bssid, const uint8_t *to, bool qos, unsigned type,
    const uint8_t *body, unsigned length)
{
	static const uint8_t llc[6] = { 0xaa, 0xaa, 0x03, 0x00, 0x00, 0x00 };
	unsigned header = qos ? FA_QOS_HEADER : FA_FRAME_HEADER;

	if (size < header + FA_LLC_LENGTH + length) {
		return (0);
	}
	memset(out, 0, header);
	out[0] = qos ? 0x88 : 0x08;
	out[1] = 0x41;				/* to the distribution system, protected */
	memcpy(out + 4, bssid, 6);
	memcpy(out + 10, own, 6);
	memcpy(out + 16, to, 6);
	memcpy(out + header, llc, sizeof(llc));
	fa_put16(out + header + 6, type);
	memcpy(out + header + FA_LLC_LENGTH, body, length);
	return (header + FA_LLC_LENGTH + length);
}

/* ---- IP ---------------------------------------------------------------------------------------------------- */

static inline void
fa_ip_header(uint8_t *out, unsigned protocol, const uint8_t *from, const uint8_t *to, unsigned total, unsigned id)
{
	memset(out, 0, FA_IP_HEADER);
	out[0] = 0x45;
	fa_put16(out + 2, total);
	fa_put16(out + 4, id);
	out[8] = 64;
	out[9] = (uint8_t)protocol;
	memcpy(out + 12, from, 4);
	memcpy(out + 16, to, 4);
	fa_put16(out + 10, fa_ip_sum(out, FA_IP_HEADER));
}

/* The payload of a whole, unfragmented IPv4 datagram with a right header. NULL else; *length is what the datagram says it holds. */
static inline const uint8_t *
fa_ip_read(const uint8_t *datagram, unsigned size, unsigned protocol, unsigned *length)
{
	unsigned header, total;

	if (size < FA_IP_HEADER || (datagram[0] >> 4) != 4) {
		return (NULL);
	}
	header = (datagram[0] & 0x0fu) * 4;
	total = fa_be16(datagram + 2);
	if (header < FA_IP_HEADER || total < header || total > size || fa_ip_sum(datagram, header) != 0) {
		return (NULL);
	}
	if ((fa_be16(datagram + 6) & 0x3fff) != 0 || datagram[9] != protocol) {
		return (NULL);
	}
	*length = total - header;
	return (datagram + header);
}

/* ---- DHCP -------------------------------------------------------------------------------------------------- */

/*
 * A message of ours. `requested` and `server` are options 50 and 54, `client` the address we already hold (a release);
 * each may be NULL. With `everybody` the server is asked to answer to all. The number of bytes, 0 when they do not fit.
 */
static inline unsigned
fa_dhcp_build(uint8_t *out, unsigned size, unsigned kind, const uint8_t *own, uint32_t xid, bool everybody, const uint8_t *requested,
    const uint8_t *server, const uint8_t *client)
{
	static const uint8_t none[4] = { 0, 0, 0, 0 }, all[4] = { 255, 255, 255, 255 };
	static const uint8_t asked[] = { 55, 5, 1, 3, 6, 51, 54 };
	uint8_t *udp = out + FA_IP_HEADER, *bootp = udp + FA_UDP_HEADER, *option = bootp + FA_BOOTP_FIXED;

	if (size < FA_DHCP_LENGTH) {
		return (0);
	}
	memset(out, 0, FA_DHCP_LENGTH);
	fa_ip_header(out, FA_PROTO_UDP, client != NULL ? client : none, client != NULL && server != NULL ? server : all, FA_DHCP_LENGTH, xid & 0xffff);
	fa_put16(udp, FA_PORT_CLIENT);
	fa_put16(udp + 2, FA_PORT_SERVER);
	fa_put16(udp + 4, FA_UDP_HEADER + FA_BOOTP_LENGTH);		/* a checksum of 0 says there is none (RFC 768) */
	bootp[0] = 1;
	bootp[1] = 1;
	bootp[2] = 6;
	bootp[4] = (uint8_t)(xid >> 24);
	bootp[5] = (uint8_t)(xid >> 16);
	bootp[6] = (uint8_t)(xid >> 8);
	bootp[7] = (uint8_t)xid;
	if (everybody) {
		bootp[10] = 0x80;
	}
	if (client != NULL) {
		memcpy(bootp + 12, client, 4);
	}
	memcpy(bootp + 28, own, 6);
	*option++ = 99;
	*option++ = 130;
	*option++ = 83;
	*option++ = 99;
	*option++ = 53;
	*option++ = 1;
	*option++ = (uint8_t)kind;
	*option++ = 61;
	*option++ = 7;
	*option++ = 1;
	memcpy(option, own, 6);
	option += 6;
	if (requested != NULL) {
		*option++ = 50;
		*option++ = 4;
		memcpy(option, requested, 4);
		option += 4;
	}
	if (server != NULL) {
		*option++ = 54;
		*option++ = 4;
		memcpy(option, server, 4);
		option += 4;
	}
	if (kind != FA_DHCP_RELEASE) {
		memcpy(option, asked, sizeof(asked));
		option += sizeof(asked);
	}
	*option = 255;
	return (FA_DHCP_LENGTH);
}

struct fa_dhcp_reply {
	unsigned	kind;
	uint8_t		address[4], server[4], router[4], mask[4], name_server[4];
	uint32_t	lease;
	bool		has_server, has_router, has_mask, has_name_server;
};

/* A server's answer to this station's transaction, read from a datagram. False for anything else. */
static inline bool
fa_dhcp_read(const uint8_t *datagram, unsigned size, const uint8_t *own, uint32_t xid, struct fa_dhcp_reply *reply)
{
	unsigned length, at, end;
	const uint8_t *udp = fa_ip_read(datagram, size, FA_PROTO_UDP, &length), *bootp;

	memset(reply, 0, sizeof(*reply));
	if (udp == NULL || length < FA_UDP_HEADER + FA_BOOTP_FIXED + 4 || fa_be16(udp) != FA_PORT_SERVER || fa_be16(udp + 2) != FA_PORT_CLIENT) {
		return (false);
	}
	if (fa_be16(udp + 4) < FA_UDP_HEADER + FA_BOOTP_FIXED + 4 || fa_be16(udp + 4) > length) {
		return (false);
	}
	bootp = udp + FA_UDP_HEADER;
	end = fa_be16(udp + 4) - FA_UDP_HEADER;
	if (bootp[0] != 2 || bootp[1] != 1 || bootp[2] != 6 || fa_be32(bootp + 4) != xid || memcmp(bootp + 28, own, 6) != 0 ||
	    bootp[236] != 99 || bootp[237] != 130 || bootp[238] != 83 || bootp[239] != 99) {
		return (false);
	}
	memcpy(reply->address, bootp + 16, 4);
	at = FA_BOOTP_FIXED + 4;
	while (at < end && bootp[at] != 255) {
		unsigned code = bootp[at], n;
		const uint8_t *value;

		if (code == 0) {
			at++;
			continue;
		}
		if (end - at < 2 || end - at - 2 < bootp[at + 1]) {
			return (false);
		}
		n = bootp[at + 1];
		value = bootp + at + 2;
		switch (code) {
		case 53:
			reply->kind = n == 1 ? value[0] : 0;
			break;
		case 1:
			if (n == 4) {
				memcpy(reply->mask, value, 4);
				reply->has_mask = true;
			}
			break;
		case 3:
			if (n >= 4 && n % 4 == 0) {
				memcpy(reply->router, value, 4);
				reply->has_router = true;
			}
			break;
		case 6:
			if (n >= 4 && n % 4 == 0) {
				memcpy(reply->name_server, value, 4);
				reply->has_name_server = true;
			}
			break;
		case 51:
			if (n == 4) {
				reply->lease = fa_be32(value);
			}
			break;
		case 54:
			if (n == 4) {
				memcpy(reply->server, value, 4);
				reply->has_server = true;
			}
			break;
		default:
			break;
		}
		at += 2 + n;
	}
	return (reply->kind == FA_DHCP_NAK || ((reply->kind == FA_DHCP_OFFER || reply->kind == FA_DHCP_ACK) && reply->has_server &&
	    (reply->address[0] | reply->address[1] | reply->address[2] | reply->address[3]) != 0));
}

/* ---- ARP and the echo -------------------------------------------------------------------------------------- */

static inline unsigned
fa_arp_request(uint8_t *out, unsigned size, const uint8_t *own, const uint8_t *own_address, const uint8_t *wanted)
{
	static const uint8_t head[8] = { 0, 1, 8, 0, 6, 4, 0, 1 };

	if (size < FA_ARP_LENGTH) {
		return (0);
	}
	memcpy(out, head, sizeof(head));
	memcpy(out + 8, own, 6);
	memcpy(out + 14, own_address, 4);
	memset(out + 18, 0, 6);
	memcpy(out + 24, wanted, 4);
	return (FA_ARP_LENGTH);
}

/* The hardware address of `wanted`, if this is its answer to `own_address`. */
static inline bool
fa_arp_read(const uint8_t *body, unsigned length, const uint8_t *own_address, const uint8_t *wanted, uint8_t *hardware)
{
	static const uint8_t head[8] = { 0, 1, 8, 0, 6, 4, 0, 2 };

	if (length < FA_ARP_LENGTH || memcmp(body, head, sizeof(head)) != 0 || memcmp(body + 14, wanted, 4) != 0 || memcmp(body + 24, own_address, 4) != 0) {
		return (false);
	}
	memcpy(hardware, body + 8, 6);
	return (true);
}

static inline unsigned
fa_echo_request(uint8_t *out, unsigned size, const uint8_t *from, const uint8_t *to, unsigned id, unsigned sequence)
{
	uint8_t *icmp = out + FA_IP_HEADER;

	if (size < FA_ECHO_LENGTH) {
		return (0);
	}
	fa_ip_header(out, FA_PROTO_ICMP, from, to, FA_ECHO_LENGTH, id + sequence);
	memset(icmp, 0, 8);
	icmp[0] = 8;
	fa_put16(icmp + 4, id);
	fa_put16(icmp + 6, sequence);
	for (unsigned i = 0; i < FA_ECHO_DATA; i++) {
		icmp[8 + i] = (uint8_t)(0x20 + i);
	}
	fa_put16(icmp + 2, fa_ip_sum(icmp, 8 + FA_ECHO_DATA));
	return (FA_ECHO_LENGTH);
}

/* Whether this datagram is `from`'s answer to that echo. */
static inline bool
fa_echo_read(const uint8_t *datagram, unsigned size, const uint8_t *from, const uint8_t *to, unsigned id, unsigned sequence)
{
	unsigned length;
	const uint8_t *icmp = fa_ip_read(datagram, size, FA_PROTO_ICMP, &length);

	return (icmp != NULL && length >= 8 && memcmp(datagram + 12, from, 4) == 0 && memcmp(datagram + 16, to, 4) == 0 && icmp[0] == 0 && icmp[1] == 0 &&
	    fa_be16(icmp + 4) == id && fa_be16(icmp + 6) == sequence && fa_ip_sum(icmp, length) == 0);
}

/* ---- answering, for stage 7's load test ------------------------------------------------------------------- */

/* Whether this is an ARP request for `own_address`. */
static inline bool
fa_arp_asks_for(const uint8_t *body, unsigned length, const uint8_t *own_address)
{
	static const uint8_t head[8] = { 0, 1, 8, 0, 6, 4, 0, 1 };

	return (length >= FA_ARP_LENGTH && memcmp(body, head, sizeof(head)) == 0 && memcmp(body + 24, own_address, 4) == 0);
}

/* The answer to such a request, for the hardware address at body + 8. The number of bytes, 0 when it is not one or does not fit. */
static inline unsigned
fa_arp_reply(uint8_t *out, unsigned size, const uint8_t *own, const uint8_t *own_address, const uint8_t *body, unsigned length)
{
	static const uint8_t head[8] = { 0, 1, 8, 0, 6, 4, 0, 2 };

	if (size < FA_ARP_LENGTH || !fa_arp_asks_for(body, length, own_address)) {
		return (0);
	}
	memcpy(out, head, sizeof(head));
	memcpy(out + 8, own, 6);
	memcpy(out + 14, own_address, 4);
	memcpy(out + 18, body + 8, 6);
	memcpy(out + 24, body + 14, 4);
	return (FA_ARP_LENGTH);
}

/* The ICMP message of an echo request to `own_address` with a right checksum, or NULL. *length is its size. */
static inline const uint8_t *
fa_echo_asks(const uint8_t *datagram, unsigned size, const uint8_t *own_address, unsigned *length)
{
	const uint8_t *icmp = fa_ip_read(datagram, size, FA_PROTO_ICMP, length);

	if (icmp == NULL || *length < 8 || memcmp(datagram + 16, own_address, 4) != 0 || icmp[0] != 8 || icmp[1] != 0 || fa_ip_sum(icmp, *length) != 0) {
		return (NULL);
	}
	return (icmp);
}

/* The answer to such a request, with its identifier, number and data. The number of bytes, 0 when it is not one or does not fit. */
static inline unsigned
fa_echo_reply(uint8_t *out, unsigned size, const uint8_t *own_address, const uint8_t *datagram, unsigned length)
{
	unsigned message;
	const uint8_t *icmp = fa_echo_asks(datagram, length, own_address, &message);

	if (icmp == NULL || message > size || size - message < FA_IP_HEADER) {
		return (0);
	}
	fa_ip_header(out, FA_PROTO_ICMP, own_address, datagram + 12, FA_IP_HEADER + message, fa_be16(datagram + 4));
	memcpy(out + FA_IP_HEADER, icmp, message);
	out[FA_IP_HEADER] = 0;
	out[FA_IP_HEADER + 2] = out[FA_IP_HEADER + 3] = 0;
	fa_put16(out + FA_IP_HEADER + 2, fa_ip_sum(out + FA_IP_HEADER, message));
	return (FA_IP_HEADER + message);
}

/* Whether the poll thread should keep this while stage 7 answers: a request that is for us. */
static inline bool
fa_net_served(unsigned type, const uint8_t *body, unsigned length, const uint8_t *own_address)
{
	unsigned message;

	if (length > FA_NET_MOST) {
		return (false);
	}
	return ((type == FA_TYPE_ARP && fa_arp_asks_for(body, length, own_address)) ||
	    (type == FA_TYPE_IP && fa_echo_asks(body, length, own_address, &message) != NULL));
}

/* Whether the poll thread should keep this for the join: an ARP answer, an echo answer, or something for the DHCP client port. */
static inline bool
fa_net_wanted(unsigned type, const uint8_t *body, unsigned length)
{
	unsigned payload;
	const uint8_t *at;

	if (length > FA_NET_MOST) {
		return (false);
	}
	if (type == FA_TYPE_ARP) {
		return (length >= FA_ARP_LENGTH && body[7] == 2);
	}
	if (type != FA_TYPE_IP) {
		return (false);
	}
	if ((at = fa_ip_read(body, length, FA_PROTO_ICMP, &payload)) != NULL) {
		return (payload >= 8 && at[0] == 0);
	}
	at = fa_ip_read(body, length, FA_PROTO_UDP, &payload);
	return (at != NULL && payload >= FA_UDP_HEADER && fa_be16(at + 2) == FA_PORT_CLIENT);
}

#endif
