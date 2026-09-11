"""Read-only Sep 10 replay inventory. Never imports the app or calls a provider.

Run with Python and pyarrow installed. Outputs are scoped beside this script.
The cache files themselves are inspected, not modified or copied.
"""
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import re
import sqlite3

import pyarrow.compute as pc
import pyarrow.parquet as pq

DAY = "2026-09-10"
PROD = Path("C:/Users/basti/Documents/ba2_trade_platform-prod")
SHARED = Path("C:/Users/basti/Documents/ba2/common/cache")
OUT = Path(__file__).resolve().parent


def rows(db, sql, params=()):
    return [dict(r) for r in db.execute(sql, params)]


def dump(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2, default=str) + "\n", encoding="utf-8")


def parquet_inventory(root, symbol, interval):
    aliases = {"1d": ("1d", "1day", "daily"), "5m": ("5m", "5min")}[interval]
    candidates = [root / "FMPOHLCVProvider" / f"{symbol}_{s}.parquet" for s in aliases]
    existing = [p for p in candidates if p.exists()]
    result = {"symbol": symbol, "interval": interval, "exists": bool(existing),
              "aliases": [str(p) for p in existing]}
    if not existing:
        return result
    p = existing[0]  # Same canonical-first precedence as the backtest reader.
    result.update(path=str(p), bytes=p.stat().st_size,
                  modified_utc=datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat())
    try:
        f = pq.ParquetFile(p)
        name = next(n for n in f.schema.names
                    if n.lower() in ("date", "datetime", "timestamp", "__index_level_0__"))
        bounds = pc.min_max(f.read(columns=[name]).column(0)).as_py()
        result.update(rows=f.metadata.num_rows, first=str(bounds["min"]), last=str(bounds["max"]),
                      reaches_day=str(bounds["max"])[:10] >= DAY)
    except Exception as exc:
        result["error"] = str(exc)
    return result


def history_inventory(root, namespace, symbol):
    p = root / "fmp_history" / f"{namespace}__{symbol}.json"
    result = {"namespace": namespace, "symbol": symbol, "exists": p.exists()}
    if not p.exists():
        return result
    result.update(path=str(p), bytes=p.stat().st_size,
                  modified_utc=datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat())
    try:
        content = json.loads(p.read_text(encoding="utf-8"))
        result.update(empty=not bool(content), sha256=hashlib.sha256(p.read_bytes()).hexdigest())
        if isinstance(content, list):
            result["rows"] = len(content)
            dates = [str(r[k]) for r in content if isinstance(r, dict)
                     for k in ("date", "publishedDate", "filingDate", "fillingDate", "acceptedDate")
                     if r.get(k)]
            result["latest_payload_date"] = max(dates) if dates else None
    except Exception as exc:
        result["error"] = str(exc)
    return result


