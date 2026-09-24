"""Test cds_agent.py end-to-end against a mocked OpenAI-compatible server.

The fake speaks /v1/chat/completions, the shape both Ollama and vLLM serve:
replies are wrapped as {"choices": [{"message": ...}]}, tool-call arguments
are JSON strings, and tool results go back linked by tool_call_id.

Round 1: the fake model returns a tool call (price_cds or price_curve_trade).
Round 2: after receiving the tool result, it returns a text answer that
embeds numbers it was given, so we can assert the engine result flowed back
into the conversation. The curve-trade executor is also tested directly.
"""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

import cds_agent

# Shared market inputs: the v1.1 smoke-trade curves (CHANGES.md reference pins).
MARKET = {
    "trade_date": "2026-07-21",
    "recovery_pct": 40,
    "credit_curve": [
        {"tenor": "1Y", "value": 80},
        {"tenor": "3Y", "value": 120},
        {"tenor": "5Y", "value": 160},
        {"tenor": "10Y", "value": 200},
    ],
    "rate_curve": [
        {"tenor": "1Y", "value": 3.5},
        {"tenor": "5Y", "value": 3.5},
        {"tenor": "10Y", "value": 3.7},
    ],
    "notional_source": "user_stated",
    "rates_source": "user_stated",
    "defaulted_inputs": [],
}
BUY_5Y = {"tenor": "5Y", "coupon_bps": 100, "notional": 10000000, "buy_protection": True}
SELL_10Y = {"tenor": "10Y", "coupon_bps": 100, "notional": 10000000, "buy_protection": False}


def _tool_call(call_id, name, arguments):
    """An OpenAI-shape tool call; arguments travel as a JSON string."""
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments)
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


class FakeServer(BaseHTTPRequestHandler):
    """Fake /v1/chat/completions; subclasses implement respond(body) -> message."""

    last_body = None

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert self.path == "/v1/chat/completions", self.path
        # null content is rejected by Ollama's OpenAI layer, so it must never be sent
        assert all(m.get("content") is not None for m in body["messages"])
        type(self).last_body = body
        msg = self.respond(body)
        finish = "tool_calls" if msg.get("tool_calls") else "stop"
        out = json.dumps(
            {"choices": [{"index": 0, "message": msg, "finish_reason": finish}]}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):  # silence
        pass

    @staticmethod
    def tool_results(body):
        return [m for m in body["messages"] if m["role"] == "tool"]


class FakeSingleLeg(FakeServer):
    def respond(self, body):
        assert body["tools"][0]["function"]["name"] == "price_cds"
        results = self.tool_results(body)
        if not results:
            # Round 1: emit a tool call (content null, as vLLM sends it)
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call("call_1", "price_cds", {**MARKET, **BUY_5Y})],
            }
        # Round 2: echo back the upfront from the tool result
        assert results[0]["tool_call_id"] == "call_1"
        data = json.loads(results[0]["content"])["results"]
        return {
            "role": "assistant",
            "content": f"Upfront is {data['upfront_clean_pct']}% and CS01 is {data['cs01_total_per_1bp']}.",
        }


class FakeCurve(FakeServer):
    """Fake model that prices a 5s10s (buy 5Y / sell 10Y) as ONE curve-trade call."""

    def respond(self, body):
        assert [t["function"]["name"] for t in body["tools"]] == ["price_cds", "price_curve_trade"]
        results = self.tool_results(body)
        if not results:
            # leaked reasoning in content must not be echoed back into history
            return {
                "role": "assistant",
                "content": "Two legs, so one curve-trade call.</think>",
                "tool_calls": [
                    _tool_call("call_curve", "price_curve_trade", {**MARKET, "legs": [BUY_5Y, SELL_10Y]})
                ],
            }
        echoed = next(m for m in body["messages"] if m.get("tool_calls"))
        assert echoed["content"] == "", echoed["content"]
        assert results[0]["tool_call_id"] == "call_curve"
        data = json.loads(results[0]["content"])
        assert "net" not in data  # legs are priced separately, never netted
        leg1, leg2 = data["per_leg"]
        return {
            "role": "assistant",
            "content": (
                f"{data['inputs_echo']['leg_count']} legs. "
                f"Leg 1 upfront {leg1['upfront_clean_pct']}%. "
                f"Leg 2 CS01 {leg2['cs01_total_per_1bp']}, "
                f"carry+roll 1m {leg2['carry_plus_roll_1m']}."
            ),
        }


