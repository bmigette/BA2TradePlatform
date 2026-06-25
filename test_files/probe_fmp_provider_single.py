"""Single sequential 5min fetch via the real provider (DB key + global gate).

If ONE sequential fetch succeeds, the 429 wall is a concurrency/throughput issue
(fetch with fewer workers). If even a lone fetch keeps 429-ing, the FMP daily/rate
budget for the 5min endpoint is exhausted and waiting (or a higher tier) is needed.
"""
import time

from ba2_providers.dataproviders.FMPOHLCVProvider import FMPOHLCVProvider

prov = FMPOHLCVProvider()
t0 = time.time()
try:
    df = prov.get_ohlcv("AAPL", "5min", start="2025-12-01", end="2025-12-05")
    n = 0 if df is None else len(df)
    print(f"OK: AAPL 5min rows={n} in {time.time()-t0:.1f}s")
    if n:
        print("first/last:", df.index.min(), df.index.max())
except Exception as exc:  # noqa: BLE001
    print(f"FAIL after {time.time()-t0:.1f}s: {type(exc).__name__}: {exc}")
