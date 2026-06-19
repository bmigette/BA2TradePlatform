"""Backtest perf: ``activity_logging_disabled()`` makes ``log_activity`` a no-op so a
backtest doesn't churn the ActivityLog queue/DB on every actionable bar. The LIVE path
(no context) keeps logging. Also guards the atexit teardown logger.
"""
import types

from ba2_common.core import db


class _SpyQueue:
    def __init__(self):
        self.items = []

    def put(self, item, timeout=None):
        self.items.append(item)


def _patch_queue(monkeypatch):
    spy = _SpyQueue()
    monkeypatch.setattr(db, "_activity_log_queue", spy)
    monkeypatch.setattr(db, "_start_activity_log_worker", lambda: None)
    # pretend a worker is alive so log_activity doesn't try to start one
    monkeypatch.setattr(db, "_activity_log_thread", types.SimpleNamespace(is_alive=lambda: True))
    return spy


def test_log_activity_queues_when_enabled(monkeypatch):
    spy = _patch_queue(monkeypatch)
    db.log_activity("SEV", "TYPE", "desc")
    assert len(spy.items) == 1


def test_log_activity_noop_when_disabled(monkeypatch):
    spy = _patch_queue(monkeypatch)
    with db.activity_logging_disabled():
        db.log_activity("SEV", "TYPE", "desc")
        db.log_activity("SEV", "TYPE", "desc2")
    assert spy.items == []  # nothing queued while disabled


def test_disabled_flag_restored_after_context(monkeypatch):
    spy = _patch_queue(monkeypatch)
    with db.activity_logging_disabled():
        pass
    db.log_activity("SEV", "TYPE", "desc")
    assert len(spy.items) == 1  # logging resumes after the context


def test_nested_disable_restores_prior_state(monkeypatch):
    spy = _patch_queue(monkeypatch)
    with db.activity_logging_disabled():
        with db.activity_logging_disabled():
            db.log_activity("SEV", "TYPE", "inner")
        # still disabled after inner exits (prior state was disabled)
        db.log_activity("SEV", "TYPE", "outer")
    assert spy.items == []
