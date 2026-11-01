#!/usr/bin/env python3
"""Fuxi audio lane milestone-1 host tool (probe requests 0x5a/0x5b, audio.c).

  snapshot [--groups 0,1,2,3]  read-only snapshot pages (group 4 = SCM queries)
  squash DIR NAME OUT          merge NAME.mdt + NAME.bNN into one MBN (no device)
  upload --dtb F --fw F        stage squashed adsp_dtb/adsp MBNs over control OUT
  boot                         stage 2: PAS-boot the ADSP once, then decode
  report                       re-read the frozen stage-2 report
  glink [--seconds N] [--answer] [--gpr] [--prm]  after a boot that reached ready: GLINK with the DSP, polled; no data is sent unless --gpr asks the APM its state; --prm then votes LPASS on and off; --graph between them opens, configures and closes the TDM graph, never starting it; --play also starts it and gives it a second of zeros, no pin muxed
  mic                          after a boot that reached ready: the DSP's clock, LPASS, SoundWire, the codec's id, bias and ADC1 read back (au_mic.h); stops at the first proof step that fails; no capture
  decode FILE                  decode a saved raw page offline
Raw pages can be saved with --save. See docs-lanes/audio.md.
"""
import argparse
import struct
import sys
from pathlib import Path

VID, PID, PRODUCT = 0x1209, 0x0001, 'Fuxi hardware probe'
GPIOS = [0, 1, 68, 98, 99, 121, 122, 123, 124, 182, 183]
PIN_ROLE = {0: 'I2C SDA (qup2 se0, func2)', 1: 'I2C SCL (qup2 se0, func2)',
            68: 'rcv/spk switch (T amp)', 98: 'T amp IRQ (0x42)', 99: 'B amp IRQ (0x40)',
            121: 'TDM1 SCK (i2s1_sck func1)', 122: 'TDM1 DIN (i2s1_data0)',
            123: 'TDM1 WS (i2s1_ws)', 124: 'TDM1 DOUT (i2s1_data1)',
            182: 'T amp RESET', 183: 'B amp RESET'}
GCC = ['vote52010', 'm_ahb', 's_ahb', 'core', 'core2x', 's0_cbcr', 's0_rcg_cmd', 's0_rcg_cfg',
       's0_dfsr'] + [f's0_perf{i}' for i in range(8)]
SCM_Q = ['init_image', 'mem_setup', 'auth_reset', 'shutdown', 'is_supported']
STEPS = {1: 'prechecks', 2: 'dtb init/load', 3: 'AOSS load_state', 4: 'LCX/LMX votes',
         5: 'dtb auth_and_reset', 6: 'adsp init/load', 7: 'adsp auth_and_reset',
         8: 'wait smp2p ready', 9: 'handover', 10: 'done', 11: 'outbound item 429'}
ERR = {0x100: 'firmware not staged', 0x1ff: 'ADSP already ready', 0x101: 'not ELF32 LE',
       0x102: 'bad phdr table', 0x103: 'bad header segment', 0x104: 'segment beyond staged bytes',
       0x105: 'filesz > memsz', 0x106: 'no load/hash or metadata too big',
       0x107: 'segments outside carveout', 0x110: 'init_image failed', 0x111: 'mem_setup failed',
       0x112: 'segment outside region', 0x113: 'read fault', 0x120: 'QMP magic/version',
       0x121: 'QMP mailbox geometry', 0x122: 'QMP link ack timeout', 0x123: 'QMP ucore channel timeout',
       0x124: 'QMP channel ack timeout', 0x125: 'QMP message pending', 0x126: 'QMP message not consumed',
       0x130: 'RSC DRV id/cfg', 0x131: 'LCX vote', 0x132: 'LMX vote', 0x140: 'dtb auth_and_reset',
       0x150: 'adsp auth_and_reset', 0x160: 'ready timeout (shutdown issued)',
       0x161: 'ADSP fatal bit (shutdown issued)', 0x170: 'TCSR mutex 3 not taken', 0x171: 'no AP-ADSP partition',
       0x172: 'no room for item 429', 1: 'OK'}


def on(cbcr):
    return 'off' if cbcr & (1 << 31) else 'ON'


def cstr(words):
    b = struct.pack(f'<{len(words)}I', *words)
    return b.split(b'\0')[0].decode('ascii', 'replace')


def decode_smp2p(r, label):
    out = [f'  {label}: magic={r[0]:#x} ver={r[1] & 0xff} feat={r[1] >> 8:#x} '
           f'pid {r[2] & 0xffff}->{r[2] >> 16} entries {r[3] >> 16}/{r[3] & 0xffff} flags={r[4]:#x}']
    for e in range(4):
        w = r[5 + 5 * e:10 + 5 * e]
        if any(w):
            out.append(f'    {cstr(w[:4])!r:20} = {w[4]:#010x}')
    return out


def ready_text(b):
    if b & 0xf0000000:
        return {0x80000000: 'no AP-ADSP partition', 0x40000000: 'no inbound item 443',
                0x20000000: 'bad smp2p magic', 0x10000000: 'no slave-kernel entry'}.get(b, hex(b))
    return f'{b:#x} ' + '|'.join(n for i, n in enumerate(['fatal', 'ready', 'handover', 'stop-ack']) if b >> i & 1)


