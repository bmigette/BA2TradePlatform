#!/usr/bin/env python
"""Chain-depth pre-flight: does the options parquet tree carry an expiry with bars at
DTE >= --min-dte for each symbol of an input universe?

WHY THIS EXISTS
----------------
The 2026-08-31 leaps-grid design (docs/superpowers/specs/2026-08-31-leaps-grid-design.md
Section 5, "Universe") measured that the options parquet cache is a SPLIT universe: liquid
names carry LEAPS-range bars (745-858 days pre-expiry on the sampled names), but smaller
names never list a January cycle far enough out, so their partitions begin only 52-246 days
pre-expiry. Submitting a grid job over the whole stage-1 universe with no filter means a
silent no-contract trial for every name that cannot reach the strategy's DTE depth -- the
same "nothing failed, the universe was quietly wrong" shape that tools/check_window_coverage.py
exists to catch on the OHLCV side (see that file's docstring). This is that check for the
options tree, parameterised on DTE depth instead of calendar coverage, because the per-strategy
requirement is a DEPTH one: LEAPS/PMCC keys need bars at DTE >= 365, O_CBS/O_PBS/O_CAL need
DTE >= 180, O_ERN only needs DTE >= 7 (design Section 5 / Section 2 gene tables).

WHAT IT CHECKS
--------------
For each symbol: walk ``<root>/<SYMBOL>/exp=<YYYY-MM-DD>/*.parquet`` (the layout written by
``ba2_providers.options.parquet_store.OptionHistoryParquetStore`` -- this tool reuses that
module's ``PROVIDER_DIR`` constant for the default root instead of hardcoding the path, and
matches its partition naming rather than reinventing a scheme). A symbol is KEPT when at
least one partition (expiry) carries a bar whose ``bar_date`` falls inside [--start, --end]
AND whose distance to that partition's expiry, in calendar days, is >= --min-dte. The DTE
convention is **inclusive**: a bar sitting at EXACTLY --min-dte days before expiry counts
(``>=``, never ``>``) -- pinned by a boundary test, because a strategy admitting entries "at
DTE >= X" must be able to see the DTE == X bar or it silently narrows its own admission band.

A symbol is DROPPED for one of two DISTINCT reasons, printed and used downstream to tell
"skip this symbol" apart from "skip this method" (design Section 5's own phrasing):

  * "no partitions at all"   -- the symbol has no ``exp=*/*.parquet`` file anywhere in the
                                 tree (never fetched, or fetched and genuinely empty --
                                 either way there is no bar data to trade this name at all).
  * "no expiries at depth"   -- partitions exist and were read, but none carried a
                                 qualifying bar inside the window at the requested depth
                                 (the split-universe case: this name lists options, just not
                                 far enough out).

Only ``bar_date`` gates the window, never ``expiry``: a LEAPS expiry can legitimately fall
AFTER --end (a Jan-2026 expiry, window ending 2025-12) while still carrying bars deep inside
the window -- that is exactly the shape the design measured. Gating on expiry too would drop
every real LEAPS partition the probe exists to find.

READ-ONLY, stdlib + pyarrow only (no pandas): this is an offline preflight over a tree that
can carry thousands of partitions, so it reads just the one ``bar_date`` column per file
rather than paying for a full-row pandas parquet load.

Usage (test venv):
    ba2-venvs/test/Scripts/python.exe tools/probe_option_chain_depth.py \\
        --symbols @tools/options_universe_top100.txt --min-dte 365 \\
        --start 2023-01-01 --end 2025-12-31 --out /tmp/leaps_universe.txt

    # quick sanity pass over a random subset instead of the full list (printed, reproducible):
    ... --sample 40 --seed 7
"""
from __future__ import annotations

import argparse
import glob
import os
import random
import sys
from dataclasses import dataclass
from datetime import date
from typing import List, Optional, Sequence

REASON_NO_PARTITIONS = "no partitions at all"
REASON_NO_DEPTH = "no expiries at depth"


def _default_root() -> str:
    # Imported at CALL time, matching parquet_store._default_root's own rationale: a
    # temp-dir CACHE_FOLDER rebind (tests, an alternate BA2_HOME) must win over anything
    # bound at import time.
    import ba2_common.config as _cfg
    from ba2_providers.options.parquet_store import PROVIDER_DIR
    return os.path.join(_cfg.CACHE_FOLDER, PROVIDER_DIR)


def _parse_symbols(raw: str) -> List[str]:
    """Comma-separated list, or ``@path`` for a file. The file may itself be one symbol per
    line OR a single comma-separated line (the same dual-format acceptance
    check_window_coverage.py uses for the Senate universe CSV) -- silently reading a
    comma-joined file as one giant symbol would probe a nonexistent ticker and report an
    empty universe as healthy.
    """
    if raw.startswith("@"):
        with open(raw[1:], encoding="utf-8") as f:
            blob = f.read()
        syms = [t.strip().strip('"').strip("'") for t in blob.replace(",", "\n").splitlines()]
    else:
        syms = [s.strip() for s in raw.split(",")]
    seen = set()
    out = []
    for s in syms:
        s = s.upper()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


