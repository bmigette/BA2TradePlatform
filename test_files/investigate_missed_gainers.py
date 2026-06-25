"""
Investigation script: Why did PennyMomentumTrader miss certain gainers?

Stocks to investigate (from today's top gainers list, under $5):
- TURB (+32%): Never appeared in any scan
- HWH (+33%): Never appeared in any scan
- AIFF (+64%): Seen but rejected for low RVOL (0.1-0.2x) most days; on Mar 24 had RVOL 2.4x but cut by max_scan_candidates=50 cap
- LNAI (+52%): Found Mar 23 in quick_filter but rejected as "biotech binary event risk"
- FATN (+43%): Seen Mar 24 in filtered_stocks with low RVOL (0.3x)
- FCHL (+70%): Found multiple times but correctly(?) rejected - dilutive offering, reverse split
- SLND (+27%): Found multiple times but rejected for "no catalyst"
- ICU (+28%): Found today with confidence 40 (just below 55 threshold)

This script:
1. Fetches current FMP gainers to see how many TURB/HWH are included
2. Fetches 5-day historical data for TURB and HWH to understand their pre-move profile
3. Checks what screener would have returned for AIFF on March 24 (RVOL=2.4x but cut by cap)
4. Fetches AIFF, LNAI details to understand sector classification and catalyst
"""

import os
import json
import sys
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FMP_API_KEY = os.getenv("FMP_API_KEY") or ""
if not FMP_API_KEY:
    # Try loading from app settings in DB
    import sqlite3
    db_path = os.path.expanduser("~/Documents/ba2_trade_platform/db.sqlite")
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT value_str FROM appsetting WHERE key = 'FMP_API_KEY' LIMIT 1")
    row = cur.fetchone()
    if row:
        FMP_API_KEY = row[0] or ""
    conn.close()

BASE_URL = "https://financialmodelingprep.com/api/v3"

def fmp_get(endpoint: str, params: dict = None) -> dict | list:
    p = {"apikey": FMP_API_KEY}
    if params:
        p.update(params)
    resp = requests.get(f"{BASE_URL}/{endpoint}", params=p, timeout=20)
    resp.raise_for_status()
    return resp.json()


def separator(title: str):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print('=' * 60)


# -----------------------------------------------------------------------
# 1. Check how many stocks the FMP gainers endpoint returns
#    and whether TURB/HWH are included
# -----------------------------------------------------------------------
separator("1. FMP /stock_market/gainers endpoint")

gainers = fmp_get("stock_market/gainers")
print(f"Total gainers returned by FMP: {len(gainers)}")
if gainers:
    print("Fields available:", list(gainers[0].keys()) if gainers else "none")
    for i, g in enumerate(gainers[:5], 1):
        print(f"  #{i}: {g.get('symbol')} +{g.get('changesPercentage', 0):.1f}% @ ${g.get('price', 0):.2f} vol={g.get('volume', 0):,}")

targets = ["TURB", "HWH", "AIFF", "LNAI", "FATN", "SLND", "ICU", "BZAI", "FCHL", "SPPL", "SVRN", "OLPX", "VSA"]
print(f"\nSearching for our targets in the gainers list:")
found = {}
for g in gainers:
    sym = (g.get("symbol") or "").upper()
    if sym in targets:
        found[sym] = g
        print(f"  FOUND: {sym} - +{g.get('changesPercentage', 0):.1f}% @ ${g.get('price', 0):.3f} vol={g.get('volume', 0):,} mcap=${g.get('marketCap', 0)/1e6:.1f}M")

for t in targets:
    if t not in found:
        print(f"  NOT IN LIST: {t}")


# -----------------------------------------------------------------------
# 2. Check TURB and HWH historical data (5-day OHLCV)
#    to understand their pre-move profile on March 24-25
# -----------------------------------------------------------------------
separator("2. TURB and HWH - Historical Profile (5-day)")

