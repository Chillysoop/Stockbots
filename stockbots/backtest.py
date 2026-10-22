"""Backtests: replay past market data through the bots' own styles and risk layer.

What it answers: how would each bot's *rules* have done with different position
sizes, daily loss stops and stop distances? It does not replay the AI. Asking
Claude or GPT about 2024 prices would leak hindsight (they've read the news), so
the backtest uses the code chooser instead: take every exit the rules suggest,
add to winners, and buy the top-ranked setups. Read results as "the rules' edge,
before the AI's judgment".

Run from the dashboard (Backtests), or on the server:
    docker compose exec app python -m stockbots.backtest --bot jev --months 24
"""
from __future__ import annotations

import argparse
import copy
import html
import json
import math
import os
import statistics
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from . import risk
from .alpaca import AlpacaClient, AlpacaError
from .config import BOTS, ET, Config, bot_def
from .indicators import pct_change
from .styles import STYLES, Move, _bar_time, daily_features, intraday_features, minutes_since_open

SLIPPAGE = {"trend": 0.0005, "dip": 0.0005, "momentum": 0.001}  # per side
TRAIN_SHARE = 0.7
START_CASH = 100_000.0


# ---- parameters ------------------------------------------------------------


@dataclass(frozen=True)
class Params:
    size_pct: float
    loss_stop_pct: float  # 0 = no daily loss stop
    stop_scale: float = 1.0  # 1 = the style's normal stop distance

    def label(self) -> str:
        loss = "off" if not self.loss_stop_pct else f"{self.loss_stop_pct:g}%"
        return f"size {self.size_pct:g}%, loss stop {loss}, stops x{self.stop_scale:g}"


def default_grid(style_key: str, cfg: Config) -> list[Params]:
    style = STYLES[style_key]
    sizes = sorted({10.0, 15.0, 20.0, 25.0, style.size_pct})
    losses = sorted({0.0, 2.0, 3.0, 5.0, cfg.daily_loss_stop_pct})
    scales = [0.75, 1.0, 1.5]
    return [Params(s, l, k) for s in sizes for l in losses for k in scales]


def current_params(style_key: str, cfg: Config) -> Params:
    return Params(STYLES[style_key].size_pct, cfg.daily_loss_stop_pct, 1.0)


# ---- one simulated account --------------------------------------------------


@dataclass
class Lot:
    qty: int
    price: float
    stop: float
    opened: datetime


@dataclass
class Trade:
    symbol: str
    opened: datetime
    closed: datetime
    entry: float
    exit: float
    qty: int
    reason: str

    @property
    def pnl(self) -> float:
        return (self.exit - self.entry) * self.qty

    @property
    def pnl_pct(self) -> float:
        return (self.exit / self.entry - 1.0) * 100.0 if self.entry else 0.0


