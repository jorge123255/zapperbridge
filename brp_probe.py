#!/usr/bin/env python3
"""BRP client probe for ZapperBox TCP 31065 (DVR_LIST / ping / api scan)."""
import socket
import struct
import zlib
import json
import sys
import time

FNV_OFFSET = 0x811C9DC5
FNV_PRIME = 0x01000193
PING_TEMPLATE = bytes.fromhex("d4ef54f130203f5c37ef07d1fe190ba1")

API = {
    "PING": 0,
    "DVR_LIST": 1,
    "DVR_START": 2,
    "DVR_STOP": 3,
    "PLAY_POS": 4,
    "DVR_DELETE": 5,
    "FILE_DOWNLOAD": 18,
}

ERRORS = [
    "SUCCESS", "FAIL", "BUSY", "BAD_API", "NOT_READY", "NOT_FOUND", "NO_DATA",
    "APP_TOO_OLD", "DEVICE_ID_NOT_ALLOWED", "DATABASE_ERROR", "NO_LICENSE",
    "NOT_SHARED", "NETWORK_FAIL", "BAD_PROTOCOL", "PACKET_ERROR", "BAD_RESPONSE",
    "PLAYBACK_NOT_ALLOWED", "BAD_INSTANCE", "NO_TUNER_AVAILABLE", "SW_TOO_OLD",
    "DRM_NOT_ALLOWED", "DRM_NOT_ENABLED", "FILE_TOO_LARGE", "FILE_CANCELLED",
    "RECORD_ACTIVE",
]


def fnv1a32(data: bytes) -> int:
    h = FNV_OFFSET
    for b in data:
        h = ((h ^ b) * FNV_PRIME) & 0xFFFFFFFF
    return h


def sign_ping(buf: bytearray) -> None:
    buf[0x30:0x40] = PING_TEMPLATE
    h = fnv1a32(bytes(buf))
    buf[0x35] = (h >> 24) & 0xFF
    buf[0x36] = (h >> 16) & 0xFF
    buf[0x39] = (h >> 8) & 0xFF
    buf[0x3A] = h & 0xFF


def pack(api, device_id, a0=0, a1=0, a2=0, a3=0, flags=1, payload=b"", ping=False,
         len1=None, len2=None):
    buf = bytearray(64)
    buf[0:4] = b"BRrp"
    buf[4:8] = b"\x00\x00\x10\x00"
    did = device_id.encode("ascii")[:16].ljust(16, b"\x00")
    buf[8:24] = did
    struct.pack_into(">H", buf, 24, flags)
    struct.pack_into(">H", buf, 26, api)
    struct.pack_into(">I", buf, 28, a0)
    struct.pack_into(">I", buf, 32, a1)
    struct.pack_into(">I", buf, 36, a2)
    extra = b""
    if len1 is not None or len2 is not None:
        struct.pack_into(">I", buf, 40, len1 or 0)
        struct.pack_into(">I", buf, 44, len2 or 0)
        extra = payload
    elif payload:
        plen = len(payload) + 1
        struct.pack_into(">I", buf, 40, plen)
        struct.pack_into(">I", buf, 44, plen)
        extra = payload + b"\x00"
    struct.pack_into(">I", buf, 48, a3)
    if ping:
        sign_ping(buf)
    return bytes(buf) + extra


def unpack_header(h: bytes) -> dict:
    assert len(h) >= 64
    d = {
        "magic": h[0:4],
        "b4_8": h[4:8].hex(),
        "id": h[8:24].split(b"\x00", 1)[0].decode("ascii", "replace"),
        "flags": struct.unpack_from(">H", h, 24)[0],
        "api": struct.unpack_from(">H", h, 26)[0],
        "a0": struct.unpack_from(">I", h, 28)[0],
        "a1": struct.unpack_from(">I", h, 32)[0],
        "a2": struct.unpack_from(">I", h, 36)[0],
        "len1": struct.unpack_from(">I", h, 40)[0],
        "len2": struct.unpack_from(">I", h, 44)[0],
        "a3": struct.unpack_from(">I", h, 48)[0],
        "u52": struct.unpack_from(">I", h, 52)[0],
        "u56": struct.unpack_from(">I", h, 56)[0],
        "u60": struct.unpack_from(">I", h, 60)[0],
        "raw": h[:64].hex(),
    }
    err = d["a0"]
    d["err_name"] = ERRORS[err] if err < len(ERRORS) else f"UNKNOWN({err})"
    return d


