"""Control-leg parsing and shared-PMT retune, using real push frames.

Fixtures in tests/fixtures/ were cut from full-snaplen :31061 captures of an M1:
  concurrent tunes of 2.1, 5.1 and 7.1 to three push IPs
  single tunes of 32.1 and 9.1 to one push IP
Control legs whole; video/audio legs as the 72-byte header + the first TS
packets (PAT, PMT). Media headers still carry the full on-wire length.

Measured (control JSON track_meta and each leg's own PAT/PMT agree):
  2.1  program 2, PMT 128      5.1  program 5, PMT 80
  7.1  program 1, PMT 48       9.1 = 32.1  program 3, PMT 48, PIDs 49/52/53
So 7.1 <-> 9.1/32.1 share a PMT but differ in program number (early release
works), while 9.1 <-> 32.1 is a full collision (falls back to the timeout).

Run under pytest, or standalone: python3 tests/test_ctrl_leg.py
"""
import json
import os
import socket
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import brptune as bt

bt.PUSH_STATE_FILE = os.path.join(tempfile.gettempdir(), "test_ctrl_leg_state.json")
bt.SEEDED_DEVS = set()

CH_21 = "0x1130000000002001"
CH_71 = "0x1130000000007001"   # same id pattern as 2.1/9.1; opaque to routing
CH_91 = "0x1130000000009001"
CH_321 = "0x1180000000020001"
IP = "203.0.113.2"
ALL = ("21", "51", "71", "91", "321")


def fx(name):
    with open(os.path.join(HERE, "fixtures", name + ".bin"), "rb") as f:
        b = f.read()
    return b[:72], b[72:]


def new_tuner(devs=("d01", "d02", "d03")):
    t = bt.Tuner.__new__(bt.Tuner)
    t.lock = threading.RLock()
    t.slots = [bt.Slot(d) for d in devs]
    t._released = {}
    t.totals = {"sessions": 0, "qdrops": 0, "qdrop_bursts": 0, "busy": 0,
                "queue_timeouts": 0, "claim_timeouts": 0, "tune_failures": 0}
    t.last_push_at = None
    t.tune_gate = threading.Lock()
    t.last_fail = None
    return t


def mk(t, i, chan_id, seq):
    s = bt.Session(chan_id)
    s.tune_seq = seq
    t.slots[i].sess = s
    return s


def leg(t, name, ip=IP):
    """Route one fixture leg exactly as _conn() does (minus the socket)."""
    h, d = fx(name)
    pn, fam = bt._frame_pat(d)
    return t._bind(bt._frame_pids(d), fam, ip, bt._frame_program(h, d), pn)


# ---------------------------------------------------------------------------
def test_frame_len_both_layouts():
    """Control legs keep their length in bytes 64-67, media legs in 68-71."""
    for ch in ALL:
        h, d = fx("ctrl_" + ch)
        assert h[:4] == b"Data" and h[4:8] == bt.K_CTRL
        assert int.from_bytes(h[68:72], "big") == 0      # the old bug: n == 0
        assert bt._frame_len(h) == len(d) > 0             # 662 / 667
        for kind in ("video", "audio"):
            hm, dm = fx("%s_%s" % (kind, ch))
            assert int.from_bytes(hm[64:68], "big") == 0
            assert bt._frame_len(hm) == int.from_bytes(hm[68:72], "big") > len(dm)


def test_frame_program_is_mpeg_program_number():
    """program_nbr is the MPEG program number, not the channel major:
    9.1 -> 3, 7.1 -> 1, and 9.1 and 32.1 are both program 3."""
    got = {ch: bt._frame_program(*fx("ctrl_" + ch)) for ch in ALL}
    assert got == {"21": 2, "51": 5, "71": 1, "91": 3, "321": 3}, got
    assert bt._chan_major(CH_91) == 9 != got["91"]
    assert bt._chan_major(CH_321) == 32 != got["321"]
    # media legs never yield a program_nbr
    assert bt._frame_program(*fx("video_91")) is None


def test_frame_pat_matches_control_leg():
    """Each media leg's own PAT carries the same program number + PMT as the
    control leg's track_meta, for every channel in the captures."""
    want = {"21": (2, 128), "51": (5, 80), "71": (1, 48), "91": (3, 48),
            "321": (3, 48)}
    for ch in ALL:
        h, d = fx("ctrl_" + ch)
        o = json.loads(d[d.find(b"{"):d.rfind(b"}") + 1])
        tm = o["track_meta"]
        assert (tm["program_nbr"], tm["pmt_pid"]) == want[ch], ch
        for kind in ("video", "audio"):
            assert bt._frame_pat(fx("%s_%s" % (kind, ch))[1]) == want[ch]
        assert bt._frame_family(fx("video_" + ch)[1]) == tm["pmt_pid"]
    assert bt._frame_pat(fx("ctrl_91")[1]) == (None, None)


