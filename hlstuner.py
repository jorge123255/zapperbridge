#!/usr/bin/env python3
"""The M1's fourth tuner: the tuner it keeps for itself, served as an HLS window.

The M1 has four physical tuners. Three are leased over BRP (instances 3, 4 and 5)
and retuned with api9; the fourth stays with the M1 and is published as a live HLS
window on :8076 at /atsc{proto}/{slot}/. Nothing we found retunes that window --
there is no HTTP tune path and api9/api10 reject instances 1 and 2 -- so it is one
fixed channel (whatever the M1 itself is watching), not a switchable tuner.

The M1's own PSI is nonstandard: the PMT carries a stray zero byte before the
PCR_PID field and both PAT and PMT declare a section_length one byte too long. We
read the elementary-stream PIDs tolerantly and rebuild our own PAT/PMT with
monotonic continuity counters, so the output is one clean single-program mux
rather than a concatenation of per-segment muxes.
"""
import os
import re
import sys
import time
import urllib.request

import fmp4ts
from brptune import OUT_PAT, OUT_PMT, PSI_SEC, mk_pat, mk_pmt, psi_packet

M1 = os.environ.get("ZM1", "")
HLS_PORT = int(os.environ.get("M1_HLS_PORT", "8076"))


def base(proto="1", slot="1"):
    return "http://%s:%d/atsc%s/%s/" % (M1, HLS_PORT, proto, slot)


