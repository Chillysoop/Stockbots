"""Regression tests for the independent code review: protection during data outages,
owner actions racing the loop, stop-outs the bot never saw, adds, restarts and settings."""
import os
import re
import sqlite3
import threading
import time
import unittest
from datetime import timedelta

from stockbots.alpaca import AlpacaError
from stockbots.brains import Pick
from stockbots.config import bot_def
from stockbots.engine import BotRunner, Engine

from .helpers import Clock, et, make_config, temp_db
from .test_engine import ScriptedBrain, make_runner

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def count(db_path, sql, *args):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(sql, args).fetchone()[0]


def broken_bars(*a, **k):
    raise AlpacaError(500, "market data is down")


class DataOutageTests(unittest.TestCase):
    def test_stops_still_placed_without_market_data(self):
        clock = Clock(et(2026, 10, 21, 11, 0))
        brain = ScriptedBrain("claude", "hold")
        runner, acct, market, db = make_runner("claude", brain, clock)
        acct._fill("SPY", "buy", 10)  # held with no stop
        acct.bars = broken_bars
        runner.tick()
        stops = [o for o in acct.open_orders() if o["type"] == "stop" and o["symbol"] == "SPY"]
        self.assertEqual(len(stops), 1, "the position is protected even with no prices")
        self.assertEqual(stops[0]["qty"], 10)
        self.assertIn("market data unavailable", runner.state["status_detail"])
        self.assertEqual(brain.calls, 0, "no new decisions without data")

    def test_momentum_still_flattens_without_market_data(self):
        clock = Clock(et(2026, 10, 21, 15, 51))
        brain = ScriptedBrain("openai", "hold")
        runner, acct, market, db = make_runner("openai", brain, clock)
        acct._fill("NVDA", "buy", 5)
        acct.bars = broken_bars
        runner.tick()
        self.assertEqual(acct.pos, {}, "the end-of-day flatten doesn't wait for market data")


class PauseRaceTests(unittest.TestCase):
    def test_pause_during_the_ai_call_blocks_the_buy(self):
        clock = Clock(et(2026, 10, 21, 11, 0))

        class PausesMidCall(ScriptedBrain):
            def pick(self, *a, **k):
                db.set_paused("jev", True)  # the owner presses Pause while the AI thinks
                return super().pick(*a, **k)

        brain = PausesMidCall("jev", "open:AAPL")
        runner, acct, market, db = make_runner("jev", brain, clock)
        market.set_price("AAPL", market.prev_close("AAPL") * 0.94)
        runner.tick()
        self.assertEqual(brain.calls, 1)
        self.assertEqual(acct.pos, {})
        d = db.recent_decisions(1)[0]
        self.assertEqual((d["outcome"], d["detail"]), ("vetoed", "bot paused"))

    def test_owner_close_all_waits_for_a_running_tick(self):
        clock = Clock(et(2026, 10, 21, 11, 0))
        entered, release = threading.Event(), threading.Event()

        class SlowBrain(ScriptedBrain):
            def pick(self, *a, **k):
                entered.set()
                release.wait(5)
                return super().pick(*a, **k)

        brain = SlowBrain("jev", "open:AAPL")
        runner, acct, market, db = make_runner("jev", brain, clock)
        market.set_price("AAPL", market.prev_close("AAPL") * 0.94)
        t = threading.Thread(target=runner.tick)
        t.start()
        self.assertTrue(entered.wait(5))
        done = []
        f = threading.Thread(target=lambda: done.append(runner.flatten()))
        f.start()
        time.sleep(0.3)
        self.assertEqual(done, [], "Close all waits while the tick is mid-decision")
        release.set()
        t.join(5)
        f.join(5)
        self.assertEqual(len(done), 1)
        self.assertEqual(acct.pos, {}, "the buy from the tick is closed too, not left behind")


