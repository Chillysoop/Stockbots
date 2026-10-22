"""The full bot loop against a pretend Alpaca account."""
import unittest

from stockbots.brains import Pick
from stockbots.config import bot_def
from stockbots.engine import BotRunner
from stockbots.sim import FakeBrain

from .helpers import Clock, et, make_config, new_account, temp_db, uptrend_market


class ScriptedBrain(FakeBrain):
    """Answers with whatever the test says, if it's on the menu."""

    def __init__(self, provider, choice):
        super().__init__(provider)
        self.choice = choice
        self.menus = []

    def pick(self, bot_name, style_label, rules, state, menu):
        self.calls += 1
        self.menus.append([m.id for m in menu])
        move = self.choice if self.choice in {m.id for m in menu} else "hold"
        return Pick(move, 0.7, "scripted", 0.0005, "scripted", 5)


def make_runner(bot_id, brain, clock, symbols=None, prices=None, cfg=None):
    cfg = cfg or make_config()
    db = temp_db(cfg)
    bot = bot_def(bot_id)
    from stockbots.styles import STYLES

    market = uptrend_market(clock, symbols or STYLES[bot["style"]].universe)
    for sym, p in (prices or {}).items():
        market.set_price(sym, p)
    acct = new_account(market)
    runner = BotRunner(bot, cfg, db, acct, brain, bot["default_name"], now_fn=clock)
    return runner, acct, market, db


