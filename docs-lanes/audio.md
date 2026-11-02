# Audio lane

## Where it stands, 2 November (device results; the log is research_notes/audio-next.md, which is not in this repository)

| What | State | Run |
|---|---|---|
| ADSP boots from the loader and stays up under iOS | works | `a-ios3`, 30 min in `a-ios6` |
| Graph and buffers, loader and kernel, TDM clock and 4 slots | works, 0 errors | `a-play2`, `a-kmic1` |
| iOS's mix reaches the DSP through the HAL plug-in | works: a ringtone from Settings, peaks 6553 and 11116, 0 dropped | `a-ios5` |
| CS35L41 B and T identify over I2C | DEVID 0x35A40 REV B2 | `a-amp2` |
| Bottom speaker from the loader at the lowest level (-30 dB, analog 0.5 dB, 2 kHz at -20 dBFS, 1 s) | no flags, PLL locked; coil current in IMON | `a-tone1`, `a-tone5` |
| Mic: TX clock 0x30C, macros, SoundWire master, WCD938x enumerated, bias 2.75 V, ADC1 | 7 of 7 proof steps | `a-mic2` |
| Mic capture through the DSP in the loader | 1641 periods, 0 bad. Gain 8 to 16: four times the level. Bias off: sample-to-sample change falls twelve times | `a-cap2` |
| The speaker heard by the mic, at the same lowest level | 2 kHz: 0.167 LSB before, 25.28 during, 0.754 after. Bias off: 0.019, 0.053, 0.041 | `a-loop1`, `a-loop2` |
| Mic in the XNU kernel, `fuxiaudio=4` | 7 of 7, 772 periods, 0 bad, came back down | `a-kmic1` |
| The bottom speaker heard by ear | the loader's tone at -30 dB, and iOS's alert tones through `fuxiaudioamp=1`: right tones, clear and clean, very quiet at half volume. 21 s without a bad buffer or a flag | `a-tone7`, `a-spk1` |
| `fuxiaudioamp=2` and `=3` (-20, -12 dB), the top speaker | written or not, **not run: each needs the owner's word** | |
| `/dev/fuxiaudiomic` read by the plug-in in mediaserverd | not proved. `fuxisbext=1` gets Safari past TCC to the prompt and the grant; the capture does not start: no StartIOProc on "Codec", no kernel mic line | `a-v8b`, `a-v8c` |

Only numbers leave the phone in the mic's runs; the probes have no request that returns its samples.

## Milestone 1 (research + probes)

Goal: iOS sound on the fuxi speakers, then headphones over USB-C. This round
covers research, a read-only loader probe, and a gated ADSP PAS boot stage. No
XNU driver yet. **No device result is claimed in this file**; every
"observed" below comes from the OrangeFox recovery captures in `research/device/`.

Source tags: **[DT]** live DT `research/device/orangefox-live.dtb`;
**[dmesg]** `research/device/orangefox-dmesg.txt`; **[cmd-db]**
`research/device/orangefox-cmd-db.txt`; **[Lin]** Linux 6.12 source file;
**[Lineage]** `research/lineage-fuxi` / `lineage-sm8550-common`; **[gcc]**
`research/gcc-kalama.c`; **[ipsw]** static analysis of the iPhone X 16.x
image (kernelcache, rootfs and dyld shared cache strings/plists, no disassembly).
**GUESS** marks anything that is not verified.

## 1. Fuxi audio topology

### Speakers (facts)

