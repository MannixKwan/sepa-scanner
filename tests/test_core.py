"""Core logic tests on synthetic data:  .venv/bin/python -m unittest discover tests"""
import unittest

import numpy as np
import pandas as pd

from minervini.config import DEFAULTS
from minervini.edgar import quarterly
from minervini.fundamentals import _accel, _growth
from minervini.positions import evaluate
from minervini.strategy import analyze
from minervini.technicals import add_indicators, rs_ratings, trend_template, volume_profile
from minervini.vcp import detect_vcp, find_setup

CFG = {k: dict(v) if isinstance(v, dict) else v for k, v in DEFAULTS.items()}


def frame(closes, vols=None, start="2025-01-02"):
    c = np.asarray(closes, float)
    idx = pd.bdate_range(start, periods=len(c))
    o = np.r_[c[0], c[:-1]]
    hi = np.maximum(o, c) * 1.004
    lo = np.minimum(o, c) * 0.996
    v = np.asarray(vols if vols is not None else np.full(len(c), 1_000_000), float)
    return pd.DataFrame({"open": o, "high": hi, "low": lo, "close": c, "volume": v}, index=idx)


def leg(a, b, n):
    return list(np.linspace(a, b, n + 1)[1:])


def vcp_series(breakout=True):
    """Stage-2 advance, then a 3-contraction base: -25% / -10% / -4%, volume drying up."""
    px = list(np.linspace(40, 100, 260))
    vol = [1_500_000] * 260
    for a, b, n, v in [(100, 75, 15, 1.3e6), (75, 97, 15, 1.1e6), (97, 87.5, 10, 9e5),
                       (87.5, 95.5, 10, 8e5), (95.5, 91.8, 6, 6e5), (91.8, 94.5, 6, 5e5)]:
        px += leg(px[-1] if px[-1] == a else a, b, n)
        vol += [v] * n
    if breakout:
        px.append(98.0)
        vol.append(2_600_000)
    return frame(px, vol)


class TrendTemplateTest(unittest.TestCase):
    def test_uptrend_passes(self):
        df = add_indicators(frame(np.linspace(40, 100, 320)))
        tt = trend_template(df, 90, CFG)
        self.assertTrue(tt["passed"], tt["checks"])

    def test_low_rs_fails(self):
        df = add_indicators(frame(np.linspace(40, 100, 320)))
        self.assertFalse(trend_template(df, 50, CFG)["passed"])

    def test_downtrend_fails(self):
        df = add_indicators(frame(np.linspace(100, 40, 320)))
        self.assertLessEqual(trend_template(df, 90, CFG)["score"], 2)

    def test_rs_ratings_rank(self):
        r = rs_ratings({"A": 0.5, "B": 0.1, "C": -0.2})
        self.assertGreater(r["A"], r["B"])
        self.assertGreater(r["B"], r["C"])
        self.assertTrue(all(1 <= x <= 99 for x in r.values()))