class Sim:
    """A pretend account following one set of parameters, stepped by run_grid()."""

    def __init__(self, style_key: str, params: Params, cfg: Config, cash: float = START_CASH):
        self.params = params
        self.cfg = copy.copy(cfg)
        self.cfg.daily_loss_stop_pct = params.loss_stop_pct if params.loss_stop_pct > 0 else 1e9
        self.cfg.wash_sale_guard = False
        base = STYLES[style_key]
        self.style = copy.copy(base)
        self.style.size_pct = params.size_pct
        scale = params.stop_scale

        def scaled_stop(f, price, _base=base.stop_for, _scale=scale):
            s = _base(f, price)
            if s is None:
                return None
            return round(price - (price - s) * _scale, 2)

        self.style.stop_for = scaled_stop
        self.slip = SLIPPAGE.get(style_key, 0.0005)
        self.cash = cash
        self.lots: dict[str, list[Lot]] = {}
        self.added: set[str] = set()
        self.last_exit: dict[str, str] = {}
        self.last_order: dict[str, str] = {}
        self.trades: list[Trade] = []
        self.equity_curve: list[tuple[date, float]] = []
        self.invested_share: list[float] = []
        self.prices: dict[str, float] = {}
        self.last_equity = cash
        self.entries_today = 0

    # -- book keeping --

    def qty(self, sym: str) -> int:
        return sum(l.qty for l in self.lots.get(sym, []))

    def avg(self, sym: str) -> float:
        lots = self.lots.get(sym, [])
        q = sum(l.qty for l in lots)
        return sum(l.qty * l.price for l in lots) / q if q else 0.0

    def equity(self) -> float:
        return self.cash + sum(self.qty(s) * self.prices.get(s, self.avg(s)) for s in self.lots)

    def _sell_lot(self, sym: str, lot: Lot, price: float, when: datetime, reason: str) -> None:
        fill = price * (1 - self.slip)
        self.cash += lot.qty * fill
        self.trades.append(Trade(sym, lot.opened, when, lot.price, fill, lot.qty, reason))

    def close(self, sym: str, price: float, when: datetime, reason: str) -> None:
        for lot in self.lots.pop(sym, []):
            self._sell_lot(sym, lot, price, when, reason)
        self.added.discard(sym)
        self.last_exit[sym] = when.isoformat()

    def buy(self, sym: str, qty: int, price: float, stop: float, when: datetime, add: bool) -> None:
        fill = price * (1 + self.slip)
        self.cash -= qty * fill
        self.lots.setdefault(sym, []).append(Lot(qty, fill, stop, when))
        if add:
            self.added.add(sym)
        self.last_order[sym] = when.isoformat()
        self.entries_today += 1

    # -- the day --

    def start_day(self) -> None:
        self.last_equity = self.equity()
        self.entries_today = 0

    def check_stops(self, bars: dict[str, dict], when: datetime) -> None:
        """Stops sit at the broker: they trigger on the bar's low, filling at the stop (or the open, on a gap)."""
        for sym in list(self.lots):
            bar = bars.get(sym)
            if not bar:
                continue
            keep = []
            for lot in self.lots[sym]:
                if float(bar["l"]) <= lot.stop:
                    self._sell_lot(sym, lot, min(float(bar["o"]), lot.stop), when, "stop")
                else:
                    keep.append(lot)
            if keep:
                self.lots[sym] = keep
            else:
                del self.lots[sym]
                self.added.discard(sym)
                self.last_exit[sym] = when.isoformat()

    def _positions(self) -> dict:
        out = {}
        for sym in self.lots:
            q = self.qty(sym)
            px = self.prices.get(sym, self.avg(sym))
            avg = self.avg(sym)
            out[sym] = {"symbol": sym, "qty": q, "avg_entry_price": avg, "current_price": px,
                        "market_value": q * px, "unrealized_plpc": (px / avg - 1) * 100 if avg else 0.0}
        return out

    def _meta(self) -> dict:
        return {s: {"opened_at": lots[0].opened.isoformat(), "added": s in self.added,
                    "stop_price": min(l.stop for l in lots)} for s, lots in self.lots.items()}

    def decide(self, now_et: datetime, feats: dict[str, dict], minutes_to_close: float) -> None:
        """Rules chooser: forced exits, then suggested exits, then adds, then the best setups."""
        style = self.style
        positions = self._positions()
        meta = self._meta()
        for sym, pos in positions.items():
            f = feats.get(sym)
            why = style.forced_exit(sym, f, pos, meta.get(sym) or {}, now_et)
            if not why and f:
                why = style.soft_exit(sym, f, pos, meta.get(sym) or {}, now_et)
            if why and sym in self.prices:
                self.close(sym, self.prices[sym], now_et, "rules")
        while True:
            positions = self._positions()
            eq = self.equity()
            ctx = risk.RiskContext(
                cfg=self.cfg, style=style, now_et=now_et, market_open=True,
                account={"equity": eq, "last_equity": self.last_equity, "cash": self.cash,
                         "buying_power": max(self.cash, 0.0)},
                positions=positions, meta=self._meta(), entries_today=self.entries_today, paused=False,
                features=feats, last_exit=self.last_exit, last_order=self.last_order,
                minutes_to_close=minutes_to_close,
            )
            if risk.entry_blocker(ctx):
                return
            moves: list[Move] = []
            for sym, pos in positions.items():
                f = feats.get(sym)
                if f:
                    ok, why = style.add(sym, f, pos, ctx.meta.get(sym) or {})
                    if ok:
                        moves.append(Move(id=f"add:{sym}", kind="add", symbol=sym, text=why, score=1e9))
            for sym, f in feats.items():
                if sym in positions or sym not in style.universe:
                    continue
                ok, why, score = style.entry(sym, f)
                if ok:
                    moves.append(Move(id=f"open:{sym}", kind="open", symbol=sym, text=why, score=score))
            moves.sort(key=lambda m: -m.score)
            done = False
            for m in moves:
                v = risk.check(m, ctx)
                if v.ok:
                    self.buy(m.symbol, v.qty, feats[m.symbol]["price"], v.stop, now_et, add=m.kind == "add")
                    done = True
                    break
            if not done:
                return

    def end_day(self, day: date) -> None:
        eq = self.equity()
        self.equity_curve.append((day, eq))
        invested = eq - self.cash
        self.invested_share.append(invested / eq if eq > 0 else 0.0)


