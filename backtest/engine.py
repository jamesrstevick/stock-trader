"""
Daily OHLC portfolio simulator — loose stand-in for the live trail/hard-stop bot.

Approximations (intentional):
- One buy + sell pass per trading day (not 15-minute checks / broker STOP_LIMIT).
- Peak from day's High; stop fill if Low <= stop (pessimistic path stress).
- Frozen universe always treated as on-watchlist (on-list trail buffer / hard stop).
- Min-hold: cannot sell on the same calendar trading day as the buy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd


@dataclass
class Position:
    ticker: str
    shares: int
    purchase: float
    purchased_day: date
    peak_gain_pct: float
    stop_gain_pct: float
    trail_active: bool


@dataclass
class SimConfig:
    starting_cash: float = 50000.0
    order_amount: float = 1000.0
    min_cash: float = 15000.0
    trail_activate_pct: float = 0.10
    trail_buffer_pct: float = 0.05
    hard_stop_pct: float = -0.10


@dataclass
class SimResult:
    start: date
    end: date
    starting_cash: float
    ending_equity: float
    algo_return_pct: float
    spy_return_pct: float
    excess_pct: float
    algo_max_dd_pct: float
    spy_max_dd_pct: float
    trade_count: int
    equity_curve: List[Dict[str, Any]] = field(default_factory=list)


def compute_trail_state_on_list(
    purchase: float,
    price: float,
    peak_gain: Optional[float],
    stop_gain: Optional[float],
    trail_active: bool,
    cfg: SimConfig,
) -> Dict[str, Any]:
    """
    Mirror stock_trader.compute_trail_state_for_position with on_watchlist=True.
    Kept local so the backtest does not depend on the live DB watchlist.
    """
    activate = float(cfg.trail_activate_pct)
    buffer = float(cfg.trail_buffer_pct)
    hard = float(cfg.hard_stop_pct)

    gain = (price - purchase) / purchase
    peak = float(peak_gain) if peak_gain is not None else gain
    if gain > peak:
        peak = gain
    implied_peak_price = purchase * (1.0 + peak)
    if peak > activate and implied_peak_price > price * 1.2 and gain < activate:
        peak = gain
        stop_gain = None
        trail_active = False

    active = bool(trail_active) or peak >= activate
    new_stop = float(stop_gain) if stop_gain is not None else None
    if active:
        candidate = peak - buffer
        if candidate < 0:
            candidate = 0.0
        if new_stop is None or candidate > new_stop:
            new_stop = candidate
        if purchase * (1.0 + float(new_stop)) > price * 1.2 and gain < activate:
            peak = gain
            active = False
            new_stop = hard
        stop_kind = 'trail' if active else 'hard'
    else:
        new_stop = hard
        stop_kind = 'hard'

    stop_price = purchase * (1.0 + float(new_stop))
    return {
        'gain_pct': gain,
        'peak_gain_pct': peak,
        'stop_gain_pct': new_stop,
        'stop_price': stop_price,
        'stop_kind': stop_kind,
        'trail_active': active,
        'breached': gain <= float(new_stop),
    }


def max_drawdown_pct(equities: Sequence[float]) -> float:
    """Max peak-to-trough drawdown as a negative fraction (e.g. -0.25)."""
    if not equities:
        return 0.0
    peak = equities[0]
    worst = 0.0
    for e in equities:
        if e > peak:
            peak = e
        if peak > 0:
            dd = (e - peak) / peak
            if dd < worst:
                worst = dd
    return float(worst)


def _bar(ohlc: Dict[str, pd.DataFrame], ticker: str, day: date) -> Optional[pd.Series]:
    df = ohlc.get(ticker)
    if df is None or df.empty:
        return None
    try:
        row = df.loc[pd.Timestamp(day)]
    except KeyError:
        return None
    if isinstance(row, pd.DataFrame):
        row = row.iloc[0]
    return row


def _trading_days(
    ohlc: Dict[str, pd.DataFrame],
    start: date,
    end: date,
    spy_ticker: str = 'SPY',
) -> List[date]:
    spy = ohlc.get(spy_ticker)
    if spy is None or spy.empty:
        # Fall back to union of all calendars
        idx = None
        for df in ohlc.values():
            if df is None or df.empty:
                continue
            idx = df.index if idx is None else idx.union(df.index)
        if idx is None:
            return []
        days = [ts.date() if hasattr(ts, 'date') else pd.Timestamp(ts).date() for ts in idx]
    else:
        days = [ts.date() if hasattr(ts, 'date') else pd.Timestamp(ts).date() for ts in spy.index]
    return [d for d in days if start <= d <= end]


def run_daily_sim(
    ranked_tickers: Sequence[str],
    ohlc: Dict[str, pd.DataFrame],
    start: date,
    end: date,
    cfg: Optional[SimConfig] = None,
    spy_ticker: str = 'SPY',
) -> SimResult:
    """
    Simulate from `start` through `end` (inclusive of trading days in range).

    ohlc: ticker -> DataFrame indexed by Timestamp date with Open/High/Low/Close.
    ranked_tickers: buy preference order (e.g. analyst upside rank).
    """
    if cfg is None:
        cfg = SimConfig()

    days = _trading_days(ohlc, start, end, spy_ticker=spy_ticker)
    if not days:
        return SimResult(
            start=start,
            end=end,
            starting_cash=cfg.starting_cash,
            ending_equity=cfg.starting_cash,
            algo_return_pct=0.0,
            spy_return_pct=0.0,
            excess_pct=0.0,
            algo_max_dd_pct=0.0,
            spy_max_dd_pct=0.0,
            trade_count=0,
        )

    cash = float(cfg.starting_cash)
    positions = {}  # type: Dict[str, Position]
    trade_count = 0
    equity_curve = []  # type: List[Dict[str, Any]]

    # SPY buy-and-hold: full notional at first available close ratio from start
    spy_start_px = None  # type: Optional[float]
    for d0 in days:
        bar0 = _bar(ohlc, spy_ticker, d0)
        if bar0 is None:
            continue
        open0 = float(bar0.get('Open') or 0)
        close0 = float(bar0.get('Close') or 0)
        spy_start_px = open0 if open0 > 0 else close0
        if spy_start_px and spy_start_px > 0:
            break
    if not spy_start_px or spy_start_px <= 0:
        spy_start_px = 1.0

    for day in days:
        # --- Buys at Open ---
        for ticker in ranked_tickers:
            if ticker in positions:
                continue
            bar = _bar(ohlc, ticker, day)
            if bar is None:
                continue
            open_px = float(bar.get('Open') or 0)
            if open_px <= 0:
                continue
            shares = int(float(cfg.order_amount) // open_px)
            if shares < 1:
                continue
            cost = shares * open_px
            if cash - cost < float(cfg.min_cash):
                continue
            cash -= cost
            positions[ticker] = Position(
                ticker=ticker,
                shares=shares,
                purchase=open_px,
                purchased_day=day,
                peak_gain_pct=0.0,
                stop_gain_pct=float(cfg.hard_stop_pct),
                trail_active=False,
            )
            trade_count += 1

        # --- Manage positions: High updates peak; Low may stop out; else Close mark ---
        to_close = []  # type: List[Tuple[str, float]]
        for ticker, pos in list(positions.items()):
            bar = _bar(ohlc, ticker, day)
            if bar is None:
                continue
            high = float(bar.get('High') or 0)
            low = float(bar.get('Low') or 0)
            close = float(bar.get('Close') or 0)
            if high <= 0 and close > 0:
                high = close
            if low <= 0 and close > 0:
                low = close
            if close <= 0:
                continue

            # Update trail from High
            state_hi = compute_trail_state_on_list(
                pos.purchase,
                high,
                pos.peak_gain_pct,
                pos.stop_gain_pct,
                pos.trail_active,
                cfg,
            )
            pos.peak_gain_pct = float(state_hi['peak_gain_pct'])
            pos.stop_gain_pct = float(state_hi['stop_gain_pct'])
            pos.trail_active = bool(state_hi['trail_active'])
            stop_price = float(state_hi['stop_price'])

            can_sell = day > pos.purchased_day
            if can_sell and low > 0 and low <= stop_price:
                to_close.append((ticker, stop_price))
                continue

            # Mark state at Close (peak already includes High)
            state_c = compute_trail_state_on_list(
                pos.purchase,
                close,
                pos.peak_gain_pct,
                pos.stop_gain_pct,
                pos.trail_active,
                cfg,
            )
            pos.peak_gain_pct = float(state_c['peak_gain_pct'])
            pos.stop_gain_pct = float(state_c['stop_gain_pct'])
            pos.trail_active = bool(state_c['trail_active'])

            # Close-breach backup (gap through stop without low print — rare)
            if can_sell and bool(state_c['breached']):
                to_close.append((ticker, close))

        for ticker, fill_px in to_close:
            pos = positions.pop(ticker, None)
            if pos is None:
                continue
            cash += pos.shares * fill_px
            trade_count += 1

        # Mark-to-market equity
        mtm = cash
        for pos in positions.values():
            bar = _bar(ohlc, pos.ticker, day)
            if bar is None:
                mtm += pos.shares * pos.purchase
                continue
            close = float(bar.get('Close') or 0)
            mtm += pos.shares * (close if close > 0 else pos.purchase)

        spy_bar = _bar(ohlc, spy_ticker, day)
        spy_close = float(spy_bar.get('Close') or 0) if spy_bar is not None else 0.0
        spy_equity = cfg.starting_cash * (spy_close / spy_start_px) if spy_close > 0 else cfg.starting_cash

        equity_curve.append({
            'date': day.isoformat(),
            'algo_equity': mtm,
            'spy_equity': spy_equity,
            'cash': cash,
            'n_positions': len(positions),
        })

    algo_eq = [float(p['algo_equity']) for p in equity_curve]
    spy_eq = [float(p['spy_equity']) for p in equity_curve]
    ending = algo_eq[-1] if algo_eq else cfg.starting_cash
    spy_ending = spy_eq[-1] if spy_eq else cfg.starting_cash
    algo_ret = (ending / cfg.starting_cash) - 1.0
    spy_ret = (spy_ending / cfg.starting_cash) - 1.0

    return SimResult(
        start=days[0],
        end=days[-1],
        starting_cash=cfg.starting_cash,
        ending_equity=ending,
        algo_return_pct=algo_ret * 100.0,
        spy_return_pct=spy_ret * 100.0,
        excess_pct=(algo_ret - spy_ret) * 100.0,
        algo_max_dd_pct=max_drawdown_pct(algo_eq) * 100.0,
        spy_max_dd_pct=max_drawdown_pct(spy_eq) * 100.0,
        trade_count=trade_count,
        equity_curve=equity_curve,
    )


def iter_start_dates(start: date, end: date, stride_days: int) -> List[date]:
    """Calendar stride starts; caller filters to days with market data."""
    if stride_days < 1:
        stride_days = 1
    out = []  # type: List[date]
    d = start
    while d <= end:
        out.append(d)
        d = d + timedelta(days=stride_days)
    return out


def summarize_results(results: Sequence[SimResult]) -> Dict[str, Any]:
    if not results:
        return {'n': 0}

    def _pctiles(vals: List[float]) -> Dict[str, float]:
        arr = np.array(vals, dtype=float)
        return {
            'median': float(np.median(arr)),
            'p10': float(np.percentile(arr, 10)),
            'p90': float(np.percentile(arr, 90)),
            'mean': float(np.mean(arr)),
            'min': float(np.min(arr)),
            'max': float(np.max(arr)),
        }

    algo_rets = [r.algo_return_pct for r in results]
    spy_rets = [r.spy_return_pct for r in results]
    excess = [r.excess_pct for r in results]
    algo_dd = [r.algo_max_dd_pct for r in results]
    spy_dd = [r.spy_max_dd_pct for r in results]

    worst_dd = min(results, key=lambda r: r.algo_max_dd_pct)
    worst_ret = min(results, key=lambda r: r.algo_return_pct)

    return {
        'n': len(results),
        'algo_return': _pctiles(algo_rets),
        'spy_return': _pctiles(spy_rets),
        'excess': _pctiles(excess),
        'algo_max_dd': _pctiles(algo_dd),
        'spy_max_dd': _pctiles(spy_dd),
        'frac_dd_worse_than_20': sum(1 for x in algo_dd if x <= -20.0) / len(algo_dd),
        'frac_dd_worse_than_40': sum(1 for x in algo_dd if x <= -40.0) / len(algo_dd),
        'frac_beat_spy': sum(1 for x in excess if x > 0) / len(excess),
        'worst_dd_start': worst_dd.start.isoformat(),
        'worst_dd_pct': worst_dd.algo_max_dd_pct,
        'worst_return_start': worst_ret.start.isoformat(),
        'worst_return_pct': worst_ret.algo_return_pct,
    }
