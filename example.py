"""End-to-end example for the isda_cds engine.

Case 1: 5Y CDS, buy protection, SNAC 100bp coupon, IG upward-sloping curve.
Case 2: ISDA benchmark identity — a flat 100bp par curve priced against a
        100bp running coupon must give ~zero clean upfront and a 100bp par
        spread at the pillar maturity.

Run with:  python3 example.py
"""

from isda_cds import CurvePoint, PricerRequest, run_pricer


def fmt(x: float) -> str:
    return f"{x:,.2f}"


def main() -> None:
    req = PricerRequest(
        trade_date="2026-07-21",
        tenor="5Y",
        coupon_bps=100,
        notional=10_000_000,
        recovery_pct=40,
        buy_protection=True,
        credit_curve=[
            CurvePoint("6M", 60),
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
    r = run_pricer(req)

    print("=== 5Y CDS, BUY PROTECTION, SNAC 100bp, IG upward-sloping curve ===")
    print(f"maturity            : {r.maturity_date}")
    print(f"upfront (clean)     : {r.upfront_pct:.4f}%  |  {fmt(r.upfront_amount)}")
    print(f"upfront (dirty)     : {r.dirty_upfront_pct:.4f}%  |  {fmt(r.dirty_upfront_amount)}")
    print(f"clean / dirty price : {r.clean_price:.4f} / {r.dirty_price:.4f}")
    print(f"accrued             : {r.accrued_days}d  |  {fmt(r.accrued_amount)}")
    print(f"par spread          : {r.par_spread_bps:.2f}bp")
    print(f"RPV01 clean / dirty : {r.clean_rpv01:.5f} / {r.rpv01:.5f}")
    print(f"survival to maturity: {r.survival_to_maturity:.2f}%")
    print(f"CS01 total          : {fmt(r.cs01_total)} per 1bp parallel")
    for c in r.cs01_per_tenor:
        print(f"  CS01 {c.tenor:<4} @ {c.spread_bps:6.1f}bp : {fmt(c.cs01)}")
    print(f"carry daily / 30d   : {fmt(r.carry_daily)} / {fmt(r.carry_monthly)}  (static curves)")
    print(
        f"rolldown 1d/1w/1m   : {fmt(r.rolldown_1d)} / {fmt(r.rolldown_1w)} / {fmt(r.rolldown_1m)}"
    )
    print("Bootstrapped hazard curve:")
    for h in r.hazard_nodes:
        print(f"  {h.tenor:<4} t={h.t:7.3f}  fwd hazard={h.hazard_pct:.4f}%  Q(t)={h.survival_pct:.3f}%")

    # --- ISDA flat-curve identity benchmark ---
    flat = PricerRequest(
        trade_date="2026-07-21",
        tenor="5Y",
        coupon_bps=100,
        notional=10_000_000,
        recovery_pct=40,
        buy_protection=True,
        credit_curve=[CurvePoint(t, 100) for t in ("6M", "1Y", "3Y", "5Y", "7Y", "10Y")],
        rate_curve=[CurvePoint(t, 3.5) for t in ("6M", "1Y", "2Y", "3Y", "5Y", "7Y", "10Y")],
    )
    fr = run_pricer(flat)
    print("=== ISDA benchmark: flat 100bp curve vs 100bp coupon ===")
    print(f"upfront (clean)     : {fr.upfront_pct:.3e}%  (expected ~0)")
    print(f"par spread          : {fr.par_spread_bps:.6f}bp  (expected 100)")


if __name__ == "__main__":
    main()
