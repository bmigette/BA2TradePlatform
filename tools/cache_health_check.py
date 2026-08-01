"""Data-quality gate for the screener grid: worker cache sync + local data gaps + local data
VALIDITY over an explicit period.

Three independent checks, all meant to run as a manual/periodic gate before trusting a
distributed optimization run — NOT the per-job pre-flight (that's the fast, size-only
push_cache, already run automatically at the start of every optimization job):

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

3. LOCAL DATA VALIDITY over an explicit period (``check_period_validity``): gaps/presence alone
   miss the failure mode where a file/partition EXISTS for every month but its CONTENT is broken
   — e.g. a rebuild whose per-symbol API calls silently degraded (rate-limited, or a poisoned
   disk cache) so every row in a "complete" metric_store partition has ``market_cap = NaN``.
   Walks --start/--end month by month and validates: metric_store partitions (per-column NaN
   rate on the columns experts actually filter on — market_cap, price, relative_volume,
   price_drop_pct, weinstein_stage, atr_*), OHLCV coverage (does each sampled symbol have AT
   LEast one bar in each month, not just "no gap > N days" which a single stray bar can satisfy),
   and expert-warmed cache existence/non-triviality (analyst targets, earnings calendar, insider
   trades, congress skill/scalper scores — the caches that are NOT part of push_cache's OHLCV/
   metric_store sync and can rot silently, see docs/plans/2026-07-15-senate-weight-fast-
   optimization.md for the Senate-specific case of this).

Usage (test venv):
    ba2-venvs/test/Scripts/python.exe tools/cache_health_check.py [--worker NAME] [--fix]
        [--skip-workers] [--skip-gaps] [--ohlcv-interval 1d] [--max-gap-days 7] [--stale-days 10]
        [--start 2022-01-01] [--end 2026-06-30] [--skip-validity] [--validity-symbols 25]
        [--nan-threshold 0.5]

--fix removes anything `push_cache` already handles (missing + stale) via the normal push/prune,
then force-repushes any CONTENT-MISMATCH file (same rel_path/size, different crc32) — the one
case `push_cache`'s size-only diff cannot detect or fix on its own. --fix only applies to the
worker-sync check; local gaps/validity are report-only (fixing them means re-fetching data, out
of scope here).
"""
import argparse
import json
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


def _months_between(start: str, end: str) -> list:
    """Inclusive list of ``YYYY-MM`` strings from *start* to *end* (``YYYY-MM-DD`` each)."""
    lo = datetime.strptime(start, "%Y-%m-%d").replace(day=1)
    hi = datetime.strptime(end, "%Y-%m-%d").replace(day=1)
    out = []
    cur = lo
    while cur <= hi:
        out.append(cur.strftime("%Y-%m"))
        cur = (cur.replace(day=1) + timedelta(days=32)).replace(day=1)
    return out


# Columns experts actually filter/size on — a NaN-heavy column here silently starves every
# cap-band screen even though the partition file "exists" and passes the month-gap check.
_METRIC_STORE_VALIDITY_COLUMNS = [
    "market_cap", "price", "close", "relative_volume", "price_drop_pct",
    "weinstein_stage", "atr_14", "float_shares",
]


def check_metric_store_validity(store_dir: str, start: str, end: str,
                                 nan_threshold: float = 0.5) -> dict:
    """For every month in [start, end], load the partition (if present) and compute each
    validity column's NaN rate. A column whose NaN rate exceeds *nan_threshold* for a month is
    reported — this is the check that would have caught the 2026-07-17 incident where two
    successive ``build-screener-metrics`` rebuilds produced ~99% NaN ``market_cap`` (a poisoned
    market-cap-history disk cache from FMP rate-limiting) despite every ``ym=`` partition
    existing and the month-gap check passing cleanly.

    Returns ``{months_checked, months_missing: [...], bad: {month: {column: nan_rate}}}``.
    """
    import glob
    import pandas as pd

    months = _months_between(start, end)
    missing = []
    bad: dict = {}
    for ym in months:
        parts = sorted(glob.glob(os.path.join(store_dir, f"ym={ym}", "*.parquet")))
        if not parts:
            missing.append(ym)
            continue
        try:
            df = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
        except Exception as e:  # noqa: BLE001 — unreadable partition is itself a finding
            bad[ym] = {"UNREADABLE": str(e)}
            continue
        if len(df) == 0:
            bad[ym] = {"EMPTY": 0.0}
            continue
        month_bad = {}
        for col in _METRIC_STORE_VALIDITY_COLUMNS:
            if col not in df.columns:
                month_bad[col] = "MISSING_COLUMN"
                continue
            nan_rate = df[col].isna().mean()
            if nan_rate > nan_threshold:
                month_bad[col] = round(float(nan_rate), 3)
        if month_bad:
            bad[ym] = month_bad
    return {"months_checked": len(months), "months_missing": missing, "bad": bad}


