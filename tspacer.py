"""Constant-rate (PCR-paced) release of an MPEG-TS byte stream.

A real HDHomeRun streams at a steady rate. The M1 pushes ~2 s bursts; passing
them straight through gives ~0.4 s of 12-24 Mbps then ~1.6 s of silence, which
a client with a straight-through proxy and a software decoder cannot ride out.

Pacer sits between the mux (Session.packets) and the client socket:
  * packets are stamped with a media time by interpolating between PCRs
    (a packet's due time = PCR-derived media time, so output rate == PCR rate);
  * output starts after a start jitter buffer (`prebuf` seconds of wall time
    since the first packet, and that much media buffered; never later than
    `max_start`);
  * when the buffer runs dry (source stall) it re-buffers like at start;
  * if the buffer exceeds `max_buf` seconds / `max_bytes` it skips ahead to a
    random-access point (never an unbounded latency); a mild +3 % speed-up
    drains slow clock drift before it gets that far;
  * with no PCR it falls back to the measured average input rate.

Pure logic: no sockets, injectable clock, so it is unit-testable at any speed.
"""
import collections
import threading
import time

PKT = 188
MAX_WRITE_PKTS = 348          # 348 * 188 = 65,424 B (<= 64 KiB)
PCR_WRAP = (1 << 33) * 300 / 27e6


def pcr_of(pkt):
    """PCR in seconds, or None."""
    if pkt[3] & 0x20 and pkt[4] >= 7 and pkt[5] & 0x10:
        base = (pkt[6] << 25 | pkt[7] << 17 | pkt[8] << 9 | pkt[9] << 1
                | pkt[10] >> 7)
        ext = ((pkt[10] & 1) << 8) | pkt[11]
        return (base * 300 + ext) / 27e6
    return None


def is_rap(pkt):
    """True for a video PES start that carries a random-access point:
    MPEG-2 sequence header, H.264 SPS, or the adaptation-field RAI flag."""
    if not (pkt[1] & 0x40):
        return False
    afc = (pkt[3] >> 4) & 3
    if not afc & 1:
        return False
    off = 4
    rai = False
    if afc & 2:
        rai = pkt[4] > 0 and bool(pkt[5] & 0x40)
        off = 5 + pkt[4]
    if off + 9 > PKT or pkt[off] or pkt[off + 1] or pkt[off + 2] != 1:
        return False
    if not 0xE0 <= pkt[off + 3] <= 0xEF:
        return False
    if rai:
        return True
    es = pkt[off + 9 + pkt[off + 8]:]
    i = es.find(b"\x00\x00\x01")
    while 0 <= i < len(es) - 3 and i < 40:
        t = es[i + 3]
        if t == 0xB3 or (t & 0x1F) == 7 and t & 0x80 == 0:
            return True
        i = es.find(b"\x00\x00\x01", i + 3)
    return False


