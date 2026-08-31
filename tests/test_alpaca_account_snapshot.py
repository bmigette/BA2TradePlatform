"""AlpacaAccount.get_account_snapshot against a REAL pydantic TradeAccount.

This is the whole point of the snapshot: Alpaca hands back a pydantic object with
no .get() and every money field typed Optional[str], while IBKR/TastyTrade hand
back a dict of floats. No live API call is made -- self.client is a MagicMock and
the TradeAccount / Asset objects are constructed from the installed alpaca-py SDK.
"""
import threading
from unittest.mock import MagicMock
from uuid import uuid4

from alpaca.trading.enums import AccountStatus
from alpaca.trading.models import TradeAccount

from ba2_trade_platform.core.account_types import AccountSnapshot
from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount


def _bare_account():
    """An AlpacaAccount without __init__ (no credentials, no broker connection).
    client is a MagicMock so _check_authentication() passes."""
    acct = object.__new__(AlpacaAccount)
    acct.id = 1
    acct.client = MagicMock()
    acct._margin_info_cache = {}
    return acct


def _trade_account(**overrides):
    """A real pydantic TradeAccount, money fields as STRINGS exactly like Alpaca."""
    kwargs = dict(
        id=uuid4(),
        account_number="PA1",
        status=AccountStatus.ACTIVE,
        cash="1000.50",
        equity="25000.00",
        buying_power="50000.00",
        non_marginable_buying_power="1000.50",
        multiplier="2",
        long_market_value="24000.00",
        short_market_value="0",
        pending_transfer_in="500.00",
    )
    kwargs.update(overrides)
    return TradeAccount(**kwargs)


def test_snapshot_coerces_alpacas_string_money_fields_to_floats():
    acct = _bare_account()
    acct.client.get_account.return_value = _trade_account()
    acct.client.get_account_configurations.return_value = MagicMock(fractional_trading=True)

    snap = acct.get_account_snapshot()

    assert snap.cash == 1000.50
    assert snap.equity == 25000.00
    assert snap.net_liquidation == 25000.00
    assert snap.buying_power == 50000.00
    assert snap.non_marginable_buying_power == 1000.50
    assert snap.long_market_value == 24000.00
    assert snap.short_market_value == 0.0
    assert snap.pending_transfer_in == 500.00


def test_snapshot_reads_the_string_multiplier_as_a_number_and_flags_margin():
    acct = _bare_account()
    acct.client.get_account.return_value = _trade_account(multiplier="4")
    acct.client.get_account_configurations.return_value = MagicMock(fractional_trading=True)

    snap = acct.get_account_snapshot()

    assert snap.margin_multiplier == 4.0
    assert snap.is_margin_account is True


def test_snapshot_of_a_cash_account_is_not_flagged_as_margin():
    acct = _bare_account()
    acct.client.get_account.return_value = _trade_account(multiplier="1")
    acct.client.get_account_configurations.return_value = MagicMock(fractional_trading=False)

    snap = acct.get_account_snapshot()

    assert snap.margin_multiplier == 1.0
    assert snap.is_margin_account is False


def test_snapshot_reports_fractional_capability_from_account_configurations():
    """TradeAccount has no fractional field -- it lives on AccountConfiguration."""
    acct = _bare_account()
    acct.client.get_account.return_value = _trade_account()
    acct.client.get_account_configurations.return_value = MagicMock(fractional_trading=True)

    assert acct.get_account_snapshot().supports_fractional is True


def test_snapshot_reports_no_fractional_when_the_account_has_it_disabled():
    acct = _bare_account()
    acct.client.get_account.return_value = _trade_account()
    acct.client.get_account_configurations.return_value = MagicMock(fractional_trading=False)

    assert acct.get_account_snapshot().supports_fractional is False


def test_snapshot_keeps_the_account_identity_in_raw():
    acct = _bare_account()
    acct.client.get_account.return_value = _trade_account()
    acct.client.get_account_configurations.return_value = MagicMock(fractional_trading=True)

    assert acct.get_account_snapshot().raw["account_number"] == "PA1"


def test_snapshot_on_auth_failure_is_all_unknown_not_all_zero():
    """get_account_info() returns None when Alpaca rejects the credentials. A 0.0
    buying power here would let the allocation page plan against a dead account."""
    acct = _bare_account()
    acct.client.get_account.side_effect = Exception("401 unauthorized")

    snap = acct.get_account_snapshot()

    assert snap == AccountSnapshot()
    assert snap.buying_power is None
    assert snap.margin_multiplier is None


def test_snapshot_still_returns_the_money_when_account_configurations_fails():
    """A failing capability probe must not lose the balances we did get. Both the typed
    SDK call AND the raw fallback fail here -- see the raw-fallback tests below for the
    case where the raw read recovers what the typed call couldn't."""
    acct = _bare_account()
    acct.client.get_account.return_value = _trade_account()
    acct.client.get_account_configurations.side_effect = Exception("500 server error")
    acct.client.get.side_effect = Exception("500 server error")

    snap = acct.get_account_snapshot()

    assert snap.buying_power == 50000.00
    assert snap.supports_fractional is False


