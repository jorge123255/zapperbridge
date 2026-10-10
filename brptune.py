#!/usr/bin/env python3
"""ZapperBox M1 BRP push tuner: api9 tune -> :31061 capture -> one remuxed MPEG-TS.

The M1 demuxes the tuned program and pushes it back to the source IP of the BRP
connection as separate fire-and-forget TCP streams (video, one per audio track,
plus a JSON control stream). Each stream carries its own PAT and a PMT that
declares only its own elementary stream, so the streams are merged here into a
single program with one rebuilt PAT/PMT.
"""
import json, os, socket, sys, threading, time, zlib
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from brp_probe import pack, unpack_header
import tsinterleave
import zdiag
import zstream

#: Monotonic clock for diagnostics (patched in tests).
_mono = time.monotonic

M1 = os.environ.get("ZM1", "")
BRP_PORT = int(os.environ.get("ZBRP", 31065))
PUSH_PORT = int(os.environ.get("ZPUSH", 31061))
DEV = os.environ.get("ZDEV", "02bb00000001")
#: Registered BRP device ids; the M1 leases one tuner instance per device id.
#: The M1's pool is three leases (instances 4, 3, 5); a fourth join answers
#: NO_TUNER_AVAILABLE no matter which id, source IP or payload is used.
DEVS = [d.strip() for d in
        os.environ.get("ZDEVS",
                       "02bb00000001,02bb00000002,02bb00000003").split(",")
        if d.strip()]


#: dev -> push-destination-IP mapping. Each registered BRP dev makes the M1
#: push that dev's streams to a *distinct* local host IP (an en0 alias), so the
#: local destination IP of a :31061 connection identifies which tuner/channel a
#: push belongs to -- even when two channels share a PMT PID. The mapping is
#: normally learned from the first push bursts; it can also be seeded from the
#: ZPUSH_IPS env var and/or a small state file so it survives restarts and the
#: alias-missing health warning fires right after a reboot.
#: e.g. ZPUSH_IPS="02bb00000001=192.0.2.30,02bb00000002=192.0.2.242"
IP_UNLEARN_CONFLICTS = int(os.environ.get("ZIP_UNLEARN_N", "3"))

PUSH_STATE_FILE = os.environ.get("ZPUSH_STATE") or os.path.join(
    os.path.dirname(os.environ["HDHR_LOG"]) if os.environ.get("HDHR_LOG")
    else os.path.dirname(os.path.abspath(__file__)),
    "push_ips_state.json")


def _valid_ip(ip):
    try:
        socket.inet_aton(ip)
        return True
    except (OSError, TypeError):
        return False


def _parse_push_ips(raw):
    out = {}
    for item in (raw or "").split(","):
        item = item.strip()
        if "=" not in item:
            continue
        dev, ip = item.split("=", 1)
        dev, ip = dev.strip(), ip.strip()
        if dev and _valid_ip(ip):
            out[dev] = ip
        elif dev:
            print("push-ip: ignoring invalid ZPUSH_IPS seed %r" % item,
                  flush=True)
    return out


def _load_push_state():
    try:
        with open(PUSH_STATE_FILE) as f:
            d = json.load(f)
        return {k: v for k, v in d.items()
                if isinstance(v, str) and _valid_ip(v)}
    except Exception:
        return {}


#: devs whose IP came from ZPUSH_IPS are "seeded": trusted, never auto-unlearned.
SEEDED_DEVS = set(_parse_push_ips(os.environ.get("ZPUSH_IPS", "")))

#: merged known mapping at import: persisted state first, env seeds win.
PUSH_IPS = {}
PUSH_IPS.update(_load_push_state())
PUSH_IPS.update(_parse_push_ips(os.environ.get("ZPUSH_IPS", "")))
for _d in sorted(PUSH_IPS):
    print("push-ip: seed %s -> %s%s" % (
        _d, PUSH_IPS[_d],
        " (ZPUSH_IPS)" if _d in SEEDED_DEVS else " (state)"), flush=True)


def _ip_local(ip):
    """True if ip is an address bindable on this host (i.e. the alias exists)."""
    if not ip:
        return False
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((ip, 0))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _chan_major(chan_id):
    """Major channel number encoded in a BRP chan_id, or None (e.g. HLS ids).

    chan_id low bits are (major << 12) | minor, so major = (v >> 12) & 0xFFF.
    """
    try:
        v = chan_id if isinstance(chan_id, int) else int(str(chan_id), 16)
    except (TypeError, ValueError):
        return None
    return (v >> 12) & 0xFFF


HDR = 72
K_VIDEO = b"\x01\x01\x01\x01"
K_AUDIO = b"\x01\x01\x01\x02"
K_CTRL = b"\x01\x01\x02\x00"
OUT_PMT = 0x1000
OUT_PAT = 0
OUT_PROG = 1
STALE_SEC = float(os.environ.get("ZSTALE", "6"))
#: Per-PID packet queue. The M1 pushes ~2 s of video in one burst (8-12k TS
#: packets observed on 1080i; max 11,899) and the whole burst is queued at once,
#: so the queue must hold at least one full burst or its head is silently lost.
#: 24000 x ~230 B ~= 5.5 MB worst case per video PID.
QMAX = int(os.environ.get("ZQMAX", "24000"))
#: Tunes are serialised: one tune at a time owns the M1 until its push is seen.
TUNE_QUEUE_WAIT = float(os.environ.get("ZTUNE_QWAIT", "30"))
CLAIM_WAIT = float(os.environ.get("ZCLAIM_WAIT", "8"))
#: A stopped lease can keep pushing its old channel for a few seconds; those
#: bursts must not be claimed by the next tune.
QUARANTINE = float(os.environ.get("ZQUAR", "8"))
CHUNK = int(os.environ.get("ZCHUNK", "256"))
PSI_SEC = float(os.environ.get("ZPSI", "0.1"))
PSI_PKTS = int(os.environ.get("ZPSI_PKTS", "64"))
PUSH_WAIT = float(os.environ.get("ZPUSH_WAIT", "6"))
#: The M1 pushes start mid-GOP. For MPEG-2 video the output is held until a
#: PES with a sequence header + I-picture arrives (see GopGate). If that does
#: not happen within this wall time / byte budget the held data is flushed
#: unchanged so a tune never hangs. ZGOP_GATE=0 disables the gate entirely.
GOP_GATE = os.environ.get("ZGOP_GATE", "1") != "0"
GOP_WAIT = float(os.environ.get("ZGOP_WAIT", "6"))
GOP_MAX_BYTES = int(os.environ.get("ZGOP_MAX_BYTES", str(8 * 1024 * 1024)))
#: Interleave the per-PID queues by media time (DTS/PTS) instead of round-robin
#: by packet, bounding the A/V skew in the byte stream. ZINTERLEAVE=0 restores
#: the previous round-robin drain exactly. ZIL_WAIT_MS bounds how long a PID with
#: no data yet may hold the others back (the M1 sends ~2 s bursts whose audio
#: ends a PES earlier than its video, so the video tail waits for the next burst).
INTERLEAVE = os.environ.get("ZINTERLEAVE", "1") != "0"
IL_WAIT = float(os.environ.get("ZIL_WAIT_MS", "2500")) / 1000.0
IL_AUDIO_LEAD = float(os.environ.get("ZIL_AUDIO_LEAD_MS", "130")) / 1000.0

