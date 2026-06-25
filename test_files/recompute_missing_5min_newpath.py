"""Recompute screener symbols missing 5min at the NEW ohlcv/fmp cache path."""
import os

from ba2_providers.screener import metric_store as ms

STORE = r"C:\Users\basti\Documents\ba2\common\cache\screener\metric_store"
NEW = r"C:\Users\basti\Documents\ba2\common\cache\ohlcv\fmp"

syms = sorted(ms.load_store(STORE)["symbol"].unique())
have = {f[:-13] for f in os.listdir(NEW) if f.endswith("_5min.parquet")} if os.path.isdir(NEW) else set()
missing = [s for s in syms if s not in have]
print(f"store={len(syms)} have5min(newpath)={len(syms)-len(missing)} missing={len(missing)}")
open(r"C:\Users\basti\Documents\dev\missing_5min_newpath.txt", "w").write(",".join(missing))
