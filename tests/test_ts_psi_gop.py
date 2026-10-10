"""Output TS fixes: spec-correct PAT/PMT and MPEG-2 random-access start.

Fixtures tests/fixtures/ts_c{1,2,3,51,91}.bin are cut from saved clips: the M1's raw push
(starts mid-GOP, first sequence header 72 KB-628 KB in) without PSI. They are fed
into a Session queue offline and read back through Session.packets(), i.e. the
real output code path (no live facade, no network).

Run under pytest, or standalone: python3 tests/test_ts_psi_gop.py
"""
import collections
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import brptune as bt

FIX = os.path.join(HERE, "fixtures")
#: name -> (video pid, audio pids); all video is MPEG-2, audio ATSC AC-3 (0x81)
CLIPS = {"c1": (0x81, (0x84, 0x85)), "c2": (0x81, (0x84, 0x85)),
         "c3": (0x81, (0x84, 0x85)), "c51": (0x51, (0x54, 0x55)),
         "c91": (0x31, (0x34, 0x35))}
HAVE_FF = bool(shutil.which("ffprobe") and shutil.which("ffmpeg"))


def pkts_of(data):
    assert len(data) % 188 == 0
    return [data[i:i + 188] for i in range(0, len(data), 188)]


def pid_of(p):
    return ((p[1] & 0x1F) << 8) | p[2]


def load(name):
    return pkts_of(open(os.path.join(FIX, "ts_" + name + ".bin"), "rb").read())


def run_session(name, es_types=None, gate=None, drain_idle=True):
    """Queue a fixture into a Session and collect Session.packets() output."""
    vpid, apids = CLIPS[name]
    s = bt.Session("test")
    s.es = {vpid: 0x02}
    for a in apids:
        s.es[a] = 0x81
    if es_types:
        s.es = dict(es_types(vpid, apids))
    s.pcr_pid, s.pmt_pid = vpid, 0x1000
    if gate is not None:
        s.gate = gate
    src = load(name)
    for p in src:
        s.q.setdefault(pid_of(p), collections.deque()).append(p)
    s.last_push = time.time()
    out = []
    for p in s.packets():
        out.append(p)
        if not any(s.q.values()):
            s.last_push = 0          # queues drained -> stale -> end of stream
    return src, out, s


# ---- independent PSI parser (does not use brptune helpers) ------------------
def crc32_bitwise(data):
    crc = 0xFFFFFFFF
    for b in data:
        for i in range(7, -1, -1):
            top = (crc >> 31) & 1
            crc = (crc << 1) & 0xFFFFFFFF
            if top ^ ((b >> i) & 1):
                crc ^= 0x04C11DB7
    return crc


def section_of(p):
    assert p[0] == 0x47 and p[1] & 0x40
    off = 4
    ptr = p[off]
    s = p[off + 1 + ptr:]
    ln = ((s[1] & 0x0F) << 8) | s[2]
    return s[:3 + ln]


def parse_pat(sec):
    assert sec[0] == 0x00 and sec[1] & 0xC0 == 0x80       # syntax ind, '0'
    ln = ((sec[1] & 0x0F) << 8) | sec[2]
    assert ln == len(sec) - 3
    assert crc32_bitwise(sec) == 0, "PAT CRC"
    assert sec[5] & 0xC0 == 0xC0                          # reserved bits
    assert sec[6] == 0 and sec[7] == 0                    # section numbers
    progs = {}
    body = sec[8:-4]
    assert len(body) % 4 == 0
    for i in range(0, len(body), 4):
        progs[(body[i] << 8) | body[i + 1]] = ((body[i + 2] & 0x1F) << 8) | body[i + 3]
    return ln, progs


