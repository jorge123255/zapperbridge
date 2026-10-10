"""ZapperBox auto-discovery from BRzb announcements on UDP 31064.

ZapperBox devices broadcast a fixed 92-byte "BRzb" datagram to UDP 31064
(observed on a home LAN, 2026-10):

  * the M1 tuner box  (dev 02bb00a00001, model ZBM1-gt698, share=3, tuners=4)
  * a Zapper Mini     (dev 02bb00c00001, model ZBM3-gt316, share=1, tuners=0)

Layout (network order, ``MSG`` below; verified byte-for-byte against the
captured M1 + Mini announces and the two zApp fixtures in join_as_mini.py):

  magic "BRzb" | ver u8 | type u8 | share u8 | f4 u8
  dev 16s | ip 16s | name 16s | model 16s
  tuners u8 | sub u8 | f11 u8 | f12 u8 | token u32 | 3 x u32 reserved

type: 0 ANNOUNCE, 1 REQUEST, 2 RESPONSE, 3 DISCOVER.
share bits: 1 = library, 2 = tuners.

This module is *passive by default*: ``Listener`` only receives. Nothing here
talks BRP (TCP 31065) or touches :31061. ``build_discover`` exists for a
future, explicitly approved active probe; the facade never sends it on its
own (nor ``build_announce``, the registration step for a new device id).
Whether the M1 answers a DISCOVER, and how often it re-announces, is
not yet proven (one M1 announce was seen in a 54 s capture).
"""
import collections
import socket
import struct
import threading
import time

PORT = 31064
MSG = struct.Struct("!4sBBBB16s16s16s16sBBBBI3I")
MAGIC = b"BRzb"
TYPES = {0: "ANNOUNCE", 1: "REQUEST", 2: "RESPONSE", 3: "DISCOVER"}
SHARE_LIBRARY = 1
SHARE_TUNERS = 2

Announce = collections.namedtuple("Announce", [
    "type", "share", "dev", "ip", "name", "model", "tuners", "sub",
    "token", "src_ip"])


def _asc(blob):
    return blob.split(b"\x00", 1)[0].decode("ascii", "replace").strip()


def _pad(text):
    raw = (text or "").encode("ascii")[:16]
    return raw + b"\x00" * (16 - len(raw))


def parse(data, src_ip=None):
    """Parse one BRzb datagram -> Announce, or None if it isn't one."""
    if len(data) != MSG.size or data[:4] != MAGIC:
        return None
    f = MSG.unpack(data)
    return Announce(type=f[2], share=f[3], dev=_asc(f[5]).lower(),
                    ip=_asc(f[6]), name=_asc(f[7]), model=_asc(f[8]),
                    tuners=f[9], sub=f[10], token=f[13], src_ip=src_ip)


def build(msg_type, share, dev, ip, name="", model="", tuners=0, sub=0,
          token=0):
    return MSG.pack(MAGIC, 0, msg_type, share, 1, _pad(dev), _pad(ip),
                    _pad(name), _pad(model), tuners, sub, 0, 0,
                    token & 0xFFFFFFFF, 0, 0, 0)


def build_discover(dev, ip):
    """DISCOVER datagram (type 3). Not sent by the facade; see module doc."""
    return build(3, 0, dev, ip)


def build_announce(dev, ip, model="ZBM3-gt316", name="", token=0):
    """Client (Mini-class) ANNOUNCE: share=1, tuners=0.

    [observed in a fresh-ID test] a new
    device id gets api6 NOT_FOUND until it has been announced on UDP 31064;
    after an ANNOUNCE from 192.0.2.30 the join succeeds and the M1 pushes
    that id's streams to .83. So registration = ANNOUNCE from the push IP,
    then api6. Not sent automatically by the facade (an explicit, approved
    registration step); never announce a real Mini's id or IP.
    """
    return build(0, 1, dev, ip, name, model, 0, 0, token)


def is_tuner_box(a):
    """An M1-class box: shares tuners and has at least one."""
    return bool(a.share & SHARE_TUNERS) and a.tuners > 0


def is_client(a):
    """A Mini-class client: owns no tuner (consumes an M1 BRP lease)."""
    return a.tuners == 0


def address(a):
    """Where to reach the box. The UDP source wins over the self-reported ip
    field (the payload can be stale after a DHCP change)."""
    return a.src_ip or a.ip


class Registry:
    """Thread-safe map dev -> (Announce, last_seen)."""

    def __init__(self, max_age=900.0):
        self.max_age = max_age
        self.lock = threading.Lock()
        self.seen = {}

    def observe(self, a, now=None):
        if a is None or a.type not in (0, 2) or not a.dev:
            return False
        with self.lock:
            self.seen[a.dev] = (a, time.time() if now is None else now)
        return True

    def _fresh(self, now=None):
        now = time.time() if now is None else now
        with self.lock:
            return [a for a, t in self.seen.values()
                    if now - t <= self.max_age]

    def m1s(self, now=None):
        return sorted((a for a in self._fresh(now) if is_tuner_box(a)),
                      key=lambda a: a.dev)

    def clients(self, now=None):
        return sorted((a for a in self._fresh(now) if is_client(a)),
                      key=lambda a: a.dev)

    def snapshot(self, now=None):
        return [{"dev": a.dev, "ip": address(a), "model": a.model,
                 "name": a.name, "tuners": a.tuners, "share": a.share,
                 "kind": "m1" if is_tuner_box(a) else
                         ("client" if is_client(a) else "other")}
                for a in sorted(self._fresh(now), key=lambda a: a.dev)]


