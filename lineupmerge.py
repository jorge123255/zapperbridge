"""Merge the lineups of several M1s into ONE HDHomeRun lineup (additive, pure).

Used only by ``hdhr.mode = "merged"``, which is OFF by default and is NOT wired
into the live facade. Nothing here opens a socket, talks to an M1, reads the
clock on its own or touches the filesystem: callers pass in api8 rows, tuner
counts, a clock and a tune callback. See docs/MERGED-LINEUP.md.

Model
-----
* Source = one M1: ``{"name", "chans": [api8 rows], "announced": int | None,
  "leases": int | None}``. Usable tuners = announced - 1 (the M1 keeps one for
  its own HLS window; announced 4 -> 3 usable), further capped by ``leases``
  (config) and never above ``m1config.LEASES_PER_M1``. Unknown ``announced``
  falls back to ``leases`` (default 3).
* Channel identity across M1s = (guide number, upper-cased call letters). The
  same station seen on several M1s becomes ONE row with a candidate list in
  source order. The same guide number carrying a different station gets its own
  row with a free minor number (never silently merged or dropped).
* Guide numbers are stable: pass the ``assignments`` dict from the previous run
  back in (persist it as JSON) and a channel keeps its number when M1s are
  added, removed or reordered. New numbers are only allocated for new stations.
* HLS rows (chan_id starting ``hls:`` / 999.x) and hidden rows are not merged:
  each M1's fixed HLS window is per-M1 and not retunable.
* Routing: ``Router.candidates(key)`` lists up M1s that carry the channel and
  have a free tuner, emptiest first (ties: source order). ``Router.tune`` tries
  them in order and fails over when the tune callback raises or returns falsy.
  A failing M1 is benched for ``bench_sec`` so the next tune does not stall on
  it again. Soft-busy (nothing free anywhere) returns a ``Busy`` result; it never
  falls back to api7 or to taking another client's lease.
"""
import collections
import hashlib
import json
import threading

LEASES_PER_M1 = 3          # mirrors m1config.LEASES_PER_M1 (kept import-free)
ASSIGN_VERSION = 1


def gn_of(c):
    return "%d.%d" % (c["chan_major"], c["chan_minor"])


def _gn_key(gn):
    try:
        return [int(x) for x in gn.split(".")]
    except ValueError:
        return [1 << 30]


def is_hls(c):
    return str(c.get("chan_id", "")).startswith("hls:") or c.get("chan_major") == 999


def ident(c):
    """Cross-M1 identity string for one api8 row."""
    return "%s|%s" % (gn_of(c), str(c.get("call_letters", "")).strip().upper())


def usable_tuners(announced=None, leases=None):
    """announced - 1 (M1 keeps one), capped by config leases and by 3."""
    cap = LEASES_PER_M1 if leases is None else max(0, min(int(leases), LEASES_PER_M1))
    if announced is None:
        return cap
    return max(0, min(int(announced) - 1, cap))


def total_tuner_count(sources, cap=None):
    """Advertised TunerCount: total usable tuners, optionally capped."""
    tot = sum(usable_tuners(s.get("announced"), s.get("leases")) for s in sources)
    return min(tot, cap) if cap else tot


Merged = collections.namedtuple("Merged", "gn name key xmltv_id ident candidates")
Candidate = collections.namedtuple("Candidate", "m1 chan_id chan")


def merge(sources, assignments=None, id_prefix=""):
    """-> (rows sorted by guide number, new_assignments, notes).

    ``assignments``: {ident: guide number} from a previous run (stability).
    ``notes`` lists collisions that needed a renumber.
    """
    prev = dict(assignments or {})
    out_assign, notes = {}, []
    by_ident = collections.OrderedDict()
    for s in sources:
        for c in s["chans"]:
            if c.get("hidden") or is_hls(c):
                continue
            ent = by_ident.setdefault(ident(c), {"chan": c, "cands": []})
            if all(x.m1 != s["name"] for x in ent["cands"]):
                ent["cands"].append(Candidate(s["name"], str(c.get("chan_id", "")), c))
    taken = {}

    def claim(idt, gn):
        taken[gn] = idt
        out_assign[idt] = gn

    # 1) previously assigned identities keep their number
    for idt in by_ident:
        gn = prev.get(idt)
        if gn and gn not in taken:
            claim(idt, gn)
    # numbers promised to identities not currently present stay reserved so
    # a missing M1 coming back gets its old number again
    reserved = {v for k, v in prev.items() if k not in by_ident}
    # 2) new identities: natural number if free (all natural claims first, so
    #    a collision never steals another station's natural number) ...
    late = []
    for idt, ent in by_ident.items():
        if idt in out_assign:
            continue
        gn = gn_of(ent["chan"])
        if gn not in taken and gn not in reserved:
            claim(idt, gn)
        else:
            late.append(idt)
    # 3) ... then collisions get the next free minor of the same major
    for idt in late:
        c = by_ident[idt]["chan"]
        minor = c["chan_minor"]
        while True:
            minor += 1
            alt = "%d.%d" % (c["chan_major"], minor)
            if alt not in taken and alt not in reserved and alt not in {
                    gn_of(e["chan"]) for e in by_ident.values()}:
                break
        claim(idt, alt)
        notes.append("%s collides on %s; assigned %s" % (idt, gn_of(c), alt))
    for k, v in prev.items():  # keep promises for stations not seen this run
        if k not in out_assign and v not in taken:
            out_assign[k] = v
    rows = []
    names = collections.Counter(
        str(e["chan"].get("call_letters", "")).strip() for e in by_ident.values())
    for idt, ent in by_ident.items():
        gn = out_assign[idt]
        call = str(ent["chan"].get("call_letters", "")).strip()
        name = "%s %s" % (call, gn) if names[call] > 1 else call
        xid = "zb-%s%s" % (("%s-" % id_prefix) if id_prefix else "", gn)
        rows.append(Merged(gn, name, "v%s" % gn, xid, idt, tuple(ent["cands"])))
    rows.sort(key=lambda r: _gn_key(r.gn))
    return rows, out_assign, notes


