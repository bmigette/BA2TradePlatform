import pytest
from ba2_common.core.ohlcv_history import (
    ACCOUNT_HISTORY_SOURCE_KEY, ACCOUNT_DEFAULT_HISTORY_SOURCE, UnknownHistorySource,
    account_history_source)


def test_key_and_declared_default():
    assert ACCOUNT_HISTORY_SOURCE_KEY == "ohlcv_history_source"
    assert ACCOUNT_DEFAULT_HISTORY_SOURCE == "fmp"


def test_unset_means_declared_default():
    assert account_history_source({}) == "fmp"
    assert account_history_source({"ohlcv_history_source": None}) == "fmp"


def test_alpaca_round_trip_and_case():
    assert account_history_source({"ohlcv_history_source": "alpaca"}) == "alpaca"
    assert account_history_source({"ohlcv_history_source": " Alpaca "}) == "alpaca"


@pytest.mark.parametrize("bad", ["", "yfinance", "FMP2", 3])
def test_unknown_refuses(bad):
    with pytest.raises(UnknownHistorySource):
        account_history_source({"ohlcv_history_source": bad})
