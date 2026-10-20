#!/usr/bin/env python
"""Prepare the Alpaca history data for backtests: ONE pipeline CLI, ONE report.

Runbook: docs/REPRODUCE-BACKTESTS.md section 4 (data preparation).  Modelled on tools/prepare_option_universe.py: named
steps, ``--steps`` / ``--from-step``, ``--dry-run``, a JSON report, exit codes 0 / 1 / 2.

STEPS (in this order; later tasks add download, build and verify)
  split-table   Judge every Alpaca / FMP split candidate by the raw price jump (and FMP's adjusted-close step) and write
                our verified split table.  All rules, verdict codes, inputs and output columns are documented in the module
                docstring of ``ba2_providers/ohlcv/alpaca_history/split_table.py``; the logic lives there, this file only
                runs it.  Reads the RAW daily store (``--store``), the FMP split calendar (fetched once, then cached under
                ``<out>/_meta/fmp_split_calendar/`` -- a rerun is OFFLINE), the FMP daily cache (``--fmp-daily-dir``) and
                ``split_overrides.json``.  Writes under ``<out>/_meta``: split_table.json, split_review.csv,
                split_accepted.csv, split_vs_fmp.csv.

FULL COMMAND (the defaults are the production paths; every path is overridable)
    python tools/prepare_alpaca_history.py --steps split-table \
        --store ~/Documents/ba2/alpaca_store_daily2016 \
        --out ~/Documents/ba2/common/cache/AlpacaHistoryOHLCVProvider \
        --fmp-daily-dir ~/Documents/ba2/common/cache/FMPOHLCVProvider \
        --db ~/Documents/ba2/trade/db.sqlite --fmp-from 2016-01-01 --fmp-to 2026-10-13
RE-RUN OFFLINE (identical command: every calendar window is in the cache, so the FMP key is never read and no request is
made; the result is byte-identical for identical inputs)
    python tools/prepare_alpaca_history.py --steps split-table
QUICK SUBSET   add ``--symbols AAPL,NVDA,TSLA``.   PLAN ONLY   add ``--dry-run`` (checks the inputs and the overrides
file, writes nothing).

REPORT.  ``<report-dir>/prepare_report.json`` (default report dir = ``<out>/_meta``): per step its status, elapsed
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
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STEPS = ("split-table",)
EXIT_OK, EXIT_FAILED, EXIT_CONFIG = 0, 1, 2


def build_parser() -> argparse.ArgumentParser:
    from ba2_providers.ohlcv.alpaca_history import split_table as st
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--steps", help=f"comma list of steps to run (default: all). Steps: {', '.join(STEPS)}")
    ap.add_argument("--from-step", choices=STEPS, help="run this step and every later one")
    ap.add_argument("--dry-run", action="store_true", help="check the inputs and the overrides, write nothing")
    ap.add_argument("--store", default=st.DEFAULT_STORE, help="raw Alpaca daily store (default: %(default)s)")
    ap.add_argument("--out", default=st.DEFAULT_OUT, help="cache folder; outputs under <out>/_meta (default: %(default)s)")
    ap.add_argument("--fmp-daily-dir", default=st.DEFAULT_FMP_DAILY, help="FMP adjusted daily files (default: %(default)s)")
    ap.add_argument("--db", default=st.DEFAULT_DB, help="SQLite DB holding the AppSetting FMP_API_KEY (read-only)")
    ap.add_argument("--symbols", help="comma-separated subset to judge (quick run)")
    ap.add_argument("--fmp-from", default="2016-01-01", help="first day of the FMP split calendar fetch")
    ap.add_argument("--fmp-to", default="2026-10-13", help="last day of the FMP split calendar fetch")
    ap.add_argument("--overrides", default=st.OVERRIDES_PATH, help="reviewed split overrides (default: %(default)s)")
    ap.add_argument("--report-dir", help="where prepare_report.json goes (default: <out>/_meta)")
    return ap


def _write_report(path: str, report: Dict[str, Any]) -> None:
    from ba2_providers.ohlcv.alpaca_history.split_table import atomic_write
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
    from ba2_providers.ohlcv.alpaca_history import split_table as st
    symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()] if a.symbols else None
    result = st.build_split_table(store=a.store, out=a.out, db=a.db, fmp_daily_dir=a.fmp_daily_dir, symbols=symbols,
                                  fmp_from=a.fmp_from, fmp_to=a.fmp_to, overrides_path=a.overrides, log=log)
    _print_split_table_summary(result)
    return result


def plan_split_table(a: argparse.Namespace) -> List[str]:
    """Dry run: what the step would read and write; raises on a missing input."""
    from ba2_providers.ohlcv.alpaca_history import split_table as st
    entries = st.load_overrides(a.overrides)                     # raises SplitOverridesPending
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
PLANNERS: Dict[str, Callable[[argparse.Namespace], List[str]]] = {"split-table": plan_split_table}


def main(argv: Optional[List[str]] = None, *, log: Optional[Callable[[str], None]] = None) -> int:
    try:
        a = build_parser().parse_args(argv)
    except SystemExit as e:
        return EXIT_CONFIG if e.code else EXIT_OK
    log = log or (lambda m: print(f"[{datetime.now():%H:%M:%S}] {m}", file=sys.stderr, flush=True))
    if a.steps and a.from_step:
        print("pass --steps or --from-step, not both", file=sys.stderr)
        return EXIT_CONFIG
    if a.steps:
        want = [s.strip() for s in a.steps.split(",") if s.strip()]
        bad = [s for s in want if s not in STEPS]
        if bad:
            print(f"unknown step(s) {bad}; steps: {', '.join(STEPS)}", file=sys.stderr)
            return EXIT_CONFIG
        steps = [s for s in STEPS if s in want]
    elif a.from_step:
        steps = list(STEPS[STEPS.index(a.from_step):])
    else:
        steps = list(STEPS)
    try:
        date.fromisoformat(a.fmp_from), date.fromisoformat(a.fmp_to)
    except ValueError as e:
        print(f"bad --fmp-from/--fmp-to: {e}", file=sys.stderr)
        return EXIT_CONFIG

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

    report_path = os.path.join(a.report_dir or os.path.join(a.out, "_meta"), "prepare_report.json")
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
            entry["status"], entry["error"] = "failed", f"{type(e).__name__}: {e}"
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
