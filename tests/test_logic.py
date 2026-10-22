"""Indicators, styles and the risk layer."""
import unittest
from datetime import timedelta

from stockbots import indicators as ind
from stockbots import risk
from stockbots.styles import STYLES, Move, trading_days_between

from .helpers import et, make_config


class IndicatorTests(unittest.TestCase):
    def test_sma(self):
        self.assertEqual(ind.sma([1, 2, 3, 4], 2), 3.5)
        self.assertIsNone(ind.sma([1], 2))
        self.assertEqual(ind.sma_at([1, 2, 3, 4, 5], 2, 1), 3.5)

    def test_rsi_extremes(self):
        self.assertEqual(ind.rsi([1, 2, 3, 4, 5], 2), 100.0)
        self.assertAlmostEqual(ind.rsi([5, 4, 3, 2, 1], 2), 0.0)
        mixed = ind.rsi([10, 11, 10, 11, 10, 11], 2)
        self.assertTrue(30 < mixed < 80)

    def test_atr_and_bollinger(self):
        highs = [11] * 20
        lows = [9] * 20
        closes = [10] * 20
        self.assertAlmostEqual(ind.atr(highs, lows, closes, 14), 2.0)
        lo, mid, hi = ind.bollinger([10] * 20)
        self.assertEqual((lo, mid, hi), (10, 10, 10))

    def test_vwap(self):
        bars = [{"vw": 10, "v": 100}, {"vw": 20, "v": 300}]
        self.assertAlmostEqual(ind.session_vwap(bars), 17.5)

    def test_trading_days(self):
        friday = et(2026, 10, 23, 10, 0).date()
        monday = et(2026, 10, 26, 10, 0).date()
        self.assertEqual(trading_days_between(friday, monday), 1)


def trend_f(price=110.0, **over):
    f = dict(price=price, sma50=105.0, sma200=95.0, sma50_10d_ago=103.0, atr14=2.0, ret63_pct=8.0, rsi14=60.0,
             day_change_pct=0.5)
    f.update(over)
    return f


class StyleTests(unittest.TestCase):
    def test_trend_entry_rules(self):
        s = STYLES["trend"]
        self.assertTrue(s.entry("SPY", trend_f())[0])
        self.assertFalse(s.entry("SPY", trend_f(sma50_10d_ago=106.0))[0], "50-day must be rising")
        self.assertFalse(s.entry("SPY", trend_f(price=113.0))[0], "too stretched above the 50-day")
        self.assertFalse(s.entry("SPY", trend_f(ret63_pct=-1))[0])
        self.assertEqual(s.stop_for(trend_f(), 110.0), 105.0)

    def test_trend_exits(self):
        s = STYLES["trend"]
        pos = {"avg_entry_price": 100, "unrealized_plpc": 2.0}
        now = et(2026, 10, 21, 11, 0)
        self.assertIsNotNone(s.soft_exit("SPY", trend_f(price=104.5), pos, {}, now))
        self.assertIsNone(s.forced_exit("SPY", trend_f(price=104.5), pos, {}, now))
        self.assertIsNotNone(s.forced_exit("SPY", trend_f(price=103.5), pos, {}, now))

    def test_dip_rules(self):
        s = STYLES["dip"]
        f = dict(price=100.0, sma200=90.0, rsi2=5.0, bb_lower=101.0, atr14=2.0, sma5=103.0, day_change_pct=-2.0)
        ok, why, score = s.entry("AAPL", f)
        self.assertTrue(ok)
        self.assertIn("dip", why)
        self.assertFalse(s.entry("AAPL", dict(f, price=89.0))[0], "below the 200-day: no dip buying")
        self.assertFalse(s.entry("AAPL", dict(f, rsi2=40.0, bb_lower=95.0))[0])
        pos = {"unrealized_plpc": 1.5}
        self.assertIsNotNone(s.soft_exit("AAPL", dict(f, price=104.0), pos, {}, et(2026, 10, 21, 11, 0)))
        meta = {"opened_at": et(2026, 10, 12, 10, 0).isoformat()}
        self.assertIsNotNone(s.forced_exit("AAPL", f, pos, meta, et(2026, 10, 21, 11, 0)))
        meta = {"opened_at": et(2026, 10, 20, 10, 0).isoformat()}
        self.assertIsNone(s.forced_exit("AAPL", f, pos, meta, et(2026, 10, 21, 11, 0)))

    def test_momentum_rules(self):
        s = STYLES["momentum"]
        f = dict(price=105.0, or_high=104.0, vwap=103.0, rel_volume=2.0, day_change_pct=3.0, atr14=4.0,
                 day_high=105.0, mins_since_high=0)
        self.assertTrue(s.entry("NVDA", f)[0])
        self.assertFalse(s.entry("NVDA", dict(f, rel_volume=1.0))[0])
        self.assertFalse(s.entry("NVDA", dict(f, price=103.5))[0])
        pos = {"unrealized_plpc": 0.2}
        self.assertIsNotNone(s.soft_exit("NVDA", dict(f, price=102.0), pos, {}, et(2026, 10, 21, 11, 0)))
        self.assertIsNotNone(s.soft_exit("NVDA", dict(f, mins_since_high=50), pos, {}, et(2026, 10, 21, 11, 0)))
        self.assertEqual(s.flatten_all_at(), "15:50")
        self.assertEqual(s.stop_for(f, 105.0), 103.0)


