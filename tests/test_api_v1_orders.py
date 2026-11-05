"""GET /orders (platform TradingOrder rows)."""
import pytest

from ba2_trade_platform.core.types import OrderStatus
from tests.api_helpers import v1_client
from tests.factories import create_account_definition, create_trading_order, create_transaction

pytestmark = pytest.mark.api_db


def test_orders_filters_and_includes_pending_and_waiting_trigger():
    a1, a2 = create_account_definition(), create_account_definition()
    tx = create_transaction()
    create_trading_order(a1.id, status=OrderStatus.PENDING, transaction_id=tx.id)
    create_trading_order(a1.id, status=OrderStatus.WAITING_TRIGGER, transaction_id=tx.id)
    create_trading_order(a2.id, status=OrderStatus.FILLED)
    c = v1_client()
    assert c.get("/orders").json()["total"] == 3
    assert c.get(f"/orders?account={a1.id}").json()["total"] == 2
    assert c.get(f"/orders?transaction={tx.id}&status=WAITING_TRIGGER").json()["total"] == 1
    assert c.get("/orders?status=nonsense").status_code == 422
    item = c.get("/orders?status=FILLED").json()["items"][0]
    assert item["status"] and item["account_id"] == a2.id and "limit_price" in item


def test_orders_paging():
    a = create_account_definition()
    for _ in range(3):
        create_trading_order(a.id)
    body = v1_client().get("/orders?limit=2&offset=2").json()
    assert body["total"] == 3 and len(body["items"]) == 1
