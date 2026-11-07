"""Alpha Vantage HISTORICAL_OPTIONS store: mapping, resume ledger, compaction, reader compatibility.

Fixture ``data/av_historical_options_nvda_2024-05-31.json`` is 22 REAL contracts of the NVDA 2024-05-31 chain
(trimmed from the 9,496-row response): traded rows, stale-``last`` no-trade rows, a bid-0 quote, as-traded
pre-split strikes (the 10:1 split was 2024-06-10; the spot was ~$1,100).
"""
import json
import os
from datetime import date

import pytest

from ba2_providers.options import OPTIONS_HISTORY_PROVIDERS, options_history_floor
from ba2_providers.options import alphavantage as av
from ba2_providers.options.parquet_store import OptionHistoryParquetStore

FIXTURE = os.path.join(os.path.dirname(__file__), "data", "av_historical_options_nvda_2024-05-31.json")
DAY = date(2024, 5, 31)


@pytest.fixture
def rows():
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)["data"]


def _row(**kw):
    base = {"contractID": "NVDA240607C01100000", "symbol": "NVDA", "expiration": "2024-06-07", "strike": "1100.00",
            "type": "call", "last": "27.50", "mark": "28.20", "bid": "27.60", "bid_size": "3", "ask": "28.80",
            "ask_size": "4", "volume": "10", "open_interest": "6164", "date": "2024-05-31",
            "implied_volatility": "0.48316", "delta": "0.5", "gamma": "0.01", "theta": "-1.2", "vega": "0.9",
            "rho": "0.2"}
    base.update(kw)
    return base


def test_registered_with_a_floor():
    assert "alphavantage" in OPTIONS_HISTORY_PROVIDERS
    assert options_history_floor("alphavantage") == date(2020, 1, 1)


def test_close_is_last_only_when_the_contract_traded(rows):
    df, stats = av.map_chain("NVDA", DAY, rows)
    assert stats.rows_in == len(rows) == 22 and len(df) == stats.rows_out
    by = df.set_index("occ_symbol")
    traded = by.loc["NVDA240607C01100000"]
    assert traded["close"] == 27.50 and traded["volume"] == 29492
    # volume 0 with a STALE last of 859.74: the print is not today's -> no close, the quote stays
    stale = by.loc["NVDA240531C00290000"]
    assert stale["volume"] == 0 and stale["close"] != stale["close"]            # NaN
    assert stale["bid"] == 797.95 and stale["ask"] == 812.05 and stale["mark"] == 805.0
    # close > 0 iff volume > 0 on the whole fixture (ThetaData's invariant)
    assert ((df["close"].notna()) == (df["volume"] > 0)).all()


def test_ohlc_is_close_only_and_open_equals_close(rows):
    df, _ = av.map_chain("NVDA", DAY, rows)
    t = df[df["close"].notna()]
    assert (t["open"] == t["close"]).all() and (t["high"] == t["close"]).all() and (t["low"] == t["close"]).all()
    nt = df[df["close"].isna()]
    assert nt["open"].isna().all()                # no trade -> no fill reference, like ThetaData


def test_strikes_are_as_traded_pre_split(rows):
    df, _ = av.map_chain("NVDA", DAY, rows)
    near = df[df["expiry"] == "2024-06-07"]["strike"]
    assert near.min() == 1095.0 and near.max() == 1110.0     # not 109.5 / 111: no split adjustment


def test_schema_is_the_thetadata_columns_plus_vendor_extras(rows):
    df, _ = av.map_chain("NVDA", DAY, rows)
    assert list(df.columns) == list(OptionHistoryParquetStore.COLUMNS) + list(av.EXTRA_COLUMNS)
    assert str(df["volume"].dtype) == "Int64" and str(df["open_interest"].dtype) == "Int64"
    assert str(df["bid"].dtype) == "float64" and str(df["bar_date"].dtype) == "string"
    assert df.loc[df.occ_symbol == "NVDA240607C01100000", "delta"].notna().all()


def test_quote_iv_and_drop_rules():
    df, stats = av.map_chain("NVDA", DAY, [
        _row(),                                                              # good
        _row(contractID="NVDA240607C01101000", ask="0", bid="0", volume="0", last="0"),   # no trade, no quote
        _row(contractID="NVDA240607C01102000", ask="0", bid="0", volume="5", last="3"),   # traded, ask 0 -> no quote
        _row(contractID="NVDA240607C01103000", bid="0", ask="0.05", volume="0", last="0"),  # bid 0 + ask: real quote
        _row(contractID="NVDA240607C01104000", implied_volatility="0"),
        _row(contractID="NVDA240607C01105000", implied_volatility="250.0"),
        _row(contractID="garbage"), _row(contractID="NVDA1240607C01100000"),      # unparsable / odd root
        _row(contractID="NVDA240607C01106000", date="2024-05-30"),
    ])
    by = df.set_index("occ_symbol")
    assert stats.dropped_priceless == 1 and stats.dropped_unparsable >= 1 and stats.dropped_wrong_date == 1
    assert "NVDA240607C01101000" not in by.index
    r = by.loc["NVDA240607C01102000"]
    assert r["close"] == 3.0 and r["bid"] != r["bid"] and r["ask"] != r["ask"]
    r = by.loc["NVDA240607C01103000"]
    assert r["bid"] == 0.0 and r["ask"] == 0.05 and r["close"] != r["close"]
    assert by.loc["NVDA240607C01104000", "iv"] != by.loc["NVDA240607C01104000", "iv"]
    assert by.loc["NVDA240607C01105000", "iv"] != by.loc["NVDA240607C01105000", "iv"]