def _get(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def playlist_names(text):
    """Segment names from an M1 playlist.

    The M1 sometimes writes its tags as ##XTINF and drops the v1/a1 prefix from
    the advertised name; lines starting with '#' would be skipped as comments by a
    strict parser, so keep the mangled ones and repair them in seg_name().
    """
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if ln.startswith("##"):
            out.append(ln.lstrip("#"))
        elif not ln.startswith("#"):
            out.append(ln)
    return out


def seg_name(name, kind):
    """Repair an advertised name: the served file is '<kind>p_<prog>_<seq>'."""
    n = name.lstrip("#")
    if re.match(r"^p_\d+_\d+$", n):
        return kind + n
    return n


def seq_of(name):
    m = re.search(r"(\d+)$", name.lstrip("#"))
    return int(m.group(1)) if m else 0


def _packets(seg):
    """188-byte packets, resyncing if a fetch came back short."""
    i, n = 0, len(seg)
    while i + 188 <= n:
        if seg[i] != 0x47:
            i += 1
            continue
        yield seg[i:i + 188]
        i += 188


def es_map(seg):
    """({pid: stream_type}, pcr_pid) from the PMT in one segment.

    The M1's PMT carries two stray zero bytes between the version byte and
    the reserved/PCR_PID field; section_length counts them, so bound every
    walk by 3 + section_length and skip the zeros before reading PCR_PID.
    """
    es, pcr = {}, None
    for p in _packets(seg):
        pid = ((p[1] & 0x1F) << 8) | p[2]
        afc = (p[3] >> 4) & 3
        off = 4
        if afc & 2:
            off += 1 + p[off]
        if not (afc & 1) or not (p[1] & 0x40) or off >= 184:
            continue
        sec = p[off + 1 + p[off]:]
        if len(sec) < 12 or sec[0] != 0x02:
            continue
        slen = ((sec[1] & 0x0F) << 8) | sec[2]
        end = 3 + slen - 4
        if end <= 12:
            continue
        i = 6
        while i < end - 4 and sec[i] == 0:
            i += 1
        if i + 2 > end:
            continue
        q = ((sec[i] & 0x1F) << 8) | sec[i + 1]
        if pcr is None:
            pcr = q
        i += 2
        if i + 2 > end:
            continue
        i += 2 + (((sec[i] & 0x0F) << 8) | sec[i + 1])
        while i + 5 <= end:
            st = sec[i]
            ep = ((sec[i + 1] & 0x1F) << 8) | sec[i + 2]
            el = ((sec[i + 3] & 0x0F) << 8) | sec[i + 4]
            if st in (0, 0xFF) or ep in (0, OUT_PAT, OUT_PMT) or ep >= 0x1FFF:
                break
            es[ep] = st
            i += 5 + el
    return es, pcr


# ---- ATSC 3.0: the same window serving fMP4 (CMAF) instead of MPEG-TS ----

_ATTR = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def _attrs(line):
    return {k: v.strip('"') for k, v in _ATTR.findall(line.split(":", 1)[-1])}


def parse_master(text):
    """What a master playlist offers: {'fmp4', 'codecs', 'video', 'audio',
    'audio_lang'}. 'audio' is the DEFAULT=YES audio rendition (else the
    first); fmp4 is True when the codecs are HEVC/AC-4 (ATSC 3.0)."""
    info = {"fmp4": False, "codecs": [], "video": None, "audio": None,
            "audio_lang": None}
    auds, pending = [], None
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if ln.startswith("#EXT-X-MEDIA:"):
            a = _attrs(ln)
            if a.get("TYPE") == "AUDIO" and a.get("URI"):
                auds.append(a)
        elif ln.startswith("#EXT-X-STREAM-INF:"):
            pending = _attrs(ln)
        elif ln.startswith("#EXT-X-MAP"):
            info["fmp4"] = True
        elif not ln.startswith("#") and pending is not None:
            codecs = [c.strip() for c in pending.get("CODECS", "").split(",") if c.strip()]
            info["codecs"] += codecs
            if info["video"] is None and any(
                    c.startswith(("hvc1", "hev1", "avc1", "avc3")) for c in codecs):
                info["video"] = ln
            pending = None
    if any(c.startswith(("hvc1", "hev1", "ac-4")) for c in info["codecs"]):
        info["fmp4"] = True
    if auds:
        d = next((a for a in auds if a.get("DEFAULT") == "YES"), auds[0])
        info["audio"], info["audio_lang"] = d["URI"], d.get("LANGUAGE")
    return info


def parse_media(text):
    """{'map': init URI or None, 'segments': [(seq, uri)]} for an fMP4 media
    playlist. The M1 writes some leading entries garbled ('##XTINF:',
    '##p_0010_...'); every line starting with '#' is a tag or garbage, never a
    segment, so they are skipped (the clean entries that follow are the live
    edge)."""
    out = {"map": None, "segments": []}
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if ln.startswith("#"):
            m = re.search(r'X-MAP:.*URI="([^"]+)"', ln)
            if m and ln.startswith("#EXT-X-MAP"):
                out["map"] = m.group(1)
            continue
        m = re.findall(r"\d+", ln)
        out["segments"].append((int(m[-1]) if m else 0, ln))
    return out


def is_fmp4_playlist(text):
    """True when a media playlist carries an EXT-X-MAP init (fMP4)."""
    return any(ln.strip().startswith("#EXT-X-MAP") for ln in text.splitlines())


def detect(base_url, get=None):
    """('fmp4', master_info) or ('ts', None) for an M1 HLS window."""
    get = get or _get
    try:
        info = parse_master(get(base_url + "master.m3u8").decode("utf-8", "replace"))
        if info["fmp4"]:
            return "fmp4", info
    except Exception:
        pass
    try:
        if is_fmp4_playlist(get(base_url + "video.m3u8").decode("utf-8", "replace")):
            return "fmp4", {"fmp4": True, "codecs": [], "video": "video.m3u8",
                            "audio": "audio1.m3u8", "audio_lang": None}
    except Exception:
        pass
    return "ts", None


class _Rendition:
    """One fMP4 media playlist: init cache plus the last sequence sent."""

    def __init__(self, url, kind, get, log):
        self.url, self.kind, self.get, self.log = url, kind, get, log
        self.dir = url.rsplit("/", 1)[0] + "/"
        self.map_uri = None
        self.tracks = None
        self.sent = -1

    def new_segments(self, fresh):
        pl = parse_media(self.get(self.url).decode("utf-8", "replace"))
        if pl["map"] and pl["map"] != self.map_uri:
            self.tracks = fmp4ts.parse_init(self.get(self.dir + pl["map"]))
            self.map_uri = pl["map"]
            self.log("hls3 %s init %s tracks=%s" % (
                self.kind, pl["map"], list(self.tracks.values())))
        if self.tracks is None:
            raise fmp4ts.Fmp4Error("%s playlist has no EXT-X-MAP init" % self.kind)
        segs = [x for x in pl["segments"] if x[0] > self.sent]
        if fresh:
            segs = segs[-1:]
        return segs

    def fetch(self, seg):
        data = self.get(self.dir + seg[1])
        self.sent = seg[0]
        return fmp4ts.parse_fragment(data, self.tracks)


class HlsTuner:
    """One continuous MPEG-TS built from the M1's fixed HLS window."""

    def __init__(self, proto="1", slot="1", fresh=True, stalls=40,
                 on_kind=None, get=None, log=None):
        self.proto = proto
        self.slot = slot
        self.fresh = fresh
        self.stalls = stalls
        self.es = {}
        self.pcr = None
        self.nseg = 0
        self.kind = None
        self.on_kind = on_kind
        self._get = get or _get
        self.log = log or (lambda m: print(m, flush=True))

    def _remux(self, segs, cc, out_cc):
        """Pass the elementary streams through with our own counters and PSI."""
        for seg in segs:
            if not seg:
                continue
            e, p = es_map(seg)
            self.es.update(e)
            if p is not None and self.pcr is None:
                self.pcr = p
        keep = set(self.es)
        if not keep:
            return
        now = time.time()
        if now - self._last_psi >= PSI_SEC:
            self._last_psi = now
            yield psi_packet(OUT_PAT, mk_pat(OUT_PMT), out_cc[OUT_PAT])
            out_cc[OUT_PAT] = (out_cc[OUT_PAT] + 1) & 0x0F
            yield psi_packet(OUT_PMT, mk_pmt(self.pcr or min(keep), self.es),
                             out_cc[OUT_PMT])
            out_cc[OUT_PMT] = (out_cc[OUT_PMT] + 1) & 0x0F
        for seg in segs:
            if not seg:
                continue
            for pkt in _packets(seg):
                pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
                if pid not in keep:
                    continue
                cc[pid] = (cc.get(pid, -1) + 1) & 0x0F
                out = bytearray(pkt)
                out[3] = (out[3] & 0xF0) | cc[pid]
                yield bytes(out)

    def packets(self):
        url = base(self.proto, self.slot)
        kind, info = detect(url, self._get)
        self.kind = kind
        if self.on_kind:
            try:
                self.on_kind(kind)
            except Exception:
                pass
        if kind == "fmp4":
            self.log("hls3: %s serves fMP4 (ATSC 3.0) codecs=%s audio=%s(%s); "
                     "remuxing HEVC/AC-4 into MPEG-TS unchanged" % (
                         url, ",".join(info["codecs"]) or "?",
                         info["audio"], info["audio_lang"]))
            for pkt in self._packets_fmp4(url, info):
                yield pkt
            return
        for pkt in self._packets_ts(url):
            yield pkt

    def _packets_fmp4(self, url, info):
        mux = fmp4ts.Muxer(log=self.log, psi_every=PSI_SEC)
        rends = [_Rendition(url + (info["video"] or "video.m3u8"), "video",
                            self._get, self.log)]
        if info.get("audio"):
            rends.append(_Rendition(url + info["audio"], "audio", self._get, self.log))
        declared = set()
        stalls, fresh = 0, self.fresh
        samples = []
        while stalls < self.stalls:
            got = False
            for r in rends:
                try:
                    segs = r.new_segments(fresh)
                except Exception as e:
                    self.log("hls3 %s playlist: %s" % (r.kind, e))
                    if r.kind == "video":
                        break
                    continue
                if r.map_uri not in declared:
                    declared.add(r.map_uri)
                    mux.declare(r.tracks)
                for seg in segs:
                    try:
                        samples += r.fetch(seg)
                    except Exception as e:
                        self.log("hls3 %s seg %s: %s" % (r.kind, seg[1], e))
                        r.sent = seg[0]
                        continue
                    if r.kind == "video":
                        got = True
                        self.nseg += 1
            if not got:
                stalls += 1
                time.sleep(1.0)
                continue
            stalls, fresh = 0, False
            self.es = dict(mux.es)
            self.pcr = fmp4ts.VIDEO_PID
            batch, samples = samples, []
            for pkt in mux.mux(batch):
                yield pkt

    def _packets_ts(self, url):
        self._last_psi = 0.0
        cc, out_cc = {}, {OUT_PAT: 0, OUT_PMT: 0}
        sent, stalls = 0, 0
        while stalls < self.stalls:
            try:
                vtext = _get(url + "video.m3u8").decode("utf-8", "replace")
            except Exception as e:
                stalls += 1
                print("hls playlist: %s" % (e,), flush=True)
                time.sleep(2)
                continue
            amap = {}
            try:
                amap = {seq_of(n): n for n in
                        playlist_names(_get(url + "audio1.m3u8").decode("utf-8", "replace"))}
            except Exception as e:
                print("hls audio playlist: %s" % (e,), flush=True)
            vsegs = [x for x in ((seq_of(n), n) for n in playlist_names(vtext))
                     if x[0] > sent]
            if not vsegs:
                stalls += 1
                time.sleep(1.5)
                continue
            stalls = 0
            if self.fresh:
                vsegs = vsegs[-1:]
                self.fresh = False
            for seq, name in vsegs:
                try:
                    vseg = _get(url + seg_name(name, "v1"))
                except Exception as e:
                    print("hls seg %s: %s" % (name, e), flush=True)
                    continue
                aseg = None
                an = amap.get(seq)
                if an:
                    try:
                        aseg = _get(url + seg_name(an, "a1"))
                    except Exception as e:
                        print("hls aseg %s: %s" % (an, e), flush=True)
                for pkt in self._remux([vseg, aseg], cc, out_cc):
                    yield pkt
                self.nseg += 1
                sent = seq


if __name__ == "__main__":
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 10
    out = sys.argv[2] if len(sys.argv) > 2 else "hls.ts"
    t = HlsTuner()
    n, t0 = 0, time.time()
    with open(out, "wb") as fh:
        for p in t.packets():
            fh.write(p)
            n += 1
            if time.time() - t0 > secs:
                break
    print("wrote %d packets / %d bytes, %d segments, es=%s pcr=%s" % (
        n, n * 188, t.nseg, t.es, t.pcr), flush=True)
