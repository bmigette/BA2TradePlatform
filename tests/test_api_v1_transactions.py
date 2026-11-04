"""GET /transactions and /transactions/{id}."""
from datetime import datetime, timedelta, timezone

import pytest

from ba2_trade_platform.core.db import add_instance
from ba2_trade_platform.core.models import ActivityLog
from ba2_trade_platform.core.types import (ActivityLogSeverity, ActivityLogType, OrderDirection,
                                           TransactionStatus)
from tests.api_helpers import v1_client
from tests.factories import (create_account_definition, create_expert_instance,
                             create_trading_order, create_transaction)

pytestmark = pytest.mark.api_db


def _setup():
    acct = create_account_definition()
    expert = create_expert_instance(account_id=acct.id)
    open_tx = create_transaction(symbol="AAPL", status=TransactionStatus.OPENED, expert_id=expert.id)
    closed = create_transaction(symbol="MSFT", status=TransactionStatus.CLOSED, expert_id=expert.id,
                                open_price=100.0, close_price=110.0, quantity=5.0)
    short = create_transaction(symbol="TSLA", status=TransactionStatus.CLOSED, side=OrderDirection.SELL,
                               open_price=100.0, close_price=90.0, quantity=2.0)
    create_trading_order(acct.id, symbol="AAPL", transaction_id=open_tx.id)
    create_trading_order(acct.id, symbol="MSFT", transaction_id=closed.id)
    return acct, expert, open_tx, closed, short


def test_list_filters_and_realized_pnl():
    acct, expert, open_tx, closed, short = _setup()
    c = v1_client()
    body = c.get("/transactions").json()
    assert body["total"] == 3
    by_sym = {i["symbol"]: i for i in body["items"]}
    assert by_sym["MSFT"]["realized_pnl"] == 50.0 and by_sym["TSLA"]["realized_pnl"] == 20.0
    assert by_sym["AAPL"]["realized_pnl"] is None and by_sym["AAPL"]["status"] == "OPENED"
    assert {i["symbol"] for i in c.get(f"/transactions?account={acct.id}").json()["items"]} == {"AAPL", "MSFT"}
    assert [i["symbol"] for i in c.get("/transactions?status=OPENED").json()["items"]] == ["AAPL"]
    assert c.get(f"/transactions?expert={expert.id}&symbol=MSFT").json()["total"] == 1
    assert c.get("/transactions?status=bogus").status_code == 422


def test_list_date_filters_on_open_date():
    _setup()
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert v1_client().get("/transactions", params={"from": future}).json()["total"] == 0


def test_detail_has_orders_locks_and_activity():
    acct, expert, open_tx, *_ = _setup()
    add_instance(ActivityLog(severity=ActivityLogSeverity.INFO, type=ActivityLogType.TP_SL_ADJUSTED,
                             description="tp moved", data={"transaction_id": open_tx.id}),
                 expunge_after_flush=True)
    add_instance(ActivityLog(severity=ActivityLogSeverity.INFO, type=ActivityLogType.TP_SL_ADJUSTED,
                             description="other", data={"transaction_id": 999}),
                 expunge_after_flush=True)
    d = v1_client().get(f"/transactions/{open_tx.id}").json()
    assert d["symbol"] == "AAPL" and d["tp_manual_override"] is False and d["sl_manual_override"] is False
    assert [o["symbol"] for o in d["orders"]] == ["AAPL"]
    assert [a["description"] for a in d["activity"]] == ["tp moved"]
    assert v1_client().get("/transactions/99999").status_code == 404
