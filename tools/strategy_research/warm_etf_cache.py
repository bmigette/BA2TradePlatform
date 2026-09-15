#!/usr/bin/env python
"""Preview, check or warm ETFTrend's daily and five-minute FMP cache.

Default is an offline preview. --check reads cache only; --run fetches missing
sessions in small resumable chunks. No backtests, application DB writes or orders.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta, timezone
import os
from pathlib import Path
import re
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.strategy_research.profiles import build_manifest
from tools.strategy_research.runtime import add_source_paths, job_lock, write_json


def build_plan(start, end, symbols):
    manifest = build_manifest(families=["etf_trend"], start=start, end=end, etf_symbols=symbols)
    bt = manifest["jobs"][0]["optimization_config"]["backtest"]
    if date.fromisoformat(end) >= datetime.now(timezone.utc).date():
        raise ValueError("End must be a completed historical date (before today)")
    # The engine preloads both intervals over start - warmup_days through end.
    first = date.fromisoformat(bt["start_date"]) - timedelta(days=bt["warmup_days"])
    return {"symbols": bt["enabled_instruments"], "backtest_start": start,
            "end": end, "fetch_start": first.isoformat(), "warmup_days": bt["warmup_days"],
            "intervals": ["1d", bt["execution_interval"]],
            "coverage_contract": "Every NYSE session must have at least 1 daily row or its regular-session count of five-minute rows. This does not certify each intraday timestamp."}


def read_key(settings_db):
    if "FMP_API_KEY" in os.environ and os.environ["FMP_API_KEY"].strip():
        return os.environ["FMP_API_KEY"].strip()
    path = Path(settings_db).expanduser().resolve()
    if not path.is_file():
        raise ValueError("Set FMP_API_KEY or point --settings-db at an existing settings database")
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute("SELECT value_str FROM appsetting WHERE key = ?", ("FMP_API_KEY",)).fetchone()
    if row is None or not row[0] or not row[0].strip():
        raise ValueError("FMP_API_KEY is absent from the selected settings database")
    return row[0].strip()


def configure_runtime(cache_dir):
    """Called only in this CLI process, before importing any provider modules."""
    os.environ["CACHE_FOLDER"] = str(cache_dir.parent)
    # Provider exceptions can contain request URLs. This tool owns progress and
    # sanitized errors instead of writing raw provider tracebacks to shared logs.
    os.environ["BA2_FILE_LOGGING"] = "0"
    os.environ["BA2_STDOUT_LOGGING"] = "0"
    add_source_paths()
    from ba2_common.core import native_cache
    if Path(native_cache.CACHE_FOLDER).resolve() != cache_dir.parent:
        raise RuntimeError("Cache configuration was already imported; run this script in a fresh process")


def read_cache(cache_dir, symbol, interval):
    import pandas as pd
    # The engine prefers the short alias. Refuse conflicting files rather than
    # extending a long-name file shadowed by another cache on read.
    paths = [cache_dir / f"{symbol}_{interval}.parquet"]
    if interval == "5min":
        paths.append(cache_dir / f"{symbol}_5m.parquet")
    present = [p for p in paths if p.is_file()]
    if len(present) > 1:
        raise ValueError(f"Conflicting interval aliases for {symbol}: {[p.name for p in present]}")
    if not present:
        return pd.DataFrame(columns=["Date", "Open", "High", "Low", "Close", "Volume"])
    return pd.read_parquet(present[0])


def validated(frame):
    import numpy as np
    import pandas as pd
    required = ["Date", "Open", "High", "Low", "Close", "Volume"]
    if not set(required) <= set(frame.columns):
        raise ValueError("OHLCV columns are missing")
    frame = frame.copy()
    frame["Date"] = pd.to_datetime(frame["Date"], utc=True, errors="raise")
    if frame["Date"].isna().any() or frame["Date"].duplicated().any():
        raise ValueError("Invalid or duplicate OHLCV timestamps")
    for column in required[1:]:
        values = pd.to_numeric(frame[column], errors="raise")
        if not np.isfinite(values).all() or (values < 0 if column == "Volume" else values <= 0).any():
            raise ValueError(f"Invalid OHLCV {column}")
        frame[column] = values
    if ((frame["High"] < frame[["Open", "Close", "Low"]].max(axis=1)) |
            (frame["Low"] > frame[["Open", "Close", "High"]].min(axis=1))).any():
        raise ValueError("Inconsistent OHLCV high/low bounds")
    return frame.sort_values("Date").reset_index(drop=True)


def session_requirements(plan, interval):
    from ba2_common.core.market_calendar import nyse_regular_sessions
    sessions = nyse_regular_sessions(date.fromisoformat(plan["fetch_start"]), date.fromisoformat(plan["end"]))
    if not sessions:
        raise ValueError("Requested history contains no NYSE sessions")
    return {opened.date(): 1 if interval == "1d" else int((closed - opened).total_seconds() // 300)
            for opened, closed in sessions}


def coverage(frame, required):
    frame = validated(frame)
    counts = frame.groupby(frame["Date"].dt.date).size().to_dict()
    # A count check is deliberately explicit: extended-hours rows can hide a
    # missing regular-session bar. This is not a claim of slot-by-slot coverage.
    missing = [day for day, count in required.items() if counts.get(day, 0) < count]
    return {"rows": len(frame), "required_sessions": len(required),
            "covered_sessions": len(required) - len(missing),
            "missing_or_short_sessions": [d.isoformat() for d in missing]}


def windows(missing, interval):
    """Small inclusive requests avoid FMP's intraday response-size truncation."""
    days = sorted(date.fromisoformat(d) for d in missing)
    width = 366 if interval == "1d" else 3
    while days:
        first = days[0]
        included = [d for d in days if d < first + timedelta(days=width)]
        last = included[-1]
        yield first, last
        days = days[len(included):]


