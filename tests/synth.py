"""Synthetic BRzb announce capture, built at test time (no real capture shipped).

Two announces on UDP 31064, using documentation addresses (RFC 5737):
an M1-like tuner box (192.0.2.10) and a client-like device (192.0.2.237).
"""
import os
import socket
import struct
import tempfile

import m1disco

M1_DEV, M1_IP = "02bb00a00001", "192.0.2.10"
CLIENT_DEV, CLIENT_IP = "02bb00c00001", "192.0.2.237"


def _frame(src_ip, payload):
    udp = struct.pack("!HHHH", 59000, 31064, 8 + len(payload), 0) + payload
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 1, 0x4000, 64,
                     17, 0, socket.inet_aton(src_ip),
                     socket.inet_aton("192.0.2.255")) + udp
    return b"\xff" * 6 + b"\x02\x00\x00\x00\x00\x01" + b"\x08\x00" + ip


def brzb_pcap(path=None):
    """Write the 2-packet classic pcap (Ethernet) and return its path."""
    if path is None:
        fd, path = tempfile.mkstemp(suffix=".pcap", prefix="synth-brzb-")
        os.close(fd)
    pk = [
        (M1_IP, m1disco.build(0, 3, M1_DEV, M1_IP, "", "ZBM1-gt698", 4, 1)),
        (CLIENT_IP, m1disco.build(0, 1, CLIENT_DEV, CLIENT_IP, "",
                                  "ZBM3-gt316", 0, 0)),
    ]
    with open(path, "wb") as fh:
        fh.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
        for i, (ip, data) in enumerate(pk):
            fr = _frame(ip, data)
            fh.write(struct.pack("<IIII", 1700000000 + i, 0, len(fr), len(fr)))
            fh.write(fr)
    return path
