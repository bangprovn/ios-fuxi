#!/usr/bin/env python3
"""Fuxi audio lane milestone-1 host tool (probe requests 0x5a/0x5b, audio.c).

  snapshot [--groups 0,1,2,3]  read-only snapshot pages (group 4 = SCM queries)
  squash DIR NAME OUT          merge NAME.mdt + NAME.bNN into one MBN (no device)
  upload --dtb F --fw F        stage squashed adsp_dtb/adsp MBNs over control OUT
  boot                         stage 2: PAS-boot the ADSP once, then decode
  report                       re-read the frozen stage-2 report
  glink [--seconds N] [--answer] [--gpr] [--prm]  after a boot that reached ready: GLINK with the DSP, polled; no data is sent unless --gpr asks the APM its state; --prm then votes LPASS on and off; --graph between them opens, configures and closes the TDM graph, never starting it
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
                0x204: 'version could not be sent', 0x212: 'stuck: open with a bad name length', 0x213: 'stuck: intents beyond the ring',
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
        for i, what in enumerate(('open', 'configure', 'close')[:r[126]]):
            status = 'no answer' if r[108 + i] == 0xffffffff else f'status {r[108 + i]:#x}'
            out.append(f'  GRAPH: {what}: {status}, heard at {r[111 + i]} ms')
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
    ap.add_argument('cmd', choices=['snapshot', 'squash', 'upload', 'boot', 'report', 'decode', 'stop', 'glink'])
    ap.add_argument('--seconds', type=int, default=30, help='glink: how long to listen, at most 45')
    ap.add_argument('--answer', action='store_true', help='glink: complete the opening of channels; still sends no data')
    ap.add_argument('--gpr', action='store_true', help='glink: with --answer, ask the APM on adsp_apps whether it is up')
    ap.add_argument('--prm', action='store_true', help='glink: with --gpr, once ready, vote the macro and dcodec on then off')
    ap.add_argument('--graph', action='store_true', help='glink: with --prm, open, configure and close the TDM graph between the votes')
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
    elif a.cmd == 'stop':
        x0, x1 = struct.unpack('<2I', req(0x415a, 0, 8))
        print(f'pas shutdown x0={x0:#x} x1={x1:#x}')
    elif a.cmd == 'glink':
        if not 1 <= a.seconds <= 45:
            ap.error('--seconds is 1 to 45')
        if (a.gpr and not a.answer) or (a.prm and not a.gpr) or (a.graph and not a.prm):
            ap.error('--graph needs --prm, --prm needs --gpr, and --gpr needs --answer')
        page = req(0x4147, a.seconds | int(a.answer) << 8 | int(a.gpr) << 9 | int(a.prm) << 10 | int(a.graph) << 11)
        r = struct.unpack('<128I', page)
        if r[0] != 0xa0d10003:
            raise SystemExit(f'no glink report: {r[0]:#x}')
        print('\n'.join(decode_glink(r)))
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