def test_parse_payload_distinguishes_throttle_error_and_empty():
    assert av.parse_payload({"data": []}, "X", DAY) == []
    with pytest.raises(av.AlphaVantageThrottled):
        av.parse_payload({"Information": "Our standard API rate limit is 75 requests per minute"}, "X", DAY)
    with pytest.raises(av.AlphaVantageOptionsError):
        av.parse_payload({"Error Message": "bad"}, "X", DAY)
    with pytest.raises(av.AlphaVantageOptionsError):
        av.parse_payload({"message": "nope"}, "X", DAY)


def test_errors_never_carry_the_api_key():
    class Boom:
        def get(self, url, params=None, timeout=None):
            raise RuntimeError(f"HTTPSConnectionPool: {url}?function=X&apikey={params['apikey']}&date=1")
    with pytest.raises(av.AlphaVantageOptionsError) as ei:
        av.fetch_chain_payload("NVDA", DAY, api_key="SECRETKEY123", session=Boom())
    assert "SECRETKEY123" not in str(ei.value) and "apikey=***" in str(ei.value)
    assert "SECRETKEY123" not in av.redact("see SECRETKEY123 here", "SECRETKEY123")


# ------------------------------------------------------------------ store
def _day_df(rows, day):
    df, st = av.map_chain("NVDA", day, [dict(r, date=day.isoformat()) for r in rows])
    return df, st


def test_ledger_resume_skips_recorded_days_including_empty(tmp_path, rows):
    store = av.AvOptionStore(str(tmp_path))
    d1, d2, d3 = date(2024, 5, 30), date(2024, 5, 31), date(2024, 6, 3)
    df, st = _day_df(rows, d1)
    store.record_day("NVDA", d1, df, st)
    empty_df, _ = av.map_chain("NVDA", d2, [])
    entry = store.record_day("NVDA", d2, empty_df)
    assert entry["s"] == "empty" and store.staged_days("NVDA") == [d1]
    assert store.pending_days("NVDA", [d1, d2, d3]) == [d3]
    # a torn final ledger line (killed mid-append) is ignored: that day is simply fetched again
    with open(store._ledger_path("NVDA"), "a") as f:
        f.write('{"d": "2024-06-03", "s": "o')
    assert store.pending_days("NVDA", [d1, d2, d3]) == [d3]


def test_compaction_makes_thetadata_shaped_partitions_a_reader_can_read(tmp_path, rows):
    store = av.AvOptionStore(str(tmp_path))
    days = [date(2024, 5, 29), date(2024, 5, 30), date(2024, 5, 31)]
    for d in days:
        df, st = _day_df(rows, d)
        store.record_day("NVDA", d, df, st)
    n = store.compact("NVDA")
    assert n == len({r["expiration"] for r in rows}) and store.staged_days("NVDA") == []
    reader = OptionHistoryParquetStore(str(tmp_path))
    full = reader.read_underlying("NVDA")
    assert set(full["bar_date"]) == {d.isoformat() for d in days}
    part = reader.read_partition("NVDA", date(2024, 6, 7))
    assert part.groupby("occ_symbol").size().eq(3).all()
    m = reader.read_manifest("NVDA", date(2024, 6, 7))
    assert m["status"] == "complete" and m["source"] == "alphavantage" and m["ohlc"] == "close-only"
    assert reader.partition_state("NVDA", date(2024, 6, 7), days[0], days[-1]).value == "complete"


def test_compaction_is_idempotent_and_merges_later_days(tmp_path, rows):
    store = av.AvOptionStore(str(tmp_path))
    d1, d2 = date(2024, 5, 30), date(2024, 5, 31)
    df1, st = _day_df(rows, d1)
    store.record_day("NVDA", d1, df1, st)
    store.compact("NVDA")
    # a kill after the merge but before the staged file was removed: the same day is merged again
    store.record_day("NVDA", d1, df1, st)
    df2, st = _day_df(rows, d2)
    store.record_day("NVDA", d2, df2, st)
    store.compact("NVDA")
    full = OptionHistoryParquetStore(str(tmp_path)).read_underlying("NVDA")
    assert len(full) == 2 * len(df1) and not full.duplicated(["occ_symbol", "bar_date"]).any()


def test_coverage_counts_recorded_days(tmp_path, rows):
    store = av.AvOptionStore(str(tmp_path))
    df, st = _day_df(rows, date(2024, 5, 30))
    store.record_day("NVDA", date(2024, 5, 30), df, st)
    assert store.coverage("NVDA", [date(2024, 5, 30), date(2024, 5, 31)]) == (1, 2)


def test_default_root_follows_cache_folder_at_call_time(monkeypatch, tmp_path):
    import ba2_common.config as cfg
    monkeypatch.setattr(cfg, "CACHE_FOLDER", str(tmp_path))
    assert av.AvOptionStore().root == os.path.join(str(tmp_path), "AlphaVantageOptionsProvider")
