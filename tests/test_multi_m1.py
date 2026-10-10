"""Per-user lineup / guide mapping, M1 auto-discovery and multi-M1 config.

Offline only: fixtures are real api8 lists and the two BRzb
announces (a synthetic pcap built at test time, tests/synth.py). Nothing here talks to an M1,
binds :80/:5004/:31061, or sends a datagram off-host (the listener test
uses loopback). Run under pytest or standalone:
    python3 tests/test_multi_m1.py
"""
import json
import os
import socket
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
sys.path.insert(0, os.path.dirname(HERE))
_tmp = tempfile.mkdtemp(prefix="zb-test-")
os.environ["ZEPG_CACHE"] = os.path.join(_tmp, "epg-cache.xml")
os.environ.pop("ZM1", None)
os.environ.pop("ZEPG_ID_PREFIX", None)

import epg as epgmod          # noqa: E402
import hdhr_emulator as h     # noqa: E402
import lineup as lu           # noqa: E402
import m1config               # noqa: E402
import m1disco                # noqa: E402

HOST = "192.0.2.10"


def chans(name):
    with open(os.path.join(FIX, name)) as fh:
        return json.load(fh)["channels"]


def with_hls(cs):
    return cs + [h.HLS_CHANNEL]


def ch(major, minor, call, transport=0x130, hidden=False):
    cid = "0x1%03x%06x%03x%03x" % (transport, 0, major, minor)
    return {"chan_id": cid, "chan_major": major, "chan_minor": minor,
            "call_letters": call, "hidden": hidden, "encrypted": False,
            "protocol": "ATSC-1", "v_res": 1080}


def fake_fetch(c):
    if not str(c.get("chan_id", "")).startswith("0x"):
        return None
    return {"program_list": [{"title": "T %s" % lu.guide_number(c),
                              "start_time": 1791500000,
                              "end_time": 1791501800}]}


# ---------------------------------------------------------------- lineup ----
def test_lineup_version_matches_live_values():
    """LineupVersion/ETag equal what the live facade served for these lists
    (STATUS: 2252539816/864307a8b9a60f45 at 51 ch, 2840204313/a94a1419aa4a5533
    at 86 ch), so clients see no spurious rescan after this change."""
    c51, c86 = with_hls(chans("api8-market-a-51.json")), with_hls(chans("api8-market-a-86.json"))
    assert h.lineup_version(c51) == 2252539816
    assert h.lineup_hash(c51)[:16] == "864307a8b9a60f45"
    assert h.lineup_version(c86) == 2840204313
    assert h.lineup_hash(c86)[:16] == "a94a1419aa4a5533"


def test_outputs_byte_identical_to_main():
    """lineup.json / lineup.m3u / playlist.m3u / XMLTV are byte-identical to
    the output of main @ 9b15358 for the 86-channel list (+999.1)."""
    with open(os.path.join(FIX, "golden-market-a-86.json")) as fh:
        g = json.load(fh)
    c = with_hls(chans("api8-market-a-86.json"))
    assert json.dumps(h.lineup_rows(c, HOST)) == g["lineup_json"]
    assert h.build_lineup_m3u(c, HOST, HOST) == g["lineup_m3u"]
    assert h.build_m3u(c, HOST) == g["playlist_m3u"]
    assert h.build_xmltv_for(c, fake_fetch)[0] == g["xmltv"]


def test_other_market_lineup_has_only_its_own_channels():
    """No hardcoded list: a different M1's api8 rows are all that is served."""
    denver = [ch(4, 1, "KCNC-HD"), ch(7, 1, "KMGH-HD", 0x150),
              ch(9, 1, "KUSA-HD", 0x160), ch(9, 2, "WX", 0x160)]
    rows = h.lineup_rows(with_hls(denver), HOST)
    assert [r["GuideNumber"] for r in rows] == ["4.1", "7.1", "9.1", "9.2", "999.1"]
    assert not any(r["GuideName"] in ("CBS2-HD", "NBC5") for r in rows)
    xml = h.build_xmltv_for(with_hls(denver), fake_fetch)[0]
    assert 'id="zb-4.1"' in xml and 'id="zb-2.1"' not in xml
    assert h.lineup_version(with_hls(denver)) != h.lineup_version(
        with_hls(chans("api8-market-a-86.json")))


