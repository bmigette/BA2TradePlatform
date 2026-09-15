"""A TP-only ruleset action must not write a PRE-broker transaction back over the
take-profit the broker just persisted (2026-09-10 production review, finding 1).

The live shape, exactly:

  1. ``_AdjustPriceLevelAction.execute()`` loads the Transaction;
  2. ``account.adjust_tp()`` writes ``take_profit`` through the ADAPTER'S OWN session
     (``AlpacaAccount.adjust_tp_sl`` re-fetches the row and commits it), so the copy
     from step 1 is now stale -- its ``take_profit`` is still None on a fresh entry;
  3. ``AdjustTakeProfitAction._post_broker_hook`` stores the TradeConditions target in
     ``meta_data`` and saves. ``db.update_instance`` copies EVERY loaded column, so
     saving the step-1 copy put the stale NULL back over the broker's price.

Production consequence: transactions 203/204/205 (CELH/CAVA/NCLH) ended with
``take_profit`` NULL but ``meta_data.TradeConditionsData.current_target_price`` intact,
and the later ``initial_setup`` stop attachment read that NULL, rebuilt the exit as
stop-only and cancelled all three TP legs.

This file runs on the REAL SQLite test database (the session-scoped ``_isolated_db``
fixture in conftest.py), NOT the in-memory backtest store -- the defect is a
detached-object persistence failure and cannot exist where the store hands back the
same object by identity. The last test pins that backtest equivalence explicitly.
"""
import pytest
from sqlmodel import Session

from ba2_common.core import trade_store as ts
from ba2_common.core.db import add_instance, get_engine, get_instance
from ba2_common.core.models import ExpertRecommendation, Transaction, TradingOrder
from ba2_common.core.TradeActions import AdjustTakeProfitAction
from ba2_common.core.types import (
    OrderDirection, OrderRecommendation, OrderStatus, OrderType, RiskLevel,
    TimeHorizon, TransactionStatus,
)


#: CELH's real ruleset target from the review, kept to full precision: the assertion is
#: that the BROKER's number survives, so a rounded stand-in would hide a re-rounding bug.
CELH_TARGET = 31.2832752
CELH_FILL = 27.3786


class _SeparateSessionAccount:
    """An account whose ``adjust_tp`` persists the take-profit the way the live adapters
    do: through its OWN Session, on its OWN copy of the row.

    That is the whole point of the reproduction -- the object the action is holding is
    NOT the object the broker write touched, so anything the action saves afterwards is
    built on pre-broker column values.
    """

    def __init__(self):
        self.id = 77
        self.adjust_tp_calls = []

    def get_instrument_current_price(self, symbol, price_type="bid"):
        return CELH_FILL

    def adjust_tp(self, transaction, new_tp_price, source=""):
        self.adjust_tp_calls.append((transaction.id, new_tp_price, source))
        with Session(get_engine()) as session:
            row = session.get(Transaction, transaction.id)
            row.take_profit = new_tp_price
            session.add(row)
            session.commit()
        return True

    def adjust_sl(self, transaction, new_sl_price, source=""):
        raise AssertionError("a TP-only action must not touch the stop loss")


class _SameObjectAccount:
    """The BACKTEST shape: the sql-less store holds one object per row, so the 'broker'
    writes the very object the action is holding."""

    def __init__(self):
        self.id = 78

    def get_instrument_current_price(self, symbol, price_type="bid"):
        return CELH_FILL

    def adjust_tp(self, transaction, new_tp_price, source=""):
        transaction.take_profit = new_tp_price
        return True


def _entry_rows(account_id):
    """A funded, TP-only entry: transaction with NO take-profit yet (the fresh-entry
    state the production rows were in), its filled entry order, and the recommendation
    the TradeActionResult is linked to."""
    rec_id = add_instance(ExpertRecommendation(
        instance_id=1, symbol="CELH", recommended_action=OrderRecommendation.BUY,
        expected_profit_percent=14.0, price_at_date=CELH_FILL, details=None,
        confidence=80.0, risk_level=RiskLevel.MEDIUM,
        time_horizon=TimeHorizon.SHORT_TERM))
    txn_id = add_instance(Transaction(
        symbol="CELH", quantity=3.0, side=OrderDirection.BUY,
        status=TransactionStatus.WAITING, open_price=CELH_FILL,
        take_profit=None, stop_loss=None))
    order_id = add_instance(TradingOrder(
        account_id=account_id, symbol="CELH", quantity=3.0, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED,
        transaction_id=txn_id, open_price=CELH_FILL,
        expert_recommendation_id=rec_id))
    return txn_id, order_id, rec_id


