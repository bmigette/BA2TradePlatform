from datetime import datetime, timezone

import pandas as pd

from ba2_providers.cache import native_cache as nc
from ba2_common.core.provider_utils import insider_effective_date, parse_provider_date


def test_event_upsert_and_no_lookahead_read():
    rows = [
        {"insider_name": "A", "transactionDate": "2026-01-05", "filingDate": "2026-01-08", "v": 1},
        {"insider_name": "B", "transactionDate": "2026-02-01", "filingDate": "2026-02-20", "v": 2},
    ]
    nc.upsert_event_rows(
        "FMPInsiderProvider", "insider_txn", "AAPL", rows,
        value_date_fn=lambda r: parse_provider_date(r["transactionDate"]),
        effective_date_fn=insider_effective_date)
    # as_of before B's filingDate => only A is knowable
    as_of = datetime(2026, 2, 10, tzinfo=timezone.utc)
    got = nc.read_event_rows("FMPInsiderProvider", "insider_txn", "AAPL", as_of)
    names = {r["insider_name"] for r in got}
    assert names == {"A"}, f"lookahead leak: {names}"


def test_event_read_after_both_effective():
    # Both filings are public once as_of passes B's filingDate.
    as_of = datetime(2026, 3, 1, tzinfo=timezone.utc)
    got = nc.read_event_rows("FMPInsiderProvider", "insider_txn", "AAPL", as_of)
    names = {r["insider_name"] for r in got}
    assert names == {"A", "B"}, f"missing rows: {names}"


def test_event_read_value_window_filter():
    # value_from clips on value_date (transactionDate), not effective_date.
    as_of = datetime(2026, 3, 1, tzinfo=timezone.utc)
    got = nc.read_event_rows("FMPInsiderProvider", "insider_txn", "AAPL", as_of,
                             value_from=datetime(2026, 1, 20, tzinfo=timezone.utc))
    names = {r["insider_name"] for r in got}
    assert names == {"B"}, f"value window wrong: {names}"


def test_event_upsert_dedupes_by_payload_hash():
    row = {"insider_name": "C", "transactionDate": "2026-03-01", "filingDate": "2026-03-02", "v": 9}
    n1 = nc.upsert_event_rows("FMPInsiderProvider", "insider_txn", "TSLA", [row],
                              value_date_fn=lambda r: parse_provider_date(r["transactionDate"]),
                              effective_date_fn=insider_effective_date)
    n2 = nc.upsert_event_rows("FMPInsiderProvider", "insider_txn", "TSLA", [row],
                              value_date_fn=lambda r: parse_provider_date(r["transactionDate"]),
                              effective_date_fn=insider_effective_date)
    assert n1 == 1  # first write persists the row
    assert n2 == 0  # second write is a no-op (dedupe)


def test_event_cache_hit_counting():
    nc.reset_stats()
    # A hit: TSLA row written above is readable as_of after its filingDate.
    as_of = datetime(2026, 4, 1, tzinfo=timezone.utc)
    got = nc.read_event_rows("FMPInsiderProvider", "insider_txn", "TSLA", as_of)
    assert {r["insider_name"] for r in got} == {"C"}
    assert nc.STATS.hits == 1 and nc.STATS.misses == 0
    # A miss: no rows for an unknown symbol.
    empty = nc.read_event_rows("FMPInsiderProvider", "insider_txn", "NOPE", as_of)
    assert empty == []
    assert nc.STATS.hits == 1 and nc.STATS.misses == 1


def test_timeseries_asof_slice():
    df = pd.DataFrame({"Date": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03"]),
                       "Close": [10, 11, 12]})
    df["effective_date"] = df["Date"]
    nc.write_timeseries("FMPOHLCVProvider", "AAPL", "1d", df)
    sliced = nc.read_timeseries("FMPOHLCVProvider", "AAPL", "1d",
                                datetime(2026, 1, 2, tzinfo=timezone.utc))
    assert list(sliced["Close"]) == [10, 11]


def test_timeseries_asof_none_returns_all():
    sliced = nc.read_timeseries("FMPOHLCVProvider", "AAPL", "1d", None)
    assert list(sliced["Close"]) == [10, 11, 12]


def test_timeseries_miss_returns_none():
    nc.reset_stats()
    assert nc.read_timeseries("FMPOHLCVProvider", "NOPE", "1d",
                              datetime(2026, 1, 2, tzinfo=timezone.utc)) is None
    assert nc.STATS.misses == 1 and nc.STATS.hits == 0