class FakeBadArgs(FakeServer):
    """Fake model whose tool-call arguments are not valid JSON."""

    def respond(self, body):
        results = self.tool_results(body)
        if not results:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [_tool_call("call_bad", "price_cds", '{"trade_date": "2026-07-21",')],
            }
        return {"role": "assistant", "content": json.loads(results[0]["content"])["error"]}


class FakeDrop(BaseHTTPRequestHandler):
    """Server that dies mid-request, as a local Ollama server did on GPU out-of-memory."""

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers["Content-Length"]))
        self.close_connection = True  # hang up without any response

    def log_message(self, *a):  # silence
        pass


class FakeText(FakeServer):
    """Fake model that answers directly, reasoning inline closed by a bare </think>."""

    def respond(self, body):
        return {"role": "assistant", "content": "17 times 3 is 51.\n</think>\n\n51"}


def _run_against(handler_cls, user_text, **kwargs):
    """Run one agent turn against a fake server; return (answer, messages)."""
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        messages = [
            {"role": "system", "content": cds_agent.build_system_prompt()},
            {"role": "user", "content": user_text},
        ]
        answer = cds_agent.run_turn(
            f"http://127.0.0.1:{server.server_address[1]}", "fake-model", messages, **kwargs
        )
        return answer, messages
    finally:
        server.shutdown()
        server.server_close()


class AgentLoopTest(unittest.TestCase):
    def test_full_tool_round_trip(self):
        answer, messages = _run_against(FakeSingleLeg, "Buy 10mm 5Y protection at 100 running...")
        # Engine reference values (v1.1 market conventions): upfront 2.595042%, CS01 4220.64
        self.assertIn("2.595042", answer)
        self.assertIn("4220.64", answer)
        # Conversation now holds assistant tool-call + tool result messages
        self.assertIn("tool", [m["role"] for m in messages])

    def test_curve_trade_round_trip(self):
        answer, messages = _run_against(
            FakeCurve, "5s10s: buy 10mm 5Y protection, sell 10mm 10Y, both 100 running..."
        )
        # Leg 1 is the v1.1 smoke trade, so it must hit the same pin as price_cds.
        self.assertIn("2 legs", answer)
        self.assertIn("Leg 1 upfront 2.595042%", answer)
        # Regression pins for leg 2 (sell 10mm 10Y) priced on its own.
        self.assertIn("Leg 2 CS01 -6810.29", answer)
        self.assertIn("carry+roll 1m 12999.96", answer)
        # One curve-trade call, not repeated price_cds calls.
        calls = [
            c["function"]["name"]
            for m in messages
            if m["role"] == "assistant"
            for c in m.get("tool_calls", [])
        ]
        self.assertEqual(calls, ["price_curve_trade"])

    def test_invalid_json_arguments_reported_to_model(self):
        answer, _ = _run_against(FakeBadArgs, "Buy 10mm 5Y protection...")
        self.assertIn("not valid JSON", answer)

    def test_dropped_connection_gives_clean_error(self):
        with self.assertRaises(SystemExit) as cm:
            _run_against(FakeDrop, "Buy 10mm 5Y protection...")
        self.assertIn("dropped the request", str(cm.exception))

    def test_executor_error_path(self):
        out = json.loads(cds_agent.execute_price_cds({"trade_date": "bad"}))
        self.assertIn("error", out)