| Item | Value | Source |
|---|---|---|
| Amps | 2 x Cirrus CS35L41 (`cirrus,cs35l41`) | [DT] |
| Bus | `qupv3_2` (wrap2) SE0 `i2c@880000`, `qcom,i2c-geni`, gpi-dma@800000 | [DT] |
| Amp "B" (bottom, music/right) | I2C 0x40, reset gpio183, IRQ gpio99 | [DT] |
| Amp "T" (top, handset + music) | I2C 0x42, reset gpio182, IRQ gpio98, receiver switch gpio68 (`rcv_gpio_high/low`) | [DT] |
| Boost | 15 uF / 1000 nH / 4000 mA peak, both amps | [DT] |
| Amp supplies | none in DT (**GUESS**: always-on VPH/1.8 V) | [DT] |
| I2C pins | gpio0 SDA / gpio1 SCL, func `qup2_se0_l*_mirb` | [DT], [Lin] pinctrl-sm8550.c |
| QUP clocks | `GCC_QUPV3_WRAP2_S0_CLK`, wrap2 m/s AHB; vote reg 0x52010 | [DT], [gcc] |
| Audio data | `PAL_DEVICE_OUT_SPEAKER` -> `TDM-LPAIF-RX-SECONDARY` | [Lineage] resourcemanager_kalama_mtp.xml |
| TDM pins | gpio121 i2s1_sck, gpio123 i2s1_ws, gpio122 i2s1_data0 (in), gpio124 i2s1_data1 (out); SoC is master | [DT] `sec_tdm_pinctrl`, sound card `msm-mi2s-master` |
| Slots | T = ASPRX1 slot 0, B = ASPRX1 slot 1, tdm-max-slots 4 | [Lineage] mixer_paths, [DT] |
| Amp DSP | "PCM Source" = DSP, DSP1 firmware "Protection" (speaker protection on the amp) | [Lineage] mixer_paths |
| Sound card | `kalama-mtp-snd-card` | [DT] |

### Headphones / USB-C (facts)

- WCD938x codec over SoundWire, through the lpass-cdc rx/tx/va/wsa macros. These
  macros are clocked and owned by the ADSP. [DT]
- fsa4480 at i2c 0-0042 switches USB-C analog audio. [DT]
- `usb_audio_qmi` exists, which means USB audio offload runs through the ADSP. [DT]/[Lineage]

### ADSP (facts)

- `remoteproc-adsp@03000000`, `qcom,kalama-adsp-pas`, `cx-supply` lcx and
  `mx-supply` lmx levels, `qcom,qmp` = aoss. [DT]
- Carveouts: `adspslpi_region` 0x9ea00000+0x4680000 and `q6_adsp_dtb_region`
  0x9e980000+0x80000. [DT]
- smp2p-adsp: `qcom,smem = <443 429>`, local pid 0 / remote pid 2, IPCC mbox
  <3 2>. Inbound "slave-kernel" bits: fatal 0, ready 1, handover 2,
  stop-ack 3, shutdown-ack 7. [DT]
- glink-edge (pid 2): IPCRTR, fastrpcglink-apps-dsp, sleepmon,
  PMIC_RTR_ADSP_APPS (battery/charger runs **on the ADSP**), PMIC_LOGS, and
  `adsp_apps` (GPR: spf_core, audio-pkt, q6prm). [DT]
- Linux `sm8550_adsp_resource`: adsp.mdt pas_id 1, adsp_dtb.mdt dtb_pas_id
  0x24, crash reason SMEM item 423, proxy power domains lcx/lmx, load_state
  "adsp". [Lin] qcom_q6v5_pas.c
- RPMh: lcx.lvl 0x30030 (8 levels, hlvl 7 = 384 TURBO), lmx.lvl 0x30040
  (5 levels, hlvl 4 = 384). The DT proxy vote is 0x180 = 384. [cmd-db], [DT]
- **Observed** in recovery: adsp.mdt (8524 B) with segments b01..b58 and
  adsp_dtb.mdt with b01..b02 load; "remote processor ... is now up" about 0.5 s
  later; the GPR devices are added and pmic_glink works. Then
  `spf_core_add_child_devices: apm is not up`, so the audio PD/APM never came
  up in recovery. **GUESS**: the userspace pieces were missing (pd-mapper/servreg,
  audioadsprpcd, ACDB loading). [dmesg]

## 2. Bring-up paths

