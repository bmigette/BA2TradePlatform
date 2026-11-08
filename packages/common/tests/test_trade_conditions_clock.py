"""Task 3 (stock decision-pass replay): every wall-clock read a live condition evaluation can reach
goes through the evaluation-clock seam, so a decision replayed under CaptureContext.for_replay reads
the RECORDED instant, while a live process (no context) and a backtest (as_of / created_at) are unchanged.
"""
from datetime import date, datetime, timedelta, timezone

import pytest

from ba2_common.core.replay.clock import ReplayMiss
from ba2_common.core.replay.context import CaptureContext, use_capture_context
from ba2_common.core.replay.schemas import ReplayStatus
from ba2_common.core.trade_repository import (
    reset_trade_repository, set_trade_repository,
)

RECORDED = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)
SYMBOL = "AAPL"


def _replay(*reads):
    return CaptureContext.for_replay(
        analysis_id="a1", clock_reads=[r.isoformat() for r in reads],
        clock_read_phases=[ReplayStatus.PHASE_PROCESS] * len(reads),
        phase=ReplayStatus.PHASE_PROCESS)


class _Rec:
    instance_id = 1
    created_at = None  # a LIVE recommendation without the stamp: the condition reads the clock


class _NoClockAccount:
    pass


@pytest.fixture(autouse=True)
def _repo():
    from tests.test_trade_conditions_storage_agnostic import FakeTradeRepository
    set_trade_repository(FakeTradeRepository())
    yield
    reset_trade_repository()


def _days_opened(order):
    from ba2_common.core.TradeConditions import DaysOpenedCondition
    return DaysOpenedCondition(account=None, instrument_name=SYMBOL, expert_recommendation=_Rec(),
                               operator_str=">", value=0.0, existing_order=order)


def _order(created):
    from ba2_common.core.models import TradingOrder
    from ba2_common.core.types import OrderDirection, OrderStatus, OrderType
    return TradingOrder(account_id=1, symbol=SYMBOL, quantity=1.0, side=OrderDirection.BUY,
                        order_type=OrderType.MARKET, status=OrderStatus.FILLED, created_at=created)


def test_days_opened_reads_the_recorded_instant_under_replay():
    cond = _days_opened(_order(RECORDED - timedelta(days=5)))
    with use_capture_context(_replay(RECORDED)):
        assert cond.evaluate() is True
    assert cond.calculated_value == pytest.approx(5.0)


def test_days_opened_without_a_recorded_read_is_a_loud_miss_not_the_wall_clock():
    cond = _days_opened(_order(RECORDED - timedelta(days=5)))
    with use_capture_context(_replay()):
        with pytest.raises(ReplayMiss):
            cond.evaluate()


def test_days_opened_live_is_still_the_wall_clock():
    created = datetime.now(timezone.utc) - timedelta(days=2)
    cond = _days_opened(_order(created))
    assert cond.evaluate() is True
    assert cond.calculated_value == pytest.approx(2.0, abs=0.01)


def test_days_opened_recommendation_created_at_still_wins():
    """Backtest semantics: the recommendation's created_at is the clock; the seam is never asked."""
    class _SimRec(_Rec):
        created_at = datetime(2024, 6, 10, tzinfo=timezone.utc)
    from ba2_common.core.TradeConditions import DaysOpenedCondition
    cond = DaysOpenedCondition(account=None, instrument_name=SYMBOL, expert_recommendation=_SimRec(),
                               operator_str=">", value=0.0,
                               existing_order=_order(datetime(2024, 6, 1, tzinfo=timezone.utc)))
    with use_capture_context(_replay()):  # no reads: would raise if the seam were consulted
        assert cond.evaluate() is True
    assert cond.calculated_value == pytest.approx(9.0)


def test_days_since_last_close_reads_the_recorded_instant_under_replay(monkeypatch):
    from ba2_common.core.TradeConditions import DaysSinceLastCloseCondition
    from ba2_common.core.models import Transaction
    from ba2_common.core.trade_repository import get_trade_repository
    from ba2_common.core.types import OrderDirection, TransactionStatus
    get_trade_repository().rows.append(Transaction(
        id=3, expert_id=1, symbol=SYMBOL, status=TransactionStatus.CLOSED, side=OrderDirection.BUY,
        quantity=1.0, open_price=1.0, close_date=RECORDED - timedelta(days=3)))
    cond = DaysSinceLastCloseCondition(account=None, instrument_name=SYMBOL, expert_recommendation=_Rec(),
                                       operator_str=">", value=2.0, existing_order=None)
    with use_capture_context(_replay(RECORDED)):
        assert cond.evaluate() is True
    assert cond.calculated_value == pytest.approx(3.0)


def test_evaluation_date_is_the_recorded_day_under_replay():
    from ba2_common.core.TradeConditions import DaysToEarningsCondition
    cond = DaysToEarningsCondition(account=_NoClockAccount(), instrument_name=SYMBOL,
                                   expert_recommendation=_Rec(), operator_str="<", value=5.0,
                                   existing_order=None)
    with use_capture_context(_replay(RECORDED)):
        got = cond._evaluation_date()
    assert got == RECORDED.astimezone().date()


def test_evaluation_date_live_is_date_today():
    from ba2_common.core.TradeConditions import DaysToEarningsCondition
    cond = DaysToEarningsCondition(account=_NoClockAccount(), instrument_name=SYMBOL,
                                   expert_recommendation=_Rec(), operator_str="<", value=5.0,
                                   existing_order=None)
    assert cond._evaluation_date() == date.today()


def test_evaluation_date_backtest_account_clock_is_untouched():
    from ba2_common.core.TradeConditions import DaysToEarningsCondition

    class _SimAccount:
        def _as_of_date(self):
            return datetime(2024, 3, 4, tzinfo=timezone.utc)
    cond = DaysToEarningsCondition(account=_SimAccount(), instrument_name=SYMBOL,
                                   expert_recommendation=_Rec(), operator_str="<", value=5.0,
                                   existing_order=None)
    with use_capture_context(_replay()):  # no reads: a simulated clock never consults the seam
        assert cond._evaluation_date() is not None