def save_frame(cache_dir, symbol, interval, frame):
    from ba2_common.core import native_cache
    if Path(native_cache.CACHE_FOLDER).resolve() != cache_dir.parent:
        raise RuntimeError("Refusing to write outside the selected cache directory")
    out = validated(frame)
    out["effective_date"] = out["Date"]
    native_cache.write_timeseries("FMPOHLCVProvider", symbol, interval, out)


def warm_pair(plan, cache_dir, symbol, interval, provider):
    """Save every valid chunk; incomplete/empty responses remain visible and retryable."""
    import pandas as pd
    required = session_requirements(plan, interval)
    frame = read_cache(cache_dir, symbol, interval)
    before = coverage(frame, required)
    fetched = 0
    for first, last in windows(before["missing_or_short_sessions"], interval):
        print(f"  Fetch {symbol}/{interval}: {first}..{last}", flush=True)
        new = provider._get_ohlcv_data_impl(
            symbol, datetime.combine(first, time.min, timezone.utc),
            datetime.combine(last, time.max, timezone.utc), interval)
        new = validated(new)
        new = new[(new["Date"].dt.date >= first) & (new["Date"].dt.date <= last)]
        if new.empty:
            raise ValueError(f"FMP returned no bars for {symbol}/{interval} {first}..{last}")
        # Reread before each merge. Keep history outside the requested window and
        # replace overlap only with actual returned data, never invented candles.
        old = validated(read_cache(cache_dir, symbol, interval))
        merged = new if old.empty else pd.concat([old, new], ignore_index=True).drop_duplicates("Date", keep="last")
        save_frame(cache_dir, symbol, interval, merged)
        fetched += 1
        chunk_required = {d: n for d, n in required.items() if first <= d <= last}
        remaining = coverage(merged, chunk_required)["missing_or_short_sessions"]
        if remaining:
            raise ValueError(f"Incomplete FMP response for {symbol}/{interval}: {remaining[:8]}; rerun retries these sessions")
    after = coverage(read_cache(cache_dir, symbol, interval), required)
    return {**after, "fetched_chunks": fetched,
            "status": "passed" if not after["missing_or_short_sessions"] else "failed"}


