#!/usr/bin/env python3
"""ZapperBox M1 -> HDHomeRun emulator.

Serves the HDHR JSON surface on :80 and the /auto/vN stream endpoint on :5004,
backed by the M1's BRP API-8 lineup and /atsc{N}/{major}/master.m3u8 HLS.
"""
import json, re, sys, time, threading, hashlib, collections, signal
import os
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from brp_probe import brp_tcp
import hlstuner
import epg as epgmod
import ssdp as ssdpmod
import lineup as lu
import codeclearn
import tspacer
import zdiag
import zstream
import webui
import reqlog

#: The M1 to serve. A literal IP, or "auto" (the default) or
#: "auto:<m1 dev id|ip>" resolves it at startup from the M1's own BRzb
#: announce on UDP 31064 (passive listen, see m1disco.py).
M1_SPEC = os.environ.get("ZM1", "auto")
M1 = M1_SPEC
_m1_info = {"spec": M1_SPEC, "dev": None,
            "source": "auto-pending" if M1_SPEC.lower().startswith("auto")
            else "configured"}
#: Set once the M1 address is known. With ZM1=auto the HTTP servers start
#: first and a background loop (start_m1_discovery) sets this when an announce
#: is heard; until then every M1-touching path answers fast without any I/O.
_m1_ready = threading.Event()
if not M1_SPEC.lower().startswith("auto"):
    _m1_ready.set()
EMU_ID = "e2c799f80007"
#: 8-hex HDHR-style id. One per virtual HDHR (i.e. per M1) when several M1s
#: are served; the default is a placeholder id.
DEVICE_ID = os.environ.get("HDHR_DEVICE_ID", "02BB0001")
#: Optional XMLTV id namespace so two M1s' guides can be merged by a client
#: without "zb-2.1" colliding. Empty (default) keeps the historical ids.
XMLTV_PREFIX = os.environ.get("ZEPG_ID_PREFIX", "")
#: Registered BRP device ids; the M1 leases one network tuner per device id.
#: The BRP pool is three leases (instances 3, 4, 5) handed out globally and
#: device-agnostic; instances 1 and 2 are never grantable, so three ids is the
#: whole BRP pool. These are the ids proven to push video.
BRP_DEVS = [d.strip() for d in os.environ.get(
    "HDHR_BRP_DEVS",
    "02bb00000001,02bb00000002,02bb00000003").split(",")
    if d.strip()]
#: The M1's own tuner is the fourth: it is published as a fixed-channel HLS
#: window on the M1's HTTP port and cannot be retuned over BRP, so we remux and
#: pass it through. HDHR_HLS_TUNER=0 drops back to the three BRP leases.
HLS_TUNER = os.environ.get("HDHR_HLS_TUNER", "1") == "1"
HLS_PROTO = os.environ.get("HDHR_HLS_PROTO", "1")
HLS_SLOT = os.environ.get("HDHR_HLS_SLOT", "1")
HLS_GUIDE = os.environ.get("HDHR_HLS_GUIDE", "999.1")
HLS_NAME = os.environ.get("HDHR_HLS_NAME", "M1-TUNER4")
TUNER_COUNT = int(os.environ.get(
    "HDHR_TUNERS", len(BRP_DEVS) + (1 if HLS_TUNER else 0)))
TUNE_SEQ = 0x0100
HTTP_PORT = int(os.environ.get("HDHR_JSON_PORT", 80))
TS_PORT = int(os.environ.get("HDHR_TS_PORT", 5004))
SSDP_ON = os.environ.get("HDHR_SSDP", "1") == "1"
SSDP_PORT = int(os.environ.get("HDHR_SSDP_PORT", 1900))
#: LOCATION / URLBase IP: the route-selected primary LAN address (never the
#: en0 aliases); override with HDHR_ADVERTISE_IP.
ADVERTISE_IP = os.environ.get("HDHR_ADVERTISE_IP") or ""
FRIENDLY = os.environ.get("HDHR_FRIENDLY", "ZapperBox M1 (emulated)")
MODEL = "ZBM1-gt698"
_ssdp = [None]

_cache = {"at": 0, "channels": []}
_api8 = {"ok_at": 0.0, "fail_at": 0.0, "err": None, "source": None,
         "err_name": None, "handshake": None, "hint": None, "_logged": None}
LINEUP_CACHE_FILES = [os.path.join(HERE, n) for n in
                      ("lineup-cache.json", "lineup-now.json", "lineup.json")]

BRP_DEV = os.environ.get("HDHR_BRP_DEV", BRP_DEVS[0])
_tuned = {"chan_id": None}
_hb = {"thread": None, "stop": False}
_seq = [0x000D]
_brp_lock = threading.RLock()
BRP_GAP = float(os.environ.get("ZGAP", "1.0"))
HB_ON = os.environ.get("HDHR_HB", "1") == "1"
HB_SEC = float(os.environ.get("HDHR_HB_SEC", "2"))


def brp_call(api, a0=3, a1=0, a3=0, payload=b"", len2=None, timeout=8.0):
    if not _m1_ready.is_set():
        raise RuntimeError("M1 not discovered yet")
    _seq[0] = (_seq[0] + 1) & 0xFFFF
    if len2 is None:
        len2 = len(payload) + 1 if payload else 0
        if payload:
            payload = payload + bytes(1)
    with _brp_lock:
        return brp_tcp(M1, api, BRP_DEV, a0=a0, a1=a1, a3=a3, payload=payload,
                       len1=0, len2=len2, flags=_seq[0], timeout=timeout)


def brp_handshake():
    return brp_call(6, a0=0, len2=0)


def _heartbeat_loop():
    while not _hb["stop"]:
        try:
            brp_call(11, a0=3, a1=int(time.time()), len2=0, timeout=2.0)
        except Exception as e:
            print("hb err: %r" % (e,), flush=True)
        time.sleep(HB_SEC)


def start_heartbeat():
    if not HB_ON:
        return
    if _hb["thread"] is not None and _hb["thread"].is_alive():
        return
    _hb["stop"] = False
    th = threading.Thread(target=_heartbeat_loop, daemon=True)
    _hb["thread"] = th
    th.start()


#: What a refusal from the M1 usually means (BRP err_name -> what to do).
API8_HINTS = {
    "NOT_SHARED": "sharing is off on this M1: turn on sharing for it in the "
                  "Zapper app",
    "NOT_FOUND": "the M1 does not know device id %(dev)s yet: register the "
                 "bridge's ids with `discover --apply` (docs/DOCKER.md)",
    "DEVICE_ID_NOT_ALLOWED": "the M1 does not allow device id %(dev)s: "
                             "register the bridge's ids with `discover "
                             "--apply` (docs/DOCKER.md)",
}


def _api8_hint(err_name):
    h = API8_HINTS.get(err_name or "")
    return h % {"dev": BRP_DEV} if h else None


def _api8_failed(handshake, tries):
    """Record why api8 failed in _api8 (shown on /health) and log it once
    per distinct reason. ``tries`` is [(a0, err_name or None, detail)]."""
    names = [n for _a, n, _d in tries if n and n != "SUCCESS"]
    hs_name = handshake.get("err_name")
    # the M1's own answer beats a local exception; api8's beats the handshake
    err_name = (names[0] if names else
                hs_name if hs_name and hs_name != "SUCCESS" else None)
    parts = ["a0=%d %s" % (a, d) for a, _n, d in tries]
    err = "api8 FAIL on a0 2/3/4/5: %s; api6 handshake: %s" % (
        ", ".join(parts) or "no attempt", handshake.get("detail"))
    hint = _api8_hint(err_name) or _api8_hint(hs_name)
    _api8.update(fail_at=time.time(), err=err, err_name=err_name,
                 handshake=handshake.get("detail"), hint=hint)
    sig = (err_name, hs_name, tuple(parts))
    if sig != _api8.get("_logged"):
        _api8["_logged"] = sig
        print("lineup: M1 %s dev %s refused or failed: %s%s" % (
            M1, BRP_DEV, err, (" -> " + hint) if hint else ""), flush=True)