class Pacer:
    def __init__(self, clock=time.monotonic, prebuf=0.8, max_buf=6.0,
                 max_bytes=12 * 1024 * 1024, max_start=1.5, ahead=0.03,
                 speedup=1.03, enabled_slew=True):
        self.clock = clock
        self.prebuf, self.max_buf, self.max_bytes = prebuf, max_buf, max_bytes
        self.max_start, self.ahead, self.speedup = max_start, ahead, speedup
        self.slew = enabled_slew
        self.cv = threading.Condition()
        self.closed = False
        self.buf = collections.deque()        # (media_s, pkt, is_rap)
        self.buf_bytes = 0
        self.raps = 0
        self.pend = []                        # packets since the last PCR
        self.pend_rap = []
        self.state = "idle"                   # idle|starting|run|starved
        self.first_in = None
        self.pcr_prev = None                  # last PCR (s)
        self.media = None                     # media time of last stamped pkt
        self.pcr_pid = None
        self.rate_bps = None                  # input rate estimate (fallback)
        self.bytes_in = 0
        self.a_wall = 0.0                     # anchor: wall time ...
        self.a_media = 0.0                    # ... at which this media plays
        self.speed = 1.0
        self._now = 0.0
        self.dry = False              # buffer fully drained while running
        self.pend_t0 = None           # when the oldest un-stamped packet came in
        self.keepalive = 1.0          # PSI-only lead-in is passed through after this
        self.late_tol = 0.25
        self.stats = {"bytes_in": 0, "bytes_out": 0, "start_delay": None,
                      "skips": 0, "skipped_pkts": 0, "rebuffers": 0,
                      "max_level": 0.0, "pcr_resets": 0, "writes": 0,
                      "mode": "pcr", "slewed": 0}

    # -- input -----------------------------------------------------------------
    def feed(self, data, now=None):
        """Feed whole 188-byte packets (bytes, any multiple)."""
        now = self.clock() if now is None else now
        with self.cv:
            if self.closed:
                return
            for i in range(0, len(data) - PKT + 1, PKT):
                self._in(data[i:i + PKT], now)
            self.stats["bytes_in"] += len(data)
            self._overflow(now)
            self.cv.notify_all()

    def close(self):
        with self.cv:
            self.closed = True
            self._flush_pend(None)
            self.cv.notify_all()

    def _in(self, pkt, now):
        self._now = now
        self.bytes_in += PKT
        if not self.pend:
            self.pend_t0 = now
        self.pend.append(pkt)
        self.pend_rap.append(is_rap(pkt))
        c = pcr_of(pkt)
        if c is not None and (self.pcr_pid is None
                              or ((pkt[1] & 0x1F) << 8 | pkt[2]) == self.pcr_pid):
            self.pcr_pid = (pkt[1] & 0x1F) << 8 | pkt[2]
            self._stamp(c)
        elif len(self.pend) >= 2500:
            # ~0.4 s at 6.8 Mbps and still no PCR: not a PCR stream (or PCR
            # is sparse) -> stamp from the measured/last-known rate
            self._flush_pend(now)

    def _data(self):
        """Stamped data is about to be queued: (re)start the jitter buffer."""
        if self.first_in is None:
            self.first_in = self._now
            self.state = "starting"
        elif self.state == "starved" or (self.state == "run" and self.dry
                                          and not self.buf):
            # source underrun: re-buffer like at start instead of dumping the
            # next burst at once
            self.state = "starting"
            self.first_in = self._now
            self.stats["rebuffers"] += 1
        self.dry = False

    def _est_rate(self, now):
        el = max(0.5, now - self.first_in) if (now and self.first_in) else 1.0
        r = self.bytes_in * 8 / el if self.rate_bps is None else self.rate_bps
        return max(r, 5e5)

    def _flush_pend(self, now):
        """No PCR to anchor on: stamp at the measured/last-known rate."""
        if not self.pend:
            return
        self._data()
        if self.pcr_prev is None:
            self.stats["mode"] = "rate"
        rate = self.rate_bps if self.rate_bps else self._est_rate(now)
        base = self.media if self.media is not None else 0.0
        dt = PKT * 8 / rate
        for k, (p, r) in enumerate(zip(self.pend, self.pend_rap)):
            self._push(base + dt * (k + 1), p, r)
        self.media = base + dt * len(self.pend)
        self.pend, self.pend_rap = [], []

    def _stamp(self, c):
        self._data()
        n = len(self.pend)
        if self.pcr_prev is None:
            base, delta = 0.0, 0.0
            self.media = 0.0
        else:
            delta = (c - self.pcr_prev) % PCR_WRAP
            if delta > 1.0:        # PCR jump/reset: keep our timeline moving
                self.stats["pcr_resets"] += 1
                delta = n * PKT * 8 / (self.rate_bps or 2e7)
            base = self.media
            if delta > 0:
                self.rate_bps = n * PKT * 8 / delta if delta > 0.005 else self.rate_bps
        for k, (p, r) in enumerate(zip(self.pend, self.pend_rap)):
            self._push(base + delta * (k + 1) / n, p, r)
        self.media = base + delta
        self.pcr_prev = c
        self.pend, self.pend_rap = [], []

    def _push(self, m, pkt, rap):
        self.buf.append((m, pkt, rap))
        self.buf_bytes += PKT
        if rap:
            self.raps += 1

    # -- buffer control -------------------------------------------------------
    def level(self):
        """Seconds of media queued."""
        if not self.buf:
            return 0.0
        return self.buf[-1][0] - self.buf[0][0]

    def _overflow(self, now):
        lv = self.level()
        if lv > self.stats["max_level"]:
            self.stats["max_level"] = lv
        if lv <= self.max_buf and self.buf_bytes <= self.max_bytes:
            return
        # skip ahead to a random-access point leaving <= half the cap
        tgt_s, tgt_b = self.max_buf / 2, self.max_bytes / 2
        end_m = self.buf[-1][0]
        cut = None
        for i, (m, p, r) in enumerate(self.buf):
            if r and end_m - m <= tgt_s and (len(self.buf) - i) * PKT <= tgt_b:
                cut = i
                break
        if cut is None:        # nothing small enough: newest RAP, else give up
            for i in range(len(self.buf) - 1, -1, -1):
                if self.buf[i][2]:
                    cut = i
                    break
        if cut is None:
            if lv > 2 * self.max_buf or self.buf_bytes > 2 * self.max_bytes:
                cut = len(self.buf) - 1     # hard cap even without a RAP
            else:
                return
        for _ in range(cut):
            _, _, r = self.buf.popleft()
            self.raps -= 1 if r else 0
        self.buf_bytes = len(self.buf) * PKT
        self.stats["skips"] += 1
        self.stats["skipped_pkts"] += cut
        if self.state == "run" and self.buf:
            self.a_wall, self.a_media = now, self.buf[0][0]

    # -- output ------------------------------------------------------------------
    def _due(self, m):
        return self.a_wall + (m - self.a_media) / self.speed

    def get(self, now=None):
        """-> (bytes|None, wait_s|None, done). wait_s is how long to sleep."""
        now = self.clock() if now is None else now
        with self.cv:
            return self._get(now)

    def _get(self, now):
        if self.closed and self.pend:
            self._flush_pend(None)
        if self.state == "idle":
            if (self.pend and not self.closed and len(self.pend) <= 300
                    and now - self.pend_t0 >= self.keepalive):
                # nothing but PAT/PMT so far (the GOP gate is still waiting):
                # keep bytes flowing so the client does not time out
                out = b"".join(self.pend)
                self.pend, self.pend_rap = [], []
                self.stats["bytes_out"] += len(out)
                self.stats["keepalive"] = self.stats.get("keepalive", 0) + 1
                return out, 0, False
            return (None, (None if not self.pend else 0.25),
                    self.closed and not self.buf)
        if self.state == "starting":
            have = self.level()
            t0 = self.first_in
            ready = (now - t0 >= self.prebuf and have >= self.prebuf * 0.9) \
                or now - t0 >= self.max_start
            if self.closed and self.buf:
                ready = True            # short stream: don't hold it back
            if not ready:
                if not self.buf and self.closed:
                    return None, 0, True
                return None, max(0.005, t0 + self.prebuf - now), False
            if self.stats["start_delay"] is None:
                self.stats["start_delay"] = now - t0
            if self.buf:
                self.a_wall, self.a_media = now, self.buf[0][0]
            self.speed = 1.0
            self.state = "run"
        if self.closed:
            # stream ended: release everything left immediately
            n = min(len(self.buf), MAX_WRITE_PKTS)
            return self._take(n), 0, False if self.buf else True
        if self.slew:
            lv = self.level()
            want = self.speedup if lv > self.max_buf * 0.75 else (
                1.0 if lv < self.max_buf * 0.5 else self.speed)
            if want != self.speed and self.buf:
                self.a_media = self.a_media + (now - self.a_wall) * self.speed
                self.a_wall = now
                self.speed = want
                self.stats["slewed"] += 1
        buf = self.buf
        if buf and now - self._due(buf[0][0]) > self.late_tol:
            # the client (not the source) fell behind: resume at the PCR rate
            # from here instead of bursting the backlog out
            self.a_wall, self.a_media = now, buf[0][0]
            self.stats["reanchors"] = self.stats.get("reanchors", 0) + 1
        lim = now + self.ahead
        n = 0
        while n < len(buf) and n < MAX_WRITE_PKTS and self._due(buf[n][0]) <= lim:
            n += 1
        if n:
            return self._take(n), 0, False
        if not buf:
            self.dry = True
            if self.pend:
                return None, 0.02, False
            return None, None, False
        return None, max(0.001, self._due(buf[0][0]) - now), False

    def _take(self, n):
        out = bytearray()
        for _ in range(n):
            _, p, r = self.buf.popleft()
            out += p
            if r:
                self.raps -= 1
        self.buf_bytes = len(self.buf) * PKT
        self.stats["bytes_out"] += len(out)
        self.stats["writes"] += 1
        return bytes(out)

    def wait(self, timeout):
        with self.cv:
            self.cv.wait(timeout)


