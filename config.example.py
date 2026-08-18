"""
Configuration template for stock trading system.
Copy to config.py and fill in your Schwab credentials.
config.py is gitignored — never commit real keys.
"""

# Schwab API Credentials
SCHWAB_API_KEY = "your_api_key_here"
SCHWAB_API_SECRET = "your_api_secret_here"
SCHWAB_REDIRECT_URI = "https://127.0.0.1"
# Legacy single-user token path (migrated to tokens_<username>.db on first run)
SCHWAB_TOKENS_DB = "~/.schwabdev/tokens.db"
# Per-user Schwab token files live here as tokens_<username>.db
SCHWAB_TOKENS_DIR = "~/.schwabdev"
# Refresh token lifetime (Schwab / schwabdev); full browser login required after this.
SCHWAB_REFRESH_TOKEN_DAYS = 7
# Dashboard banner + Actions urgent highlight when refresh expires within this many hours.
SCHWAB_AUTH_WARN_HOURS = 48
# Banner "No" snooze duration (hours); client-side localStorage.
SCHWAB_AUTH_SNOOZE_HOURS = 4

# Database Configuration
DATABASE_PATH = "market_data.db"
# Yahoo fundamentals live in a side DB so long Yahoo batches don't lock trading/UI.
FUNDAMENTALS_DATABASE_PATH = "market_fundamentals.db"
# When a due job fails (DB lock / transient), wake the loop again after this many seconds.
JOB_RETRY_SOON_SECONDS = 30

# Max age (days) before a ticker is past the staleness SLA (warn + prioritize).
MARKET_DATA_REFRESH_DAYS = 7

# Yahoo refresh job: each run fetches up to YAHOO_BATCH_SIZE oldest/missing tickers.
MARKET_DATA_JOB_INTERVAL_HOURS = 1
YAHOO_BATCH_SIZE = 150

# Yahoo fetch pacing / retry
YAHOO_FETCH_SLEEP_SECONDS = 2
YAHOO_RATE_LIMIT_WAIT_SECONDS = 600
YAHOO_MAX_RETRIES_PER_TICKER = 12
YAHOO_TRANSIENT_BACKOFF_SECONDS = 15
YAHOO_POST_THROTTLE_SLEEP_SECONDS = 5

# Trading Mode — True = log only, no Schwab orders; False = live orders
TRADE_DRY_RUN = True

# Algorithm evaluation era (set via: python main.py --mark-algorithm-start)
ALGORITHM_START = None
ALGORITHM_LEGACY_AT_START = []

DEBUG = False

# Trading Safety Rules
MINIMUM_LIQUIDATION_VALUE = 25000.0
MINIMUM_CASH = 10000.0  # Cash floor; live value is config.py → minimum_cash()

ORDER_AMOUNT_DOLLARS = 1000.0
WATCHLIST_JOB_INTERVAL_MINUTES = 30
WATCHLIST_JOB_INTERVAL_HOURS = 1

# Sell / risk management
# Legacy (unused). No sell/STOP_LIMIT until next ET calendar day after purchase.
MINIMUM_HOLD_HOURS = 16
SELL_CHECK_INTERVAL_MINUTES = 15
SCHWAB_SYNC_INTERVAL_MINUTES = 5
# If a job stays status=running longer than this, reclaim it so the loop can retry.
STALE_JOB_RUNNING_MINUTES = 5
POST_ORDER_SYNC_DELAY_SECONDS = 3
POSITIONS_SYNC_MIN_INTERVAL_SECONDS = 45

MARKET_TIMEZONE = 'America/New_York'
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 30
MARKET_CLOSE_HOUR = 16
MARKET_CLOSE_MINUTE = 0

TRAIL_ACTIVATE_PCT = 0.10
TRAIL_BUFFER_PCT = 0.10
TRAIL_BUFFER_OFF_WATCHLIST_PCT = 0.07
HARD_STOP_ON_WATCHLIST_PCT = -0.15
HARD_STOP_OFF_WATCHLIST_PCT = -0.08
STOP_LIMIT_SLIPPAGE_PCT = 0.005
STOP_REPLACE_MIN_DOLLARS = 0.05
STOP_ORDER_DURATION = 'GOOD_TILL_CANCEL'

WATCHLIST_FILTER_NAME = 'safe'

# After a sell: rebuy only when cooldown OR discount unlocks (whichever first).
REBUY_COOLDOWN_TRADING_DAYS = 5   # weekdays after sell date (ET); holidays not excluded
REBUY_DISCOUNT_PCT = 0.05         # also unlock if price <= last_sell * (1 - this)

CASH_BALANCE_FIELD = "cashBalance"

DEFAULT_START_DATE = "2020-01-01"
DEFAULT_END_DATE = None
LIMITED_TICKER_LIST = None

DEFAULT_SECTOR = None
DEFAULT_MAX_PE_RATIO = None
DEFAULT_MIN_MARKET_CAP = None

# Local web dashboard (FastAPI)
WEB_HOST = '127.0.0.1'
WEB_PORT = 8787
WEB_LOG_PATH = 'logs/trader.log'
WEB_CORS_ORIGINS = ''
WEB_FETCH_OPEN_ORDERS = False
# In-app login session lifetime (days)
SESSION_DAYS = 30
# First-run owner (only when users table is empty). Prefer --create-user after that.
BOOTSTRAP_USERNAME = 'jame'
BOOTSTRAP_PASSWORD = 'change-me'
BOOTSTRAP_DISPLAY_NAME = 'Jame'
# View-only login: sees this user's live dashboard; Action buttons are no-ops.
# Leave password empty to disable. Must differ from the owner's real password.
DEMO_VIEWER_USERNAME = 'jame'
DEMO_VIEWER_PASSWORD = ''

# WATCHLIST_FILTER_NAME seeds each user's active_filter on first create.
# New users always start with trade_dry_run=1 (Dry Run), regardless of TRADE_DRY_RUN.
# Floors / buy size start NULL until Account setup (Schwab → bounds from live cash).
# Loops for a user start only after Algorithm → Run (algorithm_start). Then Go live.
# After that, per-user values in the DB win (not switched from the webpage).
