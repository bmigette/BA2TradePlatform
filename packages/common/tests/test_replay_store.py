"""Task 1 (spec step 1): the immutable object store and the SQLite replay index.

Spec section 3 "Atomicity, concurrency and failure behavior": write immutable
objects to temp files, verify hashes, publish atomically, THEN commit references
in the dedicated index. A crash between the two leaves an orphan object, never a
half-referenced analysis. Two processes writing concurrently must both succeed.
"""
import multiprocessing as mp
import os
import threading
from datetime import datetime, timezone

import pytest

from ba2_common.core.replay._spawn_child import SHARED_PAYLOAD, write_session_in_child
from ba2_common.core.replay.codec import content_hash, encode
from ba2_common.core.replay.schemas import (
    AnalysisRecord,
    CoverageEntry,
    ProviderObservation,
    ReplayStatus,
    SessionRecord,
)
from ba2_common.core.replay.store import (
    ObjectHashMismatch,
    ObjectNotFound,
    ObjectRef,
    ObjectStore,
    ReplayIndex,
    ReplayStoreError,
)

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


def _analysis(session_id="s1", analysis_id="a1", **kwargs):
    fields = dict(
        analysis_id=analysis_id,
        attempt_id="att-1",
        session_id=session_id,
        expert_class="FMPRating",
        expert_instance_id=12,
        symbol="AAPL",
        use_case=ReplayStatus.USE_CASE_ENTER_MARKET,
        scheduled_at=UTC_NOW,
        started_at=UTC_NOW,
        finished_at=UTC_NOW,
        settings_hash="cafe",
        settings_object=None,
        bundle_object=None,
        bundle_capture_status=ReplayStatus.CAPTURE_NOT_ATTEMPTED,
        clock_reads=["2026-09-10T13:00:00+00:00"],
        outcome=ReplayStatus.OUTCOME_SKIP,
        recommendation_object=None,
        skip_reason="no data",
        error=None,
        observation_ids=[],
        branch_flags={"as_of_is_none": True},
    )
    fields.update(kwargs)
    return AnalysisRecord(**fields)


def _observation(session_id, analysis_id, seq, payload_hash):
    return ProviderObservation(
        observation_id=f"{analysis_id}-obs-{seq}",
        session_id=session_id,
        analysis_ids=[analysis_id],
        provider="fmp",
        method="price_target_consensus",
        request_identity={"symbol": "AAPL", "seq": seq},
        invocation_seq=seq,
        payload_object=payload_hash,
        payload_kind=ReplayStatus.PAYLOAD_JSON,
        response_class=ReplayStatus.RESPONSE_DATA,
        content_hash=payload_hash,
        fetched_at=None,
        observed_at=UTC_NOW,
        consumed_at=UTC_NOW,
        published_at=None,
        first_observed_at=None,
        provenance=ReplayStatus.PROVENANCE_NETWORK,
    )


# --------------------------------------------------------------------------- ObjectStore


def test_put_is_content_addressed_and_idempotent(tmp_path):
    store = ObjectStore(tmp_path)
    kind, data, _meta, _ = encode({"a": 1})
    first = store.put(kind, data)
    second = store.put(kind, data)
    assert first == second == content_hash(kind, data)
    assert store.exists(first)
    path = store.path_for(first, kind)
    assert path.exists()
    assert path.parent.name == first[:2]
    assert path.parent.parent.name == "objects"


def test_get_returns_kind_bytes_and_meta(tmp_path):
    store = ObjectStore(tmp_path)
    kind, data, _meta, _ = encode({"a": 1, "b": None})
    object_hash = store.put(kind, data)
    got_kind, got_data, got_meta = store.get(object_hash)
    assert got_kind == kind
    assert got_data == data
    assert got_meta["codec_version"] == _meta["codec_version"]


def test_get_of_a_missing_object_raises(tmp_path):
    store = ObjectStore(tmp_path)
    with pytest.raises(ObjectNotFound):
        store.get("0" * 64)


def test_corrupted_object_raises_on_read(tmp_path):
    store = ObjectStore(tmp_path)
    kind, data, _meta, _ = encode({"a": 1})
    object_hash = store.put(kind, data)
    path = store.path_for(object_hash, kind)
    path.write_bytes(b'{"codec_version":1,"object_kind":"json","payload":{"a":2}}')
    with pytest.raises(ObjectHashMismatch):
        store.get(object_hash)


