"""The engine: one loop per bot.

Every tick (60 seconds by default), for each bot:
  1. Look:      account, positions and live market data from Alpaca.
  2. Protect:   make sure every position has a stop order sitting at Alpaca.
  3. Forced:    rule-based exits (end-of-day flatten, time stops, broken trends).
  4. Menu:      the style lists the moves that are valid right now.
  5. Ask:       the bot's AI picks one move (or the code chooser, if the AI isn't answering).
  6. Check:     the risk layer can veto it; it sets the size and the stop.
  7. Record, then act: the decision is written to the database, then the order is sent.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from . import risk
from .alpaca import AlpacaClient, AlpacaError
from .brains import Brain, BrainError
from .config import ET, Config
from .db import DB, now_iso
from .styles import HOLD, STYLES, Move, daily_features, intraday_features, minutes_since_open

log = logging.getLogger("stockbots.engine")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BotRunner(threading.Thread):
    def __init__(
        self,
        bot: dict,
        cfg: Config,
        db: DB,
        client: AlpacaClient | None,
        brain: Brain | None,
        name: str,
        now_fn=utcnow,
    ):
        super().__init__(name=f"bot-{bot['id']}", daemon=True)
        self.bot = bot
        self.bot_id = bot["id"]
        self.cfg = cfg
        self.db = db
        self.client = client
        self.brain = brain
        self.display_name = name
        self.style = STYLES[bot["style"]]
        self.now_fn = now_fn
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._daily = {"day": None, "bars": {}}
        self._last_model_call = 0.0
        self._last_full_refresh = 0.0
        self._last_history = 0.0
        self._last_orders = 0.0
        self._last_flatten = -1e9
        self._mins_to_close: float | None = None
        self._seen_positions: set[str] = set()
        self.state: dict = {
            "status": "starting" if client else "not set up",
            "status_detail": "",
            "account": None,
            "positions": [],
            "clock": None,
            "history": None,
            "recent_orders": [],
            "entry_blocker": None,
            "model_status": "waiting" if brain else "no AI key: code-only (closes trades, never opens)",
            "last_update": None,
        }

    # ---- thread ---------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        if not self.client:
            return
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.tick()
            except Exception as exc:  # keep the loop alive no matter what
                log.exception("bot %s tick failed", self.bot_id)
                self._set(status="error", status_detail=f"{exc.__class__.__name__}: {str(exc)[:200]}")
            elapsed = time.monotonic() - started
            self._stop.wait(max(5.0, self.cfg.tick_seconds - elapsed))

    def _set(self, **kwargs) -> None:
        with self._lock:
            self.state.update(kwargs)
            self.state["last_update"] = now_iso()

    def snapshot(self) -> dict:
        with self._lock:
            state = dict(self.state)
        st = self.db.bot_state(self.bot_id)
        day = self.now_fn().astimezone(ET).date().isoformat()
        spend = self.db.spend_today(day, self.bot_id)
        return {
            "id": self.bot_id,
            "name": self.display_name,
            "ai_label": self.bot["ai_label"],
            "provider": self.bot["provider"],
            "model": self.cfg.provider_model(self.bot["provider"]),
            "has_ai_key": self.brain is not None,
            "style": self.style.key,
            "style_label": self.style.label,
            "rules": self.rules(),
            "paused": bool(st.get("paused")),
            "spend_today_usd": round(spend["usd"], 4),
            "calls_today": spend["calls"],
            "spend_cap_usd": self.cfg.model_daily_usd_cap,
            "entries_today": self.db.entries_today(day, self.bot_id),
            "max_entries": self.style.max_entries_per_day,
            "max_positions": self.style.max_positions,
            "size_pct": self.style.size_pct,
            **state,
        }

    def rules(self) -> str:
        st = self.db.bot_state(self.bot_id)
        return (st.get("rules") or "").strip() or self.style.default_rules

    # ---- one tick -------------------------------------------------------

    def tick(self) -> None:
        now = self.now_fn()
        now_et = now.astimezone(ET)
        today = now_et.date().isoformat()
        client = self.client

        clock = client.clock()
        if not clock["is_open"]:
            due = time.monotonic() - self._last_full_refresh >= self.cfg.closed_refresh_seconds
            if due or self.state.get("account") is None:
                self._refresh_account(clock, today)
                self._refresh_extras(force=self.state.get("history") is None)
            self._set(status="running", status_detail="market closed", clock=clock, entry_blocker="market closed")
            return

        account, positions, open_orders = self._refresh_account(clock, today)
        self._refresh_extras()
        meta = self._reconcile(positions, now)
        self._mins_to_close = _minutes_until(clock.get("next_close"), now)

        # Market data for the style's stocks plus anything we hold
        symbols = sorted(set(self.style.universe) | set(positions))
        features = self._features(symbols, now_et)

        paused = bool(self.db.bot_state(self.bot_id).get("paused"))

        # Every position gets a protective stop held at Alpaca.
        self._guard_stops(positions, open_orders, meta, features, now)

        # Forced exits never wait for the AI.
        if self._forced_exits(positions, meta, features, now_et, today):
            account, positions, open_orders = self._refresh_account(clock, today)
            meta = self.db.get_meta(self.bot_id)

        ctx = risk.RiskContext(
            cfg=self.cfg,
            style=self.style,
            now_et=now_et,
            market_open=True,
            account=account,
            positions=positions,
            meta=meta,
            entries_today=self.db.entries_today(today, self.bot_id),
            paused=paused,
            features=features,
            last_exit={s: self.db.last_exit_ts(self.bot_id, s) for s in symbols},
            last_order={s: self.db.last_order_ts(self.bot_id, s) for s in symbols},
            minutes_to_close=self._mins_to_close,
        )
        blocker = risk.entry_blocker(ctx)
        self._set(status="running", status_detail="", entry_blocker=blocker)

        if paused:
            return

        menu = self.build_menu(ctx, features, positions, meta, now_et)
        if len(menu) == 1:  # only "hold" is possible: rules decide, nothing to ask
            return

        self.decide_and_act(menu, ctx, account, positions, features, now_et, today)

    # ---- pieces ---------------------------------------------------------

    def _refresh_account(self, clock: dict, today: str):
        client = self.client
        account = client.account()
        positions = {p["symbol"]: p for p in client.positions() if p["qty"] > 0}
        open_orders = client.open_orders()
        meta = self.db.get_meta(self.bot_id)
        rows = []
        for sym, p in sorted(positions.items()):
            m = meta.get(sym) or {}
            rows.append({**p, "stop_price": m.get("stop_price"), "opened_at": m.get("opened_at"),
                         "added": bool(m.get("added"))})
        self._last_full_refresh = time.monotonic()
        day_pl = 0.0
        if account.get("last_equity"):
            day_pl = (account["equity"] / account["last_equity"] - 1) * 100
        public = {k: v for k, v in account.items() if k not in ("id", "account_number")}
        self._set(account={**public, "day_pl_pct": round(day_pl, 3)}, positions=rows, clock=clock)
        return account, positions, open_orders

    def _refresh_extras(self, force: bool = False) -> None:
        now = time.monotonic()
        try:
            if force or now - self._last_history > 900:
                self._last_history = now
                self._set(history=self.client.portfolio_history("1M", "1D"))
            if force or now - self._last_orders > 120:
                self._last_orders = now
                self._set(recent_orders=self.client.closed_orders(15))
        except AlpacaError as exc:
            log.warning("bot %s: extras refresh failed: %s", self.bot_id, exc)

    def _reconcile(self, positions: dict, now: datetime) -> dict:
        meta = self.db.get_meta(self.bot_id)
        for sym, m in meta.items():
            if sym in positions:
                continue
            opened = datetime.fromisoformat(m["opened_at"])
            if (now - opened).total_seconds() < 300 and sym not in self._seen_positions:
                continue  # order may still be filling
            self.db.delete_meta(self.bot_id, sym)
            if sym in self._seen_positions:
                self.db.record_exit(self.bot_id, sym)
                self.db.add_event(self.bot_id, "info", f"{sym} position closed at Alpaca (stop hit or closed outside the bot)")
                self._seen_positions.discard(sym)
        for sym in positions:
            self._seen_positions.add(sym)
            if sym not in meta:
                self.db.upsert_meta(self.bot_id, sym, now.isoformat(timespec="seconds"), None)
                self.db.add_event(self.bot_id, "info", f"Found a {sym} position the bot didn't open; it is now managed by the bot")
        return self.db.get_meta(self.bot_id)

    def _features(self, symbols: list[str], now_et: datetime) -> dict:
        client = self.client
        today = now_et.date()
        if self._daily["day"] != today.isoformat() or not self._daily["bars"]:
            start = now_et - timedelta(days=420)
            # Completed days only: ending at midnight keeps the request clear of the
            # free plan's "no SIP data from the last 15 minutes" rule.
            end = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
            feed = self.cfg.intraday_feed if self.style.intraday else self.cfg.daily_feed
            try:
                bars = client.bars(symbols, "1Day", start, end=end, feed=feed)
            except AlpacaError as exc:
                if feed != self.cfg.intraday_feed:
                    log.info("daily bars on %s feed not allowed (%s); using %s", feed, exc, self.cfg.intraday_feed)
                    bars = client.bars(symbols, "1Day", start, end=end, feed=self.cfg.intraday_feed)
                else:
                    raise
            self._daily = {"day": today.isoformat(), "bars": bars}
        missing = [s for s in symbols if s not in self._daily["bars"]]
        if missing:
            start = now_et - timedelta(days=420)
            end = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
            try:
                self._daily["bars"].update(client.bars(missing, "1Day", start, end=end, feed=self.cfg.intraday_feed))
            except AlpacaError:
                pass
        snaps = client.snapshots(symbols, feed=self.cfg.intraday_feed)
        intraday: dict = {}
        if self.style.intraday:
            open_et = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
            raw = client.bars(symbols, "5Min", open_et, feed=self.cfg.intraday_feed)
            for sym, rows in raw.items():
                intraday[sym] = [b for b in rows if 0 <= minutes_since_open(_parse_t(b["t"])) < 390]
        out = {}
        for sym in symbols:
            f = daily_features(self._daily["bars"].get(sym, []), snaps.get(sym), today)
            if f is None:
                continue
            if self.style.intraday:
                f = intraday_features(f, intraday.get(sym, []), now_et)
            out[sym] = f
        return out

    def _guard_stops(self, positions: dict, open_orders: list, meta: dict, features: dict, now: datetime) -> None:
        for sym, pos in positions.items():
            avail = int(pos.get("qty_available") or 0)
            if avail <= 0:
                continue  # every share is already covered by an order (its stop)
            last = self.db.last_order_ts(self.bot_id, sym)
            if last and (now - datetime.fromisoformat(last)).total_seconds() < 120:
                continue  # an order just went in; give its stop a moment to appear
            m = meta.get(sym) or {}
            f = features.get(sym) or {}
            stop = m.get("stop_price") or self.style.stop_for(f, pos["avg_entry_price"]) or round(pos["avg_entry_price"] * 0.92, 2)
            price = pos.get("current_price") or f.get("price") or 0
            if price and stop >= price:
                self._close(sym, "price is already below its stop level", decided_by="rules")
                continue
            coid = f"sb-{self.bot_id}-stop-{uuid.uuid4().hex[:10]}"
            try:
                order = self.client.submit_stop(sym, avail, stop, self.style.tif, coid)
                self.db.add_order(self.bot_id, sym, "sell", avail, "stop", stop_price=stop, client_order_id=coid,
                                  alpaca_id=(order or {}).get("id"), status=(order or {}).get("status"))
                self.db.upsert_meta(self.bot_id, sym, now.isoformat(timespec="seconds"), stop)
                self.db.add_event(self.bot_id, "info", f"Placed a protective stop for {avail} {sym} at {stop:.2f}")
            except AlpacaError as exc:
                self.db.add_order(self.bot_id, sym, "sell", avail, "stop", stop_price=stop, client_order_id=coid,
                                  error=str(exc))
                self.db.add_event(self.bot_id, "error", f"Couldn't place a stop for {sym}: {exc.message}")

    def _forced_exits(self, positions: dict, meta: dict, features: dict, now_et: datetime, today: str) -> bool:
        if not positions:
            return False
        flatten_at = self.style.flatten_all_at()
        near_close = self._mins_to_close is not None and self._mins_to_close <= 10  # half days too
        if flatten_at and (now_et.strftime("%H:%M") >= flatten_at or near_close):
            if time.monotonic() - self._last_flatten < 180:
                return False  # the closing orders from a moment ago are still filling
            self._last_flatten = time.monotonic()
            did = self.db.add_decision(self.bot_id, "close:ALL", "Close everything before the close",
                                       "rules", reason="end-of-day flatten", outcome="submitted")
            try:
                self.client.close_all()
                self.db.add_order(self.bot_id, "ALL", "sell", None, "flatten", status="submitted")
                for sym in positions:
                    self.db.delete_meta(self.bot_id, sym)
                    self.db.record_exit(self.bot_id, sym)
                    self._seen_positions.discard(sym)
            except AlpacaError as exc:
                self.db.set_decision_outcome(did, "error", exc.message)
            return True
        acted = False
        for sym, pos in positions.items():
            why = self.style.forced_exit(sym, features.get(sym), pos, meta.get(sym) or {}, now_et)
            if why:
                self._close(sym, why, decided_by="rules")
                acted = True
        return acted

    def build_menu(self, ctx: risk.RiskContext, features: dict, positions: dict, meta: dict, now_et) -> list[Move]:
        menu: list[Move] = []
        # Closes the style's rules suggest (the AI may still choose to hold)
        for sym, pos in positions.items():
            f = features.get(sym)
            if not f:
                continue
            why = self.style.soft_exit(sym, f, pos, meta.get(sym) or {}, now_et)
            if why:
                menu.append(Move(id=f"close:{sym}", kind="close", symbol=sym, text=why,
                                 score=-pos.get("unrealized_plpc", 0.0),
                                 flags={"loss": pos.get("unrealized_plpc", 0.0) < 0}))
        if risk.entry_blocker(ctx) is None:
            for sym, pos in positions.items():
                f = features.get(sym)
                if not f:
                    continue
                ok, why = self.style.add(sym, f, pos, meta.get(sym) or {})
                if ok:
                    m = Move(id=f"add:{sym}", kind="add", symbol=sym, text=why, score=pos.get("unrealized_plpc", 0.0))
                    v = risk.check(m, ctx)
                    if v.ok:
                        m.stop = v.stop
                        m.text += f" (adds {v.qty} shares, stop {v.stop:.2f})"
                        menu.append(m)
            if len(positions) < self.style.max_positions:
                candidates = []
                for sym, f in features.items():
                    if sym in positions or sym not in self.style.universe:
                        continue
                    ok, why, score = self.style.entry(sym, f)
                    if ok:
                        candidates.append((score, sym, why))
                candidates.sort(reverse=True)
                for score, sym, why in candidates:
                    if sum(1 for m in menu if m.kind == "open") >= self.cfg.max_menu_entries:
                        break
                    m = Move(id=f"open:{sym}", kind="open", symbol=sym, text=why, score=score)
                    v = risk.check(m, ctx)
                    if v.ok:
                        m.stop = v.stop
                        m.text += f" (buys {v.qty} shares, stop {v.stop:.2f})"
                        menu.append(m)
        menu.append(HOLD)
        return menu

    def state_for_ai(self, ctx: risk.RiskContext, positions: dict, features: dict, menu: list[Move], now_et) -> dict:
        def rnd(v, d=2):
            return None if v is None else round(float(v), d)

        keys = ("price", "day_change_pct", "sma5", "sma50", "sma200", "rsi2", "rsi14", "atr14", "bb_lower",
                "ret63_pct", "or_high", "or_low", "vwap", "rel_volume", "day_high", "mins_since_high")
        involved = {m.symbol for m in menu if m.symbol} | set(positions)
        market = {}
        for sym in sorted(involved):
            f = features.get(sym) or {}
            market[sym] = {k: rnd(f.get(k)) for k in keys if f.get(k) is not None}
        spy = features.get("SPY") or features.get("QQQ")
        close_et = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
        mins = self._mins_to_close if self._mins_to_close is not None else (close_et - now_et).total_seconds() / 60
        return {
            "time_et": now_et.strftime("%Y-%m-%d %H:%M"),
            "minutes_to_close": max(int(mins), 0),
            "account": {
                "equity": rnd(ctx.account.get("equity")),
                "cash": rnd(ctx.account.get("cash")),
                "day_pl_pct": rnd(ctx.day_pl_pct),
            },
            "limits": {
                "positions_held": len(positions),
                "max_positions": self.style.max_positions,
                "trades_left_today": max(self.style.max_entries_per_day - ctx.entries_today, 0),
            },
            "positions": [
                {
                    "symbol": s,
                    "qty": p["qty"],
                    "entry": rnd(p["avg_entry_price"]),
                    "price": rnd(p["current_price"]),
                    "pl_pct": rnd(p["unrealized_plpc"]),
                    "stop": rnd((ctx.meta.get(s) or {}).get("stop_price")),
                }
                for s, p in sorted(positions.items())
            ],
            "market_index_day_change_pct": rnd(spy.get("day_change_pct")) if spy else None,
            "symbols": market,
        }

    def code_choice(self, menu: list[Move]) -> Move:
        """The fallback when the AI isn't answering: take exits the rules suggest, never chase."""
        closes = [m for m in menu if m.kind == "close"]
        if closes:
            losers = [m for m in closes if m.flags.get("loss")]
            return (losers or closes)[0]
        if self.cfg.fallback_opens_trades:
            adds = sorted([m for m in menu if m.kind == "add"], key=lambda m: -m.score)
            if adds:
                return adds[0]
            opens = sorted([m for m in menu if m.kind == "open"], key=lambda m: -m.score)
            if opens:
                return opens[0]
        return HOLD

    def decide_and_act(self, menu, ctx, account, positions, features, now_et, today) -> None:
        menu_ids = [m.id for m in menu]
        by_id = {m.id: m for m in menu}
        rules = self.rules()
        pick = None
        why_code = None
        cost = 0.0
        if self.brain is None:
            why_code = "no AI key"
        elif time.monotonic() - self._last_model_call < self.cfg.model_min_interval_sec:
            return  # between paced calls the bot simply keeps what it has
        else:
            state = self.state_for_ai(ctx, positions, features, menu, now_et)
            spent = self.db.spend_today(today, self.bot_id)["usd"]
            worst = self.brain.worst_case_cost(self.display_name, self.style.label, rules, state, menu)
            if spent + worst > self.cfg.model_daily_usd_cap:
                why_code = f"daily AI budget reached (${spent:.2f} of ${self.cfg.model_daily_usd_cap:.2f})"
            else:
                self._last_model_call = time.monotonic()
                try:
                    pick = self.brain.pick(self.display_name, self.style.label, rules, state, menu)
                    cost = pick.cost_usd
                except BrainError as exc:
                    cost = exc.cost_usd
                    why_code = str(exc)
                if cost:
                    self.db.add_spend(today, self.bot_id, cost)

        if pick:
            move = by_id[pick.move_id]
            self._set(model_status="ok")
            decision_id = self.db.add_decision(
                self.bot_id, move.id, move.text, self.bot["provider"], model=pick.model,
                confidence=pick.confidence, reason=pick.reason, menu=menu_ids, cost_usd=cost,
                latency_ms=pick.latency_ms,
            )
        else:
            move = self.code_choice(menu)
            self._set(model_status=f"code is choosing: {why_code}")
            decision_id = self.db.add_decision(
                self.bot_id, move.id, move.text, "code", reason=why_code, menu=menu_ids, cost_usd=cost,
            )
        self.execute(move, ctx, decision_id, today)

    def execute(self, move: Move, ctx: risk.RiskContext, decision_id: int, today: str) -> None:
        if move.kind == "hold":
            self.db.set_decision_outcome(decision_id, "held")
            return
        verdict = risk.check(move, ctx)
        if not verdict.ok:
            self.db.set_decision_outcome(decision_id, "vetoed", verdict.reason)
            return
        sym = move.symbol
        if move.kind == "close":
            ok, detail = self._close(sym, move.text, decided_by=None)
            self.db.set_decision_outcome(decision_id, "submitted" if ok else "error", detail)
            return
        coid = f"sb-{self.bot_id}-{move.kind}-{uuid.uuid4().hex[:10]}"
        try:
            order = self.client.submit_market(sym, verdict.qty, "buy", self.style.tif, coid, stop_price=verdict.stop)
        except AlpacaError as exc:
            self.db.add_order(self.bot_id, sym, "buy", verdict.qty, move.kind, stop_price=verdict.stop,
                              client_order_id=coid, error=str(exc))
            self.db.set_decision_outcome(decision_id, "error", exc.message)
            return
        self.db.add_order(self.bot_id, sym, "buy", verdict.qty, move.kind, stop_price=verdict.stop,
                          client_order_id=coid, alpaca_id=(order or {}).get("id"), status=(order or {}).get("status"))
        now = self.now_fn().isoformat(timespec="seconds")
        self._seen_positions.add(sym)
        if move.kind == "open":
            self.db.upsert_meta(self.bot_id, sym, now, verdict.stop)
        else:
            self.db.upsert_meta(self.bot_id, sym, now, verdict.stop, added=True)
        self.db.add_entry(today, self.bot_id)
        self.db.set_decision_outcome(decision_id, "submitted",
                                     f"buy {verdict.qty} {sym}, stop {verdict.stop:.2f}")

    def _close(self, sym: str, why: str, decided_by: str | None) -> tuple[bool, str]:
        decision_id = None
        if decided_by:
            decision_id = self.db.add_decision(self.bot_id, f"close:{sym}", f"Close {sym}: {why}", decided_by,
                                               reason=why)
        try:
            self.client.close_position(sym)
        except AlpacaError as exc:
            self.db.add_order(self.bot_id, sym, "sell", None, "close", error=str(exc))
            if decision_id:
                self.db.set_decision_outcome(decision_id, "error", exc.message)
            return False, exc.message
        self.db.add_order(self.bot_id, sym, "sell", None, "close", status="submitted")
        self.db.delete_meta(self.bot_id, sym)
        self.db.record_exit(self.bot_id, sym)
        self._seen_positions.discard(sym)
        if decision_id:
            self.db.set_decision_outcome(decision_id, "submitted", f"sell all {sym}")
        return True, f"sell all {sym}"

    # ---- owner actions --------------------------------------------------

    def flatten(self) -> str:
        if not self.client:
            return "This bot isn't set up."
        self.client.close_all()
        self.db.add_order(self.bot_id, "ALL", "sell", None, "flatten", status="submitted")
        self.db.add_decision(self.bot_id, "close:ALL", "Close everything (owner)", "owner", outcome="submitted")
        self.db.add_event(self.bot_id, "info", "Owner closed all positions")
        return "Closing all positions (orders queue until the open if the market is closed)."


