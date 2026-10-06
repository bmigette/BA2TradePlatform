"""Repair FMP daily OHLCV caches stuck on a PROVISIONAL bar (AMD/INTC/MU, 2026-10-05).

A one-tick snapshot (Open == High, Low == Close) cached near 09:31 New York makes the guarded
top-up refuse the symbol forever. This tool scans ``<cache>/FMPOHLCVProvider/*_1d.parquet`` for such
bars among the newest ``--lookback`` sessions and replaces them with the vendor's final bars, through
``ba2_trade_platform/modules/dataproviders/ohlcv_provisional.py`` (the same code the live top-up
runs). Nothing else in a file is touched; a file with no repairable bar is not rewritten.

DEFAULT IS A DRY RUN (it does ask the vendor, read-only, so the counts are what ``--apply`` does).

SAFETY: the live cache folder (``<BA2_HOME>/common/cache``, also the default when ``--cache-folder``
is not given) is refused unless ``--i-know-the-apps-are-stopped`` is passed. Stop 8080/8081/8082
first. The FMP key is read (read-only sqlite3) from ``--db-file``; it is never printed.

    python tools/repair_provisional_bars.py --db-file <app db> --cache-folder <folder>            # dry run
    python tools/repair_provisional_bars.py --db-file <app db> --cache-folder <folder> --apply
    # live caches, apps stopped:
    python tools/repair_provisional_bars.py --db-file <app db> --i-know-the-apps-are-stopped --apply
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from typing import Callable, Optional

PROVIDER = "FMPOHLCVProvider"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def live_cache_folder() -> str:
    home = os.path.abspath(os.getenv("BA2_HOME", os.path.join(os.path.expanduser("~"), "Documents", "ba2")))
    return os.path.join(home, "common", "cache")


def is_live(cache_folder: Optional[str]) -> bool:
    if not cache_folder:
        return True
    norm = lambda p: os.path.normcase(os.path.abspath(p))   # noqa: E731
    return norm(cache_folder) == norm(live_cache_folder())


def read_fmp_key(db_file: str) -> str:
    conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=5)
    try:
        row = conn.execute("SELECT value_str FROM appsetting WHERE key IN ('FMP_API_KEY','fmp_api_key') "
                           "AND value_str IS NOT NULL LIMIT 1").fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        raise SystemExit(f"repair_provisional_bars: no FMP_API_KEY in {db_file}")
    return row[0]


def _load_module():
    path = os.path.join(REPO, "ba2_trade_platform", "modules", "dataproviders", "ohlcv_provisional.py")
    spec = importlib.util.spec_from_file_location("_ba2_ohlcv_provisional", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)     # imports only ba2_common: no app package __init__
    return mod


def main(argv=None, provider_factory: Optional[Callable[[], object]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db-file", help="app DB the FMP key is read from (read-only)")
    ap.add_argument("--cache-folder", help="cache root holding FMPOHLCVProvider/ (default: the live one)")
    ap.add_argument("--lookback", type=int, default=10, help="newest sessions scanned (default 10)")
    ap.add_argument("--symbols", help="comma-separated subset")
    ap.add_argument("--apply", action="store_true", help="rewrite the files (default: dry run)")
    ap.add_argument("--i-know-the-apps-are-stopped", action="store_true", dest="stopped")
    args = ap.parse_args(argv)

    if is_live(args.cache_folder) and not args.stopped:
        print("REFUSED: this is the live cache folder; stop the apps (8080/8081/8082) and pass "
              "--i-know-the-apps-are-stopped", file=sys.stderr)
        return 2
    cache = os.path.abspath(args.cache_folder) if args.cache_folder else live_cache_folder()
    os.environ["CACHE_FOLDER"] = cache          # BEFORE any ba2_common import: it reads it at import

    if provider_factory is None:
        if not args.db_file:
            print("--db-file is required (the FMP key is read from it)", file=sys.stderr)
            return 2
        key = read_fmp_key(args.db_file)

        def provider_factory():   # noqa: E306
            from ba2_providers.ohlcv.FMPOHLCVProvider import FMPOHLCVProvider
            return FMPOHLCVProvider(api_key=key)

    import pandas as pd
    from ba2_common.core import native_cache
    native_cache.CACHE_FOLDER = cache
    native_cache._CACHE_ROOT = os.path.join(cache, "datasets", "cache")
    import ba2_common.config as bcfg
    bcfg.CACHE_FOLDER = cache
    prov_mod = _load_module()

    provider = provider_factory()
    wanted = {s.strip().upper() for s in args.symbols.split(",")} if args.symbols else None
    files = sorted(glob.glob(os.path.join(cache, PROVIDER, "*_1d.parquet")))
    fetch_end = datetime.now() + timedelta(days=1)
    total = fixed = errors = 0
    for path in files:
        symbol = os.path.basename(path)[:-len("_1d.parquet")]
        if wanted and symbol not in wanted:
            continue
        total += 1
        try:
            df = pd.read_parquet(path)
            out, replaced = prov_mod.repair_provisional_bars(
                provider, df, symbol, "1d", fetch_end, lookback_bars=args.lookback)
        except Exception as e:  # noqa: BLE001 -- reported per symbol, the scan continues
            errors += 1
            print(f"{symbol}: ERROR {type(e).__name__}: {e}")
            continue
        if not replaced:
            continue
        fixed += 1
        print(f"{symbol}: {len(replaced)} provisional bar(s) {[d.isoformat() for d in replaced]}"
              + (" -> replaced" if args.apply else " (dry run)"))
        if args.apply:
            native_cache.write_timeseries(PROVIDER, symbol, "1d", out)
    mode = "APPLIED" if args.apply else "DRY RUN"
    print(f"{mode}: scanned {total} file(s), {fixed} with stuck provisional bars, {errors} error(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
