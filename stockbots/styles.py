"""The three trading styles.

A style never places orders. It turns market data into numbers ("features"),
then says which moves are on the menu right now: open a new position, add to a
winner, close a position, or hold. The bot's AI picks one move from that menu,
and the risk layer has the final say.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from . import indicators as ind
from .config import ET


@dataclass
class Move:
    id: str  # "open:NVDA", "add:NVDA", "close:AAPL", "hold"
    kind: str  # open | add | close | hold
    symbol: str | None
    text: str  # plain-English description shown to the AI and on the dashboard
    score: float = 0.0  # used by the code chooser when the AI is not answering
    stop: float | None = None
    flags: dict = field(default_factory=dict)


HOLD = Move(id="hold", kind="hold", symbol=None, text="Hold: make no trade this minute.")


def hhmm(now_et: datetime) -> str:
    return now_et.strftime("%H:%M")


def minutes_since_open(now_et: datetime) -> float:
    open_dt = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    return (now_et - open_dt).total_seconds() / 60.0


def trading_days_between(start: date, end: date) -> int:
    """Weekdays from start (exclusive) to end (inclusive). Holidays are ignored."""
    if end <= start:
        return 0
    days = 0
    d = start
    while d < end:
        d = date.fromordinal(d.toordinal() + 1)
        if d.weekday() < 5:
            days += 1
    return days


def daily_features(daily: list[dict], snap: dict | None, today_et: date) -> dict | None:
    """Indicators from completed daily bars plus the live price."""
    if not snap or not snap.get("price"):
        return None
    bars = [b for b in daily if _bar_date(b) < today_et]
    if len(bars) < 60:
        return None
    closes = [float(b["c"]) for b in bars]
    highs = [float(b["h"]) for b in bars]
    lows = [float(b["l"]) for b in bars]
    vols = [float(b.get("v") or 0) for b in bars]
    price = float(snap["price"])
    live = closes + [price]
    bb = ind.bollinger(live, 20, 2.0)
    prev_close = closes[-1]
    return {
        "price": price,
        "prev_close": prev_close,
        "day_change_pct": ind.pct_change(price, prev_close),
        "sma5": ind.sma(live, 5),
        "sma20": ind.sma(closes, 20),
        "sma50": ind.sma(closes, 50),
        "sma50_10d_ago": ind.sma_at(closes, 50, 10),
        "sma200": ind.sma(closes, 200),
        "rsi2": ind.rsi(live, 2),
        "rsi14": ind.rsi(live, 14),
        "atr14": ind.atr(highs, lows, closes, 14),
        "bb_lower": bb[0] if bb else None,
        "bb_upper": bb[2] if bb else None,
        "high20": max(highs[-20:]),
        "low20": min(lows[-20:]),
        "ret63_pct": ind.pct_change(price, closes[-63]) if len(closes) >= 63 else None,
        "avg_volume20": sum(vols[-20:]) / 20.0,
    }


def intraday_features(f: dict, bars_today: list[dict], now_et: datetime) -> dict:
    """Adds opening range, VWAP and relative volume to daily features."""
    f = dict(f)
    elapsed = max(minutes_since_open(now_et), 1.0)
    first = [b for b in bars_today if _bar_minutes(b) < 15]
    if len(first) >= 1 and elapsed >= 15:
        f["or_high"] = max(float(b["h"]) for b in first)
        f["or_low"] = min(float(b["l"]) for b in first)
    else:
        f["or_high"] = f["or_low"] = None
    f["vwap"] = ind.session_vwap(bars_today)
    vol_today = sum(float(b.get("v") or 0) for b in bars_today)
    avg = f.get("avg_volume20") or 0
    f["rel_volume"] = (vol_today / (avg * min(elapsed, 390) / 390.0)) if avg > 0 else None
    if bars_today:
        high = max(float(b["h"]) for b in bars_today)
        high_bar = max(bars_today, key=lambda b: float(b["h"]))
        f["day_high"] = max(high, f["price"])
        f["mins_since_high"] = 0.0 if f["price"] >= high else max(elapsed - _bar_minutes(high_bar) - 5, 0.0)
    else:
        f["day_high"] = f["price"]
        f["mins_since_high"] = 0.0
    return f


def _bar_date(bar: dict) -> date:
    return _bar_time(bar).date()


def _bar_time(bar: dict) -> datetime:
    t = bar["t"]
    if isinstance(t, datetime):
        return t.astimezone(ET)
    return datetime.fromisoformat(str(t).replace("Z", "+00:00")).astimezone(ET)


def _bar_minutes(bar: dict) -> float:
    return minutes_since_open(_bar_time(bar))


def _fmt(v, digits=2):
    return "n/a" if v is None else f"{v:,.{digits}f}"


class Style:
    key = "base"
    label = "Base"
    universe: list[str] = []
    size_pct = 15.0  # % of equity per new position
    add_fraction = 0.5  # an add is this share of a full position
    max_positions = 3
    max_entries_per_day = 2  # opens + adds
    tif = "gtc"
    entry_start: str | None = None  # HH:MM ET, None = app default
    entry_end: str | None = None
    intraday = False
    default_rules = ""

    def entry(self, sym: str, f: dict) -> tuple[bool, str, float]:
        return False, "", 0.0

    def add(self, sym: str, f: dict, pos: dict, meta: dict) -> tuple[bool, str]:
        return False, ""

    def soft_exit(self, sym: str, f: dict, pos: dict, meta: dict, now_et: datetime) -> str | None:
        return None

    def forced_exit(self, sym: str, f: dict | None, pos: dict, meta: dict, now_et: datetime) -> str | None:
        return None

    def stop_for(self, f: dict, price: float) -> float | None:
        return None

    def flatten_all_at(self) -> str | None:
        """HH:MM ET when every position is closed, or None."""
        return None


class TrendStyle(Style):
    key = "trend"
    label = "Trend follower"
    universe = [
        "SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV",
        "XLI", "XLY", "XLP", "XLU", "XLB", "SMH", "GLD", "TLT",
    ]
    size_pct = 20.0
    max_positions = 4
    max_entries_per_day = 2
    tif = "gtc"
    default_rules = (
        "Steady trend follower. Only buys ETFs that are in clear uptrends: price above a rising "
        "50-day average, and the 50-day above the 200-day. Prefers the strongest 3-month performers. "
        "Avoids buying anything stretched far above its 50-day average. Sits in cash when markets are "
        "choppy, adds once to a winner, and exits when the trend weakens. Patient: holding is often "
        "the right call."
    )

    def entry(self, sym, f):
        need = ("sma50", "sma200", "sma50_10d_ago", "atr14", "ret63_pct", "rsi14")
        if any(f.get(k) is None for k in need):
            return False, "", 0.0
        p = f["price"]
        ok = (
            p > f["sma50"] > f["sma200"]
            and f["sma50"] > f["sma50_10d_ago"]
            and f["ret63_pct"] > 0
            and (p - f["sma50"]) <= 3.0 * f["atr14"]
            and f["rsi14"] < 75
        )
        if not ok:
            return False, "", 0.0
        why = (
            f"Buy {sym}: uptrend (price {_fmt(p)} > 50-day {_fmt(f['sma50'])} > 200-day {_fmt(f['sma200'])}), "
            f"3-month return {_fmt(f['ret63_pct'], 1)}%, RSI14 {_fmt(f['rsi14'], 0)}"
        )
        return True, why, float(f["ret63_pct"])

    def add(self, sym, f, pos, meta):
        if meta.get("added") or not f.get("atr14") or not f.get("sma50"):
            return False, ""
        gain = f["price"] - pos["avg_entry_price"]
        if gain >= f["atr14"] and f["price"] > f["sma50"]:
            return True, (
                f"Add to {sym}: up {_fmt(pos['unrealized_plpc'], 1)}% (more than one ATR) and still above its 50-day average"
            )
        return False, ""

    def soft_exit(self, sym, f, pos, meta, now_et):
        if f.get("sma50") and f["price"] < f["sma50"]:
            return (
                f"Close {sym}: trend weakening, price {_fmt(f['price'])} slipped below the 50-day "
                f"{_fmt(f['sma50'])} ({_fmt(pos['unrealized_plpc'], 1)}% P&L)"
            )
        return None

    def forced_exit(self, sym, f, pos, meta, now_et):
        if f and f.get("sma50") and f.get("atr14") and f["price"] < f["sma50"] - 0.5 * f["atr14"]:
            return "trend broke (more than half an ATR below the 50-day average)"
        return None

    def stop_for(self, f, price):
        if not f.get("atr14"):
            return None
        return round(price - 2.5 * f["atr14"], 2)


class DipStyle(Style):
    key = "dip"
    label = "Dip buyer"
    universe = [
        "SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO", "JPM",
        "V", "MA", "UNH", "HD", "COST", "LLY", "XOM", "WMT", "PG", "KO",
    ]
    size_pct = 15.0
    max_positions = 4
    max_entries_per_day = 2
    tif = "gtc"
    default_rules = (
        "Patient dip buyer. Buys quality large caps and index funds that are in long-term uptrends "
        "(above their 200-day average) after a sharp short-term pullback (2-day RSI under 10, or price "
        "below the lower Bollinger band). Sells into the bounce, once price is back above its 5-day average "
        "or the 2-day RSI is over 70. Never chases strength, and skips dips where the long-term trend looks broken."
    )
    max_hold_days = 6

    def entry(self, sym, f):
        need = ("sma200", "rsi2", "bb_lower", "atr14")
        if any(f.get(k) is None for k in need):
            return False, "", 0.0
        p = f["price"]
        dipped = f["rsi2"] < 10 or p < f["bb_lower"]
        if not (p > f["sma200"] and dipped):
            return False, "", 0.0
        why = (
            f"Buy {sym}: dip in an uptrend (2-day RSI {_fmt(f['rsi2'], 1)}, price {_fmt(p)} vs lower band "
            f"{_fmt(f['bb_lower'])}, above 200-day {_fmt(f['sma200'])}), today {_fmt(f['day_change_pct'], 1)}%"
        )
        return True, why, 100.0 - float(f["rsi2"])

    def soft_exit(self, sym, f, pos, meta, now_et):
        bounced = (f.get("sma5") and f["price"] > f["sma5"]) or (f.get("rsi2") is not None and f["rsi2"] > 70)
        if bounced:
            return (
                f"Close {sym}: bounced (price {_fmt(f['price'])} vs 5-day {_fmt(f.get('sma5'))}, "
                f"2-day RSI {_fmt(f.get('rsi2'), 0)}), P&L {_fmt(pos['unrealized_plpc'], 1)}%"
            )
        return None

    def forced_exit(self, sym, f, pos, meta, now_et):
        opened = meta.get("opened_at")
        if opened:
            opened_day = datetime.fromisoformat(opened).astimezone(ET).date()
            if trading_days_between(opened_day, now_et.date()) >= self.max_hold_days:
                return f"time stop: held {self.max_hold_days} trading days without a bounce"
        return None

    def stop_for(self, f, price):
        if not f.get("atr14"):
            return None
        return round(price - 2.5 * f["atr14"], 2)


class MomentumStyle(Style):
    key = "momentum"
    label = "Momentum chaser"
    universe = [
        "NVDA", "TSLA", "AMD", "META", "AMZN", "NFLX", "AVGO",
        "MU", "PLTR", "COIN", "UBER", "SHOP", "CRWD",
    ]
    size_pct = 15.0
    max_positions = 3
    max_entries_per_day = 3
    tif = "day"
    entry_start = "09:45"
    entry_end = "15:00"
    intraday = True
    default_rules = (
        "Momentum chaser. Trades fast-moving, high-volume tech and growth stocks during the day. Buys "
        "breakouts above the opening range (first 15 minutes) when price is above VWAP, the stock is up at "
        "least 1.5% on the day and volume is running hot. Adds once to a winner. Gets out when price loses "
        "VWAP or the move stalls. Always flat by the close."
    )

    def entry(self, sym, f):
        need = ("or_high", "vwap", "rel_volume", "day_change_pct", "atr14")
        if any(f.get(k) is None for k in need):
            return False, "", 0.0
        p = f["price"]
        ok = p > f["or_high"] and p > f["vwap"] and f["day_change_pct"] >= 1.5 and f["rel_volume"] >= 1.3
        if not ok:
            return False, "", 0.0
        why = (
            f"Buy {sym}: breakout above opening range {_fmt(f['or_high'])}, price {_fmt(p)} above VWAP "
            f"{_fmt(f['vwap'])}, up {_fmt(f['day_change_pct'], 1)}% today, volume {_fmt(f['rel_volume'], 1)}x normal"
        )
        return True, why, float(f["rel_volume"]) * float(f["day_change_pct"])

    def add(self, sym, f, pos, meta):
        if meta.get("added") or f.get("day_high") is None:
            return False, ""
        if pos["unrealized_plpc"] >= 1.0 and f["price"] >= f["day_high"] * 0.998:
            return True, f"Add to {sym}: up {_fmt(pos['unrealized_plpc'], 1)}% and making new highs of the day"
        return False, ""

    def soft_exit(self, sym, f, pos, meta, now_et):
        if f.get("vwap") and f["price"] < f["vwap"]:
            return f"Close {sym}: lost VWAP ({_fmt(f['price'])} < {_fmt(f['vwap'])}), P&L {_fmt(pos['unrealized_plpc'], 1)}%"
        if (f.get("mins_since_high") or 0) >= 45 and pos["unrealized_plpc"] < 0.5:
            return f"Close {sym}: stalled, no new high for {int(f['mins_since_high'])} minutes, P&L {_fmt(pos['unrealized_plpc'], 1)}%"
        return None

    def stop_for(self, f, price):
        if not f.get("atr14"):
            return None
        return round(price - max(0.5 * f["atr14"], 0.01 * price), 2)

    def flatten_all_at(self):
        return "15:50"


STYLES = {s.key: s for s in (TrendStyle(), DipStyle(), MomentumStyle())}
