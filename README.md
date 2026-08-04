# Jame Trader

An always-on trading assistant: it refreshes market fundamentals from Yahoo, maintains a filtered watchlist, proposes buys under cash floors, and watches open positions with trail / hard-stop rules — with dry-run as the default safety net.

## How it runs

On your home machine (or Pi), `python main.py --loop` wakes on a schedule:

- **Yahoo refresh** — oldest / missing tickers first (~150/hour), skips names already updated today, keeps the universe roughly within a few days.
- **Watchlist + buys** — re-ranks with the active filter (default: `safe`), then considers ~$1k buys when cash rules allow.
- **Sell check** — every ~15 minutes: arm trails after +10%, ratchet stops, hard stops when the thesis (watchlist) changes.

A separate FastAPI dashboard (`python web_app.py`) is read-only in v1. Live data comes from the local API (optionally via Cloudflare Tunnel), or mock fixtures with `?mock=1`.

## Trading rules (high level)

1. **Dry-run by default** — `TRADE_DRY_RUN = True` logs what would happen; no Schwab orders until you flip it.
2. **Regular hours only** for buys/sells — 9:30–16:00 America/New_York.
3. **Hold ≥ 16 hours** after purchase (avoid same-day round-trips).
4. **Cash floor** and **account size floor** block trades that would breach them.
5. **Buys only from the active watchlist filter**, sized ~$1,000 per new name.
6. **Re-buy debounce** after a sell (e.g. ≤ last sell − 5%).
7. **Trailing stop** arms at +10% peak; buffers tighten once off-watchlist; hard stops as catastrophe floor.
8. **Resting STOP_LIMIT** at Schwab so protection remains if the bot is briefly offline.

Exact values come from `config.py` and show live on the About page.

## Dashboard

| Page | Purpose |
|------|---------|
| **About** | System overview, active filter, jobs, trading rules |
| **Trader** | Positions, orders, algo scorecard + next tasks, performance vs S&P 500 |
| **Log** | Trading story by default (Buy / Sell / Watchlist); Tasks pill for scheduler noise |
| **Actions** | Placeholder for future remote controls |

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
# Terminal A — bot
python main.py --loop

# Terminal B — dashboard
python web_app.py
```

Open `http://127.0.0.1:8787/`. Offline UI: `http://127.0.0.1:8787/?mock=1`.

### Useful one-shots

```bash
python main.py --dry-run                 # full-system preview, never places orders
python main.py --yahoo-full              # Yahoo catch-up (all tickers, oldest-first)
python main.py --mark-algorithm-start    # soft reset: snapshot equity + enroll holdings
```

### Cloudflare Tunnel (optional)

Point a tunnel at `http://127.0.0.1:8787`, put Cloudflare Access in front, and do **not** port-forward. Bind stays `127.0.0.1`.

## Project layout

```
stock-trader/
├── config.example.py   # Template (copy to config.py — not committed)
├── config.py           # Local secrets & knobs (gitignored)
├── stock_trader.py     # Core library
├── main.py             # Bot entry point
├── web_app.py          # FastAPI dashboard
├── test_functions.py   # Function-level tests
├── web/static/         # About / Trader / Log / Actions UI
└── requirements.txt
```

## License

Personal use. Trading with real money is at your own risk.
