"""Diagnostic logging (ZLOG_DIAG): log-only, behaviour-neutral.

Covers zdiag helpers, Tuner heartbeat diagnostics, Tuner.call lock-wait
diagnostics, and stream open/close reasons in hdhr_emulator. Offline: no M1,
no sockets to the M1. Runs under unittest and pytest.
"""
import contextlib
import io
import os
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import zdiag
import brptune
import hdhr_emulator as h


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@contextlib.contextmanager
def diag_env(value):
    old = os.environ.get("ZLOG_DIAG")
    if value is None:
        os.environ.pop("ZLOG_DIAG", None)
    else:
        os.environ["ZLOG_DIAG"] = value
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("ZLOG_DIAG", None)
        else:
            os.environ["ZLOG_DIAG"] = old


def capture(fn, *a, **k):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        res = fn(*a, **k)
    return res, buf.getvalue()


class Sess:
    alive = True
    chan_id = "0x1130000000002001"
    qdrops = 0
    qdrop_bursts = 0
    qhigh = 0
    queue_wait = 0.0
    nbytes = 0

    def __init__(self, pkts=(), exc=None):
        self._pkts, self._exc = list(pkts), exc

    def packets(self):
        for p in self._pkts:
            yield p
        if self._exc is not None:
            raise self._exc


def make_tuner(devs=("devA",), clock=None):
    t = brptune.Tuner.__new__(brptune.Tuner)
    t.lock = threading.RLock()
    t.dev = devs[0]
    t.seq = [500]
    t.slots = []
    for i, d in enumerate(devs):
        s = brptune.Slot(d)
        s.inst = 3 + i
        s.sess = Sess()
        t.slots.append(s)
    t._diag_rl = zdiag.RateLimit(5.0, clock=clock or time.monotonic)
    return t


