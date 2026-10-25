"""Owner amendments 2026-10-24 to the live backfill: only the requested interval and range, backward extension, the
backtest reader refuses a window before the live coverage; the warm-up is requested and a too-short history is insufficient."""
from datetime import date, datetime

import pandas as pd
import pytest

from ba2_providers.ohlcv.alpaca_history import build
from ba2_providers.ohlcv.alpaca_history.live_topup import TopUpGap
from tests.test_alpaca_history_backfill import (  # noqa: F401  (fixture `cache` is used by name)
    FakeBars, FakeCA, cache, meta_consistent, provider, raw_5min, raw_daily, read, sessions)


def test_only_the_requested_range_and_interval_is_downloaded(cache):
    bars = FakeBars(daily={"OLD": raw_daily(sessions("2026-06-01"))}, intraday={"OLD": raw_5min(sessions("2026-06-01"))})
    provider(bars).get_ohlcv_data("OLD", lookback_days=35, interval="1d")           # asked from 2026-09-19
    assert [(c[0], c[2], c[3]) for c in bars.calls] == [("1d", date(2026, 9, 19), date(2026, 10, 24))]
    got = read(cache, "OLD")
    assert pd.to_datetime(got["Date"]).min() == pd.Timestamp("2026-09-21") and not (cache / "OLD_5min.parquet").exists()
    m = meta_consistent(cache)["symbols"]["OLD"]
    assert m["origin"] == "live_backfill" and m["1d"]["covered_from"] == "2026-09-19" and m["1d"]["partial"] is True
    assert "5min" not in m


def test_a_later_read_needing_an_earlier_start_extends_backwards_by_just_the_missing_range(cache):
    bars = FakeBars(daily={"OLD": raw_daily(sessions("2026-06-01"))})
    p = provider(bars)
    p.get_ohlcv_data("OLD", lookback_days=35, interval="1d")
    first_file = read(cache, "OLD")
    p.get_ohlcv_data("OLD", lookback_days=60, interval="1d")                          # asked from 2026-08-25
    assert [(c[2], c[3]) for c in bars.calls] == [(date(2026, 9, 19), date(2026, 10, 24)), (date(2026, 8, 25), date(2026, 9, 21))]
    got = read(cache, "OLD")
    assert list(pd.to_datetime(got["Date"])) == sessions("2026-08-25")                # contiguous: every session, once
    pd.testing.assert_frame_equal(got.tail(len(first_file)).reset_index(drop=True), first_file)   # old rows untouched
    m = meta_consistent(cache)["symbols"]["OLD"]
    assert m["1d"]["covered_from"] == "2026-08-25" and m["1d"]["first"] == "2026-08-25"
    p.get_ohlcv_data("OLD", lookback_days=50, interval="1d")                          # already covered: no new call
    assert len(bars.calls) == 2


def test_backward_extension_applies_a_split_in_the_new_range_and_keeps_one_basis(cache):
    raw = raw_daily(sessions("2026-06-01"), split_on="2026-09-01")                     # 2:1 on 09-01
    ca = FakeCA({"forward_splits": [{"symbol": "SPL", "ex_date": "2026-09-01", "new_rate": 2, "old_rate": 1}]})
    p = provider(FakeBars(daily={"SPL": raw}), ca)
    p.get_ohlcv_data("SPL", lookback_days=35, interval="1d")                           # from 09-19: after the split
    assert meta_consistent(cache)["symbols"]["SPL"]["events"]["splits"] == []
    p.get_ohlcv_data("SPL", lookback_days=80, interval="1d")                           # from 08-05: the split is in the range
    expected = build.finish(build.adjust(raw[raw["Date"] >= "2026-08-05"], [("2026-09-01", 2.0)]), "1d")
    pd.testing.assert_frame_equal(read(cache, "SPL"), expected, check_dtype=False, rtol=1e-12)
    assert meta_consistent(cache)["symbols"]["SPL"]["events"]["splits"] == [["2026-09-01", 2.0]]


def test_extension_before_the_first_bar_of_an_ipo_only_moves_the_claim(cache):
    p = provider(FakeBars(daily={"NEW": raw_daily(sessions("2026-09-15"))}))
    p.get_ohlcv_data("NEW", lookback_days=35, interval="1d")                           # from 09-19, file starts 09-21
    p.get_ohlcv_data("NEW", lookback_days=50, interval="1d")                           # from 09-04: bars 09-15..09-18 exist
    assert pd.to_datetime(read(cache, "NEW")["Date"]).min() == pd.Timestamp("2026-09-15")
    p.get_ohlcv_data("NEW", lookback_days=100, interval="1d")                          # before the IPO: nothing earlier
    assert pd.to_datetime(read(cache, "NEW")["Date"]).min() == pd.Timestamp("2026-09-15")
    assert meta_consistent(cache)["symbols"]["NEW"]["1d"]["covered_from"] == "2026-08-03"


