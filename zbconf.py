"""Docker/compose-friendly config for the zapperbridge (additive).

Reads one commented TOML file (``zapperbridge.toml``, see zapperbridge.example.toml)
plus a few ``ZB_*`` environment overrides, expands it into the per-M1 model
that ``m1config`` already validates (one virtual HDHomeRun per M1, up to
``m1config.LEASES_PER_M1`` BRP device ids each, one push IP per id), and
renders the launch environment the existing facade understands.

Nothing here talks to an M1, binds a port or runs a command. Importing it
changes no runtime behaviour; only ``docker/entrypoint.py`` uses it.

Proven facts this encodes :
  * the M1 pushes TS to the IP written in the UDP 31064 ANNOUNCE ip string,
    on TCP :31061, and accepts an IP that is not local to the sender;
  * the join (api6) carries no push field, so each virtual tuner needs its
    own device id AND its own push IP;
  * the real Mini is never a push IP (list real devices in push.reserved_ips;
    nothing is hard-coded in this layer) and api7 is never used.
"""
import hashlib
import ipaddress
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys

try:  # Python 3.11+
    import tomllib
except ImportError:  # pragma: no cover - image uses 3.12
    tomllib = None

import m1config

NETWORK_MODES = ("host", "macvlan", "ipvlan")
HDHR_MODES = ("per-m1", "merged")
UI_MODES = ("off", "readonly", "admin")
#: No addresses are hard-coded here. Users list real devices (e.g. a real
#: Zapper Mini) in ``push.reserved_ips``; those are rejected as push IPs, as is
#: every configured M1 address (m1config.validate); the list is passed to m1config as
#: ``known_real_minis``.
NEVER_DEVS = ()  # device ids never used by the bridge; add real Zapper Mini ids via config
DEFAULT_DEV_PREFIX = "02bb00"   # locally-administered style, not a vendor OUI
_HEX = re.compile(r"^[0-9a-f]+$")
_HDHR_ID = re.compile(r"^[0-9A-Fa-f]{8}$")


class ConfigError(Exception):
    pass


def load(path):
    if tomllib is None:
        raise ConfigError("Python 3.11+ required for TOML config")
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def _as_list(v):
    if v is None or v == "":
        return []
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    return list(v)


def _truthy(v):
    """Config/env boolean: false for 0/false/no/off (any case) or False."""
    if isinstance(v, str):
        return v.strip().lower() not in ("0", "false", "no", "off")
    return bool(v)


def apply_env(cfg, env=None):
    """Overlay the small set of ZB_* overrides (compose-friendly)."""
    env = os.environ if env is None else env
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in cfg.items()}
    for sect in ("network", "push", "hdhr", "ui", "reqlog", "discovery"):
        cfg.setdefault(sect, {})
    if env.get("ZB_M1"):  # comma list of M1 addresses / auto / auto:<dev>
        cfg["m1"] = [{"address": a} for a in _as_list(env["ZB_M1"])]
    if env.get("ZB_RESERVED_IPS"):
        cfg["push"]["reserved_ips"] = _as_list(env["ZB_RESERVED_IPS"])
    if env.get("ZB_CHECK_FREE") not in (None, ""):
        cfg["push"]["check_free"] = (
            env["ZB_CHECK_FREE"].strip().lower() not in
            ("0", "false", "no", "off"))
    if env.get("ZB_PUSH_POOL"):
        cfg["push"]["pool"] = _as_list(env["ZB_PUSH_POOL"])
    if env.get("ZB_NETWORK_MODE"):
        cfg["network"]["mode"] = env["ZB_NETWORK_MODE"]
    if env.get("ZB_ADVERTISE_IP"):
        cfg["network"]["advertise_ip"] = env["ZB_ADVERTISE_IP"]
    if env.get("ZB_DEVICE_SEED"):
        cfg["push"]["device_seed"] = env["ZB_DEVICE_SEED"]
    if env.get("ZB_TUNER_CAP"):
        cfg["hdhr"]["tuner_cap"] = int(env["ZB_TUNER_CAP"])
    if env.get("ZB_SSDP") not in (None, ""):
        cfg["network"]["ssdp"] = env["ZB_SSDP"]
    if env.get("ZB_UI_MODE"):
        cfg["ui"]["mode"] = env["ZB_UI_MODE"]
    return cfg