def ctx_for(style="dip", now=None, **over):
    cfg = make_config()
    base = dict(
        cfg=cfg,
        style=STYLES[style],
        now_et=now or et(2026, 10, 21, 11, 0),
        market_open=True,
        account={"equity": 100_000.0, "last_equity": 100_000.0, "buying_power": 200_000.0, "cash": 100_000.0},
        positions={},
        meta={},
        entries_today=0,
        paused=False,
        features={"AAPL": dict(price=100.0, atr14=2.0)},
    )
    base.update(over)
    return risk.RiskContext(**base)


OPEN = Move(id="open:AAPL", kind="open", symbol="AAPL", text="buy")


class RiskTests(unittest.TestCase):
    def test_sizing_and_stop(self):
        v = risk.check(OPEN, ctx_for())
        self.assertTrue(v.ok, v.reason)
        self.assertEqual(v.qty, 150)  # 15% of $100k at $100
        self.assertEqual(v.stop, 95.0)  # 2.5 ATR below

    def test_vetoes(self):
        self.assertFalse(risk.check(OPEN, ctx_for(market_open=False)).ok)
        self.assertFalse(risk.check(OPEN, ctx_for(paused=True)).ok)
        self.assertFalse(risk.check(OPEN, ctx_for(entries_today=2)).ok)
        self.assertFalse(risk.check(OPEN, ctx_for(now=et(2026, 10, 21, 9, 31))).ok, "too early")
        self.assertFalse(risk.check(OPEN, ctx_for(now=et(2026, 10, 21, 15, 50))).ok, "too late")
        loss = {"equity": 96_000.0, "last_equity": 100_000.0, "buying_power": 200_000.0}
        v = risk.check(OPEN, ctx_for(account=loss))
        self.assertFalse(v.ok)
        self.assertIn("daily loss stop", v.reason)
        self.assertFalse(risk.check(OPEN, ctx_for(features={"AAPL": dict(price=3.0, atr14=0.1)})).ok)
        self.assertFalse(risk.check(OPEN, ctx_for(features={"AAPL": dict(price=100.0, atr14=9.0)})).ok,
                         "stop more than 15% away")

    def test_max_positions_and_cooldown(self):
        held = {s: {"qty": 1, "market_value": 100} for s in ("A", "B", "C", "D")}
        self.assertFalse(risk.check(OPEN, ctx_for(positions=held)).ok)
        recent = (et(2026, 10, 21, 10, 30)).isoformat()
        v = risk.check(OPEN, ctx_for(last_exit={"AAPL": recent}))
        self.assertFalse(v.ok)
        self.assertIn("cooldown", v.reason)
        old = (et(2026, 10, 21, 9, 0)).isoformat()
        self.assertTrue(risk.check(OPEN, ctx_for(last_exit={"AAPL": old})).ok)

    def test_buying_power_shrinks_size(self):
        acct = {"equity": 100_000.0, "last_equity": 100_000.0, "buying_power": 5_100.0}
        v = risk.check(OPEN, ctx_for(account=acct))
        self.assertTrue(v.ok)
        self.assertEqual(v.qty, 50)

    def test_add_limits(self):
        add = Move(id="add:AAPL", kind="add", symbol="AAPL", text="add")
        pos = {"AAPL": {"qty": 150, "market_value": 15_000.0}}
        v = risk.check(add, ctx_for(positions=pos))
        self.assertTrue(v.ok, v.reason)
        self.assertEqual(v.qty, 75)
        self.assertFalse(risk.check(add, ctx_for(positions=pos, meta={"AAPL": {"added": 1}})).ok)
        self.assertFalse(risk.check(add, ctx_for()).ok, "nothing to add to")

    def test_never_borrows_on_margin(self):
        held = {s: {"qty": 1, "market_value": 30_000.0} for s in ("A", "B", "C")}  # 90% invested
        v = risk.check(OPEN, ctx_for(positions=held))
        self.assertTrue(v.ok, v.reason)
        self.assertEqual(v.qty, 100, "only the last 10% of the account is left to spend")
        full = {s: {"qty": 1, "market_value": 25_000.0} for s in ("A", "B", "C")}
        full["D"] = {"qty": 1, "market_value": 25_000.0}
        add = Move(id="add:A", kind="add", symbol="A", text="add")
        ctx = ctx_for(style="trend", positions=full, features={"A": dict(price=100.0, atr14=2.0)})
        self.assertFalse(risk.check(add, ctx).ok, "fully invested: no adds")

    def test_close_and_hold(self):
        close = Move(id="close:AAPL", kind="close", symbol="AAPL", text="close")
        self.assertFalse(risk.check(close, ctx_for()).ok)
        v = risk.check(close, ctx_for(positions={"AAPL": {"qty": 10}}, paused=True))
        self.assertTrue(v.ok, "closing is allowed even when paused")
        self.assertTrue(risk.check(Move(id="hold", kind="hold", symbol=None, text=""), ctx_for()).ok)

    def test_recent_order_blocks_duplicates(self):
        just_now = (et(2026, 10, 21, 10, 59) + timedelta(seconds=30)).isoformat()
        self.assertFalse(risk.check(OPEN, ctx_for(last_order={"AAPL": just_now})).ok)


if __name__ == "__main__":
    unittest.main()
