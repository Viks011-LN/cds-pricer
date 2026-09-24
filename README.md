# ISDA CDS Pricer

A pure-Python implementation of the ISDA CDS Standard Model for pricing single-name credit default swaps: piecewise-flat hazard bootstrap, upfront and par spread, CS01, carry and rolldown. The core engine has **no runtime dependencies**; it uses only the Python 3.10+ standard library.

It follows the O'Kane–Turnbull (Lehman, 2003) methodology as standardised by the ISDA/Markit open-source model, including the **Markit 2012 numerical fix** for the near-zero `lambda + rate` regime.

## Validation

**The engine is cross-checked against QuantLib's `IsdaCdsEngine`, an independent implementation tested against the official ISDA C library. All five test cases pass.**

[`quantlib_crosscheck.py`](quantlib_crosscheck.py) prices each case in both libraries on identical discount curves, so any disagreement comes from the credit side (bootstrap, schedule, accrual, protection leg). It checks three things:

- the maturity date against `ql.cdsMaturity(CDS2015)`
- the clean upfront, within **1bp of notional**
- the par spread against QuantLib's fair spread, within **0.5bp**

Every case bootstraps a four-pillar credit curve (1Y / 3Y / 5Y / 10Y), so the multi-pillar bootstrap is covered.

| Case | Maturity | Upfront diff (bp of notional) | Par spread diff (bp) | Result |
|---|---|---|---|---|
| IG 5Y, upward-sloping curve | match | −0.115 | −0.082 | PASS |
| IG 5Y, sell side (symmetry) | match | +0.115 | −0.082 | PASS |
| HY 5Y, 500 running, points upfront | match | −0.659 | −0.221 | PASS |
| Distressed inverted curve, R = 20% | match | −0.857 | −0.295 | PASS |
| Short 1Y IG | match | −0.320 | −0.238 | PASS |