# ---- data -------------------------------------------------------------------


@dataclass
class Data:
    style_key: str
    days: list[date]
    daily: dict[str, list[dict]]
    intraday: dict[str, list[dict]] = field(default_factory=dict)
    feed: str = "sip"

    def benchmark(self) -> list[tuple[date, float]]:
        spy = {_bar_time(b).date(): float(b["c"]) for b in self.daily.get("SPY", [])}
        return [(d, spy[d]) for d in self.days if d in spy]


def load_data(client: AlpacaClient, style_key: str, start: date, end: date, progress=None) -> Data:
    """Fetch completed days only (the free data plan allows SIP data older than 15 minutes)."""
    style = STYLES[style_key]
    symbols = sorted(set(style.universe) | {"SPY"})
    warm = datetime(start.year, start.month, start.day, tzinfo=ET) - timedelta(days=430)
    end_dt = datetime(end.year, end.month, end.day, tzinfo=ET) + timedelta(days=1)
    feed = "sip"
    if progress:
        progress("Downloading daily prices")
    try:
        daily = client.bars(symbols, "1Day", warm, end=end_dt, feed=feed, adjustment="all", max_pages=100)
    except AlpacaError:
        feed = "iex"
        daily = client.bars(symbols, "1Day", warm, end=end_dt, feed=feed, adjustment="all", max_pages=100)
    days = sorted({_bar_time(b).date() for b in daily.get("SPY", [])})
    days = [d for d in days if start <= d <= end]
    intraday: dict[str, list[dict]] = {}
    if style.intraday:
        if progress:
            progress("Downloading 5-minute prices (this is the slow part)")
        start_dt = datetime(start.year, start.month, start.day, tzinfo=ET)
        intraday = client.bars(style.universe, "5Min", start_dt, end=end_dt, feed=feed, adjustment="all",
                               max_pages=400)
    return Data(style_key, days, daily, intraday, feed)


# ---- running ----------------------------------------------------------------