class StopOutTests(unittest.TestCase):
    def test_stop_hit_before_the_bot_looked_is_recorded_once(self):
        clock = Clock(et(2026, 10, 21, 11, 0))
        brain = ScriptedBrain("jev", "open:AAPL")
        runner, acct, market, db = make_runner("jev", brain, clock)
        market.set_price("AAPL", market.prev_close("AAPL") * 0.94)
        runner.tick()
        self.assertIn("AAPL", acct.pos)
        stop = [o for o in acct.open_orders() if o["type"] == "stop"][0]["stop"]
        market.set_price("AAPL", stop * 0.98)
        brain.choice = "hold"
        clock.set_et(et(2026, 10, 21, 11, 1))  # only a minute later
        runner.tick()
        self.assertNotIn("AAPL", acct.pos)
        self.assertEqual(db.get_meta("jev"), {})
        self.assertIsNotNone(db.last_exit_ts("jev", "AAPL"))
        clock.set_et(et(2026, 10, 21, 11, 2))
        runner.tick()
        self.assertEqual(count(runner.cfg.db_path, "SELECT COUNT(*) FROM exits WHERE symbol='AAPL'"), 1)
        self.assertEqual(count(runner.cfg.db_path, "SELECT COUNT(*) FROM loss_sales WHERE symbol='AAPL'"), 1)

    def test_a_buy_that_never_filled_is_not_an_exit(self):
        clock = Clock(et(2026, 10, 21, 11, 0))
        brain = ScriptedBrain("jev", "hold")
        runner, acct, market, db = make_runner("jev", brain, clock)
        opened = (clock() - timedelta(minutes=2)).isoformat(timespec="seconds")
        db.upsert_meta("jev", "MSFT", opened, 90.0)  # an order that hasn't filled yet
        runner.tick()
        self.assertIn("MSFT", db.get_meta("jev"), "kept while the order may still fill")
        clock.set_et(et(2026, 10, 21, 11, 4))
        runner.tick()
        self.assertNotIn("MSFT", db.get_meta("jev"))
        self.assertIsNone(db.last_exit_ts("jev", "MSFT"))
        self.assertEqual(count(runner.cfg.db_path, "SELECT COUNT(*) FROM loss_sales"), 0)

    def test_closing_a_position_the_stop_already_sold(self):
        clock = Clock(et(2026, 10, 21, 11, 0))
        brain = ScriptedBrain("jev", "open:AAPL")
        runner, acct, market, db = make_runner("jev", brain, clock)
        market.set_price("AAPL", market.prev_close("AAPL") * 0.94)
        runner.tick()
        stop = [o for o in acct.open_orders() if o["type"] == "stop"][0]["stop"]
        market.set_price("AAPL", stop * 0.98)
        acct.clock()  # the stop fills at the broker
        self.assertNotIn("AAPL", acct.pos)
        ok, detail = runner._close("AAPL", "rules say close", decided_by="rules")
        self.assertTrue(ok)
        self.assertIn("no AAPL position", detail)
        self.assertIsNotNone(db.last_exit_ts("jev", "AAPL"), "cooldown still applies")
        self.assertEqual(count(runner.cfg.db_path, "SELECT COUNT(*) FROM loss_sales WHERE symbol='AAPL'"), 1)


