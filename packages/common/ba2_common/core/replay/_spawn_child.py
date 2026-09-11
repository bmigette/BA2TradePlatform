"""A spawn-safe child entry point for the store's cross-process test.

WHY THIS IS NOT IN THE TEST FILE. ``multiprocessing`` with the ``spawn`` start
method (the only one on Windows) pickles the child target BY QUALIFIED NAME and
re-imports it in a fresh interpreter. The test package is importable as ``tests``
-- and so are three other test packages in this repo, all of them reachable on
the same ``sys.path``. Which ``tests`` the child resolved therefore depended on
the ORDER pytest happened to insert those roots, which depends on which files the
run collected: the child imported the repo-root ``tests`` package, found no
``test_replay_store`` in it, and died with ``ModuleNotFoundError`` -- but only in
a multi-file run, which reads as a flaky concurrency test rather than as the
name collision it is.

``ba2_common.core.replay._spawn_child`` is one name in one package, so it
resolves identically in every process regardless of collection order.

Nothing here is imported by the capture path; it exists for the concurrency test
and for anyone reproducing a two-process write by hand.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from ba2_common.core.replay.codec import encode
from ba2_common.core.replay.schemas import (
    AnalysisRecord,
    ProviderObservation,
    ReplayStatus,
    SessionRecord,
)
from ba2_common.core.replay.store import ObjectRef, ObjectStore, ReplayIndex

#: The fixed instant every record written here carries, so two runs of the child
#: produce byte-identical records apart from the session id.
UTC_NOW = datetime(2026, 9, 10, 13, 0, tzinfo=timezone.utc)

#: The object BOTH children publish, to exercise two processes racing on the same
#: content-addressed path.
SHARED_PAYLOAD = {"shared": "both children write me"}


def session_record(session_id: str) -> SessionRecord:
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


def analysis_record(session_id: str, analysis_id: str, observation_ids) -> AnalysisRecord:
    return AnalysisRecord(
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
        clock_reads=["2026-09-10T13:00:00+00:00"],
        outcome=ReplayStatus.OUTCOME_SKIP,
        skip_reason="no data",
        observation_ids=list(observation_ids),
        branch_flags={"as_of_is_none": True},
    )


def observation_record(session_id: str, analysis_id: str, seq: int,
                       payload_hash: str) -> ProviderObservation:
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
        observed_at=UTC_NOW,
        consumed_at=UTC_NOW,
        provenance=ReplayStatus.PROVENANCE_NETWORK,
    )


def write_session_in_child(root: str, session_id: str, count: int) -> int:
    """Write one session of ``count`` observations into the store at ``root``."""
    index = ReplayIndex(os.path.join(root, "index.sqlite"))
    store = ObjectStore(root, index=index)
    index.begin_session(session_record(session_id))

    observations = []
    refs = []
    analysis_id = f"{session_id}-a1"
    for seq in range(count):
        encoded = encode({"session": session_id, "seq": seq})
        payload_hash = store.put(encoded.kind, encoded.data)
        refs.append(ObjectRef(hash=payload_hash, kind=encoded.kind,
                              size=len(encoded.data)))
        observations.append(
            observation_record(session_id, analysis_id, seq, payload_hash))

    index.commit_analysis(
        analysis_record(session_id, analysis_id,
                        [o.observation_id for o in observations]),
        observations=observations,
        objects=refs,
        verify_objects=store,
    )

    # Both children publish this IDENTICAL object: two processes racing on the
    # same content-addressed path must both succeed (Windows os.replace can fail
    # when the loser's destination is held open by the other).
    shared = encode(SHARED_PAYLOAD)
    store.put(shared.kind, shared.data)

    index.update_session_status(
        session_id, ReplayStatus.SESSION_FINALIZED, ended_at=UTC_NOW)
    index.close()
    return 0
