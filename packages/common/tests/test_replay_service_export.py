"""Task 1 (spec step 1): the ReplayStore service, its writer and bundle export.

Recording is asynchronous after an immutable copy is made; on queue saturation
the record is dropped with a visible health error and an explicit
`missing_capture` coverage row -- never a silent gap and never an exception into
the trading path. Export/load is content-hash verified: a tampered object is
refused.
"""
import json
import threading
import time
from datetime import datetime, timezone

import pandas as pd
import pytest

from ba2_common.core.replay.context import CaptureHealth, capture_scope
from ba2_common.core.replay.schemas import (
    SCHEMA_VERSION,
    AnalysisRecord,
    ReplayStatus,
    SessionRecord,
)
from ba2_common.core.replay.service import (
    ReplayStore,
    get_replay_store,
    load_bundle,
    set_replay_store,
)
from ba2_common.core.replay.store import ObjectHashMismatch

UTC_NOW = datetime(2026, 9, 10, 13, 0, tzinfo=timezone.utc)


def _session(session_id="s1"):
    return SessionRecord(
        session_id=session_id,
        instance_id="inst-abc",
        started_at=UTC_NOW,
        ended_at=None,
        exchange_tz="America/New_York",
        app_version="2026.09.01151",
        package_versions={"ba2_common": "0.1.0"},
        source_revision="deadbeef",
        dirty=False,
        config_hashes={"cache_root": "aa11"},
        status=ReplayStatus.SESSION_OPEN,
        capabilities={"recorded_expert": True},
    )


def _meta(analysis_id="a1", session_id="s1", **kwargs):
    meta = {
        "analysis_id": analysis_id,
        "attempt_id": "att-1",
        "session_id": session_id,
        "expert_class": "DeterministicScorer",
        "expert_instance_id": 12,
        "symbol": "AAPL",
        "use_case": ReplayStatus.USE_CASE_ENTER_MARKET,
        "scheduled_at": UTC_NOW,
        "started_at": UTC_NOW,
    }
    meta.update(kwargs)
    return meta


def _frame():
    idx = pd.DatetimeIndex(["2026-09-08", "2026-09-09"], tz="America/New_York", name="ts")
    return pd.DataFrame({"close": [1.5, 2.5], "volume": [10, 20]}, index=idx)


def _record_one_analysis(store, analysis_id="a1", session_id="s1"):
    with capture_scope(store, _meta(analysis_id, session_id, settings={"threshold": 0.5})) as ctx:
        ctx.set_bundle({"symbol": "AAPL", "ohlcv": _frame(), "current_price": 231.5})
        ctx.record_observation(
            provider="fmp",
            method="price_target_consensus",
            request_identity={"symbol": "AAPL"},
            payload=[{"targetHigh": 260.0}],
            provenance=ReplayStatus.PROVENANCE_NETWORK,
        )
        ctx.set_outcome(recommendation={"signal": "BUY", "confidence": 78.1})


# --------------------------------------------------------------------------- seam


def test_the_store_seam_defaults_to_off():
    assert get_replay_store() is None
    sentinel = object()
    set_replay_store(sentinel)
    try:
        assert get_replay_store() is sentinel
    finally:
        set_replay_store(None)
    assert get_replay_store() is None


# --------------------------------------------------------------------------- sync writer


def test_sync_writer_persists_record_objects_and_observations(tmp_path):
    store = ReplayStore(tmp_path, writer="sync")
    store.begin_session(_session())
    _record_one_analysis(store)
    store.finalize_session("s1")

    analyses = store.index.analyses("s1")
    assert len(analyses) == 1
    record = analyses[0]
    assert record.bundle_capture_status == ReplayStatus.CAPTURE_CAPTURED
    assert record.outcome == ReplayStatus.OUTCOME_RECOMMENDATION
    assert record.bundle_object and record.settings_object and record.recommendation_object
    assert record.settings_hash == record.settings_object

    bundle = store.decode_object(record.bundle_object)
    assert bundle["current_price"] == 231.5
    pd.testing.assert_frame_equal(bundle["ohlcv"], _frame())

    observations = store.index.observations("a1")
    assert len(observations) == 1
    assert observations[0].content_hash == observations[0].payload_object
    assert store.decode_object(observations[0].payload_object) == [{"targetHigh": 260.0}]

    sessions = {s.session_id: s for s in store.index.list_sessions()}
    assert sessions["s1"].status == ReplayStatus.SESSION_FINALIZED
    assert sessions["s1"].ended_at is not None
    store.close()


