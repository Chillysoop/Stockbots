import os
import tempfile
from datetime import datetime, timezone

from stockbots.config import ET, Config
from stockbots.db import DB
from stockbots.sim import SimAlpaca, SimMarket, business_days_back, make_daily


class Clock:
    """A settable 'now' for tests."""

    def __init__(self, et: datetime):
        self.t = et.astimezone(timezone.utc)

    def __call__(self):
        return self.t

    def set_et(self, et: datetime):
        self.t = et.astimezone(timezone.utc)


def et(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=ET)


def make_config(tmpdir: str | None = None) -> Config:
    tmpdir = tmpdir or tempfile.mkdtemp()
    os.environ["DATA_DIR"] = tmpdir
    cfg = Config()
    cfg.tick_seconds = 1
    cfg.model_min_interval_sec = 0
    return cfg


def temp_db(cfg: Config) -> DB:
    return DB(cfg.db_path)


def uptrend_market(clock: Clock, symbols, start=100.0, drift=0.002, vol=0.006, days=300):
    market = SimMarket(clock)
    today = clock().astimezone(ET).date()
    dates = business_days_back(today, days)
    for i, s in enumerate(symbols):
        market.add_symbol(s, make_daily(start, dates, drift, vol, seed=i + 1))
    return market


def new_account(market, cash=100_000.0):
    return SimAlpaca(market, cash=cash)
