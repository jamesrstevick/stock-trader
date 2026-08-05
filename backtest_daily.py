#!/usr/bin/env python3
"""
Daily multi-start backtest — side research CLI (not part of the live trading loop).

Freezes today's active filter universe, downloads/caches Yahoo daily OHLC, then
runs a once-per-day loose model (trail/hard stops + cash floor) from many start
dates and compares each run to buy-and-hold SPY.

Usage (venv activated):
  python backtest_daily.py
  python backtest_daily.py --start 2020-01-01 --stride-days 21 --cash 50000 --max-tickers 40

Limitations: survivorship (today's safe names), daily path vs live 15m/STOP_LIMIT,
no point-in-time fundamentals. Live Yahoo refresh does NOT pull per-ticker history;
this script is the intentional OHLC consumer.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Sequence

import pandas as pd
import yfinance as yf

import config
import stock_trader as st
from backtest.engine import (
    SimConfig,
    SimResult,
    iter_start_dates,
    run_daily_sim,
    summarize_results,
)

CACHE_DIR = os.path.join('data', 'backtest_cache')
RESULTS_DIR = os.path.join('data', 'backtest_results')


def _parse_date(s: str) -> date:
    return datetime.strptime(s, '%Y-%m-%d').date()


def resolve_universe(max_tickers: Optional[int]) -> List[str]:
    st.init_database()
    filter_name = str(getattr(config, 'WATCHLIST_FILTER_NAME', 'safe')).strip().lower()
    if filter_name == 'risky':
        tickers = st.risky_filter_stocks(limit=max_tickers)
    else:
        tickers = st.safe_filter_stocks(limit=max_tickers)
    # Preserve rank order; drop empties
    out = []  # type: List[str]
    seen = set()
    for t in tickers or []:
        u = str(t).strip().upper()
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def _cache_path(ticker: str, start: date, end: date) -> str:
    safe = ticker.replace('/', '_')
    return os.path.join(
        CACHE_DIR,
        '%s_%s_%s.csv' % (safe, start.isoformat(), end.isoformat()),
    )


def load_ohlc_yahoo(
    tickers: Sequence[str],
    start: date,
    end: date,
    refresh: bool = False,
    sleep_s: float = 0.35,
) -> Dict[str, pd.DataFrame]:
    """
    Download daily OHLC (auto-adjusted) for tickers + SPY; cache CSV under data/backtest_cache/.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    # yfinance end is exclusive
    fetch_end = end + timedelta(days=1)
    want = list(tickers)
    if 'SPY' not in want:
        want = want + ['SPY']

    out = {}  # type: Dict[str, pd.DataFrame]
    for i, ticker in enumerate(want):
        path = _cache_path(ticker, start, end)
        df = None  # type: Optional[pd.DataFrame]
        if not refresh and os.path.isfile(path):
            try:
                df = pd.read_csv(path, parse_dates=['Date'], index_col='Date')
            except Exception as e:
                print('  cache read failed for %s (%s); re-fetching' % (ticker, e))
                df = None

        if df is None or df.empty:
            print('  [%d/%d] Yahoo OHLC %s ...' % (i + 1, len(want), ticker))
            try:
                hist = yf.Ticker(ticker).history(
                    start=start.isoformat(),
                    end=fetch_end.isoformat(),
                    auto_adjust=True,
                )
            except Exception as e:
                print('    FAILED %s: %s' % (ticker, e))
                hist = pd.DataFrame()
            if hist is not None and not hist.empty:
                # Normalize index to midnight dates (tz-naive)
                hist = hist.copy()
                hist.index = pd.to_datetime(hist.index).tz_localize(None).normalize()
                cols = [c for c in ('Open', 'High', 'Low', 'Close', 'Volume') if c in hist.columns]
                df = hist[cols]
                df.to_csv(path, index_label='Date')
            else:
                df = pd.DataFrame()
            if sleep_s > 0 and i + 1 < len(want):
                time.sleep(sleep_s)
        else:
            if not isinstance(df.index, pd.DatetimeIndex):
                df.index = pd.to_datetime(df.index)
            df.index = df.index.tz_localize(None).normalize()

        out[ticker] = df if df is not None else pd.DataFrame()

    return out