def _lineup_raw(force=False):
    if not _m1_ready.is_set():
        raise RuntimeError("M1 not discovered yet")
    if not force and time.time() - _cache["at"] < 300 and _cache["channels"]:
        return _cache["channels"]
    try:
        r6 = brp_handshake()
        n6 = r6.get("err_name") if r6 else None
        handshake = {"err_name": n6, "detail": n6 or "no reply"}
    except Exception as e:
        handshake = {"err_name": None, "detail": "error %r" % (e,)}
    tries = []
    for a0 in (2, 3, 4, 5):
        try:
            r = brp_call(8, a0=a0, len2=0)
        except Exception as e:
            tries.append((a0, None, "error %r" % (e,)))
            continue
        body = r.get("body") or b""
        name = r.get("err_name")
        if name != "SUCCESS":
            tries.append((a0, name, str(name)))
            continue
        if not body:
            tries.append((a0, name, "SUCCESS but empty body"))
            continue
        try:
            chans = json.loads(body)["channels"]
        except Exception as e:
            tries.append((a0, name, "SUCCESS but bad JSON (%s)" % (
                type(e).__name__,)))
            continue
        if chans:
            _cache.update(at=time.time(), channels=chans)
            _api8.update(ok_at=time.time(), err=None, err_name=None,
                         handshake=handshake["detail"], hint=None,
                         source="api8")
            if _api8.get("_logged"):
                _api8["_logged"] = None
                print("lineup: api8 OK again (%d channels)" % len(chans),
                      flush=True)
            try:
                with open(LINEUP_CACHE_FILES[0], "w") as fh:
                    json.dump({"m1": M1, "channels": chans}, fh)
            except Exception:
                pass
            return chans
        tries.append((a0, name, "SUCCESS but 0 channels"))
    _api8_failed(handshake, tries)
    for path in LINEUP_CACHE_FILES:
        chans = load_lineup_cache(path, M1)
        if chans:
            _cache.update(at=time.time() - 240, channels=chans)
            _api8["source"] = "cache:%s" % os.path.basename(path)
            return chans
    raise RuntimeError("lineup unavailable: api8 failed and no cache")

def load_lineup_cache(path, m1):
    """Channels from a lineup cache file, only if it belongs to this M1.

    New caches are tagged {"m1": ip}; a cache written for another M1 (another
    user's box, or a box in another market) is never served. Untagged legacy
    caches are still accepted so an upgrade keeps the existing fallback."""
    try:
        with open(path) as fh:
            d = json.load(fh)
        chans = d["channels"]
    except Exception:
        return None
    owner = d.get("m1") if isinstance(d, dict) else None
    if owner is not None and owner != m1:
        print("lineup cache %s belongs to M1 %s, not %s: ignored" % (
            os.path.basename(path), owner, m1), flush=True)
        return None
    return chans or None


#: The M1's own tuner, synthesised into the lineup so clients can reach it.
HLS_CHANNEL = {"chan_id": "hls:%s" % HLS_SLOT, "chan_major": 999, "chan_minor": 1,
               "call_letters": HLS_NAME, "hidden": 0, "encrypted": 0,
               "protocol": "ATSC%s" % HLS_PROTO, "h_res": 1920, "v_res": 1080}


def get_lineup(force=False):
    """BRP lineup plus the M1's own tuner as a fourth row."""
    chans = _lineup_raw(force)
    if not HLS_TUNER:
        return chans
    return chans + [HLS_CHANNEL]


#: Seconds the lineup-dependent JSON endpoints wait for the M1 before they
#: answer from the cached lineup (or []). /discover.json never waits.
LINEUP_TIMEOUT = float(os.environ.get("HDHR_LINEUP_TIMEOUT", "3.0"))
_lu_flight = {"lock": threading.Lock(), "ev": None, "box": None}
_lu_note = {"at": 0.0}


def cached_lineup():
    """Last known lineup (memory, else a cache file for THIS M1) plus the HLS
    row, or None. Never touches the network or _brp_lock."""
    if not _m1_ready.is_set():
        return None
    chans = _cache["channels"]
    if not chans:
        for path in LINEUP_CACHE_FILES:
            chans = load_lineup_cache(path, M1)
            if chans:
                break
    if not chans:
        return None
    return chans + [HLS_CHANNEL] if HLS_TUNER else chans


def _lineup_flight(force):
    """One background fetch at a time (get_lineup can block for tens of
    seconds on a dead M1 and holds _brp_lock); callers wait on it with a
    timeout instead of holding the lock themselves."""
    with _lu_flight["lock"]:
        ev = _lu_flight["ev"]
        if ev is None or ev.is_set():
            ev, box = threading.Event(), {}

            def work():
                try:
                    box["chans"] = get_lineup(force)
                except Exception as e:
                    box["err"] = e
                finally:
                    ev.set()
            _lu_flight.update(ev=ev, box=box)
            threading.Thread(target=work, daemon=True,
                             name="lineup-fetch").start()
        return _lu_flight["ev"], _lu_flight["box"]


def get_lineup_bounded(force=False, timeout=None):
    """(channels or None, reason or None). Healthy M1 / fresh cache: exactly
    get_lineup(). Otherwise waits at most `timeout` s, then returns the
    cached lineup, or (None, why) if there is none. Never raises, never
    blocks past the timeout."""
    timeout = LINEUP_TIMEOUT if timeout is None else timeout
    if not _m1_ready.is_set():
        return None, "M1 not discovered yet"
    if not force and time.time() - _cache["at"] < 300 and _cache["channels"]:
        return get_lineup(), None  # in-memory, no I/O
    ev, box = _lineup_flight(force)
    ev.wait(timeout)
    if ev.is_set() and "chans" in box:
        return box["chans"], None
    why = ("M1 unreachable or lineup fetch failed" if ev.is_set()
           else "M1 slow (>%.1fs)" % timeout)
    stale = cached_lineup()
    if stale:
        why += "; serving cached lineup"
    if time.time() - _lu_note["at"] > 30:
        _lu_note["at"] = time.time()
        print("lineup: %s%s" % (why, "" if stale else "; no cache, serving []"),
              flush=True)
    return stale, why


def abs_uri(host, base_path, uri):
    name = uri.split("?", 1)[0].rstrip("/").split("/")[-1]
    if name.endswith(".m3u8"):
        return "http://%s:%d%s%s" % (host, TS_PORT, base_path, name)
    return "http://%s:%d%s%s.ts" % (host, TS_PORT, base_path, name)


KEEP_SEGMENTS = int(os.environ.get("HDHR_KEEP_SEGMENTS", "8"))
TS_DIRECT = os.environ.get("HDHR_TS_DIRECT") == "1"
# BRP push path is the real tuner: api6+api9 make the M1 push Data-framed MPEG-TS
# to us on :31061. The HLS window on :8076 never retunes, so it always shows one
# channel -- HDHR_BRIP=0 keeps that legacy path only for debugging.
BRIP = os.environ.get("HDHR_BRIP", "1") == "1"
_brp_tuner = None


def get_brp_tuner():
    global _brp_tuner
    if not _m1_ready.is_set():
        # brptune reads ZM1 at import: importing it while ZM1 is still "auto"
        # would pin it to the literal host "auto" for good.
        raise RuntimeError("M1 not discovered yet")
    if _brp_tuner is None:
        import brptune
        _brp_tuner = brptune.Tuner(devs=BRP_DEVS)
        _brp_tuner.bootstrap()
    return _brp_tuner
M1_HLS_PORT = int(os.environ.get("M1_HLS_PORT", "8076"))


def _pct(v, lo, hi):
    if v is None:
        return 0
    return max(0, min(100, int(round((v - lo) * 100.0 / (hi - lo)))))


TUNER_STATUS_TIMEOUT = float(os.environ.get("HDHR_TUNER_STATUS_TIMEOUT", "2.0"))
# Short cache so frequent /tuners.json polling cannot keep re-taking Tuner.lock
# (shared with tunes/heartbeat) and stall a stream. 0 disables the cache.
TUNER_STATUS_TTL = float(os.environ.get("HDHR_TUNER_STATUS_TTL", "5.0"))
_tuner_status_cache = {"t": 0.0, "rows": None}
_tuner_status_cache_lock = threading.Lock()


def tuner_status_rows():
    """/tuners.json backed by BRP api19, cached for TUNER_STATUS_TTL seconds.
    api19 only answers while a tuner holds a lease; str is dBm*10 and snr is
    dB*10, both mapped onto 0-100."""
    if TUNER_STATUS_TTL > 0:
        with _tuner_status_cache_lock:
            if (_tuner_status_cache["rows"] is not None
                    and time.time() - _tuner_status_cache["t"] < TUNER_STATUS_TTL):
                return _tuner_status_cache["rows"]
    rows = _tuner_status_rows_fresh()
    if TUNER_STATUS_TTL > 0:
        with _tuner_status_cache_lock:
            _tuner_status_cache["rows"] = rows
            _tuner_status_cache["t"] = time.time()
    return rows