def resolve_start_buf(raw, prebuf=0.8, max_start=1.5, max_buf=6.0):
    """Opt-in deeper startup prebuffer (env ``ZSTART_BUF_SEC``).

    ``raw`` unset/empty -> (prebuf, max_start, None): nothing changes.
    Otherwise the start jitter buffer becomes ``raw`` seconds of media (and wall
    time since the first data), clamped to 0.1 .. max(prebuf, max_buf / 2) so
    the buffer stays well under the overflow cap, and ``max_start`` is raised to
    at least ``raw + 0.7`` so the "start no later than max_start" safety net does
    not silently cut the deeper buffer short. A bad value (not a number, <= 0,
    nan/inf) is ignored with a note. Returns (prebuf, max_start, note).

    Cost: first byte to the client moves about (value - prebuf) later, and
    the same depth is used when the pacer re-buffers after a source underrun.
    """
    if raw is None or str(raw).strip() == "":
        return prebuf, max_start, None
    try:
        v = float(str(raw).strip())
    except ValueError:
        v = float("nan")
    if not (v == v) or v <= 0 or v == float("inf"):
        return prebuf, max_start, "ZSTART_BUF_SEC=%r ignored (need a number > 0)" % (raw,)
    hi = max(prebuf, max_buf / 2.0)
    note = None
    if v > hi:
        note = "ZSTART_BUF_SEC=%g clamped to %g" % (v, hi)
        v = hi
    v = max(v, 0.1)
    return v, max(max_start, v + 0.7), note
