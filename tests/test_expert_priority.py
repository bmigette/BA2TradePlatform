"""Scheduled cohorts order capital access, not completion speed of analysis."""
from datetime import datetime, timezone
from threading import Event, Lock, Thread
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from apscheduler.triggers.cron import CronTrigger

from ba2_trade_platform.core.ExpertPriority import ExpertPriority, ExpertRun, validate_expert_priority
from ba2_trade_platform.core.WorkerQueue import WorkerQueue, AnalysisTask
from ba2_trade_platform.core.types import AnalysisUseCase, WorkerTaskStatus


def run(expert_id, priority=1, account=1, cohort="slot", self_trading=False):
    return ExpertRun(str(expert_id), cohort, expert_id, account, priority, self_trading)


def task(expert_id, name):
    return AnalysisTask(name, expert_id, "AAPL", batch_id=str(expert_id))


def coordinator(*runs):
    completed = []
    c = ExpertPriority(lambda r: completed.append(r.expert_id), lambda r: None)
    c.register(runs)
    return c, completed


def test_lower_analysis_can_finish_first_but_orders_wait_for_all_higher_work():
    c, done = coordinator(run(1, 100), run(2))
    orders, analyses = [], []
    c.submit("1", task(1, "expand"), lambda: analyses.append(1))
    c.submit("2", task(2, "low"), lambda: analyses.append(2))
    c.seal(["1", "2"])
    c.ready("2", "ENTER_MARKET", lambda: orders.append(2))
    c.finished("2", "low")
    assert analyses == [1, 2] and orders == []
    # An expansion still producing children must keep the run alive.
    c.submit("1", task(1, "high"), lambda: None)
    c.finished("1", "expand")
    c.ready("1", "ENTER_MARKET", lambda: orders.append(1))
    assert orders == []
    c.finished("1", "high")
    assert orders == [1, 2] and done == [1, 2]


def test_smart_rm_completion_not_submission_releases_lower_expert():
    c, done = coordinator(run(1, 100), run(2))
    c.submit("1", task(1, "analysis"), lambda: None)
    c.submit("2", task(2, "analysis"), lambda: None)
    orders = []
    c.ready("1", "ENTER_MARKET", lambda: c.submit("1", task(1, "smart"), lambda: None))
    c.ready("2", "ENTER_MARKET", lambda: orders.append(2))
    c.seal(["1", "2"])
    c.finished("2", "analysis")
    c.finished("1", "analysis")
    assert orders == [] and done == []
    c.finished("1", "smart")
    assert orders == [2] and done == [1, 2]


@pytest.mark.parametrize("other", [run(2, 100), run(2, 1, account=2), run(2, 1, cohort="later")])
def test_equal_priority_other_account_and_other_time_are_independent(other):
    c, done = coordinator(run(1, 100), other)
    c.submit("1", task(1, "slow"), lambda: None)
    c.seal(["1", "2"])
    assert done == [2]


def test_self_trading_experts_wait_without_occupying_a_worker():
    c, done = coordinator(run(1, 100), run(2, self_trading=True))
    started = []
    c.submit("1", task(1, "high"), lambda: started.append(1))
    c.submit("2", task(2, "inline-trades"), lambda: started.append(2))
    c.seal(["1", "2"])
    assert started == [1]
    c.finished("1", "high")
    assert started == [1, 2] and done == [1]


def test_failed_processing_and_empty_expansion_release_waiters():
    c, done = coordinator(run(1, 100), run(2, 50), run(3))
    c.ready("1", "ENTER_MARKET", Mock(side_effect=ValueError("failure")))
    c.seal(["1", "2", "3"])
    assert done == [1, 2, 3]


def test_registration_seal_prevents_fast_analysis_beating_unsubmitted_peer():
    c, done = coordinator(run(1, 100), run(2))
    orders = []
    c.ready("2", "ENTER_MARKET", lambda: orders.append(2))
    assert orders == []
    c.ready("1", "ENTER_MARKET", lambda: orders.append(1))
    c.seal(["1", "2"])
    assert orders == [1, 2]


