"""ATSC 3.0: the 999.1 HLS window serving fMP4 (HEVC + AC-4), and the
longer, better-logged api9 wait for 3.0 channels on the BRP path.

The media playlists in fixtures/atsc3_*.m3u8 are a tester's real M1 output
(no addresses in them); init/media segments are synthetic (fmp4synth).
Run: python3 -m unittest tests.test_atsc3   (or discover -s tests)
"""
import contextlib
import io
import os
import socket
import struct
import sys
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import brptune as bt
import fmp4synth as F
import fmp4ts
import hlstuner
import test_ctrl_leg as tc

FIX = os.path.join(HERE, "fixtures")
CH3_21 = "0x3200000000002001"
CH3_41 = "0x3200000000004001"
CH1_21 = "0x1130000000002001"


def fx(name):
    with open(os.path.join(FIX, name)) as fh:
        return fh.read()


def ts_packets(buf):
    return [buf[i:i + 188] for i in range(0, len(buf), 188)]


def pid_of(p):
    return ((p[1] & 0x1F) << 8) | p[2]


def pmt_streams(pkts):
    """{pid: (stream_type, descriptors)} from the first PMT packet."""
    for p in pkts:
        if pid_of(p) == bt.OUT_PMT and p[1] & 0x40:
            sec = p[5:]
            slen = ((sec[1] & 0x0F) << 8) | sec[2]
            end = 3 + slen - 4
            pil = ((sec[10] & 0x0F) << 8) | sec[11]
            i, out = 12 + pil, {}
            while i + 5 <= end:
                st = sec[i]
                ep = ((sec[i + 1] & 0x1F) << 8) | sec[i + 2]
                el = ((sec[i + 3] & 0x0F) << 8) | sec[i + 4]
                out[ep] = (st, bytes(sec[i + 5:i + 5 + el]))
                i += 5 + el
            return out
    return None


class PlaylistParsing(unittest.TestCase):
    def test_master_is_fmp4_with_default_english_audio(self):
        m = hlstuner.parse_master(fx("atsc3_master.m3u8"))
        self.assertTrue(m["fmp4"])
        self.assertEqual(m["video"], "video.m3u8")
        self.assertEqual(m["audio"], "audio1.m3u8")
        self.assertEqual(m["audio_lang"], "eng")
        self.assertIn("hvc1.2.4.L123.B0", m["codecs"])
        self.assertIn("ac-4.02.01.01", m["codecs"])

    def test_atsc1_style_master_is_not_fmp4(self):
        m = hlstuner.parse_master("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\n"
                                  "video.m3u8\n")
        self.assertFalse(m["fmp4"])

    def test_video_playlist_skips_garbled_hash_lines(self):
        pl = hlstuner.parse_media(fx("atsc3_video.m3u8"))
        self.assertEqual(pl["map"], "xix_1737660161018item-01item_init.m4i")
        names = [n for _s, n in pl["segments"]]
        self.assertEqual(len(names), 149)
        self.assertTrue(all(n.startswith("v3p_0010_") for n in names), names[:3])
        self.assertFalse([n for n in names if "XTINF" in n or n.startswith(("#", "p_"))])
        self.assertEqual(pl["segments"][0], (28304, "v3p_0010_0028304"))
        self.assertEqual(pl["segments"][-1][0], 28452)
        seqs = [s for s, _n in pl["segments"]]
        self.assertEqual(seqs, sorted(seqs))

    def test_audio_playlist(self):
        pl = hlstuner.parse_media(fx("atsc3_audio1.m3u8"))
        self.assertEqual(pl["map"], "xix_1737660161018item-02item_init.m4i")
        self.assertTrue(all(n.startswith("a3p_0020_") for _s, n in pl["segments"]))
        self.assertEqual(pl["segments"][0][0], 28312)

    def test_fmp4_detection(self):
        self.assertTrue(hlstuner.is_fmp4_playlist(fx("atsc3_video.m3u8")))
        self.assertFalse(hlstuner.is_fmp4_playlist(
            "#EXTM3U\n#EXTINF:2.0,\nv1p_0010_1\n"))
        files = {"master.m3u8": fx("atsc3_master.m3u8").encode()}
        kind, info = hlstuner.detect("http://h/", lambda u, timeout=10: files[u.rsplit("/", 1)[1]])
        self.assertEqual(kind, "fmp4")
        ts_files = {"master.m3u8": b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nvideo.m3u8\n",
                    "video.m3u8": b"#EXTM3U\n#EXTINF:2.0,\nv1p_0010_1\n"}
        kind, info = hlstuner.detect("http://h/", lambda u, timeout=10: ts_files[u.rsplit("/", 1)[1]])
        self.assertEqual((kind, info), ("ts", None))
        # no master, but the media playlist has EXT-X-MAP
        only = {"video.m3u8": fx("atsc3_video.m3u8").encode()}

        def get(u, timeout=10):
            n = u.rsplit("/", 1)[1]
            if n not in only:
                raise IOError("404")
            return only[n]
        self.assertEqual(hlstuner.detect("http://h/", get)[0], "fmp4")

    def test_atsc1_parser_unchanged(self):
        # the 1.0 path still keeps and repairs the '##' names
        names = hlstuner.playlist_names("##XTINF:2.0,\n##p_0010_5\n#EXTINF:2.0,\nv1p_0010_6\n")
        self.assertEqual(names, ["XTINF:2.0,", "p_0010_5", "v1p_0010_6"])


