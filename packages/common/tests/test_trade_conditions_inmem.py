"""TradeConditions must see trade rows under the BACKTEST in-memory store, not just SQLite.

Same bug class as test_trade_risk_management_inmem.py (M2): a raw ``select(Transaction)`` /
``select(TradingOrder)`` finds NOTHING when ``trade_store``'s in-mem store is active — it
returns empty rather than raising, so the condition silently evaluates to "nothing found" in
every backtest/GA trial while working correctly live.

MEASURED CONSEQUENCE (2026-07-30), which is why these tests exist:
``DaysSinceLastCloseCondition`` was inert for a whole optimization — with a close 3 days old
and a ">15 day" cooldown it returned the 1e9 "never closed" sentinel — so the GA spent its
search tuning a cooldown gene that did nothing. The same genome scored 103 trades / 17.55%
annualised on the in-memory path and 169 trades / 0.20% on the file-backed one, and LIVE (no
in-mem store) behaves like the latter. All of these conditions now go through
``trade_repository``, which routes to whichever backend is active.
"""
from datetime import datetime, timedelta, timezone

from ba2_common.core import trade_store as ts
from ba2_common.core.db import add_instance
from ba2_common.core.models import TradingOrder, Transaction
from ba2_common.core.types import (
    AssetClass, OptionRight, OrderDirection, OrderStatus, OrderType, TransactionStatus,
)

NOW = datetime(2024, 6, 1, tzinfo=timezone.utc)
EXPERT_ID = 1
SYMBOL = "AAPL"


class _Rec:
    """Minimal stand-in for the ExpertRecommendation the conditions read."""

    def __init__(self, expert_id=EXPERT_ID, created_at=NOW):
        self.instance_id = expert_id
        self.created_at = created_at
        self.symbol = SYMBOL


def _closed_txn(days_ago: int, expert_id=EXPERT_ID, symbol=SYMBOL):
    return Transaction(
        expert_id=expert_id, symbol=symbol, status=TransactionStatus.CLOSED,
        side=OrderDirection.BUY, quantity=1.0, open_price=100.0, close_price=110.0,
        open_date=NOW - timedelta(days=days_ago + 7), close_date=NOW - timedelta(days=days_ago),
    )


def _open_txn(side=OrderDirection.BUY, expert_id=EXPERT_ID, symbol=SYMBOL):
    return Transaction(
        expert_id=expert_id, symbol=symbol, status=TransactionStatus.OPENED,
        side=side, quantity=1.0, open_price=100.0, open_date=NOW - timedelta(days=2),
    )


def test_days_since_last_close_sees_inmem_transaction():
    """THE regression: a 3-day-old close must be FOUND, so a ">15 day" cooldown BLOCKS entry.

    Before the fix this returned 1e9 ("never closed") and allowed the entry, making the gate
    — and the GA gene that tunes it — completely inert in backtests.
    """
    from ba2_common.core.TradeConditions import DaysSinceLastCloseCondition

    with ts.inmem_trades():
        add_instance(_closed_txn(days_ago=3))
        cond = DaysSinceLastCloseCondition(
            account=None, instrument_name=SYMBOL, expert_recommendation=_Rec(),
            operator_str=">", value=15.0, existing_order=None,
        )
        allowed = cond.evaluate()

    assert cond.calculated_value == 3.0, "the 3-day-old close must be found under the in-mem store"
    assert allowed is False, "a >15 day cooldown must block an entry 3 days after the close"


def test_days_since_last_close_allows_entry_once_cooldown_elapsed():
    """The gate must still PASS when the last close is genuinely older than the cooldown —
    proving the fix didn't just make the condition block unconditionally."""
    from ba2_common.core.TradeConditions import DaysSinceLastCloseCondition

    with ts.inmem_trades():
        add_instance(_closed_txn(days_ago=40))
        cond = DaysSinceLastCloseCondition(
            account=None, instrument_name=SYMBOL, expert_recommendation=_Rec(),
            operator_str=">", value=15.0, existing_order=None,
        )
        allowed = cond.evaluate()

    assert cond.calculated_value == 40.0
    assert allowed is True