for sym in ["TURB", "HWH"]:
    print(f"\n--- {sym} ---")
    # Get profile (market cap, avg volume, etc.)
    try:
        profile = fmp_get(f"profile/{sym}")
        if profile and isinstance(profile, list):
            p = profile[0]
            print(f"  Company: {p.get('companyName')}")
            print(f"  Exchange: {p.get('exchangeShortName')} ({p.get('exchange')})")
            print(f"  Sector: {p.get('sector')} / {p.get('industry')}")
            print(f"  Market Cap: ${p.get('mktCap', 0)/1e6:.1f}M")
            print(f"  Avg Volume: {p.get('volAvg', 0):,}")
            print(f"  Float: {p.get('sharesFloat', 0):,}")
            print(f"  Beta: {p.get('beta')}")
    except Exception as e:
        print(f"  Profile error: {e}")

    # Get 5-day daily candles
    try:
        hist = fmp_get(f"historical-price-full/{sym}", {"timeseries": 10})
        if hist and isinstance(hist, dict):
            candles = hist.get("historical", [])[:7]  # last 7 days
            print(f"  Recent daily candles:")
            for c in candles:
                date = c.get("date")
                close = c.get("close")
                chg_pct = c.get("changePercent", 0)
                vol = c.get("volume", 0)
                avg_vol = p.get("volAvg", 1) if profile and isinstance(profile, list) else 1
                rvol = vol / avg_vol if avg_vol > 0 else 0
                print(f"    {date}: close=${close:.3f} chg={chg_pct:+.1f}% vol={vol:,} rvol={rvol:.1f}x")
    except Exception as e:
        print(f"  Historical error: {e}")

    # Get quote (current state)
    try:
        quote = fmp_get(f"quote/{sym}")
        if quote and isinstance(quote, list):
            q = quote[0]
            print(f"  Current quote: ${q.get('price', 0):.3f} chg={q.get('changesPercentage', 0):+.1f}%")
            print(f"    vol={q.get('volume', 0):,} avgVol={q.get('avgVolume', 0):,}")
            prev_close = q.get('previousClose', q.get('price', 0))
            cur_price = q.get('price', 0)
            rvol = q.get('volume', 0) / q.get('avgVolume', 1) if q.get('avgVolume', 0) > 0 else 0
            print(f"    RVOL (current day): {rvol:.1f}x")
    except Exception as e:
        print(f"  Quote error: {e}")


# -----------------------------------------------------------------------
# 3. Check AIFF - why low RVOL for days and what changed today
# -----------------------------------------------------------------------
separator("3. AIFF - Low RVOL for Days, Then +64%")

sym = "AIFF"
try:
    profile = fmp_get(f"profile/{sym}")
    if profile and isinstance(profile, list):
        p = profile[0]
        print(f"  Company: {p.get('companyName')}")
        print(f"  Exchange: {p.get('exchangeShortName')}")
        print(f"  Sector: {p.get('sector')} / {p.get('industry')}")
        print(f"  Market Cap: ${p.get('mktCap', 0)/1e6:.1f}M")
        print(f"  Avg Volume (30-day): {p.get('volAvg', 0):,}")
        print(f"  Float: {p.get('sharesFloat', 0):,}")
except Exception as e:
    print(f"  Profile error: {e}")

try:
    hist = fmp_get(f"historical-price-full/{sym}", {"timeseries": 10})
    if hist and isinstance(hist, dict):
        candles = hist.get("historical", [])[:7]
        print(f"\n  Recent daily candles:")
        for c in candles:
            date = c.get("date")
            close = c.get("close")
            chg_pct = c.get("changePercent", 0)
            vol = c.get("volume", 0)
            avg_vol = profile[0].get("volAvg", 1) if profile and isinstance(profile, list) else 1
            rvol = vol / avg_vol if avg_vol > 0 else 0
            print(f"    {date}: close=${close:.3f} chg={chg_pct:+.1f}% vol={vol:,} rvol={rvol:.1f}x")
except Exception as e:
    print(f"  Historical error: {e}")

# Get recent news for AIFF (to understand today's catalyst)
try:
    news = fmp_get(f"stock_news", {"tickers": sym, "limit": 5})
    if news:
        print(f"\n  Recent news ({len(news)} items):")
        for n in news[:5]:
            print(f"    [{n.get('publishedDate', '')[:10]}] {n.get('title', '')[:100]}")
except Exception as e:
    print(f"  News error: {e}")


# -----------------------------------------------------------------------
# 4. Check LNAI - why it was rejected as "biotech" on Mar 23
# -----------------------------------------------------------------------
separator("4. LNAI - Rejected as Biotech Binary Event")

sym = "LNAI"
try:
    profile = fmp_get(f"profile/{sym}")
    if profile and isinstance(profile, list):
        p = profile[0]
        print(f"  Company: {p.get('companyName')}")
        print(f"  Sector: {p.get('sector')} / {p.get('industry')}")
        print(f"  Market Cap: ${p.get('mktCap', 0)/1e6:.1f}M")
        print(f"  Avg Volume: {p.get('volAvg', 0):,}")
