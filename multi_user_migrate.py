"""
One-time / idempotent multi-user schema helpers used by init_database.
Adds user_id to per-person tables and migrates legacy rows to the owner user.
"""

from __future__ import print_function

import os
import shutil
import sqlite3
from typing import List, Optional, Sequence, Tuple

import config
import user_context as uc


def _table_cols(cursor, table: str) -> List[str]:
    cursor.execute('PRAGMA table_info(%s)' % table)
    return [row[1] for row in cursor.fetchall()]


def _table_exists(cursor, table: str) -> bool:
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return cursor.fetchone() is not None


def _add_column_if_missing(cursor, table: str, column: str, decl: str) -> None:
    if not _table_exists(cursor, table):
        return
    if column in _table_cols(cursor, table):
        return
    cursor.execute('ALTER TABLE %s ADD COLUMN %s %s' % (table, column, decl))
    print('Added %s.%s' % (table, column))


def _rebuild_composite_pk(
    cursor,
    table: str,
    create_sql: str,
    copy_columns: Sequence[str],
    owner_id: int,
) -> None:
    """
    Rebuild table so PRIMARY KEY includes user_id.
    If user_id already in PK (detected via unique index / presence + no ticker-only pk),
    skip when a companion marker flag exists.
    """
    if not _table_exists(cursor, table):
        return
    cols = _table_cols(cursor, table)
    marker = '_mu_pk_%s' % table
    # Use a tiny meta table for migration markers
    cursor.execute(
        'CREATE TABLE IF NOT EXISTS _schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT)'
    )
    cursor.execute('SELECT 1 FROM _schema_migrations WHERE name = ?', (marker,))
    if cursor.fetchone():
        return

    if 'user_id' not in cols:
        cursor.execute('ALTER TABLE %s ADD COLUMN user_id INTEGER' % table)
        cols = _table_cols(cursor, table)
    cursor.execute(
        'UPDATE %s SET user_id = ? WHERE user_id IS NULL' % table,
        (owner_id,),
    )

    tmp = table + '_mu_new'
    cursor.execute('DROP TABLE IF EXISTS %s' % tmp)
    cursor.execute(create_sql.replace(table + ' (', tmp + ' (', 1))
    col_list = ', '.join(copy_columns)
    # Only copy columns that exist
    existing = [c for c in copy_columns if c in cols or c == 'user_id']
    if 'user_id' not in existing:
        existing = ['user_id'] + existing
    sel = []
    for c in existing:
        if c == 'user_id':
            sel.append('COALESCE(user_id, %d)' % owner_id)
        else:
            sel.append(c)
    cursor.execute(
        'INSERT OR IGNORE INTO %s (%s) SELECT %s FROM %s'
        % (tmp, ', '.join(existing), ', '.join(sel), table)
    )
    cursor.execute('DROP TABLE %s' % table)
    cursor.execute('ALTER TABLE %s RENAME TO %s' % (tmp, table))
    cursor.execute(
        'INSERT OR REPLACE INTO _schema_migrations (name, applied_at) VALUES (?, datetime("now"))',
        (marker,),
    )
    print('Rebuilt %s with user_id composite key' % table)


def migrate_tokens_file_for_owner(username: str) -> None:
    """Copy legacy ~/.schwabdev/tokens.db -> tokens_<username>.db once."""
    legacy = os.path.expanduser(
        getattr(config, 'SCHWAB_TOKENS_DB', '~/.schwabdev/tokens.db')
    )
    dest = uc.default_tokens_db_for_username(username)
    if os.path.isfile(dest):
        return
    if os.path.isfile(legacy):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(legacy, dest)
        print('Copied Schwab tokens %s -> %s' % (legacy, dest))