class VcpTest(unittest.TestCase):
    def test_contractions_shrink_and_pivot(self):
        df = vcp_series(breakout=False)
        v = detect_vcp(df, CFG, end_offset=1)
        self.assertIsNotNone(v)
        self.assertTrue(v["valid"], v)
        self.assertGreaterEqual(v["count"], 3)
        self.assertEqual(v["depths"], sorted(v["depths"], reverse=True))
        self.assertAlmostEqual(v["depths"][0], 0.25, delta=0.02)
        self.assertLessEqual(v["final_depth"], 0.06)
        self.assertAlmostEqual(v["pivot"], 95.5 * 1.004, delta=0.6)
        self.assertLess(v["vol_dryup"], 0.7)

    def test_breakout_today(self):
        df = add_indicators(vcp_series(breakout=True))
        s = find_setup(df, CFG)
        self.assertTrue(s["valid"])
        self.assertEqual(s["breakout_days_ago"], 0)
        r = analyze("TEST", df, 95, CFG)
        self.assertEqual(r["status"], "BREAKOUT")
        p = r["plan"]
        self.assertLess(p["stop"], p["entry"])
        self.assertFalse(p["too_wide"])
        self.assertLessEqual(p["risk_pct"], CFG["trade"]["max_stop_pct"] + 1e-9)
        self.assertGreater(r["vol_ratio"], 1.4)

    def test_breakout_volume_grades(self):
        for vol, want in [(2.6e6, "power"), (1.5e6, "confirmed"), (0.9e6, "light")]:
            df = vcp_series(breakout=True)
            df.iloc[-1, df.columns.get_loc("volume")] = vol
            r = analyze("TEST", add_indicators(df), 95, CFG)
            self.assertEqual(r["status"], "BREAKOUT")
            self.assertEqual(r["breakout"]["strength"], want, (vol, r["breakout"]))

    def test_breakout_two_days_ago_is_buy_zone(self):
        df = vcp_series(breakout=True)
        extra = frame([98.6, 99.0], [1.5e6, 1.2e6], start=(df.index[-1] + pd.offsets.BDay(1)).strftime("%Y-%m-%d"))
        df = add_indicators(pd.concat([df, extra]))
        r = analyze("TEST", df, 95, CFG)
        self.assertEqual(r["setup"]["breakout_days_ago"], 2)
        self.assertEqual(r["status"], "BUY ZONE")

    def test_lower_highs_and_one_day_dip_is_not_a_vcp(self):
        # EZPW-like: base high 100, lower highs (97, then 84), last "contraction" a 1-day wiggle
        px = list(np.linspace(40, 100, 260))
        for a_, b_, n_ in [(100, 74, 30), (74, 97, 20), (97, 78, 15), (78, 84, 8), (84, 81.5, 1), (81.5, 83.5, 2)]:
            px += leg(a_, b_, n_)
        v = detect_vcp(frame(px), CFG, end_offset=1)
        self.assertFalse(v["valid"], v)
        self.assertGreater(v["pivot"], 90)          # the 1-day dip's high (~84) is not taken as the pivot

    def test_short_tight_final_pullback_counts(self):
        # NSIT-like: shakeout from the high, then a 4-session 4% pullback near the top of the base
        px = list(np.linspace(40, 100, 260))
        for a_, b_, n_ in [(100, 90.5, 2), (90.5, 97.5, 8), (97.5, 92, 3), (92, 97.8, 4), (97.8, 94, 4), (94, 96.5, 1)]:
            px += leg(a_, b_, n_)
        v = detect_vcp(frame(px), CFG, end_offset=1)
        self.assertTrue(v["valid"], v)
        self.assertLessEqual(v["final_depth"], 0.05)
        self.assertLess(v["pivot_below_base_high"], 0.03)

    def test_tight_base_far_below_highs_is_not_a_vcp(self):
        # contractions shrink (26% -> 5%) but the last one tightens 14% below the base high
        px = list(np.linspace(40, 100, 260))
        for a_, b_, n_ in [(100, 74, 30), (74, 86, 20), (86, 82, 8), (82, 85, 8)]:
            px += leg(a_, b_, n_)
        v = detect_vcp(frame(px), CFG, end_offset=1)
        self.assertLessEqual(v["final_depth"], 0.06)
        self.assertGreater(v["pivot_below_base_high"], 0.10)
        self.assertFalse(v["valid"], v)

    def test_base_count_and_prior_advance(self):
        # advance, 1st base (-20%, 5 weeks), new highs, then the current VCP = 2nd-stage base
        px = list(np.linspace(40, 80, 200)) + leg(80, 64, 12) + leg(64, 84, 13) + list(np.linspace(84, 100, 40))
        for a_, b_, n_ in [(100, 75, 15), (75, 97, 15), (97, 87.5, 10), (87.5, 95.5, 10), (95.5, 91.8, 6), (91.8, 94.5, 6)]:
            px += leg(a_, b_, n_)
        v = detect_vcp(frame(px), CFG, end_offset=1)
        self.assertEqual(v["base_number"], 2)
        self.assertGreater(v["prior_advance"], 0.25)

    def test_base_count_resets_after_undercut(self):
        # 1st base low ~64, 2nd correction undercuts it (to 60) -> count restarts; current base is #2
        px = (list(np.linspace(40, 80, 200)) + leg(80, 64, 12) + leg(64, 84, 13) + leg(84, 60, 15)
              + leg(60, 100, 40))
        for a_, b_, n_ in [(100, 75, 15), (75, 97, 15), (97, 87.5, 10), (87.5, 95.5, 10), (95.5, 91.8, 6), (91.8, 94.5, 6)]:
            px += leg(a_, b_, n_)
        v = detect_vcp(frame(px), CFG, end_offset=1)
        self.assertEqual(v["base_number"], 2)

    def test_new_highs_have_no_base(self):
        self.assertIsNone(detect_vcp(frame(np.linspace(40, 100, 300)), CFG))


