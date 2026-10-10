"""Read-only web UI (ZUI=readonly, default OFF). Offline: a loopback HTTP
server in-process, no M1, no BRP, no sockets to the LAN. pytest or unittest."""
import contextlib
import http.client
import json
import os
import sys
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import zdiag
import webui
import hdhr_emulator as h

FIX = os.path.join(HERE, "fixtures", "api8-market-a-51.json")


def _chans():
    d = json.load(open(FIX))
    return d["channels"] if isinstance(d, dict) else d


@contextlib.contextmanager
def zui(value="readonly"):
    old = os.environ.get("ZUI")
    if value is None:
        os.environ.pop("ZUI", None)
    else:
        os.environ["ZUI"] = value
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("ZUI", None)
        else:
            os.environ["ZUI"] = old


class FakeSess:
    def __init__(self, chan="0x1130000000002001"):
        self.chan_id, self.nbytes, self.qdrops = chan, 1234, 0


class FakePacer:
    stats = {"bytes_in": 10, "bytes_out": 9, "start_delay": 1.5, "skips": 0,
             "skipped_pkts": 0, "rebuffers": 1, "max_level": 3.2, "mode": "rate",
             "writes": 4, "pcr_resets": 0}

    def level(self):
        return 2.5


class FakeTuner:
    def live_info(self):
        return [{"chan_id": "0xabc", "nbytes": 5, "client_ip": "192.0.2.50",
                 "t0": time.time() - 3}, None, None]

    def health(self):
        return {"leases": 1, "sessions_live": 1, "sessions_closed": 0,
                "qdrops_total": 0, "qdrop_bursts_total": 0, "tune_gate_busy": False,
                "tune_fail_counts": {}, "last_push_age_sec": 0.5, "last_fail": None,
                "push_ips": [{"dev": h.BRP_DEVS[0], "push_ip": "192.0.2.30", "present": True},
                             {"dev": h.BRP_DEVS[1], "push_ip": "192.0.2.237", "present": False},
                             {"dev": h.BRP_DEVS[2], "push_ip": "192.0.2.243", "present": False}],
                "push_ip_warning": "x"}


class Base(unittest.TestCase):
    def setUp(self):
        self._saved = {k: getattr(h, k) for k in ("_cache", "_brp_tuner", "brp_call",
                                                   "get_lineup", "get_brp_tuner")}
        h._cache = {"at": time.time(), "channels": _chans()}
        h._brp_tuner = None

        def boom(*a, **k):
            raise AssertionError("UI must not touch the M1 / BRP / lineup refresh")
        h.brp_call = boom
        h.get_brp_tuner = boom
        self._lineup = h.get_lineup
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), h.Json)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        webui._live.clear()
        webui._closed.clear()
        webui.set_registry(None)
        self._env = {k: os.environ.get(k) for k in ("ZUI_RESERVED_IPS", "ZUI_TOKEN")}
        os.environ["ZUI_RESERVED_IPS"] = "192.0.2.237"
        os.environ.pop("ZUI_TOKEN", None)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.srv.shutdown()
        self.srv.server_close()
        for k, v in self._saved.items():
            setattr(h, k, v)
        zdiag.ring_disable()

    def req(self, path, method="GET"):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path)
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, dict(r.getheaders()), body

    def jget(self, path):
        st, hd, body = self.req(path)
        self.assertEqual(st, 200, body[:200])
        return json.loads(body)


class TestOffByDefault(Base):
    def test_default_off(self):
        with zui(None):
            self.assertFalse(webui.enabled())
            for p in ("/ui", "/ui/", "/ui/index.html", "/api/ui/state"):
                st, _, body = self.req(p)
                self.assertEqual(st, 404, p)
                self.assertEqual(json.loads(body), {"error": "not found"})

    def test_other_values_stay_off(self):
        for v in ("", "0", "1", "true", "admin", "write", "readwrite"):
            with zui(v):
                self.assertFalse(webui.enabled(), v)

    def test_case_insensitive_on(self):
        with zui("ReadOnly"):
            self.assertTrue(webui.enabled())

    def test_head_off_is_501_like_before(self):
        with zui(None):
            self.assertEqual(self.req("/ui", "HEAD")[0], 501)

    def test_off_does_not_register_streams_or_ring(self):
        with zui(None):
            webui.stream_open(FakeSess(), "1.1", "192.0.2.5", "ua")
            self.assertEqual(webui._live, {})
            zdiag.dlog("stream open chan=1")
            self.assertEqual(zdiag.ring_lines(), [])


