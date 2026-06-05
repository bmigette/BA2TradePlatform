"""Unit tests for option assignment / exercise / expiry reconciliation.

These tests use CANNED Alpaca activity payloads (dicts) and never touch the
network. An AlpacaAccount instance is built via __new__ (bypassing __init__/DB
client init) with a stubbed settings cache so DB writes work while no broker
connection is required. The originating option orders/transactions are seeded
into the in-memory test DB, then reconcile_option_assignments() is called with
canned activity dicts and resulting Transaction / OptionActivity state asserted.

Reconciliation semantics under test (documented choices):
- OPASN on a SHORT PUT (option order side==SELL, right==PUT): cash-secured put
  assigned -> open an equity LONG Transaction (qty = 100 * contracts,
  open_price = strike) attributed to the option's expert; close the short-put
  option Transaction with close_reason="assigned".
- OPASN on a SHORT CALL (option order side==SELL, right==CALL): shares called
  away -> close the expert's OPENED equity long Transaction for the underlying
  with close_reason="called_away", close_price=strike; close the short-call
  option Transaction with close_reason="assigned".
- OPEXP (expiry): close the option Transaction, close_reason="expired",
  close_price=0.0.
- IDEMPOTENCY: an OptionActivity row keyed on (account_id, activity_id) prevents
  re-applying effects when reconcile is called twice with the same activities.
"""
from datetime import date, datetime, timezone

import pytest

from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
from ba2_trade_platform.core.db import get_db, add_instance, get_instance
from ba2_trade_platform.core.models import (
    TradingOrder, Transaction, OptionActivity,
)
from ba2_trade_platform.core.types import (
    AssetClass, OptionRight, OrderDirection, OrderType, OrderStatus,
    TransactionStatus,
)
from sqlmodel import select


# OCC symbols used across tests:
#   AAPL @ 150 PUT  expiring 2026-01-16 -> AAPL260116P00150000
#   AAPL @ 160 CALL expiring 2026-01-16 -> AAPL260116C00160000
PUT_OCC = "AAPL260116P00150000"
CALL_OCC = "AAPL260116C00160000"


def _make_alpaca(account_id):
    """Build an AlpacaAccount that can run DB-backed reconcile with no network."""
    acct = AlpacaAccount.__new__(AlpacaAccount)
    acct.id = account_id
    acct._settings_cache = {
        "api_key": "k", "api_secret": "s", "paper_account": True,
        "data_feed": "iex",
    }
    return acct


def _seed_option_order_and_txn(account_id, expert_id, occ, right, side,
                               strike, underlying="AAPL", contracts=1):
    """Create an option Transaction + its originating filled TradingOrder."""
    opt_txn = Transaction(
        symbol=occ, quantity=contracts, side=side,
        status=TransactionStatus.OPENED, open_price=2.5,
        open_date=datetime.now(timezone.utc), expert_id=expert_id,
    )
    opt_txn_id = add_instance(opt_txn)

    order = TradingOrder(
        account_id=account_id, symbol=occ, quantity=contracts, side=side,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED,
        filled_qty=contracts, transaction_id=opt_txn_id,
        asset_class=AssetClass.OPTION, contract_symbol=occ, option_type=right,
        strike=strike, expiry=date(2026, 1, 16), underlying_symbol=underlying,
    )
    add_instance(order)
    return opt_txn_id


def _equity_longs_for_expert(expert_id):
    """Equity LONG (BUY) AAPL transactions attributed to a specific expert.

    The conftest test engine is session-scoped with no per-test cleanup, so
    committed rows from earlier tests persist. Scoping by expert_id (unique per
    test via mock_expert_instance) isolates assertions deterministically.
    """
    with get_db() as session:
        return session.exec(
            select(Transaction)
            .where(Transaction.symbol == "AAPL")
            .where(Transaction.side == OrderDirection.BUY)
            .where(Transaction.expert_id == expert_id)
        ).all()


def _option_activities_for(account_id):
    with get_db() as session:
        return session.exec(
            select(OptionActivity).where(OptionActivity.account_id == account_id)
        ).all()