| Path | What it needs | Effort | Risk |
|---|---|---|---|
| **A. ADSP + AudioReach** (production) | PAS boot (stage 2 below), QMP, RPMh, SMEM/smp2p, glink-SMEM (AP allocates FIFOs 478-480 under the TCSR hwspinlock), QRTR, a pd-mapper/servreg QMI responder, probably a fastrpc listener for dynamic modules (capi_cirrus_sp.so), GPR/APM graph build without ACDB (upstream q6apm style: CODEC_DMA/TDM end point, PCM decoder, shared-memory push), q6prm clock votes, and amp setup over I2C | weeks | medium: well-trodden upstream; recovery shows the APM may not start without the right userspace |
| **B. AP drives LPASS LPAIF directly** | LPASS core/audio clock controllers, LPAIF DMA from the AP | high | high: no Linux HLOS driver exists for SM8550 LPASS CCs (ADSP owns them), and **GUESS** XPU-protected. Not recommended |
| **C. Bit-banged TDM + amps over I2C** ("first beep") | QUP2 SE0 I2C (GENI FIFO mode) to configure both CS35L41 for I2S/TDM, PCM Source = ASP; a dedicated CPU core toggles gpio121/123/124 (GPIO function) at a low rate, e.g. 8 kHz x 2 x 16 bit = 256 kHz SCLK, which is a valid CS35L41 PLL reference ([Lin] cs35l41 PLL table) | days | medium: timing jitter; no speaker protection, so use a low digital volume and short tones; amp DSP firmware not needed if the PCM source is ASP |

Recommendation: do **C** first to prove the amps, I2C, pins and the iOS HAL path
end to end, and keep **A** as the real target (it is also needed for
charging telemetry and WCD938x headphones). Path A starts with the stage 2 probe.

USB-C headphones: digital dongles need the DWC3 in host mode. The iPhone X
kernelcache has **no xHCI driver** (EHCI/OHCI/Synopsys-OTG device only)
[ipsw], so this needs a custom xHCI kext or a UserHCI/DriverKit controller.
After that, `AppleUSBAudio` (kext + HAL plug-in, both present [ipsw]) would
**GUESS** drive class-compliant dongles. Analog USB-C goes through
fsa4480 + WCD938x and needs path A.

## 3. What iOS 16 needs on top [ipsw]

Facts:
- **Kernel family.** `IOAudio2Family` has only two classes: `IOAudio2Device`
  (keys `exclusive access owner`, `is running`) and `IOAudio2DeviceUserClient`
  (entitlement `com.apple.private.audio.driver-host`, which mediaserverd holds).
  The concrete iPhone X drivers are AppleEmbeddedAudio, AppleARMIISAudio,
  AppleCS42L75Audio, AppleCS35L26Amp and AppleAOPAudio. There is **no
  IOAudioFamily** in the kernelcache. AppleUSBAudio is present.
- **HAL host.** The HAL runs inside **mediaserverd**; iOS 16 has no coreaudiod.
  `CoreAudio.framework` contains `HALS_IOA2Manager`/`HALS_IOA2UCDevice`, which
  match the `IOAudio2Device` class. It also has an IOAudio1 back end and an
  AudioDriverKit (`HALS_IOUA*`) back end.
- **Registry keys the HAL reads.** `device UID`, `device name`, `device
  manufacturer`, `model UID`, `input/output latency`, `io buffer frame size`,
  `input/output safety offset`, `input/output streams` (`stream ID`,
  `current/available formats`, `starting channel`, `terminal type`, `buffer
  mapping options`), `controls`, `data exchange blocks`, `supports
  prewarming`.
- **Shared memory.** The HAL maps the I/O buffer and an engine status block
  (`Sample Time`, `Host Time`, `Time Stamp Seed`, client in/out positions,
  `Engine Running`).
- **HAL plug-ins.** They live in `/System/Library/Audio/Plug-Ins/HAL` (AppleAOPAudioPlugin,
  AppleUSBAudio, BTAudioHALPlugin, ...) and `/Library/Audio/Plug-Ins/HAL`
  (`VirtualAudio.plugin`). VirtualAudio builds the default device `vdef` from
  the physical speaker and codec devices plus `/Library/Audio/Tunings/D22/VAD`.
  When those are missing it fails with "Found no speaker device". It is
  disabled in this port's image (v12), and that was the earlier mediaserverd
  `'vdef'` crash.

