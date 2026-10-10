"""Passive per-channel codec learning (VideoCodec / AudioCodec for /lineup.json).

When a client tunes a channel, the PMT in the stream the bridge already
serves is parsed and the codecs are remembered per (M1 id, guide number,
GuideName) in a small JSON cache file. No extra tunes, no probing, no tuner
lock: the learner only looks at packets that are being streamed anyway, stops
after the first PMT with elementary streams (or a small packet budget) and
never raises into the streaming path. Channels nobody has tuned yet simply
carry no codec fields (nothing is guessed).
"""
import hashlib
import json
import os
import threading
import time

import tsparse

VERSION = 1
MAX_ENTRIES = 2000        # bound on cache size (oldest evicted first)
MAX_FILE_BYTES = 1 << 20  # refuse to load anything bigger than 1 MiB
BUDGET_PKTS = 20000       # packets inspected per stream before giving up

VIDEO_TYPES = {0x01: "MPEG2", 0x02: "MPEG2", 0x1B: "H264", 0x24: "HEVC"}
AUDIO_TYPES = {0x81: "AC3", 0x87: "EAC3", 0x0F: "AAC", 0x11: "AAC",
               0x03: "MPEG", 0x04: "MPEG"}
VALID_VIDEO = frozenset(VIDEO_TYPES.values())
VALID_AUDIO = frozenset(AUDIO_TYPES.values()) | {"AC4"}
_REG = {b"AC-3": "AC3", b"EAC3": "EAC3", b"AC-4": "AC4"}


def _private_audio(desc):
    """Label for a stream_type 0x06 (PES private data) ES from its descriptors."""
    q = 0
    while q + 2 <= len(desc):
        tag, ln = desc[q], desc[q + 1]
        body = desc[q + 2:q + 2 + ln]
        if tag == 0x6A:                       # DVB AC-3
            return "AC3"
        if tag == 0x7A:                       # DVB enhanced AC-3
            return "EAC3"
        if tag == 0x05 and body[:4] in _REG:  # registration descriptor
            return _REG[body[:4]]
        if tag == 0x7F and body[:1] == b"\x15":   # DVB extension: AC-4
            return "AC4"
        q += 2 + ln
    return None


def pmt_codecs(buf):
    """(video, audio) labels from the first PMT in ``buf`` that has any ES.
    Either may be None (unknown stream type or absent). Never raises."""
    try:
        for tid, sec in tsparse.sections(buf):
            if tid != 0x02 or len(sec) < 16:
                continue
            plen = ((sec[10] & 0x0F) << 8) | sec[11]
            q = 12 + plen
            end = len(sec) - 4            # CRC
            video = audio = None
            seen = False
            while q + 5 <= end:
                st = sec[q]
                elen = ((sec[q + 3] & 0x0F) << 8) | sec[q + 4]
                desc = sec[q + 5:q + 5 + elen]
                q += 5 + elen
                if st in VIDEO_TYPES:
                    seen = True
                    video = video or VIDEO_TYPES[st]
                elif st in AUDIO_TYPES:
                    seen = True
                    audio = audio or AUDIO_TYPES[st]
                elif st == 0x06:
                    lab = _private_audio(desc)
                    if lab:
                        seen = True
                        audio = audio or lab
                elif st:
                    seen = True
            if seen:
                return video, audio
    except Exception:
        pass
    return None, None


def _key(m1, gn, name):
    return "%s|%s|%s" % (m1 or "", gn, name)


