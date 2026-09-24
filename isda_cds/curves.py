"""Curve objects for the ISDA CDS Standard Model.

Both the discount curve and the credit (hazard) curve are represented as
piecewise-constant forward-rate curves, i.e. -ln P(t) is piecewise linear in
t (log-linear interpolation of discount factors / survival probabilities).
This matches the open-source ISDA model's ZC curve representation
(JpmcdsZCInterpolate with linear-in-ln(DF)).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = ["CurveNode", "ZeroCurve", "SurvivalCurve", "merge_times"]


@dataclass
class CurveNode:
    t: float  # time in years (ACT/365F from base date)
    r: float  # continuously compounded zero rate to t


class ZeroCurve:
    """Piecewise log-linear discount curve defined by zero-rate nodes."""

    def __init__(self, nodes: list[CurveNode]):
        if not nodes:
            raise ValueError("Curve requires at least one node")
        self.nodes = sorted((CurveNode(n.t, n.r) for n in nodes), key=lambda n: n.t)

    def ln_df(self, t: float) -> float:
        """ln of discount factor at time t (piecewise-linear in r*t between knots)."""
        if t <= 0:
            return 0.0
        n = self.nodes
        if t <= n[0].t or len(n) == 1:
            # flat extrapolation of the first zero rate
            return -n[0].r * t
        for i in range(1, len(n)):
            if t <= n[i].t:
                rt0 = n[i - 1].r * n[i - 1].t
                rt1 = n[i].r * n[i].t
                w = (t - n[i - 1].t) / (n[i].t - n[i - 1].t)
                return -(rt0 + w * (rt1 - rt0))
        # beyond the last knot: flat extrapolation of the last zero rate
        last = n[-1]
        return -last.r * t

    def df(self, t: float) -> float:
        return math.exp(self.ln_df(t))

    def forward(self, t0: float, t1: float) -> float:
        """Continuously compounded forward rate over (t0, t1)."""
        if t1 <= t0:
            return self.zero_rate(t0)
        return (self.ln_df(t0) - self.ln_df(t1)) / (t1 - t0)

    def zero_rate(self, t: float) -> float:
        if t <= 0:
            return self.nodes[0].r
        return -self.ln_df(t) / t

    def knots_between(self, t0: float, t1: float) -> list[float]:
        """All knot times strictly inside (t0, t1)."""
        return [n.t for n in self.nodes if t0 + 1e-12 < n.t < t1 - 1e-12]

    def clone(self) -> "ZeroCurve":
        return type(self)([CurveNode(n.t, n.r) for n in self.nodes])


class SurvivalCurve(ZeroCurve):
    """Survival curve: Q(t) = exp(-Lambda(t)) with Lambda piecewise linear,
    i.e. hazard rates are piecewise constant between knots (ISDA convention).
    Stored identically to ZeroCurve with r = average hazard to t.
    """

    def q(self, t: float) -> float:
        """Survival probability."""
        return self.df(t)

    def hazard(self, t0: float, t1: float) -> float:
        """Forward hazard rate between knots."""
        return self.forward(t0, t1)


def merge_times(*lists: list[float]) -> list[float]:
    """Merge and sort time points, deduplicating within 1e-10 tolerance."""
    all_times: list[float] = []
    for lst in lists:
        all_times.extend(lst)
    all_times.sort()
    out: list[float] = []
    for t in all_times:
        if not out or t - out[-1] > 1e-10:
            out.append(t)
    return out