def test_concurrent_completions_do_not_release_while_high_orders_are_running():
    c, done = coordinator(run(1, 100), run(2))
    entered, finish = Event(), Event()
    def high_orders():
        entered.set()
        assert finish.wait(3)
    c.submit("1", task(1, "high"), lambda: None)
    c.submit("2", task(2, "low"), lambda: None)
    c.ready("1", "ENTER_MARKET", high_orders)
    c.ready("2", "ENTER_MARKET", lambda: None)
    c.seal(["1", "2"])
    thread = Thread(target=c.finished, args=("1", "high"))
    thread.start()
    try:
        assert entered.wait(2)
        c.finished("2", "low")
        assert done == []
    finally:
        finish.set()
        thread.join(3)
    assert not thread.is_alive() and done == [1, 2]


def test_final_exit_pass_is_part_of_high_priority_run():
    done = []
    def before(r):
        if r.expert_id == 1:
            c.submit("1", task(1, "exits"), lambda: None)
    c = ExpertPriority(lambda r: done.append(r.expert_id), before)
    c.register([run(1, 100), run(2)])
    c.seal(["1", "2"])
    assert done == []
    c.finished("1", "exits")
    assert done == [1, 2]


@pytest.fixture
def worker(monkeypatch):
    q = WorkerQueue()
    q._running = True
    monkeypatch.setattr(q, "_persist_task", Mock(return_value=True))
    monkeypatch.setattr(q, "_remove_persisted_task", Mock(return_value=True))
    monkeypatch.setattr(q, "_update_persisted_task_status", Mock(return_value=True))
    return q


def test_worker_defers_processing_and_inherits_priority_to_expansion_children(worker, monkeypatch):
    q = worker
    q._expert_priority.register([run(1, 100), run(2)])
    high = q.submit_analysis_task(1, "AAPL", batch_id="1")
    low = q.submit_analysis_task(2, "MSFT", batch_id="2")
    assert q._queue.get_nowait()[2].id == high
    assert q._tasks[high].expert_priority == 100
    orders = []
    monkeypatch.setattr(q, "_process_expert_recommendations", lambda expert_id, *_: orders.append(expert_id))
    q._expert_priority.seal(["1", "2"])
    q._check_and_process_expert_recommendations(2, AnalysisUseCase.ENTER_MARKET, "2")
    q._expert_priority.finished("2", low)
    assert orders == []
    q._check_and_process_expert_recommendations(1, AnalysisUseCase.ENTER_MARKET, "1")
    q._expert_priority.finished("1", high)
    assert orders == [1, 2]


def test_cancel_held_self_trading_task_releases_run_without_enqueuing(worker):
    q = worker
    q._expert_priority.register([run(1, 100), run(2, self_trading=True)])
    high = q.submit_analysis_task(1, "AAPL", batch_id="1")
    low = q.submit_analysis_task(2, "MSFT", batch_id="2")
    q._expert_priority.seal(["1", "2"])
    assert q.cancel_task(low)
    assert q.cancel_task(high)
    assert not q._expert_priority.contains("1") and not q._expert_priority.contains("2")
    assert q._queue.qsize() == 1  # cancelled high is ignored when dequeued
    assert q._tasks[low].status == WorkerTaskStatus.FAILED


def test_expansion_exception_releases_priority(worker, monkeypatch):
    import ba2_trade_platform.core.JobManager as jm
    q = worker
    q._expert_priority.register([run(1, 100), run(2)])
    high = q.submit_instrument_expansion_task(1, "EXPERT", batch_id="1")
    q._expert_priority.seal(["1", "2"])
    monkeypatch.setattr(jm, "get_job_manager", lambda: SimpleNamespace(
        _execute_expert_driven_analysis=Mock(side_effect=ValueError("no data"))))
    q._execute_instrument_expansion_task(q._tasks[high], "test")
    assert not q._expert_priority.contains("1") and not q._expert_priority.contains("2")