def sim_config_from_live(cash: float) -> SimConfig:
    return SimConfig(
        starting_cash=float(cash),
        order_amount=float(getattr(config, 'ORDER_AMOUNT_DOLLARS', 1000.0)),
        min_cash=float(getattr(config, 'MINIMUM_CASH', 15000.0)),
        trail_activate_pct=float(getattr(config, 'TRAIL_ACTIVATE_PCT', 0.10)),
        trail_buffer_pct=float(getattr(config, 'TRAIL_BUFFER_PCT', 0.05)),
        hard_stop_pct=float(getattr(config, 'HARD_STOP_ON_WATCHLIST_PCT', -0.10)),
        rebuy_discount_pct=float(getattr(config, 'REBUY_DISCOUNT_PCT', 0.05)),
    )


def align_start_to_trading_day(
    start: date,
    ohlc: Dict[str, pd.DataFrame],
    end: date,
) -> Optional[date]:
    spy = ohlc.get('SPY')
    if spy is None or spy.empty:
        return None
    for ts in spy.index:
        d = ts.date() if hasattr(ts, 'date') else pd.Timestamp(ts).date()
        if d < start:
            continue
        if d > end:
            return None
        return d
    return None


def write_summary_csv(path: str, results: Sequence[SimResult]) -> None:
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    fields = [
        'start', 'end', 'starting_cash', 'ending_equity',
        'algo_return_pct', 'spy_return_pct', 'excess_pct',
        'algo_max_dd_pct', 'spy_max_dd_pct', 'trade_count',
    ]
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in results:
            w.writerow({
                'start': r.start.isoformat(),
                'end': r.end.isoformat(),
                'starting_cash': '%.2f' % r.starting_cash,
                'ending_equity': '%.2f' % r.ending_equity,
                'algo_return_pct': '%.4f' % r.algo_return_pct,
                'spy_return_pct': '%.4f' % r.spy_return_pct,
                'excess_pct': '%.4f' % r.excess_pct,
                'algo_max_dd_pct': '%.4f' % r.algo_max_dd_pct,
                'spy_max_dd_pct': '%.4f' % r.spy_max_dd_pct,
                'trade_count': r.trade_count,
            })


def write_equity_csv(path: str, result: SimResult) -> None:
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=['date', 'algo_equity', 'spy_equity', 'cash', 'n_positions'])
        w.writeheader()
        for row in result.equity_curve:
            w.writerow({
                'date': row['date'],
                'algo_equity': '%.2f' % row['algo_equity'],
                'spy_equity': '%.2f' % row['spy_equity'],
                'cash': '%.2f' % row['cash'],
                'n_positions': row['n_positions'],
            })