# ---------------------------------------------------------------------------
# Short PUT assigned (OPASN) -> equity long opened
# ---------------------------------------------------------------------------
def test_short_put_assignment_opens_equity_long(mock_account_def, mock_expert_instance):
    acct = _make_alpaca(mock_account_def.id)
    expert_id = mock_expert_instance.id
    opt_txn_id = _seed_option_order_and_txn(
        mock_account_def.id, expert_id, PUT_OCC, OptionRight.PUT,
        OrderDirection.SELL, strike=150.0, contracts=2,
    )

    activities = [{
        "id": "act-put-assign-1", "activity_type": "OPASN", "symbol": PUT_OCC,
        "qty": "2", "price": "0", "net_amount": "-30000",
    }]
    results = acct.reconcile_option_assignments(activities)
    assert len(results) == 1

    # The short-put option transaction is now closed/assigned.
    opt_txn = get_instance(Transaction, opt_txn_id)
    assert opt_txn.status == TransactionStatus.CLOSED
    assert opt_txn.close_reason == "assigned"

    # A new equity LONG transaction exists for AAPL, 200 shares @ 150, expert-attributed.
    equity = _equity_longs_for_expert(expert_id)
    assert len(equity) == 1
    eq = equity[0]
    assert eq.quantity == 200.0           # 100 * 2 contracts
    assert eq.open_price == 150.0
    assert eq.status == TransactionStatus.OPENED
    assert eq.expert_id == expert_id
    assert eq.meta_data and eq.meta_data.get("origin") == "csp_assignment"

    # OptionActivity audit row recorded.
    rows = _option_activities_for(mock_account_def.id)
    assert len(rows) == 1
    assert rows[0].activity_id == "act-put-assign-1"
    assert "csp_assignment" in (rows[0].result or "") or "long" in (rows[0].result or "")


# ---------------------------------------------------------------------------
# Short CALL assigned (OPASN) -> held equity long closed (called_away)
# ---------------------------------------------------------------------------
def test_short_call_assignment_closes_equity_long(mock_account_def, mock_expert_instance):
    acct = _make_alpaca(mock_account_def.id)
    expert_id = mock_expert_instance.id

    # Seed the held equity long that the call is written against.
    held = Transaction(
        symbol="AAPL", quantity=100.0, side=OrderDirection.BUY,
        status=TransactionStatus.OPENED, open_price=140.0,
        open_date=datetime.now(timezone.utc), expert_id=expert_id,
    )
    held_id = add_instance(held)

    opt_txn_id = _seed_option_order_and_txn(
        mock_account_def.id, expert_id, CALL_OCC, OptionRight.CALL,
        OrderDirection.SELL, strike=160.0, contracts=1,
    )

    activities = [{
        "id": "act-call-assign-1", "activity_type": "OPASN", "symbol": CALL_OCC,
        "qty": "1", "price": "0", "net_amount": "16000",
    }]
    acct.reconcile_option_assignments(activities)

    held_after = get_instance(Transaction, held_id)
    assert held_after.status == TransactionStatus.CLOSED
    assert held_after.close_reason == "called_away"
    assert held_after.close_price == 160.0

    opt_after = get_instance(Transaction, opt_txn_id)
    assert opt_after.status == TransactionStatus.CLOSED
    assert opt_after.close_reason == "assigned"


def test_short_call_assignment_no_long_records_result(mock_account_def, mock_expert_instance):
    acct = _make_alpaca(mock_account_def.id)
    expert_id = mock_expert_instance.id
    opt_txn_id = _seed_option_order_and_txn(
        mock_account_def.id, expert_id, CALL_OCC, OptionRight.CALL,
        OrderDirection.SELL, strike=160.0, contracts=1,
    )

    activities = [{
        "id": "act-call-nolong", "activity_type": "OPASN", "symbol": CALL_OCC,
        "qty": "1", "price": "0", "net_amount": "16000",
    }]
    acct.reconcile_option_assignments(activities)

    # Short call still gets closed (assigned).
    opt_after = get_instance(Transaction, opt_txn_id)
    assert opt_after.status == TransactionStatus.CLOSED

    rows = _option_activities_for(mock_account_def.id)
    assert len(rows) == 1
    assert "called_away_no_long" in (rows[0].result or "")


