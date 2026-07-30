"""Trade gates must behave identically wherever the rows are stored.

Split deliberately in two:

1. **Condition tests** — drive the conditions against a ``FakeTradeRepository`` holding plain
   lists. They mention no backend at all, because a condition has no business knowing whether
   its trades came from RAM or SQLite: it asks the repository and reasons about what it gets.
   This is the contract that was broken.

2. **Backend parity tests** — the only place storage is named. They seed the SAME data into the
   in-memory store and into SQLite and assert both ``TradeRepository`` implementations return
   the same answers, which is what makes (1) a valid proof for both execution modes.

WHY: ``DaysSinceLastCloseCondition`` queried storage directly with ``select(Transaction)``.
``Transaction`` is an ``IN_MEM_MODEL``, so under a RAM-only backtest that query returned EMPTY
rather than raising — with a close 3 days old and a ">15 day" cooldown the condition read the
1e9 "never closed" sentinel and allowed the entry. The gate was inert for a WHOLE optimization,
so the GA tuned a gene that did nothing: the same genome scored 103 trades / 17.55% annualised
in-memory and 169 / 0.20% on SQLite, and live (SQLite) behaved like the latter.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

import pytest

from ba2_common.core.models import Transaction, TradingOrder
from ba2_common.core.trade_repository import (
    InMemoryTradeRepository, SqlTradeRepository, TradeRepository,
    reset_trade_repository, set_trade_repository,
)
from ba2_common.core.types import (
    AssetClass, OptionRight, OrderDirection, OrderStatus, OrderType, TransactionStatus,
)

NOW = datetime(2024, 6, 1, tzinfo=timezone.utc)
EXPERT_ID = 1
SYMBOL = "AAPL"


class FakeTradeRepository(TradeRepository):
    """A repository over plain lists — no storage, no backend, no setup."""

    def __init__(self, transactions=None, orders=None):
        self.transactions: List[Transaction] = list(transactions or [])
        self.orders: List[TradingOrder] = list(orders or [])

    def transaction(self, transaction_id: Any) -> Optional[Transaction]:
        return next((t for t in self.transactions if t.id == transaction_id), None)

    def open_transactions(self, *, expert_id, symbol=None, side=None):
        return [t for t in self.transactions
                if t.expert_id == expert_id
                and t.status == TransactionStatus.OPENED
                and (symbol is None or t.symbol == symbol)
                and (side is None or t.side == side)]

    def closed_transactions(self, *, expert_id, symbol=None):
        return self._newest_close_first([
            t for t in self.transactions
            if t.expert_id == expert_id
            and t.status == TransactionStatus.CLOSED
            and (symbol is None or t.symbol == symbol)])

    def _orders_for_transactions(self, transaction_ids):
        wanted = set(transaction_ids)
        return [o for o in self.orders if o.transaction_id in wanted]


class _Rec:
    """Minimal stand-in for the ExpertRecommendation the conditions read."""

    def __init__(self, expert_id=EXPERT_ID, created_at=NOW):
        self.instance_id = expert_id
        self.created_at = created_at
        self.symbol = SYMBOL


def _closed_txn(days_ago, expert_id=EXPERT_ID, symbol=SYMBOL, txn_id=None):
    return Transaction(
        id=txn_id, expert_id=expert_id, symbol=symbol, status=TransactionStatus.CLOSED,
        side=OrderDirection.BUY, quantity=1.0, open_price=100.0, close_price=110.0,
        open_date=NOW - timedelta(days=days_ago + 7), close_date=NOW - timedelta(days=days_ago),
    )


def _open_txn(side=OrderDirection.BUY, expert_id=EXPERT_ID, symbol=SYMBOL, txn_id=None):
    return Transaction(
        id=txn_id, expert_id=expert_id, symbol=symbol, status=TransactionStatus.OPENED,
        side=side, quantity=1.0, open_price=100.0, open_date=NOW - timedelta(days=2),
    )


def _option_order(txn_id, status=OrderStatus.FILLED, option_type=OptionRight.CALL,
                  side=OrderDirection.BUY, strategy=None, underlying=SYMBOL):
    return TradingOrder(
        account_id=1, symbol="AAPL240719C00200000", quantity=1.0, side=side,
        order_type=OrderType.MARKET, status=status, transaction_id=txn_id,
        asset_class=AssetClass.OPTION, underlying_symbol=underlying,
        option_type=option_type, option_strategy=strategy,
    )


@pytest.fixture
def repo():
    """Inject a fake repository for the duration of one test, then restore auto-resolution."""
    fake = FakeTradeRepository()
    set_trade_repository(fake)
    yield fake
    reset_trade_repository()


# ---------------------------------------------------------------- conditions
def _days_since_close(value=15.0):
    from ba2_common.core.TradeConditions import DaysSinceLastCloseCondition
    return DaysSinceLastCloseCondition(
        account=None, instrument_name=SYMBOL, expert_recommendation=_Rec(),
        operator_str=">", value=value, existing_order=None,
    )


def test_cooldown_blocks_entry_while_the_last_close_is_recent(repo):
    """THE regression: a 3-day-old close must block a ">15 day" cooldown.

    Before the fix the condition never saw the close and allowed the entry, making the gate —
    and the GA gene tuning it — completely inert.
    """
    repo.transactions.append(_closed_txn(days_ago=3))
    cond = _days_since_close()
    allowed = cond.evaluate()

    assert cond.calculated_value == 3.0
    assert allowed is False


def test_cooldown_allows_entry_once_elapsed(repo):
    """Proves the fix didn't just make the gate block unconditionally."""
    repo.transactions.append(_closed_txn(days_ago=40))
    cond = _days_since_close()
    assert cond.evaluate() is True
    assert cond.calculated_value == 40.0


