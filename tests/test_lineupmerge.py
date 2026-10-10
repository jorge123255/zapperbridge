"""Offline tests for lineupmerge (fake lineups, fake clock, no sockets)."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import lineupmerge as lm  # noqa: E402


def ch(major, minor, call, cid=None, **kw):
    c = {"chan_id": cid or "0x%04x%04x" % (major, minor), "chan_major": major,
         "chan_minor": minor, "call_letters": call, "hidden": 0}
    c.update(kw)
    return c


def src(name, chans, announced=4, leases=None):
    return {"name": name, "chans": chans, "announced": announced,
            "leases": leases}


A = [ch(2, 1, "WBBM"), ch(5, 1, "WMAQ"), ch(7, 1, "WLS"),
     ch(999, 1, "HLS", cid="hls:x"), ch(9, 1, "WGN", hidden=1)]
B = [ch(5, 1, "WMAQ", cid="0xb5"), ch(7, 1, "WLS", cid="0xb7"),
     ch(32, 1, "WFLD")]


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class Tuners(unittest.TestCase):
    def test_usable(self):
        self.assertEqual(lm.usable_tuners(4), 3)
        self.assertEqual(lm.usable_tuners(4, 2), 2)
        self.assertEqual(lm.usable_tuners(6), 3)       # never above 3
        self.assertEqual(lm.usable_tuners(1), 0)
        self.assertEqual(lm.usable_tuners(0), 0)
        self.assertEqual(lm.usable_tuners(None), 3)
        self.assertEqual(lm.usable_tuners(None, 2), 2)

    def test_total_and_cap(self):
        s = [src("a", A), src("b", B)]
        self.assertEqual(lm.total_tuner_count(s), 6)
        self.assertEqual(lm.total_tuner_count(s, cap=4), 4)
        self.assertEqual(lm.total_tuner_count(s, cap=10), 6)
        self.assertEqual(lm.total_tuner_count([src("a", A, 4, 1), src("b", B, 3)]), 3)


class Merge(unittest.TestCase):
    def test_dedupe_candidates_and_order(self):
        rows, assign, notes = lm.merge([src("a", A), src("b", B)])
        self.assertEqual([r.gn for r in rows], ["2.1", "5.1", "7.1", "32.1"])
        r5 = next(r for r in rows if r.gn == "5.1")
        self.assertEqual([c.m1 for c in r5.candidates], ["a", "b"])
        self.assertEqual(r5.candidates[1].chan_id, "0xb5")  # own chan_id kept
        self.assertEqual(next(r for r in rows if r.gn == "32.1").candidates[0].m1, "b")
        self.assertEqual(notes, [])
        self.assertEqual(r5.key, "v5.1")
        self.assertEqual(r5.xmltv_id, "zb-5.1")

    def test_hidden_and_hls_excluded(self):
        rows, _, _ = lm.merge([src("a", A)])
        self.assertNotIn("999.1", [r.gn for r in rows])
        self.assertNotIn("9.1", [r.gn for r in rows])

    def test_prefix(self):
        rows, _, _ = lm.merge([src("a", A)], id_prefix="cab")
        self.assertEqual(rows[0].xmltv_id, "zb-cab-2.1")

    def test_collision_gets_new_minor_not_dropped(self):
        a = [ch(4, 1, "WXYZ"), ch(4, 2, "WXYZ-2")]
        b = [ch(4, 1, "KOTHER")]
        rows, assign, notes = lm.merge([src("a", a), src("b", b)])
        self.assertEqual(len(rows), 3)
        gns = sorted(r.gn for r in rows)
        self.assertEqual(len(set(gns)), 3)
        self.assertEqual(next(r for r in rows if r.name == "KOTHER").gn, "4.3")
        self.assertEqual(len(notes), 1)

    def test_same_call_two_numbers_keeps_names_unique(self):
        a = [ch(8, 1, "WILC"), ch(8, 2, "WILC")]
        rows, _, _ = lm.merge([src("a", a)])
        self.assertEqual([r.name for r in rows], ["WILC 8.1", "WILC 8.2"])

    def test_call_case_insensitive(self):
        rows, _, _ = lm.merge([src("a", [ch(5, 1, "wmaq")]),
                               src("b", [ch(5, 1, "WMAQ ")])])
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0].candidates), 2)

    def test_stable_when_order_changes_and_m1_added(self):
        a = [ch(4, 1, "WXYZ")]
        b = [ch(4, 1, "KOTHER")]
        rows1, assign1, _ = lm.merge([src("a", a), src("b", b)])
        g1 = {r.ident: r.gn for r in rows1}
        # reversed order, with previous assignments: nobody renumbers
        rows2, assign2, _ = lm.merge([src("b", b), src("a", a)], assign1)
        self.assertEqual({r.ident: r.gn for r in rows2}, g1)
        # without them the order decides (shows why persistence matters)
        rows3, _, _ = lm.merge([src("b", b), src("a", a)])
        self.assertNotEqual({r.ident: r.gn for r in rows3}, g1)
        # a third M1 with a station that wants an existing number
        c = [ch(4, 1, "KNEW")]
        rows4, assign4, _ = lm.merge([src("a", a), src("b", b), src("c", c)], assign2)
        g4 = {r.ident: r.gn for r in rows4}
        for k, v in g1.items():
            self.assertEqual(g4[k], v)
        self.assertEqual(len(set(g4.values())), 3)

    def test_missing_m1_number_stays_reserved(self):
        a = [ch(4, 1, "WXYZ")]
        b = [ch(4, 1, "KOTHER")]
        _, as1, _ = lm.merge([src("a", a), src("b", b)])
        other = as1["4.1|KOTHER"]
        # b offline: a newcomer that wants KOTHER's number must not take it
        n = [ch(4, int(other.split(".")[1]), "KNEW")]
        rows, as2, _ = lm.merge([src("a", a), src("n", n)], as1)
        self.assertNotEqual(as2["%s|KNEW" % other], other)
        # b returns: it gets its old number back
        rows, as3, _ = lm.merge([src("a", a), src("b", b), src("n", n)], as2)
        self.assertEqual(as3["4.1|KOTHER"], other)

    def test_assignments_roundtrip_and_garbage(self):
        _, a, _ = lm.merge([src("a", A)])
        self.assertEqual(lm.load_assignments(lm.dump_assignments(a)), a)
        for bad in ("", "{", "[]", '{"version": 9, "assign": {}}', None):
            self.assertEqual(lm.load_assignments(bad), {})

    def test_lineup_rows_and_version(self):
        rows, _, _ = lm.merge([src("a", A), src("b", B)])
        j = lm.lineup_rows(rows, "192.0.2.60:80", 5004)
        self.assertEqual(j[1]["URL"], "http://192.0.2.60:5004/auto/v5.1")
        self.assertEqual(j[1]["Candidates"], ["a", "b"])
        v1 = lm.lineup_version(rows)
        rows2, _, _ = lm.merge([src("a", A), src("b", B + [ch(50, 1, "NEW")])])
        self.assertNotEqual(v1, lm.lineup_version(rows2))
        self.assertEqual(v1, lm.lineup_version(rows))


def router(a=A, b=B, **kw):
    s = [src("a", a), src("b", b)]
    rows, _, _ = lm.merge(s)
    clk = Clock()
    return lm.Router(rows, s, clk, **kw), clk


class Routing(unittest.TestCase):
    def test_only_carrier_chosen(self):
        r, _ = router()
        self.assertEqual(r.candidates("v2.1"), ["a"])
        self.assertEqual(r.candidates("v32.1"), ["b"])
        self.assertEqual(r.candidates("v99.9"), [])

    def test_prefers_emptiest_then_order(self):
        r, _ = router()
        self.assertEqual(r.candidates("v5.1"), ["a", "b"])
        ok = lambda m, cid, c: True  # noqa: E731
        l1 = r.tune("v5.1", ok)
        self.assertEqual(l1.m1, "a")
        l2 = r.tune("v5.1", ok)           # a has 2 free, b 3 -> b
        self.assertEqual(l2.m1, "b")
        self.assertEqual(l2.chan_id, "0xb5")

    def test_uses_free_tuner_only_and_busy(self):
        r, _ = router()
        ok = lambda m, cid, c: True  # noqa: E731
        got = [r.tune("v2.1", ok) for _ in range(3)]   # a: 3 usable
        self.assertTrue(all(g.m1 == "a" for g in got))
        with self.assertRaises(lm.Busy):
            r.tune("v2.1", ok)                          # only a carries it
        r.release(got[0])
        self.assertEqual(r.tune("v2.1", ok).m1, "a")

    def test_overflow_to_second_m1(self):
        r, _ = router()
        ok = lambda m, cid, c: True  # noqa: E731
        seen = [r.tune("v5.1", ok).m1 for _ in range(6)]
        self.assertEqual(sorted(seen), ["a"] * 3 + ["b"] * 3)
        with self.assertRaises(lm.Busy):
            r.tune("v5.1", ok)

    def test_failover_and_bench(self):
        r, clk = router(bench_sec=30)
        calls = []

        def tune(m, cid, c):
            calls.append(m)
            if m == "a":
                raise RuntimeError("no lock")
            return True
        lease = r.tune("v5.1", tune)
        self.assertEqual(lease.m1, "b")
        self.assertEqual(calls, ["a", "b"])
        self.assertEqual(r.in_use("a"), 0)          # failed reservation freed
        self.assertTrue(r.snapshot()["a"]["benched"])
        calls.clear()
        r.tune("v5.1", tune)                        # a skipped while benched
        self.assertEqual(calls, ["b"])
        clk.t += 31
        calls.clear()
        r.tune("v5.1", tune)                        # bench over: a tried again
        self.assertEqual(calls[0], "a")

    def test_falsy_return_fails_over_and_all_fail(self):
        r, _ = router()
        with self.assertRaises(lm.TuneFailed) as cm:
            r.tune("v5.1", lambda m, cid, c: False)
        self.assertEqual([t[0] for t in cm.exception.tried], ["a", "b"])
        self.assertEqual(r.in_use("a") + r.in_use("b"), 0)
        with self.assertRaises(lm.Busy):            # both benched now
            r.tune("v5.1", lambda m, cid, c: True)

    def test_down_m1_skipped_and_count(self):
        r, _ = router()
        self.assertEqual(r.tuner_count(), 6)
        r.mark_up("a", False)
        self.assertEqual(r.candidates("v5.1"), ["b"])
        self.assertEqual(r.tuner_count(), 3)
        with self.assertRaises(lm.Busy):
            r.tune("v2.1", lambda *a: True)
        r.mark_up("a", True)
        self.assertEqual(r.tuner_count(), 6)

    def test_cap(self):
        r, _ = router(cap=4)
        self.assertEqual(r.tuner_count(), 4)

    def test_unknown_channel(self):
        r, _ = router()
        with self.assertRaises(KeyError):
            r.tune("v1.1", lambda *a: True)

    def test_tuner_leases_use_announced_minus_one(self):
        s = [src("a", A, announced=2)]
        rows, _, _ = lm.merge(s)
        r = lm.Router(rows, s, Clock())
        ok = lambda m, cid, c: True  # noqa: E731
        r.tune("v2.1", ok)
        with self.assertRaises(lm.Busy):
            r.tune("v2.1", ok)
        self.assertEqual(r.snapshot()["a"]["total"], 1)

    def test_tune_does_not_hold_lock_during_callback(self):
        # a tune_fn may call back into the router (e.g. snapshot) safely
        r, _ = router()
        r.tune("v2.1", lambda m, cid, c: bool(r.snapshot()))


class IdentityMatching(unittest.TestCase):
    """Guide number + call letters identity across M1s; 999.x HLS excluded."""

    def test_same_number_different_call_letters_is_separate_next_free_minor(self):
        rows, assign, notes = lm.merge([src("a", [ch(4, 1, "WAAA")]),
                                        src("b", [ch(4, 1, "WBBB")])])
        self.assertEqual([r.gn for r in rows], ["4.1", "4.2"])
        r41, r42 = rows
        self.assertEqual((r41.name, [c.m1 for c in r41.candidates]), ("WAAA", ["a"]))
        self.assertEqual((r42.name, [c.m1 for c in r42.candidates]), ("WBBB", ["b"]))
        # the renumbered one keeps its own M1 chan_id (tune uses the M1's id)
        self.assertEqual(r42.candidates[0].chan_id, "0x00040001")
        self.assertEqual(len(notes), 1)
        self.assertIn("4.2", notes[0])

    def test_next_free_minor_skips_naturally_used_minors(self):
        a = [ch(4, 1, "WAAA"), ch(4, 2, "WAAA2"), ch(4, 3, "WAAA3")]
        b = [ch(4, 1, "WBBB")]
        rows, _, _ = lm.merge([src("a", a), src("b", b)])
        self.assertEqual(next(r for r in rows if r.name == "WBBB").gn, "4.4")
        self.assertEqual(len({r.gn for r in rows}), 4)

    def test_two_collisions_get_distinct_minors_same_major(self):
        rows, _, notes = lm.merge([src("a", [ch(4, 1, "WAAA")]),
                                   src("b", [ch(4, 1, "WBBB")]),
                                   src("c", [ch(4, 1, "WCCC")])])
        self.assertEqual([r.gn for r in rows], ["4.1", "4.2", "4.3"])
        self.assertEqual([r.name for r in rows], ["WAAA", "WBBB", "WCCC"])
        self.assertEqual(len(notes), 2)
        for r in rows:
            self.assertTrue(r.gn.startswith("4."))

    def test_collision_never_changes_first_stations_number(self):
        rows, _, _ = lm.merge([src("a", [ch(4, 1, "WAAA")]),
                               src("b", [ch(4, 1, "WBBB")])])
        self.assertEqual(next(r for r in rows if r.name == "WAAA").gn, "4.1")

    def test_same_call_letters_different_numbers_stay_separate(self):
        rows, _, notes = lm.merge([src("a", [ch(8, 1, "WILC")]),
                                   src("b", [ch(8, 2, "WILC")])])
        self.assertEqual([r.gn for r in rows], ["8.1", "8.2"])
        self.assertEqual([[c.m1 for c in r.candidates] for r in rows], [["a"], ["b"]])
        self.assertEqual(notes, [])  # different numbers: no renumber needed
        # same call letters twice -> names disambiguated with the number
        self.assertEqual([r.name for r in rows], ["WILC 8.1", "WILC 8.2"])
        self.assertEqual(len({r.key for r in rows}), 2)

    def test_same_call_different_major_not_merged(self):
        rows, _, _ = lm.merge([src("a", [ch(5, 1, "WMAQ")]),
                               src("b", [ch(15, 1, "WMAQ")])])
        self.assertEqual(len(rows), 2)
        self.assertEqual([r.gn for r in rows], ["5.1", "15.1"])

    def test_exact_match_merges_and_mixed_case_whitespace_ok(self):
        rows, _, notes = lm.merge([src("a", [ch(5, 1, "WMAQ")]),
                                   src("b", [ch(5, 1, " wmaq")]),
                                   src("c", [ch(5, 1, "WLS")])])
        r = next(x for x in rows if x.gn == "5.1")
        self.assertEqual([c.m1 for c in r.candidates], ["a", "b"])
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(notes), 1)  # WLS collided and was renumbered

    def test_same_m1_same_identity_listed_once(self):
        rows, _, _ = lm.merge([src("a", [ch(5, 1, "WMAQ", cid="x1"),
                                         ch(5, 1, "WMAQ", cid="x2")])])
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0].candidates), 1)

    def test_hls_windows_excluded_from_every_m1(self):
        hls_a = ch(999, 1, "HLS", cid="hls:a")
        hls_b = ch(999, 1, "HLS", cid="hls:b")
        rows, assign, notes = lm.merge([src("a", A + [hls_a]), src("b", B + [hls_b])])
        self.assertFalse([r for r in rows if r.gn.startswith("999.")])
        self.assertFalse([r for r in rows if r.name.startswith("HLS")])
        self.assertFalse([k for k in assign if k.startswith("999.")])
        for r in rows:
            for c in r.candidates:
                self.assertFalse(str(c.chan_id).startswith("hls:"))
        self.assertEqual(notes, [])

    def test_hls_detected_by_major_or_prefix_alone(self):
        self.assertTrue(lm.is_hls(ch(999, 7, "X", cid="0x1")))      # 999.x, plain id
        self.assertTrue(lm.is_hls(ch(3, 1, "X", cid="hls:foo")))    # hls: id, normal major
        self.assertFalse(lm.is_hls(ch(9, 99, "X")))
        rows, _, _ = lm.merge([src("a", [ch(999, 2, "X", cid="0x1"),
                                         ch(3, 1, "Y", cid="hls:foo"),
                                         ch(3, 2, "Z")])])
        self.assertEqual([r.name for r in rows], ["Z"])

    def test_hls_does_not_consume_or_collide_with_numbers(self):
        # a 999.x window on one M1 must not reserve/shift anything or merge
        rows, _, notes = lm.merge([src("a", [ch(999, 1, "HLS", cid="hls:a"),
                                             ch(2, 1, "WBBM")]),
                                   src("b", [ch(999, 1, "OTHER", cid="hls:b"),
                                             ch(2, 1, "WBBM")])])
        self.assertEqual([r.gn for r in rows], ["2.1"])
        self.assertEqual([c.m1 for c in rows[0].candidates], ["a", "b"])
        self.assertEqual(notes, [])

    def test_router_never_offers_hls_key(self):
        srcs = [src("a", A), src("b", B)]
        rows, _, _ = lm.merge(srcs)
        r = lm.Router(rows, srcs, Clock())
        self.assertEqual(r.candidates("v999.1"), [])
        with self.assertRaises(KeyError):
            r.tune("v999.1", lambda m, cid, c: True)

    def test_lineup_rows_omit_hls(self):
        rows, _, _ = lm.merge([src("a", A)])
        out = lm.lineup_rows(rows, "192.0.2.30")
        self.assertFalse([x for x in out if x["GuideNumber"].startswith("999.")])


if __name__ == "__main__":
    unittest.main()