def parse_pmt(sec):
    assert sec[0] == 0x02 and sec[1] & 0xC0 == 0x80
    ln = ((sec[1] & 0x0F) << 8) | sec[2]
    assert ln == len(sec) - 3
    assert crc32_bitwise(sec) == 0, "PMT CRC"
    assert sec[5] & 0xC0 == 0xC0 and sec[6] == 0 and sec[7] == 0
    pcr = ((sec[8] & 0x1F) << 8) | sec[9]
    pil = ((sec[10] & 0x0F) << 8) | sec[11]
    e = sec[12 + pil:-4]
    es = {}
    while e:
        st, pid = e[0], ((e[1] & 0x1F) << 8) | e[2]
        il = ((e[3] & 0x0F) << 8) | e[4]
        assert e[1] & 0xE0 == 0xE0 and e[3] & 0xF0 == 0xF0
        es[pid] = (st, bytes(e[5:5 + il]))
        assert len(e) >= 5 + il
        e = e[5 + il:]
    return ln, pcr, es


# ---- tests -------------------------------------------------------------------
def test_pat_pmt_spec():
    pat = bt.mk_pat(bt.OUT_PMT)
    ln, progs = parse_pat(pat)
    assert ln == 13, ln                                  # was 11
    assert progs == {bt.OUT_PROG: bt.OUT_PMT}
    es = {0x81: 0x02, 0x84: 0x81, 0x85: 0x81}
    pmt = bt.mk_pmt(0x81, es)
    ln, pcr, parsed = parse_pmt(pmt)
    assert pcr == 0x81
    assert parsed[0x81] == (0x02, b"")
    for a in (0x84, 0x85):
        assert parsed[a] == (0x81, b"\x05\x04AC-3"), parsed[a]
    # version bits are the version, not part of the reserved field
    assert (pat[5] >> 1) & 0x1F == 0 and (pmt[5] >> 1) & 0x1F == 0
    assert (bt.mk_pmt(0x81, es, ver=5)[5] >> 1) & 0x1F == 5
    # one section fits one TS packet, PSI packets are 188 bytes with valid cc
    pk = bt.psi_packet(bt.OUT_PAT, pat, 3)
    assert len(pk) == 188 and pk[3] & 0x0F == 3 and pk[1] & 0x40
    assert section_of(pk) == pat


def test_gate_helpers():
    def pes(payload, pts=True):
        h = b"\x00\x00\x01\xe0\x00\x00\x80\x80\x05\x21\x00\x01\x00\x01"
        return h + payload
    seq = b"\x00\x00\x01\xb3\x78\x04\x38\x13"
    gop = b"\x00\x00\x01\xb8\x00\x08\x00\x40"

    def pic(t):
        return b"\x00\x00\x01\x00\x00" + bytes([t << 3]) + b"\xff\xf8"
    assert bt.mpeg2_start_ok(pes(seq + gop + pic(1)))
    assert not bt.mpeg2_start_ok(pes(seq + gop + pic(2)))      # P
    assert not bt.mpeg2_start_ok(pes(seq + gop + pic(3)))      # B
    assert not bt.mpeg2_start_ok(pes(pic(1)))                  # I, no seq hdr
    assert not bt.mpeg2_start_ok(pes(seq))                     # no picture
    assert not bt.mpeg2_start_ok(pes(b"\x00" * 40 + seq + pic(1)))  # seq not at start
    assert not bt.mpeg2_start_ok(b"")


def _skip_psi(out):
    """Index of the first non-PSI packet; everything before is PAT/PMT pairs
    (PSI is also sent while the gate waits)."""
    i = 0
    while pid_of(out[i]) in (bt.OUT_PAT, bt.OUT_PMT):
        i += 1
    return i


def video_pes_list(out, vpid):
    """[(pts, picture_coding_type, temporal_reference, es_len)] per video PES."""
    pes = []
    for p in out:
        if pid_of(p) != vpid or not p[3] & 0x10:
            continue
        if p[1] & 0x40:
            pes.append(bytearray())
        if pes:
            pes[-1] += bt._payload(p)
    res = []
    for b in pes:
        es = bt._pes_es(b)
        i = es.find(b"\x00\x00\x01\x00")
        if i < 0:
            res.append((bt._pes_pts(b), None, None, len(es)))
        else:
            res.append((bt._pes_pts(b), (es[i + 5] >> 3) & 7,
                        (es[i + 4] << 2) | (es[i + 5] >> 6), len(es)))
    return res


