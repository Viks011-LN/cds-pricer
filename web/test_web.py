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

    def test_bad_input_raises(self):
        with self.assertRaises(ValueError):
            server.price_single({**BASE, "notional": "abc"})
        with self.assertRaises(ValueError):
            server.price_single({**BASE, "tenor": None})


class MaturityTest(unittest.TestCase):
    def test_matches_engine_maturity(self):
        out = server.maturities({"trade_date": "2026-07-21", "tenors": ["5Y", "3y", "bad"]})["maturities"]
        self.assertEqual(out["5Y"], run_pricer(PricerRequest(**{**_REQ, "tenor": "5Y"})).maturity_date)
        self.assertEqual(out["3Y"], "2029-06-20")
        self.assertIsNone(out["BAD"])

    def test_custom_maturity_overrides_tenor(self):
        out = server.price_single({**BASE, "maturity_date": "2032-06-20"})["result"]
        self.assertEqual(out["maturity_date"], "2032-06-20")


_REQ = dict(
    trade_date="2026-07-21", coupon_bps=100, notional=10_000_000, recovery_pct=40, buy_protection=True,
    credit_curve=[CurvePoint("5Y", 160)], rate_curve=[CurvePoint("1Y", 3.5), CurvePoint("10Y", 3.5)],
)


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

    def test_net_buckets_are_sum_of_leg_buckets(self):
        out = server.price_curve(self.BODY)
        for b in out["net"]["cs01_per_tenor"]:
            legs = sum(c["cs01"] for l in out["per_leg"] for c in l["cs01_per_tenor"] if c["tenor"] == b["tenor"])
            self.assertAlmostEqual(b["cs01"], legs, places=2)

    def test_legs_match_single_trade_path(self):
        out = server.price_curve(self.BODY)
        leg = self.BODY["legs"][0]
        ref = run_pricer(PricerRequest(
            trade_date="2026-07-21", tenor=leg["tenor"], coupon_bps=100, notional=leg["notional"],
            recovery_pct=40, buy_protection=False, credit_curve=[CurvePoint(p["tenor"], p["value"]) for p in CURVE],
            rate_curve=[CurvePoint("1Y", 3.5), CurvePoint("10Y", 3.7)]))
        self.assertEqual(out["per_leg"][0]["cs01_total_per_1bp"], round(ref.cs01_total, 2))
        self.assertEqual(out["per_leg"][0]["carry_daily"], round(ref.carry_daily, 2))

    def test_one_or_two_legs_only(self):
        with self.assertRaises(ValueError):
            server.price_curve({**self.BODY, "legs": []})
        with self.assertRaises(ValueError):
            server.price_curve({**self.BODY, "legs": self.BODY["legs"] + self.BODY["legs"][:1]})

    def test_single_leg_matches_engine_and_has_no_net(self):
        out = server.price_curve({**self.BODY, "legs": self.BODY["legs"][:1]})
        self.assertIsNone(out["net"])
        self.assertEqual(len(out["per_leg"]), 1)
        # same figures as the leg priced inside the two-leg trade
        both = server.price_curve(self.BODY)["per_leg"][0]
        for k in ("upfront_clean_amount", "cs01_total_per_1bp", "carry_daily", "rolldown_1m", "cs01_per_tenor"):
            self.assertEqual(out["per_leg"][0][k], both[k])


if __name__ == "__main__":
    unittest.main()
