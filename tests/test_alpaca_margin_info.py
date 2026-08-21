"""AlpacaAccount.get_symbol_margin_info against mocked Asset / TradeAccount objects.

bp_factor = initial_margin_rate * account_multiplier, and Alpaca's Asset has NO
initial-margin field, so the rate is DERIVED from three facts, in this order:

1. ``Asset.marginable`` describes the SECURITY. Non-marginable -> 1.0.
2. The ACCOUNT's ``multiplier`` says whether borrowing is possible at all. Alpaca
   reports "1" for cash and limited-margin accounts (and drops a margin account to
   1 below $2,000 equity), where ``buying_power == cash``; Reg-T's 50% only exists
   where the account can actually borrow, so at 1x the rate is 1.0 for EVERY symbol.
3. ``Asset.maintenance_margin_requirement`` (30/50/75/100) FLOORS the result -- an
   initial requirement below the maintenance requirement is not a thing.

In a 2:1 account an ordinary marginable name is 0.5 * 2 = 1.0 and a non-marginable
one is 1.0 * 2 = 2.0. In a 1x account everything is 1.0, i.e. exactly cash.

No live API call: client is a MagicMock returning real alpaca-py model objects.
"""
import time
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
    acct._asset_cache = {}
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


def test_marginable_symbol_in_a_cash_account_consumes_the_full_notional():
    """CONTRACT: at multiplier=1 a marginable symbol still costs 1.0, not 0.5.

    ``Asset.marginable`` is a fact about the SECURITY; the ACCOUNT decides whether
    any borrowing is possible. Alpaca sets multiplier="1" on cash and
    limited-margin accounts (and on a margin account that falls below $2,000
    equity), and there ``buying_power == cash`` -- nothing is lent, so the
    effective initial requirement is 100% for every symbol.

    This test previously asserted 0.5 and was asserting the bug: the engine's
    feasibility test is ``sum(notional * bp_factor) <= available_buying_power``,
    so a 0.5 here let a $1,000 cash account plan $2,000 of buys. The scale-down
    never fires, the sells execute first and the buy tail rejects with
    INSUFFICIENT_FUNDS, leaving a half-executed rebalance.
    """
    acct = _bare_account(multiplier="1")
    acct.client.get_asset.return_value = _asset("AAPL", marginable=True)

    info = acct.get_symbol_margin_info(["AAPL"])["AAPL"]

    assert info.bp_factor == 1.0
    assert info.initial_margin_rate == 1.0
    assert info.marginable is True     # the asset fact is still reported honestly


def test_a_cash_account_is_never_more_optimistic_than_the_conservative_fallback():
    """The asset lookup must never BUY MORE than the no-information fallback.

    When get_asset() fails the caller falls back to
    ``default_bp_factor = account multiplier`` (1.0 at 1x). Having MORE
    information may only make the plan smaller or equal, never bigger.
    """
    acct = _bare_account(multiplier="1")
    acct.client.get_asset.side_effect = lambda s: _asset(s, marginable=True)

    infos = acct.get_symbol_margin_info(["AAPL", "MSFT"])

    assert all(i.bp_factor >= 1.0 for i in infos.values())


def test_the_maintenance_requirement_floors_the_derived_initial_rate():
    """A 100%-maintenance name cannot be bought on 50% initial margin.

    Alpaca publishes maintenance_margin_requirement per name (30/50/75/100) and
    marks plenty of hard-to-margin names 100 while still flagging them
    marginable. The derived Reg-T 0.5 has to be floored by it, or the engine
    sizes a 100%-requirement name as if it were half price.
    """
    acct = _bare_account(multiplier="2")
    acct.client.get_asset.return_value = _asset(
        "HTB", marginable=True, maintenance_margin_requirement=100.0)

    info = acct.get_symbol_margin_info(["HTB"])["HTB"]

    assert info.initial_margin_rate == 1.0
    assert info.bp_factor == 2.0
    assert info.maintenance_margin_rate == 1.0


def test_a_maintenance_requirement_above_reg_t_but_below_100_also_floors():
    """75% maintenance -> 0.75 initial, i.e. 1.5 bp_factor in a 2:1 account."""
    acct = _bare_account(multiplier="2")
    acct.client.get_asset.return_value = _asset(
        "SPCY", marginable=True, maintenance_margin_requirement=75.0)

    assert acct.get_symbol_margin_info(["SPCY"])["SPCY"].bp_factor == 1.5


def test_a_maintenance_requirement_below_reg_t_does_not_lower_the_initial_rate():
    """max(), not assignment: 30% maintenance keeps the 0.5 Reg-T initial rate."""
    acct = _bare_account(multiplier="2")
    acct.client.get_asset.return_value = _asset(
        "AAPL", marginable=True, maintenance_margin_requirement=30.0)

    info = acct.get_symbol_margin_info(["AAPL"])["AAPL"]

    assert info.initial_margin_rate == 0.5
    assert info.bp_factor == 1.0


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


