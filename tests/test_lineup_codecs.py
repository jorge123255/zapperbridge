"""/lineup.json VideoCodec/AudioCodec: only measured channels carry them."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import hdhr_emulator as h
import lineup as lu

HOST = "192.0.2.10"
FIX = os.path.join(HERE, "fixtures")


def _chans():
    with open(os.path.join(FIX, "api8-market-a-86.json")) as fh:
        return json.load(fh)["channels"] + [h.HLS_CHANNEL]


def _by_gn():
    return {r["GuideNumber"]: r for r in h.lineup_rows(_chans(), HOST)}


VIDEO = {"MPEG2", "H264", "HEVC"}
AUDIO = {"AC3", "EAC3", "AAC", "MPEG", "AC4"}


def test_codecs_json_is_well_formed():
    assert h.MEASURED_CODECS, "codecs.json empty/unreadable"
    for gn, e in h.MEASURED_CODECS.items():
        assert e.get("VideoCodec", "MPEG2") in VIDEO, gn
        assert e.get("AudioCodec", "AC3") in AUDIO, gn
        assert "VideoCodec" in e or "AudioCodec" in e, gn


def test_rows_carry_exactly_the_measured_codecs():
    rows = _by_gn()
    for gn, r in rows.items():
        e = h.MEASURED_CODECS.get(gn)
        if e and e["GuideName"] == r["GuideName"]:
            for k in ("VideoCodec", "AudioCodec"):
                if k in e:
                    assert r[k] == e[k] and isinstance(r[k], str), (gn, k)
                else:
                    assert k not in r, (gn, k)
        else:
            assert "VideoCodec" not in r and "AudioCodec" not in r, gn
    assert "VideoCodec" not in rows["999.1"]


def test_not_all_channels_share_one_pair():
    pairs = {(r.get("VideoCodec"), r.get("AudioCodec")) for r in _by_gn().values()}
    assert ("MPEG2", "AC3") in pairs and ("H264", "AC3") in pairs


def test_guidename_mismatch_not_labelled():
    c = {"chan_id": "0x1130000000002001", "chan_major": 2, "chan_minor": 1,
         "call_letters": "OTHERMARKET", "protocol": "ATSC-1", "v_res": 1080}
    assert "VideoCodec" not in h.lineup_rows([c], HOST)[0]


def test_unknown_or_bad_file_omits(tmp_path=None):
    import tempfile
    d = tempfile.mkdtemp()
    p = os.path.join(d, "x.json")
    assert h.load_measured_codecs(p) == {}
    open(p, "w").write('{"2.1": {"GuideName": "A", "VideoCodec": 5}, "3.1": "x"}')
    assert h.load_measured_codecs(p) == {}


def test_atsc3_never_labelled():
    c = {"chan_id": "0x1130000000002001", "chan_major": 2, "chan_minor": 1,
         "call_letters": "CBS2-HD", "protocol": "ATSC-3", "v_res": 1080}
    assert "VideoCodec" not in h.lineup_rows([c], HOST)[0]


def test_duplicate_gn_second_row_unlabelled():
    a = {"chan_id": "0x1130000000002001", "chan_major": 2, "chan_minor": 1,
         "call_letters": "CBS2-HD", "protocol": "ATSC-1", "v_res": 1080}
    b = dict(a, chan_id="0x11a0000000002001", call_letters="B")
    rows = h.lineup_rows([a, b], HOST)
    assert "VideoCodec" in rows[0] and "VideoCodec" not in rows[1]


def test_other_fields_order_and_version_unchanged():
    rows = h.lineup_rows(_chans(), HOST)
    base = ["GuideNumber", "GuideName", "Tags", "URL", "HD", "Modulation", "DRM"]
    for r in rows:
        assert list(r)[:7] == base
    assert h.lineup_version(_chans()) == 2840204313
    assert h.lineup_hash(_chans())[:16] == "a94a1419aa4a5533"


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v()
    print("ok")
