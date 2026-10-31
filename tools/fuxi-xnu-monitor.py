#!/usr/bin/env python3
"""Collect early XNU progress from Fuxi's isolated native USB monitor.

The image must match --payload and --kernel. Only the selected 1 GiB XNU RAM
window and the validated fixed splash framebuffer can be read; no storage writes or unrestricted physical-memory requests.
Private output includes native reports, staged progress and console bytes.
"""
import argparse
import json
from pathlib import Path
import re
import struct
import time
import zlib

import usb.core
import usb.util

MON_FIELDS = 'magic delegate active usb_mpidr launch stage boot_args entry deadline image end'.split()
WANTED = {'_fuxi_early_status', '_sweet_log_ring', '_sweet_log_total'}
OPTIONAL = {'_sweet_log_size'}  # xnu27 fuxi/0006: ring size (absent: the 4 MiB xnu-8796 ring)
RING_DEFAULT = 0x400000


def symbols(path):
    b = path.read_bytes()
    magic, cpu, _, filetype, count, _, _, _ = struct.unpack_from('<8I', b)
    if magic != 0xfeedfacf or cpu != 0x100000c:
        raise ValueError('expected an arm64 Mach-O kernel')
    offset = 32
    if filetype == 12:  # K2 MH_FILESET (tools/ios27-prelink.py): symbols live in the com.apple.kernel entry
        for _ in range(count):
            command, size = struct.unpack_from('<II', b, offset)
            if command == 0x80000035:
                fileoff, name = struct.unpack_from('<QI', b, offset + 16)[0], struct.unpack_from('<I', b, offset + 24)[0]
                if b[offset + name:b.index(b'\0', offset + name)] == b'com.apple.kernel':
                    break
            offset += size
        else:
            raise ValueError('fileset has no com.apple.kernel entry')
        count = struct.unpack_from('<I', b, fileoff + 16)[0]
        offset = fileoff + 32
    result = {}
    for _ in range(count):
        command, size = struct.unpack_from('<II', b, offset)
        if command == 2:
            start, n, strings, _ = struct.unpack_from('<4I', b, offset + 8)
            for i in range(n):
                nameoff, kind, _, _, value = struct.unpack_from('<IBBHQ', b, start + 16 * i)
                if kind & 0x0e != 0x0e:
                    continue
                name_start = strings + nameoff
                name = b[name_start:b.find(b'\0', name_start)].decode(errors='replace')
                if name in WANTED | OPTIONAL:
                    result[name] = value
        offset += size
    if not WANTED <= result.keys():
        raise ValueError(f'missing XNU diagnostic symbols: {WANTED - result.keys()}')
    return result


# ---- thermal lane (loader request 0x57) ----
# Sensor names from the live FDT /soc/thermal-zones (tsens0/1/2 = 16/16/9 sensors).
TSENS_NAMES = [
    ['aoss-0', 'cpuss-0', 'cpuss-1', 'cpuss-2', 'cpuss-3'] + ['cpu-1-%d' % i for i in range(11)],
    ['aoss-1', 'cpu-0-0', 'cpu-0-1', 'cpu-0-2', 'nspss-0', 'nspss-1', 'nspss-2', 'nspss-3',
     'video', 'ddr', 'mdmss-0', 'mdmss-1', 'mdmss-2', 'mdmss-3', 'camera-0', 'camera-1'],
    ['aoss-2'] + ['gpuss-%d' % i for i in range(8)],
]
EPSS_DOMAINS = ['silver(cpu0-2 A510)', 'gold(cpu3-6 A715/A710)', 'prime(cpu7 X3)']


def lut_decode(raw):
    w = struct.unpack('<84I', raw)
    if w[0] != 0x3155554c:
        raise ValueError('bad LUT reply')
    rows = []
    for i in range(w[2]):
        f, v = w[4 + i], w[44 + i]
        mhz = round(19.2 * (f & 0xff), 1) if f >> 30 else 300.0
        rows.append({'index': i, 'mhz': mhz, 'mv': v & 0xfff, 'turbo': (f >> 16) & 7 == 1, 'raw': hex(f)})
    return {'domain': w[1], 'count': w[2], 'rows': rows}


