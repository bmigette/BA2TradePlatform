"""Decision-pass drops/gaps in the replay service (I2: never block the pass thread, I7: capability)."""
import sqlite3
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from ba2_common.core.replay.context import CaptureHealth, capture_scope, current_capture
from ba2_common.core.replay.decision_record import pass_summary
from ba2_common.core.replay.observe import record_observation
from ba2_common.core.replay.schemas import ReplayStatus, SessionRecord
from ba2_common.core.replay.service import ReplayStore
from tests._account_tap_doubles import RecordingStore

UTC_NOW = datetime(2026, 11, 8, 14, 0, tzinfo=timezone.utc)


def _session():
    return SessionRecord(
        session_id="s1", instance_id="i", started_at=UTC_NOW, ended_at=None,
        exchange_tz="America/New_York", app_version="x", package_versions={}, source_revision="abc",
        dirty=False, config_hashes={}, status=ReplayStatus.SESSION_OPEN, capabilities={})


def _pass_submission(with_step=True):
    """(record, objects, observations) of one finished decision pass, built by the real scope."""
    meta = {
        "analysis_id": "decision-7-x", "attempt_id": "att", "session_id": "s1",
        "expert_class": "FMPRating", "expert_instance_id": 7, "symbol": "",
        "use_case": ReplayStatus.USE_CASE_ENTER_MARKET, "scheduled_at": None, "started_at": UTC_NOW,
        "branch_flags": {"record_kind": "decision_pass", "pass": "enter-market", "trigger": "scheduler"},
    }
    recorder = RecordingStore()
    with capture_scope(recorder, meta) as context:
        if with_step:
            oid = record_observation(provider="decision_step", method="rules",
                                     identity={"kind": "rules", "symbol": "AAPL"}, payload={"x": 1})
            context.decision_ledger.update({
                "symbols": {}, "steps": [{"kind": "rules", "symbol": "AAPL", "observation_id": oid}],
                "steps_not_recorded": [], "errors": [], "effects_outside_steps": []})
        context.set_decision(pass_summary(context))
    return recorder.submitted[0]


@pytest.fixture
def store(tmp_path):
    s = ReplayStore(tmp_path / "store", writer="sync")
    s.begin_session(_session())
    yield s
    s.close(timeout=2.0)


def test_a_decision_pass_gap_is_recorded_under_the_decision_capability(store):
    record, objects, observations = _pass_submission()
    bad = [replace(o, payload=object()) for o in observations]       # the codec refuses it
    store.submit(record, objects, bad)
    rows = store.index.coverage("s1")
    assert [r.capability for r in rows] == [ReplayStatus.CAPABILITY_DECISION]
    assert rows[0].status == ReplayStatus.COVERAGE_MISSING_CAPTURE


def test_a_step_payload_gap_is_listed_in_the_pass_summary_steps_not_recorded(store):
    record, objects, observations = _pass_submission()
    bad = [replace(o, payload=object()) for o in observations]
    store.submit(record, objects, bad)
    (stored,) = store.index.analyses("s1")
    summary = store.decode_object(stored.decision_object)
    (gap,) = summary["steps_not_recorded"]
    assert gap["kind"] == "rules" and gap["symbol"] == "AAPL" and "not representable" in gap["error"]


def test_a_dropped_decision_pass_is_recorded_under_the_decision_capability(tmp_path):
    s = ReplayStore(tmp_path / "store", writer="thread", queue_maxsize=1)
    s.begin_session(_session())
    release = threading.Event()
    real = s._write_pending
    s._write_pending = lambda pending: (release.wait(10), real(pending))[1]
    record, objects, observations = _pass_submission(with_step=False)
    try:
        for n in range(3):                       # 1 in the writer, 1 queued, 1 dropped
            s.submit(replace(record, analysis_id=f"decision-7-{n}"), objects, observations)
        release.set()
        assert s.drain(timeout=10) == 0
        dropped = [r for r in s.index.coverage("s1") if "saturated" in r.detail]
        assert [r.capability for r in dropped] == [ReplayStatus.CAPABILITY_DECISION]
    finally:
        release.set()
        s.close(timeout=5.0)


def test_a_full_queue_never_blocks_the_pass_thread_on_the_index(tmp_path):
    """The drop path used to write coverage synchronously (BEGIN IMMEDIATE, busy_timeout 30 s)."""
    s = ReplayStore(tmp_path / "store", writer="thread", queue_maxsize=1)
    s.begin_session(_session())
    release = threading.Event()
    real = s._write_pending
    s._write_pending = lambda pending: (release.wait(30), real(pending))[1]
    record, objects, observations = _pass_submission(with_step=False)
    holder = sqlite3.connect(str(tmp_path / "store" / "index.sqlite"), timeout=1)
    holder.isolation_level = None
    holder.execute("BEGIN IMMEDIATE")                # somebody else holds the index write lock
    try:
        s.submit(replace(record, analysis_id="decision-7-a"), objects, observations)
        s.submit(replace(record, analysis_id="decision-7-b"), objects, observations)
        started = time.monotonic()
        s.submit(replace(record, analysis_id="decision-7-c"), objects, observations)   # queue full
        elapsed = time.monotonic() - started
        assert elapsed < 1.0, f"the pass thread waited {elapsed:.1f}s on the index"
        assert s.health.count(CaptureHealth.QUEUE_SATURATION) == 1
    finally:
        holder.execute("ROLLBACK")
        holder.close()
        release.set()
        s.drain(timeout=10)
        # the writer flushed the drop once the lock was free: the gap is visible in coverage
        assert any("saturated" in r.detail for r in s.index.coverage("s1"))
        s.close(timeout=5.0)
