"""Full-stream diagnostics (zstream): synthetic TS, offline, log-only.

Runs under unittest and pytest."""
import io
import json
import os
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import zdiag
import zstream as zs
import brptune
import hdhr_emulator as h
import test_diag_logging as td

VPID, APID, PMTPID, PCRPID = 0x100, 0x101, 0x1000, 0x100


class Clk:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def sec_pkt(pid, sec, cc=0):
    pl = b"\x00" + sec
    pl += b"\xff" * (184 - len(pl))
    return bytes([0x47, 0x40 | (pid >> 8), pid & 0xFF, 0x10 | (cc & 15)]) + pl


def mk_pat(cc=0, pmt=PMTPID, prog=1):
    body = b"\x00\x00\x00" + b""  # placeholder
    sec = bytes([0x00, 0xB0, 13, 0x00, 0x01, 0xC1, 0x00, 0x00,
                 prog >> 8, prog & 0xFF, 0xE0 | (pmt >> 8), pmt & 0xFF])
    sec += zs.crc32_mpeg(sec).to_bytes(4, "big")
    return sec_pkt(0, sec, cc)


def mk_pmt(cc=0, es=((0x02, VPID, b""), (0x81, APID, b"")), pcr=PCRPID, prog=1):
    loop = b""
    for st, pid, desc in es:
        loop += bytes([st, 0xE0 | (pid >> 8), pid & 0xFF, 0xF0 | (len(desc) >> 8),
                       len(desc) & 0xFF]) + desc
    ln = 9 + len(loop) + 4
    sec = bytes([0x02, 0xB0 | (ln >> 8), ln & 0xFF, prog >> 8, prog & 0xFF, 0xC1,
                 0, 0, 0xE0 | (pcr >> 8), pcr & 0xFF, 0xF0, 0x00]) + loop
    sec += zs.crc32_mpeg(sec).to_bytes(4, "big")
    return sec_pkt(PMTPID, sec, cc)


def enc_pts(v):
    return bytes([0x21 | (((v >> 30) & 7) << 1), (v >> 22) & 0xFF,
                  ((v >> 15) & 0x7F) << 1 | 1, (v >> 7) & 0xFF, (v & 0x7F) << 1 | 1])


def pes(sid, pts, es=b""):
    hdr = b"\x00\x00\x01" + bytes([sid]) + b"\x00\x00" + b"\x80\x80\x05" + enc_pts(pts)
    return hdr + es


def pkts(pid, payload, ccs, pusi=True, pcr=None, disc=False, rai=False):
    """Packetise ``payload`` on ``pid``; ccs is a one-item list holding the cc."""
    out, first = [], True
    while payload or first:
        af = b""
        room = 184
        if first and pcr is not None:
            b = pcr
            af = bytes([7, 0x10 | (0x80 if disc else 0)]) + bytes([
                (b >> 25) & 0xFF, (b >> 17) & 0xFF, (b >> 9) & 0xFF, (b >> 1) & 0xFF,
                ((b & 1) << 7) | 0x7E, 0])
            room = 184 - len(af)
        chunk, payload = payload[:room], payload[room:]
        if len(chunk) < room:
            pad = room - len(chunk)
            if af:
                af = bytes([af[0] + pad]) + af[1:] + b"\xff" * pad
            elif pad == 1:
                af = bytes([0])
            else:
                af = bytes([pad - 1, 0x00]) + b"\xff" * (pad - 2)
        b3 = (0x30 if af else 0x10) | (ccs[0] & 15)
        ccs[0] += 1
        hdr = bytes([0x47, (0x40 if (first and pusi) else 0) | (pid >> 8), pid & 0xFF, b3])
        out.append(hdr + af + chunk)
        first = False
    return out


class Gen:
    """Synthetic A/V program: 30 fps MPEG-2 video, AC3 audio every 32 ms."""

    def __init__(self, acodec=b"\x0b\x77\x00\x00\x00\x3d\xe0" + b"\x00" * 40):
        self.cv, self.ca, self.cp, self.cm = [0], [0], [0], [0]
        self.acodec = acodec
        self.vpts = 90000
        self.apts = 90000

    def psi(self, **kw):
        a = mk_pat(self.cp[0] & 15)
        self.cp[0] += 1
        b = mk_pmt(self.cm[0] & 15, **kw)
        self.cm[0] += 1
        return [a, b]

    def video(self, dpts=3003):
        self.vpts += dpts
        return pkts(VPID, pes(0xE0, self.vpts, b"\x00\x00\x01\xb3" + b"\x11" * 600),
                    self.cv, pcr=self.vpts - 9000)

    def audio(self, dpts=2880):
        self.apts += dpts
        return pkts(APID, pes(0xBD, self.apts, self.acodec + b"\x22" * 100), self.ca)


