"""
Tests for ScreenerProviderInterface and FMPScreenerProvider.

All FMP API calls are mocked to avoid real network requests.
"""

import pytest
from unittest.mock import patch, MagicMock
import requests

from ba2_trade_platform.core.interfaces.ScreenerProviderInterface import ScreenerProviderInterface
from ba2_trade_platform.modules.dataproviders.screener.FMPScreenerProvider import FMPScreenerProvider


# ---------------------------------------------------------------------------
# Sample FMP API response data
# ---------------------------------------------------------------------------

SAMPLE_FMP_RESPONSE = [
    {
        "symbol": "ABCD",
        "companyName": "ABCD Corp",
        "price": 2.50,
        "volume": 5000000,
        "marketCap": 100000000,
        "sector": "Technology",
        "industry": "Software",
        "exchangeShortName": "NASDAQ",
        "exchange": "NASDAQ Global Market",
        "beta": 1.2,
        "isActivelyTrading": True,
        "country": "US",
    },
    {
        "symbol": "EFGH",
        "companyName": "EFGH Inc",
        "price": 1.80,
        "volume": 3000000,
        "marketCap": 50000000,
        "sector": "Energy",
        "industry": "Oil & Gas",
        "exchangeShortName": "NYSE",
        "exchange": "New York Stock Exchange",
        "beta": 0.9,
        "isActivelyTrading": True,
        "country": "US",
    },
    {
        "symbol": "IJKL",
        "companyName": "IJKL Ltd",
        "price": 3.10,
        "volume": 2000000,
        "marketCap": 75000000,
        "sector": "Financial Services",
        "industry": "Banking",
        "exchangeShortName": "NASDAQ",
        "exchange": "NASDAQ Capital Market",
        "beta": 1.5,
        "isActivelyTrading": True,
        "country": "US",
    },
]


@pytest.fixture
def provider():
    """Create an FMPScreenerProvider with a mocked API key."""
    with patch(
        "ba2_trade_platform.modules.dataproviders.screener.FMPScreenerProvider.get_app_setting",
        return_value="test_api_key",
    ):
        return FMPScreenerProvider()


# ---------------------------------------------------------------------------
# Interface contract tests
# ---------------------------------------------------------------------------


def test_is_screener_provider_interface(provider):
    """FMPScreenerProvider should implement ScreenerProviderInterface."""
    assert isinstance(provider, ScreenerProviderInterface)


def test_provider_name(provider):
    """Provider name should be 'fmp'."""
    assert provider.get_provider_name() == "fmp"


# ---------------------------------------------------------------------------
# validate_config tests
# ---------------------------------------------------------------------------


def test_validate_config_with_key(provider):
    """validate_config returns True when API key is set."""
    assert provider.validate_config() is True


def test_validate_config_without_key():
    """validate_config returns False when API key is missing."""
    with patch(
        "ba2_trade_platform.modules.dataproviders.screener.FMPScreenerProvider.get_app_setting",
        return_value=None,
    ):
        p = FMPScreenerProvider()
        assert p.validate_config() is False


# ---------------------------------------------------------------------------
# screen_stocks tests
# ---------------------------------------------------------------------------


@patch("ba2_trade_platform.modules.dataproviders.screener.FMPScreenerProvider.requests.get")
def test_screen_stocks_basic(mock_get, provider):
    """Basic screening should return normalised results."""
    mock_response = MagicMock()
    mock_response.json.return_value = SAMPLE_FMP_RESPONSE
    mock_response.raise_for_status.return_value = None
    mock_get.return_value = mock_response

    results = provider.screen_stocks({"price_min": 1.0, "price_max": 5.0, "limit": 50})

    assert len(results) == 3
    assert results[0]["symbol"] == "ABCD"
    assert results[0]["company_name"] == "ABCD Corp"
    assert results[0]["price"] == 2.50
    assert results[0]["volume"] == 5000000
    assert results[0]["market_cap"] == 100000000
    assert results[0]["sector"] == "Technology"
    assert results[0]["industry"] == "Software"
    assert results[0]["exchange"] == "NASDAQ"
    assert results[0]["beta"] == 1.2
    assert results[0]["is_actively_trading"] is True
    assert results[0]["country"] == "US"

    # Verify correct API params were sent
    call_kwargs = mock_get.call_args
    params = call_kwargs.kwargs.get("params") or call_kwargs[1].get("params")
    assert params["priceMoreThan"] == 1.0
    assert params["priceLowerThan"] == 5.0
    assert params["limit"] == 50
    assert params["isEtf"] is False
    assert params["isFund"] is False
    assert params["isActivelyTrading"] is True