def test_backward_extension_gap_writes_nothing(cache):
    days = sessions("2026-06-01")
    provider(FakeBars(daily={"OLD": raw_daily(days)})).get_ohlcv_data("OLD", lookback_days=35, interval="1d")
    snap = (cache / "OLD_1d.parquet").read_bytes()
    gappy = [d for d in days if d != pd.Timestamp("2026-09-10")]
    with pytest.raises(TopUpGap):
        provider(FakeBars(daily={"OLD": raw_daily(gappy)})).get_ohlcv_data("OLD", lookback_days=60, interval="1d")
    assert (cache / "OLD_1d.parquet").read_bytes() == snap


def test_backtest_reader_refuses_a_window_before_the_live_coverage(cache):
    from ba2_providers.ohlcv.AlpacaHistoryOHLCVProvider import AlpacaHistoryOHLCVProvider
    from ba2_providers.ohlcv.alpaca_history.live_backfill import PartialLiveBackfill
    provider(FakeBars(daily={"OLD": raw_daily(sessions("2026-06-01"))})).get_ohlcv_data("OLD", lookback_days=35, interval="1d")
    bt = AlpacaHistoryOHLCVProvider()                                                  # keyless: what a backtest builds
    with pytest.raises(PartialLiveBackfill, match=r"OLD.*PARTIAL live backfill.*2026-09-19.*Rebuild"):
        bt.get_ohlcv_data("OLD", end_date=datetime(2026, 10, 10), lookback_days=60, interval="1d")
    assert len(bt.get_ohlcv_data("OLD", end_date=datetime(2026, 10, 10), lookback_days=15, interval="1d")) > 0   # from 09-25
    bt.get_ohlcv_data("AAA", end_date=datetime(2026, 10, 10), lookback_days=60, interval="1d")                   # built symbol


def test_5min_backfill_fetches_the_requested_range_only(cache):
    bars = FakeBars(intraday={"AAA": raw_5min(sessions("2026-09-01"))})
    provider(bars).get_ohlcv_data("AAA", lookback_days=14, interval="5min")
    assert [(c[0], c[2]) for c in bars.calls] == [("5min", date(2026, 10, 10))]
    got = read(cache, "AAA", "5min")
    assert pd.to_datetime(got["Date"]).dt.normalize().min() == pd.Timestamp("2026-10-12")
    assert meta_consistent(cache)["symbols"]["AAA"]["5min"]["covered_from"] == "2026-10-10"


# ---- warm-up and short history -------------------------------------------------------------------------------------------
def test_first_read_for_a_200_session_indicator_fetches_the_warmup(cache):
    import json
    from ba2_providers.indicators.PandasIndicatorCalc import PandasIndicatorCalc
    mpath = cache / "_meta" / "build_manifest.json"                                # a build that starts years back, like the real one
    m = json.loads(mpath.read_text())
    m["symbols"]["AAA"]["1d"]["first"] = "2020-01-02"
    mpath.write_text(json.dumps(m))
    bars = FakeBars(daily={"WRM": raw_daily(sessions("2025-01-02"))})
    out = PandasIndicatorCalc(provider(bars)).get_indicator(
        "WRM", "close_200_sma", end_date=datetime(2026, 10, 23), lookback_days=30, format_type="dict")
    assert out["values"]
    assert (date(2026, 10, 23) - bars.calls[0][2]).days >= 30 + 300              # warm-up requested by the indicator provider
    assert len(read(cache, "WRM")) >= 200


def test_a_symbol_too_new_for_the_window_is_insufficient_not_computed(cache):
    from ba2_common.core.ohlcv_history import InsufficientHistory
    from ba2_providers.indicators.PandasIndicatorCalc import PandasIndicatorCalc
    ipo = sessions("2026-05-01")                                                  # about 120 sessions of history
    assert 100 < len(ipo) < 200
    import json
    mpath = cache / "_meta" / "build_manifest.json"
    m = json.loads(mpath.read_text())
    m["symbols"]["AAA"]["1d"]["first"] = "2020-01-02"
    mpath.write_text(json.dumps(m))
    calc = PandasIndicatorCalc(provider(FakeBars(daily={"IPO": raw_daily(ipo)})))
    with pytest.raises(InsufficientHistory, match=r"IPO.*close_200_sma.*200.*only"):
        calc.get_indicator("IPO", "close_200_sma", end_date=datetime(2026, 10, 23), lookback_days=30, format_type="dict")
    calc.get_indicator("IPO", "close_50_sma", end_date=datetime(2026, 10, 23), lookback_days=30, format_type="dict")
