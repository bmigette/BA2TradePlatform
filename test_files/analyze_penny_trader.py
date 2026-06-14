#!/usr/bin/env python3
"""
Comprehensive analysis of PennyMomentumTrader (expert 17) trades.
Pulls transaction data, OHLCV prices, and examines patterns to identify
improvements for avoiding losers and catching missed opportunities.
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

DB_PATH = os.path.expanduser("~/Documents/ba2_trade_platform/db.sqlite")
EXPERT_ID = 17

# Try to use FMP for price data (the same provider the expert uses)
FMP_API_KEY = os.environ.get("FMP_API_KEY") or os.environ.get("FINNHUB_API_KEY", "")


def load_env():
    """Load .env file if present."""
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def get_fmp_api_key():
    """Get FMP API key from environment."""
    load_env()
    # Check common env var names
    for key_name in ["FMP_API_KEY", "FMP_KEY"]:
        val = os.environ.get(key_name)
        if val:
            return val
    # Try to get from DB settings
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute(
        "SELECT value_str FROM appsetting WHERE key = 'fmp_api_key'"
    )
    row = cursor.fetchone()
    conn.close()
    if row and row[0]:
        return row[0]
    return None


def _ssl_context():
    """Create an SSL context that works on macOS."""
    import ssl
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())
    except ImportError:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _parse_dt(s):
    """Parse a datetime string with various formats."""
    if not s:
        return None
    s = str(s)
    for fmt in [
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
    ]:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    # Last resort
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def fetch_fmp_historical(symbol, from_date, to_date, api_key):
    """Fetch daily OHLCV from FMP."""
    import urllib.request
    url = (
        f"https://financialmodelingprep.com/api/v3/historical-price-full/{symbol}"
        f"?from={from_date}&to={to_date}&apikey={api_key}"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "BA2Analyzer/1.0"})
        with urllib.request.urlopen(req, timeout=15, context=_ssl_context()) as resp:
            data = json.loads(resp.read())
        hist = data.get("historical", [])
        if not hist:
            return None
        df = pd.DataFrame(hist)
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        return df
    except Exception as e:
        print(f"  FMP fetch failed for {symbol}: {e}")
        return None


def fetch_fmp_intraday(symbol, date_str, api_key, interval="5min"):
    """Fetch intraday data from FMP for a specific date."""
    import urllib.request
    url = (
        f"https://financialmodelingprep.com/api/v3/historical-chart/{interval}/{symbol}"
        f"?from={date_str}&to={date_str}&apikey={api_key}"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "BA2Analyzer/1.0"})
        with urllib.request.urlopen(req, timeout=15, context=_ssl_context()) as resp:
            data = json.loads(resp.read())
        if not data:
            return None
        df = pd.DataFrame(data)
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        return df
    except Exception as e:
        print(f"  FMP intraday fetch failed for {symbol}: {e}")
        return None


def get_transactions():
    """Get all transactions for expert 17."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.execute("""
        SELECT
            t.id, t.symbol, t.quantity, t.side, t.open_price, t.close_price,
            t.stop_loss, t.take_profit, t.open_date, t.close_date,
            t.close_reason, t.status, t.created_at, t.meta_data
        FROM 'transaction' t
        WHERE t.expert_id = ?
        ORDER BY t.created_at ASC
    """, (EXPERT_ID,))
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def get_recommendations():
    """Get all recommendations for expert 17."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.execute("""
        SELECT
            er.id, er.symbol, er.recommended_action, er.expected_profit_percent,
            er.price_at_date, er.confidence, er.risk_level, er.time_horizon,
            er.details, er.data, er.created_at
        FROM expertrecommendation er
        WHERE er.instance_id = ?
        ORDER BY er.created_at ASC
    """, (EXPERT_ID,))
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def get_orders_for_transaction(transaction_id):
    """Get orders for a given transaction."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.execute("""
        SELECT id, symbol, quantity, side, order_type, status, filled_qty,
               open_price, limit_price, stop_price, created_at, comment
        FROM tradingorder
        WHERE transaction_id = ?
        ORDER BY created_at ASC
    """, (transaction_id,))
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def get_monitored_state():
    """Get monitored symbols from latest market analysis."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("""
        SELECT state FROM marketanalysis
        WHERE expert_instance_id = ?
        ORDER BY created_at DESC LIMIT 1
    """, (EXPERT_ID,))
    row = cursor.fetchone()
    conn.close()
    if row and row[0]:
        state = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        return state.get("monitored_symbols", {})
    return {}


def get_all_market_analyses():
    """Get all market analyses for expert 17 with their monitored symbols."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.execute("""
        SELECT id, created_at, state, status
        FROM marketanalysis
        WHERE expert_instance_id = ? AND symbol = 'PENNY_SCAN'
        ORDER BY created_at ASC
    """, (EXPERT_ID,))
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def analyze_trade(trans, api_key):
    """Analyze a single trade with price data."""
    symbol = trans["symbol"]
    result = {
        "symbol": symbol,
        "id": trans["id"],
        "status": trans["status"],
        "side": trans["side"],
        "qty": trans["quantity"],
        "open_price": trans["open_price"],
        "close_price": trans["close_price"],
        "close_reason": trans["close_reason"],
        "open_date": trans["open_date"],
        "close_date": trans["close_date"],
    }

    # Calculate P&L
    if trans["open_price"] and trans["close_price"]:
        if trans["side"] == "BUY":
            pnl_pct = (trans["close_price"] - trans["open_price"]) / trans["open_price"] * 100
            pnl_dollar = (trans["close_price"] - trans["open_price"]) * trans["quantity"]
        else:
            pnl_pct = (trans["open_price"] - trans["close_price"]) / trans["open_price"] * 100
            pnl_dollar = (trans["open_price"] - trans["close_price"]) * trans["quantity"]
        result["pnl_pct"] = round(pnl_pct, 2)
        result["pnl_dollar"] = round(pnl_dollar, 2)
    else:
        result["pnl_pct"] = None
        result["pnl_dollar"] = None

    # Get order details
    orders = get_orders_for_transaction(trans["id"])
    result["orders"] = orders

    # Fetch historical price data around the trade
    if api_key and trans["open_date"]:
        try:
            open_dt = _parse_dt(trans["open_date"])

            from_date = (open_dt - timedelta(days=5)).strftime("%Y-%m-%d")
            to_date = (open_dt + timedelta(days=10)).strftime("%Y-%m-%d")
            trade_date = open_dt.strftime("%Y-%m-%d")

            daily_df = fetch_fmp_historical(symbol, from_date, to_date, api_key)
            if daily_df is not None and not daily_df.empty:
                # Find pre-trade close (previous day close)
                trade_day = pd.to_datetime(trade_date)
                pre_rows = daily_df[daily_df["date"] < trade_day]
                if not pre_rows.empty:
                    prev_close = pre_rows.iloc[-1]["close"]
                    result["prev_close"] = prev_close
                    if trans["open_price"]:
                        gap_pct = (trans["open_price"] - prev_close) / prev_close * 100
                        result["gap_at_entry_pct"] = round(gap_pct, 2)

                # Day of trade stats
                trade_rows = daily_df[daily_df["date"].dt.date == trade_day.date()]
                if not trade_rows.empty:
                    day_data = trade_rows.iloc[0]
                    result["day_open"] = day_data.get("open")
                    result["day_high"] = day_data.get("high")
                    result["day_low"] = day_data.get("low")
                    result["day_close"] = day_data.get("close")
                    result["day_volume"] = day_data.get("volume")
                    result["day_change_pct"] = day_data.get("changePercent")

                    if trans["open_price"] and day_data.get("high"):
                        # Max profit available from entry
                        max_profit = (day_data["high"] - trans["open_price"]) / trans["open_price"] * 100
                        result["max_intraday_profit_pct"] = round(max_profit, 2)
                        # Max loss from entry
                        max_loss = (day_data["low"] - trans["open_price"]) / trans["open_price"] * 100
                        result["max_intraday_loss_pct"] = round(max_loss, 2)

                # Post-trade performance (next 1-5 days)
                post_rows = daily_df[daily_df["date"] > trade_day]
                if not post_rows.empty and trans["open_price"]:
                    for days_ahead in [1, 2, 3, 5]:
                        if len(post_rows) >= days_ahead:
                            future_close = post_rows.iloc[days_ahead - 1]["close"]
                            future_pct = (future_close - trans["open_price"]) / trans["open_price"] * 100
                            result[f"day+{days_ahead}_pct"] = round(future_pct, 2)
                            future_high = post_rows.iloc[:days_ahead]["high"].max()
                            best_pct = (future_high - trans["open_price"]) / trans["open_price"] * 100
                            result[f"day+{days_ahead}_best_pct"] = round(best_pct, 2)

            # Fetch intraday for trade date
            intraday_df = fetch_fmp_intraday(symbol, trade_date, api_key, "5min")
            if intraday_df is not None and not intraday_df.empty:
                # Find price at key timestamps
                open_price = trans["open_price"]
                if open_price:
                    # How far had it already moved when we entered?
                    first_bar = intraday_df.iloc[0]
                    result["first_bar_price"] = first_bar.get("open") or first_bar.get("close")
                    if result.get("first_bar_price") and result.get("prev_close"):
                        premarket_move = (result["first_bar_price"] - result["prev_close"]) / result["prev_close"] * 100
                        result["premarket_move_pct"] = round(premarket_move, 2)

                    # Find the bar closest to entry time
                    if trans["open_date"]:
                        entry_time = pd.to_datetime(trans["open_date"])
                        intraday_df["time_diff"] = abs(intraday_df["date"] - entry_time)
                        entry_idx = intraday_df["time_diff"].idxmin()
                        entry_bar_idx = intraday_df.index.get_loc(entry_idx)

                        # How much had it moved from open to entry?
                        bars_before_entry = intraday_df.iloc[:entry_bar_idx + 1]
                        if not bars_before_entry.empty:
                            high_before_entry = bars_before_entry["high"].max()
                            low_before_entry = bars_before_entry["low"].min()
                            result["high_before_entry"] = high_before_entry
                            result["low_before_entry"] = low_before_entry

                        # Bars after entry - how quickly did it reverse?
                        bars_after_entry = intraday_df.iloc[entry_bar_idx:]
                        if len(bars_after_entry) >= 2:
                            # Check price 5, 15, 30 minutes after entry
                            for mins in [1, 3, 6, 12]:  # 5min bars
                                if len(bars_after_entry) > mins:
                                    future_price = bars_after_entry.iloc[mins]["close"]
                                    chg = (future_price - open_price) / open_price * 100
                                    result[f"entry+{mins*5}min_pct"] = round(chg, 2)

        except Exception as e:
            print(f"  Price analysis error for {symbol}: {e}")

    return result


def analyze_missed_opportunities(monitored_symbols, api_key):
    """Analyze symbols that were watched but never triggered."""
    results = []
    for sym, info in monitored_symbols.items():
        if info.get("status") in ("watching", "expired"):
            confidence = info.get("confidence", 0)
            catalyst = info.get("catalyst", "")
            prev_close = info.get("prev_close")
            created_at = info.get("created_at", "")
            last_price = info.get("last_price")

            result = {
                "symbol": sym,
                "status": info.get("status"),
                "confidence": confidence,
                "catalyst": catalyst[:100],
                "strategy": info.get("strategy"),
                "prev_close": prev_close,
                "last_price": last_price,
            }

            # Check entry conditions
            entry_conds = info.get("entry_conditions", {})
            last_eval = info.get("conditions_last_eval", {})
            result["entry_conditions"] = entry_conds
            result["last_eval"] = last_eval

            # Count how many conditions were met
            total = len(last_eval)
            met = sum(1 for v in last_eval.values() if v)
            result["conditions_met"] = met
            result["conditions_total"] = total
            result["conditions_unmet"] = [k for k, v in last_eval.items() if not v]

            # Fetch price data if possible
            if api_key and created_at:
                try:
                    created_dt = _parse_dt(created_at)
                    from_date = created_dt.strftime("%Y-%m-%d")
                    to_date = (created_dt + timedelta(days=5)).strftime("%Y-%m-%d")

                    daily_df = fetch_fmp_historical(sym, from_date, to_date, api_key)
                    if daily_df is not None and not daily_df.empty:
                        # How much did it move after we started watching?
                        first_row = daily_df.iloc[0]
                        best_high = daily_df["high"].max()
                        result["watched_day_close"] = first_row.get("close")
                        result["best_high_in_window"] = best_high
                        if prev_close and prev_close > 0:
                            best_move_pct = (best_high - prev_close) / prev_close * 100
                            result["best_move_from_prev_close_pct"] = round(best_move_pct, 2)
                except Exception as e:
                    print(f"  Missed opp analysis error for {sym}: {e}")

            results.append(result)
    return results


def main():
    api_key = get_fmp_api_key()
    if not api_key:
        print("WARNING: No FMP API key found. Price analysis will be limited.")
        print("Set FMP_API_KEY environment variable or check .env file.\n")
    else:
        print(f"FMP API key found: {api_key[:8]}...\n")

    # ================================================================
    # 1. TRANSACTION ANALYSIS
    # ================================================================
    print("=" * 80)
    print("EXPERT 17 (PennyMomentumTrader) - COMPREHENSIVE TRADE ANALYSIS")
    print("=" * 80)

    transactions = get_transactions()
    closed_trades = [t for t in transactions if t["status"] == "CLOSED"]
    open_trades = [t for t in transactions if t["status"] in ("OPENED", "WAITING")]

    print(f"\nTotal transactions: {len(transactions)}")
    print(f"  Closed: {len(closed_trades)}")
    print(f"  Open/Waiting: {len(open_trades)}")

    # Analyze each closed trade
    print("\n" + "-" * 80)
    print("CLOSED TRADE DETAILS")
    print("-" * 80)

    trade_analyses = []
    winners = []
    losers = []

    for trans in closed_trades:
        print(f"\n--- {trans['symbol']} (txn #{trans['id']}) ---")
        analysis = analyze_trade(trans, api_key)
        trade_analyses.append(analysis)

        if analysis["pnl_pct"] is not None:
            if analysis["pnl_pct"] > 0:
                winners.append(analysis)
            else:
                losers.append(analysis)

        print(f"  P&L: {analysis.get('pnl_pct', 'N/A')}% (${analysis.get('pnl_dollar', 'N/A')})")
        print(f"  Entry: ${analysis.get('open_price', 'N/A')} | Exit: ${analysis.get('close_price', 'N/A')}")
        print(f"  Close reason: {analysis.get('close_reason', 'N/A')}")
        if analysis.get("gap_at_entry_pct") is not None:
            print(f"  Gap at entry: {analysis['gap_at_entry_pct']}% from prev close")
        if analysis.get("premarket_move_pct") is not None:
            print(f"  Premarket move: {analysis['premarket_move_pct']}%")
        if analysis.get("max_intraday_profit_pct") is not None:
            print(f"  Max intraday profit available: {analysis['max_intraday_profit_pct']}%")
            print(f"  Max intraday loss: {analysis['max_intraday_loss_pct']}%")
        if analysis.get("day_change_pct") is not None:
            print(f"  Day change: {analysis['day_change_pct']}%")
        for key in ["entry+5min_pct", "entry+15min_pct", "entry+30min_pct", "entry+60min_pct"]:
            if key in analysis:
                print(f"  {key}: {analysis[key]}%")
        for key in ["day+1_pct", "day+2_pct", "day+3_pct", "day+5_pct"]:
            if key in analysis:
                best_key = key.replace("_pct", "_best_pct")
                print(f"  {key}: {analysis[key]}% (best: {analysis.get(best_key, 'N/A')}%)")

        # Orders analysis
        orders = analysis.get("orders", [])
        for o in orders:
            print(f"  Order: {o['side']} {o['quantity']}x @ ${o.get('open_price', o.get('limit_price', 'N/A'))} ({o['status']})")

        # Hold time
        if trans["open_date"] and trans["close_date"]:
            open_dt = _parse_dt(trans["open_date"])
            close_dt = _parse_dt(trans["close_date"])
            if open_dt and close_dt:
                hold_mins = (close_dt - open_dt).total_seconds() / 60
                result_label = "WINNER" if (analysis.get("pnl_pct") or 0) > 0 else "LOSER"
                print(f"  Hold time: {hold_mins:.0f} minutes | {result_label}")

    # ================================================================
    # 2. AGGREGATE STATISTICS
    # ================================================================
    print("\n" + "=" * 80)
    print("AGGREGATE STATISTICS")
    print("=" * 80)

    if trade_analyses:
        total_pnl = sum(a.get("pnl_dollar", 0) or 0 for a in trade_analyses)
        win_rate = len(winners) / len(trade_analyses) * 100 if trade_analyses else 0

        print(f"\nWin Rate: {win_rate:.1f}% ({len(winners)}W / {len(losers)}L)")
        print(f"Total P&L: ${total_pnl:.2f}")

        if winners:
            avg_win = sum(w["pnl_pct"] for w in winners) / len(winners)
            avg_win_dollar = sum(w["pnl_dollar"] for w in winners) / len(winners)
            max_win = max(w["pnl_pct"] for w in winners)
            print(f"Winners: avg {avg_win:.2f}% (${avg_win_dollar:.2f}), max {max_win:.2f}%")

        if losers:
            avg_loss = sum(l["pnl_pct"] for l in losers) / len(losers)
            avg_loss_dollar = sum(l["pnl_dollar"] for l in losers) / len(losers)
            max_loss = min(l["pnl_pct"] for l in losers)
            print(f"Losers: avg {avg_loss:.2f}% (${avg_loss_dollar:.2f}), max loss {max_loss:.2f}%")

        # Profit factor
        gross_profit = sum(w.get("pnl_dollar", 0) for w in winners)
        gross_loss = abs(sum(l.get("pnl_dollar", 0) for l in losers))
        if gross_loss > 0:
            print(f"Profit Factor: {gross_profit / gross_loss:.2f}")

        # Entry gap analysis
        print("\n--- Entry Gap Analysis ---")
        gap_trades = [a for a in trade_analyses if a.get("gap_at_entry_pct") is not None]
        if gap_trades:
            gap_winners = [a for a in gap_trades if (a.get("pnl_pct") or 0) > 0]
            gap_losers = [a for a in gap_trades if (a.get("pnl_pct") or 0) <= 0]

            if gap_winners:
                avg_gap_win = sum(w["gap_at_entry_pct"] for w in gap_winners) / len(gap_winners)
                print(f"  Winners avg gap at entry: {avg_gap_win:.1f}%")
            if gap_losers:
                avg_gap_loss = sum(l["gap_at_entry_pct"] for l in gap_losers) / len(gap_losers)
                print(f"  Losers avg gap at entry: {avg_gap_loss:.1f}%")

            for a in gap_trades:
                tag = "W" if (a.get("pnl_pct") or 0) > 0 else "L"
                print(f"  {a['symbol']} [{tag}]: gap={a['gap_at_entry_pct']:+.1f}%, "
                      f"p&l={a.get('pnl_pct', 'N/A')}%, "
                      f"max_profit={a.get('max_intraday_profit_pct', 'N/A')}%")

        # Hold time analysis
        print("\n--- Hold Time Analysis ---")
        for a in trade_analyses:
            trans = next(t for t in transactions if t["id"] == a["id"])
            if trans["open_date"] and trans["close_date"]:
                open_dt = _parse_dt(trans["open_date"])
                close_dt = _parse_dt(trans["close_date"])
                hold_mins = (close_dt - open_dt).total_seconds() / 60
                tag = "W" if (a.get("pnl_pct") or 0) > 0 else "L"
                print(f"  {a['symbol']} [{tag}]: held {hold_mins:.0f} min, p&l={a.get('pnl_pct')}%")

        # Post-entry price action analysis
        print("\n--- Post-Entry Price Action (did we sell too early/late?) ---")
        for a in trade_analyses:
            if a.get("pnl_pct") is not None:
                tag = "W" if a["pnl_pct"] > 0 else "L"
                future_data = []
                for key in ["day+1_pct", "day+2_pct", "day+3_pct", "day+5_pct"]:
                    if key in a:
                        future_data.append(f"{key}={a[key]}%")
                best_data = []
                for key in ["day+1_best_pct", "day+2_best_pct", "day+3_best_pct", "day+5_best_pct"]:
                    if key in a:
                        best_data.append(f"{key}={a[key]}%")
                if future_data:
                    print(f"  {a['symbol']} [{tag}] (p&l={a['pnl_pct']}%): {', '.join(future_data)}")
                    if best_data:
                        print(f"    Best possible: {', '.join(best_data)}")

    # ================================================================
    # 3. PATTERN ANALYSIS: WINNERS vs LOSERS
    # ================================================================
    print("\n" + "=" * 80)
    print("PATTERN ANALYSIS: WINNERS vs LOSERS")
    print("=" * 80)

    recommendations = get_recommendations()
    rec_by_symbol_date = {}
    for rec in recommendations:
        key = f"{rec['symbol']}_{rec['created_at'][:10]}"
        rec_by_symbol_date[key] = rec

    print("\n--- Confidence Score Distribution ---")
    for a in trade_analyses:
        trans = next(t for t in transactions if t["id"] == a["id"])
        # Find matching recommendation
        matching_recs = [r for r in recommendations
                        if r["symbol"] == a["symbol"]
                        and r.get("created_at", "")[:10] == (trans.get("open_date") or trans.get("created_at", ""))[:10]]
        conf = matching_recs[-1]["confidence"] if matching_recs else "N/A"
        tag = "W" if (a.get("pnl_pct") or 0) > 0 else "L"
        print(f"  {a['symbol']} [{tag}]: confidence={conf}, p&l={a.get('pnl_pct')}%")

    # ================================================================
    # 4. MISSED OPPORTUNITIES (watched but never entered)
    # ================================================================
    print("\n" + "=" * 80)
    print("MISSED OPPORTUNITIES (watched but never triggered)")
    print("=" * 80)

    # Get all monitored symbols across all analyses
    all_analyses = get_all_market_analyses()
    all_monitored = {}
    for ma in all_analyses:
        state = json.loads(ma["state"]) if isinstance(ma["state"], str) else ma["state"]
        if state:
            monitored = state.get("monitored_symbols", {})
            for sym, info in monitored.items():
                if info.get("status") in ("watching", "expired"):
                    # Only keep if not already traded
                    traded_symbols = {t["symbol"] for t in transactions}
                    if sym not in traded_symbols or sym in {t["symbol"] for t in transactions if t["status"] in ("OPENED", "WAITING")}:
                        pass
                    info["_analysis_date"] = ma["created_at"]
                    all_monitored[f"{sym}_{ma['created_at'][:10]}"] = {"symbol": sym, **info}

    missed = analyze_missed_opportunities(
        {k: v for k, v in all_monitored.items()},
        api_key
    )

    for m in sorted(missed, key=lambda x: x.get("best_move_from_prev_close_pct", 0) or 0, reverse=True):
        print(f"\n  {m['symbol']} (conf={m['confidence']}, strategy={m.get('strategy')})")
        print(f"    Catalyst: {m.get('catalyst', 'N/A')}")
        print(f"    Conditions met: {m['conditions_met']}/{m['conditions_total']}")
        if m.get("conditions_unmet"):
            print(f"    Unmet: {m['conditions_unmet']}")
        if m.get("best_move_from_prev_close_pct") is not None:
            print(f"    Best move from prev close: {m['best_move_from_prev_close_pct']}%")
        if m.get("watched_day_close") is not None:
            print(f"    Day close: ${m['watched_day_close']}")

    # ================================================================
    # 5. EXIT CONDITION ANALYSIS
    # ================================================================
    print("\n" + "=" * 80)
    print("EXIT TIMING ANALYSIS")
    print("=" * 80)

    # Analyze the close reasons
    close_reasons = {}
    for t in closed_trades:
        reason = t.get("close_reason", "unknown")
        close_reasons[reason] = close_reasons.get(reason, 0) + 1
    print("\nClose reason distribution:")
    for reason, count in sorted(close_reasons.items(), key=lambda x: -x[1]):
        pnls = [a.get("pnl_pct", 0) for a in trade_analyses
                if next(t for t in transactions if t["id"] == a["id"])["close_reason"] == reason
                and a.get("pnl_pct") is not None]
        avg_pnl = sum(pnls) / len(pnls) if pnls else 0
        print(f"  {reason}: {count}x (avg P&L: {avg_pnl:.2f}%)")

    # ================================================================
    # 6. CURRENT OPEN POSITIONS
    # ================================================================
    print("\n" + "=" * 80)
    print("CURRENT OPEN POSITIONS")
    print("=" * 80)

    for trans in open_trades:
        print(f"\n  {trans['symbol']} (txn #{trans['id']}) | Status: {trans['status']}")
        print(f"    Entry: ${trans.get('open_price', 'N/A')} | Qty: {trans['quantity']}")
        print(f"    Opened: {trans.get('open_date', trans.get('created_at', 'N/A'))}")

        orders = get_orders_for_transaction(trans["id"])
        for o in orders:
            print(f"    Order: {o['side']} {o['quantity']}x @ ${o.get('open_price', o.get('limit_price', 'N/A'))} ({o['status']}) - {o.get('comment', '')}")

    # ================================================================
    # 7. SUMMARY & RECOMMENDATIONS
    # ================================================================
    print("\n" + "=" * 80)
    print("IDENTIFIED PATTERNS & ISSUES")
    print("=" * 80)

    # Analyze stop loss timing
    sl_exits = [a for a in trade_analyses if a.get("close_reason") == "position_balanced"
                and (a.get("pnl_pct") or 0) < 0]
    if sl_exits:
        print(f"\n  STOP LOSS EXITS: {len(sl_exits)} trades stopped out")
        for a in sl_exits:
            print(f"    {a['symbol']}: lost {a.get('pnl_pct')}%, max avail profit was {a.get('max_intraday_profit_pct', 'N/A')}%")

    # Check for entries that immediately reversed
    quick_reversals = [a for a in trade_analyses if a.get("entry+5min_pct") is not None
                       and a["entry+5min_pct"] < -1]
    if quick_reversals:
        print(f"\n  IMMEDIATE REVERSALS (>1% down within 5min of entry): {len(quick_reversals)}")
        for a in quick_reversals:
            print(f"    {a['symbol']}: {a['entry+5min_pct']}% in 5min, gap={a.get('gap_at_entry_pct', 'N/A')}%")

    # Check if entries are chasing gaps
    high_gap_entries = [a for a in trade_analyses if a.get("gap_at_entry_pct") is not None
                        and a["gap_at_entry_pct"] > 15]
    if high_gap_entries:
        print(f"\n  HIGH-GAP ENTRIES (>15% gap at entry): {len(high_gap_entries)}")
        for a in high_gap_entries:
            tag = "W" if (a.get("pnl_pct") or 0) > 0 else "L"
            print(f"    {a['symbol']} [{tag}]: gap={a['gap_at_entry_pct']}%, p&l={a.get('pnl_pct')}%")


if __name__ == "__main__":
    main()
