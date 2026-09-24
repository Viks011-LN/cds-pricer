#!/usr/bin/env python3
"""quantlib_crosscheck.py — external validation of isda_cds vs QuantLib.

WHY THIS EXISTS
---------------
The engine's 17-test suite is strong *internal* validation (closed forms,
the ISDA flat-curve identity, symmetry), but "identical to the TypeScript
parent" is circular — same AI lineage, same potential blind spots.
QuantLib's IsdaCdsEngine is an independent implementation tested against
the official ISDA C library, which makes it the practical external referee
in the absence of a Bloomberg CDSW screen.

WHAT IT CHECKS, PER TEST CASE
-----------------------------
1. Maturity date: our post-2015 semiannual roll vs ql.cdsMaturity(CDS2015).
2. Clean upfront (currency) vs QuantLib NPV (default leg - coupon leg
   + accrual rebate = clean value to the protection buyer).
3. Par spread vs QuantLib fairSpread.

The discount curve is handed to QuantLib as discount FACTORS computed from
the same continuously-compounded zeros the engine uses, with log-linear DF
interpolation on an ACT/365F axis — i.e. the discount curves are identical
by construction, so any disagreement isolates to the CREDIT side (bootstrap,
schedule, accrual, protection-leg integration).

KNOWN RESIDUAL DIFFERENCES (expected to be well under 1bp of notional)
----------------------------------------------------------------------
- QuantLib's IsdaCdsEngine applies the ISDA C "half-day bias" in the
  accrual-on-default term by default; isda_cds does not. Worth a fraction
  of a bp on the fee leg.
- Helper/schedule conventions at the margins (settlement-day handling in
  the bootstrap helpers).
If diffs exceed the thresholds, something real is wrong — investigate
before trusting either number.

USAGE (on a local machine)
--------------------------
    pip3 install QuantLib          # one-off, ~50MB wheel
    cd ~/cds-pricer
    python3 quantlib_crosscheck.py

Exit code 0 = all cases within tolerance; 1 = at least one breach.
"""

from __future__ import annotations

import math
import sys

try:
    import QuantLib as ql
except ImportError:
    sys.exit(
        "QuantLib is not installed. Run:  pip3 install QuantLib\n"
        "(package name is exactly 'QuantLib'; it ships a prebuilt macOS wheel)"
    )

from isda_cds import CurvePoint, PricerRequest, run_pricer, tenor_to_months
from isda_cds.dates import act365, add_months, parse_iso, standard_cds_maturity, to_iso

# --------------------------------------------------------------------------
# Tolerances — deliberately tight. Breaches mean investigate, not shrug.
# --------------------------------------------------------------------------
UPFRONT_TOL_BP = 1.0   # |upfront diff| as bp of notional
SPREAD_TOL_BP = 0.5    # |par spread - fairSpread| in bp

CASES = [
    dict(
        name="IG 5Y base (the smoke trade)",
        trade_date="2026-07-21", tenor="5Y", coupon_bps=100, notional=10_000_000,
        recovery_pct=40, buy_protection=True,
        credit=[("1Y", 80), ("3Y", 120), ("5Y", 160), ("10Y", 200)],
        rates=[("1Y", 3.5), ("5Y", 3.5), ("10Y", 3.7)],
    ),
    dict(
        name="IG 5Y base, SELL side (symmetry through both libraries)",
        trade_date="2026-07-21", tenor="5Y", coupon_bps=100, notional=10_000_000,
        recovery_pct=40, buy_protection=False,
        credit=[("1Y", 80), ("3Y", 120), ("5Y", 160), ("10Y", 200)],
        rates=[("1Y", 3.5), ("5Y", 3.5), ("10Y", 3.7)],
    ),
    dict(
        name="HY 5Y, 500 running, points-upfront regime",
        trade_date="2026-07-21", tenor="5Y", coupon_bps=500, notional=5_000_000,
        recovery_pct=30, buy_protection=True,
        credit=[("1Y", 300), ("3Y", 450), ("5Y", 600), ("10Y", 700)],
        rates=[("1Y", 3.5), ("5Y", 3.5), ("10Y", 3.7)],
    ),
    dict(
        name="Distressed inverted curve, R=20",
        trade_date="2026-07-21", tenor="5Y", coupon_bps=500, notional=5_000_000,
        recovery_pct=20, buy_protection=True,
        credit=[("1Y", 1200), ("3Y", 1000), ("5Y", 900), ("10Y", 800)],
        rates=[("1Y", 3.5), ("5Y", 3.5), ("10Y", 3.7)],
    ),
    dict(
        name="Short 1Y IG",
        trade_date="2026-07-21", tenor="1Y", coupon_bps=100, notional=10_000_000,
        recovery_pct=40, buy_protection=True,
        credit=[("1Y", 80), ("3Y", 120), ("5Y", 160), ("10Y", 200)],
        rates=[("1Y", 3.5), ("5Y", 3.5), ("10Y", 3.7)],
    ),
]


