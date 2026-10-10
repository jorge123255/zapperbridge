"""Read-only web UI for the ZapperBox facade (stdlib only, OFF by default).

Enable with ``ZUI=readonly`` (anything else, including unset, leaves the UI
completely off: /ui and /api/ui/* then behave exactly like any unknown path).

Design rules (see docs/WEBUI.md):
  * GET/HEAD only; there is no write endpoint and nothing here can change
    state, restart, tune, sign out, or run a command.
  * Cached state only. It never joins the M1, never calls a BRP api, never
    asks the lineup to refresh, never bootstraps the tuner. If the facade has
    not built a tuner yet the pages say so.
  * LAN only: requests from non-private source addresses get 403.
  * Optional ``ZUI_TOKEN``: when set (>= 8 chars) every /ui and /api/ui
    request must carry it as ``Authorization: Bearer <token>`` or the
    ``zui_token`` cookie (``/ui?token=<token>`` sets the cookie once).
  * No secrets: nothing from config files or env beyond the knobs listed in
    ``/api/ui/state``; device ids/IPs are LAN identifiers already served by
    /discover.json and /tuners.json.
  * The aliases page prints ifconfig / ip commands for you to copy; the
    bridge never runs them.
  * Static files come from ``ui/`` next to this file (no build step); the JS
    renders everything with textContent (User-Agent strings are untrusted).
"""
import collections
import hmac
import ipaddress
import json
import os
import re
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(HERE, "ui")
VERSION = "webui-readonly-2"
STARTED = time.time()
CLOSED_KEEP = 50
_PATH_OK = re.compile(r"^[A-Za-z0-9_.\-]+$")
_MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
         ".json": "application/json"}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; "
       "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'")

_lk = threading.Lock()
_live = {}                                   # id(sess) -> record
_closed = collections.deque(maxlen=CLOSED_KEEP)
_seq = [0]                                  # bumped on every stream open/close
_registry = [None]                           # optional m1disco.Registry


def enabled():
    """True only for ZUI=readonly (case-insensitive). Default OFF."""
    return os.environ.get("ZUI", "").strip().lower() == "readonly"


def init():
    """Called once at facade start-up when enabled: start the in-memory log
    ring, and (only with ZUI_DISCO=1) a receive-only UDP 31064 listener so the
    Devices page can show announces. Both are passive."""
    if not enabled():
        return
    import zdiag
    zdiag.ring_enable(int(os.environ.get("ZUI_LOG_LINES", "500") or 500))
    if os.environ.get("ZUI_DISCO", "0") == "1" and _registry[0] is None:
        try:
            import m1disco
            _registry[0] = m1disco.Listener().start().registry
        except Exception as e:  # port busy etc: the page just shows none
            print("webui: passive discovery off: %r" % (e,), flush=True)


def set_registry(reg):
    _registry[0] = reg


def is_ui_path(path):
    return path == "/ui" or path.startswith("/ui/") or path.startswith("/api/ui/")


# ---- live stream registry (fed by hdhr_emulator.Stream, no-ops when off) ----
def stream_open(sess, chan_id, client_ip, ua, t_request=None):
    if not enabled():
        return
    try:
        with _lk:
            _live[id(sess)] = {"sess": sess, "chan_id": str(chan_id),
                               "client_ip": str(client_ip), "ua": str(ua or "-")[:200],
                               "t0": time.time()}
            _seq[0] += 1
    except Exception:
        pass


def stream_close(sess, reason=None, nbytes=None):
    if not enabled():
        return
    try:
        with _lk:
            rec = _live.pop(id(sess), None)
        if rec:
            _closed.append({"chan_id": rec["chan_id"], "client_ip": rec["client_ip"],
                            "ua": rec["ua"], "start": rec["t0"], "end": time.time(),
                            "reason": reason or "unknown", "bytes": nbytes,
                            "pacer": _pacer_stats(rec["sess"]),
                            "wgap": getattr(rec["sess"], "_zwgap", None)})
            _seq[0] += 1
    except Exception:
        pass


def _pacer_stats(sess):
    pc = getattr(sess, "_zpacer", None)
    if pc is None:
        return None
    try:
        st = dict(pc.stats)
        keep = ("bytes_in", "bytes_out", "start_delay", "skips", "skipped_pkts",
                "rebuffers", "max_level", "mode", "writes", "pcr_resets")
        out = {k: st.get(k) for k in keep if k in st}
        try:
            out["level"] = round(float(pc.level()), 2)
        except Exception:
            pass
        return out
    except Exception:
        return None


# ---- snapshots (all cached state) -------------------------------------------
def _ts(t):
    return None if not t else time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t))


def _tuner(emu):
    """The facade's tuner object if it already exists. Never creates one."""
    return getattr(emu, "_brp_tuner", None)