def run_grid(data: Data, params_list: list[Params], cfg: Config, progress=None) -> list[Sim]:
    """Steps every parameter set through the same days together, computing features once."""
    style = STYLES[data.style_key]
    sims = [Sim(data.style_key, p, cfg) for p in params_list]
    by_sym_index = {}
    for sym, bars in data.daily.items():
        by_sym_index[sym] = {_bar_time(b).date(): i for i, b in enumerate(bars)}
    intraday_by_day: dict[date, dict[str, list[dict]]] = {}
    for sym, bars in data.intraday.items():
        for b in bars:
            t = _bar_time(b)
            if 0 <= minutes_since_open(t) < 390:
                intraday_by_day.setdefault(t.date(), {}).setdefault(sym, []).append(b)
    symbols = sorted(set(style.universe) | {"SPY"})
    total = len(data.days)
    for n, day in enumerate(data.days):
        if progress and n % 20 == 0:
            progress(f"Simulating {day.isoformat()} ({n + 1} of {total} days)")
        for sim in sims:
            sim.start_day()
        today_bars = {}
        for sym in symbols:
            i = by_sym_index.get(sym, {}).get(day)
            if i is not None:
                today_bars[sym] = data.daily[sym][i]
        if not style.intraday:
            # Decide near the close using the day's close as the live price.
            now_et = datetime(day.year, day.month, day.day, 15, 40, tzinfo=ET)
            feats = {}
            for sym in symbols:
                i = by_sym_index.get(sym, {}).get(day)
                if i is None:
                    continue
                bars = data.daily[sym][max(0, i - 300): i + 1]
                f = daily_features(bars, {"price": float(bars[-1]["c"])}, day)
                if f:
                    feats[sym] = f
            stop_time = datetime(day.year, day.month, day.day, 12, 0, tzinfo=ET)
            for sim in sims:
                sim.check_stops(today_bars, stop_time)  # stops trade during the day, before the close
                for sym, b in today_bars.items():
                    sim.prices[sym] = float(b["c"])
                sim.decide(now_et, feats, minutes_to_close=20)
                sim.end_day(day)
            continue

        # Intraday style: step through 5-minute bars.
        day_bars = intraday_by_day.get(day, {})
        base = {}
        for sym in style.universe:
            i = by_sym_index.get(sym, {}).get(day)
            if i is None:
                continue
            prev = data.daily[sym][max(0, i - 300): i]
            if not prev:
                continue
            f0 = daily_features(prev + [data.daily[sym][i]], {"price": float(prev[-1]["c"])}, day)
            if f0:
                base[sym] = f0
        times = sorted({_bar_time(b) for rows in day_bars.values() for b in rows})
        idx = {sym: 0 for sym in day_bars}
        so_far: dict[str, list[dict]] = {sym: [] for sym in day_bars}
        feats: dict[str, dict] = {}
        for t in times:
            step_bars = {}
            for sym, rows in day_bars.items():
                k = idx[sym]
                if k < len(rows) and _bar_time(rows[k]) == t:
                    so_far[sym].append(rows[k])
                    step_bars[sym] = rows[k]
                    idx[sym] = k + 1
            now_et = t + timedelta(minutes=5)
            for sym, b in step_bars.items():
                if sym not in base:
                    continue
                f = dict(base[sym])
                price = float(b["c"])
                f["price"] = price
                f["day_change_pct"] = pct_change(price, f["prev_close"])
                feats[sym] = intraday_features(f, so_far[sym], now_et)
            mins_left = (now_et.replace(hour=16, minute=0) - now_et).total_seconds() / 60
            flatten = style.flatten_all_at() and now_et.strftime("%H:%M") >= style.flatten_all_at()
            for sim in sims:
                sim.check_stops(step_bars, now_et)
                for sym, b in step_bars.items():
                    sim.prices[sym] = float(b["c"])
                if flatten:
                    for sym in list(sim.lots):
                        sim.close(sym, sim.prices.get(sym, sim.avg(sym)), now_et, "end of day")
                else:
                    sim.decide(now_et, feats, minutes_to_close=mins_left)
        for sim in sims:
            for sym in list(sim.lots):  # nothing is held overnight by this style
                sim.close(sym, sim.prices.get(sym, sim.avg(sym)), datetime(day.year, day.month, day.day, 16, 0, tzinfo=ET), "end of day")
            sim.end_day(day)
    return sims


# ---- scoring ----------------------------------------------------------------


def metrics(curve: list[tuple[date, float]], trades: list[Trade] | None = None,
            invested: list[float] | None = None) -> dict:
    if len(curve) < 2:
        return {"return_pct": 0.0, "cagr_pct": 0.0, "max_drawdown_pct": 0.0, "sharpe": 0.0, "trades": 0,
                "win_rate_pct": 0.0, "avg_trade_pct": 0.0, "exposure_pct": 0.0}
    values = [v for _, v in curve]
    rets = [values[i] / values[i - 1] - 1 for i in range(1, len(values)) if values[i - 1] > 0]
    total = values[-1] / values[0] - 1
    years = max((curve[-1][0] - curve[0][0]).days / 365.25, 1 / 252)
    cagr = (values[-1] / values[0]) ** (1 / years) - 1 if values[0] > 0 and values[-1] > 0 else -1.0
    peak = values[0]
    mdd = 0.0
    for v in values:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    sd = statistics.pstdev(rets) if len(rets) > 1 else 0.0
    sharpe = (statistics.mean(rets) / sd * math.sqrt(252)) if sd > 0 else 0.0
    out = {
        "return_pct": total * 100,
        "cagr_pct": cagr * 100,
        "max_drawdown_pct": mdd * 100,
        "sharpe": sharpe,
    }
    if trades is not None:
        wins = [t for t in trades if t.pnl > 0]
        out["trades"] = len(trades)
        out["win_rate_pct"] = len(wins) / len(trades) * 100 if trades else 0.0
        out["avg_trade_pct"] = statistics.mean([t.pnl_pct for t in trades]) if trades else 0.0
    if invested:
        out["exposure_pct"] = statistics.mean(invested) * 100
    return out


def split(curve, share=TRAIN_SHARE):
    cut = max(int(len(curve) * share), 2)
    return curve[:cut], curve[cut - 1:]


