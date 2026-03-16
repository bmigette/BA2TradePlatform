"""
Test StockTwits data collection using two endpoints via curl_cffi:

1. _next/data endpoint  → symbol metadata (watchlist, sector, price, fundamentals)
2. Official API v2      → message stream with per-message Bullish/Bearish sentiment

Both require curl_cffi to bypass Cloudflare bot protection.

Tests:
1. Single AAPL request showing full parsed output
2. 10 symbols to check rate limiting
"""

import re
import sys
import time
import json
import requests as std_requests

from curl_cffi import requests as cf_requests

BASE_URL = "https://stocktwits.com"
API_V2_BASE = "https://api.stocktwits.com/api/2"

SESSION = cf_requests.Session(impersonate="chrome124")

PAGE_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "cache-control": "no-cache",
    "pragma": "no-cache",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
}

JSON_HEADERS = {
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "cache-control": "no-cache",
    "pragma": "no-cache",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "x-nextjs-data": "1",
}


# ---------------------------------------------------------------------------
# Build ID (cached per session)
# ---------------------------------------------------------------------------

_build_id_cache: str | None = None

def get_build_id() -> str:
    global _build_id_cache
    if _build_id_cache:
        return _build_id_cache

    resp = SESSION.get(f"{BASE_URL}/symbol/AAPL", headers=PAGE_HEADERS, timeout=20)
    resp.raise_for_status()

    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', resp.text, re.DOTALL)
    if not match:
        raise RuntimeError("Could not find __NEXT_DATA__ in StockTwits symbol page")

    next_data = json.loads(match.group(1))
    build_id = next_data.get("buildId")
    if not build_id:
        raise RuntimeError("buildId not found in __NEXT_DATA__")

    _build_id_cache = build_id
    return build_id


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

def fetch_symbol_metadata(build_id: str, symbol: str):
    """Fetch symbol metadata from _next/data endpoint."""
    url = f"{BASE_URL}/_next/data/{build_id}/symbol/{symbol}.json"
    headers = {**JSON_HEADERS, "referer": f"{BASE_URL}/symbol/{symbol}"}
    return SESSION.get(url, params={"symbol": symbol}, headers=headers, timeout=15)


def fetch_message_stream(symbol: str, limit: int = 30):
    """Fetch recent messages from official API v2."""
    url = f"{API_V2_BASE}/streams/symbol/{symbol}.json"
    return SESSION.get(url, params={"limit": limit}, timeout=15)


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------

def parse_symbol(symbol: str, meta_resp, stream_resp) -> dict:
    """Combine metadata and message stream into a unified result dict."""
    result = {
        "symbol": symbol,
        "metadata": {},
        "price": {},
        "sentiment": {},
        "messages": [],
        "errors": [],
    }

    # --- Metadata ---
    if meta_resp.status_code == 200:
        page_props = meta_resp.json().get("pageProps", {})
        d = page_props.get("initialData", {})
        result["metadata"] = {
            "company_name": d.get("title"),
            "exchange": d.get("exchange"),
            "sector": d.get("sector"),
            "industry": d.get("industry"),
            "watchlist_count": d.get("watchlistCount"),
            "instrument_class": d.get("instrumentClass"),
            "trending": d.get("trending"),
            "trending_score": d.get("trendingScore"),
            "logo_url": d.get("logoUrl"),
        }
        pd = d.get("price_data") or {}
        result["price"] = {
            "last": pd.get("last"),
            "open": pd.get("open"),
            "high": pd.get("high"),
            "low": pd.get("low"),
            "volume": pd.get("volume"),
            "change": pd.get("change"),
            "percent_change": pd.get("percent_change"),
            "previous_close": pd.get("previous_close"),
            "timestamp": pd.get("timestamp"),
        }
    else:
        result["errors"].append(f"metadata HTTP {meta_resp.status_code}")

    # --- Message stream + sentiment ---
    if stream_resp.status_code == 200:
        messages = stream_resp.json().get("messages", [])
        bull = 0
        bear = 0
        no_sentiment = 0
        sample = []

        for msg in messages:
            entities = msg.get("entities") or {}
            sent = entities.get("sentiment")
            basic = (sent.get("basic") if sent else None)
            if basic == "Bullish":
                bull += 1
            elif basic == "Bearish":
                bear += 1
            else:
                no_sentiment += 1

            if len(sample) < 5:
                sample.append({
                    "id": msg.get("id"),
                    "body": msg.get("body", "")[:150],
                    "created_at": msg.get("created_at"),
                    "sentiment": basic,
                    "likes": (msg.get("likes") or {}).get("total", 0),
                })

        total_with_sentiment = bull + bear
        result["sentiment"] = {
            "bullish": bull,
            "bearish": bear,
            "no_sentiment_tag": no_sentiment,
            "total_messages": len(messages),
            "bullish_pct": round(bull / total_with_sentiment * 100, 1) if total_with_sentiment else None,
            "bearish_pct": round(bear / total_with_sentiment * 100, 1) if total_with_sentiment else None,
        }
        result["messages"] = sample
    else:
        result["errors"].append(f"stream HTTP {stream_resp.status_code}")

    return result