Minimal replacement options:
- **Option c (recommended): an AudioServerPlugIn plus a small kext.**
  - The plug-in goes in `/Library/Audio/Plug-Ins/HAL` and runs in mediaserverd.
    It publishes one output device and stream (48 kHz, 2 ch, 16/32-bit) and
    implements `GetZeroTimeStamp` from a counter it maps from the kext.
  - The kext exposes a ring buffer plus start/stop, and later talks to the ADSP
    or the bit-bang core.
  - **GUESS**: the plug-in loads with this port's trustcache/AMFI setup.
  - **GUESS**: VirtualAudio will still not build `vdef` around an unknown
    device. The first test should go AudioQueue -> HAL device directly.
- **Option a: emulate `IOAudio2Device`/user client.** This is the only path where
  VirtualAudio and AVAudioSession routing behave as on stock. It needs the
  user-client selectors and status-block offsets, which are unknown and need
  disassembly. Effort is high.
- **Option b: port the open-source IOAudioFamily.** This works with the IOAudio1
  back end, but it gains nothing over option c.

Raw notes for this section are in the agent scratchpad, not in git.

## 4. Probe (shim/fuxi-probe/audio.c, host tool tools/fuxi-audio-probe.py)

Requests: IN `0xc0/0x5a`, OUT `0x40/0x5b`. Reports are 128 little-endian u32s
with magic 0xa0d10001 (snapshot) or 0xa0d10002 (stage 2). Word 3 counts
recovered synchronous aborts.

### Stage 1 — read-only snapshot (`snapshot`)

| Group | Reads | Notes |
|---|---|---|
| 0 | GCC wrap2 vote/CBCRs/s0 RCG+DFS perf levels; TLMM CTL/IO for gpio 0,1,68,98,99,121-124,182,183 | [gcc] offsets |
| 1 | QUP2 SE0 GENI registers + wrapper HW version | **only if** the m_ahb CBCR (0x233cc) is not CLK_OFF; gated SE registers hang the bus (touch lane) |
| 2 | AOSS QMP descriptor 0xc300000+0x0..0x38 and the first mcore mailbox words | [Lin] qcom_aoss.c layout |
| 3 | SMEM TOC, AP<->ADSP partition, items 443/429 (smp2p), 478/479/480 (glink), global item 423 (crash reason), current ready bits | [Lin] smem.c/smp2p.c; parsing is bounded |
| 4 | SCM `is_call_available` for PAS 1/2/5/6/7 and `pas_is_supported(1)` / `(0x24)` | SMCs, no state change; run separately |

The snapshot does not write MMIO at all. LPASS (0x3000000) is **not** read,
because a gated LPASS register could raise an SError.

### Stage 2 — ADSP PAS boot (`squash`, `upload`, `boot`)

The host squashes `.mdt` + `.bNN` into one ELF and stages it over control OUT
into 0x8d0000000 (dtb up to 1 MiB, adsp up to 80 MiB). Writes must be
sequential, and a short chunk closes the image. The device checks the length
and FNV-1a hash. The staging window must not overlap the loader or FDT, and
it is refused after a launch. `boot` then runs once per loader boot:

1. Precheck: both images complete; ADSP not already ready (smp2p 443).
2. dtb (pas 0x24): ELF/phdr/hash bounds; metadata -> `init_image`;
   `mem_setup` if relocatable; segments copied into 0x9e980000 [Lin] mdt_loader.
3. AOSS QMP: open the link if needed, send
   `{class: image, res: load_state, name: adsp, val: on}` [Lin] q6v5_prepare.
4. RPMh proxy votes LCX hlvl 7 and LMX hlvl 4 via apps RSC TCS0 (existing `rh_send`).
   Justification: DT `cx/mx-uV-uA 0x180` plus cmd-db levels.
5. `auth_and_reset(0x24)`.
6. Main image (pas 1) into 0x9ea00000, same rules; `auth_and_reset(1)`.
7. Poll "slave-kernel" for up to 5 s for ready (fatal stops the poll). On a
   timeout or fatal, `pas_shutdown(1)` runs, as Linux does.
8. If handover follows within 1 s, the proxy votes drop to 0 (Linux
   `adsp_handover`). Otherwise they stay up.

