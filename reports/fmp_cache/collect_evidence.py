"""Read-only FMP bandwidth investigation. Never imports providers or contacts APIs."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date
import json
from pathlib import Path
import re
import sqlite3

ROOT = Path(__file__).resolve().parents[2]
DATA = Path.home() / "Documents"
INSTANCES = {"dev": DATA / "ba2/trade", "prod": DATA / "ba2_trade_platform-prod",
             "test": DATA / "ba2/test"}


def log_evidence():
    result = {}
    fetch = re.compile(r"Fetching FMP OHLCV data for (\S+) from (\d{4}-\d\d-\d\d) to (\d{4}-\d\d-\d\d) with interval (\S+)")
    bulk = re.compile(r"StockScreener: bulk OHLCV fetched (\d+)/(\d+) symbols \((\d{4}-\d\d-\d\d) to (\d{4}-\d\d-\d\d)\)")
    received = re.compile(r"Retrieved (\d+) bars from FMP for (\S+)")
    for name, root in INSTANCES.items():
        daily = defaultdict(Counter)
        calls, sweeps, starts, samples, files = [], [], [], [], []
        seen = set()
        log_files = set()
        for pattern in ("all.debug.log*", "app.debug.log*", "app.log*"):
            log_files.update((root / "logs").glob(pattern))
        for path in sorted(log_files):
            first = last = None
            with path.open(encoding="utf-8", errors="replace") as handle:
                for number, line in enumerate(handle, 1):
                    if not re.match(r"2026-\d\d-\d\d ", line):
                        continue
                    if first is None:
                        first = line[:23]
                    last = line[:23]
                    if line[:10] < "2026-08-10":
                        continue
                    m_fetch, m_bulk, m_received = fetch.search(line), bulk.search(line), received.search(line)
                    interesting = (m_fetch or m_bulk or m_received or "StockScreener: pipeline complete" in line
                                   or "Failed to refresh parquet cache" in line or "Using cache folder:" in line
                                   or "Creating new expert instance for expert " in line
                                   or "Cleared all expert instance cache" in line)
                    if not interesting or line in seen:
                        continue
                    seen.add(line)
                    row = {"timestamp": line[:23], "file": str(path), "line": number}
                    day = daily[line[:10]]
                    if m_fetch:
                        symbol, lo, hi, interval = m_fetch.groups()
                        span = (date.fromisoformat(hi) - date.fromisoformat(lo)).days
                        row.update(symbol=symbol, start=lo, end=hi, interval=interval, span_days=span)
                        calls.append(row)
                        day["provider_fetches_" + interval] += 1
                        if interval == "1d":
                            day["deep_daily_fetches" if span > 1000 else "tail_daily_fetches"] += 1
                    elif m_bulk:
                        got, requested, lo, hi = m_bulk.groups()
                        row.update(returned_symbols=int(got), requested_symbols=int(requested), start=lo, end=hi)
                        sweeps.append(row)
                        day["screener_bulk_invocations"] += 1
                        day["screener_symbol_windows"] += int(requested)
                    elif m_received:
                        day["provider_returned_bars"] += int(m_received.group(1))
                    elif "pipeline complete" in line:
                        day["screens_completed"] += 1
                    elif "Failed to refresh parquet cache" in line:
                        day["refresh_failures"] += 1
                    elif "Using cache folder:" in line:
                        row["cache_folder"] = line.split("Using cache folder:", 1)[1].strip()
                        starts.append(row)
                        day["app_startups"] += 1
                    elif "Creating new expert instance for expert " in line:
                        expert_id = line.rsplit("expert ", 1)[1].strip()
                        day["expert_recreated_" + expert_id] += 1
                    elif "Cleared all expert instance cache" in line:
                        day["expert_cache_cleared"] += 1
            files.append({"path": str(path), "bytes": path.stat().st_size, "first": first, "last": last})
        repeated = Counter((r["timestamp"][:10], r["symbol"], r["start"], r["end"], r["interval"]) for r in calls)
        result[name] = {"files": files, "daily": {d: dict(c) for d, c in sorted(daily.items())},
                        "provider_calls": calls, "screener_bulk_invocations": sweeps, "startup_paths": starts,
                        "repeated_provider_windows": [{"day_symbol_window": list(k), "count": v}
                                                      for k, v in repeated.most_common(20) if v > 1]}
    return result


def db_evidence():
    paths = {"dev": INSTANCES["dev"] / "db.sqlite",
             "dev_before_reset": INSTANCES["dev"] / "db.sqlite.bak-pre-testreset-20260904-111605",
             "prod": INSTANCES["prod"] / "db.sqlite"}
    result = {}
    keys = {"instrument_selection_method", "execution_schedule_enter_market", "execution_schedule_open_positions",
            "enabled_instruments", "skill_horizon_days", "skill_lookback_months", "skill_min_past_trades",
            "skill_max_past_trades", "min_trader_avg_hold_days", "max_disclose_date_days", "max_trade_exec_days"}
    for name, path in paths.items():
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            experts = [dict(r) for r in db.execute("SELECT id, account_id, expert, enabled FROM expertinstance ORDER BY id")]
            for expert in experts:
                settings = {}
                for row in db.execute("SELECT key, value_str, value_json, value_float FROM expertsetting WHERE instance_id=?", (expert["id"],)):
                    if row["key"] not in keys and not row["key"].startswith("screener_"):
                        continue
                    # Preserve all typed columns. value_json often holds a default
                    # empty object even for scalar settings; it must not shadow them.
                    settings[row["key"]] = {k: row[k] for k in ("value_str", "value_float", "value_json")}
                expert["settings"] = settings
            activity = [dict(r) for r in db.execute("""SELECT substr(created_at,1,10) AS day,
                expert_instance_id, subtype, count(*) AS analyses FROM marketanalysis
                WHERE created_at >= '2026-08-10' GROUP BY day, expert_instance_id, subtype ORDER BY day, expert_instance_id""")]
            result[name] = {"database": str(path), "experts": experts, "analysis_counts": activity}
    return result


def cache_evidence():
    caches = {"dev_shared": DATA / "ba2/common/cache", "prod": DATA / "ba2_trade_platform-prod/cache"}
    output = {}
    for name, root in caches.items():
        pq = list((root / "FMPOHLCVProvider").glob("*.parquet"))
        csv = list((root / "FMPOHLCVProvider").glob("*.csv"))
        hist = list((root / "fmp_history").glob("historical_price_full__*.json"))
        aliases = {p.stem[:-3]: p for p in pq if p.stem.endswith("_5m")}
        conflicts = [s for s in aliases if (root / "FMPOHLCVProvider" / (s + "_5min.parquet")).is_file()]
        dates = Counter()
        for p in hist:
            from datetime import datetime
            dates[datetime.fromtimestamp(p.stat().st_mtime).date().isoformat()] += 1
        output[name] = {"root": str(root), "parquet_files": len(pq), "parquet_bytes": sum(p.stat().st_size for p in pq),
                        "csv_files": len(csv), "historical_price_full_json_files": len(hist),
                        "historical_price_full_json_bytes": sum(p.stat().st_size for p in hist),
                        "history_json_mtime_counts": dict(sorted(dates.items())), "intraday_alias_conflicts": conflicts}
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--part", choices=("logs", "db", "cache"), required=True)
    args = parser.parse_args()
    result = {"logs": log_evidence, "db": db_evidence, "cache": cache_evidence}[args.part]()
    output = Path(__file__).parent / f"{args.part}_evidence_2026-09-08.json"
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {args.part} evidence: {output}")
    if args.part == "logs":
        print(json.dumps({k: v["daily"] for k, v in result.items()}, indent=2))
    elif args.part == "db":
        print(json.dumps({k: {"experts": len(v["experts"]), "enabled": sum(bool(e["enabled"]) for e in v["experts"]),
                             "by_class": dict(Counter(e["expert"] for e in v["experts"] if e["enabled"]))}
                          for k, v in result.items()}, indent=2))
    else:
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
