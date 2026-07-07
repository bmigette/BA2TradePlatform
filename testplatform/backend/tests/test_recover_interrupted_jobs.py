"""Startup crash recovery: a StrategyOptimization stuck at status='running' after a crash
must be corrected to 'failed' and pushed to synced workers — today only its TaskQueue row
gets fixed (marked 'stopped'), the StrategyOptimization itself is left lying about its state."""
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.database import Base
from app.models.task_queue import TaskQueue, TaskStatus
from app.models.strategy_optimization import StrategyOptimization


@pytest.fixture()
def db_session_factory(tmp_path, monkeypatch):
    db_path = tmp_path / "recover_test.sqlite"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    eng = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)
    yield Session
    eng.dispose()


def test_crashed_optimization_status_is_corrected_and_pushed(db_session_factory, monkeypatch):
    s = db_session_factory()
    opt = StrategyOptimization(
        name="crashed-opt", status="running", created_at=datetime.now(timezone.utc),
        strategy_id=1, fitness_metric="sharpe", optimization_type="genetic",
    )
    s.add(opt)
    s.commit()
    opt_id = opt.id  # capture before the session detaches/expires it below
    task = TaskQueue(
        task_id="t-1", task_type="strategy_optimization", name="crashed-opt-task",
        status=TaskStatus.RUNNING.value, payload={"optimization_id": opt_id},
    )
    s.add(task)
    s.commit()
    s.close()

    with patch("app.models.database.SessionLocal", db_session_factory):
        pushed = []
        with patch("app.main.push_optimization", lambda o, db: pushed.append(o.status)):
            from app.main import recover_interrupted_jobs
            recover_interrupted_jobs()

    s2 = db_session_factory()
    try:
        refreshed = s2.query(StrategyOptimization).filter(StrategyOptimization.id == opt_id).first()
        assert refreshed.status == "failed"
        assert "restart" in (refreshed.error_message or "").lower()
        assert refreshed.completed_at is not None
        assert pushed == ["failed"]

        refreshed_task = s2.query(TaskQueue).filter(TaskQueue.task_id == "t-1").first()
        assert refreshed_task.status == TaskStatus.STOPPED.value  # existing behaviour untouched
    finally:
        s2.close()


def test_one_row_failing_does_not_strand_subsequent_rows(db_session_factory, monkeypatch):
    """A failure recovering one crashed optimization must not prevent the NEXT crashed
    optimization in the same batch from being recovered too — since the TaskQueue rows are
    already flipped to STOPPED by the time this runs, a skipped row here is never retried on
    a future restart."""
    s = db_session_factory()
    opt1 = StrategyOptimization(
        name="opt1-will-fail-to-push", status="running", created_at=datetime.now(timezone.utc),
        strategy_id=1, fitness_metric="sharpe", optimization_type="genetic",
    )
    opt2 = StrategyOptimization(
        name="opt2-should-still-recover", status="running", created_at=datetime.now(timezone.utc),
        strategy_id=1, fitness_metric="sharpe", optimization_type="genetic",
    )
    s.add_all([opt1, opt2])
    s.commit()
    opt1_id, opt2_id = opt1.id, opt2.id
    task1 = TaskQueue(
        task_id="t-a", task_type="strategy_optimization", name="a",
        status=TaskStatus.RUNNING.value, payload={"optimization_id": opt1_id},
    )
    task2 = TaskQueue(
        task_id="t-b", task_type="strategy_optimization", name="b",
        status=TaskStatus.RUNNING.value, payload={"optimization_id": opt2_id},
    )
    s.add_all([task1, task2])
    s.commit()
    s.close()

    pushed = []

    def _push_or_raise(o, db):
        if o.id == opt1_id:
            raise RuntimeError("simulated push failure for opt1")
        pushed.append(o.id)

    with patch("app.models.database.SessionLocal", db_session_factory):
        with patch("app.main.push_optimization", _push_or_raise):
            from app.main import recover_interrupted_jobs
            recover_interrupted_jobs()

    s2 = db_session_factory()
    try:
        r2 = s2.query(StrategyOptimization).filter(StrategyOptimization.id == opt2_id).first()
        assert r2.status == "failed"  # opt2 still got recovered despite opt1's failure
        assert r2.completed_at is not None
        assert opt2_id in pushed
    finally:
        s2.close()


def test_non_optimization_task_types_are_left_alone(db_session_factory, monkeypatch):
    s = db_session_factory()
    task = TaskQueue(
        task_id="t-2", task_type="training_job", name="unrelated-crashed-job",
        status=TaskStatus.RUNNING.value, payload={"some_other_key": 123},
    )
    s.add(task)
    s.commit()
    s.close()

    with patch("app.models.database.SessionLocal", db_session_factory):
        with patch("app.main.push_optimization") as mock_push:
            from app.main import recover_interrupted_jobs
            recover_interrupted_jobs()
            mock_push.assert_not_called()


def test_completed_optimization_is_not_touched(db_session_factory, monkeypatch):
    """A crashed TaskQueue row pointing at an ALREADY-completed optimization (e.g. the crash
    happened during a later, unrelated step) must not be clobbered back to 'failed'."""
    s = db_session_factory()
    opt = StrategyOptimization(
        name="already-done", status="completed", created_at=datetime.now(timezone.utc),
        strategy_id=1, fitness_metric="sharpe", optimization_type="genetic",
    )
    s.add(opt)
    s.commit()
    opt_id = opt.id  # capture before the session detaches/expires it below
    task = TaskQueue(
        task_id="t-3", task_type="strategy_optimization", name="already-done-task",
        status=TaskStatus.RUNNING.value, payload={"optimization_id": opt_id},
    )
    s.add(task)
    s.commit()
    s.close()

    with patch("app.models.database.SessionLocal", db_session_factory):
        with patch("app.main.push_optimization") as mock_push:
            from app.main import recover_interrupted_jobs
            recover_interrupted_jobs()
            mock_push.assert_not_called()

    s2 = db_session_factory()
    try:
        refreshed = s2.query(StrategyOptimization).filter(StrategyOptimization.id == opt_id).first()
        assert refreshed.status == "completed"
    finally:
        s2.close()
