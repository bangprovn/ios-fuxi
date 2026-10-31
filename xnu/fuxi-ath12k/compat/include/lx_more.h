/*
 * What ath12k uses of Linux and LinuxKPI's headers (FreeBSD 15.1) do not declare. Small things
 * each; where one needs a function, it is in compat/lx_base.c or src/fa_80211.c.
 *
 * Adapted from hackios wifi/lx/include/lx_more.h (MIT), see compat/PROVENANCE.md. The numeric
 * values below come from IEEE 802.11-2020 and 802.11be-2024 field definitions.
 */
#ifndef _LX_MORE_H_
#define _LX_MORE_H_

#include <linux/types.h>
#include <linux/interrupt.h>
#include <linux/dma-mapping.h>
#include <linux/module.h>
#include <linux/ieee80211.h>
#include <linux/list.h>
#include <linux/rcupdate.h>
#include <linux/skbuff.h>
#include <linux/spinlock.h>
#include <net/cfg80211.h>
#include <fa_radiotap_eht.h>

/* --- interrupts: how the line is triggered is the kernel's knowledge, not the driver's */
#define IRQF_TRIGGER_RISING	0x0001
#define IRQ_NOAUTOEN		(1 << 12)	/* request_irq() leaves the interrupt disabled */
#define IRQ_DISABLE_UNLAZY	(1 << 19)	/* disable_irq() masks at once: it always does here */
int enable_irq_wake(unsigned int irq);
int disable_irq_wake(unsigned int irq);

/* the phone is not woken by its radio: nothing here suspends */
static inline bool device_may_wakeup(struct device *dev) { return (false); }

/* --- DMA: memory for the device that need not be coherent, and the barriers around ring indices */
void *dma_alloc_noncoherent(struct device *dev, size_t size, dma_addr_t *handle,
    enum dma_data_direction dir, gfp_t gfp);
void dma_free_noncoherent(struct device *dev, size_t size, void *vaddr, dma_addr_t handle,
    enum dma_data_direction dir);
#ifndef dma_rmb
#define dma_rmb()	rmb()
#define dma_wmb()	wmb()
#define dma_mb()	mb()
#endif

/* --- a table of device ids is found through the driver (of_match_table), not through a section */
#undef MODULE_DEVICE_TABLE
#define MODULE_DEVICE_TABLE(bus, table)

/* --- no lockdep in this domain, as a Linux kernel built without it */
#define RCU_LOCKDEP_WARN(condition, message)	do { } while (0)
#define rcu_read_lock_any_held()		rcu_read_lock_held()

/* --- lists */
#define LIST_HEAD_INIT(name)	{ &(name), &(name) }

/* Everything of `head` before `entry` moves to `list`; `entry` becomes the first of `head`. */
static inline void
list_cut_before(struct list_head *list, struct list_head *head, struct list_head *entry)
{
	if (head->next == entry) {
		INIT_LIST_HEAD(list);
		return;
	}
	list->next = head->next;
	list->next->prev = list;
	list->prev = entry->prev;
	list->prev->next = list;
	head->next = entry;
	entry->prev = head;
}

/* --- locks */
DEFINE_LOCK_GUARD_1(spinlock_bh,
    spinlock_t,
    spin_lock_bh(_T->lock),
    spin_unlock_bh(_T->lock))

/*
 * --- socket buffers. All buffers in this domain are one linear piece, so the "trailer" of a
 * buffer is the buffer itself and there is no room to be made beyond what its slot has left.
 */
static inline int
skb_cow_data(struct sk_buff *skb, int tailbits, struct sk_buff **trailer)
{
	if (skb_tailroom(skb) < tailbits)
		return (-ENOMEM);
	*trailer = skb;
	return (1);
}

static inline void *
pskb_put(struct sk_buff *skb, struct sk_buff *tail, int len)
{
	return (skb_put(tail, len));
}

