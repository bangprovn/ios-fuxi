/* SPDX-License-Identifier: BSD-2-Clause-Patent */
/* Copyright (c) 2026, UniXNU contributors. All rights reserved.
 * Ported from UniXNU-sheng package/ShengAudioReach.h (08cde20edc32, BSD-2-Clause-Patent) with fuxi_ names. */
/* The audio DSP's graph manager (APM) commands for a speaker and a microphone path, as GPR packets. Two graphs per direction:
 * a device graph that owns the hardware endpoint and a stream graph that owns the shared memory endpoint; the stream graph's open
 * carries the connection into the device graph. Instance numbers below are the SHENG board topology, NOT verified for fuxi: fuxi's speaker is a TDM secondary endpoint, so
 * ar_open/config_speaker_device must be replaced once the fuxi ADSP topology is read from its ACDB (docs-lanes/audio.md). (device subgraph 0x4005 for the secondary
 * MI2S speakers, 0x4007 for codec DMA TX3, stream subgraphs 0x4001 playback and 0x4003 capture). Each packet stays within one GLINK
 * intent (FUXI_GPR_MAX_PACKET). Pure encoding: no I/O, no waits. */
#ifndef FUXI_AUDIOREACH_H
#define FUXI_AUDIOREACH_H
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif

#define FUXI_AR_APM_IID 1u
#define FUXI_AR_OP_GRAPH_OPEN 0x01001000u
#define FUXI_AR_OP_GRAPH_PREPARE 0x01001001u
#define FUXI_AR_OP_GRAPH_START 0x01001002u
#define FUXI_AR_OP_GRAPH_STOP 0x01001003u
#define FUXI_AR_OP_GRAPH_CLOSE 0x01001004u
#define FUXI_AR_OP_SET_CFG 0x01001006u
#define FUXI_AR_OP_MEM_MAP 0x0100100Cu
#define FUXI_AR_OP_MEM_UNMAP 0x0100100Du
#define FUXI_AR_OP_MEM_MAP_DONE 0x02001001u          /* payload {handle} */
#define FUXI_AR_OP_WRITE 0x0400100Au                 /* DATA_CMD_WR_SH_MEM_EP_DATA_BUFFER_V2 */
#define FUXI_AR_OP_WRITE_DONE 0x05001004u
#define FUXI_AR_OP_READ 0x0400100Bu                  /* DATA_CMD_RD_SH_MEM_EP_DATA_BUFFER_V2 */
#define FUXI_AR_OP_READ_DONE 0x05001005u
#define FUXI_AR_OP_EOS_RENDERED 0x05001001u

/* Module ids and the instance ids the board topology gives them. */
#define FUXI_AR_MOD_WR_SHMEM 0x07001000u
#define FUXI_AR_MOD_RD_SHMEM 0x07001001u
#define FUXI_AR_MOD_PCM_CNV 0x07001003u
#define FUXI_AR_MOD_PCM_ENC 0x07001004u
#define FUXI_AR_MOD_PCM_DEC 0x07001005u
#define FUXI_AR_MOD_I2S_SINK 0x0700100Au
#define FUXI_AR_MOD_TDM_SINK 0x0700100Eu             /* in fuxi's ACDB too */
#define FUXI_AR_MOD_TDM_SOURCE 0x0700100Fu
#define FUXI_AR_MOD_CODEC_DMA_SOURCE 0x07001024u

