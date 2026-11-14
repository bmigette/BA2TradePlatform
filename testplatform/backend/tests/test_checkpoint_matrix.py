"""What a starting run does with the GA checkpoint stored under its job name, cell by cell, with the
REAL handler, checkpoint store and resume service (only the backtest is a deterministic stub).

| launch \\ checkpoint      | none   | own                         | orphan (writer gone) | another LIVE run's |
|--------------------------|--------|-----------------------------|----------------------|--------------------|
| fresh (no other row)     | gen 0  | resume (retry of this row)  | retire, gen 0        | refuse, untouched  |
| plain relaunch           | gen 0  | prior row terminal: resume; | retire, gen 0        | refuse, untouched  |
|                          |        | still running: refuse (the  |                      |                    |
|                          |        | launcher claims it instead) |                      |                    |
| --resume-interrupted     | replay | resume (strict)             | retire, replay       | refuse, untouched  |

"gen 0" / "replay": the search starts at generation 0; a replay re-reads the dead row's finished
trials from the memo, so only the rest runs. "retire": the checkpoint's payload moves to a task row
of its own and the new row notes it (``discardedCheckpoints``); nothing is deleted. Every completed
cell ends with the uninterrupted job's result.
"""
from __future__ import annotations

import os

import pytest

from app.models.database import SessionLocal
from app.models.strategy_optimization import StrategyOptimization, StrategyOptimizationRunner
from app.models.task_queue import TaskQueue
from app.services import optimization_resume as R
from app.services import strategy_optimization_handler as H

from .test_optimization_resume_handler import (  # noqa: F401  (fixtures)
    _config, _kill, _launch_resume, _make_dead, _new_row, _result, _run, _seed_wide_strategy,
    _uniq, stubbed, whole)

pytest.importorskip("deap")


@pytest.fixture(scope="module", autouse=True)
def _schema():
    from app.models.database import Base, engine
    Base.metadata.create_all(bind=engine)


def _ckpt(name):
    return H._load_checkpoint(H.checkpoint_task_id(name, 0))


def _make_live(opt_id):
    """The row's run is LIVE: its recorded runner is this (running) process, handler not ended."""
    me = R.process_identity()
    db = SessionLocal()
    try:
        run = db.get(StrategyOptimizationRunner, opt_id)
        run.host, run.pid, run.pid_started, run.proc_identity = \
            me["host"], me["pid"], me["pid_started"], me["proc_identity"]
        run.ended_at = None
        db.commit()
    finally:
        db.close()


def _rename(opt_id, name):
    db = SessionLocal()
    try:
        db.get(StrategyOptimization, opt_id).name = name
        db.commit()
    finally:
        db.close()


def _delete(opt_id):
    db = SessionLocal()
    try:
        db.delete(db.get(StrategyOptimizationRunner, opt_id))
        db.delete(db.get(StrategyOptimization, opt_id))
        db.commit()
    finally:
        db.close()


def _discarded(opt_id):
    db = SessionLocal()
    try:
        notes = (db.get(StrategyOptimization, opt_id).optimization_config or {}).get(
            "discardedCheckpoints") or []
        kept = [db.query(TaskQueue.checkpoint_data).filter(TaskQueue.task_id == n["retired_as"])
                .scalar() for n in notes]
        return notes, kept
    finally:
        db.close()


def _go(opt_id, caplog, payload=None):
    calls = []
    caplog.clear()
    caplog.set_level("WARNING")
    out = _run(opt_id, calls, payload=payload)
    return out, len(calls), "RESUMING" in caplog.text


def _orphan_checkpoint(name):
    """A checkpoint of ``name`` whose writing row was deleted by hand."""
    older, _ = _kill(name, 18)
    payload = _ckpt(name)
    _delete(older)
    return older, payload


def _live_checkpoint(name):
    """A checkpoint of ``name`` written by a row that is still running (renamed, so the job name is
    free for the launch under test)."""
    writer, _ = _kill(name, 18)
    _rename(writer, name + "-elsewhere")
    _make_live(writer)
    return writer, _ckpt(name)


# ------------------------------------------------------------------ fresh start (no other row)


def test_fresh_none_starts_at_generation_0(whole, caplog):
    want, n = whole
    oid = _new_row(_seed_wide_strategy(), _uniq("mx-f-none"), _config())
    out, calls, resumed = _go(oid, caplog)
    assert out["status"] == "completed" and not resumed and calls == n
    assert _result(oid) == {**want, "status": "completed"}


def test_fresh_own_a_retry_of_the_same_row_resumes_its_checkpoint(whole, caplog):
    want, n = whole
    name = _uniq("mx-f-own")
    oid = _new_row(_seed_wide_strategy(), name, _config())
    first = []
    with pytest.raises(BaseException):           # the stub kills it at trial 18 ...
        _run(oid, first, kill_at=18)
    done = len(first) - 1
    out, calls, resumed = _go(oid, caplog)       # ... and the SAME process retries the SAME row
    assert R.runner_of(SessionLocal(), oid).resumed_from_id is None
    assert out["status"] == "completed" and resumed and done + calls == n
    assert _result(oid) == {**want, "status": "completed"}


def test_fresh_orphan_is_retired_and_starts_at_generation_0(whole, caplog):
    want, n = whole
    name = _uniq("mx-f-orphan")
    older, payload = _orphan_checkpoint(name)
    oid = _new_row(_seed_wide_strategy(), name, _config())
    out, calls, resumed = _go(oid, caplog)
    assert out["status"] == "completed" and not resumed and calls == n
    notes, kept = _discarded(oid)
    assert [x["written_by"] for x in notes] == [older] and "orphaned" in notes[0]["why"]
    assert kept == [payload]                                       # retired, not deleted
    assert _result(oid) == {**want, "status": "completed"}


