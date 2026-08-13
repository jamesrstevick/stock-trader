"""
Inject user_id into SQL against per-user tables.

Used by get_connection() so existing leaf queries stay correct under multi-user
without rewriting every call site. Shared tables (fundamentals, users, …) are
left untouched.
"""

from __future__ import print_function

import re
import sqlite3
from typing import Any, List, Optional, Sequence, Tuple, Union

import user_context as uc

USER_TABLES = frozenset([
    'positions',
    'watchlist',
    'pending_orders',
    'rebuy_guards',
    'account_snapshots',
    'trade_history',
    'position_book',
    'event_log',
    'web_commands',
])

_SQL_KEYWORDS = frozenset([
    'WHERE', 'JOIN', 'LEFT', 'INNER', 'OUTER', 'CROSS', 'ON', 'ORDER', 'GROUP',
    'LIMIT', 'HAVING', 'SET', 'VALUES', 'SELECT', 'FROM', 'AS', 'AND', 'OR',
    'NOT', 'IN', 'IS', 'NULL', 'DESC', 'ASC', 'UNION', 'ALL', 'DISTINCT',
])

# Clauses that end a WHERE predicate (keep them outside the injected parens).
_AFTER_WHERE = re.compile(
    r'\b(ORDER\s+BY|GROUP\s+BY|LIMIT|HAVING|RETURNING)\b',
    re.I,
)

_Params = Union[Sequence[Any], None]


def _already_scoped(sql: str) -> bool:
    return bool(re.search(r'\buser_id\b', sql, re.I))


def _touches_user_table(sql: str) -> bool:
    upper = sql.upper()
    for table in USER_TABLES:
        if re.search(r'\b' + table.upper() + r'\b', upper):
            return True
    return False


def _find_user_aliases(sql: str) -> List[str]:
    """Return SQL expressions for user_id columns (alias.user_id or user_id)."""
    found = []  # type: List[str]
    pattern = re.compile(
        r'\b(FROM|JOIN)\s+(\w+)(?:\s+(?:AS\s+)?(\w+))?',
        re.I,
    )
    for m in pattern.finditer(sql):
        table = m.group(2).lower()
        if table not in USER_TABLES:
            continue
        alias = m.group(3)
        if alias and alias.upper() in _SQL_KEYWORDS:
            alias = None
        if alias:
            expr = '%s.user_id' % alias
        else:
            expr = 'user_id'
        if expr not in found:
            found.append(expr)
    return found


def _fix_on_conflict_ticker(sql: str) -> str:
    """Composite PK is (user_id, ticker) — rewrite legacy ON CONFLICT(ticker)."""
    return re.sub(
        r'ON\s+CONFLICT\s*\(\s*ticker\s*\)',
        'ON CONFLICT(user_id, ticker)',
        sql,
        flags=re.I,
    )


def _inject_user_pred(sql: str, where_match, pred: str) -> str:
    """
    Turn `WHERE <cond>` into `WHERE <pred> AND (<cond>)`.

    Parentheses are required: `user_id = ? AND a OR b` is
    `(user_id = ? AND a) OR b`, which leaks other users' rows (e.g.
    account_snapshots with `liquidation_value > 0 OR total_value > 0`).
    """
    after = sql[where_match.end():]
    tail_m = _AFTER_WHERE.search(after)
    if tail_m:
        body = after[: tail_m.start()]
        tail = after[tail_m.start():]
    else:
        body = after
        tail = ''
    body = body.strip()
    tail = tail.strip()
    if not body:
        injected = ' ' + pred
    else:
        injected = ' ' + pred + ' AND (' + body + ')'
    if tail:
        injected += ' ' + tail
    return sql[: where_match.end()] + injected


