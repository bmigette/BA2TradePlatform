"""CACHE-ONLY in-place add of the ``momentum_12_1`` column to an existing screener metric store.

FactorRanker now reads 12-1 momentum point-in-time from the metric store (commit efc0da9) instead
of fetching ~400 days of daily OHLCV per symbol per rebalance — but only when the store carries a
``momentum_12_1`` column covering every universe symbol. A store built before that column existed
falls back to the (memory-heavy, OOM-prone) OHLCV path. This driver ADDS the column to such a store
WITHOUT a full rebuild and WITHOUT any network: it reads each store symbol's daily OHLCV straight
from the native parquet cache (CACHE_FOLDER/FMPOHLCVProvider/<SYM>_1d.parquet), computes the factor,
samples it as-of each existing scan date, and writes it back (every other column untouched).

Usage (test venv):
    ba2-venvs/test/Scripts/python.exe test_files/recompute_screener_momentum.py [--store <dir>]
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
        sys.exit(f"recompute-momentum: store not found: {store}")
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

    print(f"recompute-momentum: store={store}", flush=True)
    summary = ms.recompute_momentum_column(store, _ohlcv_cached)
    print(f"recompute-momentum: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