def _tuner_status_rows_fresh():
    try:
        sts = get_brp_tuner().statuses(timeout=TUNER_STATUS_TIMEOUT)
    except Exception:
        sts = []
    rows = []
    for i in range(TUNER_COUNT):
        st = sts[i] if i < len(sts) else None
        rows.append({
            "Resource": i,
            "VctNumber": "",
            "VctName": "",
            "Frequency": int(st.get("frequency") or 0) if st else 0,
            "SignalStrengthPercent": _pct(st.get("str"), -800, -200) if st else 0,
            "SignalQualityPercent": _pct(st.get("snr"), 0, 400) if st else 0,
            "SymbolQualityPercent": 0,
            "NetworkRate": 0,
            "TargetIP": "none",
            "Protocol": (st or {}).get("protocol") or "",
            "Locked": bool(st.get("locked")) if st else False,
            "RFChannel": int(st.get("rf_channel") or 0) if st else 0,
        })
    return rows


_rate_prev = {}
_rate_lock = threading.Lock()
_hls_info = {"ip": None, "t0": None}


def _streaming_rate(i, chan_id, nbytes, t0, now):
    """Float bits/s: delta since the previous poll of this slot; on the first
    poll, the average since the session started (0.0 if unknown)."""
    with _rate_lock:
        prev = _rate_prev.get(i)
        _rate_prev[i] = (chan_id, nbytes, now)
    if prev and prev[0] == chan_id and now > prev[2] and nbytes >= prev[1]:
        return float((nbytes - prev[1]) * 8.0 / (now - prev[2]))
    if t0 and now > t0:
        return float(nbytes * 8.0 / (now - t0))
    return 0.0


def _idle_row(i):
    return {"Resource": "tuner%d" % i, "VctNumber": "", "VctName": "",
            "Frequency": 0, "SignalStrengthPercent": 0,
            "SignalQualityPercent": 0, "SymbolQualityPercent": 0,
            "StreamingRate": 0.0, "TargetIP": ""}


def status_rows(chans, sts, info, now=None):
    """/status.json: top-level array, one entry per tuner (always
    TUNER_COUNT), Resource is the string 'tunerN'. Idle: empty strings, 0,
    TargetIP "". Live: channel, api19 signal (fixed 100 if unavailable),
    StreamingRate (float bits/s) and the client IP as TargetIP."""
    now = time.time() if now is None else now
    by_id = {}
    for r in lu.rows(chans, XMLTV_PREFIX):
        by_id.setdefault(str(r.chan.get("chan_id", "")).lower(), r)
    out = []
    for i in range(TUNER_COUNT):
        row = _idle_row(i)
        live = info[i] if i < len(info) else None
        if live:
            st = (sts[i] if i < len(sts) else None) or {}
            r = by_id.get(str(live["chan_id"]).lower())
            strength = (_pct(st.get("str"), -800, -200)
                        if st.get("str") is not None else 100)
            quality = (_pct(st.get("snr"), 0, 400)
                       if st.get("snr") is not None else 100)
            row.update({
                "VctNumber": r.gn if r else "",
                "VctName": r.name if r else "",
                "Frequency": int(st.get("frequency") or 0),
                "SignalStrengthPercent": strength,
                "SignalQualityPercent": quality,
                "SymbolQualityPercent": 100,
                "StreamingRate": _streaming_rate(
                    i, live["chan_id"], live["nbytes"], live.get("t0"), now),
                # streaming but client unknown: any non-empty non-"none" value
                "TargetIP": live.get("client_ip") or "0.0.0.0",
            })
        out.append(row)
    return out


def _live_info_all():
    """BRP slots from Tuner.live_info(), plus the HLS tuner in the last slot."""
    info = []
    try:
        info = list(get_brp_tuner().live_info())
    except Exception:
        info = [None] * len(BRP_DEVS)
    info += [None] * (len(BRP_DEVS) - len(info))
    if HLS_TUNER and _hls_live[0] > 0 and len(info) < TUNER_COUNT:
        info.append({"chan_id": "hls:", "nbytes": 0,
                     "client_ip": _hls_info["ip"], "t0": _hls_info["t0"]})
    return info


def status_json_rows(chans):
    info = _live_info_all()
    sts = tuner_status_rows_raw() if any(info) else []
    rows = status_rows(chans, sts, info)
    if HLS_TUNER and _hls_live[0] > 0 and len(BRP_DEVS) < len(rows):
        h = rows[len(BRP_DEVS)]
        h.update(VctNumber=HLS_GUIDE, VctName=HLS_NAME,
                 SignalStrengthPercent=100, SignalQualityPercent=100)
    return rows


def tuner_status_rows_raw():
    """api19 statuses, 5 s cache shared in spirit with /tuners.json."""
    with _tuner_status_cache_lock:
        c = _tuner_status_cache.get("raw")
        if c and time.time() - c[0] < TUNER_STATUS_TTL:
            return c[1]
    try:
        sts = get_brp_tuner().statuses(timeout=TUNER_STATUS_TIMEOUT)
    except Exception:
        sts = []
    with _tuner_status_cache_lock:
        _tuner_status_cache["raw"] = (time.time(), sts)
    return sts


def hls_base(proto, slot="1"):
    return "http://%s:%d/atsc%s/%s/" % (M1, M1_HLS_PORT, proto, slot)


