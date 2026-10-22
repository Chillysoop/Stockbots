"""DEMO=true: a pretend market on a fast clock, for previewing the dashboard.

No keys, no internet, no real trades. Password: demo-password
One real second is one market minute, starting at 10:00 ET on the latest weekday.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from .app import App
from .config import BOTS, ET, Config
from .sim import FakeBrain, SimAlpaca, SimMarket, business_days_back, make_daily
from .styles import STYLES

DEMO_PASSWORD = "demo-password"


def build_demo_app(cfg: Config) -> App:
    real_start = time.time()
    now_et = datetime.now(ET)
    day = now_et.date()
    while day.weekday() >= 5:
        day = day - timedelta(days=1)
    sim_start = datetime(day.year, day.month, day.day, 10, 0, tzinfo=ET).astimezone(timezone.utc)

    def now_fn():
        return sim_start + timedelta(minutes=(time.time() - real_start))

    market = SimMarket(now_fn, seed=11)
    market.jitter = 0.004
    market.volume_mult = 2.2
    dates = business_days_back(day, 300)
    symbols = sorted({s for st in STYLES.values() for s in st.universe})
    for i, sym in enumerate(symbols):
        start_price = 40 + (i * 37) % 400
        market.add_symbol(sym, make_daily(start_price, dates, 0.0016, 0.012, seed=100 + i))
    # A few setups so the demo has something to do
    for sym in ("AAPL", "MSFT", "JPM"):
        market.set_price(sym, market.prev_close(sym) * 0.95)
    for sym in ("NVDA", "PLTR", "AMD"):
        market.set_price(sym, market.prev_close(sym) * 1.035)

    accounts = {}

    def client_factory(key, secret):
        if key not in accounts:
            accounts[key] = SimAlpaca(market, cash=100_000.0, account_number=f"PA-{key}")
        return accounts[key]

    def brain_factory(provider, cfg_, api_key):
        return FakeBrain(provider) if api_key else None

    cfg.tick_seconds = 3
    cfg.model_min_interval_sec = 0
    cfg.closed_refresh_seconds = 30
    app = App(cfg, client_factory=client_factory, brain_factory=brain_factory, now_fn=now_fn)
    s = app.settings
    s.data["bots"] = {b["id"]: {"name": b["default_name"], "alpaca_key": f"demo-{b['id']}-key",
                                 "alpaca_secret": "demo"} for b in BOTS}
    s.data.update({"jev_key": "demo", "anthropic_key": "demo", "openai_key": "demo"})
    s.set_password(DEMO_PASSWORD)
    s.data["setup_done_at"] = int(time.time())
    s.save()
    app.start_bots()
    return app