# ---------------------------------------------------------------------------
# Expiry (OPEXP) -> option transaction closed (expired)
# ---------------------------------------------------------------------------
def test_expiry_closes_option_transaction(mock_account_def, mock_expert_instance):
    acct = _make_alpaca(mock_account_def.id)
    expert_id = mock_expert_instance.id
    opt_txn_id = _seed_option_order_and_txn(
        mock_account_def.id, expert_id, PUT_OCC, OptionRight.PUT,
        OrderDirection.BUY, strike=150.0, contracts=1,
    )

    activities = [{
        "id": "act-expiry-1", "activity_type": "OPEXP", "symbol": PUT_OCC,
        "qty": "1", "price": "0", "net_amount": "0",
    }]
    acct.reconcile_option_assignments(activities)

    opt_after = get_instance(Transaction, opt_txn_id)
    assert opt_after.status == TransactionStatus.CLOSED
    assert opt_after.close_reason == "expired"
    assert opt_after.close_price == 0.0


# ---------------------------------------------------------------------------
# Exercise (OPEXC) -> option transaction closed (exercised)
# ---------------------------------------------------------------------------
def test_exercise_closes_option_transaction(mock_account_def, mock_expert_instance):
    acct = _make_alpaca(mock_account_def.id)
    expert_id = mock_expert_instance.id
    opt_txn_id = _seed_option_order_and_txn(
        mock_account_def.id, expert_id, CALL_OCC, OptionRight.CALL,
        OrderDirection.BUY, strike=160.0, contracts=1,
    )

    activities = [{
        "id": "act-exercise-1", "activity_type": "OPEXC", "symbol": CALL_OCC,
        "qty": "1", "price": "0", "net_amount": "0",
    }]
    acct.reconcile_option_assignments(activities)

    opt_after = get_instance(Transaction, opt_txn_id)
    assert opt_after.status == TransactionStatus.CLOSED
    assert opt_after.close_reason == "exercised"


# ---------------------------------------------------------------------------
# IDEMPOTENCY -> reconcile twice applies effects once
# ---------------------------------------------------------------------------
def test_idempotency_double_reconcile_no_double_open(mock_account_def, mock_expert_instance):
    acct = _make_alpaca(mock_account_def.id)
    expert_id = mock_expert_instance.id
    _seed_option_order_and_txn(
        mock_account_def.id, expert_id, PUT_OCC, OptionRight.PUT,
        OrderDirection.SELL, strike=150.0, contracts=1,
    )

    activities = [{
        "id": "act-idem-1", "activity_type": "OPASN", "symbol": PUT_OCC,
        "qty": "1", "price": "0", "net_amount": "-15000",
    }]
    acct.reconcile_option_assignments(activities)
    acct.reconcile_option_assignments(activities)  # second call must be a no-op

    equity = _equity_longs_for_expert(expert_id)
    assert len(equity) == 1  # only ONE equity long opened despite two calls

    rows = [r for r in _option_activities_for(mock_account_def.id)
            if r.activity_id == "act-idem-1"]
    assert len(rows) == 1  # exactly one audit row per activity_id


# ---------------------------------------------------------------------------
# Malformed / unknown activity -> no crash, recorded as unhandled
# ---------------------------------------------------------------------------
def test_malformed_symbol_does_not_crash(mock_account_def):
    acct = _make_alpaca(mock_account_def.id)
    activities = [{
        "id": "act-bad-1", "activity_type": "OPASN", "symbol": "NOT_AN_OCC",
        "qty": "1", "price": "0",
    }]
    results = acct.reconcile_option_assignments(activities)  # must not raise
    assert isinstance(results, list)

    rows = [r for r in _option_activities_for(mock_account_def.id)
            if r.activity_id == "act-bad-1"]
    assert len(rows) == 1
    assert "unhandled" in (rows[0].result or "").lower()


def test_unknown_activity_type_recorded_unhandled(mock_account_def):
    acct = _make_alpaca(mock_account_def.id)
    activities = [{
        "id": "act-unknown-1", "activity_type": "OPCSH", "symbol": PUT_OCC,
        "qty": "1", "price": "0",
    }]
    acct.reconcile_option_assignments(activities)
    rows = [r for r in _option_activities_for(mock_account_def.id)
            if r.activity_id == "act-unknown-1"]
    assert len(rows) == 1
    # OPCSH has no specific handler -> recorded but unhandled/no-op.
    assert rows[0].activity_type == "OPCSH"


def test_missing_activity_id_skipped_gracefully(mock_account_def):
    acct = _make_alpaca(mock_account_def.id)
    activities = [{"activity_type": "OPEXP", "symbol": PUT_OCC, "qty": "1"}]
    results = acct.reconcile_option_assignments(activities)  # must not raise
    assert isinstance(results, list)
