"""GET /accounts/{id}/portfolio/runs."""
from datetime import datetime, timedelta, timezone

import pytest

from ba2_trade_platform.core.db import add_instance
from ba2_trade_platform.core.models import PortfolioAllocationRun
from ba2_trade_platform.core.types import OrderStatus, OrderType
from tests.api_helpers import v1_client
from tests.factories import create_account_definition, create_trading_order

pytestmark = pytest.mark.api_db


def _run(acct_id, minutes_ago, **kw):
    row = PortfolioAllocationRun(account_id=acct_id, mode="REBALANCE", plan_json={"huge": "x" * 50},
                                 created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago), **kw)
    return add_instance(row, expunge_after_flush=True)


def test_runs_are_paged_newest_first_without_plan_json():
    acct = create_account_definition(name="A", provider="MockAccount")
    ids = [_run(acct.id, m) for m in (30, 20, 10)]
    c = v1_client()
    body = c.get(f"/accounts/{acct.id}/portfolio/runs?limit=2").json()
    assert body["total"] == 3 and [r["id"] for r in body["items"]] == [ids[2], ids[1]]
    assert "plan_json" not in body["items"][0] and "income_consumed_events" not in body["items"][0]
    nxt = c.get(f"/accounts/{acct.id}/portfolio/runs?limit=2&offset=2").json()
    assert [r["id"] for r in nxt["items"]] == [ids[0]]


def test_runs_are_scoped_to_the_account():
    a = create_account_definition(name="A", provider="MockAccount")
    b = create_account_definition(name="B", provider="MockAccount")
    _run(a.id, 5)
    assert v1_client().get(f"/accounts/{b.id}/portfolio/runs").json()["items"] == []
    assert v1_client().get("/accounts/999/portfolio/runs").status_code == 404


def test_fill_status_working_settled_and_unmeasurable():
    acct = create_account_definition(name="A", provider="MockAccount")
    filled = create_trading_order(acct.id, status=OrderStatus.FILLED, filled_qty=10.0, open_price=20.0)
    working = create_trading_order(acct.id, status=OrderStatus.PENDING)
    stop = create_trading_order(acct.id, order_type=OrderType.SELL_STOP, status=OrderStatus.PENDING)
    unpriced = create_trading_order(acct.id, status=OrderStatus.FILLED, filled_qty=5.0)
    settled_run = _run(acct.id, 40, order_ids=[filled.id, stop.id], filled_buy_value=200.0)
    working_run = _run(acct.id, 30, order_ids=[filled.id, working.id])
    bare_run = _run(acct.id, 20, order_ids=[])
    unmeasured_run = _run(acct.id, 10, order_ids=[unpriced.id])
    items = {r["id"]: r for r in v1_client().get(f"/accounts/{acct.id}/portfolio/runs").json()["items"]}
    s = items[settled_run]
    assert s["fill"]["status"] == "settled" and s["fill"]["execution_orders"] == 1 and s["fill"]["orders"] == 2
    assert s["fill"]["measured_buy_value"] == 200.0 and s["filled_buy_value"] == 200.0
    assert items[working_run]["fill"]["status"] == "working"
    assert items[working_run]["fill"]["working_order_ids"] == [working.id]
    assert items[bare_run]["fill"]["status"] == "no_orders"
    u = items[unmeasured_run]["fill"]
    assert u["unmeasurable_order_ids"] == [unpriced.id] and u["measured_buy_value"] is None
    assert items[settled_run]["income_consumed"] is False


def test_runs_list_is_a_pure_read():
    acct = create_account_definition(name="A", provider="MockAccount")
    _run(acct.id, 5)
    # No account object is ever built: there is no broker in the path.
    assert v1_client().get(f"/accounts/{acct.id}/portfolio/runs").status_code == 200
