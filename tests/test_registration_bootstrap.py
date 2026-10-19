"""Registration bootstrap fixes (community report, Channels forum 47327):

1. `discover --apply` with an `auto:<dev>` address must unicast to the
   discovered IP, never to the literal spec, and report send errors per id
   instead of a traceback.
2. The planner (`discover`) and the facade launch (`entrypoint run`) must
   derive identical device ids for the same config and seed, and --apply
   refuses when the ids the running bridge recorded differ.
3. A refused api8 records the M1's err_name and the api6 handshake result
   (shown on /health) and logs it once per change with a hint.

Offline only: fake announces, fake senders, fake BRP replies. Sockets are
blocked so nothing reaches the LAN or an M1.
"""
import contextlib
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "docker"))
import m1disco    # noqa: E402
import m1super    # noqa: E402
import zbconf     # noqa: E402
import entrypoint  # noqa: E402
import hdhr_emulator as h  # noqa: E402

M1DEV, M1IP = "acdbda698f08", "192.0.2.54"
PUSH = ["192.0.2.81", "192.0.2.82", "192.0.2.83"]
TESTER_TOML = """
[[m1]]
name = "living-room"
address = "auto:acdbda698f08"
leases = 3
device_id = "02BB0012"
 push_ips = ["192.0.2.81", "192.0.2.82", "192.0.2.83"]
advertise_ip = "192.0.2.29"
[push]
device_prefix = "02bb00"
check_free = false
[network]
mode = "host"
[hdhr]
mode = "per-m1"
hls_tuner = true
"""


def m1pkt(dev=M1DEV, ip=M1IP):
    return (ip, m1disco.build(0, 3, dev, ip, "", "ZBM1-gt698", 4, 1))


_COLLECT = m1super.collect


def found(*pkts):
    return _COLLECT(m1super.observe_packets(list(pkts or [m1pkt()])))


class Sandbox(unittest.TestCase):
    """Temp data dir + config, env restored, every real send/connect fails."""

    TOML = TESTER_TOML
    ENV = {}

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(self.data)
        with open(os.path.join(self.data, "seed"), "w") as fh:
            fh.write("0123456789abcdef0123456789abcdef\n")
        self.cfg = os.path.join(self.tmp, "z.toml")
        with open(self.cfg, "w") as fh:
            fh.write(self.TOML)
        keys = ("ZB_DATA_DIR", "ZB_CONFIG", "ZB_M1", "ZB_DEVICE_SEED",
                "ZPUSH_IPS", "ZB_PUSH_POOL", "ZB_CHECK_FREE")
        self._env = {k: os.environ.get(k) for k in keys}
        for k in keys:
            os.environ.pop(k, None)
        os.environ.update(ZB_DATA_DIR=self.data, ZB_CONFIG=self.cfg)
        os.environ.update(self.ENV)
        self._o = (socket.socket.sendto, socket.socket.connect)

        def boom(*a, **k):
            raise AssertionError("network send/connect attempted in a test")
        socket.socket.sendto = socket.socket.connect = boom

    def tearDown(self):
        socket.socket.sendto, socket.socket.connect = self._o
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    # the two code paths, exactly as the container runs them
    def run_plan(self):
        """Ids + env the facade gets from `entrypoint run` (no process)."""
        cfg = zbconf.apply_env(zbconf.load(self.cfg))
        cfg, _src = zbconf.resolve_seed(cfg)
        _core, plan, errs, _w = zbconf.expand(cfg)
        self.assertEqual(errs, [])
        return plan

    def discover_plan(self, pk=None, apply=True):
        """Ids `discover --apply` would announce (same steps as main())."""
        cfg = zbconf.apply_env(zbconf.load(self.cfg))
        plan, errs, _w, _n, _c = m1super.build_plan(
            cfg, found(*(pk or [])), create_seed=apply)
        self.assertEqual(errs, [])
        return plan

    @staticmethod
    def devs(plan):
        return [[d["dev"] for d in m["brp_devs"]] for m in plan["m1s"]]

    def facade_devs(self, plan):
        """What HDHR_BRP_DEVS / ZPUSH_IPS the facade process is started with."""
        out = []
        for _name, env in zbconf.launch_envs(plan):
            e = dict(os.environ)
            e.update(env)
            out.append((e["HDHR_BRP_DEVS"].split(","), e.get("ZPUSH_IPS")))
        return out


