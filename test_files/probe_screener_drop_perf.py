"""Perf guardrail for the multi-window price-drop metric store.

Confirms the per-window price_drop_pct_2..max_lookback columns don't materially regress:
  (1) compute_daily_metrics per-symbol wall-time (max_lookback=30 vs single-window baseline),
  (2) load_store + screened_symbol_union over a multi-window store vs a legacy (drops-stripped) one,
  (3) on-disk store size delta (informational).

Run:  ~/ba2-venvs/test/bin/python test_files/probe_screener_drop_perf.py
Ad-hoc probe (NOT collected by pytest). Prints timings + a PASS/FAIL verdict vs generous budgets.
"""
import os
import shutil
import tempfile
import time

import numpy as np
import pandas as pd

from ba2_providers.screener import metric_store as ms

N_SYMBOLS = 200
N_DAYS = 500
MAX_LOOKBACK = 30
COMPUTE_RATIO_BUDGET = 5.0     # multi-window compute may cost up to ~5x the single-window baseline
SCREEN_RATIO_BUDGET = 2.5      # load+screen over the wider store stays within ~2.5x the legacy store


def _ohlcv(seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2022-01-03", periods=N_DAYS, freq="B")
    steps = rng.normal(0.0, 1.0, N_DAYS).cumsum()
    close = pd.Series(100.0 + steps - steps.min(), index=idx) + 5.0
    vol = pd.Series(rng.integers(5e5, 5e6, N_DAYS).astype(float), index=idx)
    return pd.DataFrame({"Open": close, "High": close + 1, "Low": close - 1, "Close": close, "Volume": vol})


def _dir_size_mb(path: str) -> float:
    total = 0
    for root, _d, files in os.walk(path):
        for f in files:
            total += os.path.getsize(os.path.join(root, f))
    return total / 1e6


def main() -> int:
    frames = [_ohlcv(s) for s in range(N_SYMBOLS)]

    # (1) compute_daily_metrics: single-window baseline vs multi-window.
    t0 = time.perf_counter()
    for df in frames:
        ms.compute_daily_metrics(df, max_lookback=1)        # legacy column only (baseline)
    t_base = time.perf_counter() - t0

    t0 = time.perf_counter()
    multi = [ms.compute_daily_metrics(df, max_lookback=MAX_LOOKBACK) for df in frames]
    t_multi = time.perf_counter() - t0

    ratio = t_multi / t_base if t_base else float("inf")
    print(f"[compute] baseline(1w)={t_base*1e3:7.1f}ms  multi({MAX_LOOKBACK}w)={t_multi*1e3:7.1f}ms  "
          f"ratio={ratio:4.2f}x  per-symbol={t_multi/N_SYMBOLS*1e3:5.2f}ms  budget<={COMPUTE_RATIO_BUDGET}x")

    # Assemble a store frame (sample to a weekly grid like the real build).
    grid = pd.date_range("2022-01-03", periods=N_DAYS, freq="B")[::5]
    rows = []
    for sym, m in enumerate(multi):
        s = m.reindex(grid, method="ffill").dropna(subset=["close"]).reset_index()
        s = s.rename(columns={"index": "date"})
        s["date"] = s["date"].astype(str).str.slice(0, 10)
        s["symbol"] = f"SYM{sym:04d}"
        s["sector"] = "Tech"
        s["price"] = s["close"]
        rows.append(s)
    store_df = pd.concat(rows, ignore_index=True)
    drop_cols = [c for c in store_df.columns if c.startswith("price_drop_pct_")]
    print(f"[shape]   rows={len(store_df):,}  windowed_cols={len(drop_cols)}  total_cols={store_df.shape[1]}")

    tmp = tempfile.mkdtemp(prefix="screenperf_")
    try:
        multi_dir = os.path.join(tmp, "multi")
        legacy_dir = os.path.join(tmp, "legacy")
        ms.write_partitions(multi_dir, store_df)
        ms.write_partitions(legacy_dir, store_df.drop(columns=drop_cols))

        settings = {"market_cap_min": 0, "relative_volume_min": 1.0,
                    "price_drop_pct": 3.0, "price_drop_days": 10, "max_stocks": 50}
        start_day, end_day = "2022-01-03", "2023-12-31"

        def _time_load_screen(store_dir):
            ms.clear_store_memo()
            t = time.perf_counter()
            df = ms.load_store(store_dir)
            ms.screened_symbol_union(df, start_day, end_day, settings)
            return time.perf_counter() - t

        t_legacy = min(_time_load_screen(legacy_dir) for _ in range(3))
        t_multi_ls = min(_time_load_screen(multi_dir) for _ in range(3))
        sratio = t_multi_ls / t_legacy if t_legacy else float("inf")
        print(f"[screen]  legacy={t_legacy*1e3:7.1f}ms  multi={t_multi_ls*1e3:7.1f}ms  "
              f"ratio={sratio:4.2f}x  budget<={SCREEN_RATIO_BUDGET}x")
        print(f"[size]    legacy={_dir_size_mb(legacy_dir):.2f}MB  multi={_dir_size_mb(multi_dir):.2f}MB")

        ok = ratio <= COMPUTE_RATIO_BUDGET and sratio <= SCREEN_RATIO_BUDGET
        print(f"\n{'PASS' if ok else 'FAIL'}: compute {ratio:.2f}x (<= {COMPUTE_RATIO_BUDGET}), "
              f"load+screen {sratio:.2f}x (<= {SCREEN_RATIO_BUDGET})")
        return 0 if ok else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
