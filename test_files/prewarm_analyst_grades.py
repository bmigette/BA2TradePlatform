"""Warm the analyst_grades disk cache (dated individual FMP `grades`) for the screener universe.

The new FMPRating rating-recency filter (max_analyst_age_months) reads per-analyst DATED grades
from FMP's `stable/grades` endpoint. That cache didn't exist before, so a hermetic backtest that
explores the gene would fault ("not pre-warmed"). This warms it for every screener-store symbol,
matching the prewarm semantics: frozen_ttl_cache() (engages the backtest-only disk layer) +
persist_empty_sentinel() (a no-coverage symbol is cached as `[]` -> "checked, no data").

Usage (test venv; FMP_API_KEY in env):
    ba2-venvs/test/Scripts/python.exe test_files/prewarm_analyst_grades.py [--workers 8]
"""
import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed


def main() -> int:
    import ba2_common.config as cfg
    from ba2_providers.screener import metric_store as ms
    from ba2_providers.fmp_common import frozen_ttl_cache, persist_empty_sentinel
    from ba2_experts.FMPRating import fetch_analyst_grades_cached

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workers", type=int, default=8, help="Parallel fetch threads (default 8).")
    ap.add_argument("--store", default=os.path.join(cfg.CACHE_FOLDER, "screener", "metric_store"))
    args = ap.parse_args()

    key = os.getenv("FMP_API_KEY")
    if not key:
        sys.exit("prewarm-analyst-grades: FMP_API_KEY not configured.")

    df = ms.load_store(args.store)
    symbols = sorted(df["symbol"].unique())
    print(f"prewarm-analyst-grades: warming {len(symbols)} symbols (workers={args.workers})...",
          flush=True)

    ok = err = 0
    t0 = time.time()
    with frozen_ttl_cache(), persist_empty_sentinel():
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
            futs = {ex.submit(fetch_analyst_grades_cached, key, s): s for s in symbols}
            for i, fut in enumerate(as_completed(futs), 1):
                sym = futs[fut]
                try:
                    fut.result()
                    ok += 1
                except Exception as e:  # noqa: BLE001 — one bad symbol must not abort the warm
                    err += 1
                    if err <= 20:
                        print(f"  ERR {sym}: {type(e).__name__}: {e}", flush=True)
                if i % 500 == 0:
                    print(f"  {i}/{len(symbols)}  ok={ok} err={err}  ({time.time()-t0:.0f}s)",
                          flush=True)
    print(f"prewarm-analyst-grades: done ok={ok} err={err} in {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
