"""Full-stream diagnostics (log-only; no effect on bytes, pacing, locking or protocol).

Everything here is gated by ``zdiag.enabled()`` (``ZLOG_DIAG``, default on) and
every entry point swallows its own errors, so logging can never break a stream.

Pieces
  * ``StreamAnalyzer``  - watches the OUTPUT transport stream (exactly the bytes
    written to the client): PAT/PMT + codec at start and on any change, per
    second video/audio packet counts, A/V PTS skew, PCR-PTS drift, PCR
    discontinuities, continuity-counter errors per kind, PES starts, TEI,
    scrambling, null packets and adaptation-field stuffing, duplicate packets,
    and audio-only / video-only gap lines.
  * ``InputStats``      - the M1 push side: per-second frame counts per leg
    kind (video/audio/control), first/last frame header ids, control-leg JSON
    errors, push connection open/close with reasons.
  * ``Capture``         - optional (``ZCAPTURE_SECONDS``, default 0 = off) copy
    of the first N seconds of the output TS to a size-capped rotating folder.
  * ``StreamDiag``      - glue used by hdhr_emulator's stream handlers.

Switches (all optional)
  ZLOG_DIAG        master switch (zdiag). 0/false/no/off = historical logs only.
  ZLOG_TS          0 disables only the TS analyzer (default 1).
  ZGAP_MS          audio/video gap threshold, default 100 ms (raised
                   automatically to 2.5x the stream's nominal PES period).
  ZTS_BUDGET       max analyzer thread-CPU share of a second (default 0.20);
                   over budget for 2 seconds in a row switches the analyzer off
                   for that stream (logged).
  ZTS_LINE_BUDGET  max event lines (gaps, cc errors, psi...) per stream, 300.
  ZCAPTURE_SECONDS / ZCAPTURE_DIR / ZCAPTURE_MAX_MB / ZCAPTURE_FILE_MB
  ZDIAG_JSONL      path of a JSON-lines file with one client and one summary
                   event per stream (off unless set; 5 MB, one rotation).
"""
import json
import os
import re
import shutil
import threading
import time

import zdiag

PKT = 188
_M33 = (1 << 33) - 1

KV, KA, KO = 0, 1, 2
_KNAME = ("video", "audio", "other")

# per-second counter slots
(VP, AP, OP, NULL, PESV, PESA, TEI, SCR, STUFF, DUP, CCV, CCA, CCO,
 PCRDISC, SYNC, AFBAD, AGAPS, VGAPS, APTSBACK, NSLOT) = range(20)

VIDEO_TYPES = {0x01, 0x02, 0x10, 0x1B, 0x24, 0x42}
AUDIO_TYPES = {0x03, 0x04, 0x0F, 0x11, 0x81, 0x82, 0x83, 0x87, 0x8A}
ST_NAMES = {
    0x01: "MPEG1-video", 0x02: "MPEG2-video", 0x03: "MPEG1-audio",
    0x04: "MPEG2-audio", 0x06: "private-PES", 0x0F: "AAC-ADTS",
    0x10: "MPEG4-video", 0x11: "AAC-LATM", 0x1B: "H264", 0x24: "HEVC",
    0x42: "CAVS", 0x81: "AC3", 0x82: "DTS", 0x83: "TrueHD", 0x87: "EAC3",
    0x8A: "DTS",
}
# PES private streams (type 0x06) are audio when they carry these descriptors
_AUDIO_DESC = {0x6A: "AC3", 0x7A: "EAC3", 0x7B: "DTS", 0x7C: "AAC"}
_AAC_SR = (96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000,
           12000, 11025, 8000)


def _on(name, default="1"):
    return os.environ.get(name, default).strip().lower() not in zdiag._OFF


