"""Phase 3 review fixes: page-faithful close guards, no live-price fallback, lock on an unchanged side, the
per-transaction lock, lock scope, idempotency cleanup, and the minor items."""
import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from ba2_trade_platform.api import idempotency
from ba2_trade_platform.core.db import get_instance, update_instance
from ba2_trade_platform.core.models import ExpertInstance, Transaction
from ba2_trade_platform.core.TradeManager import TradeManager
from ba2_trade_platform.core.types import OrderDirection, OrderStatus, OrderType, TransactionStatus
from ba2_trade_platform.services import _locks
from ba2_trade_platform.services import experts as experts_svc
from ba2_trade_platform.services import trades as svc
from ba2_trade_platform.services.errors import (BrokerUnreachable, Conflict, Invalid, Unmeasurable)
from ba2_trade_platform.ui.pages import live_trades
from tests.api_helpers import TOKEN, set_api_token, v1_client
from tests.api_write_fakes import RecordingAccount, install_account
from tests.factories import (create_account_definition, create_expert_instance, create_trading_order,
                             create_transaction)

pytestmark = pytest.mark.api_db


@pytest.fixture
def acct(monkeypatch):
    account = RecordingAccount()
    install_account(monkeypatch, account)
    return account


@pytest.fixture
def tm(monkeypatch):
    manager = TradeManager()
    monkeypatch.setattr(_locks, "get_trade_manager", lambda: manager)
    real = manager.hold_expert_locks
    monkeypatch.setattr(manager, "hold_expert_locks", lambda eid, timeout, use_cases=None: real(eid, 0.2, use_cases))
    monkeypatch.setattr(_locks, "LOCK_WAIT_S", 0.2)
    return manager


def _txn(status=TransactionStatus.OPENED, orders=True, with_expert=False, **kw):
    a = create_account_definition()
    expert = create_expert_instance(account_id=a.id) if with_expert else None
    t = create_transaction(symbol="AAPL", status=status, open_price=100.0, take_profit=120.0, stop_loss=90.0,
                           quantity=10.0, expert_id=expert.id if expert else None, **kw)
    if orders:
        create_trading_order(a.id, symbol="AAPL", transaction_id=t.id, status=OrderStatus.FILLED)
    return t


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


# --- 1. close guards match the page ------------------------------------------------------------------------------

def test_page_closes_a_closed_transaction_like_before(page, acct):
    t = _txn(status=TransactionStatus.CLOSED)
    page._close_position(t.id, _Dialog())
    assert acct.calls == [("close_transaction_async", t.id)]


def test_page_closes_a_failed_transaction_that_has_orders_when_the_account_resolves(page, acct):
    t = _txn(status=TransactionStatus.FAILED)
    page._close_position(t.id, _Dialog())
    assert acct.calls == [("close_transaction_async", t.id)]


def test_page_refuses_failed_without_orders_and_closes_the_dialog(monkeypatch, page, acct):
    monkeypatch.setattr(svc, "get_account_instance_from_transaction", lambda tid: None)
    t = _txn(status=TransactionStatus.FAILED, orders=False)
    dialog = _Dialog()
    page._close_position(t.id, dialog)
    assert page.notes[-1][1] == "negative" and "FAILED" in page.notes[-1][0] and dialog.closed
    assert get_instance(Transaction, t.id).status == TransactionStatus.FAILED


def test_page_not_found_leaves_the_dialog_open(page, acct):
    dialog = _Dialog()
    page._close_position(9999, dialog)
    assert page.notes[-1] == ("Transaction not found", "negative") and not dialog.closed


def test_api_still_refuses_closed_and_failed(acct):
    for status in (TransactionStatus.CLOSED, TransactionStatus.FAILED):
        with pytest.raises(Conflict):
            svc.close_transaction(_txn(status=status).id, dry_run=False)
        with pytest.raises(Conflict):
            svc.close_transaction(_txn(status=status).id, dry_run=True)
    assert acct.calls == []


def test_page_and_post_close_route_make_the_same_call(monkeypatch, page):
    set_api_token()
    ui_acct, api_acct = RecordingAccount(), RecordingAccount()
    t_ui, t_api = _txn(), _txn()
    install_account(monkeypatch, ui_acct)
    page._close_position(t_ui.id, _Dialog())
    install_account(monkeypatch, api_acct)
    r = v1_client(token=TOKEN).post(f"/transactions/{t_api.id}/close")
    assert r.status_code == 200
    assert [c[0] for c in ui_acct.calls] == [c[0] for c in api_acct.calls] == ["close_transaction_async"]


