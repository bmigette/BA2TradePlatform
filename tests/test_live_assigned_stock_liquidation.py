"""LIVE: stock delivered by a short-put assignment is held or liquidated EXACTLY as the backtest does.

Owner rule: backtest and live are 100% identical -- one shared decision function, two callers.
``assignment_hold.holds_assigned_stock`` decides in the backtest account; live reaches the same
function through ``decide_live_assigned_stock`` with the policy stored on the owning expert
instance. A not-held lot is flagged in the SAME insert that books it and sold by
``assignment_liquidation.sweep_pending_liquidations`` through ``close_transaction`` (no new order
logic). No network: the reconciler's public entry point with canned activities, as
tests/test_assignment_tag_carry.py does it; the owning instance is a stub with the settings dict.
"""
from types import SimpleNamespace

import pytest
from sqlmodel import select

from ba2_common.core import assignment_hold
from ba2_common.core.assignment_liquidation import (
    LIQUIDATION_META_KEY, PENDING_META, sweep_pending_liquidations)
from ba2_trade_platform.core.db import get_db, update_instance
from ba2_trade_platform.core.models import TradingOrder
from ba2_trade_platform.core.types import OptionRight, TransactionStatus

from tests.test_wheel_assignment_order import (
    PUT_OCC, _assign, _equity_txns, _make_alpaca, _seed_short_option)

WHEEL, CSP = 7, 3


def _tag(opt_txn_id, tag):
    with get_db() as session:
        (order,) = session.exec(
            select(TradingOrder).where(TradingOrder.transaction_id == opt_txn_id)).all()
    order.data = {**(order.data or {}), "entry_tag": tag}
    update_instance(order)


@pytest.fixture
def activities(monkeypatch):
    """Capture every log_activity the live path writes."""
    seen = []
    monkeypatch.setattr("ba2_trade_platform.core.db.log_activity",
                        lambda **kw: seen.append(kw))
    monkeypatch.setattr("ba2_common.core.db.log_activity", lambda **kw: seen.append(kw))
    return seen


def _owner(monkeypatch, settings):
    """Make the owning instance resolve to a stub carrying ``settings`` (None: unresolvable)."""
    monkeypatch.setattr(
        "ba2_trade_platform.core.utils.get_expert_instance_from_id",
        lambda _id: None if settings is None else SimpleNamespace(settings=settings))


def _assign_one(account_id, expert_id, act_id, *, tag=None):
    acct = _make_alpaca(account_id)
    opt = _seed_short_option(account_id, expert_id, PUT_OCC, OptionRight.PUT, strike=150.0,
                             contracts=2)
    if tag is not None:
        _tag(opt, tag)
    (res,) = _assign(acct, PUT_OCC, 2, act_id)
    (txn,) = _equity_txns(expert_id)
    return acct, txn, res["result"]


def _pending(txn):
    return isinstance(txn.meta_data, dict) and txn.meta_data.get(LIQUIDATION_META_KEY) == PENDING_META


# ---- the decision, through the real reconciler --------------------------------------------- #
def test_not_held_assignment_flags_exactly_the_delivered_lot(
        mock_account, mock_expert_instance, monkeypatch, activities):
    _owner(monkeypatch, {"hold_assigned_stock": False, "hold_assigned_stock_tags": None})
    _, txn, result = _assign_one(mock_account.id, mock_expert_instance.id, "a1")
    assert _pending(txn) and float(txn.quantity) == 200.0
    assert "LIQUIDATE" in result
    assert not [a for a in activities if "unresolved" in str(a.get("data"))]


def test_held_assignment_is_not_flagged(mock_account, mock_expert_instance, monkeypatch, activities):
    _owner(monkeypatch, {"hold_assigned_stock": True, "hold_assigned_stock_tags": None})
    _, txn, result = _assign_one(mock_account.id, mock_expert_instance.id, "a2")
    assert not _pending(txn) and "HOLD" in result and "unresolved" not in result


@pytest.mark.parametrize("tag,pending", [(WHEEL, False), (CSP, True)])
def test_member_scoped_policy_holds_only_the_listed_member(
        mock_account, mock_expert_instance, monkeypatch, activities, tag, pending):
    _owner(monkeypatch, {"hold_assigned_stock": False, "hold_assigned_stock_tags": [WHEEL]})
    _, txn, _ = _assign_one(mock_account.id, mock_expert_instance.id, f"a3-{tag}", tag=tag)
    assert _pending(txn) is pending


