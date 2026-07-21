"""``ReadOnlyAccountInterface.has_pending_closing_order`` — shared by backtest
(``DailyBacktestEngine._held_transactions``) and live
(``TradeManager.process_open_positions_recommendations``) — must report True while a
transaction's closing order is still WORKING (submitted, not yet filled/canceled), so exit-rule
re-evaluation is skipped until it resolves.

Without this guard, a position stays visible as "still open, needs managing" for every cycle a
submitted close takes to fill. A multi-leg option close
(``CloseOptionAction._close_multi_leg``) is submitted ``order_type="limit"`` and fills off the
NEXT bar's premium at the earliest, so if the exit condition is still true before that fill
lands, the evaluator submits ANOTHER closing order for the same position — every bar, until the
first one resolves. Each duplicate credits cash for contracts that may already be gone
(``_apply_option_fill`` never checks current holdings), compounding equity without bound. This
was the actual driver behind the 2026-07-21 options-grid fitness anomaly (fitness=43988.47,
trades=579 from a corrupted trillion-scale account balance) — a separate, larger bug than the
multi-leg parent/leg fill double-counting fixed in
``ReadOnlyAccountInterface.refresh_transactions`` the same day (that fix alone did not change
this reproduction's result at all, which is what led to finding this one).

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_pending_close_guard.py -v
"""
from __future__ import annotations

from datetime import datetime

from ba2_common.core.db import update_instance
from ba2_common.core.types import OrderDirection, OrderStatus

from tests.backtest.test_daily_engine_unit import _build_run
from tests.backtest.test_round_trip_trades import _attach_order, _fill, _open_entry

D2 = datetime(2024, 1, 3)


def _open_and_attribute(account, expert_id, symbol="AAPL", qty=10):
    """Open+fill an entry and stamp the transaction's expert_id (``_open_entry`` doesn't set
    one - it's not needed for the round-trip-trades tests it was written for)."""
    from ba2_common.core.trade_store import transactions_where

    buy_bid, txn_id = _open_entry(account, symbol=symbol, qty=qty, side=OrderDirection.BUY)
    _fill(account, buy_bid, 100.0, D2)
    account.refresh_transactions()
    txn = next(t for t in transactions_where(symbol=symbol) if t.id == txn_id)
    txn.expert_id = expert_id
    update_instance(txn)
    return txn_id


def test_held_transactions_excludes_position_with_pending_close():
    engine, account, expert, ctx, ps = _build_run()
    try:
        ps.set_clock(D2)
        txn_id = _open_and_attribute(account, expert.id)

        assert "AAPL" in engine._held_transactions(expert.id), \
            "fixture must have a real open AAPL position before adding a pending close"
        assert account.has_pending_closing_order(txn_id) is False

        # Simulate an earlier bar's close already submitted (order_type=MARKET default here is
        # fine - what matters is it stays non-terminal, i.e. not yet filled/canceled).
        _attach_order(account, txn_id, OrderDirection.SELL, qty=10)

        assert account.has_pending_closing_order(txn_id) is True
        assert "AAPL" not in engine._held_transactions(expert.id), \
            "a transaction with an already-WORKING closing order must not be re-offered for exit evaluation"
    finally:
        ctx.__exit__(None, None, None)


def test_has_pending_closing_order_false_for_terminal_close():
    """A closing order that already reached a terminal status (e.g. CANCELED - the close was
    rejected/expired) must NOT block future exit evaluation; only a still-WORKING close does."""
    engine, account, expert, ctx, ps = _build_run()
    try:
        ps.set_clock(D2)
        txn_id = _open_and_attribute(account, expert.id)

        close_bid = _attach_order(account, txn_id, OrderDirection.SELL, qty=10)
        close_order = account.get_order(close_bid)
        close_order.status = OrderStatus.CANCELED
        update_instance(close_order)

        assert account.has_pending_closing_order(txn_id) is False
        assert "AAPL" in engine._held_transactions(expert.id)
    finally:
        ctx.__exit__(None, None, None)


def test_has_pending_closing_order_false_with_no_close_submitted():
    """A single-order transaction (just the entry, no close ever attempted) is never
    considered pending - the len(orders) <= 1 short-circuit."""
    engine, account, expert, ctx, ps = _build_run()
    try:
        ps.set_clock(D2)
        txn_id = _open_and_attribute(account, expert.id)

        assert account.has_pending_closing_order(txn_id) is False
    finally:
        ctx.__exit__(None, None, None)
