"""Tests for model methods on Transaction, TradingOrder, MarketAnalysis."""
import pytest
from ba2_trade_platform.core.models import (
    Transaction, TradingOrder, MarketAnalysis,
)
from ba2_trade_platform.core.types import (
    OrderDirection, OrderType, OrderStatus, TransactionStatus,
    MarketAnalysisStatus, AnalysisUseCase,
)
from ba2_trade_platform.core.db import add_instance
from tests.factories import (
    create_account_definition, create_transaction, create_trading_order,
    create_expert_instance,
)


class TestTransactionAsString:
    def test_as_string_contains_key_fields(self):
        txn = Transaction(
            id=1, symbol="AAPL", quantity=10.0,
            status=TransactionStatus.OPENED,
            side=OrderDirection.BUY,
            open_price=150.0,
        )
        s = txn.as_string()
        assert "AAPL" in s
        assert "10.0" in s
        assert "OPENED" in s
        assert "150.0" in s

    def test_repr_equals_as_string(self):
        txn = Transaction(
            id=1, symbol="MSFT", quantity=5.0,
            status=TransactionStatus.OPENED,
            side=OrderDirection.BUY,
        )
        assert repr(txn) == txn.as_string()

    def test_str_equals_as_string(self):
        txn = Transaction(
            id=1, symbol="MSFT", quantity=5.0,
            status=TransactionStatus.OPENED,
            side=OrderDirection.BUY,
        )
        assert str(txn) == txn.as_string()


class TestTransactionGetCurrentOpenQty:
    def test_no_orders_returns_zero(self):
        acct_def = create_account_definition()
        txn = create_transaction(symbol="AAPL", quantity=10.0)
        assert txn.get_current_open_qty() == 0.0

    def test_with_filled_buy_order(self):
        acct_def = create_account_definition()
        txn = create_transaction(symbol="AAPL", quantity=10.0)
        create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=10.0,
            side=OrderDirection.BUY, status=OrderStatus.FILLED,
            transaction_id=txn.id, filled_qty=10.0,
        )
        assert txn.get_current_open_qty() == 10.0

    def test_with_filled_sell_order_subtracts(self):
        acct_def = create_account_definition()
        txn = create_transaction(symbol="AAPL", quantity=10.0)
        create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=10.0,
            side=OrderDirection.BUY, status=OrderStatus.FILLED,
            transaction_id=txn.id, filled_qty=10.0,
        )
        create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=5.0,
            side=OrderDirection.SELL, status=OrderStatus.FILLED,
            transaction_id=txn.id, filled_qty=5.0,
        )
        assert txn.get_current_open_qty() == 5.0

    def test_unfilled_orders_not_counted(self):
        acct_def = create_account_definition()
        txn = create_transaction(symbol="AAPL", quantity=10.0)
        create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=10.0,
            side=OrderDirection.BUY, status=OrderStatus.PENDING,
            transaction_id=txn.id, filled_qty=None,
        )
        assert txn.get_current_open_qty() == 0.0


class TestTradingOrderAsString:
    def test_as_string_contains_key_fields(self):
        order = TradingOrder(
            id=1, account_id=1, symbol="AAPL", quantity=10.0,
            side=OrderDirection.BUY, order_type=OrderType.MARKET,
            status=OrderStatus.PENDING,
        )
        s = order.as_string()
        assert "AAPL" in s
        assert "BUY" in s
        assert "PENDING" in s

    def test_repr_equals_as_string(self):
        order = TradingOrder(
            id=1, account_id=1, symbol="AAPL", quantity=10.0,
            side=OrderDirection.BUY, order_type=OrderType.MARKET,
            status=OrderStatus.PENDING,
        )
        assert repr(order) == order.as_string()


class TestMarketAnalysisStateDefault:
    def test_state_defaults_to_empty_dict_when_none(self):
        ma = MarketAnalysis(
            symbol="AAPL",
            expert_instance_id=1,
            status=MarketAnalysisStatus.PENDING,
            state=None,
        )
        assert ma.state == {}

    def test_state_preserves_value(self):
        ma = MarketAnalysis(
            symbol="AAPL",
            expert_instance_id=1,
            status=MarketAnalysisStatus.PENDING,
            state={"key": "value"},
        )
        assert ma.state == {"key": "value"}

    def test_state_defaults_without_explicit_none(self):
        ma = MarketAnalysis(
            symbol="AAPL",
            expert_instance_id=1,
            status=MarketAnalysisStatus.PENDING,
        )
        assert ma.state == {}


# ---------------------------------------------------------------------------
# An UNMEASURABLE fill must not read as a measured zero.
#
# ``get_current_open_qty`` counted an order with ``if order.status in executed and
# order.filled_qty:`` -- one truthiness test doing two different jobs. An EXECUTED
# order whose ``filled_qty`` is NULL means "the broker told us it filled but never
# told us how much": genuinely unknown. The old expression silently dropped it and
# returned a total that looks exactly like a measured number, which then flowed into
# ``AccountInterface.submit_close_order_for_transaction`` (net 0 -> fell back to the
# stale ordered quantity) and into Smart-RM close sizing.
#
# A ``filled_qty`` of exactly 0.0, by contrast, IS a measurement ("nothing filled")
# and must stay silent. Both directions are pinned below.
# ---------------------------------------------------------------------------

