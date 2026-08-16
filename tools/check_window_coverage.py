#!/usr/bin/env python
"""Fast pre-flight: does the OHLCV cache actually cover the START of a grid's window?

WHY THIS EXISTS, AND WHY IT IS NOT cache_health_check.py
-------------------------------------------------------
2026-08-16: a grid ran for days claiming a 2020-2025 window while ~75% of its universe had no 5min
bars before 2022. Those symbols could not be PRICED, therefore not traded, so the first two years
silently ran on a ~25% universe. Nothing failed -- `AsOfPriceSource.preload` deliberately treats
"cache exists but has no rows in this sub-range" as a legitimate gap (recent IPO, holiday, halt)
rather than an error, which is correct behaviour for one symbol and catastrophic for 75% of them.

`cache_health_check.py` HAS a per-month coverage check and it does find this -- when pointed at the
right interval and window. Two things stopped it being a usable gate:
  * its defaults are `--ohlcv-interval 1d --start 2022-01-01`; daily has deep history, so a default
    run reports everything healthy, and nobody thinks to override the exact assumption under test;
  * at 5min it is far too slow -- measured 321s for EIGHT symbols over a 6-year month-by-month
    walk, i.e. roughly an hour at a useful sample size. A gate that costs an hour does not get run.
  * it also exits 0 regardless of what it finds, so `if ! tool; then abort` never fires.

This script does the ONE question a grid must answer before it starts, in seconds:
"at this interval, do the symbols I am about to trade have bars at the BEGINNING of my window?"

It checks the FIRST `--probe-days` of the window only. That is where a truncated fetch shows up
(the 2026-08-16 cache had a hard boundary at 2022-01-03 for most symbols); checking every month
buys little and costs everything. Reads only the Date column.
"""
from __future__ import annotations

import argparse
import glob
import os
import random
import sys
from datetime import datetime, timedelta


def _first_bar(path: str):
    import pandas as pd
    try:
        d = pd.read_parquet(path, columns=["Date"])["Date"]
        return pd.to_datetime(d, utc=True).min().tz_localize(None).to_pydatetime()
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--interval", default="5min", help="Bar interval the GRID will run (not 1d unless it is).")
    ap.add_argument("--start", required=True, help="Grid window start, ISO.")
    ap.add_argument("--cache-dir", default=None,
                    help="Provider cache dir (default CACHE_FOLDER/FMPOHLCVProvider).")
    ap.add_argument("--symbols", default=None,
                    help="Comma-separated list or @file. Default: sample the whole cache.")
    ap.add_argument("--sample", type=int, default=40, help="Symbols to sample (default 40).")
    ap.add_argument("--probe-days", type=int, default=90,
                    help="A symbol passes if its first bar is within this many days of --start.")
    ap.add_argument("--max-missing-pct", type=float, default=20.0,
                    help="FAIL when more than this %% of sampled symbols lack coverage (default 20).")
    ap.add_argument("--seed", type=int, default=1337, help="Sampling seed, so a run is reproducible.")
    args = ap.parse_args()

    cache_dir = args.cache_dir or os.path.join(
        os.path.expanduser("~"), "Documents", "ba2", "common", "cache", "FMPOHLCVProvider")
    if not os.path.isdir(cache_dir):
        print(f"coverage: FATAL cache dir not found: {cache_dir}")
        return 2

    start = datetime.fromisoformat(args.start)
    cutoff = start + timedelta(days=args.probe_days)

    if args.symbols:
        raw = args.symbols
        if raw.startswith("@"):
            with open(raw[1:], encoding="utf-8") as f:
                syms = [s.strip() for s in f if s.strip()]
        else:
            syms = [s.strip() for s in raw.split(",") if s.strip()]
        files = [os.path.join(cache_dir, f"{s}_{args.interval}.parquet") for s in syms]
        files = [p for p in files if os.path.isfile(p)]
    else:
        files = glob.glob(os.path.join(cache_dir, f"*_{args.interval}.parquet"))

    if not files:
        print(f"coverage: FATAL no {args.interval} parquet files found in {cache_dir}")
        return 2

    rng = random.Random(args.seed)
    sample = files if len(files) <= args.sample else rng.sample(files, args.sample)

    missing, ok, unreadable = [], 0, 0
    for p in sample:
        sym = os.path.basename(p)[: -len(f"_{args.interval}.parquet")]
        first = _first_bar(p)
        if first is None:
            unreadable += 1
        elif first > cutoff:
            missing.append((sym, first.date()))
        else:
            ok += 1

    checked = len(sample)
    pct = 100.0 * len(missing) / max(1, checked)
    print(f"coverage: interval={args.interval} start={start.date()} "
          f"(a symbol passes if its first bar is on/before {cutoff.date()})")
    print(f"coverage: sampled {checked} of {len(files)} cached symbols -- "
          f"{ok} covered, {len(missing)} missing ({pct:.0f}%), {unreadable} unreadable")
    for sym, d in missing[:12]:
        print(f"          {sym:<8} first bar {d}")
    if len(missing) > 12:
        print(f"          ... and {len(missing) - 12} more")

    if pct > args.max_missing_pct:
        print(f"coverage: FAIL -- {pct:.0f}% exceeds the {args.max_missing_pct:.0f}% threshold.")
        print(f"coverage:   These symbols cannot be PRICED before their first bar, so they cannot")
        print(f"coverage:   trade. The run would silently use a REDUCED universe for the early part")
        print(f"coverage:   of the window and its results would not mean what the window says.")
        print(f"coverage:   Backfill: ba2-test fetch-cache --provider fmp "
              f"--timeframes {args.interval} --start {start.date()} --end <end> --symbols @<file>")
        return 1
    print("coverage: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