def test_untagged_position_under_member_policy_holds_LOUDLY(
        mock_account, mock_expert_instance, monkeypatch, activities):
    _owner(monkeypatch, {"hold_assigned_stock": False, "hold_assigned_stock_tags": [WHEEL]})
    _, txn, result = _assign_one(mock_account.id, mock_expert_instance.id, "a4")
    assert not _pending(txn)
    assert "unresolved" in result
    (act,) = [a for a in activities if "unresolved" in str(a.get("data"))]
    assert act["source_expert_id"] == mock_expert_instance.id and "no entry tag" in act["description"]


# ---- the missing-setting path -------------------------------------------------------------- #
@pytest.mark.parametrize("settings", [
    {"hold_assigned_stock": None, "hold_assigned_stock_tags": None},     # nothing stored
    {},                                                                  # keys not even declared
    None,                                                                # no owning instance
])
def test_a_missing_policy_HOLDS_and_names_the_instance(
        mock_account, mock_expert_instance, monkeypatch, activities, settings):
    _owner(monkeypatch, settings)
    _, txn, result = _assign_one(mock_account.id, mock_expert_instance.id, "a5")
    assert not _pending(txn), "an undecided policy must never sell"
    assert "HOLD (policy unresolved" in result
    (act,) = [a for a in activities if "unresolved" in str(a.get("data"))]
    assert str(mock_expert_instance.id) in act["description"]
    assert act["severity"].value == "failure"


def test_both_forms_stored_is_refused_loudly_not_resolved_by_precedence(
        mock_account, mock_expert_instance, monkeypatch, activities):
    _owner(monkeypatch, {"hold_assigned_stock": True, "hold_assigned_stock_tags": [WHEEL]})
    _, txn, result = _assign_one(mock_account.id, mock_expert_instance.id, "a6", tag=WHEEL)
    assert not _pending(txn) and "unresolved" in result


def test_an_unattributed_option_holds_loudly(mock_account, monkeypatch, activities):
    """No owning expert id at all (the originating option order was never found)."""
    _owner(monkeypatch, {"hold_assigned_stock": False, "hold_assigned_stock_tags": None})
    acct = _make_alpaca(mock_account.id)
    (res,) = _assign(acct, PUT_OCC, 1, "a7")
    assert "unhandled" in res["result"] or "unresolved" in res["result"] or "called" in res["result"]
    assert not [t for t in _equity_txns(None) if _pending(t)]


# ---- the liquidation sweep ----------------------------------------------------------------- #
class _CloseRecorder:
    def __init__(self, acct_id, outcomes):
        self.id = acct_id
        self.outcomes = list(outcomes)
        self.closed = []

    def close_transaction(self, txn_id):
        self.closed.append(txn_id)
        return self.outcomes.pop(0)


def test_the_sweep_closes_the_pending_lot_once_through_close_transaction(
        mock_account, mock_expert_instance, monkeypatch, activities):
    _owner(monkeypatch, {"hold_assigned_stock": False, "hold_assigned_stock_tags": None})
    _, txn, _ = _assign_one(mock_account.id, mock_expert_instance.id, "a8")
    rec = _CloseRecorder(mock_account.id, [{"success": True, "message": "ok", "close_order_id": 99}])
    (res,) = sweep_pending_liquidations(rec)
    assert rec.closed == [txn.id] and res["success"]


def test_an_unrelated_holding_is_never_swept(mock_account, mock_expert_instance, monkeypatch,
                                             activities):
    """Only flagged lots are closed: a held assignment lot is left alone."""
    _owner(monkeypatch, {"hold_assigned_stock": True, "hold_assigned_stock_tags": None})
    _assign_one(mock_account.id, mock_expert_instance.id, "a9")
    rec = _CloseRecorder(mock_account.id, [])
    assert sweep_pending_liquidations(rec) == [] and rec.closed == []


def test_another_accounts_pending_lot_is_not_swept(mock_account, mock_expert_instance,
                                                   monkeypatch, activities):
    _owner(monkeypatch, {"hold_assigned_stock": False, "hold_assigned_stock_tags": None})
    _assign_one(mock_account.id, mock_expert_instance.id, "a10")
    rec = _CloseRecorder(mock_account.id + 1000, [])
    assert sweep_pending_liquidations(rec) == [] and rec.closed == []


