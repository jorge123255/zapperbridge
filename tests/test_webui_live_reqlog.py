"""Live-update cursors + opt-in request log (offline, loopback only)."""
import json
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import reqlog
import webui
import zdiag
from test_webui import Base, zui, FakeSess, FakePacer


class TestEvents(Base):
    def test_off_means_404(self):
        with zui(None):
            self.assertEqual(self.req("/api/ui/events")[0], 404)

    def test_events_cursor_moves(self):
        with zui():
            zdiag.ring_enable(50)
            e0 = self.jget("/api/ui/events")
            self.assertEqual(e0["live"], 0)
            s = FakeSess()
            webui.stream_open(s, "1.1", "192.0.2.5", "ua")
            e1 = self.jget("/api/ui/events")
            self.assertGreater(e1["seq"], e0["seq"])
            self.assertEqual(e1["live"], 1)
            webui.stream_close(s, "client_closed", 5)
            e2 = self.jget("/api/ui/events")
            self.assertGreater(e2["seq"], e1["seq"])
            self.assertEqual(e2["live"], 0)
            self.assertEqual(self.jget("/api/ui/streams")["seq"], e2["seq"])

    def test_events_is_cheap_and_never_touches_tuner(self):
        with zui():
            # base class makes brp_call/get_brp_tuner raise if touched
            self.assertIn("seq", self.jget("/api/ui/events"))
            self.assertEqual(self.req("/api/ui/events", "HEAD")[0], 200)

    def test_logs_since_cursor(self):
        with zui():
            zdiag.ring_enable(50)
            os.environ["ZLOG_DIAG"] = "1"
            try:
                zdiag.dlog("stream open chan=a")
                c1 = self.jget("/api/ui/events")["log_seq"]
                self.assertGreater(c1, 0)
                zdiag.dlog("stream close chan=a reason=x")
                zdiag.dlog("noise not kept")
                d = self.jget("/api/ui/logs?since=%d" % c1)
                self.assertEqual([l["line"] for l in d["lines"]], ["stream close chan=a reason=x"])
                self.assertEqual(d["cursor"], self.jget("/api/ui/events")["log_seq"])
                self.assertEqual(self.jget("/api/ui/logs?since=%d" % d["cursor"])["lines"], [])
                self.assertEqual(len(self.jget("/api/ui/logs")["lines"]), 2)
            finally:
                os.environ.pop("ZLOG_DIAG", None)

    def test_closed_record_carries_wgap(self):
        with zui():
            s = FakeSess()
            s._zwgap = {"write_gap_ms_max": 44}
            webui.stream_open(s, "1.1", "192.0.2.5", "ua")
            webui.stream_close(s, "client_closed", 9)
            c = self.jget("/api/ui/streams")["recent_closed"][0]
            self.assertEqual(c["wgap"], {"write_gap_ms_max": 44})

    def test_js_polls_with_fetch_only(self):
        js = open(os.path.join(ROOT, "ui", "ui.js")).read()
        self.assertIn("/api/ui/events", js)
        self.assertNotIn("WebSocket", js)
        self.assertNotIn("EventSource", js)


