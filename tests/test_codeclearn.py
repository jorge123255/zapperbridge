"""codeclearn: PMT parsing, persistence, corruption, merged multi-M1, passivity."""
import json
import os
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import codeclearn as cl
import lineupmerge as lm


def _pkt(pid, payload, cc=0):
    p = bytearray(b"\xff" * 188)
    p[0] = 0x47
    p[1] = 0x40 | (pid >> 8)
    p[2] = pid & 0xFF
    p[3] = 0x10 | cc
    body = b"\x00" + payload
    p[4:4 + len(body)] = body
    return bytes(p)


def _sec(tid, body):
    n = len(body) + 4
    return bytes([tid, 0xB0 | (n >> 8), n & 0xFF]) + body + b"\0\0\0\0"


def pat(pmt_pid=0x100, prog=1):
    return _pkt(0, _sec(0, bytes([0, 1, 0xC1, 0, 0, prog >> 8, prog & 0xFF,
                                  0xE0 | (pmt_pid >> 8), pmt_pid & 0xFF])))


def pmt(es, pmt_pid=0x100, prog=1):
    """es = [(stream_type, pid, descriptor_bytes)]"""
    b = bytearray([prog >> 8, prog & 0xFF, 0xC1, 0, 0, 0xE1, 0x00, 0xF0, 0x00])
    for st, pid, d in es:
        b += bytes([st, 0xE0 | (pid >> 8), pid & 0xFF,
                    0xF0 | (len(d) >> 8), len(d) & 0xFF]) + d
    return _pkt(pmt_pid, _sec(2, bytes(b)))


def media(pid=0x101):
    p = bytearray(b"\xaa" * 188)
    p[0], p[1], p[2], p[3] = 0x47, 0x40 | (pid >> 8), pid & 0xFF, 0x10
    return bytes(p)


REG = lambda t: bytes([0x05, 4]) + t  # noqa: E731


def test_video_stream_types():
    for st, want in ((0x02, "MPEG2"), (0x01, "MPEG2"), (0x1B, "H264"), (0x24, "HEVC")):
        assert cl.pmt_codecs(pmt([(st, 0x101, b"")])) == (want, None), hex(st)


def test_audio_stream_types():
    for st, want in ((0x81, "AC3"), (0x87, "EAC3"), (0x0F, "AAC"), (0x11, "AAC"),
                     (0x03, "MPEG"), (0x04, "MPEG")):
        assert cl.pmt_codecs(pmt([(0x02, 0x101, b""), (st, 0x102, b"")])) == (
            "MPEG2", want), hex(st)


def test_private_stream_descriptors():
    cases = [(bytes([0x6A, 1, 0]), "AC3"), (bytes([0x7A, 1, 0]), "EAC3"),
             (REG(b"AC-3"), "AC3"), (REG(b"EAC3"), "EAC3"), (REG(b"AC-4"), "AC4"),
             (bytes([0x7F, 2, 0x15, 0]), "AC4")]
    for d, want in cases:
        assert cl.pmt_codecs(pmt([(0x1B, 0x101, b""), (0x06, 0x102, d)])) == ("H264", want)
    # unrecognised private data: not guessed
    assert cl.pmt_codecs(pmt([(0x1B, 0x101, b""), (0x06, 0x102, bytes([0x45, 1, 0]))])) == (
        "H264", None)


def test_audio_only_unknown_type_and_garbage():
    assert cl.pmt_codecs(pmt([(0x81, 0x102, b"")])) == (None, "AC3")
    assert cl.pmt_codecs(pmt([(0x42, 0x101, b"")])) == (None, None)
    assert cl.pmt_codecs(b"") == (None, None)
    assert cl.pmt_codecs(b"\x00" * 400) == (None, None)
    assert cl.pmt_codecs(pat()) == (None, None)


def test_first_audio_wins():
    assert cl.pmt_codecs(pmt([(0x02, 0x101, b""), (0x81, 0x102, b""),
                              (0x0F, 0x103, b"")])) == ("MPEG2", "AC3")


