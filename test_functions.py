"""
Individual function testing examples.
Each test can be run independently to verify functionality.
"""

import stock_trader as st
import config

def _print_dict_all(d, indent=0):
    """Print every key/value in a dict; recurse into nested dicts so all fields are visible."""
    prefix = "  " * indent
    for key, value in d.items():
        if isinstance(value, dict):
            print(f"{prefix}{key}: (dict)")
            _print_dict_all(value, indent + 1)
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            print(f"{prefix}{key}: (list of {len(value)} dicts)")
            for i, item in enumerate(value[:3]):  # first 3 only to avoid huge output
                print(f"{prefix}  [{i}]:")
                _print_dict_all(item, indent + 2)
            if len(value) > 3:
                print(f"{prefix}  ... and {len(value) - 3} more")
        else:
            print(f"{prefix}{key}: {value}")


def test_schwab_single_stock():
    """Test Schwab data on a single stock. Shows parsed fields then full raw_data from API."""
    print("=" * 60)
    print("Test: Get Schwab streaming data for a single stock")
    print("=" * 60)
    
    ticker = 'AAPL'
    data = st.get_streaming_data(ticker)
    if not data:
        print("No data returned.")
        return
    # Parsed/result dict (includes raw_data as one key - value is the full API response)
    print(f"\nParsed streaming data for {ticker} (result dict keys):")
    for key, value in data.items():
        if key == 'raw_data':
            print(f"  {key}: <full dict below>")
        else:
            print(f"  {key}: {value}")
    # Full raw_data packet from Schwab - every field (so display above is NOT all fields from raw_data)
    raw = data.get('raw_data')
    if raw is not None:
        print("\n--- Raw data from Schwab (all fields in the API response) ---")
        _print_dict_all(raw)
        print("---")
    else:
        print("\n(No raw_data in response)")

def test_list_all_yfinance_fields():
    """List all available yfinance fields for review."""
    print("\n" + "=" * 60)
    print("Test: List all available yfinance fields")
    print("=" * 60)
    
    ticker = 'AAPL'
    all_fields = st.list_available_yfinance_fields(ticker)
    print(len(all_fields))
    for key, value in all_fields.items():
        print(f'{key}: {value}')
    

