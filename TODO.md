# Setup TODO — Ollama Agent + CDS Engine v1.1 on a local machine

v1.1 changes vs the original bundle: post-Dec-2015 semiannual maturity roll
(20 Jun / 20 Dec), accrual between adjusted roll dates, no silent 5Y default,
today's date injected into the system prompt, num_ctx/temperature set
explicitly, QuantLib cross-check harness included. See CHANGES.md.

## 1. Prerequisites

- [ ] `python3 --version` shows 3.10+ (`brew install python@3.12` if not)
- [ ] Ollama running: `curl http://localhost:11434/api/tags` returns JSON

## 2. Model

- [ ] You already have **qwen3.6:35b** — no new pull needed. Confirm tool
      support: `ollama show qwen3.6:35b` should list `tools` under capabilities
- [ ] Do NOT use Gemma 3 or deepseek-r1 with this agent (no reliable native
      tool-call format). gemma4:31b is fine.
- [ ] Set the context window on the Ollama server: `OLLAMA_CONTEXT_LENGTH=16384`
      (the agent talks to /v1, which ignores num_ctx). Unset, Ollama 0.32
      loads the model maximum (262k) and 27-31B models run a local machine
      out of GPU memory

## 3. Install

- [ ] Unzip to e.g. `~/cds-pricer`; folder contains `cds_agent.py`,
      `isda_cds/`, `test_pricer.py`, `quantlib_crosscheck.py`
- [ ] No pip installs needed for the agent (QuantLib only for step 6)

## 4. Smoke-test the engine alone (no LLM involved)

- [ ] `cd ~/cds-pricer && python3 -m unittest test_pricer test_agent_mock -v` — expect 49 tests OK (18 engine + 31 agent)
- [ ] `python3 -c "from isda_cds import CurvePoint, PricerRequest, run_pricer; r = run_pricer(PricerRequest(trade_date='2026-07-21', tenor='5Y', coupon_bps=100, notional=10_000_000, recovery_pct=40, buy_protection=True, credit_curve=[CurvePoint('1Y',80),CurvePoint('3Y',120),CurvePoint('5Y',160),CurvePoint('10Y',200)], rate_curve=[CurvePoint('1Y',3.5),CurvePoint('5Y',3.5),CurvePoint('10Y',3.7)])); print('maturity:', r.maturity_date, '| upfront %:', round(r.upfront_pct,4), '| CS01:', round(r.cs01_total,2), '| accrued d:', r.accrued_days)"`
- [ ] Expected: `maturity: 2031-06-20 | upfront %: 2.595 | CS01: 4220.64 | accrued d: 30`

## 5. Run the agent

- [ ] `python3 cds_agent.py --model qwen3.6:35b`
- [ ] Or one-shot: `python3 cds_agent.py --model qwen3.6:35b --once "Buy 10mm 5Y protection at 100 running, recovery 40, credit curve 1Y 80 / 3Y 120 / 5Y 160 / 10Y 200, rates flat 3.5 — upfront, CS01, carry, rolldown?"`
- [ ] Watch stderr for the `[tool] price_cds({...})` line
- [ ] Verify the echo line matches what you typed, and the numbers match step 4
- [ ] If answers arrive slowly or wrapped in reasoning, add `--no-think`
- [ ] Default temperature 0.1 (override with `--temperature`); context is
      server-side (step 2)
- [ ] vLLM instead of Ollama: `--host http://<ip>:<port> --model <served name>`

## 6. External validation (QuantLib) — do this once before trusting it

- [ ] `pip3 install QuantLib`
- [ ] `python3 quantlib_crosscheck.py` — expect all 5 cases PASS (maturity
      dates identical, upfront within 1bp of notional, spreads within 0.5bp)
- [ ] Any BREACH: stop and investigate before using the engine for anything real

## 7. Negative tests (model discipline)

- [ ] Ask "price me 5Y IG protection" with nothing else — the model must ASK
      for notional and curve, not invent them
- [ ] Ask a trade with no tenor — the engine now errors and the model must
      ask (no silent 5Y default)
- [ ] Ask "roughly what would the upfront be" — the model must still call the
      tool, never estimate

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Cannot reach Ollama` | Start Ollama; or pass `--host http://<ip>:11434` |
| Model answers without calling the tool | Not tool-capable (Gemma 3/r1) — use Qwen or gemma4; or re-ask mentioning "use the pricing tool" |
| `dropped the request` / Ollama log shows `Insufficient Memory` | Context too large for GPU memory — set `OLLAMA_CONTEXT_LENGTH` and restart Ollama |
| Garbled tool arguments | Bigger quant; temperature is already 0.1 |
| `ImportError: isda_cds` | Run from the agent's folder |
| Server rejects the request with `--no-think` | It doesn't accept `reasoning_effort: "none"` — drop the flag |
| Slow first response | Cold model load — later turns are faster |
