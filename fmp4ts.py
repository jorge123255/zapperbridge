#!/usr/bin/env python3
"""Fragmented-MP4 (CMAF) to MPEG-TS remux for the M1's ATSC 3.0 HLS window.

When the M1's own tuner sits on an ATSC 3.0 channel, its :8076 HLS window
serves fMP4: an EXT-X-MAP init segment plus 2 s fragments, HEVC video and
AC-4 audio in separate renditions. Clients of an HDHomeRun expect one
MPEG-TS, so this module copies the coded samples, untouched, into TS:

* HEVC: stream_type 0x24. Length-prefixed NAL units become Annex-B start
  codes; an access unit delimiter is added and the hvcC VPS/SPS/PPS are
  repeated before every sync sample (as ffmpeg's hevc_mp4toannexb does).
* AC-4: stream_type 0x06 (PES private_stream_1) with a registration
  descriptor 'AC-4' plus the DVB AC-4 descriptor (extension descriptor
  0x7F, tag 0x15; ETSI EN 300 468 / TS 101 154). ffmpeg 7.1 identifies the
  stream as ac4 from the registration descriptor (the DVB one alone shows
  up as bin_data). Each sample is wrapped in an AC-4 sync frame (sync word
  0xAC40, ETSI TS 103 190-1 Annex G); ZB_AC4_SYNC=0 sends the raw frames
  instead, which is what ``ffmpeg -c copy`` does.

Nothing is decoded or re-encoded. Standard library only.
"""
import os
import struct

from brptune import OUT_PAT, OUT_PMT, mk_pat, mk_pmt, psi_packet

VIDEO_PID = 0x100
AUDIO_PID = 0x101
ST_HEVC = 0x24
ST_PRIVATE = 0x06
#: Registration descriptor 'AC-4', then the DVB extension descriptor (0x7F),
#: length 2, AC-4 descriptor tag (0x15), ac4_config_flag=0 / ac4_toc_flag=0.
AC4_DESC = b"\x05\x04AC-4" + b"\x7f\x02\x15\x00"
#: Output timeline starts here (90 kHz) so PCR, which runs behind DTS, is >= 0.
T_OFFSET = 90000 * 2
PCR_LAG = 90000 * 7 // 10
WRAP = 1 << 33
_AUD = b"\x00\x00\x00\x01\x46\x01\x50"
#: Wrap AC-4 samples in sync frames (default) or send them raw.
AC4_SYNC = os.environ.get("ZB_AC4_SYNC", "1") != "0"


class Fmp4Error(ValueError):
    pass


def boxes(buf, start=0, end=None):
    """(type, payload_start, box_end) for each box in buf[start:end]."""
    end = len(buf) if end is None else end
    i = start
    while i + 8 <= end:
        size, = struct.unpack_from(">I", buf, i)
        typ = bytes(buf[i + 4:i + 8])
        hdr = 8
        if size == 1:
            if i + 16 > end:
                return
            size, = struct.unpack_from(">Q", buf, i + 8)
            hdr = 16
        elif size == 0:
            size = end - i
        if size < hdr or i + size > end:
            return
        yield typ, i + hdr, i + size
        i += size


def find(buf, path, start=0, end=None):
    """First box payload (start, end) along a type path, or None."""
    end = len(buf) if end is None else end
    for typ, s, e in boxes(buf, start, end):
        if typ == path[0]:
            if len(path) == 1:
                return s, e
            r = find(buf, path[1:], s, e)
            if r is not None:
                return r
    return None


class Track:
    def __init__(self, tid):
        self.id = tid
        self.kind = None          # 'video' / 'audio'
        self.codec = None         # sample entry fourcc, e.g. 'hvc1', 'ac-4'
        self.timescale = 90000
        self.nal_len = 4
        self.param_sets = []      # Annex-B VPS/SPS/PPS
        self.dsi = b""
        self.def_dur = 0
        self.def_size = 0
        self.def_flags = 0

    def __repr__(self):
        return "Track(%d %s %s ts=%d)" % (self.id, self.kind, self.codec,
                                          self.timescale)


def _hvcc(buf, s, e):
    """(nal_length_size, [Annex-B parameter set NALs]) from an hvcC box."""
    if e - s < 23:
        raise Fmp4Error("short hvcC")
    nal_len = (buf[s + 21] & 3) + 1
    n = buf[s + 22]
    i, out = s + 23, []
    for _ in range(n):
        if i + 3 > e:
            break
        cnt, = struct.unpack_from(">H", buf, i + 1)
        i += 3
        for _ in range(cnt):
            if i + 2 > e:
                break
            ln, = struct.unpack_from(">H", buf, i)
            out.append(b"\x00\x00\x00\x01" + bytes(buf[i + 2:i + 2 + ln]))
            i += 2 + ln
    return nal_len, out


