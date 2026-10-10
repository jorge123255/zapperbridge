"""Diagnostic logging helpers (log-only; no effect on timing, locking or protocol).

``ZLOG_DIAG`` (default ``1``) turns the extra diagnostics on; ``0`` / ``false``
/ ``no`` / ``off`` restores the historical log lines exactly (no timestamps, no
extra lines). Every helper swallows its own errors so logging can never break
a stream, a heartbeat or a BRP call.

This module deliberately reads no M1 / network settings, so it is safe to
import from hdhr_emulator.py before the M1 address is resolved.
"""
import itertools, os, re, sys, threading, time
from datetime import datetime

_OFF = ("0", "false", "no", "off")


def enabled():
    """True unless ZLOG_DIAG is set to 0/false/no/off. One dict lookup."""
    return os.environ.get("ZLOG_DIAG", "1").strip().lower() not in _OFF


def ts():
    """Local wall-clock ISO-8601 with ms and UTC offset (e.g. ...-05:00)."""
    try:
        return datetime.now().astimezone().isoformat(timespec="milliseconds")
    except Exception:
        return time.strftime("%Y-%m-%dT%H:%M:%S")


def stamp(msg):
    """``[ts] msg`` when diagnostics are on, else ``msg`` unchanged."""
    return "[%s] %s" % (ts(), msg) if enabled() else msg


# ---- in-memory ring of recent stream lines (read-only web UI) -------------
# Off until ring_enable() is called (webui does that only when ZUI=readonly),
# so the default process keeps no extra state. It records a copy of selected
# lines that are printed anyway; it never changes what is printed.
_RING = {"on": False, "buf": None, "seq": 0}
_RING_LK = threading.Lock()
_RING_KEEP = re.compile(
    r"^(stream (request|open|close)|wgap (summary|rebuffer|write_gap|input_gap)"
    r"|brp stream|tune |no lineup)")


def ring_enable(maxlen=500):
    """Start keeping the last ``maxlen`` stream/diag lines in memory."""
    import collections
    with _RING_LK:
        _RING["buf"] = collections.deque(maxlen=max(10, int(maxlen)))
        _RING["on"] = True


def ring_disable():
    with _RING_LK:
        _RING["on"] = False
        _RING["buf"] = None


def _ring_add(stamp_ts, msg):
    if not _RING["on"]:
        return
    try:
        m = str(msg)
        if not _RING_KEEP.match(m):
            return
        with _RING_LK:
            if _RING["buf"] is not None:
                _RING["seq"] += 1
                _RING["buf"].append({"seq": _RING["seq"], "ts": stamp_ts,
                                     "line": m[:600]})
    except Exception:
        pass


def ring_lines(limit=200, since=None):
    """Newest-last list of {seq, ts, line}; [] when the ring is off.
    ``since`` (a seq cursor) keeps only lines newer than it."""
    with _RING_LK:
        buf = list(_RING["buf"]) if _RING["buf"] is not None else []
    if since is not None:
        buf = [e for e in buf if e.get("seq", 0) > since]
    return buf[-max(1, int(limit)):]


def ring_seq():
    """Cursor of the newest ring line (0 when none / off). Cheap."""
    return _RING["seq"] if _RING["on"] else 0


def dlog(msg):
    """Print a diagnostic line (only when enabled), timestamped, flushed."""
    if not enabled():
        return
    try:
        t = ts()
        print("[%s] %s" % (t, msg), file=sys.stdout, flush=True)
        _ring_add(t, msg)
    except Exception:
        pass


def tlog(msg):
    """Print an existing-style log line; timestamped only when enabled."""
    try:
        print(stamp(msg), flush=True)
        if _RING["on"]:
            _ring_add(ts(), msg)
    except Exception:
        pass


_seq = itertools.count(1)


def next_seq():
    """Process-wide stream sequence id (1, 2, 3 ...) used to correlate the
    per-stream log lines with a client's own logs."""
    return next(_seq)


_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def clean(value, limit=120):
    """Make an untrusted header/path value safe for one log line."""
    try:
        s = _CTRL.sub("?", str(value if value is not None else "-"))
    except Exception:
        return "?"
    s = s.strip() or "-"
    return s if len(s) <= limit else s[:limit] + "..."


def safe_path(raw):
    """Request path with the query string and fragment dropped (tokens live
    there), sanitised for logging."""
    try:
        return clean(str(raw).split("?", 1)[0].split("#", 1)[0], 160)
    except Exception:
        return "?"


class RateLimit:
    """Allow at most one event per ``interval`` seconds per key.

    ``allow(key)`` returns ``(ok, suppressed)``: when ok, ``suppressed`` is the
    number of events dropped for that key since the last allowed one."""

    def __init__(self, interval=10.0, clock=time.monotonic):
        self.interval = interval
        self.clock = clock
        self._lk = threading.Lock()
        self._next = {}
        self._dropped = {}

    def allow(self, key, interval=None):
        iv = self.interval if interval is None else interval
        now = self.clock()
        with self._lk:
            if now >= self._next.get(key, 0.0):
                self._next[key] = now + iv
                return True, self._dropped.pop(key, 0)
            self._dropped[key] = self._dropped.get(key, 0) + 1
            return False, 0


