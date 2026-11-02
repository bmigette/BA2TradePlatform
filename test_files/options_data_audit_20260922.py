"""Read-only, bounded Alpaca capability probe for the 2026-09-22 data audit.

No orders, account changes, or application imports. Credentials stay in memory;
only allowlisted data fields and HTTP status codes are written to the report.
Run with --api to opt into the market-data requests.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

import requests


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "reports/review_evidence/options-data-2026-09-22/alpaca-capabilities.json"
DATABASES = {
    "prod8081": Path.home() / "Documents/ba2_trade_platform-prod/db.sqlite",
    "options8082": Path.home() / "Documents/ba2_trade_platform-opt/db.sqlite",
}


def setting_value(row):
    _, text_value, json_value, numeric_value = row
    if text_value is not None:
        return text_value
    if json_value is not None:
        return json.loads(json_value)
    return numeric_value


def get_json(session, base, path, params, observations, label):
    try:
        response = session.get(base + path, params=params, timeout=20)
        observations[label] = {"status": response.status_code}
        if response.status_code != 200:
            return None
        return response.json()
    except Exception as exc:
        observations[label] = {"error_type": type(exc).__name__}
        return None


def present_fields(value):
    return sorted(k for k, v in value.items() if v is not None) if isinstance(value, dict) else []


def snapshot_summary(symbol, snap):
    from alpaca.data.models import OptionsSnapshot

    parsed = OptionsSnapshot(symbol, snap)
    return {
        "snapshot_fields": present_fields(snap),
        "greek_fields": present_fields(snap.get("greeks")),
        "quote_fields": present_fields(snap.get("latestQuote")),
        "trade_fields": present_fields(snap.get("latestTrade")),
        "quote_timestamp": (snap.get("latestQuote") or {}).get("t"),
        "trade_timestamp": (snap.get("latestTrade") or {}).get("t"),
        "implied_volatility": snap.get("impliedVolatility"),
        "daily_bar": {k: (snap.get("dailyBar") or {}).get(k) for k in ("t", "v", "vw", "n")},
        "previous_daily_bar": {k: (snap.get("prevDailyBar") or {}).get(k) for k in ("t", "v", "vw", "n")},
        "installed_sdk_keeps_daily_bar": hasattr(parsed, "daily_bar"),
        "installed_sdk_keeps_previous_daily_bar": hasattr(parsed, "previous_daily_bar"),
    }


def probe(label, db_path, use_api):
    connection = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
    try:
        accounts = connection.execute(
            "SELECT id FROM accountdefinition WHERE provider = 'Alpaca'"
        ).fetchall()
        result = {"accounts": [], "iv_history_rows": connection.execute(
            "SELECT count(*) FROM option_iv_snapshot"
        ).fetchone()[0]}
        for (account_id,) in accounts:
            rows = connection.execute(
                "SELECT key,value_str,value_json,value_float FROM accountsetting "
                "WHERE account_id=? AND key IN "
                "('api_key','api_secret','paper_account','options_feed','data_feed')",
                (account_id,),
            ).fetchall()
            settings = {r[0]: setting_value(r) for r in rows}
            paper = str(settings["paper_account"]).lower() == "true"
            account_result = {
                "account_id": account_id,
                "paper": paper,
                "options_feed": settings["options_feed"],
                "stock_feed": settings["data_feed"],
            }
            result["accounts"].append(account_result)
            if not use_api:
                continue
            with requests.Session() as session:
                session.headers.update({
                    "APCA-API-KEY-ID": settings["api_key"],
                    "APCA-API-SECRET-KEY": settings["api_secret"],
                })
                market = "https://data.alpaca.markets"
                trading = "https://paper-api.alpaca.markets" if paper else "https://api.alpaca.markets"
                calls = account_result["requests"] = {}
                spot_json = get_json(session, market, "/v2/stocks/SPY/quotes/latest",
                                     {"feed": "iex"}, calls, "underlying_quote")
                if not spot_json:
                    continue
                quote = spot_json.get("quote") or {}
                bid, ask = quote.get("bp"), quote.get("ap")
                if not isinstance(bid, (float, int)) or not isinstance(ask, (float, int)) or min(bid, ask) <= 0:
                    account_result["probe_note"] = "No usable underlying quote; contract probe skipped."
                    continue
                spot = (bid + ask) / 2
                today = datetime.now(timezone.utc).date()
                contracts_json = get_json(session, trading, "/v2/options/contracts", {
                    "underlying_symbols": "SPY", "type": "call", "limit": 2,
                    "expiration_date_gte": (today + timedelta(days=20)).isoformat(),
                    "expiration_date_lte": (today + timedelta(days=45)).isoformat(),
                    "strike_price_gte": round(spot * 0.99, 2),
                    "strike_price_lte": round(spot * 1.01, 2),
                }, calls, "contracts")
                if not contracts_json:
                    continue
                contracts = contracts_json.get("option_contracts") or []
                account_result["contracts"] = [
                    {k: c.get(k) for k in (
                        "symbol", "expiration_date", "strike_price", "type", "style", "size",
                        "tradable", "open_interest", "open_interest_date", "close_price", "close_price_date"
                    )} for c in contracts
                ]
                if not contracts:
                    continue
                symbols = ",".join(c["symbol"] for c in contracts)
                for feed in ("indicative", "opra"):
                    snapshot_json = get_json(session, market, "/v1beta1/options/snapshots",
                                             {"symbols": symbols, "feed": feed}, calls, f"snapshots_{feed}")
                    if snapshot_json is None:
                        continue
                    calls[f"snapshots_{feed}"]["contracts"] = {
                        symbol: snapshot_summary(symbol, snap)
                        for symbol, snap in (snapshot_json.get("snapshots") or {}).items()
                    }
                bars_json = get_json(session, market, "/v1beta1/options/bars", {
                    "symbols": symbols, "timeframe": "1Day", "limit": 20,
                    "start": (today - timedelta(days=10)).isoformat(), "end": today.isoformat(),
                }, calls, "historical_daily_bars")
                if bars_json is not None:
                    calls["historical_daily_bars"]["contracts"] = {
                        symbol: {"count": len(bars), "sample_fields": present_fields(bars[-1]) if bars else [],
                                 "sample_volume": bars[-1].get("v") if bars else None,
                                 "sample_timestamp": bars[-1].get("t") if bars else None}
                        for symbol, bars in (bars_json.get("bars") or {}).items()
                    }
        return result
    finally:
        connection.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", action="store_true")
    args = parser.parse_args()
    output = {"observed_at_utc": datetime.now(timezone.utc).isoformat(), "api_requested": args.api,
              "instances": {label: probe(label, path, args.api) for label, path in DATABASES.items()}}
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output, indent=2))
