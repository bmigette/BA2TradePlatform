"""services.trades TP/SL: the branch matrix the Live Trades dialog has always had, the guards, the lock rule."""
import pytest

from ba2_trade_platform.core.db import get_instance
from ba2_trade_platform.core.models import Transaction
from ba2_trade_platform.core.types import OrderDirection, OrderStatus, OrderType, TransactionStatus
from ba2_trade_platform.services import trades as svc
from ba2_trade_platform.services.errors import BrokerRefused, BrokerUnreachable, Conflict, Invalid, NotFound
from tests.api_write_fakes import RecordingAccount, install_account
from tests.factories import create_account_definition, create_expert_instance, create_trading_order, create_transaction

pytestmark = pytest.mark.api_db


@pytest.fixture
def acct(monkeypatch):
    account = RecordingAccount()
    install_account(monkeypatch, account)
    return account


def _txn(*, tp=120.0, sl=90.0, side=OrderDirection.BUY, status=TransactionStatus.OPENED, with_expert=False, **kw):
    a = create_account_definition()
    expert = create_expert_instance(account_id=a.id) if with_expert else None
    t = create_transaction(symbol="AAPL", side=side, status=status, open_price=100.0, take_profit=tp, stop_loss=sl,
                           expert_id=expert.id if expert else None, **kw)
    create_trading_order(a.id, symbol="AAPL", transaction_id=t.id, status=OrderStatus.FILLED)
    return t


# --- plan_tp_sl: the dialog's wording and call per case ---------------------------------------------------------

@pytest.mark.parametrize("old,new,method,args,message", [
    ((None, None), (130.0, 80.0), "adjust_tp_sl", (130.0, 80.0), "TP/SL updated to $130.00/$80.00"),
    ((120.0, 90.0), (130.0, 80.0), "adjust_tp_sl", (130.0, 80.0), "TP/SL updated to $130.00/$80.00"),
    ((120.0, 90.0), (130.0, 90.0), "adjust_tp", (130.0,), "Take Profit updated to $130.00"),
    ((120.0, 90.0), (120.0, 80.0), "adjust_sl", (80.0,), "Stop Loss updated to $80.00"),
    ((120.0, 90.0), (None, 90.0), "adjust_sl", (90.0,), "Take Profit removed, Stop Loss kept"),
    ((120.0, 90.0), (120.0, None), "adjust_tp", (120.0,), "Stop Loss removed, Take Profit kept"),
    ((120.0, 90.0), (130.0, None), "adjust_tp", (130.0,), "TP updated to $130.00, SL removed"),
    ((120.0, 90.0), (None, 80.0), "adjust_sl", (80.0,), "SL updated to $80.00, TP removed"),
    ((120.0, 90.0), (None, None), "cancel_standing", (), "TP/SL removed"),
    ((120.0, None), (None, None), "cancel_standing", (), "Take Profit removed"),
    ((None, 90.0), (None, None), "cancel_standing", (), "Stop Loss removed"),
])
def test_plan_matrix(old, new, method, args, message):
    txn = Transaction(symbol="X", quantity=1, side=OrderDirection.BUY, take_profit=old[0], stop_loss=old[1])
    step = svc.plan_tp_sl(txn, *new)
    assert step.method == method and step.ok == message
    assert tuple(v for v in (step.take_profit, step.stop_loss) if v is not None) == args


def test_plan_unchanged_is_none():
    txn = Transaction(symbol="X", quantity=1, side=OrderDirection.BUY, take_profit=120.0, stop_loss=90.0)
    assert svc.plan_tp_sl(txn, 120.0, 90.0) is None


# --- apply_tp_sl -------------------------------------------------------------------------------------------------

def test_apply_persists_then_calls_the_account(acct):
    t = _txn()
    txn, account = svc.tp_sl_context(t.id)
    result = svc.apply_tp_sl(account, txn, 130.0, 80.0, source="manual")
    assert acct.calls == [("adjust_tp_sl", t.id, 130.0, 80.0, "manual")]
    assert result.success and result.message == "TP/SL updated to $130.00/$80.00"
    stored = get_instance(Transaction, t.id)
    assert (stored.take_profit, stored.stop_loss) == (130.0, 80.0)


def test_apply_reports_a_refusal_and_a_raise(acct):
    t = _txn()
    txn, account = svc.tp_sl_context(t.id)
    acct.returns["adjust_tp"] = False
    r = svc.apply_tp_sl(account, txn, 130.0, 90.0, source="manual")
    assert not r.success and r.message == "Failed to update Take Profit" and r.error is None
    acct.raises["adjust_tp"] = RuntimeError("boom")
    txn, account = svc.tp_sl_context(t.id)
    r = svc.apply_tp_sl(account, txn, 140.0, 90.0, source="manual")
    assert not r.success and r.message == "Error updating TP: boom" and isinstance(r.error, RuntimeError)


