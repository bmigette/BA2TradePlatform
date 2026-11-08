"""Schema 3: the ``decision`` outcome, ``decision_object``, ``discard`` and the new health kinds.

A stock decision pass ends ``decision`` (its frozen summary is the ``decision_object``), ``skip`` (a
decision not to act), ``error`` or is DISCARDED (counted by reason, nothing written). Schema-1 and -2
bundles stay readable.
"""
import json
from datetime import datetime, timezone

import pytest

from ba2_common.core.replay import (
    AnalysisRecord, ReplayStatus, ReplayStore, SessionRecord, load_bundle,
)
from ba2_common.core.replay.context import CaptureHealth, MissingSkipReason, capture_scope
from ba2_common.core.replay.schemas import READABLE_SCHEMA_VERSIONS, SCHEMA_VERSION

NOW = datetime(2026, 11, 8, 15, 0, tzinfo=timezone.utc)


def _meta(**kw):
    meta = {
        "analysis_id": "decision-7-a", "attempt_id": "att-1", "session_id": "s1",
        "expert_class": "FMPRating", "expert_instance_id": 7, "symbol": "",
        "use_case": ReplayStatus.USE_CASE_ENTER_MARKET, "scheduled_at": None, "started_at": NOW,
        "branch_flags": {"record_kind": "decision_pass", "pass": "enter-market", "trigger": "scheduler",
                         "host_timezone": {"name": "Europe/Paris", "utc_offset_seconds": 3600}},
    }
    meta.update(kw)
    return meta


@pytest.fixture
def store(tmp_path):
    s = ReplayStore(tmp_path / "store", writer="sync")
    s.begin_session(SessionRecord(
        session_id="s1", instance_id="i", started_at=NOW, exchange_tz="America/New_York",
        app_version="x", package_versions={}, source_revision="abc", dirty=False))
    yield s
    s.close(timeout=2.0)


def test_schema_is_3_and_one_and_two_stay_readable():
    assert SCHEMA_VERSION == 3 and READABLE_SCHEMA_VERSIONS == (1, 2, 3)


def test_a_decision_outcome_stores_its_summary_as_the_decision_object(store):
    summary = {"pass": "enter-market", "symbols": {"AAPL": {"gates": []}}, "funded": [], "errors": []}
    with capture_scope(store, _meta()) as context:
        context.set_decision(summary)
    (record,) = store.index.analyses("s1")
    assert record.outcome == ReplayStatus.OUTCOME_DECISION == "decision"
    assert record.error is None and record.decision_object is not None
    assert record.decision_object in record.object_hashes()
    assert store.decode_object(record.decision_object) == summary


def test_an_unrecordable_summary_is_an_error_naming_it_never_a_decision_without_object(store, monkeypatch):
    monkeypatch.setattr("ba2_common.core.replay.context.logger.error", lambda *a, **k: None)
    with capture_scope(store, _meta()) as context:
        context.set_decision({"bad": object()})
    (record,) = store.index.analyses("s1")
    assert record.outcome == ReplayStatus.OUTCOME_ERROR
    assert "decision summary not recordable" in record.error
    assert record.decision_object is None and "decision" in record.capture_gaps


def test_discard_writes_nothing_and_counts_the_reason(store):
    with capture_scope(store, _meta()) as context:
        context.discard("lock_busy")
    with capture_scope(store, _meta(analysis_id="b", attempt_id="att-2")) as context:
        context.discard("lock_busy")
    with capture_scope(store, _meta(analysis_id="c", attempt_id="att-3")) as context:
        context.discard("pending_analysis_jobs")
    assert store.index.analyses("s1") == []
    assert store.health.discards() == {"lock_busy": 2, "pending_analysis_jobs": 1}
    assert store.health.count(CaptureHealth.PASS_DISCARDED) == 3
    assert store.health.total == 0


def test_a_discard_needs_a_reason():
    from ba2_common.core.replay.context import CaptureContext

    context = CaptureContext(analysis_meta=_meta(), health=CaptureHealth())
    with pytest.raises(MissingSkipReason):
        context.discard("  ")


def test_a_discard_never_hides_an_outcome_the_pass_already_has(store):
    with capture_scope(store, _meta()) as context:
        context.set_skip("no ruleset")
        context.discard("lock_busy")
    (record,) = store.index.analyses("s1")
    assert record.outcome == ReplayStatus.OUTCOME_SKIP
    assert store.health.discards() == {}


def test_a_discarded_pass_that_then_raises_is_still_written_as_the_error(store):
    with pytest.raises(RuntimeError):
        with capture_scope(store, _meta()) as context:
            context.discard("lock_busy")
            raise RuntimeError("boom")
    (record,) = store.index.analyses("s1")
    assert record.outcome == ReplayStatus.OUTCOME_ERROR


def test_new_health_kinds_are_failures_except_discards():
    health = CaptureHealth()
    health.record(CaptureHealth.PASS_NOT_RECORDED)
    health.record(CaptureHealth.STEP_NOT_RECORDED)
    assert health.total == 2
    health.record_discard("lock_busy")
    assert health.total == 2 and health.pass_discarded == 1
    assert health.as_dict()["pass_not_recorded"] == 1 and health.as_dict()["step_not_recorded"] == 1


def test_a_step_failure_is_counted_under_its_own_kind_and_on_the_record(store, monkeypatch):
    monkeypatch.setattr("ba2_common.core.replay.context.logger.error", lambda *a, **k: None)
    with capture_scope(store, _meta()) as context:
        context.note_failure("rules step not recorded", ValueError("x"), CaptureHealth.STEP_NOT_RECORDED)
        context.set_decision({"ok": True})
    (record,) = store.index.analyses("s1")
    assert record.capture_failures == {"step_not_recorded": 1}
    assert store.health.count(CaptureHealth.STEP_NOT_RECORDED) == 1


def test_a_schema_2_record_without_decision_object_loads(store):
    with capture_scope(store, _meta(analysis_id="old")) as context:
        context.set_skip("whatever")
    (record,) = store.index.analyses("s1")
    mapping = record.to_mapping()
    del mapping["decision_object"]
    mapping["schema_version"] = 2
    loaded = AnalysisRecord.from_mapping(mapping)
    assert loaded.decision_object is None and loaded.schema_version == 2


def test_an_exported_schema_3_bundle_with_a_decision_loads_and_a_v2_manifest_is_still_accepted(store, tmp_path):
    with capture_scope(store, _meta()) as context:
        context.set_decision({"pass": "enter-market"})
    out = store.export_session("s1", tmp_path / "export")
    bundle = load_bundle(out)
    (analysis,) = bundle.analyses
    assert analysis.outcome == "decision" and bundle.decode(analysis.decision_object) == {"pass": "enter-market"}
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    manifest["schema_version"] = 2
    for entry in manifest["analyses"]:
        entry.pop("decision_object", None)
        entry["schema_version"] = 2
        entry["outcome"] = "skip"
        entry["skip_reason"] = "old"
    (out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert load_bundle(out).analyses[0].decision_object is None


def test_the_recorded_timezone_comes_from_the_pass_meta():
    from zoneinfo import ZoneInfo

    from ba2_common.core.replay.context import CaptureContext

    context = CaptureContext(analysis_meta=_meta(), health=CaptureHealth())
    assert context.recorded_timezone() == ZoneInfo("Europe/Paris")
    bare = CaptureContext(analysis_meta=_meta(branch_flags={}), health=CaptureHealth())
    assert bare.recorded_timezone() is None
