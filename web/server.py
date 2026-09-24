#!/usr/bin/env python3
"""
web/server.py — browser front end for the ISDA CDS engine.
==========================================================

A small JSON API plus a static single-page app. Every number comes from the
existing engine: single trades go through ``isda_cds.run_pricer``; curve
trades go through ``cds_agent.execute_price_curve_trade`` — the same path the
terminal agent's ``price_curve_trade`` tool uses. Nothing in ``isda_cds`` or
``cds_agent.py`` is modified; this is a separate entry point.

Stdlib only (http.server), like the rest of the repo — no pip installs.

Usage:
    python3 web/server.py                 # http://127.0.0.1:8000
    python3 web/server.py --port 9000 --host 0.0.0.0
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
sys.path.insert(0, str(ROOT))

import cds_agent  # noqa: E402 — the curve-trade path lives here
from isda_cds import CurvePoint, PricerRequest, run_pricer  # noqa: E402

MAX_BODY = 256 * 1024
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------


def _num(d: dict, key: str, *, positive: bool = False) -> float:
    try:
        v = float(d[key])
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"'{key}' must be a number") from None
    if positive and v <= 0:
        raise ValueError(f"'{key}' must be greater than zero")
    return v


def _curve(points, name: str) -> list[CurvePoint]:
    if not isinstance(points, list) or not points:
        raise ValueError(f"{name} needs at least one point")
    out = []
    for p in points:
        if not isinstance(p, dict) or not str(p.get("tenor", "")).strip():
            raise ValueError(f"{name}: every point needs a tenor")
        out.append(CurvePoint(str(p["tenor"]).strip().upper(), _num(p, "value")))
    return out


def _rate_curve(body: dict) -> list[CurvePoint]:
    """Flat rate -> 1Y/10Y points at the same level (the CLI's 'rates flat X')."""
    disc = body.get("discount") or {}
    if disc.get("mode", "flat") == "flat":
        r = _num(disc, "flat_rate")
        return [CurvePoint("1Y", r), CurvePoint("10Y", r)]
    return _curve(disc.get("curve"), "discount curve")


def _recovery(body: dict) -> float:
    r = _num(body, "recovery_pct")
    if not 0 <= r < 100:
        raise ValueError("recovery must be between 0 and 100%")
    return r


def _tenor_or_maturity(d: dict) -> tuple[str | None, str | None]:
    tenor = str(d.get("tenor") or "").strip().upper() or None
    maturity = str(d.get("maturity_date") or "").strip() or None
    if not tenor and not maturity:
        raise ValueError("give a tenor (e.g. 5Y) or a maturity date")
    return tenor, maturity


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def price_single(body: dict) -> dict:
    """Price one trade.

    The engine prices off a credit curve; the traded spread becomes one
    pillar at the trade tenor (the same convention the CLI uses for a single
    quoted spread).
    """
    tenor, maturity = _tenor_or_maturity(body)
    rates = _rate_curve(body)
    pillar = tenor or "5Y"  # maturity-only trades: anchor the flat pillar at 5Y
    credit = [CurvePoint(pillar, _num(body, "traded_spread_bps", positive=True))]
    result = run_pricer(
        PricerRequest(
            trade_date=str(body.get("trade_date") or ""),
            tenor=tenor,
            maturity_date=maturity,
            coupon_bps=_num(body, "coupon_bps"),
            notional=_num(body, "notional", positive=True),
            recovery_pct=_recovery(body),
            buy_protection=bool(body.get("buy_protection", True)),
            credit_curve=credit,
            rate_curve=rates,
        )
    )
    return {
        "result": asdict(result),
        "curve_used": [asdict(p) for p in credit],
        "rate_curve": [asdict(p) for p in rates],
    }


# Summed across legs for the net view. The agent deliberately does not net
# (it prints each leg on its own); the front end shows both.
NET_FIELDS = (
    "upfront_clean_amount",
    "upfront_dirty_amount",
    "accrued_amount",
    "cs01_total_per_1bp",
    "carry_daily",
    "carry_monthly_30d",
    "rolldown_1d",
    "rolldown_1w",
    "rolldown_1m",
    "carry_plus_roll_1d",
    "carry_plus_roll_1m",
)


def price_curve(body: dict) -> dict:
    legs_in = body.get("legs")
    if not isinstance(legs_in, list) or len(legs_in) != 2:
        raise ValueError("a curve trade takes exactly two legs")
    legs = []
    for i, leg in enumerate(legs_in, start=1):
        try:
            tenor, maturity = _tenor_or_maturity(leg)
            legs.append(
                {
                    "tenor": tenor,
                    "maturity_date": maturity,
                    "coupon_bps": _num(leg, "coupon_bps"),
                    "notional": _num(leg, "notional", positive=True),
                    "buy_protection": bool(leg.get("buy_protection", True)),
                }
            )
        except ValueError as e:
            raise ValueError(f"leg {i}: {e}") from None

    rates = _rate_curve(body)
    args = {
        "trade_date": str(body.get("trade_date") or ""),
        "recovery_pct": _recovery(body),
        "credit_curve": [asdict(p) for p in _curve(body.get("credit_curve"), "credit curve")],
        "rate_curve": [asdict(p) for p in rates],
        # every input here is typed by the user in the form
        "notional_source": "user_stated",
        "rates_source": "user_stated",
        "defaulted_inputs": [],
        "legs": legs,
    }
    out = json.loads(cds_agent.execute_price_curve_trade(args))
    if "error" in out:
        raise ValueError(out["error"])

    per_leg = out["per_leg"]
    net = {k: round(sum(leg[k] for leg in per_leg), 2) for k in NET_FIELDS}
    buckets: dict[str, float] = {}
    for leg in per_leg:
        for b in leg["cs01_per_tenor"]:
            buckets[b["tenor"]] = buckets.get(b["tenor"], 0.0) + b["cs01"]
    net["cs01_per_tenor"] = [
        {"tenor": t, "spread_bps": p["value"], "cs01": round(buckets.get(t, 0.0), 2)}
        for t, p in ((c["tenor"], c) for c in args["credit_curve"])
    ]
    return {**out, "net": net}


ROUTES = {"/api/price": price_single, "/api/curve-trade": price_curve}


class Handler(BaseHTTPRequestHandler):
    server_version = "CDSPricerWeb/1.0"

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj) -> None:
        self._send(status, json.dumps(obj).encode(), "application/json")

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/":
            path = "/index.html"
        target = (STATIC / path.lstrip("/")).resolve()
        if STATIC not in target.parents or not target.is_file():
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        ctype = CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        self._send(HTTPStatus.OK, target.read_bytes(), ctype)

    def do_POST(self):  # noqa: N802
        handler = ROUTES.get(self.path.split("?", 1)[0])
        if handler is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ValueError("request too large")
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("request body must be a JSON object")
            self._json(HTTPStatus.OK, handler(body))
        except Exception as e:  # noqa: BLE001 — report engine/input errors to the page
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(e)})

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[web] {self.address_string()} {fmt % args}\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="Web front end for the ISDA CDS engine")
    ap.add_argument("--host", default="127.0.0.1", help="Bind address (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8000, help="Port (default 8000)")
    a = ap.parse_args()
    httpd = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"CDS pricer web UI on http://{a.host}:{a.port}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
