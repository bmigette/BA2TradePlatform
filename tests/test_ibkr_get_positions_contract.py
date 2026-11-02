"""IBKR must honour the tri-state ``get_positions()`` contract at the SOURCE.

``ReadOnlyAccountInterface.get_positions`` is explicit: a list on success, ``[]`` for a CONFIRMED flat
account, and ``None`` when the FETCH ITSELF FAILED.

THE BUG THESE TESTS PIN. ``IBKRAccount.get_positions`` used to end in ``except Exception: ... return []``.
Returning ``[]`` on an error tells every caller "the broker holds nothing", which is the input auto-close
logic acts on -- it DEFEATS every correct ``is None`` guard in the codebase. In particular
``reconcile_externally_closed_transactions`` would mass-close the entire IBKR book on any transient API
failure, which is exactly the 2026-07-03 Alpaca incident (8 real open transactions closed in the DB during
a DNS outage) with the broker name swapped.

History: these tests originally ran against a MagicMock with ``__init__`` bypassed because the class was
abstract and could not be built. IBKRAccount is now a real, concrete adapter, so they run against the
behavioural FakeIB (tests/ibkr_fakes.py) -- same assertions, no network.
"""
import typing

import pytest

from ba2_trade_platform.modules.accounts.IBKRAccount import IBKRAccount
from tests.ibkr_helpers import make_account

ACCOUNT_NUMBER = "DU1234567"


@pytest.fixture
def world(monkeypatch):
    account, fake = make_account(monkeypatch)
    aapl = fake.add_stock("AAPL", 265598)
    yield account, fake, aapl
    account.close()


def test_returns_none_when_the_fetch_fails(world):
    """THE BUG: an API failure reported the account as FLAT."""
    account, fake, _ = world
    fake.fail_calls["positions"] = ConnectionError("TWS socket closed")

    result = account.get_positions()

    assert result is None, (
        "IBKR reported a fetch failure as a flat account. Every `is None` guard in the "
        f"codebase is defeated by this and reconcile would close the whole book. Got: {result!r}")


def test_returns_empty_list_when_the_account_is_genuinely_flat(world):
    """[] must stay reachable and must mean CONFIRMED-flat, not "something broke"."""
    account, *_ = world
    assert account.get_positions() == []


def test_returns_the_positions_the_broker_holds(world):
    account, fake, aapl = world
    fake.add_position(aapl, 10.0, 150.0, mark=155.0)

    positions = account.get_positions()

    assert len(positions) == 1
    assert positions[0].symbol == "AAPL"
    assert positions[0].qty == 10.0


def test_a_confirmed_book_holding_only_other_accounts_rows_is_flat_not_failed(world):
    """Filtering every row out is a CONFIRMED empty book, so [] -- never None."""
    account, fake, aapl = world
    fake.add_position(aapl, 10.0, 150.0, mark=155.0, account="DU9999999")

    assert account.get_positions() == []


def test_the_declared_return_type_admits_none():
    """A signature saying `-> List[Position]` is what let `return []` look correct."""
    hints = typing.get_type_hints(IBKRAccount.get_positions)

    assert type(None) in typing.get_args(hints["return"]), (
        f"get_positions is declared {hints['return']!r}; the tri-state contract requires "
        "Optional[List[Position]], matching AlpacaAccount/TastyTradeAccount")


def test_a_gateway_that_is_down_is_a_failed_fetch_not_a_flat_book(world):
    account, fake, _ = world
    fake.connect_failure = ConnectionRefusedError("refused")

    assert account.get_positions() is None