def check_ohlcv_period_coverage(provider_dir: str, interval: str, start: str, end: str,
                                 max_symbols: int = 25) -> dict:
    """For a SAMPLE of up to *max_symbols* cached symbols, verify each month in [start, end] has
    at least one bar. This is stricter than ``check_ohlcv_gaps``'s internal-gap check: a lone bar
    surrounded by two >max_gap_days holes satisfies "no gap over threshold" at the file level but
    still leaves a whole month with zero real coverage for anything that queries that month.

    Returns ``{symbols_checked, per_symbol_missing_months: {symbol: [months...]}}``.
    """
    import glob
    import pandas as pd

    months = _months_between(start, end)
    files = sorted(glob.glob(os.path.join(provider_dir, f"*_{interval}.parquet")))[:max_symbols]
    per_symbol_missing: dict = {}
    for p in files:
        sym = os.path.basename(p)[: -len(f"_{interval}.parquet")]
        try:
            raw = pd.to_datetime(pd.read_parquet(p, columns=["Date"])["Date"], utc=True)
            present_months = set(raw.dt.tz_localize(None).dt.strftime("%Y-%m").unique())
        except Exception:  # noqa: BLE001
            per_symbol_missing[sym] = months  # unreadable -> treat as fully missing
            continue
        missing = [m for m in months if m not in present_months]
        if missing:
            per_symbol_missing[sym] = missing
    return {"symbols_checked": len(files), "per_symbol_missing_months": per_symbol_missing}


# (glob pattern under CACHE_FOLDER/fmp_history, human label) — the per-expert warmed caches that
# live OUTSIDE push_cache's OHLCV/metric_store sync path and so can rot silently between runs.
# The 3 congress_* scoring caches use a trailing ``*`` so the glob also matches the not-yet-
# COMPACTED ``<name>.delta.jsonl`` companion (see FMPSenateTraderWeight._save_scoring_cache_throttled)
# — without it, a long-lived worker that hasn't hit _CACHE_COMPACT_EVERY yet has ALL its data in the
# delta file and the base ``.json`` glob alone reports a false "no files found" (confirmed live on
# 2026-07-17: congress_scalper_scores.json had zero base entries but a populated delta).
_EXPERT_WARM_CACHE_PATTERNS = [
    ("*price_target*.json", "FMPRating analyst price targets"),
    ("*earnings*.json", "FMPEarningsDrift earnings calendar/surprises"),
    ("*insider*.json", "FMPInsiderClusterBuy insider transactions"),
    ("congress_skill_scores.json*", "FMPSenateTraderWeight trader-skill scores"),
    ("congress_scalper_scores.json*", "FMPSenateTraderWeight scalper/hold-time (roundtrip) scores"),
    ("congress_confidence_scores.json*", "FMPSenateTraderWeight trader-confidence scores"),
]