Writes in stage 2: the QMP msgram, the IPCC SEND_ID (client 0), RSC TCS0, the
two DT carveouts, and staging RAM. It does not touch PMIC regulators, fuses or
UFS. Each SMC resumes on `INTERRUPTED` with x6 carried over, bounded at 100k
loops.

**Not done (milestone 2):** glink-SMEM FIFO allocation (needs the TCSR
hwspinlock), QRTR, pd-mapper, and GPR/APM. For milestone 1, "ADSP ready + handover"
counts as success.

## 5. Device test order (coordinator)

The image is `shim/out/lane-audio/fuxi-audio-m1-boot.img`: the loader with the
0097 payload, which still boots iOS after `0x52` launch.

1. `fastboot boot fuxi-audio-m1-boot.img`, then
   `python3 tools/fuxi-audio-probe.py snapshot --groups 0,1,2,3 --save au-s1.bin`.
2. `python3 tools/fuxi-audio-probe.py snapshot --groups 4 --save au-s4.bin`.
   This runs SMC queries only; do it as a separate step so a hang is attributable.
3. Pull the firmware (section 6), squash it on the host, and stage it:
   `python3 tools/fuxi-audio-probe.py squash DIR adsp_dtb adsp_dtb.mbn`,
   `... squash DIR adsp adsp.mbn`, then
   `python3 tools/fuxi-audio-probe.py upload --dtb adsp_dtb.mbn --fw adsp.mbn`.
   Upload first extends the monitor deadline by 30 min (req 0x54).
4. `python3 tools/fuxi-audio-probe.py boot --save au-boot.bin`, then
   `report` to re-read. Reboot the phone afterwards. Stage 2 does not stop the
   ADSP on success, and the 0097 kernel knows nothing about it, so do not
   launch iOS after a successful stage 2 during this milestone.

Expected lines:
- Group 0: `gcc m_ahb ... ON` or `off`. TDM gpio121-124 most likely func 0 or 1
  with oe=0 (**GUESS**, bootloader leaves them idle). Reset gpios: unknown.
- Group 1: `protocol=3 (3=I2C)` if XBL/ABL set up SE0, otherwise
  `(wrapper AHB gated: SE not read)`.
- Group 2: `qmp magic 0x4d41494c`, version 1. Link state shows whether ABL left it open.
- Group 3: `item 443: absent` and `ADSP ready bits: no inbound item 443` if the
  ADSP is down (**GUESS**: ABL does not boot it). The crash reason should be empty.
- Group 4: `available=1` for init_image/mem_setup/auth_reset/shutdown;
  `pas_is_supported(1 adsp) x0=0x0 x1=1`.
- Stage 2 success: `stage 2 result=0x1 (OK) step=10 (done)`, wait bits
  `ready|handover` after about 500 ms (recovery dmesg showed ~0.5 s).
  `released=0x1`.
- Typical failures to report verbatim: result 0x110 (init_image refused: wrong
  pas/metadata), 0x122-0x126 (QMP), 0x140/0x150 (auth), 0x160 (no ready), 0x161
  (fatal: crash reason via a rerun of `snapshot --groups 3`).

## 6. Firmware to pull from the phone

Keep everything outside git and pass it to builds through environment variables.

- ADSP (needed for stage 2): `adsp.mdt`, `adsp.b00`..`adsp.b58` (all present
  indices), `adsp_dtb.mdt`, `adsp_dtb.b00`..`adsp_dtb.b02`. Most likely under
  `/vendor/firmware_mnt/image/` (the modem/NON-HLOS partition). **GUESS**: if
  that path is missing, check `/vendor/firmware/` and `/odm/firmware/`.
- Amp (paths C and A): from `/odm/firmware/` (proprietary-files list):
  - `cs35l41-dsp1-spk-prot.wmfw`, `cs35l41-dsp1-spk-cali.wmfw`
  - `B-cs35l41-dsp1-spk-prot.bin`, `B-cs35l41-dsp1-spk-cali.bin`
  - `T-cs35l41-dsp1-spk-prot.bin`, `T-cs35l41-dsp1-spk-cali.bin`
  - `B-music.txt`, `B-handfree.txt`
  - `T-music.txt`, `T-handset.txt`, `T-handfree.txt`
  - the init script `/odm/etc/init/init.cirrus.rc`
