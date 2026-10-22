"""Settings read from environment variables, with safe defaults.

Everything here can be overridden by an environment variable of the same name
(set it in Hostinger Docker Manager or in a .env file next to docker-compose.yml).
"""
from __future__ import annotations

import os
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def env_float(name: str, default: float) -> float:
    try:
        return float(env(name, str(default)))
    except (TypeError, ValueError):
        return default


def env_int(name: str, default: int) -> int:
    try:
        return int(float(env(name, str(default))))
    except (TypeError, ValueError):
        return default


def env_bool(name: str, default: bool) -> bool:
    value = env(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


class Config:
    """A snapshot of all settings. Built once at start; tests build their own."""

    def __init__(self) -> None:
        self.data_dir = env("DATA_DIR", "/data")
        self.settings_path = os.path.join(self.data_dir, "settings.json")
        self.db_path = os.path.join(self.data_dir, "stockbots.db")
        self.port = env_int("PORT", 8000)

        # Loop timing
        self.tick_seconds = env_int("TICK_SECONDS", 60)
        self.closed_refresh_seconds = env_int("CLOSED_REFRESH_SECONDS", 300)

        # Market data
        self.intraday_feed = env("DATA_FEED", "iex")
        self.daily_feed = env("DAILY_FEED", "sip")  # falls back to DATA_FEED if not allowed

        # Real money gate: needs BOTH of these, plus live Alpaca keys
        self.mode = (env("MODE", "paper") or "paper").lower()
        self.live_ack = env("LIVE_ACK", "")
        self.live = self.mode == "live" and self.live_ack == "I-ACCEPT-REAL-MONEY-RISK"

        # Setup page safety
        self.setup_window_min = env_int("SETUP_WINDOW_MIN", 120)

        # Risk layer (applies to every bot)
        self.daily_loss_stop_pct = env_float("DAILY_LOSS_STOP_PCT", 3.0)
        self.reentry_cooldown_min = env_int("REENTRY_COOLDOWN_MIN", 60)
        self.min_price = env_float("MIN_PRICE", 5.0)
        self.max_position_usd = env_float("MAX_POSITION_USD", 0.0)  # 0 = no cap beyond % of equity
        self.max_exposure_pct = env_float("MAX_EXPOSURE_PCT", 100.0)  # total holdings vs equity: 100 = never borrow
        self.entry_start_et = env("ENTRY_START_ET", "09:35")
        self.entry_end_et = env("ENTRY_END_ET", "15:45")
        self.fallback_opens_trades = env_bool("FALLBACK_OPENS_TRADES", False)
        self.max_menu_entries = env_int("MAX_MENU_ENTRIES", 4)

        # Model spending (per bot, per day, as this app counts it)
        self.model_daily_usd_cap = env_float("MODEL_DAILY_USD_CAP", 1.0)
        self.model_min_interval_sec = env_int("MODEL_MIN_INTERVAL_SEC", 60)
        self.model_timeout_sec = env_float("MODEL_TIMEOUT_SEC", 20.0)

        # Jev (TypeSafe)
        self.jev_model = env("JEV_MODEL", "jev-latest")
        self.jev_usd_per_mtok = env_float("JEV_USD_PER_MTOK", 0.042)
        self.jev_url = env("JEV_URL", "https://api.typesafe.ai/v1/systemone")

        # Claude (Anthropic API)
        self.claude_model = env("CLAUDE_MODEL", "claude-haiku-5-5")
        self.claude_usd_per_mtok_in = env_float("CLAUDE_USD_PER_MTOK_IN", 0.10)
        self.claude_usd_per_mtok_out = env_float("CLAUDE_USD_PER_MTOK_OUT", 0.50)
        self.claude_url = env("CLAUDE_URL", "https://api.anthropic.com/v1/messages")

        # OpenAI (the "Codex" bot)
        self.openai_model = env("OPENAI_MODEL", "gpt-6-luna")
        self.openai_usd_per_mtok_in = env_float("OPENAI_USD_PER_MTOK_IN", 0.10)
        self.openai_usd_per_mtok_out = env_float("OPENAI_USD_PER_MTOK_OUT", 0.50)
        self.openai_reasoning_effort = env("OPENAI_REASONING_EFFORT", "low")
        self.openai_url = env("OPENAI_URL", "https://api.openai.com/v1/responses")

        for price in (
            self.jev_usd_per_mtok,
            self.claude_usd_per_mtok_in,
            self.claude_usd_per_mtok_out,
            self.openai_usd_per_mtok_in,
            self.openai_usd_per_mtok_out,
        ):
            if price <= 0:
                raise SystemExit("Model prices must be above 0 so the spending cap works.")

    def provider_model(self, provider: str) -> str:
        return {
            "jev": self.jev_model,
            "claude": self.claude_model,
            "openai": self.openai_model,
        }.get(provider, provider)


# The three bots. Each has its own Alpaca paper account, AI and trading style.
BOTS = [
    {
        "id": "claude",
        "provider": "claude",
        "style": "trend",
        "default_name": "Claude Trend",
        "ai_label": "Claude",
    },
    {
        "id": "jev",
        "provider": "jev",
        "style": "dip",
        "default_name": "Jev Dips",
        "ai_label": "Jev",
    },
    {
        "id": "openai",
        "provider": "openai",
        "style": "momentum",
        "default_name": "Codex Momentum",
        "ai_label": "OpenAI",
    },
]

BOT_IDS = [b["id"] for b in BOTS]


def bot_def(bot_id: str) -> dict:
    for b in BOTS:
        if b["id"] == bot_id:
            return b
    raise KeyError(bot_id)