def thermal_decode(raw, luts=None):
    w = struct.unpack('<90I', raw)
    if w[0] != 0x31524854:
        raise ValueError('bad thermal reply')
    mask = w[1]
    out = {'mask': mask, 'cntpct': w[2] | w[3] << 32, 'temps': {}, 'invalid': [], 'tsens': [], 'cpu': []}
    for c in range(3):
        t = w[4 + 20 * c: 24 + 20 * c]
        out['tsens'].append({'srot_ver': hex(t[0]), 'srot_ctrl': hex(t[1]), 'int_en': hex(t[2]), 'trdy': hex(t[3])})
        if not mask & (1 << c):
            continue
        for s, name in enumerate(TSENS_NAMES[c]):
            v = t[4 + s]
            d = v & 0xfff
            if d & 0x800:
                d -= 0x1000
            if v >> 21 & 1:
                out['temps'][name] = d / 10
            else:
                out['invalid'].append((name, hex(v)))
    for d in range(3):
        e = w[64 + 8 * d: 72 + 8 * d]
        if not mask & 0x10:
            continue
        info = {'domain': EPSS_DOMAINS[d], 'enable': hex(e[0]), 'state_mhz': round(19.2 * (e[1] & 0xff), 1),
                'state_raw': hex(e[1]), 'dcvs_ctrl': hex(e[2]), 'perf_state': list(e[3:7])}
        if luts and d in luts and e[3] < luts[d]['count']:
            info['vote_mhz'] = luts[d]['rows'][e[3]]['mhz']
        out['cpu'].append(info)
    out['monitor_idle'] = {'mode': w[88], 'cntkctl': hex(w[89])}
    if out['temps']:
        out['max'] = max(out['temps'].items(), key=lambda kv: kv[1])
    return out


def thermal_brief(t):
    temps = t['temps']
    def grp(prefix):
        v = [x for k, x in temps.items() if k.startswith(prefix)]
        return max(v) if v else None
    parts = ['max %s %.1fC' % tuple(t['max']) if 'max' in t else 'no temps',
             'silver %s' % grp('cpu-0-'), 'gold/prime %s' % grp('cpu-1-'), 'cpuss %s' % grp('cpuss'),
             'gpu %s' % grp('gpuss'), 'ddr %s' % temps.get('ddr')]
    if t['invalid']:
        parts.append('%d invalid' % len(t['invalid']))
    if t['cpu']:
        parts.append('MHz ' + '/'.join(str(c['state_mhz']) for c in t['cpu']))
    return ', '.join(parts)


def parse_cpu_spec(spec):
    """'0:3,1:@1200,2:@1500' -> {domain: ('level', n) | ('mhz', f)}"""
    result = {}
    for item in spec.split(','):
        d, v = item.split(':')
        d = int(d)
        if d not in (0, 1, 2):
            raise ValueError('EPSS domain must be 0, 1 or 2')
        result[d] = ('mhz', float(v[1:])) if v.startswith('@') else ('level', int(v))
    return result


