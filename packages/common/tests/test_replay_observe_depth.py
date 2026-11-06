"""Nested taps record their depth (an inner tapped read called from inside a tapped read).

``option_capital_equity`` calls ``self.get_account_snapshot()``: both record, but a replay serves the
OUTER read directly, so an inner observation left on the tape misaligns a later direct read of the
same identity. The wrapper therefore stamps ``tap_depth`` (0 = called by the action itself), and
the option tape ignores depth > 0.
"""
from datetime import datetime, timezone

import pytest

from ba2_common.core.replay.context import CaptureHealth, capture_scope
from ba2_common.core.replay.observe import observe_provider
from ba2_common.core.replay.schemas import ProviderObservation, ReplayStatus

UTC_NOW = datetime(2026, 11, 5, 14, 0, tzinfo=timezone.utc)


class _Store:
    def __init__(self):
        self.health = CaptureHealth()
        self.submitted = []

    def submit(self, record, objects=None, observations=()):
        self.submitted.append((record, dict(objects or {}), list(observations)))


def _meta():
    return {"analysis_id": "a1", "attempt_id": "att-1", "session_id": "s1",
            "expert_class": "TradeManager", "expert_instance_id": 7, "symbol": "AAPL",
            "use_case": ReplayStatus.USE_CASE_ENTER_MARKET,
            "scheduled_at": UTC_NOW, "started_at": UTC_NOW}


class _Acct:
    @observe_provider("p", "inner", identity=lambda a: {})
    def inner(self):
        return 1

    @observe_provider("p", "outer", identity=lambda a: {})
    def outer(self):
        return self.inner() + 1

    @observe_provider("p", "boom", identity=lambda a: {}, record_errors=True)
    def boom(self):
        self.inner()
        raise ConnectionError("down")


def _depths(store):
    return [(p.observation.method, p.observation.tap_depth) for p in store.submitted[0][2]]


def test_a_nested_tap_records_depth_one_and_the_outer_depth_zero():
    store = _Store()
    with capture_scope(store, _meta()):
        assert _Acct().outer() == 2
        _Acct().inner()
    assert _depths(store) == [("inner", 1), ("outer", 0), ("inner", 0)]


def test_the_depth_is_restored_when_the_outer_read_raises():
    store = _Store()
    with capture_scope(store, _meta()):
        with pytest.raises(ConnectionError):
            _Acct().boom()
        _Acct().inner()
    assert _depths(store) == [("inner", 1), ("boom", 0), ("inner", 0)]


def test_depth_survives_the_mapping_round_trip_and_defaults_to_zero_for_old_records():
    store = _Store()
    with capture_scope(store, _meta()):
        _Acct().outer()
    observation = store.submitted[0][2][0].observation
    assert ProviderObservation.from_mapping(observation.to_mapping()).tap_depth == 1
    legacy = observation.to_mapping()
    del legacy["tap_depth"]
    assert ProviderObservation.from_mapping(legacy).tap_depth == 0


def test_outside_a_scope_nothing_is_touched():
    from ba2_common.core.replay import observe

    assert _Acct().outer() == 2
    assert getattr(observe._tap_state, "depth", 0) == 0