def http_get(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def seg_names(text):
    """M1 playlists interleave mangled ##XTINF/##p_... lines; those start with #
    so the usual comment filter drops them along with the real tags."""
    return [ln.strip() for ln in text.splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def seq_of(name):
    m = re.search(r"(\d+)$", name)
    return int(m.group(1)) if m else 0


def _fix_tag(line, host, base_path):
    if 'URI="' in line:
        return re.sub(r'URI="([^"]+)"',
                      lambda m: 'URI="%s"' % abs_uri(host, base_path, m.group(1)),
                      line)
    return line


def rewrite_m3u(text, host, base_path, keep_last=0):
    """Rewrite every media/rendition URI to our proxy; optionally trim a media
    playlist to its last keep_last segments (advancing EXT-X-MEDIA-SEQUENCE)."""
    head, pend, entries, seq = [], [], [], None
    text = text.replace("\x00", "")
    for line in text.splitlines():
        if line.startswith("##") or not line.strip():
            continue
        if line.startswith("#"):
            if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                try:
                    seq = int(line.split(":", 1)[1].strip())
                except ValueError:
                    seq = None
            if not entries and not pend and not line.startswith("#EXTINF"):
                head.append(line)
            else:
                pend.append(line)
            continue
        entries.append((pend, line.strip()))
        pend = []
    if entries and keep_last and len(entries) > keep_last:
        skip = len(entries) - keep_last
        entries = entries[skip:]
        if seq is not None:
            seq += skip
    out = []
    for line in head:
        if seq is not None and line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            out.append("#EXT-X-MEDIA-SEQUENCE:%d" % seq)
        else:
            out.append(_fix_tag(line, host, base_path))
    for tags, uri in entries:
        out.extend(_fix_tag(t, host, base_path) for t in tags)
        out.append(abs_uri(host, base_path, uri))
    return "\n".join(out) + "\n"

guide_number = lu.guide_number

def proto_num(c):
    return "3" if "3" in str(c.get("protocol", "")) else "1"


visible = lu.visible
#: GuideName per guide number; non-unique call letters get the number.
display_names = lu.display_names
#: sha1 / integer LineupVersion from channel numbers + names (lineup.py).
lineup_hash = lu.lineup_hash
lineup_version = lu.lineup_version


#: VideoCodec/AudioCodec are learned passively from the PMT of streams clients
#: actually tune (codeclearn.py) and cached per (M1 id, guide number, GuideName)
#: in ZCODEC_CACHE (default: codec_cache.json next to the log, i.e. /data in
#: Docker). api8 rows carry no codec field. A channel nobody has tuned yet (or
#: 3.0/DRM, hls: rows) omits both keys rather than guessing; a row may carry
#: just one of the keys. ZCODEC_LEARN=0 turns learning off (cache is still read).
CODEC_CACHE_FILE = os.environ.get("ZCODEC_CACHE") or os.path.join(
    os.path.dirname(os.environ["HDHR_LOG"]) if os.environ.get("HDHR_LOG") else HERE,
    "codec_cache.json")
CODEC_LEARN = os.environ.get("ZCODEC_LEARN", "1") != "0"
CODECS = codeclearn.CodecCache(CODEC_CACHE_FILE)


def _codec_eligible(r):
    return not (proto_num(r.chan) != "1"
                or str(r.chan.get("chan_id", "")).startswith("hls:")
                or r.key != "v" + r.gn)


def codec_fields(r):
    """HDHomeRun-style VideoCodec/AudioCodec for a lu.rows() row, {} if not yet
    learned. Only the primary row for a guide number (URL key v<GN>) and only
    ATSC 1.0."""
    if not _codec_eligible(r):
        return {}
    return CODECS.lookup(BRP_DEV, r.gn, r.name)


def codec_learner_for(chans, ch):
    """StreamLearner for the row serving ``ch`` (None when not applicable)."""
    if not CODEC_LEARN:
        return None
    try:
        for r in lu.rows(chans, XMLTV_PREFIX):
            if r.chan is ch or r.chan.get("chan_id") == ch.get("chan_id"):
                if not _codec_eligible(r):
                    return None
                return codeclearn.StreamLearner(CODECS, BRP_DEV, r.gn, r.name)
    except Exception:
        pass
    return None


#: Suffix on the /lineup.json ETag: LineupVersion/hash only cover number+name,
#: so bump this when the row *shape* changes so conditional clients refetch.
LINEUP_JSON_SHAPE = "c1"


def lineup_rows(chans, host):
    """/lineup.json rows. URL is /auto/v<GN>, or /auto/zb<chan_id> for a
    second station that shares a guide number."""
    out = []
    for r in lu.rows(chans, XMLTV_PREFIX):
        c = r.chan
        row = {
            "GuideNumber": r.gn,
            "GuideName": r.name,
            "Tags": "",
            "URL": "http://%s:%d/auto/%s" % (host.split(":")[0], TS_PORT, r.key),
            "HD": 1 if (c.get("v_res") or 0) >= 720 else 0,
            "Modulation": "8vsb" if proto_num(c) == "1" else "atsc3",
            "DRM": 1 if c.get("encrypted") else 0,
        }
        row.update(codec_fields(r))
        out.append(row)
    return out


EPG = epgmod.EpgCache(owner=M1)
#: Opt-in filler programmes for channels the M1 has no guide data for
#: (e.g. 8.1-8.3 WILC, 40.3 here). 0 = off (default, historical output).
EPG_PLACEHOLDER_H = float(os.environ.get("ZEPG_PLACEHOLDER_H", "0"))
EPG_DEFER_LIVE = os.environ.get("ZEPG_DEFER_LIVE", "1") == "1"
EPG_WATCH = float(os.environ.get("ZEPG_WATCH", "300"))
_epg_built = {"v": None}


def _epg_fetch_factory(inst):
    def fetch(c):
        cid = str(c.get("chan_id", ""))
        if not cid.startswith("0x"):
            return None
        try:
            r = brp_call(12, a0=inst, payload=json.dumps({"chan_id": cid}).encode(),
                         timeout=15)
        except Exception as e:
            print("epg api12 %s err %r" % (guide_number(c), e), flush=True)
            return None
        body = (r.get("body") or b"").rstrip(b"\x00")
        if r.get("err_name") != "SUCCESS" or not body:
            return None
        try:
            return json.loads(body)
        except Exception:
            return None
    return fetch


def build_epg():
    """api6 (lease for api12) + api12 per channel. Deferred while BRP streams
    are live, and holds the tune gate so no tune starts mid-build."""
    tuner = _brp_tuner
    gate = None
    if tuner is not None:
        if EPG_DEFER_LIVE and tuner.health()["sessions_live"]:
            raise RuntimeError("deferred: BRP stream(s) live")
        gate = tuner.tune_gate
        if not gate.acquire(timeout=30):
            raise RuntimeError("deferred: tune gate busy")
    try:
        if tuner is not None and EPG_DEFER_LIVE and tuner.health()["sessions_live"]:
            raise RuntimeError("deferred: BRP stream(s) live")
        chans = get_lineup()
        r = brp_handshake()
        inst = r.get("a1") if r and r.get("err_name") == "SUCCESS" else None
        if not inst:
            raise RuntimeError("api6 lease for api12 failed: %s" % (
                r and r.get("err_name")))
        xml, ok, nprog = build_xmltv_for(chans, _epg_fetch_factory(inst))
        real = sum(1 for c in visible(chans) if str(c.get("chan_id", "")).startswith("0x"))
        if real and ok == 0:
            raise RuntimeError("api12 returned no programmes for %d channels" % real)
        if EPG.channels_ok and ok < EPG.channels_ok // 2:
            raise RuntimeError("api12 coverage collapsed %d -> %d" % (EPG.channels_ok, ok))
        _epg_built["v"] = lineup_version(chans)
        return xml, ok, nprog
    finally:
        if gate is not None:
            gate.release()


def build_xmltv_for(chans, fetch):
    """XMLTV for this M1's lineup: ids/names match /lineup.m3u row for row."""
    by_id = {id(r.chan): r for r in lu.rows(chans, XMLTV_PREFIX)}
    return epgmod.build_xmltv(
        chans, fetch, guide_number,
        lambda c: by_id[id(c)].name if id(c) in by_id else c["call_letters"].strip(),
        xmltv_id=lambda c: by_id[id(c)].xmltv_id if id(c) in by_id else None,
        placeholder_hours=EPG_PLACEHOLDER_H)


def _epg_lineup_watch(interval=EPG_WATCH):
    """Rebuild the guide when the lineup version changes, without waiting for the
    6 h cycle. Reuses build_epg, so it still defers while BRP streams are live and
    keeps serving the last-good file if a rebuild is deferred or fails."""
    def loop():
        time.sleep(float(os.environ.get("ZEPG_WATCH_DELAY", "60")))
        while True:
            try:
                cur = lineup_version(get_lineup())
                built = _epg_built["v"]
                if cur != built and not EPG.refreshing:
                    # built is None right after a restart: the on-disk guide may
                    # predate a rescan, so reconcile once to match the lineup.
                    reason = ("unknown -> %s (restart reconcile)" % cur
                              if built is None else "%s -> %s" % (built, cur))
                    print("epg: lineup version %s, rebuilding" % reason, flush=True)
                    EPG.refresh(blocking=True)
            except Exception as e:
                print("epg watch err: %r" % (e,), flush=True)
            time.sleep(interval)
    threading.Thread(target=loop, daemon=True).start()


class Json(BaseHTTPRequestHandler):
    def _j(self, obj, code=200, headers=None):
        b = json.dumps(obj).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(b)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client went away mid-response; nothing to report

    def _t(self, text, ctype="application/x-mpegurl", code=200, headers=None):
        b = text.encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(b)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _health(self):
        chans, _why = get_lineup_bounded()
        chans = chans or []
        nrows = len(visible(chans)) if chans else 0
        now = time.time()
        api8_ok = _api8["ok_at"] > 0 and _api8["ok_at"] >= _api8["fail_at"]
        t = _brp_tuner.health() if _brp_tuner is not None else None
        age = EPG.age_sec()
        return self._j({
            "ok": bool(api8_ok and nrows),
            "time": int(now),
            "api8": {"reachable": api8_ok,
                     "last_ok_age_sec": round(now - _api8["ok_at"], 1) if _api8["ok_at"] else None,
                     "last_fail_age_sec": round(now - _api8["fail_at"], 1) if _api8["fail_at"] else None,
                     "last_error": _api8["err"], "source": _api8["source"],
                     "err_name": _api8.get("err_name"),
                     "handshake": _api8.get("handshake"),
                     "hint": _api8.get("hint")},
            "lineup": {"channels": nrows, "version": lineup_version(chans) if chans else None},
            "tuner_count": TUNER_COUNT,
            "m1": {"ip": M1, "spec": _m1_info["spec"], "dev": _m1_info["dev"],
                   "source": _m1_info["source"]},
            "brp": t if t is not None else {"initialised": False, "leases": 0,
                                           "sessions_live": 0, "last_push_age_sec": None,
                                           "qdrops_total": 0, "qdrop_bursts_total": 0},
            "warnings": ([t["push_ip_warning"]]
                         if t and t.get("push_ip_warning") else []),
            "hls_streams_live": _hls_live[0],
            "ssdp": _ssdp[0].stats() if _ssdp[0] else {"enabled": False},
            "epg": {"present": EPG.xml is not None, "fresh": EPG.fresh(),
                    "age_sec": round(age, 1) if age is not None else None,
                    "channels_with_programmes": EPG.channels_ok,
                    "programmes": EPG.programmes,
                    "refreshing": EPG.refreshing, "last_error": EPG.last_error},
        })

    def _epg(self):
        xml = EPG.get(ensure=True)
        if xml is None:
            b = b"epg not ready"
            self.send_response(503)
            self.send_header("Retry-After", "300")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        b = xml.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Last-Modified", time.strftime(
            "%a, %d %b %Y %H:%M:%S GMT", time.gmtime(EPG.at)))
        self.end_headers()
        self.wfile.write(b)

    def do_HEAD(self):
        path = self.path.split("?", 1)[0]
        if webui.enabled() and webui.is_ui_path(path):
            return webui.handle(self, path, sys.modules[__name__], head=True)
        self.send_error(501, "Unsupported method ('HEAD')")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        host = self.headers.get("Host", "localhost")
        # liveness only (container HEALTHCHECK): static, no BRP/lock/lineup,
        # no token needed, reveals nothing. M1 reachability is shown in the UI.
        if path == "/healthz":
            return self._j({"ok": True})
        # read-only UI (ZUI=readonly, default off): cached state only
        if webui.enabled() and webui.is_ui_path(path):
            return webui.handle(self, path, sys.modules[__name__])
        if path == "/health":
            return self._health()
        if path == "/device.xml":
            return self._t(device_xml(), "text/xml; charset=utf-8")
        if path in ("/epg.xml", "/xmltv.xml"):
            return self._epg()
        if path == "/discover.json":
            # Static answer: needs no lineup and never waits on the M1. The
            # version comes from the cached lineup if there is one; otherwise
            # a background fetch is started so a later poll sees the real one.
            chans = cached_lineup()
            if chans is None and _m1_ready.is_set():
                _lineup_flight(False)
            chans = chans or []
            return self._j({
                "FriendlyName": FRIENDLY,
                "Manufacturer": "BitRouter",
                "ModelNumber": MODEL,
                "FirmwareName": "hdhomerun_atsc3",
                "FirmwareVersion": "20261102",
                "DeviceID": DEVICE_ID,
                "DeviceAuth": "zbemulator",
                "BaseURL": "http://%s" % host,
                "LineupURL": "http://%s/lineup.json" % host,
                "EPGUrl": "http://%s/epg.xml" % host,
                "LineupVersion": lineup_version(chans),
                "TunerCount": TUNER_COUNT,
            })
        if path not in _LINEUP_PATHS:
            if path == "/tuners.json":
                return self._j(tuner_status_rows())
            return self._j({"error": "not found"}, 404)
        chans, why = get_lineup_bounded(force="tuning" in self.path)
        hdr = {}
        if chans is None and path in ("/lineup_status.json", "/lineup.json",
                                      "/lineup.m3u") + PLAYLIST_PATHS:
            # M1 unreachable and nothing cached: answer, never hang.
            chans = []
            hdr = {"X-Zapperbridge-Lineup": "unavailable: %s" % why,
                   "Cache-Control": "no-store"}
        elif chans is None:
            return self._j({"error": why}, 502)
        elif why:
            hdr = {"X-Zapperbridge-Lineup": "stale: %s" % why}
        if path == "/lineup_status.json":
            return self._j({"ScanInProgress": 0, "ScanPossible": 1,
                            "Source": "Cable", "SourceList": ["Cable"],
                            "LineupVersion": lineup_version(chans)},
                           headers=hdr)
        if path == "/lineup.m3u":
            h = host.split(":")[0]
            return self._t(build_lineup_m3u(chans, h, host), headers=hdr)
        if path == "/lineup.json" and hdr:
            return self._j(lineup_rows(chans, host), headers=hdr)
        if path == "/lineup.json":
            etag = '"%s-%s-%s"' % (lineup_hash(chans)[:16], LINEUP_JSON_SHAPE,
                                   CODECS.token())
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.end_headers()
                return
            b = json.dumps(lineup_rows(chans, host)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("ETag", etag)
            self.end_headers()
            self.wfile.write(b)
            return
        if path in PLAYLIST_PATHS:
            return self._t(build_m3u(chans, host.split(":")[0]),
                           headers=hdr)
        if path == "/tuners.json":
            return self._j(tuner_status_rows())
        if path == "/status.json":
            return self._j(status_json_rows(chans))
        if path == "/tuners":
            rows = status_json_rows(chans)
            return self._j({"numTuners": len(rows), "tuners": rows})
        return self._j({"error": "not found"}, 404)

    def log_message(self, *a): pass

PLAYLIST_PATHS = ("/playlist.m3u", "/playlist.m3u8", "/channels.m3u")
#: Paths whose answer is built from the lineup (everything else needs none).
_LINEUP_PATHS = ("/lineup_status.json", "/lineup.json", "/lineup.m3u",
                 "/status.json", "/tuners") + PLAYLIST_PATHS

def build_m3u(chans, ip, port=None):
    """M3U of the lineup; stream URLs point at the TS port."""
    port = port or TS_PORT
    out = ["#EXTM3U"]
    for r in lu.rows(chans):
        c, num = r.chan, r.gn
        name = c["call_letters"].strip()
        grp = "ATSC%s" % proto_num(c)
        tag = " [DRM]" if c.get("encrypted") else ""
        out.append('#EXTINF:-1 tvg-chno="%s" tvg-id="%s" group-title="%s",%s%s'
                   % (num, num if not r.dup else r.key, grp, name, tag))
        out.append("http://%s:%d/auto/%s" % (ip, port, r.key))
    return "\n".join(out) + "\n"

def _xe(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def device_xml():
    """UPnP MediaServer root description (UDN derived from DeviceID)."""
    ip = ADVERTISE_IP
    base = "http://%s%s" % (ip, "" if HTTP_PORT == 80 else ":%d" % HTTP_PORT)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<root xmlns="urn:schemas-upnp-org:device-1-0">\n'
            '  <specVersion><major>1</major><minor>0</minor></specVersion>\n'
            '  <URLBase>%s</URLBase>\n'
            '  <device>\n'
            '    <deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>\n'
            '    <friendlyName>%s</friendlyName>\n'
            '    <manufacturer>BitRouter</manufacturer>\n'
            '    <modelName>%s</modelName>\n'
            '    <modelNumber>%s</modelNumber>\n'
            '    <serialNumber>%s</serialNumber>\n'
            '    <UDN>%s</UDN>\n'
            '    <presentationURL>%s/</presentationURL>\n'
            '  </device>\n'
            '</root>\n') % (_xe(base), _xe(FRIENDLY), _xe(MODEL), _xe(MODEL),
                            _xe(DEVICE_ID), ssdpmod.udn_for(DEVICE_ID), _xe(base))


def build_lineup_m3u(chans, ip, json_host):
    """M3U from the live api8 lineup; tvg-id == XMLTV channel id."""
    out = ['#EXTM3U url-tvg="http://%s/epg.xml" x-tvg-url="http://%s/epg.xml"'
           % (json_host, json_host)]
    for r in lu.rows(chans, XMLTV_PREFIX):
        num = r.gn
        out.append('#EXTINF:-1 tvg-id="%s" tvg-chno="%s" tvg-name="%s" '
                   'channel-number="%s" group-title="ATSC%s",%s'
                   % (r.xmltv_id, num, r.name, num, proto_num(r.chan), r.name))
        out.append("http://%s:%d/auto/%s" % (ip, TS_PORT, r.key))
    return "\n".join(out) + "\n"

_hls_live = [0]

#: Raw-TS stream pacing (BRP/M1 path only; the 999.1 HLS remux is untouched).
#: The M1 pushes ~2 s bursts; ZPACE=1 releases them at the PCR rate behind a
#: small start jitter buffer so clients see a steady stream. ZPACE=0 restores
#: the previous straight write-through exactly.
PACE = os.environ.get("ZPACE", "1") != "0"
PACE_PREBUF = float(os.environ.get("ZPACE_PREBUF_MS", "800")) / 1000.0
PACE_MAX_BUF = float(os.environ.get("ZPACE_MAX_BUF_MS", "6000")) / 1000.0
PACE_MAX_START = float(os.environ.get("ZPACE_MAX_START_MS", "1500")) / 1000.0
PACE_MAX_BYTES = int(os.environ.get("ZPACE_MAX_BYTES", str(12 * 1024 * 1024)))
PACE_AHEAD = float(os.environ.get("ZPACE_AHEAD_MS", "30")) / 1000.0
PACE_SNDBUF = int(os.environ.get("ZPACE_SNDBUF", str(1024 * 1024)))
#: Opt-in deeper startup prebuffer (seconds). Unset = unchanged (0.8 s prebuf,
#: 1.5 s max start). When set it overrides ZPACE_PREBUF_MS, raises the max start
#: so it is not cut short, and adds about (value - 0.8) s of tune latency. It
#: also applies when the pacer re-buffers after a source underrun.
PACE_PREBUF, PACE_MAX_START, _START_BUF_NOTE = tspacer.resolve_start_buf(
    os.environ.get("ZSTART_BUF_SEC"), PACE_PREBUF, PACE_MAX_START, PACE_MAX_BUF)
if os.environ.get("ZSTART_BUF_SEC", "").strip():
    print("startup prebuffer: prebuf=%.2fs max_start=%.2fs%s" % (
        PACE_PREBUF, PACE_MAX_START,
        " (%s)" % _START_BUF_NOTE if _START_BUF_NOTE else ""), flush=True)
if zdiag.enabled():
    print("full-stream diagnostics: ts_analysis=%s gap_ms=%g capture_seconds=%g%s%s" % (
        "on" if zstream._on("ZLOG_TS") else "off", zstream._fenv("ZGAP_MS", 100),
        zstream.capture_seconds(),
        " capture_dir=%s" % os.environ.get("ZCAPTURE_DIR", "/tmp/zapperbridge/captures")
        if zstream.capture_seconds() > 0 else "",
        " jsonl=on" if os.environ.get("ZDIAG_JSONL") else ""), flush=True)


def _wg_level(pc):
    """Lock-free, best-effort pacer buffer level (diagnostics only)."""
    try:
        return pc.level()
    except Exception:
        return 0.0


def _wg_tick(wg, pc):
    wg.tick(_wg_level(pc), pc.stats.get("rebuffers", 0))


class Stream(BaseHTTPRequestHandler):
    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _host(self):
        return (self.headers.get("Host") or "127.0.0.1").split(":")[0]

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _fetch(self, grp, slot, name):
        """M1 segment names carry no extension; our rewritten playlists advertise
        .ts for client compatibility, so retry the bare name on a 404."""
        names = [name]
        if name.endswith(".ts"):
            names.append(name[:-3])
        last = "unreachable"
        for n in names:
            url = "http://%s:8076/%s/%s/%s" % (M1, grp, slot, n)
            try:
                with urllib.request.urlopen(url, timeout=10) as r:
                    return r.read()
            except Exception as e:
                last = "%s: %s" % (url, e)
        raise RuntimeError(last)

    def _proxy(self, grp, slot, name):
        if ".." in name or not re.match(r'^[\w.\-]+$', name):
            self.send_error(400, "bad name")
            return
        try:
            data = self._fetch(grp, slot, name)
        except Exception as e:
            print("proxy fail %s" % (e,), flush=True)
            self.send_error(502, "m1 fetch failed")
            return
        if name.endswith(".m3u8"):
            text = data.decode("utf-8", "replace")
            keep = 0 if "#EXT-X-STREAM-INF" in text else KEEP_SEGMENTS
            body = rewrite_m3u(text, self._host(),
                               "/hls/%s/%s/" % (grp, slot), keep).encode()
            self._send(200, "application/vnd.apple.mpegurl", body)
        else:
            self._send(200, "video/mp2t", data)

    def _ts_headers(self):
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.flush()
        except Exception:
            pass

    def _tune_socket(self):
        import socket
        try:
            sk = self.connection
            sk.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sk.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, PACE_SNDBUF)
        except OSError as e:
            print("sock opts: %r" % (e,), flush=True)

    def _ts_brp_paced(self, sess):
        """Mux output -> Pacer (jitter buffer, PCR-rate release) -> client.
        The reader thread only moves packets into the pacer; the M1 push
        threads never touch this path, and a slow client only ever blocks this
        handler's own write (the pacer caps its buffer and skips ahead)."""
        self._tune_socket()
        pc = tspacer.Pacer(prebuf=PACE_PREBUF, max_buf=PACE_MAX_BUF,
                           max_bytes=PACE_MAX_BYTES, max_start=PACE_MAX_START,
                           ahead=PACE_AHEAD)
        stop = threading.Event()
        if webui.enabled():
            sess._zpacer = pc   # read-only UI: pacer stats on the live page
        #: diagnostics only: how the upstream (M1 push) side ended
        rstate = {"end": None}
        reason = None
        #: diagnostics only (ZLOG_DIAG): per-second write/input timing
        wg = None
        if zdiag.enabled():
            try:
                wg = zdiag.WriteGapMonitor(sess.chan_id)
            except Exception:
                wg = None
        #: passive codec learning (PMT of the stream being served anyway)
        cl = getattr(self, "_clearn", None)
        #: diagnostics only: output TS analyzer / M1 input stats / capture
        zs = zstream.StreamDiag.create(sess, getattr(self, "_zseq", None),
                                       getattr(self, "_zguide", None))

        def reader():
            try:
                for pkt in sess.packets():
                    pc.feed(pkt)
                    if cl is not None and not cl.done:
                        cl.feed(pkt)
                    if wg is not None:
                        wg.on_input(len(pkt))
                    if stop.is_set():
                        break
                else:
                    rstate["end"] = "upstream_ended"
            except Exception as e:
                rstate["end"] = "error"
                zdiag.tlog("brp pace reader error: %r" % (e,))
            finally:
                pc.close()
        rt = threading.Thread(target=reader, daemon=True)
        rt.start()
        try:
            while True:
                data, wait, done = pc.get()
                if wg is not None:
                    _wg_tick(wg, pc)
                if zs is not None:
                    zs.tick()
                if data:
                    if wg is None:
                        self.wfile.write(data)
                    else:
                        _t0 = wg.clock()
                        self.wfile.write(data)
                        wg.on_write(len(data), _t0, wg.clock(),
                                    _wg_level(pc), pc.dry)
                    if zs is not None:
                        zs.on_write(data)
                    continue
                if done:
                    reason = rstate["end"] or "pacer_done"
                    break
                pc.wait(0.25 if wait is None else min(wait, 0.25))
        except (BrokenPipeError, ConnectionResetError):
            reason = "client_closed"
        except Exception as e:
            reason = "error"
            zdiag.tlog("brp stream error: %r" % (e,))
        finally:
            stop.set()
            pc.close()
        self._zreason = reason or "unknown"
        if wg is not None:
            wg.finish(self._zreason)
            try:
                sess._zwgap = wg.summary   # for the opt-in request log
            except Exception:
                pass
        st = pc.stats
        zdiag.tlog("brp stream %s closed (paced) in=%d out=%d start_delay=%s "
              "skips=%d skipped_pkts=%d rebuffers=%d max_level=%.2fs mode=%s "
              "writes=%d qdrops=%d" % (
                  sess.chan_id, st["bytes_in"], st["bytes_out"],
                  None if st["start_delay"] is None else round(st["start_delay"], 2),
                  st["skips"], st["skipped_pkts"], st["rebuffers"],
                  st["max_level"], st["mode"], st["writes"],
                  getattr(sess, "qdrops", -1)))
        if zs is not None:
            zs.finish(self._zreason)

    def _ts_brp(self, sess, headers_sent=False):
        """Stream the tuned program straight from the M1's BRP pushes."""
        if not headers_sent:
            self._ts_headers()
        if PACE:
            return self._ts_brp_paced(sess)
        blk, n = [], 0
        reason = None
        zs = zstream.StreamDiag.create(sess, getattr(self, "_zseq", None),
                                       getattr(self, "_zguide", None))
        try:
            cl = getattr(self, "_clearn", None)
            for pkt in sess.packets():
                if cl is not None and not cl.done:
                    cl.feed(pkt)
                blk.append(pkt)
                if len(blk) >= 7:
                    _out = b"".join(blk)
                    self.wfile.write(_out)
                    if zs is not None:
                        zs.on_write(_out)
                        zs.tick()
                    blk = []
                    n += 7
                    if n % 128 == 0:
                        self.wfile.flush()
            reason = "upstream_ended"
        except (BrokenPipeError, ConnectionResetError):
            reason = "client_closed"
        except Exception as e:
            reason = "error"
            zdiag.tlog("brp stream error: %r" % (e,))
        if blk:
            try:
                _out = b"".join(blk)
                self.wfile.write(_out)
                if zs is not None:
                    zs.on_write(_out)
            except Exception:
                if reason == "upstream_ended":
                    reason = "client_closed"
        self._zreason = reason or "unknown"
        zdiag.tlog("brp stream %s closed after %d packets qdrops=%d qdrop_bursts=%d "
              "qhigh=%d qmax=%d queue_wait=%.2fs" % (
                  sess.chan_id, n, getattr(sess, "qdrops", -1),
                  getattr(sess, "qdrop_bursts", -1), getattr(sess, "qhigh", -1),
                  getattr(__import__("brptune"), "QMAX", -1),
                  getattr(sess, "queue_wait", 0.0)))
        if zs is not None:
            zs.finish(self._zreason)

    def _ts_hls(self):
        """Stream the M1's own tuner: its fixed HLS window, remuxed into one TS."""
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        blk, n = [], 0
        try:
            for pkt in hlstuner.HlsTuner(HLS_PROTO, HLS_SLOT).packets():
                blk.append(pkt)
                if len(blk) >= 128:
                    self.wfile.write(b"".join(blk))
                    n += len(blk)
                    blk = []
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            print("hls stream error: %r" % (e,), flush=True)
        if blk:
            try:
                self.wfile.write(b"".join(blk))
            except Exception:
                pass
        print("hls stream %s closed after %d packets" % (HLS_GUIDE, n), flush=True)

    def _ts_direct(self, proto, fresh=False):
        """Push the M1 HLS window as one continuous MPEG-TS stream.

        The M1 serves only the tuned channel and numbers video/audio segments with
        the same sequence, so pair them per segment to keep A/V close."""
        base = hls_base(proto)
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        sent_v, stalls = 0, 0
        while stalls < 40:
            try:
                vtext = http_get(base + "video.m3u8").decode("utf-8", "replace")
            except Exception as e:
                stalls += 1
                print("pl video: %s" % (e,), flush=True)
                time.sleep(2)
                continue
            amap = {}
            try:
                atext = http_get(base + "audio1.m3u8").decode("utf-8", "replace")
                amap = {seq_of(n): n for n in seg_names(atext)}
            except Exception as e:
                print("pl audio: %s" % (e,), flush=True)
            vsegs = [(seq_of(n), n) for n in seg_names(vtext)]
            vsegs = [x for x in vsegs if x[0] > sent_v]
            if not vsegs:
                stalls += 1
                time.sleep(1.5)
                continue
            stalls = 0
            if sent_v == 0:
                vsegs = vsegs[-1:] if fresh else vsegs[-3:]
            for seq, name in vsegs:
                try:
                    data = http_get(base + name)
                except Exception as e:
                    print("seg %s: %s" % (name, e), flush=True)
                    continue
                aname = amap.get(seq)
                if aname:
                    try:
                        data += http_get(base + aname)
                    except Exception as e:
                        print("aseg %s: %s" % (aname, e), flush=True)
                try:
                    self.wfile.write(data)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                sent_v = seq

    def _tune(self, chan_id):
        import socket, struct
        cid = json.dumps({"chan_id": chan_id}).encode()
        hdr = bytearray(64)
        hdr[0:4]=b'BRrp'; hdr[4:8]=bytes([0,0,0x10,0])
        hdr[8:24]=EMU_ID.encode()[:16].ljust(16,b'\x00')
        global TUNE_SEQ
        TUNE_SEQ = (TUNE_SEQ + 1) & 0xFFFF
        struct.pack_into('>H',hdr,24,TUNE_SEQ)
        struct.pack_into('>H',hdr,26,9)
        struct.pack_into('>I',hdr,28,3)
        struct.pack_into('>I',hdr,40,0)
        struct.pack_into('>I',hdr,44,len(cid)+1)
        struct.pack_into('>I',hdr,48,0xC0DEFA11)
        try:
            s=socket.create_connection((M1,31065),timeout=3)
            s.sendall(bytes(hdr))
            import time as _t; _t.sleep(0.05)
            s.sendall(cid+b'\x00')
            s.settimeout(3)
            rsp = s.recv(64)
            s.close()
            if len(rsp) >= 64:
                ver = struct.unpack_from('>I', rsp, 4)[0]
                api = struct.unpack_from('>H', rsp, 26)[0]
                err = struct.unpack_from('>I', rsp, 28)[0]
                print('tune rsp api=%d err=%d ver=0x%x' % (api, err, ver), flush=True)
        except Exception as e:
            print('tune err:', e, flush=True)

    def _tune2(self, chan_id):
        """BRP tune. The M1 serves one BRP session at a time, so the whole sequence
        (handshake + tune, heartbeat included) runs under one lock; api10 only when
        something is actually tuned. api9 answers with the tuned channel JSON, which is
        the proof the tune landed -- a failed tune returns False, never the old channel."""
        if _tuned["chan_id"] == chan_id:
            _tuned["changed"] = False
            return True
        cid = json.dumps({"chan_id": chan_id}).encode()
        with _brp_lock:
            _hb["stop"] = True
            th = _hb["thread"]
            if th is not None:
                try:
                    th.join(timeout=3)
                except Exception:
                    pass
            time.sleep(BRP_GAP)
            if _tuned["chan_id"]:
                try:
                    s10 = brp_call(10, len2=0)
                    print("api10 -> %s dt=%.1f" % (s10.get("err_name"), s10.get("dt", 0)), flush=True)
                except Exception:
                    pass
                time.sleep(BRP_GAP)
            for attempt in range(4):
                _tuned["chan_id"] = None
                try:
                    brp_call(6, a0=0, len2=0)
                    time.sleep(BRP_GAP)
                    r = brp_call(9, a3=0xC0DEFA11, payload=cid)
                    err = r.get("err_name")
                    if err == "SUCCESS":
                        got = {}
                        try:
                            got = json.loads((r.get("body") or b"{}").decode("utf-8", "replace"))
                        except Exception:
                            pass
                        _tuned["chan_id"] = chan_id
                        _tuned["changed"] = True
                        start_heartbeat()
                        print("tune2 %s -> %s %s (try %d)" % (
                            chan_id, str(got.get("call_letters", "?")).strip(),
                            got.get("chan_id", "no-body"), attempt + 1), flush=True)
                        if not got.get("chan_id") or got.get("chan_id") == chan_id:
                            return True
                        continue
                    print("tune2 %s -> %s (try %d)" % (chan_id, err, attempt + 1), flush=True)
                except Exception as e:
                    print("tune2 err: %r (try %d)" % (e, attempt + 1), flush=True)
                time.sleep(BRP_GAP)
        return False

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in PLAYLIST_PATHS:
            chans, _why = get_lineup_bounded()
            body = build_m3u(chans or [], self._host(), TS_PORT).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/x-mpegurl")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/hls/"):
            parts = path.split("/")
            if len(parts) == 5 and parts[2].startswith("atsc"):
                self._proxy(parts[2], parts[3], parts[4])
            else:
                self.send_error(404, "bad hls path")
            return
        num = path.split("/auto/", 1)[-1] if "/auto/" in path else ""
        if not _m1_ready.is_set():
            return self.send_error(503, "M1 not discovered yet")
        try:
            chans = get_lineup()
        except Exception:
            chans = []
        ch = lu.find(chans, num)
        if ch is None:
            zdiag.tlog("no lineup entry for %s" % num)
            return self.send_error(404, "unknown channel")
        if HLS_TUNER and str(ch.get("chan_id", "")).startswith("hls:"):
            _hls_live[0] += 1
            _hls_info.update(ip=self.client_address[0], t0=time.time())
            try:
                self._ts_hls()
            finally:
                _hls_live[0] -= 1
            return
        if BRIP:
            # Soft model: accept the tune URL, then stream
            # or end the stream cleanly. A 503 here makes clients mark the
            # whole tuner as gone, so busy/timeout/failed tunes close with 0 B.
            tuner = get_brp_tuner()
            self._ts_headers()
            t_req = time.monotonic()
            self._zseq = zdiag.next_seq() if zdiag.enabled() else None
            self._zguide = zdiag.clean(num, 24) if zdiag.enabled() else None
            self._zlog_stream("stream request", ch["chan_id"])
            self._zlog_client(ch["chan_id"])
            self._clearn = codec_learner_for(chans, ch)
            sess = tuner.tune(ch["chan_id"])
            if sess is not None:
                sess.client_ip = self.client_address[0]
                sess.t0 = time.time()
            if sess is None:
                lf = tuner.last_fail or {}
                zdiag.tlog("tune %s soft-fail (%s): stream closed with 0 bytes" % (
                    ch["chan_id"], lf.get("reason", "unknown")))
                self._zlog_stream("stream close", ch["chan_id"],
                                  "reason=tune_failed(%s) dur=%.2fs" % (
                                      lf.get("reason", "unknown"),
                                      time.monotonic() - t_req))
                self.close_connection = True
                return
            t_open = time.monotonic()
            webui.stream_open(sess, ch["chan_id"], self.client_address[0],
                              self.headers.get("User-Agent"))
            reqlog.log_open(ch["chan_id"], self.client_address[0],
                            self.headers.get("User-Agent"), self.path)
            self._zlog_stream("stream open", ch["chan_id"],
                              "tune_took=%.2fs" % (t_open - t_req))
            self._zlog_bind(ch["chan_id"], sess, t_open - t_req)
            self._zreason = None
            try:
                self._ts_brp(sess, headers_sent=True)
            finally:
                try:
                    webui.stream_close(sess, getattr(self, "_zreason", None),
                                       getattr(sess, "nbytes", None))
                    reqlog.log_close(ch["chan_id"], self.client_address[0],
                                     self.headers.get("User-Agent"), self.path,
                                     getattr(sess, "nbytes", None),
                                     time.monotonic() - t_open,
                                     getattr(self, "_zreason", None),
                                     getattr(sess, "_zwgap", None))
                    tuner.stop(sess)
                finally:
                    self._zlog_stream(
                        "stream close", ch["chan_id"],
                        "reason=%s dur=%.2fs bytes=%s" % (
                            getattr(self, "_zreason", None) or "unknown",
                            time.monotonic() - t_open,
                            getattr(sess, "nbytes", "?")))
            return
        if not self._tune2(ch["chan_id"]):
            return self.send_error(503, "tune failed")
        proto = proto_num(ch) if ch else "1"
        fresh = bool(_tuned.get("changed"))
        if fresh:
            time.sleep(2.5)
        if TS_DIRECT:
            self._ts_direct(proto, fresh)
            return
        url = "http://%s:%d/hls/atsc%s/1/master.m3u8" % (self._host(), TS_PORT, proto)
        self.send_response(302)
        self.send_header("Location", url)
        self.end_headers()

    def log_message(self, *a): pass

    def _zlog_stream(self, what, chan_id, extra=""):
        """Diagnostic open/close line: client IP, User-Agent and request path
        (query string dropped; no other headers, so no cookies/tokens)."""
        if not zdiag.enabled():
            return
        try:
            zdiag.dlog("%s chan=%s client=%s ua=%s path=%s %s" % (
                what, chan_id, zdiag.clean(self.client_address[0], 64),
                zdiag.clean(self.headers.get("User-Agent")),
                zdiag.safe_path(self.path), extra))
        except Exception:
            pass

    def _zlog_client(self, chan_id):
        """Per-stream client line with a sequence id (diagnostics only; the
        query string and every header except User-Agent are dropped)."""
        if not zdiag.enabled():
            return
        try:
            ip, port = self.client_address[0], self.client_address[1]
            ua = zdiag.clean(self.headers.get("User-Agent"))
            path = zdiag.safe_path(self.path)
            zdiag.dlog("stream client seq=%s chan=%s guide=%s client=%s:%s ua=%s "
                       "path=%s http=%s" % (
                           self._zseq, chan_id, self._zguide, zdiag.clean(ip, 64),
                           port, ua, path,
                           zdiag.clean(getattr(self, "request_version", "-"), 16)))
            zstream.jsonl("stream_client", seq=self._zseq, chan=chan_id,
                          guide=self._zguide, client=ip, port=port, ua=ua, path=path)
        except Exception:
            pass

    def _zlog_bind(self, chan_id, sess, tune_took):
        """Ties the client's seq to the tune/session (diagnostics only)."""
        if not zdiag.enabled():
            return
        try:
            zdiag.dlog("stream bind seq=%s tune_seq=%s chan=%s guide=%s tune_took=%.2fs "
                       "claim=%.2fs queue_wait=%.2fs" % (
                           self._zseq, getattr(sess, "tune_seq", "-"), chan_id,
                           self._zguide, tune_took, getattr(sess, "claim_wait", 0.0),
                           getattr(sess, "queue_wait", 0.0)))
        except Exception:
            pass