class AddTests(unittest.TestCase):
    def test_broker_refuses_a_buy_while_a_stop_is_open(self):
        clock = Clock(et(2026, 10, 21, 11, 0))
        runner, acct, market, db = make_runner("claude", ScriptedBrain("claude", "hold"), clock)
        acct._fill("SPY", "buy", 10)
        acct.submit_stop("SPY", 10, 90.0, "gtc", "x")
        with self.assertRaises(AlpacaError) as caught:
            acct.submit_market("SPY", 5, "buy", "gtc", "y")
        self.assertIn("wash trade", caught.exception.message)

    def test_add_replaces_the_stop_with_one_for_the_whole_position(self):
        clock = Clock(et(2026, 10, 21, 11, 0))
        runner, acct, market, db = make_runner("claude", ScriptedBrain("claude", "hold"), clock)
        acct._fill("SPY", "buy", 10)
        acct.submit_stop("SPY", 10, 90.0, "gtc", "old-stop")
        runner._submit_add("SPY", 5, "sb-claude-add-test", 95.0)
        self.assertEqual(acct.pos["SPY"]["qty"], 15)
        stops = [o for o in acct.open_orders() if o["type"] == "stop" and o["symbol"] == "SPY"]
        self.assertEqual([(s["qty"], s["stop"]) for s in stops], [(15, 95.0)])

    def test_failed_add_puts_the_stop_back(self):
        clock = Clock(et(2026, 10, 21, 11, 0))
        runner, acct, market, db = make_runner("claude", ScriptedBrain("claude", "hold"), clock)
        acct._fill("SPY", "buy", 10)
        acct.submit_stop("SPY", 10, 90.0, "gtc", "old-stop")
        acct.fail_orders = True
        with self.assertRaises(AlpacaError):
            runner._submit_add("SPY", 5, "sb-claude-add-test", 95.0)
        stops = [o for o in acct.open_orders() if o["type"] == "stop" and o["symbol"] == "SPY"]
        self.assertEqual([s["qty"] for s in stops], [10])

    def test_add_through_the_loop(self):
        clock = Clock(et(2026, 10, 21, 11, 0))
        brain = ScriptedBrain("claude", "add:SPY")
        runner, acct, market, db = make_runner("claude", brain, clock)
        now_price = market.price["SPY"]
        market.set_price("SPY", now_price * 0.85)
        acct._fill("SPY", "buy", 10)  # bought well below today's price: a winner
        market.set_price("SPY", now_price)
        runner.tick()  # adopts the position and gives it a stop
        clock.set_et(et(2026, 10, 21, 11, 5))
        # The database stamps orders with the real time; line it up with the test clock.
        with sqlite3.connect(runner.cfg.db_path) as conn:
            conn.execute("UPDATE orders SET ts=?", ((clock() - timedelta(minutes=5)).isoformat(timespec="seconds"),))
        runner.tick()
        self.assertIn("add:SPY", brain.menus[-1])
        d = db.recent_decisions(1)[0]
        self.assertEqual((d["move"], d["outcome"]), ("add:SPY", "submitted"))
        qty = acct.pos["SPY"]["qty"]
        self.assertGreater(qty, 10)
        stops = [o for o in acct.open_orders() if o["type"] == "stop" and o["symbol"] == "SPY"]
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0]["qty"], qty, "one stop covers every share")
        self.assertTrue(db.get_meta("claude")["SPY"]["added"])


class EngineRestartTests(unittest.TestCase):
    def test_restart_waits_for_the_old_loop(self):
        clock = Clock(et(2026, 10, 24, 11, 0))  # a Saturday: ticks do almost nothing
        cfg = make_config()
        db = temp_db(cfg)
        in_tick, release = threading.Event(), threading.Event()

        class SlowRunner(BotRunner):
            def _tick(self):
                in_tick.set()
                release.wait(5)

        old = SlowRunner(bot_def("jev"), cfg, db, object(), None, "Jev Dips", now_fn=clock)
        engine = Engine(cfg, db)
        engine.start([old])
        self.assertTrue(in_tick.wait(5))
        threading.Timer(0.3, release.set).start()
        new = SlowRunner(bot_def("jev"), cfg, db, object(), None, "Jev Dips", now_fn=clock)
        engine.start([new])
        self.assertFalse(old.is_alive(), "the old loop finished before the new one began")
        self.assertIs(new._act_lock, old._act_lock)
        engine.stop()
        self.assertFalse(new.is_alive())


class ComposeTests(unittest.TestCase):
    def test_every_setting_can_be_set_from_docker_manager(self):
        config = open(os.path.join(ROOT, "stockbots", "config.py")).read()
        compose = open(os.path.join(ROOT, "docker-compose.yml")).read()
        names = set(re.findall(r'env(?:_float|_int|_bool)?\("([A-Z_]+)"', config))
        internal = {"DATA_DIR", "PORT"}
        missing = sorted(n for n in names - internal if not n.endswith("_URL") and f"{n}:" not in compose)
        self.assertEqual(missing, [], "add these to docker-compose.yml so Hostinger users can set them")


if __name__ == "__main__":
    unittest.main()