class CodecCache:
    """Thread-safe, persisted {(m1, guide number, GuideName): codecs}."""

    def __init__(self, path=None, clock=time.time, max_entries=MAX_ENTRIES):
        self.path = path
        self._clock = clock
        self._max = max_entries
        self._lk = threading.Lock()
        self._io = threading.Lock()
        self._e = {}
        self._token = ""
        self._saving = False
        self._load()

    # -- persistence -------------------------------------------------------
    @staticmethod
    def _clean(d):
        out = {}
        if not isinstance(d, dict) or d.get("version") != VERSION:
            return out
        ents = d.get("entries")
        if not isinstance(ents, dict):
            return out
        for k, v in ents.items():
            if not isinstance(v, dict) or not isinstance(k, str) or k.count("|") < 2:
                continue
            e = {}
            if v.get("VideoCodec") in VALID_VIDEO:
                e["VideoCodec"] = v["VideoCodec"]
            if v.get("AudioCodec") in VALID_AUDIO:
                e["AudioCodec"] = v["AudioCodec"]
            if not e:
                continue
            at = v.get("at")
            e["at"] = float(at) if isinstance(at, (int, float)) and at >= 0 else 0.0
            out[k] = e
        return out

    def _read_file(self):
        if not self.path:
            return {}
        try:
            if os.path.getsize(self.path) > MAX_FILE_BYTES:
                return {}
            with open(self.path) as fh:
                return self._clean(json.load(fh))
        except Exception:
            return {}

    def _load(self):
        self._e = self._trim(self._read_file())
        self._retoken()

    def _trim(self, e):
        if len(e) > self._max:
            keep = sorted(e.items(), key=lambda kv: kv[1]["at"], reverse=True)
            e = dict(keep[:self._max])
        return e

    def _retoken(self):
        h = hashlib.sha1()
        for k in sorted(self._e):
            v = self._e[k]
            h.update(("%s=%s,%s;" % (k, v.get("VideoCodec", ""),
                                     v.get("AudioCodec", ""))).encode())
        self._token = h.hexdigest()[:8]

    def save(self):
        """Atomic write, merged with whatever another facade process (another
        M1) wrote meanwhile. Returns True on success. Never raises."""
        if not self.path:
            return False
        try:
            with self._io:
                disk = self._read_file()
                with self._lk:
                    for k, v in self._e.items():
                        if k not in disk or v["at"] >= disk[k]["at"]:
                            disk[k] = dict(v)
                    disk = self._trim(disk)
                    self._e = dict(disk)
                    self._retoken()
                tmp = "%s.%d.tmp" % (self.path, os.getpid())
                d = os.path.dirname(self.path)
                if d:
                    os.makedirs(d, exist_ok=True)
                with open(tmp, "w") as fh:
                    json.dump({"version": VERSION, "entries": disk}, fh,
                              sort_keys=True, separators=(",", ":"))
                os.replace(tmp, self.path)
            return True
        except Exception:
            return False

    def save_async(self):
        """Persist off the caller's thread (never blocks streaming)."""
        with self._lk:
            if self._saving:
                return
            self._saving = True

        def run():
            try:
                self.save()
            finally:
                with self._lk:
                    self._saving = False
        try:
            threading.Thread(target=run, daemon=True, name="codec-save").start()
        except Exception:
            with self._lk:
                self._saving = False

    # -- API ---------------------------------------------------------------
    def record(self, m1, gn, name, video, audio):
        """Remember what a PMT said. Only fields that were identified are
        stored; returns True if the cache changed."""
        e = {}
        if video in VALID_VIDEO:
            e["VideoCodec"] = video
        if audio in VALID_AUDIO:
            e["AudioCodec"] = audio
        if not e:
            return False
        k = _key(m1, gn, name)
        with self._lk:
            old = self._e.get(k)
            if old and all(old.get(f) == v for f, v in e.items()):
                return False
            new = dict(old or {})
            new.update(e)
            new["at"] = self._clock()
            self._e[k] = new
            self._e = self._trim(self._e)
            self._retoken()
        return True

    def lookup(self, m1, gn, name):
        """{"VideoCodec":..,"AudioCodec":..} (only learned fields) or {}."""
        with self._lk:
            v = self._e.get(_key(m1, gn, name))
            return {f: v[f] for f in ("VideoCodec", "AudioCodec") if f in v} if v else {}

    def lookup_merged(self, m1s, gn, name):
        """For a merged row served by several M1s: a field is reported only if
        every candidate that learned it agrees (disagreement -> omitted)."""
        out, bad = {}, set()
        for m1 in m1s:
            for f, val in self.lookup(m1, gn, name).items():
                if f in out and out[f] != val:
                    bad.add(f)
                out.setdefault(f, val)
        return {f: v for f, v in out.items() if f not in bad}

    def token(self):
        with self._lk:
            return self._token

    def __len__(self):
        with self._lk:
            return len(self._e)


class StreamLearner:
    """Fed the packets of one stream; learns once, then costs one comparison.
    PAT (PID 0) names the PMT PID(s); only those packets are parsed."""

    def __init__(self, cache, m1, gn, name, budget=BUDGET_PKTS):
        self.cache, self.m1, self.gn, self.name = cache, m1, gn, name
        self.left = budget
        self.done = False
        self.pmt_pids = set()

    def feed(self, data):
        if self.done:
            return
        try:
            n = len(data)
            self.left -= max(1, n // 188)
            if self.left < 0:
                self.done = True
                return
            for i in range(0, n - 187, 188):
                if data[i] != 0x47 or not (data[i + 1] & 0x40):
                    continue
                pid = ((data[i + 1] & 0x1F) << 8) | data[i + 2]
                if pid == 0:
                    self._pat(data[i:i + 188])
                elif pid in self.pmt_pids:
                    v, a = pmt_codecs(data[i:i + 188])
                    if v is not None or a is not None:
                        self.done = True
                        if self.cache.record(self.m1, self.gn, self.name, v, a):
                            self.cache.save_async()
                        return
        except Exception:
            self.done = True

    def _pat(self, pkt):
        for tid, sec in tsparse.sections(pkt):
            if tid != 0:
                continue
            q = 8
            while q + 4 <= len(sec) - 4:
                prog = (sec[q] << 8) | sec[q + 1]
                if prog:
                    self.pmt_pids.add(((sec[q + 2] & 0x1F) << 8) | sec[q + 3])
                q += 4