def test_hidden_rows_not_served():
    cs = [ch(2, 1, "A"), ch(2, 2, "B", hidden=True)]
    assert [r.gn for r in lu.rows(cs)] == ["2.1"]


def test_duplicate_guide_number_gets_distinct_url_and_guide_id():
    """Fringe reception: two stations both 2.1 on different RF transports."""
    a, b = ch(2, 1, "WBBM-HD", 0x130), ch(2, 1, "WSJV-HD", 0x1a0)
    rs = lu.rows([a, b])
    assert [r.key for r in rs] == ["v2.1", "zb" + b["chan_id"]]
    assert [r.xmltv_id for r in rs] == ["zb-2.1", "zb-2.1-t1a0"]
    assert rs[0].name != rs[1].name
    assert lu.find([a, b], "v2.1") is a
    assert lu.find([a, b], "zb" + b["chan_id"]) is b
    rows = h.lineup_rows([a, b], HOST)
    assert len({r["URL"] for r in rows}) == 2
    xml = h.build_xmltv_for([a, b], fake_fetch)[0]
    assert xml.count("<channel ") == 2 and 'channel="zb-2.1-t1a0"' in xml
    assert lu.lineup_hash([a, b]) != lu.lineup_hash([a])


def test_duplicate_gn_same_call_letters_still_unique_names():
    a, b = ch(2, 1, "SAME", 0x130), ch(2, 1, "SAME", 0x1a0)
    names = [r.name for r in lu.rows([a, b])]
    assert len(set(names)) == 2


def test_find_major_only_and_unknown():
    cs = [ch(5, 1, "NBC5"), ch(5, 2, "COZI")]
    assert lu.find(cs, "v5") is cs[0]
    assert lu.find(cs, "v5.2") is cs[1]
    assert lu.find(cs, "v6.1") is None
    assert lu.find(cs, "") is None and lu.find(cs, "favicon.ico") is None


def test_chan_fields_decode():
    f = lu.chan_fields("0x1180000000020001")
    assert f == {"atsc": 1, "transport": 0x180, "major": 32, "minor": 1}
    assert lu.chan_fields("hls:1") is None


def test_xmltv_prefix_for_side_by_side_m1s():
    cs = [ch(2, 1, "CBS2-HD")]
    rs = lu.rows(cs, "m1b")
    assert rs[0].xmltv_id == "zb-m1b-2.1"


def test_placeholder_off_by_default_and_opt_in():
    cs = [ch(2, 1, "HAS"), ch(8, 1, "NOGUIDE")]

    def fetch(c):
        return fake_fetch(c) if c["chan_major"] == 2 else None
    xml, ok, n = epgmod.build_xmltv(cs, fetch, lu.guide_number,
                                    lambda c: c["call_letters"])
    assert ok == 1 and n == 1 and 'channel="zb-8.1"' not in xml
    xml, ok, n = epgmod.build_xmltv(cs, fetch, lu.guide_number,
                                    lambda c: c["call_letters"],
                                    placeholder_hours=6, now=1791500000)
    assert ok == 1, "placeholders never count as guide coverage"
    assert n == 1 + 7 * 4 and xml.count('channel="zb-8.1"') == 28


# ---------------------------------------------------------------- caches ----
def test_lineup_cache_is_per_m1():
    p = os.path.join(_tmp, "lineup-cache.json")
    with open(p, "w") as fh:
        json.dump({"m1": "198.51.100.7", "channels": [ch(2, 1, "X")]}, fh)
    assert h.load_lineup_cache(p, "192.0.2.10") is None
    assert h.load_lineup_cache(p, "198.51.100.7")[0]["call_letters"] == "X"
    with open(p, "w") as fh:          # legacy untagged cache still accepted
        json.dump({"channels": [ch(2, 1, "Y")]}, fh)
    assert h.load_lineup_cache(p, "192.0.2.10")[0]["call_letters"] == "Y"


