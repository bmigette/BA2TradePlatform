"""The exit pass's ``existing_order``: the backtest engine and the LIVE pass pick the SAME row.

Plan 2026-09-25 option-stage2-build, Task 8 review I1. ``EntryTagCondition`` reads a style
member's tag off the order the OPEN_POSITIONS pass resolves, so if the two runtimes resolve
different rows a member's exits fire in one and never in the other. Both now call
``ba2_common.core.entry_order.oldest_entry_order``; this pins it END TO END, from the same book:

* BACKTEST: the real ``DailyBacktestEngine._held_transactions`` (OPENED only, minus a pending
  close) -> ``_oldest_entry_order``, over the backtest's in-memory trade store.
* LIVE: the real ``TradeManager.process_open_positions_recommendations`` over SQLite (the
  OPENED + WAITING query, minus a pending close), with ``TradeActionEvaluator.evaluate`` spied
  to capture the ``existing_order`` it is handed.

Each book is written by one builder into both stores, and the pick is compared by the label on
the chosen row. The books are the cases where the old live code and the old backtest code
disagreed, plus the normal one.

Run from the backend dir:
    python -m pytest tests/backtest/test_entry_order_bt_live_parity.py -q
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from ba2_common.core.types import (
    AnalysisUseCase, AssetClass, OrderDirection, OrderRecommendation, OrderStatus, OrderType,
    Recommendation, TransactionStatus)

from tests.backtest.test_max_loss_stop_engine import _store_mode

SYM = "EOPX"
EXPERT = 1
ACCOUNT = 1
T0 = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0) - timedelta(days=20)


def _txn(label, *, days, status=TransactionStatus.OPENED, side=OrderDirection.SELL,
         open_date=True):
    from ba2_common.core.db import add_instance
    from ba2_common.core.models import Transaction
    when = T0 + timedelta(days=days)
    t = Transaction(symbol=SYM, quantity=1.0, side=side, status=status, open_price=1.0,
                    expert_id=EXPERT, created_at=when, open_date=when if open_date else None,
                    multiplier=100, comment=label)
    add_instance(t, expunge_after_flush=True)
    return t


def _order(txn, label, *, days=0.0, status=OrderStatus.FILLED, side=None, depends_on=None,
           parent=None):
    from ba2_common.core.db import add_instance
    from ba2_common.core.models import TradingOrder
    o = TradingOrder(account_id=ACCOUNT, symbol=SYM, quantity=1.0, side=side or txn.side,
                     order_type=OrderType.SELL_LIMIT, status=status, transaction_id=txn.id,
                     asset_class=AssetClass.OPTION, multiplier=100,
                     created_at=T0 + timedelta(days=days), depends_on_order=depends_on,
                     parent_order_id=parent, data={"mark": label})
    add_instance(o, expunge_after_flush=True)
    return o


def book_normal():
    """One member's single-leg short put: the entry is FILLED on the transaction's side."""
    t = _txn("a", days=1)
    _order(t, "a-entry", days=1)
    return "a-entry"


def book_two_positions_newer_written_first():
    """Two OPENED positions (a lot beside a position); the NEWER one has the lower id."""
    b = _txn("b", days=6)
    _order(b, "b-entry", days=6)
    a = _txn("a", days=2)
    _order(a, "a-entry", days=2)
    return "a-entry"


def book_older_waiting_beside_open():
    """An OLDER WAITING entry still working beside the held position. Old live: the WAITING
    transaction was "oldest", it has no FILLED order -> None."""
    w = _txn("w", days=0, status=TransactionStatus.WAITING, open_date=False)
    _order(w, "w-entry", days=0, status=OrderStatus.ACCEPTED)
    a = _txn("a", days=4)
    _order(a, "a-entry", days=4)
    return "a-entry"


def book_combo_parent_and_net_side_leg():
    """A credit combo: the PARENT (net SELL, carries the entry facts) and its legs, one of them
    a SELL on the net side, all FILLED with the SAME timestamp. Old live: FILLED-on-side by
    created_at with no id tie-break -> parent or leg, whichever the query returned first."""
    t = _txn("c", days=1)
    parent = _order(t, "c-parent", days=1)
    _order(t, "c-leg-short", days=1, parent=parent.id)
    _order(t, "c-leg-long", days=1, side=OrderDirection.BUY, parent=parent.id)
    return "c-parent"


def book_partly_filled_then_cancelled_entry_and_scale_in():
    """The entry filled partly and the rest was cancelled; a later scale-in FILLED on the same
    side. Old live: the scale-in (the oldest FILLED); old backtest: the entry."""
    t = _txn("a", days=1)
    _order(t, "a-entry", days=1, status=OrderStatus.CANCELED)
    _order(t, "a-scale-in", days=3)
    return "a-entry"


def book_older_dependent_leg():
    """A protective leg (``depends_on_order``) stamped EARLIER than its entry (clock skew)."""
    t = _txn("a", days=2)
    entry = _order(t, "a-entry", days=2)
    _order(t, "a-stop", days=1, side=OrderDirection.BUY, depends_on=entry.id)
    return "a-entry"


def book_equal_open_times():
    """Two positions opened on the same instant: the lower transaction id is the older."""
    a = _txn("a", days=2)
    _order(a, "a-entry", days=2)
    b = _txn("b", days=2)
    _order(b, "b-entry", days=2)
    return "a-entry"


BOOKS = [book_normal, book_two_positions_newer_written_first, book_older_waiting_beside_open,
         book_combo_parent_and_net_side_leg,
         book_partly_filled_then_cancelled_entry_and_scale_in, book_older_dependent_leg,
         book_equal_open_times]


class _NoPendingClose:
    def has_pending_closing_order(self, transaction_id):
        return False


def _backtest_pick(book):
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from ba2_common.core import trade_store as ts

    eng = object.__new__(DailyBacktestEngine)
    eng.account = _NoPendingClose()
    with ts.inmem_trades():
        expected = book()
        txns = eng._held_transactions(EXPERT)[SYM]
        order = eng._oldest_entry_order(txns)
        return expected, (order.data or {}).get("mark")


class _LiveAccount(_NoPendingClose):
    def __init__(self, account_id):
        self.id = account_id


class _LiveExpert:
    def get_setting_with_interface_default(self, key, log_warning=True):
        return {"allow_automated_trade_modification": True}[key]


def _live_pick(book, monkeypatch, run):
    from app.services.backtest.backtest_db import (
        backtest_trading_db, seed_account_definition, seed_expert_instance)
    from app.services.backtest.daily_engine import _recommendation_to_expert_recommendation
    from app.services.backtest.default_rulesets import seed_exit_ruleset_from_rules
    from ba2_common.core import trade_store as ts
    from ba2_common.core.TradeActionEvaluator import TradeActionEvaluator
    from ba2_common.core.rule_models import normalize_trade_rules
    from ba2_trade_platform.core.TradeManager import TradeManager

    _store_mode(monkeypatch, "0")
    seen = []

    def spy(self, *args, **kwargs):
        seen.append(kwargs["existing_order"])
        return []

    rules = normalize_trade_rules([{
        "id": "close", "actions": [{"action_type": "close"}],
        "conditions": {"type": "AND", "conditions": [
            {"id": "td", "field": "days_opened", "op": ">=", "value": 0.0}]}}])
    with backtest_trading_db(f"eo-parity-live-{run}"):
        assert ts.inmem_trades_active() is False
        seed_account_definition(ACCOUNT, {"starting_cash": 100_000.0}, provider="Alpaca")
        open_id = seed_exit_ruleset_from_rules(rules, name=f"eo-parity-{run}")
        seed_expert_instance(account_id=ACCOUNT, expert_class_name="_Stub",
                             enter_market_ruleset_id=0, open_positions_ruleset_id=open_id,
                             instance_id=EXPERT)
        _recommendation_to_expert_recommendation(
            Recommendation(signal=OrderRecommendation.HOLD, confidence=50.0, current_price=10.0,
                           details="hold", expected_profit_percent=0.0),
            expert_instance_id=EXPERT, symbol=SYM, as_of=datetime.now(timezone.utc),
            allow_hold=True, subtype=AnalysisUseCase.OPEN_POSITIONS)
        book()
        with patch("ba2_trade_platform.core.utils.get_expert_instance_from_id",
                   return_value=_LiveExpert()), \
             patch("ba2_trade_platform.modules.accounts.get_account_class",
                   return_value=_LiveAccount), \
             patch.object(TradeActionEvaluator, "evaluate", spy):
            TradeManager().process_open_positions_recommendations(EXPERT)
    assert len(seen) == 1, f"the live pass evaluated {len(seen)} times, expected once"
    return (seen[0].data or {}).get("mark") if seen[0] is not None else None


@pytest.mark.parametrize("book", BOOKS, ids=[b.__name__ for b in BOOKS])
def test_the_backtest_and_live_pick_the_same_entry_order(book, monkeypatch):
    expected, bt = _backtest_pick(book)
    live = _live_pick(book, monkeypatch, book.__name__)
    assert bt == live == expected, f"backtest {bt!r}, live {live!r}, expected {expected!r}"