def test_fresh_live_writer_is_refused_and_its_checkpoint_untouched(stubbed, caplog):
    name = _uniq("mx-f-live")
    writer, payload = _live_checkpoint(name)
    oid = _new_row(_seed_wide_strategy(), name, _config())
    out, calls, _ = _go(oid, caplog)
    assert out["status"] == "failed" and "LIVE" in out["error"] and calls == 0
    assert _ckpt(name) == payload


# ------------------------------------------------------------------ plain relaunch (another row exists)


def test_relaunch_none_starts_at_generation_0(whole, caplog):
    want, n = whole
    name = _uniq("mx-r-none")
    _kill(name, 3)                               # killed before its first checkpoint
    oid = _new_row(_seed_wide_strategy(), name, _config())
    out, calls, resumed = _go(oid, caplog)
    assert out["status"] == "completed" and not resumed
    assert _result(oid)["best_fitness"] == want["best_fitness"]


def test_relaunch_own_of_an_unclaimed_dead_row_is_refused_and_untouched(stubbed, caplog):
    """The prior run is dead but its row is still `running`: continuing its checkpoint from a new
    row would leave two non-terminal rows of one name. `ba2-test optimize` claims it instead
    (test_resume_third_review.py); the bare handler refuses."""
    name = _uniq("mx-r-own-open")
    prior, _ = _kill(name, 18)
    payload = _ckpt(name)
    oid = _new_row(_seed_wide_strategy(), name, _config())
    out, calls, _ = _go(oid, caplog)
    assert out["status"] == "failed" and "claims the row" in out["error"] and calls == 0
    assert _ckpt(name) == payload


def test_relaunch_own_of_a_terminal_prior_run_resumes_with_its_full_record(whole, caplog):
    """A plain retry of a FAILED job (the grids' rule: not completed -> run again) continues its
    checkpoint with all of its trials."""
    want, n = whole
    name = _uniq("mx-r-own")
    prior, done = _kill(name, 18)
    db = SessionLocal()
    try:
        db.get(StrategyOptimization, prior).status = "failed"
        db.commit()
    finally:
        db.close()
    oid = _new_row(_seed_wide_strategy(), name, _config())
    out, calls, resumed = _go(oid, caplog)
    assert out["status"] == "completed" and resumed and done + calls == n
    assert _result(oid) == {**want, "status": "completed"}


def test_relaunch_orphan_is_retired_and_starts_at_generation_0(whole, caplog):
    want, n = whole
    name = _uniq("mx-r-orphan")
    _kill(name, 3)                               # another row of the name, no checkpoint
    older, payload = _orphan_checkpoint(name)
    oid = _new_row(_seed_wide_strategy(), name, _config())
    out, calls, resumed = _go(oid, caplog)
    assert out["status"] == "completed" and not resumed
    notes, kept = _discarded(oid)
    assert [x["written_by"] for x in notes] == [older] and kept == [payload]


def test_relaunch_live_prior_run_is_refused_and_its_checkpoint_untouched(stubbed, caplog):
    name = _uniq("mx-r-live")
    prior, _ = _kill(name, 18)
    _make_live(prior)
    payload = _ckpt(name)
    oid = _new_row(_seed_wide_strategy(), name, _config())
    out, calls, _ = _go(oid, caplog)
    assert out["status"] == "failed" and "LIVE" in out["error"] and calls == 0
    assert _ckpt(name) == payload


# ------------------------------------------------------------------ --resume-interrupted


def test_resume_none_replays_from_the_memo(whole, caplog):
    want, n = whole
    name = _uniq("mx-x-none")
    killed, done = _kill(name, 3)
    _, oid = _launch_resume(name)
    out, calls, resumed = _go(oid, caplog, {"resume_of": killed})
    assert out["status"] == "completed" and not resumed and done + calls == n
    assert _result(oid) == {**want, "status": "completed"}


def test_resume_own_resumes_strictly(whole, caplog):
    want, n = whole
    name = _uniq("mx-x-own")
    killed, done = _kill(name, 18)
    _, oid = _launch_resume(name)
    out, calls, resumed = _go(oid, caplog, {"resume_of": killed})
    assert out["status"] == "completed" and resumed and done + calls == n
    assert _result(oid) == {**want, "status": "completed"}


def test_resume_orphan_is_retired_and_replays(whole, caplog):
    want, n = whole
    name = _uniq("mx-x-orphan")
    killed, done = _kill(name, 3)
    _rename(killed, name + "-hold")              # out of the way while the orphan is made
    older, payload = _orphan_checkpoint(name)
    _rename(killed, name)
    _, oid = _launch_resume(name)
    out, calls, resumed = _go(oid, caplog, {"resume_of": killed})
    assert out["status"] == "completed" and not resumed and done + calls == n
    notes, kept = _discarded(oid)
    assert [x["written_by"] for x in notes] == [older] and kept == [payload]
    assert _result(oid) == {**want, "status": "completed"}


def test_resume_live_writer_is_refused_and_its_checkpoint_untouched(stubbed, caplog):
    name = _uniq("mx-x-live")
    killed, _ = _kill(name, 3)
    _rename(killed, name + "-hold")
    writer, payload = _live_checkpoint(name)
    _rename(killed, name)
    _, oid = _launch_resume(name)
    out, calls, _ = _go(oid, caplog, {"resume_of": killed})
    assert out["status"] == "failed" and "LIVE" in out["error"] and calls == 0
    assert _ckpt(name) == payload
