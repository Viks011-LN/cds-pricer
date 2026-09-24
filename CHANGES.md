# v1.2.1 — curve legs priced separately (24 Sep 2026)

- **No netting.** `price_curve_trade` now returns only `inputs_echo` and
  `per_leg`; the `net` block (and `_NETTED_FIELDS`) is gone. Each leg carries
  exactly the block `price_cds` returns for that trade on its own (one shared
  `_results_block`), so a leg is identical to pricing it alone. Legs still go
  in ONE call, so none can be dropped.
- The system prompt shows each leg as its own block ("Leg N: BUY/SELL
  <notional> <tenor>") with the full single-trade figures, and forbids
  totalling or comparing legs.
- **No model commentary.** The model may not add a summary, read-through or
  trade view. Its own remarks were wrong three times in live runs ("net long
  credit" for a position that gains when spreads widen; buy 3Y / sell 5Y
  called a steepener; a wrong carry remark on a single trade). Figures only.
- **Display lines written by code.** Each result (and each leg) carries
  `display` lines built in Python — upfront always as the CASH amount with
  PAY/RECEIVE (e.g. `Upfront clean (cash): PAY 77,039.43  (1.1006% of
  notional)`), the price on its own line — plus a `display_heading` per
  leg. The model prints them verbatim. Fixes a live run that showed the
  price where the cash upfront belonged, and removes garbled figures.
- **Inputs lines written by code too.** `inputs_echo.display` (and each
  leg's `display_heading`) state side, notional, tenor, maturity, coupon,
  spread/curve, rates, recovery and trade date. A new required
  `defaulted_inputs` field (recovery / coupon / side / trade_date) is
  filled by the model at call time and drives the `(default)` tags; the
  trade date is instead checked by code and tagged `(today)`. Fixes a
  live run that called a stated `k100` coupon "defaulted". The whole
  answer is now code-written lines printed verbatim.
- Tests: 49 (netting tests replaced by "each leg is exactly a single
  trade" and "legs not netted"; display-line and inputs-line pins added). The v1.2 net
  pins below are historical.

---

# v1.2.0 — curve trades, OpenAI-compatible API, signed accrued (24 Sep 2026)

Rebuilt from the session record (`CONTEXT-cds-and-evals-2026-07.md`); the
original late-July code was never saved.

## Engine — one sign fix (a displayed number changes)

- **`accrued_amount` is now signed by side only**: the buyer always
  receives it (negative), the seller always pays it (positive), whatever the
  spread. The upfront's sign, by contrast, depends on spread vs coupon AND
  side. Both use the same pay-positive convention, so
  `dirty = clean + accrued` holds for both sides. Per the JPM Credit
  Derivatives Handbook (2006, p. 112): "the seller of protection (long risk)
  must pay an accrued fee upfront", separately from the market-value upfront.
  The smoke trade's accrued now shows **−8,333.33** (was +8,333.33 for both
  sides). Previously unsigned, so it could not be netted across legs: two
  offsetting legs showed 16,666.66 of "net accrued". Upfronts, prices, CS01,
  carry and rolldown are unchanged.

  | side | spread vs 100c | clean upfront | accrued |
  |---|---|---|---|
  | BUY | 60bp | −178,063.14 | −8,333.33 |
  | BUY | 160bp | 256,719.66 | −8,333.33 |
  | SELL | 60bp | 178,063.14 | 8,333.33 |
  | SELL | 160bp | −256,719.66 | 8,333.33 |

## Agent (`cds_agent.py`)

- New tool **`price_curve_trade`**: shared market inputs (trade date,
  recovery, credit curve, rate curve) plus a `legs` array (tenor or
  maturity, coupon, notional, side), minimum 2 legs. Returns:
  - `inputs_echo` — every leg as parsed, plus `leg_count`;
  - `per_leg` — each leg priced through `run_pricer` exactly as `price_cds`
    would (identical figures), with **both** `rpv01_clean_years` and
    `rpv01_dirty_years`, plus `carry_plus_roll_1m`;
  - `net` — `net_`-prefixed sums of the side-signed amounts (upfront clean/
    dirty, accrued, CS01 total and per tenor, carry, rolldown 1d/1w/1m,
    `net_carry_plus_roll_1m`).
- The session record's `mtm_pnl_amount` field (meaning never established)
  was not recreated.
- System prompt rule 1: **COUNT THE LEGS** — one leg → `price_cds`; two or
  more → ONE `price_curve_trade` call with every leg; never drop a leg; net
  figures come from the tool, never summed by the model. Both tool
  descriptions state the sign convention.
- **OpenAI-compatible API.** Talks to `/v1/chat/completions` (replies
  wrapped as `{"choices": [{"message": …}]}`, tool arguments as JSON
  strings, results returned by `tool_call_id`), so the same script drives
  Ollama and vLLM. Assistant tool-call turns are echoed back with `""`
  rather than null content, and without reasoning fields.
- `--no-think` now sends `reasoning_effort: "none"` — checked live to switch
  reasoning off on both Ollama 0.32 (which ignores `think: false` on /v1)
  and vLLM.
- **`--num-ctx` removed**: Ollama's /v1 ignores `num_ctx` in every form
  (checked). Set the context on the server — see the module docstring.
  Unset, Ollama 0.32 loads the model maximum (262k), which ran the local
  machine's GPU out of memory with Qwen3.8-27B and gemma4:31b.
- **`_THINK_RE` → `^.*</think>\s*` (DOTALL)**: strips through the last
  closing tag, so a bare `</think>` with no opening tag (Laguna) is handled.
- Robustness: malformed tool-argument JSON goes back to the model as a tool
  error instead of crashing; a server that drops the request (crash, OOM
  kill, timeout) gives a clean message instead of a traceback; output is
  UTF-8 so a Unicode minus in the answer can't crash a Windows pipe.
- Errors name the failing leg (`leg 2: KeyError: 'coupon_bps'`); a 1-leg
  curve-trade call is rejected with a pointer to `price_cds`.
- **Direction labels**: every signed amount gets a `<name>_direction` field
  right after it — `PAY`/`RECEIVE` (upfront, accrued), `GAIN`/`LOSS`
  (carry, rolldown, carry+roll), `GAIN_IF_WIDER`/`LOSS_IF_WIDER` (CS01),
  `NONE` at zero — in `price_cds` results, each curve-trade leg and the net
  block. The system prompt tells the model to describe amounts with these
  words and never infer direction from a sign.
- **Never assumes notional or rates.** A live run priced "buy 7m 5y" as
  10mm and invented a rates curve. Now: (1) the system prompt forbids
  assuming notional, rates, spread or tenor and says to ask first, and
  spells out trader shorthand ("7m"/"7mm" = 7,000,000, "k100" = 100bp
  coupon); (2) both tools require `notional_source` and `rates_source`
  (`user_stated`/`assumed`), and the executor refuses anything but
  `user_stated`, or an empty rates/credit curve, with an error telling the
  model to ask. Live: the original wording now reads 7m as 7,000,000 and
  asks for rates; a trade with no size asks for the notional.
- **Carry + rolldown**: `carry_plus_roll_1d` (carry_daily + rolldown_1d) and
  `carry_plus_roll_1m` (carry_monthly_30d + rolldown_1m) on single trades,
  every curve-trade leg and the net block (`net_carry_plus_roll_1d/1m`),
  with GAIN/LOSS labels. Built by one shared helper so single-leg and
  per-leg figures can't drift. Smoke trade: −405.31 (1d), −12,287.69 (1m).
- `cds_agent.py "question"` opens the chat with that question, so the agent
  can ask for anything missing and you can answer; `--once` still answers
  and exits.
- Shortcut `<your-user>\.local\bin\cds.cmd` (on PATH): runs the agent
  against the GLM server with `--no-think`; `cds` = chat,
  `cds "question"` = chat starting with that question.

### Reference pins (5s10s: buy 10mm 5Y / sell 10mm 10Y, 100c, R40, smoke-trade curves, trade 2026-07-21)

| metric | leg 1 (buy 5Y) | leg 2 (sell 10Y) | net |
|---|---|---|---|
| clean upfront | 259,504.25 | −736,714.09 | −477,209.84 |
| accrued | −8,333.33 | 8,333.33 | 0.00 |
| CS01 | 4,220.64 | −6,810.29 | −2,589.65 |
| carry + roll 1m | −12,287.69 | 12,999.96 | 712.27 |

Regression pins from this engine, not externally validated. Leg 1 matches the
v1.1 QuantLib-checked smoke-trade pins.

## Live checks (24 Sep 2026)

- **vLLM, GLM-5.3-Flash** (<your-model-server>:8888): the 5s10s question, thinking
  off and on — one `price_curve_trade` call with both legs, every net figure
  relayed exactly. With thinking OFF the model called the −477,209.84 net
  upfront "you pay" (it is received); thinking ON narrated it correctly.
  After the direction labels: two thinking-OFF reruns both said "you
  RECEIVE 477,209.84".
- Single-leg smoke trade on the same server: 2.595042% / 4,220.64, accrued
  −8,333.33 described as received.
- Not run on the Ollama machine: the pricer is used from one machine only.

## Tests

- 17 → 18 engine tests: accrued sign / `dirty = clean + accrued` for both
  sides.
- 2 → 24 agent tests (42 total), mock server now in the OpenAI shape:
  curve-trade round trip (one call, both tools offered, leaked reasoning not
  echoed), per-leg = single-leg pricing, net = sum of legs, offsetting legs
  net to zero (incl. accrued), 3-leg echo with clean and dirty RPV01,
  single-leg rejection, bad-leg error, invalid argument JSON, dropped
  connection, bare-`</think>` stripping, `--no-think` payload, direction
  labels matching signs (single leg, every curve leg, net), refusal of
  assumed notional/rates and empty curves in both tools, carry+roll sums.

---

# v1.1.0 — market-convention and agent-robustness patch (22 Jul 2026)

## Engine — convention fixes (numbers change)

1. **Semiannual maturity roll (post-Dec-2015).** `standard_cds_maturity` now
   implements the current single-name convention: maturities fall only on
   20 Jun / 20 Dec; anchor = 20 Jun for trades 20 Mar–19 Sep, else 20 Dec;
   maturity = anchor + tenor. Aligned with on-the-run index maturities.
   Previously the engine used the pre-2015 quarterly IMM grid.
   Example: 5Y traded 2026-07-21 → 2031-06-20 (was 2031-09-20).
   Applies to both the trade AND the curve pillars (the bootstrap now
   calibrates to the maturities the market quotes actually reference).

2. **Accrual between adjusted roll dates.** Non-final accrual periods now run
   between following-adjusted quarterly 20ths (payment date = adjusted period
   end), per the standard contract. Final period: adjusted start →
   unadjusted maturity, +1 day. Fixes accrued-day mismatches whenever a
   20th falls on a weekend (e.g. Sat 20 Jun 2026: 30 accrued days at a
   22 Jul step-in, previously 32).

3. **No silent 5Y default.** `run_pricer` raises `ValueError` if neither
   `tenor` nor `maturity_date` is supplied (previously priced a 5Y quietly).

### Reference pins (smoke trade: 10mm 5Y buy, 100c, R40, curve 80/120/160/200, rates 3.5/3.5/3.7, trade 2026-07-21)

| metric | v1.0 | v1.1 |
|---|---|---|
| maturity | 2031-09-20 | **2031-06-20** |
| clean upfront | 2.7112% / 271,124 | **2.5950% / 259,504** |
| CS01 | 4,404.28 | **4,220.64** |
| accrued | 32d / 8,888.89 | **30d / 8,333.33** |
| par spread | — | **160.0000bp** (= 5Y pillar, as it should) |

## Agent (`cds_agent.py`)

- System prompt now injects **today's date** at runtime (a local model has no
  clock and previously would have invented a trade date).
- Request now sets `options.num_ctx` (default 16384) and
  `options.temperature` (default 0.1) — the script bypasses Open WebUI
  presets, so raw Ollama defaults applied before.
- `--no-think` flag to disable reasoning on hybrid-thinking models; leaked
  `<think>` blocks are stripped from final answers regardless.
- Tool schema states that tenor OR maturity_date is required.

## New

- `quantlib_crosscheck.py`: external validation vs QuantLib's IsdaCdsEngine
  (independent ISDA implementation). 5 cases incl. HY points-upfront,
  distressed inverted curve, sell side, 1Y. Checks maturity dates against
  `ql.cdsMaturity(CDS2015)`, clean upfront within 1bp of notional, par vs
  fair spread within 0.5bp.

## Tests

- 14 → 17 tests: added semiannual roll-window cases, adjusted-accrual pin
  (30 days), and the missing-tenor error path. All pins updated.

## Still-known deviations from a full CDSW/Markit setup (accepted)

- Rates input is continuously-compounded zeros, not the ISDA deposit/swap
  bootstrap — feed matching zeros when reconciling upfronts externally.
- No cash-settlement (T+3) discounting of the upfront.
- No half-day accrual-on-default bias (ISDA C has it; sub-bp effect).
- Weekend-only holiday calendar.