def daemonize():
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    os.chdir(HERE)
    log = os.environ.get("HDHR_LOG", os.path.join(HERE, "hdhr_facade.log"))
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    for n in (0, 1, 2):
        os.dup2(fd, n)

def _ts_serve(srv):
    try:
        srv.serve_forever()
    except Exception as e:
        print("TS server died: %r" % (e,), flush=True)

#: Seconds between "still waiting for an M1" log lines while auto-discovering.
DISCOVER_LOG_SEC = float(os.environ.get("ZM1_DISCOVER_LOG_SEC", "30"))
DISCOVER_HINT = (
    "auto-discovery listens for the M1's UDP 31064 broadcast, which does not "
    "reach a Docker bridge network or another subnet/VLAN: use host or macvlan "
    "networking, or skip discovery by setting the M1 address (ZM1=<ip> for "
    "the bare facade, ZB_M1=<ip> or [[m1]] address = \"<ip>\" in the config)")


def _apply_m1(ip, dev, why):
    """Adopt the discovered M1. brptune/hlstuner read ZM1 from the env (brptune
    is imported lazily, only after _m1_ready is set: do not add a top-level
    `import brptune`, it would read ZM1 while it is still "auto")."""
    global M1
    M1 = ip
    os.environ["ZM1"] = ip
    hlstuner.M1 = ip
    EPG.owner = ip
    if EPG.xml is None:
        EPG._load_disk()
    _m1_info.update(dev=dev, source="auto: %s" % why)
    _m1_ready.set()


