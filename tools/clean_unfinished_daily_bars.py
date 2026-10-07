r"""Daily hygiene: remove the UNFINISHED newest daily bars from a cache (dry run by default).

Until every writer runs the prevention code (``ba2_common.core.ohlcv_final_bars``), an old-code app
re-creates a partial bar in each file it tops up near 09:31 New York. This tool removes them again, by
the FINALITY RULE and the file's own mtime, not by volume heuristics. A daily bar is removed when

  1. its session is NOT final per the NYSE calendar at the time of the check (session close + 4 h;
     half days; a non-NYSE symbol uses the calendar-free bound), or
  2. it is the file's NEWEST bar and the file mtime PROVES it was written inside that bar's own
     session (``written_before_final``): final by the calendar now, partial by content.

Nothing else is touched. Files are backed up (size + sha256 verified) to ``--backup-dir`` before any
change; each rewrite keeps schema/dtypes and the original mtime (so the old-code app's own 24 h
cadence is unchanged), updates the split-basis marker, and asserts the kept rows equal the backup's.
After it, re-extend the symbols it printed with the guarded top-up (``ba2-test fetch-cache
--timeframes 1d --symbols @<symbols-out> --start <a recent date> --end <last final session>``).

Cost: a footer read per file (~18.7k files: about 1-2 minutes) plus a full read of the few candidates;
0 FMP calls. Safe to run daily; best run after 20:05 ET (02:05 Paris) when the session is final.

    python tools/clean_unfinished_daily_bars.py --cache-dir <cache>\FMPOHLCVProvider --backup-dir D:\bk
    python tools/clean_unfinished_daily_bars.py --cache-dir ... --backup-dir D:\bk --apply --writers old-code-running \
        --symbols-out syms.txt --report-json clean.json

Prints: files scanned, candidates, bars to remove with their reason, and (--apply) the backup folder.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import glob
import json
import os
import shutil
import sys
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_daily_extension as chk          # noqa: E402
import repair_partial_daily_bars as rp       # noqa: E402

SUFFIX = rp.SUFFIX


def scan(cache_dir: str, now: dt.datetime) -> Dict:
    """Read-only: ``{"scanned", "candidates": [info]}``. One footer read per file."""
    from ba2_common.core import ohlcv_final_bars as fb
    candidates: List[Dict] = []
    scanned = 0
    for path in sorted(glob.glob(os.path.join(cache_dir, "*" + SUFFIX))):
        scanned += 1
        symbol = os.path.basename(path)[:-len(SUFFIX)].upper()
        last = chk.last_bar_of(path)
        if last is None:
            continue
        written = dt.datetime.fromtimestamp(os.path.getmtime(path), dt.timezone.utc)
        reasons = []
        if not fb.session_is_final(symbol, last, now):
            reasons.append("newest_session_not_final")
        elif fb.written_before_final(symbol, last, written):
            reasons.append("newest_bar_written_mid_session")
        if reasons:
            candidates.append({"symbol": symbol, "path": path, "last_bar": last.isoformat(),
                               "mtime_utc": written.isoformat(), "reasons": reasons})
    return {"scanned": scanned, "candidates": candidates}


def rows_to_drop(path: str, symbol: str, now: dt.datetime):
    """Boolean numpy mask over the file's rows (file order): True = remove."""
    import numpy as np
    import pandas as pd
    import pyarrow.parquet as pq
    from ba2_common.core import ohlcv_final_bars as fb
    dates = pq.read_table(path, columns=["Date"]).column("Date").to_pandas()
    drop = ~fb.final_mask(pd.DataFrame({"Date": dates}), symbol, "1d", now).to_numpy()
    labels = rp._day_labels(dates)
    newest = labels.max().date()
    written = dt.datetime.fromtimestamp(os.path.getmtime(path), dt.timezone.utc)
    if fb.written_before_final(symbol, newest, written):
        drop = drop | (labels == pd.Timestamp(newest)).to_numpy()
    return np.asarray(drop, dtype=bool), labels


