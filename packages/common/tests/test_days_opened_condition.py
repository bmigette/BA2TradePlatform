"""Regression test for the wall-clock/lookahead bug in ``DaysOpenedCondition``.

The condition must derive "now" from the SIMULATED as-of (the recommendation's
``created_at``) and the position OPEN time from the transaction's (sim-stamped)
``open_date`` — NOT ``datetime.now()`` and NOT the order row's wall-clock
``created_at``. Otherwise, in a backtest whose clock is a 2024 as-of, every bar
returns ``days_opened ~= 0`` and a ``days_opened > N`` exit rule NEVER fires.

This mirrors the correct sibling ``DaysSinceLastCloseCondition`` which already
uses ``expert_recommendation.created_at`` as the sim-aware reference.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace


def _setup_db(tmp_path):
    from ba2_common.core import db
    db.configure_db(str(tmp_path / "days_opened.sqlite"))
    db.init_db()
    return db


class _FakeAccount:
    """Minimal stand-in: the base condition only stores the reference."""
    id = 1


def test_days_opened_uses_sim_as_of_not_wall_clock(tmp_path):
    """SIM clock = a 2024 as-of; position opened ~40 SIM-days earlier.

    days_opened must be ~40 (NOT ~0). With the wall-clock bug the order's
    ``created_at`` is "now" (2026) and ``datetime.now()`` is also "now", so the
    difference collapses to ~0 and ``days_opened > 30`` would never fire.
    """
    from ba2_common.core import db
    from ba2_common.core.models import Transaction, TradingOrder
    from ba2_common.core.types import OrderDirection, OrderType, OrderStatus, TransactionStatus
    from ba2_common.core.TradeConditions import DaysOpenedCondition

    _setup_db(tmp_path)

    sim_as_of = datetime(2024, 6, 15, tzinfo=timezone.utc)        # the simulated bar
    sim_open = sim_as_of - timedelta(days=40)                     # opened 40 sim-days ago

    # Transaction is OPENED with a SIM-time open_date (what part 2 of the fix stamps).
    txn = Transaction(
        symbol="AAPL",
        quantity=10,
        side=OrderDirection.BUY,
        status=TransactionStatus.OPENED,
        open_date=sim_open,
    )
    txn_id = db.add_instance(txn)

    # The entry order's row created_at is WALL clock (DB default) on purpose — the
    # fix must NOT use it as the open reference in a backtest.
    entry = TradingOrder(
        account_id=1,
        symbol="AAPL",
        quantity=10,
        side=OrderDirection.BUY,
        order_type=OrderType.MARKET,
        status=OrderStatus.FILLED,
        transaction_id=txn_id,
        created_at=datetime.now(timezone.utc),  # wall clock (2026)
    )
    db.add_instance(entry, expunge_after_flush=True)

    rec = SimpleNamespace(created_at=sim_as_of, instance_id=1, symbol="AAPL")

    cond = DaysOpenedCondition(
        account=_FakeAccount(),
        instrument_name="AAPL",
        expert_recommendation=rec,
        operator_str=">",
        value=30,
        existing_order=entry,
    )

    fired = cond.evaluate()
    assert cond.get_calculated_value() is not None
    assert abs(cond.get_calculated_value() - 40.0) < 1.0, (
        f"days_opened should be ~40 (sim as-of), got {cond.get_calculated_value()}"
    )
    assert fired is True, "days_opened > 30 must FIRE after 40 sim days"


def test_days_opened_under_threshold_does_not_fire(tmp_path):
    """Symmetric guard: opened 5 sim-days ago, ``> 30`` must NOT fire."""
    from ba2_common.core import db
    from ba2_common.core.models import Transaction, TradingOrder
    from ba2_common.core.types import OrderDirection, OrderType, OrderStatus, TransactionStatus
    from ba2_common.core.TradeConditions import DaysOpenedCondition

    _setup_db(tmp_path)

    sim_as_of = datetime(2024, 6, 15, tzinfo=timezone.utc)
    sim_open = sim_as_of - timedelta(days=5)

    txn = Transaction(
        symbol="MSFT",
        quantity=10,
        side=OrderDirection.BUY,
        status=TransactionStatus.OPENED,
        open_date=sim_open,
    )
    txn_id = db.add_instance(txn)
    entry = TradingOrder(
        account_id=1,
        symbol="MSFT",
        quantity=10,
        side=OrderDirection.BUY,
        order_type=OrderType.MARKET,
        status=OrderStatus.FILLED,
        transaction_id=txn_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add_instance(entry, expunge_after_flush=True)

    rec = SimpleNamespace(created_at=sim_as_of, instance_id=1, symbol="MSFT")
    cond = DaysOpenedCondition(
        account=_FakeAccount(),
        instrument_name="MSFT",
        expert_recommendation=rec,
        operator_str=">",
        value=30,
        existing_order=entry,
    )
    assert cond.evaluate() is False
    assert abs(cond.get_calculated_value() - 5.0) < 1.0
