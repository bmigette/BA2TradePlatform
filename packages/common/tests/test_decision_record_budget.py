"""I3: a per-pass recording budget. After too much recorder time or too many recorder failures in ONE pass
the rest of its steps are not recorded: counted (STEP_NOT_RECORDED) and said in the pass summary. Never
affects the decision."""
import pytest

from ba2_common.core.replay import decision_record as dr
from ba2_common.core.replay.context import CaptureHealth, capture_scope
from tests.test_decision_record import _Evaluator, _Store, _meta, _rec, _steps
from tests.test_max_loss_persisted_at_submit import _own_db  # noqa: F401  (autouse DB)


def _good_step(symbol):
    with dr.record_rules(_Evaluator(), symbol, _rec(symbol), 9, None) as step:
        step.result = []


def _broken_step(symbol):
    broken = _Evaluator()
    broken.existing_transactions = None
    with dr.record_rules(broken, symbol, _rec(symbol), 9, None) as step:
        step.result = []


def test_the_failure_budget_stops_recording_further_steps(monkeypatch):
    monkeypatch.setattr("ba2_common.core.replay.context.logger.error", lambda *a, **k: None)
    monkeypatch.setattr(dr, "PASS_RECORD_FAILURE_BUDGET", 2)
    store = _Store()
    with capture_scope(store, _meta()) as context:
        _broken_step("A")
        _broken_step("B")                      # 2 failures: the budget is spent
        _good_step("C")                        # would have recorded; must not
        summary = dr.pass_summary(context)
        context.set_skip("t")
    assert _steps(store, "rules") == []
    skipped = [e for e in summary["steps_not_recorded"] if e["symbol"] == "C"]
    assert len(skipped) == 1 and "budget" in skipped[0]["error"]
    assert "budget" in summary["recording_budget_exceeded"]
    assert store.health.count(CaptureHealth.STEP_NOT_RECORDED) == 3      # A, B and the skipped C


def test_the_time_budget_stops_recording_further_steps(monkeypatch):
    monkeypatch.setattr(dr, "PASS_RECORD_SECONDS_BUDGET", 1.0)
    store = _Store()
    with capture_scope(store, _meta()) as context:
        _good_step("A")
        dr._add_record_seconds(context, 5.0)   # the recorder has spent more than its budget
        _good_step("B")
        summary = dr.pass_summary(context)
        context.set_skip("t")
    recorded = [o.observation.request_identity["symbol"] for o, _ in _steps(store, "rules")]
    assert recorded == ["A"]
    assert "seconds" in summary["recording_budget_exceeded"]
    assert [e["symbol"] for e in summary["steps_not_recorded"]] == ["B"]
    assert store.health.count(CaptureHealth.STEP_NOT_RECORDED) == 1


def test_within_budget_nothing_changes():
    store = _Store()
    with capture_scope(store, _meta()) as context:
        _good_step("A")
        _good_step("B")
        summary = dr.pass_summary(context)
        context.set_skip("t")
    assert len(_steps(store, "rules")) == 2
    assert summary["recording_budget_exceeded"] is None and summary["steps_not_recorded"] == []


def test_a_spent_budget_is_not_a_decision_input(monkeypatch):
    """The recorder hands the step body the same result whether or not it records."""
    monkeypatch.setattr(dr, "PASS_RECORD_SECONDS_BUDGET", 0.0)
    store = _Store()
    with capture_scope(store, _meta()) as context:
        dr._add_record_seconds(context, 1.0)
        with dr.record_rules(_Evaluator(), "A", _rec("A"), 9, None) as step:
            step.result = ["x"]                # assignment on the no-op handle is harmless
        context.set_skip("t")
