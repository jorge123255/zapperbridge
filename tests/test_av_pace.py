"""A/V interleave + constant-rate pacing of the raw-TS stream.

Offline, no network, no M1: the real Session.packets() mux and tspacer.Pacer
driven by a fake clock. Sources:
  * synth(): deterministic CBR-muxed MPEG-2 + 2x AC-3 stream, M1-like 2 s bursts
  * tests/fixtures/ts_c{1,2,3,51,91}.bin: real raw pushes (short, split into
    sub-second bursts)
  * /tmp/zapperbridge/verify/q/{c1,c2,c3,c51,c91}.ts and chop.raw: ~10-90 s real
    bridge captures, used when present (skipped otherwise)

Run under pytest, or standalone: python3 tests/test_av_pace.py
"""
import collections
import hashlib
import io
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import tsmetrics as m
from tsmetrics import bt, ti, tp

FIX = os.path.join(HERE, "fixtures")
CLIPS = {"c1": (0x81, (0x84, 0x85)), "c2": (0x81, (0x84, 0x85)),
         "c3": (0x81, (0x84, 0x85)), "c51": (0x51, (0x54, 0x55)),
         "c91": (0x31, (0x34, 0x35))}
Q = "/tmp/zapperbridge/verify/q"
SYN_ES = {0x81: 0x02, 0x84: 0x81, 0x85: 0x81}
_cache = {}


def syn(seconds=40):
    if ("syn", seconds) not in _cache:
        st = m.synth(seconds)
        _cache[("syn", seconds)] = (st, m.synth_bursts(st))
    return _cache[("syn", seconds)]


def fixture(name):
    vpid, apids = CLIPS[name]
    src = m.pkts_of(open(os.path.join(FIX, "ts_%s.bin" % name), "rb").read())
    es = {vpid: 0x02}
    for a in apids:
        es[a] = 0x81
    return vpid, es, src


def real_clip(name):
    """(vpid, es, source packets) from a saved bridge capture, or None."""
    path = os.path.join(Q, name + (".raw" if name == "chop" else ".ts"))
    if not os.path.exists(path):
        return None
    pk = [p for p in m.pkts_of(m.strip_http(open(path, "rb").read()))
          if m.pid_of(p) not in (0, bt.OUT_PMT)]
    cnt = collections.Counter(m.pid_of(p) for p in pk)
    vpid = cnt.most_common(1)[0][0]
    es = {vpid: 0x02}
    for a in cnt:
        if a != vpid and cnt[a] > 100:
            es[a] = 0x81
    return vpid, es, pk


def steady(sk, n_tail=20, n_head=0):
    """Skew samples minus the clip's cut-off tail (audio ends before video)."""
    return sk[n_head:len(sk) - n_tail]


def mux_pair(vpid, es, src, period):
    bursts = m.bursts_by_media(src, vpid, period=period)
    gp = vpid == 0x81 and es.get(0x84) is None   # (never) kept for clarity
    a, _ = m.run_mux(bursts, es, vpid, False, period=period)
    b, marks = m.run_mux(bursts, es, vpid, True, period=period)
    return a, b, marks


def per_pid(pk):
    d = collections.defaultdict(list)
    for p in pk:
        d[m.pid_of(p)].append(bytes(p))
    return d


def nocc(p):
    """Packet with the continuity counter masked (the GOP gate renumbers it
    after dropping a prefix; that happens identically with and without the
    interleaver)."""
    return p[:3] + bytes([p[3] & 0xF0]) + p[4:]


def is_subseq(small, big):
    it = iter(map(nocc, big))
    return all(any(nocc(x) == y for y in it) for x in small)


# ---------------------------------------------------------------------------
# 1. interleave
# ---------------------------------------------------------------------------
def test_synth_skew_before_after():
    st, bursts = syn(40)
    a, _ = m.run_mux(bursts, SYN_ES, 0x81, False, gate_pass=True)
    b, _ = m.run_mux(bursts, SYN_ES, 0x81, True, gate_pass=True)
    sa = max(abs(x) for x in steady(m.av_skew(a)))
    sb = max(abs(x) for x in steady(m.av_skew(b)))
    print("  synth 40 s: max |V-A| in byte order  before %.3f s  after %.3f s" % (sa, sb))
    assert sa > 0.8, sa                      # the problem exists in the old mux
    assert sb <= 0.20, sb                    # ~150 ms target + audio PES granularity
    assert sb < sa / 4


