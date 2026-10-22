"""The cross-broker wash-sale guard, the race record across broker moves, and Prepare to move."""
import unittest

from stockbots.config import bot_def
from stockbots.engine import BotRunner
from stockbots.styles import STYLES

from .helpers import Clock, et, make_config, new_account, temp_db, uptrend_market
from .test_engine import ScriptedBrain


def two_bots(clock, guard=True):
    """The dip bot (Jev) and the momentum bot (OpenAI) share one database, like on a real server."""
    cfg = make_config()
    cfg.wash_sale_guard = guard
    db = temp_db(cfg)
    symbols = sorted(set(STYLES["dip"].universe) | set(STYLES["momentum"].universe) | {"VOO"})
    market = uptrend_market(clock, symbols)
    jev_acct, oai_acct = new_account(market), new_account(market)
    jev = BotRunner(bot_def("jev"), cfg, db, jev_acct, ScriptedBrain("jev", "hold"), "Jev Dips", now_fn=clock,
                    broker="tradier", broker_label="Tradier live")
    oai = BotRunner(bot_def("openai"), cfg, db, oai_acct, ScriptedBrain("openai", "hold"), "Codex Momentum",
                    now_fn=clock, broker="public", broker_label="Public live")
    return cfg, db, market, (jev, jev_acct), (oai, oai_acct)


class WashSaleGuardTests(unittest.TestCase):
    def test_loss_sale_blocks_every_bot_for_31_days(self):
        clock = Clock(et(2026, 10, 21, 11, 0))
        cfg, db, market, (jev, jacct), (oai, oacct) = two_bots(clock)
        market.set_price("NVDA", market.prev_close("NVDA") * 0.94)
        jev.brain.choice = "open:NVDA"
        jev.tick()
        self.assertIn("NVDA", jacct.pos)
        # NVDA slips a little (still above its stop); Jev sells it at a loss
        market.set_price("NVDA", market.price["NVDA"] * 0.995)
        clock.set_et(et(2026, 10, 21, 11, 10))
        jev.tick()  # refresh positions so the loss is known
        self.assertIn("NVDA", jacct.pos)
        ok, why = jev._close("NVDA", "test", decided_by="code")
        self.assertTrue(ok, why)
        self.assertEqual(len(db.loss_sales_since("2026-01-01")), 1)
        ok, why = jev._close("NVDA", "again", decided_by="code")
        self.assertIn("no NVDA position", why)
        self.assertEqual(len(db.loss_sales_since("2026-01-01")), 1, "closing nothing records nothing")
        opens, adds = oai.wash_blocks(["NVDA", "AMD"], clock())
        self.assertIn("NVDA", opens)
        self.assertNotIn("AMD", opens)
        self.assertIn("sold at a loss", opens["NVDA"])
        # The block holds for 31 days, then lifts
        later = et(2026, 11, 21, 11, 0).astimezone(clock().tzinfo)
        self.assertIn("NVDA", jev.wash_blocks(["NVDA"], later)[0])
        much_later = et(2026, 11, 23, 11, 0).astimezone(clock().tzinfo)
        self.assertNotIn("NVDA", jev.wash_blocks(["NVDA"], much_later)[0])

    def test_bots_never_overlap_on_a_stock(self):
        clock = Clock(et(2026, 10, 21, 11, 0))
        cfg, db, market, (jev, jacct), (oai, oacct) = two_bots(clock)
        market.set_price("AMZN", market.prev_close("AMZN") * 0.94)
        jev.brain.choice = "open:AMZN"
        jev.tick()
        self.assertIn("AMZN", jacct.pos)
        opens, _ = oai.wash_blocks(["AMZN"], clock())
        self.assertIn("another bot", opens["AMZN"])
        # Even after Jev sells at a profit, its recent buy keeps the other bot out for 30 days
        market.set_price("AMZN", market.price["AMZN"] * 1.05)
        jev.tick()
        jev._close("AMZN", "profit", decided_by="code")
        opens, _ = oai.wash_blocks(["AMZN"], clock())
        self.assertIn("bought", opens["AMZN"])

    def test_equivalent_funds(self):
        clock = Clock(et(2026, 10, 21, 11, 0))
        cfg, db, market, (jev, jacct), (oai, oacct) = two_bots(clock)
        db.record_loss_sale("claude", "VOO")
        opens, _ = jev.wash_blocks(["SPY", "QQQ"], clock())
        self.assertIn("SPY", opens, "VOO and SPY track the same index")
        self.assertNotIn("QQQ", opens)

    def test_guard_blocks_the_menu_and_the_order(self):
        clock = Clock(et(2026, 10, 21, 11, 0))
        cfg, db, market, (jev, jacct), (oai, oacct) = two_bots(clock)
        db.record_loss_sale("openai", "AAPL")
        market.set_price("AAPL", market.prev_close("AAPL") * 0.94)
        market.set_price("MSFT", market.prev_close("MSFT") * 0.94)
        jev.brain.choice = "open:AAPL"
        jev.tick()
        self.assertNotIn("open:AAPL", jev.brain.menus[-1], "the AI is never offered a blocked buy")
        self.assertIn("open:MSFT", jev.brain.menus[-1])
        self.assertNotIn("AAPL", jacct.pos)

    def test_off_on_paper_by_default(self):
        cfg = make_config()
        self.assertFalse(cfg.wash_sale_guard)
        clock = Clock(et(2026, 10, 21, 11, 0))
        _, db, market, (jev, _), _ = two_bots(clock, guard=False)
        db.record_loss_sale("openai", "AAPL")
        self.assertEqual(jev.wash_blocks(["AAPL"], clock()), ({}, {}))


