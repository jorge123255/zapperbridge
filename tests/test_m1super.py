"""m1super: discovery planner. Offline: fake BRzb packets, fake sender, fake
connector. No socket is ever opened to the LAN or to an M1 (the tests block
sendto/connect to prove it). pytest or unittest."""
import contextlib
import io
import os
import socket
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import m1disco   # noqa: E402
import m1super   # noqa: E402
import zbconf    # noqa: E402

import synth  # noqa: E402
PCAP = synth.brzb_pcap()
M1IP, MINIIP = "192.0.2.10", "192.0.2.237"


def m1pkt(dev="02bb00a00001", ip=M1IP, tuners=4, share=3, model="ZBM1-gt698"):
    return (ip, m1disco.build(0, share, dev, ip, "", model, tuners, 1))


def minipkt(dev="02bb00c00001", ip=MINIIP):
    return (ip, m1disco.build(0, 1, dev, ip, "", "ZBM3-gt316", 0, 0))


def found(*pkts):
    return m1super.collect(m1super.observe_packets(list(pkts)))


class NoNet(unittest.TestCase):
    """Any real send/connect during a test is a failure."""

    def setUp(self):
        self._o = (socket.socket.sendto, socket.socket.connect, socket.socket.send)

        def boom(*a, **k):
            raise AssertionError("network send/connect attempted in a test")
        socket.socket.sendto = boom
        socket.socket.connect = boom
        socket.socket.send = boom

    def tearDown(self):
        socket.socket.sendto, socket.socket.connect, socket.socket.send = self._o


class TunerCount(NoNet):
    def test_evidence_m1(self):
        f = found(m1pkt())[0]
        self.assertEqual((f.kind, f.leases, f.confidence), ("m1", 3, "evidence"))

    def test_mini_is_client(self):
        f = found(minipkt())[0]
        self.assertEqual((f.kind, f.leases, f.confidence), ("client", 0, "none"))

    def test_unconfirmed_values_say_so(self):
        for tuners, want in ((2, 1), (3, 2), (5, 3), (9, 3)):
            f = found(m1pkt(tuners=tuners))[0]
            self.assertEqual(f.leases, want, tuners)
            self.assertEqual(f.confidence, "unconfirmed")
            self.assertIn("never captured", f.note)

    def test_sharing_off(self):
        f = found(m1pkt(share=1))[0]      # tuners=4 but no tuners bit
        self.assertEqual(f.leases, 0)
        self.assertIn("NOT_SHARED", f.note)

    def test_tuners_bit_but_zero(self):
        self.assertEqual(found(m1pkt(tuners=0))[0].leases, 0)

    def test_pcap_fixture(self):
        fs = found(*m1super.packets_from_pcap(PCAP))
        self.assertEqual({f.dev: f.kind for f in fs},
                         {"02bb00a00001": "m1", "02bb00c00001": "client"})

    def test_garbage_ignored(self):
        reg = m1super.observe_packets([("1.2.3.4", b"junk"), ("1.2.3.4", b"BRzb" + b"\0" * 10)])
        self.assertEqual(m1super.collect(reg), [])