class Fmp4Parsing(unittest.TestCase):
    def test_init_video_and_audio(self):
        tv = fmp4ts.parse_init(F.init("video", b"hvc1", tid=1, timescale=90000))
        self.assertEqual(tv[1].kind, "video")
        self.assertEqual(tv[1].codec, "hvc1")
        self.assertEqual(tv[1].timescale, 90000)
        self.assertEqual(tv[1].nal_len, 4)
        self.assertEqual([p[4] >> 1 & 0x3F for p in tv[1].param_sets], [32, 33, 34])
        ta = fmp4ts.parse_init(F.init("audio", b"ac-4", tid=2, timescale=48000))
        self.assertEqual((ta[2].kind, ta[2].codec, ta[2].timescale), ("audio", "ac-4", 48000))
        self.assertTrue(ta[2].dsi)

    def test_fragment_samples(self):
        tv = fmp4ts.parse_init(F.init("video", b"hvc1", tid=1))
        frag = F.fragment([(F.hevc_sample(19), 3003, True, 3003),
                           (F.hevc_sample(1, fill=0x11), 3003, False, 0)],
                          tid=1, base_dts=900000)
        s = fmp4ts.parse_fragment(frag, tv)
        self.assertEqual([x.dts for x in s], [900000, 903003])
        self.assertEqual([x.sync for x in s], [True, False])
        self.assertEqual(s[0].cto, 3003)
        self.assertEqual(s[1].data, F.hevc_sample(1, fill=0x11))

    def test_no_moov_is_an_error(self):
        with self.assertRaises(fmp4ts.Fmp4Error):
            fmp4ts.parse_init(b"\x00\x00\x00\x08ftyp")

    def test_annexb_params_only_on_sync(self):
        t = fmp4ts.parse_init(F.init("video", b"hvc1", tid=1))[1]
        aud = F.hevc_sample(35, size=1)
        k = fmp4ts.hevc_annexb(t, aud + F.hevc_sample(19), True)
        p = fmp4ts.hevc_annexb(t, F.hevc_sample(1), False)
        self.assertTrue(k.startswith(b"\x00\x00\x00\x01\x46\x01\x50"))
        types = lambda b: [x[0] >> 1 & 0x3F for x in b.split(b"\x00\x00\x00\x01")[1:]]
        self.assertEqual(types(k), [35, 32, 33, 34, 19])
        self.assertEqual(types(p), [35, 1])

    def test_ac4_sync_frame(self):
        self.assertEqual(fmp4ts.ac4_sync_frame(b"ab"), b"\xac\x40\x00\x02ab")
        big = b"x" * 0x10000
        self.assertEqual(fmp4ts.ac4_sync_frame(big)[:7], b"\xac\x40\xff\xff\x01\x00\x00")


