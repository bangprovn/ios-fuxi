#!/usr/bin/env python3
"""Host-only parser for the SweetFramebuffer one-shot present event log (fuxigpuncimport bit 8).

Reads a boot log (bytes, any noise), extracts the `SWEET FB: flog N ...` lines and prints where the time between a swap and its
START goes: swap->ready (writer retire), ready->copy submit, submit->copy start on the GPU (the wait behind ring work), the blit,
fence->START, plus a TE-window replay that tells "no new copy ready at the window" (GPU side) from "a copy was ready but no START"
(display side). Optional fields added by later kernels (eager copy, dp= drop path letter, trigger=) are parsed when present, and the
periodic `SWEET FB: unshown paths` lines are summed over the whole log. Long lines that the kernel wraps at 200 characters (continuations
start with '<tag>+', any logger prefix before it) are joined first. Usage: fuxi-flog.py boot.log [...]"""
import re
import statistics
import sys

LINE = re.compile(rb'SWEET FB: flog (\d+) (\d+) (\d+) (-?\d+) (-?\d+) (-?\d+) (-?\d+) (-?\d+) (-?\d+) (-?\d+) (\d+) (\d+) (\d+) (\d+) ([SDP-])((?: [a-z]+=-?\d+)*)(?: dp=([A-Za-z-]))?')
BEGIN = re.compile(rb'SWEET FB: flog begin frames=(\d+) warmup_swaps=(\d+) period_us=(\d+)(?: trigger=(\d+) trigger_drops=(\d+))?')
UNSHOWN = re.compile(rb'SWEET FB: unshown paths ((?:[a-z_]+=\d+[ ;]*)+)of (\d+) not shown; outside it ((?:[a-z_]+=\d+ ?)+); counted shown ((?:[a-z_]+=\d+ ?)+)')
UNSHOWN_KV = re.compile(rb'([a-z_]+)=(\d+)')
# Drop path letters of the frame log (dp=), as printed by SweetFramebuffer::flogPrint.
PATHS = {'s': 'stale', 'r': 'not_ready', 'u': 'superseded', 'f': 'failed_copy', 'o': 'fifo', 'c': 'refused_full', 'p': 'replaced',
         'l': 'lost', 'h': 'stop', 'a': 'spin_started', 'A': 'spin_race'}
END = re.compile(rb'SWEET FB: flog end [^\n]*')
# AppleCLCD::logWrapped: deleting the line break, any logger prefix and '<tag>+' gives back the original line.
WRAP = re.compile(rb'\r?\n[^\n]*?(?:SWEET FB: (?:flog begin|flog end|unshown paths|stages|stats)|FUXI GPU: presents)\+')


def unwrap(data):
    return WRAP.sub(b'', data)


NAMES = ('n', 'id', 'surface', 'swap', 'ready', 'submit', 'fence', 'start', 's2s', 'blit', 'ahead', 'buf', 'te', 'xfer')


def parse(data):
    """Return (period_us, entries, end_line) for the first complete log in data."""
    m = BEGIN.search(data)
    if not m:
        return None, [], None
    period = int(m.group(3))
    rows, seen = [], set()
    for x in LINE.finditer(data, m.start()):
        e = dict(zip(NAMES, (int(v) for v in x.groups()[:14])))
        if e['n'] in seen:
            break
        seen.add(e['n'])
        e['fate'] = x.group(15).decode()
        e['dp'] = x.group(17).decode() if x.group(17) else '-'
        for kv in x.group(16).split():
            k, v = kv.split(b'=')
            e[k.decode()] = int(v)
        rows.append(e)
    end = END.search(data, m.start())
    return period, rows, end.group(0).decode(errors='replace') if end else None


def unshown_totals(data):
    """Sum of every `SWEET FB: unshown paths` line (one per 600 shown frames): (windows, {field: total}, not_shown total)."""
    totals, windows, not_shown = {}, 0, 0
    for m in UNSHOWN.finditer(data):
        windows += 1
        not_shown += int(m.group(2))
        for g in (1, 3, 4):
            for k, v in UNSHOWN_KV.findall(m.group(g)):
                totals[k.decode()] = totals.get(k.decode(), 0) + int(v)
    return windows, totals, not_shown


