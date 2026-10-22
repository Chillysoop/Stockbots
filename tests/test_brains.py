"""The three AI clients, with the network replaced by canned replies."""
import json
import unittest
from unittest import mock

from stockbots.brains import BrainError, ClaudeBrain, JevBrain, OpenAIBrain
from stockbots.styles import HOLD, Move

from .helpers import make_config

MENU = [Move(id="open:NVDA", kind="open", symbol="NVDA", text="Buy NVDA"), HOLD]
STATE = {"time_et": "2026-10-21 11:00", "symbols": {"NVDA": {"price": 100}}}


class FakeResp:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data
        self.text = json.dumps(data)
        self.reason = ""

    def json(self):
        return self._data


def replies(*responses):
    it = iter(responses)
    sent = []

    def post(url, headers=None, json=None, timeout=None):
        sent.append({"url": url, "headers": headers, "json": json})
        return next(it)

    return post, sent


class JevTests(unittest.TestCase):
    def test_pick_and_cost(self):
        post, sent = replies(FakeResp(200, {
            "model": "jev-1.13.0",
            "answers": {"move": {"type": "choice", "choice": "open:NVDA", "confidence": 0.81,
                                 "probabilities": {"open:NVDA": 0.9, "hold": 0.1}}},
            "usage": {"input_tokens": 1_000_000, "output_tokens": 0},
        }))
        with mock.patch("stockbots.brains.requests.post", post):
            pick = JevBrain(make_config(), "k").pick("Jev Dips", "Dip buyer", "rules", STATE, MENU)
        self.assertEqual(pick.move_id, "open:NVDA")
        self.assertAlmostEqual(pick.cost_usd, 0.042)
        self.assertEqual(pick.confidence, 0.81)
        q = sent[0]["json"]["questions"]["move"]
        self.assertEqual(q["type"], "choice")
        self.assertEqual(set(q["criteria"]), {"open:NVDA", "hold"})
        self.assertEqual(sent[0]["headers"]["Authorization"], "Bearer k")

    def test_off_menu_is_an_error(self):
        post, _ = replies(FakeResp(200, {"answers": {"move": {"choice": "open:TSLA"}}, "usage": {}}))
        with mock.patch("stockbots.brains.requests.post", post):
            with self.assertRaises(BrainError):
                JevBrain(make_config(), "k").pick("b", "s", "r", STATE, MENU)


class ClaudeTests(unittest.TestCase):
    def test_forced_tool_pick(self):
        post, sent = replies(FakeResp(200, {
            "model": "claude-haiku-5-5",
            "content": [{"type": "tool_use", "name": "pick_move", "input": {"move": "hold", "reason": "No setup."}}],
            "usage": {"input_tokens": 2000, "output_tokens": 40},
        }))
        with mock.patch("stockbots.brains.requests.post", post):
            pick = ClaudeBrain(make_config(), "k").pick("Claude Trend", "Trend follower", "rules", STATE, MENU)
        self.assertEqual(pick.move_id, "hold")
        self.assertEqual(pick.reason, "No setup.")
        self.assertAlmostEqual(pick.cost_usd, (2000 * 0.10 + 40 * 0.50) / 1e6)
        body = sent[0]["json"]
        self.assertEqual(body["tool_choice"], {"type": "tool", "name": "pick_move"})
        self.assertEqual(body["tools"][0]["input_schema"]["properties"]["move"]["enum"], ["open:NVDA", "hold"])
        self.assertEqual(sent[0]["headers"]["anthropic-version"], "2023-06-01")

    def test_http_error(self):
        post, _ = replies(FakeResp(401, {"error": {"type": "authentication_error", "message": "invalid x-api-key"}}))
        with mock.patch("stockbots.brains.requests.post", post):
            with self.assertRaises(BrainError) as cm:
                ClaudeBrain(make_config(), "k").pick("b", "s", "r", STATE, MENU)
        self.assertIn("401", str(cm.exception))


class OpenAITests(unittest.TestCase):
    def test_structured_pick_with_extra_message(self):
        post, sent = replies(FakeResp(200, {
            "model": "gpt-6-luna",
            "status": "completed",
            "output": [
                {"type": "reasoning", "summary": []},
                {"type": "message", "content": [{"type": "output_text", "text": "not json"}]},
                {"type": "message", "content": [{"type": "output_text", "text": "{\"move\":\"open:NVDA\",\"reason\":\"Breakout.\"}"}]},
            ],
            "usage": {"input_tokens": 1500, "output_tokens": 300},
        }))
        with mock.patch("stockbots.brains.requests.post", post):
            pick = OpenAIBrain(make_config(), "k").pick("Codex Momentum", "Momentum chaser", "rules", STATE, MENU)
        self.assertEqual(pick.move_id, "open:NVDA")
        body = sent[0]["json"]
        self.assertEqual(body["text"]["format"]["type"], "json_schema")
        self.assertTrue(body["text"]["format"]["strict"])
        self.assertEqual(body["reasoning"], {"effort": "low"})

    def test_drops_reasoning_if_unsupported(self):
        post, sent = replies(
            FakeResp(400, {"error": {"message": "Unsupported parameter: 'reasoning.effort'"}}),
            FakeResp(200, {"output": [{"type": "message", "content": [
                {"type": "output_text", "text": "{\"move\":\"hold\",\"reason\":\"Wait.\"}"}]}], "usage": {}}),
        )
        brain = OpenAIBrain(make_config(), "k")
        with mock.patch("stockbots.brains.requests.post", post):
            pick = brain.pick("b", "s", "r", STATE, MENU)
        self.assertEqual(pick.move_id, "hold")
        self.assertNotIn("reasoning", sent[1]["json"])
        self.assertFalse(brain.send_reasoning)

    def test_incomplete(self):
        post, _ = replies(FakeResp(200, {"status": "incomplete", "output": [], "usage": {"input_tokens": 10}}))
        with mock.patch("stockbots.brains.requests.post", post):
            with self.assertRaises(BrainError):
                OpenAIBrain(make_config(), "k").pick("b", "s", "r", STATE, MENU)


if __name__ == "__main__":
    unittest.main()