class SameIdsTesterConfig(Sandbox):
    def test_planner_and_facade_identical(self):
        run, disc = self.run_plan(), self.discover_plan()
        self.assertEqual(self.devs(run), self.devs(disc))
        self.assertEqual([[d["push_ip"] for d in m["brp_devs"]]
                          for m in disc["m1s"]], [PUSH])
        self.assertEqual(self.facade_devs(run)[0][0], self.devs(disc)[0])
        self.assertEqual(run["seed_fp"], disc["seed_fp"])
        # the spec is kept, the discovered IP is separate
        self.assertEqual(disc["m1s"][0]["m1"], "auto:acdbda698f08")
        self.assertEqual(disc["m1s"][0]["m1_ip"], M1IP)

    def test_identical_with_user_zpush_ips_in_env(self):
        os.environ["ZPUSH_IPS"] = "02bb00000001=192.0.2.99"   # stale hand value
        run, disc = self.run_plan(), self.discover_plan()
        self.assertEqual(self.devs(run), self.devs(disc))
        devs, zp = self.facade_devs(run)[0]
        self.assertEqual(devs, self.devs(disc)[0])
        self.assertEqual(zp, ",".join("%s=%s" % x for x in zip(devs, PUSH)))

    def test_identical_without_listen(self):
        cfg = zbconf.apply_env(zbconf.load(self.cfg))
        plan, *_ = m1super.build_plan(cfg, [], create_seed=True)
        self.assertEqual(self.devs(plan), self.devs(self.run_plan()))
        self.assertIsNone(plan["m1s"][0]["m1_ip"])

    def test_seed_changes_ids_and_fingerprint(self):
        a = self.discover_plan()
        with open(os.path.join(self.data, "seed"), "w") as fh:
            fh.write("another-install\n")
        b = self.discover_plan()
        self.assertNotEqual(self.devs(a), self.devs(b))
        self.assertNotEqual(a["seed_fp"], b["seed_fp"])
        text = zbconf.render_plan(b, [], [])
        self.assertIn("fingerprint %s" % b["seed_fp"], text)
        self.assertNotIn("another-install", text)


class SameIdsNoEntries(Sandbox):
    TOML = '[push]\npool = ["192.0.2.81-83"]\ncheck_free = false\n'

    def test_default_auto_entry_identical(self):
        run, disc = self.run_plan(), self.discover_plan()
        self.assertEqual(len(disc["m1s"]), 1)
        self.assertEqual(self.devs(run), self.devs(disc))
        self.assertEqual(disc["m1s"][0]["m1_ip"], M1IP)


class SameIdsBareAuto(Sandbox):
    TOML = ('[[m1]]\naddress = "auto"\n[push]\npool = ["192.0.2.81-83"]\n'
            'check_free = false\n')

    def test_bare_auto_identical_and_not_duplicated(self):
        run, disc = self.run_plan(), self.discover_plan()
        self.assertEqual(len(disc["m1s"]), 1)
        self.assertEqual(self.devs(run), self.devs(disc))


class SameIdsEnvM1(Sandbox):
    TOML = '[push]\npool = ["192.0.2.81-83"]\ncheck_free = false\n'
    ENV = {"ZB_M1": "auto:acdbda698f08"}

    def test_zb_m1_env_identical(self):
        self.assertEqual(self.devs(self.run_plan()),
                         self.devs(self.discover_plan()))


