#!/usr/bin/env python
"""Null out vendor IV SENTINELS in an already-written option parquet store.

WHY. ThetaData signals "could not invert this" with an INT32_MAX sentinel rather than a null,
at more than one scale factor -- measured on the live backfill 2026-09-04:

    2147483646 / 10000  = 214748.3646
    2147483646 / 100000 =  21474.8365
    plus a tail of 10^2-10^3 values

IV is stored as a DECIMAL (0.2841 == 28.41%), so those are 2-21 MILLION percent. They were
0.4% of rows. ``ba2_providers.options.thetadata._clean_iv`` now drops them at ingest; this
repairs partitions written before that fix, which is far cheaper than re-fetching them (5.7 h
of API time for the 23 symbols affected, versus minutes here).

ONLY the ``iv`` column is touched, and only where the value is implausible. Prices, quotes,
volume and open interest are never modified, and no manifest is rewritten -- the schema is
unchanged, so a repaired partition stays COMPLETE and is not re-fetched.

Writes are atomic (temp + os.replace), matching OptionHistoryParquetStore's own discipline, so
an interrupted repair leaves either the old file or the new one, never a truncated parquet.

    # see what would change, touch nothing
    python tools/repair_option_iv_sentinels.py --root <store> --dry-run

    # repair
    python tools/repair_option_iv_sentinels.py --root <store>
"""
from __future__ import annotations

import argparse
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: Must match ba2_providers.options.thetadata._MAX_PLAUSIBLE_IV -- imported rather than
#: re-declared so the repair and the ingest can never disagree about what "implausible" means.
try:
    from ba2_providers.options.thetadata import _MAX_PLAUSIBLE_IV
except Exception:  # pragma: no cover - the tool must still run against a bare checkout
    _MAX_PLAUSIBLE_IV = 100.0


def _default_root() -> str:
    import ba2_common.config as cfg
    return os.path.join(cfg.CACHE_FOLDER, "ThetaDataOptionsProvider")


def repair_file(path: str, dry_run: bool) -> tuple[int, int]:
    """Returns (rows_scanned, rows_repaired) for one parquet file."""
    import pandas as pd

    df = pd.read_parquet(path)
    if "iv" not in df.columns or df.empty:
        return len(df), 0
    bad = df["iv"].notna() & (df["iv"] > _MAX_PLAUSIBLE_IV)
    n = int(bad.sum())
    if n and not dry_run:
        df.loc[bad, "iv"] = None
        tmp = path + ".repair.tmp"
        try:
            df.to_parquet(tmp, index=False)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    return len(df), n


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", help="Store root (default: the ThetaData tree).")
    p.add_argument("--dry-run", action="store_true", help="Report only; write nothing.")
    p.add_argument("--symbols", help="Comma-separated subset; default every symbol.")
    ns = p.parse_args(argv)

    root = ns.root or _default_root()
    syms = ([s.strip().upper() for s in ns.symbols.split(",")] if ns.symbols
            else sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))))

    print(f"root      : {root}")
    print(f"threshold : iv > {_MAX_PLAUSIBLE_IV}")
    print(f"mode      : {'DRY RUN (nothing written)' if ns.dry_run else 'REPAIR'}")
    print()

    tot_rows = tot_bad = tot_files = touched = 0
    for sym in syms:
        files = glob.glob(os.path.join(root, sym, "*", "*.parquet"))
        s_rows = s_bad = s_touched = 0
        for f in files:
            rows, bad = repair_file(f, ns.dry_run)
            s_rows += rows
            s_bad += bad
            if bad:
                s_touched += 1
        tot_rows += s_rows
        tot_bad += s_bad
        tot_files += len(files)
        touched += s_touched
        if s_bad:
            print(f"  {sym:8s} {s_bad:6d} sentinel iv in {s_touched}/{len(files)} partitions "
                  f"({100.0 * s_bad / s_rows:.3f}% of {s_rows:,} rows)")

    print()
    print(f"scanned {tot_files} partitions, {tot_rows:,} rows across {len(syms)} symbol(s)")
    print(f"{'would null' if ns.dry_run else 'nulled'} {tot_bad:,} iv value(s) "
          f"({100.0 * tot_bad / tot_rows if tot_rows else 0:.3f}%) in {touched} partition(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