def test_apply_unchanged_calls_nothing(acct):
    t = _txn()
    txn, account = svc.tp_sl_context(t.id)
    r = svc.apply_tp_sl(account, txn, 120.0, 90.0, source="manual")
    assert r.step is None and acct.calls == []


def test_remove_both_cancels_standing_legs_with_a_broker_id(acct):
    t = _txn()
    a = create_account_definition()
    leg = create_trading_order(a.id, transaction_id=t.id, order_type=OrderType.OCO, status=OrderStatus.OPEN,
                               broker_order_id="b-1")
    create_trading_order(a.id, transaction_id=t.id, order_type=OrderType.OCO, status=OrderStatus.OPEN)   # no broker id
    create_trading_order(a.id, transaction_id=t.id, order_type=OrderType.OCO, status=OrderStatus.CANCELED,
                         broker_order_id="b-2")                                                         # terminal
    txn, account = svc.tp_sl_context(t.id)
    svc.apply_tp_sl(account, txn, None, None, source="manual")
    assert acct.calls == [("cancel_order", leg.id)]


# --- context + validation ----------------------------------------------------------------------------------------

def test_context_errors_use_the_dialog_wording():
    with pytest.raises(NotFound, match="Transaction not found"):
        svc.tp_sl_context(999)
    t = create_transaction()
    with pytest.raises(Invalid, match="No orders linked"):
        svc.tp_sl_context(t.id)


def test_validate_rejects_swapped_values_and_falls_back_to_open_price(acct):
    t = _txn()
    txn, account = svc.tp_sl_context(t.id)
    with pytest.raises(Invalid, match="swapped"):
        svc.validate_tp_sl(account, txn, 90.0, 120.0)
    acct.price = None                       # unreadable: the open price (100) is the reference
    svc.validate_tp_sl(account, txn, 130.0, 80.0)
    with pytest.raises(Invalid):
        svc.validate_tp_sl(account, txn, 95.0, 80.0)


# --- clear_override_locks ----------------------------------------------------------------------------------------

def test_clear_override_locks():
    t = _txn(tp_manual_override=True, sl_manual_override=False)
    assert svc.clear_override_locks(t.id).cleared == ["TP"]
    stored = get_instance(Transaction, t.id)
    assert not stored.tp_manual_override and not stored.sl_manual_override
    assert svc.clear_override_locks(t.id).cleared == []
    with pytest.raises(NotFound):
        svc.clear_override_locks(12345)


# --- set_tp_sl (API entry) ---------------------------------------------------------------------------------------

def test_set_tp_sl_changes_only_the_named_side_with_source_api(acct):
    t = _txn()
    out = svc.set_tp_sl(t.id, take_profit=130.0, stop_loss=None, lock=False, dry_run=False)
    assert acct.calls == [("adjust_tp", t.id, 130.0, "api")]
    assert out["changed"] and out["action"] == "adjust_tp" and out["take_profit"] == 130.0 and out["stop_loss"] == 90.0


def test_set_tp_sl_lock_uses_the_manual_source(acct):
    t = _txn()
    svc.set_tp_sl(t.id, take_profit=130.0, stop_loss=80.0, lock=True, dry_run=False)
    assert acct.calls == [("adjust_tp_sl", t.id, 130.0, 80.0, "manual")]


@pytest.mark.parametrize("tp,sl", [(None, None), (0.0, None), (None, -1.0), (float("nan"), None)])
def test_set_tp_sl_needs_a_real_price(acct, tp, sl):
    t = _txn()
    with pytest.raises(Invalid):
        svc.set_tp_sl(t.id, take_profit=tp, stop_loss=sl, lock=False, dry_run=False)
    assert acct.calls == []


def test_set_tp_sl_wrong_side_of_the_current_price(acct):
    t = _txn()
    with pytest.raises(Invalid, match="Take-profit"):                  # TP below the current price (100) on a long
        svc.set_tp_sl(t.id, take_profit=95.0, stop_loss=None, lock=False, dry_run=False)
    with pytest.raises(Invalid, match="Stop-loss"):
        svc.set_tp_sl(t.id, take_profit=None, stop_loss=105.0, lock=False, dry_run=False)
    assert acct.calls == []


def test_set_tp_sl_validates_the_resulting_state_not_just_the_request(acct):
    t = _txn(tp=120.0, sl=90.0)
    with pytest.raises(Invalid):                         # new SL 125 is above the stored TP 120 (and the price)
        svc.set_tp_sl(t.id, take_profit=None, stop_loss=125.0, lock=False, dry_run=False)


def test_set_tp_sl_short_side(acct):
    t = _txn(tp=80.0, sl=110.0, side=OrderDirection.SELL)
    svc.set_tp_sl(t.id, take_profit=75.0, stop_loss=None, lock=False, dry_run=False)
    with pytest.raises(Invalid):
        svc.set_tp_sl(t.id, take_profit=105.0, stop_loss=None, lock=False, dry_run=False)