These figures come from `quantlib_crosscheck.py` run against QuantLib 1.43; re-run it to reproduce them (see [Running the tests](#running-the-tests)). The script lists the known sources of residual difference, such as QuantLib applying the ISDA half-day accrual-on-default bias, which this engine does not.

**What the cross-check does not cover.** CS01, carry and rolldown are not compared against an external library. They are covered by this repo's own test suite: buy/sell symmetry, CS01 bucket concentration, carry signs and rolldown monotonicity. No comparison against a commercial pricing system is recorded in this repo.

**Internal tests.** 58 tests run with a single `pytest`: 18 engine, 31 agent and 9 web API. The engine tests include:

- the protection leg against the closed form for flat hazard and rates, to 10 decimal places
- stability of the Markit fix when `lambda + f ~ -5e-8`, with negative rates
- the bootstrap repricing every pillar to zero clean upfront at its quoted par spread
- the ISDA flat-curve identity: a flat 100bp par curve against a 100bp coupon gives ~0 upfront (`~1e-13 %`) and exactly 100bp par spread

## Features

### Single-trade pricing

`run_pricer` returns, for either side (buy or sell protection):

- **Upfront:** clean and dirty, as % of notional and as cash, plus accrued. Amounts are signed as cash this side pays.
- **Prices:** clean and dirty.
- **Spread:** par spread to the trade's maturity.
- **RPV01:** clean and dirty.
- **Leg values:** protection leg and premium leg.
- **Survival:** survival probability to maturity.
- **CS01:** parallel, by bumping every quote +1bp and re-bootstrapping, and per tenor, by bumping each pillar on its own.
- **Carry:** running-coupon carry, daily and per 30 days, on static curves with no default.
- **Rolldown:** over 1 day, 1 week and 1 month, re-anchoring both curves at the horizon, carry excluded.
- **Curve diagnostics:** bootstrapped hazard rates, survival probabilities and discount factors at each node.

### Curve trades

Several positions on one reference entity, priced against a single shared credit curve: steepeners, flatteners, butterflies. Each leg is priced exactly as it would be on its own, so a leg's figures match single-trade pricing.

- The **terminal agent** takes two or more legs in one request and reports each leg separately.
- The **web front end** takes one or two legs. With two legs, it shows each leg's figures next to a **net** block, which is the plain sum across legs. The net block covers upfront, CS01 (total and by tenor), carry and rolldown, and there is also a leg × tenor CS01 matrix.

### Web front end

A single-page browser app in [`web/`](web/) with a standard-library HTTP server; it needs no extra packages. It has two views:

- **Single trade:** priced from a traded spread and a discount curve (flat zero rate by default, or a zero-rate term curve).
- **Curve trade:** one or two legs on a full credit curve. It opens with one leg; switch to two legs for a curve trade. There are presets for a 5s10s steepener and flattener, with notionals roughly CS01-neutral on the default curve.

Picking a tenor fills in the maturity from the engine's own standard CDS maturity rule. The maturity stays editable: type over it to price to a custom date. Currency is a display label only. The app has light and dark themes and works on mobile.

### Terminal agent

[`cds_agent.py`](cds_agent.py) is a chat interface for your terminal. You ask CDS questions in plain English; a local tool-calling model decides when to call the engine and narrates the results. It uses an OpenAI-compatible API, so it works with Ollama or vLLM.

**The model never computes numbers.** Every figure comes from the engine, and each result echoes the parsed inputs so you can check what the model understood. The agent refuses to assume a notional, rates or a tenor you didn't give it.

```bash
python3 cds_agent.py --model qwen3:32b                                  # Ollama on localhost
python3 cds_agent.py --host http://<your-model-server>:8888 --model <name>   # vLLM
python3 cds_agent.py --once "Buy 10mm 5Y protection at 100 running, recovery 40, \
  credit curve 1Y 80 / 3Y 120 / 5Y 160 / 10Y 200, rates flat 3.5"
```

The model's context window is set on the server, not by the agent (for Ollama, `OLLAMA_CONTEXT_LENGTH`). See the module docstring.

## Installation

You need Python 3.10 or newer. From a clone of the repo, ideally inside a virtual environment:

```bash
python3 -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e .                                      # core engine: installs nothing else
```

The core `isda_cds` package has no runtime dependencies; `pip install -e .` adds only the package itself. Optional extras:

| Extra | Installs | Needed for |
|---|---|---|
| `test` | `pytest` | running the test suite |
| `web` | nothing (standard library only) | the web front end; the extra exists for completeness |
| `validation` | `QuantLib` (large, compiled) | `quantlib_crosscheck.py` only; never needed to price |

```bash
pip install -e ".[test]"            # engine + pytest
pip install -e ".[validation]"      # engine + QuantLib, for the cross-check
```

Only `isda_cds` is packaged. `cds_agent.py`, `web/`, `example.py` and `quantlib_crosscheck.py` are scripts you run from the repo root.

## Running the tests

```bash
pip install -e ".[test]"
pytest
```

Run from the repo root, `pytest` picks up the whole suite, as configured in `pyproject.toml`:

- `test_pricer.py`: the engine
- `test_agent_mock.py`: the agent, against a fake model server
- `web/test_web.py`: the web API

No network access, model server or LLM is needed. Without pytest, the standard library runner works too:

```bash
python3 -m unittest test_pricer test_agent_mock web.test_web -v
```

To run the external QuantLib cross-check (optional; worth running once before relying on the engine):

```bash
pip install -e ".[validation]"
python3 quantlib_crosscheck.py      # exit code 0 = every case within tolerance
```

## Quick start

```bash
python3 example.py                  # end-to-end demo, prints every analytic
python3 web/server.py               # web front end on http://127.0.0.1:8000
```

From Python:

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

## Running the web front end

```bash
python3 web/server.py                       # then open http://127.0.0.1:8000
python3 web/server.py --port 9000           # another port
python3 web/server.py --host 0.0.0.0        # reachable from other machines on your network
```

Run it from the repo root. `pip install -e .` is optional: the server finds `isda_cds` next to it either way.

**How it calls the engine.** It imports `isda_cds`, plus the agent's executors from `cds_agent.py`, without changing either:

- **Single trade:** `run_pricer`. The traded spread becomes a single credit-curve pillar at the trade tenor, the same convention the agent uses for one quoted spread.
- **One-leg curve trade:** `cds_agent.execute_price_cds`, the agent's `price_cds` tool.
- **Two-leg curve trade:** `cds_agent.execute_price_curve_trade`, the agent's `price_curve_trade` tool.
- **Maturity auto-fill:** `standard_cds_maturity`.

| File | Contents |
|---|---|
| `web/server.py` | JSON API (`POST /api/price`, `POST /api/curve-trade`, `POST /api/maturity`) and static file server |
| `web/static/` | `index.html`, `style.css`, `app.js`: the single-page app |
| `web/test_web.py` | Checks the API against the engine: identical numbers, legs match single-trade pricing, net = sum of legs |

## Package layout

| File | Contents |
|---|---|
| `isda_cds/dates.py` | CDS/IMM date logic: quarterly rolls (20 Mar/Jun/Sep/Dec), standard CDS maturity, ACT/360 & ACT/365F day counts, following-business-day adjustment, fee schedule generation (+1 day accrual on the final period) |
| `isda_cds/curves.py` | `ZeroCurve` (continuously compounded zeros, log-linear interpolation in discount factors, flat extrapolation) and `SurvivalCurve` (piecewise-flat forward hazard rates); `merge_times` knot merging |
| `isda_cds/pricer.py` | Protection leg PV and fee leg RPV01 with accrual-on-default, the Markit Taylor-expansion kernels `g1`/`g2` for `|x| < 1e-4`, accrued interest, `price_cds` (clean/dirty upfront, par spread, prices) and `bootstrap_credit_curve` (sequential pillar root-finding: secant with bisection fallback) |
| `isda_cds/analytics.py` | `run_pricer`: orchestrates bootstrap + pricing, CS01 (parallel and per-tenor via re-bootstrap), carry (static curves), rolldown (1d/1w/1m re-anchored curves), curve diagnostics |
| `cds_agent.py` | Terminal agent: tool schemas, engine executors (`price_cds`, `price_curve_trade`), OpenAI-compatible chat loop |
| `web/` | Browser front end (see above) |
| `quantlib_crosscheck.py` | External validation against QuantLib's `IsdaCdsEngine` |
| `example.py` | End-to-end demo, prints every analytic |
| `test_pricer.py`, `test_agent_mock.py`, `web/test_web.py` | Test suite (58 tests) |
| `pyproject.toml` | Packaging (`isda_cds` only, no runtime dependencies), optional extras `test` / `web` / `validation`, pytest configuration |
| `CHANGES.md` | Changelog |

## The maths (summary)

### Model setup

Default is modelled as the first jump of a Poisson process with a deterministic, piecewise-flat forward hazard rate `lambda(t)`. The survival probability is:

> Q(t) = exp( -integral_0^t lambda(s) ds )

Interest rates are deterministic. Discount factors `Z(t)` come from a zero curve interpolated log-linearly in `Z` (i.e. linear in `r*t`), with flat extrapolation at both ends.

### Protection leg

For a contract protecting `[0, T]` with recovery `R`:

> ProtPV = (1 - R) * integral_0^T Z(s) (-dQ(s))

This is evaluated in closed form on each subinterval `[t_i, t_{i+1}]` where both the hazard `lambda` and the forward rate `f` are constant. With `B = lambda + f` and `dt = t_{i+1} - t_i`:

> ProtPV_i = (1 - R) * lambda * Z(t_i) Q(t_i) * dt * g1(B * dt)

where `g1(x) = (1 - e^-x)/x`.

**Markit numerical fix:** for `|x| < 1e-4`, `g1` is replaced by its Taylor expansion `1 - x/2 + x^2/6 - x^3/24`. This avoids catastrophic cancellation when `lambda + f ~ 0` (e.g. with negative rates). The second-order kernel `g2` in the accrual-on-default term gets the same treatment.

### Premium leg

The fee schedule uses quarterly IMM periods, ACT/360, a short or long stub per standard conventions, and one extra day on the final period. For each period with accrual factor `alpha_j` and payment date `p_j`:

> RPV01 = sum_j alpha_j * Z(p_j) * Q(e_j)   +   accrual-on-default terms

The accrual-on-default integral (premium accrued from period start to default time) is evaluated subinterval by subinterval with the same closed-form kernels. The contract accrues from the step-in date (T+1).

### Upfront, prices

> Dirty upfront = ProtPV - C * RPV01 (per unit notional, protection buyer)
> Clean upfront = dirty upfront + accrued
> Clean price = 100 * (1 - clean upfront); Par spread = ProtPV / cleanRPV01

In `run_pricer` output, the currency amounts are signed for the requested side. `upfront_amount`, `dirty_upfront_amount` and `accrued_amount` are cash this side pays (negative = receives), so `dirty = clean + accrued` for either side. The buyer's accrued is negative because the seller rebates it at settlement.

### Bootstrap

Pillars are sorted by maturity. For each pillar CDS quoted at par spread `S_k`, the bootstrap solves for the flat hazard on the newest segment that makes the pillar reprice to **zero clean upfront** with coupon `S_k`. The root finder uses secant iteration with a bisection fallback, bracket expansion, and an initial guess of `S_k / (1 - R)`.

### Analytics

- **CS01 (total):** bump all quoted spreads +1bp, re-bootstrap, reprice, and report the MTM change.
- **CS01 (per tenor):** bump each pillar on its own. The buckets sum approximately to the total.
- **Carry:** pure cash carry of the running coupon, ACT/360, static curves, no default: daily = `-side * C * N / 360`; monthly = 30 × daily.
- **Rolldown:** re-anchor both curves at the horizon date with the same tenor quotes, so the curve "stays where it is". The trade keeps its fixed maturity, so its remaining life shortens; re-bootstrap and reprice. Reported for 1d / 1w / 1m, carry excluded.

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

## Limitations

- **No holiday calendar:** business-day adjustment skips weekends only. Plug your own calendar into `dates.adjust_following` if you need one.
- **Rates input is continuously compounded zero rates**, not the ISDA deposit/swap bootstrap. If you have swap par rates, bootstrap them to zeros first. When reconciling upfronts against another system, feed it matching zeros.
- **No cash-settlement (T+3) discounting** of the upfront.
- **No half-day accrual-on-default bias:** the ISDA C library applies it; the effect is sub-bp.
- **Flat recovery:** one flat assumption per trade, per the standard model.
- **SNAC conventions:** all results follow the Standard North American Contract. European contracts use identical mechanics with different standard coupons.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for the full text.
