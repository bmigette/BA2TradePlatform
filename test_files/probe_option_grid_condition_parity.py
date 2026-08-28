"""Dual-storage parity probe for the option GA grid's condition set (2026-08-27).

The grid's rules fire on these conditions; the 2026-07-30 inert-gate incident
(DaysSinceLastClose reading an EMPTY RAM store) proved a condition can be dead in
backtest while alive in live. This probe seeds the SAME rows into SQLite and into
the in-memory backtest store, evaluates every storage-touching grid condition under
BOTH, and fails if any answer differs.

Conditions probed (every field the launcher's option entry/exit rules read):
  has_no_position        (entry correctness guard)
  days_opened            (exit: opt_time)
  has_assigned_shares    (O_WHEEL overlay trigger)
  has_covered_call       (O_WHEEL cc_guard)
  has_option_position    (option-position awareness)
  has_protective_put     (O_PP awareness)

Not probed here (no storage dependency — they read the recommendation object or the
account's position cache): signal, confidence, expected_profit_target_percent,
iv_rank, relative_volume, iv_to_realized_vol, profit_loss_percent, days_to_expiry.

Run:  PYTHONPATH=packages/common .venv/Scripts/python.exe test_files/probe_option_grid_condition_parity.py
"""
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

NOW = datetime(2024, 6, 1, tzinfo=timezone.utc)
EXPERT_ID = 1
SYMBOL = "AAPL"


class _Rec:
    def __init__(self):
        self.instance_id = EXPERT_ID
        self.created_at = NOW
        self.symbol = SYMBOL


def _seed():
    """Seed an identical book into whichever storage is active.

    An expert holding 100 shares that were PUT TO US by an assigned short put
    (origin=csp_assignment), an open covered call over them, an open protective
    put, and a closed trade from 20 days ago.
    """
    from ba2_common.core.db import add_instance
    from ba2_common.core.models import Transaction, TradingOrder
    from ba2_common.core.types import (
        AssetClass, OptionRight, OrderDirection, OrderStatus, OrderType,
        TransactionStatus, TXN_ORIGIN_CSP_ASSIGNMENT,
    )

    # assigned-share lot: an equity BUY transaction whose origin marks it assigned
    txn_id = add_instance(Transaction(
        expert_id=EXPERT_ID, symbol=SYMBOL, status=TransactionStatus.OPENED,
        side=OrderDirection.BUY, quantity=100.0, open_price=180.0,
        open_date=NOW - timedelta(days=5),
        meta_data={"origin": TXN_ORIGIN_CSP_ASSIGNMENT},
    ))
    # a bought-outright lot, same expert/symbol — has_assigned_shares must NOT fire on it
    add_instance(Transaction(
        expert_id=EXPERT_ID, symbol=SYMBOL, status=TransactionStatus.OPENED,
        side=OrderDirection.BUY, quantity=100.0, open_price=185.0,
        open_date=NOW - timedelta(days=3), meta_data={},
    ))
    # a CLOSED trade, 20 days ago
    add_instance(Transaction(
        expert_id=EXPERT_ID, symbol=SYMBOL, status=TransactionStatus.CLOSED,
        side=OrderDirection.BUY, quantity=10.0, open_price=100.0, close_price=110.0,
        open_date=NOW - timedelta(days=27), close_date=NOW - timedelta(days=20),
    ))
    # covered call written over the assigned lot + protective put (open option orders)
    add_instance(TradingOrder(
        account_id=1, symbol="AAPL240621C00200000", quantity=1.0,
        side=OrderDirection.SELL, order_type=OrderType.SELL_LIMIT, status=OrderStatus.FILLED,
        transaction_id=txn_id, asset_class=AssetClass.OPTION, underlying_symbol=SYMBOL,
        option_type=OptionRight.CALL, option_strategy="covered_call",
    ))
    add_instance(TradingOrder(
        account_id=1, symbol="AAPL240621P00150000", quantity=1.0,
        side=OrderDirection.BUY, order_type=OrderType.BUY_LIMIT, status=OrderStatus.FILLED,
        transaction_id=txn_id, asset_class=AssetClass.OPTION, underlying_symbol=SYMBOL,
        option_type=OptionRight.PUT, option_strategy="protective_put",
    ))
    return txn_id