# ---------------------------------------------------------------------------
# Raw fallback when the SDK's typed AccountConfiguration parse fails.
#
# alpaca.trading.models.AccountConfiguration requires dtbp_check/pdt_check with no
# default -- observed live (2026-08-31) failing across all 3 dev accounts simultaneously
# with "2 validation errors for AccountConfiguration: dtbp_check / pdt_check Field
# required", even though the raw JSON response DOES carry fractional_trading (the only
# field this snapshot actually needs). The typed call is tried first because it is the
# supported, documented path; the raw GET to the same endpoint is only a fallback for
# when Alpaca's response omits fields the SDK's model demands but this snapshot doesn't.
# ---------------------------------------------------------------------------
def test_snapshot_recovers_fractional_via_raw_fallback_when_the_typed_parse_fails():
    acct = _bare_account()
    acct.client.get_account.return_value = _trade_account()
    acct.client.get_account_configurations.side_effect = Exception(
        "2 validation errors for AccountConfiguration\ndtbp_check\n  Field required")
    acct.client.get.return_value = {
        "dtbp_check": None, "pdt_check": None,  # what's actually missing/malformed
        "fractional_trading": True, "trade_confirm_email": "all",
    }

    snap = acct.get_account_snapshot()

    assert snap.supports_fractional is True
    assert snap.buying_power == 50000.00
    acct.client.get.assert_called_once_with("/account/configurations")


def test_snapshot_raw_fallback_reports_false_for_a_genuinely_non_fractional_account():
    acct = _bare_account()
    acct.client.get_account.return_value = _trade_account()
    acct.client.get_account_configurations.side_effect = Exception("validation error")
    acct.client.get.return_value = {"fractional_trading": False}

    assert acct.get_account_snapshot().supports_fractional is False


def test_snapshot_raw_fallback_that_also_fails_still_reports_the_money():
    """Both reads failing (not just the typed one) must land exactly where the old
    single-path failure did: supports_fractional=False, balances intact."""
    acct = _bare_account()
    acct.client.get_account.return_value = _trade_account()
    acct.client.get_account_configurations.side_effect = Exception("500 server error")
    acct.client.get.side_effect = Exception("500 server error")

    snap = acct.get_account_snapshot()

    assert snap.supports_fractional is False
    assert snap.buying_power == 50000.00


def test_snapshot_raw_fallback_with_no_fractional_key_at_all_reports_false():
    """A raw response missing fractional_trading entirely (not just malformed) is the
    same as not having an answer -- False, not a KeyError."""
    acct = _bare_account()
    acct.client.get_account.return_value = _trade_account()
    acct.client.get_account_configurations.side_effect = Exception("validation error")
    acct.client.get.return_value = {"trade_confirm_email": "all"}

    assert acct.get_account_snapshot().supports_fractional is False


def test_snapshot_does_not_try_the_raw_fallback_when_the_typed_call_succeeds():
    """The raw GET is a fallback, not a second opinion -- it must not fire on the
    happy path."""
    acct = _bare_account()
    acct.client.get_account.return_value = _trade_account()
    acct.client.get_account_configurations.return_value = MagicMock(fractional_trading=True)

    acct.get_account_snapshot()

    acct.client.get.assert_not_called()


# ---------------------------------------------------------------------------
# Short-TTL snapshot cache.
#
# Reading equity through get_account_snapshot() (the C1 fix) is correct but
# COSTS MORE on Alpaca than the get_account_info() it replaced: the override also
# calls get_account_configurations() for the fractional flag, so every
# market-order validation became TWO REST round trips instead of one -- on the
# live path.
#
# The cache mirrors the _margin_info_cache pattern (class-attribute TTL + an
# explicit clear_*() companion) but with a 5 s TTL rather than 24 h: this is
# account-level MONEY, and a stale equity used for a risk check is its own bug.
# ---------------------------------------------------------------------------

def _cached_account():
    acct = _bare_account()
    acct.client.get_account.return_value = _trade_account()
    acct.client.get_account_configurations.return_value = MagicMock(fractional_trading=True)
    acct._account_snapshot_cache = None
    return acct


def test_the_ttl_is_short_because_this_is_money_not_static_asset_facts():
    """Pinned so nobody "optimises" it towards the 24 h margin-info TTL. It matches
    _BALANCE_CACHE_TTL: same endpoint, same money fields, one staleness window."""
    assert AlpacaAccount._ACCOUNT_SNAPSHOT_CACHE_TTL == 5.0


def test_n_snapshots_inside_the_ttl_make_exactly_one_round_trip():
    acct = _cached_account()

    for _ in range(10):
        assert acct.get_account_snapshot().equity == 25000.00

    assert acct.client.get_account.call_count == 1
    assert acct.client.get_account_configurations.call_count == 1