def test_alpaca_publishes_no_fractional_notional_floor():
    """The $5 fractional minimum is TASTYTRADE's rule, not a platform rule.

    Alpaca's `Asset` carries no equivalent, so `min_fractional_notional` stays None
    ("the broker published no floor"). The engine suppresses any fractional order
    below this number, so hardcoding TastyTrade's $5 here -- or defaulting the
    field to 5.0 in `MarginInfo` -- would silently refuse legal Alpaca orders that
    this account places routinely: Alpaca's own minimum is 0.001 SHARES.
    """
    acct = _bare_account()
    acct.client.get_asset.return_value = _asset(
        "AAPL", fractionable=True, min_order_size=0.001, min_trade_increment=0.001)

    info = acct.get_symbol_margin_info(["AAPL"])["AAPL"]

    assert info.min_fractional_notional is None
    assert info.min_order_size == 0.001      # SHARES -- the only minimum Alpaca has


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


def test_a_precheck_sourced_cache_entry_is_not_repriced_by_the_multiplier():
    """The repricing arithmetic is skipped when initial_margin_rate is None.

    A future non-asset entry (MARGIN_SOURCE_PRECHECK) carries a broker-measured
    bp_factor and NO initial rate to multiply, so ``rate * multiplier`` would be
    a TypeError on None. It is still RETURNED -- the guard keeps it out of the
    repricing, not out of the result. Without this test the mutation
    ``if rate is not None`` -> ``if True`` passes the whole suite.
    """
    from ba2_trade_platform.core.account_types import MARGIN_SOURCE_PRECHECK
    from ba2_trade_platform.core.account_types import MarginInfo

    acct = _bare_account(multiplier="2")
    acct._margin_info_cache["AAPL"] = (
        time.time(),
        MarginInfo(symbol="AAPL", bp_factor=0.77, initial_margin_rate=None,
                   source=MARGIN_SOURCE_PRECHECK),
    )

    info = acct.get_symbol_margin_info(["AAPL"])["AAPL"]

    assert info.bp_factor == 0.77            # NOT 0.77 * 2, and not dropped
    assert info.source == MARGIN_SOURCE_PRECHECK
    acct.client.get_asset.assert_not_called()


def test_a_blank_symbol_is_skipped_without_a_broker_round_trip():
    """`` `` / None in the basket must not become a get_asset("") call."""
    acct = _bare_account()
    acct.client.get_asset.side_effect = lambda s: _asset(s)

    infos = acct.get_symbol_margin_info(["", "   ", None, "AAPL"])

    assert set(infos) == {"AAPL"}
    assert acct.client.get_asset.call_count == 1


def test_a_cache_entry_older_than_the_ttl_is_refetched():
    """Alpaca REVOKES marginability and fractionability on individual names, and
    get_account_instance_from_id() hands out the same account object for the whole
    process lifetime -- a server up for weeks would otherwise freeze a symbol's
    facts at first sight. A stale marginable=True UNDERSTATES bp_cost, the same
    direction as the 1x bug."""
    acct = _bare_account()
    acct.client.get_asset.side_effect = lambda s: _asset(s, marginable=True)

    acct.get_symbol_margin_info(["AAPL"])
    # Age the entry past the TTL rather than sleeping.
    stamp, cached = acct._margin_info_cache["AAPL"]
    acct._margin_info_cache["AAPL"] = (stamp - AlpacaAccount._MARGIN_INFO_CACHE_TTL - 1, cached)

    acct.client.get_asset.side_effect = lambda s: _asset(s, marginable=False)
    info = acct.get_symbol_margin_info(["AAPL"])["AAPL"]

    assert acct.client.get_asset.call_count == 2
    assert info.marginable is False
    assert info.bp_factor == 2.0


def test_a_cache_entry_within_the_ttl_is_not_refetched():
    acct = _bare_account()
    acct.client.get_asset.side_effect = lambda s: _asset(s)

    acct.get_symbol_margin_info(["AAPL"])
    stamp, cached = acct._margin_info_cache["AAPL"]
    acct._margin_info_cache["AAPL"] = (stamp - AlpacaAccount._MARGIN_INFO_CACHE_TTL + 60, cached)
    acct.get_symbol_margin_info(["AAPL"])

    assert acct.client.get_asset.call_count == 1


def test_clear_margin_info_cache_forces_the_next_call_to_refetch():
    """The explicit Refresh path: a user who knows the broker changed a name
    must not have to restart the process to see it."""
    acct = _bare_account()
    acct.client.get_asset.side_effect = lambda s: _asset(s)

    acct.get_symbol_margin_info(["AAPL", "MSFT"])
    acct.clear_margin_info_cache()
    acct.get_symbol_margin_info(["AAPL", "MSFT"])

    assert acct.client.get_asset.call_count == 4
    assert acct._margin_info_cache != {}     # repopulated, not just emptied


def test_margin_info_is_frozen_so_a_cached_object_cannot_be_mutated_in_place():
    """get_symbol_margin_info hands out the CACHED object by reference; a caller
    that adjusted bp_factor on it would silently poison every later reader."""
    import dataclasses
    import pytest

    from ba2_trade_platform.core.account_types import MarginInfo

    acct = _bare_account()
    acct.client.get_asset.return_value = _asset("AAPL")
    info = acct.get_symbol_margin_info(["AAPL"])["AAPL"]

    with pytest.raises(dataclasses.FrozenInstanceError):
        info.bp_factor = 99.0


def test_no_margin_info_at_all_when_the_account_multiplier_is_unknown():
    """Without a multiplier there is no honest bp_factor to compute."""
    acct = _bare_account()
    acct.client.get_account.return_value = None
    acct.client.get_asset.side_effect = lambda s: _asset(s)

    assert acct.get_symbol_margin_info(["AAPL"]) == {}
