# Paths under `packages/` whose changes CANNOT affect GA / backtest results.
#
# `tools/check_package_versions.py` requires that a diff touching `packages/<pkg>/` (a) bumps that
# package's PACKAGE_VERSION, and (b) EITHER raises `testplatform/required_package_versions.py` for
# the package (or bumps TEST_APP_VERSION) OR touches only paths matched here. Listing a path is
# therefore the reviewed, in-diff statement "workers need not take this change".
#
# HOW TO ADD: append a glob (fnmatch syntax, `/` separators, `*` also crosses `/`) in a REVIEWED
# change. The guard judges the same diff with the BASE's list, so the edit itself needs
# `--allow-neutral-change` by hand; in CI the `ga-neutral-reviewed` PR label, or a
# `GA-Neutral-Reviewed: <reason>` trailer in a pushed commit message. In the commit message say in the commit message why the code is unreachable from the GA /
# backtest path (e.g. broker-only module never imported by the backtest engine). Prefer a narrow
# glob for one module over a wide one; never list a module the backtest engine imports.
#
# Read by TEXT (ast.literal_eval), never imported: keep it a plain list of str literals.
GA_NEUTRAL_GLOBS = [
    "packages/common/ba2_common/core/ibkr_*.py",
    "packages/common/ba2_common/core/protective_legs.py",
    "packages/common/tests/test_ibkr_*.py",
    # ML forecast contract (milestone 1): nothing imports it yet. REMOVE this glob when MLForecast is wired into the GA (milestone 3).
    "packages/common/ba2_common/core/ml_forecast/*",
    # PennyMomentumTrader live price fetch (vendor_live_price=fmp): intraday live trader, never run by GA/backtest.
    "packages/experts/ba2_experts/PennyMomentumTrader/data_gathering.py",
    # Alpaca history LIVE top-up / backfill / corporate-actions refresh (plan 2026-10-24-history-source-task11): modules that
    # exist only behind account API keys. The backtest builds AlpacaHistoryOHLCVProvider WITHOUT keys (cache-only), where
    # every one of these is unreachable. (live_state.py / live_backfill.py are NOT listed: the backtest read path calls them; nor AlpacaHistoryOHLCVProvider.py: its keyless read now refuses a
    # window before a partial live backfill, and PandasIndicatorCalc raises InsufficientHistory -> ba2_providers minimum raised.)
    "packages/providers/ba2_providers/ohlcv/alpaca_history/live_fetch.py",
    "packages/providers/ba2_providers/ohlcv/alpaca_history/live_topup.py",
    "packages/providers/ba2_providers/ohlcv/alpaca_history/corporate_actions.py",
    # Tests, docs and READMEs never ship to GA code paths.
    "packages/*/tests/*",
    "packages/*/test_files/*",
    "packages/*/docs/*",
    "packages/*/README.md",
    "packages/*/*.md",
]
