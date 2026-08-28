"""Export/import the shared ML data (provider cache + backtest/optimize results DB) as a
single portable zip -- for moving a warmed-up cache to/from another machine, or backing it up
off the dev box (e.g. onto Google Drive) without hand-picking dozens of cache subfolders.

Sources (fixed, matching ba2_common.config's data layout -- see that file's "BA2 data root
layout" comment):
    cache: CACHE_FOLDER   (~/Documents/ba2/common/cache -- OHLCV/FMP/options/screener provider
                           cache, shared by both platforms)
    db:    TEST_DIR/dl_forecasting.db (~/Documents/ba2/test/dl_forecasting.db -- the backtest/
                           optimize results DB: backtests, strategies, job results)

The DB is live (WAL mode, actively written by a running server) so a raw file copy can grab an
inconsistent snapshot mid-write. Export always takes it via sqlite3's backup API instead, which
is the engine's own safe-copy path and does not require stopping the server.

Export scopes (--scope):
    full   cache + complete DB (default; the historical behavior)
    cache  cache only, no DB
    db     complete DB only, no cache
    bt-ga  DB trimmed to ALL backtests + ALL GA jobs (strategy_optimizations), plus the
           strategies they reference and their robustness runs -- no cache
    bt     DB trimmed to backtests only (GA jobs dropped, backtests.optimization_id set to
           NULL in the exported copy), optionally filtered by labels -- no cache

In bt/bt-ga scopes the runtime-only tables (task_queue, activitylog, persistedqueuetask,
llmusagelog) are cleared from the exported copy, and the DB is VACUUMed down.

Date-windowed cache (--date-start/--date-end, only with --scope full or cache): every cache
file is opened and trimmed to that window BEFORE being written to the zip -- not just a
directory-level partition pick. See _filter_cache_file for the per-format rules (parquet: the
first matching column from DATE_COLUMN_CANDIDATES; the legacy options sqlite: as_of/date per
table; fmp_history JSON: a dataset-specific date key or a generic "*date*" key scan; the
TastyTrade options store's exp=YYYY-MM-DD partitions are windowed on their BAR dates, same as
everything else, because a contract's bars run for months before its expiry). A file whose
format or date field cannot be determined is DROPPED, not included whole -- "only this window"
would otherwise be a lie for whatever slipped through unfiltered. See FILTER_DROP_REASONS for
what each drop reason means, and the run's own printed summary for counts.

Usage:
    python tools/ml_cache_archive.py export --dest-dir "G:\\Mon Drive\\Work\\AiTrading\\Test ML Cache"
    python tools/ml_cache_archive.py export --dest-dir <dir> --name my_export.zip --overwrite
    python tools/ml_cache_archive.py export --dest-dir <dir> --scope db
    python tools/ml_cache_archive.py export --dest-dir <dir> --scope bt-ga
    python tools/ml_cache_archive.py export --dest-dir <dir> --scope bt --label goal2020 --label sen5min
    python tools/ml_cache_archive.py export --dest-dir <dir> --scope cache \\
        --date-start 2023-01-01 --date-end 2023-03-31
    python tools/ml_cache_archive.py import --archive <path/to/export.zip>
    python tools/ml_cache_archive.py import --archive <path> --overwrite --force
"""
import argparse
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
import zipfile

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                                "packages", "common"))
from ba2_common.config import CACHE_FOLDER, TEST_DIR  # noqa: E402

DEFAULT_NAMES = {
    "full": "ba2_ml_cache_export.zip",
    "cache": "ba2_cache_export.zip",
    "db": "ba2_db_export.zip",
    "bt-ga": "ba2_btga_export.zip",
    "bt": "ba2_bt_export.zip",
}

# ---------------------------------------------------------------------------
# Date-window content filtering (--date-start/--date-end)
# ---------------------------------------------------------------------------
# Checked in order against a parquet file's actual columns; the first match wins. Covers
# every parquet shape under CACHE_FOLDER as surveyed 2026-08-28: 'bar_date' (options),
# 'date' (screener metric_store, screener_fundamentals/*), 'Date' (every *OHLCVProvider,
# capitalised -- yfinance's own convention, carried through unchanged), 'published_at'
# (news sentiment scores).
DATE_COLUMN_CANDIDATES = ("bar_date", "date", "Date", "published_at", "as_of")