def run_multi_user_migration(conn: sqlite3.Connection) -> int:
    """
    Ensure auth tables + user_id columns. Returns owner user_id (jame).
    """
    uc.init_auth_tables(conn)
    conn.commit()
    # Bootstrap uses its own short-lived connections — commit first so we do not
    # self-deadlock against this long-lived migration connection.
    uc.bootstrap_default_users()
    owner = uc.get_user_by_username('jame')
    if not owner:
        users = uc.list_active_users()
        if not users:
            raise RuntimeError('multi-user migrate: no users')
        owner_id = int(users[0]['id'])
        owner_name = users[0]['username']
    else:
        owner_id = int(owner['id'])
        owner_name = owner['username']

    cursor = conn.cursor()

    # Simple add-column tables
    for table in (
        'pending_orders',
        'account_snapshots',
        'trade_history',
        'event_log',
        'web_commands',
    ):
        _add_column_if_missing(cursor, table, 'user_id', 'INTEGER')
        if _table_exists(cursor, table):
            cursor.execute(
                'UPDATE %s SET user_id = ? WHERE user_id IS NULL' % table,
                (owner_id,),
            )

    # Composite PK tables — recreate with (user_id, ticker)
    pos_cols = [
        'user_id', 'ticker', 'date_purchased', 'shares_owned', 'average_price',
        'market_value', 'current_day_profit_loss', 'day_pct', 'long_open_profit_loss',
        'open_pct', 'purchased_at', 'peak_gain_pct', 'stop_gain_pct', 'trail_active',
        'stop_order_id', 'stop_order_price', 'stop_limit_price', 'stop_order_qty',
    ]
    _rebuild_composite_pk(
        cursor,
        'positions',
        '''
        CREATE TABLE positions (
            user_id INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            date_purchased TEXT,
            shares_owned INTEGER NOT NULL DEFAULT 0,
            average_price REAL,
            market_value REAL,
            current_day_profit_loss REAL,
            day_pct REAL,
            long_open_profit_loss REAL,
            open_pct REAL,
            purchased_at TEXT,
            peak_gain_pct REAL,
            stop_gain_pct REAL,
            trail_active INTEGER,
            stop_order_id TEXT,
            stop_order_price REAL,
            stop_limit_price REAL,
            stop_order_qty INTEGER,
            PRIMARY KEY (user_id, ticker)
        )
        ''',
        pos_cols,
        owner_id,
    )

    # watchlist: dynamic columns — rebuild preserving all columns + user_id PK
    if _table_exists(cursor, 'watchlist'):
        cursor.execute(
            'CREATE TABLE IF NOT EXISTS _schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT)'
        )
        cursor.execute(
            'SELECT 1 FROM _schema_migrations WHERE name = ?',
            ('_mu_pk_watchlist',),
        )
        if not cursor.fetchone():
            cols = _table_cols(cursor, 'watchlist')
            if 'user_id' not in cols:
                cursor.execute('ALTER TABLE watchlist ADD COLUMN user_id INTEGER')
                cols = _table_cols(cursor, 'watchlist')
            cursor.execute(
                'UPDATE watchlist SET user_id = ? WHERE user_id IS NULL',
                (owner_id,),
            )
            # Build CREATE from current schema
            cursor.execute('PRAGMA table_info(watchlist)')
            info = cursor.fetchall()
            col_defs = []
            copy_names = []
            for _cid, name, ctype, _notnull, _default, _pk in info:
                if name == 'user_id':
                    continue
                copy_names.append(name)
                col_defs.append('%s %s' % (name, ctype or 'TEXT'))
            create_sql = (
                'CREATE TABLE watchlist_mu_new ('
                'user_id INTEGER NOT NULL, '
                + ', '.join(col_defs)
                + ', PRIMARY KEY (user_id, ticker))'
            )
            cursor.execute('DROP TABLE IF EXISTS watchlist_mu_new')
            cursor.execute(create_sql)
            all_copy = ['user_id'] + copy_names
            cursor.execute(
                'INSERT OR IGNORE INTO watchlist_mu_new (%s) SELECT %s FROM watchlist'
                % (
                    ', '.join(all_copy),
                    ', '.join(
                        [
                            ('COALESCE(user_id, %d)' % owner_id)
                            if c == 'user_id'
                            else c
                            for c in all_copy
                        ]
                    ),
                )
            )
            cursor.execute('DROP TABLE watchlist')
            cursor.execute('ALTER TABLE watchlist_mu_new RENAME TO watchlist')
            cursor.execute(
                'INSERT OR REPLACE INTO _schema_migrations (name, applied_at) '
                'VALUES (?, datetime("now"))',
                ('_mu_pk_watchlist',),
            )
            print('Rebuilt watchlist with user_id composite key')

    _rebuild_composite_pk(
        cursor,
        'rebuy_guards',
        '''
        CREATE TABLE rebuy_guards (
            user_id INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            last_sell_price REAL,
            last_cost_basis REAL,
            last_sold_at TEXT,
            PRIMARY KEY (user_id, ticker)
        )
        ''',
        ['user_id', 'ticker', 'last_sell_price', 'last_cost_basis', 'last_sold_at'],
        owner_id,
    )

    _rebuild_composite_pk(
        cursor,
        'position_book',
        '''
        CREATE TABLE position_book (
            user_id INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            book TEXT NOT NULL,
            tagged_at TEXT NOT NULL,
            enrolled_at TEXT,
            note TEXT,
            origin TEXT,
            PRIMARY KEY (user_id, ticker)
        )
        ''',
        [
            'user_id', 'ticker', 'book', 'tagged_at', 'enrolled_at', 'note', 'origin',
        ],
        owner_id,
    )

    # Indexes for filtered queries
    for sql in (
        'CREATE INDEX IF NOT EXISTS idx_event_log_user_ts ON event_log(user_id, ts DESC)',
        'CREATE INDEX IF NOT EXISTS idx_trade_history_user_ts ON trade_history(user_id, ts DESC)',
        'CREATE INDEX IF NOT EXISTS idx_pending_orders_user ON pending_orders(user_id)',
        'CREATE INDEX IF NOT EXISTS idx_account_snapshots_user ON account_snapshots(user_id, ts)',
    ):
        try:
            cursor.execute(sql)
        except sqlite3.OperationalError:
            pass

    # Move legacy runtime_flags into owner-scoped keys (u{id}:...)
    _migrate_runtime_flags_to_owner(cursor, owner_id)

    conn.commit()
    migrate_tokens_file_for_owner(owner_name)
    uc.ensure_user_settings(owner_id)
    # Owner account is already trading — skip onboarding card
    try:
        import filter_builder as fb
        fb.init_filter_tables(conn)
        uc.update_user_settings(owner_id, setup_complete=True)
    except Exception:
        pass
    return owner_id


