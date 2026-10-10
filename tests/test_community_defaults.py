"""Per-install seed, never-hang lineup endpoints, background M1 discovery.
Offline: no real sockets to an M1, nothing sent to the LAN."""
import contextlib
import http.client
import io
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import zbconf  # noqa: E402
import hdhr_emulator as h  # noqa: E402
import hlstuner  # noqa: E402

M1CFG = {"m1": [{"address": "192.0.2.10"}],
         "push": {"pool": ["192.0.2.240-249"]}}


def plan_ids(cfg):
    _c, plan, errs, _w = zbconf.expand(zbconf.apply_env(cfg, env={}))
    assert not errs, errs
    return ([d["dev"] for d in plan["m1s"][0]["brp_devs"]],
            plan["m1s"][0]["device_id"])


def seeded(tmp, **push):
    cfg = {"m1": list(M1CFG["m1"]),
           "push": dict(M1CFG["push"], **push)}
    return zbconf.resolve_seed(cfg, env={"ZB_DATA_DIR": tmp})


class SeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_generated_once_and_persisted_0600(self):
        cfg, src = seeded(self.tmp)
        path = os.path.join(self.tmp, "seed")
        self.assertTrue(os.path.exists(path))
        self.assertIn("NEW random seed", src)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        seed = cfg["push"]["device_seed"]
        self.assertEqual(len(seed), 32)
        self.assertNotIn(seed, src)           # never printed
        self.assertEqual(open(path).read().strip(), seed)
        self.assertEqual(os.listdir(self.tmp), ["seed"])   # no temp litter

    def test_restart_keeps_identical_ids(self):
        c1, _ = seeded(self.tmp)
        ids1 = plan_ids(c1)
        c2, src2 = seeded(self.tmp)
        self.assertIn("persisted seed file", src2)
        self.assertEqual(plan_ids(c2), ids1)
        c3, _ = seeded(self.tmp)
        self.assertEqual(plan_ids(c3), ids1)

    def test_installs_differ(self):
        a, _ = seeded(self.tmp)
        b, _ = seeded(tempfile.mkdtemp())
        self.assertNotEqual(plan_ids(a), plan_ids(b))

    def test_deleting_seed_changes_ids(self):
        a, _ = seeded(self.tmp)
        os.unlink(os.path.join(self.tmp, "seed"))
        b, _ = seeded(self.tmp)
        self.assertNotEqual(plan_ids(a), plan_ids(b))

    def test_explicit_seed_overrides_and_writes_nothing(self):
        cfg, src = seeded(self.tmp, device_seed="mine")
        self.assertIn("explicit", src)
        self.assertEqual(cfg["push"]["device_seed"], "mine")
        self.assertEqual(os.listdir(self.tmp), [])
        # env override reaches resolve_seed through apply_env
        c = zbconf.apply_env({"m1": M1CFG["m1"]}, env={"ZB_DEVICE_SEED": "e"})
        c, src = zbconf.resolve_seed(c, env={"ZB_DATA_DIR": self.tmp})
        self.assertEqual(c["push"]["device_seed"], "e")
        self.assertEqual(os.listdir(self.tmp), [])

    def test_existing_seed_never_regenerated_even_if_odd(self):
        path = os.path.join(self.tmp, "seed")
        with open(path, "w") as fh:
            fh.write("  hand written seed \n")
        cfg, src = seeded(self.tmp)
        self.assertEqual(cfg["push"]["device_seed"], "hand written seed")
        self.assertEqual(open(path).read(), "  hand written seed \n")

    def test_corrupt_seed_tolerated(self):
        path = os.path.join(self.tmp, "seed")
        for junk in (b"", b"\n  \n", b"\xff\xfe\x00\x01"):
            with open(path, "wb") as fh:
                fh.write(junk)
            cfg, src = seeded(self.tmp)
            self.assertIn("NEW random seed", src)
            self.assertIn("kept as", src)
            self.assertEqual(len(cfg["push"]["device_seed"]), 32)
            self.assertTrue(os.path.exists(path + ".bad"))
            # and now stable
            again, _ = seeded(self.tmp)
            self.assertEqual(again["push"]["device_seed"],
                             cfg["push"]["device_seed"])
            os.unlink(path)
            os.unlink(path + ".bad")

    def test_unwritable_dir_falls_back_loudly(self):
        blocker = os.path.join(self.tmp, "file")
        open(blocker, "w").close()
        cfg, src = zbconf.resolve_seed(
            {"m1": M1CFG["m1"]}, env={"ZB_DATA_DIR": blocker + "/sub"})
        self.assertEqual(cfg["push"]["device_seed"], zbconf.LEGACY_SEED)
        self.assertIn("FALLBACK", src)

    def test_create_false_is_read_only_placeholder(self):
        cfg, src = zbconf.resolve_seed({"m1": M1CFG["m1"]},
                                       env={"ZB_DATA_DIR": self.tmp},
                                       create=False)
        self.assertIn("PLACEHOLDER", src)
        self.assertEqual(os.listdir(self.tmp), [])

    def test_race_loser_uses_winner(self):
        path = os.path.join(self.tmp, "seed")
        zbconf._write_seed_atomic(path, "winner")
        with self.assertRaises(FileExistsError):
            zbconf._write_seed_atomic(path, "loser")
        self.assertEqual(open(path).read().strip(), "winner")
        self.assertEqual(os.listdir(self.tmp), ["seed"])

    def test_plan_shows_source_and_ids(self):
        cfg, src = seeded(self.tmp)
        _c, plan, errs, warns = zbconf.expand(cfg)
        plan["seed_source"] = src
        text = zbconf.render_plan(plan, errs, warns)
        self.assertIn("device id seed: NEW random seed", text)
        self.assertIn(plan["m1s"][0]["brp_devs"][0]["dev"], text)

    def test_entrypoint_check_prints_source(self):
        sys.path.insert(0, os.path.join(ROOT, "docker"))
        import entrypoint
        env = {k: os.environ.get(k) for k in
               ("ZB_DATA_DIR", "ZB_CONFIG", "ZB_M1", "ZB_DEVICE_SEED")}
        os.environ["ZB_DATA_DIR"] = self.tmp
        os.environ["ZB_CONFIG"] = os.path.join(self.tmp, "none.toml")
        os.environ["ZB_M1"] = "192.0.2.10"
        os.environ.pop("ZB_DEVICE_SEED", None)
        try:
            outs = []
            for _ in range(2):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = entrypoint.main(["entrypoint.py", "check",
                                          "--skip-free-check"])
                self.assertEqual(rc, 0)
                outs.append(buf.getvalue())
        finally:
            for k, v in env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertIn("NEW random seed", outs[0])
        self.assertIn("persisted seed file", outs[1])
        dev = [l for l in outs[0].splitlines() if l.strip().startswith("dev ")]
        self.assertTrue(dev)
        self.assertEqual(dev, [l for l in outs[1].splitlines()
                               if l.strip().startswith("dev ")])