def _check_clip_output(name, out):
    vpid, apids = CLIPS[name]
    # PAT, PMT first, then a video packet with sequence header + I-picture
    k = _skip_psi(out)
    assert k >= 2 and pid_of(out[0]) == bt.OUT_PAT and pid_of(out[1]) == bt.OUT_PMT, name
    assert pid_of(out[k]) == vpid and out[k][1] & 0x40, name
    assert b"\x00\x00\x01\xb3" in out[k], name
    # collect the first video PES
    pes = b""
    for p in out[k:]:
        if pid_of(p) == vpid:
            if p[1] & 0x40 and pes:
                break
            afc = (p[3] >> 4) & 3
            pes += p[5 + p[4]:] if afc & 2 else p[4:]
    assert bt.mpeg2_start_ok(pes), name
    pay = pes[9 + pes[8]:]
    i = pay.find(b"\x00\x00\x01\x00")
    assert (pay[i + 5] >> 3) & 7 == 1, "first picture is not an I-frame"
    # PSI parses and lists MPEG-2 video + AC-3 audio
    ln, progs = parse_pat(section_of(out[0]))
    assert progs == {1: bt.OUT_PMT}
    _, pcr, es = parse_pmt(section_of(out[1]))
    assert pcr == vpid and es[vpid][0] == 0x02
    assert sorted(p for p, (st, _d) in es.items() if st == 0x81) == sorted(apids)


def test_clips_start_and_psi():
    for name in CLIPS:
        src, out, s = run_session(name)
        assert s.gate.state == "open" and s.gate.reason == "gop", name
        assert s.gate.dropped_video > 0, "fixture should start mid-GOP"
        _check_clip_output(name, out)
        # every video packet before the first sequence header is gone
        vpid, apids = CLIPS[name]
        first_src = next(i for i, p in enumerate(src)
                         if pid_of(p) == vpid and b"\x00\x00\x01\xb3" in p[4:80])
        n_src_vid = sum(1 for p in src if pid_of(p) == vpid)
        n_out_vid = sum(1 for p in out if pid_of(p) == vpid and p[3] & 0x10)
        assert n_src_vid - n_out_vid == s.gate.dropped_video + s.gate.dropped_b_pkts, name
        assert s.gate.dropped_video == sum(1 for p in src[:first_src] if pid_of(p) == vpid)
        json.dumps(s.stats())                       # stats stay serialisable
        print("  %-4s dropped video=%d audio=%d leading-B pkts=%d wait=%.3fs" % (
            name, s.gate.dropped_video, s.gate.dropped_audio,
            s.gate.dropped_b_pkts, s.gate.wait_sec))