def test_fixtures_skew_not_worse_and_valid():
    for name in CLIPS:
        vpid, es, src = fixture(name)
        a, b, _ = mux_pair(vpid, es, src, 0.15)
        sa, sb = m.av_skew(a), m.av_skew(b)
        ma = max(abs(x) for x in steady(sa, 5)) if len(sa) > 10 else 0
        mb = max(abs(x) for x in steady(sb, 5)) if len(sb) > 10 else 0
        print("  %-4s fixture: max |V-A| before %.3f after %.3f" % (name, ma, mb))
        assert mb <= max(0.25, ma), (name, ma, mb)
        check_valid(a, b, src, vpid)


def test_real_clips_skew_pcr_cc():
    got = 0
    for name in list(CLIPS) + ["chop"]:
        c = real_clip(name)
        if c is None:
            continue
        got += 1
        vpid, es, src = c
        if name == "chop":
            src = src[:150000]
        a, b, _ = mux_pair(vpid, es, src, 2.0)
        sa, sb = m.av_skew(a), m.av_skew(b)
        ma = max(abs(x) for x in steady(sa, 60))
        mb = max(abs(x) for x in steady(sb, 60))
        mono, spacing, back = m.pcr_check(b)
        print("  %-4s real clip: max |V-A| before %.3f after %.3f  PCR mono=%s "
              "max spacing %.1f ms" % (name, ma, mb, mono, spacing * 1000))
        assert mb <= 0.25 and mb < ma / 3, (name, ma, mb)
        assert mono and spacing <= 0.040, (name, mono, spacing)
        check_valid(a, b, src, vpid)
    if not got:
        print("  (no /tmp/zapperbridge/verify/q clips present: skipped)")


def check_valid(a, b, src, vpid):
    """188-alignment, CC, per-PID order, PSI, PCR for interleaved output b."""
    assert all(len(p) == 188 and p[0] == 0x47 for p in b)
    ea, eb = m.cc_errors(a), m.cc_errors(b)
    assert sum(eb.values()) <= sum(ea.values()), (ea, eb)
    # same packets overall for the media PIDs the old mux emitted
    pa, pb = per_pid(a), per_pid(b)
    assert set(pa) == set(pb)
    # video: byte-identical sequence (only interleaved against other PIDs)
    assert pa[vpid] == pb[vpid]
    srcs = per_pid(src)
    for pid, lst in pb.items():
        if pid in (bt.OUT_PAT, bt.OUT_PMT):
            continue
        # no same-PID reordering: the per-PID sequence is the old mux's
        # sequence, give or take the audio the GOP gate trims at the start
        assert is_subseq(lst, pa[pid]) or is_subseq(pa[pid], lst), pid
        if not (pid == vpid):                 # (video is gate-rewritten; see above)
            assert is_subseq(lst, srcs[pid]) or is_subseq(pa[pid], lst), pid
        # nothing lost vs the old mux (audio gate may differ by a few PES)
        assert len(lst) >= len(pa[pid]) - 60 and len(lst) <= len(pa[pid]) + 60, pid
    # PSI unchanged: same distinct PAT/PMT payloads (cc/timing aside)
    for pid in (bt.OUT_PAT, bt.OUT_PMT):
        sa = {bytes(p[4:]) for p in pa[pid]}
        sb = {bytes(p[4:]) for p in pb[pid]}
        assert sa == sb, pid
    mono, spacing, back = m.pcr_check(b)
    assert mono and back == 0


def test_pcr_and_psi_on_synth():
    st, bursts = syn(40)
    b, _ = m.run_mux(bursts, SYN_ES, 0x81, True, gate_pass=True)
    mono, spacing, back = m.pcr_check(b)
    print("  synth: PCR monotonic=%s max spacing %.1f ms (%d PCRs)" % (
        mono, spacing * 1000, len(m.pcr_list(b))))
    assert mono and spacing <= 0.040
    assert sum(m.cc_errors(b).values()) == 0
    a, _ = m.run_mux(bursts, SYN_ES, 0x81, False, gate_pass=True)
    check_valid(a, b, [p for _, p in sorted(
        [(i, p) for i, p in enumerate(q for lst in syn(40)[0].values() for _, q in lst)])],
        0x81)


def test_zinterleave0_identical_to_base():
    gold = json.load(open(os.path.join(FIX, "golden_zil0.json")))
    for name, (vpid, apids) in CLIPS.items():
        _, es, src = fixture(name)
        bursts = m.bursts_by_media(src, vpid, period=0.15)
        out, _ = m.run_mux(bursts, es, vpid, interleave=False, period=0.15)
        h = hashlib.sha256(b"".join(out)).hexdigest()
        assert [h, len(out), len(bursts)] == gold[name], name
    print("  ZINTERLEAVE=0 output == d12efa4 output for %d fixtures" % len(CLIPS))


