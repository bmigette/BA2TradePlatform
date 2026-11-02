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

from ba2_common.config import CACHE_FOLDER
from ba2_common.core.ohlcv_history import DEFAULT_BT_HISTORY_SOURCE, HISTORY_SOURCES, history_cache_dirname


def _first_bar(path: str):
    span = _bar_span(path)
    return span[0] if span else None


def _bar_span(path: str):
    """(first bar, last bar) as naive datetimes, or None when unreadable. Reads only the Date column."""
    import pandas as pd
    try:
        d = pd.to_datetime(pd.read_parquet(path, columns=["Date"])["Date"], utc=True)
        return (d.min().tz_localize(None).to_pydatetime(), d.max().tz_localize(None).to_pydatetime())
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--interval", default="5min", help="Bar interval the GRID will run (not 1d unless it is).")
    ap.add_argument("--start", required=True, help="Grid window start, ISO.")
    ap.add_argument("--end", default=None,
                    help="Grid window end, ISO (optional). Also checks the LAST --probe-days: a symbol passes "
                         "if its last bar is on/after end - probe-days; one whose existence-interval file also "
                         "stops before that is delisted and excluded (goal2027: data must reach the end too)."),
    ap.add_argument("--source", choices=sorted(HISTORY_SOURCES), default=DEFAULT_BT_HISTORY_SOURCE,
                    help="OHLCV history source whose cache folder is checked (default "
                         f"{DEFAULT_BT_HISTORY_SOURCE}): fmp -> CACHE_FOLDER/FMPOHLCVProvider, "
                         "alpaca -> CACHE_FOLDER/AlpacaHistoryOHLCVProvider.")
    ap.add_argument("--cache-dir", default=None,
                    help="Provider cache dir; overrides --source (default CACHE_FOLDER/<source folder>).")
    ap.add_argument("--symbols", default=None,
                    help="Comma-separated list or @file. Default: sample the whole cache.")
    ap.add_argument("--sample", type=int, default=40, help="Symbols to sample (default 40).")
    ap.add_argument("--probe-days", type=int, default=90,
                    help="A symbol passes if its first bar is within this many days of --start.")
    ap.add_argument("--min-covered-pct", type=float, default=75.0,
                    help="PASS when at least this %% of ELIGIBLE sampled symbols have coverage "
                         "(default 75). Eligible = the symbol actually traded at the window start, "
                         "established from its daily file; a security that had not listed yet is "
                         "not a cache defect and is excluded, not counted against you.")
    ap.add_argument("--existence-interval", default="1d",
                    help="Interval whose history establishes WHEN a symbol started trading "
                         "(default 1d -- daily history reaches much further back than intraday).")
    ap.add_argument("--seed", type=int, default=1337, help="Sampling seed, so a run is reproducible.")
    args = ap.parse_args()

    cache_dir = args.cache_dir or os.path.join(CACHE_FOLDER, history_cache_dirname(args.source))
    if not os.path.isdir(cache_dir):
        print(f"coverage: FATAL cache dir not found: {cache_dir}")
        return 2

    start = datetime.fromisoformat(args.start)
    cutoff = start + timedelta(days=args.probe_days)
    end = datetime.fromisoformat(args.end) if args.end else None
    if end is not None and end <= start:
        print(f"coverage: FATAL --end {end.date()} is not after --start {start.date()}")
        return 2
    end_cutoff = end - timedelta(days=args.probe_days) if end is not None else None

    if args.symbols:
        raw = args.symbols
        if raw.startswith("@"):
            # Accept BOTH one-per-line lists and comma-separated files: the Senate universe ships
            # as a single CSV line (senate_universe.csv), and silently reading that as ONE symbol
            # would make the gate check a nonexistent ticker and pass on an empty sample.
            with open(raw[1:], encoding="utf-8") as f:
                blob = f.read()
            syms = [t.strip().strip('"').strip("'")
                    for t in blob.replace(",", "\n").splitlines()]
            syms = [t for t in syms if t]
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

    missing, ok, unreadable, not_listed = [], 0, 0, 0
    end_missing, end_ok, delisted = [], 0, 0
    for p in sample:
        sym = os.path.basename(p)[: -len(f"_{args.interval}.parquet")]
        span = _bar_span(p)
        if span is None:
            unreadable += 1
            continue
        first, last = span
        if end_cutoff is not None:
            if last >= end_cutoff:
                end_ok += 1
            else:
                dpath = os.path.join(cache_dir, f"{sym}_{args.existence_interval}.parquet")
                dspan = _bar_span(dpath) if os.path.isfile(dpath) else None
                if args.interval != args.existence_interval and dspan is not None and dspan[1] < end_cutoff:
                    delisted += 1          # stopped trading: nothing to fetch, ever
                else:
                    end_missing.append((sym, last.date()))
        if first <= cutoff:
            ok += 1
            continue
        # Late first bar. Two very different causes -- separate them before calling it a defect.
        # A security that had not listed yet cannot have data at any interval, and counting those
        # made this gate report 40% "missing" on a legitimate cache (the sample was SPAC units,
        # preferreds and 2025-26 IPOs). The DAILY file is the listing calendar: it reaches much
        # further back than intraday, so if daily ALSO starts late the symbol simply did not exist.
        if args.interval != args.existence_interval:
            dpath = os.path.join(cache_dir, f"{sym}_{args.existence_interval}.parquet")
            dfirst = _first_bar(dpath) if os.path.isfile(dpath) else None
            if dfirst is not None and dfirst > cutoff:
                not_listed += 1
                continue
        missing.append((sym, first.date()))

    checked = len(sample)
    eligible = ok + len(missing)          # not_listed is excluded: nothing to fetch, ever
    covered_pct = 100.0 * ok / max(1, eligible)
    pct = 100.0 - covered_pct
    print(f"coverage: interval={args.interval} start={start.date()} "
          f"(a symbol passes if its first bar is on/before {cutoff.date()})")
    print(f"coverage: sampled {checked} of {len(files)} cached symbols -- "
          f"{ok} covered, {len(missing)} missing, {not_listed} not listed yet (excluded), "
          f"{unreadable} unreadable")
    print(f"coverage: {covered_pct:.0f}% of the {eligible} ELIGIBLE symbols are covered "
          f"(need >= {args.min_covered_pct:.0f}%)")
    for sym, d in missing[:12]:
        print(f"          {sym:<8} first bar {d}")
    if len(missing) > 12:
        print(f"          ... and {len(missing) - 12} more")

    end_fail = False
    if end_cutoff is not None:
        end_eligible = end_ok + len(end_missing)
        end_pct = 100.0 * end_ok / max(1, end_eligible)
        print(f"coverage: end={end.date()} (a symbol passes if its last bar is on/after {end_cutoff.date()}): "
              f"{end_ok} covered, {len(end_missing)} missing, {delisted} delisted before the end (excluded)")
        print(f"coverage: {end_pct:.0f}% of the {end_eligible} ELIGIBLE symbols reach the end "
              f"(need >= {args.min_covered_pct:.0f}%)")
        for sym, d in end_missing[:12]:
            print(f"          {sym:<8} last bar {d}")
        if len(end_missing) > 12:
            print(f"          ... and {len(end_missing) - 12} more")
        if end_pct < args.min_covered_pct:
            print(f"coverage: FAIL -- the cache stops before the window end for too many symbols; the end of "
                  f"the run would trade a reduced universe.")
            end_fail = True

    if covered_pct < args.min_covered_pct:
        print(f"coverage: FAIL -- {covered_pct:.0f}% covered is under the "
              f"{args.min_covered_pct:.0f}% bar.")
        print(f"coverage:   These symbols cannot be PRICED before their first bar, so they cannot")
        print(f"coverage:   trade. The run would silently use a REDUCED universe for the early part")
        print(f"coverage:   of the window and its results would not mean what the window says.")
        print(f"coverage:   Backfill: ba2-test fetch-cache --provider fmp "
              f"--timeframes {args.interval} --start {start.date()} --end <end> --symbols @<file>")
        return 1
    if end_fail:
        return 1
    print("coverage: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