def _eval_all(txn_id):
    """Evaluate every storage-touching grid condition once; return {name: (bool, value)}."""
    from ba2_common.core.TradeConditions import (
        HasAssignedSharesCondition, HasCoveredCallCondition, HasNoPositionCondition,
        HasOptionPositionCondition, HasProtectivePutCondition, DaysOpenedCondition,
    )

    rec = _Rec()
    # an existing ORDER standing in for the managed position (entry fill of the txn)
    from ba2_common.core.models import TradingOrder
    from ba2_common.core.types import OrderDirection, OrderType, OrderStatus
    order = TradingOrder(
        account_id=1, symbol=SYMBOL, quantity=100.0, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED,
        transaction_id=txn_id, created_at=NOW - timedelta(days=5),
    )

    out = {}
    for name, cls in [
        ("has_no_position", HasNoPositionCondition),
        ("has_assigned_shares", HasAssignedSharesCondition),
        ("has_covered_call", HasCoveredCallCondition),
        ("has_option_position", HasOptionPositionCondition),
        ("has_protective_put", HasProtectivePutCondition),
    ]:
        c = cls(account=None, instrument_name=SYMBOL, expert_recommendation=rec,
                existing_order=order)
        out[name] = (bool(c.evaluate()), getattr(c, "calculated_value", None))

    d = DaysOpenedCondition(account=None, instrument_name=SYMBOL, expert_recommendation=rec,
                            operator_str=">", value=4.0, existing_order=order)
    out["days_opened(>4d)"] = (bool(d.evaluate()), d.calculated_value)
    return out


def _seed_discrimination():
    """A book that must make the wheel gates answer FALSE: only BOUGHT shares, no assignment."""
    from ba2_common.core.db import add_instance
    from ba2_common.core.models import Transaction
    from ba2_common.core.types import OrderDirection, TransactionStatus

    txn_id = add_instance(Transaction(
        expert_id=EXPERT_ID, symbol=SYMBOL, status=TransactionStatus.OPENED,
        side=OrderDirection.BUY, quantity=100.0, open_price=185.0,
        open_date=NOW - timedelta(days=3), meta_data={},
    ))
    return txn_id


def main():
    from ba2_common.core import db as common_db

    # ---- SQLite arms (live semantics) — one store per book
    with tempfile.TemporaryDirectory() as tmp:
        common_db.configure_db_threadlocal(f"{tmp}/parity.sqlite")
        common_db.init_db()
        try:
            sql_result = _eval_all(_seed())
        finally:
            common_db.clear_threadlocal_db()
    with tempfile.TemporaryDirectory() as tmp:
        common_db.configure_db_threadlocal(f"{tmp}/parity2.sqlite")
        common_db.init_db()
        try:
            sql_disc = _eval_all(_seed_discrimination())
        finally:
            common_db.clear_threadlocal_db()

    # ---- in-memory arms (every GA trial) — one store per book
    from ba2_common.core import trade_store as ts
    with ts.inmem_trades():
        mem_result = _eval_all(_seed())
    with ts.inmem_trades():
        mem_disc = _eval_all(_seed_discrimination())

    print(f"{'condition':22s} {'sqlite':>22s} {'in-memory':>22s}  match")
    failures = []
    for key in sql_result:
        s, m = sql_result[key], mem_result[key]
        ok = s == m
        if not ok:
            failures.append(key)
        print(f"{key:22s} {str(s):>22s} {str(m):>22s}  {'OK' if ok else 'DIVERGENT'}")

    print("\n--- discrimination arm: bought-only shares, no assignment ---")
    for key in sql_disc:
        s, m = sql_disc[key], mem_disc[key]
        ok = s == m
        if not ok:
            failures.append(f"{key} (disc)")
        print(f"{key:22s} {str(s):>22s} {str(m):>22s}  {'OK' if ok else 'DIVERGENT'}")
    # hard expectations: bought-outright shares must NOT look like assignment
    if sql_disc["has_assigned_shares"][0] is True:
        failures.append("has_assigned_shares fires on BOUGHT shares (not just parity!)")

    if failures:
        print(f"\nFAIL: {len(failures)} condition(s) diverge between backends: {failures}")
        return 1
    print("\nPASS: all grid conditions answer identically under both stores")
    return 0


if __name__ == "__main__":
    sys.exit(main())
