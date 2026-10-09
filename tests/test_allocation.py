"""Allocation rules and IBKR parsing (with a simulated IB account)."""
import copy
import unittest
from types import SimpleNamespace as NS
from unittest import mock

from minervini import allocation, ibkr
from minervini.config import DEFAULTS

CFG = copy.deepcopy(DEFAULTS)
CFG["risk"]["capital_source"] = "fixed"
CFG["risk"]["account_equity"] = 100_000


def pos(t, price, entry, stop, shares, r):
    return {"ticker": t, "price": price, "entry": entry, "stop": stop, "shares": shares,
            "r_multiple": r, "gain_pct": price / entry - 1}


class ModeTest(unittest.TestCase):
    def test_no_positions_is_pilot(self):
        self.assertEqual(allocation.choose_mode("uptrend", [], CFG)[0], "pilot")

    def test_correction_is_defensive(self):
        self.assertEqual(allocation.choose_mode("correction", [], CFG)[0], "defensive")

    def test_working_book_is_full(self):
        ps = [pos("A", 120, 100, 93, 10, 2.8), pos("B", 108, 100, 93, 10, 1.1)]
        self.assertEqual(allocation.choose_mode("uptrend", ps, CFG)[0], "full")

    def test_pressure_caps_full(self):
        ps = [pos("A", 120, 100, 93, 10, 2.8), pos("B", 108, 100, 93, 10, 1.1)]
        self.assertEqual(allocation.choose_mode("pressure", ps, CFG)[0], "normal")

    def test_losing_book_is_pilot(self):
        ps = [pos("A", 96, 100, 93, 10, -0.6), pos("B", 98, 100, 93, 10, -0.3), pos("C", 103, 100, 93, 10, 0.4)]
        self.assertEqual(allocation.choose_mode("uptrend", ps, CFG)[0], "pilot")

    def test_manual_override(self):
        cfg = copy.deepcopy(CFG)
        cfg["risk"]["mode"] = "normal"
        self.assertEqual(allocation.choose_mode("uptrend", [], cfg)[0], "normal")


class SizingTest(unittest.TestCase):
    def snap(self, positions, regime="uptrend", account=None, cfg=CFG):
        return allocation.snapshot(cfg, account, positions, regime)

    def test_pilot_sizing_from_risk(self):
        s = self.snap([])                       # pilot: 0.625% risk, 12.5% max position, 50% max exposure
        z = allocation.size_position(100, 95, s)
        self.assertEqual(z["shares"], 125)      # $625 risk / $5 per share
        self.assertIn("risk per trade", z["limit"])

    def test_position_cap_binds_on_tight_stop(self):
        s = self.snap([])
        z = allocation.size_position(100, 99, s)   # risk allows 625 sh, cap 12.5% -> 125 sh
        self.assertEqual(z["shares"], 125)
        self.assertIn("max position", z["limit"])

    def test_budget_binds_when_invested(self):
        cfg = copy.deepcopy(CFG)
        cfg["risk"]["mode"] = "normal"
        ps = [pos(t, 100, 100, 93, 225, 0.5) for t in "ABCD"]   # $90k invested
        s = self.snap(ps, cfg=cfg)
        self.assertAlmostEqual(s["budget"], 10_000)
        z = allocation.size_position(100, 95, s)
        self.assertEqual(z["shares"], 100)
        self.assertIn("budget", z["limit"])

    def test_slots_full(self):
        cfg = copy.deepcopy(CFG)
        cfg["risk"]["max_positions"] = 2
        s = self.snap([pos("A", 100, 100, 93, 10, 0.5), pos("B", 100, 100, 93, 10, 0.5)], cfg=cfg)
        self.assertEqual(allocation.size_position(100, 95, s)["shares"], 0)

    def test_defensive_buys_nothing(self):
        s = self.snap([], regime="correction")
        self.assertEqual(allocation.size_position(100, 95, s)["shares"], 0)

    def test_open_risk_cap(self):
        cfg = copy.deepcopy(CFG)
        cfg["risk"]["mode"] = "normal"
        cfg["risk"]["max_open_risk_pct"] = 0.02
        s = self.snap([pos("A", 100, 100, 85, 100, 0.0)], cfg=cfg)   # $1,500 open risk already
        z = allocation.size_position(100, 95, s)                     # $500 of risk left -> 100 sh
        self.assertEqual(z["shares"], 100)
        self.assertIn("open-risk", z["limit"])

    def test_plan_consumes_budget(self):
        s = self.snap([])                       # pilot: budget $50k, 6 slots
        rows = [{"ticker": t, "name": t, "status": "READY", "price": 98, "score": 80,
                 "plan": {"entry": 100, "stop": 99}} for t in "ABCDEF"]
        plan = allocation.allocation_plan(rows, s)
        bought = [r for r in plan["rows"] if r["shares"]]
        self.assertEqual(len(bought), 4)        # 4 x $12.5k fills the 50% pilot exposure
        self.assertLessEqual(plan["exposure_after_pct"], 0.5 + 1e-9)

    def test_ibkr_capital_and_margin(self):
        cfg = copy.deepcopy(CFG)
        cfg["risk"]["capital_source"] = "ibkr"
        acct = {"net_liq": 200_000, "cash": 50_000, "buying_power": 400_000}
        s = allocation.snapshot(cfg, acct, [], "uptrend")
        self.assertEqual(s["capital"], 200_000)
        self.assertEqual(s["deployable"], 50_000)       # no margin: cash only
        cfg["risk"]["use_margin"] = True
        self.assertEqual(allocation.snapshot(cfg, acct, [], "uptrend")["deployable"], 400_000)


