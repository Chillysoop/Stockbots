"""The backtester, on made-up price history."""
import random
import unittest
from datetime import date, datetime, timedelta, timezone

from stockbots import backtest as bt
from stockbots.alpaca import AlpacaError
from stockbots.config import ET
from stockbots.styles import STYLES

from .helpers import make_config


def weekdays(start: date, end: date):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


class FakeHistory:
    """Daily and 5-minute bars with a steady uptrend, dips and an occasional breakout."""

    def __init__(self, sip_ok=True, seed=3):
        self.sip_ok = sip_ok
        self.seed = seed
        self.calls = []

    def _daily(self, sym, start, end):
        rng = random.Random(hash(sym) % 1000 + self.seed)
        price = 50 + rng.random() * 200
        out = []
        for d in weekdays(start.date(), end.date()):
            drift = 0.0008 + (0.03 if rng.random() < 0.02 else 0) - (0.035 if rng.random() < 0.03 else 0)
            o = price
            c = max(5.0, o * (1 + drift + rng.gauss(0, 0.012)))
            h = max(o, c) * (1 + abs(rng.gauss(0, 0.006)))
            l = min(o, c) * (1 - abs(rng.gauss(0, 0.006)))
            t = datetime(d.year, d.month, d.day, 4, 0, tzinfo=timezone.utc)
            out.append({"t": t.isoformat().replace("+00:00", "Z"), "o": o, "h": h, "l": l, "c": c,
                        "v": rng.randint(900_000, 1_100_000)})
            price = c
        return out

    def bars(self, symbols, timeframe, start, end=None, feed="iex", adjustment="split", max_pages=20):
        self.calls.append((timeframe, feed))
        if feed == "sip" and not self.sip_ok:
            raise AlpacaError(403, "subscription does not permit querying recent SIP data")
        early = datetime(2023, 1, 2, tzinfo=timezone.utc)
        if timeframe == "1Day":
            out = {}
            for s in symbols:
                rows = self._daily(s, early, end)
                out[s] = [b for b in rows if datetime.fromisoformat(b["t"].replace("Z", "+00:00")) >= start]
            return out
        out = {}
        for s in symbols:
            daily = {datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(ET).date(): b
                     for b in self._daily(s, early, end)}
            rows = []
            for d in weekdays(start.date(), (end - timedelta(days=1)).date()):
                b = daily.get(d)
                if not b:
                    continue
                o, c = b["o"], b["c"]
                for k in range(78):
                    p = o + (c - o) * (k + 1) / 78
                    t = datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET) + timedelta(minutes=5 * k)
                    rows.append({"t": t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                                 "o": p, "h": p * 1.002, "l": p * 0.998, "c": p, "v": b["v"] / 78 * 1.6,
                                 "vw": p})
            out[s] = rows
        return out


class BacktestTests(unittest.TestCase):
    def test_daily_bots_run_and_score(self):
        cfg = make_config()
        client = FakeHistory()
        for bot in ("jev", "claude"):
            grid = [bt.Params(10, 3, 1.0), bt.Params(25, 0, 1.5)]
            summary = bt.run_backtest(bot, 12, client, cfg, grid=grid)
            self.assertGreater(summary["days"], 200)
            self.assertEqual(len(summary["rows"]), 3, "the current settings are always included")
            cur = next(r for r in summary["rows"] if r["current"])
            self.assertGreater(cur["all"]["trades"], 0, f"{bot} should trade on this made-up history")
            for key in ("return_pct", "max_drawdown_pct", "sharpe", "trades"):
                self.assertIn(key, cur["all"])
            self.assertLessEqual(cur["all"]["max_drawdown_pct"], 0)
            self.assertIn("return_pct", summary["benchmark"]["all"])
            page = bt.render_html(summary, "Test Bot")
            self.assertIn("Test Bot", page)
            self.assertIn("<svg", page)

    def test_momentum_runs_and_is_flat_overnight(self):
        cfg = make_config()
        summary = bt.run_backtest("openai", 2, FakeHistory(), cfg, grid=[bt.Params(15, 3, 1.0)])
        self.assertGreater(summary["days"], 30)
        for t in summary["current"]["recent_trades"]:
            self.assertEqual(t["opened"], t["closed"], "momentum trades never stay open overnight")

    def test_falls_back_to_iex(self):
        client = FakeHistory(sip_ok=False)
        bt.run_backtest("jev", 6, client, make_config(), grid=[bt.Params(15, 3, 1.0)])
        self.assertIn(("1Day", "iex"), client.calls)

    def test_no_peeking_at_the_future(self):
        """Changing prices after a date must not change any result before it."""
        cfg = make_config()
        client = FakeHistory()
        end = datetime.now(ET).date() - timedelta(days=1)
        start = end - timedelta(days=300)
        data = bt.load_data(client, "dip", start, end)
        cutoff = data.days[150]
        sims_a = bt.run_grid(data, [bt.Params(15, 3, 1.0)], cfg)
        for sym, rows in data.daily.items():
            for b in rows:
                if datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(ET).date() > cutoff:
                    b["c"] *= 3.0
                    b["h"] *= 3.0
                    b["o"] *= 0.2
                    b["l"] *= 0.2
        sims_b = bt.run_grid(data, [bt.Params(15, 3, 1.0)], cfg)
        a = [v for d, v in sims_a[0].equity_curve if d <= cutoff]
        b = [v for d, v in sims_b[0].equity_curve if d <= cutoff]
        self.assertEqual(a, b)

    def test_stop_fills(self):
        cfg = make_config()
        sim = bt.Sim("dip", bt.Params(15, 3, 1.0), cfg)
        when = datetime(2026, 1, 5, 12, tzinfo=ET)
        sim.buy("AAPL", 10, 100.0, 95.0, when, add=False)
        sim.buy("MSFT", 10, 100.0, 95.0, when, add=False)
        sim.check_stops({"AAPL": {"o": 99, "l": 94, "c": 96}, "MSFT": {"o": 90, "l": 88, "c": 91}}, when)
        fills = {t.symbol: t.exit for t in sim.trades}
        self.assertAlmostEqual(fills["AAPL"], 95.0 * (1 - sim.slip))
        self.assertAlmostEqual(fills["MSFT"], 90.0 * (1 - sim.slip), msg="a gap down fills at the open")
        self.assertEqual(sim.lots, {})

    def test_metrics(self):
        d0 = date(2026, 1, 1)
        curve = [(d0, 100.0), (d0 + timedelta(days=1), 110.0), (d0 + timedelta(days=2), 99.0),
                 (d0 + timedelta(days=3), 121.0)]
        m = bt.metrics(curve)
        self.assertAlmostEqual(m["return_pct"], 21.0)
        self.assertAlmostEqual(m["max_drawdown_pct"], -10.0)

    def test_grid_includes_current_settings(self):
        cfg = make_config()
        for key in STYLES:
            grid = bt.default_grid(key, cfg)
            self.assertIn(bt.current_params(key, cfg), grid)
            self.assertGreaterEqual(len(grid), 36)


if __name__ == "__main__":
    unittest.main()