class ReasoningTest(unittest.TestCase):
    def test_think_regex_strips_through_last_closing_tag(self):
        strip = lambda s: cds_agent._THINK_RE.sub("", s).strip()  # noqa: E731
        self.assertEqual(strip("reasoning only closed</think>\n\nAnswer"), "Answer")
        self.assertEqual(strip("<think>paired</think>Answer"), "Answer")
        self.assertEqual(strip("a</think>b</think>\nAnswer"), "Answer")
        self.assertEqual(strip("No reasoning at all"), "No reasoning at all")

    def test_bare_close_tag_stripped_from_answer(self):
        answer, _ = _run_against(FakeText, "What is 17*3?")
        self.assertEqual(answer, "51")

    def test_no_think_sends_reasoning_effort_none(self):
        _run_against(FakeText, "What is 17*3?", think=False)
        self.assertEqual(FakeText.last_body["reasoning_effort"], "none")
        self.assertEqual(FakeText.last_body["temperature"], 0.1)
        _run_against(FakeText, "What is 17*3?")
        self.assertNotIn("reasoning_effort", FakeText.last_body)


class CurveTradeExecutorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = json.loads(
            cds_agent.execute_price_curve_trade({**MARKET, "legs": [BUY_5Y, SELL_10Y]})
        )

    def test_each_leg_is_exactly_a_single_trade(self):
        # every figure price_cds gives, the leg gives, identically
        for leg_args, leg_out in zip((BUY_5Y, SELL_10Y), self.out["per_leg"]):
            single = json.loads(cds_agent.execute_price_cds({**MARKET, **leg_args}))["results"]
            for key, value in single.items():
                self.assertEqual(leg_out[key], value, key)

    def test_legs_not_netted(self):
        self.assertEqual(set(self.out), {"inputs_echo", "per_leg"})

    def test_carry_plus_roll_is_carry_plus_rolldown(self):
        res = json.loads(cds_agent.execute_price_cds({**MARKET, **BUY_5Y}))["results"]
        self.assertEqual(res["carry_plus_roll_1d"], round(res["carry_daily"] + res["rolldown_1d"], 2))
        self.assertEqual(res["carry_plus_roll_1m"], round(res["carry_monthly_30d"] + res["rolldown_1m"], 2))
        # smoke-trade pins: -277.78 + -127.53; -8,333.33 + -3,954.36
        self.assertEqual(res["carry_plus_roll_1d"], -405.31)
        self.assertEqual(res["carry_plus_roll_1m"], -12287.69)
        self.assertEqual(res["carry_plus_roll_1d_direction"], "LOSS")

    def test_every_leg_echoed_with_clean_and_dirty_rpv01(self):
        buy_3y = {**BUY_5Y, "tenor": "3Y", "notional": 5000000}
        out = json.loads(
            cds_agent.execute_price_curve_trade({**MARKET, "legs": [buy_3y, BUY_5Y, SELL_10Y]})
        )
        self.assertEqual(out["inputs_echo"]["leg_count"], 3)
        self.assertEqual([l["tenor"] for l in out["inputs_echo"]["legs"]], ["3Y", "5Y", "10Y"])
        for leg in out["per_leg"]:
            self.assertIn("rpv01_clean_years", leg)
            self.assertIn("rpv01_dirty_years", leg)

    def test_single_leg_rejected(self):
        out = json.loads(cds_agent.execute_price_curve_trade({**MARKET, "legs": [BUY_5Y]}))
        self.assertIn("use price_cds", out["error"])

    def test_error_names_the_bad_leg(self):
        bad = {k: v for k, v in SELL_10Y.items() if k != "coupon_bps"}
        out = json.loads(cds_agent.execute_price_curve_trade({**MARKET, "legs": [BUY_5Y, bad]}))
        self.assertIn("leg 2", out["error"])