def cpu_level_for(lut, kind, value):
    rows = [r for r in lut['rows'] if not r['turbo']] or lut['rows']
    if kind == 'level':
        if not 0 <= value < lut['count']:
            raise ValueError(f'level {value} outside LUT (count {lut["count"]})')
        return value
    below = [r for r in rows if r['mhz'] <= value]
    return (below[-1] if below else rows[0])['index']


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--kernel', type=Path, required=True)
    ap.add_argument('--payload', type=Path, required=True)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--seconds', type=float, default=65)
    ap.add_argument('--framebuffer',action='store_true',help='save the validated inherited framebuffer after collecting logs')
    ap.add_argument('--tour-shots',action='store_true',help='also save framebuffer-shotN-NAME.png at each "FUXI TOUR: shot N NAME" console line (fuxitour=S)')
    ap.add_argument('--skip-clocks',action='store_true',help='do not take the AHB-ungating clock snapshot before handoff')
    ap.add_argument('--reset-minutes',type=int,help='move the monitor PSCI reset deadline to now + N minutes (1-60)')
    ap.add_argument('--touch-test',type=float,metavar='SECONDS',help='before handoff: touch snapshot, power-up, then poll touch events for SECONDS')
    ap.add_argument('--touch-stage',default='9',help='touch power-up: stop after this stage (1 SE GSI mode, 2 GPI init, 9 all)')
    ap.add_argument('--touch-power',action='store_true',help='loader touch power-up (rails, reset, sense on) right before the handoff')
    ap.add_argument('--no-handoff',action='store_true',help='stop after the touch test; the loader resets at its 90 s deadline')
    ap.add_argument('--display-poll',action='store_true',help='sample CTL0 START, INTF1 autorefresh and DSI status/FIFO each poll')
    ap.add_argument('--thermal-log',type=float,metavar='SECONDS',help='sample TSENS + EPSS every SECONDS after handoff into thermal.jsonl')
    ap.add_argument('--thermal-probe',action='store_true',help='before handoff: staged first-time 0x57 probe (each TSENS/EPSS block separately) + LUT dump')
    ap.add_argument('--thermal-mask',type=lambda x:int(x,0),default=0x17,help='0x57 TH mask for samples (1/2/4 TSENS TM, 8 SROT, 0x10 EPSS)')
    ap.add_argument('--thermal-test',type=float,metavar='SECONDS',help='before handoff: sample once per second for SECONDS (with --no-handoff: stop there)')
    ap.add_argument('--monitor-idle',type=int,choices=(0,1,2),help='cpu@100 idle between USB polls: 0 busy, 1 delay 500 us, 2 WFE + timer event stream')
    ap.add_argument('--cpu-level',metavar='SPEC',help="before handoff: EPSS perf level per domain, e.g. '0:3,1:@1200,2:@1200' (LUT index, or @MHz = highest non-turbo row <= MHz)")
    ap.add_argument('--cpu-at',action='append',default=[],metavar='SECONDS=SPEC',help='after handoff: apply SPEC (as --cpu-level, or idle:MODE) at SECONDS after handoff (repeatable)')
    args = ap.parse_args()
    payload = args.payload.read_bytes()
    if payload[:8] != b'SWEETXNU':
        ap.error('invalid payload')
    virt, _, _, _, _, phys, size = struct.unpack_from('<7Q', payload, 16)
    if phys != 0x8c0000000 or size not in (0x40000000, 0x30000000, 0x13e000000):
        ap.error('unexpected Fuxi RAM window')
    line = 1024 if struct.unpack_from('<I', payload, 8)[0] == 2 else 608  # payload v2: xnu-12377
    cmdline = payload[72:72 + line].split(b'\0')[0].decode()
    if 'fuxiperf=1' in cmdline.split() and (args.cpu_level or args.cpu_at):
        ap.error('fuxiperf=1 owns EPSS: do not pass --cpu-level or --cpu-at')
    offsets = {name: value - virt for name, value in symbols(args.kernel).items()}
    if any(not 0 <= off < size - RING_DEFAULT for off in offsets.values()):
        ap.error('symbol outside XNU window')
    devices = list(usb.core.find(find_all=True, idVendor=0x1209, idProduct=0x0001))
    devices = [d for d in devices if usb.util.get_string(d, d.iProduct) == 'Fuxi hardware probe']
    if len(devices) != 1:
        ap.error('expected exactly one Fuxi native monitor')
    dev = devices[0]
    args.out.mkdir(parents=True, exist_ok=False)

    def request(number, length, value=0, index=0, timeout=3000):
        data = bytes(dev.ctrl_transfer(0xc0, number, value, index, length, timeout=timeout))
        if len(data) != length:
            raise ValueError(f'short USB response: {number:#x}')
        return data

    def monitor(value=0):
        b = request(0x46, 88, value)
        if b[:8] != b'FXIMON01':
            raise ValueError('wrong native monitor protocol')
        return dict(zip(MON_FIELDS, struct.unpack('<11Q', b)))

    def read_ram(offset, length):
        if offset < 0 or length < 0 or offset + length > size:
            raise ValueError('RAM request outside XNU window')
        output = bytearray()
        for off in range(offset, offset + length, 512):
            output.extend(request(0x58 if off >= 0x100000000 else 0x47, min(512, offset + length - off), off & 0xffff, (off >> 16) & 0xffff))
        return bytes(output)

    report = request(0x40, 152)
    if report[:8] != b'FXIPROBE':
        raise ValueError('wrong hardware report')
    (args.out / 'native-report.bin').write_bytes(report)
    core = struct.unpack('<10Q', request(0x45, 80))
    if core[2] or not core[3] or core[5] & 0xffffff != 0x100:
        raise ValueError(f'second CPU startup failed: {core}')
    monitor(1)
    for _ in range(100):
        time.sleep(.01)
        before = monitor()
        if before['active'] and before['usb_mpidr'] & 0xffffff == 0x100:
            break
    else:
        raise ValueError('USB polling did not move to cpu@100')
    (args.out / 'before.json').write_text(json.dumps(before, indent=2)+'\n')
    print('USB monitor active on cpu@100; requesting XNU handoff', flush=True)
    ufs=request(0x4b,96)
    names='magic version ahb axi unipro gdsc valid reserved cap hci_version ahit interrupt_status interrupt_enable host_status host_enable list_lo list_hi doorbell list_running task_list_lo task_list_hi task_doorbell task_running uic_command'.split()
    (args.out / 'ufs-before.json').write_text(json.dumps(dict(zip(names,struct.unpack('<24I',ufs))),indent=2)+'\n')
    (args.out / 'display-before.bin').write_bytes(request(0x42,432))
    clock_names='magic version gcc_ahb gcc_hf_axi valid gdsc ahb mdp byte0 byte0_intf pclk0 esc0 vsync non_gdsc_ahb'.split()
    if not args.skip_clocks:
        clock_raw=request(0x4c,56,1)
        (args.out / 'display-clocks-before.json').write_text(json.dumps(dict(zip(clock_names,struct.unpack('<14I',clock_raw))),indent=2)+'\n')
    def display_poll():
        # Read-only 0x4f windows: 1 CTL0, 2 INTF1, 9 DSI0 (never window 0).
        word=lambda w,o: struct.unpack('<I',request(0x4f,4,w,o))[0]
        return {'ctl_start':word(1,0x1c),'autorefresh':word(2,0x2b4),
                'dsi_status':word(9,0x4),'dsi_fifo':word(9,0xc)}
    if args.display_poll:
        print('display before handoff', display_poll(), flush=True)
    # ---- thermal lane: pre-handoff probe / levels / idle ----
    luts = {}
    thermal_on = bool(args.thermal_probe or args.thermal_log or args.thermal_test or args.cpu_level or
                      args.cpu_at or args.monitor_idle is not None)
    def thermal_sample(mask=None):
        return thermal_decode(request(0x57, 360, 0x5448, args.thermal_mask if mask is None else mask), luts)
    def cpu_apply(spec, log):
        if spec.startswith('idle:'):  # --cpu-at SECONDS=idle:MODE switches the monitor idle mode mid-run
            w = struct.unpack('<5I', request(0x57, 20, 0x4d49, int(spec[5:])))
            log.append({'monitor_idle': w[1], 'time': time.time()})
            print('monitor idle mode', w[1], flush=True)
            return
        for d, (kind, value) in sorted(parse_cpu_spec(spec).items()):
            if d not in luts:
                luts[d] = lut_decode(request(0x57, 336, 0x4c55, d))
            level = cpu_level_for(luts[d], kind, value)
            w = struct.unpack('<14I', request(0x57, 56, 0x434c, d | level << 8))
            rec = {'domain': d, 'level': level, 'mhz': luts[d]['rows'][level]['mhz'], 'rc': w[8],
                   'state_before_mhz': round(19.2 * (w[6] & 0xff), 1), 'state_after_mhz': round(19.2 * (w[9] & 0xff), 1),
                   'perf_before': w[7], 'perf_after': list(w[10:14]), 'time': time.time()}
            log.append(rec)
            print('cpu level', rec, flush=True)
            if w[8]:
                raise ValueError(f'CPU level request refused: {rec}')
    thermal_events = []
    if thermal_on:
        stages = []
        if args.thermal_probe:
            # One block per request: if the SoC resets, the last printed stage is the culprit.
            for name, mask in (('tsens0 TM', 1), ('tsens1 TM', 2), ('tsens2 TM', 4), ('EPSS state', 0x10), ('TSENS SROT', 8)):
                print('thermal probe stage', name, '...', flush=True)
                (args.out / 'thermal-stage.txt').write_text(name + '\n')
                t = thermal_sample(mask)
                stages.append({'stage': name, 'result': t})
                print('thermal probe', name, 'ok:', thermal_brief(t), flush=True)
            for d in range(3):
                print('thermal probe stage LUT', d, '...', flush=True)
                (args.out / 'thermal-stage.txt').write_text('LUT %d\n' % d)
                luts[d] = lut_decode(request(0x57, 336, 0x4c55, d))
        elif args.thermal_log or args.cpu_level or args.cpu_at:
            for d in range(3):
                luts[d] = lut_decode(request(0x57, 336, 0x4c55, d))
        for d, lut in sorted(luts.items()):
            print('EPSS', EPSS_DOMAINS[d], 'LUT MHz', [(r['mhz'], r['mv']) for r in lut['rows']], flush=True)
        (args.out / 'thermal-probe.json').write_text(json.dumps({'stages': stages, 'luts': luts}, indent=1) + '\n')
        if args.monitor_idle is not None:
            w = struct.unpack('<5I', request(0x57, 20, 0x4d49, args.monitor_idle))
            print('monitor idle mode', w[1], 'on mpidr', hex(w[3]), 'CLIDR', hex(w[4]),
                  'LoUIS', w[4] >> 21 & 7, flush=True)
            thermal_events.append({'monitor_idle': args.monitor_idle, 'clidr': hex(w[4]), 'time': time.time()})
        if args.cpu_level:
            cpu_apply(args.cpu_level, thermal_events)
        if args.thermal_test:
            with (args.out / 'thermal-test.jsonl').open('w') as f:
                end = time.monotonic() + args.thermal_test
                while time.monotonic() < end:
                    t0 = time.monotonic()
                    t = thermal_sample()
                    t['time'] = time.time()
                    t['request_ms'] = round((time.monotonic() - t0) * 1000, 2)
                    f.write(json.dumps(t) + '\n'); f.flush()
                    print('thermal', thermal_brief(t), 'req %.1f ms' % t['request_ms'], flush=True)
                    time.sleep(1)
        (args.out / 'thermal-events.json').write_text(json.dumps(thermal_events, indent=1) + '\n')
        if args.no_handoff and args.touch_test is None:
            raise SystemExit('thermal test done; no handoff')
    cpu_at = sorted((float(x.split('=', 1)[0]), x.split('=', 1)[1]) for x in args.cpu_at)
    if args.reset_minutes:
        if not 1 <= args.reset_minutes <= 60:
            raise ValueError('--reset-minutes must be 1-60')
        request(0x54, 8, args.reset_minutes)
        print(f'monitor reset deadline: +{args.reset_minutes} min', flush=True)
    input_stop = args.touch_test == 0
    if args.touch_test is not None:
        words=lambda b: list(struct.unpack('<64I',b))
        snap=[]
        for group,count in ((0,15),(1,12),(2,17),(4,10),(5,60)):
            print('touch snapshot group',group,'...',flush=True)
            w=words(request(0x55,256,0x5453,group))
            print('touch snapshot',group,[hex(v) for v in w[:count+1]],flush=True)
            snap.append(w)
        if input_stop:
            (args.out/'touch.json').write_text(json.dumps({'snapshot':snap},indent=1)+'\n')
            raise SystemExit('touch snapshot only')
        for stage in [int(x) for x in args.touch_stage.split(',')]:
            print('touch power stage',stage,'...',flush=True)
            power=words(request(0x55,256,0x5450,stage,timeout=20000))
            print('touch power',stage,[hex(v) for v in power],flush=True)
        events=[]
        until=time.monotonic()+args.touch_test
        while power[0]==6 and time.monotonic()<until:
            b=bytes(dev.ctrl_transfer(0xc0,0x55,0x5445,0,256,timeout=3000))
            for i in range(0,len(b),8):
                events.append((round(time.monotonic(),3),b[i:i+8].hex()))
                print('touch event',b[i:i+8].hex(),flush=True)
            time.sleep(.02)
        (args.out/'touch.json').write_text(json.dumps({'snapshot':snap,'power':power,'events':events},indent=1)+'\n')
        if args.no_handoff:
            raise SystemExit('touch test done; no handoff')
    if args.touch_power:
        power=list(struct.unpack('<64I',request(0x55,256,0x5450,9,timeout=20000)))
        (args.out/'touch-power.json').write_text(json.dumps(power)+'\n')
        print('touch power',[hex(v) for v in power[:36]],flush=True)
    if args.no_handoff:
        raise SystemExit('--no-handoff: stopping before the XNU handoff')
    request(0x49, 88, 0x584e)
    handoff = time.monotonic()
    next_thermal = handoff
    thermal_file = (args.out / 'thermal.jsonl').open('w') if args.thermal_log else None
    last_total = 0
    previous = None
    until = time.monotonic() + args.seconds
    renew = time.monotonic() + 600
    def save_framebuffer(name):
        raw=bytearray()
        for off in range(0,1080*2400*4,512):
            raw.extend(request(0x4e,min(512,1080*2400*4-off),off&0xffff,off>>16))
        if name=='framebuffer':
            (args.out/'framebuffer.bgra').write_bytes(raw)
        rows=[]
        for y in range(2400):
            line=raw[y*4320:(y+1)*4320]
            rgb=bytearray(1080*3)
            rgb[0::3],rgb[1::3],rgb[2::3]=line[2::4],line[1::4],line[0::4]
            rows.append(b'\0'+rgb)
        def chunk(kind,data):
            return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data))
        png=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',1080,2400,8,2,0,0,0))
        png+=chunk(b'IDAT',zlib.compress(b''.join(rows),6))+chunk(b'IEND',b'')
        (args.out/(name+'.png')).write_bytes(png)
    tour_tail, tour_done, ring = b'', set(), None
    with (args.out / 'progress.jsonl').open('w') as progress, (args.out / 'boot.log').open('wb') as log:
        while time.monotonic() < until:
            status = monitor()
            if status['stage'] == 99:
                status['error'] = request(0x48, 128).split(b'\0', 1)[0].decode(errors='replace')
            if status['stage'] >= 8 and status['stage'] != 99:
                early = read_ram(offsets['_fuxi_early_status'], 128)
                status['kernel_stage'] = struct.unpack_from('<Q', early)[0]
                status['kernel_label'] = early[8:].split(b'\0', 1)[0].decode(errors='replace')
                total = struct.unpack('<Q', read_ram(offsets['_sweet_log_total'], 8))[0]
                status['console_total'] = total
                if not last_total <= total < 0x10000000:
                    raise ValueError(f'invalid console counter: {total}')
                start = max(last_total, total - 65536)
                if start > last_total:
                    status['missed_console_bytes'] = start - last_total
                if ring is None:
                    ring = struct.unpack('<Q', read_ram(offsets['_sweet_log_size'], 8))[0] if '_sweet_log_size' in offsets else RING_DEFAULT
                    if not 0x10000 <= ring <= RING_DEFAULT or ring & (ring - 1):
                        raise ValueError(f'invalid console ring size: {ring:#x}')
                while start < total:
                    index = start % ring
                    length = min(total-start, ring-index)
                    data = read_ram(offsets['_sweet_log_ring']+index, length)
                    log.write(data)
                    tour_tail = (tour_tail+data)[-4096:] if args.tour_shots else b''
                    start += length
                log.flush()
                last_total = total
                for m in re.finditer(rb'FUXI TOUR: shot (\d+) ([a-z-]+)\n', tour_tail):
                    if m.group(1) not in tour_done:
                        tour_done.add(m.group(1))
                        shot_start = time.monotonic()
                        save_framebuffer('framebuffer-shot%s-%s' % (m.group(1).decode(), m.group(2).decode()))
                        print('tour shot', m.group(1).decode(), m.group(2).decode(), 'saved in %.1f s' % (time.monotonic()-shot_start), flush=True)
            if args.display_poll:
                status['display'] = display_poll()
            progress.write(json.dumps(status)+'\n')
            progress.flush()
            brief = {k: status[k] for k in ('stage','kernel_stage','kernel_label','console_total','error') if k in status}
            if brief != previous:
                print(brief, flush=True)
                previous = brief
            if status['stage'] == 99:
                break
            if cpu_at and time.monotonic() - handoff >= cpu_at[0][0]:
                try:  # never let a thermal-lane request end the console log
                    cpu_apply(cpu_at.pop(0)[1], thermal_events)
                except Exception as e:
                    print('cpu level change failed:', e, flush=True)
                (args.out / 'thermal-events.json').write_text(json.dumps(thermal_events, indent=1) + '\n')
            if thermal_file and time.monotonic() >= next_thermal:
                next_thermal = max(next_thermal + args.thermal_log, time.monotonic())
                try:
                    t = thermal_sample()
                except usb.core.USBError as e:
                    t = {'error': str(e), 'temps': {}, 'invalid': [], 'cpu': []}
                t['time'] = time.time()
                t['since_handoff'] = round(time.monotonic() - handoff, 1)
                t['kernel_label'] = status.get('kernel_label')
                thermal_file.write(json.dumps(t) + '\n'); thermal_file.flush()
                print('thermal +%ds' % t['since_handoff'], thermal_brief(t), flush=True)
            if args.reset_minutes and time.monotonic() > renew:
                request(0x54, 8, args.reset_minutes)
                renew = time.monotonic() + 600
            time.sleep(.25)
    (args.out/'display-after.bin').write_bytes(request(0x42,432))
    if args.framebuffer:
        save_framebuffer('framebuffer')
        print('Saved framebuffer memory; physical panel output still needs confirmation',flush=True)


if __name__ == '__main__':
    main()