def test_a_call_just_inside_the_ttl_is_still_served_from_cache():
    acct = _cached_account()
    acct.get_account_snapshot()
    stamp, cached = acct._account_snapshot_cache
    acct._account_snapshot_cache = (
        stamp - AlpacaAccount._ACCOUNT_SNAPSHOT_CACHE_TTL + 1, cached)

    acct.get_account_snapshot()

    assert acct.client.get_account.call_count == 1


def test_the_cache_expires_after_the_ttl():
    acct = _cached_account()
    acct.get_account_snapshot()
    stamp, cached = acct._account_snapshot_cache
    acct._account_snapshot_cache = (
        stamp - AlpacaAccount._ACCOUNT_SNAPSHOT_CACHE_TTL - 1, cached)

    acct.client.get_account.return_value = _trade_account(equity="31000.00")
    snap = acct.get_account_snapshot()

    assert acct.client.get_account.call_count == 2
    assert snap.equity == 31000.00  # the FRESH number, not the stale one


def test_clear_account_snapshot_cache_forces_the_next_call_to_refetch():
    acct = _cached_account()
    acct.get_account_snapshot()

    acct.clear_account_snapshot_cache()
    acct.get_account_snapshot()

    assert acct.client.get_account.call_count == 2


def test_a_submitted_order_invalidates_the_snapshot_too():
    """invalidate_balance_cache() runs after every submit_order because "a submitted
    order immediately changes buying power". The snapshot carries the SAME
    buying_power, so it must not survive that moment either -- otherwise
    TradeActions.increase_instrument_share would size the next order against
    pre-trade buying power."""
    acct = _cached_account()
    acct._balance_cache = None
    acct._balance_cache_time = 0.0
    acct._balance_cache_lock = threading.Lock()
    acct.get_account_snapshot()

    acct.invalidate_balance_cache()
    acct.get_account_snapshot()

    assert acct.client.get_account.call_count == 2


def test_an_auth_failure_is_not_cached():
    """Caching an all-None snapshot would keep a recovered account looking dead for
    the whole TTL. Only a real answer is cached."""
    acct = _cached_account()
    acct.client.get_account.side_effect = Exception("401 unauthorized")
    assert acct.get_account_snapshot() == AccountSnapshot()

    acct.client.get_account.side_effect = None
    assert acct.get_account_snapshot().equity == 25000.00


def test_a_bare_instance_without_the_cache_attribute_still_works():
    """object.__new__ instances (the test idiom, and any subclass that skips the
    __init__ seeding) must not AttributeError on the first read."""
    acct = _bare_account()
    acct.client.get_account.return_value = _trade_account()
    acct.client.get_account_configurations.return_value = MagicMock(fractional_trading=True)

    assert acct.get_account_snapshot().equity == 25000.00


def test_a_burst_of_position_size_validations_makes_one_round_trip(monkeypatch):
    """THE REGRESSION, end to end: _validate_position_size_limits reads equity via
    get_account_snapshot(), which on Alpaca is get_account() +
    get_account_configurations(). Validating a basket of orders back-to-back must
    cost ONE pair of calls, not one pair per order."""
    from tests.factories import (
        create_account_definition, create_expert_instance, create_transaction,
    )
    from ba2_trade_platform.core.db import add_instance
    from ba2_trade_platform.core.models import ExpertSetting, TradingOrder
    from ba2_trade_platform.core.types import (
        OrderDirection, OrderStatus, OrderType, TransactionStatus,
    )

    acct_def = create_account_definition()
    acct = _cached_account()
    acct.id = acct_def.id
    monkeypatch.setattr(acct, "get_instrument_current_price", lambda symbol: 150.0)

    expert_instance = create_expert_instance(
        account_id=acct_def.id, expert="MockExpert", virtual_equity_pct=100.0)
    add_instance(
        ExpertSetting(instance_id=expert_instance.id,
                      key="max_virtual_equity_per_instrument_percent",
                      value_str=None, value_float=100.0),
        expunge_after_flush=True,
    )

    class _Resolver:
        def get_expert_instance(self, expert_id):
            class _E:
                def get_available_balance(self, exclude_transaction_id=None):
                    return 1_000_000.0
            return _E()

        def get_account_instance(self, account_id):
            raise NotImplementedError

        def get_account_instance_from_transaction(self, transaction):
            raise NotImplementedError

    monkeypatch.setattr("ba2_common.core.instance_resolver._resolver", _Resolver())

    for _ in range(8):
        txn = create_transaction(symbol="AAPL", quantity=0.0, side=OrderDirection.BUY,
                                 status=TransactionStatus.WAITING, open_price=150.0,
                                 expert_id=expert_instance.id)
        order = TradingOrder(account_id=acct_def.id, symbol="AAPL", quantity=1.0,
                             side=OrderDirection.BUY, order_type=OrderType.MARKET,
                             status=OrderStatus.PENDING, transaction_id=txn.id)
        assert acct._validate_position_size_limits(order) == []

    assert acct.client.get_account.call_count == 1
    assert acct.client.get_account_configurations.call_count == 1
