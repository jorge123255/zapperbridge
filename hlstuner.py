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


class HlsTuner:
    """One continuous MPEG-TS built from the M1's fixed HLS window."""

    def __init__(self, proto="1", slot="1", fresh=True, stalls=40):
        self.proto = proto
        self.slot = slot
        self.fresh = fresh
        self.stalls = stalls
        self.es = {}
        self.pcr = None
        self.nseg = 0

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
        self._last_psi = 0.0
        url = base(self.proto, self.slot)
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
