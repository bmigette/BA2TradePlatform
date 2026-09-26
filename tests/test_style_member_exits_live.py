"""Style-group member exits in the REAL live open-positions pass: each member closes only its own.

Plan 2026-09-25 option-stage2-build, Task 8 review I1. The live half of
``testplatform/backend/tests/backtest/test_style_member_isolation_engine.py``: a style expert's
OPEN_POSITIONS ruleset ANDs ``entry_tag == <member>`` into every member exit, and
``EntryTagCondition`` reads the tag off the ``existing_order`` the pass resolves. That order is
now picked by the SAME function the backtest engine calls (``entry_order.oldest_entry_order``:
the entry order of the oldest OPENED transaction), so the tests below pin, through
``TradeManager.process_open_positions_recommendations`` with the real ``TradeActionEvaluator``,
``TradeConditions`` and ``CloseAction``:

* member B's always-true exit never closes member A's position, and A's own exit does;
* with both members' positions on the symbol, the oldest OPENED one is managed first, then the
  other once it is gone -- each by its own member's exit;
* an OLDER WAITING entry does not hide the open position (old live code picked the WAITING
  transaction, found no FILLED order on it and no exit on the symbol could fire);
* an entry that filled partly and was then cancelled still carries the position's tag (old live
  code wanted a FILLED order and found none);
* an OPENED position with no entry order row is refused by name, never evaluated against None.

Only the edges are faked: the account class (records ``close_transaction``) and the expert
object (``allow_automated_trade_modification``).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from ba2_common.core.rules_convert import trade_rules_to_live_export
from ba2_common.core.rules_export_import import RulesImporter

from ba2_trade_platform.core.db import add_instance, get_instance, update_instance
from ba2_trade_platform.core.models import TradingOrder, Transaction
from ba2_trade_platform.core.types import (
    AnalysisUseCase, AssetClass, OrderDirection, OrderRecommendation, OrderStatus, OrderType,
    TransactionStatus,
)
from tests.factories import create_account_definition, create_expert_instance, create_recommendation

TAG_A, TAG_B = 7, 3
SYMBOL = "ISOL"
NOW = datetime.now(timezone.utc)


def _member_exit(rid, tag, days_op, days):
    """``days_opened <op> <days> AND entry_tag == tag`` -> close (the style builder's shape: the
    tag leaf is appended last to the root AND)."""
    return {"id": rid, "name": rid, "continue_processing": False,
            "actions": [{"action_type": "close"}],
            "conditions": {"type": "AND", "conditions": [
                {"id": f"{rid}-td", "field": "days_opened", "op": days_op, "value": float(days)},
                {"id": f"{rid}-tag", "field": "entry_tag", "op": "==", "value": float(tag)}]}}


#: B's exit comes FIRST and is true for any B position; A's is true for any A position.
BOTH_ALWAYS = [_member_exit("b_close", TAG_B, ">=", 0), _member_exit("a_close", TAG_A, ">=", 0)]
#: B's exit true for any B position; A's can never fire.
A_NEVER = [_member_exit("b_close", TAG_B, ">=", 0), _member_exit("a_hold", TAG_A, ">", 10_000)]


class _Account:
    closed: list = []

    def __init__(self, account_id):
        self.id = account_id

    def has_pending_closing_order(self, transaction_id):
        return False

    def close_transaction(self, transaction_id):
        type(self).closed.append(transaction_id)
        return {"success": True, "message": "closed", "close_order_id": None}


class _Expert:
    def get_setting_with_interface_default(self, key, log_warning=True):
        return {"allow_automated_trade_modification": True}[key]


@pytest.fixture
def style_world(monkeypatch):
    import ba2_trade_platform.core.utils as core_utils
    import ba2_trade_platform.modules.accounts as accounts_mod

    def build(exit_rules):
        export = trade_rules_to_live_export(exit_rules=exit_rules, name="style-live")
        (ruleset_id,), _warnings = RulesImporter.import_multiple_rulesets(export)
        account_def = create_account_definition()
        inst = create_expert_instance(account_id=account_def.id,
                                      open_positions_ruleset_id=ruleset_id)
        create_recommendation(instance_id=inst.id, symbol=SYMBOL,
                              recommended_action=OrderRecommendation.HOLD,
                              subtype=AnalysisUseCase.OPEN_POSITIONS)
        return SimpleNamespace(expert_id=inst.id, account_id=account_def.id)

    _Account.closed = []
    monkeypatch.setattr(accounts_mod, "get_account_class", lambda provider: _Account)
    monkeypatch.setattr(core_utils, "get_expert_instance_from_id",
                        lambda expert_id, use_cache=True: _Expert())
    return build


def _position(w, tag, *, days_ago, status=TransactionStatus.OPENED,
              order_status=OrderStatus.FILLED, with_order=True):
    """A member's option position: an OPENED (or WAITING) transaction whose entry order carries
    the member tag, as ``_OptionEntryAction`` stamps it (a single-leg short put here)."""
    opened = NOW - timedelta(days=days_ago)
    txn = Transaction(symbol=SYMBOL, quantity=1.0, side=OrderDirection.SELL, status=status,
                      open_price=1.0, expert_id=w.expert_id, created_at=opened,
                      open_date=opened if status == TransactionStatus.OPENED else None,
                      multiplier=100)
    add_instance(txn, expunge_after_flush=True)
    if with_order:
        add_instance(TradingOrder(
            account_id=w.account_id, symbol=SYMBOL, quantity=1.0, side=OrderDirection.SELL,
            order_type=OrderType.SELL_LIMIT, status=order_status, transaction_id=txn.id,
            asset_class=AssetClass.OPTION, multiplier=100, created_at=opened,
            filled_qty=(0.5 if order_status == OrderStatus.CANCELED else None),
            data={"entry_tag": tag}), expunge_after_flush=True)
    return txn


def _pass(expert_id):
    from ba2_trade_platform.core.TradeManager import TradeManager
    return TradeManager().process_open_positions_recommendations(expert_id)


def test_member_Bs_always_true_exit_never_closes_member_As_position(style_world):
    w = style_world(A_NEVER)
    _position(w, TAG_A, days_ago=3)
    _pass(w.expert_id)
    assert _Account.closed == []


def test_each_member_closes_only_its_own_position_oldest_first(style_world):
    """A (older) and B (newer) both hold the symbol (e.g. a lot beside a position). B's exit is
    FIRST and true for any B position, yet the pass manages A's position first and A's own exit
    closes it; once A's is gone, B's exit closes B's."""
    w = style_world(BOTH_ALWAYS)
    a = _position(w, TAG_A, days_ago=5)
    b = _position(w, TAG_B, days_ago=2)
    _pass(w.expert_id)
    assert _Account.closed == [a.id]

    row = get_instance(Transaction, a.id)
    row.status = TransactionStatus.CLOSED
    update_instance(row)
    _pass(w.expert_id)
    assert _Account.closed == [a.id, b.id]