def decode(r):
    out = []
    if r[0] == 0xa0d10002:
        return decode_boot(r)
    if r[0] != 0xa0d10001:
        return f'bad magic {r[0]:#x}'
    g = r[1]
    out.append(f'group {g} status={r[2]:#x} faults={r[3]}')
    if g == 0:
        for i, n in enumerate(GCC):
            v = r[4 + i]
            out.append(f'  gcc {n:11} {v:#010x}' + (f' {on(v)}' if 'cbcr' in n or n in GCC[1:5] else ''))
        for i, gp in enumerate(GPIOS):
            n, ctl, io = r[24 + 3 * i:27 + 3 * i]
            out.append(f'  gpio{n:<3} ctl={ctl:#06x} func={ctl >> 2 & 0xf} pull={ctl & 3} '
                       f'oe={ctl >> 9 & 1} in={io & 1} out={io >> 1 & 1}  {PIN_ROLE[gp]}')
    elif g == 1:
        out.append(f'  m_ahb={r[4]:#010x}' + ('' if r[2] == 1 else '  (wrapper AHB gated: SE not read)'))
        if r[2] == 1:
            offs = [0x040, 0x048, 0x064, 0x068, 0x07c, 0x258, 0x25c, 0x268, 0x260, 0x264,
                    0x610, 0x614, 0x800, 0x804, 0xe24, 0xe28]
            for i, o in enumerate(offs):
                out.append(f'  se0+{o:#05x} {r[8 + i]:#010x}')
            fw = r[11]  # GENI_FW_REVISION_RO: protocol in [15:8]
            out.append(f'  protocol={fw >> 8 & 0xff} (3=I2C) fw_ver={fw & 0xff:#x} qup_hw_ver={r[24]:#x}')
    elif g == 2:
        names = ['magic', 'version', 'features', 'ucore_link', 'ucore_link_ack', 'ucore_ch',
                 'ucore_ch_ack', 'ucore_mbox_size', 'ucore_mbox_off', 'mcore_link', 'mcore_link_ack',
                 'mcore_ch', 'mcore_ch_ack', 'mcore_mbox_size', 'mcore_mbox_off']
        for i, n in enumerate(names):
            out.append(f'  qmp {n:16} {r[4 + i]:#010x}')
        out.append(f'  mcore mailbox words: {" ".join(f"{x:08x}" for x in r[20:24])}')
    elif g == 3:
        out.append(f'  smem sbl_version={r[4]:#x} initialized={r[5]} toc={r[6]:#x} ver={r[7]} entries={r[8]}')
        if r[2] == 1:
            out.append(f'  AP-ADSP partition @{r[9]:#x} size={r[10]:#x} free_uncached={r[11]:#x}')
            for i, item in enumerate([443, 429, 478, 479, 480]):
                out.append(f'  item {item}: ' + (f'@{r[12 + 2 * i]:#x} size={r[13 + 2 * i]}' if r[12 + 2 * i] else 'absent'))
            if r[12]:
                out += decode_smp2p(r[32:64], 'inbound 443')
            if r[14]:
                out += decode_smp2p(r[64:96], 'outbound 429')
            out.append(f'  global partition @{r[22]:#x}, crash reason 423 @{r[23]:#x} size {r[24]}: {cstr(r[96:104])!r}')
            out.append(f'  ADSP ready bits: {ready_text(r[25])}')
    elif g == 4:
        for i, n in enumerate(SCM_Q):
            out.append(f'  is_call_available({n}) x0={r[4 + 2 * i]:#x} available={r[5 + 2 * i]}')
        out.append(f'  pas_is_supported(1 adsp) x0={r[16]:#x} x1={r[17]}')
        out.append(f'  pas_is_supported(0x24 dtb) x0={r[18]:#x} x1={r[19]}')
    return '\n'.join(out)


def decode_boot(r):
    out = [f'stage 2 result={r[1]:#x} ({ERR.get(r[1], "?")}) step={r[3]} ({STEPS.get(r[3], "?")}) faults={r[2]}',
           f'  ready bits before: {ready_text(r[4])}  votes={r[5]}']
    for name, d in (('dtb', r[16:32]), ('adsp', r[48:64])):
        out.append(f'  {name}: bytes={d[0]} phnum={d[1]} hdr={d[2]} hash={d[3]} loads={d[4]} reloc={d[5]} '
                   f'min={d[6]:#x} max={d[7]:#x}')
        out.append(f'        init_image x0={d[8]:#x} x1={d[9]:#x} mem_setup x0={d[10]:#x} x1={d[11]:#x} '
                   f'copied={d[12]} zeroed={d[13]}')
    q = r[32:40]
    out.append(f'  qmp mbox off={q[0]:#x} size={q[1]} link_ack={q[2]:#x} ch_ack={q[3]:#x} opened={q[4]} sent={q[5]}')
    out.append(f'  rsc lcx={r[40]:#x} lmx={r[41]:#x}  release lcx={r[42]:#x} lmx={r[43]:#x} released={r[15]:#x}')
    out.append(f'  auth dtb x0={r[6]:#x} x1={r[7]:#x}  auth adsp x0={r[8]:#x} x1={r[9]:#x}')
    out.append(f'  wait: bits {ready_text(r[10])} after {r[11]} ms; shutdown x0={r[12]:#x} x1={r[13]:#x}')
    out.append(f'  handover bits {ready_text(r[14])}')
    if r[64]:
        out.append(f'  options {r[64]:#x}: outbound 429 @{r[67]:#x} was there {r[66]}; free_uncached {r[65]:#x} -> {r[68]:#x}; '
                   f'rang first {r[69]}, on the DSP item at loop {r[70]}')
        out += decode_smp2p(r[72:104], 'inbound 443 before the shutdown')
    return '\n'.join(out)


