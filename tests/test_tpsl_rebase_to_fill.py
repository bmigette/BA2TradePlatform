"""Tests for re-basing a pending TP/SL price off the parent order's actual fill.

When an entry is a market order, its TP/SL are computed at enter time off a
pre-fill reference price (the market order has no fill yet). If the fill comes
in different from that reference, the proportional distance must be preserved by
re-scaling against the actual fill. rebase_price_to_fill() is the pure helper.
"""
import pytest

from ba2_trade_platform.core.TradeManager import TradeManager, rebase_price_to_fill
from ba2_trade_platform.core.types import OrderDirection, OrderStatus, OrderType
from tests.factories import (
    create_account_definition, create_recommendation, create_trading_order, create_transaction,
)


class TestRebasePriceToFill:
    def test_stop_rebases_to_same_pct_of_fill(self):
        # SL set at -6% off the pre-fill reference 10.79 -> 10.1426; the buy then
        # actually filled lower at 10.66. Re-based stop must be -6% of the FILL.
        assert rebase_price_to_fill(10.1426, 10.79, 10.66) == 10.0204

    def test_tp_rebases_proportionally_sign_agnostic(self):
        # A target ABOVE the reference stays proportionally above the fill.
        assert rebase_price_to_fill(11.0, 10.0, 10.5) == 11.55

    def test_noop_when_reference_equals_fill(self):
        assert rebase_price_to_fill(10.1426, 10.66, 10.66) == 10.1426

    def test_returns_unchanged_on_missing_inputs(self):
        assert rebase_price_to_fill(10.0, None, 10.5) == 10.0
        assert rebase_price_to_fill(10.0, 10.0, None) == 10.0
        assert rebase_price_to_fill(None, 10.0, 10.5) is None

    def test_returns_unchanged_on_nonpositive_reference(self):
        assert rebase_price_to_fill(10.0, 0, 10.5) == 10.0
        assert rebase_price_to_fill(10.0, -5.0, 10.5) == 10.0


class TestTpFloorRecheckAgainstRealFill:
    """End-to-end coverage (real DB, TradeManager._check_all_waiting_trigger_orders) for the
    CALX-shaped bug: a market-order entry's TP is computed pre-fill, so a real fill that slips
    against the pre-fill reference can leave the TP under the configured min_take_profit_percent
    floor (or even below breakeven). This re-checks that floor once the real fill is known,
    mirroring the existing SL rebase in the same method."""

    def _setup(self, real_fill, tp_reference, tp_limit_price, min_take_profit_percent=2.0):
        acct = create_account_definition(provider="MockAccount")
        rec = create_recommendation(
            instance_id=1, symbol="CALX", price_at_date=tp_reference,
            min_take_profit_percent=min_take_profit_percent,
        )
        txn = create_transaction(symbol="CALX", quantity=3.0, side=OrderDirection.BUY, open_price=real_fill)
        parent = create_trading_order(
            account_id=acct.id, symbol="CALX", quantity=3.0, side=OrderDirection.BUY,
            order_type=OrderType.MARKET, status=OrderStatus.FILLED, open_price=real_fill,
            expert_recommendation_id=rec.id, transaction_id=txn.id,
        )
        dependent = create_trading_order(
            account_id=acct.id, symbol="CALX", quantity=3.0, side=OrderDirection.SELL,
            order_type=OrderType.SELL_LIMIT, status=OrderStatus.WAITING_TRIGGER,
            limit_price=tp_limit_price, transaction_id=txn.id,
            depends_on_order=parent.id, depends_order_status_trigger=OrderStatus.FILLED,
            data={"tp_percent_target": 0.0, "tpsl_reference_price": tp_reference},
        )
        return parent, dependent, txn

    def test_tp_floor_corrected_against_real_fill(self):
        """CALX's actual numbers: pre-fill reference $37.45, computed TP $35.18 (already BELOW
        the reference, let alone the real $36.80 fill), real fill $36.80, default 2% floor.
        Floor-enforced price must be 36.80 * 1.02 = 37.536."""
        parent, dependent, txn = self._setup(real_fill=36.80, tp_reference=37.45, tp_limit_price=35.18)

        TradeManager()._check_all_waiting_trigger_orders()

        from ba2_trade_platform.core.db import get_instance
        from ba2_trade_platform.core.models import Transaction, TradingOrder
        refreshed = get_instance(TradingOrder, dependent.id)
        assert refreshed.limit_price == pytest.approx(36.80 * 1.02)
        assert refreshed.data.get("tp_floor_rechecked_at_fill") is True
        refreshed_txn = get_instance(Transaction, txn.id)
        assert refreshed_txn.take_profit == pytest.approx(36.80 * 1.02)

    def test_tp_left_untouched_when_already_above_floor(self):
        """A TP that already clears the floor relative to the real fill must NOT be touched --
        this stays a floor re-check, not a full rebase."""
        parent, dependent, txn = self._setup(real_fill=34.96, tp_reference=35.00, tp_limit_price=36.8665)

        TradeManager()._check_all_waiting_trigger_orders()

        from ba2_trade_platform.core.db import get_instance
        from ba2_trade_platform.core.models import TradingOrder
        refreshed = get_instance(TradingOrder, dependent.id)
        assert refreshed.limit_price == pytest.approx(36.8665)
        assert not refreshed.data.get("tp_floor_rechecked_at_fill")

    def test_tp_floor_respects_custom_min_take_profit_percent(self):
        parent, dependent, txn = self._setup(
            real_fill=100.0, tp_reference=100.0, tp_limit_price=101.0, min_take_profit_percent=6.0)

        TradeManager()._check_all_waiting_trigger_orders()

        from ba2_trade_platform.core.db import get_instance
        from ba2_trade_platform.core.models import TradingOrder
        refreshed = get_instance(TradingOrder, dependent.id)
        assert refreshed.limit_price == pytest.approx(106.0)