def _fmt_block(title: str, stats: Dict[str, float]) -> None:
    print(
        '  %s: median %+7.2f%% | p10 %+7.2f%% | p90 %+7.2f%% | '
        'min %+7.2f%% | max %+7.2f%%'
        % (
            title,
            stats['median'],
            stats['p10'],
            stats['p90'],
            stats['min'],
            stats['max'],
        )
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    default_start = str(getattr(config, 'DEFAULT_START_DATE', '2020-01-01') or '2020-01-01')
    parser = argparse.ArgumentParser(
        description='Daily multi-start backtest vs SPY (side research tool)',
    )
    parser.add_argument('--start', default=default_start, help='First possible start YYYY-MM-DD')
    parser.add_argument('--end', default=None, help='End date YYYY-MM-DD (default: today)')
    parser.add_argument('--stride-days', type=int, default=21, help='Calendar days between starts')
    parser.add_argument('--cash', type=float, default=50000.0, help='Starting cash per run')
    parser.add_argument(
        '--max-tickers',
        type=int,
        default=40,
        help='Cap frozen filter universe size (0 = all matches)',
    )
    parser.add_argument('--refresh-cache', action='store_true', help='Re-download Yahoo OHLC')
    parser.add_argument(
        '--min-days',
        type=int,
        default=60,
        help='Skip starts with fewer than this many trading days remaining',
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    start = _parse_date(args.start)
    end = _parse_date(args.end) if args.end else date.today()
    max_tickers = None if int(args.max_tickers) <= 0 else int(args.max_tickers)

    print('=' * 60)
    print('DAILY MULTI-START BACKTEST (loose model vs SPY)')
    print('=' * 60)
    print('Window: %s -> %s | stride=%dd | cash=$%s | max_tickers=%s'
          % (start, end, args.stride_days, '{:,.0f}'.format(args.cash), max_tickers or 'all'))

    print('\nFreezing filter universe from DB...')
    universe = resolve_universe(max_tickers)
    filter_name = getattr(config, 'WATCHLIST_FILTER_NAME', 'safe')
    print('  Filter: %s | %d tickers' % (filter_name, len(universe)))
    if not universe:
        print('ERROR: empty universe. Refresh Yahoo fundamentals / loosen filter.')
        return 1
    print('  Names: %s%s' % (
        ', '.join(universe[:15]),
        ' ...' if len(universe) > 15 else '',
    ))

    print('\nLoading daily OHLC (cache: %s)...' % CACHE_DIR)
    ohlc = load_ohlc_yahoo(universe, start, end, refresh=bool(args.refresh_cache))
    spy = ohlc.get('SPY')
    if spy is None or spy.empty:
        print('ERROR: no SPY history - cannot benchmark.')
        return 1
    n_ok = sum(1 for t in universe if not ohlc.get(t, pd.DataFrame()).empty)
    print('  Price history available for %d / %d universe names + SPY' % (n_ok, len(universe)))

    cfg = sim_config_from_live(args.cash)
    print('\nSim knobs: order=$%.0f min_cash=$%.0f trail_act=%.0f%% buffer=%.0f%% hard=%.0f%%'
          % (
              cfg.order_amount,
              cfg.min_cash,
              cfg.trail_activate_pct * 100,
              cfg.trail_buffer_pct * 100,
              cfg.hard_stop_pct * 100,
          ))

    candidates = iter_start_dates(start, end, int(args.stride_days))
    results = []  # type: List[SimResult]
    print('\nRunning %d candidate starts...' % len(candidates))
    for raw_start in candidates:
        aligned = align_start_to_trading_day(raw_start, ohlc, end)
        if aligned is None:
            continue
        # Count remaining spy days
        spy_days = [
            (ts.date() if hasattr(ts, 'date') else pd.Timestamp(ts).date())
            for ts in spy.index
        ]
        remaining = sum(1 for d in spy_days if aligned <= d <= end)
        if remaining < int(args.min_days):
            continue
        result = run_daily_sim(universe, ohlc, aligned, end, cfg=cfg)
        results.append(result)
        print(
            '  %s -> %s | algo %+6.1f%% | SPY %+6.1f%% | excess %+6.1f%% | '
            'maxDD algo %+5.1f%% spy %+5.1f%% | trades %d'
            % (
                result.start,
                result.end,
                result.algo_return_pct,
                result.spy_return_pct,
                result.excess_pct,
                result.algo_max_dd_pct,
                result.spy_max_dd_pct,
                result.trade_count,
            )
        )

    if not results:
        print('ERROR: no simulations completed (check dates / min-days / data).')
        return 1

    summary = summarize_results(results)
    print('\n' + '=' * 60)
    print('AGGREGATE (%d starts)' % summary['n'])
    print('=' * 60)
    _fmt_block('Algo return ', summary['algo_return'])
    _fmt_block('SPY return  ', summary['spy_return'])
    _fmt_block('Excess      ', summary['excess'])
    _fmt_block('Algo max DD ', summary['algo_max_dd'])
    _fmt_block('SPY max DD  ', summary['spy_max_dd'])
    print(
        '  Beat SPY: %.0f%% of starts | DD<=-20%%: %.0f%% | DD<=-40%%: %.0f%%'
        % (
            100 * summary['frac_beat_spy'],
            100 * summary['frac_dd_worse_than_20'],
            100 * summary['frac_dd_worse_than_40'],
        )
    )
    print(
        '  Worst algo DD: %+.1f%% (start %s)'
        % (summary['worst_dd_pct'], summary['worst_dd_start'])
    )
    print(
        '  Worst algo return: %+.1f%% (start %s)'
        % (summary['worst_return_pct'], summary['worst_return_start'])
    )

    os.makedirs(RESULTS_DIR, exist_ok=True)
    summary_path = os.path.join(RESULTS_DIR, 'summary.csv')
    write_summary_csv(summary_path, results)
    worst = min(results, key=lambda r: r.algo_max_dd_pct)
    equity_path = os.path.join(RESULTS_DIR, 'equity_worst.csv')
    write_equity_csv(equity_path, worst)
    print('\nWrote %s' % summary_path)
    print('Wrote %s (worst DD start %s)' % (equity_path, worst.start))
    print('\nDone.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
