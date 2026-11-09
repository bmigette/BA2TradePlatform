"""Cancel / pause of a RUNNING task: the handler stops on both, and the queue never finalises
over the user's request.

Before the fix ``cancel_task`` wrote CANCELLED but the optimization handler only polled
``is_task_paused`` (PAUSED only), so a cancelled GA ran to the end; and ``_process_task_inline``
then wrote COMPLETED over CANCELLED / PAUSED, which also made a paused job un-resumable.
"""
from __future__ import annotations

import uuid

import pytest

from app.models.database import Base, SessionLocal, engine
from app.models.task_queue import TaskQueue, TaskStatus
from app.services.task_queue import TaskQueueService


@pytest.fixture()
def svc():
    Base.metadata.create_all(bind=engine)
    return TaskQueueService(max_workers=1, name="stop-test")


def _running_task(svc, handler, task_type="stop_test"):
    svc.register_handler(task_type, handler)
    tid = svc.queue_task(task_type=task_type, name=f"t-{uuid.uuid4().hex[:6]}", payload={})
    db = SessionLocal()
    try:
        row = db.query(TaskQueue).filter(TaskQueue.task_id == tid).first()
        row.status = TaskStatus.RUNNING.value
        db.commit()
        db.refresh(row)
        db.expunge(row)
        return tid, row
    finally:
        db.close()


def _row(tid):
    db = SessionLocal()
    try:
        r = db.query(TaskQueue).filter(TaskQueue.task_id == tid).first()
        return r.status, r.result, r.progress
    finally:
        db.close()


def test_stop_reason_distinguishes_pause_cancel_and_none(svc):
    tid, _ = _running_task(svc, lambda t, p: {"status": "completed"})
    assert svc.stop_reason(tid) is None
    assert svc.pause_task(tid)
    assert svc.stop_reason(tid) == "paused" and svc.is_task_paused(tid)
    assert svc.cancel_task(tid)
    assert svc.stop_reason(tid) == "cancelled" and not svc.is_task_paused(tid)
    assert svc.stop_reason("no-such-task") is None


def test_cancel_during_run_stays_cancelled_even_if_handler_returns_completed(svc):
    def handler(task_id, payload):
        assert svc.cancel_task(task_id)
        return {"status": "completed", "best": 1}

    tid, row = _running_task(svc, handler)
    svc._process_task_inline(row, "w")
    status, result, progress = _row(tid)
    assert status == TaskStatus.CANCELLED.value
    assert result is None                    # no partial "completed" result is kept
    assert progress != 100.0


def test_cancelled_handler_result_marks_cancelled(svc):
    tid, row = _running_task(svc, lambda t, p: {"status": "cancelled"})
    svc._process_task_inline(row, "w")
    assert _row(tid)[0] == TaskStatus.CANCELLED.value


def test_a_crash_after_cancel_neither_retries_nor_fails(svc):
    def handler(task_id, payload):
        svc.cancel_task(task_id)
        raise RuntimeError("died while stopping")

    tid, row = _running_task(svc, handler)
    svc._process_task_inline(row, "w")
    assert _row(tid)[0] == TaskStatus.CANCELLED.value      # not QUEUED (retry), not FAILED


def test_pause_ends_paused_and_resume_accepts_it(svc):
    def handler(task_id, payload):
        assert svc.pause_task(task_id)
        return {"status": "paused"}

    tid, row = _running_task(svc, handler)
    svc._process_task_inline(row, "w")
    status, result, progress = _row(tid)
    assert status == TaskStatus.PAUSED.value
    assert progress != 100.0
    assert svc.resume_task(tid) is True
    assert _row(tid)[0] == TaskStatus.QUEUED.value


def test_handler_returning_paused_maps_to_paused_without_pause_task(svc):
    tid, row = _running_task(svc, lambda t, p: {"status": "paused"})
    svc._process_task_inline(row, "w")
    assert _row(tid)[0] == TaskStatus.PAUSED.value
    assert svc.resume_task(tid) is True


