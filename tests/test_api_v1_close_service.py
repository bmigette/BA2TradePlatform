"""services.trades close: guards, the orphan branch, the dry run, error mapping, and page/API parity."""
import asyncio
from types import SimpleNamespace

import pytest

from ba2_trade_platform.core.db import get_instance
from ba2_trade_platform.core.models import Transaction
from ba2_trade_platform.core.types import OrderStatus, OrderType, TransactionStatus
from ba2_trade_platform.services import trades as svc
from ba2_trade_platform.services.errors import BrokerRefused, BrokerUnreachable, Conflict, NotFound
from ba2_trade_platform.ui.pages import live_trades
from tests.api_write_fakes import RecordingAccount, install_account
from tests.factories import create_account_definition, create_trading_order, create_transaction

pytestmark = pytest.mark.api_db


@pytest.fixture
def acct(monkeypatch):
    account = RecordingAccount()
    install_account(monkeypatch, account)
    return account


def _txn(status=TransactionStatus.OPENED, orders=True, **kw):
    a = create_account_definition()
    t = create_transaction(symbol="AAPL", status=status, quantity=10.0, **kw)
    if orders:
        create_trading_order(a.id, symbol="AAPL", transaction_id=t.id, status=OrderStatus.FILLED)
    return t


@pytest.mark.parametrize("status", [TransactionStatus.CLOSING, TransactionStatus.CLOSED, TransactionStatus.FAILED])
def test_close_refuses_closing_closed_failed(acct, status):
    t = _txn(status=status)
    with pytest.raises(Conflict):
        svc.close_transaction(t.id, dry_run=False)
    with pytest.raises(Conflict):
        svc.close_transaction(t.id, dry_run=True)
    assert acct.calls == []


def test_close_unknown_transaction_is_404(acct):
    with pytest.raises(NotFound):
        svc.close_transaction(4242, dry_run=False)
    with pytest.raises(NotFound):
        svc.close_transaction(4242, dry_run=True)


def test_close_runs_the_accounts_async_close(acct):
    t = _txn()
    out = svc.close_transaction(t.id, dry_run=False)
    assert acct.calls == [("close_transaction_async", t.id)]
    assert out["success"] and out["close_order_id"] == 99 and out["orphaned"] is False


def test_close_failure_result_is_a_502_with_the_platform_message(acct):
    t = _txn()
    acct.close_result = {"success": False, "message": "Close refused: wash trade"}
    with pytest.raises(BrokerRefused, match="wash trade"):
        svc.close_transaction(t.id, dry_run=False)


def test_close_maps_a_raised_broker_error(acct):
    t = _txn()
    acct.raises["close_transaction_async"] = TimeoutError("host:443 timed out")
    with pytest.raises(BrokerUnreachable) as e:
        svc.close_transaction(t.id, dry_run=False)
    assert "host:443" not in e.value.message


def test_orphaned_transaction_is_marked_closed_without_the_broker(monkeypatch, acct):
    monkeypatch.setattr(svc, "get_account_instance_from_transaction", lambda tid: None)
    t = _txn(orders=False)
    out = svc.close_transaction(t.id, dry_run=False)
    assert out["orphaned"] is True and acct.calls == []
    assert get_instance(Transaction, t.id).status == TransactionStatus.CLOSED


def test_dry_run_lists_what_would_be_touched_and_contacts_nothing(acct):
    t = _txn()
    a = create_account_definition()
    live = create_trading_order(a.id, transaction_id=t.id, order_type=OrderType.OCO, status=OrderStatus.OPEN,
                                broker_order_id="b-1")
    waiting = create_trading_order(a.id, transaction_id=t.id, order_type=OrderType.SELL_STOP, status=OrderStatus.WAITING_TRIGGER)
    create_trading_order(a.id, transaction_id=t.id, order_type=OrderType.OCO, status=OrderStatus.CANCELED,
                         broker_order_id="b-2")
    out = svc.close_transaction(t.id, dry_run=True)
    assert [o["id"] for o in out["would_cancel"]] == [live.id]
    assert [o["id"] for o in out["would_delete"]] == [waiting.id]
    assert out["would_submit_close"]["side"] == "SELL" and out["would_submit_close"]["quantity"] == 10.0
    assert acct.calls == [] and get_instance(Transaction, t.id).status == TransactionStatus.OPENED


def test_dry_run_of_an_orphan_says_so_and_writes_nothing(acct):
    t = _txn(orders=False)
    out = svc.close_transaction(t.id, dry_run=True)
    assert out["orphaned"] is True and get_instance(Transaction, t.id).status == TransactionStatus.OPENED


# --- page parity -------------------------------------------------------------------------------------------------

class _Dialog:
    closed = False

    def close(self):
        self.closed = True


@pytest.fixture
def page(monkeypatch):
    notes = []
    monkeypatch.setattr(live_trades.ui, "notify", lambda message, **kw: notes.append((message, kw.get("type"))))
    tab = live_trades.LiveTradesTab.__new__(live_trades.LiveTradesTab)
    tab._refresh_transactions = lambda: None
    tab.notes = notes
    client = SimpleNamespace(safe_invoke=lambda fn: fn())
    monkeypatch.setattr("nicegui.context", SimpleNamespace(client=client))
    monkeypatch.setattr("nicegui.background_tasks.create", lambda coro, name=None: asyncio.run(coro))
    return tab


def test_page_and_api_close_make_the_same_call(monkeypatch, page):
    ui_acct, api_acct = RecordingAccount(), RecordingAccount()
    t_ui, t_api = _txn(), _txn()
    install_account(monkeypatch, ui_acct)
    page._close_position(t_ui.id, _Dialog())
    install_account(monkeypatch, api_acct)
    svc.close_transaction(t_api.id, dry_run=False)
    assert [c[0] for c in ui_acct.calls] == [c[0] for c in api_acct.calls] == ["close_transaction_async"]
    assert ("Close order submitted", "positive") in page.notes


def test_page_shows_the_guard_refusals(monkeypatch, page, acct):
    closing = _txn(status=TransactionStatus.CLOSING)
    dialog = _Dialog()
    page._close_position(closing.id, dialog)
    assert page.notes[-1] == ("Transaction is already being closed", "warning") and dialog.closed
    failed = _txn(status=TransactionStatus.FAILED)
    page._close_position(failed.id, _Dialog())
    assert page.notes[-1][1] == "negative" and "FAILED" in page.notes[-1][0]
    assert acct.calls == []


def test_page_retry_requires_closing_and_orphan_branch(monkeypatch, page, acct):
    opened = _txn()
    page._retry_close_position(opened.id, _Dialog())
    assert page.notes[-1] == ("Transaction is not in CLOSING status", "warning")
    stuck = _txn(status=TransactionStatus.CLOSING)
    page._retry_close_position(stuck.id, _Dialog())
    assert acct.calls == [("close_transaction_async", stuck.id)]
    monkeypatch.setattr(svc, "get_account_instance_from_transaction", lambda tid: None)
    orphan = _txn(status=TransactionStatus.CLOSING, orders=False)
    page._retry_close_position(orphan.id, _Dialog())
    assert page.notes[-1] == ("Transaction closed (was orphaned with no orders)", "positive")