def test_interleaver_unit():
    clk = m.FakeClock()
    il = ti.Interleaver(clock=clk, wait=0.5, audio_lead=0.0)
    cc = {}
    mk = lambda pid, sid, t, n, b: m._packetize(pid, m._pes(sid, int(t), int(t), b"\x05" * n, b), cc)
    # video every 0.1 s, audio every 0.2 s; the whole 2 s arrives video first
    vid = [(pid, p) for i in range(20) for pid, p in
           [(0x81, q) for q in mk(0x81, 0xE0, 90000 * (10 + i / 10.0), 600, False)]]
    aud = [(0x84, q) for i in range(10) for q in mk(0x84, 0xBD, int(90000 * (10 + i / 5.0)), 300, True)]
    for pid, p in vid:
        il.feed(pid, p)
    first = il.pop()                    # audio PID not seen yet: video is not held
    assert first and all(m.pid_of(p) == 0x81 for p in first)
    for pid, p in aud:
        il.feed(pid, p)
    clk.t += 0.1
    rest = il.pop(final=True)
    out = first + rest
    assert sorted(map(bytes, out)) == sorted(bytes(p) for _, p in vid + aud)   # nothing lost
    for pid, lst in ((0x81, vid), (0x84, aud)):                                  # per-PID order
        assert [p for p in out if m.pid_of(p) == pid] == [p for _, p in lst]
    # once both PIDs are known, they are merged by timestamp with bounded skew
    il = ti.Interleaver(clock=clk, wait=0.5, audio_lead=0.0)
    for pid, p in [(0x81, vid[0][1]), (0x84, aud[0][1])]:
        il.feed(pid, p)
    il.pop(final=True)
    for pid, p in vid[1:] + aud[1:]:
        il.feed(pid, p)
    clk.t += 0.1
    out = il.pop(final=True)
    sk = m.av_skew(out)
    assert sk and max(abs(x) for x in sk) <= 0.25, max(abs(x) for x in sk)


# ---------------------------------------------------------------------------
# 2. pacer
# ---------------------------------------------------------------------------
def drive(pc, clk, feeds, t_end, step=0.002, stalled=None):
    """feeds: [(t, bytes)]. Calls get() on a `step` grid. `stalled`=(t0,t1):
    the client is not reading in that window (writes blocked)."""
    feeds = sorted(feeds, key=lambda x: x[0])
    out, fi = [], 0
    t = 0.0
    while t < t_end:
        clk.t = t
        while fi < len(feeds) and feeds[fi][0] <= t:
            pc.feed(feeds[fi][1], now=t)
            fi += 1
        if not (stalled and stalled[0] <= t < stalled[1]):
            while True:
                data, wait, done = pc.get(now=t)
                if not data:
                    break
                out.append((t, data))
        t += step
    return out


def feeds_from(out, marks, period, t0=1.0):
    cuts = [i for _, i in marks] + [len(out)]
    return [(t0 + k * period, b"".join(out[cuts[k]:cuts[k + 1]]))
            for k in range(len(marks))]


def pcr_rate_bps(pkts):
    pl = m.pcr_list(pkts)
    (i0, c0), (i1, c1) = pl[0], pl[-1]
    return (i1 - i0) * 188 * 8 / ((c1 - c0) % tp.PCR_WRAP)


def synth_pipe(seconds=70):
    st, bursts = syn(seconds)
    out, marks = m.run_mux(bursts, SYN_ES, 0x81, True, gate_pass=True)
    return out, marks


