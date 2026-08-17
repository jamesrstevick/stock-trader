# Jame Trader

An always-on trading assistant: it refreshes market fundamentals from Yahoo, maintains a filtered watchlist, proposes buys under cash floors, and watches open positions with trail / hard-stop rules — with dry-run as the default safety net.

## How it runs

On your home machine (or Pi), `python main.py --loop` wakes on a schedule:

- **Yahoo refresh** — oldest / missing tickers first (~150/hour), skips names already updated today, keeps the universe roughly within a few days.
- **Watchlist + buys** — re-ranks with the active filter (default: `safe`), then considers ~$1k buys when cash rules allow.
- **Sell check** — every ~15 minutes: arm trails after +10%, ratchet stops, hard stops when the thesis (watchlist) changes.

A separate FastAPI dashboard (`python web_app.py`) uses **in-app login** (username/password, ~30-day session). Each user has their own Schwab link, filter, watchlist, positions, and log. Yahoo fundamentals are shared. Live data comes from the local API, or mock fixtures with `?mock=1`.

## Trading rules (high level)

1. **Dry-run by default** — `TRADE_DRY_RUN = True` logs what would happen; no Schwab orders until you flip it.
2. **Regular hours only** for buys/sells — 9:30–16:00 America/New_York.
3. **Hold ≥ 16 hours** after purchase (avoid same-day round-trips).
4. **Cash floor** and **account size floor** block buys that would breach them; sells still run.
5. **Buys only from the active watchlist filter**, sized ~$1,000 per new name.
6. **Trailing stop** arms at +10% peak; buffers tighten once off-watchlist; hard stops as catastrophe floor.
7. **Resting STOP_LIMIT** at Schwab so protection remains if the bot is briefly offline.

Exact values come from `config.py` and show live on the About page.

## Dashboard

| Page | Purpose |
|------|---------|
| **Trader** | Positions, orders, algo scorecard + next tasks, performance vs S&P 500 |
| **Log** | Trading story by default (Buy / Sell / Watchlist); Tasks pill for scheduler noise |
| **Actions** | **Schwab reconnect** (paste-back OAuth) and future remote controls |
| **About** | System overview, active filter, jobs, trading rules |

## Setup

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy config.example.py config.py   # or: cp config.example.py config.py
```

Edit `config.py` with your Schwab API credentials. Keep `TRADE_DRY_RUN = True` until you intentionally go live.

```bash
# One shot (Windows) — opens three windows: loop, dashboard, cloudflared tunnel
.\start.ps1
# or: start.bat
# skip tunnel: .\start.ps1 -NoTunnel

# Dell always-on host (auto-restart + Pull from GitHub button):
.\supervisor.ps1

# Or manually:
# Terminal A — bot
python main.py --loop

# Terminal B — dashboard
python web_app.py

# Terminal C — Cloudflare tunnel (optional, for trader.traderjame.com)
cloudflared tunnel run stock-trader
```

Open `http://127.0.0.1:8787/` — sign in (default owner `jame` is created on first DB init). Offline UI: `http://127.0.0.1:8787/?mock=1`.

Add another user (e.g. your brother):

```bash
python main.py --create-user brother "their-password" --display Brother
```

Set that user’s active filter / dry-run in the DB (`user_settings`) or ask for a small admin helper later — the webpage filter dropdown is view-only.

### Useful one-shots

```bash
python main.py --dry-run                 # full-system preview, never places orders
python main.py --yahoo-full              # Yahoo catch-up (all tickers, oldest-first)
python main.py --mark-algorithm-start    # soft reset: snapshot equity + enroll holdings
```

### Daily backtest (research side script)

Rough once-per-day stand-in for trail/hard-stop + cash rules, frozen from today's filter universe, compared to SPY across many start dates. Does not place trades or touch the live loop.

```bash
python backtest_daily.py
python backtest_daily.py --start 2020-01-01 --stride-days 21 --cash 50000 --max-tickers 40
```

Results: console aggregate + `data/backtest_results/summary.csv` (and `equity_worst.csv`). Yahoo OHLC is cached under `data/backtest_cache/`.

### Schwab reconnect (Actions)

Schwab refresh tokens last ~**7 days**. Access tokens refresh automatically until then; after expiry the bot skips live trades until you log in again.

From the dashboard (local or via tunnel):

1. Open **Actions → Schwab reconnect** (or tap **Yes** on the warning banner).
2. **Open Schwab login** → approve in the new tab.
3. Schwab redirects to `https://127.0.0.1/?code=…` (page may fail — copy the full URL from the address bar within ~30 seconds).
4. Paste into the field and **Submit**. The Pi exchanges the code and writes `~/.schwabdev/tokens.db`; the bot hot-reloads.

Within `SCHWAB_AUTH_WARN_HOURS` (default **48**), a site banner asks to reconnect; **No** snoozes it for `SCHWAB_AUTH_SNOOZE_HOURS` (default **4**). The Actions nav badge stays visible while warning even if snoozed. You can reconnect early anytime to reset the 7-day clock.

Schwab reconnect requires a signed-in session (each user’s tokens stay in their own `tokens_<username>.db`).

### Remote access (optional)

Keep the bind on `127.0.0.1` and reach the dashboard via your LAN or a mesh VPN (e.g. Tailscale). Do **not** port-forward the dashboard to the open internet without extra hardening.

### Dell always-on host

On the Dell you do **not** need Cursor. Double-click **`setup_host.bat`** in the repo folder (File Explorer). It installs Python 3.12 if needed, creates `.venv`, installs packages, and starts the supervisor.

First time only: copy `config.py` from your Surface into that same folder (or let the script create one from `config.example.py` and paste your Schwab keys). Windows: **Sleep → Never**. Leave the setup/supervisor window open.

Later deploys: push from the Surface, then **Actions → Dell host → Pull from GitHub**.

To start at logon: `.\install_host_startup.ps1` then `Start-ScheduledTask -TaskName JameTraderHost`.


## Project layout

```
stock-trader/
├── setup_host.bat / setup_host.ps1  # Dell: double-click to install + run
├── start.ps1 / start.bat  # Dev launch: loop + dashboard + tunnel
├── supervisor.ps1         # Dell host: auto-restart + git pull commands
├── install_host_startup.ps1  # Scheduled Task: supervisor at logon
├── host_control.py     # Dashboard ↔ supervisor command/status files
├── config.example.py   # Template (copy to config.py — not committed)
├── config.py           # Local secrets & knobs (gitignored)
├── stock_trader.py     # Core library
├── main.py             # Bot entry point
├── web_app.py          # FastAPI dashboard
├── backtest_daily.py   # Side CLI: daily multi-start backtest vs SPY
├── backtest/           # Backtest engine (not used by live loop)
├── test_functions.py   # Function-level tests
├── web/static/         # About / Trader / Log / Actions UI
└── requirements.txt
```

## License

Personal use. Trading with real money is at your own risk.