def pick_m1(registry, want=None, now=None):
    """Choose the M1 to serve. Returns (Announce or None, reason).

    want: None/"" or "auto" -> the only M1 seen (ambiguous if several);
          a 12-hex dev id or an IP -> that box only.
    Never guesses between several M1s: that is a per-user config decision.
    """
    m1s = registry.m1s(now)
    want = (want or "").strip().lower()
    if want.startswith("auto:"):
        want = want[5:]
    if want in ("", "auto"):
        if len(m1s) == 1:
            return m1s[0], "only M1 seen"
        if not m1s:
            return None, "no M1 announce seen"
        return None, "ambiguous: %d M1s seen (%s); set ZM1 to one" % (
            len(m1s), ", ".join("%s@%s" % (a.dev, address(a)) for a in m1s))
    for a in m1s:
        if want in (a.dev, address(a), a.ip):
            return a, "matched %s" % want
    return None, "M1 %s not seen" % want


class Listener:
    """Passive UDP 31064 listener feeding a Registry (receive-only)."""

    def __init__(self, registry=None, port=PORT, bind_ip="0.0.0.0"):
        self.registry = registry or Registry()
        self.port = port
        self.bind_ip = bind_ip
        self.sock = None
        self.stop_flag = False
        self.packets = 0
        self.thread = None

    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        s.bind((self.bind_ip, self.port))
        s.settimeout(1.0)
        self.sock = s
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        return self

    def _loop(self):
        while not self.stop_flag:
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            self.packets += 1
            self.registry.observe(parse(data, addr[0]))

    def shutdown(self):
        self.stop_flag = True
        try:
            self.sock.close()
        except Exception:
            pass


def resolve_m1(spec, wait=75.0, listener=None, poll=0.5):
    """Resolve ZM1. A literal IP is returned unchanged (no discovery at all).
    "auto" / "auto:<dev|ip>" listens passively up to `wait` s for an announce.
    Returns (ip or None, dev or None, reason)."""
    spec = (spec or "").strip()
    if spec and not spec.lower().startswith("auto"):
        return spec, None, "configured"
    own = listener is None
    lst = listener or Listener().start()
    try:
        end = time.time() + wait
        while True:
            a, why = pick_m1(lst.registry, spec)
            if a is not None or time.time() >= end or why.startswith("ambig"):
                return (address(a) if a else None,
                        a.dev if a else None, why)
            time.sleep(poll)
    finally:
        if own:
            lst.shutdown()


# ---- offline helpers (tests / tools) --------------------------------------
def read_pcap_udp(path, port=PORT):
    """[(t, src_ip, payload)] for IPv4 UDP datagrams to/from `port` in a
    classic little/big-endian pcap (Ethernet, BSD loopback or raw IP)."""
    with open(path, "rb") as fh:
        f = fh.read()
    magic = f[:4]
    if magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1"):
        e = "<"
    elif magic in (b"\xa1\xb2\xc3\xd4", b"\xa1\xb2\x3c\x4d"):
        e = ">"
    else:
        raise ValueError("not a classic pcap")
    nano = magic in (b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d")
    link = struct.unpack(e + "I", f[20:24])[0]
    out, off = [], 24
    while off + 16 <= len(f):
        ts, frac, incl, _orig = struct.unpack(e + "IIII", f[off:off + 16])
        pkt = f[off + 16:off + 16 + incl]
        off += 16 + incl
        if link == 1:
            if len(pkt) < 14 or pkt[12:14] != b"\x08\x00":
                continue
            ip = pkt[14:]
        elif link == 0:
            ip = pkt[4:]
        elif link in (101, 228):
            ip = pkt
        else:
            continue
        if len(ip) < 20 or ip[0] >> 4 != 4 or ip[9] != 17:
            continue
        ihl = (ip[0] & 15) * 4
        sp, dp, ulen = struct.unpack("!HHH", ip[ihl:ihl + 6])
        if port not in (sp, dp):
            continue
        out.append((ts + frac / (1e9 if nano else 1e6),
                    socket.inet_ntoa(ip[12:16]), ip[ihl + 8:ihl + ulen]))
    return out


if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) > 1 and sys.argv[1].endswith("cap"):
        reg = Registry(max_age=1e12)
        for t, src, data in read_pcap_udp(sys.argv[1]):
            reg.observe(parse(data, src), now=t)
        print(json.dumps(reg.snapshot(now=0), indent=1))
    else:
        wait = float(sys.argv[1]) if len(sys.argv) > 1 else 75.0
        print("listening passively on udp/%d for %.0fs" % (PORT, wait))
        lst = Listener().start()
        time.sleep(wait)
        lst.shutdown()
        print(json.dumps(lst.registry.snapshot(), indent=1))