def test_list_yfinance_fields():
    """List all available yfinance fields for review."""
    print("\n" + "=" * 60)
    print("Test: List all available yfinance fields")
    print("=" * 60)
    
    ticker = 'AAPL'
    all_fields = st.list_available_yfinance_fields(ticker)
    
    print(f"\nAll available fields from yfinance for {ticker}:")
    print(f"Total fields: {len(all_fields)}\n")
    
    # Currently stored fields
    stored_fields = [
        'pe_ratio', 'market_cap', 'sector', 'avg_volume', 
        'beta', 'short_float', 'current_price', 'target_price'
    ]
    
    yfinance_to_stored = {
        'trailingPE': 'pe_ratio',
        'marketCap': 'market_cap',
        'sector': 'sector',
        'averageVolume': 'avg_volume',
        'beta': 'beta',
        'shortPercentOfFloat': 'short_float',
        'currentPrice': 'current_price',
        'targetMeanPrice': 'target_price'
    }
    
    print("=" * 60)
    print("CURRENTLY STORED FIELDS:")
    print("=" * 60)
    for yf_field, stored_name in yfinance_to_stored.items():
        value = all_fields.get(yf_field, 'N/A')
        print(f"  {stored_name:20} <- {yf_field:25} = {value}")
    
    print("\n" + "=" * 60)
    print("OTHER POTENTIALLY USEFUL FIELDS:")
    print("=" * 60)
    
    useful_fields = {
        # Valuation metrics
        'forwardPE': 'Forward P/E ratio',
        'pegRatio': 'PEG ratio (P/E to growth)',
        'priceToBook': 'Price to book ratio',
        'priceToSalesTrailing12Months': 'Price to sales ratio',
        'enterpriseToRevenue': 'Enterprise value to revenue',
        'enterpriseToEbitda': 'Enterprise value to EBITDA',
        
        # Financial health
        'debtToEquity': 'Debt to equity ratio',
        'currentRatio': 'Current ratio (liquidity)',
        'quickRatio': 'Quick ratio',
        'totalCash': 'Total cash',
        'totalDebt': 'Total debt',
        'totalRevenue': 'Total revenue',
        'grossProfits': 'Gross profits',
        'freeCashflow': 'Free cash flow',
        'operatingCashflow': 'Operating cash flow',
        
        # Growth metrics
        'revenueGrowth': 'Revenue growth rate',
        'earningsGrowth': 'Earnings growth rate',
        'earningsQuarterlyGrowth': 'Quarterly earnings growth',
        'profitMargins': 'Profit margins',
        'grossMargins': 'Gross margins',
        'operatingMargins': 'Operating margins',
        
        # Dividends
        'dividendRate': 'Annual dividend rate',
        'dividendYield': 'Dividend yield',
        'payoutRatio': 'Payout ratio',
        'exDividendDate': 'Ex-dividend date',
        
        # Market data
        '52WeekHigh': '52-week high price',
        '52WeekLow': '52-week low price',
        'fiftyTwoWeekHigh': '52-week high (alternate)',
        'fiftyTwoWeekLow': '52-week low (alternate)',
        'dayHigh': 'Day high',
        'dayLow': 'Day low',
        'previousClose': 'Previous close price',
        'open': 'Opening price',
        'volume': 'Current volume',
        'averageVolume10days': '10-day average volume',
        'averageVolume': 'Average volume',
        
        # Company info
        'industry': 'Industry',
        'fullTimeEmployees': 'Number of employees',
        'website': 'Company website',
        'longName': 'Full company name',
        'exchange': 'Stock exchange',
        'currency': 'Currency',
        
        # Analyst data
        'recommendationMean': 'Analyst recommendation (1-5)',
        'recommendationKey': 'Recommendation key',
        'numberOfAnalystOpinions': 'Number of analyst opinions',
        'targetHighPrice': 'High target price',
        'targetLowPrice': 'Low target price',
        'targetMeanPrice': 'Mean target price (already stored)',
        
        # Ownership
        'heldPercentInsiders': 'Percent held by insiders',
        'heldPercentInstitutions': 'Percent held by institutions',
        'sharesOutstanding': 'Shares outstanding',
        'floatShares': 'Float shares',
        'sharesShort': 'Shares short',
        'shortRatio': 'Short ratio',
        
        # Other metrics
        'bookValue': 'Book value',
        'priceToBook': 'Price to book',
        'enterpriseValue': 'Enterprise value',
        'ebitda': 'EBITDA',
        'returnOnAssets': 'Return on assets',
        'returnOnEquity': 'Return on equity',
    }
    
    for field, description in useful_fields.items():
        if field in all_fields:
            value = all_fields[field]
            print(f"  {field:35} = {value:15} ({description})")
    
    print("\n" + "=" * 60)
    print("To see ALL fields, run:")
    print(f"  all_fields = st.list_available_yfinance_fields('{ticker}')")
    print("  for key, value in all_fields.items():")
    print("      print(f'{key}: {value}')")
    print("=" * 60)


def test_yfinance_data():
    """Test yfinance data fetching."""
    print("\n" + "=" * 60)
    print("Test: Fetch stock data from yfinance")
    print("=" * 60)
    
    ticker = 'AAPL'
    data, error_type = st.fetch_stock_data(ticker)
    if data:
        print(f"Data for {ticker}:")
        print(f"  P/E Ratio: {data['pe_ratio']}")
        print(f"  Market Cap: {data['market_cap']}")
        print(f"  Sector: {data['sector']}")
        print(f"  Price data shape: {data['price_data'].shape}")
        print(f"  Latest close: {data['price_data']['Close'].iloc[-1] if not data['price_data'].empty else 'N/A'}")
    else:
        print(f"Error fetching data for {ticker}: {error_type}")


