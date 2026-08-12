"""
Per-user custom watchlist filter: criteria JSON → SQL over fundamentals.
One custom filter per user, max 15 fields. Built-ins (safe/risky) stay in stock_trader.
"""

from __future__ import print_function

import json
import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import config
import user_context as uc

MAX_CUSTOM_FIELDS = 15

# Allowlist: field_key → {column or expr, meaning, default_op, unit hints}
FIELD_CATALOG = [
    {
        'field': 'market_cap',
        'column': 'market_cap',
        'meaning': 'Total market value of the company (shares outstanding × price)',
        'ops': ['gt', 'gte', 'lt', 'lte', 'between'],
        'value_kind': 'dollars',
    },
    {
        'field': 'beta',
        'column': 'beta',
        'meaning': 'Volatility vs the broad market (1.0 ≈ moves with the market)',
        'ops': ['gt', 'gte', 'lt', 'lte', 'between'],
        'value_kind': 'number',
    },
    {
        'field': 'short_float',
        'column': 'short_float',
        'meaning': 'Share of float sold short (0.10 = 10%)',
        'ops': ['gt', 'gte', 'lt', 'lte', 'between'],
        'value_kind': 'fraction',
    },
    {
        'field': 'pe_ratio',
        'column': 'pe_ratio',
        'meaning': 'Price-to-earnings (price per share / earnings per share)',
        'ops': ['gt', 'gte', 'lt', 'lte', 'between'],
        'value_kind': 'number',
    },
    {
        'field': 'peg_ratio',
        'column': 'peg_ratio',
        'meaning': 'P/E divided by expected earnings growth',
        'ops': ['gt', 'gte', 'lt', 'lte', 'between'],
        'value_kind': 'number',
    },
    {
        'field': 'analyst_upside',
        'column': None,
        'expr': '((target_price - current_price) / current_price)',
        'extra_where': 'current_price > 0 AND target_price > 0',
        'meaning': 'Implied upside from current price to consensus analyst target',
        'ops': ['gt', 'gte', 'lt', 'lte', 'between'],
        'value_kind': 'fraction',
    },
    {
        'field': 'recommendation_mean',
        'column': 'recommendation_mean',
        'meaning': 'Analyst consensus score (1=Strong Buy … 5=Sell)',
        'ops': ['gt', 'gte', 'lt', 'lte', 'between'],
        'value_kind': 'number',
    },
    {
        'field': 'dividend_yield',
        'column': 'dividend_yield',
        'meaning': 'Annual dividend yield in percentage points (1.0 = 1%)',
        'ops': ['gt', 'gte', 'lt', 'lte', 'between'],
        'value_kind': 'percent_points',
    },
    {
        'field': 'avg_volume',
        'column': 'avg_volume',
        'meaning': 'Average daily trading volume',
        'ops': ['gt', 'gte', 'lt', 'lte', 'between'],
        'value_kind': 'number',
    },
    {
        'field': 'forward_pe',
        'column': 'forward_pe',
        'meaning': 'Forward P/E based on projected earnings',
        'ops': ['gt', 'gte', 'lt', 'lte', 'between'],
        'value_kind': 'number',
    },
    {
        'field': 'price_to_book',
        'column': 'price_to_book',
        'meaning': 'Price to book value',
        'ops': ['gt', 'gte', 'lt', 'lte', 'between'],
        'value_kind': 'number',
    },
    {
        'field': 'debt_to_equity',
        'column': 'debt_to_equity',
        'meaning': 'Total debt / shareholders equity',
        'ops': ['gt', 'gte', 'lt', 'lte', 'between'],
        'value_kind': 'number',
    },
    {
        'field': 'profit_margins',
        'column': 'profit_margins',
        'meaning': 'Net profit margin (fraction)',
        'ops': ['gt', 'gte', 'lt', 'lte', 'between'],
        'value_kind': 'fraction',
    },
    {
        'field': 'revenue_growth',
        'column': 'revenue_growth',
        'meaning': 'Revenue growth rate (fraction)',
        'ops': ['gt', 'gte', 'lt', 'lte', 'between'],
        'value_kind': 'fraction',
    },
    {
        'field': 'current_price',
        'column': 'current_price',
        'meaning': 'Latest share price',
        'ops': ['gt', 'gte', 'lt', 'lte', 'between'],
        'value_kind': 'dollars',
    },
]

_FIELD_BY_NAME = {f['field']: f for f in FIELD_CATALOG}

# Safe Giant starter (8 fields) — mirrors catalog defaults
STARTER_CRITERIA = [
    {'field': 'market_cap', 'op': 'gt', 'value': 25e9, 'why': 'Mega-cap liquidity and durability'},
    {'field': 'beta', 'op': 'between', 'min': 0.0, 'max': 1.3, 'why': 'Cap high-volatility names'},
    {'field': 'short_float', 'op': 'lt', 'value': 0.10, 'why': 'Avoid crowded short interest'},
    {'field': 'pe_ratio', 'op': 'between', 'min': 0.0, 'max': 35.0, 'why': 'Profitable, not richly priced'},
    {'field': 'peg_ratio', 'op': 'between', 'min': 0.0, 'max': 2.0, 'why': 'Growth at a reasonable price'},
    {'field': 'analyst_upside', 'op': 'gt', 'value': 0.10, 'why': 'Meaningful expected appreciation'},
    {'field': 'recommendation_mean', 'op': 'lte', 'value': 2.0, 'why': 'Buy / Strong Buy bias'},
    {'field': 'dividend_yield', 'op': 'gt', 'value': 1.0, 'why': 'Income support while holding'},
]


