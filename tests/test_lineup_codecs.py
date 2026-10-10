"""/lineup.json VideoCodec/AudioCodec: only channels learned from a tuned
stream's PMT carry them (no shipped market data, no guessing)."""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import codeclearn as cl
import hdhr_emulator as h

HOST = "192.0.2.10"
FIX = os.path.join(HERE, "fixtures")


def _chans():
    with open(os.path.join(FIX, "api8-market-a-86.json")) as fh:
        return json.load(fh)["channels"] + [h.HLS_CHANNEL]


def _by_gn():
    return {r["GuideNumber"]: r for r in h.lineup_rows(_chans(), HOST)}


def _fresh(monkeypatch=None):
    tmp = tempfile.mkdtemp()
    h.CODECS = cl.CodecCache(os.path.join(tmp, "c.json"))
    return h.CODECS


def test_no_codecs_shipped_and_nothing_learned_means_no_fields():
    assert not os.path.exists(os.path.join(os.path.dirname(HERE), "codecs.json"))
    _fresh()
    for gn, r in _by_gn().items():
        assert "VideoCodec" not in r and "AudioCodec" not in r, gn


def test_learned_channel_carries_exactly_learned_fields():
    c = _fresh()
    rows = h.lu.rows(_chans(), h.XMLTV_PREFIX)
    r = next(x for x in rows if x.key == "v" + x.gn and h.proto_num(x.chan) == "1")
    c.record(h.BRP_DEV, r.gn, r.name, "MPEG2", None)
    row = _by_gn()[r.gn]
    assert row["VideoCodec"] == "MPEG2" and "AudioCodec" not in row
    c.record(h.BRP_DEV, r.gn, r.name, "MPEG2", "AC3")
    row = _by_gn()[r.gn]
    assert (row["VideoCodec"], row["AudioCodec"]) == ("MPEG2", "AC3")
    others = [v for k, v in _by_gn().items() if k != r.gn]
    assert all("VideoCodec" not in v for v in others)


def test_guidename_or_m1_mismatch_not_labelled():
    c = _fresh()
    c.record(h.BRP_DEV, "2.1", "OTHERMARKET", "MPEG2", "AC3")
    c.record("some-other-m1", "2.1", "CBS", "H264", "AAC")
    ch = {"chan_id": "0x1130000000002001", "chan_major": 2, "chan_minor": 1,
          "call_letters": "CBS", "protocol": "ATSC-1", "v_res": 1080}
    assert "VideoCodec" not in h.lineup_rows([ch], HOST)[0]
    c.record(h.BRP_DEV, "2.1", "CBS", "H264", "AAC")
    assert h.lineup_rows([ch], HOST)[0]["VideoCodec"] == "H264"


def test_hls_and_atsc3_rows_never_labelled():
    c = _fresh()
    c.record(h.BRP_DEV, "999.1", h.HLS_CHANNEL["call_letters"], "MPEG2", "AC3")
    assert "VideoCodec" not in _by_gn()["999.1"]
    ch = {"chan_id": "0x1130000000003001", "chan_major": 3, "chan_minor": 1,
          "call_letters": "NG", "protocol": "ATSC-3", "v_res": 2160}
    c.record(h.BRP_DEV, "3.1", "NG", "HEVC", "AC4")
    assert "VideoCodec" not in h.lineup_rows([ch], HOST)[0]


def test_codec_token_changes_so_etag_refetches():
    c = _fresh()
    t0 = c.token()
    c.record(h.BRP_DEV, "2.1", "CBS", "MPEG2", "AC3")
    assert c.token() != t0


def test_learner_for_picks_the_primary_row_and_skips_others():
    _fresh()
    chans = _chans()
    prim = next(x for x in h.lu.rows(chans, h.XMLTV_PREFIX)
                if h.proto_num(x.chan) == "1" and x.key == "v" + x.gn)
    lr = h.codec_learner_for(chans, prim.chan)
    assert lr and (lr.gn, lr.name, lr.m1) == (prim.gn, prim.name, h.BRP_DEV)
    assert h.codec_learner_for(chans, h.HLS_CHANNEL) is None


# unittest discovery (CI runs `python -m unittest discover`): expose the
# module-level test functions as TestCase methods; pytest also still works.
import unittest  # noqa: E402


class Tests(unittest.TestCase):
    pass


for _n, _f in list(globals().items()):
    if _n.startswith("test_") and callable(_f):
        setattr(Tests, _n, (lambda fn: lambda self: fn())(_f))
