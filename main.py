"""
Main entry point script for stock trading system.

Usage:
  python main.py                     # run due jobs once (TRADE_DRY_RUN=True => log only, no Schwab orders)
  python main.py --dry-run           # one-shot full-system preview (never places orders)
  python main.py --loop              # always-on mode (periodic jobs; respects TRADE_DRY_RUN)
  python main.py --yahoo-full        # one-shot Yahoo catch-up (all tickers, oldest-first)
  python main.py --mark-algorithm-start [--force]
                                     # soft reset: snapshot + enroll all holdings (scorecard excludes them)
  python web_app.py                  # local dashboard (separate process)
"""

import sys
import stock_trader as st


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    st.setup_file_logging()
    if '--dry-run' in argv or '-n' in argv:
        st.dry_run_system()
        return
    if '--yahoo-full' in argv:
        # batch_size=0 => no cap (full universe, oldest-first)
        st.refresh_market_data(batch_size=0)
        return
    if '--mark-algorithm-start' in argv:
        st.mark_algorithm_start(force='--force' in argv)
        return
    once = '--loop' not in argv
    st.run_trader(once=once)


if __name__ == "__main__":
    main()