def test_structure_alignment_cc_pcr_audio():
    for name in CLIPS:
        src, out, s = run_session(name)
        vpid, apids = CLIPS[name]
        assert all(len(p) == 188 and p[0] == 0x47 for p in out), name
        # continuity counters: media PIDs continue exactly as in the source
        # (only a prefix is dropped); PSI PIDs count 0..15 on their own
        last = {}
        for p in out:
            pid, afc, cc = pid_of(p), (p[3] >> 4) & 3, p[3] & 0x0F
            if afc & 1 and pid in last:
                assert cc == (last[pid] + 1) & 0x0F, (name, hex(pid))
            if afc & 1:
                last[pid] = cc
        # PCR (if any adaptation field carries one) must not go backwards
        pcrs = []
        for p in out:
            if p[3] & 0x20 and p[4] >= 7 and p[5] & 0x10:
                pcrs.append(((p[6] << 25) | (p[7] << 17) | (p[8] << 9)
                             | (p[9] << 1) | (p[10] >> 7)))
        assert all(b >= a for a, b in zip(pcrs, pcrs[1:])), name
        # audio aligned with the I-frame: no audio PES earlier than its PTS
        out_pts_v = None
        for p in out:
            if pid_of(p) == vpid and p[1] & 0x40:
                out_pts_v = bt._pes_pts(bt._payload(p))
                break
        assert out_pts_v is not None
        for a in apids:
            first = [bt._pes_pts(bt._payload(p)) for p in out
                     if pid_of(p) == a and p[1] & 0x40]
            assert first, "audio %x lost" % a
            assert (first[0] - out_pts_v) & ((1 << 33) - 1) < (1 << 32), name
            # and the audio kept is a prefix-cut of the source audio
            src_a = [p for p in src if pid_of(p) == a]
            out_a = [p for p in out if pid_of(p) == a]
            assert out_a == src_a[len(src_a) - len(out_a):], (name, hex(a))
            assert out_a[0][1] & 0x40                  # starts on a PES boundary


def _ffmpeg_errors(path):
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "null", "-"],
                       capture_output=True, text=True, timeout=120)
    return r.stderr


def test_ffprobe_ffmpeg():
    if not HAVE_FF:
        print("  (ffprobe/ffmpeg missing; skipped)")
        return
    tmp = tempfile.mkdtemp()
    for name in CLIPS:
        src, out, s = run_session(name)
        new = os.path.join(tmp, name + "_new.ts")
        old = os.path.join(tmp, name + "_old.ts")
        open(new, "wb").write(b"".join(out))
        open(old, "wb").write(b"".join(src))     # raw first burst, no PSI
        j = json.loads(subprocess.run(
            ["ffprobe", "-v", "error", "-show_programs", "-show_streams",
             "-of", "json", new], capture_output=True, text=True).stdout)
        assert len(j["programs"]) == 1, (name, j["programs"])
        kinds = sorted(st["codec_name"] for st in j["programs"][0]["streams"])
        assert kinds == ["ac3", "ac3", "mpeg2video"], (name, kinds)
        err = _ffmpeg_errors(new)
        bad = [l for l in err.splitlines()
               if "Invalid frame dimensions" in l or "ref picture" in l.lower()
               or "Invalid mb type" in l or "motion_type" in l]
        assert not bad, (name, bad)
        print("  %-4s ffmpeg -v error: %r" % (name, err.strip()[:160]))


def test_fallback_no_sequence_header():
    """No sequence header ever arrives: after the budget everything is flushed
    unchanged (PAT/PMT first), so the tune never hangs."""
    name = "c1"
    vpid, apids = CLIPS[name]
    src = load(name)
    first = next(i for i, p in enumerate(src)
                 if pid_of(p) == vpid and b"\x00\x00\x01\xb3" in p[4:80])
    pre = src[:first]                                    # lead-in only
    assert not any(b"\x00\x00\x01\xb3" in p for p in pre if pid_of(p) == vpid)
    clock = [100.0]
    g = bt.GopGate(budget_sec=6, budget_bytes=1 << 30, clock=lambda: clock[0])
    es = {vpid: 0x02, apids[0]: 0x81, apids[1]: 0x81}
    got = []
    for p in pre:
        got += g.feed(p, es)
    assert got == [] and g.state == "wait" and not g.opened
    assert g.tick() == []                                # budget not spent yet
    clock[0] += 6.5
    flushed = g.tick()                                   # idle queue, time up
    assert g.state == "pass" and g.reason == "timeout" and g.opened
    assert flushed == pre                                # byte-identical, in order
    assert g.feed(src[first], es) == [src[first]]        # later packets pass
    # byte budget path
    g = bt.GopGate(budget_sec=1e9, budget_bytes=188 * 50)
    got = []
    for p in pre:
        got += g.feed(p, es)
        if g.state == "pass":
            break
    assert g.reason == "budget" and got == pre[:len(got)] and len(got) == 51
    # end of stream while waiting
    g = bt.GopGate(budget_sec=1e9, budget_bytes=1 << 30)
    for p in pre[:10]:
        g.feed(p, es)
    assert g.flush() == pre[:10] and g.reason == "eos"
    # through Session.packets(): PAT/PMT first, then every packet unchanged
    s_gate = bt.GopGate(budget_sec=0.3)
    vp, ap = CLIPS[name]
    s = bt.Session("fb")
    s.es = {vp: 0x02, ap[0]: 0x81, ap[1]: 0x81}
    s.pcr_pid, s.pmt_pid, s.gate = vp, 0x1000, s_gate
    for p in pre:
        s.q.setdefault(pid_of(p), collections.deque()).append(p)
    s.last_push = time.time()
    out = []
    t0 = time.time()
    for p in s.packets():
        out.append(p)
        if not any(s.q.values()):
            s.last_push = 0
    assert time.time() - t0 < 10
    assert s_gate.state == "pass" and s_gate.reason in ("timeout", "eos")
    media = [p for p in out if pid_of(p) not in (bt.OUT_PAT, bt.OUT_PMT)]
    assert media == pre
    assert pid_of(out[0]) == bt.OUT_PAT and pid_of(out[1]) == bt.OUT_PMT


