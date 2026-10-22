"""Wires settings, database, Alpaca clients and AIs into running bots."""
from __future__ import annotations

import logging
import os
import time

from .alpaca import AlpacaClient
from .brains import make_brain
from .config import BOTS, Config
from .db import DB
from .engine import BotRunner, Engine, utcnow
from .settings_store import SettingsStore

log = logging.getLogger("stockbots.app")

PROVIDER_KEY = {"jev": "jev_key", "claude": "anthropic_key", "openai": "openai_key"}


class App:
    def __init__(self, cfg: Config, client_factory=None, brain_factory=None, now_fn=utcnow):
        self.cfg = cfg
        os.makedirs(cfg.data_dir, exist_ok=True)
        self.db = DB(cfg.db_path)
        self.settings = SettingsStore(cfg.settings_path)
        self.engine = Engine(cfg, self.db)
        self.started = time.time()
        self.client_factory = client_factory or (lambda key, secret: AlpacaClient(key, secret, live=cfg.live))
        self.brain_factory = brain_factory or make_brain
        self.now_fn = now_fn
        if cfg.mode == "live" and not cfg.live:
            log.warning("MODE=live is set but LIVE_ACK is missing: staying on PAPER.")

    def setup_status(self) -> dict:
        if self.settings.setup_done:
            return {"open": False, "done": True, "reason": "Setup is finished."}
        left = self.cfg.setup_window_min * 60 - (time.time() - self.started)
        if left <= 0:
            return {
                "open": False,
                "done": False,
                "reason": "Setup locked: it stays open for "
                f"{self.cfg.setup_window_min} minutes after the server starts. Restart the app container to reopen it.",
            }
        return {"open": True, "done": False, "minutes_left": int(left // 60) + 1}

    def start_bots(self) -> None:
        runners = []
        for bot in BOTS:
            b = self.settings.bot(bot["id"])
            client = None
            enabled = b.get("enabled", True) is not False
            if enabled and b.get("alpaca_key") and b.get("alpaca_secret"):
                client = self.client_factory(b["alpaca_key"], b["alpaca_secret"])
            ai_key = self.settings.key(PROVIDER_KEY[bot["provider"]])
            brain = self.brain_factory(bot["provider"], self.cfg, ai_key)
            name = (b.get("name") or "").strip() or bot["default_name"]
            runners.append(BotRunner(bot, self.cfg, self.db, client, brain, name, now_fn=self.now_fn))
        self.engine.start(runners)
        log.info("Started %d bots (%s mode)", len([r for r in runners if r.client]),
                 "LIVE" if self.cfg.live else "paper")

    def reopen_setup(self) -> None:
        """Stop the bots and start Setup over (positions and their stops stay at Alpaca)."""
        self.engine.stop()
        try:
            os.remove(self.settings.path)
        except FileNotFoundError:
            pass
        self.settings.data = {}
        self.started = time.time()  # a fresh setup window
        self.db.add_event(None, "info", "Owner reopened Setup; bots stopped until it's finished")