def test_finalize_waits_for_the_thread_writer_to_drain(tmp_path):
    store = ReplayStore(tmp_path, writer="thread")
    store.begin_session(_session())
    for n in range(20):
        _record_one_analysis(store, analysis_id=f"a{n}")
    store.finalize_session("s1")
    assert len(store.index.analyses("s1")) == 20
    assert store.health.total == 0
    store.close()


def test_identical_bundles_are_stored_once(tmp_path):
    store = ReplayStore(tmp_path, writer="sync")
    store.begin_session(_session())
    _record_one_analysis(store, analysis_id="a1")
    _record_one_analysis(store, analysis_id="a2")
    store.finalize_session("s1")
    analyses = {a.analysis_id: a for a in store.index.analyses("s1")}
    assert analyses["a1"].bundle_object == analyses["a2"].bundle_object
    files = list((tmp_path / "objects").rglob("*.json")) + list((tmp_path / "objects").rglob("*.arrow"))
    # bundle json + frame arrow + settings + recommendation + observation payload
    assert len(files) == 5
    store.close()


# --------------------------------------------------------------------------- saturation


def test_queue_saturation_drops_the_record_loudly_and_never_raises(tmp_path, monkeypatch):
    from ba2_common.core.replay import service as service_module

    monkeypatch.setattr(service_module.logger, "error", lambda *a, **k: None)
    store = ReplayStore(tmp_path, writer="thread", queue_maxsize=1)
    store.begin_session(_session())

    release = threading.Event()
    original = store._write_pending

    def _blocked(pending):
        release.wait(timeout=30)
        return original(pending)

    monkeypatch.setattr(store, "_write_pending", _blocked)

    for n in range(8):
        _record_one_analysis(store, analysis_id=f"a{n}")

    assert store.health.queue_saturation > 0
    release.set()
    store.finalize_session("s1")

    coverage = store.index.coverage("s1")
    dropped = [c for c in coverage if c.status == ReplayStatus.COVERAGE_MISSING_CAPTURE]
    assert len(dropped) == store.health.queue_saturation
    assert dropped[0].capability == ReplayStatus.CAPABILITY_RECORDED_EXPERT
    persisted = len(store.index.analyses("s1"))
    assert persisted + len(dropped) == 8
    store.close()


def test_interrupted_sessions_are_marked_on_open(tmp_path):
    store = ReplayStore(tmp_path, writer="sync")
    store.begin_session(_session("s1"))
    _record_one_analysis(store)
    store.close()  # crash: never finalized

    reopened = ReplayStore(tmp_path, writer="sync")
    assert reopened.mark_interrupted_sessions() == ["s1"]
    assert reopened.index.list_sessions()[0].status == ReplayStatus.SESSION_INTERRUPTED
    reopened.close()


# --------------------------------------------------------------------------- export / load


def test_export_then_load_round_trips_and_verifies_hashes(tmp_path):
    store = ReplayStore(tmp_path / "store", writer="sync")
    store.begin_session(_session())
    _record_one_analysis(store)
    store.finalize_session("s1")
    out = store.export_session("s1", tmp_path / "export")
    store.close()

    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == SCHEMA_VERSION
    assert manifest["session"]["session_id"] == "s1"
    assert (out / "coverage.json").exists()
    for entry in manifest["objects"]:
        assert not entry["path"].startswith("/")
        assert ":" not in entry["path"]
        assert (out / entry["path"]).exists()

    bundle = load_bundle(out)
    assert bundle.session.session_id == "s1"
    assert bundle.session.status == ReplayStatus.SESSION_FINALIZED
    assert len(bundle.analyses) == 1
    record = bundle.analyses[0]
    assert isinstance(record, AnalysisRecord)
    assert record.expert_class == "DeterministicScorer"
    assert record.clock_reads == ()
    decoded = bundle.decode(record.bundle_object)
    pd.testing.assert_frame_equal(decoded["ohlcv"], _frame())
    assert bundle.decode(record.recommendation_object) == {"signal": "BUY", "confidence": 78.1}
    assert len(bundle.observations) == 1
    assert bundle.observations[0].request_identity == {"symbol": "AAPL"}
    assert bundle.observations_for(record.analysis_id) == bundle.observations
    assert ReplayStore.load_bundle(out).session.session_id == "s1"


