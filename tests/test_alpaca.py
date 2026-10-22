"""The Alpaca client builds the right requests (the network is faked)."""
import json
import unittest
from datetime import datetime, timezone

from stockbots.alpaca import PAPER_URL, LIVE_URL, AlpacaClient, AlpacaError


class Resp:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self._data = data
        self.content = b"" if data is None else json.dumps(data).encode()
        self.text = self.content.decode()
        self.reason = ""

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def request(self, method, url, headers=None, params=None, json=None, timeout=None):
        self.calls.append((method, url, params, json))
        return self.handler(method, url, params, json)

    def post(self, url, headers=None, json=None, timeout=None):
        return self.request("POST", url, headers=headers, json=json, timeout=timeout)


class AlpacaClientTests(unittest.TestCase):
    def test_paper_by_default(self):
        self.assertEqual(AlpacaClient("k", "s").base, PAPER_URL)
        self.assertEqual(AlpacaClient("k", "s", live=True).base, LIVE_URL)
        self.assertEqual(AlpacaClient("k", "s").headers["APCA-API-KEY-ID"], "k")

    def test_entry_with_server_side_stop(self):
        s = FakeSession(lambda m, u, p, j: Resp(200, {"id": "o1", "status": "accepted"}))
        AlpacaClient("k", "s", session=s).submit_market("AAPL", 10, "buy", "gtc", "cid-1", stop_price=95.123)
        method, url, _, body = s.calls[0]
        self.assertEqual((method, url), ("POST", PAPER_URL + "/v2/orders"))
        self.assertEqual(body["order_class"], "oto")
        self.assertEqual(body["stop_loss"], {"stop_price": "95.12"})
        self.assertEqual((body["qty"], body["type"], body["time_in_force"]), ("10", "market", "gtc"))

    def test_duplicate_client_id_returns_existing_order(self):
        def handler(m, u, p, j):
            if m == "POST":
                return Resp(422, {"message": "client_order_id must be unique"})
            return Resp(200, {"id": "o1", "status": "filled"})

        s = FakeSession(handler)
        order = AlpacaClient("k", "s", session=s).submit_market("AAPL", 1, "buy", "day", "cid-1")
        self.assertEqual(order["id"], "o1")

    def test_close_cancels_that_symbols_orders_first(self):
        def handler(m, u, p, j):
            if m == "GET" and u.endswith("/v2/orders"):
                return Resp(200, [{"id": "stop1", "symbol": "AAPL"}, {"id": "other", "symbol": "MSFT"}])
            if m == "DELETE" and "/v2/orders/" in u:
                return Resp(204)
            if m == "DELETE" and u.endswith("/v2/positions/AAPL"):
                return Resp(200, {"id": "close1"})
            return Resp(404, {"message": "nope"})

        s = FakeSession(handler)
        AlpacaClient("k", "s", session=s).close_position("AAPL")
        deletes = [u for m, u, _, _ in s.calls if m == "DELETE"]
        self.assertEqual(deletes, [PAPER_URL + "/v2/orders/stop1", PAPER_URL + "/v2/positions/AAPL"])

    def test_bars_follow_pages(self):
        pages = [
            Resp(200, {"bars": {"SPY": [{"t": "a", "c": 1}]}, "next_page_token": "p2"}),
            Resp(200, {"bars": {"SPY": [{"t": "b", "c": 2}], "QQQ": [{"t": "b", "c": 3}]}, "next_page_token": None}),
        ]
        s = FakeSession(lambda m, u, p, j: pages.pop(0))
        out = AlpacaClient("k", "s", session=s).bars(["SPY", "QQQ"], "1Day", datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertEqual([b["c"] for b in out["SPY"]], [1, 2])
        self.assertEqual(len(out["QQQ"]), 1)
        self.assertEqual(s.calls[1][2]["page_token"], "p2")
        self.assertEqual(s.calls[0][2]["timeframe"], "1Day")

    def test_errors_are_explained(self):
        s = FakeSession(lambda m, u, p, j: Resp(403, {"message": "forbidden"}))
        with self.assertRaises(AlpacaError) as cm:
            AlpacaClient("k", "s", session=s).account()
        self.assertEqual(cm.exception.status, 403)

    def test_snapshot_shapes(self):
        data = {"AAPL": {"latestTrade": {"p": 101.5}, "dailyBar": {"c": 101}, "prevDailyBar": {"c": 100}}}
        s = FakeSession(lambda m, u, p, j: Resp(200, data))
        snap = AlpacaClient("k", "s", session=s).snapshots(["AAPL"])
        self.assertEqual(snap["AAPL"]["price"], 101.5)
        self.assertEqual(snap["AAPL"]["prev_close"], 100)


if __name__ == "__main__":
    unittest.main()