class Muxing(unittest.TestCase):
    def _mux(self, lead_nonsync=False):
        tv = fmp4ts.parse_init(F.init("video", b"hvc1", tid=1))
        ta = fmp4ts.parse_init(F.init("audio", b"ac-4", tid=1, timescale=48000))
        vs = [(F.hevc_sample(1), 3003, False, 0)] if lead_nonsync else []
        vs += [(F.hevc_sample(19 if i == 0 else 1), 3003, i == 0, 3003) for i in range(20)]
        vf = F.fragment(vs, tid=1)
        af = F.fragment([(b"\x40" * 200, 2048, True, 0) for _ in range(30)], tid=1)
        m = fmp4ts.Muxer(log=lambda s: None)
        m.declare(tv)
        m.declare(ta)
        return m, b"".join(m.mux(fmp4ts.parse_fragment(vf, tv) + fmp4ts.parse_fragment(af, ta)))

    def test_pmt_hevc_and_ac4(self):
        _m, out = self._mux()
        pkts = ts_packets(out)
        self.assertTrue(all(len(p) == 188 and p[0] == 0x47 for p in pkts))
        st = pmt_streams(pkts)
        self.assertEqual(st[fmp4ts.VIDEO_PID][0], 0x24)
        self.assertEqual(st[fmp4ts.AUDIO_PID][0], 0x06)
        self.assertIn(b"\x05\x04AC-4", st[fmp4ts.AUDIO_PID][1])
        self.assertIn(b"\x7f\x02\x15", st[fmp4ts.AUDIO_PID][1])

    def test_continuity_and_pcr(self):
        _m, out = self._mux()
        last, first_video = {}, None
        for p in ts_packets(out):
            pid = pid_of(p)
            cc = p[3] & 0x0F
            if pid in last:
                self.assertEqual(cc, (last[pid] + 1) & 0x0F, pid)
            last[pid] = cc
            if pid == fmp4ts.VIDEO_PID and first_video is None:
                first_video = p
        self.assertTrue(first_video[3] & 0x20)          # adaptation field
        self.assertTrue(first_video[5] & 0x10)          # PCR
        self.assertTrue(first_video[5] & 0x40)          # random access
        self.assertTrue(first_video[1] & 0x40)          # PES start

    def test_video_starts_on_sync_sample(self):
        _m, out = self._mux(lead_nonsync=True)
        for p in ts_packets(out):
            if pid_of(p) == fmp4ts.VIDEO_PID:
                self.assertTrue(p[5] & 0x40)
                break

    def test_audio_pes_carries_sync_frame(self):
        _m, out = self._mux()
        i = out.find(b"\x00\x00\x01\xbd")
        self.assertGreater(i, 0)
        hl = out[i + 8]
        self.assertEqual(out[i + 9 + hl:i + 11 + hl], b"\xac\x40")


class HlsTunerFmp4(unittest.TestCase):
    def _files(self):
        files = {"master.m3u8": fx("atsc3_master.m3u8").encode(),
                 "vinit": F.init("video", b"hvc1", tid=1),
                 "ainit": F.init("audio", b"ac-4", tid=1, timescale=48000)}
        vpl = ('#EXTM3U\n#EXT-X-MAP:URI="vinit"\n##XTINF:2.0020,\n##p_0010_0000009\n')
        apl = ('#EXTM3U\n#EXT-X-MAP:URI="ainit"\n##XTINF:2.0020,\n##p_0020_0000017\n')
        for i in range(2):
            files["v3p_0010_%07d" % (10 + i)] = F.fragment(
                [(F.hevc_sample(19 if k == 0 else 1), 3003, k == 0, 0) for k in range(60)],
                tid=1, base_dts=i * 180180)
            files["a3p_0020_%07d" % (18 + i)] = F.fragment(
                [(b"\x40" * 100, 2048, True, 0) for _ in range(47)],
                tid=1, base_dts=i * 96256)
            vpl += "#EXTINF:2.0020,\nv3p_0010_%07d\n" % (10 + i)
            apl += "#EXTINF:2.0020,\na3p_0020_%07d\n" % (18 + i)
        files["video.m3u8"], files["audio1.m3u8"] = vpl.encode(), apl.encode()
        return files

    def test_end_to_end(self):
        files = self._files()
        asked = []

        def get(u, timeout=10):
            n = u.rsplit("/", 1)[1]
            asked.append(n)
            if n not in files:
                raise IOError("404 %s" % n)
            return files[n]
        logs, kinds = [], []
        orig = hlstuner.time.sleep
        hlstuner.time.sleep = lambda s: None
        try:
            t = hlstuner.HlsTuner("3", "1", fresh=False, stalls=1, get=get,
                                  log=logs.append, on_kind=kinds.append)
            out = b"".join(t.packets())
        finally:
            hlstuner.time.sleep = orig
        self.assertEqual(kinds, ["fmp4"])
        self.assertTrue([x for x in logs if x.startswith("hls3:") and "fMP4 (ATSC 3.0)" in x], logs)
        self.assertEqual(t.nseg, 2)
        self.assertFalse([n for n in asked if "p_00" in n and not n.startswith(("v3p", "a3p"))], asked)
        self.assertNotIn("audio2.m3u8", asked)
        pids = {pid_of(p) for p in ts_packets(out)}
        self.assertTrue({fmp4ts.VIDEO_PID, fmp4ts.AUDIO_PID} <= pids)

    def test_row_follows_served_kind(self):
        import hdhr_emulator as h
        before = h.HLS_CHANNEL["protocol"]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                h.hls_seen_kind("fmp4")
            self.assertEqual(h.HLS_CHANNEL["protocol"], "ATSC3")
            self.assertEqual(h.proto_num(h.HLS_CHANNEL), "3")
            self.assertEqual(h.HLS_CHANNEL["chan_major"], 999)
            with contextlib.redirect_stdout(io.StringIO()):
                h.hls_seen_kind("ts")
            self.assertEqual(h.HLS_CHANNEL["protocol"], "ATSC1")
        finally:
            h.HLS_CHANNEL["protocol"] = before