def test_set_tp_sl_refuses_a_locked_side_unless_lock_is_given(acct):
    t = _txn(tp_manual_override=True)
    with pytest.raises(Conflict, match="locked"):
        svc.set_tp_sl(t.id, take_profit=130.0, stop_loss=None, lock=False, dry_run=False)
    assert acct.calls == []
    assert get_instance(Transaction, t.id).take_profit == 120.0        # nothing persisted either
    svc.set_tp_sl(t.id, take_profit=130.0, stop_loss=None, lock=True, dry_run=False)
    assert acct.calls == [("adjust_tp", t.id, 130.0, "manual")]
    svc.set_tp_sl(t.id, take_profit=None, stop_loss=85.0, lock=False, dry_run=False)    # the other side is free


@pytest.mark.parametrize("status", [TransactionStatus.CLOSING, TransactionStatus.CLOSED, TransactionStatus.FAILED])
def test_set_tp_sl_refuses_a_transaction_that_is_ending(acct, status):
    t = _txn(status=status)
    with pytest.raises(Conflict):
        svc.set_tp_sl(t.id, take_profit=130.0, stop_loss=None, lock=False, dry_run=False)
    assert acct.calls == []


def test_set_tp_sl_unchanged_is_a_no_op_not_an_error(acct):
    t = _txn()
    out = svc.set_tp_sl(t.id, take_profit=120.0, stop_loss=None, lock=False, dry_run=False)
    assert out["changed"] is False and acct.calls == []


def test_set_tp_sl_dry_run_touches_nothing(acct):
    t = _txn()
    a = create_account_definition()
    create_trading_order(a.id, transaction_id=t.id, order_type=OrderType.OCO, status=OrderStatus.OPEN,
                         broker_order_id="b-1")
    out = svc.set_tp_sl(t.id, take_profit=130.0, stop_loss=80.0, lock=True, dry_run=True)
    assert out["dry_run"] and out["action"] == "adjust_tp_sl" and out["would_set_locks"] == ["TP", "SL"]
    assert [leg["broker_order_id"] for leg in out["standing_legs"]] == ["b-1"]
    assert acct.calls == []
    stored = get_instance(Transaction, t.id)
    assert (stored.take_profit, stored.stop_loss, stored.tp_manual_override) == (120.0, 90.0, False)


def test_set_tp_sl_maps_broker_failures(acct):
    t = _txn()
    acct.returns["adjust_tp"] = False
    with pytest.raises(BrokerRefused):
        svc.set_tp_sl(t.id, take_profit=130.0, stop_loss=None, lock=False, dry_run=False)
    acct.returns.clear()
    acct.raises["adjust_tp"] = ConnectionError("down")
    with pytest.raises(BrokerUnreachable) as e:
        svc.set_tp_sl(t.id, take_profit=135.0, stop_loss=None, lock=False, dry_run=False)
    assert "ConnectionError" in e.value.message and "down" not in e.value.message
    acct.raises["adjust_tp"] = KeyError("defect")
    with pytest.raises(KeyError):
        svc.set_tp_sl(t.id, take_profit=140.0, stop_loss=None, lock=False, dry_run=False)


def test_set_tp_sl_waits_for_the_expert_lock(acct, monkeypatch):
    from ba2_trade_platform.core.TradeManager import TradeManager
    from ba2_trade_platform.services import _locks
    tm = TradeManager()
    monkeypatch.setattr(_locks, "get_trade_manager", lambda: tm)
    t = _txn(with_expert=True)
    expert_id = get_instance(Transaction, t.id).expert_id
    lock = tm._processing_lock(expert_id, "enter_market")
    lock.acquire()
    real = tm.hold_expert_locks
    monkeypatch.setattr(tm, "hold_expert_locks", lambda eid, timeout: real(eid, 0.2))
    try:
        with pytest.raises(Conflict, match="being processed"):
            svc.set_tp_sl(t.id, take_profit=130.0, stop_loss=None, lock=False, dry_run=False)
        assert acct.calls == []
        svc.set_tp_sl(t.id, take_profit=130.0, stop_loss=None, lock=False, dry_run=True)   # a dry run needs no lock
    finally:
        lock.release()
    svc.set_tp_sl(t.id, take_profit=130.0, stop_loss=None, lock=False, dry_run=False)
    assert len(acct.calls) == 1


def test_unlock(acct):
    t = _txn(tp_manual_override=True, sl_manual_override=True)
    assert svc.unlock_tp_sl(t.id, dry_run=True)["cleared"] == ["TP", "SL"]
    assert get_instance(Transaction, t.id).tp_manual_override is True
    assert svc.unlock_tp_sl(t.id, dry_run=False)["cleared"] == ["TP", "SL"]
    assert get_instance(Transaction, t.id).tp_manual_override is False
