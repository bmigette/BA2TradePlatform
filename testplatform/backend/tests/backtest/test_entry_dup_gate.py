"""Live-parity duplicate-position entry gate (Phase 1c of the live<->backtest unification
plan). The backtest's _run_expert_bar now skips an entry when an OPENED or WAITING
transaction already exists for (expert, symbol) — mirroring TradeManager's enter_market
safety check (OPENED+WAITING), so a not-yet-filled WAITING entry can't be stacked."""
from app.services.backtest.backtest_db import backtest_trading_db
from app.services.backtest.daily_engine import DailyBacktestEngine
from ba2_common.core.db import add_instance
from ba2_common.core.models import Transaction
from ba2_common.core.types import OrderDirection, TransactionStatus


def _txn(expert_id, symbol, status):
    return Transaction(expert_id=expert_id, symbol=symbol, quantity=10.0,
                       side=OrderDirection.BUY, status=status)


def test_dup_gate_opened_and_waiting_and_scoping():
    ctx = backtest_trading_db("dupgate")
    ctx.__enter__()
    try:
        eng = DailyBacktestEngine.__new__(DailyBacktestEngine)
        # nothing held -> not blocked
        assert eng._has_open_or_waiting_position(1, "AAPL") is False

        # an OPENED position blocks a duplicate entry
        add_instance(_txn(1, "AAPL", TransactionStatus.OPENED))
        assert eng._has_open_or_waiting_position(1, "AAPL") is True

        # a WAITING (not-yet-filled) entry ALSO blocks — the key parity gain
        add_instance(_txn(1, "MSFT", TransactionStatus.WAITING))
        assert eng._has_open_or_waiting_position(1, "MSFT") is True

        # scoped by symbol AND expert
        assert eng._has_open_or_waiting_position(1, "NVDA") is False
        assert eng._has_open_or_waiting_position(2, "AAPL") is False

        # a CLOSED position does NOT block a re-entry
        add_instance(_txn(1, "NVDA", TransactionStatus.CLOSED))
        assert eng._has_open_or_waiting_position(1, "NVDA") is False
    finally:
        ctx.__exit__(None, None, None)
