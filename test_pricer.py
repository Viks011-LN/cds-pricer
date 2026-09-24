"""Validation suite for the isda_cds engine (pure stdlib unittest).

Run with:  python3 -m unittest test_pricer -v
       or: python3 test_pricer.py
"""

import math
import unittest
from dataclasses import replace

from isda_cds import (
    CdsPricingInputs,
    CreditPillar,
    CurveNode,
    CurvePoint,
    PricerRequest,
    SurvivalCurve,
    ZeroCurve,
    bootstrap_credit_curve,
    build_fee_schedule,
    parse_iso,
    price_cds,
    protection_leg_pv,
    run_pricer,
    standard_cds_maturity,
    tenor_to_months,
    to_iso,
)

TRADE = parse_iso("2026-07-21")


def flat_disc(r: float) -> ZeroCurve:
    return ZeroCurve([CurveNode(t=0.5, r=r), CurveNode(t=30.0, r=r)])


BASE_REQ = PricerRequest(
    trade_date="2026-07-21",
    tenor="5Y",
    coupon_bps=100,
    notional=10_000_000,
    recovery_pct=40,
    buy_protection=True,
    credit_curve=[
        CurvePoint("1Y", 80),
        CurvePoint("3Y", 120),
        CurvePoint("5Y", 160),
        CurvePoint("7Y", 180),
        CurvePoint("10Y", 200),
    ],
    rate_curve=[
        CurvePoint("6M", 3.6),
        CurvePoint("1Y", 3.5),
        CurvePoint("2Y", 3.4),
        CurvePoint("3Y", 3.4),
        CurvePoint("5Y", 3.5),
        CurvePoint("7Y", 3.6),
        CurvePoint("10Y", 3.7),
    ],
)


class TestDates(unittest.TestCase):
    def test_standard_cds_maturity_5y(self):
        # Post-2015 semiannual roll: 5Y traded Jul-2026 sits in the
        # [20 Mar, 19 Sep] window -> anchor 20 Jun 2026 -> 20 Jun 2031
        # (same maturity as the on-the-run index).
        mat = standard_cds_maturity(TRADE, 60)
        self.assertEqual(to_iso(mat), "2031-06-20")

    def test_semiannual_roll_windows(self):
        cases = [
            ("2026-03-19", "2030-12-20"),  # day before the Mar roll: old Dec anchor
            ("2026-03-20", "2031-06-20"),  # Mar roll day: new Jun anchor
            ("2026-09-19", "2031-06-20"),  # last day of the Jun-anchor window
            ("2026-09-20", "2031-12-20"),  # Sep roll day: Dec anchor
            ("2027-01-15", "2031-12-20"),  # Jan trade: Dec anchor of PRIOR year
        ]
        for trade, expected in cases:
            mat = standard_cds_maturity(parse_iso(trade), 60)
            self.assertEqual(to_iso(mat), expected, f"trade {trade}")

    def test_fee_schedule_stub_and_last_period(self):
        mat = standard_cds_maturity(TRADE, 12)
        sched = build_fee_schedule(TRADE, mat)
        # 20 Jun 2026 is a Saturday: accrual starts on the ADJUSTED date
        self.assertEqual(to_iso(sched[0].accrual_start), "2026-06-22")
        self.assertTrue(sched[-1].is_last)
        # last period accrual has +1 day
        self.assertGreater(sched[-1].accrual_time, 0.25)

    def test_accrued_days_use_adjusted_period_start(self):
        # Trade 2026-07-21, step-in 2026-07-22, accrual from Mon 22 Jun
        # (adjusted) -> 30 days, not 32 from the raw Saturday 20th.
        res = run_pricer(BASE_REQ)
        self.assertEqual(res.accrued_days, 30)

    def test_missing_tenor_and_maturity_raises(self):
        req = replace(BASE_REQ, tenor=None, maturity_date=None)
        with self.assertRaises(ValueError):
            run_pricer(req)

    def test_tenor_parsing(self):
        self.assertEqual(tenor_to_months("6M"), 6)
        self.assertEqual(tenor_to_months("5Y"), 60)


class TestProtectionLeg(unittest.TestCase):
    def test_closed_form_flat_hazard_flat_rates(self):
        r, h, R = 0.03, 0.02, 0.4
        disc = flat_disc(r)
        cred = SurvivalCurve([CurveNode(t=30.0, r=h)])
        T = 5.0
        pv = protection_leg_pv(disc, cred, 0.0, T, R)
        analytic = (1 - R) * h * (1 - math.exp(-(r + h) * T)) / (r + h)
        self.assertAlmostEqual(pv, analytic, places=10)

    def test_markit_numerical_fix_near_zero_lambda_plus_rate(self):
        h = 0.00028429134225
        r = -0.00028433979274  # negative rate, lambda+f ~ -5e-8
        disc = flat_disc(r)
        cred = SurvivalCurve([CurveNode(t=30.0, r=h)])
        pv = protection_leg_pv(disc, cred, 0.0, 5.0, 0.4)
        # analytic limit as (h+r)->0: (1-R)*h*T
        analytic = 0.6 * h * 5.0
        self.assertAlmostEqual(pv, analytic, places=8)
        self.assertTrue(math.isfinite(pv))