@patch("ba2_trade_platform.modules.dataproviders.screener.FMPScreenerProvider.requests.get")
def test_screen_stocks_sector_exclusion(mock_get, provider):
    """Sector exclusion should filter results client-side."""
    mock_response = MagicMock()
    mock_response.json.return_value = SAMPLE_FMP_RESPONSE
    mock_response.raise_for_status.return_value = None
    mock_get.return_value = mock_response

    results = provider.screen_stocks({
        "price_min": 1.0,
        "sector_exclude": ["Energy", "Financial Services"],
    })

    assert len(results) == 1
    assert results[0]["symbol"] == "ABCD"
    assert results[0]["sector"] == "Technology"


@patch("ba2_trade_platform.modules.dataproviders.screener.FMPScreenerProvider.requests.get")
def test_screen_stocks_sector_exclusion_case_insensitive(mock_get, provider):
    """Sector exclusion should be case-insensitive."""
    mock_response = MagicMock()
    mock_response.json.return_value = SAMPLE_FMP_RESPONSE
    mock_response.raise_for_status.return_value = None
    mock_get.return_value = mock_response

    results = provider.screen_stocks({
        "sector_exclude": ["energy"],
    })

    symbols = [r["symbol"] for r in results]
    assert "EFGH" not in symbols
    assert "ABCD" in symbols


@patch("ba2_trade_platform.modules.dataproviders.screener.FMPScreenerProvider.requests.get")
def test_screen_stocks_empty_response(mock_get, provider):
    """Empty API response should return empty list."""
    mock_response = MagicMock()
    mock_response.json.return_value = []
    mock_response.raise_for_status.return_value = None
    mock_get.return_value = mock_response

    results = provider.screen_stocks({"price_min": 1.0})

    assert results == []


@patch("ba2_trade_platform.modules.dataproviders.screener.FMPScreenerProvider.requests.get")
def test_screen_stocks_api_error(mock_get, provider):
    """API errors should return empty list, not raise."""
    mock_get.side_effect = requests.RequestException("Connection error")

    results = provider.screen_stocks({"price_min": 1.0})

    assert results == []


@patch("ba2_trade_platform.modules.dataproviders.screener.FMPScreenerProvider.requests.get")
def test_screen_stocks_exchanges_filter(mock_get, provider):
    """Exchanges filter should be comma-joined in the API request."""
    mock_response = MagicMock()
    mock_response.json.return_value = []
    mock_response.raise_for_status.return_value = None
    mock_get.return_value = mock_response

    provider.screen_stocks({"exchanges": ["NASDAQ", "NYSE"]})

    call_kwargs = mock_get.call_args
    params = call_kwargs.kwargs.get("params") or call_kwargs[1].get("params")
    assert params["exchange"] == "NASDAQ,NYSE"


@patch("ba2_trade_platform.modules.dataproviders.screener.FMPScreenerProvider.requests.get")
def test_screen_stocks_market_cap_filters(mock_get, provider):
    """Market cap filters should map to correct FMP params."""
    mock_response = MagicMock()
    mock_response.json.return_value = []
    mock_response.raise_for_status.return_value = None
    mock_get.return_value = mock_response

    provider.screen_stocks({
        "market_cap_min": 10000000,
        "market_cap_max": 500000000,
        "volume_min": 1000000,
    })

    call_kwargs = mock_get.call_args
    params = call_kwargs.kwargs.get("params") or call_kwargs[1].get("params")
    assert params["marketCapMoreThan"] == 10000000
    assert params["marketCapLowerThan"] == 500000000
    assert params["volumeMoreThan"] == 1000000


def test_screen_stocks_no_api_key():
    """Screening without API key should return empty list."""
    with patch(
        "ba2_trade_platform.modules.dataproviders.screener.FMPScreenerProvider.get_app_setting",
        return_value=None,
    ):
        p = FMPScreenerProvider()
        results = p.screen_stocks({"price_min": 1.0})
        assert results == []
