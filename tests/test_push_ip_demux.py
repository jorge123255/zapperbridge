"""Unit tests for per-dev push-IP demultiplexing in brptune._bind / stop / health.

Run under pytest, or standalone (`python3 tests/test_push_ip_demux.py`) via the
__main__ runner at the bottom -- the project ships no pytest dependency.

The IPs here are documentation/loopback ranges, never machine-specific:
  127.0.0.1    -> always present (bindable) on any host
  203.0.113.x  -> TEST-NET-3, never a local alias -> treated as "missing"
"""
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import brptune as bt

# Isolate state-file writes and the seeded-dev set from the host environment.
bt.PUSH_STATE_FILE = os.path.join(tempfile.gettempdir(), "test_push_ips_state.json")
bt.SEEDED_DEVS = set()

DEVS = ["d01", "d02", "d03"]
# Real BRP chan_ids: major = (v >> 12) & 0xFFF. Measured: 9.1 and 32.1 are both
# program 3 / PMT 48 (identical); 7.1 is program 1 / PMT 48.
CH_21 = "0x1130000000002001"   # major 2
CH_71 = "0x1130000000007001"   # major 7 (same id pattern)
CH_91 = "0x1130000000009001"   # major 9
CH_321 = "0x1180000000020001"  # major 32 (0x20)
PMT_21, PMT_32, PMT_9 = 128, 48, 48


def new_tuner(devs=DEVS):
    t = bt.Tuner.__new__(bt.Tuner)          # no __init__ -> never binds :31061
    t.lock = threading.RLock()
    t.slots = [bt.Slot(d) for d in devs]
    t._released = {}
    t.totals = {"sessions": 0, "qdrops": 0, "qdrop_bursts": 0,
                "busy": 0, "queue_timeouts": 0, "claim_timeouts": 0,
                "tune_failures": 0}
    t.last_push_at = None
    t.tune_gate = threading.Lock()
    t.last_fail = None
    return t


def mk(t, i, chan_id, tune_seq, family=None):
    s = bt.Session(chan_id)
    s.tune_seq = tune_seq
    s.family = family
    t.slots[i].sess = s
    return s


# ---------------------------------------------------------------------------
def test_shared_pmt_routed_by_ip():
    """S1: 9.1 and 32.1 share PMT 48 but route to distinct sessions by IP."""
    t = new_tuner()
    s21 = mk(t, 0, CH_21, 1)
    assert t._bind(set(), PMT_21, "203.0.113.1") is s21
    assert t.slots[0].push_ip == "203.0.113.1" and s21.family == 128
    s32 = mk(t, 1, CH_321, 2)
    assert t._bind(set(), PMT_32, "203.0.113.2") is s32
    s9 = mk(t, 2, CH_91, 3)
    r = t._bind(set(), PMT_9, "203.0.113.3")
    assert r is s9, "9.1 must route by IP, not collide onto 32.1 (shared PMT 48)"
    assert len({id(s21), id(s32), id(s9)}) == 3
    # a later ES-only burst (no PAT) on 9.1's IP still routes to its session
    assert t._bind({0x31, 0x32}, None, "203.0.113.3") is s9


def test_legacy_shared_pmt_collides_without_ip():
    """S2: REPRODUCTION OF THE OLD BUG -- without a local IP, shared PMT 48
    hands 9.1's burst to the already-streaming 32.1 session (the collision
    this PR fixes)."""
    t = new_tuner()
    s32 = mk(t, 1, CH_321, 2, family=48)     # 32.1 already streaming
    s9 = mk(t, 2, CH_91, 3)                   # 9.1 fresh, also PMT 48
    r = t._bind(set(), 48, None)             # no IP -> legacy family routing
    assert r is s32 and s9.family is None, "legacy path collides onto 32.1"


def test_family_fallback_distinct_pmt():
    """S3: family fallback still works when the local IP is unknown."""
    t = new_tuner()
    s21 = mk(t, 0, CH_21, 1)
    assert t._bind(set(), 128, None) is s21 and s21.family == 128


def test_leftover_push_after_stop_same_slot():
    """A trailing burst of the just-stopped channel must not claim the new
    session on the same reused slot/IP."""
    t = new_tuner()
    a = mk(t, 0, CH_21, 1)
    assert t._bind(set(), 128, "203.0.113.1") is a and a.family == 128
    t.stop(a)
    assert t.slots[0].quar_family == 128 and t.slots[0].push_ip == "203.0.113.1"
    b = mk(t, 0, CH_91, 2)                    # new channel, different PMT
    # trailing OLD burst (PMT 128) within the quarantine window -> dropped
    assert t._bind(set(), 128, "203.0.113.1") is None
    assert b.family is None, "old burst must not claim the new session"
    # the real new burst (PMT 48) claims it
    assert t._bind(set(), 48, "203.0.113.1") is b and b.family == 48


