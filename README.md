# ISDA CDS Standard Model — Python Engine (standalone, no UI)

A pure-Python implementation of the ISDA CDS Standard Model pricing engine, ported 1:1 from the TypeScript engine and validated to produce **numerically identical output**. It follows the O'Kane–Turnbull (Lehman, 2003) methodology as standardized by the ISDA/Markit open-source model, including the **Markit 2012 numerical fix** for the near-zero `lambda + rate` regime.

**Zero dependencies** — Python 3.10+ standard library only (`math`, `datetime`, `dataclasses`, `re`).

## Quick start

```bash
python3 example.py                     # runnable end-to-end demo
python3 -m unittest test_pricer -v     # 18-test validation suite
```

Minimal usage:

```python
from isda_cds import CurvePoint, PricerRequest, run_pricer

req = PricerRequest(
    trade_date="2026-07-21",
    tenor="5Y",                      # or maturity_date="2031-06-20"
    coupon_bps=100,                  # SNAC running coupon
    notional=10_000_000,
    recovery_pct=40,
    buy_protection=True,
    credit_curve=[CurvePoint("1Y", 80), CurvePoint("3Y", 120),
                  CurvePoint("5Y", 160), CurvePoint("10Y", 200)],   # par spreads, bps
    rate_curve=[CurvePoint("1Y", 3.5), CurvePoint("5Y", 3.5),
                CurvePoint("10Y", 3.7)],                            # zero rates, %
)
res = run_pricer(req)
print(res.upfront_pct, res.cs01_total, res.carry_daily, res.rolldown_1m)
```

## Web front end (demo)

