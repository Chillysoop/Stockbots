"""A small Alpaca REST client: just the calls the bots need.

Trading:   https://paper-api.alpaca.markets (paper) or https://api.alpaca.markets (live)
Market data: https://data.alpaca.markets
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import requests

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"

log = logging.getLogger("stockbots.alpaca")


class AlpacaError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"Alpaca {status}: {message}")
        self.status = status
        self.message = message


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AlpacaClient:
    def __init__(
        self,
        key: str,
        secret: str,
        live: bool = False,
        timeout: float = 10.0,
        session: requests.Session | None = None,
    ):
        self.base = LIVE_URL if live else PAPER_URL
        self.data = DATA_URL
        self.timeout = timeout
        self.session = session or requests.Session()
        self.headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}

    # ---- plumbing -------------------------------------------------------

    def _request(self, method: str, url: str, params=None, body=None, retries: int = 2):
        last_error: AlpacaError | None = None
        for attempt in range(retries + 1):
            try:
                resp = self.session.request(
                    method, url, headers=self.headers, params=params, json=body, timeout=self.timeout
                )
            except requests.RequestException as exc:
                last_error = AlpacaError(0, f"network error ({exc.__class__.__name__})")
                if attempt < retries:
                    time.sleep(1 + attempt)
                    continue
                raise last_error
            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = AlpacaError(resp.status_code, _message(resp))
                if attempt < retries:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise last_error
            if resp.status_code >= 400:
                raise AlpacaError(resp.status_code, _message(resp))
            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()
        raise last_error or AlpacaError(0, "request failed")

    def _get(self, path: str, params=None, data_api: bool = False):
        base = self.data if data_api else self.base
        return self._request("GET", base + path, params=params)

    # ---- account --------------------------------------------------------

    def account(self) -> dict:
        a = self._get("/v2/account") or {}
        return {
            "id": a.get("id"),
            "account_number": a.get("account_number"),
            "status": a.get("status"),
            "equity": _num(a.get("equity")),
            "last_equity": _num(a.get("last_equity")),
            "cash": _num(a.get("cash")),
            "buying_power": _num(a.get("buying_power")),
            "trading_blocked": bool(a.get("trading_blocked")),
            "account_blocked": bool(a.get("account_blocked")),
        }

    def clock(self) -> dict:
        c = self._get("/v2/clock") or {}
        return {
            "is_open": bool(c.get("is_open")),
            "timestamp": c.get("timestamp"),
            "next_open": c.get("next_open"),
            "next_close": c.get("next_close"),
        }

    def positions(self) -> list[dict]:
        out = []
        for p in self._get("/v2/positions") or []:
            out.append(
                {
                    "symbol": p.get("symbol"),
                    "qty": _num(p.get("qty")),
                    "qty_available": _num(p.get("qty_available"), _num(p.get("qty"))),
                    "side": p.get("side", "long"),
                    "avg_entry_price": _num(p.get("avg_entry_price")),
                    "current_price": _num(p.get("current_price")),
                    "market_value": _num(p.get("market_value")),
                    "unrealized_pl": _num(p.get("unrealized_pl")),
                    "unrealized_plpc": _num(p.get("unrealized_plpc")) * 100.0,
                }
            )
        return out

    # ---- orders ---------------------------------------------------------

    def open_orders(self) -> list[dict]:
        # Not nested: stop legs of filled entry orders then show up as their own open orders.
        orders = self._get("/v2/orders", params={"status": "open", "limit": 500}) or []
        flat: list[dict] = []
        for o in orders:
            flat.append(o)
            for leg in o.get("legs") or []:
                flat.append(leg)
        return flat

    def closed_orders(self, limit: int = 20) -> list[dict]:
        orders = self._get(
            "/v2/orders", params={"status": "closed", "limit": limit, "direction": "desc"}
        ) or []
        return [
            {
                "symbol": o.get("symbol"),
                "side": o.get("side"),
                "type": o.get("type"),
                "status": o.get("status"),
                "qty": _num(o.get("filled_qty") or o.get("qty")),
                "price": _num(o.get("filled_avg_price")) or None,
                "time": o.get("filled_at") or o.get("canceled_at") or o.get("submitted_at"),
                "client_order_id": o.get("client_order_id"),
            }
            for o in orders
            if o.get("status") in ("filled", "partially_filled")
        ]

    def submit_market(
        self,
        symbol: str,
        qty: int,
        side: str,
        tif: str,
        client_order_id: str,
        stop_price: float | None = None,
    ) -> dict:
        """Market order. With stop_price, Alpaca holds a protective stop for us (OTO)."""
        body: dict[str, Any] = {
            "symbol": symbol,
            "qty": str(int(qty)),
            "side": side,
            "type": "market",
            "time_in_force": tif,
            "client_order_id": client_order_id,
        }
        if stop_price is not None:
            body["order_class"] = "oto"
            body["stop_loss"] = {"stop_price": f"{stop_price:.2f}"}
        return self._post_order(body)

    def submit_stop(self, symbol: str, qty: int, stop_price: float, tif: str, client_order_id: str) -> dict:
        body = {
            "symbol": symbol,
            "qty": str(int(qty)),
            "side": "sell",
            "type": "stop",
            "stop_price": f"{stop_price:.2f}",
            "time_in_force": tif,
            "client_order_id": client_order_id,
        }
        return self._post_order(body)

    def _post_order(self, body: dict) -> dict:
        # Network errors are retried with the same client_order_id; Alpaca rejects
        # a duplicate id, so a retry can never place the same order twice.
        for attempt in range(3):
            try:
                resp = self.session.post(
                    self.base + "/v2/orders", headers=self.headers, json=body, timeout=self.timeout
                )
            except requests.RequestException:
                if attempt < 2:
                    time.sleep(1 + attempt)
                    continue
                existing = self.order_by_client_id(body["client_order_id"])
                if existing:
                    return existing
                raise AlpacaError(0, "network error placing order")
            if resp.status_code < 400:
                return resp.json()
            msg = _message(resp)
            if resp.status_code == 422 and "client_order_id" in msg.lower():
                existing = self.order_by_client_id(body["client_order_id"])
                if existing:
                    return existing
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
            raise AlpacaError(resp.status_code, msg)
        raise AlpacaError(0, "order failed")

    def order_by_client_id(self, client_order_id: str) -> dict | None:
        try:
            return self._get("/v2/orders:by_client_order_id", params={"client_order_id": client_order_id})
        except AlpacaError:
            return None

    def cancel_order(self, order_id: str) -> None:
        try:
            self._request("DELETE", f"{self.base}/v2/orders/{order_id}")
        except AlpacaError as exc:
            if exc.status not in (404, 422):  # already gone / already filled
                raise

    def close_position(self, symbol: str) -> dict | None:
        """Cancel this symbol's open orders (e.g. its stop), then sell at market."""
        for o in self.open_orders():
            if o.get("symbol") == symbol and o.get("id"):
                self.cancel_order(o["id"])
        last: AlpacaError | None = None
        for attempt in range(4):
            try:
                return self._request("DELETE", f"{self.base}/v2/positions/{symbol}")
            except AlpacaError as exc:
                last = exc
                if exc.status == 404:
                    return None  # nothing to close
                # Shares can be briefly held by an order that is still canceling.
                if exc.status in (403, 422) and attempt < 3:
                    time.sleep(1.0 + attempt)
                    continue
                raise
        if last:
            raise last
        return None

    def close_all(self) -> None:
        self._request("DELETE", f"{self.base}/v2/positions", params={"cancel_orders": "true"})

    def portfolio_history(self, period: str = "1M", timeframe: str = "1D") -> dict:
        h = self._get(
            "/v2/account/portfolio/history", params={"period": period, "timeframe": timeframe}
        ) or {}
        return {
            "timestamp": h.get("timestamp") or [],
            "equity": [(_num(x) if x is not None else None) for x in (h.get("equity") or [])],
            "base_value": _num(h.get("base_value")),
        }

    # ---- market data ----------------------------------------------------

    def bars(
        self,
        symbols: list[str],
        timeframe: str,
        start: datetime,
        end: datetime | None = None,
        feed: str = "iex",
    ) -> dict[str, list[dict]]:
        params: dict[str, Any] = {
            "symbols": ",".join(symbols),
            "timeframe": timeframe,
            "start": iso(start),
            "limit": 10000,
            "adjustment": "split",
            "feed": feed,
            "sort": "asc",
        }
        if end is not None:
            params["end"] = iso(end)
        out: dict[str, list[dict]] = {s: [] for s in symbols}
        for _ in range(20):  # pages
            data = self._get("/v2/stocks/bars", params=params, data_api=True) or {}
            for sym, rows in (data.get("bars") or {}).items():
                out.setdefault(sym, []).extend(rows or [])
            token = data.get("next_page_token")
            if not token:
                break
            params["page_token"] = token
        return out

    def snapshots(self, symbols: list[str], feed: str = "iex") -> dict[str, dict]:
        data = self._get(
            "/v2/stocks/snapshots", params={"symbols": ",".join(symbols), "feed": feed}, data_api=True
        ) or {}
        if "snapshots" in data and isinstance(data["snapshots"], dict):
            data = data["snapshots"]
        out = {}
        for sym, snap in data.items():
            if not isinstance(snap, dict):
                continue
            trade = snap.get("latestTrade") or {}
            quote = snap.get("latestQuote") or {}
            daily = snap.get("dailyBar") or {}
            prev = snap.get("prevDailyBar") or {}
            price = _num(trade.get("p")) or _num(daily.get("c"))
            out[sym] = {
                "price": price,
                "bid": _num(quote.get("bp")) or None,
                "ask": _num(quote.get("ap")) or None,
                "day_open": _num(daily.get("o")) or None,
                "day_high": _num(daily.get("h")) or None,
                "day_low": _num(daily.get("l")) or None,
                "day_volume": _num(daily.get("v")),
                "prev_close": _num(prev.get("c")) or None,
                "trade_time": trade.get("t"),
            }
        return out


def _message(resp: requests.Response) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict):
            return str(data.get("message") or data.get("error") or data)[:300]
    except ValueError:
        pass
    return (resp.text or resp.reason or "")[:300]