class TestBootstrap(unittest.TestCase):
    def test_reprices_pillars_to_zero_clean_upfront(self):
        disc = flat_disc(0.035)
        pillars = [
            CreditPillar(standard_cds_maturity(TRADE, 12), 0.008),
            CreditPillar(standard_cds_maturity(TRADE, 36), 0.012),
            CreditPillar(standard_cds_maturity(TRADE, 60), 0.016),
            CreditPillar(standard_cds_maturity(TRADE, 120), 0.02),
        ]
        cred = bootstrap_credit_curve(TRADE, pillars, 0.4, disc)
        for p in pillars:
            res = price_cds(
                CdsPricingInputs(
                    trade_date=TRADE,
                    maturity=p.maturity,
                    coupon=p.spread,
                    recovery=0.4,
                    notional=1.0,
                    buy_protection=True,
                    disc=disc,
                    cred=cred,
                )
            )
            self.assertLess(abs(res.upfront_clean), 1e-10)
            # par spread should equal quoted spread at the pillar
            self.assertAlmostEqual(res.par_spread, p.spread, places=8)

    def test_hazard_approximates_spread_over_lgd(self):
        disc = flat_disc(0.03)
        s = 0.01
        cred = bootstrap_credit_curve(
            TRADE,
            [CreditPillar(standard_cds_maturity(TRADE, 60), s)],
            0.4,
            disc,
        )
        h = cred.nodes[0].r
        self.assertGreater(h, s / 0.6 * 0.95)
        self.assertLess(h, s / 0.6 * 1.1)


class TestFullPricerAnalytics(unittest.TestCase):
    def test_upfront_positive_when_spread_above_coupon(self):
        res = run_pricer(BASE_REQ)
        # 5Y par ~160bp vs 100bp coupon: protection worth more than coupon
        # -> buyer PAYS upfront (positive).
        self.assertGreater(res.upfront_amount, 0)
        self.assertGreater(res.par_spread_bps, 120)
        self.assertLess(res.par_spread_bps, 200)
        # upfront approx (S - C) * cleanRPV01
        approx = (res.par_spread_bps - 100) / 10000 * res.clean_rpv01 * BASE_REQ.notional
        self.assertLess(abs(res.upfront_amount - approx) / approx, 0.05)

    def test_cs01_positive_for_buyer_concentrated_at_trade_tenor(self):
        res = run_pricer(BASE_REQ)
        self.assertGreater(res.cs01_total, 0)
        # roughly cleanRPV01 * 1bp * notional
        approx = res.clean_rpv01 * 1e-4 * BASE_REQ.notional
        self.assertGreater(res.cs01_total, approx * 0.7)
        self.assertLess(res.cs01_total, approx * 1.3)
        # per-tenor sums approx to total
        total = sum(x.cs01 for x in res.cs01_per_tenor)
        self.assertLess(abs(total - res.cs01_total), abs(res.cs01_total) * 0.05 + 5)
        # 5Y bucket should dominate
        five = next(x for x in res.cs01_per_tenor if x.tenor == "5Y")
        max_bucket = max(abs(x.cs01) for x in res.cs01_per_tenor)
        self.assertEqual(abs(five.cs01), max_bucket)

    def test_carry_sign_by_side(self):
        res = run_pricer(BASE_REQ)
        self.assertLess(res.carry_daily, 0)
        self.assertAlmostEqual(
            res.carry_daily, -(100 / 10000) * 10_000_000 / 360, places=6
        )
        self.assertAlmostEqual(res.carry_monthly, res.carry_daily * 30, places=6)

        sell = run_pricer(replace(BASE_REQ, buy_protection=False))
        self.assertGreater(sell.carry_daily, 0)

    def test_rolldown_negative_for_buyer_on_upward_sloping_curve(self):
        res = run_pricer(BASE_REQ)
        # buyer of protection on upward-sloping curve loses value as trade rolls down
        self.assertLess(res.rolldown_1m, 0)
        self.assertLess(abs(res.rolldown_1d), abs(res.rolldown_1w))
        self.assertLess(abs(res.rolldown_1w), abs(res.rolldown_1m))

    def test_buy_sell_symmetry(self):
        buy = run_pricer(BASE_REQ)
        sell = run_pricer(replace(BASE_REQ, buy_protection=False))
        self.assertAlmostEqual(buy.upfront_amount, -sell.upfront_amount, places=6)
        self.assertAlmostEqual(buy.cs01_total, -sell.cs01_total, places=6)
        self.assertAlmostEqual(buy.accrued_amount, -sell.accrued_amount, places=6)

    def test_accrued_signed_so_dirty_equals_clean_plus_accrued(self):
        # Buyer receives the accrued rebate (negative), seller pays it; the same
        # identity must hold for both sides so accrued nets across legs.
        for buy_protection, sign in ((True, -1), (False, 1)):
            res = run_pricer(replace(BASE_REQ, buy_protection=buy_protection))
            self.assertAlmostEqual(res.accrued_amount, sign * 30 / 360 * 0.01 * 10_000_000, places=6)
            self.assertAlmostEqual(
                res.dirty_upfront_amount, res.upfront_amount + res.accrued_amount, places=6
            )

    def test_negative_rates_without_numerical_noise(self):
        res = run_pricer(
            replace(
                BASE_REQ,
                rate_curve=[
                    CurvePoint("1Y", -0.3),
                    CurvePoint("5Y", -0.1),
                    CurvePoint("10Y", 0.1),
                ],
                credit_curve=[
                    CurvePoint("1Y", 2),
                    CurvePoint("5Y", 3),
                    CurvePoint("10Y", 4),
                ],
            )
        )
        self.assertTrue(math.isfinite(res.upfront_amount))
        self.assertTrue(math.isfinite(res.cs01_total))
        self.assertGreater(res.par_spread_bps, 0)

    def test_isda_flat_curve_identity(self):
        res = run_pricer(
            replace(
                BASE_REQ,
                coupon_bps=100,
                credit_curve=[
                    CurvePoint("1Y", 100),
                    CurvePoint("3Y", 100),
                    CurvePoint("5Y", 100),
                    CurvePoint("10Y", 100),
                ],
            )
        )
        self.assertLess(abs(res.upfront_pct), 1e-6)
        self.assertAlmostEqual(res.par_spread_bps, 100, places=4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
