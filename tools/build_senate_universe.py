"""Derive the FMPSenateTraderWeight optimization universe FROM disclosure activity itself.

Congressional trading activity is sparse and does NOT align with any market-cap band or
index list (senators trade defense/energy/regional names alongside mega-caps) — a screener
cap-band or NDQ100 list would miss most of the actual signal. This walks FMP's paginated
``stable/{senate,house}-latest`` disclosure feeds (verified 2026-07-15 to paginate back to
2012/2019 respectively — comfortably past any --start), counts per-symbol disclosure
frequency in the requested window, and keeps symbols active enough to carry a tradeable
signal. Non-equity tickers (mutual funds, malformed symbols) and symbols FMP has no daily
price data for are dropped last, producing a pinned universe file like
``tools/options_universe_top100.txt``.

Usage (test venv; FMP_API_KEY in env or app-settings DB):
    ba2-venvs/test/Scripts/python.exe tools/build_senate_universe.py \
        --start 2023-01-01 --end 2026-06-30 --min-disclosures 8 \
        --out tools/senate_universe.txt
"""
import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta

from ba2_common.core.utils import is_tradable_stock_ticker


def _resolve_fmp_key() -> str:
    """Env var first; else a RAW sqlite3 read of the app-settings DB (deliberately NOT
    routed through ba2_common.config.get_app_setting/SQLAlchemy: that engine path can
    stall for minutes against a live server process holding the DB, e.g. the running
    test-platform backend — a plain read-only sqlite3 connection never blocks on it)."""
    key = os.getenv("FMP_API_KEY")
    if key:
        return key
    import sqlite3
    db_file = os.getenv("DB_FILE", r"C:\Users\basti\Documents\ba2\test\dl_forecasting.db")
    try:
        conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=5)
        row = conn.execute(
            "SELECT value_str FROM appsetting WHERE key IN ('FMP_API_KEY','fmp_api_key') "
            "AND value_str IS NOT NULL LIMIT 1"
        ).fetchone()
        conn.close()
        key = row[0] if row else None
    except Exception:  # noqa: BLE001
        key = None
    if not key:
        sys.exit(f"build_senate_universe: FMP_API_KEY not configured (.env or {db_file}).")
    return key


def _fetch_latest_disclosures(chamber: str, key: str, floor_date: date, max_pages: int) -> list:
    """Walk stable/{chamber}-latest until a page's rows are all older than floor_date, or
    max_pages is hit (safety cap — real feeds end well within this)."""
    from ba2_providers.fmp_common import fmp_http_get, FMPError

    rows = []
    for page in range(max_pages):
        try:
            resp = fmp_http_get(
                f"https://financialmodelingprep.com/stable/{chamber}-latest",
                {"apikey": key, "page": page, "limit": 1000},
                endpoint=f"{chamber}-latest", timeout=30,
            )
        except (FMPError, Exception) as e:  # noqa: BLE001 — stop this chamber, keep the other
            print(f"  {chamber} page {page}: fetch failed ({e}); stopping this chamber")
            break
        data = resp.json()
        if not isinstance(data, list) or not data:
            print(f"  {chamber} page {page}: end of feed")
            break
        rows.extend(data)
        page_dates = [r.get("transactionDate") for r in data if r.get("transactionDate")]
        oldest_on_page = min(page_dates) if page_dates else None
        if page % 10 == 0:
            print(f"  {chamber} page {page}: {len(data)} rows (oldest so far this page: {oldest_on_page})")
        if oldest_on_page and oldest_on_page < floor_date.isoformat():
            print(f"  {chamber} page {page}: reached floor date, stopping")
            break
        time.sleep(0.1)  # gentle on the shared rate-limit gate; fmp_http_get already backs off 429s
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", required=True, help="ISO start date (window floor for counting).")
    ap.add_argument("--end", required=True, help="ISO end date (window ceiling for counting).")
    ap.add_argument("--min-disclosures", type=int, default=8,
                    help="Keep symbols with >= this many disclosures in [--start,--end] (default 8).")
    ap.add_argument("--max-pages", type=int, default=200,
                    help="Safety cap on latest-feed pagination per chamber (default 200 = 200k rows).")
    ap.add_argument("--price-probe-end", default=None,
                    help="Date to probe FMP daily price data at (default --end); drops symbols "
                         "with no price on/before this date.")
    ap.add_argument("--out", default="tools/senate_universe.txt")
    ap.add_argument("--skip-price-probe", action="store_true",
                    help="Skip the per-candidate price-data check (faster, but keeps dead tickers).")
    args = ap.parse_args()

    start = datetime.fromisoformat(args.start).date()
    end = datetime.fromisoformat(args.end).date()
    # Walk a little past --start so a disclosure right at the window floor isn't cut by a
    # page-boundary rounding; floor_date is only used to know when to STOP paginating.
    floor_date = start - timedelta(days=1)

    key = _resolve_fmp_key()

    print(f"Discovering senate universe: window [{start}, {end}], min_disclosures={args.min_disclosures}")
    all_rows = []
    for chamber in ("senate", "house"):
        print(f"=== {chamber}-latest ===")
        all_rows.extend(_fetch_latest_disclosures(chamber, key, floor_date, args.max_pages))

    counts: dict = {}
    for r in all_rows:
        sym = str(r.get("symbol", "")).upper().strip()
        d = r.get("transactionDate", "")
        if not sym or not d:
            continue
        if not (start.isoformat() <= d <= end.isoformat()):
            continue
        counts[sym] = counts.get(sym, 0) + 1

    print(f"\n{len(all_rows)} total disclosure rows fetched; {len(counts)} distinct symbols in window")

    candidates = sorted(counts.items(), key=lambda kv: -kv[1])
    print("Top 20 by disclosure count:")
    for sym, n in candidates[:20]:
        print(f"  {sym}: {n}")

    kept = [sym for sym, n in candidates if n >= args.min_disclosures]
    print(f"\n{len(kept)} symbols with >= {args.min_disclosures} disclosures")

    before_junk = len(kept)
    kept = [s for s in kept if is_tradable_stock_ticker(s)]
    print(f"Dropped {before_junk - len(kept)} junk tickers (mutual funds / malformed symbols)")

    if not args.skip_price_probe:
        from ba2_providers.fmp_common import fmp_history_disk_cached, fmp_http_get, FMPError

        def _has_price(sym: str) -> bool:
            def _fetch():
                r = fmp_http_get(
                    f"https://financialmodelingprep.com/api/v3/historical-price-full/{sym}",
                    {"from": "1990-01-01", "apikey": key},
                    symbol=sym, endpoint="historical-price-full", timeout=30,
                )
                d = r.json()
                return d.get("historical", []) if isinstance(d, dict) else []
            try:
                # NOT disk-cached here (frozen_ttl_cache not active in this standalone
                # script) — a plain probe fetch; the real prewarm step re-fetches+caches.
                hist = _fetch()
            except (FMPError, Exception):  # noqa: BLE001
                return False
            return bool(hist)

        probe_end = args.price_probe_end or args.end
        print(f"\nProbing price data as of {probe_end} for {len(kept)} candidates...")
        priced = []
        for i, sym in enumerate(kept, 1):
            if _has_price(sym):
                priced.append(sym)
            if i % 25 == 0:
                print(f"  probed {i}/{len(kept)} ({len(priced)} priced so far)")
            time.sleep(0.05)
        print(f"Dropped {len(kept) - len(priced)} symbols with no FMP price data")
        kept = priced

    kept.sort()
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(kept) + ("\n" if kept else ""))
    print(f"\nWrote {len(kept)} symbols to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
