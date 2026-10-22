"""Tradier as a broker for one bot.

Tradier places the orders and holds the account. Market data and the market
clock still come from Alpaca (any Alpaca paper account's keys work; that
account never trades), so every bot sees the same prices.

Paper mode uses Tradier's sandbox (https://sandbox.tradier.com, sandbox token).
Live mode (MODE=live + LIVE_ACK) uses https://api.tradier.com with a live token.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

import requests

from .alpaca import AlpacaClient, AlpacaError
from .config import ET

LIVE_URL = "https://api.tradier.com/v1"
SANDBOX_URL = "https://sandbox.tradier.com/v1"

log = logging.getLogger("stockbots.tradier")

OPEN_STATES = ("open", "partially_filled", "pending")


def _list(value) -> list:
    """Tradier returns one item as a bare object, none as 'null' or missing."""
    if value in (None, "null", ""):
        return []
    if isinstance(value, list):
        return value
    return [value]


def _items(data, outer: str, inner: str) -> list:
    """Reads e.g. {"positions": {"position": [...]}}, where either level may be 'null'."""
    box = (data or {}).get(outer) if isinstance(data, dict) else None
    if not isinstance(box, dict):
        return []
    return _list(box.get(inner))


def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class TradierClient:
    """Speaks the same methods the engine uses on AlpacaClient."""

    def __init__(
        self,
        token: str,
        data: AlpacaClient,
        account_id: str | None = None,
        live: bool = False,
        state_dir: str | None = None,
        timeout: float = 10.0,
        session: requests.Session | None = None,
        fill_wait_seconds: float = 10.0,
    ):
        self.base = LIVE_URL if live else SANDBOX_URL
        self.data = data
        self.timeout = timeout
        self.session = session or requests.Session()
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        self._account_id = account_id or None
        self.state_dir = state_dir
        self.fill_wait_seconds = fill_wait_seconds
        self._lock = threading.RLock()

    # ---- plumbing -------------------------------------------------------

    def _request(self, method: str, path: str, params=None, form=None, retries: int = 2):
        url = self.base + path
        last: AlpacaError | None = None
        for attempt in range(retries + 1):
            try:
                resp = self.session.request(method, url, headers=self.headers, params=params, data=form,
                                            timeout=self.timeout)
            except requests.RequestException as exc:
                last = AlpacaError(0, f"Tradier network error ({exc.__class__.__name__})")
                if attempt < retries and method == "GET":
                    time.sleep(1 + attempt)
                    continue
                raise last
            text = resp.text or ""
            quota = resp.status_code == 400 and "quota" in text.lower()
            if (quota or resp.status_code == 429 or resp.status_code >= 500) and attempt < retries and method == "GET":
                time.sleep(2 * (attempt + 1))
                continue
            if resp.status_code >= 400:
                raise AlpacaError(resp.status_code, f"Tradier: {_message(resp)}")
            if not text.strip():
                return {}
            try:
                return resp.json()
            except ValueError:
                raise AlpacaError(resp.status_code, "Tradier sent a reply that isn't JSON")
        raise last or AlpacaError(0, "Tradier request failed")

    def profile_accounts(self) -> list[dict]:
        data = self._request("GET", "/user/profile")
        return _items(data, "profile", "account")

    @property
    def account_id(self) -> str:
        if not self._account_id:
            accounts = [a for a in self.profile_accounts() if a.get("status", "active") == "active"]
            if not accounts:
                raise AlpacaError(404, "Tradier: no active account for this token")
            self._account_id = str(accounts[0]["account_number"])
        return self._account_id

    # ---- start-of-day equity and equity history (Tradier has no endpoint for these) ----

    def _state_path(self) -> str | None:
        if not self.state_dir:
            return None
        return os.path.join(self.state_dir, f"tradier-{self.account_id}.json")

    def _load_state(self) -> dict:
        path = self._state_path()
        if not path:
            return getattr(self, "_mem_state", {})
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def _save_state(self, state: dict) -> None:
        path = self._state_path()
        if not path:
            self._mem_state = state
            return
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, path)

    def _note_equity(self, equity: float) -> float:
        """Remembers the first equity seen each trading day; returns it as 'last_equity'."""
        with self._lock:
            state = self._load_state()
            today = datetime.now(ET).date().isoformat()
            days = state.setdefault("days", {})
            if today not in days:
                days[today] = equity
                for old in sorted(days)[:-60]:  # keep about three months
                    days.pop(old, None)
                self._save_state(state)
            return float(days[today])

    # ---- account --------------------------------------------------------

    def account(self) -> dict:
        b = (self._request("GET", f"/accounts/{self.account_id}/balances") or {}).get("balances") or {}
        equity = _num(b.get("total_equity"))
        kind = b.get("account_type")
        sub = b.get(kind) if isinstance(b.get(kind), dict) else {}
        if kind == "cash":
            bp = _num(sub.get("cash_available"), _num(b.get("total_cash")))
        else:
            bp = _num(sub.get("stock_buying_power"), _num(b.get("total_cash")))
        return {
            "id": self.account_id,
            "account_number": self.account_id,
            "status": "ACTIVE",
            "equity": equity,
            "last_equity": self._note_equity(equity),
            "cash": _num(b.get("total_cash")),
            "buying_power": bp,
            "trading_blocked": False,
            "account_blocked": False,
        }

    def clock(self) -> dict:
        return self.data.clock()

    def _quotes(self, symbols: list[str]) -> dict[str, float]:
        if not symbols:
            return {}
        try:
            snaps = self.data.snapshots(symbols)
            return {s: v["price"] for s, v in snaps.items() if v.get("price")}
        except AlpacaError:
            data = self._request("GET", "/markets/quotes", params={"symbols": ",".join(symbols)})
            out = {}
            for q in _items(data, "quotes", "quote"):
                if q.get("symbol") and q.get("last") is not None:
                    out[q["symbol"]] = _num(q["last"])
            return out

    def positions(self) -> list[dict]:
        data = self._request("GET", f"/accounts/{self.account_id}/positions") or {}
        rows = [p for p in _items(data, "positions", "position") if _num(p.get("quantity")) > 0]
        if not rows:
            return []
        prices = self._quotes([p["symbol"] for p in rows])
        held = self._held_by_open_sells()
        out = []
        for p in rows:
            sym = p["symbol"]
            qty = _num(p.get("quantity"))
            avg = _num(p.get("cost_basis")) / qty if qty else 0.0
            price = prices.get(sym, avg)
            out.append({
                "symbol": sym,
                "qty": qty,
                "qty_available": max(qty - held.get(sym, 0.0), 0.0),
                "side": "long",
                "avg_entry_price": avg,
                "current_price": price,
                "market_value": price * qty,
                "unrealized_pl": (price - avg) * qty,
                "unrealized_plpc": ((price / avg - 1) * 100) if avg else 0.0,
            })
        return out

    # ---- orders ---------------------------------------------------------

    def _orders(self) -> list[dict]:
        data = self._request("GET", f"/accounts/{self.account_id}/orders") or {}
        return _items(data, "orders", "order")

    def _held_by_open_sells(self) -> dict[str, float]:
        held: dict[str, float] = {}
        for o in self._orders():
            if o.get("status") in OPEN_STATES and o.get("side") == "sell":
                held[o["symbol"]] = held.get(o["symbol"], 0.0) + _num(o.get("remaining_quantity"), _num(o.get("quantity")))
        return held

    def open_orders(self) -> list[dict]:
        return [o for o in self._orders() if o.get("status") in OPEN_STATES]

    def closed_orders(self, limit: int = 20) -> list[dict]:
        done = [o for o in self._orders() if o.get("status") in ("filled", "partially_filled")]
        done.sort(key=lambda o: o.get("transaction_date") or "", reverse=True)
        return [
            {
                "symbol": o.get("symbol"),
                "side": o.get("side"),
                "type": o.get("type"),
                "status": o.get("status"),
                "qty": _num(o.get("exec_quantity")),
                "price": _num(o.get("avg_fill_price")) or None,
                "time": o.get("transaction_date"),
                "client_order_id": o.get("tag"),
            }
            for o in done[:limit]
        ]

    def _order(self, order_id) -> dict:
        data = self._request("GET", f"/accounts/{self.account_id}/orders/{order_id}") or {}
        return data.get("order") or {}

    def _by_tag(self, tag: str) -> dict | None:
        for o in self._orders():
            if o.get("tag") == tag:
                return o
        return None

    def _place(self, form: dict) -> dict:
        """Place one order. A network error is never blindly retried: we look for it by tag first."""
        tag = form.get("tag")
        try:
            data = self._request("POST", f"/accounts/{self.account_id}/orders", form=form, retries=0)
        except AlpacaError as exc:
            if exc.status == 0 and tag:
                time.sleep(1.5)
                existing = self._by_tag(tag)
                if existing:
                    return existing
            raise
        placed = data.get("order") or {}
        if not placed.get("id"):
            raise AlpacaError(422, f"Tradier didn't accept the order: {str(data)[:200]}")
        # A 200 can still be rejected downstream: check the order's real status.
        order = self._order(placed["id"])
        if order.get("status") in ("rejected", "error"):
            reason = order.get("reason_description") or "rejected"
            raise AlpacaError(422, f"Tradier rejected the order: {reason}")
        return {**order, "id": placed["id"]}

    def submit_market(self, symbol, qty, side, tif, client_order_id, stop_price=None) -> dict:
        # Tradier market orders are day orders. The protective stop is placed as its
        # own order once the buy fills (the engine's stop guard covers any gap).
        order = self._place({
            "class": "equity", "symbol": symbol, "side": side, "quantity": str(int(qty)),
            "type": "market", "duration": "day", "tag": _tag(client_order_id),
        })
        if stop_price is None:
            return order
        deadline = time.monotonic() + self.fill_wait_seconds
        while order.get("status") not in ("filled",) and time.monotonic() < deadline:
            time.sleep(1.0)
            order = {**self._order(order["id"]), "id": order["id"]}
            if order.get("status") in ("rejected", "canceled", "expired", "error"):
                raise AlpacaError(422, f"Tradier order {order.get('status')}")
        filled = int(_num(order.get("exec_quantity")))
        if filled > 0:
            try:
                self.submit_stop(symbol, filled, stop_price, tif, client_order_id + "-sl")
            except AlpacaError as exc:
                log.warning("Tradier stop for %s not placed yet (%s); the stop guard will retry", symbol, exc)
        return order

    def submit_stop(self, symbol, qty, stop_price, tif, client_order_id) -> dict:
        return self._place({
            "class": "equity", "symbol": symbol, "side": "sell", "quantity": str(int(qty)),
            "type": "stop", "stop": f"{stop_price:.2f}", "duration": "gtc" if tif == "gtc" else "day",
            "tag": _tag(client_order_id),
        })

    def cancel_order(self, order_id) -> None:
        try:
            self._request("DELETE", f"/accounts/{self.account_id}/orders/{order_id}", retries=0)
        except AlpacaError as exc:
            if exc.status not in (400, 404, 422):
                raise

    def close_position(self, symbol: str) -> dict | None:
        for o in self.open_orders():
            if o.get("symbol") == symbol and o.get("id"):
                self.cancel_order(o["id"])
        pos = next((p for p in self.positions() if p["symbol"] == symbol), None)
        if not pos:
            return None
        last: AlpacaError | None = None
        for attempt in range(4):
            try:
                return self._place({
                    "class": "equity", "symbol": symbol, "side": "sell", "quantity": str(int(pos["qty"])),
                    "type": "market", "duration": "day", "tag": _tag(f"sb-close-{symbol}-{int(time.time())}-{attempt}"),
                })
            except AlpacaError as exc:
                last = exc
                if attempt < 3 and exc.status != 0:
                    time.sleep(1.0 + attempt)  # shares may still be held by a canceling stop
                    continue
                raise
        if last:
            raise last
        return None

    def close_all(self) -> None:
        errors = []
        for p in self.positions():
            try:
                self.close_position(p["symbol"])
            except AlpacaError as exc:
                errors.append(f"{p['symbol']}: {exc.message}")
        if errors:
            raise AlpacaError(422, "; ".join(errors)[:300])

    def portfolio_history(self, period="1M", timeframe="1D") -> dict:
        state = self._load_state()
        days = state.get("days") or {}
        ts, eq = [], []
        for day in sorted(days)[-23:]:
            d = datetime.fromisoformat(day).replace(hour=16, tzinfo=ET)
            ts.append(int(d.astimezone(timezone.utc).timestamp()))
            eq.append(float(days[day]))
        return {"timestamp": ts, "equity": eq, "base_value": eq[0] if eq else 0.0}

    # ---- market data comes from Alpaca ----------------------------------

    def bars(self, *args, **kwargs):
        return self.data.bars(*args, **kwargs)

    def snapshots(self, *args, **kwargs):
        return self.data.snapshots(*args, **kwargs)


def _tag(value: str) -> str:
    """Tradier tags: letters, numbers and hyphens, 255 characters at most."""
    clean = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in value)
    return clean[:255]


def _message(resp: requests.Response) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict):
            fault = data.get("fault") or {}
            errors = data.get("errors") or {}
            return str(fault.get("faultstring") or errors.get("error") or data)[:200]
    except ValueError:
        pass
    return (resp.text or resp.reason or "")[:200]