def test_epg_cache_is_per_m1():
    path = os.path.join(_tmp, "epg-owner.xml")
    good = '<?xml version="1.0"?>\n<tv>\n</tv>\n'
    c = epgmod.EpgCache(path=path, owner="198.51.100.7")
    assert c.refresh(builder=lambda: (good, 1, 0))
    assert epgmod.EpgCache(path=path, owner="198.51.100.7").xml == good
    assert epgmod.EpgCache(path=path, owner="192.0.2.10").xml is None
    os.remove(path + ".meta.json")    # legacy: no meta -> loaded
    assert epgmod.EpgCache(path=path, owner="192.0.2.10").xml == good


# ------------------------------------------------------------- discovery ----
import synth  # noqa: E402
_PCAP = synth.brzb_pcap()


def _fixture_announces():
    pk = m1disco.read_pcap_udp(_PCAP)
    return [(t, m1disco.parse(d, src)) for t, src, d in pk]


def test_pcap_announces_parse():
    anns = _fixture_announces()
    assert len(anns) == 2
    m1 = next(a for _, a in anns if a.dev == "02bb00a00001")
    mini = next(a for _, a in anns if a.dev == "02bb00c00001")
    assert (m1.model, m1.tuners, m1.share, m1.type) == ("ZBM1-gt698", 4, 3, 0)
    assert m1disco.is_tuner_box(m1) and not m1disco.is_client(m1)
    assert m1disco.address(m1) == "192.0.2.10"
    assert (mini.model, mini.tuners, mini.share) == ("ZBM3-gt316", 0, 1)
    assert m1disco.is_client(mini) and not m1disco.is_tuner_box(mini)


def test_build_roundtrips_captured_bytes():
    for _t, src, data in m1disco.read_pcap_udp(
            _PCAP):
        a = m1disco.parse(data, src)
        assert m1disco.build(a.type, a.share, a.dev, a.ip, a.name, a.model,
                             a.tuners, a.sub, a.token) == data


def test_build_announce_matches_real_mini_shape():
    """Client ANNOUNCE has the captured Mini's shape (share=1, tuners=0)."""
    for _t, src, data in m1disco.read_pcap_udp(
            _PCAP):
        a = m1disco.parse(data, src)
        if m1disco.is_client(a):
            assert m1disco.build_announce(a.dev, a.ip, a.model, a.name,
                                          a.token) == data
    b = m1disco.parse(m1disco.build_announce("02bb00000004", "192.0.2.5"))
    assert (b.type, b.share, b.tuners, b.ip) == (0, 1, 0, "192.0.2.5")


def test_parse_rejects_non_brzb():
    assert m1disco.parse(b"BRzb" + bytes(10)) is None
    assert m1disco.parse(b"XXXX" + bytes(88)) is None
    d = m1disco.build_discover("e2c799f80007", "192.0.2.1")
    assert m1disco.parse(d).type == 3


def test_registry_ignores_discover_and_pick_single_m1():
    reg = m1disco.Registry(max_age=1e12)
    for t, a in _fixture_announces():
        reg.observe(a, now=t)
    reg.observe(m1disco.parse(m1disco.build_discover("aabbccddeeff", "192.0.2.9")))
    assert [a.dev for a in reg.m1s(now=0)] == ["02bb00a00001"]
    assert [a.dev for a in reg.clients(now=0)] == ["02bb00c00001"]
    a, why = m1disco.pick_m1(reg, "auto", now=0)
    assert a.dev == "02bb00a00001" and m1disco.address(a) == "192.0.2.10"
    assert m1disco.pick_m1(reg, "auto:02bb00a00001", now=0)[0] is a
    assert m1disco.pick_m1(reg, "192.0.2.10", now=0)[0] is a
    assert m1disco.pick_m1(reg, "auto:02bb0000000", now=0)[0] is None


def test_pick_m1_never_guesses_between_two():
    reg = m1disco.Registry(max_age=1e12)
    for dev, ip in (("02bb00a00001", "192.0.2.10"),
                    ("02bb0090001", "192.0.2.251")):
        reg.observe(m1disco.parse(m1disco.build(0, 3, dev, ip, "", "ZBM1-gt698", 4, 1),
                                  ip), now=0)
    a, why = m1disco.pick_m1(reg, "auto", now=0)
    assert a is None and why.startswith("ambiguous")
    assert m1disco.pick_m1(reg, "auto:02bb0090001", now=0)[0].ip == "192.0.2.251"


