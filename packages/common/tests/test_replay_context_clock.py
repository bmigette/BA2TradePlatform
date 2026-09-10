"""Task 1 (spec step 1): the per-analysis capture context and the clock seam.

Recording is observational: a failure inside recording must be counted in
CaptureHealth and logged once per analysis, never raised into the expert.
`replay_now` keeps production semantics (wall clock) and, in replay mode, hands
back the recorded reads in order — running out is a loud ReplayMiss, never a
fresh wall-clock read.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from ba2_common.core.replay.clock import ReplayMiss, replay_now
from ba2_common.core.replay.context import (
    CaptureContext,
    CaptureHealth,
    capture_aware_submit,
    capture_scope,
    current_capture,
    run_in_capture_context,
    use_capture_context,
)
from ba2_common.core.replay.schemas import ReplayStatus

UTC_NOW = datetime(2026, 9, 10, 13, 0, tzinfo=timezone.utc)


class _FakeStore:
    """The submit boundary capture_scope talks to."""

    def __init__(self, raises=None):
        self.health = CaptureHealth()
        self.submitted = []
        self._raises = raises

    def submit(self, record, objects=None, observations=()):
        if self._raises is not None:
            raise self._raises
        self.submitted.append((record, dict(objects or {}), list(observations)))


def _meta(analysis_id="a1", **kwargs):
    meta = {
        "analysis_id": analysis_id,
        "attempt_id": "att-1",
        "session_id": "s1",
        "expert_class": "FMPRating",
        "expert_instance_id": 12,
        "symbol": "AAPL",
        "use_case": ReplayStatus.USE_CASE_ENTER_MARKET,
        "scheduled_at": UTC_NOW,
        "started_at": UTC_NOW,
    }
    meta.update(kwargs)
    return meta


# --------------------------------------------------------------------------- context var


def test_no_context_when_capture_is_off():
    assert current_capture() is None
    assert CaptureContext.current() is None
    with capture_scope(None, _meta()) as ctx:
        assert ctx is None
        assert current_capture() is None
        assert CaptureContext.current() is None
    assert current_capture() is None


def test_capture_scope_publishes_and_clears_the_context():
    store = _FakeStore()
    with capture_scope(store, _meta()) as ctx:
        assert current_capture() is ctx
        assert CaptureContext.current() is ctx
        assert ctx.mode == ReplayStatus.MODE_CAPTURE
        ctx.set_outcome(skip_reason="not enough analysts")
    assert current_capture() is None
    assert len(store.submitted) == 1
    record, objects, observations = store.submitted[0]
    assert record.analysis_id == "a1"
    assert record.outcome == ReplayStatus.OUTCOME_SKIP
    assert record.skip_reason == "not enough analysts"
    assert record.finished_at is not None
    assert observations == []


def test_scope_records_bundle_settings_outcome_and_observations():
    store = _FakeStore()
    bundle = {"symbol": "AAPL", "rows": [{"eps": 1.0}]}
    with capture_scope(store, _meta(settings={"threshold": 0.5})) as ctx:
        ctx.set_bundle(bundle)
        bundle["rows"][0]["eps"] = 99.0  # mutated AFTER capture
        observation_id = ctx.record_observation(
            provider="fmp",
            method="price_target_consensus",
            request_identity={"symbol": "AAPL"},
            payload=[{"targetHigh": 1.5}],
            provenance=ReplayStatus.PROVENANCE_NETWORK,
        )
        ctx.set_outcome(recommendation={"signal": "BUY"})

    record, objects, observations = store.submitted[0]
    assert record.bundle_capture_status == ReplayStatus.CAPTURE_CAPTURED
    assert record.outcome == ReplayStatus.OUTCOME_RECOMMENDATION
    assert record.observation_ids == (observation_id,)
    assert objects["bundle"]["rows"] == [{"eps": 1.0}]  # frozen before the mutation
    assert objects["settings"] == {"threshold": 0.5}
    assert objects["recommendation"] == {"signal": "BUY"}
    assert len(observations) == 1
    pending = observations[0]
    assert pending.observation.provider == "fmp"
    assert pending.observation.invocation_seq == 0
    assert pending.observation.payload_kind == ReplayStatus.PAYLOAD_JSON
    assert pending.observation.response_class == ReplayStatus.RESPONSE_DATA
    assert pending.observation.analysis_ids == ("a1",)
    assert pending.payload == [{"targetHigh": 1.5}]


def test_observations_are_numbered_in_invocation_order():
    store = _FakeStore()
    with capture_scope(store, _meta()) as ctx:
        for n in range(3):
            ctx.record_observation(
                provider="fmp",
                method="upgrade_downgrade",
                request_identity={"symbol": "AAPL", "page": n},
                payload=[n],
                provenance=ReplayStatus.PROVENANCE_DISK_CACHE,
            )
        ctx.set_outcome(skip_reason="done")
    observations = store.submitted[0][2]
    assert [p.observation.invocation_seq for p in observations] == [0, 1, 2]


def test_empty_and_missing_payloads_are_classified_not_dropped():
    store = _FakeStore()
    with capture_scope(store, _meta()) as ctx:
        ctx.record_observation(
            provider="fmp",
            method="a",
            request_identity={},
            payload=None,
            provenance=ReplayStatus.PROVENANCE_UNKNOWN,
        )
        ctx.record_observation(
            provider="fmp",
            method="b",
            request_identity={},
            payload=[],
            provenance=ReplayStatus.PROVENANCE_UNKNOWN,
        )
        ctx.record_observation(
            provider="fmp",
            method="c",
            request_identity={},
            payload=12.5,
            provenance=ReplayStatus.PROVENANCE_MEMO_CACHE,
        )
        ctx.set_outcome(skip_reason="done")
    observations = [p.observation for p in store.submitted[0][2]]
    assert [o.payload_kind for o in observations] == [
        ReplayStatus.PAYLOAD_NONE,
        ReplayStatus.PAYLOAD_JSON,
        ReplayStatus.PAYLOAD_SCALAR,
    ]
    assert [o.response_class for o in observations] == [
        ReplayStatus.RESPONSE_EMPTY,
        ReplayStatus.RESPONSE_EMPTY,
        ReplayStatus.RESPONSE_DATA,
    ]


def test_an_exception_escaping_the_scope_is_recorded_and_re_raised():
    store = _FakeStore()
    with pytest.raises(ValueError, match="boom"):
        with capture_scope(store, _meta()) as ctx:
            ctx.set_bundle({"symbol": "AAPL"})
            raise ValueError("boom")
    record = store.submitted[0][0]
    assert record.outcome == ReplayStatus.OUTCOME_ERROR
    assert "boom" in record.error
    assert "ValueError" in record.error


def test_an_analysis_that_sets_no_outcome_is_still_recorded_as_an_error():
    """No silent failure: a scope must never produce an outcome-less record."""
    store = _FakeStore()
    with capture_scope(store, _meta()):
        pass
    record = store.submitted[0][0]
    assert record.outcome == ReplayStatus.OUTCOME_ERROR
    assert record.error


# --------------------------------------------------------------------------- health


class _Unfreezable:
    def __deepcopy__(self, memo):
        raise RuntimeError("cannot copy")


def test_a_recording_failure_never_propagates_into_the_expert(monkeypatch):
    from ba2_common.core.replay import context as context_module

    errors = []
    monkeypatch.setattr(context_module.logger, "error", lambda msg, *a, **k: errors.append(msg))

    store = _FakeStore()
    with capture_scope(store, _meta()) as ctx:
        ctx.set_bundle({"bad": _Unfreezable()})
        ctx.record_observation(
            provider="fmp",
            method="x",
            request_identity={},
            payload=_Unfreezable(),
            provenance=ReplayStatus.PROVENANCE_UNKNOWN,
        )
        ctx.set_outcome(recommendation={"signal": "HOLD"})

    assert store.health.other == 2
    record = store.submitted[0][0]
    assert record.bundle_capture_status == ReplayStatus.CAPTURE_FAILED
    assert record.outcome == ReplayStatus.OUTCOME_RECOMMENDATION
    assert len(errors) == 1, "capture health is logged once per analysis"


def test_a_failing_submit_is_swallowed_and_counted(monkeypatch):
    from ba2_common.core.replay import context as context_module

    monkeypatch.setattr(context_module.logger, "error", lambda *a, **k: None)
    store = _FakeStore(raises=OSError("disk full"))
    with capture_scope(store, _meta()) as ctx:
        ctx.set_outcome(skip_reason="done")
    assert store.health.disk_error == 1


def test_capture_health_snapshot_is_a_plain_dict():
    health = CaptureHealth()
    health.record(CaptureHealth.QUEUE_SATURATION)
    health.record(CaptureHealth.QUEUE_SATURATION)
    health.record(CaptureHealth.UNSUPPORTED_TYPE)
    snapshot = health.as_dict()
    assert snapshot == {
        "queue_saturation": 2,
        "disk_error": 0,
        "unsupported_type": 1,
        "other": 0,
    }
    assert health.total == 3


# --------------------------------------------------------------------------- clock


def test_replay_now_returns_as_of_when_given():
    as_of = datetime(2024, 5, 1, 12, 0, tzinfo=timezone.utc)
    assert replay_now(as_of) is as_of
    store = _FakeStore()
    with capture_scope(store, _meta()) as ctx:
        assert replay_now(as_of) is as_of
        ctx.set_outcome(skip_reason="done")
    assert store.submitted[0][0].clock_reads == ()


def test_replay_now_without_a_context_is_a_plain_utc_wall_clock():
    before = datetime.now(timezone.utc)
    now = replay_now()
    after = datetime.now(timezone.utc)
    assert now.tzinfo is not None
    assert before - timedelta(seconds=1) <= now <= after + timedelta(seconds=1)


def test_capture_mode_records_every_clock_read_in_order():
    store = _FakeStore()
    with capture_scope(store, _meta()) as ctx:
        first = replay_now()
        second = replay_now()
        ctx.set_outcome(skip_reason="done")
    reads = store.submitted[0][0].clock_reads
    assert len(reads) == 2
    assert reads[0] == first.isoformat()
    assert reads[1] == second.isoformat()
    assert first <= second


def test_replay_mode_returns_the_recorded_reads_then_raises():
    recorded = [
        "2026-09-10T13:00:00+00:00",
        "2026-09-10T13:00:05+00:00",
    ]
    ctx = CaptureContext.for_replay(analysis_id="a1", clock_reads=recorded)
    with use_capture_context(ctx):
        assert ctx.mode == ReplayStatus.MODE_REPLAY
        assert replay_now() == datetime(2026, 9, 10, 13, 0, tzinfo=timezone.utc)
        assert replay_now() == datetime(2026, 9, 10, 13, 0, 5, tzinfo=timezone.utc)
        with pytest.raises(ReplayMiss) as excinfo:
            replay_now()
    assert excinfo.value.kind == "clock_read"
    assert excinfo.value.analysis_id == "a1"
    assert current_capture() is None


def test_replay_mode_still_honours_an_explicit_as_of():
    as_of = datetime(2024, 5, 1, tzinfo=timezone.utc)
    ctx = CaptureContext.for_replay(analysis_id="a1", clock_reads=[])
    with use_capture_context(ctx):
        assert replay_now(as_of) is as_of


# --------------------------------------------------------------------------- concurrency


def test_concurrent_observations_get_distinct_ids(monkeypatch):
    """Two provider calls racing inside one analysis must not share an id.

    A shared id is silently destructive: the index writes observations with
    INSERT OR REPLACE, so the second row would overwrite the first.
    """
    from ba2_common.core.replay import context as context_module

    real_freeze = context_module.freeze

    def _slow_freeze(obj):
        time.sleep(0.01)  # widen the window between drawing a seq and appending
        return real_freeze(obj)

    monkeypatch.setattr(context_module, "freeze", _slow_freeze)

    store = _FakeStore()
    ids = []
    with capture_scope(store, _meta()) as ctx:
        def _record(n):
            ids.append(
                ctx.record_observation(
                    provider="fmp",
                    method="quote",
                    request_identity={"symbol": f"S{n}"},
                    payload=[n],
                    provenance=ReplayStatus.PROVENANCE_NETWORK,
                )
            )

        threads = [threading.Thread(target=_record, args=(n,)) for n in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert not any(t.is_alive() for t in threads)
        ctx.set_outcome(skip_reason="done")

    assert len(ids) == 6
    assert len(set(ids)) == 6
    record, _objects, observations = store.submitted[0]
    assert len(observations) == 6
    assert len(set(record.observation_ids)) == 6
    assert sorted(p.observation.invocation_seq for p in observations) == [0, 1, 2, 3, 4, 5]


def test_pool_workers_only_see_the_context_through_the_helpers():
    """A ContextVar does not cross into a ThreadPoolExecutor worker by itself."""
    store = _FakeStore()
    with capture_scope(store, _meta()) as ctx:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert pool.submit(current_capture).result(timeout=30) is None
            assert capture_aware_submit(pool, current_capture).result(timeout=30) is ctx
            wrapped = run_in_capture_context(current_capture)
            assert pool.submit(wrapped).result(timeout=30) is ctx
            # safe to reuse the same wrapper concurrently (executor.map style)
            assert list(pool.map(lambda _: wrapped(), range(4))) == [ctx] * 4
        ctx.set_outcome(skip_reason="done")


def test_a_clock_read_inside_a_pool_worker_is_recorded_via_the_helper():
    store = _FakeStore()
    with capture_scope(store, _meta()) as ctx:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(run_in_capture_context(replay_now)).result(timeout=30)
        ctx.set_outcome(skip_reason="done")
    assert len(store.submitted[0][0].clock_reads) == 1


def test_the_record_carries_this_analysis_own_failure_counts(monkeypatch):
    from ba2_common.core.replay import context as context_module

    monkeypatch.setattr(context_module.logger, "error", lambda *a, **k: None)
    store = _FakeStore()
    with capture_scope(store, _meta()) as ctx:
        ctx.set_bundle({"bad": _Unfreezable()})
        ctx.record_observation(
            provider="fmp",
            method="x",
            request_identity={},
            payload=_Unfreezable(),
            provenance=ReplayStatus.PROVENANCE_UNKNOWN,
        )
        ctx.set_outcome(skip_reason="done")
    record = store.submitted[0][0]
    assert record.capture_failures == {"other": 2}
    assert record.capture_gaps == ()


def test_a_clean_analysis_records_no_failures():
    store = _FakeStore()
    with capture_scope(store, _meta()) as ctx:
        ctx.set_bundle({"symbol": "AAPL"})
        ctx.set_outcome(skip_reason="done")
    assert store.submitted[0][0].capture_failures == {}


# --------------------------------------------------------------------------- skip


def test_a_skip_records_its_reason_and_no_recommendation_object():
    """A row must say ONE thing: the analysis skipped, and on what.

    Carrying a recommendation object as well would leave a reader guessing which
    of the two the live platform acted on.
    """
    from ba2_common.core.replay.context import MissingSkipReason  # noqa: F401

    context = CaptureContext(analysis_meta=_meta(), health=CaptureHealth())
    context.set_outcome(recommendation={"signal": "HOLD"})
    context.set_skip("no consensus data")

    record = context.build_record()
    assert record.outcome == ReplayStatus.OUTCOME_SKIP
    assert record.skip_reason == "no consensus data"
    assert "recommendation" not in context.objects, (
        "a skip must not also leave a recommendation on the row")


def test_a_reasonless_skip_is_a_typed_error_not_a_degraded_capture():
    """``skip=True`` with no reason is a CONTRACT breach, not a recording failure.

    Reporting it as "capture degraded" would blame the recorder for a defect in
    the expert, and would leave a skip row nobody can act on.
    """
    from ba2_common.core.replay.context import MissingSkipReason

    context = CaptureContext(analysis_meta=_meta(), health=CaptureHealth())
    for empty in (None, "", "   "):
        with pytest.raises(MissingSkipReason):
            context.set_skip(empty)
    assert context.capture_failures == {}, (
        "a contract breach must not be counted as capture degradation")
    assert context.health.total == 0
