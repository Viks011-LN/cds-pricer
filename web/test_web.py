"""Tests for the web front end's API handlers (stdlib unittest).

Run from the repo root:  python3 -m unittest web.test_web -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import server  # noqa: E402
from isda_cds import CurvePoint, PricerRequest, run_pricer  # noqa: E402

BASE = {
    "trade_date": "2026-07-21",
    "tenor": "5Y",
    "traded_spread_bps": 160,
    "coupon_bps": 100,
    "notional": 10_000_000,
    "recovery_pct": 40,
    "buy_protection": True,
    "discount": {"mode": "flat", "flat_rate": 3.5},
}
CURVE = [{"tenor": t, "value": v} for t, v in (("1Y", 80), ("3Y", 120), ("5Y", 160), ("10Y", 200))]


class SingleTradeTest(unittest.TestCase):
    def test_matches_engine_directly(self):
        out = server.price_single(dict(BASE))["result"]
        ref = run_pricer(PricerRequest(
            trade_date="2026-07-21", tenor="5Y", coupon_bps=100, notional=10_000_000,
            recovery_pct=40, buy_protection=True, credit_curve=[CurvePoint("5Y", 160)],
            rate_curve=[CurvePoint("1Y", 3.5), CurvePoint("10Y", 3.5)]))
        self.assertEqual(out["upfront_amount"], ref.upfront_amount)
        self.assertEqual(out["cs01_total"], ref.cs01_total)

    def test_no_market_means_no_mtm(self):
        self.assertIsNone(server.price_single(dict(BASE))["mtm"])

    def test_mtm_sign_follows_side(self):
        wider = {"mode": "flat", "spread_bps": 170}
        buy = server.price_single({**BASE, "market": wider})["mtm"]["pnl"]
        sell = server.price_single({**BASE, "buy_protection": False, "market": wider})["mtm"]["pnl"]
        self.assertGreater(buy, 0)  # protection buyer gains when spreads widen
        self.assertAlmostEqual(buy, -sell, places=6)

    def test_market_curve_used_for_risk(self):
        out = server.price_single({**BASE, "market": {"mode": "curve", "curve": CURVE}})
        self.assertEqual(len(out["result"]["cs01_per_tenor"]), 4)

    def test_bad_input_raises(self):
        with self.assertRaises(ValueError):
            server.price_single({**BASE, "notional": "abc"})
        with self.assertRaises(ValueError):
            server.price_single({**BASE, "tenor": None})


class CurveTradeTest(unittest.TestCase):
    BODY = {
        "trade_date": "2026-07-21",
        "recovery_pct": 40,
        "credit_curve": CURVE,
        "discount": {"mode": "curve", "curve": [{"tenor": "1Y", "value": 3.5}, {"tenor": "10Y", "value": 3.7}]},
        "legs": [
            {"tenor": "5Y", "coupon_bps": 100, "notional": 10_000_000, "buy_protection": False},
            {"tenor": "10Y", "coupon_bps": 100, "notional": 6_000_000, "buy_protection": True},
        ],
    }

    def test_net_is_sum_of_legs(self):
        out = server.price_curve(self.BODY)
        self.assertEqual(len(out["per_leg"]), 2)
        for k in ("cs01_total_per_1bp", "carry_daily", "rolldown_1m"):
            self.assertAlmostEqual(out["net"][k], sum(l[k] for l in out["per_leg"]), places=2)
        self.assertEqual([b["tenor"] for b in out["net"]["cs01_per_tenor"]], ["1Y", "3Y", "5Y", "10Y"])

    def test_needs_two_legs(self):
        with self.assertRaises(ValueError):
            server.price_curve({**self.BODY, "legs": self.BODY["legs"][:1]})


if __name__ == "__main__":
    unittest.main()