def start_m1_discovery(on_found, log_every=None, resolver=None, sleep=time.sleep):
    """ZM1=auto[:dev|ip]: find the M1 from its own announce in the background,
    forever, while the HTTP side is already serving (/healthz OK, empty
    lineup). Calls on_found() once, after the M1 is adopted. Returns the
    thread, or None if the M1 address is literal / already known."""
    if _m1_ready.is_set():
        return None
    log_every = DISCOVER_LOG_SEC if log_every is None else log_every
    import m1disco
    resolver = resolver or m1disco.resolve_m1

    def loop():
        lst = None
        try:
            lst = m1disco.Listener().start()
        except Exception as e:
            print("m1 auto-discovery: cannot listen on UDP %d (%s)" % (
                m1disco.PORT, e), flush=True)
        t0 = time.time()
        while not _m1_ready.is_set():
            try:
                kw = {"listener": lst} if lst is not None else {}
                ip, dev, why = resolver(M1_SPEC, wait=max(log_every, 1.0), **kw)
            except Exception as e:
                ip, dev, why = None, None, "error: %r" % (e,)
            if ip:
                print("m1 auto-discovery (%s): %s dev=%s (%s)" % (
                    M1_SPEC, ip, dev, why), flush=True)
                _apply_m1(ip, dev, why)
                if lst is not None:
                    lst.shutdown()
                try:
                    on_found()
                except Exception as e:
                    print("post-discovery start error: %r" % (e,), flush=True)
                return
            print("m1 auto-discovery (%s): still waiting after %ds (%s); %s. "
                  "Serving /healthz and an empty lineup meanwhile." % (
                      M1_SPEC, int(time.time() - t0), why, DISCOVER_HINT),
                  flush=True)
            if why.startswith("ambig"):
                sleep(log_every)  # pick_m1 returns at once; do not spin
    th = threading.Thread(target=loop, daemon=True, name="m1-discovery")
    th.start()
    return th