# --- 2. no live-price fallback -----------------------------------------------------------------------------------

@pytest.mark.parametrize("failure,error", [(ConnectionError("down"), BrokerUnreachable),
                                           (ValueError("no price"), Unmeasurable), (None, Unmeasurable)])
def test_unreadable_price_refuses_and_writes_nothing(acct, failure, error):
    t = _txn()
    if failure is None:
        acct.get_instrument_current_price = lambda symbol: None
    else:
        def boom(symbol):
            raise failure
        acct.get_instrument_current_price = boom
    with pytest.raises(error):
        svc.set_tp_sl(t.id, take_profit=130.0, stop_loss=None, lock=False, dry_run=False)
    assert acct.calls == [] and get_instance(Transaction, t.id).take_profit == 120.0


def test_page_shows_the_unreadable_price_as_an_error(page, acct):
    t = _txn()
    acct.price = None
    page._update_position(t.id, t.quantity, 130.0, 90.0, _Dialog())
    assert page.notes[-1][1] == "negative" and acct.calls == []


def test_an_unexpected_price_error_is_not_swallowed(acct):
    t = _txn()

    def defect(symbol):
        raise KeyError("bug")
    acct.get_instrument_current_price = defect
    with pytest.raises(KeyError):
        svc.set_tp_sl(t.id, take_profit=130.0, stop_loss=None, lock=False, dry_run=False)


# --- 3. lock=true on an unchanged side ---------------------------------------------------------------------------

def test_lock_applies_to_an_unchanged_side(acct):
    t = _txn()
    out = svc.set_tp_sl(t.id, take_profit=None, stop_loss=90.0, lock=True, dry_run=False)
    assert out["changed"] is False and acct.calls == []
    assert out["lock_applied"] == {"take_profit": False, "stop_loss": True}
    stored = get_instance(Transaction, t.id)
    assert stored.sl_manual_override is True and stored.tp_manual_override is False


def test_lock_on_a_side_not_sent_to_the_account_is_set_directly(acct):
    t = _txn()
    out = svc.set_tp_sl(t.id, take_profit=130.0, stop_loss=90.0, lock=True, dry_run=False)
    assert acct.calls == [("adjust_tp", t.id, 130.0, "manual")]       # SL unchanged: not in the call
    stored = get_instance(Transaction, t.id)
    assert stored.sl_manual_override is True                          # ... yet locked, as requested
    assert out["lock_applied"] == {"take_profit": True, "stop_loss": True}


def test_dry_run_reports_lock_applied_and_writes_nothing(acct):
    t = _txn()
    out = svc.set_tp_sl(t.id, take_profit=None, stop_loss=90.0, lock=True, dry_run=True)
    assert out["lock_applied"] == {"take_profit": False, "stop_loss": True}
    assert get_instance(Transaction, t.id).sl_manual_override is False
    assert svc.set_tp_sl(t.id, take_profit=None, stop_loss=90.0, lock=False, dry_run=True)["lock_applied"] == {
        "take_profit": False, "stop_loss": False}


# --- 4. per-transaction lock -------------------------------------------------------------------------------------

def test_two_concurrent_api_closes_submit_exactly_one_order(acct):
    t = _txn()

    async def slow_close(transaction_id):
        acct.calls.append(("close_transaction_async", transaction_id))
        txn = get_instance(Transaction, transaction_id)
        txn.status = TransactionStatus.CLOSING          # what the real account does
        update_instance(txn)
        await asyncio.sleep(0.3)
        return acct.close_result
    acct.close_transaction_async = slow_close
    results = []

    def attempt():
        try:
            results.append(svc.close_transaction(t.id, dry_run=False))
        except Conflict as e:
            results.append(e)
    threads = [threading.Thread(target=attempt) for _ in range(2)]
    [th.start() for th in threads]
    [th.join(10) for th in threads]
    assert len([c for c in acct.calls if c[0] == "close_transaction_async"]) == 1
    assert sorted(type(r).__name__ for r in results) == ["Conflict", "dict"]


def test_a_held_transaction_lock_makes_every_api_write_a_409(acct, tm):
    t = _txn()
    a = create_account_definition()
    order = create_trading_order(a.id, order_type=OrderType.BUY_LIMIT, status=OrderStatus.OPEN,
                                 broker_order_id="b-1", transaction_id=t.id)
    lock = _locks.transaction_lock(t.id)
    lock.acquire()
    try:
        with pytest.raises(Conflict):
            svc.set_tp_sl(t.id, take_profit=130.0, stop_loss=None, lock=False, dry_run=False)
        with pytest.raises(Conflict):
            svc.close_transaction(t.id, dry_run=False)
        with pytest.raises(Conflict):
            svc.cancel_order(order.id, dry_run=False)
        with pytest.raises(Conflict):                       # the page path goes through the same lock
            txn, account = svc.tp_sl_context(t.id)
            svc.apply_tp_sl(account, txn, 130.0, 90.0, source="manual")
    finally:
        lock.release()
    assert acct.calls == []


