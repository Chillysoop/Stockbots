"""Public (public.com) as a broker for one bot. Real money only: Public has no sandbox.

Public places the orders and holds the account. Market data and the market clock
come from Alpaca, so every bot sees the same prices.

Auth: a personal secret key (Public settings > API) is exchanged for a short-lived
access token, which this client renews on its own before it expires.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone

import requests

from .alpaca import AlpacaClient, AlpacaError
from .config import ET

BASE_URL = "https://api.public.com"
TOKEN_PATH = "/userapiauthservice/personal/access-tokens"

OPEN_STATES = ("NEW", "PARTIALLY_FILLED", "PENDING_REPLACE", "PENDING_CANCEL")
DONE_BAD = ("REJECTED", "CANCELLED", "EXPIRED")

log = logging.getLogger("stockbots.public")


def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def order_uuid(client_order_id: str) -> str:
    """Public wants a UUID per order, and treats a reused one as the same order.

    Deriving it from our client order id makes every retry idempotent.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "stockbots:" + client_order_id))


class PublicClient:
    def __init__(
        self,
        secret: str,
        data: AlpacaClient,
        account_id: str | None = None,
        state_dir: str | None = None,
        timeout: float = 10.0,
        session: requests.Session | None = None,
        token_minutes: int = 60,
        fill_wait_seconds: float = 10.0,
    ):
        self.secret = secret
        self.data = data
        self._account_id = account_id or None
        self.state_dir = state_dir
        self.timeout = timeout
        self.session = session or requests.Session()
        self.token_minutes = token_minutes
        self.fill_wait_seconds = fill_wait_seconds
        self._token: str | None = None
        self._token_expires = 0.0
        self._lock = threading.RLock()

    # ---- plumbing -------------------------------------------------------

    def _access_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._token_expires - 300:
                return self._token
            try:
                resp = self.session.post(
                    BASE_URL + TOKEN_PATH,
                    json={"secret": self.secret, "validityInMinutes": self.token_minutes},
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                raise AlpacaError(0, f"Public network error ({exc.__class__.__name__})")
            if resp.status_code >= 400:
                raise AlpacaError(resp.status_code, f"Public rejected the secret key: {_message(resp)}")
            token = (resp.json() or {}).get("accessToken")
            if not token:
                raise AlpacaError(resp.status_code, "Public sent no access token")
            self._token = token
            self._token_expires = time.time() + self.token_minutes * 60
            return token

    def _request(self, method: str, path: str, body=None, retries: int = 2):
        for attempt in range(retries + 1):
            headers = {"Authorization": f"Bearer {self._access_token()}", "Content-Type": "application/json"}
            try:
                resp = self.session.request(method, BASE_URL + path, headers=headers, json=body, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt < retries:
                    time.sleep(1 + attempt)
                    continue
                raise AlpacaError(0, f"Public network error ({exc.__class__.__name__})")
            if resp.status_code == 401 and attempt < retries:
                with self._lock:
                    self._token = None  # token expired early: get a new one
                continue
            if (resp.status_code == 429 or resp.status_code >= 500) and attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            if resp.status_code >= 400:
                raise AlpacaError(resp.status_code, f"Public: {_message(resp)}")
            if not (resp.text or "").strip():
                return {}
            return resp.json()
        raise AlpacaError(0, "Public request failed")

    def accounts(self) -> list[dict]:
        return (self._request("GET", "/userapigateway/trading/account") or {}).get("accounts") or []

    @property
    def account_id(self) -> str:
        if not self._account_id:
            brokerage = [a for a in self.accounts() if a.get("accountType") == "BROKERAGE"]
            if not brokerage:
                raise AlpacaError(404, "Public: no brokerage account for this key")
            self._account_id = str(brokerage[0]["accountId"])
        return self._account_id

    def _portfolio(self) -> dict:
        return self._request("GET", f"/userapigateway/trading/{self.account_id}/portfolio/v2") or {}

    # ---- start-of-day equity and equity history -------------------------

    def _state_path(self) -> str | None:
        if not self.state_dir:
            return None
        return os.path.join(self.state_dir, f"public-{self.account_id}.json")

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
        with self._lock:
            state = self._load_state()
            today = datetime.now(ET).date().isoformat()
            days = state.setdefault("days", {})
            if today not in days:
                days[today] = equity
                for old in sorted(days)[:-60]:
                    days.pop(old, None)
                self._save_state(state)
            return float(days[today])

    # ---- account --------------------------------------------------------

    def account(self) -> dict:
        p = self._portfolio()
        equity = _num(p.get("totalAccountValue"))
        if not equity:
            equity = sum(_num(e.get("value")) for e in p.get("equity") or [])
        bp = p.get("buyingPower") or {}
        return {
            "id": self.account_id,
            "account_number": self.account_id,
            "status": "ACTIVE",
            "equity": equity,
            "last_equity": self._note_equity(equity),
            "cash": _num(p.get("cash")),
            "buying_power": _num(bp.get("buyingPower"), _num(bp.get("cashOnlyBuyingPower"))),
            "trading_blocked": False,
            "account_blocked": False,
        }

    def clock(self) -> dict:
        return self.data.clock()

    def positions(self) -> list[dict]:
        p = self._portfolio()
        held: dict[str, float] = {}
        for o in p.get("orders") or []:
            if o.get("status") in OPEN_STATES and o.get("side") == "SELL":
                sym = (o.get("instrument") or {}).get("symbol")
                left = _num(o.get("quantity")) - _num(o.get("filledQuantity"))
                held[sym] = held.get(sym, 0.0) + max(left, 0.0)
        out = []
        for pos in p.get("positions") or []:
            inst = pos.get("instrument") or {}
            if inst.get("type") not in (None, "EQUITY"):
                continue
            sym = inst.get("symbol")
            qty = _num(pos.get("quantity"))
            if not sym or qty <= 0:
                continue
            cost = pos.get("costBasis") or {}
            avg = _num(cost.get("unitCost")) or (_num(cost.get("totalCost")) / qty if qty else 0.0)
            price = _num((pos.get("lastPrice") or {}).get("lastPrice")) or (_num(pos.get("currentValue")) / qty)
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

    def open_orders(self) -> list[dict]:
        out = []
        for o in self._portfolio().get("orders") or []:
            if o.get("status") in OPEN_STATES:
                out.append({**o, "id": o.get("orderId"), "symbol": (o.get("instrument") or {}).get("symbol")})
        return out

    def closed_orders(self, limit: int = 20) -> list[dict]:
        done = [o for o in self._portfolio().get("orders") or [] if o.get("status") in ("FILLED", "PARTIALLY_FILLED")]
        done.sort(key=lambda o: o.get("closedAt") or o.get("createdAt") or "", reverse=True)
        return [
            {
                "symbol": (o.get("instrument") or {}).get("symbol"),
                "side": (o.get("side") or "").lower(),
                "type": (o.get("type") or "").lower(),
                "status": (o.get("status") or "").lower(),
                "qty": _num(o.get("filledQuantity")),
                "price": _num(o.get("averagePrice")) or None,
                "time": o.get("closedAt") or o.get("createdAt"),
                "client_order_id": o.get("orderId"),
            }
            for o in done[:limit]
        ]

    def _get_order(self, order_id: str) -> dict:
        try:
            return self._request("GET", f"/userapigateway/trading/{self.account_id}/order/{order_id}") or {}
        except AlpacaError as exc:
            if exc.status == 404:
                return {}  # may not be visible for a moment after placing
            raise

    def _place(self, client_order_id: str, symbol: str, side: str, qty: int, order_type: str, tif: str,
               stop_price: float | None = None) -> dict:
        oid = order_uuid(client_order_id)
        body = {
            "orderId": oid,
            "instrument": {"symbol": symbol, "type": "EQUITY"},
            "orderSide": side,
            "orderType": order_type,
            "expiration": {"timeInForce": tif},
            "quantity": str(int(qty)),
            "equityMarketSession": "CORE",
            "useMargin": False,
        }
        if stop_price is not None:
            body["stopPrice"] = f"{stop_price:.2f}"
        # Same orderId on every retry: Public treats it as one order, so retries are safe.
        self._request("POST", f"/userapigateway/trading/{self.account_id}/order", body=body)
        order = {}
        for _ in range(5):
            order = self._get_order(oid)
            if order:
                break
            time.sleep(0.6)
        if order.get("status") == "REJECTED":
            raise AlpacaError(422, f"Public rejected the order: {order.get('rejectReason') or 'no reason given'}")
        return {**order, "id": oid}

    def submit_market(self, symbol, qty, side, tif, client_order_id, stop_price=None) -> dict:
        order = self._place(client_order_id, symbol, side.upper(), qty, "MARKET", "DAY")
        if stop_price is None:
            return order
        deadline = time.monotonic() + self.fill_wait_seconds
        while order.get("status") != "FILLED" and time.monotonic() < deadline:
            time.sleep(1.0)
            order = {**self._get_order(order["id"]), "id": order["id"]}
            if order.get("status") in DONE_BAD:
                raise AlpacaError(422, f"Public order {str(order.get('status')).lower()}")
        filled = int(_num(order.get("filledQuantity")))
        if filled > 0:
            try:
                self.submit_stop(symbol, filled, stop_price, tif, client_order_id + "-sl")
            except AlpacaError as exc:
                log.warning("Public stop for %s not placed yet (%s); the stop guard will retry", symbol, exc)
        return order

    def submit_stop(self, symbol, qty, stop_price, tif, client_order_id) -> dict:
        return self._place(client_order_id, symbol, "SELL", qty, "STOP", "GTC" if tif == "gtc" else "DAY",
                           stop_price=stop_price)

    def cancel_order(self, order_id) -> None:
        try:
            self._request("DELETE", f"/userapigateway/trading/{self.account_id}/order/{order_id}", retries=0)
        except AlpacaError as exc:
            if exc.status not in (400, 404, 422):
                raise

    def close_position(self, symbol: str) -> dict | None:
        canceled = False
        for o in self.open_orders():
            if o.get("symbol") == symbol and o.get("id"):
                self.cancel_order(o["id"])
                canceled = True
        pos = next((p for p in self.positions() if p["symbol"] == symbol), None)
        if not pos:
            return None
        if canceled:
            time.sleep(1.0)  # cancels are asynchronous at Public
        tag = f"sb-close-{symbol}-{int(time.time())}"
        last: AlpacaError | None = None
        for attempt in range(4):
            try:
                return self._place(f"{tag}-{attempt}", symbol, "SELL", int(pos["qty"]), "MARKET", "DAY")
            except AlpacaError as exc:
                last = exc
                if attempt < 3 and exc.status != 0:
                    time.sleep(1.0 + attempt)
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


def _message(resp: requests.Response) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict):
            return str(data.get("message") or data.get("error") or data)[:200]
    except ValueError:
        pass
    return (resp.text or resp.reason or "")[:200]
