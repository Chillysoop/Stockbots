"""Plain-Python indicators. Inputs are lists ordered oldest -> newest."""
from __future__ import annotations

import math


def sma(values: list[float], n: int) -> float | None:
    if n <= 0 or len(values) < n:
        return None
    return sum(values[-n:]) / n


def sma_at(values: list[float], n: int, bars_ago: int) -> float | None:
    """SMA as it was `bars_ago` bars before the last one."""
    if bars_ago <= 0:
        return sma(values, n)
    if len(values) < n + bars_ago:
        return None
    window = values[-(n + bars_ago):-bars_ago]
    return sum(window) / n


def rsi(values: list[float], n: int) -> float | None:
    """Wilder's RSI. Needs at least n+1 values."""
    if len(values) < n + 1:
        return None
    gains = []
    losses = []
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:n]) / n
    avg_loss = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        avg_gain = (avg_gain * (n - 1) + gains[i]) / n
        avg_loss = (avg_loss * (n - 1) + losses[i]) / n
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def atr(highs: list[float], lows: list[float], closes: list[float], n: int) -> float | None:
    """Wilder's Average True Range."""
    if len(closes) < n + 1 or len(highs) != len(closes) or len(lows) != len(closes):
        return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    value = sum(trs[:n]) / n
    for i in range(n, len(trs)):
        value = (value * (n - 1) + trs[i]) / n
    return value


def bollinger(values: list[float], n: int = 20, k: float = 2.0) -> tuple[float, float, float] | None:
    """Returns (lower, middle, upper)."""
    if len(values) < n:
        return None
    window = values[-n:]
    mid = sum(window) / n
    var = sum((v - mid) ** 2 for v in window) / n
    sd = math.sqrt(var)
    return mid - k * sd, mid, mid + k * sd


def pct_change(new: float, old: float) -> float | None:
    if not old:
        return None
    return (new / old - 1.0) * 100.0


def session_vwap(bars: list[dict]) -> float | None:
    """VWAP from intraday bars that carry 'vw' (bar VWAP) and 'v' (volume)."""
    num = 0.0
    den = 0.0
    for b in bars:
        v = float(b.get("v") or 0)
        price = b.get("vw") or b.get("c")
        if v > 0 and price:
            num += float(price) * v
            den += v
    if den <= 0:
        return None
    return num / den


def r(value: float | None, digits: int = 2) -> float | None:
    """Round for display / prompts."""
    if value is None:
        return None
    return round(float(value), digits)
