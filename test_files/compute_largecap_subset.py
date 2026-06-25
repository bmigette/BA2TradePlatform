"""How many store symbols ever reach >=10B / >=2B cap (bounds scr-large / scr-mid prefetch)."""
import os

from ba2_providers.screener import metric_store as ms

STORE = r"C:\Users\basti\Documents\ba2\common\cache\screener\metric_store"
CACHE = r"C:\Users\basti\Documents\ba2\common\cache\FMPOHLCVProvider"

df = ms.load_store(STORE)
have5 = {f[:-13] for f in os.listdir(CACHE) if f.endswith("_5min.parquet")}

# A symbol qualifies for a flavour if it EVER hits the cap floor in-window.
peak = df.groupby("symbol")["market_cap"].max()
for label, floor in (("large >=10B", 10e9), ("mid >=2B", 2e9), ("small >=0.3B", 0.3e9)):
    syms = sorted(peak[peak >= floor].index)
    missing = [s for s in syms if s not in have5]
    print(f"{label:14} symbols={len(syms):4d}  already5min={len(syms)-len(missing):4d}  missing={len(missing):4d}")
    if floor == 10e9:
        open(r"C:\Users\basti\Documents\dev\largecap_missing_5min.txt", "w").write(",".join(missing))