#: Legacy fixed seed. Only used when no seed is configured AND none can be
#: persisted (data dir not writable); every such install derives the same ids.
LEGACY_SEED = "zapperbridge"
SEED_FILE = "seed"


def data_dir(env=None):
    env = os.environ if env is None else env
    return env.get("ZB_DATA_DIR") or "/data"


def _read_seed(path):
    """Seed text from `path`, or None if absent/empty/unreadable-as-text.
    Any non-empty printable text is accepted verbatim (so a hand-written or
    older seed keeps its ids); only empty / binary garbage counts as corrupt."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(1024)
    except FileNotFoundError:
        return None, "missing"
    except OSError as e:
        return None, "unreadable: %s" % e
    try:
        txt = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None, "corrupt (not text)"
    if not txt or len(txt) > 256 or not txt.isprintable():
        return None, "corrupt (empty or not printable)"
    return txt, "ok"


def _write_seed_atomic(path, seed):
    """Create `path` with 0600, atomically, and never replace an existing
    file: write a temp file then hard-link it into place (link fails if the
    target exists, so a concurrent first run cannot clobber a seed)."""
    d = os.path.dirname(path)
    tmp = os.path.join(d, ".seed.%d.%s.tmp" % (os.getpid(),
                                               secrets.token_hex(4)))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(seed + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            raise
        except OSError:  # filesystem without hard links
            if os.path.exists(path):
                raise FileExistsError(path)
            os.replace(tmp, path)
            tmp = None
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def resolve_seed(cfg, env=None, create=True):
    """Pick the device-id seed. Returns (cfg, source_text).

    1. an explicit push.device_seed / ZB_DEVICE_SEED always wins;
    2. else <data dir>/seed (ZB_DATA_DIR, default /data), created once with a
       random value on first run and never regenerated while present;
    3. else (create=False and no file) a placeholder, or the legacy fixed
       seed if the data dir cannot be written.
    The seed itself is never put in the returned text."""
    env = os.environ if env is None else env
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in cfg.items()}
    push = cfg.setdefault("push", {})
    if str(push.get("device_seed") or "").strip():
        return cfg, "explicit device_seed (config / ZB_DEVICE_SEED)"
    path = os.path.join(data_dir(env), SEED_FILE)
    seed, why = _read_seed(path)
    if seed is not None:
        push["device_seed"] = seed
        return cfg, "persisted seed file %s" % path
    if not create:
        push["device_seed"] = LEGACY_SEED
        return cfg, ("PLACEHOLDER: no seed file at %s yet; a random one is "
                     "created at first run, so these ids are NOT final" % path)
    note = ""
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        if why.startswith(("corrupt", "unreadable")):
            bad = path + ".bad"
            try:
                os.replace(path, bad)
                note = " (previous file was %s; kept as %s, ids changed)" % (
                    why, bad)
            except OSError:
                pass
        new = secrets.token_hex(16)
        try:
            _write_seed_atomic(path, new)
            push["device_seed"] = new
            return cfg, "NEW random seed written to %s%s; keep this file" \
                " (deleting it changes every device id)" % (path, note)
        except FileExistsError:  # lost a race: use the winner's
            seed, _ = _read_seed(path)
            if seed is not None:
                push["device_seed"] = seed
                return cfg, "persisted seed file %s" % path
            raise OSError("seed file appeared but is unreadable")
    except OSError as e:
        push["device_seed"] = LEGACY_SEED
        return cfg, ("FALLBACK legacy fixed seed: cannot persist a seed in %s "
                     "(%s). Mount a writable /data (or set ZB_DATA_DIR / "
                     "device_seed); installs sharing this fallback derive the "
                     "SAME device ids" % (os.path.dirname(path), e))


ACTIVE_FILE = "active-ids.json"


def write_active(plan, env=None):
    """Record the device ids + push IPs the facade is started with in
    <data dir>/active-ids.json, so `discover --apply` can refuse to announce
    ids the running bridge does not use. No seed, no secrets. Best effort:
    returns (path or None, error or None)."""
    import json
    import time
    path = os.path.join(data_dir(env), ACTIVE_FILE)
    rec = {"written_at": int(time.time()), "seed_fp": plan.get("seed_fp"),
           "m1s": [{"name": m["name"], "m1": m["m1"],
                    "devs": [d["dev"] for d in m["brp_devs"]],
                    "push_ips": [d["push_ip"] for d in m["brp_devs"]]}
                   for m in plan["m1s"]]}
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "w") as fh:
            json.dump(rec, fh, indent=1)
        os.replace(tmp, path)
        return path, None
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return None, str(e)


def read_active(env=None):
    """(record or None, why) from write_active()."""
    import json
    path = os.path.join(data_dir(env), ACTIVE_FILE)
    try:
        with open(path) as fh:
            d = json.load(fh)
    except FileNotFoundError:
        return None, "%s not found" % path
    except (OSError, ValueError) as e:
        return None, "%s unreadable: %s" % (path, e)
    if not isinstance(d, dict) or not isinstance(d.get("m1s"), list):
        return None, "%s malformed" % path
    return d, "ok"


def parse_pool(entries):
    """Expand ["a.b.c.240-249", "a.b.c.250-a.b.c.252", "10.0.0.8",
    "192.0.2.0/29"] into an ordered, de-duplicated list of IPs.
    Raises ConfigError on garbage. Does not apply the deny list."""
    out, seen = [], set()

    def add(ip):
        if ip not in seen:
            seen.add(ip)
            out.append(ip)
    for ent in _as_list(entries):
        try:
            if "/" in ent:
                for h in ipaddress.ip_network(ent, strict=False).hosts():
                    add(str(h))
            elif "-" in ent:
                a, b = [p.strip() for p in ent.split("-", 1)]
                lo = ipaddress.IPv4Address(a)
                hi = (ipaddress.IPv4Address(b) if "." in b else
                      ipaddress.IPv4Address(
                          a.rsplit(".", 1)[0] + "." + str(int(b))))
                if hi < lo:
                    raise ValueError("range end before start")
                if int(hi) - int(lo) > 1023:
                    raise ValueError("range too large")
                for n in range(int(lo), int(hi) + 1):
                    add(str(ipaddress.IPv4Address(n)))
            else:
                add(str(ipaddress.IPv4Address(ent)))
        except ValueError as e:
            raise ConfigError("push pool entry %r: %s" % (ent, e))
    return out


def derive_dev(prefix, seed, key, slot):
    """Stable 12-hex device id: <prefix><sha1(seed|key|slot)[...]>."""
    prefix = (prefix or DEFAULT_DEV_PREFIX).lower()
    if not _HEX.match(prefix) or not 2 <= len(prefix) <= 8 or len(prefix) % 2:
        raise ConfigError("device_prefix %r must be 2-8 hex digits (even)"
                          % prefix)
    h = hashlib.sha1(("%s|%s|%d" % (seed, key, slot)).encode()).hexdigest()
    return prefix + h[: 12 - len(prefix)]


def seed_fingerprint(seed):
    """Short, non-reversible tag of the device-id seed (8 hex). Printed by
    check/run/discover so two containers can be compared without showing
    the seed itself."""
    return hashlib.sha1(("seed-fp|%s" % seed).encode()).hexdigest()[:8]


def derive_hdhr_id(seed, key):
    return hashlib.sha1(("hdhr|%s|%s" % (seed, key)).encode()
                        ).hexdigest()[:8].upper()


def expand(cfg):
    """TOML-ish dict -> (m1config cfg, plan dict, errors, warnings)."""
    errs, warns = [], []
    net, push = cfg.get("network") or {}, cfg.get("push") or {}
    hd, ui = cfg.get("hdhr") or {}, cfg.get("ui") or {}
    rl = cfg.get("reqlog") or {}

    mode = net.get("mode", "host")
    if mode not in NETWORK_MODES:
        errs.append("network.mode %r not in %s" % (mode, list(NETWORK_MODES)))
    hmode = hd.get("mode", "per-m1")
    if hmode not in HDHR_MODES:
        errs.append("hdhr.mode %r not in %s" % (hmode, list(HDHR_MODES)))
    uimode = ui.get("mode", "off")
    if uimode not in UI_MODES:
        errs.append("ui.mode %r not in %s" % (uimode, list(UI_MODES)))
    elif uimode == "admin":
        tok = ui.get("admin_token") or os.environ.get("ZUI_ADMIN_TOKEN", "")
        if len(tok) < 16:
            errs.append("ui.mode = \"admin\" needs a token of >= 16 chars "
                        "(env ZUI_ADMIN_TOKEN; never put it in the file)")
    ztok = os.environ.get("ZUI_TOKEN", "").strip()
    if ztok and len(ztok) < 8:
        errs.append("ZUI_TOKEN must be at least 8 characters")
    elif uimode == "readonly" and not ztok:
        warns.append("ui.mode = \"readonly\" without ZUI_TOKEN: the UI is "
                     "open to every private-LAN address that can reach :80 "
                     "(set env ZUI_TOKEN to require a token; never expose it "
                     "to the internet)")
    if ui.get("admin_token"):
        warns.append("ui.admin_token in the file is discouraged; use env "
                     "ZUI_ADMIN_TOKEN")

    adv_default = net.get("advertise_ip")
    if mode in ("macvlan", "ipvlan") and not adv_default:
        errs.append("network.mode %s needs network.advertise_ip (the "
                    "container's own LAN IP)" % mode)
    if mode == "host" and not adv_default:
        warns.append("network.advertise_ip unset: the facade will pick the "
                     "route-selected primary LAN IP")

    try:  # reserved_ips accepts the same forms as the pool; never_ips is the
        # older name for the same list
        never_ips = set(parse_pool(_as_list(push.get("reserved_ips")) +
                                   _as_list(push.get("never_ips"))))
    except ConfigError as e:
        errs.append("push.reserved_ips: %s" % e)
        never_ips = set()
    never_devs = set(NEVER_DEVS) | {d.lower() for d in
                                    _as_list(push.get("never_devs"))}
    try:
        pool = parse_pool(push.get("pool"))
    except ConfigError as e:
        errs.append(str(e))
        pool = []
    hit = [ip for ip in pool if ip in never_ips]
    if hit:
        errs.append("push.pool contains reserved/real-device IP(s) %s"
                    % ", ".join(hit))
        pool = [ip for ip in pool if ip not in never_ips]

    seed = str(push.get("device_seed") or LEGACY_SEED)
    prefix = push.get("device_prefix", DEFAULT_DEV_PREFIX)
    try:
        derive_dev(prefix, seed, "x", 0)
    except ConfigError as e:
        errs.append(str(e))
        prefix = DEFAULT_DEV_PREFIX

    if not pool and not any(_as_list(e.get("push_ips"))
                            for e in cfg.get("m1") or []):
        warns.append("push.pool is empty: push IPs are learned at first tune "
                     "and only one stream can run at a time. Set push.pool "
                     "for concurrent tuners.")
    entries = cfg.get("m1") or []
    if not entries:
        entries = [{"address": "auto"}]
        warns.append("no [[m1]] entries: using one auto-discovered M1 "
                     "(several M1s: list them, or use auto:<m1 dev id>)")
    cap = hd.get("tuner_cap")
    if cap is not None and (not isinstance(cap, int) or cap < 1):
        errs.append("hdhr.tuner_cap must be a positive integer")
        cap = None

    explicit_ips = set()
    for e in entries:
        for ip in _as_list(e.get("push_ips")):
            explicit_ips.add(ip)
    free = [ip for ip in pool if ip not in explicit_ips]

    m1s, remaining = [], cap
    for i, e in enumerate(entries):
        addr = str(e.get("address", "")).strip()
        name = e.get("name") or "m1-%d" % (i + 1)
        key = addr if addr and not addr.lower().startswith("auto") else name
        leases = int(e.get("leases", m1config.LEASES_PER_M1))
        if not 1 <= leases <= m1config.LEASES_PER_M1:
            errs.append("%s: leases %d outside 1-%d" % (
                name, leases, m1config.LEASES_PER_M1))
            leases = max(1, min(leases, m1config.LEASES_PER_M1))
        devs = [d.lower() for d in _as_list(e.get("devs"))]
        if devs and len(devs) != leases:
            errs.append("%s: devs lists %d ids but leases = %d" % (
                name, len(devs), leases))
        pips = _as_list(e.get("push_ips"))
        if pips and len(pips) != leases:
            errs.append("%s: push_ips lists %d IPs but leases = %d" % (
                name, len(pips), leases))
        brp = []
        for s in range(leases):
            dev = devs[s] if s < len(devs) else derive_dev(prefix, seed,
                                                           key, s)
            if dev in never_devs:
                errs.append("%s: device id %s is a real device's id" % (
                    name, dev))
            if s < len(pips):
                ip = pips[s]
            elif free:
                ip = free.pop(0)
            elif not pool and not explicit_ips:
                ip = None  # no pool at all: legacy learn-at-first-tune
            else:
                ip = None
                errs.append("%s: push IP pool exhausted at slot %d (need one "
                            "IP per tuner; add addresses to push.pool)" % (
                                name, s))
            if ip in never_ips:
                errs.append("%s: push ip %s is reserved/real-device" % (
                    name, ip))
            brp.append({"dev": dev, "push_ip": ip})
        hls = bool(e.get("hls_tuner", hd.get("hls_tuner", True)))
        real = leases + (1 if hls else 0)
        tc = real
        if remaining is not None:
            tc = max(0, min(real, remaining))
            remaining -= tc
            if tc == 0:
                warns.append("%s: tuner_cap reached; M1 advertises 0 tuners"
                             % name)
        m = {"name": name, "m1": addr, "brp_devs": brp, "hls_tuner": hls,
             "device_id": (e.get("device_id") or derive_hdhr_id(seed, key)
                           ).upper(),
             "tuner_count": tc}
        adv = e.get("advertise_ip") or adv_default
        if adv:
            m["advertise_ip"] = adv
        if i == 0:
            m["http_port"] = int(e.get("http_port",
                                       net.get("http_port", 80)))
            m["ts_port"] = int(e.get("ts_port", net.get("ts_port", 5004)))
        else:
            m["http_port"] = int(e.get("http_port",
                                       net.get("http_port", 80)) +
                                 (0 if e.get("advertise_ip") else i))
            m["ts_port"] = int(e.get("ts_port", net.get("ts_port", 5004))
                               + (0 if e.get("advertise_ip") else i))
            if not e.get("advertise_ip") and "http_port" not in e:
                warns.append("%s: no own advertise_ip, so ports shifted to "
                             "%d/%d; HDHR clients may only find :80" % (
                                 name, m["http_port"], m["ts_port"]))
        fn = e.get("friendly_name") or hd.get("friendly_name")
        if fn:
            m["friendly_name"] = fn
        if e.get("xmltv_prefix"):
            m["xmltv_prefix"] = e["xmltv_prefix"]
        m1s.append(m)

    # Always hand m1config the user's list (possibly empty) so its built-in
    # default is not what drives the Docker layer.
    core = {"m1s": m1s, "known_real_minis": sorted(
        set(_as_list(push.get("known_real_minis"))) | never_ips)}
    m1_ips = [m["m1"] for m in m1s if m1config._ip_ok(m["m1"])]
    e2, w2 = m1config.validate(core, m1_ips=m1_ips)
    errs += [x for x in e2 if x not in errs]
    warns += w2
    for ip in sorted(set(never_ips)):
        for m in m1s:
            if any(d["push_ip"] == ip for d in m["brp_devs"]):
                msg = "%s: push ip %s is reserved/real-device" % (
                    m["name"], ip)
                if msg not in errs and not any(ip in x for x in errs):
                    errs.append(msg)

    merged = None
    if hmode == "merged":
        # Plan-only: validated and printed by check/plan, but the facade is not
        # wired for it (docs/MERGED-LINEUP.md), so `run` refuses.
        usable = {m["name"]: min(len([d for d in m["brp_devs"]]),
                                 m1config.LEASES_PER_M1) for m in m1s}
        tot = sum(usable.values())
        merged = {"enabled": True, "usable": usable,
                  "tuner_count": min(tot, cap) if cap else tot,
                  "device_id": (hd.get("device_id") or derive_hdhr_id(
                      seed, "merged")).upper(),
                  "advertise_ip": adv_default,
                  "friendly_name": hd.get("friendly_name")}
        if not _HDHR_ID.match(merged["device_id"]):
            errs.append("hdhr.device_id must be 8 hex digits")
        if len(m1s) < 2:
            warns.append("hdhr.mode = \"merged\" with one M1 is the same as "
                         "per-m1")
        warns.append("hdhr.mode = \"merged\" is plan-only: lineupmerge.py is "
                     "tested but not wired into the facade, so `run` refuses "
                     "to start (see docs/MERGED-LINEUP.md)")

    extras = {
        "UI_MODE": uimode,
        "SSDP": _truthy(net.get("ssdp", True)),
        "DISCOVERY_RESPONDER": bool(hd.get("discovery_responder", False)),
        "CHECKSUM_DEVICE_ID": bool(hd.get("checksum_device_id", False)),
        "REQLOG": bool(rl.get("enabled", False)),
        "REQLOG_DAYS": int(rl.get("retention_days", 7)),
        "REQLOG_MB": int(rl.get("max_mb", 50)),
    }
    for k, v in (("hdhr.discovery_responder", extras["DISCOVERY_RESPONDER"]),
                 ("hdhr.checksum_device_id", extras["CHECKSUM_DEVICE_ID"]),
                 ("ui.mode", uimode == "admin")):
        if v:
            warns.append("%s is accepted but reserved: the facade ignores it "
                         "until that feature ships" % k)
    cf = push.get("check_free", True)
    if not isinstance(cf, bool):
        errs.append("push.check_free must be true or false")
        cf = True
    cff = push.get("check_free_fail", True)
    if not isinstance(cff, bool):
        errs.append("push.check_free_fail must be true or false")
        cff = True
    plan = {"mode": mode, "pool": pool, "m1s": m1s,
            "seed_fp": seed_fingerprint(seed),
            "reserved_ips": sorted(never_ips),
            "check_free": {"enabled": cf, "fail": cff},
            "tuners_total": (merged["tuner_count"] if merged else
                             sum(m["tuner_count"] for m in m1s)),
            "merged": merged,
            "extras": extras}
    return core, plan, errs, warns


def launch_envs(plan):
    """[(name, env dict)] one facade process per M1, via m1config.env_for."""
    out = []
    for m in plan["m1s"]:
        env = m1config.env_for(m)
        x = plan["extras"]
        env["ZUI_MODE"] = x["UI_MODE"]
        if x["UI_MODE"] == "readonly":   # webui.py reads ZUI (read-only, no write routes)
            env["ZUI"] = "readonly"
            env["ZUI_RESERVED_IPS"] = ",".join(plan.get("reserved_ips") or ())
        if x["REQLOG"]:
            env["ZREQLOG"] = os.environ.get("ZB_REQLOG_PATH", "/data/zapperbridge-req.jsonl")
            env["ZREQLOG_DAYS"] = str(x["REQLOG_DAYS"])
            env["ZREQLOG_MB"] = str(x["REQLOG_MB"])
        if x["DISCOVERY_RESPONDER"]:
            env["HDHR_DISCOVERY_RESPONDER"] = "1"
        if not x["SSDP"]:                # default (on) leaves the facade's own default
            env["HDHR_SSDP"] = "0"
        out.append((m["name"], env))
    return out


def _is_local(ip):
    """True if this host already owns ``ip`` (bind succeeds). Sends nothing."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((ip, 0))
        return True
    except OSError:
        return False
    finally:
        s.close()


