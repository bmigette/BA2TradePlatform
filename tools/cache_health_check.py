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
import random
import sys
from datetime import datetime, timedelta
from typing import List, Optional

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
                                 max_symbols: int = 25, existence_interval: str = "1d") -> dict:
    """For a SAMPLE of up to *max_symbols* cached symbols, verify each month in [start, end] that
    the symbol ACTUALLY TRADED has at least one bar. Stricter than ``check_ohlcv_gaps``'s
    internal-gap check: a lone bar surrounded by two >max_gap_days holes satisfies "no gap over
    threshold" at the file level but still leaves a whole month with zero real coverage.

    "ACTUALLY TRADED" is the correction made 2026-08-17. The check used to flag EVERY month
    without a bar, which conflates two completely different things:

        (a) we failed to fetch data that exists      -> a real defect, must fail the gate
        (b) the security did not exist yet           -> nothing to fetch, ever

    On this universe (b) dominates and made the gate useless: it reported 40% of symbols
    "missing" 2020 data when the sample was SPAC units, preferreds and recent IPOs -- AACB
    (first traded 2025-04), AACI (2026-03), AACOU (2026-02), AACP (2026-05), AADX (2026-06).
    FMP returns nothing for those because there is nothing to return. A gate that cries wolf on
    60% of a legitimate cache gets overridden, which is worse than no gate.

    The DAILY file is the existence calendar: daily history goes back far further than intraday,
    so a month where 1d has bars but the target interval does not is a genuine hole, and a month
    where NEITHER has bars is a security that was not listed. This still catches the incident the
    check was written for (2026-08-16: symbols had 1d back to 2019 but no 5min before 2022 --
    those months have daily bars, so they are still flagged).

    Falls back to the old "any empty month counts" rule when the daily file is absent, since
    without it existence cannot be established; those symbols are reported separately so the
    fallback is never silent.

    Returns ``{symbols_checked, per_symbol_missing_months, months_skipped_not_listed,
    symbols_without_existence_calendar}``.
    """
    import glob
    import pandas as pd

    months = _months_between(start, end)
    # RANDOM sample, not the alphabetical head. Taking [:max_symbols] of a sorted glob checked the
    # same AACG/AAFTX/AAME prefix every run -- reproducible, and blind to any pattern that does not
    # happen to live at the start of the alphabet. That is how the 2026-08-16 gap survived: 93.6%
    # of 5min symbols had no pre-2020 history and this check never sampled widely enough to notice.
    # Seeded so a run is still reproducible, just not biased.
    _all = sorted(glob.glob(os.path.join(provider_dir, f"*_{interval}.parquet")))
    files = _all if len(_all) <= max_symbols else random.Random(1337).sample(_all, max_symbols)
    def _months_of(path: str):
        try:
            raw = pd.to_datetime(pd.read_parquet(path, columns=["Date"])["Date"], utc=True)
            return set(raw.dt.tz_localize(None).dt.strftime("%Y-%m").unique())
        except Exception:  # noqa: BLE001
            return None

    per_symbol_missing: dict = {}
    skipped_not_listed = 0
    no_calendar: list = []
    for p in files:
        sym = os.path.basename(p)[: -len(f"_{interval}.parquet")]
        present_months = _months_of(p)
        if present_months is None:
            per_symbol_missing[sym] = months  # unreadable -> treat as fully missing
            continue
        # Existence calendar from the DAILY file (skip when we ARE the daily file).
        eligible = months
        if interval != existence_interval:
            daily = _months_of(os.path.join(provider_dir, f"{sym}_{existence_interval}.parquet"))
            if daily is None:
                no_calendar.append(sym)          # fall back to the strict rule, but say so
            else:
                eligible = [m for m in months if m in daily]
                skipped_not_listed += len(months) - len(eligible)
        missing = [m for m in eligible if m not in present_months]
        if missing:
            per_symbol_missing[sym] = missing
    return {"symbols_checked": len(files), "per_symbol_missing_months": per_symbol_missing,
            "months_skipped_not_listed": skipped_not_listed,
            "symbols_without_existence_calendar": no_calendar}


