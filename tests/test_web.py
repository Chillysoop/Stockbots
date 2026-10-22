"""Setup, login and the dashboard API, through a real local web server."""
import socket
import threading
import time
import unittest
from unittest import mock

import requests
import uvicorn

from stockbots.alpaca import AlpacaError
from stockbots.app import App
from stockbots.sim import FakeBrain, SimAlpaca
from stockbots.web import create_web

from .helpers import Clock, et, make_config, uptrend_market

H = {"X-Stockbots": "1"}


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = make_config()
        cls.cfg.tick_seconds = 3600  # keep the bot loops quiet during web tests
        clock = Clock(et(2026, 10, 24, 11, 0))
        market = uptrend_market(clock, ["SPY", "AAPL", "NVDA"])
        cls.accounts = {}

        def client_factory(key, secret):
            return cls.accounts.setdefault(key, SimAlpaca(market, account_number=key))

        cls.app = App(cls.cfg, client_factory=client_factory,
                      brain_factory=lambda p, c, k: FakeBrain(p) if k else None, now_fn=clock)

        def alpaca_check(key, secret):
            if key == "bad":
                raise AlpacaError(401, "unauthorized")
            return {"account_number": key.split("-")[0]}

        from .test_backtest import FakeHistory

        web = create_web(cls.app, alpaca_check=alpaca_check, data_factory=lambda k, s: FakeHistory())
        cls.port = free_port()
        cls.server = uvicorn.Server(uvicorn.Config(web, host="127.0.0.1", port=cls.port, log_level="error"))
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        for _ in range(50):
            if cls.server.started:
                break
            time.sleep(0.1)
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(timeout=5)
        cls.app.engine.stop()

    def setup_body(self, **over):
        body = {
            "agree": [True, True, True],
            "password": "correct horse",
            "jev_key": "jk",
            "anthropic_key": "ak",
            "openai_key": "ok",
            "bots": {
                "claude": {"name": "Claude Trend", "alpaca_key": "A1-key", "alpaca_secret": "s"},
                "jev": {"name": "Jev Dips", "alpaca_key": "A2-key", "alpaca_secret": "s"},
                "openai": {"name": "Codex Momentum", "alpaca_key": "A3-key", "alpaca_secret": "s"},
            },
        }
        body.update(over)
        return body

    def test_full_flow(self):
        s = requests.Session()
        # Before setup everything points at /setup and the API stays shut.
        r = s.get(self.base + "/", allow_redirects=False)
        self.assertEqual((r.status_code, r.headers["location"]), (302, "/setup"))
        self.assertEqual(s.get(self.base + "/api/state").status_code, 401)
        status = s.get(self.base + "/api/setup").json()
        self.assertTrue(status["open"])
        self.assertEqual([b["id"] for b in status["bots"]], ["claude", "jev", "openai"])

        # No CSRF header -> refused
        self.assertEqual(s.post(self.base + "/api/setup", json=self.setup_body()).status_code, 403)

        # Missing and bad fields are explained
        r = s.post(self.base + "/api/setup", headers=H, json=self.setup_body(password="short", agree=[True]))
        self.assertEqual(r.status_code, 400)
        self.assertIn("password", r.json()["errors"])
        self.assertIn("agree", r.json()["errors"])

        bad = self.setup_body()
        bad["bots"]["jev"]["alpaca_key"] = "bad"
        with mock.patch("stockbots.brains.Brain.check_key", create=True, return_value=(True, "ok")), \
             mock.patch("stockbots.brains.JevBrain.check_key", return_value=(True, "ok")), \
             mock.patch("stockbots.brains.ClaudeBrain.check_key", return_value=(True, "ok")), \
             mock.patch("stockbots.brains.OpenAIBrain.check_key", return_value=(False, "OpenAI rejected this key.")):
            r = s.post(self.base + "/api/setup", headers=H, json=bad)
        errors = r.json()["errors"]
        self.assertIn("jev.alpaca", errors)
        self.assertEqual(errors["openai_key"], "OpenAI rejected this key.")

        same = self.setup_body()
        same["bots"]["openai"]["alpaca_key"] = "A1-other"
        with mock.patch("stockbots.brains.JevBrain.check_key", return_value=(True, "ok")), \
             mock.patch("stockbots.brains.ClaudeBrain.check_key", return_value=(True, "ok")), \
             mock.patch("stockbots.brains.OpenAIBrain.check_key", return_value=(True, "ok")):
            r = s.post(self.base + "/api/setup", headers=H, json=same)
            self.assertIn("accounts", r.json()["errors"], "two bots on one Alpaca account is refused")
            r = s.post(self.base + "/api/setup", headers=H, json=self.setup_body())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(self.app.settings.setup_done)

        # The setup response logged us in
        state = s.get(self.base + "/api/state").json()
        self.assertEqual(len(state["bots"]), 3)
        self.assertEqual(state["mode"], "paper")
        self.assertNotIn("A1-key", str(state), "keys never leave the server")

        # Setup is now closed for everyone
        self.assertEqual(requests.post(self.base + "/api/setup", headers=H, json=self.setup_body()).status_code, 403)

        # Owner actions
        r = s.post(self.base + "/api/bots/jev/pause", headers=H, json={})
        self.assertTrue(r.json()["ok"])
        self.assertTrue(self.app.db.bot_state("jev")["paused"])
        r = s.post(self.base + "/api/bots/jev/rules", headers=H, json={"rules": "Only buy SPY dips."})
        self.assertEqual(r.json()["rules"], "Only buy SPY dips.")
        r = s.post(self.base + "/api/bots/jev/rules", headers=H, json={"rules": ""})
        self.assertIn("dip buyer", r.json()["rules"].lower())
        self.assertEqual(s.post(self.base + "/api/bots/nope/pause", headers=H, json={}).status_code, 404)

        # A stranger can't act
        self.assertEqual(requests.post(self.base + "/api/bots/jev/resume", headers=H, json={}).status_code, 401)

        # Log out, then log in
        s.post(self.base + "/api/logout", headers=H, json={})
        s.cookies.clear()
        self.assertEqual(s.get(self.base + "/api/state").status_code, 401)
        r = s.post(self.base + "/api/login", headers=H, json={"password": "wrong password"})
        self.assertEqual(r.status_code, 401)
        r = s.post(self.base + "/api/login", headers=H, json={"password": "correct horse"})
        self.assertTrue(r.json()["ok"])
        self.assertEqual(s.get(self.base + "/api/state").status_code, 200)
        page = s.get(self.base + "/")
        self.assertIn("Stockbots", page.text)
        self.assertEqual(page.headers.get("x-frame-options"), "DENY")

        # Backtests: start one from the dashboard API and open its report
        self.assertEqual(requests.get(self.base + "/api/backtests").status_code, 401)
        r = s.post(self.base + "/api/backtests", headers=H, json={"bot": "jev", "months": 6})
        self.assertTrue(r.json()["ok"], r.text)
        self.assertEqual(s.post(self.base + "/api/backtests", headers=H, json={"bot": "jev", "months": 99}).status_code, 400)
        for _ in range(300):
            listing = s.get(self.base + "/api/backtests").json()
            if not listing["running"] and listing["reports"]:
                break
            time.sleep(0.2)
        report = listing["reports"][0]
        self.assertTrue(report["ok"], report)
        page = s.get(self.base + f"/backtests/{report['id']}")
        self.assertIn("SPY", page.text)
        self.assertEqual(requests.get(self.base + f"/backtests/{report['id']}", allow_redirects=False).status_code, 302)
        self.assertEqual(s.get(self.base + "/backtests/..%2Fsettings").status_code, 404)

        # The owner can reopen Setup and come back with just one bot
        self.assertEqual(requests.post(self.base + "/api/setup/reopen", headers=H, json={}).status_code, 401)
        r = s.post(self.base + "/api/setup/reopen", headers=H, json={})
        self.assertTrue(r.json()["ok"])
        self.assertFalse(self.app.settings.setup_done)
        self.assertTrue(s.get(self.base + "/api/setup").json()["open"])
        only_jev = self.setup_body(anthropic_key="unused-key", openai_key="")
        only_jev["bots"]["claude"] = {"enabled": False}
        only_jev["bots"]["openai"] = {"enabled": False}
        none_on = self.setup_body()
        for b in none_on["bots"].values():
            b["enabled"] = False
        with mock.patch("stockbots.brains.JevBrain.check_key", return_value=(True, "ok")):
            r = s.post(self.base + "/api/setup", headers=H, json=none_on)
            self.assertIn("bots", r.json()["errors"])
            r = s.post(self.base + "/api/setup", headers=H, json=only_jev)
        self.assertEqual(r.status_code, 200, r.text)
        state = s.get(self.base + "/api/state").json()
        on = {b["id"]: b["enabled"] for b in state["bots"]}
        self.assertEqual(on, {"claude": False, "jev": True, "openai": False})
        self.assertEqual(self.app.settings.data.get("anthropic_key"), "", "keys for bots that are off aren't kept")


if __name__ == "__main__":
    unittest.main()