def to_ql_date(d):
    return ql.Date(d.day, d.month, d.year)


def build_ql_discount(trade, rate_points):
    """Replicate the engine's discount curve exactly: node times ACT/365F,
    DF = exp(-zero * t), log-linear DF interpolation (DiscountCurve default),
    flat extrapolation."""
    dates, dfs = [to_ql_date(trade)], [1.0]
    for tenor, zero_pct in rate_points:
        d = add_months(trade, tenor_to_months(tenor))
        t = act365(trade, d)
        dates.append(to_ql_date(d))
        dfs.append(math.exp(-(zero_pct / 100.0) * t))
    curve = ql.DiscountCurve(dates, dfs, ql.Actual365Fixed())
    curve.enableExtrapolation()
    return ql.YieldTermStructureHandle(curve)


def build_ql_credit(yts, credit_points, recovery):
    helpers = []
    for tenor, spread_bps in credit_points:
        helpers.append(
            ql.SpreadCdsHelper(
                ql.QuoteHandle(ql.SimpleQuote(spread_bps / 10000.0)),
                ql.Period(tenor),
                0,                              # settlement days
                ql.WeekendsOnly(),              # matches the engine's calendar
                ql.Quarterly,
                ql.Following,
                ql.DateGeneration.CDS2015,      # semiannual roll, as patched
                ql.Actual360(),
                recovery,
                yts,
            )
        )
    curve = ql.PiecewiseFlatHazardRate(ql.Settings.instance().evaluationDate,
                                       helpers, ql.Actual365Fixed())
    curve.enableExtrapolation()
    return ql.DefaultProbabilityTermStructureHandle(curve)


def price_in_quantlib(case):
    trade = parse_iso(case["trade_date"])
    trade_ql = to_ql_date(trade)
    ql.Settings.instance().evaluationDate = trade_ql

    yts = build_ql_discount(trade, case["rates"])
    recovery = case["recovery_pct"] / 100.0
    prob = build_ql_credit(yts, case["credit"], recovery)

    # Maturity per QuantLib's own CDS2015 rule (independent check of ours)
    cds_maturity_fn = getattr(ql, "cdsMaturity", None)
    if cds_maturity_fn is not None:
        maturity_ql = cds_maturity_fn(trade_ql, ql.Period(case["tenor"]),
                                      ql.DateGeneration.CDS2015)
    else:
        # very old bindings: fall back to our own rule and note it
        m = standard_cds_maturity(trade, tenor_to_months(case["tenor"]))
        maturity_ql = to_ql_date(m)
        print("  [warn] ql.cdsMaturity unavailable — maturity check degraded")

    schedule = ql.Schedule(
        trade_ql, maturity_ql, ql.Period(3, ql.Months), ql.WeekendsOnly(),
        ql.Following, ql.Unadjusted, ql.DateGeneration.CDS2015, False,
    )
    side = ql.Protection.Buyer if case["buy_protection"] else ql.Protection.Seller
    cds = ql.CreditDefaultSwap(
        side, case["notional"], case["coupon_bps"] / 10000.0, schedule,
        ql.Following, ql.Actual360(),
        True,                # settlesAccrual
        True,                # paysAtDefaultTime
        trade_ql + 1,        # protection start = T+1 (step-in)
    )
    # Defaults = the canonical ISDA setup: Taylor numerical fix,
    # half-day accrual bias, piecewise forwards in coupon periods.
    cds.setPricingEngine(ql.IsdaCdsEngine(prob, recovery, yts))

    out = {
        "maturity": f"{maturity_ql.year():04d}-{int(maturity_ql.month()):02d}-{maturity_ql.dayOfMonth():02d}",
        "npv": cds.NPV(),                      # clean value to the chosen side
        "fair_spread_bps": cds.fairSpread() * 10000.0,
    }
    for label, attr in (("default_leg", "defaultLegNPV"),
                        ("coupon_leg", "couponLegNPV"),
                        ("accrual_rebate", "accrualRebateNPV")):
        try:
            out[label] = getattr(cds, attr)()
        except Exception:
            out[label] = float("nan")
    return out


