"""goal2027 2.4 (owner decision: ENTRY FIRST is KEPT). Pins today's live same-minute order: an expert's
ENTER_MARKET order processing runs before its OPEN_POSITIONS processing, and JobManager parks the exit pass
behind an in-flight entry pass (WorkerQueue.defer_open_positions_if_entry_in_flight, e5e37c6a).
Backtest twin: testplatform/backend/tests/backtest/test_same_minute_pass_order_bt.py.
The GA can still put exits before entries by choosing manage_time earlier than entry_time."""
import inspect

from ba2_trade_platform.core.ExpertPriority import ExpertPriority, ExpertRun
from ba2_trade_platform.core.types import AnalysisUseCase


def _drive(first, second):
    p, seen = ExpertPriority(lambda r: None, lambda r: None), []
    r = ExpertRun(batch_id="1", cohort="c", expert_id=1, account_id=1, priority=1, self_trading=False)
    p.register([r])
    r.tasks.add("t")
    p.seal(["1"])
    for case in (first, second):
        p.ready("1", case, lambda c=case: seen.append(c.value))
    p.finished("1", "t")
    return seen


def test_entry_is_processed_before_manage_whatever_the_arrival_order():
    expected = ["enter_market", "open_positions"]
    assert _drive(AnalysisUseCase.ENTER_MARKET, AnalysisUseCase.OPEN_POSITIONS) == expected
    assert _drive(AnalysisUseCase.OPEN_POSITIONS, AnalysisUseCase.ENTER_MARKET) == expected


def test_scheduler_parks_the_exit_pass_behind_entry():
    from ba2_trade_platform.core.JobManager import JobManager
    assert "defer_open_positions_if_entry_in_flight" in inspect.getsource(JobManager._execute_scheduled_analysis)