def test_load_bundle_rejects_a_tampered_object(tmp_path):
    store = ReplayStore(tmp_path / "store", writer="sync")
    store.begin_session(_session())
    _record_one_analysis(store)
    store.finalize_session("s1")
    out = store.export_session("s1", tmp_path / "export")
    store.close()

    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    target = next(
        e for e in manifest["objects"]
        if e["kind"] == "json" and b"231.5" in (out / e["path"]).read_bytes()
    )
    path = out / target["path"]
    original = path.read_bytes()
    tampered = original.replace(b"231.5", b"999.5")
    assert tampered != original
    path.write_bytes(tampered)

    with pytest.raises(ObjectHashMismatch):
        load_bundle(out)


def test_load_bundle_rejects_a_future_schema_version(tmp_path):
    store = ReplayStore(tmp_path / "store", writer="sync")
    store.begin_session(_session())
    _record_one_analysis(store)
    store.finalize_session("s1")
    out = store.export_session("s1", tmp_path / "export")
    store.close()

    manifest_path = out / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = SCHEMA_VERSION + 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="schema_version"):
        load_bundle(out)


def test_health_is_shared_between_store_and_context(tmp_path):
    store = ReplayStore(tmp_path, writer="sync")
    assert isinstance(store.health, CaptureHealth)
    store.begin_session(_session())
    with capture_scope(store, _meta()) as ctx:
        assert ctx.health is store.health
        ctx.set_outcome(skip_reason="done")
    store.finalize_session("s1")
    store.close()


# --------------------------------------------------------------------------- closed store


def test_submitting_after_close_is_dropped_visibly_not_silently(tmp_path, monkeypatch):
    from ba2_common.core.replay import service as service_module

    monkeypatch.setattr(service_module.logger, "error", lambda *a, **k: None)
    store = ReplayStore(tmp_path, writer="thread")
    store.begin_session(_session())
    _record_one_analysis(store, analysis_id="a-early")
    store.finalize_session("s1")
    store.close()

    _record_one_analysis(store, analysis_id="a-late")  # must not raise, must not vanish

    assert store.closed
    assert store.health.other == 1
    dropped = [c for c in store.index.coverage("s1") if c.analysis_id == "a-late"]
    assert len(dropped) == 1
    assert dropped[0].status == ReplayStatus.COVERAGE_MISSING_CAPTURE
    assert "closed" in dropped[0].detail
    assert [a.analysis_id for a in store.index.analyses("s1")] == ["a-early"]
    store.index.close()


def test_drain_is_bounded_and_finalize_reports_what_never_landed(tmp_path, monkeypatch):
    from ba2_common.core.replay import service as service_module

    monkeypatch.setattr(service_module.logger, "error", lambda *a, **k: None)
    store = ReplayStore(tmp_path, writer="thread", queue_maxsize=8)
    store.begin_session(_session())

    release = threading.Event()
    original = store._write_pending
    monkeypatch.setattr(
        store, "_write_pending", lambda pending: (release.wait(timeout=30), original(pending))[1]
    )

    for n in range(3):
        _record_one_analysis(store, analysis_id=f"a{n}")

    started = time.monotonic()
    remaining = store.finalize_session("s1", timeout=0.2)
    assert time.monotonic() - started < 10, "finalize must not hang on a stuck writer"
    assert remaining > 0
    assert store.health.queue_saturation > 0
    sessions = {s.session_id: s for s in store.index.list_sessions()}
    assert sessions["s1"].status == ReplayStatus.SESSION_INTERRUPTED

    release.set()
    store.close(timeout=10)


