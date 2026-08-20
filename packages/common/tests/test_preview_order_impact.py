"""AccountInterface.preview_order_impact: a broker-side order dry-run.

CONCRETE and returning None by default. None means "this broker has no precheck",
NOT "the order is free" -- Alpaca has no order-preview endpoint and keeps the
base, so it relies on get_symbol_margin_info() instead. TastyTrade overrides it.
"""
from ba2_common.core.interfaces.AccountInterface import AccountInterface
from ba2_common.core.models import TradingOrder
from ba2_common.core.types import OrderDirection, OrderType


class _StubTradingAccount(AccountInterface):
    """Minimal concrete AccountInterface: only preview_order_impact is under test,
    so every abstract method is a no-op stub purely to satisfy ABC instantiation."""

    def __init__(self, id):
        self.id = id
        self._settings_cache = None

    def _get_instrument_current_price_impl(self, *a, **k): return None
    def _submit_order_impl(self, *a, **k): return None
    def adjust_sl(self, *a, **k): return None
    def adjust_tp(self, *a, **k): return None
    def adjust_tp_sl(self, *a, **k): return None
    def cancel_order(self, *a, **k): return None
    def get_account_info(self): return {}
    def get_balance(self): return None
    def get_balance_history(self, *a, **k): return []
    def get_dividends(self, *a, **k): return []
    def get_filled_trades(self, *a, **k): return []
    def get_order(self, *a, **k): return None
    def get_orders(self, status=None): return []
    def get_positions(self): return []
    def modify_order(self, *a, **k): return None
    def refresh_orders(self, *a, **k): return True
    def refresh_positions(self, *a, **k): return True
    def symbols_exist(self, symbols): return {}


def _order():
    return TradingOrder(account_id=1, symbol="AAPL", quantity=10.0,
                        side=OrderDirection.BUY, order_type=OrderType.MARKET)


def test_preview_order_impact_returns_none_when_the_broker_has_no_precheck():
    assert _StubTradingAccount(1).preview_order_impact(_order()) is None


def test_preview_order_impact_does_not_mutate_or_persist_the_candidate_order():
    """It is a DRY RUN: it must not save the row or stamp a broker_order_id."""
    order = _order()
    _StubTradingAccount(1).preview_order_impact(order)
    assert order.id is None
    assert order.broker_order_id is None
    assert order.quantity == 10.0