def _fenv(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def _s33(d):
    """Signed difference of two 33-bit 90 kHz values."""
    d &= _M33
    return d - (1 << 33) if d >= (1 << 32) else d


_CRC_T = []
for _n in range(256):
    _c = _n << 24
    for _ in range(8):
        _c = ((_c << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if _c & 0x80000000 else (_c << 1) & 0xFFFFFFFF
    _CRC_T.append(_c)


def crc32_mpeg(data):
    c = 0xFFFFFFFF
    for b in data:
        c = ((c << 8) & 0xFFFFFFFF) ^ _CRC_T[((c >> 24) ^ b) & 0xFF]
    return c


def _cpu():
    try:
        return time.thread_time()
    except Exception:  # pragma: no cover - platforms without thread_time
        return time.process_time()


def sniff_audio(b):
    """Codec label from the first bytes of an audio ES payload, or None."""
    try:
        if len(b) < 7:
            return None
        if b[0] == 0x0B and b[1] == 0x77:
            bsid = b[5] >> 3
            if bsid > 10:
                return "EAC3"
            sr = {0: "48k", 1: "44.1k", 2: "32k"}.get(b[4] >> 6, "?")
            return "AC3/%s/acmod%d" % (sr, b[6] >> 5)
        if b[0] == 0xFF and (b[1] & 0xF6) == 0xF0:
            i = (b[2] >> 2) & 15
            sr = _AAC_SR[i] if i < len(_AAC_SR) else "?"
            return "AAC-ADTS/%s/ch%d" % (sr, ((b[2] & 1) << 2) | (b[3] >> 6))
        if b[0] == 0xFF and (b[1] & 0xE0) == 0xE0 and (b[1] >> 1) & 3:
            layer = {1: "L3", 2: "MP2", 3: "L1"}[(b[1] >> 1) & 3]
            sr = (44.1, 48, 32, 0)[(b[2] >> 2) & 3]
            return "MPEG-audio/%s/%gk" % (layer, sr)
        if b[0] == 0x56 and (b[1] & 0xE0) == 0xE0:
            return "AAC-LATM"
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------
# JSON-lines side file (optional)
# --------------------------------------------------------------------------
_jlk = threading.Lock()
_JMAX = 5 * 1024 * 1024


def jsonl(event, **fields):
    """Append one JSON line to ``ZDIAG_JSONL`` when it is set (else no-op)."""
    path = os.environ.get("ZDIAG_JSONL", "").strip()
    if not path or not zdiag.enabled():
        return
    try:
        rec = {"ts": zdiag.ts(), "event": event}
        rec.update(fields)
        line = json.dumps(rec, separators=(",", ":"), default=str) + "\n"
        with _jlk:
            try:
                if os.path.getsize(path) > _JMAX:
                    os.replace(path, path + ".1")
            except OSError:
                pass
            with open(path, "a") as f:
                f.write(line)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Output transport stream analyzer
# --------------------------------------------------------------------------
class StreamAnalyzer:
    def __init__(self, label, seq=None, clock=time.monotonic, emit=None,
                 gap_ms=None, line_budget=None, cpu=_cpu):
        self.label, self.seq = label, seq
        self.clock = clock
        self.emit = emit or zdiag.dlog
        self.cpu_fn = cpu
        self.gap_ms = _fenv("ZGAP_MS", 100) if gap_ms is None else gap_ms
        self.line_budget = (int(_fenv("ZTS_LINE_BUDGET", 300))
                            if line_budget is None else line_budget)
        self.cpu_budget = _fenv("ZTS_BUDGET", 0.20)
        self.pcr_jump_ms = _fenv("ZPCR_JUMP_MS", 500)
        self.t_start = clock()
        self.sec_end = self.t_start + 1.0
        self.sec = [0] * NSLOT
        self.tot = [0] * NSLOT
        self.ps = {}                 # pid -> [last_cc, prev_i, prev_data, cc_err, pkts]
        self.kinds = {}              # pid -> KV/KA/KO
        self.declared = set()
        self.pmt_pids = {}           # pmt pid -> program
        self.pcr_pid = None
        self.psi_cache = {}
        self.psi_sig = None
        self.psi_changes = 0
        self.psi_crc_err = 0
        self.pat_sig = None
        self.carry = b""
        self.off = False
        self.cpu = 0.0
        self.sec_cpu = 0.0
        self.over = 0
        self.errors = 0
        self.nbytes = 0
        # PTS / PCR state
        self.v_pts = self.a_pts = None       # last video / primary audio PTS
        self.a_pts_pid = None
        self.a_last = {}                     # audio pid -> last PTS
        self.v_wall = self.a_wall = None
        self.v_gap_open = self.a_gap_open = False
        self.nom = {KV: [], KA: []}          # first PES deltas (ms) per kind
        self.nom_val = {KV: None, KA: None}
        self.last_pcr = None
        self.off0 = None
        self.pcr_pts = None
        self.pcr_drift_max = 0.0
        self.skew_sec_max = None
        self.skew_max = None
        self.skew_last = None
        self.codec = {}
        self.codec_changes = 0
        # gap stats
        self.a_gap_n = self.v_gap_n = 0
        self.a_gap_wall_max = self.a_gap_pts_max = 0.0
        self.v_gap_wall_max = self.v_gap_pts_max = 0.0
        self.a_back_n = self.a_jump_n = 0
        # line limiter
        self.ev_lines = 0
        self.ev_sec_lines = 0
        self.ev_dropped = 0
        self.cc_lines = 0

    # -- output -----------------------------------------------------------
    def _id(self):
        return "stream=%s seq=%s" % (self.label, self.seq if self.seq is not None else "-")

    def _say(self, msg):
        try:
            self.emit(msg)
        except Exception:
            pass

    def _event(self, msg):
        """Event line, limited per stream and per second."""
        if self.ev_lines >= self.line_budget or self.ev_sec_lines >= 10:
            self.ev_dropped += 1
            return
        self.ev_lines += 1
        self.ev_sec_lines += 1
        self._say(msg)

    # -- public ------------------------------------------------------------
    def feed(self, data, now=None):
        """Analyse bytes exactly as written to the client."""
        if self.off or not data:
            return
        try:
            t0 = self.cpu_fn()
            now = self.clock() if now is None else now
            self.nbytes += len(data)
            if self.carry:
                data = self.carry + bytes(data)
                self.carry = b""
            r = len(data) % PKT
            if r:
                self.carry = bytes(data[len(data) - r:])
                data = data[:len(data) - r]
            self._feed(data, now)
            d = self.cpu_fn() - t0
            self.cpu += d
            self.sec_cpu += d
        except Exception:
            self.errors += 1

    def tick(self, now=None):
        """Call often while live; emits the per-second line and stall lines."""
        try:
            now = self.clock() if now is None else now
            self._stalls(now)
            if now < self.sec_end:
                return
            self._second(now)
        except Exception:
            self.errors += 1

    # -- core --------------------------------------------------------------
    def _feed(self, data, now):
        sec, ps, kinds = self.sec, self.ps, self.kinds
        for i in range(0, len(data), PKT):
            if data[i] != 0x47:
                sec[SYNC] += 1
                continue
            b1 = data[i + 1]
            pid = ((b1 & 0x1F) << 8) | data[i + 2]
            if pid == 0x1FFF:
                sec[NULL] += 1
                continue
            b3 = data[i + 3]
            k = kinds.get(pid, KO)
            sec[k] += 1                      # VP / AP / OP are slots 0/1/2
            if b1 & 0x80:
                sec[TEI] += 1
            if b3 & 0xC0:
                sec[SCR] += 1
            afc = (b3 >> 4) & 3
            cc = b3 & 15
            st = ps.get(pid)
            if st is None:
                st = ps[pid] = [None, 0, None, 0, 0]
            st[4] += 1
            disc = 0
            pcr_at = False
            off = i + 4
            if afc & 2:
                alen = data[i + 4]
                if alen > 183:
                    sec[AFBAD] += 1
                    off = i + PKT
                else:
                    off = i + 5 + alen
                    if alen > 0:
                        fl = data[i + 5]
                        disc = fl & 0x80
                        used = 1 + (6 if fl & 0x10 else 0) + (6 if fl & 0x08 else 0)
                        if alen > used:
                            sec[STUFF] += alen - used
                        if fl & 0x10 and pid == self.pcr_pid and alen >= 7:
                            pcr_at = True
            if afc & 1:
                last = st[0]
                err = False
                if last is not None and not disc:
                    if cc == last:
                        pd = st[2]
                        if pd is not None and pd[st[1]:st[1] + PKT] == data[i:i + PKT]:
                            sec[DUP] += 1
                        else:
                            err = True
                    elif cc != ((last + 1) & 15):
                        err = True
                st[0], st[1], st[2] = cc, i, data
                if err:
                    sec[CCV + k] += 1
                    st[3] += 1
                    self._cc_line(pid, k, last, cc)
                if (b1 & 0x40) and off + 9 <= i + PKT:
                    self._pusi(pid, k, data, i, off, now)
            if pcr_at:
                # after the PES header of the same packet, so the PTS paired
                # with this PCR is the picture that starts here
                self._pcr(data, i, disc, now)

    def _cc_line(self, pid, k, last, cc):
        if self.cc_lines >= 20:
            return
        self.cc_lines += 1
        self._event("ts cc_error %s pid=0x%x kind=%s expected=%d got=%d" % (
            self._id(), pid, _KNAME[k], ((last + 1) & 15) if last is not None else -1, cc))

    def _pusi(self, pid, k, data, i, off, now):
        if pid == 0 or pid in self.pmt_pids:
            self._psi(pid, data, i, off)
            return
        if data[off] or data[off + 1] or data[off + 2] != 1:
            return
        sid = data[off + 3]
        if k == KO and pid not in self.declared:
            if 0xE0 <= sid <= 0xEF:
                k = self.kinds[pid] = KV
            elif 0xC0 <= sid <= 0xDF:
                k = self.kinds[pid] = KA
        if k == KV:
            self.sec[PESV] += 1
        elif k == KA:
            self.sec[PESA] += 1
        else:
            return
        pts = None
        if data[off + 7] & 0x80 and off + 14 <= i + PKT:
            t = data[off + 9:off + 14]
            pts = (((t[0] >> 1) & 7) << 30 | t[1] << 22 | (t[2] >> 1) << 15
                   | t[3] << 7 | t[4] >> 1)
        if k == KV:
            self._v_pes(pid, pts, now)
        else:
            lbl = sniff_audio(data[off + 9 + data[off + 8]:i + PKT])
            self._a_pes(pid, pts, now, lbl)

    # -- PSI ---------------------------------------------------------------
    def _psi(self, pid, data, i, off):
        s = off + 1 + data[off]
        end = i + PKT
        if s + 3 > end:
            return
        ln = ((data[s + 1] & 0x0F) << 8) | data[s + 2]
        if s + 3 + ln > end or ln < 9:
            return
        sec = bytes(data[s:s + 3 + ln])
        if self.psi_cache.get(pid) == sec:
            return
        if crc32_mpeg(sec) != 0:
            self.psi_crc_err += 1
            self._event("ts psi_crc_error %s pid=0x%x" % (self._id(), pid))
            return
        self.psi_cache[pid] = sec
        tid = sec[0]
        if tid == 0x00 and pid == 0:
            progs = {}
            e = sec[8:-4]
            while len(e) >= 4:
                pn = (e[0] << 8) | e[1]
                if pn:
                    progs[((e[2] & 0x1F) << 8) | e[3]] = pn
                e = e[4:]
            sig = tuple(sorted(progs.items()))
            if sig != self.pat_sig:
                if self.pat_sig is not None:
                    self.psi_changes += 1
                    self._event("ts pat_change %s old=%s new=%s" % (
                        self._id(), list(self.pat_sig), list(sig)))
                self.pat_sig = sig
                self.pmt_pids = progs
        elif tid == 0x02 and pid in self.pmt_pids:
            self._pmt(pid, sec)

    def _pmt(self, pid, sec):
        prog = (sec[3] << 8) | sec[4]
        pcr = ((sec[8] & 0x1F) << 8) | sec[9]
        pil = ((sec[10] & 0x0F) << 8) | sec[11]
        e = sec[12 + pil:-4]
        es = []
        while len(e) >= 5:
            st = e[0]
            ep = ((e[1] & 0x1F) << 8) | e[2]
            el = ((e[3] & 0x0F) << 8) | e[4]
            desc, d = [], e[5:5 + el]
            while len(d) >= 2:
                desc.append(d[0])
                d = d[2 + d[1]:]
            es.append((ep, st, tuple(sorted(set(desc)))))
            e = e[5 + el:]
        sig = (prog, pcr, tuple(sorted(es)))
        if sig == self.psi_sig:
            return
        old, self.psi_sig = self.psi_sig, sig
        self.pcr_pid = pcr
        kinds, declared = {}, set()
        names = []
        for ep, st, desc in es:
            declared.add(ep)
            if st in VIDEO_TYPES:
                kd, nm = KV, ST_NAMES.get(st, "0x%02x" % st)
            elif st in AUDIO_TYPES:
                kd, nm = KA, ST_NAMES.get(st, "0x%02x" % st)
            elif st == 0x06 and any(t in _AUDIO_DESC for t in desc):
                kd = KA
                nm = next(_AUDIO_DESC[t] for t in desc if t in _AUDIO_DESC)
            else:
                kd, nm = KO, ST_NAMES.get(st, "0x%02x" % st)
            kinds[ep] = kd
            names.append("0x%x:0x%02x:%s:%s" % (ep, st, nm, _KNAME[kd]))
        # in place: _feed holds a local reference to self.kinds
        self.kinds.clear()
        self.kinds.update(kinds)
        self.declared = declared
        if old is None:
            self._say("ts psi %s program=%d pmt_pid=0x%x pcr_pid=0x%x es=[%s]" % (
                self._id(), prog, pid, pcr, " ".join(names)))
        else:
            self.psi_changes += 1
            self._event("ts psi_change %s program=%d pmt_pid=0x%x pcr_pid=0x%x es=[%s] "
                        "(was program=%d pcr_pid=0x%x es=%d)" % (
                            self._id(), prog, pid, pcr, " ".join(names),
                            old[0], old[1], len(old[2])))

    # -- PCR / PES ---------------------------------------------------------
    def _pcr(self, data, i, disc, now):
        base = (data[i + 6] << 25 | data[i + 7] << 17 | data[i + 8] << 9
                | data[i + 9] << 1 | data[i + 10] >> 7)
        if self.last_pcr is not None and not disc:
            d = _s33(base - self.last_pcr) / 90.0
            if d < 0 or d > self.pcr_jump_ms:
                self.sec[PCRDISC] += 1
                self._event("ts pcr_discontinuity %s delta_ms=%.0f" % (self._id(), d))
        self.last_pcr = base
        if self.v_pts is not None:
            off = _s33(self.v_pts - base) / 90.0
            if self.off0 is None:
                self.off0 = off
            self.pcr_pts = off
            dr = abs(off - self.off0)
            if dr > self.pcr_drift_max:
                self.pcr_drift_max = dr

    def _thr(self, kind):
        nv = self.nom_val[kind]
        return max(self.gap_ms, 2.5 * nv) if nv else max(self.gap_ms, 500.0)

    def _learn_nominal(self, kind, d):
        lst = self.nom[kind]
        if self.nom_val[kind] is None and 0 < d < 500:
            lst.append(d)
            if len(lst) >= 12:
                lst.sort()
                self.nom_val[kind] = lst[len(lst) // 2]

    def _skew(self):
        if self.a_pts is None or self.v_pts is None:
            return
        sk = _s33(self.a_pts - self.v_pts) / 90.0
        self.skew_last = sk
        if self.skew_sec_max is None or abs(sk) > abs(self.skew_sec_max):
            self.skew_sec_max = sk
        if self.skew_max is None or abs(sk) > abs(self.skew_max):
            self.skew_max = sk

    def _v_pes(self, pid, pts, now):
        wall = None
        if self.v_wall is not None:
            wall = (now - self.v_wall) * 1000.0
        self.v_wall = now
        self.v_gap_open = False
        pms = None
        if pts is not None:
            if self.v_pts is not None:
                pms = _s33(pts - self.v_pts) / 90.0
                self._learn_nominal(KV, pms)
            self.v_pts = pts
            self._skew()
        thr = self._thr(KV)
        if (wall is not None and wall > thr) or (pms is not None and pms > thr):
            self.sec[VGAPS] += 1
            self.v_gap_n += 1
            self.v_gap_wall_max = max(self.v_gap_wall_max, wall or 0.0)
            self.v_gap_pts_max = max(self.v_gap_pts_max, pms or 0.0)
            self._event("ts gap %s kind=video wall_ms=%s pts_ms=%s thr_ms=%.0f audio_age_ms=%s" % (
                self._id(), "-" if wall is None else "%.0f" % wall,
                "-" if pms is None else "%.0f" % pms, thr,
                "-" if self.a_wall is None else "%.0f" % ((now - self.a_wall) * 1000)))

    def _a_pes(self, pid, pts, now, label):
        wall = None
        if self.a_wall is not None:
            wall = (now - self.a_wall) * 1000.0
        self.a_wall = now
        self.a_gap_open = False
        if self.a_pts_pid is None:
            self.a_pts_pid = pid
        pms = None
        if pts is not None:
            last = self.a_last.get(pid)
            if last is not None:
                d = _s33(pts - last) / 90.0
                if d < 0:
                    self.sec[APTSBACK] += 1
                    self.a_back_n += 1
                    self._event("ts audio_pts_backwards %s pid=0x%x delta_ms=%.0f" % (
                        self._id(), pid, d))
                else:
                    pms = d
                    if pid == self.a_pts_pid:
                        self._learn_nominal(KA, d)
                    if d > 1000:
                        self.a_jump_n += 1
            self.a_last[pid] = pts
            if pid == self.a_pts_pid:
                self.a_pts = pts
                self._skew()
        if label is not None:
            old = self.codec.get(pid)
            if old is None:
                self.codec[pid] = label
                self._say("ts audio_codec %s pid=0x%x codec=%s" % (self._id(), pid, label))
            elif old != label:
                self.codec[pid] = label
                self.codec_changes += 1
                self._event("ts audio_codec_change %s pid=0x%x old=%s new=%s" % (
                    self._id(), pid, old, label))
        thr = self._thr(KA)
        if (wall is not None and wall > thr) or (pms is not None and pms > thr):
            self.sec[AGAPS] += 1
            self.a_gap_n += 1
            self.a_gap_wall_max = max(self.a_gap_wall_max, wall or 0.0)
            self.a_gap_pts_max = max(self.a_gap_pts_max, pms or 0.0)
            self._event("ts gap %s kind=audio pid=0x%x wall_ms=%s pts_ms=%s thr_ms=%.0f%s video_age_ms=%s" % (
                self._id(), pid, "-" if wall is None else "%.0f" % wall,
                "-" if pms is None else "%.0f" % pms, thr,
                " pts_jump=1" if pms is not None and pms > 1000 else "",
                "-" if self.v_wall is None else "%.0f" % ((now - self.v_wall) * 1000)))

    def _stalls(self, now):
        """Ongoing audio-only / video-only stall (no PES for a while)."""
        for kd, wall, flag in ((KA, self.a_wall, "a_gap_open"),
                               (KV, self.v_wall, "v_gap_open")):
            if wall is None or getattr(self, flag):
                continue
            age = (now - wall) * 1000.0
            if age > max(self._thr(kd), 1000.0):
                setattr(self, flag, True)
                oth = self.v_wall if kd == KA else self.a_wall
                self._event("ts stall %s kind=%s no_pes_ms=%.0f other_age_ms=%s" % (
                    self._id(), _KNAME[kd], age,
                    "-" if oth is None else "%.0f" % ((now - oth) * 1000)))

    # -- per second / summary ------------------------------------------------
    def _second(self, now):
        s = self.sec
        pm = lambda v: "-" if v is None else "%d" % (v // 90)
        line = ("ts sec %s t=%.0f v_pkts=%d v_bytes=%d a_pkts=%d a_bytes=%d o_pkts=%d "
                "null=%d pes_v=%d pes_a=%d v_pts_ms=%s a_pts_ms=%s av_skew_ms=%s "
                "av_skew_max_ms=%s pcr_pts_ms=%s pcr_drift_ms=%s pcr_disc=%d "
                "cc_err_v=%d cc_err_a=%d cc_err_o=%d dup=%d tei=%d scr=%d "
                "af_stuff_bytes~=%d sync_err=%d a_gaps=%d v_gaps=%d a_pts_back=%d "
                "ev_dropped=%d an_us=%d" % (
                    self._id(), now - self.t_start, s[VP], s[VP] * PKT, s[AP],
                    s[AP] * PKT, s[OP], s[NULL], s[PESV], s[PESA],
                    pm(self.v_pts), pm(self.a_pts),
                    "-" if self.skew_last is None else "%.0f" % self.skew_last,
                    "-" if self.skew_sec_max is None else "%.0f" % self.skew_sec_max,
                    "-" if self.pcr_pts is None else "%.0f" % self.pcr_pts,
                    "-" if self.pcr_pts is None or self.off0 is None
                    else "%.0f" % (self.pcr_pts - self.off0),
                    s[PCRDISC], s[CCV], s[CCA], s[CCO], s[DUP], s[TEI], s[SCR],
                    s[STUFF], s[SYNC], s[AGAPS], s[VGAPS], s[APTSBACK],
                    self.ev_dropped, int(self.sec_cpu * 1e6)))
        self._say(line)
        # analyzer overhead guard (thread CPU share of the last second)
        el = max(0.5, now - (self.sec_end - 1.0))
        if self.sec_cpu / el > self.cpu_budget:
            self.over += 1
            if self.over >= 2:
                self.off = True
                self._say("ts analyzer_off %s reason=overhead cpu_share=%.2f budget=%.2f" % (
                    self._id(), self.sec_cpu / el, self.cpu_budget))
        else:
            self.over = 0
        t = self.tot
        for j in range(NSLOT):
            t[j] += s[j]
            s[j] = 0
        self.sec_cpu = 0.0
        self.skew_sec_max = None
        self.ev_sec_lines = 0
        self.sec_end = now + 1.0

    def summary(self, reason=None):
        """Stream-end summary text (also usable by tests)."""
        t = [a + b for a, b in zip(self.tot, self.sec)]
        dur = max(0.0, self.clock() - self.t_start)
        pids = sorted(self.ps.items(), key=lambda kv: -kv[1][4])[:8]
        per = ",".join("0x%x(%s):%d/%d" % (p, _KNAME[self.kinds.get(p, KO)][0], v[4], v[3])
                       for p, v in pids)
        return ("ts summary %s dur=%.1fs close=%s bytes=%d v_pkts=%d v_bytes=%d "
                "a_pkts=%d a_bytes=%d o_pkts=%d null=%d pes_v=%d pes_a=%d "
                "cc_err_v=%d cc_err_a=%d cc_err_o=%d dup=%d tei=%d scr=%d "
                "pcr_disc=%d max_av_skew_ms=%s max_pcr_pts_drift_ms=%.0f "
                "a_gaps=%d a_gap_max_wall_ms=%.0f a_gap_max_pts_ms=%.0f "
                "v_gaps=%d v_gap_max_wall_ms=%.0f v_gap_max_pts_ms=%.0f "
                "a_pts_back=%d a_pts_jump=%d psi_changes=%d psi_crc_err=%d "
                "codec_changes=%d codec=%s a_nominal_pes_ms=%s v_nominal_pes_ms=%s "
                "sync_err=%d ev_dropped=%d an_cpu_ms=%d an_off=%d pid_pkts/ccerr=%s" % (
                    self._id(), dur, reason or "-", self.nbytes, t[VP], t[VP] * PKT,
                    t[AP], t[AP] * PKT, t[OP], t[NULL], t[PESV], t[PESA],
                    t[CCV], t[CCA], t[CCO], t[DUP], t[TEI], t[SCR], t[PCRDISC],
                    "-" if self.skew_max is None else "%.0f" % self.skew_max,
                    self.pcr_drift_max, self.a_gap_n, self.a_gap_wall_max,
                    self.a_gap_pts_max, self.v_gap_n, self.v_gap_wall_max,
                    self.v_gap_pts_max, self.a_back_n, self.a_jump_n,
                    self.psi_changes, self.psi_crc_err, self.codec_changes,
                    ",".join(sorted(set(self.codec.values()))) or "-",
                    "-" if not self.nom_val[KA] else "%.0f" % self.nom_val[KA],
                    "-" if not self.nom_val[KV] else "%.0f" % self.nom_val[KV],
                    t[SYNC], self.ev_dropped, int(self.cpu * 1000),
                    1 if self.off else 0, per or "-"))

    def finish(self, reason=None):
        try:
            text = self.summary(reason)
            self._say(text)
            return text
        except Exception:
            return None


# --------------------------------------------------------------------------
# M1 push side
# --------------------------------------------------------------------------
_KIND = {b"\x01\x01\x01\x01": "v", b"\x01\x01\x01\x02": "a", b"\x01\x01\x02\x00": "c"}
_unbound_rl = zdiag.RateLimit(5.0)


def kind_of(k):
    return _KIND.get(bytes(k), "?")


def hdr_sid(hdr):
    """Per-tune id the M1 writes in header bytes 28-31 (same for the video and
    audio legs of one tune; 0 on the control leg)."""
    try:
        return int.from_bytes(hdr[28:32], "big")
    except Exception:
        return -1


def log_unbound(kind, local_ip, pids, family):
    """A push connection that no session claimed (its data is dropped)."""
    try:
        if not zdiag.enabled():
            return
        ok, dropped = _unbound_rl.allow(("unbound", kind_of(kind), local_ip))
        if ok:
            zdiag.dlog("push conn unbound kind=%s dst_ip=%s pids=%s fam=%s%s" % (
                kind_of(kind), local_ip, sorted(pids)[:6], family,
                " (+%d suppressed)" % dropped if dropped else ""))
    except Exception:
        pass


class InputStats:
    """Per-session M1 push statistics. Pushes arrive on several threads; one
    small private lock guards the counters. ``session`` supplies tune_seq."""

    MAXEV = 60

    def __init__(self, label, session=None, clock=time.monotonic, emit=None):
        self.label, self.session = label, session
        self.clock = clock
        self.emit = emit or zdiag.dlog
        self.seq = None
        self.lk = threading.Lock()
        self.t_start = clock()
        self.sec_end = self.t_start + 1.0
        self.frames = {"v": 0, "a": 0, "c": 0, "?": 0}
        self.bytes = {"v": 0, "a": 0, "c": 0, "?": 0}
        self.s_frames = dict(self.frames)
        self.s_bytes = dict(self.bytes)
        self.first = self.last = None
        self.s_first = self.s_last = None
        self.last_t = None
        self.s_gap = 0.0
        self.gap_max = 0.0
        self.sids = set()
        self.sid_changes = 0
        self.conn_open = {"v": 0, "a": 0, "c": 0, "?": 0}
        self.conn_total = dict(self.conn_open)
        self.conn_closed = dict(self.conn_open)
        self.ctrl_n = self.ctrl_err = 0
        self.ev = 0

    def _id(self):
        tseq = getattr(self.session, "tune_seq", None)
        return "stream=%s seq=%s tune_seq=%s" % (
            self.label, "-" if self.seq is None else self.seq,
            "-" if tseq is None else tseq)

    def _say(self, msg, event=True):
        if event:
            if self.ev >= self.MAXEV:
                return
            self.ev += 1
        try:
            self.emit(msg)
        except Exception:
            pass

    def on_frame(self, kind, hdr, n):
        try:
            k = kind_of(kind)
            now = self.clock()
            sid = hdr_sid(hdr)
            note = None
            with self.lk:
                self.frames[k] += 1
                self.bytes[k] += n
                self.s_frames[k] += 1
                self.s_bytes[k] += n
                tag = "%s:%x" % (k, sid)
                if self.first is None:
                    self.first = tag
                if self.s_first is None:
                    self.s_first = tag
                self.last = self.s_last = tag
                if self.last_t is not None:
                    g = now - self.last_t
                    if g > self.s_gap:
                        self.s_gap = g
                    if g > self.gap_max:
                        self.gap_max = g
                self.last_t = now
                if k != "c":
                    if self.sids and sid not in self.sids:
                        self.sid_changes += 1
                        note = "ts push_sid_change %s sid=0x%x known=%s kind=%s" % (
                            self._id(), sid, ["0x%x" % x for x in sorted(self.sids)], k)
                    self.sids.add(sid)
            if note:
                self._say(note)
        except Exception:
            pass

    def on_ctrl(self, meta, tracks):
        try:
            with self.lk:
                self.ctrl_n += 1
            tr = ["0x%x:t0x%x%s" % (t.get("pid", 0), t.get("type", 0) or 0,
                                    "" if t.get("active", True) else ":inactive")
                  for t in (tracks or []) if isinstance(t, dict) and t.get("pid")]
            self._say("ts ctrl_leg %s program=%s pmt_pid=%s pcr_pid=%s video_pid=%s tracks=[%s]" % (
                self._id(), (meta or {}).get("program_nbr"), (meta or {}).get("pmt_pid"),
                (meta or {}).get("pcr_pid"), (meta or {}).get("video_pid"), " ".join(tr)))
        except Exception:
            pass

    def on_ctrl_err(self, why="json"):
        try:
            with self.lk:
                self.ctrl_err += 1
            self._say("ts ctrl_leg_error %s why=%s" % (self._id(), why))
        except Exception:
            pass

    def on_conn(self, kind, local_ip, sid, dev=None):
        try:
            k = kind_of(kind)
            with self.lk:
                self.conn_open[k] += 1
                self.conn_total[k] += 1
                co = dict(self.conn_open)
            self._say("ts push_conn_open %s kind=%s dst_ip=%s sid=0x%x dev=%s open={v:%d a:%d c:%d}" % (
                self._id(), k, local_ip, sid, dev or "-", co["v"], co["a"], co["c"]))
        except Exception:
            pass

    def on_conn_close(self, kind, why):
        try:
            k = kind_of(kind)
            with self.lk:
                self.conn_open[k] = max(0, self.conn_open[k] - 1)
                self.conn_closed[k] += 1
                co = dict(self.conn_open)
            self._say("ts push_conn_close %s kind=%s why=%s open={v:%d a:%d c:%d}" % (
                self._id(), k, why, co["v"], co["a"], co["c"]))
        except Exception:
            pass

    def conns(self):
        with self.lk:
            t = self.conn_total
            return "v:%d a:%d c:%d" % (t["v"], t["a"], t["c"])

    def tick(self, now=None):
        try:
            now = self.clock() if now is None else now
            if now < self.sec_end:
                return
            with self.lk:
                f, b, co = self.s_frames, self.s_bytes, self.conn_open
                gap = (now - self.last_t) if self.last_t is not None else None
                line = ("ts in_sec %s t=%.0f frames={v:%d a:%d c:%d} bytes={v:%d a:%d c:%d} "
                        "first=%s last=%s max_frame_gap_ms=%d since_last_ms=%s "
                        "conns_open={v:%d a:%d c:%d} ctrl_err=%d" % (
                            self._id(), now - self.t_start, f["v"], f["a"], f["c"],
                            b["v"], b["a"], b["c"], self.s_first or "-",
                            self.s_last or "-", round(self.s_gap * 1000),
                            "-" if gap is None else "%d" % round(gap * 1000),
                            co["v"], co["a"], co["c"], self.ctrl_err))
                self.s_frames = {"v": 0, "a": 0, "c": 0, "?": 0}
                self.s_bytes = dict(self.s_frames)
                self.s_first = self.s_last = None
                self.s_gap = 0.0
                self.sec_end = now + 1.0
            self._say(line, event=False)
        except Exception:
            pass

    def summary_text(self):
        with self.lk:
            f, b, t, c = self.frames, self.bytes, self.conn_total, self.conn_closed
            return ("ts in_summary %s frames={v:%d a:%d c:%d} bytes={v:%d a:%d c:%d} "
                    "max_frame_gap_ms=%d conns_total={v:%d a:%d c:%d} "
                    "conns_closed={v:%d a:%d c:%d} ctrl_legs=%d ctrl_err=%d sid_changes=%d" % (
                        self._id(), f["v"], f["a"], f["c"], b["v"], b["a"], b["c"],
                        round(self.gap_max * 1000), t["v"], t["a"], t["c"],
                        c["v"], c["a"], c["c"], self.ctrl_n, self.ctrl_err,
                        self.sid_changes))


def new_input(label, session=None):
    """InputStats for a new Session, or None when diagnostics are off."""
    try:
        if zdiag.enabled():
            return InputStats(label, session)
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------
# Optional output capture
# --------------------------------------------------------------------------
_NAME_RE = re.compile(r"^\d{8}-\d{6}_[A-Za-z0-9._-]+\.ts$")


def capture_seconds():
    return _fenv("ZCAPTURE_SECONDS", 0)


class Capture:
    """First N seconds of the output TS -> a size-capped rotating folder."""

    def __init__(self, f, path, seconds, max_bytes, clock=time.monotonic, emit=None):
        self.f, self.path, self.seconds = f, path, seconds
        self.max_bytes, self.n = max_bytes, 0
        self.clock, self.t0 = clock, clock()
        self.emit = emit or zdiag.dlog
        self.done = False

    @classmethod
    def start(cls, name, clock=time.monotonic, emit=None):
        try:
            secs = capture_seconds()
            if secs <= 0 or not zdiag.enabled():
                return None
            d = os.environ.get("ZCAPTURE_DIR", "/tmp/zapperbridge/captures")
            total = int(_fenv("ZCAPTURE_MAX_MB", 50) * 1048576)
            per = min(total, int(_fenv("ZCAPTURE_FILE_MB", 25) * 1048576))
            os.makedirs(d, mode=0o700, exist_ok=True)
            if shutil.disk_usage(d).free < 1 << 30:
                zdiag.dlog("capture skipped: <1 GB free in %s" % d)
                return None
            cls.prune(d, total - per)
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(name))[:60] or "x"
            path = os.path.join(d, "%s_%s.ts" % (time.strftime("%Y%m%d-%H%M%S"), safe))
            f = open(path, "wb", buffering=256 * 1024)
            (emit or zdiag.dlog)("capture start path=%s seconds=%g max_bytes=%d" % (
                path, secs, per))
            return cls(f, path, secs, per, clock, emit)
        except Exception:
            return None

    @staticmethod
    def prune(d, keep_bytes):
        """Delete the oldest capture files (our own naming only) until the
        folder fits ``keep_bytes``."""
        files = []
        for n in os.listdir(d):
            if _NAME_RE.match(n):
                p = os.path.join(d, n)
                try:
                    st = os.stat(p)
                    files.append((st.st_mtime, st.st_size, p))
                except OSError:
                    pass
        files.sort()
        total = sum(s for _, s, _ in files)
        for _, s, p in files:
            if total <= max(0, keep_bytes):
                break
            try:
                os.remove(p)
                total -= s
            except OSError:
                pass

    def write(self, data):
        if self.done:
            return
        try:
            if self.clock() - self.t0 >= self.seconds or self.n + len(data) > self.max_bytes:
                self.close("seconds" if self.clock() - self.t0 >= self.seconds else "size_cap")
                return
            self.f.write(data)
            self.n += len(data)
        except Exception:
            self.close("error")

    def close(self, why="end"):
        if self.done:
            return
        self.done = True
        try:
            self.f.close()
            (self.emit)("capture done path=%s bytes=%d why=%s" % (self.path, self.n, why))
        except Exception:
            pass


# --------------------------------------------------------------------------
# Glue used by hdhr_emulator
# --------------------------------------------------------------------------
class StreamDiag:
    def __init__(self, sess, seq, guide=None, clock=time.monotonic):
        self.sess, self.seq, self.clock = sess, seq, clock
        label = getattr(sess, "chan_id", "?")
        self.an = (StreamAnalyzer(label, seq, clock=clock) if _on("ZLOG_TS") else None)
        self.zin = getattr(sess, "zin", None)
        if self.zin is not None:
            self.zin.seq = seq
        self.cap = Capture.start("%s" % (guide or label), clock=clock)
        self.label = label
        self.guide = guide

    @classmethod
    def create(cls, sess, seq=None, guide=None, clock=time.monotonic):
        try:
            if not zdiag.enabled():
                return None
            return cls(sess, seq, guide, clock)
        except Exception:
            return None

    def tick(self, now=None):
        try:
            if self.an is not None:
                self.an.tick(now)
            if self.zin is not None:
                self.zin.tick(now)
        except Exception:
            pass

    def on_write(self, data, now=None):
        """After the bytes were written to the client."""
        try:
            if self.an is not None:
                self.an.feed(data, now)
            if self.cap is not None:
                self.cap.write(data)
        except Exception:
            pass

    def finish(self, reason=None):
        try:
            text = None
            if self.an is not None:
                text = self.an.finish(reason)
            if self.zin is not None:
                zdiag.dlog(self.zin.summary_text())
            if self.cap is not None:
                self.cap.close("stream_end")
            jsonl("stream_summary", seq=self.seq, chan=self.label, guide=self.guide,
                  reason=reason, ts_summary=text,
                  input=self.zin.summary_text() if self.zin is not None else None)
        except Exception:
            pass