def rewrite_without(path: str, drop, backup_path: str) -> Dict:
    """Atomic rewrite keeping the rows where ``drop`` is False; schema/compression preserved, original
    mtime restored; the kept rows are asserted equal to the backup's (restoring on failure)."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    st = os.stat(path)
    table = pq.read_table(path)
    keep = table.filter(pa.array(~drop))
    compression = pq.ParquetFile(path).metadata.row_group(0).column(0).compression.lower()
    tmp = f"{path}.clean.{os.getpid()}.tmp"
    try:
        pq.write_table(keep, tmp, compression=compression)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    original = pq.read_table(backup_path)
    now = pq.read_table(path)
    if not (now.schema.equals(original.schema, check_metadata=False)
            and now.equals(original.filter(pa.array(~drop)))):
        shutil.copy2(backup_path, path)
        raise RuntimeError(f"{path}: kept rows differ from the backup after the rewrite; RESTORED from {backup_path}")
    os.utime(path, (st.st_atime, st.st_mtime))
    return {"rows_before": original.num_rows, "rows_after": now.num_rows}


def run(args: argparse.Namespace) -> int:
    cache_dir = os.path.abspath(args.cache_dir)
    if not os.path.isdir(cache_dir):
        raise rp.RepairRefused(f"--cache-dir {cache_dir} is not a directory")
    backup_dir = os.path.abspath(args.backup_dir)
    rp.check_backup_location(backup_dir, cache_dir)
    root = os.path.dirname(os.path.realpath(cache_dir))
    for out in (args.report_json, args.symbols_out):
        if out and rp._is_within(os.path.abspath(out), root):
            raise rp.RepairRefused(f"{out} is inside the cache root: write reports outside the cache")
    if args.apply:
        if not args.writers:
            raise rp.RepairRefused(f"--apply needs --writers {{{','.join(rp.WRITERS_CHOICES)}}}")
        rp.guarded_writer_self_test()
    from ba2_common.core import ohlcv_final_bars as fb
    now = dt.datetime.fromisoformat(args.now).replace(tzinfo=dt.timezone.utc) if args.now else fb.now_utc()

    found = scan(cache_dir, now)
    todo = []
    for c in found["candidates"]:
        drop, labels = rows_to_drop(c["path"], c["symbol"], now)
        if not drop.any():
            continue
        c["bars_to_remove"] = [labels[i].date().isoformat() for i in drop.nonzero()[0]]
        c["_drop"] = drop
        todo.append(c)

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_backup = os.path.join(backup_dir, f"clean-{stamp}")
    summary = {"tool": "clean_unfinished_daily_bars", "mode": "APPLY" if args.apply else "DRY_RUN",
               "checked_at_utc": now.isoformat(), "cache_dir": cache_dir, "writers": args.writers,
               "files_scanned": found["scanned"], "candidates": len(found["candidates"]),
               "files_to_clean": len(todo), "bars_to_remove": sum(len(c["bars_to_remove"]) for c in todo),
               "backup_dir": run_backup if args.apply and todo else None, "cleaned_symbols": [], "files": []}

    if args.apply and todo:
        os.makedirs(run_backup, exist_ok=False)
        manifest = []
        for c in todo:                                           # back up and verify EVERYTHING first
            rel = os.path.relpath(c["path"], cache_dir)
            dst = os.path.join(run_backup, "files", rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            pairs = [(c["path"], dst)]
            shutil.copy2(c["path"], dst)
            marker = rp._marker_path(c["path"])
            if os.path.exists(marker):
                mdst = os.path.join(run_backup, "files", os.path.relpath(marker, cache_dir))
                os.makedirs(os.path.dirname(mdst), exist_ok=True)
                shutil.copy2(marker, mdst)
                pairs.append((marker, mdst))
            for src, cp in pairs:
                if os.path.getsize(src) != os.path.getsize(cp) or rp.sha256_of(src) != rp.sha256_of(cp):
                    raise rp.RepairRefused(f"backup verification FAILED for {src}; nothing was modified")
            c["backup"] = dst
            manifest.append({"symbol": c["symbol"], "last_bar": c["last_bar"], "mtime_utc": c["mtime_utc"],
                             "sha256": rp.sha256_of(c["path"]), "backup": dst})
        with open(os.path.join(run_backup, "manifest.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(manifest[0]))
            w.writeheader()
            w.writerows(manifest)
        for c in todo:
            res = rewrite_without(c["path"], c["_drop"], c["backup"])
            new_last = chk.last_bar_of(c["path"])
            rp.update_marker(c["path"], new_last, res["rows_after"])
            c["new_last_bar"] = None if new_last is None else new_last.isoformat()
            c["action"] = "cleaned"
            summary["cleaned_symbols"].append(c["symbol"])
    for c in todo:
        c.pop("_drop", None)
        c.setdefault("action", "would_clean")
        summary["files"].append({k: v for k, v in c.items() if k != "path"} | {"path": c["path"]})

    print(f"{summary['mode']}: checked at {now.isoformat()} -- scanned {summary['files_scanned']} file(s), "
          f"{summary['candidates']} candidate(s), {summary['files_to_clean']} file(s) with "
          f"{summary['bars_to_remove']} unfinished bar(s)")
    for c in todo[:40]:
        print(f"  {c['symbol']:<8} {c['action']:<10} remove {c['bars_to_remove']} ({', '.join(c['reasons'])}) last bar {c['last_bar']}")
    if len(todo) > 40:
        print(f"  ... {len(todo) - 40} more (see --report-json)")
    if args.apply and todo:
        print(f"  backup {run_backup}; re-extend with: ba2-test fetch-cache --timeframes 1d --symbols @<symbols-out> ...")
    elif not args.apply:
        print("  dry run: nothing was written. Add --apply --writers old-code-running to clean.")
    if args.report_json:
        os.makedirs(os.path.dirname(os.path.abspath(args.report_json)) or ".", exist_ok=True)
        with open(args.report_json, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, sort_keys=True)
    if args.symbols_out:
        syms = summary["cleaned_symbols"] if args.apply else [c["symbol"] for c in todo]
        with open(args.symbols_out, "w", encoding="utf-8") as f:
            f.write("\n".join(syms) + ("\n" if syms else ""))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], epilog=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache-dir", required=True)
    ap.add_argument("--backup-dir", required=True, help="OUTSIDE the cache")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--writers", choices=rp.WRITERS_CHOICES)
    ap.add_argument("--now", help="ISO UTC instant to judge finality at (default: the clock)")
    ap.add_argument("--report-json")
    ap.add_argument("--symbols-out")
    args = ap.parse_args(argv)
    try:
        return run(args)
    except rp.RepairRefused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
