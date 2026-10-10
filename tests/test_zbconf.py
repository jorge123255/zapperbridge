"""Offline tests for zbconf (Docker config): no sockets, no M1.
Run: python3 tests/test_zbconf.py   (or pytest)
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import m1config  # noqa: E402
import zbconf    # noqa: E402


def build(cfg):
    return zbconf.expand(zbconf.apply_env(cfg, env={}))


def one(pool=("192.0.2.240-249",), **m):
    e = {"address": "192.0.2.10"}
    e.update(m)
    return {"m1": [e], "push": {"pool": list(pool)}}


def with_reserved(cfg, *ips):
    cfg["push"]["reserved_ips"] = list(ips)
    return cfg


class Pool(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(zbconf.parse_pool(["10.0.0.5-7"]),
                         ["10.0.0.5", "10.0.0.6", "10.0.0.7"])
        self.assertEqual(zbconf.parse_pool("10.0.0.1, 10.0.0.1"), ["10.0.0.1"])
        self.assertEqual(zbconf.parse_pool(["10.0.0.0/30"]),
                         ["10.0.0.1", "10.0.0.2"])
        self.assertEqual(len(zbconf.parse_pool(["10.0.0.1-10.0.0.3"])), 3)

    def test_garbage(self):
        for bad in ("nope", "10.0.0.9-10.0.0.1", "10.0.0.1-999"):
            with self.assertRaises(zbconf.ConfigError):
                zbconf.parse_pool([bad])


class Plan(unittest.TestCase):
    def test_single_m1_defaults(self):
        _c, plan, errs, _w = build(one())
        self.assertEqual(errs, [])
        m = plan["m1s"][0]
        self.assertEqual([d["push_ip"] for d in m["brp_devs"]],
                         ["192.0.2.240", "192.0.2.241", "192.0.2.242"])
        self.assertEqual(m["tuner_count"], 4)
        self.assertEqual(plan["tuners_total"], 4)

    def test_ids_stable_unique_and_hex(self):
        a = build(one())[1]["m1s"][0]["brp_devs"]
        b = build(one())[1]["m1s"][0]["brp_devs"]
        self.assertEqual(a, b)
        devs = [d["dev"] for d in a]
        self.assertEqual(len(set(devs)), 3)
        for d in devs:
            self.assertRegex(d, r"^02bb00[0-9a-f]{6}$")

    def test_seed_changes_ids(self):
        c = one()
        c["push"]["device_seed"] = "other"
        self.assertNotEqual(build(c)[1]["m1s"][0]["brp_devs"],
                            build(one())[1]["m1s"][0]["brp_devs"])

    def test_two_m1s_disjoint_blocks(self):
        c = {"m1": [{"address": "192.0.2.10", "advertise_ip": "192.0.2.60"},
                    {"address": "192.0.2.251", "advertise_ip": "192.0.2.61"}],
             "push": {"pool": ["192.0.2.240-249"]}}
        _c, plan, errs, _w = build(c)
        self.assertEqual(errs, [])
        devs = [d["dev"] for m in plan["m1s"] for d in m["brp_devs"]]
        ips = [d["push_ip"] for m in plan["m1s"] for d in m["brp_devs"]]
        self.assertEqual(len(set(devs)), 6)
        self.assertEqual(len(set(ips)), 6)
        self.assertEqual(plan["tuners_total"], 8)

    def test_tuner_cap(self):
        c = {"m1": [{"address": "192.0.2.10"},
                    {"address": "192.0.2.251", "advertise_ip": "192.0.2.61"}],
             "push": {"pool": ["192.0.2.240-249"]}, "hdhr": {"tuner_cap": 5}}
        plan = build(c)[1]
        self.assertEqual([m["tuner_count"] for m in plan["m1s"]], [4, 1])
        self.assertEqual(plan["tuners_total"], 5)

    def test_no_m1_block_means_auto(self):
        _c, plan, errs, warns = build({"push": {"pool": ["192.0.2.240-242"]}})
        self.assertEqual(errs, [])
        self.assertEqual(plan["m1s"][0]["m1"], "auto")
        self.assertTrue(any("auto-discovered" in w for w in warns))

    def test_env_overrides(self):
        cfg = zbconf.apply_env({}, env={"ZB_M1": "192.0.2.10",
                                        "ZB_PUSH_POOL": "192.0.2.240-242",
                                        "ZB_TUNER_CAP": "2"})
        _c, plan, errs, _w = zbconf.expand(cfg)
        self.assertEqual(errs, [])
        self.assertEqual(plan["tuners_total"], 2)


class Rejections(unittest.TestCase):
    def has(self, errs, text):
        self.assertTrue(any(text in e for e in errs), errs)

    def test_reserved_in_pool(self):
        c = with_reserved(one(pool=("192.0.2.236-239",)), "192.0.2.237")
        _c, _p, errs, _w = build(c)
        self.has(errs, "192.0.2.237")

    def test_reserved_explicit(self):
        c = with_reserved(one(pool=(), push_ips=["192.0.2.237",
                                                 "192.0.2.241",
                                                 "192.0.2.242"]),
                          "192.0.2.237")
        self.has(build(c)[2], ".237")

    def test_reserved_range_and_legacy_never_ips(self):
        c = one(pool=("192.0.2.240-243",))
        c["push"]["reserved_ips"] = ["192.0.2.240-241"]
        c["push"]["never_ips"] = ["192.0.2.243"]
        _c, plan, errs, _w = build(c)
        self.has(errs, "192.0.2.240")
        self.assertEqual(plan["reserved_ips"],
                         ["192.0.2.240", "192.0.2.241", "192.0.2.243"])

    def test_237_not_hardcoded_in_docker_layer(self):
        # nothing reserved => .237 is just an address (legacy m1config
        # default still protects the live facade, not this layer)
        c = one(pool=("192.0.2.236-239",))
        _c, plan, errs, _w = build(c)
        self.assertEqual(errs, [])
        self.assertEqual(plan["reserved_ips"], [])
        self.assertEqual(_c["known_real_minis"], [])
        self.assertIn("192.0.2.237",
                      [d["push_ip"] for d in plan["m1s"][0]["brp_devs"]] +
                      plan["pool"])

    def test_legacy_m1config_default_intact(self):
        self.assertEqual(m1config.DEFAULT_REAL_MINIS, ())   # no built-in real-device defaults
        errs, _w = m1config.validate({"known_real_minis": ["192.0.2.237"], "m1s": [{
            "name": "a", "m1": "192.0.2.10", "device_id": "AABBCCDD",
            "brp_devs": [{"dev": "02bb00000001",
                          "push_ip": "192.0.2.237"}]}]})
        self.assertTrue(any("237" in e for e in errs), errs)

    def test_bad_reserved_entry(self):
        c = with_reserved(one(), "nope")
        self.has(build(c)[2], "reserved_ips")

    def test_reserved_env(self):
        cfg = zbconf.apply_env(one(pool=("192.0.2.236-239",)),
                               env={"ZB_RESERVED_IPS": "192.0.2.237"})
        self.has(zbconf.expand(cfg)[2], ".237")

    def test_m1_own_ip_as_push(self):
        _c, _p, errs, _w = build(one(pool=("192.0.2.10-252",)))
        self.has(errs, "M1's address")

    def test_duplicate_push_ips(self):
        _c, _p, errs, _w = build(one(pool=(), push_ips=["192.0.2.240"] * 3))
        self.has(errs, "shared by")

    def test_duplicate_dev_ids(self):
        _c, _p, errs, _w = build(one(devs=["02bb00000001"] * 3))
        self.has(errs, "already used")

    def test_real_mini_dev(self):
        cfg = one(devs=["02bb00c00001", "02bb00000002", "02bb00000003"])
        cfg["push"]["never_devs"] = ["02bb00c00001"]
        _c, _p, errs, _w = build(cfg)
        self.has(errs, "real device")

    def test_empty_pool_is_warning_not_error(self):
        _c, _p, errs, warns = build(one(pool=()))
        self.assertEqual(errs, [])
        self.assertTrue(any("pool is empty" in w for w in warns))

    def test_clash_wildcard_vs_specific(self):
        c = {"m1": [{"address": "192.0.2.10"},
                    {"address": "192.0.2.251", "advertise_ip": "192.0.2.61",
                     "http_port": 80}],
             "push": {"pool": ["192.0.2.240-249"]}}
        self.has(build(c)[2], "already used")

    def test_pool_exhausted(self):
        _c, _p, errs, _w = build(one(pool=("192.0.2.240-241",)))
        self.has(errs, "exhausted")

    def test_leases_range(self):
        _c, _p, errs, _w = build(one(leases=4))
        self.has(errs, "outside 1-3")

    def test_macvlan_needs_advertise(self):
        c = one()
        c["network"] = {"mode": "macvlan"}
        self.has(build(c)[2], "advertise_ip")

    def test_bad_modes(self):
        c = one()
        c["network"] = {"mode": "bridge"}
        c["hdhr"] = {"mode": "merged"}
        c["ui"] = {"mode": "admin"}
        errs = build(c)[2]
        self.has(errs, "network.mode")
        self.has(errs, "token")

    def test_duplicate_m1_address(self):
        c = {"m1": [{"address": "192.0.2.10"}, {"address": "192.0.2.10"}],
             "push": {"pool": ["192.0.2.240-249"]}}
        self.has(build(c)[2], "already used")

    def test_bad_prefix(self):
        c = one()
        c["push"]["device_prefix"] = "zz"
        self.has(build(c)[2], "device_prefix")


class Output(unittest.TestCase):
    def test_env_matches_m1config(self):
        _c, plan, errs, _w = build(one(friendly_name="Den"))
        self.assertEqual(errs, [])
        (name, env), = zbconf.launch_envs(plan)
        m = plan["m1s"][0]
        self.assertEqual(env, dict(m1config.env_for(m), ZUI_MODE="off"))
        self.assertEqual(env["ZM1"], "192.0.2.10")
        self.assertIn("ZPUSH_IPS", env)
        self.assertEqual(env["HDHR_FRIENDLY"], "Den")

    def test_render_and_hints_never_execute(self):
        _c, plan, errs, warns = build(one())
        txt = zbconf.render_plan(plan, errs, warns)
        self.assertIn("ip addr add 192.0.2.240/32", txt)
        self.assertIn("push 192.0.2.241:31061", txt)

    def test_example_file_parses(self):
        cfg = zbconf.load(os.path.join(ROOT, "zapperbridge.example.toml"))
        _c, plan, errs, _w = build(cfg)
        self.assertEqual(plan["m1s"][0]["m1"], "auto")
        self.assertEqual(errs, [])

    def test_example_with_m1_and_pool(self):
        cfg = zbconf.load(os.path.join(ROOT, "zapperbridge.example.toml"))
        cfg["m1"] = [{"address": "192.0.2.10"}]
        cfg["push"]["pool"] = ["192.0.2.240-249"]
        self.assertEqual(build(cfg)[2], [])


class Entrypoint(unittest.TestCase):
    def test_check_ok_and_bad(self):
        import subprocess
        ep = os.path.join(ROOT, "docker", "entrypoint.py")
        with tempfile.TemporaryDirectory() as d:
            good = os.path.join(d, "g.toml")
            open(good, "w").write(
                '[[m1]]\naddress="192.0.2.10"\n[push]\n'
                'pool=["192.0.2.240-242"]\n')
            bad = os.path.join(d, "b.toml")
            open(bad, "w").write(
                '[[m1]]\naddress="192.0.2.10"\n[push]\n'
                'pool=["192.0.2.237","192.0.2.241","192.0.2.242"]\n')
            open(bad, "a").write('reserved_ips=["192.0.2.237"]\n')
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith("ZB_")}
            r = subprocess.run([sys.executable, ep, "check"], capture_output=True,
                               env=dict(env, ZB_CONFIG=good,
                                        ZB_CHECK_FREE="0"))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            r = subprocess.run([sys.executable, ep, "check"], capture_output=True,
                               env=dict(env, ZB_CONFIG=bad))
            self.assertEqual(r.returncode, 2)
            self.assertIn(b"refusing to start", r.stderr)


class FreeCheck(unittest.TestCase):
    class R:
        def __init__(self, rc=0, out=b""):
            self.returncode, self.stdout = rc, out

    def probe(self, ping_rc=1, neigh=b"", local=False, tools=("ping", "ip")):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            if cmd[0].endswith("ping"):
                return self.R(ping_rc)
            return self.R(0, neigh)
        which = lambda n: "/bin/" + n if n in tools else None  # noqa: E731
        res = zbconf.probe_ip("192.0.2.240", run=run, which=which,
                              is_local=lambda ip: local, platform="linux")
        return res, calls

    def test_free(self):
        (state, _d), calls = self.probe()
        self.assertEqual(state, "free")
        self.assertEqual(calls[0][1:3], ["-c", "1"])  # one echo only

    def test_ping_answer(self):
        self.assertEqual(self.probe(ping_rc=0)[0][0], "in-use")

    def test_arp_entry(self):
        n = b"192.0.2.240 dev eth0 lladdr aa:bb:cc:dd:ee:ff REACHABLE"
        self.assertEqual(self.probe(neigh=n)[0][0], "in-use")

    def test_arp_failed_is_free(self):
        n = b"192.0.2.240 dev eth0  FAILED"
        self.assertEqual(self.probe(neigh=n)[0][0], "free")

    def test_local_alias_not_probed(self):
        res, calls = self.probe(local=True)
        self.assertEqual(res[0], "local")
        self.assertEqual(calls, [])

    def test_no_tools_unknown_not_inuse(self):
        self.assertEqual(self.probe(tools=())[0][0], "unknown")

    def test_check_free_skips_m1_and_reserved_and_never_sends_to_them(self):
        c = with_reserved(one(pool=("192.0.2.240-242",)), "192.0.2.241")
        # reserved .241 is an error in the plan, but the probe must still
        # never touch it; m1 .250 isn't in the pool anyway
        _c, plan, _e, _w = build(c)
        seen = []

        def probe(ip):
            seen.append(ip)
            return ("in-use", "answered ping") if ip == "192.0.2.242" \
                else ("free", "")
        used, notes = zbconf.check_free(plan, probe=probe)
        self.assertNotIn("192.0.2.241", seen)
        self.assertNotIn("192.0.2.10", seen)
        self.assertEqual(used, ["192.0.2.242 (answered ping)"])

    def test_plan_flags_default_and_off(self):
        self.assertEqual(build(one())[1]["check_free"],
                         {"enabled": True, "fail": True})
        c = one()
        c["push"]["check_free"] = False
        self.assertFalse(build(c)[1]["check_free"]["enabled"])
        cfg = zbconf.apply_env(one(), env={"ZB_CHECK_FREE": "0"})
        self.assertFalse(zbconf.expand(cfg)[1]["check_free"]["enabled"])
        c["push"]["check_free"] = "yes"
        self.assertTrue(any("check_free" in e for e in build(c)[2]))


class EntrypointFree(unittest.TestCase):
    def run_main(self, probe, argv=("ep", "check"), **push):
        import importlib.util
        import io
        import contextlib
        spec = importlib.util.spec_from_file_location(
            "zb_entry", os.path.join(ROOT, "docker", "entrypoint.py"))
        ep = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ep)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "z.toml")
            extra = "".join("%s=%s\n" % (k, v) for k, v in push.items())
            open(p, "w").write('[[m1]]\naddress="192.0.2.10"\n[push]\n'
                               'pool=["192.0.2.240-242"]\n' + extra)
            old = dict(os.environ)
            os.environ["ZB_CONFIG"] = p
            os.environ.pop("ZB_CHECK_FREE", None)
            orig = zbconf.probe_ip
            zbconf.probe_ip = probe
            # check_free binds probe at def time; pass through explicitly
            real_cf = zbconf.check_free
            zbconf.check_free = lambda plan: real_cf(plan, probe=probe)
            out = io.StringIO()
            err = io.StringIO()
            try:
                with contextlib.redirect_stdout(out), \
                        contextlib.redirect_stderr(err):
                    rc = ep.main(list(argv))
            finally:
                zbconf.probe_ip = orig
                zbconf.check_free = real_cf
                os.environ.clear()
                os.environ.update(old)
        return rc, out.getvalue(), err.getvalue()

    def test_in_use_fails(self):
        rc, out, err = self.run_main(lambda ip: ("in-use", "answered ping")
                                     if ip.endswith(".241") else ("free", ""))
        self.assertEqual(rc, 2)
        self.assertIn("192.0.2.241", out)
        self.assertIn("already in use", err)

    def test_in_use_warn_only(self):
        rc, out, _e = self.run_main(lambda ip: ("in-use", "x"),
                                    check_free_fail="false")
        self.assertEqual(rc, 0)
        self.assertIn("WARN", out)

    def test_skip_flag_and_off_never_probe(self):
        def boom(ip):
            raise AssertionError("probed")
        self.assertEqual(self.run_main(boom, argv=("ep", "check",
                                                   "--skip-free-check"))[0], 0)
        self.assertEqual(self.run_main(boom, check_free="false")[0], 0)

    def test_all_free_ok(self):
        self.assertEqual(self.run_main(lambda ip: ("free", ""))[0], 0)



class Merged(unittest.TestCase):
    def cfg(self, **hd):
        c = {"m1": [{"address": "192.0.2.10", "name": "a"},
                    {"address": "192.0.2.251", "name": "b"}],
             "push": {"pool": ["192.0.2.240-249"]},
             "hdhr": dict({"mode": "merged"}, **hd)}
        return c

    def test_merged_accepted_plan_only(self):
        _c, plan, errs, warns = build(self.cfg())
        self.assertEqual(errs, [])
        self.assertTrue(any("plan-only" in w for w in warns))
        self.assertEqual(plan["merged"]["usable"], {"a": 3, "b": 3})
        self.assertEqual(plan["merged"]["tuner_count"], 6)
        self.assertEqual(plan["tuners_total"], 6)
        self.assertIn("MERGED HDHR", zbconf.render_plan(plan, errs, warns))

    def test_merged_cap_and_leases(self):
        c = self.cfg(tuner_cap=4)
        self.assertEqual(build(c)[1]["tuners_total"], 4)
        c = self.cfg()
        c["m1"][1]["leases"] = 1
        self.assertEqual(build(c)[1]["merged"]["tuner_count"], 4)

    def test_per_m1_default_has_no_merged(self):
        c = self.cfg()
        c["hdhr"] = {}
        plan = build(c)[1]
        self.assertIsNone(plan["merged"])
        self.assertEqual(plan["tuners_total"], 8)  # 2 x (3 + HLS)

    def test_bad_device_id(self):
        self.has(build(self.cfg(device_id="xyz"))[2], "device_id")

    def has(self, items, text):
        self.assertTrue(any(text in x for x in items), (text, items))

    def test_entrypoint_refuses_run(self):
        import importlib.util
        import io
        import contextlib
        spec = importlib.util.spec_from_file_location(
            "ep", os.path.join(ROOT, "docker", "entrypoint.py"))
        ep = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ep)
        import tomllib  # noqa: F401
        p = os.path.join(tempfile.mkdtemp(), "z.toml")
        with open(p, "w") as f:
            f.write('[[m1]]\naddress="192.0.2.10"\n[[m1]]\n'
                    'address="192.0.2.251"\n[push]\n'
                    'pool=["192.0.2.240-249"]\ncheck_free=false\n'
                    '[hdhr]\nmode="merged"\n')
        os.environ["ZB_CONFIG"] = p
        try:
            for cmd, want in (("check", 0), ("run", 2)):
                with contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(io.StringIO()):
                    rc = ep.main(["ep", cmd])
                self.assertEqual(rc, want, cmd)
        finally:
            del os.environ["ZB_CONFIG"]

if __name__ == "__main__":
    unittest.main()


class UiWiring(unittest.TestCase):
    def test_default_off_sets_no_zui(self):
        _c, plan, errs, _w = build(one())
        (_n, env), = zbconf.launch_envs(plan)
        self.assertEqual(env["ZUI_MODE"], "off")
        self.assertNotIn("ZUI", env)

    def test_readonly_wires_zui_and_reserved(self):
        c = with_reserved(one(), "192.0.2.237")
        c["ui"] = {"mode": "readonly"}
        _c, plan, errs, warns = build(c)
        self.assertEqual(errs, [])
        (_n, env), = zbconf.launch_envs(plan)
        self.assertEqual(env["ZUI"], "readonly")
        self.assertEqual(env["ZUI_RESERVED_IPS"], "192.0.2.237")
        self.assertTrue(any("ZUI_TOKEN" in w for w in warns), warns)

    def test_short_token_rejected(self):
        c = one()
        c["ui"] = {"mode": "readonly"}
        old = os.environ.get("ZUI_TOKEN")
        os.environ["ZUI_TOKEN"] = "abc"
        try:
            self.assertTrue(any("ZUI_TOKEN" in e for e in build(c)[2]))
        finally:
            if old is None:
                os.environ.pop("ZUI_TOKEN")
            else:
                os.environ["ZUI_TOKEN"] = old

    def test_reqlog_env_is_a_path(self):
        c = one()
        c["reqlog"] = {"enabled": True}
        _c, plan, errs, _w = build(c)
        (_n, env), = zbconf.launch_envs(plan)
        self.assertTrue(env["ZREQLOG"].endswith(".jsonl"), env["ZREQLOG"])


class SsdpOption(unittest.TestCase):
    def _env(self, c, env=None):
        c = zbconf.apply_env(c, env=env or {})
        _c, plan, errs, _w = zbconf.expand(c)
        self.assertEqual(errs, [])
        (_n, e), = zbconf.launch_envs(plan)
        return e

    def test_default_unchanged(self):
        self.assertNotIn("HDHR_SSDP", self._env(one()))

    def test_config_false_disables(self):
        c = one()
        c.setdefault("network", {})["ssdp"] = False
        self.assertEqual(self._env(c)["HDHR_SSDP"], "0")

    def test_env_off_disables_and_on_keeps(self):
        self.assertEqual(self._env(one(), {"ZB_SSDP": "0"})["HDHR_SSDP"], "0")
        self.assertEqual(self._env(one(), {"ZB_SSDP": "off"})["HDHR_SSDP"], "0")
        self.assertNotIn("HDHR_SSDP", self._env(one(), {"ZB_SSDP": "1"}))
        c = one()
        c.setdefault("network", {})["ssdp"] = False
        self.assertNotIn("HDHR_SSDP", self._env(c, {"ZB_SSDP": "true"}))
