"""Tradier, Public and choosing a broker per bot (the network is faked)."""
import json
import os
import unittest
from unittest import mock
from urllib.parse import parse_qs

from stockbots import brokers
from stockbots.alpaca import AlpacaClient, AlpacaError
from stockbots.publicapi import PublicClient, order_uuid
from stockbots.tradier import LIVE_URL, SANDBOX_URL, TradierClient, _tag

from .helpers import make_config


class Resp:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self._data = data
        self.text = "" if data is None else json.dumps(data)
        self.content = self.text.encode()
        self.reason = ""

    def json(self):
        return self._data


class FakeData:
    """Stands in for the Alpaca market-data client."""

    def clock(self):
        return {"is_open": True, "next_close": None, "next_open": None, "timestamp": None}

    def snapshots(self, symbols, feed="iex"):
        return {s: {"price": 110.0} for s in symbols}

    def bars(self, *a, **k):
        return {}


class Routes:
    """A tiny fake HTTP server: (METHOD, path suffix) -> response or callable."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def request(self, method, url, headers=None, params=None, data=None, json=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params, "data": data, "json": json,
                           "headers": headers})
        for (m, suffix), resp in self.routes.items():
            if m == method and url.endswith(suffix):
                return resp(self.calls[-1]) if callable(resp) else resp
        return Resp(404, {"message": f"no route {method} {url}"})

    def post(self, url, headers=None, json=None, data=None, timeout=None):
        return self.request("POST", url, headers=headers, json=json, data=data, timeout=timeout)

    def placed(self):
        return [c for c in self.calls if c["method"] == "POST" and c["url"].endswith("/orders")]


class TradierTests(unittest.TestCase):
    def client(self, routes, live=False):
        s = Routes(routes)
        return TradierClient("tok", FakeData(), account_id="VA1", live=live, session=s, fill_wait_seconds=0), s

    def test_urls_and_auth(self):
        c, s = self.client({("GET", "/balances"): Resp(200, {"balances": {"total_equity": 5000, "total_cash": 5000,
                                                                            "account_type": "cash",
                                                                            "cash": {"cash_available": 4000}}})})
        a = c.account()
        self.assertTrue(s.calls[0]["url"].startswith(SANDBOX_URL))
        self.assertEqual(s.calls[0]["headers"]["Authorization"], "Bearer tok")
        self.assertEqual((a["equity"], a["buying_power"], a["last_equity"]), (5000, 4000, 5000))
        live, _ = self.client({}, live=True)
        self.assertEqual(live.base, LIVE_URL)

    def test_single_and_empty_lists(self):
        c, _ = self.client({
            ("GET", "/positions"): Resp(200, {"positions": {"position": {"symbol": "AAPL", "quantity": 10, "cost_basis": 1000}}}),
            ("GET", "/orders"): Resp(200, {"orders": {"order": {"id": 9, "symbol": "AAPL", "side": "sell", "status": "open",
                                                                 "quantity": 10, "remaining_quantity": 10}}}),
        })
        pos = c.positions()
        self.assertEqual(len(pos), 1)
        p = pos[0]
        self.assertEqual((p["avg_entry_price"], p["current_price"], p["qty_available"]), (100.0, 110.0, 0.0))
        self.assertAlmostEqual(p["unrealized_plpc"], 10.0)
        empty, _ = self.client({("GET", "/positions"): Resp(200, {"positions": "null"})})
        self.assertEqual(empty.positions(), [])

    def test_buy_then_stop(self):
        def order(call):
            return Resp(200, {"order": {"id": 1, "status": "filled", "exec_quantity": 10}})

        c, s = self.client({
            ("POST", "/orders"): Resp(200, {"order": {"id": 1, "status": "ok"}}),
            ("GET", "/orders/1"): order,
        })
        c.submit_market("AAPL", 10, "buy", "gtc", "sb-jev-open-abc", stop_price=95.5)
        buy, stop = [parse_qs("&".join(f"{k}={v}" for k, v in p["data"].items())) for p in s.placed()]
        self.assertEqual((buy["type"], buy["side"], buy["quantity"]), (["market"], ["buy"], ["10"]))
        self.assertEqual((stop["type"], stop["stop"], stop["duration"], stop["side"]),
                         (["stop"], ["95.50"], ["gtc"], ["sell"]))

    def test_downstream_reject_is_an_error(self):
        c, _ = self.client({
            ("POST", "/orders"): Resp(200, {"order": {"id": 7, "status": "ok"}}),
            ("GET", "/orders/7"): Resp(200, {"order": {"id": 7, "status": "rejected",
                                                        "reason_description": "insufficient funds"}}),
        })
        with self.assertRaises(AlpacaError) as cm:
            c.submit_market("AAPL", 1, "buy", "day", "x")
        self.assertIn("insufficient funds", cm.exception.message)

    def test_close_cancels_stop_then_sells(self):
        c, s = self.client({
            ("GET", "/orders"): Resp(200, {"orders": {"order": [
                {"id": 5, "symbol": "AAPL", "side": "sell", "status": "open", "quantity": 10, "remaining_quantity": 10}]}}),
            ("DELETE", "/orders/5"): Resp(200, {"order": {"id": 5, "status": "ok"}}),
            ("GET", "/positions"): Resp(200, {"positions": {"position": [{"symbol": "AAPL", "quantity": 10, "cost_basis": 1000}]}}),
            ("POST", "/orders"): Resp(200, {"order": {"id": 6, "status": "ok"}}),
            ("GET", "/orders/6"): Resp(200, {"order": {"id": 6, "status": "filled"}}),
        })
        c.close_position("AAPL")
        methods = [(x["method"], x["url"].rsplit("/v1", 1)[1]) for x in s.calls]
        self.assertIn(("DELETE", "/accounts/VA1/orders/5"), methods)
        sell = s.placed()[-1]["data"]
        self.assertEqual((sell["side"], sell["quantity"], sell["type"]), ("sell", "10", "market"))

    def test_tag_rules(self):
        self.assertEqual(_tag("sb_jev:open/1"), "sb-jev-open-1")


class PublicTests(unittest.TestCase):
    def client(self, routes):
        routes = {("POST", "/access-tokens"): Resp(200, {"accessToken": "acc"}), **routes}
        s = Routes(routes)
        return PublicClient("secret", FakeData(), account_id="A1", session=s, fill_wait_seconds=0), s

    def test_token_exchange_once(self):
        c, s = self.client({("GET", "/portfolio/v2"): Resp(200, {"totalAccountValue": "5000", "cash": "1000",
                                                                   "buyingPower": {"buyingPower": "1000"}})})
        c.account()
        c.account()
        tokens = [x for x in s.calls if x["url"].endswith("/access-tokens")]
        self.assertEqual(len(tokens), 1)
        self.assertEqual(tokens[0]["json"]["secret"], "secret")
        self.assertEqual(s.calls[-1]["headers"]["Authorization"], "Bearer acc")

    def test_positions_and_held_shares(self):
        c, _ = self.client({("GET", "/portfolio/v2"): Resp(200, {
            "positions": [{"instrument": {"symbol": "NVDA", "type": "EQUITY"}, "quantity": "4",
                           "costBasis": {"unitCost": "100"}, "lastPrice": {"lastPrice": "120"}}],
            "orders": [{"orderId": "o1", "instrument": {"symbol": "NVDA"}, "side": "SELL", "status": "NEW",
                        "quantity": "4", "filledQuantity": "0"}],
        })})
        p = c.positions()[0]
        self.assertEqual((p["qty"], p["avg_entry_price"], p["current_price"], p["qty_available"]), (4, 100, 120, 0))
        self.assertEqual(c.open_orders()[0]["id"], "o1")

    def test_order_ids_make_retries_safe(self):
        self.assertEqual(order_uuid("abc"), order_uuid("abc"))
        self.assertNotEqual(order_uuid("abc"), order_uuid("abd"))
        oid = order_uuid("sb-x")
        c, s = self.client({
            ("POST", "/order"): Resp(200, {"orderId": oid}),
            ("GET", f"/order/{oid}"): Resp(200, {"orderId": oid, "status": "FILLED", "filledQuantity": "3"}),
            ("GET", f"/order/{order_uuid('sb-x-sl')}"): Resp(200, {"status": "NEW"}),
        })
        c.submit_market("NVDA", 3, "buy", "gtc", "sb-x", stop_price=99.0)
        posts = [x["json"] for x in s.calls if x["method"] == "POST" and x["url"].endswith("/order")]
        self.assertEqual(posts[0]["orderId"], oid)
        self.assertEqual((posts[0]["orderType"], posts[0]["orderSide"], posts[0]["quantity"]), ("MARKET", "BUY", "3"))
        self.assertEqual((posts[1]["orderType"], posts[1]["stopPrice"], posts[1]["expiration"]["timeInForce"]),
                         ("STOP", "99.00", "GTC"))

    def test_rejected_order(self):
        oid = order_uuid("sb-r")
        c, _ = self.client({
            ("POST", "/order"): Resp(200, {"orderId": oid}),
            ("GET", f"/order/{oid}"): Resp(200, {"status": "REJECTED", "rejectReason": "not enough cash"}),
        })
        with self.assertRaises(AlpacaError) as cm:
            c.submit_market("NVDA", 1, "buy", "day", "sb-r")
        self.assertIn("not enough cash", cm.exception.message)


PAPER = {"alpaca_key": "PKX", "alpaca_secret": "s"}


class ChooseTests(unittest.TestCase):
    def cfg(self, live=False):
        c = make_config()
        c.live = live
        return c

    def test_paper_defaults_to_alpaca_paper(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            ch = brokers.choose("jev", self.cfg(), PAPER)
        self.assertIsInstance(ch.client, AlpacaClient)
        self.assertEqual((ch.broker, ch.label, ch.problem), ("alpaca", "Alpaca paper", None))
        self.assertIn("paper-api", ch.client.base)
        self.assertEqual(brokers.choose("jev", self.cfg(), PAPER, enabled=False).broker, "off")

    def test_paper_tradier_sandbox(self):
        with mock.patch.dict(os.environ, {"PAPER_BROKER_JEV": "tradier"}):
            missing = brokers.choose("jev", self.cfg(), PAPER)
        self.assertIsNone(missing.client)
        self.assertIn("TRADIER_SANDBOX_TOKEN", missing.problem)
        with mock.patch.dict(os.environ, {"PAPER_BROKER_JEV": "tradier", "TRADIER_SANDBOX_TOKEN_JEV": "t"}):
            ch = brokers.choose("jev", self.cfg(), PAPER)
        self.assertEqual((ch.label, ch.client.base), ("Tradier sandbox", SANDBOX_URL))

    def test_live_needs_its_own_keys(self):
        with mock.patch.dict(os.environ, {"LIVE_BROKER_CLAUDE": "alpaca"}):
            ch = brokers.choose("claude", self.cfg(live=True), PAPER)
        self.assertIsNone(ch.client, "paper keys are never used for live trading")
        with mock.patch.dict(os.environ, {"ALPACA_LIVE_KEY_CLAUDE": "AK", "ALPACA_LIVE_SECRET_CLAUDE": "s"}):
            ch = brokers.choose("claude", self.cfg(live=True), PAPER)
        self.assertEqual(ch.client.base, "https://api.alpaca.markets")
        with mock.patch.dict(os.environ, {"LIVE_BROKER_JEV": "tradier", "TRADIER_TOKEN_JEV": "t"}):
            ch = brokers.choose("jev", self.cfg(live=True), PAPER)
        self.assertEqual((ch.label, ch.client.base), ("Tradier live", LIVE_URL))
        with mock.patch.dict(os.environ, {"LIVE_BROKER_OPENAI": "public", "PUBLIC_SECRET_OPENAI": "p"}):
            ch = brokers.choose("openai", self.cfg(live=True), PAPER)
        self.assertEqual(ch.label, "Public live")
        with mock.patch.dict(os.environ, {"LIVE_BROKER_OPENAI": "robinhood"}):
            ch = brokers.choose("openai", self.cfg(live=True), PAPER)
        self.assertIn("must be one of", ch.problem)
        with mock.patch.dict(os.environ, {"LIVE_BROKER_OPENAI": "off"}):
            self.assertEqual(brokers.choose("openai", self.cfg(live=True), PAPER).broker, "off")


if __name__ == "__main__":
    unittest.main()