def _after_m1_ready():
    """Everything that needs the M1 address: guide builder and SSDP."""
    global ADVERTISE_IP
    if os.environ.get("ZEPG", "1") == "1":
        EPG.start_background(build_epg)
        _epg_lineup_watch()
    if not ADVERTISE_IP:
        try:
            ADVERTISE_IP = ssdpmod.primary_ip(M1)
        except Exception:
            ADVERTISE_IP = "127.0.0.1"
    if SSDP_ON:
        try:
            _ssdp[0] = ssdpmod.Ssdp(ADVERTISE_IP, HTTP_PORT, DEVICE_ID,
                                    port=SSDP_PORT,
                                    log=lambda m: print(m, flush=True)).start()
        except Exception as e:
            print("ssdp disabled (%s). Normal on a Docker bridge network, which "
                  "has no LAN multicast; HDHomeRun apps can still be added by "
                  "IP, or use host/macvlan networking for auto-discovery."
                  % (e,), flush=True)


if __name__ == "__main__":
    if os.environ.get("HDHR_DAEMON") == "1":
        daemonize()
    webui.init()
    try:
        ts_srv = ThreadingHTTPServer(("0.0.0.0", TS_PORT), Stream)
    except OSError as e:
        print("TS bind fail :%d %r" % (TS_PORT, e), flush=True)
        raise SystemExit(1)
    t = threading.Thread(target=_ts_serve, args=(ts_srv,), daemon=True)
    t.start()
    if _m1_ready.is_set():
        _after_m1_ready()
    else:
        print("m1 auto-discovery (%s): waiting for the M1 in the background; "
              "serving now. %s" % (M1_SPEC, DISCOVER_HINT), flush=True)
        start_m1_discovery(_after_m1_ready)

    def _bye(signum, frame):
        print("signal %d: shutting down" % signum, flush=True)
        if _ssdp[0]:
            _ssdp[0].shutdown()
        os._exit(0)
    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)
    print("HDHR emulator: JSON on :%d  TS-redirect on :%d" % (HTTP_PORT, TS_PORT), flush=True)
    ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Json).serve_forever()
