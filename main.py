"""
Main entry point script for stock trading system.

Usage:
  python main.py                     # run due jobs once (TRADE_DRY_RUN=True => log only, no Schwab orders)
  python main.py --dry-run           # one-shot full-system preview (never places orders)
  python main.py --loop              # always-on mode (periodic jobs; respects TRADE_DRY_RUN)
  python main.py --yahoo-full        # one-shot Yahoo catch-up (all tickers, oldest-first)
  python main.py --mark-algorithm-start [--force]
                                     # soft reset: snapshot + enroll all holdings (scorecard excludes them)
  python main.py --create-user NAME PASS [--display NAME] [--admin]
                                     # add a dashboard/trading user
  python web_app.py                  # local dashboard (separate process)

Scheduled runs (--loop / default once) keep the terminal relatively quiet: short
status lines (which job / user) still print; verbose dumps go to the web Log and
logs/trader.log. CLI one-shots (--dry-run, --yahoo-full, etc.) stay fully verbose.
"""

import sys
import stock_trader as st
import user_context as uc


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    st.setup_file_logging()
    if '--create-user' in argv:
        i = argv.index('--create-user')
        rest = argv[i + 1:]
        if len(rest) < 2:
            print('Usage: python main.py --create-user USERNAME PASSWORD [--display NAME] [--admin]')
            sys.exit(2)
        username = rest[0]
        password = rest[1]
        display = username
        if '--display' in rest:
            di = rest.index('--display')
            if di + 1 < len(rest):
                display = rest[di + 1]
        st.init_database()
        result = uc.create_user(
            username,
            password,
            display_name=display,
            is_admin=('--admin' in rest),
        )
        print(result)
        sys.exit(0 if result.get('ok') else 1)
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
    if not once and not st.acquire_trader_loop_lock():
        # Second --loop would fight over market_data.db and stall Schwab/jobs.
        sys.exit(1)
    # Relatively quiet: status lines still show; verbose job dumps go to the log.
    st.print_loop_status(
        'Trader %s (status lines on) — details in web Log / logs/trader.log'
        % ('loop' if not once else 'pass')
    )
    st.set_console_quiet(True)
    try:
        st.run_trader(once=once)
    finally:
        st.set_console_quiet(False)


if __name__ == "__main__":
    main()
