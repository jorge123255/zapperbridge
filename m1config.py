"""Multi-M1 / multi-Mini config model (validation + per-M1 launch env).

One *virtual HDHomeRun* per M1. Each entry owns:
  * its M1 (IP, or "auto:<m1 dev>" resolved from the BRzb announce),
  * up to 3 BRP device ids (the M1 grants exactly 3 leases: instances 3/4/5),
    each with the local push IP the M1 sends that id's TS to (:31061),
  * its own HDHR DeviceID, advertise IP and JSON/TS ports,
  * whether the M1's fixed HLS window is offered as an extra (non-retunable)
    tuner, and an optional XMLTV id prefix.

Nothing here talks to an M1. ``validate`` is pure; ``env_for`` renders the
environment the existing single-M1 facade already understands, so one
process per M1 can be launched from one file. See
docs/MULTI-M1-PLAN.md for the design and what still needs live proof.

Example (a single-M1 setup):

    {"m1s": [{"name": "living-room", "m1": "192.0.2.10",
              "m1_dev": "02bb00a00001", "device_id": "02BB0001",
              "advertise_ip": "192.0.2.30", "http_port": 80, "ts_port": 5004,
              "brp_devs": [{"dev": "02bb00c00001", "push_ip": "192.0.2.30"},
                           {"dev": "02bb00000002", "push_ip": "192.0.2.242"},
                           {"dev": "02bb00000003", "push_ip": "192.0.2.243"}],
              "hls_tuner": true, "tuner_count": 4}]}
"""
import json
import re
import socket

LEASES_PER_M1 = 3
_DEV = re.compile(r"^[0-9a-f]{12}$")
_HDHR_ID = re.compile(r"^[0-9A-F]{8}$")
# Addresses of real Zapper Minis must never be push IPs (they would steal the
# M1's TS pushes). Empty by default; list yours in cfg["known_real_minis"].
DEFAULT_REAL_MINIS = ()


def _ip_ok(ip):
    try:
        socket.inet_aton(ip)
        return ip.count(".") == 3
    except (OSError, TypeError):
        return False


def load(path):
    with open(path) as fh:
        return json.load(fh)


def normalise(cfg):
    out = []
    for i, m in enumerate(cfg.get("m1s") or []):
        m = dict(m)
        m.setdefault("name", "m1-%d" % (i + 1))
        m.setdefault("http_port", 80)
        m.setdefault("ts_port", 5004)
        m.setdefault("hls_tuner", True)
        m.setdefault("xmltv_prefix", "")
        devs = []
        for d in m.get("brp_devs") or []:
            if isinstance(d, str):
                d = {"dev": d}
            devs.append({"dev": str(d.get("dev", "")).strip().lower(),
                         "push_ip": d.get("push_ip")})
        m["brp_devs"] = devs
        m.setdefault("tuner_count", len(devs[:LEASES_PER_M1])
                     + (1 if m["hls_tuner"] else 0))
        out.append(m)
    return out


