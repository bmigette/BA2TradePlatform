"""Data-quality gate for the screener grid: worker cache sync + local data gaps.

Two independent checks, both meant to run as a manual/periodic gate before trusting a distributed
optimization run — NOT the per-job pre-flight (that's the fast, size-only push_cache, already run
automatically at the start of every optimization job):

1. WORKER CACHE SYNC (``check_cache_integrity``): reads + CRC32-checksums EVERY file in the
   master's cache and each configured remote worker's cache to catch the one drift (rel_path,
   size)-based sync can't see — a local rebuild that rewrites a file's content at its OLD byte
   size (e.g. the screener metric_store recomputing a column in place). CRC32, not a cryptographic
   hash: this is corruption/staleness detection over a possibly tens-of-GB cache, not a security
   boundary, so the much faster checksum is the right tradeoff.

2. LOCAL DATA GAPS (``check_local_gaps``): the master's OWN cache can be quietly incomplete even
   with no worker involved — a fetch that silently truncated, or a symbol whose provider stopped
   returning fresh bars. Checks the screener metric_store for missing ``ym=`` month partitions,
   and the OHLCV cache for per-symbol internal date gaps + symbols that fell behind the rest of
   the universe (their last cached bar is much older than the panel-wide newest bar).

Usage (test venv):
    ba2-venvs/test/Scripts/python.exe tools/cache_health_check.py [--worker NAME] [--fix]
        [--skip-workers] [--skip-gaps] [--ohlcv-interval 1d] [--max-gap-days 7] [--stale-days 10]

--fix removes anything `push_cache` already handles (missing + stale) via the normal push/prune,
then force-repushes any CONTENT-MISMATCH file (same rel_path/size, different crc32) — the one
case `push_cache`'s size-only diff cannot detect or fix on its own. --fix only applies to the
worker-sync check; local gaps are a report only (fixing them means re-fetching data, out of scope
here).
"""
import argparse
import os
import sys
from datetime import datetime, timedelta

_BACKEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "testplatform", "backend")
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)
os.chdir(_BACKEND_DIR)  # app.* relative imports + default DB path resolution expect this cwd


def _force_repush(worker: dict, rel_paths: list, log) -> dict:
    from app.services import cache_sync, worker_client
    local = cache_sync.build_manifest()
    stream = cache_sync.iter_tar(rel_paths, local["root"])
    import httpx
    base, headers = worker_client._base(worker), worker_client._headers(worker)
    with httpx.Client(timeout=None) as c:
        r = c.post(f"{base}/cache/push", headers=headers, content=stream)
        r.raise_for_status()
        res = r.json()
    log(f"force-repush -> {worker['name']}: {res}")
    return res


def check_screener_month_gaps(store_dir: str) -> list:
    """Return sorted ``YYYY-MM`` strings missing between the store's earliest and latest present
    ``ym=`` partition (a hole in an otherwise-continuous month range is a build that silently
    skipped a period, not an intentional start date)."""
    if not os.path.isdir(store_dir):
        return []
    present = sorted(d[len("ym="):] for d in os.listdir(store_dir) if d.startswith("ym="))
    if len(present) < 2:
        return []
    lo, hi = datetime.strptime(present[0], "%Y-%m"), datetime.strptime(present[-1], "%Y-%m")
    present_set = set(present)
    missing = []
    cur = lo
    while cur <= hi:
        key = cur.strftime("%Y-%m")
        if key not in present_set:
            missing.append(key)
        cur = (cur.replace(day=1) + timedelta(days=32)).replace(day=1)
    return missing


