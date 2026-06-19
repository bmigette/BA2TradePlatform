"""
Test script: AMGN 1m gap analysis & FMP API validation

Steps:
  1. Load existing AMGN_1m.csv cache and report all gaps
  2. For each gap (first 5), attempt a direct FMP API call and report what comes back
  3. Run extend_ohlcv_cache() with full logging to show what the gap-fill
     actually does -- including whether it detects the gaps and whether FMP
     returns data for those periods
"""

import os
import sys
import logging
import requests
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Ensure backend root is on sys.path and load .env
# ---------------------------------------------------------------------------
BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

# Load .env from project root (one level above backend)
from dotenv import load_dotenv
load_dotenv(BACKEND_ROOT.parent / ".env")

# Enable verbose logging so we can see exactly what extend_ohlcv_cache does
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
# Quiet noisy libraries
for noisy in ("urllib3", "httpcore", "httpx", "hpack"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

logger = logging.getLogger("test_amgn_gaps")

GAP_THRESHOLD = pd.Timedelta(days=5)
SYMBOL = "AMGN"
INTERVAL = "1m"
MAX_GAPS_TO_TEST = 5  # Limit FMP API calls in Step 2


# ---------------------------------------------------------------------------
# Step 1 - Analyse existing cache
# ---------------------------------------------------------------------------
def analyse_cache(cache_path: Path) -> pd.DataFrame:
    logger.info(f"\n{'='*60}")
    logger.info(f"STEP 1: Analysing cache: {cache_path}")
    logger.info(f"{'='*60}")

    if not cache_path.exists():
        logger.warning("Cache file does not exist.")
        return pd.DataFrame()

    df = pd.read_csv(cache_path)
    df["Date"] = pd.to_datetime(df["Date"], utc=True, errors="coerce")
    df = df.dropna(subset=["Date"]).sort_values("Date").reset_index(drop=True)

    logger.info(f"  Rows      : {len(df):,}")
    logger.info(f"  Date from : {df['Date'].min()}")
    logger.info(f"  Date to   : {df['Date'].max()}")

    diffs = df["Date"].diff()
    gap_rows = diffs[diffs > GAP_THRESHOLD]

    if gap_rows.empty:
        logger.info("  No gaps found (all consecutive rows <= 5 days apart).")
    else:
        logger.info(f"  Found {len(gap_rows)} gap(s) (showing all):")
        for idx in gap_rows.index:
            gs = df.loc[idx - 1, "Date"]
            ge = df.loc[idx, "Date"]
            days = int((ge - gs).total_seconds() / 86400)
            logger.info(f"    Gap #{idx}: {gs.date()} -> {ge.date()}  ({days} days)")

    return df


# ---------------------------------------------------------------------------
# Step 2 - Direct FMP API call for first N gaps
# ---------------------------------------------------------------------------
def test_fmp_for_gaps(df: pd.DataFrame, api_key: str):
    logger.info(f"\n{'='*60}")
    logger.info(f"STEP 2: Testing FMP API for first {MAX_GAPS_TO_TEST} gap date ranges")
    logger.info(f"{'='*60}")

    if df.empty:
        logger.warning("No cache data -- skipping FMP gap test.")
        return

    diffs = df["Date"].diff()
    gap_rows = diffs[diffs > GAP_THRESHOLD]

    if gap_rows.empty:
        logger.info("No gaps to test.")
        return

    base_url = "https://financialmodelingprep.com/api/v3"

    for i, idx in enumerate(gap_rows.index[:MAX_GAPS_TO_TEST]):
        gs = df.loc[idx - 1, "Date"]
        ge = df.loc[idx, "Date"]
        logger.info(f"\n  --- Testing gap {i+1}/{MAX_GAPS_TO_TEST}: {gs.date()} -> {ge.date()} ---")

        # Try a 30-day window starting from gap_start
        window_end = min(gs + timedelta(days=30), ge)
        url = f"{base_url}/historical-chart/1min/{SYMBOL}"
        params = {
            "apikey": api_key,
            "from": gs.strftime("%Y-%m-%d"),
            "to": window_end.strftime("%Y-%m-%d"),
        }
        logger.info(f"  GET {url}  from={params['from']} to={params['to']}")

        try:
            resp = requests.get(url, params=params, timeout=30)
            logger.info(f"  HTTP {resp.status_code}")
            data = resp.json()
            if isinstance(data, list) and len(data) > 0:
                tmp = pd.DataFrame(data)
                tmp["date"] = pd.to_datetime(tmp["date"], utc=True)
                logger.info(f"  FMP returned {len(tmp)} bars")
                logger.info(f"     Range: {tmp['date'].min()} -> {tmp['date'].max()}")
            elif isinstance(data, list):
                logger.info("  FMP returned an EMPTY list -- no data for this period")
            else:
                logger.info(f"  FMP returned unexpected structure: {str(data)[:200]}")
        except Exception as e:
            logger.error(f"  FMP request failed: {e}")

    logger.info(f"\n  (Skipped remaining {len(gap_rows) - MAX_GAPS_TO_TEST} gaps)")


# ---------------------------------------------------------------------------
# Step 3 - Run extend_ohlcv_cache and observe behaviour
# ---------------------------------------------------------------------------
def test_extend_cache():
    logger.info(f"\n{'='*60}")
    logger.info("STEP 3: Running extend_ohlcv_cache() with full logging")
    logger.info(f"{'='*60}")

    # get_ohlcv_provider returns the shared FMP provider augmented with the
    # backend OHLCV disk-cache layer (extend_ohlcv_cache / _get_cache_file).
    from app.api.datasets import get_ohlcv_provider

    provider = get_ohlcv_provider("fmp")
    end_date = datetime.now()
    start_date = end_date - timedelta(days=15 * 365)

    logger.info(f"  Calling extend_ohlcv_cache({SYMBOL}, {start_date.date()}, {end_date.date()}, '{INTERVAL}')")
    logger.info("  (Watch for 'Found X gap(s)' and 'Gap start->end: N bars' messages below)\n")

    def progress(pct: float, msg: str):
        logger.info(f"  [PROGRESS {pct:5.1f}%] {msg}")

    df = provider.extend_ohlcv_cache(
        symbol=SYMBOL,
        start_date=start_date,
        end_date=end_date,
        interval=INTERVAL,
        progress_callback=progress,
    )

    logger.info(f"\n  extend_ohlcv_cache returned {len(df):,} rows")
    if not df.empty:
        logger.info(f"  Date range: {df['Date'].min()} -> {df['Date'].max()}")

    return df


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    api_key = os.getenv("FMP_API_KEY")
    if not api_key:
        logger.error("FMP_API_KEY environment variable not set. Exiting.")
        sys.exit(1)

    cache_path = BACKEND_ROOT / "datasets" / "cache" / "ohlcv" / "fmp" / f"{SYMBOL}_{INTERVAL}.csv"

    # Step 1 -- analyse cache
    df_cache = analyse_cache(cache_path)

    # Step 2 -- test FMP API for first few gaps
    test_fmp_for_gaps(df_cache, api_key)

    # Step 3 -- run extend_ohlcv_cache and watch what happens
    df_result = test_extend_cache()

    # Step 4 -- re-analyse cache after extend to see if gaps closed
    logger.info(f"\n{'='*60}")
    logger.info("STEP 4: Re-analysing cache after extend_ohlcv_cache()")
    logger.info(f"{'='*60}")
    analyse_cache(cache_path)


if __name__ == "__main__":
    main()