# fmp_history/<prefix>__<SYMBOL>.json -> which key(s) to try, in order, as EACH record's
# date. Not exhaustive of every prefix seen under fmp_history/ (there are ~18) -- prefixes
# missing here fall back to a generic scan for any key whose name contains "date" (see
# _record_date in _filter_json_bytes), which covers the rest correctly as long as they
# follow the same convention. A prefix that carries no per-record date at all (e.g. the
# congress skill-score JSONL, which is a whole-history aggregate: roundtrips, avg_hold_days
# -- nothing a 3-month window could mean anything for) is dropped, not guessed at.
FMP_HISTORY_DATE_KEYS = {
    "balance_sheet_annual": ("date",),
    "income_statement_annual": ("date",),
    "cashflow_statement_annual": ("date",),
    "earnings_estimates_quarterly": ("date",),
    "past_earnings_quarterly": ("date",),
    "grades_historical": ("date",),
    "analyst_grades": ("date",),
    "historical_price_full": ("date",),
    "price_target": ("publishedDate",),
    "insider": ("filingDate", "transactionDate"),
    "insider_v2": ("filingDate", "transactionDate"),
    "congress_house_trades": ("disclosureDate", "transactionDate"),
    "congress_senate_trades": ("disclosureDate", "transactionDate"),
    "congress_house_latest": ("disclosureDate", "transactionDate"),
    "congress_senate_latest": ("disclosureDate", "transactionDate"),
    "congress_trader_history": ("disclosureDate", "transactionDate"),
}

# What each drop reason (the summary this run prints, and the manifest's "cache_filter"
# block) actually means -- so "1,204 dropped: no_date_field" is something an operator can
# act on rather than a mystery count.
FILTER_DROP_REASONS = {
    "empty": "had a usable date field, but no rows/records fell inside the window",
    "no_date_field": "no recognisable date column/key -- cannot be windowed, so not included",
    "meta_sidecar": "a .meta.json stats sidecar describing the WHOLE unfiltered file -- stale "
                    "once its file is windowed, so dropped rather than left misleading",
    "unsupported_format": "not parquet/sqlite/json/jsonl -- unknown shape, not included",
}
# Tables that only hold runtime state -- never part of a bt/ga results export.
RUNTIME_TABLES = ("task_queue", "activitylog", "persistedqueuetask", "llmusagelog")
DB_ARCNAME = "db/dl_forecasting.db"
CACHE_ARC_PREFIX = "cache/"
MANIFEST_ARCNAME = "manifest.json"


def _default_db_path() -> str:
    return os.path.join(TEST_DIR, "dl_forecasting.db")