def test_cooldown_uses_the_most_recent_close(repo):
    """Ordering is applied by the repository, so newest-close-first must hold."""
    repo.transactions += [_closed_txn(days_ago=40), _closed_txn(days_ago=2),
                          _closed_txn(days_ago=25)]
    cond = _days_since_close()
    cond.evaluate()
    assert cond.calculated_value == 2.0


def test_cooldown_ignores_other_experts_and_symbols(repo):
    repo.transactions += [_closed_txn(days_ago=3, expert_id=999),
                          _closed_txn(days_ago=3, symbol="MSFT")]
    cond = _days_since_close()
    assert cond.evaluate() is True
    assert cond.calculated_value == 1e9, "another expert's/symbol's close must not count"


def test_buy_and_sell_position_conditions_discriminate_side(repo):
    from ba2_common.core.TradeConditions import (
        HasBuyPositionCondition, HasSellPositionCondition,
    )
    repo.transactions.append(_open_txn(side=OrderDirection.BUY))
    buy = HasBuyPositionCondition(account=None, instrument_name=SYMBOL,
                                  expert_recommendation=_Rec(), existing_order=None)
    sell = HasSellPositionCondition(account=None, instrument_name=SYMBOL,
                                    expert_recommendation=_Rec(), existing_order=None)
    assert buy.evaluate() is True
    assert sell.evaluate() is False


def test_option_position_condition_resolves_the_transaction_to_order_link(repo):
    """The old SQL join had no in-memory equivalent; the repository expresses it as
    open-transactions -> their orders, so it must resolve without a join."""
    from ba2_common.core.TradeConditions import HasOptionPositionCondition
    repo.transactions.append(_open_txn(txn_id=7))
    repo.orders.append(_option_order(txn_id=7))
    cond = HasOptionPositionCondition(account=None, instrument_name=SYMBOL,
                                      expert_recommendation=_Rec(), existing_order=None)
    assert cond.evaluate() is True


def test_option_position_condition_ignores_terminal_orders(repo):
    from ba2_common.core.TradeConditions import HasOptionPositionCondition
    repo.transactions.append(_open_txn(txn_id=7))
    repo.orders.append(_option_order(txn_id=7, status=OrderStatus.CANCELED))
    cond = HasOptionPositionCondition(account=None, instrument_name=SYMBOL,
                                      expert_recommendation=_Rec(), existing_order=None)
    assert cond.evaluate() is False