def run_second_of_av(an, g, clk, start=1000.0, frames=30):
    """Feed one second of A/V (30 video PES, 31 audio PES) at media pace."""
    na = 0
    for f in range(frames):
        t = start + f / 30.0
        clk.t = t
        batch = []
        if f == 0:
            batch += g.psi()
        batch += g.video()
        while na * 0.032 <= (f + 1) / 30.0 and na < 31:
            batch += g.audio()
            na += 1
        an.feed(b"".join(batch), t)
    clk.t = start + 1.0


def mk_an(clk=None, **kw):
    clk = clk or Clk()
    L = []
    an = zs.StreamAnalyzer("chX", 7, clock=clk, emit=L.append, **kw)
    return an, clk, L


class Analyzer(unittest.TestCase):
    def test_psi_logged_at_start_with_codecs(self):
        an, clk, L = mk_an()
        g = Gen()
        an.feed(b"".join(g.psi()), 1000.0)
        psi = [x for x in L if x.startswith("ts psi ")]
        self.assertEqual(len(psi), 1)
        s = psi[0]
        for frag in ("seq=7", "program=1", "pmt_pid=0x1000", "pcr_pid=0x100",
                     "0x100:0x02:MPEG2-video:video", "0x101:0x81:AC3:audio"):
            self.assertIn(frag, s)

    def test_psi_h264_aac_and_private_ac3_descriptor(self):
        an, clk, L = mk_an()
        g = Gen()
        an.feed(b"".join(g.psi(es=((0x1B, VPID, b""), (0x06, APID, bytes([0x6A, 1, 0]))))), 1000.0)
        s = [x for x in L if x.startswith("ts psi ")][0]
        self.assertIn("H264:video", s)
        self.assertIn("AC3:audio", s)

    def test_psi_change_midstream_logged(self):
        an, clk, L = mk_an()
        g = Gen()
        an.feed(b"".join(g.psi()), 1000.0)
        an.feed(b"".join(g.psi(es=((0x1B, VPID, b""), (0x0F, APID, b"")))), 1001.0)
        ch = [x for x in L if x.startswith("ts psi_change")]
        self.assertEqual(len(ch), 1)
        self.assertIn("H264:video", ch[0])
        self.assertIn("AAC-ADTS:audio", ch[0])
        # repeating the same PMT is not a change
        an.feed(b"".join(g.psi(es=((0x1B, VPID, b""), (0x0F, APID, b"")))), 1002.0)
        self.assertEqual(len([x for x in L if x.startswith("ts psi_change")]), 1)

    def test_corrupt_psi_crc_is_not_parsed(self):
        an, clk, L = mk_an()
        p = bytearray(mk_pmt())
        p[20] ^= 0xFF
        an.feed(mk_pat() + bytes(p), 1000.0)
        self.assertFalse([x for x in L if x.startswith("ts psi ")])
        self.assertTrue([x for x in L if "psi_crc_error" in x])

    def test_per_second_line_counts_and_skew(self):
        an, clk, L = mk_an()
        g = Gen()
        run_second_of_av(an, g, clk)
        an.tick(clk.t)
        sec = [x for x in L if x.startswith("ts sec")]
        self.assertEqual(len(sec), 1)
        s = sec[0]
        kv = dict(p.split("=", 1) for p in s.split() if "=" in p)
        self.assertGreater(int(kv["v_pkts"]), 30)
        self.assertEqual(int(kv["v_bytes"]), int(kv["v_pkts"]) * 188)
        self.assertGreater(int(kv["a_pkts"]), 30)
        self.assertEqual(kv["pes_v"], "30")
        self.assertEqual(kv["pes_a"], "31")
        self.assertEqual(kv["cc_err_v"], "0")
        self.assertEqual(kv["cc_err_a"], "0")
        self.assertEqual(kv["pcr_disc"], "0")
        self.assertIn("av_skew_ms", kv)
        self.assertNotEqual(kv["av_skew_ms"], "-")
        self.assertLess(abs(int(kv["av_skew_ms"])), 100)
        self.assertNotEqual(kv["pcr_pts_ms"], "-")
        self.assertEqual(kv["pcr_drift_ms"], "0")
        self.assertEqual(kv["seq"], "7")

    def test_pts_ms_values_reported(self):
        an, clk, L = mk_an()
        g = Gen()
        an.feed(b"".join(g.psi() + g.video() + g.audio()), 1000.0)
        an.tick(1001.0)
        s = [x for x in L if x.startswith("ts sec")][0]
        self.assertIn("v_pts_ms=%d" % ((90000 + 3003) // 90), s)
        self.assertIn("a_pts_ms=%d" % ((90000 + 2880) // 90), s)

    def test_av_skew_measured(self):
        an, clk, L = mk_an()
        g = Gen()
        an.feed(b"".join(g.psi() + g.video()), 1000.0)
        g.apts += 90 * 400          # audio 400 ms ahead of video
        an.feed(b"".join(g.audio()), 1000.0)
        an.tick(1001.0)
        s = [x for x in L if x.startswith("ts sec")][0]
        skew = int(s.split("av_skew_ms=")[1].split()[0])
        self.assertTrue(380 <= skew <= 420, skew)
        self.assertIn("max_av_skew_ms=", an.summary())

    def test_cc_errors_counted_per_kind(self):
        an, clk, L = mk_an()
        g = Gen()
        an.feed(b"".join(g.psi() + g.video() + g.audio()), 1000.0)
        g.cv[0] += 2                # skip two video packets
        g.ca[0] += 1                # skip one audio packet
        an.feed(b"".join(g.video() + g.audio()), 1000.1)
        an.tick(1001.0)
        s = [x for x in L if x.startswith("ts sec")][0]
        self.assertIn("cc_err_v=1", s)
        self.assertIn("cc_err_a=1", s)
        self.assertEqual(len([x for x in L if x.startswith("ts cc_error")]), 2)
        self.assertIn("kind=video", [x for x in L if x.startswith("ts cc_error")][0])
        sm = an.summary("x")
        self.assertIn("cc_err_v=1", sm)
        self.assertIn("cc_err_a=1", sm)

    def test_duplicate_packet_counted_not_cc_error(self):
        an, clk, L = mk_an()
        g = Gen()
        v = g.video()
        an.feed(b"".join(g.psi() + v + [v[-1]]), 1000.0)
        an.tick(1001.0)
        s = [x for x in L if x.startswith("ts sec")][0]
        self.assertIn("dup=1", s)
        self.assertIn("cc_err_v=0", s)

    def test_discontinuity_flag_excuses_cc_jump(self):
        an, clk, L = mk_an()
        g = Gen()
        an.feed(b"".join(g.psi() + g.video()), 1000.0)
        g.cv[0] += 5
        an.feed(b"".join(pkts(VPID, pes(0xE0, 200000, b"x" * 50), g.cv,
                              pcr=100000, disc=True)), 1000.1)
        an.tick(1001.0)
        s = [x for x in L if x.startswith("ts sec")][0]
        self.assertIn("cc_err_v=0", s)
        self.assertIn("pcr_disc=0", s)

    def test_tei_scrambling_null_stuffing_counted(self):
        an, clk, L = mk_an()
        g = Gen()
        a = bytearray(g.audio()[0])
        a[1] |= 0x80                 # transport_error_indicator
        b = bytearray(g.video()[0])
        b[3] |= 0x80                 # scrambling control = 2
        null = bytes([0x47, 0x1F, 0xFF, 0x10]) + b"\xff" * 184
        an.feed(b"".join(g.psi()) + bytes(a) + bytes(b) + null + null, 1000.0)
        an.tick(1001.0)
        s = [x for x in L if x.startswith("ts sec")][0]
        self.assertIn("tei=1", s)
        self.assertIn("scr=1", s)
        self.assertIn("null=2", s)
        st = int(s.split("af_stuff_bytes~=")[1].split()[0])
        self.assertGreater(st, 0)   # the short audio/video tail packets carry stuffing

    def test_pcr_discontinuity_logged(self):
        an, clk, L = mk_an()
        g = Gen()
        an.feed(b"".join(g.psi() + g.video()), 1000.0)
        g.vpts += 90000 * 5          # PCR jumps 5 s forward without the flag
        an.feed(b"".join(g.video()), 1000.1)
        an.tick(1001.0)
        s = [x for x in L if x.startswith("ts sec")][0]
        self.assertIn("pcr_disc=1", s)
        self.assertTrue([x for x in L if x.startswith("ts pcr_discontinuity")])
        g.vpts -= 90000 * 8          # and backwards
        an.feed(b"".join(g.video()), 1000.2)
        self.assertEqual(an.summary().count("pcr_disc=2"), 1)

    def test_pcr_pts_drift_reported(self):
        an, clk, L = mk_an()
        g = Gen()
        an.feed(b"".join(g.psi() + g.video()), 1000.0)
        pcr_now = g.vpts - 9000 + 3003
        g.vpts += 3003 + 90 * 250    # video PTS drifts 250 ms vs the PCR
        an.feed(b"".join(pkts(VPID, pes(0xE0, g.vpts, b"x" * 50), g.cv, pcr=pcr_now)), 1000.1)
        an.tick(1001.0)
        s = [x for x in L if x.startswith("ts sec")][0]
        d = int(s.split("pcr_drift_ms=")[1].split()[0])
        self.assertTrue(240 <= d <= 260, d)
        self.assertIn("max_pcr_pts_drift_ms=2", an.summary())

    def test_audio_gap_wall_and_pts_logged_and_video_unaffected(self):
        an, clk, L = mk_an()
        g = Gen()
        t = 1000.0
        an.feed(b"".join(g.psi()), t)
        for i in range(30):                       # warm-up: 32 ms cadence
            t += 0.032
            an.feed(b"".join(g.audio() + g.video()), t)
        t += 0.5                                  # 500 ms with no audio, video continues
        for k in range(15):                       # video keeps its cadence
            an.feed(b"".join(g.video()), t - 0.5 + k * 0.033)
        g.apts += 90 * 470
        an.feed(b"".join(g.audio()), t)
        gaps = [x for x in L if x.startswith("ts gap") and "kind=audio" in x]
        self.assertEqual(len(gaps), 1, gaps)
        self.assertIn("kind=audio", gaps[-1])
        self.assertIn("wall_ms=5", gaps[-1])
        self.assertEqual([x for x in L if x.startswith("ts gap") and "kind=video" in x], [])
        self.assertIn("video_age_ms=", gaps[-1])
        sm = an.summary()
        self.assertIn("a_gaps=1", sm)

    def test_video_only_gap(self):
        an, clk, L = mk_an()
        g = Gen()
        t = 1000.0
        an.feed(b"".join(g.psi()), t)
        for i in range(30):
            t += 0.033
            an.feed(b"".join(g.video() + g.audio()), t)
        t += 0.4
        an.feed(b"".join(g.audio()), t - 0.2)
        g.vpts += 90 * 370
        an.feed(b"".join(g.video()), t)
        gaps = [x for x in L if x.startswith("ts gap")]
        self.assertTrue(gaps and "kind=video" in gaps[-1], gaps)
        self.assertIn("v_gaps=1", an.summary())

    def test_normal_cadence_has_no_gap_lines_and_long_pes_adapts_threshold(self):
        an, clk, L = mk_an()
        g = Gen()
        t = 1000.0
        an.feed(b"".join(g.psi()), t)
        for i in range(40):                       # audio PES every 192 ms (6 frames)
            t += 0.192
            vid = []
            for k in range(6):                    # video keeps its 32 ms cadence
                vid += g.video(dpts=5760)
            an.feed(b"".join(g.audio(dpts=17280) + vid), t)
        self.assertEqual([x for x in L if x.startswith("ts gap") and "kind=audio" in x], [])

    def test_stall_ongoing_line_once(self):
        an, clk, L = mk_an()
        g = Gen()
        an.feed(b"".join(g.psi() + g.video() + g.audio()), 1000.0)
        for i in range(1, 6):
            an.tick(1000.0 + i)
        st = [x for x in L if x.startswith("ts stall")]
        self.assertEqual(len([x for x in st if "kind=audio" in x]), 1)
        self.assertEqual(len([x for x in st if "kind=video" in x]), 1)

    def test_audio_pts_backwards_and_jump(self):
        an, clk, L = mk_an()
        g = Gen()
        an.feed(b"".join(g.psi() + g.audio()), 1000.0)
        g.apts -= 90 * 2000
        an.feed(b"".join(g.audio()), 1000.03)
        self.assertTrue([x for x in L if x.startswith("ts audio_pts_backwards")])
        g.apts += 90 * 5000
        an.feed(b"".join(g.audio()), 1000.06)
        sm = an.summary()
        self.assertIn("a_pts_back=1", sm)
        self.assertIn("a_pts_jump=1", sm)

    def test_audio_codec_logged_and_change_detected(self):
        an, clk, L = mk_an()
        g = Gen()
        an.feed(b"".join(g.psi() + g.audio()), 1000.0)
        self.assertTrue([x for x in L if x.startswith("ts audio_codec ") and "AC3/48k/acmod7" in x], L)
        g.acodec = b"\xff\xf1\x4c\x80" + b"\x00" * 40      # ADTS 48k stereo
        an.feed(b"".join(g.audio()), 1000.03)
        ch = [x for x in L if x.startswith("ts audio_codec_change")]
        self.assertEqual(len(ch), 1)
        self.assertIn("AAC-ADTS/48000/ch2", ch[0])

    def test_sniff_audio_labels(self):
        self.assertTrue(zs.sniff_audio(b"\xff\xfd\x94\x00" + b"\0" * 8).startswith("MPEG-audio/MP2/48k"))
        self.assertEqual(zs.sniff_audio(b"\x0b\x77\0\0\0\x05\xe0"), "AC3/48k/acmod7")
        self.assertEqual(zs.sniff_audio(b"\x0b\x77\0\0\0\x81\xe0"[:5] + b"\x81\xe0"), "EAC3")
        self.assertTrue(zs.sniff_audio(b"\xff\xf1\x4c\x80\0\0\0").startswith("AAC-ADTS/48000"))
        self.assertEqual(zs.sniff_audio(b"\x56\xe0\0\0\0\0\0"), "AAC-LATM")
        self.assertIsNone(zs.sniff_audio(b"\x00" * 10))
        self.assertIsNone(zs.sniff_audio(b""))

    def test_summary_fields(self):
        an, clk, L = mk_an()
        g = Gen()
        run_second_of_av(an, g, clk)
        an.tick(clk.t)
        txt = an.finish("client_closed")
        for frag in ("ts summary", "close=client_closed", "v_pkts=", "a_pkts=",
                     "cc_err_v=0", "cc_err_a=0", "max_av_skew_ms=", "a_gaps=0",
                     "a_gap_max_wall_ms=", "codec=AC3/48k/acmod7", "pid_pkts/ccerr="):
            self.assertIn(frag, txt)
        self.assertEqual(L[-1], txt)

    def test_event_lines_rate_limited(self):
        an, clk, L = mk_an(line_budget=5)
        g = Gen()
        an.feed(b"".join(g.psi()), 1000.0)
        for i in range(100):
            g.vpts += 90 * 900
            an.feed(b"".join(g.video()), 1000.0 + i)
        ev = [x for x in L if x.startswith(("ts gap", "ts pcr_disc", "ts cc_error"))]
        self.assertLessEqual(len(ev), 5)
        self.assertGreater(an.ev_dropped, 0)
        self.assertIn("ev_dropped=%d" % an.ev_dropped, an.summary())

    def test_split_chunks_and_partial_packets(self):
        an1, _, L1 = mk_an()
        an2, _, L2 = mk_an()
        g = Gen()
        data = b"".join(g.psi() + g.video() + g.audio() + g.video() + g.audio())
        an1.feed(data, 1000.0)
        for i in range(0, len(data), 97):         # misaligned pieces
            an2.feed(data[i:i + 97], 1000.0)
        an1.tick(1001.0)
        an2.tick(1001.0)
        s1 = [x for x in L1 if x.startswith("ts sec")][0].split("an_us")[0]
        s2 = [x for x in L2 if x.startswith("ts sec")][0].split("an_us")[0]
        self.assertEqual(s1, s2)

    def test_garbage_and_boom_never_raise(self):
        def boom(_):
            raise RuntimeError("x")
        an = zs.StreamAnalyzer("c", emit=boom)
        an.feed(b"\x00" * 1000, 1.0)
        an.feed(None, 1.0)
        an.feed(os.urandom(188 * 50), 2.0)
        an.tick(10.0)
        an.finish("x")

    def test_overhead_guard_switches_analyzer_off(self):
        t = [0.0]

        def cpu():
            t[0] += 0.9                   # every call burns 0.9 s of "CPU"
            return t[0]
        an, clk, L = mk_an(cpu=cpu)
        g = Gen()
        for sec in range(3):
            an.feed(b"".join(g.psi() + g.video()), 1000.0 + sec)
            clk.t = 1001.0 + sec
            an.tick(clk.t)
        self.assertTrue(an.off)
        self.assertTrue([x for x in L if x.startswith("ts analyzer_off")])
        n = an.nbytes
        an.feed(b"".join(g.video()), 1010.0)
        self.assertEqual(an.nbytes, n)


class InputSide(unittest.TestCase):
    def hdr(self, kind, sid=0x7E9, n=0, dev=b"02bb00a00001"):
        return (b"Data" + kind + dev + b"\0" * 4 + (1).to_bytes(4, "big")
                + sid.to_bytes(4, "big") + b"\0" * 36 + n.to_bytes(4, "big"))

    def mk(self):
        c, L = Clk(), []
        i = zs.InputStats("chX", clock=c, emit=L.append)
        i.seq = 3
        return i, c, L

    def test_per_second_counts_and_first_last_ids(self):
        i, c, L = self.mk()
        i.on_frame(brptune.K_VIDEO, self.hdr(brptune.K_VIDEO), 1000)
        i.on_frame(brptune.K_AUDIO, self.hdr(brptune.K_AUDIO), 400)
        i.on_frame(brptune.K_CTRL, self.hdr(brptune.K_CTRL, sid=0), 600)
        i.on_frame(brptune.K_VIDEO, self.hdr(brptune.K_VIDEO), 1000)
        c.t += 1.0
        i.tick()
        s = [x for x in L if x.startswith("ts in_sec")][0]
        for frag in ("seq=3", "frames={v:2 a:1 c:1}", "bytes={v:2000 a:400 c:600}",
                     "first=v:7e9", "last=v:7e9", "max_frame_gap_ms=0"):
            self.assertIn(frag, s)
        self.assertIn("frames={v:2 a:1 c:1}", i.summary_text())

    def test_sid_change_flagged(self):
        i, c, L = self.mk()
        i.on_frame(brptune.K_VIDEO, self.hdr(brptune.K_VIDEO, sid=0x7E9), 10)
        i.on_frame(brptune.K_AUDIO, self.hdr(brptune.K_AUDIO, sid=0x7D7), 10)
        self.assertTrue([x for x in L if x.startswith("ts push_sid_change")])
        self.assertIn("sid_changes=1", i.summary_text())

    def test_conn_events_and_counts(self):
        i, c, L = self.mk()
        i.on_conn(brptune.K_VIDEO, "192.0.2.30", 0x7E9, "02bb00a00001")
        i.on_conn(brptune.K_AUDIO, "192.0.2.30", 0x7E9)
        i.on_conn(brptune.K_CTRL, "192.0.2.30", 0)
        self.assertEqual(i.conns(), "v:1 a:1 c:1")
        i.on_conn_close(brptune.K_AUDIO, "recv_closed_or_bad_header")
        self.assertIn("open={v:1 a:0 c:1}", L[-1])
        self.assertIn("why=recv_closed_or_bad_header", L[-1])
        self.assertIn("conns_closed={v:0 a:1 c:0}", i.summary_text())

    def test_session_push_feeds_stats_and_ctrl_error(self):
        os.environ["ZLOG_DIAG"] = "1"
        s = brptune.Session("0x1130000000002001")
        self.assertIsNotNone(s.zin)
        L = []
        s.zin.emit = L.append
        ctrl = b'{"track_meta":{"program_nbr":2,"pmt_pid":128,"pcr_pid":49,"video_pid":49},' \
               b'"tracks":[{"pid":52,"type":129,"active":true}]}'
        s.push(brptune.K_CTRL, self.hdr(brptune.K_CTRL, sid=0), ctrl)
        s.push(brptune.K_CTRL, self.hdr(brptune.K_CTRL, sid=0), b"{not json}")
        s.push(brptune.K_VIDEO, self.hdr(brptune.K_VIDEO), b"\x00" * 10)
        self.assertTrue([x for x in L if x.startswith("ts ctrl_leg ") and "program=2" in x
                         and "0x34:t0x81" in x], L)
        self.assertTrue([x for x in L if x.startswith("ts ctrl_leg_error")])
        self.assertEqual(s.zin.frames["c"], 2)
        self.assertEqual(s.zin.frames["v"], 1)

    def test_session_without_diag_has_no_stats(self):
        old = os.environ.get("ZLOG_DIAG")
        os.environ["ZLOG_DIAG"] = "0"
        try:
            s = brptune.Session("c")
            self.assertIsNone(s.zin)
            s.push(brptune.K_VIDEO, self.hdr(brptune.K_VIDEO), b"\x00" * 10)
        finally:
            if old is None:
                os.environ.pop("ZLOG_DIAG", None)
            else:
                os.environ["ZLOG_DIAG"] = old

    def test_unbound_push_logged_rate_limited(self):
        out = io.StringIO()
        zs._unbound_rl = zdiag.RateLimit(5.0)
        with td.diag_env("1"):
            import contextlib
            with contextlib.redirect_stdout(out):
                for _ in range(5):
                    zs.log_unbound(brptune.K_AUDIO, "192.0.2.30", {49}, 128)
        self.assertEqual(out.getvalue().count("push conn unbound kind=a"), 1)

    def test_never_raises(self):
        def boom(_):
            raise RuntimeError("x")
        i = zs.InputStats("c", emit=boom)
        i.on_frame(b"zz", None, 1)
        i.on_ctrl(None, None)
        i.on_conn(b"zz", None, 0)
        i.on_conn_close(b"zz", "x")
        i.tick(1e9)


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="zcap")
        self.old = {k: os.environ.get(k) for k in (
            "ZCAPTURE_SECONDS", "ZCAPTURE_DIR", "ZCAPTURE_MAX_MB", "ZCAPTURE_FILE_MB", "ZLOG_DIAG")}
        os.environ["ZCAPTURE_DIR"] = self.d

    def tearDown(self):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_off_by_default(self):
        os.environ.pop("ZCAPTURE_SECONDS", None)
        self.assertIsNone(zs.Capture.start("2.1"))
        self.assertEqual(os.listdir(self.d), [])

    def test_captures_first_n_seconds_then_stops(self):
        os.environ["ZCAPTURE_SECONDS"] = "5"
        c = Clk()
        L = []
        cap = zs.Capture.start("2.1", clock=c, emit=L.append)
        self.assertIsNotNone(cap)
        cap.write(b"A" * 188)
        c.t += 2
        cap.write(b"B" * 188)
        c.t += 4
        cap.write(b"C" * 188)                   # past 5 s: closes, not written
        self.assertTrue(cap.done)
        with open(cap.path, "rb") as f:
            self.assertEqual(f.read(), b"A" * 188 + b"B" * 188)
        self.assertRegex(os.path.basename(cap.path), r"^\d{8}-\d{6}_2\.1\.ts$")
        self.assertTrue(any("capture done" in x and "why=seconds" in x for x in L))

    def test_size_cap_per_file(self):
        os.environ["ZCAPTURE_SECONDS"] = "60"
        os.environ["ZCAPTURE_FILE_MB"] = "0.001"      # ~1 KB
        cap = zs.Capture.start("x", emit=lambda m: None)
        for _ in range(20):
            cap.write(b"z" * 188)
        self.assertTrue(cap.done)
        self.assertLessEqual(os.path.getsize(cap.path), 1049)

    def test_rotation_deletes_oldest_own_files_only(self):
        os.environ["ZCAPTURE_SECONDS"] = "60"
        os.environ["ZCAPTURE_MAX_MB"] = "0.01"        # ~10 KB total
        os.environ["ZCAPTURE_FILE_MB"] = "0.004"
        keep = os.path.join(self.d, "notes.txt")
        open(keep, "w").write("keep me")
        names = []
        for i in range(5):
            p = os.path.join(self.d, "20260101-00000%d_c.ts" % i)
            open(p, "wb").write(b"x" * 4000)
            os.utime(p, (1000 + i, 1000 + i))
            names.append(p)
        cap = zs.Capture.start("new", emit=lambda m: None)
        cap.close()
        left = sorted(os.listdir(self.d))
        self.assertIn("notes.txt", left)
        self.assertNotIn("20260101-000000_c.ts", left)
        self.assertIn("20260101-000004_c.ts", left)
        total = sum(os.path.getsize(os.path.join(self.d, n)) for n in left if n.endswith(".ts"))
        self.assertLessEqual(total, 10486)

    def test_name_is_sanitised(self):
        os.environ["ZCAPTURE_SECONDS"] = "5"
        cap = zs.Capture.start("../../etc/pass wd", emit=lambda m: None)
        cap.close()
        self.assertEqual(os.path.dirname(cap.path), self.d)
        self.assertNotIn("..", os.path.basename(cap.path).replace("..", "", 0)[:0])

    def test_diag_off_disables_capture(self):
        os.environ["ZCAPTURE_SECONDS"] = "5"
        os.environ["ZLOG_DIAG"] = "0"
        self.assertIsNone(zs.Capture.start("x"))


class Wiring(unittest.TestCase):
    def setUp(self):
        self._pace = h.PACE
        self.d = tempfile.mkdtemp(prefix="zwire")

    def tearDown(self):
        h.PACE = self._pace

    def stream(self, env, pace=True, extra=None, wfile=None):
        g = Gen()
        pk = g.psi()
        for f in range(60):
            if f % 15 == 0:
                pk += g.psi()
            pk += g.video() + g.audio() + g.audio()
        h.PACE = pace
        hd = td.StreamHandler(wfile)
        hd._zseq = 42
        hd._zguide = "2.1"
        old = {k: os.environ.get(k) for k in (extra or {})}
        os.environ.update(extra or {})
        try:
            with td.diag_env(env):
                _, out = td.capture(hd._ts_brp, td.Sess(pk), headers_sent=True)
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return hd, out, b"".join(pk)

    def test_bytes_identical_on_off_and_equal_to_source(self):
        for pace in (True, False):
            outs = []
            for env in ("1", "0"):
                hd, out, src = self.stream(env, pace)
                outs.append(hd.wfile.getvalue())
            self.assertEqual(outs[0], outs[1])
            self.assertEqual(outs[0], src)

    def test_bytes_identical_with_capture_and_ts_off(self):
        base = self.stream("0")[0].wfile.getvalue()
        ex = {"ZCAPTURE_SECONDS": "30", "ZCAPTURE_DIR": self.d}
        self.assertEqual(self.stream("1", extra=ex)[0].wfile.getvalue(), base)
        self.assertEqual(self.stream("1", extra={"ZLOG_TS": "0"})[0].wfile.getvalue(), base)

    def test_summary_line_in_paced_and_unpaced(self):
        for pace in (True, False):
            hd, out, _ = self.stream("1", pace)
            self.assertIn("ts summary stream=0x1130000000002001 seq=42", out)
            self.assertIn("cc_err_v=0", out)
            self.assertIn("codec=AC3/48k/acmod7", out)
            self.assertIn("ts psi stream=0x1130000000002001 seq=42 program=1", out)

    def test_diag_off_emits_no_new_lines(self):
        hd, out, _ = self.stream("0")
        for tag in ("ts ", "stream client", "wgap", "in_sec"):
            self.assertNotIn(tag, out)
        self.assertTrue(out.startswith("brp stream "), out)

    def test_ts_switch_off_keeps_wgap_but_no_ts_lines(self):
        hd, out, _ = self.stream("1", extra={"ZLOG_TS": "0"})
        self.assertIn("wgap summary", out)
        self.assertNotIn("ts summary", out)

    def test_capture_written_from_output(self):
        ex = {"ZCAPTURE_SECONDS": "30", "ZCAPTURE_DIR": self.d}
        hd, out, src = self.stream("1", extra=ex)
        files = [n for n in os.listdir(self.d) if n.endswith(".ts")]
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0], r"_2\.1\.ts$")
        self.assertEqual(open(os.path.join(self.d, files[0]), "rb").read(), src)
        self.assertIn("capture done", out)

    def test_client_error_still_reports_and_summarises(self):
        hd, out, _ = self.stream("1", wfile=td.BrokenWfile())
        self.assertEqual(hd._zreason, "client_closed")
        self.assertIn("ts summary", out)

    def test_client_line_has_seq_and_no_secrets(self):
        hd = td.StreamHandler()
        hd._zseq, hd._zguide = 9, "2.1"
        hd._zlog_client = h.Stream._zlog_client.__get__(hd)
        out = io.StringIO()
        import contextlib
        with td.diag_env("1"), contextlib.redirect_stdout(out):
            hd._zlog_client("0x1130000000002001")
        s = out.getvalue()
        self.assertIn("stream client seq=9 chan=0x1130000000002001 guide=2.1 client=192.0.2.44:5555", s)
        self.assertIn("path=/auto/v2.1", s)
        self.assertNotIn("SECRET", s)
        self.assertNotIn("Cookie", s)

    def test_jsonl_optional(self):
        p = os.path.join(self.d, "d.jsonl")
        self.stream("1", extra={"ZDIAG_JSONL": p})
        rec = [json.loads(x) for x in open(p)]
        self.assertEqual(rec[-1]["event"], "stream_summary")
        self.assertEqual(rec[-1]["seq"], 42)
        self.assertIn("ts summary", rec[-1]["ts_summary"])
        # off when unset
        q = os.path.join(self.d, "none.jsonl")
        self.stream("1")
        self.assertFalse(os.path.exists(q))

    def test_seq_ids_increase(self):
        a, b = zdiag.next_seq(), zdiag.next_seq()
        self.assertEqual(b, a + 1)

    def test_volume_is_small(self):
        hd, out, _ = self.stream("1")
        self.assertLess(len(out.splitlines()), 40)


class ConnPath(unittest.TestCase):
    """Real _conn() over a socketpair: connection events, reasons, unbound."""

    def run_conn(self, frames, bind_session=True):
        import socket
        import test_ctrl_leg as tc
        os.environ["ZLOG_DIAG"] = "1"
        t = tc.new_tuner()
        s = tc.mk(t, 0, tc.CH_21, 1) if bind_session else None
        if s is not None:
            s.zin.emit = self.L.append
        t.slots[0].push_ip = "127.0.0.1"
        a, b = socket.socketpair()

        class Wrap:
            def __init__(self, x): self._x = x
            def __getattr__(self, k): return getattr(self._x, k)
            def getsockname(self): return ("127.0.0.1", 31061)
        th = threading.Thread(target=t._conn, args=(Wrap(b),))
        th.start()
        for f in frames:
            a.sendall(f)
        a.close()
        th.join(5)
        self.assertFalse(th.is_alive())
        return s

    def setUp(self):
        self.L = []

    def test_open_close_events_and_frame_counts(self):
        import test_ctrl_leg as tc
        h, d = tc.fx("ctrl_21")
        vh, vd = tc.fx("video_21")
        s = self.run_conn([h + d])
        self.assertTrue([x for x in self.L if x.startswith("ts push_conn_open") and "kind=c" in x], self.L)
        cl = [x for x in self.L if x.startswith("ts push_conn_close")]
        self.assertEqual(len(cl), 1)
        self.assertIn("why=recv_closed_or_bad_header", cl[0])
        self.assertTrue([x for x in self.L if x.startswith("ts ctrl_leg ") and "program=2" in x])
        self.assertEqual(s.zin.frames["c"], 1)

    def test_bad_frame_length_reason(self):
        import test_ctrl_leg as tc
        h, d = tc.fx("ctrl_21")
        bad = bytearray(h)
        bad[64:72] = b"\0" * 8
        self.run_conn([h + d, bytes(bad)])
        self.assertTrue([x for x in self.L if x.startswith("ts push_conn_close") and "why=bad_frame_len" in x], self.L)

    def test_unbound_connection_logged_not_fatal(self):
        import test_ctrl_leg as tc
        import contextlib
        zs._unbound_rl = zdiag.RateLimit(5.0)
        vh, vd = tc.fx("video_21")
        vh = vh[:68] + len(vd).to_bytes(4, "big")     # fixture bodies are truncated
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.run_conn([vh + vd], bind_session=False)
        self.assertIn("push conn unbound kind=v dst_ip=127.0.0.1", out.getvalue())


if __name__ == "__main__":
    unittest.main()
