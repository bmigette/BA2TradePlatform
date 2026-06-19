#!/usr/bin/env python
"""Migrate the legacy fetch-cache OHLCV layout into the canonical native cache.

BACKGROUND
----------
OHLCV used to be cached in TWO places for the same provider:

  (A) native    CACHE_FOLDER/<ProviderClassName>/<SYM>_<interval>.parquet   (schema +effective_date)
  (B) fetch-cache CACHE_FOLDER/ohlcv/<provider_short>/<SYM>_<interval>.parquet (Date+OHLCV, no eff_date)

Everything reads (A); ``ba2-test fetch-cache`` historically wrote (B). They never met, so a bulk
download in (B) was invisible to the backtest. The writer is now repointed to (A); this one-shot
script relocates EXISTING (B) data into (A) so nothing has to be re-downloaded.

For each ``CACHE_FOLDER/ohlcv/<short>/<SYM>_<interval>.parquet`` (and legacy ``.csv``):
  1. resolve ``<short>`` -> ProviderClassName via the ba2_providers registry CLASS object
     (no instantiation -> no API key needed);
  2. read it (Date,OHLCV), stamp ``effective_date = Date``;
  3. if a native file already exists, MERGE + dedupe on Date (prefer the native row on a tie);
  4. write via ``native_cache.write_timeseries`` and VERIFY (effective_date present, row count
     conserved).

Idempotent: re-running is a no-op once data is merged. Default is a DRY RUN — pass ``--apply`` to
write. ``--delete-source`` removes the legacy ``ohlcv/<short>/`` dirs only after every file verified.

Usage
-----
    ./venv/bin/python scripts/migrate_ohlcv_fetch_cache.py            # dry run, default cache
    ./venv/bin/python scripts/migrate_ohlcv_fetch_cache.py --apply
    ./venv/bin/python scripts/migrate_ohlcv_fetch_cache.py --apply --delete-source
    ./venv/bin/python scripts/migrate_ohlcv_fetch_cache.py --cache-folder /path/to/cache --apply
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd


# Fallback short->ClassName map if the ba2_providers registry can't be imported (e.g. a partial
# env). The registry is preferred; this just keeps the script runnable in isolation.
_FALLBACK_SHORT_TO_CLASS = {
    "fmp": "FMPOHLCVProvider",
    "yfinance": "YFinanceDataProvider",
    "alpaca": "AlpacaOHLCVProvider",
    "polygon": "PolygonOHLCVProvider",
    "eodhd": "EODHDOHLCVProvider",
    "alphavantage": "AlphaVantageOHLCVProvider",
}


def _short_to_class_map() -> dict:
    """short provider name -> provider CLASS name, from the registry (no instantiation)."""
    try:
        from ba2_providers import OHLCV_PROVIDERS
        return {short: cls.__name__ for short, cls in OHLCV_PROVIDERS.items()}
    except Exception:
        return dict(_FALLBACK_SHORT_TO_CLASS)


def _naive_dates(s: pd.Series) -> pd.Series:
    """Normalize a Date series to tz-naive UTC. The native 5min cache stores tz-AWARE Dates while
    the legacy fetch-cache stored tz-NAIVE ones; mixing them breaks concat/dedupe (a tz-naive vs
    tz-aware comparison error). The backtest reads Dates as tz-naive UTC anyway."""
    s = pd.to_datetime(s)
    if getattr(s.dt, "tz", None) is not None:
        s = s.dt.tz_convert("UTC").dt.tz_localize(None)
    return s


def _read_source(fp: Path) -> pd.DataFrame:
    df = pd.read_csv(fp) if fp.suffix == ".csv" else pd.read_parquet(fp)
    df = df.copy()
    df["Date"] = _naive_dates(df["Date"])
    return df


def _merge_prefer_native(native: pd.DataFrame, src: pd.DataFrame) -> pd.DataFrame:
    """Union on Date; on a duplicate Date keep the NATIVE row (it is the canonical, as_of-stamped
    copy). Returns a Date-sorted frame (tz-naive Date). effective_date is (re)stamped by the caller."""
    native = native.copy()
    native["Date"] = _naive_dates(native["Date"])
    if "effective_date" in native.columns:
        native = native.drop(columns=["effective_date"])  # re-stamped on write; avoid tz mismatch
    # native first so drop_duplicates(keep='first') prefers it on a tie
    merged = pd.concat([native, src], ignore_index=True)
    merged = (
        merged.drop_duplicates(subset=["Date"], keep="first")
              .sort_values("Date")
              .reset_index(drop=True)
    )
    return merged


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Migrate legacy fetch-cache OHLCV into the native cache.")
    ap.add_argument("--cache-folder", default=None,
                    help="Cache root (default: ba2_common.config.CACHE_FOLDER).")
    ap.add_argument("--apply", action="store_true",
                    help="Actually write (default is a dry run that only reports).")
    ap.add_argument("--delete-source", action="store_true",
                    help="After all files verify, remove the legacy ohlcv/<short>/ dirs.")
    args = ap.parse_args(argv)

    from ba2_common.core import native_cache
    if args.cache_folder:
        cache_folder = Path(args.cache_folder)
        # Repoint native_cache so timeseries_path/write target the requested root.
        native_cache.CACHE_FOLDER = str(cache_folder)
    else:
        from ba2_common.config import CACHE_FOLDER as _CF
        cache_folder = Path(_CF)

    legacy_root = cache_folder / "ohlcv"
    short_to_class = _short_to_class_map()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] cache root: {cache_folder}")
    print(f"[{mode}] legacy fetch-cache root: {legacy_root}")
    if not legacy_root.exists():
        print("Nothing to migrate: legacy ohlcv/ root does not exist.")
        return 0

    n_migrated = n_merged = n_skipped = n_failed = 0
    verified_dirs = []  # short dirs whose files all verified (candidates for --delete-source)

    for short_dir in sorted(p for p in legacy_root.iterdir() if p.is_dir()):
        short = short_dir.name
        class_name = short_to_class.get(short)
        if not class_name:
            print(f"  ! {short}/: unknown provider short name (no class mapping) — SKIPPING dir")
            continue
        files = sorted(list(short_dir.glob("*.parquet")) + list(short_dir.glob("*.csv")))
        print(f"  {short}/ -> {class_name}/  ({len(files)} file(s))")
        dir_ok = True

        for fp in files:
            # filename: <SYM>_<interval>
            stem = fp.stem
            if "_" not in stem:
                print(f"    ! {fp.name}: cannot parse <SYM>_<interval> — SKIPPING")
                n_skipped += 1
                dir_ok = False
                continue
            symbol, interval = stem.rsplit("_", 1)
            symbol = symbol.upper()  # native_cache.timeseries_path uppercases the symbol

            try:
                src = _read_source(fp)
                src_rows = len(src)
                src["effective_date"] = src["Date"]

                native_path = Path(native_cache.timeseries_path(class_name, symbol, interval))
                native_rows = 0
                if native_path.exists():
                    native = pd.read_parquet(native_path)
                    native_rows = len(native)
                    out = _merge_prefer_native(native, src)
                    out["effective_date"] = out["Date"]
                    action = "merge"
                else:
                    out = src
                    action = "new"

                out_rows = len(out)
                expected_min = max(src_rows, native_rows)
                if out_rows < expected_min:
                    print(f"    ! {fp.name}: row regression (out={out_rows} < max(src={src_rows},"
                          f" native={native_rows})) — SKIPPING")
                    n_failed += 1
                    dir_ok = False
                    continue

                if args.apply:
                    native_cache.write_timeseries(class_name, symbol, interval, out)
                    # verify
                    chk = pd.read_parquet(native_path)
                    if "effective_date" not in chk.columns or len(chk) != out_rows:
                        print(f"    ! {fp.name}: VERIFY FAILED (rows={len(chk)} eff_date="
                              f"{'effective_date' in chk.columns}) — leaving source in place")
                        n_failed += 1
                        dir_ok = False
                        continue

                tag = "MERGED" if action == "merge" else "migrated"
                print(f"    {symbol}_{interval}: {tag} src={src_rows} native={native_rows} -> {out_rows}")
                if action == "merge":
                    n_merged += 1
                else:
                    n_migrated += 1
            except Exception as e:  # noqa: BLE001
                print(f"    ! {fp.name}: ERROR {e!r} — SKIPPING")
                n_failed += 1
                dir_ok = False

        if dir_ok:
            verified_dirs.append(short_dir)

    print(f"\n[{mode}] summary: migrated={n_migrated} merged={n_merged} "
          f"skipped={n_skipped} failed={n_failed}")

    if args.delete_source:
        if not args.apply:
            print("--delete-source ignored in dry run (use --apply).")
        elif n_failed:
            print("--delete-source SKIPPED: some files failed to verify; not deleting any source.")
        else:
            import shutil
            for d in verified_dirs:
                shutil.rmtree(d)
                print(f"  deleted source dir {d}")
            # Remove the now-empty ohlcv/ root if nothing is left.
            try:
                if legacy_root.exists() and not any(legacy_root.iterdir()):
                    legacy_root.rmdir()
                    print(f"  removed empty {legacy_root}")
            except OSError:
                pass
    elif args.apply and not n_failed and (n_migrated or n_merged):
        print("Source left in place. Re-run with --delete-source once you've reconciled a backtest.")

    return 1 if n_failed else 0


if __name__ == "__main__":
    sys.exit(main())
