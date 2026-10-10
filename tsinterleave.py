"""Interleave per-PID TS packet streams by media time (A/V alignment).

The M1 pushes each elementary stream on its own leg, ~2 s of media at a time.
Draining the per-PID queues round-robin by *packet* puts a whole burst of video
ahead of its audio in the byte stream (up to ~2 s of A/V skew). Players that
read the stream straight through (raw-TS proxy -> custom engine) then
see video far ahead of audio and stall/drift.

Interleaver merges the PIDs smallest-timestamp-first (DTS when present, else
PTS; 33-bit wrap safe) at PES-chunk granularity. Rules that keep the output a
valid TS:
  * packets of one PID are NEVER reordered (only interleaved between PIDs), so
    per-PID continuity counters and the PCR sequence (carried on a PID) keep
    their order and values;
  * chunks are cut only on packet boundaries; a PES split across chunks is
    still emitted in order (a PES may be interleaved with other PIDs' packets);
  * delay is bounded: a PID with no data yet holds the others back at most
    `wait` seconds of wall time, and a partial chunk is released after `quiet`.
"""
import collections
import time

_M33 = 1 << 33


def sdiff(a, b):
    """Signed 33-bit timestamp difference a-b in 90 kHz ticks."""
    return ((a - b + (1 << 32)) % _M33) - (1 << 32)


def pes_times(pkt):
    """(pts, dts, stream_id) of a PES start packet (pts/dts may be None), else None."""
    if not (pkt[1] & 0x40):
        return None
    afc = (pkt[3] >> 4) & 3
    if not afc & 1:
        return None
    off = 4
    if afc & 2:
        off = 5 + pkt[4]
    if off + 14 > 188 or pkt[off] or pkt[off + 1] or pkt[off + 2] != 1:
        return None
    fl = pkt[off + 7]
    if not fl & 0x80:
        return (None, None, pkt[off + 3])

    def ts(o):
        t = pkt[o:o + 5]
        return (((t[0] >> 1) & 7) << 30 | t[1] << 22 | (t[2] >> 1) << 15
                | t[3] << 7 | t[4] >> 1)
    pts = ts(off + 9)
    dts = ts(off + 14) if (fl & 0x40) and off + 19 <= 188 else None
    return (pts, dts, pkt[off + 3])


class _Pid:
    __slots__ = ("cur", "key", "ready", "last_key", "step", "last_in", "n")

    def __init__(self):
        self.cur = []
        self.key = None       # key of the chunk being collected
        self.ready = collections.deque()   # (key, [pkts])
        self.last_key = None  # key of the last chunk emitted
        self.step = 0         # EMA of key increments between emitted chunks
        self.last_in = 0.0
        self.n = 0


class Interleaver:
    def __init__(self, clock=time.time, wait=2.5, quiet=0.03, max_chunk=32,
                 active=4.0, audio_lead=0.10):
        self.clock = clock
        self.wait = wait
        self.quiet = quiet
        self.max_chunk = max_chunk
        self.active = active
        #: non-video PES are scheduled this much *earlier* than their PTS: an
        #: audio PES covers ~190 ms starting at its PTS, so sending it on its
        #: PTS would leave the byte stream's audio up to a PES behind the video.
        self.lead = int(audio_lead * 90000)
        self.p = {}
        self.stall = None
        self.expired = False
        self.stats = {"held_timeouts": 0, "in": 0, "out": 0}

    # -- input ---------------------------------------------------------------
    def _close(self, st):
        if st.cur:
            st.ready.append((st.key, st.cur))
            st.cur = []

    def feed(self, pid, pkt, now=None):
        now = self.clock() if now is None else now
        st = self.p.get(pid)
        if st is None:
            st = self.p[pid] = _Pid()
        st.last_in = now
        st.n += 1
        self.stats["in"] += 1
        t = pes_times(pkt)
        if t is not None:
            self._close(st)
            k = t[1] if t[1] is not None else t[0]
            if k is None:
                k = st.key if st.key is not None else st.last_key
            elif not 0xE0 <= t[2] <= 0xEF:
                k = (k - self.lead) % _M33
            st.key = k
        elif len(st.cur) >= self.max_chunk:
            self._close(st)
        st.cur.append(pkt)

    # -- output --------------------------------------------------------------
    def _bound(self, st):
        """Smallest key this PID can still emit, or None if unknown."""
        if st.cur:
            return st.key
        if st.last_key is not None:
            return (st.last_key + st.step) % _M33
        return None

    def pending(self):
        return any(st.ready or st.cur for st in self.p.values())

    def pop(self, now=None, final=False):
        now = self.clock() if now is None else now
        out = []
        for st in self.p.values():
            if st.cur and (final or now - st.last_in >= self.quiet):
                self._close(st)
        while True:
            heads = [(pid, st) for pid, st in sorted(self.p.items()) if st.ready]
            if not heads:
                break
            best = heads[0]
            for h in heads[1:]:
                a, b = h[1].ready[0][0], best[1].ready[0][0]
                # a chunk with no timestamp yet (stream start mid-PES) sorts first
                if (b is not None) and (a is None or sdiff(a, b) < 0):
                    best = h
            bk = best[1].ready[0][0]
            ok = True
            if not final and bk is not None:
                for pid, st in self.p.items():
                    if st.ready or now - st.last_in > self.active:
                        continue
                    bd = self._bound(st)
                    if bd is None or sdiff(bk, bd) > 0:
                        ok = False
                        break
            if not ok:
                if self.stall is None:
                    self.stall = now
                if now - self.stall < self.wait:
                    break
                if not self.expired:
                    self.expired = True
                    self.stats["held_timeouts"] += 1
            else:
                self.stall = None
                self.expired = False
            key, pk = best[1].ready.popleft()
            st = best[1]
            if key is not None:
                if st.last_key is not None:
                    d = sdiff(key, st.last_key)
                    if 0 < d < 90000 * 2:
                        st.step = d if not st.step else (st.step * 3 + d) // 4
                st.last_key = key
            out.extend(pk)
        if not any(st.ready for st in self.p.values()):
            self.stall = None
            self.expired = False
        self.stats["out"] += len(out)
        return out