def snap_streams(emu, now=None):
    now = time.time() if now is None else now
    live = []
    with _lk:
        recs = list(_live.values())
    for r in recs:
        s = r["sess"]
        live.append({"chan_id": r["chan_id"], "client_ip": r["client_ip"],
                     "ua": r["ua"], "start": _ts(r["t0"]),
                     "age_sec": round(now - r["t0"], 1),
                     "bytes": getattr(s, "nbytes", None),
                     "qdrops": getattr(s, "qdrops", None),
                     "pacer": _pacer_stats(s)})
    live.sort(key=lambda x: x["start"] or "")
    closed = [dict(c, start=_ts(c["start"]), end=_ts(c["end"]),
                   dur_sec=round(c["end"] - c["start"], 1))
              for c in list(_closed)]
    hls = getattr(emu, "_hls_live", [0])[0]
    return {"live": live, "hls_live": hls, "recent_closed": closed[::-1],
            "seq": _seq[0]}


def snap_lineup(emu):
    """Cached lineup only (hdhr_emulator._cache); never calls api8."""
    cache = getattr(emu, "_cache", {})
    chans = list(cache.get("channels") or [])
    rows = []
    try:
        for r in emu.lu.rows(chans, getattr(emu, "XMLTV_PREFIX", "")):
            c = r.chan
            rows.append({"number": r.gn, "name": r.name, "chan_id": str(c.get("chan_id", "")),
                         "protocol": str(c.get("protocol", "")),
                         "hd": 1 if (c.get("v_res") or 0) >= 720 else 0,
                         "drm": 1 if c.get("encrypted") else 0})
    except Exception:
        rows = []
    return {"count": len(rows), "cached_age_sec": (round(time.time() - cache["at"], 1)
                                                    if cache.get("at") else None),
            "hls_tuner": bool(getattr(emu, "HLS_TUNER", False)), "channels": rows}


def _slots(emu):
    t = _tuner(emu)
    out = []
    if t is None:
        return out, False
    try:
        info = t.live_info()
    except Exception:
        info = []
    devs = list(getattr(emu, "BRP_DEVS", []))
    for i, dev in enumerate(devs):
        li = info[i] if i < len(info) else None
        out.append({"slot": i + 1, "kind": "brp-lease", "dev": dev,
                    "state": "streaming" if li else "idle",
                    "chan_id": (li or {}).get("chan_id"),
                    "client_ip": (li or {}).get("client_ip"),
                    "started": _ts((li or {}).get("t0")),
                    "bytes": (li or {}).get("nbytes")})
    return out, True


def snap_state(emu, now=None):
    now = time.time() if now is None else now
    api8 = getattr(emu, "_api8", {})
    ok = api8.get("ok_at", 0) > 0 and api8.get("ok_at", 0) >= api8.get("fail_at", 0)
    slots, have = _slots(emu)
    if getattr(emu, "HLS_TUNER", False):
        slots.append({"slot": len(slots) + 1, "kind": "m1-hls-fixed", "dev": None,
                      "state": "streaming" if getattr(emu, "_hls_live", [0])[0] > 0 else "idle",
                      "chan_id": "hls:", "client_ip": (getattr(emu, "_hls_info", {}) or {}).get("ip")
                      if getattr(emu, "_hls_live", [0])[0] > 0 else None,
                      "started": None, "bytes": None})
    t = _tuner(emu)
    brp = None
    if t is not None:
        try:
            brp = t.health()   # lock-only snapshot + local bind probes, no BRP
        except Exception as e:
            brp = {"error": repr(e)}
    epg = getattr(emu, "EPG", None)
    m1i = getattr(emu, "_m1_info", {})
    warnings = []
    if brp and brp.get("push_ip_warning"):
        warnings.append(brp["push_ip_warning"])
    if not have:
        warnings.append("tuner not initialised yet (no tune since start); slot "
                        "detail appears after the first stream")
    import zdiag
    if not zdiag.enabled():
        warnings.append("ZLOG_DIAG is off: the Logs page will stay empty")
    return {
        "version": VERSION, "mode": "readonly",
        "time": _ts(now), "uptime_sec": round(now - STARTED, 1),
        "m1": {"ip": getattr(emu, "M1", None), "spec": m1i.get("spec"),
               "dev": m1i.get("dev"), "source": m1i.get("source")},
        "health": {"ok": bool(ok and snap_lineup(emu)["count"]),
                   "api8_reachable": bool(ok),
                   "api8_last_ok_age_sec": (round(now - api8["ok_at"], 1)
                                            if api8.get("ok_at") else None),
                   "api8_source": api8.get("source"), "api8_error": api8.get("err"),
                   "epg_present": bool(epg is not None and epg.xml is not None)},
        "lineup_count": snap_lineup(emu)["count"],
        "tuner_count": getattr(emu, "TUNER_COUNT", None),
        "slots": slots,
        "brp": ({k: brp.get(k) for k in ("leases", "sessions_live", "sessions_closed",
                                         "qdrops_total", "qdrop_bursts_total",
                                         "tune_gate_busy", "tune_fail_counts",
                                         "last_push_age_sec", "last_fail")}
                if brp and "error" not in brp else brp),
        "warnings": warnings,
        "config": {"device_id": getattr(emu, "DEVICE_ID", None),
                   "http_port": getattr(emu, "HTTP_PORT", None),
                   "ts_port": getattr(emu, "TS_PORT", None),
                   "advertise_ip": getattr(emu, "ADVERTISE_IP", None) or None,
                   "friendly_name": getattr(emu, "FRIENDLY", None),
                   "zlog_diag": zdiag.enabled()},
    }


