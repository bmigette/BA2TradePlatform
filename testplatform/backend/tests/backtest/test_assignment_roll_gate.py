"""Review 2026-08-30 F8 — the transaction roll must also run for SETTLEMENT writes.

``daily_engine``'s bar tail (``_fills_and_settlements``, extracted from ``run()`` steps
4..4b) rolled transactions only ``if filled`` — the fill engine's change signal. But the
three settlement passes run AFTER that gate and write synthetic FILLED orders of their
own: 4a-pre (assignment liquidations), 4a (expiry settlement, whose short-ITM assignment
books a closing fill on an offsetting EQUITY lot), 4a-bis (margin liquidation). Expiry
and margin liquidation close their OPTION transactions directly — safe — but the
assignment paths that close/open an EQUITY transaction rely on the roll. On a QUIET bar
(no fill anywhere) the equity transaction therefore stayed OPENED indefinitely, and
``_has_open_or_waiting_position`` locked the symbol out of re-entry until any later fill
anywhere. O_CC/O_WHEEL arms exposed.

The fix mirrors the F1 precedent (``_expire_stale_option_limits`` feeds
``refresh_orders``'s change signal): each settlement pass reports whether it changed the
book, and ``refresh_transactions()`` runs after 4a-bis when any did. A fills-only bar
keeps the single existing roll — no double roll.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from ba2_common.core.types import AssetClass, TransactionStatus

from tests.backtest.test_option_assignment_share_ledger import (
    _build,
    _open_equity_txns,
)


def _open_or_waiting(symbol="AAPL"):
    """Every transaction the dup gate counts for ``symbol`` (any asset class)."""
    from ba2_common.core.trade_store import transactions_where

    return transactions_where(
        symbol=symbol,
        statuses=[TransactionStatus.OPENED, TransactionStatus.WAITING])


def _assert_quiet_bar(acct):
    """Precondition: nothing can fill this bar, so the OLD ``if filled`` gate is never
    True and any roll must come from the settlement change signal."""
    from ba2_common.core.types import OrderStatus

    active = set(OrderStatus.get_active_statuses())
    assert not any(getattr(o, "status", None) in active for o in acct._active_orders()), \
        "fixture defect: a working order could fill on the 'quiet' bar"


def test_called_away_on_a_quiet_bar_closes_the_equity_transaction_same_bar(tmp_path):
    """Covered call assigned at expiry on a bar with NO other fill: the share lot's
    equity transaction must reach CLOSED on the SAME bar (the settlement pass feeds the
    roll), so the symbol is re-enterable next bar instead of locked indefinitely."""
    engine, acct, ps, ctx, eq_txn = _build(tmp_path, 47, with_equity_lot=True)
    try:
        expiry_bar = datetime(2024, 3, 15)
        ps.set_clock(expiry_bar)
        _assert_quiet_bar(acct)

        engine._fills_and_settlements(expiry_bar)

        from ba2_common.core.db import get_instance
        from ba2_common.core.models import Transaction

        txn = get_instance(Transaction, eq_txn)
        assert txn.status == TransactionStatus.CLOSED, (
            f"equity transaction {eq_txn} is {txn.status} after the shares were called "
            f"away on a quiet bar — the settlement wrote a synthetic FILLED closing "
            f"order, but the roll was gated on an unrelated fill signal (F8)."
        )
        assert _open_equity_txns(acct) == []
        # The dup gate must be free for AAPL: no OPENED/WAITING transaction of any
        # asset class survives the bar (the option txn is closed directly by settle).
        assert _open_or_waiting("AAPL") == []
    finally:
        ctx.__exit__(None, None, None)


def test_orphan_liquidation_on_a_quiet_bar_closes_the_assigned_stock_txn_same_bar(tmp_path):
    """4a-pre's next-bar orphan sale is ALSO a settlement write: the assigned-stock
    transaction (naked short call -> short shares, bought back at the next bar's open)
    must reach CLOSED on the liquidation bar even when nothing else fills."""
    engine, acct, ps, ctx, _ = _build(tmp_path, 48, with_equity_lot=False)
    try:
        expiry_bar = datetime(2024, 3, 15)
        ps.set_clock(expiry_bar)
        _assert_quiet_bar(acct)
        engine._fills_and_settlements(expiry_bar)   # assignment: short shares + pending sale

        assigned_open = [t for t in _open_or_waiting("AAPL")
                         if t.asset_class != AssetClass.OPTION]
        assert len(assigned_open) == 1, "fixture defect: expected one assigned stock lot"

        liq_bar = datetime(2024, 3, 18)
        ps.set_clock(liq_bar)
        _assert_quiet_bar(acct)
        engine._fills_and_settlements(liq_bar)

        assert _open_or_waiting("AAPL") == [], (
            "the orphan liquidation's synthetic FILLED order did not roll into its "
            "transaction on a quiet bar — the symbol stays locked (F8)."
        )
    finally:
        ctx.__exit__(None, None, None)


def test_a_fills_only_bar_still_rolls_exactly_once(tmp_path):
    """The perf gate stays: when only fills happened, exactly ONE roll runs (no double
    roll); on a bar with neither fills nor settlement activity, none runs."""
    engine, acct, ps, ctx, _ = _build(tmp_path, 49, with_equity_lot=True)
    try:
        calls = []
        real = acct.refresh_transactions

        def counting():
            calls.append(1)
            return real()

        acct.refresh_transactions = counting

        # 2024-03-06: nothing works, nothing expires, nothing pending -> NO roll.
        quiet = datetime(2024, 3, 6)
        ps.set_clock(quiet)
        _assert_quiet_bar(acct)
        engine._fills_and_settlements(quiet)
        assert len(calls) == 0, "a no-event bar must not roll at all (perf gate)"

        # 2024-03-15: the expiry settles (assignment) -> exactly ONE roll.
        expiry_bar = datetime(2024, 3, 15)
        ps.set_clock(expiry_bar)
        engine._fills_and_settlements(expiry_bar)
        assert len(calls) == 1, "a settlement-only bar must roll exactly once"
    finally:
        ctx.__exit__(None, None, None)