class TestPages(Base):
    def test_all_endpoints_json_and_cached_only(self):
        with zui():
            st = self.jget("/api/ui/state")
            self.assertEqual(st["mode"], "readonly")
            self.assertGreater(st["lineup_count"], 5)
            self.assertTrue(any("not initialised" in w for w in st["warnings"]))
            lu = self.jget("/api/ui/lineup")
            self.assertEqual(lu["count"], st["lineup_count"])
            self.assertTrue(lu["channels"][0]["number"])
            self.assertEqual(self.jget("/api/ui/streams")["live"], [])
            dv = self.jget("/api/ui/devices")
            self.assertEqual(dv["configured_m1s"][0]["ip"], h.M1)
            self.assertEqual(dv["protected_minis"], ["192.0.2.237"])  # from ZUI_RESERVED_IPS only
            self.assertIsNone(dv["passive_discovery"])
            self.assertIn("rows", self.jget("/api/ui/aliases"))
            self.assertEqual(self.jget("/api/ui/logs")["lines"], [])

    def test_slots_with_tuner(self):
        h._brp_tuner = FakeTuner()
        with zui():
            st = self.jget("/api/ui/state")
            brp_slots = [s for s in st["slots"] if s["kind"] == "brp-lease"]
            self.assertEqual(len(brp_slots), len(h.BRP_DEVS))
            self.assertEqual(brp_slots[0]["state"], "streaming")
            self.assertEqual(brp_slots[0]["client_ip"], "192.0.2.50")
            self.assertEqual(brp_slots[1]["state"], "idle")
            if h.HLS_TUNER:
                self.assertEqual(st["slots"][-1]["kind"], "m1-hls-fixed")

    def test_live_and_closed_streams(self):
        s = FakeSess()
        s._zpacer = FakePacer()
        with zui():
            webui.stream_open(s, s.chan_id, "192.0.2.50", "Evil<script>UA")
            live = self.jget("/api/ui/streams")["live"]
            self.assertEqual(live[0]["client_ip"], "192.0.2.50")
            self.assertEqual(live[0]["ua"], "Evil<script>UA")  # raw; JS uses textContent
            self.assertEqual(live[0]["pacer"]["rebuffers"], 1)
            self.assertEqual(live[0]["pacer"]["level"], 2.5)
            webui.stream_close(s, "client_closed", 99)
            d = self.jget("/api/ui/streams")
            self.assertEqual(d["live"], [])
            self.assertEqual(d["recent_closed"][0]["reason"], "client_closed")
            self.assertEqual(d["recent_closed"][0]["bytes"], 99)

    def test_closed_is_bounded(self):
        with zui():
            for i in range(webui.CLOSED_KEEP + 20):
                s = FakeSess()
                webui.stream_open(s, "c", "192.0.2.5", "u")
                webui.stream_close(s, "x", 1)
            self.assertEqual(len(webui._closed), webui.CLOSED_KEEP)

    def test_aliases_page_commands_only_and_reserved(self):
        h._brp_tuner = FakeTuner()
        with zui():
            a = self.jget("/api/ui/aliases")
            by = {r["push_ip"]: r["status"] for r in a["rows"]}
            self.assertTrue(by["192.0.2.30"].startswith("ok"))
            self.assertIn("reserved", by["192.0.2.237"])
            self.assertEqual(by["192.0.2.243"], "missing on this host")
            self.assertEqual(a["missing"], ["192.0.2.243"])
            self.assertEqual(a["commands"]["macos"],
                             ["sudo ifconfig en0 alias 192.0.2.243 255.255.255.255"])
            self.assertEqual(a["commands"]["linux"],
                             ["sudo ip addr add 192.0.2.243/32 dev eth0"])

    def test_devices_passive_registry(self):
        import m1disco
        reg = m1disco.Registry()
        reg.observe(m1disco.parse(m1disco.build(0, 3, "02bb00a00001", "192.0.2.10",
                                                 "M1", "ZBM1-gt698", 4), "192.0.2.10"))
        reg.observe(m1disco.parse(m1disco.build(0, 1, "02bb00c00001", "192.0.2.237",
                                                 "Mini", "ZBM3-gt316", 0), "192.0.2.237"))
        webui.set_registry(reg)
        with zui():
            pd = self.jget("/api/ui/devices")["passive_discovery"]
            kinds = {r["dev"]: (r["kind"], r["protected"]) for r in pd}
            self.assertEqual(kinds["02bb00a00001"], ("m1", False))
            self.assertEqual(kinds["02bb00c00001"], ("client", True))

    def test_logs_ring(self):
        with zui():
            zdiag.ring_enable(50)
            zdiag.dlog("stream open chan=1 client=192.0.2.5 ua=x")
            zdiag.dlog("wgap sec stream=1 t=1 out_bytes=1")          # filtered
            zdiag.dlog("wgap summary stream=1 dur=1.0s close=client_closed")
            zdiag.tlog("brp stream 1 closed (paced) in=1 out=1")
            zdiag.dlog("unrelated chatter")                           # filtered
            lines = [x["line"] for x in self.jget("/api/ui/logs")["lines"]]
            self.assertEqual(len(lines), 3)
            self.assertTrue(lines[0].startswith("stream open"))
            self.assertTrue(lines[1].startswith("wgap summary"))
            self.assertTrue(self.jget("/api/ui/logs?limit=1")["lines"][0]["line"]
                            .startswith("brp stream"))

    def test_ring_bounded(self):
        zdiag.ring_enable(10)
        for i in range(100):
            zdiag._ring_add("t", "stream open n=%d" % i)
        self.assertEqual(len(zdiag.ring_lines(1000)), 10)

    def test_ring_off_with_diag_off(self):
        zdiag.ring_enable(10)
        old = os.environ.get("ZLOG_DIAG")
        os.environ["ZLOG_DIAG"] = "0"
        try:
            zdiag.dlog("stream open x")
            self.assertEqual(zdiag.ring_lines(), [])
        finally:
            if old is None:
                os.environ.pop("ZLOG_DIAG")
            else:
                os.environ["ZLOG_DIAG"] = old