def test_registry_expiry_and_src_ip_wins():
    reg = m1disco.Registry(max_age=60)
    a = m1disco.parse(m1disco.build(0, 3, "02bb00a00001", "10.0.0.5", "",
                                    "ZBM1-gt698", 4, 1), "192.0.2.10")
    reg.observe(a, now=1000)
    assert m1disco.address(reg.m1s(now=1030)[0]) == "192.0.2.10"
    assert reg.m1s(now=1061) == []


def test_resolve_literal_ip_needs_no_listener():
    assert m1disco.resolve_m1("192.0.2.10", wait=0) == (
        "192.0.2.10", None, "configured")


def test_listener_loopback_end_to_end():
    lst = m1disco.Listener(port=0, bind_ip="127.0.0.1").start()
    try:
        port = lst.sock.getsockname()[1]
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto(m1disco.build(0, 3, "02bb00a00001", "127.0.0.1", "",
                               "ZBM1-gt698", 4, 1), ("127.0.0.1", port))
        s.close()
        ip, dev, why = m1disco.resolve_m1("auto", wait=3, listener=lst, poll=0.05)
        assert (ip, dev) == ("127.0.0.1", "02bb00a00001"), why
    finally:
        lst.shutdown()


# ---------------------------------------------------------------- config ----
EXAMPLE = {"m1s": [{
    "name": "living-room", "m1": "192.0.2.10", "m1_dev": "02bb00a00001",
    "device_id": "02BB0001", "advertise_ip": "192.0.2.30",
    "http_port": 80, "ts_port": 5004,
    "brp_devs": [{"dev": "02bb00c00001", "push_ip": "192.0.2.30"},
                 {"dev": "02bb00000002", "push_ip": "192.0.2.242"},
                 {"dev": "02bb00000003", "push_ip": "192.0.2.243"}],
    "hls_tuner": True, "tuner_count": 4}]}


def test_example_config_reproduces_live_launch():
    errs, warns = m1config.validate(EXAMPLE, client_ips={"192.0.2.237"},
                                    m1_ips={"192.0.2.10"})
    assert errs == [] and warns == []
    env = m1config.env_for(EXAMPLE["m1s"][0])
    # exactly this ZPUSH_IPS line
    assert env["ZPUSH_IPS"] == ("02bb00c00001=192.0.2.30,02bb00000002="
                                "192.0.2.242,02bb00000003=192.0.2.243")
    assert env["HDHR_DEVICE_ID"] == "02BB0001" and env["HDHR_TUNERS"] == "4"
    assert env["ZM1"] == "192.0.2.10" and env["HDHR_HLS_TUNER"] == "1"
    assert env["HDHR_BRP_DEVS"] == env["ZDEVS"] == ",".join(h.BRP_DEVS)
    assert "ZEPG_ID_PREFIX" not in env


def test_tuner_map_leases_then_fixed_hls():
    tm = m1config.tuner_map(EXAMPLE["m1s"][0])
    assert [t["kind"] for t in tm] == ["brp-lease"] * 3 + ["m1-hls-fixed"]
    assert tm[1]["push_ip"] == "192.0.2.242" and tm[3]["retunable"] is False


def test_two_m1_conflicts_are_errors():
    bad = {"m1s": [EXAMPLE["m1s"][0], {
        "name": "basement", "m1": "auto:02bb0090001", "device_id": "02BB0001",
        "advertise_ip": "192.0.2.30", "http_port": 80, "ts_port": 5005,
        "brp_devs": [{"dev": "02bb00000003", "push_ip": "192.0.2.243"},
                     {"dev": "02bb00000007", "push_ip": "192.0.2.237"},
                     "02bb00000008", "02bb00012aa2"]}]}
    errs, warns = m1config.validate(bad, client_ips={"192.0.2.237"})
    joined = "\n".join(errs)
    assert "device_id 02BB0001 already used" in joined
    assert "http_port 192.0.2.30:80 already used" in joined
    assert "dev 02bb00000003 already used" in joined
    assert "push_ip 192.0.2.243 shared" in joined
    assert "192.0.2.237 belongs to a real Mini" in joined
    assert any("grants only 3 leases" in w for w in warns)
    assert any("no push_ip" in w for w in warns)


