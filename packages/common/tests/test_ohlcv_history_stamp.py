"""Derived stores name the OHLCV history they were built from (plan alpaca-history-source, Task 8)."""
import os

import pytest

from ba2_common.core.market_condition_source import (
    SOURCE_PROFILE_ALPACA_DAILY, SOURCE_PROFILE_FMP_DAILY, certify_source_columns, fmp_daily_cache_path,
    provider_dir_for_source_profile, source_profile_for_history)
from ba2_common.core.ohlcv_history import OHLCV_HISTORY_SOURCE_KEY, UnknownHistorySource
from ba2_common.core.ohlcv_history_stamp import (
    HistorySourceMismatch, require_matching_history_source, stamp_history_source, stored_history_source)


def test_stamp_uses_the_job_config_key_and_validates():
    assert stamp_history_source("alpaca") == {OHLCV_HISTORY_SOURCE_KEY: "alpaca"}
    with pytest.raises(UnknownHistorySource):
        stamp_history_source("yahoo")


def test_unstamped_manifest_reads_as_fmp():
    assert stored_history_source({"n_symbols": 3}) == "fmp"
    assert stored_history_source({OHLCV_HISTORY_SOURCE_KEY: "alpaca"}) == "alpaca"


@pytest.mark.parametrize("stored,job,ok", [
    ({}, "fmp", True), ({}, "alpaca", False),
    ({OHLCV_HISTORY_SOURCE_KEY: "fmp"}, "fmp", True), ({OHLCV_HISTORY_SOURCE_KEY: "fmp"}, "alpaca", False),
    ({OHLCV_HISTORY_SOURCE_KEY: "alpaca"}, "alpaca", True), ({OHLCV_HISTORY_SOURCE_KEY: "alpaca"}, "fmp", False)])
def test_mismatch_refuses(stored, job, ok):
    if ok:
        assert require_matching_history_source(stored, job, what="x") == job
    else:
        with pytest.raises(HistorySourceMismatch, match="built from the"):
            require_matching_history_source(stored, job, what="the panel")


def test_a_garbage_stamp_refuses_rather_than_reading_as_fmp():
    with pytest.raises(UnknownHistorySource):
        stored_history_source({OHLCV_HISTORY_SOURCE_KEY: ""})


def test_market_condition_source_profile_differs_per_source():
    assert source_profile_for_history("fmp") == SOURCE_PROFILE_FMP_DAILY
    assert source_profile_for_history("alpaca") == SOURCE_PROFILE_ALPACA_DAILY == "alpaca-daily-split-verified-v1"
    assert SOURCE_PROFILE_FMP_DAILY != SOURCE_PROFILE_ALPACA_DAILY
    with pytest.raises(UnknownHistorySource):
        source_profile_for_history("nope")


def test_each_source_profile_reads_its_own_cache_folder(tmp_path):
    assert provider_dir_for_source_profile(SOURCE_PROFILE_FMP_DAILY) == "FMPOHLCVProvider"
    assert provider_dir_for_source_profile(SOURCE_PROFILE_ALPACA_DAILY) == "AlpacaHistoryOHLCVProvider"
    with pytest.raises(ValueError, match="unknown market-condition source profile"):
        provider_dir_for_source_profile("fmp-daily-split-adjusted-v2")
    for folder in ("FMPOHLCVProvider", "AlpacaHistoryOHLCVProvider"):
        (tmp_path / folder).mkdir()
    (tmp_path / "AlpacaHistoryOHLCVProvider" / "AAPL_1d.parquet").write_bytes(b"")
    root = str(tmp_path)
    assert fmp_daily_cache_path("AAPL", root) is None            # default profile: FMP folder, empty
    assert fmp_daily_cache_path("AAPL", root, source_profile=SOURCE_PROFILE_ALPACA_DAILY).endswith(
        "AlpacaHistoryOHLCVProvider" + os.sep + "AAPL_1d.parquet")


def test_certification_report_carries_the_profile_it_certified(tmp_path):
    rep = certify_source_columns(str(tmp_path), source_profile=SOURCE_PROFILE_ALPACA_DAILY)
    assert rep.source_profile == SOURCE_PROFILE_ALPACA_DAILY and not rep.consistent
    assert all("AlpacaHistoryOHLCVProvider" in s.reason for s in rep.symbols)