class Plan(NoNet):
    def plan(self, cfg=None, *pk):
        return m1super.build_plan(cfg or {}, found(*pk))

    def test_discovered_m1_blocks(self):
        cfg = {"push": {"pool": ["192.0.2.240-249"]}}
        plan, errs, warns, notes, _ = self.plan(cfg, m1pkt(), minipkt())
        self.assertEqual(errs, [])
        m = plan["m1s"][0]
        self.assertEqual((m["m1"], m["tuner_count"], len(m["brp_devs"])), (M1IP, 4, 3))
        self.assertEqual([d["push_ip"] for d in m["brp_devs"]],
                         ["192.0.2.240", "192.0.2.241", "192.0.2.242"])
        self.assertTrue(all(len(d["dev"]) == 12 for d in m["brp_devs"]))
        self.assertIn(MINIIP, plan["reserved_ips"])           # Mini protected
        self.assertTrue(any("protected real client" in n for n in notes))

    def test_two_m1s_get_separate_blocks(self):
        cfg = {"push": {"pool": ["192.0.2.240-249"]}}
        plan, errs, *_ = self.plan(cfg, m1pkt(), m1pkt("02bb0090001", "192.0.2.251"))
        self.assertEqual(errs, [])
        self.assertEqual(len(plan["m1s"]), 2)
        ips = [d["push_ip"] for m in plan["m1s"] for d in m["brp_devs"]]
        devs = [d["dev"] for m in plan["m1s"] for d in m["brp_devs"]]
        self.assertEqual(len(set(ips)), 6)
        self.assertEqual(len(set(devs)), 6)
        self.assertEqual(len({m["device_id"] for m in plan["m1s"]}), 2)

    def test_stable_ids_across_runs(self):
        cfg = {"push": {"pool": ["192.0.2.240-249"]}}
        a = self.plan(cfg, m1pkt())[0]
        b = self.plan(cfg, m1pkt())[0]
        self.assertEqual(a["m1s"], b["m1s"])

    def test_configured_wins_over_announce(self):
        cfg = {"m1": [{"name": "garage", "address": "auto:02bb00a00001", "leases": 2}],
               "push": {"pool": ["192.0.2.240-249"]}}
        plan, errs, _w, notes, _ = self.plan(cfg, m1pkt())
        self.assertEqual(len(plan["m1s"]), 1)          # not duplicated
        self.assertEqual(len(plan["m1s"][0]["brp_devs"]), 2)
        self.assertTrue(any("overrides" in n for n in notes))

    def test_configured_by_ip_gets_leases_from_announce(self):
        cfg = {"m1": [{"address": M1IP}], "push": {"pool": ["192.0.2.240-249"]}}
        plan, *_ = self.plan(cfg, m1pkt())
        self.assertEqual(len(plan["m1s"][0]["brp_devs"]), 3)

    def test_unusable_m1_skipped_with_reason(self):
        plan, errs, warns, notes, cfg2 = self.plan({}, m1pkt(share=1))
        self.assertTrue(any("NOT_SHARED" in n for n in notes))
        self.assertFalse(cfg2["m1"])

    def test_reserved_user_list_kept_and_pool_conflict_errors(self):
        cfg = {"push": {"pool": ["192.0.2.237-239"], "reserved_ips": ["192.0.2.238"]}}
        plan, errs, *_ = self.plan(cfg, m1pkt(), minipkt())
        self.assertTrue(any("reserved" in e for e in errs))
        self.assertEqual(set(plan["reserved_ips"]), {"192.0.2.237", "192.0.2.238"})

    def test_no_default_237_push_ip(self):
        plan, *_ = self.plan({"push": {"pool": ["192.0.2.230-239"]}}, m1pkt())
        ips = [d["push_ip"] for d in plan["m1s"][0]["brp_devs"]]
        self.assertNotIn(MINIIP, ips)       # because the *user list* has it? no: no Mini seen
        # without a Mini announce and an empty reserved list .237 is an ordinary
        # pool address (it is the user's list that protects it), so make sure the
        # plan still hands out in pool order and never invents a default:
        self.assertEqual(ips, ["192.0.2.230", "192.0.2.231", "192.0.2.232"])

    def test_plan_has_no_sends(self):
        plan, *_ = self.plan({"push": {"pool": ["192.0.2.240-249"]}}, m1pkt())
        pv = m1super.announce_preview(plan)       # only builds datagrams
        self.assertEqual(len(pv), 3)
        a = m1disco.parse(bytes.fromhex(pv[0]["announce"]["hex"]), "x")
        self.assertEqual((a.type, a.share, a.tuners, a.ip, a.dev),
                         (0, 1, 0, pv[0]["push_ip"], pv[0]["dev"]))

    def test_render_dry_run_text(self):
        fs = found(m1pkt())
        plan, errs, warns, notes, _ = m1super.build_plan(
            {"push": {"pool": ["192.0.2.240-249"]}}, fs)
        txt = m1super.render(fs, plan, errs, warns, notes)
        self.assertIn("DRY RUN: nothing is sent", txt)
        self.assertIn("would announce", txt)