# (glob pattern under CACHE_FOLDER/fmp_history, human label) — the per-expert warmed caches that
# live OUTSIDE push_cache's OHLCV/metric_store sync path and so can rot silently between runs.
#
# skill/confidence moved to PER-PARAMETER-COMBO shards (``congress_skill_scores__<params>.jsonl``,
# plain JSON-Lines, one shard per horizon/lookback/min_past/max_past combo — see the Senate
# scoring-store sharding work) — found 2026-08-01 that the OLD single-file glob
# (``congress_skill_scores.json*``) silently matches NOTHING post-sharding, so
# check_expert_warm_cache reported "0 files found" for genuinely-complete, 2020-2026-covering
# data (confirmed by direct read of the .jsonl shards). scalper is NOT sharded (still one file +
# delta companion, trailing ``*`` catches the not-yet-COMPACTED ``.delta.jsonl`` companion — see
# FMPSenateTraderWeight._save_scoring_cache_throttled; without it a long-lived worker that hasn't
# hit _CACHE_COMPACT_EVERY yet has ALL its data in the delta file and the base ``.json`` glob alone
# reports a false "no files found", confirmed live 2026-07-17).
_EXPERT_WARM_CACHE_PATTERNS = [
    ("*price_target*.json", "FMPRating analyst price targets"),
    ("*earnings*.json", "FMPEarningsDrift earnings calendar/surprises"),
    ("*insider*.json", "FMPInsiderClusterBuy insider transactions"),
    # Added 2026-08-07: these were a total blind spot. FactorRanker fetches with
    # lookback_periods=1 and the disk cache is keyed WITHOUT depth, so every
    # statement file on disk held exactly ONE row (the latest filing). Nothing
    # here noticed, because "file exists and has >0 entries" is true of a 1-row
    # file -- see _MIN_ROWS_PER_SYMBOL for the depth half of the check.
    ("income_statement_annual__*.json", "FactorRanker/DeterministicScorer income statements"),
    ("balance_sheet_annual__*.json", "FactorRanker/DeterministicScorer balance sheets"),
    ("cashflow_statement_annual__*.json", "FactorRanker/DeterministicScorer cash-flow statements"),
    ("analyst_grades__*.json", "FMPRating/DeterministicScorer analyst grades"),
    ("congress_senate_trades__*.json", "FMPSenateTrader* senate disclosures"),
    ("congress_house_trades__*.json", "FMPSenateTrader* house disclosures"),
    ("congress_skill_scores__*.jsonl", "FMPSenateTraderWeight trader-skill scores"),
    ("congress_scalper_scores.json*", "FMPSenateTraderWeight scalper/hold-time (roundtrip) scores"),
    ("congress_confidence_scores__*.jsonl", "FMPSenateTraderWeight trader-confidence scores"),
]