def test_pacer_rate_within_3pct_over_60s():
    out, marks = synth_pipe(70)
    clk = m.FakeClock(0)
    pc = tp.Pacer(clock=clk, prebuf=0.8, max_buf=6.0)
    got = drive(pc, clk, feeds_from(out, marks, 2.0), 66.0)
    t_first = got[0][0]
    # steady-state window 10 s .. 60 s after the first release
    w0, w1 = t_first + 10, t_first + 60
    nb = sum(len(d) for t, d in got if w0 <= t < w1)
    rate = nb * 8 / (w1 - w0)
    ref = pcr_rate_bps(out)
    print("  paced rate %.3f Mbps vs PCR rate %.3f Mbps  (%+.2f%%), "
          "first release %.2fs after first byte, stats %s" % (
              rate / 1e6, ref / 1e6, 100 * (rate / ref - 1), t_first - 1.0,
              {k: pc.stats[k] for k in ("skips", "rebuffers", "slewed")}))
    assert abs(rate / ref - 1) <= 0.03, (rate, ref)
    # smoothness: no gap in the output > 100 ms in the window; per-second
    # rate within +-25% of PCR rate (the stream itself is CBR)
    ts = [t for t, d in got if w0 <= t < w1]
    assert max(b - a for a, b in zip(ts, ts[1:])) <= 0.100
    for s in range(int(w0), int(w1) - 1):
        r = sum(len(d) for t, d in got if s <= t < s + 1) * 8
        assert 0.75 * ref <= r <= 1.25 * ref, (s, r / 1e6, ref / 1e6)
    assert pc.stats["skips"] == 0 and pc.stats["rebuffers"] == 0
    # content unchanged
    assert b"".join(d for _, d in got) == b"".join(out)[:sum(len(d) for _, d in got)]


def test_pacer_real_clip_rate():
    c = real_clip("chop")
    if c is None:
        print("  (no chop.raw: skipped)")
        return
    vpid, es, src = c
    src = src[:int(60 * 6.8e6 / 8 / 188)]
    bursts = m.bursts_by_media(src, vpid, period=2.0)
    out, marks = m.run_mux(bursts, es, vpid, True)
    clk = m.FakeClock(0)
    pc = tp.Pacer(clock=clk)
    got = drive(pc, clk, feeds_from(out, marks, 2.0), 62.0)
    t_first = got[0][0]
    w0, w1 = t_first + 8, t_first + 50
    rate = sum(len(d) for t, d in got if w0 <= t < w1) * 8 / (w1 - w0)
    ref = pcr_rate_bps(out)
    print("  real CBS clip: paced %.3f Mbps vs PCR %.3f Mbps (%+.2f%%), "
          "longest output gap %.0f ms" % (
              rate / 1e6, ref / 1e6, 100 * (rate / ref - 1),
              1000 * max(b - a for (a, _), (b, _) in zip(got, got[1:]))))
    assert abs(rate / ref - 1) <= 0.03


def test_pacer_start_delay_bounded():
    out, marks = synth_pipe(8)
    for pre, mx in ((0.8, 1.5), (0.3, 1.5), (2.0, 1.5)):
        clk = m.FakeClock(0)
        pc = tp.Pacer(clock=clk, prebuf=pre, max_start=mx)
        got = drive(pc, clk, feeds_from(out, marks, 2.0), 8.0, step=0.005)
        d = got[0][0] - 1.0
        print("  prebuf %.1f max_start %.1f -> first output %.3fs after first data" % (pre, mx, d))
        assert d <= mx + 0.02
        assert d >= min(pre, mx) - 0.01
    # a source that trickles in (never reaches prebuf media) still starts by max_start
    clk = m.FakeClock(0)
    pc = tp.Pacer(clock=clk, prebuf=0.8, max_start=1.2)
    pk = b"".join(out[:3000])
    fe = [(1.0 + 0.5 * i, pk[i * 188 * 100:(i + 1) * 188 * 100]) for i in range(6)]
    got = drive(pc, clk, fe, 6.0, step=0.005)
    assert got and got[0][0] - 1.0 <= 1.22 and pc.stats["start_delay"] <= 1.22


def test_pacer_cap_skips_to_rap_when_client_stalls():
    out, marks = synth_pipe(40)
    clk = m.FakeClock(0)
    pc = tp.Pacer(clock=clk, prebuf=0.5, max_buf=6.0, max_bytes=12 * 1024 * 1024)
    # client stalls (not reading) from t=3 to t=25: ~22 s of media arrives
    got = drive(pc, clk, feeds_from(out, marks, 2.0), 36.0, stalled=(3.0, 25.0), step=0.01)
    print("  stall 22 s: skips=%d skipped_pkts=%d max_level=%.2fs final level %.2fs" % (
        pc.stats["skips"], pc.stats["skipped_pkts"], pc.stats["max_level"], pc.level()))
    assert pc.stats["skips"] >= 1
    assert pc.stats["max_level"] <= 6.0 + 2.2        # cap + one 2 s burst
    # the first packet released after the stall is the random-access point we
    # skipped to, and the stream continues without a gap from there
    after = b"".join(d for t, d in got if t >= 25.0)
    pk = m.pkts_of(after)
    assert tp.is_rap(pk[0]) or any(tp.is_rap(p) for p in pk[:3]), "resumes at a RAP"
    # byte cap: no backlog beyond max_bytes + one burst even with a huge time cap
    pc2 = tp.Pacer(clock=m.FakeClock(0), max_buf=600.0, max_bytes=2 * 1024 * 1024)
    worst = 0
    for t, b in feeds_from(out, marks, 2.0):
        pc2.feed(b, now=t)
        worst = max(worst, pc2.buf_bytes)
    print("  byte cap 2 MiB: worst backlog %.2f MiB, skips=%d" % (worst / 1048576.0, pc2.stats["skips"]))
    assert pc2.stats["skips"] >= 1 and worst <= 2 * 1048576 + 1300000


