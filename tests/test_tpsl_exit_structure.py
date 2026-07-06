"""
Tests for the TP/SL exit-order structure decision (AlpacaAccount._target_exit_spec).

The state machine maps what is actually set on a transaction to the single
standing exit order kept at the broker:

  - TP and SL set  -> OCO (limit leg = TP, stop leg = SL)
  - TP only        -> plain limit order (no stop leg)
  - SL only        -> plain stop order (no limit leg)
  - neither        -> None (no standing exit order at all)

Placeholder legs are forbidden: Alpaca rejects far-out-of-scale limit prices,
and any standing SELL wash-trade-blocks (40310000) new BUY orders on the same
symbol from other experts.

_target_exit_spec only touches enums (no self state), so it is exercised via
the unbound method with a plain namespace standing in for the account.
"""
from types import SimpleNamespace

from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
from ba2_trade_platform.core.types import OrderDirection, OrderType


ACCOUNT = SimpleNamespace()  # _target_exit_spec never reads self


def _spec(entry_side, take_profit, stop_loss):
    transaction = SimpleNamespace(take_profit=take_profit, stop_loss=stop_loss)
    entry_order = SimpleNamespace(side=entry_side)
    return AlpacaAccount._target_exit_spec(ACCOUNT, transaction, entry_order)


class TestLongPosition:
    """Entry BUY -> exits are SELL orders."""

    def test_both_gives_oco(self):
        assert _spec(OrderDirection.BUY, 20.0, 10.0) == (OrderType.OCO, 20.0, 10.0, "TPSL")

    def test_tp_only_gives_sell_limit(self):
        assert _spec(OrderDirection.BUY, 20.0, None) == (OrderType.SELL_LIMIT, 20.0, None, "TP")

    def test_sl_only_gives_sell_stop(self):
        assert _spec(OrderDirection.BUY, None, 10.0) == (OrderType.SELL_STOP, None, 10.0, "SL")

    def test_neither_gives_none(self):
        assert _spec(OrderDirection.BUY, None, None) is None

    def test_zero_values_treated_as_unset(self):
        assert _spec(OrderDirection.BUY, 0, 0) is None
        assert _spec(OrderDirection.BUY, 20.0, 0) == (OrderType.SELL_LIMIT, 20.0, None, "TP")
        assert _spec(OrderDirection.BUY, 0, 10.0) == (OrderType.SELL_STOP, None, 10.0, "SL")


class TestShortPosition:
    """Entry SELL -> exits are BUY orders."""

    def test_both_gives_oco(self):
        assert _spec(OrderDirection.SELL, 10.0, 20.0) == (OrderType.OCO, 10.0, 20.0, "TPSL")

    def test_tp_only_gives_buy_limit(self):
        assert _spec(OrderDirection.SELL, 10.0, None) == (OrderType.BUY_LIMIT, 10.0, None, "TP")

    def test_sl_only_gives_buy_stop(self):
        assert _spec(OrderDirection.SELL, None, 20.0) == (OrderType.BUY_STOP, None, 20.0, "SL")

    def test_neither_gives_none(self):
        assert _spec(OrderDirection.SELL, None, None) is None
