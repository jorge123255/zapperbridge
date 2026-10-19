"""Multi-M1 discovery supervisor (DRY-RUN by default, additive, not imported
by the facade).

Finds M1s from their UDP 31064 BRzb announce (passive listen, or an offline
pcap, or an optional TCP-connect sweep), reads the tuner count, builds the
per-M1 device-id + push-IP blocks through ``zbconf`` and prints a ``plan`` of
what WOULD be announced/registered. Nothing is sent unless ``--apply`` is given
on the command line (config files and env cannot turn it on).

What it never does (also asserted in tests/test_m1super.py):
  * send anything by default (``plan``/``discover`` are receive-only/offline);
  * use api7, sign anything out, spoof a source address, or touch an
    address in ``push.reserved_ips`` / a real Mini / an M1;
  * run ip/ifconfig (alias commands are only printed);
  * send an api6 join. Registration = ANNOUNCE from the push IP, then the
    facade's own bootstrap joins each id when it starts (see
    docs/MULTI-M1-PLAN.md section 5); ``--apply`` sends only the ANNOUNCE.

Evidence behind the tuner count (all from m1disco.py's captured announces):
  * M1 ``02bb00a00001``: share=3, tuners=4 -> 3 usable BRP leases; the 4th
    tuner is kept by the M1 for itself (its fixed HLS window)
    [observed: 3 leases, instances 3/4/5; "announced 4, only
    3 usable because it keeps the 4th for itself"].
  * RULE: usable tuners = announced tuners - 1 (the M1 keeps one). Only
    announced=4 (usable 3) is confirmed; every other value follows the same
    rule but is reported "unconfirmed".
  * Mini ``02bb00c00001``: share=1, tuners=0 (a client, never a tuner box).
  Only those two share values were ever seen. What the share byte means beyond
  "bit 2 = offers tuners, bit 1 = library" and what tuners means when it is not
  4 are UNCONFIRMED, so for any other value the plan says so and the config
  ``leases`` key (if set) always wins.
"""
import argparse
import ipaddress
import json
import os
import socket
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import m1config  # noqa: E402
import m1disco   # noqa: E402
import zbconf    # noqa: E402

BRP_PORT = 31065      # M1 BRP (TCP); the sweep only checks that it accepts
MAX_SWEEP_HOSTS = 1024
EVIDENCE_TUNERS = 4   # announced tuners byte seen on the one captured M1
M1_KEEPS = 1          # tuners the M1 keeps for itself (HLS); not usable by us


def usable_from_announced(announced):
    """usable = announced - 1 (the M1 keeps one for itself). Never < 0.
    Only announced=4 -> 3 is confirmed (see leases_from_announce)."""
    return max(0, int(announced) - M1_KEEPS)


# ---- tuner count -----------------------------------------------------------
def leases_from_announce(a):
    """(leases, confidence, note) for one announce.

    leases 0 = not an M1 we can use. confidence: "evidence" (value seen on a
    real M1), "unconfirmed" (extrapolated), "none"."""
    if not (a.share & m1disco.SHARE_TUNERS):
        if a.tuners > 0:
            return 0, "none", ("announces tuners=%d but share=%d has no "
                               "tuners bit: sharing is off (NOT_SHARED); turn "
                               "sharing on in the Zapper app" % (a.tuners, a.share))
        return 0, "none", "client (tuners=0), not a tuner box"
    if a.tuners <= 0:
        return 0, "none", "share has the tuners bit but tuners=0"
    if a.tuners == EVIDENCE_TUNERS:
        return (m1config.LEASES_PER_M1, "evidence",
                "announced tuners=%d = %d usable (announced - 1; the M1 keeps "
                "the 4th for itself) (matches the captured M1)"
                % (a.tuners, m1config.LEASES_PER_M1))
    n = max(1, min(m1config.LEASES_PER_M1, usable_from_announced(a.tuners)))
    return n, "unconfirmed", (
        "announced tuners=%d was never captured (only 4); applying the rule "
        "usable = announced - 1 = %d (capped at %d), UNCONFIRMED. "
        "Set leases in [[m1]] to override"
        % (a.tuners, n, m1config.LEASES_PER_M1))