def check_expert_warm_cache(cache_folder: str) -> dict:
    """Existence + non-triviality (file present, non-empty, parseable, has >0 entries) of each
    known per-expert warmed-cache file pattern under ``CACHE_FOLDER/fmp_history``. These are NOT
    covered by push_cache's OHLCV/metric_store sync and are built lazily during trial execution
    for some experts (e.g. Senate's skill/scalper/confidence scores) — a missing or empty file
    here doesn't fail a job outright but silently degrades or (for Senate) can cause remote-worker
    trial timeouts as the cache gets rebuilt cold on every worker independently.

    Any ``.jsonl`` match (both the ``.delta.jsonl`` compaction companion AND a sharded
    ``congress_skill_scores__<params>.jsonl``) is JSON-LINES — one JSON object/line — and counted
    by LINE, not ``json.load``: a sharded file is not a single JSON document and would otherwise
    be misreported as unreadable.

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
                if base.endswith(".jsonl"):
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
    # Statements are POINT-IN-TIME gated on their FILING date, so an earliest
    # filing later than `start` means the fundamental sections are empty for the
    # whole window -- which is exactly what happened (earliest fillingDate
    # 2025-10-31 against a 2020 start) and what this check would have caught.
    ("income_statement_annual__*.json", "income-statement filing history",
     ("fillingDate", "filingDate", "acceptedDate", "date")),
    ("balance_sheet_annual__*.json", "balance-sheet filing history",
     ("fillingDate", "filingDate", "acceptedDate", "date")),
    ("cashflow_statement_annual__*.json", "cash-flow filing history",
     ("fillingDate", "filingDate", "acceptedDate", "date")),
    ("analyst_grades__*.json", "analyst-grades history", ("date", "publishedDate")),
]

# Minimum rows a per-symbol cache must hold before it can serve a MULTI-PERIOD
# calculation. Existence + ">0 entries" is not enough: a 1-row statement file is
# non-empty and still cannot compute Piotroski (needs 2 fiscal years) or growth
# acceleration (needs 3+). Only namespaces with a hard structural requirement are
# listed -- a thin price-target history is legitimate, a 1-row balance sheet is not.
# Share of symbols allowed to sit below the minimum before it counts as systemic
# rather than a legitimate tail (ETFs/SPAC units have no statements; a company
# listed last year has one filing). Measured after a full re-warm: ~5%.
MAX_SHALLOW_PCT = 15.0

_MIN_ROWS_PER_SYMBOL = {
    "income_statement_annual__*.json": 3,
    "balance_sheet_annual__*.json": 3,
    "cashflow_statement_annual__*.json": 3,
}


def check_warm_cache_depth(cache_folder: str, sample: int = 60) -> dict:
    """Flag per-symbol caches too SHALLOW for the calculations that read them.

    Catches the failure this tool missed for months: a depth-agnostic cache key
    plus a caller that fetches ``limit=1`` pins every file to one row, which then
    passes every existence/non-emptiness check while silently disabling whole
    scoring sections downstream.
    """
    import glob

    fmp_dir = os.path.join(cache_folder, "fmp_history")
    out: dict = {}
    for pattern, min_rows in _MIN_ROWS_PER_SYMBOL.items():
        matches = sorted(glob.glob(os.path.join(fmp_dir, pattern)))[:sample]
        shallow, counts = [], []
        for p in matches:
            base = os.path.basename(p)
            sym = base.split("__", 1)[-1].rsplit(".json", 1)[0]
            try:
                with open(p, encoding="utf-8") as f:
                    rows = json.load(f)
            except Exception:  # noqa: BLE001 — unreadable file is itself a finding
                shallow.append(f"{sym} (unreadable)")
                continue
            if isinstance(rows, dict):
                rows = rows.get("data") or rows.get("rows") or []
            n = len(rows) if hasattr(rows, "__len__") else 0
            counts.append(n)
            if n < min_rows:
                shallow.append(f"{sym}={n}")
        median = sorted(counts)[len(counts) // 2] if counts else 0
        pct = (len(shallow) / len(matches) * 100.0) if matches else 0.0
        # A SYSTEMIC failure (poisoned cache depth) drags the MEDIAN down -- when
        # FactorRanker's limit=1 pinned these files, the median was 1 and 100% of
        # symbols were short. A healthy cache still has a small legitimate tail:
        # ETFs and SPAC units have no financial statements at all, and a company
        # listed last year genuinely has one annual filing. Alarming per-symbol
        # would fire forever on that tail and train everyone to ignore this check
        # -- which is precisely how the original existence-only check became
        # useless. So the verdict keys on the median, with the tail reported for
        # information.
        out[pattern] = {
            "sampled": len(matches), "min_rows": min_rows,
            "median_rows": median,
            "shallow_count": len(shallow), "shallow_pct": round(pct, 1),
            "shallow": shallow[:15],
            "systemic": median < min_rows or pct > MAX_SHALLOW_PCT,
        }
    return out


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
    """Check the Senate trader-skill scores' DATE-RANGE coverage against every month in
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

    The cache is now PER-PARAMETER-COMBO SHARDED (``congress_skill_scores__<horizon>_<lookback>_
    <min_past>_<max_past>.jsonl``, plain JSON-Lines) rather than one file (found 2026-08-01: the
    old single-file check silently read nothing post-sharding and reported "0 entries, all months
    missing" for data that was actually complete). Reads across ALL shards + each shard's
    not-yet-compacted ``.delta.jsonl`` companion.

    Each key is ``"{trader}|{history_len}|{as_of_day}|{horizon_days}|{min_past}|{max_past}|
    {lookback_months}"`` — ``as_of_day`` (``YYYY-MM-DD``) is the 3rd pipe-delimited field.

    Returns ``{entries, distinct_days, months_checked, months_missing: [...]}``.
    """
    import glob

    months = _months_between(start, end)
    fmp_dir = os.path.join(cache_folder, "fmp_history")
    keys = []
    for path in sorted(glob.glob(os.path.join(fmp_dir, "congress_skill_scores__*.jsonl"))):
        if path.endswith(".delta.jsonl"):
            continue  # picked up alongside its base shard below
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        keys.append(json.loads(line)["k"])
        except Exception:  # noqa: BLE001 — missing/corrupt shard -> no coverage from it
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


def _default_options_universe_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "options_universe_large_cap.txt")


def _read_symbol_list(path: str) -> List[str]:
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [ln.strip().upper() for ln in f if ln.strip() and not ln.startswith("#")]


#: Vendor name -> its store's cache folder, matching warm_options_history.py's own
#: --provider -> default --out mapping (both providers write the SAME COLUMNS shape via the
#: SAME OptionHistoryParquetStore, just to separate trees, so one check function covers both).
_OPTIONS_PROVIDER_DIRS = {"tastytrade": "TastyTradeOptionsProvider",
                          "thetadata": "ThetaDataOptionsProvider"}


def check_options_cache(universe_path: Optional[str] = None, iv_sample: int = 15,
                         shallow_expiries_threshold: int = 3,
                         gap_start: Optional[str] = None, gap_end: Optional[str] = None,
                         provider: str = "tastytrade") -> dict:
    """Coverage + non-triviality of an options parquet store built by
    ``tools/warm_options_history.py --provider {tastytrade,thetadata}``
    (``CACHE_FOLDER/{TastyTradeOptionsProvider,ThetaDataOptionsProvider}``).

    Four things, none of which the other checks in this file touch (options data is entirely
    outside push_cache's OHLCV/metric_store sync and outside the fmp_history warm-cache checks):

    1. UNIVERSE COVERAGE: for each symbol in the target universe (default
       ``tools/options_universe_large_cap.txt``, the same list ``run_option_warmup_parallel.py``
       warms), whether it has at least one COMPLETED (underlying, expiry) manifest -- NOT just a
       directory. A directory alone is created the moment a symbol's chunk starts, before
       anything actually finishes, so counting it as "present" would call a symbol whose worker
       died mid-first-fetch "covered". Separately reports symbols the store has data for that
       AREN'T in the current universe file (harmless -- e.g. a symbol dropped from the universe
       after already being warmed, like the punctuation-ticker cleanup in warm_options_history.py)
       so that never reads as a coverage gap.

    2. PER-SYMBOL DEPTH: symbols WITH at least one completed partition but very few of them -- a
       stalled/interrupted worker chunk (see ``run_option_warmup_parallel.py``'s crash/reboot
       recovery) or a symbol that genuinely has a thin chain, indistinguishable from a directory
       listing alone. Symbols with ZERO completed partitions are reported separately (as
       "started, nothing finished yet"), not folded into this bucket -- they're a different
       situation (in flight or dead) from "finished a few, needs more".

    3. IV/OPEN_INTEREST/VOLUME NON-NULL RATE (sampled, from symbols that actually have data): the
       entire reason this store exists over the incumbent options_history.sqlite is that
       ``open_interest`` is NULL across all 1,440,782 of ITS chain rows, with no bar column to
       recover it (``imp_volatility`` is thin there rather than absent -- 46% of chain rows,
       88.2% of bar rows -- re-measured 2026-08-31; see
       ba2_common.core.option_selector._publishes_spread, parquet_store.py's COLUMNS docstring
       and warm_options_history.py's module docstring). A
       silently-regressed fetch that stopped populating these fields would pass every other check
       here (files exist, non-empty, right shape) while quietly reproducing the exact defect this
       pipeline was built to fix. ``open``/``high``/``low``/``close`` are NOT sampled here: they
       are written via ``float(bar.open)`` etc in ``parquet_store._frame`` (see there), which
       raises at write time rather than persisting a null, so a column-complete partition can
       only ever have those four columns fully populated -- sampling them would always report
       100% and catch nothing a "does the partition exist" check doesn't already catch.

    4. EXPIRY GAPS (every symbol with data, not sampled -- this is cheap: directory listing +
       date arithmetic, no parquet reads): ``--discovery synthetic`` (the default, and the only
       mode a personal OAuth app can use -- see warm_options_history.py) plans to fetch EVERY
       Friday in the window (``expiry_calendar``), so that is the oracle. A Friday with a
       manifest -- COMPLETE *or* EMPTY, a holiday-shifted Friday genuinely has no chain and is
       recorded as such -- is not a gap; a Friday with NO manifest at all inside the SPAN the
       symbol has otherwise already reached (strictly between its earliest and latest completed
       expiry) means that unit was queued and never finished: a worker chunk that died mid-run,
       or a resume that somehow skipped it. Fridays outside that span are a coverage/"shallow"
       question (the symbol just hasn't caught up yet), not a gap in the middle of finished work,
       so they are deliberately excluded here to keep this signal specific.

    Returns ``{universe_size, covered, zero_partitions: [...], never_started: [...],
    extra_not_in_universe: [...], shallow: {symbol: n_expiries}, total_partitions,
    iv_sample: {...}, gaps: {symbol: [missing_expiry_iso, ...]}, gap_window: [start, end]}``.
    """
    from ba2_providers.options.parquet_store import OptionHistoryParquetStore
    from ba2_providers.options.tastytrade import expiry_calendar
    from datetime import date

    if provider not in _OPTIONS_PROVIDER_DIRS:
        raise ValueError(f"unknown options provider {provider!r}; "
                         f"expected one of {sorted(_OPTIONS_PROVIDER_DIRS)}")
    import ba2_common.config as _cfg
    store = OptionHistoryParquetStore(
        root=os.path.join(_cfg.CACHE_FOLDER, _OPTIONS_PROVIDER_DIRS[provider]))
    universe_path = universe_path or _default_options_universe_path()
    universe = set(_read_symbol_list(universe_path))
    present_dirs = set(store.underlyings())

    # completed_expiries() is the ground truth ("has real data"), not underlyings() ("has a
    # directory") -- computed once per present symbol and reused for every category below.
    per_symbol_expiries = {sym: len(store.completed_expiries(sym)) for sym in sorted(present_dirs)}
    with_data = {sym for sym, n in per_symbol_expiries.items() if n > 0}
    total_partitions = sum(per_symbol_expiries.values())

    covered = sorted(universe & with_data) if universe else sorted(with_data)
    zero_partitions = sorted(universe & (present_dirs - with_data)) if universe else []
    never_started = sorted(universe - present_dirs) if universe else []
    extra_not_in_universe = sorted(present_dirs - universe) if universe else []
    shallow = {sym: n for sym in (universe & with_data if universe else with_data)
              if (n := per_symbol_expiries[sym]) < shallow_expiries_threshold}

    # IV/open_interest non-null rate, sampled from symbols that actually HAVE data -- sampling
    # from present_dirs would waste slots on the zero-partition case above (nothing to read).
    # Reading every symbol's full history here would be as slow as the fetch itself; a random
    # sample (seeded, so a re-run is comparable) is enough to catch a systemic regression (every
    # row null) vs. normal sparse-quote NaNs.
    iv_res = {"sampled": 0, "rows": 0, "iv_nonnull_pct": None, "oi_nonnull_pct": None,
              "volume_nonnull_pct": None, "close_nonnull_pct": None,
              "quote_nonnull_pct": None, "priceless_rows": 0, "unreadable": []}
    pool = sorted(with_data)
    sample_syms = pool if len(pool) <= iv_sample else random.Random(1337).sample(pool, iv_sample)
    iv_nonnull = oi_nonnull = vol_nonnull = rows = 0
    close_nonnull = quote_nonnull = priceless = 0
    for sym in sample_syms:
        try:
            df = store.read_underlying(sym)
        except Exception:  # noqa: BLE001 — an unreadable partition is itself a finding
            iv_res["unreadable"].append(sym)
            continue
        if df is None or df.empty:
            continue
        iv_res["sampled"] += 1
        rows += len(df)
        iv_nonnull += int(df["iv"].notna().sum())
        oi_nonnull += int(df["open_interest"].notna().sum())
        vol_nonnull += int(df["volume"].notna().sum())
        # CLOSE and QUOTE coverage. close is NULL on a day the contract did not trade, which
        # is normal and common (44.9% of a liquid ThetaData chain) -- so a low close rate is
        # NOT itself a fault. What IS a fault is a row with neither a close nor a quote: it
        # cannot be marked, and a price-less chain row skips the selector's penny-contract
        # gate instead of being rejected by it. Counted explicitly because the previous
        # version of this check sampled only iv/oi/volume and could not see either column.
        close_nn = df["close"].notna()
        close_nonnull += int(close_nn.sum())
        if "bid" in df.columns and "ask" in df.columns:
            quote_nn = df["bid"].notna() & df["ask"].notna()
        else:
            quote_nn = close_nn & False  # store predates the quote columns
        quote_nonnull += int(quote_nn.sum())
        priceless += int((~close_nn & ~quote_nn).sum())
    iv_res["rows"] = rows
    iv_res["priceless_rows"] = priceless
    if rows:
        iv_res["iv_nonnull_pct"] = round(iv_nonnull / rows * 100.0, 1)
        iv_res["oi_nonnull_pct"] = round(oi_nonnull / rows * 100.0, 1)
        iv_res["volume_nonnull_pct"] = round(vol_nonnull / rows * 100.0, 1)
        iv_res["close_nonnull_pct"] = round(close_nonnull / rows * 100.0, 1)
        iv_res["quote_nonnull_pct"] = round(quote_nonnull / rows * 100.0, 1)

    # Expiry gaps: see docstring point 4. The oracle is every Friday --discovery synthetic
    # would have PLANNED to fetch; comparing that against completed_expiries() (which counts
    # EMPTY manifests as present, so a real holiday-shifted Friday is never a false positive)
    # finds a hole left by a chunk that died mid-run or a resume that skipped ahead.
    gstart = date.fromisoformat(gap_start) if gap_start else date(2023, 1, 1)
    gend = date.fromisoformat(gap_end) if gap_end else date.today()
    expected = expiry_calendar(gstart, gend)
    gaps: dict = {}
    for sym in (covered if universe else sorted(with_data)):
        actual = set(store.completed_expiries(sym))
        if not actual:
            continue
        lo, hi = min(actual), max(actual)
        missing = [e for e in expected if lo < e < hi and e not in actual]
        if missing:
            gaps[sym] = [e.isoformat() for e in missing]

    return {
        "universe_size": len(universe), "covered": covered,
        "zero_partitions": zero_partitions, "never_started": never_started,
        "extra_not_in_universe": extra_not_in_universe,
        "total_partitions": total_partitions, "shallow": shallow, "iv_sample": iv_res,
        "gaps": gaps, "gap_window": [gstart.isoformat(), gend.isoformat()],
        "store_root": store.root, "disk_bytes": store.disk_bytes(),
    }


def print_options_cache_report(universe_path: Optional[str], iv_sample: int,
                                gap_start: Optional[str] = None,
                                gap_end: Optional[str] = None,
                                provider: str = "tastytrade") -> bool:
    """Runs check_options_cache and prints a report. Returns True if nothing looks broken."""
    print(f"\n=== options cache ({_OPTIONS_PROVIDER_DIRS[provider]}) ===")
    res = check_options_cache(universe_path, iv_sample, gap_start=gap_start, gap_end=gap_end,
                              provider=provider)
    ok = True

    print(f"  store root: {res['store_root']}  ({res['disk_bytes'] / (1 << 30):.2f} GB on disk)")
    print(f"  universe coverage: {len(res['covered'])}/{res['universe_size']} symbol(s) have "
          f"at least one COMPLETED partition; {res['total_partitions']} completed "
          f"(underlying, expiry) partition(s) total")

    if res["zero_partitions"]:
        n = len(res["zero_partitions"])
        print(f"  STARTED, NOTHING FINISHED YET ({n}) -- in flight, or a dead/interrupted "
              f"chunk: {res['zero_partitions'][:15]}{' ...' if n > 15 else ''}")
        ok = False

    if res["never_started"]:
        n = len(res["never_started"])
        print(f"  NEVER STARTED ({n}): {res['never_started'][:15]}"
              f"{' ...' if n > 15 else ''}")
        ok = False

    if res["extra_not_in_universe"]:
        n = len(res["extra_not_in_universe"])
        print(f"  ({n} symbol(s) cached but not in the current universe file -- harmless, e.g. "
              f"dropped from the universe after being warmed: {res['extra_not_in_universe'][:10]}"
              f"{' ...' if n > 10 else ''})")

    if res["shallow"]:
        print(f"  shallow ({len(res['shallow'])} symbol(s) with < a handful of completed "
              f"expiries -- likely still in progress or an interrupted chunk):")
        for sym, n in sorted(res["shallow"].items(), key=lambda kv: kv[1])[:15]:
            print(f"    [{sym}] {n} expiries")
    else:
        print("  no shallow symbols (every present symbol has a reasonable expiry count).")

    iv = res["iv_sample"]
    print(f"  IV/open_interest/volume non-null rate (sampled {iv['sampled']} symbol(s), "
          f"{iv['rows']} row(s)):")
    if iv["rows"]:
        print(f"    iv: {iv['iv_nonnull_pct']}%   open_interest: {iv['oi_nonnull_pct']}%   "
              f"volume: {iv['volume_nonnull_pct']}%")
        # A near-total null rate on a REAL sample is exactly the incumbent-cache defect this
        # store exists to fix -- fail loud, don't just note it.
        if iv["iv_nonnull_pct"] < 5.0 and iv["oi_nonnull_pct"] < 5.0:
            print("    <-- both near-zero: this looks like the exact defect this store was "
                  "built to avoid (see module docstring). Check the fetch path, not just data.")
            ok = False
        if iv["volume_nonnull_pct"] < 50.0:
            print("    <-- volume mostly null: bars without volume are close to useless even "
                  "with iv/open_interest populated. Check the fetch path.")
            ok = False
    else:
        print("    no readable rows sampled -- store may be empty or still warming up.")
    if iv["unreadable"]:
        print(f"    unreadable: {iv['unreadable']}")
        ok = False

    gaps = res["gaps"]
    gw = res["gap_window"]
    if gaps:
        n_missing = sum(len(v) for v in gaps.values())
        print(f"  EXPIRY GAPS ({len(gaps)} symbol(s), {n_missing} missing Friday(s) total, "
              f"window {gw[0]}..{gw[1]}) -- fetched some later expiries but skipped one in the "
              f"middle; likely an interrupted worker chunk:")
        for sym, missing in sorted(gaps.items())[:15]:
            shown = missing[:5]
            print(f"    [{sym}] {shown}{' ...' if len(missing) > 5 else ''}")
        ok = False
    else:
        print(f"  no expiry gaps (window {gw[0]}..{gw[1]}): every symbol's completed expiries "
              f"are contiguous with no unfetched Friday between its earliest and latest.")

    return ok


# Expert -> max indicator lookback in trading BARS, and the bars->calendar-days conversion.
# MIRRORS ``daily_backtest_handler._EXPERT_WARMUP_BARS`` / ``derive_warmup_days`` (kept as a
# local copy rather than an import so this tool stays runnable without the backtest package on
# the path; if the engine's table changes, update here too).
_WARMUP_BARS_BY_EXPERT = {
    "FactorRanker": 252,          # momentum_12_1 (12 months) -- the deepest lookback
    # PremiumSeller (300 bars) removed 2026-08-31 with the expert's deletion (plan Task 12).
    "FMPRating": 10,
    "FMPEarningsDrift": 10,
    "FMPInsiderClusterBuy": 10,
    "FinnHubRating": 10,
    "FMPSenateTraderWeight": 10,  # ATR floor governs
    "FMPSenateTraderCopy": 10,
}
_WARMUP_FLOOR_DAYS = 60
_BARS_TO_CALDAYS = 1.45


def warmup_days_for(experts: List[str]) -> int:
    """Calendar-day warmup the engine will derive for *experts* (mirrors derive_warmup_days)."""
    max_bars = 14  # ATR-14 floor
    for name in experts:
        max_bars = max(max_bars, _WARMUP_BARS_BY_EXPERT.get(name, 20))
    return max(_WARMUP_FLOOR_DAYS, int(max_bars * _BARS_TO_CALDAYS) + 10)


def check_warmup_coverage(store_dir: str, provider_dir: str, interval: str, start: str,
                           experts: List[str], max_symbols: int = 0) -> dict:
    """DEPENDENCY check: does each symbol the run will actually SCREEN have enough OHLCV history
    BEFORE ``start`` to satisfy the indicator warmup the engine derives for *experts*?

    Presence/NaN checks cannot catch this. A metric_store month can be fully populated and every
    OHLCV file present, yet an indicator that needs N bars of lead-in (EMA-200, ATR-14,
    Weinstein's 30-week MA, FactorRanker's 252-bar momentum) silently yields NaN/garbage for any
    symbol whose history STARTS at the backtest's start date. Found 2026-08-01: 1114/2290 (48.6%)
    of the symbols in the ``ym=2020-01`` partition had ZERO bars before 2020-01-01 -- all starting
    exactly 2020-01-02 -- because the 1d backfill was run with ``--start 2020-01-01`` and the
    OHLCV as-of cache only ever extends FORWARD, never backward, on a warm hit. The tell was
    weinstein_stage sitting at ~50% NaN through early 2020, almost exactly matching that 48.6%.

    Universe = the symbols present in the metric_store partition for ``start``'s month (i.e. what
    the screener can actually select on day one), not the whole OHLCV cache -- a symbol that never
    passes the screen needing no warmup is not a finding.

    Returns ``{required_days, universe, checked, ok, insufficient: {symbol: bars_before_start},
    zero: n, examples: [...]}``.
    """
    import glob
    import pandas as pd

    required_days = warmup_days_for(experts)
    start_ts = pd.Timestamp(start)
    warmup_start = start_ts - pd.Timedelta(days=required_days)
    # Trading bars expected in that calendar window (~252/yr), with slack for holidays.
    required_bars = int(required_days / _BARS_TO_CALDAYS * 0.9)

    ym = start_ts.strftime("%Y-%m")
    parts = sorted(glob.glob(os.path.join(store_dir, f"ym={ym}", "*.parquet")))
    if not parts:
        return {"required_days": required_days, "universe": 0, "checked": 0, "ok": 0,
                "insufficient": {}, "zero": 0, "examples": [],
                "error": f"no metric_store partition for ym={ym}"}
    df = pd.concat([pd.read_parquet(p, columns=["symbol"]) for p in parts], ignore_index=True)
    syms = sorted(df["symbol"].unique())
    if max_symbols and len(syms) > max_symbols:
        syms = syms[:max_symbols]

    insufficient, zero, ok = {}, 0, 0
    for s in syms:
        p = os.path.join(provider_dir, f"{s}_{interval}.parquet")
        if not os.path.exists(p):
            insufficient[s] = -1  # no file at all
            continue
        try:
            dts = pd.to_datetime(pd.read_parquet(p, columns=["Date"])["Date"], utc=True).dt.tz_localize(None)
        except Exception:  # noqa: BLE001
            insufficient[s] = -1
            continue
        n_before = int(((dts >= warmup_start) & (dts < start_ts)).sum())
        if n_before == 0:
            zero += 1
            insufficient[s] = 0
        elif n_before < required_bars:
            insufficient[s] = n_before
        else:
            ok += 1
    return {"required_days": required_days, "required_bars": required_bars,
            "universe": len(syms), "checked": len(syms), "ok": ok,
            "insufficient": insufficient, "zero": zero,
            "examples": sorted(insufficient.items())[:10]}


# Experts the warmup-dependency check sizes against: every non-option expert a goal2020-style
# equity grid runs. FactorRanker's 252-bar momentum dominates, so this is the deepest requirement
# any equity job will derive. (PremiumSeller — options, 300 bars — was excluded here until its
# deletion, 2026-08-31: the options cache floors at 2024, so it could never run a 2020 window.)
_warmup_experts = ["FactorRanker", "FMPRating", "FMPEarningsDrift",
                   "FMPInsiderClusterBuy", "FMPSenateTraderWeight"]


def check_period_validity(start: str, end: str, nan_threshold: float,
                           validity_symbols: int, interval: str) -> bool:
    """Orchestrates the validity sub-checks over [start, end] and prints a report.
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
              f"{n_bad} have >=1 month with zero bars in a month they TRADED")
        if oh_res.get("months_skipped_not_listed"):
            print(f"    ({oh_res['months_skipped_not_listed']} symbol-months skipped: the security "
                  f"was not listed yet -- not a cache defect)")
        if oh_res.get("symbols_without_existence_calendar"):
            nc = oh_res["symbols_without_existence_calendar"]
            print(f"    ({len(nc)} symbol(s) have no {interval!r} daily file to establish listing "
                  f"dates, judged strictly: {nc[:5]})")
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

    print("  WARM-CACHE DEPTH (multi-period calculations need >1 row per symbol):")
    depth = check_warm_cache_depth(CACHE_FOLDER)
    for pattern, info in depth.items():
        if not info["sampled"]:
            print(f"    {pattern}: no files sampled")
            continue
        tail = (f"  ({info['shallow_count']}/{info['sampled']} = {info['shallow_pct']}% "
                f"below {info['min_rows']}: ETFs/SPAC units/recent IPOs)"
                if info["shallow_count"] else "")
        if info["systemic"]:
            tail = (f"  <-- SYSTEMIC: median {info['median_rows']} < {info['min_rows']} or "
                    f"{info['shallow_pct']}% short -- cache depth looks poisoned")
            ok = False
        print(f"    {pattern}: median {info['median_rows']} row(s){tail}")

    print(f"  INDICATOR WARMUP dependency ({start} start, experts={','.join(_warmup_experts)}):")
    wu = check_warmup_coverage(SCREENER_STORE_DIR, provider_dir, interval, start, _warmup_experts)
    if wu.get("error"):
        print(f"    {wu['error']}")
    else:
        n_bad = len(wu["insufficient"])
        print(f"    needs {wu['required_days']}d (~{wu['required_bars']} bars) of history BEFORE "
              f"{start}; day-one screen universe = {wu['universe']} symbol(s)")
        print(f"    sufficient: {wu['ok']}   insufficient: {n_bad} (of which {wu['zero']} have "
              f"ZERO pre-{start} bars)")
        for sym, n in wu["examples"]:
            print(f"      [{sym}] bars_before_start={'NO FILE' if n < 0 else n}")
        if n_bad:
            ok = False
            print(f"    -> indicators needing lead-in (EMA/ATR/Weinstein/momentum) will be NaN or "
                  f"wrong for those symbols. Re-fetch 1d OHLCV with a start >= {wu['required_days']}d "
                  f"before {start}.")

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
    ap.add_argument("--check-options", action="store_true",
                    help="Also check the options parquet store(s) -- see --options-provider "
                         "(universe coverage, per-symbol partition depth, "
                         "IV/open_interest/volume non-null rate, per-symbol expiry gaps). Off "
                         "by default -- separate from the equity-grid checks above and can be "
                         "slow on a large store.")
    ap.add_argument("--options-provider", choices=("tastytrade", "thetadata", "both"),
                    default="tastytrade",
                    help="Which options parquet store(s) to check (default tastytrade, "
                         "matching warm_options_history.py's own default). 'both' checks the "
                         "TastyTrade and ThetaData trees separately, one report each -- they "
                         "are always distinct folders, never merged.")
    ap.add_argument("--options-universe", default=None,
                    help="Symbol list to check options coverage against (default: "
                         "tools/options_universe_large_cap.txt).")
    ap.add_argument("--options-iv-sample", type=int, default=15,
                    help="How many symbols to sample for the IV/open_interest/volume non-null check (default 15).")
    ap.add_argument("--options-gap-start", default=None,
                    help="Start of the expiry-gap oracle window, YYYY-MM-DD (default: 2023-01-01, "
                         "matching warm_options_history.py's DEFAULT_START).")
    ap.add_argument("--options-gap-end", default=None,
                    help="End of the expiry-gap oracle window, YYYY-MM-DD (default: today).")
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

    if args.check_options:
        providers = (list(_OPTIONS_PROVIDER_DIRS) if args.options_provider == "both"
                    else [args.options_provider])
        for prov in providers:
            if not print_options_cache_report(args.options_universe, args.options_iv_sample,
                                              args.options_gap_start, args.options_gap_end,
                                              provider=prov):
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
