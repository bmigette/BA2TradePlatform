r"""Repair daily OHLCV caches contaminated by PARTIAL (forming) bars, 2026-09-14 .. 2026-10-07.

THE DEFECT. From 2026-09-14 the dev app's 09:31 New York top-up wrote the vendor's still-forming bar of
the day into the SHARED daily parquet cache (one tick, O == H and L == C); the tail guard then froze the
file, so the partial bar became "history" for every backtest. 312 bars in 273 symbols were measured (a
lower bound); 259 are the newest bar of their file and 53 are interior bars older than the newest.
The prevention (``ba2_common.core.ohlcv_final_bars``, enforced in ``native_cache.write_timeseries``)
stops new ones. This tool removes the existing ones the only way that also catches interior bars:

    1. BACKUP every file it will touch (and its split-basis marker) to ``--backup-dir``, OUTSIDE the
       cache, and verify every copy (size + sha256) BEFORE anything is modified;
    2. TRUNCATE each affected ``<SYM>_1d.parquet`` to the bars dated <= ``--cutoff`` (schema, dtypes,
       compression and the values of the kept rows are preserved; the kept rows are asserted equal to
       the backup's, and a failed assertion restores the file from the backup);
    3. leave the vendor's FINAL bars to be re-appended by the normal guarded top-up (``ba2-test
       fetch-cache --timeframes 1d --end <last final session>``), which compares the last cached bars
       with the vendor's before appending.

DRY RUN BY DEFAULT: nothing is written (not even a backup) unless ``--apply``. ``--apply`` additionally
needs ``--writers`` (see below) and a passing self-test that the guarded writer is the code in use.

SELECTION. Candidates are the union of ``--symbols-file`` (one symbol per line) and every daily file whose
mtime is on/after ``--modified-since``. ``--select`` is REQUIRED:
    flagged         truncate a candidate only if a bar dated > cutoff is flagged as partial
                    (volume < 5% of the prior-20-session median for a liquid name, or a newest bar the
                    file's mtime proves was written inside its own trading session);
    all-after-cutoff truncate every candidate that holds any bar dated > cutoff (the runbook's choice:
                    simple, and the guarded top-up re-appends final bars; costs one re-fetch per file).
``--cutoff`` is REQUIRED (no default): it must be a session before the first live top-up that wrote into
the cache. Measured on the dev cache (m6/m7 of the 2026-10-07 refresh): earliest flagged partial bar
2026-09-14, no file has an mtime between 2026-09-01 and 2026-09-13, flagged-bar rate on 08-10..09-11 =
0.058 % (the false-positive baseline) -> 2026-09-11 is the last clean session.

``--writers`` (REQUIRED with --apply) records what you know about OTHER processes that can write these
files while the repair and the re-extension run. The in-process self-test cannot see them: a running
app on pre-fix code re-creates a partial bar in every file it tops up near 09:31 NY (a file written
today is not topped up again for 24 h).
    stopped          every writer (apps, ba2-test, the research skill) is stopped
    guarded          every writer that can run is already on the prevention code
    old-code-running pre-fix writers may run: the exposure is stated in the report; run the completion
                     check (tools/check_daily_extension.py) after their next 09:31 NY top-up

REPORT. Per file: bars removed, new last bar, flagged bars with reasons, membership in ``--store-universe``,
and a machine-readable JSON (``--report-json``) for a completion check; ``--symbols-out`` writes the
truncated symbols one per line (the symbols file for the re-extension).

    python tools/repair_partial_daily_bars.py --cache-dir <cache>\FMPOHLCVProvider --cutoff 2026-09-11 \
        --backup-dir D:\backups\refresh --modified-since 2026-09-01 --select all-after-cutoff \
        --report-json report.json                     # dry run
    ... --apply --writers old-code-running            # the real thing
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import glob
import hashlib
import json
import os
import shutil
import sys
from typing import Dict, List, Optional, Sequence, Tuple

SUFFIX = "_1d.parquet"
MARKER_DIRNAME = "_split_basis"          # == ba2_common.core.split_basis.MARKER_DIRNAME (checked by the test)
LOW_VOLUME_RATIO = 0.05
LIQUID_MEDIAN_VOLUME = 50_000
WRITERS_CHOICES = ("stopped", "guarded", "old-code-running")


class RepairRefused(RuntimeError):
    """A precondition failed; nothing was written."""


# --------------------------------------------------------------------------- helpers
def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_within(path: str, folder: str) -> bool:
    path, folder = os.path.normcase(os.path.realpath(path)), os.path.normcase(os.path.realpath(folder))
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def check_backup_location(backup_dir: str, cache_dir: str) -> None:
    """The backup must not live in the cache (``cache_sync`` would treat it as cache) nor contain it.
    ``cache_dir`` is the provider folder; the cache ROOT (its parent, which also holds the metric store,
    fmp_history, ...) is excluded too."""
    root = os.path.dirname(os.path.realpath(cache_dir))
    for folder, what in ((cache_dir, "the cache folder"), (root, "the cache root")):
        if _is_within(backup_dir, folder):
            raise RepairRefused(f"--backup-dir {backup_dir} is inside {what} {folder}: a backup there would be "
                                f"synced and read as cache. Choose a folder outside the cache.")
    if _is_within(cache_dir, backup_dir):
        raise RepairRefused(f"--backup-dir {backup_dir} contains the cache folder {cache_dir}")


def read_symbols(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8") as f:
        return sorted({tok.upper() for line in f for tok in line.split() if tok and not tok.startswith("#")})


def guarded_writer_self_test() -> None:
    """Prove, in THIS process, that the OHLCV writer refuses to persist an unfinished bar: write a
    frame holding a bar dated tomorrow through ``native_cache.write_timeseries`` into a temp cache and
    read it back. Raises ``RepairRefused`` when the bar reached the disk (pre-fix code) or the module is
    missing."""
    import tempfile
    try:
        import pandas as pd
        from ba2_common.core import native_cache, ohlcv_final_bars
    except ImportError as e:
        raise RepairRefused(f"the guarded writer (ba2_common.core.ohlcv_final_bars) cannot be imported: {e}") from e
    if getattr(native_cache, "ohlcv_final_bars", None) is not ohlcv_final_bars:
        raise RepairRefused("native_cache.write_timeseries does not use ohlcv_final_bars: pre-fix code is running")
    tomorrow = dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=3)
    frame = pd.DataFrame({"Date": pd.to_datetime(["2020-01-02", tomorrow.isoformat()]),
                          "Open": 1.0, "High": 2.0, "Low": 0.5, "Close": 1.5, "Volume": 10})
    frame["effective_date"] = frame["Date"]
    with tempfile.TemporaryDirectory(prefix="ba2_guard_selftest_") as tmp:
        old = native_cache.CACHE_FOLDER
        native_cache.CACHE_FOLDER = tmp
        try:
            native_cache.write_timeseries("SelfTest", "ZZZZ", "1d", frame)
            got = pd.read_parquet(native_cache.find_timeseries_path("SelfTest", "ZZZZ", "1d"))
        finally:
            native_cache.CACHE_FOLDER = old
    if len(got) != 1:
        raise RepairRefused(f"self-test FAILED: the writer persisted a bar dated {tomorrow} ({len(got)} rows on disk)")


# --------------------------------------------------------------------------- per-file analysis
def _day_labels(dates):
    import pandas as pd
    d = pd.to_datetime(dates)
    if getattr(d.dt, "tz", None) is not None:
        d = d.dt.tz_convert("UTC").dt.tz_localize(None)
    return d.dt.normalize()


def analyse_file(path: str, symbol: str, cutoff: dt.date) -> Dict:
    """Read-only facts about one file: rows, last bar, bars after the cutoff and the flagged ones."""
    import numpy as np
    import pandas as pd
    from ba2_common.core import ohlcv_final_bars
    df = pd.read_parquet(path, columns=["Date", "Open", "High", "Low", "Close", "Volume"])
    out: Dict = {"symbol": symbol, "path": path, "rows": int(len(df)), "bytes": os.path.getsize(path),
                 "mtime_utc": dt.datetime.fromtimestamp(os.path.getmtime(path), dt.timezone.utc).isoformat(),
                 "flagged": [], "rows_after_cutoff": 0, "last_bar": None}
    if df.empty:
        return out
    days = _day_labels(df["Date"])
    order = np.argsort(days.to_numpy(), kind="stable")
    df, days = df.iloc[order].reset_index(drop=True), days.iloc[order].reset_index(drop=True)
    out["last_bar"] = days.iloc[-1].date().isoformat()
    after = days > pd.Timestamp(cutoff)
    out["rows_after_cutoff"] = int(after.sum())
    if not after.any():
        return out
    vol = df["Volume"].astype(float)
    med = vol.shift(1).rolling(20, min_periods=10).median()
    ratio = vol / med
    for i in np.where(after.to_numpy())[0]:
        if med.iloc[i] == med.iloc[i] and med.iloc[i] >= LIQUID_MEDIAN_VOLUME and ratio.iloc[i] < LOW_VOLUME_RATIO:
            out["flagged"].append({"bar": days.iloc[i].date().isoformat(), "reason": "volume_below_5pct_of_prior_20_median",
                                   "volume": float(vol.iloc[i]), "median_volume": float(med.iloc[i])})
    newest = days.iloc[-1].date()
    written = dt.datetime.fromtimestamp(os.path.getmtime(path), dt.timezone.utc)
    if newest > cutoff and ohlcv_final_bars.written_before_final(symbol, newest, written):
        same = [f for f in out["flagged"] if f["bar"] == newest.isoformat()]
        if same:
            same[0]["also"] = "newest_bar_written_before_its_session_was_final"
        else:
            out["flagged"].append({"bar": newest.isoformat(), "reason": "newest_bar_written_before_its_session_was_final",
                                   "mtime_utc": written.isoformat()})
    return out


# --------------------------------------------------------------------------- truncation
def _kept_mask(table, cutoff: dt.date):
    import numpy as np
    import pandas as pd
    days = _day_labels(table.column("Date").to_pandas())
    return (days <= pd.Timestamp(cutoff)).to_numpy(dtype=bool), days


def truncate_file(path: str, cutoff: dt.date, backup_path: str) -> Dict:
    """Rewrite ``path`` keeping the bars dated <= cutoff. Atomic (temp + replace); asserts the kept rows
    equal the backup's; restores the file from ``backup_path`` and raises when they do not."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    table = pq.read_table(path)
    mask, _days = _kept_mask(table, cutoff)
    keep = table.filter(pa.array(mask))
    compression = pq.ParquetFile(path).metadata.row_group(0).column(0).compression.lower()
    tmp = f"{path}.repair.{os.getpid()}.tmp"
    try:
        pq.write_table(keep, tmp, compression=compression)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    # the rows <= cutoff must be value-identical to the backup, schema included
    original = pq.read_table(backup_path)
    omask, _ = _kept_mask(original, cutoff)
    expected = original.filter(pa.array(omask))
    now = pq.read_table(path)
    if not (now.schema.equals(original.schema, check_metadata=False) and now.equals(expected)):
        shutil.copy2(backup_path, path)
        raise RuntimeError(f"{path}: kept rows differ from the backup after truncation; RESTORED from {backup_path}")
    return {"rows_before": original.num_rows, "rows_after": now.num_rows,
            "removed": original.num_rows - now.num_rows, "mask_kept": int(mask.sum())}