def test_non_mpeg2_passes_through_unchanged():
    """H.264 / HEVC labelled channels (and gate disabled) are never gated."""
    for st in (0x1B, 0x24):
        src, out, s = run_session(
            "c1", es_types=lambda v, a, st=st: {v: st, a[0]: 0x81, a[1]: 0x81})
        assert s.gate.state == "pass" and s.gate.reason == "not-mpeg2"
        media = [p for p in out if pid_of(p) not in (bt.OUT_PAT, bt.OUT_PMT)]
        for pid in {pid_of(p) for p in src}:      # nothing dropped / altered
            assert [p for p in media if pid_of(p) == pid] == \
                [p for p in src if pid_of(p) == pid]
        assert len(media) == len(src)
        assert pid_of(out[0]) == bt.OUT_PAT and pid_of(out[1]) == bt.OUT_PMT
    # type learned later (ctrl leg defaults the video PID to MPEG-2)
    g = bt.GopGate(budget_sec=10)
    src = load("c1")
    assert g.feed(src[0], {0x81: 0x02, 0x84: 0x81}) == []
    out = g.feed(src[1], {0x81: 0x24, 0x84: 0x81})
    assert g.reason == "not-mpeg2" and out == src[:2]
    old = bt.GOP_GATE
    try:
        bt.GOP_GATE = False
        assert bt.GopGate().state == "pass"
    finally:
        bt.GOP_GATE = old


def _nocc(p):
    return p[:3] + bytes([p[3] & 0xF0]) + p[4:]


def test_mid_stream_pass_after_open_is_stable():
    """After the gate opened, later video is untouched (apart from the
    renumbered continuity counter and the removed leading-B packets)."""
    src, out, s = run_session("c2")
    vpid, apids = CLIPS["c2"]
    tail_src = [_nocc(p) for p in src if pid_of(p) == vpid]
    tail_out = [_nocc(p) for p in out if pid_of(p) == vpid and p[3] & 0x10]
    # out == src minus dropped lead-in minus leading-B packets (a contiguous
    # block right after the I picture)
    first = tail_src.index(tail_out[0])
    rest = tail_src[first:]
    assert len(rest) == len(tail_out) + s.gate.dropped_b_pkts
    nb = s.gate.dropped_b_pkts
    i_len = next(i for i in range(1, len(tail_out)) if tail_out[i][1] & 0x40)
    assert tail_out[:i_len] == rest[:i_len]                       # I PES intact
    assert tail_out[i_len:] == rest[i_len + nb:]                  # rest intact


