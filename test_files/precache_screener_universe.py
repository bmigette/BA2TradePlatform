"""Pre-cache per-symbol FMP history for the SCREENER UNIVERSE before screener-based optimization.

Screener-based opt screens a DIFFERENT subset of symbols each scan day, so every grid expert
(FMPRating / FMPEarningsDrift / FMPInsiderClusterBuy) must have its per-symbol history on disk
for ANY symbol the screener could admit — otherwise the GA individuals cold-fetch from FMP and
either crawl or 429-storm. This warms that whole universe up front.

The universe is read FROM THE BUILT METRIC STORE — the same universe ``build-screener-metrics``
resolves, but already cleaned: ``enumerate_universe`` (cap>=floor) raw also admits mutual funds /
ETFs (huge AUM "marketCap"), which the metric_store drops because they have no tradeable OHLCV.
So the store's distinct symbols ARE the real screener universe; pre-caching that exact set keeps
the expert-history cache aligned with what the screener can actually admit. (``--from-enumerate``
falls back to the raw screener if the store isn't built yet — noisier, includes funds.)

The actual caching is delegated to ``ba2-test prewarm`` (it engages the backtest frozen disk
cache + threadpool correctly); this script only resolves the universe and shells out, so coverage
can't drift from prewarm's real fetch surface. NOTE: this is COMPLEMENTARY to build-metrics —
build-metrics caches the SCREEN metrics (market_cap/rvol/weinstein the screener filters on); this
caches the per-expert ANALYSIS history (ratings/earnings/insider) used AFTER a symbol is screened.

Usage (test venv; FMP_API_KEY/DB_FILE inherited by the prewarm subprocess):
    ba2-venvs/test/Scripts/python.exe test_files/precache_screener_universe.py \
        [--cap-min 2e9] [--workers 3] \
        [--experts FMPRating,FMPEarningsDrift,FMPInsiderClusterBuy] [--end ISO] [--dry-run]

Run with a low --workers (default 3) while the broad 5min fetch is still downloading so the two
stay within the FMP 750/min budget. prewarm is resumable (disk-cached), so it can be paused/rerun.
"""
import argparse
import os
import sys

# Screener optimization's loosest cap gene floor (ba2test_launcher._SCREENER_OPT
# ['screener_market_cap_min']['min']). The metric_store + this pre-cache must cover down to it.
_SCREENER_CAP_FLOOR = 2e9
_DEFAULT_EXPERTS = "FMPRating,FMPEarningsDrift,FMPInsiderClusterBuy"


def _resolve_key() -> str:
    key = os.getenv("FMP_API_KEY")
    if key:
        return key
    try:
        from ba2_common.config import get_app_setting
        return get_app_setting("FMP_API_KEY")
    except Exception:  # noqa: BLE001
        return ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cap-min", type=float, default=_SCREENER_CAP_FLOOR,
                    help=f"Market-cap floor for the universe (default {_SCREENER_CAP_FLOOR:.0e} = screener gene floor).")
    ap.add_argument("--price-min", type=float, default=0.0)
    ap.add_argument("--volume-min", type=float, default=0.0)
    ap.add_argument("--workers", type=int, default=3,
                    help="prewarm fetch threads (default 3 — conservative while the 5min fetch runs).")
    ap.add_argument("--experts", default=_DEFAULT_EXPERTS)
    ap.add_argument("--batch-size", type=int, default=700,
                    help="Symbols per prewarm subprocess (chunked to stay under the Windows cmdline limit).")
    ap.add_argument("--end", default=None, help="ISO end date for the in-Python filter (default now).")
    ap.add_argument("--store", default=None, help="Metric-store dir (default = SCREENER_STORE_DIR).")
    ap.add_argument("--from-enumerate", action="store_true",
                    help="Resolve the universe from the raw FMP screener instead of the built store "
                         "(noisier — includes mutual funds/ETFs; use only if the store isn't built).")
    ap.add_argument("--include-funds", action="store_true",
                    help="Do NOT exclude ETFs/mutual funds (default: exclude them — experts don't apply).")
    ap.add_argument("--dry-run", action="store_true", help="Print the universe size and exit (no fetching).")
    args = ap.parse_args()

    key = _resolve_key()
    if not key:
        sys.exit("FMP_API_KEY not configured (set env or the app-settings DB via DB_FILE).")

    from ba2_providers.screener import metric_store as ms
    if args.from_enumerate:
        rows = ms.enumerate_universe(key, args.cap_min, args.price_min, args.volume_min)
        syms = sorted({r["symbol"] for r in rows if r.get("symbol")})
        src = f"raw screener enumerate (cap>={args.cap_min:.2e}, includes funds)"
    else:
        store = args.store
        if not store:
            from ba2_common.config import SCREENER_STORE_DIR
            store = SCREENER_STORE_DIR
        df = ms.load_store(store)
        if df is None or getattr(df, "empty", True):
            sys.exit(f"metric store empty at {store} — build it first "
                     f"(ba2-test build-screener-metrics) or pass --from-enumerate.")
        syms = sorted(df["symbol"].dropna().astype(str).unique().tolist())
        src = f"metric store {store}"
    print(f"screener universe: {len(syms)} symbols from {src} (experts: {args.experts})")
    if not syms:
        sys.exit("resolved 0 symbols — check the store / cap floor / FMP key.")

    # EXCLUDE ETFs/mutual funds (default): the grade/earnings/insider experts don't apply to them,
    # so they return empty -> write NO cache file -> get re-fetched on every run (slow, wasted FMP
    # calls) and never count as done. ~half the broad universe is funds; dropping them roughly
    # halves the work and removes the re-fetch churn. The FMP screener row carries isEtf/isFund.
    if not args.include_funds:
        funds = {r["symbol"] for r in ms._fetch_screener_rows(key)
                 if r.get("symbol") and (r.get("isEtf") or r.get("isFund"))}
        before = len(syms)
        syms = [s for s in syms if s not in funds]
        print(f"excluded {before - len(syms)} ETFs/funds -> {len(syms)} real equities")
        if not syms:
            sys.exit("all symbols were funds — pass --include-funds to override.")
    if args.dry_run:
        print("first 40:", ",".join(syms[:40]))
        return 0

    # Delegate the actual caching to the installed `ba2-test prewarm` (same venv as this python).
    # CHUNK the symbol list: 9k+ symbols would overflow the Windows ~32K command-line limit, so
    # run prewarm in batches (each is resumable — disk-cached — so a failed batch can be retried).
    import subprocess
    exe = os.path.join(os.path.dirname(sys.executable), "ba2-test.exe")
    if not os.path.exists(exe):
        exe = os.path.join(os.path.dirname(sys.executable), "ba2-test")

    def _chunks(lst, n):
        for i in range(0, len(lst), n):
            yield lst[i:i + n]

    batches = list(_chunks(syms, args.batch_size))
    print(f">> prewarm in {len(batches)} batch(es) of <= {args.batch_size} "
          f"(experts={args.experts}, workers={args.workers})")
    rc = 0
    for i, batch in enumerate(batches, 1):
        cmd = [exe, "prewarm", "--symbols", ",".join(batch),
               "--experts", args.experts, "--workers", str(args.workers)]
        if args.end:
            cmd += ["--end", args.end]
        print(f">> [batch {i}/{len(batches)}] {len(batch)} symbols ...", flush=True)
        r = subprocess.run(cmd, env=os.environ.copy()).returncode
        if r != 0:
            print(f">> batch {i} returned {r} (continuing — disk cache is incremental).")
            rc = r
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