A browser UI for the engine lives in `web/`. It is a separate entry point: it imports `isda_cds` (and, for curve trades, the agent's `price_curve_trade` executor from `cds_agent.py`) without changing either, and the terminal CLI works exactly as before. Like the engine, it has **no dependencies** — the server is Python's standard-library `http.server`, the page is plain HTML/CSS/JS.

```bash
python3 web/server.py                       # then open http://127.0.0.1:8000
python3 web/server.py --port 9000           # another port
python3 web/server.py --host 0.0.0.0        # reachable from other machines on your network
python3 -m unittest web.test_web -v         # API tests
```

- **Single trade** — side, notional, currency, tenor (or a custom maturity date), trade date, traded spread, running coupon, recovery, and a discount curve (flat zero rate by default, or a zero-rate term curve). The traded spread becomes a single credit-curve pillar at the trade tenor, as in the CLI. Shows upfront (clean, dirty, accrued, as cash with pay/receive), prices, par spread, RPV01, protection and premium legs, survival to maturity, CS01 (total and by tenor), carry, and 1d/1w/1m rolldown, plus the bootstrapped survival curve.
- **Curve trade** — two or more legs on one shared credit curve (steepeners, flatteners, butterflies; presets included, notionals roughly CS01-neutral on the default curve). Each leg is priced through `cds_agent.execute_price_curve_trade`, the path behind the agent's `price_curve_trade` tool. The page shows each leg on its own, as the CLI does (upfront, CS01 total and by tenor, carry, rolldown), next to a **net** block with the same figures summed across the legs. A side-by-side table and a CS01 bucket matrix (leg × tenor, with a net row) are shown below.
- Currency is a display label only; the engine does not depend on currency.

| File | Contents |
|---|---|
| `web/server.py` | JSON API (`POST /api/price`, `POST /api/curve-trade`) and static file server |
| `web/static/` | `index.html`, `style.css`, `app.js`: the single-page app (light and dark themes, mobile-friendly) |
| `web/test_web.py` | Checks the API against the engine: identical numbers, legs match single-trade pricing, net = sum of legs |

## Package layout

| File | Contents |
|---|---|
| `isda_cds/dates.py` | CDS/IMM date logic: quarterly rolls (20 Mar/Jun/Sep/Dec), standard CDS maturity, ACT/360 & ACT/365F day counts, following-business-day adjustment, fee schedule generation (+1 day accrual on the final period) |
| `isda_cds/curves.py` | `ZeroCurve` (continuously compounded zeros, log-linear interpolation in discount factors, flat extrapolation) and `SurvivalCurve` (piecewise-flat forward hazard rates); `merge_times` knot merging |
| `isda_cds/pricer.py` | Protection leg PV and fee leg RPV01 with accrual-on-default, the Markit Taylor-expansion kernels `g1`/`g2` for `|x| < 1e-4`, accrued interest, `price_cds` (clean/dirty upfront, par spread, prices) and `bootstrap_credit_curve` (sequential pillar root-finding: secant with bisection fallback) |
| `isda_cds/analytics.py` | `run_pricer`: orchestrates bootstrap + pricing, CS01 (parallel and per-tenor via re-bootstrap), carry (static curves), rolldown (1d/1w/1m re-anchored curves), curve diagnostics |
| `example.py` | End-to-end demo, prints every analytic |
| `test_pricer.py` | Validation suite (stdlib `unittest`, 18 tests) |

## The maths (summary)

### Model setup

Default is modeled as the first jump of a Poisson process with deterministic piecewise-flat forward hazard rate `lambda(t)`. Survival probability:

> Q(t) = exp( -integral_0^t lambda(s) ds )

Interest rates are deterministic; discount factors `Z(t)` come from a zero curve interpolated log-linearly in `Z` (i.e., linear in `r*t`), flat extrapolation on both ends.

### Protection leg

For a contract protecting `[0, T]` with recovery `R`:

> ProtPV = (1 - R) * integral_0^T Z(s) (-dQ(s))

Evaluated in closed form on each subinterval `[t_i, t_{i+1}]` where both hazard `lambda` and forward rate `f` are constant. With `B = lambda + f`, `dt = t_{i+1} - t_i`:

> ProtPV_i = (1 - R) * lambda * Z(t_i) Q(t_i) * dt * g1(B * dt)

where `g1(x) = (1 - e^-x)/x`. **Markit numerical fix:** for `|x| < 1e-4`, `g1` is replaced by its Taylor expansion `1 - x/2 + x^2/6 - x^3/24` to avoid catastrophic cancellation when `lambda + f ~ 0` (e.g., negative rates). Same treatment for the second-order kernel `g2` used in the accrual-on-default term.

### Premium leg

Fee schedule: quarterly IMM periods, ACT/360, short/long stub per standard conventions, final period accrues one extra day. For each period with accrual factor `alpha_j`, payment date `p_j`:

> RPV01 = sum_j alpha_j * Z(p_j) * Q(e_j)   +   accrual-on-default terms

The accrual-on-default integral (premium accrued from period start to default time) is evaluated with the same closed-form kernels, subinterval by subinterval. Contract accrues from the step-in date (T+1).

### Upfront, prices

> Dirty upfront = ProtPV - C * RPV01 (per unit notional, protection buyer)
> Clean upfront = dirty upfront + accrued
> Clean price = 100 * (1 - clean upfront); Par spread = ProtPV / cleanRPV01

In `run_pricer` output the currency amounts are signed for the requested side: `upfront_amount`, `dirty_upfront_amount` and `accrued_amount` are cash this side pays (negative = receives), so `dirty = clean + accrued` for either side. The buyer's accrued is negative — the seller rebates it at settlement.

### Bootstrap

Pillars sorted by maturity; for each pillar CDS quoted at par spread `S_k`, solve for the flat hazard on the newest segment such that the pillar reprices to **zero clean upfront** with coupon `S_k`. Root finder: secant iteration with bisection fallback, bracket expansion, initial guess `S_k / (1 - R)`.

### Analytics

- **CS01 (total):** bump all quoted spreads +1bp, re-bootstrap, reprice; report MTM change. **Per tenor:** bump each pillar individually (sums approximately to total).
- **Carry:** pure cash carry of the running coupon, ACT/360, static curves, no default: daily = `-side * C * N / 360`; monthly = 30 x daily.
- **Rolldown:** re-anchor both curves at the horizon date (same tenor quotes — the curve "stays where it is"), keep the trade's fixed maturity so its remaining life shortens, re-bootstrap and reprice. Reported for 1d / 1w / 1m, carry excluded.

## Conventions

| Item | Convention |
|---|---|
| Maturity roll | Semiannual (post-Dec-2015): maturities on 20 Jun / 20 Dec only; coupons remain quarterly on the 20ths |
| Fee accrual | ACT/360, quarterly between ADJUSTED roll dates, final period to unadjusted maturity +1 day |
| Payment dates | Adjusted following business day (weekends only; no holiday calendar) |
| Curve day count | ACT/365F for curve times |
| Protection start | Step-in = trade date + 1 (T+1) |
| Recovery | Flat, applied as `(1 - R)` LGD |
| Discounting | Continuously compounded zeros, log-linear DF interpolation |

## Validation

- Output is numerically identical to the reference TypeScript engine (`diff` of both example outputs shows no numeric differences).
- 18 unit tests, including:
  - protection leg vs closed-form for flat hazard/rates (10 decimal places);
  - Markit-fix stability when `lambda + f ~ -5e-8` with negative rates;
  - bootstrap repricing every pillar to zero clean upfront and quoted par spread;
  - the ISDA flat-curve identity: flat 100bp par curve vs 100bp coupon gives ~0 upfront (`~1e-13 %`) and exactly 100bp par spread;
  - buy/sell symmetry, CS01 bucket concentration, carry signs, rolldown monotonicity.

## Notes and limitations

- No holiday calendar (weekend-only business day adjustment) — plug in your own calendar in `dates.adjust_following` if you need one.
- Rates curve input is continuously compounded zero rates; if you have swap par rates, bootstrap them to zeros first (the full ISDA model bootstraps the money-market/swap curve; this engine takes zeros directly to keep inputs transparent).
- Recovery is a single flat assumption per trade, per the standard model.
- All results are per the SNAC (Standard North American Contract) conventions; European contracts use identical mechanics with different standard coupons.
