"""Trade-level analytics built on the ISDA pricing engine:

upfront, RPV01, CS01 (parallel + per tenor), carry (static curves), and
rolldown over 1d / 1w / 1m horizons.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from .curves import CurveNode, ZeroCurve
from .dates import (
    act365,
    add_days,
    add_months,
    parse_iso,
    standard_cds_maturity,
    to_iso,
)
from .pricer import CdsPricingInputs, CreditPillar, bootstrap_credit_curve, price_cds

__all__ = [
    "CurvePoint",
    "PricerRequest",
    "TenorCs01",
    "HazardNode",
    "DiscountNode",
    "PricerResponse",
    "tenor_to_months",
    "run_pricer",
]


@dataclass
class CurvePoint:
    tenor: str  # e.g. "6M", "1Y", "5Y"
    value: float  # spread in bps for credit, rate in % for rates


@dataclass
class PricerRequest:
    trade_date: str  # ISO date
    coupon_bps: float
    notional: float
    recovery_pct: float  # 0-100
    buy_protection: bool
    credit_curve: list[CurvePoint]  # spreads in bps
    rate_curve: list[CurvePoint]  # zero rates in %
    tenor: str | None = None  # IMM tenor like "5Y" (used if maturity_date absent)
    maturity_date: str | None = None  # ISO date, overrides tenor


@dataclass
class TenorCs01:
    tenor: str
    spread_bps: float
    cs01: float


@dataclass
class HazardNode:
    tenor: str
    t: float
    hazard_pct: float
    survival_pct: float


@dataclass
class DiscountNode:
    tenor: str
    t: float
    zero_pct: float
    df: float


@dataclass
class PricerResponse:
    maturity_date: str
    upfront_pct: float  # clean upfront, % of notional, signed for trade side
    upfront_amount: float  # clean upfront in currency, signed for trade side
    dirty_upfront_pct: float
    dirty_upfront_amount: float
    clean_price: float
    dirty_price: float
    accrued_days: int
    accrued_amount: float  # sign set by side only (buyer receives -> always negative); pay-positive like the upfronts, so dirty = clean + accrued
    rpv01: float  # dirty RPV01 in years
    clean_rpv01: float
    par_spread_bps: float  # par spread to trade maturity
    cs01_total: float  # currency per 1bp parallel shift
    cs01_per_tenor: list[TenorCs01]
    carry_daily: float
    carry_monthly: float  # 30 calendar days
    carry_note: str
    rolldown_1d: float
    rolldown_1w: float
    rolldown_1m: float
    protection_leg_pct: float
    premium_leg_pct: float
    survival_to_maturity: float
    hazard_nodes: list[HazardNode]
    discount_nodes: list[DiscountNode]


def tenor_to_months(s: str) -> int:
    """Convert a tenor string like '6M' or '5Y' to months."""
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(M|Y)$", s.strip().upper())
    if not m:
        raise ValueError(f"Invalid tenor: {s}")
    n = float(m.group(1))
    return round(n * 12) if m.group(2) == "Y" else round(n)


def _build_discount_curve(trade_date: date, pts: list[CurvePoint]) -> ZeroCurve:
    nodes = []
    for p in pts:
        months = tenor_to_months(p.tenor)
        d = add_months(trade_date, months)
        nodes.append(CurveNode(t=act365(trade_date, d), r=p.value / 100.0))
    return ZeroCurve(nodes)


@dataclass
class _Pillar:
    tenor: str
    maturity: date
    spread: float


def _credit_pillars(trade_date: date, pts: list[CurvePoint]) -> list[_Pillar]:
    return [
        _Pillar(
            tenor=p.tenor,
            maturity=standard_cds_maturity(trade_date, tenor_to_months(p.tenor)),
            spread=p.value / 10000.0,
        )
        for p in pts
    ]


def run_pricer(req: PricerRequest) -> PricerResponse:
    trade_date = parse_iso(req.trade_date)
    coupon = req.coupon_bps / 10000.0
    recovery = req.recovery_pct / 100.0
    notional = req.notional
    sign = 1.0 if req.buy_protection else -1.0

    if req.maturity_date:
        maturity = parse_iso(req.maturity_date)
    elif req.tenor:
        maturity = standard_cds_maturity(trade_date, tenor_to_months(req.tenor))
    else:
        raise ValueError(
            "Either tenor (e.g. '5Y') or maturity_date (YYYY-MM-DD) must be "
            "provided — refusing to assume a default trade tenor."
        )

    disc = _build_discount_curve(trade_date, req.rate_curve)
    pillars = _credit_pillars(trade_date, req.credit_curve)
    cred = bootstrap_credit_curve(
        trade_date,
        [CreditPillar(p.maturity, p.spread) for p in pillars],
        recovery,
        disc,
    )

    def _price(td, mat, d, c):
        return price_cds(
            CdsPricingInputs(
                trade_date=td,
                maturity=mat,
                coupon=coupon,
                recovery=recovery,
                notional=notional,
                buy_protection=req.buy_protection,
                disc=d,
                cred=c,
            )
        )

    base_price = _price(trade_date, maturity, disc, cred)

    # ---------- CS01 ----------
    # parallel: shift all quoted spreads +1bp, re-bootstrap, reprice
    bump_all = bootstrap_credit_curve(
        trade_date,
        [CreditPillar(p.maturity, p.spread + 1e-4) for p in pillars],
        recovery,
        disc,
    )
    cs01_total = _price(trade_date, maturity, disc, bump_all).mtm - base_price.mtm

    # per tenor: bump each pillar individually
    cs01_per_tenor: list[TenorCs01] = []
    for idx, p in enumerate(pillars):
        bumped = bootstrap_credit_curve(
            trade_date,
            [
                CreditPillar(q.maturity, q.spread + (1e-4 if j == idx else 0.0))
                for j, q in enumerate(pillars)
            ],
            recovery,
            disc,
        )
        pr = _price(trade_date, maturity, disc, bumped)
        cs01_per_tenor.append(
            TenorCs01(tenor=p.tenor, spread_bps=p.spread * 10000.0, cs01=pr.mtm - base_price.mtm)
        )

    # ---------- Carry (static curves) ----------
    # Cash carry from running-coupon accrual over the horizon assuming nothing
    # moves and no default. Buyer pays coupon -> negative carry (ACT/360).
    daily_coupon = coupon * notional / 360.0
    carry_daily = -sign * daily_coupon
    carry_monthly = carry_daily * 30.0

    # ---------- Rolldown ----------
    # Roll the valuation forward by h keeping the SPREAD CURVE (by tenor) and
    # rate curve unchanged. The trade's maturity is fixed, so its remaining
    # life shortens and it rolls down the curve. Rolldown = MTM(t+h) - MTM(t),
    # excluding carry (pure curve-roll price effect, clean MTM comparison).
    roll: dict[str, float] = {}
    for key, days, months in (("1d", 1, 0), ("1w", 7, 0), ("1m", 0, 1)):
        fwd_date = add_months(trade_date, months) if months else add_days(trade_date, days)
        if fwd_date >= maturity:
            roll[key] = -base_price.mtm
            continue
        # re-anchor curves at fwd_date with identical tenor-quoted levels
        disc_f = _build_discount_curve(fwd_date, req.rate_curve)
        pillars_f = _credit_pillars(fwd_date, req.credit_curve)
        cred_f = bootstrap_credit_curve(
            fwd_date,
            [CreditPillar(p.maturity, p.spread) for p in pillars_f],
            recovery,
            disc_f,
        )
        pr = _price(fwd_date, maturity, disc_f, cred_f)
        roll[key] = pr.mtm - base_price.mtm

    # ---------- decomposition + curve diagnostics ----------
    t_mat = act365(trade_date, maturity)
    hazard_nodes = []
    for i, n in enumerate(cred.nodes):
        t0 = 0.0 if i == 0 else cred.nodes[i - 1].t
        hazard_nodes.append(
            HazardNode(
                tenor=pillars[i].tenor if i < len(pillars) else f"{n.t:.2f}Y",
                t=n.t,
                hazard_pct=cred.forward(t0, n.t) * 100.0,
                survival_pct=cred.q(n.t) * 100.0,
            )
        )
    discount_nodes = [
        DiscountNode(
            tenor=req.rate_curve[i].tenor if i < len(req.rate_curve) else f"{n.t:.2f}Y",
            t=n.t,
            zero_pct=n.r * 100.0,
            df=disc.df(n.t),
        )
        for i, n in enumerate(disc.nodes)
    ]

    return PricerResponse(
        maturity_date=to_iso(maturity),
        upfront_pct=sign * base_price.upfront_clean * 100.0,
        upfront_amount=sign * base_price.upfront_clean * notional,
        dirty_upfront_pct=sign * base_price.upfront_dirty * 100.0,
        dirty_upfront_amount=sign * base_price.upfront_dirty * notional,
        clean_price=base_price.clean_price,
        dirty_price=100.0 * (1.0 - base_price.upfront_dirty),
        accrued_days=base_price.accrued_days,
        # the seller rebates accrued to the buyer at settlement
        accrued_amount=-sign * base_price.accrued * notional,
        rpv01=base_price.rpv01,
        clean_rpv01=base_price.clean_rpv01,
        par_spread_bps=base_price.par_spread * 10000.0,
        cs01_total=cs01_total,
        cs01_per_tenor=cs01_per_tenor,
        carry_daily=carry_daily,
        carry_monthly=carry_monthly,
        carry_note="Cash carry from running coupon accrual (ACT/360), static curves, no default assumed.",
        rolldown_1d=roll["1d"],
        rolldown_1w=roll["1w"],
        rolldown_1m=roll["1m"],
        protection_leg_pct=base_price.protection_pv * 100.0,
        premium_leg_pct=coupon * base_price.rpv01 * 100.0,
        survival_to_maturity=cred.q(t_mat) * 100.0,
        hazard_nodes=hazard_nodes,
        discount_nodes=discount_nodes,
    )
