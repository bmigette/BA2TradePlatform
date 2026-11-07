"""Download Alpha Vantage ``HISTORICAL_OPTIONS`` end-of-day chains into the AV option store.

One API call = one (symbol, trading day) chain. Resumable and crash-safe: every fetched day (rows or a
recorded empty) is written to ``<root>/<SYM>/_days`` and then recorded in ``<root>/<SYM>/_ledger.jsonl``;
a (symbol, date) in the ledger is never fetched again, and the staged days are compacted into the
ThetaData-shaped ``exp=`` partitions every ``--compact-every`` days and at the end of each symbol (and on
Ctrl-C). Layout, mapping rules and the close/split decisions: ``ba2_providers/options/alphavantage.py``.

    python tools/download_av_option_chains.py \\
        --symbols-file tools/options_universe_large_cap.txt --symbols-file tools/options_universe_top100.txt \\
        --start 2020-01-01 --db ~/Documents/ba2/test/dl_forecasting.db --report av_options_report.json

Rate limit: the premium plan is 75 calls/minute for the KEY. ``--calls-per-minute`` is this process's own
cap (default 60); ``--key-limit`` (75) is the ceiling over every process that shares ``--budget-file``
(several downloaders coexist through it). A process that does NOT use the file, such as
``tools/backfill_av_estimates.py`` (~45 calls/min), is invisible to it: while both run, pass
``--calls-per-minute 25``. The API key comes from the AppSetting ``alpha_vantage_api_key`` of ``--db`` and is
never printed or logged (error text is redacted).

Failures are not recorded as done: they are listed in the report with their reason and retried by the next run.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence


def read_symbols(paths: Sequence[str]) -> List[str]:
    out: List[str] = []
    for p in paths:
        text = open(os.path.expanduser(p), encoding="utf-8").read()
        for tok in text.replace(",", "\n").split():
            tok = tok.strip().upper()
            if tok and not tok.startswith("#") and tok not in out:
                out.append(tok)
    return out


def trading_days(start: date, end: date) -> List[date]:
    """NYSE regular sessions in [start, end] (holidays and half days from pandas_market_calendars)."""
    from zoneinfo import ZoneInfo

    from ba2_common.core.market_calendar import nyse_regular_sessions
    ny = ZoneInfo("America/New_York")
    return [o.astimezone(ny).date() for o, _c in nyse_regular_sessions(start, end)]


def latest_session(today: Optional[date] = None) -> date:
    """The most recent session strictly before ``today`` (a chain is final after the close)."""
    today = today or date.today()
    days = trading_days(today - timedelta(days=12), today - timedelta(days=1))
    if not days:
        raise RuntimeError("no trading session found in the 12 days before today")
    return days[-1]


class Runner:
    """The download loop, with its collaborators injected (tests pass fakes; no network)."""

    def __init__(self, store, fetch: Callable[[str, date], List[Dict[str, Any]]], budget,
                 *, workers: int = 4, compact_every: int = 200, max_attempts: int = 4,
                 abort_after: int = 60, sleep: Callable[[float], None] = time.sleep,
                 log: Callable[[str], None] = lambda m: print(m, flush=True)):
        self.store, self.fetch, self.budget = store, fetch, budget
        self.workers, self.compact_every, self.max_attempts = workers, compact_every, max_attempts
        self.abort_after, self.sleep, self.log = abort_after, sleep, log
        self._consecutive_failures = 0
        self._lock = threading.Lock()
        self.stop = threading.Event()
        self.aborted: Optional[str] = None

    def _one(self, symbol: str, day: date, res: dict) -> None:
        from ba2_providers.options.alphavantage import AlphaVantageThrottled, map_chain
        if self.stop.is_set():
            return
        reason = "unknown"
        for attempt in range(1, self.max_attempts + 1):
            try:
                self.budget.acquire()
                rows = self.fetch(symbol, day)
                df, stats = map_chain(symbol, day, rows)
                self.store.record_day(symbol, day, df, stats)
                with self._lock:
                    res["ok" if len(df) else "empty"] += 1
                    res["rows"] += len(df)
                    self._consecutive_failures = 0
                return
            except AlphaVantageThrottled as e:
                reason = f"throttled: {e}"
                self.sleep(min(30.0 * attempt, 120.0))
            except Exception as e:  # noqa: BLE001 -- one day's failure is reported, never stops the run
                reason = f"{type(e).__name__}: {str(e)[:200]}"
                self.sleep(min(2.0 * attempt, 10.0))
        with self._lock:
            res["failed"][day.isoformat()] = reason
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.abort_after:
                self.aborted = f"{self._consecutive_failures} consecutive failures; last: {reason}"
                self.stop.set()

    def run_symbol(self, symbol: str, days: Sequence[date]) -> dict:
        res = {"ok": 0, "empty": 0, "rows": 0, "failed": {}, "skipped": 0, "partitions": 0}
        pending = self.store.pending_days(symbol, days)
        res["skipped"] = len(days) - len(pending)
        try:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                for i in range(0, len(pending), self.compact_every):
                    chunk = pending[i:i + self.compact_every]
                    list(pool.map(lambda d: self._one(symbol, d, res), chunk))
                    res["partitions"] += self.store.compact(symbol)
                    if self.stop.is_set():
                        break
        finally:
            res["partitions"] += self.store.compact(symbol)
        res["bytes"] = self.store.disk_bytes(symbol)
        return res

    def run(self, symbols: Sequence[str], days: Sequence[date], report_path: Optional[str] = None,
            meta: Optional[dict] = None) -> dict:
        report = {"started": datetime.now(timezone.utc).isoformat(), **(meta or {}),
                  "days_per_symbol": len(days), "symbols": {}}
        t0, calls = time.time(), 0
        try:
            for i, sym in enumerate(symbols, 1):
                if self.stop.is_set():
                    break
                res = self.run_symbol(sym, days)
                report["symbols"][sym] = res
                calls += res["ok"] + res["empty"] + len(res["failed"])
                rate = calls / max(time.time() - t0, 1e-9) * 60
                self.log(f"[{i}/{len(symbols)}] {sym}: ok={res['ok']} empty={res['empty']} "
                         f"failed={len(res['failed'])} skipped={res['skipped']} rows={res['rows']} "
                         f"{res['bytes'] / 1e6:.1f} MB  ({rate:.0f} calls/min)")
                if report_path:
                    _write_report(report_path, report)
        except KeyboardInterrupt:
            self.stop.set()
            self.aborted = "interrupted"
        report["finished"] = datetime.now(timezone.utc).isoformat()
        report["aborted"] = self.aborted
        report["totals"] = _totals(report)
        if report_path:
            _write_report(report_path, report)
        return report


def _totals(report: dict) -> dict:
    syms = list(report["symbols"].values())
    reasons: Dict[str, int] = {}
    for s in syms:
        for r in s["failed"].values():
            key = r.split(":")[0]
            reasons[key] = reasons.get(key, 0) + 1
    return {"symbols": len(syms), "ok": sum(s["ok"] for s in syms),
            "empty": sum(s["empty"] for s in syms), "failed": sum(len(s["failed"]) for s in syms),
            "skipped": sum(s["skipped"] for s in syms), "rows": sum(s["rows"] for s in syms),
            "failure_reasons": reasons}


def _write_report(path: str, report: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1)
    os.replace(tmp, path)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols-file", action="append", required=True, help="repeatable; symbols are unioned")
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--end", default=None, help="default: the latest completed session")
    ap.add_argument("--calls-per-minute", type=int, default=60, help="this process's own cap (default 60)")
    ap.add_argument("--key-limit", type=int, default=75, help="ceiling over all processes sharing --budget-file")
    ap.add_argument("--budget-file", default=None,
                    help="shared rate-budget file (default <BA2 cache>/_av_rate_budget.json)")
    ap.add_argument("--workers", type=int, default=4, help="concurrent requests (latency hiding only)")
    ap.add_argument("--compact-every", type=int, default=200)
    ap.add_argument("--max-attempts", type=int, default=4)
    ap.add_argument("--abort-after", type=int, default=60, help="stop after N consecutive failed days")
    ap.add_argument("--limit", type=int, default=None, help="first N symbols only (a smoke run)")
    ap.add_argument("--root", default=None, help="store root (default <cache>/AlphaVantageOptionsProvider)")
    ap.add_argument("--report", default=None, help="write a JSON report (per-symbol counts, failures with reasons)")
    ap.add_argument("--dry-run", action="store_true", help="count the calls still owed; fetch nothing")
    ap.add_argument("--db", required=True,
                    help="sqlite DB holding the API key (AppSetting), e.g. ~/Documents/ba2/test/dl_forecasting.db")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    import logging
    logging.disable(logging.WARNING)
    from ba2_common.core import db as _db
    _db.configure_db(os.path.expanduser(args.db))
    import ba2_common.config as cfg
    from ba2_common.config import get_app_setting
    from ba2_providers.av_rate_budget import FileRateBudget
    from ba2_providers.options.alphavantage import AvOptionStore, fetch_chain_payload

    symbols = read_symbols(args.symbols_file)
    if args.limit:
        symbols = symbols[:args.limit]
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else latest_session()
    days = trading_days(start, end)
    store = AvOptionStore(os.path.expanduser(args.root) if args.root else None)
    print(f"{len(symbols)} symbols x {len(days)} sessions ({start}..{end}); store {store.root}", flush=True)

    if args.dry_run:
        owed = sum(len(store.pending_days(s, days)) for s in symbols)
        print(f"calls owed: {owed:,} (~{owed / max(args.calls_per_minute, 1) / 60 / 24:.1f} days at "
              f"{args.calls_per_minute}/min)")
        return 0

    api_key = get_app_setting("alpha_vantage_api_key")
    if not api_key:
        print("alpha_vantage_api_key is not set in the AppSettings of --db", file=sys.stderr)
        return 2
    import requests
    session = requests.Session()
    budget = FileRateBudget(args.budget_file or os.path.join(cfg.CACHE_FOLDER, "_av_rate_budget.json"),
                            owner=f"av_options:{os.getpid()}", per_minute=args.calls_per_minute,
                            key_limit=args.key_limit)

    def fetch(symbol: str, day: date):
        return fetch_chain_payload(symbol, day, api_key=api_key, session=session)

    runner = Runner(store, fetch, budget, workers=args.workers, compact_every=args.compact_every,
                    max_attempts=args.max_attempts, abort_after=args.abort_after)
    report = runner.run(symbols, days, args.report,
                        meta={"start": start.isoformat(), "end": end.isoformat(),
                              "calls_per_minute": args.calls_per_minute})
    print(json.dumps(report["totals"]))
    if report["aborted"]:
        print(f"ABORTED: {report['aborted']}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