def test_pause_requested_but_handler_finishes_keeps_paused(svc):
    def handler(task_id, payload):
        svc.pause_task(task_id)
        return {"status": "completed"}

    tid, row = _running_task(svc, handler)
    svc._process_task_inline(row, "w")
    assert _row(tid)[0] == TaskStatus.PAUSED.value


def test_normal_completion_unchanged(svc):
    tid, row = _running_task(svc, lambda t, p: {"status": "completed", "x": 1})
    svc._process_task_inline(row, "w")
    status, result, progress = _row(tid)
    assert (status, result, progress) == (TaskStatus.COMPLETED.value, {"status": "completed", "x": 1}, 100.0)


def test_failed_and_partial_results_unchanged(svc):
    tid, row = _running_task(svc, lambda t, p: {"status": "failed", "error": "boom"})
    svc._process_task_inline(row, "w")
    assert _row(tid)[0] == TaskStatus.FAILED.value
    tid, row = _running_task(svc, lambda t, p: {"status": "partial"})
    svc._process_task_inline(row, "w")
    assert _row(tid)[0] == TaskStatus.COMPLETED.value


# ------------------------------------------------------------------ the optimization handler
def _opt_env(monkeypatch):
    from tests import test_strategy_optimization_handler as T
    from app.services import strategy_optimization_handler as H

    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(H, "_build_hoisted_state", lambda cfg: {})
    return T, H


def _run_opt_through_queue(monkeypatch, svc, stop_after, how):
    """Run a REAL (serial, stubbed-backtest) GA as a queued task; call ``how`` on the queue after
    ``stop_after`` trials. Returns (trials_run, task_status, optimization_row)."""
    T, H = _opt_env(monkeypatch)
    trials = []

    def stub(cfg, hoisted, decoded):
        trials.append(1)
        if len(trials) == stop_after:
            how(svc, state["tid"])
        return T._deterministic_stub(cfg, hoisted, decoded)

    state = {}
    monkeypatch.setattr(H, "_run_trial_backtest", stub)
    monkeypatch.setattr(H, "get_task_queue", lambda: svc)
    sid = T._seed_strategy()
    opt_id = T._seed_opt(sid, config=T._ga_config(populationSize=8, generations=4))
    tid, row = _running_task(svc, H.handle_strategy_optimization, task_type="strategy_optimization_stop")
    state["tid"] = tid
    db = SessionLocal()
    try:
        r = db.query(TaskQueue).filter(TaskQueue.task_id == tid).first()
        r.payload = {"optimization_id": opt_id}
        db.commit()
        db.refresh(r)
        db.expunge(r)
    finally:
        db.close()
    svc._process_task_inline(r, "w")
    return len(trials), _row(tid)[0], T._load_opt(opt_id), tid


def test_optimization_cancel_stops_at_the_next_check_and_stays_cancelled(monkeypatch, svc):
    n, status, opt, _ = _run_opt_through_queue(
        monkeypatch, svc, 3, lambda q, tid: q.cancel_task(tid))
    assert n <= 4, f"the GA kept running after the cancel ({n} trials of 32)"
    assert status == TaskStatus.CANCELLED.value
    assert opt.status == "cancelled"
    assert opt.progress != 100.0


def test_optimization_pause_ends_paused_and_resumable(monkeypatch, svc):
    n, status, opt, tid = _run_opt_through_queue(
        monkeypatch, svc, 5, lambda q, tid: q.pause_task(tid))
    assert n <= 6
    assert status == TaskStatus.PAUSED.value
    assert opt.status != "completed"
    assert svc.resume_task(tid) is True


def test_optimization_completes_normally_when_nobody_stops_it(monkeypatch, svc):
    n, status, opt, _ = _run_opt_through_queue(monkeypatch, svc, 10 ** 9, lambda q, tid: None)
    assert status == TaskStatus.COMPLETED.value
    assert opt.status == "completed"