- AudioReach (path A): `/vendor/lib/rfsa/adsp/capi_cirrus_sp.so`, plus
  `/odm/etc/acdbdata/Tutu/Tutu_acdb_cal.acdb` and
  `Tutu_workspaceFileXml.qwsp`. Also the `/vendor/etc/audio/sku_kalama/` mixer_paths and
  resourcemanager XMLs, `card-defs.xml`, and `usecaseKvManager.xml`.
  **GUESS**: the dynamic modules in `/vendor/dsp/` (dsp partition) may also be needed.
- Per-device speaker calibration, if present: run `getprop | grep -i cirrus` and
  look under `/mnt/vendor/persist/audio/` (read-only copy, do not modify persist).

## 7. Next steps

- Device results for stages 1 and 2.
- Path C prototype: GENI I2C FIFO driver in the probe; read CS35L41 DEVID
  (0x0 = 0x35a40) and REVID on both amps. The first write follows only after
  the reads succeed.
- Path A milestone 2: glink-SMEM + QRTR + servreg, then the APM-up check.

# Audio lane — port plan P-AU (speaker first, then microphone)

Nothing below has been run on the fuxi device: every device effect is **not yet observed**. Sheng facts are from UniXNU's
`docs/devices/sheng.md` Audio section (which records a device pass on sheng: all amps PLL-locked, 155 of 155 test-tone periods
sent, capture 200 of 200 periods). Sheng is an SM8550-class board, so most of the DSP side carries over.

## 8. What carries over from sheng and what does not

| Item | Sheng | Fuxi | Verdict |
|---|---|---|---|
| ADSP PAS boot, QMP, RPMh proxy votes, SMEM/SMP2P/GLINK, IPCC | UEFI-side, firmware from modem_a `/image` | already done in the loader probe stage 2 (section 4, unobserved) | carries over; XNU-side owner still missing |
| GPR packet codec, APM "SPF ready" query | `ShengGpr.c` | `xnu/fuxi-audio/fuxi_gpr.c` (this commit) | ported, host-tested |
| AudioReach command framing, shmem map/write/read, PRM clock and block votes | `ShengAudioReach.c` | `xnu/fuxi-audio/fuxi_audioreach.c` | framing ported; **graph ids are sheng's** |
| PRM clock ids (TX core 0x30c, VA core 0x307), macro and dcodec votes make LPASS registers reachable | observed on sheng | same SoC family | likely carries over (**GUESS**) |
| DSP buffers: 32-bit address, stream index in the upper word, buffer in low DRAM outside the kernel window, SMMU in pass-through; a translating SMMU context resets the board | sheng default PA 0xd0000000 | the PA must be re-checked against fuxi's memory map and ADT | rule carries over, address does not |
| Host clock: CoreAudio assumes 24 MHz, the board timer is 19.2 MHz; fix by patching the constant behind AudioGetHostClockFrequency | sheng plug-in | iOS image is the same, SM8550 timer is 19.2 MHz | likely carries over (**GUESS**), apply to `tools/fuxi-audio` |
| Speaker amps | 6 x CS35L43, I2C 0x40-0x42 on QUP1 SE5 and QUP2 SE0, shared reset gpio90, secondary MI2S gpio121-124, ASP RX1 direct, DSP bypassed, 16-bit I2S 48 kHz, gain 18 dB | 2 x CS35L41, I2C 0x40/0x42 on QUP2 SE0, resets gpio183/gpio182, IRQ gpio99/98, receiver switch gpio68, TDM secondary gpio121-124, slots 0 (T) and 1 (B) | **device specific**; `ShengCs35l43.c` is a template only (different chip, register map not identical) |
| Graph ids (subgraphs 0x4001/0x4005/0x4003/0x4007, instances 0x60xx) and the I2S sink param | sheng board topology | fuxi's ACDB topology is unknown; its speaker endpoint is TDM, not I2S | **device specific**, must be discovered |
| Mic: WCD938x on the TX SoundWire master, TX and VA macros gated by the VA macro; AMIC3 / micbias 3 / ADC2 / codec DMA TX3 mono 48 kHz | sheng | fuxi DT has WCD938x, lpass-cdc tx-macro@6AE0000 and va-macro, AMIC1-5, micbias1-4 | codec side likely carries over; **which AMIC/micbias fuxi's mic uses is not known** |
| GENI I2C core with 1-8 byte writes and 32-bit amp registers | `GeniI2c.c` (BSD) | `shim` has a touch-lane GENI driver | later port candidate |
| Other BSD blocks: `QcomGlink/Smem/Smp2p/Qrtr/Rpmh/Mdt/Qmp` | sheng | none in XNU | later ports (P-AU2/P-AU3) |