class Found:
    """One discovered (or configured) box."""

    def __init__(self, dev, ip, model="", name="", tuners=0, share=0,
                 source="announce", kind="m1", leases=0, confidence="none",
                 note="", seen=None):
        self.dev, self.ip, self.model, self.name = dev, ip, model, name
        self.tuners, self.share, self.source, self.kind = tuners, share, source, kind
        self.leases, self.confidence, self.note = leases, confidence, note
        self.seen = seen

    def as_dict(self):
        return {k: getattr(self, k) for k in (
            "dev", "ip", "model", "name", "tuners", "share", "source", "kind",
            "leases", "confidence", "note")}


def found_from_announce(a, source="announce", now=None):
    kind = ("m1" if m1disco.is_tuner_box(a) else
            "client" if m1disco.is_client(a) else "other")
    leases, conf, note = leases_from_announce(a)
    return Found(a.dev, m1disco.address(a), a.model, a.name, a.tuners, a.share,
                 source, kind, leases, conf, note, now)


def collect(registry, now=None, source="announce"):
    """Found list from an m1disco.Registry (receive-only data)."""
    out = []
    for a in registry._fresh(now):
        out.append(found_from_announce(a, source))
    return sorted(out, key=lambda f: f.dev)


def observe_packets(packets, registry=None, now=None):
    """Feed [(src_ip, datagram bytes)] (fake or pcap) into a Registry."""
    reg = registry or m1disco.Registry(max_age=1e12)
    for src, data in packets:
        reg.observe(m1disco.parse(data, src), now=now if now is not None else 0)
    return reg


def packets_from_pcap(path):
    return [(src, data) for _t, src, data in m1disco.read_pcap_udp(path)]


def listen_passive(seconds, port=m1disco.PORT, bind_ip="0.0.0.0", registry=None):
    """Receive-only UDP 31064 listen (binds, never sends). Returns a Registry."""
    lst = m1disco.Listener(registry=registry, port=port, bind_ip=bind_ip).start()
    try:
        time.sleep(max(0.0, seconds))
    finally:
        lst.shutdown()
    return lst.registry


# ---- optional sweep (TCP connect only; off unless --sweep) -----------------
def _tcp_open(ip, port=BRP_PORT, timeout=0.4):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, port))
        return True
    except OSError:
        return False
    finally:
        s.close()   # connect+close only: no BRP bytes are ever written


def sweep(cidrs, skip=(), connect=_tcp_open, port=BRP_PORT):
    """Addresses in ``cidrs`` that accept a TCP connection on the BRP port.
    Sends nothing but the TCP handshake. ``skip`` (reserved IPs, real Minis,
    already-known M1s) is never contacted. ``connect`` is injectable."""
    skip = set(skip)
    hosts = []
    for c in zbconf._as_list(cidrs):
        try:
            net = ipaddress.ip_network(c, strict=False)
        except ValueError as e:
            raise zbconf.ConfigError("sweep %r: %s" % (c, e))
        if net.num_addresses > MAX_SWEEP_HOSTS + 2:
            raise zbconf.ConfigError(
                "sweep %s too large (max %d hosts)" % (c, MAX_SWEEP_HOSTS))
        hosts += [str(h) for h in net.hosts()]
    return [h for h in dict.fromkeys(hosts)
            if h not in skip and connect(h, port)]


# ---- merge discovery with the user's config -> zbconf plan -----------------
def _entry_matches(entry, f):
    addr = str(entry.get("address", "")).strip().lower()
    if addr.startswith("auto:"):
        addr = addr[5:]
    return addr in (f.dev, f.ip.lower())


def _is_bare_auto(entry):
    return str(entry.get("address", "")).strip().lower() in ("", "auto")