class _Emu(unittest.TestCase):
    """Json handler on an ephemeral port with the M1 side faked."""

    def setUp(self):
        keys = ("_cache", "get_lineup", "LINEUP_TIMEOUT", "LINEUP_CACHE_FILES",
                "M1", "EPG")
        self._saved = {k: getattr(h, k) for k in keys}
        self._ready = h._m1_ready.is_set()
        self._flight = dict(h._lu_flight)
        h._lu_flight.update(ev=None, box=None)
        h._m1_ready.set()
        h.M1 = "192.0.2.10"
        h._cache = {"at": 0, "channels": []}
        h.LINEUP_CACHE_FILES = [os.path.join(tempfile.mkdtemp(), "none.json")]
        h.LINEUP_TIMEOUT = 0.4
        self.release = threading.Event()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), h.Json)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.release.set()
        self.srv.shutdown()
        self.srv.server_close()
        for k, v in self._saved.items():
            setattr(h, k, v)
        h._lu_flight.update(self._flight)
        if not self._ready:
            h._m1_ready.clear()

    def hang(self):
        def slow(force=False):
            self.release.wait(30)
            raise RuntimeError("lineup unavailable: api8 failed and no cache")
        h.get_lineup = slow

    def get(self, path, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        t = time.time()
        c.request("GET", path, headers=headers or {})
        r = c.getresponse()
        body = r.read()
        return r.status, body, r, time.time() - t


CH = [{"chan_id": "0x1130000000002001", "chan_major": 2, "chan_minor": 1,
       "call_letters": "CBS2-HD", "v_res": 1080}]


class DeadM1Tests(_Emu):
    def test_discover_is_instant(self):
        self.hang()
        st, body, _r, dt = self.get("/discover.json")
        self.assertEqual(st, 200)
        self.assertLess(dt, 0.3)
        self.assertIn(b"DeviceID", body)

    def test_lineup_json_empty_within_timeout(self):
        self.hang()
        st, body, r, dt = self.get("/lineup.json")
        self.assertEqual((st, body), (200, b"[]"))
        self.assertLess(dt, 2.0)
        self.assertIn("unavailable", r.getheader("X-Zapperbridge-Lineup"))

    def test_lineup_status_within_timeout(self):
        self.hang()
        st, body, _r, dt = self.get("/lineup_status.json")
        self.assertEqual(st, 200)
        self.assertIn(b"ScanPossible", body)
        self.assertLess(dt, 2.0)

    def test_concurrent_requests_share_one_fetch(self):
        calls = []
        ev = self.release

        def slow(force=False):
            calls.append(1)
            ev.wait(30)
            raise RuntimeError("x")
        h.get_lineup = slow
        ts = [threading.Thread(target=self.get, args=("/lineup.json",))
              for _ in range(5)]
        [t.start() for t in ts]
        [t.join(10) for t in ts]
        self.assertEqual(len(calls), 1)

    def test_cached_lineup_served_when_m1_down(self):
        h._cache = {"at": time.time() - 1000, "channels": list(CH)}
        self.hang()
        st, body, r, dt = self.get("/lineup.json")
        self.assertEqual(st, 200)
        self.assertLess(dt, 2.0)
        self.assertIn(b"CBS2-HD", body)
        self.assertIn("stale", r.getheader("X-Zapperbridge-Lineup"))

    def test_discover_uses_cached_version_without_waiting(self):
        h._cache = {"at": time.time() - 1000, "channels": list(CH)}
        self.hang()
        st, body, _r, dt = self.get("/discover.json")
        self.assertEqual(st, 200)
        self.assertLess(dt, 0.3)
        self.assertIn(str(h.lineup_version(h.cached_lineup())).encode(), body)

    def test_no_brp_lock_held_by_handlers(self):
        self.hang()
        self.get("/lineup.json")
        got = h._brp_lock.acquire(timeout=0.5)   # RLock: same thread succeeds
        self.assertTrue(got)
        h._brp_lock.release()
        # the handler thread itself must not be the one blocked in get_lineup
        names = [t.name for t in threading.enumerate()]
        self.assertIn("lineup-fetch", names)

    def test_healthy_behaviour_unchanged(self):
        h.get_lineup = lambda force=False: list(CH)
        h._cache = {"at": time.time(), "channels": list(CH)}
        st, body, r, _dt = self.get("/lineup.json")
        self.assertEqual(st, 200)
        self.assertIn(b"CBS2-HD", body)
        self.assertIsNone(r.getheader("X-Zapperbridge-Lineup"))
        etag = r.getheader("ETag")
        self.assertTrue(etag)
        st2, _b, _r2, _ = self.get("/lineup.json", {"If-None-Match": etag})
        self.assertEqual(st2, 304)
        # a healthy but cold fetch (cache empty) is still returned in full
        h._cache = {"at": 0, "channels": []}
        h._lu_flight.update(ev=None, box=None)
        st3, body3, r3, _ = self.get("/lineup.json")
        self.assertEqual(st3, 200)
        self.assertIn(b"CBS2-HD", body3)
        self.assertIsNone(r3.getheader("X-Zapperbridge-Lineup"))

    def test_unknown_path_404_without_lineup_fetch(self):
        self.hang()
        st, _b, _r, dt = self.get("/nope")
        self.assertEqual(st, 404)
        self.assertLess(dt, 0.3)


class PendingDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self._saved = (h.M1, h.M1_SPEC, dict(h._m1_info), h.EPG.owner,
                       hlstuner.M1, os.environ.get("ZM1"),
                       h._m1_ready.is_set())
        h._m1_ready.clear()
        h.M1_SPEC = "auto"
        h.M1 = "auto"
        import m1disco

        class FakeL:
            def start(self):
                return self

            def shutdown(self):
                pass
        self._L = m1disco.Listener
        m1disco.Listener = FakeL
        self.m1disco = m1disco
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), h.Json)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.m1disco.Listener = self._L
        (h.M1, h.M1_SPEC, info, h.EPG.owner, hlstuner.M1, z, ready) = self._saved
        h._m1_info.clear()
        h._m1_info.update(info)
        if z is None:
            os.environ.pop("ZM1", None)
        else:
            os.environ["ZM1"] = z
        h._m1_ready.set() if ready else h._m1_ready.clear()

    def get(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        t = time.time()
        c.request("GET", path)
        r = c.getresponse()
        return r.status, r.read(), time.time() - t

    def test_serves_while_pending(self):
        self.assertEqual(self.get("/healthz")[:2], (200, b'{"ok": true}'))
        st, body, dt = self.get("/lineup.json")
        self.assertEqual((st, body), (200, b"[]"))
        self.assertLess(dt, 1.0)
        self.assertEqual(self.get("/discover.json")[0], 200)
        self.assertEqual(self.get("/lineup_status.json")[0], 200)
        with self.assertRaises(RuntimeError):
            h.get_brp_tuner()
        with self.assertRaises(RuntimeError):
            h.brp_call(8)

    def test_background_loop_waits_logs_then_adopts(self):
        calls = []
        found = threading.Event()

        def resolver(spec, wait=0, listener=None):
            calls.append(spec)
            time.sleep(0.02)
            if len(calls) < 3:
                return None, None, "no announce heard"
            return "192.0.2.77", "02aa00000001", "announce"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            th = h.start_m1_discovery(found.set, log_every=0.05,
                                      resolver=resolver)
            self.assertTrue(found.wait(5))
            th.join(5)
        out = buf.getvalue()
        self.assertGreaterEqual(out.count("still waiting"), 2)
        self.assertIn("host or macvlan", out)
        self.assertIn("ZB_M1", out)
        self.assertTrue(h._m1_ready.is_set())
        self.assertEqual(h.M1, "192.0.2.77")
        self.assertEqual(os.environ["ZM1"], "192.0.2.77")
        self.assertEqual(hlstuner.M1, "192.0.2.77")
        self.assertEqual(h._m1_info["dev"], "02aa00000001")

    def test_literal_m1_needs_no_discovery(self):
        h._m1_ready.set()
        self.assertIsNone(h.start_m1_discovery(lambda: None))

    def test_ambiguous_does_not_spin(self):
        n = []

        def resolver(spec, wait=0, listener=None):
            n.append(1)
            return None, None, "ambiguous: 2 M1s"
        sleeps = []

        def fake_sleep(s):
            sleeps.append(s)
            if len(sleeps) >= 3:
                h._m1_ready.set()    # stop the loop
        with contextlib.redirect_stdout(io.StringIO()):
            th = h.start_m1_discovery(lambda: None, log_every=7,
                                      resolver=resolver, sleep=fake_sleep)
            th.join(5)
        self.assertEqual(sleeps, [7, 7, 7])


if __name__ == "__main__":
    unittest.main()