# ---------------------------------------------------------------------------
# TEST 1: Single symbol
# ---------------------------------------------------------------------------

print("=" * 70)
print("TEST 1 — Full fetch for AAPL")
print("=" * 70)

print("Getting build ID...")
try:
    build_id = get_build_id()
    print(f"  Build ID: {build_id}")
except Exception as e:
    print(f"FAILED: {e}")
    sys.exit(1)

meta = fetch_symbol_metadata(build_id, "AAPL")
stream = fetch_message_stream("AAPL", limit=30)

print(f"Metadata status: {meta.status_code}  |  Stream status: {stream.status_code}")
result = parse_symbol("AAPL", meta, stream)
print(json.dumps(result, indent=2, default=str))


# ---------------------------------------------------------------------------
# TEST 2: 10 symbols rate limit check
# ---------------------------------------------------------------------------

print()
print("=" * 70)
print("TEST 2 — 10 symbols (0.3s delay between each)")
print("=" * 70)

SYMBOLS = ["AAPL", "TSLA", "GME", "AMC", "NVDA", "SPY", "SOUN", "MARA", "PLUG", "RIOT"]

results_summary = []
for i, symbol in enumerate(SYMBOLS):
    t0 = time.time()
    meta = fetch_symbol_metadata(build_id, symbol)
    stream = fetch_message_stream(symbol, limit=30)
    elapsed = time.time() - t0

    parsed = parse_symbol(symbol, meta, stream)
    sc = parsed["sentiment"]
    wl = parsed["metadata"].get("watchlist_count", "?")
    bull = sc.get("bullish", 0)
    bear = sc.get("bearish", 0)
    bpct = f"{sc['bullish_pct']}%" if sc.get("bullish_pct") is not None else "n/a"
    meta_ok = "OK" if meta.status_code == 200 else f"ERR{meta.status_code}"
    stream_ok = "OK" if stream.status_code == 200 else f"ERR{stream.status_code}"

    print(f"  [{i+1:2d}] {symbol:6s} | meta={meta_ok} stream={stream_ok} | {elapsed:.2f}s | "
          f"wl={wl} | Bull={bull}({bpct}) Bear={bear} | errs={parsed['errors']}")

    results_summary.append({
        "symbol": symbol,
        "meta_status": meta.status_code,
        "stream_status": stream.status_code,
        "elapsed_s": round(elapsed, 3),
        "bullish": bull,
        "bearish": bear,
        "errors": parsed["errors"],
    })

    time.sleep(0.3)

print()
all_ok = all(r["meta_status"] == 200 and r["stream_status"] == 200 for r in results_summary)
if all_ok:
    print("All 10 requests succeeded (meta + stream) — no rate limiting.")
else:
    errors = [r for r in results_summary if r["meta_status"] != 200 or r["stream_status"] != 200]
    print(f"Errors on {len(errors)} symbols: {[r['symbol'] for r in errors]}")

avg_time = sum(r["elapsed_s"] for r in results_summary) / len(results_summary)
print(f"Avg combined request time: {avg_time:.2f}s")