def _real_minis():
    """Reserved (never-a-push-IP) addresses: ONLY what the operator configured
    (push.reserved_ips, passed as ZUI_RESERVED_IPS). No built-in default."""
    raw = os.environ.get("ZUI_RESERVED_IPS", "")
    return [x.strip() for x in raw.replace(";", ",").split(",") if x.strip()]


def snap_devices(emu, now=None):
    t = _tuner(emu)
    push = {}
    if t is not None:
        try:
            for d in t.health().get("push_ips", []):
                push[d["dev"]] = d
        except Exception:
            pass
    brp = [{"dev": d, "push_ip": (push.get(d) or {}).get("push_ip"),
            "alias_present": (push.get(d) or {}).get("present")}
           for d in getattr(emu, "BRP_DEVS", [])]
    m1i = getattr(emu, "_m1_info", {})
    out = {"configured_m1s": [{"ip": getattr(emu, "M1", None), "spec": m1i.get("spec"),
                               "dev": m1i.get("dev"), "source": m1i.get("source"),
                               "hdhr_device_id": getattr(emu, "DEVICE_ID", None),
                               "brp_devs": brp,
                               "hls_tuner": bool(getattr(emu, "HLS_TUNER", False))}],
           "passive_discovery": None, "protected_minis": _real_minis()}
    reg = _registry[0]
    if reg is not None:
        try:
            snap = reg.snapshot()
            for r in snap:
                r["protected"] = (r["kind"] == "client")
            out["passive_discovery"] = snap
        except Exception:
            pass
    return out


def _local_ips():
    ips = set()
    try:
        import socket
        for fam, _t, _p, _c, sa in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(sa[0])
    except Exception:
        pass
    return ips


def alias_commands(ips, ifname="en0"):
    """Copy-paste commands only. The facade never executes these."""
    mac = ["sudo ifconfig %s alias %s 255.255.255.255" % (ifname, ip) for ip in ips]
    lin = ["sudo ip addr add %s/32 dev %s" % (ip, "eth0") for ip in ips]
    return {"macos": mac, "linux": lin,
            "note": "Replace the interface name with your LAN interface. Check the "
                    "address is unused first (e.g. arping -D). The bridge never runs these."}


def snap_aliases(emu):
    reserved = set(_real_minis())
    m1ip = getattr(emu, "M1", None)
    devs = snap_devices(emu)["configured_m1s"][0]["brp_devs"]
    rows, missing = [], []
    for d in devs:
        ip = d["push_ip"]
        if not ip:
            status = "unknown"
        elif ip in reserved:
            status = "reserved (real Mini): never use as a push IP"
        elif ip == m1ip:
            status = "is the M1's own address: not usable"
        elif d["alias_present"] is False:
            status = "missing on this host"
            missing.append(ip)
        elif d["alias_present"]:
            status = "ok (bindable on this host)"
        else:
            status = "unknown (tuner not initialised yet)"
        rows.append(dict(d, status=status))
    return {"rows": rows, "missing": missing, "reserved_ips": sorted(reserved),
            "commands": alias_commands(missing) if missing else alias_commands([]),
            "checked_by": "local bind probe in /health data; nothing is executed"}


def snap_logs(limit=200, since=None):
    """Ring lines (each has a ``seq``). With ``since`` only newer lines come
    back; ``cursor`` is the value to pass next time."""
    import zdiag
    return {"lines": zdiag.ring_lines(limit, since), "ring_on": zdiag._RING["on"],
            "cursor": zdiag.ring_seq()}


def snap_events(emu=None):
    """Very cheap change cursor for the live pages: three integers, no
    snapshots, no tuner/BRP/lineup access. The page refetches only when a
    cursor moved (or while a stream is live, whose byte counters move)."""
    import zdiag
    with _lk:
        n = len(_live)
    return {"seq": _seq[0], "log_seq": zdiag.ring_seq(), "live": n,
            "hls_live": getattr(emu, "_hls_live", [0])[0] if emu is not None else 0,
            "time": _ts(time.time())}