def summarize(sims: list[Sim], data: Data, current: Params) -> dict:
    bench = data.benchmark()
    btrain, btest = split(bench)
    rows = []
    for sim in sims:
        train, test = split(sim.equity_curve)
        cut_day = train[-1][0] if train else None
        rows.append({
            "params": sim.params.label(),
            "size_pct": sim.params.size_pct,
            "loss_stop_pct": sim.params.loss_stop_pct,
            "stop_scale": sim.params.stop_scale,
            "current": sim.params == current,
            "all": metrics(sim.equity_curve, sim.trades, sim.invested_share),
            "train": metrics(train, [t for t in sim.trades if cut_day and t.closed.date() <= cut_day]),
            "test": metrics(test, [t for t in sim.trades if cut_day and t.closed.date() > cut_day]),
        })
    rows.sort(key=lambda r: -r["train"]["sharpe"])
    cur = next((s for s in sims if s.params == current), sims[0])
    return {
        "style": data.style_key,
        "start": data.days[0].isoformat() if data.days else None,
        "end": data.days[-1].isoformat() if data.days else None,
        "days": len(data.days),
        "feed": data.feed,
        "train_end": split(cur.equity_curve)[0][-1][0].isoformat() if len(cur.equity_curve) > 2 else None,
        "benchmark": {"all": metrics(bench), "train": metrics(btrain), "test": metrics(btest)},
        "rows": rows,
        "current": {
            "params": cur.params.label(),
            "curve": [(d.isoformat(), round(v, 2)) for d, v in cur.equity_curve],
            "bench_curve": [(d.isoformat(), v) for d, v in bench],
            "recent_trades": [
                {"symbol": t.symbol, "opened": t.opened.date().isoformat(), "closed": t.closed.date().isoformat(),
                 "pnl_pct": round(t.pnl_pct, 2), "reason": t.reason}
                for t in cur.trades[-15:]
            ],
        },
    }


# ---- report -----------------------------------------------------------------


def _f(v, digits=1, suffix="%", sign=True):
    if v is None:
        return "–"
    s = f"{v:+.{digits}f}" if sign else f"{v:.{digits}f}"
    return s + suffix


def _chart(cur: list, bench: list) -> str:
    """An inline SVG of the current settings vs holding SPY, in % from the start."""
    if len(cur) < 2:
        return "<p class='muted'>Not enough days to draw a chart.</p>"
    # Lines only inside the SVG (it scales to any width); every label is HTML, so phones can read it.
    W, H, pl, pr, pt, pb = 900, 300, 4, 4, 8, 8
    s1 = [(d, (v / cur[0][1] - 1) * 100) for d, v in cur]
    s2 = [(d, (v / bench[0][1] - 1) * 100) for d, v in bench] if len(bench) > 1 else []
    allv = [v for _, v in s1 + s2] + [0.0]
    lo, hi = min(allv), max(allv)
    pad = max(hi - lo, 1) * 0.1
    lo, hi = lo - pad, hi + pad
    days = [d for d, _ in s1]
    pos = {d: i for i, d in enumerate(days)}
    x = lambda d: pl + pos.get(d, 0) / max(len(days) - 1, 1) * (W - pl - pr)
    y = lambda v: pt + (1 - (v - lo) / (hi - lo)) * (H - pt - pb)

    def path(series):
        pts = [(x(d), y(v)) for d, v in series if d in pos]
        return "M" + " L".join(f"{a:.1f},{b:.1f}" for a, b in pts)

    parts = [f'<svg viewBox="0 0 {W} {H}" preserveAspectRatio="none" role="img" '
             f'aria-label="Backtest result versus holding SPY">']
    parts.append(f'<line x1="{pl}" x2="{W - pr}" y1="{y(0):.1f}" y2="{y(0):.1f}" stroke="var(--line)" '
                 f'stroke-width="1.4" vector-effect="non-scaling-stroke"/>')
    if s2:
        parts.append(f'<path d="{path(s2)}" fill="none" stroke="var(--ink-3)" stroke-width="2" '
                     f'stroke-dasharray="5 4" vector-effect="non-scaling-stroke"/>')
    parts.append(f'<path d="{path(s1)}" fill="none" stroke="var(--accent)" stroke-width="3" '
                 f'vector-effect="non-scaling-stroke"/>')
    parts.append("</svg>")
    legend = (f'<p class="legend"><span class="item"><span class="sw bot"></span>This bot {s1[-1][1]:+.1f}%</span>'
              + (f'<span class="item"><span class="sw spy"></span>Holding SPY {s2[-1][1]:+.1f}%</span>' if s2 else "")
              + f'<span class="range">From {html.escape(str(days[0]))} to {html.escape(str(days[-1]))}, '
                f'scale {lo:+.0f}% to {hi:+.0f}%</span></p>')
    return legend + "".join(parts)