#define FUXI_AR_SG_PLAY 0x4001u
#define FUXI_AR_SG_SPEAKER 0x4005u
#define FUXI_AR_SG_CAPTURE 0x4003u
#define FUXI_AR_SG_MIC 0x4007u
#define FUXI_AR_IID_PLAY_SHMEM 0x6001u
#define FUXI_AR_IID_PLAY_DEC 0x6002u
#define FUXI_AR_IID_PLAY_CNV 0x6003u
#define FUXI_AR_IID_SPEAKER_I2S 0x6051u
#define FUXI_AR_IID_SPEAKER_TDM 0x6052u              /* our own: a client names its graph's parts */
#define FUXI_AR_IID_MIC_DMA 0x6070u
#define FUXI_AR_IID_MONITOR_TDM 0x6072u              /* the amplifiers' voltage and current coming back */
#define FUXI_AR_IID_CAP_CNV 0x6022u
#define FUXI_AR_IID_CAP_ENC 0x6023u
#define FUXI_AR_IID_CAP_SHMEM 0x6024u

typedef struct {
    uint32_t rate;           /* Hz */
    uint16_t bits;           /* 16 */
    uint16_t channels;       /* 1 or 2 */
} FuxiArFormat;

/* A TDM endpoint. Fuxi's speakers are on the secondary one, the SoC its clock master. */
typedef struct {
    uint32_t intf;           /* 1 secondary */
    uint32_t slot_mask;
    uint16_t slots, slot_width;
    uint16_t sync_mode, invert_sync, sync_data_delay;
    uint16_t lane;           /* sd line: gpio124, i2s1_data1, is 1 */
    uint32_t clock_attribute;/* 1 couple no, 4 the same with the bit clock inverted */
} FuxiArTdm;

/* Each builder writes one complete GPR packet into out (capacity bytes) and returns its size, or 0 when it does not fit.
 * port: the GPR port the answers come back to; token: echoed in the answer. */
uint32_t fuxi_ar_open_speaker_device(uint8_t *out, uint32_t capacity, uint32_t port, uint32_t token);
uint32_t fuxi_ar_config_speaker_device(uint8_t *out, uint32_t capacity, uint32_t port, uint32_t token, const FuxiArFormat *f);
uint32_t fuxi_ar_open_speaker_tdm(uint8_t *out, uint32_t capacity, uint32_t port, uint32_t token);
uint32_t fuxi_ar_config_speaker_tdm(uint8_t *out, uint32_t capacity, uint32_t port, uint32_t token, const FuxiArFormat *f, const FuxiArTdm *t);
uint32_t fuxi_ar_open_playback_stream(uint8_t *out, uint32_t capacity, uint32_t port, uint32_t token);
uint32_t fuxi_ar_open_playback_stream_tdm(uint8_t *out, uint32_t capacity, uint32_t port, uint32_t token);  /* the same, into the TDM endpoint */
uint32_t fuxi_ar_open_monitor_tdm(uint8_t *out, uint32_t capacity, uint32_t port, uint32_t token);
uint32_t fuxi_ar_config_monitor_tdm(uint8_t *out, uint32_t capacity, uint32_t port, uint32_t token, const FuxiArFormat *f, const FuxiArTdm *t);
uint32_t fuxi_ar_open_capture_stream_tdm(uint8_t *out, uint32_t capacity, uint32_t port, uint32_t token);
uint32_t fuxi_ar_config_playback_stream(uint8_t *out, uint32_t capacity, uint32_t port, uint32_t token, const FuxiArFormat *f);
uint32_t fuxi_ar_config_playback_shmem(uint8_t *out, uint32_t capacity, uint32_t port, uint32_t token, const FuxiArFormat *f);
uint32_t fuxi_ar_open_mic_device(uint8_t *out, uint32_t capacity, uint32_t port, uint32_t token);
/* channel_mask: codec DMA channels in use (bit 0 first channel). */
uint32_t fuxi_ar_config_mic_device(uint8_t *out, uint32_t capacity, uint32_t port, uint32_t token, const FuxiArFormat *f, uint32_t channel_mask);
uint32_t fuxi_ar_open_capture_stream(uint8_t *out, uint32_t capacity, uint32_t port, uint32_t token);
uint32_t fuxi_ar_config_capture_stream(uint8_t *out, uint32_t capacity, uint32_t port, uint32_t token, const FuxiArFormat *f);
/* opcode: FUXI_AR_OP_GRAPH_PREPARE, _START, _STOP or _CLOSE for the one subgraph sg. */
uint32_t fuxi_ar_graph_command(uint8_t *out, uint32_t capacity, uint32_t port, uint32_t token, uint32_t opcode, uint32_t sg);
/* Maps one physically contiguous region (rounded up to 4 KiB) for the DSP; the answer is FUXI_AR_OP_MEM_MAP_DONE with the handle. */
uint32_t fuxi_ar_map(uint8_t *out, uint32_t capacity, uint32_t port, uint32_t token, uint64_t address, uint32_t bytes);
uint32_t fuxi_ar_unmap(uint8_t *out, uint32_t capacity, uint32_t port, uint32_t token, uint32_t handle);
/* One buffer for the playback shared memory endpoint; the answer is FUXI_AR_OP_WRITE_DONE with the same token. */
uint32_t fuxi_ar_write(uint8_t *out, uint32_t capacity, uint32_t port, uint32_t token, uint64_t address, uint32_t handle, uint32_t bytes);
/* One empty buffer for the capture shared memory endpoint; the answer is FUXI_AR_OP_READ_DONE. */
uint32_t fuxi_ar_read(uint8_t *out, uint32_t capacity, uint32_t port, uint32_t token, uint64_t address, uint32_t handle, uint32_t bytes);