class ZdiagHelpers(unittest.TestCase):
    def test_enabled_default_on_and_off_values(self):
        with diag_env(None):
            self.assertTrue(zdiag.enabled())
        for v in ("0", "false", "No", "OFF"):
            with diag_env(v):
                self.assertFalse(zdiag.enabled(), v)
        with diag_env("1"):
            self.assertTrue(zdiag.enabled())

    def test_ts_is_local_iso_with_offset(self):
        t = zdiag.ts()
        self.assertRegex(t, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d$")

    def test_stamp_and_off_is_unchanged(self):
        with diag_env("1"):
            self.assertRegex(zdiag.stamp("x y"), r"^\[\d{4}-.*\] x y$")
        with diag_env("0"):
            self.assertEqual(zdiag.stamp("x y"), "x y")
            _, out = capture(zdiag.dlog, "hidden")
            self.assertEqual(out, "")
            _, out = capture(zdiag.tlog, "legacy line")
            self.assertEqual(out, "legacy line\n")

    def test_clean_and_safe_path(self):
        self.assertEqual(zdiag.clean("a\r\nb\x00c"), "a??b?c")
        self.assertEqual(zdiag.clean(None), "-")
        self.assertTrue(zdiag.clean("x" * 500).endswith("..."))
        self.assertEqual(zdiag.safe_path("/auto/v2.1?token=SECRET#f"), "/auto/v2.1")

    def test_ratelimit_per_key_and_suppressed_count(self):
        c = Clock()
        rl = zdiag.RateLimit(5.0, clock=c)
        self.assertEqual(rl.allow("a"), (True, 0))
        self.assertEqual(rl.allow("a"), (False, 0))
        self.assertEqual(rl.allow("a"), (False, 0))
        self.assertEqual(rl.allow("b"), (True, 0))
        c.t += 5.1
        self.assertEqual(rl.allow("a"), (True, 2))
        self.assertEqual(rl.allow("a"), (False, 0))


class HeartbeatDiag(unittest.TestCase):
    def setUp(self):
        self.clk = Clock()
        self._m = brptune._mono
        brptune._mono = self.clk
        self.t = make_tuner(clock=self.clk)
        self.state = {}
        self.calls = []

    def tearDown(self):
        brptune._mono = self._m

    def run_pass(self, reply=None, dt=0.0, exc=None, advance=0.0):
        def fake(api, **kw):
            self.calls.append((api, kw))
            self.clk.t += dt
            if exc:
                raise exc
            return reply
        self.t.call = fake
        self.clk.t += advance
        with diag_env("1"):
            return capture(self.t._hb_pass, self.state)[1]

    def test_ok_beat_is_silent_and_args_unchanged(self):
        out = self.run_pass({"err_name": "SUCCESS"}, dt=0.05)
        self.assertEqual(out, "")
        api, kw = self.calls[0]
        self.assertEqual(api, 11)
        self.assertEqual((kw["a0"], kw["dev"], kw["timeout"]), (3, "devA", 3.0))
        self.assertIn("a1", kw)

    def test_slow_beat_logged(self):
        out = self.run_pass({"err_name": "SUCCESS"}, dt=1.5)
        self.assertIn("hb dev=devA", out)
        self.assertIn("slow dt=1.50s", out)
        self.assertRegex(out, r"^\[\d{4}-")

    def test_none_and_non_success_logged(self):
        out = self.run_pass(None)
        self.assertIn("no reply (None)", out)
        self.clk.t += 6
        out = self.run_pass({"err_name": "BUSY"})
        self.assertIn("reply err=BUSY", out)

    def test_exception_logged_not_swallowed_silently(self):
        out = self.run_pass(exc=OSError("boom"))
        self.assertIn("raised OSError('boom')", out)

    def test_gap_over_4s_logged_and_recovery_reported(self):
        self.run_pass({"err_name": "SUCCESS"})
        self.clk.t += 6
        out = self.run_pass(None)
        self.assertIn("gap=6.0s since last ok beat (still failing)", out)
        self.clk.t += 6
        out = self.run_pass({"err_name": "SUCCESS"})
        self.assertIn("gap=12.0s since last ok beat", out)
        self.assertNotIn("still failing", out)
        out = self.run_pass({"err_name": "SUCCESS"}, advance=1.0)
        self.assertEqual(out, "")

    def test_gap_not_flagged_at_or_below_4s(self):
        self.run_pass({"err_name": "SUCCESS"})
        out = self.run_pass({"err_name": "SUCCESS"}, advance=3.9)
        self.assertEqual(out, "")

    def test_rate_limited_with_suppressed_count(self):
        first = self.run_pass(None)
        self.assertIn("hb dev=devA", first)
        self.assertEqual(self.run_pass(None, advance=1.0), "")
        self.assertEqual(self.run_pass(None, advance=1.0), "")
        out = self.run_pass(None, advance=5.0)
        self.assertIn("(+2 suppressed)", out)

    def test_new_session_resets_gap(self):
        self.run_pass({"err_name": "SUCCESS"})
        self.t.slots[0].sess = Sess()           # new stream on the same dev
        out = self.run_pass({"err_name": "SUCCESS"}, advance=30.0)
        self.assertEqual(out, "")

    def test_idle_slots_not_beaten(self):
        self.t.slots[0].sess = None
        self.run_pass({"err_name": "SUCCESS"})
        self.assertEqual(self.calls, [])

    def test_diag_off_swallows_exceptions_and_logs_nothing(self):
        def fake(api, **kw):
            self.calls.append(api)
            raise OSError("boom")
        self.t.call = fake
        with diag_env("0"):
            _, out = capture(self.t._hb_pass, self.state)
        self.assertEqual(out, "")
        self.assertEqual(self.calls, [11])
        self.assertEqual(self.state, {})


class CallLockDiag(unittest.TestCase):
    def setUp(self):
        self.t = make_tuner()
        self.t.DIAG_SLOW = 0.05

    def test_wrapper_is_transparent(self):
        seen = []

        def impl(api, a0, a1, a2, a3, payload, timeout, dev):
            seen.append((api, a0, a1, a2, a3, payload, timeout, dev))
            return {"ok": 1}
        self.t._call_impl = impl
        for v in ("1", "0"):
            with diag_env(v):
                r, out = capture(self.t.call, 9, a0=4, a3=7, payload={"x": 1},
                                 timeout=2.0, dev="d")
            self.assertEqual(r, {"ok": 1})
            self.assertEqual(out, "")
            self.assertEqual(seen[-1], (9, 4, 0, 0, 7, {"x": 1}, 2.0, "d"))
        self.assertIsNone(self.t._lock_holder)

    def test_exception_propagates_and_holder_cleared(self):
        def impl(*a):
            raise OSError("x")
        self.t._call_impl = impl
        with diag_env("1"):
            with self.assertRaises(OSError):
                self.t.call(9)
        self.assertIsNone(self.t._lock_holder)
        self.assertTrue(self.t.lock.acquire(blocking=False))   # released
        self.t.lock.release()

    def test_wait_and_holder_logged(self):
        started, release = threading.Event(), threading.Event()
        n = []

        def impl(api, *a):
            n.append(api)
            if api == 9:
                started.set()
                time.sleep(0.3)
            return None
        self.t._call_impl = impl

        def holder():
            self.t.call(9, dev="devH")
        with diag_env("1"):
            th = threading.Thread(target=holder, name="tune-thread")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                th.start()
                started.wait(2)
                self.t.call(11, dev="devB")
                th.join()
        out = buf.getvalue()
        self.assertIn("lock wait api11 dev=devB", out)
        self.assertIn("holder=api9 dev=devH thread=tune-thread", out)
        self.assertIn("lock held api9 dev=devH", out)
        self.assertEqual(sorted(n), [9, 11])

    def test_fast_calls_are_silent(self):
        self.t._call_impl = lambda *a: None
        with diag_env("1"):
            _, out = capture(self.t.call, 11)
        self.assertEqual(out, "")

    def test_reentrant_call_keeps_outer_holder(self):
        def impl(api, *a):
            if api == 9:
                self.t.call(11)
                self.assertEqual(self.t._lock_holder[0], 9)
            return None
        self.t._call_impl = impl
        with diag_env("1"):
            capture(self.t.call, 9)
        self.assertIsNone(self.t._lock_holder)


class FakeHeaders(dict):
    def get(self, k, d=None):
        return dict.get(self, k, d)


class StreamHandler:
    """Minimal stand-in for hdhr_emulator.Stream (borrows its methods)."""
    _ts_brp = h.Stream._ts_brp
    _ts_brp_paced = h.Stream._ts_brp_paced
    _zlog_stream = h.Stream._zlog_stream

    def __init__(self, wfile=None, path="/auto/v2.1?token=SECRET", ua="VLC/3.0\n"):
        self.wfile = wfile or io.BytesIO()
        self.client_address = ("192.0.2.44", 5555)
        self.headers = FakeHeaders({"User-Agent": ua,
                                    "Authorization": "Bearer TOPSECRET",
                                    "Cookie": "sid=TOPSECRET"})
        self.path = path
        self.flushed = 0

    def _ts_headers(self):
        pass

    def _tune_socket(self):
        pass


class BrokenWfile:
    def write(self, b):
        raise BrokenPipeError()

    def flush(self):
        pass


PKT = b"\x47\x1f\xff\x10" + b"\xff" * 184


class StreamReasons(unittest.TestCase):
    def setUp(self):
        self._pace = h.PACE

    def tearDown(self):
        h.PACE = self._pace

    def run_stream(self, sess, wfile=None, pace=False):
        h.PACE = pace
        hd = StreamHandler(wfile)
        with diag_env("1"):
            _, out = capture(hd._ts_brp, sess, headers_sent=True)
        return hd, out

    def test_unpaced_upstream_ended(self):
        hd, out = self.run_stream(Sess([PKT] * 20))
        self.assertEqual(hd._zreason, "upstream_ended")
        self.assertRegex(out, r"^\[\d{4}-.*\] brp stream ")

    def test_unpaced_client_closed(self):
        hd, _ = self.run_stream(Sess([PKT] * 20), wfile=BrokenWfile())
        self.assertEqual(hd._zreason, "client_closed")

    def test_unpaced_error(self):
        hd, out = self.run_stream(Sess([PKT] * 3, exc=RuntimeError("bad")))
        self.assertEqual(hd._zreason, "error")
        self.assertIn("brp stream error: RuntimeError('bad')", out)

    def test_unpaced_tail_write_failure_is_client_closed(self):
        hd, _ = self.run_stream(Sess([PKT] * 3), wfile=BrokenWfile())
        self.assertEqual(hd._zreason, "client_closed")

    def test_paced_upstream_ended_or_pacer_done(self):
        hd, out = self.run_stream(Sess([PKT] * 50), pace=True)
        self.assertIn(hd._zreason, ("upstream_ended", "pacer_done"))
        self.assertIn("closed (paced)", out)

    def test_paced_reader_error(self):
        hd, out = self.run_stream(Sess([PKT] * 3, exc=RuntimeError("bad")),
                                  pace=True)
        self.assertEqual(hd._zreason, "error")
        self.assertIn("brp pace reader error", out)

    def test_paced_client_closed(self):
        sess = Sess([PKT] * 20000)
        hd, _ = self.run_stream(sess, wfile=BrokenWfile(), pace=True)
        self.assertIn(hd._zreason, ("client_closed", "upstream_ended",
                                    "pacer_done"))

    def test_diag_off_keeps_legacy_log_line(self):
        h.PACE = False
        hd = StreamHandler()
        with diag_env("0"):
            _, out = capture(hd._ts_brp, Sess([PKT] * 7), headers_sent=True)
        self.assertTrue(out.startswith("brp stream "), out)


class StreamOpenCloseLine(unittest.TestCase):
    def test_line_has_client_ua_path_and_no_secrets(self):
        hd = StreamHandler()
        with diag_env("1"):
            _, out = capture(hd._zlog_stream, "stream open", "cid1", "x=1")
        self.assertRegex(out, r"^\[\d{4}-")
        self.assertIn("stream open chan=cid1 client=192.0.2.44", out)
        self.assertIn("ua=VLC/3.0?", out)            # newline neutralised
        self.assertIn("path=/auto/v2.1 ", out)       # query dropped
        for secret in ("SECRET", "TOPSECRET", "Bearer", "Cookie", "token"):
            self.assertNotIn(secret, out)
        self.assertEqual(out.count("\n"), 1)

    def test_off_prints_nothing(self):
        with diag_env("0"):
            _, out = capture(StreamHandler()._zlog_stream, "stream open", "c")
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