def test_days_since_last_close_picks_the_most_recent_close():
    """Ordering is done in Python by the repository (not SQL), so it must still be newest-first."""
    from ba2_common.core.TradeConditions import DaysSinceLastCloseCondition

    with ts.inmem_trades():
        add_instance(_closed_txn(days_ago=40))
        add_instance(_closed_txn(days_ago=2))
        add_instance(_closed_txn(days_ago=25))
        cond = DaysSinceLastCloseCondition(
            account=None, instrument_name=SYMBOL, expert_recommendation=_Rec(),
            operator_str=">", value=15.0, existing_order=None,
        )
        cond.evaluate()

    assert cond.calculated_value == 2.0, "must use the MOST RECENT close, not an arbitrary one"


def test_days_since_last_close_ignores_other_experts_and_symbols():
    with ts.inmem_trades():
        from ba2_common.core.TradeConditions import DaysSinceLastCloseCondition
        add_instance(_closed_txn(days_ago=3, expert_id=999))       # another expert
        add_instance(_closed_txn(days_ago=3, symbol="MSFT"))       # another symbol
        cond = DaysSinceLastCloseCondition(
            account=None, instrument_name=SYMBOL, expert_recommendation=_Rec(),
            operator_str=">", value=15.0, existing_order=None,
        )
        allowed = cond.evaluate()

    assert cond.calculated_value == 1e9, "another expert's/symbol's close must not count"
    assert allowed is True


def test_has_buy_and_sell_position_conditions_see_inmem_transactions():
    from ba2_common.core.TradeConditions import (
        HasBuyPositionCondition, HasSellPositionCondition,
    )

    with ts.inmem_trades():
        add_instance(_open_txn(side=OrderDirection.BUY))
        buy = HasBuyPositionCondition(
            account=None, instrument_name=SYMBOL, expert_recommendation=_Rec(),
            existing_order=None)
        sell = HasSellPositionCondition(
            account=None, instrument_name=SYMBOL, expert_recommendation=_Rec(),
            existing_order=None)
        has_buy, has_sell = buy.evaluate(), sell.evaluate()

    assert has_buy is True, "the open BUY transaction must be visible under the in-mem store"
    assert has_sell is False, "side must still be discriminated (no SELL position exists)"


def test_has_option_position_condition_sees_inmem_order_join():
    """The old select().join() had no in-mem equivalent; the repository expresses the join as
    open-transactions -> their orders, so this must now resolve under the store."""
    from ba2_common.core.TradeConditions import HasOptionPositionCondition

    with ts.inmem_trades():
        txn_id = add_instance(_open_txn())
        add_instance(TradingOrder(
            account_id=1, symbol="AAPL240719C00200000", quantity=1.0,
            side=OrderDirection.BUY, order_type=OrderType.MARKET, status=OrderStatus.FILLED,
            transaction_id=txn_id, asset_class=AssetClass.OPTION,
            underlying_symbol=SYMBOL, option_type=OptionRight.CALL,
        ))
        cond = HasOptionPositionCondition(
            account=None, instrument_name=SYMBOL, expert_recommendation=_Rec(),
            existing_order=None)
        has = cond.evaluate()

    assert has is True, "the option order linked to an OPEN transaction must be found"


def test_has_option_position_ignores_terminal_orders():
    from ba2_common.core.TradeConditions import HasOptionPositionCondition

    with ts.inmem_trades():
        txn_id = add_instance(_open_txn())
        add_instance(TradingOrder(
            account_id=1, symbol="AAPL240719C00200000", quantity=1.0,
            side=OrderDirection.BUY, order_type=OrderType.MARKET,
            status=OrderStatus.CANCELED, transaction_id=txn_id,
            asset_class=AssetClass.OPTION, underlying_symbol=SYMBOL,
            option_type=OptionRight.CALL,
        ))
        cond = HasOptionPositionCondition(
            account=None, instrument_name=SYMBOL, expert_recommendation=_Rec(),
            existing_order=None)
        has = cond.evaluate()

    assert has is False, "a cancelled (terminal) order is not an open option position"
