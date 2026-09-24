#!/usr/bin/env python3
"""
cds_agent.py — Your own local-model tool-calling agent for the ISDA CDS engine.
==============================================================================

An interactive terminal chat: you type natural-language CDS questions, the
local model (via the OpenAI-compatible /v1/chat/completions API with native
`tools=`) decides when to call a tool — `price_cds` for a single leg,
`price_curve_trade` for two or more legs on the same name — this script
executes the deterministic ISDA engine locally, feeds the exact numbers back,
and the model narrates them. The same code drives Ollama and vLLM.

Usage:
    python3 cds_agent.py                        # Ollama on localhost, default model qwen3:32b
    python3 cds_agent.py --model gemma4:31b
    python3 cds_agent.py --host http://<your-model-server>:8888 --model GLM-5.3-Flash-EXL3   # vLLM
    python3 cds_agent.py --once "Buy 10mm 5Y protection at 100 running, \
        recovery 40, credit curve 1Y 80 / 3Y 120 / 5Y 160 / 10Y 200, rates flat 3.5"

Requirements:
    - Python 3.10+ (stdlib only — no pip installs needed; talks to the model
      server over plain HTTP with urllib)
    - An OpenAI-compatible server with a tool-capable model: Ollama
      (`ollama serve`, http://localhost:11434) or vLLM (started with tool
      calling enabled, e.g. `--enable-auto-tool-choice --tool-call-parser ...`)
    - The context window is set on the SERVER — this API cannot set it
      (Ollama's /v1 ignores num_ctx in any form; checked 24 Sep 2026).
      Ollama: set OLLAMA_CONTEXT_LENGTH (e.g. 16384) where `ollama serve`
      runs. Unset, Ollama 0.32 loads the model's MAXIMUM context (262k for
      Qwen3.8) and a 27-31B model then runs a local machine out of GPU
      memory ("Compute error" / dropped connection). vLLM: --max-model-len.
      Too small a window is the opposite failure: it silently truncates the
      system prompt after a few tool rounds and the model starts freelancing.
    - The `isda_cds` package folder next to this script (already included).

The model NEVER computes analytics itself: the system prompt forbids it and
all numbers come from the engine. The tool result includes an echo of the
parsed inputs so you can check the model transcribed your curve correctly.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

# Make isda_cds importable regardless of the working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from isda_cds import CurvePoint, PricerRequest, run_pricer  # noqa: E402

# ---------------------------------------------------------------------------
# 1. Tool schema — what the model sees (OpenAI-style)
# ---------------------------------------------------------------------------

_SIGN_NOTE = (
    "Each result has ready-to-print `display` lines (upfront as a CASH "
    "amount) — print them verbatim. "
    "Every signed amount has a <name>_direction field beside it: PAY/RECEIVE "
    "for cash this side pays or receives (upfront, accrued; dirty = clean + "
    "accrued), GAIN/LOSS for P&L (carry, rolldown), GAIN_IF_WIDER/"
    "LOSS_IF_WIDER for CS01 (P&L per +1bp parallel widening). Describe each "
    "amount with its direction word — never infer the direction from the sign."
)

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "price_cds",
        "description": (
            "Price a credit default swap with the ISDA Standard Model. Returns "
            "upfront (clean/dirty, % and currency), clean/dirty price, RPV01, "
            "par spread, CS01 (total and per tenor), carry (daily/monthly, "
            "static curves), rolldown (1d/1w/1m) and carry+rolldown (1d/1m). "
            "ALWAYS call this tool for "
            "any CDS pricing/risk question. NEVER estimate these numbers yourself. "
            + _SIGN_NOTE
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "trade_date": {
                    "type": "string",
                    "description": "Trade date, ISO format YYYY-MM-DD. If the user does not give one, use today's date.",
                },
                "tenor": {
                    "type": "string",
                    "description": "Trade tenor like '5Y' or '6M' (standard semiannual-roll maturity). REQUIRED unless maturity_date is given — the engine refuses to assume a default tenor.",
                },
                "maturity_date": {
                    "type": "string",
                    "description": "Explicit maturity date YYYY-MM-DD (overrides tenor). Usually omitted.",
                },
                "coupon_bps": {
                    "type": "number",
                    "description": "Running coupon in basis points, e.g. 100 or 500 (SNAC standard coupons).",
                },
                "notional": {
                    "type": "number",
                    "description": "Notional in currency units, e.g. 7000000 for '7m' / '7mm' / '7 million'. Only ever from the user — never a default.",
                },
                "recovery_pct": {
                    "type": "number",
                    "description": "Assumed recovery rate in percent, e.g. 40. Default to 40 for senior unsecured if unspecified.",
                },
                "buy_protection": {
                    "type": "boolean",
                    "description": "true = buy protection (short credit), false = sell protection (long credit).",
                },
                "credit_curve": {
                    "type": "array",
                    "description": "Credit curve as par spreads in bps, e.g. [{'tenor':'1Y','value':80},{'tenor':'5Y','value':160}]. If the user quotes a single flat spread, use one point at the trade tenor.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "tenor": {"type": "string", "description": "e.g. '6M', '1Y', '5Y'"},
                            "value": {"type": "number", "description": "par spread in bps"},
                        },
                        "required": ["tenor", "value"],
                    },
                },
                "rate_curve": {
                    "type": "array",
                    "description": "Zero rate curve in percent, e.g. [{'tenor':'1Y','value':3.5},{'tenor':'10Y','value':3.7}]. If the user says 'rates flat X', use points at 1Y and 10Y both with value X. Never invent a curve.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "tenor": {"type": "string"},
                            "value": {"type": "number", "description": "zero rate in %"},
                        },
                        "required": ["tenor", "value"],
                    },
                },
                "notional_source": {
                    "type": "string",
                    "enum": ["user_stated", "assumed"],
                    "description": "'user_stated' only if the user gave the notional (for every leg). Otherwise do NOT call — ask the user for it.",
                },
                "rates_source": {
                    "type": "string",
                    "enum": ["user_stated", "assumed"],
                    "description": "'user_stated' only if the user gave the rates or explicitly told you to use a default. Otherwise do NOT call — ask the user for them.",
                },
                "defaulted_inputs": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["recovery", "coupon", "side", "trade_date"]},
                    "description": "Which of these YOU filled in because the user did not state them: recovery (40%), coupon (100/500bp), side (buy protection), trade_date (today). [] if the user stated all of them.",
                },
            },
            "required": [
                "trade_date",
                "coupon_bps",
                "notional",
                "recovery_pct",
                "buy_protection",
                "credit_curve",
                "rate_curve",
                "notional_source",
                "rates_source",
                "defaulted_inputs",
            ],
        },
    },
}

# Multi-leg tool: market inputs are shared (one reference entity, one credit
# curve), trade terms are per leg. Field definitions are reused from
# price_cds so the two schemas cannot drift apart.
_PROPS = TOOL_SCHEMA["function"]["parameters"]["properties"]

CURVE_TRADE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "price_curve_trade",
        "description": (
            "Price a multi-leg CDS trade on ONE reference entity (curve "
            "flattener/steepener, 5s10s, 3s5s, switch, or any 2+ positions on "
            "the same credit curve) with the ISDA Standard Model. Returns "
            "inputs_echo (every leg as parsed) and per_leg results — each leg "
            "priced on its own exactly as price_cds would (upfront, RPV01 clean "
            "and dirty, par spread, CS01, carry, rolldown, carry+rolldown). No "
            "netting. Put EVERY leg in ONE call. NEVER estimate these numbers "
            "yourself. "
            + _SIGN_NOTE
        ),
        "parameters": {
            "type": "object",
            "properties": {
                **{
                    k: _PROPS[k]
                    for k in (
                        "trade_date",
                        "recovery_pct",
                        "credit_curve",
                        "rate_curve",
                        "notional_source",
                        "rates_source",
                        "defaulted_inputs",
                    )
                },
                "legs": {
                    "type": "array",
                    "minItems": 2,
                    "description": (
                        "One entry per leg, in the order the user states them. "
                        "Every leg the user states must appear — never drop one."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            k: _PROPS[k]
                            for k in ("tenor", "maturity_date", "coupon_bps", "notional", "buy_protection")
                        },
                        "required": ["coupon_bps", "notional", "buy_protection"],
                    },
                },
            },
            "required": [
                "trade_date",
                "recovery_pct",
                "credit_curve",
                "rate_curve",
                "notional_source",
                "rates_source",
                "defaulted_inputs",
                "legs",
            ],
        },
    },
}

TOOLS = [TOOL_SCHEMA, CURVE_TRADE_SCHEMA]

def build_system_prompt() -> str:
    """System prompt with today's date injected at runtime.

    A local model has NO clock: without this it will confidently invent a
    trade date from its training era, and every maturity/accrued number
    will be silently wrong.
    """
    today = date.today().isoformat()
    return (
        "You are a CDS trading assistant with access to two deterministic ISDA "
        "Standard Model pricing tools: price_cds (one leg) and "
        "price_curve_trade (two or more legs on the same name).\n"
        f"Today's date is {today}.\n"
        "Rules:\n"
        "1. COUNT THE LEGS first. Exactly one leg -> call price_cds. Two or "
        "more legs (curve trade, flattener, steepener, switch, or several "
        "positions on the same name) -> make ONE price_curve_trade call with "
        "every leg in the legs array. Never call price_cds repeatedly for a "
        "multi-leg trade. Every leg the user states must appear — never drop "
        "one.\n"
        "2. For ANY question involving CDS pricing, upfront, MTM, CS01, DV01, "
        "carry, or rolldown you MUST call a tool. NEVER compute, estimate, or "
        "guess these numbers yourself. Never add up, net or compare legs.\n"
        "3. NEVER assume the notional, the rates, the spread/credit curve, or "
        "the tenor. If any is missing or unclear, ask the user BEFORE calling a "
        "tool (one short question listing everything missing). Trader "
        "shorthand is clear, not missing: '7m', '7mm', '7mio' or '7 million' "
        "as a trade size = 7,000,000 notional (the tenor is written "
        "separately, e.g. '5y'); 'k100' or '100 running' = 100bp coupon; "
        "'at 124' / '124bps' = 124bp spread; 'rates flat 3.5' = 3.5% at every "
        "tenor. Only these may be defaulted: recovery 40%, coupon 100bp (IG) "
        f"or 500bp (HY), buy protection, trade date = today ({today}). List "
        "each one YOU filled in in defaulted_inputs (recovery, coupon, side, "
        "trade_date); [] if the user stated them all.\n"
        "4. After the tool returns, print inputs_echo's `display` lines, then "
        "the result's `display` lines, EXACTLY as given, one per line — do "
        "not rephrase, reorder, round, drop or add to them. They already mark "
        "defaults, show upfront as the CASH amount (never the price) and "
        "carry the direction words (PAY/RECEIVE, GAIN/LOSS).\n"
        "5. For a curve trade, print inputs_echo's `display` lines, then each "
        "leg's display_heading followed by that leg's display lines, leg by "
        "leg, as separate blocks. Do not total, net or compare the legs.\n"
        "6. Show only the tool's figures. Do NOT add a summary, read-through or "
        "view of the trade (e.g. calling it a steepener or flattener, net "
        "long or short credit).\n"
        "7. If the tool returns an error, report it verbatim and ask for "
        "corrected inputs. Do not fall back to your own estimates."
    )

# ---------------------------------------------------------------------------
# 2. Tool executor — deterministic engine call
# ---------------------------------------------------------------------------


def _require_user_inputs(args: dict) -> None:
    """Refuse to price on an assumed notional or rates, so the model must ask.

    The prompt alone is not enough: a live run priced '7m' as 10mm and
    invented a rates curve, flagging both only in passing.
    """
    if args.get("notional_source") != "user_stated":
        raise ValueError(
            "notional not stated by the user. Do not assume one — ask the user "
            "for it (e.g. '10mm'); if they gave it, set notional_source='user_stated'."
        )
    if args.get("rates_source") != "user_stated" or not args.get("rate_curve"):
        raise ValueError(
            "rates not stated by the user. Do not assume them — ask the user "
            "(e.g. 'rates flat 3.5'); if they gave them, set rates_source='user_stated'."
        )
    if not args.get("credit_curve"):
        raise ValueError("no spread or credit curve given — ask the user for it (e.g. '124bp').")
    defaulted = args.get("defaulted_inputs")
    if not isinstance(defaulted, list) or any(d not in _DEFAULTABLE for d in defaulted):
        raise ValueError(
            "defaulted_inputs must list which of recovery, coupon, side, "
            "trade_date you filled in yourself — [] if the user stated them all."
        )


_DEFAULTABLE = ("recovery", "coupon", "side", "trade_date")


def _inputs_text(args: dict, credit: list[CurvePoint], rates: list[CurvePoint]):
    """Shared-input wording for the inputs display, and a '(default)' tagger.

    Written by code because the model mislabelled a stated coupon ('k100')
    as defaulted when it restated the inputs itself.
    """
    defaulted = set(args["defaulted_inputs"])

    def tag(name: str) -> str:
        return " (default)" if name in defaulted else ""

    if len(credit) == 1:
        spread = f"spread {credit[0].value:g}bp"
    else:
        spread = "curve " + " / ".join(f"{p.tenor} {p.value:g}bp" for p in credit)
    if len({p.value for p in rates}) == 1:
        rate = f"rates flat {rates[0].value:g}%"
    else:
        rate = "rates " + " / ".join(f"{p.tenor} {p.value:g}%" for p in rates)
    # the trade date is checked, not taken on trust: a live curve-trade run
    # left trade_date out of defaulted_inputs although the user gave none
    today = " (today)" if str(args["trade_date"]) == date.today().isoformat() else ""
    when = f"Recovery {float(args['recovery_pct']):g}%{tag('recovery')} | trade date {args['trade_date']}{today}"
    return spread, rate, when, tag


def _parse_curves(args: dict) -> tuple[list[CurvePoint], list[CurvePoint]]:
    _require_user_inputs(args)
    credit = [CurvePoint(str(p["tenor"]), float(p["value"])) for p in args["credit_curve"]]
    rates = [CurvePoint(str(p["tenor"]), float(p["value"])) for p in args["rate_curve"]]
    return credit, rates


# Words for each signed amount, (positive, negative), so the model never has
# to interpret a sign: models reliably misread them (a live GLM run called a
# received net upfront "you pay").
_CASH = ("PAY", "RECEIVE")  # cash this side pays at settlement
_PNL = ("GAIN", "LOSS")  # P&L to this side
_DIRECTION_WORDS = {
    "upfront_clean_amount": _CASH,
    "upfront_dirty_amount": _CASH,
    "accrued_amount": _CASH,
    "cs01_total_per_1bp": ("GAIN_IF_WIDER", "LOSS_IF_WIDER"),  # P&L per +1bp widening
    "carry_daily": _PNL,
    "carry_monthly_30d": _PNL,
    "rolldown_1d": _PNL,
    "rolldown_1w": _PNL,
    "rolldown_1m": _PNL,
    "carry_plus_roll_1d": _PNL,
    "carry_plus_roll_1m": _PNL,
}


def _with_directions(block: dict) -> dict:
    """Insert '<key>_direction' right after each signed amount in the block."""
    out = {}
    for key, value in block.items():
        out[key] = value
        words = _DIRECTION_WORDS.get(key)
        if words:
            out[f"{key}_direction"] = words[0] if value > 0 else words[1] if value < 0 else "NONE"
    return out


_CARRY_NOTE = (
    "Carry is running-coupon accrual (ACT/360), static curves, no default. "
    "carry_plus_roll_1d = carry_daily + rolldown_1d; carry_plus_roll_1m = "
    "carry_monthly_30d (30 days) + rolldown_1m (one calendar month)."
)


def _carry_and_roll(r) -> dict:
    """Carry, rolldown and carry+roll; the sums use the rounded parts so they foot."""
    carry_d, carry_m = round(r.carry_daily, 2), round(r.carry_monthly, 2)
    roll_d, roll_m = round(r.rolldown_1d, 2), round(r.rolldown_1m, 2)
    return {
        "carry_daily": carry_d,
        "carry_monthly_30d": carry_m,
        "rolldown_1d": roll_d,
        "rolldown_1w": round(r.rolldown_1w, 2),
        "rolldown_1m": roll_m,
        "carry_plus_roll_1d": round(carry_d + roll_d, 2),
        "carry_plus_roll_1m": round(carry_m + roll_m, 2),
    }


def _display_lines(res: dict) -> list[str]:
    """Ready-to-print result lines, written by code rather than the model.

    Left to itself the model sometimes showed the price where the cash
    upfront belonged, or garbled a figure; these lines fix field, sign and
    wording, and the model prints them verbatim.
    """

    def amt(key: str) -> str:  # direction word + unsigned amount
        word = res[f"{key}_direction"]
        return "0.00" if word == "NONE" else f"{word} {abs(res[key]):,.2f}"

    cs01_word = res["cs01_total_per_1bp_direction"].removesuffix("_IF_WIDER")
    cs01 = "0.00" if cs01_word == "NONE" else f"{cs01_word} {abs(res['cs01_total_per_1bp']):,.2f}"
    return [
        f"Upfront clean (cash): {amt('upfront_clean_amount')}  ({abs(res['upfront_clean_pct']):.4f}% of notional)",
        f"Upfront dirty (cash): {amt('upfront_dirty_amount')}  ({abs(res['upfront_dirty_pct']):.4f}% of notional)",
        f"Accrued ({res['accrued_days']} days, cash): {amt('accrued_amount')}",
        f"Price clean / dirty: {res['clean_price']:.4f} / {res['dirty_price']:.4f}",
        f"Par spread: {res['par_spread_bps']:.2f}bp",
        f"RPV01 clean / dirty: {res['rpv01_clean_years']:.4f} / {res['rpv01_dirty_years']:.4f} years",
        f"CS01: {cs01} per +1bp spread widening",
        f"Carry: {amt('carry_daily')} daily / {amt('carry_monthly_30d')} per 30 days",
        f"Rolldown: {amt('rolldown_1d')} (1d) / {amt('rolldown_1w')} (1w) / {amt('rolldown_1m')} (1m)",
        f"Carry + roll: {amt('carry_plus_roll_1d')} (1d) / {amt('carry_plus_roll_1m')} (1m)",
    ]


def _results_block(r) -> dict:
    """Full result for one priced trade/leg — the same block for both tools."""
    res = _with_directions({
        "maturity_date": r.maturity_date,
        "upfront_clean_pct": round(r.upfront_pct, 6),
        "upfront_clean_amount": round(r.upfront_amount, 2),
        "upfront_dirty_pct": round(r.dirty_upfront_pct, 6),
        "upfront_dirty_amount": round(r.dirty_upfront_amount, 2),
        "clean_price": round(r.clean_price, 4),
        "dirty_price": round(r.dirty_price, 4),
        "accrued_days": r.accrued_days,
        "accrued_amount": round(r.accrued_amount, 2),
        "par_spread_bps": round(r.par_spread_bps, 4),
        "rpv01_dirty_years": round(r.rpv01, 6),
        "rpv01_clean_years": round(r.clean_rpv01, 6),
        "survival_to_maturity_pct": round(r.survival_to_maturity, 4),
        "cs01_total_per_1bp": round(r.cs01_total, 2),
        "cs01_per_tenor": [
            {"tenor": c.tenor, "spread_bps": c.spread_bps, "cs01": round(c.cs01, 2)}
            for c in r.cs01_per_tenor
        ],
        **_carry_and_roll(r),
        "carry_note": _CARRY_NOTE,
    })
    return {"display": _display_lines(res), **res}


def execute_price_cds(args: dict) -> str:
    """Run the ISDA engine on model-supplied arguments; return JSON string."""
    try:
        credit, rates = _parse_curves(args)
        req = PricerRequest(
            trade_date=str(args["trade_date"]),
            tenor=str(args["tenor"]) if args.get("tenor") else None,
            maturity_date=str(args["maturity_date"]) if args.get("maturity_date") else None,
            coupon_bps=float(args["coupon_bps"]),
            notional=float(args["notional"]),
            recovery_pct=float(args["recovery_pct"]),
            buy_protection=bool(args["buy_protection"]),
            credit_curve=credit,
            rate_curve=rates,
        )
        r = run_pricer(req)
        spread, rate, when, tag = _inputs_text(args, credit, rates)
        side_word = "BUY" if req.buy_protection else "SELL"
        return json.dumps(
            {
                "inputs_echo": {
                    "display": [
                        f"Trade: {side_word} protection{tag('side')} {req.notional:,.0f} "
                        f"{req.tenor or r.maturity_date}, matures {r.maturity_date}",
                        f"Coupon {req.coupon_bps:g}bp{tag('coupon')} | {spread} | {rate}",
                        when,
                    ],
                    "trade_date": req.trade_date,
                    "tenor": req.tenor,
                    "maturity_date": r.maturity_date,
                    "coupon_bps": req.coupon_bps,
                    "notional": req.notional,
                    "recovery_pct": req.recovery_pct,
                    "side": "BUY protection" if req.buy_protection else "SELL protection",
                    "credit_curve_bps": [{"tenor": p.tenor, "spread_bps": p.value} for p in credit],
                    "rate_curve_pct": [{"tenor": p.tenor, "rate_pct": p.value} for p in rates],
                },
                "results": _results_block(r),
            }
        )
    except Exception as e:  # noqa: BLE001 — surface everything to the model
        return json.dumps({"error": f"{type(e).__name__}: {e}"})


def execute_price_curve_trade(args: dict) -> str:
    """Price every leg on the shared curves, each on its own; return JSON string.

    Each leg goes through run_pricer exactly as price_cds would, so a leg's
    figures are identical to pricing it alone. Legs are deliberately not
    netted — each leg is priced separately.
    """
    try:
        legs = args.get("legs") or []
        if len(legs) < 2:
            raise ValueError(
                f"price_curve_trade needs at least 2 legs, got {len(legs)} — "
                "use price_cds for a single leg."
            )
        credit, rates = _parse_curves(args)
        spread, rate, when, tag = _inputs_text(args, credit, rates)

        echo_legs, per_leg = [], []
        for i, leg in enumerate(legs, start=1):
            try:
                req = PricerRequest(
                    trade_date=str(args["trade_date"]),
                    tenor=str(leg["tenor"]) if leg.get("tenor") else None,
                    maturity_date=str(leg["maturity_date"]) if leg.get("maturity_date") else None,
                    coupon_bps=float(leg["coupon_bps"]),
                    notional=float(leg["notional"]),
                    recovery_pct=float(args["recovery_pct"]),
                    buy_protection=bool(leg["buy_protection"]),
                    credit_curve=credit,
                    rate_curve=rates,
                )
                r = run_pricer(req)
            except Exception as e:  # noqa: BLE001 — name the leg that failed
                raise ValueError(f"leg {i}: {type(e).__name__}: {e}") from e

            side = "BUY protection" if req.buy_protection else "SELL protection"
            echo_legs.append(
                {
                    "leg": i,
                    "tenor": req.tenor,
                    "maturity_date": r.maturity_date,
                    "side": side,
                    "notional": req.notional,
                    "coupon_bps": req.coupon_bps,
                }
            )
            heading = (
                f"Leg {i}: {'BUY' if req.buy_protection else 'SELL'} protection{tag('side')} "
                f"{req.notional:,.0f} {req.tenor or r.maturity_date}, matures "
                f"{r.maturity_date}, coupon {req.coupon_bps:g}bp{tag('coupon')}"
            )
            per_leg.append(
                {"leg": i, "display_heading": heading, "tenor": req.tenor, "side": side, **_results_block(r)}
            )

        return json.dumps(
            {
                "inputs_echo": {
                    "display": [f"{spread[0].upper()}{spread[1:]} | {rate}", when],
                    "trade_date": str(args["trade_date"]),
                    "recovery_pct": float(args["recovery_pct"]),
                    "credit_curve_bps": [{"tenor": p.tenor, "spread_bps": p.value} for p in credit],
                    "rate_curve_pct": [{"tenor": p.tenor, "rate_pct": p.value} for p in rates],
                    "leg_count": len(echo_legs),
                    "legs": echo_legs,
                },
                "per_leg": per_leg,
            }
        )
    except Exception as e:  # noqa: BLE001 — surface everything to the model
        return json.dumps({"error": f"{type(e).__name__}: {e}"})


TOOL_EXECUTORS = {
    "price_cds": execute_price_cds,
    "price_curve_trade": execute_price_curve_trade,
}

# ---------------------------------------------------------------------------
# 3. OpenAI-compatible chat plumbing (stdlib HTTP, no pip installs)
# ---------------------------------------------------------------------------


def chat_completion(
    host: str,
    model: str,
    messages: list[dict],
    tools: list[dict],
    temperature: float = 0.1,
    think: bool | None = None,
) -> dict:
    """One non-streaming /v1/chat/completions round trip; returns the message.

    Works against Ollama and vLLM alike. This API cannot set the context
    window — that is fixed server-side (see the module docstring).
    """
    body_dict: dict = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "stream": False,
        "temperature": temperature,
    }
    if think is False:
        # The one reasoning switch both Ollama and vLLM honour (checked
        # 24 Sep 2026: Ollama 0.32 ignores think=false; vLLM also accepts
        # chat_template_kwargs, but reasoning_effort covers both).
        body_dict["reasoning_effort"] = "none"
    base = host.rstrip("/").removesuffix("/v1")
    req = urllib.request.Request(
        f"{base}/v1/chat/completions",
        data=json.dumps(body_dict).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{host} returned HTTP {e.code}: {e.read().decode()[:500]}") from e
    except urllib.error.URLError as e:
        raise SystemExit(
            f"Cannot reach a model server at {host} ({e}). Is Ollama/vLLM "
            "running? Check the host/port or pass --host."
        ) from e
    except OSError as e:  # dropped mid-request (server crash, OOM kill) or timeout
        raise SystemExit(
            f"{host} dropped the request ({type(e).__name__}: {e}). The server "
            "may have crashed — check its log (Ollama on Apple silicon: GPU "
            "out of memory usually means the context window is too large)."
        ) from e
    return body["choices"][0]["message"]


# Strips everything through the LAST </think>. Some models (Laguna) emit their
# reasoning inline in `content` closed by a bare </think> with no opening tag,
# which a paired <think>...</think> pattern misses.
_THINK_RE = re.compile(r"^.*</think>\s*", re.DOTALL)


def run_turn(
    host: str,
    model: str,
    messages: list[dict],
    max_tool_rounds: int = 5,
    temperature: float = 0.1,
    think: bool | None = None,
) -> str:
    """Send messages; execute any tool calls; loop until a text answer."""
    for _ in range(max_tool_rounds):
        msg = chat_completion(host, model, messages, TOOLS, temperature, think)
        tool_calls = msg.get("tool_calls") or []
        content = _THINK_RE.sub("", msg.get("content") or "").strip()
        if not tool_calls:
            return content or "(empty response)"

        # Echo the tool-call turn back without reasoning fields or leaked
        # reasoning text; "" rather than null content keeps Ollama happy.
        messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})
        for call in tool_calls:
            fn = call.get("function", {})
            name = fn.get("name", "")
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError as e:
                args, result = None, json.dumps({"error": f"tool arguments were not valid JSON: {e}"})
            if args is not None:
                executor = TOOL_EXECUTORS.get(name)
                print(f"  [tool] {name}({json.dumps(args)[:1200]}...)", file=sys.stderr)
                result = (
                    executor(args)
                    if executor
                    else json.dumps({"error": f"unknown tool {name}"})
                )
            messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": result})
    return "(stopped: too many tool rounds — model may be looping)"


# ---------------------------------------------------------------------------
# 4. CLI
# ---------------------------------------------------------------------------


def main() -> None:
    # Windows pipes default to cp1252, which cannot encode the Unicode minus
    # models like to write ("−477,209.84") — and replacing it would hide the
    # sign of a P&L figure. Emit UTF-8 everywhere.
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser(description="Local-model agent for the ISDA CDS engine")
    ap.add_argument("--model", default="qwen3:32b", help="Model name as the server lists it (tool-capable)")
    ap.add_argument("--host", default="http://localhost:11434",
                    help="OpenAI-compatible base URL (Ollama http://localhost:11434, vLLM http://<ip>:<port>)")
    ap.add_argument("question", nargs="*",
                    help="Start the chat with this question (you can then answer follow-ups)")
    ap.add_argument("--once", default=None, help="Ask a single question and exit (for scripts)")
    ap.add_argument("--temperature", type=float, default=0.1,
                    help="Sampling temperature — keep low: argument transcription wants no creativity")
    ap.add_argument("--no-think", action="store_true",
                    help="Send reasoning_effort=none — disables thinking on Ollama and vLLM "
                         "(faster; needed if answers come back empty because they went to reasoning)")
    args = ap.parse_args()

    think = False if args.no_think else None

    messages: list[dict] = [{"role": "system", "content": build_system_prompt()}]

    if args.once:
        messages.append({"role": "user", "content": args.once})
        print(run_turn(args.host, args.model, messages, temperature=args.temperature, think=think))
        return

    print(f"CDS agent ready — model={args.model}, host={args.host}")
    print("Type your CDS question ('quit' to exit). Example:")
    print("  Buy 7mm 5Y protection at 124bp, 100 running, rates flat 3.5\n")
    # a question given on the command line opens the chat, so the agent can
    # still ask for anything missing and you can answer
    pending = " ".join(args.question).strip()
    while True:
        if pending:
            user, pending = pending, ""
            print(f"you> {user}")
        else:
            try:
                user = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
        if not user:
            continue
        if user.lower() in {"quit", "exit", "q"}:
            break
        messages.append({"role": "user", "content": user})
        answer = run_turn(args.host, args.model, messages, temperature=args.temperature, think=think)
        messages.append({"role": "assistant", "content": answer})
        print(f"\nagent> {answer}\n")


if __name__ == "__main__":
    main()
