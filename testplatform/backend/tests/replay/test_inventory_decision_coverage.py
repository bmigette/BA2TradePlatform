"""``replay inventory`` states decision-pass COVERAGE per expert, and a red header when capture failed.

Per expert: passes recorded (a decision, or a recorded decision not to act), discarded (counted by the
live recorder, written into the session's ``capabilities.capture_health`` at finalize), not recorded
(the pass ran, the scope could not open) and failed (the pass ended in an error). A session whose recorder
counted any failure opens its inventory with a loud banner; a clean session does not.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ba2_common.core.replay import ReplayStatus, ReplayStore, SessionRecord
from ba2_common.core.replay.context import CaptureHealth
from ba2_common.core.replay.schemas import AnalysisRecord

from app.services.replay import inventory

NOW = datetime(2026, 11, 8, 15, 0, tzinfo=timezone.utc)


def _pass(n, expert, outcome, *, error=None, skip_reason=None, failures=None):
    return AnalysisRecord(
        analysis_id=f"decision-{n}", attempt_id=f"a{n}", session_id="s1", expert_class=expert,
        expert_instance_id=n, symbol="", use_case="enter_market", started_at=NOW, finished_at=NOW,
        outcome=outcome, error=error, skip_reason=skip_reason,
        branch_flags={"record_kind": "decision_pass", "pass": "enter-market", "trigger": "scheduler"},
        capture_failures=failures or {})


def _export(tmp_path, passes, health_events=()):
    store = ReplayStore(tmp_path / "store", writer="sync")
    store.begin_session(SessionRecord(
        session_id="s1", instance_id="i", started_at=NOW, exchange_tz="America/New_York",
        app_version="x", package_versions={}, source_revision="abc", dirty=False))
    try:
        for analysis in passes:
            store.submit(analysis)
        for event in health_events:
            event(store.health)
        store.finalize_session("s1")
        return store.export_session("s1", tmp_path / "bundle")
    finally:
        store.close(timeout=2.0)


PASSES = [
    _pass(1, "FMPRating", ReplayStatus.OUTCOME_DECISION),
    _pass(2, "FMPRating", ReplayStatus.OUTCOME_DECISION),
    _pass(3, "FMPRating", ReplayStatus.OUTCOME_SKIP, skip_reason="automation off"),
    _pass(4, "FMPRating", ReplayStatus.OUTCOME_ERROR, error="boom"),
    _pass(5, "EarningsDrift", ReplayStatus.OUTCOME_ERROR, error="pass not recorded: KeyError: 'x'",
          failures={CaptureHealth.PASS_NOT_RECORDED: 1}),
    _pass(6, "EarningsDrift", ReplayStatus.OUTCOME_DECISION),
]


def test_decision_coverage_is_counted_per_expert(tmp_path):
    bundle = _export(tmp_path, PASSES, [
        lambda h: h.record_discard("lock_miss", "FMPRating"),
        lambda h: h.record_discard("lock_miss", "FMPRating"),
        lambda h: h.record_discard("pending_jobs", "EarningsDrift"),
        lambda h: h.record(CaptureHealth.PASS_NOT_RECORDED),
    ])

    coverage = inventory.run(bundle)["decision_coverage"]

    assert coverage["by_expert"] == {
        "EarningsDrift": {"recorded": 1, "discarded": 1, "not_recorded": 1, "failed": 0},
        "FMPRating": {"recorded": 3, "discarded": 2, "not_recorded": 0, "failed": 1},
    }
    assert coverage["totals"] == {"recorded": 4, "discarded": 3, "not_recorded": 1, "failed": 1}
    assert coverage["discards_by_reason"] == {"lock_miss": 2, "pending_jobs": 1}
    assert coverage["health_recorded"] is True
    assert coverage["capture_failures"] == 1


def test_a_session_whose_recorder_counted_a_failure_opens_with_a_banner(tmp_path):
    bundle = _export(tmp_path, PASSES, [lambda h: h.record(CaptureHealth.STEP_NOT_RECORDED)])
    markdown = inventory.to_markdown(inventory.run(bundle))
    assert markdown.splitlines()[0].startswith("**CAPTURE FAILURES")
    assert "step_not_recorded" in markdown.splitlines()[0]
    assert "## Decision coverage" in markdown and "| FMPRating |" in markdown


def test_a_clean_session_has_no_banner_and_discards_alone_are_not_a_failure(tmp_path):
    bundle = _export(tmp_path, PASSES[:1], [lambda h: h.record_discard("lock_miss", "FMPRating")])
    inventory_ = inventory.run(bundle)
    assert inventory_["decision_coverage"]["capture_failures"] == 0
    assert not inventory.to_markdown(inventory_).startswith("**CAPTURE FAILURES")


def test_a_session_without_recorded_health_says_so_instead_of_reading_as_clean(tmp_path):
    bundle = _export(tmp_path, PASSES[:1])
    import json
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["session"]["capabilities"]["capture_health"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    coverage = inventory.run(bundle)["decision_coverage"]

    assert coverage["health_recorded"] is False
    assert coverage["by_expert"]["FMPRating"]["discarded"] is None       # unknown, not zero
    assert "no capture_health" in inventory.to_markdown(inventory.run(bundle))