def probe_ip(ip, run=subprocess.run, which=shutil.which, is_local=_is_local,
             platform=None, timeout=2):
    """Passive-ish liveness probe of a CANDIDATE push IP.

    Returns (state, detail) with state in:
      "local"   this host/container already owns the address (fine, skipped)
      "in-use"  something answered ping or has an ARP/neighbour entry
      "free"    no answer
      "unknown" no usable tool (never treated as in-use)
    One ICMP echo and a neighbour-table read only. Nothing is sent to an M1,
    nothing is spoofed, no address is configured. Never call it with an M1 or
    a reserved address (``check_free`` filters those out).
    """
    if is_local(ip):
        return "local", "already assigned to this host"
    platform = platform or sys.platform
    used_tool = False
    ping = which("ping")
    if ping:
        used_tool = True
        wait = "1000" if platform == "darwin" else "1"
        try:
            r = run([ping, "-c", "1", "-W", wait, ip], capture_output=True,
                    timeout=timeout + 2)
            if r.returncode == 0:
                return "in-use", "answered ping"
        except (OSError, subprocess.SubprocessError):
            used_tool = False
    for cmd in (["ip", "neigh", "show", ip], ["arp", "-n", ip]):
        tool = which(cmd[0])
        if not tool:
            continue
        used_tool = True
        try:
            r = run([tool] + cmd[1:], capture_output=True,
                    timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            continue
        out = (r.stdout or b"").decode("utf-8", "replace").lower()
        bad = ("failed", "incomplete", "no entry", "not found")
        if r.returncode == 0 and not any(b in out for b in bad) and (
                "lladdr" in out or re.search(r"(?:[0-9a-f]{1,2}:){5}"
                                             r"[0-9a-f]{1,2}", out)):
            return "in-use", "has an ARP/neighbour entry"
    return ("free", "no answer") if used_tool else (
        "unknown", "no ping/ip/arp tool available")


def check_free(plan, probe=probe_ip):
    """Probe every planned push IP. Returns (in_use, notes) where in_use is a
    list of "ip (detail)" strings and notes are informational lines.
    Skips reserved IPs and M1 addresses (never probed)."""
    skip = set(plan.get("reserved_ips") or ()) | {
        m["m1"] for m in plan["m1s"]}
    in_use, notes, seen = [], [], set()
    for m in plan["m1s"]:
        for d in m["brp_devs"]:
            ip = d["push_ip"]
            if not ip or ip in skip or ip in seen:
                continue
            seen.add(ip)
            state, detail = probe(ip)
            if state == "in-use":
                in_use.append("%s (%s)" % (ip, detail))
            elif state == "local":
                notes.append("push ip %s: %s" % (ip, detail))
            elif state == "unknown":
                notes.append("push ip %s: could not check (%s)" % (ip, detail))
    return in_use, notes


def alias_hints(plan, iface="eth0"):
    """Copy-paste ``ip addr`` lines for host mode. Printed, never executed."""
    ips = sorted({d["push_ip"] for m in plan["m1s"] for d in m["brp_devs"]
                  if d["push_ip"]})
    return ["sudo ip addr add %s/32 dev %s   # arping -D -I %s -c 2 %s first"
            % (ip, iface, iface, ip) for ip in ips]


def render_plan(plan, errs, warns):
    L = []
    for x in errs:
        L.append("ERROR %s" % x)
    for x in warns:
        L.append("WARN  %s" % x)
    if plan.get("reserved_ips"):
        L.append("reserved (never push IPs): %s" % ", ".join(
            plan["reserved_ips"]))
    if plan.get("seed_source"):
        L.append("device id seed: %s%s" % (
            plan["seed_source"], " (fingerprint %s)" % plan["seed_fp"]
            if plan.get("seed_fp") else ""))
    L.append("network mode: %s | advertised TunerCount total: %d" % (
        plan["mode"], plan["tuners_total"]))
    if plan.get("merged"):
        g = plan["merged"]
        L.append("MERGED HDHR %s (plan only): one lineup from %d M1(s), "
                 "TunerCount %d, usable %s" % (
                     g["device_id"], len(plan["m1s"]), g["tuner_count"],
                     ", ".join("%s=%d" % kv for kv in sorted(g["usable"].items()))))
    for m in plan["m1s"]:
        res = m.get("m1_ip")
        L.append("M1 %s  addr=%s%s  hdhr=%s  tuners=%d  http=%d ts=%d" % (
            m["name"], m["m1"], " (= %s)" % res if res and res != m["m1"]
            else "", m["device_id"], m["tuner_count"],
            m["http_port"], m["ts_port"]))
        for d in m["brp_devs"]:
            L.append("   dev %s -> push %s:31061" % (
                d["dev"], d["push_ip"] or "(learned at first tune)"))
        if m["hls_tuner"]:
            L.append("   + 1 fixed HLS tuner (not retunable)")
    if plan["mode"] == "host":
        L.append("host mode: add each push IP as an alias on the host NIC "
                 "(not done for you):")
        L += ["   " + x for x in alias_hints(plan)]
    return "\n".join(L)