def test_an_older_WAITING_entry_does_not_hide_the_open_position(style_world):
    """B's entry is still working (WAITING, order ACCEPTED) and OLDER than A's open position.
    Old live code took the WAITING transaction as "oldest", found no FILLED order on it, and
    evaluated every exit on the symbol against None: A's exit could never fire."""
    w = style_world(BOTH_ALWAYS)
    _position(w, TAG_B, days_ago=9, status=TransactionStatus.WAITING,
              order_status=OrderStatus.ACCEPTED)
    a = _position(w, TAG_A, days_ago=3)
    _pass(w.expert_id)
    assert _Account.closed == [a.id]


def test_a_partly_filled_then_cancelled_entry_still_scopes_its_position(style_world):
    """The entry order filled partly and the rest was cancelled: the position is OPENED and the
    row carries the tag, but it is not FILLED (old live code wanted FILLED and found nothing)."""
    w = style_world(BOTH_ALWAYS)
    a = _position(w, TAG_A, days_ago=3, order_status=OrderStatus.CANCELED)
    _pass(w.expert_id)
    assert _Account.closed == [a.id]


def test_an_open_position_without_an_entry_order_is_refused_by_name(style_world, monkeypatch):
    """Evaluated against None, every order-reading exit would read False (and CloseAction lose
    its transaction link): the pass skips the symbol with an ERROR naming the transaction."""
    from ba2_trade_platform.core import TradeManager as tm_mod

    errors = []
    monkeypatch.setattr(tm_mod.logger, "error", lambda msg, *a, **k: errors.append(str(msg)))
    w = style_world(BOTH_ALWAYS)
    t = _position(w, TAG_A, days_ago=3, with_order=False)
    _pass(w.expert_id)
    assert _Account.closed == []
    assert any("ENTRY_ORDER_UNRESOLVED" in m and f"transaction {t.id} ({SYMBOL})" in m
               for m in errors), errors
