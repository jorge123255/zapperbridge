"""Opt-in deeper startup prebuffer (ZSTART_BUF_SEC). Offline, fake clock.

Run under pytest or: python3 tests/test_startup_buffer.py
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
import test_av_pace as ap  # noqa: E402
import tsmetrics as m      # noqa: E402

tp = m.tp
R = tp.resolve_start_buf


# -- env parsing ---------------------------------------------------------------
def test_unset_is_unchanged():
    for raw in (None, "", "  "):
        assert R(raw) == (0.8, 1.5, None)
    assert R(None, 0.5, 1.2, 4.0) == (0.5, 1.2, None)


def test_value_sets_prebuf_and_lifts_max_start():
    p, ms, note = R("2.0")
    assert p == 2.0 and note is None
    assert ms >= 2.7          # not cut short by the old 1.5 s net
    p, ms, _ = R("0.5")       # shallower is allowed; max_start never shrinks
    assert p == 0.5 and ms == 1.5


def test_bad_values_ignored_and_big_values_clamped():
    for bad in ("abc", "0", "-1", "nan", "inf"):
        p, ms, note = R(bad)
        assert (p, ms) == (0.8, 1.5) and note and "ignored" in note, bad
    p, ms, note = R("99")     # clamp to max_buf / 2 = 3 s
    assert p == 3.0 and note and "clamped" in note and ms >= 3.7
    assert R("0.01")[0] == 0.1


def _probe(env_val):
    env = dict(os.environ)
    env.pop("ZSTART_BUF_SEC", None)
    env.pop("ZPACE_PREBUF_MS", None)
    env.pop("ZPACE_MAX_START_MS", None)
    if env_val is not None:
        env["ZSTART_BUF_SEC"] = env_val
    r = subprocess.run(
        [sys.executable, "-c",
         "import hdhr_emulator as h; print(h.PACE_PREBUF, h.PACE_MAX_START)"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return [float(x) for x in r.stdout.strip().splitlines()[-1].split()]


def test_facade_defaults_unchanged_and_env_applies():
    assert _probe(None) == [0.8, 1.5]
    pb, ms = _probe("2.0")
    assert pb == 2.0 and ms >= 2.7


# -- simulated bursty M1 -------------------------------------------------------
def _bursts():
    out, marks = ap.synth_pipe(40)
    return ap.feeds_from(out, marks, 2.0)      # ~2 s of media per burst


def _late(base, delays):
    """Burst k arrives `delays[k]` s after its nominal time (order kept)."""
    res, last = [], 0.0
    for k, (t, b) in enumerate(base):
        t2 = max(t + delays.get(k, 0.0), last + 0.01)
        last = t2
        res.append((t2, b))
    return res


def _run(raw, feeds, end=36.0):
    clk = m.FakeClock(0)
    p, ms, _ = R(raw)
    pc = tp.Pacer(clock=clk, prebuf=p, max_start=ms)
    got = ap.drive(pc, clk, feeds, end, step=0.005)
    return pc, got


def test_bursty_input_fewer_startup_rebuffers_at_2s():
    base = _bursts()
    scenarios = ({3: 1.4}, {2: 1.0, 6: 1.3}, {1: 1.2, 3: 1.0})
    for dl in scenarios:
        feeds = _late(base, dl)
        pc0, _ = _run(None, feeds)
        pc2, got2 = _run("2.0", feeds)
        assert pc0.stats["rebuffers"] >= 1, dl             # default: a stall
        assert pc2.stats["rebuffers"] == 0, dl             # 2.0 s: rides it out
        # same bytes as the default setting (prefix of one another)
        _, got0 = _run(None, feeds)
        x, y = b"".join(d for _, d in got0), b"".join(d for _, d in got2)
        n = min(len(x), len(y))
        assert n > 1000000 and x[:n] == y[:n], dl
        # no output gap over 100 ms once running (steady release)
        ts = [t for t, _ in got2]
        gaps = [b - a for a, b in zip(ts, ts[1:]) if a > ts[0] + 1]
        assert max(gaps) < 0.35, (dl, max(gaps))


def test_no_late_burst_changes_nothing_but_latency():
    feeds = _bursts()                                       # perfectly regular
    pc0, g0 = _run(None, feeds)
    pc2, g2 = _run("2.0", feeds)
    assert pc0.stats["rebuffers"] == pc2.stats["rebuffers"] == 0
    d0, d2 = pc0.stats["start_delay"], pc2.stats["start_delay"]
    assert abs(d0 - 0.8) < 0.05 and abs(d2 - 2.0) < 0.05
    # extra tune latency is about (value - 0.8) s, as documented
    assert abs((g2[0][0] - g0[0][0]) - 1.2) < 0.05
    # same bytes in the same order (one is a prefix of the other)
    x, y = b"".join(d for _, d in g0), b"".join(d for _, d in g2)
    n = min(len(x), len(y))
    assert n > 1000000 and x[:n] == y[:n]


def test_default_max_start_would_have_cut_it_short():
    """Why max_start is lifted: with the old 1.5 s net a 2.0 s prebuf is moot."""
    feeds = _bursts()
    clk = m.FakeClock(0)
    pc = tp.Pacer(clock=clk, prebuf=2.0, max_start=1.5)
    ap.drive(pc, clk, feeds, 10.0, step=0.005)
    assert pc.stats["start_delay"] <= 1.52
    pc2, _ = _run("2.0", feeds, 10.0)
    assert pc2.stats["start_delay"] >= 1.98


def test_slow_trickle_still_starts_by_max_start():
    out, marks = ap.synth_pipe(8)
    pk = b"".join(out[:3000])
    fe = [(1.0 + 0.5 * i, pk[i * 188 * 100:(i + 1) * 188 * 100]) for i in range(6)]
    clk = m.FakeClock(0)
    p, ms, _ = R("2.0")
    pc = tp.Pacer(clock=clk, prebuf=p, max_start=ms)
    got = ap.drive(pc, clk, fe, 8.0, step=0.005)
    assert got and pc.stats["start_delay"] <= ms + 0.02


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