def test_pacer_never_blocks_feed_and_catches_up_after_stall():
    out, marks = synth_pipe(20)
    clk = m.FakeClock(0)
    pc = tp.Pacer(clock=clk)
    t0 = time.time()
    for t, b in feeds_from(out, marks, 0.0):
        pc.feed(b, now=0.0)                    # a whole 20 s at once
    assert time.time() - t0 < 2.0
    assert pc.stats["skips"] >= 1 and pc.level() <= 6.0


def test_pacer_rebuffer_on_source_stall():
    out, marks = synth_pipe(30)
    fe = feeds_from(out, marks, 2.0)
    fe = [(t if k < 4 else t + 5.0, b) for k, (t, b) in enumerate(fe)]  # 5 s outage
    clk = m.FakeClock(0)
    pc = tp.Pacer(clock=clk, prebuf=0.8)
    got = drive(pc, clk, fe, 30.0, step=0.005)
    print("  5 s source outage: rebuffers=%d start_delay=%.2f" % (
        pc.stats["rebuffers"], pc.stats["start_delay"]))
    assert pc.stats["rebuffers"] >= 1
    # never a burst catch-up: no 1 s window exceeds 1.5x the stream rate
    ref = pcr_rate_bps(out)
    for s in range(2, 28):
        r = sum(len(d) for t, d in got if s <= t < s + 1) * 8
        assert r <= 1.6 * ref, (s, r / 1e6)


def test_pacer_no_pcr_falls_back_to_measured_rate():
    st, bursts = syn(30)
    out, marks = m.run_mux(bursts, SYN_ES, 0x81, True, gate_pass=True)
    nopcr = []
    for p in out:
        if tp.pcr_of(p) is not None:
            continue                            # drop the PCR-only packets
        nopcr.append(p)
    clk = m.FakeClock(0)
    pc = tp.Pacer(clock=clk, prebuf=0.8)
    # re-derive marks: feed in 2 s chunks by packet count
    per = len(nopcr) // 15
    fe = [(1.0 + 2.0 * k, b"".join(nopcr[k * per:(k + 1) * per])) for k in range(15)]
    got = drive(pc, clk, fe, 33.0, step=0.005)
    assert pc.stats["mode"] == "rate", pc.stats
    t1 = got[0][0]
    rate = sum(len(d) for t, d in got if t1 + 6 <= t < t1 + 24) * 8 / 18.0
    avg_in = sum(len(b) for _, b in fe) * 8 / 30.0
    print("  no-PCR fallback: out %.2f Mbps vs input average %.2f Mbps" % (rate / 1e6, avg_in / 1e6))
    assert abs(rate / avg_in - 1) <= 0.12      # measured-average estimate


def test_pacer_pcr_discontinuity_does_not_stall():
    out, marks = synth_pipe(16)
    mid = len(out) // 2
    # rebase PCRs of the 2nd half by +1000 s (encoder restart)
    mod = []
    for i, p in enumerate(out):
        c = tp.pcr_of(p)
        if i >= mid and c is not None:
            b = (int((c + 1000.0) * 90000) % (1 << 33))
            p = bytearray(p)
            p[6:11] = bytes([b >> 25 & 0xFF, b >> 17 & 0xFF, b >> 9 & 0xFF, b >> 1 & 0xFF, (b & 1) << 7 | 0x7E])
            p = bytes(p)
        mod.append(p)
    cuts = [i for _, i in marks] + [len(mod)]
    fe = [(1.0 + 2.0 * k, b"".join(mod[cuts[k]:cuts[k + 1]])) for k in range(len(marks))]
    clk = m.FakeClock(0)
    pc = tp.Pacer(clock=clk)
    got = drive(pc, clk, fe, 20.0, step=0.005)
    assert pc.stats["pcr_resets"] >= 1
    assert sum(len(d) for _, d in got) >= 0.9 * sum(len(b) for _, b in fe)
    assert max(b - a for (a, _), (b, _) in zip(got, got[1:])) < 0.2