def _minutes_until(iso_ts: str | None, now: datetime) -> float | None:
    if not iso_ts:
        return None
    try:
        then = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if then.tzinfo is None:
        return None
    return (then - now).total_seconds() / 60.0


def _parse_t(t) -> datetime:
    if isinstance(t, datetime):
        return t.astimezone(ET)
    return datetime.fromisoformat(str(t).replace("Z", "+00:00")).astimezone(ET)


class Engine:
    """Owns the three bot loops."""

    def __init__(self, cfg: Config, db: DB):
        self.cfg = cfg
        self.db = db
        self.runners: dict[str, BotRunner] = {}
        self.started_at = utcnow()

    def start(self, runners: list[BotRunner]) -> None:
        self.stop()
        self.runners = {r.bot_id: r for r in runners}
        for r in runners:
            r.start()

    def stop(self) -> None:
        for r in self.runners.values():
            r.stop()
        self.runners = {}

    def snapshot(self) -> dict:
        bots = [r.snapshot() for r in self.runners.values()]
        clock = next((b.get("clock") for b in bots if b.get("clock")), None)
        return {
            "mode": "live" if self.cfg.live else "paper",
            "clock": clock,
            "bots": bots,
            "decisions": self.db.recent_decisions(80),
            "events": self.db.recent_events(25),
            "server_time": now_iso(),
        }