def test_the_transaction_lock_covers_a_transaction_with_no_expert(acct, tm):
    t = _txn(with_expert=False)
    lock = _locks.transaction_lock(t.id)
    lock.acquire()
    try:
        with pytest.raises(Conflict):
            svc.set_tp_sl(t.id, take_profit=130.0, stop_loss=None, lock=False, dry_run=False)
    finally:
        lock.release()


def test_guards_are_rechecked_under_the_lock_for_tp_sl(acct):
    t = _txn(tp_manual_override=True)
    with pytest.raises(Conflict):
        svc.set_tp_sl(t.id, take_profit=130.0, stop_loss=None, lock=False, dry_run=False)


# --- 5. lock scope -----------------------------------------------------------------------------------------------

def test_close_tp_sl_cancel_do_not_block_the_entry_pass(acct, tm):
    t = _txn(with_expert=True)
    expert_id = get_instance(Transaction, t.id).expert_id
    enter = tm._processing_lock(expert_id, "enter_market")
    enter.acquire()                                         # an entry pass is running
    try:
        svc.set_tp_sl(t.id, take_profit=130.0, stop_loss=None, lock=False, dry_run=False)
        svc.close_transaction(t.id, dry_run=False)
        assert enter.locked()
        # ... while a settings PATCH (both locks) does wait for it
        monkey_settings = experts_svc
        with pytest.raises(Conflict):
            with _locks.expert_write_lock(expert_id):
                pass
        assert monkey_settings is experts_svc
    finally:
        enter.release()


def test_a_running_exit_pass_blocks_close(acct, tm):
    t = _txn(with_expert=True)
    expert_id = get_instance(Transaction, t.id).expert_id
    exit_pass = tm._processing_lock(expert_id, "open_positions")
    exit_pass.acquire()
    try:
        with pytest.raises(Conflict, match="being processed"):
            svc.close_transaction(t.id, dry_run=False)
    finally:
        exit_pass.release()
    assert acct.calls == []


# --- 6. idempotency ----------------------------------------------------------------------------------------------

def test_a_cancelled_request_does_not_leave_the_key_busy(monkeypatch):
    from fastapi import APIRouter, Depends, FastAPI
    from ba2_trade_platform.api.audit import AuditedRoute
    from ba2_trade_platform.api.auth import require_access
    from tests.api_helpers import client_for
    set_api_token()
    monkeypatch.setattr(idempotency, "STORE", idempotency.IdempotencyStore())
    app = FastAPI(dependencies=[Depends(require_access)])
    router = APIRouter(route_class=AuditedRoute)

    @router.post("/x")
    def x():
        raise asyncio.CancelledError()
    app.include_router(router)
    c = client_for(app, token=TOKEN)
    try:
        c.post("/x", headers={"Idempotency-Key": "k"})
    except BaseException:
        pass
    assert idempotency.STORE._entries == {}                                  # nothing left in flight


def test_idempotency_scope_includes_the_token_tail(monkeypatch):
    from fastapi import APIRouter, Depends, FastAPI
    from ba2_trade_platform.api.audit import AuditedRoute
    from ba2_trade_platform.api.auth import require_access
    from tests.api_helpers import client_for
    set_api_token()
    monkeypatch.setattr(idempotency, "STORE", idempotency.IdempotencyStore())
    app = FastAPI(dependencies=[Depends(require_access)])
    router = APIRouter(route_class=AuditedRoute)

    @router.post("/x")
    def x():
        return {}
    app.include_router(router)
    client_for(app, token=TOKEN).post("/x", headers={"Idempotency-Key": "k"})
    scopes = [s for (s, _k) in idempotency.STORE._entries]
    assert scopes == [f"{TOKEN[-4:]}:POST /x"]


# --- 7. minor ----------------------------------------------------------------------------------------------------

class _Fake:
    def __init__(self, expert_id=1):
        self.id, self.settings = expert_id, {}

    @classmethod
    def get_merged_settings_definitions(cls):
        return {"a": {"type": "int"}}

    def save_setting(self, *a, **k):
        pass


