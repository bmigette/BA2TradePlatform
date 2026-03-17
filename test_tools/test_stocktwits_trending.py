"""
Test StockTwits Trending API endpoints.

Tests the three trending discovery endpoints used by StockTwitsTrending:
  - top_watched
  - most_active
  - symbols_enhanced

No authentication required — endpoints are publicly accessible.
An OAuth token can optionally be provided for higher rate limits.

Usage:
    python test_tools/test_stocktwits_trending.py
    python test_tools/test_stocktwits_trending.py --token YOUR_TOKEN --price-max 8.0

The script prints a summary table for each endpoint, then shows the full
deduplicated result from StockTwitsTrending.get_trending_symbols().
"""

import sys
import argparse
from curl_cffi import requests as cf_requests

TRENDING_BASE = "https://api.stocktwits.com/api/2/trending"
# (endpoint_name, json_key_in_response)
ENDPOINTS = [
    ("top_watched", "top_watched"),
    ("most_active", "most_active"),
    ("symbols_enhanced", "symbols"),
]

# ---------------------------------------------------------------------------
# CLI args
# ---------------------------------------------------------------------------

parser = argparse.ArgumentParser(description="Test StockTwits trending endpoints")
parser.add_argument("--token", default="", help="Optional StockTwits OAuth token")
parser.add_argument("--price-max", type=float, default=6.0, help="Max price filter (default 6.0)")
parser.add_argument("--limit", type=int, default=100, help="Symbols per endpoint (default 100)")
args = parser.parse_args()

OAUTH_TOKEN = args.token.strip()
PRICE_MAX = args.price_max
LIMIT = args.limit

SESSION = cf_requests.Session(impersonate="chrome124")
HEADERS = {
    "accept": "application/json",
    "accept-language": "en-US,en;q=0.9",
    "cache-control": "no-cache",
    "origin": "https://stocktwits.com",
    "pragma": "no-cache",
    "referer": "https://stocktwits.com/",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
}
if OAUTH_TOKEN:
    HEADERS["authorization"] = f"OAuth {OAUTH_TOKEN}"

PARAMS = {
    "class": "all",
    "limit": LIMIT,
    "page_num": 1,
    "payloads": "qprices",
    "enable_price_v2": "true",
}


def extract_price(sym_obj: dict) -> float | None:
    """Extract last price from price_data payload."""
    pd = sym_obj.get("price_data") or {}
    try:
        val = float(pd.get("last") or 0)
        if val > 0:
            return val
    except (TypeError, ValueError):
        pass
    return None


def extract_change_pct(sym_obj: dict) -> float | None:
    pd = sym_obj.get("price_data") or {}
    try:
        val = pd.get("percent_change")
        if val is not None:
            return float(val)
    except (TypeError, ValueError):
        pass
    return None


# ---------------------------------------------------------------------------
# Test each endpoint individually
# ---------------------------------------------------------------------------

auth_mode = f"authenticated (token={OAUTH_TOKEN[:8]}...)" if OAUTH_TOKEN else "unauthenticated (public)"
print(f"\nStockTwits Trending API Test  |  price_max=${PRICE_MAX:.2f}  |  limit={LIMIT}  |  {auth_mode}")
print("=" * 80)

all_endpoint_data: dict[str, list] = {}

for endpoint, response_key in ENDPOINTS:
    url = f"{TRENDING_BASE}/{endpoint}.json"
    print(f"\n--- {endpoint} ---")
    print(f"GET {url}")

    try:
        resp = SESSION.get(url, headers=HEADERS, params=PARAMS, timeout=15)
        print(f"HTTP {resp.status_code}")
        resp.raise_for_status()
    except Exception as e:
        print(f"FAILED: {e}")
        all_endpoint_data[endpoint] = []
        continue

    data = resp.json()
    symbols = data.get(response_key, [])
    cursor = data.get("cursor", {})
    print(f"Total symbols returned: {len(symbols)}  |  cursor: {cursor}")

    # Filter and display
    under_price = [s for s in symbols if (extract_price(s) or 999) <= PRICE_MAX]
    print(f"Under ${PRICE_MAX:.2f}: {len(under_price)} symbols")
    print()
    print(f"{'Symbol':<8} {'Price':>7} {'Chg%':>7} {'RVOL':>6} {'Watchlist':>10} {'TrendScore':>11}  Company")
    print("-" * 80)
    for sym_obj in under_price[:30]:
        symbol = (sym_obj.get("symbol") or "").upper()
        price = extract_price(sym_obj) or 0.0
        chg = extract_change_pct(sym_obj)
        chg_str = f"{chg:+.1f}%" if chg is not None else "  n/a"
        fund = sym_obj.get("fundamentals") or {}
        vol = (sym_obj.get("price_data") or {}).get("volume")
        avg_vol = fund.get("average_daily_volume_last_month")
        rvol_str = f"{vol/avg_vol:.1f}x" if vol and avg_vol and avg_vol > 0 else "  n/a"
        wl = sym_obj.get("watchlist_count", "")
        ts = sym_obj.get("trending_score", "")
        name = (sym_obj.get("title") or "")[:28]
        print(f"{symbol:<8} {price:>7.3f} {chg_str:>7} {rvol_str:>6} {str(wl):>10} {str(ts):>11}  {name}")

    if len(under_price) > 30:
        print(f"  ... and {len(under_price) - 30} more")

    all_endpoint_data[endpoint] = symbols


# ---------------------------------------------------------------------------
# Full dedup test via StockTwitsTrending provider
# ---------------------------------------------------------------------------

print()
print("=" * 80)
print(f"Full StockTwitsTrending.get_trending_symbols() result (price_max=${PRICE_MAX:.2f})")
print("=" * 80)

try:
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from ba2_trade_platform.modules.dataproviders.socialmedia.StockTwitsTrending import StockTwitsTrending

    provider = StockTwitsTrending(oauth_token=OAUTH_TOKEN, price_max=PRICE_MAX, limit_per_endpoint=LIMIT)
    results = provider.get_trending_symbols()

    print(f"\nTotal unique symbols: {len(results)}")
    multi_source = [r for r in results if len(r["sources"]) > 1]
    print(f"Appearing in 2+ endpoints: {len(multi_source)}")
    print()
    print(f"{'Symbol':<8} {'Price':>7} {'Chg%':>7} {'RVOL':>6} {'WatchList':>9} {'Sources':<35}  Company")
    print("-" * 85)
    for r in results:
        price = r.get("price") or 0.0
        chg = r.get("change_pct")
        chg_str = f"{chg:+.1f}%" if chg is not None else "  n/a"
        rvol = r.get("rvol")
        rvol_str = f"{rvol:.1f}x" if rvol else "  n/a"
        wl = r.get("watchlist_count", "")
        sources = "+".join(r.get("sources", []))
        name = (r.get("company_name") or "")[:25]
        print(f"{r['symbol']:<8} {price:>7.3f} {chg_str:>7} {rvol_str:>6} {str(wl):>9} {sources:<35}  {name}")

    print(f"\nDone. {len(results)} symbols ready for pipeline injection.")

except Exception as e:
    print(f"StockTwitsTrending provider test FAILED: {e}")
    import traceback
    traceback.print_exc()
