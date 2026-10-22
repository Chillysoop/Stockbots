"""SQLite storage: decisions, orders, per-position notes, spending, bot state."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    bot TEXT NOT NULL,
    move TEXT NOT NULL,
    move_text TEXT,
    decided_by TEXT NOT NULL,      -- jev | claude | openai | code | rules
    model TEXT,
    confidence REAL,
    reason TEXT,
    menu TEXT,                     -- JSON list of move ids offered
    cost_usd REAL DEFAULT 0,
    latency_ms INTEGER,
    outcome TEXT,                  -- filled/submitted, vetoed, error, held
    detail TEXT
);
CREATE INDEX IF NOT EXISTS decisions_bot_ts ON decisions(bot, ts);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    bot TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL,
    kind TEXT,                     -- open | add | close | stop | flatten
    stop_price REAL,
    client_order_id TEXT,
    alpaca_id TEXT,
    status TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS orders_bot_ts ON orders(bot, ts);

CREATE TABLE IF NOT EXISTS position_meta (
    bot TEXT NOT NULL,
    symbol TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    stop_price REAL,
    added INTEGER DEFAULT 0,
    PRIMARY KEY (bot, symbol)
);

CREATE TABLE IF NOT EXISTS exits (
    bot TEXT NOT NULL,
    symbol TEXT NOT NULL,
    ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS spend (
    day TEXT NOT NULL,
    bot TEXT NOT NULL,
    usd REAL NOT NULL DEFAULT 0,
    calls INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, bot)
);

CREATE TABLE IF NOT EXISTS entries (
    day TEXT NOT NULL,
    bot TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, bot)
);

CREATE TABLE IF NOT EXISTS bot_state (
    bot TEXT PRIMARY KEY,
    paused INTEGER NOT NULL DEFAULT 0,
    rules TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    bot TEXT,
    level TEXT NOT NULL,
    message TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def _all(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def _one(self, sql: str, args: tuple = ()) -> dict | None:
        rows = self._all(sql, args)
        return rows[0] if rows else None

    # ---- decisions ------------------------------------------------------

    def add_decision(self, bot: str, move: str, move_text: str, decided_by: str, *, model=None,
                     confidence=None, reason=None, menu=None, cost_usd=0.0, latency_ms=None,
                     outcome=None, detail=None) -> int:
        cur = self._exec(
            "INSERT INTO decisions (ts, bot, move, move_text, decided_by, model, confidence, reason, menu,"
            " cost_usd, latency_ms, outcome, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), bot, move, move_text, decided_by, model, confidence, reason,
             json.dumps(menu or []), cost_usd, latency_ms, outcome, detail),
        )
        return int(cur.lastrowid)

    def set_decision_outcome(self, decision_id: int, outcome: str, detail: str | None = None) -> None:
        self._exec("UPDATE decisions SET outcome=?, detail=? WHERE id=?", (outcome, detail, decision_id))

    def recent_decisions(self, limit: int = 60, bot: str | None = None) -> list[dict]:
        if bot:
            return self._all("SELECT * FROM decisions WHERE bot=? ORDER BY id DESC LIMIT ?", (bot, limit))
        return self._all("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,))

    # ---- orders ---------------------------------------------------------

    def add_order(self, bot: str, symbol: str, side: str, qty: float, kind: str, *, stop_price=None,
                  client_order_id=None, alpaca_id=None, status=None, error=None) -> None:
        self._exec(
            "INSERT INTO orders (ts, bot, symbol, side, qty, kind, stop_price, client_order_id, alpaca_id,"
            " status, error) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), bot, symbol, side, qty, kind, stop_price, client_order_id, alpaca_id, status, error),
        )

    def last_order_ts(self, bot: str, symbol: str) -> str | None:
        row = self._one(
            "SELECT ts FROM orders WHERE bot=? AND symbol=? AND error IS NULL ORDER BY id DESC LIMIT 1",
            (bot, symbol),
        )
        return row["ts"] if row else None

    # ---- position notes -------------------------------------------------

    def get_meta(self, bot: str) -> dict[str, dict]:
        return {r["symbol"]: r for r in self._all("SELECT * FROM position_meta WHERE bot=?", (bot,))}

    def upsert_meta(self, bot: str, symbol: str, opened_at: str, stop_price: float | None, added: bool = False):
        self._exec(
            "INSERT INTO position_meta (bot, symbol, opened_at, stop_price, added) VALUES (?,?,?,?,?)"
            " ON CONFLICT(bot, symbol) DO UPDATE SET stop_price=COALESCE(excluded.stop_price, stop_price),"
            " added=MAX(added, excluded.added)",
            (bot, symbol, opened_at, stop_price, 1 if added else 0),
        )

    def delete_meta(self, bot: str, symbol: str) -> None:
        self._exec("DELETE FROM position_meta WHERE bot=? AND symbol=?", (bot, symbol))

    def record_exit(self, bot: str, symbol: str) -> None:
        self._exec("INSERT INTO exits (bot, symbol, ts) VALUES (?,?,?)", (bot, symbol, now_iso()))

    def last_exit_ts(self, bot: str, symbol: str) -> str | None:
        row = self._one("SELECT ts FROM exits WHERE bot=? AND symbol=? ORDER BY rowid DESC LIMIT 1", (bot, symbol))
        return row["ts"] if row else None

    # ---- spending and counters -----------------------------------------

    def spend_today(self, day: str, bot: str) -> dict:
        row = self._one("SELECT usd, calls FROM spend WHERE day=? AND bot=?", (day, bot))
        return row or {"usd": 0.0, "calls": 0}

    def add_spend(self, day: str, bot: str, usd: float) -> None:
        self._exec(
            "INSERT INTO spend (day, bot, usd, calls) VALUES (?,?,?,1)"
            " ON CONFLICT(day, bot) DO UPDATE SET usd=usd+excluded.usd, calls=calls+1",
            (day, bot, float(usd)),
        )

    def entries_today(self, day: str, bot: str) -> int:
        row = self._one("SELECT count FROM entries WHERE day=? AND bot=?", (day, bot))
        return int(row["count"]) if row else 0

    def add_entry(self, day: str, bot: str) -> None:
        self._exec(
            "INSERT INTO entries (day, bot, count) VALUES (?,?,1)"
            " ON CONFLICT(day, bot) DO UPDATE SET count=count+1",
            (day, bot),
        )

    # ---- bot state ------------------------------------------------------

    def bot_state(self, bot: str) -> dict:
        row = self._one("SELECT paused, rules FROM bot_state WHERE bot=?", (bot,))
        return row or {"paused": 0, "rules": None}

    def set_paused(self, bot: str, paused: bool) -> None:
        self._exec(
            "INSERT INTO bot_state (bot, paused) VALUES (?,?) ON CONFLICT(bot) DO UPDATE SET paused=excluded.paused",
            (bot, 1 if paused else 0),
        )

    def set_rules(self, bot: str, rules: str) -> None:
        self._exec(
            "INSERT INTO bot_state (bot, rules) VALUES (?,?) ON CONFLICT(bot) DO UPDATE SET rules=excluded.rules",
            (bot, rules),
        )

    # ---- events ---------------------------------------------------------

    def add_event(self, bot: str | None, level: str, message: str) -> None:
        self._exec("INSERT INTO events (ts, bot, level, message) VALUES (?,?,?,?)", (now_iso(), bot, level, message[:500]))

    def recent_events(self, limit: int = 30) -> list[dict]:
        return self._all("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