@dataclass(frozen=True)
class ProbeResult:
    symbol: str
    kept: bool
    reason: Optional[str]      # None when kept
    best_dte: Optional[int]    # deepest in-window DTE found, even when it fell short


def _partition_files(root: str, symbol: str) -> List[str]:
    return sorted(glob.glob(os.path.join(root, symbol, "exp=*", "*.parquet")))


def _expiry_from_partition_path(path: str) -> Optional[date]:
    exp_dir = os.path.basename(os.path.dirname(path))
    if not exp_dir.startswith("exp="):
        return None
    try:
        return date.fromisoformat(exp_dir[len("exp="):])
    except ValueError:
        return None


def _bar_dates(path: str) -> List[date]:
    import pyarrow.parquet as pq

    table = pq.read_table(path, columns=["bar_date"])
    out: List[date] = []
    for v in table.column("bar_date").to_pylist():
        if v is None:
            continue
        try:
            out.append(date.fromisoformat(v))
        except ValueError:
            continue
    return out


def probe_symbol(root: str, symbol: str, min_dte: int, start: date, end: date) -> ProbeResult:
    files = _partition_files(root, symbol)
    if not files:
        return ProbeResult(symbol, kept=False, reason=REASON_NO_PARTITIONS, best_dte=None)

    best_dte: Optional[int] = None
    for path in files:
        expiry = _expiry_from_partition_path(path)
        if expiry is None:
            continue
        try:
            bar_dates = _bar_dates(path)
        except Exception:  # noqa: BLE001 - a corrupt/unreadable partition is a skip, not a crash
            continue
        for bar_date in bar_dates:
            if bar_date < start or bar_date > end:
                continue
            dte = (expiry - bar_date).days
            if best_dte is None or dte > best_dte:
                best_dte = dte
            if dte >= min_dte:
                return ProbeResult(symbol, kept=True, reason=None, best_dte=dte)

    return ProbeResult(symbol, kept=False, reason=REASON_NO_DEPTH, best_dte=best_dte)


def probe_symbols(root: str, symbols: Sequence[str], min_dte: int, start: date,
                  end: date) -> List[ProbeResult]:
    return [probe_symbol(root, s, min_dte, start, end) for s in symbols]


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", required=True,
                    help="Comma-separated list or @file (one per line, or one comma-joined "
                         "line -- both accepted).")
    ap.add_argument("--min-dte", type=int, required=True,
                    help="A symbol is kept when it has a bar at this many days or MORE "
                         "before some expiry (>=, inclusive) inside --start/--end.")
    ap.add_argument("--start", required=True, help="Window start, ISO date. Gates bar_date.")
    ap.add_argument("--end", required=True, help="Window end, ISO date. Gates bar_date.")
    ap.add_argument("--out", required=True, help="Path to write the KEPT symbol list to, "
                                                  "one per line, sorted.")
    ap.add_argument("--root", default=None,
                    help="Options parquet tree root. Default: "
                         "CACHE_FOLDER/TastyTradeOptionsProvider (the real store).")
    ap.add_argument("--sample", type=int, default=None,
                    help="Probe only a random SAMPLE of the input symbol list (default: "
                         "full scan of every symbol given). Printed when used.")
    ap.add_argument("--seed", type=int, default=1337,
                    help="Sampling seed, so a --sample run is reproducible (default 1337).")
    args = ap.parse_args(argv)

    root = args.root if args.root is not None else _default_root()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    if end < start:
        print(f"probe: FATAL --end {end} is before --start {start}")
        return 2

    symbols = _parse_symbols(args.symbols)
    if not symbols:
        print("probe: FATAL no symbols in --symbols")
        return 2

    if args.sample is not None:
        rng = random.Random(args.seed)
        population = symbols
        symbols = sorted(rng.sample(population, args.sample)) if args.sample < len(population) \
            else sorted(population)
        print(f"probe: sampling {len(symbols)} of {len(population)} symbols (seed={args.seed})")

    print(f"probe: root={root}")
    print(f"probe: min_dte={args.min_dte} window={start}..{end} symbols={len(symbols)}")

    results = probe_symbols(root, symbols, args.min_dte, start, end)
    kept = [r for r in results if r.kept]
    dropped = [r for r in results if not r.kept]

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        for r in sorted(kept, key=lambda r: r.symbol):
            f.write(r.symbol + "\n")

    print(f"probe: kept {len(kept)}/{len(results)}, dropped {len(dropped)} -> wrote {args.out}")
    for r in dropped:
        detail = f" (best {r.best_dte}d in window)" if r.best_dte is not None else ""
        print(f"          DROP {r.symbol:<8} {r.reason}{detail}")

    if not kept:
        print("probe: FAIL -- zero symbols kept. Nothing in this universe can trade this "
              "strategy's DTE depth over this window.")
        return 1
    print("probe: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