def absolute(e, key):
    v = e[key]
    return None if v < 0 else e['swap'] + v


def pct(values, p):
    if not values:
        return -1
    s = sorted(values)
    return s[min(len(s) - 1, int(len(s) * p / 100))]


def summary(name, values):
    if not values:
        return '%s n=0' % name
    return '%s n=%d p10=%d p50=%d p90=%d avg=%d' % (name, len(values), pct(values, 10), pct(values, 50), pct(values, 90), statistics.mean(values))


def te_replay(period, rows, tol=400):
    """Per gap between consecutive shown STARTs: a gap longer than one period (+tol us) is classified at the first missed window
    (previous START + period): a newer copy had fenced by then (display side missed it), a newer swap existed but its copy had not
    fenced (GPU side late; lateness recorded), or no newer swap existed (client idle)."""
    shown = [e for e in rows if e['fate'] == 'S' and e['start'] >= 0]
    out = {'gaps': 0, 'one_period': 0, 'display_missed': 0, 'copy_late': 0, 'client_idle': 0, 'late_us': [], 'late_parts': []}
    for a, b in zip(shown, shown[1:]):
        sa, sb = absolute(a, 'start'), absolute(b, 'start')
        out['gaps'] += 1
        if sb - sa <= period + tol:
            out['one_period'] += 1
            continue
        w = sa + period + tol
        newer = [e for e in rows if e['n'] > a['n'] and e['swap'] <= w]
        fenced = [e for e in newer if e['fence'] >= 0 and absolute(e, 'fence') <= w]
        if fenced:
            out['display_missed'] += 1
        elif newer:
            out['copy_late'] += 1
            e = min((e for e in rows if e['n'] > a['n'] and e['fence'] >= 0), key=lambda e: absolute(e, 'fence'), default=None)
            if e:
                out['late_us'].append(absolute(e, 'fence') - w)
                out['late_parts'].append((e['ready'], e['submit'] - e['ready'], max(e['s2s'], 0), e['ahead']))
        else:
            out['client_idle'] += 1
    return out


