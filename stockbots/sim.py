"""A pretend Alpaca account and market, for tests and the demo dashboard.

It speaks the same methods as AlpacaClient, fills market orders instantly at the
current price, and holds stop orders that trigger when price falls through them.
Nothing here talks to the internet.
"""
from __future__ import annotations

import itertools
import math
import random
import threading
import uuid
from datetime import date, datetime, timedelta, timezone

from .alpaca import AlpacaError
from .config import ET


def business_days_back(end: date, n: int) -> list[date]:
    days = []
    d = end
    while len(days) < n:
        d = d - timedelta(days=1)
        if d.weekday() < 5:
            days.append(d)
    return list(reversed(days))


def make_daily(start_price: float, days: list[date], drift: float, vol: float, seed: int) -> list[dict]:
    rng = random.Random(seed)
    bars = []
    price = start_price
    for d in days:
        o = price
        c = max(1.0, o * (1 + drift + rng.gauss(0, vol)))
        h = max(o, c) * (1 + abs(rng.gauss(0, vol / 2)))
        l = min(o, c) * (1 - abs(rng.gauss(0, vol / 2)))
        t = datetime(d.year, d.month, d.day, 4, 0, tzinfo=timezone.utc)
        bars.append({"t": t.isoformat().replace("+00:00", "Z"), "o": o, "h": h, "l": l, "c": c,
                     "v": rng.randint(800_000, 1_200_000), "vw": (o + c) / 2})
        price = c
    return bars


class SimMarket:
    def __init__(self, now_fn, seed: int = 7):
        self.now_fn = now_fn
        self.lock = threading.RLock()
        self.daily: dict[str, list[dict]] = {}
        self.price: dict[str, float] = {}
        self.intraday: dict[str, list[dict]] = {}
        self.rng = random.Random(seed)
        self.jitter = 0.0  # random price moves per tick (demo)
        self.volume_mult = 1.0  # intraday volume vs a normal day (demo)

    def add_symbol(self, sym: str, daily: list[dict], price: float | None = None):
        with self.lock:
            self.daily[sym] = daily
            self.price[sym] = price if price is not None else daily[-1]["c"]

    def set_price(self, sym: str, price: float):
        with self.lock:
            self.price[sym] = price

    def step(self):
        if not self.jitter:
            return
        with self.lock:
            for sym in self.price:
                self.price[sym] = max(1.0, self.price[sym] * (1 + self.rng.gauss(0.0002, self.jitter)))

    def prev_close(self, sym: str) -> float:
        return self.daily[sym][-1]["c"]