class VolumeProfileTest(unittest.TestCase):
    def _series(self, big_move, big_vol, small_move, small_vol, n=160):
        px, vol = [50.0], [1e6]
        for i in range(1, n):
            big = i % 5 == 0
            px.append(px[-1] * (1 + (big_move if big else small_move)))
            vol.append(big_vol if big else small_vol)
        return frame(px, vol)

    def test_accumulation_grades_a(self):
        # +2% days on 3M shares, quiet -0.5% days on 0.5M: up on volume, down on light volume
        vp = volume_profile(self._series(0.02, 3e6, -0.005, 0.5e6), CFG)
        self.assertEqual(vp["grade"], "A", vp)
        self.assertGreater(vp["heavy_up"], 5)
        self.assertEqual(vp["heavy_down"], 0)

    def test_distribution_grades_e(self):
        vp = volume_profile(self._series(-0.02, 3e6, 0.005, 0.5e6), CFG)
        self.assertEqual(vp["grade"], "E", vp)

    def test_even_volume_is_neutral(self):
        vp = volume_profile(frame(np.linspace(40, 60, 160), np.full(160, 1e6)), CFG)
        self.assertIn(vp["grade"], ("B", "C"))   # no heavy days either way
        self.assertEqual(vp["heavy_up"] + vp["heavy_down"], 0)


class ExitRulesTest(unittest.TestCase):
    def _df(self, path):
        pre = list(np.linspace(60, 100, 250))
        return add_indicators(frame(pre + path))

    def test_breakeven_after_2r(self):
        # entry 100, stop 93 (R=7): runs to 116 (>2R) then eases to 108
        df = self._df(leg(100, 116, 12) + leg(116, 108, 4))
        entry_date = df.index[250].strftime("%Y-%m-%d")
        p = evaluate({"ticker": "X", "shares": 10, "entry_price": 100, "stop": 93, "entry_date": entry_date}, df, CFG)
        self.assertEqual(p["stop"], 100)
        self.assertIn("breakeven", p["stop_reason"])

    def test_stop_hit_exits(self):
        df = self._df(leg(100, 104, 3) + leg(104, 92, 4))
        entry_date = df.index[250].strftime("%Y-%m-%d")
        p = evaluate({"ticker": "X", "shares": 10, "entry_price": 100, "stop": 93, "entry_date": entry_date}, df, CFG)
        self.assertEqual(p["action"], "EXIT")

    def test_partial_profit_signal(self):
        df = self._df(leg(100, 124, 20))
        entry_date = df.index[250].strftime("%Y-%m-%d")
        p = evaluate({"ticker": "X", "shares": 10, "entry_price": 100, "stop": 93, "entry_date": entry_date}, df, CFG)
        self.assertEqual(p["action"], "SELL PARTIAL")
        self.assertGreaterEqual(p["stop"], 107)  # locked in +1R after 3R


    def test_failed_breakout_tightens(self):
        df = self._df(leg(100, 103, 2) + leg(103, 96, 3))           # 5 sessions in, 4% under entry
        entry_date = df.index[250].strftime("%Y-%m-%d")
        p = evaluate({"ticker": "X", "shares": 10, "entry_price": 100, "stop": 93, "entry_date": entry_date}, df, CFG)
        self.assertIn("failed_breakout", [s["kind"] for s in p["signals"]])
        self.assertIn(p["action"], ("TIGHTEN", "EXIT"))   # here the 50-day break also fires (EXIT wins)

    def test_time_stop_notes_dead_money(self):
        df = self._df(list(100 + np.sin(np.arange(25)) * 1.5))      # 25 sessions going nowhere
        entry_date = df.index[250].strftime("%Y-%m-%d")
        p = evaluate({"ticker": "X", "shares": 10, "entry_price": 100, "stop": 93, "entry_date": entry_date}, df, CFG)
        self.assertIn("time_stop", [s["kind"] for s in p["signals"]])


