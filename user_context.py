"""
Multi-user identity: password hashes, sessions, and trading user context.

Passwords are checked only on this machine (SQLite). Sessions last ~30 days.
The bot sets current_user_id while running each person's jobs; the web app sets
it from the session cookie.
"""

from __future__ import print_function

import hashlib
import hmac
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Optional

import config

try:
    import contextvars
except ImportError:  # pragma: no cover
    contextvars = None  # type: ignore

SESSION_COOKIE = 'trader_session'
_CURRENT_USER_ID = None  # type: ignore
if contextvars is not None:
    _CURRENT_USER_ID = contextvars.ContextVar('current_user_id', default=None)

# Fallback when contextvars unavailable or unset (single-threaded bot default)
_FALLBACK_USER_ID = None  # type: Optional[int]

# runtime_flags keys that stay global (not prefixed per user)
GLOBAL_FLAG_KEYS = frozenset([
    'last_loop_wake',
    'rth_session_started',
    'yahoo_universe_size',
])

# job_runs names that stay global (shared Yahoo refresh)
GLOBAL_JOB_NAMES = frozenset([
    'refresh_market_data',
])


def _db_path() -> str:
    return getattr(config, 'DATABASE_PATH', 'market_data.db')


def _sessions_db_path() -> str:
    """
    Login sessions live in a side DB so Sign in / Sign out never wait on the
    trader loop's market_data.db write lock.
    """
    override = getattr(config, 'SESSIONS_DATABASE_PATH', None)
    if override:
        return str(override)
    main = os.path.abspath(_db_path())
    root, ext = os.path.splitext(main)
    return root + '_sessions' + (ext or '.db')


def configure_connection(
    conn: sqlite3.Connection,
    busy_timeout_ms: int = 60000,
) -> None:
    """Busy timeout + WAL so web dashboard and trader loop can share the DB."""
    try:
        conn.execute('PRAGMA busy_timeout=%d' % int(busy_timeout_ms))
    except Exception:
        pass
    try:
        conn.execute('PRAGMA journal_mode=WAL')
    except Exception:
        pass
    try:
        conn.execute('PRAGMA synchronous=NORMAL')
    except Exception:
        pass


def get_connection(timeout: float = 60.0, busy_timeout_ms: int = 60000) -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=float(timeout))
    configure_connection(conn, busy_timeout_ms=int(busy_timeout_ms))
    return conn


def get_sessions_connection(
    timeout: float = 5.0,
    busy_timeout_ms: int = 5000,
) -> sqlite3.Connection:
    path = _sessions_db_path()
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    conn = sqlite3.connect(path, timeout=float(timeout))
    configure_connection(conn, busy_timeout_ms=int(busy_timeout_ms))
    return conn


_SESSIONS_TABLE_READY = False