def check_ohlcv_gaps(provider_dir: str, interval: str = "1d",
                      max_gap_days: int = 7, stale_days: int = 10) -> dict:
    """Scan every ``<SYMBOL>_<interval>.parquet`` under *provider_dir* for internal date gaps
    (a run of missing calendar days > *max_gap_days* — big enough that it's not just a weekend/
    holiday) and symbols whose last cached bar trails the panel's TYPICAL last-bar date (the mode
    across all symbols, not the single newest) by more than *stale_days*.

    Mode, not max: fetches run on a rolling/staggered cadence, so a handful of symbols legitimately
    sit a few days ahead of the rest at any given moment — comparing against the single freshest
    symbol would flag almost the ENTIRE universe as "stale" every time (a real false-positive this
    check hit on the first run: 10919/10981 symbols vs. one 13-day-fresher outlier). The mode is
    what "the fetch pipeline is currently caught up to" for most of the universe; a cohort sitting
    materially behind IT is the actual signal (e.g. a subset stuck at a stale bulk-download date
    while the rest kept refreshing).

    Returns ``{scanned, panel_newest, panel_typical, internal_gaps: {symbol: [(gap_start,
    gap_end), ...]}, stale_symbols: {symbol: last_date}}``.
    """
    import glob
    import pandas as pd

    files = sorted(glob.glob(os.path.join(provider_dir, f"*_{interval}.parquet")))
    per_symbol_last: dict = {}
    internal_gaps: dict = {}
    for p in files:
        sym = os.path.basename(p)[: -len(f"_{interval}.parquet")]
        try:
            # utc=True + drop tz: some cached files store tz-naive dates, others tz-aware —
            # normalizing both to the same (tz-less) representation avoids a naive/aware compare.
            raw = pd.to_datetime(pd.read_parquet(p, columns=["Date"])["Date"], utc=True)
            dates = raw.dt.tz_localize(None).sort_values().unique()
        except Exception:  # noqa: BLE001 — unreadable/corrupt file is itself a data-quality finding
            internal_gaps[sym] = [("UNREADABLE", "UNREADABLE")]
            continue
        if len(dates) == 0:
            continue
        per_symbol_last[sym] = dates[-1]
        diffs = pd.Series(dates).diff().dt.days.to_numpy()[1:]
        gaps = [(str(pd.Timestamp(dates[i]).date()), str(pd.Timestamp(dates[i + 1]).date()))
                for i in range(len(diffs)) if diffs[i] > max_gap_days]
        if gaps:
            internal_gaps[sym] = gaps

    if not per_symbol_last:
        return {"scanned": 0, "panel_newest": None, "panel_typical": None,
                "internal_gaps": {}, "stale_symbols": {}}
    panel_newest = max(per_symbol_last.values())
    panel_typical = pd.Series(list(per_symbol_last.values())).mode().iloc[0]
    stale = {
        sym: str(pd.Timestamp(last).date())
        for sym, last in per_symbol_last.items()
        if (panel_typical - last).days > stale_days
    }
    return {
        "scanned": len(files), "panel_newest": str(pd.Timestamp(panel_newest).date()),
        "panel_typical": str(pd.Timestamp(panel_typical).date()),
        "internal_gaps": internal_gaps, "stale_symbols": stale,
    }