def _human(nbytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if nbytes < 1024:
            return f"{nbytes:.1f}{unit}"
        nbytes /= 1024
    return f"{nbytes:.1f}PB"


def _safe_sqlite_copy(src_path: str, dst_path: str) -> None:
    """Copy a (possibly live, WAL-mode) SQLite DB via the backup API -- safe under concurrent
    writers, unlike a raw file copy which can grab a torn mid-write snapshot."""
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(dst_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _robocopy_move(src_path: str, dest_dir: str, dest_name: str) -> None:
    """Move a large file onto a possibly-flaky mount (cloud-sync drives, etc.) via robocopy.

    2026-08-24 INCIDENT: a 32GB export completed cleanly, then shutil.move's fallback
    copy2->open() failed with OSError [Errno 22] ("Ressources systeme insuffisantes" /
    ERROR_NO_SYSTEM_RESOURCES / WinError 1450) writing to a Google-Drive-streamed mount under
    local memory pressure from a concurrently-running grid. That is a KERNEL-level resource
    transient, not a size or permissions problem -- the SAME single-shot open() will often just
    fail again a second later. robocopy is the Windows-native tool built for exactly this: it
    retries a failed copy on its own (/R retries, /W wait-seconds-between), rather than a Python
    file handle raising once and giving up.

    Raises RuntimeError with robocopy's own exit code on failure (robocopy's return codes are
    NOT unix-style -- 0-7 all mean some degree of success, only 8+ is a real failure).
    """
    args = [
        "robocopy", os.path.dirname(src_path), dest_dir, os.path.basename(src_path),
        "/R:8", "/W:20",      # retry up to 8 times, 20s apart -- rides out a transient resource dip
        "/NP", "/NFL", "/NDL",  # quiet: no per-file/per-dir noise for a single multi-GB file
    ]
    # capture as bytes + decode with errors="replace": robocopy writes the console's OEM
    # codepage (e.g. cp850 on a French locale), not Python's default text encoding -- decoding
    # as text=True raised UnicodeDecodeError in the subprocess reader thread during testing.
    result = subprocess.run(args, capture_output=True)
    stdout = result.stdout.decode("utf-8", errors="replace")
    stderr = result.stderr.decode("utf-8", errors="replace")
    if result.returncode >= 8:
        raise RuntimeError(f"robocopy failed (exit {result.returncode}): {stdout[-2000:]}\n{stderr[-2000:]}")
    copied = os.path.join(dest_dir, os.path.basename(src_path))
    if not os.path.exists(copied):
        raise RuntimeError(f"robocopy reported success (exit {result.returncode}) but "
                           f"{copied} does not exist")
    if os.path.getsize(copied) != os.path.getsize(src_path):
        raise RuntimeError(
            f"robocopy reported success (exit {result.returncode}) but sizes differ: "
            f"src={os.path.getsize(src_path)} dst={os.path.getsize(copied)}")
    if copied != os.path.join(dest_dir, dest_name):
        os.replace(copied, os.path.join(dest_dir, dest_name))
    os.remove(src_path)


def _iter_cache_files(cache_dir: str):
    for root, _dirs, files in os.walk(cache_dir):
        for fname in files:
            # Every cache writer in this codebase (parquet_store.py, metric_store.py,
            # fmp_common.py) stages via "<path>.<pid>.<tid>.tmp" then os.replace()'s it
            # into place -- so a ".tmp" file on disk is a write STILL IN PROGRESS, never
            # a finished cache artifact. Reading one produces truncated/partial content
            # at best (2026-08-28: reading one that finished writing between the walk and
            # the read raced os.replace() and the file vanished mid-export -- see the
            # FileNotFoundError guards below, which are the belt to this braces).
            if fname.endswith(".tmp"):
                continue
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, cache_dir).replace(os.sep, "/")
            yield full, CACHE_ARC_PREFIX + rel


def _filter_db(db_file: str, scope: str, labels: list) -> dict:
    """Trim a full DB snapshot (in place) down to backtest/GA-job content.

    bt-ga: keep all backtests + all strategy_optimizations (GA jobs), plus the strategies
           they reference and their robustness runs.
    bt:    keep backtests (optionally filtered by labels substring on backtests.labels),
           drop all GA jobs; backtests.optimization_id is set to NULL in the exported copy.
    Runtime-only tables are cleared in both scopes, then the file is VACUUMed.
    Returns kept-row stats for the manifest."""
    con = sqlite3.connect(db_file)
    try:
        cur = con.cursor()
        if labels:
            clause = " OR ".join(["labels LIKE ?"] * len(labels))
            params = [f"%{lbl}%" for lbl in labels]
            keep_ids = [r[0] for r in cur.execute(
                f"SELECT id FROM backtests WHERE {clause}", params)]
        else:
            keep_ids = [r[0] for r in cur.execute("SELECT id FROM backtests")]
        if not keep_ids:
            raise SystemExit(f"Label filter {labels} matched 0 backtests -- nothing to export.")

        cur.execute("CREATE TEMP TABLE _keep_bt (id INTEGER PRIMARY KEY)")
        cur.executemany("INSERT INTO _keep_bt (id) VALUES (?)", [(i,) for i in keep_ids])
        cur.execute("CREATE TEMP TABLE _keep_strat (id INTEGER PRIMARY KEY)")
        cur.execute(
            "INSERT OR IGNORE INTO _keep_strat (id) "
            "SELECT DISTINCT strategy_id FROM backtests "
            "WHERE strategy_id IS NOT NULL AND id IN (SELECT id FROM _keep_bt)")
        if scope == "bt-ga":
            cur.execute(
                "INSERT OR IGNORE INTO _keep_strat (id) "
                "SELECT DISTINCT strategy_id FROM strategy_optimizations "
                "WHERE strategy_id IS NOT NULL")

        cur.execute("DELETE FROM robustness_runs "
                    "WHERE backtest_id NOT IN (SELECT id FROM _keep_bt)")
        if scope == "bt":
            cur.execute("UPDATE backtests SET optimization_id = NULL "
                        "WHERE optimization_id IS NOT NULL")
            cur.execute("DELETE FROM strategy_optimizations")
        cur.execute("DELETE FROM backtests WHERE id NOT IN (SELECT id FROM _keep_bt)")
        cur.execute("DELETE FROM strategies WHERE id NOT IN (SELECT id FROM _keep_strat)")
        for table in RUNTIME_TABLES:
            cur.execute(f"DELETE FROM {table}")
        con.commit()

        stats = {
            "backtests_kept": cur.execute("SELECT COUNT(*) FROM backtests").fetchone()[0],
            "ga_jobs_kept": cur.execute("SELECT COUNT(*) FROM strategy_optimizations").fetchone()[0],
            "strategies_kept": cur.execute("SELECT COUNT(*) FROM strategies").fetchone()[0],
            "robustness_runs_kept": cur.execute("SELECT COUNT(*) FROM robustness_runs").fetchone()[0],
        }
        cur.execute("VACUUM")
        return stats
    finally:
        con.close()


def _parse_date_arg(name: str, s: str) -> "pd.Timestamp":
    try:
        return pd.Timestamp(s)
    except (ValueError, TypeError) as e:
        raise SystemExit(f"--{name} {s!r} is not a valid date (expected YYYY-MM-DD): {e}")


def _naive(ts: "pd.Series") -> "pd.Series":
    """Strip tz so a comparison against the (naive) window bounds never raises."""
    try:
        if ts.dt.tz is not None:
            return ts.dt.tz_localize(None)
    except (AttributeError, TypeError):
        pass
    return ts


def _record_date(d: dict, known_keys) -> "pd.Timestamp | None":
    """The first parseable date this record carries, per ``known_keys`` in priority order --
    or, when ``known_keys`` is empty (prefix not in FMP_HISTORY_DATE_KEYS), any key whose
    NAME contains "date". Returns None when nothing usable was found (record kept OUT of a
    window decision, not assumed in-range)."""
    keys = known_keys or [k for k in d if isinstance(k, str) and "date" in k.lower()]
    for k in keys:
        v = d.get(k)
        if not v:
            continue
        ts = pd.to_datetime(v, errors="coerce")
        if pd.isna(ts):
            continue
        if ts.tzinfo is not None:
            ts = ts.tz_localize(None)
        return ts
    return None


def _filter_parquet_bytes(full_path: str, start: "pd.Timestamp", end_excl: "pd.Timestamp"):
    df = pd.read_parquet(full_path)
    date_col = next((c for c in DATE_COLUMN_CANDIDATES if c in df.columns), None)
    if date_col is None:
        return None, "no_date_field"
    dt = _naive(pd.to_datetime(df[date_col], errors="coerce"))
    filtered = df.loc[(dt >= start) & (dt < end_excl)]
    if filtered.empty:
        return None, "empty"
    buf = io.BytesIO()
    filtered.to_parquet(buf, engine="pyarrow")
    return buf.getvalue(), "kept"


def _filter_sqlite_bytes(full_path: str, start: "pd.Timestamp", end_excl: "pd.Timestamp"):
    """Copy every table, filtered by whichever DATE_COLUMN_CANDIDATES column it has (the
    legacy options cache's option_chain.as_of / option_bar.date); a table with none of
    those columns is copied WHOLE -- there is exactly one sqlite file under CACHE_FOLDER
    today and both its tables have one, so this is a defensive fallback, not the common
    case."""
    uri = "file:" + urllib.parse.quote(full_path) + "?mode=ro&immutable=1"
    src = sqlite3.connect(uri, uri=True)
    tmp_path = f"{full_path}.datewindow-{os.getpid()}.tmp"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    try:
        tables = [r[0] for r in src.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        dst = sqlite3.connect(tmp_path)
        kept_any = False
        try:
            start_s, end_s = start.strftime("%Y-%m-%d"), end_excl.strftime("%Y-%m-%d")
            for table in tables:
                create_sql = src.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (table,)).fetchone()[0]
                dst.execute(create_sql)
                cols = [r[1] for r in src.execute(f"PRAGMA table_info({table})")]
                date_col = next((c for c in DATE_COLUMN_CANDIDATES if c in cols), None)
                if date_col:
                    rows = src.execute(
                        f"SELECT * FROM {table} WHERE {date_col} >= ? AND {date_col} < ?",
                        (start_s, end_s)).fetchall()
                else:
                    rows = src.execute(f"SELECT * FROM {table}").fetchall()
                if rows:
                    kept_any = True
                    placeholders = ",".join("?" * len(cols))
                    dst.executemany(f"INSERT INTO {table} VALUES ({placeholders})", rows)
            dst.commit()
        finally:
            dst.close()
        if not kept_any:
            return None, "empty"
        with open(tmp_path, "rb") as f:
            return f.read(), "kept"
    finally:
        src.close()
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def _filter_json_bytes(full_path: str, start: "pd.Timestamp", end_excl: "pd.Timestamp"):
    with open(full_path, encoding="utf-8") as f:
        data = json.load(f)

    prefix = os.path.basename(full_path).split("__", 1)[0]
    known_keys = FMP_HISTORY_DATE_KEYS.get(prefix, ())

    def _in_range(d):
        ts = _record_date(d, known_keys)
        return ts is not None and start <= ts < end_excl

    if isinstance(data, list):
        if not data:
            return None, "empty"
        if not isinstance(data[0], dict):
            return None, "unsupported_format"
        if all(_record_date(d, known_keys) is None for d in data[:5]):
            return None, "no_date_field"
        kept = [d for d in data if _in_range(d)]
        if not kept:
            return None, "empty"
        return json.dumps(kept).encode("utf-8"), "kept"

    if isinstance(data, dict):
        # e.g. fred/*.json: {"series_id": ..., "observations": [{"date": ..., ...}, ...]}
        list_keys = [k for k, v in data.items()
                    if isinstance(v, list) and v and isinstance(v[0], dict)]
        for lk in list_keys:
            items = data[lk]
            if all(_record_date(d, known_keys) is None for d in items[:5]):
                continue
            kept_items = [d for d in items if _in_range(d)]
            if not kept_items:
                return None, "empty"
            new_data = dict(data)
            new_data[lk] = kept_items
            return json.dumps(new_data).encode("utf-8"), "kept"
        return None, "no_date_field"

    return None, "unsupported_format"


def _filter_jsonl_bytes(full_path: str, start: "pd.Timestamp", end_excl: "pd.Timestamp"):
    kept_lines = []
    any_date_seen = False
    with open(full_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(d, dict):
                continue
            ts = _record_date(d, ())
            if ts is None:
                continue
            any_date_seen = True
            if start <= ts < end_excl:
                kept_lines.append(line)
    if not any_date_seen:
        return None, "no_date_field"
    if not kept_lines:
        return None, "empty"
    return ("\n".join(kept_lines) + "\n").encode("utf-8"), "kept"


def _filter_cache_file(full_path: str, start: "pd.Timestamp", end_excl: "pd.Timestamp"):
    """``(bytes, reason)`` -- bytes is None (drop, do not write) with reason naming why
    whenever the file's content couldn't be proven inside the window; see
    FILTER_DROP_REASONS for what each reason means."""
    lower = full_path.lower()
    if lower.endswith(".meta.json"):
        return None, "meta_sidecar"
    if lower.endswith(".sqlite"):
        return _filter_sqlite_bytes(full_path, start, end_excl)
    if lower.endswith(".parquet"):
        return _filter_parquet_bytes(full_path, start, end_excl)
    if lower.endswith(".jsonl"):
        return _filter_jsonl_bytes(full_path, start, end_excl)
    if lower.endswith(".json"):
        return _filter_json_bytes(full_path, start, end_excl)
    return None, "unsupported_format"


def cmd_export(args: argparse.Namespace) -> None:
    scope = args.scope
    if args.label and scope != "bt":
        raise SystemExit("--label is only valid with --scope bt.")
    if bool(args.date_start) != bool(args.date_end):
        raise SystemExit("--date-start and --date-end must be given together.")
    date_window = None
    if args.date_start:
        if scope not in ("full", "cache"):
            raise SystemExit("--date-start/--date-end only apply with --scope full or cache "
                             "(a date window is a CACHE-content filter; the DB scopes filter "
                             "by backtest label instead).")
        start = _parse_date_arg("date-start", args.date_start)
        end_excl = _parse_date_arg("date-end", args.date_end) + pd.Timedelta(days=1)
        if end_excl <= start:
            raise SystemExit(f"--date-end ({args.date_end}) is not after --date-start "
                             f"({args.date_start}).")
        date_window = (start, end_excl)

    include_cache = scope in ("full", "cache")
    need_db = scope != "cache"
    if include_cache and not os.path.isdir(CACHE_FOLDER):
        raise SystemExit(f"Cache folder not found: {CACHE_FOLDER}")
    db_path = _default_db_path()
    if need_db and not os.path.isfile(db_path):
        raise SystemExit(f"Results DB not found: {db_path}")

    name = args.name or DEFAULT_NAMES[scope]
    os.makedirs(args.dest_dir, exist_ok=True)
    final_path = os.path.join(args.dest_dir, name)
    if os.path.exists(final_path) and not args.overwrite:
        raise SystemExit(f"{final_path} already exists. Pass --overwrite to replace it.")

    compression = zipfile.ZIP_DEFLATED if args.compress else zipfile.ZIP_STORED
    # Most of the cache is already-compressed parquet -- DEFLATE barely shrinks it and just
    # burns CPU over tens of GB, so STORED (no recompression) is the default.

    tmp_dir = tempfile.mkdtemp(prefix="ba2_ml_cache_export_")
    tmp_zip = os.path.join(tmp_dir, "export.zip.partial")
    tmp_db = os.path.join(tmp_dir, "dl_forecasting.db")
    t0 = time.monotonic()
    db_size = 0
    try:
        manifest = {
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "scope": scope,
            "label_filters": args.label or [],
            "compression": "deflate" if args.compress else "stored",
        }
        if date_window:
            manifest["date_start"] = args.date_start
            manifest["date_end"] = args.date_end

        if need_db:
            print(f"Snapshotting {db_path} via sqlite backup API...")
            _safe_sqlite_copy(db_path, tmp_db)
            manifest["source_db_path"] = db_path
            if scope in ("bt", "bt-ga"):
                print(f"Filtering DB snapshot (scope={scope})...")
                stats = _filter_db(tmp_db, scope, args.label or [])
                manifest.update(stats)
                print(f"  kept: {stats['backtests_kept']} backtests, {stats['ga_jobs_kept']} GA jobs, "
                      f"{stats['strategies_kept']} strategies, {stats['robustness_runs_kept']} robustness runs")
            db_size = os.path.getsize(tmp_db)
            manifest["db_bytes"] = db_size

        cache_files = []
        filter_stats = {}
        cache_kept_count = 0
        cache_kept_bytes = 0
        if include_cache:
            print(f"Scanning {CACHE_FOLDER} ...")
            cache_files = list(_iter_cache_files(CACHE_FOLDER))
            # A live cache is a moving target -- this export can (and, running alongside the
            # GA grid, routinely does) race a writer's own atomic write-then-replace on a
            # file the walk above already listed. Vanishing between the walk and this SIZE
            # SUM is cosmetic (only the printed/manifest total is affected); the same race at
            # the actual READ, in the write loop below, is handled per-file there instead of
            # aborting the whole export over one file's timing.
            cache_scan_size = 0
            for f, _arc in cache_files:
                try:
                    cache_scan_size += os.path.getsize(f)
                except OSError:
                    pass
            print(f"  {len(cache_files):,} candidate files, {_human(cache_scan_size)}"
                  + (" (pre-filter)" if date_window else ""))
            manifest["source_cache_dir"] = CACHE_FOLDER
            manifest["cache_candidate_file_count"] = len(cache_files)
            manifest["cache_candidate_bytes"] = cache_scan_size

        print(f"Writing {tmp_zip} ...")
        with zipfile.ZipFile(tmp_zip, "w", compression=compression, allowZip64=True) as zf:
            if need_db:
                zf.write(tmp_db, DB_ARCNAME)
            done = 0
            next_report = 2000
            vanished = 0
            for full, arcname in cache_files:
                # Same live-cache race as the size scan above, at the point that actually
                # matters: a file the walk saw can be gone (replaced by its own writer's
                # os.replace(), or cleared) by the time this loop reaches it. One file
                # losing that race is not a reason to abort an export that may already be
                # an hour in -- it is dropped like any other unreadable file, and counted
                # separately from the FILTER_DROP_REASONS (those are about CONTENT, this is
                # about the file no longer existing at all).
                try:
                    if date_window:
                        start, end_excl = date_window
                        data, reason = _filter_cache_file(full, start, end_excl)
                        filter_stats[reason] = filter_stats.get(reason, 0) + 1
                        if data is not None:
                            zf.writestr(arcname, data)
                            cache_kept_count += 1
                            cache_kept_bytes += len(data)
                    else:
                        zf.write(full, arcname)
                        cache_kept_count += 1
                        cache_kept_bytes += os.path.getsize(full)
                except (FileNotFoundError, PermissionError):
                    vanished += 1
                done += 1
                if done >= next_report:
                    print(f"  ...{done:,}/{len(cache_files):,} files scanned")
                    next_report += 2000
            manifest["cache_file_count"] = cache_kept_count
            manifest["cache_bytes"] = cache_kept_bytes
            manifest["cache_vanished_during_export"] = vanished
            if date_window:
                manifest["cache_filter"] = filter_stats
            zf.writestr(MANIFEST_ARCNAME, json.dumps(manifest, indent=2))

        zip_size = os.path.getsize(tmp_zip)
        print(f"Moving to {final_path} ...")
        try:
            # Fast path: same-drive or a mount that behaves like a normal filesystem.
            shutil.move(tmp_zip, final_path)
        except OSError as e:
            # Cross-device (WinError 17) or a resource transient (WinError 1450 / errno 22,
            # seen writing to a Google-Drive-streamed mount under memory pressure) -- fall back
            # to robocopy, which retries on its own instead of failing once and giving up.
            print(f"  shutil.move failed ({e!r}); retrying via robocopy (auto-retries on "
                 f"transient failures)...")
            _robocopy_move(tmp_zip, args.dest_dir, name)
        moved = True
    except BaseException:
        moved = False
        raise
    finally:
        # NEVER discard a successfully-built zip just because the MOVE failed -- that is what
        # threw away 32GB / hours of work on 2026-08-24. Only clean up the temp dir once the
        # file has actually landed at final_path.
        if moved or not os.path.exists(tmp_zip):
            shutil.rmtree(tmp_dir, ignore_errors=True)
        else:
            print(f"\nMOVE FAILED -- the completed export was NOT deleted, it is still at:\n"
                 f"  {tmp_zip}\n"
                 f"Move it to {final_path} by hand, or re-run this command (robocopy resumes "
                 f"a partial transfer rather than re-copying from scratch).")

    elapsed = time.monotonic() - t0
    print(f"Done in {elapsed:.0f}s: {final_path} ({_human(zip_size)})")
    if include_cache:
        print(f"  cache: {cache_kept_count:,} files, {_human(cache_kept_bytes)}"
              + (f" (of {len(cache_files):,} candidates)" if date_window else ""))
        if vanished:
            print(f"  {vanished:,} file(s) vanished between the scan and the write (a live "
                 f"cache writer's own atomic replace/clear won the race) -- skipped, not "
                 f"an error.")
        if date_window:
            print("  cache filter breakdown:")
            for reason, count in sorted(filter_stats.items(), key=lambda kv: -kv[1]):
                note = FILTER_DROP_REASONS.get(reason, "")
                print(f"    {reason:20s} {count:>7,}" + (f"  -- {note}" if reason != "kept" else ""))
    if need_db:
        print(f"  db:    {_human(db_size)}")


def cmd_import(args: argparse.Namespace) -> None:
    if not os.path.isfile(args.archive):
        raise SystemExit(f"Archive not found: {args.archive}")

    cache_dir = args.cache_dir or CACHE_FOLDER
    db_path = args.db_path or _default_db_path()

    with zipfile.ZipFile(args.archive, "r") as zf:
        try:
            manifest = json.loads(zf.read(MANIFEST_ARCNAME))
            print("Archive manifest:")
            for k, v in manifest.items():
                print(f"  {k}: {v}")
        except KeyError:
            manifest = None
            print("(no manifest.json in this archive -- proceeding without it)")

        names = zf.namelist()
        has_db = DB_ARCNAME in names
        members = [m for m in names if m.startswith(CACHE_ARC_PREFIX)]
        if not has_db and not members:
            raise SystemExit("Archive contains neither a DB nor any cache files -- nothing to restore.")
        if manifest and manifest.get("scope") not in (None, "full"):
            extra = []
            if manifest.get("label_filters"):
                extra.append(f"labels={manifest['label_filters']}")
            if manifest.get("date_start"):
                extra.append(f"window={manifest['date_start']}..{manifest['date_end']}")
            print(f"WARNING: this archive is a PARTIAL export (scope={manifest['scope']}"
                  + (", " + ", ".join(extra) if extra else "")
                  + (". Importing it REPLACES the entire DB." if has_db else
                     " -- no DB in this archive, cache only."))

        if has_db:
            # Refuse to clobber a DB that looks like it's currently attached to a running
            # server (WAL/SHM sidecars present) unless the caller explicitly forces it.
            wal_sidecars = [db_path + suffix for suffix in ("-wal", "-shm")
                            if os.path.exists(db_path + suffix)]
            if wal_sidecars and not args.force:
                raise SystemExit(
                    f"{db_path} has active WAL sidecars ({', '.join(wal_sidecars)}) -- looks like a "
                    "server is running against it. Stop it first, or pass --force to override.")

        if not args.overwrite:
            conflicts = []
            if has_db and os.path.exists(db_path):
                conflicts.append(db_path)
            if members and os.path.isdir(cache_dir) and os.listdir(cache_dir):
                conflicts.append(cache_dir + " (non-empty)")
            if conflicts:
                raise SystemExit(
                    "Refusing to import over existing data (pass --overwrite to proceed): "
                    + "; ".join(conflicts))

        if has_db:
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            print(f"Restoring DB -> {db_path}")
            with zf.open(DB_ARCNAME) as src, open(db_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
        else:
            print("No DB in this archive -- skipping DB restore.")

        if members:
            os.makedirs(cache_dir, exist_ok=True)
            print(f"Restoring cache -> {cache_dir}")
            for i, member in enumerate(members, 1):
                rel = member[len(CACHE_ARC_PREFIX):]
                if not rel:
                    continue
                target = os.path.join(cache_dir, *rel.split("/"))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(member) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                if i % 2000 == 0:
                    print(f"  ...{i:,}/{len(members):,} files")
        else:
            print("No cache in this archive -- skipping cache restore.")

    print(f"Done: restored {len(members):,} cache files"
          + (" + DB." if has_db else " (no DB in archive)."))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_export = sub.add_parser("export", help="Archive the provider cache + results DB into one zip.")
    p_export.add_argument("--dest-dir", required=True, help="Directory to write the zip into.")
    p_export.add_argument("--scope", choices=("full", "cache", "db", "bt-ga", "bt"), default="full",
                          help="What to export: full = cache + complete DB (default); "
                               "cache = cache only, no DB; db = complete DB only; "
                               "bt-ga = DB trimmed to all backtests + GA jobs; "
                               "bt = DB trimmed to backtests only.")
    p_export.add_argument("--label", action="append", default=None, metavar="SUBSTR",
                          help="With --scope bt: keep only backtests whose labels contain SUBSTR "
                               "(repeatable, OR-combined, case-insensitive).")
    p_export.add_argument("--date-start", default=None, metavar="YYYY-MM-DD",
                          help="With --scope full/cache: window the CACHE CONTENT to this date "
                               "range (inclusive), file by file -- not a directory-level pick. "
                               "Requires --date-end. See the module docstring for what happens "
                               "to a file whose format/date field can't be determined.")
    p_export.add_argument("--date-end", default=None, metavar="YYYY-MM-DD",
                          help="End of the window (inclusive). Requires --date-start.")
    p_export.add_argument("--name", default=None,
                          help=f"Zip filename (default per scope: "
                               f"{' / '.join(DEFAULT_NAMES[s] for s in ('full', 'cache', 'db', 'bt-ga', 'bt'))}).")
    p_export.add_argument("--overwrite", action="store_true",
                          help="Replace the destination zip if it already exists.")
    p_export.add_argument("--compress", action="store_true",
                          help="Use DEFLATE instead of STORED (slower, saves little on parquet).")
    p_export.set_defaults(func=cmd_export)

    p_import = sub.add_parser("import", help="Restore a cache+DB export back onto this machine.")
    p_import.add_argument("--archive", required=True, help="Path to the export zip.")
    p_import.add_argument("--cache-dir", default=None,
                          help=f"Restore target for the cache (default: {CACHE_FOLDER}).")
    p_import.add_argument("--db-path", default=None,
                          help=f"Restore target for the DB (default: {_default_db_path()}).")
    p_import.add_argument("--overwrite", action="store_true",
                          help="Proceed even if the target DB/cache already has content.")
    p_import.add_argument("--force", action="store_true",
                          help="Proceed even if the target DB looks like it's live (WAL sidecars present).")
    p_import.set_defaults(func=cmd_import)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