class SameIdsLiteralIP(Sandbox):
    TOML = ('[[m1]]\naddress = "192.0.2.54"\n[push]\npool = ["192.0.2.81-83"]\n'
            'check_free = false\n')

    def test_literal_ip_identical(self):
        disc = self.discover_plan()
        self.assertEqual(self.devs(self.run_plan()), self.devs(disc))
        self.assertEqual(disc["m1s"][0]["m1_ip"], M1IP)


class ApplyResolvesAuto(Sandbox):
    def test_unicast_goes_to_discovered_ip_not_spec(self):
        plan = self.discover_plan()
        sent = []
        res = m1super.apply_plan(plan, [], apply=True,
                                 send=lambda ip, d, m1: sent.append(m1) or
                                 [("255.255.255.255", None), (m1, None)],
                                 is_local=lambda ip: True)
        self.assertEqual(sent, [M1IP] * 3)
        self.assertTrue(all(r["sent"] and not r["error"] for r in res))

    def test_unresolved_auto_is_broadcast_only_with_note(self):
        cfg = zbconf.apply_env(zbconf.load(self.cfg))
        plan, *_ = m1super.build_plan(cfg, [], create_seed=True)
        sent = []
        res = m1super.apply_plan(plan, [], apply=True,
                                 send=lambda ip, d, m1: sent.append(m1),
                                 is_local=lambda ip: True)
        self.assertEqual(sent, [None] * 3)
        self.assertTrue(all("broadcast only" in r["note"] for r in res))

    def test_m1_ip_is_never_a_push_ip(self):
        plan = self.discover_plan()
        plan["m1s"][0]["brp_devs"][0]["push_ip"] = M1IP
        with self.assertRaises(m1super.ApplyRefused):
            m1super.apply_plan(plan, [], apply=True, send=lambda *a: [],
                               is_local=lambda ip: True)

    def test_send_error_reported_not_raised(self):
        plan = self.discover_plan()

        def bad(ip, d, m1):
            raise socket.gaierror(-2, "Name or service not known")
        res = m1super.apply_plan(plan, [], apply=True, send=bad,
                                 is_local=lambda ip: True)
        self.assertEqual(len(res), 3)
        self.assertTrue(all(not r["sent"] and "gaierror" in r["error"]
                            for r in res))

    def test_udp_sender_keeps_going_per_target(self):
        calls = []

        class S:
            def setsockopt(self, *a): pass
            def bind(self, a): pass

            def sendto(self, d, t):
                calls.append(t)
                if t[0] != "255.255.255.255":
                    raise socket.gaierror(-2, "Name or service not known")

            def close(self): pass
        with mock.patch.object(m1super.socket, "socket", lambda *a, **k: S()):
            out = m1super._udp_sender("192.0.2.81", b"x", "auto:acdbda698f08")
        self.assertEqual(len(calls), 2)
        self.assertIsNone(out[0][1])
        self.assertIn("gaierror", out[1][1])

    def _main_apply(self, send):
        orig = m1super.apply_plan

        def fake(plan, errs, apply=False, send_=None, is_local=None):
            return orig(plan, errs, apply=apply, send=send,
                        is_local=lambda ip: True)
        out = []
        with mock.patch.object(m1super, "apply_plan", fake), \
                mock.patch.object(m1super, "collect",
                                  lambda reg, now=None, source="announce":
                                  found()):
            rc = m1super.main(["plan", "--config", self.cfg, "--apply"],
                              out=out.append)
        return rc, "\n".join(out)

    def test_cli_reports_failure_rc4_no_traceback(self):
        def bad(ip, d, m1):
            return [("255.255.255.255", None), (m1, "gaierror: boom")]
        rc, text = self._main_apply(bad)
        self.assertEqual(rc, 4)
        self.assertIn("PARTLY announced", text)
        self.assertIn("(M1 %s)" % M1IP, text)

    def test_cli_ok_path_names_unicast_target(self):
        rc, text = self._main_apply(lambda ip, d, m1: [("b", None), (m1, None)])
        self.assertEqual(rc, 0)
        self.assertIn("+ unicast to %s" % M1IP, text)
        self.assertIn("no record of a running bridge", text)


