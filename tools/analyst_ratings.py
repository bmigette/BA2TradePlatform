"""CLI equivalent of the UI 'Analyst Ratings' tab (ui/pages/tools.py).

Fetches analyst ratings for a symbol from Finnhub and FMP, using the same
endpoints as the UI:
  - Finnhub  GET /api/v1/stock/recommendation
  - FMP      GET /stable/grades
  - FMP      GET /stable/price-target-consensus
  - FMP      GET /stable/grades-consensus

Key resolution order:
  1. env vars FMP_API_KEY / FINNHUB_API_KEY
  2. creds.env / .env at repo root (FMP_API_KEY=... style lines)
  3. app settings DB (get_app_setting), same as the UI

Usage:
  python tools/analyst_ratings.py AAOI
  python tools/analyst_ratings.py AAOI --grades-limit 40 --json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
TIMEOUT = 15


def _load_dotenv_keys() -> Dict[str, str]:
    keys: Dict[str, str] = {}
    for name in ("creds.env", ".env"):
        p = REPO_ROOT / name
        if not p.exists():
            continue
        try:
            for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and v:
                    keys.setdefault(k, v)
        except Exception:
            pass
    return keys


CANDIDATE_DBS = [
    r"C:\Users\basti\Documents\ba2_trade_platform-prod\db.sqlite",
    r"C:\Users\basti\Documents\ba2_trade_platform\db.sqlite",
]


def _db_setting(key: str) -> Optional[str]:
    try:
        sys.path.insert(0, str(REPO_ROOT))
        from ba2_trade_platform.config import get_app_setting

        val = get_app_setting(key)
        if val:
            return val
    except Exception:
        pass
    # Fallback: read appsetting table directly from known platform DBs
    import sqlite3

    for db in CANDIDATE_DBS:
        if not Path(db).exists():
            continue
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
            try:
                row = con.execute(
                    "SELECT value_str FROM appsetting WHERE key = ? LIMIT 1", (key,)
                ).fetchone()
                if row and row[0]:
                    return row[0]
            finally:
                con.close()
        except Exception:
            continue
    return None


def get_key(env_name: str, app_setting_keys: List[str]) -> Optional[str]:
    val = os.environ.get(env_name)
    if val:
        return val
    val = _load_dotenv_keys().get(env_name)
    if val:
        return val
    for k in app_setting_keys:
        val = _db_setting(k)
        if val:
            return val
    return None


def _get(url: str, params: Dict[str, Any]) -> Any:
    resp = requests.get(url, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def fetch_finnhub_ratings(symbol: str, key: str) -> Optional[List[Dict[str, Any]]]:
    return _get(
        "https://finnhub.io/api/v1/stock/recommendation",
        {"symbol": symbol, "token": key},
    )


def fetch_fmp_grades(symbol: str, key: str, limit: int = 20) -> Optional[List[Dict[str, Any]]]:
    return _get(
        "https://financialmodelingprep.com/stable/grades",
        {"symbol": symbol, "apikey": key, "limit": limit},
    )


def fetch_fmp_price_target(symbol: str, key: str) -> Optional[Dict[str, Any]]:
    data = _get(
        "https://financialmodelingprep.com/stable/price-target-consensus",
        {"symbol": symbol, "apikey": key},
    )
    if isinstance(data, list):
        return data[0] if data else None
    return data if isinstance(data, dict) else None


def fetch_fmp_grades_consensus(symbol: str, key: str) -> Optional[Dict[str, Any]]:
    data = _get(
        "https://financialmodelingprep.com/stable/grades-consensus",
        {"symbol": symbol, "apikey": key},
    )
    if isinstance(data, list):
        return data[0] if data else None
    return data if isinstance(data, dict) else None


def _fmt_num(v: Any, digits: int = 2) -> str:
    if v is None:
        return "-"
    try:
        return f"{float(v):,.{digits}f}"
    except (TypeError, ValueError):
        return str(v)


def compare_sources(symbols: List[str]) -> int:
    """Compare Finnhub vs FMP analyst rating buckets for several symbols."""
    fmp_key = get_key("FMP_API_KEY", ["FMP_API_KEY", "fmp_api_key"])
    fh_key = get_key("FINNHUB_API_KEY", ["finnhub_api_key"])
    if not fmp_key or not fh_key:
        print("Need both FMP and Finnhub keys to compare.", file=sys.stderr)
        return 2

    rows = []
    for sym in symbols:
        sym = sym.upper()
        fh_counts = None
        try:
            data = fetch_finnhub_ratings(sym, fh_key)
            if data:
                latest = sorted(data, key=lambda r: r.get("period", ""), reverse=True)[0]
                fh_counts = [latest.get(k, 0) for k in ("strongBuy", "buy", "hold", "sell", "strongSell")]
        except Exception as e:
            print(f"{sym}: finnhub failed: {e}", file=sys.stderr)
        fmp_counts = None
        try:
            c = fetch_fmp_grades_consensus(sym, fmp_key)
            if c:
                fmp_counts = [c.get(k, 0) for k in ("strongBuy", "buy", "hold", "sell", "strongSell")]
        except Exception as e:
            print(f"{sym}: fmp failed: {e}", file=sys.stderr)
        rows.append((sym, fh_counts, fmp_counts))

    labels = ["sBuy", "buy", "hold", "sell", "sSell"]
    hdr = f"{'sym':<7} | {'source':<4} | " + " ".join(f"{l:>5}" for l in labels) + f" | {'tot':>4} | {'bull%':>5} | consensus"
    print("\n" + hdr)
    print("-" * len(hdr))
    for sym, fh, fmp in rows:
        for src, counts in (("fh", fh), ("fmp", fmp)):
            if counts is None:
                print(f"{sym:<7} | {src:<4} | (no data)")
                continue
            tot = sum(counts)
            bull = (counts[0] + counts[1]) / tot * 100 if tot else 0.0
            cons = f"{counts[0]}b+{counts[1]}b/{counts[2]}h/{counts[3]+counts[4]}s"
            print(f"{sym:<7} | {src:<4} | " + " ".join(f"{c:>5}" for c in counts) + f" | {tot:>4} | {bull:>4.0f}% | {cons}")
        if fh and fmp:
            fh_tot, fmp_tot = sum(fh), sum(fmp)
            fh_bull = (fh[0] + fh[1]) / fh_tot * 100 if fh_tot else 0.0
            fmp_bull = (fmp[0] + fmp[1]) / fmp_tot * 100 if fmp_tot else 0.0
            print(f"{'':<7} | diff | bull% gap: {abs(fh_bull - fmp_bull):.0f} pts, analysts gap: {abs(fh_tot - fmp_tot)}")
    return 0


def print_report(symbol: str, fh: Any, grades: Any, target: Any, consensus: Any, limit: int) -> None:
    print(f"\n=== Analyst ratings for {symbol} ===\n")

    print("--- Finnhub: recommendation trends (monthly) ---")
    if fh is None:
        print("  (unavailable or no finnhub key)")
    elif not fh:
        print("  no data")
    else:
        print(f"  {'period':<10} {'strongBuy':>9} {'buy':>6} {'hold':>6} {'sell':>6} {'strongSell':>10}")
        for row in fh[:6]:
            print(
                f"  {str(row.get('period','')):<10} "
                f"{row.get('strongBuy',0):>9} {row.get('buy',0):>6} "
                f"{row.get('hold',0):>6} {row.get('sell',0):>6} {row.get('strongSell',0):>10}"
            )

    print("\n--- FMP: grades consensus ---")
    if consensus is None:
        print("  (unavailable or no FMP key)")
    elif not consensus:
        print("  no data")
    else:
        for k in ("strongBuyCount", "buyCount", "holdCount", "sellCount", "strongSellCount", "consensus"):
            if k in consensus:
                print(f"  {k}: {consensus[k]}")

    print("\n--- FMP: price target consensus ---")
    if target is None:
        print("  (unavailable or no FMP key)")
    elif not target:
        print("  no data")
    else:
        for k in ("targetHigh", "targetLow", "targetConsensus", "targetMedian", "analystCount", "publishedDate"):
            if k in target:
                v = target[k]
                print(f"  {k}: {_fmt_num(v) if isinstance(v, (int, float)) else v}")

    print(f"\n--- FMP: recent grades (last {limit}) ---")
    if grades is None:
        print("  (unavailable or no FMP key)")
    elif not grades:
        print("  no data")
    else:
        print(f"  {'date':<12} {'analyst':<24} {'action':<18} {'grade':<16} {'priceWhenPosted':>15}")
        for g in grades[:limit]:
            date = str(g.get("date", ""))[:10]
            analyst = str(g.get("analystCompany", ""))[:23]
            action = str(g.get("action", ""))[:17]
            grade = str(g.get("newGrade", "") or g.get("grade", ""))[:15]
            print(f"  {date:<12} {analyst:<24} {action:<18} {grade:<16} {_fmt_num(g.get('priceWhenPosted')):>15}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Analyst ratings CLI (Finnhub + FMP), same sources as UI Analyst Ratings tab")
    ap.add_argument("symbol", help="Ticker symbol, e.g. AAOI (or several, comma-separated with --compare)")
    ap.add_argument("--grades-limit", type=int, default=20, help="Max FMP grades to fetch/show (default 20)")
    ap.add_argument("--json", action="store_true", help="Dump raw JSON instead of the report")
    ap.add_argument("--compare", action="store_true", help="Compare Finnhub vs FMP rating buckets (symbol = comma-separated list)")
    args = ap.parse_args()

    if args.compare:
        return compare_sources([s for s in args.symbol.replace(";", ",").split(",") if s.strip()])

    symbol = args.symbol.upper()
    fmp_key = get_key("FMP_API_KEY", ["FMP_API_KEY", "fmp_api_key"])
    fh_key = get_key("FINNHUB_API_KEY", ["finnhub_api_key"])
    if not fh_key:
        fh_key = get_key("FINNHUB_API_KEY", ["FINNHUB_API_KEY"])

    if not fmp_key and not fh_key:
        print("No API keys found (env FMP_API_KEY/FINNHUB_API_KEY, creds.env/.env, or app settings DB).", file=sys.stderr)
        return 2

    fh = grades = target = consensus = None
    if fh_key:
        try:
            fh = fetch_finnhub_ratings(symbol, fh_key)
        except Exception as e:
            print(f"Finnhub fetch failed: {e}", file=sys.stderr)
    if fmp_key:
        for name, fn in (
            ("grades", lambda: fetch_fmp_grades(symbol, fmp_key, args.grades_limit)),
            ("price target", lambda: fetch_fmp_price_target(symbol, fmp_key)),
            ("grades consensus", lambda: fetch_fmp_grades_consensus(symbol, fmp_key)),
        ):
            try:
                val = fn()
                if name == "grades":
                    grades = val
                elif name == "price target":
                    target = val
                else:
                    consensus = val
            except Exception as e:
                print(f"FMP {name} fetch failed: {e}", file=sys.stderr)

    if args.json:
        print(json.dumps({"symbol": symbol, "finnhub": fh, "fmp_grades": grades,
                          "fmp_price_target": target, "fmp_grades_consensus": consensus}, indent=2, default=str))
    else:
        print_report(symbol, fh, grades, target, consensus, args.grades_limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