def init_filter_tables(conn: Optional[sqlite3.Connection] = None) -> None:
    own = conn is None
    if own:
        conn = uc.get_connection()
    assert conn is not None
    conn.execute(
        '''
        CREATE TABLE IF NOT EXISTS user_filters (
            user_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            criteria_json TEXT NOT NULL,
            updated_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        '''
    )
    # setup_complete on user_settings
    try:
        cols = [r[1] for r in conn.execute('PRAGMA table_info(user_settings)').fetchall()]
        if 'setup_complete' not in cols:
            conn.execute(
                'ALTER TABLE user_settings ADD COLUMN setup_complete INTEGER NOT NULL DEFAULT 0'
            )
    except Exception:
        pass
    # One-time: clear seeded floors for accounts that never finished onboarding
    # so they must set bounds from live Schwab cash (e.g. john).
    try:
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS _schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at TEXT
            )
            '''
        )
        marker = 'null_floors_until_setup_v1'
        row = conn.execute(
            'SELECT 1 FROM _schema_migrations WHERE name = ?', (marker,)
        ).fetchone()
        if not row:
            conn.execute(
                '''
                UPDATE user_settings
                SET minimum_cash = NULL,
                    minimum_liquidation_value = NULL,
                    order_amount_dollars = NULL
                WHERE COALESCE(setup_complete, 0) = 0
                '''
            )
            from datetime import datetime
            conn.execute(
                'INSERT INTO _schema_migrations (name, applied_at) VALUES (?, ?)',
                (marker, datetime.now().isoformat(timespec='seconds')),
            )
    except Exception:
        pass
    if own:
        conn.commit()
        conn.close()


def field_catalog_for_api() -> List[Dict[str, Any]]:
    out = []
    for f in FIELD_CATALOG:
        out.append({
            'field': f['field'],
            'meaning': f['meaning'],
            'ops': list(f['ops']),
            'value_kind': f.get('value_kind') or 'number',
        })
    return out


def starter_criteria() -> List[Dict[str, Any]]:
    rows = []
    for c in STARTER_CRITERIA:
        meta = _FIELD_BY_NAME.get(c['field']) or {}
        row = dict(c)
        row['meaning'] = meta.get('meaning') or c['field']
        rows.append(row)
    return rows


def _clause_for_criterion(c: Dict[str, Any]) -> Tuple[Optional[str], List[Any]]:
    field = (c.get('field') or '').strip()
    meta = _FIELD_BY_NAME.get(field)
    if not meta:
        return None, []
    op = (c.get('op') or 'gt').strip().lower()
    if op not in meta['ops']:
        return None, []
    col = meta.get('expr') or meta.get('column')
    if not col:
        return None, []
    parts = []
    params = []  # type: List[Any]
    extra = meta.get('extra_where')
    if extra:
        parts.append('(%s)' % extra)
    if op == 'between':
        lo = c.get('min', c.get('value'))
        hi = c.get('max')
        if lo is None or hi is None:
            return None, []
        parts.append('(%s) >= ? AND (%s) <= ?' % (col, col))
        params.extend([float(lo), float(hi)])
    elif op == 'gt':
        parts.append('(%s) > ?' % col)
        params.append(float(c['value']))
    elif op == 'gte':
        parts.append('(%s) >= ?' % col)
        params.append(float(c['value']))
    elif op == 'lt':
        parts.append('(%s) < ?' % col)
        params.append(float(c['value']))
    elif op == 'lte':
        parts.append('(%s) <= ?' % col)
        params.append(float(c['value']))
    else:
        return None, []
    # Require non-null column when using physical column
    if meta.get('column'):
        parts.insert(0, '%s IS NOT NULL' % meta['column'])
    return ' AND '.join(parts), params


def compile_criteria(criteria: List[Dict[str, Any]]) -> Tuple[str, List[Any]]:
    """Return (WHERE fragment without leading WHERE, params)."""
    if not criteria:
        return '1=0', []
    if len(criteria) > MAX_CUSTOM_FIELDS:
        criteria = criteria[:MAX_CUSTOM_FIELDS]
    clauses = []
    params = []  # type: List[Any]
    for c in criteria:
        frag, p = _clause_for_criterion(c)
        if frag:
            clauses.append('(%s)' % frag)
            params.extend(p)
    if not clauses:
        return '1=0', []
    return ' AND '.join(clauses), params


def _fundamentals_connect(timeout: float = 60.0) -> sqlite3.Connection:
    """Yahoo fundamentals side DB (shared; not user-scoped)."""
    path = getattr(config, 'FUNDAMENTALS_DATABASE_PATH', None)
    if not path:
        main = getattr(config, 'DATABASE_PATH', 'market_data.db')
        root, ext = os.path.splitext(main)
        if root.endswith('market_data'):
            path = root[: -len('market_data')] + 'market_fundamentals' + (ext or '.db')
        else:
            path = root + '_fundamentals' + (ext or '.db')
    conn = sqlite3.connect(str(path), timeout=float(timeout))
    uc.configure_connection(conn)
    return conn


def preview_match_count(criteria: List[Dict[str, Any]]) -> Dict[str, Any]:
    where, params = compile_criteria(criteria or [])
    conn = _fundamentals_connect()
    try:
        row = conn.execute(
            'SELECT COUNT(*) FROM fundamentals WHERE ' + where,
            params,
        ).fetchone()
        n = int(row[0] or 0) if row else 0
    finally:
        conn.close()
    return {
        'ok': True,
        'count': n,
        'field_count': min(len(criteria or []), MAX_CUSTOM_FIELDS),
        'max_fields': MAX_CUSTOM_FIELDS,
    }


def list_custom_tickers(criteria: List[Dict[str, Any]], limit: Optional[int] = None) -> List[str]:
    where, params = compile_criteria(criteria or [])
    sql = (
        'SELECT ticker FROM fundamentals WHERE ' + where +
        ' ORDER BY CASE WHEN current_price > 0 AND target_price > 0 '
        'THEN ((target_price - current_price) / current_price) ELSE -1e99 END DESC'
    )
    params = list(params)
    if limit is not None:
        sql += ' LIMIT ?'
        params.append(int(limit))
    conn = _fundamentals_connect()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def get_user_custom_filter(user_id: int) -> Optional[Dict[str, Any]]:
    init_filter_tables()
    conn = uc.get_connection()
    try:
        row = conn.execute(
            'SELECT name, criteria_json, updated_at FROM user_filters WHERE user_id = ?',
            (user_id,),
        ).fetchone()
        if not row:
            return None
        try:
            criteria = json.loads(row[1] or '[]')
        except Exception:
            criteria = []
        return {
            'name': row[0],
            'criteria': criteria,
            'updated_at': row[2],
        }
    finally:
        conn.close()


def save_user_custom_filter(
    user_id: int,
    name: str,
    criteria: List[Dict[str, Any]],
) -> Dict[str, Any]:
    init_filter_tables()
    name = (name or '').strip()
    if not name:
        return {'ok': False, 'error': 'Filter name required'}
    if name.lower() in ('safe', 'risky'):
        return {'ok': False, 'error': 'Name reserved for built-in filters'}
    if not criteria:
        return {'ok': False, 'error': 'Add at least one field'}
    if len(criteria) > MAX_CUSTOM_FIELDS:
        return {'ok': False, 'error': 'At most %d fields' % MAX_CUSTOM_FIELDS}
    # Validate compile
    where, _params = compile_criteria(criteria)
    if where == '1=0':
        return {'ok': False, 'error': 'No valid criteria'}
    conn = uc.get_connection()
    try:
        conn.execute(
            '''
            INSERT INTO user_filters (user_id, name, criteria_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                name = excluded.name,
                criteria_json = excluded.criteria_json,
                updated_at = excluded.updated_at
            ''',
            (
                user_id,
                name,
                json.dumps(criteria),
                datetime.now().isoformat(timespec='seconds'),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    preview = preview_match_count(criteria)
    return {
        'ok': True,
        'filter': {'name': name, 'criteria': criteria},
        'count': preview.get('count'),
    }


def delete_user_custom_filter(user_id: int) -> Dict[str, Any]:
    init_filter_tables()
    conn = uc.get_connection()
    try:
        conn.execute('DELETE FROM user_filters WHERE user_id = ?', (user_id,))
        conn.commit()
    finally:
        conn.close()
    return {'ok': True}


def format_set_to(c: Dict[str, Any]) -> str:
    op = (c.get('op') or '').lower()
    field = c.get('field') or ''
    meta = _FIELD_BY_NAME.get(field) or {}
    kind = meta.get('value_kind') or 'number'

    def fmt(v):
        try:
            x = float(v)
        except (TypeError, ValueError):
            return str(v)
        if kind == 'dollars':
            if abs(x) >= 1e9:
                return '$%.1fB' % (x / 1e9)
            if abs(x) >= 1e6:
                return '$%.1fM' % (x / 1e6)
            return '$%.2f' % x
        if kind == 'fraction':
            return '%.0f%%' % (x * 100.0) if abs(x) <= 2 else '%.2f' % x
        return ('%.2f' % x).rstrip('0').rstrip('.')

    if op == 'between':
        return '%s – %s' % (fmt(c.get('min')), fmt(c.get('max')))
    if op == 'gt':
        return '> %s' % fmt(c.get('value'))
    if op == 'gte':
        return '≥ %s' % fmt(c.get('value'))
    if op == 'lt':
        return '< %s' % fmt(c.get('value'))
    if op == 'lte':
        return '≤ %s' % fmt(c.get('value'))
    return str(c.get('value'))
