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


def _exit_meta():
    meta = _meta()
    meta["branch_flags"] = {**meta["branch_flags"], "pass": "open-positions"}
    return meta


def test_the_exit_pass_has_its_own_small_budget():
    """An exit pass guards open positions: it may spend EXIT_PASS_RECORD_SECONDS_BUDGET, not the 30 s of an entry pass."""
    assert dr.EXIT_PASS_RECORD_SECONDS_BUDGET <= 5.0 < dr.PASS_RECORD_SECONDS_BUDGET
    spent = dr.EXIT_PASS_RECORD_SECONDS_BUDGET + 0.5
    store = _Store()
    with capture_scope(store, _exit_meta()) as context:
        dr._add_record_seconds(context, spent)
        _good_step("A")
        summary = dr.pass_summary(context)
        context.set_skip("t")
    assert _steps(store, "rules") == [] and "seconds" in summary["recording_budget_exceeded"]
    store = _Store()
    with capture_scope(store, _meta()) as context:           # the same spend is fine in an entry pass
        dr._add_record_seconds(context, spent)
        _good_step("A")
        context.set_skip("t")
    assert len(_steps(store, "rules")) == 1


def test_pass_state_is_under_the_budget_too():
    store = _Store()
    with capture_scope(store, _exit_meta()) as context:
        dr._add_record_seconds(context, 60.0)
        assert dr.record_pass_state(expert=None, expert_instance=None, pass_name="open-positions",
                                    lookback_days=1, cutoff_time=None, decision_time=None, account=None,
                                    ruleset_id=None, fetched_recommendations=[],
                                    selected_recommendations=[]) is None
        summary = dr.pass_summary(context)
        context.set_skip("t")
    assert [e["kind"] for e in summary["steps_not_recorded"]] == [dr.KIND_PASS_STATE]
    assert "budget" in summary["steps_not_recorded"][0]["error"]
    assert store.health.count(CaptureHealth.STEP_NOT_RECORDED) == 1


def test_the_scope_close_submit_is_counted_in_the_recorder_time(monkeypatch):
    import time as _time
    errors = []
    monkeypatch.setattr("ba2_common.core.replay.context.logger.error", lambda msg, *a, **k: errors.append(msg))
    store = _Store()
    real_submit = store.submit

    def slow_submit(*args, **kwargs):
        _time.sleep(0.05)
        return real_submit(*args, **kwargs)

    store.submit = slow_submit
    monkeypatch.setattr(dr, "EXIT_PASS_RECORD_SECONDS_BUDGET", 0.01)
    with capture_scope(store, _exit_meta()) as context:
        _good_step("A")
        before = context.decision_ledger["record_seconds"]
        context.set_skip("t")
    assert context.decision_ledger["record_seconds"] >= before + 0.04
    assert any("submit" in m and "budget" in m for m in errors)