def dump_assignments(assign):
    return json.dumps({"version": ASSIGN_VERSION, "assign": assign},
                      sort_keys=True, indent=1)


def load_assignments(text):
    """Tolerant: garbage or another version -> {} (numbers get re-derived)."""
    try:
        d = json.loads(text)
        if d.get("version") == ASSIGN_VERSION and isinstance(d.get("assign"), dict):
            return {str(k): str(v) for k, v in d["assign"].items()}
    except (TypeError, ValueError, AttributeError):
        pass
    return {}


def lineup_rows(rows, host, ts_port=5004):
    """HDHR /lineup.json rows (core fields only; the facade adds codec/HD)."""
    return [{"GuideNumber": r.gn, "GuideName": r.name, "Tags": "",
             "URL": "http://%s:%d/auto/%s" % (host.split(":")[0], ts_port, r.key),
             "Candidates": [c.m1 for c in r.candidates]} for r in rows]


def lineup_version(rows):
    lines = ["%s|%s" % (r.gn, r.name) for r in rows]
    return int(hashlib.sha1("\n".join(lines).encode()).hexdigest()[:8], 16)


class Busy(Exception):
    """No candidate M1 has a free tuner (soft-busy; clients keep the tuner)."""


class TuneFailed(Exception):
    """Every candidate with a free tuner failed to tune."""

    def __init__(self, tried):
        Exception.__init__(self, "all candidates failed: %s" % ", ".join(
            "%s(%s)" % t for t in tried))
        self.tried = tried


Lease = collections.namedtuple("Lease", "m1 key chan_id id")


class Router:
    """Tracks per-M1 tuner use and picks an M1 for a channel. Thread-safe.

    ``rows``    merged rows from :func:`merge`
    ``sources`` same source dicts (tuner counts)
    ``clock``   callable returning seconds (injectable for tests)
    """

    def __init__(self, rows, sources, clock, bench_sec=30.0, cap=None):
        self._lock = threading.Lock()
        self._clock = clock
        self.bench_sec = float(bench_sec)
        self.cap = cap
        self.set_rows(rows)
        self.total = {s["name"]: usable_tuners(s.get("announced"), s.get("leases"))
                      for s in sources}
        self.order = [s["name"] for s in sources]
        self.up = {n: True for n in self.order}
        self._bench = {}
        self._leases = {}
        self._seq = 0

    def set_rows(self, rows):
        self.rows = {r.key: r for r in rows}

    # -- state ---------------------------------------------------------
    def in_use(self, m1):
        return sum(1 for ls in self._leases.values() if ls.m1 == m1)

    def free(self, m1):
        return max(0, self.total.get(m1, 0) - self.in_use(m1))

    def mark_up(self, m1, up):
        with self._lock:
            self.up[m1] = bool(up)

    def _available(self, m1, now):
        return (self.up.get(m1) and self._bench.get(m1, 0) <= now
                and self.free(m1) > 0)

    def tuner_count(self):
        """Advertised TunerCount now: usable tuners of M1s that are up."""
        with self._lock:
            t = sum(n for m, n in self.total.items() if self.up.get(m))
        return min(t, self.cap) if self.cap else t

    def candidates(self, key):
        """Candidate M1 names for ``key`` with a free tuner, best first."""
        with self._lock:
            return self._cands(key)

    def _cands(self, key):
        r = self.rows.get(key)
        if r is None:
            return []
        now = self._clock()
        c = [x.m1 for x in r.candidates if self._available(x.m1, now)]
        pos = {n: i for i, n in enumerate(self.order)}
        c.sort(key=lambda n: (-self.free(n), pos.get(n, 99)))
        return c

    # -- tune ----------------------------------------------------------
    def tune(self, key, tune_fn):
        """Reserve a tuner on the best M1 and call ``tune_fn(m1, chan_id,
        chan)``. A raise or falsy return fails over to the next candidate.
        Returns a Lease (call :meth:`release` when the stream ends).
        Raises KeyError (unknown channel), Busy, or TuneFailed."""
        if key not in self.rows:
            raise KeyError(key)
        tried, skip = [], set()
        while True:
            with self._lock:
                c = [m for m in self._cands(key) if m not in skip]
                if not c:
                    break
                m1 = c[0]
                cand = next(x for x in self.rows[key].candidates if x.m1 == m1)
                self._seq += 1
                lease = Lease(m1, key, cand.chan_id, self._seq)
                self._leases[lease.id] = lease  # reserve before the slow call
            try:
                ok = tune_fn(m1, cand.chan_id, cand.chan)
                err = None if ok else "no stream"
            except Exception as e:  # noqa: BLE001 - failover on any failure
                ok, err = False, "%s: %s" % (type(e).__name__, e)
            if ok:
                return lease
            with self._lock:
                self._leases.pop(lease.id, None)
                self._bench[m1] = self._clock() + self.bench_sec
            skip.add(m1)
            tried.append((m1, err))
        if tried:
            raise TuneFailed(tried)
        raise Busy(key)

    def release(self, lease):
        with self._lock:
            self._leases.pop(lease.id, None)

    def snapshot(self):
        with self._lock:
            now = self._clock()
            return {m: {"total": self.total[m], "in_use": self.in_use(m),
                        "up": bool(self.up.get(m)),
                        "benched": self._bench.get(m, 0) > now}
                    for m in self.order}
