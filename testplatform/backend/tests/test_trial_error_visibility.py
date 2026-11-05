"""A trial that RAISES is still scored as a failure, but never silently (2026-10-24).

BEFORE. ``DistributedEvaluator._consume_local`` turned a raising trial into ``{ok: False,
fitness: 0.0, error: repr(e)}`` with no log line at all; a pool child's own logging is disabled,
``_trial_worker`` returned only ``repr(e)`` (no traceback) and the worker server's ``_job_status``
did the same for a job whose future raised. The master's only trace was one WARNING without the
genome. A defect hitting every trial looked like a poor population.

NOW. The exception type, message and traceback travel in the result dict (so a REMOTE worker's
error reaches the master through ``/job-status``), the local consumer logs at ERROR with the trial
and optimization ids, the handler logs every failed trial at ERROR with the genome key/params and
counts them, and each generation's summary reports the count. Fitness semantics are unchanged.
"""
import logging
import sys
import types
from concurrent.futures import Future

import pytest

import app.services.distributed_eval as de
import app.services.strategy_optimization_handler as h
from app.services.distributed_eval import DistributedEvaluator


def _patch_backtest(monkeypatch, fn):
    mod = types.ModuleType("app.services.backtest.daily_backtest_handler")
    mod.run_daily_backtest = fn
    monkeypatch.setitem(sys.modules, "app.services.backtest.daily_backtest_handler", mod)


def test_trial_worker_failure_carries_type_message_and_traceback(monkeypatch):
    def boom(cfg, progress_cb=None):
        raise ValueError("bad genome 42")

    _patch_backtest(monkeypatch, boom)
    out = h._trial_worker({}, "sharpe_ratio")
    assert out["ok"] is False and out["fitness"] == 0.0          # semantics unchanged
    assert out["error_type"] == "ValueError"
    assert "bad genome 42" in out["error"]
    assert "Traceback" in out["traceback"] and "bad genome 42" in out["traceback"]


class _RaisingPool:
    def submit(self, _fn, config, _metric):
        raise ValueError(f"pool exploded on {config['v']}")


def test_local_consumer_logs_a_raising_trial_and_counts_it(caplog):
    caplog.set_level(logging.ERROR)
    ev = DistributedEvaluator(_RaisingPool(), "sharpe", n_consumers=1, optimization_id="opt-77",
                              workers=[], log=lambda *_: None)
    ev.start()
    try:
        jobs = [(i, {"i": i}, f"k{i}", {"v": i}) for i in range(3)]
        outs = {i: out for i, _f, _k, out in ev.execute_jobs(jobs)}
    finally:
        ev.stop()
    assert all(o["ok"] is False and o["fitness"] == 0.0 for o in outs.values())
    assert all(o["error_type"] == "ValueError" and "Traceback" in o["traceback"]
               for o in outs.values())
    text = "\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)
    assert "ValueError" in text and "pool exploded on 1" in text and "opt-77" in text
    assert any(r.exc_info for r in caplog.records if r.levelno >= logging.ERROR), \
        "the traceback must be logged from inside the except"
    assert ev.trial_errors == 3


def test_worker_server_job_status_returns_the_error_text_and_logs_it(caplog):
    from app import worker_server as ws
    caplog.set_level(logging.ERROR)
    fut = Future()
    fut.set_exception(RuntimeError("worker-side kaboom"))
    with ws._JOBS_LOCK:
        ws._JOBS["jid-err"] = fut
    body = ws._job_status("jid-err")
    res = body["result"]
    assert body["status"] == "done" and res["ok"] is False
    assert res["error_type"] == "RuntimeError" and "worker-side kaboom" in res["error"]
    assert "Traceback" in res["traceback"]
    assert any("worker-side kaboom" in r.getMessage() and r.levelno >= logging.ERROR
               for r in caplog.records)


def test_tally_logs_each_failed_trial_with_the_genome_and_summarises(caplog):
    caplog.set_level(logging.ERROR)
    tally = h._TrialErrorTally()
    out = {"ok": False, "error": "ValueError('x')", "error_type": "ValueError",
           "traceback": "Traceback (most recent call last): ...", "origin": "box1"}
    tally.record(out, "key-abcdef", {"tp": 3, "sl": -2}, gen=4, n_gens=30)
    tally.record(dict(out, error_type="KeyError", error="KeyError('y')"), "key-2", {"tp": 1})
    text = "\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)
    assert "key-abcdef" in text and '"tp": 3' in text and "box1" in text
    assert "ValueError" in text and "Traceback" in text
    assert tally.n == 2
    s = tally.summary(total=10)
    assert "2 of 10" in s and "ValueError" in s and "KeyError" in s
    assert h._TrialErrorTally().summary(total=10) == "0 trial error(s) of 10"


def test_the_handler_wires_the_tally_and_the_generation_summary():
    src = open(h.__file__, encoding="utf-8").read()
    assert "tally.record(" in src and "tally.summary(" in src
