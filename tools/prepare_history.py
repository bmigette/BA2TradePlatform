#!/usr/bin/env python
"""Prepare the history data for backtests, per vendor: ONE pipeline CLI, ONE report per source.

Runbook: docs/REPRODUCE-BACKTESTS.md section 4 (data preparation).  Modelled on tools/prepare_option_universe.py: named
steps, ``--steps`` / ``--from-step``, ``--dry-run``, a JSON report, exit codes 0 / 1 / 2.

REQUIRED ``--source {alpaca,fmp}``.  Steps valid per source (in this order):
    alpaca: download, split-table, build, verify        fmp: download, verify
  IMPLEMENTED today: alpaca / split-table ONLY.  Every other valid step (and the default "all steps") refuses with
  "not implemented yet (plan Task 4)", exit 2, before anything runs; a step that is not valid for the source also refuses
  (exit 2).  Never a silent no-op.

STEP alpaca / split-table
  split-table   Judge every Alpaca / FMP split candidate by the raw price jump (and FMP's adjusted-close step) and write
                our verified split table.  All rules, verdict codes, inputs and output columns are documented in the module
                docstring of ``ba2_providers/ohlcv/alpaca_history/split_table.py``; the rules live there, the build / I-O in ``split_build.py``; this file only
                runs it.  Reads the RAW daily store (``--store``), the FMP split calendar (fetched once, then cached under
                ``<out>/_meta/fmp_split_calendar/`` -- a rerun is OFFLINE), the FMP daily cache (``--fmp-daily-dir``) and
                ``split_overrides.json``.  Writes under ``<out>/_meta``: split_table.json, split_review.csv,
                split_accepted.csv, split_vs_fmp.csv.

FULL COMMAND (the defaults are the production paths; every path is overridable)
    python tools/prepare_history.py --source alpaca --steps split-table \
        --store ~/Documents/ba2/alpaca_store_daily2016 \
        --out ~/Documents/ba2/common/cache/AlpacaHistoryOHLCVProvider \
        --fmp-daily-dir ~/Documents/ba2/common/cache/FMPOHLCVProvider \
        --db ~/Documents/ba2/trade/db.sqlite --fmp-from 2016-01-01 --fmp-to <store asof + 1 day>
RE-RUN OFFLINE (identical command: every calendar window is in the cache, so the FMP key is never read and no request is
made; the result is byte-identical for identical inputs)
    python tools/prepare_history.py --source alpaca --steps split-table
QUICK SUBSET   add ``--symbols AAPL,NVDA,TSLA``.   PLAN ONLY   add ``--dry-run`` (checks the inputs and the overrides
file, writes nothing).

REPORT.  ``<report-dir>/prepare_history_<source>_report.json`` (default report dir = ``<out>/_meta``): per step its status, elapsed
seconds, and result (symbols judged, accepted events, review rows, the verdict counters, the comparison with FMP's calendar
by class with up to 10 examples each, the FMP daily files used) -- or the error that stopped it.

Exit codes: 0 every step passed; 1 a step failed (named in the report and on stderr); 2 a configuration error, including a
``split_overrides.json`` entry still pending review (reviewed_by starting with 'pending' REFUSES the build).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE_STEPS = {"alpaca": ("download", "split-table", "build", "verify"), "fmp": ("download", "verify")}
IMPLEMENTED = {("alpaca", "split-table")}
EXIT_OK, EXIT_FAILED, EXIT_CONFIG = 0, 1, 2


def build_parser() -> argparse.ArgumentParser:
    from ba2_providers.ohlcv.alpaca_history import split_build as sb, split_table as st
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True, choices=sorted(SOURCE_STEPS), help="the vendor whose history to prepare")
    ap.add_argument("--steps", help="comma list of steps to run (default: every step of the source). Steps: "
                    + "; ".join(f"{k}: {', '.join(v)}" for k, v in SOURCE_STEPS.items()))
    ap.add_argument("--from-step", help="run this step and every later one of the source")
    ap.add_argument("--dry-run", action="store_true", help="check the inputs and the overrides, write nothing")
    ap.add_argument("--store", default=sb.DEFAULT_STORE, help="raw Alpaca daily store (default: %(default)s)")
    ap.add_argument("--out", default=sb.DEFAULT_OUT, help="cache folder; outputs under <out>/_meta (default: %(default)s)")
    ap.add_argument("--fmp-daily-dir", default=sb.DEFAULT_FMP_DAILY, help="FMP adjusted daily files (default: %(default)s)")
    ap.add_argument("--db", default=sb.DEFAULT_DB, help="SQLite DB holding the AppSetting FMP_API_KEY (read-only)")
    ap.add_argument("--symbols", help="comma-separated subset to judge (quick run)")
    ap.add_argument("--fmp-from", default="2016-01-01", help="first day of the FMP split calendar fetch")
    ap.add_argument("--fmp-to", help="last day of the FMP split calendar fetch (default: the store's asof + 1 day; "
                    "a value before the asof is refused)")
    ap.add_argument("--overrides", default=st.OVERRIDES_PATH, help="reviewed split overrides (default: %(default)s)")
    ap.add_argument("--report-dir", help="where prepare_history_<source>_report.json goes (default: <out>/_meta)")
    return ap


def _write_report(path: str, report: Dict[str, Any]) -> None:
    from ba2_providers.ohlcv.alpaca_history.split_build import atomic_write
    atomic_write(path, json.dumps(report, indent=1, default=str).encode("utf-8"))


def _print_split_table_summary(r: Dict[str, Any]) -> None:
    print(f"asof {r['asof']}  symbols judged {r['symbols_judged']}  symbols with splits {r['symbols_with_splits']}  "
          f"accepted events {r['accepted']}  review rows {r['review_rows']} (applied {r['review_applied']})")
    for k, v in r["stats"].items():
        print(f"  {k}: {v}")
    print("vs FMP's calendar:")
    for cls, n in r["compare"].items():
        print(f"  {cls}: {n}")
        for row in r["compare_examples"].get(cls, []):
            print("     " + "  ".join(f"{k}={v}" for k, v in row.items() if k != "class"))
    print(f"FMP daily files used: {r['fmp_daily_files']['count']} (sha256 {r['fmp_daily_files']['sha256'][:16]}...)")
    print(f"wrote {r['meta']}")


def step_split_table(a: argparse.Namespace, log: Callable[[str], None]) -> Dict[str, Any]:
    from ba2_providers.ohlcv.alpaca_history import split_build as sb
    from ba2_providers.ohlcv.alpaca_history.split_table import _norm_symbol
    symbols = [_norm_symbol(x) for x in a.symbols.split(",") if x.strip()] if a.symbols else None
    result = sb.build_split_table(store=a.store, out=a.out, db=a.db, fmp_daily_dir=a.fmp_daily_dir, symbols=symbols,
                                  fmp_from=a.fmp_from, fmp_to=a.fmp_to, overrides_path=a.overrides, log=log)
    _print_split_table_summary(result)
    return result


def plan_split_table(a: argparse.Namespace) -> List[str]:
    """Dry run: what the step would read and write; raises on a missing input."""
    from ba2_providers.ohlcv.alpaca_history.split_table import load_overrides
    entries = load_overrides(a.overrides)                     # raises SplitOverridesPending
    for label, path in (("store", os.path.join(a.store, "STORE_INFO.json")),
                        ("corporate actions", os.path.join(a.store, "corporate_actions")),
                        ("FMP daily dir", a.fmp_daily_dir)):
        if not os.path.exists(path):
            raise FileNotFoundError(f"{label} not found: {path}")
    cal = os.path.join(a.out, "_meta", "fmp_split_calendar")
    cached = len(os.listdir(cal)) if os.path.isdir(cal) else 0
    meta = os.path.join(a.out, "_meta")
    return [f"split-table: {len(entries)} reviewed override(s); {cached} cached FMP calendar window(s) "
            f"({'offline' if cached else 'the FMP key will be read from ' + a.db})",
            f"split-table: would write split_table.json, split_review.csv, split_accepted.csv, split_vs_fmp.csv under {meta}"]


RUNNERS: Dict[str, Callable[[argparse.Namespace, Callable[[str], None]], Dict[str, Any]]] = {"split-table": step_split_table}
PLANNERS: Dict[str, Callable[[argparse.Namespace], List[str]]] = {"split-table": plan_split_table}   # alpaca only


def main(argv: Optional[List[str]] = None, *, log: Optional[Callable[[str], None]] = None) -> int:
    try:
        a = build_parser().parse_args(argv)
    except SystemExit as e:
        return EXIT_CONFIG if e.code else EXIT_OK
    log = log or (lambda m: print(f"[{datetime.now():%H:%M:%S}] {m}", file=sys.stderr, flush=True))
    valid = SOURCE_STEPS[a.source]
    if a.steps and a.from_step:
        print("pass --steps or --from-step, not both", file=sys.stderr)
        return EXIT_CONFIG
    if a.steps:
        want = [x.strip() for x in a.steps.split(",") if x.strip()]
        bad = [x for x in want if x not in valid]
        if bad:
            print(f"step(s) {bad} are not valid for --source {a.source}; valid: {', '.join(valid)}", file=sys.stderr)
            return EXIT_CONFIG
        steps = [x for x in valid if x in want]
    elif a.from_step:
        if a.from_step not in valid:
            print(f"--from-step {a.from_step!r} is not valid for --source {a.source}; valid: {', '.join(valid)}",
                  file=sys.stderr)
            return EXIT_CONFIG
        steps = list(valid[valid.index(a.from_step):])
    else:
        steps = list(valid)
    todo = [x for x in steps if (a.source, x) not in IMPLEMENTED]
    if todo:
        done = ", ".join(f"{src}/{st_}" for src, st_ in sorted(IMPLEMENTED))
        print(f"step(s) {todo} of --source {a.source}: not implemented yet (plan Task 4); nothing was run. "
              f"Implemented: {done}", file=sys.stderr)
        return EXIT_CONFIG
    try:
        date.fromisoformat(a.fmp_from)
        if a.fmp_to:
            date.fromisoformat(a.fmp_to)
    except ValueError as e:
        print(f"bad --fmp-from/--fmp-to: {e}", file=sys.stderr)
        return EXIT_CONFIG

    from ba2_providers.fmp_common import redact
    from ba2_providers.ohlcv.alpaca_history.split_table import SplitOverridesPending
    if a.dry_run:
        try:
            for s in steps:
                for line in PLANNERS[s](a):
                    print(line)
        except SplitOverridesPending as e:
            print(f"REFUSED: {e}", file=sys.stderr)
            return EXIT_CONFIG
        except FileNotFoundError as e:
            print(str(e), file=sys.stderr)
            return EXIT_CONFIG
        return EXIT_OK

    report_path = os.path.join(a.report_dir or os.path.join(a.out, "_meta"), f"prepare_history_{a.source}_report.json")
    report: Dict[str, Any] = {"started": datetime.now().isoformat(timespec="seconds"), "argv": argv if argv is not None
                              else sys.argv[1:], "steps": []}
    code = EXIT_OK
    for s in steps:
        t0 = time.time()
        entry: Dict[str, Any] = {"step": s}
        try:
            entry["result"] = RUNNERS[s](a, log)
            entry["status"] = "ok"
        except SplitOverridesPending as e:
            print(f"REFUSED: {e}", file=sys.stderr)
            return EXIT_CONFIG                                   # nothing ran, nothing to report
        except Exception as e:                                   # a failed step stops the pipeline, named
            # the traceback and message go to the report and stderr REDACTED (a request error can embed the key)
            entry["status"], entry["error"] = "failed", redact(f"{type(e).__name__}: {e}")
            entry["traceback"] = redact(traceback.format_exc())
            print(f"step {s} FAILED: {entry['error']}", file=sys.stderr)
            code = EXIT_FAILED
        entry["elapsed_s"] = round(time.time() - t0, 1)
        report["steps"].append(entry)
        if code != EXIT_OK:
            break
    report["finished"] = datetime.now().isoformat(timespec="seconds")
    _write_report(report_path, report)
    log(f"report: {report_path}")
    return code


if __name__ == "__main__":
    sys.exit(main())
