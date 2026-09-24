"""ISDA CDS Standard Model pricing engine.

Implements the O'Kane-Turnbull / JP Morgan (ISDA open-source) methodology:

- Protection (contingent) leg integrated over the merged knots of the
  discount and survival curves, assuming piecewise-constant hazard rates and
  forward interest rates.
- Fee (premium) leg with accrual-on-default, integrated period by period.
- Markit numerical fix (Nov 2012): Taylor expansion of the accrual-on-default
  and contingent-leg kernels when |(lambda + f) * dt| is close to zero, which
  removes catastrophic cancellation (esp. with negative rates).
- Piecewise-flat hazard curve bootstrap from par CDS spreads (bracketed
  secant/bisection root finding per pillar).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from .curves import CurveNode, SurvivalCurve, ZeroCurve, merge_times
from .dates import FeePeriod, act365, add_days, build_fee_schedule, days_between

__all__ = [
    "protection_leg_pv",
    "fee_leg_rpv01",
    "accrued_fraction",
    "CdsPricingInputs",
    "CdsPricingResult",
    "FeeLegResult",
    "price_cds",
    "bootstrap_credit_curve",
]

EXPANSION_THRESHOLD = 1e-4  # |x| threshold for the Taylor series, per Markit note


def _g1(x: float) -> float:
    """g1(x) = (1 - e^{-x}) / x, with Taylor series for small |x| (Markit fix)."""
    if abs(x) < EXPANSION_THRESHOLD:
        return 1.0 - x / 2.0 + x * x / 6.0 - x**3 / 24.0 + x**4 / 120.0
    return (1.0 - math.exp(-x)) / x


def _g2(x: float) -> float:
    """g2(x) = (1 - e^{-x}(1 + x)) / x^2, with Taylor series for small |x|."""
    if abs(x) < EXPANSION_THRESHOLD:
        return 0.5 - x / 3.0 + x * x / 8.0 - x**3 / 30.0 + x**4 / 144.0
    return (1.0 - math.exp(-x) * (1.0 + x)) / (x * x)


def protection_leg_pv(
    disc: ZeroCurve,
    cred: SurvivalCurve,
    t_start: float,
    t_end: float,
    recovery: float,
) -> float:
    """Protection leg PV per unit notional for protection over [t_start, t_end].

    Uses the ISDA closed form on each sub-interval where both the hazard rate
    (lambda) and forward interest rate (f) are constant::

        PV = (1-R) * sum over intervals of
             lambda/(lambda+f) * (1 - e^{-(lambda+f) dt}) * Q(t0) * D(t0)

    With the Markit fix the kernel is computed as ``lambda * dt * g1(x)`` with
    x = (lambda+f) dt, which is stable as lambda+f -> 0.
    """
    if t_end <= t_start:
        return 0.0
    knots = [
        t
        for t in merge_times(
            [t_start, t_end],
            disc.knots_between(t_start, t_end),
            cred.knots_between(t_start, t_end),
        )
        if t_start <= t <= t_end
    ]

    pv = 0.0
    for i in range(len(knots) - 1):
        t0, t1 = knots[i], knots[i + 1]
        dt = t1 - t0
        if dt <= 0:
            continue
        lam = cred.forward(t0, t1)
        f = disc.forward(t0, t1)
        q0 = cred.q(t0)
        d0 = disc.df(t0)
        x = (lam + f) * dt
        pv += lam * dt * _g1(x) * q0 * d0
    return (1.0 - recovery) * pv


def _accrual_on_default_interval(
    lam: float, f: float, q0: float, d0: float, s0: float, dt: float
) -> float:
    """Accrual-on-default on a constant-(lambda, f) sub-interval.

    With s = time since accrual start and x = (lambda+f) dt::

        A = lambda * Q(t0) D(t0) * [ s0 * dt * g1(x) + dt^2 * g2(x) ]

    The g1/g2 series form is the Markit numerical fix.
    """
    x = (lam + f) * dt
    return lam * q0 * d0 * (s0 * dt * _g1(x) + dt * dt * _g2(x))


@dataclass
class FeeLegResult:
    pv: float  # dirty PV of 1 unit of running coupon (dirty RPV01, years)
    accrued: float  # accrued premium fraction (years, ACT/360) at valuation


def fee_leg_rpv01(
    disc: ZeroCurve,
    cred: SurvivalCurve,
    periods: list[FeePeriod],
    base_date: date,
    stepin_time: float,
    prot_start: float,
) -> FeeLegResult:
    """Fee leg risky PV01: expected PV of 1 unit of spread paid on the
    schedule, including accrual-on-default, per unit notional. Values at base
    t=0. Periods ending before the step-in time are skipped (except the last).
    """
    pv = 0.0
    accrued = 0.0

    for p in periods:
        t_end = act365(base_date, p.accrual_end)
        t_pay = act365(base_date, p.pay_date)
        if t_end <= stepin_time and not p.is_last:
            continue

        t_acc_start_raw = act365(base_date, p.accrual_start)

        # --- coupon payment contingent on survival to period end ---
        q = cred.q(max(t_end, 0.0))
        d = disc.df(max(t_pay, 0.0))
        pv += p.accrual_time * q * d

        # --- accrual on default over [max(t_acc_start, prot_start), t_end] ---
        obs_start = max(t_acc_start_raw, prot_start, 0.0)
        obs_end = t_end
        if obs_end > obs_start:
            knots = [
                t
                for t in merge_times(
                    [obs_start, obs_end],
                    disc.knots_between(obs_start, obs_end),
                    cred.knots_between(obs_start, obs_end),
                )
                if obs_start <= t <= obs_end
            ]

            # full period length in model time, to convert time-accrual into
            # the ACT/360 coupon fraction: accrued = accrual_time * (s / period_len)
            period_len = t_end - t_acc_start_raw
            aod = 0.0
            for i in range(len(knots) - 1):
                t0, t1 = knots[i], knots[i + 1]
                dt = t1 - t0
                if dt <= 0:
                    continue
                lam = cred.forward(t0, t1)
                f = disc.forward(t0, t1)
                q0 = cred.q(t0)
                d0 = disc.df(t0)
                s0 = t0 - t_acc_start_raw
                aod += _accrual_on_default_interval(lam, f, q0, d0, s0, dt)
            if period_len > 0:
                pv += (p.accrual_time / period_len) * aod

        # --- accrued at valuation (step-in) for this period ---
        if t_acc_start_raw < stepin_time and t_end > stepin_time:
            acc_days = days_between(
                p.accrual_start, add_days(base_date, round(stepin_time * 365))
            )
            accrued += max(acc_days, 0) / 360.0

    return FeeLegResult(pv=pv, accrued=accrued)


def accrued_fraction(periods: list[FeePeriod], stepin_date: date) -> float:
    """Simple accrued premium (ACT/360) from accrual start to step-in date."""
    for p in periods:
        if p.accrual_start <= stepin_date < p.accrual_end:
            return days_between(p.accrual_start, stepin_date) / 360.0
    return 0.0


@dataclass
class CdsPricingInputs:
    trade_date: date
    maturity: date
    coupon: float  # decimal running coupon, e.g. 0.01
    recovery: float  # e.g. 0.4
    notional: float
    buy_protection: bool  # True = long protection (pay coupon)
    disc: ZeroCurve
    cred: SurvivalCurve
    base_date: date | None = None  # valuation base (defaults to trade_date)


@dataclass
class CdsPricingResult:
    protection_pv: float  # per unit notional, > 0
    rpv01: float  # dirty risky PV01 (years)
    clean_rpv01: float
    accrued: float  # accrued premium fraction of notional (coupon * acc_frac)
    accrued_days: int
    upfront_dirty: float  # per unit notional, protection buyer perspective
    upfront_clean: float
    par_spread: float
    clean_price: float  # 100 * (1 - upfront_clean)
    cash_settlement: float  # currency amount, signed for the requested side
    mtm: float  # clean MTM in currency for the requested side


def price_cds(inp: CdsPricingInputs) -> CdsPricingResult:
    """Price a standard CDS. Times are measured from base_date (valuation).

    Cash settlement discounting to T+3 is ignored (PV expressed as of base).
    """
    base = inp.base_date or inp.trade_date
    stepin_date = add_days(inp.trade_date, 1)
    stepin_time = act365(base, stepin_date)
    prot_start = max(stepin_time, 0.0)
    t_mat = act365(base, inp.maturity)

    periods = build_fee_schedule(inp.trade_date, inp.maturity)

    prot_pv = protection_leg_pv(inp.disc, inp.cred, prot_start, t_mat, inp.recovery)
    fee = fee_leg_rpv01(inp.disc, inp.cred, periods, base, stepin_time, prot_start)

    acc_frac = accrued_fraction(periods, stepin_date)
    acc_days = round(acc_frac * 360)
    accrued_premium = acc_frac * inp.coupon

    upfront_dirty = prot_pv - inp.coupon * fee.pv  # protection buyer pays this
    upfront_clean = upfront_dirty + accrued_premium
    clean_rpv01 = fee.pv - acc_frac
    par_spread = prot_pv / clean_rpv01 if clean_rpv01 > 0 else 0.0

    sign = 1.0 if inp.buy_protection else -1.0
    return CdsPricingResult(
        protection_pv=prot_pv,
        rpv01=fee.pv,
        clean_rpv01=clean_rpv01,
        accrued=accrued_premium,
        accrued_days=acc_days,
        upfront_dirty=upfront_dirty,
        upfront_clean=upfront_clean,
        par_spread=par_spread,
        clean_price=100.0 * (1.0 - upfront_clean),
        cash_settlement=sign * upfront_dirty * inp.notional,
        mtm=sign * upfront_clean * inp.notional,
    )


@dataclass
class CreditPillar:
    maturity: date
    spread: float  # decimal par spread


def bootstrap_credit_curve(
    trade_date: date,
    pillars: list[CreditPillar],
    recovery: float,
    disc: ZeroCurve,
    base_date: date | None = None,
) -> SurvivalCurve:
    """Bootstrap a piecewise-flat hazard-rate curve from par CDS spreads.

    Each pillar's average-hazard node is solved so that a CDS to that pillar's
    maturity, paying the quoted par spread, has zero CLEAN upfront.
    """
    base = base_date or trade_date
    sorted_pillars = sorted(pillars, key=lambda p: p.maturity)
    nodes: list[CurveNode] = []

    for pillar in sorted_pillars:
        t = act365(base, pillar.maturity)
        guess = pillar.spread / (1.0 - recovery)  # initial guess
        nodes.append(CurveNode(t=t, r=max(guess, 1e-10)))

        def objective(h: float, _pillar: CreditPillar = pillar) -> float:
            nodes[-1].r = h
            cred = SurvivalCurve([CurveNode(n.t, n.r) for n in nodes])
            res = price_cds(
                CdsPricingInputs(
                    trade_date=trade_date,
                    maturity=_pillar.maturity,
                    coupon=_pillar.spread,
                    recovery=recovery,
                    notional=1.0,
                    buy_protection=True,
                    disc=disc,
                    cred=cred,
                    base_date=base,
                )
            )
            return res.upfront_clean

        solved = _find_root(objective, 1e-10, max(guess * 10, 0.5), 1e-14, 100, guess)
        nodes[-1].r = solved

    return SurvivalCurve(nodes)


def _find_root(
    f: Callable[[float], float],
    lo: float,
    hi: float,
    tol: float,
    max_iter: int,
    guess: float | None = None,
) -> float:
    """Robust 1-D root finder: expands the bracket if needed, then combines
    secant and bisection steps (Dekker-style). Adequate for the monotone
    objective used in the bootstrap.
    """
    a, b = lo, hi
    fa, fb = f(a), f(b)

    # expand upper bracket if same sign
    expand = 0
    while fa * fb > 0 and expand < 60:
        b *= 2
        fb = f(b)
        expand += 1
    if fa * fb > 0:
        # no root found; return best guess
        return guess if guess is not None else (a + b) / 2

    for _ in range(max_iter):
        # secant step, falling back to bisection when unstable/out of bracket
        if abs(fb - fa) > 1e-300:
            s = b - fb * (b - a) / (fb - fa)
        else:
            s = (a + b) / 2
        if not (min(a, b) < s < max(a, b)):
            s = (a + b) / 2
        fs = f(s)
        if abs(fs) < tol or abs(b - a) < 1e-16:
            return s
        if fa * fs < 0:
            b, fb = s, fs
        else:
            a, fa = s, fs
        if abs(fa) < abs(fb):
            a, b = b, a
            fa, fb = fb, fa
    return b