## 9. Steps, dependencies, firmware and gates

Sources and licenses: UniXNU-sheng commit `08cde20edc32` (BSD-2-Clause-Patent), files under `package/` named in each ported
header. hackios (APSL) was read for ideas only (its single RSC mutex).

| Step | Work | Needs | Status |
|---|---|---|---|
| P-AU0 | read-only probe, loader ADSP PAS stage 2, HAL plug-in `tools/fuxi-audio` that counts and drops | section 4-6 | in tree, device result not yet observed |
| **P-AU1** | GPR codec + APM readiness query, AudioReach framing, RSC arbiter, host tests, sync check | nothing | **done in this commit** (`tools/test-fuxi-audio.py`) |
| P-AU2 | XNU owner for the ADSP: keep the loader-stage-2 boot and hand the state to XNU, or port Qcom{Mdt,Qmp,Smem,Smp2p}; wire the arbiter into `fuxi_wlan.c`; add a build hook for 0113 | P-AU1; ADSP firmware (below); an SCM caller in XNU or the loader hand-off | planned |
| P-AU3 | GLINK `adsp_apps` channel (sheng: DSP offers 24 intents of 128 B and grants one 4 KiB intent on request), IPCC mailbox, TCSR hwspinlock for the SMEM FIFOs | P-AU2 | planned |
| P-AU4 | APM readiness: run `fuxi_gpr_spf_*` until ready. First device experiment; recovery showed `apm is not up`, so a pd-mapper/servreg responder may also be needed (**GUESS**) | P-AU3 | planned |
| P-AU5 | PRM: clock votes and macro/dcodec block votes (`fuxi_ar_prm_*`) | P-AU4 | planned |
| P-AU6 | speaker graphs: device graph (TDM sink on LPAIF secondary, slots 0/1) + playback stream graph, shared-memory map and write loop | fuxi topology ids (below); buffer PA decision | planned, blocked on facts |
| P-AU7 | amps: GENI I2C on QUP2 SE0, pin muxes (gpio0/1 I2C, 121-124 TDM), reset gpio182/183, read DEVID (0x35a40) first, then ASP RX1 direct, low gain, short tone | CS35L41 register init (see blockers) | planned |
| P-AU8 | kernel audio service with the rings and control page of `ShengAudioAbi.h` (ideas; new ABI to write) feeding the `tools/fuxi-audio` HAL plug-in; apply the 19.2 MHz host clock fix | P-AU6, P-AU7 | planned |
| P-AU9 | microphone: PRM TX/VA macro clocks, TX SoundWire master, WCD938x enumeration, capture graph (codec DMA TX3 source, read-shmem sink) | P-AU5, fuxi mic routing | planned, after speaker |

### RPMh serialization (finding)