def test_two_m1_valid_config():
    second = {"name": "cabin", "m1": "auto:02bb0090001", "device_id": "02BB0002",
              "advertise_ip": "192.0.2.84", "xmltv_prefix": "cabin",
              "brp_devs": [{"dev": "02bb00000007", "push_ip": "192.0.2.84"},
                           {"dev": "02bb00000008", "push_ip": "192.0.2.244"},
                           {"dev": "02bb00012aa2", "push_ip": "192.0.2.245"}]}
    cfg = {"m1s": [EXAMPLE["m1s"][0], second]}
    errs, warns = m1config.validate(cfg, client_ips={"192.0.2.237"})
    assert errs == [], errs
    assert len(warns) == 1 and ":31061" in warns[0]
    env = m1config.env_for(second)
    assert env["ZEPG_ID_PREFIX"] == "cabin" and env["HDHR_TUNERS"] == "4"
    assert env["ZM1"] == "auto:02bb0090001"


def test_tuner_count_oversell_warns():
    m = dict(EXAMPLE["m1s"][0], tuner_count=5)
    _e, w = m1config.validate({"m1s": [m]})
    assert any("tuner_count 5 > 4" in x for x in w)


# ------------------------------------------------- HTTP (loopback, stubbed) --
def test_http_lineup_and_dup_stream_route_loopback():
    """Json + Stream handlers on 127.0.0.1 ephemeral ports with the lineup and
    tuner stubbed: no BRP, no :80/:5004/:31061."""
    import threading
    import urllib.request
    import urllib.error
    from http.server import ThreadingHTTPServer
    a, b = ch(2, 1, "WBBM-HD", 0x130), ch(2, 1, "WSJV-HD", 0x1a0)
    tuned = []

    class FakeTuner:
        last_fail = {"reason": "busy"}

        def tune(self, cid):
            tuned.append(cid)
            return None
    saved = (h.get_lineup, h.get_brp_tuner)
    h.get_lineup = lambda force=False: [a, b]
    h.get_brp_tuner = lambda: FakeTuner()
    js = ThreadingHTTPServer(("127.0.0.1", 0), h.Json)
    ts = ThreadingHTTPServer(("127.0.0.1", 0), h.Stream)
    for srv in (js, ts):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        jp, tp = js.server_address[1], ts.server_address[1]
        rows = json.loads(urllib.request.urlopen(
            "http://127.0.0.1:%d/lineup.json" % jp, timeout=5).read())
        assert [r["URL"].split("/auto/")[1] for r in rows] == [
            "v2.1", "zb" + b["chan_id"]]
        d = json.loads(urllib.request.urlopen(
            "http://127.0.0.1:%d/discover.json" % jp, timeout=5).read())
        assert d["DeviceID"] == "02BB0001" and d["TunerCount"] == h.TUNER_COUNT
        hl = json.loads(urllib.request.urlopen(
            "http://127.0.0.1:%d/health" % jp, timeout=5).read())
        assert hl["m1"]["ip"] == "192.0.2.10" and hl["m1"]["source"] == "configured"
        for tail, want in (("v2.1", a), ("zb" + b["chan_id"], b)):
            r = urllib.request.urlopen("http://127.0.0.1:%d/auto/%s" % (tp, tail),
                                       timeout=5)
            assert r.status == 200 and r.read() == b""   # soft-fail, 0 bytes
            assert tuned[-1] == want["chan_id"]
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/auto/v9.9" % tp, timeout=5)
            raise AssertionError("expected 404")
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        js.shutdown()
        ts.shutdown()
        h.get_lineup, h.get_brp_tuner = saved


