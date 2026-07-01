"""Pre-fetch 2026 H1 (Jan-Jun) data so a future walk-forward validation has an out-of-sample
window the -aggr grid's GA never touched (grid trains/ranks on 2023-01-01..2026-01-01 only).

Two steps:
  1) Extend the screener METRIC store (daily OHLCV + market-cap + float, one row/symbol/scan-day)
     from 2026-01-01 to 2026-07-01 via `ba2-test build-screener-metrics` — same mechanism that
     built the existing 2022-06..2025-12 partitions, so the universe/schema stay consistent.
  2) Fetch 5MIN OHLCV (the actual per-trade backtest resolution) for the store's full symbol
     universe over the same window, via `ba2-test fetch-cache`, chunked to stay under the
     Windows ~32K command-line length limit (mirrors precache_screener_universe.py's chunking).

Runs LOW-PRIORITY/CONSERVATIVE workers throughout: this shares the FMP 750/min budget and local
CPU with any concurrently-running optimize grid (e.g. the -aggr matrix), so it must not starve it.

Usage (test venv; FMP_API_KEY in env):
    ba2-venvs/test/Scripts/python.exe test_files/fetch_2026h1_walkforward_data.py \
        [--start 2026-01-01] [--end 2026-07-01] [--market-cap-min 50000000] \
        [--build-workers 4] [--fetch-workers 4] [--batch-size 700] [--skip-build] [--skip-5min]
"""
import argparse
import os
import subprocess
import sys


def _exe(name: str) -> str:
    e = os.path.join(os.path.dirname(sys.executable), f"{name}.exe")
    return e if os.path.exists(e) else os.path.join(os.path.dirname(sys.executable), name)


def _chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default="2026-07-01")
    ap.add_argument("--store", default=None, help="Metric-store dir (default = SCREENER_STORE_DIR).")
    ap.add_argument("--market-cap-min", type=float, default=50_000_000.0,
                    help="LOOSEST cap bound for the metric-store extend (default $50M — matches this "
                         "codebase's documented small-cap-band floor; the existing store also admits "
                         "some sub-$50M noise from whatever floor originally built it, which this "
                         "doesn't try to reverse-engineer).")
    ap.add_argument("--build-workers", type=int, default=4)
    ap.add_argument("--fetch-workers", type=int, default=4)
    ap.add_argument("--batch-size", type=int, default=700,
                    help="Symbols per fetch-cache subprocess (Windows cmdline length limit).")
    ap.add_argument("--skip-build", action="store_true", help="Skip the metric-store extend step.")
    ap.add_argument("--skip-5min", action="store_true", help="Skip the 5min OHLCV fetch step.")
    args = ap.parse_args()

    # No key check here: both subprocesses (build-screener-metrics / fetch-cache) resolve
    # FMP_API_KEY themselves via `os.getenv(...) or get_app_setting(...)` (DB app-settings
    # fallback), so this wrapper doesn't need the key directly — ms.load_store below is a
    # pure disk read.
    from ba2_common.config import SCREENER_STORE_DIR
    store = args.store or SCREENER_STORE_DIR
    exe = _exe("ba2-test")

    if not args.skip_build:
        print(f">> [1/2] extending metric store {store} -> {args.start}..{args.end} "
              f"(market_cap_min={args.market_cap_min:.0e}, workers={args.build_workers})", flush=True)
        cmd = [exe, "build-screener-metrics", "--store", store,
               "--start", args.start, "--end", args.end,
               "--market-cap-min", str(args.market_cap_min),
               "--workers", str(args.build_workers)]
        rc = subprocess.run(cmd, env=os.environ.copy()).returncode
        print(f">> [1/2] build-screener-metrics exit={rc}", flush=True)
        if rc != 0:
            print(">> [1/2] non-zero exit — continuing to step 2 anyway (store extend is resumable).")
    else:
        print(">> [1/2] SKIPPED (--skip-build)", flush=True)

    if args.skip_5min:
        print(">> [2/2] SKIPPED (--skip-5min)", flush=True)
        return 0

    from ba2_providers.screener import metric_store as ms
    df = ms.load_store(store)
    if df is None or getattr(df, "empty", True):
        sys.exit(f"metric store empty at {store} — cannot resolve universe for the 5min fetch.")
    syms = sorted(df["symbol"].dropna().astype(str).unique().tolist())
    print(f">> [2/2] fetching 5min OHLCV for {len(syms)} symbols, {args.start}..{args.end} "
          f"(workers={args.fetch_workers}, batch_size={args.batch_size})", flush=True)

    batches = list(_chunks(syms, args.batch_size))
    rc_all = 0
    for i, batch in enumerate(batches, 1):
        cmd = [exe, "fetch-cache", "--symbols", ",".join(batch),
               "--timeframes", "5min", "--start", args.start, "--end", args.end,
               "--workers", str(args.fetch_workers)]
        print(f">> [2/2] batch {i}/{len(batches)} ({len(batch)} symbols) ...", flush=True)
        rc = subprocess.run(cmd, env=os.environ.copy()).returncode
        if rc != 0:
            print(f">> [2/2] batch {i} returned {rc} (continuing — disk cache is incremental/resumable).")
            rc_all = rc
    print(f">> done. final rc={rc_all}", flush=True)
    return rc_all


if __name__ == "__main__":
    raise SystemExit(main())