def test_leading_b_removed():
    """Open GOP: the B-pictures that follow the I and display before it
    (they reference the previous GOP) are gone; first PES after the I is a P."""
    for name in CLIPS:
        vpid, _a = CLIPS[name]
        src, out, s = run_session(name)
        sp = [x for x in video_pes_list(src, vpid)]
        pl = video_pes_list(out, vpid)
        assert pl[0][1] == 1, name                          # starts with the I
        # source: the first PES after the I was a leading B (display before I)
        i_idx = next(i for i, x in enumerate(sp) if x[1] == 1 and x[0] == pl[0][0])
        assert sp[i_idx + 1][1] == 3 and sp[i_idx + 1][0] < pl[0][0], name
        assert s.gate.dropped_b >= 1
        # output: no picture with PTS before the I's PTS, anywhere
        assert all(x[0] is None or (x[0] - pl[0][0]) & ((1 << 33) - 1) < (1 << 32)
                   for x in pl), name
        # and the first PES after the I is a P (or another I), not a B
        assert pl[1][1] in (1, 2), (name, pl[:3])
        # PES count: source PES from the I on, minus the dropped leading Bs
        assert len(pl) == len(sp) - i_idx - s.gate.dropped_b, name


def test_leading_b_decode_and_first_frame_is_i():
    if not HAVE_FF:
        print("  (ffprobe/ffmpeg missing; skipped)")
        return
    tmp = tempfile.mkdtemp()
    for name in CLIPS:
        src, out, s = run_session(name)
        f = os.path.join(tmp, name + ".ts")
        open(f, "wb").write(b"".join(out))
        # decode the first 30 frames including corrupt output: no MV / B errors
        r = subprocess.run(["ffmpeg", "-v", "error", "-flags", "+output_corrupt",
                            "-err_detect", "explode", "-i", f, "-map", "0:v",
                            "-frames:v", "30", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=120)
        bad = [l for l in r.stderr.splitlines() if any(
            w in l for w in ("motion_type", "MVs not available", "Invalid mb type",
                             "Invalid frame dimensions", "ac-tex"))]
        assert not bad and r.returncode == 0, (name, r.stderr[:300])
        j = json.loads(subprocess.run(
            ["ffprobe", "-v", "error", "-flags", "+output_corrupt",
             "-select_streams", "v:0", "-show_frames", "-read_intervals", "%+#3",
             "-of", "json", f], capture_output=True, text=True).stdout)
        assert j["frames"][0]["pict_type"] == "I", (name, j["frames"][0])


def test_leading_b_safety():
    """PES that is not a lone B (extra picture inside), closed GOP, or a stall
    must not lose data; CC stays continuous across a removed block."""
    vpid, apids = CLIPS["c1"]
    src = load("c1")
    es = {vpid: 0x02, apids[0]: 0x81, apids[1]: 0x81}
    # closed_gop=1 in the GOP header => no removal at all
    pes0 = b"\x00\x00\x01\xe0\x00\x00\x80\x00\x00" + \
        b"\x00\x00\x01\xb3\x78\x04\x38\x13\x00\x00\x01\xb8\x00\x08\x00\x40"
    assert bt.mpeg2_closed_gop(pes0)
    assert not bt.mpeg2_closed_gop(pes0[:-1] + b"\x00")
    # a "B" PES that also contains a second picture is kept unchanged
    g = bt.GopGate(budget_sec=10)
    g.state, g.vpid, g.lead, g.opened = "open", vpid, "scan", False
    def vp(payload, pusi, cc):
        p = bytearray(188)
        p[0], p[1], p[2], p[3] = 0x47, (0x40 if pusi else 0) | (vpid >> 8), vpid & 0xFF, 0x10 | cc
        p[4:4 + len(payload)] = payload
        return bytes(p)
    hdr = b"\x00\x00\x01\xe0\x00\x00\x80\x00\x00"
    b_pic = b"\x00\x00\x01\x00\x00\x18\xff\xf8"          # pct 3
    p_pic = b"\x00\x00\x01\x00\x00\x10\xff\xf8"          # pct 2
    out = g._vid(vp(hdr + b_pic + b"\x11" * 20 + p_pic, True, 0))
    assert out == []                                     # pending
    out = g._vid(vp(hdr + p_pic, True, 1))               # next PES -> settle
    assert len(out) == 2 and out[0][3] & 0x0F == 0 and g.dropped_b == 0
    # lone B is dropped, following packets get renumbered contiguously
    g = bt.GopGate(budget_sec=10)
    g.state, g.vpid, g.lead, g.opened, g.v_cc = "open", vpid, "scan", False, 5
    assert g._vid(vp(hdr + b_pic, True, 6)) == []
    o1 = g._vid(vp(hdr + p_pic, True, 7))
    assert g.dropped_b == 1 and len(o1) == 1 and o1[0][3] & 0x0F == 6
    o2 = g._vid(vp(b"\x22" * 30, False, 8))
    assert o2[0][3] & 0x0F == 7
    # stalled pending B is released unchanged by tick() after 0.25 s
    clock = [0.0]
    g = bt.GopGate(budget_sec=10, clock=lambda: clock[0])
    g.state, g.vpid, g.lead, g.opened = "open", vpid, "scan", False
    pk = vp(hdr + b_pic, True, 3)
    assert g._vid(pk) == [] and g.tick() == []
    clock[0] = 1.0
    assert g.tick() == [pk] and g.lead is None
    # end of stream releases a pending B too
    g = bt.GopGate(budget_sec=10)
    g.state, g.vpid, g.lead, g.opened = "open", vpid, "scan", False
    g._vid(pk)
    assert g.flush() == [pk]


def test_pcr_after_gate():
    """PCR survives the gate: present on the video PID, never goes backwards,
    steps stay below 100 ms (ISO 13818-1 limit), real counts printed."""
    for name in CLIPS:
        vpid, _a = CLIPS[name]
        src, out, s = run_session(name)

        def pcrs(pk):
            r = []
            for p in pk:
                if pid_of(p) == vpid and p[3] & 0x20 and p[4] >= 7 and p[5] & 0x10:
                    r.append(((p[6] << 25) | (p[7] << 17) | (p[8] << 9)
                              | (p[9] << 1) | (p[10] >> 7)) * 300 + (((p[10] & 1) << 8) | p[11]))
            return r
        a, b = pcrs(src), pcrs(out)
        assert a and b, name                                   # PCR is present
        steps = [y - x for x, y in zip(b, b[1:])]
        assert all(0 <= d <= 27000000 // 10 for d in steps), (name, max(steps), min(steps))
        # PCR must not be later than the first video DTS/PTS it accompanies
        print("  %-4s PCR packets: src=%d out=%d  step min/max = %.1f/%.1f ms" % (
            name, len(a), len(b), min(steps) / 27000, max(steps) / 27000))
        # the PCR in each removed packet survives: no more than the
        # source count minus those before the I-frame
        before_i = sum(1 for p in src[:next(i for i, p in enumerate(src)
                       if pid_of(p) == vpid and b"\x00\x00\x01\xb3" in p[4:80])]
                       if pid_of(p) == vpid and p[3] & 0x20 and p[4] >= 7 and p[5] & 0x10)
        assert len(b) == len(a) - before_i, (name, len(a), len(b), before_i)


def test_eos_flush_sends_psi_first():
    """Stream ends while the gate is still waiting: held data is released
    through emit(), so PAT/PMT come first even when none went out while waiting."""
    name = "c1"
    vpid, apids = CLIPS[name]
    src = load(name)
    first = next(i for i, p in enumerate(src)
                 if pid_of(p) == vpid and b"\x00\x00\x01\xb3" in p[4:80])
    pre = src[:first]
    old = (bt.PSI_SEC, bt.PSI_PKTS)
    bt.PSI_SEC, bt.PSI_PKTS = 1e12, 1 << 30        # no periodic / wait-time PSI
    try:
        s = bt.Session("eos")
        s.es = {vpid: 0x02, apids[0]: 0x81, apids[1]: 0x81}
        s.pcr_pid, s.pmt_pid = vpid, 0x1000
        s.gate = bt.GopGate(budget_sec=1e9, budget_bytes=1 << 30)
        for p in pre:
            s.q.setdefault(pid_of(p), collections.deque()).append(p)
        s.last_push = time.time()
        out = []
        for p in s.packets():
            out.append(p)
            if not any(s.q.values()):
                s.last_push = 0
    finally:
        bt.PSI_SEC, bt.PSI_PKTS = old
    assert s.gate.reason == "eos"
    assert pid_of(out[0]) == bt.OUT_PAT and pid_of(out[1]) == bt.OUT_PMT
    media = [p for p in out if pid_of(p) not in (bt.OUT_PAT, bt.OUT_PMT)]
    assert len(media) == len(pre) and out[2:2 + 1] == media[:1]
    for pid in {pid_of(p) for p in pre}:
        assert [p for p in media if pid_of(p) == pid] == [p for p in pre if pid_of(p) == pid]


def test_psi_while_waiting():
    """While the gate waits (no sequence header yet) PAT/PMT keep flowing every
    ~PSI_SEC with correct continuity counters; the wait stays bounded."""
    name = "c1"
    vpid, apids = CLIPS[name]
    src = load(name)
    first = next(i for i, p in enumerate(src)
                 if pid_of(p) == vpid and b"\x00\x00\x01\xb3" in p[4:80])
    pre = src[:first]
    s = bt.Session("wait")
    s.es = {vpid: 0x02, apids[0]: 0x81, apids[1]: 0x81}
    s.pcr_pid, s.pmt_pid = vpid, 0x1000
    s.gate = bt.GopGate(budget_sec=0.6, budget_bytes=1 << 30)
    for p in pre:
        s.q.setdefault(pid_of(p), collections.deque()).append(p)
    s.last_push = time.time()          # stream stays "alive": no stale exit
    out, times, nmedia = [], [], 0
    t0 = time.time()
    for p in s.packets():
        out.append(p)
        times.append(time.time() - t0)
        if pid_of(p) not in (bt.OUT_PAT, bt.OUT_PMT):
            nmedia += 1
            if nmedia == len(pre):
                s.alive = False
    elapsed = time.time() - t0
    assert s.gate.state == "pass" and s.gate.reason == "timeout"
    assert 0.55 <= elapsed < 3, elapsed                      # bounded
    k = _skip_psi(out)
    n_pat = sum(1 for p in out[:k] if pid_of(p) == bt.OUT_PAT)
    assert n_pat >= 4, n_pat                    # ~every 100 ms for 0.6 s
    assert [p for p in out if pid_of(p) not in (bt.OUT_PAT, bt.OUT_PMT)] == \
        [p for p in out[k:] if pid_of(p) not in (bt.OUT_PAT, bt.OUT_PMT)]
    for pid in (bt.OUT_PAT, bt.OUT_PMT):
        ccs = [p[3] & 0x0F for p in out if pid_of(p) == pid]
        assert all(b == (a + 1) & 0x0F for a, b in zip(ccs, ccs[1:])), pid
    # PSI packets parse and are CRC-valid
    parse_pat(section_of(out[0]))
    parse_pmt(section_of(out[1]))
    print("  PSI while waiting: %d PAT in %.2fs before first media" % (n_pat, times[k]))


if __name__ == "__main__":
    fails = 0
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for name, fn in tests:
        try:
            fn()
            print("ok  ", name)
        except Exception as e:           # noqa: BLE001
            import traceback
            traceback.print_exc()
            print("FAIL", name, e)
            fails += 1
    print("%d passed, %d failed" % (len(tests) - fails, fails))
    sys.exit(1 if fails else 0)
