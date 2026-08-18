"""
Main library module for stock trading system.
Contains all trading functions organized by category.
"""

import json
import logging
import os
import re
import sqlite3
import sys
import threading
import pandas as pd
import yfinance as yf
import requests
import time
from datetime import datetime, date, timedelta, timezone
from logging.handlers import RotatingFileHandler
from typing import List, Dict, Optional, Any, Tuple
import load_config
import config
import user_context as uc

# Schwab API — per-user clients; module globals mirror the current user context
SCHWAB_CLIENT = None
SCHWAB_AVAILABLE = False
SCHWAB_AUTH_NEEDED = False  # True when browser OAuth paste-back is required
_SCHWAB_CLIENTS = {}  # type: Dict[int, Any]
_SCHWAB_AVAILABLE = {}  # type: Dict[int, bool]
_SCHWAB_AUTH_NEEDED = {}  # type: Dict[int, bool]


def _uid() -> int:
    """Current trading / dashboard user id (defaults to first user if unset)."""
    return uc.require_user_id()


def _schwab_tokens_db_path(user_id: Optional[int] = None) -> str:
    uid = int(user_id) if user_id is not None else _uid()
    return uc.tokens_db_for_user(uid)


def _sync_schwab_globals(user_id: Optional[int] = None) -> None:
    """Point SCHWAB_* module globals at the given (or current) user."""
    global SCHWAB_CLIENT, SCHWAB_AVAILABLE, SCHWAB_AUTH_NEEDED
    uid = int(user_id) if user_id is not None else _uid()
    SCHWAB_CLIENT = _SCHWAB_CLIENTS.get(uid)
    SCHWAB_AVAILABLE = bool(_SCHWAB_AVAILABLE.get(uid, False))
    SCHWAB_AUTH_NEEDED = bool(_SCHWAB_AUTH_NEEDED.get(uid, False))


def _schwab_refuse_interactive_auth(auth_url: str) -> str:
    """call_on_auth for headless init — never block on input()."""
    uid = _uid()
    _SCHWAB_AUTH_NEEDED[uid] = True
    _sync_schwab_globals(uid)
    raise RuntimeError(
        'Schwab interactive login required (use Actions → Schwab reconnect)'
    )


def get_schwab_authorize_url() -> str:
    """Schwab OAuth authorize URL (same as schwabdev builds)."""
    import urllib.parse
    client_id = getattr(config, 'SCHWAB_API_KEY', '') or ''
    redirect = getattr(config, 'SCHWAB_REDIRECT_URI', 'https://127.0.0.1') or 'https://127.0.0.1'
    q = urllib.parse.urlencode({
        'client_id': client_id,
        'redirect_uri': redirect,
    })
    return 'https://api.schwabapi.com/v1/oauth/authorize?' + q


def _read_schwab_token_issued(column: str) -> Optional[datetime]:
    """Read a *_issued timestamp from schwabdev tokens DB, or None."""
    path = _schwab_tokens_db_path()
    if not os.path.isfile(path):
        return None
    if column not in ('refresh_token_issued', 'access_token_issued'):
        return None
    try:
        conn = sqlite3.connect(path)
        try:
            row = conn.execute(
                'SELECT %s FROM schwabdev LIMIT 1' % column
            ).fetchone()
        finally:
            conn.close()
        if not row or not row[0]:
            return None
        issued = datetime.fromisoformat(str(row[0]))
        if issued.tzinfo is None:
            issued = issued.replace(tzinfo=timezone.utc)
        return issued
    except Exception:
        return None


def _read_schwab_refresh_issued() -> Optional[datetime]:
    """Read refresh_token_issued from schwabdev tokens DB, or None."""
    return _read_schwab_token_issued('refresh_token_issued')


def _read_schwab_access_issued() -> Optional[datetime]:
    """Read access_token_issued from schwabdev tokens DB, or None."""
    return _read_schwab_token_issued('access_token_issued')


def maybe_log_schwab_access_refresh() -> None:
    """Log once per new access-token issue time (Schwab API auto-refresh ~30 min)."""
    issued = _read_schwab_access_issued()
    if issued is None:
        return
    key = issued.isoformat()
    prev = get_runtime_flag('schwab_access_refresh_logged_for')
    if prev == key:
        return
    set_runtime_flag('schwab_access_refresh_logged_for', key)
    if not prev:
        # First observation this install — don't spam "refreshed" on cold start
        return
    try:
        log_event(
            'web',
            'Schwab API access token refreshed',
            detail={'access_token_issued': key},
        )
    except Exception:
        pass


def maybe_log_schwab_auth_alerts(auth: Optional[Dict[str, Any]] = None) -> None:
    """
    One WARN when entering the 48h window; one ISSUE when refresh is expired.
    Each keyed to refresh_expires_at so reconnect (new expiry) can alert again later.
    """
    auth = auth or get_schwab_auth_status()
    if not isinstance(auth, dict):
        return
    hours = auth.get('hours_left')
    expiry_key = str(auth.get('refresh_expires_at') or 'missing')

    if not auth.get('warn'):
        return

    # Expired / missing tokens → ISSUE (once per expiry identity)
    if hours is None or hours <= 0:
        flagged = get_runtime_flag('schwab_auth_issue_logged_for')
        if flagged != expiry_key:
            set_runtime_flag('schwab_auth_issue_logged_for', expiry_key)
            msg = (
                'Schwab API credentials expired — reconnect via Actions '
                'before live trades can resume'
            )
            if hours is None:
                msg = (
                    'Schwab API credentials missing — reconnect via Actions '
                    'before live trades can resume'
                )
            log_event('web', msg, level='issue', detail=auth)
        return

    # Inside warn window but still valid → WARN once
    flagged_w = get_runtime_flag('schwab_auth_warn_logged_for')
    if flagged_w != expiry_key:
        set_runtime_flag('schwab_auth_warn_logged_for', expiry_key)
        log_event(
            'web',
            'Schwab login expires in %.0f hours — reconnect via Actions' % hours,
            level='warn',
            detail=auth,
        )


def get_schwab_auth_status() -> Dict[str, Any]:
    """
    Dashboard / Actions status for Schwab OAuth.
    hours_left is based on refresh_token_issued + SCHWAB_REFRESH_TOKEN_DAYS.

    warn / Actions badge: only when there is no token, refresh is expired, or
    hours_left is within SCHWAB_AUTH_WARN_HOURS — not merely because this process
    failed to construct a Client while tokens on disk are still valid.
    """
    warn_hours = float(getattr(config, 'SCHWAB_AUTH_WARN_HOURS', 48))
    snooze_hours = float(getattr(config, 'SCHWAB_AUTH_SNOOZE_HOURS', 4))
    refresh_days = float(getattr(config, 'SCHWAB_REFRESH_TOKEN_DAYS', 7))
    redirect = getattr(config, 'SCHWAB_REDIRECT_URI', 'https://127.0.0.1')
    issued = _read_schwab_refresh_issued()
    now = datetime.now(timezone.utc)
    expires_at = None  # type: Optional[datetime]
    hours_left = None  # type: Optional[float]
    if issued is not None:
        expires_at = issued + timedelta(days=refresh_days)
        hours_left = (expires_at - now).total_seconds() / 3600.0

    token_missing = issued is None
    expired = hours_left is not None and hours_left <= 0
    in_warn_window = hours_left is not None and 0 < hours_left <= warn_hours

    # Badge / banner / urgent card — clock only (plus never-authenticated)
    warn = bool(token_missing or expired or in_warn_window)

    uid_for_auth = _uid()
    auth_needed = bool(_SCHWAB_AUTH_NEEDED.get(uid_for_auth, SCHWAB_AUTH_NEEDED))
    # Human login required when refresh is dead / missing (not when Client just needs re-init)
    needs_login = bool(token_missing or expired or auth_needed)
    if hours_left is not None and hours_left > 0:
        # Valid refresh on disk: clear sticky needs_login from a prior failed init
        needs_login = False

    if token_missing or expired:
        state = 'disconnected'
    elif in_warn_window:
        state = 'expiring'
    else:
        state = 'connected'

    uid = _uid()
    _sync_schwab_globals(uid)
    return {
        'available': bool(_SCHWAB_AVAILABLE.get(uid, SCHWAB_AVAILABLE)),
        'needs_login': needs_login,
        'warn': warn,
        'state': state,
        'refresh_issued_at': issued.isoformat() if issued else None,
        'refresh_expires_at': expires_at.isoformat() if expires_at else None,
        'hours_left': hours_left,
        'warn_hours': warn_hours,
        'snooze_hours': snooze_hours,
        'redirect_uri': redirect,
        'authorize_url': get_schwab_authorize_url(),
        'tokens_db': _schwab_tokens_db_path(uid),
        'user_id': uid,
    }


def _schwab_tokens_row_present(tokens_path: str) -> bool:
    """True when tokens_*.db has a schwabdev row (avoid schwabdev empty-DB auth spam)."""
    if not tokens_path or not os.path.isfile(tokens_path):
        return False
    try:
        conn = sqlite3.connect(tokens_path, timeout=2.0)
        try:
            row = conn.execute('SELECT 1 FROM schwabdev LIMIT 1').fetchone()
        finally:
            conn.close()
        return row is not None
    except Exception:
        return False


def initialize_schwab_client(interactive: bool = False, user_id: Optional[int] = None) -> bool:
    """
    Initialize or re-initialize the Schwab API client for a user.
    interactive=False (default): refuse browser/input OAuth — set SCHWAB_AUTH_NEEDED.
    interactive=True: allow schwabdev default paste flow (local terminal only).
    """
    uid = int(user_id) if user_id is not None else _uid()
    try:
        import schwabdev
    except ImportError:
        print("Error: schwabdev library not installed. Install with: pip install schwabdev")
        _SCHWAB_CLIENTS[uid] = None
        _SCHWAB_AVAILABLE[uid] = False
        _SCHWAB_AUTH_NEEDED[uid] = True
        _sync_schwab_globals(uid)
        return False

    call_on_auth = None if interactive else _schwab_refuse_interactive_auth
    tokens_path = _schwab_tokens_db_path(uid)
    try:
        os.makedirs(os.path.dirname(tokens_path) or '.', exist_ok=True)
    except Exception:
        pass
    # Headless: do not construct Client on an empty token DB — schwabdev prints
    # "Could not load tokens" / "refresh token has expired" and starts auth noise.
    if not interactive and not _schwab_tokens_row_present(tokens_path):
        _SCHWAB_CLIENTS[uid] = None
        _SCHWAB_AVAILABLE[uid] = False
        _SCHWAB_AUTH_NEEDED[uid] = True
        _sync_schwab_globals(uid)
        try:
            uname = None
            for u in uc.list_active_users():
                if int(u['id']) == uid:
                    uname = u.get('username') or str(uid)
                    break
            print_loop_status(
                'No Schwab tokens for %s — reconnect in dashboard'
                % (uname or ('user %s' % uid))
            )
        except Exception:
            pass
        return False
    try:
        client = schwabdev.Client(
            config.SCHWAB_API_KEY,
            config.SCHWAB_API_SECRET,
            config.SCHWAB_REDIRECT_URI,
            tokens_db=tokens_path,
            call_on_auth=call_on_auth,
        )
        _SCHWAB_CLIENTS[uid] = client
        _SCHWAB_AVAILABLE[uid] = True
        _SCHWAB_AUTH_NEEDED[uid] = False
        _sync_schwab_globals(uid)
        print("Schwab API client initialized for user_id=%s." % uid)
        return True
    except Exception as e:
        _SCHWAB_CLIENTS[uid] = None
        _SCHWAB_AVAILABLE[uid] = False
        msg = str(e).lower()
        if 'interactive login required' in msg:
            _SCHWAB_AUTH_NEEDED[uid] = True
        _sync_schwab_globals(uid)
        print(f"Warning: Could not initialize Schwab client: {e}")
        return False


def _parse_schwab_oauth_callback(callback_url: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Parse pasted redirect URL or raw code.
    Returns (code, error). error is set when parsing fails.
    """
    import urllib.parse
    raw = (callback_url or '').strip()
    if not raw:
        return None, 'callback_url required'
    redirect = getattr(config, 'SCHWAB_REDIRECT_URI', 'https://127.0.0.1') or 'https://127.0.0.1'
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme:
        if not raw.lower().startswith(redirect.lower()):
            return None, (
                'Callback URL must start with configured redirect URI (%s)' % redirect
            )
        code = urllib.parse.parse_qs(parsed.query).get('code', [None])[0]
        if not code:
            return None, 'No code= parameter in callback URL'
        return code, None
    code = urllib.parse.unquote(raw)
    if len(code) < 10:
        return None, 'Paste the full redirect URL including code='
    return code, None


def _exchange_schwab_authorization_code(code: str) -> Dict[str, Any]:
    """
    Exchange a one-time OAuth authorization code for access/refresh tokens.
    Raises RuntimeError with a user-facing message on failure.
    """
    import base64
    import urllib.parse
    # Schwab sometimes returns the code still percent-encoded in odd pastes
    code = urllib.parse.unquote(str(code or '').strip())
    if not code:
        raise RuntimeError('Missing authorization code')
    redirect = getattr(config, 'SCHWAB_REDIRECT_URI', 'https://127.0.0.1') or 'https://127.0.0.1'
    headers = {
        'Authorization': 'Basic '
        + base64.b64encode(
            ('%s:%s' % (config.SCHWAB_API_KEY, config.SCHWAB_API_SECRET)).encode('utf-8')
        ).decode('utf-8'),
        'Content-Type': 'application/x-www-form-urlencoded',
    }
    data = {
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': redirect,
    }
    try:
        resp = requests.post(
            'https://api.schwabapi.com/v1/oauth/token',
            headers=headers,
            data=data,
            timeout=30,
        )
    except requests.RequestException as e:
        raise RuntimeError('Network error talking to Schwab: %s' % e)
    if not resp.ok:
        body = (resp.text or '').lower()
        if 'invalid_grant' in body or 'expired' in body or 'revoked' in body:
            raise RuntimeError(
                'Authorization code invalid or expired (~30s). '
                'Open Schwab login again and paste the full address-bar URL quickly.'
            )
        raise RuntimeError(
            'Schwab token exchange failed (HTTP %s). Check app key/secret and try again.'
            % resp.status_code
        )
    try:
        payload = resp.json()
    except Exception:
        raise RuntimeError('Schwab token response was not JSON')
    if not isinstance(payload, dict):
        raise RuntimeError('Schwab token response was not an object')
    if not payload.get('access_token') or not payload.get('refresh_token'):
        raise RuntimeError('Schwab token response missing access_token/refresh_token')
    return payload


def _persist_schwab_token_response(tokens_path: str, token_dictionary: Dict[str, Any]) -> None:
    """Write schwabdev-compatible tokens.db row from an OAuth token JSON payload."""
    now = datetime.now(timezone.utc)
    access = str(token_dictionary.get('access_token') or '')
    refresh = str(token_dictionary.get('refresh_token') or '')
    id_token = str(token_dictionary.get('id_token') or '')
    expires_in = int(token_dictionary.get('expires_in') or 1800)
    token_type = str(token_dictionary.get('token_type') or 'Bearer')
    scope = str(token_dictionary.get('scope') or 'api')
    try:
        os.makedirs(os.path.dirname(tokens_path) or '.', exist_ok=True)
    except Exception:
        pass
    conn = sqlite3.connect(tokens_path, timeout=30)
    try:
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS schwabdev (
                access_token_issued TEXT NOT NULL,
                refresh_token_issued TEXT NOT NULL,
                access_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                id_token TEXT NOT NULL,
                expires_in INTEGER,
                token_type TEXT,
                scope TEXT
            )
            '''
        )
        conn.execute('DELETE FROM schwabdev')
        conn.execute(
            '''
            INSERT INTO schwabdev (
                access_token_issued, refresh_token_issued,
                access_token, refresh_token, id_token,
                expires_in, token_type, scope
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                now.isoformat(),
                now.isoformat(),
                access,
                refresh,
                id_token,
                expires_in,
                token_type,
                scope,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def complete_schwab_oauth(
    callback_url: str,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Exchange a pasted Schwab redirect URL (…?code=…) for tokens and re-init the client.
    Forces a new refresh token even if one is still valid (early reconnect).
    Tokens are written to the current (or given) user's token DB only.

    Exchanges the code ourselves first — schwabdev 3.x crashes with
    "'bool' object has no attribute 'get'" when the code is expired/invalid.
    """
    uid = int(user_id) if user_id is not None else _uid()
    with uc.use_user(uid):
        code, parse_err = _parse_schwab_oauth_callback(callback_url)
        if parse_err:
            return {'ok': False, 'error': parse_err}

        tokens_path = _schwab_tokens_db_path(uid)
        try:
            token_payload = _exchange_schwab_authorization_code(code)
            _persist_schwab_token_response(tokens_path, token_payload)
        except Exception as e:
            _SCHWAB_CLIENTS[uid] = None
            _SCHWAB_AVAILABLE[uid] = False
            _SCHWAB_AUTH_NEEDED[uid] = True
            _sync_schwab_globals(uid)
            err = str(e)
            # schwabdev legacy path (if still hit elsewhere)
            if (
                "has no attribute 'get'" in err
                or ("bool" in err and "get" in err)
            ):
                err = (
                    'Authorization code invalid or expired (~30s). '
                    'Open Schwab login again and paste the full address-bar URL quickly.'
                )
            return {'ok': False, 'error': err}

        # Load Client from the fresh tokens.db (no interactive auth callback)
        if not initialize_schwab_client(interactive=False, user_id=uid):
            _SCHWAB_AUTH_NEEDED[uid] = True
            _sync_schwab_globals(uid)
            return {
                'ok': False,
                'error': (
                    'Tokens saved but client failed to initialize — '
                    'restart the dashboard / trader loop and try again.'
                ),
            }
        status = get_schwab_auth_status()
        if not isinstance(status, dict):
            status = {}
        # Return to the browser first in spirit: never let logging/flags block the
        # HTTP response when market_data.db is locked by main.py --loop.
        account_snap = None
        try:
            acct = get_account_info() or {}
            if account_info_usable(acct):
                account_snap = {
                    'cash': float(acct.get('cash') or 0.0),
                    'liquidation_value': float(acct.get('liquidation_value') or 0.0),
                    'as_of': datetime.now().isoformat(timespec='seconds'),
                }
        except Exception:
            account_snap = None
        if account_snap:
            try:
                record_account_snapshot(note='schwab_oauth')
            except Exception:
                pass
        result = {
            'ok': True,
            'schwab': status,
            'message': 'Connected — refresh token renewed (~7 days)',
            'account': account_snap,
            'bounds': compute_setup_bounds(account_snap) if account_snap else None,
            'onboarding_stage': get_onboarding_stage(),
        }
        try:
            log_event(
                'web',
                'Schwab API refresh OK — credentials renewed (~7 days)',
                detail={
                    'refresh_expires_at': status.get('refresh_expires_at'),
                    'hours_left': status.get('hours_left'),
                },
            )
        except Exception:
            pass
        try:
            set_runtime_flag(
                'schwab_auth_warn_logged_for', '',
                timeout=1.0, busy_timeout_ms=500, retries=1,
            )
            set_runtime_flag(
                'schwab_auth_issue_logged_for', '',
                timeout=1.0, busy_timeout_ms=500, retries=1,
            )
        except Exception:
            pass
        return result


def maybe_reinit_schwab_client(user_id: Optional[int] = None) -> bool:
    """If Schwab is down for this user, try a non-interactive re-init."""
    uid = int(user_id) if user_id is not None else _uid()
    if _SCHWAB_AVAILABLE.get(uid) and _SCHWAB_CLIENTS.get(uid) is not None:
        _sync_schwab_globals(uid)
        return True
    return initialize_schwab_client(interactive=False, user_id=uid)


# ============================================================================
# Database Functions
# ============================================================================

_DATABASE_READY = False
_DB_INIT_LOCK = threading.Lock()


def mark_database_ready_if_present() -> bool:
    """
    If market_data.db already has core tables, mark init complete without DDL.
    Used when full init_database cannot get a write lock (trader loop busy).
    """
    global _DATABASE_READY
    if _DATABASE_READY:
        return True
    try:
        conn = sqlite3.connect(config.DATABASE_PATH, timeout=1.0)
        try:
            uc.configure_connection(conn, busy_timeout_ms=1000)
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='positions'"
            ).fetchone()
        finally:
            conn.close()
        if row:
            _DATABASE_READY = True
            print(
                'Database already present at %s (skipped locked full init)'
                % config.DATABASE_PATH
            )
            try:
                _ensure_fundamentals_side_db()
            except Exception as e2:
                print('Warning: fundamentals side DB setup: %s' % e2)
            return True
    except Exception as e:
        print('Warning: mark_database_ready_if_present: %s' % e)
    return False


def init_database(
    timeout: float = 60.0,
    busy_timeout_ms: int = 60000,
    init_schwab: bool = True,
):
    """Create database tables if they don't exist (once per process)."""
    global _DATABASE_READY
    if _DATABASE_READY:
        return
    with _DB_INIT_LOCK:
        if _DATABASE_READY:
            return
        try:
            _init_database_unlocked(
                timeout=timeout,
                busy_timeout_ms=busy_timeout_ms,
                init_schwab=init_schwab,
            )
        except sqlite3.OperationalError as e:
            if 'locked' in str(e).lower() and mark_database_ready_if_present():
                if init_schwab:
                    try:
                        _ensure_schwab_initialized_once()
                    except Exception as e2:
                        print('Warning: Schwab client init: %s' % e2)
                return
            raise


def _init_database_unlocked(
    timeout: float = 60.0,
    busy_timeout_ms: int = 60000,
    init_schwab: bool = True,
):
    """Inner init — caller must hold _DB_INIT_LOCK and check _DATABASE_READY."""
    global _DATABASE_READY
    conn = sqlite3.connect(config.DATABASE_PATH, timeout=float(timeout))
    uc.configure_connection(conn, busy_timeout_ms=int(busy_timeout_ms))
    try:
        conn.execute('PRAGMA busy_timeout=%d' % int(busy_timeout_ms))
    except Exception:
        pass
    cursor = conn.cursor()
    
    # Create fundamentals table (1 row per stock)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS fundamentals (
            ticker TEXT PRIMARY KEY,
            pe_ratio REAL,
            market_cap INTEGER,
            sector TEXT,
            avg_volume INTEGER,
            beta REAL,
            short_float REAL,
            current_price REAL,
            target_price REAL,
            -- Valuation metrics
            forward_pe REAL,
            peg_ratio REAL,
            price_to_book REAL,
            price_to_sales REAL,
            enterprise_to_revenue REAL,
            enterprise_to_ebitda REAL,
            -- Financial health
            debt_to_equity REAL,
            current_ratio REAL,
            quick_ratio REAL,
            total_cash INTEGER,
            total_debt INTEGER,
            total_revenue INTEGER,
            gross_profits INTEGER,
            free_cashflow INTEGER,
            operating_cashflow INTEGER,
            -- Growth metrics
            revenue_growth REAL,
            earnings_growth REAL,
            earnings_quarterly_growth REAL,
            profit_margins REAL,
            gross_margins REAL,
            operating_margins REAL,
            -- Dividends
            dividend_rate REAL,
            dividend_yield REAL,
            -- Market data
            previous_close REAL,
            week_52_high REAL,
            week_52_low REAL,
            week_52_change REAL,
            -- Company info
            full_time_employees INTEGER,
            -- Ownership
            held_percent_institutions REAL,
            -- Additional numeric fields - moving averages
            fifty_day_average REAL,
            two_hundred_day_average REAL,
            -- Additional numeric fields - analyst data
            recommendation_mean REAL,
            number_of_analyst_opinions INTEGER,
            target_high_price REAL,
            target_low_price REAL,
            -- Additional numeric fields - shares
            shares_outstanding INTEGER,
            float_shares INTEGER,
            shares_short INTEGER,
            short_ratio REAL,
            -- Additional numeric fields - valuation
            book_value REAL,
            enterprise_value INTEGER,
            ebitda INTEGER,
            return_on_assets REAL,
            return_on_equity REAL,
            -- Additional numeric fields - dividends
            payout_ratio REAL,
            -- Additional numeric fields - volume
            average_volume_10days INTEGER,
            -- Additional numeric fields - ownership
            held_percent_insiders REAL,
            last_updated TEXT
        )
    ''')
    
    # Add new columns if they don't exist (for existing databases)
    # Check which columns exist and add missing ones
    cursor.execute("PRAGMA table_info(fundamentals)")
    existing_columns = [row[1] for row in cursor.fetchall()]
    
    required_columns = {
        'avg_volume': 'INTEGER',
        'beta': 'REAL',
        'short_float': 'REAL',
        'current_price': 'REAL',
        'target_price': 'REAL',
        # Valuation metrics
        'forward_pe': 'REAL',
        'peg_ratio': 'REAL',
        'price_to_book': 'REAL',
        'price_to_sales': 'REAL',
        'enterprise_to_revenue': 'REAL',
        'enterprise_to_ebitda': 'REAL',
        # Financial health
        'debt_to_equity': 'REAL',
        'current_ratio': 'REAL',
        'quick_ratio': 'REAL',
        'total_cash': 'INTEGER',
        'total_debt': 'INTEGER',
        'total_revenue': 'INTEGER',
        'gross_profits': 'INTEGER',
        'free_cashflow': 'INTEGER',
        'operating_cashflow': 'INTEGER',
        # Growth metrics
        'revenue_growth': 'REAL',
        'earnings_growth': 'REAL',
        'earnings_quarterly_growth': 'REAL',
        'profit_margins': 'REAL',
        'gross_margins': 'REAL',
        'operating_margins': 'REAL',
        # Dividends
        'dividend_rate': 'REAL',
        'dividend_yield': 'REAL',
        # Market data
        'previous_close': 'REAL',
        'week_52_high': 'REAL',
        'week_52_low': 'REAL',
        'week_52_change': 'REAL',
        # Company info
        'full_time_employees': 'INTEGER',
        # Ownership
        'held_percent_institutions': 'REAL',
        # Additional numeric fields - moving averages
        'fifty_day_average': 'REAL',
        'two_hundred_day_average': 'REAL',
        # Additional numeric fields - analyst data
        'recommendation_mean': 'REAL',
        'number_of_analyst_opinions': 'INTEGER',
        'target_high_price': 'REAL',
        'target_low_price': 'REAL',
        # Additional numeric fields - shares
        'shares_outstanding': 'INTEGER',
        'float_shares': 'INTEGER',
        'shares_short': 'INTEGER',
        'short_ratio': 'REAL',
        # Additional numeric fields - valuation
        'book_value': 'REAL',
        'enterprise_value': 'INTEGER',
        'ebitda': 'INTEGER',
        'return_on_assets': 'REAL',
        'return_on_equity': 'REAL',
        # Additional numeric fields - dividends
        'payout_ratio': 'REAL',
        # Additional numeric fields - volume
        'average_volume_10days': 'INTEGER',
        # Additional numeric fields - ownership
        'held_percent_insiders': 'REAL',
        'last_updated': 'TEXT'
    }
    
    # Dynamically discover additional numeric fields from yfinance (skip if schema rich)
    try:
        if len(existing_columns) < 40:
            sample_stock = yf.Ticker('AAPL')
            sample_info = sample_stock.info
            if sample_info:
                def camel_to_snake(name):
                    import re
                    # Handle names starting with numbers (e.g., "52WeekChange" -> "week_52_change")
                    if name and name[0].isdigit():
                        match = re.match(r'^(\d+)([A-Z].*)', name)
                        if match:
                            number_part = match.group(1)
                            rest = match.group(2)
                            # Convert rest to snake_case and append number
                            s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', rest)
                            s2 = re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1).lower()
                            return f'week_{number_part}_{s2}' if 'week' in s2.lower() else f'{s2}_{number_part}'
                    # Normal camelCase conversion
                    s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
                    return re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1).lower()
                
                # Fields we've already captured
                captured_fields = {
                    'trailingPE', 'marketCap', 'sector', 'averageVolume', 'beta', 'shortPercentOfFloat',
                    'currentPrice', 'targetMeanPrice', 'forwardPE', 'pegRatio', 'priceToBook',
                    'priceToSalesTrailing12Months', 'enterpriseToRevenue', 'enterpriseToEbitda',
                    'debtToEquity', 'currentRatio', 'quickRatio', 'totalCash', 'totalDebt',
                    'totalRevenue', 'grossProfits', 'freeCashflow', 'operatingCashflow',
                    'revenueGrowth', 'earningsGrowth', 'earningsQuarterlyGrowth', 'profitMargins',
                    'grossMargins', 'operatingMargins', 'dividendRate', 'dividendYield',
                    'previousClose', 'fiftyTwoWeekHigh', '52WeekHigh', 'fiftyTwoWeekLow', '52WeekLow',
                    'fiftyTwoWeekChange', '52WeekChange',  # 52-week change
                    'fullTimeEmployees', 'heldPercentInstitutions', 'fiftyDayAverage',
                    'twoHundredDayAverage', 'recommendationMean', 'numberOfAnalystOpinions',
                    'targetHighPrice', 'targetLowPrice', 'sharesOutstanding', 'floatShares',
                    'sharesShort', 'shortRatio', 'bookValue', 'enterpriseValue', 'ebitda',
                    'returnOnAssets', 'returnOnEquity', 'payoutRatio', 'averageVolume10days',
                    'heldPercentInsiders'
                }
                
                # Add any other numeric fields
                for key, value in sample_info.items():
                    if key not in captured_fields:
                        if isinstance(value, (int, float)) and value is not None:
                            snake_key = camel_to_snake(key)
                            # Ensure column name doesn't start with a number (SQL requirement)
                            if snake_key and snake_key[0].isdigit():
                                snake_key = 'field_' + snake_key
                            # Determine type (INTEGER for int, REAL for float)
                            col_type = 'INTEGER' if isinstance(value, int) else 'REAL'
                            if snake_key not in required_columns:
                                required_columns[snake_key] = col_type
    except Exception as e:
        print(f"Warning: Could not discover additional fields from yfinance: {e}")
    
    for col_name, col_type in required_columns.items():
        if col_name not in existing_columns:
            try:
                cursor.execute(f'ALTER TABLE fundamentals ADD COLUMN {col_name} {col_type}')
                print(f"Added missing column: {col_name}")
            except sqlite3.OperationalError as e:
                error_msg = str(e).lower()
                # Check if error is because column already exists (can happen in race conditions)
                if 'duplicate' in error_msg or 'already exists' in error_msg:
                    print(f"Column {col_name} already exists (skipping)")
                else:
                    print(f"Warning: Could not add column {col_name}: {e}")
    
    # Verify all required columns exist
    cursor.execute("PRAGMA table_info(fundamentals)")
    final_columns = [row[1] for row in cursor.fetchall()]
    missing = [col for col in required_columns.keys() if col not in final_columns]
    if missing:
        print(f"Warning: Some columns are still missing: {missing}")
    else:
        print("All required columns verified.")
    
    # Prices table removed - only fundamentals table is used
    # Historical price data is not stored, only current_price in fundamentals
    
    # Create positions table - actual holdings (synced from our orders + Schwab)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS positions (
            ticker TEXT PRIMARY KEY,
            date_purchased TEXT,
            shares_owned INTEGER NOT NULL DEFAULT 0,
            average_price REAL,
            market_value REAL,
            current_day_profit_loss REAL,
            day_pct REAL,
            long_open_profit_loss REAL,
            open_pct REAL
        )
    ''')
    # Add columns for P/L display and trail-stop state
    cursor.execute("PRAGMA table_info(positions)")
    pos_cols = [row[1] for row in cursor.fetchall()]
    for col, ctype in [
        ('day_pct', 'REAL'),
        ('open_pct', 'REAL'),
        ('purchased_at', 'TEXT'),       # ISO datetime for min-hold checks
        ('peak_gain_pct', 'REAL'),      # high-water unrealized gain vs purchase (fraction)
        ('stop_gain_pct', 'REAL'),      # ratcheting trail stop level (fraction vs purchase)
        ('trail_active', 'INTEGER'),    # 1 once peak hit TRAIL_ACTIVATE_PCT
        ('stop_order_id', 'TEXT'),      # Schwab resting STOP_LIMIT order id
        ('stop_order_price', 'REAL'),   # last submitted stop trigger
        ('stop_limit_price', 'REAL'),   # last submitted limit price
        ('stop_order_qty', 'INTEGER'),  # qty on that resting order
        ('stop_defer_logged', 'INTEGER'),  # 1 after same-day defer log (once)
    ]:
        if col not in pos_cols:
            try:
                cursor.execute(f"ALTER TABLE positions ADD COLUMN {col} {ctype}")
            except sqlite3.OperationalError:
                pass
    
    # Cash / equity snapshots for success tracking (cash primary)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS account_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            cash REAL,
            effective_cash REAL,
            liquidation_value REAL,
            total_value REAL,
            note TEXT
        )
    ''')
    
    # After a sell: block re-buying until cooldown OR discount unlocks (see rebuy_allowed).
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS rebuy_guards (
            ticker TEXT PRIMARY KEY,
            last_sell_price REAL,
            last_cost_basis REAL,
            last_sold_at TEXT
        )
    ''')
    
    # Create pending_orders table - unfilled buy orders (reduce effective cash until filled/cancelled)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pending_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            date_ordered TEXT NOT NULL,
            quantity_ordered INTEGER NOT NULL,
            order_amount_dollars REAL NOT NULL,
            order_id TEXT
        )
    ''')
    
    # Create watchlist table - stores stocks selected by filters with all yfinance + Schwab data
    # Get all columns from fundamentals table to include in watchlist
    cursor.execute("PRAGMA table_info(fundamentals)")
    fundamentals_columns = cursor.fetchall()
    
    # Build watchlist table schema
    # Start with ticker as primary key
    watchlist_schema = ['ticker TEXT PRIMARY KEY']
    
    # Add all fundamentals columns (except ticker and last_updated)
    # ticker is already added as primary key, last_updated will be added separately for watchlist
    for col_info in fundamentals_columns:
        col_name = col_info[1]
        col_type = col_info[2]
        if col_name not in ('ticker', 'last_updated'):
            watchlist_schema.append(f'{col_name} {col_type}')
    
    # Add Schwab streaming data fields
    schwab_fields = [
        # Price data
        'schwab_price REAL', 'schwab_bid REAL', 'schwab_ask REAL', 'schwab_mark REAL',
        'schwab_open REAL', 'schwab_high REAL', 'schwab_low REAL', 'schwab_previous_close REAL',
        # Change data
        'schwab_net_change REAL', 'schwab_net_percent_change REAL', 'schwab_mark_change REAL',
        'schwab_mark_percent_change REAL', 'schwab_post_market_change REAL', 'schwab_post_market_percent_change REAL',
        # Volume data
        'schwab_volume INTEGER', 'schwab_bid_size INTEGER', 'schwab_ask_size INTEGER',
        # Market data
        'schwab_exchange TEXT', 'schwab_quote_time INTEGER', 'schwab_trade_time INTEGER',
        'schwab_market_status TEXT', 'schwab_realtime BOOLEAN',
        # 52-week data
        'schwab_week_52_high REAL', 'schwab_week_52_low REAL',
        # Extended hours
        'schwab_extended_last_price REAL', 'schwab_extended_volume INTEGER',
        'schwab_extended_bid REAL', 'schwab_extended_ask REAL',
        # Fundamental data from Schwab
        'schwab_pe_ratio REAL', 'schwab_dividend_yield REAL', 'schwab_eps REAL',
        'schwab_shares_outstanding INTEGER', 'schwab_avg_10_day_volume REAL', 'schwab_avg_1_year_volume REAL',
        # Dividend info
        'schwab_div_amount REAL', 'schwab_div_freq INTEGER', 'schwab_div_pay_amount REAL',
        'schwab_next_div_ex_date TEXT', 'schwab_next_div_pay_date TEXT',
        # Reference data
        'schwab_cusip TEXT', 'schwab_description TEXT', 'schwab_is_shortable BOOLEAN',
        'schwab_is_hard_to_borrow BOOLEAN',
        # Metadata
        'schwab_asset_main_type TEXT', 'schwab_asset_sub_type TEXT', 'schwab_quote_type TEXT',
        'schwab_timestamp TEXT'
    ]
    watchlist_schema.extend(schwab_fields)
    
    # Metadata fields only (ownership moved to positions table)
    watchlist_schema.extend([
        'date_added TEXT',
        'last_updated TEXT'
    ])
    
    # Create watchlist table
    # Check if table already exists to avoid duplicate column errors
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='watchlist'")
    table_exists = cursor.fetchone() is not None
    
    if not table_exists:
        try:
            create_watchlist_sql = f'''
                CREATE TABLE watchlist (
                    {', '.join(watchlist_schema)}
                )
            '''
            cursor.execute(create_watchlist_sql)
        except sqlite3.OperationalError as e:
            if 'duplicate column' in str(e).lower():
                print(f"Error: Duplicate column detected in watchlist schema. This shouldn't happen.")
                print(f"Error details: {e}")
                print("Please drop the watchlist table manually if it exists: DROP TABLE IF EXISTS watchlist;")
                raise
            else:
                raise
    else:
        # Table exists - check for missing columns and add them
        cursor.execute("PRAGMA table_info(watchlist)")
        existing_watchlist_columns = [row[1] for row in cursor.fetchall()]
        
        # Check for duplicate last_updated (shouldn't happen, but handle it)
        last_updated_count = existing_watchlist_columns.count('last_updated')
        if last_updated_count > 1:
            print("Warning: watchlist table has duplicate 'last_updated' columns.")
            print("This table needs to be recreated. Dropping and recreating watchlist table...")
            cursor.execute("DROP TABLE watchlist")
            create_watchlist_sql = f'''
                CREATE TABLE watchlist (
                    {', '.join(watchlist_schema)}
                )
            '''
            cursor.execute(create_watchlist_sql)
            print("✓ Recreated watchlist table")
        else:
            # Check for missing columns and add them
            for col_def in watchlist_schema:
                # Parse column definition: "column_name TYPE" or "column_name TYPE DEFAULT value"
                col_name = col_def.split()[0]
                if col_name not in existing_watchlist_columns:
                    try:
                        # Extract type and any constraints
                        col_parts = col_def.split()
                        if len(col_parts) >= 2:
                            col_type = col_parts[1]
                            # Add any additional constraints (DEFAULT, etc.)
                            constraints = ' '.join(col_parts[2:]) if len(col_parts) > 2 else ''
                            alter_sql = f'ALTER TABLE watchlist ADD COLUMN {col_name} {col_type}'
                            if constraints:
                                alter_sql += f' {constraints}'
                            cursor.execute(alter_sql)
                            print(f"Added missing column to watchlist: {col_name}")
                    except sqlite3.OperationalError as e:
                        error_msg = str(e).lower()
                        if 'duplicate' not in error_msg and 'already exists' not in error_msg:
                            print(f"Warning: Could not add column {col_name} to watchlist: {e}")
        
        # One-time migration: remove ownership columns from watchlist (moved to positions table)
        ownership_cols = ['shares_owned', 'purchase_price', 'date_purchased']
        for col in ownership_cols:
            if col in existing_watchlist_columns:
                try:
                    if col == 'shares_owned':
                        cursor.execute("UPDATE watchlist SET shares_owned = 0")
                    else:
                        cursor.execute(f"UPDATE watchlist SET {col} = NULL")
                except Exception:
                    pass
                try:
                    cursor.execute(f"ALTER TABLE watchlist DROP COLUMN {col}")
                    print(f"Migrated: dropped column watchlist.{col}")
                except sqlite3.OperationalError:
                    pass  # SQLite < 3.35: leave column in place but unused
    
    # Job run timestamps for crash-resumable scheduled work (e.g. weekly market refresh)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS job_runs (
            job_name TEXT PRIMARY KEY,
            interval_days REAL,
            last_started TEXT,
            last_completed TEXT,
            status TEXT,
            progress_note TEXT
        )
    ''')
    cursor.execute("PRAGMA table_info(job_runs)")
    job_cols = [row[1] for row in cursor.fetchall()]
    if 'progress_note' not in job_cols:
        try:
            cursor.execute('ALTER TABLE job_runs ADD COLUMN progress_note TEXT')
        except sqlite3.OperationalError:
            pass

    # Structured events for the web Log page
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS event_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            level TEXT NOT NULL DEFAULT 'info',
            category TEXT NOT NULL,
            message TEXT NOT NULL,
            detail_json TEXT
        )
    ''')
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_event_log_ts ON event_log(ts DESC)'
    )

    # Future Actions page: queued commands for the trader loop
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS web_commands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            command TEXT NOT NULL,
            payload_json TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            finished_at TEXT,
            result_note TEXT
        )
    ''')

    # Runtime flags (e.g. buys_paused) readable by jobs + dashboard
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS runtime_flags (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        )
    ''')

    # Full buy/sell ledger for model evaluation (price, qty, time — not full fundamentals)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS trade_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            side TEXT NOT NULL,
            ticker TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            price REAL,
            dollars REAL,
            cost_basis_per_share REAL,
            realized_pl REAL,
            order_id TEXT,
            mode TEXT,
            note TEXT,
            scorecard TEXT
        )
    ''')
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_trade_history_ts ON trade_history(ts DESC)'
    )
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_trade_history_ticker ON trade_history(ticker)'
    )
    _ensure_column(cursor, 'trade_history', 'scorecard', 'TEXT')

    # Position book: book = sell-management; origin = scorecard eligibility
    # origin: legacy | algo_buy | enrolled
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS position_book (
            ticker TEXT PRIMARY KEY,
            book TEXT NOT NULL,
            tagged_at TEXT NOT NULL,
            enrolled_at TEXT,
            note TEXT,
            origin TEXT
        )
    ''')
    _ensure_column(cursor, 'position_book', 'origin', 'TEXT')

    # Multi-user: auth tables, user_id columns, migrate legacy rows → owner (jame)
    # Commit first so migration/bootstrap nested connections do not self-deadlock.
    conn.commit()
    try:
        from multi_user_migrate import run_multi_user_migration
        run_multi_user_migration(conn)
    except Exception as e:
        print('Warning: multi-user migration: %s' % e)
        try:
            conn.commit()
        except Exception:
            pass

    try:
        _rewrite_legacy_event_log(conn)
        conn.commit()
    except Exception as e:
        print('Warning: event_log cleanup: %s' % e)
        try:
            conn.commit()
        except Exception:
            pass

    try:
        conn.commit()
    except Exception:
        pass
    try:
        conn.close()
    except Exception:
        pass
    try:
        _ensure_fundamentals_side_db()
    except Exception as e:
        print('Warning: fundamentals side DB setup: %s' % e)
    _DATABASE_READY = True
    print(f"Database initialized at {config.DATABASE_PATH}")
    print('Fundamentals DB: %s' % fundamentals_db_path())
    if init_schwab:
        _ensure_schwab_initialized_once()


_SCHWAB_BOOTSTRAPPED = False


def _ensure_schwab_initialized_once() -> None:
    global _SCHWAB_BOOTSTRAPPED
    if _SCHWAB_BOOTSTRAPPED:
        return
    _SCHWAB_BOOTSTRAPPED = True
    try:
        import schwabdev  # noqa: F401
        initialize_schwab_client(interactive=False)
    except ImportError:
        print("Warning: schwabdev library not installed. Install with: pip install schwabdev")
    except Exception as e:
        print("Warning: Schwab client init: %s" % e)


def _ensure_column(cursor, table: str, column: str, decl: str) -> None:
    """Add a column to an existing SQLite table if missing."""
    cursor.execute('PRAGMA table_info(%s)' % table)
    cols = [row[1] for row in cursor.fetchall()]
    if column not in cols:
        cursor.execute('ALTER TABLE %s ADD COLUMN %s %s' % (table, column, decl))


def get_connection(timeout: float = 60.0, busy_timeout_ms: int = 60000):
    """Return a database connection (auto-scopes per-user tables to current user)."""
    import sql_user_scope
    return sql_user_scope.connect(
        config.DATABASE_PATH,
        timeout=timeout,
        busy_timeout_ms=busy_timeout_ms,
    )


def fundamentals_db_path() -> str:
    """Path to the Yahoo fundamentals side database."""
    override = getattr(config, 'FUNDAMENTALS_DATABASE_PATH', None)
    if override:
        return str(override)
    main = os.path.abspath(getattr(config, 'DATABASE_PATH', 'market_data.db'))
    root, ext = os.path.splitext(main)
    if root.endswith('market_data'):
        return root[: -len('market_data')] + 'market_fundamentals' + (ext or '.db')
    return root + '_fundamentals' + (ext or '.db')


def get_fundamentals_connection(
    timeout: float = 60.0,
    busy_timeout_ms: int = 60000,
) -> sqlite3.Connection:
    """Connection to Yahoo fundamentals DB (shared, not user-scoped)."""
    path = fundamentals_db_path()
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    conn = sqlite3.connect(path, timeout=float(timeout))
    uc.configure_connection(conn, busy_timeout_ms=int(busy_timeout_ms))
    return conn


def _db_is_locked(exc: BaseException) -> bool:
    """True when SQLite is contending with another writer (expected under --loop)."""
    return 'locked' in str(exc).lower()


_LOOP_LOCK_FP = None  # type: Optional[Any]


def trader_loop_lock_path() -> str:
    """Path for the exclusive --loop lock file (next to market_data.db)."""
    main = os.path.abspath(getattr(config, 'DATABASE_PATH', 'market_data.db'))
    return os.path.join(os.path.dirname(main) or '.', 'trader_loop.lock')


def acquire_trader_loop_lock() -> bool:
    """
    Ensure only one python main.py --loop runs at a time.
    Keeps the lock file open for the process lifetime.
    """
    global _LOOP_LOCK_FP
    if _LOOP_LOCK_FP is not None:
        return True
    path = trader_loop_lock_path()
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    fp = open(path, 'a+')
    try:
        fp.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, IOError, OverflowError):
        try:
            existing = fp.read().strip()
        except Exception:
            existing = ''
        fp.close()
        print_loop_status(
            'Another trader loop is already running%s — exit'
            % ((' (pid %s)' % existing) if existing else '')
        )
        return False
    try:
        fp.seek(0)
        fp.truncate()
        fp.write(str(os.getpid()))
        fp.flush()
    except Exception:
        pass
    _LOOP_LOCK_FP = fp

    def _release():
        global _LOOP_LOCK_FP
        f = _LOOP_LOCK_FP
        _LOOP_LOCK_FP = None
        if not f:
            return
        try:
            if os.name == 'nt':
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            f.close()
        except Exception:
            pass

    import atexit
    atexit.register(_release)
    return True


# When a due job fails (lock / transient), pull the next loop wake forward.
_EARLY_WAKE_AT = None  # type: Optional[datetime]


def request_early_wake(seconds: Optional[float] = None, reason: str = '') -> None:
    """Ask run_trader to wake sooner than the normal next-due schedule."""
    global _EARLY_WAKE_AT
    if seconds is None:
        seconds = float(getattr(config, 'JOB_RETRY_SOON_SECONDS', 30))
    at = datetime.now() + timedelta(seconds=max(5.0, float(seconds)))
    if _EARLY_WAKE_AT is None or at < _EARLY_WAKE_AT:
        _EARLY_WAKE_AT = at
    if reason:
        print_loop_status('Retry soon (%.0fs): %s' % (float(seconds), reason))


def take_early_wake(next_wake: Optional[datetime]) -> Optional[datetime]:
    """Merge any requested early wake into next_wake; clear the request."""
    global _EARLY_WAKE_AT
    early = _EARLY_WAKE_AT
    _EARLY_WAKE_AT = None
    if early is None:
        return next_wake
    if next_wake is None or early < next_wake:
        return early
    return next_wake


def _ensure_fundamentals_side_db() -> None:
    """
    Create market_fundamentals.db and one-time copy rows from legacy
    market_data.fundamentals when the side DB is empty.
    """
    fund_path = os.path.abspath(fundamentals_db_path())
    main_path = os.path.abspath(config.DATABASE_PATH)
    parent = os.path.dirname(fund_path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)

    if not os.path.isfile(main_path):
        # Brand-new install: create empty fundamentals schema on the side DB.
        fconn = get_fundamentals_connection()
        try:
            fconn.execute(
                '''
                CREATE TABLE IF NOT EXISTS fundamentals (
                    ticker TEXT PRIMARY KEY,
                    pe_ratio REAL,
                    market_cap INTEGER,
                    sector TEXT,
                    avg_volume INTEGER,
                    beta REAL,
                    short_float REAL,
                    current_price REAL,
                    target_price REAL,
                    last_updated TEXT
                )
                '''
            )
            fconn.commit()
        finally:
            fconn.close()
        return

    conn = sqlite3.connect(main_path, timeout=120.0)
    try:
        uc.configure_connection(conn, busy_timeout_ms=120000)
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='fundamentals'"
        ).fetchone()
        if not row or not row[0]:
            return
        conn.execute('ATTACH DATABASE ? AS funddb', (fund_path,))
        create_sql = re.sub(
            r'CREATE\s+TABLE\s+(IF\s+NOT\s+EXISTS\s+)?["\']?fundamentals["\']?',
            'CREATE TABLE IF NOT EXISTS funddb.fundamentals',
            str(row[0]),
            count=1,
            flags=re.I,
        )
        conn.execute(create_sql)
        main_cols = list(conn.execute('PRAGMA table_info(fundamentals)'))
        fund_cols = {
            r[1] for r in conn.execute('PRAGMA funddb.table_info(fundamentals)')
        }
        for r in main_cols:
            name, decl = r[1], r[2]
            if name not in fund_cols:
                try:
                    conn.execute(
                        'ALTER TABLE funddb.fundamentals ADD COLUMN %s %s'
                        % (name, decl or 'TEXT')
                    )
                except sqlite3.OperationalError:
                    pass
        fund_n = int(
            conn.execute('SELECT COUNT(*) FROM funddb.fundamentals').fetchone()[0]
            or 0
        )
        if fund_n == 0:
            main_n = int(
                conn.execute('SELECT COUNT(*) FROM main.fundamentals').fetchone()[0]
                or 0
            )
            if main_n > 0:
                col_names = [r[1] for r in main_cols]
                cols = ', '.join(col_names)
                conn.execute(
                    'INSERT INTO funddb.fundamentals (%s) SELECT %s FROM main.fundamentals'
                    % (cols, cols)
                )
                print(
                    'Migrated %d fundamentals row(s) -> %s'
                    % (main_n, fund_path)
                )
        conn.commit()
        try:
            conn.execute('DETACH DATABASE funddb')
        except Exception:
            pass
    finally:
        conn.close()


# ============================================================================
# File logging + structured events (web dashboard)
# ============================================================================

_FILE_LOGGING_READY = False
# When True, stdout still goes to the log file but not the terminal (loop mode).
_CONSOLE_QUIET = False


class _TeeStream(object):
    """Write to the original stream and a log file."""

    def __init__(self, primary, log_file, respect_quiet=False):
        self._primary = primary
        self._log_file = log_file
        self._respect_quiet = bool(respect_quiet)

    def write(self, data):
        if not (self._respect_quiet and _CONSOLE_QUIET):
            try:
                self._primary.write(data)
            except Exception:
                pass
        try:
            self._log_file.write(data)
            self._log_file.flush()
        except Exception:
            pass
        return len(data) if data is not None else 0

    def flush(self):
        try:
            self._primary.flush()
        except Exception:
            pass
        try:
            self._log_file.flush()
        except Exception:
            pass

    def isatty(self):
        try:
            return self._primary.isatty()
        except Exception:
            return False


def set_console_quiet(quiet: bool = True) -> None:
    """Mute verbose terminal stdout (log file still receives everything)."""
    global _CONSOLE_QUIET
    _CONSOLE_QUIET = bool(quiet)


def print_loop_status(msg: str) -> None:
    """
    Short progress line that always reaches the terminal, even in quiet mode.
    Also appended to the log file / tee.
    """
    text = str(msg)
    if not text.endswith('\n'):
        text = text + '\n'
    # Bypass quiet tee for the live console
    try:
        out = getattr(sys, '__stdout__', None) or sys.stdout
        if isinstance(out, _TeeStream):
            out = out._primary
        out.write(text)
        out.flush()
    except Exception:
        pass
    # Ensure log file gets it when stdout is teed + quiet
    try:
        if isinstance(sys.stdout, _TeeStream):
            sys.stdout._log_file.write(text)
            sys.stdout._log_file.flush()
    except Exception:
        pass


def setup_file_logging(log_path: Optional[str] = None) -> str:
    """
    Tee stdout/stderr to a rotating log file for the web Log/debug views.
    Safe to call multiple times (no-ops after the first successful setup).
    """
    global _FILE_LOGGING_READY
    if log_path is None:
        log_path = getattr(config, 'WEB_LOG_PATH', 'logs/trader.log')
    log_path = os.path.abspath(log_path)
    if _FILE_LOGGING_READY:
        return log_path

    log_dir = os.path.dirname(log_path)
    if log_dir and not os.path.isdir(log_dir):
        os.makedirs(log_dir)

    # Keep a Python logging handler for structured use; tee covers print()
    logger = logging.getLogger('stock_trader')
    logger.setLevel(logging.INFO)
    if not any(isinstance(h, RotatingFileHandler) for h in logger.handlers):
        handler = RotatingFileHandler(
            log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8'
        )
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        logger.addHandler(handler)

    # Tee print() output (open in append mode; RotatingFileHandler also writes).
    # stdout respects console-quiet; stderr always shows (crashes / tracebacks).
    tee_fp = open(log_path, 'a', encoding='utf-8')
    if not isinstance(sys.stdout, _TeeStream):
        sys.stdout = _TeeStream(sys.stdout, tee_fp, respect_quiet=True)
    if not isinstance(sys.stderr, _TeeStream):
        sys.stderr = _TeeStream(sys.stderr, tee_fp, respect_quiet=False)

    _FILE_LOGGING_READY = True
    return log_path


# Viewer-facing log buckets (everything else collapses into task).
_LOG_CATEGORY_ALIASES = {
    'buy': 'buy',
    'sell': 'sell',
    'watchlist': 'watchlist',
    'task': 'task',
    'job': 'task',
    'yahoo': 'task',
    'account': 'task',
    'book': 'task',
    'algorithm': 'task',
    'web': 'task',
    'order': 'sell',
    'trade': 'task',
    'stop-limit': 'stop-limit',
    'stop_limit': 'stop-limit',
    'stoplimit': 'stop-limit',
    'stop': 'stop-limit',
}

# Bump when user-facing event_log copy changes. Next process start rewrites stored rows
# (DELL Pull from GitHub restarts the loop/dashboard, so this is "first pull").
_EVENT_LOG_CLEAN_VERSION = 4
_EVENT_LOG_CLEAN_FLAG = 'event_log_clean_version'
_LOG_TICKER_RE = re.compile(r'\b([A-Z]{1,6}(?:\.[A-Z]{1,2})?)\b')
_LOG_MONEY_RE = re.compile(r'\$([0-9]+(?:\.[0-9]+)?)')
_LOG_QTY_SH_RE = re.compile(r'(?:^|\s)(\d+)\s+sh\b', re.I)
_LOG_WAS_SH_RE = re.compile(r'\bwas\s+(\d+)\s+sh\b', re.I)


def normalize_log_category(category: Optional[str]) -> str:
    """Map any event category into buy | sell | stop-limit | watchlist | task."""
    key = (category or 'task').strip().lower()
    return _LOG_CATEGORY_ALIASES.get(key, 'task')


def log_event(
    category: str,
    message: str,
    level: str = 'info',
    detail: Optional[Dict[str, Any]] = None,
) -> None:
    """Append a structured event for the web Log page (and print a one-liner)."""
    ts = datetime.now().isoformat(timespec='seconds')
    cat = normalize_log_category(category)
    lvl = (level or 'info').strip().lower()
    if lvl in ('warning', 'warn'):
        lvl = 'warn'
    elif lvl == 'issue':
        lvl = 'issue'
    elif lvl not in ('info', 'error', 'warn', 'issue'):
        lvl = 'info'
    detail_out = dict(detail) if isinstance(detail, dict) else {}
    if category and normalize_log_category(category) == 'task' and category != 'task':
        detail_out.setdefault('source', str(category))
    detail_json = json.dumps(detail_out) if detail_out else (
        json.dumps(detail) if detail is not None else None
    )
    # Prefer in-context user only — never call require_user_id() here (DB lookup
    # can block web_app startup for ~60s while the trader loop holds a write lock).
    uid = uc.current_user_id()

    def _insert(conn):
        cursor = conn.cursor()
        try:
            cursor.execute(
                '''
                INSERT INTO event_log (ts, level, category, message, detail_json, user_id)
                VALUES (?, ?, ?, ?, ?, ?)
                ''',
                (ts, lvl, cat, message, detail_json, uid),
            )
        except sqlite3.OperationalError:
            cursor.execute(
                '''
                INSERT INTO event_log (ts, level, category, message, detail_json)
                VALUES (?, ?, ?, ?, ?)
                ''',
                (ts, lvl, cat, message, detail_json),
            )

    try:
        # Fail fast when the trader loop holds the DB; console/file still get the line.
        conn = get_connection(timeout=2.0, busy_timeout_ms=2000)
        _insert(conn)
        conn.commit()
        conn.close()
        try:
            bump_dashboard_rev()
        except Exception:
            pass
    except Exception as e:
        print(f"(event_log write failed: {e})")
    if lvl == 'warn':
        print(f"[{ts}] WARN/{cat}: {message}")
    elif lvl == 'issue':
        print(f"[{ts}] ISSUE/{cat}: {message}")
    elif lvl == 'error':
        print(f"[{ts}] ERROR/{cat}: {message}")
    else:
        print(f"[{ts}] {cat}: {message}")


def _fmt_log_px(price: Any) -> str:
    return '$%.2f' % float(price)


def format_bought_log_message(
    ticker: str,
    quantity: Optional[int] = None,
    price: Optional[float] = None,
    dry_run: bool = False,
) -> str:
    prefix = 'DRY-RUN ' if dry_run else ''
    qty_s = ('%s ' % int(quantity)) if quantity is not None else ''
    if price is not None:
        try:
            return '%sBOUGHT %s%s @ %s' % (prefix, qty_s, ticker, _fmt_log_px(price))
        except (TypeError, ValueError):
            pass
    return '%sBOUGHT %s%s' % (prefix, qty_s, ticker)


def format_sold_log_message(
    ticker: str,
    quantity: Optional[int] = None,
    price: Optional[float] = None,
    dry_run: bool = False,
) -> str:
    prefix = 'DRY-RUN ' if dry_run else ''
    qty_s = ('%s ' % int(quantity)) if quantity is not None else ''
    if price is not None:
        try:
            return '%sSOLD %s%s @ %s' % (prefix, qty_s, ticker, _fmt_log_px(price))
        except (TypeError, ValueError):
            pass
    return '%sSOLD %s%s' % (prefix, qty_s, ticker)


def format_stop_limit_log_message(
    ticker: str,
    stop_price: float,
    previous_stop: Optional[float] = None,
    deferred: bool = False,
) -> str:
    px = _fmt_log_px(stop_price)
    if deferred:
        return 'STOP-LIMIT set @ %s for %s (deferred to avoid same-day trading)' % (
            px, ticker,
        )
    if previous_stop is not None:
        try:
            prev = float(previous_stop)
            new = float(stop_price)
            verb = 'increased' if new >= prev else 'decreased'
            delta = round(abs(new - prev), 2)
            return 'STOP-LIMIT %s %s to %s for %s' % (
                verb, _fmt_log_px(delta), _fmt_log_px(new), ticker,
            )
        except (TypeError, ValueError):
            pass
    return 'STOP-LIMIT set @ %s for %s' % (px, ticker)


def log_stop_limit_event(
    ticker: str,
    stop_price: float,
    previous_stop: Optional[float] = None,
    deferred: bool = False,
    dry_run: bool = False,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    msg = format_stop_limit_log_message(
        ticker, stop_price, previous_stop=previous_stop, deferred=deferred,
    )
    detail = dict(extra) if extra else {}
    detail['ticker'] = ticker
    try:
        detail['stop_price'] = float(stop_price)
    except (TypeError, ValueError):
        pass
    if previous_stop is not None:
        try:
            detail['previous_stop'] = float(previous_stop)
        except (TypeError, ValueError):
            pass
    detail['phase'] = (
        'deferred' if deferred else ('moved' if previous_stop is not None else 'set')
    )
    detail['order_type'] = 'STOP_LIMIT'
    if dry_run:
        detail['dry_run'] = True
    log_event('stop-limit', msg, detail=detail)


def _emit_stop_limit_placed_or_moved(
    ticker: str,
    stop_price: float,
    previous_stop: Optional[float] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """User log for a new or ratcheted stop. Same price (qty-only) is silent."""
    new_px = round(float(stop_price), 2)
    prev_px = None  # type: Optional[float]
    if previous_stop is not None:
        try:
            prev_px = round(float(previous_stop), 2)
        except (TypeError, ValueError):
            prev_px = None
    if prev_px is not None and abs(prev_px - new_px) < 0.01:
        _set_stop_defer_logged(ticker, False)
        return
    log_stop_limit_event(
        ticker,
        new_px,
        previous_stop=prev_px,
        dry_run=trade_dry_run_enabled(),
        extra=extra,
    )
    _set_stop_defer_logged(ticker, False)


def _stop_defer_already_logged(ticker: str) -> bool:
    init_database()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            'SELECT stop_defer_logged FROM positions WHERE ticker = ?',
            (ticker,),
        )
        row = cursor.fetchone()
    except sqlite3.OperationalError:
        row = None
    conn.close()
    return bool(row and row[0])


def _set_stop_defer_logged(ticker: str, value: bool) -> None:
    init_database()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            'UPDATE positions SET stop_defer_logged = ? WHERE ticker = ?',
            (1 if value else 0, ticker),
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass
    conn.close()


def _detail_float(detail: Dict[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        if key not in detail or detail.get(key) is None:
            continue
        try:
            return float(detail.get(key))
        except (TypeError, ValueError):
            continue
    return None


def _detail_int(detail: Dict[str, Any], *keys: str) -> Optional[int]:
    for key in keys:
        if key not in detail or detail.get(key) is None:
            continue
        try:
            return int(float(detail.get(key)))
        except (TypeError, ValueError):
            continue
    return None


def _first_money(text: str) -> Optional[float]:
    m = _LOG_MONEY_RE.search(text or '')
    if not m:
        return None
    try:
        return float(m.group(1))
    except (TypeError, ValueError):
        return None


def _qty_from_legacy_message(text: str, detail: Dict[str, Any]) -> Optional[int]:
    qty = _detail_int(detail, 'quantity', 'shares_owned', 'qty')
    if qty is not None:
        return qty
    was = _LOG_WAS_SH_RE.search(text or '')
    if was:
        try:
            return int(was.group(1))
        except (TypeError, ValueError):
            pass
    sh = _LOG_QTY_SH_RE.search(text or '')
    if sh:
        try:
            return int(sh.group(1))
        except (TypeError, ValueError):
            pass
    return None


def _legacy_event_plan(
    category: str,
    message: str,
    detail: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Map an old event_log row to keep / update / delete.

    Returns keys: action ('keep'|'update'|'delete'), plus category/message/kind/ticker
    when rewriting.
    """
    detail = detail if isinstance(detail, dict) else {}
    msg = (message or '').strip()
    cat = normalize_log_category(category)
    upper = msg.upper()
    ticker = detail.get('ticker')
    if ticker:
        ticker = str(ticker).strip().upper()
    else:
        ticker = None

    if upper.startswith('STOP CANCELLED'):
        return {'action': 'delete'}
    if upper.startswith('PLACING BUY') or upper.startswith('PLACING MARKET SELL'):
        return {'action': 'delete'}
    if 'REBUY DEBOUNCE' in upper or str(detail.get('hint') or '') == 'rebuy_debounce':
        return {'action': 'delete'}

    if msg.startswith('STOP-LIMIT '):
        tkr = ticker
        if not tkr:
            fm = re.search(r'\bfor\s+([A-Z]{1,6}(?:\.[A-Z]{1,2})?)\b', msg)
            if fm:
                tkr = fm.group(1)
        moved = re.match(
            r'^STOP-LIMIT moved from \$([0-9.]+) to \$([0-9.]+) for ([A-Z]{1,6}(?:\.[A-Z]{1,2})?)',
            msg,
        )
        if moved:
            tkr = moved.group(3).upper()
            prev_px = float(moved.group(1))
            new_px = float(moved.group(2))
            return {
                'action': 'update',
                'category': 'stop-limit',
                'message': format_stop_limit_log_message(
                    tkr, new_px, previous_stop=prev_px,
                ),
                'kind': 'stop_moved',
                'ticker': tkr,
                'stop_px': new_px,
            }
        bumped = re.match(
            r'^STOP-LIMIT (?:increased|decreased) \$([0-9.]+) to \$([0-9.]+) for ([A-Z]{1,6}(?:\.[A-Z]{1,2})?)',
            msg,
        )
        if bumped:
            tkr = bumped.group(3).upper()
            first_px = float(bumped.group(1))
            new_px = float(bumped.group(2))
            # Older copy used the previous stop as the first dollar amount.
            # New copy uses the change size (usually much smaller than the stop).
            if new_px > 0 and first_px >= new_px * 0.5:
                prev_px = first_px
                return {
                    'action': 'update',
                    'category': 'stop-limit',
                    'message': format_stop_limit_log_message(
                        tkr, new_px, previous_stop=prev_px,
                    ),
                    'kind': 'stop_moved',
                    'ticker': tkr,
                    'stop_px': new_px,
                }
            return {
                'action': 'update' if cat != 'stop-limit' else 'keep',
                'category': 'stop-limit',
                'message': msg,
                'kind': 'stop_moved',
                'ticker': tkr,
                'stop_px': new_px,
            }
        kind = 'stop_set'
        if '(deferred to avoid same-day trading)' in msg:
            kind = 'stop_deferred'
        return {
            'action': 'update' if cat != 'stop-limit' else 'keep',
            'category': 'stop-limit',
            'message': msg,
            'kind': kind,
            'ticker': tkr,
            'stop_px': _first_money(msg),
        }

    if upper.startswith('BOUGHT ') and ': FILL CONFIRMED' in upper:
        m = re.match(
            r'^BOUGHT\s+([A-Z]{1,6}(?:\.[A-Z]{1,2})?)\s*:',
            msg,
            re.I,
        )
        tkr = (m.group(1).upper() if m else ticker)
        qty = _qty_from_legacy_message(msg, detail)
        px = _detail_float(detail, 'price') or _first_money(msg)
        if tkr:
            return {
                'action': 'update',
                'category': 'buy',
                'message': format_bought_log_message(tkr, qty, px),
                'kind': 'bought',
                'ticker': tkr,
            }

    dry = bool(detail.get('dry_run')) or upper.startswith('DRY-RUN')
    arm = re.match(
        r'^(?:DRY-RUN would ARM STOP|STOP LIVE)\s+(\d+)\s+([A-Z]{1,6}(?:\.[A-Z]{1,2})?)\s+@\s+\$([0-9.]+)',
        msg,
    )
    if arm:
        tkr = arm.group(2).upper()
        try:
            stop_px = float(arm.group(3))
        except (TypeError, ValueError):
            stop_px = _detail_float(detail, 'stop_price')
        if tkr and stop_px is not None:
            return {
                'action': 'update',
                'category': 'stop-limit',
                'message': format_stop_limit_log_message(tkr, stop_px),
                'kind': 'stop_set',
                'ticker': tkr,
                'stop_px': stop_px,
                'replace': bool(detail.get('replace')) or '(replace' in msg.lower(),
            }

    deferred = re.match(
        r'^STOP deferred for\s+([A-Z]{1,6}(?:\.[A-Z]{1,2})?)\b',
        msg,
        re.I,
    )
    if deferred:
        tkr = deferred.group(1).upper()
        stop_px = _detail_float(detail, 'stop_price') or _first_money(msg)
        if tkr and stop_px is not None:
            return {
                'action': 'update',
                'category': 'stop-limit',
                'message': format_stop_limit_log_message(
                    tkr, stop_px, deferred=True,
                ),
                'kind': 'stop_deferred',
                'ticker': tkr,
                'stop_px': stop_px,
            }
        if tkr:
            return {
                'action': 'update',
                'category': 'stop-limit',
                'message': 'STOP-LIMIT for %s (deferred to avoid same-day trading)' % tkr,
                'kind': 'stop_deferred',
                'ticker': tkr,
            }

    failed = re.match(
        r'^ARM STOP failed\s+([A-Z]{1,6}(?:\.[A-Z]{1,2})?)\b',
        msg,
        re.I,
    )
    if failed:
        tkr = failed.group(1).upper()
        return {
            'action': 'update',
            'category': 'stop-limit',
            'message': 'STOP-LIMIT failed for %s' % tkr,
            'kind': 'stop_failed',
            'ticker': tkr,
        }

    dry_buy = re.match(
        r'^DRY-RUN would (?:PLACE )?BUY\s+(\d+)\s+([A-Z]{1,6}(?:\.[A-Z]{1,2})?)(?:\s+@\s+~?\$([0-9.]+))?',
        msg,
        re.I,
    )
    if dry_buy:
        tkr = dry_buy.group(2).upper()
        qty = int(dry_buy.group(1))
        px = float(dry_buy.group(3)) if dry_buy.group(3) else _detail_float(detail, 'price')
        return {
            'action': 'update',
            'category': 'buy',
            'message': format_bought_log_message(tkr, qty, px, dry_run=True),
            'kind': 'bought',
            'ticker': tkr,
        }

    dry_sell = re.match(
        r'^DRY-RUN would PLACE MARKET SELL\s+(\d+)\s+([A-Z]{1,6}(?:\.[A-Z]{1,2})?)(?:\s+@\s+~?\$([0-9.]+))?',
        msg,
        re.I,
    )
    if dry_sell:
        tkr = dry_sell.group(2).upper()
        qty = int(dry_sell.group(1))
        px = float(dry_sell.group(3)) if dry_sell.group(3) else _detail_float(detail, 'price')
        return {
            'action': 'update',
            'category': 'sell',
            'message': format_sold_log_message(tkr, qty, px, dry_run=True),
            'kind': 'sold',
            'ticker': tkr,
        }

    sold = re.match(
        r'^SOLD\s+([A-Z]{1,6}(?:\.[A-Z]{1,2})?)\b',
        msg,
        re.I,
    )
    if sold:
        tkr = sold.group(1).upper()
        qty = _qty_from_legacy_message(msg, detail)
        px = _detail_float(detail, 'price', 'stop_order_price')
        if px is None:
            stop_m = re.search(r'@ stop \$([0-9.]+)', msg, re.I)
            if stop_m:
                try:
                    px = float(stop_m.group(1))
                except (TypeError, ValueError):
                    px = None
        if px is None:
            px = _first_money(msg)
        return {
            'action': 'update',
            'category': 'sell',
            'message': format_sold_log_message(tkr, qty, px, dry_run=dry),
            'kind': 'sold',
            'ticker': tkr,
        }

    if upper.startswith('BUY PASS SKIPPED') or upper.startswith('SELL PASS SKIPPED'):
        return {
            'action': 'update' if cat != 'task' else 'keep',
            'category': 'task',
            'message': msg,
            'kind': 'task',
        }

    if cat in ('buy', 'sell') and 'STOP' in upper and 'SOLD' not in upper:
        tkr = ticker
        if not tkr:
            tm = _LOG_TICKER_RE.search(msg.replace('STOP', ' ', 1))
            if tm:
                tkr = tm.group(1)
        stop_px = _detail_float(detail, 'stop_price') or _first_money(msg)
        if tkr and stop_px is not None:
            deferred_bit = 'DEFER' in upper
            return {
                'action': 'update',
                'category': 'stop-limit',
                'message': format_stop_limit_log_message(
                    tkr, stop_px, deferred=deferred_bit,
                ),
                'kind': 'stop_deferred' if deferred_bit else 'stop_set',
                'ticker': tkr,
                'stop_px': stop_px,
            }

    return {'action': 'keep', 'category': cat, 'message': msg, 'kind': cat, 'ticker': ticker}


def _rewrite_legacy_event_log(conn: sqlite3.Connection) -> None:
    """Rewrite stored Log rows when _EVENT_LOG_CLEAN_VERSION is bumped."""
    cursor = conn.cursor()
    stored_ver = 0
    try:
        cursor.execute(
            'SELECT value FROM runtime_flags WHERE key = ?',
            (_EVENT_LOG_CLEAN_FLAG,),
        )
        flag = cursor.fetchone()
        if flag:
            try:
                stored_ver = int(flag[0])
            except (TypeError, ValueError):
                stored_ver = 0
        if stored_ver < 1:
            cursor.execute(
                "SELECT value FROM runtime_flags WHERE key = 'event_log_clean_v1'"
            )
            legacy = cursor.fetchone()
            if legacy and str(legacy[0]) == '1':
                stored_ver = 1
        if stored_ver >= int(_EVENT_LOG_CLEAN_VERSION):
            return
    except sqlite3.OperationalError:
        return
    try:
        cursor.execute(
            'SELECT id, user_id, category, message, detail_json FROM event_log ORDER BY id ASC'
        )
        rows = cursor.fetchall()
    except sqlite3.OperationalError:
        try:
            cursor.execute(
                'SELECT id, category, message, detail_json FROM event_log ORDER BY id ASC'
            )
            rows = [(r[0], None, r[1], r[2], r[3]) for r in cursor.fetchall()]
        except sqlite3.OperationalError:
            return

    # Per user: last armed stop price, and whether the last stop-limit row was deferred.
    last_stop = {}  # type: Dict[Any, Dict[str, float]]
    last_deferred = {}  # type: Dict[Any, Dict[str, bool]]
    n_update = 0
    n_delete = 0
    for row in rows:
        ev_id, user_id, category, message, detail_json = row
        detail = {}  # type: Dict[str, Any]
        if detail_json:
            try:
                parsed = json.loads(detail_json)
                if isinstance(parsed, dict):
                    detail = parsed
            except Exception:
                detail = {}
        plan = _legacy_event_plan(category, message, detail)
        action = plan.get('action')
        uid = user_id
        if uid not in last_stop:
            last_stop[uid] = {}
            last_deferred[uid] = {}
        kind = plan.get('kind')
        ticker = plan.get('ticker')
        if action == 'delete':
            cursor.execute('DELETE FROM event_log WHERE id = ?', (ev_id,))
            n_delete += 1
            continue
        if kind == 'stop_deferred' and ticker:
            if last_deferred[uid].get(ticker):
                cursor.execute('DELETE FROM event_log WHERE id = ?', (ev_id,))
                n_delete += 1
                continue
            last_deferred[uid][ticker] = True
        elif kind in ('stop_set', 'stop_moved') and ticker:
            prev = last_stop[uid].get(ticker)
            stop_px = plan.get('stop_px')
            if stop_px is None:
                stop_px = _first_money(str(plan.get('message') or message))
            if (
                prev is not None
                and stop_px is not None
                and abs(float(prev) - float(stop_px)) >= 0.01
                and kind == 'stop_set'
            ):
                plan['message'] = format_stop_limit_log_message(
                    ticker, float(stop_px), previous_stop=float(prev),
                )
                plan['kind'] = 'stop_moved'
                action = 'update'
            if stop_px is not None:
                last_stop[uid][ticker] = float(stop_px)
            last_deferred[uid][ticker] = False
        elif kind == 'sold' and ticker:
            last_deferred[uid][ticker] = False
            last_stop[uid].pop(ticker, None)

        new_cat = plan.get('category')
        new_msg = plan.get('message')
        if action == 'update' and new_cat and new_msg:
            if new_cat != category or new_msg != message:
                cursor.execute(
                    'UPDATE event_log SET category = ?, message = ? WHERE id = ?',
                    (new_cat, new_msg, ev_id),
                )
                n_update += 1

    cursor.execute(
        '''
        INSERT OR REPLACE INTO runtime_flags (key, value, updated_at)
        VALUES (?, ?, ?)
        ''',
        (
            _EVENT_LOG_CLEAN_FLAG,
            str(int(_EVENT_LOG_CLEAN_VERSION)),
            datetime.now().isoformat(timespec='seconds'),
        ),
    )
    if n_update or n_delete:
        print(
            'Event log cleanup: updated %s, removed %s older rows'
            % (n_update, n_delete)
        )


def get_runtime_flag(key: str, default: Optional[str] = None) -> Optional[str]:
    scoped = uc.scoped_flag_key(key)
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT value FROM runtime_flags WHERE key = ?', (scoped,))
        row = cursor.fetchone()
        # Legacy unscoped keys: only the migrated owner (jame) may inherit them.
        # Other users must not pick up e.g. algorithm_start from the shared key.
        if row is None and scoped != key:
            uid = uc.current_user_id()
            owner = uc.get_user_by_username('jame') or {}
            owner_id = owner.get('id')
            if uid is not None and owner_id is not None and int(uid) == int(owner_id):
                cursor.execute('SELECT value FROM runtime_flags WHERE key = ?', (key,))
                row = cursor.fetchone()
        conn.close()
        if row is None:
            return default
        return row[0]
    except Exception:
        return default


def set_runtime_flag(
    key: str,
    value: str,
    timeout: float = 5.0,
    busy_timeout_ms: int = 5000,
    retries: int = 4,
) -> bool:
    """
    Persist a runtime flag. Returns True on success.

    Defaults fail within a few seconds so web handlers (e.g. Schwab OAuth)
    are not stuck for minutes when the trader loop holds market_data.db.
    """
    if not _DATABASE_READY:
        try:
            init_database()
        except Exception as e:
            print('Warning: set_runtime_flag init_database failed: %s' % e)
            return False
    scoped = uc.scoped_flag_key(key)
    last_err = None  # type: Optional[Exception]
    attempts = max(1, int(retries))
    for attempt in range(attempts):
        try:
            conn = get_connection(
                timeout=float(timeout),
                busy_timeout_ms=int(busy_timeout_ms),
            )
            try:
                cursor = conn.cursor()
                cursor.execute(
                    'INSERT OR REPLACE INTO runtime_flags (key, value, updated_at) VALUES (?, ?, ?)',
                    (scoped, value, datetime.now().isoformat(timespec='seconds'))
                )
                conn.commit()
            finally:
                conn.close()
            return True
        except sqlite3.OperationalError as e:
            last_err = e
            if 'locked' not in str(e).lower():
                raise
            time.sleep(0.05 * (attempt + 1))
    print('Warning: set_runtime_flag(%s) failed after retries: %s' % (scoped, last_err))
    return False


def bump_dashboard_rev() -> int:
    """Increment content revision so the web UI knows to reload portfolio/log."""
    try:
        cur = int(get_runtime_flag('dashboard_rev', '0') or '0')
    except (TypeError, ValueError):
        cur = 0
    nxt = cur + 1
    set_runtime_flag('dashboard_rev', str(nxt))
    return nxt


def get_dashboard_rev() -> int:
    try:
        return int(get_runtime_flag('dashboard_rev', '0') or '0')
    except (TypeError, ValueError):
        return 0


def buys_paused() -> bool:
    return str(get_runtime_flag('buys_paused', '0')).strip() in ('1', 'true', 'True', 'yes')


# ============================================================================
# Data Fetching Functions (yfinance - Daily)
# ============================================================================

def get_all_tickers() -> List[str]:
    """
    Fetch all US stock tickers from SEC.gov.
    Returns a list of ticker symbols.
    """
    if config.LIMITED_TICKER_LIST:
        return config.LIMITED_TICKER_LIST
    
    try:
        # SEC requires an email in the User-Agent header
        headers = {'User-Agent': 'jamesrstevick@gmail.com'}
        
        # Download the official ticker list from SEC
        print("Downloading ticker list from SEC.gov...")
        url = "https://www.sec.gov/files/company_tickers.json"
        response = requests.get(url, headers=headers)
        response.raise_for_status()  # Raise an exception for bad status codes
        
        # Parse the data
        # SEC format: {'0': {'cik':..., 'ticker':...}, '1': {...}}
        data = response.json()
        ticker_list = []
        
        for entry in data.values():
            ticker = entry.get('ticker')
            if ticker:
                ticker_list.append(ticker)
        
        # Remove duplicates and sort
        ticker_list = sorted(list(set(ticker_list)))
        
        print(f"Found {len(ticker_list)} unique tickers from SEC (e.g., {ticker_list[:5]})")
        return ticker_list
        
    except Exception as e:
        print(f"Error fetching tickers from SEC: {e}")
        print("Falling back to sample tickers.")
        return ["AAPL", "MSFT", "GOOGL", "AMZN", "TSLA", "META", "NVDA", "JPM", "V", "JNJ"]


def list_available_yfinance_fields(ticker: str = 'AAPL') -> Dict[str, Any]:
    """
    List all available fields from yfinance for a ticker.
    Useful for reviewing what data is available vs what we're storing.
    
    Args:
        ticker: Stock ticker to check (default: 'AAPL')
    
    Returns:
        Dictionary with all available fields from stock.info
    """
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        return info
    except Exception as e:
        print(f"Error fetching fields for {ticker}: {e}")
        return {}


def fetch_stock_data(ticker: str, include_history: bool = False) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Get fundamental data for a single ticker from yfinance.
    Returns a tuple: (data_dict, error_type)
    
    Args:
        ticker: Stock ticker symbol
        include_history: If True, fetch historical price data (default: False for speed)
    
    Returns:
        Tuple of (data_dict, error_type) where:
        - data_dict: Dictionary with ticker, fundamentals, and optionally price_data (None on error)
        - error_type: None if successful;
            'rate_limit' for 429 / throttle;
            'transient' for network / 5xx / timeouts (retry);
            'other' for invalid ticker / permanent failures
    """
    try:
        stock = yf.Ticker(ticker)
        
        # Get info for fundamentals
        info = stock.info
        
        # Empty info: often soft throttle / flaky Yahoo; retry as transient.
        # Truly dead symbols still fail after retries and stay unmarked for next refresh.
        if not info or len(info) == 0:
            return None, 'transient'
        
        # Start with known fields
        data = {
            'ticker': ticker,
            'pe_ratio': info.get('trailingPE', None),
            'market_cap': info.get('marketCap', None),
            'sector': info.get('sector', None),
            'avg_volume': info.get('averageVolume', None),
            'beta': info.get('beta', None),
            'short_float': info.get('shortPercentOfFloat', None),
            'current_price': info.get('currentPrice', None),
            'target_price': info.get('targetMeanPrice', None),
            # Valuation metrics
            'forward_pe': info.get('forwardPE', None),
            # yfinance key: pegRatio (camelCase). Often missing; fallback computed below when possible.
            'peg_ratio': info.get('pegRatio', None),
            'price_to_book': info.get('priceToBook', None),
            'price_to_sales': info.get('priceToSalesTrailing12Months', None),
            'enterprise_to_revenue': info.get('enterpriseToRevenue', None),
            'enterprise_to_ebitda': info.get('enterpriseToEbitda', None),
            # Financial health
            'debt_to_equity': info.get('debtToEquity', None),
            'current_ratio': info.get('currentRatio', None),
            'quick_ratio': info.get('quickRatio', None),
            'total_cash': info.get('totalCash', None),
            'total_debt': info.get('totalDebt', None),
            'total_revenue': info.get('totalRevenue', None),
            'gross_profits': info.get('grossProfits', None),
            'free_cashflow': info.get('freeCashflow', None),
            'operating_cashflow': info.get('operatingCashflow', None),
            # Growth metrics
            'revenue_growth': info.get('revenueGrowth', None),
            'earnings_growth': info.get('earningsGrowth', None),
            'earnings_quarterly_growth': info.get('earningsQuarterlyGrowth', None),
            'profit_margins': info.get('profitMargins', None),
            'gross_margins': info.get('grossMargins', None),
            'operating_margins': info.get('operatingMargins', None),
            # Dividends
            'dividend_rate': info.get('dividendRate', None),
            'dividend_yield': info.get('dividendYield', None),
            # Market data
            'previous_close': info.get('previousClose', None),
            'week_52_high': info.get('fiftyTwoWeekHigh', None) or info.get('52WeekHigh', None),
            'week_52_low': info.get('fiftyTwoWeekLow', None) or info.get('52WeekLow', None),
            # Company info
            'full_time_employees': info.get('fullTimeEmployees', None),
            # Ownership
            'held_percent_institutions': info.get('heldPercentInstitutions', None),
            # Additional numeric fields - moving averages
            'fifty_day_average': info.get('fiftyDayAverage', None),
            'two_hundred_day_average': info.get('twoHundredDayAverage', None),
            # Additional numeric fields - analyst data
            'recommendation_mean': info.get('recommendationMean', None),
            'number_of_analyst_opinions': info.get('numberOfAnalystOpinions', None),
            'target_high_price': info.get('targetHighPrice', None),
            'target_low_price': info.get('targetLowPrice', None),
            # Additional numeric fields - shares
            'shares_outstanding': info.get('sharesOutstanding', None),
            'float_shares': info.get('floatShares', None),
            'shares_short': info.get('sharesShort', None),
            'short_ratio': info.get('shortRatio', None),
            # Additional numeric fields - valuation
            'book_value': info.get('bookValue', None),
            'enterprise_value': info.get('enterpriseValue', None),
            'ebitda': info.get('ebitda', None),
            'return_on_assets': info.get('returnOnAssets', None),
            'return_on_equity': info.get('returnOnEquity', None),
            # Additional numeric fields - dividends
            'payout_ratio': info.get('payoutRatio', None),
            # Additional numeric fields - volume
            'average_volume_10days': info.get('averageVolume10days', None),
            # Additional numeric fields - ownership
            'held_percent_insiders': info.get('heldPercentInsiders', None)
        }
        
        # Fallback: compute PEG when yfinance doesn't return pegRatio. PEG = P/E / (earnings growth %).
        if data.get('peg_ratio') is None and data.get('pe_ratio') and data.get('earnings_growth') and data['earnings_growth'] > 0:
            growth_pct = data['earnings_growth'] * 100  # decimal 0.15 -> 15%
            data['peg_ratio'] = data['pe_ratio'] / growth_pct
        
        # Add 52-week change explicitly (can come as 52WeekChange or fiftyTwoWeekChange)
        week_52_change = info.get('52WeekChange', None) or info.get('fiftyTwoWeekChange', None)
        if week_52_change is not None:
            data['week_52_change'] = week_52_change
        
        # Dynamically add any other numeric fields from info that we haven't captured
        # Convert camelCase to snake_case for database column names
        def camel_to_snake(name):
            import re
            # Handle names starting with numbers (e.g., "52WeekChange" -> "week_52_change")
            if name and name[0].isdigit():
                # Find first letter and move number prefix to end
                match = re.match(r'^(\d+)([A-Z].*)', name)
                if match:
                    number_part = match.group(1)
                    rest = match.group(2)
                    # Convert rest to snake_case and append number
                    s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', rest)
                    s2 = re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1).lower()
                    return f'week_{number_part}_{s2}' if 'week' in s2.lower() else f'{s2}_{number_part}'
            # Normal camelCase conversion
            s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
            return re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1).lower()
        
        # Fields we've already captured (in snake_case)
        captured_fields = {
            'trailingPE', 'marketCap', 'sector', 'averageVolume', 'beta', 'shortPercentOfFloat',
            'currentPrice', 'targetMeanPrice', 'forwardPE', 'pegRatio', 'priceToBook',
            'priceToSalesTrailing12Months', 'enterpriseToRevenue', 'enterpriseToEbitda',
            'debtToEquity', 'currentRatio', 'quickRatio', 'totalCash', 'totalDebt',
            'totalRevenue', 'grossProfits', 'freeCashflow', 'operatingCashflow',
            'revenueGrowth', 'earningsGrowth', 'earningsQuarterlyGrowth', 'profitMargins',
            'grossMargins', 'operatingMargins', 'dividendRate', 'dividendYield',
            'previousClose', 'fiftyTwoWeekHigh', '52WeekHigh', 'fiftyTwoWeekLow', '52WeekLow',
            'fiftyTwoWeekChange', '52WeekChange',  # Add 52-week change variants
            'fullTimeEmployees', 'heldPercentInstitutions', 'fiftyDayAverage',
            'twoHundredDayAverage', 'recommendationMean', 'numberOfAnalystOpinions',
            'targetHighPrice', 'targetLowPrice', 'sharesOutstanding', 'floatShares',
            'sharesShort', 'shortRatio', 'bookValue', 'enterpriseValue', 'ebitda',
            'returnOnAssets', 'returnOnEquity', 'payoutRatio', 'averageVolume10days',
            'heldPercentInsiders'
        }
        
        # Add any other numeric fields
        for key, value in info.items():
            if key not in captured_fields:
                # Only include numeric types (int, float) and exclude None
                if isinstance(value, (int, float)) and value is not None:
                    snake_key = camel_to_snake(key)
                    # Ensure column name doesn't start with a number (SQL requirement)
                    if snake_key and snake_key[0].isdigit():
                        snake_key = 'field_' + snake_key
                    data[snake_key] = value
        
        # Only fetch historical data if requested
        if include_history:
            hist = stock.history(start=config.DEFAULT_START_DATE, end=config.DEFAULT_END_DATE)
            data['price_data'] = hist
        else:
            data['price_data'] = pd.DataFrame()  # Empty DataFrame
        
        return data, None
        
    except Exception as e:
        error_msg = str(e).lower()
        
        # Throttling / quota
        if (
            '429' in error_msg
            or 'too many requests' in error_msg
            or 'rate limit' in error_msg
            or 'quota' in error_msg
        ):
            return None, 'rate_limit'
        
        # Transient — keep trying (network blips, Yahoo 5xx, timeouts)
        transient_markers = (
            'timeout', 'timed out', 'temporarily', 'connection', 'reset',
            '503', '502', '504', '500', 'ssl', 'unreachable', 'unavailable',
            'jsondecode', 'expecting value', 'remote end closed', 'chunked',
        )
        if any(m in error_msg for m in transient_markers):
            return None, 'transient'
        
        # Likely permanent / bad symbol / unexpected
        return None, 'other'


def _yahoo_fetch_with_retries(
    ticker: str,
    max_retries: int,
    rate_limit_wait: float,
    transient_backoff: float,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], Dict[str, int]]:
    """
    Fetch one ticker with retries for rate_limit and transient errors.
    Returns (data, final_error_type, counters). final_error_type is None on success.
    """
    counters = {'rate_limit_waits': 0, 'transient_retries': 0}
    last_err = 'other'  # type: Optional[str]
    for attempt in range(max_retries):
        data, error_type = fetch_stock_data(ticker)
        if error_type is None and data is not None:
            return data, None, counters

        last_err = error_type or 'other'
        if last_err == 'rate_limit':
            counters['rate_limit_waits'] += 1
            print(
                f"\n⚠️  Rate limited on {ticker} "
                f"(wait #{counters['rate_limit_waits']}, attempt {attempt + 1}/{max_retries}) "
                f"— sleeping {rate_limit_wait / 60:.0f} min then retry..."
            )
            time.sleep(rate_limit_wait)
            continue
        if last_err == 'transient':
            counters['transient_retries'] += 1
            wait = min(transient_backoff * (2 ** min(attempt, 5)), 300)
            print(
                f"\n⚠️  Transient error on {ticker} "
                f"(attempt {attempt + 1}/{max_retries}) — backoff {wait:.0f}s then retry..."
            )
            time.sleep(wait)
            continue
        # Permanent / empty info — no point burning retries
        return None, last_err, counters

    return None, last_err, counters


def _store_fundamentals_row(cursor, ticker: str, data: Dict[str, Any], current_date: str) -> None:
    """INSERT OR REPLACE one fundamentals row from a fetch_stock_data dict."""
    cursor.execute("PRAGMA table_info(fundamentals)")
    column_names = [row[1] for row in cursor.fetchall()]
    insert_columns = [col for col in column_names if col != 'ticker']
    columns_str = ', '.join(insert_columns)
    placeholders = ', '.join(['?'] * len(insert_columns))
    values = []
    for col in insert_columns:
        if col == 'last_updated':
            values.append(current_date)
        else:
            values.append(data.get(col, None))
    query = f'''
        INSERT OR REPLACE INTO fundamentals (ticker, {columns_str})
        VALUES (?, {placeholders})
    '''
    cursor.execute(query, (ticker,) + tuple(values))


def _touch_fundamentals_updated(cursor, ticker: str, current_date: str) -> None:
    """
    Set last_updated without wiping other columns (invalid / empty symbols).
    Keeps dead tickers from monopolizing the oldest-first hourly batch.
    """
    cursor.execute('SELECT ticker FROM fundamentals WHERE ticker = ?', (ticker,))
    if cursor.fetchone():
        cursor.execute(
            'UPDATE fundamentals SET last_updated = ? WHERE ticker = ?',
            (current_date, ticker)
        )
    else:
        cursor.execute(
            'INSERT INTO fundamentals (ticker, last_updated) VALUES (?, ?)',
            (ticker, current_date)
        )


def _market_data_job_interval_days() -> float:
    """Hourly Yahoo batch job interval as fractional days for job_runs."""
    hours = float(getattr(config, 'MARKET_DATA_JOB_INTERVAL_HOURS', 1))
    return max(hours, 0.0) / 24.0


def _parse_fundamentals_updated_date(value: Optional[str]):
    """Parse fundamentals.last_updated to a date (accepts YYYY-MM-DD or ISO datetime prefix)."""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    # Date-only or leading date from an ISO timestamp
    try:
        return datetime.strptime(text[:10], '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


def _select_yahoo_refresh_batch(
    cursor,
    tickers: List[str],
    current_date: str,
    batch_size: int,
    max_age_days: int,
) -> Tuple[List[str], List[str], Dict[str, int]]:
    """
    Pick oldest / missing tickers for this run (oldest-first rotation).

    Always skips tickers whose last_updated date is today (same calendar day).

    Returns:
        (batch, overdue_tickers, stats)
        overdue = missing or age >= max_age_days (SLA breach list)
    """
    cursor.execute('SELECT ticker, last_updated FROM fundamentals')
    updated = {row[0]: row[1] for row in cursor.fetchall()}
    current = datetime.strptime(current_date, '%Y-%m-%d').date()

    scored = []  # type: List[Tuple[str, str]]
    overdue = []  # type: List[str]
    missing = 0
    within_sla = 0
    skipped_today = 0

    for ticker in tickers:
        last_updated = updated.get(ticker)
        if not last_updated:
            missing += 1
            scored.append((ticker, ''))  # sort before any real date
            overdue.append(ticker)
            continue
        last_date = _parse_fundamentals_updated_date(last_updated)
        if last_date is None:
            overdue.append(ticker)
            scored.append((ticker, ''))
            continue
        age = (current - last_date).days
        if age < 1:
            # Updated today — never refetch same calendar day (hourly or full catch-up)
            skipped_today += 1
            within_sla += 1
            continue
        sort_key = last_date.strftime('%Y-%m-%d')
        scored.append((ticker, sort_key))
        if age >= max_age_days:
            overdue.append(ticker)
        else:
            within_sla += 1

    scored.sort(key=lambda item: item[1])
    if batch_size and batch_size > 0:
        batch = [t for t, _ in scored[:batch_size]]
    else:
        batch = [t for t, _ in scored]

    stats = {
        'universe': len(tickers),
        'missing': missing,
        'overdue': len(overdue),
        'within_sla': within_sla,
        'skipped_today': skipped_today,
    }
    return batch, overdue, stats


def populate_database(batch_size: Optional[int] = None) -> bool:
    """
    Fetch and store Yahoo fundamentals for a batch of tickers (oldest / missing first).

    Writes go to FUNDAMENTALS_DATABASE_PATH. Each ticker is a short open→write→commit→close
    so Yahoo network/sleep never holds a trading-DB or fundamentals write lock.

    Returns:
        True if this batch finished; False if aborted early.
    """
    init_database()

    max_age_days = int(getattr(config, 'MARKET_DATA_REFRESH_DAYS', 7))
    if batch_size is None:
        batch_size = int(getattr(config, 'YAHOO_BATCH_SIZE', 150))

    interval_days = _market_data_job_interval_days()
    _job = get_job_run(JOB_REFRESH_MARKET_DATA)
    if not _job or _job.get('status') != 'running':
        mark_job_started(JOB_REFRESH_MARKET_DATA, interval_days)

    # Short read: pick batch, then release the connection before any Yahoo I/O.
    conn = get_fundamentals_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(fundamentals)")
        existing_columns = [row[1] for row in cursor.fetchall()]
        if 'last_updated' not in existing_columns:
            print("Adding missing 'last_updated' column to fundamentals DB...")
            try:
                cursor.execute(
                    'ALTER TABLE fundamentals ADD COLUMN last_updated TEXT'
                )
                conn.commit()
                print("✓ Added last_updated column")
            except sqlite3.OperationalError as e:
                print(f"Warning: Could not add last_updated column: {e}")

        tickers = get_all_tickers()
        total_tickers = len(tickers)
        current_date = datetime.now().strftime('%Y-%m-%d')
        batch, overdue_before, stale_stats = _select_yahoo_refresh_batch(
            cursor, tickers, current_date, batch_size, max_age_days
        )
    finally:
        conn.close()

    batch_n = len(batch)
    fetch_sleep = float(getattr(config, 'YAHOO_FETCH_SLEEP_SECONDS', 2))

    print(f"Universe: {total_tickers} tickers")
    print('Fundamentals DB: %s' % fundamentals_db_path())
    print(
        f"Staleness SLA: max {max_age_days}d — "
        f"{stale_stats['within_sla']} within SLA, "
        f"{stale_stats['overdue']} overdue/missing "
        f"({stale_stats['missing']} missing)"
    )
    print(
        f"Skip if updated today: {stale_stats.get('skipped_today', 0)} ticker(s) "
        f"(last_updated={current_date})"
    )
    if stale_stats['overdue'] > 0:
        hours_catchup = (
            (stale_stats['overdue'] + max(batch_size, 1) - 1) // max(batch_size, 1)
            if batch_size and batch_size > 0
            else 1
        )
        print(
            f"⚠️  {stale_stats['overdue']} ticker(s) past {max_age_days}-day SLA "
            f"(~{hours_catchup} hourly batch(es) to clear at batch_size={batch_size or 'all'})"
        )
        if overdue_before[:8]:
            print(f"   Oldest/missing examples: {overdue_before[:8]}")
    if batch_size and batch_size > 0:
        print(f"This run: fetch {batch_n} ticker(s) (oldest-first, cap={batch_size})")
    else:
        print(
            f"This run: FULL CATCH-UP — fetch {batch_n} due ticker(s) "
            f"(oldest-first, no batch cap; excludes updated today)"
        )
    est_min = batch_n * fetch_sleep / 60.0
    if est_min >= 60:
        print(f"Estimated time: ~{est_min / 60:.1f} hours at {fetch_sleep:.0f}s/ticker")
    else:
        print(f"Estimated time: ~{est_min:.1f} min at {fetch_sleep:.0f}s/ticker")
    print("One short DB write per ticker (no lock across Yahoo waits). Ctrl+C saves.\n")
    update_job_progress(
        JOB_REFRESH_MARKET_DATA,
        f"batch {batch_n}: overdue={stale_stats['overdue']}/{total_tickers}"
    )

    if batch_n == 0:
        print("Nothing to fetch.")
        update_job_progress(JOB_REFRESH_MARKET_DATA, "batch empty")
        return True

    successful = 0
    failed = 0
    skipped = 0
    invalid_tickers = []  # type: List[str]
    retry_later = []  # type: List[str]
    rate_limit_waits = 0
    transient_retries = 0
    fetches_done = 0

    post_throttle_sleep = float(getattr(config, 'YAHOO_POST_THROTTLE_SLEEP_SECONDS', 5))
    rate_limit_wait = float(getattr(config, 'YAHOO_RATE_LIMIT_WAIT_SECONDS', 600))
    max_retries = int(getattr(config, 'YAHOO_MAX_RETRIES_PER_TICKER', 12))
    transient_backoff = float(getattr(config, 'YAHOO_TRANSIENT_BACKOFF_SECONDS', 15))
    throttle_slow_until = 0.0

    start_time = time.time()

    def _write_one(ticker: str, data: Optional[Dict[str, Any]], touch_only: bool) -> None:
        """Short transaction: open → write → commit → close."""
        wconn = get_fundamentals_connection(timeout=30.0, busy_timeout_ms=30000)
        try:
            wcur = wconn.cursor()
            if touch_only:
                _touch_fundamentals_updated(wcur, ticker, current_date)
            else:
                _store_fundamentals_row(wcur, ticker, data or {}, current_date)
            wconn.commit()
        finally:
            wconn.close()

    def _process_one(ticker: str, index: int, total: int) -> str:
        """Fetch+store one ticker. Returns 'ok' | 'invalid' | 'retry_later'."""
        nonlocal successful, failed, skipped, rate_limit_waits, transient_retries
        nonlocal fetches_done, fetch_sleep, throttle_slow_until

        sleep_for = post_throttle_sleep if time.time() < throttle_slow_until else fetch_sleep
        if fetches_done > 0:
            time.sleep(sleep_for)
        fetches_done += 1

        print(f"Processing {ticker} ({index}/{total})...", end=' ')
        data, err, counters = _yahoo_fetch_with_retries(
            ticker,
            max_retries=max_retries,
            rate_limit_wait=rate_limit_wait,
            transient_backoff=transient_backoff,
        )
        rate_limit_waits += counters.get('rate_limit_waits', 0)
        transient_retries += counters.get('transient_retries', 0)
        if counters.get('rate_limit_waits', 0) > 0:
            throttle_slow_until = time.time() + 30 * 60
            fetch_sleep = max(fetch_sleep, post_throttle_sleep)

        if err is None and data is not None:
            _write_one(ticker, data, touch_only=False)
            successful += 1
            print("✓ Processed")
            return 'ok'

        if err in ('rate_limit', 'transient'):
            failed += 1
            print(f"deferred after retries ({err})")
            return 'retry_later'

        _write_one(ticker, None, touch_only=True)
        skipped += 1
        invalid_tickers.append(ticker)
        print(f"skipped ({err or 'no data'}) — marked attempted")
        return 'invalid'

    try:
        for i, ticker in enumerate(batch, 1):
            try:
                status = _process_one(ticker, i, batch_n)
                if status == 'retry_later':
                    retry_later.append(ticker)
            except KeyboardInterrupt:
                print(f"\n\n⚠️  Process interrupted by user.")
                note = (
                    f"interrupted at {ticker} ({i}/{batch_n}); "
                    f"ok={successful} fail={failed}"
                )
                print(f"Saved progress: {note}")
                update_job_progress(JOB_REFRESH_MARKET_DATA, note)
                return False
            except Exception as e:
                print(f"Error processing {ticker}: {e} — continuing")
                failed += 1
                retry_later.append(ticker)
                if _db_is_locked(e):
                    request_early_wake(reason='Yahoo write locked')

            if i % 10 == 0:
                elapsed = time.time() - start_time
                avg_time_per_fetch = elapsed / max(fetches_done, 1)
                remaining = (batch_n - i) * avg_time_per_fetch
                note = (
                    f"batch {i}/{batch_n}; ok={successful} fail={failed} "
                    f"deferred={len(retry_later)}; last={ticker}"
                )
                update_job_progress(JOB_REFRESH_MARKET_DATA, note)
                print(f"\n📊 Progress: {i}/{batch_n} ({i * 100 / batch_n:.1f}%)")
                print(
                    f"   Successful: {successful}, Skipped (invalid): {skipped}, "
                    f"Failed/deferred: {failed}"
                )
                print(f"   Rate-limit waits: {rate_limit_waits}, transient retries: {transient_retries}")
                print(f"   Estimated time remaining: {remaining / 60:.1f} min\n")

        if retry_later:
            print(f"\n--- Retry pass: {len(retry_later)} deferred ticker(s) ---\n")
            update_job_progress(
                JOB_REFRESH_MARKET_DATA,
                f"retry pass: {len(retry_later)} deferred"
            )
            still_failed = []  # type: List[str]
            for j, ticker in enumerate(retry_later, 1):
                try:
                    status = _process_one(ticker, j, len(retry_later))
                    if status == 'retry_later':
                        still_failed.append(ticker)
                except KeyboardInterrupt:
                    print(f"\n\n⚠️  Interrupted during retry pass.")
                    update_job_progress(
                        JOB_REFRESH_MARKET_DATA,
                        f"interrupted retry at {ticker}; ok={successful}"
                    )
                    return False
                except Exception as e:
                    print(f"Retry error {ticker}: {e}")
                    still_failed.append(ticker)
            if still_failed:
                print(
                    f"\n⚠️  {len(still_failed)} ticker(s) still failed after retries "
                    f"(not marked fresh — front of next hourly batch). "
                    f"Examples: {still_failed[:10]}"
                )

        rconn = get_fundamentals_connection()
        try:
            _, overdue_after, stale_after = _select_yahoo_refresh_batch(
                rconn.cursor(), tickers, current_date, 0, max_age_days
            )
        finally:
            rconn.close()

        total_time = time.time() - start_time
        print(f"\n{'=' * 60}")
        print("Yahoo batch complete")
        print(f"{'=' * 60}")
        print(f"This batch: {batch_n} attempted")
        print(f"  ✓ Successful: {successful}")
        print(f"  ⊘ Skipped (invalid): {skipped}")
        if invalid_tickers:
            print(f"  Invalid examples: {invalid_tickers[:10]}")
        print(f"  ✗ Failed/deferred: {failed}")
        print(f"  ⏸️  Rate-limit waits: {rate_limit_waits}, transient retries: {transient_retries}")
        print(
            f"SLA after batch: {stale_after['within_sla']} within {max_age_days}d, "
            f"{stale_after['overdue']} still overdue/missing"
        )
        if stale_after['overdue'] > 0:
            print(
                f"⚠️  Still past SLA: {stale_after['overdue']} "
                f"(examples: {overdue_after[:8]})"
            )
        else:
            print(f"✓ All tickers within {max_age_days}-day SLA")
        print(f"Total time: {total_time / 60:.1f} min")
        print(f"{'=' * 60}\n")

        update_job_progress(
            JOB_REFRESH_MARKET_DATA,
            f"batch done ok={successful}/{batch_n}; overdue_left={stale_after['overdue']}"
        )
        try:
            set_runtime_flag('yahoo_universe_size', str(total_tickers))
        except Exception:
            pass
        log_event(
            'yahoo',
            f"Yahoo batch finished: ok={successful}/{batch_n}, "
            f"invalid={skipped}, deferred={failed}, overdue_left={stale_after['overdue']}",
            detail={
                'successful': successful,
                'batch_n': batch_n,
                'skipped': skipped,
                'failed': failed,
                'overdue_left': stale_after['overdue'],
                'skipped_today': stale_stats.get('skipped_today', 0),
            },
        )
        return True

    except Exception as e:
        print(f"\n⚠️  Fatal error during population: {e}")
        print(f"Saved {successful} tickers before error. Re-run to resume.")
        update_job_progress(
            JOB_REFRESH_MARKET_DATA,
            f"fatal: {e}; ok={successful}"
        )
        log_event('yahoo', f"Yahoo batch fatal error: {e}", level='error')
        if _db_is_locked(e):
            request_early_wake(reason='Yahoo batch locked')
        return False


# ============================================================================
# Scheduled jobs (run_trader backbone)
# ============================================================================

JOB_REFRESH_MARKET_DATA = 'refresh_market_data'
JOB_WATCHLIST_AND_BUYS = 'watchlist_and_buys'
JOB_SELL_CHECK = 'sell_check'
JOB_SCHWAB_SYNC = 'schwab_sync'

# Short console / Next Tasks labels
_JOB_DISPLAY_NAMES = {
    'refresh_market_data': 'Yahoo refresh',
    'schwab_sync': 'Schwab sync',
    'watchlist_and_buys': 'Watchlist & buys',
    'sell_check': 'Sell check',
}

# Relative-quiet status lines (what the loop is doing)
_JOB_STATUS_PHRASES = {
    'refresh_market_data': 'Refreshing Yahoo market data',
    'schwab_sync': 'Updating Schwab',
    'watchlist_and_buys': 'Updating watchlist & buys',
    'sell_check': 'Running sell check',
}

# Only these jobs require the US cash equity regular session.
JOBS_REQUIRE_MARKET_OPEN = frozenset({
    JOB_WATCHLIST_AND_BUYS,
    JOB_SELL_CHECK,
    JOB_SCHWAB_SYNC,
})

# Steady-state phase offsets (seconds after interval anchor / market open).
# Jobs stay periodic but do not need to fire in the same loop pass.
JOB_PHASE_OFFSET_SECONDS = {
    JOB_SCHWAB_SYNC: 0,
    JOB_SELL_CHECK: 120,
    JOB_WATCHLIST_AND_BUYS: 420,
}

# Soft-failure retry backoff (seconds). Includes phase so retries desynchronize.
JOB_RETRY_BACKOFF_SECONDS = {
    JOB_SCHWAB_SYNC: 45,
    JOB_SELL_CHECK: 150,
    JOB_WATCHLIST_AND_BUYS: 210,
}

# Rank matches by analyst upside; missing prices sort last under DESC.
_ANALYST_UPSIDE_ORDER_BY = (
    "ORDER BY CASE WHEN current_price > 0 AND target_price > 0 "
    "THEN ((target_price - current_price) / current_price) ELSE -1e99 END DESC"
)


def get_job_run(job_name: str) -> Optional[Dict[str, Any]]:
    """Return job_runs row for job_name, or None if missing."""
    job_name = uc.scoped_job_name(job_name)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        'SELECT job_name, interval_days, last_started, last_completed, status, progress_note '
        'FROM job_runs WHERE job_name = ?',
        (job_name,)
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    return {
        'job_name': row[0],
        'interval_days': row[1],
        'last_started': row[2],
        'last_completed': row[3],
        'status': row[4],
        'progress_note': row[5] if len(row) > 5 else None,
    }


def mark_job_started(job_name: str, interval_days: float) -> None:
    """Upsert job row and set status=running with last_started now."""
    job_name = uc.scoped_job_name(job_name)
    now = datetime.now().isoformat()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        '''
        INSERT INTO job_runs (job_name, interval_days, last_started, last_completed, status, progress_note)
        VALUES (?, ?, ?, NULL, 'running', 'started')
        ON CONFLICT(job_name) DO UPDATE SET
            interval_days = excluded.interval_days,
            last_started = excluded.last_started,
            status = 'running',
            progress_note = 'started'
        ''',
        (job_name, float(interval_days), now)
    )
    conn.commit()
    conn.close()


def reclaim_stale_running_job(
    job_name: str,
    max_minutes: Optional[float] = None,
) -> bool:
    """
    If a job was left status=running too long (crash / hung lock), mark it failed
    so the loop can start a fresh attempt. Returns True when reclaimed.
    """
    if max_minutes is None:
        max_minutes = float(getattr(config, 'STALE_JOB_RUNNING_MINUTES', 5))
    row = get_job_run(job_name)
    if not row or str(row.get('status') or '') != 'running':
        return False
    started_s = row.get('last_started')
    if not started_s:
        return False
    try:
        started = datetime.fromisoformat(str(started_s))
    except (TypeError, ValueError):
        return False
    age_min = (datetime.now() - started).total_seconds() / 60.0
    if age_min < float(max_minutes):
        return False
    note = 'stale running (%.0f min) — reclaimed' % age_min
    mark_job_failed(job_name, note)
    print_loop_status('Reclaimed stuck job %s after %.0f min' % (job_name, age_min))
    try:
        log_event('task', note, level='warn', detail={'job_name': job_name})
    except Exception:
        pass
    return True


def update_job_progress(job_name: str, note: str) -> None:
    """Save a short progress note (resume visibility); does not change due/completed."""
    job_name = uc.scoped_job_name(job_name)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE job_runs SET progress_note = ?, status = 'running' WHERE job_name = ?",
        (note, job_name)
    )
    conn.commit()
    conn.close()


def mark_job_completed(job_name: str) -> None:
    """Set last_completed now and status=idle (only call after full success)."""
    job_name = uc.scoped_job_name(job_name)
    now = datetime.now().isoformat()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        '''
        UPDATE job_runs
        SET last_completed = ?, status = 'idle', progress_note = 'complete'
        WHERE job_name = ?
        ''',
        (now, job_name)
    )
    conn.commit()
    conn.close()


def mark_job_failed(job_name: str, note: Optional[str] = None) -> None:
    """Set status=failed; leave last_completed unchanged so the job stays due."""
    job_name = uc.scoped_job_name(job_name)
    conn = get_connection()
    cursor = conn.cursor()
    if note is not None:
        cursor.execute(
            "UPDATE job_runs SET status = 'failed', progress_note = ? WHERE job_name = ?",
            (note, job_name)
        )
    else:
        cursor.execute(
            "UPDATE job_runs SET status = 'failed' WHERE job_name = ?",
            (job_name,)
        )
    conn.commit()
    conn.close()


def format_job_interval(interval_days: float) -> str:
    """
    Human interval for logs/dry-run: minutes if < 1h, hours if < 1d, else days.
    Values rounded to 2 decimals (integers omit trailing .00).
    """
    try:
        days = float(interval_days)
    except (TypeError, ValueError):
        return str(interval_days)
    if days < 0:
        days = 0.0
    minutes = days * 24.0 * 60.0
    if minutes < 60.0 - 1e-9:
        m = round(minutes, 2)
        if abs(m - int(m)) < 1e-9:
            return '%d min' % int(m)
        return '%.2f min' % m
    hours = days * 24.0
    if hours < 24.0 - 1e-9:
        h = round(hours, 2)
        if abs(h - int(h)) < 1e-9:
            n = int(h)
            return '%d hour' % n if n == 1 else '%d hours' % n
        return '%.2f hours' % h
    d = round(days, 2)
    if abs(d - int(d)) < 1e-9:
        n = int(d)
        return '%d day' % n if n == 1 else '%d days' % n
    return '%.2f days' % d


def _market_tz():
    """America/New_York zoneinfo (stdlib 3.9+)."""
    try:
        from zoneinfo import ZoneInfo
        name = getattr(config, 'MARKET_TIMEZONE', 'America/New_York')
        return ZoneInfo(name)
    except Exception:
        return None


def _now_market() -> datetime:
    """Current time in market timezone (naive local fallback)."""
    tz = _market_tz()
    if tz is not None:
        return datetime.now(tz)
    return datetime.now()


def is_us_equity_market_open(when: Optional[datetime] = None) -> bool:
    """
    True during US regular trading hours (default 9:30–16:00 America/New_York),
    Monday–Friday. Does not skip exchange holidays.
    """
    now = when if when is not None else _now_market()
    tz = _market_tz()
    if tz is not None and now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    elif tz is not None and now.tzinfo is not None:
        now = now.astimezone(tz)
    if now.weekday() >= 5:
        return False
    open_h = int(getattr(config, 'MARKET_OPEN_HOUR', 9))
    open_m = int(getattr(config, 'MARKET_OPEN_MINUTE', 30))
    close_h = int(getattr(config, 'MARKET_CLOSE_HOUR', 16))
    close_m = int(getattr(config, 'MARKET_CLOSE_MINUTE', 0))
    minutes = now.hour * 60 + now.minute
    open_mins = open_h * 60 + open_m
    close_mins = close_h * 60 + close_m
    return open_mins <= minutes < close_mins


def next_us_equity_market_open(when: Optional[datetime] = None) -> datetime:
    """Next regular-session open (local/ET datetime, timezone-aware when possible)."""
    now = when if when is not None else _now_market()
    tz = _market_tz()
    if tz is not None and now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    elif tz is not None and now.tzinfo is not None:
        now = now.astimezone(tz)
    open_h = int(getattr(config, 'MARKET_OPEN_HOUR', 9))
    open_m = int(getattr(config, 'MARKET_OPEN_MINUTE', 30))
    # Walk forward day-by-day to next weekday open (skip if already before today's open)
    for offset in range(0, 10):
        day = (now + timedelta(days=offset)).date()
        if day.weekday() >= 5:
            continue
        candidate = datetime(day.year, day.month, day.day, open_h, open_m, 0)
        if tz is not None:
            candidate = candidate.replace(tzinfo=tz)
        if candidate > now:
            return candidate
    # Fallback: tomorrow 9:30
    nxt = now + timedelta(days=1)
    return datetime(nxt.year, nxt.month, nxt.day, open_h, open_m, 0,
                    tzinfo=tz if tz is not None else None)


def _force_job_due(job_name: str) -> None:
    """Clear last_completed so the job is due on the next scheduler check."""
    init_database()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT job_name FROM job_runs WHERE job_name = ?', (job_name,))
    if cursor.fetchone():
        cursor.execute(
            '''
            UPDATE job_runs
            SET last_completed = NULL, status = 'idle',
                progress_note = 'reset for market open'
            WHERE job_name = ?
            ''',
            (job_name,)
        )
    else:
        cursor.execute(
            '''
            INSERT INTO job_runs
                (job_name, interval_days, last_started, last_completed, status, progress_note)
            VALUES (?, NULL, NULL, NULL, 'idle', 'reset for market open')
            ''',
            (job_name,)
        )
    conn.commit()
    conn.close()


def _set_job_next_due_in(
    job_name: str,
    interval_days: float,
    due_in_seconds: float,
    note: str = 'staggered',
) -> None:
    """
    Schedule job due after due_in_seconds by backdating last_completed.
    Clears mid-cycle 'failed/running' immediacy so is_job_due waits.
    """
    job_name = uc.scoped_job_name(job_name)
    due_in_seconds = max(0.0, float(due_in_seconds))
    interval_sec = max(1.0, float(interval_days) * 86400.0)
    completed = datetime.now() - timedelta(seconds=interval_sec - due_in_seconds)
    completed_s = completed.isoformat(timespec='seconds')
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        '''
        INSERT INTO job_runs
            (job_name, interval_days, last_started, last_completed, status, progress_note)
        VALUES (?, ?, ?, ?, 'idle', ?)
        ON CONFLICT(job_name) DO UPDATE SET
            interval_days = excluded.interval_days,
            last_started = excluded.last_started,
            last_completed = excluded.last_completed,
            status = 'idle',
            progress_note = excluded.progress_note
        ''',
        (job_name, float(interval_days), completed_s, completed_s, note),
    )
    conn.commit()
    conn.close()


def defer_failed_job(job_name: str, interval_days: float) -> float:
    """
    After a soft failure, push next attempt out by job backoff (not 30s hammer).
    Returns backoff seconds used.
    """
    base = job_name.split(':', 1)[-1] if ':' in job_name else job_name
    # Prefer unscoped name for lookup
    bare = base
    for known in (
        JOB_SCHWAB_SYNC, JOB_SELL_CHECK, JOB_WATCHLIST_AND_BUYS, JOB_REFRESH_MARKET_DATA,
    ):
        if job_name.endswith(known) or job_name == known:
            bare = known
            break
    backoff = float(JOB_RETRY_BACKOFF_SECONDS.get(bare, 60))
    _set_job_next_due_in(
        bare, interval_days, backoff, note='retry deferred %.0fs' % backoff,
    )
    return backoff


def maybe_reset_jobs_at_market_open() -> bool:
    """
    Once per RTH session day: stagger RTH jobs after the open (phase offsets)
    so sync / sell / watchlist do not all fire in one pass.
    Returns True if a reset was applied this call.
    """
    if not is_us_equity_market_open():
        return False
    today = _now_market().strftime('%Y-%m-%d')
    if get_runtime_flag('rth_session_started') == today:
        return False
    set_runtime_flag('rth_session_started', today)
    intervals = {
        JOB_SCHWAB_SYNC: _schwab_sync_interval_days(),
        JOB_SELL_CHECK: _sell_check_interval_days(),
        JOB_WATCHLIST_AND_BUYS: _watchlist_job_interval_days(),
    }
    for job_name in JOBS_REQUIRE_MARKET_OPEN:
        phase = float(JOB_PHASE_OFFSET_SECONDS.get(job_name, 0))
        _set_job_next_due_in(
            job_name,
            intervals.get(job_name, 1.0 / 24.0),
            phase,
            note='market open stagger +%ss' % int(phase),
        )
    log_event(
        'task',
        'Market open — staggered RTH job cycles for %s' % today,
    )
    print('Market open detected — RTH jobs staggered for this session.')
    return True


def is_job_due(job_name: str, interval_days: float) -> bool:
    """
    True if never completed, interval elapsed since last_completed, or an incomplete
    cycle is in progress (started after last completion / failed mid-run).
    """
    row = get_job_run(job_name)
    if not row or not row.get('last_completed'):
        return True

    # Mid-cycle resume: started a run that never finished successfully
    status = row.get('status')
    last_started = row.get('last_started')
    last_completed = row.get('last_completed')
    if status in ('running', 'failed') and last_started:
        try:
            started = datetime.fromisoformat(last_started)
            completed = datetime.fromisoformat(last_completed)
            if started > completed:
                return True
        except (TypeError, ValueError):
            return True

    try:
        completed_dt = datetime.fromisoformat(last_completed)
    except (TypeError, ValueError):
        return True
    return datetime.now() - completed_dt >= timedelta(days=float(interval_days))


def next_job_due_at(job_name: str, interval_days: float) -> Optional[datetime]:
    """Return next due datetime, or None if due immediately / never completed."""
    if is_job_due(job_name, interval_days):
        return None
    row = get_job_run(job_name)
    if not row or not row.get('last_completed'):
        return None
    try:
        last_completed = datetime.fromisoformat(row['last_completed'])
    except (TypeError, ValueError):
        return None
    return last_completed + timedelta(days=float(interval_days))


def refresh_market_data(batch_size: Optional[int] = None) -> bool:
    """
    Yahoo refresh job: oldest/missing tickers first.

    Args:
        batch_size: Max tickers this run. None = YAHOO_BATCH_SIZE (hourly default).
                    0 or negative = full catch-up (all tickers, oldest-first).

    Marks job complete after each successful run so the hourly schedule can resume.
    Interrupted runs stay due for immediate resume.

    Returns:
        True if the run finished; False if aborted early.
    """
    init_database()
    interval_days = _market_data_job_interval_days()
    hours = float(getattr(config, 'MARKET_DATA_JOB_INTERVAL_HOURS', 1))
    default_batch = int(getattr(config, 'YAHOO_BATCH_SIZE', 150))
    sla_days = int(getattr(config, 'MARKET_DATA_REFRESH_DAYS', 7))
    effective = default_batch if batch_size is None else batch_size
    mode = 'FULL CATCH-UP' if (effective is not None and effective <= 0) else f'batch={effective}'
    print("=" * 60)
    print(
        f"Job: {JOB_REFRESH_MARKET_DATA} "
        f"(every {hours:g} hour(s), {mode}, SLA={sla_days}d)"
    )
    print("Oldest/missing first; commits every 10 tickers.")
    print("=" * 60)
    mark_job_started(JOB_REFRESH_MARKET_DATA, interval_days)
    try:
        ok = populate_database(batch_size=batch_size)
        if ok:
            mark_job_completed(JOB_REFRESH_MARKET_DATA)
            print(f"Job {JOB_REFRESH_MARKET_DATA}: completed ({mode})")
            return True
        mark_job_failed(
            JOB_REFRESH_MARKET_DATA,
            note='incomplete — re-run refresh_market_data() to resume'
        )
        print(
            f"Job {JOB_REFRESH_MARKET_DATA}: incomplete/interrupted. "
            f"Re-run st.refresh_market_data() (or run_trader); "
            f"progress is kept via fundamentals.last_updated."
        )
        return False
    except Exception as e:
        mark_job_failed(JOB_REFRESH_MARKET_DATA, note=f'error: {e}')
        print(f"Job {JOB_REFRESH_MARKET_DATA}: error: {e}")
        print("Re-run to resume; progress is saved per ticker in fundamentals.last_updated.")
        return False


def _sell_check_interval_days() -> float:
    """Convert SELL_CHECK_INTERVAL_MINUTES to fractional days for job_runs."""
    minutes = float(getattr(config, 'SELL_CHECK_INTERVAL_MINUTES', 15))
    return minutes / (24.0 * 60.0)


def _schwab_sync_interval_days() -> float:
    """Light account reconcile cadence (pending fills + marks / stop exits)."""
    minutes = float(getattr(config, 'SCHWAB_SYNC_INTERVAL_MINUTES', 5))
    return max(minutes, 0.0) / (24.0 * 60.0)


def _watchlist_job_interval_days() -> float:
    """Watchlist/buys interval as fractional days (minutes preferred, else hours)."""
    if hasattr(config, 'WATCHLIST_JOB_INTERVAL_MINUTES'):
        minutes = float(getattr(config, 'WATCHLIST_JOB_INTERVAL_MINUTES', 30))
        return max(minutes, 0.0) / (24.0 * 60.0)
    if hasattr(config, 'WATCHLIST_JOB_INTERVAL_HOURS'):
        hours = float(getattr(config, 'WATCHLIST_JOB_INTERVAL_HOURS', 1))
        return max(hours, 0.0) / 24.0
    return float(getattr(config, 'WATCHLIST_JOB_INTERVAL_DAYS', 1))


def get_scheduled_jobs() -> List[Tuple[str, Any, float]]:
    """
    Ordered job list for run_trader / dry_run_system.
    Each item: (job_name, callable, interval_days).
    """
    return [
        (JOB_REFRESH_MARKET_DATA, refresh_market_data,
         _market_data_job_interval_days()),
        (JOB_SCHWAB_SYNC, run_schwab_sync_job, _schwab_sync_interval_days()),
        (JOB_WATCHLIST_AND_BUYS, run_watchlist_and_buys_job,
         _watchlist_job_interval_days()),
        (JOB_SELL_CHECK, run_sell_check_job, _sell_check_interval_days()),
    ]


def _as_naive_local(dt: datetime) -> datetime:
    """Compare wake times in naive local clock (strip tz if present)."""
    if dt.tzinfo is None:
        return dt
    return dt.astimezone().replace(tzinfo=None)


def _run_trader_pass_for_user(
    user: Dict[str, Any],
    jobs: List[Tuple[str, Any, float]],
    market_open: bool,
    next_wake: Optional[datetime],
) -> Optional[datetime]:
    """Run due jobs for one user; return updated next_wake candidate."""
    uid = int(user['id'])
    uname = user.get('username') or str(uid)
    with uc.use_user(uid):
        _sync_schwab_globals(uid)
        # Gate 2: no per-user jobs until algorithm_start (stops john / unset accounts).
        if not get_algorithm_start():
            print_loop_status(
                '[%s] Skipping — onboard in Actions (Schwab → settings → Run)'
                % uname
            )
            return next_wake
        print_loop_status('Starting pass for %s…' % uname)
        maybe_reinit_schwab_client(uid)
        maybe_reset_jobs_at_market_open()
        try:
            maybe_record_daily_equity_close()
        except Exception as e:
            print('maybe_record_daily_equity_close (%s): %s' % (uname, e))

        for job_name, job_fn, interval_days in jobs:
            # Shared Yahoo job runs once outside per-user loop
            if job_name == JOB_REFRESH_MARKET_DATA:
                continue
            needs_rth = job_name in JOBS_REQUIRE_MARKET_OPEN
            label = format_job_interval(interval_days)
            title = _JOB_DISPLAY_NAMES.get(job_name, job_name)
            phrase = _JOB_STATUS_PHRASES.get(job_name, title)

            # Unstick jobs left "running" after a crash / hung DB lock
            reclaim_stale_running_job(job_name)
            row = get_job_run(job_name)
            if row and str(row.get('status') or '') == 'running':
                # Do not re-enter — mark_job_started would reset the reclaim timer
                print_loop_status(
                    '[%s] %s still running — waiting to reclaim'
                    % (uname, phrase)
                )
                wake_candidate = datetime.now() + timedelta(minutes=1)
                if next_wake is None or wake_candidate < next_wake:
                    next_wake = wake_candidate
                continue

            if needs_rth and not market_open:
                nxt_open = next_us_equity_market_open()
                print(
                    f"\n[{uname}] Market closed — skip {job_name} "
                    f"(every {label}; next open {nxt_open.isoformat()})"
                )
                wake_candidate = _as_naive_local(nxt_open)
                if next_wake is None or wake_candidate < next_wake:
                    next_wake = wake_candidate
                continue

            if is_job_due(job_name, interval_days):
                print_loop_status('[%s] %s…' % (uname, phrase))
                print(f"\n[{uname}] Due: {job_name} (every {label}) — running now")
                log_event('task', f"Task started: {job_name}")
                ok = False
                try:
                    ok = bool(job_fn())
                    if ok:
                        print_loop_status('[%s] %s done' % (uname, phrase))
                    else:
                        print_loop_status(
                            '[%s] %s did not complete — deferred retry'
                            % (uname, phrase)
                        )
                except Exception as e:
                    print_loop_status('[%s] %s failed: %s' % (uname, phrase, e))
                    print('[%s] job %s failed: %s' % (uname, job_name, e))
                    try:
                        mark_job_failed(job_name, str(e)[:200])
                    except Exception:
                        pass
                    ok = False
                if not ok:
                    backoff = defer_failed_job(job_name, interval_days)
                    print_loop_status(
                        '[%s] %s retry in %.0fs' % (uname, phrase, backoff)
                    )
                    due_at = next_job_due_at(job_name, interval_days)
                    if due_at is not None and (
                        next_wake is None or due_at < next_wake
                    ):
                        next_wake = due_at
                    else:
                        wake_candidate = datetime.now() + timedelta(seconds=backoff)
                        if next_wake is None or wake_candidate < next_wake:
                            next_wake = wake_candidate
            else:
                due_at = next_job_due_at(job_name, interval_days)
                if due_at is not None:
                    print(
                        f"\n[{uname}] Not due: {job_name} — next run at "
                        f"{due_at.isoformat()} (every {label})"
                    )
                    if next_wake is None or due_at < next_wake:
                        next_wake = due_at
                else:
                    print(f"\n[{uname}] Not due: {job_name} (every {label})")
    return next_wake


def run_trader(once: bool = True) -> None:
    """
    Parent entry: run due scheduled jobs for each active user.

    Yahoo refresh runs once (shared). Watchlist/buys and sell_check run per user
    during US RTH only.
    """
    setup_file_logging()
    init_database()
    # Global wake flag (not user-scoped)
    prev = uc.current_user_id()
    uc.set_current_user_id(None)
    set_runtime_flag('last_loop_wake', datetime.now().isoformat(timespec='seconds'))
    uc.set_current_user_id(prev)

    jobs = get_scheduled_jobs()
    sleep_chunk_seconds = 60

    while True:
        try:
            wake_ts = datetime.now().isoformat(timespec='seconds')
            prev = uc.current_user_id()
            uc.set_current_user_id(None)
            set_runtime_flag('last_loop_wake', wake_ts)
            uc.set_current_user_id(prev)

            market_open = is_us_equity_market_open()
            print_loop_status(
                'Loop wake %s — market %s'
                % (wake_ts, 'OPEN' if market_open else 'CLOSED')
            )
            print("=" * 60)
            print(f"run_trader (once={once}) — {wake_ts}")
            print("US RTH: %s" % ('OPEN' if market_open else 'CLOSED'))
            print("=" * 60)

            next_wake = None  # type: Optional[datetime]

            # Shared Yahoo refresh (no per-user scope needed)
            for job_name, job_fn, interval_days in jobs:
                if job_name != JOB_REFRESH_MARKET_DATA:
                    continue
                label = format_job_interval(interval_days)
                title = _JOB_DISPLAY_NAMES.get(job_name, job_name)
                phrase = _JOB_STATUS_PHRASES.get(job_name, title)
                # Use first user context only for job_runs row naming (global job name)
                users = uc.list_active_users()
                ctx_uid = int(users[0]['id']) if users else None
                ctx = uc.use_user(ctx_uid) if ctx_uid else _nullcontext()
                with ctx:
                    reclaim_stale_running_job(job_name)
                    row = get_job_run(job_name)
                    if row and str(row.get('status') or '') == 'running':
                        print_loop_status(
                            '%s still running — waiting to reclaim' % phrase
                        )
                        wake_candidate = datetime.now() + timedelta(minutes=1)
                        if next_wake is None or wake_candidate < next_wake:
                            next_wake = wake_candidate
                        continue
                    if is_job_due(job_name, interval_days):
                        print_loop_status('%s…' % phrase)
                        print(f"\nDue: {job_name} (every {label}) — running now")
                        log_event('task', f"Task started: {job_name}")
                        ok = False
                        try:
                            ok = bool(job_fn())
                            if ok:
                                print_loop_status('%s done' % phrase)
                            else:
                                print_loop_status(
                                    '%s did not complete — will retry soon' % phrase
                                )
                        except Exception as e:
                            print_loop_status('%s failed: %s' % (phrase, e))
                            try:
                                mark_job_failed(job_name, str(e)[:200])
                            except Exception:
                                pass
                            ok = False
                        if not ok:
                            request_early_wake(reason='%s needs retry' % phrase)
                    else:
                        due_at = next_job_due_at(job_name, interval_days)
                        if due_at is not None:
                            print(
                                f"\nNot due: {job_name} — next run at {due_at.isoformat()} "
                                f"(every {label})"
                            )
                            if next_wake is None or due_at < next_wake:
                                next_wake = due_at

            for user in uc.list_active_users():
                next_wake = _run_trader_pass_for_user(
                    user, jobs, market_open, next_wake
                )

            if once:
                print_loop_status('Trader pass complete')
                print("\nrun_trader batch pass complete.")
                log_event('task', 'Trader loop pass complete')
                return

            next_wake = take_early_wake(next_wake)
            if next_wake is None:
                shortest = min(interval for _, _, interval in jobs)
                next_wake = datetime.now() + timedelta(days=shortest)
                print(f"\nNo next due time; sleeping until {next_wake.isoformat()}")

            print_loop_status(
                'Sleeping until %s' % next_wake.strftime('%H:%M:%S')
            )
            while datetime.now() < next_wake:
                remaining = (next_wake - datetime.now()).total_seconds()
                if remaining <= 0:
                    break
                time.sleep(min(sleep_chunk_seconds, remaining))
        except sqlite3.OperationalError as e:
            if 'locked' not in str(e).lower():
                raise
            print_loop_status('Database locked — retry in 30s')
            print('Warning: database locked in trader loop — sleeping 30s then retrying')
            request_early_wake(30, reason='loop database locked')
            time.sleep(30)
            if once:
                raise


class _nullcontext(object):
    def __enter__(self):
        return None

    def __exit__(self, *args):
        return False


# ============================================================================
# Streaming Data Functions (Schwab API - Real-time)
# ============================================================================

def setup_streaming(tickers: List[str]):
    """
    Initialize Schwab streaming for selected tickers.
    Returns a streaming client object.
    
    Note: This is a placeholder for future streaming implementation.
    Streaming requires async/await patterns and WebSocket connections.
    For now, use get_streaming_data() to get individual quotes.
    """
    if not SCHWAB_AVAILABLE:
        print("Warning: Schwab API not available. Using placeholder.")
        return None
    
    # TODO: Future implementation with schwabdev streaming
    # Streaming requires async/await and WebSocket handling
    # For now, use get_streaming_data() for individual quotes
    print(f"Note: Streaming not yet implemented. Use get_streaming_data() for individual quotes.")
    print(f"Would set up streaming for: {tickers}")
    return None


def get_streaming_data(ticker: str) -> Optional[Dict[str, Any]]:
    """
    Get real-time price/data for a ticker from Schwab API.
    Can be called independently to test on a single stock.
    Returns current price, volume, and other real-time data.
    Trade decisions must not invent prices — returns error dict if API unavailable.
    """
    if not SCHWAB_AVAILABLE or SCHWAB_CLIENT is None:
        print(f"Warning: Schwab API not available for {ticker}.")
        return {
            'ticker': ticker,
            'price': None,
            'error': 'Schwab API not available',
            'timestamp': datetime.now().isoformat(),
        }
    
    try:
        # Get quote from Schwab API
        response = SCHWAB_CLIENT.quote(ticker)
        quote_data = response.json()
        
        # Handle different response formats
        # Response might be a dict with ticker as key, or direct dict, or list
        if isinstance(quote_data, dict):
            # Check if ticker is a key in the response (common format)
            if ticker in quote_data:
                quote_data = quote_data[ticker]
            # If it's already the quote data, use it as is
        elif isinstance(quote_data, list) and len(quote_data) > 0:
            # If it's a list, take the first element
            quote_data = quote_data[0]
        
        # Extract from nested structure: quote, extended, fundamental, reference, regular sections
        quote_section = quote_data.get('quote', {})
        extended_section = quote_data.get('extended', {})
        fundamental_section = quote_data.get('fundamental', {})
        reference_section = quote_data.get('reference', {})
        regular_section = quote_data.get('regular', {})
        
        # Extract price - prefer quote section, fallback to regular or extended
        price = (quote_section.get('lastPrice') or 
                regular_section.get('regularMarketLastPrice') or 
                extended_section.get('lastPrice') or
                quote_section.get('mark') or
                quote_section.get('closePrice'))
        
        # Extract bid and ask from quote section
        bid = quote_section.get('bidPrice')
        ask = quote_section.get('askPrice')
        bid_size = quote_section.get('bidSize')
        ask_size = quote_section.get('askSize')
        
        # Extract price-related fields from quote section
        open_price = quote_section.get('openPrice')
        high_price = quote_section.get('highPrice')
        low_price = quote_section.get('lowPrice')
        previous_close = quote_section.get('closePrice')
        mark = quote_section.get('mark')
        
        # Change fields from quote section
        net_change = quote_section.get('netChange')
        net_percent_change = quote_section.get('netPercentChange')
        mark_change = quote_section.get('markChange')
        mark_percent_change = quote_section.get('markPercentChange')
        
        # Volume from quote section
        volume = quote_section.get('totalVolume')
        
        # Market data
        exchange = reference_section.get('exchangeName') or reference_section.get('exchange')
        quote_time = quote_section.get('quoteTime')
        trade_time = quote_section.get('tradeTime')
        market_status = quote_section.get('securityStatus')
        
        # 52-week high/low from quote section
        week_52_high = quote_section.get('52WeekHigh')
        week_52_low = quote_section.get('52WeekLow')
        
        # Extended hours data
        extended_last_price = extended_section.get('lastPrice')
        extended_volume = extended_section.get('totalVolume')
        extended_bid = extended_section.get('bidPrice')
        extended_ask = extended_section.get('askPrice')
        
        # Post-market change
        post_market_change = quote_section.get('postMarketChange')
        post_market_percent_change = quote_section.get('postMarketPercentChange')
        
        # Fundamental data
        pe_ratio = fundamental_section.get('peRatio')
        dividend_yield = fundamental_section.get('divYield')
        eps = fundamental_section.get('eps')
        shares_outstanding = fundamental_section.get('sharesOutstanding')
        avg_10_day_volume = fundamental_section.get('avg10DaysVolume')
        avg_1_year_volume = fundamental_section.get('avg1YearVolume')
        
        # Dividend information
        div_amount = fundamental_section.get('divAmount')
        div_freq = fundamental_section.get('divFreq')
        div_pay_amount = fundamental_section.get('divPayAmount')
        next_div_ex_date = fundamental_section.get('nextDivExDate')
        next_div_pay_date = fundamental_section.get('nextDivPayDate')
        
        # Reference data
        cusip = reference_section.get('cusip')
        description = reference_section.get('description')
        is_shortable = reference_section.get('isShortable')
        is_hard_to_borrow = reference_section.get('isHardToBorrow')
        
        # Other metadata
        asset_main_type = quote_data.get('assetMainType')
        asset_sub_type = quote_data.get('assetSubType')
        quote_type = quote_data.get('quoteType')
        realtime = quote_data.get('realtime')
        
        # If still no price, try bid/ask mid-point
        if price is None:
            if bid is not None and ask is not None:
                price = (float(bid) + float(ask)) / 2
            elif bid is not None:
                price = float(bid)
            elif ask is not None:
                price = float(ask)
        
        if price is None:
            print(f"Warning: Could not extract price from quote data for {ticker}")
            print(f"Available keys in response: {list(quote_data.keys()) if isinstance(quote_data, dict) else 'Not a dict'}")
            print(f"Raw response structure: {type(quote_data)}")
            # Return None but include raw_data for debugging
            return {
                'ticker': ticker,
                'price': None,
                'error': 'Could not extract price',
                'raw_data': quote_data,
                'timestamp': datetime.now().isoformat()
            }
        
        # Build comprehensive result dictionary
        result = {
            'ticker': ticker,
            'timestamp': datetime.now().isoformat(),
            
            # Price data (main quote section)
            'price': float(price) if price is not None else 0.0,
            'bid': float(bid) if bid is not None else None,
            'ask': float(ask) if ask is not None else None,
            'mark': float(mark) if mark is not None else None,
            'open': float(open_price) if open_price is not None else None,
            'high': float(high_price) if high_price is not None else None,
            'low': float(low_price) if low_price is not None else None,
            'previous_close': float(previous_close) if previous_close is not None else None,
            
            # Change data
            'net_change': float(net_change) if net_change is not None else None,
            'net_percent_change': float(net_percent_change) if net_percent_change is not None else None,
            'mark_change': float(mark_change) if mark_change is not None else None,
            'mark_percent_change': float(mark_percent_change) if mark_percent_change is not None else None,
            'post_market_change': float(post_market_change) if post_market_change is not None else None,
            'post_market_percent_change': float(post_market_percent_change) if post_market_percent_change is not None else None,
            
            # Volume data
            'volume': int(volume) if volume is not None else None,
            'bid_size': int(bid_size) if bid_size is not None else None,
            'ask_size': int(ask_size) if ask_size is not None else None,
            
            # Market data
            'exchange': exchange,
            'quote_time': quote_time,
            'trade_time': trade_time,
            'market_status': market_status,
            'realtime': realtime,
            
            # 52-week data
            'week_52_high': float(week_52_high) if week_52_high is not None else None,
            'week_52_low': float(week_52_low) if week_52_low is not None else None,
            
            # Extended hours data
            'extended_last_price': float(extended_last_price) if extended_last_price is not None else None,
            'extended_volume': int(extended_volume) if extended_volume is not None else None,
            'extended_bid': float(extended_bid) if extended_bid is not None else None,
            'extended_ask': float(extended_ask) if extended_ask is not None else None,
            
            # Fundamental data
            'pe_ratio': float(pe_ratio) if pe_ratio is not None else None,
            'dividend_yield': float(dividend_yield) if dividend_yield is not None else None,
            'eps': float(eps) if eps is not None else None,
            'shares_outstanding': int(shares_outstanding) if shares_outstanding is not None else None,
            'avg_10_day_volume': float(avg_10_day_volume) if avg_10_day_volume is not None else None,
            'avg_1_year_volume': float(avg_1_year_volume) if avg_1_year_volume is not None else None,
            
            # Dividend information
            'div_amount': float(div_amount) if div_amount is not None else None,
            'div_freq': int(div_freq) if div_freq is not None else None,
            'div_pay_amount': float(div_pay_amount) if div_pay_amount is not None else None,
            'next_div_ex_date': next_div_ex_date,
            'next_div_pay_date': next_div_pay_date,
            
            # Reference data
            'cusip': cusip,
            'description': description,
            'is_shortable': is_shortable,
            'is_hard_to_borrow': is_hard_to_borrow,
            
            # Metadata
            'asset_main_type': asset_main_type,
            'asset_sub_type': asset_sub_type,
            'quote_type': quote_type,
            
            # Raw data for debugging/advanced use
            'raw_data': quote_data
        }
        
        return result
        
    except Exception as e:
        print(f"Error fetching Schwab data for {ticker}: {e}")
        return None


def monitor_streaming(tickers: List[str], callback):
    """
    Monitor streaming data and call callback when criteria met.
    Callback should accept (ticker, streaming_data) as arguments.
    """
    if not SCHWAB_AVAILABLE:
        print("Warning: Schwab API not available. Cannot monitor streaming.")
        return
    
    # TODO: Implement with schwabdev
    # stream = setup_streaming(tickers)
    # while True:
    #     for ticker in tickers:
    #         data = get_streaming_data(ticker)
    #         callback(ticker, data)
    
    print(f"Placeholder: Would monitor streaming for {tickers}")


# ============================================================================
# Query Functions
# ============================================================================

def query_stocks(criteria: str) -> pd.DataFrame:
    """
    Query stocks based on SQL criteria (similar to your example).
    Returns a pandas DataFrame.
    """
    sql = criteria or ''
    if re.search(r'\bfundamentals\b', sql, re.I):
        conn = get_fundamentals_connection()
    else:
        conn = get_connection()
    try:
        df = pd.read_sql(criteria, conn)
        return df
    except Exception as e:
        print(f"Error executing query: {e}")
        return pd.DataFrame()
    finally:
        conn.close()


def get_fundamentals(ticker: str, verbose: bool = False) -> Optional[Dict[str, Any]]:
    """
    Get fundamental data for a ticker from database. Returns all columns and prints them.
    
    Args:
        ticker: Stock ticker symbol
        verbose: If True, prints detailed information. If False, prints only key stats.
    
    Returns:
        Dictionary with all fundamental data, or None if not found
    """
    conn = get_fundamentals_connection()
    cursor = conn.cursor()
    
    # Get all column names dynamically
    cursor.execute("PRAGMA table_info(fundamentals)")
    columns_info = cursor.fetchall()
    column_names = [row[1] for row in columns_info]
    
    # Select all columns
    cursor.execute('SELECT * FROM fundamentals WHERE ticker = ?', (ticker,))
    
    row = cursor.fetchone()
    conn.close()
    
    if row:
        # Build dictionary dynamically from all columns
        result = dict(zip(column_names, row))
        
        if verbose:
            # Print all fundamentals data (verbose mode)
            print(f"\n{'='*80}")
            print(f"Fundamentals for {ticker}")
            print(f"{'='*80}")
            
            # Group and print fields in a readable format
            # Basic Info
            print("\n📊 BASIC INFO:")
            if result.get('sector'):
                print(f"  Sector: {result.get('sector')}")
            if result.get('current_price'):
                print(f"  Current Price: ${result.get('current_price'):,.2f}")
            if result.get('previous_close'):
                print(f"  Previous Close: ${result.get('previous_close'):,.2f}")
            if result.get('last_updated'):
                print(f"  Last Updated: {result.get('last_updated')}")
            
            # Market Data
            print("\n💹 MARKET DATA:")
            if result.get('market_cap'):
                print(f"  Market Cap: ${result.get('market_cap'):,}")
            if result.get('avg_volume'):
                print(f"  Average Volume: {result.get('avg_volume'):,}")
            if result.get('beta') is not None:
                print(f"  Beta: {result.get('beta'):.2f}")
            if result.get('short_float') is not None:
                print(f"  Short Float: {result.get('short_float'):.2%}")
            if result.get('week_52_high'):
                print(f"  52-Week High: ${result.get('week_52_high'):,.2f}")
            if result.get('week_52_low'):
                print(f"  52-Week Low: ${result.get('week_52_low'):,.2f}")
            if result.get('week_52_change') is not None:
                print(f"  52-Week Change: {result.get('week_52_change'):.2%}")
            
            # Valuation Metrics
            print("\n💰 VALUATION METRICS:")
            if result.get('pe_ratio') is not None:
                print(f"  P/E Ratio: {result.get('pe_ratio'):.2f}")
            if result.get('forward_pe') is not None:
                print(f"  Forward P/E: {result.get('forward_pe'):.2f}")
            if result.get('peg_ratio') is not None:
                print(f"  PEG Ratio: {result.get('peg_ratio'):.2f}")
            if result.get('price_to_book') is not None:
                print(f"  Price to Book: {result.get('price_to_book'):.2f}")
            if result.get('price_to_sales') is not None:
                print(f"  Price to Sales: {result.get('price_to_sales'):.2f}")
            if result.get('enterprise_to_revenue') is not None:
                print(f"  Enterprise to Revenue: {result.get('enterprise_to_revenue'):.2f}")
            if result.get('enterprise_to_ebitda') is not None:
                print(f"  Enterprise to EBITDA: {result.get('enterprise_to_ebitda'):.2f}")
            if result.get('target_price'):
                print(f"  Target Price: ${result.get('target_price'):,.2f}")
            
            # Financial Health
            print("\n🏦 FINANCIAL HEALTH:")
            if result.get('debt_to_equity') is not None:
                print(f"  Debt to Equity: {result.get('debt_to_equity'):.2f}")
            if result.get('current_ratio') is not None:
                print(f"  Current Ratio: {result.get('current_ratio'):.2f}")
            if result.get('quick_ratio') is not None:
                print(f"  Quick Ratio: {result.get('quick_ratio'):.2f}")
            if result.get('total_cash'):
                print(f"  Total Cash: ${result.get('total_cash'):,}")
            if result.get('total_debt'):
                print(f"  Total Debt: ${result.get('total_debt'):,}")
            if result.get('total_revenue'):
                print(f"  Total Revenue: ${result.get('total_revenue'):,}")
            if result.get('gross_profits'):
                print(f"  Gross Profits: ${result.get('gross_profits'):,}")
            if result.get('free_cashflow'):
                print(f"  Free Cash Flow: ${result.get('free_cashflow'):,}")
            if result.get('operating_cashflow'):
                print(f"  Operating Cash Flow: ${result.get('operating_cashflow'):,}")
            
            # Growth Metrics
            print("\n📈 GROWTH METRICS:")
            if result.get('revenue_growth') is not None:
                print(f"  Revenue Growth: {result.get('revenue_growth'):.2%}")
            if result.get('earnings_growth') is not None:
                print(f"  Earnings Growth: {result.get('earnings_growth'):.2%}")
            if result.get('earnings_quarterly_growth') is not None:
                print(f"  Earnings Quarterly Growth: {result.get('earnings_quarterly_growth'):.2%}")
            if result.get('profit_margins') is not None:
                print(f"  Profit Margins: {result.get('profit_margins'):.2%}")
            if result.get('gross_margins') is not None:
                print(f"  Gross Margins: {result.get('gross_margins'):.2%}")
            if result.get('operating_margins') is not None:
                print(f"  Operating Margins: {result.get('operating_margins'):.2%}")
            
            # Dividends
            print("\n💵 DIVIDENDS:")
            if result.get('dividend_rate'):
                print(f"  Dividend Rate: ${result.get('dividend_rate'):.2f}")
            if result.get('dividend_yield') is not None:
                print(f"  Dividend Yield: {result.get('dividend_yield'):.2f}%")
            if result.get('payout_ratio') is not None:
                print(f"  Payout Ratio: {result.get('payout_ratio'):.2%}")
            
            # Moving Averages
            print("\n📊 MOVING AVERAGES:")
            if result.get('fifty_day_average'):
                print(f"  50-Day Average: ${result.get('fifty_day_average'):,.2f}")
            if result.get('two_hundred_day_average'):
                print(f"  200-Day Average: ${result.get('two_hundred_day_average'):,.2f}")
            
            # Analyst Data
            print("\n👥 ANALYST DATA:")
            if result.get('recommendation_mean') is not None:
                rec_mean = result.get('recommendation_mean')
                rec_text = {1: "Strong Buy", 2: "Buy", 3: "Hold", 4: "Underperform", 5: "Sell"}.get(int(rec_mean), f"{rec_mean:.2f}")
                print(f"  Recommendation Mean: {rec_text} ({rec_mean:.2f})")
            if result.get('number_of_analyst_opinions'):
                print(f"  Number of Analyst Opinions: {result.get('number_of_analyst_opinions')}")
            if result.get('target_high_price'):
                print(f"  Target High Price: ${result.get('target_high_price'):,.2f}")
            if result.get('target_low_price'):
                print(f"  Target Low Price: ${result.get('target_low_price'):,.2f}")
            
            # Shares & Ownership
            print("\n📋 SHARES & OWNERSHIP:")
            if result.get('shares_outstanding'):
                print(f"  Shares Outstanding: {result.get('shares_outstanding'):,}")
            if result.get('float_shares'):
                print(f"  Float Shares: {result.get('float_shares'):,}")
            if result.get('shares_short'):
                print(f"  Shares Short: {result.get('shares_short'):,}")
            if result.get('short_ratio') is not None:
                print(f"  Short Ratio: {result.get('short_ratio'):.2f}")
            if result.get('held_percent_institutions') is not None:
                print(f"  Held by Institutions: {result.get('held_percent_institutions'):.2%}")
            if result.get('held_percent_insiders') is not None:
                print(f"  Held by Insiders: {result.get('held_percent_insiders'):.2%}")
            
            # Company Info
            print("\n🏢 COMPANY INFO:")
            if result.get('full_time_employees'):
                print(f"  Full-Time Employees: {result.get('full_time_employees'):,}")
            if result.get('book_value') is not None:
                print(f"  Book Value: ${result.get('book_value'):,.2f}")
            if result.get('enterprise_value'):
                print(f"  Enterprise Value: ${result.get('enterprise_value'):,}")
            if result.get('ebitda'):
                print(f"  EBITDA: ${result.get('ebitda'):,}")
            if result.get('return_on_assets') is not None:
                print(f"  Return on Assets: {result.get('return_on_assets'):.2%}")
            if result.get('return_on_equity') is not None:
                print(f"  Return on Equity: {result.get('return_on_equity'):.2%}")
            
            # Print any additional fields that weren't covered above
            covered_fields = {
                'ticker', 'sector', 'current_price', 'previous_close', 'last_updated',
                'market_cap', 'avg_volume', 'beta', 'short_float', 'week_52_high', 'week_52_low', 'week_52_change',
                'pe_ratio', 'forward_pe', 'peg_ratio', 'price_to_book', 'price_to_sales',
                'enterprise_to_revenue', 'enterprise_to_ebitda', 'target_price',
                'debt_to_equity', 'current_ratio', 'quick_ratio', 'total_cash', 'total_debt',
                'total_revenue', 'gross_profits', 'free_cashflow', 'operating_cashflow',
                'revenue_growth', 'earnings_growth', 'earnings_quarterly_growth',
                'profit_margins', 'gross_margins', 'operating_margins',
                'dividend_rate', 'dividend_yield', 'payout_ratio',
                'fifty_day_average', 'two_hundred_day_average',
                'recommendation_mean', 'number_of_analyst_opinions', 'target_high_price', 'target_low_price',
                'shares_outstanding', 'float_shares', 'shares_short', 'short_ratio',
                'held_percent_institutions', 'held_percent_insiders',
                'full_time_employees', 'book_value', 'enterprise_value', 'ebitda',
                'return_on_assets', 'return_on_equity'
            }
            
            additional_fields = {k: v for k, v in result.items() if k not in covered_fields and v is not None}
            if additional_fields:
                print("\n📌 ADDITIONAL FIELDS:")
                for key, value in sorted(additional_fields.items()):
                    if isinstance(value, (int, float)):
                        if abs(value) >= 1000000:
                            print(f"  {key}: {value:,.0f}")
                        elif isinstance(value, float):
                            print(f"  {key}: {value:.2f}")
                        else:
                            print(f"  {key}: {value:,}")
                    else:
                        print(f"  {key}: {value}")
            
            print(f"\n{'='*80}\n")
        else:
            # Short mode - print only key stats (5-10 main metrics)
            print(f"{ticker}: ", end='')
            stats = []
            
            if result.get('current_price'):
                stats.append(f"Price: ${result.get('current_price'):,.2f}")
            if result.get('market_cap'):
                stats.append(f"Market Cap: ${result.get('market_cap'):,}")
            if result.get('pe_ratio') is not None:
                stats.append(f"P/E: {result.get('pe_ratio'):.2f}")
            if result.get('beta') is not None:
                stats.append(f"Beta: {result.get('beta'):.2f}")
            if result.get('target_price'):
                target = result.get('target_price')
                current = result.get('current_price', 0)
                if current > 0:
                    upside = ((target - current) / current) * 100
                    stats.append(f"Upside: {upside:+.1f}%")
            if result.get('recommendation_mean') is not None:
                rec_mean = result.get('recommendation_mean')
                rec_text = {1: "Strong Buy", 2: "Buy", 3: "Hold", 4: "Underperform", 5: "Sell"}.get(int(rec_mean), f"{rec_mean:.1f}")
                stats.append(f"Rec: {rec_text}")
            if result.get('revenue_growth') is not None:
                stats.append(f"Rev Growth: {result.get('revenue_growth'):.1%}")
            if result.get('fifty_day_average') and result.get('two_hundred_day_average'):
                ma50 = result.get('fifty_day_average')
                ma200 = result.get('two_hundred_day_average')
                if ma50 > ma200:
                    stats.append("MA: ↑")
                else:
                    stats.append("MA: ↓")
            
            print(" | ".join(stats))
        
        return result
    return None


def get_price_history(ticker: str, start_date: Optional[str] = None, 
                     end_date: Optional[str] = None) -> pd.DataFrame:
    """
    Get price history for a ticker from database.
    
    Note: Historical price data is no longer stored in the database.
    Only current_price is stored in the fundamentals table.
    Use yfinance or Schwab API for historical data if needed.
    """
    print(f"Note: Historical price data is not stored in database for {ticker}.")
    print("Only current_price is available in fundamentals table.")
    print("Use yfinance or Schwab API for historical data.")
    return pd.DataFrame()


# ============================================================================
# Filter Functions
# ============================================================================

def filter_stocks(criteria_dict: Dict[str, Any], limit: Optional[int] = None) -> List[str]:
    """
    Filter stocks by criteria (sector, P/E ratio, etc.).
    Returns tickers ranked by analyst upside (highest first).

    example: risky = risky_filter_stocks(min_beta=2.0, min_analyst_upside=0.30)
    
    Supported criteria:
    - sector: Filter by sector name
    - max_pe_ratio: Maximum P/E ratio
    - min_pe_ratio: Minimum P/E ratio
    - min_market_cap: Minimum market cap
    - min_avg_volume: Minimum average volume
    - min_beta: Minimum beta
    - min_short_float: Minimum short float percentage
    - min_analyst_upside: Minimum analyst upside percentage (calculated as (target_price - current_price) / current_price)
    
    Args:
        criteria_dict: Filter criteria
        limit: Max tickers to return (None = all). If fewer match, returns all matches.
    """
    conn = get_fundamentals_connection()
    cursor = conn.cursor()
    
    # Build query dynamically based on criteria
    query = "SELECT ticker FROM fundamentals WHERE 1=1"
    params = []  # type: List[Any]
    
    if 'sector' in criteria_dict:
        query += " AND sector = ?"
        params.append(criteria_dict['sector'])
    
    if 'max_pe_ratio' in criteria_dict:
        query += " AND pe_ratio < ?"
        params.append(criteria_dict['max_pe_ratio'])
    
    if 'min_pe_ratio' in criteria_dict:
        query += " AND pe_ratio > ?"
        params.append(criteria_dict['min_pe_ratio'])
    
    if 'min_market_cap' in criteria_dict:
        query += " AND market_cap > ?"
        params.append(criteria_dict['min_market_cap'])
    
    if 'min_avg_volume' in criteria_dict:
        query += " AND avg_volume > ?"
        params.append(criteria_dict['min_avg_volume'])
    
    if 'min_beta' in criteria_dict:
        query += " AND beta > ?"
        params.append(criteria_dict['min_beta'])
    
    if 'min_short_float' in criteria_dict:
        query += " AND short_float > ?"
        params.append(criteria_dict['min_short_float'])
    
    if 'min_analyst_upside' in criteria_dict:
        query += " AND current_price > 0 AND target_price > 0 AND ((target_price - current_price) / current_price) > ?"
        params.append(criteria_dict['min_analyst_upside'])
    
    query += " " + _ANALYST_UPSIDE_ORDER_BY
    if limit is not None:
        query += " LIMIT ?"
        params.append(int(limit))
    
    cursor.execute(query, params)
    results = cursor.fetchall()
    conn.close()
    
    return [row[0] for row in results]


# ============================================================================
# Predefined Stock Filters
# ============================================================================
# These filters are easily editable and sectioned for different trading strategies.
# Each filter is a self-contained function with clear criteria.
# To add a new filter, copy one of the existing filters and modify the SQL query.
#
# Available filters:
# - risky_filter_stocks(): High volatility momentum stocks with short squeeze potential
# - safe_filter_stocks(): Mega cap stable stocks with value characteristics
#
# FILTER METRIC DESCRIPTIONS (Data from Yahoo Finance via yfinance):
# All metrics are stored in SQLite database and filtered via efficient SQL queries.
# No data is loaded into memory - only matching tickers are returned.
#
# BASIC METRICS:
# - market_cap: Total market value of company (shares × price) in dollars
# - avg_volume: Average daily trading volume over recent period in shares
# - current_price: Latest stock price from Yahoo Finance in dollars
# - beta: Stock volatility relative to market (1.0 = moves with market)
# - short_float: Percentage of shares sold short as decimal (0.15 = 15%)
# - pe_ratio: Price-to-earnings ratio (price per share / earnings per share)
# - target_price: Analyst consensus target mean price in dollars
# - min_analyst_upside: Minimum percentage upside from current to target price
#
# VALUATION METRICS:
# - forward_pe: Forward P/E ratio (based on projected earnings)
# - peg_ratio: PEG ratio (P/E divided by earnings growth rate)
# - price_to_book: Price to book value ratio (market cap / book value)
# - price_to_sales: Price to sales ratio (market cap / revenue)
# - enterprise_to_revenue: Enterprise value to revenue ratio
# - enterprise_to_ebitda: Enterprise value to EBITDA ratio
#
# FINANCIAL HEALTH:
# - debt_to_equity: Total debt divided by shareholder equity
# - current_ratio: Current assets / current liabilities (liquidity measure)
# - quick_ratio: (Current assets - inventory) / current liabilities
# - total_cash: Total cash and cash equivalents in dollars
# - total_debt: Total debt outstanding in dollars
# - total_revenue: Total revenue in dollars
# - gross_profits: Gross profits in dollars
# - free_cashflow: Free cash flow in dollars (operating cash - capital expenditures)
# - operating_cashflow: Operating cash flow in dollars
#
# GROWTH METRICS:
# - revenue_growth: Year-over-year revenue growth rate as decimal (0.15 = 15%)
# - earnings_growth: Year-over-year earnings growth rate as decimal
# - earnings_quarterly_growth: Quarterly earnings growth rate as decimal
# - profit_margins: Net profit margin as decimal (0.20 = 20%)
# - gross_margins: Gross profit margin as decimal
# - operating_margins: Operating profit margin as decimal
#
# DIVIDENDS:
# - dividend_rate: Annual dividend rate per share in dollars
# - dividend_yield: Dividend yield in percentage points (e.g. 1 = 1%, 3.2 = 3.2%)
#
# MARKET DATA:
# - previous_close: Previous trading day's closing price in dollars
# - week_52_high: 52-week high price in dollars
# - week_52_low: 52-week low price in dollars
#
# COMPANY INFO:
# - full_time_employees: Number of full-time employees
#
# OWNERSHIP:
# - held_percent_institutions: Percentage of shares held by institutions as decimal (0.75 = 75%)

def risky_filter_stocks(
    min_market_cap: int = 2000000000,
    min_avg_volume: int = 1000000,
    min_price: float = 5.0,
    min_beta: float = 1.5,
    min_short_float: float = 0.15,
    min_analyst_upside: float = 0.20,
    limit: Optional[int] = None,
) -> List[str]:
    """
    RISKY FILTER: High volatility momentum stocks with short squeeze potential.
    
    Finds stocks matching these criteria:
    - Safety: Big cap, liquid, not penny stocks
    - Volatility: High beta (moves more than market)
    - Short squeeze potential: High short interest
    - Value: Analyst upside > threshold
    
    Results are ranked by analyst upside (highest first).
    
    Args:
        min_market_cap: Minimum market cap (default: $2B)
        min_avg_volume: Minimum average volume (default: 1M shares/day)
        min_price: Minimum current price (default: $5)
        min_beta: Minimum beta (default: 1.5 = 50% more volatile than market)
        min_short_float: Minimum short float percentage (default: 0.15 = 15%)
        min_analyst_upside: Minimum analyst upside percentage (default: 0.20 = 20%)
        limit: Max tickers to return (None = all). If fewer match, returns all matches.
    
    Returns:
        List of tickers matching all criteria, ranked by analyst upside
    """
    conn = get_fundamentals_connection()
    cursor = conn.cursor()
    
    # RISKY FILTER SQL Query
    # Calculates "Analyst Upside" instantly: ((Target - Price) / Price)
    query = """
    SELECT ticker 
    FROM fundamentals 
    WHERE 
        -- 1. Safety Filters (Big & Liquid)
        market_cap > ?
        AND avg_volume > ?
        AND current_price > ?
        
        -- 2. Volatility Filters (Action)
        AND beta > ?
        
        -- 3. The "Squeeze" Filter
        AND short_float > ?
        
        -- 4. The "Value" Filter (Analyst Upside > threshold)
        AND current_price > 0
        AND target_price > 0
        AND ((target_price - current_price) / current_price) > ?
    """ + _ANALYST_UPSIDE_ORDER_BY
    
    params = [
        min_market_cap,
        min_avg_volume,
        min_price,
        min_beta,
        min_short_float,
        min_analyst_upside,
    ]  # type: List[Any]
    if limit is not None:
        query += " LIMIT ?"
        params.append(int(limit))
    
    try:
        cursor.execute(query, params)
        rows = cursor.fetchall()
        
        # Clean up list (removes commas/parentheses)
        watchlist = [row[0] for row in rows]
        
        return watchlist
    except sqlite3.OperationalError as e:
        print(f"Error executing risky filter: {e}")
        print("Make sure you've run populate_database() to populate the new fields.")
        return []
    finally:
        conn.close()


def safe_filter_stocks(
    # min_market_cap: int = 50000000000,
    min_market_cap: int = 25000000000,
    # min_beta: float = 0.8,
    # max_beta: float = 1.2,
    # min_beta was 0.6 ("near market"); lowered to 0 — low beta is fine for safety/diversity;
    # keep max_beta to exclude high-volatility names.
    min_beta: float = 0.0,
    max_beta: float = 1.3,
    # max_short_float: float = 0.05,
    max_short_float: float = 0.1,
    min_pe_ratio: float = 0.0,
    max_pe_ratio: float = 35.0,
    min_peg_ratio: float = 0.0,
    max_peg_ratio: float = 2.0,
    min_analyst_upside: float = 0.10,
    max_recommendation_mean: float = 2.0,
    min_dividend_yield: float = 1.0,
    limit: Optional[int] = None,
) -> List[str]:
    """
    SAFE FILTER: Safe giant stocks with stability and value.
    
    Finds stocks matching these criteria:
    - Giant: Mega cap companies ($25B+)
    - Stability: Beta between 0.0 and 1.3 (cap volatility; low beta allowed)
    - No enemies: Low short interest (< 10%)
    - Value: Profitable with reasonable P/E ratio (0.0 to 35.0)
    - Growth at Reasonable Price: PEG ratio between 0.0 and 2.0 (PEG data required)
    - Upside: Analysts see growth potential (> 10%)
    - Analyst Recommendation: Mean recommendation <= 2.0 (Buy/Strong Buy; recommendation required)
    - Uptrend: (disabled) 50-day MA above 200-day MA — commented out as noisy
    - Dividend: Dividend yield > 1% (for longer holds)
    
    Results are ranked by analyst upside (highest first).
    
    Args:
        min_market_cap: Minimum market cap (default: $25B)
        min_beta: Minimum beta (default: 0.0; low beta OK)
        max_beta: Maximum beta (default: 1.3)
        max_short_float: Maximum short float percentage (default: 0.1 = 10%)
        min_pe_ratio: Minimum P/E ratio (default: 0.0 = must be profitable)
        max_pe_ratio: Maximum P/E ratio (default: 35.0 = not overpriced)
        min_peg_ratio: Minimum PEG ratio (default: 0.0)
        max_peg_ratio: Maximum PEG ratio (default: 2.0)
        min_analyst_upside: Minimum analyst upside percentage (default: 0.10 = 10%)
        max_recommendation_mean: Maximum analyst recommendation mean (default: 2.0, where 1=Strong Buy, 2=Buy, 3=Hold, 4=Underperform, 5=Sell)
        min_dividend_yield: Minimum dividend yield in percentage points (default: 1 = 1%)
        limit: Max tickers to return (None = all). If fewer match, returns all matches.
    
    Returns:
        List of tickers matching all criteria, ranked by analyst upside
    """
    conn = get_fundamentals_connection()
    cursor = conn.cursor()
    
    # SAFE FILTER SQL Query
    # The "Safe Giant" Filter
    query = """
    SELECT ticker 
    FROM fundamentals 
    WHERE 
        -- 1. The "Giant" Rule (Mega Cap)
        market_cap > ?
        
        -- 2. The "Stability" Rule (beta <= max; min lowered to 0 for low-beta names)
        AND beta BETWEEN ? AND ?
        
        -- 3. The "No Enemies" Rule (Low Short Interest)
        AND short_float < ?
        
        -- 4. The "Value" Rule (Profitable)
        AND pe_ratio > ?
        AND pe_ratio < ?
        
        -- 5. The "Growth at Reasonable Price" Rule (PEG Ratio; NULL not allowed)
        AND peg_ratio IS NOT NULL
        AND peg_ratio > ?
        AND peg_ratio < ?
        
        -- 6. The "Upside" Rule (Still want growth!)
        AND current_price > 0
        AND target_price > 0
        AND ((target_price - current_price) / current_price) > ?
        
        -- 7. The "Analyst Recommendation" Rule (Buy/Strong Buy; NULL not allowed)
        AND recommendation_mean IS NOT NULL
        AND recommendation_mean <= ?
        
        -- 8. The "Uptrend" Rule (50-day MA above 200-day MA) — disabled (noisy)
        -- AND fifty_day_average IS NOT NULL
        -- AND two_hundred_day_average IS NOT NULL
        -- AND fifty_day_average > two_hundred_day_average
        
        -- 9. The "Dividend" Rule (yield > 1%; data stored as percentage points e.g. 1 = 1%)
        AND dividend_yield IS NOT NULL
        AND dividend_yield > ?
    """ + _ANALYST_UPSIDE_ORDER_BY
    
    params = [
        min_market_cap,
        min_beta,
        max_beta,
        max_short_float,
        min_pe_ratio,
        max_pe_ratio,
        min_peg_ratio,
        max_peg_ratio,
        min_analyst_upside,
        max_recommendation_mean,
        min_dividend_yield,
    ]  # type: List[Any]
    if limit is not None:
        query += " LIMIT ?"
        params.append(int(limit))
    
    try:
        cursor.execute(query, params)
        rows = cursor.fetchall()
        
        # Clean up list (removes commas/parentheses)
        watchlist = [row[0] for row in rows]
        
        # Show grid of tickers and their filter-criteria values (returned / limited set)
        display_safe_filter_grid(filter_name='safe', tickers=watchlist)
        
        return watchlist
    except sqlite3.OperationalError as e:
        print(f"Error executing safe filter: {e}")
        print("Make sure you've run populate_database() to populate the new fields.")
        return []
    finally:
        conn.close()


def get_all_filter_results(limit: Optional[int] = None) -> Dict[str, List[str]]:
    """
    Convenience function to run all predefined filters and return results.
    
    Args:
        limit: Optional max tickers per filter (None = all), ranked by analyst upside.
    
    Returns:
        Dictionary with filter names as keys and ticker lists as values
    """
    return {
        'risky': risky_filter_stocks(limit=limit),
        'safe': safe_filter_stocks(limit=limit),
    }


def display_safe_filter_grid(filter_name: str = 'safe', tickers: Optional[List[str]] = None) -> None:
    """
    Display a terminal grid of filtered tickers with their filter-criteria values.
    ~16 rows (tickers) x ~8 columns (ticker + criteria).
    
    Args:
        filter_name: 'safe' or 'risky' (default: 'safe')
        tickers: Optional list of tickers to display. If None, runs the filter to get tickers.
    """
    if tickers is None:
        tickers = safe_filter_stocks() if filter_name == 'safe' else risky_filter_stocks()
    if filter_name == 'safe':
        criteria_cols = [
            'market_cap', 'beta', 'short_float', 'pe_ratio', 'peg_ratio',
            'recommendation_mean', 'dividend_yield',
            'current_price', 'target_price', 'fifty_day_average', 'two_hundred_day_average'
        ]
    else:
        criteria_cols = [
            'market_cap', 'avg_volume', 'current_price', 'beta', 'short_float',
            'current_price', 'target_price'
        ]
    
    if not tickers:
        print("No tickers match the filter.")
        return
    
    conn = get_fundamentals_connection()
    cursor = conn.cursor()
    
    # Fetch all needed columns for these tickers in one query
    cursor.execute("PRAGMA table_info(fundamentals)")
    fund_cols = [row[1] for row in cursor.fetchall()]
    
    if filter_name == 'safe':
        want_cols = ['ticker', 'market_cap', 'beta', 'short_float', 'pe_ratio', 'peg_ratio',
                     'recommendation_mean', 'dividend_yield', 'current_price', 'target_price',
                     'fifty_day_average', 'two_hundred_day_average']
    else:
        want_cols = ['ticker', 'market_cap', 'avg_volume', 'current_price', 'beta', 'short_float', 'target_price']
    
    select_cols = [c for c in want_cols if c in fund_cols]
    placeholders = ','.join(['?'] * len(tickers))
    
    query = f"SELECT {', '.join(select_cols)} FROM fundamentals WHERE ticker IN ({placeholders})"
    cursor.execute(query, tickers)
    rows = cursor.fetchall()
    conn.close()
    
    col_names = select_cols
    # Preserve caller order (filters return tickers ranked by analyst upside)
    by_ticker = {dict(zip(col_names, row)).get('ticker'): row for row in rows}
    rows = [by_ticker[t] for t in tickers if t in by_ticker]
    
    # Display columns (12): Ticker, Price, MA50, MA200, MktCap(B), Beta, Short%, P/E, PEG, Upside%, Rec, Div%
    display_headers = ['Ticker', 'Price', 'MA50', 'MA200', 'MktCap(B)', 'Beta', 'Short%', 'P/E', 'PEG', 'Upside%', 'Rec', 'Div%']
    # Only include columns we have
    if filter_name != 'safe':
        display_headers = ['Ticker', 'MktCap(B)', 'AvgVol', 'Beta', 'Short%', 'Price', 'Target', 'Upside%']
    
    def fmt_val(v, col):
        if v is None: return '—'
        if col == 'market_cap':
            return f"{float(v)/1e9:.1f}" if v else '—'
        if col == 'beta': return f"{float(v):.2f}"
        if col == 'short_float': return f"{float(v)*100:.1f}" if v is not None else '—'
        if col == 'pe_ratio': return f"{float(v):.1f}" if v is not None else '—'
        if col == 'peg_ratio': return f"{float(v):.2f}" if v is not None else '—'
        if col == 'recommendation_mean': return f"{float(v):.1f}" if v is not None else '—'
        if col == 'dividend_yield': return f"{float(v):.1f}" if v is not None else '—'
        if col == 'current_price': return f"{float(v):.1f}" if v is not None else '—'
        if col == 'target_price': return f"{float(v):.1f}" if v is not None else '—'
        if col == 'fifty_day_average': return f"{float(v):.1f}" if v is not None else '—'
        if col == 'two_hundred_day_average': return f"{float(v):.1f}" if v is not None else '—'
        return str(v)[:8]
    
    # Build display rows
    data_rows = []
    for row in rows:
        d = dict(zip(col_names, row))
        price = d.get('current_price') or 0
        target = d.get('target_price') or 0
        upside = ((target - price) / price * 100) if price and target else None
        
        if filter_name == 'safe':
            data_rows.append([
                d.get('ticker', '')[:6],
                fmt_val(d.get('current_price'), 'current_price'),
                fmt_val(d.get('fifty_day_average'), 'fifty_day_average'),
                fmt_val(d.get('two_hundred_day_average'), 'two_hundred_day_average'),
                fmt_val(d.get('market_cap'), 'market_cap'),
                fmt_val(d.get('beta'), 'beta'),
                fmt_val(d.get('short_float'), 'short_float') if d.get('short_float') is not None else '—',
                fmt_val(d.get('pe_ratio'), 'pe_ratio'),
                fmt_val(d.get('peg_ratio'), 'peg_ratio'),
                f"{upside:.1f}" if upside is not None else '—',
                fmt_val(d.get('recommendation_mean'), 'recommendation_mean'),
                fmt_val(d.get('dividend_yield'), 'dividend_yield')
            ])
        else:
            data_rows.append([
                d.get('ticker', '')[:6],
                fmt_val(d.get('market_cap'), 'market_cap'),
                str(d.get('avg_volume', ''))[:8] if d.get('avg_volume') else '—',
                fmt_val(d.get('beta'), 'beta'),
                fmt_val(d.get('short_float'), 'short_float') if d.get('short_float') is not None else '—',
                fmt_val(d.get('current_price'), 'current_price'),
                fmt_val(d.get('target_price'), 'target_price'),
                f"{upside:.1f}" if upside is not None else '—'
            ])
    
    # Column widths (header + content)
    widths = [max(len(h), 6) for h in display_headers]
    for r in data_rows:
        for i, c in enumerate(r):
            if i < len(widths):
                widths[i] = max(widths[i], len(str(c)))
    
    # Pad to same length
    for i in range(len(widths), len(display_headers)):
        widths.append(len(display_headers[i]))
    widths = widths[:len(display_headers)]
    
    def row_str(items, w):
        return ' | '.join(str(x).ljust(w[i]) for i, x in enumerate(items) if i < len(w))
    
    sep = '-+-'.join('-' * w for w in widths)
    print()
    print(row_str(display_headers, widths))
    print(sep)
    for r in data_rows:
        print(row_str(r, widths))
    print(f"\n({len(data_rows)} tickers)")
    print()


def display_positions_table() -> None:
    """
    Display the positions table in the terminal as a grid, with (X positions) below.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(positions)")
    pos_cols = [row[1] for row in cursor.fetchall()]
    have_pct = 'day_pct' in pos_cols and 'open_pct' in pos_cols
    if have_pct:
        cursor.execute("""
            SELECT ticker, date_purchased, shares_owned, average_price, market_value,
                   current_day_profit_loss, day_pct, long_open_profit_loss, open_pct
            FROM positions
            WHERE shares_owned > 0
            ORDER BY open_pct IS NULL, open_pct DESC
        """)
    else:
        cursor.execute("""
            SELECT ticker, date_purchased, shares_owned, average_price, market_value,
                   current_day_profit_loss, long_open_profit_loss
            FROM positions
            WHERE shares_owned > 0
            ORDER BY ticker
        """)
    rows = cursor.fetchall()
    conn.close()
    
    display_headers = ['Ticker', 'Date', 'Shares', 'AvgPrice', 'CostBasis', 'MarketVal', 'Day P/L', 'Day %', 'Open P/L', 'Open %']
    
    def fmt(v):
        if v is None: return '—'
        if isinstance(v, float):
            return f"{v:,.2f}" if abs(v) >= 0.01 or v == 0 else f"{v:.4f}"
        return str(v)
    
    def fmt_pct(v):
        if v is None: return '—'
        if isinstance(v, float):
            return f"{v:+.2f}%"
        return str(v)
    
    data_rows = []
    total_cost_basis = 0.0
    total_market_value = 0.0
    total_day_pl = 0.0
    total_open_pl = 0.0
    for row in rows:
        if have_pct:
            ticker, date_purchased, shares_owned, average_price, market_value, day_pl, day_pct, open_pl, open_pct = row
        else:
            ticker, date_purchased, shares_owned, average_price, market_value, day_pl, open_pl = row
            day_pct, open_pct = None, None
        cost_basis = (float(shares_owned or 0) * float(average_price or 0)) if (shares_owned is not None and average_price is not None) else None
        if cost_basis is not None:
            total_cost_basis += cost_basis
        if market_value is not None:
            total_market_value += float(market_value)
        if day_pl is not None:
            total_day_pl += float(day_pl)
        if open_pl is not None:
            total_open_pl += float(open_pl)
        date_str = (date_purchased[:10] if date_purchased and len(date_purchased) >= 10 else date_purchased) or '—'
        data_rows.append([
            (ticker or '')[:8],
            date_str,
            str(int(shares_owned)) if shares_owned is not None else '—',
            fmt(average_price),
            fmt(cost_basis),
            fmt(market_value),
            fmt(day_pl),
            fmt_pct(day_pct),
            fmt(open_pl),
            fmt_pct(open_pct)
        ])
    
    if not data_rows:
        print("\nPositions (0)")
        print("No positions.")
        return
    
    widths = [max(len(h), 6) for h in display_headers]
    for r in data_rows:
        for i, c in enumerate(r):
            if i < len(widths):
                widths[i] = max(widths[i], len(str(c)))
    # Portfolio-level percentages from totals (evenly weighted by dollar amounts):
    # Open % = (total market value - total cost basis) / total cost basis
    # Day % = total Day P/L / start-of-day value, where start-of-day value = total market value - total Day P/L
    open_pct_total = ((total_market_value - total_cost_basis) / total_cost_basis * 100) if total_cost_basis and abs(total_cost_basis) > 1e-9 else None
    sod_value = total_market_value - total_day_pl
    day_pct_total = (total_day_pl / sod_value * 100) if sod_value and abs(sod_value) > 1e-9 else None
    # Totals row: first 4 cols (TOTAL + blank), then 6 rightmost
    totals_row = ['TOTAL', '', '', '', fmt(total_cost_basis), fmt(total_market_value), fmt(total_day_pl), fmt_pct(day_pct_total), fmt(total_open_pl), fmt_pct(open_pct_total)]
    for i, c in enumerate(totals_row):
        if i < len(widths):
            widths[i] = max(widths[i], len(str(c)))
    widths = widths[:len(display_headers)]
    
    def row_str(items, w):
        return ' | '.join(str(x).ljust(w[i]) for i, x in enumerate(items) if i < len(w))
    
    sep = '-+-'.join('-' * w for w in widths)
    print()
    print(row_str(display_headers, widths))
    print(sep)
    for r in data_rows:
        print(row_str(r, widths))
    print(sep)
    print(row_str(totals_row, widths))
    print(f"\n({len(data_rows)} positions)")
    print()


# ============================================================================
# Watchlist Management Functions
# ============================================================================

def update_watchlist(filter_name: str = 'safe') -> Dict[str, int]:
    """
    Update watchlist based on filter results.
    Fetches fundamentals + Schwab quotes with no trading-DB lock held, then
    applies all watchlist writes in one short transaction.
    
    Args:
        filter_name: Name of filter to use ('safe' or 'risky', default: 'safe')
    
    Returns:
        Dictionary with counts: {'added': int, 'removed': int, 'updated': int, 'kept_with_shares': int}
    """
    init_database()

    # Phase 1 — short read of current watchlist / positions (release before network)
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT ticker FROM watchlist')
        current_tickers_set = {row[0] for row in cursor.fetchall()}
        cursor.execute('SELECT ticker FROM positions WHERE shares_owned > 0')
        owned_tickers_set = {row[0] for row in cursor.fetchall()}
        cursor.execute('PRAGMA table_info(watchlist)')
        watchlist_columns = [
            row[1] for row in cursor.fetchall()
            if row[1] not in ('ticker', 'user_id')
        ]
        date_added_map = {}  # type: Dict[str, Any]
        if 'date_added' in watchlist_columns:
            cursor.execute('SELECT ticker, date_added FROM watchlist')
            date_added_map = {row[0]: row[1] for row in cursor.fetchall() if row[1]}
    finally:
        conn.close()

    filter_name = (filter_name or 'safe').strip()
    if filter_name == 'safe':
        filter_results = safe_filter_stocks()
    elif filter_name == 'risky':
        filter_results = risky_filter_stocks()
    else:
        import filter_builder as fb
        custom = fb.get_user_custom_filter(_uid())
        if not custom:
            raise ValueError(
                "Unknown filter %r — use 'safe', 'risky', or save a custom filter."
                % filter_name
            )
        if (custom.get('name') or '').strip().lower() != filter_name.lower():
            raise ValueError(
                "Unknown filter %r — your custom filter is named %r."
                % (filter_name, custom.get('name'))
            )
        filter_results = fb.list_custom_tickers(custom.get('criteria') or [])

    filter_tickers_set = set(filter_results)
    stats = {'added': 0, 'removed': 0, 'updated': 0, 'kept_with_shares': 0}
    current_timestamp = datetime.now().isoformat()

    fconn = get_fundamentals_connection()
    try:
        fundamentals_columns = [
            row[1] for row in fconn.execute('PRAGMA table_info(fundamentals)')
            if row[1] != 'ticker'
        ]
    finally:
        fconn.close()

    schwab_mapping = {
        'price': 'schwab_price', 'bid': 'schwab_bid', 'ask': 'schwab_ask', 'mark': 'schwab_mark',
        'open': 'schwab_open', 'high': 'schwab_high', 'low': 'schwab_low',
        'previous_close': 'schwab_previous_close',
        'net_change': 'schwab_net_change', 'net_percent_change': 'schwab_net_percent_change',
        'mark_change': 'schwab_mark_change', 'mark_percent_change': 'schwab_mark_percent_change',
        'post_market_change': 'schwab_post_market_change',
        'post_market_percent_change': 'schwab_post_market_percent_change',
        'volume': 'schwab_volume', 'bid_size': 'schwab_bid_size', 'ask_size': 'schwab_ask_size',
        'exchange': 'schwab_exchange', 'quote_time': 'schwab_quote_time',
        'trade_time': 'schwab_trade_time', 'market_status': 'schwab_market_status',
        'realtime': 'schwab_realtime',
        'week_52_high': 'schwab_week_52_high', 'week_52_low': 'schwab_week_52_low',
        'extended_last_price': 'schwab_extended_last_price',
        'extended_volume': 'schwab_extended_volume', 'extended_bid': 'schwab_extended_bid',
        'extended_ask': 'schwab_extended_ask',
        'pe_ratio': 'schwab_pe_ratio', 'dividend_yield': 'schwab_dividend_yield',
        'eps': 'schwab_eps', 'shares_outstanding': 'schwab_shares_outstanding',
        'avg_10_day_volume': 'schwab_avg_10_day_volume',
        'avg_1_year_volume': 'schwab_avg_1_year_volume',
        'div_amount': 'schwab_div_amount', 'div_freq': 'schwab_div_freq',
        'div_pay_amount': 'schwab_div_pay_amount',
        'next_div_ex_date': 'schwab_next_div_ex_date',
        'next_div_pay_date': 'schwab_next_div_pay_date',
        'cusip': 'schwab_cusip', 'description': 'schwab_description',
        'is_shortable': 'schwab_is_shortable', 'is_hard_to_borrow': 'schwab_is_hard_to_borrow',
        'asset_main_type': 'schwab_asset_main_type',
        'asset_sub_type': 'schwab_asset_sub_type', 'quote_type': 'schwab_quote_type',
        'timestamp': 'schwab_timestamp',
    }

    # Phase 2 — network / fundamentals (no trading-DB connection open)
    pending = []  # type: List[Tuple[str, Dict[str, Any], bool]]
    for ticker in filter_results:
        fundamentals = get_fundamentals(ticker)
        if not fundamentals:
            print(
                'Warning: No fundamentals data for %s, skipping watchlist update'
                % ticker
            )
            continue
        schwab_data = get_streaming_data(ticker)
        if not schwab_data or 'error' in schwab_data:
            print(
                'Warning: Could not get Schwab data for %s, continuing with fundamentals only'
                % ticker
            )
            schwab_data = {}
        values_dict = {}  # type: Dict[str, Any]
        for col in fundamentals_columns:
            values_dict[col] = fundamentals.get(col)
        for schwab_key, db_key in schwab_mapping.items():
            values_dict[db_key] = schwab_data.get(schwab_key)
        is_update = ticker in current_tickers_set
        if is_update:
            if ticker in date_added_map:
                values_dict['date_added'] = date_added_map[ticker]
            values_dict['last_updated'] = current_timestamp
        else:
            values_dict['date_added'] = current_timestamp
            values_dict['last_updated'] = current_timestamp
        pending.append((ticker, values_dict, is_update))

    to_remove = [
        t for t in (current_tickers_set - filter_tickers_set)
        if t not in owned_tickers_set
    ]
    kept = [
        t for t in (current_tickers_set - filter_tickers_set)
        if t in owned_tickers_set
    ]
    stats['kept_with_shares'] = len(kept)

    # Phase 3 — short write transaction
    conn = get_connection()
    try:
        cursor = conn.cursor()
        for ticker, values_dict, is_update in pending:
            if is_update:
                update_cols = [c for c in values_dict.keys() if c in watchlist_columns]
                if update_cols:
                    set_clause = ', '.join(['%s = ?' % col for col in update_cols])
                    update_values = [values_dict[col] for col in update_cols]
                    cursor.execute(
                        'UPDATE watchlist SET %s WHERE ticker = ?' % set_clause,
                        update_values + [ticker],
                    )
                stats['updated'] += 1
            else:
                insert_cols = ['ticker'] + watchlist_columns
                placeholders = ', '.join(['?'] * len(insert_cols))
                insert_values = [ticker] + [
                    values_dict.get(col, None) for col in watchlist_columns
                ]
                cursor.execute(
                    'INSERT INTO watchlist (%s) VALUES (%s)'
                    % (', '.join(insert_cols), placeholders),
                    insert_values,
                )
                stats['added'] += 1
        for ticker in to_remove:
            cursor.execute('DELETE FROM watchlist WHERE ticker = ?', (ticker,))
            stats['removed'] += 1
        conn.commit()
    finally:
        conn.close()

    return stats


def get_watchlist(only_owned: bool = False) -> List[Dict[str, Any]]:
    """
    Get all stocks in watchlist.
    
    Args:
        only_owned: If True, return only watchlist rows for tickers we own (positions.shares_owned > 0)
    
    Returns:
        List of dictionaries with all watchlist fields
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    if only_owned:
        cursor.execute("""
            SELECT w.*, p.shares_owned FROM watchlist w
            INNER JOIN positions p ON w.ticker = p.ticker AND p.shares_owned > 0
        """)
    else:
        cursor.execute("SELECT * FROM watchlist")
    
    columns = [description[0] for description in cursor.description]
    results = [dict(zip(columns, row)) for row in cursor.fetchall()]
    conn.close()
    return results


def update_shares_owned(ticker: str, shares_change: int):
    """
    Update shares owned for a ticker in the positions table (called after successful buy/sell).
    
    Args:
        ticker: Stock ticker
        shares_change: Positive for buy, negative for sell
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT shares_owned FROM positions WHERE ticker = ?", (ticker,))
    result = cursor.fetchone()
    current_shares = (result[0] or 0) if result else 0
    new_shares = current_shares + shares_change
    
    if new_shares < 0:
        print(f"Warning: Cannot have negative shares. Current: {current_shares}, Change: {shares_change}")
        conn.close()
        return
    
    if new_shares == 0:
        cursor.execute("DELETE FROM positions WHERE ticker = ?", (ticker,))
    elif result:
        cursor.execute("UPDATE positions SET shares_owned = ? WHERE ticker = ?", (new_shares, ticker))
    else:
        # New position (first buy)
        cursor.execute(
            "INSERT INTO positions (ticker, date_purchased, shares_owned) VALUES (?, ?, ?)",
            (ticker, datetime.now().strftime('%Y-%m-%d'), new_shares)
        )
    
    conn.commit()
    conn.close()


def get_price_change(ticker: str) -> Optional[Dict[str, Any]]:
    """
    Calculate percentage change from purchase price.
    
    Args:
        ticker: Stock ticker
    
    Returns:
        Dictionary with ticker, purchase_price, current_price, change_percent
        Returns None if stock not purchased
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT average_price, shares_owned FROM positions WHERE ticker = ?", (ticker,))
    result = cursor.fetchone()
    
    if not result or result[1] == 0:
        conn.close()
        return None
    
    # Use average_price from Schwab sync; if not yet synced, return None
    purchase_price = result[0]
    conn.close()
    if purchase_price is None:
        return None

    # Live Schwab only (same rule as trade decisions)
    current_price = get_trade_price(ticker)
    if not current_price:
        return None

    change_percent = ((current_price - purchase_price) / purchase_price) * 100
    return {
        'ticker': ticker,
        'purchase_price': purchase_price,
        'current_price': current_price,
        'change_percent': change_percent
    }


# ============================================================================
# Trading Criteria Functions (Placeholders)
# ============================================================================

def check_buy_criteria(ticker: str, streaming_data: Dict[str, Any], 
                      account_data: Dict[str, Any]) -> bool:
    """
    Returns True if buy conditions are met.
    Uses real-time streaming data from Schwab for selected stocks.
    
    This is a placeholder - customize with your own criteria.
    """
    # Example placeholder criteria:
    # - Check if price is below a threshold
    # - Check if account has enough cash
    # - Check time-based conditions
    
    if streaming_data and 'price' in streaming_data:
        price = streaming_data['price']
        # Placeholder: buy if price < 150
        if price < 150:
            if account_data and account_data.get('cash', 0) > 1000:
                return True
    
    return False


def check_sell_criteria(ticker: str, streaming_data: Dict[str, Any],
                        account_data: Dict[str, Any]) -> bool:
    """
    Returns True if sell conditions are met.
    Uses real-time streaming data from Schwab for selected stocks.
    
    This is a placeholder - customize with your own criteria.
    """
    # Example placeholder criteria:
    # - Check if price is above a threshold
    # - Check if we own the stock
    # - Check time-based conditions
    
    if streaming_data and 'price' in streaming_data:
        price = streaming_data['price']
        # Placeholder: sell if price > 200
        if price > 200:
            # Check if we own this stock
            if account_data and account_data.get('positions', {}).get(ticker, 0) > 0:
                return True
    
    return False


# ============================================================================
# Trading Functions
# ============================================================================

def _placeholder_account_info(reason: str = '') -> Dict[str, Any]:
    """
    Unusable balances when Schwab is unavailable.

    Intentionally null — never invent $10,000 (or any default) for UI / snapshots.
    """
    if reason:
        print('Warning: %s Returning unavailable account info (no fabricated balances).' % reason)
    return {
        'ok': False,
        'source': 'placeholder',
        'cash': None,
        'total_value': None,
        'liquidation_value': None,
        'round_trips': 0,
        'positions': {},
    }


def _unavailable_account_info(reason: str = '') -> Dict[str, Any]:
    """Unusable balances when the API responded but account data is missing."""
    if reason:
        print('Warning: %s' % reason)
    return {
        'ok': False,
        'source': 'unavailable',
        'cash': None,
        'total_value': None,
        'liquidation_value': None,
        'round_trips': 0,
        'positions': {},
    }


def _is_fabricated_balance_triplet(
    cash: Any,
    liquidation_value: Any,
    total_value: Any,
) -> bool:
    """True for the legacy offline sentinel cash=liq=total=10000."""
    try:
        c = float(cash)
        liq = float(liquidation_value)
        tot = float(total_value)
    except (TypeError, ValueError):
        return False
    return (
        abs(c - 10000.0) < 0.01
        and abs(liq - 10000.0) < 0.01
        and abs(tot - 10000.0) < 0.01
    )


def account_info_usable(account: Optional[Dict[str, Any]]) -> bool:
    """True when account balances look real enough to snapshot / chart."""
    if not account:
        return False
    if account.get('ok') is False:
        return False
    if str(account.get('source') or '') in ('placeholder', 'unavailable'):
        return False
    if _is_fabricated_balance_triplet(
        account.get('cash'),
        account.get('liquidation_value'),
        account.get('total_value'),
    ):
        return False
    try:
        liq = float(account.get('liquidation_value') or 0.0)
        total = float(account.get('total_value') or 0.0)
        cash = float(account.get('cash') or 0.0)
    except (TypeError, ValueError):
        return False
    equity = liq if liq > 0 else total
    # Equity is preferred; cash-only payloads still count when Schwab marked ok.
    return equity > 0 or cash > 0


def get_latest_account_snapshot(
    exclude_fabricated: bool = True,
) -> Optional[Dict[str, Any]]:
    """Most recent persisted account snapshot for the current user (or None)."""
    try:
        if not _DATABASE_READY:
            if not mark_database_ready_if_present():
                init_database()
    except Exception:
        pass
    uid = _uid()
    try:
        conn = get_connection(timeout=2.0, busy_timeout_ms=2000)
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT ts, cash, effective_cash, liquidation_value, total_value, note
            FROM account_snapshots
            WHERE user_id = ?
              AND (COALESCE(liquidation_value, 0) > 0 OR COALESCE(total_value, 0) > 0)
            ORDER BY id DESC
            LIMIT 40
            ''',
            (uid,),
        )
        rows = cursor.fetchall()
        conn.close()
    except Exception:
        return None
    for ts, cash, effective, liq, total, note in rows:
        if exclude_fabricated and _is_fabricated_balance_triplet(cash, liq, total):
            continue
        return {
            'ts': ts,
            'cash': float(cash) if cash is not None else None,
            'effective_cash': float(effective) if effective is not None else None,
            'liquidation_value': float(liq) if liq is not None else None,
            'total_value': float(total) if total is not None else None,
            'note': note,
        }
    return None


def get_account_info() -> Dict[str, Any]:
    """Fetch account balance/info from Schwab API."""
    if not SCHWAB_AVAILABLE or SCHWAB_CLIENT is None:
        return _placeholder_account_info('Schwab API not available.')
    
    try:
        # Get linked accounts first
        linked_accounts_response = SCHWAB_CLIENT.linked_accounts()
        linked_accounts = linked_accounts_response.json()
        
        if config.DEBUG:
            print(f"\nDEBUG: Linked accounts response type: {type(linked_accounts)}")
            if isinstance(linked_accounts, list):
                print(f"DEBUG: Number of linked accounts: {len(linked_accounts)}")
                if len(linked_accounts) > 0:
                    print(f"DEBUG: First account keys: {list(linked_accounts[0].keys())}")
            elif isinstance(linked_accounts, dict):
                print(f"DEBUG: Linked accounts dict keys: {list(linked_accounts.keys())}")
        
        if not linked_accounts:
            return _unavailable_account_info('No linked accounts found.')
        
        # Handle different response formats
        if isinstance(linked_accounts, dict):
            # If it's a dict, might have accounts as a list inside
            if 'accounts' in linked_accounts:
                linked_accounts = linked_accounts['accounts']
            elif 'linkedAccounts' in linked_accounts:
                linked_accounts = linked_accounts['linkedAccounts']
        
        # Get account hash for first account
        if isinstance(linked_accounts, list) and len(linked_accounts) > 0:
            account_hash = linked_accounts[0].get('hashValue') or linked_accounts[0].get('accountHash') or linked_accounts[0].get('accountNumber')
        else:
            return _unavailable_account_info(
                'Unexpected linked accounts format: %s' % type(linked_accounts)
            )
        
        if not account_hash:
            print(
                'Warning: Could not find account hash. First account data: %s'
                % (linked_accounts[0] if isinstance(linked_accounts, list) else linked_accounts)
            )
            return _unavailable_account_info('Could not find account hash.')
        
        if config.DEBUG:
            print(f"DEBUG: Using account hash: {account_hash}")
        
        # Get account details with positions
        account_response = SCHWAB_CLIENT.account_details(account_hash, fields="positions")
        account_data = account_response.json()
        
        # Extract securitiesAccount (the actual account data is nested here)
        securities_account = account_data.get('securitiesAccount', {})
        if not securities_account:
            return _unavailable_account_info('No securitiesAccount found in response')
        
        # Extract currentBalances (current account balances).
        # Cash is from the first linked account (account_hash). Which field is used is set in config.CASH_BALANCE_FIELD.
        current_balances = securities_account.get('currentBalances', {})
        # Use configured field for cash; fallback to same order if that field is missing or zero
        cash_field = getattr(config, 'CASH_BALANCE_FIELD', 'availableFunds')
        cash = current_balances.get(cash_field, 0.0)
        if cash == 0.0:
            cash = current_balances.get('availableFunds', 0.0)
        if cash == 0.0:
            cash = current_balances.get('cashBalance', 0.0)
        if cash == 0.0:
            cash = current_balances.get('cashAvailableForTrading', 0.0)
        
        # Extract total value from currentBalances
        # Prefer equity (total account equity) or liquidationValue
        total_value = current_balances.get('equity', 0.0)
        if total_value == 0.0:
            total_value = current_balances.get('liquidationValue', 0.0)
        if total_value == 0.0:
            # Fallback to initialBalances if currentBalances doesn't have it
            initial_balances = securities_account.get('initialBalances', {})
            total_value = initial_balances.get('equity', 0.0) or initial_balances.get('accountValue', 0.0)
        
        # Extract liquidationValue separately
        liquidation_value = current_balances.get('liquidationValue', 0.0)
        if liquidation_value == 0.0:
            initial_balances = securities_account.get('initialBalances', {})
            liquidation_value = initial_balances.get('liquidationValue', 0.0)
        
        # Extract roundTrips (day trade count in 5-day rolling window)
        round_trips = securities_account.get('roundTrips', 0)
        
        # Extract positions (store as int so display shows 57 not 57.0)
        positions = {}
        positions_data = securities_account.get('positions', [])
        for pos in positions_data:
            instrument = pos.get('instrument', {})
            symbol = instrument.get('symbol', 'UNKNOWN')
            quantity = pos.get('longQuantity', 0) - pos.get('shortQuantity', 0)
            if quantity != 0:
                positions[symbol] = int(quantity)

        result = {
            'ok': True,
            'source': 'schwab',
            'cash': float(cash) if cash else 0.0,
            'total_value': float(total_value) if total_value else 0.0,
            'liquidation_value': float(liquidation_value) if liquidation_value else 0.0,
            'round_trips': int(round_trips) if round_trips is not None else 0,
            'positions': positions,
        }
        if not account_info_usable(result):
            return _unavailable_account_info(
                'Schwab returned zero equity/cash balances (token or account read failed?).'
            )
        return result
        
    except Exception as e:
        return _placeholder_account_info('Error fetching account info from Schwab: %s.' % e)


def get_pending_orders_total_dollars() -> float:
    """Return SUM(order_amount_dollars) from pending_orders (unfilled buy orders). Used for effective cash."""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COALESCE(SUM(order_amount_dollars), 0) FROM pending_orders")
        row = cursor.fetchone()
        conn.close()
        return float(row[0]) if row and row[0] is not None else 0.0
    except sqlite3.OperationalError:
        # pending_orders table may not exist yet (old DB or init_database not run)
        return 0.0


def trade_dry_run_enabled() -> bool:
    """True = log what we would do; do not submit orders to Schwab."""
    try:
        return bool(uc.get_user_settings(_uid()).get('trade_dry_run', True))
    except Exception:
        return bool(getattr(config, 'TRADE_DRY_RUN', True))


def minimum_cash() -> float:
    """Cash floor — per-user setting, falling back to config.MINIMUM_CASH."""
    try:
        val = uc.get_user_settings(_uid()).get('minimum_cash')
        if val is not None:
            return float(val)
    except Exception:
        pass
    return float(config.MINIMUM_CASH)


def minimum_liquidation_value() -> float:
    """Account-value floor — per-user setting, falling back to config."""
    try:
        val = uc.get_user_settings(_uid()).get('minimum_liquidation_value')
        if val is not None:
            return float(val)
    except Exception:
        pass
    return float(getattr(config, 'MINIMUM_LIQUIDATION_VALUE', 25000.0))


def active_watchlist_filter() -> str:
    """One active filter per user (not switched from the webpage)."""
    try:
        name = uc.get_user_settings(_uid()).get('active_filter') or 'safe'
        return str(name)
    except Exception:
        return str(getattr(config, 'WATCHLIST_FILTER_NAME', 'safe') or 'safe')


def order_amount_dollars() -> float:
    try:
        val = uc.get_user_settings(_uid()).get('order_amount_dollars')
        if val is not None:
            return float(val)
    except Exception:
        pass
    return float(getattr(config, 'ORDER_AMOUNT_DOLLARS', 1000.0))


def schwab_order_submit_allowed() -> bool:
    """True only when TRADE_DRY_RUN is False — place/cancel/replace on Schwab."""
    return not trade_dry_run_enabled()


def is_trading_allowed(
    account_data: Optional[Dict[str, Any]] = None,
    trade_amount_dollars: float = 0.0,
    extra_reserved_dollars: float = 0.0,
) -> Tuple[bool, str]:
    """
    Buy-safety check. Cash and account-value floors block buys only; sells skip this.
    Both liquidation value and cash floor must pass. Cash check: we must not drop below
    minimum_cash() after the buy, so effective_cash - trade_amount_dollars must be >= floor.

    Args:
        account_data: Optional account data dict. If None, will fetch it automatically.
        trade_amount_dollars: Order amount so we block if the buy would take effective cash
            below the cash floor. Use 0 for "can I buy at all?".
        extra_reserved_dollars: Additional dollars already committed this pass (proposed buys not
            yet in pending_orders). Counted like pending against the cash floor.

    Returns:
        Tuple of (is_allowed: bool, reason: str)
    """
    # Fetch account data if not provided
    if account_data is None:
        account_data = get_account_info()
    
    if not account_data:
        return False, "Could not fetch account information"
    if not account_info_usable(account_data):
        return False, "Schwab account balances unavailable — reconnect or wait for sync; trading blocked."

    try:
        liquidation_value = float(account_data.get('liquidation_value') or 0.0)
    except (TypeError, ValueError):
        liquidation_value = 0.0
    min_liquidation = minimum_liquidation_value()
    if liquidation_value < min_liquidation:
        return False, f"Liquidation value (${liquidation_value:,.2f}) is below minimum (${min_liquidation:,.2f}); buy blocked."
    
    # Effective cash = Schwab cash minus pending (unfilled) buy orders (+ this-pass reserves).
    # Must not drop below cash floor after this trade.
    try:
        cash = float(account_data.get('cash') or 0.0)
    except (TypeError, ValueError):
        cash = 0.0
    pending_total = get_pending_orders_total_dollars() + float(extra_reserved_dollars or 0.0)
    effective_cash = cash - pending_total
    min_cash = minimum_cash()
    cash_after_trade = effective_cash - trade_amount_dollars
    if cash_after_trade < min_cash:
        if trade_amount_dollars > 0:
            return False, f"This buy (${trade_amount_dollars:,.2f}) would leave effective cash at ${cash_after_trade:,.2f}, below minimum (${min_cash:,.2f}); buy blocked."
        return False, f"Effective cash (${effective_cash:,.2f} = ${cash:,.2f} - ${pending_total:,.2f} pending) is below minimum (${min_cash:,.2f}); buy blocked."
    
    # All safety checks passed
    if pending_total > 0:
        return True, f"Trading allowed. Liquidation: ${liquidation_value:,.2f}, Cash: ${cash:,.2f}, Pending: ${pending_total:,.2f}, Effective cash: ${effective_cash:,.2f}"
    return True, f"Trading allowed. Liquidation: ${liquidation_value:,.2f}, Cash: ${cash:,.2f}"


def get_schwab_positions_raw(account_hash: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
    """
    Fetch raw positions list from Schwab API (for inspection/testing).
    Returns the list of position objects as returned by the API, or None on error.
    """
    if not SCHWAB_AVAILABLE or SCHWAB_CLIENT is None:
        print("Warning: Schwab API not available.")
        return None
    try:
        if not account_hash:
            linked_accounts_response = SCHWAB_CLIENT.linked_accounts()
            linked_accounts = linked_accounts_response.json()
            if not linked_accounts or (isinstance(linked_accounts, list) and len(linked_accounts) == 0):
                print("Error: No linked accounts found.")
                return None
            if isinstance(linked_accounts, list):
                account_hash = linked_accounts[0].get('hashValue')
            else:
                account_hash = linked_accounts.get('hashValue')
            if not account_hash:
                print("Error: Could not get account hash.")
                return None
        resp = SCHWAB_CLIENT.account_details(account_hash, fields="positions")
        if resp.status_code != 200:
            print(f"Error fetching positions: Status {resp.status_code}")
            return None
        account_data = resp.json()
        securities_account = account_data.get('securitiesAccount', {})
        return securities_account.get('positions', []) or None
    except Exception as e:
        print(f"Error getting raw positions: {e}")
        return None


def fetch_and_sync_schwab_positions(account_hash: Optional[str] = None) -> int:
    """
    Fetch positions from Schwab API and upsert into the positions table.
    Updates shares_owned (longQuantity), average_price, market_value,
    current_day_profit_loss, day_pct, long_open_profit_loss, open_pct.
    
    Args:
        account_hash: Optional account hash. If None, uses first linked account.
    
    Returns:
        Number of positions synced. -1 on hard failure; -2 if deferred (DB locked).
    """
    if not SCHWAB_AVAILABLE or SCHWAB_CLIENT is None:
        print("Warning: Schwab API not available. Cannot sync positions.")
        return 0

    # Fetch Schwab once, then retry only the DB write if the trader loop holds a lock.
    try:
        if not account_hash:
            linked_accounts_response = SCHWAB_CLIENT.linked_accounts()
            linked_accounts = linked_accounts_response.json()
            if not linked_accounts or (isinstance(linked_accounts, list) and len(linked_accounts) == 0):
                print("Error: No linked accounts found.")
                return 0
            if isinstance(linked_accounts, list):
                account_hash = linked_accounts[0].get('hashValue')
            else:
                account_hash = linked_accounts.get('hashValue')
            if not account_hash:
                print("Error: Could not get account hash.")
                return 0

        resp = SCHWAB_CLIENT.account_details(account_hash, fields="positions")
        if resp.status_code != 200:
            print(f"Error fetching positions: Status {resp.status_code}")
            return 0

        account_data = resp.json()
        securities_account = account_data.get('securitiesAccount', {})
        positions_data = securities_account.get('positions', [])
    except Exception as e:
        print(f"Error syncing positions from Schwab: {e}")
        return -1

    for attempt in range(5):
        try:
            return _apply_schwab_positions_to_db(positions_data, account_hash)
        except sqlite3.OperationalError as e:
            if not _db_is_locked(e):
                print(f"Error syncing positions from Schwab: {e}")
                import traceback
                traceback.print_exc()
                return -1
            time.sleep(0.2 * (attempt + 1))
        except Exception as e:
            print(f"Error syncing positions from Schwab: {e}")
            import traceback
            traceback.print_exc()
            return -1
    print(
        'Warning: position sync deferred — database is locked '
        '(trader loop busy; will retry on next refresh)'
    )
    return -2


def _apply_schwab_positions_to_db(
    positions_data: List[Dict[str, Any]],
    account_hash: Optional[str],
) -> int:
    """Write a Schwab positions payload into the local positions table."""
    if not positions_data:
        conn = get_connection(timeout=10.0, busy_timeout_ms=8000)
        try:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "SELECT ticker, stop_order_id FROM positions WHERE stop_order_id IS NOT NULL"
                )
                for tkr, oid in cursor.fetchall():
                    if oid:
                        cancel_stop_order(str(oid), account_hash=account_hash)
            except sqlite3.OperationalError as e:
                if _db_is_locked(e):
                    raise
            cursor.execute("DELETE FROM positions")
            conn.commit()
        finally:
            conn.close()
        return 0

    conn = get_connection(timeout=10.0, busy_timeout_ms=8000)
    try:
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(positions)")
        pos_cols = [row[1] for row in cursor.fetchall()]
        for col, ctype in [
            ('day_pct', 'REAL'), ('open_pct', 'REAL'),
            ('purchased_at', 'TEXT'), ('peak_gain_pct', 'REAL'),
            ('stop_gain_pct', 'REAL'), ('trail_active', 'INTEGER'),
            ('stop_order_id', 'TEXT'), ('stop_order_price', 'REAL'),
            ('stop_limit_price', 'REAL'), ('stop_order_qty', 'INTEGER'),
            ('stop_defer_logged', 'INTEGER'),
        ]:
            if col not in pos_cols:
                try:
                    cursor.execute(f"ALTER TABLE positions ADD COLUMN {col} {ctype}")
                except sqlite3.OperationalError:
                    pass
        synced = 0
        seen_tickers = set()  # type: set
        # Positions that left the account — log hard vs trail stop-limit fills after commit
        closed_exits = []  # type: List[Dict[str, Any]]
        if config.DEBUG and positions_data:
            print("DEBUG: First position keys:", list(positions_data[0].keys()))
            if positions_data[0].get('instrument'):
                print("DEBUG: First position instrument keys:", list(positions_data[0]['instrument'].keys()))
        for p in positions_data:
            instrument = p.get('instrument', {})
            ticker = instrument.get('symbol')
            if not ticker:
                continue
            long_qty = p.get('longQuantity', 0) or 0
            short_qty = p.get('shortQuantity', 0) or 0
            qty = long_qty - short_qty
            if qty <= 0:
                try:
                    cursor.execute(
                        """
                        SELECT stop_order_id, trail_active, stop_order_price,
                               shares_owned, average_price
                        FROM positions WHERE ticker = ?
                        """,
                        (ticker,),
                    )
                    row = cursor.fetchone()
                    if row:
                        oid, trail_a, stop_px, sh, avg = row
                        if oid or trail_a or stop_px is not None:
                            closed_exits.append({
                                'ticker': ticker,
                                'stop_order_id': oid,
                                'trail_active': bool(trail_a),
                                'stop_order_price': stop_px,
                                'shares_owned': sh,
                                'average_price': avg,
                            })
                        if oid and not str(oid).startswith('SIM'):
                            cancel_stop_order(str(oid), account_hash=account_hash)
                except sqlite3.OperationalError as e:
                    if _db_is_locked(e):
                        raise
                cursor.execute("DELETE FROM positions WHERE ticker = ?", (ticker,))
                continue
            seen_tickers.add(ticker)
            avg_price = p.get('averagePrice')
            market_val = p.get('marketValue')
            current_day_pl = p.get('currentDayProfitLoss')
            long_open_pl = p.get('longOpenProfitLoss')
            # Day % = day P/L as % of start-of-day value (market_value - current_day_profit_loss)
            day_pct = None
            if current_day_pl is not None and market_val is not None:
                sod_val = float(market_val) - float(current_day_pl)
                if sod_val and abs(sod_val) > 1e-9:
                    day_pct = (float(current_day_pl) / sod_val) * 100
            # Open % = total P/L as % of cost basis (shares * average_price)
            open_pct = None
            if long_open_pl is not None and avg_price is not None and qty and float(avg_price) > 0:
                cost_basis = qty * float(avg_price)
                if cost_basis > 0:
                    open_pct = (float(long_open_pl) / cost_basis) * 100
            
            # Prefer Schwab-provided acquired/purchase date if present; else use local date
            date_purchased = None
            for key in ('acquiredDate', 'dateAcquired', 'averagePriceDate', 'purchaseDate'):
                raw = p.get(key) or instrument.get(key)
                if raw is not None:
                    if isinstance(raw, (int, float)):
                        try:
                            from datetime import timezone
                            dt = datetime.fromtimestamp(raw / 1000.0 if raw > 1e12 else raw, tz=timezone.utc)
                            date_purchased = dt.strftime('%Y-%m-%d')
                        except (OSError, ValueError):
                            pass
                    elif isinstance(raw, str) and len(raw) >= 10:
                        date_purchased = raw[:10]
                    if date_purchased:
                        break
            if not date_purchased:
                date_purchased = datetime.now().strftime('%Y-%m-%d')
            purchased_at_default = f"{date_purchased}T00:00:00"
            # Preserve existing purchase timestamps and trail state on sync
            cursor.execute("""
                INSERT INTO positions (
                    ticker, date_purchased, shares_owned, average_price, market_value,
                    current_day_profit_loss, day_pct, long_open_profit_loss, open_pct,
                    purchased_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker) DO UPDATE SET
                    date_purchased = COALESCE(positions.date_purchased, excluded.date_purchased),
                    shares_owned = excluded.shares_owned,
                    average_price = excluded.average_price,
                    market_value = excluded.market_value,
                    current_day_profit_loss = excluded.current_day_profit_loss,
                    day_pct = excluded.day_pct,
                    long_open_profit_loss = excluded.long_open_profit_loss,
                    open_pct = excluded.open_pct,
                    purchased_at = COALESCE(positions.purchased_at, excluded.purchased_at)
            """, (
                ticker, date_purchased, int(qty),
                float(avg_price) if avg_price is not None else None,
                float(market_val) if market_val is not None else None,
                float(current_day_pl) if current_day_pl is not None else None,
                day_pct,
                float(long_open_pl) if long_open_pl is not None else None,
                open_pct,
                purchased_at_default,
            ))
            synced += 1
        
        # Flat locally but gone from Schwab: cancel orphan stops and remove rows
        try:
            cursor.execute(
                """
                SELECT ticker, stop_order_id, trail_active, stop_order_price,
                       shares_owned, average_price
                FROM positions WHERE shares_owned > 0
                """
            )
            for tkr, oid, trail_a, stop_px, sh, avg in cursor.fetchall():
                if tkr not in seen_tickers:
                    closed_exits.append({
                        'ticker': tkr,
                        'stop_order_id': oid,
                        'trail_active': bool(trail_a),
                        'stop_order_price': stop_px,
                        'shares_owned': sh,
                        'average_price': avg,
                    })
                    if oid and not str(oid).startswith('SIM'):
                        cancel_stop_order(str(oid), account_hash=account_hash)
                    cursor.execute("DELETE FROM positions WHERE ticker = ?", (tkr,))
        except sqlite3.OperationalError as e:
            if _db_is_locked(e):
                raise
            pass

        conn.commit()
        synced_count = synced
        closed = list(closed_exits)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    set_runtime_flag(
        'positions_synced_at',
        datetime.now().isoformat(timespec='seconds'),
        timeout=2.0,
        busy_timeout_ms=2000,
        retries=2,
    )
    for ex in closed:
        _log_position_closed_on_sync(ex)
    try:
        bump_dashboard_rev()
    except Exception:
        pass
    return synced_count


def _log_position_closed_on_sync(ex: Dict[str, Any]) -> None:
    """Log when a managed position disappears — distinguish hard vs trail stop fills."""
    ticker = ex.get('ticker')
    if not ticker:
        return
    oid = ex.get('stop_order_id')
    trail = bool(ex.get('trail_active'))
    stop_px = ex.get('stop_order_price')
    shares = ex.get('shares_owned')
    # Only attribute to stop-limit when we had a resting protective order tracked
    if not oid and stop_px is None:
        return
    exit_info = describe_sell_exit('trail' if trail else 'hard', trail)
    qty = None  # type: Optional[int]
    try:
        if shares:
            qty = int(shares)
    except (TypeError, ValueError):
        qty = None
    px = None  # type: Optional[float]
    try:
        if stop_px is not None:
            px = float(stop_px)
    except (TypeError, ValueError):
        px = None
    msg = format_sold_log_message(ticker, qty, px)
    print(f"  {msg} ({exit_info['short_label']})")
    log_event(
        'sell',
        msg,
        detail={
            'ticker': ticker,
            'exit_kind': exit_info['exit_kind'],
            'source': 'stop_limit_fill',
            'phase': 'filled',
            'stop_order_id': oid,
            'stop_order_price': stop_px,
            'trail_active': trail,
            'shares_owned': shares,
            'average_price': ex.get('average_price'),
        },
    )


def refresh_schwab_positions_if_needed(
    force: bool = False,
    min_age_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Pull shares / market value / day+open P/L from Schwab into positions.

    Buy/sell jobs should force=True. Dashboard/UI may throttle via
    POSITIONS_SYNC_MIN_INTERVAL_SECONDS so polls do not hammer the API.
    """
    init_database()
    if min_age_seconds is None:
        min_age_seconds = float(
            getattr(config, 'POSITIONS_SYNC_MIN_INTERVAL_SECONDS', 45)
        )
    now = datetime.now()
    last_s = get_runtime_flag('positions_synced_at')
    age = None  # type: Optional[float]
    if last_s:
        try:
            age = (now - datetime.fromisoformat(last_s)).total_seconds()
        except ValueError:
            age = None

    if not force and age is not None and age < float(min_age_seconds):
        return {
            'ok': True,
            'synced': False,
            'skipped': True,
            'reason': 'fresh',
            'age_seconds': age,
            'synced_at': last_s,
            'count': None,
        }

    if not SCHWAB_AVAILABLE or SCHWAB_CLIENT is None:
        return {
            'ok': False,
            'synced': False,
            'skipped': True,
            'reason': 'schwab_unavailable',
            'age_seconds': age,
            'synced_at': last_s,
            'count': 0,
        }

    count = fetch_and_sync_schwab_positions()
    if count is not None and int(count) == -2:
        return {
            'ok': False,
            'synced': False,
            'skipped': True,
            'reason': 'database_locked',
            'age_seconds': age,
            'synced_at': last_s,
            'count': 0,
        }
    if count is not None and int(count) < 0:
        return {
            'ok': False,
            'synced': False,
            'skipped': False,
            'reason': 'sync_failed',
            'age_seconds': age,
            'synced_at': last_s,
            'count': 0,
        }

    synced_at = get_runtime_flag('positions_synced_at') or now.isoformat(
        timespec='seconds'
    )
    return {
        'ok': True,
        'synced': True,
        'skipped': False,
        'reason': None,
        'age_seconds': 0.0,
        'synced_at': synced_at,
        'count': int(count or 0),
    }


def _get_account_hash() -> Optional[str]:
    """First linked Schwab account hash, or None."""
    if not SCHWAB_AVAILABLE or SCHWAB_CLIENT is None:
        return None
    try:
        linked_accounts_response = SCHWAB_CLIENT.linked_accounts()
        linked_accounts = linked_accounts_response.json()
        if not linked_accounts or (
            isinstance(linked_accounts, list) and len(linked_accounts) == 0
        ):
            return None
        if isinstance(linked_accounts, list):
            return linked_accounts[0].get('hashValue')
        return linked_accounts.get('hashValue')
    except Exception as e:
        print(f"Error getting account hash: {e}")
        return None


def _extract_order_id(response) -> Optional[str]:
    """Order id from Schwab Location header, if present."""
    if response is None:
        return None
    location = ''
    try:
        location = response.headers.get('Location', '') or ''
    except Exception:
        return None
    if not location:
        return None
    parts = location.split('/')
    if 'orders' in parts:
        order_idx = parts.index('orders')
        if order_idx + 1 < len(parts):
            return parts[order_idx + 1]
    return None


def build_stop_limit_sell_order(
    ticker: str,
    quantity: int,
    stop_price: float,
    limit_price: float,
) -> Dict[str, Any]:
    """Schwab STOP_LIMIT SELL JSON body."""
    duration = str(
        getattr(config, 'STOP_ORDER_DURATION', 'GOOD_TILL_CANCEL') or 'GOOD_TILL_CANCEL'
    )
    return {
        'orderType': 'STOP_LIMIT',
        'session': 'NORMAL',
        'duration': duration,
        'orderStrategyType': 'SINGLE',
        'stopPrice': round(float(stop_price), 2),
        'price': round(float(limit_price), 2),
        'orderLegCollection': [
            {
                'instruction': 'SELL',
                'quantity': int(quantity),
                'instrument': {
                    'symbol': ticker,
                    'assetType': 'EQUITY',
                },
            }
        ],
    }


def stop_limit_price_from_stop(stop_price: float) -> float:
    """Limit a small % below stop so the order can fill after trigger."""
    slip = float(getattr(config, 'STOP_LIMIT_SLIPPAGE_PCT', 0.005))
    limit = float(stop_price) * (1.0 - slip)
    # Never round limit above stop
    limit = min(limit, float(stop_price) - 0.01) if float(stop_price) > 0.02 else limit
    return round(max(limit, 0.01), 2)


def get_position_stop_order(ticker: str) -> Dict[str, Any]:
    """Return stored broker stop fields for a ticker."""
    init_database()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            '''
            SELECT stop_order_id, stop_order_price, stop_limit_price, stop_order_qty
            FROM positions WHERE ticker = ?
            ''',
            (ticker,),
        )
        row = cursor.fetchone()
    except sqlite3.OperationalError:
        row = None
    conn.close()
    if not row:
        return {
            'stop_order_id': None,
            'stop_order_price': None,
            'stop_limit_price': None,
            'stop_order_qty': None,
        }
    return {
        'stop_order_id': row[0],
        'stop_order_price': row[1],
        'stop_limit_price': row[2],
        'stop_order_qty': row[3],
    }


def sync_position_stop_order(
    ticker: str,
    order_id: Optional[str],
    stop_price: Optional[float],
    limit_price: Optional[float],
    qty: Optional[int],
) -> None:
    init_database()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        '''
        UPDATE positions
        SET stop_order_id = ?, stop_order_price = ?, stop_limit_price = ?, stop_order_qty = ?
        WHERE ticker = ?
        ''',
        (
            order_id,
            float(stop_price) if stop_price is not None else None,
            float(limit_price) if limit_price is not None else None,
            int(qty) if qty is not None else None,
            ticker,
        ),
    )
    conn.commit()
    conn.close()


def clear_position_stop_order(ticker: str) -> None:
    sync_position_stop_order(ticker, None, None, None, None)


def cancel_stop_order(order_id: str, account_hash: Optional[str] = None) -> bool:
    """Cancel a Schwab order by id. Returns True on success."""
    if not order_id or str(order_id) in ('PENDING', 'SIM-STOP') or str(order_id).startswith('SIM'):
        return False
    if not SCHWAB_AVAILABLE or SCHWAB_CLIENT is None:
        print(f"Cannot cancel stop {order_id}: Schwab not available")
        return False
    if trade_dry_run_enabled():
        print(f"[DRY-RUN] Would cancel stop order {order_id}")
        return True
    account_hash = account_hash or _get_account_hash()
    if not account_hash:
        print(f"Cannot cancel stop {order_id}: no account hash")
        return False
    try:
        response = SCHWAB_CLIENT.cancel_order(account_hash, order_id)
        ok = response.status_code in (200, 201, 204)
        if ok:
            print(f"✓ Cancelled stop order {order_id}")
        else:
            print(
                f"✗ Cancel stop {order_id} failed: {response.status_code} {response.text}"
            )
        return ok
    except Exception as e:
        print(f"Error cancelling stop {order_id}: {e}")
        return False


def place_stop_limit_sell(
    ticker: str,
    quantity: int,
    stop_price: float,
    limit_price: Optional[float] = None,
    account_hash: Optional[str] = None,
) -> Optional[str]:
    """
    Place a resting STOP_LIMIT SELL. Returns new order_id or None.
    Updates positions.stop_order_* on success.
    """
    qty = int(quantity)
    if qty < 1:
        return None
    stop = round(float(stop_price), 2)
    limit = (
        round(float(limit_price), 2)
        if limit_price is not None
        else stop_limit_price_from_stop(stop)
    )
    if trade_dry_run_enabled():
        print(
            f"[DRY-RUN] STOP-LIMIT {qty} {ticker} @ ${stop:.2f} "
            f"(limit ${limit:.2f})"
        )
        sync_position_stop_order(ticker, 'SIM-STOP', stop, limit, qty)
        return 'SIM-STOP'

    if not SCHWAB_AVAILABLE or SCHWAB_CLIENT is None:
        print(f"Cannot place stop for {ticker}: Schwab not available")
        return None
    account_hash = account_hash or _get_account_hash()
    if not account_hash:
        print(f"Cannot place stop for {ticker}: no account hash")
        return None

    order = build_stop_limit_sell_order(ticker, qty, stop, limit)
    try:
        print(
            f"  ARMING STOP {qty} {ticker} @ ${stop:.2f} "
            f"(limit ${limit:.2f})..."
        )
        response = SCHWAB_CLIENT.place_order(account_hash, order)
        if response.status_code not in (200, 201):
            print(
                f"✗ STOP_LIMIT place failed for {ticker}: "
                f"{response.status_code} {response.text}"
            )
            log_event(
                'stop-limit',
                f'STOP-LIMIT failed for {ticker}',
                level='error',
                detail={'status': response.status_code, 'body': response.text[:500]},
            )
            return None
        order_id = _extract_order_id(response)
        if not order_id:
            print(
                f"✓ STOP_LIMIT accepted for {ticker} "
                f"(no order id in Location; will reconcile later)"
            )
            order_id = 'PENDING'
        else:
            print(f"✓ STOP_LIMIT placed for {ticker} order_id={order_id}")
        sync_position_stop_order(ticker, order_id, stop, limit, qty)
        return order_id
    except Exception as e:
        print(f"Error placing STOP_LIMIT for {ticker}: {e}")
        import traceback
        traceback.print_exc()
        return None


def replace_stop_limit_sell(
    ticker: str,
    order_id: str,
    quantity: int,
    stop_price: float,
    limit_price: Optional[float] = None,
    account_hash: Optional[str] = None,
) -> Optional[str]:
    """
    Replace an existing stop with a new STOP_LIMIT. Returns new order_id if known.
    """
    qty = int(quantity)
    stop = round(float(stop_price), 2)
    limit = (
        round(float(limit_price), 2)
        if limit_price is not None
        else stop_limit_price_from_stop(stop)
    )
    if trade_dry_run_enabled():
        print(
            f"[DRY-RUN] STOP-LIMIT {qty} {ticker} @ ${stop:.2f} "
            f"(replace, limit ${limit:.2f})"
        )
        sync_position_stop_order(ticker, order_id or 'SIM-STOP', stop, limit, qty)
        return order_id or 'SIM-STOP'

    if not SCHWAB_AVAILABLE or SCHWAB_CLIENT is None:
        print(f"Cannot replace stop for {ticker}: Schwab not available")
        return None
    account_hash = account_hash or _get_account_hash()
    if not account_hash:
        print(f"Cannot replace stop for {ticker}: no account hash")
        return None

    order = build_stop_limit_sell_order(ticker, qty, stop, limit)
    try:
        print(
            f"  ARMING STOP {qty} {ticker} @ ${stop:.2f} "
            f"(replace, limit ${limit:.2f})..."
        )
        response = SCHWAB_CLIENT.replace_order(account_hash, order_id, order)
        if response.status_code not in (200, 201):
            print(
                f"✗ STOP_LIMIT replace failed for {ticker}: "
                f"{response.status_code} {response.text}"
            )
            # Fall back: cancel + place
            print(f"  Falling back to cancel+place for {ticker}...")
            cancel_stop_order(order_id, account_hash=account_hash)
            clear_position_stop_order(ticker)
            return place_stop_limit_sell(
                ticker, qty, stop, limit, account_hash=account_hash
            )
        new_id = _extract_order_id(response) or order_id
        sync_position_stop_order(ticker, new_id, stop, limit, qty)
        print(f"✓ STOP_LIMIT replaced for {ticker} order_id={new_id}")
        return new_id
    except Exception as e:
        print(f"Error replacing STOP_LIMIT for {ticker}: {e}")
        import traceback
        traceback.print_exc()
        return None


def ensure_broker_stop_limit(
    ticker: str,
    quantity: int,
    stop_price: float,
    spot_price: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Place or replace resting STOP_LIMIT as needed.

    Returns dict with keys: action (placed|replaced|unchanged|skipped|failed), order_id, ...
    Never arms on the same ET calendar day as purchase (avoids PDT via stop fills).
    """
    qty = int(quantity)
    stop = round(float(stop_price), 2)
    limit = stop_limit_price_from_stop(stop)

    hold_ok, hold_reason = is_min_hold_met(ticker)
    if not hold_ok:
        # Cancel any same-day resting stop so it cannot fill into a day trade.
        try:
            cancel_position_broker_stop(ticker)
        except Exception as e:
            print(f"Warning: cancel deferred stop for {ticker}: {e}")
        return {
            'action': 'skipped',
            'reason': 'stop deferred — %s' % hold_reason,
            'stop_price': stop,
            'limit_price': limit,
        }

    # Already through stop → sell_now path should handle; don't place invalid stop
    if spot_price is not None and float(spot_price) <= stop:
        return {
            'action': 'skipped',
            'reason': 'spot at/through stop (use sell_now)',
            'stop_price': stop,
            'limit_price': limit,
        }

    stored = get_position_stop_order(ticker)
    existing_id = stored.get('stop_order_id')
    existing_stop = stored.get('stop_order_price')
    existing_qty = stored.get('stop_order_qty')
    min_move = float(getattr(config, 'STOP_REPLACE_MIN_DOLLARS', 0.05))
    existing_s = str(existing_id) if existing_id else ''
    is_sim = existing_s in ('SIM-STOP',) or existing_s.startswith('SIM')
    is_pending = existing_s == 'PENDING'
    is_sim_or_pending = (not existing_id) or is_sim or is_pending

    # Dry-run SIM stops: treat like live orders so we log set/moved once, not every pass.
    # Live mode must still promote SIM → a real Schwab order.
    if is_sim and trade_dry_run_enabled():
        if existing_stop is not None and abs(float(existing_stop) - stop) < min_move:
            if existing_qty is None or int(existing_qty) == qty:
                return {
                    'action': 'unchanged',
                    'order_id': existing_id,
                    'stop_price': float(existing_stop),
                    'limit_price': stored.get('stop_limit_price'),
                    'quantity': existing_qty,
                }
        order_id = place_stop_limit_sell(ticker, qty, stop, limit)
        if order_id:
            _emit_stop_limit_placed_or_moved(
                ticker, stop, previous_stop=existing_stop,
                extra={'order_id': order_id, 'quantity': qty, 'limit_price': limit},
            )
            return {
                'action': 'replaced' if existing_stop is not None else 'placed',
                'order_id': order_id,
                'stop_price': stop,
                'limit_price': limit,
                'quantity': qty,
            }
        return {
            'action': 'failed',
            'reason': 'place failed',
            'stop_price': stop,
            'limit_price': limit,
        }

    need_place = is_sim_or_pending or (
        existing_qty is not None and int(existing_qty) != qty
    )
    if need_place:
        # Qty mismatch with live order, or promote PENDING → real place
        if existing_id and not is_sim_or_pending:
            if existing_qty is not None and int(existing_qty) != qty:
                cancel_stop_order(str(existing_id))
                clear_position_stop_order(ticker)
        elif existing_id and is_sim_or_pending:
            clear_position_stop_order(ticker)
        order_id = place_stop_limit_sell(ticker, qty, stop, limit)
        if order_id:
            _emit_stop_limit_placed_or_moved(
                ticker, stop, previous_stop=existing_stop,
                extra={'order_id': order_id, 'quantity': qty, 'limit_price': limit},
            )
            return {
                'action': 'placed',
                'order_id': order_id,
                'stop_price': stop,
                'limit_price': limit,
                'quantity': qty,
            }
        return {
            'action': 'failed',
            'reason': 'place failed',
            'stop_price': stop,
            'limit_price': limit,
        }

    if existing_stop is not None and abs(float(existing_stop) - stop) < min_move:
        return {
            'action': 'unchanged',
            'order_id': existing_id,
            'stop_price': float(existing_stop),
            'limit_price': stored.get('stop_limit_price'),
            'quantity': existing_qty,
        }

    new_id = replace_stop_limit_sell(ticker, str(existing_id), qty, stop, limit)
    if new_id:
        _emit_stop_limit_placed_or_moved(
            ticker, stop, previous_stop=existing_stop,
            extra={'order_id': new_id, 'quantity': qty, 'limit_price': limit},
        )
        return {
            'action': 'replaced',
            'order_id': new_id,
            'stop_price': stop,
            'limit_price': limit,
            'quantity': qty,
        }
    return {
        'action': 'failed',
        'reason': 'replace failed',
        'order_id': existing_id,
        'stop_price': stop,
        'limit_price': limit,
    }


def cancel_position_broker_stop(ticker: str) -> None:
    """Cancel resting stop for ticker (if any) and clear DB fields."""
    stored = get_position_stop_order(ticker)
    oid = stored.get('stop_order_id')
    if oid and not str(oid).startswith('SIM'):
        cancel_stop_order(str(oid))
    elif oid:
        print(f"[DRY-RUN] Clearing sim stop for {ticker}")
    clear_position_stop_order(ticker)


def execute_buy(ticker: str, quantity: int):
    """Execute buy order. Submits to Schwab only when TRADE_DRY_RUN is False."""
    # Safety check: Verify stock is in watchlist
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT ticker FROM watchlist WHERE ticker = ?", (ticker,))
    if not cursor.fetchone():
        conn.close()
        print(f"⚠️  Trading blocked: {ticker} is not in watchlist.")
        print(f"   Buy order for {quantity} shares of {ticker} cancelled.")
        print(f"   Add {ticker} to watchlist using update_watchlist() before trading.")
        return
    conn.close()
    
    # Live Schwab price required for sizing / cash-floor check (no Yahoo fallback)
    price = get_trade_price(ticker)
    if price is None:
        print(f"⚠️  Trading blocked: no live Schwab price for {ticker}.")
        print(f"   Buy order for {quantity} shares of {ticker} cancelled.")
        return
    total = float(price) * quantity
    price_str = f" at ${float(price):.2f} per share for a total of ${total:.2f}"

    # Safety check: trading allowed and this trade would not drop effective cash below MINIMUM_CASH
    order_amount = float(total)
    is_allowed, reason = is_trading_allowed(trade_amount_dollars=order_amount)
    if not is_allowed:
        print(f"⚠️  Trading blocked: {reason}")
        print(f"   Buy order for {quantity} shares of {ticker} cancelled.")
        return
    
    if schwab_order_submit_allowed():
        if not SCHWAB_AVAILABLE:
            print(f"Error: Cannot execute real trade for {ticker} - Schwab API not available")
            return
        
        try:
            # Get account hash (needed for placing orders)
            linked_accounts_response = SCHWAB_CLIENT.linked_accounts()
            linked_accounts = linked_accounts_response.json()
            
            if not linked_accounts or (isinstance(linked_accounts, list) and len(linked_accounts) == 0):
                print(f"Error: No linked accounts found. Cannot place order for {ticker}")
                return
            
            # Get first account hash
            if isinstance(linked_accounts, list):
                account_hash = linked_accounts[0].get('hashValue')
            else:
                account_hash = linked_accounts.get('hashValue')
            
            if not account_hash:
                print(f"Error: Could not get account hash. Cannot place order for {ticker}")
                return
            
            # Build market order JSON for BUY
            order = {
                "orderType": "MARKET",
                "session": "NORMAL",
                "duration": "DAY",
                "orderStrategyType": "SINGLE",
                "orderLegCollection": [
                    {
                        "instruction": "BUY",
                        "quantity": quantity,
                        "instrument": {
                            "symbol": ticker,
                            "assetType": "EQUITY"
                        }
                    }
                ]
            }
            
            # Place the order
            print(f"Placing market BUY order for {quantity} shares of {ticker}{price_str}...")
            response = SCHWAB_CLIENT.place_order(account_hash, order)
            
            # Check response
            if response.status_code in [200, 201]:
                # Extract order ID from Location header if available
                location = response.headers.get('Location', '')
                order_id = None
                if location:
                    # Location format: .../accounts/{hash}/orders/{order_id}
                    parts = location.split('/')
                    if 'orders' in parts:
                        order_idx = parts.index('orders')
                        if order_idx + 1 < len(parts):
                            order_id = parts[order_idx + 1]
                
                if order_id:
                    print(f"✓ Order placed successfully! Order ID: {order_id}")
                else:
                    print(f"✓ Order placed successfully! (Response: {response.status_code})")

                # Record as pending order (positions come from fetch_and_sync_schwab_positions when order fills)
                order_amount_dollars = float(total) if total is not None else (float(price) * quantity) if price else 0.0
                date_ordered = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
                conn = get_connection()
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO pending_orders (ticker, date_ordered, quantity_ordered, order_amount_dollars, order_id) VALUES (?, ?, ?, ?, ?)",
                    (ticker, date_ordered, quantity, order_amount_dollars, order_id)
                )
                # Seed purchase timestamp / trail fields for min-hold (sync will fill shares when filled)
                now_iso = datetime.now().isoformat()
                cur.execute("""
                    INSERT INTO positions (ticker, date_purchased, shares_owned, average_price, purchased_at,
                                          peak_gain_pct, stop_gain_pct, trail_active)
                    VALUES (?, ?, 0, ?, ?, 0, NULL, 0)
                    ON CONFLICT(ticker) DO UPDATE SET
                        purchased_at = COALESCE(positions.purchased_at, excluded.purchased_at),
                        date_purchased = COALESCE(positions.date_purchased, excluded.date_purchased),
                        average_price = COALESCE(positions.average_price, excluded.average_price)
                """, (ticker, datetime.now().strftime('%Y-%m-%d'),
                      float(price) if price else None, now_iso))
                conn.commit()
                conn.close()
                # Tag before trade_history so scorecard sees origin=algo_buy
                try:
                    set_position_book(
                        ticker,
                        'algorithm',
                        note='bought by algorithm',
                        origin='algo_buy',
                    )
                except Exception as book_err:
                    print(f"Warning: could not tag {ticker} as algorithm book: {book_err}")
                record_trade(
                    'buy', ticker, quantity,
                    price=float(price) if price is not None else None,
                    order_id=order_id,
                    mode='real',
                    note='PLACING BUY — order submitted, awaiting fill',
                    scorecard='algorithm',
                )
                clear_rebuy_guard(ticker)
                try:
                    sync_schwab_account_after_order('buy')
                except Exception as sync_err:
                    print(f"Warning: post-buy Schwab sync failed: {sync_err}")
            else:
                print(f"✗ Order failed with status {response.status_code}")
                print(f"  Response: {response.text}")
                return
                
        except Exception as e:
            print(f"Error placing buy order for {ticker}: {e}")
            import traceback
            traceback.print_exc()
            return
    else:
        print(
            f"[DRY-RUN] {format_bought_log_message(ticker, quantity, price, dry_run=True)}"
        )
        print(f"   (TRADE_DRY_RUN — no Schwab order; shares_owned not updated)")
        log_event(
            'buy',
            format_bought_log_message(ticker, quantity, price, dry_run=True),
            detail={
                'ticker': ticker,
                'quantity': quantity,
                'price': float(price) if price is not None else None,
                'phase': 'filled',
                'dry_run': True,
            },
        )
        try:
            set_position_book(
                ticker,
                'algorithm',
                note='dry-run buy by algorithm',
                origin='algo_buy',
            )
        except Exception as book_err:
            print(f"Warning: could not tag {ticker} as algorithm book: {book_err}")
        record_trade(
            'buy', ticker, quantity,
            price=float(price) if price is not None else None,
            mode='simulation',
            note='DRY-RUN PLACING BUY — not submitted',
            scorecard='algorithm',
        )
        clear_rebuy_guard(ticker)


def get_position_origin(ticker: str) -> Optional[str]:
    """Return origin: 'legacy' | 'algo_buy' | 'enrolled', or None if untagged."""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT origin FROM position_book WHERE ticker = ?', (ticker,))
        row = cursor.fetchone()
        conn.close()
        if not row or row[0] is None:
            return None
        return str(row[0])
    except Exception:
        return None


def position_counts_for_algo_scorecard(ticker: str, side: str = 'sell') -> bool:
    """
    Algo-only scorecard: only positions bought by the algorithm after start.

    Legacy holdouts and enrolled-from-legacy names never count (even if sold).
    """
    origin = get_position_origin(ticker)
    if origin == 'algo_buy':
        return True
    # Buy path before origin row exists: treat as algo if era has started
    if (side or '').lower() == 'buy' and get_algorithm_start():
        return True
    return False


def describe_sell_exit(
    stop_kind: Optional[str] = None,
    trail_active: bool = False,
) -> Dict[str, str]:
    """
    Labels for protective exits:
    - hard_stop: price fell to the loss floor vs cost
    - trail_stop: gave back gains after trail was armed / tracking
    """
    kind = (stop_kind or '').strip().lower()
    if kind == 'trail' or (kind not in ('hard', 'trail') and trail_active):
        return {
            'exit_kind': 'trail_stop',
            'short_label': 'TRAIL-STOP',
            'summary': 'gave back gains after trail tracking',
        }
    return {
        'exit_kind': 'hard_stop',
        'short_label': 'HARD-STOP',
        'summary': 'hit loss floor (too low vs cost)',
    }


def record_trade(
    side: str,
    ticker: str,
    quantity: int,
    price: Optional[float],
    order_id: Optional[str] = None,
    cost_basis_per_share: Optional[float] = None,
    mode: Optional[str] = None,
    note: Optional[str] = None,
    scorecard: Optional[str] = None,
) -> Optional[int]:
    """
    Append one row to trade_history for model/performance tracking.

    side: 'buy' or 'sell'
    price: intended/fill estimate at order time (Schwab fill may differ slightly)
    cost_basis_per_share: for sells, average cost used for realized_pl
    scorecard: 'algorithm' counts on algo-only scorecard; 'excluded' does not
               (legacy / enrolled sells). Auto-detected from position origin if None.
    """
    side_norm = (side or '').strip().lower()
    if side_norm not in ('buy', 'sell'):
        print('record_trade: side must be buy or sell')
        return None
    if mode is None:
        mode = 'simulation' if trade_dry_run_enabled() else 'real'
    if scorecard is None:
        scorecard = 'algorithm' if position_counts_for_algo_scorecard(ticker, side_norm) else 'excluded'
    else:
        scorecard = str(scorecard).strip().lower()
        if scorecard not in ('algorithm', 'excluded'):
            scorecard = 'excluded'
    ts = datetime.now().isoformat(timespec='seconds')
    qty = int(quantity)
    px = float(price) if price is not None else None
    dollars = (px * qty) if px is not None else None
    basis = float(cost_basis_per_share) if cost_basis_per_share is not None else None
    realized = None
    if side_norm == 'sell' and px is not None and basis is not None:
        realized = (px - basis) * qty
    try:
        init_database()
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            '''
            INSERT INTO trade_history (
                ts, side, ticker, quantity, price, dollars,
                cost_basis_per_share, realized_pl, order_id, mode, note, scorecard
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                ts, side_norm, ticker, qty, px, dollars,
                basis, realized, order_id, mode, note, scorecard,
            )
        )
        trade_id = cursor.lastrowid
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"record_trade failed: {e}")
        return None
    sc_s = '' if scorecard == 'algorithm' else ' [excluded from algo scorecard]'
    if side_norm == 'buy':
        print(
            f"  trade_history: BUY {qty} {ticker} @ "
            f"{('$' + format(px, ',.2f')) if px is not None else 'n/a'} "
            f"(id={trade_id}){sc_s}"
        )
    else:
        pl_s = (
            f", realized P/L ${realized:,.2f}" if realized is not None else ''
        )
        print(
            f"  trade_history: SELL {qty} {ticker} @ "
            f"{('$' + format(px, ',.2f')) if px is not None else 'n/a'}{pl_s} "
            f"(id={trade_id}){sc_s}"
        )
    base_msg = (
        f"{side_norm.upper()} {qty} {ticker}"
        + (f" @ ${px:.2f}" if px is not None else '')
        + (f" realized ${realized:,.2f}" if realized is not None else '')
        + sc_s
    )
    # Ledger stays in trade_history only — do not duplicate into the viewer Log.
    return trade_id


def get_trade_history(
    ticker: Optional[str] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """Return trade_history rows newest first (for eval / future dashboard)."""
    init_database()
    limit = max(1, min(int(limit), 5000))
    conn = get_connection()
    cursor = conn.cursor()
    if ticker:
        cursor.execute(
            '''
            SELECT id, ts, side, ticker, quantity, price, dollars,
                   cost_basis_per_share, realized_pl, order_id, mode, note, scorecard
            FROM trade_history WHERE ticker = ?
            ORDER BY id DESC LIMIT ?
            ''',
            (ticker, limit)
        )
    else:
        cursor.execute(
            '''
            SELECT id, ts, side, ticker, quantity, price, dollars,
                   cost_basis_per_share, realized_pl, order_id, mode, note, scorecard
            FROM trade_history ORDER BY id DESC LIMIT ?
            ''',
            (limit,)
        )
    rows = cursor.fetchall()
    conn.close()
    cols = [
        'id', 'ts', 'side', 'ticker', 'quantity', 'price', 'dollars',
        'cost_basis_per_share', 'realized_pl', 'order_id', 'mode', 'note',
        'scorecard',
    ]
    return [dict(zip(cols, r)) for r in rows]


def summarize_trade_history(ticker: Optional[str] = None) -> Dict[str, Any]:
    """Aggregate buys/sells and sum of recorded realized_pl on sells."""
    init_database()
    conn = get_connection()
    cursor = conn.cursor()
    if ticker:
        cursor.execute(
            """
            SELECT
              SUM(CASE WHEN side='buy' THEN 1 ELSE 0 END),
              SUM(CASE WHEN side='buy' THEN dollars ELSE 0 END),
              SUM(CASE WHEN side='sell' THEN 1 ELSE 0 END),
              SUM(CASE WHEN side='sell' THEN dollars ELSE 0 END),
              SUM(CASE WHEN side='sell' THEN COALESCE(realized_pl, 0) ELSE 0 END)
            FROM trade_history WHERE ticker = ?
            """,
            (ticker,),
        )
    else:
        cursor.execute(
            """
            SELECT
              SUM(CASE WHEN side='buy' THEN 1 ELSE 0 END),
              SUM(CASE WHEN side='buy' THEN dollars ELSE 0 END),
              SUM(CASE WHEN side='sell' THEN 1 ELSE 0 END),
              SUM(CASE WHEN side='sell' THEN dollars ELSE 0 END),
              SUM(CASE WHEN side='sell' THEN COALESCE(realized_pl, 0) ELSE 0 END)
            FROM trade_history
            """
        )
    row = cursor.fetchone()
    conn.close()
    return {
        'ticker': ticker,
        'buy_count': int(row[0] or 0),
        'buy_dollars': float(row[1] or 0),
        'sell_count': int(row[2] or 0),
        'sell_dollars': float(row[3] or 0),
        'realized_pl_sum': float(row[4] or 0),
    }


def record_rebuy_guard(
    ticker: str,
    sell_price: Optional[float],
    cost_basis: Optional[float] = None,
) -> None:
    """Remember last sell so we don't immediately re-buy the same name."""
    init_database()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        '''
        INSERT INTO rebuy_guards (ticker, last_sell_price, last_cost_basis, last_sold_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            last_sell_price = excluded.last_sell_price,
            last_cost_basis = COALESCE(excluded.last_cost_basis, rebuy_guards.last_cost_basis),
            last_sold_at = excluded.last_sold_at
        ''',
        (ticker, sell_price, cost_basis, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


def get_rebuy_guard(ticker: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            'SELECT last_sell_price, last_cost_basis, last_sold_at FROM rebuy_guards WHERE ticker = ?',
            (ticker,)
        )
        row = cursor.fetchone()
    except sqlite3.OperationalError:
        row = None
    conn.close()
    if not row:
        return None
    return {
        'last_sell_price': row[0],
        'last_cost_basis': row[1],
        'last_sold_at': row[2],
    }


def clear_rebuy_guard(ticker: str) -> None:
    """Drop debounce after a successful buy so the next sell starts a fresh window."""
    init_database()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('DELETE FROM rebuy_guards WHERE ticker = ?', (ticker,))
        conn.commit()
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()


def _rebuy_sell_market_date(last_sold_at: Any) -> Optional[date]:
    """ET calendar date of last sell from rebuy_guards.last_sold_at."""
    parsed = _parse_purchased_at(last_sold_at)
    if parsed is None and last_sold_at:
        raw = str(last_sold_at).strip()
        if len(raw) >= 10:
            try:
                return datetime.strptime(raw[:10], '%Y-%m-%d').date()
            except ValueError:
                return None
        return None
    if parsed is None:
        return None
    return _as_market_datetime(parsed).date()


def weekdays_between_exclusive_start(
    start: date,
    end: date,
) -> int:
    """
    Count Mon–Fri dates strictly after `start` through `end` inclusive.
    Exchange holidays count as trading days (no holiday calendar).
    """
    if end <= start:
        return 0
    count = 0
    d = start + timedelta(days=1)
    while d <= end:
        if d.weekday() < 5:
            count += 1
        d += timedelta(days=1)
    return count


def rebuy_allowed(ticker: str, price: float) -> Tuple[bool, str]:
    """
    After a sell, unlock rebuy when either:
      - price <= last_sell * (1 - REBUY_DISCOUNT_PCT), or
      - >= REBUY_COOLDOWN_TRADING_DAYS weekdays have elapsed since sell date (ET).
    """
    guard = get_rebuy_guard(ticker)
    if not guard:
        return True, 'no prior sell guard'

    discount = float(getattr(config, 'REBUY_DISCOUNT_PCT', 0.05))
    cooldown_days = int(getattr(config, 'REBUY_COOLDOWN_TRADING_DAYS', 5))
    last_sell = guard.get('last_sell_price')
    sold_at = guard.get('last_sold_at')

    if last_sell is not None and float(last_sell) > 0:
        ceiling = float(last_sell) * (1.0 - discount)
        if float(price) <= ceiling:
            return True, (
                f'rebuy ok: ${float(price):.2f} <= {discount * 100:.0f}% below sell '
                f'(${float(last_sell):.2f} -> ceiling ${ceiling:.2f})'
            )
    else:
        ceiling = None

    sell_day = _rebuy_sell_market_date(sold_at)
    today = _now_market().date()
    elapsed = weekdays_between_exclusive_start(sell_day, today) if sell_day else 0
    if sell_day is not None and elapsed >= cooldown_days:
        return True, (
            f'rebuy ok: {elapsed} weekdays since sell on {sell_day.isoformat()} '
            f'(need {cooldown_days})'
        )

    parts = []
    if sell_day is not None:
        left = max(0, cooldown_days - elapsed)
        parts.append(
            f'{left} weekday(s) left of {cooldown_days}-day cooldown '
            f'(sold {sell_day.isoformat()} ET)'
        )
    else:
        parts.append(f'need {cooldown_days} weekdays since sell (unknown sell date)')
    if ceiling is not None:
        parts.append(
            f'or price <= ${ceiling:.2f} ({discount * 100:.0f}% below '
            f'last sell ${float(last_sell):.2f}; now ${float(price):.2f})'
        )
    elif last_sell is None or float(last_sell) <= 0:
        # No usable sell price — only time can unlock; if sell_day missing, allow.
        if sell_day is None:
            return True, 'no sell price or date on guard'
    return False, 'rebuy debounce: ' + '; '.join(parts)


def execute_sell(
    ticker: str,
    quantity: int,
    note: Optional[str] = None,
    exit_kind: Optional[str] = None,
):
    """Execute sell order. Submits to Schwab only when TRADE_DRY_RUN is False.
    Allowed for any owned position (may be off watchlist).

    note / exit_kind: optional log labels (hard_stop vs trail_stop).
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT shares_owned, average_price FROM positions WHERE ticker = ? AND shares_owned > 0",
        (ticker,)
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        print(f"⚠️  Trading blocked: {ticker} is not in positions (nothing to sell).")
        print(f"   Sell order for {quantity} shares of {ticker} cancelled.")
        return
    cost_basis = float(row[1]) if row[1] is not None else None
    sell_price = get_trade_price(ticker)
    if sell_price is None:
        print(f"⚠️  Trading blocked: no live Schwab price for {ticker}.")
        print(f"   Sell order for {quantity} shares of {ticker} cancelled.")
        return

    if schwab_order_submit_allowed():
        if not SCHWAB_AVAILABLE:
            print(f"Error: Cannot execute real trade for {ticker} - Schwab API not available")
            return
        
        try:
            # Get account hash (needed for placing orders)
            linked_accounts_response = SCHWAB_CLIENT.linked_accounts()
            linked_accounts = linked_accounts_response.json()
            
            if not linked_accounts or (isinstance(linked_accounts, list) and len(linked_accounts) == 0):
                print(f"Error: No linked accounts found. Cannot place order for {ticker}")
                return
            
            # Get first account hash
            if isinstance(linked_accounts, list):
                account_hash = linked_accounts[0].get('hashValue')
            else:
                account_hash = linked_accounts.get('hashValue')
            
            if not account_hash:
                print(f"Error: Could not get account hash. Cannot place order for {ticker}")
                return
            
            # Build market order JSON for SELL
            order = {
                "orderType": "MARKET",
                "session": "NORMAL",
                "duration": "DAY",
                "orderStrategyType": "SINGLE",
                "orderLegCollection": [
                    {
                        "instruction": "SELL",
                        "quantity": quantity,
                        "instrument": {
                            "symbol": ticker,
                            "assetType": "EQUITY"
                        }
                    }
                ]
            }
            
            exit_info = describe_sell_exit(
                'trail' if exit_kind == 'trail_stop' else 'hard',
                trail_active=(exit_kind == 'trail_stop'),
            )
            print(
                f"  Placing market sell {quantity} {ticker}"
                + (f" @ ~${float(sell_price):.2f}" if sell_price is not None else '')
                + f" ({exit_info['short_label']})..."
            )

            response = SCHWAB_CLIENT.place_order(account_hash, order)

            # Check response
            if response.status_code in [200, 201]:
                # Extract order ID from Location header if available
                location = response.headers.get('Location', '')
                order_id = None
                if location:
                    # Location format: .../accounts/{hash}/orders/{order_id}
                    parts = location.split('/')
                    if 'orders' in parts:
                        order_idx = parts.index('orders')
                        if order_idx + 1 < len(parts):
                            order_id = parts[order_idx + 1]

                if order_id:
                    print(f"✓ Order placed successfully! Order ID: {order_id}")
                else:
                    print(f"✓ Order placed successfully! (Response: {response.status_code})")

                # Update watchlist shares owned (negative for sell, only after successful order)
                update_shares_owned(ticker, -quantity)
                record_rebuy_guard(ticker, sell_price=sell_price, cost_basis=cost_basis)
                trade_note = (
                    f"PLACING MARKET SELL — order submitted "
                    f"({exit_info['short_label']})"
                )
                record_trade(
                    'sell', ticker, quantity,
                    price=float(sell_price) if sell_price is not None else None,
                    order_id=order_id,
                    cost_basis_per_share=cost_basis,
                    mode='real',
                    note=trade_note,
                )
                clear_position_stop_order(ticker)
                try:
                    sync_schwab_account_after_order('sell')
                except Exception as sync_err:
                    print(f"Warning: post-sell Schwab sync failed: {sync_err}")
                sold_msg = format_sold_log_message(
                    ticker, quantity,
                    float(sell_price) if sell_price is not None else None,
                )
                print(f"  {sold_msg}")
                log_event(
                    'sell',
                    sold_msg,
                    detail={
                        'ticker': ticker,
                        'quantity': quantity,
                        'price': sell_price,
                        'order_id': order_id,
                        'exit_kind': exit_kind or exit_info['exit_kind'],
                        'phase': 'filled',
                        'order_type': 'MARKET',
                    },
                )
            else:
                print(f"✗ Order failed with status {response.status_code}")
                print(f"  Response: {response.text}")
                return

        except Exception as e:
            print(f"Error placing sell order for {ticker}: {e}")
            import traceback
            traceback.print_exc()
            return
    else:
        exit_info = describe_sell_exit(
            'trail' if exit_kind == 'trail_stop' else 'hard',
            trail_active=(exit_kind == 'trail_stop'),
        )
        sold_msg = format_sold_log_message(
            ticker, quantity,
            float(sell_price) if sell_price is not None else None,
            dry_run=True,
        )
        print(f"[DRY-RUN] {sold_msg}")
        if note:
            print(f"   ({note})")
        print(f"   (TRADE_DRY_RUN — no Schwab order; shares_owned not updated)")
        log_event(
            'sell',
            sold_msg,
            detail={
                'ticker': ticker,
                'quantity': quantity,
                'price': sell_price,
                'exit_kind': exit_kind or exit_info['exit_kind'],
                'phase': 'filled',
                'order_type': 'MARKET',
                'dry_run': True,
                'reason': note,
            },
        )
        record_trade(
            'sell', ticker, quantity,
            price=float(sell_price) if sell_price is not None else None,
            cost_basis_per_share=cost_basis,
            mode='simulation',
            note='DRY-RUN PLACE MARKET SELL — not submitted',
        )
        # Still record debounce so dry-run loops behave consistently
        record_rebuy_guard(ticker, sell_price=sell_price, cost_basis=cost_basis)
        clear_position_stop_order(ticker)


def _parse_schwab_orders_payload(orders_data: Any) -> List[Dict[str, Any]]:
    """Normalize Schwab orders JSON (list or wrapped dict) into a list of order dicts."""
    if isinstance(orders_data, list):
        return orders_data
    if isinstance(orders_data, dict):
        orders = orders_data.get('orders', orders_data.get('orderList', []))
        if orders:
            return orders
        for value in orders_data.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                if 'status' in value[0] or 'orderId' in value[0]:
                    return value
    return []


def _open_orders_from_schwab_orders(orders: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Filter Schwab orders to open/working legs: ticker, quantity, order_id."""
    open_statuses = {
        'OPEN', 'WORKING', 'QUEUED', 'ACCEPTED', 'AWAITING_PARENT_ORDER',
        'AWAITING_CONDITION', 'AWAITING_STOP_CONDITION',
        'AWAITING_MANUAL_REVIEW', 'AWAITING_UR_OUT', 'PENDING_ACTIVATION',
        'PENDING_CANCEL', 'PENDING_REPLACE',
    }
    open_orders = []
    for order in orders:
        if order.get('status', '') not in open_statuses:
            continue
        order_id = order.get('orderId') or order.get('order_id')
        if order_id is not None:
            order_id = str(order_id)
        for leg in order.get('orderLegCollection', []) or []:
            instrument = leg.get('instrument', {}) or {}
            ticker = instrument.get('symbol', '')
            quantity = int(leg.get('quantity', 0) or 0)
            if ticker and quantity > 0:
                open_orders.append({
                    'ticker': ticker,
                    'quantity': quantity,
                    'order_id': order_id,
                })
    return open_orders


def get_open_orders(account_hash: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Get all open orders from Schwab API.
    
    Args:
        account_hash: Optional account hash. If None, uses first linked account.
    
    Returns:
        List of dictionaries with 'ticker', 'quantity', and 'order_id' (optional) keys, or empty list on error.
        Example: [{'ticker': 'DELL', 'quantity': 1, 'order_id': '123'}, ...]
    """
    if not SCHWAB_AVAILABLE or SCHWAB_CLIENT is None:
        print("Warning: Schwab API not available. Cannot retrieve open orders.")
        return []
    
    try:
        # Get account hash if not provided
        if not account_hash:
            linked_accounts_response = SCHWAB_CLIENT.linked_accounts()
            linked_accounts = linked_accounts_response.json()
            
            if not linked_accounts or (isinstance(linked_accounts, list) and len(linked_accounts) == 0):
                print("Error: No linked accounts found.")
                return []
            
            if isinstance(linked_accounts, list):
                account_hash = linked_accounts[0].get('hashValue')
            else:
                account_hash = linked_accounts.get('hashValue')
            
            if not account_hash:
                print("Error: Could not get account hash.")
                return []
        
        # schwabdev accepts datetime objects and converts them; both account_orders*
        # methods require fromEnteredTime / toEnteredTime (no zero-arg form).
        to_date = datetime.now(timezone.utc) + timedelta(days=1)
        from_date = datetime.now(timezone.utc) - timedelta(days=90)
        response = SCHWAB_CLIENT.account_orders(account_hash, from_date, to_date)

        if response.status_code != 200:
            print(f"Error retrieving orders: Status {response.status_code}")
            print(f"Response: {response.text}")
            return []

        orders = _parse_schwab_orders_payload(response.json())
        if not orders:
            # Wider window for older resting GTC stops
            from_date_wide = to_date - timedelta(days=365)
            response_wide = SCHWAB_CLIENT.account_orders(account_hash, from_date_wide, to_date)
            if response_wide.status_code == 200:
                orders = _parse_schwab_orders_payload(response_wide.json())

        return _open_orders_from_schwab_orders(orders)
            
    except Exception as e:
        print(f"Error getting open orders: {e}")
        import traceback
        traceback.print_exc()
        return []


def reconcile_pending_orders(account_hash: Optional[str] = None) -> int:
    """
    Remove from pending_orders any row whose order_id is no longer in Schwab open orders
    (filled or cancelled). Logs each cleared buy so the UI/log stay in sync.
    
    Returns:
        Number of rows removed from pending_orders.
    """
    try:
        open_orders = get_open_orders(account_hash)
        open_order_ids = {str(o['order_id']) for o in open_orders if o.get('order_id')}
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, order_id, ticker, quantity_ordered, order_amount_dollars
            FROM pending_orders WHERE order_id IS NOT NULL
            """
        )
        rows = cursor.fetchall()
        cleared = []  # type: List[Dict[str, Any]]
        for pk, oid, ticker, qty, dollars in rows:
            if oid and str(oid) not in open_order_ids:
                cleared.append({
                    'id': pk,
                    'order_id': oid,
                    'ticker': ticker,
                    'quantity': qty,
                    'dollars': dollars,
                })
                cursor.execute("DELETE FROM pending_orders WHERE id = ?", (pk,))
        # Also drop pending rows with no order_id that are older than today (orphan)
        cursor.execute(
            """
            DELETE FROM pending_orders
            WHERE order_id IS NULL AND date_ordered < ?
            """,
            ((datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d'),),
        )
        conn.commit()
        conn.close()
        for row in cleared:
            qty = row.get('quantity')
            dollars = row.get('dollars')
            px = None  # type: Optional[float]
            try:
                if qty and dollars and float(qty) > 0:
                    px = float(dollars) / float(qty)
            except (TypeError, ValueError):
                px = None
            ticker = row.get('ticker')
            qty_i = None  # type: Optional[int]
            try:
                if qty is not None:
                    qty_i = int(qty)
            except (TypeError, ValueError):
                qty_i = None
            msg = format_bought_log_message(ticker, qty_i, px)
            print(f"  {msg}")
            log_event(
                'buy',
                msg,
                detail={
                    'ticker': ticker,
                    'quantity': qty,
                    'price': px,
                    'order_id': row.get('order_id'),
                    'dollars': dollars,
                    'source': 'pending_reconcile',
                    'phase': 'filled',
                },
            )
        return len(cleared)
    except sqlite3.OperationalError:
        # pending_orders table may not exist yet
        return 0


def sync_schwab_account(force_positions: bool = True) -> Dict[str, Any]:
    """
    Reconcile pending buys against Schwab open orders, then refresh positions
    (marks, shares, stop-limit exits). Used after orders and by schwab_sync job.
    """
    cleared = 0
    try:
        cleared = reconcile_pending_orders()
    except Exception as e:
        print(f"Warning: pending-order reconcile failed: {e}")
    pos = refresh_schwab_positions_if_needed(force=bool(force_positions))
    return {
        'pending_cleared': cleared,
        'positions': pos,
    }


def sync_schwab_account_after_order(label: str = 'order') -> Dict[str, Any]:
    """Short wait for market orders to fill, then reconcile + position sync."""
    delay = float(getattr(config, 'POST_ORDER_SYNC_DELAY_SECONDS', 3))
    if delay > 0:
        print(f"  Waiting {delay:.0f}s for Schwab fill ({label})...")
        time.sleep(delay)
    result = sync_schwab_account(force_positions=True)
    print(
        f"  Post-{label} sync: pending_cleared={result.get('pending_cleared')}, "
        f"positions_ok={bool((result.get('positions') or {}).get('ok'))}"
    )
    return result


def display_open_orders(account_hash: Optional[str] = None):
    """
    Display open orders in a readable format.
    
    Args:
        account_hash: Optional account hash. If None, uses first linked account.
    """
    orders = get_open_orders(account_hash)
    
    if not orders:
        print("\nNo open orders found.")
        return
    
    print(f"\n{'='*60}")
    print(f"Open Orders ({len(orders)})")
    print(f"{'='*60}")
    
    for order in orders:
        ticker = order.get('ticker', 'N/A')
        quantity = order.get('quantity', 0)
        print(f"  {ticker}: {quantity} share(s)")
    
    print(f"\n{'='*60}\n")


# ============================================================================
# Buy / sell decision layer (dry-run capable)
# ============================================================================

def get_trade_price(ticker: str) -> Optional[float]:
    """
    Live Schwab quote only (no Yahoo / DB fallback).

    Buy/sell/stop decisions skip the ticker when this returns None.
    """
    if not SCHWAB_AVAILABLE or SCHWAB_CLIENT is None:
        print(f"  get_trade_price({ticker}): Schwab API not available — no trade price")
        return None
    streaming_data = get_streaming_data(ticker)
    if not streaming_data:
        print(f"  get_trade_price({ticker}): Schwab quote failed — no trade price")
        return None
    if streaming_data.get('error'):
        print(
            f"  get_trade_price({ticker}): Schwab error "
            f"({streaming_data.get('error')}) — no trade price"
        )
        return None
    price = streaming_data.get('price')
    if price is None:
        print(f"  get_trade_price({ticker}): Schwab quote missing price — no trade price")
        return None
    try:
        px = float(price)
    except (TypeError, ValueError):
        print(f"  get_trade_price({ticker}): invalid Schwab price {price!r}")
        return None
    if px <= 0:
        print(f"  get_trade_price({ticker}): non-positive Schwab price {px}")
        return None
    return px


def _parse_purchased_at(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        pass
    try:
        return datetime.strptime(value[:10], '%Y-%m-%d')
    except ValueError:
        return None


def _as_market_datetime(dt: datetime) -> datetime:
    """Interpret dt in MARKET_TIMEZONE (naive values treated as already in that zone)."""
    tz = _market_tz()
    if tz is None:
        return dt
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def get_purchase_market_date(ticker: str) -> Optional[date]:
    """
    Purchase calendar date in MARKET_TIMEZONE (ET).

    Prefer date_purchased (Schwab trade date) when it is a bare date; otherwise
    convert purchased_at / datetime to the ET calendar day. FINRA day trades
    use this calendar trading day, not a rolling 24-hour window.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT purchased_at, date_purchased FROM positions WHERE ticker = ?",
        (ticker,)
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    purchased_at, date_purchased = row[0], row[1]
    # Bare YYYY-MM-DD is the trade date — use directly (no local-midnight skew).
    if date_purchased and len(str(date_purchased).strip()) >= 10:
        raw = str(date_purchased).strip()
        try:
            return datetime.strptime(raw[:10], '%Y-%m-%d').date()
        except ValueError:
            pass
    purchased = _parse_purchased_at(purchased_at) or _parse_purchased_at(date_purchased)
    if not purchased:
        return None
    return _as_market_datetime(purchased).date()


def hold_hours_elapsed(ticker: str) -> Optional[float]:
    """Hours since purchased_at (or date_purchased). None if unknown."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT purchased_at, date_purchased FROM positions WHERE ticker = ?",
        (ticker,)
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    purchased = _parse_purchased_at(row[0]) or _parse_purchased_at(row[1])
    if not purchased:
        return None
    now = _now_market()
    purch = _as_market_datetime(purchased)
    if purch.tzinfo is not None and now.tzinfo is None:
        now = now.replace(tzinfo=purch.tzinfo)
    elif purch.tzinfo is None and now.tzinfo is not None:
        purch = purch.replace(tzinfo=now.tzinfo)
    return (now - purch).total_seconds() / 3600.0


def hours_until_exit_allowed(ticker: str) -> Optional[float]:
    """Hours until next ET calendar day after purchase (when exits/stops unlock)."""
    purch = get_purchase_market_date(ticker)
    if purch is None:
        return None
    now = _now_market()
    unlock_day = purch + timedelta(days=1)
    tz = _market_tz()
    unlock = datetime(unlock_day.year, unlock_day.month, unlock_day.day, 0, 0, 0)
    if tz is not None:
        unlock = unlock.replace(tzinfo=tz)
        if now.tzinfo is None:
            now = now.replace(tzinfo=tz)
    secs = (unlock - now).total_seconds()
    return max(0.0, secs / 3600.0)


def is_min_hold_met(ticker: str) -> Tuple[bool, str]:
    """
    Whether market sells and broker STOP_LIMITs are allowed for ticker.

    Hard rule: no exit/stop on the same ET calendar day as purchase (FINRA day
    trade = same trading day). Buy Mon 1pm / sell Tue 8am is allowed.
    """
    purch = get_purchase_market_date(ticker)
    if purch is None:
        return False, "unknown purchase time"
    today = _now_market().date()
    if today <= purch:
        left = hours_until_exit_allowed(ticker)
        left_s = f", ~{left:.1f}h until next ET day" if left is not None else ""
        return False, (
            f"same trading day as purchase ({purch.isoformat()} ET){left_s}; "
            f"exits/stops deferred until next ET calendar day"
        )
    return True, f"hold ok (purchased {purch.isoformat()} ET; today {today.isoformat()} ET)"


def ticker_on_watchlist(ticker: str) -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM watchlist WHERE ticker = ?", (ticker,))
    found = cursor.fetchone() is not None
    conn.close()
    return found


def get_owned_tickers() -> set:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT ticker FROM positions WHERE shares_owned > 0")
    owned = {row[0] for row in cursor.fetchall()}
    conn.close()
    return owned


def get_pending_buy_tickers() -> set:
    """Tickers with unfilled buy orders (treat as already spoken for)."""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT ticker FROM pending_orders")
        pending = {row[0] for row in cursor.fetchall()}
        conn.close()
        return pending
    except sqlite3.OperationalError:
        return set()


def get_watchlist_ranked() -> List[str]:
    """Watchlist tickers ranked by analyst upside (highest first)."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(f"""
        SELECT ticker FROM watchlist
        WHERE current_price > 0 AND target_price > 0
        {_ANALYST_UPSIDE_ORDER_BY}
    """)
    ranked = [row[0] for row in cursor.fetchall()]
    # Append any watchlist names missing prices (unranked tail)
    cursor.execute("SELECT ticker FROM watchlist")
    all_wl = [row[0] for row in cursor.fetchall()]
    conn.close()
    seen = set(ranked)
    for t in all_wl:
        if t not in seen:
            ranked.append(t)
    return ranked


def record_account_snapshot(note: str = '') -> None:
    """Persist cash + liquidation snapshot (cash is primary success metric)."""
    init_database()
    account = get_account_info() or {}
    if not account_info_usable(account):
        msg = 'Skipped account snapshot (%s): unusable Schwab balances' % (note or 'untitled')
        print('Warning: %s' % msg)
        try:
            log_event('account', msg, detail={
                'note': note,
                'source': account.get('source'),
                'ok': account.get('ok'),
                'cash': account.get('cash'),
                'liquidation_value': account.get('liquidation_value'),
                'total_value': account.get('total_value'),
            })
        except Exception:
            pass
        return
    cash = float(account.get('cash') or 0.0)
    pending = get_pending_orders_total_dollars()
    effective = cash - pending
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        '''
        INSERT INTO account_snapshots
            (ts, cash, effective_cash, liquidation_value, total_value, note)
        VALUES (?, ?, ?, ?, ?, ?)
        ''',
        (
            datetime.now().isoformat(),
            cash,
            effective,
            float(account.get('liquidation_value') or 0.0),
            float(account.get('total_value') or 0.0),
            note,
        )
    )
    conn.commit()
    conn.close()


def purge_invalid_account_snapshots() -> int:
    """
    Remove unusable snapshot rows so charts / Cash never show fabricated balances:
      - zero equity (failed API reads)
      - legacy offline sentinel cash=liq=total=10000
    """
    init_database()
    uid = _uid()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        '''
        DELETE FROM account_snapshots
        WHERE user_id = ?
          AND (
            (
                COALESCE(liquidation_value, 0) <= 0
                AND COALESCE(total_value, 0) <= 0
            )
            OR (
                ABS(COALESCE(cash, 0) - 10000) < 0.01
                AND ABS(COALESCE(liquidation_value, 0) - 10000) < 0.01
                AND ABS(COALESCE(total_value, 0) - 10000) < 0.01
            )
          )
        ''',
        (uid,),
    )
    removed = int(cursor.rowcount or 0)
    conn.commit()
    conn.close()
    return removed


# ============================================================================
# Algorithm start / legacy holdouts / performance
# ============================================================================

def get_algorithm_start() -> Optional[str]:
    """ISO start timestamp for the algorithm era (config override or DB flag)."""
    cfg = getattr(config, 'ALGORITHM_START', None)
    if cfg:
        return str(cfg)
    return get_runtime_flag('algorithm_start')


def set_position_book(
    ticker: str,
    book: str,
    note: Optional[str] = None,
    enrolled_at: Optional[str] = None,
    origin: Optional[str] = None,
) -> None:
    """
    Tag a ticker's sell book and scorecard origin.

    book: 'legacy' (sell-skipped) or 'algorithm' (sell-managed)
    origin: 'legacy' | 'algo_buy' | 'enrolled' — only algo_buy counts on scorecard
    """
    book_norm = (book or '').strip().lower()
    if book_norm not in ('legacy', 'algorithm'):
        raise ValueError("book must be 'legacy' or 'algorithm'")
    origin_norm = None
    if origin is not None:
        origin_norm = str(origin).strip().lower()
        if origin_norm not in ('legacy', 'algo_buy', 'enrolled'):
            raise ValueError("origin must be 'legacy', 'algo_buy', or 'enrolled'")
    init_database()
    now = datetime.now().isoformat(timespec='seconds')
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        'SELECT book, enrolled_at, origin FROM position_book WHERE ticker = ?',
        (ticker,),
    )
    prev = cursor.fetchone()
    if enrolled_at is None and book_norm == 'algorithm':
        enrolled_at = now if not prev or prev[0] != 'algorithm' else prev[1]
    if origin_norm is None:
        if prev and prev[2]:
            origin_norm = prev[2]
        elif book_norm == 'legacy':
            origin_norm = 'legacy'
        else:
            origin_norm = 'algo_buy'
    cursor.execute(
        '''
        INSERT INTO position_book (ticker, book, tagged_at, enrolled_at, note, origin)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            book = excluded.book,
            tagged_at = excluded.tagged_at,
            enrolled_at = COALESCE(excluded.enrolled_at, position_book.enrolled_at),
            note = COALESCE(excluded.note, position_book.note),
            origin = excluded.origin
        ''',
        (ticker, book_norm, now, enrolled_at, note, origin_norm)
    )
    conn.commit()
    conn.close()


def get_position_book(ticker: str) -> Optional[str]:
    """Return 'legacy', 'algorithm', or None if untagged."""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT book FROM position_book WHERE ticker = ?', (ticker,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def is_legacy_holdout(ticker: str) -> bool:
    return get_position_book(ticker) == 'legacy'


def list_position_books() -> Dict[str, List[str]]:
    """Tickers grouped by book (owned positions preferred for display)."""
    init_database()
    owned = sorted(get_owned_tickers())
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT ticker, book, origin FROM position_book')
    tags = {}  # type: Dict[str, str]
    origins = {}  # type: Dict[str, str]
    for r in cursor.fetchall():
        tags[r[0]] = r[1]
        if r[2]:
            origins[r[0]] = r[2]
    conn.close()
    legacy = []  # type: List[str]
    algorithm = []  # type: List[str]
    untagged = []  # type: List[str]
    algo_buys = []  # type: List[str]
    enrolled = []  # type: List[str]
    for t in owned:
        b = tags.get(t)
        if b == 'legacy':
            legacy.append(t)
        elif b == 'algorithm':
            algorithm.append(t)
            if origins.get(t) == 'enrolled':
                enrolled.append(t)
            elif origins.get(t) == 'algo_buy':
                algo_buys.append(t)
        else:
            untagged.append(t)
    return {
        'legacy': legacy,
        'algorithm': algorithm,
        'untagged': untagged,
        'algo_buys': algo_buys,
        'enrolled': enrolled,
        'all_tags': tags,
        'all_origins': origins,
    }


def enroll_to_algorithm(ticker: str, note: Optional[str] = None) -> None:
    """
    Put a holding under sell rules.

    Origin 'enrolled' = pre-start / carve-in holding — gains never count on algo scorecard.
    """
    set_position_book(
        ticker,
        'algorithm',
        note=note or 'enrolled (sell-managed; excluded from algo scorecard)',
        enrolled_at=datetime.now().isoformat(timespec='seconds'),
        origin='enrolled',
    )
    log_event(
        'book',
        f'{ticker} enrolled into algorithm book (scorecard excluded)',
    )


def compute_trail_state_for_position(
    ticker: str,
    purchase: float,
    price: float,
    peak_gain: Optional[float] = None,
    stop_gain: Optional[float] = None,
    trail_active: bool = False,
) -> Dict[str, Any]:
    """
    Same trail/hard-stop math as propose_sells (bring a name 'up to speed').

    Returns peak/stop/kind without writing proposals. Caller may persist.
    """
    activate = float(getattr(config, 'TRAIL_ACTIVATE_PCT', 0.10))
    buffer_on = float(getattr(config, 'TRAIL_BUFFER_PCT', 0.10))
    buffer_off = float(getattr(config, 'TRAIL_BUFFER_OFF_WATCHLIST_PCT', 0.07))
    hard_on = float(getattr(config, 'HARD_STOP_ON_WATCHLIST_PCT', -0.15))
    hard_off = float(getattr(config, 'HARD_STOP_OFF_WATCHLIST_PCT', -0.08))

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
    on_wl = ticker_on_watchlist(ticker)
    buffer = buffer_off if not on_wl else buffer_on
    hard = hard_off if not on_wl else hard_on

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
        'ticker': ticker,
        'purchase': purchase,
        'price': price,
        'gain_pct': gain,
        'peak_gain_pct': peak,
        'stop_gain_pct': new_stop,
        'stop_price': stop_price,
        'stop_kind': stop_kind,
        'trail_active': active,
        'on_watchlist': on_wl,
        'breached': gain <= float(new_stop),
    }


def bring_positions_up_to_speed(
    tickers: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Seed peak/trail/hard-stop state so enrolled (or listed) names look as if
    they had always been algorithm-managed. Does not place orders.
    """
    init_database()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT ticker, shares_owned, average_price, peak_gain_pct, stop_gain_pct, trail_active
        FROM positions WHERE shares_owned > 0
    """)
    rows = cursor.fetchall()
    conn.close()

    want = None  # type: Optional[set]
    if tickers is not None:
        want = {str(t).strip().upper() for t in tickers}

    results = []  # type: List[Dict[str, Any]]
    for ticker, shares, avg_price, peak_gain, stop_gain, trail_active in rows:
        if want is not None and ticker not in want:
            continue
        if int(shares or 0) <= 0:
            continue
        if avg_price is None or float(avg_price) <= 0:
            results.append({
                'ticker': ticker,
                'ok': False,
                'reason': 'missing average_price',
            })
            continue
        price = get_trade_price(ticker)
        if not price or price <= 0:
            results.append({
                'ticker': ticker,
                'ok': False,
                'reason': 'no live Schwab price',
            })
            continue
        state = compute_trail_state_for_position(
            ticker,
            float(avg_price),
            float(price),
            peak_gain=peak_gain,
            stop_gain=stop_gain,
            trail_active=bool(trail_active),
        )
        _update_position_trail_state(
            ticker,
            state['peak_gain_pct'],
            state['stop_gain_pct'],
            state['trail_active'],
        )
        state['ok'] = True
        results.append(state)
        print(
            f"  up-to-speed {ticker}: gain {state['gain_pct']*100:.1f}% | "
            f"peak {state['peak_gain_pct']*100:.1f}% | "
            f"{state['stop_kind']} stop ${state['stop_price']:.2f} "
            f"({state['stop_gain_pct']*100:.1f}%)"
            + (' [ALREADY THROUGH STOP]' if state['breached'] else '')
        )
    return results


def mark_algorithm_start(force: bool = False) -> Dict[str, Any]:
    """
    Soft-reset go-live:

    - Sets algorithm_start + account snapshot
    - Enrolls ALL current holdings under sell rules (origin=enrolled, not on scorecard)
      except optional ALGORITHM_LEGACY_AT_START carve-outs
    - Brings trail/hard-stop state up to speed (as if always algorithm-managed)
    - New buys after this are origin=algo_buy and DO count on the scorecard
    """
    init_database()
    existing = get_runtime_flag('algorithm_start')
    if existing and not force:
        print(
            f"Algorithm start already set to {existing}. "
            f"Pass force=True or use --mark-algorithm-start --force to redo."
        )
        return {
            'ok': False,
            'algorithm_start': existing,
            'reason': 'already_set',
        }

    cfg = getattr(config, 'ALGORITHM_START', None)
    start_ts = str(cfg) if cfg else datetime.now().isoformat(timespec='seconds')
    set_runtime_flag('algorithm_start', start_ts)

    try:
        fetch_and_sync_schwab_positions()
    except Exception as e:
        print(f"Warning: position sync before algorithm start failed: {e}")

    owned = sorted(get_owned_tickers())
    legacy_cfg = {
        str(t).strip().upper()
        for t in (getattr(config, 'ALGORITHM_LEGACY_AT_START', None) or [])
        if t
    }
    enrolled_now = []  # type: List[str]
    legacy_now = []  # type: List[str]
    for t in owned:
        if t in legacy_cfg:
            set_position_book(
                t,
                'legacy',
                note='legacy carve-out at algorithm start (sell-skipped)',
                origin='legacy',
            )
            legacy_now.append(t)
        else:
            enroll_to_algorithm(
                t,
                note='enrolled at algorithm start (pre-start holding; excluded from algo scorecard)',
            )
            enrolled_now.append(t)

    print("Bringing enrolled positions up to speed (trail / hard stops)...")
    speed = bring_positions_up_to_speed(tickers=enrolled_now)

    books = list_position_books()
    record_account_snapshot(note='algorithm_start')
    log_event(
        'algorithm',
        f'Algorithm start {start_ts}; enrolled={enrolled_now}; legacy={legacy_now}',
        detail={
            'algorithm_start': start_ts,
            'enrolled': enrolled_now,
            'legacy': legacy_now,
        },
    )
    print("=" * 60)
    print("ALGORITHM START (soft reset)")
    print("=" * 60)
    print(f"  Start: {start_ts}")
    print(f"  Enrolled under sell rules ({len(enrolled_now)}): {enrolled_now}")
    if legacy_now:
        print(f"  Legacy carve-outs sell-skipped ({len(legacy_now)}): {legacy_now}")
    print("  Trail/hard-stop state seeded from current prices (up to speed).")
    print("  Algo scorecard: ONLY new buys after start (origin=algo_buy).")
    print("  Enrolled pre-start holdings never count on the scorecard.")
    print("  Snapshot saved with note=algorithm_start.")
    print("=" * 60)
    return {
        'ok': True,
        'algorithm_start': start_ts,
        'enrolled': enrolled_now,
        'legacy': legacy_now,
        'up_to_speed': speed,
        'performance': get_algorithm_performance(),
    }


def get_algorithm_start_snapshot() -> Optional[Dict[str, Any]]:
    """First account_snapshots row with note=algorithm_start."""
    init_database()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        '''
        SELECT ts, cash, effective_cash, liquidation_value, total_value, note
        FROM account_snapshots
        WHERE note = 'algorithm_start'
        ORDER BY id ASC LIMIT 1
        '''
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    return {
        'ts': row[0],
        'cash': row[1],
        'effective_cash': row[2],
        'liquidation_value': row[3],
        'total_value': row[4],
        'note': row[5],
    }


def get_algorithm_performance() -> Dict[str, Any]:
    """
    Soft-reset performance since algorithm_start.

    Primary (algo-only scorecard):
      - Realized P/L on trades with scorecard='algorithm' (algo buys + their sells)
      - Unrealized P/L on open positions with origin='algo_buy'
      - Legacy / enrolled names never count (even if later sold)

    Secondary (account context only):
      - Whole-account equity vs start snapshot (includes legacy MTM)
    """
    start = get_algorithm_start()
    snap = get_algorithm_start_snapshot()
    books = list_position_books()
    account = get_account_info() or {}
    pending = get_pending_orders_total_dollars()
    # Never surface fabricated placeholder balances — fall back to last real snapshot.
    if not account_info_usable(account):
        last = get_latest_account_snapshot() or {}
        cash = float(last['cash']) if last.get('cash') is not None else None
        liq = (
            float(last['liquidation_value'])
            if last.get('liquidation_value') is not None else None
        )
        total = (
            float(last['total_value'])
            if last.get('total_value') is not None
            else liq
        )
    else:
        cash = float(account.get('cash') or 0.0)
        liq = float(account.get('liquidation_value') or 0.0)
        total = float(account.get('total_value') or liq or 0.0)
    effective = (cash - pending) if cash is not None else None

    start_liq = float(snap['liquidation_value']) if snap and snap.get('liquidation_value') is not None else None
    start_total = float(snap['total_value']) if snap and snap.get('total_value') is not None else None
    start_eff = float(snap['effective_cash']) if snap and snap.get('effective_cash') is not None else None
    baseline = start_liq if start_liq is not None else start_total
    current_eq = None
    if liq is not None and float(liq) > 0:
        current_eq = float(liq)
    elif total is not None and float(total) > 0:
        current_eq = float(total)
    equity_delta = (
        (current_eq - baseline)
        if baseline is not None and current_eq is not None
        else None
    )

    # Trades since start — split scorecard vs excluded
    trades_since = []  # type: List[Dict[str, Any]]
    algo_trades = []  # type: List[Dict[str, Any]]
    algo_realized = 0.0
    excluded_realized = 0.0
    algo_buy_dollars = 0.0
    algo_sell_dollars = 0.0
    start_key = (start or '')[:19]
    origins = books.get('all_origins') or {}
    if start_key:
        for t in get_trade_history(limit=5000):
            ts = (t.get('ts') or '')[:19]
            if ts < start_key:
                continue
            trades_since.append(t)
            sc = (t.get('scorecard') or '').lower()
            # Legacy rows without scorecard: infer from current origin (best effort)
            if not sc:
                if origins.get(t.get('ticker')) == 'algo_buy' or (
                    t.get('side') == 'buy'
                ):
                    sc = 'algorithm'
                else:
                    sc = 'excluded'
            if sc != 'algorithm':
                if t.get('side') == 'sell' and t.get('realized_pl') is not None:
                    excluded_realized += float(t['realized_pl'])
                continue
            algo_trades.append(t)
            if t.get('side') == 'sell' and t.get('realized_pl') is not None:
                algo_realized += float(t['realized_pl'])
            if t.get('side') == 'buy' and t.get('dollars') is not None:
                algo_buy_dollars += float(t['dollars'])
            if t.get('side') == 'sell' and t.get('dollars') is not None:
                algo_sell_dollars += float(t['dollars'])

    # Unrealized: only origin=algo_buy (not enrolled, not legacy)
    algo_unrealized = 0.0
    algo_mv = 0.0
    legacy_mv = 0.0
    enrolled_mv = 0.0
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        '''
        SELECT ticker, shares_owned, average_price, market_value, long_open_profit_loss
        FROM positions WHERE shares_owned > 0
        '''
    )
    tags = books.get('all_tags') or {}
    for ticker, shares, avg, mv, open_pl in cursor.fetchall():
        book = tags.get(ticker) or 'untagged'
        origin = origins.get(ticker) or ('legacy' if book == 'legacy' else None)
        mv_f = float(mv) if mv is not None else 0.0
        if book == 'legacy' or origin == 'legacy':
            legacy_mv += mv_f
        elif origin == 'enrolled':
            enrolled_mv += mv_f
        elif origin == 'algo_buy':
            algo_mv += mv_f
            if open_pl is not None:
                algo_unrealized += float(open_pl)
            elif avg is not None and mv is not None:
                algo_unrealized += mv_f - float(shares or 0) * float(avg)
    conn.close()

    algo_total_pl = algo_realized + algo_unrealized

    return {
        'algorithm_start': start,
        'start_snapshot': snap,
        'now': {
            'cash': cash,
            'effective_cash': effective,
            'liquidation_value': liq,
            'total_value': total,
            'effective_cash_at_start': start_eff,
        },
        # Primary algo-only scorecard
        'scorecard': {
            'realized_pl': algo_realized,
            'unrealized_pl': algo_unrealized,
            'total_pl': algo_total_pl,
            'buy_dollars': algo_buy_dollars,
            'sell_dollars': algo_sell_dollars,
            'trade_count': len(algo_trades),
            'open_market_value': algo_mv,
        },
        # Backward-compatible aliases (algo-only, not whole-account)
        'realized_pl_since_start': algo_realized,
        'buy_dollars_since_start': algo_buy_dollars,
        'sell_dollars_since_start': algo_sell_dollars,
        'trades_since_start': len(algo_trades),
        'algorithm_book_market_value': algo_mv,
        'algorithm_book_unrealized_pl': algo_unrealized,
        'legacy_book_market_value': legacy_mv,
        'enrolled_book_market_value': enrolled_mv,
        'excluded_realized_pl_since_start': excluded_realized,
        # Account context (includes legacy MTM — not the algo grade)
        'account_equity_delta_since_start': equity_delta,
        'equity_delta_since_start': equity_delta,
        'equity_delta_pct': (
            (equity_delta / baseline * 100.0)
            if equity_delta is not None and baseline and abs(baseline) > 1e-9
            else None
        ),
        'books': {
            'legacy': books['legacy'],
            'algorithm': books['algorithm'],
            'algo_buys': books.get('algo_buys') or [],
            'enrolled': books.get('enrolled') or [],
            'untagged': books['untagged'],
        },
        'note': (
            'Algo scorecard = origin=algo_buy only. '
            'Legacy and enrolled sells are excluded even if the bot sells them. '
            'equity_delta_since_start is whole-account context (includes legacy MTM).'
        ),
    }


# ============================================================================
# Account equity vs S&P (SPY) performance series
# ============================================================================

PERFORMANCE_RANGES = {
    '1D': 1,
    '1W': 7,
    '1M': 30,
    '3M': 91,
    '6M': 182,
    '1Y': 365,
}

_spy_history_cache = {
    'key': None,
    'data': None,
    'fetched_at': 0.0,
}  # type: Dict[str, Any]


def _parse_iso_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if 'T' in text:
            return datetime.fromisoformat(text.replace('Z', '+00:00')).date()
        return date.fromisoformat(text[:10])
    except (TypeError, ValueError):
        try:
            return datetime.strptime(text[:10], '%Y-%m-%d').date()
        except (TypeError, ValueError):
            return None


def _is_at_or_after_market_close(when: Optional[datetime] = None) -> bool:
    """True on a weekday at/after regular-session close (ET)."""
    now = when if when is not None else _now_market()
    tz = _market_tz()
    if tz is not None and now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    elif tz is not None and now.tzinfo is not None:
        now = now.astimezone(tz)
    if now.weekday() >= 5:
        return False
    close_h = int(getattr(config, 'MARKET_CLOSE_HOUR', 16))
    close_m = int(getattr(config, 'MARKET_CLOSE_MINUTE', 0))
    return (now.hour * 60 + now.minute) >= (close_h * 60 + close_m)


def _has_daily_close_for_date(day: date) -> bool:
    init_database()
    day_s = day.isoformat()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        '''
        SELECT 1 FROM account_snapshots
        WHERE note = 'daily_close' AND substr(ts, 1, 10) = ?
        LIMIT 1
        ''',
        (day_s,),
    )
    found = cursor.fetchone() is not None
    conn.close()
    return found


def maybe_record_daily_equity_close() -> bool:
    """
    After RTH close on a weekday, persist one account_snapshots row with
    note='daily_close' for today's ET calendar date. Idempotent.
    """
    now = _now_market()
    if now.weekday() >= 5:
        return False
    if not _is_at_or_after_market_close(now):
        return False
    day = now.date()
    if _has_daily_close_for_date(day):
        return False
    try:
        record_account_snapshot(note='daily_close')
        # record_account_snapshot may skip unusable API reads — only count if persisted
        if not _has_daily_close_for_date(day):
            print(
                'Warning: daily equity close skipped for %s (unusable account info)'
                % day.isoformat()
            )
            return False
        log_event('account', 'Daily equity close snapshot for %s' % day.isoformat())
        print('Recorded daily equity close snapshot for %s' % day.isoformat())
        return True
    except Exception as e:
        print('Warning: daily equity close snapshot failed: %s' % e)
        return False


def get_account_equity_daily_series(
    start_day: date,
    end_day: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """
    One equity point per calendar day from account_snapshots.

    Preference within a day: daily_close > algorithm_start > other notes (last ts).
    Read-only — no purge / no live Schwab call (those blocked the web chart).
    """
    init_database()
    if end_day is None:
        end_day = _now_market().date()
    # Fail fast if the trader loop holds a write lock — chart should still load.
    conn = get_connection(timeout=3.0, busy_timeout_ms=2000)
    try:
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT ts, cash, effective_cash, liquidation_value, total_value, note
            FROM account_snapshots
            WHERE substr(ts, 1, 10) >= ? AND substr(ts, 1, 10) <= ?
            ORDER BY ts ASC
            ''',
            (start_day.isoformat(), end_day.isoformat()),
        )
        rows = cursor.fetchall()
    finally:
        conn.close()

    by_day = {}  # type: Dict[str, Dict[str, Any]]
    rank = {'daily_close': 3, 'algorithm_start': 2}

    for ts, cash, effective, liq, total, note in rows:
        day_s = (ts or '')[:10]
        if not day_s:
            continue
        note_s = (note or '').strip()
        equity = None
        if liq is not None:
            try:
                equity = float(liq)
            except (TypeError, ValueError):
                equity = None
        if equity is None and total is not None:
            try:
                equity = float(total)
            except (TypeError, ValueError):
                equity = None
        # Ignore failed API reads persisted as $0 (would chart as -100%)
        if equity is None or equity <= 0:
            continue
        if _is_fabricated_balance_triplet(cash, liq, total):
            continue
        candidate = {
            'date': day_s,
            'equity': equity,
            'ts': ts,
            'note': note_s,
            'cash': float(cash) if cash is not None else None,
            'effective_cash': float(effective) if effective is not None else None,
            'liquidation_value': float(liq) if liq is not None else None,
            'total_value': float(total) if total is not None else None,
            '_rank': rank.get(note_s, 1),
        }
        prev = by_day.get(day_s)
        if prev is None:
            by_day[day_s] = candidate
            continue
        if candidate['_rank'] > prev['_rank']:
            by_day[day_s] = candidate
        elif candidate['_rank'] == prev['_rank'] and (ts or '') >= (prev.get('ts') or ''):
            by_day[day_s] = candidate

    out = []  # type: List[Dict[str, Any]]
    for day_s in sorted(by_day.keys()):
        item = dict(by_day[day_s])
        item.pop('_rank', None)
        out.append(item)
    return out


def fetch_spy_daily_closes(
    start_day: date,
    end_day: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """Daily SPY adjusted closes from Yahoo (cached ~15 minutes)."""
    global _spy_history_cache
    if end_day is None:
        end_day = _now_market().date()
    # yfinance end is exclusive
    fetch_end = end_day + timedelta(days=1)
    key = '%s:%s' % (start_day.isoformat(), end_day.isoformat())
    now_ts = time.time()
    if (
        _spy_history_cache.get('key') == key
        and _spy_history_cache.get('data') is not None
        and now_ts - float(_spy_history_cache.get('fetched_at') or 0) < 900
    ):
        return list(_spy_history_cache['data'])

    points = []  # type: List[Dict[str, Any]]
    try:
        hist = yf.Ticker('SPY').history(
            start=start_day.isoformat(),
            end=fetch_end.isoformat(),
            auto_adjust=True,
        )
        if hist is not None and not hist.empty:
            for idx, row in hist.iterrows():
                try:
                    if hasattr(idx, 'date'):
                        d = idx.date()
                    else:
                        d = pd.Timestamp(idx).date()
                except Exception:
                    continue
                close = row.get('Close')
                if close is None or (isinstance(close, float) and pd.isna(close)):
                    continue
                points.append({
                    'date': d.isoformat(),
                    'close': float(close),
                })
    except Exception as e:
        print('Warning: SPY history fetch failed: %s' % e)

    _spy_history_cache = {
        'key': key,
        'data': points,
        'fetched_at': now_ts,
    }
    return list(points)


def get_performance_comparison(range_key: str = '1M') -> Dict[str, Any]:
    """
    Whole-account equity % vs SPY over a range toggle window.

    Window is clamped to elapsed time since algorithm_start. Both series are
    normalized to 0% at the first point in the (clamped) window — brokerage style.

    Read-only for the web dashboard — daily_close recording stays on the trader loop.
    """
    init_database()

    key = (range_key or '1M').strip().upper()
    if key not in PERFORMANCE_RANGES:
        key = '1M'
    range_days = int(PERFORMANCE_RANGES[key])

    start_raw = get_algorithm_start()
    start_day = _parse_iso_date(start_raw)
    today = _now_market().date()

    empty = {
        'ok': False,
        'range': key,
        'range_days': range_days,
        'algorithm_start': start_raw,
        'window_start': None,
        'window_end': today.isoformat(),
        'clamped': False,
        'portfolio_return_pct': None,
        'spy_return_pct': None,
        'points': [],
        'note': (
            'Whole-account equity % vs SPY since algorithm_start. '
            'Algo-only realized/unrealized stays on the scorecard.'
        ),
    }

    if not start_day:
        empty['error'] = 'algorithm_start not set — run python main.py --mark-algorithm-start'
        return empty

    # Requested window start, then clamp to algo start (cannot look before start)
    requested_start = today - timedelta(days=max(range_days - 1, 0))
    window_start = max(start_day, requested_start)
    clamped = window_start > requested_start

    equity_all = get_account_equity_daily_series(start_day, today)
    spy_all = fetch_spy_daily_closes(start_day, today)

    equity_by = {p['date']: p['equity'] for p in equity_all}
    spy_by = {p['date']: p['close'] for p in spy_all}
    win_s = window_start.isoformat()

    # Points on/after window start, plus prior close as baseline (needed for 1D).
    before = [p for p in equity_all if p['date'] < win_s]
    in_win = [p for p in equity_all if p['date'] >= win_s]
    series = []  # type: List[Dict[str, Any]]
    if before and in_win:
        series = [before[-1]] + in_win
    elif in_win:
        series = in_win
    elif before:
        series = [before[-1]]
    elif equity_all:
        series = [equity_all[-1]]

    spy_dates_sorted = sorted(spy_by.keys())

    def _spy_on_or_before(day_s: str) -> Optional[float]:
        if day_s in spy_by:
            return spy_by[day_s]
        prior = None
        for d in spy_dates_sorted:
            if d <= day_s:
                prior = spy_by[d]
            else:
                break
        return prior

    raw_points = []  # type: List[Dict[str, Any]]
    for p in series:
        day_s = p['date']
        eq = equity_by.get(day_s)
        if eq is None:
            continue
        raw_points.append({
            'date': day_s,
            'equity': eq,
            'spy': _spy_on_or_before(day_s),
        })

    if not raw_points:
        empty['ok'] = True
        empty['window_start'] = win_s
        empty['clamped'] = clamped
        empty['error'] = 'No account equity snapshots yet for this window'
        return empty

    base_equity = raw_points[0]['equity']
    base_spy = None
    for p in raw_points:
        if p.get('spy') is not None:
            base_spy = p['spy']
            break

    points = []  # type: List[Dict[str, Any]]
    for p in raw_points:
        port_pct = None
        spy_pct = None
        if base_equity and abs(base_equity) > 1e-9:
            port_pct = (float(p['equity']) / base_equity - 1.0) * 100.0
        if base_spy is not None and p.get('spy') is not None and abs(base_spy) > 1e-9:
            spy_pct = (float(p['spy']) / base_spy - 1.0) * 100.0
        points.append({
            'date': p['date'],
            'equity': p['equity'],
            'spy': p.get('spy'),
            'portfolio_pct': port_pct,
            'spy_pct': spy_pct,
        })

    port_ret = points[-1]['portfolio_pct'] if points else None
    spy_ret = points[-1]['spy_pct'] if points else None

    return {
        'ok': True,
        'range': key,
        'range_days': range_days,
        'algorithm_start': start_raw,
        'window_start': points[0]['date'] if points else win_s,
        'window_end': points[-1]['date'] if points else today.isoformat(),
        'clamped': clamped,
        'portfolio_return_pct': port_ret,
        'spy_return_pct': spy_ret,
        'baseline_equity': base_equity,
        'baseline_spy': base_spy,
        'point_count': len(points),
        'points': points,
        'note': (
            'Whole-account equity %% vs SPY. Window=%s, clamped to elapsed time since '
            'algorithm_start when shorter. Daily close snapshots + live mark.'
        ) % key,
    }


def _update_position_trail_state(
    ticker: str,
    peak_gain_pct: float,
    stop_gain_pct: Optional[float],
    trail_active: bool,
) -> None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        '''
        UPDATE positions
        SET peak_gain_pct = ?, stop_gain_pct = ?, trail_active = ?
        WHERE ticker = ?
        ''',
        (peak_gain_pct, stop_gain_pct, 1 if trail_active else 0, ticker)
    )
    conn.commit()
    conn.close()


def propose_buys(ranked: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """
    Propose $ORDER_AMOUNT_DOLLARS buys from top of ranked watchlist.
    Skips owned / pending / rebuy-debounce names; keeps proposing until cash floors block.
    Warns if cash can fund a buy but no watchlist name is eligible (loosen filter).

    Args:
        ranked: Optional ticker list (analyst-upside order). If None, uses current
                DB watchlist. Dry-run passes the filter result to simulate a refresh.
    """
    order_amount = order_amount_dollars()
    # Rank #1–N by analyst upside: skip reasons here are worth a dashboard WARN.
    warn_top_n = int(getattr(config, 'BUY_WARN_TOP_N', 5))
    if ranked is None:
        ranked = get_watchlist_ranked()
    owned = get_owned_tickers()
    pending = get_pending_buy_tickers()
    proposals = []  # type: List[Dict[str, Any]]
    account = get_account_info() or {}
    saw_buy = False
    stopped_on_cash = False
    # Dollars committed by buy proposals earlier in this pass (not yet in pending_orders).
    reserved_dollars = 0.0

    for rank_i, ticker in enumerate(ranked):
        rank = rank_i + 1
        top = rank <= max(1, warn_top_n)

        if ticker in owned:
            proposals.append({
                'action': 'skip_buy',
                'ticker': ticker,
                'reason': 'already owned',
                'rank': rank,
            })
            continue
        if ticker in pending:
            proposals.append({
                'action': 'skip_buy',
                'ticker': ticker,
                'reason': 'pending buy order',
                'rank': rank,
            })
            continue

        price = get_trade_price(ticker)
        if not price or price <= 0:
            proposals.append({
                'action': 'skip_buy',
                'ticker': ticker,
                'reason': 'no live Schwab price',
                'rank': rank,
            })
            if top:
                proposals.append({
                    'action': 'warning',
                    'ticker': ticker,
                    'reason': (
                        f"#{rank} watchlist {ticker} has no live Schwab price — "
                        f"cannot buy top analyst-upside name"
                    ),
                    'rank': rank,
                    'hint': 'schwab_price',
                })
            continue

        ok_rebuy, rebuy_reason = rebuy_allowed(ticker, price)
        if not ok_rebuy:
            proposals.append({
                'action': 'skip_buy',
                'ticker': ticker,
                'reason': rebuy_reason,
                'price': price,
                'rank': rank,
            })
            if top:
                proposals.append({
                    'action': 'warning',
                    'ticker': ticker,
                    'reason': (
                        f"#{rank} watchlist {ticker} blocked by rebuy debounce "
                        f"(@ ${price:.2f}) — {rebuy_reason}. "
                        f"Wait for cooldown or a deeper discount."
                    ),
                    'price': price,
                    'rank': rank,
                    'hint': 'rebuy_debounce',
                })
            continue

        quantity = int(order_amount / price)
        if quantity < 1:
            reason = f'price ${price:.2f} -> 0 shares for ${order_amount:.0f}'
            proposals.append({
                'action': 'skip_buy',
                'ticker': ticker,
                'reason': reason,
                'price': price,
                'rank': rank,
            })
            if top:
                proposals.append({
                    'action': 'warning',
                    'ticker': ticker,
                    'reason': (
                        f"#{rank} watchlist {ticker} too expensive for "
                        f"${order_amount:.0f} order size (@ ${price:.2f}) — "
                        f"raise ORDER_AMOUNT_DOLLARS or skip this name"
                    ),
                    'price': price,
                    'rank': rank,
                    'hint': 'order_size',
                })
            continue

        trade_dollars = quantity * price
        allowed, reason = is_trading_allowed(
            account_data=account,
            trade_amount_dollars=trade_dollars,
            extra_reserved_dollars=reserved_dollars,
        )
        if not allowed:
            proposals.append({
                'action': 'skip_buy',
                'ticker': ticker,
                'reason': reason,
                'price': price,
                'quantity': quantity,
                'dollars': trade_dollars,
                'rank': rank,
            })
            stopped_on_cash = True
            # Cash/liquidation floor: stop walking the list for further buys
            break

        proposals.append({
            'action': 'buy',
            'ticker': ticker,
            'reason': (
                f'#{rank} watchlist, ${order_amount:.0f} size, ranked by analyst upside'
            ),
            'price': price,
            'quantity': quantity,
            'dollars': trade_dollars,
            'rank': rank,
        })
        saw_buy = True
        reserved_dollars += trade_dollars
        # Treat as owned for the rest of this pass so we don't double-buy the same name
        owned.add(ticker)

    # Cash available for a full-size buy, but nothing eligible left on the watchlist
    if not saw_buy and not stopped_on_cash:
        can_fund, fund_reason = is_trading_allowed(
            account_data=account, trade_amount_dollars=order_amount
        )
        if can_fund:
            msg = (
                f"Enough cash for a ${order_amount:.0f} buy, but no eligible "
                f"watchlist names left (owned/pending/rebuy-debounce/no Schwab price). "
                f"Consider loosening the '{active_watchlist_filter()}' filter "
                f"or checking Schwab connectivity."
            )
            print(f"⚠️  {msg}")
            proposals.append({
                'action': 'warning',
                'ticker': None,
                'reason': msg,
                'hint': 'no_eligible_buys',
            })
        elif not ranked:
            msg = (
                f"Watchlist is empty. "
                f"Filter '{active_watchlist_filter()}' may be too tight "
                f"or Yahoo data needs a refresh. ({fund_reason})"
            )
            print(f"⚠️  {msg}")
            proposals.append({
                'action': 'warning',
                'ticker': None,
                'reason': msg,
                'hint': 'empty_watchlist',
            })

    return proposals


def emit_trade_pass_warnings(
    proposals: List[Dict[str, Any]],
    category: str = 'buy',
) -> None:
    """Write proposal warnings to event_log (level=warn) for the dashboard Log."""
    for p in proposals or []:
        if p.get('action') != 'warning':
            continue
        msg = str(p.get('reason') or '').strip()
        if not msg:
            continue
        # Avoid double "WARNING:" prefix in the UI tag era
        if msg.upper().startswith('WARNING:'):
            msg = msg[8:].strip()
        detail = {
            'hint': p.get('hint'),
            'ticker': p.get('ticker'),
            'rank': p.get('rank'),
            'price': p.get('price'),
        }
        log_event(category, msg, level='warn', detail=detail)


def propose_sells() -> List[Dict[str, Any]]:
    """
    Propose actions for open positions (picks up current holdings as-is):
    - sell_now: market sell immediately (trail/hard stop already breached)
    - place_stop_limit: protective stop-limit at computed stop price (not yet hit)
    - defer_stop_limit: would place stop but same ET purchase day (PDT) — wait until next day
    - skip_sell: would sell but min-hold blocks
    Trail: peak +10% activate, 10% buffer on-list / 7% off-list (ratchet up only).
    Hard stop: -15% on-list / -8% off-list (also used as stop-limit while in work mode).
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT ticker, shares_owned, average_price, peak_gain_pct, stop_gain_pct, trail_active
        FROM positions WHERE shares_owned > 0
    """)
    rows = cursor.fetchall()
    conn.close()

    proposals = []  # type: List[Dict[str, Any]]
    for ticker, shares, avg_price, peak_gain, stop_gain, trail_active in rows:
        shares = int(shares or 0)
        if shares <= 0:
            continue

        book = get_position_book(ticker)
        # After algorithm_start: only 'algorithm' book is sell-managed.
        # 'legacy' carve-outs and untagged names are skipped until enrolled.
        if get_algorithm_start() and book != 'algorithm':
            reason = (
                'legacy carve-out (not algorithm-managed)'
                if book == 'legacy'
                else 'untagged (enroll_to_algorithm to manage)'
            )
            proposals.append({
                'action': 'skip_sell',
                'ticker': ticker,
                'book': book or 'untagged',
                'shares': shares,
                'quantity': shares,
                'reason': reason,
            })
            continue

        if avg_price is None or float(avg_price) <= 0:
            proposals.append({
                'action': 'skip_sell',
                'ticker': ticker,
                'reason': 'missing average_price',
            })
            continue

        purchase = float(avg_price)
        price = get_trade_price(ticker)
        if not price or price <= 0:
            proposals.append({
                'action': 'skip_sell',
                'ticker': ticker,
                'reason': 'no live Schwab price',
            })
            continue

        state = compute_trail_state_for_position(
            ticker,
            purchase,
            float(price),
            peak_gain=peak_gain,
            stop_gain=stop_gain,
            trail_active=bool(trail_active),
        )
        peak = state['peak_gain_pct']
        new_stop = state['stop_gain_pct']
        active = state['trail_active']
        stop_kind = state['stop_kind']
        stop_price = state['stop_price']
        gain = state['gain_pct']
        on_wl = state['on_watchlist']
        buffer = float(
            getattr(config, 'TRAIL_BUFFER_OFF_WATCHLIST_PCT', 0.07)
            if not on_wl
            else getattr(config, 'TRAIL_BUFFER_PCT', 0.10)
        )

        # Persist peak/stop state so dry-runs and live checks stay consistent
        _update_position_trail_state(ticker, peak, new_stop, active)

        hold_ok, hold_reason = is_min_hold_met(ticker)
        base = {
            'ticker': ticker,
            'shares': shares,
            'quantity': shares,
            'purchase': purchase,
            'price': price,
            'gain_pct': gain,
            'peak_gain_pct': peak,
            'stop_gain_pct': new_stop,
            'stop_price': stop_price,
            'stop_kind': stop_kind,
            'trail_active': active,
            'on_watchlist': on_wl,
        }

        # Immediate market sell if price already at/through the stop
        if state['breached']:
            if not hold_ok:
                reason = f'{stop_kind} stop hit but {hold_reason}'
                proposals.append(dict(base, action='skip_sell', reason=reason))
                proposals.append({
                    'action': 'warning',
                    'ticker': ticker,
                    'reason': (
                        f"{ticker} stop breached ({stop_kind} @ ${stop_price:.2f}) but "
                        f"same-day PDT rule blocks the market sell — {hold_reason}. "
                        f"Broker STOP_LIMIT is not armed until the next ET calendar day."
                    ),
                    'hint': 'min_hold_blocks_exit',
                    'stop_price': stop_price,
                    'stop_kind': stop_kind,
                })
            else:
                exit_info = describe_sell_exit(stop_kind, active)
                proposals.append(dict(
                    base,
                    action='sell_now',
                    exit_kind=exit_info['exit_kind'],
                    reason=(
                        f"{exit_info['short_label']}: {exit_info['summary']}; "
                        f'gain {gain*100:.1f}% <= stop {float(new_stop)*100:.1f}% '
                        f'(@ ${stop_price:.2f}); '
                        f'{"off" if not on_wl else "on"} watchlist — market sell now'
                    ),
                ))
            continue

        # Same ET calendar day as purchase: never arm broker stop (PDT).
        if not hold_ok:
            proposals.append(dict(
                base,
                action='defer_stop_limit',
                reason=(
                    f'defer stop-limit @ ${stop_price:.2f} until next ET day — {hold_reason}'
                ),
            ))
            continue

        # Not hit yet: place / refresh stop-limit below market
        if active:
            reason = (
                f'place stop-limit @ ${stop_price:.2f} ({float(new_stop)*100:.1f}% vs cost); '
                f'trail armed peak {peak*100:.1f}%, buffer {buffer*100:.0f}%, '
                f'gain now {gain*100:.1f}%, {"off" if not on_wl else "on"} watchlist'
            )
        else:
            activate_pct = float(getattr(config, 'TRAIL_ACTIVATE_PCT', 0.10))
            reason = (
                f'place stop-limit @ ${stop_price:.2f} ({float(new_stop)*100:.1f}% hard stop); '
                f'work mode gain {gain*100:.1f}% < activate {activate_pct*100:.0f}%, '
                f'{"off" if not on_wl else "on"} watchlist'
            )
        proposals.append(dict(base, action='place_stop_limit', reason=reason))

    return proposals


def propose_trades() -> Dict[str, List[Dict[str, Any]]]:
    """What the system would buy and sell right now, with reasons (no orders)."""
    init_database()
    refresh_schwab_positions_if_needed(force=True)
    record_account_snapshot(note='propose_trades')
    buys = propose_buys()
    sells = propose_sells()
    return {'buys': buys, 'sells': sells}


def print_trade_proposals(proposals: Optional[Dict[str, List[Dict[str, Any]]]] = None) -> None:
    """Pretty-print buy/sell proposals."""
    if proposals is None:
        proposals = propose_trades()
    print("=" * 60)
    print("Trade proposals (dry-run view)")
    print("=" * 60)
    print("\nBUYS:")
    buys = proposals.get('buys') or []
    if not buys:
        print("  (none)")
    for p in buys:
        if p.get('action') == 'warning':
            print(f"  ⚠️  {p.get('reason')}")
            continue
        extras = []
        if p.get('quantity'):
            extras.append(f"qty={p['quantity']}")
        if p.get('price'):
            extras.append(f"px=${p['price']:.2f}")
        if p.get('dollars'):
            extras.append(f"${p['dollars']:.2f}")
        extra = f" ({', '.join(extras)})" if extras else ""
        print(f"  [{p.get('action')}] {p.get('ticker')}: {p.get('reason')}{extra}")

    print("\nSELLS / STOP-LIMITS:")
    sells = proposals.get('sells') or []
    if not sells:
        print("  (none)")
    for p in sells:
        gain = p.get('gain_pct')
        gain_s = f" gain={gain*100:.1f}%" if gain is not None else ""
        stop_s = ""
        if p.get('stop_price') is not None:
            stop_s = f" stop=${p['stop_price']:.2f}"
        print(f"  [{p.get('action')}] {p.get('ticker')}: {p.get('reason')}{gain_s}{stop_s}")
    print()


def run_buy_pass(dry_run: Optional[bool] = None) -> List[Dict[str, Any]]:
    """Propose buys and optionally execute each actionable buy (until cash floors block)."""
    if dry_run is None:
        dry_run = trade_dry_run_enabled()
    sync = refresh_schwab_positions_if_needed(force=True)
    if not sync.get('ok'):
        msg = (
            f"Buy pass skipped: Schwab position sync failed "
            f"({sync.get('reason')}) — refusing stale owned/cash data"
        )
        print(f"  {msg}")
        log_event('task', msg, level='error', detail=sync)
        return [{
            'action': 'skip_buy',
            'ticker': None,
            'reason': msg,
        }]
    proposals = propose_buys()
    emit_trade_pass_warnings(proposals, category='task')
    for p in proposals:
        if p.get('action') == 'warning':
            print(f"  ⚠️  {p.get('reason')}")
            continue
        print(f"  [{p.get('action')}] {p.get('ticker')}: {p.get('reason')}")
        if p.get('action') == 'buy' and not dry_run:
            execute_buy(p['ticker'], int(p['quantity']))
        elif p.get('action') == 'buy' and dry_run:
            qty = p.get('quantity')
            tkr = p.get('ticker')
            px = p.get('price')
            msg = format_bought_log_message(tkr, qty, px, dry_run=True)
            print(f"    ({msg})")
            log_event(
                'buy',
                msg,
                detail={
                    'ticker': tkr,
                    'quantity': qty,
                    'price': px,
                    'dollars': p.get('dollars'),
                    'reason': p.get('reason'),
                    'phase': 'filled',
                    'dry_run': True,
                },
            )
            if tkr:
                clear_rebuy_guard(str(tkr))
    return proposals


def run_sell_pass(dry_run: Optional[bool] = None) -> List[Dict[str, Any]]:
    """Propose sells/stop-limits and optionally execute immediate sells / broker stops."""
    if dry_run is None:
        dry_run = trade_dry_run_enabled()
    sync = refresh_schwab_positions_if_needed(force=True)
    if not sync.get('ok'):
        msg = (
            f"Sell pass skipped: Schwab position sync failed "
            f"({sync.get('reason')}) — refusing stale shares/marks"
        )
        print(f"  {msg}")
        log_event('task', msg, level='error', detail=sync)
        return [{
            'action': 'skip_sell',
            'ticker': None,
            'reason': msg,
        }]
    proposals = propose_sells()
    emit_trade_pass_warnings(proposals, category='task')
    for p in proposals:
        if p.get('action') == 'warning':
            print(f"  ⚠️  {p.get('reason')}")
            continue
        print(f"  [{p.get('action')}] {p.get('ticker')}: {p.get('reason')}")
        ticker = p.get('ticker')
        if p.get('action') == 'sell_now':
            exit_info = describe_sell_exit(p.get('stop_kind'), bool(p.get('trail_active')))
            exit_kind = p.get('exit_kind') or exit_info['exit_kind']
            qty = int(p.get('quantity') or 0)
            # execute_sell logs SOLD (or dry-run SOLD)
            if dry_run:
                execute_sell(
                    ticker,
                    qty,
                    note=p.get('reason'),
                    exit_kind=exit_kind,
                )
                continue
            try:
                cancel_position_broker_stop(ticker)
            except Exception as e:
                print(f"  Warning: cancel stop before sell_now failed for {ticker}: {e}")
            execute_sell(
                ticker,
                qty,
                note=p.get('reason'),
                exit_kind=exit_kind,
            )
        elif p.get('action') == 'defer_stop_limit':
            # Same-day purchase: cancel any resting stop; log once until it is placed.
            stop_px = p.get('stop_price')
            already = _stop_defer_already_logged(ticker)
            if not dry_run:
                try:
                    cancel_position_broker_stop(ticker)
                except Exception as e:
                    print(f"  Warning: cancel deferred stop for {ticker}: {e}")
            if already:
                continue
            if stop_px is None:
                continue
            log_stop_limit_event(
                ticker,
                float(stop_px),
                deferred=True,
                dry_run=bool(dry_run),
                extra={
                    'quantity': p.get('quantity'),
                    'reason': p.get('reason'),
                },
            )
            _set_stop_defer_logged(ticker, True)
        elif p.get('action') == 'place_stop_limit' and dry_run and not trade_dry_run_enabled():
            # Forced dry-run while live mode is on — log only, do not submit.
            stop_px = p.get('stop_price')
            if stop_px is None:
                continue
            stored = get_position_stop_order(ticker)
            _emit_stop_limit_placed_or_moved(
                ticker,
                float(stop_px),
                previous_stop=stored.get('stop_order_price'),
                extra={
                    'quantity': p.get('quantity'),
                    'reason': p.get('reason'),
                    'dry_run': True,
                },
            )
        elif p.get('action') == 'place_stop_limit':
            result = ensure_broker_stop_limit(
                ticker,
                int(p['quantity']),
                float(p['stop_price']),
                spot_price=p.get('price'),
            )
            action = result.get('action')
            if action == 'unchanged':
                print(
                    f"    (live: broker stop unchanged "
                    f"${result.get('stop_price'):.2f} id={result.get('order_id')})"
                )
            elif action in ('placed', 'replaced'):
                print(
                    f"    (live: STOP_LIMIT {action} "
                    f"stop=${result.get('stop_price'):.2f} "
                    f"limit=${result.get('limit_price'):.2f} "
                    f"id={result.get('order_id')})"
                )
            elif action == 'skipped':
                print(f"    (live: skipped broker stop — {result.get('reason')})")
            else:
                print(
                    f"    (live: broker stop {action} — {result.get('reason')}; "
                    f"DB trail still tracked; sell_now is backup if breached)"
                )
    return proposals


def dry_run_system() -> Dict[str, Any]:
    """
    One-pass dry-run of the full active system *right now*.

    Walks the same jobs as run_trader(once=True), but:
    - Never places orders
    - Does not run the long Yahoo populate (only reports if it would)
    - Does not mutate watchlist via Schwab (reports filter preview instead)
    - Shows buys, immediate sells, and stop-limit placements for current positions

    Active mode (run_trader) keeps looping on periodic intervals; this is a snapshot.
    """
    init_database()
    now = datetime.now()
    report = {
        'ts': now.isoformat(),
        'jobs': [],
        'buys': [],
        'sells': [],
        'filter_preview': {},
    }  # type: Dict[str, Any]

    print("=" * 60)
    print(f"DRY RUN SYSTEM (one pass) — {now.isoformat()}")
    print("No orders will be placed. Yahoo hourly batch will NOT be executed.")
    print("=" * 60)

    sync = refresh_schwab_positions_if_needed(force=True)
    if sync.get('ok'):
        print(
            f"\n--- Positions sync ---"
            f"\n  Schwab positions refreshed ({sync.get('count')} rows)"
        )
    else:
        print(
            f"\n--- Positions sync ---"
            f"\n  Warning: sync failed ({sync.get('reason')}) — marks may be stale"
        )

    account = get_account_info() or {}
    pending = get_pending_orders_total_dollars()
    cash = float(account.get('cash') or 0.0)
    effective_cash = cash - pending
    min_cash = minimum_cash()
    order_amount = order_amount_dollars()
    # Cash that can go to new buys while staying at/above the cash floor
    spendable = max(0.0, effective_cash - min_cash)
    buys_affordable = int(spendable // order_amount) if order_amount > 0 else 0
    print("\n--- Account ---")
    print(f"  Cash: ${cash:,.2f}")
    print(f"  Effective cash (cash - pending ${pending:,.2f}): ${effective_cash:,.2f}")
    print(f"  Cash floor (MINIMUM_CASH): ${min_cash:,.2f}")
    print(f"  Spendable on new stocks: ${spendable:,.2f} "
          f"(effective cash − floor)")
    print(f"  ≈ {buys_affordable} buy(s) at ~${order_amount:,.0f} each "
          f"(ORDER_AMOUNT_DOLLARS)")
    print(f"  Liquidation: ${float(account.get('liquidation_value') or 0):,.2f}")
    print(f"  TRADE_DRY_RUN={trade_dry_run_enabled()} "
          f"({'no Schwab orders' if trade_dry_run_enabled() else 'LIVE — orders will submit'})")

    # Algorithm era / books
    algo_start = get_algorithm_start()
    books = list_position_books()
    print("\n--- Algorithm / books ---")
    if algo_start:
        print(f"  ALGORITHM_START: {algo_start}")
        print(f"  Enrolled under sell rules ({len(books.get('enrolled') or [])}): "
              f"{books.get('enrolled') or []}")
        if books['legacy']:
            print(f"  Legacy carve-outs ({len(books['legacy'])}): {books['legacy']}")
        algo_buys = books.get('algo_buys') or []
        print(f"  Algo buys on scorecard ({len(algo_buys)}): {algo_buys}")
        if books['untagged']:
            print(f"  Untagged ({len(books['untagged'])}): {books['untagged']}")
        perf = get_algorithm_performance()
        report['algorithm'] = perf
        sc = perf.get('scorecard') or {}
        print(
            f"  ALGO SCORECARD (post-start buys only): "
            f"total P/L ${float(sc.get('total_pl') or 0):+,.2f} "
            f"(realized ${float(sc.get('realized_pl') or 0):+,.2f} + "
            f"unrealized ${float(sc.get('unrealized_pl') or 0):+,.2f})"
        )
        print(
            f"    Deployed buys ${float(sc.get('buy_dollars') or 0):,.2f} | "
            f"sells ${float(sc.get('sell_dollars') or 0):,.2f} | "
            f"trades {sc.get('trade_count')} | "
            f"open MV ${float(sc.get('open_market_value') or 0):,.2f}"
        )
        print(
            f"  Enrolled (managed, not on scorecard) MV "
            f"${float(perf.get('enrolled_book_market_value') or 0):,.2f}"
        )
        if perf.get('legacy_book_market_value'):
            print(
                f"  Legacy MV ${float(perf.get('legacy_book_market_value') or 0):,.2f}"
            )
        ed = perf.get('account_equity_delta_since_start')
        ep = perf.get('equity_delta_pct')
        if ed is not None:
            pct_s = f" ({ep:+.2f}%)" if ep is not None else ""
            print(f"  Account equity vs start (context): ${ed:+,.2f}{pct_s}")
    else:
        print("  ALGORITHM_START: not set")
        print("  When ready: python main.py --mark-algorithm-start")
        print("  Soft reset: enroll all holdings + seed stops; scorecard = new buys only.")
        books = list_position_books()
        print(f"  Already enrolled ({len(books.get('enrolled') or [])}): "
              f"{books.get('enrolled') or []}")
        report['algorithm'] = {'algorithm_start': None, 'books': books}

    market_open = is_us_equity_market_open()
    print("\n--- Scheduled jobs (what active mode would do this pass) ---")
    print(f"  US RTH now: {'OPEN' if market_open else 'CLOSED'}")
    if not market_open:
        print(f"  Next open: {next_us_equity_market_open().isoformat()}")
    for job_name, _fn, interval_days in get_scheduled_jobs():
        due = is_job_due(job_name, interval_days)
        due_at = next_job_due_at(job_name, interval_days)
        row = get_job_run(job_name)
        last_completed = row.get('last_completed') if row else None
        label = format_job_interval(interval_days)
        needs_rth = job_name in JOBS_REQUIRE_MARKET_OPEN
        blocked_closed = needs_rth and not market_open
        would_run = due and not blocked_closed
        entry = {
            'job_name': job_name,
            'interval_days': interval_days,
            'interval_label': label,
            'requires_market_open': needs_rth,
            'due_now': due,
            'would_run_now': would_run,
            'last_completed': last_completed,
            'next_due': due_at.isoformat() if due_at else None,
        }
        report['jobs'].append(entry)
        if blocked_closed:
            print(f"  skip (market closed): {job_name} (every {label}); "
                  f"last_completed={last_completed}")
        elif would_run:
            print(f"  WOULD RUN NOW: {job_name} (every {label}); "
                  f"last_completed={last_completed}")
        else:
            print(f"  skip (not due): {job_name} (every {label}) — "
                  f"next {due_at.isoformat() if due_at else '?'} "
                  f"(last_completed={last_completed})")

    # Job 1 detail: Yahoo refresh
    print("\n--- 1) Yahoo market data refresh ---")
    yahoo_due = is_job_due(JOB_REFRESH_MARKET_DATA, _market_data_job_interval_days())
    yahoo_job = get_job_run(JOB_REFRESH_MARKET_DATA)
    batch = int(getattr(config, 'YAHOO_BATCH_SIZE', 150))
    sla_days = int(getattr(config, 'MARKET_DATA_REFRESH_DAYS', 7))
    hours = float(getattr(config, 'MARKET_DATA_JOB_INTERVAL_HOURS', 1))
    if yahoo_job and yahoo_job.get('progress_note'):
        print(f"  Last progress: {yahoo_job.get('progress_note')}")
        print(f"  Status: {yahoo_job.get('status')}")
    print(f"  Config: every {hours:g}h, batch={batch}, SLA={sla_days}d (oldest-first)")
    if yahoo_due:
        print(f"  Active mode WOULD run refresh_market_data() (~{batch} tickers)")
        print("  (skipped in this dry-run — data used is current DB snapshot)")
    else:
        print("  Not due — would NOT refresh Yahoo this pass; using existing fundamentals")

    # Job 2 detail: watchlist + buys
    print("\n--- 2) Watchlist update + buys ---")
    wl_due = is_job_due(JOB_WATCHLIST_AND_BUYS, _watchlist_job_interval_days())
    filter_name = active_watchlist_filter()
    wl_would = wl_due and market_open
    if wl_would:
        print(
            f"  Active mode WOULD update_watchlist(filter={filter_name}) then buy pass "
            f"(every {format_job_interval(_watchlist_job_interval_days())}, RTH only)"
        )
    elif not market_open:
        print("  Market closed — watchlist/buys would NOT run; filter preview still shown")
    else:
        print("  Watchlist job not due — buy pass still shown from CURRENT watchlist state")

    # Full filter preview (no limit) — does not rewrite Schwab/DB watchlist
    print(f"  Filter preview ({filter_name}, ALL matches by analyst upside):")
    if filter_name == 'risky':
        preview = risky_filter_stocks(limit=None)
    else:
        preview = safe_filter_stocks(limit=None)
    current_wl = set(get_watchlist_ranked())
    would_add = [t for t in preview if t not in current_wl]
    report['filter_preview'] = {
        'filter_name': filter_name,
        'tickers': preview,
        'would_add': would_add,
        'current_watchlist_count': len(current_wl),
    }
    print(f"    match count: {len(preview)}")
    print(f"    all matches: {preview}")
    print(f"    not currently on watchlist ({len(would_add)}): {would_add}")

    # If watchlist job would run, buys use the NEW filter ranking (what update_watchlist would load).
    # Otherwise walk the current DB watchlist (job would not refresh yet).
    if wl_would:
        print("\n  Buy proposals (as if watchlist refreshed from filter above):")
        buys = propose_buys(ranked=preview)
    else:
        print("\n  Buy proposals (from current ranked watchlist — job would not refresh this pass):")
        buys = propose_buys()
    report['buys'] = buys
    actionable_buys = [b for b in buys if b.get('action') == 'buy']
    if not buys:
        print("    (none)")
    for p in buys:
        line = f"    [{p.get('action')}] {p.get('ticker')}: {p.get('reason')}"
        if p.get('action') == 'buy':
            line += f" — WOULD BUY {p.get('quantity')} @ ${p.get('price'):.2f} (${p.get('dollars'):.2f})"
        print(line)
    if not actionable_buys:
        print("    => No new buy this pass")

    # Job 3 detail: sells / stops on current positions
    print("\n--- 3) Sell check (current positions) ---")
    sell_due = is_job_due(JOB_SELL_CHECK, _sell_check_interval_days())
    sell_would = sell_due and market_open
    if sell_would:
        print(
            f"  Active mode WOULD run sell check now "
            f"(every {format_job_interval(_sell_check_interval_days())}, RTH only)"
        )
    elif not market_open:
        print("  Market closed — sell-check job would NOT run; positions still evaluated below")
    else:
        print("  Sell-check job not due on timer — still evaluating positions for this dry-run")

    sells = propose_sells()
    report['sells'] = sells
    sell_now = [s for s in sells if s.get('action') == 'sell_now']
    place_stop = [s for s in sells if s.get('action') == 'place_stop_limit']
    defer_stop = [s for s in sells if s.get('action') == 'defer_stop_limit']
    skipped = [s for s in sells if s.get('action') == 'skip_sell']

    print(f"\n  Immediate MARKET SELLS ({len(sell_now)}):")
    if not sell_now:
        print("    (none)")
    for p in sell_now:
        print(f"    [{p.get('ticker')}] {p.get('reason')}")
        print(f"      WOULD SELL {p.get('quantity')} shares now @ ~${p.get('price'):.2f}")

    print(f"\n  STOP-LIMIT orders to place/refresh ({len(place_stop)}):")
    if not place_stop:
        print("    (none)")
    for p in place_stop:
        print(f"    [{p.get('ticker')}] {p.get('reason')}")
        print(
            f"      WOULD PLACE stop-limit sell {p.get('quantity')} sh "
            f"@ ${p.get('stop_price'):.2f} (spot ${p.get('price'):.2f}, "
            f"cost ${p.get('purchase'):.2f})"
        )

    print(f"\n  STOP-LIMIT deferred until next ET day ({len(defer_stop)}):")
    if not defer_stop:
        print("    (none)")
    for p in defer_stop:
        print(f"    [{p.get('ticker')}] {p.get('reason')}")

    legacy_skips = [
        s for s in skipped
        if 'holdout' in (s.get('reason') or '')
    ]
    other_skips = [s for s in skipped if s not in legacy_skips]
    if legacy_skips:
        print(f"\n  Legacy / holdout (sell-skipped) ({len(legacy_skips)}):")
        for p in legacy_skips:
            print(f"    [{p.get('ticker')}] book={p.get('book')} — {p.get('reason')}")
    if other_skips:
        print(f"\n  Blocked by same-day PDT / data ({len(other_skips)}):")
        for p in other_skips:
            print(f"    [{p.get('ticker')}] {p.get('reason')}")

    print("\n--- Dry-run summary ---")
    print(f"  Yahoo refresh this pass: {'YES (would run)' if yahoo_due else 'no'} "
          f"(may run when market closed)")
    print(f"  Watchlist+buy this pass: {'YES (would run)' if wl_would else 'no'} "
          f"(RTH only, every {format_job_interval(_watchlist_job_interval_days())})")
    print(f"  Sell-check this pass: {'YES (would run)' if sell_would else 'no'} "
          f"(RTH only, every {format_job_interval(_sell_check_interval_days())})")
    print(f"  Buys to place: {len(actionable_buys)}")
    print(f"  Market sells now: {len(sell_now)}")
    print(f"  Stop-limits to place: {len(place_stop)}")
    print(f"  Stop-limits deferred (same day): {len(defer_stop)}")
    print("\nActive mode: run_trader(once=False); RTH jobs reset once at each market open.")
    print("Dry-run complete.\n")

    record_account_snapshot(note='dry_run_system')
    return report


def run_watchlist_and_buys_job() -> bool:
    """RTH job: refresh watchlist from filter, then buy pass (respects TRADE_DRY_RUN)."""
    init_database()
    if not is_us_equity_market_open():
        print(f"Job {JOB_WATCHLIST_AND_BUYS}: skipped — market closed")
        return False
    interval = _watchlist_job_interval_days()
    filter_name = active_watchlist_filter()
    mark_job_started(JOB_WATCHLIST_AND_BUYS, interval)
    try:
        sync = refresh_schwab_positions_if_needed(force=True)
        if not sync.get('ok'):
            print(
                f"Warning: Schwab position sync before watchlist/buys failed "
                f"({sync.get('reason')})"
            )
        print(f"Updating watchlist (filter={filter_name})...")
        stats = update_watchlist(filter_name=filter_name)
        print(f"Watchlist: {stats}")
        log_event(
            'watchlist',
            f"Watchlist updated (filter={filter_name}): {stats}",
            detail={'filter': filter_name, 'stats': stats},
        )
        if buys_paused():
            print("Buy pass skipped — buys_paused flag is set")
            log_event('task', 'Buy pass skipped (buys_paused)')
        else:
            print("Buy pass...")
            buys = run_buy_pass()
            n_buy = len([b for b in (buys or []) if b.get('action') == 'buy'])
            log_event(
                'task',
                f"Buy pass finished ({'dry-run' if trade_dry_run_enabled() else 'live'}); "
                f"{n_buy} buy action(s)",
                detail={'count': n_buy},
            )
        record_account_snapshot(note='watchlist_and_buys')
        mark_job_completed(JOB_WATCHLIST_AND_BUYS)
        return True
    except Exception as e:
        mark_job_failed(JOB_WATCHLIST_AND_BUYS)
        log_event('task', f"Watchlist/buys task failed: {e}", level='error')
        print(f"Job {JOB_WATCHLIST_AND_BUYS} failed: {e}")
        return False


def run_schwab_sync_job() -> bool:
    """RTH job: reconcile pending buys + refresh positions / stop-limit exits."""
    init_database()
    if not is_us_equity_market_open():
        print(f"Job {JOB_SCHWAB_SYNC}: skipped — market closed")
        return False
    interval = _schwab_sync_interval_days()
    mark_job_started(JOB_SCHWAB_SYNC, interval)
    try:
        # Pick up tokens written by dashboard OAuth without restarting the loop
        if not SCHWAB_AVAILABLE:
            maybe_reinit_schwab_client()
        auth = get_schwab_auth_status()
        maybe_log_schwab_auth_alerts(auth)
        maybe_log_schwab_access_refresh()
        print("Schwab account sync (pending orders + positions)...")
        result = sync_schwab_account(force_positions=True)
        cleared = int(result.get('pending_cleared') or 0)
        pos = result.get('positions') or {}
        log_event(
            'task',
            f"Schwab sync finished: pending_cleared={cleared}, "
            f"positions_synced={bool(pos.get('synced'))}",
            detail={'pending_cleared': cleared, 'positions': pos},
        )
        mark_job_completed(JOB_SCHWAB_SYNC)
        return True
    except Exception as e:
        mark_job_failed(JOB_SCHWAB_SYNC)
        log_event('task', f"Schwab sync task failed: {e}", level='error')
        print(f"Job {JOB_SCHWAB_SYNC} failed: {e}")
        return False


def run_sell_check_job() -> bool:
    """RTH job: trail / hard-stop sell pass (respects TRADE_DRY_RUN)."""
    init_database()
    if not is_us_equity_market_open():
        print(f"Job {JOB_SELL_CHECK}: skipped — market closed")
        return False
    interval = _sell_check_interval_days()
    mark_job_started(JOB_SELL_CHECK, interval)
    try:
        print("Sell check pass...")
        sells = run_sell_pass()
        n_now = len([s for s in (sells or []) if s.get('action') == 'sell_now'])
        n_stop = len([s for s in (sells or []) if s.get('action') == 'place_stop_limit'])
        n_defer = len([s for s in (sells or []) if s.get('action') == 'defer_stop_limit'])
        log_event(
            'task',
            f"Sell check finished: sell_now={n_now}, stop_limit={n_stop}, "
            f"defer_stop={n_defer} "
            f"({'dry-run' if trade_dry_run_enabled() else 'live'})",
            detail={
                'sell_now': n_now,
                'place_stop_limit': n_stop,
                'defer_stop_limit': n_defer,
            },
        )
        record_account_snapshot(note='sell_check')
        mark_job_completed(JOB_SELL_CHECK)
        return True
    except Exception as e:
        mark_job_failed(JOB_SELL_CHECK)
        log_event('task', f"Sell check task failed: {e}", level='error')
        print(f"Job {JOB_SELL_CHECK} failed: {e}")
        return False


def monitor_and_trade():
    """
    Legacy helper: print current trade proposals (dry-run).
    Prefer run_trader(), propose_trades(), or run_buy_pass / run_sell_pass.
    """
    print_trade_proposals(propose_trades())


# ============================================================================
# Web dashboard data (JSON for FastAPI)
# ============================================================================

def get_yahoo_staleness_summary() -> Dict[str, Any]:
    """Lightweight SLA stats from fundamentals (no SEC download)."""
    init_database()
    current_date = datetime.now().strftime('%Y-%m-%d')
    max_age_days = int(getattr(config, 'MARKET_DATA_REFRESH_DAYS', 7))
    conn = get_fundamentals_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM fundamentals')
    in_db = int(cursor.fetchone()[0] or 0)
    cursor.execute(
        '''
        SELECT COUNT(*) FROM fundamentals
        WHERE last_updated IS NOT NULL
          AND substr(last_updated, 1, 10) = ?
        ''',
        (current_date,)
    )
    updated_today = int(cursor.fetchone()[0] or 0)
    cursor.execute(
        '''
        SELECT COUNT(*) FROM fundamentals
        WHERE last_updated IS NULL
           OR length(trim(last_updated)) = 0
           OR julianday(?) - julianday(substr(last_updated, 1, 10)) >= ?
        ''',
        (current_date, float(max_age_days))
    )
    overdue = int(cursor.fetchone()[0] or 0)
    conn.close()
    universe = get_runtime_flag('yahoo_universe_size')
    try:
        universe_n = int(universe) if universe else in_db
    except (TypeError, ValueError):
        universe_n = in_db
    return {
        'sla_days': max_age_days,
        'in_db': in_db,
        'universe_size': universe_n,
        'updated_today': updated_today,
        'overdue_or_stale': overdue,
        'within_sla_est': max(in_db - overdue, 0),
    }


def _watchlist_filter_catalog() -> Dict[str, Dict[str, Any]]:
    """
    Human-readable descriptions for every built-in watchlist filter.

    Each criterion lists: field, meaning, set_to (threshold), and why.
    Thresholds mirror the defaults of safe_filter_stocks / risky_filter_stocks.
    """
    safe = {
        'name': 'safe',
        'title': 'Safe Giant',
        'summary': (
            'Mega-cap, relatively stable names with value characteristics, '
            'analyst upside, and a modest dividend — ranked by analyst upside.'
        ),
        'ranking': 'Analyst upside ((target_price − current_price) / current_price), highest first',
        'criteria': [
            {
                'field': 'market_cap',
                'meaning': 'Total market value of the company (shares outstanding × price)',
                'set_to': '> $25B',
                'why': 'Stick to giant / mega-cap firms that tend to be more liquid and durable',
            },
            {
                'field': 'beta',
                'meaning': 'Volatility vs the broad market (1.0 ≈ moves with the market)',
                'set_to': '0.0 – 1.3',
                'why': 'Cap high-volatility names; low beta is allowed for safety and diversity',
            },
            {
                'field': 'short_float',
                'meaning': 'Share of float sold short (0.10 = 10%)',
                'set_to': '< 10%',
                'why': 'Avoid crowded short interest (“no enemies”) that can spike volatility',
            },
            {
                'field': 'pe_ratio',
                'meaning': 'Price-to-earnings (price per share / earnings per share)',
                'set_to': '0.0 – 35.0',
                'why': 'Require profitability (P/E > 0) while excluding richly priced names',
            },
            {
                'field': 'peg_ratio',
                'meaning': 'P/E divided by expected earnings growth (growth at a reasonable price)',
                'set_to': '0.0 – 2.0',
                'why': 'Prefer growth that is not overpaying; PEG data is required',
            },
            {
                'field': 'analyst_upside',
                'meaning': 'Implied upside from current price to consensus analyst target',
                'set_to': '> 10%',
                'why': 'Even “safe” names should still have meaningful expected appreciation',
            },
            {
                'field': 'recommendation_mean',
                'meaning': 'Analyst consensus score (1=Strong Buy … 5=Sell)',
                'set_to': '≤ 2.0',
                'why': 'Bias toward Buy / Strong Buy; recommendation data is required',
            },
            {
                'field': 'dividend_yield',
                'meaning': 'Annual dividend yield in percentage points (1.0 = 1%)',
                'set_to': '> 1%',
                'why': 'Prefer income support for longer holds while waiting on upside',
            },
        ],
        'disabled': [
            {
                'field': 'fifty_day_average > two_hundred_day_average',
                'meaning': 'Classic “golden cross” uptrend (50-day MA above 200-day MA)',
                'set_to': 'disabled',
                'why': 'Turned off as noisy — too many false signals for this strategy',
            },
        ],
    }

    risky = {
        'name': 'risky',
        'title': 'Risky Momentum',
        'summary': (
            'Liquid mid/large caps with high beta and short interest — '
            'short-squeeze / momentum candidates ranked by analyst upside.'
        ),
        'ranking': 'Analyst upside ((target_price − current_price) / current_price), highest first',
        'criteria': [
            {
                'field': 'market_cap',
                'meaning': 'Total market value of the company (shares outstanding × price)',
                'set_to': '> $2B',
                'why': 'Stay above micro-caps for basic size and liquidity safety',
            },
            {
                'field': 'avg_volume',
                'meaning': 'Average daily trading volume in shares',
                'set_to': '> 1M shares/day',
                'why': 'Need enough liquidity to enter and exit without huge slippage',
            },
            {
                'field': 'current_price',
                'meaning': 'Latest Yahoo Finance price',
                'set_to': '> $5',
                'why': 'Exclude penny stocks that are hard to trade cleanly',
            },
            {
                'field': 'beta',
                'meaning': 'Volatility vs the broad market (1.0 ≈ moves with the market)',
                'set_to': '> 1.5',
                'why': 'Want names that move more than the market (action / momentum)',
            },
            {
                'field': 'short_float',
                'meaning': 'Share of float sold short (0.15 = 15%)',
                'set_to': '> 15%',
                'why': 'High short interest is the squeeze-potential signal',
            },
            {
                'field': 'analyst_upside',
                'meaning': 'Implied upside from current price to consensus analyst target',
                'set_to': '> 20%',
                'why': 'Require a stronger value / upside case to justify the risk',
            },
        ],
        'disabled': [],
    }

    return {'safe': safe, 'risky': risky}


def get_watchlist_filter_description(filter_name: Optional[str] = None) -> Dict[str, Any]:
    """Return one filter description (defaults to this user's active filter)."""
    name = filter_name or active_watchlist_filter()
    name = str(name).strip().lower()
    catalog = _watchlist_filter_catalog()
    desc = catalog.get(name)
    if desc is None:
        return {
            'name': name,
            'title': name,
            'summary': 'Unknown filter configured for this user.',
            'ranking': None,
            'criteria': [],
            'disabled': [],
        }
    return desc


def get_watchlist_filter_dashboard() -> Dict[str, Any]:
    """Dashboard payload: active filter + full catalog for the UI dropdown (view-only)."""
    catalog = _watchlist_filter_catalog()
    active = str(active_watchlist_filter()).strip().lower()
    # Stable UI order: safe first, then risky, then any future filters
    order = ['safe', 'risky']
    filters = [catalog[k] for k in order if k in catalog]
    for k in sorted(catalog.keys()):
        if k not in order:
            filters.append(catalog[k])
    return {
        'active': active,
        'count': len(filters),
        'filters': filters,
        # Convenience: active filter object (also first paint without JS lookup)
        'selected': catalog.get(active) or get_watchlist_filter_description(active),
    }


def get_trading_rules_dashboard() -> Dict[str, Any]:
    """
    Ordered list of trading rules for the Home page (basic → advanced).
    Values are pulled live from config so the UI stays in sync.
    """
    dry = trade_dry_run_enabled()
    min_cash = minimum_cash()
    min_liq = minimum_liquidation_value()
    order_amt = order_amount_dollars()
    filter_name = str(active_watchlist_filter())
    rebuy_days = int(getattr(config, 'REBUY_COOLDOWN_TRADING_DAYS', 5))
    rebuy_pct = float(getattr(config, 'REBUY_DISCOUNT_PCT', 0.05)) * 100.0
    trail_act = float(getattr(config, 'TRAIL_ACTIVATE_PCT', 0.10)) * 100.0
    trail_buf = float(getattr(config, 'TRAIL_BUFFER_PCT', 0.10)) * 100.0
    trail_off = float(getattr(config, 'TRAIL_BUFFER_OFF_WATCHLIST_PCT', 0.07)) * 100.0
    hard_on = abs(float(getattr(config, 'HARD_STOP_ON_WATCHLIST_PCT', -0.15))) * 100.0
    hard_off = abs(float(getattr(config, 'HARD_STOP_OFF_WATCHLIST_PCT', -0.08))) * 100.0
    slip = float(getattr(config, 'STOP_LIMIT_SLIPPAGE_PCT', 0.005)) * 100.0
    sell_mins = int(getattr(config, 'SELL_CHECK_INTERVAL_MINUTES', 15))
    open_h = int(getattr(config, 'MARKET_OPEN_HOUR', 9))
    open_m = int(getattr(config, 'MARKET_OPEN_MINUTE', 30))
    close_h = int(getattr(config, 'MARKET_CLOSE_HOUR', 16))
    close_m = int(getattr(config, 'MARKET_CLOSE_MINUTE', 0))
    tz = str(getattr(config, 'MARKET_TIMEZONE', 'America/New_York'))
    session = '%d:%02d–%d:%02d %s' % (open_h, open_m, close_h, close_m, tz)

    rules = [
        {
            'id': 'dry_run',
            'title': 'Dry-run safety net',
            'set_to': 'ON — no Schwab orders' if dry else 'OFF — live Schwab orders',
            'why': 'Paper-trade by default; flip TRADE_DRY_RUN only when ready for real fills',
        },
        {
            'id': 'market_hours',
            'title': 'Buys and sells only in regular hours',
            'set_to': session,
            'why': 'Watchlist/buys and sell-check jobs skip when the US equity session is closed',
        },
        {
            'id': 'no_day_trade',
            'title': 'No buy-and-sell the same day',
            'set_to': 'No market sell or broker STOP_LIMIT until the next ET calendar day',
            'why': (
                'FINRA day trade = same trading day (not 24h). Buy Mon 1pm / sell Tue 8am '
                'is fine; stops are deferred so a same-day fill cannot create a round trip'
            ),
        },
        {
            'id': 'cash_floor',
            'title': 'Cash floor',
            'set_to': 'Keep ≥ $%s effective cash' % '{:,.0f}'.format(min_cash),
            'why': 'No buy may leave cash (minus pending buys) below this floor. Sells still run.',
        },
        {
            'id': 'liquidation_floor',
            'title': 'Account size floor',
            'set_to': 'Liquidation value ≥ $%s' % '{:,.0f}'.format(min_liq),
            'why': 'No buy while account value is under this threshold. Sells still run.',
        },
        {
            'id': 'watchlist_only',
            'title': 'Buy only from the active filter / watchlist',
            'set_to': "Filter '%s'" % filter_name,
            'why': 'New buys must be on the current watchlist — no random tickers',
        },
        {
            'id': 'position_size',
            'title': 'Fixed buy size',
            'set_to': '~$%s per new name' % '{:,.0f}'.format(order_amt),
            'why': 'Keeps position sizing consistent and cash-floor math simple',
        },
        {
            'id': 'rebuy_debounce',
            'title': 'Re-buy debounce after a sell',
            'set_to': (
                '%d weekdays after sell OR ≤ last sell − %.0f%%'
                % (rebuy_days, rebuy_pct)
            ),
            'why': (
                'Avoid immediately rebuying the same name after a stop; unlock on '
                'cooldown or a meaningful discount'
            ),
        },
        {
            'id': 'trail_activate',
            'title': 'Trailing stop arms after a gain',
            'set_to': 'Arm at +%.0f%% peak unrealized' % trail_act,
            'why': 'Protect winners once they have moved enough; ignore noise before that',
        },
        {
            'id': 'trail_buffer',
            'title': 'Trail buffer below peak',
            'set_to': '%.0f%% on watchlist · %.0f%% once off' % (trail_buf, trail_off),
            'why': 'Stop ratchets up with new highs; tighter once the thesis (watchlist) breaks',
        },
        {
            'id': 'hard_stop',
            'title': 'Hard stop vs cost',
            'set_to': '−%.0f%% on watchlist · −%.0f%% once off' % (hard_on, hard_off),
            'why': 'Catastrophe floor when a trail never armed, or thesis is gone',
        },
        {
            'id': 'broker_stop',
            'title': 'Resting broker STOP_LIMIT',
            'set_to': (
                'Arm after next ET day · limit %.2f%% below stop · %s' % (
                    slip, str(getattr(config, 'STOP_ORDER_DURATION', 'GOOD_TILL_CANCEL'))
                )
            ),
            'why': (
                'Protective sell sits at Schwab once past the purchase day; never armed '
                'same day so a stop fill cannot create a PDT round trip'
            ),
        },
        {
            'id': 'sell_cadence',
            'title': 'Sell check cadence',
            'set_to': 'Every %d minutes (RTH)' % sell_mins,
            'why': 'Re-evaluate trails / hard stops while the market is open',
        },
        {
            'id': 'legacy_books',
            'title': 'Legacy holdings are sell-skipped',
            'set_to': 'Only algorithm / enrolled books get auto sells',
            'why': 'Pre-marked legacy carve-outs stay human-managed until enrolled',
        },
        {
            'id': 'buys_paused',
            'title': 'Buys can be paused',
            'set_to': 'Paused' if buys_paused() else 'Allowed (runtime flag)',
            'why': 'A runtime pause blocks new buys without changing the rest of the system',
        },
    ]
    return {'count': len(rules), 'rules': rules}


def get_next_scheduled_tasks() -> List[Dict[str, Any]]:
    """
    Upcoming scheduled tasks, soonest first.
    RTH-only tasks that are due while the market is closed wait until next open.
    Jobs with status=running show elapsed time instead of a bare "now".
    """
    init_database()
    now = datetime.now()
    market_open = is_us_equity_market_open()
    next_open = _as_naive_local(next_us_equity_market_open())
    items = []  # type: List[Dict[str, Any]]
    for job_name, _fn, interval_days in get_scheduled_jobs():
        row = get_job_run(job_name)
        status = str((row or {}).get('status') or '')
        running = status == 'running'
        running_minutes = None  # type: Optional[int]
        if running and row and row.get('last_started'):
            try:
                started = datetime.fromisoformat(str(row['last_started']))
                running_minutes = int(
                    max(0, (now - started).total_seconds() // 60)
                )
            except (TypeError, ValueError):
                running_minutes = 0

        due = is_job_due(job_name, interval_days)
        due_at = next_job_due_at(job_name, interval_days)
        needs_rth = job_name in JOBS_REQUIRE_MARKET_OPEN
        if due or running:
            run_at = now
        else:
            run_at = due_at if due_at is not None else now
        if needs_rth and not market_open and run_at < next_open:
            run_at = next_open
            running = False
        seconds = (run_at - now).total_seconds()
        minutes = int(max(0, round(seconds / 60.0)))
        title = _JOB_DISPLAY_NAMES.get(job_name, job_name.replace('_', ' ').title())
        if running:
            if running_minutes is None or running_minutes <= 0:
                when = 'running'
            elif running_minutes == 1:
                when = 'running 1 min'
            else:
                when = 'running %d min' % running_minutes
            minutes = 0
        elif minutes <= 0:
            when = 'now'
        elif minutes == 1:
            when = 'in 1 minute'
        elif minutes > 60:
            hours = max(1, int(round(minutes / 60.0)))
            when = 'in 1 hour' if hours == 1 else 'in %d hours' % hours
        else:
            when = 'in %d minutes' % minutes
        label = '%s %s' % (title, when)
        items.append({
            'job_name': job_name,
            'title': title,
            'due_at': run_at.isoformat(timespec='seconds'),
            'minutes_until': minutes,
            'due_now': (not running) and minutes <= 0,
            'running': bool(running),
            'running_minutes': running_minutes,
            'status': status or None,
            'requires_market_open': needs_rth,
            'when': when,
            'label': label,
        })
    items.sort(
        key=lambda x: (
            0 if x.get('running') else 1,
            int(x['minutes_until']),
            str(x['title']),
        )
    )
    return items


def get_dashboard_status() -> Dict[str, Any]:
    """Home-page live strip: jobs, flags, Yahoo SLA."""
    init_database()
    jobs_out = []  # type: List[Dict[str, Any]]
    market_open = is_us_equity_market_open()
    for job_name, _fn, interval_days in get_scheduled_jobs():
        row = get_job_run(job_name)
        due = is_job_due(job_name, interval_days)
        due_at = next_job_due_at(job_name, interval_days)
        needs_rth = job_name in JOBS_REQUIRE_MARKET_OPEN
        jobs_out.append({
            'job_name': job_name,
            'interval_days': interval_days,
            'interval_label': format_job_interval(interval_days),
            'requires_market_open': needs_rth,
            'due_now': due,
            'would_run_now': bool(due and (not needs_rth or market_open)),
            'last_started': row.get('last_started') if row else None,
            'last_completed': row.get('last_completed') if row else None,
            'status': row.get('status') if row else None,
            'progress_note': row.get('progress_note') if row else None,
            'next_due': due_at.isoformat() if due_at else None,
        })
    user = uc.get_user_by_id(_uid())
    try:
        setup = get_account_setup_status()
    except Exception:
        setup = _account_setup_status_fallback()
    try:
        algo_ctl = get_algorithm_control_status()
    except Exception:
        algo_ctl = {'needs_first_run': False}
    schwab = get_schwab_auth_status()
    try:
        stage = get_onboarding_stage()
    except Exception:
        stage = 'done'
    needs_attention = (
        stage != 'done'
        or bool(schwab.get('warn'))
    )
    return {
        'ts': datetime.now().isoformat(timespec='seconds'),
        'dashboard_rev': get_dashboard_rev(),
        'trade_dry_run': trade_dry_run_enabled(),
        'buys_paused': buys_paused(),
        'market_open': market_open,
        'next_market_open': next_us_equity_market_open().isoformat(),
        'last_loop_wake': get_runtime_flag('last_loop_wake'),
        'algorithm_start': get_algorithm_start(),
        'algorithm': get_algorithm_performance(),
        'jobs': jobs_out,
        'next_tasks': get_next_scheduled_tasks(),
        'yahoo': get_yahoo_staleness_summary(),
        'watchlist_filter': get_watchlist_filter_dashboard(),
        'trading_rules': get_trading_rules_dashboard(),
        'schwab': schwab,
        'account_setup': setup,
        'algorithm_control': algo_ctl,
        'onboarding_stage': stage,
        'actions_attention': needs_attention,
        'user': {
            'id': _uid(),
            'username': (user or {}).get('username'),
            'display_name': (user or {}).get('display_name'),
            'is_admin': bool((user or {}).get('is_admin')),
        },
    }


def _dashboard_stop_display_price(
    stop_order_price: Optional[float],
    stop_gain_pct: Optional[float],
    average_price: Optional[float],
) -> Optional[float]:
    """Prefer resting broker stop trigger; else implied stop from trail/hard math."""
    if stop_order_price is not None:
        try:
            px = float(stop_order_price)
            if px > 0:
                return px
        except (TypeError, ValueError):
            pass
    if stop_gain_pct is not None and average_price is not None:
        try:
            avg = float(average_price)
            if avg > 0:
                return avg * (1.0 + float(stop_gain_pct))
        except (TypeError, ValueError):
            pass
    return None


def _dashboard_days_held(
    date_purchased: Optional[str],
    purchased_at: Optional[str] = None,
) -> Optional[int]:
    """Whole days owned for display — prefer Schwab date_purchased over purchased_at."""
    purchased = _parse_purchased_at(date_purchased) or _parse_purchased_at(purchased_at)
    if purchased is None:
        return None
    return max(0, (datetime.now().date() - purchased.date()).days)


def _dashboard_position_status(
    ticker: str,
    book: Optional[str],
    stop_display_price: Optional[float],
    trail_active: bool = False,
) -> Dict[str, Any]:
    """
    Trader Status column:
    - holding: min-hold window (market sells blocked)
    - trail: trailing stop armed (peak reached activate %)
    - floor: hard-stop only (never reached trail activate yet) — shown in red in UI
    - tracking: sell-managed but no stop level yet
    - skipped / not enrolled: outside algorithm book
    """
    book_norm = book or 'untagged'
    if book_norm == 'legacy':
        return {
            'status': 'skipped',
            'status_label': 'legacy (not managed)',
            'min_hold_met': None,
            'hold_hours_left': None,
        }
    if book_norm != 'algorithm':
        return {
            'status': 'not_enrolled',
            'status_label': 'not enrolled',
            'min_hold_met': None,
            'hold_hours_left': None,
        }

    hold_ok, _hold_reason = is_min_hold_met(ticker)
    left = hours_until_exit_allowed(ticker) if not hold_ok else 0.0

    if not hold_ok:
        if left is not None and left > 0:
            label = 'holding · next ET day (%.0fh)' % left
        else:
            label = 'holding · until next ET day'
        return {
            'status': 'holding',
            'status_label': label,
            'min_hold_met': False,
            'hold_hours_left': left,
        }

    if stop_display_price is not None:
        px = float(stop_display_price)
        if trail_active:
            return {
                'status': 'trail',
                'status_label': 'trail $%.2f' % px,
                'min_hold_met': True,
                'hold_hours_left': 0.0,
            }
        return {
            'status': 'floor',
            'status_label': 'floor $%.2f' % px,
            'min_hold_met': True,
            'hold_hours_left': 0.0,
        }

    return {
        'status': 'tracking',
        'status_label': 'tracking',
        'min_hold_met': True,
        'hold_hours_left': 0.0,
    }


_dashboard_refresh_lock = threading.Lock()
_dashboard_refresh_started = {}  # type: Dict[int, float]


def _positions_sync_status_from_flags() -> Dict[str, Any]:
    """Last known positions sync metadata (no Schwab / no write)."""
    last_s = get_runtime_flag('positions_synced_at')
    age = None  # type: Optional[float]
    if last_s:
        try:
            age = (datetime.now() - datetime.fromisoformat(last_s)).total_seconds()
        except ValueError:
            age = None
    min_age = float(getattr(config, 'POSITIONS_SYNC_MIN_INTERVAL_SECONDS', 45))
    fresh = age is not None and age < min_age
    return {
        'ok': True,
        'synced': False,
        'skipped': True,
        'reason': 'fresh' if fresh else 'pending_refresh',
        'age_seconds': age,
        'synced_at': last_s,
        'count': None,
    }


def _schedule_dashboard_schwab_refresh() -> None:
    """
    Fire-and-forget Schwab reconcile/sync so /api/portfolio never blocks the UI.

    The trader loop also syncs during sell_check; this keeps marks fresher while
    someone is watching the Trader page, without waiting on DB writers.
    """
    uid = uc.current_user_id()
    min_age = float(getattr(config, 'POSITIONS_SYNC_MIN_INTERVAL_SECONDS', 45))
    status = _positions_sync_status_from_flags()
    if status.get('reason') == 'fresh':
        return
    key = int(uid) if uid is not None else 0
    now = time.time()
    with _dashboard_refresh_lock:
        last = float(_dashboard_refresh_started.get(key) or 0.0)
        if now - last < min_age:
            return
        _dashboard_refresh_started[key] = now

    def _worker():
        try:
            ctx = uc.use_user(int(uid)) if uid is not None else _nullcontext()
            with ctx:
                if uid is not None:
                    _sync_schwab_globals(int(uid))
                try:
                    reconcile_pending_orders()
                except Exception:
                    pass
                try:
                    refresh_schwab_positions_if_needed(force=False)
                except Exception:
                    pass
                # Refresh cash / equity snapshot when Schwab is reachable
                try:
                    maybe_reinit_schwab_client()
                    if account_info_usable(get_account_info() or {}):
                        record_account_snapshot(note='dashboard_sync')
                except Exception:
                    pass
        except Exception as e:
            print('dashboard background sync: %s' % e)

    threading.Thread(
        target=_worker, daemon=True, name='dashboard-schwab-sync'
    ).start()


_WATCHLIST_FIELD_META = {
    'ticker': {'label': 'Ticker', 'kind': 'text'},
    'market_cap': {'label': 'Mkt cap', 'kind': 'dollars'},
    'beta': {'label': 'Beta', 'kind': 'number'},
    'short_float': {'label': 'Short %', 'kind': 'fraction'},
    'pe_ratio': {'label': 'P/E', 'kind': 'number'},
    'peg_ratio': {'label': 'PEG', 'kind': 'number'},
    'analyst_upside': {'label': 'Upside', 'kind': 'fraction'},
    'recommendation_mean': {'label': 'Rec', 'kind': 'number'},
    'dividend_yield': {'label': 'Div', 'kind': 'percent_points'},
    'avg_volume': {'label': 'Avg vol', 'kind': 'shares'},
    'current_price': {'label': 'Price', 'kind': 'price'},
    'forward_pe': {'label': 'Fwd P/E', 'kind': 'number'},
    'price_to_book': {'label': 'P/B', 'kind': 'number'},
    'debt_to_equity': {'label': 'D/E', 'kind': 'number'},
    'profit_margins': {'label': 'Margin', 'kind': 'fraction'},
    'revenue_growth': {'label': 'Rev growth', 'kind': 'fraction'},
}


def _watchlist_column_meta(field: str) -> Dict[str, str]:
    """Short table header + value kind for a filter field."""
    known = _WATCHLIST_FIELD_META.get(field)
    if known:
        return dict(known)
    kind = 'number'
    try:
        import filter_builder as fb
        meta = getattr(fb, '_FIELD_BY_NAME', {}).get(field) or {}
        kind = str(meta.get('value_kind') or 'number')
        if field == 'current_price' or (kind == 'dollars' and field != 'market_cap'):
            kind = 'price'
    except Exception:
        pass
    label = field.replace('_', ' ').title()
    return {'label': label, 'kind': kind}


def _watchlist_display_fields(filter_name: Optional[str] = None) -> Tuple[List[str], str, str]:
    """Active filter's criterion fields, title, and name for the trader watchlist table."""
    name = str(filter_name or active_watchlist_filter() or 'safe').strip()
    name_l = name.lower()
    catalog = _watchlist_filter_catalog()
    desc = catalog.get(name_l)
    if desc:
        fields = [c.get('field') for c in (desc.get('criteria') or []) if c.get('field')]
        title = str(desc.get('title') or name)
        return fields, title, name_l
    try:
        import filter_builder as fb
        custom = fb.get_user_custom_filter(_uid())
    except Exception:
        custom = None
    if custom:
        custom_name = str(custom.get('name') or '').strip()
        if custom_name.lower() == name_l:
            fields = [c.get('field') for c in (custom.get('criteria') or []) if c.get('field')]
            title = custom_name or name
            return fields, title, custom_name
    return ['analyst_upside'], name, name_l


def get_dashboard_watchlist() -> Dict[str, Any]:
    """
    Trader watchlist table: membership from watchlist, columns from the active
    filter's fields, rows ranked by analyst upside (highest first).
    """
    filter_fields, filter_title, filter_name = _watchlist_display_fields()
    columns = [{'key': 'ticker', 'label': 'Ticker', 'kind': 'text'}]
    seen = {'ticker'}
    for field in filter_fields:
        if not field or field in seen:
            continue
        seen.add(field)
        meta = _watchlist_column_meta(field)
        columns.append({
            'key': field,
            'label': meta['label'],
            'kind': meta['kind'],
        })

    empty = {
        'filter': filter_name,
        'filter_title': filter_title,
        'ranking': 'analyst_upside',
        'columns': columns,
        'rows': [],
        'count': 0,
    }
    try:
        ranked = get_watchlist_ranked()
    except sqlite3.OperationalError:
        return empty

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('PRAGMA table_info(watchlist)')
        wl_cols = [row[1] for row in cursor.fetchall()]
        if not wl_cols:
            return empty
        select_cols = []
        for col in ['ticker', 'current_price', 'target_price'] + [
            f for f in filter_fields if f != 'analyst_upside'
        ]:
            if col in wl_cols and col not in select_cols:
                select_cols.append(col)
        if 'ticker' not in select_cols:
            return empty
        cursor.execute('SELECT ' + ', '.join(select_cols) + ' FROM watchlist')
        by_ticker = {}  # type: Dict[str, Dict[str, Any]]
        for row in cursor.fetchall():
            d = dict(zip(select_cols, row))
            ticker = d.get('ticker')
            if not ticker:
                continue
            price = d.get('current_price')
            target = d.get('target_price')
            upside = None  # type: Optional[float]
            try:
                price_f = float(price) if price is not None else None
                target_f = float(target) if target is not None else None
                if price_f and target_f and price_f > 0:
                    upside = (target_f - price_f) / price_f
            except (TypeError, ValueError):
                upside = None
            values = {'ticker': ticker, 'analyst_upside': upside}
            for field in filter_fields:
                if field == 'analyst_upside':
                    continue
                values[field] = d.get(field)
            by_ticker[str(ticker)] = values
    except sqlite3.OperationalError:
        return empty
    finally:
        conn.close()

    rows = []  # type: List[Dict[str, Any]]
    for i, ticker in enumerate(ranked):
        d = by_ticker.get(str(ticker))
        if not d:
            continue
        item = dict(d)
        item['rank'] = i + 1
        rows.append(item)
    return {
        'filter': filter_name,
        'filter_title': filter_title,
        'ranking': 'analyst_upside',
        'columns': columns,
        'rows': rows,
        'count': len(rows),
    }


def get_dashboard_portfolio() -> Dict[str, Any]:
    """Trader page: positions, pending/open orders, watchlist, totals (read-only)."""
    init_database()
    # Do not await Schwab/DB writers here — that could stall Loading for ~60s
    # when the trader loop holds a write lock. Kick a background refresh instead.
    _schedule_dashboard_schwab_refresh()
    positions_sync = _positions_sync_status_from_flags()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(positions)")
    pos_cols = [row[1] for row in cursor.fetchall()]
    have_pct = 'day_pct' in pos_cols and 'open_pct' in pos_cols
    have_trail = all(c in pos_cols for c in ('peak_gain_pct', 'stop_gain_pct', 'trail_active'))
    have_broker_stop = all(
        c in pos_cols
        for c in ('stop_order_id', 'stop_order_price', 'stop_limit_price', 'stop_order_qty')
    )
    have_purchased_at = 'purchased_at' in pos_cols
    select_cols = [
        'ticker', 'date_purchased', 'shares_owned', 'average_price', 'market_value',
        'current_day_profit_loss', 'long_open_profit_loss',
    ]
    if have_pct:
        select_cols = [
            'ticker', 'date_purchased', 'shares_owned', 'average_price', 'market_value',
            'current_day_profit_loss', 'day_pct', 'long_open_profit_loss', 'open_pct',
        ]
    if have_purchased_at:
        select_cols = select_cols + ['purchased_at']
    if have_trail:
        select_cols = select_cols + ['peak_gain_pct', 'stop_gain_pct', 'trail_active']
    if have_broker_stop:
        select_cols = select_cols + [
            'stop_order_id', 'stop_order_price', 'stop_limit_price', 'stop_order_qty',
        ]
    cursor.execute(
        'SELECT ' + ', '.join(select_cols) +
        ' FROM positions WHERE shares_owned > 0 ORDER BY ticker'
    )
    pos_rows = cursor.fetchall()
    positions = []  # type: List[Dict[str, Any]]
    total_cost = 0.0
    total_mv = 0.0
    total_day = 0.0
    total_open = 0.0
    for row in pos_rows:
        d = dict(zip(select_cols, row))
        ticker = d.get('ticker')
        date_purchased = d.get('date_purchased')
        shares = d.get('shares_owned')
        avg = d.get('average_price')
        mv = d.get('market_value')
        day_pl = d.get('current_day_profit_loss')
        open_pl = d.get('long_open_profit_loss')
        day_pct = d.get('day_pct') if have_pct else None
        open_pct = d.get('open_pct') if have_pct else None
        peak = d.get('peak_gain_pct') if have_trail else None
        stop_g = d.get('stop_gain_pct') if have_trail else None
        trail = d.get('trail_active') if have_trail else None
        shares_f = float(shares or 0)
        avg_f = float(avg) if avg is not None else None
        cost = (shares_f * avg_f) if avg_f is not None else None
        if cost is not None:
            total_cost += cost
        if mv is not None:
            total_mv += float(mv)
        if day_pl is not None:
            total_day += float(day_pl)
        if open_pl is not None:
            total_open += float(open_pl)
        stop_order_price = d.get('stop_order_price') if have_broker_stop else None
        current_price = None
        if mv is not None and shares_f > 0:
            try:
                current_price = float(mv) / shares_f
            except (TypeError, ValueError, ZeroDivisionError):
                current_price = None
        stop_display = _dashboard_stop_display_price(stop_order_price, stop_g, avg)
        book = get_position_book(ticker) or 'untagged'
        trail_on = bool(trail) if trail is not None else False
        status_info = _dashboard_position_status(
            ticker, book, stop_display, trail_active=trail_on,
        )
        purchased_at = d.get('purchased_at') if have_purchased_at else None
        days_held = _dashboard_days_held(
            date_purchased if isinstance(date_purchased, str) else None,
            purchased_at if isinstance(purchased_at, str) else None,
        )
        positions.append({
            'ticker': ticker,
            'date_purchased': date_purchased,
            'days_held': days_held,
            'shares_owned': int(shares) if shares is not None else 0,
            'average_price': avg,
            'current_price': current_price,
            'cost_basis': cost,
            'market_value': mv,
            'day_pl': day_pl,
            'day_pct': day_pct,
            'open_pl': open_pl,
            'open_pct': open_pct,
            'peak_gain_pct': peak,
            'stop_gain_pct': stop_g,
            'trail_active': trail_on,
            'book': book,
            'stop_order_id': d.get('stop_order_id') if have_broker_stop else None,
            'stop_order_price': stop_order_price,
            'stop_limit_price': d.get('stop_limit_price') if have_broker_stop else None,
            'stop_order_qty': d.get('stop_order_qty') if have_broker_stop else None,
            'stop_display_price': stop_display,
            'status': status_info['status'],
            'status_label': status_info['status_label'],
            'min_hold_met': status_info['min_hold_met'],
            'hold_hours_left': status_info['hold_hours_left'],
        })

    # Resting STOP_LIMIT sells (same working orders Schwab shows as open).
    limit_sells = []  # type: List[Dict[str, Any]]
    for p in positions:
        oid = p.get('stop_order_id')
        if not oid:
            continue
        qty = p.get('stop_order_qty')
        if qty is None:
            qty = p.get('shares_owned')
        limit_sells.append({
            'ticker': p.get('ticker'),
            'quantity': qty,
            'order_id': oid,
            'order_type': 'STOP_LIMIT',
            'stop_price': p.get('stop_order_price'),
            'limit_price': p.get('stop_limit_price'),
            'source': 'limit_sell',
        })

    pending = []  # type: List[Dict[str, Any]]
    try:
        cursor.execute(
            '''
            SELECT id, ticker, date_ordered, quantity_ordered, order_amount_dollars, order_id
            FROM pending_orders ORDER BY date_ordered DESC
            '''
        )
        for r in cursor.fetchall():
            pending.append({
                'id': r[0],
                'ticker': r[1],
                'date_ordered': r[2],
                'quantity_ordered': r[3],
                'order_amount_dollars': r[4],
                'order_id': r[5],
            })
    except sqlite3.OperationalError:
        pass

    conn.close()

    open_orders = []  # type: List[Dict[str, Any]]
    if getattr(config, 'WEB_FETCH_OPEN_ORDERS', False):
        try:
            open_orders = get_open_orders() or []
        except Exception:
            open_orders = []
    limit_ids = {str(o.get('order_id')) for o in limit_sells if o.get('order_id')}
    if limit_ids and open_orders:
        open_orders = [
            o for o in open_orders
            if str(o.get('order_id') or '') not in limit_ids
        ]

    sod = total_mv - total_day
    day_pct_total = (total_day / sod * 100.0) if sod and abs(sod) > 1e-9 else None
    open_pct_total = (
        (total_mv - total_cost) / total_cost * 100.0
        if total_cost and abs(total_cost) > 1e-9 else None
    )

    books = list_position_books()
    sync_age = positions_sync.get('age_seconds')
    sync_stale = (not positions_sync.get('ok')) or (
        sync_age is not None
        and float(sync_age) > float(
            getattr(config, 'POSITIONS_SYNC_MIN_INTERVAL_SECONDS', 45)
        ) * 3
    )

    # Cash / account value: last real Schwab snapshot only (never fabricated $10k).
    # Green while younger than SCHWAB_SYNC_INTERVAL_MINUTES; orange after that.
    # Do not purge here — DELETE on every portfolio poll contends with the trader loop.
    snap_dict = get_latest_account_snapshot(exclude_fabricated=True)
    account_snapshot = None  # type: Optional[Dict[str, Any]]
    if snap_dict:
        snap_age = None  # type: Optional[float]
        try:
            snap_age = (
                datetime.now() - datetime.fromisoformat(str(snap_dict['ts']))
            ).total_seconds()
        except Exception:
            snap_age = None
        stale_after = float(
            getattr(config, 'SCHWAB_SYNC_INTERVAL_MINUTES', 5)
        ) * 60.0
        snap_stale = snap_age is None or snap_age > stale_after
        # Also stale when Schwab client is down (showing last known real balance)
        if not SCHWAB_AVAILABLE:
            snap_stale = True
        account_snapshot = {
            'ts': snap_dict.get('ts'),
            'cash': snap_dict.get('cash'),
            'effective_cash': snap_dict.get('effective_cash'),
            'liquidation_value': snap_dict.get('liquidation_value'),
            'total_value': snap_dict.get('total_value'),
            'note': snap_dict.get('note'),
            'stale': bool(snap_stale),
            'age_seconds': snap_age,
            'fresh_within_seconds': stale_after,
            'source': 'snapshot',
        }

    port_user = uc.get_user_by_id(_uid()) or {}
    return {
        'ts': datetime.now().isoformat(timespec='seconds'),
        'positions': positions,
        'positions_sync': {
            'ok': bool(positions_sync.get('ok')),
            'synced': bool(positions_sync.get('synced')),
            'skipped': bool(positions_sync.get('skipped')),
            'reason': positions_sync.get('reason'),
            'synced_at': positions_sync.get('synced_at'),
            'age_seconds': sync_age,
            'stale': bool(sync_stale),
        },
        'books': {
            'legacy': books['legacy'],
            'algorithm': books['algorithm'],
            'algo_buys': books.get('algo_buys') or [],
            'enrolled': books.get('enrolled') or [],
            'untagged': books['untagged'],
        },
        'algorithm': get_algorithm_performance(),
        'next_tasks': get_next_scheduled_tasks(),
        'pending_orders': pending,
        'open_orders': open_orders,
        'limit_sells': limit_sells,
        'watchlist': get_dashboard_watchlist(),
        'totals': {
            'cost_basis': total_cost,
            'market_value': total_mv,
            'day_pl': total_day,
            'day_pct': day_pct_total,
            'open_pl': total_open,
            'open_pct': open_pct_total,
            'realized_note': (
                'Realized P/L since algorithm_start is in algorithm.realized_pl_since_start '
                '(from trade_history). Day/open P/L are Schwab-synced position fields.'
            ),
        },
        'account_snapshot': account_snapshot,
        'user': {
            'id': _uid(),
            'username': port_user.get('username'),
            'display_name': port_user.get('display_name'),
            'is_admin': bool(port_user.get('is_admin')),
        },
    }


def get_dashboard_events(
    limit: int = 100,
    categories: Optional[List[str]] = None,
    before_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Log page: newest structured events for the current user only.

    Returns {'events': [...], 'has_more': bool}. Use before_id to page older
    (id < before_id). Fetches limit+1 rows so has_more is exact.
    """
    init_database()
    limit = max(1, min(int(limit), 500))
    uid = _uid()
    user = uc.get_user_by_id(uid) or {}

    allowed = {'buy', 'sell', 'stop-limit', 'watchlist', 'task'}
    cats = []  # type: List[str]
    if categories:
        for raw in categories:
            key = normalize_log_category(raw)
            if key in allowed and key not in cats:
                cats.append(key)

    before = None  # type: Optional[int]
    if before_id is not None:
        try:
            before = int(before_id)
        except (TypeError, ValueError):
            before = None
        if before is not None and before < 1:
            before = None

    fetch_n = limit + 1
    where_parts = []  # type: List[str]
    params = []  # type: List[Any]
    if cats:
        placeholders = ','.join(['?'] * len(cats))
        where_parts.append('category IN (%s)' % placeholders)
        params.extend(cats)
    if before is not None:
        where_parts.append('id < ?')
        params.append(before)
    params.append(fetch_n)

    sql = 'SELECT id, ts, level, category, message, detail_json FROM event_log'
    if where_parts:
        sql += ' WHERE ' + ' AND '.join(where_parts)
    sql += ' ORDER BY id DESC LIMIT ?'

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(sql, tuple(params))
    rows = cursor.fetchall()
    conn.close()

    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]

    events = []  # type: List[Dict[str, Any]]
    for r in rows:
        detail = None
        if r[5]:
            try:
                detail = json.loads(r[5])
            except Exception:
                detail = r[5]
        events.append({
            'id': r[0],
            'ts': r[1],
            'level': r[2],
            'category': r[3],
            'message': r[4],
            'detail': detail,
            'user_id': uid,
            'username': user.get('username'),
            'display_name': user.get('display_name') or user.get('username'),
        })
    return {'events': events, 'has_more': has_more}


def _schwab_linked_ok(auth: Optional[Dict[str, Any]] = None) -> bool:
    auth = auth or get_schwab_auth_status()
    return auth.get('state') in ('connected', 'expiring')


def fetch_setup_account_snapshot(live: bool = False) -> Optional[Dict[str, Any]]:
    """
    Cash + liquidation for setup bounds, plus as_of for the age line.
    Default: last persisted Schwab snapshot (so the UI can show min/hrs/days ago).
    live=True: hit Schwab now (used when saving floors).
    Snapshot errors must not skip the live Schwab read.
    """
    def _from_snap(snap: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not snap:
            return None
        cash = float(snap.get('cash') or 0.0)
        liq = float(snap.get('liquidation_value') or 0.0)
        if cash <= 0 and liq <= 0:
            return None
        return {
            'cash': cash,
            'liquidation_value': liq,
            'as_of': snap.get('ts'),
        }

    try:
        if not _schwab_linked_ok():
            return None
    except Exception:
        return None

    if not live:
        try:
            from_db = _from_snap(get_latest_account_snapshot(exclude_fabricated=True))
            if from_db:
                return from_db
        except Exception:
            pass

    try:
        maybe_reinit_schwab_client()
        acct = get_account_info() or {}
        if account_info_usable(acct):
            return {
                'cash': float(acct.get('cash') or 0.0),
                'liquidation_value': float(acct.get('liquidation_value') or 0.0),
                'as_of': datetime.now().isoformat(timespec='seconds'),
            }
    except Exception:
        pass

    try:
        return _from_snap(get_latest_account_snapshot(exclude_fabricated=True))
    except Exception:
        return None


def compute_setup_bounds(
    account: Optional[Dict[str, Any]],
    minimum_cash_value: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Bound floors/buy size from live Schwab balances.
    Min cash: 0 <= x <= cash (same rule on first setup and later edits)
    Min account value: 0 <= x <= liquidation (same rule on first setup and later edits)
    Buy size: 0 < x <= cash - min_cash (or cash while min_cash unset)
    """
    cash = float((account or {}).get('cash') or 0.0)
    liq = float((account or {}).get('liquidation_value') or 0.0)
    mc = minimum_cash_value
    if mc is None:
        spend_cap = max(0.0, cash)
    else:
        spend_cap = max(0.0, cash - float(mc))
    # Floor = all cash → no spendable now; still allow a stored buy size for later.
    order_max = spend_cap if spend_cap > 0.0 else max(cash, 1.0)
    return {
        'minimum_cash': {
            'min_inclusive': 0.0,
            'max_inclusive': cash,
            'valid_range': True,
        },
        'minimum_liquidation_value': {
            'min_inclusive': 0.0,
            'max_inclusive': liq,
            'valid_range': True,
        },
        'order_amount_dollars': {
            'min_exclusive': 0.0,
            'max_inclusive': order_max,
            'valid_range': order_max > 0.0,
        },
        'cash': cash,
        'liquidation_value': liq,
    }


def validate_setup_values(
    minimum_cash_val: float,
    minimum_liq_val: float,
    order_amount_val: float,
    account: Optional[Dict[str, Any]],
) -> Optional[str]:
    """Return error string if values violate bounds; else None."""
    if account is None:
        return 'Link Schwab and load account balances before saving floors'
    bounds = compute_setup_bounds(account, minimum_cash_value=minimum_cash_val)
    bc = bounds['minimum_cash']
    bl = bounds['minimum_liquidation_value']
    bo = bounds['order_amount_dollars']
    cash_hi = float(bc.get('max_inclusive', bounds['cash']))
    if not (0.0 <= minimum_cash_val <= cash_hi):
        return 'Minimum cash must be between $0 and $%,.0f' % cash_hi
    liq_hi = float(bl.get('max_inclusive', bounds['liquidation_value']))
    if not (0.0 <= minimum_liq_val <= liq_hi):
        return 'Minimum account value must be between $0 and $%,.0f' % liq_hi
    if not (bo['min_exclusive'] < order_amount_val <= bo['max_inclusive']):
        return 'Buy size must be more than $0 and at most $%,.0f' % bo['max_inclusive']
    return None


def suggest_setup_values(account: Optional[Dict[str, Any]]) -> Dict[str, float]:
    """Sensible defaults inside bounds when account balances are known."""
    if not account:
        return {
            'minimum_cash': 10000.0,
            'minimum_liquidation_value': 25000.0,
            'order_amount_dollars': 1000.0,
        }
    cash = float(account.get('cash') or 0.0)
    liq = float(account.get('liquidation_value') or 0.0)
    # Mid-range floor, clamped to [0, cash].
    if cash <= 0.0:
        min_cash = 0.0
    else:
        mid = cash / 2.0
        min_cash = max(0.0, min(cash, round(mid / 100.0) * 100.0))
    if liq <= 0.0:
        min_liq = 0.0
    else:
        mid_liq = liq / 2.0
        min_liq = max(0.0, min(liq, round(mid_liq / 100.0) * 100.0))
    buy_cap = max(1.0, cash - min_cash)
    order_amt = min(1000.0, buy_cap)
    if order_amt <= 0:
        order_amt = 1.0
    return {
        'minimum_cash': float(min_cash),
        'minimum_liquidation_value': float(min_liq),
        'order_amount_dollars': float(order_amt),
    }


def get_onboarding_stage() -> str:
    """
    schwab | settings | algorithm | go_live | done
    Admins with finished setup skip the Account setup card path.
    """
    uid = _uid()
    settings = uc.get_user_settings(uid)
    schwab_ok = _schwab_linked_ok()
    setup_done = bool(settings.get('setup_complete')) or user_is_admin(uid)
    if not schwab_ok and not user_is_admin(uid):
        return 'schwab'
    if not setup_done:
        return 'settings' if schwab_ok else 'schwab'
    if not get_algorithm_start():
        return 'algorithm'
    if trade_dry_run_enabled():
        return 'go_live'
    return 'done'


def _account_setup_status_fallback() -> Dict[str, Any]:
    """Settings + Schwab state when the full setup payload cannot be built."""
    try:
        settings = uc.get_user_settings(_uid())
    except Exception:
        settings = {}
    try:
        auth = get_schwab_auth_status()
    except Exception:
        auth = {}
    schwab_ok = auth.get('state') in ('connected', 'expiring')
    complete = bool(settings.get('setup_complete'))
    try:
        complete = complete or user_is_admin()
    except Exception:
        pass
    account = None
    if schwab_ok:
        try:
            maybe_reinit_schwab_client()
            account = fetch_setup_account_snapshot()
        except Exception:
            account = None
    bounds = None
    if account:
        try:
            mc = settings.get('minimum_cash')
            bounds = compute_setup_bounds(
                account, minimum_cash_value=float(mc) if mc is not None else None,
            )
        except Exception:
            bounds = None
    return {
        'setup_complete': complete,
        'can_finish': False,
        'steps': {
            'schwab_linked': bool(schwab_ok),
            'minimum_cash': settings.get('minimum_cash') is not None,
            'minimum_liquidation_value': settings.get('minimum_liquidation_value') is not None,
            'order_amount_dollars': settings.get('order_amount_dollars') is not None,
        },
        'schwab': {
            'state': auth.get('state'),
            'warn': auth.get('warn'),
            'needs_login': auth.get('needs_login'),
        },
        'account': account,
        'bounds': bounds,
        'settings': {
            'minimum_cash': settings.get('minimum_cash'),
            'minimum_liquidation_value': settings.get('minimum_liquidation_value'),
            'order_amount_dollars': settings.get('order_amount_dollars'),
            'active_filter': settings.get('active_filter') or 'safe',
            'trade_dry_run': settings.get('trade_dry_run'),
        },
        'suggestions': suggest_setup_values(None),
    }


def get_account_setup_status() -> Dict[str, Any]:
    """Checklist for Actions → Account setup (Schwab + floors + buy size)."""
    try:
        import filter_builder as fb
        fb.init_filter_tables()
    except Exception:
        pass
    uid = _uid()
    settings = uc.get_user_settings(uid)
    auth = get_schwab_auth_status()
    schwab_ok = _schwab_linked_ok(auth)
    if schwab_ok:
        try:
            maybe_reinit_schwab_client()
        except Exception:
            pass
    account = None
    if schwab_ok:
        try:
            account = fetch_setup_account_snapshot()
        except Exception:
            account = None
    steps = {
        'schwab_linked': bool(schwab_ok),
        'minimum_cash': settings.get('minimum_cash') is not None,
        'minimum_liquidation_value': settings.get('minimum_liquidation_value') is not None,
        'order_amount_dollars': settings.get('order_amount_dollars') is not None,
    }
    # Owner/admin already runs — never require the onboarding Account setup card.
    complete = bool(settings.get('setup_complete')) or user_is_admin(uid)
    if complete and not settings.get('setup_complete'):
        try:
            uc.update_user_settings(uid, setup_complete=True)
            settings = uc.get_user_settings(uid)
        except Exception:
            pass
    can_finish = bool(schwab_ok) and all(
        steps[k] for k in (
            'minimum_cash', 'minimum_liquidation_value', 'order_amount_dollars',
        )
    )
    # If values present, re-check against live bounds when finishing is attempted
    # (status still reports can_finish from presence; save validates strictly).
    if can_finish and account is not None:
        try:
            err = validate_setup_values(
                float(settings['minimum_cash']),
                float(settings['minimum_liquidation_value']),
                float(settings['order_amount_dollars']),
                account,
            )
            if err:
                can_finish = False
        except Exception:
            can_finish = False
    mc = settings.get('minimum_cash')
    bounds = None
    if account:
        try:
            bounds = compute_setup_bounds(
                account, minimum_cash_value=float(mc) if mc is not None else None,
            )
        except Exception:
            bounds = None
    try:
        stage = get_onboarding_stage()
    except Exception:
        stage = 'done' if complete else 'settings'
    return {
        'setup_complete': complete,
        'can_finish': can_finish and bool(account),
        'onboarding_stage': stage,
        'steps': steps,
        'schwab': {
            'state': auth.get('state'),
            'warn': auth.get('warn'),
            'needs_login': auth.get('needs_login'),
        },
        'account': account,
        'bounds': bounds,
        'settings': {
            'minimum_cash': settings.get('minimum_cash'),
            'minimum_liquidation_value': settings.get('minimum_liquidation_value'),
            'order_amount_dollars': settings.get('order_amount_dollars'),
            'active_filter': settings.get('active_filter') or 'safe',
            'trade_dry_run': settings.get('trade_dry_run'),
        },
        'suggestions': suggest_setup_values(account),
    }


def user_is_admin(user_id: Optional[int] = None) -> bool:
    uid = int(user_id) if user_id is not None else _uid()
    u = uc.get_user_by_id(uid) or {}
    return bool(u.get('is_admin'))


def get_algorithm_control_status() -> Dict[str, Any]:
    """Actions → Algorithm control card."""
    import filter_builder as fb
    fb.init_filter_tables()
    uid = _uid()
    settings = uc.get_user_settings(uid)
    start = get_algorithm_start()
    custom = fb.get_user_custom_filter(uid)
    options = [
        {'name': 'safe', 'title': 'Safe Giant'},
        {'name': 'risky', 'title': 'Risky Momentum'},
    ]
    if custom:
        options.append({'name': custom['name'], 'title': custom['name']})
    setup_done = bool(settings.get('setup_complete')) or user_is_admin(uid)
    return {
        'algorithm_start': start,
        'needs_first_run': not bool(start),
        'trade_dry_run': bool(settings.get('trade_dry_run')),
        'active_filter': settings.get('active_filter') or 'safe',
        'filter_options': options,
        'setup_complete': setup_done,
        'schwab_linked': _schwab_linked_ok(),
        'onboarding_stage': get_onboarding_stage(),
        'custom_filter': custom,
        'field_catalog': fb.field_catalog_for_api(),
        'starter_criteria': fb.starter_criteria(),
        'max_custom_fields': fb.MAX_CUSTOM_FIELDS,
        'can_run': bool(setup_done and _schwab_linked_ok()),
        'can_go_live': bool(setup_done and _schwab_linked_ok() and start),
    }


def save_account_setup(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Save floors / buy size; optionally mark setup complete (not filter)."""
    uid = _uid()
    if not _schwab_linked_ok():
        return {
            'ok': False,
            'error': 'Link Schwab before setting floors and buy size',
            'setup': get_account_setup_status(),
        }
    account = fetch_setup_account_snapshot(live=True)
    kwargs = {}  # type: Dict[str, Any]
    try:
        if 'minimum_cash' in payload:
            kwargs['minimum_cash'] = float(payload['minimum_cash'])
        if 'minimum_liquidation_value' in payload:
            kwargs['minimum_liquidation_value'] = float(payload['minimum_liquidation_value'])
        if 'order_amount_dollars' in payload:
            kwargs['order_amount_dollars'] = float(payload['order_amount_dollars'])
    except (TypeError, ValueError):
        return {
            'ok': False,
            'error': 'Floors and buy size must be numbers',
            'setup': get_account_setup_status(),
        }
    # Merge with existing for partial saves
    settings = uc.get_user_settings(uid)
    min_cash = kwargs.get(
        'minimum_cash',
        settings.get('minimum_cash'),
    )
    min_liq = kwargs.get(
        'minimum_liquidation_value',
        settings.get('minimum_liquidation_value'),
    )
    order_amt = kwargs.get(
        'order_amount_dollars',
        settings.get('order_amount_dollars'),
    )
    if payload.get('finish') or len(kwargs) >= 3:
        if min_cash is None or min_liq is None or order_amt is None:
            return {
                'ok': False,
                'error': 'Set minimum cash, minimum account value, and buy size',
                'setup': get_account_setup_status(),
            }
        err = validate_setup_values(
            float(min_cash), float(min_liq), float(order_amt), account,
        )
        if err:
            return {'ok': False, 'error': err, 'setup': get_account_setup_status()}
    if payload.get('finish'):
        kwargs['setup_complete'] = True
    if kwargs:
        uc.update_user_settings(uid, **kwargs)
    return {
        'ok': True,
        'setup': get_account_setup_status(),
        'onboarding_stage': get_onboarding_stage(),
    }


def run_algorithm_action(action: str, filter_name: Optional[str] = None) -> Dict[str, Any]:
    """
    action: run | pause | go_live
    """
    uid = _uid()
    action = (action or '').strip().lower()
    settings = uc.get_user_settings(uid)
    setup_done = bool(settings.get('setup_complete')) or user_is_admin(uid)
    schwab_ok = _schwab_linked_ok()

    if filter_name:
        uc.set_user_active_filter(uid, str(filter_name).strip())

    if action == 'pause':
        uc.set_user_trade_dry_run(uid, True)
        log_event('web', 'Trading paused (dry-run on)')
        return {
            'ok': True,
            'algorithm': get_algorithm_control_status(),
            'onboarding_stage': get_onboarding_stage(),
        }

    if action == 'go_live':
        if not setup_done:
            return {
                'ok': False,
                'error': 'Finish account setup before going live',
                'algorithm': get_algorithm_control_status(),
            }
        if not schwab_ok:
            return {
                'ok': False,
                'error': 'Link Schwab before going live',
                'algorithm': get_algorithm_control_status(),
            }
        if not get_algorithm_start():
            return {
                'ok': False,
                'error': 'Press Run first to set your algorithm start (dry-run)',
                'algorithm': get_algorithm_control_status(),
            }
        uc.set_user_trade_dry_run(uid, False)
        log_event('web', 'Trading set to LIVE (dry-run off)', level='warn')
        return {
            'ok': True,
            'algorithm': get_algorithm_control_status(),
            'onboarding_stage': get_onboarding_stage(),
        }

    if action == 'run':
        if not setup_done:
            return {
                'ok': False,
                'error': 'Finish account setup (Schwab + floors + buy size) before Run',
                'algorithm': get_algorithm_control_status(),
            }
        if not schwab_ok:
            return {
                'ok': False,
                'error': 'Link Schwab before starting the algorithm',
                'algorithm': get_algorithm_control_status(),
            }
        active = (filter_name or settings.get('active_filter') or '').strip()
        if not active:
            return {
                'ok': False,
                'error': 'Choose a filter before Run',
                'algorithm': get_algorithm_control_status(),
            }
        if filter_name:
            pass  # already set above
        elif not settings.get('active_filter'):
            uc.set_user_active_filter(uid, active)
        start = get_algorithm_start()
        if not start:
            result = mark_algorithm_start(force=False)
            # Ensure dry-run for first start
            uc.set_user_trade_dry_run(uid, True)
            log_event(
                'web',
                'Algorithm start marked — scorecard/performance baseline set (still dry-run until Go live)',
                detail={'algorithm_start': result.get('algorithm_start')},
            )
        else:
            log_event('web', 'Algorithm Run acknowledged (already started %s)' % start)
        return {
            'ok': True,
            'algorithm': get_algorithm_control_status(),
            'onboarding_stage': get_onboarding_stage(),
        }
    return {'ok': False, 'error': 'Unknown action (use run, pause, or go_live)'}


def get_file_log_tail(lines: int = 200) -> str:
    """Raw trader.log tail for debugging."""
    path = getattr(config, 'WEB_LOG_PATH', 'logs/trader.log')
    lines = max(1, min(int(lines), 2000))
    if not os.path.isfile(path):
        return ''
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            all_lines = f.readlines()
        return ''.join(all_lines[-lines:])
    except Exception as e:
        return f'(failed to read log: {e})'


def enqueue_web_command(command: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Queue a command for a future Actions worker (v1 UI does not call this).
    Allowed names are whitelisted.
    """
    allowed = {
        'dry_run', 'yahoo_batch', 'yahoo_full', 'pause_buys', 'resume_buys',
    }
    if command not in allowed:
        return {'ok': False, 'error': f'command not allowed: {command}'}
    init_database()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        '''
        INSERT INTO web_commands (command, payload_json, status, created_at)
        VALUES (?, ?, 'pending', ?)
        ''',
        (
            command,
            json.dumps(payload) if payload is not None else None,
            datetime.now().isoformat(timespec='seconds'),
        )
    )
    cmd_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return {'ok': True, 'id': cmd_id, 'command': command, 'status': 'pending'}