def update_marker(parquet_path: str, new_last: Optional[dt.date], rows: int) -> bool:
    """Keep the split-basis full-fetch marker truthful after a truncation: ``last_bar`` and ``rows`` follow
    the file; ``fetched_on_utc`` (what the split check reads: the history was fetched after the split) is
    left exactly as it was. Returns True when a marker existed and was rewritten."""
    folder, name = os.path.split(parquet_path)
    marker = os.path.join(folder, MARKER_DIRNAME, name[:-len(".parquet")] + ".json")
    if not os.path.exists(marker):
        return False
    with open(marker, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["last_bar"] = new_last.isoformat() if new_last else None
    data["rows"] = int(rows)
    tmp = f"{marker}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, sort_keys=True)
    os.replace(tmp, marker)
    return True


def _marker_path(parquet_path: str) -> str:
    folder, name = os.path.split(parquet_path)
    return os.path.join(folder, MARKER_DIRNAME, name[:-len(".parquet")] + ".json")


# --------------------------------------------------------------------------- main flow
def select_candidates(cache_dir: str, symbols: Optional[Sequence[str]], since: Optional[dt.date]) -> Tuple[List[Tuple[str, str]], List[str]]:
    """``([(symbol, path)], missing_symbols)``: the union of the symbols file and the files modified since."""
    found: Dict[str, str] = {}
    for p in sorted(glob.glob(os.path.join(cache_dir, "*" + SUFFIX))):
        found[os.path.basename(p)[:-len(SUFFIX)].upper()] = p
    picked: Dict[str, str] = {}
    missing: List[str] = []
    for s in (symbols or ()):
        if s in found:
            picked[s] = found[s]
        else:
            missing.append(s)
    if since is not None:
        for s, p in found.items():
            if dt.datetime.fromtimestamp(os.path.getmtime(p), dt.timezone.utc).date() >= since:
                picked[s] = p
    return sorted(picked.items()), missing


def run(args: argparse.Namespace) -> int:
    cache_dir = os.path.abspath(args.cache_dir)
    if not os.path.isdir(cache_dir):
        raise RepairRefused(f"--cache-dir {cache_dir} is not a directory")
    cutoff = dt.date.fromisoformat(args.cutoff)
    since = dt.date.fromisoformat(args.modified_since) if args.modified_since else None
    if not args.symbols_file and since is None:
        raise RepairRefused("select files with --symbols-file and/or --modified-since")
    backup_dir = os.path.abspath(args.backup_dir)
    check_backup_location(backup_dir, cache_dir)
    for out_path in (args.report_json, args.symbols_out):
        if out_path and _is_within(os.path.abspath(out_path), os.path.dirname(os.path.realpath(cache_dir))):
            raise RepairRefused(f"{out_path} is inside the cache root: write reports outside the cache")
    if args.apply:
        if not args.writers:
            raise RepairRefused(f"--apply needs --writers {{{','.join(WRITERS_CHOICES)}}}: say what else can write these "
                                f"files meanwhile (see --help)")
        guarded_writer_self_test()

    universe = set(read_symbols(args.store_universe)) if args.store_universe else None
    symbols = read_symbols(args.symbols_file) if args.symbols_file else None
    candidates, missing = select_candidates(cache_dir, symbols, since)

    rows: List[Dict] = []
    for symbol, path in candidates:
        info = analyse_file(path, symbol, cutoff)
        info["in_store_universe"] = None if universe is None else (symbol in universe)
        info["flagged_bars"] = [f["bar"] for f in info["flagged"]]
        wanted = info["rows_after_cutoff"] > 0 and (args.select == "all-after-cutoff" or bool(info["flagged"]))
        if wanted and args.restrict_to_universe and universe is not None and symbol not in universe:
            info["action"], wanted = "skipped:not_in_store_universe", False
        elif not info["rows_after_cutoff"]:
            info["action"] = "unchanged:no_bars_after_cutoff"
        elif not wanted:
            info["action"] = "unchanged:not_flagged"
        else:
            info["action"] = "truncate"
        info["bars_to_remove"] = info["rows_after_cutoff"] if wanted else 0
        info["new_last_bar"] = None
        rows.append(info)

    todo = [r for r in rows if r["action"] == "truncate"]
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_backup = os.path.join(backup_dir, f"repair-{stamp}")
    summary = {
        "tool": "repair_partial_daily_bars", "mode": "APPLY" if args.apply else "DRY_RUN",
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "cache_dir": cache_dir,
        "cutoff": cutoff.isoformat(), "select": args.select, "modified_since": args.modified_since,
        "writers": args.writers, "backup_dir": run_backup if args.apply else None,
        "symbols_file_missing_in_cache": missing, "candidates": len(rows),
        "files_to_truncate": len(todo), "bars_to_remove": sum(r["bars_to_remove"] for r in todo),
        "store_universe_given": universe is not None,
        "to_truncate_in_universe": sum(1 for r in todo if r["in_store_universe"]),
        "to_truncate_outside_universe": sum(1 for r in todo if r["in_store_universe"] is False),
        "files_flagged": sum(1 for r in rows if r["flagged"]),
        "truncated_symbols": [], "files": rows,
    }

    if args.apply and todo:
        # 1. back up EVERYTHING that will change, verify every copy, only then modify
        os.makedirs(run_backup, exist_ok=False)
        manifest = []
        for r in todo:
            rel = os.path.relpath(r["path"], cache_dir)
            dst = os.path.join(run_backup, "files", rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(r["path"], dst)
            copies = [(r["path"], dst)]
            marker = _marker_path(r["path"])
            if os.path.exists(marker):
                mdst = os.path.join(run_backup, "files", os.path.relpath(marker, cache_dir))
                os.makedirs(os.path.dirname(mdst), exist_ok=True)
                shutil.copy2(marker, mdst)
                copies.append((marker, mdst))
            for src, cp in copies:
                if os.path.getsize(src) != os.path.getsize(cp) or sha256_of(src) != sha256_of(cp):
                    raise RepairRefused(f"backup verification FAILED for {src} -> {cp}; nothing was modified")
            r["backup"] = dst
            r["sha256_before"] = sha256_of(r["path"])
            manifest.append({"symbol": r["symbol"], "rows": r["rows"], "last_bar": r["last_bar"],
                             "mtime_utc": r["mtime_utc"], "sha256": r["sha256_before"], "backup": dst})
        with open(os.path.join(run_backup, "manifest.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(manifest[0]))
            w.writeheader()
            w.writerows(manifest)
        summary["backup_verified_files"] = len(manifest)
        # 2. truncate
        for r in todo:
            res = truncate_file(r["path"], cutoff, r["backup"])
            after = analyse_file(r["path"], r["symbol"], cutoff)
            r["new_last_bar"] = after["last_bar"]
            r["bars_removed"] = res["removed"]
            r["rows_after"] = res["rows_after"]
            new_last = dt.date.fromisoformat(after["last_bar"]) if after["last_bar"] else None
            r["marker_updated"] = update_marker(r["path"], new_last, res["rows_after"])
            r["action"] = "truncated"
            summary["truncated_symbols"].append(r["symbol"])
        summary["bars_removed"] = sum(r["bars_removed"] for r in todo)
    else:
        # dry run (or nothing to do): the new last bar is the last bar <= cutoff
        for r in todo:
            r["new_last_bar"] = _last_bar_up_to(r["path"], cutoff)
            r["bars_removed"] = 0
    summary["truncated_symbols_planned"] = [r["symbol"] for r in todo]

    _print_report(summary)
    if args.report_json:
        os.makedirs(os.path.dirname(os.path.abspath(args.report_json)) or ".", exist_ok=True)
        with open(args.report_json, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, sort_keys=True)
    if args.symbols_out:
        syms = summary["truncated_symbols"] if args.apply else summary["truncated_symbols_planned"]
        with open(args.symbols_out, "w", encoding="utf-8") as f:
            f.write("\n".join(syms) + ("\n" if syms else ""))
    if args.apply and todo:
        with open(os.path.join(run_backup, "summary.json"), "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, sort_keys=True)
    return 0


def _last_bar_up_to(path: str, cutoff: dt.date) -> Optional[str]:
    import pandas as pd
    days = _day_labels(pd.read_parquet(path, columns=["Date"])["Date"])
    days = days[days <= pd.Timestamp(cutoff)]
    return None if days.empty else days.max().date().isoformat()


def _print_report(s: Dict) -> None:
    print(f"{s['mode']}: cache {s['cache_dir']} cutoff {s['cutoff']} select={s['select']}")
    print(f"  candidates {s['candidates']}, flagged files {s['files_flagged']}, files to truncate "
          f"{s['files_to_truncate']} ({s['bars_to_remove']} bars)"
          + (f", in store universe {s['to_truncate_in_universe']}, outside {s['to_truncate_outside_universe']}"
             if s["store_universe_given"] else ""))
    if s["symbols_file_missing_in_cache"]:
        print(f"  symbols without a daily file: {len(s['symbols_file_missing_in_cache'])}")
    for r in s["files"]:
        if r["action"] in ("truncate", "truncated") or r["flagged"]:
            print(f"  {r['symbol']:<8} {r['action']:<10} rows {r['rows']} -> removes {r['bars_to_remove'] or r.get('bars_removed', 0)}"
                  f" new last {r.get('new_last_bar')} flagged {r['flagged_bars']} universe {r['in_store_universe']}")
    if s["mode"] == "APPLY":
        print(f"  backup {s['backup_dir']}; truncated {len(s['truncated_symbols'])} file(s), "
              f"{s.get('bars_removed', 0)} bars removed")
    else:
        print("  dry run: nothing was written. Re-run with --apply --writers ... to repair.")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], epilog=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache-dir", required=True, help="the provider folder holding <SYM>_1d.parquet (e.g. <cache>\\FMPOHLCVProvider)")
    ap.add_argument("--cutoff", required=True, help="YYYY-MM-DD: keep bars dated <= this (REQUIRED, no default)")
    ap.add_argument("--backup-dir", required=True, help="folder OUTSIDE the cache for the verified backup")
    ap.add_argument("--select", required=True, choices=("flagged", "all-after-cutoff"))
    ap.add_argument("--symbols-file", help="one symbol per line")
    ap.add_argument("--modified-since", help="YYYY-MM-DD: every daily file modified on/after (UTC date)")
    ap.add_argument("--store-universe", help="the store universe symbol list (reported; see --restrict-to-universe)")
    ap.add_argument("--restrict-to-universe", action="store_true",
                    help="never truncate a symbol outside --store-universe (the re-extension would not cover it)")
    ap.add_argument("--report-json", help="machine-readable summary (outside the cache)")
    ap.add_argument("--symbols-out", help="write the truncated symbols (one per line) for the re-extension")
    ap.add_argument("--apply", action="store_true", help="modify the files (default: dry run)")
    ap.add_argument("--writers", choices=WRITERS_CHOICES, help="REQUIRED with --apply: what else can write these files")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except RepairRefused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