/* The DSP's power and resource manager (PRM, port 2): an LPASS clock at hz (0 releases it; attribute "couple no", default root), and
 * a hardware block vote (1 LPASS core macros, 2 digital codec). The answer is FUXI_AR_OP_PRM_REQUEST_DONE / _RELEASE_DONE. */
#define FUXI_AR_PRM_IID 2u
#define FUXI_AR_OP_PRM_REQUEST 0x0100100Fu
#define FUXI_AR_OP_PRM_RELEASE 0x01001010u
#define FUXI_AR_OP_PRM_REQUEST_DONE 0x02001002u
#define FUXI_AR_OP_PRM_RELEASE_DONE 0x02001003u
#define FUXI_AR_CLK_TX_CORE_MCLK 0x30cu   /* PRM clock ids, not the device tree indices (57 was refused) */
#define FUXI_AR_CLK_VA_CORE_MCLK 0x307u
#define FUXI_AR_HW_MACRO 1u
#define FUXI_AR_HW_DCODEC 2u
uint32_t fuxi_ar_prm_clock(uint8_t *out, uint32_t capacity, uint32_t port, uint32_t token, uint32_t clock_id, uint32_t hz);
uint32_t fuxi_ar_prm_clock_attr(uint8_t *out, uint32_t capacity, uint32_t port, uint32_t token, uint32_t clock_id, uint32_t hz, uint32_t attribute);
uint32_t fuxi_ar_prm_vote(uint8_t *out, uint32_t capacity, uint32_t port, uint32_t token, uint32_t block, int enable);

/* Answers. */
typedef struct {
    uint32_t opcode, token, src_port;
    uint32_t result_opcode, status;  /* basic result: the command and its status (0 success); framework state: 1 ready; PRM: status */
    uint32_t handle;                 /* FUXI_AR_OP_MEM_MAP_DONE */
    uint64_t address;                /* write/read done: the buffer */
    uint32_t data_bytes, offset;     /* read done: bytes filled, starting offset bytes into the buffer */
} FuxiArAnswer;
enum {FuxiArUnknown = 0, FuxiArBasicResult = 1, FuxiArMapped = 2, FuxiArWriteDone = 3, FuxiArReadDone = 4, FuxiArFrameworkState = 5, FuxiArPrmDone = 6, FuxiArBad = -1};
int fuxi_ar_parse(const uint8_t *in, uint32_t length, FuxiArAnswer *a);

#ifdef __cplusplus
}
#endif
#endif