except Exception as e:
    print(f"  Profile error: {e}")

try:
    news = fmp_get(f"stock_news", {"tickers": sym, "limit": 5})
    if news:
        print(f"\n  Recent news:")
        for n in news[:5]:
            print(f"    [{n.get('publishedDate', '')[:10]}] {n.get('title', '')[:100]}")
except Exception as e:
    print(f"  News error: {e}")


# -----------------------------------------------------------------------
# 5. Check SLND - rejected for "no catalyst" but up 27% today
# -----------------------------------------------------------------------
separator("5. SLND - Rejected for 'No Catalyst' but +27%")

sym = "SLND"
try:
    profile = fmp_get(f"profile/{sym}")
    if profile and isinstance(profile, list):
        p = profile[0]
        print(f"  Company: {p.get('companyName')}")
        print(f"  Sector: {p.get('sector')} / {p.get('industry')}")
        print(f"  Market Cap: ${p.get('mktCap', 0)/1e6:.1f}M")
        print(f"  Avg Volume: {p.get('volAvg', 0):,}")
except Exception as e:
    print(f"  Profile error: {e}")

try:
    news = fmp_get(f"stock_news", {"tickers": sym, "limit": 5})
    if news:
        print(f"\n  Recent news:")
        for n in news[:5]:
            print(f"    [{n.get('publishedDate', '')[:10]}] {n.get('title', '')[:100]}")
except Exception as e:
    print(f"  News error: {e}")


# -----------------------------------------------------------------------
# 6. Check screener result WITHOUT the 50-stock cap to see how AIFF ranks
# -----------------------------------------------------------------------
separator("6. FMP Screener: How many stocks pass penny criteria?")

params = {
    "priceMoreThan": 0.10,
    "priceLowerThan": 5.5,
    "volumeMoreThan": 300000,
    "marketCapMoreThan": 8000000,
    "marketCapLowerThan": 500000000,
    "isEtf": False,
    "isFund": False,
    "isActivelyTrading": True,
    "limit": 250,
}
screener_results = fmp_get("stock-screener", params)
print(f"Total stocks matching penny criteria (no cap): {len(screener_results)}")

# Enrich with quotes to compute RVOL
print(f"\nChecking target stocks in screener results:")
sr_syms = {(r.get("symbol") or "").upper() for r in screener_results}
for t in ["AIFF", "TURB", "HWH", "LNAI", "FATN", "SLND", "ICU"]:
    if t in sr_syms:
        matching = [r for r in screener_results if r.get("symbol") == t]
        if matching:
            r = matching[0]
            idx = next((i for i, x in enumerate(screener_results) if x.get("symbol") == t), -1)
            print(f"  {t}: IN SCREENER at rank #{idx+1}")
        else:
            print(f"  {t}: in sr_syms but no match?")
    else:
        print(f"  {t}: NOT IN SCREENER")

# Show top 60 by checking if we can compute rank
print(f"\nFirst 60 screener results (symbols):")
for i, r in enumerate(screener_results[:60], 1):
    sym = r.get("symbol", "?")
    marker = " <<<" if sym in targets else ""
    print(f"  #{i:2d}: {sym:8} ${r.get('price', 0):.2f} mcap=${r.get('marketCap', 0)/1e6:.0f}M{marker}")


# -----------------------------------------------------------------------
# 7. Fetch pre-market quotes for HWH and TURB to see if they showed up early
# -----------------------------------------------------------------------
separator("7. ICU - Rejected at Confidence 40 (just below 55 threshold)")

sym = "ICU"
try:
    profile = fmp_get(f"profile/{sym}")
    if profile and isinstance(profile, list):
        p = profile[0]
        print(f"  Company: {p.get('companyName')}")
        print(f"  Sector: {p.get('sector')} / {p.get('industry')}")
        print(f"  Market Cap: ${p.get('mktCap', 0)/1e6:.1f}M")
        print(f"  Avg Volume: {p.get('volAvg', 0):,}")
except Exception as e:
    print(f"  Profile error: {e}")

try:
    news = fmp_get(f"stock_news", {"tickers": sym, "limit": 5})
    if news:
        print(f"\n  Recent news:")
        for n in news[:5]:
            print(f"    [{n.get('publishedDate', '')[:10]}] {n.get('title', '')[:100]}")
except Exception as e:
    print(f"  News error: {e}")


print("\n\nDone! See analysis above for improvement suggestions.")