class BreakoutAlertTest(unittest.TestCase):
    class FakeState:
        def __init__(self):
            self.sent, self.kv = set(), {}
        def alerted(self, key, day): return key in self.sent
        def mark_alert(self, key, day): self.sent.add(key)
        def get(self, k, default=None): return self.kv.get(k, default)
        def set(self, k, v): self.kv[k] = v

    def _row(self, strength, ratio):
        return {"ticker": "XYZ", "status": "BREAKOUT", "price": 101.0, "pivot_dist": 0.01, "vol_ratio": ratio,
                "fund_pass": True, "plan": {"entry": 100.0, "buy_zone_hi": 105.0, "stop": 95.0, "too_wide": False},
                "breakout": {"strength": strength, "volume_ratio": ratio, "change": 0.025}}

    def test_light_then_confirmed_sends_one_upgrade(self):
        from minervini.pipeline import _events
        st, market = self.FakeState(), {"regime": "uptrend", "label": "Up", "guidance": ""}
        def run(strength, ratio):
            ev = _events({}, [self._row(strength, ratio)], [], market, st, CFG, "2026-10-12")
            for e in ev:
                st.mark_alert(e["key"], "2026-10-12")
            return ev
        first = run("light", 0.9)
        self.assertEqual(len(first), 1)
        self.assertIn("wait for volume", first[0]["text"])
        up = run("confirmed", 1.6)
        self.assertEqual(len(up), 1)
        self.assertIn("now confirmed", up[0]["text"])
        self.assertEqual(run("confirmed", 1.7), [])     # no repeats
        self.assertEqual(run("light", 1.2), [])         # never downgrades
        power = run("power", 2.4)
        self.assertIn("POWER BREAKOUT (upgrade)", power[0]["text"])


class FundamentalsTest(unittest.TestCase):
    def test_growth_and_accel(self):
        s = [("2024-03-31", 1.0), ("2024-06-30", 1.0), ("2024-09-30", 1.0), ("2024-12-31", 1.0),
             ("2025-03-31", 1.2), ("2025-06-30", 1.4), ("2025-09-30", 1.7), ("2025-12-31", 2.1)]
        g = _growth(s, tol=20)
        ys = [x["yoy"] for x in g]
        self.assertAlmostEqual(ys[-1], 1.1)
        self.assertEqual(_accel(ys), 3)

    def test_turnaround(self):
        g = _growth([("2024-06-30", -0.5), ("2025-06-30", 0.3)], tol=20)
        self.assertTrue(g[-1]["turnaround"])
        self.assertIsNone(g[-1]["yoy"])

    def test_edgar_q4_derived(self):
        facts = [
            {"start": "2025-01-01", "end": "2025-03-31", "val": 10, "filed": "2025-05-01"},
            {"start": "2025-04-01", "end": "2025-06-30", "val": 12, "filed": "2025-08-01"},
            {"start": "2025-07-01", "end": "2025-09-30", "val": 14, "filed": "2025-11-01"},
            {"start": "2025-01-01", "end": "2025-12-31", "val": 52, "filed": "2026-02-15"},
            {"start": "2025-01-01", "end": "2025-06-30", "val": 22, "filed": "2025-08-01"},  # YTD, ignored
        ]
        q = dict(quarterly(facts))
        self.assertEqual(q["2025-12-31"], 16)
        self.assertEqual(len(q), 4)


if __name__ == "__main__":
    unittest.main()