def test_database_query():
    """Test database queries."""
    print("\n" + "=" * 60)
    print("Test: Query database")
    print("=" * 60)
    
    # Initialize database first
    st.init_database()
    
    # Example query
    query = "SELECT * FROM fundamentals WHERE ticker='AAPL'"
    df = st.query_stocks(query)
    
    if not df.empty:
        print("Query results:")
        print(df)
    else:
        print("No data found. Run st.populate_database() first.")


def test_account_info():
    """Test account info retrieval."""
    print("\n" + "=" * 60)
    print("Test: Get account info from Schwab")
    print("=" * 60)
    
    account_data = st.get_account_info()
    print("Account info:")
    print(f"  Cash: ${account_data.get('cash', 0):,.2f}")
    print(f"  Total Value: ${account_data.get('total_value', 0):,.2f}")
    print(f"  Liquidation Value: ${account_data.get('liquidation_value', 0):,.2f}")
    print(f"  Round Trips (Day Trades): {account_data.get('round_trips', 0)}/3 (5-day rolling window)")
    positions = account_data.get('positions', {})
    positions_int = {t: int(q) for t, q in positions.items()}
    print(f"  Positions: {positions_int}")
    
    # Test trading safety check
    is_allowed, reason = st.is_trading_allowed(account_data)
    print(f"\nTrading Safety Check:")
    print(f"  Allowed: {is_allowed}")
    print(f"  Reason: {reason}")


def test_buy_criteria():
    """Test buy criteria function."""
    print("\n" + "=" * 60)
    print("Test: Check buy criteria")
    print("=" * 60)
    
    ticker = 'AAPL'
    streaming_data = st.get_streaming_data(ticker)
    account_data = st.get_account_info()
    
    should_buy = st.check_buy_criteria(ticker, streaming_data, account_data)
    print(f"Should buy {ticker}? {should_buy}")
    print(f"  Streaming data: {streaming_data}")
    print(f"  Account cash: ${account_data.get('cash', 0):,.2f}")


def test_sell_criteria():
    """Test sell criteria function."""
    print("\n" + "=" * 60)
    print("Test: Check sell criteria")
    print("=" * 60)
    
    ticker = 'AAPL'
    streaming_data = st.get_streaming_data(ticker)
    account_data = st.get_account_info()
    
    should_sell = st.check_sell_criteria(ticker, streaming_data, account_data)
    print(f"Should sell {ticker}? {should_sell}")
    print(f"  Streaming data: {streaming_data}")
    positions = account_data.get('positions', {})
    print(f"  Positions: { {t: int(q) for t, q in positions.items()} }")


def test_get_fundamentals():
    """Test getting fundamentals from database."""
    print("\n" + "=" * 60)
    print("Test: Get fundamentals from database")
    print("=" * 60)
    
    ticker = 'AAPL'
    fundamentals = st.get_fundamentals(ticker)
    
    if fundamentals:
        print(f"Fundamentals for {ticker}:")
        for key, value in fundamentals.items():
            print(f"  {key}: {value}")
    else:
        print(f"No fundamentals found for {ticker}. Run st.populate_database() first.")


def test_get_price_history():
    """Test getting price history from database."""
    print("\n" + "=" * 60)
    print("Test: Get price history from database")
    print("=" * 60)
    
    ticker = 'AAPL'
    price_history = st.get_price_history(ticker, start_date='2024-01-01')
    
    if not price_history.empty:
        print(f"Price history for {ticker} (last 5 rows):")
        print(price_history.tail())
    else:
        print(f"No price history found for {ticker}. Run st.populate_database() first.")