def validate(cfg, client_ips=(), m1_ips=()):
    """(errors, warnings). client_ips: IPs of real Minis/apps seen on the LAN
    (from m1disco), never usable as push IPs. m1_ips: M1 addresses.
    Always rejected as push IPs: cfg["known_real_minis"] (default: none) and every configured M1 IP."""
    errs, warns = [], []
    m1s = normalise(cfg)
    real = cfg.get("known_real_minis")
    client_ips = set(client_ips) | set(
        DEFAULT_REAL_MINIS if real is None else real)
    m1_ips = set(m1_ips) | {str(m.get("m1", "")).strip() for m in m1s
                            if _ip_ok(str(m.get("m1", "")).strip())}
    seen_m1 = {}
    if not m1s:
        errs.append("no m1s configured")
    seen_dev, seen_push, seen_ids, seen_bind = {}, {}, {}, {}
    names = set()
    for m in m1s:
        n = m["name"]
        if n in names:
            errs.append("%s: duplicate name" % n)
        names.add(n)
        spec = str(m.get("m1", "")).strip()
        if not spec:
            errs.append("%s: m1 missing (IP or auto:<dev>)" % n)
        elif not spec.lower().startswith("auto") and not _ip_ok(spec):
            errs.append("%s: m1 %r is not an IP or auto spec" % (n, spec))
        elif not spec.lower().startswith("auto"):
            if spec in seen_m1:
                errs.append("%s: m1 %s already used by %s (use one entry "
                            "with up to %d device ids)" % (
                                n, spec, seen_m1[spec], LEASES_PER_M1))
            seen_m1[spec] = n
        did = str(m.get("device_id", "")).upper()
        if not _HDHR_ID.match(did):
            errs.append("%s: device_id %r must be 8 hex digits" % (n, did))
        elif did in seen_ids:
            errs.append("%s: device_id %s already used by %s" % (
                n, did, seen_ids[did]))
        seen_ids[did] = n
        adv = m.get("advertise_ip")
        if adv is not None and not _ip_ok(adv):
            errs.append("%s: advertise_ip %r invalid" % (n, adv))
        for port_key in ("http_port", "ts_port"):
            try:
                port = int(m[port_key])
                if not 1 <= port <= 65535:
                    raise ValueError(port)
            except (TypeError, ValueError):
                errs.append("%s: %s %r is not a valid port" % (
                    n, port_key, m[port_key]))
                continue
            bind = adv or "*"
            # a wildcard bind clashes with any specific bind on the port
            clash = [(b, o) for (b, p), o in seen_bind.items()
                     if p == port and (b == bind or b == "*" or bind == "*")]
            if clash:
                errs.append("%s: %s %s:%d already used by %s" % (
                    n, port_key, bind, port, clash[0][1]))
            seen_bind[(bind, port)] = n
        devs = m["brp_devs"]
        if not devs:
            errs.append("%s: no brp_devs (needs 1-%d registered ids)" % (
                n, LEASES_PER_M1))
        if len(devs) > LEASES_PER_M1:
            warns.append("%s: %d brp_devs but the M1 grants only %d leases; "
                         "extras never stream" % (n, len(devs), LEASES_PER_M1))
        for d in devs:
            dev, ip = d["dev"], d["push_ip"]
            if not _DEV.match(dev):
                errs.append("%s: dev %r must be 12 lowercase hex" % (n, dev))
            if dev in seen_dev:
                errs.append("%s: dev %s already used by %s" % (
                    n, dev, seen_dev[dev]))
            seen_dev[dev] = n
            if ip is None:
                warns.append("%s: dev %s has no push_ip (learned at first "
                             "tune; concurrency needs one IP per dev)" % (n, dev))
                continue
            if not _ip_ok(ip):
                errs.append("%s: dev %s push_ip %r invalid" % (n, dev, ip))
                continue
            if ip in client_ips:
                errs.append("%s: dev %s push_ip %s belongs to a real Mini/"
                            "client on the LAN" % (n, dev, ip))
            if ip in m1_ips:
                errs.append("%s: dev %s push_ip %s is an M1's address" % (
                    n, dev, ip))
            if ip in seen_push:
                errs.append("%s: push_ip %s shared by %s and %s (each dev "
                            "needs its own: the local IP is the routing key)"
                            % (n, ip, seen_push[ip], dev))
            seen_push[ip] = dev
        real = min(len(devs), LEASES_PER_M1) + (1 if m["hls_tuner"] else 0)
        if int(m["tuner_count"]) > real:
            warns.append("%s: tuner_count %d > %d real tuners (%d BRP leases%s)"
                         % (n, m["tuner_count"], real,
                            min(len(devs), LEASES_PER_M1),
                            " + 1 fixed HLS" if m["hls_tuner"] else ""))
    if len(m1s) > 1:
        warns.append("%d M1s: each facade process must bind :31061 on its own "
                     "push IPs only (brptune binds 0.0.0.0 today; per-IP bind "
                     "is phase 2 of the plan)" % len(m1s))
    return errs, warns


def tuner_map(m):
    """Advertised HDHR tuner index -> backing resource for one M1 entry.
    Rows 0..k-1 are the BRP leases in brp_devs order (= brptune slot order,
    = /tuners.json rows); the last row is the M1's own fixed HLS window."""
    m = normalise({"m1s": [m]})[0]
    out = []
    for i, d in enumerate(m["brp_devs"][:LEASES_PER_M1]):
        out.append({"tuner": i, "kind": "brp-lease", "dev": d["dev"],
                    "push_ip": d["push_ip"]})
    if m["hls_tuner"]:
        out.append({"tuner": len(out), "kind": "m1-hls-fixed",
                    "retunable": False})
    return out


def env_for(m):
    """Environment for one facade process serving this M1 entry."""
    m = normalise({"m1s": [m]})[0]
    devs = [d["dev"] for d in m["brp_devs"]]
    env = {
        "ZM1": str(m["m1"]),
        "HDHR_BRP_DEVS": ",".join(devs),
        "ZDEVS": ",".join(devs),
        "HDHR_DEVICE_ID": str(m["device_id"]).upper(),
        "HDHR_JSON_PORT": str(m["http_port"]),
        "HDHR_TS_PORT": str(m["ts_port"]),
        "HDHR_TUNERS": str(m["tuner_count"]),
        "HDHR_HLS_TUNER": "1" if m["hls_tuner"] else "0",
    }
    pis = ",".join("%s=%s" % (d["dev"], d["push_ip"])
                   for d in m["brp_devs"] if d["push_ip"])
    if pis:
        env["ZPUSH_IPS"] = pis
    if m.get("advertise_ip"):
        env["HDHR_ADVERTISE_IP"] = m["advertise_ip"]
    if m.get("xmltv_prefix"):
        env["ZEPG_ID_PREFIX"] = m["xmltv_prefix"]
    if m.get("friendly_name"):
        env["HDHR_FRIENDLY"] = m["friendly_name"]
    return env


if __name__ == "__main__":
    import sys
    cfg = load(sys.argv[1])
    # validate() itself rejects the real Mini (.237 default) and any
    # configured M1's own address as a push_ip.
    e, w = validate(cfg)
    for x in e:
        print("ERROR", x)
    for x in w:
        print("WARN ", x)
    for m in normalise(cfg):
        print("# %s" % m["name"])
        print(" ".join("%s=%s" % kv for kv in sorted(env_for(m).items())))
    raise SystemExit(1 if e else 0)