def check_expert_warm_cache(cache_folder: str) -> dict:
    """Existence + non-triviality (file present, non-empty, parseable, has >0 entries) of each
    known per-expert warmed-cache file pattern under ``CACHE_FOLDER/fmp_history``. These are NOT
    covered by push_cache's OHLCV/metric_store sync and are built lazily during trial execution
    for some experts (e.g. Senate's skill/scalper/confidence scores) — a missing or empty file
    here doesn't fail a job outright but silently degrades or (for Senate) can cause remote-worker
    trial timeouts as the cache gets rebuilt cold on every worker independently.

    A ``.delta.jsonl`` match (JSON-Lines, one ``{"k": ..., "v": ...}`` object per line) is counted
    by LINE, not ``json.load`` — it is not a single JSON document.

    Returns ``{pattern_label: {"files": n, "total_entries": n, "empty_files": [...]}}``.
    """
    import glob

    fmp_dir = os.path.join(cache_folder, "fmp_history")
    out = {}
    for pattern, label in _EXPERT_WARM_CACHE_PATTERNS:
        matches = sorted(glob.glob(os.path.join(fmp_dir, pattern)))
        total_entries = 0
        empty_files = []
        for p in matches:
            base = os.path.basename(p)
            try:
                if base.endswith(".delta.jsonl"):
                    with open(p, encoding="utf-8") as f:
                        n = sum(1 for line in f if line.strip())
                else:
                    with open(p, encoding="utf-8") as f:
                        data = json.load(f)
                    n = len(data) if hasattr(data, "__len__") else 1
                total_entries += n
                if n == 0:
                    empty_files.append(base)
            except Exception:  # noqa: BLE001
                empty_files.append(base + " (unreadable)")
        out[label] = {"files": len(matches), "total_entries": total_entries, "empty_files": empty_files}
    return out


# (glob pattern, human label, candidate per-row date field names tried in order) for the two
# expert-warmed caches confirmed to store a genuine per-row date: FMPRating's price-target
# history (``publishedDate``) and FMPInsiderClusterBuy's Form-4 history (``transactionDate``).
# Both are fetched FULL/unconditional per symbol (no from/to params — see
# ``fmp_history_disk_cached`` callers in FMPRating.py / FMPInsiderProvider.py), so unlike the
# metric_store market_cap/float bug (2026-08-01: symbol-only cache existence check silently
# served a shorter range after the window widened — see fetch_historical_market_cap's docstring)
# these can't be truncated by a caching short-circuit. This still checks a REAL thing: whether
# the underlying FMP history actually reaches back far enough for the requested period, which a
# plain existence/entry-count check (``check_expert_warm_cache`` above) cannot see.
_WARM_CACHE_DATE_FIELDS = [
    ("*price_target*.json", "FMPRating price-target history", ("publishedDate", "date")),
    ("*insider*.json", "FMPInsiderClusterBuy Form-4 history", ("transactionDate", "filingDate", "date")),
]


def check_warm_cache_date_coverage(cache_folder: str, start: str, sample: int = 20) -> dict:
    """For each pattern in ``_WARM_CACHE_DATE_FIELDS``, sample up to *sample* cached files and
    find each symbol's EARLIEST row date. Returns ``{label: {"sampled": n, "no_data_before_start":
    {symbol: earliest_date}, "unparseable": [symbols]}}`` — ``no_data_before_start`` symbols may
    be a genuine FMP history-depth limit (not necessarily a bug) but are worth knowing about
    before trusting a pre-``start`` backtest for that expert."""
    import glob

    fmp_dir = os.path.join(cache_folder, "fmp_history")
    out: dict = {}
    for pattern, label, date_fields in _WARM_CACHE_DATE_FIELDS:
        matches = sorted(glob.glob(os.path.join(fmp_dir, pattern)))[:sample]
        no_data_before_start: dict = {}
        unparseable: list = []
        for p in matches:
            base = os.path.basename(p)
            sym = base.split("__", 1)[-1].rsplit(".json", 1)[0] if "__" in base else base
            try:
                with open(p, encoding="utf-8") as f:
                    rows = json.load(f)
                if not isinstance(rows, list) or not rows:
                    continue  # empty-sentinel or non-list payload -> nothing to date-check
                dates = []
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    for field in date_fields:
                        v = row.get(field)
                        if v:
                            dates.append(str(v)[:10])
                            break
                if not dates:
                    unparseable.append(sym)
                    continue
                earliest = min(dates)
                if earliest > start:
                    no_data_before_start[sym] = earliest
            except Exception:  # noqa: BLE001 — unreadable file is itself a finding
                unparseable.append(sym + " (unreadable)")
        out[label] = {"sampled": len(matches), "no_data_before_start": no_data_before_start,
                      "unparseable": unparseable}
    return out