def test_shared_pmt_retune_needs_program_nbr():
    """When old and new share a PMT on the same slot, hold the ambiguous burst
    until the new tune's control leg announces its program_nbr and the media
    leg's PAT program_number matches it. Measured: 7.1 = program 1 / PMT 48,
    9.1 = program 3 / PMT 48. program_nbr is the MPEG program number, not the
    channel major (9)."""
    t = new_tuner()
    a = mk(t, 1, CH_71, 1)                    # 7.1, program 1, PMT 48
    assert t._bind(set(), 48, "203.0.113.2", pn=1) is a and a.pat_prog == 1
    t.stop(a)
    assert t.slots[1].quar_prog == 1
    b = mk(t, 1, CH_91, 2)                    # 9.1, program 3, PMT 48
    # ambiguous burst (PMT 48, no program known yet) during quarantine -> held
    assert t._bind(set(), 48, "203.0.113.2", pn=3) is None and b.family is None
    # 9.1's control leg (no TS, program_nbr 3) routes to b and records it
    assert t._bind(set(), None, "203.0.113.2", prog=3) is b
    assert b.expect_prog == 3 and b.family is None
    # trailing old-program (1) burst and the major number (9) -> still held
    assert t._bind(set(), 48, "203.0.113.2", pn=1) is None
    assert t._bind(set(), 48, "203.0.113.2", pn=9) is None
    # PAT program 3 matches the control leg -> claim
    assert t._bind(set(), 48, "203.0.113.2", pn=3) is b and b.family == 48
    assert b.pat_prog == 3


def test_same_program_retune_waits_for_quarantine():
    """32.1 -> 9.1 on one slot: both are program 3 / PMT 48 / PIDs 49/52/53
    (measured), so the frames cannot tell them apart; hold until expiry."""
    t = new_tuner()
    a = mk(t, 1, CH_321, 1)
    assert t._bind(set(), 48, "203.0.113.2", pn=3) is a
    t.stop(a)
    b = mk(t, 1, CH_91, 2)
    assert t._bind(set(), None, "203.0.113.2", prog=3) is None
    assert b.expect_prog is None          # == quarantined program: ambiguous
    assert t._bind(set(), 48, "203.0.113.2", pn=3) is None and b.family is None
    t.slots[1].quar_until = time.time() - 0.01
    assert t._bind(set(), 48, "203.0.113.2", pn=3) is b and b.family == 48


def test_same_channel_retune_allowed():
    """Re-tuning the *same* channel on a slot is allowed immediately."""
    t = new_tuner()
    a = mk(t, 0, CH_21, 1)
    assert t._bind(set(), 128, "203.0.113.1") is a
    t.stop(a)
    b = mk(t, 0, CH_21, 2)                    # same channel again
    assert t._bind(set(), 128, "203.0.113.1") is b and b.family == 128


def test_unconfirmed_ip_cleared_on_claim_timeout():
    """An IP learned during a tune that then claim-times-out is dropped, and a
    late burst to it is not routed."""
    t = new_tuner()
    a = mk(t, 0, CH_91, 1)
    # first burst has no PAT (family None) -> learns IP but leaves it unconfirmed
    r = t._bind(set(), None, "203.0.113.5")
    assert r is a and t.slots[0].push_ip == "203.0.113.5"
    assert t.slots[0].push_ip_confirmed is False and a.family is None
    t.stop(a)                                 # claim timeout path (family None)
    assert t.slots[0].push_ip is None, "unconfirmed learned IP must be cleared"
    # late burst to the old IP, no live session -> dropped
    assert t._bind(set(), 48, "203.0.113.5") is None


def test_known_ip_without_live_session_dropped():
    """A burst to a known IP whose slot has no live session is dropped, not
    leaked into family routing."""
    t = new_tuner()
    t.slots[0].push_ip = "203.0.113.1"        # known, but slot0.sess is None
    assert t._bind(set(), 128, "203.0.113.1") is None


