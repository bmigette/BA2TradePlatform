"""Enumerate the broad FMP universe (cap >= 300M, actively trading US) for the screener
optimization, and compute which symbols still need 1d / 5min OHLCV at the native cache."""
import os

from ba2_providers.screener.metric_store import _fetch_screener_rows
from ba2_common.config import get_app_setting, CACHE_FOLDER

api_key = os.getenv("FMP_API_KEY") or get_app_setting("FMP_API_KEY")
if not api_key:
    raise SystemExit("FMP_API_KEY not configured")

rows = _fetch_screener_rows(api_key)
# cap >= 10M (lets small/micro in) + >= $1M/day dollar-volume liquidity (keeps it tradeable).
# The liquidity floor is the real filter; the cap floor is intentionally low.
CAP_MIN, DV_MIN = 10e6, 1e6
syms = sorted({
    r["symbol"] for r in rows
    if r.get("symbol") and (r.get("marketCap") or 0) >= CAP_MIN and (r.get("price") or 0) > 0
    and (r.get("price") or 0) * (r.get("volume") or 0) >= DV_MIN
    and "." not in r["symbol"]  # drop odd tickers
})
print(f"screener rows: {len(rows)} | cap>=10M & >=$1M/day liq: {len(syms)}")

native = os.path.join(CACHE_FOLDER, "FMPOHLCVProvider")
have = set(os.listdir(native)) if os.path.isdir(native) else set()
miss_1d = [s for s in syms if f"{s}_1d.parquet" not in have]
miss_5min = [s for s in syms if f"{s}_5min.parquet" not in have]
print(f"missing 1d: {len(miss_1d)} | missing 5min: {len(miss_5min)}")

open(r"C:\Users\basti\Documents\dev\broad_universe.txt", "w").write(",".join(syms))
open(r"C:\Users\basti\Documents\dev\broad_missing_1d.txt", "w").write(",".join(miss_1d))
open(r"C:\Users\basti\Documents\dev\broad_missing_5min.txt", "w").write(",".join(miss_5min))
