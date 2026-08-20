"""AlpacaAccount.get_symbol_margin_info against mocked Asset / TradeAccount objects.

bp_factor = initial_margin_rate * account_multiplier, and Alpaca's Asset has NO
initial-margin field, so the rate is derived: marginable -> 0.5 (Reg-T),
non-marginable -> 1.0. In a 2:1 account that is 1.0 vs 2.0.

No live API call: client is a MagicMock returning real alpaca-py model objects.
"""
from unittest.mock import MagicMock
from uuid import uuid4

from alpaca.trading.enums import (
    AccountStatus, AssetClass, AssetExchange, AssetStatus,
)
from alpaca.trading.models import Asset, TradeAccount

from ba2_trade_platform.core.account_types import MARGIN_SOURCE_ASSET
from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount


def _bare_account(multiplier="2"):
    acct = object.__new__(AlpacaAccount)
    acct.id = 1
    acct.client = MagicMock()
    acct._margin_info_cache = {}
    acct.client.get_account.return_value = TradeAccount(
        id=uuid4(), account_number="PA1", status=AccountStatus.ACTIVE,
        cash="1000", equity="25000", buying_power="50000", multiplier=multiplier)
    acct.client.get_account_configurations.return_value = MagicMock(fractional_trading=True)
    return acct


def _asset(symbol="AAPL", marginable=True, fractionable=True,
           min_order_size=0.001, min_trade_increment=0.001,
           maintenance_margin_requirement=30.0):
    # `asset_class` is exposed under the pydantic alias "class", so it must be
    # passed via a dict splat -- Asset(asset_class=...) raises "Field required".
    return Asset(
        id=uuid4(), **{"class": AssetClass.US_EQUITY}, exchange=AssetExchange.NASDAQ,
        symbol=symbol, status=AssetStatus.ACTIVE, tradable=True, marginable=marginable,
        shortable=True, easy_to_borrow=True, fractionable=fractionable,
        min_order_size=min_order_size, min_trade_increment=min_trade_increment,
        maintenance_margin_requirement=maintenance_margin_requirement)


def test_marginable_symbol_in_a_2x_account_consumes_buying_power_dollar_for_dollar():
    acct = _bare_account(multiplier="2")
    acct.client.get_asset.return_value = _asset("AAPL", marginable=True)

    info = acct.get_symbol_margin_info(["AAPL"])["AAPL"]

    assert info.bp_factor == 1.0          # 0.5 Reg-T * 2 multiplier
    assert info.initial_margin_rate == 0.5
    assert info.marginable is True
    assert info.source == MARGIN_SOURCE_ASSET


def test_non_marginable_symbol_in_a_2x_account_consumes_double():
    acct = _bare_account(multiplier="2")
    acct.client.get_asset.return_value = _asset("GME", marginable=False)

    info = acct.get_symbol_margin_info(["GME"])["GME"]

    assert info.bp_factor == 2.0          # 1.0 * 2 multiplier
    assert info.initial_margin_rate == 1.0
    assert info.marginable is False


def test_marginable_symbol_in_a_cash_account_consumes_half():
    acct = _bare_account(multiplier="1")
    acct.client.get_asset.return_value = _asset("AAPL", marginable=True)

    assert acct.get_symbol_margin_info(["AAPL"])["AAPL"].bp_factor == 0.5


def test_maintenance_margin_percentage_is_converted_to_a_rate():
    """Alpaca publishes 30.0 meaning 30%; MarginInfo carries the 0-1 rate."""
    acct = _bare_account()
    acct.client.get_asset.return_value = _asset("AAPL", maintenance_margin_requirement=30.0)

    assert acct.get_symbol_margin_info(["AAPL"])["AAPL"].maintenance_margin_rate == 0.3


def test_fractionability_and_trade_increments_are_carried_through():
    acct = _bare_account()
    acct.client.get_asset.return_value = _asset(
        "AAPL", fractionable=True, min_order_size=0.001, min_trade_increment=0.001)

    info = acct.get_symbol_margin_info(["AAPL"])["AAPL"]

    assert info.fractionable is True
    assert info.min_order_size == 0.001
    assert info.min_trade_increment == 0.001


def test_symbols_are_normalised_before_lookup():
    acct = _bare_account()
    acct.client.get_asset.side_effect = lambda s: _asset(s)

    infos = acct.get_symbol_margin_info([" aapl ", "msft"])

    assert set(infos) == {"AAPL", "MSFT"}
    assert acct.client.get_asset.call_args_list[0][0][0] == "AAPL"


def test_a_symbol_the_broker_cannot_describe_is_omitted_not_defaulted():
    """An omitted symbol makes the caller fall back to the conservative
    account-multiplier factor. A fabricated entry here would over-deploy."""
    acct = _bare_account()

    def _get_asset(symbol):
        if symbol == "BADSYM":
            raise Exception("404 asset not found")
        return _asset(symbol)

    acct.client.get_asset.side_effect = _get_asset

    infos = acct.get_symbol_margin_info(["AAPL", "BADSYM"])

    assert set(infos) == {"AAPL"}


def test_a_second_request_for_the_same_symbol_hits_the_cache_not_the_api():
    """Alpaca has no bulk asset endpoint, so this is one HTTP call per symbol --
    the page refreshes the same basket repeatedly and must not re-fetch."""
    acct = _bare_account()
    acct.client.get_asset.side_effect = lambda s: _asset(s)

    acct.get_symbol_margin_info(["AAPL", "MSFT"])
    calls_after_first = acct.client.get_asset.call_count
    acct.get_symbol_margin_info(["AAPL", "MSFT"])

    assert calls_after_first == 2
    assert acct.client.get_asset.call_count == 2


def test_a_cached_symbol_is_repriced_when_the_account_multiplier_changes():
    """The cache holds the ASSET facts (marginability, increments), which do not
    change intraday. The multiplier does -- Alpaca moves an account between 1/2/4
    as it crosses the PDT threshold -- and this process is long-lived, so a cache
    hit must re-derive bp_factor from the multiplier read on THIS call."""
    acct = _bare_account(multiplier="2")
    acct.client.get_asset.side_effect = lambda s: _asset(s, marginable=True)

    assert acct.get_symbol_margin_info(["AAPL"])["AAPL"].bp_factor == 1.0

    acct.client.get_account.return_value = TradeAccount(
        id=uuid4(), account_number="PA1", status=AccountStatus.ACTIVE,
        cash="1000", equity="25000", buying_power="100000", multiplier="4")

    assert acct.get_symbol_margin_info(["AAPL"])["AAPL"].bp_factor == 2.0
    assert acct.client.get_asset.call_count == 1     # still no second asset fetch


def test_no_margin_info_at_all_when_the_account_multiplier_is_unknown():
    """Without a multiplier there is no honest bp_factor to compute."""
    acct = _bare_account()
    acct.client.get_account.return_value = None
    acct.client.get_asset.side_effect = lambda s: _asset(s)

    assert acct.get_symbol_margin_info(["AAPL"]) == {}