_CRC_T = []
for _n in range(256):
    _c = _n << 24
    for _ in range(8):
        _c = ((_c << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if _c & 0x80000000 else (_c << 1) & 0xFFFFFFFF
    _CRC_T.append(_c)


def crc32_mpeg(data):
    crc = 0xFFFFFFFF
    for b in data:
        crc = ((crc << 8) ^ _CRC_T[((crc >> 24) ^ b) & 0xFF]) & 0xFFFFFFFF
    return crc


def _section(tid, body):
    out = bytearray([tid, 0xB0 | ((len(body) + 4 >> 8) & 0x0F), (len(body) + 4) & 0xFF])
    out += body
    out += crc32_mpeg(out).to_bytes(4, "big")
    return bytes(out)


#: stream_types that carry audio PES (ATSC AC-3 0x81 plus the usual suspects).
AUDIO_TYPES = frozenset((0x03, 0x04, 0x0F, 0x11, 0x81, 0x82, 0x83, 0x84,
                         0x85, 0x86, 0x87, 0x8A))
#: ATSC A/52 registration descriptor for AC-3 audio carried as stream_type 0x81.
REG_AC3 = bytes([0x05, 0x04]) + b"AC-3"


def mk_pat(pmt_pid, ver=0):
    """Spec-correct PAT: transport_stream_id, version, section_number (0) and
    last_section_number (0), then one program -> PMT PID."""
    body = bytearray(b"\x00\x01")
    body.append(0xC1 | ((ver & 0x1F) << 1))
    body += b"\x00\x00"
    body += bytes([OUT_PROG >> 8, OUT_PROG & 0xFF,
                   0xE0 | (pmt_pid >> 8), pmt_pid & 0xFF])
    return _section(0x00, body)


def mk_pmt(pcr_pid, es, ver=0):
    """Spec-correct PMT (section_number/last_section_number present, reserved
    bits set, version in the right bits). AC-3 (0x81) gets the ATSC 'AC-3'
    registration descriptor."""
    body = bytearray()
    body += bytes([OUT_PROG >> 8, OUT_PROG & 0xFF])
    body.append(0xC1 | ((ver & 0x1F) << 1))
    body += b"\x00\x00"
    body += bytes([0xE0 | (pcr_pid >> 8), pcr_pid & 0xFF, 0xF0, 0x00])
    for pid, st in sorted(es.items()):
        desc = REG_AC3 if st == 0x81 else b""
        body += bytes([st, 0xE0 | (pid >> 8), pid & 0xFF,
                       0xF0 | (len(desc) >> 8), len(desc) & 0xFF]) + desc
    return _section(0x02, body)


def psi_packet(pid, section, cc):
    p = bytearray(188)
    p[0] = 0x47
    p[1] = 0x40 | ((pid >> 8) & 0x1F)
    p[2] = pid & 0xFF
    p[3] = 0x10 | (cc & 0x0F)
    body = b"\x00" + section
    p[4:4 + len(body)] = body
    p[4 + len(body):] = b"\xff" * (188 - 4 - len(body))
    return bytes(p)


def _recv_all(sk, n):
    buf = b""
    while len(buf) < n:
        try:
            d = sk.recv(min(65536, n - len(buf)))
        except Exception:
            return None
        if not d:
            return None
        buf += d
    return buf


def _frame_pids(data):
    """PIDs carried by one push frame (PAT pid 0 is shared by every stream)."""
    out = set()
    for i in range(0, len(data) - 187, 188):
        p = data[i:i + 188]
        if p[0] != 0x47:
            break
        pid = ((p[1] & 0x1F) << 8) | p[2]
        if pid:
            out.add(pid)
    return out


def _frame_len(hdr):
    """Body length of one push frame.

    Media legs (video/audio) carry it in bytes 68-71 with 64-67 zero; control
    legs carry it in bytes 64-67 with 68-71 zero (push3.pcap: every control
    leg is n@64=662/667, n@68=0). Reading only 68-71 dropped every control leg.
    """
    return (int.from_bytes(hdr[68:72], "big")
            or int.from_bytes(hdr[64:68], "big"))


def _frame_pat(data):
    """(program_number, PMT PID) from the source PAT in one push frame, or
    (None, None). program_number is the MPEG program number (9.1 -> 3)."""
    for i in range(0, len(data) - 187, 188):
        p = data[i:i + 188]
        if p[0] != 0x47:
            break
        if ((p[1] & 0x1F) << 8 | p[2]) != 0 or not p[1] & 0x40:
            continue
        off = 4 + (1 + p[4] if p[3] & 0x20 else 0)
        b = p[off + 1 + p[off]:]
        if len(b) < 12 or b[0] != 0x00:
            continue
        ln = ((b[1] & 0x0F) << 8) | b[2]
        e = b[8:3 + ln - 4]
        while len(e) >= 4:
            pn = (e[0] << 8) | e[1]
            if pn:
                return pn, ((e[2] & 0x1F) << 8) | e[3]
            e = e[4:]
    return None, None


def _frame_family(data):
    """PMT PID announced by the PAT inside one push frame, or None.

    Every push stream (video, each audio track) carries PAT + the PMT PID of
    the program it belongs to, so the PMT PID identifies the *channel* a push
    belongs to even though each stream carries only one elementary PID.
    """
    return _frame_pat(data)[1]


def _frame_program(hdr, data):
    """program_nbr from a control (K_CTRL) push frame's JSON, else None.

    track_meta.program_nbr is the MPEG program_number of the tuned program
    (the same value as the PAT entry in its media legs), NOT the channel's
    major number. Measured: 2.1 -> 2, 5.1 -> 5, 7.1 -> 1, 9.1 -> 3 and
    32.1 -> 3 (9.1 and 32.1 also share PMT 48, so a same-slot retune between
    them still waits for the quarantine to expire). The
    control leg arrives once, at the start of a tune, before the media legs.
    """
    if not hdr or hdr[4:8] != K_CTRL:
        return None
    i, j = data.find(b"{"), data.rfind(b"}")
    if i < 0 or j <= i:
        return None
    try:
        o = json.loads(data[i:j + 1].decode("utf-8", "replace"))
    except Exception:
        return None
    tm = o.get("track_meta") or {}
    prog = tm.get("program_nbr")
    return prog if isinstance(prog, int) else None


def _pes_pts(pl):
    """PTS (90 kHz) of a PES whose first byte is pl[0], or None."""
    if len(pl) < 14 or pl[0] != 0 or pl[1] != 0 or pl[2] != 1 or not pl[7] & 0x80:
        return None
    t = pl[9:14]
    return (((t[0] >> 1) & 7) << 30 | t[1] << 22 | (t[2] >> 1) << 15
            | t[3] << 7 | t[4] >> 1)


def _payload(pkt):
    afc = (pkt[3] >> 4) & 3
    if not afc & 1:
        return b""
    off = 4
    if afc & 2:
        off = 5 + pkt[4]
    return pkt[off:] if off < 188 else b""


def mpeg2_start_ok(pes):
    """True if this video PES payload (starting at the PES header) begins at a
    random-access point: sequence header (00 00 01 B3) near the start followed
    by an I-picture (picture_coding_type 1 in the 00 00 01 00 header)."""
    if len(pes) < 9 or pes[:3] != b"\x00\x00\x01":
        return False
    pay = bytes(pes[9 + pes[8]:])
    s = pay.find(b"\x00\x00\x01\xb3", 0, 20)
    if s < 0:
        return False
    p = pay.find(b"\x00\x00\x01\x00", s + 4)
    return p >= 0 and p + 6 <= len(pay) and ((pay[p + 5] >> 3) & 7) == 1


def _pes_es(pes):
    """Elementary-stream bytes of a PES (payload starting at the PES header)."""
    return bytes(pes[9 + pes[8]:]) if len(pes) >= 9 else b""


def mpeg2_closed_gop(pes):
    """True if the GOP header in this PES says closed_gop=1. False when it says
    open, or when there is no GOP header (then leading B-pictures are assumed to
    reference a missing earlier anchor)."""
    es = _pes_es(pes)
    s = es.find(b"\x00\x00\x01\xb3", 0, 20)
    g = es.find(b"\x00\x00\x01\xb8", s + 4) if s >= 0 else -1
    p = es.find(b"\x00\x00\x01\x00", s + 4) if s >= 0 else -1
    if g < 0 or (p >= 0 and g > p) or g + 8 > len(es):
        return False
    return bool(es[g + 7] & 0x40)


def _leading_b_pes(pes):
    """None if this PES does not start with a picture header; else its
    picture_coding_type."""
    es = _pes_es(pes)
    if len(es) < 6 or es[:4] != b"\x00\x00\x01\x00":
        return None
    return (es[5] >> 3) & 7


def _pcr_of(pkt):
    if pkt[3] & 0x20 and pkt[4] >= 7 and pkt[5] & 0x10:
        return pkt[6:12]
    return None


class GopGate:
    """Start MPEG-2 output at a random-access point.

    States: WAIT (holding packets), OPEN (gated start found), PASS (not MPEG-2,
    or budget exhausted: everything passes unchanged). feed() returns the list
    of packets to emit now; ``opened`` is set once when output begins.

    On OPEN the video PES holding the sequence header + I-picture is emitted
    first, earlier video is dropped, and audio PES with a PTS earlier than that
    I-picture are dropped (audio is cut on PES boundaries). Only prefixes are
    dropped, so continuity counters stay valid from the first emitted packet.
    """
    WAIT, OPEN, PASS = "wait", "open", "pass"
    EVAL_BYTES = 1500

    def __init__(self, budget_sec=None, budget_bytes=None, clock=time.time):
        self.budget_sec = GOP_WAIT if budget_sec is None else budget_sec
        self.budget_bytes = GOP_MAX_BYTES if budget_bytes is None else budget_bytes
        self.clock = clock
        self.state = self.WAIT if GOP_GATE else self.PASS
        self.opened = False
        self.reason = "disabled" if not GOP_GATE else None
        self.t0 = None
        self.held = []
        self.vpid = None
        self.cand = None            # [held index, payload bytearray, evaluated]
        self.pts_v = None
        self.audio_pids = frozenset()
        self.started = {}
        self.dropped_video = 0
        self.dropped_audio = 0
        self.dropped_b = 0           # leading B-pictures removed (open GOP)
        self.dropped_b_pkts = 0      # their payload-bearing TS packets
        self.wait_sec = 0.0
        # open-GOP state (OPEN only): lead = None | "first" (inside the I PES)
        # | "scan" (looking at the PES after the I for leading B-pictures)
        self.lead = None
        self.pend = None             # packets of a candidate leading-B PES
        self.pend_t = 0.0
        self.pend_pay = bytearray()
        self.cc_shift = 0            # payload packets removed from video PID
        self.v_cc = None             # cc of the last video packet emitted

    def _pass(self, reason):
        self.state = self.PASS
        self.reason = reason
        self.opened = True
        self.wait_sec = self.clock() - self.t0 if self.t0 else 0.0
        out, self.held, self.cand = self.held, [], None
        return out

    def tick(self):
        """Budget check without a packet (idle queues). Returns packets."""
        if self.state == self.WAIT and self.t0 is not None \
                and self.clock() - self.t0 > self.budget_sec:
            return self._pass("timeout")
        if self.state == self.OPEN and self.pend \
                and self.clock() - self.pend_t > 0.25:
            return self._settle(False)       # stalled mid-PES: emit unchanged
        return []

    def flush(self):
        """End of stream while still waiting: release what was held."""
        if self.state == self.WAIT:
            return self._pass("eos")
        if self.state == self.OPEN and self.pend:
            return self._settle(False)
        return []

    def _audio(self, pkt):
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        if pkt[1] & 0x40:
            pts = _pes_pts(_payload(pkt))
            ok = (self.pts_v is None or pts is None
                  or ((pts - self.pts_v) & ((1 << 33) - 1)) < (1 << 32))
            self.started[pid] = ok
        if self.started.get(pid):
            return True
        self.dropped_audio += 1
        return False

    def _open(self, idx, pes):
        self.pts_v = _pes_pts(pes)
        self.state = self.OPEN
        self.reason = "gop"
        self.opened = True
        self.wait_sec = self.clock() - self.t0
        held, self.held, self.cand = self.held, [], None
        out = [self._emit_v(held[idx])]
        self.lead = None if mpeg2_closed_gop(pes) else "first"
        for i, p in enumerate(held):
            if i == idx:
                continue
            pid = ((p[1] & 0x1F) << 8) | p[2]
            if pid == self.vpid:
                if i < idx:
                    self.dropped_video += 1
                    continue
                out.extend(self._vid(p))
            elif pid in self.audio_pids:
                if self._audio(p):
                    out.append(p)
            else:
                out.append(p)
        return out


    # ---- open GOP: drop the leading B-pictures that follow the first I ------
    def _emit_v(self, pkt):
        """Emit one video-PID packet with the continuity counter renumbered
        for the payload packets removed so far."""
        if self.cc_shift and pkt[3] & 0x10:
            b = bytearray(pkt)
            b[3] = (b[3] & 0xF0) | ((pkt[3] - self.cc_shift) & 0x0F)
            pkt = bytes(b)
        if pkt[3] & 0x10:
            self.v_cc = pkt[3] & 0x0F
        return pkt

    def _drop_v(self, pkt):
        """Remove a video packet; its PCR (if any) survives in an
        adaptation-only packet that reuses the last emitted cc."""
        if pkt[3] & 0x10:
            self.cc_shift = (self.cc_shift + 1) & 0x0F
            self.dropped_b_pkts += 1
        pcr = _pcr_of(pkt)
        if pcr is None or self.v_cc is None:
            return []
        b = bytearray(b"\xff" * 188)
        b[0] = 0x47
        b[1] = pkt[1] & 0xBF                   # no PUSI
        b[2] = pkt[2]
        b[3] = 0x20 | self.v_cc                # adaptation only: cc not advanced
        b[4] = 183
        b[5] = 0x10                            # PCR flag only
        b[6:12] = pcr
        return [bytes(b)]

    def _settle(self, drop):
        pend, self.pend, self.pend_pay = self.pend, None, bytearray()
        if drop:
            self.dropped_b += 1
            out = []
            for p in pend:
                out += self._drop_v(p)
            return out
        self.lead = None
        return [self._emit_v(p) for p in pend]

    def _pend_decide(self):
        """Pending PES complete: drop it only if it holds exactly one B-picture
        (no second picture / sequence / GOP start code inside)."""
        es = _pes_es(self.pend_pay)
        extra = any(es.find(sc, 4) >= 0 for sc in
                    (b"\x00\x00\x01\x00", b"\x00\x00\x01\xb3", b"\x00\x00\x01\xb8"))
        return self._settle(not extra)

    def _vid(self, pkt):
        if self.lead is None and not self.pend:
            return [self._emit_v(pkt)]
        pusi = pkt[1] & 0x40
        out = []
        if self.pend:
            if not pusi:
                self.pend.append(pkt)
                self.pend_pay += _payload(pkt)
                n = (self.pend_pay[4] << 8) | self.pend_pay[5] \
                    if len(self.pend_pay) >= 6 else 0
                if n and len(self.pend_pay) >= n + 6:
                    return self._pend_decide()      # length-delimited PES done
                return []
            out = self._pend_decide()               # next PES starts: settle
        if self.lead == "first":
            if not pusi:
                return out + [self._emit_v(pkt)]
            self.lead = "scan"
        if self.lead == "scan" and pusi:
            t = _leading_b_pes(_payload(pkt))
            if t == 3:
                self.pend = [pkt]
                self.pend_pay = bytearray(_payload(pkt))
                self.pend_t = self.clock()
                return out
            self.lead = None                        # first P / I: lead-in over
        return out + [self._emit_v(pkt)]

    def feed(self, pkt, es):
        if self.state == self.PASS:
            return [pkt]
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        if self.state == self.OPEN:
            if pid == self.vpid:
                return self._vid(pkt)
            if pid in self.audio_pids and not self._audio(pkt):
                return []
            return [pkt]
        if self.t0 is None:
            self.t0 = self.clock()
        if es:
            vids = sorted(p for p, st in es.items() if st == 0x02)
            if not vids:
                self.held.append(pkt)
                return self._pass("not-mpeg2")
            if self.vpid is not None and self.vpid not in vids:
                self.held.append(pkt)
                return self._pass("type-changed")
            self.vpid = vids[0]
            self.audio_pids = frozenset(p for p, st in es.items() if st in AUDIO_TYPES)
        self.held.append(pkt)
        if self.clock() - self.t0 > self.budget_sec \
                or len(self.held) * 188 > self.budget_bytes:
            return self._pass("budget")
        if self.vpid is None or pid != self.vpid:
            return []
        pl = _payload(pkt)
        idx = len(self.held) - 1
        if pkt[1] & 0x40:
            # next video PES: settle the previous candidate first
            c = self.cand
            if c is not None and not c[2] and mpeg2_start_ok(c[1]):
                # current packet is last in held: reopen handles it as "rest"
                return self._open(c[0], c[1])
            self.cand = [idx, bytearray(pl), False]
        elif self.cand is not None and not self.cand[2]:
            self.cand[1] += pl
        c = self.cand
        if c is not None and not c[2] and len(c[1]) >= self.EVAL_BYTES:
            if mpeg2_start_ok(c[1]):
                return self._open(c[0], c[1])
            c[2] = True      # evaluated, not a random-access point
        return []


class Session:
    """One tuned program: collects pushes and exposes merged TS packets."""

    def __init__(self, chan_id):
        self.chan_id = chan_id
        self.alive = True
        self.lock = threading.Lock()
        self.q = {}
        self.es = {}
        self.pmt_pid = None
        self.pcr_pid = None
        self.meta = {}
        self.tracks = []
        self.last_push = time.time()
        self.npush = 0
        self.nbytes = 0
        self.kinds = {}
        self.src_pat_pid = None
        self.psi_pids = set()
        self.tune_seq = 0
        self.family = None
        #: program_number announced by this tune's control leg (program_nbr).
        self.expect_prog = None
        #: PAT program_number of the media legs that claimed this session.
        self.pat_prog = None
        self.qdrops = 0
        self.qdrop_bursts = 0
        self.qhigh = 0
        self.queue_wait = 0.0
        self.claim_wait = 0.0
        self.gate = GopGate()
        self.clock = time.time
        #: diagnostics only (ZLOG_DIAG): M1 push-side stats; None when off
        self.zin = zstream.new_input(chan_id, self)

    def _learn(self, pid, data, off):
        """Parse PAT/PMT only. PES payloads start 00 00 01 xx and must not be
        mistaken for a PAT, so require section_syntax_indicator + valid ids."""
        if pid in self.es:
            return
        ptr = data[off]
        b = data[off + 1 + ptr:]
        if len(b) < 6:
            return
        tid = b[0]
        ln = ((b[1] & 0x0F) << 8) | b[2]
        sec = b[3:3 + ln]
        if not (ln >= 9 and len(sec) == ln and b[1] & 0x80 and sec[2] & 0xC0 == 0xC0):
            return
        if tid == 0x00 and len(sec) >= 9 and (len(sec) - 9) % 4 == 0:
            e = sec[5:len(sec) - 4]
            while len(e) >= 4:
                prog = (e[0] << 8) | e[1]
                p = ((e[2] & 0x1F) << 8) | e[3]
                if prog and p not in (OUT_PAT, OUT_PMT):
                    self.pmt_pid = p
                    self.src_pat_pid = pid
                    self.psi_pids.add(pid)
                e = e[4:]
        elif tid == 0x02 and len(sec) >= 9 and ((sec[0] << 8) | sec[1]) != 0:
            if pid not in (OUT_PAT, OUT_PMT):
                self.pmt_pid = pid
                self.psi_pids.add(pid)
            for base in (9, 6):
                pcr = ((sec[base - 4] & 0x1F) << 8) | sec[base - 3]
                plen = ((sec[base - 2] & 0x0F) << 8) | sec[base - 1]
                es = self._es_list(sec[base + plen:])
                if es is not None:
                    self.pcr_pid = pcr
                    for ep, st in es:
                        if ep not in (OUT_PAT, OUT_PMT):
                            self.es[ep] = st
                    return

    @staticmethod
    def _es_list(e):
        """ES loop must consume the section exactly (4 trailing CRC bytes)."""
        out = []
        while len(e) > 4:
            if len(e) < 5:
                return None
            ep = ((e[1] & 0x1F) << 8) | e[2]
            el = ((e[3] & 0x0F) << 8) | e[4]
            if ep == 0x1FFF or ep > 0x1FFF or 5 + el >= len(e):
                return None
            out.append((ep, e[0]))
            e = e[5 + el:]
        return out if out else None

    def push(self, kind, hdr, data):
        if not self.alive:
            return
        self.npush += 1
        self.nbytes += len(data)
        self.last_push = time.time()
        self.kinds[kind] = self.kinds.get(kind, 0) + 1
        zin = getattr(self, "zin", None)
        if zin is not None:
            zin.on_frame(kind, hdr, len(data))
        if kind == K_CTRL:
            i, j = data.find(b"{"), data.rfind(b"}")
            if i < 0 or j <= i:
                if zin is not None:
                    zin.on_ctrl_err("no_json")
            if i >= 0 and j > i:
                try:
                    o = json.loads(data[i:j + 1].decode("utf-8", "replace"))
                except Exception:
                    if zin is not None:
                        zin.on_ctrl_err("json")
                    return
                self.meta = o.get("track_meta") or {}
                self.tracks = o.get("tracks") or []
                if zin is not None:
                    zin.on_ctrl(self.meta, self.tracks)
                if self.meta.get("pmt_pid"):
                    self.pmt_pid = self.meta["pmt_pid"]
                if self.meta.get("pcr_pid"):
                    self.pcr_pid = self.meta["pcr_pid"]
                if self.meta.get("video_pid"):
                    self.es.setdefault(self.meta["video_pid"], 0x02)
                for t in self.tracks:
                    if t.get("pid") and t.get("active", True):
                        self.es.setdefault(t["pid"], t.get("type", 0x81))
            return
        n = len(data)
        i = 0
        dropped = False
        while i + 188 <= n:
            if data[i] != 0x47:
                i += 1
                continue
            pid = ((data[i + 1] & 0x1F) << 8) | data[i + 2]
            pusi = (data[i + 1] >> 6) & 1
            afc = (data[i + 3] >> 4) & 3
            if pid in (OUT_PAT, OUT_PMT):
                i += 188
                continue
            if pusi:
                off = i + 4
                if afc & 2:
                    off = i + 5 + data[i + 4]
                if off < i + 188:
                    self._learn(pid, data, off)
            if pid in self.psi_pids or pid in (self.pmt_pid, self.src_pat_pid):
                i += 188
                continue
            q = self.q.get(pid)
            if q is None:
                q = self.q[pid] = deque(maxlen=QMAX)
            if len(q) == QMAX:
                self.qdrops += 1
                dropped = True
            q.append(bytes(data[i:i + 188]))
            if len(q) > self.qhigh:
                self.qhigh = len(q)
            i += 188
        if dropped:
            self.qdrop_bursts += 1

    def stale(self):
        return time.time() - self.last_push > STALE_SEC

    def stats(self):
        return {"chan_id": self.chan_id, "pushes": self.npush, "bytes": self.nbytes,
                "pids": sorted(self.q), "es": self.es, "pmt": self.pmt_pid,
                "pcr": self.pcr_pid, "gop_gate": self.gate_stats(), "kinds": {k.hex(): v for k, v in self.kinds.items()}}

    def gate_stats(self):
        g = self.gate
        return {"state": g.state, "reason": g.reason,
                "wait_sec": round(g.wait_sec, 3),
                "dropped_video_pkts": g.dropped_video,
                "dropped_audio_pkts": g.dropped_audio,
                "dropped_leading_b": g.dropped_b,
                "dropped_leading_b_pkts": g.dropped_b_pkts}

    def packets(self):
        gate = self.gate
        cc = {OUT_PAT: 0, OUT_PMT: 0}
        idle = 0
        #: [time of last PSI, media packets since last PSI, media since PSI (any)]
        last = [0.0, 0, 0]
        es, pcr_pid = {}, None
        il = (tsinterleave.Interleaver(clock=self.clock, wait=IL_WAIT,
                                 audio_lead=IL_AUDIO_LEAD)
              if INTERLEAVE else None)

        def psi(es, pcr_pid, force=False):
            now = time.time()
            if not es:
                return []
            if not force and now - last[0] < PSI_SEC and last[1] < PSI_PKTS:
                return []
            last[0], last[1], last[2] = now, 0, 0
            a = psi_packet(OUT_PAT, mk_pat(OUT_PMT), cc[OUT_PAT])
            cc[OUT_PAT] = (cc[OUT_PAT] + 1) & 0x0F
            b = psi_packet(OUT_PMT, mk_pmt(pcr_pid, es), cc[OUT_PMT])
            cc[OUT_PMT] = (cc[OUT_PMT] + 1) & 0x0F
            return (a, b)

        def emit(out, es, pcr_pid):
            """Yield gate output; PAT/PMT go first when output begins (unless
            they were just sent while waiting)."""
            if gate.opened:
                gate.opened = False
                if last[2] or not cc[OUT_PAT]:
                    for q in psi(es, pcr_pid, force=True):
                        yield q
            for p in out:
                yield p
                last[1] += 1
                last[2] += 1
                for q in psi(es, pcr_pid):
                    yield q

        while self.alive:
            if self.stale():
                idle += 1
                with self.lock:
                    es = dict(self.es)
                    pcr_pid = self.pcr_pid
                if pcr_pid is None:
                    pcr_pid = next((p for p, s in es.items() if s in (0x02, 0x1B, 0x24, 0x0F)), None)
                if il is not None and il.pending():
                    for q in il.pop(final=True):
                        for p in emit(gate.feed(q, es), es, pcr_pid):
                            yield p
                if idle > 3:
                    # stream ended: release anything the gate still holds
                    for p in emit(gate.flush(), es, pcr_pid):
                        yield p
                    return
                for p in emit(gate.tick(), es, pcr_pid):
                    yield p
                time.sleep(0.4)
                continue
            idle = 0
            with self.lock:
                runs = []
                for p in sorted(self.q):
                    q = self.q[p]
                    if q:
                        runs.append([q.popleft() for _ in range(min(len(q), CHUNK))])
                es = dict(self.es)
                pmt_pid, pcr_pid = self.pmt_pid, self.pcr_pid
            if il is not None:
                # runs are in sorted-PID order; per-PID packet order is kept
                for r in runs:
                    for p in r:
                        il.feed(((p[1] & 0x1F) << 8) | p[2], p)
                batch = il.pop()
            else:
                batch = []
                while any(runs):
                    for r in runs:
                        if r:
                            batch.append(r.pop(0))
            if pcr_pid is None:
                pcr_pid = next((p for p, s in es.items() if s in (0x02, 0x1B, 0x24, 0x0F)), None)
            if not batch:
                for p in emit(gate.tick(), es, pcr_pid):
                    yield p
                # keep PSI flowing while idle or while the gate is waiting
                for q in psi(es, pcr_pid):
                    yield q
                time.sleep(0.02)
                continue
            for p in batch:
                for o in emit(gate.feed(p, es), es, pcr_pid):
                    yield o
                if gate.state == GopGate.WAIT:
                    # waiting for a random-access point: keep the stream alive
                    for q in psi(es, pcr_pid):
                        yield q


class Slot:
    """One registered BRP device id and the M1 tuner instance leased to it."""

    def __init__(self, dev):
        self.dev = dev
        self.inst = None
        self.sess = None
        self.reserved = False
        self.bound = False
        #: Local host IP the M1 pushes this dev's streams to. Seeded from
        #: ZPUSH_IPS / the state file, else learned from the first burst.
        self.push_ip = PUSH_IPS.get(dev)
        #: True once the IP is trusted (seeded, loaded from state, or a tune
        #: actually claimed on it). An unconfirmed *learned* IP is dropped if
        #: its tune claim-times-out, so a bad learn never becomes permanent.
        self.push_ip_confirmed = self.push_ip is not None
        #: consecutive family conflicts on this IP -> auto-unlearn trigger.
        self.ip_conflicts = 0
        #: short per-slot quarantine for trailing bursts of the just-stopped
        #: channel (the M1 keeps pushing the old channel to the same IP ~2 s).
        self.quar_until = 0.0
        self.quar_family = None
        self.quar_chan = None
        #: program_number of the just-stopped channel (None if unknown).
        self.quar_prog = None


class Tuner:
    """Concurrent BRP tunes: the M1 leases one tuner instance per device id."""

    def __init__(self, port=PUSH_PORT, dev=None, devs=None):
        if devs is None:
            devs = list(DEVS)
        if dev and dev not in devs:
            devs = [dev] + list(devs)
        self.devs = devs
        self.dev = dev or (devs[0] if devs else DEV)
        self.slots = [Slot(d) for d in devs]
        self.lock = threading.RLock()
        self.seq = [500]
        self.tseq = [0]
        self.port = port
        self._srv = None
        self.tune_gate = threading.Lock()
        self._released = {}
        self.last_push_at = 0.0
        self.totals = {"sessions": 0, "qdrops": 0, "qdrop_bursts": 0,
                       "busy": 0, "queue_timeouts": 0, "claim_timeouts": 0,
                       "tune_failures": 0}
        self.last_fail = None
        self._start_listener()
        threading.Thread(target=self._heartbeat, daemon=True).start()

    def _start_listener(self):
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", self.port))
        s.listen(16)
        self._srv = s
        threading.Thread(target=self._accept, args=(s,), daemon=True).start()

    def _accept(self, s):
        while True:
            try:
                c, a = s.accept()
            except OSError:
                return
            threading.Thread(target=self._conn, args=(c,), daemon=True).start()

    def _conn(self, c):
        """Bind one push connection to its session, then feed it frames."""
        c.settimeout(15)
        zin = zkind = None          # diagnostics only (ZLOG_DIAG)
        why = "done"
        try:
            h = _recv_all(c, HDR)
            if not h or h[:4] != b"Data":
                return
            n = _frame_len(h)
            if n == 0 or n > 1 << 24:
                return
            d = _recv_all(c, n)
            if d is None:
                return
            try:
                local_ip = c.getsockname()[0]
            except OSError:
                local_ip = None
            pn, fam = _frame_pat(d)
            sess = self._bind(_frame_pids(d), fam, local_ip,
                              _frame_program(h, d), pn)
            if sess is None:
                zstream.log_unbound(h[4:8], local_ip, _frame_pids(d), fam)
            else:
                zin = getattr(sess, "zin", None)
                if zin is not None:
                    zkind = h[4:8]
                    zin.on_conn(zkind, local_ip, zstream.hdr_sid(h),
                                h[8:20].decode("ascii", "replace"))
            while sess is not None and sess.alive:
                self._feed(sess, h, d)
                h = _recv_all(c, HDR)
                if not h or h[:4] != b"Data":
                    why = "recv_closed_or_bad_header"
                    return
                n = _frame_len(h)
                if n == 0 or n > 1 << 24:
                    why = "bad_frame_len"
                    return
                d = _recv_all(c, n)
                if d is None:
                    why = "recv_closed_mid_frame"
                    return
            if sess is not None:
                why = "session_ended"
        except Exception:
            why = "error"
        finally:
            if zin is not None:
                zin.on_conn_close(zkind, why)
            try:
                c.close()
            except Exception:
                pass

    def _feed(self, sess, hdr, data):
        with sess.lock:
            sess.push(hdr[4:8], hdr, data)
        self.last_push_at = time.time()

    def _bind(self, pids, family=None, local_ip=None, prog=None, pn=None):
        """Route a push connection; see _bind_route. ``prog`` is a control
        leg's program_nbr, ``pn`` a media leg's PAT program_number. A control
        leg (no TS, family None) records prog on the session it routes to.

        Routing and the expect_prog/pat_prog updates happen under one
        acquisition of self.lock (an RLock; _bind_route re-enters it), so a
        concurrent media leg or stop() never sees a half-updated session. The
        first control leg of a tune wins (the M1 sends one per tune), and the
        first claiming PAT program sticks, so a late leg cannot overwrite
        them."""
        with self.lock:
            sess = self._bind_route(pids, family, local_ip, prog, pn)
            if sess is not None:
                if prog is not None and family is None:
                    if sess.expect_prog is not None:
                        if prog != sess.expect_prog:
                            # stale control for another program: drop it so
                            # it can't replace the session's meta/tracks
                            print("push-ip: stale ctrl dropped prog=%s "
                                  "expect=%s" % (prog, sess.expect_prog),
                                  flush=True)
                            return None
                    elif self._old_ctrl(sess, prog):
                        return None  # (family-fallback path; see _quar_hold)
                    else:
                        sess.expect_prog = prog
                if (family is not None and pn is not None
                        and sess.family == family and sess.pat_prog is None):
                    sess.pat_prog = pn
            return sess

    def _slot_of(self, sess):
        return next((s for s in self.slots if s.sess is sess), None)

    def _old_ctrl(self, sess, prog):
        """True if a control leg's program equals the program of the channel
        just stopped on sess's slot, during its quarantine. It may be a stale
        leg of the old tune, so it is dropped before it can be fed (its
        tracks would leak into the new session's PMT) or learn an IP; the new
        tune's own control leg can still set expect_prog. For a same-program
        pair (32.1 -> 9.1, both program 3) the new control is dropped too, so
        expect_prog stays unset and the hold times out (the documented
        fallback; 9b15358 dropped every control leg anyway)."""
        slot = self._slot_of(sess)
        return (slot is not None and slot.quar_prog is not None
                and prog == slot.quar_prog and time.time() < slot.quar_until
                and slot.quar_chan != sess.chan_id)

    def _trailing(self, slot, sess, family, pn, now):
        """True for a burst on a *claimed* session that carries the claimed
        PMT but the quarantined old channel's PAT program (e.g. 7.1 program 1
        after 9.1 program 3 claimed PMT 48)."""
        return (slot is not None and family is not None and pn is not None
                and family == sess.family
                and sess.pat_prog is not None and pn != sess.pat_prog
                and pn == slot.quar_prog and slot.quar_chan != sess.chan_id
                and now < slot.quar_until)

    def _quar_hold(self, slot, sess, family, pn, local_ip, now, prog=None):
        """True if a burst must be held because it may be a trailing burst of
        the channel just stopped on this slot (same PMT, quarantine live,
        different channel). It is released early only when its PAT
        program_number matches the new tune's control-leg program_nbr and
        differs from the old channel's; otherwise the quarantine times out."""
        if family is None:
            if prog is not None and self._old_ctrl(sess, prog):
                print("push-ip: quarantine hold ctrl ip=%s dev=%s prog=%s "
                      "left=%.1fs" % (local_ip, slot.dev, prog,
                      slot.quar_until - now), flush=True)
                return True
            return False
        if (family != slot.quar_family
                or now >= slot.quar_until or slot.quar_chan == sess.chan_id):
            return False
        exp = sess.expect_prog
        if (exp is not None and pn is not None and pn == exp
                and pn != slot.quar_prog):
            return False
        print("push-ip: quarantine hold ip=%s dev=%s fam=%s pn=%s expect=%s "
              "old_prog=%s left=%.1fs" % (local_ip, slot.dev, family, pn, exp,
              slot.quar_prog, slot.quar_until - now), flush=True)
        return True

    def _bind_route(self, pids, family=None, local_ip=None, prog=None,
                    pn=None):
        """Route a push connection to the session whose *channel* it carries.

        Primary key is the local destination IP the M1 pushed to: each BRP dev
        pushes to a distinct host IP (learned from the first burst of each tune,
        since tunes are serialised, or seeded via ZPUSH_IPS / the state file),
        so two channels that share a PMT PID no longer collide onto one
        session. When the IP is unknown we fall back to the PMT-PID "family"
        routing below.

        Trailing bursts: after stop() the M1 keeps pushing the old channel to
        the same IP for a few seconds, and slot 0 (.83) is reused on every
        channel change. Each slot carries a short quarantine
        (quar_family/quar_until/quar_chan): while the new session is still
        unclaimed, a burst whose family matches the just-stopped channel's PMT
        is dropped instead of claiming the new session with the old PMT -- and
        if old and new share a PMT, the burst is held (_quar_hold) unless its
        PAT program_number (``pn``) equals the program_nbr the new tune's
        control leg announced and differs from the old channel's. If old and
        new also share a program_number the hold lasts until the quarantine
        expires (QUARANTINE s after stop).
        """
        now = time.time()
        with self.lock:
            # ---- primary: route by local destination IP ----
            if local_ip is not None:
                owner = next((s for s in self.slots
                              if s.push_ip == local_ip), None)
                if owner is not None:
                    osess = owner.sess
                    if osess is None or not osess.alive:
                        # known IP but its slot isn't live: drop rather than
                        # fall through to family routing (would hijack a slot).
                        return None
                    if osess.family is None:
                        # unclaimed: guard against trailing old-channel bursts
                        if self._quar_hold(owner, osess, family, pn,
                                           local_ip, now, prog):
                            return None
                        if family is not None:
                            osess.family = family
                            owner.push_ip_confirmed = True
                        owner.bound = True
                        return osess
                    # claimed: accept only bursts of the claimed family
                    if family is not None and family != osess.family:
                        if (family == owner.quar_family
                                and now < owner.quar_until):
                            return None  # trailing old-channel burst
                        owner.ip_conflicts += 1
                        print("push-ip: conflict ip=%s dev=%s have_fam=%s "
                              "got_fam=%s n=%d" % (local_ip, owner.dev,
                              osess.family, family, owner.ip_conflicts),
                              flush=True)
                        if (owner.ip_conflicts >= IP_UNLEARN_CONFLICTS
                                and owner.dev not in SEEDED_DEVS):
                            print("push-ip: unlearn ip=%s dev=%s after %d "
                                  "conflicts" % (local_ip, owner.dev,
                                  owner.ip_conflicts), flush=True)
                            owner.push_ip = None
                            owner.push_ip_confirmed = False
                            owner.ip_conflicts = 0
                            self._save_state_locked()
                        return None
                    # same PMT as the claim but the old channel's PAT
                    # program during its quarantine: trailing burst (e.g. 7.1
                    # program 1 after 9.1 program 3 claimed PMT 48)
                    if (family is None and prog is not None
                            and self._old_ctrl(osess, prog)):
                        return None  # stale old-tune control leg
                    if self._trailing(owner, osess, family, pn, now):
                        print("push-ip: trailing hold ip=%s dev=%s fam=%s "
                              "pn=%s have=%s" % (local_ip, owner.dev, family,
                              pn, osess.pat_prog), flush=True)
                        return None
                    owner.ip_conflicts = 0
                    owner.bound = True
                    return osess
                # unknown IP: learn it into a fresh unclaimed slot with no IP
                fresh = [s for s in self.slots
                         if s.sess is not None and s.sess.alive
                         and s.sess.family is None and s.push_ip is None]
                if fresh:
                    slot = max(fresh, key=lambda fs: fs.sess.tune_seq)
                    if self._quar_hold(slot, slot.sess, family, pn,
                                       local_ip, now, prog):
                        return None  # trailing old burst on a re-used slot
                    slot.push_ip = local_ip
                    slot.push_ip_confirmed = family is not None
                    if family is not None:
                        slot.sess.family = family
                    slot.bound = True
                    print("push-ip: learn ip=%s dev=%s chan=%s fam=%s" % (
                        local_ip, slot.dev, slot.sess.chan_id, family),
                        flush=True)
                    self._save_state_locked()
                    return slot.sess
            # ---- fallback: PMT-PID "family" routing ----
            live = [s.sess for s in self.slots
                    if s.sess is not None and s.sess.alive]
            if family is not None:
                for sess in live:
                    if sess.family == family:
                        if self._trailing(self._slot_of(sess), sess, family,
                                          pn, now):
                            return None  # trailing old-program burst
                        return sess
                fresh = [x for x in live if x.family is None]
                if not fresh:
                    return None
                target = max(fresh, key=lambda x: x.tune_seq)
                rel = self._released.get(family)
                if (rel and now - rel[0] < QUARANTINE
                        and rel[1] != target.chan_id):
                    # same early release as _quar_hold: PAT program matches
                    # the new tune's control leg and isn't the old program
                    ts = self._slot_of(target)
                    if not (pn is not None and pn == target.expect_prog
                            and ts is not None and pn != ts.quar_prog):
                        return None
                target.family = family
                for s in self.slots:
                    if s.sess is target:
                        s.bound = True
                return target
            best, score, unbound = None, 0, None
            for s in self.slots:
                sess = s.sess
                if sess is None or not sess.alive:
                    continue
                hit = len(pids & set(sess.q))
                if hit > score:
                    best, score = sess, hit
                if not s.bound and (unbound is None or
                                    sess.tune_seq > unbound.tune_seq):
                    unbound = sess
            target = best or unbound
            if target is None:
                return None
            for s in self.slots:
                if s.sess is target:
                    s.bound = True
            return target

    #: (api, dev, thread name, monotonic acquire time) of the outermost
    #: ``call`` holding ``self.lock``; diagnostics only (ZLOG_DIAG).
    _lock_holder = None
    #: Seconds of lock wait / hold / heartbeat latency worth a log line.
    DIAG_SLOW = 1.0
    #: Seconds without a successful api11 beat for a live slot worth a log line.
    DIAG_HB_GAP = 4.0
    _diag_rl = zdiag.RateLimit(5.0)

    def call(self, api, a0=3, a1=0, a2=0, a3=0, payload=None, timeout=8.0,
             dev=None):
        """One BRP request/response, with lock-wait diagnostics.

        Behaviour is exactly ``_call_impl``: the same lock is taken the same way
        (``self.lock`` is an RLock, so the inner ``with self.lock`` re-enters).
        Diagnostics only measure how long we waited for the lock and who held
        it, and log (rate-limited) when that exceeds ``DIAG_SLOW``. Set
        ``ZLOG_DIAG=0`` to bypass the wrapper entirely."""
        if not zdiag.enabled():
            return self._call_impl(api, a0, a1, a2, a3, payload, timeout, dev)
        t_req = _mono()
        held = self._lock_holder
        self.lock.acquire()
        try:
            t_got = _mono()
            prev = self._lock_holder
            self._lock_holder = (api, dev or self.dev,
                                 threading.current_thread().name, t_got)
            wait = t_got - t_req
            if wait > self.DIAG_SLOW:
                self._diag_lock_wait(api, dev, wait, held, t_req)
            try:
                return self._call_impl(api, a0, a1, a2, a3, payload, timeout,
                                       dev)
            finally:
                self._lock_holder = prev
                if prev is None:
                    hold = _mono() - t_got
                    if hold > self.DIAG_SLOW:
                        self._diag_lock_hold(api, dev, hold)
        finally:
            self.lock.release()

    def _diag_lock_wait(self, api, dev, wait, held, t_req):
        try:
            ok, dropped = self._diag_rl.allow(("lockwait", api))
            if not ok:
                return
            if held:
                h = "holder=api%s dev=%s thread=%s held_for=%.2fs" % (
                    held[0], held[1], held[2], max(0.0, t_req - held[3]))
            else:
                h = "holder=unknown(non-call or released as we queued)"
            zdiag.dlog("lock wait api%s dev=%s waited=%.2fs %s%s" % (
                api, dev or self.dev, wait, h,
                " (+%d suppressed)" % dropped if dropped else ""))
        except Exception:
            pass

    def _diag_lock_hold(self, api, dev, hold):
        try:
            ok, dropped = self._diag_rl.allow(("lockhold", api))
            if ok:
                zdiag.dlog("lock held api%s dev=%s for %.2fs%s" % (
                    api, dev or self.dev, hold,
                    " (+%d suppressed)" % dropped if dropped else ""))
        except Exception:
            pass

    def _call_impl(self, api, a0=3, a1=0, a2=0, a3=0, payload=None,
                   timeout=8.0, dev=None):
        """One BRP request/response. len2 is the on-wire body size; len1 is the
        size after inflate, so reading max() over-reads compressed replies and
        _recv_all() then returns None on timeout."""
        with self.lock:
            self.seq[0] = (self.seq[0] + 1) & 0xFFFF
            kw = dict(a0=a0, a1=a1, a2=a2, a3=a3, flags=self.seq[0])
            if payload is not None:
                if isinstance(payload, (dict, list)):
                    payload = json.dumps(payload, separators=(", ", ": ")).encode()
                payload += b"\x00"
                kw["payload"] = payload
                kw["len1"] = 0
                kw["len2"] = len(payload)
            sk = socket.create_connection((M1, BRP_PORT), timeout=timeout)
            try:
                sk.sendall(pack(api, dev or self.dev, **kw))
                h = _recv_all(sk, 64)
                if not h:
                    return None
                r = unpack_header(h)
                n = r["len2"] or r["len1"]
                body = _recv_all(sk, n) if n else b""
                r["body"] = body or b""
                return r
            finally:
                sk.close()

    def status(self, a0=3, timeout=5.0):
        """BRP api19 tuner status: protocol, RF frequency, lock, SNR, RSSI."""
        r = self.call(19, a0=a0, a1=1, timeout=timeout)
        body = (r or {}).get("body") or b""
        if not body:
            return None
        if body[:1] == b"\x78":
            try:
                body = zlib.decompress(body)
            except Exception:
                return None
        try:
            return json.loads(body.rstrip(b"\x00"))
        except Exception:
            return None

    def statuses(self, timeout=5.0):
        """api19 for every slot that is actively streaming; None otherwise.

        The M1's api19 returns the currently-tuned frontend for any leased
        instance, not just the one that issued the tune. Querying idle-but-
        leased slots therefore duplicates the active channel's status across
        rows, so a single live stream showed up as two locked tuners in
        /tuners.json. Restrict the query to slots that hold a live session so
        each row maps to a real stream (idle leases report zero)."""
        with self.lock:
            insts = [s.inst if (s.sess is not None and s.sess.alive) else None
                     for s in self.slots]
        return [self.status(a0=i, timeout=timeout) if i is not None else None
                for i in insts]

    def live_info(self):
        """Per slot: None when idle, else {chan_id, nbytes, client_ip, t0}.
        Read-only, no BRP traffic (used by /status.json)."""
        with self.lock:
            return [({"chan_id": s.sess.chan_id, "nbytes": s.sess.nbytes,
                      "client_ip": getattr(s.sess, "client_ip", None),
                      "t0": getattr(s.sess, "t0", None)}
                     if (s.sess is not None and s.sess.alive) else None)
                    for s in self.slots]

    def _heartbeat(self):
        """Keep every leased tuner alive; the M1 drops idle leases."""
        state = {}
        while True:
            self._hb_pass(state)
            time.sleep(float(os.environ.get("ZHB_SEC", "2")))

    def _hb_pass(self, state):
        """One heartbeat pass over the live slots (api11 each).

        ``state`` maps (dev, session id) -> monotonic time of the last
        successful beat (or of first sight). Diagnostics are log-only: with
        ZLOG_DIAG=0 this is the historical loop body (errors swallowed)."""
        with self.lock:
            live = [(s.dev, s.inst, id(s.sess)) for s in self.slots
                    if s.sess is not None and s.sess.alive]
        diag = zdiag.enabled()
        if diag:
            keys = set((d, sid) for d, _i, sid in live)
            for k in [k for k in state if k not in keys]:
                del state[k]
        for dev, inst, sid in live:
            t0 = _mono()
            r = err = None
            try:
                r = self.call(11, a0=3 if inst is None else inst,
                              a1=int(time.time()), dev=dev, timeout=3.0)
            except Exception as e:
                err = e
            if diag:
                self._hb_diag(state, (dev, sid), dev, inst, t0, r, err)

    def _hb_diag(self, state, key, dev, inst, t0, r, err):
        try:
            now = _mono()
            dt = now - t0
            last_ok = state.setdefault(key, t0)
            ok = err is None and bool(r) and r.get("err_name") == "SUCCESS"
            gap = now - last_ok
            if ok:
                state[key] = now
            problems = []
            if err is not None:
                problems.append("raised %r" % (err,))
            elif not r:
                problems.append("no reply (None)")
            elif not ok:
                problems.append("reply err=%s" % (r.get("err_name"),))
            if dt > self.DIAG_SLOW:
                problems.append("slow dt=%.2fs" % dt)
            if gap > self.DIAG_HB_GAP:
                problems.append("gap=%.1fs since last ok beat%s" % (
                    gap, "" if ok else " (still failing)"))
            if not problems:
                return
            allowed, dropped = self._diag_rl.allow(("hb", dev))
            if allowed:
                zdiag.dlog("hb dev=%s inst=%s dt=%.2fs: %s%s" % (
                    dev, inst, dt, "; ".join(problems),
                    " (+%d suppressed)" % dropped if dropped else ""))
        except Exception:
            pass

    def _save_state_locked(self):
        """Persist dev->push_ip (call with self.lock held). Best-effort."""
        try:
            m = {s.dev: s.push_ip for s in self.slots if s.push_ip}
            tmp = PUSH_STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(m, f)
            os.replace(tmp, PUSH_STATE_FILE)
        except Exception as e:
            print("push-ip: state save failed: %r" % e, flush=True)

    def stop(self, sess):
        """Stop a tune but keep the lease: the pool is small and sticky."""
        save = False
        with self.lock:
            if sess.alive:
                self.totals["sessions"] += 1
                self.totals["qdrops"] += sess.qdrops
                self.totals["qdrop_bursts"] += sess.qdrop_bursts
            sess.alive = False
            if sess.family is not None:
                self._released[sess.family] = (time.time(), sess.chan_id)
            slot = next((s for s in self.slots if s.sess is sess), None)
            if slot is not None:
                # quarantine trailing old-channel bursts still arriving on this
                # slot's push IP, so they can't claim the next session.
                slot.quar_until = time.time() + QUARANTINE
                slot.quar_family = sess.family
                slot.quar_chan = sess.chan_id
                slot.quar_prog = (sess.pat_prog if sess.pat_prog is not None
                                  else sess.expect_prog)
                # a tune that never claimed (claim-timeout) may have learned a
                # wrong IP; drop it unless seeded or already confirmed earlier.
                if (sess.family is None and slot.push_ip
                        and slot.dev not in SEEDED_DEVS
                        and not slot.push_ip_confirmed):
                    print("push-ip: clear unconfirmed ip=%s dev=%s after "
                          "unclaimed tune" % (slot.push_ip, slot.dev),
                          flush=True)
                    slot.push_ip = None
                    save = True
                slot.sess = None
                slot.reserved = False
                slot.bound = False
                slot.ip_conflicts = 0
            if save:
                self._save_state_locked()
        if slot is None or slot.inst is None:
            return
        try:
            self.call(10, a0=slot.inst, dev=slot.dev, timeout=3.0)
        except Exception:
            pass

    def tune(self, chan_id, gap=0.8):
        """Tune a free slot; None means no tuner, queue timeout or no push.

        Tunes are serialised through ``tune_gate``: the gate is held from api9
        until this session has claimed its own push family (PMT PID), so a new
        family can only belong to the one tune in flight. Every exit path
        releases the gate; a tune whose push never arrives within CLAIM_WAIT is
        stopped and reported as a failure instead of streaming 0 bytes.
        """
        t0 = time.time()
        if not self.tune_gate.acquire(timeout=TUNE_QUEUE_WAIT):
            zdiag.tlog("tune %s: queue wait > %.0fs, giving up" % (
                chan_id, TUNE_QUEUE_WAIT))
            self._fail(chan_id, "queue_timeouts")
            return None
        try:
            qwait = time.time() - t0
            slot = self._reserve()
            if slot is None:
                zdiag.tlog("no free tuner slot for %s" % chan_id)
                self._fail(chan_id, "busy")
                return None
            sess = self._tune_slot(slot, chan_id, gap)
            if sess is None:
                self._fail(chan_id, "tune_failures")
                return None
            t1 = time.time()
            deadline = t1 + CLAIM_WAIT
            while sess.family is None and time.time() < deadline:
                time.sleep(0.05)
            if sess.family is None:
                zdiag.tlog("tune %s: no push claimed in %.1fs, stopping" % (
                    chan_id, CLAIM_WAIT))
                self.stop(sess)
                self._fail(chan_id, "claim_timeouts")
                return None
            sess.queue_wait = qwait
            sess.claim_wait = time.time() - t1
            zdiag.tlog("tune %s: pmt=%d queue_wait=%.2fs claim=%.2fs total=%.2fs" % (
                chan_id, sess.family, qwait, sess.claim_wait,
                time.time() - t0))
            zin = getattr(sess, "zin", None)
            if zin is not None:
                zdiag.dlog("tune claim %s dev=%s inst=%s pmt=%d queue_wait=%.2fs "
                           "claim=%.2fs push_conns_so_far={%s}" % (
                               zin._id(), slot.dev, slot.inst, sess.family, qwait,
                               sess.claim_wait, zin.conns()))
            return sess
        except Exception as e:
            zdiag.tlog("tune %s error %r" % (chan_id, e))
            self._fail(chan_id, "tune_failures")
            return None
        finally:
            self.tune_gate.release()

    def _fail(self, chan_id, kind):
        with self.lock:
            self.totals[kind] = self.totals.get(kind, 0) + 1
            self.last_fail = {"chan_id": chan_id, "reason": kind,
                              "at": time.time()}

    def _reserve(self):
        with self.lock:
            for s in self.slots:
                if not s.reserved and s.sess is None:
                    s.reserved = True
                    return s
        return None

    def join(self, slot):
        """api6 JOIN: the response's a1 is the tuner instance we get to use."""
        try:
            r = self.call(6, a0=0, dev=slot.dev, timeout=6.0)
        except Exception as e:
            zdiag.tlog("join %s err %r" % (slot.dev, e))
            return None
        if r and r.get("err_name") == "SUCCESS" and r.get("a1"):
            slot.inst = r["a1"]
            return slot.inst
        zdiag.tlog("join %s -> %s" % (slot.dev, r and r.get("err_name")))
        return None

    def bootstrap(self):
        """Join every device id so lineup/EPG calls already hold a lease."""
        for s in self.slots:
            if s.inst is None:
                self.join(s)
            time.sleep(0.2)

    def _tune_slot(self, slot, chan_id, gap):
        sess = Session(chan_id)
        inst = slot.inst
        for attempt in range(3):
            if inst is None:
                inst = self.join(slot)
                time.sleep(gap)
            try:
                r9 = self.call(9, a0=3 if inst is None else inst,
                               a3=0xC0DEFA11, payload={"chan_id": chan_id},
                               dev=slot.dev)
            except Exception as e:
                zdiag.tlog("tune err %r dev=%s (try %d)" % (
                    e, slot.dev, attempt + 1))
                time.sleep(gap)
                continue
            if r9 and r9.get("err_name") == "SUCCESS":
                with self.lock:
                    self.tseq[0] += 1
                    sess.tune_seq = self.tseq[0]
                    slot.sess = sess
                    slot.bound = False
                sess.last_push = time.time()
                for _ in range(int(PUSH_WAIT / 0.25)):
                    if sess.npush:
                        break
                    time.sleep(0.25)
                zdiag.tlog("tuned %s dev=%s inst=%s pushes=%d" % (
                    chan_id, slot.dev, inst, sess.npush))
                return sess
            zdiag.tlog("tune %s dev=%s inst=%s -> %s (try %d)" % (
                chan_id, slot.dev, inst, r9 and r9.get("err_name"),
                attempt + 1))
            inst = None
            time.sleep(gap)
        with self.lock:
            slot.reserved = False
        return None

    def live(self):
        with self.lock:
            return [{"dev": s.dev, "inst": s.inst,
                     "chan_id": s.sess.chan_id if s.sess else None}
                    for s in self.slots]

    def health(self):
        """Read-only snapshot for /health (no BRP calls on the lock path)."""
        with self.lock:
            live = [s.sess for s in self.slots
                    if s.sess is not None and s.sess.alive]
            leases = sum(1 for s in self.slots if s.inst is not None)
            qd = self.totals["qdrops"] + sum(x.qdrops for x in live)
            qb = self.totals["qdrop_bursts"] + sum(x.qdrop_bursts for x in live)
            slot_ips = [(s.dev, s.push_ip) for s in self.slots]
            out = {
                "slots": len(self.slots),
                "leases": leases,
                "sessions_live": len(live),
                "sessions_closed": self.totals["sessions"],
                "live": [{"chan_id": x.chan_id, "family": x.family,
                          "qdrops": x.qdrops, "qhigh": x.qhigh,
                          "last_push_age_sec": round(time.time() - x.last_push, 1)}
                         for x in live],
                "last_push_age_sec": (round(time.time() - self.last_push_at, 1)
                                      if self.last_push_at else None),
                "qdrops_total": qd,
                "qdrop_bursts_total": qb,
                "qmax": QMAX,
                "tune_gate_busy": self.tune_gate.locked(),
                "tune_fail_counts": {k: self.totals.get(k, 0) for k in
                                     ("busy", "queue_timeouts",
                                      "claim_timeouts", "tune_failures")},
                "last_fail": (dict(self.last_fail, age_sec=round(
                    time.time() - self.last_fail["at"], 1))
                    if self.last_fail else None),
            }
        # Presence is a socket bind per unique IP: compute once, off the lock.
        present = {}
        for _dev, ip in slot_ips:
            if ip and ip not in present:
                present[ip] = _ip_local(ip)
        out["push_ips"] = [{"dev": dev, "push_ip": ip,
                            "present": (present.get(ip) if ip else None)}
                           for dev, ip in slot_ips]
        missing = sorted({ip for _dev, ip in slot_ips
                          if ip and not present.get(ip)})
        out["push_ip_warning"] = (
            ("push IP alias(es) missing on host: %s -- concurrent streams on "
             "those tuners will fail until re-added (e.g. `ifconfig en0 alias "
             "<ip> 255.255.255.255`)" % ", ".join(missing)) if missing else None)
        return out


if __name__ == "__main__":
    cid = sys.argv[1] if len(sys.argv) > 1 else "0x1130000000002001"
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 10
    out = sys.argv[3] if len(sys.argv) > 3 else "out.ts"
    t = Tuner()
    s = t.tune(cid)
    if not s:
        print("TUNE FAILED")
        raise SystemExit(1)
    print("tuned %s" % cid, flush=True)
    n = 0
    t0 = time.time()
    with open(out, "wb") as f:
        for p in s.packets():
            f.write(p)
            n += 1
            if time.time() - t0 > secs:
                break
    print("wrote %d packets / %d bytes" % (n, n * 188), flush=True)
    print("stats", s.stats(), flush=True)
    t.stop(s)