/* --- 802.11 values LinuxKPI has no name for */
#define KHZ_TO_MHZ(f)				((f) / 1000)
#define WLAN_EID_EXTENSION			255
/* radio measurement action frames (IEEE 802.11-2020, 9.6.6) */
#define WLAN_CATEGORY_RADIO_MEASUREMENT			5
#define WLAN_RM_ACTION_LINK_MEASUREMENT_REQUEST		2
#define WLAN_RM_ACTION_LINK_MEASUREMENT_REPORT		3
/* the driver wants to hear of every change of regulatory domain it brought about itself */
#define WIPHY_FLAG_NOTIFY_REGDOM_BY_DRIVER		BIT(24)
#define IEEE80211_TPE_EIRP_ENTRIES_320MHZ	5
#define IEEE80211_TPE_PSD_ENTRIES_320MHZ	16
#define WLAN_EXT_CAPA11_BCN_PROTECT		BIT(84 % 8)
#define IEEE80211_EHT_OPER_MCS15_DISABLE	0x40
/*
 * Same field as LinuxKPI's other IEEE80211_EHT_PHY_CAP4_* defines, which hold placeholder counters
 * rather than the standard's bits. Used only in ath12k's mesh filter; see compat/PROVENANCE.md.
 */
#define IEEE80211_EHT_PHY_CAP4_PSR_SR_SUPP	0x02

/* --- what the edits of tools/ath12k-edits.json call in the driver's files */
void lx_vlog(const char *format, va_list list);
void lx_vlog_ratelimited(const char *format, va_list list);
void *lx_dma_zalloc(size_t size);
void lx_dma_free(void *memory, size_t size);
void fa_radio_mac(u8 *mac);		/* src/fa_80211.c: design section 4, station.address */
struct ath12k_base;
bool fa_stage_htc_ready(struct ath12k_base *ab);	/* src/fa_hif.c: stops ath12k_core_start at stage 1 */
bool fa_stage_registered(struct ath12k_base *ab);	/* src/fa_hif.c: stops the hardware group's start at stage 2 */
void fa_wmi_event(u32 id, u32 length);			/* src/fa_hif.c: the order firmware's WMI events come in */
bool fa_refuse_eht(void);				/* src/fa_80211.c: says so once; always true */

/* --- the six functions LinuxKPI 15.1 lacks (design section 4) */

/* The extension element `ext` in a list of elements, or NULL. */
static inline const struct element *
cfg80211_find_ext_elem(u8 ext, const u8 *ies, unsigned int len)
{
	const u8 *at = ies, *end = ies + len;

	while (end - at >= 2 && end - at - 2 >= at[1]) {
		if (at[0] == WLAN_EID_EXTENSION && at[1] >= 1 && at[2] == ext)
			return ((const struct element *)at);
		at += 2 + at[1];
	}
	return (NULL);
}

/*
 * Length of the Supported EHT-MCS And NSS Set: 4 for a 20 MHz-only non-AP station, otherwise 3
 * for each of <=80, 160 and 320 MHz that the capabilities claim. Literal bits of the standard's
 * HE PHY capability byte 0 (channel width set) and EHT PHY capability byte 0.
 */
static inline u8
ieee80211_eht_mcs_nss_size(const struct ieee80211_he_cap_elem *he,
    const struct ieee80211_eht_cap_elem_fixed *eht, bool from_ap)
{
	u8 size = 3;

	if (!from_ap && (he->phy_cap_info[0] & 0x1e) == 0)
		return (4);
	if (he->phy_cap_info[0] & 0x08)
		size += 3;
	if (eht->phy_cap_info[0] & 0x02)
		size += 3;
	return (size);
}

/*
 * EML Capabilities (802.11be Figure 9-1002w): padding delay in bits 1-3, transition delay in bits
 * 4-6, transition timeout in bits 11-14. Only reached with EMLSR, that is with MLO, which this
 * domain does not enable. LinuxKPI's IEEE80211_EML_CAP_* values other than EMLSR_SUPP are
 * placeholders, so these read the bits directly.
 */
static inline u32
ieee80211_emlsr_pad_delay_in_us(u16 eml_cap)
{
	u32 code = (eml_cap >> 1) & 0x7;

	return (code == 0 || code > 4 ? 0 : 32u << (code - 1));
}

static inline u32
ieee80211_emlsr_trans_delay_in_us(u16 eml_cap)
{
	u32 code = (eml_cap >> 4) & 0x7;

	return (code == 0 || code > 5 ? 0 : 16u << (code - 1));
}

static inline u32
ieee80211_eml_trans_timeout_in_us(u16 eml_cap)
{
	u32 code = (eml_cap >> 11) & 0xf;

	return (code == 0 || code > 11 ? 0 : 128u << (code - 1));
}

/* Whether mac80211 holds this transmit queue stopped; the station layer knows (src/fa_80211.c). */
struct ieee80211_hw;
int ieee80211_queue_stopped(struct ieee80211_hw *hw, int queue);

#endif