def test_no_temp_files_are_left_behind(tmp_path):
    store = ObjectStore(tmp_path)
    kind, data, _meta, _ = encode({"a": 1})
    store.put(kind, data)
    leftovers = [p for p in (tmp_path / "objects").rglob("*.tmp")]
    assert leftovers == []


# --------------------------------------------------------------------------- orphans


def test_object_written_without_an_index_commit_is_an_orphan(tmp_path):
    """Simulated crash between object publish and index commit."""
    index = ReplayIndex(tmp_path / "index.sqlite")
    store = ObjectStore(tmp_path, index=index)
    index.begin_session(_session())

    referenced_kind, referenced_data, _rmeta, _ = encode({"kept": True})
    referenced = store.put(referenced_kind, referenced_data)
    index.commit_analysis(
        _analysis(bundle_object=referenced, bundle_capture_status=ReplayStatus.CAPTURE_CAPTURED),
        objects=[ObjectRef(hash=referenced, kind=referenced_kind, size=len(referenced_data))],
        verify_objects=store,
    )

    orphan_kind, orphan_data, _ometa, _ = encode({"crashed": True})
    orphan = store.put(orphan_kind, orphan_data)

    orphans = list(store.iter_orphans(grace_seconds=0))
    assert orphans == [orphan]
    assert store.exists(referenced)
    assert index.has_object(referenced)
    assert not index.has_object(orphan)
    index.close()


def test_orphans_inside_the_grace_period_are_not_listed(tmp_path):
    index = ReplayIndex(tmp_path / "index.sqlite")
    store = ObjectStore(tmp_path, index=index)
    kind, data, _meta, _ = encode({"fresh": True})
    store.put(kind, data)
    assert list(store.iter_orphans(grace_seconds=3600)) == []
    assert len(list(store.iter_orphans(grace_seconds=0))) == 1
    index.close()


# --------------------------------------------------------------------------- index


def test_commit_analysis_is_transactional_and_reference_complete(tmp_path):
    index = ReplayIndex(tmp_path / "index.sqlite")
    store = ObjectStore(tmp_path, index=index)
    index.begin_session(_session())

    missing = "1" * 64
    with pytest.raises(ReplayStoreError):
        index.commit_analysis(
            _analysis(bundle_object=missing, bundle_capture_status=ReplayStatus.CAPTURE_CAPTURED),
            objects=[ObjectRef(hash=missing, kind="json", size=2)],
            verify_objects=store,
        )
    assert index.analyses("s1") == []
    assert index.observations("a1") == []
    index.close()


def test_sessions_analyses_observations_and_coverage_round_trip(tmp_path):
    index = ReplayIndex(tmp_path / "index.sqlite")
    store = ObjectStore(tmp_path, index=index)
    index.begin_session(_session())

    kind, data, _meta, _ = encode({"consensus": [{"targetHigh": 1.5}]})
    payload_hash = store.put(kind, data)
    observation = _observation("s1", "a1", 0, payload_hash)
    record = _analysis(
        bundle_object=payload_hash,
        bundle_capture_status=ReplayStatus.CAPTURE_CAPTURED,
        observation_ids=[observation.observation_id],
        outcome=ReplayStatus.OUTCOME_RECOMMENDATION,
        skip_reason=None,
    )
    index.commit_analysis(
        record,
        observations=[observation],
        coverage=[
            CoverageEntry(
                session_id="s1",
                analysis_id="a1",
                capability=ReplayStatus.CAPABILITY_RECORDED_EXPERT,
                status=ReplayStatus.COVERAGE_NOT_RUN,
                detail="replay has not run",
            )
        ],
        objects=[ObjectRef(hash=payload_hash, kind=kind, size=len(data))],
        verify_objects=store,
    )

    sessions = index.list_sessions()
    assert [s.session_id for s in sessions] == ["s1"]
    assert sessions[0].package_versions == {"ba2_common": "0.1.0"}
    assert sessions[0].started_at == UTC_NOW

    analyses = index.analyses("s1")
    assert len(analyses) == 1
    assert analyses[0] == record
    assert analyses[0].branch_flags == {"as_of_is_none": True}

    observations = index.observations("a1")
    assert len(observations) == 1
    assert observations[0] == observation
    assert observations[0].request_identity == {"symbol": "AAPL", "seq": 0}

    coverage = index.coverage("s1")
    assert len(coverage) == 1
    assert coverage[0].status == ReplayStatus.COVERAGE_NOT_RUN
    assert index.referenced_hashes() == {payload_hash}
    index.close()


