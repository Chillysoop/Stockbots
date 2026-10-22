"""The AIs that pick moves: Jev (TypeSafe), Claude (Anthropic) and OpenAI.

Every brain gets the same thing: the bot's rules, a compact market snapshot, and
a menu of moves that are valid right now. It returns exactly one move id from the
menu. None of them can see an order endpoint or place a trade.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import requests

from .config import Config
from .styles import Move


@dataclass
class Pick:
    move_id: str
    confidence: float | None
    reason: str | None
    cost_usd: float
    model: str
    latency_ms: int


class BrainError(Exception):
    def __init__(self, message: str, cost_usd: float = 0.0):
        super().__init__(message)
        self.cost_usd = cost_usd


def system_prompt(bot_name: str, style_label: str, rules: str) -> str:
    return (
        f"You are {bot_name}, an automated US stock-trading bot ({style_label}). "
        "Each time you are asked, pick exactly one move from the menu. Only the listed moves are possible. "
        "Plain code checks every order, sizes it, and places a stop-loss; you only choose. "
        "Holding is a valid choice and is often correct.\n\nYour rules:\n" + rules.strip()
    )


def user_prompt(state: dict, menu: list[Move]) -> str:
    lines = "\n".join(f"- {m.id}: {m.text}" for m in menu)
    return (
        "Current state (JSON):\n"
        + json.dumps(state, separators=(",", ":"))
        + "\n\nMenu (pick one id):\n"
        + lines
        + "\n\nAnswer with one move id from the menu and a one-sentence reason."
    )


def _post(url: str, headers: dict, body: dict, timeout: float, retries: int = 1) -> requests.Response:
    last = None
    for attempt in range(retries + 1):
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=timeout)
        except requests.Timeout:
            raise BrainError("timed out")
        except requests.RequestException as exc:
            last = BrainError(f"network error ({exc.__class__.__name__})")
            if attempt < retries:
                time.sleep(1.0)
                continue
            raise last
        if resp.status_code in (429, 500, 502, 503, 529) and attempt < retries:
            time.sleep(1.5 * (attempt + 1))
            continue
        return resp
    raise last or BrainError("request failed")


def _error_text(resp: requests.Response) -> str:
    try:
        data = resp.json()
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict):
            return str(err.get("message") or err.get("type") or err)[:200]
        if isinstance(data, dict) and "detail" in data:
            return str(data["detail"])[:200]
        return str(data)[:200]
    except ValueError:
        return (resp.text or resp.reason or "")[:200]


def _approx_tokens(*texts: str) -> int:
    return int(sum(len(t) for t in texts) / 3.0) + 200


class Brain:
    provider = "base"

    def __init__(self, cfg: Config, api_key: str):
        self.cfg = cfg
        self.api_key = api_key

    @property
    def model(self) -> str:
        return self.cfg.provider_model(self.provider)

    def worst_case_cost(self, bot_name: str, style_label: str, rules: str, state: dict, menu: list[Move]) -> float:
        raise NotImplementedError

    def pick(self, bot_name: str, style_label: str, rules: str, state: dict, menu: list[Move]) -> Pick:
        raise NotImplementedError

    def check_key(self) -> tuple[bool, str]:
        raise NotImplementedError

    @staticmethod
    def _valid(move_id: str | None, menu: list[Move]) -> bool:
        return bool(move_id) and move_id in {m.id for m in menu}


class JevBrain(Brain):
    provider = "jev"

    def _instructions(self, bot_name, style_label, rules) -> str:
        return (
            f"Decide for {bot_name}, an automated stock-trading bot ({style_label}). Its rules: {rules.strip()} "
            "Pick the single best move for this bot right now, given its rules and the state. "
            "Holding is often correct."
        )

    def worst_case_cost(self, bot_name, style_label, rules, state, menu):
        text = json.dumps(state) + self._instructions(bot_name, style_label, rules) + "".join(m.text for m in menu)
        return _approx_tokens(text) * self.cfg.jev_usd_per_mtok / 1e6

    def pick(self, bot_name, style_label, rules, state, menu):
        body = {
            "model": self.cfg.jev_model,
            "state": state,
            "questions": {
                "move": {
                    "type": "choice",
                    "instructions": self._instructions(bot_name, style_label, rules),
                    "criteria": {m.id: m.text for m in menu},
                }
            },
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        t0 = time.monotonic()
        resp = _post(self.cfg.jev_url, headers, body, self.cfg.model_timeout_sec)
        latency = int((time.monotonic() - t0) * 1000)
        if resp.status_code >= 400:
            raise BrainError(f"Jev {resp.status_code}: {_error_text(resp)}")
        data = resp.json()
        usage = data.get("usage") or {}
        cost = float(usage.get("input_tokens") or 0) * self.cfg.jev_usd_per_mtok / 1e6
        answer = (data.get("answers") or {}).get("move") or {}
        choice = answer.get("choice")
        if not self._valid(choice, menu):
            raise BrainError(f"Jev answered off the menu ({choice!r})", cost)
        conf = answer.get("confidence")
        probs = answer.get("probabilities") or {}
        p = probs.get(choice)
        reason = f"probability {p:.0%}" if isinstance(p, (int, float)) else None
        return Pick(choice, float(conf) if conf is not None else None, reason, cost,
                    str(data.get("model") or self.model), latency)

    def check_key(self):
        parts = urlsplit(self.cfg.jev_url)
        url = f"{parts.scheme}://{parts.netloc}/v1/models"
        try:
            resp = requests.get(url, headers={"Authorization": f"Bearer {self.api_key}"}, timeout=10)
        except requests.RequestException as exc:
            return False, f"Couldn't reach TypeSafe ({exc.__class__.__name__})."
        if resp.status_code == 200:
            return True, "Jev key works."
        if resp.status_code in (401, 403):
            return False, "TypeSafe rejected this key."
        return False, f"TypeSafe answered {resp.status_code}."


class ClaudeBrain(Brain):
    provider = "claude"
    max_tokens = 300

    def _headers(self):
        return {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    def worst_case_cost(self, bot_name, style_label, rules, state, menu):
        tokens_in = _approx_tokens(system_prompt(bot_name, style_label, rules), user_prompt(state, menu)) + 300
        return (tokens_in * self.cfg.claude_usd_per_mtok_in + self.max_tokens * self.cfg.claude_usd_per_mtok_out) / 1e6

    def pick(self, bot_name, style_label, rules, state, menu):
        tool = {
            "name": "pick_move",
            "description": "Choose exactly one move from the menu.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "move": {"type": "string", "enum": [m.id for m in menu]},
                    "reason": {"type": "string", "description": "One short sentence."},
                },
                "required": ["move", "reason"],
            },
        }
        body = {
            "model": self.cfg.claude_model,
            "max_tokens": self.max_tokens,
            "system": system_prompt(bot_name, style_label, rules),
            "messages": [{"role": "user", "content": user_prompt(state, menu)}],
            "tools": [tool],
            "tool_choice": {"type": "tool", "name": "pick_move"},
        }
        t0 = time.monotonic()
        resp = _post(self.cfg.claude_url, self._headers(), body, self.cfg.model_timeout_sec)
        latency = int((time.monotonic() - t0) * 1000)
        if resp.status_code >= 400:
            raise BrainError(f"Claude {resp.status_code}: {_error_text(resp)}")
        data = resp.json()
        usage = data.get("usage") or {}
        tokens_in = (
            float(usage.get("input_tokens") or 0)
            + float(usage.get("cache_creation_input_tokens") or 0)
            + float(usage.get("cache_read_input_tokens") or 0)
        )
        cost = (
            tokens_in * self.cfg.claude_usd_per_mtok_in
            + float(usage.get("output_tokens") or 0) * self.cfg.claude_usd_per_mtok_out
        ) / 1e6
        for block in data.get("content") or []:
            if block.get("type") == "tool_use" and block.get("name") == "pick_move":
                args = block.get("input") or {}
                move = args.get("move")
                if self._valid(move, menu):
                    return Pick(move, None, str(args.get("reason") or "")[:240] or None, cost,
                                str(data.get("model") or self.model), latency)
                raise BrainError(f"Claude answered off the menu ({move!r})", cost)
        raise BrainError("Claude gave no pick", cost)

    def check_key(self):
        try:
            resp = requests.get("https://api.anthropic.com/v1/models", headers=self._headers(), timeout=10)
        except requests.RequestException as exc:
            return False, f"Couldn't reach Anthropic ({exc.__class__.__name__})."
        if resp.status_code == 200:
            return True, "Anthropic key works."
        if resp.status_code in (401, 403):
            return False, "Anthropic rejected this key."
        return False, f"Anthropic answered {resp.status_code}."


class OpenAIBrain(Brain):
    provider = "openai"
    max_output_tokens = 800

    def __init__(self, cfg, api_key):
        super().__init__(cfg, api_key)
        self.send_reasoning = bool(cfg.openai_reasoning_effort)

    def _headers(self):
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def worst_case_cost(self, bot_name, style_label, rules, state, menu):
        tokens_in = _approx_tokens(system_prompt(bot_name, style_label, rules), user_prompt(state, menu)) + 300
        return (
            tokens_in * self.cfg.openai_usd_per_mtok_in
            + self.max_output_tokens * self.cfg.openai_usd_per_mtok_out
        ) / 1e6

    def _body(self, bot_name, style_label, rules, state, menu):
        body = {
            "model": self.cfg.openai_model,
            "instructions": system_prompt(bot_name, style_label, rules),
            "input": user_prompt(state, menu),
            "max_output_tokens": self.max_output_tokens,
            "store": False,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "pick_move",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "move": {"type": "string", "enum": [m.id for m in menu]},
                            "reason": {"type": "string"},
                        },
                        "required": ["move", "reason"],
                        "additionalProperties": False,
                    },
                }
            },
        }
        if self.send_reasoning:
            body["reasoning"] = {"effort": self.cfg.openai_reasoning_effort}
        return body

    def pick(self, bot_name, style_label, rules, state, menu):
        t0 = time.monotonic()
        resp = _post(self.cfg.openai_url, self._headers(), self._body(bot_name, style_label, rules, state, menu),
                     self.cfg.model_timeout_sec)
        if resp.status_code == 400 and self.send_reasoning and "reasoning" in _error_text(resp).lower():
            # This model doesn't take a reasoning setting: drop it from now on.
            self.send_reasoning = False
            resp = _post(self.cfg.openai_url, self._headers(), self._body(bot_name, style_label, rules, state, menu),
                         self.cfg.model_timeout_sec)
        latency = int((time.monotonic() - t0) * 1000)
        if resp.status_code >= 400:
            raise BrainError(f"OpenAI {resp.status_code}: {_error_text(resp)}")
        data = resp.json()
        usage = data.get("usage") or {}
        cost = (
            float(usage.get("input_tokens") or 0) * self.cfg.openai_usd_per_mtok_in
            + float(usage.get("output_tokens") or 0) * self.cfg.openai_usd_per_mtok_out
        ) / 1e6
        texts: list[str] = []
        if isinstance(data.get("output_text"), str):
            texts.append(data["output_text"])
        for item in data.get("output") or []:
            if item.get("type") != "message":
                continue
            for part in item.get("content") or []:
                if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                    texts.append(part["text"])
        # Some models occasionally send more than one message item: take the first valid pick.
        for text in texts:
            try:
                args = json.loads(text)
            except ValueError:
                continue
            move = args.get("move") if isinstance(args, dict) else None
            if self._valid(move, menu):
                return Pick(move, None, str(args.get("reason") or "")[:240] or None, cost,
                            str(data.get("model") or self.model), latency)
        if data.get("status") == "incomplete":
            raise BrainError("OpenAI stopped before answering (token limit)", cost)
        raise BrainError("OpenAI gave no valid pick", cost)

    def check_key(self):
        try:
            resp = requests.get("https://api.openai.com/v1/models", headers=self._headers(), timeout=10)
        except requests.RequestException as exc:
            return False, f"Couldn't reach OpenAI ({exc.__class__.__name__})."
        if resp.status_code == 200:
            return True, "OpenAI key works."
        if resp.status_code in (401, 403):
            return False, "OpenAI rejected this key."
        return False, f"OpenAI answered {resp.status_code}."


BRAINS = {"jev": JevBrain, "claude": ClaudeBrain, "openai": OpenAIBrain}


def make_brain(provider: str, cfg: Config, api_key: str | None) -> Brain | None:
    if not api_key:
        return None
    return BRAINS[provider](cfg, api_key)