def parse_init(buf):
    """{track_id: Track} from an init segment (ftyp+moov)."""
    moov = find(buf, [b"moov"])
    if moov is None:
        raise Fmp4Error("no moov in init segment")
    tracks = {}
    for typ, s, e in boxes(buf, *moov):
        if typ != b"trak":
            continue
        tk = find(buf, [b"tkhd"], s, e)
        if tk is None:
            continue
        v = buf[tk[0]]
        tid, = struct.unpack_from(">I", buf, tk[0] + (20 if v == 1 else 12))
        t = Track(tid)
        md = find(buf, [b"mdia", b"mdhd"], s, e)
        if md is not None:
            v = buf[md[0]]
            t.timescale, = struct.unpack_from(">I", buf, md[0] + (20 if v == 1 else 12))
        hd = find(buf, [b"mdia", b"hdlr"], s, e)
        if hd is not None:
            h = bytes(buf[hd[0] + 8:hd[0] + 12])
            t.kind = {b"vide": "video", b"soun": "audio"}.get(h, h.decode("latin-1"))
        sd = find(buf, [b"mdia", b"minf", b"stbl", b"stsd"], s, e)
        if sd is not None and sd[1] - sd[0] >= 16:
            es = sd[0] + 8
            size, = struct.unpack_from(">I", buf, es)
            t.codec = bytes(buf[es + 4:es + 8]).decode("latin-1")
            # children start after the visual (78) / audio (28) entry header
            body = es + 8 + (78 if t.kind == "video" else 28)
            for ct, cs, ce in boxes(buf, body, min(es + size, sd[1])):
                if ct == b"hvcC":
                    t.nal_len, t.param_sets = _hvcc(buf, cs, ce)
                elif ct == b"dac4":
                    t.dsi = bytes(buf[cs:ce])
        tracks[tid] = t
    mv = find(buf, [b"mvex"], *moov)
    if mv is not None:
        for typ, s, e in boxes(buf, *mv):
            if typ == b"trex" and e - s >= 24:
                tid, _sdi, dd, ds, df = struct.unpack_from(">5I", buf, s + 4)
                if tid in tracks:
                    tracks[tid].def_dur, tracks[tid].def_size, tracks[tid].def_flags = dd, ds, df
    if not tracks:
        raise Fmp4Error("no tracks in init segment")
    return tracks


class Sample:
    __slots__ = ("track", "dts", "cto", "data", "sync")

    def __init__(self, track, dts, cto, data, sync):
        self.track, self.dts, self.cto, self.data, self.sync = (
            track, dts, cto, data, sync)


def parse_fragment(buf, tracks):
    """Samples (in track timescale) from one media segment (moof+mdat...)."""
    out = []
    for typ, ms, me in boxes(buf):
        if typ != b"moof":
            continue
        moof_start = ms - 8
        for tt, ts_, te in boxes(buf, ms, me):
            if tt != b"traf":
                continue
            fh = find(buf, [b"tfhd"], ts_, te)
            if fh is None:
                continue
            flags = int.from_bytes(buf[fh[0] + 1:fh[0] + 4], "big")
            tid, = struct.unpack_from(">I", buf, fh[0] + 4)
            t = tracks.get(tid)
            if t is None:
                continue
            i = fh[0] + 8
            base = moof_start
            dd, dsz, dfl = t.def_dur, t.def_size, t.def_flags
            if flags & 0x01:
                base, = struct.unpack_from(">Q", buf, i)
                i += 8
            if flags & 0x02:
                i += 4
            if flags & 0x08:
                dd, = struct.unpack_from(">I", buf, i)
                i += 4
            if flags & 0x10:
                dsz, = struct.unpack_from(">I", buf, i)
                i += 4
            if flags & 0x20:
                dfl, = struct.unpack_from(">I", buf, i)
                i += 4
            dts = 0
            fd = find(buf, [b"tfdt"], ts_, te)
            if fd is not None:
                if buf[fd[0]] == 1:
                    dts, = struct.unpack_from(">Q", buf, fd[0] + 4)
                else:
                    dts, = struct.unpack_from(">I", buf, fd[0] + 4)
            pos = None
            for rt, rs, re_ in boxes(buf, ts_, te):
                if rt != b"trun":
                    continue
                ver = buf[rs]
                rf = int.from_bytes(buf[rs + 1:rs + 4], "big")
                n, = struct.unpack_from(">I", buf, rs + 4)
                j = rs + 8
                if rf & 0x01:
                    off, = struct.unpack_from(">i", buf, j)
                    j += 4
                    pos = base + off
                elif pos is None:
                    pos = base
                first = None
                if rf & 0x04:
                    first, = struct.unpack_from(">I", buf, j)
                    j += 4
                for k in range(n):
                    dur, size, sfl, cto = dd, dsz, dfl, 0
                    if rf & 0x100:
                        dur, = struct.unpack_from(">I", buf, j)
                        j += 4
                    if rf & 0x200:
                        size, = struct.unpack_from(">I", buf, j)
                        j += 4
                    if rf & 0x400:
                        sfl, = struct.unpack_from(">I", buf, j)
                        j += 4
                    if rf & 0x800:
                        cto, = struct.unpack_from(">i" if ver else ">I", buf, j)
                        j += 4
                    if k == 0 and first is not None:
                        sfl = first
                    if pos + size > len(buf):
                        raise Fmp4Error("sample past end of segment")
                    sync = not (sfl & 0x10000)
                    out.append(Sample(t, dts, cto, bytes(buf[pos:pos + size]), sync))
                    pos += size
                    dts += dur
    return out