class Atsc3Tune(unittest.TestCase):
    def test_standard_from_chan_id(self):
        self.assertEqual(bt.atsc_standard(CH3_21), 3)
        self.assertEqual(bt.atsc_standard(int(CH3_41, 16)), 3)
        self.assertEqual(bt.atsc_standard(CH1_21), 1)
        self.assertIsNone(bt.atsc_standard("hls:1"))
        self.assertEqual((int(CH3_41, 16) >> 12) & 0xFFF, 4)

    def test_wait_by_nibble(self):
        self.assertEqual(bt.tune_wait(CH3_21), bt.ATSC3_TUNE_WAIT)
        self.assertEqual(bt.tune_wait(CH1_21), bt.TUNE_WAIT)
        self.assertEqual(bt.TUNE_WAIT, 8.0)
        self.assertEqual(bt.ATSC3_TUNE_WAIT, 25.0)

    def _tuner(self, reply):
        t = tc.new_tuner()
        t.slots[0].inst = 3
        calls = []

        def call(api, a0=3, a1=0, a2=0, a3=0, payload=None, timeout=8.0, dev=None):
            calls.append((api, timeout))
            if api == 9:
                t.last_raw = {"api": 9, "nbytes": 0, "partial": ""}
                return reply
            return None
        t.call = call
        return t, calls

    def test_unanswered_atsc3_tune(self):
        t, calls = self._tuner(None)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertIsNone(t.tune(CH3_41, gap=0))
        log = buf.getvalue()
        self.assertEqual([c for c in calls if c[0] == 9], [(9, 25.0)])
        self.assertIn("waiting up to 25s for api9", log)
        self.assertIn("api9 after", log)
        self.assertIn("no reply bytes", log)
        self.assertIn("ATSC 3.0 tune not answered by M1 within 25s", log)
        self.assertIn("push conns seen during tune=0", log)
        self.assertEqual(t.last_fail["reason"], "tune_failures")
        self.assertEqual(t.last_fail["detail"], "ATSC 3.0 tune not answered by M1 within 25s")
        self.assertFalse(t.slots[0].reserved)

    def test_atsc3_error_reply_logged_raw(self):
        r = {"err_name": "NO_SIGNAL", "a0": 7, "a1": 0, "a2": 0, "a3": 0,
             "len1": 0, "len2": 0, "body": b'{"x": 1}\x00'}
        t, calls = self._tuner(r)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertIsNone(t.tune(CH3_21, gap=0))
        self.assertIn('err_name=NO_SIGNAL a0=7', buf.getvalue())
        self.assertIn('body=\'{"x": 1}\'', buf.getvalue())

    def test_atsc1_tune_unchanged(self):
        t, calls = self._tuner(None)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertIsNone(t.tune(CH1_21, gap=0))
        self.assertEqual([c for c in calls if c[0] == 9], [(9, 8.0)] * 3)
        self.assertNotIn("atsc3", buf.getvalue())
        self.assertNotIn("detail", t.last_fail)

    def test_reply_summary_partial(self):
        s = bt.reply_summary(None, {"nbytes": 12, "partial": "ab" * 12})
        self.assertIn("partial header 12/64", s)

    def test_payload_format(self):
        self.assertEqual(bt.payload_format(b"\x47" + b"\x00" * 187 + b"\x47"), "ts")
        self.assertEqual(bt.payload_format(b"\x00\x00\x00\x18ftypiso6"), "fmp4")
        self.assertEqual(bt.payload_format(b"\x00\x00\x01\x00moof"), "fmp4")
        self.assertEqual(bt.payload_format(b"Data\x01\x01"), "data-framed")
        self.assertEqual(bt.payload_format(b"\x01\x02\x03"), "other")

    def test_push_peek_flags_fmp4(self):
        t = tc.new_tuner()
        a, b = socket.socketpair()
        hdr = b"Data" + b"\x01\x01\x01\x01" + b"\x00" * 56 + struct.pack(">II", 0, 24)
        a.sendall(hdr + b"\x00\x00\x00\x18ftypiso6" + b"\x00" * 12)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            t._a3_push_peek(b, CH3_21)
        log = buf.getvalue()
        self.assertIn("format=data-framed", log)
        self.assertIn("body=fmp4", log)
        self.assertIn("is fMP4 (ftyp/moof), not MPEG-TS", log)
        # peek must not consume: the normal path still reads the header
        self.assertEqual(b.recv(4), b"Data")
        a.close()
        b.close()


if __name__ == "__main__":
    unittest.main()
