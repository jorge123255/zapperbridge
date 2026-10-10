"""Minimal SSDP (UPnP discovery) for the HDHR facade.

Answers M-SEARCH on 239.255.255.250:1900, sends NOTIFY ssdp:alive every
~10 min and ssdp:byebye on shutdown. LOCATION always uses one fixed LAN IP
(the advertise IP), never the en0 alias addresses.
"""
import random
import socket
import struct
import threading
import time
import uuid

GROUP = "239.255.255.250"
ST_ROOT = "upnp:rootdevice"
ST_MEDIA = "urn:schemas-upnp-org:device:MediaServer:1"
SERVER = "macOS/1 UPnP/1.0 zapperbridge-hdhr/1"


def udn_for(device_id):
    """Stable UPnP UDN derived from the HDHR DeviceID (DeviceID unchanged)."""
    return "uuid:%s" % uuid.uuid5(uuid.NAMESPACE_URL,
                                  "zapperbridge-hdhr:%s" % device_id.upper())


def primary_ip(probe="192.0.2.10"):
    """Source IP the kernel picks toward the LAN (not an alias)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((probe, 9))
        return s.getsockname()[0]
    finally:
        s.close()


class Ssdp:
    def __init__(self, ip, http_port, device_id, port=1900, max_age=1800,
                 notify_every=600, log=print):
        self.ip = ip
        self.port = port
        self.location = "http://%s%s/device.xml" % (
            ip, "" if http_port == 80 else ":%d" % http_port)
        self.udn = udn_for(device_id)
        self.max_age = max_age
        self.notify_every = notify_every
        self.log = log
        self.stop = False
        self.replies = 0
        self.notifies = 0
        self.sock = None
        self.out = None

    # -- message builders -------------------------------------------------
    def _usn(self, nt):
        return self.udn if nt == self.udn else "%s::%s" % (self.udn, nt)

    def _targets(self):
        return [ST_ROOT, self.udn, ST_MEDIA]

    def _reply(self, st):
        return ("HTTP/1.1 200 OK\r\n"
                "CACHE-CONTROL: max-age=%d\r\n"
                "DATE: %s\r\n"
                "EXT:\r\n"
                "LOCATION: %s\r\n"
                "SERVER: %s\r\n"
                "ST: %s\r\n"
                "USN: %s\r\n"
                "\r\n" % (self.max_age,
                          time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime()),
                          self.location, SERVER, st, self._usn(st))).encode()

    def _notify(self, nt, nts):
        lines = ["NOTIFY * HTTP/1.1",
                 "HOST: %s:%d" % (GROUP, self.port),
                 "NT: %s" % nt,
                 "NTS: %s" % nts,
                 "USN: %s" % self._usn(nt)]
        if nts == "ssdp:alive":
            lines += ["CACHE-CONTROL: max-age=%d" % self.max_age,
                      "LOCATION: %s" % self.location,
                      "SERVER: %s" % SERVER]
        return ("\r\n".join(lines) + "\r\n\r\n").encode()

    # -- sockets ------------------------------------------------------------
    def _open(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        s.bind(("", self.port))
        mreq = struct.pack("4s4s", socket.inet_aton(GROUP),
                           socket.inet_aton(self.ip))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        self.sock = s
        o = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        o.bind((self.ip, 0))          # replies/notifies leave from the advertise IP
        o.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                     socket.inet_aton(self.ip))
        o.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        self.out = o

    def send_notify(self, nts):
        for nt in self._targets():
            try:
                self.out.sendto(self._notify(nt, nts), (GROUP, self.port))
                self.notifies += 1
            except Exception as e:
                self.log("ssdp notify %s err %r" % (nts, e))

    def _handle(self, data, addr):
        try:
            text = data.decode("utf-8", "replace")
        except Exception:
            return
        if not text.startswith("M-SEARCH"):
            return
        hdr = {}
        for line in text.split("\r\n")[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                hdr[k.strip().upper()] = v.strip()
        if hdr.get("MAN", "").strip('"') != "ssdp:discover":
            return
        st = hdr.get("ST", "")
        if st == "ssdp:all":
            sts = self._targets()
        elif st in self._targets():
            sts = [st]
        else:
            return
        try:
            mx = max(0, min(int(hdr.get("MX", "1")), 3))
        except ValueError:
            mx = 1

        def go():
            time.sleep(random.uniform(0, mx * 0.5) if mx else 0)
            for x in sts:
                try:
                    self.out.sendto(self._reply(x), addr)
                    self.replies += 1
                except Exception as e:
                    self.log("ssdp reply err %r" % (e,))
        threading.Thread(target=go, daemon=True).start()

    def _rx(self):
        while not self.stop:
            try:
                data, addr = self.sock.recvfrom(4096)
            except OSError:
                if self.stop:
                    return
                time.sleep(0.5)
                continue
            self._handle(data, addr)

    def _alive_loop(self):
        while not self.stop:
            self.send_notify("ssdp:alive")
            for _ in range(int(self.notify_every)):
                if self.stop:
                    return
                time.sleep(1)

    def start(self):
        self._open()
        threading.Thread(target=self._rx, daemon=True).start()
        threading.Thread(target=self._alive_loop, daemon=True).start()
        self.log("ssdp on :%d group %s if %s location %s udn %s" % (
            self.port, GROUP, self.ip, self.location, self.udn))
        return self

    def shutdown(self):
        if self.stop:
            return
        self.stop = True
        try:
            self.send_notify("ssdp:byebye")
            self.log("ssdp byebye sent")
        except Exception:
            pass
        for s in (self.sock, self.out):
            try:
                s.close()
            except Exception:
                pass

    def stats(self):
        return {"enabled": True, "ip": self.ip, "port": self.port,
                "location": self.location, "udn": self.udn,
                "msearch_replies": self.replies, "notifies": self.notifies}
