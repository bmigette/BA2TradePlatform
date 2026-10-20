"""The OHLCV history-source resolver (plan 2026-10-20-alpaca-history-source, Task 6)."""
import pytest

from ba2_common.core import ohlcv_history as oh


def test_both_sources_round_trip_to_registry_key_and_folder():
    assert oh.history_registry_key("fmp") == "fmp"
    assert oh.history_cache_dirname("fmp") == "FMPOHLCVProvider"
    assert oh.history_registry_key("alpaca") == "alpaca_hist"
    assert oh.history_cache_dirname("alpaca") == "AlpacaHistoryOHLCVProvider"


def test_a_job_without_a_stamp_ran_on_fmp():
    assert oh.resolve_job_history_source({}) == "fmp" == oh.LEGACY_HISTORY_SOURCE


def test_a_stamped_job_runs_on_its_stamp():
    assert oh.resolve_job_history_source({oh.OHLCV_HISTORY_SOURCE_KEY: "alpaca"}) == "alpaca"
    assert oh.resolve_job_history_source({oh.OHLCV_HISTORY_SOURCE_KEY: "fmp"}) == "fmp"


@pytest.mark.parametrize("bad", ["", "Alpaca", "polygon", None])
def test_an_unknown_stamp_refuses(bad):
    with pytest.raises(oh.UnknownHistorySource):
        oh.resolve_job_history_source({oh.OHLCV_HISTORY_SOURCE_KEY: bad})


@pytest.mark.parametrize("fn", [oh.history_registry_key, oh.history_cache_dirname, oh.other_history_cache_dirnames])
def test_every_lookup_refuses_an_unknown_source(fn):
    with pytest.raises(oh.UnknownHistorySource):
        fn("yfinance")


def test_new_jobs_default_to_alpaca():
    assert oh.DEFAULT_BT_HISTORY_SOURCE == "alpaca"


def test_other_folders_exclude_the_selected_source():
    assert oh.other_history_cache_dirnames("alpaca") == ("FMPOHLCVProvider",)
    assert oh.other_history_cache_dirnames("fmp") == ("AlpacaHistoryOHLCVProvider",)