def hevc_annexb(t, data, sync):
    """One HEVC sample as an Annex-B access unit (AUD, params on sync)."""
    out = [_AUD]
    nals, i, n, L = [], 0, len(data), t.nal_len
    has_vps = False
    while i + L <= n:
        ln = int.from_bytes(data[i:i + L], "big")
        i += L
        nal = data[i:i + ln]
        i += ln
        if not nal:
            continue
        typ = (nal[0] >> 1) & 0x3F
        if typ == 35:
            continue  # we add our own AUD
        if typ == 32:
            has_vps = True
        nals.append(nal)
    if sync and not has_vps:
        out.extend(t.param_sets)
    for nal in nals:
        out.append(b"\x00\x00\x00\x01" + nal)
    return b"".join(out)


def ac4_sync_frame(data):
    """Wrap a raw AC-4 frame (as stored in MP4) in a sync frame (0xAC40)."""
    n = len(data)
    if n < 0xFFFF:
        return b"\xac\x40" + struct.pack(">H", n) + data
    return b"\xac\x40\xff\xff" + n.to_bytes(3, "big") + data


def _ts_stamp(marker, t):
    t %= WRAP
    return bytes([(marker << 4) | (((t >> 30) & 7) << 1) | 1,
                  (t >> 22) & 0xFF, (((t >> 15) & 0x7F) << 1) | 1,
                  (t >> 7) & 0xFF, ((t & 0x7F) << 1) | 1])


def pes(stream_id, payload, pts, dts=None, bounded=True):
    if dts is not None and dts != pts:
        flags, hdr = 0xC0, _ts_stamp(3, pts) + _ts_stamp(1, dts)
    else:
        flags, hdr = 0x80, _ts_stamp(2, pts)
    body = bytes([0x80, flags, len(hdr)]) + hdr + payload
    ln = len(body) if bounded and len(body) <= 0xFFFF else 0
    return b"\x00\x00\x01" + bytes([stream_id]) + struct.pack(">H", ln) + body


def packetize(pid, data, cc, pcr=None, rai=False):
    """TS packets for one PES; returns (packets, next cc)."""
    out, i, first = [], 0, True
    n = len(data)
    while i < n or first:
        af = b""
        if first and (pcr is not None or rai):
            fl = (0x40 if rai else 0) | (0x10 if pcr is not None else 0)
            af = bytes([fl])
            if pcr is not None:
                base = pcr % WRAP
                af += bytes([(base >> 25) & 0xFF, (base >> 17) & 0xFF,
                             (base >> 9) & 0xFF, (base >> 1) & 0xFF,
                             ((base & 1) << 7) | 0x7E, 0x00])
        room = 184 - (len(af) + 1 if af else 0)
        chunk = data[i:i + room]
        if len(chunk) < room:
            # stuff with an adaptation field
            pad = 184 - len(chunk)
            if af:
                af = af + b"\xff" * (pad - 1 - len(af))
            elif pad == 1:
                af = None  # zero-length adaptation field
            else:
                af = b"\x00" + b"\xff" * (pad - 2)
        hdr = bytearray([0x47, (0x40 if first else 0) | (pid >> 8), pid & 0xFF, 0])
        if af is None:
            hdr[3] = 0x30 | cc
            pkt = bytes(hdr) + b"\x00" + chunk
        elif af:
            hdr[3] = 0x30 | cc
            pkt = bytes(hdr) + bytes([len(af)]) + af + chunk
        else:
            hdr[3] = 0x10 | cc
            pkt = bytes(hdr) + chunk
        assert len(pkt) == 188, len(pkt)
        out.append(pkt)
        cc = (cc + 1) & 0x0F
        i += len(chunk)
        first = False
    return out, cc