def check_senate_skill_score_coverage(cache_folder: str, start: str, end: str) -> dict:
    """Check ``congress_skill_scores.json``'s DATE-RANGE coverage against every month in
    [start, end] — existence/entry-count alone (``check_expert_warm_cache`` above) cannot catch
    this: the cache can hold thousands of entries and still leave whole YEARS of the grid's actual
    backtest range uncovered.

    This is exactly the gap that motivated ``_do_senate_scores`` in
    ``testplatform/ba2test_launcher.py``: a population=4 priming run left the cache with 550
    entries spanning ``2022-10-03``..``2024-12-09`` after 2+ hours — file-existence alone looked
    "fine" (non-empty, thousands-adjacent entry count) while the actual 2025-2026 half of the grid
    range had ZERO coverage, because each GA individual's distinct schedule genes only walk a
    different subset of calendar days (the cache key buckets by exact day — see
    ``FMPSenateTraderWeight._get_trader_skill_cached``'s docstring), so small-population reuse
    across trials is far lower than entry count alone suggests.

    Each key is ``"{trader}|{history_len}|{as_of_day}|{horizon_days}|{min_past}|{max_past}|
    {lookback_months}"`` — ``as_of_day`` (``YYYY-MM-DD``) is the 3rd pipe-delimited field. Merges
    the base file with any not-yet-compacted delta lines (mirrors
    ``FMPSenateTraderWeight._load_scoring_cache``).

    Returns ``{entries, distinct_days, months_checked, months_missing: [...]}``.
    """
    months = _months_between(start, end)
    path = os.path.join(cache_folder, "fmp_history", "congress_skill_scores.json")
    keys = []
    try:
        with open(path, encoding="utf-8") as f:
            keys.extend(json.load(f).keys())
    except Exception:  # noqa: BLE001 — missing/corrupt base -> no coverage from it
        pass
    try:
        with open(path + ".delta.jsonl", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    keys.append(json.loads(line)["k"])
    except Exception:  # noqa: BLE001 — missing/corrupt delta -> ignore
        pass
    as_of_days = {parts[2] for k in keys if len(parts := k.split("|")) >= 3}
    present_months = {d[:7] for d in as_of_days}
    missing_months = [m for m in months if m not in present_months]
    return {
        "entries": len(keys), "distinct_days": len(as_of_days),
        "months_checked": len(months), "months_missing": missing_months,
    }


def check_period_validity(start: str, end: str, nan_threshold: float,
                           validity_symbols: int, interval: str) -> bool:
    """Orchestrates the three validity sub-checks over [start, end] and prints a report.
    Returns True if nothing looks broken."""
    from ba2_common.config import SCREENER_STORE_DIR, CACHE_FOLDER

    print(f"\n=== local data VALIDITY ({start} .. {end}) ===")
    ok = True

    print(f"  metric_store column validity ({SCREENER_STORE_DIR}):")
    ms_res = check_metric_store_validity(SCREENER_STORE_DIR, start, end, nan_threshold)
    if ms_res["months_missing"]:
        ok = False
        print(f"    MISSING {len(ms_res['months_missing'])} month partition(s) in range: "
              f"{ms_res['months_missing'][:10]}{' ...' if len(ms_res['months_missing']) > 10 else ''}")
    if ms_res["bad"]:
        ok = False
        print(f"    {len(ms_res['bad'])}/{ms_res['months_checked']} month(s) have a column "
              f"exceeding {nan_threshold:.0%} NaN (or unreadable/empty):")
        for ym, cols in list(ms_res["bad"].items())[:10]:
            print(f"      [{ym}] {cols}")
        if len(ms_res["bad"]) > 10:
            print(f"      ... +{len(ms_res['bad']) - 10} more month(s)")
    if not ms_res["months_missing"] and not ms_res["bad"]:
        print(f"    all {ms_res['months_checked']} month(s) present, no column exceeds "
              f"{nan_threshold:.0%} NaN.")

    provider_dir = os.path.join(CACHE_FOLDER, "FMPOHLCVProvider")
    print(f"  OHLCV per-month coverage sample ({provider_dir}, interval={interval}, "
          f"{validity_symbols} symbol(s)):")
    if not os.path.isdir(provider_dir):
        print("    directory not found, skipping.")
    else:
        oh_res = check_ohlcv_period_coverage(provider_dir, interval, start, end, validity_symbols)
        n_bad = len(oh_res["per_symbol_missing_months"])
        print(f"    scanned {oh_res['symbols_checked']} symbol(s); "
              f"{n_bad} have >=1 month with zero bars in range")
        for sym, months in list(oh_res["per_symbol_missing_months"].items())[:10]:
            print(f"      [{sym}] missing: {months[:6]}{' ...' if len(months) > 6 else ''}")
        if n_bad:
            ok = False

    print(f"  expert-warmed cache existence ({os.path.join(CACHE_FOLDER, 'fmp_history')}):")
    ew_res = check_expert_warm_cache(CACHE_FOLDER)
    for label, info in ew_res.items():
        flag = ""
        if info["files"] == 0:
            flag = "  <-- no files found"
        elif info["empty_files"]:
            flag = f"  <-- {len(info['empty_files'])} empty/unreadable"
        print(f"    {label}: {info['files']} file(s), {info['total_entries']} total entries{flag}")
        if info["files"] == 0 or info["empty_files"]:
            ok = False

    print(f"  expert-warmed cache DATE-RANGE coverage (sampled, start={start}):")
    dc_res = check_warm_cache_date_coverage(CACHE_FOLDER, start)
    for label, info in dc_res.items():
        n_gap = len(info["no_data_before_start"])
        flag = f"  <-- {n_gap} symbol(s) with earliest data AFTER {start}" if n_gap else ""
        print(f"    {label}: sampled {info['sampled']}{flag}")
        for sym, earliest in list(info["no_data_before_start"].items())[:5]:
            print(f"      [{sym}] earliest={earliest}")
        if info["unparseable"]:
            print(f"      {len(info['unparseable'])} unparseable/no-date-field file(s): "
                  f"{info['unparseable'][:5]}")
        # Informational only (see _WARM_CACHE_DATE_FIELDS docstring: a gap here may be a genuine
        # FMP history-depth limit, not a bug) -- does not flip `ok`.

    print(f"  Senate trader-skill score DATE-RANGE coverage ({start} .. {end}):")
    sk_res = check_senate_skill_score_coverage(CACHE_FOLDER, start, end)
    print(f"    {sk_res['entries']} entries, {sk_res['distinct_days']} distinct as-of day(s)")
    if sk_res["months_missing"]:
        ok = False
        print(f"    MISSING coverage in {len(sk_res['months_missing'])}/{sk_res['months_checked']} "
              f"month(s): {sk_res['months_missing'][:10]}"
              f"{' ...' if len(sk_res['months_missing']) > 10 else ''}")
        print("    (run: ba2-test prewarm --experts FMPSenateTraderWeight --start <start> --end <end> "
              "-- see _do_senate_scores in testplatform/ba2test_launcher.py)")
    else:
        print(f"    all {sk_res['months_checked']} month(s) have >=1 scored as-of day.")

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
    ap.add_argument("--start", default=None, help="Period start (YYYY-MM-DD) for the validity check (default: metric_store's own earliest ym= partition).")
    ap.add_argument("--end", default=None, help="Period end (YYYY-MM-DD) for the validity check (default: metric_store's own latest ym= partition).")
    ap.add_argument("--skip-validity", action="store_true", help="Skip the period validity check (metric_store column NaN rates + OHLCV per-month coverage + expert warm-cache existence).")
    ap.add_argument("--validity-symbols", type=int, default=25, help="How many OHLCV symbols to sample for the per-month coverage check (default 25).")
    ap.add_argument("--nan-threshold", type=float, default=0.5, help="Flag a metric_store column if its NaN rate for a month exceeds this fraction (default 0.5 = 50%%).")
    args = ap.parse_args()

    overall_ok = True

    if not args.skip_gaps:
        if not check_local_gaps(args.max_gap_days, args.stale_days, args.ohlcv_interval):
            overall_ok = False

    if not args.skip_validity:
        from ba2_common.config import SCREENER_STORE_DIR
        start, end = args.start, args.end
        if start is None or end is None:
            present = sorted(d[len("ym="):] for d in os.listdir(SCREENER_STORE_DIR)
                              if d.startswith("ym=")) if os.path.isdir(SCREENER_STORE_DIR) else []
            if not present:
                print("\n=== local data VALIDITY ===\n  metric_store empty/missing, skipping (pass --start/--end explicitly).")
                present = None
            if present:
                start = start or f"{present[0]}-01"
                end = end or f"{present[-1]}-28"
        if start and end:
            if not check_period_validity(start, end, args.nan_threshold, args.validity_symbols, args.ohlcv_interval):
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