def price_in_engine(case):
    req = PricerRequest(
        trade_date=case["trade_date"], tenor=case["tenor"],
        coupon_bps=case["coupon_bps"], notional=case["notional"],
        recovery_pct=case["recovery_pct"], buy_protection=case["buy_protection"],
        credit_curve=[CurvePoint(t, v) for t, v in case["credit"]],
        rate_curve=[CurvePoint(t, v) for t, v in case["rates"]],
    )
    return run_pricer(req)


def main() -> int:
    print(f"QuantLib {ql.__version__} vs isda_cds cross-check")
    print(f"Tolerances: upfront ±{UPFRONT_TOL_BP}bp of notional, "
          f"spread ±{SPREAD_TOL_BP}bp\n")

    breaches = 0
    for case in CASES:
        print(f"=== {case['name']} ===")
        try:
            eng = price_in_engine(case)
            qlr = price_in_quantlib(case)
        except Exception as e:  # noqa: BLE001
            print(f"  FAILED to price: {type(e).__name__}: {e}\n")
            breaches += 1
            continue

        n = case["notional"]
        up_diff = eng.upfront_amount - qlr["npv"]
        up_diff_bp = up_diff / n * 10000.0
        sp_diff = eng.par_spread_bps - qlr["fair_spread_bps"]
        mat_match = eng.maturity_date == qlr["maturity"]

        print(f"  maturity      engine {eng.maturity_date}  |  QL {qlr['maturity']}"
              f"  {'OK' if mat_match else '** MISMATCH **'}")
        print(f"  clean upfront engine {eng.upfront_amount:>14,.2f}  |  "
              f"QL NPV {qlr['npv']:>14,.2f}  |  diff {up_diff:>+10,.2f} "
              f"({up_diff_bp:+.3f}bp)")
        print(f"  par spread    engine {eng.par_spread_bps:>10.4f}bp  |  "
              f"QL fair {qlr['fair_spread_bps']:>10.4f}bp  |  diff {sp_diff:+.4f}bp")
        print(f"  QL legs: default {qlr['default_leg']:,.2f}  "
              f"coupon {qlr['coupon_leg']:,.2f}  rebate {qlr['accrual_rebate']:,.2f}")

        ok = mat_match and abs(up_diff_bp) <= UPFRONT_TOL_BP and abs(sp_diff) <= SPREAD_TOL_BP
        if not ok:
            breaches += 1
        print(f"  -> {'PASS' if ok else '*** BREACH ***'}\n")

    if breaches:
        print(f"{breaches} case(s) breached tolerance — do NOT trust either "
              "number until reconciled.")
        return 1
    print("All cases within tolerance. The engine agrees with an independent "
          "ISDA implementation.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