def render_html(summary: dict, bot_name: str) -> str:
    e = html.escape
    cur = next((r for r in summary["rows"] if r["current"]), summary["rows"][0])
    bench = summary["benchmark"]
    best = summary["rows"][0]
    curve = [(datetime.fromisoformat(d).date(), v) for d, v in summary["current"]["curve"]]
    bcurve = [(datetime.fromisoformat(d).date(), v) for d, v in summary["current"]["bench_curve"]]
    beat = cur["all"]["return_pct"] - bench["all"]["return_pct"]
    headline = (f"Following its rules, {e(bot_name)} would have made {_f(cur['all']['return_pct'])}, "
                f"{'ahead of' if beat >= 0 else 'behind'} holding SPY by {abs(beat):.1f} points")
    def row(r):
        cls = ' class="current"' if r["current"] else ""
        return (
            f"<tr{cls}><td>{e(r['params'])}{' (now)' if r['current'] else ''}</td>"
            f"<td>{_f(r['train']['return_pct'])}</td><td>{r['train']['sharpe']:.2f}</td>"
            f"<td>{_f(r['test']['return_pct'])}</td><td>{_f(r['test']['max_drawdown_pct'])}</td>"
            f"<td>{_f(r['all']['return_pct'])}</td><td>{_f(r['all']['max_drawdown_pct'])}</td>"
            f"<td>{r['all'].get('trades', 0)}</td><td>{_f(r['all'].get('win_rate_pct'), 0, '%', False)}</td></tr>")

    top = summary["rows"][:10]
    if not any(r["current"] for r in top):
        top = top + [cur]
    rows_html = [row(r) for r in top]
    all_rows_html = "".join(row(r) for r in summary["rows"])
    trades_html = "".join(
        f"<tr><td>{e(t['symbol'])}</td><td>{e(t['opened'])}</td><td>{e(t['closed'])}</td>"
        f"<td class=\"{'gain' if t['pnl_pct'] > 0 else 'loss'}\">{_f(t['pnl_pct'])}</td><td>{e(t['reason'])}</td></tr>"
        for t in reversed(summary["current"]["recent_trades"]))
    same_as_now = best["current"]
    head = ("<tr><th>Settings</th><th>First period</th><th>Sharpe</th><th>Test period</th><th>Test drawdown</th>"
            "<th>Whole run</th><th>Drawdown</th><th>Trades</th><th>Winners</th></tr>")
    spy_row = (f"<tr><td>Hold SPY</td><td>{_f(bench['train']['return_pct'])}</td><td>{bench['train']['sharpe']:.2f}</td>"
               f"<td>{_f(bench['test']['return_pct'])}</td><td>{_f(bench['test']['max_drawdown_pct'])}</td>"
               f"<td>{_f(bench['all']['return_pct'])}</td><td>{_f(bench['all']['max_drawdown_pct'])}</td><td>–</td><td>–</td></tr>")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Backtest: {e(bot_name)}</title>
