#!/usr/bin/env python
"""Prepare the history data for backtests, per vendor: ONE pipeline CLI, ONE report per source.

Runbook: docs/REPRODUCE-BACKTESTS.md section 4 (data preparation).  Modelled on tools/prepare_option_universe.py: named
steps, ``--steps`` / ``--from-step``, ``--dry-run``, a JSON report, exit codes 0 / 1 / 2.

REQUIRED ``--source {alpaca,fmp}``.  Steps valid per source (in this order):
    alpaca: download, split-table, build, verify, market-conditions        fmp: download, verify, market-conditions
  Every step is implemented.  A step that is not valid for the source refuses (exit 2).  Never a silent no-op.  The default
  (no ``--steps``) runs EVERY step of the source, ``download`` first: it makes network requests (alpaca: resume-safe, ~0 requests
  over complete stores) -- pass ``--steps`` to run less.

STEP alpaca / download
  download      Runs ``tools/alpaca_history_download.py`` three times with the RECORDED arguments (code constants ``ALPACA_DOWNLOADS``:
                this is the runbook's source of truth).  Keys and account come from the arguments below.
                  1. 5-minute store ``--store-5min``: ``<store>/symbols.txt``, start 2019-01-01, end 2026-10-13, phases default
                     (corporate_actions, daily, intraday), max-gap-days 5;
                  2. daily store ``--store``: ``<store>/symbols.txt``, start 2016-01-01, end 2026-10-13, asof 2026-10-12, phases
                     corporate_actions,daily, max-gap-days 5;
                  3. the same daily store, phases daily, symbols ``<store>/symbols_spinco_20261020.txt`` plus
                     ``<out>/_meta/spinco_symbols_missing.txt`` (the split-table step's list of spin-off companies without bars)
                     if that file exists (merged, de-duplicated, order kept).
                Common: ``--keys-db`` (default: ``--db``) ``--account-id 1 --rpm 150 --min-remaining 60``.  A rerun over complete
                stores makes ~0 data requests; a run that exits non-zero fails the step.
    python tools/prepare_history.py --source alpaca --steps download

STEP alpaca / build
  build         Builds ``<out>/<SYM>_1d.parquet`` and ``<out>/<SYM>_5min.parquet`` (the FMP file layout) from the RAW daily store
                (``--store``), the RAW 5-minute store (``--store-5min``) and ``<out>/_meta/split_table.json``, with a process pool
                (``--workers``, one symbol at a time per worker).  Adjustment, break and session rules, file naming, the refusals
                (run_id of the table vs its CSVs, asof) and the manifest ``<out>/_meta/build_manifest.json``: module docstring of
                ``ba2_providers/ohlcv/alpaca_history/build.py``.  ``--symbols A,B`` builds a subset.
    python tools/prepare_history.py --source alpaca --steps build --workers 6

STEP alpaca|fmp / verify
  verify        DATA-QUALITY GATE over the BUILT cache (alpaca: ``--out``; fmp: ``--fmp-daily-dir``): structural checks of every file,
                5-minute coverage and self-consistency of the panel, residual overnight discontinuities vs the other vendor, the
                cross-vendor ratio stability, an independent Yahoo daily check on a seeded sample, and ED-11 readiness.  Each check
                writes its CSV under ``<report-dir>/verify/`` (default ``<out>/_meta/verify``); the step prints PASS / FAIL / INFO per check
                and exits 1 on any FAIL.  Checks, thresholds and CSVs: docstring of ``ba2_providers/ohlcv/alpaca_history/verify.py``.
                ``--panel-symbols`` (JSON list) selects the population the thresholds judge; ``--symbols`` restricts the run (the Yahoo
                sample is then those symbols); ``--refresh-yahoo`` re-fetches Yahoo.
    python tools/prepare_history.py --source alpaca --steps verify --panel-symbols panel_symbols.json
    python tools/prepare_history.py --source fmp --steps verify --panel-symbols panel_symbols.json

STEP fmp / download
  download      Wraps ``ba2-test fetch-cache`` (runbook 4.1 step 1) with the arguments given here -- the symbol universe and the window are
                the operator's, so they are REQUIRED (``--fmp-symbols-file`` ``--fmp-start`` ``--fmp-end``; no default universe):
                    python testplatform/ba2test_launcher.py fetch-cache --provider fmp --timeframes 5min,1d --start <fmp-start>                         --end <fmp-end> --symbols @<fmp-symbols-file> --workers <fmp-workers, default 5>
                The FMP key is read by ba2-test as everywhere else.  Non-zero exit of fetch-cache fails the step.
    python tools/prepare_history.py --source fmp --steps download --fmp-symbols-file universe.txt --fmp-start 2020-01-01 --fmp-end 2025-12-31

STEP alpaca|fmp / market-conditions
  market-conditions   The LAST step: warms, verifies and prepares the market-condition feature snapshots on this source's cache.
                For BOTH profiles (``ohlcv-v1``, ``ta-structure-v1``) it runs ``tools/warm_market_conditions.py`` ``plan`` -> ``build
                --cache-only`` -> ``verify`` -> ``prepare-host`` with ``--ohlcv-history-source <source>`` (alpaca / fmp), over
                ``--universe-file`` and the decision window ``--start`` / ``--end`` (ALL THREE REQUIRED: the step is refused, exit 2,
                without them).  It never fetches (the caches are built by the earlier steps).  The price folder the warm tool reads is
                ``<--mc-cache-root>/AlpacaHistoryOHLCVProvider`` (alpaca) or ``.../FMPOHLCVProvider`` (fmp), default ``--mc-cache-root``
                = the platform CACHE_FOLDER; ``--out`` (alpaca) / ``--fmp-daily-dir`` (fmp) must be that same folder or the step refuses.
                For ``--source alpaca`` the plan is made with ``--gap-fill previous`` (the build drops Alpaca's no-trade placeholder daily
                bars, so thin names have real gaps; the previous bar is carried forward inside the market-condition calculation only);
                ``--source fmp`` keeps the warm tool's default (no fill).
                The JSON report records, per profile, the manifest digest and the plan / build / verify / prepare-host outcome; the step
                (and the run) exits 1 when ANY profile fails.  Digests differ per source and per host (runbook 4.3).
    python tools/prepare_history.py --source alpaca --steps market-conditions \
        --universe-file tools/options_universe_top100.txt --start 2020-01-02 --end 2025-12-31

STEP alpaca / split-table
  split-table   Judge every Alpaca / FMP split candidate by the raw price jump (and FMP's adjusted-close step) and write
                our verified split table.  All rules, verdict codes, inputs and output columns are documented in the module
                docstring of ``ba2_providers/ohlcv/alpaca_history/split_table.py``; the rules live there, the build / I-O in ``split_build.py``; this file only
                runs it.  Reads the RAW daily store (``--store``), the FMP split calendar (fetched once, then cached under
                ``<out>/_meta/fmp_split_calendar/`` -- a rerun is OFFLINE), the FMP daily cache (``--fmp-daily-dir``) and
                ``split_overrides.json``.  Yahoo (``yfinance``, cached under ``<out>/_meta/yahoo_splits/``, ``--refresh-yahoo`` to fetch
                again) is consulted ONLY to confirm unlisted (inferred) splits -- never for listed events.  Writes under ``<out>/_meta``: split_table.json, split_review.csv,
                split_accepted.csv, split_vs_fmp.csv, fmp_daily_inputs.csv, yahoo_inputs.csv.

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
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE_STEPS = {"alpaca": ("download", "split-table", "build", "verify", "market-conditions"),
                "fmp": ("download", "verify", "market-conditions")}
MC_PROFILES = ("ohlcv-v1", "ta-structure-v1")
SPINCO_SYMBOLS_FILE = "symbols_spinco_20261020.txt"       # in the daily store: spin-off companies fetched by hand on 2026-10-20
SPINCO_MISSING_FILE = "spinco_symbols_missing.txt"        # <out>/_meta, written by the split-table step
DOWNLOAD_END = "2026-10-13"                                # first day NOT fetched (the stores' asof is the day before)
DOWNLOAD_ASOF = "2026-10-12"
#: THE RECORDED ``alpaca_history_download.py`` runs (runbook source of truth).  store: which store argument; symbols: file name in
#: the store (None = the spin-off list, see ``_spinco_symbols``); a None asof / phases is left to the downloader's default.
ALPACA_DOWNLOADS = (
    {"label": "5-minute store", "store": "5min", "symbols": "symbols.txt", "start": "2019-01-01", "end": DOWNLOAD_END,
     "asof": None, "phases": None, "max_gap_days": 5},
    {"label": "daily store + corporate actions", "store": "daily", "symbols": "symbols.txt", "start": "2016-01-01",
     "end": DOWNLOAD_END, "asof": DOWNLOAD_ASOF, "phases": "corporate_actions,daily", "max_gap_days": 5},
    {"label": "daily store, spin-off companies", "store": "daily", "symbols": None, "start": "2016-01-01", "end": DOWNLOAD_END,
     "asof": DOWNLOAD_ASOF, "phases": "daily", "max_gap_days": 5},
)
FMP_TIMEFRAMES = "5min,1d"
EXIT_OK, EXIT_FAILED, EXIT_CONFIG = 0, 1, 2


def build_parser() -> argparse.ArgumentParser:
    from ba2_providers.ohlcv.alpaca_history import build as bld, split_build as sb, split_table as st, verify as ver
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
    ap.add_argument("--store-5min", default=bld.DEFAULT_INTRADAY_STORE,
                    help="raw Alpaca 5-minute store (download, build; default: %(default)s)")
    ap.add_argument("--workers", type=int, default=4, help="build / verify process pool size (default: %(default)s)")
    ap.add_argument("--keys-db", help="SQLite DB with the Alpaca market-data keys (download; default: --db)")
    ap.add_argument("--account-id", type=int, default=1, help="account whose market-data keys the download uses (default: %(default)s)")
    ap.add_argument("--rpm", type=float, default=150, help="download request ceiling per minute (default: %(default)s)")
    ap.add_argument("--min-remaining", type=int, default=60, help="download: pause when fewer calls remain (default: %(default)s)")
    ap.add_argument("--fmp-symbols-file", help="fmp download: universe file, one symbol per line (REQUIRED for that step)")
    ap.add_argument("--fmp-start", help="fmp download: first day (REQUIRED for that step)")
    ap.add_argument("--fmp-end", help="fmp download: last day (REQUIRED for that step)")
    ap.add_argument("--fmp-workers", type=int, default=5, help="fmp download: fetch-cache --workers (default: %(default)s)")
    ap.add_argument("--verify-years", default="2019-2025", help="verify: year or range YYYY-YYYY (default: %(default)s)")
    ap.add_argument("--panel-symbols", help="verify: JSON list of the screener panel's symbols (the population the thresholds judge)")
    ap.add_argument("--yahoo-sample-size", type=int, default=ver.YAHOO_SAMPLE, help="verify: random panel symbols for the Yahoo check")
    ap.add_argument("--yahoo-seed", type=int, default=ver.YAHOO_SEED, help="verify: seed of that sample")
    ap.add_argument("--min-coverage", type=float, default=ver.MIN_COVERAGE, help="verify threshold (default: %(default)s)")
    ap.add_argument("--max-selfcons-bad", type=float, default=ver.MAX_SELFCONS_BAD,
                    help="verify threshold: share of days whose last 5-minute close is > 2%% off the daily close (default: %(default)s)")
    ap.add_argument("--symbols", help="comma-separated subset (quick run; split-table, build, verify)")
    ap.add_argument("--fmp-from", default="2016-01-01", help="first day of the FMP split calendar fetch")
    ap.add_argument("--fmp-to", help="last day of the FMP split calendar fetch (default: the store's asof + 1 day; "
                    "a value before the asof is refused)")
    ap.add_argument("--refresh-yahoo", action="store_true",
                    help="fetch Yahoo again: split-table (rule-B split history, default: use <out>/_meta/yahoo_splits) and verify (daily closes, default: use <report>/verify/yahoo_daily)")
    ap.add_argument("--overrides", default=st.OVERRIDES_PATH, help="reviewed split overrides (default: %(default)s)")
    ap.add_argument("--universe-file", help="market-conditions: universe file, one symbol per line (REQUIRED for that step)")
    ap.add_argument("--start", help="market-conditions: first DECISION session, ISO date (REQUIRED for that step)")
    ap.add_argument("--end", help="market-conditions: last DECISION session, ISO date, a COMPLETED session (REQUIRED for that step)")
    ap.add_argument("--mc-cache-root", help="market-conditions: CACHE_FOLDER holding the provider folders and market_conditions/ "
                    "(default: the platform CACHE_FOLDER; the step refuses an --out / --fmp-daily-dir that is not inside it)")
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
    print(f"Yahoo symbols consulted (unlisted-split candidates only): {r['yahoo_files']['count']}")
    print(f"FMP daily files used: {r['fmp_daily_files']['count']} (sha256 {r['fmp_daily_files']['sha256'][:16]}...)")
    print(f"wrote {r['meta']}")


def step_split_table(a: argparse.Namespace, log: Callable[[str], None]) -> Dict[str, Any]:
    from ba2_providers.ohlcv.alpaca_history import split_build as sb
    from ba2_providers.ohlcv.alpaca_history.split_table import _norm_symbol
    symbols = [_norm_symbol(x) for x in a.symbols.split(",") if x.strip()] if a.symbols else None
    result = sb.build_split_table(store=a.store, out=a.out, db=a.db, fmp_daily_dir=a.fmp_daily_dir, symbols=symbols,
                                  fmp_from=a.fmp_from, fmp_to=a.fmp_to, overrides_path=a.overrides, log=log,
                                  refresh_yahoo=a.refresh_yahoo)
    _print_split_table_summary(result)
    return result


def plan_split_table(a: argparse.Namespace) -> List[str]:
    """Dry run: what the step would read and write; raises on a missing input."""
    from ba2_providers.ohlcv.alpaca_history.split_table import load_bar_caps, load_overrides
    entries = load_overrides(a.overrides)
    caps = load_bar_caps(a.overrides)                     # raises SplitOverridesPending
    for label, path in (("store", os.path.join(a.store, "STORE_INFO.json")),
                        ("corporate actions", os.path.join(a.store, "corporate_actions")),
                        ("FMP daily dir", a.fmp_daily_dir)):
        if not os.path.exists(path):
            raise FileNotFoundError(f"{label} not found: {path}")
    cal = os.path.join(a.out, "_meta", "fmp_split_calendar")
    cached = len(os.listdir(cal)) if os.path.isdir(cal) else 0
    meta = os.path.join(a.out, "_meta")
    return [f"split-table: {len(entries)} reviewed split/spin-off override(s) + {len(caps)} bar_cap override(s) (applied by build); {cached} cached FMP calendar window(s) "
            f"({'offline' if cached else 'the FMP key will be read from ' + a.db})",
            f"split-table: would write split_table.json, split_review.csv, split_accepted.csv, split_vs_fmp.csv under {meta}"]


def _load_downloader():
    """tools/alpaca_history_download.py as a module (it is a script beside this file, not a package)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alpaca_history_download.py")
    spec = importlib.util.spec_from_file_location("alpaca_history_download", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _read_symbol_lines(path: str) -> List[str]:
    with open(path, encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip()]


def _spinco_symbols(a: argparse.Namespace) -> List[str]:
    """The spin-off list of the store plus the split-table step's missing-spinco list (if present): merged, de-duplicated."""
    syms = _read_symbol_lines(os.path.join(a.store, SPINCO_SYMBOLS_FILE))
    missing = os.path.join(a.out, "_meta", SPINCO_MISSING_FILE)
    if os.path.isfile(missing):
        syms += _read_symbol_lines(missing)
    return list(dict.fromkeys(syms))


def download_argv(a: argparse.Namespace, spec: Dict[str, Any], symbols_file: str) -> List[str]:
    """The argv of one recorded downloader run."""
    store = a.store_5min if spec["store"] == "5min" else a.store
    argv = ["--store", store, "--symbols-file", symbols_file, "--start", spec["start"], "--end", spec["end"]]
    if spec["asof"]:
        argv += ["--asof", spec["asof"]]
    if spec["phases"]:
        argv += ["--phases", spec["phases"]]
    argv += ["--max-gap-days", str(spec["max_gap_days"]), "--keys-db", a.keys_db or a.db, "--account-id", str(a.account_id),
             "--rpm", f"{a.rpm:g}", "--min-remaining", str(a.min_remaining)]
    return argv


def step_alpaca_download(a: argparse.Namespace, log: Callable[[str], None]) -> Dict[str, Any]:
    dl = _load_downloader()
    runs = []
    with tempfile.TemporaryDirectory() as tmp:
        for spec in ALPACA_DOWNLOADS:
            store = a.store_5min if spec["store"] == "5min" else a.store
            if spec["symbols"]:
                symbols_file = os.path.join(store, spec["symbols"])
            else:
                symbols_file = os.path.join(tmp, "symbols_spinco_merged.txt")
                with open(symbols_file, "w", encoding="utf-8") as f:
                    f.write("\n".join(_spinco_symbols(a)) + "\n")
            argv = download_argv(a, spec, symbols_file)
            log(f"download: {spec['label']}: alpaca_history_download.py {' '.join(argv)}")
            code = dl.main(argv)
            runs.append({"label": spec["label"], "argv": argv, "exit_code": code})
            if code != 0:
                raise RuntimeError(f"alpaca_history_download.py exited {code} for '{spec['label']}' (see its log above)")
    return {"runs": runs}


def plan_alpaca_download(a: argparse.Namespace) -> List[str]:
    lines = []
    for spec in ALPACA_DOWNLOADS:
        store = a.store_5min if spec["store"] == "5min" else a.store
        sym = spec["symbols"] or f"<{SPINCO_SYMBOLS_FILE} + {SPINCO_MISSING_FILE} merged>"
        if not os.path.isdir(store):
            raise FileNotFoundError(f"store not found: {store}")
        lines.append("download: alpaca_history_download.py " + " ".join(download_argv(a, spec, os.path.join(store, sym))))
    _spinco_symbols(a)                                           # raises if the spin-off list is missing
    return lines


def fmp_download_argv(a: argparse.Namespace) -> List[str]:
    missing = [n for n, v in (("--fmp-symbols-file", a.fmp_symbols_file), ("--fmp-start", a.fmp_start), ("--fmp-end", a.fmp_end)) if not v]
    if missing:
        raise ValueError(f"fmp download needs {', '.join(missing)}")
    date.fromisoformat(a.fmp_start)
    date.fromisoformat(a.fmp_end)
    if not os.path.isfile(a.fmp_symbols_file):
        raise FileNotFoundError(f"--fmp-symbols-file not found: {a.fmp_symbols_file}")
    launcher = os.path.join(REPO, "testplatform", "ba2test_launcher.py")
    return [sys.executable, launcher, "fetch-cache", "--provider", "fmp", "--timeframes", FMP_TIMEFRAMES, "--start", a.fmp_start,
            "--end", a.fmp_end, "--symbols", "@" + os.path.abspath(a.fmp_symbols_file), "--workers", str(a.fmp_workers)]


def step_fmp_download(a: argparse.Namespace, log: Callable[[str], None]) -> Dict[str, Any]:
    argv = fmp_download_argv(a)
    log("download: " + " ".join(argv))
    proc = subprocess.run(argv, cwd=REPO)
    if proc.returncode != 0:
        raise RuntimeError(f"ba2-test fetch-cache exited {proc.returncode}")
    return {"argv": argv, "exit_code": proc.returncode}


def plan_fmp_download(a: argparse.Namespace) -> List[str]:
    return ["download: " + " ".join(fmp_download_argv(a))]


def _symbol_list(a: argparse.Namespace) -> Optional[List[str]]:
    return [x.strip() for x in a.symbols.split(",") if x.strip()] if a.symbols else None


def _make_yahoo(a: argparse.Namespace, out_dir: str):
    """The Yahoo daily source shared by build (second witness for daily H/L) and verify: one cache under ``<report>/verify/yahoo_daily``."""
    from ba2_providers.ohlcv.alpaca_history import verify as ver
    from ba2_providers.ohlcv.alpaca_history.split_table import build_rename_map, read_corporate_actions, rename_successors
    renames = build_rename_map(read_corporate_actions(a.store))
    return ver.YahooDaily(os.path.join(out_dir, "yahoo_daily"), refresh=a.refresh_yahoo, successors=lambda s: rename_successors(s, renames))


def step_build(a: argparse.Namespace, log: Callable[[str], None]) -> Dict[str, Any]:
    from ba2_providers.ohlcv.alpaca_history import build as bld
    yahoo = _make_yahoo(a, _verify_dirs(a)[2])
    result = bld.build_cache(daily_store=a.store, intraday_store=a.store_5min, out=a.out, symbols=_symbol_list(a),
                             workers=a.workers, fmp_daily_dir=a.fmp_daily_dir, yahoo=yahoo, overrides_path=a.overrides, log=log)
    per = result.pop("per_symbol_s")
    result["seconds_per_symbol"] = round(sum(per.values()) / max(len(per), 1), 3)
    result["slowest"] = sorted(per.items(), key=lambda kv: -kv[1])[:5]
    for k, v in result.items():
        print(f"  {k}: {v}")
    return result


def plan_build(a: argparse.Namespace) -> List[str]:
    from ba2_providers.ohlcv.alpaca_history import build as bld
    table = bld.load_split_table(a.out)                       # raises BuildError on a missing table / run_id mismatch
    bld.load_bar_caps(a.overrides)                            # raises SplitOverridesPending on a pending override (bar_cap entries included)
    for label, path in (("daily store", a.store), ("5-minute store", a.store_5min)):
        if not os.path.isdir(os.path.join(path, "bars")):
            raise FileNotFoundError(f"{label} not found: {path}")
    return [f"build: split table {table.run_id} (asof {table.asof}); would write <SYM>_1d/_5min.parquet and "
            f"build_manifest.json under {a.out} with {a.workers} workers"]


def _years(text: str) -> List[int]:
    lo, _, hi = text.partition("-")
    return list(range(int(lo), int(hi or lo) + 1))


def _verify_dirs(a: argparse.Namespace) -> Tuple[str, Optional[str], str]:
    """(cache to verify, the other vendor's cache, output dir)."""
    cache = a.out if a.source == "alpaca" else a.fmp_daily_dir
    other = a.fmp_daily_dir if a.source == "alpaca" else (a.out if os.path.isdir(a.out) else None)
    return cache, other, os.path.join(a.report_dir or os.path.join(a.out, "_meta"), "verify")


def step_verify(a: argparse.Namespace, log: Callable[[str], None]) -> Dict[str, Any]:
    from ba2_providers.ohlcv.alpaca_history import verify as ver
    from ba2_providers.ohlcv.alpaca_history.split_table import build_rename_map, read_corporate_actions, rename_successors
    cache, other, out_dir = _verify_dirs(a)
    panel = None
    if a.panel_symbols:
        with open(a.panel_symbols, encoding="utf-8") as f:
            panel = [s.strip().upper().replace(".", "-") for s in json.load(f)]
    yahoo = _make_yahoo(a, out_dir)
    from ba2_providers.screener.live_sim_build import load_exclusions       # ONE source of truth: the screener panel's reviewed exclusions
    excluded = {s: str(e["reason"]) for s, e in load_exclusions().items()}
    symbols = [x.strip().upper().replace(".", "-") for x in a.symbols.split(",") if x.strip()] if a.symbols else None
    breaks = ver.load_breaks(os.path.join(a.out, "_meta")) if a.source == "alpaca" else {}
    spinoffs = ver.load_spinoffs(os.path.join(a.out, "_meta")) if a.source == "alpaca" else {}
    splits = ver.load_splits(os.path.join(a.out, "_meta")) if a.source == "alpaca" else None
    proven = ver.load_proven_events(os.path.join(a.out, "_meta")) if a.source == "alpaca" else None
    results = ver.run_gate(cache, other=other, out_dir=out_dir, symbols=symbols, panel=panel, years=_years(a.verify_years),
                           breaks=breaks, spinoffs=spinoffs, splits=splits, proven_events=proven, excluded=excluded, workers=a.workers, yahoo=yahoo, yahoo_sample=a.yahoo_sample_size, yahoo_seed=a.yahoo_seed,
                           min_coverage=a.min_coverage, max_selfcons_bad=a.max_selfcons_bad, log=log)
    for name, r in results.items():
        print(f"{r['status']:5s} {name}: {json.dumps(r['summary'], default=str)[:1500]}")
        if r["csv"]:
            print(f"       {r['csv']}")
    failed = ver.gate_failed(results)
    out = {"checks": {k: {"status": v["status"], "summary": v["summary"], "csv": v["csv"]} for k, v in results.items()}, "failed": failed}
    if failed:
        raise RuntimeError("verify FAILED: " + ", ".join(failed))
    return out


def plan_verify(a: argparse.Namespace) -> List[str]:
    cache, other, out_dir = _verify_dirs(a)
    if not os.path.isdir(cache):
        raise FileNotFoundError(f"cache not found: {cache}")
    _years(a.verify_years)
    return [f"verify: would read {cache} (other vendor: {other}) years {a.verify_years}, write the check CSVs under {out_dir}"]


def _load_warm():
    """tools/warm_market_conditions.py as a module (a script beside this file, not a package); the seam tests replace."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "warm_market_conditions.py")
    spec = importlib.util.spec_from_file_location("warm_market_conditions", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def mc_inputs(a: argparse.Namespace) -> Tuple[str, str, str, str, str]:
    """(universe file, start, end, cache root, price folder) of the market-conditions step, or ValueError (a config error)."""
    from ba2_common.core.market_condition_source import provider_dir_for_source_profile, source_profile_for_history
    missing = [n for n, v in (("--universe-file", a.universe_file), ("--start", a.start), ("--end", a.end)) if not v]
    if missing:
        raise ValueError(f"market-conditions needs {', '.join(missing)} (the universe and the decision window are the operator's)")
    if date.fromisoformat(a.end) < date.fromisoformat(a.start):
        raise ValueError(f"--end {a.end} is before --start {a.start}")
    if not os.path.isfile(a.universe_file):
        raise FileNotFoundError(f"--universe-file not found: {a.universe_file}")
    if a.mc_cache_root:
        root = os.path.abspath(a.mc_cache_root)
    else:
        from ba2_common import config
        root = os.path.abspath(config.CACHE_FOLDER)
    folder = os.path.join(root, provider_dir_for_source_profile(source_profile_for_history(a.source)))
    have = a.out if a.source == "alpaca" else a.fmp_daily_dir
    if os.path.normcase(os.path.abspath(have)) != os.path.normcase(folder):
        raise ValueError(f"market-conditions would read {folder} but the {'--out' if a.source == 'alpaca' else '--fmp-daily-dir'} "
                         f"cache is {have}: pass --mc-cache-root so both name the same folder")
    return a.universe_file, a.start, a.end, root, folder


class StepFailed(RuntimeError):
    """A step that failed but has a result worth reporting (``result``)."""

    def __init__(self, message: str, result: Dict[str, Any]):
        super().__init__(message)
        self.result = result


def mc_commands(a: argparse.Namespace, profile: str, plan_path: str, digest: str = "<digest>") -> List[List[str]]:
    """The four warm_market_conditions argv lists (without the program) of one profile; runbook 4.3 in code."""
    universe, start, end, root, _folder = mc_inputs(a)
    from ba2_common.core.market_condition_source import GAP_FILL_PREVIOUS
    hs = ["--ohlcv-history-source", a.source, "--quiet"]
    cr = ["--cache-root", root]                                # build takes none: it reads the plan's own cache_root
    # alpaca: the build drops Alpaca's no-trade placeholder daily bars, so thin names have real gaps; the plan carries the previous
    # valid bar forward inside the market-condition calculation only (the history files keep the gaps).  fmp keeps its default.
    gap = ["--gap-fill", GAP_FILL_PREVIOUS] if a.source == "alpaca" else []
    return [["plan", "--profile", profile, "--universe-file", universe, "--start", start, "--end", end, "--out", plan_path, *cr, *hs, *gap],
            ["build", "--plan", plan_path, "--cache-only", "--print-digest", *hs],
            ["verify", "--manifest", digest, *cr, *hs],
            ["prepare-host", "--manifest", digest, "--profile", profile, "--jobs", str(a.workers), *cr, *hs]]


def _run_warm(warm, argv: List[str]) -> Tuple[int, str, str]:
    """warm.main(argv) in-process; (exit code, stdout, stderr)."""
    import contextlib
    import io
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = warm.main(argv)
    return code, out.getvalue(), err.getvalue()


def step_market_conditions(a: argparse.Namespace, log: Callable[[str], None]) -> Dict[str, Any]:
    mc_inputs(a)                                              # validates; a ValueError here is a failed step, not a silent skip
    warm = _load_warm()
    plan_dir = os.path.join(a.report_dir or os.path.join(a.out, "_meta"), "market_conditions")
    os.makedirs(plan_dir, exist_ok=True)
    profiles: Dict[str, Dict[str, Any]] = {}
    for profile in MC_PROFILES:
        plan_path = os.path.join(plan_dir, f"plan.{profile}.{a.source}.json")
        res: Dict[str, Any] = {"plan": plan_path, "digest": None}
        profiles[profile] = res
        digest = "<digest>"
        for i, name in enumerate(("plan", "build", "verify", "prepare-host")):
            argv = mc_commands(a, profile, plan_path, digest)[i]
            log(f"market-conditions {profile}: warm_market_conditions.py {' '.join(argv)}")
            code, out, err = _run_warm(warm, argv)
            res[name] = {"exit_code": code}
            if name == "build" and code == 0:
                lines = [ln for ln in out.splitlines() if ln.strip()]
                digest = lines[-1].strip() if lines else ""
                if not digest:
                    res[name]["error"] = "build published no manifest digest"
                    code = 1
                res["digest"] = digest or None
            if code != 0:
                tail = (out if name != "build" else err).strip()
                res[name]["output"] = tail[-4000:]
                res["failed"] = name
                print(f"market-conditions {profile}: {name} FAILED (exit {code}): {tail[-2000:]}", file=sys.stderr)
                break                                         # the next profile still runs: a report with BOTH outcomes
    failed = {p: r["failed"] for p, r in profiles.items() if "failed" in r}
    result = {"source": a.source, "universe_file": a.universe_file, "start": a.start, "end": a.end,
              "digests": {p: r["digest"] for p, r in profiles.items()}, "profiles": profiles}
    for p, r in profiles.items():
        print(f"  {p}: digest {r['digest']}" + (f"  FAILED at {r['failed']}" if "failed" in r else ""))
    if failed:
        raise StepFailed("market-conditions FAILED: " + ", ".join(f"{p} at {n}" for p, n in failed.items()), result)
    return result


def plan_market_conditions(a: argparse.Namespace) -> List[str]:
    lines = []
    for profile in MC_PROFILES:
        for argv in mc_commands(a, profile, f"<plan.{profile}.{a.source}.json>"):
            lines.append("market-conditions: warm_market_conditions.py " + " ".join(argv))
    return lines


RUNNERS: Dict[Tuple[str, str], Callable[[argparse.Namespace, Callable[[str], None]], Dict[str, Any]]] = {
    ("alpaca", "download"): step_alpaca_download, ("alpaca", "split-table"): step_split_table, ("alpaca", "build"): step_build,
    ("alpaca", "verify"): step_verify, ("fmp", "download"): step_fmp_download, ("fmp", "verify"): step_verify,
    ("alpaca", "market-conditions"): step_market_conditions, ("fmp", "market-conditions"): step_market_conditions}
PLANNERS: Dict[Tuple[str, str], Callable[[argparse.Namespace], List[str]]] = {
    ("alpaca", "download"): plan_alpaca_download, ("alpaca", "split-table"): plan_split_table, ("alpaca", "build"): plan_build,
    ("alpaca", "verify"): plan_verify, ("fmp", "download"): plan_fmp_download, ("fmp", "verify"): plan_verify,
    ("alpaca", "market-conditions"): plan_market_conditions, ("fmp", "market-conditions"): plan_market_conditions}


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
    try:
        date.fromisoformat(a.fmp_from)
        if a.fmp_to:
            date.fromisoformat(a.fmp_to)
    except ValueError as e:
        print(f"bad --fmp-from/--fmp-to: {e}", file=sys.stderr)
        return EXIT_CONFIG

    if "market-conditions" in steps:
        try:
            mc_inputs(a)
        except (ValueError, FileNotFoundError) as e:
            print(f"REFUSED: step market-conditions: {e}", file=sys.stderr)
            return EXIT_CONFIG

    from ba2_providers.fmp_common import redact
    from ba2_providers.ohlcv.alpaca_history.build import BuildError
    from ba2_providers.ohlcv.alpaca_history.split_table import SplitOverridesPending
    if a.dry_run:
        try:
            for s in steps:
                for line in PLANNERS[(a.source, s)](a):
                    print(line)
        except SplitOverridesPending as e:
            print(f"REFUSED: {e}", file=sys.stderr)
            return EXIT_CONFIG
        except (FileNotFoundError, ValueError, BuildError) as e:
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
            entry["result"] = RUNNERS[(a.source, s)](a, log)
            entry["status"] = "ok"
        except (SplitOverridesPending, BuildError) as e:
            print(f"REFUSED: {e}", file=sys.stderr)
            return EXIT_CONFIG                                   # a refused input: nothing (more) to report
        except Exception as e:                                   # a failed step stops the pipeline, named
            # the traceback and message go to the report and stderr REDACTED (a request error can embed the key)
            entry["status"], entry["error"] = "failed", redact(f"{type(e).__name__}: {e}")
            if isinstance(e, StepFailed):
                entry["result"] = e.result
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