class EngineTests(unittest.TestCase):
    def test_market_closed_does_nothing(self):
        clock = Clock(et(2026, 10, 24, 11, 0))  # Saturday
        brain = ScriptedBrain("jev", "open:AAPL")
        runner, acct, market, db = make_runner("jev", brain, clock)
        runner.tick()
        self.assertEqual(brain.calls, 0)
        self.assertEqual(acct.pos, {})
        self.assertEqual(runner.state["entry_blocker"], "market closed")
        self.assertIsNotNone(runner.state["account"])

    def test_dip_buy_gets_a_server_side_stop(self):
        clock = Clock(et(2026, 10, 21, 11, 0))
        brain = ScriptedBrain("jev", "open:AAPL")
        runner, acct, market, db = make_runner("jev", brain, clock)
        # A sharp drop today in a long uptrend
        market.set_price("AAPL", market.prev_close("AAPL") * 0.94)
        runner.tick()
        self.assertIn("open:AAPL", brain.menus[0])
        self.assertIn("AAPL", acct.pos)
        stops = [o for o in acct.open_orders() if o["type"] == "stop" and o["symbol"] == "AAPL"]
        self.assertEqual(len(stops), 1)
        self.assertLess(stops[0]["stop"], market.price["AAPL"])
        d = db.recent_decisions(1)[0]
        self.assertEqual((d["move"], d["decided_by"], d["outcome"]), ("open:AAPL", "jev", "submitted"))
        self.assertEqual(db.entries_today("2026-10-21", "jev"), 1)
        self.assertGreater(db.spend_today("2026-10-21", "jev")["usd"], 0)

    def test_ai_down_means_no_new_trades(self):
        clock = Clock(et(2026, 10, 21, 11, 0))
        brain = FakeBrain("jev", error="Jev 529: overloaded")
        runner, acct, market, db = make_runner("jev", brain, clock)
        market.set_price("AAPL", market.prev_close("AAPL") * 0.94)
        runner.tick()
        self.assertEqual(acct.pos, {})
        d = db.recent_decisions(1)[0]
        self.assertEqual((d["move"], d["decided_by"]), ("hold", "code"))
        self.assertIn("overloaded", d["reason"])
        self.assertIn("code is choosing", runner.state["model_status"])

    def test_spend_cap_stops_ai_calls(self):
        clock = Clock(et(2026, 10, 21, 11, 0))
        cfg = make_config()
        cfg.model_daily_usd_cap = 0.0005
        brain = ScriptedBrain("jev", "open:AAPL")
        runner, acct, market, db = make_runner("jev", brain, clock, cfg=cfg)
        market.set_price("AAPL", market.prev_close("AAPL") * 0.94)
        runner.tick()
        self.assertEqual(brain.calls, 0)
        self.assertIn("budget", db.recent_decisions(1)[0]["reason"])

    def test_paused_bot_does_not_trade(self):
        clock = Clock(et(2026, 10, 21, 11, 0))
        brain = ScriptedBrain("jev", "open:AAPL")
        runner, acct, market, db = make_runner("jev", brain, clock)
        db.set_paused("jev", True)
        market.set_price("AAPL", market.prev_close("AAPL") * 0.94)
        runner.tick()
        self.assertEqual(brain.calls, 0)
        self.assertEqual(acct.pos, {})

    def test_stop_hit_is_noticed(self):
        clock = Clock(et(2026, 10, 21, 11, 0))
        brain = ScriptedBrain("jev", "open:AAPL")
        runner, acct, market, db = make_runner("jev", brain, clock)
        market.set_price("AAPL", market.prev_close("AAPL") * 0.94)
        runner.tick()
        self.assertIn("AAPL", acct.pos)
        stop = [o for o in acct.open_orders() if o["type"] == "stop"][0]["stop"]
        market.set_price("AAPL", stop * 0.98)  # crash through the stop
        brain.choice = "hold"
        clock.set_et(et(2026, 10, 21, 11, 5))
        runner.tick()
        self.assertNotIn("AAPL", acct.pos)
        self.assertIsNotNone(db.last_exit_ts("jev", "AAPL"))
        self.assertTrue(any("closed at Alpaca" in e["message"] for e in db.recent_events()))

    def test_guard_places_stop_on_adopted_position(self):
        clock = Clock(et(2026, 10, 21, 11, 0))
        brain = ScriptedBrain("claude", "hold")
        runner, acct, market, db = make_runner("claude", brain, clock)
        acct._fill("SPY", "buy", 10)  # bought outside the bot, no stop
        runner.tick()
        stops = [o for o in acct.open_orders() if o["type"] == "stop" and o["symbol"] == "SPY"]
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0]["qty"], 10)

    def test_momentum_flattens_before_close(self):
        clock = Clock(et(2026, 10, 21, 15, 51))
        brain = ScriptedBrain("openai", "hold")
        runner, acct, market, db = make_runner("openai", brain, clock)
        acct._fill("NVDA", "buy", 5)
        runner.tick()
        self.assertEqual(acct.pos, {})
        d = db.recent_decisions(5)
        self.assertTrue(any(x["move"] == "close:ALL" and x["decided_by"] == "rules" for x in d))

    def test_half_day_flatten_uses_alpacas_clock(self):
        clock = Clock(et(2026, 11, 27, 12, 52))  # day after Thanksgiving: closes at 1 PM
        brain = ScriptedBrain("openai", "hold")
        runner, acct, market, db = make_runner("openai", brain, clock)
        real_clock = acct.clock

        def half_day_clock():
            c = real_clock()
            c["next_close"] = et(2026, 11, 27, 13, 0).isoformat()
            return c

        acct.clock = half_day_clock
        acct._fill("NVDA", "buy", 5)
        runner.tick()
        self.assertEqual(acct.pos, {}, "flattened 10 minutes before an early close")
        self.assertEqual(runner.state["entry_blocker"], "too close to the closing bell")

    def test_soft_exit_offered_and_taken(self):
        clock = Clock(et(2026, 10, 21, 11, 0))
        brain = ScriptedBrain("jev", "open:AAPL")
        runner, acct, market, db = make_runner("jev", brain, clock)
        market.set_price("AAPL", market.prev_close("AAPL") * 0.94)
        runner.tick()
        # Bounce back well above the 5-day average
        market.set_price("AAPL", market.prev_close("AAPL") * 1.06)
        brain.choice = "close:AAPL"
        clock.set_et(et(2026, 10, 21, 11, 10))
        runner.tick()
        self.assertIn("close:AAPL", brain.menus[-1])
        self.assertNotIn("AAPL", acct.pos)
        self.assertEqual([o for o in acct.open_orders() if o["symbol"] == "AAPL"], [], "stop canceled on close")

    def test_snapshot_has_no_secrets(self):
        clock = Clock(et(2026, 10, 21, 11, 0))
        runner, acct, market, db = make_runner("jev", ScriptedBrain("jev", "hold"), clock)
        runner.tick()
        snap = runner.snapshot()
        text = repr(snap).lower()
        self.assertNotIn("secret", text)
        self.assertEqual(snap["ai_label"], "Jev")
        self.assertEqual(snap["style"], "dip")


if __name__ == "__main__":
    unittest.main()
