"""goal2027 2.4 parity, LIVE side: an expert's OPEN_POSITIONS (manage) order processing runs before its
ENTER_MARKET (entry) processing when both fire in the same minute (ba2_common SAME_MINUTE_PASS_ORDER).
Backtest twin: testplatform/backend/tests/backtest/test_same_minute_pass_order_bt.py."""
import logging

from ba2_common.core.schedule_genes import SAME_MINUTE_PASS_ORDER
from ba2_trade_platform.core.ExpertPriority import ExpertPriority, ExpertRun
from ba2_trade_platform.core.types import AnalysisUseCase


def _run(batch="1"):
    return ExpertRun(batch_id=batch, cohort="c", expert_id=1, account_id=1, priority=1, self_trading=False)


def _prio():
    return ExpertPriority(lambda r: None, lambda r: None)


def _drive(ready_order):
    p, seen = _prio(), []
    p.register([_run()])
    p.seal(["1"])
    for case in ready_order:
        p.ready("1", case, lambda c=case: seen.append(c.value))
    return seen


def test_entry_ready_first_still_processes_manage_first():
    # The two callbacks are ready together only once all the run's tasks are done; arrival order is arbitrary.
    p, seen = _prio(), []
    r = _run()
    p.register([r])
    r.tasks.add("t")                       # an analysis is outstanding: nothing may process yet
    p.seal(["1"])
    p.ready("1", AnalysisUseCase.ENTER_MARKET, lambda: seen.append("enter_market"))
    p.ready("1", AnalysisUseCase.OPEN_POSITIONS, lambda: seen.append("open_positions"))
    assert seen == []
    p.finished("1", "t")
    assert seen == list(SAME_MINUTE_PASS_ORDER)


def test_manage_ready_first_keeps_the_same_order():
    p, seen = _prio(), []
    r = _run()
    p.register([r])
    r.tasks.add("t")
    p.seal(["1"])
    p.ready("1", AnalysisUseCase.OPEN_POSITIONS, lambda: seen.append("open_positions"))
    p.ready("1", AnalysisUseCase.ENTER_MARKET, lambda: seen.append("enter_market"))
    p.finished("1", "t")
    assert seen == list(SAME_MINUTE_PASS_ORDER)


def test_order_is_logged_at_info(monkeypatch):
    import ba2_trade_platform.core.ExpertPriority as ep
    msgs = []
    monkeypatch.setattr(ep.logger, "info", lambda m, *a, **k: msgs.append(m % a if a else m))
    p = _prio()
    r = _run()
    p.register([r])
    r.tasks.add("t")
    p.seal(["1"])
    p.ready("1", AnalysisUseCase.ENTER_MARKET, lambda: None)
    p.ready("1", AnalysisUseCase.OPEN_POSITIONS, lambda: None)
    p.finished("1", "t")
    assert any("same-minute pass order" in m and "open_positions -> enter_market" in m for m in msgs), msgs


def test_scheduler_no_longer_parks_the_exit_pass_behind_entry():
    """JobManager must submit the OPEN_POSITIONS expansion immediately (the parking made entry first)."""
    import inspect
    from ba2_trade_platform.core.JobManager import JobManager
    src = inspect.getsource(JobManager._execute_scheduled_analysis)
    assert "defer_open_positions_if_entry_in_flight" not in src