GLINK_CMDS = {0: 'VERSION', 1: 'VERSION_ACK', 2: 'OPEN', 3: 'CLOSE', 4: 'OPEN_ACK', 5: 'INTENT', 6: 'RX_DONE', 7: 'INTENT_REQ',
              8: 'INTENT_REQ_ACK', 9: 'TX_DATA', 11: 'CLOSE_ACK', 12: 'TX_DATA_CONT', 13: 'READ_NOTIF', 14: 'RX_DONE_REUSE', 15: 'SIGNALS'}
GLINK_RESULT = {1: 'listened to the end', 0x201: 'no AP-ADSP partition', 0x202: 'TCSR mutex 3 not taken', 0x203: 'no descriptor or FIFO of a usable size',
                0x204: 'version could not be sent', 0x205: 'the buffer would fall on this code', 0x206: 'no free stream match', 0x212: 'stuck: open with a bad name length', 0x213: 'stuck: intents beyond the ring',
                0x214: 'stuck: data longer than the ring', 0x215: 'stuck: unknown command', 0x211: 'stuck: ring positions cannot be true'}


def decode_glink(r):
    out = [f'glink result={r[1]:#x} ({GLINK_RESULT.get(r[1], "?")}) seconds={r[2]} answer={r[3]} faults={r[4]}',
           f'  descriptor @{r[8]:#x} size {r[9]}; tx 479 @{r[10]:#x} size {r[11]}, made by us {r[12]}',
           f'  rx 480 @{r[13]:#x} size {r[14]} first seen at {r[15]} ms, in the cached end {r[17]}',
           f'  rx messages {r[18]}, tx {r[19]}, tx full {r[20]}, stuck {r[21]}, its version seen {r[22]}, acked {r[23]}, features {r[24]:#x}',
           f'  channels answered {r[25]}, their acks {r[26]}; log {r[27]} records, {r[28]} lost; last at {r[29]} ms',
           f'  tx tail {r[30]:#x} head {r[31]:#x}, rx tail {r[32]:#x} head {r[33]:#x}; ready bits {ready_text(r[34])}, fatal bits {r[16]:#x}',
           f'  crash reason 423 @{r[35]:#x} size {r[36]}: {cstr(r[40:72])!r}']
    if r[3] & 2:
        out += [f'  GPR: adsp_apps lcid {r[79] & 0xffff} rcid {r[79] >> 16}, its ack {r[80] & 0xff}, buffers offered {r[80] >> 8}, its buffers left {r[81]}, data on other channels {r[74]}',
                f'  GPR: asked {r[72]} times, first at {r[76]} ms; answers {r[73]}, first at {r[77]} ms, last at {r[78]} ms; APM READY {r[75]}']
        for what, bytes_, words in (('first', r[82], r[84:100]), ('last', r[83], r[100:108])):
            if bytes_:
                out.append(f'  GPR: {what} answer, {bytes_} bytes: ' + decode_gpr(words, bytes_))
    if r[3] & 4:
        out.append(f'  PRM: {r[124]} steps sent, the first at {r[125]} ms')
        for i, what in enumerate(('macro on', 'dcodec on', 'dcodec off', 'macro off')[:r[124]]):
            status = 'no answer' if r[116 + i] == 0xffffffff else f'status {r[116 + i]:#x}'
            out.append(f'  PRM: {what}: {status}, heard at {r[120 + i]} ms')
    if r[3] & 8:
        out.append(f'  GRAPH: {r[126]} packets sent; the long buffer {r[114]} bytes, refused {r[115]}, have {r[127] & 0xff} asked {r[127] >> 8}')
    return out


GRAPH_STEPS = {3: ('open', 'configure', 'close'),
               15: ('open device', 'configure device', 'open stream', 'configure stream', 'configure its input', 'map the buffers', 'prepare device', 'prepare stream',
                    'start device', 'start stream', 'stop stream', 'stop device', 'close stream', 'close device', 'unmap'),
               29: ('TDM clock on', 'open device', 'configure device', 'open stream', 'configure stream', 'configure its input', 'map the buffers', 'prepare device', 'prepare stream',
                    'start device', 'start stream', 'open way back', 'configure way back', 'open its stream', 'configure its stream', 'prepare way back', 'prepare its stream', 'start way back', 'start its stream',
                    'stop its stream', 'stop way back', 'close its stream', 'close way back', 'stop stream', 'stop device', 'close stream', 'close device', 'unmap', 'TDM clock off'),
               17: ('TDM clock on', 'open device', 'configure device', 'open stream', 'configure stream', 'configure its input', 'map the buffers', 'prepare device', 'prepare stream',
                    'start device', 'start stream', 'stop stream', 'stop device', 'close stream', 'close device', 'unmap', 'TDM clock off')}


TONE_RESULT = {1: 'played to the end', 0x400: 'no map handle', 0x402: 'zeros not played', 0x403: 'the vote faulted', 0x404: 'wrapper clock', 0x405: 'engine', 0x406: "SE's clock",
               0x420: 'I2C', 0x430: 'readback', 0x440: 'OTP', 0x450: 'not who it should be', 0x460: 'PLL', 0x470: 'power up not done', 0x480: 'fault flag'}


