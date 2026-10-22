"""Setup settings (keys, owner password) kept in /data/settings.json.

The file is readable only by the app. Environment variables, when set, win over
the file, so keys can also be given through Hostinger Docker Manager.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from .config import BOT_IDS, env

ENV_KEYS = {
    "jev_key": "JEV_API_KEY",
    "anthropic_key": "ANTHROPIC_API_KEY",
    "openai_key": "OPENAI_API_KEY",
}


def bot_env(bot_id: str, what: str) -> str:
    return f"ALPACA_{what.upper()}_{bot_id.upper()}"  # e.g. ALPACA_KEY_JEV, ALPACA_SECRET_JEV


class SettingsStore:
    def __init__(self, path: str):
        self.path = path
        self.data: dict = {}
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                self.data = json.load(fh)
        except FileNotFoundError:
            self.data = {}
        except (OSError, ValueError):
            self.data = {}

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2)
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)

    # ---- state ----------------------------------------------------------

    @property
    def setup_done(self) -> bool:
        return bool(self.data.get("setup_done_at") and self.data.get("owner_hash"))

    def key(self, name: str) -> str | None:
        return env(ENV_KEYS[name]) or self.data.get(name) or None

    def bot(self, bot_id: str) -> dict:
        b = dict((self.data.get("bots") or {}).get(bot_id) or {})
        b["alpaca_key"] = env(bot_env(bot_id, "key")) or b.get("alpaca_key")
        b["alpaca_secret"] = env(bot_env(bot_id, "secret")) or b.get("alpaca_secret")
        return b

    def bots_configured(self) -> list[str]:
        return [b for b in BOT_IDS if self.bot(b).get("alpaca_key") and self.bot(b).get("alpaca_secret")]

    # ---- owner password -------------------------------------------------

    def set_password(self, password: str) -> None:
        salt = secrets.token_bytes(16)
        digest = _hash(password, salt)
        self.data["owner_salt"] = base64.b64encode(salt).decode()
        self.data["owner_hash"] = base64.b64encode(digest).decode()
        self.data["session_secret"] = secrets.token_hex(32)

    def check_password(self, password: str) -> bool:
        env_pw = env("OWNER_PASSWORD")
        if env_pw and len(env_pw) >= 8 and hmac.compare_digest(env_pw.encode(), password.encode()):
            return True
        try:
            salt = base64.b64decode(self.data["owner_salt"])
            want = base64.b64decode(self.data["owner_hash"])
        except (KeyError, ValueError):
            return False
        return hmac.compare_digest(_hash(password, salt), want)

    # ---- session cookies ------------------------------------------------

    def _secret(self) -> bytes:
        s = self.data.get("session_secret")
        if not s:
            s = self.data["session_secret"] = secrets.token_hex(32)
        return s.encode()

    def make_session(self, days: int = 30) -> str:
        exp = int(time.time()) + days * 86400
        payload = f"v1.{exp}"
        sig = hmac.new(self._secret(), payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{sig}"

    def valid_session(self, token: str | None) -> bool:
        if not token or not self.setup_done:
            return False
        try:
            version, exp, sig = token.split(".")
        except ValueError:
            return False
        payload = f"{version}.{exp}"
        want = hmac.new(self._secret(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(want, sig):
            return False
        try:
            return int(exp) > time.time()
        except ValueError:
            return False


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
