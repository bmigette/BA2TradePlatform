r"""Repair FMP daily OHLCV caches stuck on a PROVISIONAL newest bar (AMD/INTC/MU/FSLR/QCOM/CLS, 2026-10).

A bar cached mid-session (a snapshot taken near 09:31 New York) makes the guarded top-up refuse the
symbol forever. For every ``<cache>/FMPOHLCVProvider/*_1d.parquet`` this tool replaces the NEWEST
cached bar with the vendor's final bar when the file proves it was written mid-session (file mtime
on that bar's own New York session date, before 20:00 ET) and the anchors/range checks of
``ba2_trade_platform/modules/dataproviders/ohlcv_provisional.py`` (the same code the live top-up
runs) hold. Nothing else in a file is touched; a file with nothing to repair is not rewritten.
It relies on the files' mtimes: do not copy a cache with new mtimes before running it.

EVERY RUN (dry run included) REQUIRES ``--i-know-the-apps-are-stopped``: stop the app whose cache
you point at (dev 8080, prod 8081, opt 8082 -- each has its own cache folder). A dry run does ask
the vendor (read-only) so its counts are what ``--apply`` would do; it writes nothing.
The FMP key is read (read-only sqlite3) from ``--db-file`` and never printed.

    --db-file       the app DB holding FMP_API_KEY (dev/prod/opt each have their own)
    --cache-folder  that app's cache root (the folder that CONTAINS FMPOHLCVProvider/);
                    default <BA2_HOME>/common/cache
    --symbols       comma list, e.g. AMD,INTC,MU,FSLR,QCOM,CLS

    python tools/repair_provisional_bars.py --db-file <db> --cache-folder <cache> --i-know-the-apps-are-stopped
    python tools/repair_provisional_bars.py --db-file <db> --cache-folder <cache> --i-know-the-apps-are-stopped --apply

Bars dated today (New York) are always skipped: they are still forming.

PER APP (add --symbols A,B,C and, after checking the dry run, --apply):
  dev  8080: --db-file C:\Users\basti\Documents\ba2\trade\db.sqlite
             --cache-folder C:\Users\basti\Documents\ba2\common\cache
  prod 8081: --db-file C:\Users\basti\Documents\ba2_trade_platform-prod\db.sqlite
             --cache-folder C:\Users\basti\Documents\ba2_trade_platform-prod\cache
  opt  8082: --db-file C:\Users\basti\Documents\ba2_trade_platform-opt\db.sqlite
             --cache-folder C:\Users\basti\Documents\ba2_trade_platform-opt\cache

DEV CAVEAT: the dev cache (<BA2_HOME>/common/cache) is SHARED with the test platform. Always pass
--symbols (never a blind scan), run between GA jobs, stop any local ba2-test fetch-cache / serve
first, and push the cache to the workers afterwards. Run in the Paris morning before 15:30 (before
the US open, so no bar is forming and FMP's 09:30 ET rate-limit window is avoided).
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Callable, Optional

PROVIDER = "FMPOHLCVProvider"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def live_cache_folder() -> str:
    home = os.path.abspath(os.getenv("BA2_HOME", os.path.join(os.path.expanduser("~"), "Documents", "ba2")))
    return os.path.join(home, "common", "cache")


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
    ap = argparse.ArgumentParser(description=__doc__.split(chr(10) * 2)[0], epilog=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db-file", help="app DB the FMP key is read from (read-only)")
    ap.add_argument("--cache-folder", help="cache root holding FMPOHLCVProvider/ (default: <BA2_HOME>/common/cache)")
    ap.add_argument("--lookback", type=int, default=5, help="newest sessions compared as anchors (default 5)")
    ap.add_argument("--symbols", help="comma-separated subset")
    ap.add_argument("--apply", action="store_true", help="rewrite the files (default: dry run)")
    ap.add_argument("--i-know-the-apps-are-stopped", action="store_true", dest="stopped")
    args = ap.parse_args(argv)

    if not args.stopped:
        print("REFUSED: stop the app that owns this cache (8080 dev / 8081 prod / 8082 opt) and pass "
              "--i-know-the-apps-are-stopped (required for dry runs too)", file=sys.stderr)
        return 2
    cache = os.path.abspath(args.cache_folder) if args.cache_folder else live_cache_folder()
    os.environ["CACHE_FOLDER"] = cache          # BEFORE any ba2_common import: it reads it at import
    # FMP HTTP errors carry ?apikey=...: redact every log line and every printed error text
    from ba2_trade_platform import log_redaction
    log_redaction.install()

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
            newest = pd.Timestamp(df["Date"].max()).date()
            if newest >= datetime.now(ZoneInfo("America/New_York")).date():
                continue                  # today's New York bar is still forming: never touched
            out, replaced = prov_mod.repair_provisional_bars(
                provider, df, symbol, "1d", fetch_end, mtime=os.path.getmtime(path),
                lookback_bars=args.lookback)
            if not replaced:
                continue
            fixed += 1
            print(f"{symbol}: {len(replaced)} provisional bar(s) {[d.isoformat() for d in replaced]}"
                  + (" -> replaced" if args.apply else " (dry run)"))
            if args.apply:
                native_cache.write_timeseries(PROVIDER, symbol, "1d", out)
        except Exception as e:  # noqa: BLE001 -- reported per symbol (redacted), the scan continues
            errors += 1
            print(f"{symbol}: ERROR {type(e).__name__}: {log_redaction.redact_text(str(e))}")
    mode = "APPLIED" if args.apply else "DRY RUN"
    print(f"{mode}: scanned {total} file(s), {fixed} with stuck provisional bars, {errors} error(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
