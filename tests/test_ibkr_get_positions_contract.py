"""IBKR must honour the tri-state ``get_positions()`` contract at the SOURCE.

``ReadOnlyAccountInterface.get_positions`` is explicit: a list on success, ``[]``
for a CONFIRMED flat account, and ``None`` when the FETCH ITSELF FAILED.

THE BUG THESE TESTS PIN. ``IBKRAccount.get_positions`` ended in
``except Exception: ... return []``. Returning ``[]`` on an error tells every
caller "the broker holds nothing", which is the input auto-close logic acts on --
it DEFEATS every correct ``is None`` guard in the codebase. In particular
``reconcile_externally_closed_transactions`` would mass-close the entire IBKR
book on any transient API failure, which is exactly the 2026-07-03 Alpaca
incident (8 real open transactions closed in the DB during a DNS outage) with
the broker name swapped.

No network: the ``ib_async`` client is a mock throughout and the account object
is built without ``__init__`` so nothing tries to reach TWS.
"""
from unittest.mock import MagicMock

import pytest

from ba2_trade_platform.modules.accounts.IBKRAccount import IBKRAccount

ACCOUNT_NUMBER = "DU1234567"


class _ConcreteIBKRAccount(IBKRAccount):
    """IBKRAccount is ABSTRACT as it stands and cannot be instantiated at all.

    It never implements ``get_balance``, ``get_order``, ``get_orders``,
    ``refresh_positions``, ``symbols_exist``, ``adjust_tp``, ``adjust_sl``,
    ``adjust_tp_sl``, ``_submit_order_impl`` or
    ``_get_instrument_current_price_impl``, so ``IBKRAccount(id)`` raises TypeError
    before it ever reaches TWS. (Consistent with ``supports_trading = False`` and
    with there being no IBKR account in the live DB.) The abstract surface is
    stubbed here so ``get_positions`` -- which IS implemented, and IS the return
    contract under test -- can be exercised. Nothing else is called.
    """

    def _stub(self, *a, **kw):                          # pragma: no cover - guard
        raise AssertionError("not part of the get_positions contract under test")

    get_balance = get_order = get_orders = refresh_positions = _stub
    symbols_exist = adjust_tp = adjust_sl = adjust_tp_sl = _stub
    _submit_order_impl = _get_instrument_current_price_impl = _stub


def _account(ib_positions=None, positions_raise=None):
    """An IBKRAccount wired to a mock ib_async client. Never touches TWS."""
    acct = object.__new__(_ConcreteIBKRAccount)  # bypass __init__ -> no connection attempt
    acct.id = 1
    acct._settings_cache = {"account": ACCOUNT_NUMBER, "host": "127.0.0.1",
                            "port": 7497, "client_id": 1}
    acct._connected = True
    acct._authentication_error = None
    acct.ib = MagicMock()
    acct.ib.isConnected.return_value = True     # _ensure_connected is then a no-op
    if positions_raise is not None:
        acct.ib.positions.side_effect = positions_raise
    else:
        acct.ib.positions.return_value = list(ib_positions or [])
    return acct


def _ib_position(symbol="AAPL", qty=10.0, account=ACCOUNT_NUMBER):
    pos = MagicMock()
    pos.account = account
    pos.contract.symbol = symbol
    pos.position = qty
    pos.avgCost = 150.0
    pos.marketPrice = 155.0
    pos.marketValue = 1550.0
    pos.unrealizedPNL = 50.0
    return pos


def test_returns_none_when_the_fetch_fails():
    """THE BUG: an API failure reported the account as FLAT."""
    acct = _account(positions_raise=ConnectionError("TWS socket closed"))

    result = acct.get_positions()

    assert result is None, (
        "IBKR reported a fetch failure as a flat account. Every `is None` guard in the "
        f"codebase is defeated by this and reconcile would close the whole book. Got: {result!r}")


def test_returns_empty_list_when_the_account_is_genuinely_flat():
    """[] must stay reachable and must mean CONFIRMED-flat, not "something broke"."""
    assert _account(ib_positions=[]).get_positions() == []


def test_returns_the_positions_the_broker_holds():
    positions = _account(ib_positions=[_ib_position()]).get_positions()

    assert len(positions) == 1
    assert positions[0].symbol == "AAPL"
    assert positions[0].qty == 10.0


def test_a_confirmed_book_holding_only_other_accounts_rows_is_flat_not_failed():
    """Filtering every row out is a CONFIRMED empty book, so [] — never None."""
    other = _ib_position(account="DU9999999")

    assert _account(ib_positions=[other]).get_positions() == []


def test_the_declared_return_type_admits_none():
    """A signature saying `-> List[Position]` is what let `return []` look correct."""
    import typing

    hints = typing.get_type_hints(IBKRAccount.get_positions)

    assert type(None) in typing.get_args(hints["return"]), (
        f"get_positions is declared {hints['return']!r}; the tri-state contract requires "
        "Optional[List[Position]], matching AlpacaAccount/TastyTradeAccount")