def rewrite_sql(sql: str, params: _Params, user_id: int) -> Tuple[str, List[Any]]:
    params_list = list(params) if params is not None else []
    if not sql:
        return sql, params_list

    stripped = sql.strip()
    upper = stripped.upper()

    # INSERT must run before _already_scoped: PRAGMA-driven INSERTs include
    # user_id in the column list with a NULL placeholder, which would otherwise
    # skip injection and hit NOT NULL (watchlist.user_id).
    m = re.match(
        r'^(INSERT\s+(?:OR\s+\w+\s+)?)INTO\s+(\w+)\s*\(([^)]*)\)\s*VALUES\s*\(',
        stripped,
        re.I | re.S,
    )
    if m and m.group(2).lower() in USER_TABLES:
        cols = [c.strip() for c in m.group(3).split(',') if c.strip()]
        lower_cols = [c.lower() for c in cols]
        if 'user_id' in lower_cols:
            idx = lower_cols.index('user_id')
            while len(params_list) <= idx:
                params_list.append(None)
            params_list[idx] = user_id
            return _fix_on_conflict_ticker(stripped), params_list
        rest = stripped[m.end():]
        new_sql = '%sINTO %s (user_id, %s) VALUES (?, %s' % (
            m.group(1),
            m.group(2),
            m.group(3).strip(),
            rest,
        )
        return _fix_on_conflict_ticker(new_sql), [user_id] + params_list

    # Even if user_id already appears (e.g. log_event SELECT), still fix ON CONFLICT(ticker)
    if _already_scoped(sql):
        if _touches_user_table(sql) and re.search(r'ON\s+CONFLICT\s*\(\s*ticker\s*\)', sql, re.I):
            return _fix_on_conflict_ticker(sql), params_list
        return sql, params_list

    if not _touches_user_table(sql):
        return sql, params_list

    # DELETE FROM table ...
    m = re.match(r'^DELETE\s+FROM\s+(\w+)\b', stripped, re.I)
    if m and m.group(1).lower() in USER_TABLES:
        wh = re.search(r'\bWHERE\b', stripped, re.I)
        if wh:
            new_sql = _inject_user_pred(stripped, wh, 'user_id = ?')
            return new_sql, [user_id] + params_list
        new_sql = stripped + ' WHERE user_id = ?'
        return new_sql, params_list + [user_id]

    # UPDATE table SET ...
    m = re.match(r'^UPDATE\s+(\w+)\s+SET\b', stripped, re.I)
    if m and m.group(1).lower() in USER_TABLES:
        wh = re.search(r'\bWHERE\b', stripped, re.I)
        if wh:
            before = stripped[: wh.start()]
            n_set = before.count('?')
            new_sql = _inject_user_pred(stripped, wh, 'user_id = ?')
            return new_sql, params_list[:n_set] + [user_id] + params_list[n_set:]
        new_sql = stripped + ' WHERE user_id = ?'
        return new_sql, params_list + [user_id]

    # SELECT / WITH ...
    if upper.startswith('SELECT') or upper.startswith('WITH'):
        aliases = _find_user_aliases(stripped)
        if not aliases:
            return sql, params_list
        pred = ' AND '.join(['%s = ?' % a for a in aliases])
        n = len(aliases)
        wh = re.search(r'\bWHERE\b', stripped, re.I)
        if wh:
            new_sql = _inject_user_pred(stripped, wh, pred)
            return new_sql, [user_id] * n + params_list
        ins = re.search(r'\b(ORDER\s+BY|GROUP\s+BY|LIMIT|HAVING)\b', stripped, re.I)
        clause = ' WHERE ' + pred
        if ins:
            new_sql = stripped[: ins.start()] + clause + ' ' + stripped[ins.start():]
        else:
            new_sql = stripped + clause
        # New user_id placeholders are before existing ORDER/LIMIT params
        return new_sql, [user_id] * n + params_list

    return sql, params_list


class ScopedCursor(sqlite3.Cursor):
    def execute(self, sql, parameters=()):
        try:
            uid = uc.require_user_id()
        except Exception:
            return sqlite3.Cursor.execute(self, sql, parameters)
        new_sql, new_params = rewrite_sql(sql, parameters, uid)
        return sqlite3.Cursor.execute(self, new_sql, new_params)

    def executemany(self, sql, seq_of_parameters):
        try:
            uid = uc.require_user_id()
        except Exception:
            return sqlite3.Cursor.executemany(self, sql, seq_of_parameters)
        rewritten = []
        new_sql = sql
        for parameters in seq_of_parameters:
            new_sql, new_params = rewrite_sql(sql, parameters, uid)
            rewritten.append(new_params)
        return sqlite3.Cursor.executemany(self, new_sql, rewritten)


class ScopedConnection(sqlite3.Connection):
    def cursor(self, factory=None):
        if factory is None:
            factory = ScopedCursor
        return sqlite3.Connection.cursor(self, factory)

    def execute(self, sql, parameters=()):
        cur = self.cursor()
        cur.execute(sql, parameters)
        return cur

    def executemany(self, sql, seq_of_parameters):
        cur = self.cursor()
        cur.executemany(sql, seq_of_parameters)
        return cur


def connect(
    db_path: str,
    timeout: float = 60.0,
    busy_timeout_ms: int = 60000,
) -> sqlite3.Connection:
    conn = sqlite3.connect(
        db_path, timeout=float(timeout), factory=ScopedConnection
    )
    uc.configure_connection(conn, busy_timeout_ms=int(busy_timeout_ms))
    return conn