def test_filter_stocks():
    """Test filtering stocks by criteria."""
    print("\n" + "=" * 60)
    print("Test: Filter stocks by criteria")
    print("=" * 60)
    
    criteria = {
        'sector': 'Technology',
        'max_pe_ratio': 30
    }
    
    filtered = st.filter_stocks(criteria)
    print(f"Stocks matching criteria {criteria}:")
    print(filtered)


def test_risky_filter():
    """Test the risky filter function (ranked by analyst upside; optional limit)."""
    print("\n" + "=" * 60)
    print("Test: Risky filter (high beta, short squeeze, analyst upside)")
    print("=" * 60)
    
    watchlist = st.risky_filter_stocks(
        min_market_cap=2000000000,      # $2B
        min_avg_volume=1000000,         # 1M shares/day
        min_price=5.0,                  # $5 minimum
        min_beta=1.5,                   # 50% more volatile than market
        min_short_float=0.15,           # 15% short interest
        min_analyst_upside=0.20,         # 20% analyst upside
        limit=10,                       # top 10 by analyst upside
    )
    
    print(f"--- RISKY WATCHLIST ({len(watchlist)} matches, limit=10) ---")
    print(watchlist)


def test_safe_filter():
    """Test the safe filter function (ranked by analyst upside; optional limit)."""
    print("\n" + "=" * 60)
    print("Test: Safe filter (mega cap, stable, low short interest)")
    print("=" * 60)
    
    watchlist = st.safe_filter_stocks(
        min_market_cap=25000000000,     # $25B
        min_beta=0.6,                   # Beta between 0.6 and 1.3
        max_beta=1.3,
        max_short_float=0.1,            # Less than 10% shorted
        min_pe_ratio=0.0,               # Must be profitable
        max_pe_ratio=35.0,              # Not overpriced
        min_peg_ratio=0.0,              # Minimum PEG ratio
        max_peg_ratio=2.0,              # Maximum PEG ratio
        min_analyst_upside=0.15,        # 15% analyst upside
        limit=10,                       # top 10 by analyst upside
    )
    
    print(f"--- SAFE GIANTS WATCHLIST ({len(watchlist)} matches) ---")
    print(watchlist)


def test_execute_trade():
    """Test executing a trade (simulation mode)."""
    print("\n" + "=" * 60)
    print("Test: Execute trade (simulation mode)")
    print("=" * 60)
    
    ticker = 'DELL'
    quantity = 1
    
    print(f"TRADE_DRY_RUN={config.TRADE_DRY_RUN} "
          f"({'no Schwab orders' if config.TRADE_DRY_RUN else 'LIVE orders'})")
    st.execute_buy(ticker, quantity)
    # st.execute_sell(ticker, quantity)


def test_open_orders():
    """Test retrieving open orders from Schwab."""
    print("\n" + "=" * 60)
    print("Test: Get open orders from Schwab")
    print("=" * 60)
    
    st.display_open_orders()


def test_schwab_single_position():
    """Show all info from Schwab API for one position. Picks one stock from your positions (or first from API)."""
    print("\n" + "=" * 60)
    print("Test: Schwab API – full position object for one stock")
    print("=" * 60)
    
    # Prefer a ticker from our positions table so we show "your" position
    pick_ticker = None
    conn = st.get_connection()
    cur = conn.cursor()
    cur.execute("SELECT ticker FROM positions WHERE shares_owned > 0 LIMIT 1")
    row = cur.fetchone()
    conn.close()
    if row:
        pick_ticker = row[0]
    
    positions_data = st.get_schwab_positions_raw()
    if not positions_data:
        print("No positions returned from Schwab API.")
        return
    
    # Find the position to show: match pick_ticker if we have one, else first
    chosen = None
    for p in positions_data:
        inst = p.get('instrument', {})
        sym = inst.get('symbol')
        if pick_ticker and sym == pick_ticker:
            chosen = p
            break
    if chosen is None:
        chosen = positions_data[0]
        sym = chosen.get('instrument', {}).get('symbol', '?')
        print(f"No local position matched; showing first position from API: {sym}\n")
    else:
        print(f"Showing full API data for position: {pick_ticker}\n")
    
    print("--- Raw position object from Schwab (all fields) ---")
    _print_dict_all(chosen)
    print("---")