ROUTES = {
    "/api/ui/state": snap_state, "/api/ui/streams": snap_streams,
    "/api/ui/lineup": snap_lineup, "/api/ui/devices": snap_devices,
    "/api/ui/aliases": snap_aliases,
    "/api/ui/events": snap_events,
}


def lan_ok(ip):
    """RFC1918 / loopback / link-local only (IPv4-mapped accepted)."""
    try:
        a = ipaddress.ip_address(ip.split("%")[0])
        if getattr(a, "ipv4_mapped", None):
            a = a.ipv4_mapped
        return a.is_private or a.is_loopback or a.is_link_local
    except Exception:
        return False


def token():
    """The configured access token, or '' (auth off). Too-short values are
    ignored by ``token_problem`` and refuse all requests (fail closed)."""
    return os.environ.get("ZUI_TOKEN", "").strip()


def token_problem():
    t = token()
    return "ZUI_TOKEN must be at least 8 characters" if t and len(t) < 8 else None


def _cookie(h, name):
    for part in (h.headers.get("Cookie") or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v
    return ""


def token_ok(h):
    t = token()
    if not t:
        return True
    if len(t) < 8:
        return False                     # fail closed
    auth = h.headers.get("Authorization") or ""
    cand = auth[7:].strip() if auth[:7].lower() == "bearer " else _cookie(h, "zui_token")
    return bool(cand) and hmac.compare_digest(cand.encode(), t.encode())


def _send(h, code, body, ctype, head=False, extra=()):
    h.send_response(code)
    for k, v in extra:
        h.send_header(k, v)
    h.send_header("Content-Type", ctype)
    h.send_header("Content-Length", str(len(body)))
    h.send_header("Cache-Control", "no-store")
    h.send_header("X-Content-Type-Options", "nosniff")
    h.send_header("Content-Security-Policy", CSP)
    h.end_headers()
    if not head:
        h.wfile.write(body)


def _json(h, obj, code=200, head=False):
    _send(h, code, json.dumps(obj).encode(), "application/json", head)


def handle(h, path, emu, head=False):
    """Serve one /ui or /api/ui request. Caller checks enabled() first."""
    if not lan_ok(h.client_address[0]):
        return _json(h, {"error": "LAN only"}, 403, head)
    if token():
        # one-time login: /ui?token=... sets an HttpOnly cookie, then redirects
        q = h.path.split("?", 1)[1] if "?" in h.path else ""
        m = re.search(r"(?:^|&)token=([A-Za-z0-9_.~\-]{1,200})(?:&|$)", q)
        if (m and path in ("/ui", "/ui/") and len(token()) >= 8
                and hmac.compare_digest(m.group(1).encode(), token().encode())):
            return _send(h, 303, b"", "text/plain", head,
                         extra=(("Location", "/ui"),
                                ("Set-Cookie", "zui_token=%s; Path=/; HttpOnly; "
                                 "SameSite=Strict; Max-Age=2592000" % m.group(1))))
        if not token_ok(h):
            return _send(h, 401, json.dumps({"error": "token required"}).encode(),
                         "application/json", head,
                         extra=(("WWW-Authenticate", "Bearer"),))
    try:
        if path.startswith("/api/ui/"):
            if path == "/api/ui/logs":
                q = h.path.split("?", 1)[1] if "?" in h.path else ""
                m = re.search(r"(?:^|&)limit=(\d{1,4})(?:&|$)", q)
                ms = re.search(r"(?:^|&)since=(\d{1,12})(?:&|$)", q)
                return _json(h, snap_logs(int(m.group(1)) if m else 200,
                                          int(ms.group(1)) if ms else None), head=head)
            fn = ROUTES.get(path)
            if fn is None:
                return _json(h, {"error": "not found"}, 404, head)
            return _json(h, fn(emu), head=head)
        name = path[len("/ui"):].lstrip("/") or "index.html"
        if not _PATH_OK.match(name) or name.startswith("."):
            return _json(h, {"error": "not found"}, 404, head)
        fp = os.path.join(UI_DIR, name)
        ext = os.path.splitext(name)[1].lower()
        if ext not in _MIME or not os.path.isfile(fp):
            return _json(h, {"error": "not found"}, 404, head)
        with open(fp, "rb") as fh:
            return _send(h, 200, fh.read(), _MIME[ext], head)
    except (BrokenPipeError, ConnectionResetError):
        return None
    except Exception as e:
        return _json(h, {"error": "ui error: %s" % type(e).__name__}, 500, head)