def merge_config(cfg, found, sweep_ips=()):
    """(cfg2, notes): the user's cfg plus an ``[[m1]]`` entry per discovered
    M1 that is not already configured. Configured entries always win (their
    ``leases``/``devs``/``push_ips`` are untouched); a discovered M1 uses the
    announce-derived lease count. Real clients seen on the LAN (tuners=0)
    have their IPs and ids added to the reserved/never lists.

    Each entry's discovered address is recorded as ``_resolved_ip`` (an
    ``auto``/``auto:<dev>`` spec stays as written, so the device ids are
    derived from exactly the same key the facade uses; only ``--apply``
    needs the IP). A bare ``auto`` entry (or no ``[[m1]]`` at all, which the
    facade treats as one ``auto`` entry) is bound to the first discovered M1
    not claimed by an explicit entry, instead of adding a second entry with
    the IP as its key (that derived different ids from the running facade)."""
    cfg = {k: (dict(v) if isinstance(v, dict) else
               list(v) if isinstance(v, list) else v) for k, v in cfg.items()}
    cfg["m1"] = [dict(e) for e in cfg.get("m1") or []]
    push = cfg["push"] = dict(cfg.get("push") or {})
    notes = []
    clients = [f for f in found if f.kind == "client"]
    if clients:
        res = zbconf._as_list(push.get("reserved_ips")) + zbconf._as_list(
            push.get("never_ips"))
        nd = zbconf._as_list(push.get("never_devs"))
        for c in clients:
            if c.ip and c.ip not in res:
                res.append(c.ip)
            if c.dev and c.dev not in nd:
                nd.append(c.dev)
            notes.append("protected real client %s @ %s (%s): added to "
                         "reserved IPs / never devs" % (c.dev, c.ip, c.model or "?"))
        push["reserved_ips"], push["never_devs"] = res, nd
    m1s = []
    for f in found:
        if f.kind == "other":
            notes.append("%s @ %s ignored: %s" % (f.dev, f.ip, f.note))
        if f.kind == "m1":
            m1s.append(f)
    if not cfg["m1"] and any(f.leases >= 1 for f in m1s):
        # the facade's implicit default: one auto entry (expand() names it
        # m1-1 and keys its ids on that name)
        cfg["m1"].append({"address": "auto"})
        notes.append("no [[m1]] entries: the first discovered M1 is planned "
                     "as the facade's default \"auto\" entry (same ids as run)")

    def note_leases(e, f):
        if f.leases and "leases" not in e:
            e["leases"] = f.leases
            notes.append("%s: leases=%d from announce (%s)" % (
                f.dev, f.leases, f.confidence))
        elif "leases" in e and f.leases and int(e["leases"]) != f.leases:
            notes.append("%s: config leases=%s overrides announce-derived "
                         "%d (%s)" % (f.dev, e["leases"], f.leases,
                                      f.confidence))

    claimed = set()
    for f in m1s:   # explicit matches first (an IP or auto:<dev|ip>)
        hit = [e for e in cfg["m1"] if not _is_bare_auto(e)
               and "_resolved_ip" not in e and _entry_matches(e, f)]
        if hit:
            hit[0]["_resolved_ip"] = f.ip
            note_leases(hit[0], f)
            claimed.add(f.dev)
    for f in m1s:   # then bare "auto" entries take the rest, in dev order
        if f.dev in claimed or f.leases < 1:
            continue
        free = [e for e in cfg["m1"] if _is_bare_auto(e)
                and "_resolved_ip" not in e]
        if free:
            free[0]["_resolved_ip"] = f.ip
            note_leases(free[0], f)
            claimed.add(f.dev)
            notes.append("auto entry %s resolved to M1 %s @ %s" % (
                free[0].get("name") or "#%d" % (cfg["m1"].index(free[0]) + 1),
                f.dev, f.ip))
    for f in m1s:
        if f.dev in claimed:
            continue
        if f.leases < 1:
            notes.append("%s @ %s skipped: %s" % (f.dev, f.ip, f.note))
            continue
        cfg["m1"].append({"name": "m1-%s" % f.dev[-6:], "address": f.ip,
                          "leases": f.leases, "_resolved_ip": f.ip})
        notes.append("discovered M1 %s @ %s: %s [%s]" % (
            f.dev, f.ip, f.note, f.confidence))
    for ip in sweep_ips:
        if not any(str(e.get("address", "")) == ip for e in cfg["m1"]) and not any(
                f.ip == ip for f in found):
            notes.append("sweep: %s accepts TCP %d but sent no announce; not "
                         "added (no device id/tuner count). Add it as an "
                         "[[m1]] address if it is an M1" % (ip, BRP_PORT))
    return cfg, notes