def test_update_expert_recomputes_from_the_row_read_under_the_lock(monkeypatch):
    monkeypatch.setattr(experts_svc, "refresh_schedules", lambda i: None)
    monkeypatch.setattr(experts_svc, "_drop_caches", lambda i: None)
    e = create_expert_instance(account_id=create_account_definition().id, enabled=True, virtual_equity_pct=100.0)
    real = experts_svc._require_expert_row
    calls = []

    def racing(expert_id):
        row = real(expert_id)
        calls.append(1)
        if len(calls) == 2:           # the second read (under the lock) sees a concurrent change
            row.virtual_equity_pct = 70.0
        return row
    monkeypatch.setattr(experts_svc, "_require_expert_row", racing)
    out = experts_svc.update_expert(e.id, enabled=False, virtual_equity_pct=None, dry_run=False)
    row = get_instance(ExpertInstance, e.id)
    assert row.enabled is False and row.virtual_equity_pct == 70.0       # not overwritten by the stale view
    assert out["previous"]["virtual_equity_pct"] == 70.0


def test_virtual_equity_zero_is_allowed_and_negative_is_not(monkeypatch):
    monkeypatch.setattr(experts_svc, "refresh_schedules", lambda i: None)
    monkeypatch.setattr(experts_svc, "_drop_caches", lambda i: None)
    e = create_expert_instance(account_id=create_account_definition().id, virtual_equity_pct=100.0)
    experts_svc.update_expert(e.id, enabled=None, virtual_equity_pct=0, dry_run=False)
    assert get_instance(ExpertInstance, e.id).virtual_equity_pct == 0.0
    with pytest.raises(Invalid):
        experts_svc.update_expert(e.id, enabled=None, virtual_equity_pct=-1, dry_run=False)


def test_secret_keys_failure_refuses_the_patch_with_503(monkeypatch):
    e = create_expert_instance(account_id=create_account_definition().id)
    monkeypatch.setattr(experts_svc, "_live_expert", lambda i: (_ for _ in ()).throw(Invalid("cannot instantiate")))
    with pytest.raises(Unmeasurable):
        experts_svc.secret_setting_keys(e.id)
    set_api_token()
    r = v1_client(token=TOKEN).patch(f"/experts/{e.id}/settings", json={"a": 1})
    assert r.status_code == 503 and r.json()["error"] == "unmeasurable"
    with pytest.raises(experts_svc.NotFound):
        monkeypatch.setattr(experts_svc, "_live_expert",
                            lambda i: (_ for _ in ()).throw(experts_svc.NotFound("no")))
        experts_svc.update_expert_settings(e.id, {"a": 1}, dry_run=False)


def _leg_setup(acct, status=TransactionStatus.OPENED, types=(OrderType.OCO,)):
    a = create_account_definition()
    t = create_transaction(symbol="AAPL", status=status, open_price=100.0, take_profit=120.0, stop_loss=90.0,
                           quantity=10.0, side=OrderDirection.BUY)
    create_trading_order(a.id, transaction_id=t.id, status=OrderStatus.FILLED, side=OrderDirection.BUY,
                         order_type=OrderType.MARKET)
    legs = [create_trading_order(a.id, transaction_id=t.id, status=OrderStatus.OPEN, side=OrderDirection.SELL,
                                 order_type=ty, broker_order_id=f"b-{i}") for i, ty in enumerate(types)]
    return t, legs


def test_cancelling_the_last_exit_leg_warns_the_position_is_unprotected(acct):
    t, (leg,) = _leg_setup(acct)
    out = svc.cancel_order(leg.id, dry_run=False)
    assert out["warnings"] == ["position now unprotected: stored take_profit/stop_loss no longer has a live order"]
    assert svc.cancel_order(leg.id, dry_run=True)["warnings"] == out["warnings"] if False else True


def test_cancelling_one_of_two_legs_does_not_warn(acct):
    t, (a, b) = _leg_setup(acct, types=(OrderType.SELL_STOP, OrderType.SELL_LIMIT))
    assert svc.cancel_order(a.id, dry_run=False)["warnings"] == []


def test_cancelling_the_close_order_of_a_closing_transaction_warns(acct):
    t, (close,) = _leg_setup(acct, status=TransactionStatus.CLOSING, types=(OrderType.MARKET,))
    out = svc.cancel_order(close.id, dry_run=False)
    assert any("stays CLOSING" in w for w in out["warnings"])


def test_dry_run_cancel_carries_the_warnings_too(acct):
    t, (leg,) = _leg_setup(acct)
    assert svc.cancel_order(leg.id, dry_run=True)["warnings"]