def test_ctrl_leg_fills_session_meta():
    """A control leg reaches its session and Session.push parses track_meta."""
    t = new_tuner()
    s = mk(t, 2, CH_91, 1)
    h, d = fx("ctrl_91")
    assert leg(t, "ctrl_91") is s and s.expect_prog == 3
    t._feed(s, h, d)
    assert s.meta.get("program_nbr") == 3 and s.pmt_pid == 48
    # video 49 + the active audio track 52; 53 (Spanish) is active=false
    assert s.pcr_pid == 49 and s.es == {49: 0x02, 52: 0x81}


def _stopped(t, i, chan, ch):
    """Run a real old session on slot i through its first legs, then stop()."""
    old = mk(t, i, chan, 1)
    t.slots[i].push_ip = IP
    assert leg(t, "ctrl_" + ch) is old
    assert leg(t, "video_" + ch) is old
    t.stop(old)
    return old


def test_shared_pmt_retune_same_slot_real_frames():
    """7.1 (program 1, PMT 48) stopped, 9.1 (program 3, PMT 48) tuned on the
    same slot: trailing bursts are held, the new tune's control leg + PAT
    program 3 claim at once (no wait for the quarantine to expire)."""
    t = new_tuner()
    _stopped(t, 1, CH_71, "71")
    assert t.slots[1].quar_prog == 1 and t.slots[1].quar_family == 48
    new = mk(t, 1, CH_91, 2)
    # trailing 7.1 media -> held, never reaches the new session
    assert leg(t, "video_71") is None and new.family is None
    # 9.1 media before its control leg -> held (program not yet known)
    assert leg(t, "video_91") is None and new.family is None
    assert leg(t, "ctrl_91") is new and new.expect_prog == 3
    assert leg(t, "video_71") is None, "old program must stay held"
    assert leg(t, "video_91") is new and new.family == 48 and new.pat_prog == 3
    assert leg(t, "audio_91") is new
    assert t.slots[1].quar_until > time.time(), "claimed inside quarantine"
    # review #1: a trailing 7.1 leg *after* the claim is still rejected and
    # does not overwrite the claimed program
    assert leg(t, "video_71") is None and leg(t, "audio_71") is None
    assert new.pat_prog == 3
    # review #5: a late program-1 control leg is dropped, expect_prog kept
    assert leg(t, "ctrl_71") is None and new.expect_prog == 3


def test_ctrl_leg_late_old_control_before_claim():
    """review #5: old control after the new one, before any media claim."""
    t = new_tuner()
    _stopped(t, 1, CH_71, "71")
    new = mk(t, 1, CH_91, 2)
    assert leg(t, "ctrl_91") is new and leg(t, "ctrl_71") is None
    assert new.expect_prog == 3
    assert leg(t, "video_71") is None
    assert leg(t, "video_91") is new and new.pat_prog == 3


def test_old_control_first_does_not_block_new():
    """re-review P1: the old 7.1 control arrives *before* 9.1's. It is not
    recorded, so 9.1's own control still sets expect_prog and 9.1 claims
    early; trailing 7.1 media stays held."""
    t = new_tuner()
    _stopped(t, 1, CH_71, "71")
    new = mk(t, 1, CH_91, 2)
    assert leg(t, "ctrl_71") is None and new.expect_prog is None
    assert leg(t, "video_71") is None
    assert leg(t, "ctrl_91") is new and new.expect_prog == 3
    assert leg(t, "video_91") is new and new.pat_prog == 3
    assert t.slots[1].quar_until > time.time()


def test_stale_control_not_fed_after_accept():
    """re-review P2: once 9.1's control is accepted, a late 7.1 control is
    dropped by _bind, so it never reaches Session.push (meta stays 9.1's)."""
    t = new_tuner()
    _stopped(t, 1, CH_71, "71")
    new = mk(t, 1, CH_91, 2)
    h, d = fx("ctrl_91")
    assert leg(t, "ctrl_91") is new
    t._feed(new, h, d)
    assert leg(t, "ctrl_71") is None
    assert new.expect_prog == 3 and new.meta.get("program_nbr") == 3


def test_old_control_first_never_fed_or_learned():
    """re-review 3: 2.1 (program 2, PIDs 129/132/133) stopped on a slot, then
    9.1 tuned; a stale 2.1 control leg arriving first over a real socket is
    dropped before Session.push, so no 2.1 PIDs leak into 9.1's PMT. Also on
    the IP-learning path (slot has no push IP yet), it learns nothing."""
    for learned in (True, False):
        t = new_tuner()
        _stopped(t, 1, CH_21, "21")
        assert t.slots[1].quar_prog == 2
        if not learned:
            t.slots[1].push_ip = None
        new = mk(t, 1, CH_91, 2)
        assert leg(t, "ctrl_21") is None
        assert t.slots[1].push_ip == (IP if learned else None)
        ok = leg(t, "ctrl_91")
        assert ok is new and new.expect_prog == 3
        t._feed(new, *fx("ctrl_91"))
        assert set(new.es) == {49, 52} and not set(new.es) & {129, 132, 133}
        assert new.meta.get("program_nbr") == 3