class ActiveIdsCrossCheck(Sandbox):
    def test_roundtrip_and_match(self):
        run = self.run_plan()
        path, err = zbconf.write_active(run)
        self.assertIsNone(err)
        with open(path) as fh:
            rec = json.load(fh)
        self.assertNotIn("0123456789abcdef", json.dumps(rec))   # no seed
        self.assertEqual(m1super.check_active(self.discover_plan(), rec), [])

    def test_mismatch_refuses_before_sending(self):
        zbconf.write_active(self.run_plan())
        with open(os.path.join(self.data, "seed"), "w") as fh:
            fh.write("a-different-volume\n")
        sent = []
        out = []
        with mock.patch.object(m1super, "collect",
                               lambda reg, now=None, source="announce": found()), \
                mock.patch.object(m1super, "_udp_sender",
                                  lambda *a: sent.append(a)):
            rc = m1super.main(["plan", "--config", self.cfg, "--apply"],
                              out=out.append)
        self.assertEqual((rc, sent), (3, []))
        self.assertIn("docker compose exec", "\n".join(out))

    def test_entrypoint_run_records_ids(self):
        import signal
        saved = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT))
        self.addCleanup(signal.signal, signal.SIGTERM, saved[0])
        self.addCleanup(signal.signal, signal.SIGINT, saved[1])
        with mock.patch.object(entrypoint.subprocess, "Popen") as P, \
                contextlib.redirect_stdout(io.StringIO()):
            P.return_value.poll.return_value = 0
            entrypoint.main(["entrypoint.py", "run", "--skip-free-check"])
        rec, why = zbconf.read_active()
        self.assertEqual(why, "ok")
        self.assertEqual([m["devs"] for m in rec["m1s"]],
                         self.devs(self.discover_plan()))


class Api8Reason(unittest.TestCase):
    """_lineup_raw keeps the M1's refusal reason (fake BRP replies)."""

    def setUp(self):
        self.saved = {k: getattr(h, k) for k in (
            "brp_call", "brp_handshake", "LINEUP_CACHE_FILES")}
        self.saved_api8 = dict(h._api8)
        self.saved_cache = dict(h._cache)
        self.ready = h._m1_ready.is_set()
        h._m1_ready.set()
        h._cache.update(at=0, channels=[])
        h.LINEUP_CACHE_FILES = [os.path.join(tempfile.mkdtemp(), "none.json")]
        h._api8.update(err=None, err_name=None, handshake=None, hint=None,
                       _logged=None)

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(h, k, v)
        h._api8.clear()
        h._api8.update(self.saved_api8)
        h._cache.clear()
        h._cache.update(self.saved_cache)
        if not self.ready:
            h._m1_ready.clear()

    def fail_with(self, name, hs="NOT_FOUND"):
        h.brp_handshake = lambda: {"err_name": hs}
        h.brp_call = lambda api, **k: {"err_name": name, "body": b""}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(RuntimeError):
                h._lineup_raw(force=True)
        return buf.getvalue()

    def test_not_found_recorded_with_hint(self):
        log = self.fail_with("NOT_FOUND")
        self.assertEqual(h._api8["err_name"], "NOT_FOUND")
        self.assertEqual(h._api8["handshake"], "NOT_FOUND")
        self.assertIn("a0=2 NOT_FOUND", h._api8["err"])
        self.assertIn("discover --apply", h._api8["hint"])
        self.assertIn(h.BRP_DEV, h._api8["hint"])
        self.assertIn("discover --apply", log)

    def test_not_shared_hint(self):
        self.fail_with("NOT_SHARED", hs="NOT_SHARED")
        self.assertIn("turn on sharing", h._api8["hint"])

    def test_device_id_not_allowed_hint(self):
        self.fail_with("DEVICE_ID_NOT_ALLOWED", hs="SUCCESS")
        self.assertEqual(h._api8["err_name"], "DEVICE_ID_NOT_ALLOWED")
        self.assertIn("discover --apply", h._api8["hint"])

    def test_logged_once_per_change(self):
        self.assertIn("refused", self.fail_with("NOT_FOUND"))
        self.assertEqual(self.fail_with("NOT_FOUND"), "")
        self.assertIn("NOT_SHARED", self.fail_with("NOT_SHARED"))

    def test_handshake_exception_and_network_error_kept(self):
        def hs():
            raise OSError("connection refused")
        h.brp_handshake = hs

        def call(api, **k):
            raise OSError("timed out")
        h.brp_call = call
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                h._lineup_raw(force=True)
        self.assertIsNone(h._api8["err_name"])
        self.assertIn("connection refused", h._api8["handshake"])
        self.assertIn("timed out", h._api8["err"])
        self.assertTrue(h._api8["err"].startswith("api8 FAIL on a0 2/3/4/5"))

    def test_success_clears_reason(self):
        self.fail_with("NOT_FOUND")
        h.brp_handshake = lambda: {"err_name": "SUCCESS"}
        body = json.dumps({"channels": [{"chan_id": "x"}]}).encode()
        h.brp_call = lambda api, **k: {"err_name": "SUCCESS", "body": body}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(h._lineup_raw(force=True), [{"chan_id": "x"}])
        self.assertIsNone(h._api8["err_name"])
        self.assertIsNone(h._api8["hint"])