def init_sessions_table() -> None:
    """Ensure the dedicated sessions DB exists (and migrate legacy rows once)."""
    global _SESSIONS_TABLE_READY
    if _SESSIONS_TABLE_READY:
        return
    conn = get_sessions_connection()
    try:
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                read_only INTEGER NOT NULL DEFAULT 0
            )
            '''
        )
        conn.commit()
        _ensure_sessions_read_only_column(conn)
        try:
            n = conn.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]
        except Exception:
            n = 0
        if not n:
            _migrate_sessions_from_main(conn)
            _ensure_sessions_read_only_column(conn)
    finally:
        conn.close()
    _SESSIONS_TABLE_READY = True


def _ensure_sessions_read_only_column(conn: sqlite3.Connection) -> None:
    """Add sessions.read_only for DBs created before the view-only login."""
    try:
        cols = [r[1] for r in conn.execute('PRAGMA table_info(sessions)').fetchall()]
    except Exception:
        return
    if 'read_only' in cols:
        return
    try:
        conn.execute(
            'ALTER TABLE sessions ADD COLUMN read_only INTEGER NOT NULL DEFAULT 0'
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass


def _migrate_sessions_from_main(sessions_conn: sqlite3.Connection) -> None:
    """Copy legacy sessions rows out of market_data.db if present."""
    try:
        main = get_connection(timeout=2.0, busy_timeout_ms=2000)
    except Exception:
        return
    try:
        try:
            rows = main.execute(
                '''
                SELECT token_hash, user_id, created_at, expires_at
                FROM sessions
                '''
            ).fetchall()
        except sqlite3.OperationalError:
            return
        if not rows:
            return
        sessions_conn.executemany(
            '''
            INSERT OR IGNORE INTO sessions
                (token_hash, user_id, created_at, expires_at)
            VALUES (?, ?, ?, ?)
            ''',
            rows,
        )
        sessions_conn.commit()
        print('Migrated %d session(s) to %s' % (len(rows), _sessions_db_path()))
    except Exception as e:
        print('Warning: session migration skipped: %s' % e)
    finally:
        try:
            main.close()
        except Exception:
            pass


def session_days() -> int:
    return int(getattr(config, 'SESSION_DAYS', 30))


def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    """Return 'scrypt$N$r$p$salt_hex$hash_hex'."""
    if salt is None:
        salt = os.urandom(16)
    n, r, p = 2 ** 14, 8, 1
    dk = hashlib.scrypt(
        password.encode('utf-8'),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=32,
    )
    return 'scrypt$%d$%d$%d$%s$%s' % (n, r, p, salt.hex(), dk.hex())


def verify_password(password: str, encoded: str) -> bool:
    try:
        parts = encoded.split('$')
        if len(parts) != 6 or parts[0] != 'scrypt':
            return False
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = bytes.fromhex(parts[4])
        expected = bytes.fromhex(parts[5])
        dk = hashlib.scrypt(
            password.encode('utf-8'),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
        )
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


def init_auth_tables(conn: Optional[sqlite3.Connection] = None) -> None:
    own = conn is None
    if own:
        conn = get_connection()
    assert conn is not None
    c = conn.cursor()
    c.execute(
        '''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            display_name TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            is_admin INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        '''
    )
    c.execute(
        '''
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        '''
    )
    c.execute(
        '''
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            active_filter TEXT NOT NULL DEFAULT 'safe',
            trade_dry_run INTEGER NOT NULL DEFAULT 1,
            minimum_cash REAL,
            minimum_liquidation_value REAL,
            order_amount_dollars REAL,
            schwab_tokens_db TEXT,
            updated_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        '''
    )
    _ensure_user_settings_columns(conn)
    if own:
        conn.commit()
        conn.close()


def _ensure_user_settings_columns(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the initial user_settings schema."""
    cols = {r[1] for r in conn.execute('PRAGMA table_info(user_settings)').fetchall()}
    if 'buy_limit_enabled' not in cols:
        conn.execute(
            'ALTER TABLE user_settings ADD COLUMN buy_limit_enabled INTEGER NOT NULL DEFAULT 0'
        )
    if 'buy_limit_pct' not in cols:
        conn.execute(
            'ALTER TABLE user_settings ADD COLUMN buy_limit_pct INTEGER'
        )


def ensure_user_settings(user_id: int, conn: Optional[sqlite3.Connection] = None) -> None:
    own = conn is None
    if own:
        conn = get_connection()
    assert conn is not None
    _ensure_user_settings_columns(conn)
    c = conn.cursor()
    c.execute('SELECT user_id FROM user_settings WHERE user_id = ?', (user_id,))
    if c.fetchone() is None:
        filt = getattr(config, 'WATCHLIST_FILTER_NAME', 'safe') or 'safe'
        # New accounts: Dry Run on; floors/buy size left NULL until Account setup.
        dry = 1
        c.execute(
            '''
            INSERT INTO user_settings (
                user_id, active_filter, trade_dry_run,
                minimum_cash, minimum_liquidation_value, order_amount_dollars,
                updated_at
            ) VALUES (?, ?, ?, NULL, NULL, NULL, ?)
            ''',
            (
                user_id,
                filt,
                dry,
                datetime.now().isoformat(timespec='seconds'),
            ),
        )
    if own:
        conn.commit()
        conn.close()


def create_user(
    username: str,
    password: str,
    display_name: Optional[str] = None,
    is_admin: bool = False,
) -> Dict[str, Any]:
    init_auth_tables()
    username = (username or '').strip().lower()
    if not username or not password:
        return {'ok': False, 'error': 'username and password required'}
    conn = get_connection()
    try:
        c = conn.cursor()
        now = datetime.now().isoformat(timespec='seconds')
        try:
            c.execute(
                '''
                INSERT INTO users (username, password_hash, display_name, is_active, is_admin, created_at)
                VALUES (?, ?, ?, 1, ?, ?)
                ''',
                (
                    username,
                    hash_password(password),
                    display_name or username,
                    1 if is_admin else 0,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            return {'ok': False, 'error': 'username already exists'}
        user_id = int(c.lastrowid)
        ensure_user_settings(user_id, conn=conn)
        conn.commit()
        return {'ok': True, 'user_id': user_id, 'username': username}
    finally:
        conn.close()


def set_password(username: str, password: str) -> Dict[str, Any]:
    """Set password for an existing user, or create admin jame-style owner if missing."""
    init_auth_tables()
    username = (username or '').strip().lower()
    if not username or not password:
        return {'ok': False, 'error': 'username and password required'}
    existing = get_user_by_username(username)
    if not existing:
        return create_user(username, password, display_name=username, is_admin=True)
    conn = get_connection()
    try:
        conn.execute(
            'UPDATE users SET password_hash=? WHERE username=?',
            (hash_password(password), username),
        )
        conn.commit()
        return {'ok': True, 'username': username, 'updated': True}
    finally:
        conn.close()


def change_own_password(
    user_id: int,
    current_password: str,
    new_password: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Confirm the signed-in user's current password.
    When new_password is omitted or empty, only verify.
    When provided, replace the hash (session stays valid).
    """
    init_auth_tables()
    profile = get_user_by_id(int(user_id))
    if not profile or not profile.get('is_active'):
        return {'ok': False, 'error': 'Account not found'}
    user = get_user_by_username(profile.get('username') or '')
    if not user or not verify_password(current_password or '', user.get('password_hash') or ''):
        return {'ok': False, 'error': 'Current password is incorrect'}
    if new_password is None or new_password == '':
        return {'ok': True, 'verified': True}
    if not str(new_password).strip():
        return {'ok': False, 'error': 'Enter a new password'}
    if str(new_password).strip().lower() == NEW_USER_LOGIN_TOKEN:
        return {'ok': False, 'error': 'Choose a real password (not "newuser")'}
    if verify_password(new_password, user.get('password_hash') or ''):
        return {'ok': False, 'error': 'New password must be different'}
    conn = get_connection()
    try:
        conn.execute(
            'UPDATE users SET password_hash=? WHERE id=?',
            (hash_password(new_password), int(user_id)),
        )
        conn.commit()
        return {'ok': True, 'updated': True, 'username': user.get('username')}
    finally:
        conn.close()


def bootstrap_default_users() -> None:
    """Create the owner account if no users exist (first multi-user install)."""
    init_auth_tables()
    conn = get_connection()
    try:
        n = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        if n and int(n) > 0:
            return
    finally:
        conn.close()
    username = getattr(config, 'BOOTSTRAP_USERNAME', None) or 'jame'
    password = getattr(config, 'BOOTSTRAP_PASSWORD', None) or ''
    display = getattr(config, 'BOOTSTRAP_DISPLAY_NAME', None) or username
    if not password:
        print(
            'Warning: no users yet and BOOTSTRAP_PASSWORD is empty — '
            'set it in config.py or run: python main.py --create-user ...'
        )
        return
    create_user(str(username), str(password), display_name=str(display), is_admin=True)


def get_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    # Short wait — auth should not stall behind trader-loop writers.
    conn = get_connection(timeout=5.0, busy_timeout_ms=5000)
    try:
        c = conn.cursor()
        c.execute(
            'SELECT id, username, display_name, is_active, is_admin FROM users WHERE id = ?',
            (user_id,),
        )
        row = c.fetchone()
        if not row:
            return None
        return {
            'id': row[0],
            'username': row[1],
            'display_name': row[2],
            'is_active': bool(row[3]),
            'is_admin': bool(row[4]),
        }
    finally:
        conn.close()


def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    conn = get_connection(timeout=5.0, busy_timeout_ms=5000)
    try:
        c = conn.cursor()
        c.execute(
            '''
            SELECT id, username, password_hash, display_name, is_active, is_admin
            FROM users WHERE username = ?
            ''',
            ((username or '').strip().lower(),),
        )
        row = c.fetchone()
        if not row:
            return None
        return {
            'id': row[0],
            'username': row[1],
            'password_hash': row[2],
            'display_name': row[3],
            'is_active': bool(row[4]),
            'is_admin': bool(row[5]),
        }
    finally:
        conn.close()


def list_active_users() -> List[Dict[str, Any]]:
    init_auth_tables()
    conn = get_connection()
    try:
        rows = conn.execute(
            '''
            SELECT id, username, display_name, is_admin
            FROM users WHERE is_active = 1 ORDER BY id
            '''
        ).fetchall()
        return [
            {
                'id': r[0],
                'username': r[1],
                'display_name': r[2],
                'is_admin': bool(r[3]),
            }
            for r in rows
        ]
    finally:
        conn.close()


# Login-page signup: unused username + this password reveals Set password.
NEW_USER_LOGIN_TOKEN = 'newuser'


def register_from_login(username: str, new_password: str) -> Dict[str, Any]:
    """
    Create a non-admin user from the login form (password field was NEW_USER_LOGIN_TOKEN).
    Returns {ok, user} or {ok: False, error}.
    """
    init_auth_tables()
    username = (username or '').strip().lower()
    new_password = new_password or ''
    if not username:
        return {'ok': False, 'error': 'Enter a username'}
    if get_user_by_username(username):
        return {
            'ok': False,
            'error': 'That username is already taken — sign in with your password',
        }
    if not new_password.strip():
        return {'ok': False, 'error': 'Enter a password in Set password'}
    if new_password.strip().lower() == NEW_USER_LOGIN_TOKEN:
        return {'ok': False, 'error': 'Choose a real password (not "newuser")'}
    created = create_user(
        username,
        new_password,
        display_name=username,
        is_admin=False,
    )
    if not created.get('ok'):
        return {'ok': False, 'error': created.get('error') or 'Could not create user'}
    user = get_user_by_username(username)
    if not user or not user.get('is_active'):
        return {'ok': False, 'error': 'Account created but sign-in failed — try signing in'}
    return {
        'ok': True,
        'user': {
            'id': user['id'],
            'username': user['username'],
            'display_name': user['display_name'],
            'is_admin': user['is_admin'],
        },
    }


def authenticate(username: str, password: str) -> Optional[Dict[str, Any]]:
    init_auth_tables()
    user = get_user_by_username(username)
    if not user or not user.get('is_active'):
        return None
    if not verify_password(password, user['password_hash']):
        return None
    return {
        'id': user['id'],
        'username': user['username'],
        'display_name': user['display_name'],
        'is_admin': user['is_admin'],
    }


def _secret_equals(left: str, right: str) -> bool:
    a = str(left or '')
    b = str(right or '')
    if len(a) != len(b):
        return False
    try:
        return hmac.compare_digest(a, b)
    except TypeError:
        return False


def authenticate_demo_viewer(username: str, password: str) -> Optional[Dict[str, Any]]:
    """
    Special password that opens DEMO_VIEWER_USERNAME's live dashboard as read-only.
    Does not use the owner's password hash. Empty DEMO_VIEWER_PASSWORD disables this.
    """
    want_user = (getattr(config, 'DEMO_VIEWER_USERNAME', None) or '').strip().lower()
    want_pass = str(getattr(config, 'DEMO_VIEWER_PASSWORD', None) or '')
    if not want_user or not want_pass:
        return None
    got_user = (username or '').strip().lower()
    got_pass = str(password or '')
    if not _secret_equals(got_user, want_user) or not _secret_equals(got_pass, want_pass):
        return None
    init_auth_tables()
    user = get_user_by_username(want_user)
    if not user or not user.get('is_active'):
        return None
    return {
        'id': user['id'],
        'username': user['username'],
        'display_name': user['display_name'],
        'is_admin': user['is_admin'],
        'read_only': True,
    }


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def create_session(user_id: int, read_only: bool = False) -> str:
    init_sessions_table()
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=session_days())
    conn = get_sessions_connection()
    try:
        try:
            conn.execute(
                '''
                INSERT INTO sessions (token_hash, user_id, created_at, expires_at, read_only)
                VALUES (?, ?, ?, ?, ?)
                ''',
                (
                    _hash_token(token),
                    user_id,
                    now.isoformat(),
                    expires.isoformat(),
                    1 if read_only else 0,
                ),
            )
        except sqlite3.OperationalError:
            _ensure_sessions_read_only_column(conn)
            conn.execute(
                '''
                INSERT INTO sessions (token_hash, user_id, created_at, expires_at, read_only)
                VALUES (?, ?, ?, ?, ?)
                ''',
                (
                    _hash_token(token),
                    user_id,
                    now.isoformat(),
                    expires.isoformat(),
                    1 if read_only else 0,
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return token


def _load_session_row(conn: sqlite3.Connection, token_hash: str):
    try:
        return conn.execute(
            '''
            SELECT user_id, expires_at, read_only
            FROM sessions WHERE token_hash = ?
            ''',
            (token_hash,),
        ).fetchone()
    except sqlite3.OperationalError:
        row = conn.execute(
            'SELECT user_id, expires_at FROM sessions WHERE token_hash = ?',
            (token_hash,),
        ).fetchone()
        if not row:
            return None
        return (row[0], row[1], 0)


def user_from_session_token(token: Optional[str]) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    init_sessions_table()
    th = _hash_token(token)
    user_id = None  # type: Optional[int]
    read_only = False
    conn = get_sessions_connection()
    try:
        row = _load_session_row(conn, th)
        if not row:
            return None
        user_id, expires_at = int(row[0]), row[1]
        read_only = bool(row[2]) if len(row) > 2 else False
        try:
            exp = datetime.fromisoformat(str(expires_at))
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp < datetime.now(timezone.utc):
                conn.execute('DELETE FROM sessions WHERE token_hash = ?', (th,))
                conn.commit()
                return None
        except Exception:
            return None
    finally:
        conn.close()

    user = get_user_by_id(int(user_id))
    if not user or not user.get('is_active'):
        return None
    return {
        'id': int(user['id']),
        'username': user['username'],
        'display_name': user.get('display_name'),
        'is_admin': bool(user.get('is_admin')),
        'read_only': bool(read_only),
    }


def destroy_session(token: Optional[str]) -> None:
    """Best-effort session row delete. Cookie clear is enough for logout UX."""
    if not token:
        return
    try:
        init_sessions_table()
        conn = get_sessions_connection(timeout=2.0, busy_timeout_ms=2000)
        try:
            conn.execute(
                'DELETE FROM sessions WHERE token_hash = ?',
                (_hash_token(token),),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def get_user_settings(user_id: int) -> Dict[str, Any]:
    ensure_user_settings(user_id)
    try:
        import filter_builder as fb
        fb.init_filter_tables()
    except Exception:
        pass
    conn = get_connection()
    try:
        cols = [r[1] for r in conn.execute('PRAGMA table_info(user_settings)').fetchall()]
        has_setup = 'setup_complete' in cols
        has_buy_limit = 'buy_limit_enabled' in cols
        select_cols = [
            'active_filter', 'trade_dry_run', 'minimum_cash',
            'minimum_liquidation_value', 'order_amount_dollars', 'schwab_tokens_db',
        ]
        if has_setup:
            select_cols.append('setup_complete')
        if has_buy_limit:
            select_cols.extend(['buy_limit_enabled', 'buy_limit_pct'])
        row = conn.execute(
            'SELECT %s FROM user_settings WHERE user_id = ?' % ', '.join(select_cols),
            (user_id,),
        ).fetchone()
        if not row:
            return {
                'active_filter': 'safe',
                'trade_dry_run': True,
                'minimum_cash': float(getattr(config, 'MINIMUM_CASH', 10000.0)),
                'minimum_liquidation_value': float(
                    getattr(config, 'MINIMUM_LIQUIDATION_VALUE', 25000.0)
                ),
                'order_amount_dollars': float(
                    getattr(config, 'ORDER_AMOUNT_DOLLARS', 1000.0)
                ),
                'schwab_tokens_db': None,
                'setup_complete': False,
                'buy_limit_enabled': False,
                'buy_limit_pct': None,
            }
        idx = 6
        setup_complete = False
        if has_setup:
            setup_complete = bool(row[idx])
            idx += 1
        buy_limit_enabled = False
        buy_limit_pct = None
        if has_buy_limit:
            buy_limit_enabled = bool(row[idx])
            buy_limit_pct = row[idx + 1]
            if buy_limit_pct is not None:
                try:
                    buy_limit_pct = int(buy_limit_pct)
                except (TypeError, ValueError):
                    buy_limit_pct = None
        return {
            'active_filter': row[0] or 'safe',
            'trade_dry_run': bool(row[1]),
            'minimum_cash': row[2],
            'minimum_liquidation_value': row[3],
            'order_amount_dollars': row[4],
            'schwab_tokens_db': row[5],
            'setup_complete': setup_complete,
            'buy_limit_enabled': buy_limit_enabled,
            'buy_limit_pct': buy_limit_pct,
        }
    finally:
        conn.close()


def update_user_settings(user_id: int, **kwargs) -> Dict[str, Any]:
    """Patch user_settings fields. Known keys only."""
    ensure_user_settings(user_id)
    try:
        import filter_builder as fb
        fb.init_filter_tables()
    except Exception:
        pass
    allowed = {
        'active_filter', 'trade_dry_run', 'minimum_cash',
        'minimum_liquidation_value', 'order_amount_dollars', 'setup_complete',
        'buy_limit_enabled', 'buy_limit_pct',
    }
    sets = []
    vals = []  # type: List[Any]
    for k, v in kwargs.items():
        if k not in allowed:
            continue
        if k in ('trade_dry_run', 'setup_complete', 'buy_limit_enabled'):
            v = 1 if v else 0
        if k == 'buy_limit_pct' and v is not None:
            v = int(v)
        sets.append('%s = ?' % k)
        vals.append(v)
    if not sets:
        return get_user_settings(user_id)
    sets.append('updated_at = ?')
    vals.append(datetime.now().isoformat(timespec='seconds'))
    vals.append(user_id)
    conn = get_connection()
    try:
        conn.execute(
            'UPDATE user_settings SET ' + ', '.join(sets) + ' WHERE user_id = ?',
            vals,
        )
        conn.commit()
    finally:
        conn.close()
    return get_user_settings(user_id)


def set_user_active_filter(user_id: int, filter_name: str) -> None:
    ensure_user_settings(user_id)
    conn = get_connection()
    try:
        conn.execute(
            '''
            UPDATE user_settings
            SET active_filter = ?, updated_at = ?
            WHERE user_id = ?
            ''',
            (filter_name, datetime.now().isoformat(timespec='seconds'), user_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_user_trade_dry_run(user_id: int, dry_run: bool) -> None:
    ensure_user_settings(user_id)
    conn = get_connection()
    try:
        conn.execute(
            '''
            UPDATE user_settings
            SET trade_dry_run = ?, updated_at = ?
            WHERE user_id = ?
            ''',
            (1 if dry_run else 0, datetime.now().isoformat(timespec='seconds'), user_id),
        )
        conn.commit()
    finally:
        conn.close()


def current_user_id() -> Optional[int]:
    if _CURRENT_USER_ID is not None:
        uid = _CURRENT_USER_ID.get()
        if uid is not None:
            return int(uid)
    return _FALLBACK_USER_ID


def require_user_id() -> int:
    uid = current_user_id()
    if uid is None:
        # Default to first admin / first user for CLI tools
        users = list_active_users()
        if not users:
            bootstrap_default_users()
            users = list_active_users()
        if not users:
            raise RuntimeError('No users configured')
        return int(users[0]['id'])
    return int(uid)


def set_current_user_id(user_id: Optional[int]) -> None:
    global _FALLBACK_USER_ID
    _FALLBACK_USER_ID = user_id
    if _CURRENT_USER_ID is not None:
        _CURRENT_USER_ID.set(user_id)


@contextmanager
def use_user(user_id: int) -> Iterator[int]:
    """Run a block as a given trading user (bot loop / dashboard request)."""
    prev_fb = _FALLBACK_USER_ID
    token = None
    if _CURRENT_USER_ID is not None:
        token = _CURRENT_USER_ID.set(user_id)
    set_current_user_id(user_id)
    try:
        yield user_id
    finally:
        if _CURRENT_USER_ID is not None and token is not None:
            _CURRENT_USER_ID.reset(token)
        set_current_user_id(prev_fb)


def scoped_flag_key(key: str) -> str:
    if key in GLOBAL_FLAG_KEYS:
        return key
    uid = current_user_id()
    if uid is None:
        return key
    # Already scoped?
    if key.startswith('u') and ':' in key:
        return key
    return 'u%s:%s' % (uid, key)


def scoped_job_name(job_name: str) -> str:
    if job_name in GLOBAL_JOB_NAMES:
        return job_name
    uid = require_user_id()
    if job_name.startswith('u') and ':' in job_name:
        return job_name
    return 'u%s:%s' % (uid, job_name)


def default_tokens_db_for_username(username: str) -> str:
    base = getattr(config, 'SCHWAB_TOKENS_DIR', '~/.schwabdev')
    return os.path.join(os.path.expanduser(base), 'tokens_%s.db' % username)


def tokens_db_for_user(user_id: int) -> str:
    settings = get_user_settings(user_id)
    custom = settings.get('schwab_tokens_db')
    if custom:
        return os.path.expanduser(str(custom))
    user = get_user_by_id(user_id)
    username = (user or {}).get('username') or ('user%d' % user_id)
    return default_tokens_db_for_username(username)