def test_same_program_control_leaves_timeout_path():
    """32.1 -> 9.1: 9.1's control (program 3) equals the quarantined program,
    so it is not recorded; media holds until expiry, then claims."""
    t = new_tuner()
    _stopped(t, 1, CH_321, "321")
    new = mk(t, 1, CH_91, 2)
    assert leg(t, "ctrl_91") is None and new.expect_prog is None
    assert leg(t, "video_91") is None
    t.slots[1].quar_until = time.time() - 0.01
    assert leg(t, "video_91") is new and new.pat_prog == 3


def test_family_fallback_rejects_trailing_old_program():
    """re-review P3: with no local IP (family fallback), a claimed PMT-48
    session for 9.1 rejects a trailing 7.1 (program 1) leg in quarantine."""
    t = new_tuner()
    _stopped(t, 1, CH_71, "71")
    new = mk(t, 1, CH_91, 2)
    assert leg(t, "ctrl_91") is new
    assert leg(t, "video_91") is new and new.pat_prog == 3
    assert leg(t, "video_71", ip=None) is None
    assert leg(t, "video_91", ip=None) is new


def test_family_fallback_shared_pmt_early_release():
    """re-review 4: with no local IP (family fallback), 7.1 -> 9.1 on one
    slot: trailing 7.1 media is held by the _released gate, 9.1 media is let
    through once 9.1's control leg announced program 3; without a control
    leg it still waits for the quarantine."""
    for with_ctrl in (True, False):
        t = new_tuner()
        _stopped(t, 1, CH_71, "71")
        assert 48 in t._released
        new = mk(t, 1, CH_91, 2)
        assert leg(t, "video_71", ip=None) is None
        if with_ctrl:
            assert leg(t, "ctrl_91", ip=None) is new and new.expect_prog == 3
            assert leg(t, "video_71", ip=None) is None
            assert leg(t, "video_91", ip=None) is new and new.family == 48
        else:
            assert leg(t, "video_91", ip=None) is None and new.family is None


def test_bind_is_atomic_under_tuner_lock():
    """review #3: _bind holds self.lock across routing + state updates."""
    t = new_tuner()
    s = mk(t, 0, CH_91, 1)
    t.slots[0].push_ip = IP
    seen = []
    orig = t._bind_route

    def spy(*a, **k):
        seen.append(t.lock._is_owned())
        return orig(*a, **k)
    t._bind_route = spy
    assert leg(t, "ctrl_91") is s and seen == [True]


def test_same_program_collision_falls_back_to_timeout():
    """32.1 -> 9.1 on the same slot: both are program 3 / PMT 48 / PIDs
    49/52/53, so nothing in the frames tells them apart. The burst stays held
    until the quarantine expires (documented caveat), then claims."""
    t = new_tuner()
    _stopped(t, 1, CH_321, "321")
    assert t.slots[1].quar_prog == 3 and t.slots[1].quar_family == 48
    new = mk(t, 1, CH_91, 2)
    # 9.1's control = program 3 = the quarantined program: dropped
    assert leg(t, "ctrl_91") is None and new.expect_prog is None
    assert leg(t, "video_91") is None and new.family is None
    assert leg(t, "video_321") is None
    t.slots[1].quar_until = time.time() - 0.01   # quarantine expired
    assert leg(t, "video_91") is new and new.family == 48


def test_conn_reads_ctrl_leg_over_socket():
    """End-to-end through _conn(): a control leg sent over a real socket is
    no longer dropped (the 9b15358 bug) and lands in the session."""
    t = new_tuner()
    s = mk(t, 0, CH_91, 1)
    t.slots[0].push_ip = "127.0.0.1"
    a, b = socket.socketpair()
    h, d = fx("ctrl_91")

    class Wrap:
        """socketpair is AF_UNIX: report an IP from getsockname() like a
        real accepted :31061 TCP connection would."""
        def __init__(self, s): self._s = s
        def __getattr__(self, k): return getattr(self._s, k)
        def getsockname(self): return ("127.0.0.1", 31061)

    th = threading.Thread(target=t._conn, args=(Wrap(b),))
    th.start()
    a.sendall(h + d)
    a.close()
    th.join(5)
    assert not th.is_alive()
    assert s.expect_prog == 3 and s.meta.get("pmt_pid") == 48
    assert s.kinds.get(bt.K_CTRL) == 1


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    fails = 0
    for fn in fns:
        try:
            fn()
            print("PASS %s" % fn.__name__)
        except Exception as e:
            fails += 1
            print("FAIL %s: %r" % (fn.__name__, e))
    print("\n%d passed, %d failed" % (len(fns) - fails, fails))
    sys.exit(1 if fails else 0)