def test_a_failed_sale_is_loud_and_stays_pending_for_the_next_sweep(
        mock_account, mock_expert_instance, monkeypatch, activities):
    _owner(monkeypatch, {"hold_assigned_stock": False, "hold_assigned_stock_tags": None})
    _, txn, _ = _assign_one(mock_account.id, mock_expert_instance.id, "a11")
    rec = _CloseRecorder(mock_account.id, [
        {"success": False, "message": "wash trade 40310000", "close_order_id": None},
        {"success": True, "message": "ok", "close_order_id": 5}])
    (first,) = sweep_pending_liquidations(rec)
    assert not first["success"]
    (fail,) = [a for a in activities if a.get("data", {}).get("assignment_liquidation")]
    assert "wash trade" in fail["description"] and fail["severity"].value == "failure"
    (second,) = sweep_pending_liquidations(rec)          # still pending -> retried
    assert second["success"] and rec.closed == [txn.id, txn.id]


def test_a_raising_close_is_caught_and_recorded_not_propagated(
        mock_account, mock_expert_instance, monkeypatch, activities):
    _owner(monkeypatch, {"hold_assigned_stock": False, "hold_assigned_stock_tags": None})
    _assign_one(mock_account.id, mock_expert_instance.id, "a12")

    class Boom(_CloseRecorder):
        def close_transaction(self, txn_id):
            raise RuntimeError("broker down")

    (res,) = sweep_pending_liquidations(Boom(mock_account.id, []))
    assert not res["success"] and "broker down" in res["message"]


def test_a_closed_lot_leaves_the_sweep(mock_account, mock_expert_instance, monkeypatch, activities):
    _owner(monkeypatch, {"hold_assigned_stock": False, "hold_assigned_stock_tags": None})
    _, txn, _ = _assign_one(mock_account.id, mock_expert_instance.id, "a13")
    txn.status = TransactionStatus.CLOSED
    update_instance(txn)
    rec = _CloseRecorder(mock_account.id, [])
    assert sweep_pending_liquidations(rec) == []


def test_the_refresh_runs_the_sweep_even_when_no_new_activity_arrived(mock_account, monkeypatch):
    from ba2_trade_platform.core.TradeManager import TradeManager
    acct = _make_alpaca(mock_account.id)
    calls = []
    monkeypatch.setattr(type(acct), "get_option_activities", lambda self, after: [])
    monkeypatch.setattr(type(acct), "process_pending_assignment_liquidations",
                        lambda self: calls.append(self.id))
    fake_self = SimpleNamespace(logger=SimpleNamespace(
        info=lambda *a, **k: None, error=lambda *a, **k: None))
    TradeManager._reconcile_account_option_activities(fake_self, acct)
    assert calls == [mock_account.id]


# ---- parity: ONE shared function, two callers ---------------------------------------------- #
@pytest.mark.parametrize("cfg,tag", [
    ({"hold_assigned_stock": True}, None), ({"hold_assigned_stock": False}, None),
    ({"hold_assigned_stock": True}, 5), ({"hold_assigned_stock": False}, 5),
    ({"hold_assigned_stock": False, "hold_assigned_stock_tags": [5]}, 5),
    ({"hold_assigned_stock": False, "hold_assigned_stock_tags": [5]}, 6),
])
def test_live_and_backtest_reach_the_same_decision_through_the_same_functions(
        monkeypatch, cfg, tag):
    bt_calls, live_calls = [], []
    real_resolve, real_holds = (assignment_hold.resolve_assigned_stock_hold_policy,
                                assignment_hold.holds_assigned_stock)

    def spy_holds(policy, entry_tag, *, where):
        live_calls.append((policy, entry_tag))
        return real_holds(policy, entry_tag, where=where)

    monkeypatch.setattr(assignment_hold, "holds_assigned_stock", spy_holds)
    live = assignment_hold.decide_live_assigned_stock(
        {"hold_assigned_stock": cfg["hold_assigned_stock"],
         "hold_assigned_stock_tags": cfg.get("hold_assigned_stock_tags")}, tag, where="live")
    # the backtest account's two calls, verbatim (BacktestAccount._assignment_disposition)
    bt_policy = real_resolve(cfg)
    bt_hold = real_holds(bt_policy, tag, where="bt")
    bt_calls.append((bt_policy, tag))
    assert live.problem is None and live.hold == bt_hold
    assert live_calls == bt_calls, "live must call holds_assigned_stock with the BT's policy+tag"


def test_the_backtest_account_still_decides_through_the_shared_functions():
    """Source pin: if the BT stops calling the shared pair, the parity above pins nothing."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    bt = (root / "testplatform/backend/app/services/backtest/backtest_account.py").read_text(
        encoding="utf-8")
    assert re.search(r"holds_assigned_stock\(\s*resolve_assigned_stock_hold_policy\(self\._cfg\)", bt)
    live = (root / "packages/common/ba2_common/core/assignment_hold.py").read_text(encoding="utf-8")
    assert "holds_assigned_stock(policy, entry_tag" in live