class TestReqlog(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "req.jsonl")
        for k in ("ZREQLOG", "ZUI_REQLOG", "ZREQLOG_MAX_BYTES", "ZREQLOG_FILES"):
            os.environ.pop(k, None)

    def tearDown(self):
        for k in ("ZREQLOG", "ZUI_REQLOG", "ZREQLOG_MAX_BYTES", "ZREQLOG_FILES"):
            os.environ.pop(k, None)

    def lines(self, p=None):
        return [json.loads(l) for l in open(p or self.p)]

    def test_default_off_creates_nothing(self):
        self.assertIsNone(reqlog.path())
        self.assertFalse(reqlog.log_open("1.1", "1.2.3.4", "ua", "/auto/v1.1"))
        self.assertEqual(os.listdir(self.d), [])

    def test_both_env_names(self):
        os.environ["ZUI_REQLOG"] = self.p
        self.assertEqual(reqlog.path(), self.p)
        os.environ["ZREQLOG"] = self.p + "2"
        self.assertEqual(reqlog.path(), self.p + "2")

    def test_open_close_records_and_no_query(self):
        os.environ["ZREQLOG"] = self.p
        reqlog.log_open("0xabc", "192.0.2.5", "AppleCoreMedia\n\x00x",
                        "/auto/v2.1?token=SECRET&apikey=Z#frag")
        reqlog.log_close("0xabc", "192.0.2.5", "UA", "/auto/v2.1?token=SECRET",
                         1234, 12.3456, "client_closed", {"write_gap_ms_max": 44})
        raw = open(self.p).read()
        self.assertNotIn("SECRET", raw)
        self.assertNotIn("apikey", raw)
        self.assertNotIn("frag", raw)
        a, b = self.lines()
        self.assertEqual((a["event"], a["path"]), ("open", "/auto/v2.1"))
        self.assertNotIn("\n", a["ua"])
        self.assertEqual(b["event"], "close")
        for k in ("ts", "chan_id", "client_ip", "ua", "path", "bytes", "dur_sec",
                  "reason", "wgap"):
            self.assertIn(k, b)
        self.assertEqual((b["bytes"], b["dur_sec"], b["reason"], b["wgap"]),
                         (1234, 12.35, "client_closed", {"write_gap_ms_max": 44}))
        self.assertEqual(oct(os.stat(self.p).st_mode & 0o777), "0o600")

    def test_rotation_5mb_default_and_three_files(self):
        os.environ["ZREQLOG"] = self.p
        self.assertEqual(reqlog.DEFAULT_MAX_BYTES, 5 * 1024 * 1024)
        self.assertEqual(reqlog.DEFAULT_FILES, 3)
        os.environ["ZREQLOG_MAX_BYTES"] = "1024"
        for i in range(200):
            reqlog.log_open("c%d" % i, "1.2.3.4", "ua" * 20, "/auto/v1.1")
        names = sorted(os.listdir(self.d))
        self.assertEqual(names, ["req.jsonl", "req.jsonl.1", "req.jsonl.2"])
        for n in names:
            self.assertLess(os.path.getsize(os.path.join(self.d, n)), 1024 + 300)
        # newest records are in the live file, oldest were dropped
        self.assertEqual(self.lines()[-1]["chan_id"], "c199")
        self.assertNotIn("c0", [r["chan_id"] for n in names
                                for r in self.lines(os.path.join(self.d, n))])

    def test_errors_swallowed(self):
        os.environ["ZREQLOG"] = os.path.join(self.d, "nodir", "x", "req.jsonl")
        self.assertFalse(reqlog.log_open("1", "ip", "ua", "/p"))
        os.environ["ZREQLOG"] = self.d            # a directory
        self.assertFalse(reqlog.log_close("1", "ip", "ua", "/p", object(), "x", None, 5))

    def test_no_secret_sources_in_module(self):
        src = open(os.path.join(ROOT, "reqlog.py")).read()
        for bad in ("headers", "Cookie", "Authorization", "environ.get(\"ZAPPER"):
            self.assertNotIn(bad, src.replace("request header, cookie", ""), bad)

    def test_wgap_summary_dict(self):
        t = [0.0]
        m = zdiag.WriteGapMonitor("x", clock=lambda: t[0], emit=lambda s: None)
        for _ in range(3):
            t0 = t[0]
            t[0] += 0.03
            m.on_write(188, t0, t[0], 1.5)
        m.finish("client_closed")
        self.assertEqual(m.summary["close"], "client_closed")
        self.assertIn("write_gap_ms_max", m.summary)
        self.assertIn("input_gap_ms_max", m.summary)


if __name__ == "__main__":
    unittest.main()