def test_positions():
    """Sync positions from Schwab, then display positions table (same as last section of main.py)."""
    print("\n" + "=" * 60)
    print("Positions")
    print("=" * 60)
    print("Syncing from Schwab...")
    n = st.fetch_and_sync_schwab_positions()
    print(f"Synced {n} position(s) from Schwab.\n")
    st.display_positions_table()


def run_all_tests():
    """Run all test functions."""
    print("Running all tests...\n")
    
    test_schwab_single_stock()
    test_schwab_single_position()
    test_yfinance_data()
    test_database_query()
    test_account_info()
    test_buy_criteria()
    test_sell_criteria()
    test_get_fundamentals()
    test_get_price_history()
    test_filter_stocks()
    test_risky_filter()
    test_safe_filter()
    test_execute_trade()
    
    print("\n" + "=" * 60)
    print("All tests complete!")
    print("=" * 60)


def test_refresh_market_data():
    """Run Yahoo full-market refresh job alone (long-running)."""
    print("\n" + "=" * 60)
    print("Test: refresh_market_data()")
    print("=" * 60)
    ok = st.refresh_market_data()
    print(f"Completed successfully: {ok}")


def test_run_trader():
    """Run parent scheduler once (batch); runs due jobs only."""
    print("\n" + "=" * 60)
    print("Test: run_trader(once=True)")
    print("=" * 60)
    st.run_trader(once=True)
    job = st.get_job_run(st.JOB_REFRESH_MARKET_DATA)
    print(f"job_runs[{st.JOB_REFRESH_MARKET_DATA}]: {job}")


def test_propose_trades():
    """Dry-run: what would we buy/sell today and why (no orders)."""
    print("\n" + "=" * 60)
    print("Test: propose_trades() / print_trade_proposals()")
    print("=" * 60)
    print(f"TRADE_DRY_RUN={config.TRADE_DRY_RUN}")
    st.print_trade_proposals()


def test_dry_run_system():
    """Full one-pass dry-run of active system (jobs + buys + stops/sells)."""
    print("\n" + "=" * 60)
    print("Test: dry_run_system()")
    print("=" * 60)
    st.dry_run_system()


def test_buy_pass_dry_run():
    """Buy pass forced dry-run."""
    print("\n" + "=" * 60)
    print("Test: run_buy_pass(dry_run=True)")
    print("=" * 60)
    st.run_buy_pass(dry_run=True)


def test_sell_pass_dry_run():
    """Sell/trail pass forced dry-run."""
    print("\n" + "=" * 60)
    print("Test: run_sell_pass(dry_run=True)")
    print("=" * 60)
    st.run_sell_pass(dry_run=True)


if __name__ == "__main__":

    # Populate / refresh (long-running):
    # st.refresh_market_data()
    # st.populate_database()

    # Run individual test or all tests
    # Uncomment the test you want to run:
    
    # test_schwab_single_stock()
    # test_schwab_single_position()
    # test_yfinance_data()
    # test_list_yfinance_fields()
    # test_list_all_yfinance_fields()
    # test_database_query()
    # test_account_info()
    # test_buy_criteria()
    # test_sell_criteria()
    # test_get_fundamentals()
    # test_get_price_history()
    # test_filter_stocks()
    # test_risky_filter()
    # test_safe_filter()
    # test_execute_trade()
    # test_open_orders()
    # test_refresh_market_data()
    # test_run_trader()
    # test_propose_trades()
    # test_dry_run_system()
    # test_buy_pass_dry_run()
    # test_sell_pass_dry_run()
    test_positions()
    
    # Or run all tests:
    # run_all_tests()