# Flags that stay global (not copied to u{id}:)
_GLOBAL_RUNTIME_FLAGS = frozenset([
    'last_loop_wake',
    'rth_session_started',
    'yahoo_universe_size',
])


def _migrate_runtime_flags_to_owner(cursor, owner_id: int) -> None:
    marker = '_mu_runtime_flags_owner'
    cursor.execute(
        'CREATE TABLE IF NOT EXISTS _schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT)'
    )
    cursor.execute('SELECT 1 FROM _schema_migrations WHERE name = ?', (marker,))
    if cursor.fetchone():
        return
    prefix = 'u%d:' % int(owner_id)
    rows = cursor.execute('SELECT key, value, updated_at FROM runtime_flags').fetchall()
    moved = 0
    for key, value, updated_at in rows:
        if not key or key.startswith('u') and ':' in key:
            continue
        if key in _GLOBAL_RUNTIME_FLAGS:
            continue
        scoped = prefix + key
        existing = cursor.execute(
            'SELECT value FROM runtime_flags WHERE key = ?', (scoped,)
        ).fetchone()
        if existing:
            # Prefer richer legacy dashboard_rev / non-empty values
            if key == 'dashboard_rev':
                try:
                    if int(value or 0) > int(existing[0] or 0):
                        cursor.execute(
                            'UPDATE runtime_flags SET value = ?, updated_at = ? WHERE key = ?',
                            (value, updated_at, scoped),
                        )
                        moved += 1
                except (TypeError, ValueError):
                    pass
            continue
        cursor.execute(
            'INSERT OR REPLACE INTO runtime_flags (key, value, updated_at) VALUES (?, ?, ?)',
            (scoped, value, updated_at),
        )
        moved += 1
    cursor.execute(
        'INSERT OR REPLACE INTO _schema_migrations (name, applied_at) VALUES (?, datetime("now"))',
        (marker,),
    )
    if moved:
        print('Migrated %d runtime_flags to %s*' % (moved, prefix))
