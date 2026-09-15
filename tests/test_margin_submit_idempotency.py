"""Detached retry objects must not submit a second entry or reserve their own funds."""
import pytest

from ba2_common.core.db import add_instance, get_instance, update_instance
from ba2_common.core.models import TradingOrder
from ba2_common.core.types import OrderDirection, OrderStatus
from tests.test_reductions_survive_the_ceiling import (
    _order, _over_the_ceiling_world, _with_instances,
)


@pytest.mark.usefixtures("reset_test_db")
@pytest.mark.parametrize("exhausted", [False, True])
def test_stale_retry_reads_broker_acceptance_before_rechecking_headroom(monkeypatch, exhausted):
    account, expert, _ = _over_the_ceiling_world()
    account._snap.long_market_value = 0.0
    original = _order(account, side=OrderDirection.BUY, qty=1.0)
    order_id = add_instance(original)
    original = get_instance(TradingOrder, order_id)
    stale = get_instance(TradingOrder, order_id)
    accepted = []

    def submit(order, **kwargs):
        accepted.append(order.id)
        order.broker_order_id = "accepted-once"
        order.status = OrderStatus.NEW
        update_instance(order)
        return get_instance(TradingOrder, order_id)

    monkeypatch.setattr(account, "_submit_order_impl", submit)
    _with_instances(account, expert, lambda: account.submit_order(original))
    assert stale.broker_order_id is None
    # Exhausting the account makes a second validation fail as well: an accepted
    # order should be returned without charging its reservation against itself.
    if exhausted:
        account._snap.long_market_value = 22_000.0
    result = _with_instances(account, expert, lambda: account.submit_order(stale))
    assert result.broker_order_id == "accepted-once"
    assert accepted == [order_id]