def _literal_ip(addr):
    try:
        ipaddress.IPv4Address(str(addr))
        return str(addr)
    except ValueError:
        return None


def announce_preview(plan):
    """What ``--apply`` would send, as data. Builds the datagrams (so the
    layout is checked) but sends nothing."""
    out = []
    for m in plan["m1s"]:
        for d in m["brp_devs"]:
            ip = d["push_ip"]
            pkt = m1disco.build_announce(d["dev"], ip or "")
            out.append({"m1": m["name"], "dev": d["dev"], "push_ip": ip,
                        "announce": {"udp": "255.255.255.255:%d" % m1disco.PORT,
                                     "source_ip": ip, "ip_field": ip,
                                     "share": 1, "tuners": 0,
                                     "bytes": len(pkt), "hex": pkt.hex()},
                        "then": "facade bootstrap: api6 join for this id from %s "
                                "(not done by m1super)" % (ip or "its push IP")})
    return out


def build_plan(cfg, found=(), sweep_ips=(), env=None, create_seed=False):
    """(plan dict, errors, warnings, notes, merged cfg). Pure unless
    create_seed (only --apply): then a missing per-install seed file is
    created so the announced ids match what `run` will use.

    The ids come from the same zbconf.expand() call `run` makes, on the same
    entries (an auto spec is kept as written). Each planned M1 also gets
    ``m1_ip``: the literal address, or the discovered IP of an auto spec
    (None when discovery did not resolve it)."""
    cfg2, notes = merge_config(cfg, list(found), sweep_ips)
    resolved = [e.pop("_resolved_ip", None) for e in cfg2["m1"]]
    cfg3 = zbconf.apply_env(cfg2, env=env or {})
    cfg3, seed_source = zbconf.resolve_seed(cfg3, env=env, create=create_seed)
    _core, plan, errs, warns = zbconf.expand(cfg3)
    plan["seed_source"] = seed_source
    for i, m in enumerate(plan["m1s"]):
        ip = _literal_ip(m["m1"]) or (resolved[i] if i < len(resolved)
                                      else None)
        m["m1_ip"] = ip
        if m["m1"].lower().startswith("auto") and not ip:
            warns.append("%s: address %s not resolved by discovery (listen "
                         "longer with --listen, or set the M1's IP)" % (
                             m["name"], m["m1"]))
    return plan, errs, warns, notes, cfg2


def render(found, plan, errs, warns, notes, apply=False):
    L = ["== discovered =="]
    if not found:
        L.append("(nothing seen)")
    for f in found:
        L.append("%-6s %s @ %s model=%s share=%d tuners=%d src=%s" % (
            f.kind, f.dev, f.ip, f.model or "?", f.share, f.tuners, f.source))
        if f.kind == "m1":
            L.append("       -> leases=%d [%s] %s" % (f.leases, f.confidence, f.note))
    for n in notes:
        L.append("note: " + n)
    L.append("")
    L.append("== plan (%s) ==" % ("APPLY requested" if apply else "DRY RUN: nothing is sent"))
    L.append(zbconf.render_plan(plan, errs, warns))
    L.append("")
    L.append("== would announce (UDP 31064, from each push IP) ==")
    pv = announce_preview(plan)
    if not pv:
        L.append("(nothing)")
    for p in pv:
        L.append("%s dev %s: ANNOUNCE ip=%s share=1 tuners=0, then %s" % (
            p["m1"], p["dev"], p["push_ip"] or "(no push IP)", p["then"]))
    return "\n".join(L)


# ---- the only code path that sends: explicit --apply -----------------------
class ApplyRefused(Exception):
    pass


