"""Shared TS measurement + offline mux-simulation helpers for the pacing tests
and tools/e2e_pace.py (no network, no M1)."""
import collections
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import brptune as bt
import tsinterleave as ti
import tspacer as tp


def pkts_of(data):
    n = len(data) // 188
    return [data[i * 188:(i + 1) * 188] for i in range(n)]


def pid_of(p):
    return ((p[1] & 0x1F) << 8) | p[2]


def strip_http(raw):
    i = raw.find(b"\r\n\r\n")
    if raw[:5] == b"HTTP/" and i > 0:
        raw = raw[i + 4:]
    return raw[:len(raw) // 188 * 188]


def pes_kind(p):
    """'v' / 'a' / None for a PES-start packet (by stream_id)."""
    if not p[1] & 0x40 or not (p[3] >> 4) & 1:
        return None
    off = 5 + p[4] if (p[3] >> 4) & 2 else 4
    if off + 4 > 188 or p[off] or p[off + 1] or p[off + 2] != 1:
        return None
    sid = p[off + 3]
    if 0xE0 <= sid <= 0xEF:
        return "v"
    if sid == 0xBD or 0xC0 <= sid <= 0xDF:
        return "a"
    return None


def av_skew(pkts):
    """Per PES start in byte order: latest video PTS - latest audio PTS (s),
    using the lowest audio PID. Returns list of skews."""
    lastv = lasta = None
    apid = None
    out = []
    for p in pkts:
        k = pes_kind(p)
        if k is None:
            continue
        t = ti.pes_times(p)
        if not t or t[0] is None:
            continue
        if k == "v":
            lastv = t[0]
        else:
            pid = pid_of(p)
            if apid is None or pid < apid:
                apid = pid
            if pid != apid:
                continue
            lasta = t[0]
        if lastv is not None and lasta is not None:
            out.append(ti.sdiff(lastv, lasta) / 90000.0)
    return out


def pcr_list(pkts):
    """[(index, pcr_seconds)] in output order."""
    return [(i, tp.pcr_of(p)) for i, p in enumerate(pkts) if tp.pcr_of(p) is not None]


def pcr_check(pkts):
    """-> (monotonic, max media spacing s, max backwards step s)."""
    pl = pcr_list(pkts)
    mx, back, mono = 0.0, 0.0, True
    for (i, a), (j, b) in zip(pl, pl[1:]):
        d = (b - a) % tp.PCR_WRAP
        if d > tp.PCR_WRAP / 2:
            mono = False
            back = max(back, tp.PCR_WRAP - d)
        else:
            mx = max(mx, d)
    return mono, mx, back


def cc_errors(pkts):
    """Continuity errors per PID (payload packets must increment by 1; a
    repeated duplicate is allowed once)."""
    last = {}
    errs = collections.Counter()
    for p in pkts:
        pid = pid_of(p)
        if pid == 0x1FFF:
            continue
        afc = (p[3] >> 4) & 3
        cc = p[3] & 15
        if not afc & 1:
            continue
        if pid in last and last[pid] is not None:
            lc = last[pid]
            if cc != (lc + 1) & 15 and cc != lc:
                errs[pid] += 1
        last[pid] = cc
    return errs


def bursts_by_media(pkts, vpid, period=2.0):
    """Split a clip into M1-like push bursts of `period` s of media time
    (by PCR on vpid). Returns list of lists of packets."""
    out, cur, t0 = [], [], None
    for p in pkts:
        c = tp.pcr_of(p) if pid_of(p) == vpid else None
        if c is not None:
            if t0 is None:
                t0 = c
            if cur and (c - t0) % tp.PCR_WRAP >= period:
                out.append(cur)
                cur, t0 = [], c
        cur.append(p)
    if cur:
        out.append(cur)
    return out


class FakeClock:
    """Callable clock that is also a drop-in for the `time` module bits that
    brptune uses (time(), sleep()): sleep() just advances the clock."""

    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def time(self):
        return self.t

    def monotonic(self):
        return self.t

    def sleep(self, d):
        self.t += d


def run_mux(bursts, es, pcr_pid, interleave, period=2.0, quiet_gap=0.1,
            gate_pass=False):
    """Run the real Session.packets() over `bursts` (each a list of source TS
    packets, PAT/PMT already stripped) with the interleave flag set, driven by a
    fake clock. Returns (output packets, [(burst index, out index at burst
    start)]). Burst i is pushed at fake time i*period."""
    saved = getattr(bt, "INTERLEAVE", None)
    saved_time = bt.time
    bt.INTERLEAVE = interleave
    try:
        clk = FakeClock()
        bt.time = clk
        s = bt.Session("sim")
        s.clock = clk
        s.es = dict(es)
        s.pcr_pid, s.pmt_pid = pcr_pid, 0x1000
        s.gate = bt.GopGate(clock=clk)
        if gate_pass:
            s.gate.state = bt.GopGate.PASS
        out, marks = [], []
        nxt = [0]

        def push():
            if nxt[0] >= len(bursts):
                return False
            clk.t = max(clk.t, 1000.0 + nxt[0] * period)
            for p in bursts[nxt[0]]:
                s.q.setdefault(pid_of(p), collections.deque()).append(p)
            marks.append((nxt[0], len(out)))
            nxt[0] += 1
            s.last_push = clk.t
            return True
        push()
        for p in s.packets():
            out.append(p)
            if not any(s.q.values()):
                if not push():
                    s.last_push = 0
        return out, marks
    finally:
        if saved is not None:
            bt.INTERLEAVE = saved
        bt.time = saved_time


# ---- deterministic synthetic M1-like push (MPEG-2 1080i + 2x AC-3) -----------
SYN_V, SYN_A = (0x81, (0x84, 0x85))


def _pts_bytes(marker, t):
    return bytes([(marker << 4) | (((t >> 30) & 7) << 1) | 1,
                  (t >> 22) & 0xFF, (((t >> 15) & 0x7F) << 1) | 1,
                  (t >> 7) & 0xFF, ((t & 0x7F) << 1) | 1])


def _pes(sid, pts, dts, payload, bounded):
    if dts is not None and dts != pts:
        hdr = b"\x80\xC0\x0A" + _pts_bytes(3, pts) + _pts_bytes(1, dts)
    else:
        hdr = b"\x80\x80\x05" + _pts_bytes(2, pts)
    n = len(hdr) + len(payload)
    return (b"\x00\x00\x01" + bytes([sid]) + (n.to_bytes(2, "big") if bounded and n < 65536
            else b"\x00\x00") + hdr + payload)


def _packetize(pid, pes, cc, pcr=None, rai=False):
    """PES -> TS packets (stuffing in the last one). cc: dict pid->next cc."""
    out, first, i = [], True, 0
    while i < len(pes):
        room = 184
        af = b""
        if first and (pcr is not None or rai):
            fl = (0x10 if pcr is not None else 0) | (0x40 if rai else 0)
            af = bytes([fl])
            if pcr is not None:
                base = int(pcr * 90000) % (1 << 33)
                af += bytes([base >> 25 & 0xFF, base >> 17 & 0xFF, base >> 9 & 0xFF,
                             base >> 1 & 0xFF, (base & 1) << 7 | 0x7E, 0])
            af = bytes([len(af)]) + af
            room -= len(af)
        chunk = pes[i:i + room]
        if len(chunk) < room:
            pad = room - len(chunk)
            if af:
                af = bytes([af[0] + pad]) + af[1:] + b"\xFF" * pad
            elif pad == 1:
                af = b"\x00"
            else:
                af = bytes([pad - 1, 0]) + b"\xFF" * (pad - 2)
            room = len(chunk)
        i += room
        c = cc.get(pid, 0)
        cc[pid] = (c + 1) & 15
        hdr = bytes([0x47, (0x40 if first else 0) | (pid >> 8), pid & 0xFF,
                     (0x30 if af else 0x10) | c])
        out.append(hdr + af + chunk)
        first = False
    return out


def synth(seconds=30.0, vpid=0x81, apids=(0x84, 0x85), seed=1, pcr_ms=35.0):
    """A CBR-muxed synthetic M1 program: 1080i29.97 MPEG-2 (IBBPBBP... decode
    order, PTS!=DTS on I/P, DTS==PTS on B), two AC-3 PES streams (192 ms), a
    PCR-only packet on the video PID every `pcr_ms`, values = byte position at
    the content rate. Video leads audio by 0.3 s in the mux (as in broadcast).
    Returns {pid: [(mux_time_s, pkt), ...]} (per-PID order = mux order)."""
    import random
    rnd = random.Random(seed)
    ft = 1001 / 30000.0
    cc = {}
    units = []                                  # (sendkey, pid, [pkts])
    base = 90000 * 5
    gop = ["I", "B", "B", "P", "B", "B", "P", "B", "B", "P", "B", "B", "P", "B", "B"]
    for i in range(int(seconds / ft)):
        typ = gop[i % 15]
        dts = base + int(i * ft * 90000)
        pts = dts + (int(2 * ft * 90000) if typ in "IP" else 0)
        size = int({"I": 70000, "P": 25000, "B": 9000}[typ] * (0.8 + 0.4 * rnd.random()))
        es = bytes(max(b, 2) for b in (bytes(rnd.getrandbits(8) for _ in range(64))
                                        * (size // 64)))
        if typ == "I":
            es = b"\x00\x00\x01\xB3\x78\x04\x38\x35\xFF\xFF\xE0\x18" + es
        es = b"\x00\x00\x01\x00\x00\x0F\xFF\xF8" + es
        units.append((dts / 90000.0 - 0.3, vpid, _packetize(
            vpid, _pes(0xE0, pts, dts, es, False), cc, rai=(typ == "I"))))
    for a in apids:
        for j in range(int(seconds / 0.192)):
            pts = base + int(j * 0.192 * 90000)
            es = bytes(max(b, 2) for b in bytes(rnd.getrandbits(8) for _ in range(64)) * 72)
            units.append((pts / 90000.0, a, _packetize(a, _pes(0xBD, pts, None, es, True), cc)))
    units.sort(key=lambda u: (u[0], u[1]))
    flat = [(pid, p) for _, pid, pk in units for p in pk]
    rate = len(flat) * 188 * 8 / seconds            # content rate, bits/s
    every = max(1, int(pcr_ms / 1000.0 * rate / (188 * 8)))
    out = {vpid: [], **{a: [] for a in apids}}
    vcc, n, k = 0, 0, 0
    for pid, p in flat:
        if k % every == 0:
            t = n * 188 * 8 / rate
            b = int((5 + t) * 90000) % (1 << 33)
            af = bytes([183, 0x10, b >> 25 & 0xFF, b >> 17 & 0xFF, b >> 9 & 0xFF,
                        b >> 1 & 0xFF, (b & 1) << 7 | 0x7E, 0]) + b"\xFF" * 176
            out[vpid].append((t, bytes([0x47, vpid >> 8, vpid & 0xFF, 0x20 | vcc]) + af))
            n += 1
        k += 1
        if pid == vpid:
            vcc = p[3] & 15
        out[pid].append((n * 188 * 8 / rate, p))
        n += 1
    return out


def synth_bursts(streams, period=2.0):
    """Group per-PID synthetic packets into M1-like bursts of `period` s."""
    t0 = min(p[0][0] for p in streams.values())
    tmax = max(p[-1][0] for p in streams.values())
    out = []
    for b in range(int((tmax - t0) / period) + 1):
        lo, hi = t0 + b * period, t0 + (b + 1) * period
        blk = []
        for pid, lst in streams.items():
            blk += [p for t, p in lst if lo <= t < hi]
        if blk:
            out.append(blk)
    return out