def _entry(**kw):
    m = {"name": "a", "m1": "192.0.2.10", "device_id": "02BB0001",
         "advertise_ip": "192.0.2.30", "http_port": 80, "ts_port": 5004,
         "brp_devs": [{"dev": "02bb00c00001", "push_ip": "192.0.2.30"}]}
    m.update(kw)
    return m


def test_review_default_rejects_real_mini_and_m1_push_ip():
    import copy
    c = {"m1s": [_entry(brp_devs=[{"dev": "02bb00c00001",
                                   "push_ip": "192.0.2.237"}])]}
    e, _ = m1config.validate(c)               # no client_ips passed (CLI path)
    assert any("real Mini" in x for x in e), e
    c = {"m1s": [_entry(brp_devs=[{"dev": "02bb00c00001",
                                   "push_ip": "192.0.2.10"}])]}
    e, _ = m1config.validate(c)
    assert any("M1's address" in x for x in e), e
    # configurable list replaces the default
    c = {"known_real_minis": ["192.0.2.99"], "m1s": [_entry(
        brp_devs=[{"dev": "02bb00c00001", "push_ip": "192.0.2.99"}])]}
    e, _ = m1config.validate(c)
    assert any("real Mini" in x for x in e), e
    # the CLI entry point rejects it too
    import subprocess
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump({"m1s": [_entry(brp_devs=[{"dev": "02bb00c00001",
                   "push_ip": "192.0.2.237"}])]}, fh)
    r = subprocess.run([sys.executable, os.path.join(os.path.dirname(HERE),
                        "m1config.py"), fh.name], capture_output=True, text=True)
    os.unlink(fh.name)
    assert r.returncode == 1 and "ERROR" in r.stdout, r.stdout
    # The example config still validates clean
    e, _ = m1config.validate(EXAMPLE)
    assert not e, e


def test_review_wildcard_port_clash_and_bad_port():
    for first, second in ((_entry(), _entry(name="b", device_id="02BB0069",
                                            m1="192.0.2.251",
                                            advertise_ip=None,
                                            brp_devs=[{"dev": "02bb00000002",
                                                       "push_ip": "192.0.2.242"}])),):
        for a, b in ((first, second), (second, first)):
            e, _ = m1config.validate({"m1s": [a, b]})
            assert any("already used" in x for x in e), e
    # different ports: fine
    e, _ = m1config.validate({"m1s": [_entry(), _entry(
        name="b", device_id="02BB0069", m1="192.0.2.251", advertise_ip=None,
        http_port=8080, ts_port=5005,
        brp_devs=[{"dev": "02bb00000002", "push_ip": "192.0.2.242"}])]})
    assert not [x for x in e if "already used" in x], e
    for bad in ("abc", None, 0, 70000):
        e, _ = m1config.validate({"m1s": [_entry(http_port=bad)]})
        assert any("not a valid port" in x for x in e), (bad, e)


def test_review_duplicate_m1_ip_flagged():
    e, _ = m1config.validate({"m1s": [_entry(), _entry(
        name="b", device_id="02BB0069", advertise_ip="192.0.2.84",
        brp_devs=[{"dev": "02bb00000002", "push_ip": "192.0.2.242"}])]})
    assert any("already used by a" in x and "192.0.2.10" in x for x in e), e


def test_review_brptune_m1_only_via_env_note():
    here = os.path.dirname(HERE)
    src = open(os.path.join(here, "hdhr_emulator.py")).read()
    body = src.split("def _apply_m1", 1)[1].split("\ndef start_m1_discovery", 1)[0]
    assert "brptune/hlstuner read ZM1 from the env" in body
    # no top-level `import brptune` in the facade (lazy import is what makes
    # the env export in _apply_m1 take effect before brptune reads ZM1)
    for line in src.splitlines():
        assert line.strip() != "import brptune" or line.startswith((" ", "\t")), line


if __name__ == "__main__":
    fails = 0
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        try:
            fn()
            print("PASS", name)
        except Exception as e:      # noqa: BLE001
            fails += 1
            print("FAIL", name, repr(e))
    print("%d/%d passed" % (len(tests) - fails, len(tests)))
    raise SystemExit(1 if fails else 0)