def clean_error(exc, key):
    message = str(exc)
    if key:
        message = message.replace(key, "[REDACTED]")
    return re.sub(r"(?i)(apikey|api_key)=([^&\s]+)", r"\1=[REDACTED]", message)


def parser():
    ap = argparse.ArgumentParser(description=__doc__)
    modes = ap.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true", help="Offline plan only (default)")
    modes.add_argument("--check", action="store_true", help="Read and report existing cache coverage")
    modes.add_argument("--run", action="store_true", help="Download missing cache sections from FMP")
    ap.add_argument("--start", default="2020-01-01", help="Backtest start; warmup is added automatically")
    ap.add_argument("--end", default="2025-12-31", help="Inclusive, completed historical date")
    ap.add_argument("--symbols", nargs="+", default=["SPY", "IEF", "TLT", "GLD"])
    home = Path(os.environ["BA2_HOME"]).expanduser() if "BA2_HOME" in os.environ else Path.home() / "Documents/ba2"
    ap.add_argument("--cache-dir", type=Path, default=home / "common/cache/FMPOHLCVProvider")
    ap.add_argument("--settings-db", type=Path, default=home / "test/dl_forecasting.db",
                    help="Read-only key source when FMP_API_KEY is not in the environment")
    ap.add_argument("--output-dir", type=Path, default=ROOT / "reports/strategy_research/etf-warmup")
    return ap


def main(argv=None):
    args = parser().parse_args(argv)
    key = None
    try:
        plan = build_plan(args.start, args.end, args.symbols)
        cache_dir, output = args.cache_dir.expanduser().resolve(), args.output_dir.expanduser().resolve()
        if cache_dir.name != "FMPOHLCVProvider":
            raise ValueError("--cache-dir must be the FMPOHLCVProvider directory, as in the research driver")
        if output == ROOT:
            raise ValueError("Use an output subdirectory, not the repository root")
        write_json(output / "plan.json", {**plan, "cache_dir": str(cache_dir)})
        print(f"ETF universe: {', '.join(plan['symbols'])}; {plan['fetch_start']}..{plan['end']} (includes {plan['warmup_days']} warmup days)")
        print(f"Intervals: {', '.join(plan['intervals'])}\nCache: {cache_dir}")
        if not (args.check or args.run):
            print(f"Preview only. Use --check or --run. Plan: {output / 'plan.json'}")
            return 0
        configure_runtime(cache_dir)
        report = {"plan": plan, "mode": "run" if args.run else "check", "results": []}
        provider = None
        for symbol in plan["symbols"]:
            for interval in plan["intervals"]:
                try:
                    required = session_requirements(plan, interval)
                    state = coverage(read_cache(cache_dir, symbol, interval), required)
                    if args.run and state["missing_or_short_sessions"]:
                        if provider is None:
                            key = read_key(args.settings_db)
                            from ba2_providers.ohlcv.FMPOHLCVProvider import FMPOHLCVProvider
                            provider = FMPOHLCVProvider(api_key=key)
                        # Coordinates this tool's processes for a given symbol;
                        # native_cache also performs atomic per-file writes.
                        with job_lock(cache_dir / f".{symbol}-etf-warmup.lock"):
                            state = warm_pair(plan, cache_dir, symbol, interval, provider)
                    state["status"] = "failed" if state["missing_or_short_sessions"] else "passed"
                except Exception as exc:
                    state = {"status": "failed", "error": clean_error(exc, key)}
                record = {"symbol": symbol, "interval": interval, **state}
                report["results"].append(record)
                write_json(output / "coverage.json", report)
                print(f"{symbol}/{interval}: {state['status']}" + (f" - {state['error']}" if "error" in state else
                      f" ({state['covered_sessions']}/{state['required_sessions']} sessions)"), flush=True)
        failed = sum(r["status"] != "passed" for r in report["results"])
        print(f"{failed} failed pair(s). Report: {output / 'coverage.json'}")
        return 1 if failed else 0
    except Exception as exc:
        print(f"ETF warmup: {clean_error(exc, key)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