def decode_vi(raw, period, start, end):
    """How much 2 kHz, and how much of anything, on each of the four slots: during the tone, and before it. 24 frames a cycle, so the sums need no float tables of their own."""
    import math
    cos = [math.cos(2 * math.pi * k / 24) for k in range(24)]
    sin = [math.sin(2 * math.pi * k / 24) for k in range(24)]
    frames = len(raw) // 16
    untouched = sum(1 for i in range(0, frames * 16, 4) if raw[i:i + 4] == b'ZZZZ')
    out = [f'  VI: {frames} frames back, {frames / 48000:.2f} s; {untouched} of {frames * 4} words still the pattern; first words of the tone window {raw[start * period:start * period + 16].hex()}']
    samples = struct.unpack(f'<{frames * 4}i', raw[:frames * 16])
    def window(first, last, what):
        lines = []
        for slot in range(4):
            a_ = b_ = rms = peak = 0.0
            n = 0
            for f in range(first * period // 16, min(last * period // 16, frames)):
                v = float(samples[f * 4 + slot])
                a_ += v * cos[f % 24]; b_ += v * sin[f % 24]; rms += v * v; peak = max(peak, abs(v)); n += 1
            if n:
                tone = 2 * math.hypot(a_, b_) / n
                lines.append(f'  VI: {what} slot {slot}: 2 kHz {tone:12.0f} ({20 * math.log10(max(tone, 1) / 2147483648):6.1f} dBFS), rms {math.sqrt(rms / n):12.0f}, peak {peak:12.0f}')
        return lines
    out += window(start + 5, end - 5, 'tone  ') if end > start + 10 else ['  VI: no tone window']
    length = max(end - start - 10, 10)
    out += window(max(start - length - 15, 0), max(start - 15, 1), 'before')
    return out


def decode_tone(q):
    words = lambda at: ' '.join(f'{x:#x}' for x in q[at:at + 4])
    out = [f'  TONE: result {q[72]:#x} ({TONE_RESULT.get(q[72], "see the code: 0x401 + step << 8 is a list step")})',
           f'  TONE: zeros played before it {q[73]}, bad {q[74]}; gpio121 {q[75]:#x} gpio124 {q[76]:#x}',
           f'  TONE: prepare {q[77]:#x} at step {q[78]}, OTP fields {q[105]}; power up {q[79]:#x} at step {q[80]} after {q[81]} ms',
           f'  TONE: muted check {q[82]:#x}: {words(83)}',
           f'  TONE: tone: {q[87]} to {q[89]} periods played, {q[88]} to {q[90]} ms, {q[91]} frames; check {q[92]:#x}: {words(93)}',
           f'  TONE: after it check {q[97]:#x}: {words(98)}',
           *[f'  TONE: pads {what}: ' + ' '.join(f'gpio{121 + i} high {q[at + 2 * i] & 0xffff} changes {q[at + 2 * i + 1]}' for i in range(4))
             for what, at in (('muxed, amp in reset', 40), ('amp up, muted, zeros', 48), ('tone', 56))],
           f'  TONE: failed reg {q[102]:#x} want {q[103]:#x} got {q[104]:#x}; reset lines back: gpio183 {q[106]:#x} gpio182 {q[107]:#x}']
    return out


def decode_graph(q, sent):
    out = []
    for i, what in enumerate(GRAPH_STEPS.get(q[39], ())[:sent]):
        status = 'no answer' if q[i] == 0xffffffff else f'status {q[i]:#x}'
        out.append(f'  GRAPH: {what}: {status}, heard at {q[48 + i]} ms')
    if q[39] == 15:
        out.append(f'  PLAY: bypass in stream match {q[41] - 1}; sGFSR {q[40]:#x} before, {q[42]:#x} after, syndrome {q[43]:#x} {q[44]:#x}; first word of the buffer {q[45]:#x}')
        out.append(f'  PLAY: map handle {q[38]:#x}; buffers sent {q[32]}, played {q[33]}, first at {q[34]} ms, last at {q[35]} ms, last status {q[36]:#x}, bad {q[37]}')
        if q[33] > 1:
            ms = (q[35] - q[34]) / (q[33] - 1)
            out.append(f'  PLAY: {ms:.2f} ms a buffer of 20 ms')
    return out


GPR_OPCODES = {0x02001005: 'BASIC_RESULT {opcode, status}', 0x02001007: 'APM_CMD_RSP_GET_SPF_STATE {status: 1 ready}'}


def decode_gpr(w, size):
    text = f'size {w[0] >> 8} domains to {w[1] & 0xff} from {(w[1] >> 8) & 0xff} port {w[2]:#x}->{w[3]:#x} token {w[4]} opcode {w[5]:#x} ({GPR_OPCODES.get(w[5], "?")})'
    return text + ' payload ' + ' '.join(f'{x:#x}' for x in w[6:min(len(w), (size + 3) // 4)])


def decode_glink_log(words, count):
    out = []
    names = []
    for i in range(count):
        w = words[12 * i:12 * i + 12]
        cmd, p1 = w[1] & 0xffff, w[1] >> 16
        sent = cmd & 0x8000
        name = GLINK_CMDS.get(cmd & 0x7fff, f'cmd {cmd & 0x7fff}')
        raw = struct.pack('<8I', *w[4:12])
        line = f'  {w[0]:6} ms {"AP ->" if sent else "   <-"} {name:14} p1={p1:<6} p2={w[2]:#x} extra={w[3]}'
        if (cmd & 0x7fff) == 2:
            text = raw.split(b'\0')[0].decode('ascii', 'replace')
            line += f' name={text!r}'
            if not sent:
                names.append(text)
        elif w[3]:
            line += ' ' + raw[:min(32, w[3])].hex()
        out.append(line)
    out.append(f'  the DSP opened: {names}')
    return out


MIC_STEPS = {1: "the DSP's clock 0x30C", 2: 'LPASS reachable', 3: "macros clocked, the master's version", 4: 'SoundWire frames', 5: 'codec enumerated, its id', 6: "codec's registers", 7: 'init, bias and ADC1 read back'}
MIC_WHY = {1: 'an abort: the hypervisor or a gated block said no', 2: 'the votes were refused or not answered', 3: 'the clock refused all three ways', 4: 'the clock not answered', 5: 'cmd-db does not say 0x40a00 for ldob15: not voted',
           6: 'the RPMh vote failed', 7: 'gpio108 did not take', 8: 'the LPI pads did not take', 9: 'a macro clock bit did not stay', 10: 'the master is not version 2', 11: 'no frames: +0x5064 bit 0 stayed 0', 12: 'nobody attached in a second',
           13: 'not manufacturer 0x0217 part 0x010d', 14: 'a read over the bus did not come back', 15: 'chip id not 0d 01', 16: 'a write refused or lost', 17: 'a register did not read back', 18: 'clash or command error in +0x5000'}
MIC_WANT = [(0x3103, 0xce, 0xce), (0x312a, 0x10, 0), (0x3029, 0xf0, 0x80), (0x3001, 0xc0, 0x80), (0x345b, 2, 2), (0x31e1, 0xff, 0x14), (0x31df, 0x1f, 8), (0x34d0, 0xff, 0x55), (0x34d1, 0xff, 0x44), (0x34d2, 0xff, 0x11), (0x34d3, 0xff, 0), (0x34d4, 0xff, 0),
            (0x306b, 0xe0, 0xe0), (0x306e, 0xe0, 0xe0), (0x3071, 0xe0, 0xe0), (0x3074, 0xe0, 0xe0), (0x308f, 1, 0), (0x3022, 0xff, 0x63), (0x3409, 0xf0, 0xf0), (0x3408, 0x18, 0x18), (0x3417, 1, 1), (0x3457, 3, 2), (0x300f, 0x40, 0), (0x3410, 0xf, 3), (0x300e, 0x1f, 8)]


def decode_mic(r):
    """Every proof step, in order: passed, failed with why, or not reached. Word numbers are AU_MIC_R_* of au_mic.h; the test holds them together."""
    if r[0] != 0xa0d10007:
        return f'mic: bad magic {r[0]:#x}'
    passed, failed, why = r[1], r[2], r[3]
    out = [f'mic: {passed} of 7 proof steps passed' + (f'; stopped at {failed}, {MIC_STEPS.get(failed, "?")}: {MIC_WHY.get(why, why)}' if failed else ('' if passed == 7 else '; the stage did not run')),
           f'  faults {r[4]}, first at {r[5]:#x}; undid {r[6]:#x}; faults on the way down {r[7]}']

    def head(step):
        return f'  {step} {MIC_STEPS[step]}: ' + ('FAILED' if failed == step else 'ok' if passed >= step else 'not reached')
    tries = ' '.join('not tried' if x == 0xfffffffe else 'no answer' if x == 0xffffffff else f'{x:#x}' for x in r[10:13])
    out += [head(1), f'    votes macro {r[8]:#x} dcodec {r[9]:#x}; clock 19.2 MHz, 9.6 MHz, attribute 0: {tries}; took try {r[13]}']
    if passed >= 2 or failed == 2:
        out += [head(2), f'    VA CORE_ID {r[14]:#x} {r[15]:#x} {r[16]:#x} {r[17]:#x}']
    else:
        out.append(head(2))
    if passed >= 3 or failed == 3:
        out += [head(3), f'    cmd-db ldob15 {r[19]:#x}; votes mode {r[20]:#x} volts {r[21]:#x} enable {r[22]:#x}; gpio108 found {r[23]:#x} {r[24]:#x}, set {r[25]:#x} {r[26]:#x}',
                f'    LPI found {r[27]:#x} {r[28]:#x} {r[29]:#x} {r[30]:#x}, set {r[31]:#x} {r[32]:#x} {r[33]:#x} {r[34]:#x}; slew {r[35]:#x} -> {r[36]:#x}',
                f'    VA +0 {r[37]:#x} +4 {r[38]:#x} +8 {r[39]:#x} +80 {r[40]:#x}; TX +0 {r[41]:#x} +4 {r[42]:#x} +8 {r[43]:#x} +90 {r[44]:#x}; master version ' + (f'{r[45]:#x}' if why != 9 else 'not read')]
    else:
        out.append(head(3))
    if passed >= 4 or failed == 4:
        out += [head(4), f'    +5064 {r[46]:#x} after {r[47]} ms; +5000 {r[48]:#x} (bit 1 a new device); +100 {r[49]:#x}; +104c {r[50]:#x}']
    else:
        out.append(head(4))
    if passed >= 5 or failed == 5:
        ident = ' '.join(f'{x:02x}' for x in r[56:62])
        out += [head(5), f'    +1090 {r[51]:#x} after {r[52]} ms; device {r[53]}; enumerator {r[54]:#x} {r[55]:#x}; SCP id {ident}' + (' -- as live DT' if ident == '23 02 17 01 0d 00' else '') + f'; failed reads {r[62]}']
    else:
        out.append(head(5))
    if passed >= 6 or failed == 6:
        variant = (r[70] >> 1) & 0xf
        name = {0: 'WCD9380', 5: 'WCD9385'}.get(variant, 'unknown')
        out += [head(6), f'    chip id {r[63]:02x} {r[64]:02x} {r[65]:02x} {r[66]:02x}; 0x3022 {r[67]:#x} 0x3029 {r[68]:#x} 0x3103 {r[69]:#x}; 0x34b0 {r[70]:#x} variant {variant} ({name}); differ from the driver table {r[71]:#x}']
    else:
        out.append(head(6))
    if passed >= 7 or failed == 7:
        out += [head(7), f'    read back {r[76]} of {len(MIC_WANT)}; +5000 {r[75]:#x}' + (f'; first wrong {r[72]:#x}, want {r[73]:#x}, got {r[74]:#x}' if r[72] else '')]
        out.append('    ' + ' '.join(f'{reg:04x}={r[77 + i]:02x}' for i, (reg, _, _) in enumerate(MIC_WANT[:r[76]])))
    else:
        out.append(head(7))
    out.append(f'  down: 0x3022 {r[104]:#x}; gpio108 {r[105]:#x} {r[106]:#x}; gpio0 pad {r[107]:#x}; TX +8 {r[108]:#x}')
    return '\n'.join(out)


def squash(directory, name, dest):
    """pil-squasher: place every split .bNN segment at its p_offset."""
    mdt = (Path(directory) / f'{name}.mdt').read_bytes()
    assert mdt[:4] == b'\x7fELF' and mdt[4] == 1 and mdt[5] == 1, 'expected ELF32 LE .mdt'
    phoff, = struct.unpack_from('<I', mdt, 28)
    phnum, = struct.unpack_from('<H', mdt, 44)
    out = bytearray(mdt)
    for i in range(phnum):
        _, off, _, _, filesz, _, _, _ = struct.unpack_from('<8I', mdt, phoff + 32 * i)
        if not filesz:
            continue
        if off + filesz <= len(mdt):
            continue  # already inside the .mdt (headers, hash segment)
        seg = (Path(directory) / f'{name}.b{i:02d}').read_bytes()
        assert len(seg) == filesz, f'{name}.b{i:02d}: {len(seg)} != {filesz}'
        if len(out) < off + filesz:
            out.extend(b'\0' * (off + filesz - len(out)))
        out[off:off + filesz] = seg
    Path(dest).write_bytes(out)
    print(f'{dest}: {len(out)} bytes from {name}.mdt + {phnum} phdrs')


def fnv(data):
    h = 2166136261
    for b in data:
        h = ((h ^ b) * 16777619) & 0xffffffff
    return h


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('cmd', choices=['snapshot', 'squash', 'upload', 'boot', 'report', 'decode', 'stop', 'glink', 'smmu', 'poke', 'amp', 'tone', 'mic'])
    ap.add_argument('--monitors', action='store_true', help="tone: also bring the amplifier's voltage and current back, and look for the tone in them")
    ap.add_argument('--owner-agreed', action='store_true', help='tone: the owner has agreed to writes to the amplifier: bottom speaker, lowest level, a second')
    ap.add_argument('--which', type=int, default=3, help='amp: 1 B at 0x40, 2 T at 0x42, 3 both; read only')
    ap.add_argument('--what', type=int, default=0, help='poke: 1 the zeros, 2 the stream match, 8 with it stop at the S2CR, 16 stream 0x1061 rather than 0x1001, 4 take it away, or their sum')
    ap.add_argument('--sid', type=lambda x: int(x, 0), default=0x1001, help='smmu: the stream, by default the DSP buffers of Linux sm8550.dtsi')
    ap.add_argument('--seconds', type=int, default=30, help='glink: how long to listen, at most 45')
    ap.add_argument('--answer', action='store_true', help='glink: complete the opening of channels; still sends no data')
    ap.add_argument('--gpr', action='store_true', help='glink: with --answer, ask the APM on adsp_apps whether it is up')
    ap.add_argument('--prm', action='store_true', help='glink: with --gpr, once ready, vote the macro and dcodec on then off')
    ap.add_argument('--graph', action='store_true', help='glink: with --prm, open, configure and close the TDM graph between the votes')
    ap.add_argument('--play', action='store_true', help='glink: with --graph, instead start them and give them a second of zeros; no pin is muxed')
    ap.add_argument('--outbound', action='store_true', help='boot: create the AP outbound SMP2P item 429 first')
    ap.add_argument('--doorbell', action='store_true', help='boot: with --outbound, also ring LPASS SMP2P')
    ap.add_argument('--wait', type=int, default=0, help='boot: seconds to wait for ready, at most 45')
    ap.add_argument('args', nargs='*')
    ap.add_argument('--groups', default='0,1,2,3')
    ap.add_argument('--dtb', type=Path)
    ap.add_argument('--fw', type=Path)
    ap.add_argument('--timeout', type=int, default=60000, help='control timeout ms')
    ap.add_argument('--save', type=Path, help='append raw 512-byte pages here')
    ap.add_argument('--raw', action='store_true')
    a = ap.parse_args()
    if a.cmd == 'squash':
        if len(a.args) != 3:
            ap.error('squash DIR NAME OUT')
        squash(*a.args)
        return 0
    if a.cmd == 'decode':
        blob = Path(a.args[0]).read_bytes()
        for i in range(0, len(blob) - 511, 512):
            print(decode(struct.unpack_from('<128I', blob, i)))
        return 0
    import usb.core
    import usb.util
    devs = [d for d in usb.core.find(find_all=True, idVendor=VID, idProduct=PID)
            if usb.util.get_string(d, d.iProduct) == PRODUCT]
    if len(devs) != 1:
        ap.error('expected exactly one Fuxi probe (1209:0001)')
    dev = devs[0]

    def req(value, index=0, length=512):
        return bytes(dev.ctrl_transfer(0xc0, 0x5a, value, index, length, timeout=a.timeout))

    def show(page):
        if a.save:
            with a.save.open('ab') as f:
                f.write(page)
        r = struct.unpack('<128I', page)
        print(decode(r))
        if a.raw:
            for i in range(0, 128, 8):
                print('  w%3d:' % i, ' '.join('%08x' % x for x in r[i:i + 8]))

    if a.cmd == 'snapshot':
        for g in [int(x) for x in a.groups.split(',')]:
            show(req(0x4153, g))
    elif a.cmd == 'upload':
        if not a.dtb or not a.fw:
            ap.error('upload needs --dtb and --fw (squashed MBNs)')
        dev.ctrl_transfer(0xc0, 0x54, 30, 0, 8, timeout=a.timeout)  # reset deadline: +30 min
        for img, path in ((0, a.dtb), (1, a.fw)):
            data = path.read_bytes()
            if len(data) % 512 == 0:
                data += b'\0'  # the device closes an image on a short chunk; trailing pad is harmless
            for c in range((len(data) + 511) // 512):
                part = data[c * 512:(c + 1) * 512]
                idx = (img << 15) | (0x4000 if c == 0 else 0) | (c >> 16)
                n = dev.ctrl_transfer(0x40, 0x5b, c & 0xffff, idx, part, timeout=a.timeout)
                assert n == len(part), f'short OUT at chunk {c}'
                if c % 4096 == 0:
                    print(f'  image {img}: {c * 512}/{len(data)}', flush=True)
            st = struct.unpack('<12I', req(0x4155, 0, 48))
            want = fnv(data)
            got_len, got_hash, done = st[1 + 3 * img:4 + 3 * img]
            print(f'image {img}: device len={got_len} hash={got_hash:#x} done={done}; host len={len(data)} hash={want:#x}')
            if got_len != len(data) or got_hash != want:
                raise SystemExit('staging mismatch')
    elif a.cmd == 'boot':
        if a.outbound or a.doorbell or a.wait:
            if a.doorbell and not a.outbound:
                ap.error('--doorbell needs --outbound')
            if not 0 <= a.wait <= 45:
                ap.error('--wait is at most 45')
            show(req(0x4158, int(a.outbound) | int(a.doorbell) << 1 | a.wait << 8))
        else:
            show(req(0x4142))
    elif a.cmd == 'poke':
        r = struct.unpack('<16I', req(0x4154, a.what, 64))
        print(f'poke {r[1]}: magic {r[0]:#x}, this code at {r[2]:#x}, stack at {r[3]:#x}, faults {r[13]}')
        print(f'  zeros: before {r[4]:#x}, after {r[5]:#x} and {r[6]:#x}')
        print(f'  bypass: sGFSR before {r[7]:#x}; slot+1 {r[8]}, SMR {r[9]:#x} S2CR {r[10]:#x}; sGFSR after {r[11]:#x}, after taking it away {r[12]:#x}; got to step {r[14]} of 3')
    elif a.cmd == 'tone':
        if not a.owner_agreed:
            ap.error('tone writes to an amplifier: --owner-agreed')
        page = req(0x414f, 0x4232 if a.monitors else 0x4231)
        r = struct.unpack('<128I', page)
        if r[0] != 0xa0d10003:
            raise SystemExit(f'no report: {r[0]:#x}')
        print('\n'.join(decode_glink(r)))
        q = struct.unpack('<128I', req(0x4149))
        print('\n'.join(decode_graph(q, r[126])))
        q3 = struct.unpack('<128I', req(0x414a))
        print('\n'.join(decode_tone(q3)))
        if a.monitors:
            print(f'  VI: apps_smmu sGFSR {q3[118]:#x} syndrome {q3[119]:#x} {q3[120]:#x}; bank 4 FSR {q3[121]:#x} FAR {q3[122]:#x} {q3[123]:#x}')
            print(f'  VI: buffers sent {q3[108]} back {q3[109]}, last status {q3[110]:#x}, bad {q3[111]}, last {q3[112]} bytes at offset {q3[113]}; during the tone {q3[114]} to {q3[115]}')
            raw = bytearray()
            base, period = q3[116], q3[117]
            for page_ in range(q3[109] * period // 512):
                raw += req(0x4156, page_)
            beyond = bytes(req(0x4156, (q3[109] + 20) * period // 512))
            print(f'  VI: 20 periods past the last: {beyond[:16].hex()}: the pattern if the DSP has stopped there')
            if a.save:
                a.save.with_suffix('.vi').write_bytes(raw)
            print('\n'.join(decode_vi(raw, period, q3[114], q3[115])))
        count = min(r[27], 96)
        words = []
        for p_ in range((count * 12 + 127) // 128):
            words += struct.unpack('<128I', req(0x4148, p_))
        print('\n'.join(decode_glink_log(words, count)))
        if a.save:
            with a.save.open('ab') as f:
                f.write(page + struct.pack('<128I', *q))
    elif a.cmd == 'mic':
        page = req(0x4143, 0x4d31)
        r = struct.unpack('<128I', page)
        if r[0] != 0xa0d10003:
            raise SystemExit(f'no report: {r[0]:#x}')
        print('\n'.join(decode_glink(r)))
        m = struct.unpack('<128I', req(0x4145))
        print(decode_mic(m))
        q = struct.unpack('<128I', req(0x4149))
        status = 'no answer' if q[3] == 0xffffffff else f'status {q[3]:#x}'
        print(f'  the clock let go: {status}, heard at {q[51]} ms (the list sent {r[126]} steps: 4 means it was asked)')
        count = min(r[27], 96)
        words = []
        for p_ in range((count * 12 + 127) // 128):
            words += struct.unpack('<128I', req(0x4148, p_))
        print('\n'.join(decode_glink_log(words, count)))
        if a.save:
            with a.save.open('ab') as f:
                f.write(page + struct.pack('<128I', *m))
    elif a.cmd == 'amp':
        r = struct.unpack('<128I', req(0x4141, a.which, 512))
        results = {1: 'done', 0x301: 'the vote faulted', 0x302: 'wrapper clocks did not come', 0x303: 'the engine could not be read',
                   0x304: 'no I2C firmware with FIFOs, or busy', 0x305: "the SE's clock did not come"}
        print(f'amp: result {r[1]:#x} ({results.get(r[1], "?")}), which {r[2]}, faults {r[21]}; vote {r[4]:#x}, m_ahb {r[5]:#x} s_ahb {r[6]:#x}, se0 {r[10]:#x}')
        print(f'  engine: fw {r[7]:#x}, if disable {r[8]:#x}, status {r[9]:#x}')
        print(f'  pins found: gpio0 {r[11]:#x} gpio1 {r[12]:#x} gpio182 {r[13]:#x} gpio183 {r[14]:#x}; reset set low {r[15]:#x} {r[16]:#x}; back {r[19]:#x} {r[20]:#x}; SDA, SCL idle in {r[17] & 1} {r[18] & 1}')
        outcome = {0: 'ok', 1: 'NACK', 2: 'bus error', 3: 'timeout', 4: 'fault', 5: 'short'}
        for p, name in enumerate(('B', 'T')):
            m = r[24 + 12 * p:36 + 12 * p]
            if not m[10]:
                continue
            good = m[6] == 0x35a40 and m[7] in (0xb0, 0xb2) and not (m[5] >> 31)
            print(f'  {name} @{m[1]:#x}: {outcome.get(m[2], m[2])}, reset line io {m[0]:#x}; OTP boot {m[4]:#x} after {m[3]} ms, error word {m[5]:#x}; DEVID {m[6]:#x} REVID {m[7]:#x} OTPID {m[8]:#x}; irq seen {m[9]:#x}' +
                  (' -- a CS35L41' if good else ''))
    elif a.cmd == 'smmu':
        r = struct.unpack('<128I', req(0x414d, a.sid))
        if r[0] != 0xa0d10004:
            raise SystemExit(f'no smmu report: {r[0]:#x}')
        print(f'apps_smmu for stream {r[1]:#x}: faults {r[2]} sCR0 {r[3]:#x} (unmatched streams {"fault" if r[3] & 0x400 else "bypass"}) IDR0 {r[4]:#x} IDR1 {r[5]:#x}, {r[6]} matches')
        for i in range(min(r[6], 20)):
            m = r[7 + 6 * i:13 + 6 * i]
            kind = {0: 'translates', 1: 'bypass', 2: 'fault'}.get((m[2] >> 16) & 3, '?')
            print(f'  group {m[0]}: SMR {m[1]:#x} (id {m[1] & 0xffff:#x} mask {(m[1] >> 16) & 0x7fff:#x}) S2CR {m[2]:#x} {kind}' +
                  (f', CB {m[2] & 0xff}: SCTLR {m[3]:#x} TCR {m[4]:#x} CBAR {m[5]:#x}' if ((m[2] >> 16) & 3) == 0 else ''))
    elif a.cmd == 'stop':
        x0, x1 = struct.unpack('<2I', req(0x415a, 0, 8))
        print(f'pas shutdown x0={x0:#x} x1={x1:#x}')
    elif a.cmd == 'glink':
        if not 1 <= a.seconds <= 45:
            ap.error('--seconds is 1 to 45')
        if (a.gpr and not a.answer) or (a.prm and not a.gpr) or (a.graph and not a.prm) or (a.play and not a.graph):
            ap.error('--play needs --graph, --graph needs --prm, --prm needs --gpr, and --gpr needs --answer')
        page = req(0x4147, a.seconds | int(a.answer) << 8 | int(a.gpr) << 9 | int(a.prm) << 10 | int(a.graph) << 11 | int(a.play) << 12)
        r = struct.unpack('<128I', page)
        if r[0] != 0xa0d10003:
            raise SystemExit(f'no glink report: {r[0]:#x}')
        print('\n'.join(decode_glink(r)))
        if r[3] & 8:
            print('\n'.join(decode_graph(struct.unpack('<128I', req(0x4149)), r[126])))
        count = min(r[27], 96)
        words = []
        for p in range((count * 12 + 127) // 128):
            words += struct.unpack('<128I', req(0x4148, p))
        print('\n'.join(decode_glink_log(words, count)))
        if a.save:
            with a.save.open('ab') as f:
                f.write(page + struct.pack(f'<{len(words)}I', *words))
    elif a.cmd == 'report':
        show(req(0x4152))
    return 0


if __name__ == '__main__':
    sys.exit(main())
