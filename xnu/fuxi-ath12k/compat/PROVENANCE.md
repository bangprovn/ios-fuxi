# Provenance of xnu/fuxi-ath12k/compat

Adapted from hackios (`oosexgang/hakios-xnu-kernel`) revision `85bb05f0a4ed09e4e275f64047afed0122f59180`,
directory `wifi/lx`. That directory is under the MIT License by the repository's `LICENSE`; its text and
notice are kept in `LICENSE.hackios` and apply to the adapted files and to the changes made here.

| Here | From `wifi/lx/` | Change |
|---|---|---|
| `lx_base.c` | `src/lx_base.c` | 80 MiB pool, free list for rings, 4 KiB alignment for pieces of a page or more (R2), waiting `timer_delete_sync`, `lx_vlog`, firmware answers ENOENT, device-tree answers, no `_ioremap_attr` |
| `lx.h` | `src/lx.h` | Only the base functions |
| `include/lx_prelude.h` | `include/lx_prelude.h` | PAGE_SIZE assertion (R2) |
| `include/lx_more.h` | `include/lx_more.h` | The six missing functions and other small gaps for ath12k |
| `include/linux/{irq,of,of_reserved_mem,remoteproc,rhashtable}.h` | `include/linux/` | Comments; `of_reserved_mem` answers "none" |
| `include/machine/pcpu.h`, `include/sys/{cdefs,linker_set}.h` | `include/` | `__GLOBL` dropped; sysinit set dropped |
| LinuxKPI edits `lx-device-of-node`, `lx-bss-conf-fields`, `lx-reg-rule-psd`, `lx-channel-psd`, `lx-regd-sync-copies` in `tools/ath12k-edits.json` | `patches/` | Same effect, restated as exact-match edits |
| `src/fa_80211.c` (first part) | `src/lx_80211.c` | Radio allocation, regulatory domain, channel arithmetic; the station is not there yet |

Written for this tree: `include/lx_os.h` (the call list has the shape of hackios `bsd/sys/hakios.lx.h`, which is
APSL and was not copied), `include/fa_radiotap_eht.h` (values from radiotap.org), and the stand-ins
`include/linux/{acpi,bug,clk,ctype,of_graph,overflow,panic_notifier,rtnetlink}.h` and
`include/linux/remoteproc/qcom_rproc.h`.

`include/linux/overflow.h` replaces LinuxKPI's copy of Linux's header. That copy is `GPL-2.0 OR MIT`; it could be
taken under MIT, but this domain's gate refuses any license line naming GPL, so it is not read at all.

Nothing here comes from hakios-arm64e-dada or UniXNU/Ath12kWifi (findings only), and nothing from Linux outside
`drivers/net/wireless/ath/ath12k` (fetched at build time, BSD-3-Clause-Clear, not committed).

## Known placeholders in LinuxKPI 15.1 that this layer does not fix

`net/cfg80211.h` defines `IEEE80211_EHT_PHY_CAP*`, `IEEE80211_EHT_PPE_THRES_*`, `IEEE80211_EHT_MAC_CAP*` and
`IEEE80211_EML_CAP_*` (other than `EMLSR_SUPP`) as running counters, not the standard's bit values. ath12k's EHT
capability filter and PPE threshold code (`mac.c` around lines 3301-3390 and 8717-8848) therefore test the wrong
bits. Until those are corrected the station layer must not offer EHT. `IEEE80211_EHT_PHY_CAP0_320MHZ_IN_6GHZ` happens
to be right. `lx_more.h`'s `IEEE80211_EHT_PHY_CAP4_PSR_SR_SUPP` uses the standard's value and is used only in the
mesh filter, whose siblings are wrong anyway.