def test_covered_call_condition_requires_the_matching_strategy(repo):
    from ba2_common.core.TradeConditions import HasCoveredCallCondition
    repo.transactions.append(_open_txn(txn_id=7))
    repo.orders.append(_option_order(txn_id=7, option_type=OptionRight.CALL,
                                     side=OrderDirection.SELL, strategy="protective_put"))
    cond = HasCoveredCallCondition(account=None, instrument_name=SYMBOL,
                                   expert_recommendation=_Rec(), existing_order=None)
    assert cond.evaluate() is False, "a non-covered-call option leg must not satisfy it"

    repo.orders.append(_option_order(txn_id=7, option_type=OptionRight.CALL,
                                     side=OrderDirection.SELL, strategy="covered_call"))
    cond2 = HasCoveredCallCondition(account=None, instrument_name=SYMBOL,
                                    expert_recommendation=_Rec(), existing_order=None)
    assert cond2.evaluate() is True


# ----------------------------------------------------- backend parity (only
# place in this file that names a storage backend)
def _days_ago(dt) -> int:
    """Age in days, tolerating SQLite's naive round-trip.

    Worth knowing: the in-memory store hands back the tz-AWARE datetime it was given, while
    SQLite returns it NAIVE. DaysSinceLastCloseCondition already normalises this
    (``close_date.replace(tzinfo=utc)`` when naive); the test must do the same or it would
    fail on a backend difference that the production code handles.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (NOW - dt).days


@contextmanager
def _sqlite_backend(tmp_path):
    """A private, schema'd SQLite for the SQL arm.

    Deliberately self-contained: the suite has no globally-migrated database, and other tests
    leave their own thread-local DB overrides behind, so relying on ambient state made these
    tests pass alone and fail in a full run ("no such table: transaction").
    """
    from ba2_common.core import db as common_db
    common_db.configure_db_threadlocal(str(tmp_path / "parity.sqlite"))
    common_db.init_db()
    try:
        yield
    finally:
        common_db.clear_threadlocal_db()


@contextmanager
def _inmem_backend():
    """The backtest in-memory store."""
    from ba2_common.core import trade_store as ts
    with ts.inmem_trades():
        yield


def test_both_backends_return_the_same_closed_transactions(tmp_path):
    """The two implementations must agree — otherwise the fake-repository condition tests
    above only prove one execution mode."""
    from ba2_common.core.db import add_instance

    expert_id = 8101
    ages = (40, 2, 25)

    with _sqlite_backend(tmp_path):
        for days in ages:
            add_instance(_closed_txn(days_ago=days, expert_id=expert_id))
        sql_days = [_days_ago(t.close_date)
                    for t in SqlTradeRepository().closed_transactions(expert_id=expert_id,
                                                                      symbol=SYMBOL)]

    with _inmem_backend():
        for days in ages:
            add_instance(_closed_txn(days_ago=days, expert_id=expert_id))
        mem_days = [_days_ago(t.close_date)
                    for t in InMemoryTradeRepository().closed_transactions(
                        expert_id=expert_id, symbol=SYMBOL)]

    assert sql_days == [2, 25, 40], "newest close must come first"
    assert mem_days == sql_days, "both backends must agree, ordering included"


def test_both_backends_resolve_open_option_orders_identically(tmp_path):
    """The transaction -> order link must resolve on both backends (the old SQL join had no
    in-memory equivalent)."""
    from ba2_common.core.db import add_instance

    expert_id = 8201

    with _sqlite_backend(tmp_path):
        txn_id = add_instance(_open_txn(expert_id=expert_id))
        add_instance(_option_order(txn_id=txn_id))
        sql_hits = SqlTradeRepository().open_option_orders(expert_id=expert_id,
                                                           underlying=SYMBOL)

    with _inmem_backend():
        txn_id = add_instance(_open_txn(expert_id=expert_id))
        add_instance(_option_order(txn_id=txn_id))
        mem_hits = InMemoryTradeRepository().open_option_orders(expert_id=expert_id,
                                                                underlying=SYMBOL)

    assert len(sql_hits) == 1
    assert len(mem_hits) == len(sql_hits), "both backends must resolve the txn->order link"
