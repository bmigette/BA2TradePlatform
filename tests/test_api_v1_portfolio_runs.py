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


def test_unfinalised_run_does_not_report_its_recorded_zero_fills():
    acct = create_account_definition(name="A", provider="MockAccount")
    working = create_trading_order(acct.id, status=OrderStatus.PENDING)
    filled = create_trading_order(acct.id, status=OrderStatus.FILLED, filled_qty=1.0, open_price=2.0)
    open_run = _run(acct.id, 20, order_ids=[working.id])
    done_run = _run(acct.id, 10, order_ids=[filled.id], filled_buy_value=2.0,
                    income_consumed_at=datetime.now(timezone.utc))
    # A working run that DID consume income keeps its figures.
    consumed_working = _run(acct.id, 5, order_ids=[working.id], filled_buy_value=7.0,
                            income_consumed_at=datetime.now(timezone.utc))
    items = {r["id"]: r for r in v1_client().get(f"/accounts/{acct.id}/portfolio/runs").json()["items"]}
    assert items[open_run]["filled_buy_value"] is None and items[open_run]["filled_sell_value"] is None
    assert "not finalised" in items[open_run]["filled_note"]
    assert items[done_run]["filled_buy_value"] == 2.0 and items[done_run]["filled_note"] is None
    assert items[consumed_working]["filled_buy_value"] == 7.0


def test_only_protective_legs_is_no_execution_orders():
    acct = create_account_definition(name="A", provider="MockAccount")
    stop = create_trading_order(acct.id, order_type=OrderType.SELL_STOP, status=OrderStatus.PENDING)
    rid = _run(acct.id, 5, order_ids=[stop.id])
    item = v1_client().get(f"/accounts/{acct.id}/portfolio/runs").json()["items"][0]
    assert item["id"] == rid and item["fill"]["status"] == "no_execution_orders"
    assert item["fill"]["orders"] == 1 and item["fill"]["execution_orders"] == 0


def test_a_missing_order_row_is_logged_once_per_request_at_warning(monkeypatch):
    from ba2_trade_platform.logger import logger
    acct = create_account_definition(name="A", provider="MockAccount")
    _run(acct.id, 20, order_ids=[9001, 9002])
    _run(acct.id, 10, order_ids=[9001])
    errors, warnings = [], []
    monkeypatch.setattr(logger, "error", lambda msg, *a, **k: errors.append(str(msg)))
    monkeypatch.setattr(logger, "warning", lambda msg, *a, **k: warnings.append(str(msg)))
    c = v1_client()
    body = c.get(f"/accounts/{acct.id}/portfolio/runs").json()
    assert errors == [] and len(warnings) == 1 and "9001" in warnings[0] and "9002" in warnings[0]
    assert all(r["fill"]["status"] == "working" for r in body["items"])     # a vanished order still stalls the run
    c.get(f"/accounts/{acct.id}/portfolio/runs")
    assert errors == [] and len(warnings) == 2                                # once per request


def test_a_corrupt_order_ids_entry_is_an_envelope_naming_the_run():
    acct = create_account_definition(name="A", provider="MockAccount")
    bad = _run(acct.id, 5, order_ids=[1, "not-an-id"])
    r = v1_client().get(f"/accounts/{acct.id}/portfolio/runs")
    assert r.status_code == 503 and r.json()["error"] == "unmeasurable"
    assert r.json()["detail"]["run_id"] == bad and f"run {bad}" in r.json()["message"]