def test_scheduler_first_low_callback_registers_every_peer_before_dispatch(worker, monkeypatch):
    import ba2_trade_platform.core.JobManager as module
    jm = module.JobManager.__new__(module.JobManager)
    jm._lock, jm._dispatch_lock, jm._dispatched_slots = Lock(), Lock(), set()
    slot = datetime(2026, 9, 15, 13, 30, tzinfo=timezone.utc)
    trigger = CronTrigger(hour=13, minute=30, timezone=timezone.utc)
    jm._scheduled_jobs = {f"expert_{i}": SimpleNamespace(trigger=trigger, next_run_time=slot,
        args=[i, "EXPERT", AnalysisUseCase.ENTER_MARKET]) for i in [2, 1]}
    # Maintenance jobs have no expert arguments; sharing a trigger must be harmless.
    jm._scheduled_jobs["account_refresh"] = SimpleNamespace(trigger=trigger, args=[])
    jm._scheduled_jobs["expert_paused"] = SimpleNamespace(trigger=trigger, next_run_time=None,
        args=[999, "EXPERT", AnalysisUseCase.ENTER_MARKET])
    records = {i: SimpleNamespace(id=i, account_id=1, enabled=True, priority=100 if i == 1 else 1) for i in [1, 2]}
    monkeypatch.setattr(module, "get_instance", lambda _, i: records[i])
    monkeypatch.setattr(module, "get_worker_queue", lambda: worker)
    from ba2_trade_platform.core import utils
    monkeypatch.setattr(utils, "get_expert_instance_from_id", lambda i: SimpleNamespace())
    monkeypatch.setattr(utils, "expert_uses_risk_manager", lambda cls: True)
    calls = []
    def dispatch(i, *args, **kwargs):
        assert len(worker._expert_priority._runs) == 2
        calls.append(i)
    monkeypatch.setattr(jm, "_execute_scheduled_analysis", dispatch)
    jm._execute_scheduled_group(2, "EXPERT", AnalysisUseCase.ENTER_MARKET, scheduled_for=slot)
    jm._execute_scheduled_group(1, "EXPERT", AnalysisUseCase.ENTER_MARKET, scheduled_for=slot)
    assert calls == [1, 2]


def test_executor_passes_original_fire_time_without_mutating_job(monkeypatch):
    from apscheduler.executors.pool import ThreadPoolExecutor
    from ba2_trade_platform.core.ScheduledExpertExecutor import ScheduledExpertExecutor
    received = []
    monkeypatch.setattr(ThreadPoolExecutor, "_do_submit_job", lambda self, job, times: received.append((job, times)))
    executor = ScheduledExpertExecutor()
    try:
        job = SimpleNamespace(kwargs={"a": 1})
        slot = datetime(2026, 9, 15, 13, 30, tzinfo=timezone.utc)
        executor._do_submit_job(job, [slot])
        assert job.kwargs == {"a": 1}
        assert received[0][0].kwargs == {"a": 1, "scheduled_for": slot}
        assert received[0][1] == [slot]
    finally:
        executor.shutdown()


class _ScheduledReceiver:
    def __init__(self):
        self.received = []
        self.event = Event()

    def execute(self, expert_id, scheduled_for=None):
        self.received.append((expert_id, scheduled_for))
        self.event.set()


def test_real_apscheduler_bound_method_execution():
    from apscheduler.schedulers.background import BackgroundScheduler
    from ba2_trade_platform.core.ScheduledExpertExecutor import ScheduledExpertExecutor
    executor = ScheduledExpertExecutor()
    scheduler = BackgroundScheduler(executors={"experts": executor})
    receiver = _ScheduledReceiver()
    scheduler.start(paused=True)
    try:
        slot = datetime.now(timezone.utc)
        job = scheduler.add_job(receiver.execute, "date", run_date=slot, args=[11],
                                executor="experts", misfire_grace_time=60)
        executor.submit_job(job, [slot])
        assert receiver.event.wait(3)
        assert receiver.received == [(11, slot)]
        assert job.kwargs == {} and job.args == (11,)
    finally:
        scheduler.shutdown()