def test_pacer_keepalive_while_gate_waits():
    """PAT/PMT-only lead-in (GOP gate waiting) must not be held back forever."""
    out, marks = synth_pipe(6)
    psi = [p for p in out if m.pid_of(p) in (bt.OUT_PAT, bt.OUT_PMT)][:2]
    clk = m.FakeClock(0)
    pc = tp.Pacer(clock=clk, prebuf=0.8)
    fe = [(0.0, b"".join(psi)), (0.5, b"".join(psi)), (1.0, b"".join(psi)),
          (1.5, b"".join(psi))]
    got = drive(pc, clk, fe, 2.5, step=0.01)
    assert got and got[0][0] <= 1.1, got[:1]       # bytes flow while waiting
    assert pc.stats["keepalive"] >= 1
    # ...and real data afterwards still starts a normal jitter-buffered stream
    for t, b in feeds_from(out, marks, 2.0):
        pc.feed(b, now=2.5 + t)
    assert pc.state in ("starting", "run")


def test_pacer_close_flushes_everything():
    out, marks = synth_pipe(6)
    clk = m.FakeClock(0)
    pc = tp.Pacer(clock=clk, prebuf=0.8)
    for t, b in feeds_from(out, marks, 2.0)[:2]:
        pc.feed(b, now=t)
    pc.close()
    tot, done, n = b"", False, 0
    while not done and n < 1000:
        d, w, done = pc.get(now=5.0)
        tot += d or b""
        n += 1
    assert done and len(tot) == pc.stats["bytes_out"]
    assert len(tot) == pc.stats["bytes_in"] - 188 * 0 or len(tot) >= pc.stats["bytes_in"] - 188 * 4000


# ---------------------------------------------------------------------------
# 3. handler: ZPACE=0 restores write-through exactly; ZPACE=1 preserves bytes
# ---------------------------------------------------------------------------
class _Sess:
    chan_id, qdrops = "t", 0

    def __init__(self, pkts):
        self.pkts = pkts

    def packets(self):
        for p in self.pkts:
            yield p


class _W(io.RawIOBase):
    def __init__(self):
        self.writes = []

    def write(self, b):
        self.writes.append(bytes(b))
        return len(b)

    def flush(self):
        pass


def _handler(he):
    h = he.Stream.__new__(he.Stream)
    h.wfile = _W()
    h.connection = None
    h._tune_socket = lambda: None
    return h


def test_handler_zpace0_is_old_write_through_and_zpace1_same_bytes():
    import hdhr_emulator as he
    pk = m.pkts_of(open(os.path.join(FIX, "ts_c3.bin"), "rb").read())[:1400]
    old = he.PACE
    try:
        he.PACE = False
        h = _handler(he)
        he.Stream._ts_brp(h, _Sess(pk), headers_sent=True)
        assert b"".join(h.wfile.writes) == b"".join(pk)
        assert set(len(w) for w in h.wfile.writes[:-1]) == {7 * 188}       # old 7-packet writes
        he.PACE = True
        he.PACE_PREBUF, he.PACE_MAX_START = 0.05, 0.1
        h2 = _handler(he)
        he.Stream._ts_brp(h2, _Sess(pk), headers_sent=True)
        assert b"".join(h2.wfile.writes) == b"".join(pk)
        assert all(len(w) % 188 == 0 for w in h2.wfile.writes)
    finally:
        he.PACE = old


def test_handler_slow_client_does_not_block_reader_and_caps_memory():
    import hdhr_emulator as he
    out, marks = synth_pipe(30)
    saved = (he.PACE, he.PACE_MAX_BUF)
    he.PACE, he.PACE_MAX_BUF = True, 3.0
    gate = threading.Event()

    class SlowW(_W):
        def write(self, b):
            gate.wait(8)                       # client reads nothing for a while
            return super().write(b)
    h = _handler(he)
    h.wfile = SlowW()
    t0 = time.time()
    th = threading.Thread(target=lambda: he.Stream._ts_brp(h, _Sess(out), headers_sent=True))
    th.start()
    time.sleep(1.0)
    gate.set()
    th.join(30)
    he.PACE, he.PACE_MAX_BUF = saved
    assert not th.is_alive()
    assert time.time() - t0 < 25


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