class SimAlpaca:
    def __init__(self, market: SimMarket, cash: float = 100_000.0, account_number: str | None = None):
        self.m = market
        self.cash = cash
        self.start_equity = cash
        self.last_equity = cash
        self.account_number = account_number or uuid.uuid4().hex[:10]
        self.pos: dict[str, dict] = {}  # sym -> {qty, avg}
        self.orders: list[dict] = []  # all orders
        self.history: list[tuple[int, float]] = []
        self.ids = itertools.count(1)
        self.lock = threading.RLock()
        self.fail_orders = False

    # helpers
    def _now(self):
        return self.m.now_fn()

    def _price(self, sym):
        return self.m.price[sym]

    def _equity(self):
        return self.cash + sum(p["qty"] * self._price(s) for s, p in self.pos.items())

    def _run_stops(self):
        for o in self.orders:
            if o["status"] == "new" and o["type"] == "stop" and self._price(o["symbol"]) <= o["stop"]:
                self._fill(o["symbol"], "sell", o["qty"])
                o["status"] = "filled"
                o["filled_at"] = self._now().isoformat()
                o["filled_avg_price"] = self._price(o["symbol"])

    def _fill(self, sym, side, qty):
        price = self._price(sym)
        if side == "buy":
            p = self.pos.setdefault(sym, {"qty": 0, "avg": 0.0})
            p["avg"] = (p["avg"] * p["qty"] + price * qty) / (p["qty"] + qty)
            p["qty"] += qty
            self.cash -= price * qty
        else:
            p = self.pos.get(sym)
            if not p:
                return
            qty = min(qty, p["qty"])
            p["qty"] -= qty
            self.cash += price * qty
            if p["qty"] <= 0:
                del self.pos[sym]

    def _held(self, sym):
        return sum(o["qty"] for o in self.orders if o["status"] == "new" and o["symbol"] == sym and o["side"] == "sell")

    # AlpacaClient interface
    def clock(self):
        now = self._now().astimezone(ET)
        open_ = now.weekday() < 5 and (now.hour, now.minute) >= (9, 30) and now.hour < 16
        self.m.step()
        with self.lock:
            self._run_stops()
        close = now.replace(hour=16, minute=0, second=0, microsecond=0)
        nxt = now.replace(hour=9, minute=30, second=0, microsecond=0)
        if now >= nxt:
            nxt += timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
        return {"is_open": open_, "timestamp": now.isoformat(), "next_open": nxt.isoformat(),
                "next_close": close.isoformat()}

    def account(self):
        with self.lock:
            eq = self._equity()
            self.history.append((int(self._now().timestamp()), eq))
            return {"id": self.account_number, "account_number": self.account_number, "status": "ACTIVE",
                    "equity": eq, "last_equity": self.last_equity, "cash": self.cash,
                    "buying_power": max(self.cash, 0) * 2, "trading_blocked": False, "account_blocked": False}

    def positions(self):
        with self.lock:
            out = []
            for s, p in self.pos.items():
                price = self._price(s)
                out.append({"symbol": s, "qty": float(p["qty"]), "qty_available": float(p["qty"] - self._held(s)),
                            "side": "long", "avg_entry_price": p["avg"], "current_price": price,
                            "market_value": price * p["qty"], "unrealized_pl": (price - p["avg"]) * p["qty"],
                            "unrealized_plpc": (price / p["avg"] - 1) * 100})
            return out

    def open_orders(self):
        with self.lock:
            return [dict(o) for o in self.orders if o["status"] == "new"]

    def closed_orders(self, limit=20):
        with self.lock:
            done = [o for o in self.orders if o["status"] == "filled"][-limit:]
            return [{"symbol": o["symbol"], "side": o["side"], "type": o["type"], "status": o["status"],
                     "qty": o["qty"], "price": o.get("filled_avg_price"), "time": o.get("filled_at"),
                     "client_order_id": o.get("client_order_id")} for o in reversed(done)]

    def _order(self, **kw):
        o = {"id": str(next(self.ids)), "status": "new", **kw}
        self.orders.append(o)
        return o

    def submit_market(self, symbol, qty, side, tif, client_order_id, stop_price=None):
        with self.lock:
            if self.fail_orders:
                raise AlpacaError(403, "insufficient buying power")
            self._fill(symbol, side, qty)
            o = self._order(symbol=symbol, qty=qty, side=side, type="market", tif=tif,
                            client_order_id=client_order_id)
            o["status"] = "filled"
            o["filled_at"] = self._now().isoformat()
            o["filled_avg_price"] = self._price(symbol)
            if stop_price is not None:
                self._order(symbol=symbol, qty=qty, side="sell", type="stop", stop=stop_price, tif=tif,
                            client_order_id=client_order_id + "-sl")
            return dict(o)

    def submit_stop(self, symbol, qty, stop_price, tif, client_order_id):
        with self.lock:
            return dict(self._order(symbol=symbol, qty=qty, side="sell", type="stop", stop=stop_price, tif=tif,
                                    client_order_id=client_order_id))

    def cancel_order(self, order_id):
        with self.lock:
            for o in self.orders:
                if o["id"] == order_id and o["status"] == "new":
                    o["status"] = "canceled"

    def close_position(self, symbol):
        with self.lock:
            for o in self.orders:
                if o["symbol"] == symbol and o["status"] == "new":
                    o["status"] = "canceled"
            p = self.pos.get(symbol)
            if not p:
                return None
            qty = p["qty"]
            self._fill(symbol, "sell", qty)
            return dict(self._order(symbol=symbol, qty=qty, side="sell", type="market", status="filled",
                                    filled_avg_price=self._price(symbol), filled_at=self._now().isoformat()))

    def close_all(self):
        with self.lock:
            for s in list(self.pos):
                self.close_position(s)

    def portfolio_history(self, period="1M", timeframe="1D"):
        with self.lock:
            # A month of pretend history ending today, then today's live points.
            now = self._now()
            base = self.start_equity
            ts, eq = [], []
            rng = random.Random(hash(self.account_number) & 0xFFFF)
            value = base
            walk = []
            for i in range(21, 0, -1):
                ts.append(int((now - timedelta(days=i)).timestamp()))
                value *= 1 + rng.gauss(0.0006, 0.006)
                walk.append(value)
            # bend the pretend walk so it ends at yesterday's real close
            drift = (self.last_equity - walk[-1]) / len(walk)
            eq = [round(w + drift * (k + 1), 2) for k, w in enumerate(walk)]
            eq[0] = base
            ts.append(int(now.timestamp()))
            eq.append(round(self._equity(), 2))
            return {"timestamp": ts, "equity": eq, "base_value": base}

    def bars(self, symbols, timeframe, start, end=None, feed="iex", **_):
        with self.lock:
            if timeframe == "1Day":
                return {s: list(self.m.daily.get(s, [])) for s in symbols}
            out = {}
            now = self._now().astimezone(ET)
            for s in symbols:
                if s in self.m.intraday:
                    out[s] = list(self.m.intraday[s])
                    continue
                out[s] = self._synth_intraday(s, now)
            return out

    def _synth_intraday(self, sym, now):
        price = self._price(sym)
        prev = self.m.prev_close(sym)
        open_dt = now.replace(hour=9, minute=30, second=0, microsecond=0)
        n = max(int((now - open_dt).total_seconds() // 300), 0)
        daily = self.m.daily.get(sym) or []
        avg = sum(b["v"] for b in daily[-20:]) / max(len(daily[-20:]), 1)
        bars = []
        for i in range(n):
            frac = (i + 1) / max(n, 1)
            p = prev + (price - prev) * frac
            t = (open_dt + timedelta(minutes=5 * i)).astimezone(timezone.utc)
            v = avg / 78 * self.m.volume_mult * (1 + 0.2 * math.sin(i))
            bars.append({"t": t.isoformat().replace("+00:00", "Z"), "o": p, "h": p * 1.001, "l": p * 0.999,
                         "c": p, "v": v, "vw": p})
        return bars

    def snapshots(self, symbols, feed="iex"):
        with self.lock:
            out = {}
            for s in symbols:
                if s not in self.m.price:
                    continue
                out[s] = {"price": self._price(s), "bid": None, "ask": None, "day_open": None, "day_high": None,
                          "day_low": None, "day_volume": 0, "prev_close": self.m.prev_close(s), "trade_time": None}
            return out


class FakeBrain:
    """Picks the first open, else the first close, else hold. Costs a tenth of a cent."""

    def __init__(self, provider: str, error: str | None = None):
        self.provider = provider
        self.error = error
        self.calls = 0

    def worst_case_cost(self, *a, **k):
        return 0.001

    def pick(self, bot_name, style_label, rules, state, menu):
        from .brains import BrainError, Pick

        self.calls += 1
        if self.error:
            raise BrainError(self.error)
        for kind in ("open", "close", "add"):
            for m in menu:
                if m.kind == kind:
                    return Pick(m.id, 0.72 if self.provider == "jev" else None,
                                "Matches its rules.", 0.0008, f"fake-{self.provider}", 42)
        return Pick("hold", 0.8 if self.provider == "jev" else None, "Nothing fits the rules right now.",
                    0.0008, f"fake-{self.provider}", 40)

    def check_key(self):
        return True, "ok"
