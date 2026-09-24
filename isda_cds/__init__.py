"""isda_cds — Standalone ISDA CDS Standard Model pricing engine (pure stdlib).

Implements the O'Kane-Turnbull / ISDA open-source methodology with the
Markit 2012 numerical fix: piecewise-flat hazard bootstrap from par spreads,
protection & premium legs with accrual-on-default, upfront, CS01 (parallel +
per tenor), carry, and 1d/1w/1m rolldown.

High-level entry point: :func:`isda_cds.run_pricer`.
"""

from .analytics import (
    CurvePoint,
    DiscountNode,
    HazardNode,
    PricerRequest,
    PricerResponse,
    TenorCs01,
    run_pricer,
    tenor_to_months,
)
from .curves import CurveNode, SurvivalCurve, ZeroCurve, merge_times
from .dates import (
    FeePeriod,
    act360,
    act365,
    add_days,
    add_months,
    adjust_following,
    build_fee_schedule,
    days_between,
    is_cds_date,
    next_cds_date,
    parse_iso,
    prev_cds_date,
    standard_cds_maturity,
    to_iso,
)
from .pricer import (
    CdsPricingInputs,
    CdsPricingResult,
    CreditPillar,
    FeeLegResult,
    accrued_fraction,
    bootstrap_credit_curve,
    fee_leg_rpv01,
    price_cds,
    protection_leg_pv,
)

__version__ = "1.1.0"

__all__ = [
    # analytics
    "CurvePoint",
    "PricerRequest",
    "PricerResponse",
    "TenorCs01",
    "HazardNode",
    "DiscountNode",
    "run_pricer",
    "tenor_to_months",
    # curves
    "CurveNode",
    "ZeroCurve",
    "SurvivalCurve",
    "merge_times",
    # dates
    "FeePeriod",
    "act360",
    "act365",
    "add_days",
    "add_months",
    "adjust_following",
    "build_fee_schedule",
    "days_between",
    "is_cds_date",
    "next_cds_date",
    "prev_cds_date",
    "parse_iso",
    "standard_cds_maturity",
    "to_iso",
    # pricer
    "CdsPricingInputs",
    "CdsPricingResult",
    "CreditPillar",
    "FeeLegResult",
    "accrued_fraction",
    "bootstrap_credit_curve",
    "fee_leg_rpv01",
    "price_cds",
    "protection_leg_pv",
]