def _udp_sender(src_ip, data, m1=None, port=m1disco.PORT):
    """Broadcast the ANNOUNCE and, when ``m1`` is given, also unicast it to the
    M1 (the hand-run test that registered fresh ids did both). Each target is
    tried on its own; returns [(target, error or None)] so a failed unicast
    does not hide a successful broadcast (or the other way round)."""
    out = []
    for tgt in ["255.255.255.255"] + ([m1] if m1 else []):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.bind((src_ip, 0))  # real source = the push IP, no spoofing
            s.sendto(data, (tgt, port))
            out.append((tgt, None))
        except OSError as e:
            out.append((tgt, "%s: %s" % (type(e).__name__, e)))
        finally:
            s.close()
    return out


def apply_plan(plan, errs, apply=False, send=None, is_local=zbconf._is_local):
    """Send one ANNOUNCE per planned (dev, push IP). ``apply`` must be True
    (the CLI sets it only from --apply). Refuses on any config error, on a
    missing push IP, or when the push IP is not owned by this host (a real
    source address is required; nothing is spoofed). The unicast copy goes to
    the M1's resolved IP (``m1_ip``), never to an unresolved ``auto:`` spec.
    A send error is reported per id (``sent`` False + ``error``) instead of
    aborting with a traceback. ``send`` is injectable for tests."""
    if apply is not True:
        raise ApplyRefused("apply requires an explicit apply=True (--apply)")
    if errs:
        raise ApplyRefused("config has errors; refusing to announce: %s" % errs[0])
    send = send or _udp_sender
    never = set(plan.get("reserved_ips") or ()) | {m["m1"] for m in plan["m1s"]} \
        | {m.get("m1_ip") for m in plan["m1s"] if m.get("m1_ip")}
    todo = []
    for m in plan["m1s"]:
        target = m.get("m1_ip") or _literal_ip(m["m1"])
        for d in m["brp_devs"]:
            ip, dev = d["push_ip"], d["dev"]
            if not ip:
                raise ApplyRefused("%s dev %s has no push IP" % (m["name"], dev))
            if ip in never:
                raise ApplyRefused("%s: push ip %s is reserved or an M1" % (m["name"], ip))
            if dev in zbconf.NEVER_DEVS:
                raise ApplyRefused("%s: dev %s is a real device's id" % (m["name"], dev))
            if not is_local(ip):
                raise ApplyRefused("push ip %s is not assigned to this host; add "
                                   "the alias first (printed by plan)" % ip)
            todo.append((m["name"], dev, ip, target, m["m1"]))
    res = []
    for name, dev, ip, target, spec in todo:
        r = {"m1": name, "dev": dev, "push_ip": ip, "m1_ip": target,
             "sent": True, "error": None, "note": None}
        if not target:
            r["note"] = ("M1 address %s not resolved: broadcast only, no "
                         "unicast copy" % spec)
        # token = unix time, as in the announce that registered fresh ids
        try:
            per = send(ip, m1disco.build_announce(
                dev, ip, token=int(time.time()) & 0x7fffffff), target)
        except OSError as e:
            per = [("send", "%s: %s" % (type(e).__name__, e))]
        bad = [(t, e) for t, e in (per or []) if e]
        if bad:
            r["error"] = "; ".join("%s -> %s" % (t, e) for t, e in bad)
            r["sent"] = len(bad) < len(per)   # partly sent
        res.append(r)
    return res


def check_active(plan, active):
    """Mismatches between this plan's ids and the ids the running facade
    recorded at start (zbconf.read_active). [] when they agree or when there
    is no record for an M1."""
    out = []
    by_name = {m.get("name"): m for m in (active or {}).get("m1s") or []}
    for m in plan["m1s"]:
        a = by_name.get(m["name"])
        if not a:
            continue
        mine = [d["dev"] for d in m["brp_devs"]]
        if list(a.get("devs") or []) != mine:
            out.append("%s: this plan would announce %s but the bridge that "
                       "last ran with this data dir uses %s" % (
                           m["name"], ",".join(mine), ",".join(a.get("devs") or [])))
    return out