class RaceAndMoveTests(unittest.TestCase):
    def test_race_survives_a_broker_move(self):
        clock = Clock(et(2026, 10, 21, 15, 0))
        cfg, db, market, (jev, _), _ = two_bots(clock)
        # Day 1 on Tradier: $5,000 -> $5,100 (+2%)
        db.note_equity("jev", "2026-10-20", "tradier:VA1", 5000)
        db.note_equity("jev", "2026-10-20", "tradier:VA1", 5100)
        # Day 2 moved to an Alpaca sub-account with $20,000: $20,000 -> $20,200 (+1%)
        db.note_equity("jev", "2026-10-21", "alpaca:SUB1", 20000)
        db.note_equity("jev", "2026-10-21", "alpaca:SUB1", 20200)
        race = jev.race_series()
        self.assertAlmostEqual(race["return_pct"][-1], (1.02 * 1.01 - 1) * 100, places=3)
        self.assertEqual(race["return_pct"][0], 0.0)
        self.assertEqual(len(race["timestamp"]), 3)

    def test_overnight_moves_count(self):
        clock = Clock(et(2026, 10, 21, 15, 0))
        cfg, db, market, (jev, _), _ = two_bots(clock)
        db.note_equity("jev", "2026-10-20", "a:1", 100)
        db.note_equity("jev", "2026-10-20", "a:1", 110)  # +10% on day 1
        db.note_equity("jev", "2026-10-21", "a:1", 99)  # gapped down overnight
        self.assertAlmostEqual(jev.race_series()["return_pct"][-1], -1.0, places=3)

    def test_prepare_move(self):
        clock = Clock(et(2026, 10, 21, 11, 0))
        cfg, db, market, (jev, jacct), _ = two_bots(clock)
        market.set_price("AAPL", market.prev_close("AAPL") * 0.94)
        jev.brain.choice = "open:AAPL"
        jev.tick()
        self.assertIn("AAPL", jacct.pos)
        msg = jev.prepare_move()
        self.assertIn("closing every position", msg)
        self.assertEqual(jacct.pos, {})
        self.assertEqual(jacct.open_orders(), [])
        st = db.bot_state("jev")
        self.assertTrue(st["paused"] and st["moving"])
        jev.tick()  # refresh: now flat
        snap = jev.snapshot()
        self.assertTrue(snap["moving"])
        self.assertTrue(snap["ready_to_move"])
        self.assertEqual(snap["broker_label"], "Tradier live")
        jev.finish_move()
        self.assertFalse(jev.snapshot()["moving"])


if __name__ == "__main__":
    unittest.main()
