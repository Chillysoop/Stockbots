"""The risk layer: plain code that has the final say on every move.

It can veto a move, and it decides the size and the stop. The AI never sizes a
trade and never sets a stop.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import Config
from .styles import Move, Style


@dataclass
class Verdict:
    ok: bool
    reason: str
    qty: int = 0
    stop: float | None = None


@dataclass
class RiskContext:
    cfg: Config
    style: Style
    now_et: datetime
    market_open: bool
    account: dict
    positions: dict  # symbol -> position
    meta: dict  # symbol -> position notes
    entries_today: int
    paused: bool
    features: dict  # symbol -> features
    last_exit: dict = field(default_factory=dict)  # symbol -> ISO time
    last_order: dict = field(default_factory=dict)  # symbol -> ISO time
    minutes_to_close: float | None = None  # from Alpaca's clock (handles half days)

    @property
    def day_pl_pct(self) -> float:
        last = self.account.get("last_equity") or 0
        if not last:
            return 0.0
        return (self.account.get("equity", 0) / last - 1.0) * 100.0

    @property
    def loss_stopped(self) -> bool:
        return self.day_pl_pct <= -abs(self.cfg.daily_loss_stop_pct)

    def in_entry_window(self) -> bool:
        start = self.style.entry_start or self.cfg.entry_start_et
        end = self.style.entry_end or self.cfg.entry_end_et
        hm = self.now_et.strftime("%H:%M")
        return start <= hm <= end


def _minutes_since(iso_ts: str | None, now_utc: datetime) -> float | None:
    if not iso_ts:
        return None
    try:
        then = datetime.fromisoformat(iso_ts)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (now_utc - then).total_seconds() / 60.0


def entry_blocker(ctx: RiskContext) -> str | None:
    """Why no new trades (opens or adds) are allowed right now, or None."""
    if not ctx.market_open:
        return "market closed"
    if ctx.account.get("trading_blocked") or ctx.account.get("account_blocked"):
        return "account blocked by Alpaca"
    if ctx.paused:
        return "bot paused"
    if ctx.loss_stopped:
        return f"daily loss stop hit ({ctx.day_pl_pct:.1f}% today)"
    if not ctx.in_entry_window():
        return "outside entry hours"
    if ctx.minutes_to_close is not None and ctx.minutes_to_close < 15:
        return "too close to the closing bell"
    if ctx.entries_today >= ctx.style.max_entries_per_day:
        return f"daily trade limit reached ({ctx.style.max_entries_per_day})"
    return None


def full_size_qty(ctx: RiskContext, price: float) -> int:
    budget = ctx.account.get("equity", 0) * ctx.style.size_pct / 100.0
    if ctx.cfg.max_position_usd > 0:
        budget = min(budget, ctx.cfg.max_position_usd)
    return int(math.floor(budget / price)) if price > 0 else 0


def check(move: Move, ctx: RiskContext) -> Verdict:
    if move.kind == "hold":
        return Verdict(True, "hold")

    if not ctx.market_open:
        return Verdict(False, "market closed")
    if ctx.account.get("trading_blocked") or ctx.account.get("account_blocked"):
        return Verdict(False, "account blocked by Alpaca")

    sym = move.symbol or ""
    pos = ctx.positions.get(sym)

    if move.kind == "close":
        if not pos or pos.get("qty", 0) <= 0:
            return Verdict(False, f"no {sym} position to close")
        return Verdict(True, "close", qty=int(pos["qty"]))

    blocker = entry_blocker(ctx)
    if blocker:
        return Verdict(False, blocker)

    f = ctx.features.get(sym)
    if not f or not f.get("price"):
        return Verdict(False, f"no live price for {sym}")
    price = float(f["price"])
    if price < ctx.cfg.min_price:
        return Verdict(False, f"{sym} under ${ctx.cfg.min_price:.0f}")

    now_utc = ctx.now_et.astimezone(timezone.utc)
    since_order = _minutes_since(ctx.last_order.get(sym), now_utc)
    if since_order is not None and since_order < 2:
        return Verdict(False, f"{sym} traded under 2 minutes ago")

    stop = ctx.style.stop_for(f, price)
    if stop is None or not (0 < stop < price):
        return Verdict(False, f"no valid stop for {sym}")
    if (price - stop) / price > 0.15:
        return Verdict(False, f"{sym} stop too far away")

    full_qty = full_size_qty(ctx, price)

    if move.kind == "open":
        if pos:
            return Verdict(False, f"already holding {sym}")
        if len(ctx.positions) >= ctx.style.max_positions:
            return Verdict(False, f"already at {ctx.style.max_positions} positions")
        since_exit = _minutes_since(ctx.last_exit.get(sym), now_utc)
        if since_exit is not None and since_exit < ctx.cfg.reentry_cooldown_min:
            return Verdict(False, f"{sym} closed {since_exit:.0f} min ago (cooldown)")
        qty = full_qty
    elif move.kind == "add":
        if not pos:
            return Verdict(False, f"no {sym} position to add to")
        if (ctx.meta.get(sym) or {}).get("added"):
            return Verdict(False, f"already added to {sym}")
        qty = int(math.floor(full_qty * ctx.style.add_fraction))
        limit = ctx.account.get("equity", 0) * ctx.style.size_pct / 100.0 * 1.5
        if ctx.cfg.max_position_usd > 0:
            limit = min(limit, ctx.cfg.max_position_usd * 1.5)
        room = limit - pos.get("market_value", 0)
        qty = min(qty, int(math.floor(room / price)) if room > 0 else 0)
    else:
        return Verdict(False, f"unknown move {move.kind}")

    bp = ctx.account.get("buying_power", 0)
    if qty * price * 1.02 > bp:
        qty = int(math.floor(bp / (price * 1.02)))
    if qty < 1:
        return Verdict(False, f"not enough room or buying power for 1 share of {sym}")
    return Verdict(True, "ok", qty=qty, stop=stop)