def test_open_sessions_are_marked_interrupted_on_restart(tmp_path):
    index = ReplayIndex(tmp_path / "index.sqlite")
    index.begin_session(_session("s1"))
    index.begin_session(_session("s2"))
    index.update_session_status("s2", ReplayStatus.SESSION_FINALIZED, ended_at=UTC_NOW)
    index.close()

    reopened = ReplayIndex(tmp_path / "index.sqlite")
    assert reopened.mark_interrupted() == ["s1"]
    by_id = {s.session_id: s for s in reopened.list_sessions()}
    assert by_id["s1"].status == ReplayStatus.SESSION_INTERRUPTED
    assert by_id["s2"].status == ReplayStatus.SESSION_FINALIZED
    assert by_id["s2"].ended_at == UTC_NOW
    reopened.close()


# --------------------------------------------------------------------------- concurrency


# The child target lives in ``ba2_common.core.replay._spawn_child``, NOT here.
# ``spawn`` re-imports the target by qualified name in a fresh interpreter, and
# this file's package is importable as ``tests`` -- as are three other test
# packages in this repo. Which one the child resolved depended on the order
# pytest inserted those roots, i.e. on which files the run collected, so the
# child died with ModuleNotFoundError in a full run and passed on its own.


def test_two_processes_write_the_same_root_concurrently(tmp_path):
    root = str(tmp_path)
    ctx = mp.get_context("spawn")
    procs = [
        ctx.Process(target=write_session_in_child, args=(root, f"s{n}", 50))
        for n in (1, 2)
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=180)
    assert [p.exitcode for p in procs] == [0, 0]

    index = ReplayIndex(tmp_path / "index.sqlite")
    assert sorted(s.session_id for s in index.list_sessions()) == ["s1", "s2"]
    total = len(index.observations("s1-a1")) + len(index.observations("s2-a1"))
    assert total == 100
    assert len(index.referenced_hashes()) == 100

    # the object both children published exists exactly once, uncorrupted
    store = ObjectStore(tmp_path, index=index)
    kind, data, _meta, _ = encode(SHARED_PAYLOAD)
    shared_hash = content_hash(kind, data)
    assert store.exists(shared_hash)
    assert store.get(shared_hash)[1] == data
    assert len(list((tmp_path / "objects").rglob(f"{shared_hash}*"))) == 1
    index.close()


def test_close_only_touches_this_threads_connection(tmp_path):
    """sqlite3 refuses a cross-thread close; the file must still be unlocked."""
    index = ReplayIndex(tmp_path / "index.sqlite")
    index.begin_session(_session())
    errors = []

    def _worker():
        index.begin_session(_session("s-worker"))  # opens this thread's connection
        assert index.close() >= 1  # the main thread's connection is still open
        errors.extend(())

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join(timeout=30)
    assert not thread.is_alive()
    assert errors == []

    assert index.close() == 0
    # Windows keeps an open sqlite handle locked: a rename proves both closed.
    target = tmp_path / "index.sqlite.moved"
    os.replace(tmp_path / "index.sqlite", target)
    assert target.exists()


def test_index_reopens_after_close_so_late_gaps_are_still_recorded(tmp_path):
    index = ReplayIndex(tmp_path / "index.sqlite")
    index.begin_session(_session())
    index.close()
    index.record_coverage(
        [
            CoverageEntry(
                session_id="s1",
                analysis_id="late",
                capability=ReplayStatus.CAPABILITY_RECORDED_EXPERT,
                status=ReplayStatus.COVERAGE_MISSING_CAPTURE,
                detail="arrived after close",
            )
        ]
    )
    assert [c.analysis_id for c in index.coverage("s1")] == ["late"]
    index.close()