`xnu/fuxi-wlan/fuxi_wlan.c:278` `rh_send_request` always uses ACTIVE TCS 0 for its callers and guards it with the plain
`rh_poisoned` flag (`:276`, checked at `:281`, set at `:290`). That is a check-then-set with no atomic or lock, and it also does
read-modify-write on the shared RSC IRQ-enable word (`:288`, `:291`, `:343`). Today it is safe only because the header comment
says no other XNU code drives the apps RSC and the loader probe runs before XNU. The moment the audio owner votes in XNU (the
ADSP proxy drop after handover, PRM follow-ups) next to WLAN QMP/ICC votes, two sequences can interleave, which is the hackios
bug (audio DSP and modem asking through TCS 0 together). Using a different TCS per owner would not help: the IRQ-enable RMW and
the status registers are shared. `fuxi_rpmh_arb.h` is the cure: a bounded-spin owner-tagged lock around program/trigger/wait/cleanup
that poisons the arbiter on an uncertain sequence. The test drives 4 threads over a modelled TCS: unguarded runs saw thousands of
overlaps, guarded runs saw none. It is **not yet wired** into `fuxi_wlan.c` (that file belongs to the WLAN lane and patch 0099 is
regenerated from it; the wiring is two lines: enter before the precheck and leave on every return, with `ok` = result 0).

### Firmware to pull from the phone (never in git, never downloaded here)

As in section 6. Extra items that matter for the speaker path: the ACDB (`Tutu_acdb_cal.acdb`, `Tutu_workspaceFileXml.qwsp`) and
the Lineage/vendor `usecaseKvManager.xml`, `resourcemanager_kalama_mtp.xml` and `card-defs.xml`, because they hold the real
subgraph/module instance ids for `PAL_DEVICE_OUT_SPEAKER` -> `TDM-LPAIF-RX-SECONDARY`. Sheng needed no amp firmware (DSP bypass),
so `cs35l41-dsp1-spk-*` files are not required for the first tone; expect harsh sound and no speaker protection, so keep the first
tone short and quiet.

### Boot-args and gating (proposal)

The kernel cmdline is nearly full, so: one bitmask `fuxiaudio` (shorter than sheng's two args), preferably carried as an ADT
`/product`/`chosen` option written by `mkadt-fuxi.py` so it costs no cmdline bytes; default 0 = nothing changes.
Bits: 1 ADSP up and SPF query, 2 PRM votes and speaker graph with a 750 Hz tone (short, attenuated), 4 amps, 8 kernel audio
service for the HAL plug-in, 16 microphone capture self-test. Higher bits imply lower ones. A second arg for the period buffer PA
only if the ADT cannot carry it. The HAL plug-in stays opt-in through `fuxi-sys-root.py --audio-plugin`.

### Blockers and facts needed

1. ~~Fuxi ACDB/topology ids~~ Not needed: a client names its own graph's ids. TDM module and parameters known, and the DSP plays through them (a-play2, research_notes/audio-next.md). Capture path still to do.
2. ~~CS35L41 register init for ASP RX1 direct.~~ `shim/fuxi-probe/au_amp_on.h`, every step read back; run in the loader at the lowest level. Levels above that, and the protection firmware's absence at them, are the open question: no datasheet.
3. ~~A reserved low-DRAM window for the DSP buffers.~~ 0xd0000000 up works in the loader and in XNU, outside the bank XNU owns, with streams 0x1001/0x80 and 0x1061 routed by the loader to context bank 4 (stage 1 off): bypass, and 0x1801, are refused by the hypervisor.
4. ~~An SCM/PAS caller inside XNU~~ A hand-off from the loader after stage 2 (`ADSP=1` in boot/ios-remote.sh). A DSP restart under iOS is still not possible.
5. ~~Which AMIC/micbias fuxi's built-in microphone uses~~ AMIC1, MIC BIAS1 at 2.75 V, ADC1, master port 3 lane 1, SWR_MIC4, codec DMA TX3 (a-cap2).
6. ~~Whether the APM needs a servreg/pd-mapper responder~~ It does not: a-gpr1, status 1 two seconds after GLINK with neither.

### Tests

`python3 tools/test-fuxi-audio.py` builds `xnu/fuxi-audio/*.c` with ASan/UBSan against `tools/fuxi-audio-host/*_host.c`:
GPR 37 checks, AudioReach 182 checks (sheng ids), arbiter 24k checks. `python3 tools/sync-fuxi-audio.py --check` verifies patch
`xnu/patches/0113-fuxi-audio.patch` (new files under `bsd/dev`, in no build list, so the kernel build is unchanged).