class Sweep(NoNet):
    def test_sweep_uses_injected_connect_and_skips(self):
        calls = []

        def conn(ip, port):
            calls.append((ip, port))
            return ip in ("192.0.2.10", "192.0.2.5")
        hits = m1super.sweep(["192.0.2.0/29", "192.0.2.10/32"],
                             skip={"192.0.2.1"}, connect=conn)
        self.assertEqual(hits, ["192.0.2.5", "192.0.2.10"])
        self.assertNotIn("192.0.2.1", [c[0] for c in calls])
        self.assertTrue(all(p == m1super.BRP_PORT for _, p in calls))

    def test_sweep_size_cap_and_garbage(self):
        with self.assertRaises(zbconf.ConfigError):
            m1super.sweep(["10.0.0.0/16"], connect=lambda *a: False)
        with self.assertRaises(zbconf.ConfigError):
            m1super.sweep(["nope"], connect=lambda *a: False)

    def test_sweep_never_contacts_reserved_in_cli(self):
        # CLI path: reserved IPs and known M1s are skipped before any connect
        seen = []
        orig = m1super.sweep
        m1super.sweep = lambda c, skip=(), **k: seen.append(set(skip)) or []
        try:
            cfg = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
            cfg.write('[push]\nreserved_ips=["192.0.2.237"]\n')
            cfg.close()
            m1super.main(["plan", "--config", cfg.name, "--from-pcap", PCAP,
                          "--sweep", "192.0.2.0/24"], out=lambda s: None)
        finally:
            m1super.sweep = orig
            os.unlink(cfg.name)
        self.assertIn("192.0.2.237", seen[0])
        self.assertIn(M1IP, seen[0])


class Apply(NoNet):
    def mkplan(self):
        fs = found(m1pkt())
        plan, errs, *_ = m1super.build_plan(
            {"push": {"pool": ["192.0.2.240-249"]}}, fs)
        return plan, errs

    def test_refuses_without_apply(self):
        plan, errs = self.mkplan()
        sent = []
        for flag in (False, None, 1, "yes"):
            with self.assertRaises(m1super.ApplyRefused):
                m1super.apply_plan(plan, errs, apply=flag,
                                   send=lambda *a: sent.append(a))
        self.assertEqual(sent, [])

    def test_apply_sends_one_announce_per_id_from_push_ip(self):
        plan, errs = self.mkplan()
        sent = []
        res = m1super.apply_plan(plan, errs, apply=True,
                                 send=lambda ip, data: sent.append((ip, data)),
                                 is_local=lambda ip: True)
        self.assertEqual(len(res), 3)
        for (ip, data), d in zip(sent, plan["m1s"][0]["brp_devs"]):
            a = m1disco.parse(data, ip)
            self.assertEqual((ip, a.dev, a.ip, a.share, a.tuners),
                             (d["push_ip"], d["dev"], d["push_ip"], 1, 0))

    def test_refuses_when_alias_not_local(self):
        plan, errs = self.mkplan()
        sent = []
        with self.assertRaises(m1super.ApplyRefused):
            m1super.apply_plan(plan, errs, apply=True, send=lambda *a: sent.append(a),
                               is_local=lambda ip: ip != "192.0.2.241")
        self.assertEqual(sent, [])            # nothing partially sent

    def test_refuses_reserved_m1_and_real_dev(self):
        plan, errs = self.mkplan()
        for key, val, field in (("push_ip", MINIIP, "ip"), ("push_ip", M1IP, "ip"),
                                ("dev", "02bb00c00001", "dev")):
            p = {**plan, "reserved_ips": [MINIIP],
                 "m1s": [dict(plan["m1s"][0], brp_devs=[
                     dict(plan["m1s"][0]["brp_devs"][0], **{key: val})])]}
            orig, zbconf.NEVER_DEVS = zbconf.NEVER_DEVS, ("02bb00c00001",)
            try:
                with self.assertRaises(m1super.ApplyRefused):
                    m1super.apply_plan(p, [], apply=True, send=lambda *a: None,
                                       is_local=lambda ip: True)
            finally:
                zbconf.NEVER_DEVS = orig

    def test_refuses_on_errors_and_missing_push_ip(self):
        plan, errs = self.mkplan()
        with self.assertRaises(m1super.ApplyRefused):
            m1super.apply_plan(plan, ["bad"], apply=True, send=lambda *a: None)
        p2, e2, *_ = m1super.build_plan({}, found(m1pkt()))
        with self.assertRaises(m1super.ApplyRefused):
            m1super.apply_plan(p2, e2, apply=True, send=lambda *a: None,
                               is_local=lambda ip: True)

    def test_cli_never_applies_without_flag(self):
        out = []
        rc = m1super.main(["plan", "--config", "/nonexistent", "--from-pcap", PCAP],
                          out=out.append)
        self.assertEqual(rc, 0)
        self.assertIn("DRY RUN", "\n".join(out))

    def test_config_cannot_enable_apply(self):
        cfg = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        cfg.write('[discovery]\napply = true\n[push]\npool=["192.0.2.240-249"]\n')
        cfg.close()
        sent = []
        orig = m1super._udp_sender
        m1super._udp_sender = lambda *a: sent.append(a)
        try:
            out = []
            rc = m1super.main(["plan", "--config", cfg.name, "--from-pcap", PCAP],
                              out=out.append)
        finally:
            m1super._udp_sender = orig
            os.unlink(cfg.name)
        self.assertEqual((rc, sent), (0, []))

    def test_cli_apply_path_uses_sender_only_with_flag(self):
        cfg = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        cfg.write('[push]\npool=["192.0.2.240-249"]\n')
        cfg.close()
        sent = []
        orig = (m1super._udp_sender, zbconf._is_local)
        orig_apply = m1super.apply_plan

        def fake_apply(plan, errs, apply=False, send=None, is_local=None):
            return orig_apply(plan, errs, apply=apply,
                              send=lambda ip, d: sent.append(ip),
                              is_local=lambda ip: True)
        m1super.apply_plan = fake_apply
        try:
            out = []
            rc = m1super.main(["plan", "--config", cfg.name, "--from-pcap", PCAP,
                               "--apply"], out=out.append)
        finally:
            m1super.apply_plan = orig_apply
            os.unlink(cfg.name)
        self.assertEqual(rc, 0)
        self.assertEqual(len(sent), 3)


