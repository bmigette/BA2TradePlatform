"""CACHE-ONLY in-place add of the ``atr_<period>`` columns to an existing screener metric store.

The GA/optimize trial-worker path (test_files/run_screener_capband_matrix.py) has no hermetic
route to a live indicator provider for ATR-based risk sizing (TradeRiskManagement._risk_atr_quantity
-> position_sizing.get_latest_atr): the plain PandasIndicatorCalc fallback would either hit the
network mid-run or silently return no ATR, so the safeguard SL (synthesize_safeguard_stop) fell
back to its risk%-only floor with ATR never tightening it. This driver precomputes ATR (Wilder's
smoothing, one column per period in ba2_providers.screener.metric_store.ATR_PERIODS = 7/14/21/28,
matching the atr_period optimizer gene) into the SAME screener metric store already used for
universe screening, so MetricStoreATRProvider (testplatform/backend/app/services/backtest/
seam_wiring.py) can read it offline, as-of, with zero network. Adds the columns WITHOUT a full
store rebuild: reads each store symbol's daily OHLCV straight from the native parquet cache
(CACHE_FOLDER/FMPOHLCVProvider/<SYM>_1d.parquet), computes ATR, samples it as-of each existing
scan date, and writes it back (every other column untouched).

Usage (test venv):
    ba2-venvs/test/Scripts/python.exe test_files/recompute_screener_atr.py [--store <dir>]
"""
import argparse
import os
import sys

import pandas as pd


def main() -> int:
    import ba2_common.config as cfg
    from ba2_providers.screener import metric_store as ms

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--store", default=os.path.join(cfg.CACHE_FOLDER, "screener", "metric_store"),
                    help="Path to the parquet metric-store dir (default: the common screener store).")
    args = ap.parse_args()

    store = args.store
    if not os.path.isdir(store):
        sys.exit(f"recompute-atr: store not found: {store}")
    ohlcv_dir = os.path.join(cfg.CACHE_FOLDER, "FMPOHLCVProvider")

    def _ohlcv_cached(sym):
        # Direct parquet read = guaranteed offline (never the provider's fetch-on-miss). Same
        # normalization the build's _ohlcv applies: tz-naive, ascending, date-indexed.
        p = os.path.join(ohlcv_dir, f"{sym}_1d.parquet")
        if not os.path.isfile(p):
            return None
        df = pd.read_parquet(p)
        if df is None or len(df) == 0 or "Date" not in df.columns:
            return None
        idx = pd.to_datetime(df["Date"])
        if idx.dt.tz is not None:
            idx = idx.dt.tz_localize(None)
        return df.set_index(idx).sort_index()

    print(f"recompute-atr: store={store} periods={ms.ATR_PERIODS}", flush=True)
    summary = ms.recompute_atr_columns(store, _ohlcv_cached)
    print(f"recompute-atr: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
