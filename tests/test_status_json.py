"""/status.json + /tuners: array / {numTuners,tuners}, string Resource,
float StreamingRate, TargetIP "" when idle. /tuners.json unchanged."""
import json
import os
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import hdhr_emulator as h

CID = "0x1130000000002001"
CH = [{"chan_id": CID, "chan_major": 2, "chan_minor": 1,
       "call_letters": "CBS2-HD", "v_res": 1080}]
N = h.TUNER_COUNT


def _rows(info, sts, now=100.0):
    return h.status_rows(CH, sts, info, now=now)


def _check_types(row):
    assert isinstance(row["Resource"], str) and row["Resource"].startswith("tuner")
    for k in ("VctNumber", "VctName", "TargetIP"):
        assert isinstance(row[k], str)
    for k in ("Frequency", "SignalStrengthPercent", "SignalQualityPercent",
              "SymbolQualityPercent"):
        assert type(row[k]) is int
    assert type(row["StreamingRate"]) is float


def test_idle_exact_shape():
    rows = _rows([None] * N, [])
    assert isinstance(rows, list) and len(rows) == N
    for i, r in enumerate(rows):
        _check_types(r)
        assert r == {"Resource": "tuner%d" % i, "VctNumber": "", "VctName": "",
                     "Frequency": 0, "SignalStrengthPercent": 0,
                     "SignalQualityPercent": 0, "SymbolQualityPercent": 0,
                     "StreamingRate": 0.0, "TargetIP": ""}


def test_live_slot():
    h._rate_prev.clear()
    info = [None] * N
    info[1] = {"chan_id": CID, "nbytes": 0, "client_ip": "192.0.2.50", "t0": 90.0}
    sts = [None, {"frequency": 575000000, "str": -500, "snr": 200}]
    rows = _rows(info, sts, now=100.0)
    r = rows[1]
    _check_types(r)
    assert r["Resource"] == "tuner1" and r["VctNumber"] == "2.1"
    assert r["VctName"] == "CBS2-HD" and r["Frequency"] == 575000000
    assert r["SignalStrengthPercent"] == 50 and r["SignalQualityPercent"] == 50
    assert r["TargetIP"] == "192.0.2.50"
    assert rows[0]["TargetIP"] == "" and rows[2]["TargetIP"] == ""
    info[1]["nbytes"] = 125000
    assert _rows(info, sts, now=101.0)[1]["StreamingRate"] == 1000000.0


def test_live_without_api19_or_client():
    h._rate_prev.clear()
    info = [None] * N
    info[0] = {"chan_id": CID, "nbytes": 0, "client_ip": None, "t0": None}
    r = _rows(info, [None])[0]
    _check_types(r)
    assert r["SignalStrengthPercent"] == 100 and r["TargetIP"] not in ("", "none")


def test_http_routes_and_tuners_json_unchanged():
    # Handler wiring with BRP stubbed: no network to the M1.
    h.get_lineup = lambda force=False: CH
    h.get_brp_tuner = lambda: (_ for _ in ()).throw(RuntimeError("no brp"))
    h._hls_live[0] = 0
    srv = ThreadingHTTPServer(("127.0.0.1", 0), h.Json)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    try:
        g = lambda p: json.load(urllib.request.urlopen(base + p, timeout=5))
        s = g("/status.json")
        assert isinstance(s, list) and len(s) == N
        t = g("/tuners")
        assert t["numTuners"] == N and t["tuners"] == s
        for r in s:
            _check_types(r)
            assert r["TargetIP"] == ""
        old = g("/tuners.json")           # legacy shape: int Resource, unchanged
        assert all(type(r["Resource"]) is int for r in old)
    finally:
        srv.shutdown()


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v(); print("ok", k)