def test_no_overwrite_of_known_ip():
    """A burst from a new IP learns into a free slot, never overwriting a slot
    that already has a (seeded/known) IP."""
    t = new_tuner()
    a = mk(t, 0, CH_21, 1)
    t.slots[0].push_ip = "203.0.113.1"
    t.slots[0].push_ip_confirmed = True
    assert t._bind(set(), 128, "203.0.113.1") is a
    b = mk(t, 1, CH_91, 2)
    assert t._bind(set(), 48, "203.0.113.2") is b
    assert t.slots[1].push_ip == "203.0.113.2"
    assert t.slots[0].push_ip == "203.0.113.1", "existing IP must not change"


def test_permuted_mapping_routes_by_ip():
    """Routing follows the IP, not the tune order."""
    t = new_tuner()
    t.slots[0].push_ip, t.slots[1].push_ip = "203.0.113.1", "203.0.113.2"
    t.slots[0].push_ip_confirmed = t.slots[1].push_ip_confirmed = True
    a = mk(t, 0, CH_21, 1)
    b = mk(t, 1, CH_91, 2)
    assert t._bind(set(), 48, "203.0.113.2") is b      # .2 -> slot1
    assert t._bind(set(), 128, "203.0.113.1") is a     # .1 -> slot0


def test_first_burst_no_pat_routes_without_family():
    """A first burst with no PAT (family None) on a known IP routes to the
    owner session without setting its family."""
    t = new_tuner()
    t.slots[0].push_ip = "203.0.113.1"
    t.slots[0].push_ip_confirmed = True
    a = mk(t, 0, CH_21, 1)
    assert t._bind({0x31}, None, "203.0.113.1") is a and a.family is None


def test_conflict_unlearn_after_n():
    """Repeated family conflicts on a learned (non-seeded) IP unlearn it."""
    t = new_tuner()
    a = mk(t, 0, CH_21, 1, family=128)
    t.slots[0].push_ip = "203.0.113.1"
    t.slots[0].push_ip_confirmed = True
    for _ in range(bt.IP_UNLEARN_CONFLICTS):
        assert t._bind(set(), 48, "203.0.113.1") is None   # wrong family
    assert t.slots[0].push_ip is None, "learned IP unlearned after N conflicts"


def test_seeded_ip_never_unlearned():
    """A seeded dev's IP survives conflicts (never auto-unlearned)."""
    t = new_tuner()
    bt.SEEDED_DEVS.add("d01")
    try:
        a = mk(t, 0, CH_21, 1, family=128)
        t.slots[0].push_ip = "203.0.113.1"
        t.slots[0].push_ip_confirmed = True
        for _ in range(bt.IP_UNLEARN_CONFLICTS + 2):
            t._bind(set(), 48, "203.0.113.1")
        assert t.slots[0].push_ip == "203.0.113.1"
    finally:
        bt.SEEDED_DEVS.discard("d01")


def test_parse_push_ips_malformed():
    assert bt._parse_push_ips("") == {}
    assert bt._parse_push_ips(None) == {}
    assert bt._parse_push_ips("garbage,,=,a=,=1.2.3.4") == {}
    assert bt._parse_push_ips("d01=1.2.3.4, bad=999.1, d02=10.0.0.2") == {
        "d01": "1.2.3.4", "d02": "10.0.0.2"}


def test_valid_ip():
    assert bt._valid_ip("1.2.3.4") is True
    assert bt._valid_ip("999.1") is False
    assert bt._valid_ip("nope") is False
    assert bt._valid_ip(None) is False


def test_chan_major():
    assert bt._chan_major(CH_21) == 2
    assert bt._chan_major(CH_91) == 9
    assert bt._chan_major(CH_321) == 32
    assert bt._chan_major("hls:1") is None


def test_ip_local():
    assert bt._ip_local("127.0.0.1") is True
    assert bt._ip_local("203.0.113.9") is False
    assert bt._ip_local(None) is False


def test_health_push_ip_warning():
    t = new_tuner()
    t.slots[0].push_ip = "127.0.0.1"          # present
    t.slots[1].push_ip = "203.0.113.9"        # missing
    h = t.health()
    byip = {e["push_ip"]: e for e in h["push_ips"]}
    assert byip["127.0.0.1"]["present"] is True
    assert byip["203.0.113.9"]["present"] is False
    w = h["push_ip_warning"]
    assert w and "203.0.113.9" in w and "127.0.0.1" not in w


def test_health_no_warning_when_present():
    t = new_tuner()
    t.slots[0].push_ip = "127.0.0.1"
    assert t.health()["push_ip_warning"] is None


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
