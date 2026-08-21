"""C2: a caller that asks for a protective stop must never get a NAKED position back
reported as success.

``AccountInterface.submit_order`` sends the entry FIRST and only then calls
``adjust_tp`` / ``adjust_sl`` / ``adjust_tp_sl``, each wrapped in
``except NotImplementedError`` -> ``logger.warning(... not set)``. That warning is the
whole of the failure handling: the entry order is still returned as the success value,
so ``TradeManager``, ``FactorRanker.portfolio`` and ``SmartRiskManagerToolkit`` -- all
four ``sl_price=`` call sites -- record a filled position and move on, believing it is
protected.

TastyTrade's ``adjust_*`` stubs returned ``False`` rather than raising, so even that
warning never fired. Fixing the stubs alone is NOT enough: the guard branch only logs.
The position is already live by the time it runs, and there is no honest answer left --
returning the order claims protection that does not exist, and returning ``None``
strands a real broker position the caller thinks never opened.

So the check moved BEFORE the broker call: a broker that cannot attach protective legs
declares ``supports_protective_legs = False`` and ``submit_order`` refuses the request
outright. The only two outcomes are then "the stop exists" or "nothing was opened".

The gate lives in the broker-agnostic base, and MockAccount overrides ``submit_order``,
so these call ``AccountInterface.submit_order(acct, ...)`` directly -- the same trick
``tests/test_washtrade_lock.py`` uses.
"""
import pytest

from tests.conftest import MockAccount
from tests.factories import create_account_definition, create_transaction
from ba2_trade_platform.core.interfaces.AccountInterface import AccountInterface
from ba2_trade_platform.core.models import TradingOrder
from ba2_trade_platform.core.types import (
    OrderDirection, OrderStatus, OrderType, TransactionStatus,
)


class _CapableAccount(MockAccount):
    """A broker that CAN attach protective legs (Alpaca's behaviour)."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.impl_calls = []
        self.adjust_calls = []

    def _submit_order_impl(self, trading_order, tp_price=None, sl_price=None,
                           is_closing_order=False, use_complex_order=False):
        self.impl_calls.append({'tp': tp_price, 'sl': sl_price})
        return super()._submit_order_impl(
            trading_order, tp_price=tp_price, sl_price=sl_price,
            is_closing_order=is_closing_order, use_complex_order=use_complex_order)

    def adjust_tp_sl(self, transaction, new_tp_price=None, new_sl_price=None, source=""):
        self.adjust_calls.append('tp_sl')
        return True

    def adjust_tp(self, transaction, new_tp_price, source=""):
        self.adjust_calls.append('tp')
        return True

    def adjust_sl(self, transaction, new_sl_price, source=""):
        self.adjust_calls.append('sl')
        return True


class _LegLessAccount(_CapableAccount):
    """A broker that CANNOT attach protective legs -- TastyTrade's situation."""

    supports_protective_legs = False

    def adjust_tp_sl(self, transaction, new_tp_price=None, new_sl_price=None, source=""):
        raise NotImplementedError("no protective legs on this broker")

    def adjust_tp(self, transaction, new_tp_price, source=""):
        raise NotImplementedError("no protective legs on this broker")

    def adjust_sl(self, transaction, new_sl_price, source=""):
        raise NotImplementedError("no protective legs on this broker")


def _entry_order(acct):
    txn = create_transaction(symbol="AAPL", side=OrderDirection.BUY,
                            status=TransactionStatus.WAITING)
    return TradingOrder(account_id=acct.id, symbol="AAPL", quantity=1.0,
                        side=OrderDirection.BUY, order_type=OrderType.MARKET,
                        status=OrderStatus.PENDING, transaction_id=txn.id)


def test_a_requested_stop_a_broker_cannot_place_refuses_before_anything_is_opened():
    acct = _LegLessAccount(create_account_definition().id)
    order = _entry_order(acct)

    with pytest.raises(ValueError) as exc:
        AccountInterface.submit_order(acct, order, sl_price=90.0)

    assert "protective" in str(exc.value).lower()
    assert acct.impl_calls == [], "no order may reach the broker"
    assert order.status != OrderStatus.FILLED


def test_a_requested_take_profit_a_broker_cannot_place_is_refused_too():
    acct = _LegLessAccount(create_account_definition().id)
    order = _entry_order(acct)

    with pytest.raises(ValueError):
        AccountInterface.submit_order(acct, order, tp_price=200.0)

    assert acct.impl_calls == []


def test_the_same_broker_still_submits_orders_that_ask_for_no_protection():
    """The refusal is scoped to the protective request, not to the broker."""
    acct = _LegLessAccount(create_account_definition().id)

    result = AccountInterface.submit_order(acct, _entry_order(acct))

    assert result is not None
    assert result.status == OrderStatus.FILLED
    assert len(acct.impl_calls) == 1


def test_a_broker_that_can_place_legs_is_unaffected():
    acct = _CapableAccount(create_account_definition().id)

    result = AccountInterface.submit_order(acct, _entry_order(acct), sl_price=90.0)

    assert result is not None
    assert acct.impl_calls[-1]['sl'] == 90.0
    assert acct.adjust_calls == ['sl']
