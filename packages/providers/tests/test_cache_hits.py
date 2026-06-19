"""Cache-hit-count regression gate (Task 11 Step 1).

Proves the native cache actually serves slices from cache rather than re-fetching:
  - a second identical read of a primed key is a STATS.hit and issues ZERO new
    fetches (the cache substrate never calls a fetch_impl on a hit);
  - the design's caching claim (a 50-symbol x 500-bar slice loop issues ~50 fetches,
    NOT 25 000) holds with a fetch-counting fake provider.

These use the temp DB + temp CACHE_FOLDER from the providers conftest, so nothing
touches the real ~/Documents/.../cache tree.
"""
from datetime import datetime, timezone

import pandas as pd

from ba2_providers.cache import native_cache as nc
from ba2_common.core.provider_utils import insider_effective_date, parse_provider_date


# ---------------------------------------------------------------------------
# Step 1a: second identical read is a cache HIT and issues zero new fetches.
# ---------------------------------------------------------------------------
def test_second_read_is_cache_hit():
    """Prime the event cache once, then two reads: both hit the cache (no fetch
    path), and STATS records hits with zero misses for the populated key."""
    nc.reset_stats()
    rows = [
        {"insider_name": "H1", "transactionDate": "2026-01-05",
         "filingDate": "2026-01-08", "v": 1},
    ]
    nc.upsert_event_rows(
        "FMPInsiderProvider", "insider_txn", "HITSYM", rows,
        value_date_fn=lambda r: parse_provider_date(r["transactionDate"]),
        effective_date_fn=insider_effective_date)

    as_of = datetime(2026, 6, 1, tzinfo=timezone.utc)
    first = nc.read_event_rows("FMPInsiderProvider", "insider_txn", "HITSYM", as_of)
    assert {r["insider_name"] for r in first} == {"H1"}
    assert nc.STATS.hits == 1 and nc.STATS.misses == 0

    second = nc.read_event_rows("FMPInsiderProvider", "insider_txn", "HITSYM", as_of)
    assert second == first
    # Second read served entirely from cache: another hit, still zero misses,
    # and the cache substrate never bumped STATS.fetches (no fetch_impl called).
    assert nc.STATS.hits == 2 and nc.STATS.misses == 0
    assert nc.STATS.fetches == 0


# ---------------------------------------------------------------------------
# Step 1b: a many-symbol slice loop fetches once per symbol, not once per slice.
# ---------------------------------------------------------------------------
class _CountingTimeSeriesProvider:
    """Fake OHLCV-style source that counts how many times it actually fetches.

    A read-through cache (read_timeseries -> miss -> fetch -> write_timeseries ->
    read_timeseries hits forever after) fetches a symbol AT MOST once even when
    sliced at 500 different as_of points; without caching it would refetch on
    every slice (50 * 500 = 25 000)."""

    def __init__(self, bars: int = 500):
        self.fetch_count = 0
        self._bars = bars

    def fetch(self, symbol: str):
        self.fetch_count += 1
        dates = pd.date_range("2024-01-01", periods=self._bars, freq="D", tz="UTC")
        df = pd.DataFrame({
            "Date": dates,
            "Close": range(self._bars),
        })
        df["effective_date"] = df["Date"]
        return df


def test_slice_loop_fetches_once_per_symbol_not_per_slice():
    """50 symbols x 500 daily as_of slices => ~50 fetches (one per symbol on the
    first miss), NOT 25 000. Demonstrates the native cache's slice-serving claim."""
    nc.reset_stats()
    src = _CountingTimeSeriesProvider(bars=500)

    symbols = [f"SYM{i:03d}" for i in range(50)]
    slice_dates = pd.date_range("2024-06-01", periods=500, freq="D", tz="UTC")

    reads = 0
    for sym in symbols:
        for as_of in slice_dates:
            df = nc.read_timeseries("CountingProvider", sym, "1d", as_of.to_pydatetime())
            if df is None:
                # First miss for this symbol: fetch the full series once, persist it.
                full = src.fetch(sym)
                nc.write_timeseries("CountingProvider", sym, "1d", full)
                df = nc.read_timeseries("CountingProvider", sym, "1d", as_of.to_pydatetime())
            assert df is not None
            reads += 1

    assert reads == 50 * 500
    # Exactly one real fetch per symbol despite 25 000 slice reads.
    assert src.fetch_count == 50, f"expected 50 fetches, got {src.fetch_count}"
    # The cache served the overwhelming majority of reads from disk.
    assert nc.STATS.hits >= 50 * 500 - 50
    assert nc.STATS.misses == 50  # exactly one miss per symbol (the priming read)
