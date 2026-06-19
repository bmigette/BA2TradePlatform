"""Deterministic, network-free construction checks for ba2_providers.

FMPOHLCVProvider reads FMP_API_KEY from the app-settings DB at construction (no
network I/O). The session-scoped conftest fixture isolates the DB to a throwaway
sqlite, so we seed the key here first; construction then succeeds without ever
hitting the network.
"""
import pytest


@pytest.fixture
def _seed_fmp_key():
    from ba2_common.core import db
    from ba2_common.core.models import AppSetting
    db.add_instance(AppSetting(key="FMP_API_KEY", value_str="test-key"))
    yield


def test_construct_fmp_ohlcv_provider(_seed_fmp_key):
    from ba2_providers.ohlcv.FMPOHLCVProvider import FMPOHLCVProvider
    prov = FMPOHLCVProvider()  # construction must not hit the network
    assert prov is not None
    assert prov.api_key == "test-key"


def test_construct_screener_engine():
    from ba2_providers.StockScreener import StockScreener
    assert StockScreener is not None
    # StockScreener takes a settings dict; construct with an empty config (no I/O).
    screener = StockScreener({})
    assert screener is not None


def test_get_provider_constructs_fmp_ohlcv(_seed_fmp_key):
    from ba2_providers import get_provider
    prov = get_provider("ohlcv", "fmp")
    assert prov is not None
