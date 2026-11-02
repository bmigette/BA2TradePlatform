"""Fractional equity quantities stay fractional through percent closes."""

from types import SimpleNamespace

import pytest

from ba2_common.core.TradeActions import TradeAction
from ba2_common.core.types import OrderDirection, TransactionStatus


class _ProbeAction(TradeAction):
    def execute(self):
        raise NotImplementedError

    def get_description(self):
        return "probe"


def _fractional_lot(quantity=0.4018):
    return SimpleNamespace(
        id=1,
        quantity=quantity,
        side=OrderDirection.BUY,
        status=TransactionStatus.OPENED,
        get_current_open_qty=lambda: quantity,
    )


def test_percent_close_preserves_fractional_quantity():
    calls = []
    account = SimpleNamespace(
        reduce_transaction=lambda transaction_id, quantity: (
            calls.append((transaction_id, quantity)) or {
                "success": True,
                "message": "reduced",
                "close_order_ids": [7],
            }),
        close_transaction=lambda transaction_id: {
            "success": True,
            "message": "closed",
            "close_order_id": 8,
        },
    )
    action = _ProbeAction("AAPL", account, "sell")
    action.close_percent = 50.0
    action.create_and_save_action_result = lambda **kwargs: kwargs

    result = action._close_own_position(
        [_fractional_lot()], broker_position=0.4018,
        action_type="sell", what="Sell closes the long")

    assert result["success"] is True
    assert result["data"]["quantity"] == pytest.approx(0.2009)
    assert calls == [(1, pytest.approx(0.2009))]


def test_whole_share_percent_close_keeps_legacy_flooring():
    account = SimpleNamespace(
        reduce_transaction=lambda *_args: {
            "success": True, "message": "reduced", "close_order_ids": [7],
        },
        close_transaction=lambda *_args: {
            "success": True, "message": "closed", "close_order_id": 8,
        },
    )
    action = _ProbeAction("AAPL", account, "sell")
    action.close_percent = 50.0
    action.create_and_save_action_result = lambda **kwargs: kwargs

    result = action._close_own_position(
        [_fractional_lot(3.0)], broker_position=3.0,
        action_type="sell", what="Sell closes the long")

    assert result["success"] is True
    assert result["data"]["quantity"] == 1.0