def report(path, data):
    period, rows, end = parse(data)
    print('== %s' % path)
    windows, totals, not_shown = unshown_totals(data)
    if windows:
        print('unshown paths over %d windows (not shown %d): ' % (windows, not_shown) + ' '.join('%s=%d' % kv for kv in totals.items()))
    m = BEGIN.search(data)
    if m and m.group(4) is not None:
        print('flog trigger=%s trigger_drops=%s warmup_swaps=%s' % (m.group(4).decode(), m.group(5).decode(), m.group(2).decode()))
    if not rows:
        print('no frame log')
        return
    span = rows[-1]['swap'] - rows[0]['swap']
    fates = {f: sum(e['fate'] == f for e in rows) for f in 'SDP-'}
    print('frames=%d period_us=%d span_ms=%.1f client_swap_hz=%.1f shown_hz=%.1f fates S/D/P/-=%d/%d/%d/%d' % (
        len(rows), period, span / 1000, (len(rows) - 1) * 1e6 / span if span else 0, fates['S'] * 1e6 / span if span else 0,
        fates['S'], fates['D'], fates['P'], fates['-']))
    iv = [b['swap'] - a['swap'] for a, b in zip(rows, rows[1:])]
    print(summary('swap_interval_us', iv))
    copied = [e for e in rows if e['submit'] >= 0]
    print(summary('swap_to_ready_us', [e['ready'] for e in copied if e['ready'] >= 0]))
    print(summary('ready_to_submit_us', [e['submit'] - e['ready'] for e in copied if e['ready'] >= 0]))
    behind = [e for e in copied if e['ahead']]
    alone = [e for e in copied if not e['ahead']]
    print(summary('submit_to_copy_start_us(behind client job, %d)' % len(behind), [e['s2s'] for e in behind if e['s2s'] >= 0]))
    print(summary('submit_to_copy_start_us(ring idle, %d)' % len(alone), [e['s2s'] for e in alone if e['s2s'] >= 0]))
    print(summary('blit_us', [e['blit'] for e in copied if e['blit'] >= 0]))
    print(summary('submit_to_fence_us', [e['fence'] - e['submit'] for e in copied if e['fence'] >= 0]))
    sh = [e for e in rows if e['fate'] == 'S' and e['start'] >= 0 and e['fence'] >= 0]
    print(summary('fence_to_start_us(shown)', [e['start'] - e['fence'] for e in sh]))
    print(summary('ready_to_start_us(shown)', [e['start'] - e['ready'] for e in sh if e['ready'] >= 0]))
    print(summary('swap_to_start_us(shown)', [e['start'] for e in sh]))
    # Where swap->START goes for shown frames, as average shares.
    if sh:
        parts = {'gpu_render(swap->ready)': [e['ready'] for e in sh], 'ready->submit': [e['submit'] - e['ready'] for e in sh],
                 'queue_behind_ring(s2s)': [max(e['s2s'], 0) for e in sh], 'blit+flush': [e['fence'] - e['submit'] - max(e['s2s'], 0) for e in sh],
                 'fence->START': [e['start'] - e['fence'] for e in sh]}
        tot = statistics.mean(e['start'] for e in sh)
        print('swap_to_start split (shown, avg us): ' + ' '.join('%s=%d(%.0f%%)' % (k, statistics.mean(v), 100 * statistics.mean(v) / tot) for k, v in parts.items()))
    # Copies finishing in pairs: the earlier of two fences inside half a period is replaced while it waits for START.
    pair = sum(1 for a, b in zip(copied, copied[1:]) if a['fence'] >= 0 and b['fence'] >= 0 and 0 <= absolute(b, 'fence') - absolute(a, 'fence') < period // 2)
    print('copies=%d behind_client_job=%d ring_idle=%d fence_pairs_within_half_period=%d' % (len(copied), len(behind), len(alone), pair))
    # GPU timeline per copy: when the previous client job ended (copy start - nothing), approximate GPU idle before each copy.
    gaps = []
    last = None
    for e in sh:
        if last is not None:
            gaps.append((e['te'] - last) & 0xffff)
        last = e['te']
    hist = [sum(1 for g in gaps if g == 1), sum(1 for g in gaps if g == 2), sum(1 for g in gaps if g >= 3)]
    print('start_te_gaps 1/2/3+=%d/%d/%d' % tuple(hist))
    r = te_replay(period, rows)
    print('shown_gaps=%d one_period=%d longer: display_missed=%d copy_late=%d client_idle=%d' % (
        r['gaps'], r['one_period'], r['display_missed'], r['copy_late'], r['client_idle']))
    if r['late_parts']:
        lp = r['late_parts']
        print(summary('copy_late_fence_after_window_us', r['late_us']) + ' late copies: avg swap_to_ready=%d ready_to_submit=%d queue_behind_ring=%d behind_client_job=%d/%d' % (
            statistics.mean(x[0] for x in lp), statistics.mean(x[1] for x in lp), statistics.mean(x[2] for x in lp), sum(1 for x in lp if x[3]), len(lp)))
    dps = {}
    for e in rows:
        if e['dp'] != '-':
            dps[e['dp']] = dps.get(e['dp'], 0) + 1
    if dps:
        print('drop_paths ' + ' '.join('%s=%d' % (PATHS.get(k, k), v) for k, v in sorted(dps.items())))
    extra = sorted({k for e in rows for k in e if k not in NAMES and k not in ('fate', 'dp')})
    for k in extra:
        print(summary(k, [e[k] for e in rows if k in e and e[k] >= 0]))
    if end:
        print(end)


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    for path in argv[1:]:
        with open(path, 'rb') as f:
            report(path, unwrap(f.read()))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