def test_a_writer_failure_leaves_a_missing_capture_row(tmp_path, monkeypatch):
    from ba2_common.core.replay import service as service_module

    monkeypatch.setattr(service_module.logger, "error", lambda *a, **k: None)
    store = ReplayStore(tmp_path, writer="thread")
    store.begin_session(_session())

    def _boom(pending):
        raise OSError("disk went away")

    monkeypatch.setattr(store, "_write_pending", _boom)
    _record_one_analysis(store, analysis_id="a-doomed")
    assert store.drain(timeout=10) == 0

    assert store.health.disk_error == 1
    coverage = store.index.coverage("s1")
    assert [c.analysis_id for c in coverage] == ["a-doomed"]
    assert coverage[0].status == ReplayStatus.COVERAGE_MISSING_CAPTURE
    assert "writer failed" in coverage[0].detail
    assert store.index.analyses("s1") == []
    store.close()


# --------------------------------------------------------------------------- capture gaps


class _Opaque:
    """A value the codec refuses (a capture gap), but freeze copies happily."""


def test_an_unsupported_settings_object_is_a_named_gap_not_a_silent_one(tmp_path, monkeypatch):
    from ba2_common.core.replay import service as service_module

    monkeypatch.setattr(service_module.logger, "error", lambda *a, **k: None)
    store = ReplayStore(tmp_path, writer="sync")
    store.begin_session(_session())
    with capture_scope(store, _meta(settings={"opaque": _Opaque()})) as ctx:
        ctx.set_bundle({"symbol": "AAPL"})
        ctx.set_outcome(recommendation={"signal": "HOLD"})
    store.finalize_session("s1")

    record = store.index.analyses("s1")[0]
    assert record.capture_gaps == ("settings",)
    assert record.settings_object is None
    assert record.bundle_object is not None  # the rest of the analysis is intact
    assert record.recommendation_object is not None
    assert store.health.unsupported_type == 1

    coverage = store.index.coverage("s1")
    assert len(coverage) == 1
    assert coverage[0].status == ReplayStatus.COVERAGE_MISSING_CAPTURE
    assert "settings" in coverage[0].detail
    store.close()


def test_an_unsupported_bundle_is_marked_unsupported(tmp_path, monkeypatch):
    from ba2_common.core.replay import service as service_module

    monkeypatch.setattr(service_module.logger, "error", lambda *a, **k: None)
    store = ReplayStore(tmp_path, writer="sync")
    store.begin_session(_session())
    with capture_scope(store, _meta()) as ctx:
        ctx.set_bundle({"opaque": _Opaque()})
        ctx.set_outcome(skip_reason="no data")
    store.finalize_session("s1")

    record = store.index.analyses("s1")[0]
    assert record.bundle_capture_status == ReplayStatus.CAPTURE_UNSUPPORTED
    assert record.capture_gaps == ("bundle",)
    assert record.outcome == ReplayStatus.OUTCOME_SKIP
    store.close()


def test_concurrent_observations_land_as_distinct_index_rows(tmp_path):
    store = ReplayStore(tmp_path, writer="sync")
    store.begin_session(_session())
    with capture_scope(store, _meta()) as ctx:
        def _record(n):
            ctx.record_observation(
                provider="fmp",
                method="quote",
                request_identity={"symbol": f"S{n}"},
                payload=[n],
                provenance=ReplayStatus.PROVENANCE_NETWORK,
            )

        threads = [threading.Thread(target=_record, args=(n,)) for n in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        ctx.set_outcome(skip_reason="done")
    store.finalize_session("s1")

    observations = store.index.observations("a1")
    assert len(observations) == 6
    assert len({o.observation_id for o in observations}) == 6
    assert {tuple(o.request_identity.items()) for o in observations} == {
        (("symbol", f"S{n}"),) for n in range(6)
    }
    store.close()
