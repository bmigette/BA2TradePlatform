"""AlpacaAccount.get_account_snapshot against a REAL pydantic TradeAccount.

This is the whole point of the snapshot: Alpaca hands back a pydantic object with
no .get() and every money field typed Optional[str], while IBKR/TastyTrade hand
back a dict of floats. No live API call is made -- self.client is a MagicMock and
the TradeAccount / Asset objects are constructed from the installed alpaca-py SDK.
"""
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
    """A failing capability probe must not lose the balances we did get."""
    acct = _bare_account()
    acct.client.get_account.return_value = _trade_account()
    acct.client.get_account_configurations.side_effect = Exception("500 server error")

    snap = acct.get_account_snapshot()

    assert snap.buying_power == 50000.00
    assert snap.supports_fractional is False