def check_local_gaps(max_gap_days: int, stale_days: int, interval: str) -> bool:
    """Report screener month-continuity + OHLCV per-symbol gaps/staleness on the LOCAL cache
    only (no worker involved). Returns True if everything looks continuous."""
    from ba2_common.config import SCREENER_STORE_DIR, CACHE_FOLDER

    print("\n=== local data gaps ===")
    ok = True

    missing_months = check_screener_month_gaps(SCREENER_STORE_DIR)
    print(f"  screener metric_store ({SCREENER_STORE_DIR}):")
    if missing_months:
        ok = False
        print(f"    MISSING {len(missing_months)} month partition(s): {missing_months}")
    else:
        print("    month coverage continuous.")

    provider_dir = os.path.join(CACHE_FOLDER, "FMPOHLCVProvider")
    print(f"  OHLCV cache ({provider_dir}, interval={interval}):")
    if not os.path.isdir(provider_dir):
        print("    directory not found, skipping.")
    else:
        res = check_ohlcv_gaps(provider_dir, interval=interval,
                                max_gap_days=max_gap_days, stale_days=stale_days)
        print(f"    scanned {res['scanned']} symbol file(s); panel-newest bar: {res['panel_newest']}, "
              f"panel-typical (mode) bar: {res['panel_typical']}")
        n_gap, n_stale = len(res["internal_gaps"]), len(res["stale_symbols"])
        print(f"    internal gaps (> {max_gap_days}d): {n_gap} symbol(s)")
        for sym, gaps in list(res["internal_gaps"].items())[:10]:
            print(f"      [gap] {sym}: {gaps[:3]}{' ...' if len(gaps) > 3 else ''}")
        print(f"    stale symbols (last bar > {stale_days}d behind panel-typical): {n_stale}")
        for sym, last in list(res["stale_symbols"].items())[:10]:
            print(f"      [stale] {sym}: last={last}")
        if n_gap or n_stale:
            ok = False

    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--worker", default=None, help="Check only this worker NAME (default: all enabled, non-local).")
    ap.add_argument("--fix", action="store_true", help="Push/prune/repush anything found out of sync (worker check only).")
    ap.add_argument("--skip-workers", action="store_true", help="Skip the remote-worker cache-sync check.")
    ap.add_argument("--skip-gaps", action="store_true", help="Skip the local data-gap check.")
    ap.add_argument("--ohlcv-interval", default="1d", help="OHLCV interval to gap-scan (default 1d — 5min is much slower).")
    ap.add_argument("--max-gap-days", type=int, default=7, help="Internal-gap threshold in calendar days (default 7).")
    ap.add_argument("--stale-days", type=int, default=10,
                    help="Flag a symbol whose last bar trails the panel-wide newest bar by more than this many days (default 10).")
    args = ap.parse_args()

    overall_ok = True

    if not args.skip_gaps:
        if not check_local_gaps(args.max_gap_days, args.stale_days, args.ohlcv_interval):
            overall_ok = False

    if args.skip_workers:
        print()
        print("ALL CHECKS PASSED." if overall_ok else "ISSUES FOUND (see above).")
        return 0 if overall_ok else 1

    from app.models.database import SessionLocal
    from app.models import Worker
    from app.services import worker_client

    db = SessionLocal()
    try:
        q = db.query(Worker).filter(Worker.is_local == False, Worker.is_enabled == True)  # noqa: E712
        if args.worker:
            q = q.filter(Worker.name == args.worker)
        workers = [{"id": w.id, "name": w.name, "url": w.url, "password": w.password} for w in q.all()]
    finally:
        db.close()

    if not workers:
        print("\nNo matching enabled remote workers configured.")
        print("ALL CHECKS PASSED." if overall_ok else "ISSUES FOUND (see above).")
        return 0 if overall_ok else 1

    print("\n=== worker cache sync ===")
    for w in workers:
        print(f"\n=== {w['name']} ({w['url']}) ===")
        try:
            res = worker_client.check_cache_integrity(w)
        except Exception as e:  # noqa: BLE001
            print(f"  UNREACHABLE / check failed: {e!r}")
            overall_ok = False
            continue

        print(f"  local files:  {res['local_count']}")
        print(f"  remote files: {res['remote_count']}")
        print(f"  missing (master has, worker doesn't/wrong size): {len(res['missing'])}")
        print(f"  stale   (worker has, master no longer does):     {len(res['stale'])}")
        print(f"  content mismatch (same rel_path+size, different crc32): {len(res['content_mismatch'])}")
        for label in ("missing", "stale", "content_mismatch"):
            for rel in res[label][:5]:
                print(f"    [{label}] {rel}")
            if len(res[label]) > 5:
                print(f"    [{label}] ... +{len(res[label]) - 5} more")

        if res["ok"]:
            print("  HEALTHY")
            continue

        if not args.fix:
            print("  OUT OF SYNC (re-run with --fix to repair)")
            overall_ok = False
            continue

        print("  fixing...")
        if res["missing"] or res["stale"]:
            worker_client.push_cache(w, log=print)
        if res["content_mismatch"]:
            _force_repush(w, res["content_mismatch"], log=print)
        # Re-check after fixing so the report reflects reality, not intent.
        res2 = worker_client.check_cache_integrity(w)
        print(f"  post-fix: ok={res2['ok']} missing={len(res2['missing'])} "
              f"stale={len(res2['stale'])} content_mismatch={len(res2['content_mismatch'])}")
        if not res2["ok"]:
            overall_ok = False

    print()
    print("ALL CHECKS PASSED." if overall_ok else "ISSUES FOUND (see above).")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
