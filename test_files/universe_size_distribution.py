"""Show how many FMP symbols qualify at various cap floors + a liquidity (dollar-volume) filter,
to size the OHLCV fetch for the screener optimization universe."""
import os

from ba2_providers.screener.metric_store import _fetch_screener_rows
from ba2_common.config import get_app_setting

api_key = os.getenv("FMP_API_KEY") or get_app_setting("FMP_API_KEY")
rows = [r for r in _fetch_screener_rows(api_key)
        if r.get("symbol") and (r.get("price") or 0) > 0 and "." not in r["symbol"]]
print(f"actively-trading US rows (price>0): {len(rows)} (FMP screener limit is 10000)")


def count(cap_min, dollar_vol_min=0.0):
    n = 0
    for r in rows:
        cap = r.get("marketCap") or 0
        px = r.get("price") or 0
        vol = r.get("volume") or 0
        if cap >= cap_min and px * vol >= dollar_vol_min:
            n += 1
    return n


caps = [r.get("marketCap") or 0 for r in rows if (r.get("marketCap") or 0) > 0]
print(f"cap range in the 10000 rows: min={min(caps)/1e6:.0f}M  max={max(caps)/1e9:.0f}B")
print("(screener is capped at 10000 rows ~cap-desc, so floors below ~min just return all rows)\n")
print("cap floor   | all     | +$0.5M/day | +$1M/day | +$5M/day")
for label, cap in [("10M", 10e6), ("30M", 30e6), ("50M", 50e6), ("100M", 100e6),
                   ("300M", 300e6), ("1B", 1e9), ("2B", 2e9)]:
    print(f"  >= {label:<6} | {count(cap):>6}  | {count(cap,5e5):>6}     | "
          f"{count(cap,1e6):>6}   | {count(cap,5e6):>6}")