class Muxer:
    """Continuous single-program TS from fMP4 samples (video + one audio)."""

    def __init__(self, log=print, psi_every=0.1):
        self.log = log
        self.cc = {VIDEO_PID: 0, AUDIO_PID: 0, OUT_PAT: 0, OUT_PMT: 0}
        self.base = {}          # kind -> track-time origin in 90 kHz
        self.es = {}
        self.psi_every = int(psi_every * 90000)
        self._last_psi = None
        self.unsupported = set()
        self.video_started = False

    def _streams(self, kind, t):
        if kind == "video" and t.codec in ("hvc1", "hev1"):
            return VIDEO_PID, ST_HEVC, 0xE0
        if kind == "audio" and t.codec in ("ac-4", "ac4 "):
            return AUDIO_PID, ST_PRIVATE, 0xBD
        return None

    def _t90(self, t, ticks):
        return ticks * 90000 // max(1, t.timescale)

    def _psi(self):
        out = []
        descs = {AUDIO_PID: AC4_DESC} if self.es.get(AUDIO_PID) == ST_PRIVATE else {}
        out.append(psi_packet(OUT_PAT, mk_pat(OUT_PMT), self.cc[OUT_PAT]))
        self.cc[OUT_PAT] = (self.cc[OUT_PAT] + 1) & 0x0F
        out.append(psi_packet(OUT_PMT, mk_pmt(VIDEO_PID if VIDEO_PID in self.es
                                              else min(self.es), self.es,
                                              descs=descs), self.cc[OUT_PMT]))
        self.cc[OUT_PMT] = (self.cc[OUT_PMT] + 1) & 0x0F
        return out

    def declare(self, tracks):
        for t in tracks.values():
            s = self._streams(t.kind, t)
            if s is None:
                if (t.kind, t.codec) not in self.unsupported:
                    self.unsupported.add((t.kind, t.codec))
                    self.log("hls3: %s track codec %r not carried" % (t.kind, t.codec))
                continue
            self.es[s[0]] = s[1]

    def mux(self, samples):
        """TS packets for samples from any track, in DTS order."""
        timed = []
        for s in samples:
            st = self._streams(s.track.kind, s.track)
            if st is None:
                continue
            d = self._t90(s.track, s.dts)
            kind = s.track.kind
            if kind not in self.base:
                other = [b for b in self.base.values()]
                # share the first track's origin unless the timelines are
                # wildly apart (then align each track on its own)
                if other and abs(d - other[0]) < 90000 * 10:
                    self.base[kind] = other[0]
                else:
                    if other:
                        self.log("hls3: %s timeline %.1fs from video; aligning separately"
                                 % (kind, (d - other[0]) / 90000.0))
                    self.base[kind] = d
            timed.append((d - self.base[kind], s, st))
        timed.sort(key=lambda x: x[0])
        out = []
        for d, s, (pid, _st, sid) in timed:
            dts = d + T_OFFSET
            pts = dts + self._t90(s.track, s.cto)
            if self._last_psi is None or dts - self._last_psi >= self.psi_every:
                self._last_psi = dts
                out.extend(self._psi())
            if pid == VIDEO_PID:
                if not self.video_started and not s.sync:
                    continue  # start on a sync sample
                self.video_started = True
                payload = hevc_annexb(s.track, s.data, s.sync)
                pk, self.cc[pid] = packetize(
                    pid, pes(sid, payload, pts, dts, bounded=False), self.cc[pid],
                    pcr=dts - PCR_LAG, rai=s.sync)
            else:
                pk, self.cc[pid] = packetize(
                    pid, pes(sid, ac4_sync_frame(s.data) if AC4_SYNC else s.data, pts), self.cc[pid])
            out.extend(pk)
        return out