# ---- CLI --------------------------------------------------------------------
def main(argv=None, out=print):
    ap = argparse.ArgumentParser(
        prog="m1super", description="multi-M1 discovery plan (dry run by default)")
    ap.add_argument("cmd", nargs="?", default="plan", choices=("plan", "discover"))
    ap.add_argument("--config", default=os.environ.get("ZB_CONFIG", "/config/zapperbridge.toml"))
    ap.add_argument("--listen", type=float, default=0.0, metavar="SEC",
                    help="passively listen on UDP 31064 for SEC seconds (receive-only)")
    ap.add_argument("--from-pcap", metavar="FILE", help="read announces from a pcap (offline)")
    ap.add_argument("--sweep", action="append", default=[], metavar="CIDR",
                    help="also TCP-connect-check CIDR on the BRP port (no bytes sent)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--apply", action="store_true",
                    help="SEND one ANNOUNCE per planned id from its push IP "
                         "(off unless given; never exercised by tests on hardware)")
    a = ap.parse_args(argv)
    cfg = {}
    if os.path.exists(a.config):
        try:
            cfg = zbconf.load(a.config)
        except Exception as e:
            out("ERROR cannot read %s: %s" % (a.config, e))
            return 2
    cfg = zbconf.apply_env(cfg)
    disc = cfg.get("discovery") or {}
    listen = a.listen or float(disc.get("listen_seconds", 0) or 0)
    sweeps = a.sweep or zbconf._as_list(disc.get("sweep"))
    reg = m1disco.Registry(max_age=1e12)
    if a.from_pcap:
        observe_packets(packets_from_pcap(a.from_pcap), reg)
    if listen > 0:
        listen_passive(listen, registry=reg)
    found = collect(reg)
    sweep_ips = []
    if sweeps:
        known = {f.ip for f in found}
        reserved = set(zbconf._as_list((cfg.get("push") or {}).get("reserved_ips")))
        try:
            sweep_ips = sweep(sweeps, skip=known | reserved)
        except zbconf.ConfigError as e:
            out("ERROR %s" % e)
            return 2
    plan, errs, warns, notes, _cfg2 = build_plan(cfg, found, sweep_ips,
                                                  create_seed=a.apply)
    if a.json:
        out(json.dumps({"found": [f.as_dict() for f in found], "notes": notes,
                        "errors": errs, "warnings": warns, "plan": plan,
                        "would_announce": announce_preview(plan)}, indent=1))
    else:
        out(render(found, plan, errs, warns, notes, a.apply))
    if errs:
        return 2
    if a.apply:
        active, why = zbconf.read_active()
        if active is None:
            out("note: no record of a running bridge in %s (%s). If the bridge "
                "runs in another container, run this inside it so both use the "
                "same /data/seed: docker compose exec zapperbridge python3 "
                "/app/docker/entrypoint.py discover --listen 15 --apply"
                % (zbconf.data_dir(), why))
        else:
            mism = check_active(plan, active)
            if mism:
                for x in mism:
                    out("REFUSED: " + x)
                out("REFUSED: device id seed fingerprint here %s, bridge %s. "
                    "This container sees a different /data (seed) than the "
                    "running bridge. Run it inside the bridge container: "
                    "docker compose exec zapperbridge python3 "
                    "/app/docker/entrypoint.py discover --listen 15 --apply"
                    % (plan.get("seed_fp"), active.get("seed_fp")))
                return 3
        try:
            res = apply_plan(plan, errs, apply=True)
        except ApplyRefused as e:
            out("REFUSED: %s" % e)
            return 3
        failed = 0
        for r in res:
            if r.get("note"):
                out("note: %s dev %s: %s" % (r["m1"], r["dev"], r["note"]))
            if r.get("error"):
                failed += 1
                out("%s %s from %s (M1 %s): %s" % (
                    "PARTLY announced" if r["sent"] else "FAILED to announce",
                    r["dev"], r["push_ip"], r.get("m1_ip") or "unresolved",
                    r["error"]))
            else:
                out("announced %s from %s (broadcast%s)" % (
                    r["dev"], r["push_ip"],
                    " + unicast to %s" % r["m1_ip"] if r.get("m1_ip") else ""))
        if failed:
            return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