class TestStaticAndSafety(Base):
    def test_static_files_and_headers(self):
        with zui():
            for p, ct in (("/ui", "text/html"), ("/ui/", "text/html"),
                          ("/ui/index.html", "text/html"),
                          ("/ui/ui.js", "application/javascript"), ("/ui/ui.css", "text/css")):
                st, hd, body = self.req(p)
                self.assertEqual(st, 200, p)
                self.assertTrue(hd["Content-Type"].startswith(ct), p)
                self.assertIn("default-src 'none'", hd["Content-Security-Policy"])
                self.assertEqual(hd["X-Content-Type-Options"], "nosniff")
            self.assertEqual(self.req("/ui", "HEAD")[0], 200)

    def test_no_traversal(self):
        with zui():
            for p in ("/ui/../webui.py", "/ui/..%2fwebui.py", "/ui/%2e%2e/webui.py",
                      "/ui/.hidden", "/ui/a/b", "/ui/webui.py", "/ui/nope.js",
                      "/api/ui/../state", "/api/ui/nope"):
                self.assertEqual(self.req(p)[0], 404, p)

    def test_no_write_methods(self):
        with zui():
            for m in ("POST", "PUT", "DELETE", "PATCH"):
                self.assertEqual(self.req("/api/ui/state", m)[0], 501, m)

    def test_non_lan_refused(self):
        for ip in ("8.8.8.8", "1.1.1.1", "2001:4860:4860::8888", "not-an-ip"):
            self.assertFalse(webui.lan_ok(ip), ip)
        for ip in ("192.0.2.5", "10.0.0.2", "172.16.4.4", "127.0.0.1",
                   "169.254.1.1", "::1", "::ffff:192.0.2.9"):
            self.assertTrue(webui.lan_ok(ip), ip)

        class H:
            client_address = ("8.8.8.8", 1)
            path = "/api/ui/state"
            wfile = type("W", (), {"write": lambda s, b: None})()
            codes = []

            def send_response(self, c):
                self.codes.append(c)

            def send_header(self, *a):
                pass

            def end_headers(self):
                pass
        hh = H()
        webui.handle(hh, "/api/ui/state", h)
        self.assertEqual(hh.codes, [403])

    def test_source_has_no_exec_or_write_paths(self):
        src = open(os.path.join(ROOT, "webui.py")).read()
        for bad in ("subprocess", "os.system", "os.popen", "socket.send", "brp_call(",
                    "get_brp_tuner(", "get_lineup(", "api7", "do_POST", "open(fp, \"w"):
            self.assertNotIn(bad, src, bad)
        js = open(os.path.join(ROOT, "ui", "ui.js")).read()
        for bad in ("innerHTML", "outerHTML", "eval(", "document.write", "method:", "POST"):
            self.assertNotIn(bad, js, bad)
        self.assertNotIn("http://", open(os.path.join(ROOT, "ui", "index.html")).read())

    def test_logs_limit_parsing(self):
        with zui():
            zdiag.ring_enable(20)
            self.assertEqual(self.jget("/api/ui/logs?limit=abc")["lines"], [])


if __name__ == "__main__":
    unittest.main()