@pytest.mark.parametrize("value", [0, -1, 1.5, True, None, "invalid"])
def test_priority_rejects_invalid_values(value):
    with pytest.raises((ValueError, TypeError)):
        validate_expert_priority(value)


def test_default_priority_and_migration_existing_rows():
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from ba2_trade_platform.core.models import ExpertInstance
    assert ExpertInstance(account_id=1, expert="FMPRating").priority == 1
    # Load the migration by path: the repo's alembic/ is not a Python package.
    from importlib.util import spec_from_file_location, module_from_spec
    from pathlib import Path
    spec = spec_from_file_location("priority_migration", Path(__file__).parents[1] /
        "alembic/versions/d9e3b72a10fc_add_expert_priority.py")
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE expertinstance (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO expertinstance(id) VALUES(11)")
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migration.upgrade()  # safe if create_all already added the column
        assert connection.exec_driver_sql("SELECT priority FROM expertinstance").scalar() == 1
        connection.exec_driver_sql("INSERT INTO expertinstance(id) VALUES(12)")
        assert connection.exec_driver_sql("SELECT priority FROM expertinstance WHERE id=12").scalar() == 1


@pytest.mark.parametrize("legacy", [{}, {"priority": None}, {"expert_params": {"something": 7}}])
def test_old_settings_import_as_one_and_preserve_existing_priority(legacy):
    from ba2_trade_platform.core.ExpertPriority import priority_from_settings
    assert priority_from_settings(legacy) == 1
    assert priority_from_settings(legacy, current=100) == 100
    assert priority_from_settings({"priority": 50}, current=100) == 50


def test_legacy_tasks_without_expert_priority_keep_queue_behavior():
    from ba2_trade_platform.core.SmartPriorityQueue import SmartPriorityQueue
    q = SmartPriorityQueue()
    old = SimpleNamespace(expert_instance_id=1)
    q.put((0, 1, old))
    assert q.get_nowait()[2] is old


def test_legacy_positional_task_constructors_keep_task_priority():
    from ba2_trade_platform.core.WorkerQueue import SmartRiskManagerTask, InstrumentExpansionTask
    items = [AnalysisTask("a", 1, "AAPL", AnalysisUseCase.ENTER_MARKET, 7),
             InstrumentExpansionTask("b", 1, "EXPERT", AnalysisUseCase.ENTER_MARKET, 7),
             SmartRiskManagerTask("c", 1, 1, 7)]
    assert all(item.priority == 7 and item.expert_priority == 1 for item in items)


def test_running_analysis_cannot_be_cancelled_or_release_its_priority(worker):
    q = worker
    q._expert_priority.register([run(1, 100), run(2)])
    ident = q.submit_analysis_task(1, "AAPL", batch_id="1")
    q._tasks[ident].status = WorkerTaskStatus.RUNNING
    q._tasks[ident].market_analysis_id = 99
    q._expert_priority.seal(["1", "2"])
    assert not q.cancel_analysis_task(1, "AAPL")[0]
    assert not q.cancel_analysis_by_market_analysis_id(99)[0]
    assert q._expert_priority.contains("1")


def test_recovered_smart_rm_task_waits_for_higher_priority(worker):
    q = worker
    q._expert_priority.register([run(1, 100), run(2)])
    high = q.submit_analysis_task(1, "AAPL", batch_id="1")
    smart = q.submit_smart_risk_manager_task(2, 1, batch_id="2")
    q._expert_priority.seal(["1", "2"])
    assert q._queue.qsize() == 1
    assert q._queue.get_nowait()[2].id == high
    q._expert_priority.finished("1", high)
    assert q._queue.get_nowait()[2].id == smart


def test_completed_analysis_still_parks_exit_pass_until_priority_processing(worker):
    q = worker
    q._expert_priority.register([run(1, 100), run(2)])
    high = q.submit_analysis_task(1, "AAPL", batch_id="1")
    low = q.submit_analysis_task(2, "MSFT", batch_id="2")
    q._tasks[low].status = WorkerTaskStatus.COMPLETED
    q._check_and_process_expert_recommendations(2, AnalysisUseCase.ENTER_MARKET, "2")
    q._expert_priority.finished("2", low)
    assert q.defer_open_positions_if_entry_in_flight(2, "2")


@pytest.mark.parametrize("other_group,expected", [
    (("2026-09-15T09:30:00+00:00", 1), "high"),
    (("2026-09-15T10:30:00+00:00", 1), "other"),
    (("2026-09-16T09:30:00+00:00", 1), "other"),
    (("2026-09-15T09:30:00+00:00", 2), "other"),
    (None, "other"),
])
def test_queue_priority_only_affects_same_time_and_account(other_group, expected):
    from ba2_trade_platform.core.SmartPriorityQueue import SmartPriorityQueue
    q = SmartPriorityQueue()
    high = task(1, "high")
    high.priority_group = ("2026-09-15T09:30:00+00:00", 1)
    high.expert_priority = 100
    other = task(2, "other")
    other.priority_group = other_group
    # Ordinary fairness favors expert 2, which has not had a turn yet.
    q._expert_last_picked[1] = 10.0
    q.put((0, 1, high))
    q.put((0, 2, other))
    assert q.get_nowait()[2].id == expected


def test_different_time_processing_does_not_wait_for_earlier_callback():
    c, done = coordinator(run(1, 100, cohort="2026-09-15T09:30:00+00:00"),
                          run(2, cohort="2026-09-15T10:30:00+00:00"))
    entered, finish, later_processed = Event(), Event(), Event()
    def earlier_orders():
        entered.set()
        assert finish.wait(3)
    c.submit("1", task(1, "early"), lambda: None)
    c.submit("2", task(2, "late"), lambda: None)
    c.ready("1", "ENTER_MARKET", earlier_orders)
    c.ready("2", "ENTER_MARKET", later_processed.set)
    c.seal(["1", "2"])
    thread = Thread(target=c.finished, args=("1", "early"))
    thread.start()
    try:
        assert entered.wait(2)
        c.finished("2", "late")
        assert later_processed.is_set()
        assert done == [2]
    finally:
        finish.set()
        thread.join(3)
    assert not thread.is_alive() and done == [2, 1]


@pytest.mark.parametrize("early,late", [
    (datetime(2026, 9, 15, 9, 30, tzinfo=timezone.utc),
     datetime(2026, 9, 15, 10, 30, tzinfo=timezone.utc)),
    (datetime(2026, 9, 14, 9, 30, tzinfo=timezone.utc),
     datetime(2026, 9, 15, 9, 30, tzinfo=timezone.utc)),
], ids=["different-time-same-day", "same-time-different-day"])
def test_later_schedule_runs_while_earlier_high_priority_is_still_analyzing(worker, monkeypatch, early, late):
    import ba2_trade_platform.core.JobManager as module
    from ba2_trade_platform.core import utils
    jm = module.JobManager.__new__(module.JobManager)
    jm._lock, jm._dispatch_lock, jm._dispatched_slots = Lock(), Lock(), set()
    slots = {1: early, 2: early, 3: late}
    jm._scheduled_jobs = {f"expert_{i}": SimpleNamespace(
        trigger=CronTrigger(day_of_week=slot.weekday(), hour=slot.hour, minute=30, timezone=timezone.utc),
        next_run_time=slot, args=[i, "EXPERT", AnalysisUseCase.ENTER_MARKET])
        for i, slot in slots.items()}
    records = {i: SimpleNamespace(id=i, account_id=1, enabled=True,
                                 priority=100 if i == 1 else 1) for i in slots}
    monkeypatch.setattr(module, "get_instance", lambda _, i: records[i])
    monkeypatch.setattr(module, "get_worker_queue", lambda: worker)
    monkeypatch.setattr(utils, "get_expert_instance_from_id", lambda i: SimpleNamespace())
    monkeypatch.setattr(utils, "expert_uses_risk_manager", lambda cls: True)
    submitted, processed = {}, []
    def dispatch(i, symbol, use_case, scheduled_for):
        batch = f"{i}_{scheduled_for.astimezone().strftime('%H%M_%Y%m%d')}"
        ident = worker.submit_analysis_task(i, symbol, subtype=use_case, batch_id=batch)
        submitted[i] = (batch, ident)
    monkeypatch.setattr(jm, "_execute_scheduled_analysis", dispatch)
    monkeypatch.setattr(worker, "_process_expert_recommendations", lambda i, *_: processed.append(i))
    jm._execute_scheduled_group(1, "EXPERT", AnalysisUseCase.ENTER_MARKET, scheduled_for=early)
    assert set(submitted) == {1, 2}  # Future schedules are not pre-registered.
    jm._execute_scheduled_group(3, "EXPERT", AnalysisUseCase.ENTER_MARKET, scheduled_for=late)
    for i in (2, 3):
        batch, ident = submitted[i]
        worker._check_and_process_expert_recommendations(i, AnalysisUseCase.ENTER_MARKET, batch)
        worker._expert_priority.finished(batch, ident)
    assert processed == [3]  # Earlier low waits; later low does not.
    assert worker._expert_priority.contains(submitted[1][0])
    worker._expert_priority.finished(*submitted[1])
    assert processed == [3, 2]


def test_monday_only_priority_expert_is_not_registered_for_tuesday(worker, monkeypatch):
    from zoneinfo import ZoneInfo
    import ba2_trade_platform.core.JobManager as module
    from ba2_trade_platform.core import utils
    jm = module.JobManager.__new__(module.JobManager)
    jm._lock, jm._dispatch_lock, jm._dispatched_slots = Lock(), Lock(), set()
    market_zone = ZoneInfo("America/New_York")
    tuesday = datetime(2026, 9, 15, 13, 30, tzinfo=timezone.utc)  # 09:30 New York
    monday_trigger = CronTrigger(day_of_week="mon", hour=9, minute=30, timezone=market_zone)
    daily_trigger = CronTrigger(day_of_week="mon-fri", hour=9, minute=30, timezone=market_zone)
    jm._scheduled_jobs = {
        "expert_11": SimpleNamespace(trigger=monday_trigger,
            next_run_time=monday_trigger.get_next_fire_time(None, tuesday),
            args=[11, "EXPERT", AnalysisUseCase.ENTER_MARKET]),
        "expert_2": SimpleNamespace(trigger=daily_trigger, next_run_time=tuesday,
            args=[2, "EXPERT", AnalysisUseCase.ENTER_MARKET]),
    }
    records = {11: SimpleNamespace(id=11, account_id=1, enabled=True, priority=100),
               2: SimpleNamespace(id=2, account_id=1, enabled=True, priority=1)}
    lookup = Mock(side_effect=lambda _, i: records[i])
    monkeypatch.setattr(module, "get_instance", lookup)
    monkeypatch.setattr(module, "get_worker_queue", lambda: worker)
    monkeypatch.setattr(utils, "get_expert_instance_from_id", lambda i: SimpleNamespace())
    monkeypatch.setattr(utils, "expert_uses_risk_manager", lambda cls: True)
    submitted, processed = [], []
    def dispatch(i, symbol, use_case, scheduled_for):
        submitted.append(i)
        assert {r.expert_id for r in worker._expert_priority._runs.values()} == {2}
        batch = f"{i}_{scheduled_for.astimezone().strftime('%H%M_%Y%m%d')}"
        ident = worker.submit_analysis_task(i, symbol, subtype=use_case, batch_id=batch)
        worker._check_and_process_expert_recommendations(i, use_case, batch)
        worker._expert_priority.finished(batch, ident)
    monkeypatch.setattr(jm, "_execute_scheduled_analysis", dispatch)
    monkeypatch.setattr(worker, "_process_expert_recommendations", lambda i, *_: processed.append(i))
    jm._execute_scheduled_group(2, "EXPERT", AnalysisUseCase.ENTER_MARKET, scheduled_for=tuesday)
    assert submitted == processed == [2]
    assert lookup.call_count == 1 and lookup.call_args.args[1] == 2
    assert worker._expert_priority.waiting_batches() == set()