class FakeIB:
    """Simulated IBKR account: HKD base currency, one US stock with a stop, a pending buy, an HK stock."""

    def managedAccounts(self):
        return ["U1234567"]

    def accountValues(self, acct):
        return [NS(tag="ExchangeRate", currency="USD", value="7.8"), NS(tag="ExchangeRate", currency="HKD", value="1"),
                NS(tag="CashBalance", currency="USD", value="20000")]

    def accountSummary(self, acct):
        return [NS(tag="NetLiquidation", currency="HKD", value="780000"),
                NS(tag="TotalCashValue", currency="HKD", value="390000"),
                NS(tag="BuyingPower", currency="HKD", value="1560000"),
                NS(tag="AccountType", currency="", value="INDIVIDUAL")]

    def reqAllOpenOrders(self):
        stk = lambda s, cur="USD": NS(secType="STK", symbol=s, currency=cur)
        return [NS(contract=stk("NVDA"), order=NS(action="SELL", orderType="STP", auxPrice=220.0, lmtPrice=0)),
                NS(contract=stk("ZBRA"), order=NS(action="BUY", orderType="STP LMT", auxPrice=375.0, lmtPrice=380.0,
                                                  totalQuantity=20))]

    def portfolio(self, acct):
        return [NS(contract=NS(secType="STK", symbol="NVDA", currency="USD", localSymbol="NVDA"), position=100,
                   averageCost=228.0, marketValue=23770.0),
                NS(contract=NS(secType="STK", symbol="700", currency="HKD", localSymbol="700"), position=100,
                   averageCost=400.0, marketValue=50000.0)]

    def disconnect(self):
        pass


class IbkrParseTest(unittest.TestCase):
    def test_parse_hkd_account(self):
        with mock.patch.object(ibkr, "_connect", return_value=FakeIB()):
            out = ibkr.fetch_portfolio(CFG)
        a = out["account"]
        self.assertAlmostEqual(a["net_liq"], 100_000)        # HKD 780k / 7.8
        self.assertAlmostEqual(a["cash"], 50_000)
        self.assertEqual(a["base_currency"], "HKD")
        self.assertEqual([p["ticker"] for p in out["positions"]], ["NVDA"])
        self.assertEqual(out["positions"][0]["stop"], 220.0)
        self.assertEqual(out["pending_buys"][0]["ticker"], "ZBRA")
        self.assertAlmostEqual(out["pending_buys"][0]["value_usd"], 20 * 375.0)
        self.assertEqual(out["other_holdings"][0]["symbol"], "700")
        self.assertAlmostEqual(out["other_holdings"][0]["value_usd"], 50000 / 7.8, places=1)


if __name__ == "__main__":
    unittest.main()


class IbSnapshotFallbackTest(unittest.TestCase):
    def test_uses_last_snapshot_while_gateway_down(self):
        import tempfile
        from pathlib import Path
        from minervini import pipeline
        cfg = copy.deepcopy(DEFAULTS)
        cfg["positions"]["source"] = "ibkr"
        cfg["positions"]["file"] = "does-not-exist.yaml"
        live = {"positions": [{"ticker": "NVDA", "shares": 100.0, "entry_price": 228.0, "entry_date": None,
                               "stop": 220.0, "notes": "", "source": "ibkr"}],
                "account": {"net_liq": 100_000, "cash": 77_000}, "pending_buys": [], "other_holdings": [], "stops": {}}
        with tempfile.TemporaryDirectory() as d, mock.patch.object(pipeline, "IB_CACHE", Path(d) / "ib.json"):
            with mock.patch.object(pipeline, "fetch_portfolio", return_value=live):
                pos, ib = pipeline._load_positions(cfg)
                self.assertFalse(ib["stale"])
            with mock.patch.object(pipeline, "fetch_portfolio", return_value=None):
                pos, ib = pipeline._load_positions(cfg)
                self.assertTrue(ib["stale"])
                self.assertEqual([p["ticker"] for p in pos], ["NVDA"])
                self.assertEqual(ib["account"]["net_liq"], 100_000)