def test_cache_roundtrip_and_atomic_file():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "sub", "c.json")
    c = cl.CodecCache(p)
    assert c.record("m1a", "2.1", "CBS", "MPEG2", "AC3") is True
    assert c.record("m1a", "2.1", "CBS", "MPEG2", "AC3") is False   # no change
    assert c.save()
    assert os.listdir(os.path.dirname(p)) == ["c.json"]             # no tmp left
    c2 = cl.CodecCache(p)
    assert c2.lookup("m1a", "2.1", "CBS") == {"VideoCodec": "MPEG2", "AudioCodec": "AC3"}
    assert c2.lookup("m1b", "2.1", "CBS") == {}
    assert c2.lookup("m1a", "2.1", "NBC") == {}
    assert c2.token() == c.token()


def test_partial_update_keeps_other_field():
    c = cl.CodecCache(None)
    c.record("m", "5.1", "X", "H264", None)
    c.record("m", "5.1", "X", None, "AAC")
    assert c.lookup("m", "5.1", "X") == {"VideoCodec": "H264", "AudioCodec": "AAC"}
    assert c.record("m", "5.1", "X", "bogus", "bogus") is False


def test_corruption_tolerated():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "c.json")
    for junk in ("", "{not json", "[]", '{"version":2,"entries":{}}',
                 '{"version":1,"entries":[1]}', '{"version":1,"entries":{"bad":{}}}',
                 '{"version":1,"entries":{"a|b|c":{"VideoCodec":"EVIL"}}}',
                 "\x00\xff" * 50):
        open(p, "w").write(junk)
        c = cl.CodecCache(p)
        assert len(c) == 0, junk
        assert c.record("m", "1.1", "N", "MPEG2", None)
        assert c.save()
        assert cl.CodecCache(p).lookup("m", "1.1", "N") == {"VideoCodec": "MPEG2"}


def test_oversize_file_ignored_and_entries_bounded():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "c.json")
    with open(p, "w") as fh:
        fh.write(" " * (cl.MAX_FILE_BYTES + 1))
    assert len(cl.CodecCache(p)) == 0
    t = [0.0]
    c = cl.CodecCache(None, clock=lambda: t.__setitem__(0, t[0] + 1) or t[0], max_entries=5)
    for i in range(20):
        c.record("m", "%d.1" % i, "N%d" % i, "MPEG2", "AC3")
    assert len(c) == 5
    assert c.lookup("m", "19.1", "N19") and not c.lookup("m", "0.1", "N0")


def test_save_merges_other_process_entries():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "c.json")
    a, b = cl.CodecCache(p), cl.CodecCache(p)
    a.record("m1a", "2.1", "A", "MPEG2", "AC3")
    b.record("m1b", "2.1", "B", "H264", "AAC")
    assert a.save() and b.save()
    got = cl.CodecCache(p)
    assert got.lookup("m1a", "2.1", "A") and got.lookup("m1b", "2.1", "B")


def test_unwritable_path_never_raises():
    c = cl.CodecCache("/proc/definitely/not/writable/c.json")
    c.record("m", "1.1", "N", "MPEG2", "AC3")
    assert c.save() is False
    c.save_async()
    time.sleep(0.1)
    assert c.lookup("m", "1.1", "N")