def _capture_model_errors(monkeypatch):
    """Collect ``logger.error`` text from the shared ba2_common logger.

    NOT caplog: ``logger.py`` sets ``propagate = False``, so caplog's root handler
    sees nothing. ``models.get_current_open_qty`` imports the logger lazily from
    ``ba2_common.logger``, so patching that object's ``.error`` catches it.
    """
    import ba2_common.logger as _log
    messages = []
    monkeypatch.setattr(_log.logger, "error", lambda msg, *a, **k: messages.append(str(msg)))
    return messages


class TestGetCurrentOpenQtyUnmeasurableFill:
    def test_executed_order_with_null_filled_qty_is_logged_loudly(self, monkeypatch):
        """THE DEFECT: a FILLED order with no filled_qty was dropped in silence."""
        acct_def = create_account_definition()
        txn = create_transaction(symbol="AAPL", quantity=10.0)
        create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=10.0,
            side=OrderDirection.BUY, status=OrderStatus.FILLED,
            transaction_id=txn.id, filled_qty=None,
        )
        errors = _capture_model_errors(monkeypatch)

        qty = txn.get_current_open_qty()

        assert errors, (
            "an EXECUTED order with no filled_qty is unmeasurable, not zero -- "
            "it must not be dropped silently")
        assert any("filled_qty" in m for m in errors), errors
        # The value itself is unchanged (the tri-state does not fit the float return);
        # the log is what makes the gap visible. See the report for that decision.
        assert qty == 0.0

    def test_a_measured_zero_fill_stays_silent(self, monkeypatch):
        """THE INVERSE: ``filled_qty == 0.0`` on an executed order is a MEASUREMENT
        ('nothing filled'), and must neither log nor change the total."""
        acct_def = create_account_definition()
        txn = create_transaction(symbol="AAPL", quantity=10.0)
        create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=10.0,
            side=OrderDirection.BUY, status=OrderStatus.FILLED,
            transaction_id=txn.id, filled_qty=0.0,
        )
        errors = _capture_model_errors(monkeypatch)

        assert txn.get_current_open_qty() == 0.0
        assert errors == [], ("a measured zero fill is an answer, not a gap", errors)

    def test_a_non_executed_order_with_null_filled_qty_stays_silent(self, monkeypatch):
        """THE INVERSE #2: a PENDING order has no fill yet BY DEFINITION. Splitting the
        truthiness must not turn every resting order into an error."""
        acct_def = create_account_definition()
        txn = create_transaction(symbol="AAPL", quantity=10.0)
        create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=10.0,
            side=OrderDirection.BUY, status=OrderStatus.PENDING,
            transaction_id=txn.id, filled_qty=None,
        )
        errors = _capture_model_errors(monkeypatch)

        assert txn.get_current_open_qty() == 0.0
        assert errors == [], errors

    def test_measurable_siblings_are_still_counted(self, monkeypatch):
        """A gap in one order must not discard the orders that DID report a fill."""
        acct_def = create_account_definition()
        txn = create_transaction(symbol="AAPL", quantity=10.0)
        create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=10.0,
            side=OrderDirection.BUY, status=OrderStatus.FILLED,
            transaction_id=txn.id, filled_qty=10.0,
        )
        create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=4.0,
            side=OrderDirection.SELL, status=OrderStatus.FILLED,
            transaction_id=txn.id, filled_qty=None,
        )
        errors = _capture_model_errors(monkeypatch)

        assert txn.get_current_open_qty() == 10.0
        assert errors, "the unmeasurable SELL must still be reported"


class TestGetCurrentOpenQtyMutationGaps:
    """Gaps a 212-mutation run found in the surrounding accounting."""

    def test_an_unmeasurable_order_does_not_abort_the_scan(self, monkeypatch):
        """``continue``, not ``break``. With the unmeasurable order FIRST, aborting
        would silently drop every later fill and return a smaller 'measured' net."""
        acct_def = create_account_definition()
        txn = create_transaction(symbol="AAPL", quantity=10.0)
        create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=4.0,
            side=OrderDirection.SELL, status=OrderStatus.FILLED,
            transaction_id=txn.id, filled_qty=None,
        )
        create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=10.0,
            side=OrderDirection.BUY, status=OrderStatus.FILLED,
            transaction_id=txn.id, filled_qty=10.0,
        )
        errors = _capture_model_errors(monkeypatch)

        assert txn.get_current_open_qty() == 10.0
        assert errors, "the unmeasurable order is still reported"

    def test_a_resting_order_with_a_partial_fill_is_still_not_counted(self, monkeypatch):
        """Splitting the status test from the fill test must not let a NON-executed
        order through just because it happens to carry a filled_qty."""
        acct_def = create_account_definition()
        txn = create_transaction(symbol="AAPL", quantity=10.0)
        create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=10.0,
            side=OrderDirection.BUY, status=OrderStatus.PENDING,
            transaction_id=txn.id, filled_qty=5.0,
        )
        errors = _capture_model_errors(monkeypatch)

        assert txn.get_current_open_qty() == 0.0
        assert errors == [], errors

    def test_a_partly_filled_SELL_counts_what_filled_not_what_was_ordered(self, monkeypatch):
        acct_def = create_account_definition()
        txn = create_transaction(symbol="AAPL", quantity=10.0)
        create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=10.0,
            side=OrderDirection.BUY, status=OrderStatus.FILLED,
            transaction_id=txn.id, filled_qty=10.0,
        )
        create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=10.0,
            side=OrderDirection.SELL, status=OrderStatus.FILLED,
            transaction_id=txn.id, filled_qty=4.0,
        )

        assert txn.get_current_open_qty() == 6.0