class Source(unittest.TestCase):
    def test_no_api7_join_or_shell_in_module(self):
        src = open(os.path.join(ROOT, "m1super.py")).read()
        import ast
        tree = ast.parse(src)
        for n in ast.walk(tree):     # drop docstrings, keep real code + literals
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)) and n.body \
                    and isinstance(n.body[0], ast.Expr) \
                    and isinstance(getattr(n.body[0], "value", None), ast.Constant):
                n.body = n.body[1:] or [ast.Pass()]
        code = ast.unparse(tree)
        for bad in ("subprocess", "os.system", "brp_call", "brptune", "api7",
                    "api6(", "ifconfig(", "ip addr add"):
            self.assertNotIn(bad, code, bad)

    def test_entrypoint_discover_is_dry_run(self):
        sys.path.insert(0, os.path.join(ROOT, "docker"))
        import entrypoint
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            os.environ["ZB_CONFIG"] = "/nonexistent"
            try:
                rc = entrypoint.main(["entrypoint.py", "discover", "--from-pcap", PCAP])
            finally:
                os.environ.pop("ZB_CONFIG")
        self.assertEqual(rc, 0)
        self.assertIn("DRY RUN", buf.getvalue())


class UsableRuleTests(unittest.TestCase):
    def test_usable_is_announced_minus_one(self):
        for ann, want in ((0, 0), (1, 0), (2, 1), (4, 3), (6, 5)):
            self.assertEqual(m1super.usable_from_announced(ann), want)

    def test_only_four_is_evidence_and_wording(self):
        f = found(m1pkt(tuners=4))[0]
        self.assertEqual((f.leases, f.confidence), (3, "evidence"))
        self.assertIn("announced - 1", f.note)
        self.assertIn("keeps the 4th", f.note)
        g = found(m1pkt(tuners=3))[0]
        self.assertEqual((g.leases, g.confidence), (2, "unconfirmed"))
        self.assertIn("UNCONFIRMED", g.note)

if __name__ == "__main__":
    unittest.main()