<style>
:root {{ --bg:#e8edf2; --surface:#f8fafc; --ink:#1a2533; --ink-2:#4a5768; --ink-3:#7a8696; --line:#d3dbe4; --gain:#1d7f47; --loss:#bf3a2f; --accent:#2f63c9; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#111923; --surface:#18222e; --ink:#e5ebf1; --ink-2:#aab5c2; --ink-3:#7d8998; --line:#2c3947; --gain:#4cc27f; --loss:#f07a6e; --accent:#6f9bff; }} }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 "Public Sans", system-ui, sans-serif; font-variant-numeric: tabular-nums; }}
main {{ max-width: 1000px; margin: 0 auto; padding: 24px 18px 50px; }}
h1 {{ font-size: clamp(24px, 4vw, 34px); line-height:1.1; letter-spacing:-0.02em; margin: 6px 0 10px; }}
h2 {{ font-size: 19px; margin: 0 0 6px; }}
.panel {{ background: var(--surface); border:1px solid var(--line); border-radius: 14px; padding: 16px 18px; margin: 14px 0; overflow-x: auto; }}
.muted {{ color: var(--ink-2); }}
svg {{ width:100%; height:auto; overflow: visible; }}
table {{ border-collapse: collapse; width: 100%; font-size: 13.5px; }}
th, td {{ text-align: right; padding: 5px 6px; border-top: 1px solid var(--line); white-space: nowrap; }}
th:first-child, td:first-child {{ text-align: left; }}
th {{ color: var(--ink-3); font-weight: 600; border-top: 0; }}
tr.current td {{ font-weight: 700; background: color-mix(in srgb, var(--accent) 10%, transparent); }}
.gain {{ color: var(--gain); }} .loss {{ color: var(--loss); }}
ul {{ padding-left: 20px; }}
details summary {{ cursor: pointer; margin-top: 10px; font-weight: 600; color: var(--ink-2); }}
svg {{ height: 220px; }}
.legend {{ display: flex; flex-wrap: wrap; gap: 6px 16px; align-items: center; margin: 0 0 10px; font-weight: 600; }}
.legend .sw {{ display: inline-block; width: 22px; height: 0; border-top: 3px solid var(--accent); margin-right: 6px; vertical-align: middle; }}
.legend .sw.spy {{ border-top: 2px dashed var(--ink-3); }}
.legend .range {{ font-weight: 400; color: var(--ink-3); font-size: 13px; width: 100%; }}
.legend .item {{ white-space: nowrap; }}
a {{ color: inherit; }}
</style></head><body><main>
<p class="muted"><a href="/">Back to the dashboard</a></p>
<h1>{headline}</h1>
<p class="muted">{e(summary['start'] or '')} to {e(summary['end'] or '')}, {summary['days']} trading days, $100,000 start, current settings ({e(cur['params'])}). Price data: {e(summary['feed'].upper())}.</p>

<div class="panel">{_chart(curve, bcurve)}
<p class="muted">Max drawdown {_f(cur['all']['max_drawdown_pct'])} (SPY {_f(bench['all']['max_drawdown_pct'])}). Sharpe {cur['all']['sharpe']:.2f} (SPY {bench['all']['sharpe']:.2f}). {cur['all'].get('trades', 0)} trades, {_f(cur['all'].get('win_rate_pct'), 0, '%', False)} winners, invested {_f(cur['all'].get('exposure_pct'), 0, '%', False)} of the time on average.</p></div>

<div class="panel">
<h2>Other settings</h2>
<p class="muted">Sorted by how well each did in the first {int(TRAIN_SHARE * 100)}% of the period (through {e(summary.get('train_end') or '')}). The later "test" period wasn't used to rank anything, so it's the honest check. {"Your current settings rank first." if same_as_now else "If a row only looks good in the first period, it's probably luck."}</p>
<table><thead>{head}</thead>
<tbody>{''.join(rows_html)}
{spy_row}
</tbody></table>
<details><summary>Show all {len(summary['rows'])} settings</summary>
<table><thead>{head}</thead><tbody>{all_rows_html}{spy_row}</tbody></table></details></div>

<div class="panel"><h2>Latest trades with current settings</h2>
<table><thead><tr><th>Stock</th><th>Bought</th><th>Sold</th><th>Result</th><th>Why sold</th></tr></thead><tbody>{trades_html or '<tr><td colspan="5">No trades.</td></tr>'}</tbody></table></div>

<div class="panel"><h2>How to read this</h2><ul>
<li>This replays the bot's rules, sizes and stops, not its AI. Asking an AI about past prices leaks hindsight, so a simple code chooser takes every setup the rules rank highest. The real bot's AI may skip some of these trades.</li>
<li>Trades fill at the close (or at the stop) with {SLIPPAGE.get(summary['style'], 0.0005) * 100:.2f}% slippage each way, no commissions. Swing bots decide once a day near the close; the live bot checks every minute.</li>
<li>The stock lists are today's large, liquid names, which flatters any test that looks backward (survivorship bias).</li>
<li>Past results don't predict future ones. This isn't financial advice.</li>
</ul></div>
</main></body></html>"""


# ---- jobs (dashboard) -------------------------------------------------------


class BacktestJobs:
    """Runs one backtest at a time in the background and keeps the reports in /data/backtests."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.dir = os.path.join(cfg.data_dir, "backtests")
        os.makedirs(self.dir, exist_ok=True)
        self.lock = threading.Lock()
        self.current: dict | None = None

    def list(self) -> dict:
        items = []
        for name in sorted(os.listdir(self.dir), reverse=True):
            if name.endswith(".json"):
                try:
                    with open(os.path.join(self.dir, name), "r", encoding="utf-8") as fh:
                        items.append(json.load(fh))
                except (OSError, ValueError):
                    continue
        with self.lock:
            running = dict(self.current) if self.current else None
        return {"running": running, "reports": items[:20]}

    def start(self, bot_id: str, months: int, client: AlpacaClient, bot_name: str) -> str:
        with self.lock:
            if self.current:
                raise RuntimeError("A backtest is already running.")
            job_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + bot_id
            self.current = {"id": job_id, "bot": bot_id, "name": bot_name, "months": months,
                            "status": "Starting", "started": time.time()}
        threading.Thread(target=self._run, args=(job_id, bot_id, months, client, bot_name), daemon=True).start()
        return job_id

    def _progress(self, text: str) -> None:
        with self.lock:
            if self.current:
                self.current["status"] = text

    def _run(self, job_id, bot_id, months, client, bot_name) -> None:
        meta = {"id": job_id, "bot": bot_id, "name": bot_name, "months": months,
                "created": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        try:
            summary = run_backtest(bot_id, months, client, self.cfg, progress=self._progress)
            report = render_html(summary, bot_name)
            with open(os.path.join(self.dir, job_id + ".html"), "w", encoding="utf-8") as fh:
                fh.write(report)
            cur = next(r for r in summary["rows"] if r["current"])
            meta.update({"ok": True, "start": summary["start"], "end": summary["end"],
                         "return_pct": round(cur["all"]["return_pct"], 2),
                         "spy_return_pct": round(summary["benchmark"]["all"]["return_pct"], 2),
                         "max_drawdown_pct": round(cur["all"]["max_drawdown_pct"], 2),
                         "best_params": summary["rows"][0]["params"]})
        except Exception as exc:  # report the failure on the dashboard
            meta.update({"ok": False, "error": f"{exc.__class__.__name__}: {str(exc)[:200]}"})
        with open(os.path.join(self.dir, job_id + ".json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh)
        with self.lock:
            self.current = None

    def report_path(self, job_id: str) -> str | None:
        if not job_id.replace("-", "").isalnum():
            return None
        path = os.path.join(self.dir, job_id + ".html")
        return path if os.path.exists(path) else None


def run_backtest(bot_id: str, months: int, client: AlpacaClient, cfg: Config, progress=None,
                 grid: list[Params] | None = None) -> dict:
    style_key = bot_def(bot_id)["style"]
    end = datetime.now(ET).date() - timedelta(days=1)
    start = end - timedelta(days=int(months * 30.44))
    data = load_data(client, style_key, start, end, progress=progress)
    if len(data.days) < 30:
        raise RuntimeError("Not enough price history came back to run a backtest.")
    grid = grid or default_grid(style_key, cfg)
    current = current_params(style_key, cfg)
    if current not in grid:
        grid.append(current)
    sims = run_grid(data, grid, cfg, progress=progress)
    if progress:
        progress("Writing the report")
    return summarize(sims, data, current)


def main() -> None:
    from .settings_store import SettingsStore

    parser = argparse.ArgumentParser(description="Backtest a bot's rules on past prices.")
    parser.add_argument("--bot", choices=[b["id"] for b in BOTS], required=True)
    parser.add_argument("--months", type=int, default=24)
    args = parser.parse_args()
    cfg = Config()
    keys = SettingsStore(cfg.settings_path).bot(args.bot)
    if not keys.get("alpaca_key"):
        raise SystemExit("This bot has no Alpaca keys yet: finish Setup first.")
    client = AlpacaClient(keys["alpaca_key"], keys["alpaca_secret"], live=False)
    summary = run_backtest(args.bot, args.months, client, cfg, progress=lambda s: print(s, flush=True))
    out = os.path.join(cfg.data_dir, "backtests")
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{args.bot}.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(render_html(summary, bot_def(args.bot)["default_name"]))
    cur = next(r for r in summary["rows"] if r["current"])
    print(f"Current settings: {cur['all']['return_pct']:+.1f}% vs SPY {summary['benchmark']['all']['return_pct']:+.1f}%")
    print(f"Report: {path}")


if __name__ == "__main__":
    main()