def test_thread_safety():
    c = cl.CodecCache(os.path.join(tempfile.mkdtemp(), "c.json"))

    def w(n):
        for i in range(200):
            c.record("m", "%d.%d" % (n, i % 7), "N", "MPEG2", "AC3")
            c.lookup("m", "1.1", "N")
            c.token()
    ts = [threading.Thread(target=w, args=(n,)) for n in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert c.save() and len(c) == 8 * 7


def test_stream_learner_learns_from_pat_pmt_then_stops():
    c = cl.CodecCache(None)
    lr = cl.StreamLearner(c, "m1", "2.1", "CBS")
    lr.feed(media())
    assert not lr.done
    lr.feed(pmt([(0x02, 0x101, b"")]))        # PMT before PAT: ignored
    assert not lr.done
    lr.feed(pat() + media() + pmt([(0x02, 0x101, b""), (0x81, 0x102, b"")]))
    assert lr.done
    assert c.lookup("m1", "2.1", "CBS") == {"VideoCodec": "MPEG2", "AudioCodec": "AC3"}
    lr.feed(pmt([(0x1B, 0x101, b"")]))        # done: ignored, cheap
    assert c.lookup("m1", "2.1", "CBS")["VideoCodec"] == "MPEG2"


def test_stream_learner_other_pmt_pid_and_budget():
    c = cl.CodecCache(None)
    lr = cl.StreamLearner(c, "m", "9.1", "N")
    lr.feed(pat(pmt_pid=0x1000))
    lr.feed(pmt([(0x1B, 0x101, b"")], pmt_pid=0x1000))
    assert c.lookup("m", "9.1", "N") == {"VideoCodec": "H264"}
    lr2 = cl.StreamLearner(c, "m", "8.1", "N", budget=5)
    for _ in range(10):
        lr2.feed(media())
    assert lr2.done
    lr2.feed(pat() + pmt([(0x02, 0x101, b"")]))
    assert c.lookup("m", "8.1", "N") == {}    # gave up: nothing guessed


def test_stream_learner_never_raises_on_garbage():
    lr = cl.StreamLearner(cl.CodecCache(None), "m", "1.1", "N")
    for junk in (b"", b"\x47", os.urandom(188 * 3), b"\x47\x40\x00" + b"\xff" * 185):
        lr.feed(junk)


def test_learner_cost_is_small():
    lr = cl.StreamLearner(cl.CodecCache(None), "m", "1.1", "N", budget=10 ** 9)
    blob = media() * 7
    t = time.perf_counter()
    for _ in range(2000):
        lr.feed(blob)
    assert time.perf_counter() - t < 2.0   # 14k packets, generous bound


def _ch(major, minor, call, cid):
    return {"chan_id": cid, "chan_major": major, "chan_minor": minor,
            "call_letters": call, "hidden": 0}


def test_merged_lineup_multi_m1():
    A = [_ch(5, 1, "WMAQ", "0xa5"), _ch(7, 1, "WLS", "0xa7"), _ch(2, 1, "WBBM", "0xa2")]
    B = [_ch(5, 1, "WMAQ", "0xb5"), _ch(7, 1, "WLS", "0xb7")]
    rows, _a, _n = lm.merge([{"name": "A", "chans": A, "announced": 4, "leases": None},
                             {"name": "B", "chans": B, "announced": 4, "leases": None}])
    c = cl.CodecCache(None)
    c.record("A", "5.1", "WMAQ", "MPEG2", "AC3")
    c.record("B", "5.1", "WMAQ", "MPEG2", "AAC")     # audio disagrees
    c.record("A", "7.1", "WLS", "H264", "AC3")       # only A learned it
    c.record("A", "2.1", "WBBM", "MPEG2", "AC3")
    out = {r["GuideNumber"]: r for r in lm.lineup_rows(rows, "192.0.2.10", codecs=c)}
    assert out["5.1"]["VideoCodec"] == "MPEG2" and "AudioCodec" not in out["5.1"]
    assert out["7.1"]["VideoCodec"] == "H264" and out["7.1"]["AudioCodec"] == "AC3"
    assert out["2.1"]["VideoCodec"] == "MPEG2"
    plain = lm.lineup_rows(rows, "192.0.2.10")
    assert all("VideoCodec" not in r for r in plain)


# unittest discovery (CI runs `python -m unittest discover`): expose the
# module-level test functions as TestCase methods; pytest also still works.
import unittest  # noqa: E402


class Tests(unittest.TestCase):
    pass


for _n, _f in list(globals().items()):
    if _n.startswith("test_") and callable(_f):
        setattr(Tests, _n, (lambda fn: lambda self: fn())(_f))


class WebUiSnap(unittest.TestCase):
    def test_snap_lineup_counts_learned_and_unknown(self):
        import hdhr_emulator as h
        import webui
        h.CODECS = cl.CodecCache(None)
        with open(os.path.join(HERE, "fixtures", "api8-market-a-86.json")) as fh:
            chans = json.load(fh)["channels"]
        h._cache["channels"], h._cache["at"] = chans, time.time()
        s0 = webui.snap_lineup(h)
        self.assertEqual(s0["codecs_learned"], 0)
        self.assertEqual(s0["codecs_unknown"], s0["count"])
        r = next(x for x in h.lu.rows(chans, h.XMLTV_PREFIX)
                 if x.key == "v" + x.gn and h.proto_num(x.chan) == "1")
        h.CODECS.record(h.BRP_DEV, r.gn, r.name, "MPEG2", "AC3")
        s1 = webui.snap_lineup(h)
        self.assertEqual(s1["codecs_learned"], 1)
        row = next(x for x in s1["channels"] if x["number"] == r.gn and x["name"] == r.name)
        self.assertEqual((row["video"], row["audio"]), ("MPEG2", "AC3"))