def main():
    db = sqlite3.connect(f"file:{(PROD / 'db.sqlite').as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    db.execute("BEGIN")
    experts = rows(db, "SELECT * FROM expertinstance WHERE enabled=1")
    ids = [e["id"] for e in experts]
    marks = ",".join("?" for _ in ids)
    analyses = rows(db, "SELECT * FROM marketanalysis WHERE created_at>=? AND created_at<?",
                    (DAY, "2026-09-11"))
    settings = rows(db, f"SELECT * FROM expertsetting WHERE instance_id IN ({marks})", ids)
    settings = [s for s in settings if not re.search(r"api.?key|secret|password|token", s["key"], re.I)]
    txns = rows(db, f"SELECT * FROM 'transaction' WHERE expert_id IN ({marks})", ids)
    recs = rows(db, "SELECT * FROM expertrecommendation WHERE created_at>=? AND created_at<?",
                (DAY, "2026-09-11"))
    outputs = rows(db, "SELECT a.id,a.market_analysis_id,a.name,a.type,a.text,a.provider_category,"
                   "a.provider_name,a.provider_metadata,a.format_type FROM analysisoutput a "
                   "JOIN marketanalysis m ON m.id=a.market_analysis_id "
                   "WHERE m.created_at>=? AND m.created_at<?", (DAY, "2026-09-11"))
    rulesets = rows(db, "SELECT * FROM ruleset WHERE id IN (SELECT enter_market_ruleset_id "
                    "FROM expertinstance WHERE enabled=1 UNION SELECT open_positions_ruleset_id "
                    "FROM expertinstance WHERE enabled=1)")
    links = rows(db, "SELECT l.*, e.name,e.triggers,e.actions,e.extra_parameters,e.continue_processing "
                 "FROM ruleset_eventaction_link l JOIN eventaction e ON e.id=l.eventaction_id "
                 "WHERE l.ruleset_id IN (SELECT enter_market_ruleset_id FROM expertinstance WHERE enabled=1 "
                 "UNION SELECT open_positions_ruleset_id FROM expertinstance WHERE enabled=1) "
                 "ORDER BY l.ruleset_id,l.order_index")
    orders = rows(db, "SELECT id,account_id,symbol,quantity,side,order_type,status,filled_qty,"
                  "filled_avg_price,open_price,limit_price,stop_price,created_at,expert_recommendation_id,"
                  "transaction_id,depends_on_order,parent_order_id,data,broker_order_id IS NOT NULL AS broker_sent "
                  "FROM tradingorder WHERE account_id=1 AND created_at>='2026-08-01' ORDER BY id")
    cache_count = db.execute("SELECT count(*) FROM provider_cache").fetchone()[0]
    db.rollback()
    db.close()
    selected_logs = [line for line in (PROD / "logs/app.log").read_text(encoding="utf-8").splitlines()
                     if line.startswith(DAY) and ("StockScreener LIVE SELECTION:" in line
                         or "Capital mapping:" in line or "Inputs: price=" in line)]
    snapshot = dict(captured_at_utc=datetime.now(timezone.utc).isoformat(), day=DAY,
                    production_read_only=True, experts=experts, expert_settings=settings,
                    analyses=analyses, recommendations=recs, analysis_outputs=outputs,
                    transactions_current_state=txns, orders=orders, rulesets=rulesets,
                    ordered_eventactions=links, selected_logs=selected_logs,
                    provider_cache_rows=cache_count,
                    limitations=["Current transaction state is not an opening-bell snapshot.",
                                 "No account credentials or broker identifiers exported.",
                                 "No raw provider bundles invented from narrative output."])
    dump("live_inputs.json", snapshot)
    symbols_by_expert = {e["id"]: sorted({a["symbol"] for a in analyses
                                         if a["expert_instance_id"] == e["id"]}) for e in experts}
    by_type = {}
    for e in experts:
        by_type.setdefault(e["expert"], set()).update(symbols_by_expert[e["id"]])
    for expert, symbols in by_type.items():
        (OUT / f"symbols_{expert}.txt").write_text("\n".join(sorted(symbols)) + "\n", encoding="utf-8")
    symbols = sorted(set().union(*by_type.values()) | {"SPY"})
    (OUT / "symbols_all.txt").write_text("\n".join(symbols) + "\n", encoding="utf-8")
    namespaces = {
        "FMPRating": ["grades_historical", "price_target"],
        "FMPEarningsDrift": ["past_earnings_quarterly"],
        "FMPInsiderClusterBuy": ["insider_v2"],
        "DeterministicScorer": ["balance_sheet_annual", "income_statement_annual", "cashflow_statement_annual",
                                "grades_historical", "price_target", "past_earnings_quarterly"],
    }
    def setting(instance_id, key):
        match = next(s for s in settings if s["instance_id"] == instance_id and s["key"] == key)
        for col in ("value_str", "value_float"):
            if match[col] is not None:
                return match[col]
        return json.loads(match["value_json"])

    needs = set()
    for expert in experts:
        syms = symbols_by_expert[expert["id"]]
        required = list(namespaces[expert["expert"]])
        if expert["expert"] == "FMPRating" and setting(expert["id"], "max_analyst_age_months") > 0:
            required.append("analyst_grades")
        if expert["expert"] in ("FMPEarningsDrift", "FMPInsiderClusterBuy"):
            if setting(expert["id"], "expected_profit_mode") == "model":
                required.extend(("past_earnings_quarterly", "earnings_estimates_quarterly"))
        needs.update((ns, sym) for ns in required for sym in syms)
    needs = sorted(needs)
    dump("history_prewarm_manifest.json", {ns: sorted(sym for n, sym in needs if n == ns)
                                          for ns in sorted({n for n, _ in needs})})
    inventory = {"captured_at_utc": snapshot["captured_at_utc"], "day": DAY,
                 "symbols_by_expert": symbols_by_expert, "symbols_with_benchmark": len(symbols),
                 "required_history_keys": len(needs), "roots": {}}
    for label, root in (("production", PROD / "cache"), ("shared", SHARED)):
        bars = [parquet_inventory(root, sym, iv) for sym in symbols for iv in ("1d", "5m")]
        history = [history_inventory(root, ns, sym) for ns, sym in needs]
        inventory["roots"][label] = dict(path=str(root), bars=bars, history=history,
            bar_summary={iv: dict(total=len(symbols), present=sum(x["exists"] for x in bars if x["interval"] == iv),
                reaches_day=sum(x.get("reaches_day", False) for x in bars if x["interval"] == iv)) for iv in ("1d", "5m")},
            history_summary=dict(total=len(needs), present=sum(x["exists"] for x in history),
                empty=sum(x.get("empty", False) for x in history),
                refreshed_on_day=sum(x.get("modified_utc", "")[:10] >= DAY for x in history)))
    dump("cache_inventory.json", inventory)
    print(json.dumps({"analyses": len(analyses), "recommendations": len(recs), "outputs": len(outputs),
                      "symbols": len(symbols), "by_expert": {str(k): len(v) for k,v in symbols_by_expert.items()},
                      "history_keys": len(needs),
                      "coverage": {k: {j: v[j] for j in ("bar_summary", "history_summary")}
                                   for k,v in inventory["roots"].items()}}, indent=2))


if __name__ == "__main__":
    main()
