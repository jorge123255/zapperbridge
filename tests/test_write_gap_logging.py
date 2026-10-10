"""Write-gap logging (ZLOG_DIAG): WriteGapMonitor + paced writer wiring.

Log-only checks; offline. Runs under unittest and pytest."""
import io
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import zdiag
import hdhr_emulator as h
import test_diag_logging as td


class Clk:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def mk(**kw):
    c, lines = Clk(), []
    m = zdiag.WriteGapMonitor("chanX", clock=c, emit=lines.append, **kw)
    return m, c, lines


class Monitor(unittest.TestCase):
    def test_per_second_line_fields(self):
        m, c, L = mk()
        m.on_input(188, 100.01)
        m.on_input(188, 100.05)
        m.on_write(1000, 100.10, 100.10, 0.8)
        m.on_write(1000, 100.20, 100.20, 0.8)
        c.t = 101.0
        m.tick(0.8)
        sec = [x for x in L if x.startswith("wgap sec")]
        self.assertEqual(len(sec), 1)
        s = sec[0]
        for frag in ("out_bytes=2000", "writes=2", "max_write_gap_ms=100",
                     "level=0.80s", "in_bytes=376", "in_reads=2",
                     "max_in_gap_ms=40"):
            self.assertIn(frag, s)

    def test_no_second_line_before_a_second_passes(self):
        m, c, L = mk()
        c.t = 100.5
        m.tick(0.0)
        self.assertEqual(L, [])

    def test_write_gap_over_100ms_logged_with_cause_and_level(self):
        m, c, L = mk()
        m.on_write(10, 100.0, 100.0, 0.5)
        m.on_write(10, 100.3, 100.3, 0.5)
        g = [x for x in L if x.startswith("wgap write_gap")]
        self.assertEqual(len(g), 1)
        self.assertIn("gap_ms=300", g[0])
        self.assertIn("level=0.50s", g[0])
        self.assertIn("cause=pacer_wait", g[0])

    def test_small_gap_not_logged(self):
        m, c, L = mk()
        m.on_write(10, 100.0, 100.0, 0.5)
        m.on_write(10, 100.05, 100.05, 0.5)
        self.assertEqual([x for x in L if x.startswith("wgap write_gap")], [])

    def test_cause_client_backpressure(self):
        m, c, L = mk()
        m.on_write(10, 100.0, 100.0, 0.5)
        m.on_write(10, 100.1, 100.5, 0.5)     # write blocked 400 ms
        self.assertIn("cause=client_backpressure", L[-1])
        self.assertIn("block_ms=400", L[-1])

    def test_cause_buffer_empty(self):
        m, c, L = mk()
        m.on_write(10, 100.0, 100.0, 0.5)
        m.on_write(10, 100.4, 100.4, 0.0, dry=True)
        self.assertIn("cause=buffer_empty", L[-1])

    def test_cause_upstream_gap_and_input_gap_line(self):
        m, c, L = mk()
        m.on_write(10, 100.0, 100.0, 0.5)
        m.on_input(188, 100.0)
        m.on_input(188, 100.4)
        self.assertTrue(any("input_gap" in x and "gap_ms=400" in x
                            and "cause=upstream_gap" in x for x in L))
        m.on_write(10, 100.45, 100.45, 0.5)
        self.assertIn("cause=upstream_gap", L[-1])

    def test_cause_rebuffer_and_rebuffer_timestamp_line(self):
        m, c, L = mk()
        m.on_write(10, 100.0, 100.0, 0.5)
        m.tick(0.0, rebuffers=1)
        self.assertTrue(any(x.startswith("wgap rebuffer") and "n=1" in x for x in L))
        m.tick(0.0, rebuffers=1)              # same count: not logged again
        self.assertEqual(len([x for x in L if x.startswith("wgap rebuffer")]), 1)
        m.on_write(10, 100.3, 100.3, 0.8)
        self.assertIn("cause=rebuffer", L[-1])

    def test_gap_lines_rate_limited_per_second(self):
        m, c, L = mk(max_gap_lines_per_s=20)
        t = 100.0
        m.on_write(1, t, t, 0.5)
        for _ in range(60):
            t += 0.2
            m.on_write(1, t, t, 0.5)
        gaps = [x for x in L if x.startswith("wgap write_gap")]
        self.assertEqual(len(gaps), 20)
        c.t = 101.0
        m.tick(0.5)
        self.assertIn("gap_lines_dropped=40", [x for x in L if x.startswith("wgap sec")][0])
        # next second the budget is back
        m.on_write(1, t + 0.2, t + 0.2, 0.5)
        self.assertEqual(len([x for x in L if x.startswith("wgap write_gap")]), 21)

    def test_summary_percentiles_and_counts(self):
        m, c, L = mk()
        t = 100.0
        m.on_write(1, t, t, 0.5)
        gaps = [0.01] * 90 + [0.2] * 8 + [0.6, 0.9]
        for g in gaps:
            t += g
            m.on_write(1, t, t, 0.5)
        m.finish("client_closed")
        s = L[-1]
        self.assertTrue(s.startswith("wgap summary"))
        self.assertIn("p50=10", s)
        self.assertIn("p95=200", s)
        self.assertIn("max=900", s)
        self.assertIn("gt100=10", s)
        self.assertIn("gt500=2", s)
        self.assertIn("close=client_closed", s)

    def test_never_raises(self):
        def boom(_):
            raise RuntimeError("x")
        m = zdiag.WriteGapMonitor("c", emit=boom)
        m.on_input(1)
        m.on_write(1, 0.0, 0.0, 0.0)
        m.on_write(1, 1.0, 1.0, 0.0)
        m.tick(0.0, rebuffers=3, now=1e9)
        m.finish()
        m.on_write("bad", None, None, None)


class PacedWiring(unittest.TestCase):
    def setUp(self):
        self._pace = h.PACE

    def tearDown(self):
        h.PACE = self._pace

    def run_paced(self, env, n=300):
        h.PACE = True
        hd = td.StreamHandler()
        with td.diag_env(env):
            _, out = td.capture(hd._ts_brp, td.Sess([td.PKT] * n), headers_sent=True)
        return hd, out

    def test_on_emits_summary_line(self):
        hd, out = self.run_paced("1")
        self.assertIn("wgap summary stream=0x1130000000002001", out)
        self.assertIn("closed (paced)", out)

    def test_off_emits_no_wgap_lines_and_same_legacy_line(self):
        hd, out = self.run_paced("0")
        self.assertNotIn("wgap", out)
        self.assertTrue(out.startswith("brp stream "), out)

    def test_bytes_written_unchanged_by_logging(self):
        outs = []
        for env in ("1", "0"):
            h.PACE = True
            hd = td.StreamHandler()
            with td.diag_env(env):
                td.capture(hd._ts_brp, td.Sess([td.PKT] * 300), headers_sent=True)
            outs.append(hd.wfile.getvalue())
        self.assertEqual(outs[0], outs[1])

    def test_client_closed_still_reported(self):
        h.PACE = True
        hd = td.StreamHandler(td.BrokenWfile())
        with td.diag_env("1"):
            _, out = td.capture(hd._ts_brp, td.Sess([td.PKT] * 20000), headers_sent=True)
        self.assertIn(hd._zreason, ("client_closed", "upstream_ended", "pacer_done"))
        self.assertIn("wgap summary", out)


if __name__ == "__main__":
    unittest.main()