class NoAssumedInputsTest(unittest.TestCase):
    """The executor refuses assumed notional/rates, so the model has to ask."""

    def assert_refused(self, args, fragment, tool=cds_agent.execute_price_cds):
        out = json.loads(tool(args))
        self.assertIn("error", out)
        self.assertIn(fragment, out["error"])

    def test_assumed_notional_refused(self):
        self.assert_refused({**MARKET, **BUY_5Y, "notional_source": "assumed"}, "ask the user")

    def test_missing_notional_source_refused(self):
        args = {k: v for k, v in {**MARKET, **BUY_5Y}.items() if k != "notional_source"}
        self.assert_refused(args, "notional not stated")

    def test_assumed_rates_refused(self):
        self.assert_refused({**MARKET, **BUY_5Y, "rates_source": "assumed"}, "rates not stated")

    def test_empty_rate_curve_refused_even_if_marked_stated(self):
        self.assert_refused({**MARKET, **BUY_5Y, "rate_curve": []}, "rates not stated")

    def test_empty_credit_curve_refused(self):
        self.assert_refused({**MARKET, **BUY_5Y, "credit_curve": []}, "no spread")

    def test_curve_trade_guarded_too(self):
        self.assert_refused(
            {**MARKET, "notional_source": "assumed", "legs": [BUY_5Y, SELL_10Y]},
            "notional not stated",
            tool=cds_agent.execute_price_curve_trade,
        )

    def test_both_schemas_require_the_source_fields(self):
        for schema in cds_agent.TOOLS:
            required = schema["function"]["parameters"]["required"]
            self.assertIn("notional_source", required)
            self.assertIn("rates_source", required)
            self.assertIn("defaulted_inputs", required)


class DisplayLinesTest(unittest.TestCase):
    """Code writes the printed lines, so upfront is always cash, never the price."""

    def test_single_trade_lines(self):
        res = json.loads(cds_agent.execute_price_cds({**MARKET, **BUY_5Y}))["results"]
        self.assertEqual(
            res["display"],
            [
                "Upfront clean (cash): PAY 259,504.25  (2.5950% of notional)",
                "Upfront dirty (cash): PAY 251,170.92  (2.5117% of notional)",
                "Accrued (30 days, cash): RECEIVE 8,333.33",
                "Price clean / dirty: 97.4050 / 97.4883",
                "Par spread: 160.00bp",
                "RPV01 clean / dirty: 4.3251 / 4.4084 years",
                "CS01: GAIN 4,220.64 per +1bp spread widening",
                "Carry: LOSS 277.78 daily / LOSS 8,333.33 per 30 days",
                "Rolldown: LOSS 127.53 (1d) / LOSS 892.73 (1w) / LOSS 3,954.36 (1m)",
                "Carry + roll: LOSS 405.31 (1d) / LOSS 12,287.69 (1m)",
            ],
        )

    def test_curve_legs_have_heading_and_cash_upfront(self):
        out = json.loads(
            cds_agent.execute_price_curve_trade({**MARKET, "legs": [BUY_5Y, SELL_10Y]})
        )
        leg2 = out["per_leg"][1]
        self.assertEqual(
            leg2["display_heading"],
            "Leg 2: SELL protection 10,000,000 10Y, matures 2036-06-20, coupon 100bp",
        )
        self.assertEqual(leg2["display"][0], "Upfront clean (cash): RECEIVE 736,714.09  (7.3671% of notional)")
        self.assertEqual(leg2["display"][2], "Accrued (30 days, cash): PAY 8,333.33")