class WriteGapMonitor:
    """Per-stream write/input timing diagnostics for the paced writer.

    Log-only: it never sleeps, never takes the pacer lock and never touches the
    socket. All entry points swallow their own errors. ``clock`` and ``emit``
    are injectable for tests. Thread model: ``on_input`` is called from the
    reader thread, ``on_write`` / ``tick`` / ``on_rebuffer`` / ``finish`` from
    the writer thread; one small private lock guards the counters.

    Definitions (seconds on the monotonic clock):
      * write gap  = completion of write N minus completion of write N-1, i.e.
        what the client sees between two chunks (includes send blocking);
      * block time = how long one ``wfile.write`` call blocked on the socket;
      * input gap  = time between two consecutive reads from the M1 mux.
    Cause tags on a gap line: ``client_backpressure`` (a write blocked
    >= gap threshold), ``rebuffer`` (pacer re-buffered), ``upstream_gap`` (an
    input gap >threshold happened inside the window), ``buffer_empty`` (pacer
    had nothing queued) or ``pacer_wait`` (none of those).
    """

    HIST_MAX_MS = 5000

    def __init__(self, label, clock=time.monotonic, emit=None,
                 gap_thresh=0.1, big_thresh=0.5, max_gap_lines_per_s=20):
        self.label = label
        self.summary = None   # dict set by finish() for the request log
        self.clock = clock
        self.emit = emit or dlog
        self.gap_thresh, self.big_thresh = gap_thresh, big_thresh
        self.max_lines = max_gap_lines_per_s
        self._lk = threading.Lock()
        self.t_start = clock()
        self._sec_end = self.t_start + 1.0
        self._sec_n = 0
        self._reset_sec()
        # lifetime
        self.last_w_end = None
        self.last_in = None
        self.last_igap_t = None
        self.last_igap = 0.0
        self.rebuf_seen = 0
        self.rebuf_flag = False
        self._wh = [0] * (self.HIST_MAX_MS + 2)
        self._ih = [0] * (self.HIST_MAX_MS + 2)
        self.w_n = self.w_max = self.w_gt = self.w_big = 0
        self.i_n = self.i_max = self.i_gt = self.i_big = 0
        self.w_max = self.i_max = 0.0
        self.blk_max_total = 0.0
        self.lines_dropped_total = 0

    def _reset_sec(self):
        self.s_bytes = self.s_writes = 0
        self.s_wgap = 0.0
        self.s_blk = 0.0
        self.s_in_bytes = self.s_in_reads = 0
        self.s_igap = 0.0
        self.s_lines = 0
        self.s_dropped = 0

    def _say(self, msg):
        try:
            self.emit(msg)
        except Exception:
            pass

    def _gap_line(self, msg):
        """Immediate gap line, capped per second (caller holds no lock)."""
        with self._lk:
            if self.s_lines >= self.max_lines:
                self.s_dropped += 1
                self.lines_dropped_total += 1
                return
            self.s_lines += 1
        self._say(msg)

    @staticmethod
    def _bump(hist, gap):
        ms = int(gap * 1000.0)
        hist[ms if ms <= WriteGapMonitor.HIST_MAX_MS
             else WriteGapMonitor.HIST_MAX_MS + 1] += 1

    @staticmethod
    def _pct(hist, n, frac):
        if n <= 0:
            return 0
        want, acc = max(1, int(round(n * frac + 0.4999))), 0
        for ms, c in enumerate(hist):
            acc += c
            if acc >= want:
                return ms
        return len(hist) - 1

    # -- reader thread -----------------------------------------------------
    def on_input(self, nbytes, now=None):
        try:
            now = self.clock() if now is None else now
            gap = None
            with self._lk:
                if self.last_in is not None:
                    gap = now - self.last_in
                    self._bump(self._ih, gap)
                    self.i_n += 1
                    if gap > self.i_max:
                        self.i_max = gap
                    if gap > self.s_igap:
                        self.s_igap = gap
                    if gap > self.gap_thresh:
                        self.i_gt += 1
                        self.last_igap_t, self.last_igap = now, gap
                    if gap > self.big_thresh:
                        self.i_big += 1
                self.last_in = now
                self.s_in_bytes += nbytes
                self.s_in_reads += 1
            if gap is not None and gap > self.gap_thresh:
                self._gap_line("wgap input_gap stream=%s gap_ms=%d cause=upstream_gap" % (
                    self.label, round(gap * 1000)))
        except Exception:
            pass

    # -- writer thread -----------------------------------------------------
    def on_write(self, nbytes, t0, t1, level, dry=False):
        """One finished socket write: began ``t0``, returned ``t1``."""
        try:
            blk = max(0.0, t1 - t0)
            gap = None
            cause = None
            with self._lk:
                if self.last_w_end is not None:
                    gap = t1 - self.last_w_end
                    self._bump(self._wh, gap)
                    self.w_n += 1
                    if gap > self.w_max:
                        self.w_max = gap
                    if gap > self.s_wgap:
                        self.s_wgap = gap
                    if gap > self.gap_thresh:
                        self.w_gt += 1
                        if blk >= self.gap_thresh:
                            cause = "client_backpressure"
                        elif self.rebuf_flag:
                            cause = "rebuffer"
                        elif (self.last_igap_t is not None
                              and self.last_igap_t > self.last_w_end):
                            cause = "upstream_gap"
                        elif dry or level <= 0.02:
                            cause = "buffer_empty"
                        else:
                            cause = "pacer_wait"
                    if gap > self.big_thresh:
                        self.w_big += 1
                self.rebuf_flag = False
                self.last_w_end = t1
                self.s_bytes += nbytes
                self.s_writes += 1
                if blk > self.s_blk:
                    self.s_blk = blk
                if blk > self.blk_max_total:
                    self.blk_max_total = blk
            if cause:
                self._gap_line(
                    "wgap write_gap stream=%s gap_ms=%d block_ms=%d level=%.2fs "
                    "bytes=%d cause=%s" % (self.label, round(gap * 1000),
                                           round(blk * 1000), level, nbytes, cause))
        except Exception:
            pass

    def on_rebuffer(self, count, level, now=None):
        """The pacer's rebuffer counter went up (logged with a timestamp)."""
        try:
            with self._lk:
                self.rebuf_flag = True
            self._say("wgap rebuffer stream=%s n=%d level=%.2fs" % (
                self.label, count, level))
        except Exception:
            pass

    def tick(self, level, rebuffers=None, now=None):
        """Call often (>= every 0.25 s) while live; emits the per-second line
        and notices rebuffer-counter increases."""
        try:
            now = self.clock() if now is None else now
            if rebuffers is not None and rebuffers > self.rebuf_seen:
                self.rebuf_seen = rebuffers
                self.on_rebuffer(rebuffers, level, now)
            if now < self._sec_end:
                return
            with self._lk:
                line = ("wgap sec stream=%s t=%.0f out_bytes=%d writes=%d "
                        "max_write_gap_ms=%d max_block_ms=%d level=%.2fs "
                        "in_bytes=%d in_reads=%d max_in_gap_ms=%d gap_lines_dropped=%d" % (
                            self.label, now - self.t_start, self.s_bytes,
                            self.s_writes, round(self.s_wgap * 1000),
                            round(self.s_blk * 1000), level, self.s_in_bytes,
                            self.s_in_reads, round(self.s_igap * 1000),
                            self.s_dropped))
                self._reset_sec()
                self._sec_end = now + 1.0
            self._say(line)
        except Exception:
            pass

    def finish(self, reason=None):
        """Stream-end summary line."""
        try:
            with self._lk:
                msg = ("wgap summary stream=%s dur=%.1fs writes=%d "
                       "write_gap_ms p50=%d p95=%d max=%d gt100=%d gt500=%d "
                       "max_block_ms=%d input_reads=%d input_gap_ms p50=%d "
                       "p95=%d max=%d gt100=%d gt500=%d gap_lines_dropped=%d "
                       "close=%s" % (
                           self.label, self.clock() - self.t_start, self.w_n + 1
                           if self.last_w_end is not None else 0,
                           self._pct(self._wh, self.w_n, 0.5),
                           self._pct(self._wh, self.w_n, 0.95),
                           round(self.w_max * 1000), self.w_gt, self.w_big,
                           round(self.blk_max_total * 1000), self.i_n + 1
                           if self.last_in is not None else 0,
                           self._pct(self._ih, self.i_n, 0.5),
                           self._pct(self._ih, self.i_n, 0.95),
                           round(self.i_max * 1000), self.i_gt, self.i_big,
                           self.lines_dropped_total, reason or "-"))
            self.summary = {
                "dur_sec": round(self.clock() - self.t_start, 1),
                "write_gap_ms_p50": self._pct(self._wh, self.w_n, 0.5),
                "write_gap_ms_p95": self._pct(self._wh, self.w_n, 0.95),
                "write_gap_ms_max": round(self.w_max * 1000),
                "write_gap_gt100": self.w_gt, "write_gap_gt500": self.w_big,
                "max_block_ms": round(self.blk_max_total * 1000),
                "input_gap_ms_max": round(self.i_max * 1000),
                "input_gap_gt100": self.i_gt, "input_gap_gt500": self.i_big,
                "close": reason or "-"}
            self._say(msg)
        except Exception:
            pass