class TesterPost60(Sandbox):
    """Forum post 60: literal address, same toml, `persisted seed file` in
    both outputs, yet different ids. Same seed => identical ids (proved
    here); different ids therefore mean the two containers read different
    /data/seed files (different volumes)."""
    TOML = TESTER_TOML.replace('"auto:acdbda698f08"', '"192.0.2.54"')

    def test_literal_address_plan_equals_facade(self):
        run, disc = self.run_plan(), self.discover_plan()
        self.assertEqual(self.devs(run), self.devs(disc))
        self.assertEqual(self.facade_devs(run)[0][0], self.devs(disc)[0])

    def test_only_a_different_seed_explains_a_mismatch(self):
        run = self.run_plan()
        with open(os.path.join(self.data, "seed"), "w") as fh:
            fh.write("other-volume\n")
        disc = self.discover_plan()
        self.assertNotEqual(self.devs(run), self.devs(disc))
        self.assertNotEqual(run["seed_fp"], disc["seed_fp"])

    def test_address_form_is_part_of_the_key(self):
        # documented behaviour: a literal address keys the ids on the IP,
        # an auto spec on the entry name; changing the form changes the ids
        lit = self.discover_plan()
        with open(self.cfg, "w") as fh:
            fh.write(TESTER_TOML)
        auto = self.discover_plan()
        self.assertNotEqual(self.devs(lit), self.devs(auto))


class ExplicitDevsWorkaround(Sandbox):
    DEVS = ["02bb00a0735a", "02bb003c6c0a", "02bb00c7fbb9"]
    TOML = TESTER_TOML.replace(
        "leases = 3\n",
        'leases = 3\ndevs = ["02bb00a0735a", "02bb003c6c0a", "02bb00c7fbb9"]\n')

    def test_devs_honoured_by_both_paths_even_with_different_seeds(self):
        run = self.run_plan()
        with open(os.path.join(self.data, "seed"), "w") as fh:
            fh.write("other-volume\n")
        disc = self.discover_plan()
        self.assertEqual(self.devs(run), [self.DEVS])
        self.assertEqual(self.devs(disc), [self.DEVS])
        self.assertEqual(self.facade_devs(run)[0][0], self.DEVS)
        sent = []
        m1super.apply_plan(disc, [], apply=True,
                           send=lambda ip, d, m1: sent.append(
                               m1disco.parse(d, ip).dev) or [],
                           is_local=lambda ip: True)
        self.assertEqual(sent, self.DEVS)


if __name__ == "__main__":
    unittest.main()