def recv_exact(sock, n, timeout=8.0):
    sock.settimeout(timeout)
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


def try_inflate(payload: bytes, claimed):
    if not payload:
        return payload
    for blob in (payload, payload[4:], struct.pack(">I", claimed or len(payload)) + payload):
        for wbits in (15, -15, 31):
            try:
                return zlib.decompress(blob, wbits)
            except Exception:
                pass
    return payload


def brp_tcp(host, api, device_id, **kw):
    payload = kw.pop("payload", b"")
    ping = kw.pop("ping", False)
    timeout = kw.pop("timeout", 8.0)
    pkt = pack(api, device_id, payload=payload, ping=ping, **kw)
    s = socket.socket()
    s.settimeout(timeout)
    t0 = time.time()
    try:
        s.connect((host, 31065))
        s.sendall(pkt)
        hdr = recv_exact(s, 64, timeout)
        dt = time.time() - t0
        if len(hdr) < 64:
            return {"ok": False, "note": f"short header {len(hdr)}", "raw": hdr.hex(), "dt": dt}
        u = unpack_header(hdr)
        n = u["len2"] or u["len1"]
        body = b""
        if n:
            body = recv_exact(s, n, timeout)
            u["body_got"] = len(body)
        rest = b""
        s.settimeout(0.3)
        try:
            while True:
                c = s.recv(4096)
                if not c:
                    break
                rest += c
                if len(rest) > 1_000_000:
                    break
        except socket.timeout:
            pass
        decoded = try_inflate(body, u["len1"])
        if u["len1"] > 1 and len(decoded) >= u["len1"] - 1:
            decoded = decoded[: u["len1"] - 1]
        u["ok"] = True
        u["dt"] = dt
        u["body"] = decoded
        u["rest"] = rest
        u["req_hex"] = pkt[:64].hex()
        return u
    except Exception as e:
        return {"ok": False, "error": repr(e), "dt": time.time() - t0}
    finally:
        try:
            s.close()
        except Exception:
            pass


def brp_udp(host, api, device_id, **kw):
    pkt = pack(api, device_id, ping=kw.pop("ping", False), **kw)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(3.0)
    try:
        s.sendto(pkt, (host, 31065))
        data, addr = s.recvfrom(4096)
        u = unpack_header(data[:64]) if len(data) >= 64 else {}
        u["ok"] = True
        u["from"] = addr
        u["udp_len"] = len(data)
        u["raw"] = data[:128].hex()
        if len(data) > 64:
            u["body"] = data[64:]
        return u
    except Exception as e:
        return {"ok": False, "error": repr(e)}
    finally:
        s.close()


def show(label, r):
    print(f"\n=== {label} ===")
    if not r.get("ok"):
        print(r)
        return
    body = r.get("body", b"")
    rest = r.get("rest", b"")
    meta = {k: r[k] for k in (
        "id", "flags", "api", "a0", "err_name", "a1", "a2", "len1", "len2",
        "a3", "u52", "u56", "u60", "b4_8", "dt",
    ) if k in r}
    print(meta)
    print("hdr", r.get("raw", "")[:160])
    if body:
        preview = body[:400]
        print("body", len(body), preview[:120])
        try:
            txt = body.decode("utf-8")
            print("utf8", txt[:800])
        except Exception:
            pass
        try:
            print("json", json.dumps(json.loads(body), indent=2)[:1500])
        except Exception:
            pass
    if rest:
        print("rest", len(rest), rest[:80])


def main():
    hosts = [
        ("M1", "192.0.2.10", "02bb00a00001"),
        ("Mini", "192.0.2.21", "02bb00c00001"),
    ]
    for name, ip, did in hosts:
        print(f"\n########## {name} {ip} id={did} ##########")
        show(f"{name} DVR_LIST boxid", brp_tcp(ip, API["DVR_LIST"], did))
        show(f"{name} DVR_LIST empty-id", brp_tcp(ip, API["DVR_LIST"], ""))
        show(f"{name} DVR_LIST peer-id", brp_tcp(
            ip, API["DVR_LIST"],
            "02bb00c00001" if name == "M1" else "02bb00a00001",
        ))
        show(f"{name} PING tcp", brp_tcp(ip, API["PING"], did, ping=True, a0=0x11223344, a1=0x55667788, a2=0x99AABBCC))
        show(f"{name} PING udp", brp_udp(ip, API["PING"], did, ping=True, a0=0x11223344, a1=0x55667788, a2=0x99AABBCC))

if __name__ == "__main__":
    main()