class InputsDisplayTest(unittest.TestCase):
    """Code writes the inputs lines; '(default)' marks only what the model filled in."""

    def test_single_trade_inputs_lines(self):
        args = {**MARKET, **BUY_5Y, "defaulted_inputs": ["recovery", "trade_date"]}
        echo = json.loads(cds_agent.execute_price_cds(args))["inputs_echo"]
        self.assertEqual(
            echo["display"],
            [
                "Trade: BUY protection 10,000,000 5Y, matures 2031-06-20",
                "Coupon 100bp | curve 1Y 80bp / 3Y 120bp / 5Y 160bp / 10Y 200bp"
                " | rates 1Y 3.5% / 5Y 3.5% / 10Y 3.7%",
                "Recovery 40% (default) | trade date 2026-07-21",
            ],
        )

    def test_trade_date_today_flagged_by_code(self):
        today = cds_agent.date.today().isoformat()
        echo = json.loads(cds_agent.execute_price_cds({**MARKET, **BUY_5Y, "trade_date": today}))["inputs_echo"]
        self.assertEqual(echo["display"][2], f"Recovery 40% | trade date {today} (today)")

    def test_stated_coupon_not_marked_default(self):
        # the live mislabel: 'k100' was stated, so coupon must carry no tag
        echo = json.loads(cds_agent.execute_price_cds({**MARKET, **BUY_5Y}))["inputs_echo"]
        self.assertNotIn("(default)", " ".join(echo["display"]))

    def test_flat_rates_and_single_spread_wording(self):
        args = {
            **MARKET,
            **BUY_5Y,
            "credit_curve": [{"tenor": "5Y", "value": 124}],
            "rate_curve": [{"tenor": "1Y", "value": 3.5}, {"tenor": "10Y", "value": 3.5}],
        }
        echo = json.loads(cds_agent.execute_price_cds(args))["inputs_echo"]
        self.assertEqual(echo["display"][1], "Coupon 100bp | spread 124bp | rates flat 3.5%")

    def test_curve_trade_marks_defaulted_coupon_and_side_per_leg(self):
        args = {**MARKET, "defaulted_inputs": ["coupon", "side"], "legs": [BUY_5Y, SELL_10Y]}
        out = json.loads(cds_agent.execute_price_curve_trade(args))
        self.assertEqual(
            out["per_leg"][0]["display_heading"],
            "Leg 1: BUY protection (default) 10,000,000 5Y, matures 2031-06-20, coupon 100bp (default)",
        )
        self.assertEqual(
            out["inputs_echo"]["display"][0],
            "Curve 1Y 80bp / 3Y 120bp / 5Y 160bp / 10Y 200bp | rates 1Y 3.5% / 5Y 3.5% / 10Y 3.7%",
        )

    def test_defaulted_inputs_must_be_given_and_valid(self):
        missing = {k: v for k, v in {**MARKET, **BUY_5Y}.items() if k != "defaulted_inputs"}
        for args in (missing, {**MARKET, **BUY_5Y, "defaulted_inputs": ["notional"]}):
            out = json.loads(cds_agent.execute_price_cds(args))
            self.assertIn("defaulted_inputs must list", out["error"])


class DirectionLabelTest(unittest.TestCase):
    """Every signed amount carries a word, so no model has to read a sign."""

    def assert_labels_match_signs(self, block):
        labelled = [k for k in block if k in cds_agent._DIRECTION_WORDS]
        self.assertTrue(labelled)
        for key in labelled:
            pos, neg = cds_agent._DIRECTION_WORDS[key]
            v = block[key]
            self.assertEqual(block[f"{key}_direction"], pos if v > 0 else neg if v < 0 else "NONE", key)

    def test_single_leg_buyer(self):
        res = json.loads(cds_agent.execute_price_cds({**MARKET, **BUY_5Y}))["results"]
        self.assert_labels_match_signs(res)
        # spread 160 > coupon 100: buyer pays clean upfront, receives accrued
        self.assertEqual(res["upfront_clean_amount_direction"], "PAY")
        self.assertEqual(res["accrued_amount_direction"], "RECEIVE")
        self.assertEqual(res["cs01_total_per_1bp_direction"], "GAIN_IF_WIDER")
        self.assertEqual(res["carry_daily_direction"], "LOSS")

    def test_curve_trade_legs(self):
        out = json.loads(
            cds_agent.execute_price_curve_trade({**MARKET, "legs": [BUY_5Y, SELL_10Y]})
        )
        for leg in out["per_leg"]:
            self.assert_labels_match_signs(leg)
        buy_5y, sell_10y = out["per_leg"]
        # seller of 10Y at 200 vs a 100 coupon receives the upfront
        self.assertEqual(sell_10y["upfront_clean_amount_direction"], "RECEIVE")
        self.assertEqual(sell_10y["accrued_amount_direction"], "PAY")
        self.assertEqual(sell_10y["cs01_total_per_1bp_direction"], "LOSS_IF_WIDER")
        self.assertEqual(buy_5y["cs01_total_per_1bp_direction"], "GAIN_IF_WIDER")


if __name__ == "__main__":
    unittest.main(verbosity=2)
