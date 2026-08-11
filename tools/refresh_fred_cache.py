"""Refresh the FRED macro series disk cache.

The experts read this cache and NEVER fetch: ``fred_series.get_series_as_of`` raises on a
missing file rather than reaching for the network, so a backtest can't silently run on
absent macro data. This script is the only writer.

Run it:
  * before a backtest/optimization that uses DeterministicScorer (prewarm), and
  * on a schedule for the live platform, so the regime doesn't go stale.

The files land under ``CACHE_FOLDER/fred/`` and are therefore picked up by
``cache_sync.build_manifest`` automatically -- remote GA workers receive them with the
rest of the cache, no extra wiring.

Usage:
    python tools/refresh_fred_cache.py                  # refresh all series
    python tools/refresh_fred_cache.py --series VIXCLS UNRATE
    python tools/refresh_fred_cache.py --max-age-hours 24   # skip fresh files
    python tools/refresh_fred_cache.py --check              # report age, fetch nothing
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                                "packages", "common"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                                "packages", "providers"))

from ba2_providers.macro import fred_series  # noqa: E402


def _api_key() -> str:
    """The FRED key lives in AppSetting, not .env (matching FREDMacroProvider)."""
    from ba2_common.core.db import get_app_setting
    key = get_app_setting("fred_api_key")
    if not key:
        raise SystemExit(
            "FRED API key not configured. Set 'fred_api_key' in the AppSetting table "
            "(Settings page in the live UI).")
    return key


def _age_hours(path: str):
    if not os.path.exists(path):
        return None
    return (time.time() - os.path.getmtime(path)) / 3600.0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--series", nargs="*", default=None,
                    help="Series ids to refresh (default: every id in SERIES_SPEC)")
    ap.add_argument("--max-age-hours", type=float, default=None,
                    help="Skip series whose cached file is younger than this")
    ap.add_argument("--check", action="store_true",
                    help="Report cache state and exit without fetching")
    args = ap.parse_args()

    series = [s.upper() for s in (args.series or fred_series.SERIES_SPEC)]
    unknown = [s for s in series if s not in fred_series.SERIES_SPEC]
    if unknown:
        raise SystemExit(f"Unknown series {unknown}. Known: {sorted(fred_series.SERIES_SPEC)}")

    if args.check:
        print(f"{'series':<12} {'obs':>8}  {'age(h)':>8}  range")
        for sid in series:
            path = fred_series.cache_path(sid)
            age = _age_hours(path)
            if age is None:
                print(f"{sid:<12} {'-':>8}  {'MISSING':>8}")
                continue
            rows = json.load(open(path, encoding="utf-8"))["observations"]
            print(f"{sid:<12} {len(rows):>8,}  {age:>8.1f}  "
                  f"{rows[0]['date']} -> {rows[-1]['date']}")
        return

    key = _api_key()
    refreshed = skipped = failed = 0
    for sid in series:
        age = _age_hours(fred_series.cache_path(sid))
        if args.max_age_hours is not None and age is not None and age < args.max_age_hours:
            print(f"{sid:<12} skip (age {age:.1f}h)")
            skipped += 1
            continue
        try:
            n = fred_series.refresh_series(sid, key)
            print(f"{sid:<12} {n:,} observations")
            refreshed += 1
        except Exception as e:                       # noqa: BLE001 - report, keep going
            # One bad series must not block the rest; a partial refresh still leaves
            # every other series usable, and the failure is loud.
            print(f"{sid:<12} FAILED: {e}")
            failed += 1

    print(f"\nrefreshed={refreshed} skipped={skipped} failed={failed}")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