def _run_tp_action(account, order, rec_id):
    action = AdjustTakeProfitAction(
        "CELH", account, OrderRecommendation.BUY, existing_order=order,
        expert_recommendation=get_instance(ExpertRecommendation, rec_id),
        take_profit_price=CELH_TARGET)
    return action.execute()


def test_the_brokers_take_profit_survives_the_metadata_write():
    """The regression: after the action, the row must carry BOTH the broker's TP and the
    TradeConditions target. Before the fix the TP came back NULL."""
    account = _SeparateSessionAccount()
    txn_id, order_id, rec_id = _entry_rows(account.id)

    result = _run_tp_action(account, get_instance(TradingOrder, order_id), rec_id)
    assert result["success"] is True, result["message"]
    assert account.adjust_tp_calls == [(txn_id, CELH_TARGET, "ruleset")]

    saved = get_instance(Transaction, txn_id)
    assert saved.take_profit == pytest.approx(CELH_TARGET), (
        "the post-hook wrote the PRE-broker transaction back and erased the take-profit "
        "the broker had just persisted")
    assert saved.meta_data["TradeConditionsData"]["current_target_price"] == pytest.approx(
        round(CELH_TARGET, 2))


def test_an_existing_stop_loss_is_not_erased_either():
    """``update_instance`` copies EVERY column, so the stale write was never TP-specific:
    any column the adapter changed inside its own session was equally exposed."""
    account = _SeparateSessionAccount()
    txn_id, order_id, rec_id = _entry_rows(account.id)
    with Session(get_engine()) as session:      # a stop attached after the action loaded
        row = session.get(Transaction, txn_id)  # its copy, as the safeguard pass does
        row.stop_loss = 23.8571
        session.add(row)
        session.commit()

    _run_tp_action(account, get_instance(TradingOrder, order_id), rec_id)

    saved = get_instance(Transaction, txn_id)
    assert saved.take_profit == pytest.approx(CELH_TARGET)
    assert saved.stop_loss == pytest.approx(23.8571)


def test_metadata_already_present_is_updated_not_replaced():
    """The hook owns exactly one key. Everything else in meta_data must survive it."""
    account = _SeparateSessionAccount()
    txn_id, order_id, rec_id = _entry_rows(account.id)
    with Session(get_engine()) as session:
        row = session.get(Transaction, txn_id)
        row.meta_data = {"TradeConditionsData": {"current_target_price": 1.0,
                                                 "something_else": "keep me"},
                         "OtherBlock": {"n": 3}}
        session.add(row)
        session.commit()

    _run_tp_action(account, get_instance(TradingOrder, order_id), rec_id)

    saved = get_instance(Transaction, txn_id)
    assert saved.take_profit == pytest.approx(CELH_TARGET)
    conditions = saved.meta_data["TradeConditionsData"]
    assert conditions["current_target_price"] == pytest.approx(round(CELH_TARGET, 2))
    assert conditions["something_else"] == "keep me"
    assert saved.meta_data["OtherBlock"] == {"n": 3}


def test_the_backtest_store_is_unaffected_because_it_holds_one_object():
    """BACKTEST EQUIVALENCE. In the sql-less store ``get_instance`` returns the SAME
    object by identity, so the re-read the fix adds resolves to the object the action
    already had: no extra read, no different value, nothing to change in a backtest."""
    account = _SameObjectAccount()
    with ts.inmem_trades():
        txn_id, order_id, rec_id = _entry_rows(account.id)
        order = get_instance(TradingOrder, order_id)
        transaction = get_instance(Transaction, txn_id)
        assert get_instance(Transaction, txn_id) is transaction, (
            "the store must hand back one object per row; the no-op claim rests on it")

        result = _run_tp_action(account, order, rec_id)
        assert result["success"] is True, result["message"]

        saved = get_instance(Transaction, txn_id)
        assert saved is transaction
        assert saved.take_profit == pytest.approx(CELH_TARGET)
        assert saved.meta_data["TradeConditionsData"]["current_target_price"] == pytest.approx(
            round(CELH_TARGET, 2))
